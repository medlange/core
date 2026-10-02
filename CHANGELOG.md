<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

What arrived in each MedicalOS release block, and what did not, for somebody deciding whether to
move between two of them.

**This file is a view, not a second source.** The authoritative record for a release is its
Release Decision Record under [`docs/releases/`](docs/releases/): six fields fixed by
`MOS-REL-003`, including the gate result per named check, the images and their digests, the
requirement ids newly cited, and the human who approved the tag. Where this file and a record
disagree, the record wins. Every section below links to its own.

`MOS-REL-095` requires this file in Keep-a-Changelog format, one section per release. It is also
the platform's side of one IEC 62304 clause: §18's clause-9.5 row — problem reports and their
resolutions are retained — names `CHANGELOG.md`, so an entry deleted here is evidence deleted.

## Read this first: nothing below is tagged

`git tag` is empty. Each version below is a release **block** whose contents are complete in the
tree and whose gate row ran green, recorded and approved — and not tagged. `MOS-REL-004` gates the
tag on the gate row and `MOS-REL-003` requires the record committed first, which is where these
three stopped. Two consequences for a reader:

- a version heading here is **not a git ref you can check out**;
- the date on a heading is the date its record was approved, **not a release date**.

The dates and the approver are quoted from the records rather than asserted here.
[`docs/releases/README.md`](docs/releases/README.md) carries the full account, including the
sentence each record repeats: an attribution is not an attestation, the name was entered on the
repository owner's instruction, and it is neither a signature nor an authenticated act.

## Two headings that are not Keep-a-Changelog's

Keep-a-Changelog names six change types — Added, Changed, Deprecated, Removed, Fixed, Security.
Two headings below are not among them, and the reason is that what they carry has no equivalent
in that set:

- **Deferred under `MOS-REL-009`** — scope planned for a release that did not ship. It is not
  *Removed*: nothing was taken away from anybody, because it had never arrived. `MOS-REL-005`
  makes a Tier B deferral reappear in the next release, so each one is a debt with a due date
  rather than a deletion.
- **Dropped permanently (Tier C)** — the same, for the tier `MOS-REL-005` allows to be dropped
  outright. An item here is not coming back.

Inventing a Keep-a-Changelog heading that nearly fits would have cost a reader more than the
deviation does.

## [Unreleased]

The 0.4.0 block: unattended study-arrival operation, third-party viewer verification, the MCP tool
surface, agentic report drafting, and the operator-surface elements Chapter 19 places there.
Contents and gate are in [§15.1.2](docs/spec/15-delivery.md).

**There is deliberately no `[0.4.0]` section, and the absence is a decision rather than an
omission.** `MOS-REL-004` does not merely leave 0.4.0 untagged — it forbids the tag. The four
checks in that gate row (`unattended-arrival`, `second-viewer`, `mcp-tenancy`,
`narrative-postcondition`) have no modules, and `tests/unit/test_gate_contract.py` declares the
release `NOT_YET` and asserts at two places that none may have one yet. A section here would be
the same mistake as writing the release record itself, which register entry 125 records at length.

Returning to this block from the 0.3.0 cut, as `MOS-REL-005` requires: the clinician model picker,
the training submit surface with run monitoring, the configuration-search surface, the promotion
screen, and the refusal catalogue at the 0.3.0 set.

## [0.3.0] — approved 2026-09-17, not tagged

[Release Decision Record](docs/releases/0.3.0.md)

### Added

- `Artifact` registry, and capability resolution as a pure function.
- Sealed-mode services, and the Kafka `JobQueue` driver beside the Postgres one.
- A **second capability** — lung nodule on LIDC-IDRI — added with **zero core code changes**,
  which is the property the `zero-core-change` gate asserts rather than the feature.
- The training pipeline elements Chapter 17 places here (`MOS-TRAIN-190`): `TrainingRun` with the
  `Orchestrator` port and one driver, MONAI Bundle as the source form of a native-mode model
  artifact, nnU-Net permitted as a training backend, `ConversionRun`, and the three-act promotion.
- The honest-metric display rule in force end to end (`MOS-UI-300`).

### Deferred under `MOS-REL-009`

Each returns in 0.4.0 under `MOS-REL-005`; the record holds what was not delivered in each case.

| Item | Register slug | Tier |
|---|---|---|
| The clinician model picker | `ui030-model-picker` | B |
| The training submit surface with run monitoring | `ui030-training-submit-surface` | B |
| The configuration-search surface | `ui030-configuration-search-surface` | B |
| The promotion screen | `ui030-promotion-screen` | B |
| The refusal catalogue extended to the 0.3.0 set | `ui030-refusal-catalogue` | B |
| The operator-surface elements returning from the 0.2.0 cut | `ui030-returning-set-from-020` | B |

### Dropped permanently (Tier C)

| Item | Register slug |
|---|---|
| The search diagnostics block | `ui030-search-diagnostics-block` |
| The conversion surface | `ui030-conversion-surface` |

## [0.2.0] — approved 2026-09-17, not tagged

[Release Decision Record](docs/releases/0.2.0.md)

### Added

- The evidence plane: `Dataset`, `DatasetVersion`, `DatasetSplit`, `AnnotationSet`,
  `EvaluationRun`, `AcceptanceCriteria`, `ValidationReport`.
- Gated deployment, applicability envelopes, research-use-only marking, and `ResultReview`.
- The training-pipeline elements Chapter 17 places here (`MOS-TRAIN-190`): the curation queue
  (`HarvestCandidate`, `CurationBatch`) with its exclusion vocabulary, per-`Tenant`
  `training_use_allowed` with its `TrainingDataPolicy`, the corpus stratification check run at
  seal, and the generated transform chain with its byte-equality check.

### Deferred under `MOS-REL-009`

Each returns in 0.3.0 under `MOS-REL-005`.

| Item | Register slug | Tier |
|---|---|---|
| The `ResultReview` surface with its `REJECTED` review rendering | `ui020-resultreview-surface` | B |
| The curation queue surface | `ui020-curation-queue-surface` | B |
| The training-policy surface | `ui020-training-policy-surface` | B |
| The seal, split-freeze, leakage and stratification refusal rendering | `ui020-training-refusal-rendering` | B |
| The annotation campaign surface | `ui020-annotation-campaign-surface` | B |
| The refusal catalogue at the 0.2.0 set | `ui020-refusal-catalogue` | B |
| The first issue of the UI contribution statement | `ui020-contribution-statement` | B |
| The operator-surface elements returning from the 0.1.0 cut | `ui020-returning-set-from-010` | B |

## [0.1.0] — approved 2026-09-17, not tagged

[Release Decision Record](docs/releases/0.1.0.md)

### Added

- DICOM Gateway with de-identification; study triage with `SeriesSelector`.
- `Job` with the Postgres queue driver; the geometry contract.
- Platform-side SEG and SR writing with deterministic UIDs.
- One native service (pleural effusion, own data); vendored lung segmentation; `emphysema_laa`
  as a deterministic measurement rather than a learned model.
- API-key authentication; `tenant_id` with Postgres row-level security; audit.
- Single Triton, docker compose.
- The operator-surface elements Chapter 19 places here (`MOS-UI-365`): the `REJECTED`/`FAILED`
  distinction carried on four independent channels, the `MOS-SAFE-012` adjacency set rendered
  without interaction on every result-bearing surface, and the clinician surface's three-part
  refusal rendering with the machine-readable code secondary.

### Deferred under `MOS-REL-009`

Each returned in 0.2.0 under `MOS-REL-005`. This section read "None" in an earlier issue of the
record and that was wrong; the record now says why, and the correction is the reason the two rows
below exist.

| Item | Register slug | Tier |
|---|---|---|
| The two surfaces inside a rebuilt OHIF bundle | `ui010-rebuilt-ohif-bundle` | B |
| The refusal catalogue and its CI completeness check | `ui010-refusal-catalogue` | B |

## Before 0.1.0

`MOS-REL-014` requires an exit record for the pre-0.1.0 spike at `docs/releases/spike-week0.md` —
two viewer observations with screenshots, the DICOM battery green in CI, and a selector error count
with a per-error cause. **It is not in the tree, and until this file was written nothing in the
repository said so.** §15's own acceptance criterion 5 asserts as a checkable fact that the
document "exists", so a reviewer executing that criterion records a failure; `MOS-REL-012` makes an
unexecuted acceptance criterion equivalent to an unsatisfied requirement. Register entry 149 holds
it, and `tests/unit/test_release_records.py` freezes it so the gap cannot grow or shrink silently.
