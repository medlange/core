# SPDX-License-Identifier: Apache-2.0
"""The evidence plane's sealed half. Chapter 7 sections 7.2-7.5, chapter 17 section 17.6.

`MOS-EVID-006`: "The evidence plane is not a sixth architectural plane. It is a set of
entities, a gate function and a report format, hosted in the control plane. No component
of the serving path depends on it at request time except the gate lookup performed at
deployment time (7.9) and the envelope check performed at triage time (7.10)."

So this package has no worker, no HTTP surface and no queue. It is called at DEPLOYMENT
and CURATION time, by the training pipeline and the evidence API, and the serving path
does not import it.

WHAT IS HERE (release 0.2.0, chapter 15 section 15.2.5, chapter 17 `MOS-TRAIN-190`)

    digest         MOS-EVID-007/008/009/010/035, MOS-TRAIN-089 -- content addressing and
                   the tenant-scoped surrogate keys
    manifest       the three manifest formats and their three declared sort orders
    profile        `acquisition_profile`, MOS-EVID-024/025
    stratification the corpus stratification check C1-C7, MOS-TRAIN-088
    leakage        the leakage checks L1-L5 and the pre-split identity check,
                   MOS-EVID-034, MOS-TRAIN-115/116/117/118
    store          where a manifest object lives, MOS-EVID-015
    repo           `seal_dataset_version`, `freeze_split`, `freeze_annotation_set`
    errors         `SealRefused` / `FreezeRefused` / `EvaluationRefused`,
                   machine-readable

    metrics        the MOS-EVID-054 registry, the two pinned conventions of section 7.6
                   and the ONE per-case metric computation
    ci             the patient-level cluster bootstrap, MOS-EVID-057 to MOS-EVID-060
    aggregate      the reference aggregation function MOS-EVID-066 names
    evaluation     `create_run`, `record_cases`, `finish_run` -- `evaluation_runs`,
                   `evaluation_case_metrics` and `evaluation_case_scores` (0007)

WHAT IS NOT HERE, AND WHERE IT GOES

    `AcceptanceCriteria`, the deployment gate, `ValidationReport`, applicability
    envelopes, `ResultReview` and RUO marking are the rest of the measuring half of this
    release. They bind to a sealed cohort and cite an `EvaluationRun`, so they sit on top
    of these modules rather than inside them, and neither `0006_evidence.up.sql` nor
    `0007_evaluation.up.sql` declares any of their tables.

`MOS-EVID-004` and `MOS-EVID-005` bind every identifier in this package: the word
"validation" never appears unqualified, and the five forbidden strings appear nowhere.
"""

from medos.sdk.refusal import (
    EvaluationRefused,
    EvidenceError,
    FreezeRefused,
    Refusal,
    SealRefused,
)

__all__ = [
    "EvidenceError",
    "Refusal",
    "SealRefused",
    "FreezeRefused",
    "EvaluationRefused",
]
