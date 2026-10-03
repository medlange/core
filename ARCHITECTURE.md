<!-- SPDX-License-Identifier: Apache-2.0 -->
# Architecture

> MedicalOS integrates, governs and evidences medical AI services. It does not diagnose,
> does not replace a PACS, and is not itself a medical device.

This describes how MedicalOS is put together and, more usefully, **why the boundaries are
where they are.** Most of them exist because putting them anywhere else would make some
claim unverifiable.

---

## The one-sentence version

**Medlange Core** (`medos.sdk`) is the SDK an integrator builds on: it reads a model card
the trainer wrote, rebuilds the model's preprocessing pipeline, runs the study through
it against an inference server, and postprocesses the outputs per model. Around it sit
three products under the Medlange umbrella — **Core** (this repository's SDK), **Trainer**
(the batch image in the lineage of nnU-Net and MONAI that fits models and writes their
cards) and **Viewer** (the standalone DICOMweb viewer) — connected through adapters to a
PACS, a message bus and an inference server, in a local mode or driven by an external
management system over the bus.

The platform is still here — it is the reference local-mode composition on top of the
SDK — and its planes read the same as they did:

## The planes

| Plane | What it owns | Where |
|---|---|---|
| **SDK** | Model cards, the serving pipeline, preprocessing chains, canonical digests, adapters (PACS, bus, inference), postprocessing | `medos/medos/sdk/` |
| **Gateway** | DICOMweb in and out. The only component permitted to hold a PACS credential. | `medos/medos/gateway/`, `medos/medos/dicomweb/` |
| **Core** | Geometry, the source↔canonical transform, masks, measurements, UID derivation (the pure half the SDK stands on) | `medos/medos/core/` |
| **Service** | Capabilities — the models. Handed a volume, return findings. | `medos/medos/capabilities/`, `medos/services/` |
| **Writer** | DICOM SEG and SR, attribute inheritance, series numbering | `medos/medos/writer/` |
| **Evidence** | Sealed dataset versions, annotation manifests, validation reports, leakage checks | `medos/medos/evidence/` |
| **Training** | Cohorts, policies, splits, run records — and a separate image that does the fitting | `medos/medos/training/`, `trainer/` |
| **Resolution** | Which deployed model serves a given request | `medos/medos/resolution/` |

Two things in this repository are not planes and do not appear above, because they are
PRODUCTS rather than layers of one:

| | what it is | where |
|---|---|---|
| **Medlange Viewer** | a standalone DICOMweb viewer. No build step, no bundler, no runtime dependency; `MOS-UI-009a`'s four guarantees | `viewer/` |
| **Medlange Trainer** | the batch image that fits models and writes their model cards. No operator web surface, ever | `trainer/` |

The SDK used to be a separate published package (`medicalos_preprocessing/`, which
`MOS-IMG-003` required on its own); it moved inside the distribution as `medos.sdk` when
the platform became the SDK, and the pivot -- including the specification's now-historical
account of the two-package arrangement -- is recorded in register entry 151.

The training pipeline has no operator web surface and never did: `trainer/` is driven
through files and the training-plane API, and the no-code training console built once at
`medos/web/training-console/` was withdrawn at specification 0.4.0 (`MOS-UI-100` CUT,
register entry 150).

## Two products

**"Product" means two different things in this repository and both are correct, so the
difference is written down here rather than inferred.** This section is about the two
DEPLOYABLES built from the platform package -- Core and Train, one image each, the same
`medos/medos/` source with a different router set. The repository ALSO holds four product
DIRECTORIES -- `viewer/`, `trainer/`, `medos/` and `medicalos_preprocessing/` -- which is
a statement about who owns which source tree, not about what is deployed. The trainer is
a third deployable and is not in the table below; `MOS-REL-039` (one deployable per trust
boundary) governs that list and `docs/spec/15-delivery.md` is where it is maintained.

MedicalOS ships as two deployables built from one repository.

| | |
|---|---|
| **Core** (`medos.api.app:create_app`, image `medicalos/medos`) | The PACS-and-models service: DICOM in and out, jobs, capabilities, registries, results, reviews, and the whole evidence plane. 26 `/api/v1` paths. |
| **Train** (`medos.api.training_plane:create_training_app`, image `medicalos/medos-train`) | Core plus model preparation: harvesting, curation, dataset versions, splits, annotation sets, training runs, configuration searches, conversion runs. 52 paths. |

**Train is Core plus routers, never a fork.** A gate asserts Core's served set stays a
strict subset, because the moment it does not the two have begun to diverge and the
split has stopped being cheap.

The boundary runs through the *preparation* of a model, not through the *evidence* about
one. Core keeps the evidence plane: it can record what fitted a model — `MOS-TRAIN-124`'s
nine facts arrive as a declaration, not as something Core computes — without owning a GPU
or a cohort. A service that could only speak about inference would be much weaker under
IEC 62304, and nothing about running a fit is needed to say where one came from.

What the two share is the database. That is a decision rather than an accident, and the
measurement behind it: of 45 tables, 29 are written only by Core and 12 only by Train,
and **no Core table has a foreign key into a Train table**. The dependency runs one way,
so the schema could be separated — what cannot be separated cheaply is Core's public
surface, which Train reaches into at 43 call sites across four packages that declare no
API. That, and not the schema, is what two repositories would cost.

## Five boundaries, and what each one is load-bearing for

### 1. Only the gateway holds a PACS credential

`MOS-DATA-006`. A capability cannot fetch its own images and a service token cannot STOW.
Without this, a `Result`'s provenance is a claim about what the platform *thinks* it read;
with it, the platform knows, because it is the only thing that read anything.

In the shipped compose stack Orthanc is on an `internal: true` network with **no host
port** — the gateway is not merely the recommended path, it is the only reachable one.

### 2. The training image carries torch; the platform does not

`MOS-TRAIN-225` forbids the nnU-Net planner from the serving image's import closure.
`medos/medos/training/chain.py` goes further and **generates** MONAI configuration as data —
`{"_target_": "monai.transforms.Flip"}` — so the platform never imports MONAI at all.

The two images talk over **files and an exit code**. There is no socket, no RPC, no shared
process. `medos/medos/training/orchestrator.py` documents the run directory and
`medicalos_preprocessing/contract.py` implements the other side.

The consequence is that a training run's software provenance is a property of an image
digest rather than of a claim, which is what `MOS-TRAIN-126` means by "recorded rather than
asserted". The trainer stamps its own commit and a digest over its installed inventory at
build time; nobody types them, and the build refuses without them.

### 3. Capabilities are composed at build time, never loaded at run time

`MOS-REL-108` forbids dynamic module import in a platform process, on `MOS-CONF-109`'s
IEC 62304 §4.3 segregation argument. An earlier version resolved providers with
`importlib.import_module()` on an environment variable; it was removed and replaced with a
static `CATALOGUE` reached by top-level import.

`MEDOS_CAPABILITY_PROVIDERS` therefore holds a **selector key**, not a path. See
[`docs/services/AUTHORING.md`](docs/services/AUTHORING.md) for what this means for a third
party — briefly: they ship an image, not a plugin.

### 4. The evidence plane is outside the serving path

`MOS-EVID-006`. Nothing a clinician waits for can be blocked by, or can quietly skip, an
evidence write. The cost is that evidence has no HTTP surface, which is why the seal is
driven by a run record rather than a request and why several gate checks build their own
database rather than driving the deployed one.

### 5. The platform makes no clinical claim

`MOS-SAFE-002`/`MOS-SAFE-003`. The manufacturer of any clinical output is the publisher of
the `ServiceVersion` that produced it, named in `clinical.legal_manufacturer` and carried
on every generated DICOM object so the chain from a rendered overlay back to a legally
responsible party resolves **offline, from the object alone**.

## Identifiers

Every derived object's UID comes from `medos/medos/core/uids.py::derive_uid` — a UUIDv5 over a
fixed namespace and the full argument tuple, so the same declared inputs always produce the
same UID (`MOS-IMG-085`), which is what makes a retry idempotent at the PACS.

`derive_uid` has **no `StudyInstanceUID` parameter and will not grow one** (`MOS-IMG-069`).
Within the platform every object is *derived from* an acquisition, so a study UID is
something received and never invented. `medos/medos/writer/identity.py` copies it.

Ingest tools that reconstruct DICOM from files therefore live in `medos/tools/` and mint their
own identifiers under their own root — outside the platform, because they are doing the one
thing it refuses to do.

## Refusal as a design element

The platform answers `503 DeploymentNotDeclared` rather than guessing whenever a
declaration is a statement only a deployment can make: what its images' de-identification
provenance is, what the nine facts about the software that fitted a model are, which raw
segment name means which clinical concept.

`medos/medos/config/devmode.py` is the other half of that. A development stack answers those
questions so the platform works on a laptop, every answer carries
`dev-stack-not-for-patients`, and the loaders refuse such a value unless `MEDOS_ENV` is
exactly `dev` — **unset is not dev**, because the failure that actually happens is an
operator copying the compose file and setting nothing.

Run `python -m medos.cli doctor` to see which of a deployment's declarations are refusing
(correct) and which are merely unconfigured (yours to fix).

## What is not here

`docs/spec/` is the normative specification and is ahead of the code in named places;
`docs/spec/99-known-inconsistencies.md` records 153 of them with what each costs.

Notably absent from this document because they are absent from the code: a published base
image for third-party services, a seeded demo corpus, and any UI beyond the first-party
viewer. (A no-code training console existed at `medos/web/training-console/` and was
withdrawn at specification 0.4.0; see register entry 150.)
