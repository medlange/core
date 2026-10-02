# SPDX-License-Identifier: Apache-2.0
"""The metric registry and the ONE per-case metric computation. Chapter 7 section 7.6.

Requirements implemented here
-----------------------------
MOS-EVID-047  the primary segmentation figure is mean-of-per-case Dice,
              `dice_mean_per_case`, the unweighted arithmetic mean over eligible cases.
MOS-EVID-048  `dice_pooled` MUST also be computed and persisted on every segmentation
              run; it has no per-case decomposition, so this module persists the three
              voxel counts it is computed from (MOS-EVID-068).
MOS-EVID-049  the unqualified word "dice" is never a field name, key or label.
MOS-EVID-051  the empty-ground-truth policy is `exclude_and_report_separately`, with one
              deliberate asymmetry: a MISSED finding scores 0.0 and is always counted.
MOS-EVID-053  `fp_volume_threshold_ml` is declared per capability and recorded on the run.
MOS-EVID-054  every metric is registered with a stable id and the registry has a version;
              a run MUST NOT persist a metric id absent from the registry.
MOS-EVID-055  a threshold metric without its operating point is not a measurement.
MOS-EVID-065  per-case metrics are persisted; aggregates alone MUST NOT be stored.
MOS-EVID-067  `strata` carries the acquisition and clinical stratum of each case.
MOS-EVID-068  `gt_voxels`, `pred_voxels` and `intersection_voxels` are persisted for
              every segmentation case EVEN WHEN the per-case value is NULL.

ONE EVALUATION IMPLEMENTATION, NOT TWO
--------------------------------------
docs/spec/15-delivery.md section 15.2.5 states it as a release requirement: "One
evaluation implementation, not two." The failure it prevents is specific and silent. A
development notebook computes Dice one way, the gate computes it another, both are
defensible, and the two disagree by 0.01 -- which is smaller than any margin anyone
would notice and larger than the difference the gate is being asked to rule on. Nobody
ever sees the discrepancy, because the two numbers are never printed side by side.

So `case_metric_rows()` is the only function in this repository that decides what a
per-case metric value is, whether the case is eligible, and why it is not. The gate
(section 7.9.2) pairs rows it produced; the aggregate block (`medos.evidence.aggregate`)
is a pure function of rows it produced; a development run calls the same function with
the same arguments. `observation_from_masks()` is the only place a mask becomes a count,
for the same reason.

WHY ELIGIBILITY IS A PER-METRIC DECISION AND NOT A PER-CASE ONE
---------------------------------------------------------------
A case with zero reference voxels is excluded from `dice_mean_per_case` (MOS-EVID-051)
and is a perfectly ordinary, eligible NEGATIVE for `brier` or `specificity`. A case with
an empty PREDICTION has a defined Dice of 0.0 -- the asymmetry MOS-EVID-051 spells out --
and an undefined `hd95_mm`, because the distance from a surface to nothing is not a
number. Both facts are properties of the (case, metric) pair, which is exactly the grain
`evaluation_case_metrics` is keyed at.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "METRIC_REGISTRY_VERSION",
    "REGISTRY",
    "MetricSpec",
    "THRESHOLD_METRICS",
    "EMPTY_GT_METRICS",
    "SEGMENTATION_PER_CASE_METRICS",
    "UNDEFINED_REASONS",
    "DEFAULT_BOOTSTRAP_B",
    "DEFAULT_BOOTSTRAP_SEED",
    "DEFAULT_CI_METHOD",
    "CI_METHODS",
    "metric_conventions",
    "CaseObservation",
    "CaseMetricRow",
    "case_dice",
    "observation_from_masks",
    "case_metric_rows",
    "UnknownMetric",
]


class UnknownMetric(LookupError):
    """MOS-EVID-054: a metric id absent from the registry version recorded on the run."""


# =====================================================================================
# The registry.  MOS-EVID-054.
# =====================================================================================
@dataclass(frozen=True)
class MetricSpec:
    """One row of the MOS-EVID-054 table.

    `aggregation` is the reference aggregation `medos.evidence.aggregate` applies; it is
    part of the registry and not a caller's choice, because "mean" and "median" over the
    same per-case column are different published numbers.
    """

    id: str
    applies_to: tuple[str, ...]
    per_case: bool
    needs_threshold: bool
    unit: str
    aggregation: str


def _spec(
    metric_id: str,
    applies_to: str,
    per_case: bool,
    needs_threshold: bool,
    unit: str,
    aggregation: str,
) -> MetricSpec:
    return MetricSpec(
        id=metric_id,
        applies_to=tuple(applies_to.split(",")),
        per_case=per_case,
        needs_threshold=needs_threshold,
        unit=unit,
        aggregation=aggregation,
    )


# MOS-EVID-054's table, transcribed row for row, PLUS the four empty-GT ids that
# MOS-EVID-052 and MOS-STORE-296 require to be persisted as metric rows.
#
# THE SPECIFICATION CONTRADICTS ITSELF HERE AND THE CONTRADICTION IS REPORTED, NOT
# SILENTLY RESOLVED: MOS-EVID-052 requires the five-metric empty-GT block to be persisted
# and reported, MOS-STORE-296 names all five again as "its own metric rows", and
# MOS-EVID-054 forbids persisting any metric id absent from the registry -- whose table
# lists only `empty_gt_false_positive_rate`. Obeying MOS-EVID-054 literally makes
# MOS-EVID-052 unimplementable. The four missing ids are therefore registered here, in
# registry version 1, and the defect is reported upward rather than left as a divergence
# a reader would have to discover.
_ROWS: tuple[MetricSpec, ...] = (
    _spec("dice_mean_per_case", "segmentation", True, False, "1", "mean"),
    _spec("dice_pooled", "segmentation", False, False, "1", "pooled"),
    _spec("iou_mean_per_case", "segmentation", True, False, "1", "mean"),
    _spec("hd95_mm", "segmentation", True, False, "mm", "median"),
    _spec("assd_mm", "segmentation", True, False, "mm", "median"),
    _spec("volume_error_ml", "segmentation", True, False, "mL", "mean"),
    _spec("volume_ape", "segmentation", True, False, "1", "median"),
    _spec("sensitivity", "classification,detection", False, True, "1", "count"),
    _spec("specificity", "classification", False, True, "1", "count"),
    _spec("ppv", "classification,detection", False, True, "1", "count"),
    _spec("froc_sensitivity", "detection", False, True, "1", "interpolated"),
    _spec("auroc", "classification", False, False, "1", "curve"),
    _spec("auprc", "classification", False, False, "1", "curve"),
    _spec("ece_15bin", "classification", False, False, "1", "binned"),
    _spec("brier", "classification", True, False, "1", "mean"),
    _spec("mae", "measurement", True, False, "metric unit", "mean"),
    _spec("empty_gt_false_positive_rate", "segmentation", False, True, "1", "count"),
    # MOS-EVID-052 / MOS-STORE-296, absent from the MOS-EVID-054 table. See above.
    _spec("empty_gt_case_count", "segmentation", False, False, "cases", "count"),
    _spec("empty_gt_mean_fp_volume_ml", "segmentation", False, False, "mL", "mean"),
    _spec("empty_gt_p95_fp_volume_ml", "segmentation", False, False, "mL", "p95"),
    _spec("empty_gt_max_fp_volume_ml", "segmentation", False, False, "mL", "max"),
)

REGISTRY: Mapping[str, MetricSpec] = {row.id: row for row in _ROWS}

# The version of the table above. Recorded on every run (MOS-EVID-054) and reconciled
# with `metric_conventions.metric_registry_version` by a database CHECK, so a run can
# always be read back against the registry it was measured under.
METRIC_REGISTRY_VERSION = 1

# MOS-EVID-055: these four MUST NOT be persisted, reported or published without the
# operating point that produced them.
THRESHOLD_METRICS = frozenset(m.id for m in _ROWS if m.needs_threshold)

# MOS-EVID-052's block.
EMPTY_GT_METRICS = (
    "empty_gt_case_count",
    "empty_gt_false_positive_rate",
    "empty_gt_mean_fp_volume_ml",
    "empty_gt_p95_fp_volume_ml",
    "empty_gt_max_fp_volume_ml",
)

# What `case_metric_rows()` computes for a segmentation subject from counts and volumes.
# `hd95_mm` and `assd_mm` are registered and NOT in this tuple: they need the surfaces,
# not the counts, so they arrive through `CaseObservation.supplied` -- computed by the
# geometry code, carried through the same eligibility rules as everything else.
SEGMENTATION_PER_CASE_METRICS = (
    "dice_mean_per_case",
    "iou_mean_per_case",
    "volume_error_ml",
    "volume_ape",
)

# The `undefined_reason` vocabulary of MOS-EVID-065 / section 12.12. Mirrored exactly.
UNDEFINED_REASONS = ("empty_ground_truth", "empty_prediction", "excluded", "error")

# MOS-EVID-058: "The default is the percentile method with B = 2000". The seed is
# recorded on the run and the interval MUST be reproducible from the per-case rows alone.
DEFAULT_BOOTSTRAP_B = 2000
DEFAULT_BOOTSTRAP_SEED = 20260101
DEFAULT_CI_METHOD = "percentile_cluster_bootstrap"
CI_METHODS = ("percentile_cluster_bootstrap", "bca_cluster_bootstrap")


def metric_conventions(
    *,
    fp_volume_threshold_ml: float,
    ci_method: str = DEFAULT_CI_METHOD,
    bootstrap_b: int = DEFAULT_BOOTSTRAP_B,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
    metric_registry_version: int = METRIC_REGISTRY_VERSION,
) -> dict[str, Any]:
    """The section 7.6 block, recorded verbatim on every run. Section 7.12.1's shape.

    The two conventions are NOT parameters. `dice_aggregation` and `empty_gt_policy` are
    written as constants because MOS-EVID-047 and MOS-EVID-051 fix them platform-wide and
    "both are irreversible the moment a number is published" -- a keyword argument here
    would be an invitation to publish an incomparable figure, and the database CHECK
    `evaluation_runs_conventions_pinned` would refuse the row anyway.

    `fp_volume_threshold_ml` IS a parameter because MOS-EVID-053 says it is declared per
    capability in the AcceptanceCriteria. For `pleural_effusion` the declared value is
    10.0; this function does not know that, and inventing a default here would put the
    capability's clinical judgement in the wrong module.
    """
    if ci_method not in CI_METHODS:
        raise ValueError(
            f"MOS-EVID-058: ci_method MUST be one of {CI_METHODS}, got {ci_method!r}"
        )
    if bootstrap_b < 1:
        raise ValueError("MOS-EVID-058: bootstrap_b MUST be positive")
    if not (fp_volume_threshold_ml > 0):
        raise ValueError(
            "MOS-EVID-053: fp_volume_threshold_ml MUST be a positive volume in mL"
        )
    return {
        "dice_aggregation": "mean_of_per_case",
        "empty_gt_policy": "exclude_and_report_separately",
        "fp_volume_threshold_ml": float(fp_volume_threshold_ml),
        "ci_method": ci_method,
        "bootstrap_b": int(bootstrap_b),
        "bootstrap_seed": int(bootstrap_seed),
        "metric_registry_version": int(metric_registry_version),
    }


# =====================================================================================
# One case, as measured.  MOS-EVID-067, MOS-EVID-068, MOS-EVID-069.
# =====================================================================================
@dataclass(frozen=True)
class CaseObservation:
    """Everything measured about one case, before any metric is named.

    The three voxel counts are the primitive: every overlap metric in
    SEGMENTATION_PER_CASE_METRICS is a function of them and of the two volumes, so they
    are what gets persisted (MOS-EVID-068) and the metrics are what gets derived. The
    other direction -- persisting Dice and deriving the counts -- is not invertible, and
    `dice_pooled` and its bootstrap (MOS-EVID-060) need the counts.

    `strata` is MOS-EVID-067's block: "a stratum that is not persisted cannot be gated
    on". It carries the acquisition strata and the capability's declared clinical strata.
    NO PHI: `patient_key` is the HMAC of MOS-EVID-010, and the UIDs are UIDs.
    """

    case_key: str
    patient_key: str
    study_instance_uid: str
    series_instance_uid: str
    gt_voxels: int
    pred_voxels: int
    intersection_voxels: int
    gt_volume_ml: float
    pred_volume_ml: float
    strata: Mapping[str, Any] = field(default_factory=dict)
    # MOS-STORE-299: the continuous, PRE-threshold score and the reference label, so a
    # ROC or PR curve is recomputable and a threshold re-selectable without re-running
    # inference.
    case_score: float | None = None
    case_label: int | None = None
    candidates: tuple[Mapping[str, Any], ...] = ()
    # Metrics computed elsewhere (hd95_mm, assd_mm from the surfaces; mae from the
    # measurement code) and carried through the SAME eligibility rules. A value of None
    # means the geometry code could not define it for this case.
    supplied: Mapping[str, float | None] = field(default_factory=dict)
    # Set when the case could not be evaluated at all. Every metric row for the case is
    # then NULL with this reason and `eligible = false` -- an errored case is never
    # silently dropped, because a cohort that shrinks without saying so is how n moves.
    error: str | None = None
    excluded: str | None = None

    def __post_init__(self) -> None:
        if self.intersection_voxels > min(self.gt_voxels, self.pred_voxels):
            raise ValueError(
                f"case {self.case_key}: intersection_voxels "
                f"{self.intersection_voxels} exceeds the smaller mask "
                f"(gt={self.gt_voxels}, pred={self.pred_voxels}); an impossible "
                "intersection makes dice_pooled a plausible number with no traceable "
                "defect (MOS-EVID-068)"
            )
        if min(self.gt_voxels, self.pred_voxels, self.intersection_voxels) < 0:
            raise ValueError(f"case {self.case_key}: voxel counts MUST NOT be negative")
        if self.case_label is not None and self.case_label not in (0, 1):
            raise ValueError("case_label is the reference: 0 or 1 (section 12.12)")
        for metric_id in self.supplied:
            if metric_id not in REGISTRY:
                raise UnknownMetric(
                    f"MOS-EVID-054: {metric_id!r} is not in registry version "
                    f"{METRIC_REGISTRY_VERSION}"
                )


@dataclass(frozen=True)
class CaseMetricRow:
    """One row of `evaluation_case_metrics`, exactly as it is persisted."""

    case_key: str
    patient_key: str
    study_instance_uid: str
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
    strata: Mapping[str, Any]


def case_dice(
    gt_voxels: int, pred_voxels: int, intersection_voxels: int
) -> float | None:
    """MOS-EVID-051's reference implementation, over counts rather than masks.

    Chapter 7 prints it over numpy arrays:

        if g.sum() == 0: return None          # empty GT: excluded from the aggregate
        return float(2.0 * (p & g).sum() / (p.sum() + g.sum()))

    which is this function applied to `observation_from_masks()`'s output. Counts rather
    than masks here because the persisted row carries counts (MOS-EVID-068), so the gate
    and the recomputation check (MOS-EVID-066) can re-derive every per-case value from
    the database alone, months later, with no image in reach.

    The two edge cases are the whole convention:
      * `gt_voxels == 0`  -> None. NOT 1.0 and NOT 0.0. Scoring an empty-GT case 1.0 puts
        a floor under the headline figure equal to the negative fraction of the cohort,
        which lets a vendor move a published number from 0.72 to 0.89 by enriching
        negatives and changing nothing about the model.
      * `gt_voxels > 0, pred_voxels == 0` -> 0.0, and it COUNTS. A missed finding is a
        failure of the thing being measured.
    """
    if gt_voxels == 0:
        return None
    return float(2.0 * intersection_voxels / (pred_voxels + gt_voxels))


def _case_iou(gt_voxels: int, pred_voxels: int, intersection_voxels: int) -> float | None:
    if gt_voxels == 0:
        return None
    union = pred_voxels + gt_voxels - intersection_voxels
    if union == 0:  # pragma: no cover - unreachable while gt_voxels > 0
        return None
    return float(intersection_voxels / union)


def observation_from_masks(
    pred_mask: Any,
    gt_mask: Any,
    *,
    voxel_volume_ml: float,
    case_key: str,
    patient_key: str,
    study_instance_uid: str,
    series_instance_uid: str,
    strata: Mapping[str, Any] | None = None,
    case_score: float | None = None,
    case_label: int | None = None,
    candidates: Sequence[Mapping[str, Any]] = (),
    supplied: Mapping[str, float | None] | None = None,
) -> CaseObservation:
    """Two masks in source geometry -> the counts that get persisted. One place.

    `voxel_volume_ml` is the SOURCE-geometry voxel volume (section 12.12: "source-geometry
    volumes"), so a resampled prediction must be mapped back before it reaches here --
    reporting a volume in the model's working geometry is a measurement of the
    preprocessing, not of the patient.

    The boolean reduction happens here and nowhere else: `astype(bool)` on a probability
    map and `> 0.5` on the same map are different masks, and two call sites that each
    chose one is the concrete form the "two implementations" defect takes.
    """
    pred = pred_mask.astype(bool)
    gt = gt_mask.astype(bool)
    if pred.shape != gt.shape:
        raise ValueError(
            f"case {case_key}: prediction {pred.shape} and reference {gt.shape} are not "
            "the same grid; an overlap metric over two grids is not a measurement"
        )
    gt_voxels = int(gt.sum())
    pred_voxels = int(pred.sum())
    intersection = int((pred & gt).sum())
    return CaseObservation(
        case_key=case_key,
        patient_key=patient_key,
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        gt_voxels=gt_voxels,
        pred_voxels=pred_voxels,
        intersection_voxels=intersection,
        gt_volume_ml=float(gt_voxels * voxel_volume_ml),
        pred_volume_ml=float(pred_voxels * voxel_volume_ml),
        strata=dict(strata or {}),
        case_score=case_score,
        case_label=case_label,
        candidates=tuple(dict(c) for c in candidates),
        supplied=dict(supplied or {}),
    )


def _overlap_value(obs: CaseObservation, metric: str) -> float | None:
    if metric == "dice_mean_per_case":
        return case_dice(obs.gt_voxels, obs.pred_voxels, obs.intersection_voxels)
    if metric == "iou_mean_per_case":
        return _case_iou(obs.gt_voxels, obs.pred_voxels, obs.intersection_voxels)
    if metric == "volume_error_ml":
        if obs.gt_voxels == 0:
            return None
        return float(obs.pred_volume_ml - obs.gt_volume_ml)
    if metric == "volume_ape":
        if obs.gt_voxels == 0 or obs.gt_volume_ml == 0.0:
            return None
        return float(abs(obs.pred_volume_ml - obs.gt_volume_ml) / obs.gt_volume_ml)
    if metric == "brier":
        if obs.case_score is None or obs.case_label is None:
            return None
        return float((obs.case_score - float(obs.case_label)) ** 2)
    return obs.supplied.get(metric)


def case_metric_rows(
    obs: CaseObservation,
    *,
    metrics: Sequence[str] = SEGMENTATION_PER_CASE_METRICS,
) -> tuple[CaseMetricRow, ...]:
    """The one function that decides a per-case value, its eligibility and its reason.

    The eligibility ladder, in order, because the order is the convention:

      1. `error`      -- the case could not be evaluated. Every metric is NULL, reason
                         `error`, ineligible. The case still gets ROWS: a cohort that
                         silently shrinks is how `n` moves between two runs that are
                         supposed to be paired (MOS-EVID-086).
      2. `excluded`   -- a declared, reasoned exclusion. Same shape, reason `excluded`.
      3. empty GT     -- MOS-EVID-051 row 3 and 4: NULL, reason `empty_ground_truth`,
                         ineligible, and counted instead in the empty-GT block. This is
                         the only place the platform's headline figure can be made a
                         function of cohort composition, so it is the one place that has
                         both a database CHECK and this branch.
      4. empty pred   -- with a NON-empty reference. Dice and IoU are DEFINED and equal
                         0.0 and the case IS eligible (MOS-EVID-051's deliberate
                         asymmetry). A surface distance is undefined, so a supplied
                         `hd95_mm` of None becomes reason `empty_prediction`.
      5. otherwise    -- the value, eligible.

    Raises `UnknownMetric` for an id absent from the registry: MOS-EVID-070 makes such a
    row an INVALIDATED run, and refusing to compute it is cheaper than invalidating it.
    """
    for metric in metrics:
        if metric not in REGISTRY:
            raise UnknownMetric(
                f"MOS-EVID-054: {metric!r} is not in registry version "
                f"{METRIC_REGISTRY_VERSION}; a run MUST NOT persist an unregistered "
                "metric id"
            )

    rows: list[CaseMetricRow] = []
    for metric in metrics:
        value: float | None
        reason: str | None
        eligible: bool

        # Each branch assigns the reason on its own line rather than packing the three
        # into a tuple: this is the executable form of MOS-EVID-051 and
        # `test_one_evaluation_implementation_not_two` greps for a SECOND module that
        # assigns one. A decision that cannot be grepped for cannot be shown to be made
        # in one place.
        if obs.error is not None:
            value = None
            reason = "error"
            eligible = False
        elif obs.excluded is not None:
            value = None
            reason = "excluded"
            eligible = False
        elif obs.gt_voxels == 0 and REGISTRY[metric].applies_to[0] == "segmentation":
            # Rows 3 and 4 of the MOS-EVID-051 table. `value` is NULL for every
            # segmentation metric, not only for Dice: the empty-GT case is reported in
            # its own block, and a cohort whose `n` differs metric by metric on the same
            # eligible set is a reporting trap rather than extra information.
            value = None
            reason = "empty_ground_truth"
            eligible = False
        else:
            value = _overlap_value(obs, metric)
            if value is None:
                # A supplied metric the geometry code could not define. With an empty
                # prediction the reason is that emptiness; otherwise the case is simply
                # out of this metric's scope and says so.
                reason = "empty_prediction" if obs.pred_voxels == 0 else "excluded"
                eligible = False
            else:
                reason = None
                eligible = True

        rows.append(
            CaseMetricRow(
                case_key=obs.case_key,
                patient_key=obs.patient_key,
                study_instance_uid=obs.study_instance_uid,
                series_instance_uid=obs.series_instance_uid,
                metric=metric,
                value=value,
                undefined_reason=reason,
                eligible=eligible,
                gt_voxels=obs.gt_voxels,
                pred_voxels=obs.pred_voxels,
                intersection_voxels=obs.intersection_voxels,
                gt_volume_ml=obs.gt_volume_ml,
                pred_volume_ml=obs.pred_volume_ml,
                strata=dict(obs.strata),
            )
        )
    return tuple(rows)
