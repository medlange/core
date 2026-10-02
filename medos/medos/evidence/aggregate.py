# SPDX-License-Identifier: Apache-2.0
"""The reference aggregation function. Chapter 7 sections 7.6.1, 7.6.2 and 7.7.2.

Requirements implemented here
-----------------------------
MOS-EVID-047  `dice_mean_per_case` is the primary figure: the UNWEIGHTED arithmetic mean
              of per-case Dice over the eligible cases.
MOS-EVID-048  `dice_pooled` MUST ALSO be computed and persisted on every segmentation run.
MOS-EVID-049  no reported figure is ever named `dice`.
MOS-EVID-051  empty-ground-truth cases are excluded from the Dice aggregate and reported
              separately.
MOS-EVID-052  the empty-GT block: case count, false-positive rate, mean, p95 and max
              false-positive volume.
MOS-EVID-054  every persisted metric id resolves in the registry.
MOS-EVID-055  a threshold metric carries the operating point that produced it.
MOS-EVID-056  every aggregate carries `n`, `n_patients`, a confidence interval and the
              convention block. A bare scalar MUST fail validation.
MOS-EVID-060  `dice_pooled` is bootstrapped from the persisted per-case counts.
MOS-EVID-066  `evaluation_runs.aggregate_metrics` MUST be recomputable from the per-case
              rows, to within 1e-9 relative, BY THIS FUNCTION.

WHY THIS IS A PURE FUNCTION OF ROWS
-----------------------------------
`aggregate_metrics()` takes rows and returns a list. It does not touch the database, the
object store, the model or the clock. That is what makes MOS-EVID-066 checkable at all:
the check is "run the reference function over the persisted rows and compare with the
persisted block", and it is only meaningful if the function cannot see anything the rows
do not contain. A version of this that reached for the run row to find, say, the
threshold, would make the check tautological in the one direction it matters --
the rows and the aggregate would agree because both came from the same hidden state.

THE STRATUM LABEL NAMES THE FILTER, NOT THE ELIGIBLE SET
--------------------------------------------------------
Section 7.12.1's example labels the Dice aggregates `"stratum": "gt_positive"`, which is
the same set as "the cases eligible for Dice" -- the empty-GT cases are the rest. This
module labels that aggregate with the subgroup filter that produced it (`all`, or a
declared clinical stratum) and reports eligibility through `n` and the empty-GT block,
because the two facts are different: `n` shrinking because a stratum was filtered and `n`
shrinking because half the cohort had no finding are different events, and one label
cannot mean both. The empty-GT block keeps the section 7.12.1 spelling: `gt_empty`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from medos.evidence import ci as _ci
from medos.evidence.metrics import (
    REGISTRY,
    THRESHOLD_METRICS,
    CaseMetricRow,
    UnknownMetric,
)

__all__ = [
    "Subgroup",
    "ALL_CASES",
    "aggregate_metrics",
    "validate_aggregates",
    "aggregates_agree",
    "AggregationError",
]


class AggregationError(ValueError):
    """An aggregate block that MOS-EVID-056 or MOS-EVID-055 forbids."""


# =====================================================================================
# Subgroups.  MOS-EVID-067: "Subgroup criteria are evaluated by filtering on this
# column; a stratum that is not persisted cannot be gated on."
# =====================================================================================
@dataclass(frozen=True)
class Subgroup:
    """A named filter over `evaluation_case_metrics.strata`.

    `where` maps a stratum field to either a literal (equality) or a
    `{"op": ..., "value": ...}` object with op in `eq`, `ne`, `lt`, `lte`, `gt`, `gte`,
    `in`. Declarative and JSON-serialisable on purpose: the same object is what an
    AcceptanceCriteria subgroup criterion (section 7.8) names, so the criterion and the
    aggregate it is evaluated against cannot be filtering on different things.

    A case whose `strata` LACKS the field does not match. It is not treated as a
    non-match to be quietly dropped either -- `aggregate_metrics` counts those cases and
    a subgroup that matched nothing is reported with `n = 0` rather than omitted, because
    a criterion whose stratum vanished must come back INDETERMINATE and cannot do that if
    the aggregate simply is not there (chapter 7 acceptance check 22).
    """

    name: str
    where: Mapping[str, Any]

    def matches(self, strata: Mapping[str, Any]) -> bool:
        for field_name, predicate in self.where.items():
            if field_name not in strata:
                return False
            observed = strata[field_name]
            if isinstance(predicate, Mapping) and "op" in predicate:
                if not _compare(observed, str(predicate["op"]), predicate.get("value")):
                    return False
            elif observed != predicate:
                return False
        return True


def _compare(observed: Any, op: str, bound: Any) -> bool:
    if op == "eq":
        return bool(observed == bound)
    if op == "ne":
        return bool(observed != bound)
    if op == "in":
        return observed in bound
    if observed is None or bound is None:
        return False
    if op == "lt":
        return bool(observed < bound)
    if op == "lte":
        return bool(observed <= bound)
    if op == "gt":
        return bool(observed > bound)
    if op == "gte":
        return bool(observed >= bound)
    raise AggregationError(f"unknown subgroup operator {op!r}")


ALL_CASES = Subgroup(name="all", where={})


# =====================================================================================
# The aggregation itself
# =====================================================================================
def _rows_as_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, CaseMetricRow):
            out.append(
                {
                    "case_key": row.case_key,
                    "patient_key": row.patient_key,
                    "series_instance_uid": row.series_instance_uid,
                    "metric": row.metric,
                    "value": row.value,
                    "undefined_reason": row.undefined_reason,
                    "eligible": row.eligible,
                    "gt_voxels": row.gt_voxels,
                    "pred_voxels": row.pred_voxels,
                    "intersection_voxels": row.intersection_voxels,
                    "gt_volume_ml": row.gt_volume_ml,
                    "pred_volume_ml": row.pred_volume_ml,
                    "strata": dict(row.strata),
                }
            )
        else:
            out.append(dict(row))
    return out


def _entry(
    metric: str,
    stratum: str,
    value: float | None,
    ci: tuple[float, float] | None,
    n: int,
    n_patients: int,
    *,
    operating_point: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """One aggregate object, in the shape section 7.12.1's `aggregates` array prints."""
    out: dict[str, Any] = {
        "metric": metric,
        "stratum": stratum,
        "value": None if value is None else float(value),
        "ci_low": None if ci is None else float(ci[0]),
        "ci_high": None if ci is None else float(ci[1]),
        "n": int(n),
        "n_patients": int(n_patients),
    }
    if operating_point is not None:
        out["operating_point"] = dict(operating_point)
    if note is not None:
        out["note"] = note
    return out


def _n_patients(rows: Sequence[Mapping[str, Any]]) -> int:
    return len({r["patient_key"] for r in rows})


def _statistic(aggregation: str):
    if aggregation == "median":
        return np.median
    return np.mean


def aggregate_metrics(
    case_rows: Iterable[Any],
    *,
    conventions: Mapping[str, Any],
    case_scores: Sequence[Mapping[str, Any]] = (),
    subgroups: Sequence[Subgroup] = (),
    operating_point: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The ONE aggregation. MOS-EVID-066 names it "the reference aggregation function".

    `case_rows` are `evaluation_case_metrics` rows -- `CaseMetricRow` objects or mappings
    with the same members, so the caller may pass what it just computed or what it just
    read back from the database, and get the same answer either way. That equivalence is
    the whole content of the recomputation check.

    `case_scores` are `evaluation_case_scores` rows and are required only for the
    count-based family (`sensitivity`, `specificity`, `ppv`), which has no per-case metric
    column -- see the module note in `medos.evidence.evaluation` about MOS-EVID-066's
    "from `evaluation_case_metrics` alone".

    Returns the `aggregates` array, ordered deterministically (stratum, then metric) so
    that the canonical digest over it is stable across runs of the same measurement.
    """
    rows = _rows_as_dicts(case_rows)
    for row in rows:
        if row["metric"] not in REGISTRY:
            raise UnknownMetric(
                f"MOS-EVID-054/MOS-EVID-070: {row['metric']!r} is not a registered "
                "metric id; a run persisting it MUST be marked INVALIDATED"
            )

    groups = list(subgroups) or []
    if not any(g.name == ALL_CASES.name for g in groups):
        groups = [ALL_CASES, *groups]

    b = int(conventions["bootstrap_b"])
    seed = int(conventions["bootstrap_seed"])
    fp_threshold = float(conventions["fp_volume_threshold_ml"])

    # The metric set is taken from the WHOLE run, not from each filtered subgroup.
    # Otherwise a subgroup that matched no case produces no entries at all, and a
    # criterion over a stratum that collapsed finds nothing to evaluate and says nothing
    # -- which is the failure chapter 7 acceptance check 22 requires to be INDETERMINATE.
    reported = sorted({r["metric"] for r in rows})

    out: list[dict[str, Any]] = []
    for group in groups:
        in_group = [r for r in rows if group.matches(r["strata"])]
        out.extend(
            _per_case_family(in_group, group.name, reported, b=b, seed=seed)
        )
        out.extend(
            _pooled_dice(in_group, group.name, reported, b=b, seed=seed)
        )
        out.extend(
            _empty_gt_block(
                in_group, group.name, fp_threshold=fp_threshold, b=b, seed=seed
            )
        )
        if case_scores and operating_point is not None:
            keys = {r["case_key"] for r in in_group} if group.where else None
            scored = [
                s for s in case_scores if keys is None or s["case_key"] in keys
            ]
            out.extend(
                _count_family(
                    scored, group.name, operating_point=operating_point, b=b, seed=seed
                )
            )

    out.sort(key=lambda e: (e["stratum"], e["metric"]))
    validate_aggregates(out)
    return out


def _per_case_family(
    rows: Sequence[Mapping[str, Any]],
    stratum: str,
    reported: Sequence[str],
    *,
    b: int,
    seed: int,
) -> list[dict[str, Any]]:
    """Every registered per-case metric the run reports, aggregated as registered."""
    out: list[dict[str, Any]] = []
    for metric in reported:
        spec = REGISTRY[metric]
        if not spec.per_case:
            continue
        eligible = [
            r for r in rows if r["metric"] == metric and r["eligible"] and
            r["value"] is not None
        ]
        if not eligible:
            # MOS-EVID-056 still applies: the aggregate is REPORTED with n = 0 and no
            # value. Omitting it would let a criterion over a collapsed stratum silently
            # find nothing to evaluate, which chapter 7 acceptance check 22 requires to
            # be INDETERMINATE rather than absent.
            out.append(
                _entry(metric, stratum, None, None, 0, 0, note="no_eligible_cases")
            )
            continue
        values = [float(r["value"]) for r in eligible]
        patients = [str(r["patient_key"]) for r in eligible]
        statistic = _statistic(spec.aggregation)
        point = float(statistic(values))
        interval = _ci.cluster_bootstrap_ci(
            values, patients, statistic, b=b, seed=seed
        )
        out.append(
            _entry(metric, stratum, point, interval, len(values), len(set(patients)))
        )
    return out


def _pooled_dice(
    rows: Sequence[Mapping[str, Any]],
    stratum: str,
    reported: Sequence[str],
    *,
    b: int,
    seed: int,
) -> list[dict[str, Any]]:
    """MOS-EVID-048 and MOS-EVID-060, from the persisted counts and nothing else.

    `2*sum(intersection) / sum(gt + pred)` over the SAME eligible case set as
    `dice_mean_per_case` (MOS-EVID-048: "over the same eligible case set"), which is what
    makes the two figures comparable: they differ only in the weighting, which is the
    entire point of reporting both.
    """
    if "dice_mean_per_case" not in reported:
        return []
    eligible = [
        r for r in rows if r["metric"] == "dice_mean_per_case" and r["eligible"]
    ]
    if not eligible:
        # Reported with n = 0 rather than omitted, for the reason `_per_case_family`
        # gives: a collapsed stratum must be visible to a criterion, not absent from it.
        return [_entry("dice_pooled", stratum, None, None, 0, 0,
                       note="no_eligible_cases")]
    numerators = [2.0 * float(r["intersection_voxels"]) for r in eligible]
    denominators = [
        float(r["gt_voxels"]) + float(r["pred_voxels"]) for r in eligible
    ]
    patients = [str(r["patient_key"]) for r in eligible]
    total_den = sum(denominators)
    if total_den <= 0:  # pragma: no cover - gt_voxels > 0 on every eligible Dice row
        return []
    point = sum(numerators) / total_den
    interval = _ci.cluster_bootstrap_ratio_ci(
        numerators, denominators, patients, b=b, seed=seed
    )
    return [
        _entry(
            "dice_pooled", stratum, point, interval, len(eligible), len(set(patients))
        )
    ]


def _empty_gt_block(
    rows: Sequence[Mapping[str, Any]],
    stratum: str,
    *,
    fp_threshold: float,
    b: int,
    seed: int,
) -> list[dict[str, Any]]:
    """MOS-EVID-052's five metrics, over the cases Dice excluded.

    The block is what keeps the false-positive behaviour VISIBLE instead of laundering it
    into the same average. Reported on the `gt_empty` stratum of section 7.12.1, suffixed
    with the subgroup name when the subgroup is not the whole cohort -- one label cannot
    mean both "this clinical stratum" and "the empty-GT part of it".
    """
    empty = [
        r
        for r in rows
        if r["metric"] == "dice_mean_per_case"
        and r["undefined_reason"] == "empty_ground_truth"
    ]
    if not empty:
        return []
    label = "gt_empty" if stratum == ALL_CASES.name else f"{stratum}:gt_empty"
    volumes = [float(r["pred_volume_ml"]) for r in empty]
    patients = [str(r["patient_key"]) for r in empty]
    n = len(empty)
    n_pat = len(set(patients))
    indicators = [1.0 if v > fp_threshold else 0.0 for v in volumes]

    op = {"name": "fp_volume_threshold_ml", "value": float(fp_threshold)}
    out = [
        # A count of the cohort is a DESCRIPTION, not an estimate of a population
        # parameter, so it is reported with a degenerate interval and a note rather than
        # with a resampled one. Fabricating an interval around "how many negatives were
        # in this cohort" would be a number with no referent.
        _entry(
            "empty_gt_case_count", label, float(n), (float(n), float(n)), n, n_pat,
            note="descriptive_no_interval",
        ),
        _entry(
            "empty_gt_false_positive_rate",
            label,
            float(np.mean(indicators)),
            _ci.cluster_bootstrap_ci(indicators, patients, np.mean, b=b, seed=seed),
            n,
            n_pat,
            operating_point=op,
        ),
        _entry(
            "empty_gt_mean_fp_volume_ml",
            label,
            float(np.mean(volumes)),
            _ci.cluster_bootstrap_ci(volumes, patients, np.mean, b=b, seed=seed),
            n,
            n_pat,
        ),
        _entry(
            "empty_gt_p95_fp_volume_ml",
            label,
            float(np.quantile(volumes, 0.95)),
            _ci.cluster_bootstrap_ci(
                volumes, patients, lambda v: float(np.quantile(v, 0.95)), b=b, seed=seed
            ),
            n,
            n_pat,
        ),
        _entry(
            "empty_gt_max_fp_volume_ml",
            label,
            float(max(volumes)),
            (float(max(volumes)), float(max(volumes))),
            n,
            n_pat,
            note="descriptive_no_interval",
        ),
    ]
    return out


def _count_family(
    scores: Sequence[Mapping[str, Any]],
    stratum: str,
    *,
    operating_point: Mapping[str, Any],
    b: int,
    seed: int,
) -> list[dict[str, Any]]:
    """`sensitivity`, `specificity` and `ppv` at a DECLARED operating point.

    MOS-EVID-055: "A number without an operating point is not a measurement." The point
    is therefore an argument, never a default, and it is copied onto every entry it
    produced. MOS-EVID-059: the interval is the same cluster bootstrap over the indicator
    variables -- Wilson and Clopper-Pearson assume independence this cohort does not have
    the moment one patient contributes two studies.
    """
    threshold = float(operating_point["value"])
    usable = [
        s
        for s in scores
        if s.get("case_score") is not None and s.get("case_label") is not None
    ]

    def _block(
        metric: str, selected: Sequence[Mapping[str, Any]], hit: Sequence[float]
    ) -> dict[str, Any]:
        if not selected:
            return _entry(
                metric, stratum, None, None, 0, 0,
                operating_point=dict(operating_point), note="no_eligible_cases",
            )
        patients = [str(s["patient_key"]) for s in selected]
        return _entry(
            metric,
            stratum,
            float(np.mean(hit)),
            _ci.cluster_bootstrap_ci(hit, patients, np.mean, b=b, seed=seed),
            len(selected),
            len(set(patients)),
            operating_point=dict(operating_point),
        )

    positives = [s for s in usable if int(s["case_label"]) == 1]
    negatives = [s for s in usable if int(s["case_label"]) == 0]
    predicted = [s for s in usable if float(s["case_score"]) >= threshold]
    return [
        _block(
            "sensitivity",
            positives,
            [1.0 if float(s["case_score"]) >= threshold else 0.0 for s in positives],
        ),
        _block(
            "specificity",
            negatives,
            [1.0 if float(s["case_score"]) < threshold else 0.0 for s in negatives],
        ),
        _block(
            "ppv",
            predicted,
            [1.0 if int(s["case_label"]) == 1 else 0.0 for s in predicted],
        ),
    ]


# =====================================================================================
# Validation and comparison
# =====================================================================================
def validate_aggregates(entries: Sequence[Mapping[str, Any]]) -> None:
    """MOS-EVID-055, MOS-EVID-056, MOS-EVID-049 and MOS-EVID-054, over a block.

    "A bare scalar metric MUST fail schema validation. This is the rule that makes the
    previous version's `dice: 0.91` -- no cohort, no n, no CI, no run -- structurally
    impossible."
    """
    for entry in entries:
        metric = entry.get("metric")
        if not metric or metric not in REGISTRY:
            raise UnknownMetric(
                f"MOS-EVID-054: {metric!r} is not a registered metric id"
            )
        if metric == "dice":  # pragma: no cover - unreachable via REGISTRY
            raise AggregationError("MOS-EVID-049: `dice` is never a reported figure")
        for required in ("stratum", "value", "ci_low", "ci_high", "n", "n_patients"):
            if required not in entry:
                raise AggregationError(
                    f"MOS-EVID-056: aggregate {metric!r} is missing {required!r}; every "
                    "aggregate carries n, n_patients, a confidence interval and the "
                    "convention block"
                )
        if entry["value"] is not None and not math.isfinite(float(entry["value"])):
            raise AggregationError(
                f"MOS-EVID-008: {metric!r} is not finite; non-finite numbers are "
                "rejected at write time"
            )
        if metric in THRESHOLD_METRICS and "operating_point" not in entry:
            raise AggregationError(
                f"MOS-EVID-055: {metric!r} MUST NOT be persisted without the operating "
                "point that produced it; a number without one is not a measurement"
            )
        if entry["value"] is None and entry["n"] != 0:
            raise AggregationError(
                f"MOS-EVID-056: {metric!r} has no value but claims n = {entry['n']}"
            )


def aggregates_agree(
    left: Sequence[Mapping[str, Any]],
    right: Sequence[Mapping[str, Any]],
    *,
    rel_tol: float = 1e-9,
) -> tuple[bool, list[str]]:
    """MOS-EVID-066's comparison: same keys, same values within 1e-9 RELATIVE.

    Returns `(agreed, differences)`. The differences are returned rather than raised
    because the caller decides what a disagreement means: at CI time it is a failure, and
    at MOS-EVID-070 time it is grounds for marking the run INVALIDATED.
    """
    def _key(entry: Mapping[str, Any]) -> tuple[str, str]:
        return str(entry["stratum"]), str(entry["metric"])

    left_map = {_key(e): e for e in left}
    right_map = {_key(e): e for e in right}
    differences: list[str] = []
    for key in sorted(set(left_map) | set(right_map)):
        if key not in left_map or key not in right_map:
            differences.append(f"{key[1]}@{key[0]}: present in only one block")
            continue
        a, bb = left_map[key], right_map[key]
        for member in ("value", "ci_low", "ci_high", "n", "n_patients"):
            x, y = a.get(member), bb.get(member)
            if x is None or y is None:
                if x is not y:
                    differences.append(f"{key[1]}@{key[0]}.{member}: {x!r} vs {y!r}")
                continue
            x, y = float(x), float(y)
            scale = max(abs(x), abs(y), 1.0)
            if abs(x - y) / scale > rel_tol:
                differences.append(
                    f"{key[1]}@{key[0]}.{member}: {x!r} vs {y!r} "
                    f"(rel {abs(x - y) / scale:.3e})"
                )
    return (not differences), differences
