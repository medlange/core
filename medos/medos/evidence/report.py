# SPDX-License-Identifier: Apache-2.0
"""The `ValidationReport` document: build it, validate it, sign it. Section 7.12.

    MOS-EVID-002  the bounding sentence, reproduced VERBATIM on every generated report.
    MOS-EVID-011  a bundle that crosses a tenancy boundary re-keys `patient_key` under a
                  per-report salt recorded as `patient_key_scheme.report_salt_id`.
    MOS-EVID-049  a report containing a key named `dice` MUST fail schema validation.
    MOS-EVID-055  a threshold metric without its operating point is not a measurement and
                  the schema MUST reject it.
    MOS-EVID-056  every aggregate carries `n`, `n_patients`, a confidence interval and the
                  convention block. A bare scalar MUST fail schema validation.
    MOS-EVID-113  `criteria_results` carries one entry per criterion in the bound
                  `AcceptanceCriteria` version, including SKIPPED and INDETERMINATE. A
                  report that omits a criterion MUST fail verification.
    MOS-EVID-114  `verdict` MUST be re-derivable from `criteria_results`.
    MOS-EVID-115  `valid_until` MUST be set and MUST NOT exceed 24 months from `issued_at`.
    MOS-EVID-116  no PHI: no PatientID, PatientName, dates of birth, accession numbers,
                  institution-identifying free text or SOP Instance UIDs. Per-case rows are
                  keyed by a re-keyed `patient_key` and a per-report series pseudonym.
    MOS-EVID-117  `approver` names a real, identifiable human with a role, an organisation,
                  a written statement and an `identity_assurance`. An automated approver
                  MUST NOT be permitted.
    MOS-EVID-118  JCS canonicalisation, Ed25519, DSSE.
    MOS-EVID-119  the private key is NOT held by the evaluation runner, and signing is a
                  distinct, separately authorised step. Chapter 8 spells the two
                  permissions: `validation_report.create` generates, `validation_report.
                  approve` signs, and `MOS-SEC-042` forbids a seeded role holding both.
    MOS-EVID-121  every report is signed; an unsigned report is not accepted by the gate.
    MOS-EVID-142  a `monitoring_period` report MUST NOT carry a verdict of PASS.

A SPECIFICATION CONTRADICTION, RESOLVED IN THE OPEN AND REPORTED UPWARD
-----------------------------------------------------------------------
`MOS-EVID-002` requires the bounding sentence verbatim. That sentence contains the words
"clinical validation" (as a DENIAL: "It is not a clinical validation"). `MOS-EVID-005`
forbids that exact string in "any string literal, template, translation file, or generated
artifact produced by platform code" and requires CI to grep for it repository-wide, with
only two exclusions -- the specification itself, and publisher-supplied `regulatory_status`
values. Neither covers `MOS-EVID-002`'s own mandated text, so the two requirements cannot
both be satisfied as written.

Resolved toward `MOS-EVID-002`, which is the specific rule about this specific string, and
`forbidden_claim_hits()` below carries the exclusion explicitly rather than by accident:
the scanner strips the verbatim disclaimer before searching, so a report that carries the
denial passes and a report that carries the CLAIM does not. Obfuscating the literal to slip
past a grep was rejected: it would defeat the check while still emitting the string.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from medos.evidence import criteria as criteria_mod
from medos.evidence import metrics as metrics_mod
from medos.evidence.digest import sha256_of
from medos.evidence.dsse import PAYLOAD_TYPE, build_envelope, key_id
from medos.sdk.canonical import canonical_bytes, canonical_json, new_ulid

__all__ = [
    "SCHEMA_VERSION",
    "DISCLAIMER",
    "REPORT_KINDS",
    "VERDICTS",
    "SUBJECT_KINDS",
    "STATUSES",
    "FORBIDDEN_CLAIMS",
    "SIGNING_PERMISSION",
    "AUTHORING_PERMISSION",
    "ReportError",
    "SigningRefused",
    "new_report_id",
    "report_bytes",
    "report_digest",
    "validate_report",
    "derive_verdict",
    "forbidden_claim_hits",
    "phi_hits",
    "phi_hits_in_json",
    "sign_report",
]

SCHEMA_VERSION: Final[str] = "medicalos.io/validation-report/v1"

# MOS-EVID-002, verbatim. Changing one character of this string changes what the platform
# claims, so it lives here once and every report copies it.
DISCLAIMER: Final[str] = (
    "This report states the measured behaviour of a pinned software artifact on a named, "
    "content-addressed cohort under the declared conventions. It is not a clinical "
    "validation, not a regulatory clearance, and not a statement of fitness for any "
    "patient population outside the declared applicability envelope."
)

REPORT_KINDS: Final[tuple[str, ...]] = (
    "vendor_evidence",
    "site_acceptance",
    "monitoring_period",
)  # MOS-EVID-003: there is no fourth kind and no unlabelled evidence.
VERDICTS: Final[tuple[str, ...]] = ("PASS", "FAIL", "INDETERMINATE")  # MOS-EVID-083
SUBJECT_KINDS: Final[tuple[str, ...]] = ("service_version", "model_version")
STATUSES: Final[tuple[str, ...]] = ("ACTIVE", "SUPERSEDED", "REVOKED", "EXPIRED")

# MOS-EVID-005's closed list.
FORBIDDEN_CLAIMS: Final[tuple[str, ...]] = (
    "clinically validated",
    "clinical validation",
    "fda approved",
    "ce marked",
    "diagnostic accuracy certified",
)

# Chapter 8 section 8.3.2's permission table.
AUTHORING_PERMISSION: Final[str] = "validation_report.create"
SIGNING_PERMISSION: Final[str] = "validation_report.approve"

_ULID_RE: Final[re.Pattern[str]] = re.compile(r"^vr_[0-9A-HJKMNP-TV-Z]{26}$")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")
_RFC3339_RE: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
)
# MOS-EVID-116: an eight-digit run standing on its own is a DICOM DA. Bounded by
# non-alphanumerics so that the eight digits that occur inside a 64-character hex digest
# are not mistaken for a date -- a scanner that cries wolf on every digest is a scanner
# that gets switched off.
_DATE8_RE: Final[re.Pattern[str]] = re.compile(r"(?<![0-9A-Za-z])(?:19|20)\d{6}(?![0-9A-Za-z])")


class ReportError(ValueError):
    """A document that is not a well-formed ValidationReport."""


class SigningRefused(PermissionError):
    """MOS-EVID-119 / MOS-SEC-042: this principal MUST NOT sign evidence.

    A `PermissionError` and not a `ValueError`: the caller held a credential and the
    credential was not enough, which is an authorisation outcome and belongs in the audit
    trail as one.
    """


def new_report_id() -> str:
    """`vr_<ULID>`. Section 7.2."""
    return new_ulid("vr")


# =====================================================================================
# Canonical bytes and the content address.
# =====================================================================================
def report_bytes(doc: dict[str, Any]) -> bytes:
    """JCS-canonical UTF-8. MOS-EVID-008.

    This is what is digested, what is signed, and what `report.json` contains -- all three
    the same bytes, so `MOS-EVID-123` check 2 ("the DSSE payload decodes to bytes identical
    to report.json") is a property of the export rather than a coincidence of formatting.
    """
    return canonical_bytes(doc)


def report_digest(doc: dict[str, Any]) -> str:
    """`sha256:<hex>` over the canonical bytes. MOS-EVID-007."""
    return sha256_of(report_bytes(doc))


# =====================================================================================
# MOS-EVID-114: the verdict is DERIVED, never asserted.
# =====================================================================================
def derive_verdict(criteria_results: list[dict[str, Any]], *, kind: str) -> str:
    """The published combination rule of section 7.9.3, and nothing else.

    `all_of` over BLOCKING criteria: FAIL if any is FAIL; else INDETERMINATE if any is
    INDETERMINATE; else PASS. Advisory criteria are reported and never counted
    (`MOS-EVID-084`). `SKIPPED` is not a blocking outcome -- a regression criterion on a
    first deployment has no incumbent to compare against and its absence is not a failure.

    `MOS-EVID-142` overrides everything for a monitoring report: monitoring has no ground
    truth, so it cannot pass or fail an artifact against a clinical bar.
    """
    if kind == "monitoring_period":
        return "INDETERMINATE"
    blocking = [
        str(r.get("status"))
        for r in criteria_results
        if r.get("severity", "blocking") == "blocking"
    ]
    if "FAIL" in blocking:
        return "FAIL"
    if "INDETERMINATE" in blocking:
        return "INDETERMINATE"
    return "PASS"


COMBINATION_RULE: Final[str] = (
    "all_of over blocking criteria: FAIL if any blocking criterion is FAIL; else "
    "INDETERMINATE if any blocking criterion is INDETERMINATE; else PASS. Advisory "
    "criteria are reported and never counted (MOS-EVID-084). A monitoring_period report "
    "is always INDETERMINATE (MOS-EVID-142)."
)


# =====================================================================================
# Schema validation.  MOS-EVID-123 check 4. A TOTAL function: it returns problems, it
# does not raise, because the verifier has to print all of them and then exit 5.
# =====================================================================================
def validate_report(doc: Any) -> list[str]:
    """Every schema problem in `doc`, as human-readable lines. Empty list means valid."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["report.json is not a JSON object"]

    def need(path: str, value: Any, kind: type | tuple[type, ...], why: str) -> bool:
        if not isinstance(value, kind) or (isinstance(value, str) and not value):
            problems.append(f"{path}: {why}")
            return False
        return True

    if doc.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version MUST be {SCHEMA_VERSION!r}")
    if not _ULID_RE.match(str(doc.get("report_id", ""))):
        problems.append("report_id MUST be vr_<ULID> (section 7.2)")
    if not isinstance(doc.get("report_version"), int) or doc.get("report_version", 0) < 1:
        problems.append(
            "report_version MUST be a positive integer; section 12.12 keys "
            "UNIQUE (tenant_id, subject_kind, subject_id, capability_id, kind, "
            "report_version) on it"
        )
    kind = doc.get("kind")
    if kind not in REPORT_KINDS:
        problems.append(f"kind MUST be one of {REPORT_KINDS} (MOS-EVID-003)")
    if doc.get("disclaimer") != DISCLAIMER:
        problems.append("disclaimer MUST be MOS-EVID-002's sentence, verbatim")

    # MOS-EVID-115.
    issued, valid_until = doc.get("issued_at"), doc.get("valid_until")
    for path, value in (("issued_at", issued), ("valid_until", valid_until)):
        if not isinstance(value, str) or not _RFC3339_RE.match(value):
            problems.append(f"{path} MUST be RFC 3339 with a Z offset")
    if isinstance(issued, str) and isinstance(valid_until, str):
        try:
            t0 = _parse_ts(issued)
            t1 = _parse_ts(valid_until)
            if t1 <= t0:
                problems.append("valid_until MUST be after issued_at")
            elif t1 - t0 > timedelta(days=731):  # 24 months, leap-year tolerant
                problems.append("valid_until MUST NOT exceed 24 months (MOS-EVID-115)")
        except ValueError:
            pass

    subject = doc.get("subject")
    if need("subject", subject, dict, "MUST be an object"):
        if subject.get("kind") not in SUBJECT_KINDS:
            problems.append(f"subject.kind MUST be one of {SUBJECT_KINDS}")
        for key in ("id", "version"):
            need(f"subject.{key}", subject.get(key), str, "MUST be a non-empty string")
        if not _DIGEST_RE.match(str(subject.get("image_digest", ""))):
            problems.append("subject.image_digest MUST be sha256:<hex> (MOS-EVID-007)")

    capability = doc.get("capability")
    if need("capability", capability, dict, "MUST be an object"):
        need("capability.id", capability.get("id"), str, "MUST be a non-empty string")
        if not isinstance(capability.get("criteria_version"), int):
            problems.append("capability.criteria_version MUST be an integer")

    cohort = doc.get("cohort")
    if need("cohort", cohort, dict, "MUST be an object"):
        for key in (
            "dataset_version_digest",
            "split_digest",
            "annotation_digest",
        ):
            if not _DIGEST_RE.match(str(cohort.get(key, ""))):
                problems.append(f"cohort.{key} MUST be sha256:<hex>")
        if cohort.get("partition") not in ("train", "tune", "test", "excluded"):
            problems.append("cohort.partition MUST use section 7.4.1's vocabulary")
        for key in ("n_patients", "n_series"):
            if not isinstance(cohort.get(key), int) or cohort.get(key, 0) < 1:
                problems.append(f"cohort.{key} MUST be a positive integer")
        if cohort.get("reference_of_record") is not True and kind != "monitoring_period":
            problems.append(
                "cohort.reference_of_record MUST be true; MOS-EVID-090 refuses a gate on "
                "a reference standard that is not of record"
            )
        leakage = cohort.get("leakage_report")
        if need("cohort.leakage_report", leakage, dict, "MUST be an object"):
            for check in ("L1", "L2", "L3", "L4", "L5"):
                if check not in leakage:
                    problems.append(f"cohort.leakage_report MUST carry {check}")

    run = doc.get("evaluation_run")
    if need("evaluation_run", run, dict, "MUST be an object"):
        need("evaluation_run.id", run.get("id"), str, "MUST be a non-empty string")
        need("evaluation_run.code_commit", run.get("code_commit"), str, "MUST be present")
        if run.get("code_dirty") is not False:
            problems.append(
                "evaluation_run.code_dirty MUST be false; MOS-EVID-062 refuses evidence "
                "from a dirty tree"
            )
        if not _DIGEST_RE.match(str(run.get("run_digest", ""))):
            problems.append("evaluation_run.run_digest MUST be sha256:<hex>")
        conventions = run.get("metric_conventions")
        if need("evaluation_run.metric_conventions", conventions, dict, "MUST be an object"):
            for key in (
                "dice_aggregation",
                "empty_gt_policy",
                "fp_volume_threshold_ml",
                "ci_method",
                "bootstrap_b",
                "bootstrap_seed",
                "metric_registry_version",
            ):
                if key not in conventions:
                    problems.append(f"metric_conventions.{key} is required (MOS-EVID-056)")
            if kind in ("vendor_evidence", "site_acceptance") and conventions.get(
                "empty_gt_policy"
            ) != "exclude_and_report_separately":
                problems.append(
                    "MOS-EVID-051: exclude_and_report_separately is the only empty-GT "
                    f"policy permitted for a {kind} report in 0.2.0"
                )
        thresholds = run.get("operating_thresholds")
        if isinstance(thresholds, dict) and isinstance(cohort, dict):
            # Chapter 7 acceptance check 13: threshold selection MUST NOT have touched the
            # partition the report is measured on. Selecting the operating point on the
            # test partition and then reporting performance at it is the oldest way to
            # publish a number that does not survive contact with a second cohort.
            for name, spec in thresholds.items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("selected_on") == cohort.get("partition"):
                    problems.append(
                        f"operating_thresholds.{name}.selected_on is the reported "
                        "partition; threshold selection MUST NOT touch it (check 13)"
                    )

    problems.extend(_validate_aggregates(doc.get("aggregates"), run))
    problems.extend(_validate_criteria_results(doc.get("criteria_results")))

    approver = doc.get("approver")
    if need("approver", approver, dict, "MUST be an object (MOS-EVID-117)"):
        for key in ("name", "role", "organisation", "statement", "identity_assurance"):
            need(f"approver.{key}", approver.get(key), str, "MUST be a non-empty string")
        if not _RFC3339_RE.match(str(approver.get("approved_at", ""))):
            problems.append("approver.approved_at MUST be RFC 3339 with a Z offset")
        assurance = str(approver.get("identity_assurance", ""))
        if assurance and ":" not in assurance:
            problems.append(
                "approver.identity_assurance MUST tie the approval to an authenticated "
                "identity, e.g. oidc:<issuer>/sub/<subject> (MOS-EVID-117)"
            )

    envelope = doc.get("applicability_envelope")
    if need("applicability_envelope", envelope, dict, "MUST be an object"):
        if not _DIGEST_RE.match(str(envelope.get("digest", ""))):
            problems.append("applicability_envelope.digest MUST be sha256:<hex>")
        if not isinstance(envelope.get("version"), int):
            problems.append("applicability_envelope.version MUST be an integer")

    scheme = doc.get("patient_key_scheme")
    if need("patient_key_scheme", scheme, dict, "MUST be an object (MOS-EVID-011)"):
        need("patient_key_scheme.report_salt_id", scheme.get("report_salt_id"), str,
             "MUST name the per-report salt the export re-keyed under (MOS-EVID-011)")

    artifacts = doc.get("artifacts")
    if need("artifacts", artifacts, dict, "MUST be an object"):
        for key in ("case_metrics_csv_digest", "case_scores_csv_digest"):
            if not _DIGEST_RE.match(str(artifacts.get(key, ""))):
                problems.append(f"artifacts.{key} MUST be sha256:<hex>")

    verdict = doc.get("verdict")
    if verdict not in VERDICTS:
        problems.append(f"verdict MUST be one of {VERDICTS} (MOS-EVID-083)")
    if kind == "monitoring_period" and verdict != "INDETERMINATE":
        problems.append(
            "a monitoring_period report MUST NOT carry PASS or FAIL; monitoring has no "
            "reference standard (MOS-EVID-142)"
        )
    if kind == "site_acceptance" and not _DIGEST_RE.match(
        str((doc.get("cites") or {}).get("vendor_report_digest", ""))
    ):
        problems.append(
            "a site_acceptance report MUST cite the vendor_evidence report it was "
            "performed against, by report_digest (MOS-EVID-128)"
        )

    problems.extend(f"MOS-EVID-049: {hit}" for hit in _bare_dice_keys(doc))
    problems.extend(
        f"MOS-EVID-005: forbidden claim {hit!r}" for hit in forbidden_claim_hits(doc)
    )
    return problems


def _validate_aggregates(aggregates: Any, run: Any) -> list[str]:
    """MOS-EVID-055 and MOS-EVID-056: the five companions, and no bare scalar."""
    problems: list[str] = []
    if not isinstance(aggregates, list) or not aggregates:
        return ["aggregates MUST be a non-empty array"]
    thresholds = run.get("operating_thresholds", {}) if isinstance(run, dict) else {}
    for i, agg in enumerate(aggregates):
        if not isinstance(agg, dict):
            problems.append(f"aggregates[{i}] MUST be an object")
            continue
        metric = agg.get("metric")
        if metric not in metrics_mod.REGISTRY:
            problems.append(
                f"aggregates[{i}].metric {metric!r} is absent from the MOS-EVID-054 registry"
            )
        for key in ("value", "ci_low", "ci_high"):
            if not isinstance(agg.get(key), (int, float)) or isinstance(agg.get(key), bool):
                problems.append(f"aggregates[{i}].{key} MUST be a number (MOS-EVID-056)")
        for key in ("n", "n_patients"):
            if not isinstance(agg.get(key), int) or isinstance(agg.get(key), bool):
                problems.append(f"aggregates[{i}].{key} MUST be an integer (MOS-EVID-056)")
        if not isinstance(agg.get("stratum"), str) or not agg.get("stratum"):
            problems.append(f"aggregates[{i}].stratum MUST name a stratum")
        # MOS-EVID-055 names exactly four metrics, and `medos.evidence.criteria` owns that
        # tuple. NOT `metrics_mod.THRESHOLD_METRICS`, which also carries
        # `empty_gt_false_positive_rate`: the section 7.6.3 registry says that one needs a
        # threshold "via volume threshold", meaning `fp_volume_threshold_ml` declared once
        # per document by MOS-EVID-053 and recorded on the run -- and section 7.8.2's own
        # `fp_on_negatives` criterion carries no `operating_threshold`, so reading the
        # registry column the other way makes the chapter's worked example invalid.
        if metric in criteria_mod.OPERATING_POINT_METRICS:
            op = agg.get("operating_point")
            if not isinstance(op, dict) or "name" not in op or "value" not in op:
                problems.append(
                    f"aggregates[{i}]: {metric} without an operating point is not a "
                    "measurement (MOS-EVID-055)"
                )
            elif op["name"] not in thresholds:
                problems.append(
                    f"aggregates[{i}].operating_point.name {op['name']!r} is absent from "
                    "evaluation_run.operating_thresholds (MOS-EVID-078)"
                )
    return problems


def _validate_criteria_results(results: Any) -> list[str]:
    problems: list[str] = []
    if not isinstance(results, list) or not results:
        return ["criteria_results MUST be a non-empty array (MOS-EVID-113)"]
    seen: set[str] = set()
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            problems.append(f"criteria_results[{i}] MUST be an object")
            continue
        cid = r.get("id")
        if not isinstance(cid, str) or not cid:
            problems.append(f"criteria_results[{i}].id MUST be the criterion's stable id")
        elif cid in seen:
            problems.append(f"criteria_results[{i}]: duplicate criterion id {cid!r}")
        else:
            seen.add(cid)
        if r.get("status") not in ("PASS", "FAIL", "INDETERMINATE", "SKIPPED"):
            problems.append(
                f"criteria_results[{i}].status MUST be PASS, FAIL, INDETERMINATE or "
                "SKIPPED (MOS-EVID-113)"
            )
        if r.get("severity", "blocking") not in ("blocking", "advisory"):
            problems.append(f"criteria_results[{i}].severity MUST be blocking or advisory")
    return problems


# =====================================================================================
# MOS-EVID-049 and MOS-EVID-005: two greps over the document, not over the repository.
# =====================================================================================
def _bare_dice_keys(node: Any, path: str = "$") -> list[str]:
    """Any object key equal to the unqualified overlap word. MOS-EVID-049.

    Keys only, and not values: `"metric": "dice_mean_per_case"` is the CORRECT spelling and
    a scanner that fired on it would make the right thing unreportable.
    """
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.strip().lower() in {"dice", "accuracy"}:
                hits.append(f"{path}.{key} is an unqualified performance key")
            hits.extend(_bare_dice_keys(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits.extend(_bare_dice_keys(value, f"{path}[{i}]"))
    return hits


def forbidden_claim_hits(doc: Any) -> list[str]:
    """MOS-EVID-005's closed list, over the rendered document.

    `MOS-EVID-002`'s disclaimer is stripped first, and only that exact sentence: see the
    module docstring for why the two requirements contradict each other and why this is the
    direction the contradiction is resolved in.
    """
    text = canonical_json(doc) if not isinstance(doc, str) else doc
    text = text.replace(DISCLAIMER, " ")
    lowered = text.lower()
    return [claim for claim in FORBIDDEN_CLAIMS if claim in lowered]


def phi_hits(text: str, *, forbidden: list[str]) -> list[str]:
    """MOS-EVID-116, as the export-time gate. Chapter 7 acceptance check 36.

    Two scans, because the two failure modes are different:

      * `forbidden` is the caller's list of source values -- PatientID, PatientName,
        accession numbers, source SOP Instance UIDs. An EXACT substring hit is a leak, and
        this is the only scan that can find a UID, because a structural UID regex cannot
        tell a source StudyInstanceUID from the SOPClassUID that every manifest line
        legitimately carries.
      * an eight-digit run standing alone is a DICOM DA in disguise -- a birth date or a
        study date that survived de-identification.

    STRINGS ONLY. Call `phi_hits_in_json` for a structured member and let it decide which
    scalars are text: a DICOM DA is always a string, while `"bootstrap_seed": 20260101` and
    an eight-digit voxel count are numbers that the date pattern matches exactly. A scanner
    that fires on the seed the specification itself prints is a scanner that gets switched
    off, and then nothing is scanned at all.
    """
    hits: list[str] = []
    for value in forbidden:
        if value and len(value) >= 3 and value in text:
            # The VALUE is never echoed: chapter 8 forbids printing PHI, and a leak
            # detector that prints the leak is a second leak.
            hits.append(f"a forbidden source value of length {len(value)} appears verbatim")
    for match in _DATE8_RE.finditer(text):
        hits.append(f"an 8-digit date-shaped token at offset {match.start()}")
    return hits


def phi_hits_in_json(node: Any, *, forbidden: list[str], path: str = "$") -> list[str]:
    """`phi_hits` over the STRING scalars of a parsed document, with their paths.

    Numbers are scanned for nothing. `MOS-EVID-116`'s list -- PatientID, PatientName, dates
    of birth, accession numbers, institution free text, SOP Instance UIDs -- is a list of
    DICOM string values, and every one of them arrives in a manifest as a JSON string.
    """
    hits: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            for hit in phi_hits(str(key), forbidden=forbidden):
                if "verbatim" in hit:  # a key never holds a date; only a leaked value
                    hits.append(f"{path}.{key}: {hit}")
            hits.extend(phi_hits_in_json(value, forbidden=forbidden, path=f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            hits.extend(phi_hits_in_json(value, forbidden=forbidden, path=f"{path}[{i}]"))
    elif isinstance(node, str):
        hits.extend(f"{path}: {hit}" for hit in phi_hits(node, forbidden=forbidden))
    return hits


# =====================================================================================
# Signing.  MOS-EVID-118 / MOS-EVID-119 / MOS-SEC-042.
# =====================================================================================
def sign_report(
    doc: dict[str, Any],
    signers: list[tuple[bytes, bytes]],
    *,
    principal_scope: list[str] | tuple[str, ...],
) -> tuple[bytes, dict[str, Any]]:
    """`(canonical report bytes, DSSE envelope)`. Refuses a principal that MUST NOT sign.

    `MOS-EVID-119`: "Signing MUST be a distinct, separately-authorised step from running
    the evaluation, so that producing a run and attesting to it are different acts by
    different principals." Chapter 8 gives the two permissions their names and
    `MOS-SEC-042` forbids a seeded role holding both, so the evaluation runner -- the
    `evidence_scientist` role, which holds `validation_report.read/create/export` -- cannot
    reach this function's body. Chapter 7 acceptance check 38 is exactly that attempt.

    The check is here and not in an HTTP handler on purpose: a report can be signed from
    the CLI, from a worker and from the API, and an authorisation that lives in one of the
    three callers is absent from the other two.
    """
    from medos.security.scopes import scope_permits  # noqa: PLC0415

    if not scope_permits(principal_scope, SIGNING_PERMISSION):
        raise SigningRefused(
            f"signing a ValidationReport requires {SIGNING_PERMISSION!r}. Holding "
            f"{AUTHORING_PERMISSION!r} is not enough and MUST NOT be: producing a run and "
            "attesting to it are different acts by different principals (MOS-EVID-119, "
            "MOS-SEC-042)."
        )
    if not signers:
        raise SigningRefused(
            "an unsigned ValidationReport MUST NOT be accepted by the gate (MOS-EVID-121)"
        )
    payload = report_bytes(doc)
    return payload, build_envelope(payload, signers, payload_type=PAYLOAD_TYPE)


def signer_key_ids(signers: list[tuple[bytes, bytes]]) -> list[str]:
    return [key_id(pub) for _, pub in signers]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
