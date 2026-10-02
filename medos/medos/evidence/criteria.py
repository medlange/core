# SPDX-License-Identifier: Apache-2.0
"""`AcceptanceCriteria`: the grammar, its validator, and the absolute half of its evaluator.

Chapter 7 section 7.8. Chapter 6 `MOS-REG-049` for the ownership rule.

    MOS-EVID-050  A criterion MUST name `dice_mean_per_case` or `dice_pooled` explicitly;
                  there is no default.
    MOS-EVID-052  A criteria set constraining `dice_mean_per_case` without also
                  constraining at least one empty-GT metric MUST be rejected AT AUTHORING
                  TIME.
    MOS-EVID-053  `fp_volume_threshold_ml` MUST be declared per capability and recorded on
                  the run.
    MOS-EVID-075  The CLINICAL bar belongs to the Capability. The ENGINEERING bar -- p95
                  latency, GPU ceiling, ResultBundle schema validity, throughput --
                  belongs to the ModelVersion/ServiceVersion and MUST NOT appear here.
    MOS-EVID-076  A DECLARATIVE document with a fixed grammar. It MUST NOT be an
                  expression string evaluated by `eval`, a Python callable, a SQL fragment
                  or a template, and the evaluator MUST be a TOTAL function over the
                  grammar with no dynamic code path.
    MOS-EVID-077  `bound` DEFAULTS to `ci_lower_95` for any "performance at least X"
                  criterion. Gating on a point estimate is a defect: on 40 cases the point
                  estimate crosses a fixed bar on noise alone.
    MOS-EVID-078  A criterion whose metric requires a threshold MUST declare
                  `operating_threshold`, and it MUST match the run's -- otherwise
                  INDETERMINATE with reason `threshold_mismatch`.
    MOS-EVID-079  Authoring is validated against the grammar at WRITE time: an unknown
                  field, an unknown metric id, a stratum referencing a field absent from
                  the run's `strata` keys, or a `dice_mean_per_case` criterion without its
                  empty-GT companion is rejected.
    MOS-EVID-083  The verdict is PASS, FAIL or INDETERMINATE, and INDETERMINATE is NEVER
                  folded into FAIL: one needs more data, the other a different model.
    MOS-EVID-084  Advisory criteria are evaluated and reported and never gate.
    MOS-EVID-087  `margin` is declared per metric and justified in `margin_rationale`.
    MOS-EVID-113  One `criteria_results` entry per criterion, including SKIPPED.

SCOPE: this module owns the GRAMMAR and the ABSOLUTE half. `spec.regression` is a PAIRED
test over the per-case rows of two runs (MOS-EVID-085) and is evaluated by
`medos.evidence.gate`, which holds both. The combination rule is
`medos.evidence.report.derive_verdict` and is not restated here -- MOS-EVID-114 requires
the verdict to be re-derivable by "the published combination rule", singular, and a second
copy is how a report and a gate come to disagree about the same results array.

THE SELECTOR OP VOCABULARY, AND WHY IT IS THIS ONE
---------------------------------------------------
Section 7.8.1 closes the set: "`op` values permitted on a selector: `==`, `!=`, `<`, `<=`,
`>`, `>=`, `in`, `not_in`." `medos.evidence.aggregate.Subgroup` uses a different spelling
(`eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`) for the same idea and has no `not_in`. Rather
than translate -- which loses `not_in` and puts a mapping table between an authored
document and the rows it selects -- this module filters the rows itself with the
chapter's own eight operators and hands the already-filtered set to the reference
aggregator. The aggregation stays single-sourced (MOS-EVID-066); only the selection is
here, where the grammar is. REPORTED as a divergence between two modules' spellings of a
selector rather than papered over.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from medos.evidence import metrics as metrics_mod

__all__ = [
    "BOUNDS",
    "REGRESSION_BOUNDS",
    "COMPARISONS",
    "SELECTOR_OPS",
    "OPERATING_POINT_METRICS",
    "SEVERITIES",
    "STATUSES",
    "ON_INSUFFICIENT",
    "REQUIRED_STRATUM_FIELDS",
    "CriteriaError",
    "CriterionResult",
    "validate_criteria",
    "strata_of",
    "selector_matches",
    "filter_rows",
    "evaluate_absolute",
    "criteria_version_of",
]

BOUNDS: Final[frozenset[str]] = frozenset({"point", "ci_lower_95", "ci_upper_95"})
REGRESSION_BOUNDS: Final[frozenset[str]] = frozenset({"ci_lower_95", "catastrophic_count"})
COMPARISONS: Final[frozenset[str]] = frozenset({">=", ">", "<=", "<"})
SEVERITIES: Final[frozenset[str]] = frozenset({"blocking", "advisory"})
STATUSES: Final[frozenset[str]] = frozenset({"PASS", "FAIL", "INDETERMINATE", "SKIPPED"})
ON_INSUFFICIENT: Final[frozenset[str]] = frozenset({"indeterminate", "fail"})

# Section 7.8.1, verbatim and closed.
SELECTOR_OPS: Final[tuple[str, ...]] = ("==", "!=", "<", "<=", ">", ">=", "in", "not_in")

# MOS-EVID-055 names exactly these four: "A `sensitivity`, `specificity`, `ppv` or
# `froc_sensitivity` value MUST NOT be persisted, reported or published without the
# operating point that produced it."
#
# NOT `metrics_mod.THRESHOLD_METRICS`, which also contains `empty_gt_false_positive_rate`.
# The section 7.6.3 registry table says that metric needs a threshold "via volume
# threshold" -- meaning `fp_volume_threshold_ml`, declared ONCE for the whole document by
# MOS-EVID-053 and recorded on the run, not per criterion. Reading the registry's column
# as "requires `operating_threshold` on the criterion" makes section 7.8.2's own worked
# example invalid: its `fp_on_negatives` criterion declares no operating threshold.
# REPORTED as a collision between two spellings of "threshold"; the criteria grammar
# follows MOS-EVID-055 and the worked example.
OPERATING_POINT_METRICS: Final[tuple[str, ...]] = (
    "sensitivity",
    "specificity",
    "ppv",
    "froc_sensitivity",
)

# MOS-EVID-067's minimum persisted `strata` keys. A capability's declared clinical strata
# are added by the caller (`pleural_effusion` declares `reference_volume_ml`).
REQUIRED_STRATUM_FIELDS: Final[tuple[str, ...]] = (
    "slice_thickness_mm",
    "pixel_spacing_mm_max",
    "convolution_kernel_class",
    "manufacturer",
    "contrast_phase",
    "patient_age_years",
    "patient_sex",
)

_CRITERION_KEYS: Final[frozenset[str]] = frozenset(
    {
        "id", "metric", "stratum", "operating_threshold", "bound", "op", "value",
        "margin", "min_cases", "min_patients", "on_insufficient_cases", "severity",
    }
)
_SPEC_KEYS: Final[frozenset[str]] = frozenset(
    {
        "combine", "fp_volume_threshold_ml", "case_definition", "strata",
        "absolute", "regression", "margin_rationale",
    }
)
# MOS-EVID-075 / MOS-STORE-297: the engineering bar lives on the version. A criteria
# document carrying one has moved a latency budget onto the clinical bar, which makes a
# slow build a CLINICAL refusal and a fast one clinical evidence.
_ENGINEERING_KEYS: Final[tuple[str, ...]] = (
    "p95_latency_ms", "latency", "peak_gpu_memory_mib", "gpu", "throughput",
    "result_schema_valid_rate", "engineering", "engineering_acceptance",
)


class CriteriaError(ValueError):
    """A document the grammar refuses. MOS-EVID-079."""


@dataclass(frozen=True)
class CriterionResult:
    """One entry of `criteria_results`. MOS-EVID-113.

    `as_dict()` is the shape `deployment_gate_decisions.criterion_results` stores and
    `report.derive_verdict` consumes, so the gate's record and the report's array are the
    same object rather than two renderings of one.
    """

    id: str
    status: str
    severity: str = "blocking"
    metric: str | None = None
    stratum: str | None = None
    bound: str | None = None
    op: str | None = None
    observed: float | None = None
    required: float | None = None
    n: int | None = None
    n_patients: int | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "severity": self.severity,
        }
        for key in ("metric", "stratum", "bound", "op", "observed", "required",
                    "n", "n_patients", "reason"):
            value = getattr(self, key)
            if value is not None:
                out[key] = value
        return out


# =====================================================================================
# Authoring validation.  MOS-EVID-079, at WRITE time.
# =====================================================================================
def criteria_version_of(document: Mapping[str, Any]) -> int:
    version = document.get("metadata", {}).get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise CriteriaError("metadata.version MUST be an integer >= 1")
    return version


def strata_of(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """`{stratum_id: selector}`. `{}` is the whole cohort (section 7.8.1)."""
    out: dict[str, dict[str, Any]] = {}
    for entry in document.get("spec", {}).get("strata", []) or []:
        if isinstance(entry, Mapping) and isinstance(entry.get("id"), str):
            selector = entry.get("selector", {})
            out[entry["id"]] = dict(selector) if isinstance(selector, Mapping) else {}
    return out


def validate_criteria(
    document: Mapping[str, Any],
    *,
    known_strata_fields: Sequence[str] | None = None,
) -> None:
    """Refuse anything MOS-EVID-052, -053, -075, -077, -078, -079 or -087 forbids.

    `known_strata_fields` defaults to MOS-EVID-067's minimum set. Pass the capability's
    declared clinical strata as well -- for `pleural_effusion`, `reference_volume_ml` --
    or a legitimate subgroup criterion is refused. Chapter 7 acceptance check 16's second
    half is the other direction: a stratum over `scanner_serial`, which nothing persists,
    must be refused, because a criterion over an absent field selects zero cases and reads
    as a clean pass.
    """
    if not isinstance(document, Mapping):
        raise CriteriaError("the criteria document MUST be an object")

    metadata = document.get("metadata")
    if not isinstance(metadata, Mapping):
        raise CriteriaError("the document has no `metadata` object")
    capability_id = metadata.get("capability_id")
    if not isinstance(capability_id, str) or not capability_id:
        raise CriteriaError(
            "metadata.capability_id is required: AcceptanceCriteria belongs to the "
            "Capability and is versioned with it (MOS-EVID-075, MOS-REG-049)"
        )
    criteria_version_of(document)

    spec = document.get("spec")
    if not isinstance(spec, Mapping):
        raise CriteriaError("the document has no `spec` object")
    # The engineering check runs BEFORE the unknown-field check, so that a latency budget
    # on a clinical document is refused with the reason it is actually wrong -- "this bar
    # belongs to the version" -- rather than with a generic "unknown field". The two
    # rejections send an author to two different places.
    engineering = sorted(k for k in _ENGINEERING_KEYS if k in spec)
    if engineering:
        raise CriteriaError(
            f"{engineering} is an ENGINEERING bar and belongs to the "
            f"ModelVersion/ServiceVersion, not to the Capability's clinical criteria "
            f"(MOS-EVID-075, MOS-STORE-297)"
        )

    unknown = sorted(set(spec) - _SPEC_KEYS)
    if unknown:
        raise CriteriaError(f"unknown spec fields: {unknown} (MOS-EVID-079)")
    if spec.get("combine", "all_of") != "all_of":
        raise CriteriaError("`combine` has exactly one value in 0.2.0: all_of")

    known = set(known_strata_fields) if known_strata_fields is not None else set(
        REQUIRED_STRATUM_FIELDS
    )
    strata = strata_of(document)
    for sid, selector in strata.items():
        missing = sorted(set(selector) - known)
        if missing:
            raise CriteriaError(
                f"stratum {sid!r} selects on {missing}, absent from the persisted "
                f"`strata` keys {sorted(known)}; a stratum that is not persisted cannot "
                f"be gated on (MOS-EVID-067, MOS-EVID-079)"
            )
        for field_name, clause in selector.items():
            if not isinstance(clause, Mapping) or "op" not in clause or "value" not in clause:
                raise CriteriaError(
                    f"stratum {sid!r}.{field_name}: expected {{op, value}} (section 7.8.1)"
                )
            if clause["op"] not in SELECTOR_OPS:
                raise CriteriaError(
                    f"stratum {sid!r}.{field_name}: op {clause['op']!r} is not one of "
                    f"{list(SELECTOR_OPS)}"
                )

    absolute = spec.get("absolute", [])
    regression = spec.get("regression", [])
    if not isinstance(absolute, list) or not isinstance(regression, list):
        raise CriteriaError("`absolute` and `regression` MUST be arrays")
    if not absolute and not regression:
        raise CriteriaError("a criteria document with no criterion gates nothing")

    seen: set[str] = set()
    for criterion in absolute:
        _validate_criterion(criterion, strata, seen, kind="absolute")
    for criterion in regression:
        _validate_criterion(criterion, strata, seen, kind="regression")

    # MOS-EVID-052, and chapter 7 acceptance check 16's first half. Constraining the
    # headline overlap figure without constraining false positives on negatives lets a
    # model that paints every empty study keep the headline, because exclusion removes
    # exactly those cases from it.
    metrics_used = {c.get("metric") for c in absolute if isinstance(c, Mapping)}
    if "dice_mean_per_case" in metrics_used and not (
        metrics_used & set(metrics_mod.EMPTY_GT_METRICS)
    ):
        raise CriteriaError(
            "a `dice_mean_per_case` criterion MUST be accompanied by at least one "
            f"empty-ground-truth criterion (one of "
            f"{list(metrics_mod.EMPTY_GT_METRICS)}) -- MOS-EVID-052"
        )
    if metrics_used & set(metrics_mod.EMPTY_GT_METRICS) and (
        spec.get("fp_volume_threshold_ml") is None
    ):
        raise CriteriaError(
            "`fp_volume_threshold_ml` MUST be declared per capability and recorded on the "
            "run (MOS-EVID-053)"
        )

    # MOS-EVID-087: delta is a clinical judgement, not a statistical parameter, and "MUST
    # NOT be tuned to make a specific candidate pass". Without the justification that
    # second half is unauditable, so the document is refused rather than accepted with a
    # blank field.
    rationale = spec.get("margin_rationale") or metadata.get("margin_rationale")
    if regression and not (isinstance(rationale, str) and rationale.strip()):
        raise CriteriaError(
            "`margin_rationale` is required when regression criteria are declared "
            "(MOS-EVID-087)"
        )


def _validate_criterion(
    criterion: Any, strata: Mapping[str, Any], seen: set[str], *, kind: str
) -> None:
    if not isinstance(criterion, Mapping):
        raise CriteriaError("a criterion MUST be an object")
    unknown = sorted(set(criterion) - _CRITERION_KEYS)
    if unknown:
        raise CriteriaError(f"unknown criterion fields: {unknown} (MOS-EVID-079)")

    cid = criterion.get("id")
    if not isinstance(cid, str) or not cid:
        raise CriteriaError(
            "every criterion MUST carry a stable `id`; it appears in the verdict"
        )
    if cid in seen:
        raise CriteriaError(
            f"duplicate criterion id {cid!r}: MOS-EVID-113 requires one results entry per "
            f"criterion, which a duplicate id makes ambiguous"
        )
    seen.add(cid)

    metric = criterion.get("metric")
    if metric not in metrics_mod.REGISTRY:
        raise CriteriaError(
            f"{cid}: metric {metric!r} is absent from the MOS-EVID-054 registry. "
            f"MOS-EVID-050: a criterion names `dice_mean_per_case` or `dice_pooled` "
            f"explicitly and there is no default."
        )

    stratum = criterion.get("stratum")
    if stratum not in strata:
        raise CriteriaError(f"{cid}: stratum {stratum!r} is not declared in `spec.strata`")

    if criterion.get("severity", "blocking") not in SEVERITIES:
        raise CriteriaError(f"{cid}: `severity` MUST be blocking or advisory")
    if criterion.get("on_insufficient_cases", "indeterminate") not in ON_INSUFFICIENT:
        raise CriteriaError(f"{cid}: `on_insufficient_cases` MUST be indeterminate or fail")
    for name in ("min_cases", "min_patients"):
        value = criterion.get(name)
        bad = isinstance(value, bool) or not isinstance(value, int) or value < 0
        if value is not None and bad:
            raise CriteriaError(f"{cid}: `{name}` MUST be a non-negative integer")

    # MOS-EVID-077. The default is applied here, once: an omitted `bound` must never
    # silently mean `point`, which is the defect the requirement exists to prevent.
    bound = criterion.get("bound", "ci_lower_95")
    permitted = BOUNDS if kind == "absolute" else REGRESSION_BOUNDS
    if bound not in permitted:
        raise CriteriaError(f"{cid}: `bound` {bound!r} is not one of {sorted(permitted)}")

    if kind == "regression":
        margin = criterion.get("margin")
        if isinstance(margin, bool) or not isinstance(margin, (int, float)):
            raise CriteriaError(f"{cid}: `margin` MUST be a number (MOS-EVID-087)")
        if float(margin) <= 0.0:
            raise CriteriaError(
                f"{cid}: `margin` MUST be > 0; delta = 0 is an exact-equality test that no "
                f"honest rebuild passes (section 7.9.1)"
            )
        needs_comparison = bound == "catastrophic_count"
    else:
        if "margin" in criterion:
            raise CriteriaError(
                f"{cid}: `margin` is a regression-criterion field (section 7.8.1)"
            )
        needs_comparison = True

    if needs_comparison:
        if criterion.get("op") not in COMPARISONS:
            raise CriteriaError(f"{cid}: `op` MUST be one of {sorted(COMPARISONS)}")
        value = criterion.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CriteriaError(f"{cid}: `value` MUST be a number")
    else:
        # A non-inferiority criterion is a test against `-margin` (MOS-EVID-085). An `op`
        # here would be a second, contradictory bar.
        if criterion.get("op") is not None or criterion.get("value") is not None:
            raise CriteriaError(
                f"{cid}: a non-inferiority criterion is a test against -margin and takes "
                f"no `op`/`value` (MOS-EVID-085)"
            )

    # MOS-EVID-078, declared here and matched against the run in `evaluate_absolute`.
    if metric in OPERATING_POINT_METRICS:
        threshold = criterion.get("operating_threshold")
        if not isinstance(threshold, Mapping) or not {"name", "value"} <= set(threshold):
            raise CriteriaError(
                f"{cid}: metric {metric!r} requires `operating_threshold: {{name, value}}` "
                f"(MOS-EVID-078). A number without an operating point is not a measurement "
                f"(MOS-EVID-055)."
            )
    elif criterion.get("operating_threshold") is not None:
        raise CriteriaError(f"{cid}: metric {metric!r} takes no operating threshold")


# =====================================================================================
# Selection.  A TOTAL function over section 7.8.1's eight operators. No eval, no
# callable, no template -- MOS-EVID-076.
# =====================================================================================
def _apply(op: str, observed: Any, want: Any) -> bool:
    try:
        if op == "==":
            return bool(observed == want)
        if op == "!=":
            return bool(observed != want)
        if op == "<":
            return bool(observed < want)
        if op == "<=":
            return bool(observed <= want)
        if op == ">":
            return bool(observed > want)
        if op == ">=":
            return bool(observed >= want)
        if op == "in":
            return observed in want
        if op == "not_in":
            return observed not in want
    except TypeError:
        # `3.0 < "soft"`: a type mismatch between the authored bound and the persisted
        # value. Not a match, and not an exception that aborts the gate mid-verdict.
        return False
    return False


def selector_matches(strata: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    """Does one case's persisted `strata` satisfy every clause? `{}` matches everything.

    A field ABSENT from `strata` never matches. MOS-EVID-067: "a stratum that is not
    persisted cannot be gated on", and treating an absent field as satisfied silently
    widens the stratum to the whole cohort -- the direction that inflates a number.
    """
    for field_name, clause in selector.items():
        if not isinstance(clause, Mapping):
            return False
        op = clause.get("op")
        if op not in SELECTOR_OPS:
            return False
        if field_name not in strata:
            return False
        observed = strata[field_name]
        if observed is None:
            return False
        if not _apply(str(op), observed, clause.get("value")):
            return False
    return True


def filter_rows(rows: Sequence[Any], selector: Mapping[str, Any]) -> list[Any]:
    """Rows in the stratum. Accepts `CaseMetricRow`s and score mappings alike."""
    out = []
    for row in rows:
        strata = row.strata if hasattr(row, "strata") else row.get("strata", {})
        if selector_matches(strata or {}, selector):
            out.append(row)
    return out


# =====================================================================================
# The absolute half.  Never raises -- every path returns a status (MOS-EVID-076).
# =====================================================================================
def evaluate_absolute(
    criterion: Mapping[str, Any],
    aggregates: Mapping[tuple[str, str], Mapping[str, Any]],
    *,
    operating_thresholds: Mapping[str, Any],
) -> CriterionResult:
    """One absolute criterion against the recomputed `{(metric, stratum): entry}` block."""
    cid = str(criterion.get("id", "?"))
    severity = str(criterion.get("severity", "blocking"))
    metric = str(criterion.get("metric", ""))
    stratum = str(criterion.get("stratum", ""))
    bound = str(criterion.get("bound", "ci_lower_95"))  # MOS-EVID-077
    op = str(criterion.get("op", ">="))
    required = criterion.get("value")

    def out(status: str, **kw: Any) -> CriterionResult:
        return CriterionResult(
            id=cid, status=status, severity=severity, metric=metric, stratum=stratum,
            bound=bound, op=op,
            required=float(required) if isinstance(required, (int, float)) else None,
            **kw,
        )

    # MOS-EVID-078: the threshold the criterion names MUST be the one the run used. A
    # sensitivity measured at 0.35 is not a sensitivity measured at 0.50, and comparing
    # them against one bar is comparing two different models.
    if metric in OPERATING_POINT_METRICS:
        want = criterion.get("operating_threshold") or {}
        name = want.get("name")
        got = operating_thresholds.get(name) if isinstance(name, str) else None
        got_value = got.get("value") if isinstance(got, Mapping) else got
        if got is None or not _close(got_value, want.get("value")):
            return out("INDETERMINATE", reason="threshold_mismatch")

    entry = aggregates.get((metric, stratum))
    if entry is None:
        # Not a pass. Treating an absent measurement as satisfied is the direction that
        # publishes a number nobody computed.
        return out("INDETERMINATE", reason="aggregate_absent")

    n = int(entry.get("n") or 0)
    n_patients = int(entry.get("n_patients") or 0)
    min_cases = criterion.get("min_cases")
    min_patients = criterion.get("min_patients")
    if (isinstance(min_cases, int) and n < min_cases) or (
        isinstance(min_patients, int) and n_patients < min_patients
    ):
        # MOS-EVID-083 and chapter 7 acceptance check 22: insufficient data needs MORE
        # DATA, a failing bar needs a DIFFERENT MODEL. Folding one into the other sends
        # the site to the wrong remedy.
        status = "FAIL" if criterion.get("on_insufficient_cases") == "fail" else "INDETERMINATE"
        return out(status, reason="insufficient_cases", n=n, n_patients=n_patients)

    observed = {
        "point": entry.get("value"),
        "ci_lower_95": entry.get("ci_low"),
        "ci_upper_95": entry.get("ci_high"),
    }.get(bound)
    if not isinstance(observed, (int, float)) or not isinstance(required, (int, float)):
        return out("INDETERMINATE", reason="unevaluable_bound", n=n, n_patients=n_patients)

    passed = {
        ">=": observed >= required,
        ">": observed > required,
        "<=": observed <= required,
        "<": observed < required,
    }[op]
    return out(
        "PASS" if passed else "FAIL",
        observed=float(observed),
        n=n,
        n_patients=n_patients,
    )


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-12
    return a == b
