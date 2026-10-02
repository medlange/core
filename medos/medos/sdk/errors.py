# SPDX-License-Identifier: Apache-2.0
"""Refusals the training-corpus side raises, on the evidence plane's error model.

`Refusal` itself is NOT redefined here. `medos.sdk.refusal.Refusal` already is the
form a report reproduces verbatim (`MOS-TRAIN-091`, `MOS-EVID-036` key their waiver
objects on its `check_id`), and a second refusal record with the same shape and a
different name would mean a `ValidationReport` citing a cohort had to merge two
vocabularies to say why the cohort nearly did not exist. CONTRACT.md section 0: define
only what this module owns.

What this module owns is the three refusals chapter 17 raises BEFORE a seal is reachable:

  `HarvestRefused`    `MOS-TRAIN-072` / `MOS-TRAIN-075` / `MOS-TRAIN-077` -- the tenant
                      may not contribute this data to training at all. `MOS-TRAIN-072`
                      fixes the wire form: "`403` and problem type
                      `training-use-not-permitted`". It is the ONE refusal in this package
                      whose status is 403 rather than 422, because it is a permission
                      answer and not a clinical one.
  `CurationRefused`   `MOS-TRAIN-080` / `MOS-TRAIN-203` / `MOS-TRAIN-204` -- the queue
                      refuses a decision, or a vocabulary, or a draw.
  `ChainRefused`      `MOS-TRAIN-132` -- the transform-chain generator cannot represent a
                      `PreprocessingSpec` field exactly, and MUST NOT substitute the
                      nearest available option.

Release 0.3.0 (`MOS-TRAIN-190`) adds four more, all of the same shape and all raised
BEFORE the expensive thing happens rather than after it, on `MOS-TRAIN-144`'s argument:
"Failing in the first second of a two-hour GPU run is materially cheaper than failing at
the gate."

  `RunRefused`        `MOS-TRAIN-115` / `MOS-TRAIN-124` / `MOS-TRAIN-125` /
                      `MOS-TRAIN-211` -- the split leaks, the binding is incomplete, the
                      tree is dirty, or a `label` capability was hand-configured with no
                      rationale.
  `PartitionForbidden` `MOS-TRAIN-141` / `MOS-TRAIN-214` -- the cohort resolver was asked
                      for `test` by a principal that may not read it. **403, not an empty
                      set**: "An empty set is indistinguishable from 'no such patient',
                      and a retry loop around an empty set is how a silent read becomes a
                      routine one."
  `SearchRefused`     `MOS-TRAIN-216` / `MOS-TRAIN-220` / `MOS-TRAIN-234` -- a second
                      nomination, a space that is code, a space that searches a quantity
                      the fingerprint froze, or a budget with an absent bound.
  `ConversionRefused` `MOS-TRAIN-160` to `MOS-TRAIN-166` -- the conversion did not hold
                      E1/E2/E3 on every case, or it changed something a conversion may
                      not change.

Spec: MOS-TRAIN-072, MOS-TRAIN-075, MOS-TRAIN-077, MOS-TRAIN-080, MOS-TRAIN-115,
MOS-TRAIN-124, MOS-TRAIN-125, MOS-TRAIN-132, MOS-TRAIN-141, MOS-TRAIN-160 to
MOS-TRAIN-166, MOS-TRAIN-203, MOS-TRAIN-204, MOS-TRAIN-211, MOS-TRAIN-214,
MOS-TRAIN-216, MOS-TRAIN-220, MOS-TRAIN-234, MOS-EVID-036.
"""

from __future__ import annotations

from typing import Any

from medos.sdk.refusal import Refusal, RefusalError

__all__ = [
    "Refusal",
    "TrainingError",
    "HarvestRefused",
    "CurationRefused",
    "ChainRefused",
    "RunRefused",
    "PartitionForbidden",
    "SearchRefused",
    "ConversionRefused",
]


class TrainingError(RuntimeError):
    """Base for anything this package raises that is not a refusal."""


class HarvestRefused(RefusalError):
    """The tenant may not contribute this data to a training corpus.

    `MOS-TRAIN-072`: "When it is false, the harvest MUST refuse, with `403` and problem
    type `training-use-not-permitted`. There is no per-study, per-user or per-environment
    override, and no platform-administrator bypass." The absence of an override is the
    requirement; a keyword argument named `force` on any function in this package would
    be the bypass the sentence forbids, so there is none.
    """

    problem_type = "https://medicalos.dev/problems/training-use-not-permitted"
    title = "training use is not permitted for this tenant"

    def as_problem(self) -> dict[str, Any]:
        """`MOS-TRAIN-072`'s 403, and `class` is an authorisation answer, not a clinical one.

        The distinction is the one CONTRACT.md section 9 draws and it survives here: a
        clinician whose cohort was refused for C1 must change the cohort; an operator
        whose harvest was refused for a missing `TrainingDataPolicy` must go and record an
        instrument, and no amount of changing the cohort will help.
        """
        problem = super().as_problem()
        problem["status"] = 403
        problem["class"] = "authorization"
        return problem


class CurationRefused(RefusalError):
    """A curation-queue operation is refused. `MOS-TRAIN-080`, `MOS-TRAIN-203/204`."""

    problem_type = "https://medicalos.dev/problems/curation-refused"
    title = "the curation operation was refused"


class ChainRefused(RefusalError):
    """The generator cannot emit a chain for this `PreprocessingSpec`. `MOS-TRAIN-132`.

    "The generator MUST refuse to emit a chain for any `PreprocessingSpec` field it cannot
    represent exactly, and MUST NOT substitute the nearest available option." The refusal
    names the field, because the failure this prevents -- a training chain that differs
    from the serving one -- surfaces months later as an unexplained gap between
    evaluation-cohort and production performance that nobody can localise.
    """

    problem_type = "https://medicalos.dev/problems/transform-chain-refused"
    title = "the transform chain was not generated"


class RunRefused(RefusalError):
    """A `TrainingRun` MUST NOT be submitted, or MUST NOT reach `SUCCEEDED`.

    Four unrelated-looking causes, one shape, because they are all the same statement:
    the GPU time would have been spent producing a candidate nobody can defend.

    `MOS-TRAIN-115`/`MOS-TRAIN-116`: a leaking split blocks the run "before the first
    batch is loaded", and a `MOS-EVID-036` waiver does not permit it -- the waiver is a
    reporting instrument, not an authorisation.
    `MOS-TRAIN-124`: "A run missing any field below MUST NOT reach `state = SUCCEEDED`,
    and its output MUST NOT be registrable as a `ModelVersion`."
    `MOS-TRAIN-125`: `code_dirty = true` blocks `SUCCEEDED`.
    `MOS-TRAIN-211`: a hand-configured backend on a `label` capability with no rationale
    leaves the first question at the approval gate unanswerable.
    """

    problem_type = "https://medicalos.dev/problems/training-run-refused"
    title = "the training run was refused"


class PartitionForbidden(RefusalError):
    """The `test` partition was requested by a principal that may not read it.

    `MOS-TRAIN-141`: "The cohort resolver MUST be handed a split view filtered to
    `{train, tune}` and MUST return **403, not an empty set**, on a `test` request."
    `MOS-TRAIN-214` extends it to "the search driver, each trial process, the ranking
    step, the ensemble builder and any interactive session attached to the search's
    workspace."

    The status is 403 and the class is `authorization`, for the same reason
    `HarvestRefused` is: a caller who receives an empty set will change the query, and a
    caller who receives a 403 will go and read the rule.
    """

    problem_type = "https://medicalos.dev/problems/partition-not-readable"
    title = "that partition is not readable by this principal"

    def as_problem(self) -> dict[str, Any]:
        problem = super().as_problem()
        problem["status"] = 403
        problem["class"] = "authorization"
        return problem


class SearchRefused(RefusalError):
    """A `ConfigurationSearch` MUST NOT be recorded, or MUST NOT nominate.

    `MOS-TRAIN-216`: "A `ConfigurationSearch` MUST nominate **exactly one** trial."
    `MOS-TRAIN-220`: the space "MUST NOT be a Python callable, an expression string, a
    lambda or a template evaluated at search time" -- a space that is code has no digest
    that means anything -- and "a field named in `derived_from_fingerprint` MUST NOT also
    appear as a search axis".
    `MOS-TRAIN-234`: "an absent bound is a registration error, not an unlimited one."
    """

    problem_type = "https://medicalos.dev/problems/configuration-search-refused"
    title = "the configuration search was refused"


class ConversionRefused(RefusalError):
    """A `ConversionRun` MUST NOT be registered. `MOS-TRAIN-160` to `MOS-TRAIN-166`.

    `MOS-TRAIN-164`: "A conversion failing any tolerance MUST NOT be registered. The
    remedy is a different conversion -- another precision class, another opset, a
    different plugin or layer-fusion configuration -- never a relaxed tolerance on that
    version. A tolerance relaxed to admit one artifact silently relaxes it for every
    future artifact of that family."

    So there is no `tolerances=` argument on any function in `medos.training.conversion`
    that widens a bound, and no keyword that skips a case. A `ModelVersion` MAY declare
    TIGHTER values and MUST NOT declare looser ones (`MOS-TRAIN-162`), which is a
    comparison this package performs and never a value it accepts.
    """

    problem_type = "https://medicalos.dev/problems/conversion-refused"
    title = "the conversion was refused"
