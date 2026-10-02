# SPDX-License-Identifier: Apache-2.0
"""`ResultReview` -- the state machine, the permissions and the event names.

docs/spec/09-clinical-safety.md section 9.6 owns this entity: `MOS-SAFE-057` fixes the
posture, `MOS-SAFE-058` the field contract, `MOS-SAFE-059` the transitions, `MOS-SAFE-060`
the append-only rule, `MOS-SAFE-061` the `review_mode` enum, `MOS-SAFE-062` the derived
projection, `MOS-SAFE-063` the closed side-effect list, `MOS-SAFE-067` the permissions,
`MOS-SAFE-068` the `reviewer_class` derivation and `MOS-SAFE-070` the event names.
docs/spec/15-delivery.md section 15.1.2's 0.2.0 row names `ResultReview` as a deliverable.

THE OPEN QUESTION THIS MODULE DOES NOT RESOLVE
-----------------------------------------------
Chapter 16 carries OQ-01 -- which of the two meanings of "human in the loop" the product
adopts -- as an OPEN question, and `MOS-SAFE-057` settles a DEFAULT over it without closing
it: MedicalOS 0.2 adopts "Interpretation A with recorded review". Results are stored,
marked and visible; `ResultReview` records what a human concluded; it is "never a
precondition for storage or for visibility".

So this module implements the structural half and takes no position on the question.
Concretely:

  * Nothing here gates storage, visibility, or a read path on a review state. There is no
    function that answers "may this result be shown". `results.review_status` is a
    projection (`MOS-SAFE-062`) and a label, not a filter.
  * `mandatory_pre_publication` -- the name Interpretation B would be expressed under --
    is RESERVED and REFUSED (`MOS-SAFE-061`), in `medos/medos/safety/policy.py`, with
    `class: not_implemented` and a pointer at chapter 16. Refusing it is what keeps the
    question open: a half-built gating path would settle OQ-01 by accident, in code,
    without anyone deciding.
  * `SIDE_EFFECTS` below states `MOS-SAFE-063`'s closed list as data, and
    `tests/integration/test_safety_envelope.py` asserts the negative half of it -- a full
    review cycle produces zero `jobs` rows and zero STOW-RS calls -- which is the test
    `MOS-SAFE-063` requires chapter 14 to include.

WHY THE STATE MACHINE IS A TABLE AND NOT A METHOD PER TRANSITION
-----------------------------------------------------------------
`MOS-SAFE-059` says "The state machine is exactly:" and then gives eight rows. A table that
can be compared to those eight rows by eye -- and by a test -- is auditable; eight methods
with `if self.state == ...` at the top are not. `TRANSITIONS` is that table, keyed
`(from_state, action)`, and `permitted` is the only thing that reads it.

REVIEWER CLASS: WHY IT IS `non_clinical` FOR EVERY PRINCIPAL IN THIS RELEASE
----------------------------------------------------------------------------
`MOS-SAFE-068`: `reviewer_class = clinical` "if and only if BOTH hold: the submitting
principal is a `User` (not a `ServiceAccount`, not an `ApiKey`), AND the `Role` named by
`reviewer_role` is one the principal currently holds and carries `clinically_qualified =
true` (MOS-SAFE-104). Every other case ... MUST be recorded `reviewer_class =
non_clinical`."

This deployment has no `roles` table -- 0004_auth shipped without one and chapter 8 owns
it. A principal therefore holds no roles, so the second conjunct is false for everyone, so
`non_clinical` is the answer the requirement itself prescribes. That is not a stub: it is
the correct evaluation of the rule against the state of the system. `RoleDirectory` is the
port through which the answer will change when chapter 8's table lands, and
`NoRoleDirectory` is the driver that says "this principal holds no roles" rather than
"assume yes".

`MOS-SAFE-104` is honoured in the negative and that is the load-bearing part:
`clinically_qualified` is "the only input by which the platform may decide that a review
counts as a qualified human read", and the platform "MUST NOT derive that decision from
the role `key`, the `display_name`, a job-title string, or a hardcoded list of keys".
There is no list of role keys anywhere in this module, no `RADIOLOGIST`, no `CLINICIAN`,
and a test greps for them.

Spec: MOS-SAFE-057..072, MOS-SAFE-104, MOS-SEC-038, MOS-STORE-233, CONTRACT.md section 3.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol

__all__ = [
    "ReviewState",
    "ReviewAction",
    "SubmitOutcome",
    "ReviewerClass",
    "RejectionReason",
    "STATES",
    "TERMINAL_STATES",
    "SUBMIT_OUTCOMES",
    "REJECTION_REASONS",
    "TRANSITIONS",
    "PERMISSIONS",
    "EVENT_NAMES",
    "SIDE_EFFECTS",
    "CLAIM_LEASE",
    "MIN_RATIONALE_CHARS",
    "InvalidTransition",
    "SubmissionInvalid",
    "RoleDirectory",
    "NoRoleDirectory",
    "permitted",
    "next_state",
    "permission_for",
    "event_for",
    "reviewer_class_for",
    "validate_submission",
    "may_emit_verified_sr",
]

ReviewState = Literal[
    "PENDING", "IN_REVIEW", "ACCEPTED", "MODIFIED", "REJECTED", "EXPIRED", "SUPERSEDED"
]
ReviewAction = Literal[
    "create", "claim", "release", "submit", "expire", "supersede", "reopen"
]
SubmitOutcome = Literal["ACCEPTED", "MODIFIED", "REJECTED"]
ReviewerClass = Literal["clinical", "non_clinical"]
RejectionReason = Literal[
    "false_positive",
    "false_negative",
    "wrong_laterality",
    "mis_segmentation",
    "measurement_implausible",
    "wrong_series_analysed",
    "out_of_intended_use",
    "other",
]

STATES: tuple[ReviewState, ...] = (
    "PENDING",
    "IN_REVIEW",
    "ACCEPTED",
    "MODIFIED",
    "REJECTED",
    "EXPIRED",
    "SUPERSEDED",
)

# `MOS-SAFE-059`: "`ACCEPTED`, `MODIFIED`, `REJECTED`, `EXPIRED` and `SUPERSEDED` are
# terminal for that row; a review is corrected by a new round, never by mutating a
# submitted row."
TERMINAL_STATES: frozenset[str] = frozenset(
    {"ACCEPTED", "MODIFIED", "REJECTED", "EXPIRED", "SUPERSEDED"}
)

SUBMIT_OUTCOMES: frozenset[str] = frozenset({"ACCEPTED", "MODIFIED", "REJECTED"})

REJECTION_REASONS: frozenset[str] = frozenset(
    {
        "false_positive",
        "false_negative",
        "wrong_laterality",
        "mis_segmentation",
        "measurement_implausible",
        "wrong_series_analysed",
        "out_of_intended_use",
        "other",
    }
)

# `MOS-SAFE-058`: `action_rationale` is "REQUIRED, >= 20 chars, for `MODIFIED` and
# `REJECTED`", and `MOS-SAFE-066` requires one on reopen. Mirrored by the two CHECK
# constraints in `0010_safety.up.sql`; the constraint is the guarantee and this is the
# 422 with a JSON Pointer.
MIN_RATIONALE_CHARS = 20

# `MOS-SAFE-059`: "`IN_REVIEW` -> `PENDING` | `POST .../release` or claim lease expiry
# (60 min)".
CLAIM_LEASE = timedelta(minutes=60)


@dataclass(frozen=True)
class _Transition:
    to: ReviewState
    permission: str | None  # None == "platform", i.e. no principal performs it
    event: str
    new_round: bool = False


# ---------------------------------------------------------------------------------------
# `MOS-SAFE-059`'s eight rows, keyed `(from_state, action)`. `from_state` is None for the
# platform's creation row ("--" in the requirement's table).
#
# Reading it against the requirement, row for row:
#   --          -> PENDING     platform, on Result creation when review_mode != off
#   PENDING     -> IN_REVIEW   POST .../claim      result.review.submit
#   PENDING     -> EXPIRED     review_due_at elapsed, platform
#   PENDING     -> SUPERSEDED  the Result is superseded, platform
#   IN_REVIEW   -> A|M|R       POST .../submit     result.review.submit
#   IN_REVIEW   -> PENDING     POST .../release or lease expiry  result.review.submit
#   IN_REVIEW   -> SUPERSEDED  the Result is superseded, platform
#   terminal-4  -> round+1     POST .../reopen     result.review.reopen
#
# `submit` maps to a PLACEHOLDER `to` of "ACCEPTED": the actual terminal state is the
# caller's `action` argument, constrained to `SUBMIT_OUTCOMES`, and `next_state` returns
# it. Encoding three rows would triple the table for one branch that `validate_submission`
# already has to police.
# ---------------------------------------------------------------------------------------
TRANSITIONS: Mapping[tuple[str | None, str], _Transition] = {
    (None, "create"): _Transition("PENDING", None, "result.review.created"),
    ("PENDING", "claim"): _Transition(
        "IN_REVIEW", "result.review.submit", "result.review.claimed"
    ),
    ("PENDING", "expire"): _Transition("EXPIRED", None, "result.review.expired"),
    ("PENDING", "supersede"): _Transition(
        "SUPERSEDED", None, "result.review.superseded"
    ),
    ("IN_REVIEW", "submit"): _Transition(
        "ACCEPTED", "result.review.submit", "result.review.submitted"
    ),
    ("IN_REVIEW", "release"): _Transition(
        "PENDING", "result.review.submit", "result.review.released"
    ),
    ("IN_REVIEW", "supersede"): _Transition(
        "SUPERSEDED", None, "result.review.superseded"
    ),
    ("ACCEPTED", "reopen"): _Transition(
        "PENDING", "result.review.reopen", "result.review.reopened", new_round=True
    ),
    ("MODIFIED", "reopen"): _Transition(
        "PENDING", "result.review.reopen", "result.review.reopened", new_round=True
    ),
    ("REJECTED", "reopen"): _Transition(
        "PENDING", "result.review.reopen", "result.review.reopened", new_round=True
    ),
    ("EXPIRED", "reopen"): _Transition(
        "PENDING", "result.review.reopen", "result.review.reopened", new_round=True
    ),
}

# `MOS-SAFE-067`, verbatim. "These permission names are normative; Chapter 8 owns the
# role->permission mapping." Every one matches `MOS-SEC-031`'s grammar
# `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$`, which `medos.security.scopes.validate_scope`
# enforces at issuance -- so a credential can actually carry them.
PERMISSIONS: Mapping[str, str] = {
    "result.review.read": "read review rows and history",
    "result.review.submit": "claim, release, submit a review",
    "result.review.assign": "set assignee_user_id",
    "result.review.reopen": "open a new round on a terminal review",
    "result.read.research": "see research_only results in list endpoints (section 9.4 E5)",
    "deployment.approve_clinical": "be the named approver in the E1 gate (MOS-SAFE-036)",
}

# `MOS-SAFE-070`, verbatim. "No new event-bus topic is created; these are audit and
# webhook events only."
EVENT_NAMES: frozenset[str] = frozenset(
    {
        "result.review.created",
        "result.review.claimed",
        "result.review.released",
        "result.review.submitted",
        "result.review.expired",
        "result.review.reopened",
        "result.review.superseded",
    }
)

# `MOS-SAFE-063`'s closed list, as data. "The **only** permitted side effects of any
# `ResultReview` transition are: an `AuditEvent`, a webhook delivery, a metric increment,
# and -- for `ACCEPTED`/`MODIFIED` in `clinical` mode when
# `Deployment.emit_verified_sr_on_accept` is `true` (default `false`) -- issuance of a new
# SR revision per MOS-SAFE-065. No transition may enqueue a Job, alter a stored DICOM
# object, notify a patient, write to any external clinical system, retrain anything, or
# change a Deployment."
SIDE_EFFECTS: tuple[str, ...] = (
    "audit_event",
    "webhook_delivery",
    "metric_increment",
    "verified_sr_revision",  # conditional; see `may_emit_verified_sr`
)

FORBIDDEN_SIDE_EFFECTS: tuple[str, ...] = (
    "enqueue_job",
    "alter_stored_dicom_object",
    "notify_patient",
    "write_external_clinical_system",
    "retrain",
    "change_deployment",
)


class InvalidTransition(ValueError):
    """A transition `MOS-SAFE-059`'s table does not contain.

    A `ValueError` and not a `MedosError`: the API layer renders it as a 409 with the
    current state, and a review transition is never a job state, so it must not be
    reachable through the `ClinicalRejection` / `SystemFailure` hierarchy that decides
    whether a JOB is `REJECTED` or `FAILED`.
    """

    def __init__(self, state: str | None, action: str) -> None:
        super().__init__(
            f"MOS-SAFE-059 has no transition from {state or '--'} on {action!r}; "
            f"available from {state or '--'}: "
            f"{sorted(a for (s, a) in TRANSITIONS if s == state)}"
        )
        self.state = state
        self.action = action


class SubmissionInvalid(ValueError):
    """A submit body missing a field `MOS-SAFE-058` makes conditionally required.

    Carries `pointers` so `MOS-SAFE-069`'s "422 on missing conditional fields" can be an
    RFC 9457 `violations[]` with a JSON Pointer per field rather than one sentence.
    """

    def __init__(self, problems: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in sorted(problems.items())))
        self.problems = dict(problems)

    @property
    def pointers(self) -> list[dict[str, str]]:
        return [
            {"pointer": f"/{field}", "detail": detail}
            for field, detail in sorted(self.problems.items())
        ]


def permitted(state: str | None, action: str) -> _Transition:
    """The `MOS-SAFE-059` row for `(state, action)`, or raise `InvalidTransition`."""
    try:
        return TRANSITIONS[(state, action)]
    except KeyError:
        raise InvalidTransition(state, action) from None


def next_state(state: str | None, action: str, *, outcome: str | None = None) -> ReviewState:
    """The state after `action`. `outcome` names the terminal state for `submit`."""
    transition = permitted(state, action)
    if action != "submit":
        return transition.to
    if outcome not in SUBMIT_OUTCOMES:
        raise SubmissionInvalid(
            {"action": f"MUST be one of {sorted(SUBMIT_OUTCOMES)}, got {outcome!r}"}
        )
    return outcome  # type: ignore[return-value]


def permission_for(state: str | None, action: str) -> str | None:
    """The permission `MOS-SAFE-059` requires, or None when the actor is the platform."""
    return permitted(state, action).permission


def event_for(state: str | None, action: str) -> str:
    """The `MOS-SAFE-070` event name for this transition."""
    return permitted(state, action).event


# =======================================================================================
# MOS-SAFE-068 / MOS-SAFE-104
# =======================================================================================
class RoleDirectory(Protocol):
    """The one input `MOS-SAFE-104` permits for "is this a qualified human read".

    `holds_clinically_qualified_role(principal_kind, principal_id, role_key)` answers the
    SECOND conjunct of `MOS-SAFE-068` -- "the `Role` named by `reviewer_role` is one the
    principal currently holds and carries `clinically_qualified = true`" -- and only that.
    It is deliberately not `roles_of(principal)`: a caller handed a list of roles is a
    caller that can filter it by name, and `MOS-SAFE-104` forbids deciding from the role
    key. The narrow question has only one honest implementation.
    """

    def holds_clinically_qualified_role(
        self, *, principal_kind: str, principal_id: str, role_key: str
    ) -> bool: ...


class NoRoleDirectory:
    """The driver for a deployment with no `roles` table. Answers False, always.

    Not a stub and not a TODO. `MOS-SAFE-068` enumerates the cases that MUST be recorded
    `non_clinical` and "a role the principal does not hold" is one of them; with no roles
    table, no principal holds any role, so False is the evaluated answer. When chapter 8's
    `roles` / `user_roles` tables land, a `PostgresRoleDirectory` replaces this and every
    call site is already in the right place.

    The alternative -- defaulting to True, or deriving qualification from the scope a
    credential happens to carry -- would let a service account produce a `VERIFIED` SR,
    which `MOS-SAFE-068` closes with: "Machine review is not human review; the platform
    MUST refuse to let a service account produce a `VERIFIED` SR under any configuration."
    """

    def holds_clinically_qualified_role(
        self, *, principal_kind: str, principal_id: str, role_key: str
    ) -> bool:
        return False


def reviewer_class_for(
    *,
    principal_kind: str,
    principal_id: str,
    reviewer_role: str | None,
    roles: RoleDirectory | None = None,
) -> ReviewerClass:
    """`MOS-SAFE-068`, as a conjunction of exactly the two conditions it names.

    Nothing in this function reads the SPELLING of `reviewer_role`. It is passed to the
    directory as an opaque key and the directory answers about the attribute. That is
    `MOS-SAFE-104`'s prohibition made structural: there is no branch here that could
    consult a list of job titles, because the string never meets a comparison.
    """
    if principal_kind != "user":
        return "non_clinical"
    if not reviewer_role:
        return "non_clinical"
    directory = roles or NoRoleDirectory()
    qualified = directory.holds_clinically_qualified_role(
        principal_kind=principal_kind,
        principal_id=principal_id,
        role_key=reviewer_role,
    )
    return "clinical" if qualified else "non_clinical"


def may_emit_verified_sr(
    *,
    state: str,
    reviewer_class: str,
    clinical_use_mode: str,
    emit_verified_sr_on_accept: bool,
) -> bool:
    """`MOS-SAFE-065` gated by `MOS-SAFE-068` and `MOS-SAFE-042`. All four must hold.

    Returns a boolean and issues nothing: writing the SR revision is chapter 4's
    `MOS-IMG-062` with the `output_index` `MOS-IMG-065` allocates for review round n, and
    that allocation does not exist in this release. The GATE exists now so that the writer,
    when it lands, cannot be wired up without passing through it -- and so that the
    property `MOS-SAFE-068` closes with is testable today: no configuration makes this
    return True for a service account.
    """
    if not emit_verified_sr_on_accept:
        return False
    if state not in ("ACCEPTED", "MODIFIED"):
        return False
    if reviewer_class != "clinical":
        return False
    # MOS-SAFE-042: "In `research_only` mode this is forbidden."
    return str(clinical_use_mode).strip().lower() == "clinical"


def validate_submission(
    *,
    outcome: str,
    action_rationale: str | None,
    modifications: object | None,
    rejection_reason: str | None,
) -> None:
    """`MOS-SAFE-058`'s conditional requirements. Raises `SubmissionInvalid` with pointers.

    Mirrors the two CHECK constraints in `0010_safety.up.sql`. Two layers on purpose, the
    same way `medos.security.scopes` mirrors the `api_keys_scope_grammar` CHECK: the
    constraint is the guarantee that no path writes a bad row, and this is the 422 that
    tells the caller which field, which `MOS-SAFE-069` requires ("422 on missing
    conditional fields").
    """
    problems: dict[str, str] = {}
    if outcome not in SUBMIT_OUTCOMES:
        raise SubmissionInvalid(
            {"action": f"MUST be one of {sorted(SUBMIT_OUTCOMES)}, got {outcome!r}"}
        )

    needs_rationale = outcome in ("MODIFIED", "REJECTED")
    rationale = (action_rationale or "").strip()
    if needs_rationale and len(rationale) < MIN_RATIONALE_CHARS:
        problems["action_rationale"] = (
            f"REQUIRED for {outcome} and MUST be at least {MIN_RATIONALE_CHARS} "
            f"characters (MOS-SAFE-058); got {len(rationale)}"
        )
    if outcome == "MODIFIED" and not modifications:
        problems["modifications"] = (
            "REQUIRED for MODIFIED: an RFC 6902 patch against the Result.findings "
            "document (MOS-SAFE-058). A MODIFIED review with no patch records that "
            "something was changed without recording what"
        )
    if outcome == "REJECTED":
        if not rejection_reason:
            problems["rejection_reason"] = (
                f"REQUIRED for REJECTED; one of {sorted(REJECTION_REASONS)} "
                "(MOS-SAFE-058)"
            )
        elif rejection_reason not in REJECTION_REASONS:
            problems["rejection_reason"] = (
                f"{rejection_reason!r} is not in the closed set "
                f"{sorted(REJECTION_REASONS)} (MOS-SAFE-058)"
            )
    if outcome == "ACCEPTED":
        if modifications:
            problems["modifications"] = (
                "MUST be absent for ACCEPTED: a review that patched the findings is "
                "MODIFIED, not ACCEPTED (MOS-SAFE-058)"
            )
        if rejection_reason:
            problems["rejection_reason"] = "MUST be absent for ACCEPTED"
    if problems:
        raise SubmissionInvalid(problems)
