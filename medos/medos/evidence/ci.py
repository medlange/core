# SPDX-License-Identifier: Apache-2.0
"""Patient-level cluster bootstrap. Chapter 7 section 7.6.4.

Requirements implemented here
-----------------------------
MOS-EVID-056  every aggregate is persisted with `n`, `n_patients`, a confidence interval
              and the convention block. A bare scalar MUST fail validation.
MOS-EVID-057  confidence intervals MUST be computed by patient-level cluster bootstrap:
              resample `patient_key` with replacement, take ALL cases belonging to each
              drawn patient, recompute the statistic. Resampling cases independently MUST
              NOT be used.
MOS-EVID-058  percentile method, B = 2000, the seed recorded on the run, and the interval
              reproducible from the persisted per-case rows alone.
MOS-EVID-059  count-based metrics take the same cluster bootstrap over the indicator
              variables; Wilson and Clopper-Pearson MUST NOT be used when a patient
              contributes more than one case.
MOS-EVID-060  `dice_pooled` has no per-case decomposition, so its bootstrap resamples
              patients and recomputes the pooled numerator and denominator from the
              persisted per-case counts.

WHY THE CLUSTER, AND WHAT IT COSTS
----------------------------------
Four studies of one patient are not four observations. A patient with an unusual anatomy
contributes four correlated errors, and an interval that treats them as independent is
too narrow -- it reports more certainty than the cohort contains, on exactly the cases
where the model is most likely to be wrong in a correlated way. Chapter 7 acceptance
check 11 requires this to be demonstrated rather than asserted: resampling cases instead
of patients on a fixture where one patient contributes four studies MUST produce a
different interval. `tests/integration/test_evaluation.py` runs that comparison.

The cost is real and is stated rather than hidden: the interval is wider, so a marginal
model that a case-level bootstrap would have passed does not pass. MOS-EVID-089 requires
that more evidence makes a criterion EASIER to satisfy, and the cluster bootstrap has
that property -- the interval narrows as patients are added, which is the direction that
rewards collecting more data rather than slicing the same data more finely.

REPRODUCIBILITY IS PART OF THE CONTRACT
---------------------------------------
Chapter 7 acceptance check 11 asks for BIT-IDENTICAL bounds on recomputation. That
constrains the implementation and not only the mathematics: the draw order, the number of
draws, and the order in which a drawn patient's cases enter the sample all affect the
result through floating-point summation. So `cluster_bootstrap_ci` below reproduces the
call sequence of the chapter's reference snippet exactly -- `rng.integers(0, n, n)` once
per replicate over a patient list in FIRST-APPEARANCE order -- and `_grouped` is shared
by both entry points so the two cannot drift.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np

__all__ = [
    "cluster_bootstrap_ci",
    "cluster_bootstrap_ratio_ci",
    "case_bootstrap_ci",
]


def _grouped(
    values: Sequence[float], patient_keys: Sequence[str]
) -> tuple[list[str], dict[str, list[float]]]:
    """Group values by patient in FIRST-APPEARANCE order.

    First-appearance and not sorted: the chapter's snippet builds the dict by iteration
    and lists it, and `dict` preserves insertion order in Python 3.7+. Sorting here would
    be defensible and would change every published interval by a few ULP, which is
    exactly the kind of invisible divergence acceptance check 11 exists to catch. The
    caller controls the row order (`medos.evidence.evaluation.load_case_metrics` orders by
    `case_key`), so the sequence is deterministic without this function imposing one.
    """
    groups: dict[str, list[float]] = {}
    for v, pk in zip(values, patient_keys):
        groups.setdefault(pk, []).append(float(v))
    return list(groups), groups


def cluster_bootstrap_ci(
    values: Sequence[float],
    patient_keys: Sequence[str],
    statistic: Callable[[Any], float] = np.mean,
    b: int = 2000,
    seed: int = 20260101,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """MOS-EVID-057 / MOS-EVID-058, the chapter's reference implementation.

    Positional signature and default arguments follow section 7.6.4's snippet so that a
    reader holding the specification can call this function without translating it.

    `alpha = 0.05` gives the two-sided 95 % percentile interval. The gate's one-sided
    95 % lower bound (MOS-EVID-085) is the 5th percentile, which is this function at
    `alpha = 0.10` -- section 7.9.2's snippet does exactly that and the comment there says
    so. It is not restated as a second function, because a second function is where the
    two would diverge.

    Degenerate inputs are not special-cased into a fabricated interval: zero values raises
    (an interval over nothing is not an interval), and a single patient returns an
    interval of zero width, which is the honest answer -- every resample of one cluster is
    that cluster.
    """
    if len(values) != len(patient_keys):
        raise ValueError("values and patient_keys MUST be the same length")
    if not values:
        raise ValueError(
            "MOS-EVID-056: an aggregate over zero eligible cases has no interval; report "
            "n = 0 and no value rather than inventing one"
        )
    keys, groups = _grouped(values, patient_keys)
    rng = np.random.default_rng(seed)
    n = len(keys)
    boot = np.empty(b, dtype=float)
    for i in range(b):
        drawn = rng.integers(0, n, n)
        sample = [v for j in drawn for v in groups[keys[j]]]
        boot[i] = statistic(sample)
    lo, hi = np.quantile(boot, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)


def cluster_bootstrap_ratio_ci(
    numerators: Sequence[float],
    denominators: Sequence[float],
    patient_keys: Sequence[str],
    b: int = 2000,
    seed: int = 20260101,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """MOS-EVID-060: the pooled-ratio bootstrap, for `dice_pooled`.

    A pooled figure is `sum(numerator) / sum(denominator)` over the cohort, so it has no
    per-case value to resample. What CAN be resampled is the pair of counts each case
    contributes -- which is why MOS-EVID-068 requires `gt_voxels`, `pred_voxels` and
    `intersection_voxels` to be persisted on every case even when the per-case metric is
    NULL. Without them this interval is not computable from the database and `dice_pooled`
    would be a number with no uncertainty, which MOS-EVID-056 forbids.

    The draw sequence matches `cluster_bootstrap_ci` exactly: same rng, same
    `rng.integers(0, n, n)` per replicate, same patient order.
    """
    if not (len(numerators) == len(denominators) == len(patient_keys)):
        raise ValueError("numerators, denominators and patient_keys MUST align")
    if not numerators:
        raise ValueError("MOS-EVID-056: a pooled figure over zero cases has no interval")
    pairs: dict[str, list[tuple[float, float]]] = {}
    for num, den, pk in zip(numerators, denominators, patient_keys):
        pairs.setdefault(pk, []).append((float(num), float(den)))
    keys = list(pairs)
    rng = np.random.default_rng(seed)
    n = len(keys)
    boot = np.empty(b, dtype=float)
    for i in range(b):
        drawn = rng.integers(0, n, n)
        num_sum = 0.0
        den_sum = 0.0
        for j in drawn:
            for num, den in pairs[keys[j]]:
                num_sum += num
                den_sum += den
        boot[i] = num_sum / den_sum if den_sum > 0 else float("nan")
    lo, hi = np.nanquantile(boot, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)


def case_bootstrap_ci(
    values: Sequence[float],
    statistic: Callable[[Any], float] = np.mean,
    b: int = 2000,
    seed: int = 20260101,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """The WRONG bootstrap -- cases resampled independently. NOT FOR PUBLICATION.

    MOS-EVID-057 forbids this for any reported interval and MOS-EVID-059 repeats the
    prohibition for count-based metrics. It exists for one purpose: chapter 7 acceptance
    check 11 requires a demonstration that clustering is ACTUALLY APPLIED -- "assert that
    resampling cases instead of patients produces a *different* interval on a fixture
    where one patient contributes four studies". A claim that cannot fail is not a check,
    and the only way to make that one able to fail is to have the wrong answer available
    to compare against.

    No caller in `medos/medos/` outside that test may use it; the integration test greps for
    call sites and fails on a second one.
    """
    if not values:
        raise ValueError("an interval over zero cases is not an interval")
    rng = np.random.default_rng(seed)
    arr = np.asarray(values, dtype=float)
    n = arr.size
    boot = np.empty(b, dtype=float)
    for i in range(b):
        boot[i] = statistic(arr[rng.integers(0, n, n)])
    lo, hi = np.quantile(boot, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)
