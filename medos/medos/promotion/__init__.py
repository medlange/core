# SPDX-License-Identifier: Apache-2.0
"""The three human acts, the five promotion steps, the rollback window. Chapter 17 §17.10.

THIS PACKAGE IS DELIBERATELY NOT INSIDE `medos/medos/training/`
----------------------------------------------------------
`MOS-TRAIN-189`: "There MUST be no path -- no function, no API call, no scheduled task, no
policy -- from a `TrainingRun`, a `ConversionRun`, an `EvaluationRun` or a
`ValidationReport` to a `state = SERVING` `Deployment` that does not pass through steps 4
and 5 of `MOS-TRAIN-181`. CI MUST assert this as a call-graph property: no symbol reachable
from the pipeline package transitively reaches the deployment role-mutation or
`clinical_use_mode` transition functions."

A call-graph property is only assertable if the two halves are separable, so they are two
packages: `medos.training` produces candidates and imports nothing from here, and
`medos.promotion` consumes them and is imported by no module of `medos.training`.
`tests/integration/test_training_run.py` walks the import closure of `medos.training` and
fails if this package, or `medos.evidence.deployment`, appears in it.

`medos/medos/promotion/` rather than a module inside `medos/medos/evidence/`: CONTRACT.md
section 0 says to define what a file does not cover only inside one's own module, and the
deployment state machine of 0008 is shared with the capability-resolution work of this
release. A separate package is the smaller claim.

WHAT IS HERE

    acts      `MOS-TRAIN-173`'s A1/A2/A3, `MOS-TRAIN-181`'s five steps, `MOS-TRAIN-184`'s
              refusal of `auto_promote`, `MOS-TRAIN-185` to `MOS-TRAIN-188`'s rollback,
              and `assert_human()` -- the write-time check `MOS-TRAIN-152` requires.
    dossier   `MOS-TRAIN-176`'s fifteen items, rendered offline and computing nothing
              (`MOS-TRAIN-178`).

WHAT IS NOT, AND WILL NOT BE

    No `auto_promote()`. No `promote_best()`. No bulk approval: `MOS-TRAIN-232` requires
    `artifact.approve` to "accept exactly one `model_version_id` per call" and forbids "a
    bulk-approval endpoint, a collection-valued approval body, an 'approve all passing'
    control, a saved filter that approves on match, or an automated promotion of the best
    of a sweep". Every function here that approves or promotes takes a scalar id, and the
    integration test asserts the signatures.

Spec: MOS-TRAIN-173 to MOS-TRAIN-189, MOS-REG-076 to MOS-REG-080, MOS-EVID-090 to
MOS-EVID-093, MOS-SAFE-036, MOS-SAFE-037.
"""

from __future__ import annotations

from medos.promotion.acts import (
    PROMOTION_STEPS,
    THREE_ACTS,
    ApprovalRefused,
    PromotionError,
    approve_artifact,
    approve_clinical_use,
    assert_human,
    assert_rollback_window_affordable,
    create_candidate_deployment,
    cutover,
    rollback,
    verify_candidate,
)

__all__ = [
    "PROMOTION_STEPS",
    "THREE_ACTS",
    "ApprovalRefused",
    "PromotionError",
    "approve_artifact",
    "approve_clinical_use",
    "assert_human",
    "assert_rollback_window_affordable",
    "create_candidate_deployment",
    "cutover",
    "rollback",
    "verify_candidate",
]
