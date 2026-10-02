# SPDX-License-Identifier: Apache-2.0
"""Row access for the three tables `0010_safety.up.sql` creates.

`applicability_envelopes`, `envelope_decisions`, `result_reviews`. Same shape as
`medos.db.repo`: plain SQL, no ORM (CONTRACT.md section 1), every write inside
`tenant_tx()` so the session GUC is set exactly once by the one function permitted to set
it (`MOS-SEC-074`/`075`).

WHAT IS AND IS NOT ENFORCED HERE
---------------------------------
The state machine is `medos/medos/safety/review.py` and the marking gate is
`medos/medos/safety/marking.py`. This module writes rows and refuses transitions the database
would refuse anyway, which is not redundancy: `MOS-SAFE-069` requires a 409 with
`already_claimed` rather than a constraint violation surfacing as a 500, and the
information needed to say WHICH principal holds the claim is in the row this module has
already read.

`results.review_status` is never named in a statement here. `MOS-SAFE-062` makes it a
projection maintained by the `result_reviews_sync` trigger "in the same transaction as the
review transition", and "MUST NOT be writable through any API". An UPDATE of it from
application code would be a second writer of a derived column, and the two would disagree
the first time a transition took a path that forgot it.

Spec: MOS-SAFE-058..070, MOS-SAFE-104, MOS-EVID-095, MOS-EVID-100..104, MOS-SEC-074,
MOS-SEC-075, MOS-SEC-149, MOS-STORE-357, CONTRACT.md sections 1, 8 and 11.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import current_tenant, tenant_tx
from medos.safety.envelope import (
    ApplicabilityEnvelope,
    EnvelopeVerdict,
)
from medos.safety.policy import DeploymentSafetyPolicy
from medos.safety.review import (
    CLAIM_LEASE,
    TERMINAL_STATES,
    InvalidTransition,
    ReviewerClass,
    ReviewState,
    event_for,
    next_state,
    permitted,
    validate_submission,
)
from medos.sdk.canonical import new_ulid

__all__ = [
    "EnvelopeRow",
    "ReviewRow",
    "ReviewConflict",
    "ReviewNotFound",
    "declare_envelope",
    "resolve_envelope",
    "record_decision",
    "tenant_marginal_policy",
    "open_review",
    "open_reviews_for_job",
    "get_review",
    "get_review_rounds",
    "list_reviews",
    "claim_review",
    "release_review",
    "submit_review",
    "assign_review",
    "reopen_review",
    "expire_due_reviews",
    "release_expired_claims",
    "supersede_reviews_for_result",
    "audit_detail",
]


class ReviewConflict(RuntimeError):
    """`MOS-SAFE-069`'s 409 cases: `already_claimed`, and a transition from a stale state.

    Carries `code` so the API renders `already_claimed` rather than a sentence, and
    `state` so the caller can re-read without a second round trip.
    """

    def __init__(self, code: str, message: str, *, state: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.state = state


class ReviewNotFound(LookupError):
    """No review row with that public id in this tenant. Rendered 404."""


@dataclass(frozen=True)
class EnvelopeRow:
    """One stored `applicability_envelopes` row, with its declaration parsed back."""

    id: str
    public_id: str
    envelope: ApplicabilityEnvelope
    envelope_digest: str
    created_at: datetime


@dataclass(frozen=True)
class ReviewRow:
    """One `result_reviews` row. A plain projection; the state machine is elsewhere."""

    id: str
    public_id: str
    result_id: str
    round: int
    state: ReviewState
    assignee_user_id: str | None
    reviewer_user_id: str | None
    reviewer_role: str | None
    reviewer_class: ReviewerClass | None
    claimed_at: datetime | None
    submitted_at: datetime | None
    review_due_at: datetime | None
    action_rationale: str | None
    modifications: Any | None
    rejection_reason: str | None
    known_failure_mode_id: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def as_api(self) -> dict[str, Any]:
        """The wire projection. `id` is the `rrv_` public id and never the uuid."""
        return {
            "id": self.public_id,
            "result_id": self.result_id,
            "round": self.round,
            "state": self.state,
            "assignee_user_id": self.assignee_user_id,
            "reviewer_user_id": self.reviewer_user_id,
            "reviewer_role": self.reviewer_role,
            "reviewer_class": self.reviewer_class,
            "claimed_at": self.claimed_at,
            "submitted_at": self.submitted_at,
            "review_due_at": self.review_due_at,
            "action_rationale": self.action_rationale,
            "modifications": self.modifications,
            "rejection_reason": self.rejection_reason,
            "known_failure_mode_id": self.known_failure_mode_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_review(row: Mapping[str, Any]) -> ReviewRow:
    return ReviewRow(
        id=str(row["id"]),
        public_id=str(row["public_id"]),
        result_id=str(row["result_id"]),
        round=int(row["round"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        assignee_user_id=_opt_str(row["assignee_user_id"]),
        reviewer_user_id=_opt_str(row["reviewer_user_id"]),
        reviewer_role=row["reviewer_role"],
        reviewer_class=row["reviewer_class"],
        claimed_at=row["claimed_at"],
        submitted_at=row["submitted_at"],
        review_due_at=row["review_due_at"],
        action_rationale=row["action_rationale"],
        modifications=row["modifications"],
        rejection_reason=row["rejection_reason"],
        known_failure_mode_id=row["known_failure_mode_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


# =======================================================================================
# applicability_envelopes -- MOS-EVID-095
# =======================================================================================
def declare_envelope(
    conn: psycopg.Connection[Any],
    envelope: ApplicabilityEnvelope,
    *,
    declared_by: str,
) -> EnvelopeRow:
    """Store one version of one subject's envelope. Idempotent on the content address.

    A re-declaration of a byte-identical envelope returns the existing row rather than
    raising. `MOS-EVID-095` makes the entity immutable per version and the digest is a pure
    function of the declaration, so "declare the same thing twice" carries no information
    and refusing it would make a deployment's bootstrap non-idempotent -- which is how a
    restart loop turns into an outage.

    A re-declaration of a DIFFERENT envelope at the same `version` raises the unique
    violation, and that is correct: it is an attempt to change a published clinical claim
    without publishing a new version, which is exactly `MOS-EVID-098`'s widening move.
    """
    manifest = envelope.as_manifest()
    digest = envelope.digest
    with tenant_tx(conn):
        existing = conn.execute(
            """
            SELECT id, public_id, envelope_digest, created_at
              FROM applicability_envelopes
             WHERE envelope_digest = %s
            """,
            (digest,),
        ).fetchone()
        if existing is not None:
            return EnvelopeRow(
                id=str(existing["id"]),
                public_id=str(existing["public_id"]),
                envelope=envelope,
                envelope_digest=digest,
                created_at=existing["created_at"],
            )
        row = conn.execute(
            """
            INSERT INTO applicability_envelopes (
                tenant_id, public_id, subject_kind, subject_id, subject_version,
                version, derivation, derived_from_evaluation_run, constraints,
                marginal_policy_default, envelope_digest, declared_by)
            VALUES (%(tenant)s, %(pid)s, %(skind)s, %(sid)s, %(sver)s,
                    %(ver)s, %(deriv)s, %(run)s, %(cons)s,
                    %(policy)s, %(digest)s, %(by)s)
            RETURNING id, public_id, created_at
            """,
            {
                "tenant": current_tenant(),
                "pid": new_ulid("ae"),
                "skind": envelope.subject_kind,
                "sid": envelope.subject_id,
                "sver": envelope.subject_version,
                "ver": envelope.version,
                "deriv": envelope.derivation,
                "run": envelope.derived_from_evaluation_run,
                "cons": Jsonb(manifest["constraints"]),
                "policy": envelope.marginal_policy_default,
                "digest": digest,
                "by": declared_by,
            },
        ).fetchone()
        assert row is not None
        return EnvelopeRow(
            id=str(row["id"]),
            public_id=str(row["public_id"]),
            envelope=envelope,
            envelope_digest=digest,
            created_at=row["created_at"],
        )


def resolve_envelope(
    conn: psycopg.Connection[Any],
    *,
    subject_id: str,
    subject_version: str,
    subject_kind: str = "service_version",
) -> EnvelopeRow | None:
    """The HIGHEST declared version for a subject, or None.

    "Highest version" is computed here rather than stored as an `is_active` flag; see
    `0010_safety.up.sql` section 4 for why a flag would eventually be true on two rows.

    Returning None rather than raising is deliberate and is NOT the same as permitting the
    job. `MOS-EVID-095` requires every ServiceVersion that declares a capability to carry
    an envelope, and the CALLER decides what an absent one means -- the worker treats it as
    a declaration gap and says so in the step detail rather than silently proceeding as if
    the study had been checked. Deciding that here would hide the gap inside a lookup.
    """
    with tenant_tx(conn):
        row = conn.execute(
            """
            SELECT id, public_id, subject_kind, subject_id, subject_version, version,
                   derivation, derived_from_evaluation_run, constraints,
                   marginal_policy_default, envelope_digest, created_at
              FROM applicability_envelopes
             WHERE subject_kind = %s AND subject_id = %s AND subject_version = %s
             ORDER BY version DESC
             LIMIT 1
            """,
            (subject_kind, subject_id, subject_version),
        ).fetchone()
    if row is None:
        return None
    envelope = ApplicabilityEnvelope.from_manifest(
        {
            "subject_kind": row["subject_kind"],
            "subject_id": row["subject_id"],
            "subject_version": row["subject_version"],
            "version": row["version"],
            "derivation": row["derivation"],
            "derived_from_evaluation_run": row["derived_from_evaluation_run"],
            "marginal_policy_default": row["marginal_policy_default"],
            "constraints": row["constraints"],
        }
    )
    stored = str(row["envelope_digest"])
    if envelope.digest != stored:
        # The stored digest and the digest recomputed from the stored constraints
        # disagree. That means a row was written by a path that did not use
        # `envelope_digest()`, or the table was edited out from under the trigger. Either
        # way the content address is not an address, and a `ValidationReport` citing it
        # resolves to something other than what was enforced. Fail loudly.
        raise ValueError(
            f"applicability_envelopes {row['public_id']}: stored digest {stored} does "
            f"not match the digest of its own constraints ({envelope.digest}). The "
            "envelope is not content-addressed and MUST NOT be used to judge a study"
        )
    return EnvelopeRow(
        id=str(row["id"]),
        public_id=str(row["public_id"]),
        envelope=envelope,
        envelope_digest=stored,
        created_at=row["created_at"],
    )


def tenant_marginal_policy(conn: psycopg.Connection[Any]) -> str:
    """`tenants.marginal_policy` for the bound tenant. `MOS-EVID-101`."""
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT marginal_policy FROM tenants WHERE id = %s", (current_tenant(),)
        ).fetchone()
    return str(row["marginal_policy"]) if row else "flag"


def record_decision(
    conn: psycopg.Connection[Any],
    verdict: EnvelopeVerdict,
    *,
    envelope_row: EnvelopeRow,
    study_instance_uid: str,
    series_instance_uid: str | None,
    job_uuid: str | None,
    capability_id: str | None = None,
) -> str:
    """Append one `envelope_decisions` row. `MOS-EVID-104`'s export reads these.

    `job_uuid` is the `jobs.id` uuid and not the `job_` public id, because the column is a
    foreign key. None is a legitimate value: `MOS-DATA-076`'s ambient auto-routing case
    produces a decision and no Job at all, and a decision that could not be recorded
    without a job would make that case invisible to the monitoring `MOS-EVID-104` requires.
    """
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO envelope_decisions (
                tenant_id, public_id, job_id, study_instance_uid, series_instance_uid,
                envelope_id, envelope_version, envelope_digest, subject_id,
                subject_version, capability_id, zone, marginal_policy, outcome,
                reason_code, violations, observed)
            VALUES (%(tenant)s, %(pid)s, %(job)s, %(study)s, %(series)s,
                    %(env)s, %(ever)s, %(edig)s, %(sid)s,
                    %(sver)s, %(cap)s, %(zone)s, %(policy)s, %(outcome)s,
                    %(reason)s, %(viol)s, %(obs)s)
            RETURNING public_id
            """,
            {
                "tenant": current_tenant(),
                "pid": new_ulid("envd"),
                "job": job_uuid,
                "study": study_instance_uid,
                "series": series_instance_uid,
                "env": envelope_row.id,
                "ever": envelope_row.envelope.version,
                "edig": envelope_row.envelope_digest,
                "sid": envelope_row.envelope.subject_id,
                "sver": envelope_row.envelope.subject_version,
                "cap": capability_id,
                "zone": verdict.zone,
                "policy": verdict.marginal_policy,
                "outcome": verdict.outcome,
                "reason": verdict.reason_code,
                "viol": Jsonb(verdict.violations()),
                "obs": Jsonb(dict(verdict.observed)),
            },
        ).fetchone()
        assert row is not None
        return str(row["public_id"])


# =======================================================================================
# result_reviews -- MOS-SAFE-058..070
# =======================================================================================
def open_review(
    conn: psycopg.Connection[Any],
    *,
    result_id: str,
    policy: DeploymentSafetyPolicy,
    now: datetime | None = None,
) -> ReviewRow | None:
    """`MOS-SAFE-059` row 1: the platform's `PENDING` row on Result creation.

    Returns None when `review_mode = off`. Idempotent per `(result_id, round=1)`: a retry
    that reaches the result stage again (`MOS-STORE-275` makes that a supported path and
    requires it to be treated as success) must not raise on the unique constraint.
    """
    if not policy.opens_review_on_result:
        return None
    moment = now or datetime.now(UTC)
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO result_reviews (
                tenant_id, result_id, public_id, round, state, review_due_at)
            VALUES (%s, %s, %s, 1, 'PENDING', %s)
            ON CONFLICT ON CONSTRAINT result_reviews_round_uk DO NOTHING
            RETURNING *
            """,
            (
                current_tenant(),
                result_id,
                new_ulid("rrv"),
                policy.review_due_at(now=moment),
            ),
        ).fetchone()
        if row is None:
            existing = conn.execute(
                "SELECT * FROM result_reviews WHERE result_id = %s AND round = 1",
                (result_id,),
            ).fetchone()
            return _row_to_review(existing) if existing else None
        return _row_to_review(row)


def open_reviews_for_job(
    conn: psycopg.Connection[Any],
    *,
    result_ids: Sequence[str],
    policy: DeploymentSafetyPolicy,
    now: datetime | None = None,
) -> tuple[ReviewRow, ...]:
    """`open_review` for every result a job produced. Called inside the job's own
    transaction so that `MOS-SAFE-059`'s "on `Result` creation" is literally true."""
    opened = [open_review(conn, result_id=rid, policy=policy, now=now) for rid in result_ids]
    return tuple(r for r in opened if r is not None)


def get_review(conn: psycopg.Connection[Any], public_id: str) -> ReviewRow:
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT * FROM result_reviews WHERE public_id = %s", (public_id,)
        ).fetchone()
    if row is None:
        raise ReviewNotFound(public_id)
    return _row_to_review(row)


def get_review_rounds(conn: psycopg.Connection[Any], public_id: str) -> tuple[ReviewRow, ...]:
    """Every round for the Result the named review belongs to. `MOS-SAFE-069`'s `rounds[]`.

    "`GET /api/v1/result-reviews/{id}` ... includes all rounds via `rounds[]`." A review is
    corrected by a new round and never by an edit (`MOS-SAFE-059`), so the history IS the
    record and a single-row response would hide the correction.
    """
    review = get_review(conn, public_id)
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT * FROM result_reviews WHERE result_id = %s ORDER BY round ASC",
            (review.result_id,),
        ).fetchall()
    return tuple(_row_to_review(r) for r in rows)


def list_reviews(
    conn: psycopg.Connection[Any],
    *,
    result_id: str | None = None,
    state: str | None = None,
    assignee_user_id: str | None = None,
    overdue: bool = False,
    limit: int = 50,
    now: datetime | None = None,
) -> tuple[ReviewRow, ...]:
    """`GET /api/v1/result-reviews`'s filters. Tenant-scoped by RLS, not by this WHERE."""
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
    if result_id is not None:
        clauses.append("result_id = %(result_id)s")
        params["result_id"] = result_id
    if state is not None:
        clauses.append("state = %(state)s")
        params["state"] = state
    if assignee_user_id is not None:
        clauses.append("assignee_user_id = %(assignee)s")
        params["assignee"] = assignee_user_id
    if overdue:
        # An overdue review is one that is still OPEN past its due date. A terminal row
        # past its due date is not overdue -- it is finished -- and listing it under
        # `overdue=true` is how a queue that is actually empty looks permanently red.
        clauses.append("review_due_at IS NOT NULL AND review_due_at < %(now)s")
        clauses.append("state IN ('PENDING','IN_REVIEW')")
        params["now"] = now or datetime.now(UTC)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with tenant_tx(conn):
        rows = conn.execute(
            f"SELECT * FROM result_reviews {where} "
            "ORDER BY created_at DESC, round DESC LIMIT %(limit)s",
            params,
        ).fetchall()
    return tuple(_row_to_review(r) for r in rows)


def _guard(review: ReviewRow, action: str) -> None:
    try:
        permitted(review.state, action)
    except InvalidTransition as exc:
        raise ReviewConflict(
            "invalid_transition", str(exc), state=review.state
        ) from exc


def claim_review(
    conn: psycopg.Connection[Any],
    public_id: str,
    *,
    reviewer_user_id: str,
    now: datetime | None = None,
) -> ReviewRow:
    """`PENDING -> IN_REVIEW`. 409 `already_claimed` when another principal holds it.

    `MOS-SAFE-069`: "409 `already_claimed` if `reviewer_user_id` set by another principal".
    The check is on the ROW as read inside the transaction and the UPDATE is guarded by
    `state = 'PENDING'` in its WHERE, so two concurrent claims cannot both succeed: the
    loser updates zero rows and is reported the conflict rather than silently overwriting
    the winner's claim.
    """
    moment = now or datetime.now(UTC)
    with tenant_tx(conn):
        review = get_review(conn, public_id)
        _guard(review, "claim")
        if review.reviewer_user_id and review.reviewer_user_id != reviewer_user_id:
            raise ReviewConflict(
                "already_claimed",
                f"review {public_id} is claimed by another principal",
                state=review.state,
            )
        row = conn.execute(
            """
            UPDATE result_reviews
               SET state = 'IN_REVIEW', reviewer_user_id = %s, claimed_at = %s
             WHERE public_id = %s AND state = 'PENDING'
            RETURNING *
            """,
            (reviewer_user_id, moment, public_id),
        ).fetchone()
        if row is None:
            raise ReviewConflict(
                "already_claimed",
                f"review {public_id} was claimed concurrently",
                state=review.state,
            )
        return _row_to_review(row)


def release_review(
    conn: psycopg.Connection[Any], public_id: str, *, reviewer_user_id: str
) -> ReviewRow:
    """`IN_REVIEW -> PENDING`. "only by the current claimant" (`MOS-SAFE-069`).

    The claimant check is not a courtesy: a principal who can release another's claim can
    take a review out from under a reader mid-read, and the row's `claimed_at` would then
    describe a claim nobody made.
    """
    with tenant_tx(conn):
        review = get_review(conn, public_id)
        _guard(review, "release")
        if review.reviewer_user_id != reviewer_user_id:
            raise ReviewConflict(
                "not_claimant",
                f"review {public_id} is claimed by another principal; "
                "only the current claimant may release it",
                state=review.state,
            )
        row = conn.execute(
            """
            UPDATE result_reviews
               SET state = 'PENDING', reviewer_user_id = NULL, claimed_at = NULL
             WHERE public_id = %s AND state = 'IN_REVIEW'
            RETURNING *
            """,
            (public_id,),
        ).fetchone()
        assert row is not None
        return _row_to_review(row)


def submit_review(
    conn: psycopg.Connection[Any],
    public_id: str,
    *,
    outcome: str,
    reviewer_user_id: str,
    reviewer_role: str,
    reviewer_class: ReviewerClass,
    action_rationale: str | None = None,
    modifications: Any | None = None,
    rejection_reason: str | None = None,
    known_failure_mode_id: str | None = None,
    now: datetime | None = None,
) -> ReviewRow:
    """`IN_REVIEW -> ACCEPTED | MODIFIED | REJECTED`. Terminal for this row.

    `reviewer_class` is passed IN rather than derived here, and that is deliberate:
    `MOS-SAFE-068` derives it from the PRINCIPAL and the role directory, both of which are
    request-scoped facts this module does not have and must not guess. The API layer calls
    `medos.safety.review.reviewer_class_for` and hands the answer down. A repository that
    derived it would have to know what kind of credential made the request, which is
    exactly the knowledge `MOS-SEC-008` concentrates in the PEP.
    """
    validate_submission(
        outcome=outcome,
        action_rationale=action_rationale,
        modifications=modifications,
        rejection_reason=rejection_reason,
    )
    moment = now or datetime.now(UTC)
    with tenant_tx(conn):
        review = get_review(conn, public_id)
        _guard(review, "submit")
        if review.reviewer_user_id != reviewer_user_id:
            raise ReviewConflict(
                "not_claimant",
                f"review {public_id} is claimed by another principal; a review is "
                "submitted by the principal that claimed it",
                state=review.state,
            )
        terminal = next_state(review.state, "submit", outcome=outcome)
        row = conn.execute(
            """
            UPDATE result_reviews
               SET state = %(state)s,
                   reviewer_role = %(role)s,
                   reviewer_class = %(class)s,
                   submitted_at = %(now)s,
                   action_rationale = %(rationale)s,
                   modifications = %(mods)s,
                   rejection_reason = %(reason)s,
                   known_failure_mode_id = %(fm)s
             WHERE public_id = %(pid)s AND state = 'IN_REVIEW'
            RETURNING *
            """,
            {
                "state": terminal,
                "role": reviewer_role,
                "class": reviewer_class,
                "now": moment,
                "rationale": action_rationale,
                "mods": None if modifications is None else Jsonb(modifications),
                "reason": rejection_reason,
                "fm": known_failure_mode_id,
                "pid": public_id,
            },
        ).fetchone()
        if row is None:
            raise ReviewConflict(
                "invalid_transition",
                f"review {public_id} is no longer IN_REVIEW",
                state=review.state,
            )
        return _row_to_review(row)


def assign_review(
    conn: psycopg.Connection[Any], public_id: str, *, assignee_user_id: str
) -> ReviewRow:
    """`POST .../assign`. Not a state transition -- `MOS-SAFE-059` has no row for it.

    Assignment is a queue hint and changes nothing about who may claim; `MOS-SAFE-067`
    gives it its own permission (`result.review.assign`) precisely because it is a
    different act from reviewing. Refused on a terminal row because assigning a finished
    review to someone is an instruction that cannot be carried out.
    """
    with tenant_tx(conn):
        review = get_review(conn, public_id)
        if review.is_terminal:
            raise ReviewConflict(
                "invalid_transition",
                f"review {public_id} is {review.state} and cannot be assigned; "
                "reopen it for a new round instead (MOS-SAFE-066)",
                state=review.state,
            )
        row = conn.execute(
            "UPDATE result_reviews SET assignee_user_id = %s WHERE public_id = %s "
            "RETURNING *",
            (assignee_user_id, public_id),
        ).fetchone()
        assert row is not None
        return _row_to_review(row)


def reopen_review(
    conn: psycopg.Connection[Any],
    public_id: str,
    *,
    action_rationale: str,
    policy: DeploymentSafetyPolicy,
    now: datetime | None = None,
) -> ReviewRow:
    """`MOS-SAFE-066`: a NEW ROW at `round + 1`, `PENDING`. The old row is untouched.

    "Reopening does not invalidate a previously issued verified SR; a corrected conclusion
    produces another SR revision." So nothing about the previous round is edited -- it
    cannot be, the trigger forbids it -- and the new round starts empty. The rationale is
    recorded on the NEW row, because it is the reason this round exists.
    """
    rationale = (action_rationale or "").strip()
    if len(rationale) < 20:
        from medos.safety.review import MIN_RATIONALE_CHARS, SubmissionInvalid

        raise SubmissionInvalid(
            {
                "action_rationale": (
                    f"REQUIRED on reopen and MUST be at least {MIN_RATIONALE_CHARS} "
                    "characters (MOS-SAFE-066)"
                )
            }
        )
    moment = now or datetime.now(UTC)
    with tenant_tx(conn):
        review = get_review(conn, public_id)
        _guard(review, "reopen")
        row = conn.execute(
            """
            INSERT INTO result_reviews (
                tenant_id, result_id, public_id, round, state, review_due_at,
                action_rationale, assignee_user_id)
            VALUES (%s, %s, %s, %s, 'PENDING', %s, %s, %s)
            RETURNING *
            """,
            (
                current_tenant(),
                review.result_id,
                new_ulid("rrv"),
                review.round + 1,
                policy.review_due_at(now=moment),
                rationale,
                review.assignee_user_id,
            ),
        ).fetchone()
        assert row is not None
        return _row_to_review(row)


def expire_due_reviews(
    conn: psycopg.Connection[Any], *, now: datetime | None = None
) -> tuple[ReviewRow, ...]:
    """`PENDING -> EXPIRED` when `review_due_at` has elapsed. Platform, no principal.

    Only `PENDING` expires. `MOS-SAFE-059` gives `IN_REVIEW` a different fate on time --
    the 60-minute claim lease returns it to `PENDING` (`release_expired_claims`) -- and an
    `IN_REVIEW` row that expired outright would strand a reader mid-read and record
    `EXPIRED` against a review someone was in the middle of doing.
    """
    moment = now or datetime.now(UTC)
    with tenant_tx(conn):
        rows = conn.execute(
            """
            UPDATE result_reviews
               SET state = 'EXPIRED'
             WHERE state = 'PENDING'
               AND review_due_at IS NOT NULL
               AND review_due_at < %s
            RETURNING *
            """,
            (moment,),
        ).fetchall()
    return tuple(_row_to_review(r) for r in rows)


def release_expired_claims(
    conn: psycopg.Connection[Any], *, now: datetime | None = None
) -> tuple[ReviewRow, ...]:
    """`IN_REVIEW -> PENDING` after the 60-minute claim lease. `MOS-SAFE-059`.

    The same shape as `medos.db.queue`'s lease reclaim, and for the same reason: a reader
    who closed the tab must not hold a review forever.
    """
    moment = now or datetime.now(UTC)
    with tenant_tx(conn):
        rows = conn.execute(
            """
            UPDATE result_reviews
               SET state = 'PENDING', reviewer_user_id = NULL, claimed_at = NULL
             WHERE state = 'IN_REVIEW'
               AND claimed_at IS NOT NULL
               AND claimed_at < %s
            RETURNING *
            """,
            (moment - CLAIM_LEASE,),
        ).fetchall()
    return tuple(_row_to_review(r) for r in rows)


def supersede_reviews_for_result(
    conn: psycopg.Connection[Any], *, result_id: str
) -> tuple[ReviewRow, ...]:
    """`PENDING | IN_REVIEW -> SUPERSEDED` when the Result is superseded. `MOS-SAFE-059`.

    Terminal rows are left alone: a review that was ACCEPTED before the result was
    superseded stays ACCEPTED, because it records what a human concluded about the object
    that existed at the time. Rewriting it to SUPERSEDED would erase a completed human act.
    """
    with tenant_tx(conn):
        rows = conn.execute(
            """
            UPDATE result_reviews
               SET state = 'SUPERSEDED'
             WHERE result_id = %s AND state IN ('PENDING','IN_REVIEW')
            RETURNING *
            """,
            (result_id,),
        ).fetchall()
    return tuple(_row_to_review(r) for r in rows)


# `MOS-SAFE-070`'s seven event names, keyed by the action that produces them. Derived from
# `medos.safety.review.TRANSITIONS` at import so the two cannot drift: `event_for` is
# called once per action with a state that action is legal from, which is exactly the
# reachability check a hand-written table would not perform.
_EVENT_BY_ACTION: dict[str, str] = {
    "create": event_for(None, "create"),
    "claim": event_for("PENDING", "claim"),
    "release": event_for("IN_REVIEW", "release"),
    "submit": event_for("IN_REVIEW", "submit"),
    "expire": event_for("PENDING", "expire"),
    "supersede": event_for("PENDING", "supersede"),
    "reopen": event_for("ACCEPTED", "reopen"),
}


def audit_detail(review: ReviewRow, action: str) -> dict[str, Any]:
    """The `AuditEvent` detail `MOS-SAFE-070` requires, minus the fields the caller owns.

    "`action` = the event name below, `resource` = `result_review_id`, carrying
    `result_id`, `job_id`, `service_version`, `reviewer_user_id`, `reviewer_role`."
    `job_id` and `service_version` are the caller's -- they come from the `results` row --
    and are merged in at the call site so this function cannot go stale against a schema it
    does not read.

    `action_rationale`, `modifications` and `rejection_reason` are NOT included. They are
    clinical free text written by a reviewer about a specific patient's images
    ("subdiaphragmatic ascites on the right"), and `MOS-SEC-105` keeps P2 content out of
    structured logs. The audit row records that the act happened and by whom; the content
    of the conclusion lives in the review row, behind `result.review.read`.
    """
    return {
        "event": _EVENT_BY_ACTION[action],
        "result_review_id": review.public_id,
        "result_id": review.result_id,
        "round": review.round,
        "state": review.state,
        "reviewer_user_id": review.reviewer_user_id,
        "reviewer_role": review.reviewer_role,
        "reviewer_class": review.reviewer_class,
    }
