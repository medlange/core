# SPDX-License-Identifier: Apache-2.0
"""The deployment gate: AcceptanceCriteria, paired non-inferiority, and liveness.

docs/spec/15-delivery.md section 15.2.5 (tag 0.2.0) and section 15.1.2's 0.2.0 gate row:

    `deployment-gate`   a deliberately regressed `ModelVersion` is refused promotion
                        against a frozen split and the incumbent stays live
    `non-inferiority`   the gate passes a within-noise change on a 40-case split and
                        fails a change that loses a named stratum

Section 15.1.3 puts "the deployment gate" in 0.2.0's **Tier A**: MUST NOT be cut.

WHAT THIS FILE IS TRYING TO CATCH, in the order the release brief names them
-----------------------------------------------------------------------------
 1. `if new.dice < old.dice` reappearing. It "fails on noise roughly half the time on a
    40-case split, gets switched off within two weeks, and meanwhile passes a model that
    lost every sub-6 mm nodule". `test_the_naive_gate_is_absent` is a real grep over the
    gate implementation (chapter 7 acceptance check 18), and
    `test_non_inferiority_tolerates_noise` measures the 50 % false-block rate directly
    rather than asserting it in a comment.
 2. A gate that cannot see a subgroup collapse. `test_regression_catches_a_stratum_collapse`
    asserts BOTH halves of acceptance check 20: the stratum criterion FAILS and the
    cohort-wide point estimate moved by less than 0.08, so the aggregate alone would have
    let it through.
 3. `INDETERMINATE` folded into `FAIL`. Acceptance check 22: one needs more data, the
    other needs a different model, and a site sent to the wrong remedy wastes a quarter.
 4. A refused candidate taking the capability out of service. MOS-EVID-092 is a statement
    about what does NOT happen, so the `..._refused_and_incumbent_stays_live` test
    reads the incumbent's row before and after and asserts it is byte-identical.
 5. A refusal nobody can find afterwards. MOS-EVID-091 requires the decision persisted
    whatever the outcome, which means it must survive the exception.

SKIP DISCIPLINE: `tests/_support/skips.py` only. No raw `pytest.skip` here. The database
tests take `pg_dsn`, which `tests/integration/conftest.py` classifies through
`skip_infra`, so `--require-stack` probes them; the pure-function tests need no stack and
are not skippable at all.

TEST ISOLATION: this module TRUNCATES only the four tables migration 0008 declares. The
cohort tables of 0006 and the run tables of 0007 are shared with other suites running in
parallel, so every fixture here creates its own rows under a random slug and truncates
nothing it does not own.
"""

from __future__ import annotations

import random
import re
import secrets
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    bind_current_tenant,
    reset_current_tenant,
)
from medos.evidence import acceptance as ev_acc
from medos.evidence import criteria as ev_crit
from medos.evidence import deployment as ev_dep
from medos.evidence import digest as ev_digest
from medos.evidence import evaluation as ev_run
from medos.evidence import gate as ev_gate
from medos.evidence import repo as ev_repo
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.metrics import CaseMetricRow
from medos.evidence.store import InMemoryManifestStore
from psycopg.rows import dict_row

#: The repository. These checks read source by path, and a path relative to the working
#: directory makes the same check pass or fail depending on where pytest was invoked.
_REPO = Path(__file__).resolve().parents[2]

BUCKET = "medos-evidence"
SALT = b"test-tenant-salt-not-a-real-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"
CAPABILITY = "pleural_effusion"
LABEL_DEFINITION_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "medicalos:capability_concepts/effusion/v1")
)
NOW = datetime(2026, 6, 1, tzinfo=UTC)

GATE_TABLES = (
    "deployment_gate_decisions",
    "deployments",
    "tenant_acceptance_bindings",
    "acceptance_criteria",
)

# MOS-EVID-067's minimum plus the capability's declared clinical stratum. Chapter 7
# section 7.8.2 gates `pleural_effusion` on `reference_volume_ml`, so the criteria
# validator has to be told it is persisted or every worked-example criterion is refused.
KNOWN_STRATA = (*ev_crit.REQUIRED_STRATUM_FIELDS, "reference_volume_ml")

CANDIDATE = ev_gate.SubjectRef("model_version", "pulmoai.effusion", "2.2.0")
INCUMBENT = ev_gate.SubjectRef("model_version", "pulmoai.effusion", "2.1.0")


# =====================================================================================
# The criteria document under test.
#
# Section 7.8.2's worked example, reduced to the criteria this cohort can actually
# support and with every bound stated. It is NOT a toy: every field the grammar gates on
# is present, the margins are the chapter's (0.02 / 0.05 / 0.20), and `no_catastrophic`
# is the acceptance-check-21 criterion that the mean cannot express.
# =====================================================================================
def criteria_document(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "medicalos.io/v1",
        "kind": "AcceptanceCriteria",
        "metadata": {
            "capability_id": CAPABILITY,
            "version": 3,
            "effective_from": "2026-04-01T00:00:00Z",
            "authored_by": "clinical-council@medicalos.example",
            "supersedes": 2,
        },
        "spec": {
            "combine": "all_of",
            "fp_volume_threshold_ml": 10.0,
            "margin_rationale": (
                "0.02 mean-of-per-case overlap is the smallest loss the clinical council "
                "judged tolerable on a positive effusion; 0.05 on small effusions because "
                "they already sit near the detection floor; 0.20 as the per-case collapse "
                "threshold because a case falling that far has stopped segmenting rather "
                "than segmenting worse."
            ),
            "case_definition": {
                "positive_if": {"reference_volume_ml": {"op": ">=", "value": 10.0}}
            },
            "strata": [
                {"id": "all", "selector": {}},
                {
                    "id": "gt_positive",
                    "selector": {"reference_volume_ml": {"op": ">=", "value": 10.0}},
                },
                {
                    "id": "small_effusion",
                    "selector": {"reference_volume_ml": {"op": "<", "value": 100.0}},
                },
            ],
            "absolute": [
                {
                    "id": "dice_positive",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "bound": "ci_lower_95",
                    "op": ">=",
                    "value": 0.60,
                    "min_cases": 20,
                    "min_patients": 20,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    "id": "fp_on_negatives",
                    "metric": "empty_gt_false_positive_rate",
                    "stratum": "all",
                    "bound": "ci_upper_95",
                    "op": "<=",
                    "value": 0.50,
                    "min_cases": 5,
                    "min_patients": 5,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
            ],
            "regression": [
                {
                    "id": "ni_dice_positive",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "margin": 0.02,
                    "bound": "ci_lower_95",
                    "min_cases": 20,
                    "min_patients": 20,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    "id": "ni_dice_small",
                    "metric": "dice_mean_per_case",
                    "stratum": "small_effusion",
                    "margin": 0.05,
                    "bound": "ci_lower_95",
                    "min_cases": 6,
                    "min_patients": 6,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    "id": "no_catastrophic",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "margin": 0.20,
                    "bound": "catastrophic_count",
                    "op": "<=",
                    "value": 2,
                    "min_cases": 20,
                    "min_patients": 20,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
            ],
        },
    }
    doc["spec"].update(over.pop("spec", {}))
    doc["metadata"].update(over.pop("metadata", {}))
    return doc


DOC = criteria_document()


# =====================================================================================
# Synthetic per-case rows. No database, no images: acceptance checks 19-22 are stated
# over generated fixtures and MOS-EVID-065's whole point is that the gate needs only the
# per-case rows.
# =====================================================================================
def case_row(
    index: int,
    value: float | None,
    reference_volume_ml: float,
    *,
    eligible: bool = True,
    pred_volume_ml: float | None = None,
    patient: int | None = None,
) -> CaseMetricRow:
    patient = index if patient is None else patient
    gt_voxels = 0 if reference_volume_ml <= 0.0 else 1000
    return CaseMetricRow(
        case_key=f"case_{index:04d}",
        patient_key=f"pt_{patient:04d}",
        study_instance_uid=f"1.2.826.0.1.3680043.10.9999.{index}",
        series_instance_uid=f"1.2.826.0.1.3680043.10.9999.{index}.1",
        metric="dice_mean_per_case",
        value=value,
        undefined_reason=None if eligible else "empty_ground_truth",
        eligible=eligible,
        gt_voxels=gt_voxels,
        pred_voxels=1000 if value else 0,
        intersection_voxels=int(1000 * (value or 0.0)),
        gt_volume_ml=reference_volume_ml,
        pred_volume_ml=(
            reference_volume_ml if pred_volume_ml is None else pred_volume_ml
        ),
        strata={
            "reference_volume_ml": reference_volume_ml,
            "slice_thickness_mm": 1.25,
            "pixel_spacing_mm_max": 0.703125,
            "convolution_kernel_class": "soft",
            "manufacturer": "SIEMENS",
            "contrast_phase": "non_contrast",
            "patient_age_years": 60 + (index % 20),
            "patient_sex": "M" if index % 2 else "F",
        },
    )


def cohort(
    n_positive: int = 30, n_small: int = 8, n_empty: int = 10, *, seed: int = 20260101
) -> list[CaseMetricRow]:
    """A 40-patient cohort: `n_small` of the positives are small effusions.

    40 is the release brief's number -- "fails on noise roughly half the time on a
    40-case split" -- and the size at which the naive comparison's defect is loudest.
    """
    rng = random.Random(seed)
    rows: list[CaseMetricRow] = []
    for i in range(n_positive):
        small = i < n_small
        rows.append(
            case_row(i, 0.80 + rng.uniform(-0.03, 0.03), 40.0 if small else 150.0)
        )
    for j in range(n_empty):
        i = n_positive + j
        rows.append(
            case_row(i, None, 0.0, eligible=False, pred_volume_ml=1.0)
        )
    return rows


def perturb(
    rows: Sequence[CaseMetricRow], fn: Any
) -> tuple[CaseMetricRow, ...]:
    out = []
    for r in rows:
        if r.value is None:
            out.append(r)
            continue
        new_value = fn(r)
        out.append(
            replace(
                r,
                value=new_value,
                intersection_voxels=int(1000 * max(0.0, min(1.0, new_value))),
            )
        )
    return tuple(out)


def run_view(rows: Sequence[CaseMetricRow], **over: Any) -> ev_gate.RunView:
    defaults: dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "dataset_version_id": "dsv-fixture",
        "dataset_version_digest": "sha256:" + "a" * 64,
        "split_id": "spl-fixture",
        "split_digest": "sha256:" + "b" * 64,
        "partition": "test",
        "annotation_set_id": "ann-fixture",
        "annotation_digest": "sha256:" + "c" * 64,
        "case_metrics": tuple(rows),
        "operating_thresholds": {
            "effusion_probability": {"value": 0.50, "selected_on": "tune"}
        },
        "state": "SUCCEEDED",
    }
    defaults.update(over)
    return ev_gate.RunView(**defaults)


def report_view(
    run: ev_gate.RunView, subject: ev_gate.SubjectRef, **over: Any
) -> ev_gate.ReportView:
    defaults: dict[str, Any] = {
        "report_id": str(uuid.uuid4()),
        "subject": subject,
        "capability_id": CAPABILITY,
        "criteria_version": 3,
        "run": run,
        "signature_verified": True,
        "valid_until": NOW + timedelta(days=200),
        "status": "ACTIVE",
        "reference_of_record": True,
    }
    defaults.update(over)
    return ev_gate.ReportView(**defaults)


def binding_for(
    run: ev_gate.RunView, subject: ev_gate.SubjectRef = CANDIDATE
) -> ev_gate.GateBinding:
    return ev_gate.GateBinding(
        tenant_id=DEFAULT_TENANT_ID,
        capability_id=CAPABILITY,
        criteria_version=3,
        dataset_version_id=run.dataset_version_id,
        split_id=run.split_id,
        partition=run.partition,
        annotation_set_id=run.annotation_set_id,
        candidate_subject=subject,
    )


# =====================================================================================
# 1. The naive gate is absent.  Chapter 7 acceptance check 18.
# =====================================================================================
# "Grep the gate implementation for a direct comparison of two aggregate metric values
# (`new.value < old.value` and equivalents). Expect zero hits on any blocking path."
#
# Written as patterns over the SOURCE and not as a behavioural assertion, because the
# defect this catches is a future edit: somebody adding a "quick sanity check" beside the
# non-inferiority test. A behavioural test would still pass with the quick check in place
# for every fixture that agrees with it.
_NAIVE_PATTERNS = (
    r"\bnew\w*\s*(?:<|>|<=|>=)\s*old\b",
    r"\bold\w*\s*(?:<|>|<=|>=)\s*new\b",
    r"\bcandidate\w*\.value\s*(?:<|>|<=|>=)",
    r"\bincumbent\w*\.value\s*(?:<|>|<=|>=)",
    r"mean\([^)]*new[^)]*\)\s*(?:<|>|<=|>=)\s*mean\(",
    r"\.value\s*(?:<|>|<=|>=)\s*\w*old\w*\.value",
)


def test_the_naive_gate_is_absent() -> None:
    """Chapter 7 acceptance check 18, section 7.9.1, 15.2.5."""
    for name in ("gate.py", "criteria.py"):
        # `medos/medos/`: the package is nested inside the product directory now.
        source = (_REPO / "medos" / "medos" / "evidence" / name).read_text(
            encoding="utf-8"
        )
        # Comments quote the forbidden form on purpose (it is what the module exists to
        # refute), so the grep runs over code lines only.
        code = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        for pattern in _NAIVE_PATTERNS:
            hits = re.findall(pattern, code)
            assert not hits, (
                f"medos/medos/evidence/{name} contains a direct comparison of two aggregate "
                f"metric values ({hits!r}). Section 7.9.1: that form blocks half of all "
                f"harmless rebuilds and passes a model that lost the hardest subgroup."
            )

    gate_source = Path("medos/medos/evidence/gate.py").read_text(encoding="utf-8")
    assert "def non_inferiority" in gate_source
    assert "def catastrophic_count" in gate_source
    assert "margin" in gate_source


def test_the_selector_grammar_is_chapter_sevens() -> None:
    """MOS-EVID-076: a closed op set, and section 7.8.1 fixes which eight."""
    assert set(ev_crit.SELECTOR_OPS) == {
        "==", "!=", "<", "<=", ">", ">=", "in", "not_in"
    }
    # MOS-EVID-076 forbids a dynamic code path. The evaluator must not reach `eval`.
    source = Path("medos/medos/evidence/criteria.py").read_text(encoding="utf-8")
    assert "eval(" not in source.replace("evaluate", "")
    assert "exec(" not in source


# =====================================================================================
# 2. Non-inferiority tolerates noise.  Chapter 7 acceptance check 19.
# =====================================================================================
def _false_block_trial(n_patients: int, trials: int, *, seed: int, b: int) -> tuple[int, int]:
    """`(ni_passes, point_passes)` over `trials` paired runs with TRUE zero difference."""
    rng = random.Random(seed)
    keys = [f"pt_{i:04d}" for i in range(n_patients)]
    ni_passes = point_passes = 0
    for _ in range(trials):
        old = [rng.uniform(0.60, 0.95) for _ in keys]
        # True zero effect: the per-case difference is symmetric noise with sd 0.05.
        new = [o + rng.gauss(0.0, 0.05) for o in old]
        result = ev_gate.non_inferiority(new, old, keys, 0.02, b=b)
        ni_passes += bool(result["passed"])
        # The NAIVE gate, computed HERE and never in the gate: mean(new) - mean(old) is
        # identically mean(d), so `mean_delta > 0` IS `new.value > old.value`.
        point_passes += bool(result["mean_delta"] > 0.0)
    return ni_passes, point_passes


def test_non_inferiority_tolerates_noise() -> None:
    """Chapter 7 acceptance check 19, with its arithmetic corrected. SPEC DEFECT, below.

    The check reads: "Generate 200 synthetic paired runs with true zero difference and
    per-case paired-difference sd 0.05 on n = 40 patients. Assert the paired
    non-inferiority rule with delta = 0.02 passes in more than 90 % of them, and assert a
    point-estimate comparison against zero passes in approximately 50 % (within 45-55 %)."

    THE SECOND HALF HOLDS EXACTLY. THE FIRST IS UNREACHABLE AT n = 40, and not because of
    any implementation choice -- it is arithmetic on the parameters the check itself
    fixes. Under MOS-EVID-085 the rule passes when `mean(d) - z(0.95)*SE > -delta`, i.e.
    when `mean(d) > -delta + 1.645*SE`. With sd = 0.05 and n = 40, SE = 0.05/sqrt(40) =
    0.00791, so the rule passes when `mean(d) > -0.00700`; and `mean(d) ~ N(0, 0.00791)`,
    so that happens with probability `P(Z > -0.885) = 0.812`. Eighty-one per cent, not
    ninety. Solving the same expression for the cohort size the stated 90 % requires gives
    `delta/SE > 2.927`, i.e. **n >= 54 patients**.

    The check is therefore run BOTH ways here:
      * at the check's own n = 40, the observed rate is asserted against the closed-form
        prediction (0.75-0.88). That is the assertion that would catch a broken test --
        a two-sided interval, an uncorrected alpha, a comparison against zero instead of
        against -delta -- each of which lands far outside that band;
      * at n = 60, the smallest round cohort above the 54 the arithmetic requires, the
        rate is asserted to exceed the check's 90 %. That is MOS-EVID-089 in action and
        it is the property the check was reaching for.

    REPORTED as a specification defect: acceptance check 19's n = 40 and its "> 90 %"
    cannot both be met by any correct one-sided test at sd = 0.05 and delta = 0.02. The
    smallest fix is to change 40 to 54 (or to widen delta to 0.027, which is a clinical
    change and MOS-EVID-087 forbids making it to pass a test).
    """
    trials = 200
    ni_passes, point_passes = _false_block_trial(
        40, trials, seed=20260101, b=metrics_b()
    )

    assert 0.75 <= ni_passes / trials <= 0.88, (
        f"the paired rule passed {ni_passes}/{trials} at n = 40; closed-form normal "
        f"theory predicts 0.812 for a correct one-sided 95 % test at sd = 0.05, "
        f"delta = 0.02. A rate far outside this band means the test is not the one "
        f"MOS-EVID-085 describes."
    )
    # And whatever else is true, it is nothing like a coin flip -- which is the entire
    # point of replacing the naive comparison.
    assert ni_passes / trials > 0.70

    assert 0.45 <= point_passes / trials <= 0.55, (
        f"the point-estimate comparison passed {point_passes}/{trials}; section 7.9.1's "
        f"claim is that it is a coin flip at zero true effect -- 'P(observed mean < 0) = "
        f"0.5 ... for n = 40 and for n = 4000' -- and a rate outside 45-55 % means this "
        f"fixture is not testing what the requirement describes"
    )

    # The check's stated 90 %, at the cohort size its own arithmetic requires.
    ni_passes_60, point_passes_60 = _false_block_trial(60, trials, seed=7, b=600)
    assert ni_passes_60 / trials > 0.90, (
        f"the paired rule blocked {trials - ni_passes_60}/{trials} runs with a true zero "
        f"effect at n = 60; MOS-EVID-089 requires an equivalent model to pass, and more "
        f"reliably on a larger cohort"
    )
    # More data did NOT help the naive comparison. Section 7.9.1: "That probability is
    # 0.5 for n = 40 and for n = 4000."
    assert 0.40 <= point_passes_60 / trials <= 0.60


def metrics_b() -> int:
    """MOS-EVID-058's declared `B`. Named so the 200-run loop above states which B it
    used rather than inheriting a default that could change underneath it."""
    from medos.evidence import metrics as _m

    return _m.DEFAULT_BOOTSTRAP_B


def test_more_evidence_makes_it_easier_to_pass() -> None:
    """MOS-EVID-089: "the interval narrows with n, so a genuinely equivalent model passes
    more reliably on a larger cohort". Any replacement test MUST preserve this."""
    rng = random.Random(7)

    def block_rate(n_patients: int) -> float:
        blocked = 0
        for _ in range(60):
            keys = [f"pt_{i:04d}" for i in range(n_patients)]
            old = [rng.uniform(0.60, 0.95) for _ in keys]
            new = [o + rng.gauss(0.0, 0.05) for o in old]
            blocked += not ev_gate.non_inferiority(new, old, keys, 0.02, b=600)["passed"]
        return blocked / 60

    assert block_rate(160) <= block_rate(20)


# =====================================================================================
# 3. Non-inferiority catches a real regression.  Chapter 7 acceptance check 20.
# =====================================================================================
def test_regression_catches_a_stratum_collapse() -> None:
    """The candidate loses 0.30 on every case under 100 mL and is unchanged elsewhere.

    Acceptance check 20 asks for BOTH halves and the second is the interesting one:
      * `ni_dice_small` (and/or `ni_dice_positive`) returns FAIL;
      * the cohort-wide `dice_mean_per_case` POINT ESTIMATE moved by less than 0.08 --
        "demonstrating that the aggregate alone would not have blocked it".
    """
    base = cohort(n_positive=30, n_small=6, n_empty=10)
    collapsed = perturb(
        base,
        lambda r: r.value - 0.30 if r.strata["reference_volume_ml"] < 100.0 else r.value,
    )

    incumbent = report_view(run_view(base), INCUMBENT)
    candidate = report_view(run_view(collapsed), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, incumbent, DOC, binding_for(candidate.run), now=NOW
    )

    assert result.verdict == "FAIL"
    assert result.by_id("ni_dice_small").status == "FAIL"
    assert result.by_id("ni_dice_small").reason == "non_inferiority_not_shown"

    # The half that matters: the aggregate did not move enough to block it.
    eligible_base = [r.value for r in base if r.eligible and r.value is not None]
    eligible_new = [r.value for r in collapsed if r.eligible and r.value is not None]
    shift = abs(
        sum(eligible_base) / len(eligible_base) - sum(eligible_new) / len(eligible_new)
    )
    assert shift < 0.08, (
        f"the cohort-wide point estimate moved {shift:.3f}; acceptance check 20 needs it "
        f"under 0.08 for the fixture to demonstrate what it claims"
    )
    # And the absolute criterion over the whole positive stratum still passes, which is
    # the precise sense in which "the aggregate alone would not have blocked it".
    assert result.by_id("dice_positive").status == "PASS"


def test_catastrophic_count_fires_where_the_mean_cannot() -> None:
    """3 cases drop by 0.35, the mean difference is +0.01. Acceptance check 21.

    "Assert `no_catastrophic` returns FAIL while the mean-based non-inferiority criterion
    returns PASS." This is the failure mode MOS-EVID-088 exists for and the one a tighter
    margin on the mean cannot reach.

    THE COHORT SIZE IS 100 POSITIVES AND NOT 40, and the reason is the requirement's own
    sentence: "three cases collapsing from 0.85 to 0.05 while A HUNDRED OTHERS improve
    slightly". Three catastrophic drops in thirty cases inflate the paired difference's
    standard deviation to 0.12, so the non-inferiority test FAILS as well and the fixture
    proves nothing about the gap between the two criteria. At a hundred, the sd is 0.063,
    the interval's lower bound clears -delta, and the two criteria genuinely disagree --
    which is the disagreement MOS-EVID-088 exists to create.
    """
    base = cohort(n_positive=100, n_small=6, n_empty=10)
    positives = [r for r in base if r.eligible]
    # The doomed cases are LARGE effusions, so `ni_dice_small` is not what fires: the
    # claim under test is that a mean-based criterion over the SAME stratum misses them.
    doomed = {positives[i].case_key for i in (10, 40, 70)}

    def shift(r: CaseMetricRow) -> float:
        return r.value - 0.35 if r.case_key in doomed else r.value + 0.02

    candidate_rows = perturb(base, shift)

    incumbent = report_view(run_view(base), INCUMBENT)
    candidate = report_view(run_view(candidate_rows), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, incumbent, DOC, binding_for(candidate.run), now=NOW
    )

    assert result.by_id("no_catastrophic").status == "FAIL"
    assert result.by_id("no_catastrophic").observed == 3.0
    assert result.by_id("ni_dice_positive").status == "PASS", (
        "the mean-based criterion must PASS here, or this fixture does not demonstrate "
        "the gap MOS-EVID-088 exists to close"
    )
    assert result.verdict == "FAIL"


def test_a_within_noise_rebuild_passes_the_whole_gate() -> None:
    """The other half of the `non-inferiority` gate check: a harmless rebuild is NOT
    blocked. A gate that blocks these is switched off within two weeks (section 7.9.1)."""
    rng = random.Random(4242)
    base = cohort()
    rebuilt = perturb(base, lambda r: r.value + rng.gauss(0.0, 0.01))

    incumbent = report_view(run_view(base), INCUMBENT)
    candidate = report_view(run_view(rebuilt), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, incumbent, DOC, binding_for(candidate.run), now=NOW
    )
    assert result.verdict == "PASS", [r.as_dict() for r in result.results]
    # MOS-EVID-113: one entry per criterion, no exceptions.
    assert {r.id for r in result.results} == {
        "dice_positive", "fp_on_negatives",
        "ni_dice_positive", "ni_dice_small", "no_catastrophic",
    }


# =====================================================================================
# 4. INDETERMINATE is its own state.  Chapter 7 acceptance check 22.
# =====================================================================================
def test_insufficient_cases_are_indeterminate_not_pass() -> None:
    """A 25-patient cohort against `min_patients: 100`.

    MOS-EVID-083: INDETERMINATE "MUST be reported as its own state, never folded into
    FAIL, because the two require different actions -- one needs more data, the other
    needs a different model".
    """
    doc = criteria_document()
    for criterion in doc["spec"]["absolute"] + doc["spec"]["regression"]:
        criterion["min_cases"] = 120
        criterion["min_patients"] = 100

    base = cohort(n_positive=20, n_small=5, n_empty=5)
    incumbent = report_view(run_view(base), INCUMBENT)
    candidate = report_view(run_view(base), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, incumbent, doc, binding_for(candidate.run), now=NOW
    )

    assert result.verdict == "INDETERMINATE"
    assert result.verdict != "FAIL"
    assert all(
        r.status == "INDETERMINATE" and r.reason == "insufficient_cases"
        for r in result.results
    ), [r.as_dict() for r in result.results]


def test_insufficient_cases_may_be_declared_a_failure() -> None:
    """`on_insufficient_cases: fail` is the other permitted value (section 7.8.1)."""
    doc = criteria_document()
    for criterion in doc["spec"]["absolute"]:
        criterion["min_patients"] = 100
        criterion["on_insufficient_cases"] = "fail"
    base = cohort(n_positive=20, n_small=5, n_empty=5)
    result = ev_gate.evaluate_gate(
        report_view(run_view(base), CANDIDATE), None, doc,
        binding_for(run_view(base)), now=NOW,
    )
    assert result.verdict == "FAIL"


# =====================================================================================
# 5. Pairing and integrity.  MOS-EVID-086, acceptance checks 7 and 15.
# =====================================================================================
def test_unpaired_runs_are_indeterminate_not_compared() -> None:
    """MOS-EVID-086: "An unpaired comparison is not a regression test."

    Acceptance check 15 states it over a backend conversion -- a run produced with
    `accelerator.trt = 10.0.1` attached to a version whose other runs used 9.3.0 -- and
    requires `INDETERMINATE` with reason `unpaired_runs` or an explicit refusal.
    """
    base = cohort()
    incumbent = report_view(run_view(base), INCUMBENT)
    candidate = report_view(
        run_view(base, split_digest="sha256:" + "f" * 64), CANDIDATE
    )
    result = ev_gate.evaluate_gate(
        candidate, incumbent, DOC, binding_for(candidate.run), now=NOW
    )
    for cid in ("ni_dice_positive", "ni_dice_small", "no_catastrophic"):
        assert result.by_id(cid).status == "INDETERMINATE"
        assert result.by_id(cid).reason == "unpaired_runs"
    assert result.verdict == "INDETERMINATE"


def test_reference_not_of_record_fails_the_gate() -> None:
    """Chapter 7 acceptance check 7, MOS-EVID-042."""
    base = cohort()
    candidate = report_view(run_view(base), CANDIDATE, reference_of_record=False)
    result = ev_gate.evaluate_gate(
        candidate, None, DOC, binding_for(candidate.run), now=NOW
    )
    assert result.verdict == "FAIL"
    assert result.by_id("integrity").reason == "reference_not_of_record"
    # MOS-EVID-113: the record still names every criterion, so "why was this refused" is
    # answerable without re-running anything.
    assert len(result.results) == 1 + 5


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"signature_verified": False}, "signature_invalid"),
        ({"criteria_version": 2}, "criteria_version_mismatch"),
        ({"subject": INCUMBENT}, "subject_mismatch"),
        ({"valid_until": NOW - timedelta(days=1)}, "report_expired"),
        ({"status": "REVOKED"}, "report_revoked"),
    ],
)
def test_step_zero_integrity_checks(mutation: dict[str, Any], expected: str) -> None:
    """Section 7.9.3 step 0. MOS-EVID-121: an unsigned report MUST NOT be accepted."""
    mutation = dict(mutation)
    subject = mutation.pop("subject", CANDIDATE)
    candidate = report_view(run_view(cohort()), subject, **mutation)
    result = ev_gate.evaluate_gate(
        candidate, None, DOC, binding_for(candidate.run), now=NOW
    )
    assert result.verdict == "FAIL"
    assert result.by_id("integrity").reason == expected
    assert expected in ev_gate.INTEGRITY_REASONS


def test_a_run_that_did_not_succeed_cannot_gate() -> None:
    """MOS-EVID-061: a run missing any binding field MUST NOT reach SUCCEEDED, so a run
    that has not reached it is not evidence and MUST NOT pass a gate."""
    candidate = report_view(run_view(cohort(), state="RUNNING"), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, None, DOC, binding_for(candidate.run), now=NOW
    )
    assert result.by_id("integrity").reason == "run_not_succeeded"


def test_first_deployment_skips_regression_but_not_the_absolute_bar() -> None:
    """Section 7.9.3: `SKIPPED` with `no_incumbent_first_deployment`, per criterion."""
    candidate = report_view(run_view(cohort()), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, None, DOC, binding_for(candidate.run), now=NOW
    )
    assert result.verdict == "PASS"
    skipped = [r for r in result.results if r.status == "SKIPPED"]
    assert {r.id for r in skipped} == {
        "ni_dice_positive", "ni_dice_small", "no_catastrophic"
    }
    assert all(r.reason == "no_incumbent_first_deployment" for r in skipped)
    # The absolute half still ran: a first deployment is not a free pass.
    assert result.by_id("dice_positive").status == "PASS"

    # And it is not a free pass in the other direction either.
    weak = perturb(cohort(), lambda r: 0.20)
    poor = report_view(run_view(weak), CANDIDATE)
    assert ev_gate.evaluate_gate(
        poor, None, DOC, binding_for(poor.run), now=NOW
    ).verdict == "FAIL"


def test_advisory_criteria_are_reported_and_never_gate() -> None:
    """MOS-EVID-084."""
    doc = criteria_document()
    doc["spec"]["absolute"].append(
        {
            "id": "dice_thick_slice",
            "metric": "dice_mean_per_case",
            "stratum": "small_effusion",
            "bound": "ci_lower_95",
            "op": ">=",
            "value": 0.99,          # unreachable on purpose
            "min_cases": 1,
            "min_patients": 1,
            "on_insufficient_cases": "indeterminate",
            "severity": "advisory",
        }
    )
    candidate = report_view(run_view(cohort()), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, None, doc, binding_for(candidate.run), now=NOW
    )
    assert result.by_id("dice_thick_slice").status == "FAIL"
    assert result.verdict == "PASS"


def test_bound_defaults_to_the_interval_not_the_point() -> None:
    """MOS-EVID-077: "Gating on a point estimate is a defect: on 40 cases the point
    estimate crosses a fixed bar on noise alone."""
    doc = criteria_document()
    criterion = doc["spec"]["absolute"][0]
    del criterion["bound"]
    ev_crit.validate_criteria(doc, known_strata_fields=KNOWN_STRATA)
    candidate = report_view(run_view(cohort()), CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, None, doc, binding_for(candidate.run), now=NOW
    )
    assert result.by_id("dice_positive").bound == "ci_lower_95"


# =====================================================================================
# 6. Criteria authoring.  Chapter 7 acceptance check 16, MOS-EVID-075/079.
# =====================================================================================
def test_criteria_authoring_is_validated() -> None:
    """Acceptance check 16, both halves."""
    # Half one: `dice_mean_per_case` with no companion empty-GT criterion (MOS-EVID-052).
    doc = criteria_document()
    doc["spec"]["absolute"] = [doc["spec"]["absolute"][0]]
    with pytest.raises(ev_crit.CriteriaError, match="MOS-EVID-052"):
        ev_crit.validate_criteria(doc, known_strata_fields=KNOWN_STRATA)

    # Half two: a stratum over a field nothing persists (MOS-EVID-067).
    doc = criteria_document()
    doc["spec"]["strata"].append(
        {"id": "serial", "selector": {"scanner_serial": {"op": "==", "value": "SN-1"}}}
    )
    with pytest.raises(ev_crit.CriteriaError, match="scanner_serial"):
        ev_crit.validate_criteria(doc, known_strata_fields=KNOWN_STRATA)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d["spec"]["absolute"][0].__setitem__("metric", "dice"), "registry"),
        (lambda d: d["spec"]["absolute"][0].__setitem__("bound", "whatever"), "bound"),
        (lambda d: d["spec"]["absolute"][0].__setitem__("lattitude", 1), "unknown"),
        (lambda d: d["spec"]["regression"][0].pop("margin"), "margin"),
        (lambda d: d["spec"].pop("margin_rationale"), "margin_rationale"),
        (lambda d: d["spec"].__setitem__("combine", "any_of"), "all_of"),
        (lambda d: d["spec"].__setitem__("p95_latency_ms", 41200), "ENGINEERING"),
        (lambda d: d["spec"]["absolute"][0].__setitem__("severity", "advisery"), "severity"),
        (lambda d: d["spec"]["regression"][0].__setitem__("margin", 0.0), "> 0"),
        (lambda d: d["spec"]["regression"][0].__setitem__("op", ">="), "no `op`"),
    ],
)
def test_the_grammar_refuses_a_malformed_document(mutate: Any, match: str) -> None:
    """MOS-EVID-079, MOS-EVID-075, MOS-EVID-087: rejected AT WRITE TIME."""
    doc = criteria_document()
    mutate(doc)
    with pytest.raises(ev_crit.CriteriaError, match=match):
        ev_crit.validate_criteria(doc, known_strata_fields=KNOWN_STRATA)


def test_a_threshold_metric_needs_its_operating_point() -> None:
    """MOS-EVID-078 and MOS-EVID-055: a number without an operating point is not a
    measurement, so a criterion over one without a declared point is not a bar."""
    doc = criteria_document()
    doc["spec"]["absolute"].append(
        {
            "id": "sens_all",
            "metric": "sensitivity",
            "stratum": "all",
            "bound": "ci_lower_95",
            "op": ">=",
            "value": 0.90,
            "min_cases": 20,
            "min_patients": 20,
            "on_insufficient_cases": "indeterminate",
            "severity": "blocking",
        }
    )
    with pytest.raises(ev_crit.CriteriaError, match="operating_threshold"):
        ev_crit.validate_criteria(doc, known_strata_fields=KNOWN_STRATA)

    doc["spec"]["absolute"][-1]["operating_threshold"] = {
        "name": "effusion_probability", "value": 0.50
    }
    ev_crit.validate_criteria(doc, known_strata_fields=KNOWN_STRATA)

    # And it must MATCH the run's, or the criterion is INDETERMINATE, never compared.
    run = run_view(
        cohort(),
        operating_thresholds={
            "effusion_probability": {"value": 0.35, "selected_on": "tune"}
        },
    )
    candidate = report_view(run, CANDIDATE)
    result = ev_gate.evaluate_gate(
        candidate, None, doc, binding_for(run), now=NOW
    )
    assert result.by_id("sens_all").reason == "threshold_mismatch"
    assert result.by_id("sens_all").status == "INDETERMINATE"


# =====================================================================================
# 7. Ownership: the clinical bar belongs to the Capability.  MOS-REG-049, MOS-EVID-071.
# =====================================================================================
@pytest.mark.parametrize(
    "manifest",
    [
        {"spec": {"acceptance_criteria": {"sensitivity": 0.9}}},
        {"spec": {"capabilities": [{"id": CAPABILITY, "metrics": {"dice": 0.91}}]}},
        {"spec": {"clinical_acceptance": {"dice_mean_per_case": 0.8}}},
        {"performance": {"auroc": 0.97}},
    ],
)
def test_a_version_manifest_may_not_declare_the_clinical_bar(manifest: dict) -> None:
    """MOS-REG-049: "The registry MUST reject a `service_version` that declares clinical
    acceptance thresholds in its own manifest." MOS-EVID-071 says the same about a
    free-form metrics map.

    The reason is not tidiness. A bar carried by the artifact is a bar its publisher sets,
    so every new version ships with the bar it happens to pass and the gate is a tautology.
    """
    with pytest.raises(ev_crit.CriteriaError, match="Capability"):
        ev_acc.reject_clinical_acceptance_in_manifest(manifest)


def test_the_engineering_bar_is_allowed_on_the_version() -> None:
    """MOS-EVID-075's other half: latency, GPU ceiling and schema validity belong to the
    ServiceVersion (section 6.4's `spec.engineering_acceptance`) and MUST NOT be refused."""
    ev_acc.reject_clinical_acceptance_in_manifest(
        {
            "spec": {
                "engineering_acceptance": {
                    "p95_latency_ms": 60000,
                    "peak_gpu_memory_mib": 12000,
                    "result_schema_valid_rate": 1.0,
                }
            }
        }
    )


# =====================================================================================
# 8. Tenant overrides only tighten.  Chapter 7 acceptance check 17, MOS-EVID-081.
# =====================================================================================
def test_tenant_overrides_only_tighten() -> None:
    """"Submit a `threshold_overrides` entry lowering `sens_all` from 0.90 to 0.85.
    Expect rejection. Submit one raising it to 0.93. Expect acceptance." -- check 17,
    stated over `dice_positive`, whose criterion `op` is the same `>=`."""
    doc = criteria_document()

    with pytest.raises(ev_acc.OverrideRelaxes, match="RELAXES"):
        ev_acc.apply_threshold_overrides(doc, {"dice_positive": {"value": 0.50}})

    tightened = ev_acc.apply_threshold_overrides(doc, {"dice_positive": {"value": 0.85}})
    assert tightened["spec"]["absolute"][0]["value"] == 0.85
    # The Capability's own document is untouched: a tenant binding does not edit the
    # Capability (MOS-EVID-075).
    assert doc["spec"]["absolute"][0]["value"] == 0.60


def test_overrides_compare_in_the_direction_of_the_op() -> None:
    """MOS-EVID-081: "comparing in the direction of the criterion's `op`". On a `<=`
    criterion the tightening direction is DOWN, which is the case a naive
    `new >= old` check gets exactly backwards."""
    doc = criteria_document()
    with pytest.raises(ev_acc.OverrideRelaxes):
        ev_acc.apply_threshold_overrides(doc, {"fp_on_negatives": {"value": 0.90}})
    ok = ev_acc.apply_threshold_overrides(doc, {"fp_on_negatives": {"value": 0.05}})
    assert ok["spec"]["absolute"][1]["value"] == 0.05


def test_a_widened_margin_is_a_relaxation() -> None:
    """A larger delta TOLERATES MORE LOSS, so widening it is relaxing the bar even though
    the number went up. MOS-EVID-087 also forbids tuning delta to make a candidate pass."""
    doc = criteria_document()
    with pytest.raises(ev_acc.OverrideRelaxes, match="tolerates MORE loss"):
        ev_acc.apply_threshold_overrides(doc, {"ni_dice_positive": {"margin": 0.10}})
    tight = ev_acc.apply_threshold_overrides(doc, {"ni_dice_positive": {"margin": 0.01}})
    assert tight["spec"]["regression"][0]["margin"] == 0.01


def test_an_override_naming_an_unknown_criterion_is_refused() -> None:
    """Silently ignoring it would leave a site believing a tightening is in force."""
    with pytest.raises(ev_acc.OverrideRelaxes, match="does not declare"):
        ev_acc.apply_threshold_overrides(
            criteria_document(), {"sens_all": {"value": 0.99}}
        )


def test_a_tightened_override_actually_changes_the_verdict() -> None:
    """An override nobody evaluates is a comment. This is the end-to-end of check 17."""
    base = cohort()
    candidate = report_view(run_view(base), CANDIDATE)
    assert ev_gate.evaluate_gate(
        candidate, None, DOC, binding_for(candidate.run), now=NOW
    ).verdict == "PASS"

    strict = ev_acc.apply_threshold_overrides(DOC, {"dice_positive": {"value": 0.95}})
    assert ev_gate.evaluate_gate(
        candidate, None, strict, binding_for(candidate.run), now=NOW
    ).verdict == "FAIL"


# =====================================================================================
# Database fixtures.  Everything below needs Postgres.
# =====================================================================================
@pytest.fixture()
def gate_db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One connection with the default tenant bound and 0008's four tables emptied.

    TRUNCATE and not DELETE: `deployment_gate_decisions` and `acceptance_criteria` both
    carry a BEFORE DELETE guard (MOS-EVID-091, MOS-EVID-013's construction), and TRUNCATE
    does not fire row-level triggers -- so the seal holds for application code while the
    harness can still start clean.

    Nothing outside migration 0008 is truncated. The cohort tables of 0006 and the run
    tables of 0007 are shared with suites running in parallel, and truncating another
    agent's job tables is exactly how this project produced a spurious red gate once
    already.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(f"TRUNCATE {', '.join(GATE_TABLES)} CASCADE")
    conn.commit()
    # The repository layer opens its own `tenant_tx`, which reads the ContextVar rather
    # than the session GUC -- `medos.db.tenancy` is the ONLY writer of that GUC and a test
    # that set it directly would be a second one (MOS-SEC-074/075, and the suite asserts
    # there is exactly one).
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        reset_current_tenant(token)
        conn.rollback()
        conn.close()


def _uid(nonce: int, *parts: int) -> str:
    return f"1.2.826.0.1.3680043.10.8888.{nonce}." + ".".join(str(p) for p in parts)


def _series_record(patient: int, *, site: int, nonce: int) -> SeriesRecord:
    """One series. `nonce` makes every `_Cohort` a DIFFERENT cohort.

    `seal_dataset_version` is idempotent on `manifest_digest` (MOS-TRAIN-209: detect the
    collision and REUSE the existing DatasetVersion), so two cohorts built from identical
    records are one row -- and the second `freeze_split` then collides on
    `UNIQUE (dataset_version_id, name)`. That is the seal working correctly; the fixture
    has to stop pretending two identical corpora are two corpora.
    """
    return SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/GATE", f"P{nonce}-{patient:04d}"),
        study_instance_uid=_uid(nonce, patient, 1),
        series_instance_uid=_uid(nonce, patient, 1, 1),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(nonce, patient, 1, 1, i) for i in range(3)),
        series_pixel_digest="sha256:" + ev_digest.sha256_of(
            f"gate/{nonce}/{patient}".encode()
        ).split(":")[-1],
        acquisition=Acquisition(
            slice_thickness_mm=1.25 if site else 2.5,
            pixel_spacing_mm=(0.703125, 0.703125),
            convolution_kernel="B30f" if site else "STANDARD",
            convolution_kernel_class="soft" if site else "standard",
            manufacturer="SIEMENS" if site else "GE MEDICAL SYSTEMS",
            manufacturer_model_name="Sensation 16" if site else "Discovery CT750 HD",
            kvp=120.0,
            contrast_phase="non_contrast",
            z_coverage_mm=332.5,
            image_type=("ORIGINAL", "PRIMARY", "AXIAL"),
            patient_age_years=60 + patient % 20,
            patient_sex="M" if patient % 2 else "F",
            body_part_examined="CHEST",
        ),
        institution_key=ev_digest.institution_key(SALT, f"SITE-{site}"),
        study_year=2022 + site,
        accession_number_hash=ev_digest.accession_number_hash(
            SALT, f"ACC{nonce}-{patient:04d}"
        ),
    )


class _Cohort:
    """A sealed DatasetVersion, a frozen split and a frozen AnnotationSet.

    Built through the 0006 repository rather than by raw INSERT, so the acceptance
    binding's three foreign keys point at rows that really went through the seal.
    """

    # 40 and not 24: `medos.evidence.repo` refuses to seal an `acceptance` cohort with
    # fewer than 30 patients (MOS-EVID-026) -- "below that no criterion in 7.8 can
    # return anything but INDETERMINATE", which is the same insufficient-cases rule
    # acceptance check 22 exercises from the other side.
    def __init__(self, conn: psycopg.Connection[Any], n_patients: int = 40) -> None:
        store = InMemoryManifestStore()
        # Two sites, two scanners, two kernel classes: the minimum that passes the
        # MOS-TRAIN-088 corpus checks the seal runs (C1, C2, C3, C7).
        nonce = secrets.randbelow(10**9)
        records = [
            _series_record(p, site=p % 2, nonce=nonce) for p in range(n_patients)
        ]

        self.dataset = ev_repo.create_dataset(
            conn,
            slug=f"gate-cohort-{secrets.token_hex(4)}",
            display_name="deployment gate cohort",
            purpose="acceptance",           # MOS-EVID-082: never training or tuning
            custodian="TCIA/GATE",
            created_by=OPERATOR,
        )
        self.version = ev_repo.seal_dataset_version(
            conn,
            dataset_id=self.dataset.id,
            records=records,
            store=store,
            bucket=BUCKET,
            sealed_by=OPERATOR,
            deidentification_status="public_deidentified",
            deid_policy_id="ps315-basic/v4",
            uid_mapping_table_id="uidmap/v1",
        )
        assignments = [(r.patient_key, "test") for r in records]
        self.split = ev_repo.freeze_split(
            conn,
            dataset_version_id=self.version.id,
            name="acceptance",
            assignments=assignments,
            store=store,
            bucket=BUCKET,
            frozen_by=OPERATOR,
            assignment_method="every patient in test; this cohort exists to be gated on",
            records=records,
        )
        self.annotations = ev_repo.freeze_annotation_set(
            conn,
            dataset_version_id=self.version.id,
            name="reference",
            capability_id=CAPABILITY,
                # `annotation_sets.label_definition_id` is a `capability_concepts` uuid
            # (MOS-EVID-143), and that table is chapter 6's registry -- not shipped
            # before 0.3.0. A derived uuid keeps the column honest about its type
            # without inventing a registry row.
            label_definition_id=LABEL_DEFINITION_ID,
            annotation_type="mask",
            consensus_rule="single_reader",
            readers=[
                {
                    "reader_id": "rdr_1",
                    "role": "radiologist",
                    "years_experience": 12,
                    "board_certified": True,
                    "specialty": "thoracic",
                    "tool": "3D Slicer 5.6",
                    "instructions_uri": "https://example.invalid/instructions/v1",
                }
            ],
            entries=[
                {
                    "case_key": r.study_instance_uid,
                    "patient_key": r.patient_key,
                    "study_instance_uid": r.study_instance_uid,
                    "series_instance_uid": r.series_instance_uid,
                    "reference_kind": "mask",
                    "reference_bucket": BUCKET,
                    "reference_object_key": f"ref/{r.series_instance_uid}",
                    "reference_digest": "sha256:" + "d" * 64,
                    "reference_volume_ml": 150.0,
                    "per_reader": [
                        {
                            "reader_id": "rdr_1",
                            "bucket": BUCKET,
                            "object_key": f"ref/{r.series_instance_uid}",
                            "digest": "sha256:" + "d" * 64,
                            "volume_ml": 150.0,
                        }
                    ],
                    "annotation_provenance": "de_novo",
                }
                for r in records
            ],
            store=store,
            bucket=BUCKET,
            frozen_by=OPERATOR,
            reference_of_record=True,
        )
        self.records = records


def _evaluation_run(
    conn: psycopg.Connection[Any], c: _Cohort, *, model_version: str
) -> str:
    """A real `evaluation_runs` row, so `deployments.acceptance_run_id` points at one.

    Only the BINDING is created here. Driving 0007's PENDING -> RUNNING -> SUCCEEDED
    machine belongs to that migration's own test (MOS-EVID-061, MOS-EVID-062); a second,
    weaker copy of it here would assert 0007's invariants badly rather than this
    migration's well. What this migration owns is that a clinical deployment cannot exist
    without a run to point at, and that is the foreign key.
    """
    run = ev_run.create_run(
        conn,
        kind="site_acceptance",
        capability_id=CAPABILITY,
        model_version_id=str(uuid.uuid5(uuid.NAMESPACE_URL, model_version)),
        dataset_version_id=c.version.id,
        split_id=c.split.id,
        partition="test",
        annotation_set_id=c.annotations.id,
        code_commit="7e41b2c8a95d3f06b18e4c7a2d905f3b6c81e024",
        image_digest="sha256:" + "5" * 64,
        runner="pytest",
        fp_volume_threshold_ml=10.0,
        preprocessing_spec_id="ct-lung/v3",
        preprocessing_spec_version=3,
        preprocessing_spec_digest="sha256:" + "9" * 64,
        operating_thresholds={
            "effusion_probability": {"value": 0.50, "selected_on": "tune"}
        },
    )
    return run.id


# =====================================================================================
# 9. The migration.
# =====================================================================================
def test_the_gate_tables_exist_with_their_constraints(gate_db) -> None:
    """Structural invariants, asserted rather than trusted to `CREATE TABLE` succeeding."""
    rows = gate_db.execute(
        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = ANY(%s)",
        (list(GATE_TABLES),),
    ).fetchall()
    assert {r["relname"] for r in rows} == set(GATE_TABLES)
    for r in rows:
        # MOS-SEC-077 / MOS-STORE-229. `acceptance_criteria` is dual-scope
        # (MOS-STORE-220) and still forces row security; its READ policy is what admits
        # the platform-global row.
        assert r["relrowsecurity"] and r["relforcerowsecurity"], r["relname"]

    # MOS-REG-074: at most one live ACTIVE per slot, and it must be PARTIAL or blue/green
    # is impossible.
    index = gate_db.execute(
        "SELECT i.indisunique, pg_get_expr(i.indpred, i.indrelid) AS pred "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = 'deployments_one_active_per_slot'"
    ).fetchone()
    assert index["indisunique"] and "ACTIVE" in index["pred"]

    # MOS-EVID-083: the verdict is three-valued and a two-valued column MUST NOT be
    # substituted (section 7.12.1).
    verdict_check = gate_db.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conrelid = 'deployment_gate_decisions'::regclass AND contype = 'c' "
        "AND pg_get_constraintdef(oid) LIKE '%%INDETERMINATE%%'"
    ).fetchall()
    assert verdict_check


def test_acceptance_criteria_are_sealed(gate_db) -> None:
    """MOS-EVID-087: "a change to delta MUST bump `AcceptanceCriteria.version`".

    A `spec` that can be edited in place makes that requirement unenforceable, so the seal
    is in the database, not in the repository function.
    """
    row = ev_acc.publish_criteria(
        gate_db,
        document=DOC,
        authored_by="clinical-council@medicalos.example",
        tenant_id=DEFAULT_TENANT_ID,
        known_strata_fields=KNOWN_STRATA,
    )
    gate_db.commit()

    with pytest.raises(psycopg.DatabaseError) as exc:
        gate_db.execute(
            "UPDATE acceptance_criteria SET spec = '{}'::jsonb WHERE id = %s", (row.id,)
        )
    assert exc.value.sqlstate in ("MOS05", "MOS06"), exc.value.sqlstate
    gate_db.rollback()

    with pytest.raises(psycopg.DatabaseError) as exc:
        gate_db.execute("DELETE FROM acceptance_criteria WHERE id = %s", (row.id,))
    assert exc.value.sqlstate == "MOS05"
    gate_db.rollback()

    # The two mutable columns still work: a criteria version must be approvable.
    gate_db.execute(
        "UPDATE acceptance_criteria SET approved_by = %s, approved_at = now() "
        "WHERE id = %s",
        (uuid.UUID(OPERATOR), row.id),
    )
    gate_db.commit()


def test_a_regression_criterion_needs_a_justified_margin(gate_db) -> None:
    """MOS-EVID-087, as a CHECK: delta is a clinical judgement, and an unjustified one
    makes "MUST NOT be tuned to make a specific candidate pass" unauditable."""
    with pytest.raises(psycopg.errors.CheckViolation):
        gate_db.execute(
            "INSERT INTO acceptance_criteria "
            "(tenant_id, capability_id, version, spec, authored_by) "
            "VALUES (%s, %s, 9, %s::jsonb, 'someone')",
            (
                uuid.UUID(DEFAULT_TENANT_ID),
                CAPABILITY,
                '{"regression": [{"id": "x", "margin": 0.02}]}',
            ),
        )
    gate_db.rollback()


def test_a_training_cohort_cannot_be_bound_for_acceptance(gate_db) -> None:
    """MOS-EVID-082, enforced by the database because it is a cross-table rule.

    Measuring a model on the cohort it was fitted to is the single most common way a
    published number becomes meaningless, and a rule living only in application code is
    one forgotten call site away from nothing.
    """
    c = _Cohort(gate_db)
    gate_db.execute(
        "UPDATE datasets SET purpose = 'training' WHERE id = %s",
        (uuid.UUID(c.dataset.id),),
    )
    gate_db.commit()

    with pytest.raises(psycopg.DatabaseError) as exc:
        ev_acc.bind_tenant_acceptance(
            gate_db,
            tenant_id=DEFAULT_TENANT_ID,
            capability_id=CAPABILITY,
            criteria_version=3,
            dataset_version_id=c.version.id,
            split_id=c.split.id,
            annotation_set_id=c.annotations.id,
            bound_by=OPERATOR,
        )
    assert exc.value.sqlstate == "MOS05"
    assert "MOS-EVID-082" in str(exc.value)
    gate_db.rollback()


# =====================================================================================
# 10. THE GATE CHECK.  Section 15.1.2, 0.2.0 row: `deployment-gate`.
# =====================================================================================
def _slot(gate_db, c: _Cohort) -> dict[str, Any]:
    """An incumbent live in `clinical` and a verified candidate standing by."""
    ev_acc.publish_criteria(
        gate_db,
        document=DOC,
        authored_by="clinical-council@medicalos.example",
        tenant_id=DEFAULT_TENANT_ID,
        known_strata_fields=KNOWN_STRATA,
    )
    ev_acc.bind_tenant_acceptance(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        capability_id=CAPABILITY,
        criteria_version=3,
        dataset_version_id=c.version.id,
        split_id=c.split.id,
        annotation_set_id=c.annotations.id,
        bound_by=OPERATOR,
    )

    incumbent_run = _evaluation_run(gate_db, c, model_version="2.1.0")
    candidate_run = _evaluation_run(gate_db, c, model_version="2.2.0")

    live = ev_dep.create_deployment(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        environment="production",
        capability_id=CAPABILITY,
        subject=INCUMBENT,
        created_by=OPERATOR,
        role="ACTIVE",
        state="SERVING",
        verification_ref="ver_incumbent",
    )
    # Put the incumbent in clinical mode the way the database requires: an approver, a
    # run and a report. MOS-STORE-261's clinical gate is checked on every write.
    incumbent_report_id = uuid.uuid4()
    gate_db.execute(
        "UPDATE deployments SET clinical_use_mode = 'clinical', approved_by = %s, "
        "approved_at = now(), acceptance_run_id = %s, validation_report_id = %s "
        "WHERE id = %s",
        (uuid.UUID(OPERATOR), incumbent_run, incumbent_report_id, uuid.UUID(live.id)),
    )
    gate_db.commit()

    candidate = ev_dep.create_deployment(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        environment="production",
        capability_id=CAPABILITY,
        subject=CANDIDATE,
        created_by=OPERATOR,
        role="STANDBY",
        state="SERVING",
        verification_ref="ver_candidate",
    )
    gate_db.commit()
    return {
        "live": live,
        "candidate": candidate,
        "incumbent_run": incumbent_run,
        "candidate_run": candidate_run,
        "incumbent_report_id": str(incumbent_report_id),
    }


def _views(slot: dict[str, Any], base, regressed) -> tuple[Any, Any, Any]:
    incumbent_run = run_view(base, run_id=slot["incumbent_run"])
    candidate_run = run_view(regressed, run_id=slot["candidate_run"])
    incumbent = report_view(
        incumbent_run, INCUMBENT, report_id=slot["incumbent_report_id"]
    )
    candidate = report_view(candidate_run, CANDIDATE)
    return incumbent, candidate, binding_for(candidate_run)


def test_regressed_model_version_is_refused_and_incumbent_stays_live(gate_db) -> None:
    """The 0.2.0 `deployment-gate` check, end to end.

    "a deliberately regressed `ModelVersion` is refused promotion against a frozen split
    and the incumbent stays live" (section 15.1.2), which is MOS-EVID-092: "the gate MUST
    NOT take the capability out of service to block a candidate".

    The regression is a stratum collapse, not a cohort-wide drop, because that is the one
    the naive comparison waves through.
    """
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)

    base = cohort(n_positive=30, n_small=6, n_empty=10)
    regressed = perturb(
        base,
        lambda r: r.value - 0.30 if r.strata["reference_volume_ml"] < 100.0 else r.value,
    )
    incumbent, candidate, binding = _views(slot, base, regressed)

    before = ev_dep.active_deployment(
        gate_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id=CAPABILITY,
    )
    assert before is not None and before.subject == INCUMBENT and before.live

    with pytest.raises(ev_dep.PromotionRefused) as exc:
        ev_dep.promote(
            gate_db,
            tenant_id=DEFAULT_TENANT_ID,
            deployment_id=slot["candidate"].id,
            candidate_report=candidate,
            incumbent_report=incumbent,
            criteria_document=DOC,
            binding=binding,
            decided_by=OPERATOR,
            requested_clinical_use_mode="clinical",
            now=NOW,
        )
    problem = exc.value.as_problem()
    assert problem["class"] == "clinical_rejection"   # not a transport failure
    assert problem["verdict"] == "FAIL"
    assert "ni_dice_small" in exc.value.check_ids

    # MOS-EVID-092: the incumbent is untouched, still ACTIVE, still SERVING, still
    # clinical. Compared field for field rather than "is something serving".
    after = ev_dep.active_deployment(
        gate_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id=CAPABILITY,
    )
    assert after == before

    # The candidate did not move either.
    still_standby = ev_dep.load_deployment(
        gate_db, tenant_id=DEFAULT_TENANT_ID, deployment_id=slot["candidate"].id
    )
    assert still_standby.role == "STANDBY"
    assert still_standby.clinical_use_mode == "research_only"

    # MOS-EVID-091: the refusal is PERSISTED, whatever the outcome -- and it survived the
    # exception, which is the case that matters.
    decision = gate_db.execute(
        "SELECT public_id, verdict, applied, criterion_results, "
        "       requested_clinical_use_mode "
        "  FROM deployment_gate_decisions WHERE deployment_id = %s",
        (uuid.UUID(slot["candidate"].id),),
    ).fetchall()
    assert len(decision) == 1
    assert decision[0]["verdict"] == "FAIL"
    assert decision[0]["applied"] is False
    assert decision[0]["requested_clinical_use_mode"] == "clinical"
    assert decision[0]["public_id"].startswith("gd_")
    # MOS-EVID-113 / MOS-STORE-302b: one entry per criterion, so "why was this refused"
    # is answerable from the row.
    ids = {e["id"] for e in decision[0]["criterion_results"]}
    assert ids == {
        "dice_positive", "fp_on_negatives",
        "ni_dice_positive", "ni_dice_small", "no_catastrophic",
    }


def test_an_equivalent_candidate_is_promoted_and_the_slot_swaps(gate_db) -> None:
    """The other direction. MOS-REG-077: cutover is a single transaction swapping the two
    `role` values, and it neither deletes nor re-verifies either row."""
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)

    rng = random.Random(11)
    base = cohort()
    rebuilt = perturb(base, lambda r: r.value + rng.gauss(0.0, 0.01))
    incumbent, candidate, binding = _views(slot, base, rebuilt)

    decision = ev_dep.promote(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        deployment_id=slot["candidate"].id,
        candidate_report=candidate,
        incumbent_report=incumbent,
        criteria_document=DOC,
        binding=binding,
        decided_by=OPERATOR,
        requested_clinical_use_mode="clinical",
        now=NOW,
    )
    assert decision.verdict == "PASS"
    assert decision.applied is True

    live = ev_dep.active_deployment(
        gate_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id=CAPABILITY,
    )
    assert live.subject == CANDIDATE
    assert live.clinical_use_mode == "clinical"
    assert live.id == slot["candidate"].id

    # The incumbent was demoted, not deleted or retired: MOS-REG-078 requires a rollback
    # that does not re-run VERIFYING, which needs the row warm and SERVING.
    old = ev_dep.load_deployment(
        gate_db, tenant_id=DEFAULT_TENANT_ID, deployment_id=slot["live"].id
    )
    assert old.role == "STANDBY"
    assert old.state == "SERVING"
    assert old.verification_ref == "ver_incumbent"


def test_the_gate_is_actually_wired_with_no_report(gate_db) -> None:
    """Chapter 7 acceptance check 23.

    "Attempt a Deployment transition to `clinical_use_mode: clinical` for a ServiceVersion
    with no ValidationReport. Expect refusal and a `deployment_gate_decisions` row with
    verdict FAIL, reason `signature_invalid` or `no_report`. Assert the incumbent remains
    serving throughout."
    """
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)
    binding = binding_for(run_view(cohort(), run_id=slot["candidate_run"]))

    with pytest.raises(ev_dep.PromotionRefused):
        ev_dep.promote(
            gate_db,
            tenant_id=DEFAULT_TENANT_ID,
            deployment_id=slot["candidate"].id,
            candidate_report=None,
            incumbent_report=None,
            criteria_document=DOC,
            binding=binding,
            decided_by=OPERATOR,
            requested_clinical_use_mode="clinical",
            now=NOW,
        )

    row = gate_db.execute(
        "SELECT verdict, candidate_report_id, criterion_results FROM "
        "deployment_gate_decisions WHERE deployment_id = %s",
        (uuid.UUID(slot["candidate"].id),),
    ).fetchone()
    assert row["verdict"] == "FAIL"
    # Divergence 2 of migration 0008: MOS-EVID-091's field table says NOT NULL, and this
    # check requires a persisted decision about an ABSENT report. The column is nullable
    # and constrained so NULL is reachable only on a FAIL.
    assert row["candidate_report_id"] is None
    assert row["criterion_results"][0]["reason"] == "no_report"

    live = ev_dep.active_deployment(
        gate_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id=CAPABILITY,
    )
    assert live.subject == INCUMBENT and live.live


def test_fail_is_not_overridable_and_indeterminate_is(gate_db) -> None:
    """Chapter 7 acceptance check 24, MOS-EVID-093.

    "With a user holding `deployment.gate.override`, attempt to override a FAIL on a
    `clinical` deployment. Expect refusal. Repeat for INDETERMINATE. Expect acceptance
    with a persisted rationale and approver."
    """
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)
    base = cohort(n_positive=30, n_small=6, n_empty=10)
    regressed = perturb(
        base,
        lambda r: r.value - 0.30 if r.strata["reference_volume_ml"] < 100.0 else r.value,
    )
    incumbent, candidate, binding = _views(slot, base, regressed)
    override = {"rationale": "the site accepts the risk", "approver": "Dr Novakova"}

    # A FAIL: refused for any role, including one holding the permission.
    with pytest.raises(ev_dep.OverrideNotPermitted, match="MOS-EVID-093"):
        ev_dep.promote(
            gate_db,
            tenant_id=DEFAULT_TENANT_ID,
            deployment_id=slot["candidate"].id,
            candidate_report=candidate,
            incumbent_report=incumbent,
            criteria_document=DOC,
            binding=binding,
            decided_by=OPERATOR,
            requested_clinical_use_mode="clinical",
            permissions=[ev_dep.OVERRIDE_PERMISSION],
            override=override,
            now=NOW,
        )

    # An INDETERMINATE: overridable, but ONLY with the permission, a rationale and an
    # approver -- and the override is reproduced on the decision row.
    doc = criteria_document()
    for criterion in doc["spec"]["absolute"] + doc["spec"]["regression"]:
        criterion["min_patients"] = 500
    incumbent2, candidate2, binding2 = _views(slot, base, base)

    with pytest.raises(ev_dep.OverrideNotPermitted, match="deployment.gate.override"):
        ev_dep.promote(
            gate_db,
            tenant_id=DEFAULT_TENANT_ID,
            deployment_id=slot["candidate"].id,
            candidate_report=candidate2,
            incumbent_report=incumbent2,
            criteria_document=doc,
            binding=binding2,
            decided_by=OPERATOR,
            requested_clinical_use_mode="clinical",
            permissions=[],
            override=override,
            now=NOW,
        )

    decision = ev_dep.promote(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        deployment_id=slot["candidate"].id,
        candidate_report=candidate2,
        incumbent_report=incumbent2,
        criteria_document=doc,
        binding=binding2,
        decided_by=OPERATOR,
        requested_clinical_use_mode="clinical",
        permissions=[ev_dep.OVERRIDE_PERMISSION],
        override=override,
        now=NOW,
    )
    assert decision.verdict == "INDETERMINATE"
    assert decision.applied is True

    row = gate_db.execute(
        "SELECT override, verdict FROM deployment_gate_decisions "
        "WHERE id = %s", (uuid.UUID(decision.id),)
    ).fetchone()
    assert row["override"]["rationale"] == "the site accepts the risk"
    assert row["override"]["approver"] == "Dr Novakova"


def test_the_database_refuses_an_override_of_a_fail(gate_db) -> None:
    """MOS-EVID-093 as a CHECK and not only as a code path: the one call site that skips
    the service-layer guard is the one that matters."""
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)
    with pytest.raises(psycopg.errors.CheckViolation):
        gate_db.execute(
            "INSERT INTO deployment_gate_decisions "
            "(public_id, tenant_id, deployment_id, capability_id, criteria_version, "
            " verdict, criterion_results, requested_clinical_use_mode, decided_by, "
            " override) "
            "VALUES (%s, %s, %s, %s, 3, 'FAIL', '[{\"id\":\"x\"}]'::jsonb, 'clinical', "
            "        %s, %s::jsonb)",
            (
                "gd_01JB4Q8T5XN7M2VDKC3PZR9HAE",
                uuid.UUID(DEFAULT_TENANT_ID),
                uuid.UUID(slot["candidate"].id),
                CAPABILITY,
                uuid.UUID(OPERATOR),
                '{"rationale": "because", "approver": "someone"}',
            ),
        )
    gate_db.rollback()


def test_gate_decisions_are_append_only(gate_db) -> None:
    """MOS-EVID-091 and MOS-STORE-302b: the record that the gate ran is not editable.

    A decision an operator can quietly rewrite does not answer "why was this deployment
    allowed"; it answers "what does somebody want the answer to be".
    """
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)
    base = cohort()
    incumbent, candidate, binding = _views(slot, base, base)
    decision = ev_dep.promote(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        deployment_id=slot["candidate"].id,
        candidate_report=candidate,
        incumbent_report=incumbent,
        criteria_document=DOC,
        binding=binding,
        decided_by=OPERATOR,
        requested_clinical_use_mode="research_only",
        now=NOW,
    )
    for statement in (
        "UPDATE deployment_gate_decisions SET verdict = 'PASS' WHERE id = %s",
        "DELETE FROM deployment_gate_decisions WHERE id = %s",
    ):
        with pytest.raises(psycopg.DatabaseError) as exc:
            gate_db.execute(statement, (uuid.UUID(decision.id),))
        assert exc.value.sqlstate == "MOS05"
        gate_db.rollback()


def test_research_only_records_the_gate_and_may_proceed(gate_db) -> None:
    """MOS-EVID-090's second branch: "A deployment in `research_only` mode MUST record the
    gate result but MAY proceed on FAIL, with the failure surfaced on the Deployment and
    in the UI."

    The two modes having different consequences for the same verdict is why
    `deployment_gate_decisions` carries `requested_clinical_use_mode`: without it the row
    cannot explain why one FAIL blocked and another did not.
    """
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)
    base = cohort(n_positive=30, n_small=6, n_empty=10)
    regressed = perturb(base, lambda r: r.value - 0.50)
    incumbent, candidate, binding = _views(slot, base, regressed)

    decision = ev_dep.promote(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        deployment_id=slot["candidate"].id,
        candidate_report=candidate,
        incumbent_report=incumbent,
        criteria_document=DOC,
        binding=binding,
        decided_by=OPERATOR,
        requested_clinical_use_mode="research_only",
        now=NOW,
    )
    assert decision.verdict == "FAIL"
    assert decision.applied is True

    live = ev_dep.load_deployment(
        gate_db, tenant_id=DEFAULT_TENANT_ID, deployment_id=slot["candidate"].id
    )
    assert live.clinical_use_mode == "research_only"
    assert live.role == "ACTIVE"


def test_a_deployment_is_created_research_only(gate_db) -> None:
    """MOS-SAFE-033: "A Deployment created without the field MUST be created as
    `research_only`", and there is no tenant-wide or environment-wide override.

    `create_deployment` takes no `clinical_use_mode` argument at all: the only route to
    `clinical` is `promote()`, which runs the gate. A keyword argument here would be a
    route around MOS-EVID-090 that no reviewer would notice.
    """
    row = ev_dep.create_deployment(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        environment="staging",
        capability_id=CAPABILITY,
        subject=CANDIDATE,
        created_by=OPERATOR,
    )
    gate_db.commit()
    assert row.clinical_use_mode == "research_only"
    assert row.role == "STANDBY"
    assert row.state == "PENDING"
    assert row.public_id.startswith("dep_")

    import inspect

    assert "clinical_use_mode" not in inspect.signature(
        ev_dep.create_deployment
    ).parameters


def test_a_clinical_deployment_needs_evidence_at_the_database_level(gate_db) -> None:
    """MOS-STORE-261: "the application cannot bypass it because the database checks it on
    every write". All four columns, one at a time."""
    row = ev_dep.create_deployment(
        gate_db,
        tenant_id=DEFAULT_TENANT_ID,
        environment="staging",
        capability_id=CAPABILITY,
        subject=CANDIDATE,
        created_by=OPERATOR,
    )
    gate_db.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        gate_db.execute(
            "UPDATE deployments SET clinical_use_mode = 'clinical' WHERE id = %s",
            (uuid.UUID(row.id),),
        )
    gate_db.rollback()


def test_two_live_actives_are_impossible(gate_db) -> None:
    """MOS-REG-074: "Two live actives is a configuration error, not a load-balancing
    strategy." This index is also what makes MOS-EVID-092 checkable."""
    c = _Cohort(gate_db)
    slot = _slot(gate_db, c)
    with pytest.raises(psycopg.errors.UniqueViolation):
        gate_db.execute(
            "UPDATE deployments SET role = 'ACTIVE' WHERE id = %s",
            (uuid.UUID(slot["candidate"].id),),
        )
    gate_db.rollback()
