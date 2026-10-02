# SPDX-License-Identifier: Apache-2.0
"""The deployment gate as an executable rule. Chapter 7 section 7.9.

    MOS-EVID-085  A regression criterion MUST be evaluated as a ONE-SIDED PAIRED
                  NON-INFERIORITY test on the per-case differences d_i = m_new,i - m_old,i
                  over the cases present in BOTH runs, H0: mu_d <= -delta against
                  H1: mu_d > -delta. It passes when the lower bound of the one-sided 95 %
                  patient-cluster bootstrap interval of mean(d) exceeds -delta.
    MOS-EVID-086  The two runs MUST share `dataset_version_digest`, `split_digest`,
                  `partition` and `annotation_digest`; otherwise INDETERMINATE with reason
                  `unpaired_runs`. "An unpaired comparison is not a regression test."
    MOS-EVID-087  `margin` (delta) is declared per metric in the Capability's
                  AcceptanceCriteria and justified in `margin_rationale`.
    MOS-EVID-088  `bound: catastrophic_count` counts the cases where the INCUMBENT exceeds
                  the candidate by more than `margin` and compares that count to `value`.
    MOS-EVID-089  More evidence MUST make it EASIER to pass, not harder.
    MOS-EVID-090  Transitioning a Deployment into `clinical_use_mode: clinical`, or
                  changing the version behind a clinical deployment, MUST call
                  `evaluate_gate()` and MUST refuse on FAIL or INDETERMINATE.
    MOS-EVID-092  On FAIL or INDETERMINATE the incumbent MUST REMAIN LIVE AND SERVING.
    MOS-EVID-113  One results entry per criterion, including SKIPPED.
    MOS-EVID-114  The verdict is re-derivable by the published combination rule --
                  `medos.evidence.report.derive_verdict`, which is not restated here.

WHY `if new.dice < old.dice` IS NOT A GATE  (section 7.9.1; 15.2.5 names it twice)
-----------------------------------------------------------------------------------
The naive comparison fails in BOTH directions and the release brief is explicit that it
"fails on noise roughly half the time on a 40-case split, gets switched off within two
weeks, and meanwhile passes a model that lost every sub-6 mm nodule".

  * IT BLOCKS ON NOISE, AND MORE DATA DOES NOT HELP. Rebuild the identical model against a
    different TensorRT version: the true mean paired difference is zero, the observed one
    is a random variable centred on zero, so `P(observed < 0) = 0.5` -- at n = 40 and at
    n = 4000 alike. A gate that blocks half of all harmless rebuilds gets turned off, and
    then there is no gate at all.
  * IT PASSES A MODEL THAT LOST THE HARDEST SUBGROUP. Where 85 % of positives exceed
    100 mL, total failure below 100 mL moves `dice_mean_per_case` by at most ~0.11 and
    usually far less -- under any bar anyone would author. An aggregate cannot see a
    subgroup collapse.

So the gate has three parts and none is optional: ABSOLUTE criteria (evaluated by
`medos.evidence.criteria`), a PAIRED NON-INFERIORITY test with a DECLARED MARGIN over
per-case pairs, and SUBGROUP FLOORS with a CATASTROPHIC-CASE COUNT. The subgroup half is
not a fourth mechanism: every criterion carries a `stratum`, so `ni_sens_small` over
`small_effusion` IS the floor, and the stratum-collapse case of chapter 7 acceptance check
20 is detected by that criterion while the cohort-wide point estimate barely moves.

There is no comparison of two AGGREGATE metric values anywhere on a blocking path in this
module. Chapter 7 acceptance check 18 requires that, and
`tests/integration/test_deployment_gate.py::test_the_naive_gate_is_absent` greps for it.

WHAT THIS MODULE IS NOT
-----------------------
It computes no metrics (`medos.evidence.metrics`, `medos.evidence.aggregate`), validates
no criteria documents (`medos.evidence.criteria`), signs and verifies nothing
(`medos.evidence.dsse`), and moves no Deployment (`medos.evidence.deployment`). It takes
two reports and a bound criteria document and returns a verdict with one entry per
criterion. Keeping it free of I/O is what makes acceptance checks 19-22 -- 200 synthetic
paired runs, a stratum collapse, a catastrophic fixture, a 25-patient cohort -- runnable
without a database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from medos.evidence import aggregate as aggregate_mod
from medos.evidence import ci as ci_mod
from medos.evidence import criteria as criteria_mod
from medos.evidence import metrics as metrics_mod
from medos.evidence import report as report_mod
from medos.evidence.criteria import CriterionResult

__all__ = [
    "SubjectRef",
    "RunView",
    "ReportView",
    "GateBinding",
    "GateResult",
    "INTEGRITY_REASONS",
    "GATE_REOPENING_INPUTS",
    "paired_series",
    "non_inferiority",
    "catastrophic_count",
    "build_aggregates",
    "evaluate_regression",
    "evaluate_gate",
]

# Section 7.9.3 step 0, plus the two this implementation adds and REPORTS:
#
#   `no_report`      Chapter 7 acceptance check 23 attempts a promotion for a version with
#                    NO ValidationReport and requires a persisted decision with verdict
#                    FAIL. The chapter's snippet dereferences
#                    `candidate_report.criteria_version` before any such check, so the
#                    absent-report case is an AttributeError rather than a verdict.
#   `report_revoked` MOS-STORE-301a revokes every report citing a run bound to a
#                    DatasetVersion marked DEFECTIVE, and MOS-EVID-126 owns the status
#                    lifecycle. Step 0 checks the signature and the expiry and not the
#                    STANDING, so a revoked report would pass a clinical gate and the
#                    revocation cascade would be decorative.
INTEGRITY_REASONS: Final[tuple[str, ...]] = (
    "no_report",
    "signature_invalid",
    "criteria_version_mismatch",
    "subject_mismatch",
    "report_expired",
    "report_revoked",
    "wrong_cohort",
    "wrong_partition",
    "reference_not_of_record",
    "run_not_succeeded",
)

# MOS-EVID-094: a change to any of these RE-OPENS the gate. Published as data so that the
# caller deciding "must this be re-gated" and the caller recording why cannot drift. A
# threshold change is a clinical change and is gated exactly like a weights change
# (MOS-REG-035: it MUST NOT be shipped as configuration).
GATE_REOPENING_INPUTS: Final[tuple[str, ...]] = (
    "service_version_id",
    "model_version_id",
    "preprocessing_spec_version",
    "operating_thresholds",
    "criteria_version",
    "tenant_acceptance_binding",
    "inference_backend",
    "accelerator",
)


@dataclass(frozen=True)
class SubjectRef:
    """What was evaluated. Section 7.12.1's `subject` block reduced to its identity.

    `kind` is `service_version` or `model_version` -- the two values
    `validation_reports.subject_kind` and `capability_claims.subject_kind` both carry. A
    ModelVersion is a legitimate gate subject: section 15.1.2 states the 0.2.0
    `deployment-gate` check over "a deliberately regressed `ModelVersion`", and
    MOS-EVID-064 makes a backend conversion a new ModelVersion needing its own run.
    """

    kind: str
    id: str
    version: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "version": self.version}


@dataclass(frozen=True)
class RunView:
    """The `EvaluationRun` facts the gate reads, plus its per-case rows.

    A VIEW, not the row: `evaluation_runs` is declared by migration 0007 and this module
    never writes it. The four digest fields are exactly MOS-EVID-086's pairing binding --
    the id says which row, the digest says which CONTENT, and after a restore-from-backup
    those are not the same question. `case_metrics` is the `evaluation_case_metrics` set
    MOS-EVID-065 requires to exist: without it no paired test is possible at all, which is
    why "aggregates alone MUST NOT be stored".
    """

    run_id: str
    dataset_version_id: str
    dataset_version_digest: str
    split_id: str
    split_digest: str
    partition: str
    annotation_set_id: str
    annotation_digest: str
    case_metrics: tuple[metrics_mod.CaseMetricRow, ...] = ()
    case_scores: tuple[Mapping[str, Any], ...] = ()
    operating_thresholds: Mapping[str, Any] = field(default_factory=dict)
    state: str = "SUCCEEDED"

    def pairing_binding(self) -> tuple[str, str, str, str]:
        return (
            self.dataset_version_digest,
            self.split_digest,
            self.partition,
            self.annotation_digest,
        )


@dataclass(frozen=True)
class ReportView:
    """The `ValidationReport` facts step 0 of `evaluate_gate()` checks.

    `signature_verified` is a boolean THE CALLER SUPPLIES, computed by
    `medos.evidence.dsse` against a trusted key. It is not recomputed here and it has NO
    DEFAULT: MOS-EVID-121 says an unsigned report MUST NOT be accepted by the gate, and a
    field that defaults to True is a field that is True on the day someone forgets it.
    """

    report_id: str
    subject: SubjectRef
    capability_id: str
    criteria_version: int
    run: RunView
    signature_verified: bool
    valid_until: datetime
    status: str = "ACTIVE"
    reference_of_record: bool = True
    envelope_digest: str | None = None
    kind: str = "vendor_evidence"


@dataclass(frozen=True)
class GateBinding:
    """The tenant's acceptance binding plus the subject being proposed.

    Section 7.9.3's snippet reads `binding.candidate_subject`, which the
    `tenant_acceptance_bindings` row of MOS-EVID-080 does not carry -- that row is one per
    `(tenant, capability)` and names a COHORT, not a candidate. The two are composed here:
    every other member mirrors the row, and `candidate_subject` is supplied per invocation
    by the promotion call.
    """

    tenant_id: str
    capability_id: str
    criteria_version: int
    dataset_version_id: str
    split_id: str
    partition: str
    annotation_set_id: str
    candidate_subject: SubjectRef
    threshold_overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateResult:
    """A verdict and one entry per criterion. MOS-EVID-083, MOS-EVID-113."""

    verdict: str
    results: tuple[CriterionResult, ...]

    @property
    def blocked(self) -> bool:
        """MOS-EVID-090: clinical refuses on FAIL *and* on INDETERMINATE."""
        return self.verdict != "PASS"

    def as_json(self) -> list[dict[str, Any]]:
        return [r.as_dict() for r in self.results]

    def reasons(self) -> tuple[str, ...]:
        return tuple(r.reason for r in self.results if r.reason)

    def by_id(self, criterion_id: str) -> CriterionResult:
        for r in self.results:
            if r.id == criterion_id:
                return r
        raise KeyError(criterion_id)


# =====================================================================================
# The paired test.  MOS-EVID-085, MOS-EVID-088.
# =====================================================================================
def _indicator_rows(
    run: RunView, metric: str, selector: Mapping[str, Any]
) -> list[tuple[str, str, float]]:
    """`(series_uid, patient_key, 0/1)` for a count-based metric. MOS-EVID-059.

    `sensitivity`, `specificity` and `ppv` have no per-case metric column: their per-case
    datum is an indicator derived from `evaluation_case_scores` at the declared operating
    point, which is how `medos.evidence.aggregate._count_family` computes the aggregate.
    The paired test needs the same indicator per case, derived the same way, or the
    regression test and the absolute test would be measuring two different things.

    The score rows carry no `series_instance_uid` of their own, so they are joined to the
    case-metric rows by `case_key` -- the primary key `evaluation_case_scores` is keyed on.
    """
    threshold = run.operating_thresholds
    uid_by_case: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for row in run.case_metrics:
        uid_by_case.setdefault(row.case_key, (row.series_instance_uid, row.strata or {}))

    out: list[tuple[str, str, float]] = []
    for score in run.case_scores:
        case_key = str(score.get("case_key"))
        if case_key not in uid_by_case:
            continue
        uid, strata = uid_by_case[case_key]
        if not criteria_mod.selector_matches(strata, selector):
            continue
        case_score, label = score.get("case_score"), score.get("case_label")
        if case_score is None or label is None:
            continue
        point = _threshold_value(threshold)
        if point is None:
            continue
        predicted = float(case_score) >= point
        positive = int(label) == 1
        if metric == "sensitivity":
            if not positive:
                continue
            value = 1.0 if predicted else 0.0
        elif metric == "specificity":
            if positive:
                continue
            value = 0.0 if predicted else 1.0
        else:  # ppv
            if not predicted:
                continue
            value = 1.0 if positive else 0.0
        out.append((uid, str(score.get("patient_key")), value))
    return out


def _threshold_value(thresholds: Mapping[str, Any]) -> float | None:
    for entry in thresholds.values():
        value = entry.get("value") if isinstance(entry, Mapping) else entry
        if isinstance(value, (int, float)):
            return float(value)
    return None


def paired_series(
    candidate: RunView,
    incumbent: RunView,
    metric: str,
    selector: Mapping[str, Any],
) -> tuple[list[str], list[float], list[float], list[str]]:
    """Aligned per-case arrays over the cases present in BOTH runs. Section 7.9.2.

    Joined on `series_instance_uid`, because section 7.7.2 says so in as many words: "the
    series UID is what the pairing function of 7.9.2 joins on". A case eligible in one run
    and not the other DROPS OUT of the pair rather than being compared against nothing,
    and the resulting `n` is reported so that a shrunken pairing is visible rather than
    silently changing what was measured.
    """
    if metric in criteria_mod.OPERATING_POINT_METRICS:
        new_pairs = {u: (pk, v) for u, pk, v in _indicator_rows(candidate, metric, selector)}
        old_pairs = {u: (pk, v) for u, pk, v in _indicator_rows(incumbent, metric, selector)}
    else:
        new_pairs = {
            r.series_instance_uid: (r.patient_key, float(r.value))
            for r in candidate.case_metrics
            if r.metric == metric
            and r.eligible
            and r.value is not None
            and criteria_mod.selector_matches(r.strata or {}, selector)
        }
        old_pairs = {
            r.series_instance_uid: (r.patient_key, float(r.value))
            for r in incumbent.case_metrics
            if r.metric == metric
            and r.eligible
            and r.value is not None
            and criteria_mod.selector_matches(r.strata or {}, selector)
        }

    uids = sorted(set(new_pairs) & set(old_pairs))
    return (
        uids,
        [new_pairs[u][1] for u in uids],
        [old_pairs[u][1] for u in uids],
        # The candidate's key. Both runs cover the same cohort under MOS-EVID-086, so the
        # two agree; taking one side keeps the cluster unit single-sourced.
        [new_pairs[u][0] for u in uids],
    )


def non_inferiority(
    new_vals: Sequence[float],
    old_vals: Sequence[float],
    patient_keys: Sequence[str],
    margin: float,
    *,
    b: int = metrics_mod.DEFAULT_BOOTSTRAP_B,
    seed: int = metrics_mod.DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """H0: mu_d <= -delta against H1: mu_d > -delta. MOS-EVID-085.

    Passes when the lower bound of the ONE-SIDED 95 % patient-cluster bootstrap interval
    of `mean(d)` exceeds `-margin`. `alpha = 0.10` in the two-sided percentile call yields
    the 5th percentile, which is that one-sided bound -- section 7.9.2's snippet does
    exactly this and says so.

    Note what is NOT here: no comparison of `mean(new)` against `mean(old)`. The statistic
    is the mean of the PER-CASE DIFFERENCES, whose sampling variability the cluster
    bootstrap measures, and the decision is against `-delta` rather than against zero.
    Those two differences are the whole of section 7.9.1, and MOS-EVID-089 falls out of
    the first: the interval narrows with n, so a genuinely equivalent model passes MORE
    reliably on a larger cohort rather than less.
    """
    if len(new_vals) != len(old_vals) or len(new_vals) != len(patient_keys):
        raise ValueError("paired arrays differ in length; the join produced a mismatch")
    if margin <= 0.0:
        raise ValueError("margin (delta) MUST be > 0 -- MOS-EVID-087")
    deltas = [float(n) - float(o) for n, o in zip(new_vals, old_vals)]
    if not deltas:
        return {
            "mean_delta": float("nan"),
            "ci_lower_95_one_sided": float("nan"),
            "margin": float(margin),
            "passed": False,
            "n": 0,
            "n_patients": 0,
        }
    lo, _hi = ci_mod.cluster_bootstrap_ci(
        deltas, list(patient_keys), b=b, seed=seed, alpha=0.10
    )
    return {
        "mean_delta": sum(deltas) / len(deltas),
        "ci_lower_95_one_sided": float(lo),
        "margin": float(margin),
        "passed": bool(lo > -margin),
        "n": len(deltas),
        "n_patients": len(set(patient_keys)),
    }


def catastrophic_count(
    new_vals: Sequence[float], old_vals: Sequence[float], delta: float
) -> int:
    """Cases where the INCUMBENT exceeds the candidate by more than `delta`. MOS-EVID-088.

    "This catches the failure mode non-inferiority on the mean cannot: three cases
    collapsing from 0.85 to 0.05 while a hundred others improve slightly." Per-case by
    construction -- a mean cannot express it, which is why this is a separate criterion
    shape and not a tighter margin on the same one.
    """
    return sum(1 for n, o in zip(new_vals, old_vals) if (float(o) - float(n)) > delta)


# =====================================================================================
# The absolute half's inputs.
# =====================================================================================
def build_aggregates(
    criteria_document: Mapping[str, Any], run: RunView
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """`{(metric, stratum): aggregate entry}` for every pair the document names.

    RECOMPUTED from `evaluation_case_metrics` rather than read from the run's stored
    `aggregate_metrics`, because MOS-EVID-067 makes a subgroup criterion "evaluated by
    filtering on this column" and a Capability's strata are authored independently of any
    run -- `small_effusion` and `thick_slice` in section 7.8.2's worked example have no
    precomputed aggregate on any run. MOS-EVID-066 requires the stored block and a
    recomputation to agree to 1e-9 anyway, so this cannot disagree with the report.

    The aggregation itself is `medos.evidence.aggregate.aggregate_metrics`, the reference
    implementation MOS-EVID-066 names. Only the SELECTION is done here, with the chapter's
    own eight-operator selector grammar (`medos.evidence.criteria`).
    """
    spec = criteria_document.get("spec", {})
    strata = criteria_mod.strata_of(criteria_document)
    conventions = metrics_mod.metric_conventions(
        fp_volume_threshold_ml=float(spec.get("fp_volume_threshold_ml") or 0.0)
    )

    wanted: dict[str, list[tuple[str, Mapping[str, Any] | None]]] = {}
    for criterion in spec.get("absolute", []) or []:
        if not isinstance(criterion, Mapping):
            continue
        stratum = str(criterion.get("stratum", ""))
        wanted.setdefault(stratum, []).append(
            (str(criterion.get("metric", "")), criterion.get("operating_threshold"))
        )

    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for stratum, entries in wanted.items():
        selector = strata.get(stratum)
        if selector is None:
            continue
        rows = criteria_mod.filter_rows(run.case_metrics, selector)
        # `evaluation_case_scores` has no `strata` column of its own (section 7.7.2: the
        # strata live on `evaluation_case_metrics`), so a subgroup's score rows are the
        # ones whose CASE is in the subgroup, joined on `case_key` -- the key that table
        # is keyed on. Filtering the score rows directly on a selector they cannot satisfy
        # would silently empty every count-based subgroup aggregate, which
        # `evaluate_absolute` would then report as INDETERMINATE for the whole family.
        in_stratum = {r.case_key for r in rows}
        scores = [
            s for s in run.case_scores if str(s.get("case_key")) in in_stratum
        ]
        operating_point = next(
            (dict(op) for _m, op in entries if isinstance(op, Mapping)), None
        )
        try:
            block = aggregate_mod.aggregate_metrics(
                rows,
                conventions=conventions,
                case_scores=scores,
                operating_point=operating_point,
            )
        except (aggregate_mod.AggregationError, metrics_mod.UnknownMetric, ValueError):
            # An aggregate that cannot be computed is OMITTED, never defaulted:
            # `evaluate_absolute` turns an absent one into INDETERMINATE with reason
            # `aggregate_absent`, which is the outcome that needs a human. A zero would
            # read as FAIL and anything else as PASS, and neither is true.
            continue
        for entry in block:
            out[(str(entry["metric"]), stratum)] = entry
    return out


# =====================================================================================
# The regression half.
# =====================================================================================
def evaluate_regression(
    criterion: Mapping[str, Any],
    candidate: RunView,
    incumbent: RunView,
    selector: Mapping[str, Any],
) -> CriterionResult:
    """One regression criterion over the paired runs. Never raises.

    The order of refusals is deliberate:
      1. MOS-EVID-086's pairing binding. An unpaired comparison is not a regression test,
         so nothing downstream means anything -- and the answer is INDETERMINATE, not
         FAIL: a mismatched cohort needs a rerun, not a different model.
      2. MOS-EVID-078's operating point, on BOTH runs. A sensitivity at 0.50 and one at
         0.35 are not comparable numbers, and differencing them is worse than not
         comparing them at all.
      3. `min_cases` / `min_patients`, over the PAIRED set. The intersection is what was
         actually tested; a criterion claiming 60 cases while 12 paired is exactly the
         defect this check surfaces.
    """
    cid = str(criterion.get("id", "?"))
    severity = str(criterion.get("severity", "blocking"))
    metric = str(criterion.get("metric", ""))
    stratum = str(criterion.get("stratum", ""))
    bound = str(criterion.get("bound", "ci_lower_95"))
    margin = criterion.get("margin")

    def out(status: str, **kw: Any) -> CriterionResult:
        return CriterionResult(
            id=cid, status=status, severity=severity, metric=metric, stratum=stratum,
            bound=bound, **kw,
        )

    if candidate.pairing_binding() != incumbent.pairing_binding():
        return out("INDETERMINATE", reason="unpaired_runs")

    if metric in criteria_mod.OPERATING_POINT_METRICS:
        want = criterion.get("operating_threshold") or {}
        name = want.get("name")
        for run in (candidate, incumbent):
            got = run.operating_thresholds.get(name) if isinstance(name, str) else None
            got_value = got.get("value") if isinstance(got, Mapping) else got
            if got is None or not _close(got_value, want.get("value")):
                return out("INDETERMINATE", reason="threshold_mismatch")

    uids, new_vals, old_vals, keys = paired_series(candidate, incumbent, metric, selector)
    n, n_patients = len(uids), len(set(keys))

    min_cases = criterion.get("min_cases")
    min_patients = criterion.get("min_patients")
    if (isinstance(min_cases, int) and n < min_cases) or (
        isinstance(min_patients, int) and n_patients < min_patients
    ):
        status = "FAIL" if criterion.get("on_insufficient_cases") == "fail" else "INDETERMINATE"
        return out(status, reason="insufficient_cases", n=n, n_patients=n_patients)

    if isinstance(margin, bool) or not isinstance(margin, (int, float)) or float(margin) <= 0:
        return out("INDETERMINATE", reason="margin_absent", n=n, n_patients=n_patients)

    if bound == "catastrophic_count":
        # MOS-EVID-088. The comparison is `count <op> value` -- a count of per-case
        # collapses against an authored ceiling, never one aggregate against another.
        observed = catastrophic_count(new_vals, old_vals, float(margin))
        op = str(criterion.get("op", "<="))
        required = criterion.get("value")
        if isinstance(required, bool) or not isinstance(required, (int, float)):
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
            op=op,
            required=float(required),
            n=n,
            n_patients=n_patients,
            reason=None if passed else "catastrophic_cases",
        )

    result = non_inferiority(new_vals, old_vals, keys, float(margin))
    return out(
        "PASS" if result["passed"] else "FAIL",
        observed=float(result["ci_lower_95_one_sided"]),
        op=">",
        required=-float(margin),
        n=n,
        n_patients=n_patients,
        reason=None if result["passed"] else "non_inferiority_not_shown",
    )


# =====================================================================================
# The gate.  MOS-EVID-090, section 7.9.3.
# =====================================================================================
def evaluate_gate(
    candidate_report: ReportView | None,
    incumbent_report: ReportView | None,
    criteria_document: Mapping[str, Any],
    binding: GateBinding,
    *,
    now: datetime | None = None,
) -> GateResult:
    """`(verdict, per-criterion results)`. Total: every path returns a verdict.

    The chapter's snippet returns a SINGLE `integrity` entry on a step-0 failure and stops.
    This returns that entry AND a SKIPPED entry for every criterion, because MOS-EVID-113
    requires one entry per criterion in the bound version and
    `deployment_gate_decisions.criterion_results` is the row that answers "why was this
    deployment allowed" (MOS-STORE-302b) -- a decision record holding one line reading
    `signature_invalid` does not answer it. REPORTED as an addition.
    """
    now = now or datetime.now(UTC)
    spec = criteria_document.get("spec", {})
    strata = criteria_mod.strata_of(criteria_document)
    absolute = [c for c in (spec.get("absolute") or []) if isinstance(c, Mapping)]
    regression = [c for c in (spec.get("regression") or []) if isinstance(c, Mapping)]

    # -- 0. Report integrity and subject binding. ------------------------------------
    reason = _integrity_failure(candidate_report, binding, now=now)
    if reason is not None:
        entries = [
            CriterionResult(id="integrity", status="FAIL", severity="blocking", reason=reason)
        ]
        entries.extend(_all_skipped(absolute + regression, "integrity_failed"))
        return GateResult("FAIL", tuple(entries))

    assert candidate_report is not None  # narrowed by `_integrity_failure`
    results: list[CriterionResult] = []

    # -- 1. Absolute criteria, against the candidate run alone. -----------------------
    aggregates = build_aggregates(criteria_document, candidate_report.run)
    thresholds = dict(candidate_report.run.operating_thresholds)
    for criterion in absolute:
        results.append(
            criteria_mod.evaluate_absolute(
                criterion, aggregates, operating_thresholds=thresholds
            )
        )

    # -- 2. Regression criteria, only when an incumbent exists. -----------------------
    if incumbent_report is None:
        # Section 7.9.3 verbatim. A first deployment has nothing to regress against, and
        # that is not a licence to skip the absolute bar -- only the regression half is
        # skipped, and each criterion gets its own entry (MOS-EVID-113).
        results.extend(_all_skipped(regression, "no_incumbent_first_deployment"))
    else:
        for criterion in regression:
            results.append(
                evaluate_regression(
                    criterion,
                    candidate_report.run,
                    incumbent_report.run,
                    strata.get(str(criterion.get("stratum", "")), {}),
                )
            )

    # MOS-EVID-114: "the published combination rule", singular. Imported, never restated.
    verdict = report_mod.derive_verdict(
        [r.as_dict() for r in results], kind=candidate_report.kind
    )
    return GateResult(verdict, tuple(results))


def _all_skipped(
    criteria: Sequence[Mapping[str, Any]], reason: str
) -> list[CriterionResult]:
    return [
        CriterionResult(
            id=str(c.get("id", "?")),
            status="SKIPPED",
            severity=str(c.get("severity", "blocking")),
            metric=c.get("metric"),
            stratum=c.get("stratum"),
            reason=reason,
        )
        for c in criteria
    ]


def _integrity_failure(
    report: ReportView | None, binding: GateBinding, *, now: datetime
) -> str | None:
    """The first failed step-0 check, or None. Section 7.9.3 step 0, in its order."""
    if report is None:
        return "no_report"
    if not report.signature_verified:
        return "signature_invalid"  # MOS-EVID-121
    if report.criteria_version != binding.criteria_version:
        return "criteria_version_mismatch"  # MOS-EVID-094
    if report.subject != binding.candidate_subject:
        return "subject_mismatch"
    if report.valid_until <= now:
        return "report_expired"  # MOS-EVID-115
    if report.status != "ACTIVE":
        return "report_revoked"  # MOS-EVID-126, MOS-STORE-301a
    if report.run.dataset_version_id != binding.dataset_version_id:
        return "wrong_cohort"
    if report.run.partition != binding.partition:
        return "wrong_partition"
    if not report.reference_of_record:
        return "reference_not_of_record"  # MOS-EVID-042, acceptance check 7
    if report.run.state != "SUCCEEDED":
        return "run_not_succeeded"  # MOS-EVID-061
    return None


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-12
    return a == b
