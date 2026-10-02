# SPDX-License-Identifier: Apache-2.0
"""`non-inferiority` -- the 0.2.0 gate check of docs/spec/15-delivery.md section 15.1.2.

    `non-inferiority` (the gate passes a within-noise change on a 40-case split and fails
    a change that loses a named stratum)

BOTH ARMS, OR THE CHECK PROVES NOTHING
---------------------------------------
That sentence is the whole design of this module, and it is why §15.1.2 wrote the check
with an "and" in it. A gate with only the FAIL arm is satisfied by `return "FAIL"`, which
is why §15.2.5 says the naive comparison "gets switched off within two weeks" -- it blocks
a correct rebuild roughly half the time on a split this size, so the second week is spent
arguing with it and the third is spent removing it. A gate with only the PASS arm is
satisfied by `return "PASS"`, which passes "a model that lost every sub-6 mm nodule".

So the two arms below run on the SAME 40-patient frozen split, against the SAME criteria
document, through the SAME `evaluate_gate` call, and differ only in what the candidate's
per-case rows say:

    arm 1   candidate = incumbent + N(0, 0.01) per case        -> PASS
    arm 2   candidate = incumbent, minus 0.25 on small effusions -> FAIL, at `ni_dice_small`

and arm 2 additionally asserts that the COHORT-WIDE point estimate barely moved, so the
aggregate alone would have waved it through. That is the defect being gated on, stated as
a number rather than as a warning in a comment.

WHY THE FALSE-BLOCK RATE IS MEASURED AND NOT ASSERTED
------------------------------------------------------
§15.2.5's claim about `if new.dice < old.dice` -- "fails on noise roughly half the time on
a 40-case split" -- is checkable, so `test_non_inferiority_tolerates_noise_that_the_naive_
comparison_would_block` checks it: many trials of a genuinely unchanged model, counted both
ways. If that number ever stops being about half, the sentence in the specification is wrong
and someone should know.

Needs Postgres: a throwaway database created by `tests/gate/conftest.py::evidence_dsn`.

Spec: MOS-EVID-065, MOS-EVID-067, MOS-EVID-085, MOS-EVID-086, MOS-EVID-088, MOS-EVID-113,
MOS-EVID-114, chapter 7 acceptance checks 18-22, MOS-REL-004, MOS-REL-012.
"""

from __future__ import annotations

import random
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from tests.gate._evidence import (
    DOC,
    NOW,
    Cohort,
    case_rows_for,
    ev_gate,
    forty_case_split,
    mean_value,
    paired_views,
    perturb,
    stratum_rows,
)

pytestmark = pytest.mark.gate_0_2_0

REPO_ROOT = Path(__file__).resolve().parents[2]

#: §15.1.2's number, and §15.2.5's: "a 40-case split". One case per patient, so the
#: clustered bootstrap of MOS-EVID-057 has 40 clusters and the naive comparison has its
#: worst-case sample size.
N_PATIENTS = 40

#: The stratum arm 2 destroys, and the criterion that must notice.
COLLAPSED_STRATUM = "small_effusion"
STRATUM_CRITERION = "ni_dice_small"

#: The loss inflicted on that stratum: five times `ni_dice_small`'s declared margin of
#: 0.05, so a gate that misses it is not close to working rather than a borderline case
#: whose verdict is a judgement call.
#:
#: It is also chosen to keep the COHORT-WIDE movement small. Eight of thirty positives
#: losing 0.25 moves the `gt_positive` mean by 8/30 x 0.25 = 0.067, which is what makes
#: the second half of arm 2 -- "the aggregate would not have noticed" -- a real assertion
#: rather than an arithmetic coincidence. A larger loss would fail the gate more loudly
#: and prove less.
STRATUM_LOSS = 0.25

#: The per-case noise of arm 1. A rebuild of the same model on the same data: a different
#: cuDNN kernel, a different GPU, a reordered reduction. MOS-EVID-088's margin exists for
#: exactly this and nothing else.
REBUILD_SIGMA = 0.01


@pytest.fixture()
def split(evidence_db: psycopg.Connection[Any], manifest_store: Any) -> Cohort:
    """A sealed 40-patient cohort with a frozen, leakage-checked split over it.

    The gate arms below read per-case rows keyed to THESE patients, so "a 40-case split"
    means a split object that was actually frozen and not a list of forty numbers.
    """
    cohort = Cohort(evidence_db, manifest_store, n_patients=N_PATIENTS)
    evidence_db.commit()
    assert cohort.split.leakage_report["L1"] == "pass"
    assert sum(cohort.split.partition_patients.values()) == N_PATIENTS
    return cohort


# ======================================================================================
# 1. ARM ONE. A within-noise rebuild is PASSED.
# ======================================================================================
def test_non_inferiority_passes_a_within_noise_change_on_a_40_case_split(
    split: Cohort,
) -> None:
    """The arm that stops the gate being `return "FAIL"`.

    The candidate is the incumbent with independent per-case noise. Nothing has changed
    about the model's behaviour; the numbers differ because floating-point reductions on a
    different accelerator are not associative. `MOS-EVID-085`'s paired test is what
    distinguishes this from a real loss, and the seed is fixed so that a gate run in CI and
    a gate run on a laptop reach the same verdict.
    """
    rng = random.Random(4242)
    incumbent_rows = case_rows_for(split.records)
    candidate_rows = perturb(
        incumbent_rows, lambda r: r.value + rng.gauss(0.0, REBUILD_SIGMA)
    )

    incumbent, candidate, binding = paired_views(
        incumbent_rows, candidate_rows, cohort=split
    )
    result = ev_gate.evaluate_gate(candidate, incumbent, DOC, binding, now=NOW)

    assert result.verdict == "PASS", (
        "a rebuild within the noise floor was blocked. Per criterion: "
        + "; ".join(f"{r.id}={r.status}" for r in result.results)
    )
    assert not result.blocked

    # Not vacuously: the regression criteria were EVALUATED, not skipped for want of an
    # incumbent (MOS-EVID-113 requires one entry per criterion either way).
    for criterion_id in ("ni_dice_positive", STRATUM_CRITERION, "no_catastrophic"):
        entry = result.by_id(criterion_id)
        assert entry.status == "PASS", f"{criterion_id} -> {entry.status} ({entry.reason})"


# ======================================================================================
# 2. ARM TWO. A named stratum collapses and the gate FAILS -- while the mean barely moves.
# ======================================================================================
def test_non_inferiority_fails_a_change_that_loses_a_named_stratum(
    split: Cohort,
) -> None:
    """The arm that stops the gate being `return "PASS"`, and chapter 7 acceptance check 20.

    Both halves are asserted, and the second is the one that matters:

      1. the stratum criterion FAILS and the verdict is FAIL;
      2. the COHORT-WIDE point estimate moved by less than 0.08, so a gate reading only
         `dice_mean_per_case` over `gt_positive` -- or worse, a single aggregate Dice --
         would have promoted this candidate.

    §15.2.5's example is "a model that lost every sub-6 mm nodule". Here it is the small
    effusions: eight of thirty positives, near the detection floor, where the clinical
    consequence of losing them is largest and the arithmetic consequence smallest.
    """
    incumbent_rows = case_rows_for(split.records)
    candidate_rows = perturb(
        incumbent_rows,
        lambda r: r.value - STRATUM_LOSS
        if r.strata["reference_volume_ml"] < 100.0
        else r.value,
    )

    incumbent, candidate, binding = paired_views(
        incumbent_rows, candidate_rows, cohort=split
    )
    result = ev_gate.evaluate_gate(candidate, incumbent, DOC, binding, now=NOW)

    assert result.verdict == "FAIL"
    assert result.blocked
    assert result.by_id(STRATUM_CRITERION).status == "FAIL", (
        f"{STRATUM_CRITERION} did not fire on a {STRATUM_LOSS} loss across "
        f"{COLLAPSED_STRATUM}; per criterion: "
        + "; ".join(f"{r.id}={r.status}" for r in result.results)
    )

    # THE SECOND HALF. The aggregate alone would not have noticed.
    before = mean_value(stratum_rows(incumbent_rows, "gt_positive"))
    after = mean_value(stratum_rows(candidate_rows, "gt_positive"))
    assert before - after < 0.08, (
        f"the cohort-wide point estimate moved by {before - after:.3f}; this fixture no "
        f"longer demonstrates a collapse that a whole-cohort comparison would miss, so "
        f"the FAIL above no longer proves the gate looks at strata"
    )

    # And the collapse really is confined to the named stratum, so the FAIL is attributable.
    small_before = mean_value(stratum_rows(incumbent_rows, COLLAPSED_STRATUM))
    small_after = mean_value(stratum_rows(candidate_rows, COLLAPSED_STRATUM))
    assert small_before - small_after > 0.20

    # MOS-EVID-113: one entry per criterion, so "why was this refused" is answerable from
    # the decision record alone rather than from whoever ran it.
    assert {r.id for r in result.results} == {
        "dice_positive", "fp_on_negatives",
        "ni_dice_positive", "ni_dice_small", "no_catastrophic",
    }


# ======================================================================================
# 3. The naive comparison, measured -- and absent from the implementation.
# ======================================================================================
def _rebuild_noise(seed: int) -> Any:
    """A per-case perturbation function with its own generator bound at call time.

    Defined here rather than as a lambda in the loop below so that the generator is bound
    per trial and not captured from the enclosing scope: a closure over a loop variable is
    the classic way to make every trial silently share one stream, which would turn a
    measurement of a rate into a measurement of one draw.
    """
    rng = random.Random(seed)
    return lambda row: row.value + rng.gauss(0.0, REBUILD_SIGMA)


def _false_block_rate(trials: int, *, seed: int, bootstrap_b: int) -> tuple[float, float]:
    """`(naive false-block rate, non-inferiority false-block rate)` on 40 unchanged cases.

    "Unchanged" means the model genuinely did not change: the candidate is the incumbent
    plus independent, mean-zero, per-case noise, so EVERY block counted here is a false
    block. Two counters over the same trials, because the comparison between them is the
    whole point of `MOS-EVID-085`.

    `forty_case_split` rather than a sealed cohort: this is a simulation of a decision rule
    and not a gate arm, so it needs forty numbers and no database. The two arms above use
    the real frozen split.

    `non_inferiority` is called directly rather than through `evaluate_regression` so that
    `b` can be lowered. `MOS-EVID-058`'s default is 2000, and 40 trials at 2000 replicates
    is a minute of pure Python to measure a rate to one significant figure.
    """
    naive = 0
    paired = 0
    for trial in range(trials):
        base = [r for r in forty_case_split(seed=seed + trial) if r.eligible]
        rebuilt = perturb(base, _rebuild_noise(seed + trial))

        # `if new.dice < old.dice`, the rule §15.2.5 forbids, spelled out in one line.
        if mean_value(rebuilt) < mean_value(base):
            naive += 1

        outcome = ev_gate.non_inferiority(
            [r.value for r in rebuilt],
            [r.value for r in base],
            [r.patient_key for r in base],
            margin=0.02,
            b=bootstrap_b,
            seed=seed + trial,
        )
        if not outcome["passed"]:
            paired += 1
    return naive / trials, paired / trials


def test_non_inferiority_tolerates_noise_that_the_naive_comparison_would_block() -> None:
    """§15.2.5's claim about `if new.dice < old.dice`, measured rather than asserted.

    "not `if new.dice < old.dice`, which fails on noise roughly half the time on a 40-case
    split, gets switched off within two weeks, and meanwhile passes a model that lost every
    sub-6 mm nodule."

    Forty trials of a model that did not change. The naive comparison blocks about half of
    them -- it is a coin flip on the sign of a mean-zero perturbation, and it could hardly
    be anything else. The paired non-inferiority test blocks few or none, because a
    confidence interval on the per-case DIFFERENCE knows the difference is centred on zero.

    The bounds are deliberately loose (naive in [0.3, 0.7]; paired at most 0.15). A tight
    bound on a stochastic quantity is a flaky gate, and a flaky release gate is a gate that
    gets bypassed -- which is the same failure mode the requirement is about.
    """
    naive, paired = _false_block_rate(40, seed=90210, bootstrap_b=200)

    assert 0.30 <= naive <= 0.70, (
        f"the naive comparison blocked {naive:.0%} of unchanged rebuilds; §15.2.5 says "
        f"'roughly half the time', and a figure far from that means this fixture no "
        f"longer represents the situation the requirement describes"
    )
    assert paired <= 0.15, (
        f"the paired non-inferiority test blocked {paired:.0%} of unchanged rebuilds. "
        f"That is the behaviour that gets a gate switched off."
    )
    assert paired < naive


#: A direct comparison of two AGGREGATE metric values, in any of the spellings someone
#: would reach for. Chapter 7 acceptance check 18: "Expect zero hits on any blocking path."
_NAIVE_PATTERNS = (
    r"\bnew\w*\s*(?:<|>|<=|>=)\s*old\b",
    r"\bold\w*\s*(?:<|>|<=|>=)\s*new\b",
    r"\bcandidate\w*\.value\s*(?:<|>|<=|>=)",
    r"\bincumbent\w*\.value\s*(?:<|>|<=|>=)",
    r"mean\([^)]*new[^)]*\)\s*(?:<|>|<=|>=)\s*mean\(",
    r"\.value\s*(?:<|>|<=|>=)\s*\w*old\w*\.value",
)


def test_non_inferiority_is_not_a_direct_comparison_of_two_aggregates() -> None:
    """Chapter 7 acceptance check 18, over the source of the blocking path.

    Written as patterns over the implementation and not as a behavioural assertion, because
    the defect this catches is a FUTURE EDIT: somebody adding a "quick sanity check" beside
    the non-inferiority test. A behavioural test would still pass with the quick check in
    place for every fixture that happens to agree with it -- and the fixtures in this module
    agree with it on arm 2, which is precisely when it would go unnoticed.
    """
    for name in ("gate.py", "criteria.py"):
        source = (REPO_ROOT / "medos" / "medos" / "evidence" / name).read_text(encoding="utf-8")
        # Docstrings and comments name the forbidden form in order to forbid it, so the
        # scan is over code lines only.
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        for pattern in _NAIVE_PATTERNS:
            hits = re.findall(pattern, code)
            assert not hits, (
                f"medos/medos/evidence/{name} compares two aggregates directly ({pattern!r} -> "
                f"{hits}). §15.2.5 forbids exactly that on a blocking path."
            )
