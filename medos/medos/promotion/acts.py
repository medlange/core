# SPDX-License-Identifier: Apache-2.0
"""Three human acts, five steps, one reverse role swap. `MOS-TRAIN-173` to `MOS-TRAIN-189`.

`MOS-TRAIN-173`: "Promotion of a candidate into service requires three distinct human
decisions. They MUST be three distinct permissions, three distinct audit records, and they
MUST NOT be collapsed into one action or one permission."

    A1  Issue the ValidationReport     evidence.report.issue    -> validation_reports.approver
    A2  Approve the artifact           artifact.approve         -> lifecycle_status = APPROVED
    A3  Promote to clinical use        deployment.approve_clinical -> clinical_use_mode

`MOS-TRAIN-175`: the three MAY be exercised by the same person where the tenant's
governance permits, "and the audit trail MUST record them as three separate acts with
three timestamps regardless". So each function here writes its own `AuditEvent` and none
of them writes two.

A1 IS NOT IN THIS MODULE, AND THAT IS NOT AN OMISSION
------------------------------------------------------
Issuing the report -- binding the named human approver, producing the DSSE envelope,
writing the `validation_reports` row -- is chapter 7's, and `medos.evidence.reports`
already owns it. `MOS-TRAIN-152`'s write-time check is the part this module contributes:
`assert_human()` is exported so the issuance path and the two acts here share ONE
definition of "not a human", rather than each deciding for itself.

WHY `assert_human` IS A WRITE-TIME CHECK AND NOT A REVIEW STEP
----------------------------------------------------------------
`MOS-TRAIN-152`: "A `ValidationReport` presented for issuance whose
`approver.identity_assurance` names a service account, an API key, a CI job identity or any
non-human principal MUST be rejected at write time (`MOS-EVID-117`). This check MUST live
in the write path, not in review: an automated approver is exactly the shape a
well-intentioned automation of the last mile takes."

THE FIVE STEPS, AND WHICH TWO ARE HUMAN
-----------------------------------------
`MOS-TRAIN-181`'s table is `PROMOTION_STEPS` below, as data. `MOS-TRAIN-182`: "Steps 1
through 3 MAY be executed by automation that an operator triggered. Steps 4 and 5 MUST each
require a distinct human action. In particular, a `PASS` from `evaluate_gate()` MUST NOT
trigger step 5, and no configuration value, feature flag or promotion policy MUST be able
to make it do so. The gate says the candidate is *permitted*; it does not say the candidate
should go *now*, and the difference between those two statements is where every remaining
piece of clinical judgement lives."

That is why `cutover()` takes an `actor` it checks with `assert_human()` and takes NO gate
result: it cannot be reached from a verdict, because it does not read one.

PROMOTION MINTS, NEVER MUTATES
-------------------------------
`MOS-TRAIN-180`: "Promotion MUST mint a **new** `Deployment` row. It MUST NOT update
`service_version_id` or any model reference on a row whose `state` is `SERVING`." Two
layers, as that requirement requires: 0013 revokes `UPDATE` on `deployments(subject_kind,
subject_id, subject_version)` from `medicalos_app`, and 0008's
`deployments_immutable_identity` trigger binds the owner as well. `cutover()` swaps `role`
between two existing rows and writes no subject column at all.

Spec: MOS-TRAIN-152, MOS-TRAIN-173 to MOS-TRAIN-189, MOS-REG-076 to MOS-REG-080,
MOS-EVID-090 to MOS-EVID-093, MOS-EVID-117, MOS-SAFE-036, MOS-SAFE-037, MOS-OPS-084.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from medos.db import audit
from medos.db.tenancy import tenant_tx
from medos.evidence.deployment import DeploymentRow, active_deployment, load_deployment
from medos.registry import repo as registry_repo

__all__ = [
    "DEFAULT_ROLLBACK_WINDOW_DAYS",
    "NON_HUMAN_ACTOR_KINDS",
    "PROMOTION_STEPS",
    "THREE_ACTS",
    "ApprovalRefused",
    "PromotionError",
    "ResidencyBudget",
    "approve_artifact",
    "approve_clinical_use",
    "assert_human",
    "assert_no_auto_promote",
    "assert_rollback_window_affordable",
    "create_candidate_deployment",
    "cutover",
    "rollback",
    "verify_candidate",
]


class PromotionError(RuntimeError):
    """Base. A promotion that cannot proceed, for a reason the operator must act on."""


class ApprovalRefused(PromotionError):
    """An act was attempted by a principal, or in a state, that MUST NOT perform it."""


#: `MOS-TRAIN-173`'s table, as data. Rendered by the operator surfaces so the three acts
#: are three rows in the UI as well as three rows in the audit trail.
THREE_ACTS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "A1",
        "act": "Issue the ValidationReport",
        "permission": "evidence.report.issue",
        "attests": "the evidence is sound, complete and correctly bound",
        "recorded_on": "validation_reports.approver (MOS-EVID-117) + DSSE envelope",
    },
    {
        "id": "A2",
        "act": "Approve the artifact",
        "permission": "artifact.approve",
        "attests": "this candidate may be deployed at all",
        "recorded_on": "artifacts.lifecycle_status = APPROVED (MOS-REG-021) + AuditEvent",
    },
    {
        "id": "A3",
        "act": "Promote to clinical use",
        "permission": "deployment.approve_clinical",
        "attests": "this deployment, in this environment, for this tenant, may see patients",
        "recorded_on": "deployments.clinical_use_mode (MOS-SAFE-036, MOS-SAFE-037)",
    },
)

#: `MOS-TRAIN-181`'s five steps, as data, with who may perform each.
PROMOTION_STEPS: Final[tuple[dict[str, Any], ...]] = (
    {"step": 1, "action": "Create the candidate Deployment at role=STANDBY, state=PENDING",
     "actor": "operator-triggered automation permitted", "human": False,
     "reference": "MOS-REG-076"},
    {"step": 2, "action": "PENDING -> VERIFYING -> SERVING: supply-chain verification, "
                          "then the worker golden-fixture self-test",
     "actor": "automatic", "human": False, "reference": "MOS-REG-075, MOS-IMG-054"},
    {"step": 3, "action": "evaluate_gate() against the tenant's acceptance binding",
     "actor": "operator-triggered automation permitted", "human": False,
     "reference": "MOS-EVID-090, MOS-EVID-091"},
    {"step": 4, "action": "For clinical_use_mode: clinical, the E1 gate with its named "
                          "approver",
     "actor": "human, A3", "human": True, "reference": "MOS-SAFE-036"},
    {"step": 5, "action": "Cutover: the single transaction swapping role between "
                          "incumbent and candidate",
     "actor": "human", "human": True, "reference": "MOS-REG-077"},
)

#: `MOS-TRAIN-174`: "No service account, API key, CI identity, scheduled task or any other
#: non-human principal MUST hold `evidence.report.issue`, `artifact.approve`,
#: `deployment.approve_clinical`, `deployment.promote` or `deployment.gate.override`."
#: The kinds are `medos.db.audit.ACTOR_KINDS`; these are the four that are not a person.
NON_HUMAN_ACTOR_KINDS: Final[frozenset[str]] = frozenset(
    {"service_account", "workload", "service_version", "platform_admin"}
)

#: `MOS-TRAIN-186`: "for a declared `rollback_window`, default 30 days".
DEFAULT_ROLLBACK_WINDOW_DAYS: Final[int] = 30


def assert_human(actor: audit.Actor, *, act: str) -> None:
    """`MOS-TRAIN-174` / `MOS-TRAIN-152`, at the write path. Raises `ApprovalRefused`.

    `platform_admin` is in the refused set even though a platform administrator is a
    person, because `MOS-TRAIN-072`'s phrasing carries here too: the platform-administrator
    identity is a ROLE a process can also hold, and an act whose audit row says
    `platform_admin` does not name the human who performed it. `MOS-EVID-117` wants a named
    approver, so the actor kind must be `user`.
    """
    if actor.kind == "user":
        return
    raise ApprovalRefused(
        f"{act} requires a named human. actor.kind is {actor.kind!r}, which is a "
        f"non-human principal ({sorted(NON_HUMAN_ACTOR_KINDS)}). MOS-TRAIN-174 makes this "
        "a query over the role-grant tables in CI and MOS-TRAIN-152 makes it a write-time "
        "check here: 'an automated approver is exactly the shape a well-intentioned "
        "automation of the last mile takes'."
    )


# =====================================================================================
# A2 -- approve the artifact.  MOS-TRAIN-179.
# =====================================================================================
def approve_artifact(
    conn: psycopg.Connection[Any],
    *,
    model_version_id: str,
    actor: audit.Actor,
    trace_id: str,
    approval_rationale: str,
    validation_report_id: str,
    validation_report_digest: str,
    gate_decision_id: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """`VALIDATED -> APPROVED`. One scalar id per call. `MOS-TRAIN-179`, `MOS-TRAIN-232`.

    "Approval is a statement about the **artifact**: it makes the candidate pass filter F3
    (`MOS-REG-055`) and does nothing else. It does not route a study, does not create a
    deployment and does not change what is serving."

    `MOS-TRAIN-232` is why the first parameter is a scalar and there is no plural sibling:
    "`artifact.approve` MUST accept exactly one `model_version_id` per call; the API MUST
    NOT expose a bulk-approval endpoint, a collection-valued approval body, an 'approve all
    passing' control, a saved filter that approves on match, or an automated promotion of
    the best of a sweep." The integration test asserts this signature takes a scalar.

    `MOS-TRAIN-179` requires `approved_by`, the rationale, the report and its digest and
    the gate-decision row to be RECORDED. `artifacts` has no column for any of them --
    chapter 12 section 12.9 declares none and this component creates no migration against
    another chapter's table -- so they are recorded in the `AuditEvent` detail and in
    `status_reason`, both of which `registry_changelog` reproduces verbatim under
    `MOS-REG-010`. Stated here rather than left to be discovered; see the component report.
    """
    assert_human(actor, act="artifact.approve (A2 of MOS-TRAIN-173)")
    if len(approval_rationale.strip()) < 20:
        raise ApprovalRefused(
            "MOS-TRAIN-179: approval records an approval_rationale of at least 20 "
            f"characters; got {len(approval_rationale.strip())}. The rationale is what a "
            "later reader has instead of the conversation that produced the decision"
        )
    if not validation_report_id or not validation_report_digest:
        raise ApprovalRefused(
            "MOS-TRAIN-179: approval records the validation_report_id and its digest. "
            "MOS-TRAIN-151 makes issuance (A1) the act BEFORE this one, so an approval "
            "with no report is an approval of evidence nobody signed"
        )
    if not gate_decision_id:
        raise ApprovalRefused(
            "MOS-TRAIN-179: approval records the deployment_gate_decisions row it relied "
            "on (MOS-EVID-091)"
        )

    reason = (
        f"APPROVED by {actor.id} (A2, MOS-TRAIN-173). report={validation_report_id} "
        f"digest={validation_report_digest} gate_decision={gate_decision_id}. "
        f"rationale: {approval_rationale.strip()}"
    )
    # ONE transaction. `MOS-SEC-149`: "Where the audited action occurs inside a database
    # transaction, the `AuditEvent` MUST be inserted in that same transaction. A committed
    # state change with no audit row MUST NOT be possible." `tenant_tx` is re-entrant, so
    # the registry's own scope becomes a savepoint under this one.
    with tenant_tx(conn):
        row = registry_repo.set_status(
            conn,
            model_version_id,
            to_status="APPROVED",
            reason=reason,
            actor=actor,
            trace_id=trace_id,
            request_id=request_id,
            kind="model_version",
        )
        # The registry writes `artifact.status.approved` for the lifecycle edge; this
        # second row is act A2 itself, carrying what MOS-TRAIN-179 requires recorded and
        # what no registry column has a place for.
        audit.record(
            conn,
            action="artifact.approve",
            action_class="governance",
            actor=actor,
            resource=audit.Resource(
                kind="model_version", id=row["public_id"], version=row["version"]
            ),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "act": "A2",
                "permission": "artifact.approve",
                "approved_by": actor.id,
                "approval_rationale": approval_rationale.strip(),
                "validation_report_id": validation_report_id,
                "validation_report_digest": validation_report_digest,
                "deployment_gate_decision_id": gate_decision_id,
                # MOS-TRAIN-179, stated on the record so a reader does not infer more:
                "scope": "the artifact only; approval routes no study and starts no "
                         "deployment",
            },
        )
    return row


# =====================================================================================
# MOS-TRAIN-186 -- the rollback window is a capacity question, answered before step 1
# =====================================================================================
@dataclass(frozen=True)
class ResidencyBudget:
    """What a node can hold, and what is already reserved on it. Chapter 13 section 13.10.3.

    A plain value object, supplied by the caller. `MOS-TRAIN-123` forbids the training side
    from requesting a reservation from the residency service, and the promotion side reads
    a budget rather than negotiating one for the same reason: the arithmetic belongs to
    chapter 13 and this module's job is to run it before step 1 rather than discover it at
    step 5.
    """

    total_bytes: int
    reserved_bytes: int

    @property
    def free_bytes(self) -> int:
        return self.total_bytes - self.reserved_bytes


def assert_rollback_window_affordable(
    *,
    incumbent_weights_bytes: int,
    candidate_weights_bytes: int,
    budget: ResidencyBudget,
    rollback_window_days: int = DEFAULT_ROLLBACK_WINDOW_DAYS,
) -> dict[str, Any]:
    """`MOS-TRAIN-186`: can the predecessor stay warm for the whole window? Before step 1.

    "The outgoing incumbent MUST remain at `role = STANDBY, state = SERVING` -- warm,
    verified and resident -- for a declared `rollback_window`, default 30 days, and MUST
    NOT be retired inside that window by any automation. Its GPU residency MUST be reserved
    for the window under Chapter 13's budget arithmetic, and the reservation MUST be
    checked **before step 1** of `MOS-TRAIN-181`, not discovered at step 5. A promotion that
    cannot afford to keep its predecessor warm is a promotion without a rollback, and it
    must be refused while it is still a capacity question."

    Pure arithmetic, so it can be run in a UI preview, in CI, and again at step 1.
    """
    if rollback_window_days <= 0:
        raise ValueError(
            "MOS-TRAIN-186 declares a rollback_window; zero days is a promotion with no "
            "rollback, which MOS-TRAIN-185 exists to prevent"
        )
    needed = int(incumbent_weights_bytes) + int(candidate_weights_bytes)
    if needed > budget.free_bytes:
        raise PromotionError(
            f"the rollback window cannot be afforded: keeping the incumbent resident "
            f"alongside the candidate needs {needed} bytes and {budget.free_bytes} are "
            f"free. MOS-TRAIN-186: refuse it while it is still a capacity question. "
            f"MOS-OPS-083 is what this becomes if it is discovered at step 5 -- a "
            f"409 unsatisfiable after the GPU hours are spent, after a report is "
            f"written, and after a named human has read a dossier for a model that "
            f"cannot run"
        )
    return {
        "rollback_window_days": int(rollback_window_days),
        "reserved_bytes": needed,
        "free_bytes_after": budget.free_bytes - needed,
        "expires_at": (
            datetime.now(UTC) + timedelta(days=int(rollback_window_days))
        ).isoformat(),
    }


def assert_no_auto_promote(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    environment: str,
    capability_id: str,
    promotion_policy: Mapping[str, Any] | None,
) -> None:
    """`MOS-TRAIN-184`, in Python, mirroring 0013's trigger. Raises `ApprovalRefused`.

    The trigger is the guarantee -- it binds a `psql` session too -- and this is the good
    error message, on the same two-layer pattern `medos.registry.lifecycle` uses for
    `MOS-REG-021`.
    """
    if not promotion_policy or not bool(promotion_policy.get("auto_promote")):
        return
    with tenant_tx(conn, tenant_id) as tx:
        clash = tx.execute(
            "SELECT public_id FROM deployments WHERE environment = %s "
            "AND capability_id = %s AND clinical_use_mode = 'clinical' "
            "AND state <> 'RETIRED' LIMIT 1",
            (environment, capability_id),
        ).fetchone()
    if clash is not None:
        raise ApprovalRefused(
            f"MOS-TRAIN-184: auto_promote is refused because capability {capability_id!r} "
            f"already has a clinical deployment in {environment} "
            f"({dict(clash)['public_id']}). A research canary configured to auto-promote, "
            "sharing a capability slot with a clinical deployment, is one "
            "clinical_use_mode edit away from the automated production-to-serving path "
            "chapter 17 exists to forbid"
        )


# =====================================================================================
# Steps 1, 2, 4 and 5
# =====================================================================================
def create_candidate_deployment(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    environment: str,
    capability_id: str,
    subject_kind: str,
    subject_id: str,
    subject_version: str,
    created_by: str,
    actor: audit.Actor,
    trace_id: str,
    rollback_reservation: Mapping[str, Any],
    promotion_policy: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> DeploymentRow:
    """Step 1: a NEW row at `role = STANDBY, state = PENDING`. `MOS-TRAIN-180`.

    `rollback_reservation` is what `assert_rollback_window_affordable()` returned, and it is
    required: `MOS-TRAIN-186` puts the check "**before step 1** ... not discovered at step
    5", so a step 1 that never saw one is the state that requirement forbids.

    The row is `research_only` and is not `ACTIVE`. `MOS-SAFE-033`: "A Deployment created
    without the field MUST be created as `research_only`", and there is no
    `clinical_use_mode` parameter here at all -- the only route is `approve_clinical_use()`,
    which is act A3 and requires a human.
    """
    if not rollback_reservation or "rollback_window_days" not in rollback_reservation:
        raise PromotionError(
            "MOS-TRAIN-186: the incumbent's residency reservation for the rollback window "
            "MUST be checked before step 1. Call assert_rollback_window_affordable() and "
            "pass its result"
        )
    assert_no_auto_promote(
        conn,
        tenant_id=tenant_id,
        environment=environment,
        capability_id=capability_id,
        promotion_policy=promotion_policy,
    )

    policy = dict(promotion_policy or {})
    policy.setdefault("auto_promote", False)
    policy["rollback_window_days"] = int(rollback_reservation["rollback_window_days"])
    policy["rollback_reservation"] = dict(rollback_reservation)

    from medos.sdk.canonical import new_ulid

    public_id = new_ulid("dep")
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            """
            INSERT INTO deployments
                (public_id, tenant_id, environment, capability_id, subject_kind,
                 subject_id, subject_version, role, state, traffic_permille,
                 promotion_policy, residency, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'STANDBY', 'PENDING', 0, %s,
                    'resident', %s)
            RETURNING id
            """,
            (
                public_id, _uuid.UUID(str(tenant_id)), environment, capability_id,
                subject_kind, subject_id, subject_version, Jsonb(policy),
                _uuid.UUID(str(created_by)),
            ),
        ).fetchone()
        audit.record(
            conn,
            action="deployment.create",
            action_class="governance",
            actor=actor,
            resource=audit.Resource(kind="deployment", id=public_id),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "step": 1,
                "role": "STANDBY",
                "state": "PENDING",
                "subject": f"{subject_kind}:{subject_id}@{subject_version}",
                "rollback_reservation": dict(rollback_reservation),
                "clinical_use_mode": "research_only",
            },
        )
    deployment = load_deployment(
        conn, tenant_id=tenant_id, deployment_id=str(dict(row)["id"])
    )
    assert deployment is not None  # noqa: S101 - just inserted inside this transaction
    return deployment


def verify_candidate(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    deployment_id: str,
    verification_ref: str,
    actor: audit.Actor,
    trace_id: str,
    request_id: str | None = None,
) -> DeploymentRow:
    """Step 2: `PENDING -> VERIFYING -> SERVING`. Automatic. `MOS-REG-075`, `MOS-IMG-054`.

    `verification_ref` is the supply-chain verification plus the worker's golden-fixture
    self-test, recorded as one reference. 0008's `deployments_serving_verified` CHECK is
    what makes it impossible to reach `SERVING` without one; this function is where it is
    written.

    The row reaches `SERVING` at `role = STANDBY`: it is warm and it receives no traffic.
    `MOS-REG-073` makes `role` and `state` orthogonal precisely so that this state is
    expressible -- "`role` says what traffic a row should get, `state` whether it may get
    any".
    """
    if not verification_ref:
        raise PromotionError(
            "MOS-REG-075: a deployment MUST NOT reach SERVING without a recorded "
            "verification_ref. The reference covers the supply-chain verification and the "
            "worker golden-fixture self-test of MOS-IMG-054"
        )
    with tenant_tx(conn, tenant_id) as tx:
        tx.execute(
            "UPDATE deployments SET state = 'VERIFYING' WHERE id = %s AND state = 'PENDING'",
            (_uuid.UUID(str(deployment_id)),),
        )
        updated = tx.execute(
            "UPDATE deployments SET state = 'SERVING', verification_ref = %s "
            "WHERE id = %s AND state = 'VERIFYING' RETURNING public_id",
            (verification_ref, _uuid.UUID(str(deployment_id))),
        ).fetchone()
        if updated is None:
            raise PromotionError(
                f"deployment {deployment_id} was not PENDING; step 2 runs once and the "
                "state machine of MOS-REG-075 is not re-entered"
            )
        audit.record(
            conn,
            action="deployment.verify",
            action_class="governance",
            actor=actor,
            resource=audit.Resource(kind="deployment", id=dict(updated)["public_id"]),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={"step": 2, "verification_ref": verification_ref,
                    "state": "SERVING", "role": "STANDBY"},
        )
    row = load_deployment(conn, tenant_id=tenant_id, deployment_id=deployment_id)
    assert row is not None  # noqa: S101
    return row


def approve_clinical_use(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    deployment_id: str,
    actor: audit.Actor,
    trace_id: str,
    approval_rationale: str,
    validation_report_id: str,
    acceptance_run_id: str,
    gate_decision_id: str,
    permissions: Sequence[str] = (),
    request_id: str | None = None,
    now: datetime | None = None,
) -> DeploymentRow:
    """Step 4 / act A3: `deployment.approve_clinical`, by a named human. `MOS-SAFE-036`.

    This is the ONLY route from `research_only` to `clinical` in this package, and it does
    NOT cut over: `MOS-TRAIN-182` keeps steps 4 and 5 distinct, and `MOS-TRAIN-183` keeps
    the incumbent live throughout. After this call the candidate is still `role = STANDBY`.

    0008's `deployments_clinical_gate` CHECK requires `approved_by`, `approved_at`,
    `acceptance_run_id` and `validation_report_id` to be non-null for a clinical row, so
    every one of them is a parameter with no default.
    """
    assert_human(actor, act="deployment.approve_clinical (A3 of MOS-TRAIN-173)")
    if "deployment.approve_clinical" not in set(permissions):
        raise ApprovalRefused(
            "MOS-TRAIN-173: act A3 requires the permission deployment.approve_clinical, "
            "which MOS-TRAIN-174 forbids any non-human principal from holding"
        )
    if len(approval_rationale.strip()) < 20:
        raise ApprovalRefused(
            "A3 attests that this deployment, in this environment, for this tenant, may "
            "see patients; the rationale is at least 20 characters"
        )

    if not _is_uuid(actor.id):
        # `deployments.approved_by` is `uuid` (0008) and 0008's `deployments_clinical_gate`
        # CHECK requires it non-null for a clinical row. An actor whose id is not a uuid
        # cannot be recorded there, and MOS-EVID-117 wants a NAMED approver rather than a
        # null one, so this is a refusal and not a silent None.
        raise ApprovalRefused(
            f"A3 records approved_by on the deployment row, which is a uuid column; "
            f"actor.id {actor.id!r} is not one. MOS-EVID-117 requires a named approver"
        )
    deployment = load_deployment(conn, tenant_id=tenant_id, deployment_id=deployment_id)
    if deployment is None:
        raise PromotionError(f"no deployment {deployment_id} in tenant {tenant_id}")
    if deployment.state != "SERVING":
        raise PromotionError(
            f"the candidate is {deployment.state}; step 2 (MOS-REG-075) runs before "
            "step 4, so the golden-fixture self-test has already passed when a human is "
            "asked to decide"
        )

    stamp = now or datetime.now(UTC)
    with tenant_tx(conn, tenant_id) as tx:
        tx.execute(
            """
            UPDATE deployments
               SET clinical_use_mode = 'clinical', approved_by = %s, approved_at = %s,
                   validation_report_id = %s, acceptance_run_id = %s
             WHERE id = %s
            """,
            (
                _uuid.UUID(str(actor.id)),
                stamp,
                _uuid.UUID(str(validation_report_id)),
                _uuid.UUID(str(acceptance_run_id)),
                _uuid.UUID(str(deployment_id)),
            ),
        )
        audit.record(
            conn,
            action="deployment.clinical_use_mode.promoted",
            action_class="clinical",
            actor=actor,
            resource=audit.Resource(kind="deployment", id=deployment.public_id),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "step": 4,
                "act": "A3",
                "permission": "deployment.approve_clinical",
                "approval_rationale": approval_rationale.strip(),
                "validation_report_id": validation_report_id,
                "acceptance_run_id": acceptance_run_id,
                "deployment_gate_decision_id": gate_decision_id,
                "role_after": deployment.role,
                "note": "A3 does not cut over; step 5 is a separate human action "
                        "(MOS-TRAIN-182)",
            },
        )
    row = load_deployment(conn, tenant_id=tenant_id, deployment_id=deployment_id)
    assert row is not None  # noqa: S101
    return row


def _is_uuid(value: str) -> bool:
    try:
        _uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def cutover(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    environment: str,
    capability_id: str,
    candidate_deployment_id: str,
    actor: audit.Actor,
    trace_id: str,
    reason: str,
    permissions: Sequence[str] = (),
    request_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Step 5: the single transaction swapping `role`. Human. `MOS-REG-077`, `MOS-TRAIN-180`.

    Takes NO gate verdict, deliberately. `MOS-TRAIN-182`: "a `PASS` from `evaluate_gate()`
    MUST NOT trigger step 5, and no configuration value, feature flag or promotion policy
    MUST be able to make it do so." A function that read a verdict could be called from one.

    Writes NO subject column. `MOS-TRAIN-180`: "It MUST NOT update `service_version_id` or
    any model reference on a row whose `state` is `SERVING`", and 0013 revokes the grant so
    that a statement attempting it fails rather than being caught here.

    The outgoing incumbent lands at `role = STANDBY, state = SERVING` -- warm, not retired.
    `MOS-TRAIN-183`: "the incumbent leaves service only at step 5 and only into `STANDBY`."
    """
    assert_human(actor, act="cutover (step 5 of MOS-TRAIN-181)")
    if "deployment.promote" not in set(permissions):
        raise ApprovalRefused(
            "MOS-TRAIN-174: cutover requires deployment.promote, which no non-human "
            "principal may hold"
        )
    if len(reason.strip()) < 10:
        raise ApprovalRefused("step 5 records why the candidate should go NOW, not merely "
                              "that it is permitted to (MOS-TRAIN-182)")

    stamp = now or datetime.now(UTC)
    incumbent = active_deployment(
        conn, tenant_id=tenant_id, environment=environment, capability_id=capability_id
    )
    candidate = load_deployment(
        conn, tenant_id=tenant_id, deployment_id=candidate_deployment_id
    )
    if candidate is None:
        raise PromotionError(f"no deployment {candidate_deployment_id}")
    if candidate.state != "SERVING":
        raise PromotionError(
            f"the candidate is {candidate.state}; only a verified, warm row is cut over "
            "(MOS-REG-075)"
        )
    if incumbent is not None and incumbent.id == candidate.id:
        raise PromotionError("the candidate is already ACTIVE")

    with tenant_tx(conn, tenant_id) as tx:
        # Demote FIRST. `deployments_one_active_per_slot` is a plain unique index, checked
        # per statement, so the reverse order would collide inside the transaction -- and
        # a deferred index would mean a window in which the slot has two ACTIVE rows.
        if incumbent is not None:
            tx.execute(
                "UPDATE deployments SET role = 'STANDBY', traffic_permille = 0 "
                "WHERE id = %s",
                (_uuid.UUID(incumbent.id),),
            )
        tx.execute(
            "UPDATE deployments SET role = 'ACTIVE', traffic_permille = 1000, "
            "activated_at = %s WHERE id = %s",
            (stamp, _uuid.UUID(candidate.id)),
        )
        audit.record(
            conn,
            action="deployment.promote",
            action_class="clinical",
            actor=actor,
            resource=audit.Resource(kind="deployment", id=candidate.public_id),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "step": 5,
                "permission": "deployment.promote",
                "reason": reason.strip(),
                "incumbent": incumbent.public_id if incumbent else None,
                "incumbent_role_after": "STANDBY" if incumbent else None,
                "candidate_role_after": "ACTIVE",
                # MOS-TRAIN-180: a promotion MINTS. Both ids are recorded so that "two
                # rows for the slot" is checkable from the audit trail alone.
                "rows_for_the_slot": [
                    x.public_id for x in (incumbent, candidate) if x is not None
                ],
            },
        )
    return {
        "candidate": candidate.public_id,
        "incumbent": incumbent.public_id if incumbent else None,
        "at": stamp,
    }


def rollback(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    environment: str,
    capability_id: str,
    target_deployment_id: str,
    actor: audit.Actor,
    trace_id: str,
    reason: str,
    non_artifact_cause: bool = False,
    permissions: Sequence[str] = (),
    request_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The reverse role swap. No new run, no new report, no new approval. `MOS-TRAIN-185`.

    "Rollback MUST be the reverse role swap of `MOS-REG-078` and MUST NOT require a new
    `EvaluationRun`, a new `ValidationReport` or a new approval, provided the target's
    `lifecycle_status` is not `SUSPENDED` or `RECALLED`. Speed is the safety property here:
    an approval process attached to rollback converts a bad promotion into a long outage,
    and the second-order effect is that operators stop promoting at all."

    So this function takes no report, no gate result and no criteria document, and it does
    not re-enter `VERIFYING`: the target has been at `state = SERVING` the whole time
    (`MOS-TRAIN-186`).

    `MOS-TRAIN-188`: it writes an `AuditEvent` carrying the reason and moves the rolled-back
    candidate to `SUSPENDED` "unless the operator explicitly records that the rollback was
    for a non-artifact cause -- capacity, an unrelated outage, a scheduling conflict.
    Leaving a rolled-back candidate at `APPROVED` invites a second promotion of the same
    defect by a different operator a week later."

    `MOS-TRAIN-187`: nothing here touches a `Result`. "Rollback changes what runs next."
    """
    if "deployment.promote" not in set(permissions):
        raise ApprovalRefused(
            "MOS-TRAIN-174: a rollback is a role mutation and requires deployment.promote"
        )
    if len(reason.strip()) < 10:
        raise ApprovalRefused("MOS-TRAIN-188: a rollback writes an AuditEvent carrying "
                              "the reason")

    stamp = now or datetime.now(UTC)
    target = load_deployment(conn, tenant_id=tenant_id, deployment_id=target_deployment_id)
    if target is None:
        raise PromotionError(f"no deployment {target_deployment_id}")
    if target.state != "SERVING":
        raise PromotionError(
            f"the rollback target is {target.state}. MOS-TRAIN-186 keeps the outgoing "
            "incumbent at role=STANDBY, state=SERVING -- warm, verified and resident -- "
            "for the rollback window, precisely so that this call does not have to "
            "re-enter VERIFYING"
        )
    with tenant_tx(conn, tenant_id) as tx:
        status = tx.execute(
            "SELECT lifecycle_status, public_id FROM artifacts WHERE public_id = %s",
            (target.subject.id,),
        ).fetchone()
        if status is not None and dict(status)["lifecycle_status"] in (
            "SUSPENDED", "RECALLED"
        ):
            raise PromotionError(
                f"the rollback target's artifact is {dict(status)['lifecycle_status']}; "
                "MOS-TRAIN-185 permits a rollback only while the target is neither "
                "SUSPENDED nor RECALLED"
            )

        current = active_deployment(
            conn, tenant_id=tenant_id, environment=environment,
            capability_id=capability_id,
        )
        if current is not None:
            tx.execute(
                "UPDATE deployments SET role = 'STANDBY', traffic_permille = 0 "
                "WHERE id = %s",
                (_uuid.UUID(current.id),),
            )
        tx.execute(
            "UPDATE deployments SET role = 'ACTIVE', traffic_permille = 1000, "
            "activated_at = %s WHERE id = %s",
            (stamp, _uuid.UUID(target.id)),
        )
        audit.record(
            conn,
            action="deployment.rollback",
            action_class="clinical",
            actor=actor,
            resource=audit.Resource(kind="deployment", id=target.public_id),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "reason": reason.strip(),
                "rolled_back": current.public_id if current else None,
                "restored": target.public_id,
                "non_artifact_cause": bool(non_artifact_cause),
                "results_untouched": True,   # MOS-TRAIN-187, MOS-SAFE-055
                "required_new_evaluation_run": False,
                "required_new_validation_report": False,
                "required_new_approval": False,
            },
        )

    suspended: str | None = None
    if current is not None and not non_artifact_cause:
        # MOS-TRAIN-188. A separate call, so it is a separate audited act: suspending the
        # artifact is a registry decision and rolling back is a deployment one.
        row = registry_repo.set_status(
            conn,
            current.subject.id,
            to_status="SUSPENDED",
            reason=f"rolled back at {stamp.isoformat()}: {reason.strip()} "
                   "(MOS-TRAIN-188)",
            actor=actor,
            trace_id=trace_id,
            request_id=request_id,
        )
        suspended = row["public_id"]

    return {
        "restored": target.public_id,
        "rolled_back": current.public_id if current else None,
        "suspended_artifact": suspended,
        "at": stamp,
    }
