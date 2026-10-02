# SPDX-License-Identifier: Apache-2.0
"""`medicalos-verify` -- the seven checks. Chapter 7 section 7.12.3.

    MOS-EVID-113  every criterion in the bound `AcceptanceCriteria` version appears in
                  `criteria_results`. A report that omits one MUST fail verification.
    MOS-EVID-114  `verdict` MUST be re-derivable from `criteria_results` by the published
                  combination rule, and a mismatch MUST fail with a DISTINCT exit code.
    MOS-EVID-122  the bundle is self-contained: no network access and no MedicalOS
                  instance is needed to verify it.
    MOS-EVID-123  exactly these seven checks, in this order, stopping at the first
                  failure, with these exit codes:

                    1 checksums ............................. 2
                    2 DSSE payload == report.json ........... 3
                    3 a signature verifies against the trust bundle ... 4
                    4 report.json validates against its schema ....... 5
                    5 aggregates recomputed from case_metrics.csv .... 6
                    6 criteria re-evaluated, verdict reproduced ...... 7
                    7 input digests match the bundled files ......... 8

                  Exit 0 means all seven passed. Exit 1 is reserved for usage errors.
    MOS-EVID-124  verification MUST NOT require network access, and the verifier MUST
                  REFUSE to make any outbound connection unless `--check-revocation` is
                  passed explicitly.
    MOS-EVID-125  offline verification cannot detect revocation. The verifier states this
                  on every run.

WHY THIS MODULE RECOMPUTES RATHER THAN RE-READS
-----------------------------------------------
Check 5 exists to answer "do the published aggregates follow from the published per-case
rows". If the verifier called the same function that produced them, it would be asking the
producer to confirm itself: the check would catch a tampered CSV and would not catch a
defect in the aggregation. So the aggregation below is written against the requirements
(`MOS-EVID-047`, `-048`, `-051`, `-052`) and not lifted from the runner. It DOES share the
bootstrap -- `medos.evidence.ci` -- because `MOS-EVID-058` asks for BIT-IDENTICAL interval
bounds, and two independent floating-point resamplers cannot deliver that; a divergent
interval would fail every honest report. The seam is deliberate and it is the only one.

REPORTED: once `medos.evidence.aggregate` lands, a reconciliation test asserting that it
and this module agree to 1e-12 on one fixture is owed. Two aggregators that never meet is
how the "one evaluation implementation" rule of section 15.2.5 gets violated slowly.

NO SOCKET IN THE CLOSURE
------------------------
Nothing imported here can open a connection, and `test_validation_report.py` asserts that
by inspecting `sys.modules` after a verification run in a subprocess whose `socket` module
has been made to raise. `--check-revocation` imports its HTTP client INSIDE the branch, so
the default path cannot reach one even by accident. That is `MOS-EVID-124` as a property
rather than as a promise.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import numpy as np

from medos.evidence import ci as ci_mod
from medos.evidence import criteria as criteria_mod
from medos.evidence import metrics as metrics_mod
from medos.evidence.bundle import (
    CASE_METRIC_COLUMNS,
    Bundle,
    BundleError,
    parse_checksums,
    read_bundle,
    read_csv,
)
from medos.evidence.digest import sha256_of
from medos.evidence.dsse import TrustedKey, backend_name, verify_envelope
from medos.evidence.report import (
    COMBINATION_RULE,
    SCHEMA_VERSION,
    derive_verdict,
    validate_report,
)

__all__ = [
    "EXIT_OK",
    "EXIT_USAGE",
    "CHECK_NAMES",
    "CHECK_EXIT_CODES",
    "REL_TOLERANCE",
    "CheckResult",
    "Verification",
    "verify_bundle",
    "verify_archive",
    "render",
]

EXIT_OK: Final[int] = 0
EXIT_USAGE: Final[int] = 1  # MOS-EVID-123 reserves it; nothing else may use it.

CHECK_NAMES: Final[tuple[str, ...]] = (
    "checksums",
    "dsse payload == report.json",
    "signature",
    f"schema {SCHEMA_VERSION}",
    "aggregates recomputed",
    "criteria re-evaluated",
    "input digests",
)
# Check n fails with exit code n + 1. Written out rather than computed, because the table
# in MOS-EVID-123 is normative and a reader must be able to diff it against this line.
CHECK_EXIT_CODES: Final[tuple[int, ...]] = (2, 3, 4, 5, 6, 7, 8)

REL_TOLERANCE: Final[float] = 1e-9  # MOS-EVID-066 and MOS-EVID-123 check 5


@dataclass(frozen=True)
class CheckResult:
    number: int
    name: str
    ok: bool
    detail: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_OK if self.ok else CHECK_EXIT_CODES[self.number - 1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.number,
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass
class Verification:
    checks: list[CheckResult] = field(default_factory=list)
    report: dict[str, Any] | None = None
    signatures: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    revocation: str = "not_checked"
    usage_error: str | None = None

    @property
    def exit_code(self) -> int:
        if self.usage_error is not None:
            return EXIT_USAGE
        for check in self.checks:
            if not check.ok:
                return check.exit_code
        return EXIT_OK if len(self.checks) == len(CHECK_NAMES) else EXIT_USAGE

    @property
    def verified(self) -> bool:
        return self.exit_code == EXIT_OK

    def as_dict(self) -> dict[str, Any]:
        doc = self.report or {}
        return {
            "verified": self.verified,
            "exit_code": self.exit_code,
            "checks": [c.as_dict() for c in self.checks],
            "signatures": self.signatures,
            "verdict": doc.get("verdict"),
            "report_id": doc.get("report_id"),
            "kind": doc.get("kind"),
            "issued_at": doc.get("issued_at"),
            "valid_until": doc.get("valid_until"),
            "revocation": self.revocation,
            "notes": self.notes,
            "usage_error": self.usage_error,
            "signature_backend": backend_name(),
        }


# =====================================================================================
# The entry points.
# =====================================================================================
def verify_archive(
    archive: bytes, trusted: Sequence[TrustedKey], *, now: datetime | None = None
) -> Verification:
    """Read the `tar.gz` and verify it. A malformed archive is a USAGE error, not a check.

    Exit 1 and not exit 2: `MOS-EVID-123`'s check 1 is "every file in CHECKSUMS.sha256
    present and matching", which presupposes an archive to read. Reporting "checksums
    failed" for a file that is not a tar at all would send the operator looking for a
    tampered member.
    """
    try:
        bundle = read_bundle(archive)
    except BundleError as exc:
        return Verification(usage_error=str(exc))
    return verify_bundle(bundle, trusted, now=now)


def verify_bundle(
    bundle: Bundle, trusted: Sequence[TrustedKey], *, now: datetime | None = None
) -> Verification:
    """The seven checks, in order, stopping at the first failure. MOS-EVID-123."""
    out = Verification()
    # MOS-EVID-125, printed on every run whatever the outcome.
    out.notes.append(
        "revocation status NOT checked (offline). A report that verifies here may since "
        "have been withdrawn; revocation is a separate signed statement (MOS-EVID-126)."
    )

    ok, detail = _check_1_checksums(bundle)
    out.checks.append(CheckResult(1, CHECK_NAMES[0], ok, detail))
    if not ok:
        return out

    ok, detail, payload = _check_2_payload(bundle)
    out.checks.append(CheckResult(2, CHECK_NAMES[1], ok, detail))
    if not ok:
        return out

    ok, detail, sigs = _check_3_signature(bundle, trusted)
    out.signatures = sigs
    out.checks.append(CheckResult(3, CHECK_NAMES[2], ok, detail))
    if not ok:
        return out

    try:
        report = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        out.checks.append(CheckResult(4, CHECK_NAMES[3], False, f"report.json: {exc}"))
        return out
    out.report = report if isinstance(report, dict) else None

    ok, detail = _check_4_schema(report, now=now, notes=out.notes)
    out.checks.append(CheckResult(4, CHECK_NAMES[3], ok, detail))
    if not ok:
        return out

    ok, detail, recomputed = _check_5_aggregates(bundle, report)
    out.checks.append(CheckResult(5, CHECK_NAMES[4], ok, detail))
    if not ok:
        return out

    ok, detail = _check_6_criteria(bundle, report, recomputed)
    out.checks.append(CheckResult(6, CHECK_NAMES[5], ok, detail))
    if not ok:
        return out

    ok, detail = _check_7_digests(bundle, report, out.notes)
    out.checks.append(CheckResult(7, CHECK_NAMES[6], ok, detail))
    return out


# =====================================================================================
# 1. Checksums.
# =====================================================================================
def _check_1_checksums(bundle: Bundle) -> tuple[bool, str]:
    """Every listed file present and matching, AND every present file listed.

    Both directions. `MOS-EVID-123` states the first; the second is added here because a
    one-directional check accepts a bundle with an EXTRA member -- a second `report.json`
    under another name, a shadow `case_metrics.csv` -- and "the checksums passed" would be
    true of an archive carrying an unlisted file. Stated as a strengthening rather than
    slipped in.
    """
    from medos.evidence.bundle import REQUIRED_MEMBERS  # noqa: PLC0415

    try:
        listed = parse_checksums(bundle.member("CHECKSUMS.sha256"))
    except BundleError as exc:
        return False, str(exc)

    missing = [p for p in listed if p not in bundle.members]
    if missing:
        return False, f"listed but absent: {sorted(missing)[:5]}"
    mismatched = [
        p
        for p, want in listed.items()
        if sha256_of(bundle.members[p]) != f"sha256:{want}"
    ]
    if mismatched:
        return False, f"content does not match its checksum: {sorted(mismatched)[:5]}"
    unlisted = [p for p in bundle.members if p != "CHECKSUMS.sha256" and p not in listed]
    if unlisted:
        return False, f"present but unlisted in CHECKSUMS.sha256: {sorted(unlisted)[:5]}"
    absent = [m for m in REQUIRED_MEMBERS if m not in bundle.members]
    if absent:
        return False, f"MOS-EVID-122 members missing from the bundle: {absent}"
    return True, f"{len(listed) + 1} files"


# =====================================================================================
# 2. The DSSE payload IS report.json.
# =====================================================================================
def _check_2_payload(bundle: Bundle) -> tuple[bool, str, bytes]:
    """MOS-EVID-123 check 2: byte-for-byte, not "parses to the same object".

    Two JSON documents that parse equal can differ in bytes, and only one of the two byte
    strings was signed. Comparing the parsed forms is how a signature over a canonical
    payload gets attached to a rendered, re-indented, semantically identical `report.json`
    that nobody signed.
    """
    import base64  # noqa: PLC0415

    try:
        envelope = bundle.json_member("report.dsse.json")
    except BundleError as exc:
        return False, str(exc), b""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), str):
        return False, "report.dsse.json carries no payload", b""
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
    except Exception:  # noqa: BLE001
        return False, "report.dsse.json payload is not valid base64", b""
    on_disk = bundle.member("report.json")
    if payload != on_disk:
        return (
            False,
            f"the signed payload ({len(payload)} bytes) is not report.json "
            f"({len(on_disk)} bytes); the signature does not cover this file",
            payload,
        )
    return True, "", payload


# =====================================================================================
# 3. At least one signature verifies against a key in the SUPPLIED trust bundle.
# =====================================================================================
def _check_3_signature(
    bundle: Bundle, trusted: Sequence[TrustedKey]
) -> tuple[bool, str, list[dict[str, Any]]]:
    """MOS-EVID-120: report WHICH signature verified against WHICH key.

    `keys/publisher_ed25519.pub` inside the bundle is never consulted here. A key shipped
    inside the artifact it vouches for is a convenience for provisioning, not a trust root;
    accepting it would make every self-signed bundle verify.
    """
    if not trusted:
        return False, "no trusted keys were supplied", []
    try:
        envelope = bundle.json_member("report.dsse.json")
    except BundleError as exc:
        return False, str(exc), []
    result = verify_envelope(envelope, list(trusted))
    sigs = [r.as_dict() for r in result.results]
    if result.error:
        return False, result.error, sigs
    if not result.any_verified:
        return (
            False,
            "no presented signature verifies against a key in the trust bundle "
            f"({len(result.results)} presented, {len(trusted)} trusted)",
            sigs,
        )
    named = [f'{r.keyid} -> "{r.trusted_key_name}"' for r in result.results if r.verified]
    return True, ", ".join(named), sigs


# =====================================================================================
# 4. Schema.
# =====================================================================================
def _check_4_schema(
    report: Any, *, now: datetime | None, notes: list[str]
) -> tuple[bool, str]:
    problems = validate_report(report)
    if problems:
        return False, "; ".join(problems[:6]) + (
            f" (+{len(problems) - 6} more)" if len(problems) > 6 else ""
        )
    # Expiry is a FRESHNESS signal, not a safety mechanism (MOS-EVID-115), so it is a note
    # and not a failed check: a site's policy for expired evidence is the site's.
    moment = now or datetime.now(UTC)
    valid_until = _parse_ts(str(report.get("valid_until")))
    if valid_until is not None and valid_until < moment:
        notes.append(
            f"report EXPIRED on {report.get('valid_until')}. MOS-EVID-115: expiry is a "
            "freshness signal, not a safety mechanism; the site's policy applies."
        )
    return True, ""


# =====================================================================================
# 5. Aggregates, recomputed from case_metrics.csv.
# =====================================================================================
@dataclass(frozen=True)
class _Row:
    case_key: str
    patient_key: str
    series_instance_uid: str
    metric: str
    value: float | None
    undefined_reason: str | None
    eligible: bool
    gt_voxels: int
    pred_voxels: int
    intersection_voxels: int
    gt_volume_ml: float
    pred_volume_ml: float
    strata: dict[str, Any]


def _load_rows(bundle: Bundle) -> list[_Row]:
    records = read_csv(bundle.member("case_metrics.csv"), CASE_METRIC_COLUMNS)
    rows: list[_Row] = []
    for rec in records:
        value = rec["value"].strip()
        strata = json.loads(rec["strata"]) if rec["strata"].strip() else {}
        if not isinstance(strata, dict):
            raise BundleError("case_metrics.csv: strata MUST be a JSON object")
        rows.append(
            _Row(
                case_key=rec["case_key"],
                patient_key=rec["patient_key"],
                series_instance_uid=rec["series_instance_uid"],
                metric=rec["metric"],
                value=None if not value else float(value),
                undefined_reason=rec["undefined_reason"].strip() or None,
                eligible=rec["eligible"].strip().lower() == "true",
                gt_voxels=int(rec["gt_voxels"]),
                pred_voxels=int(rec["pred_voxels"]),
                intersection_voxels=int(rec["intersection_voxels"]),
                gt_volume_ml=float(rec["gt_volume_ml"]),
                pred_volume_ml=float(rec["pred_volume_ml"]),
                strata=strata,
            )
        )
    return rows


_SELECTOR_OPS: Final[frozenset[str]] = frozenset(
    {"==", "!=", "<", "<=", ">", ">=", "in", "not_in"}
)


def _selector_matches(strata: dict[str, Any], selector: dict[str, Any]) -> bool:
    """Section 7.8.1's selector grammar, as a TOTAL function. MOS-EVID-076.

    No `eval`, no expression string, no callable. A field absent from `strata` does NOT
    match: `MOS-EVID-067` says a stratum that is not persisted cannot be gated on, and
    treating an absent field as a match silently widens the stratum to the whole cohort --
    the direction that inflates a number.
    """
    for name, clause in selector.items():
        if not isinstance(clause, dict):
            return False
        op, want = clause.get("op"), clause.get("value")
        if op not in _SELECTOR_OPS or name not in strata:
            return False
        got = strata[name]
        if got is None:
            return False
        try:
            if op == "==" and got != want:
                return False
            if op == "!=" and got == want:
                return False
            if op == "<" and not got < want:
                return False
            if op == "<=" and not got <= want:
                return False
            if op == ">" and not got > want:
                return False
            if op == ">=" and not got >= want:
                return False
            if op == "in" and got not in want:
                return False
            if op == "not_in" and got in want:
                return False
        except TypeError:
            return False
    return True


_ANCHOR: Final[str] = "dice_mean_per_case"


def _recompute(
    metric: str,
    rows: Sequence[_Row],
    *,
    conventions: dict[str, Any],
) -> tuple[float, float, float, int, int]:
    """`(value, ci_low, ci_high, n, n_patients)` for one metric over one stratum.

    Raises `ValueError` with a reason the verifier prints. Never returns a fabricated
    number: a metric this module cannot recompute fails check 5, because "skipped" and
    "matched" read identically in a summary line and only one of them is evidence.
    """
    spec = metrics_mod.REGISTRY.get(metric)
    if spec is None:
        raise ValueError(f"{metric!r} is absent from the MOS-EVID-054 registry")
    b = int(conventions.get("bootstrap_b", 2000))
    seed = int(conventions.get("bootstrap_seed", 20260101))
    threshold = float(conventions.get("fp_volume_threshold_ml", 0.0))

    if metric in metrics_mod.EMPTY_GT_METRICS:
        empty = [r for r in rows if r.metric == _ANCHOR and r.gt_voxels == 0]
        if not empty:
            raise ValueError(f"{metric}: no empty-ground-truth cases in this stratum")
        keys = [r.patient_key for r in empty]
        n, n_pat = len(empty), len(set(keys))
        if metric == "empty_gt_case_count":
            # A census. Its interval is itself; MOS-EVID-056 asks for the companion and
            # bootstrapping a count would answer a question nobody asked.
            return (float(n), float(n), float(n), n, n_pat)
        if metric == "empty_gt_false_positive_rate":
            values = [1.0 if r.pred_volume_ml > threshold else 0.0 for r in empty]
            stat: Callable[[Any], float] = np.mean
        elif metric == "empty_gt_mean_fp_volume_ml":
            values = [r.pred_volume_ml for r in empty]
            stat = np.mean
        elif metric == "empty_gt_p95_fp_volume_ml":
            values = [r.pred_volume_ml for r in empty]
            stat = _p95
        else:  # empty_gt_max_fp_volume_ml
            values = [r.pred_volume_ml for r in empty]
            stat = np.max
        lo, hi = ci_mod.cluster_bootstrap_ci(values, keys, stat, b, seed)
        return (float(stat(values)), lo, hi, n, n_pat)

    if spec.aggregation == "pooled":
        # MOS-EVID-048 and MOS-EVID-060: over the SAME eligible case set as the mean, from
        # the persisted voxel counts, which is why MOS-EVID-068 requires them.
        keep = [r for r in rows if r.metric == _ANCHOR and r.eligible]
        if not keep:
            raise ValueError("dice_pooled: no eligible segmentation cases in this stratum")
        num = [2.0 * r.intersection_voxels for r in keep]
        den = [float(r.gt_voxels + r.pred_voxels) for r in keep]
        keys = [r.patient_key for r in keep]
        lo, hi = ci_mod.cluster_bootstrap_ratio_ci(num, den, keys, b, seed)
        total_den = sum(den)
        if total_den <= 0:
            raise ValueError("dice_pooled: the pooled denominator is zero")
        return (sum(num) / total_den, lo, hi, len(keep), len(set(keys)))

    if spec.aggregation in {"mean", "median", "count"}:
        keep = [r for r in rows if r.metric == metric and r.eligible and r.value is not None]
        if not keep:
            raise ValueError(f"{metric}: no eligible per-case rows in this stratum")
        values = [float(r.value) for r in keep]  # type: ignore[arg-type]
        keys = [r.patient_key for r in keep]
        # `count` is MOS-EVID-059's count-based metric: the mean of the per-case indicator,
        # bootstrapped over patient clusters rather than over cases.
        stat = np.median if spec.aggregation == "median" else np.mean
        lo, hi = ci_mod.cluster_bootstrap_ci(values, keys, stat, b, seed)
        return (float(stat(values)), lo, hi, len(keep), len(set(keys)))

    raise ValueError(
        f"{metric!r} aggregates by {spec.aggregation!r}, which needs curve reconstruction "
        "from case_scores.csv; not implemented in 0.2.0, so this report MUST NOT be "
        "reported as verified"
    )


def _p95(values: Any) -> float:
    return float(np.quantile(np.asarray(values, dtype=float), 0.95))


def _check_5_aggregates(
    bundle: Bundle, report: dict[str, Any]
) -> tuple[bool, str, dict[tuple[str, str], tuple[float, float, float, int, int]]]:
    """MOS-EVID-066 / MOS-EVID-123 check 5, to within 1e-9 RELATIVE."""
    recomputed: dict[tuple[str, str], tuple[float, float, float, int, int]] = {}
    try:
        rows = _load_rows(bundle)
        criteria = bundle.json_member("criteria.yaml")
    except (BundleError, ValueError, json.JSONDecodeError) as exc:
        return False, str(exc), recomputed
    if not rows:
        return (
            False,
            "case_metrics.csv carries no rows; MOS-EVID-065 forbids storing aggregates "
            "without the per-case rows they came from",
            recomputed,
        )

    strata = {
        str(s.get("id")): s.get("selector", {})
        for s in (criteria.get("spec", {}) or {}).get("strata", [])
        if isinstance(s, dict)
    }
    conventions = (report.get("evaluation_run") or {}).get("metric_conventions", {})
    worst = 0.0
    for agg in report.get("aggregates", []):
        metric, stratum = str(agg.get("metric")), str(agg.get("stratum"))
        selector = strata.get(stratum)
        if selector is None:
            return (
                False,
                f"aggregate ({metric}, {stratum}): the stratum is not declared in "
                "criteria.yaml, so it cannot be recomputed",
                recomputed,
            )
        subset = [r for r in rows if _selector_matches(r.strata, selector)]
        try:
            got = _recompute(metric, subset, conventions=conventions)
        except ValueError as exc:
            return False, f"aggregate ({metric}, {stratum}): {exc}", recomputed
        recomputed[(metric, stratum)] = got
        for name, claimed, actual in (
            ("value", agg.get("value"), got[0]),
            ("ci_low", agg.get("ci_low"), got[1]),
            ("ci_high", agg.get("ci_high"), got[2]),
        ):
            delta = _rel_delta(claimed, actual)
            if delta is None or delta > REL_TOLERANCE:
                return (
                    False,
                    f"aggregate ({metric}, {stratum}).{name}: report says {claimed!r}, "
                    f"the per-case rows give {actual!r}",
                    recomputed,
                )
            worst = max(worst, delta)
        for name, claimed, actual in (
            ("n", agg.get("n"), got[3]),
            ("n_patients", agg.get("n_patients"), got[4]),
        ):
            if claimed != actual:
                return (
                    False,
                    f"aggregate ({metric}, {stratum}).{name}: report says {claimed!r}, "
                    f"the per-case rows give {actual!r}",
                    recomputed,
                )
    n_agg = len(report.get("aggregates", []))
    return True, f"{n_agg} aggregates, max rel. delta {worst:.1e}", recomputed


def _rel_delta(claimed: Any, actual: float) -> float | None:
    if not isinstance(claimed, (int, float)) or isinstance(claimed, bool):
        return None
    if not math.isfinite(actual) or not math.isfinite(float(claimed)):
        return None
    scale = max(abs(float(claimed)), abs(actual), 1.0)
    return abs(float(claimed) - actual) / scale


# =====================================================================================
# 6. Criteria re-evaluated, verdict reproduced.
# =====================================================================================
def _evaluate_absolute(
    criterion: dict[str, Any],
    recomputed: dict[tuple[str, str], tuple[float, float, float, int, int]],
    thresholds: dict[str, Any],
) -> tuple[str, str | None]:
    """`(status, reason)` for one absolute criterion. A total function. MOS-EVID-076."""
    metric = str(criterion.get("metric", ""))
    stratum = str(criterion.get("stratum", ""))
    bound = str(criterion.get("bound", "ci_lower_95"))  # MOS-EVID-077
    op = str(criterion.get("op", ">="))
    required = criterion.get("value")

    # MOS-EVID-055's four, from `medos.evidence.criteria` -- see the note in
    # `medos.evidence.report._validate_aggregates` for why this is not the registry's
    # wider `THRESHOLD_METRICS`.
    if metric in criteria_mod.OPERATING_POINT_METRICS:
        want = criterion.get("operating_threshold") or {}
        got = thresholds.get(want.get("name"))
        got_value = got.get("value") if isinstance(got, dict) else got
        if got is None or not _numeric_equal(got_value, want.get("value")):
            return "INDETERMINATE", "threshold_mismatch"  # MOS-EVID-078

    got_agg = recomputed.get((metric, stratum))
    if got_agg is None:
        return "INDETERMINATE", "aggregate_absent"
    value, ci_low, ci_high, n, n_patients = got_agg

    min_cases, min_patients = criterion.get("min_cases"), criterion.get("min_patients")
    if (isinstance(min_cases, int) and n < min_cases) or (
        isinstance(min_patients, int) and n_patients < min_patients
    ):
        # MOS-EVID-083: insufficient data needs more data, a FAIL needs a different model,
        # and folding the first into the second sends the reader to the wrong remedy.
        status = "FAIL" if criterion.get("on_insufficient_cases") == "fail" else "INDETERMINATE"
        return status, "insufficient_cases"

    observed = {"point": value, "ci_lower_95": ci_low, "ci_upper_95": ci_high}.get(bound)
    if observed is None or not isinstance(required, (int, float)):
        return "INDETERMINATE", "unevaluable_bound"
    passed = {
        ">=": observed >= required,
        ">": observed > required,
        "<=": observed <= required,
        "<": observed < required,
    }.get(op)
    if passed is None:
        return "INDETERMINATE", "unknown_comparison"
    return ("PASS" if passed else "FAIL"), None


def _check_6_criteria(
    bundle: Bundle,
    report: dict[str, Any],
    recomputed: dict[tuple[str, str], tuple[float, float, float, int, int]],
) -> tuple[bool, str]:
    """MOS-EVID-113 and MOS-EVID-114."""
    try:
        criteria = bundle.json_member("criteria.yaml")
    except BundleError as exc:
        return False, str(exc)
    spec = criteria.get("spec", {}) or {}
    absolute = spec.get("absolute", []) or []
    regression = spec.get("regression", []) or []
    if spec.get("combine", "all_of") != "all_of":
        return False, "spec.combine has exactly one value in 0.2.0: all_of"

    claimed = {
        str(r.get("id")): r
        for r in report.get("criteria_results", [])
        if isinstance(r, dict)
    }
    declared_ids = [str(c.get("id")) for c in [*absolute, *regression] if isinstance(c, dict)]

    # MOS-EVID-113: one entry per criterion, including SKIPPED and INDETERMINATE.
    missing = [cid for cid in declared_ids if cid not in claimed]
    if missing:
        return False, f"criteria_results omits {missing}; MOS-EVID-113 requires an entry"
    extra = [cid for cid in claimed if cid not in declared_ids]
    if extra:
        return False, f"criteria_results carries {extra}, absent from criteria.yaml"

    thresholds = (report.get("evaluation_run") or {}).get("operating_thresholds", {})
    for criterion in absolute:
        cid = str(criterion.get("id"))
        entry = claimed[cid]
        status, reason = _evaluate_absolute(criterion, recomputed, thresholds)
        said = str(entry.get("status"))
        if said != status:
            return (
                False,
                f"criterion {cid}: the report says {said}, re-evaluation against the "
                f"recomputed aggregates gives {status}"
                + (f" ({reason})" if reason else ""),
            )
        # The STATUS is what the verdict is derived from, but the numbers beside it are
        # what a human reads. A row saying `observed 0.95, required 0.90, PASS` over an
        # aggregate of 0.81 derives the same verdict and tells the reader something false,
        # so every field the entry chooses to state is checked against the criterion and
        # the recomputed aggregate rather than only the status.
        for field_name, expected in (
            ("metric", criterion.get("metric")),
            ("stratum", criterion.get("stratum")),
            ("bound", criterion.get("bound", "ci_lower_95")),
            ("op", criterion.get("op")),
            ("severity", criterion.get("severity", "blocking")),
        ):
            if field_name in entry and entry[field_name] != expected:
                return (
                    False,
                    f"criterion {cid}.{field_name} is reported {entry[field_name]!r}, "
                    f"but criteria.yaml declares {expected!r}",
                )
        declared = criterion.get("value")
        if "required" in entry and not _numeric_equal(entry["required"], declared):
            return (
                False,
                f"criterion {cid}.required is {entry['required']!r}, but criteria.yaml "
                f"declares {criterion.get('value')!r}",
            )
        got = recomputed.get((str(criterion.get("metric")), str(criterion.get("stratum"))))
        if "observed" in entry and got is not None:
            bound = str(criterion.get("bound", "ci_lower_95"))
            want = {"point": got[0], "ci_lower_95": got[1], "ci_upper_95": got[2]}.get(bound)
            delta = _rel_delta(entry["observed"], want) if want is not None else None
            if delta is None or delta > REL_TOLERANCE:
                return (
                    False,
                    f"criterion {cid}.observed is {entry['observed']!r}, but the "
                    f"recomputed {bound} is {want!r}",
                )

    # Regression criteria are PAIRED (MOS-EVID-085) and a bundle carries one run, so they
    # cannot be re-evaluated offline. What CAN be checked is that the report did not claim
    # a pass it never computed: SKIPPED or INDETERMINATE is the only honest entry here.
    for criterion in regression:
        cid = str(criterion.get("id"))
        said = str(claimed[cid].get("status"))
        if said not in {"SKIPPED", "INDETERMINATE", "FAIL"}:
            return (
                False,
                f"regression criterion {cid} is reported {said}; a paired non-inferiority "
                "test needs the incumbent run, which is not in this bundle, so a PASS "
                "here is not verifiable (MOS-EVID-085, MOS-EVID-086)",
            )

    derived = derive_verdict(list(claimed.values()), kind=str(report.get("kind")))
    if derived != report.get("verdict"):
        return (
            False,
            f"verdict {report.get('verdict')!r} is not what criteria_results derives "
            f"({derived!r}) under the published rule: {COMBINATION_RULE}",
        )
    return True, (
        f"{len(declared_ids)} criteria, verdict {report.get('verdict')} reproduced"
    )


def _numeric_equal(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-12
    return a == b


# =====================================================================================
# 7. Input digests.
# =====================================================================================
_DIGEST_BINDINGS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("criteria.yaml", ("capability", "criteria_digest")),
    ("envelope.yaml", ("applicability_envelope", "digest")),
    ("plausibility.yaml", ("plausibility", "rule_set_digest")),
    ("case_metrics.csv", ("artifacts", "case_metrics_csv_digest")),
    ("case_scores.csv", ("artifacts", "case_scores_csv_digest")),
    ("dataset_manifest.jsonl", ("artifacts", "dataset_manifest_digest")),
    ("split_manifest.jsonl", ("artifacts", "split_manifest_digest")),
    ("annotation_manifest.jsonl", ("artifacts", "annotation_manifest_digest")),
    ("leakage_report.json", ("artifacts", "leakage_report_digest")),
)


def _check_7_digests(
    bundle: Bundle, report: dict[str, Any], notes: list[str]
) -> tuple[bool, str]:
    """Every digest the report states about a bundled file, against that file's bytes.

    The cohort, split and annotation digests in `report.json` are the SEALED, tenant-side
    content addresses. They name manifests whose UIDs `MOS-EVID-116` strips on export, so
    they cannot equal the digest of anything in this archive -- see the note in
    `medos.evidence.bundle`. They are checked for presence and REPORTED as citations, and
    the re-keyed export manifests are what is digest-checked.
    """
    for member, path in _DIGEST_BINDINGS:
        stated = report
        for key in path:
            stated = stated.get(key, {}) if isinstance(stated, dict) else {}
        if not isinstance(stated, str) or not stated:
            return False, f"report.json states no digest at {'.'.join(path)} for {member}"
        actual = sha256_of(bundle.member(member))
        if stated != actual:
            return (
                False,
                f"{member}: report.json says {stated}, the bundled file digests to {actual}",
            )
    cohort = report.get("cohort", {}) or {}
    cited = {
        "cohort": cohort.get("dataset_version_digest"),
        "split": cohort.get("split_digest"),
        "annotations": cohort.get("annotation_digest"),
    }
    absent = [k for k, v in cited.items() if not isinstance(v, str) or not v]
    if absent:
        return False, f"report.json cites no content address for {absent}"
    notes.append(
        "the sealed cohort, split and annotation digests are CITATIONS to tenant-side "
        "objects and are not checkable offline; the re-keyed export manifests in this "
        "bundle are what was digest-checked (MOS-EVID-011, MOS-EVID-116)."
    )
    return True, "cohort, split, annotations, envelope, plausibility"


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, AttributeError):
        return None


# =====================================================================================
# Rendering -- the output shape section 7.12.3 prints.
# =====================================================================================
def render(result: Verification) -> str:
    """The seven lines, the verdict line and the notes. MOS-EVID-123 / MOS-EVID-125."""
    if result.usage_error is not None:
        return f"USAGE ERROR  {result.usage_error}\n"
    total = len(CHECK_NAMES)
    lines: list[str] = []
    for check in result.checks:
        status = "ok" if check.ok else "FAILED"
        detail = f" ({check.detail})" if check.detail else ""
        lines.append(f"[{check.number}/{total}] {check.name:<34} {status}{detail}")
    doc = result.report or {}
    if result.verified:
        subject = doc.get("subject", {}) or {}
        capability = doc.get("capability", {}) or {}
        lines.append(
            f"VERDICT {doc.get('verdict')}  subject {subject.get('id')}@"
            f"{subject.get('version')}  capability {capability.get('id')}  criteria v"
            f"{capability.get('criteria_version')}"
        )
    else:
        failed = next((c for c in result.checks if not c.ok), None)
        lines.append(
            f"NOT VERIFIED  check {failed.number} ({failed.name}) failed; exit "
            f"{result.exit_code}"
            if failed
            else f"NOT VERIFIED  exit {result.exit_code}"
        )
    if doc.get("issued_at"):
        lines.append(
            f"NOTE  Report issued {doc.get('issued_at')}, valid until "
            f"{doc.get('valid_until')}."
        )
    for note in result.notes:
        lines.append(f"NOTE  {note}")
    lines.append(f"NOTE  signature backend: {backend_name()}")
    return "\n".join(lines) + "\n"
