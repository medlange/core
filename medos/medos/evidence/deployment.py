# SPDX-License-Identifier: Apache-2.0
"""Deployment liveness and the gated promotion. Chapter 6 §6.8, chapter 7 §7.9.3.

    MOS-REG-072  `Deployment` is the ONLY entity that determines whether a version receives
                 work. `lifecycle_status` answers "may this be deployed at all", never "is
                 this live".
    MOS-REG-073  `role` and `state` are orthogonal and both required.
    MOS-REG-074  At most one `role = ACTIVE, state = SERVING` per
                 (tenant, environment, capability).
    MOS-REG-077  Cutover is a SINGLE TRANSACTION swapping the two `role` values. It MUST
                 NOT delete, recreate or re-verify either row.
    MOS-EVID-090 Transitioning a Deployment into `clinical_use_mode: clinical`, or changing
                 the version behind a clinical deployment, MUST call `evaluate_gate()` and
                 MUST refuse on `FAIL` or `INDETERMINATE`. A `research_only` deployment
                 MUST record the gate result but MAY proceed on `FAIL`.
    MOS-EVID-091 Every invocation MUST be persisted, whatever the outcome.
    MOS-EVID-092 On `FAIL` or `INDETERMINATE` the incumbent MUST REMAIN LIVE AND SERVING.
                 "The gate MUST NOT take the capability out of service to block a
                 candidate."
    MOS-EVID-093 A `FAIL` MUST NOT be overridable in `clinical` mode BY ANY ROLE. An
                 `INDETERMINATE` MAY be overridden only by a holder of
                 `deployment.gate.override`, only with a written rationale and a named
                 approver, and the override MUST be reproduced on the Deployment, in the
                 audit trail and on every ValidationReport subsequently issued.
    MOS-EVID-094 A change to the version, the PreprocessingSpec version, any operating
                 threshold, the criteria version, the tenant binding, the inference backend
                 or the accelerator class RE-OPENS the gate.
    MOS-SAFE-033 `clinical_use_mode` defaults to `research_only` and has no tenant-wide,
                 service-wide or environment-wide override.

THE ONE PROPERTY THIS MODULE EXISTS TO MAKE TRUE
-------------------------------------------------
`MOS-EVID-092` is a statement about what does NOT happen: a refused candidate leaves the
incumbent's row untouched. The way that requirement gets broken is a promotion written as
"retire the old, install the new, then check" -- at which point a failing gate has already
taken the capability out of service, and the safest-looking implementation is the one that
turns a blocked candidate into an outage. So the order here is fixed and is the opposite:
evaluate, record, and only then -- inside one transaction, and only on an outcome that
permits it -- demote the incumbent and promote the candidate. Nothing in the refusal path
issues an `UPDATE` against any deployment row at all.

WHY A REFUSAL IS RECORDED BEFORE IT IS RAISED
----------------------------------------------
`MOS-EVID-091` says every invocation is persisted "whatever the outcome", and
`MOS-STORE-302b` says the rows are how "why was this deployment allowed" is answered. An
exception raised inside the transaction that writes the decision would roll the decision
back, so the refusal would be invisible in exactly the case that matters. `record_gate_decision`
therefore commits, and `promote()` raises afterwards.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import tenant_tx
from medos.evidence import gate as gate_mod
from medos.evidence.gate import GateBinding, GateResult, ReportView, SubjectRef
from medos.sdk.canonical import new_ulid
from medos.sdk.refusal import EvidenceError, Refusal

__all__ = [
    "OVERRIDE_PERMISSION",
    "DeploymentRow",
    "GateDecision",
    "PromotionRefused",
    "OverrideNotPermitted",
    "create_deployment",
    "load_deployment",
    "active_deployment",
    "record_gate_decision",
    "promote",
]

# Chapter 8 §8.3.2's spelling, class `governance`, quoted by MOS-STORE-302b. A literal in
# one place, because a typo in a permission string fails OPEN in the direction of
# "principal does not hold it", which is safe -- and fails CLOSED the day someone fixes the
# typo in the grant table and not here.
OVERRIDE_PERMISSION = "deployment.gate.override"

_SLOT = ("tenant_id", "environment", "capability_id")


class PromotionRefused(EvidenceError):
    """The gate refused the promotion. A clinical outcome, not a system failure.

    CONTRACT.md §9 and chapter 10's RFC 9457 model: to a caller, a refused promotion and a
    dead database look identical unless the error model separates them, and only one of the
    two is information the caller must act on. The persisted `deployment_gate_decisions`
    row is committed before this is raised (MOS-EVID-091).
    """

    problem_type = "https://medicalos.dev/problems/deployment-gate-refused"
    title = "the deployment gate refused the promotion"

    def __init__(self, decision: GateDecision) -> None:
        self.decision = decision
        self.refusals: tuple[Refusal, ...] = tuple(
            Refusal(
                check_id=r.id,
                code=(r.reason or r.status).upper(),
                message=(
                    f"{r.id}: {r.status}"
                    + (f" ({r.reason})" if r.reason else "")
                    + (
                        f" -- {r.metric} on stratum {r.stratum}, bound {r.bound} "
                        f"observed {r.observed} vs required {r.required}"
                        if r.observed is not None
                        else ""
                    )
                ),
                observed=r.observed,
                bound=r.required,
                detail={"severity": r.severity, "n": r.n, "n_patients": r.n_patients},
            )
            for r in decision.result.results
            if r.severity == "blocking" and r.status in ("FAIL", "INDETERMINATE")
        )
        super().__init__(
            f"gate verdict {decision.verdict} for deployment {decision.deployment_id}: "
            + "; ".join(r.message for r in self.refusals)
        )

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(r.check_id for r in self.refusals)

    def as_problem(self) -> dict[str, Any]:
        return {
            "type": self.problem_type,
            "title": self.title,
            "status": 422,
            "class": "clinical_rejection",
            "detail": str(self),
            "verdict": self.decision.verdict,
            "gate_decision_id": self.decision.public_id,
            "refusals": [r.as_dict() for r in self.refusals],
        }


class OverrideNotPermitted(EvidenceError):
    """MOS-EVID-093. Raised BEFORE the gate runs, so nothing is recorded as overridden.

    Two distinct cases, both refused and both named:
      * a `FAIL` in `clinical` mode -- not overridable by any role, ever;
      * a principal without `deployment.gate.override`, or an override with no written
        rationale or no named approver.
    """


@dataclass(frozen=True)
class DeploymentRow:
    id: str
    public_id: str
    tenant_id: str
    environment: str
    capability_id: str
    subject: SubjectRef
    role: str
    state: str
    clinical_use_mode: str
    traffic_permille: int = 0
    verification_ref: str | None = None
    validation_report_id: str | None = None
    acceptance_run_id: str | None = None
    approved_by: str | None = None
    activated_at: datetime | None = None

    @property
    def live(self) -> bool:
        """MOS-REG-073 filter F4: eligible for traffic."""
        return self.state == "SERVING" and self.role in ("ACTIVE", "CANARY")


@dataclass(frozen=True)
class GateDecision:
    """One persisted `deployment_gate_decisions` row plus the result it recorded."""

    id: str
    public_id: str
    deployment_id: str
    verdict: str
    applied: bool
    requested_clinical_use_mode: str
    result: GateResult
    override: Mapping[str, Any] | None = None

    @property
    def refused(self) -> bool:
        return not self.applied


def _subject_of(row: Mapping[str, Any]) -> SubjectRef:
    return SubjectRef(
        kind=row["subject_kind"], id=row["subject_id"], version=row["subject_version"]
    )


def _to_row(row: Mapping[str, Any]) -> DeploymentRow:
    return DeploymentRow(
        id=str(row["id"]),
        public_id=row["public_id"],
        tenant_id=str(row["tenant_id"]),
        environment=row["environment"],
        capability_id=row["capability_id"],
        subject=_subject_of(row),
        role=row["role"],
        state=row["state"],
        clinical_use_mode=row["clinical_use_mode"],
        traffic_permille=row.get("traffic_permille", 0),
        verification_ref=row.get("verification_ref"),
        validation_report_id=(
            str(row["validation_report_id"]) if row.get("validation_report_id") else None
        ),
        acceptance_run_id=(
            str(row["acceptance_run_id"]) if row.get("acceptance_run_id") else None
        ),
        approved_by=str(row["approved_by"]) if row.get("approved_by") else None,
        activated_at=row.get("activated_at"),
    )


_SELECT = """
    SELECT id, public_id, tenant_id, environment, capability_id,
           subject_kind, subject_id, subject_version, role, state, clinical_use_mode,
           traffic_permille, verification_ref, validation_report_id, acceptance_run_id,
           approved_by, activated_at
      FROM deployments
"""


def create_deployment(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    environment: str,
    capability_id: str,
    subject: SubjectRef,
    created_by: str,
    role: str = "STANDBY",
    state: str = "PENDING",
    verification_ref: str | None = None,
    traffic_permille: int = 0,
    promotion_policy: Mapping[str, Any] | None = None,
) -> DeploymentRow:
    """Create a deployment. It is `research_only` and it is not ACTIVE. MOS-SAFE-033.

    Neither default is a convenience. MOS-SAFE-033: "A Deployment created without the
    field MUST be created as `research_only`", and `clinical_use_mode` is not a parameter
    here at all -- the only route to `clinical` is `promote()`, which runs the gate. A
    keyword argument would be a route around MOS-EVID-090 that no reviewer would notice.
    """
    if state == "SERVING" and not verification_ref:
        # MOS-REG-075, caught here as well as by the CHECK so the message names the rule.
        raise ValueError(
            "MOS-REG-075: a deployment MUST NOT reach SERVING without a recorded "
            "verification_ref"
        )
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            """
            INSERT INTO deployments
                (public_id, tenant_id, environment, capability_id,
                 subject_kind, subject_id, subject_version, role, state,
                 traffic_permille, verification_ref, promotion_policy, created_by,
                 activated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s = 'SERVING' THEN now() END)
            RETURNING id, public_id, tenant_id, environment, capability_id,
                      subject_kind, subject_id, subject_version, role, state,
                      clinical_use_mode, traffic_permille, verification_ref,
                      validation_report_id, acceptance_run_id, approved_by, activated_at
            """,
            (
                new_ulid("dep"),
                _uuid.UUID(str(tenant_id)),
                environment,
                capability_id,
                subject.kind,
                subject.id,
                subject.version,
                role,
                state,
                traffic_permille,
                verification_ref,
                Jsonb(dict(promotion_policy)) if promotion_policy else None,
                _uuid.UUID(str(created_by)),
                state,
            ),
        ).fetchone()
    return _to_row(dict(row))


def load_deployment(
    conn: psycopg.Connection[Any], *, tenant_id: str, deployment_id: str
) -> DeploymentRow | None:
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            _SELECT + " WHERE tenant_id = %s AND id = %s",
            (_uuid.UUID(str(tenant_id)), _uuid.UUID(str(deployment_id))),
        ).fetchone()
    return _to_row(dict(row)) if row is not None else None


def active_deployment(
    conn: psycopg.Connection[Any], *, tenant_id: str, environment: str, capability_id: str
) -> DeploymentRow | None:
    """The one live ACTIVE row in the slot, or None. MOS-REG-074, MOS-STORE-262.

    This is the query MOS-EVID-092 is checked with: run it before a refused promotion and
    after, and the same row must come back unchanged.
    """
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            _SELECT
            + """ WHERE tenant_id = %s AND environment = %s AND capability_id = %s
                    AND role = 'ACTIVE' AND state = 'SERVING' """,
            (_uuid.UUID(str(tenant_id)), environment, capability_id),
        ).fetchone()
    return _to_row(dict(row)) if row is not None else None


def _check_override(
    verdict: str,
    requested_mode: str,
    override: Mapping[str, Any] | None,
    permissions: Iterable[str],
) -> None:
    """MOS-EVID-093, evaluated before anything is written."""
    if override is None:
        return
    if verdict == "FAIL" and requested_mode == "clinical":
        raise OverrideNotPermitted(
            "MOS-EVID-093: a FAIL MUST NOT be overridable in clinical mode by any role. "
            "A failing criterion is a statement about the candidate, and the remedy is a "
            "different candidate."
        )
    if verdict not in ("INDETERMINATE",):
        raise OverrideNotPermitted(
            f"an override is only meaningful against an INDETERMINATE verdict; this one "
            f"is {verdict}"
        )
    if OVERRIDE_PERMISSION not in set(permissions):
        raise OverrideNotPermitted(
            f"MOS-EVID-093: overriding an INDETERMINATE requires the permission "
            f"{OVERRIDE_PERMISSION!r}"
        )
    if not str(override.get("rationale", "")).strip():
        raise OverrideNotPermitted(
            "MOS-EVID-093: an override MUST carry a written rationale"
        )
    if not str(override.get("approver", "")).strip():
        raise OverrideNotPermitted(
            "MOS-EVID-093: an override MUST name an approver"
        )


def record_gate_decision(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    deployment_id: str,
    capability_id: str,
    criteria_version: int,
    result: GateResult,
    decided_by: str,
    requested_clinical_use_mode: str,
    candidate_report_id: str | None,
    incumbent_report_id: str | None = None,
    override: Mapping[str, Any] | None = None,
    applied: bool = False,
    _tx: psycopg.Connection[Any] | None = None,
) -> GateDecision:
    """Insert the append-only decision row. MOS-EVID-091.

    Every invocation, whatever the outcome, including the `SKIPPED` criteria
    (MOS-STORE-302b). `_tx` lets `promote()` write the decision and move the deployment in
    one transaction, so an applied promotion and its justification commit together or not
    at all.
    """
    public_id = new_ulid("gd")
    sql = """
        INSERT INTO deployment_gate_decisions
            (public_id, tenant_id, deployment_id, capability_id, criteria_version,
             candidate_report_id, incumbent_report_id, verdict, criterion_results,
             requested_clinical_use_mode, applied, decided_by, override)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, public_id
    """
    params = (
        public_id,
        _uuid.UUID(str(tenant_id)),
        _uuid.UUID(str(deployment_id)),
        capability_id,
        criteria_version,
        _uuid.UUID(str(candidate_report_id)) if candidate_report_id else None,
        _uuid.UUID(str(incumbent_report_id)) if incumbent_report_id else None,
        result.verdict,
        Jsonb(result.as_json()),
        requested_clinical_use_mode,
        applied,
        _uuid.UUID(str(decided_by)),
        Jsonb(dict(override)) if override else None,
    )
    if _tx is not None:
        row = _tx.execute(sql, params).fetchone()
    else:
        with tenant_tx(conn, tenant_id) as tx:
            row = tx.execute(sql, params).fetchone()
    return GateDecision(
        id=str(row["id"]),
        public_id=row["public_id"],
        deployment_id=str(deployment_id),
        verdict=result.verdict,
        applied=applied,
        requested_clinical_use_mode=requested_clinical_use_mode,
        result=result,
        override=dict(override) if override else None,
    )


def promote(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    deployment_id: str,
    candidate_report: ReportView | None,
    incumbent_report: ReportView | None,
    criteria_document: Mapping[str, Any],
    binding: GateBinding,
    decided_by: str,
    requested_clinical_use_mode: str = "clinical",
    permissions: Iterable[str] = (),
    override: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    raise_on_refusal: bool = True,
) -> GateDecision:
    """Run the gate and, only if it permits, make the candidate live. MOS-EVID-090/092.

    Returns the persisted decision. Raises `PromotionRefused` AFTER the decision is
    committed when the promotion did not happen and `raise_on_refusal` is set; callers that
    want the verdict without the exception (a dry run, a UI preview) pass False.

    The refusal path issues NO `UPDATE` against any deployment row. That is the whole of
    MOS-EVID-092 and it is a property of this function's shape, not of a check inside it.
    """
    now = now or datetime.now(UTC)
    if requested_clinical_use_mode not in ("research_only", "clinical"):
        raise ValueError(f"unknown clinical_use_mode {requested_clinical_use_mode!r}")

    candidate = load_deployment(conn, tenant_id=tenant_id, deployment_id=deployment_id)
    if candidate is None:
        raise ValueError(f"no deployment {deployment_id} in tenant {tenant_id}")

    result = gate_mod.evaluate_gate(
        candidate_report, incumbent_report, criteria_document, binding, now=now
    )

    # MOS-EVID-093, before anything is written: an override of a FAIL in clinical mode is
    # refused outright rather than recorded and then ignored.
    _check_override(result.verdict, requested_clinical_use_mode, override, permissions)

    # MOS-EVID-090's two-branch rule, stated once.
    if requested_clinical_use_mode == "clinical":
        may_apply = result.verdict == "PASS" or (
            result.verdict == "INDETERMINATE" and override is not None
        )
    else:
        # "A deployment in `research_only` mode MUST record the gate result but MAY
        # proceed on `FAIL`, with the failure surfaced on the Deployment and in the UI."
        may_apply = True

    criteria_version = int(
        criteria_document.get("metadata", {}).get("version", binding.criteria_version)
    )
    candidate_report_id = candidate_report.report_id if candidate_report else None
    incumbent_report_id = incumbent_report.report_id if incumbent_report else None

    if not may_apply:
        decision = record_gate_decision(
            conn,
            tenant_id=tenant_id,
            deployment_id=deployment_id,
            capability_id=candidate.capability_id,
            criteria_version=criteria_version,
            result=result,
            decided_by=decided_by,
            requested_clinical_use_mode=requested_clinical_use_mode,
            candidate_report_id=candidate_report_id,
            incumbent_report_id=incumbent_report_id,
            override=None,
            applied=False,
        )
        if raise_on_refusal:
            raise PromotionRefused(decision)
        return decision

    # -- The applied path. One transaction, MOS-REG-077. ------------------------------
    with tenant_tx(conn, tenant_id) as tx:
        decision = record_gate_decision(
            conn,
            tenant_id=tenant_id,
            deployment_id=deployment_id,
            capability_id=candidate.capability_id,
            criteria_version=criteria_version,
            result=result,
            decided_by=decided_by,
            requested_clinical_use_mode=requested_clinical_use_mode,
            candidate_report_id=candidate_report_id,
            incumbent_report_id=incumbent_report_id,
            override=override,
            applied=True,
            _tx=tx,
        )

        # Demote the incumbent FIRST. `deployments_one_active_per_slot` is a plain partial
        # unique index and is therefore checked per statement, not deferred: promoting
        # before demoting would violate it mid-transaction even though the end state is
        # legal. MOS-REG-077 requires a swap, not a delete-and-recreate, and this is the
        # order in which a swap is expressible.
        tx.execute(
            """
            UPDATE deployments
               SET role = 'STANDBY'
             WHERE tenant_id = %s AND environment = %s AND capability_id = %s
               AND role = 'ACTIVE' AND state = 'SERVING' AND id <> %s
            """,
            (
                _uuid.UUID(str(tenant_id)),
                candidate.environment,
                candidate.capability_id,
                _uuid.UUID(str(deployment_id)),
            ),
        )

        tx.execute(
            """
            UPDATE deployments
               SET role                 = 'ACTIVE',
                   state                = 'SERVING',
                   clinical_use_mode    = %s,
                   verification_ref     = coalesce(verification_ref, %s),
                   acceptance_run_id    = %s,
                   validation_report_id = %s,
                   approved_by          = %s,
                   approved_at          = %s,
                   activated_at         = coalesce(activated_at, now())
             WHERE tenant_id = %s AND id = %s
            """,
            (
                requested_clinical_use_mode,
                decision.public_id,
                (
                    _uuid.UUID(str(candidate_report.run.run_id))
                    if candidate_report is not None
                    else None
                ),
                _uuid.UUID(str(candidate_report_id)) if candidate_report_id else None,
                _uuid.UUID(str(decided_by)),
                now,
                _uuid.UUID(str(tenant_id)),
                _uuid.UUID(str(deployment_id)),
            ),
        )

    return decision
