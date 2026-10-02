# SPDX-License-Identifier: Apache-2.0
"""The kind mapping and the lifecycle graph. `MOS-REG-020`, `MOS-REG-021`, `MOS-REG-022`.

ONE statement of the graph, in Python, mirrored by ONE statement in SQL
----------------------------------------------------------------------
`0012_artifacts.up.sql` carries the same graph as a `BEFORE UPDATE` trigger, because a
graph enforced only in the application is a graph that a `psql` session walks straight
past -- and a `RECALLED` version that someone quietly set back to `APPROVED` is a recalled
device back in clinical use. The trigger is the guarantee; this module is the good error
message and the thing the API can consult before it writes.
`tests/integration/test_artifacts.py::test_python_and_sql_agree_on_the_lifecycle_graph`
asserts the two are the same graph, edge for edge, so the duplication cannot drift.

TWO KIND VOCABULARIES, AND WHICH COLUMN CARRIES WHICH
-----------------------------------------------------
Chapter 6 writes VERSION-level kinds (`service_version`, `model_version`, ...); chapter 12
`MOS-STORE-253` stores the FAMILY-level kind (`service`, `model`, ...) in
`artifacts.kind`, and fixes the mapping as "the identity plus the `_version`/`_spec`/`_set`
suffix". Manifests, API paths and error messages speak chapter 6's vocabulary; the column
speaks chapter 12's. `FAMILY_KIND` and `VERSION_KIND` are that mapping and there is no
second copy of it.

`MOS-REG-038` -- `lifecycle_status` is lifecycle-ONLY. `STAGING`, `DEPLOYED` and
`PRODUCTION` are not in any set below and MUST NOT be added: whether a version is live is
`Deployment`'s answer (`MOS-REG-072`), and a registry that also answers it gives two
answers that will eventually disagree.

Spec: MOS-REG-020, MOS-REG-021, MOS-REG-022, MOS-REG-038, MOS-REG-109, MOS-STORE-253,
MOS-EVID-014 (who originates `SEALED`/`DEFECTIVE`). Pure: no I/O, no clock.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "EXECUTABLE_STATUSES",
    "FAMILY_KIND",
    "LIFECYCLE_STATUSES",
    "SEALED_STATUSES",
    "TRANSITIONS",
    "TRANSITION_PERMISSION",
    "VERSION_KIND",
    "permission_for",
    "transition_allowed",
]

# `MOS-STORE-253`: identity plus the `_version` / `_spec` / `_set` suffix. `tool`, `agent`
# and `workflow` are absent on purpose -- section 15.2.6 forbids any of them being backed
# by a table, endpoint or executor before 0.4.0, and 0012 seeds no manifest schema for
# them, which makes the prohibition a foreign-key failure rather than a code review.
VERSION_KIND: Final[dict[str, str]] = {
    "service": "service_version",
    "model": "model_version",
    "preprocessing": "preprocessing_spec",
    "dataset": "dataset_version",
    "annotation": "annotation_set",
    "policy_set": "policy_set_version",
}
FAMILY_KIND: Final[dict[str, str]] = {v: k for k, v in VERSION_KIND.items()}

# `MOS-REG-020`, first row: the eight-value lifecycle of a versioned executable.
EXECUTABLE_STATUSES: Final[tuple[str, ...]] = (
    "DRAFT", "REGISTERED", "VALIDATING", "VALIDATED", "APPROVED", "DEPRECATED",
    "SUSPENDED", "RECALLED",
)
# Second row: no DRAFT/VALIDATING/VALIDATED for a spec, a workflow or a policy set --
# nothing evaluates them, so the evaluation states would be states nothing can leave.
_SPEC_STATUSES: Final[tuple[str, ...]] = (
    "REGISTERED", "APPROVED", "DEPRECATED", "SUSPENDED", "RECALLED",
)
# Third row: `MOS-EVID-014` sets and cascades these; the registry stores but never
# originates them.
SEALED_STATUSES: Final[tuple[str, ...]] = ("SEALED", "DEFECTIVE")

LIFECYCLE_STATUSES: Final[dict[str, tuple[str, ...]]] = {
    "service_version": EXECUTABLE_STATUSES,
    "model_version": EXECUTABLE_STATUSES,
    "preprocessing_spec": _SPEC_STATUSES,
    "workflow_version": _SPEC_STATUSES,
    "policy_set_version": _SPEC_STATUSES,
    "dataset_version": SEALED_STATUSES,
    "annotation_set": SEALED_STATUSES,
}

# `MOS-REG-021`'s table, read as edges. `SUSPENDED -> previous status` is not an edge with
# a fixed head and is handled by `transition_allowed` below.
TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "DRAFT": frozenset({"REGISTERED", "RECALLED"}),
    "REGISTERED": frozenset({"VALIDATING", "SUSPENDED", "RECALLED"}),
    "VALIDATING": frozenset({"VALIDATED", "REGISTERED", "SUSPENDED", "RECALLED"}),
    "VALIDATED": frozenset({"APPROVED", "DEPRECATED", "SUSPENDED", "RECALLED"}),
    "APPROVED": frozenset({"DEPRECATED", "SUSPENDED", "RECALLED"}),
    "DEPRECATED": frozenset({"SUSPENDED", "RECALLED"}),
    # `MOS-REG-021`: the only exits from SUSPENDED are the status it held before, and
    # RECALLED. The head of the first edge is a fact about history, so it is supplied by
    # the caller (the trigger reads it from `registry_changelog`).
    "SUSPENDED": frozenset({"RECALLED"}),
    # `MOS-REG-022`: RECALLED is irreversible. Not "discouraged" -- there is no edge.
    "RECALLED": frozenset(),
    # The evidence-plane pair (`MOS-EVID-014`).
    "SEALED": frozenset({"DEFECTIVE"}),
    "DEFECTIVE": frozenset(),
}

# `MOS-REG-021`'s permission column. `artifact.recall` is separate from
# `artifact.status.set` because `MOS-REG-109` requires it to be separately grantable:
# a recall is irreversible and has a patient-facing consequence, so it MUST NOT be
# reachable by the routine status-management permission.
TRANSITION_PERMISSION: Final[dict[str, str]] = {
    "REGISTERED": "artifact.publish",
    "VALIDATING": "evidence.run",
    "VALIDATED": "evidence.run",
    "APPROVED": "artifact.approve",
    "DEPRECATED": "artifact.status.set",
    "SUSPENDED": "artifact.suspend",
    "RECALLED": "artifact.recall",
    "DEFECTIVE": "evidence.run",
    "SEALED": "evidence.run",
}


def permission_for(to_status: str, *, from_status: str | None = None) -> str:
    """The permission `MOS-REG-021` requires for this transition.

    Un-suspension takes `artifact.suspend`, not the permission of the status being
    restored: the row's own table says so ("`SUSPENDED` | previous status |
    `artifact.suspend`"), and it is the right answer -- restoring a version is the same
    operator decision as suspending it, reversed.
    """
    if from_status == "SUSPENDED" and to_status != "RECALLED":
        return "artifact.suspend"
    if to_status == "REGISTERED" and from_status == "VALIDATING":
        return "evidence.run"  # "run failed or was abandoned"
    try:
        return TRANSITION_PERMISSION[to_status]
    except KeyError:  # pragma: no cover - callers validate the status first
        raise ValueError(f"{to_status!r} is not a lifecycle status") from None


def transition_allowed(
    kind: str, from_status: str, to_status: str, *, previous_status: str | None = None
) -> tuple[bool, str]:
    """`(allowed, reason_code)`. `reason_code` is `''` when allowed.

    `previous_status` is the status the row held before it was suspended, which is what
    `MOS-REG-021`'s "`SUSPENDED` -> previous status" edge points at. `None` means the
    caller could not establish one, and an un-suspension is then refused rather than
    guessed: guessing would let a suspended, never-approved version re-enter service as
    `APPROVED`.
    """
    version_kind = VERSION_KIND.get(kind, kind)
    permitted = LIFECYCLE_STATUSES.get(version_kind)
    if permitted is None:
        return False, "unknown_kind"
    if to_status not in permitted:
        return False, "status_not_permitted_for_kind"
    if from_status == to_status:
        return False, "no_op_transition"
    if from_status == "RECALLED":
        return False, "recall_is_irreversible"
    if from_status == "SUSPENDED" and to_status != "RECALLED":
        if previous_status is None:
            return False, "no_previous_status_recorded"
        if to_status != previous_status:
            return False, "suspension_restores_previous_status_only"
        return True, ""
    if to_status in TRANSITIONS.get(from_status, frozenset()):
        return True, ""
    return False, "transition_not_in_graph"
