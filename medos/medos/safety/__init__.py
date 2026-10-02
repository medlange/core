# SPDX-License-Identifier: Apache-2.0
"""`medos.safety` -- the three chapter 9 items release 0.2.0 names.

docs/spec/15-delivery.md section 15.1.2, the 0.2.0 row: "applicability envelopes; RUO
marking; `ResultReview`". One module per subject, plus the storage they need
(`medos/medos/db/migrations/0010_safety.up.sql`).

    envelope.py    ApplicabilityEnvelope: the declaration, the three zones, the
                   MOS-EVID-102 rejection object. Pure; imports no capability and no
                   database.
    attributes.py  The nine study attributes an envelope is evaluated against, derived
                   from DICOM headers by chapter 3's own rules (MOS-DATA-060).
    marking.py     MOS-SAFE-039's single shared verifier: in research_only the writer
                   refuses to emit an unmarked object. The 0.2.0 gate's `ruo-marking`.
    review.py      MOS-SAFE-058..070's state machine, permissions and event names. The
                   structural half of ResultReview; chapter 16's OQ-01 stays open.
    policy.py      The four Deployment facts chapter 9 reads, behind a port, because
                   chapter 6 owns the `deployments` table and it does not exist yet.
    repo.py        Row access for the three tables 0010 creates.

WHY THIS IS A PACKAGE AND NOT SPREAD ACROSS `worker/` AND `writer/`
--------------------------------------------------------------------
CONTRACT.md section 1 does not define a `safety/` package, and section 0 says to define
what is needed inside one's own module and REPORT it. This is that module, and the reason
it is one module rather than three edits is `MOS-SAFE-039`'s "a single function shared by
the SEG, SR and SC writers": a marking check that lives in `writer/seg.py` is a marking
check that gets copied into `writer/sr.py`, and the copy is what rots. The same argument
holds for the envelope -- `MOS-EVID-100` puts its evaluation beyond the service's reach,
and a pure module with no capability import is the enforceable form of that.

The three subjects are separate FILES because they are edited for separate reasons: a new
envelope attribute, a new DICOM marker, and a new review state have nothing to do with one
another, and a file that three unrelated changes touch is a file with three unrelated
merge conflicts.
"""

from __future__ import annotations

from medos.safety.attributes import StudyAttributes, attributes_from_paths, study_attributes
from medos.safety.envelope import (
    ApplicabilityEnvelope,
    EnumConstraint,
    EnvelopeDeclarationError,
    EnvelopeVerdict,
    EnvelopeViolation,
    RangeConstraint,
    envelope_digest,
    evaluate,
)
from medos.safety.marking import (
    MarkingAbsent,
    MarkingViolation,
    apply_research_marking,
    assert_marked,
    is_research_only,
    marking_violations,
    normalise_mode,
)
from medos.safety.policy import (
    DeploymentPolicySource,
    DeploymentSafetyPolicy,
    ReviewModeReserved,
    StaticDeploymentPolicySource,
    default_policy_source,
)
from medos.safety.review import (
    EVENT_NAMES,
    PERMISSIONS,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransition,
    NoRoleDirectory,
    RoleDirectory,
    SubmissionInvalid,
    may_emit_verified_sr,
    reviewer_class_for,
    validate_submission,
)

__all__ = [
    # envelope
    "ApplicabilityEnvelope",
    "EnvelopeDeclarationError",
    "EnvelopeVerdict",
    "EnvelopeViolation",
    "EnumConstraint",
    "RangeConstraint",
    "evaluate",
    "envelope_digest",
    # attributes
    "StudyAttributes",
    "study_attributes",
    "attributes_from_paths",
    # marking
    "MarkingAbsent",
    "MarkingViolation",
    "apply_research_marking",
    "assert_marked",
    "marking_violations",
    "is_research_only",
    "normalise_mode",
    # review
    "EVENT_NAMES",
    "PERMISSIONS",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "InvalidTransition",
    "NoRoleDirectory",
    "RoleDirectory",
    "SubmissionInvalid",
    "may_emit_verified_sr",
    "reviewer_class_for",
    "validate_submission",
    # policy
    "DeploymentPolicySource",
    "DeploymentSafetyPolicy",
    "ReviewModeReserved",
    "StaticDeploymentPolicySource",
    "default_policy_source",
]
