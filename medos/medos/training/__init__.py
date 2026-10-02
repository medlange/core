# SPDX-License-Identifier: Apache-2.0
"""The producing side of the evidence plane: curation, the chain, and the training run.

Chapter 17 is the model-development pipeline. `MOS-TRAIN-190` splits it across two
releases and this package now holds both halves.

RELEASE 0.2.0 -- everything that has to exist BEFORE a training run can be honest, which
is why it shipped a release earlier than the training run itself:

    errors       `HarvestRefused` / `CurationRefused` / `ChainRefused`, on
                 `medos.sdk.refusal.Refusal` so a `ValidationReport` citing a
                 cohort reproduces one refusal vocabulary and not two
    vocabulary   `MOS-TRAIN-080`'s decision and reason-code sets, and `MOS-TRAIN-204`'s
                 screen: a reason code MUST NOT mean a model outcome
    policy       `TrainingDataPolicy` and `tenants.training_use_allowed` -- MOS-TRAIN-072
                 to MOS-TRAIN-077. The legal basis is stored, digested and displayed, and
                 never assessed (`MOS-TRAIN-074`)
    curation     `SamplingPlan`, `HarvestBatch`/`CurationBatch`, `HarvestCandidate`,
                 `CurationDecision`, and `seal_from_batch` -- MOS-TRAIN-078 to
                 MOS-TRAIN-088, MOS-TRAIN-111, MOS-TRAIN-202 to MOS-TRAIN-209
    spec         `PreprocessingSpec`, parsed and validated (`MOS-IMG-049`)
    preprocess   `build_chain` -- the ONE constructor of a deterministic preprocessing
                 chain (`MOS-TRAIN-034`) -- and `record_golden`, the ONE recorder of
                 `golden_fixture.output_tensor_sha256` (`MOS-TRAIN-134`, `MOS-IMG-058`)
    chain        the generator: spec -> `configs/inference.json`, byte-reproducible from
                 the spec alone (`MOS-TRAIN-131`), and the parser back
    fixtures     the three spec fixtures and the synthetic golden phantom, with the two
                 pinned hashes `MOS-TRAIN-133`'s byte-equality check compares against

RELEASE 0.3.0 -- the run itself, and everything downstream of it that is machine work:

    runs         `RunBinding` and `TrainingRun` -- MOS-TRAIN-124's nine pinned inputs,
                 MOS-TRAIN-125's dirty-tree block, MOS-TRAIN-127's seed variance and
                 MOS-TRAIN-216's test-exposure counter
    orchestrator `MOS-TRAIN-122`'s port, its four constraints as data, one shipped driver
                 (a separate OS process) and one test fake (`MOS-REL-048`)
    cohort       the resolver the training and search jobs are handed: 403 on `test`,
                 never an empty set (`MOS-TRAIN-141`, `MOS-TRAIN-214`)
    bundle       the MONAI Bundle as the source form of a native-mode model artifact
                 (`MOS-TRAIN-129`), its metadata contract (`MOS-TRAIN-130`) and the
                 ensemble combination-rule vocabulary (`MOS-TRAIN-227`, `MOS-TRAIN-228`)
    autoconfig   the fingerprint -> `PreprocessingSpec` exporter that refuses rather than
                 substitutes, and the freeze (`MOS-TRAIN-223` to `MOS-TRAIN-226`)
    search       `ConfigurationSearch`: the declarative space, the budget bound and the
                 single nomination (`MOS-TRAIN-213` to `MOS-TRAIN-220`, `MOS-TRAIN-234`)
    conversion   `ConversionRun`, E1/E2/E3 and the per-case tolerances of
                 `MOS-TRAIN-159` to `MOS-TRAIN-169`
    candidate    `MOS-TRAIN-138`/`MOS-TRAIN-139`: register at `REGISTERED`, then
                 `VALIDATING` and `VALIDATED`, which are the only two statuses this
                 pipeline can cause

WHAT IS DELIBERATELY ABSENT, AND STAYS ABSENT
----------------------------------------------
`MOS-TRAIN-189`: "There MUST be no path -- no function, no API call, no scheduled task, no
policy -- from a `TrainingRun`, a `ConversionRun`, an `EvaluationRun` or a
`ValidationReport` to a `state = SERVING` `Deployment`", and CI must assert it as a
call-graph property. So the three human acts, the five promotion steps and the approval
dossier are `medos/medos/promotion/`, a package this one does not import;
`tests/integration/test_training_run.py::
test_no_symbol_reachable_from_the_pipeline_reaches_a_deployment_mutation` walks the import
closure and fails if that ever stops being true. `medos.evidence.deployment` is absent from
the closure for the same reason.

`MOS-TRAIN-123` is why the GPU-pool guard is an injected callable rather than an import of
`medos.inference.residency`: "The orchestrator MUST NOT be able to request a reservation
from `medicalos-tritond` or to cause an eviction."

`MOS-TRAIN-198` names the seam the deferred mechanisms enter through -- active learning,
federated training, automated retraining -- and it is `curation.add_candidate` together
with the `Orchestrator` port: every one of them is a producer of candidates that a named
human then decides about (`MOS-TRAIN-196`), or a driver behind four methods. None of them
acquires a route to a `Deployment`, because there is none to acquire.

Spec: docs/spec/17-training-pipeline.md; docs/spec/12-data-model.md section 12.12.1
(MOS-STORE-358, MOS-STORE-359); migrations 0011_curation and 0013_training.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
