# SPDX-License-Identifier: Apache-2.0
"""`leakage-check` -- the 0.2.0 gate check of docs/spec/15-delivery.md section 15.1.2.

    `leakage-check` (a patient present in two splits fails the build)

Section 15.1.3 puts patient-level `DatasetSplit` in 0.2.0's **Tier A**: MUST NOT be cut.
`MOS-EVID-034` runs L1-L5 at freeze time; `MOS-TRAIN-116` and `MOS-TRAIN-117` fix L1's
scope -- `patient_key` alone, all studies, all dates, with no relaxation available.

"FAILS THE BUILD" IS ASSERTED AS A WRITE THAT DID NOT HAPPEN
------------------------------------------------------------
The interesting failure is not "a function returned fail". It is a `dataset_splits` row
that exists anyway -- because the check ran in a different process, or ran after the
INSERT, or its result was logged and dropped. So the arm below counts the rows before and
after and requires the number to be unchanged, and reads back the manifest store to require
that no split manifest was written either. A refusal that leaves a half-built split behind
is how a cohort ends up sealed against a partition nobody can reproduce.

WHY THE SAME PATIENT IN TRAIN AND TEST IS THE ONE TO GATE ON
-------------------------------------------------------------
Of the twelve ways evidence can be wrong, this is the one that inflates every number at once
and is invisible in all of them: the model is scored on a patient it was fitted to, the
aggregate looks excellent, the confidence interval looks tight, and the stratified breakdown
looks uniform. Nothing in the report says anything is wrong. `MOS-TRAIN-117` therefore
allows no window, no date argument and no per-study relaxation, and the second arm here
asserts that the ABSENCE of those knobs is a property of the source rather than of the
current call site -- a `min_days_between_studies` parameter added in good faith next year
would silently turn this gate green on a leaking cohort.

Needs Postgres: a throwaway database created by `tests/gate/conftest.py::evidence_dsn`.
Nothing here touches the deployment's own schema.

Spec: MOS-EVID-028, MOS-EVID-029, MOS-EVID-031, MOS-EVID-034, MOS-EVID-036,
MOS-TRAIN-115, MOS-TRAIN-116, MOS-TRAIN-117, MOS-TRAIN-118, MOS-REL-004, MOS-REL-012.
"""

from __future__ import annotations

import ast
import secrets
from pathlib import Path
from typing import Any

import pytest
from medos.evidence import leakage as ev_leak
from medos.evidence import repo as ev_repo
from medos.sdk.refusal import FreezeRefused

from tests.gate._evidence import (
    BUCKET,
    OPERATOR,
    Cohort,
    annotation_entries,
    series_record,
)

pytestmark = pytest.mark.gate_0_2_0

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A cohort big enough to seal under `MOS-EVID-026`'s 30-patient floor for an acceptance
#: corpus, and small enough that the seal's stratification checks run in milliseconds.
N_PATIENTS = 32


def _sealed(conn: Any, store: Any) -> tuple[Any, list[Any]]:
    """A sealed DatasetVersion with NO split yet. The left-hand side of every arm below."""
    nonce = secrets.randbelow(10**9)
    records = [series_record(p, site=p % 2, nonce=nonce) for p in range(N_PATIENTS)]
    dataset = ev_repo.create_dataset(
        conn,
        slug=f"leak-cohort-{secrets.token_hex(4)}",
        display_name="leakage-check cohort",
        purpose="acceptance",
        custodian="TCIA/GATE",
        created_by=OPERATOR,
    )
    sealed = ev_repo.seal_dataset_version(
        conn,
        dataset_id=dataset.id,
        records=records,
        store=store,
        bucket=BUCKET,
        sealed_by=OPERATOR,
        deidentification_status="public_deidentified",
        deid_policy_id="ps315-basic/v4",
        uid_mapping_table_id="uidmap/v1",
    )
    conn.commit()
    return sealed, records


def _split_count(conn: Any) -> int:
    return conn.execute("SELECT count(*) AS n FROM dataset_splits").fetchone()["n"]


# ======================================================================================
# 1. THE CHECK. A patient on both sides, and no split row exists afterwards.
# ======================================================================================
def test_leakage_check_a_patient_in_two_partitions_fails_the_build(
    evidence_db: Any, manifest_store: Any
) -> None:
    """The 0.2.0 gate's `leakage-check`, asserted as a write that did not happen.

    One patient of thirty-two is assigned to `train` as well as to `test`. `MOS-EVID-029`
    makes `assignments` a sequence of PAIRS precisely so that this is representable: a
    mapping could not express it, and an argument type that silently repairs the defect
    repairs it in the worst possible place.

    The patient's key is not printed, here or in the refusal's message: it is a
    tenant-scoped HMAC and not a patient identifier, but the discipline holds anyway.
    """
    sealed, records = _sealed(evidence_db, manifest_store)
    keys = sorted({r.patient_key for r in records})
    leaked = keys[0]

    assignments = [(k, "test" if i % 2 else "train") for i, k in enumerate(keys)]
    assignments.append((leaked, "test"))  # ... and the same patient again, on the other side

    before_rows = _split_count(evidence_db)
    before_objects = len(manifest_store)

    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_split(
            evidence_db,
            dataset_version_id=sealed.id,
            name="holdout",
            assignments=assignments,
            store=manifest_store,
            bucket=BUCKET,
            frozen_by=OPERATOR,
            assignment_method="deliberately leaking, for the release gate",
            records=records,
        )
    evidence_db.rollback()

    # The refusal names L1 and says what it is, in machine-readable form.
    assert "L1" in exc.value.check_ids
    refusal = next(r for r in exc.value.refusals if r.check_id == "L1")
    assert refusal.code == "patient_in_two_partitions"
    assert refusal.observed >= 1 and refusal.bound == 0

    # AND THE BUILD DID NOT HAPPEN. Neither the row nor the manifest object.
    assert _split_count(evidence_db) == before_rows, (
        "freeze_split refused and wrote a dataset_splits row anyway"
    )
    assert len(manifest_store) == before_objects, (
        "freeze_split refused and left a split manifest in the object store"
    )


def test_leakage_check_the_same_patient_across_years_is_still_a_leak(
    evidence_db: Any, manifest_store: Any
) -> None:
    """`MOS-TRAIN-117`: "all studies, all dates". No window makes it acceptable.

    The plausible-sounding mistake is a rule like "the same patient is fine if the studies
    are five years apart". It is not fine: the anatomy is the same anatomy, and a model
    fitted to the 2019 screening CT has seen the patient it is being scored on in 2024.
    L1 takes no date argument, so it cannot be told otherwise -- and this arm proves that
    by handing it a cohort whose two partitions differ by `study_year`.
    """
    sealed, records = _sealed(evidence_db, manifest_store)
    keys = sorted({r.patient_key for r in records})
    # Every record already carries a study_year; the cohort spans two of them by
    # construction (site 0 -> 2022, site 1 -> 2023).
    assert len({r.study_year for r in records}) > 1

    assignments = [(k, "train") for k in keys] + [(keys[3], "test")]
    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_split(
            evidence_db,
            dataset_version_id=sealed.id,
            name="by-year",
            assignments=assignments,
            store=manifest_store,
            bucket=BUCKET,
            frozen_by=OPERATOR,
            assignment_method="split by acquisition year, for the release gate",
            records=records,
        )
    evidence_db.rollback()
    assert "L1" in exc.value.check_ids


# ======================================================================================
# 2. The clean cohort still freezes. Without this, the check proves only that nothing
#    freezes.
# ======================================================================================
def test_leakage_check_a_disjoint_split_freezes_and_records_its_report(
    evidence_db: Any, manifest_store: Any
) -> None:
    """The control group, plus `MOS-EVID-036`: the report is stored ON the split row.

    A leakage result that lives in a log is a leakage result nobody can audit two years
    later, and a waiver that can be added after the freeze is not a waiver.
    """
    cohort = Cohort(evidence_db, manifest_store, n_patients=N_PATIENTS)
    evidence_db.commit()

    assert cohort.split.leakage_report["L1"] == "pass"
    for check in ("L2", "L3", "L5"):
        assert cohort.split.leakage_report[check] == "pass"

    row = evidence_db.execute(
        "SELECT leakage_report, manifest_digest FROM dataset_splits WHERE id = %s",
        (cohort.split.id,),
    ).fetchone()
    assert row["leakage_report"]["L1"] == "pass"
    assert row["manifest_digest"].startswith("sha256:")

    # The annotation set over the same cohort froze too, so the cohort this gate's other
    # checks bind to is a complete one.
    assert cohort.annotations.annotation_digest.startswith("sha256:")
    assert len(annotation_entries(cohort.records)) == N_PATIENTS


# ======================================================================================
# 3. The escape hatches do not exist, and cannot be added quietly.
# ======================================================================================
_FORBIDDEN_PARAMETERS = (
    "seed",                      # MOS-EVID-028: a split is materialised, never reseeded
    "min_days_between_studies",  # MOS-TRAIN-117: no window
    "study_level_split",         # MOS-TRAIN-117: the unit is the patient
    "allow_same_patient",        # MOS-TRAIN-117: there is no such permission
)


def test_leakage_check_has_no_parameter_that_would_permit_a_leak() -> None:
    """The four arguments `MOS-EVID-028` and `MOS-TRAIN-117` forbid, absent from the source.

    Parsed with `ast` over the two functions that define the split, rather than grepped:
    the words appear in prose in both modules -- they are named in order to be refused --
    and a check that a docstring can break is a check that gets weakened until it passes.

    This is the arm that survives a well-meaning future edit. Every behavioural assertion
    above would still pass on the day someone adds `allow_same_patient=False` "for
    symmetry"; the parameter's mere existence is what turns L1 from an invariant into a
    default.
    """
    for relative, function in (
        ("medos/medos/evidence/repo.py", "freeze_split"),
        ("medos/medos/evidence/leakage.py", "leakage_report"),
    ):
        path = REPO_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fn = next(
            n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == function
        )
        args = fn.args
        names = {
            a.arg
            for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
        }
        for forbidden in _FORBIDDEN_PARAMETERS:
            assert forbidden not in names, (
                f"{relative}:{function} accepts {forbidden!r}. MOS-TRAIN-117 and "
                f"MOS-EVID-028 make the patient-level split unconditional; a parameter "
                f"that can relax it is the relaxation."
            )
        assert args.vararg is None and args.kwarg is None, (
            f"{relative}:{function} takes **kwargs, so the forbidden parameters above "
            f"cannot be ruled out by inspecting its signature"
        )


def test_leakage_check_a_waiver_never_unblocks_training(evidence_db: Any) -> None:
    """`MOS-TRAIN-115`: L1, L2, L3 and L5 stay blocking for TRAINING even when waived.

    A waiver is an operational judgement about one evaluation cohort. It is never a
    statement that the model may be fitted to the test set, and the two are easy to
    conflate because both are spelled "we accepted the leakage". `waiver_blocks_training`
    is the function the 0.3.0 training run calls before the first batch loads; it lives
    with the checks so that the rule and the thing it constrains cannot drift apart, and
    it is asserted here rather than in 0.3.0 so that it exists before the caller does.
    """
    records = [series_record(p, site=p % 2, nonce=7) for p in range(4)]
    keys = sorted({r.patient_key for r in records})
    leaking = [(k, "train") for k in keys] + [(keys[0], "test")]

    plain = ev_leak.leakage_report(records, leaking)
    assert next(c for c in plain.checks if c.id == "L1").outcome == "fail"
    assert ev_leak.blocking_refusals(plain)

    waived = ev_leak.leakage_report(
        records,
        leaking,
        waivers=[
            {
                "check_id": "L1",
                "waived_by": "u1",
                "waived_at": "2026-01-01T00:00:00Z",
                "rationale": "accepted for this evaluation cohort only",
            }
        ],
    )
    assert next(c for c in waived.checks if c.id == "L1").outcome == "waived"
    assert "L1" in ev_leak.waiver_blocks_training(waived), (
        "a waived L1 stopped blocking training; MOS-TRAIN-115 says a waiver is never "
        "permission to fit a model to the cases it will be scored on"
    )
