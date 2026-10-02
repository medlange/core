# SPDX-License-Identifier: Apache-2.0
"""`leakage-blocks-training` -- §15.1.2's 0.3.0 row via chapter 17's `MOS-TRAIN-193`.

    "a cohort with a patient across two splits cannot start a training run"

and chapter 17's acceptance criterion 1 is the executable form:

    "Assert the freeze-time L1 of `MOS-EVID-034` reports `fail`; then attach a
     `MOS-EVID-036` waiver to it and submit a `TrainingRun` against that split. Assert the
     run is refused **before the first batch is loaded**, with the violating `patient_key`
     and both partitions named, and that the waiver did not permit it."

BLOCKS, NOT REPORTS. THAT IS THE WHOLE CHECK.
-----------------------------------------------
`MOS-EVID-036`'s waiver is legitimate and this check does not dispute it: a waiver is a
statement about what a REPORT may claim, taken by a named person with a rationale. What it
is not is permission to fit a model on the data. A leakage check that produces a report an
operator can acknowledge is a leakage check that gets acknowledged, and the model is
trained anyway. So the split freezes, the waiver attaches, and `tr.submit` still refuses --
that sequence, in that order, is the check.

"BEFORE THE FIRST BATCH" IS ASSERTED AS A ROW COUNT
-----------------------------------------------------
There is no batch loader to instrument in this deployment, so the observable is stronger
and cheaper: after the refusal, `training_runs` holds zero rows. A refusal that happened
after a row existed would be a cancellation, and a cancelled run has consumed the split.

WHICH CHECK IS PROVOKED, AND A SPEC CONTRADICTION REPORTED RATHER THAN PATCHED
--------------------------------------------------------------------------------
§15.1.2 words this as "a patient across two SPLITS". The mechanism the chapters define
(`MOS-EVID-034` L1, `MOS-TRAIN-116`/`117`) is patient disjointness across PARTITIONS WITHIN
ONE split -- `freeze_split` evaluates L1 over the `(patient_key, partition)` pairs of a
single `DatasetSplit` and has no view of any other split. That divergence is already
recorded as item 59 of `docs/spec/99-known-inconsistencies.md`, and the partition reading is
the only one the code can enforce.

There is a second, sharper one, and it is the reason the leak below is an L3 and not an L1.
`MOS-STORE-293` makes `dataset_split_members` `PRIMARY KEY (split_id, patient_key)`, so one
patient in two partitions of one split is STRUCTURALLY IMPOSSIBLE -- the schema refuses the
row before any checker sees it. L1 therefore cannot be made to fail through the freeze path
at all. That is the schema doing its job, and it means criterion 1's literal fixture cannot
be built. What CAN be built is L3: two DISTINCT patients sharing a `series_pixel_digest`
across the partition boundary, which is the same clinical defect (the same pixels are in
train and in tune) arriving through a door the primary key does not close.

`MOS-TRAIN-115` treats L1, L2, L3 and L5 identically, so the blocking claim is unaffected --
and `test_..._every_failing_check_blocks_identically` asserts that equivalence rather than
relying on it. Both facts are reported, neither is patched.

Needs a schema.

Spec: MOS-EVID-034, MOS-EVID-036, MOS-TRAIN-115, MOS-TRAIN-116, MOS-TRAIN-117,
MOS-TRAIN-193, MOS-STORE-293, MOS-REL-004; chapter 17 acceptance criteria 1 and 2.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
from medos.sdk.errors import RunRefused
from medos.training import runs as tr

from tests.gate import _platform as P

pytestmark = pytest.mark.gate_0_3_0

#: `MOS-EVID-036`'s waiver, complete. The rationale is a real one -- a phantom scanned
#: twice is the canonical legitimate L3 -- because a waiver a reviewer would reject on
#: sight is not the waiver this check needs to defeat.
WAIVER: dict[str, Any] = {
    "check_id": "L3",
    "waived_by": P.OPERATOR,
    "waived_at": "2026-03-11T00:00:00Z",
    "rationale": "two acquisitions of one phantom; retained deliberately",
    "affected_pairs": [],
}


def _submit(conn: Any, cohort: P.Cohort, **overrides: Any) -> Any:
    return tr.submit(
        conn,
        binding=P.run_binding(cohort, **overrides),
        actor=P.SERVICE_ACTOR,
        trace_id=P.TRACE,
        leakage=cohort.leakage,
        output_kind="label",
    )


def _run_rows(conn: Any) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM training_runs").fetchone()["n"])


# =====================================================================================
# THE CHECK
# =====================================================================================
@pytest.mark.parametrize(
    "leak_partitions", [("train", "tune"), ("tune", "test"), ("train", "test")]
)
def test_leakage_blocks_training_a_leak_across_any_partition_pair_refuses_the_run(
    platform_db: Any, manifest_store: Any, leak_partitions: tuple[str, str]
) -> None:
    """Criterion 1, including its "repeat with the leak across train/tune and tune/test".

    Three pairs and not two: `train`/`test` is the one an operator would call obvious, and
    a check that only exercised the subtle pairs would not notice a guard that special-
    cased it.
    """
    leaky = P.prepare_cohort(
        platform_db,
        manifest_store,
        shared_pixels=(0, 1),
        leak_partitions=leak_partitions,
        waivers=(WAIVER,),
    )
    assert "L3" in leaky.leakage, (
        f"the frozen split carries no L3 verdict at all ({sorted(leaky.leakage)}); the "
        f"fixture did not produce the leak it is built to produce"
    )
    with pytest.raises(RunRefused) as exc:
        _submit(platform_db, leaky)

    refusal = exc.value.refusals[0]
    assert refusal.check_id == "MOS-TRAIN-115", refusal
    assert refusal.code == "leakage_blocks_training", refusal
    assert "L3" in refusal.observed, (
        f"the refusal does not name the failing check: {refusal.observed!r}. An operator "
        f"cannot act on 'leakage' without being told which check and over what."
    )
    assert _run_rows(platform_db) == 0, (
        "a training_runs row exists after the refusal. MOS-TRAIN-115 requires the refusal "
        "BEFORE the first batch is loaded; a row that exists and was then refused is a "
        "cancelled run, and a cancelled run has already consumed the split."
    )
    platform_db.rollback()


def test_leakage_blocks_training_the_waiver_does_not_license_fitting_on_the_data(
    platform_db: Any, manifest_store: Any
) -> None:
    """The half of criterion 1 that names the waiver. `MOS-EVID-036` / `MOS-TRAIN-115`.

    The split FROZE with a failing check, which is correct -- a waiver is a statement
    about what a report may claim and freezing is a reporting act. Submitting a run
    against it is not, and the same waiver counts for nothing there.
    """
    leaky = P.prepare_cohort(
        platform_db, manifest_store, shared_pixels=(0, 1), waivers=(WAIVER,)
    )
    verdict = leaky.leakage["L3"]
    waived = verdict == "waived" or (
        isinstance(verdict, dict) and verdict.get("outcome") == "waived"
    )
    assert waived, (
        f"the waiver did not take effect at freeze time (L3 = {verdict!r}), so the run's "
        f"refusal below is not evidence that a waiver fails to license training -- it "
        f"would be evidence that the waiver was never applied"
    )
    with pytest.raises(RunRefused) as exc:
        _submit(platform_db, leaky)
    assert exc.value.refusals[0].code == "leakage_blocks_training"
    platform_db.rollback()


def test_leakage_blocks_training_five_years_between_the_studies_changes_nothing(
    platform_db: Any, manifest_store: Any
) -> None:
    """Criterion 1's last sentence, and criterion 2's subject. `MOS-TRAIN-117`.

    "Repeat with the two studies of the leaked patient dated five years apart and assert
    the outcome is unchanged." The temptation this closes is a time window: a patient
    imaged in 2019 and 2024 feels like two patients, and is not -- anatomy, implants and
    scanner-specific texture all persist.
    """
    leaky = P.prepare_cohort(
        platform_db,
        manifest_store,
        shared_pixels=(0, 1),
        study_years=(2019, 2024),
        waivers=(WAIVER,),
    )
    with pytest.raises(RunRefused) as exc:
        _submit(platform_db, leaky)
    assert exc.value.refusals[0].code == "leakage_blocks_training"
    assert _run_rows(platform_db) == 0
    platform_db.rollback()


def test_leakage_blocks_training_the_split_tooling_has_no_time_window_escape_hatch() -> None:
    """Criterion 2's second half, verbatim.

    "grep the split tooling for any configuration named `min_days_between_studies`,
    `study_level_split`, `allow_same_patient` or equivalent; expect zero hits."

    A configuration key is how the check above gets switched off by somebody who is sure
    their case is different, and it would never appear in a test run.
    """
    forbidden = {
        "min_days_between_studies",
        "study_level_split",
        "allow_same_patient",
        "patient_overlap_allowed",
        "leakage_tolerance",
        "skip_leakage",
    }
    hits: list[str] = []
    for root in (Path(P.ev_repo.__file__).parent, Path(tr.__file__).parent):
        for path in sorted(root.glob("*.py")):
            hits.extend(f"{path.name}:{n}" for n in _configuration_names(path, forbidden))
    assert not hits, (
        f"the split tooling carries a configuration that weakens patient-level "
        f"disjointness: {hits}. MOS-TRAIN-117 makes L1 patient-scoped with no window."
    )


def _configuration_names(path: Path, forbidden: set[str]) -> list[str]:
    """Forbidden names used as CONFIGURATION, read with `ast`.

    A textual grep cannot do this job, and getting it wrong in the safe direction is worse
    than not having the check: `medos/medos/evidence/repo.py`'s `freeze_split` docstring says in
    prose that there is no `min_days_between_studies`, no `study_level_split` and no
    `allow_same_patient` -- which a grep reports as three hits, three times, forever, until
    somebody deletes the check that keeps crying wolf.

    So a name counts when it is a parameter, a keyword argument, a binding, an attribute
    or a string literal that is not a docstring. Prose does not count, and a string key
    -- `config.get("allow_same_patient")`, the likeliest smuggling route -- does.
    """
    import ast as _ast

    tree = _ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in _ast.walk(tree):
        if isinstance(
            node, (_ast.Module, _ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)
        ):
            body = getattr(node, "body", None) or []
            if (
                body
                and isinstance(body[0], _ast.Expr)
                and isinstance(body[0].value, _ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    found: list[str] = []
    for node in _ast.walk(tree):
        name: str | None = None
        if isinstance(node, _ast.arg):
            name = node.arg
        elif isinstance(node, _ast.keyword):
            name = node.arg
        elif isinstance(node, _ast.Name):
            name = node.id
        elif isinstance(node, _ast.Attribute):
            name = node.attr
        elif (
            isinstance(node, _ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            name = node.value
        if name in forbidden:
            found.append(f"{getattr(node, 'lineno', '?')} {name}")
    return found


def test_leakage_blocks_training_a_missing_report_is_not_a_clean_one(
    platform_db: Any, manifest_store: Any
) -> None:
    """An optional check with a permissive default is not a check. `MOS-TRAIN-115`.

    The easiest way to defeat everything above is to submit with no leakage report at all.
    `tr.submit` must refuse that too, and refuse it DIFFERENTLY, so the two situations are
    distinguishable in an audit trail.
    """
    clean = P.prepare_cohort(platform_db, manifest_store)
    with pytest.raises(RunRefused) as exc:
        tr.submit(
            platform_db,
            binding=P.run_binding(clean),
            actor=P.SERVICE_ACTOR,
            trace_id=P.TRACE,
            leakage=None,
            output_kind="label",
        )
    assert exc.value.refusals[0].code == "leakage_report_not_supplied", exc.value.refusals
    assert _run_rows(platform_db) == 0
    platform_db.rollback()


def test_leakage_blocks_training_a_clean_cohort_is_accepted(
    platform_db: Any, manifest_store: Any
) -> None:
    """THE CONTROL. Without it, `tr.submit` could refuse everything and score six passes.

    Every other assertion in this module is "the run was refused". A submit path that
    raised `RunRefused` unconditionally -- a typo in a precondition, a schema the fixture
    no longer satisfies -- would satisfy all of them.
    """
    clean = P.prepare_cohort(platform_db, manifest_store)
    # L1, L2, L3 and L5 are the blocking set of MOS-TRAIN-115. L4 -- the perceptual-hash
    # near-duplicate check -- reports `skipped` on a manifest that carries no perceptual
    # hash, which this synthetic cohort does not, and `skipped` is its correct verdict
    # there rather than a silent pass: MOS-EVID-034 records what it could not compare.
    outcomes = {
        check: clean.leakage[check] for check in ("L1", "L2", "L3", "L4", "L5")
    }
    blocking = {k: v for k, v in outcomes.items() if k != "L4"}
    assert all(v == "pass" for v in blocking.values()), (
        f"the control cohort is not clean: {blocking}"
    )
    assert outcomes["L4"] in ("pass", "skipped"), outcomes["L4"]
    row = _submit(platform_db, clean)
    assert row is not None
    assert _run_rows(platform_db) == 1
    platform_db.rollback()


def test_leakage_blocks_training_every_failing_check_blocks_identically() -> None:
    """`MOS-TRAIN-115` treats L1, L2, L3 and L5 alike -- asserted, not assumed.

    The module docstring explains why the fixture provokes L3: `MOS-STORE-293`'s primary
    key makes an L1 fixture unbuildable. That substitution is only sound if the blocking
    rule does not discriminate between the checks, so the rule is read here. If it grew a
    per-check branch, the L3 evidence above would stop standing for L1 and this test says
    so.
    """
    source = inspect.getsource(tr._leakage_refusals)
    for check in ("L1", "L2", "L3", "L5"):
        assert check in source or "BLOCKING" in source.upper(), (
            f"medos.training.runs._leakage_refusals does not mention {check}; the set of "
            f"blocking checks cannot be read off it"
        )
    # No check may be handled by a branch of its own: that is how one of them acquires a
    # softer rule without anybody noticing.
    branches = [
        line.strip()
        for line in source.splitlines()
        if ("if " in line or "elif " in line)
        and any(f'"{c}"' in line or f"'{c}'" in line for c in ("L1", "L2", "L3", "L5"))
    ]
    assert not branches, (
        f"a per-check branch in the blocking rule: {branches}. MOS-TRAIN-115 blocks on "
        f"any failing check; a branch is where one of them stops blocking."
    )
