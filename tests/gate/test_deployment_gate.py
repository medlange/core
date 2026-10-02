# SPDX-License-Identifier: Apache-2.0
"""`deployment-gate` -- the 0.2.0 gate check of docs/spec/15-delivery.md section 15.1.2.

    `deployment-gate` (a deliberately regressed `ModelVersion` is refused promotion
    against a frozen split and the incumbent stays live)

Section 15.1.3 puts the deployment gate in 0.2.0's **Tier A**: MUST NOT be cut under any
circumstance. `MOS-EVID-090` refuses a clinical promotion on FAIL and on INDETERMINATE;
`MOS-EVID-091` requires the decision persisted whatever the outcome; `MOS-EVID-092` is the
half of the sentence that is easiest to lose -- "the gate MUST NOT take the capability out
of service to block a candidate".

THE SECOND CLAUSE IS THE ONE THAT NEEDS A TEST
-----------------------------------------------
"Refused promotion" is a statement about a function that raised. "The incumbent stays live"
is a statement about what did NOT happen to a different row, and it is the one a plausible
implementation gets wrong: retire the candidate's slot, or demote the active while
evaluating, or wrap the whole thing in a transaction that leaves the capability with no
ACTIVE deployment for the duration. Any of those turns a refused upgrade into an outage,
and a site that has had one outage from a refused upgrade will insist the gate be
switchable. So this module reads the incumbent's row before and after and requires it to be
EQUAL -- field for field, not "something is still serving".

AND THE REFUSAL MUST OUTLIVE THE EXCEPTION
-------------------------------------------
`MOS-EVID-091`: the gate decision is persisted whether it passed or failed. The failure
case is the one that is easy to lose, because the natural implementation raises and the
natural transaction rolls back -- and a refusal nobody can find afterwards is a refusal that
gets re-litigated at the next release meeting from memory. The arm below reads
`deployment_gate_decisions` after the exception and requires the row, with one entry per
criterion (`MOS-EVID-113`, `MOS-STORE-302b`).

THE CONTROL GROUP IS NOT OPTIONAL. A gate that refuses everything satisfies clause one,
clause two and `MOS-EVID-091` all at once, so
`test_deployment_gate_an_equivalent_candidate_is_promoted_and_the_slot_swaps` runs the same
call with the same cohort and a candidate that is genuinely equivalent, and requires the
promotion to happen.

Needs Postgres: a throwaway database created by `tests/gate/conftest.py::evidence_dsn`.

Spec: MOS-EVID-080, MOS-EVID-086, MOS-EVID-090, MOS-EVID-091, MOS-EVID-092, MOS-EVID-093,
MOS-EVID-113, MOS-REG-074, MOS-REG-077, MOS-REG-078, MOS-STORE-261, MOS-STORE-302b,
MOS-REL-004, MOS-REL-012.
"""

from __future__ import annotations

import random
import uuid
from typing import Any

import psycopg
import pytest
from medos.db.tenancy import DEFAULT_TENANT_ID

from tests.gate._evidence import (
    CANDIDATE,
    CAPABILITY,
    CRITERION_IDS,
    DOC,
    INCUMBENT,
    NOW,
    OPERATOR,
    Cohort,
    case_rows_for,
    ev_dep,
    live_slot,
    paired_views,
    perturb,
)

pytestmark = pytest.mark.gate_0_2_0

ENVIRONMENT = "production"

#: 40 patients, the release brief's split size, and above `MOS-EVID-026`'s floor of 30 for
#: an `acceptance` cohort -- below which "no criterion in 7.8 can return anything but
#: INDETERMINATE", which would make every arm here prove the wrong thing.
N_PATIENTS = 40

#: The regression: the small-effusion stratum collapses. NOT a cohort-wide drop, because a
#: cohort-wide drop is the one even the naive comparison catches -- see
#: `tests/gate/test_non_inferiority.py`, which owns that argument in full.
STRATUM_LOSS = 0.25


@pytest.fixture()
def slot(
    evidence_db: psycopg.Connection[Any], manifest_store: Any
) -> tuple[Cohort, dict[str, Any]]:
    """A frozen 40-patient cohort, an incumbent live in clinical use, a candidate standing by.

    Everything is built through the repository rather than by INSERT, so the acceptance
    binding's three foreign keys point at rows that really went through the seal
    (MOS-EVID-080) and "against a frozen split" means a split that was frozen.
    """
    cohort = Cohort(evidence_db, manifest_store, n_patients=N_PATIENTS)
    evidence_db.commit()
    return cohort, live_slot(evidence_db, cohort)


def _views(
    cohort: Cohort, slot: dict[str, Any], incumbent_rows: Any, candidate_rows: Any
) -> tuple[Any, Any, Any]:
    return paired_views(
        incumbent_rows,
        candidate_rows,
        cohort=cohort,
        incumbent_run_id=slot["incumbent_run"],
        candidate_run_id=slot["candidate_run"],
        incumbent_report_id=slot["incumbent_report_id"],
    )


def _active(conn: Any) -> Any:
    return ev_dep.active_deployment(
        conn,
        tenant_id=DEFAULT_TENANT_ID,
        environment=ENVIRONMENT,
        capability_id=CAPABILITY,
    )


# ======================================================================================
# 1. THE CHECK. Refused, and the incumbent is untouched.
# ======================================================================================
def test_deployment_gate_a_regressed_model_version_is_refused_and_the_incumbent_stays_live(
    evidence_db: psycopg.Connection[Any], slot: tuple[Cohort, dict[str, Any]]
) -> None:
    """§15.1.2's `deployment-gate`, end to end through `medos.evidence.deployment.promote`.

    The subject is a `ModelVersion` and not a `ServiceVersion`, exactly as §15.1.2 writes
    it: `MOS-EVID-064` makes a backend conversion a new ModelVersion needing its own run,
    so the ModelVersion is the thing a site most often proposes to swap and the one whose
    refusal must not cost an outage.
    """
    cohort, live = slot
    incumbent_rows = case_rows_for(cohort.records)
    candidate_rows = perturb(
        incumbent_rows,
        lambda r: r.value - STRATUM_LOSS
        if r.strata["reference_volume_ml"] < 100.0
        else r.value,
    )
    incumbent, candidate, binding = _views(cohort, live, incumbent_rows, candidate_rows)

    before = _active(evidence_db)
    assert before is not None
    assert before.subject == INCUMBENT and before.live
    assert before.clinical_use_mode == "clinical"

    with pytest.raises(ev_dep.PromotionRefused) as exc:
        ev_dep.promote(
            evidence_db,
            tenant_id=DEFAULT_TENANT_ID,
            deployment_id=live["candidate"].id,
            candidate_report=candidate,
            incumbent_report=incumbent,
            criteria_document=DOC,
            binding=binding,
            decided_by=OPERATOR,
            requested_clinical_use_mode="clinical",
            now=NOW,
        )

    # The refusal is a CLINICAL REJECTION and not a transport failure -- chapter 10's RFC
    # 9457 `class` enum. A site that reads "500" retries; a site that reads
    # "clinical_rejection" reads the criterion ids.
    problem = exc.value.as_problem()
    assert problem["class"] == "clinical_rejection"
    assert problem["verdict"] == "FAIL"
    assert "ni_dice_small" in exc.value.check_ids

    # MOS-EVID-092. Field for field, not "something is still serving".
    after = _active(evidence_db)
    assert after == before, (
        "the incumbent's row changed while a candidate was being refused; "
        f"{before} -> {after}"
    )

    # The candidate did not move either: still STANDBY, still research_only.
    still_standby = ev_dep.load_deployment(
        evidence_db, tenant_id=DEFAULT_TENANT_ID, deployment_id=live["candidate"].id
    )
    assert still_standby.role == "STANDBY"
    assert still_standby.clinical_use_mode == "research_only"


def test_deployment_gate_the_refusal_survives_the_exception(
    evidence_db: psycopg.Connection[Any], slot: tuple[Cohort, dict[str, Any]]
) -> None:
    """`MOS-EVID-091`: the decision is persisted whatever the outcome.

    The natural implementation raises and the natural transaction rolls back, which loses
    exactly the record a regulator, an auditor or next quarter's release meeting would ask
    for. `MOS-STORE-302b` makes `criterion_results` the row that answers "why was this
    refused", so a single line reading `verdict: FAIL` would not be enough and the entry
    set is asserted rather than the count.
    """
    cohort, live = slot
    incumbent_rows = case_rows_for(cohort.records)
    candidate_rows = perturb(
        incumbent_rows,
        lambda r: r.value - STRATUM_LOSS
        if r.strata["reference_volume_ml"] < 100.0
        else r.value,
    )
    incumbent, candidate, binding = _views(cohort, live, incumbent_rows, candidate_rows)

    with pytest.raises(ev_dep.PromotionRefused):
        ev_dep.promote(
            evidence_db,
            tenant_id=DEFAULT_TENANT_ID,
            deployment_id=live["candidate"].id,
            candidate_report=candidate,
            incumbent_report=incumbent,
            criteria_document=DOC,
            binding=binding,
            decided_by=OPERATOR,
            requested_clinical_use_mode="clinical",
            now=NOW,
        )

    rows = evidence_db.execute(
        "SELECT public_id, verdict, applied, criterion_results, "
        "       requested_clinical_use_mode "
        "  FROM deployment_gate_decisions WHERE deployment_id = %s",
        (uuid.UUID(live["candidate"].id),),
    ).fetchall()
    assert len(rows) == 1, f"{len(rows)} gate decisions recorded for one refusal"
    row = rows[0]
    assert row["verdict"] == "FAIL"
    assert row["applied"] is False
    assert row["requested_clinical_use_mode"] == "clinical"
    assert row["public_id"].startswith("gd_")
    assert {entry["id"] for entry in row["criterion_results"]} == set(CRITERION_IDS)


# ======================================================================================
# 2. THE CONTROL GROUP. An equivalent candidate IS promoted, and the slot swaps.
# ======================================================================================
def test_deployment_gate_an_equivalent_candidate_is_promoted_and_the_slot_swaps(
    evidence_db: psycopg.Connection[Any], slot: tuple[Cohort, dict[str, Any]]
) -> None:
    """Without this, every arm above is satisfied by a gate that refuses everything.

    `MOS-REG-077`: cutover is a single transaction swapping the two `role` values, and it
    neither deletes nor re-verifies either row. `MOS-REG-078` is why the demoted incumbent
    must stay `SERVING`: a rollback that had to re-run VERIFYING is a rollback nobody can
    perform at 02:00, which is the only hour anyone ever needs one.
    """
    cohort, live = slot
    rng = random.Random(11)
    incumbent_rows = case_rows_for(cohort.records)
    rebuilt = perturb(incumbent_rows, lambda r: r.value + rng.gauss(0.0, 0.01))
    incumbent, candidate, binding = _views(cohort, live, incumbent_rows, rebuilt)

    decision = ev_dep.promote(
        evidence_db,
        tenant_id=DEFAULT_TENANT_ID,
        deployment_id=live["candidate"].id,
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

    now_live = _active(evidence_db)
    assert now_live.subject == CANDIDATE
    assert now_live.id == live["candidate"].id
    assert now_live.clinical_use_mode == "clinical"

    # Demoted, not deleted and not retired.
    old = ev_dep.load_deployment(
        evidence_db, tenant_id=DEFAULT_TENANT_ID, deployment_id=live["live"].id
    )
    assert old.role == "STANDBY"
    assert old.state == "SERVING"
    assert old.verification_ref == "ver_incumbent"

    # MOS-EVID-091 again, from the other side: the PASS is recorded too, so the audit
    # trail is not a list of refusals with silence where the promotions were.
    recorded = evidence_db.execute(
        "SELECT verdict, applied FROM deployment_gate_decisions WHERE deployment_id = %s",
        (uuid.UUID(live["candidate"].id),),
    ).fetchall()
    assert [(r["verdict"], r["applied"]) for r in recorded] == [("PASS", True)]


# ======================================================================================
# 3. The two ways a refusal could be undone anyway.
# ======================================================================================
def test_deployment_gate_a_fail_cannot_be_overridden_even_by_direct_sql(
    evidence_db: psycopg.Connection[Any], slot: tuple[Cohort, dict[str, Any]]
) -> None:
    """`MOS-EVID-093`: an override applies to INDETERMINATE, never to FAIL.

    The distinction is the whole of chapter 7 acceptance check 22: INDETERMINATE means
    "we do not have enough evidence", which a named human may accept responsibility for;
    FAIL means "the evidence says no", which is not a risk anyone is entitled to accept on
    a patient's behalf.

    Asserted at the DATABASE, through the one call site that skips the service layer,
    because that is the call site that matters -- a guard only in Python is a guard that an
    incident-time `psql` session walks straight past.
    """
    _cohort, live = slot
    with pytest.raises(psycopg.errors.CheckViolation):
        evidence_db.execute(
            "INSERT INTO deployment_gate_decisions "
            "(public_id, tenant_id, deployment_id, capability_id, criteria_version, "
            " verdict, criterion_results, requested_clinical_use_mode, decided_by, "
            " override) "
            "VALUES (%s, %s, %s, %s, 3, 'FAIL', '[{\"id\":\"ni_dice_small\"}]'::jsonb, "
            "        'clinical', %s, %s::jsonb)",
            (
                "gd_01JB4Q8T5XN7M2VDKC3PZR9HAE",
                uuid.UUID(DEFAULT_TENANT_ID),
                uuid.UUID(live["candidate"].id),
                CAPABILITY,
                uuid.UUID(OPERATOR),
                '{"rationale": "the site accepts the risk", "approver": "someone"}',
            ),
        )
    evidence_db.rollback()


def test_deployment_gate_cannot_leave_the_capability_with_two_actives(
    evidence_db: psycopg.Connection[Any], slot: tuple[Cohort, dict[str, Any]]
) -> None:
    """`MOS-REG-074`: "Two live actives is a configuration error, not a load-balancing
    strategy."

    This index is also what makes `MOS-EVID-092` checkable at all. "The incumbent stays
    live" is only meaningful if "live" is a slot with exactly one occupant; without the
    partial unique index, a promotion that half-applied would leave two ACTIVE rows and
    the assertion above would be reading whichever one the query returned first.
    """
    _cohort, live = slot
    with pytest.raises(psycopg.errors.UniqueViolation):
        evidence_db.execute(
            "UPDATE deployments SET role = 'ACTIVE' WHERE id = %s",
            (uuid.UUID(live["candidate"].id),),
        )
    evidence_db.rollback()

    # And exactly one ACTIVE remains, which is the incumbent.
    still = _active(evidence_db)
    assert still is not None and still.subject == INCUMBENT
