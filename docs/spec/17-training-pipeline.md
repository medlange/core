<!-- MedicalOS Specification v0.4.0 — chapter 17 of 19. Normative.
     236 requirements. Do not edit without a requirement-ID review. -->

[← 16. Open Questions](16-open-questions.md) · [Index](../../MEDICALOS_SPEC.md) · [18. Standards and Regulatory Conformance →](18-conformance.md)

---

## 17. Model Development Pipeline

Every other chapter of this document describes what happens to a model that already exists. This one describes where it comes from. It is the last chapter because it is the one that most easily destroys the others: a training pipeline that reaches far enough forward becomes a path from production imaging to a serving model that never passes through a dataset, a split, an annotation set, an evaluation run, a report, or a person. That path is faster, it is what every MLOps reference architecture draws, and it is the one thing MedicalOS exists to make impossible.

The pipeline specified here is therefore deliberately open at one end. It automates everything from harvesting a study to a signed `ValidationReport`, and then it stops, holding a candidate. Somebody has to pick the candidate up.

---

### 17.1 Scope and the closed-loop prohibition

#### 17.1.1 Position

**MOS-TRAIN-001** The model development pipeline MUST run entirely off the production inference path. No component of it MAY be invoked, directly or transitively, by triage (Chapter 3), by job dispatch (Chapter 5), by a native or sealed service execution (Chapter 2), or by the DICOM writer (Chapter 4). No component of the serving path MAY read a pipeline artifact other than a published, signed `ModelVersion` and its co-located `PreprocessingSpec` resolved through the registry (`MOS-REG-025`).

**MOS-TRAIN-002** The pipeline is a **producer of Chapter 7 entities and nothing else**. It MUST NOT define a parallel evidence vocabulary, a second metrics store, a second report format, or a "training metrics" surface that is not an `EvaluationRun`. Its complete output set is:

| Produced entity | Owner of the contract | What this chapter adds |
|---|---|---|
| `DatasetVersion` (`purpose: training`, `tuning`, `acceptance`) | Chapter 7, `MOS-EVID-014` ff. | how the cohort is harvested, curated and stratified (17.4) |
| `AnnotationSet` | Chapter 7, `MOS-EVID-038` ff. | how readers are convened and how provenance is recorded (17.5) |
| `DatasetSplit` | Chapter 7, `MOS-EVID-028` ff. | nothing; the split contract is used unmodified |
| `PreprocessingSpec` | Chapter 4, `MOS-IMG-045` ff. | the MONAI serialization and the golden-fixture recording side (17.3) |
| `ModelVersion` artifact | Chapter 6, `MOS-REG-030` ff. | the MONAI Bundle layout it is packaged from (17.2) |
| `EvaluationRun` | Chapter 7, `MOS-EVID-061` ff. | nothing; runs are submitted, not reimplemented |
| `ValidationReport` | Chapter 7, `MOS-EVID-112` ff. | nothing; the report is requested and signed per Chapter 7 |

**MOS-TRAIN-003** The pipeline's terminal state is a **candidate**: an `artifact` row of `kind = "model_version"` at `lifecycle_status = VALIDATED` (Chapter 6, `MOS-REG-020`), with a signed `ValidationReport` bound to it. The pipeline MUST NOT produce any state beyond that, and MUST NOT be capable of producing one.

**MOS-TRAIN-004** Pipeline execution MUST run in the offline evidence-plane environment (Chapter 15's adopted batch orchestrator, `MOS-REL-027`). It MUST NOT use the `JobQueue` port of Chapter 5, MUST NOT write a `Job`, `JobStep` or `Result` row, and MUST NOT publish to any `medicalos.jobs.*` topic. `MOS-REL-036` forbids the batch engine on the serving path; this requirement is its mirror image and forbids the serving queue in the pipeline. The two engines share no runtime.

#### 17.1.2 The prohibition, stated normatively

**MOS-TRAIN-005** **No automated path MUST exist from production data to a serving model.** Concretely, the platform MUST NOT implement, and MUST NOT be configurable to implement, any of the following edges:

| Forbidden edge | Why it is forbidden |
|---|---|
| production `Result` → training trigger → weight update → serving | routes around `DatasetVersion`, `DatasetSplit`, `AnnotationSet`, `EvaluationRun` and `ValidationReport` in one step |
| `ResultReview` outcome → retraining | already forbidden by `MOS-SAFE-063`'s side-effect allowlist; restated here because the pipeline is the component that would want it |
| pipeline → `artifact.lifecycle_status = APPROVED` | the promotion decision is a person's (`MOS-TRAIN-009`) |
| pipeline → `Deployment` create, promote or role change | deployment is Chapter 6's, gated by Chapter 7's `evaluate_gate()` and Chapter 9's E1 |
| pipeline → in-place mutation of a published weights blob | `MOS-REG-018`; see 17.1.3 |
| monitoring drift alert → automatic retrain-and-replace | `MOS-EVID-139` already forbids an alert changing deployment state; the retrain half is forbidden here |
| a running service container writing weights, adapters or calibration state | `MOS-SVC-058`; restated because online learning is the form this takes |

**MOS-TRAIN-006** Everything up to and including the signed `ValidationReport` MAY be fully automated, and SHOULD be. Harvest, curation dispatch, split freezing, training, packaging, evaluation submission and report generation MUST be expressible as one unattended run. The prohibition is not a prohibition on automation; it is a prohibition on one specific edge.

**MOS-TRAIN-007** Signing the report MUST remain a separate, separately-authorised act, per `MOS-EVID-119`. The pipeline runner MUST NOT hold the report signing key. A pipeline that both produces a run and attests to it has collapsed two principals into one.

**MOS-TRAIN-008** The transition `VALIDATED → APPROVED` MUST be performed by a human holding `artifact.approve` (`MOS-REG-021`), and the approving identity MUST satisfy `MOS-EVID-117`: a named person, with a role, an organisation, a written statement and an `identity_assurance` value. An automated approver MUST NOT be permitted, and a service account MUST NOT hold `artifact.approve`.

**MOS-TRAIN-009** The pipeline's execution identity MUST be a `ServiceAccount` whose permission set is exactly `{dataset.create, dataset.seal, annotation.freeze, split.freeze, evaluation.submit, artifact.publish}`. It MUST NOT hold `artifact.approve`, `deployment.create`, `deployment.promote`, `deployment.gate.override`, `result.publish` or `phi.reidentify`. The gate is a permission boundary, not a convention: an accidental call MUST fail with `403` rather than succeed quietly.

**MOS-TRAIN-010** A scheduled or event-triggered pipeline run is permitted. A scheduled or event-triggered **promotion** is not. A run that completes with a `PASS` verdict MUST notify — webhook, ticket, review queue — and MUST NOT act.

**MOS-TRAIN-011** The platform MUST expose the candidate's full evidence set at the moment of the promotion decision: the `ValidationReport`, the diff against the incumbent's report, the per-stratum results (`MOS-EVID-067`), the regression comparison (`MOS-EVID-085`) and the corpus stratification report of `MOS-TRAIN-088`. A promotion UI that shows a single aggregate number and a button is non-conformant, because it reproduces the automated path with a human-shaped delay in it.

#### 17.1.3 Why the loop must stay open: reproducibility and recall

**MOS-TRAIN-012** Every clinical output MedicalOS produces is attributed to an immutable `ModelVersion`. `MOS-SAFE-003` requires the chain from a rendered overlay back to a legally responsible manufacturer to be resolvable offline from the object alone; `MOS-SAFE-046` snapshots the producing deployment onto the `Result`; `MOS-REG-040` requires a recalled version to enumerate every job, result and generated `SeriesInstanceUID` it ever produced. All three depend on one property: that `(model_id, model_version)` names exactly one set of weights, one `PreprocessingSpec`, and one operating point, forever.

**MOS-TRAIN-013** Therefore the pipeline MUST NOT mutate a published artifact. A retrained model, a re-tuned threshold, a re-exported backend, a re-recorded golden fixture and a changed preprocessing field each produce a **new** `ModelVersion` with a new digest, a new `EvaluationRun` and a new report. `MOS-IMG-046`, `MOS-REG-035` and `MOS-EVID-064` each state one case of this rule; this chapter states the general one for the producing side and adds no exception.

**MOS-TRAIN-014** Online learning, continual learning, federated weight averaging into a live artifact, runtime calibration fitting, and adapter or LoRA hot-swap on a serving model MUST NOT be implemented in releases 0.1.0 through 0.4.0. The reason is not that they do not work; it is that the derived `SeriesInstanceUID` of `MOS-IMG-062` is a function of `model_version`, so a weight that changes without a version change makes two clinically different outputs collide on one identity, and makes `MOS-REG-040`'s impact set uncomputable. A recall of such a model could not say what it produced.

**MOS-TRAIN-015** A service container MUST NOT write to its weights path, MUST NOT persist state between executions, and MUST NOT accumulate per-site calibration. The native-mode runner mounts weights read-only; the sealed-mode contract already forbids mutation (`MOS-SVC-058`). A service that requires per-site calibration MUST express it as a per-site `ModelVersion` with its own evidence, which is exactly the cost the rule intends to impose.

**MOS-TRAIN-016** Federated or multi-site training is permitted as a *training-time* activity: weights may be aggregated across sites before packaging. The aggregated result is a single new `ModelVersion` with a single `EvaluationRun` on a held-out cohort. What MUST NOT happen is aggregation into a deployed artifact.

**MOS-TRAIN-017** CI MUST assert all of the following, and the build MUST fail on any one:

1. No module under the serving packages imports any module under the pipeline packages.
2. The pipeline's `ServiceAccount` role grant contains none of the permissions named in `MOS-TRAIN-009`'s exclusion list.
3. No code path writes `lifecycle_status = 'APPROVED'` other than the `artifact.approve` handler.
4. No code path writes to a `deployments` row from pipeline packages.
5. A repository-wide grep finds no call that both reads a `results` row and writes a `datasets`, `dataset_versions` or training-artifact row in one transaction.

**MOS-TRAIN-018** Chapter 14 MUST carry an end-to-end negative test: drive a complete pipeline run to a `PASS` verdict, assert that the candidate is `VALIDATED`, assert that zero `deployments` rows changed, assert that the incumbent is still serving, and assert that the candidate receives no traffic until an `artifact.approve` call is made by a principal with a human identity.

---

### 17.2 MONAI: what is adopted, what is wrapped, what is refused

The register of `MOS-REL-027` requires a row for every subsystem and forbids recording "build" without naming a disqualified incumbent. MONAI is not one subsystem; it is five products under one name, and they do not get one decision.

**MOS-TRAIN-019** The rows below MUST be added verbatim to `docs/adr/BUILD_VS_ADOPT.md` under `MOS-REL-027`. They are normative in the same sense as the rows already there.

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **MONAI Core** — transforms, `sliding_window_inference`, losses, metrics, networks | Apache-2.0 | **ADOPT** | The dictionary transform vocabulary (`Orientationd`, `Spacingd`, `ScaleIntensityRanged`, `NormalizeIntensityd`, `CropForegroundd`), `monai.inferers.sliding_window_inference`, `DiceCELoss`, the 3D network zoo | `medicalos-preprocessing`, which *constructs* a `Compose` from a `PreprocessingSpec` and is the only constructor in the codebase | `ForegroundCropToMinSized` (one transform, 17.3.4); the spec↔chain serialization; the golden-fixture hash | The chain is the serialization target for `MOS-IMG-049`. One chain, versioned once, executed by training and serving alike, is the mechanism that closes train/serve skew at its root rather than testing for it afterwards. Re-implementing spline resampling and Gaussian-blended sliding windows would be a research project with a worse determinism story. |
| **MONAI Bundle** — `configs/`, `models/`, `metadata.json` with a declared schema | Apache-2.0 | **ADOPT** | The bundle directory layout and `metadata.json` schema as the on-disk form of a native-mode `ModelVersion` artifact | The OCI packaging of `MOS-REG-084`, which carries the bundle as layers | The mapping from bundle fields to the `model_version` manifest of `MOS-REG-030`; the `PreprocessingSpec` and golden-fixture blobs, which a bundle has no slot for | A bundle already carries weights, an inference configuration and typed metadata under one directory with a published schema. That is 80 % of what `MOS-REG-030` needs, and it is the interchange format the Model Zoo speaks. |
| **MONAI Model Zoo** | per-bundle; varies | **ADOPT as a source** | Pretrained bundles as *inputs* to the pipeline | Re-packaging into a MedicalOS `ModelVersion` | Licence verification per `MOS-REL-038`; a mandatory own `EvaluationRun` | A zoo bundle is a starting point, never a `ModelVersion`. Its published metrics were measured on its own cohort under its own conventions and are not MedicalOS metrics (`MOS-EVID-071`). |
| **MONAI Label** — annotation server + viewer plugins — **AMENDED at specification 0.4.0** | Apache-2.0 | **ADOPT** | The annotation server, ~~its Slicer and OHIF clients,~~ its 3D Slicer client, its scribble and interactive-segmentation apps | An authentication shim binding every session to a `User`; an exporter writing per-reader masks into an `AnnotationSet` manifest | Reader identity binding; the `annotation_provenance` record; the consensus computation, which the platform performs, not the tool | It is the only open annotation stack that already speaks DICOMweb, runs against a segmentation model for seeding, and ~~has a real viewer story~~ reaches a reader through a client this platform deploys. It is the natural producer of `AnnotationSet`. Its own training loop is disabled — see `MOS-TRAIN-106`. |
| **MONAI Deploy / MAP** | Apache-2.0 | **REFUSE** | nothing | nothing | nothing | See `MOS-TRAIN-026` below and `MOS-REL-033`. |
| **nnU-Net** (via MONAI's integration) | Apache-2.0; weights vary | **PERMIT as a training backend** | The self-configuring planner and the training recipe | A plan exporter that emits a `PreprocessingSpec` (`MOS-TRAIN-030`) | The refusal to run nnU-Net's own inference wrapper on the serving path | It is the strongest segmentation baseline available and the 0.1–0.3 corpus is segmentation-heavy. Not adopting it would mean hand-tuning architectures against a published baseline that beats them. |

**The MONAI Label row, amended at 0.4.0, and why the strike is this narrow.** Release 0.4.0 withdrew the OHIF
deployment: `medos/deploy/compose/docker-compose.yml` no longer runs `ohif/app:v3.9.2`, and the clinician surface is
first-party (`MOS-UI-009a`, Chapter 19 §19.1.2). The question that decides what happens to this row is whether the OHIF
client was load-bearing for the adoption or incidental to it, and it was incidental. What is adopted here is an
**annotation stack**, and the properties that disqualified the alternatives are all the server's: it speaks
DICOMweb, it runs a segmentation model for seeding, and it produces per-reader masks an exporter can turn into
an `AnnotationSet`. None of those is a property of a client. The client this chapter actually writes into a data
field is the other one — `MOS-TRAIN-097` fixes `annotation_readers.tool` for this stack to the literal
`"MONAI Label 0.8.4 + 3D Slicer 5.6.2"` — and Chapter 19 §19.4.9 specifies a round trip for that client and
none for the other. A plugin for a viewer this platform does not run is not something the platform adopts, and
leaving it in the adopted set would make
`MOS-TRAIN-019`'s verbatim-append MUST copy a false claim into `docs/adr/BUILD_VS_ADOPT.md`, which is the
shape of defect `MOS-REL-027` exists to prevent. The amended row is what `MOS-TRAIN-019` now requires to be
carried verbatim, and the copy in that file MUST be brought into line with it.

**What the strike does not settle, said here rather than left to be discovered.** `MOS-TRAIN-095` is unchanged:
MONAI Label MUST be the annotation surface for mask, bounding-box and point annotation types. Chapter 19 §19.4
specifies that surface over an OHIF build — `MOS-UI-202`'s ADOPT row and `MOS-UI-206`'s requirement that the
ergonomics set ship as OHIF configuration — and the compose stack no longer runs one. `MOS-UI-205a` records the
consequence in the same terms: the host of the annotation surface is now an open question, because the
tie-break that used to settle it silently was that the platform's own OHIF build wins, and there is no longer a
platform OHIF build for anything to be integrated against. Whether the reader reaches MONAI Label through the
3D Slicer round trip of §19.4.9 — bounded by `MOS-UI-203`, which forbids Slicer to be the surface a campaign
opens into — through an OHIF build raised for annotation alone, or through something else is chapter 19's
ruling and not this chapter's. This chapter records only that a campaign cannot open until it is made, because
`MOS-TRAIN-096`'s per-reader identity and `MOS-TRAIN-099`'s edit measurements are properties of whatever
surface is chosen and cannot be assumed of one that has not been. The gap itself is recorded in
`MOS-UI-205a`'s third clause, which states it as undecided and says why; register entry 106 in
`docs/spec/99-known-inconsistencies.md` holds the amendment wave this strike belongs to and does
not record this gap, which is named here so that a reader following the entry does not conclude it
was covered there.

#### 17.2.1 MONAI Core

**MOS-TRAIN-020** `medicalos-preprocessing` MUST implement the forward transform of `MOS-IMG-031` as a MONAI `Compose`, and MUST NOT contain a second, non-MONAI implementation of any step. A hand-written resampler, a hand-written intensity clip or a hand-written sliding window in first-party code is a `MOS-REL-032` violation.

**MOS-TRAIN-021** The MONAI version MUST be pinned exactly and MUST be recorded in `PreprocessingSpec.backend.resampler` per the value convention of `MOS-TRAIN-055`. `MOS-IMG-049` owns the field; this chapter fixes only what a MONAI-serialized chain writes into it.

#### 17.2.2 MONAI Bundle

**MOS-TRAIN-022** A `native`-mode `ModelVersion` whose `weights_availability` is `platform_managed` MUST be packaged from a MONAI Bundle with this layout. The mapping to `MOS-REG-030`'s manifest is fixed:

```text
pulmo-lung-lobes-2.1.0/
  configs/
    metadata.json          -> ModelVersion.spec.io, spec.capabilities, licence
    inference.json         -> the network definition and the sliding-window arguments
  models/
    model.pt               -> ModelVersion.spec.weights (torchscript)
    model.onnx             -> ModelVersion.spec.weights (onnx), when exported
  medicalos/
    preprocessing.yaml     -> the PreprocessingSpec artifact (MOS-IMG-049)
    golden_input.nii.gz    -> golden_fixture.path
    golden_tensor.f32       -> the expected model-space tensor, for MOS-IMG-055 diagnostics
  LICENSE
  docs/README.md
```

**MOS-TRAIN-023** The `medicalos/` subdirectory is a MedicalOS addition to the bundle layout and MUST be present. A bundle without it MUST be rejected at packaging, because `MOS-REG-036` requires the golden fixture and `MOS-SVC-016` requires the `PreprocessingSpec` pin, and a stock MONAI Bundle has a slot for neither.

**MOS-TRAIN-024** `configs/inference.json` MUST NOT be the source of truth for any preprocessing or sliding-window value. Those values live in `medicalos/preprocessing.yaml` and MUST be generated into `inference.json` by the packager, never edited there. Two editable copies of `overlap` is exactly the skew this chapter exists to prevent.

**MOS-TRAIN-025** A bundle obtained from the MONAI Model Zoo MUST be re-registered as a new `ModelVersion` under the registering party's own signing identity, MUST carry the upstream licence in its artifact manifest (`MOS-REL-038`), MUST record the upstream bundle name, version and digest in `spec.derived_from`, and MUST have its own `EvaluationRun` before it can reach `VALIDATED`. Its upstream published metrics MUST NOT be written into any MedicalOS field (`MOS-EVID-071`).

#### 17.2.3 MONAI Deploy and the MAP format: refused

**MOS-TRAIN-026** MONAI Deploy's MAP MUST NOT be adopted as a service packaging format, a model artifact format, or an execution target. The platform MUST NOT build, publish, accept at registration, or execute a MAP.

The reasoning is a build-versus-adopt reasoning and belongs in full rather than in a table cell. A MAP is not a model container; it is a **standalone medical imaging application**. Its operator graph begins with a DICOM data loader and ends with `DICOMSegmentationWriterOperator` or `DICOMTextSRWriterOperator`. That is the whole point of the format: a vendor ships one artifact, you point it at a directory of DICOM, and DICOM objects come out the other end. The format's value proposition is precisely that it owns its own DICOM I/O.

MedicalOS has already sold that ground to a different owner. The ownership boundary of Chapter 2 assigns *all* DICOM object writing to the platform; `MOS-IMG-062` derives every generated `SeriesInstanceUID` and `SOPInstanceUID` deterministically from `(idempotency_key, model_id, model_version, output_index)`; `MOS-IMG-069` forbids minting a `StudyInstanceUID`; `MOS-IMG-070` requires a QIDO-RS skip-if-present check before the first write so that a retry resumes rather than re-runs; `MOS-IMG-075` binds the Type 1 equipment tags to the `legal_manufacturer` identity that Chapter 9 makes legally load-bearing; `MOS-SAFE-039` requires the RUO marker check to run on the assembled dataset immediately before STOW-RS, in one function shared by the SEG, SR and SC writers, with no flag that disables it. Adopting MAP would push all of that back out into vendor containers — one deterministic-identity implementation per vendor, one RUO marking implementation per vendor, one equipment-tag implementation per vendor, none of them verifiable at the boundary. The failure mode is not that a vendor writes a bad SEG; it is that the platform can no longer make a statement about what was written.

The second half of the refusal is the execution shape, and `MOS-REL-033` already records it: a MAP is *invoked* — it is a process that starts, consumes an input directory, writes an output directory and exits. The platform's unit of execution is a queue consumer holding a lease, heartbeating against `timeouts.heartbeat_interval_ms` (`MOS-SVC-041`), reporting `phase` and `JobStep` rows, and surviving a `max.poll.interval.ms` window. Forcing a per-invocation application into that shape means a supervisor process per job that translates leases into subprocess lifetimes and directory scans into progress — a wrapper with no upside, since the model inside the MAP is exactly what the sealed-mode HTTP contract of Chapter 2 already accepts directly.

**MOS-TRAIN-027** A vendor whose model is distributed as a MAP MUST be onboarded by repackaging: extract the inference operators, drop the DICOM loader and the DICOM writer operators, and publish the remainder as a `sealed`-mode `ServiceVersion` implementing the Chapter 2 execution contract and returning a `ResultBundle`. The platform MUST NOT offer a MAP-compatibility runner, and MUST NOT accept a MAP as `execution.sealed.image`.

**MOS-TRAIN-028** The refusal is scoped to MONAI Deploy's packaging and I/O layers. MONAI Deploy's *inference* operators are ordinary MONAI Core code and MAY be used inside a service.

#### 17.2.4 nnU-Net

**MOS-TRAIN-029** nnU-Net MAY be used as a training backend through MONAI's integration. Its planner MAY determine target spacing, patch size, normalisation scheme and network topology.

**MOS-TRAIN-030** An nnU-Net plan MUST be exported into a `PreprocessingSpec` before packaging, by a deterministic exporter that maps `plans.json` fields onto `MOS-IMG-049` fields, and the exported spec MUST be the artifact that both training validation and serving execute. The nnU-Net plan file MUST NOT be shipped inside the `ModelVersion` artifact as an executable configuration, because that would be a second preprocessing implementation reachable at serving time.

**MOS-TRAIN-031** nnU-Net's own predictor (`nnUNetPredictor` and its `predict_from_files` family) MUST NOT run on the serving path. Native-mode inference is Triton (`MOS-SVC-037`, `MOS-SVC-040`), and the sliding window is MONAI's, configured from the spec.

**MOS-TRAIN-032** nnU-Net's built-in cross-validation split MUST NOT be used as a `DatasetSplit`. Splits are patient-level, materialised manifests with leakage checks (`MOS-EVID-028`, `MOS-EVID-029`, `MOS-EVID-034`); nnU-Net's is a seeded, case-level, regenerable partition and satisfies none of those properties. The pipeline MUST hand nnU-Net a fold assignment derived from the frozen `DatasetSplit`.

**MOS-TRAIN-033** nnU-Net's default post-processing search (largest-connected-component removal, per-label) MUST be treated as part of the model, not as an evaluation-time convenience. A post-processing choice selected on data MUST be selected on the `tune` partition (`MOS-EVID-033`), MUST be recorded on the `ModelVersion`, and MUST be executed identically at serving time.

---

### 17.3 `PreprocessingSpec` as a serialized MONAI transform chain

This is the section that makes the rest of the chapter safe. `MOS-IMG-045` through `MOS-IMG-058` already require one preprocessing implementation, imported by serving and training alike, pinned by digest, and verified at worker startup against a hash recorded at training time. What they do not say is what that implementation *is*. It is a MONAI `Compose`, constructed from the spec by one function.

#### 17.3.1 The one-implementation rule, as a code rule

**MOS-TRAIN-034** `medicalos_preprocessing.build_chain(spec) -> monai.transforms.Compose` MUST be the only function in the MedicalOS codebase that instantiates a MONAI transform for the deterministic preprocessing path. Training scripts, evaluation runners, the native-mode serving runner and the golden-fixture recorder MUST all obtain their chain from it.

**MOS-TRAIN-035** The training repository MUST NOT declare a transform chain in YAML, JSON, a MONAI bundle config, an nnU-Net plan, or Python. CI MUST enforce this with a grep over the training packages for the MONAI transform class names and for `monai.transforms.Compose`, allowing them only inside `medicalos_preprocessing` and inside the augmentation module of `MOS-TRAIN-037`.

**MOS-TRAIN-036** `build_chain` MUST be **total and pure**: a valid `PreprocessingSpec` maps to exactly one `Compose`, with no environment lookup, no feature flag, no `if training:` branch, and no dependence on the input volume. A spec field that cannot be expressed as a MONAI transform MUST NOT be added to the spec until the transform exists.

**MOS-TRAIN-037** Random augmentation is **not** preprocessing and MUST NOT appear in the serialized chain. It lives in `medicalos_training.augment`, is applied only after `build_chain`'s output during training, and is not part of the artifact. CI MUST assert that no transform whose class name begins with `Rand` is ever emitted by `build_chain`. An augmentation that leaks into the spec is applied at serving time, non-deterministically, on every patient.

**MOS-TRAIN-038** `LoadImaged` and `EnsureChannelFirstd` MUST NOT appear in the serialized chain. At serving time the input is already a `CanonicalVolume` produced by `medicalos-imaging` (`MOS-IMG-036`); at training time and in the startup self-test the loader is `medicalos-imaging` reading the fixture (`MOS-IMG-054` step 2). The hashed chain therefore begins at `Orientationd`, and the loader is an adapter outside it.

#### 17.3.2 The chain

The worked example is a chest-CT five-lobe segmentation model, `pulmo.lung-lobes` 2.1.0. It is chosen over the pleural-effusion example of `MOS-IMG-052` because its `io.output_kind` is `label`, which is the case in which `label_interpolator` actually does work in the inverse.

**MOS-TRAIN-039** The constructor below is normative. Its transform order MUST be the order of `MOS-IMG-031` steps (1) through (5); step (6) is the sliding window of 17.3.5 and is not part of the `Compose`.

```python
# medicalos_preprocessing/chain.py
# The ONLY constructor of a MedicalOS preprocessing chain (MOS-TRAIN-034).
# Imported by: medicalos_training.run, medicalos_evidence.evaluate,
#              medicalos_serving.native_runner, medicalos_preprocessing.record.

import math
from functools import partial

import torch
from monai.transforms import (
    Compose,
    EnsureTyped,
    NormalizeIntensityd,
    Orientationd,
    ScaleIntensityRanged,
    Spacingd,
)

from medicalos_preprocessing.transforms import ForegroundCropToMinSized

# MOS-IMG-049 `image_interpolator` / `label_interpolator` -> MONAI `mode`.
# Integer modes select a spline order; see MOS-TRAIN-055 for the dispatch pin.
_INTERP = {"nearest": 0, "linear": 1, "bspline3": 3}


def _above(x, threshold: float):
    """Module-level and picklable on purpose: a lambda here is not reproducible
    across a multiprocess DataLoader and is not inspectable in a failure report."""
    return x > threshold


def _margin_voxels(margin_mm, spacing_mm):
    """MOS-TRAIN-047. Half-up, not numpy's banker's rounding: 0.5 mm at 1.0 mm
    spacing must be one voxel in every implementation, on every platform."""
    return tuple(int(math.floor(m / s + 0.5)) for m, s in zip(margin_mm, spacing_mm))


def build_chain(spec, keys=("image",), label_keys=()) -> Compose:
    torch.set_num_threads(1)                       # MOS-IMG-048, MOS-TRAIN-056
    all_keys = tuple(keys) + tuple(label_keys)

    # Image gets the declared interpolator; labels always get nearest-equivalent.
    resample_mode = tuple(
        _INTERP[spec.image_interpolator] if k in keys else _INTERP[spec.label_interpolator]
        for k in all_keys
    )

    return Compose(
        [
            # (1) orientation permutation and flips
            Orientationd(keys=all_keys, axcodes=spec.orientation_target),
            # (2) resample to model spacing
            Spacingd(
                keys=all_keys,
                pixdim=tuple(spec.target_spacing_mm),
                mode=resample_mode,
                padding_mode="border",
                align_corners=True,
                dtype=torch.float32,
            ),
            # (3) foreground crop, with min-extent semantics and HU-space padding
            ForegroundCropToMinSized(
                keys=all_keys,
                source_key=keys[0],
                select_fn=partial(_above, threshold=spec.foreground_crop.threshold_hu),
                margin=_margin_voxels(
                    spec.foreground_crop.margin_mm, spec.target_spacing_mm
                ),
                min_size=tuple(spec.foreground_crop.min_size_voxels),
                pad_value=spec.canonical_geometry.padding_output_value,
            ),
            # (4) intensity clip — a pure clip, NOT fused with (5). See MOS-TRAIN-045.
            ScaleIntensityRanged(
                keys=keys,
                a_min=spec.clip.min_hu,
                a_max=spec.clip.max_hu,
                b_min=spec.clip.min_hu,
                b_max=spec.clip.max_hu,
                clip=True,
            ),
            # (5) normalisation from spec-recorded dataset statistics
            NormalizeIntensityd(
                keys=keys,
                subtrahend=spec.normalisation.mean,
                divisor=spec.normalisation.std,
                nonzero=False,
                channel_wise=False,
            ),
            EnsureTyped(keys=all_keys, dtype=torch.float32, track_meta=True),
        ],
        unpack_items=False,
        log_stats=False,
    )
```

Training calls `build_chain(spec, keys=("image",), label_keys=("label",))`. Serving calls `build_chain(spec)`. That single argument difference is the entire difference between the training and serving preprocessing paths.

#### 17.3.3 Field-by-field serialization

**MOS-TRAIN-040** The mapping below is the complete serialization contract. Every `PreprocessingSpec` field of `MOS-IMG-049` that affects the forward transform MUST appear in exactly one row, and `build_chain` MUST read every row.

| `PreprocessingSpec` field | MONAI construct | Argument | Notes |
|---|---|---|---|
| `orientation_target` | `Orientationd` | `axcodes` | identical convention; `MOS-TRAIN-041` |
| `axis_order` | — | — | realised by `Orientationd`'s permutation; MUST NOT be applied a second time |
| `target_spacing_mm` | `Spacingd` | `pixdim` | axes `(0,1,2)` after permutation |
| `image_interpolator` | `Spacingd` | `mode[i]` for image keys | `nearest`→0, `linear`→1, `bspline3`→3 |
| `label_interpolator` | `Spacingd` | `mode[i]` for label keys | `MOS-TRAIN-043` |
| `canonical_geometry.padding_output_value` | `Spacingd`, `ForegroundCropToMinSized` | `padding_mode="border"`; `pad_value` | `MOS-TRAIN-049` |
| `foreground_crop.threshold_hu` | `ForegroundCropToMinSized` | `select_fn` | closed over the threshold, not a lambda |
| `foreground_crop.margin_mm` | `ForegroundCropToMinSized` | `margin` | converted by `_margin_voxels`; `MOS-TRAIN-047` |
| `foreground_crop.min_size_voxels` | `ForegroundCropToMinSized` | `min_size` | `MOS-TRAIN-048` |
| `foreground_crop.mode: none` | — | transform omitted entirely | not a no-op transform; the chain differs and so does the hash |
| `clip.min_hu`, `clip.max_hu` | `ScaleIntensityRanged` | `a_min`, `a_max`, `b_min`, `b_max`, `clip=True` | `MOS-TRAIN-045` |
| `normalisation.scheme: zscore_dataset` + `statistics_source: spec` | `NormalizeIntensityd` | `subtrahend=mean`, `divisor=std` | `MOS-TRAIN-046` |
| `normalisation.scheme: zscore_case` | `NormalizeIntensityd` | `subtrahend=None`, `divisor=None` | permitted only per `MOS-IMG-050` |
| `normalisation.scheme: zscore_foreground` | `NormalizeIntensityd` | `nonzero=True` | requires `foreground_crop.mode != none` |
| `normalisation.scheme: clip_scale` | `ScaleIntensityRanged` | `b_min`, `b_max` from `io.input.value_range`; `NormalizeIntensityd` omitted | the only scheme in which (4) and (5) are one transform |
| `io.input_dtype` | `EnsureTyped` | `dtype` | `float32` only in 0.2.0 |
| `patch.size_voxels` | `sliding_window_inference` | `roi_size` | 17.3.5 |
| `patch.sliding_window_overlap` | `sliding_window_inference` | `overlap` | `MOS-IMG-051` |
| `patch.blend` | `sliding_window_inference` | `mode` | `gaussian`→`"gaussian"`, `uniform`→`"constant"` |
| `patch.gaussian_sigma_scale` | `sliding_window_inference` | `sigma_scale` | |
| `patch.batch_size` | `sliding_window_inference` | `sw_batch_size` | forced to 1 in the self-test (`MOS-IMG-054`) |
| `tta.axes`, `tta.reduction` | `monai.transforms.Flip` + reduction | — | `MOS-TRAIN-053` |
| `backend.resampler` | — | — | records the dispatch, `MOS-TRAIN-055` |

##### Orientation

**MOS-TRAIN-041** `orientation_target` and MONAI's `axcodes` use the identical convention: a three-character string over `{L,R,A,P,S,I}` giving, for each array axis in increasing-index order, the anatomical direction the axis points toward. `MOS-IMG-038` defines it for the platform; nibabel, which MONAI delegates to, defines it the same way. `build_chain` MUST therefore pass `orientation_target` through unmodified, and MUST NOT contain a conversion table.

**MOS-TRAIN-042** CI MUST assert the identity rather than assume it: for every spec fixture, run `build_chain` on a synthetic volume with a known affine and assert `nibabel.orientations.aff2axcodes(out.affine) == spec.orientation_target`. A silently mirrored volume produces a plausible, structurally valid, anatomically wrong segmentation, and `MOS-REG-037` already states that orientation is the failure that passes every other check.

##### Image versus label interpolation

**MOS-TRAIN-043** `Spacingd`'s `mode` MUST be passed as a **tuple aligned to `keys`**, never as a scalar, whenever a label key is present. The image key gets `_INTERP[spec.image_interpolator]`; every label key gets `_INTERP[spec.label_interpolator]`, which `MOS-IMG-034` constrains to nearest-neighbour or `onehot_linear_argmax`.

**MOS-TRAIN-044** A scalar `mode` in a chain that carries a label key MUST be a CI failure. Spline-interpolating an integer label array at 1.5 mm target spacing invents label values that are not in `io.label_set` — a voxel of value 2.4 between lobe 2 and lobe 3 — and `MOS-IMG-049` makes a value outside the closed label set a `FAILED` job. The defect surfaces as a rare job failure on thin-slice studies months after the model ships, and is untraceable from the failure alone.

##### HU windowing

**MOS-TRAIN-045** The clip of step (4) MUST be a separate transform from the normalisation of step (5) for every `normalisation.scheme` except `clip_scale`. `ScaleIntensityRanged` with `b_min = a_min` and `b_max = a_max` and `clip=True` is a pure clip, and that is the required form. Fusing the two into one affine rescale is numerically equivalent only when the normalisation is itself affine over the same window, and it makes `MOS-IMG-031`'s ordered steps unobservable — a spec-to-chain diff can no longer show which of the two changed.

##### The source of the normalisation statistics

**MOS-TRAIN-046** `normalisation.mean` and `normalisation.std` MUST be computed over the **`train` partition of the frozen `DatasetSplit` only**, after steps (1) through (4) of the chain, over voxels inside the foreground crop. They MUST NOT be computed over the sealed `DatasetVersion` as a whole, over the `tune` partition, or over the `test` partition, and they MUST NOT be recomputed at serving time. The values MUST be written literally into the spec and the spec MUST set `statistics_source: spec`.

Statistics computed over the whole cohort are a leak: the test cases' intensity distribution enters the model's input normalisation, and the measured performance is optimistic by an amount nobody can estimate afterwards. It is the cheapest leak in medical imaging to commit and the hardest to see in a report, because no split check catches it — the split is honest; only the statistics crossed it.

**MOS-TRAIN-047** `foreground_crop.margin_mm` is converted to voxels once, at chain construction, at the declared `target_spacing_mm`, by `floor(mm / spacing + 0.5)`. It MUST NOT be converted at the source spacing of an individual case, because the crop happens after resampling, and it MUST NOT use `numpy.round`, whose half-to-even behaviour makes 0.5 mm at 1.0 mm spacing round to zero voxels while a hand-written implementation rounds to one.

##### Foreground cropping and the minimum extent

**MOS-TRAIN-048** MONAI's `CropForegroundd` offers `margin` and `k_divisible` but has no minimum-extent guarantee, and `MOS-IMG-049` requires one. MedicalOS therefore ships exactly one added transform, `ForegroundCropToMinSized`, which MUST:

1. compute the foreground bounding box with MONAI's own `generate_spatial_bounding_box` using `select_fn` and `margin`, clamped to the array;
2. expand the box symmetrically along each axis, clamped to the array, until the extent reaches `min_size_voxels[a]` or the array is exhausted, giving the odd voxel of an asymmetric deficit to the **high-index** side;
3. crop with `SpatialCrop`;
4. pad any residual deficit with `SpatialPad(method="symmetric", mode="constant", value=pad_value)`.

```python
# medicalos_preprocessing/transforms.py — the one transform MedicalOS adds to MONAI.
from monai.transforms import MapTransform, SpatialCrop, SpatialPad
from monai.transforms.utils import generate_spatial_bounding_box


class ForegroundCropToMinSized(MapTransform):
    def __init__(self, keys, source_key, select_fn, margin, min_size, pad_value):
        super().__init__(keys, allow_missing_keys=False)
        self.source_key = source_key
        self.select_fn = select_fn
        self.margin = tuple(int(m) for m in margin)
        self.min_size = tuple(int(m) for m in min_size)
        self.pad_value = float(pad_value)

    def _expand(self, start, end, shape):
        lo, hi = list(start), list(end)
        for a, want in enumerate(self.min_size):
            deficit = want - (hi[a] - lo[a])
            if deficit <= 0:
                continue
            # Odd deficit: the extra voxel goes high. This is the only tie-break in
            # the crop and it is fixed here so two implementations cannot differ by
            # one slice — which would change the tensor hash and nothing else.
            low_take = min(lo[a], deficit // 2)
            lo[a] -= low_take
            hi[a] = min(shape[a], hi[a] + (deficit - low_take))
            still = want - (hi[a] - lo[a])
            if still > 0:
                lo[a] = max(0, lo[a] - still)      # residual is padded, not invented
        return lo, hi

    def __call__(self, data):
        d = dict(data)
        start, end = generate_spatial_bounding_box(
            d[self.source_key],
            select_fn=self.select_fn,
            channel_indices=0,
            margin=self.margin,
            allow_smaller=False,
        )
        shape = tuple(d[self.source_key].shape[1:])
        start, end = self._expand(start, end, shape)
        crop = SpatialCrop(roi_start=start, roi_end=end)
        pad = SpatialPad(
            spatial_size=self.min_size, method="symmetric",
            mode="constant", value=self.pad_value,
        )
        for key in self.key_iterator(d):
            d[key] = pad(crop(d[key]))
        return d
```

**MOS-TRAIN-049** Padding MUST happen **inside step (3)**, in HU space, at `canonical_geometry.padding_output_value`, so that padded voxels traverse the clip of step (4) and the normalisation of step (5) exactly as real voxels do. A pad inserted after normalisation would need its own declared constant in normalised units, which is a second place for the two implementations to disagree.

**MOS-TRAIN-050** `SpatialPad`'s constant is passed as `value=`, the torch keyword, because the chain operates on `MetaTensor`. The numpy path takes `constant_values=` and silently ignores `value=`. The chain MUST therefore assert that its inputs are torch-backed, and `backend.resampler` MUST record the torch version (`MOS-TRAIN-055`). This is a one-line hazard that produces a zero-padded volume instead of an air-padded one, and it is invisible everywhere except the golden-fixture hash.

**MOS-TRAIN-051** `foreground_crop.min_size_voxels` MUST be element-wise greater than or equal to `patch.size_voxels`. This makes `sliding_window_inference`'s own padding unreachable, which removes a second, undeclared padding implementation from the path. Registration MUST reject a spec that violates it.

##### Patching, sliding window and blending

**MOS-TRAIN-052** Step (6) MUST be executed by `monai.inferers.sliding_window_inference` with every argument taken from the spec:

```python
# medicalos_preprocessing/window.py
from monai.inferers import sliding_window_inference

_BLEND = {"gaussian": "gaussian", "uniform": "constant"}


def infer_volume(spec, network, x, sw_device):
    # x: 1xCxDxHxW, the output of build_chain. cval is the normalised image of the
    # HU padding value; MOS-TRAIN-051 makes it unreachable, and it is declared anyway
    # so that "unreachable" is a checkable claim rather than an assumption.
    cval = (
        min(max(spec.canonical_geometry.padding_output_value, spec.clip.min_hu),
            spec.clip.max_hu)
        - spec.normalisation.mean
    ) / spec.normalisation.std
    return sliding_window_inference(
        inputs=x,
        roi_size=tuple(spec.patch.size_voxels),
        sw_batch_size=spec.patch.batch_size,
        predictor=network,
        overlap=spec.patch.sliding_window_overlap,
        mode=_BLEND[spec.patch.blend],
        sigma_scale=spec.patch.gaussian_sigma_scale,
        padding_mode="constant",
        cval=cval,
        device=x.device,
        sw_device=sw_device,
        progress=False,
    )
```

**MOS-TRAIN-053** `patch.blend: gaussian` MUST be the default for volumetric segmentation. With `uniform` blending, every voxel in an overlap region is the unweighted mean of predictions made at different positions within their patches, including positions at a patch border where the network's effective receptive field is truncated by zero padding inside the convolutions. The visible result is a rectilinear seam grid at the stride of the sliding window, strongest at `overlap` values below 0.5. Gaussian weighting with `sigma_scale: 0.125` drives the border contribution toward zero. No exception is raised in either case; the only evidence is the mask.

**MOS-TRAIN-054** `tta.axes` MUST contain only axes that were used as mirror augmentation during training. Averaging a prediction over a mirrored input assumes the network is approximately equivariant under that mirror; a network never trained with left–right mirroring is not, and for a laterality-bearing `io.label_set` — `lobe_left_upper` against `lobe_right_upper`, `pleural_effusion_left` against `pleural_effusion_right` — it is confidently not. The lung-lobe spec below therefore declares `tta.axes: []`. A non-empty `tta.axes` MUST be justified by the recorded training augmentation configuration on the `TrainingRun`, and CI MUST assert the correspondence.

##### Backend dispatch and determinism

**MOS-TRAIN-055** For a MONAI-serialized chain, `PreprocessingSpec.backend.resampler` MUST be the semicolon-separated string `monai==<exact>;torch==<exact>;USE_COMPILED=<0|1>;interp=<grid_pull|map_coordinates>`. `MOS-IMG-049` owns the field and permits any package-and-version string; this chapter fixes only the value convention for MONAI chains, and the `SimpleITK==2.3.1` form of `MOS-IMG-052` remains valid for non-MONAI ones.

The dispatch pin is not ceremony. An integer `mode` passed to `Spacingd` selects a spline order, and MONAI resolves it either to its compiled `grid_pull` kernel when `monai.config.USE_COMPILED` is true, or to a `scipy.ndimage` fallback when it is not. The two are not bit-identical at a cubic spline order. A wheel rebuilt without the compiled extension therefore changes every resampled voxel in the fourth decimal place, changes nothing observable in a metric, and changes the golden-fixture hash — which is the outcome the self-test is designed to produce.

**MOS-TRAIN-056** `build_chain` MUST call `torch.set_num_threads(1)` and the preprocessing path MUST run on CPU, per `MOS-IMG-048`. The recorder MUST additionally set `torch.use_deterministic_algorithms(True)` and record the outcome on the `TrainingRun`.

**MOS-TRAIN-057** At startup the native runner MUST assert that the installed `monai`, `torch` and `numpy` versions and the observed `monai.config.USE_COMPILED` equal the values encoded in `backend.resampler` and `backend.numpy`, and MUST refuse readiness on mismatch with the same structured event `preprocessing_selftest_failed` that `MOS-IMG-055` requires. The version assertion is a better error message than the hash mismatch, not a substitute for it: both MUST run, in that order.

#### 17.3.4 The spec the chain serializes to

**MOS-TRAIN-058** The following is a valid, complete `PreprocessingSpec` under the schema of `MOS-IMG-049`, and is the CI fixture for the MONAI serialization round trip.

```yaml
schema_version: "1.0"
id: prep.pulmo.lung-lobes
version: 2.1.0
model_id: pulmo.lung-lobes
model_version: 2.1.0

canonical_geometry:
  gantry_tilt:
    mode: reject
    max_deg: 0.0
  non_uniform_spacing: resample_to_uniform
  padding_output_value: -1024.0

orientation_target: "SPL"          # -> Orientationd(axcodes="SPL")
axis_order: [k, j, i]
target_spacing_mm: [1.5, 1.0, 1.0] # -> Spacingd(pixdim=(1.5, 1.0, 1.0))
image_interpolator: bspline3       # -> Spacingd mode 3 for "image"
label_interpolator: nearest        # -> Spacingd mode 0 for "label"
probability_interpolator: linear

clip:
  min_hu: -1024.0                  # -> ScaleIntensityRanged(a_min=-1024, b_min=-1024)
  max_hu: 300.0                    # -> ScaleIntensityRanged(a_max=300,   b_max=300)

normalisation:
  scheme: zscore_dataset
  statistics_source: spec
  mean: -604.12                    # computed on the `train` partition only
  std: 421.87                      # (MOS-TRAIN-046)

foreground_crop:
  mode: threshold_bbox
  threshold_hu: -300.0             # body contour; lungs lie inside it
  margin_mm: [5.0, 5.0, 5.0]       # -> margin=(3, 5, 5) voxels at target spacing
  min_size_voxels: [96, 224, 224]  # >= patch.size_voxels (MOS-TRAIN-051)

patch:
  size_voxels: [96, 192, 192]
  sliding_window_overlap: 0.5
  blend: gaussian
  gaussian_sigma_scale: 0.125
  batch_size: 1

tta:
  axes: []                         # MOS-TRAIN-054: laterality-bearing label set
  reduction: mean

io:
  input_dtype: float32
  input_layout: NCDHW
  channels: 1
  output_kind: label
  label_set:
    - {value: 0, name: background}
    - {value: 1, name: lobe_right_upper}
    - {value: 2, name: lobe_right_middle}
    - {value: 3, name: lobe_right_lower}
    - {value: 4, name: lobe_left_upper}
    - {value: 5, name: lobe_left_lower}

inverse:
  aggregate: gaussian_weighted_mean
  undo_crop: pad_background
  target_grid: canonical
  assert_shape_equal: true
  affine_tolerance: 1.0e-4

backend:
  resampler: "monai==1.4.0;torch==2.4.1+cpu;USE_COMPILED=1;interp=grid_pull"
  numpy: "numpy==1.26.4"

golden_fixture:
  path: medicalos/golden_input.nii.gz
  sha256: "e3f0b7411d8c26a95f04e7b3182cd6a4790fb25ce81a34d7602f9bc45e13a87d"
  output_tensor_sha256: "17c9a4e0d6b35f82914ea7c0538bd12f64903ae7c58d21b04f7e6a935cd08b2e"
  output_shape: [1, 1, 96, 192, 192]
  recorded_at: "2026-07-02T14:06:51Z"
  recorded_by_commit: "6d0be71a34f95c82e17d40bb95c3a7f2e08d41cb"
```

#### 17.3.5 Recording the golden fixture at training time

**MOS-TRAIN-059** `MOS-IMG-058` places the recording obligation on the training path. The recorder MUST be `medicalos_preprocessing.record.record_golden(spec, fixture_path) -> (sha256, shape)`, which MUST execute `MOS-IMG-054` steps 1 through 4 and differ from the worker's self-test only in that it returns the digest instead of comparing it. The comparison is the *only* line that may differ; a separate recording implementation is exactly the defect the test exists to catch.

**MOS-TRAIN-060** The pipeline MUST call `record_golden` as the last step before the artifact is signed, and MUST write its two outputs into `golden_fixture.output_tensor_sha256` and `golden_fixture.output_shape`. The spec MUST NOT be hand-edited afterwards; registration MUST reject a spec whose recorded hash does not reproduce (`MOS-TRAIN-062`).

**MOS-TRAIN-061** The golden fixture MUST be a volume the artifact may lawfully redistribute. It MUST come from a `DatasetVersion` whose `deidentification_status` is `public_deidentified` and whose licence permits redistribution, or from a synthetic phantom generated by the pipeline. It MUST NOT come from `clinical`-class ingest (`MOS-DATA-049`) under any consent basis, because the artifact is shipped to sites and to vendors and the fixture travels with it. The fixture's source `DatasetVersion` id and case digest MUST be recorded on the `TrainingRun`; `MOS-IMG-049` owns the `golden_fixture` block and this chapter does not extend it.

**MOS-TRAIN-062** Before signing, CI MUST re-run the self-test of `MOS-IMG-054` on a **clean checkout**, in a container built from the pinned `backend.resampler` and `backend.numpy` versions, on CPU, and MUST fail the build on mismatch. The self-test's value is that it fires at worker startup; its cost, if it first fires there, is a suspended deployment at an inconvenient hour. The same check in CI costs ninety seconds.

**MOS-TRAIN-063** The expected tensor itself MUST be shipped in the bundle at `medicalos/golden_tensor.f32` so that `MOS-IMG-055`'s `max(|T − T_expected|)` diagnostic is computable at a failing site. A hash mismatch with no residual magnitude cannot distinguish a version skew in the fourth decimal from a transposed volume.

**MOS-TRAIN-064** The `EvaluationRun` that produces a candidate's `capability_claims` MUST execute inference through `build_chain` and `infer_volume` with the same spec the artifact ships. `MOS-EVID-061` already binds the `PreprocessingSpec` version to the run; this requirement binds the *code path*, so that "the run used the spec" is true in the only sense that matters.

**MOS-TRAIN-065** A change to any `PreprocessingSpec` field, including `backend.resampler`, MUST produce a new spec version, a new `ModelVersion` and a new `EvaluationRun` (`MOS-IMG-046`). The pipeline MUST NOT offer a "re-record the hash" operation, because that operation's only effect is to make a real behavioural change pass a test designed to catch it.

**MOS-TRAIN-066** The round-trip property MUST be tested: for every spec fixture, `build_chain(parse(serialize(spec)))` MUST produce a `Compose` whose transform class names, order and constructor arguments are equal to those of `build_chain(spec)`, compared structurally. A spec field that survives serialization but never reaches the chain is a field that documents a behaviour the model does not have.

**MOS-TRAIN-067** The inverse transform (`MOS-IMG-032`) MUST be constructed from the same spec object by `medicalos_preprocessing.build_inverse(spec)`, using MONAI's `Invertd` over the recorded `MetaTensor` history where available and the declared `label_interpolator` / `probability_interpolator` otherwise. `MOS-IMG-033`'s shape and affine assertions are the acceptance test for it, and MUST run on every job, not only in CI.

---

### 17.4 Data acquisition and curation

#### 17.4.1 The harvest path

**MOS-TRAIN-068** The pipeline MUST acquire imaging only from the **de-identified side** of the DICOM Gateway, as the `dataset_export` consumer class of `MOS-DATA-021`, which forces `clean_pixel_data: true` and enforces burned-in-PHI screening. The pipeline MUST NOT hold a PACS credential (`MOS-DATA-002`), MUST NOT be reachable from the PACS network (`MOS-DATA-006`), MUST NOT request the `clinical_viewer` or `platform_writer` consumer classes, and MUST NOT hold `phi.reidentify`.

**MOS-TRAIN-069** Harvested content lives in the tenant's de-identified UID space and MUST stay there. `MOS-EVID-021` already binds a sealed `DatasetVersion` to its `uid_mapping_table_id`; the harvest MUST record the same `deid_policy_version` and `deid_key_version` on every candidate, because a key rotation opens a new UID space beside the old one (`MOS-DATA-032`) and a corpus that straddles two spaces is not comparable with itself.

**MOS-TRAIN-070** The harvest MUST exclude any study in `quarantine` (`MOS-DATA-047`, `MOS-DATA-048`), any study whose de-identification did not complete, and any series whose pixel-PHI detector returned a non-empty detection under a policy whose action was not `REMOVE`.

**MOS-TRAIN-071** The harvest MUST NOT pool studies across tenants into one `HarvestBatch`. A `DatasetVersion` combining tenants is permitted only when every contributing tenant independently satisfies 17.4.2, and its `source_id` MUST name every contributing tenant.

#### 17.4.2 Permission to train: the tenant flag and the legal basis

**MOS-TRAIN-072** Each `Tenant` MUST carry `training_use_allowed boolean NOT NULL DEFAULT false`. When it is false, the harvest MUST refuse, with `403` and problem type `training-use-not-permitted`. There is no per-study, per-user or per-environment override, and no platform-administrator bypass.

**MOS-TRAIN-073** Setting `training_use_allowed = true` MUST require a `TrainingDataPolicy` record on the tenant, and the platform MUST refuse the flag without it:

| Field | Constraint | Meaning |
|---|---|---|
| `tenant_id` | PK | the tenant |
| `legal_basis` | ∈ `broad_consent`, `research_ethics_approval`, `public_corpus_licence`, `data_processing_agreement`, `national_derogation` | the basis the tenant asserts |
| `basis_reference` | `NOT NULL` | the instrument: approval number, agreement id, licence SPDX id |
| `basis_document_digest` | `sha256:…`, nullable | digest of the uploaded instrument, when one is held |
| `scope` | `jsonb NOT NULL` | `{"modalities":["CT"],"body_parts":["CHEST"],"capabilities":["lung_lobes"],"date_from":"2023-01-01","date_to":null}` |
| `permits_redistribution` | `boolean NOT NULL DEFAULT false` | whether a derived artifact may leave the tenant |
| `recorded_by`, `recorded_at` | `NOT NULL` | the named human who asserted it |
| `expires_at` | nullable | after which the flag auto-reverts to false |
| `revoked_at`, `revocation_reason` | nullable | withdrawal |

**MOS-TRAIN-074** The platform MUST NOT interpret, validate against a registry, or assess the sufficiency of `legal_basis` or `basis_reference`. It MUST store them, digest them, display them, refuse the harvest without them, and reproduce them in every `DatasetVersion`'s provenance. This is the same posture `MOS-SAFE-029` takes toward `regulatory_status`, for the same reason: the assertion is the site's, and a platform that validates it has quietly assumed it.

**MOS-TRAIN-075** A harvest MUST be refused for any study outside `scope`, and MUST be refused entirely once `expires_at` has passed or `revoked_at` is set. Expiry MUST flip `training_use_allowed` to false automatically and MUST emit `tenant.training_use.expired`.

**MOS-TRAIN-076** Revocation MUST NOT retroactively alter a sealed `DatasetVersion` — `MOS-EVID-013` forbids editing sealed objects — but it MUST block every new harvest and MUST be recorded on the tenant so that a subsequent `DatasetVersion` cannot be sealed from candidates acquired after it. What erasure means for imaging already committed to a sealed cohort is `OQ-16` and is not resolved here.

**MOS-TRAIN-077** `permits_redistribution = false` MUST block the tenant's data from any `vendor_evidence` export (`MOS-EVID-011`) and from use as a golden fixture (`MOS-TRAIN-061`).

#### 17.4.3 The curation queue

**MOS-TRAIN-078** The harvest MUST produce `HarvestCandidate` rows, never a `DatasetVersion` directly. `MOS-EVID-016` forbids a DatasetVersion defined by a live query; the curation queue is where the materialised list is decided.

**MOS-TRAIN-079** Every candidate MUST carry: `patient_key` (`MOS-EVID-010`), the de-identified study, series and instance UIDs, the geometry descriptor of `MOS-IMG-036`, the `acquisition_profile` fields of `MOS-TRAIN-084`, `institution_key`, whether the target capability ever ran on the study and with what outcome, and the `deid_policy_version`. It MUST NOT carry `PatientName`, `PatientBirthDate`, `AccessionNumber`, institution free text, or any source-space UID.

**MOS-TRAIN-080** Every candidate MUST receive an explicit `CurationDecision` by a named human before it can enter a `DatasetVersion`. Auto-inclusion MUST NOT be implemented. Auto-*exclusion* on a mechanical predicate — gantry tilt rejected, non-uniform spacing, missing series, duplicate `series_pixel_digest` — IS permitted, MUST record the predicate id and its digest, and MUST be visible in the queue as an exclusion rather than an absence.

| `CurationDecision` field | Constraint | Meaning |
|---|---|---|
| `candidate_id`, `decided_by`, `decided_at` | `NOT NULL` | the named human |
| `decision` | ∈ `include`, `exclude`, `defer` | |
| `reason_code` | closed set; `NOT NULL` when `exclude` | `quality_artefact`, `wrong_anatomy`, `wrong_phase`, `prior_treatment`, `duplicate_patient`, `geometry_unsupported`, `annotation_infeasible`, `out_of_scope` |
| `note` | free text, no PHI | reviewer note |
| `review_seconds` | `NOT NULL` | time on task, for the queue's own quality signal |

**MOS-TRAIN-081** Exclusion reasons MUST be aggregated per cohort and reproduced in any `ValidationReport` citing the resulting `DatasetVersion`. A cohort assembled by excluding a third of the candidates as `quality_artefact` describes a different population from the one the model will meet, and the reader of the report must be able to see that.

**MOS-TRAIN-082** `DatasetVersion` sealing from accepted candidates is Chapter 7's operation (`MOS-EVID-015`); the pipeline MUST call it and MUST NOT write the manifest itself.

#### 17.4.4 Trap 1 — the feedback loop

A model that has been deployed generates the traffic from which its successor is harvested. The studies in the corpus are the studies the service was routed to, inside the applicability envelope the service declared. The candidate annotations are seeded from the service's own masks, and a reader correcting a mask corrects what is visibly wrong and leaves what looks right. The successor is then trained on its predecessor's decision boundary, evaluated against a reference standard partly derived from it, and measured as better. Its systematic misses are absent from the training signal because no human ever saw them, and its confident errors have become labels.

**MOS-TRAIN-083** A `HarvestBatch` MUST declare a versioned `SamplingPlan` before any candidate is drawn, and the realised per-case sampling weight MUST be persisted on the candidate. Sampling MUST NOT be "everything the service ran on", and MUST NOT be a random sample of it either: the base rate of the cases that carry information is too low.

The plan MUST stratify on at least `(score_band, review_outcome, ran_on_platform, acquisition_bucket)` and MUST satisfy:

| Stratum | Minimum sampling rule | Why |
|---|---|---|
| `review_outcome ∈ {REJECTED, MODIFIED}` | **census** — every such case, sampling fraction 1.0 | these are the only cases in which a human demonstrably disagreed with the model; they are the entire corrective signal and there are never many |
| lowest `score_band` (below the operating point, or the bottom decile) | sampling fraction ≥ 3× that of the top band | the decision boundary lives here; the top band teaches the model what it already knows |
| `ran_on_platform = false` | ≥ `min_naive_fraction`, default **0.20** of patients | studies the capability never saw — no Job, or `REJECTED` at triage or by the envelope. Without this stratum the corpus can only ever describe the inside of the current envelope, and the model can never be shown to work outside it |
| plausibility `fail` or `warn` (`MOS-EVID-110`) | census | the platform already flagged these as implausible output; they are free hard negatives |

**MOS-TRAIN-084** Every candidate MUST record `acquisition_profile` at curation time, copied from the source header without imputation, with a missing value serialised as `null` and never as a default (`MOS-EVID-020`): `manufacturer`, `manufacturer_model_name`, `convolution_kernel`, `convolution_kernel_class`, `slice_thickness_mm`, `pixel_spacing_mm`, `kvp`, `exposure_mas`, `contrast_phase`, `iterative_recon_strength`, `station_key`, `institution_key`, `study_year`. Recording it at sealing time from the manifest is too late: stratification has to be computable *before* the cohort is chosen.

**MOS-TRAIN-085** Annotation provenance MUST be recorded per case, with the values and rules of `MOS-TRAIN-098`. A corpus that cannot say which of its labels came from a model is a corpus whose feedback-loop exposure is unmeasurable.

**MOS-TRAIN-086** The **model-seeded fraction** MUST be computed per partition of the frozen `DatasetSplit` and persisted on the `TrainingRun`, and MUST satisfy:

| Partition | Ceiling on `annotation_provenance = model_seeded_corrected` | Ceiling on `model_output_unreviewed` |
|---|---|---|
| `train` | 0.50 (declared per campaign; MUST NOT exceed 0.50) | 0.00 |
| `tune` | 0.25 | 0.00 |
| `test` | 0.00 | 0.00 |

The `test` ceiling is `MOS-EVID-042` restated for the producing side: a reference standard derived from the artifact under evaluation, or from any artifact sharing its weights, training data or postprocessing, is not a reference standard. Exceeding a ceiling MUST block the split from freezing, not merely warn.

**MOS-TRAIN-087** Corpus generation MUST be tracked. A case annotated de novo has `corpus_generation = 0`; a case whose annotation was seeded by a model whose own training corpus had maximum generation *g* has `corpus_generation = g + 1`. A case with `corpus_generation >= 2` MUST NOT be used in any partition. Compounding is the part of this trap that has no natural brake: each generation's errors are the next generation's ground truth, the measured numbers improve monotonically, and nothing in the evidence plane notices, because every individual run is honest.

#### 17.4.5 Trap 2 — site specialization

The second trap is subtler because it never produces a wrong number. A corpus harvested from one hospital's traffic carries that hospital's scanners, its reconstruction kernels, its contrast protocol, its referral pattern and its disease prevalence. A model trained on it and measured on a held-out split of it is measured on its own acquisition distribution. Every figure in the report is correct. The model then loses several points of Dice at the second site, and `MOS-EVID-097`'s envelope, derived from that same cohort's `acquisition_profile`, was never wide enough to have warned anyone.

**MOS-TRAIN-088** Sealing a `DatasetVersion` whose `Dataset.purpose` is `training` from a `HarvestBatch`, and issuing any `vendor_evidence` report against a cohort so sealed, MUST run the **corpus stratification check** and MUST record its full result as a `CorpusStratificationReport` on the batch. A `fail` MUST block sealing.

| Check | Statistic | Default bound | Outcome on breach |
|---|---|---|---|
| C1 site concentration | max share of patients from one `institution_key` | ≤ 0.60 | `fail` |
| C2 scanner concentration | max share of series from one `(manufacturer, manufacturer_model_name)` | ≤ 0.70 | `fail` |
| C3 kernel coverage | number of `convolution_kernel_class` values each holding ≥ 0.10 of series | ≥ 2 | `fail` |
| C4 thickness spread | distinct `slice_thickness_mm` values, and p90/p10 ratio | ≥ 3 values **or** ratio ≥ 1.5 | `warn` |
| C5 envelope coverage | for every categorical value and every numeric decile the declared `ApplicabilityEnvelope` marks `IN`, the number of patients present | ≥ 20 per cell | `fail` |
| C6 temporal spread | share of patients from the single most-represented `study_year` | ≤ 0.75 | `warn` |
| C7 single-site declaration | `institution_key` cardinality | ≥ 2 for `vendor_evidence`; exactly 1 permitted for `site_acceptance` | `fail` / `n/a` |

**MOS-TRAIN-089** `institution_key` MUST be `HMAC-SHA256(tenant_salt, InstitutionName)` truncated to 10 bytes and base32-encoded, by the same construction `MOS-EVID-035` uses for accession numbers. The raw institution name MUST NOT be stored on a candidate, in a split manifest or in a report (`MOS-EVID-116`).

**MOS-TRAIN-090** C5 is the check that matters most and the one most easily rationalised away. `MOS-EVID-098` already forbids widening an envelope bound without a run on a cohort that covers it; C5 is the same rule applied at the producing end, so that "validated on 2.5 mm archival data, declared for 0.6 mm" fails at harvest rather than at envelope publication.

**MOS-TRAIN-091** A check MAY be waived only by writing a `waiver` object carrying `{check_id, waived_by, waived_at, rationale, observed, bound}`, following the waiver discipline of `MOS-EVID-036`. The waiver MUST be reproduced in full in every `ValidationReport` citing the cohort. There is no silent waiver, and a waived C1 or C7 MUST additionally force the string "single-site cohort" into the report's cohort summary.

**MOS-TRAIN-092** `warn` outcomes MUST be reported and MUST NOT block. The distinction matters: C4 and C6 describe a corpus that is narrow, which is a fact the reader needs; C1, C2, C3, C5 and C7 describe a corpus that cannot support the claim being made from it.

**MOS-TRAIN-093** The stratification report MUST be computed from `acquisition_profile` alone and MUST be recomputable from the sealed manifest, so that a reader can verify it offline from the exported bundle (`MOS-EVID-122`).

**MOS-TRAIN-094** Per-stratum evaluation is not a substitute for a stratified corpus, and the pipeline MUST NOT treat a passing subgroup criterion as discharging C1–C7. `MOS-EVID-067` guarantees that strata are persisted and gateable; it cannot manufacture cases from a scanner the cohort does not contain.

---

### 17.5 Annotation and `AnnotationSet` production

#### 17.5.1 MONAI Label as the annotation surface

**MOS-TRAIN-095** MONAI Label MUST be the annotation surface for mask, bounding-box and point annotation types. It MUST run inside the evidence-plane network, MUST read imaging through the Gateway as the `dataset_export` consumer class, and MUST NOT be given a PACS route (`MOS-DATA-006`).

**MOS-TRAIN-096** Every MONAI Label session MUST authenticate as a MedicalOS `User`, and the `reader_id` written into `annotation_readers` (`MOS-EVID-038`) MUST be derived from that identity. A shared login, an anonymous session, a service account, or a deployment with authentication disabled MUST be refused. `MOS-EVID-038` requires the set to *name* its readers; a shared account makes that requirement unsatisfiable after the fact.

**MOS-TRAIN-097** For every campaign the pipeline MUST populate Chapter 7's reader fields and MUST NOT invent parallel ones: `role` (`radiologist`, `resident`, `algorithm`, `registry_extract`), `years_experience`, `board_certified`, `specialty`, `tool` — for this stack the literal form `"MONAI Label 0.8.4 + 3D Slicer 5.6.2"` — `instructions_uri`, and `blinded_to`.

#### 17.5.2 `annotation_provenance`

**MOS-TRAIN-098** Every annotated case MUST carry `annotation_provenance` with exactly one of three values. The enum is owned by this chapter.

| Value | Definition | Permitted use |
|---|---|---|
| `de_novo` | a reader produced the annotation from the image alone, with `blinded_to` containing `model_output` | any partition; the only value permitted in a `reference_of_record` set |
| `model_seeded_corrected` | a model output was presented to a reader as a starting mask and the reader edited and accepted it | `train` and `tune` only, under the ceilings of `MOS-TRAIN-086` |
| `model_output_unreviewed` | a model output with no human edit | never permitted in an `AnnotationSet`; may exist only as a `seed` record |

**MOS-TRAIN-099** A `model_seeded_corrected` case MUST additionally record `{seed_model_id, seed_model_version, seed_evaluation_run_id, seed_operating_point, seed_dice, voxels_added, voxels_removed, edit_seconds}`. `seed_dice` is the agreement between the seed and the reader's accepted mask, computed by the platform's own metric code (`MOS-EVID-047`), never by MONAI Label.

**MOS-TRAIN-100** `seed_dice` MUST be reported per campaign, because it is the direct measurement of the anchoring this chapter is defending against. A campaign whose mean `seed_dice` exceeds 0.98 has produced labels, not corrections, and its output SHOULD be treated as `model_output_unreviewed` until the cause is understood.

**MOS-TRAIN-101** Every campaign that uses seeding MUST include a **de-novo control arm**: a declared fraction of at least 0.10 of cases, drawn by the `SamplingPlan` rather than chosen by readers, annotated `de_novo` by the same reader pool under the same instructions. The control arm's inter-reader agreement (`MOS-EVID-043`) is the campaign's own noise floor.

**MOS-TRAIN-102** A campaign that uses seeding for a `train` or `tune` cohort MUST additionally run a **paired anchoring sub-study**: a declared subset annotated both ways, either by two different readers from the pool or by one reader with a washout of at least 30 days. The published statistic is `anchoring_delta`, the mean per-case difference in reference volume between the seeded and de-novo annotations of the same case, with a patient-level cluster bootstrap interval (`MOS-EVID-057`). It MUST be reproduced in every `ValidationReport` citing the `AnnotationSet`. Without it, "the readers corrected the model" is an assertion; with it, it is a number with an interval.

**MOS-TRAIN-103** A reader who corrected a seeded mask from model family *F* MUST NOT serve as a reader on an `AnnotationSet` used to evaluate any version of *F*. Reader-level anchoring is not removed by changing the model version; it is carried in the reader.

#### 17.5.3 Readers, blinding and consensus

**MOS-TRAIN-104** Readers MUST NOT be shown the model's confidence score, its operating point, another reader's annotation, or the clinical report, unless the corresponding value is recorded in `blinded_to` as *not* blinded. The annotation client MUST enforce this rather than rely on instructions.

**MOS-TRAIN-105** Consensus MUST be computed by `medicalos-evidence`, not by MONAI Label. MONAI Label produces per-reader masks; the platform reduces them under the `consensus_rule` and `consensus_params` of `MOS-EVID-039`. The reduction MUST be reproducible from the persisted per-reader masks alone, which is why `staple` must record its iterations, tolerance, initial sensitivity and specificity, and RNG seed — a tool-side reduction records none of them and cannot be re-derived.

**MOS-TRAIN-106** MONAI Label's own training loop MUST be disabled. Its `train` endpoint MUST be unreachable, its active-learning re-seeding from a locally trained checkpoint MUST be off, and any weights it produces MUST NOT be registrable as a `ModelVersion`. A labelling tool that trains on the labels it is collecting and re-seeds the next case from that model is the closed loop of `MOS-TRAIN-005`, implemented inside a tool, with no `DatasetVersion`, no split, no evaluation and no report. It is the single easiest way to defeat this chapter, and it is a default in the tool.

**MOS-TRAIN-107** Where MONAI Label's sampling strategies are used to order the queue, the strategy id, its version and the realised case order MUST be persisted per case. Active learning is not a convenience; it changes which cases exist in the cohort, and a cohort assembled by uncertainty sampling is not a random sample of anything. `MOS-EVID-028`'s `assignment_method` is the field that carries this forward into the split.

**MOS-TRAIN-108** Per-reader masks MUST be persisted individually and MUST be written in **source geometry** with origin, spacing and direction preserved to 1e-4 (`MOS-EVID-041`). MONAI Label operates on resampled volumes; the exporter MUST apply the inverse transform of `MOS-IMG-032` before writing, and MUST assert `MOS-IMG-033`'s shape and affine equality. An annotation stored in model space is refused at the Chapter 7 boundary, which is the right place for it to fail, but the exporter is where it must not be produced.

**MOS-TRAIN-109** `reference_volume_ml` MUST be computed by the platform's own volume code (`MOS-EVID-044`) from the persisted source-geometry mask. MONAI Label's reported volume MUST NOT be stored.

**MOS-TRAIN-110** An `AnnotationSet` MUST be frozen by an explicit call once the campaign closes. The pipeline MUST NOT append to a frozen set; adding a reader or a case produces a new `AnnotationSet` bound to the same `DatasetVersion`.

### 17.6 Dataset sealing and splits

Chapter 7 owns `Dataset`, `DatasetVersion`, `DatasetSplit` and `AnnotationSet` — their fields, their digests, their sealing semantics and the five leakage checks. This section owns the **production procedure**: how a clinician's working cohort becomes those objects, which principal is allowed to touch which partition, and the two gates the procedure cannot pass without. Nothing here redefines a Chapter 7 entity; where a field name appears it is Chapter 7's field name.

#### 17.6.1 Where PHI stops

**MOS-TRAIN-199** Every component of this pipeline — curation UI, annotation server, orchestrator, training job, evaluation job, conversion job, packaging job — MUST obtain imaging exclusively through the `dataset_export` consumer class of Chapter 3 (`MOS-DATA-021`), which forces `clean_pixel_data: true` and the de-identified UID space. No pipeline component MUST hold, mount, receive or derive a PACS credential (`MOS-DATA-002`), and no pipeline principal MUST be resolvable to the `clinical_viewer`, `service` or `platform_writer` consumer classes.

**MOS-TRAIN-200** The pipeline MUST NOT hold the tenant UID mapping key or any read path into `deid_uid_map` (Chapter 3, `MOS-DATA-033`). `patient_key` (`MOS-EVID-010`) and the de-identified UIDs arrive already computed, inside the manifest the exporter produced. The pipeline therefore has no mechanism by which a training artifact, a metric row or a report can be re-identified, and that is a property of the grant table rather than of the code.

**MOS-TRAIN-201** The reverse direction MUST also be closed: no artifact this pipeline produces — bundle, checkpoint, plan, per-case metric row, dossier, report — MUST carry a source SOP Instance UID, a `PatientID`, an `AccessionNumber` or a study date. Chapter 7 imposes this on the `ValidationReport` (`MOS-EVID-116`); it holds for every intermediate the pipeline writes, because an intermediate that leaks PHI leaks it into an object store with a different retention policy from the PACS.

#### 17.6.2 Curation is a materialised list, and the exclusions are kept

**MOS-TRAIN-202** Curation MUST produce a `CurationBatch`: an explicit, materialised list of candidate `(patient_key, study_instance_uid, series_instance_uid)` triples with a per-candidate disposition. A `DatasetVersion` MUST NOT be sealed from a query, a folder, a DICOMweb filter or a view (`MOS-EVID-016`); the `CurationBatch` is the object that makes that rule satisfiable in practice.

```sql
-- Semantic definition. Physical form follows Chapter 12's conventions.
CREATE TABLE curation_batch_items (
  batch_id            text NOT NULL,
  patient_key         text NOT NULL,
  study_instance_uid  text NOT NULL,
  series_instance_uid text NOT NULL,
  disposition         text NOT NULL CHECK (disposition IN ('include','exclude')),
  reason_code         text,                       -- closed vocabulary, per Dataset
  reason_note         text,
  decided_by          text NOT NULL,
  decided_at          timestamptz NOT NULL,
  PRIMARY KEY (batch_id, series_instance_uid),
  CHECK ((disposition = 'exclude') = (reason_code IS NOT NULL))
);
```

**MOS-TRAIN-203** Every excluded candidate MUST be retained with a `reason_code` drawn from a closed vocabulary declared on the `Dataset`. Deleting an excluded row MUST be refused. This is the rule that keeps a cohort honest: cases dropped one at a time because "the model does badly on them" is the mechanism by which a training corpus becomes optimistic, and it is invisible from the sealed manifest alone — the sealed manifest shows only what survived.

**MOS-TRAIN-204** `reason_code` MUST NOT include any value whose meaning is a model outcome. The vocabulary is a statement about the *image or the patient* — `slice_thickness_out_of_spec`, `gantry_tilt_uncorrectable`, `contrast_phase_unknown`, `prior_resection`, `motion_artifact_reader_rejected`, `annotation_unavailable`, `duplicate_of_included_series`. A curation tool that offers `model_performed_poorly`, `outlier`, `hard_case` or an equivalent MUST be treated as non-conformant.

**MOS-TRAIN-205** The exclusion set MUST be recorded as the `derivation` object of `MOS-EVID-022` on the sealed version. When exclusion was decided by a machine-checkable predicate, `predicate_digest` MUST be the digest of that predicate's serialised form. When exclusion was decided case by case by a human, `derivation` MUST carry `{"op":"exclude","reason":"reader_judgement","removed_series":N}` and MUST enumerate the excluded `patient_key`s; a human judgement has no predicate and MUST NOT be represented as if it had one.

#### 17.6.3 Which partition each stage may read

**MOS-TRAIN-206** The pipeline's read permissions on evidence data are the table below. They are grants and RLS predicates, not conventions in code.

| Stage | `Dataset.purpose` | Partition | Pipeline may read |
|---|---|---|---|
| Model fitting | `training` | `train` | yes |
| Checkpoint, threshold, preprocessing-variant selection | `training` | `tune` | yes |
| Conversion equivalence cohort (17.9.3) | `training` | `tune` | yes |
| Candidate `EvaluationRun` (17.8) | `training` | `test` | only through the evaluation job, after `MOS-TRAIN-141` |
| Deployment gate, site acceptance | `acceptance` | `test` | **no** |
| Monitoring | `monitoring` | — | **no** |

**MOS-TRAIN-207** The training orchestrator's service account MUST NOT be granted read on any `DatasetVersion` whose `Dataset.purpose` is `acceptance` or `monitoring`. Chapter 7 already forbids binding a `training` or `tuning` dataset to an acceptance binding (`MOS-EVID-082`); this is the other half of the same wall, and without it the acceptance cohort is one convenience script away from being a validation set.

#### 17.6.4 The seal procedure

**MOS-TRAIN-208** Sealing MUST execute the following steps in this order, each recorded as a pipeline step. A failure at any step MUST leave no `dataset_versions` row and no manifest object.

1. Freeze the `CurationBatch`; take the `disposition = 'include'` rows.
2. Apply the `patient_key` alias table of `MOS-TRAIN-116` before anything else.
3. Retrieve each series through `dataset_export`.
4. Compute `series_pixel_digest` per `MOS-EVID-018` over stored pixel values.
5. Build one manifest line per series in the format of Chapter 7 §7.3.2, with `sop_instance_uids` in the canonical slice order of Chapter 4 (`MOS-EVID-017`).
6. Sort by `(patient_key, study_instance_uid, series_instance_uid)`; compute `manifest_digest` per `MOS-EVID-009`.
7. Compute `acquisition_profile` per `MOS-EVID-024`.
8. Write the manifest object and the `dataset_versions` row in one transaction (`MOS-EVID-015`).

**MOS-TRAIN-209** Sealing MUST be idempotent on content. Re-running steps 1–8 on the same included set MUST produce a byte-identical manifest and therefore the same `manifest_digest`; the pipeline MUST detect the collision against the `UNIQUE` constraint on `manifest_digest` and reuse the existing `DatasetVersion` rather than mint a second identity for the same bytes.

**MOS-TRAIN-111** A seal MUST be refused when a retrieved series is rejected by the geometry contract of Chapter 4 under the `PreprocessingSpec` the capability will serve — gantry tilt beyond the declared `canonical_geometry.gantry_tilt.max_deg`, or non-uniform spacing under `canonical_geometry.non_uniform_spacing: reject` (`MOS-IMG-049`). The refusal MUST name the offending `series_instance_uid` and MUST be resolvable only by excluding that series with a recorded `reason_code` (`MOS-TRAIN-203`). It MUST NOT be resolvable by editing the spec. Widening the spec to admit the training data changes what is served to every patient, and it is the cheapest-looking fix on the screen at that moment.

#### 17.6.5 The split as a frozen manifest

**MOS-TRAIN-112** Assignment MUST be a pure function evaluated once, whose output is the manifest. A seed is an input to that one evaluation and a fact recorded in `assignment_method`; it is never the split (`MOS-EVID-028`). The reference implementation:

```python
# medicalos/pipeline/split.py
import hashlib

PARTITIONS = (("train", 70), ("tune", 10), ("test", 20))

def assign(patient_keys, strata, seed_label, partitions=PARTITIONS):
    """Deterministic, stratified, patient-level. Returns manifest lines, not a seed.

    patient_keys: sorted list of patient_key strings
    strata:       patient_key -> dict of stratum name -> stratum value
    seed_label:   a stable string, e.g. "pulmo.pleural-effusion/2026-03-11"
    """
    buckets = {}
    for pk in sorted(patient_keys):
        key = tuple(sorted(strata[pk].items()))
        buckets.setdefault(key, []).append(pk)

    lines, cuts = [], []
    acc = 0
    for name, pct in partitions:
        acc += pct
        cuts.append((name, acc))

    for key, members in sorted(buckets.items()):
        ordered = sorted(
            members,
            key=lambda pk: hashlib.sha256(f"{seed_label}|{pk}".encode()).hexdigest(),
        )
        n = len(ordered)
        start = 0
        for name, upto in cuts:
            end = (n * upto) // 100
            for pk in ordered[start:end]:
                lines.append({"patient_key": pk, "partition": name,
                              "fold": None, "stratum": dict(key)})
            start = end
    return sorted(lines, key=lambda r: r["patient_key"])
```

**MOS-TRAIN-113** The split MUST be stratified on the capability's declared clinical stratum — for `pleural_effusion`, `reference_volume_ml` decile (`MOS-EVID-067`) — and SHOULD additionally be stratified on `acquisition.manufacturer` and a `slice_thickness_mm` band. `stratified_by` MUST record whatever was used. An unstratified 70/10/20 over 800 patients routinely places every study from a minority scanner model on one side of the split, and the resulting subgroup floor (`MOS-EVID-088`) is then evaluated on zero cases and reports `SKIPPED` rather than failing.

**MOS-TRAIN-114** The pipeline MUST refuse to start a training run against a split whose `test` partition holds fewer than 30 distinct `patient_key`s. Below that the candidate `EvaluationRun` the run exists to produce cannot return anything but `INDETERMINATE` (`MOS-EVID-083`), and Chapter 7 already sets the same floor on acceptance cohorts (`MOS-EVID-026`). Spending GPU time to reach a verdict that is structurally unreachable is the failure this check exists to prevent.

#### 17.6.6 The leakage check as a blocking gate

Chapter 7 runs L1–L5 at freeze time (`MOS-EVID-034`). This section adds a second, stricter execution: **at the start of every training run, against the frozen manifest, with the run refusing to begin on a hit.** Freeze-time checking protects the evidence; run-time checking protects the model, and the two have different waiver rules.

**MOS-TRAIN-115** L1, L2, L3 and L5 MUST be re-executed by the training job against the frozen split manifest before the first batch is loaded, and a `fail` on any of them MUST abort the run. A `MOS-EVID-036` waiver MUST NOT permit a training run to proceed on L1, L2, L3 or L5. A waiver is a statement about what a report may claim; it is not a licence to fit on the test set. L4 (near-duplicate, 64-bit dHash Hamming ≤ 6) MAY be waived for a training run because the detector has a real false-positive rate on serial screening studies, and the waiver MUST then be reproduced on the `TrainingRun` and on every `ValidationReport` citing the resulting candidate.

**MOS-TRAIN-116** L1 is patient disjointness and MUST be evaluated over `patient_key` alone. This is the check, at both levels — over the manifest lines, before they are loaded, and over the loaded rows:

```python
# medicalos/pipeline/leakage.py — manifest-level L1. Blocking.
def l1_patient_disjointness(split_manifest_lines):
    """A patient_key in two non-excluded partitions. Returns violations, not a bool."""
    seen, violations = {}, []
    for line in split_manifest_lines:
        pk, part = line["patient_key"], line["partition"]
        if part == "excluded":
            continue
        prior = seen.setdefault(pk, part)
        if prior != part:
            violations.append({"patient_key": pk,
                               "partitions": sorted({prior, part})})
    return violations
```

```sql
-- Row-level L1, over the loaded split. Blocking: a non-empty result aborts the run.
SELECT m.patient_key,
       array_agg(DISTINCT m.partition ORDER BY m.partition) AS partitions
FROM   dataset_split_members m
WHERE  m.split_id = $1
  AND  m.partition <> 'excluded'
GROUP  BY m.patient_key
HAVING count(DISTINCT m.partition) > 1;
```

Both MUST run. The row-level query can only return rows if the loader is defective, because `dataset_split_members` is keyed on `(split_id, patient_key)` (Chapter 7 §7.4.1) — which is exactly why the manifest-level check is the one that does the work, and why "the primary key makes this impossible" is not an argument for omitting it.

**MOS-TRAIN-117** L1 MUST be evaluated over `patient_key`, across **all studies and all dates**, and MUST NOT be evaluated over any study-, series-, accession- or date-scoped key, and MUST NOT be relaxed by a time window. A patient with a 2019 screening CT in `train` and a 2024 follow-up in `test` is a leak: it is the same anatomy, the network has memorised its ribcage, its emphysema pattern and its pleural contour, and the test estimate is optimistic by an amount that cannot be bounded after the fact. There is no interval after which two studies of one patient become independent observations, and a split tool that offers a "minimum days between studies" option is offering a way to defeat this check.

**MOS-TRAIN-118** L1 alone cannot see a patient registered under two `PatientID`s, because `patient_key` is `HMAC(tenant_salt, issuer|patient_id)` (`MOS-EVID-010`) and two MRNs produce two keys. An L2 (study) or L5 (accession) hit MUST therefore be treated as a **defect in patient identity**, not as an independent finding. The remedy MUST be: record the two keys as aliases in the tenant `patient_key_aliases` table, re-seal the `DatasetVersion` with the alias applied at step 2 of `MOS-TRAIN-208`, and re-freeze the split. Resolving an L2 or L5 hit by moving the offending study across partitions MUST be refused — it leaves two identities for one patient, L1 still passes, and the next version of the cohort leaks again in a new place.

```sql
CREATE TABLE patient_key_aliases (
  tenant_id            uuid NOT NULL,
  canonical_patient_key text NOT NULL,
  aliased_patient_key   text NOT NULL,
  evidence              text NOT NULL,   -- which check found it: 'L2' | 'L5' | 'manual'
  recorded_by           text NOT NULL,
  recorded_at           timestamptz NOT NULL,
  PRIMARY KEY (tenant_id, aliased_patient_key),
  CHECK (canonical_patient_key <> aliased_patient_key)
);
```

**MOS-TRAIN-119** Aliases MUST be applied by the exporter before manifest lines are built, so that a sealed manifest never contains two keys for one patient. An alias discovered after sealing MUST produce a new `DatasetVersion` with `parent_version_id` set and a `derivation` recording the merge; it MUST NOT mutate the sealed one (`MOS-EVID-014`).

#### 17.6.7 What the pipeline hands to the trainer

**MOS-TRAIN-120** A training job's cohort argument MUST be exactly the tuple `(dataset_version_digest, split_digest, annotation_digest, fit_partition)`. The job MUST NOT accept a filesystem path, a directory, a bucket prefix or a glob as a cohort argument, MUST NOT be able to enumerate the object store, and MUST fail loudly rather than fall back to a local copy when resolution fails. A directory argument is how a training run silently fits on the test set eight months later, after the directory has been repopulated by someone who was not on the project when the split was frozen.

---

### 17.7 Training runs

#### 17.7.1 Where the orchestrator runs, stated as constraints

This specification names no orchestration product. Chapter 15's register already adopts a batch workflow engine for the offline evidence-plane path and forbids one on the serving path (`MOS-REL-036`, and the register row for **Workflow**); a gate that names a product is not a gate (`MOS-REL-008`). The constraints below are what any candidate must satisfy.

**MOS-TRAIN-121** The orchestrator MUST satisfy all four of:

| # | Constraint | Why |
|---|---|---|
| C1 | A separate deployable, a separate service account, a separate network namespace from every serving component | A training outage must not be able to become a clinical outage |
| C2 | MUST NOT appear between a `Job` and its `Result`, and MUST NOT be reachable from the serving path | `MOS-REL-036`; the serving step executor is Chapter 5's, not this one |
| C3 | MUST NOT hold `job.create`, `deployment.create`, `deployment.promote`, `deployment.gate.override`, `artifact.approve`, or `evidence.report.issue` | The ruling of 17.10, expressed as a grant |
| C4 | MUST NOT be a dependency of any serving component: a total outage of the orchestrator MUST NOT change the outcome of any `Job` | A pipeline that can break production is not off the production path |

**MOS-TRAIN-122** The orchestrator MUST sit behind a port so that a site already running one is not required to run a second:

```go
// Package training. One shipped driver; a test fake satisfies MOS-REL-048.
type Orchestrator interface {
    Submit(ctx context.Context, spec TrainingRunSpec) (RunID, error)
    Poll(ctx context.Context, id RunID) (RunState, error)
    Cancel(ctx context.Context, id RunID) error
    Logs(ctx context.Context, id RunID, since time.Time) (io.ReadCloser, error)
}
```

The port MUST have a row in the build-versus-adopt register (`MOS-REL-027`) naming the incumbents considered.

**MOS-TRAIN-123** Training GPUs MUST be a pool disjoint from the serving pool, **or** the orchestrator MUST refuse to schedule while the shared pool carries any `clinical_use_mode: clinical` deployment. The orchestrator MUST NOT be able to request a reservation from `medicalos-tritond` or to cause an eviction under Chapter 13's residency contract (§13.10.3, `MOS-OPS-084`). A training job that evicts a resident engine converts a research activity into a clinical latency incident, and the incident presents as a serving problem with no serving cause.

#### 17.7.2 The `TrainingRun` record

**MOS-TRAIN-124** `TrainingRun` is an entity of this chapter. It is the training-side analogue of `EvaluationRun` and it exists for the same reason: it MUST bind every input that can change the artifact. A run missing any field below MUST NOT reach `state = SUCCEEDED`, and its output MUST NOT be registrable as a `ModelVersion`.

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `tr_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `capability_id` | `NOT NULL` | the clinical function being trained for |
| `dataset_version_id`, `dataset_version_digest` | `NOT NULL` | the cohort |
| `split_id`, `split_digest` | `NOT NULL` | the frozen split |
| `fit_partition`, `select_partition` | `NOT NULL`, default `'train'` / `'tune'` | which partitions the run may read |
| `annotation_set_id`, `annotation_digest` | `NOT NULL` | the reference standard (`MOS-EVID-040`) |
| `preprocessing_spec_id`, `preprocessing_spec_version`, `preprocessing_spec_digest` | `NOT NULL` | Chapter 4 (`MOS-IMG-045`) |
| `code_commit` | 40-hex, `NOT NULL` | training repository revision |
| `code_dirty` | `boolean NOT NULL` | working tree state at submit |
| `image_digest` | `sha256:…`, `NOT NULL` | OCI digest of the training container |
| `training_backend` | `jsonb NOT NULL` | `{"kind":"monai_supervised"\|"nnunet","version":"1.5.0","plan_digest":null}` |
| `hyperparameters`, `hyperparameters_digest` | `NOT NULL` | the full set, digested under JCS (`MOS-EVID-008`) |
| `search_id`, `trial_index`, `nominated` | nullable FK / nullable int / `boolean NOT NULL DEFAULT false` | set when the run is a trial of a `ConfigurationSearch` (`MOS-TRAIN-218`, `MOS-TRAIN-219`) |
| `search_trial_score` | `double precision`, nullable | the selection statistic this trial was ranked by; never a reported metric (`MOS-TRAIN-222`) |
| `backend_rationale` | `text`, non-null when `training_backend.kind = 'monai_supervised'` for a `label` capability | `MOS-TRAIN-211` |
| `seeds` | `jsonb NOT NULL` | `{"python":20260311,"numpy":20260311,"torch":20260311,"dataloader_worker_base":900}` |
| `determinism` | `jsonb NOT NULL` | `{"torch_use_deterministic_algorithms":true,"cudnn_benchmark":false,"cublas_workspace_config":":4096:8","tf32_allowed":false}` |
| `hardware` | `jsonb NOT NULL` | `{"gpu_model":"NVIDIA A100-SXM4-80GB","gpu_count":2,"driver":"550.54.15","cuda":"12.4","cudnn":"9.1.0","nccl":"2.21.5"}` |
| `framework_versions` | `jsonb NOT NULL` | `{"torch":"2.4.1","monai":"1.4.0","numpy":"1.26.4","simpleitk":"2.3.1"}` |
| `started_at`, `finished_at`, `runner` | `NOT NULL` on terminal | provenance |
| `state` | ∈ `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` | run lifecycle |
| `bundle_digest` | `sha256:…`, non-null on `SUCCEEDED` | the produced MONAI Bundle |
| `candidate_model_version_id` | nullable FK | set by `MOS-TRAIN-137` |
| `run_digest` | `NOT NULL`, `UNIQUE` | digest over the full binding above |

**MOS-TRAIN-125** `code_dirty = true` MUST block `state = SUCCEEDED`, by the same argument Chapter 7 makes for evaluation (`MOS-EVID-062`): a run from an uncommitted tree cannot be re-entered, and a candidate that cannot be re-entered cannot be diagnosed when it later fails in a subgroup nobody looked at.

**MOS-TRAIN-126** Bit-exact reproducibility of a training run MUST NOT be required and MUST NOT be claimed. cuDNN kernel selection, atomics in scatter reductions, NCCL reduction order and dataloader worker interleaving make a bit-identical multi-GPU re-run unavailable in practice, and forcing every deterministic flag costs throughput while still not covering every kernel. What the record above guarantees is weaker and sufficient: every input is pinned, the determinism settings actually used are recorded rather than asserted, and a re-run is a *comparable* run rather than an identical one.

**MOS-TRAIN-127** Each capability MUST have a **seed-variance characterisation** recorded before its first candidate is promoted: at least three `TrainingRun`s differing only in `seeds`, each evaluated on the same `test` partition, with the observed standard deviation of the primary metric persisted on the Capability as `seed_variance = {"metric":"dice_mean_per_case","runs":3,"sd":0.011}`. It MUST be refreshed whenever `training_backend`, `hardware.gpu_count` or the hyperparameter set changes materially.

**MOS-TRAIN-128** `seed_variance.sd` MUST be rendered beside the declared non-inferiority margin δ in the approval dossier (`MOS-TRAIN-175`, item 9). It MUST NOT be used to compute, adjust or justify δ: δ is a clinical judgement owned by the Capability's `AcceptanceCriteria` and Chapter 7 forbids tuning it to make a candidate pass (`MOS-EVID-087`). The dossier shows both so that an approver can see, without doing arithmetic, whether the gate they are about to rely on can distinguish a real change from a re-run of the same code.

#### 17.7.3 The output is a MONAI Bundle

**MOS-TRAIN-129** A `SUCCEEDED` run's output artifact MUST be a MONAI Bundle. The bundle is the source form; the registered `ModelVersion` is the signed OCI artifact of `MOS-REG-084`, and the mapping between them MUST be exactly:

| `model_version` OCI layer (`MOS-REG-084`) | Bundle path |
|---|---|
| weights blob (`spec.weights`) | `models/model.ts` or `models/model.onnx` — one file, one digest |
| `PreprocessingSpec` blob | `configs/preprocessing.json`, byte-identical to the registered `preprocessing_spec` artifact (`MOS-REG-086`) |
| golden-fixture volume blob | `docs/golden_fixture.nii.gz` (`MOS-IMG-053`) |
| golden-fixture expected-tensor blob | `docs/golden_fixture_tensor.f32` |
| bundle metadata, carried into `artifact.manifest.spec.bundle` | `configs/metadata.json` |

```
effusion_unet_3_4_0/
  configs/
    metadata.json
    preprocessing.json          # the registered PreprocessingSpec, verbatim
    inference.json              # generated, MOS-TRAIN-131
    train.json
  models/
    model.pt                    # training checkpoint, NOT the served artifact
    model.ts                    # TorchScript export, the served artifact
  docs/
    README.md
    licence.txt
    golden_fixture.nii.gz
    golden_fixture_tensor.f32
```

**MOS-TRAIN-130** `configs/metadata.json` MUST declare `version`, `monai_version`, `pytorch_version`, `numpy_version` and a complete `network_data_format` with `spatial_shape`, `dtype` and `channel_def` for every input and output. The registry MUST reject a bundle whose `network_data_format` disagrees with `ModelVersion.spec.io` (`MOS-REG-033`, `MOS-REG-037`). Two declarations of the tensor contract that are allowed to disagree are worse than one, because the mirrored segmentation that results passes every structural check (`MOS-IMG-037` rationale, `MOS-SVC-018`).

#### 17.7.4 One transform chain, generated from the spec

The train/serve skew hazard is not closed by writing the same transforms twice carefully. It is closed by there being one chain, generated from one versioned document, and by a test that proves the generated chain and the serving implementation agree byte for byte.

**MOS-TRAIN-131** The MONAI transform chain in `configs/inference.json` MUST be **generated** from the registered `PreprocessingSpec` by a single generator in `medicalos-preprocessing`. It MUST NOT be hand-written beside the spec, and it MUST be byte-reproducible from the spec alone. The generated form for the 0.1.0 pleural-effusion spec (`MOS-IMG-052`):

```json
{
  "preprocessing": {
    "_target_": "Compose",
    "transforms": [
      {"_target_": "LoadImaged", "keys": ["image"], "image_only": false,
       "ensure_channel_first": true},
      {"_target_": "Orientationd", "keys": ["image"], "axcodes": "LPS"},
      {"_target_": "Spacingd", "keys": ["image"], "pixdim": [1.5, 0.8, 0.8],
       "mode": "bilinear", "align_corners": true},
      {"_target_": "ScaleIntensityRanged", "keys": ["image"],
       "a_min": -1000.0, "a_max": 400.0, "b_min": -1.0, "b_max": 1.0, "clip": true},
      {"_target_": "CropForegroundd", "keys": ["image"], "source_key": "image",
       "select_fn": "$lambda x: x > -0.7", "margin": [8, 8, 8], "allow_smaller": false}
    ]
  },
  "inferer": {
    "_target_": "SlidingWindowInferer",
    "roi_size": [128, 192, 192],
    "sw_batch_size": 1,
    "overlap": 0.5,
    "mode": "gaussian",
    "sigma_scale": 0.125,
    "padding_mode": "constant",
    "cval": -1.0
  }
}
```

**MOS-TRAIN-132** The generator MUST refuse to emit a chain for any `PreprocessingSpec` field it cannot represent exactly, and MUST NOT substitute the nearest available option. A spec declaring `image_interpolator: bspline3` against a transform whose `mode` set offers only `nearest`, `bilinear` and `trilinear` MUST fail generation with the field named. Silent substitution produces a training chain that differs from `medicalos-preprocessing` — which is what the serving worker actually runs (`MOS-IMG-048`) — and the first symptom is an unexplained gap between evaluation-cohort and production performance that nobody can localise, because both sides are individually self-consistent.

**MOS-TRAIN-133** For every registered `PreprocessingSpec` version, CI MUST assert that the generated MONAI chain and `medicalos-preprocessing` produce the **byte-identical** model-space tensor on the golden fixture, compared by the `sha256` of `MOS-IMG-054` step 4 and **not** by a tolerance (`MOS-IMG-056`). This is the check that turns "one chain, versioned once, used by training and serving alike" from an intention into a fact; without it, adopting a transform library buys convenience and re-opens exactly the hazard it was adopted to close.

**MOS-TRAIN-134** `golden_fixture.output_tensor_sha256` MUST be recorded by `medicalos-preprocessing` and by nothing else (`MOS-IMG-058`), even when `MOS-TRAIN-133` passes. The serving self-test compares the worker's computation against this value; if the value were recorded by the training chain, the self-test would be comparing the serving implementation against a number the serving implementation did not produce, and a co-drift of both would be invisible.

#### 17.7.5 nnU-Net as a training backend

**MOS-TRAIN-135** `training_backend.kind = "nnunet"` is permitted. Its self-configured plan MUST be frozen at run start and recorded as `training_backend.plan_digest`. A run whose plan was derived from any cohort other than the `fit_partition` of the pinned split MUST be refused: plan derivation reads spacing, intensity and foreground statistics, and deriving them over `train ∪ tune ∪ test` is a test-set read performed by a configuration step.

**MOS-TRAIN-136** The derived plan MUST be transcribed into a `PreprocessingSpec` (`MOS-IMG-049`) and that spec MUST be the registered one. nnU-Net's own preprocessing MUST NOT be invoked at serving time. The plan sets target spacing, normalisation scheme and its statistics source, patch size, and the foreground crop rule — precisely the fields `PreprocessingSpec` declares. Leaving them inside a plan file means the served pipeline is configured by a document the registry does not index, the signature does not cover, the provenance record does not pin, and the golden-fixture self-test cannot see.

**MOS-TRAIN-137** An nnU-Net ensemble — multiple folds, or a `3d_lowres` → `3d_fullres` cascade — MUST be exported as a single artifact with one content digest, or MUST NOT be registered. A `ModelVersion` that resolves at serving time to "whichever fold directories are present" has no content digest, cannot be loaded by Triton as one named model (`MOS-OPS-068`), and cannot be the subject of an `EvaluationRun`. Cross-validation folds MUST be expressed as the `fold` member of the split manifest line (`MOS-EVID-030`) within the `train` partition and MUST NOT be implemented as a re-partition that touches `tune` or `test`.

#### 17.7.6 Automated configuration search

Two different things travel under the word "AutoML", and they carry different risks.

The first is **dataset-fingerprint auto-configuration**: read a fingerprint of the sealed cohort — spacing distribution, shape distribution, foreground intensity percentiles, modality, class balance — and *derive* a preprocessing and training configuration from published heuristics. It is deterministic, it runs once, and no trained model is in the loop. nnU-Net's planner and MONAI `Auto3DSeg`'s `DataAnalyzer` both do this. 17.7.5 already permits it for nnU-Net; this section makes it the default and fixes what has to be frozen afterwards.

The second is **search**: train N configurations, score them, keep the best. `Auto3DSeg`'s `AutoRunner` does this across algorithm templates and then ensembles the survivors. Search is permitted here, bounded, and carrying an obligation the first thing does not carry, because a configuration chosen *because it scored best on a partition* has been chosen partly because of that partition's noise. Its score on that partition is therefore an overstatement of what it will do anywhere else, and the overstatement is invisible in every artifact the search produces.

**MOS-TRAIN-210** The rows below MUST be appended to the `MOS-REL-027` register table of `MOS-TRAIN-019` in `docs/adr/BUILD_VS_ADOPT.md`. They are normative in the same sense as the rows already there.

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **Dataset-fingerprint auto-configuration** — the nnU-Net planner; MONAI `Auto3DSeg` `DataAnalyzer` + `BundleGen` | Apache-2.0 | **ADOPT as the default training backend** for segmentation capabilities | The fingerprint computation and the published derivation rules for target spacing, intensity window, normalisation scheme, foreground crop and patch size | An exporter that transcribes the derived configuration into a `PreprocessingSpec` (`MOS-TRAIN-223`) and freezes it | The freeze; the refusal to re-derive at serving time (`MOS-TRAIN-225`); the fingerprint digest on the run | For 3D medical segmentation the auto-configured baseline reliably beats hand-tuning by a team that is not doing this full time, and the 0.1–0.4 corpus is segmentation-heavy. Hand-tuning here means spending months to arrive below a published default. |
| **`Auto3DSeg` `AutoRunner`** — multi-algorithm search and ensembling | Apache-2.0 | **PERMIT, bounded** | The algorithm templates, the trial loop and the ensembler as a *producer of candidates* | A `ConfigurationSearch` record (`MOS-TRAIN-218`); the nomination step (`MOS-TRAIN-216`); the single-artifact ensemble export (`MOS-TRAIN-227`) | The selection provenance, the budget bound, and the prohibition on reading `test` | It is the cheapest way to find out whether a second architecture family helps on a given capability. What it does not do is produce an unbiased estimate of the winner, which is why everything in this section attaches to it rather than to the planner. |
| **Neural architecture search** — `DiNTS` and equivalents | Apache-2.0 | **NOT ADOPTED as a default** | nothing by default | — | — | The search phase costs GPU-weeks per capability, and for 3D segmentation on cohorts of a few hundred patients the published margin over an auto-configured U-Net is small, inconsistent across datasets, and of the same order as the seed variance `MOS-TRAIN-127` already requires us to measure. Paying GPU-weeks for a difference we cannot distinguish from a re-run is not a build-versus-adopt decision, it is an arithmetic one. |
| **Generic large-scale hyperparameter search** — Bayesian, population-based or bandit optimisation over a wide space, tens to hundreds of trials | n/a | **NOT ADOPTED as a default** | nothing by default | — | — | The auto-configured defaults already sit near the optimum for learning rate, schedule and augmentation on this class of problem; the marginal metric gain is small while the selection bias of `MOS-TRAIN-213` grows with the number of trials. A wide search buys a small real improvement and a large imaginary one, and only the imaginary one is visible in the report. |

**MOS-TRAIN-211** For a capability whose `io.output_kind` is `label` (`MOS-IMG-049`), the default `training_backend.kind` MUST be a dataset-fingerprint auto-configuring backend — `nnunet` or `auto3dseg`. A hand-configured `monai_supervised` run for such a capability MAY be submitted and MUST record a `backend_rationale` of at least 20 characters on the `TrainingRun` naming what the auto-configured baseline failed to do. The rationale is not a gate; it exists so that the first question at the approval gate is answerable: *was this compared against the default, or did nobody run the default?*

**MOS-TRAIN-212** Neural architecture search and generic hyperparameter search MUST NOT be enabled by default in any shipped pipeline configuration, MUST NOT be reachable from a one-argument entry point, and MUST NOT be presented in the UI as a recommended path. Either MAY be run as a `ConfigurationSearch` under the whole of this section — the budget bound of `MOS-TRAIN-234`, the provenance of `MOS-TRAIN-218`, the partition rule of `MOS-TRAIN-214` and the single nomination of `MOS-TRAIN-216` — and MUST additionally record on the search a declared expected margin and the incumbent auto-configured baseline it is being run against. A search with no baseline to beat has no stopping rule other than the budget.

**The hazard this section exists for.** A search that trains N configurations and keeps the one with the best score on partition P has performed a maximum over N noisy estimates of the same underlying quantity, and the maximum of N noisy estimates is biased upward. The arithmetic is unforgiving and does not require an adversary. Take a segmentation capability with a per-case Dice standard deviation of 0.10 — ordinary for pleural effusion or lobe segmentation — scored on a `tune` partition of 80 patients. The standard error of the mean is 0.10 / √80 ≈ 0.011. The expected maximum of 30 independent draws sits roughly 1.9 standard errors above the common mean, so a search over 30 configurations that were *all genuinely equal* is expected to report a winner about 0.021 Dice above the truth. Two Dice points, manufactured entirely out of noise, in the number everybody will quote.

That number does not reproduce. It is a property of the 80 patients that chose the winner, not of the model. The first place it fails to reproduce is the second site, where it presents as an unexplained two-point drop with no cause anywhere in the deployment: the image digest matches, the golden fixture matches, the envelope fits, the conversion equivalence passes. Nothing is broken. The vendor figure was never real. This is worse than a defect, because there is no defect to find and the site's only remaining hypothesis is that its own data are wrong.

**MOS-TRAIN-213** A **`ConfigurationSearch`** is any procedure that produces more than one trained artifact from one cohort and retains a subset of them by a score computed on data. This includes an `AutoRunner` sweep, an nnU-Net configuration comparison across `2d` / `3d_fullres` / `3d_lowres` / cascade, a learning-rate or augmentation sweep, a checkpoint-epoch selection across more than one candidate epoch, and an ensemble-membership choice. It does not include a single run, and it does not include fingerprint derivation, which reads statistics rather than scores. Any procedure meeting this definition MUST be recorded as a `ConfigurationSearch` (`MOS-TRAIN-218`), whether or not the tool that performed it calls itself AutoML.

**MOS-TRAIN-214** A `ConfigurationSearch` and every process it spawns MUST be confined to the `train` and `tune` partitions of the frozen split. Chapter 7's partition vocabulary already supplies the partition a search may never read: it is `test` (`MOS-EVID-030`, 7.4.1), and this specification adds **no fourth partition** — `val` remains not a permitted value and a "search validation" partition MUST NOT be introduced. Enforcement is structural in two places: `ConfigurationSearch.read_partitions` is `text[] NOT NULL` with `CHECK (read_partitions <@ ARRAY['train','tune'])`, so `test` is not expressible in the record; and the cohort resolver MUST return **403, not an empty set**, on a `test` request from the search principal, extending `MOS-TRAIN-141` to the search driver, each trial process, the ranking step, the ensemble builder and any interactive session attached to the search's workspace.

**MOS-TRAIN-215** The selection metric MUST be computed either on the `tune` partition, or on cross-validation folds *within* `train` expressed as the `fold` member of the split manifest line (`MOS-EVID-030`); which one was used MUST be recorded as `selection_partition` and, for the fold case, `selection_folds`. The `AcceptanceCriteria` of the Capability (`MOS-EVID-076`) MUST be evaluated only against a partition that no search, no tuning step and no model selection ever touched. Concretely, that is two distinct cohorts and both are already walled off: the candidate `EvaluationRun` of `MOS-TRAIN-140` reads the `test` partition, which `MOS-TRAIN-214` makes unreachable from the search; and the deployment gate reads a `DatasetVersion` whose `Dataset.purpose` is `acceptance` (`MOS-EVID-026`, `MOS-EVID-082`), which the pipeline's service account cannot read at all (`MOS-TRAIN-207`). A search that has read either of them has not produced a candidate; it has produced a number about itself.

**MOS-TRAIN-216** A `ConfigurationSearch` MUST nominate **exactly one** trial. Only the nominated trial MAY be registered as a `ModelVersion` under `MOS-TRAIN-138` and only the nominated trial MAY be the subject of a candidate `EvaluationRun` on `test`. Evaluating all N trials on `test` and keeping the best is the same defect with more GPU hours and a cleaner-looking audit trail. The platform MUST maintain, per `(capability_id, split_digest)`, a `test_exposure_count` — the number of distinct `SUCCEEDED` `EvaluationRun`s on that split's `test` partition — MUST increment it on every such run, MUST render it in the approval dossier, and MUST NOT allow it to be reset. A split's `test` partition is a consumable; the counter is how a team finds out it has been spent.

**MOS-TRAIN-217** A trial that was not nominated MAY be nominated later, and doing so MUST be an explicit recorded act: a new `ConfigurationSearch` row referencing the original by `parent_search_id`, naming the new nomination, carrying a rationale, and incurring a fresh increment of `test_exposure_count`. The non-nominated trials' checkpoints MUST be retained with their digests recorded for the tenant's artifact retention period, so that a re-nomination is auditable rather than a re-run. A silent second nomination off the back of a first `test` run is selection on `test` performed one candidate at a time.

**MOS-TRAIN-218** Each search MUST produce a `ConfigurationSearch` record. Chapter 12's conventions own the physical form.

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `cs_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `capability_id` | `NOT NULL` | the clinical function searched for |
| `parent_search_id` | nullable FK | set by `MOS-TRAIN-217` |
| `dataset_version_id`, `split_id`, `split_digest`, `annotation_digest` | `NOT NULL` | the cohort every trial is bound to; all trials share it |
| `backend` | `jsonb NOT NULL` | `{"kind":"auto3dseg","version":"1.4.0","runner":"AutoRunner"}` |
| `fingerprint_digest` | `NOT NULL` | digest of the derived fingerprint document (`MOS-TRAIN-223`) |
| `space`, `space_digest` | `NOT NULL` | the declarative space document and its JCS digest (`MOS-EVID-008`) |
| `strategy` | ∈ `grid`, `random`, `adaptive` | how points are drawn |
| `seed` | `bigint NOT NULL` | the search seed, distinct from `TrainingRun.seeds` |
| `read_partitions` | `text[] NOT NULL`, `CHECK (read_partitions <@ ARRAY['train','tune'])` | `MOS-TRAIN-214` |
| `selection_metric` | `NOT NULL` | a metric registry id (`MOS-EVID-070`) |
| `selection_partition` | ∈ `tune`, `train` | `MOS-TRAIN-215` |
| `selection_folds` | `int[]`; `CHECK ((selection_partition = 'train') = (selection_folds IS NOT NULL))` | the `fold` values used |
| `selection_rule` | `text NOT NULL` | the rule in words, including the tie-break |
| `trials_planned`, `trials_completed`, `trials_failed` | `int NOT NULL` | volume |
| `nominated_training_run_id`, `runner_up_training_run_id` | FK, non-null on `SUCCEEDED` | `MOS-TRAIN-216` |
| `selection_margin` | `double precision` | nominated minus runner-up on `selection_metric` |
| `budget` | `jsonb NOT NULL` | `MOS-TRAIN-234` |
| `cost` | `jsonb NOT NULL` | `MOS-TRAIN-236` |
| `code_commit`, `image_digest` | `NOT NULL` | the search driver |
| `state` | ∈ `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED` | lifecycle |
| `search_digest` | `NOT NULL`, `UNIQUE` | digest over the binding above |

```yaml
# ConfigurationSearch — the recorded form of a completed Auto3DSeg sweep.
id: cs_01JRB4K2Z7N9X3PQD5TWM8CVFA
capability_id: lung_lobes
parent_search_id: null
dataset_version_id: dsv_01JAY7N4K2ZP8QVCM3RXTD6WEB
split_digest: "sha256:c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e47f90b5d3a18c6f2e04b9d7a1"
annotation_digest: "sha256:9d7a1c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e47f90b5d3a18c6f2e04b"
backend: {kind: auto3dseg, version: "1.4.0", runner: AutoRunner}
fingerprint_digest: "sha256:2b7f90c4e16a83d5f027b9c4e81d3a06f5c29e74b0d8a3162f4e7c905bd13a68"
space_digest: "sha256:9e41c05b7d2a63f8104e9b3c57d8206af41c93e5b0d7a284f6c1e390b5d72a4c"
strategy: grid
seed: 20260311
read_partitions: [train, tune]
selection_metric: dice_mean_per_case
selection_partition: tune
selection_folds: null
selection_rule: >-
  argmax over completed trials of dice_mean_per_case on the tune partition at
  operating point 0.50; ties broken by lowest trial_index
trials_planned: 12
trials_completed: 12
trials_failed: 0
nominated_training_run_id: tr_01JRB6Q8V4M2Y7KTC3ZPN5HDEA
runner_up_training_run_id: tr_01JRB6R1W5N3Z8LUD4AQP6JEFB
selection_margin: 0.006
budget:
  max_trials: 12
  max_gpu_hours: 96.0
  max_wall_clock_hours: 72.0
  max_ensemble_members: 5
  max_footprint_bytes: 5368709120
cost:
  gpu_hours_used: 81.4
  wall_clock_hours: 26.2
  stop_reason: space_exhausted    # space_exhausted | max_trials | max_gpu_hours |
                                  # max_wall_clock_hours | cancelled | failed
state: SUCCEEDED
```

**MOS-TRAIN-219** Every trial MUST be an ordinary `TrainingRun` carrying the complete binding of `MOS-TRAIN-124`, with `search_id`, `trial_index` and `nominated` set. A search MUST NOT introduce a second, lighter run record for trials. Two consequences follow and both are wanted: a trial that would not satisfy `MOS-TRAIN-125`'s `code_dirty = false` cannot be a trial, and the nominated candidate needs no promotion from a trial record to a real one, because it already was one. `CREATE UNIQUE INDEX training_runs_one_nomination ON training_runs (search_id) WHERE nominated;` MUST exist, so that `MOS-TRAIN-216` is a database property rather than a convention in the driver.

**MOS-TRAIN-220** The search space MUST be a declarative document with a fixed grammar, digested under JCS (`MOS-EVID-008`). It MUST NOT be a Python callable, an expression string, a lambda or a template evaluated at search time, for the reason `MOS-EVID-076` gives for `AcceptanceCriteria`: a space that is code has no digest that means anything. A field named in `derived_from_fingerprint` MUST NOT also appear as a search axis, and registration of a space violating that MUST be refused — a quantity cannot be both derived from the cohort and searched over, and a space that does both silently overwrites the frozen value with the searched one.

```yaml
# search space — declarative, digested, never evaluated as code.
schema_version: "1.0"
id: space.pulmo.lung-lobes.auto3dseg
strategy: grid
axes:
  - {name: algorithm,     values: [segresnet, swinunetr, segresnet2d]}
  - {name: learning_rate, values: [0.0002, 0.0005]}
  - {name: num_epochs,    values: [600]}
  - {name: patch_size,    values: [[96, 192, 192], [128, 192, 192]]}
constraints:
  - {when: {algorithm: segresnet2d}, forbid: {patch_size: [128, 192, 192]}}
derived_from_fingerprint:          # frozen by MOS-TRAIN-223; MUST NOT appear above
  - target_spacing_mm
  - clip
  - normalisation
  - foreground_crop
```

**MOS-TRAIN-221** A `ValidationReport` whose headline metric was computed on a partition that any selection was performed on MUST disclose that in the report document itself. Chapter 7 owns the report; the addition required there is the `evaluation_run.selection` member and the extension of `MOS-EVID-033` to cover automated search, and it MUST carry, when the candidate came from a `ConfigurationSearch`: `search_id`, `trials_completed`, `space_digest`, `selection_metric`, `selection_partition`, `selection_rule`, `selection_margin`, and `headline_metric_selected_on` — a boolean stating whether the partition the headline figure was computed on is the partition selection used. Under `MOS-TRAIN-214` that boolean is `false` for every conformant pipeline run, and that is exactly why it must be present and computed rather than assumed: an undisclosed selected metric is byte-for-byte indistinguishable from a held-out one, and a field that is always `false` when things are right is the only kind of field that can be `true` when they are not.

**MOS-TRAIN-222** A cross-validation summary metric — nnU-Net's `summary.json` fold-aggregated Dice, `AutoRunner`'s per-algorithm validation score, or any figure the search itself computed to rank trials — MUST NOT be written into `evaluation_case_metrics`, `evaluation_runs.aggregate_metrics`, `capability_claims`, a `ValidationReport` aggregate, a `ModelVersion` field, or any UI surface that presents model performance. It is a selection statistic, it was computed on data the model was fitted against under the search's own conventions rather than the `metric_conventions` of `MOS-EVID-061`, and `MOS-EVID-071` already forbids the shape it wants to take. It MAY be stored on the `TrainingRun` as `search_trial_score` and MAY be rendered inside the dossier's search block, labelled as a selection statistic.

**Freezing what the fingerprint derived.** Auto-configuration's whole value is that it reads the cohort and picks constants. That is also its whole hazard, because the code that picks them is the same code at training time and at serving time, and only the data differ. A re-derivation at serve time against a different cohort produces a different target spacing, a different intensity window and different normalisation statistics — a materially different transform, executed by a byte-identical container, against a byte-identical weights digest, with every structural check passing. This is train/serve skew in its most deceptive form: there is no version to compare, no digest that differs, and no error anywhere. The model receives inputs it was never trained on and returns plausible masks.

**MOS-TRAIN-223** The derived configuration MUST be transcribed into a `PreprocessingSpec` (`MOS-IMG-049`) by a deterministic exporter at training time, and that spec MUST be the registered one, pinned to the `ModelVersion` through `spec.preprocessing_spec_ref` (`MOS-REG-030`). The fingerprint document itself — `plans.json` for nnU-Net, `datastats.yaml` plus the per-algorithm `hyper_parameters.yaml` for `Auto3DSeg` — MUST be digested as `fingerprint_digest` and retained for audit, and MUST NOT be shipped inside the `ModelVersion` artifact as an executable configuration (`MOS-TRAIN-030`, `MOS-TRAIN-136`). For the backend versions pinned in `training_backend.version`, the mapping is fixed:

| Derived quantity | nnU-Net `plans.json` | `Auto3DSeg` | `PreprocessingSpec` field (`MOS-IMG-049`) |
|---|---|---|---|
| Target spacing | `configurations.3d_fullres.spacing` | `hyper_parameters.yaml: resample_resolution`, from `datastats.yaml: stats_summary.image_stats.spacing.median` | `target_spacing_mm` |
| Axis permutation | `transpose_forward` | the analyzer's canonical axis order | composed into `axis_order` and `orientation_target`; never carried as a separate runtime step |
| Intensity window | `foreground_intensity_properties_per_channel.0.percentile_00_5` / `.percentile_99_5` | `hyper_parameters.yaml: intensity_bounds`, from `stats_summary.image_foreground_stats.intensity.percentile_00_5` / `.percentile_99_5` | `clip.min_hu` / `clip.max_hu` |
| Normalisation scheme | `configurations.3d_fullres.normalization_schemes` (`CTNormalization`) | `hyper_parameters.yaml: normalize_mode` | `normalisation.scheme: zscore_dataset` with `normalisation.statistics_source: spec` |
| Dataset statistics | `foreground_intensity_properties_per_channel.0.mean` / `.std` | `stats_summary.image_foreground_stats.intensity.mean` / `.stdev` | `normalisation.mean` / `normalisation.std`, computed on `train` only (`MOS-TRAIN-046`) |
| Foreground crop | crop-to-nonzero; `configurations.3d_fullres.use_mask_for_norm` | `hyper_parameters.yaml: crop_mode` | `foreground_crop.mode`, `.threshold_hu`, `.margin_mm`, `.min_size_voxels` |
| Patch size | `configurations.3d_fullres.patch_size` | `hyper_parameters.yaml: patch_size` | `patch.size_voxels` |
| Training batch size | `configurations.3d_fullres.batch_size` | `hyper_parameters.yaml: num_patches_per_iter` | **not** a `PreprocessingSpec` field — see `MOS-TRAIN-224` |

The exporter MUST refuse to emit for any derived quantity it cannot map exactly and MUST NOT substitute the nearest available value, by the same argument `MOS-TRAIN-132` makes for the transform-chain generator. A backend version bump that changes a fingerprint field name MUST fail the export loudly rather than silently drop the field to a schema default.

**MOS-TRAIN-224** The derived *training* batch size MUST be recorded in `TrainingRun.hyperparameters` and MUST NOT be written into `PreprocessingSpec.patch.batch_size`. They are different quantities with the same English name: the first is how many patches a gradient step consumed and is a property of the run, the second is the sliding-window batch of `MOS-IMG-049`, is constrained by `allowed_patch_batch_sizes` (`MOS-OPS-078`), and is an engineering knob that `MOS-OPS-088` expects an operator to change at 2 a.m. to fit another model on a GPU. Conflating them makes a serving knob look like a trained property — at which point changing it appears to require revalidation — or makes a trained property look like a knob, which is worse.

**MOS-TRAIN-225** The serving path MUST NOT re-derive any fingerprint quantity. The nnU-Net planner, `nnUNetPlansManager`, `Auto3DSeg`'s `DataAnalyzer` and any equivalent MUST NOT be present in the serving image's import closure, and CI MUST assert their absence by module-name grep over the resolved closure, in the same check that `MOS-TRAIN-136` already requires for nnU-Net preprocessing. `normalisation.statistics_source` MUST be `spec` for every capability trained under an auto-configuring backend, so that `MOS-IMG-050`'s prohibition on a serve-time scheme change is enforceable from the spec alone.

**MOS-TRAIN-226** The golden-fixture startup self-test (`MOS-IMG-054`, `MOS-REG-036`) is what makes the freeze verifiable rather than merely asserted, and `MOS-TRAIN-060` already requires the fixture hash to be recorded as the last step before signing — which, for an auto-configured model, means *after* the derived constants are frozen into the spec. CI MUST additionally run the negative form: delete the fingerprint document from the packaged artifact, re-run the self-test on a clean checkout per `MOS-TRAIN-062`, and assert it still reproduces `golden_fixture.output_tensor_sha256` byte for byte. If the served transform depends on the fingerprint document at all, that test fails; if it passes, the constants are genuinely in the spec and nowhere else.

**Ensembles.** `AutoRunner`'s natural output is not a model. It is N models and a rule for combining them, and the rule is the part that tends to arrive as three lines in a notebook.

**MOS-TRAIN-227** An ensemble MUST be registered as **one** `ModelVersion` with one `spec.weights.digest`, declaring its members and its combination rule explicitly. The declaration lives in the bundle's `configs/metadata.json`, which `MOS-TRAIN-129` already carries into `artifact.manifest.spec.bundle` and which `MOS-REG-084`'s signature therefore already covers; it MUST name each member's trial `training_run_id`, its checkpoint digest, its weight if the rule is weighted, and the rule itself. Registering N `ModelVersion`s plus an averaging step that exists only in the serving runner MUST be refused, and `MOS-TRAIN-137` already states why: an artifact that resolves at serving time to whichever members are present has no content digest, cannot be named by `MOS-OPS-068`, and cannot be the subject of an `EvaluationRun`.

**MOS-TRAIN-228** The combination rule MUST be drawn from this closed vocabulary, MUST be applied in **model space**, after the members' post-activation outputs and **before** the inverse transform of `MOS-IMG-032`, and before any post-processing selected under `MOS-TRAIN-033`.

| Rule | Operates on | Definition | Permitted |
|---|---|---|---|
| `mean_probability` | per-voxel post-activation probabilities | arithmetic mean over members, then threshold at `spec.operating_point.value` | yes; the default |
| `weighted_mean_probability` | per-voxel post-activation probabilities | fixed declared weights, then threshold | yes, and the weights are a selected quantity: they MUST be selected under `MOS-TRAIN-215` and recorded in `selection_rule` |
| `majority_vote` | per-voxel binarised member labels | each member thresholded first, then per-voxel majority; ties resolve to background | yes |
| `staple` or any iterative data-dependent fusion | per-case | estimated per case at inference time | **no** — the served function would differ per study, so no fixed artifact exists to evaluate |

`mean_probability` followed by a threshold and `majority_vote` over pre-thresholded masks are different functions and disagree most on exactly the small, low-contrast findings that the aggregate metric weights least. The artifact declares one of them, and it is the one measured.

**MOS-TRAIN-229** Exactly one `EvaluationRun` MUST measure the ensemble **as served** — all members, the declared combination rule, the registered `PreprocessingSpec`, through the serving stack of `MOS-TRAIN-153`. Averaging N per-member runs, or reporting the best member's run, MUST be refused; `MOS-EVID-032` already forbids an aggregate computed across two bindings and this is that rule applied to members. Per-member `EvaluationRun`s MAY exist for diagnosis, MUST be on the `tune` partition only, MUST NOT be referenced by any `capability_claims` row (`MOS-EVID-072`), and MUST NOT appear in the approval dossier — a member table beside an ensemble figure invites an approver to pick a member, which is a model-selection decision made at the gate on data the gate was not given.

**MOS-TRAIN-230** On Triton, an ensemble `ModelVersion` MUST be served as **one** model directory under **one** `triton_model_name` derived by `MOS-OPS-068` from the ensemble's own `(model_id, version)`, with the constant version directory `1` (`MOS-OPS-069`) and a single serialized graph inside it into which the members and the combination rule are exported. Triton's `platform: "ensemble"` scheduler MUST NOT be used to express the combination rule, and neither MUST a Business Logic Scripting model nor any `python`-backend combiner for a `clinical` deployment (`MOS-TRAIN-155`). Three reasons, each load-bearing: an `ensemble_scheduling` stanza places clinical behaviour in `config.pbtxt`, which `MOS-TRAIN-171` forbids precisely because that file is editable on the node; it splits the served unit across N model directories with N digests, which is the artifact shape `MOS-TRAIN-137` refuses; and it leaves the combination un-named by `MOS-OPS-068`, so two versions of the ensemble cannot be resident at once and canary and blue/green (`MOS-REG-076`, `MOS-REG-077`) stop working for ensembles alone. The generated `config.pbtxt` obligations are unchanged — `max_batch_size: 0`, no dynamic batcher, `instance_group.count: 1` (`MOS-OPS-072`, `MOS-OPS-073`, `MOS-OPS-076`) — and the conversion equivalence checks of `MOS-TRAIN-159`–`MOS-TRAIN-163` apply to the combined graph, not to any member.

**MOS-TRAIN-231** `medicalos.json`'s `weights_bytes` and `workspace_bytes_by_patch_batch` (`MOS-OPS-070`) MUST be **measured on the combined graph**, never summed from member figures. An N-member ensemble costs roughly N times the weights, N times the inference wall time and a workspace that is not additive, and Chapter 13's residency arithmetic (§13.10.3, `MOS-OPS-084`) is what decides whether it can be deployed at all. The search's `budget.max_ensemble_members` and `budget.max_footprint_bytes` MUST therefore be derived from the target deployment's node budget **before the search starts**. A search permitted to nominate a five-member ensemble onto a fleet that can hold two produces a candidate whose only possible deployment outcome is `409 unsatisfiable` (`MOS-OPS-083`) — discovered after the GPU hours are spent, after a report is written, and after a named human has read a dossier for a model that cannot run.

**Candidate volume and the human gate.** A search turns one candidate into N. The approval gate of 17.10 does not scale with N, and it MUST NOT be made to. That asymmetry is not an ergonomics defect to be engineered away; it is the control. Everything else in this chapter is machinery for making one candidate legible to one person, and the only sound response to a queue of forty candidates is for the search to nominate fewer, not for the gate to admit more at a time.

**MOS-TRAIN-232** Promotion decisions MUST remain per-candidate. `artifact.approve` MUST accept exactly one `model_version_id` per call; the API MUST NOT expose a bulk-approval endpoint, a collection-valued approval body, an "approve all passing" control, a saved filter that approves on match, or an automated promotion of the best of a sweep. A request carrying an array of ids MUST be rejected with RFC 9457 `class: schema_violation`, and CI MUST assert that the handler's signature takes a scalar id. The same holds for `evidence.report.issue` and `deployment.approve_clinical`. This restates `MOS-TRAIN-008`, `MOS-TRAIN-174`, `MOS-TRAIN-182` and `MOS-TRAIN-189` at the one shape a search makes genuinely tempting to build, which is the shape where each individual approval is still a human act and nobody read anything.

**MOS-TRAIN-233** A `PASS` verdict on a trial MUST NOT nominate it, and a ranking view MUST NOT carry a promotion control. `MOS-TRAIN-184`'s prohibition on `auto_promote` extends to any policy expressed over a search: "promote the top-ranked candidate", "promote any candidate exceeding the incumbent by δ", and "promote automatically when exactly one candidate passes" are all the forbidden edge of `MOS-TRAIN-005` with a sweep in front of it. CI MUST assert, as part of the call-graph property of `MOS-TRAIN-189`, that no symbol reachable from the search driver reaches the approval or deployment-mutation handlers.

**MOS-TRAIN-234** A search's compute budget MUST be bounded before it starts and MUST be recorded. `budget` MUST declare `max_trials`, `max_gpu_hours`, `max_wall_clock_hours`, `max_ensemble_members` and `max_footprint_bytes`; an absent bound is a registration error, not an unlimited one. The orchestrator MUST stop the search at the first bound reached, MUST record which one in `cost.stop_reason`, and MUST leave a valid `ConfigurationSearch` record either way — a budget-terminated search that nominated a trial is a legitimate candidate, and a budget-terminated search that nominated none is a legitimate outcome that MUST NOT be papered over by relaxing the bound and resuming. `MOS-TRAIN-123` continues to apply unchanged: a search MUST NOT be schedulable onto a pool carrying a `clinical` deployment, and N trials make that N times easier to violate by accident.

**MOS-TRAIN-235** A search MUST be reproducible from its recorded seed and configuration space. For `strategy: grid` or `random`, the sequence of trial configurations MUST be a pure function of `(space_digest, seed, fingerprint_digest)`, and re-running MUST produce the identical sequence of `config_digest` values in the identical order. For `strategy: adaptive`, where trial *k* depends on the scores of trials 1…*k*−1, the sequence MUST be a pure function of `(space_digest, seed, fingerprint_digest, recorded trial scores)` — which is why `search_trial_score` MUST be persisted per trial rather than only for the winner. Bit-exact reproduction of trial *weights* MUST NOT be required and MUST NOT be claimed; `MOS-TRAIN-126` already explains why, and it applies unchanged to every trial. What is reproducible is the trajectory: which configurations were tried, in what order, and which one won.

**MOS-TRAIN-236** `cost.gpu_hours_used`, `cost.wall_clock_hours`, `cost.stop_reason`, `trials_completed` and `trials_failed` MUST be recorded on the search and MUST be rendered in the approval dossier beside `selection_margin`. The pairing is the point: it lets a reader see what the search cost against what it bought, and `MOS-TRAIN-127`'s `seed_variance.sd` is what tells them whether it bought anything at all. A search that spent 81 GPU-hours to produce a winner 0.006 Dice ahead of the runner-up, against a measured seed standard deviation of 0.011, selected noise — and the dossier must make that arithmetic visible without requiring the approver to perform it.

#### 17.7.7 Registering the candidate

**MOS-TRAIN-138** On `state = SUCCEEDED`, the pipeline MUST register the bundle as a new `ModelVersion` at `lifecycle_status = REGISTERED` (`MOS-REG-021`, permission `artifact.publish`), with `spec.derived_from = null`, `spec.preprocessing_spec_ref` naming the registered spec, `spec.golden_fixture` populated per `MOS-TRAIN-134`, `spec.not_validated_for` and `spec.known_failure_modes` non-empty (`MOS-REG-032`), and `spec.evaluation_run_id` null. The `TrainingRun.candidate_model_version_id` MUST be set in the same transaction, so that every candidate has exactly one producing run and every run has at most one candidate.

**MOS-TRAIN-139** Starting the candidate `EvaluationRun` of 17.8 MUST transition the candidate to `VALIDATING` (`MOS-REG-021`, permission `evidence.run`), and its completion MUST transition it to `VALIDATED` or back to `REGISTERED`. `VALIDATING` and `VALIDATED` are the **only** statuses this pipeline can cause. The pipeline's service account MUST hold `artifact.publish` and `evidence.run` and MUST NOT hold `artifact.approve`. This is the architectural ruling of the chapter expressed where it can be tested: as a row in the grant table.

---

### 17.8 Evaluation and the candidate gate

#### 17.8.1 Binding the run

**MOS-TRAIN-140** The candidate `EvaluationRun` MUST be created by the pipeline, MUST carry the complete binding of `MOS-EVID-061`, MUST name `partition: "test"`, and MUST use the same `split_digest` and `annotation_digest` the `TrainingRun` was fitted against. Chapter 7 owns the run; this chapter owns the obligation that the pipeline never creates one whose cohort is not the cohort the candidate was built on.

**MOS-TRAIN-141** The training and selection jobs MUST NOT be able to read the `test` partition at all. The cohort resolver MUST be handed a split view filtered to `{train, tune}` and MUST return **403, not an empty set**, on a `test` request. An empty set is indistinguishable from "no such patient", and a retry loop around an empty set is how a silent read becomes a routine one.

**MOS-TRAIN-142** Operating thresholds, checkpoint selection and any preprocessing variant MUST be selected on `tune` (`MOS-EVID-033`). The pipeline MUST refuse to start the candidate `EvaluationRun` unless every threshold it will report is already recorded from a `tune`-partition run with `selected_on: "tune"`, so that `MOS-EVID-033`'s CI assertion cannot fail at report time on a run that already consumed the GPU.

**MOS-TRAIN-143** The evaluation job MUST write `evaluation_case_metrics` and `evaluation_case_scores` incrementally as cases complete (`MOS-EVID-065`, `MOS-EVID-068`, `MOS-EVID-069`). A job that dies mid-cohort MUST leave the run `FAILED` with the partial rows retained. It MAY be resumed into the same run **only** when `code_commit`, `image_digest`, `inference_backend` and `accelerator` are byte-identical to the aborted attempt, verified at resume; otherwise the remainder MUST be a new run. A resumed run that mixes two code states produces aggregates that are recomputable from the per-case rows (`MOS-EVID-066`) and still describe no artifact that exists.

**MOS-TRAIN-144** The pipeline MUST refuse to start a run whose cohort cannot supply a `strata` key that any blocking criterion of the bound `AcceptanceCriteria` version filters on (`MOS-EVID-067`, `MOS-EVID-079`). Failing in the first second of a two-hour GPU run is materially cheaper than failing at the gate, and the failure mode — a subgroup floor silently reporting `SKIPPED` — is one the gate is not designed to catch.

**MOS-TRAIN-145** The pipeline MUST call Chapter 7's `AcceptanceCriteria` evaluator (`MOS-EVID-076`) and `evaluate_gate()` (`MOS-EVID-090`). It MUST NOT implement a comparison of its own. CI MUST assert that the pipeline package imports the evidence evaluator and contains no arithmetic comparison of two aggregate metric values on any blocking path, which is the pipeline-side half of Chapter 7 acceptance check 18.

#### 17.8.2 The incumbent has to be re-run, and this is the step that gets skipped

**MOS-TRAIN-146** A regression criterion requires the candidate and incumbent runs to share `dataset_version_digest`, `split_digest`, `partition` and `annotation_digest` (`MOS-EVID-086`). The incumbent will almost never have been evaluated on the candidate's split, because the split is new — the cohort grew, or a leakage alias was merged. The pipeline MUST therefore, before the regression criterion is evaluated, **materialise an incumbent `EvaluationRun` on the candidate's split**, executing the incumbent `ModelVersion` exactly as it is deployed: its own `PreprocessingSpec` version, its own `inference_backend`, its own `accelerator`, its own `operating_thresholds`.

**MOS-TRAIN-147** Reusing the incumbent's historical run on its own older split MUST be refused. It returns `INDETERMINATE` with `unpaired_runs` (`MOS-EVID-086`), and a pipeline that treats `INDETERMINATE` as "no incumbent exists" takes the `no_incumbent_first_deployment` branch of `evaluate_gate()` and silently converts every upgrade into a first deployment — at which point the regression test has been removed from the system by a code path that looks like defensive handling. `INDETERMINATE` from `unpaired_runs` MUST abort the pipeline with that reason surfaced, never fall through to `SKIPPED`.

**MOS-TRAIN-148** The incumbent re-run MUST use the **incumbent's** `PreprocessingSpec`, not the candidate's. Two models with different specs remain comparable on the same cases; forcing one spec on both measures a configuration that will never be served and produces a number that means nothing to anyone who later has to defend it.

**MOS-TRAIN-149** The pipeline MUST NOT contain a direct comparison of the candidate's and incumbent's aggregate metric values on any blocking path. The regression rule is Chapter 7's paired non-inferiority test (`MOS-EVID-085`). The one-paragraph reason belongs in the pipeline's own README so that it is read by whoever is tempted to simplify: at zero true effect, `new < old` on a point estimate blocks with probability 0.5, and that probability does not fall with sample size — it is 0.5 at n = 40 and 0.5 at n = 4000. The first harmless event that trips it is a runtime version bump, the second is a driver upgrade, and a gate that blocks half of all harmless rebuilds is switched off within two weeks. After that the project believes it has a regression gate and does not have one, which is worse than never having built it.

#### 17.8.3 The report, and where automation stops

**MOS-TRAIN-150** The pipeline MUST assemble the complete `ValidationReport` payload of Chapter 7 §7.12.1 with `kind: "vendor_evidence"`, canonicalise it under JCS (`MOS-EVID-008`), compute `report_digest`, and produce the offline bundle layout of `MOS-EVID-122`. It MUST verify its own output with `medicalos-verify` (`MOS-EVID-123`) on a network-isolated runner before presenting it, so that an unverifiable report is caught by the machine that made it rather than by the site that received it.

**MOS-TRAIN-151** The pipeline MUST NOT hold a report signing key (`MOS-EVID-119`) and MUST NOT populate `approver` (`MOS-EVID-117`). Issuance — binding the named human approver, producing the DSSE envelope, writing the `validation_reports` row — requires `evidence.report.issue` and is the first of the three human acts of 17.10.1. The automated portion of this chapter therefore terminates at: *a complete, self-verified, unsigned report payload with a computed digest and a rendered approval dossier, and a candidate sitting at `lifecycle_status = VALIDATED`.* Every step from data to that point is machine work and MUST be machine work; nothing past it is.

**MOS-TRAIN-152** A `ValidationReport` presented for issuance whose `approver.identity_assurance` names a service account, an API key, a CI job identity or any non-human principal MUST be rejected at write time (`MOS-EVID-117`). This check MUST live in the write path, not in review: an automated approver is exactly the shape a well-intentioned automation of the last mile takes.

---

### 17.9 Serving conversion

#### 17.9.1 The trap

What was evaluated in 17.8 is a PyTorch module running fp32 under a Python inferer inside a training container. What is served is a serialised graph running under a Triton backend on a specific GPU architecture, possibly at reduced precision, with a different kernel selection, a different reduction order and a different memory layout. These are different numerical artifacts that happen to share a weights file. The registry already rules that a conversion is a new `ModelVersion` with its own `EvaluationRun` (`MOS-REG-102`, `MOS-REG-103`, `MOS-EVID-064`, `MOS-SVC-017`, `MOS-OPS-071`). This section specifies how the pipeline satisfies that, and what the equivalence check between checkpoint and served plan actually measures.

**MOS-TRAIN-153** The converted version's `EvaluationRun` MUST execute inference through the **same serving stack the deployment will use**: the same generated `config.pbtxt` (`MOS-OPS-068`–`MOS-OPS-074`), the same `allowed_patch_batch_sizes` (`MOS-OPS-078`), the same GPU compute capability, and the same `built_for` triple recorded in `medicalos.json` (`MOS-OPS-070`). An `EvaluationRun` executed against the checkpoint inside a training container MUST NOT be attached to a converted `ModelVersion`, whatever its numbers are. **The run must exercise the plan actually served, not the checkpoint that was trained** — this sentence is the whole content of the requirement and is the single thing most often got wrong.

#### 17.9.2 The two Triton paths for a MONAI Bundle

| Path | Triton backend | Served unit | Permitted in | Consequence |
|---|---|---|---|---|
| Bundle → TorchScript | `pytorch_libtorch` | `model.ts` | `research_only`, `clinical` | One serialised graph, one content digest, no Python at serving |
| Bundle → ONNX | `onnxruntime` | `model.onnx` | `research_only`, `clinical` | Portable across GPU architectures: one artifact serves a mixed fleet |
| Bundle → ONNX → TensorRT | `tensorrt_plan` | `model.plan` | `research_only`, `clinical`, **one `ModelVersion` per compute capability** | Lowest latency; pinned to `(compute capability, TensorRT version)` |
| Bundle under the Python backend | `python` | the bundle directory | `research_only` **and** `role: SHADOW` only | Numerics are the checkpoint's, but the served unit is a directory of Python |

**MOS-TRAIN-154** ONNX under `onnxruntime` MUST be the default. TensorRT MUST be adopted per capability only when a measured p95 latency requirement on the `ModelVersion`'s engineering bar (`MOS-EVID-075`) is not met by ONNX Runtime, and both measurements MUST be recorded on the version. The arithmetic that motivates this default is the evidence plane's, not the GPU's: a TensorRT plan is pinned to a compute capability and a TensorRT build (`MOS-OPS-070`), so a fleet with `sm_86` and `sm_89` nodes needs two `ModelVersion`s — and by `MOS-REG-103` and `MOS-EVID-064` that is two `EvaluationRun`s, two incumbent re-runs, two `ValidationReport`s, two approvals and two DICOM result lineages (`MOS-REG-105`) for one trained model. The cost of TensorRT is not engine build time; it is a multiplication of the evidence plane by the number of GPU generations in the fleet, paid again on every retrain.

**MOS-TRAIN-155** The Python backend MUST NOT be used for any deployment with `clinical_use_mode: clinical`. Three reasons, each load-bearing: (a) the served unit is a directory of Python whose effective behaviour depends on the interpreter's installed package set rather than on a weights digest, so the signed unit of `MOS-REG-084` has nothing to cover; (b) `medicalos.json`'s `built_for` has no serialised plan to describe, so the load-time refusal of `MOS-OPS-070` cannot fire and an architecture mismatch surfaces as a wrong answer instead of a load error; (c) it places MONAI and torch inside the serving container, which is what `MOS-REL-041`'s "weights arrive at runtime by id and digest" exists to prevent. It MAY be used for `research_only` and for `role: SHADOW` deployments (`MOS-REG-082`), where it is the cheapest way for a candidate to accumulate local disagreement evidence before its export is done.

#### 17.9.3 The `ConversionRun` record

**MOS-TRAIN-156** Conversion MUST happen in a packaging job inside this pipeline, its output MUST be signed, and Triton MUST NOT build an engine at load time (`MOS-OPS-071`). Each conversion MUST produce a `ConversionRun`:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `cv_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `source_model_version_id` | `NOT NULL` | the `derived_from` of the output (`MOS-REG-103`) |
| `target_format` | ∈ `torchscript`, `onnx`, `tensorrt_plan` | what is produced |
| `precision` | ∈ `fp32`, `tf32`, `fp16`, `int8` | the tolerance class of `MOS-TRAIN-162` |
| `toolchain` | `jsonb NOT NULL` | `{"torch":"2.4.1","onnx":"1.16.2","opset":18,"onnxruntime":"1.19.2","tensorrt":"10.3.0"}` |
| `built_for` | `jsonb`, non-null for `tensorrt_plan` | `{"cuda_compute_capability":"8.6","tensorrt_version":"10.3.0","cuda_version":"12.4"}` |
| `calibration_dataset_version_id`, `calibration_partition` | non-null iff `precision = 'int8'` | `MOS-TRAIN-157` |
| `equivalence_cohort_digest` | `NOT NULL` | `MOS-TRAIN-159` |
| `equivalence` | `jsonb NOT NULL` | the recorded E1/E2/E3 block |
| `code_commit`, `image_digest` | `NOT NULL` | the packaging job |
| `target_model_version_id` | nullable FK | set on success |
| `state` | ∈ `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED` | run lifecycle |

**MOS-TRAIN-157** `int8` calibration MUST draw its calibration cohort from the `tune` partition and MUST record it. Calibrating on `test` is operating-point selection on the test set under another name and is refused by `MOS-EVID-033`.

**MOS-TRAIN-158** ONNX export MUST pin `opset_version`, MUST use a fixed spatial input shape when the `PreprocessingSpec` declares a fixed `patch.size_voxels`, and MUST limit dynamic axes to the leading batch dimension (`MOS-OPS-072`). A graph exported with dynamic spatial axes and served at a fixed patch size is not the graph the equivalence check measured.

#### 17.9.4 The checkpoint-versus-served-plan equivalence check

`MOS-REG-104` requires that a `conversion_equivalence` block be recorded on the converted version and that it MUST NOT substitute for the evaluation run. This section specifies the corpus, the procedure and the tolerances.

**MOS-TRAIN-159** Two corpora, both mandatory:

- **The golden fixture** of `MOS-IMG-053`. One volume. It catches a gross wiring error — transposed axes, a lost normalisation, an output head bound to the wrong tensor — in seconds, before a cohort run is scheduled.
- **An `equivalence_cohort`**: at least 20 distinct `patient_key`s drawn from the `tune` partition of the split the source version was trained on, frozen as a named subset with its own digest, and reused **unchanged** for every conversion of that model family, so that two conversions are comparable to each other and a slow degradation across a series of conversions is visible. The `test` partition MUST NOT be used.

**MOS-TRAIN-160** Both artifacts MUST be executed through the identical `PreprocessingSpec` version, the identical sliding-window patch order, TTA disabled, and `patch.batch_size = 1`. Any difference in these makes the measured difference uninterpretable, because it then contains a preprocessing term the check was not designed to see.

**MOS-TRAIN-161** Three quantities MUST be computed per case, at three points in the pipeline, and all three MUST be recorded in `conversion_equivalence`. `max_abs_logit_diff` MUST also be recorded because `MOS-REG-104` names it, but it MUST NOT be the gating quantity.

| id | Quantity | Computed on | Why this point |
|---|---|---|---|
| `E1` | `max_abs_probability_diff` | post-activation probability map, model space | bounded in [0,1], so a tolerance is interpretable across models; the raw logit scale is model-specific and a fixed logit tolerance means different things for different heads |
| `E2` | `post_threshold_dice` | binarised label map at `spec.operating_point.value`, model space | the quantity the clinical output is actually derived from; a probability difference below the threshold everywhere is harmless and a smaller one straddling it is not |
| `E3` | `volume_rel_diff` | the reported measurement in **source** geometry (`MOS-IMG-040`) | the number that reaches the SR. E1 and E2 can both pass while the inverse transform and the source-grid resampling amplify a boundary-voxel disagreement into a reportable difference |

**MOS-TRAIN-162** Default tolerances by precision class. A `ModelVersion` MAY declare tighter values and MUST NOT declare looser ones.

| Precision class | E1 `max_abs_probability_diff` | E2 `post_threshold_dice`, per case | E3 `volume_rel_diff`, per case |
|---|---|---|---|
| `fp32` — TorchScript, ONNX Runtime fp32 | ≤ 1e-4 | ≥ 0.9995 | ≤ 1e-3 |
| `tf32` / `fp16` — TensorRT, mixed precision | ≤ 5e-3 | ≥ 0.998 | ≤ 5e-3 |
| `int8` — post-training quantisation | ≤ 5e-2 | ≥ 0.99 | ≤ 2e-2 |

**MOS-TRAIN-163** E2 and E3 MUST be satisfied by **every case** in the equivalence cohort, not by the cohort mean. The tolerances above are per-case floors and ceilings. A mean `post_threshold_dice` of 0.999 over 20 cases is consistent with nineteen cases at 1.000 and one at 0.980, and on a 40 mL effusion a Dice of 0.980 is a symmetric difference of roughly 1.6 mL — small in isolation, and exactly the size of difference that moves a single case across a reporting threshold while the aggregate reports that nothing happened.

**MOS-TRAIN-164** A conversion failing any tolerance MUST NOT be registered. The remedy is a different conversion — another precision class, another opset, a different plugin or layer-fusion configuration — never a relaxed tolerance on that version. A tolerance relaxed to admit one artifact silently relaxes it for every future artifact of that family.

**MOS-TRAIN-165** An E1/E2/E3 pass MUST NOT be reported, summarised, or displayed as evidence of clinical equivalence. It is evidence that the conversion did not break the graph. The clinical question is answered only by the converted version's own `EvaluationRun` against the `AcceptanceCriteria` (`MOS-REG-104`, `MOS-EVID-064`), and quantisation moves behaviour most in exactly the small, low-contrast cases the aggregate metric weights least. The approval dossier MUST render the equivalence block and the converted version's run side by side, and MUST NOT be able to show the equivalence block without the run.

**MOS-TRAIN-166** A converted `ModelVersion` MUST reference the **same** `PreprocessingSpec` version as its `derived_from` source, and its `spec.golden_fixture.output_tensor_sha256` MUST be byte-identical to the source's — preprocessing is not what changed. If the conversion required a preprocessing change (a fixed input shape the spec did not declare, a layout change, a different normalisation range), that is a new `PreprocessingSpec` version by `MOS-IMG-046`, which is a new `ModelVersion` lineage requiring a fresh training-side evaluation — not a conversion, and it MUST NOT be registered with `derived_from` set as though it were.

**MOS-TRAIN-167** `medicalos.json` (`MOS-OPS-070`) MUST be produced by the conversion job, and `built_for` MUST be read from the driver and runtime on the machine that performed the conversion, never taken from configuration or from a template. A `built_for` that is configured rather than observed is a declaration that a plan will load correctly on hardware it was not built for, and Triton's refusal to load is the last defence.

**MOS-TRAIN-168** The conversion job MUST assert bit-identical output on the golden fixture across every value in `allowed_patch_batch_sizes` before publish (`MOS-OPS-078`). This is where that assertion runs. It is what allows Chapter 13 to treat "reduce `patch_batch_size` to fit another model on the GPU" as a capacity decision rather than a revalidation event.

**MOS-TRAIN-169** A conversion MUST NOT be performed against a source `ModelVersion` whose `lifecycle_status` is `SUSPENDED` or `RECALLED`. When a source is recalled, the registry MUST surface every version whose `spec.derived_from` names it on the impact query of `MOS-REG-040`, MUST propose recall of each, and MUST refuse to leave any of them un-recalled without a recorded rationale naming why the defect does not reach the derived artifact. A recall that stops at the source while its TensorRT conversion keeps serving is the recall failing at the only point where it mattered.

**MOS-TRAIN-170** Triton operations for the converted artifact are Chapter 13's and are not restated here: model naming (`MOS-OPS-068`), the constant version directory (`MOS-OPS-069`), the prohibition on load-time accelerator stanzas (`MOS-OPS-071`), `max_batch_size: 0` and the disabled dynamic batcher (`MOS-OPS-072`, `MOS-OPS-076`, `MOS-OPS-077`), `instance_group.count` (`MOS-OPS-073`), the server flags and the forbidden response cache (`MOS-OPS-074`), warmup (`MOS-OPS-075`) and GPU residency (`MOS-REG-076a`, §13.10.3). This pipeline's obligation is to emit an artifact and a `config.pbtxt` that satisfy all of them, and to **fail at packaging time** when they are not satisfied rather than at load time on a node in a hospital.

**MOS-TRAIN-171** The generated `config.pbtxt` MUST be part of the signed artifact and MUST be reproducible from `ModelVersion.spec` plus `medicalos.json`. A configuration file that can be edited on the node is a way to change serving numerics without changing a `ModelVersion`, which is `MOS-SAFE-073` row 17 by a different route.

**MOS-TRAIN-172** The converted version MUST re-pass the Capability's `AcceptanceCriteria` and MUST carry its own paired non-inferiority result against the incumbent, produced by the same procedure as 17.8 including the incumbent re-run of `MOS-TRAIN-146`. The incumbent for a conversion is whatever is currently serving — which may be the source version itself, in which case the comparison is precisely the one that matters: the plan against the checkpoint it came from, on the test partition, through the gate.

---

### 17.10 Promotion

#### 17.10.1 Three human acts, three permissions

**MOS-TRAIN-173** Promotion of a candidate into service requires three distinct human decisions. They MUST be three distinct permissions, three distinct audit records, and they MUST NOT be collapsed into one action or one permission.

| # | Act | Permission | What is attested | Recorded on |
|---|---|---|---|---|
| A1 | Issue the `ValidationReport` | `evidence.report.issue` | the evidence is sound, complete and correctly bound | `validation_reports.approver` (`MOS-EVID-117`), DSSE envelope (`MOS-EVID-118`) |
| A2 | Approve the artifact | `artifact.approve` | this candidate may be deployed at all | `artifact.lifecycle_status = APPROVED` (`MOS-REG-021`) + `AuditEvent` |
| A3 | Promote to clinical use | `deployment.approve_clinical` | this deployment, in this environment, for this tenant, may see patients | `deployment.clinical_use_mode.promoted` (`MOS-SAFE-036`, `MOS-SAFE-037`) |

**MOS-TRAIN-174** No service account, API key, CI identity, scheduled task or any other non-human principal MUST hold `evidence.report.issue`, `artifact.approve`, `deployment.approve_clinical`, `deployment.promote` or `deployment.gate.override`. This MUST be asserted as a query over the role-grant tables in CI, not as a code review convention. It is the primary mechanical expression of this chapter's ruling, and it is falsifiable in one SQL statement.

**MOS-TRAIN-175** A1, A2 and A3 MAY be exercised by the same person where the tenant's governance permits it, and the audit trail MUST record them as three separate acts with three timestamps regardless. Whether one person may hold all three in a `clinical` tenant is a governance question, not an engineering one, and it is carried to Chapter 16 rather than decided here.

#### 17.10.2 The approval dossier

**MOS-TRAIN-176** The dossier MUST be generated, MUST be renderable offline from the report bundle of `MOS-EVID-122` plus registry rows with no network access, and MUST contain exactly the items below. An approver deciding from a metric pasted into a chat message is the failure this object exists to prevent; an approver deciding from a forty-page appendix is the same failure with better manners.

1. **Candidate identity** — `model_id`, `version`, `content_digest`, `derived_from`, `preprocessing_spec_ref` and its digest, and the bundle `metadata.json` version block.
2. **Incumbent** — the version it would replace, its `deployment_id`, its `activated_at`, and how long it has been serving.
3. **Cohort** — `dataset_version_digest`, `patient_count`, the `test` partition's `n_patients`, the `acquisition_profile` summary (`MOS-EVID-024`), the licence, and `deidentification_status`.
4. **Reference standard** — `AnnotationSet` readers and roles, `consensus_rule`, `reference_of_record`, and the inter-reader agreement of `MOS-EVID-043`. A Dice of 0.82 against a reference whose own inter-reader Dice is 0.84 is a different statement from the same figure against 0.97, and the approver must see both numbers on the same line.
5. **Leakage** — L1 through L5 results with every waiver reproduced in full (`MOS-EVID-036`), and the `patient_key_aliases` applied at seal time (`MOS-TRAIN-118`).
6. **Verdict** — the `CriteriaVerdict` with one row per criterion including every `SKIPPED` and `INDETERMINATE` (`MOS-EVID-113`).
7. **Regression** — `mean_delta`, `ci_lower_95_one_sided`, `margin`, `n`, `n_patients`, the `margin_rationale` (`MOS-EVID-087`), and the `catastrophic_count` (`MOS-EVID-088`).
8. **Strata** — the per-stratum table covering every stratum any blocking criterion filters on, with every stratum whose `n < 10` explicitly marked underpowered rather than rendered as a number.
9. **Seed variance** — `seed_variance` (`MOS-TRAIN-127`) rendered beside the declared δ.
10. **Conversion** — when the candidate is a conversion: the `ConversionRun` id, precision class, E1/E2/E3 against their tolerances, and the statement of `MOS-TRAIN-165` verbatim.
11. **Envelope diff** — every `ApplicabilityEnvelope` bound that differs from the incumbent's, with widened bounds flagged and the run that justifies each (`MOS-EVID-098`).
12. **Publisher declarations** — `not_validated_for` and `known_failure_modes` verbatim (`MOS-REG-032`).
13. **Plausibility** — the firing rate of each `fail`-severity rule on the acceptance cohort (`MOS-EVID-112`), so that "this rule has never fired" and "this rule has never been exercised" are distinguishable.
14. **Scope of the decision** — the explicit list of deployments this approval would and would not affect.
15. **Configuration search** — when the candidate came from a `ConfigurationSearch` (`MOS-TRAIN-218`): `search_id`, `trials_completed`, `space_digest`, `selection_metric`, `selection_partition`, `selection_rule`, `selection_margin`, `cost.gpu_hours_used`, `cost.stop_reason`, and the `test_exposure_count` of `MOS-TRAIN-216` for this split. `selection_margin` MUST be rendered on the same line as `seed_variance.sd` (`MOS-TRAIN-127`), and when `selection_margin <= seed_variance.sd` the item MUST carry the literal marker `selection not distinguishable from run-to-run noise`. When the candidate is an ensemble, the member count and the combination rule of `MOS-TRAIN-228` MUST appear here; per-member figures MUST NOT (`MOS-TRAIN-229`).

**MOS-TRAIN-177** The dossier MUST present the incumbent's figures computed on the **same** cohort — the re-run of `MOS-TRAIN-146` — and MUST label any historical published figure for the incumbent separately and as historical. Placing a candidate's fresh number beside an incumbent's year-old number from a different cohort is the most common way an approval is obtained for a comparison that was never performed.

**MOS-TRAIN-178** Every number in the dossier MUST be rendered from a `capability_claims` row or a persisted `evaluation_runs` field and MUST link to its `evaluation_run_id` (`MOS-EVID-072`). The dossier MUST NOT compute a figure of its own. A rendering layer that can compute is a second evaluation implementation with no run behind it.

#### 17.10.3 What promotion does, and what it must not mutate

**MOS-TRAIN-179** `artifact.approve` transitions the candidate `VALIDATED → APPROVED` (`MOS-REG-021`) and MUST record `approved_by`, an `approval_rationale` of at least 20 characters, the `validation_report_id` and its digest, and the `deployment_gate_decisions` row it relied on (`MOS-EVID-091`). Approval is a statement about the **artifact**: it makes the candidate pass filter F3 (`MOS-REG-055`) and does nothing else. It does not route a study, does not create a deployment and does not change what is serving.

**MOS-TRAIN-180** Promotion MUST mint a **new `Deployment` row**. It MUST NOT update `service_version_id` or any model reference on a row whose `state` is `SERVING`. This MUST be enforced structurally: `deployments.service_version_id` MUST be immutable after insert, by revoking `UPDATE` on the column from the application role and by a `BEFORE UPDATE` trigger, in the same manner Chapter 7 requires for sealed evidence columns (`MOS-EVID-013`). A platform that can edit a serving row can change what is running without producing a deployment record, and every provenance record written before that edit then names an artifact that was not the one that ran — an unfalsifiable condition that no later audit can detect.

**MOS-TRAIN-181** The promotion sequence MUST be exactly these five steps, each its own audited call:

| Step | Action | Actor | Reference |
|---|---|---|---|
| 1 | Create the candidate `Deployment` at `role = STANDBY, state = PENDING` | operator-triggered automation permitted | `MOS-REG-076` |
| 2 | `PENDING → VERIFYING → SERVING`: supply-chain verification, then the worker golden-fixture self-test | automatic | `MOS-REG-075`, `MOS-IMG-054` |
| 3 | `evaluate_gate()` against the tenant's acceptance binding | operator-triggered automation permitted | `MOS-EVID-090`, `MOS-EVID-091` |
| 4 | For `clinical_use_mode: clinical`, the E1 gate with its named approver | **human, A3** | `MOS-SAFE-036` |
| 5 | Cutover: the single transaction swapping `role` between incumbent and candidate | **human** | `MOS-REG-077` |

**MOS-TRAIN-182** Steps 1 through 3 MAY be executed by automation that an operator triggered. Steps 4 and 5 MUST each require a distinct human action. In particular, a `PASS` from `evaluate_gate()` MUST NOT trigger step 5, and no configuration value, feature flag or promotion policy MUST be able to make it do so. The gate says the candidate is *permitted*; it does not say the candidate should go *now*, and the difference between those two statements is where every remaining piece of clinical judgement lives.

**MOS-TRAIN-183** On `FAIL` or `INDETERMINATE` at step 3 the incumbent MUST remain live and serving (`MOS-EVID-092`) and the candidate deployment MUST remain at `role = STANDBY`. The pipeline MUST NOT retire, suspend or drain the incumbent as part of any promotion sequence; the incumbent leaves service only at step 5 and only into `STANDBY`.

**MOS-TRAIN-184** `promotion_policy.auto_promote` MUST be `false` whenever `clinical_use_mode = clinical` (`MOS-REG-080`). This chapter adds: `auto_promote: true` MUST be refused for **any** deployment in a `(tenant, environment)` where the same `capability_id` has any `clinical_use_mode: clinical` deployment. A research canary configured to auto-promote, sharing a capability slot with a clinical deployment, is one `clinical_use_mode` edit away from the automated production-to-serving path this chapter exists to forbid.

#### 17.10.4 Rollback

**MOS-TRAIN-185** Rollback MUST be the reverse role swap of `MOS-REG-078` and MUST NOT require a new `EvaluationRun`, a new `ValidationReport` or a new approval, provided the target's `lifecycle_status` is not `SUSPENDED` or `RECALLED`. Speed is the safety property here: an approval process attached to rollback converts a bad promotion into a long outage, and the second-order effect is that operators stop promoting at all.

**MOS-TRAIN-186** The outgoing incumbent MUST remain at `role = STANDBY, state = SERVING` — warm, verified and resident — for a declared `rollback_window`, default 30 days, and MUST NOT be retired inside that window by any automation. Its GPU residency MUST be reserved for the window under Chapter 13's budget arithmetic (§13.10.3, `MOS-OPS-084`), and the reservation MUST be checked **before step 1** of `MOS-TRAIN-181`, not discovered at step 5. A promotion that cannot afford to keep its predecessor warm is a promotion without a rollback, and it must be refused while it is still a capacity question.

**MOS-TRAIN-187** A rollback MUST NOT alter, withdraw, re-derive or re-mark any `Result` the promoted version already produced (`MOS-SAFE-055`, `MOS-SAFE-073` rows 5 and 7). Rollback changes what runs next. The DICOM objects the promoted version wrote keep their deterministic `SeriesInstanceUID`s (`MOS-REG-105`) and their provenance, and re-running a study under the rolled-back version produces a distinct series — which is correct, because the numerics differ and PACS must show that they differ.

**MOS-TRAIN-188** A rollback MUST write an `AuditEvent` carrying the reason and MUST move the rolled-back candidate to `SUSPENDED` (`MOS-REG-021`) unless the operator explicitly records that the rollback was for a non-artifact cause — capacity, an unrelated outage, a scheduling conflict. Leaving a rolled-back candidate at `APPROVED` invites a second promotion of the same defect by a different operator a week later.

**MOS-TRAIN-189** There MUST be no path — no function, no API call, no scheduled task, no policy — from a `TrainingRun`, a `ConversionRun`, an `EvaluationRun` or a `ValidationReport` to a `state = SERVING` `Deployment` that does not pass through steps 4 and 5 of `MOS-TRAIN-181`. CI MUST assert this as a call-graph property: no symbol reachable from the pipeline package transitively reaches the deployment role-mutation or `clinical_use_mode` transition functions. That assertion, together with the grant query of `MOS-TRAIN-174`, is what makes this chapter's ruling structural rather than procedural. An automated path from production data to a serving model would route around the entire evidence plane, and the evidence plane is the platform's reason to exist.

---

### 17.11 Release placement and what is deferred

#### 17.11.1 Placement

**MOS-TRAIN-190** This chapter's contents are assigned to releases as follows, consistent with Chapter 15 §15.1.2 and the spine's release plan. The evidence plane is the prerequisite: curation, sealing, splits and annotation land **with** it in 0.2.0; training, evaluation, conversion and promotion land in 0.3.0.

| Element | 0.1.0 | 0.2.0 | 0.3.0 | 0.4.0 |
|---|---|---|---|---|
| `CurationBatch`, exclusion vocabulary, `patient_key_aliases` | — | **ships** | unchanged | unchanged |
| Seal procedure (`MOS-TRAIN-208`–`MOS-TRAIN-111`) | — | **ships** | unchanged | unchanged |
| Split freezing, stratified assignment, `test` floor | — | **ships** | unchanged | unchanged |
| L1–L5 as a blocking gate (`MOS-TRAIN-115`–`MOS-TRAIN-119`) | — | **ships** — satisfies Chapter 15's `leakage-check` | + run-time re-execution | unchanged |
| `AnnotationSet` production | — | **ships** | unchanged | unchanged |
| Generated transform chain + the byte-equality check of `MOS-TRAIN-133` | — | **ships** | unchanged | unchanged |
| `TrainingRun`, `Orchestrator` port, one driver | — | — | **ships** | unchanged |
| MONAI Bundle as the native-mode artifact source form | — | — | **ships** | unchanged |
| Candidate registration at `REGISTERED` → `VALIDATING` | — | — | **ships** | unchanged |
| Incumbent re-run (`MOS-TRAIN-146`) | — | — | **ships** | unchanged |
| `ConversionRun`, E1/E2/E3, tolerances | — | — | **ships** | unchanged |
| Auto-configuration as the default backend, the fingerprint freeze (`MOS-TRAIN-223`–`MOS-TRAIN-226`) | — | — | **ships** | unchanged |
| `ConfigurationSearch`, search provenance, budget bound, single nomination | — | — | **ships** | unchanged |
| Single-artifact ensembles and the combination-rule vocabulary | — | — | **ships** | unchanged |
| Approval dossier, the three-act promotion, rollback window | — | — | **ships** | unchanged |

**MOS-TRAIN-191** The transform-chain generator and its byte-equality check MUST land in 0.2.0 even though no training run exists before 0.3.0. It depends only on `PreprocessingSpec`, which ships in 0.1.0, and it closes the train/serve skew hazard **before** a second model exists to skew. Deferring it to 0.3.0 would mean the first model produced by this pipeline is also the first test of the mechanism meant to protect it.

**MOS-TRAIN-192** The 0.1.0 pleural-effusion model is trained outside this pipeline and packaged by the explicit artifact step of `MOS-REL-015`. This chapter does not apply retroactively and MUST NOT be read as legitimising that artifact's provenance. Its `ValidationReport` MUST state that no `TrainingRun` record exists for it, and the model MUST be re-sealed and re-evaluated through this pipeline before 0.3.0 or MUST carry that statement for its whole life.

**MOS-TRAIN-193** The gate checks this chapter adds to Chapter 15's 0.3.0 gate row are named in observable terms and name no product (`MOS-REL-008`): `leakage-blocks-training`, `chain-equivalence`, `served-plan-equivalence`, `no-auto-promote`. Each is defined by the correspondingly numbered acceptance criterion below and MUST be executed in the release's CI run (`MOS-REL-012`).

#### 17.11.2 Deferred, with the reason

**MOS-TRAIN-194** Automated retraining triggered by a monitoring signal MUST NOT be implemented in 0.1–0.4. A drift alert that starts a training run, which produces a candidate, which passes a gate, which promotes itself, is the automated path from production data to a serving model — the exact construction this chapter forbids, assembled from four individually reasonable components. Monitoring alerts notify; they do not act (`MOS-EVID-139`).

**MOS-TRAIN-195** Continual or online learning on production traffic MUST NOT be implemented. A sealed `DatasetVersion` is a materialised list of instances (`MOS-EVID-016`); a stream is not a list, and a model fitted on a stream has no cohort to evaluate against, no split to check for leakage, and no manifest for a report to cite. Production images become training data only by passing through `MOS-TRAIN-202`'s curation and `MOS-TRAIN-208`'s seal, like any other images.

**MOS-TRAIN-196** Active learning MAY be implemented, and only as a **suggestion** mechanism: a ranking over unannotated candidates presented to a curator. Every suggested case MUST enter the `CurationBatch` as an ordinary candidate with a recorded human disposition, MUST be subject to the same exclusion vocabulary (`MOS-TRAIN-204`), and MUST NOT bypass sealing or splitting. A selection policy that reads model outputs and writes directly into a training cohort is a feedback loop with no evidence plane in it.

**MOS-TRAIN-197** Federated and multi-site training are deferred beyond 0.4 with a stated blocker rather than a vague one: L3 and L4 of `MOS-EVID-034` compare `series_pixel_digest` and image hashes **across partitions**, which a federation cannot compute centrally without defeating the reason it is a federation. Until the leakage checks have a federated answer, a federated cohort cannot be shown to be leak-free, and an unverified split is not a split. Distributed multi-node training within one site, a local mirror of a public model zoo, and foundation-model fine-tuning are deferred as scope, not as principle, and require no new seam. Configuration search is **not** deferred — it is specified in 17.7.6 and bounded there — but distributed search infrastructure beyond a single orchestrator driver is, on the same scope-not-principle basis.

**MOS-TRAIN-198** Each deferred item above MUST be deferred behind a named seam that exists from the release in which its first driver ships (`MOS-REL-023`). The seam for all of them is the `Orchestrator` port of `MOS-TRAIN-122` together with the `CurationBatch` boundary of `MOS-TRAIN-202`: every deferred mechanism enters as a producer of curation candidates or as an orchestrator driver, and none of them acquires a route to a `Deployment`.

---

### Acceptance criteria

Each check is executable by a CI job or by a reviewer with database access. Failing any of them means this half of the chapter is not implemented.

1. **The leakage check blocks training, not just reporting.** Construct a frozen split in which one `patient_key` appears in `train` and in `test`. Assert the freeze-time L1 of `MOS-EVID-034` reports `fail`; then attach a `MOS-EVID-036` waiver to it and submit a `TrainingRun` against that split. Assert the run is refused before the first batch is loaded, with the violating `patient_key` and both partitions named, and that the waiver did not permit it (`MOS-TRAIN-115`, `MOS-TRAIN-116`). Repeat with the leak placed across `train`/`tune` and across `tune`/`test`. Repeat with the two studies of the leaked patient dated five years apart and assert the outcome is unchanged (`MOS-TRAIN-117`).

2. **L1 is patient-scoped, not study-scoped.** Build a fixture in which one patient's 2019 and 2024 studies fall on opposite sides of the split. Assert L1 fails. Then grep the split tooling for any configuration named `min_days_between_studies`, `study_level_split`, `allow_same_patient` or equivalent; expect zero hits (`MOS-TRAIN-117`).

3. **Two MRNs are an identity defect, not a partition defect.** Seal a cohort in which one patient appears under two `PatientID`s with distinct `patient_key`s and one study each, placed in different partitions. Assert L2 reports `fail`, that moving one study across partitions is refused as a remedy, and that the only accepted remedy writes a `patient_key_aliases` row, re-seals the `DatasetVersion` with a `parent_version_id` and a merge `derivation`, and re-freezes the split (`MOS-TRAIN-118`, `MOS-TRAIN-119`).

4. **Exclusions survive.** Assert that `DELETE` on `curation_batch_items` fails for the application role, that a sealed `DatasetVersion` derived from a batch carries a `derivation` whose `removed_series` equals the batch's `exclude` count, and that the exclusion vocabulary configured for every `Dataset` contains no value whose meaning is a model outcome (`MOS-TRAIN-203`, `MOS-TRAIN-204`, `MOS-TRAIN-205`).

5. **Sealing is idempotent and refuses bad geometry.** Run the seal twice on one included set; assert one `dataset_versions` row and one `manifest_digest`. Inject a series with 6° gantry tilt against a spec declaring `gantry_tilt.mode: reject`; assert the seal is refused naming that `series_instance_uid`, and that editing the spec's `max_deg` to admit it produces a new `PreprocessingSpec` version and therefore a new `ModelVersion` lineage rather than a re-seal (`MOS-TRAIN-209`, `MOS-TRAIN-111`, `MOS-IMG-046`).

6. **The golden-fixture equivalence check between the generated chain and the serving implementation.** For every registered `PreprocessingSpec` version, run the generated MONAI chain and `medicalos-preprocessing` on the shipped golden fixture with TTA disabled and `patch.batch_size = 1`, hash both output tensors by `MOS-IMG-054` step 4, and assert **byte equality** — not equality within a tolerance. Then perturb the generated chain's `Spacingd` mode from `bilinear` to `nearest` and assert the check fails. Then submit a `PreprocessingSpec` declaring `image_interpolator: bspline3` and assert the generator refuses to emit rather than substituting (`MOS-TRAIN-132`, `MOS-TRAIN-133`).

7. **One recorder for the golden hash.** Assert that `golden_fixture.output_tensor_sha256` on every registered `model_version` was produced by `medicalos-preprocessing`, by asserting a recomputation from the shipped fixture through that package reproduces it exactly, and grep the training package for any independent implementation of the step-4 hash; expect zero hits (`MOS-TRAIN-134`, `MOS-IMG-058`).

8. **The checkpoint-versus-served-plan equivalence check.** Convert a trained checkpoint to ONNX fp32 and to a TensorRT fp16 plan. For each, run the golden fixture and the 20-patient `equivalence_cohort` through both the checkpoint and the converted artifact under the identical `PreprocessingSpec`, patch order, TTA setting and `patch.batch_size`. Assert E1, E2 and E3 are recorded per case; assert the fp32 conversion satisfies E1 ≤ 1e-4, E2 ≥ 0.9995 and E3 ≤ 1e-3 on **every** case; assert the fp16 conversion is held to the `tf32/fp16` row. Then inject a single case at `post_threshold_dice = 0.980` into an otherwise perfect fp32 cohort and assert registration is refused even though the cohort mean is 0.999 (`MOS-TRAIN-159`–`MOS-TRAIN-164`).

9. **The run must exercise the served plan.** Attempt to attach to a `tensorrt_plan` `ModelVersion` an `EvaluationRun` whose `inference_backend` is the training container's PyTorch runtime. Expect refusal. Attempt to attach a run executed on `sm_80` to a version whose `built_for.cuda_compute_capability` is `8.6`. Expect refusal (`MOS-TRAIN-153`, `MOS-OPS-070`, `MOS-EVID-064`).

10. **Equivalence is not clinical equivalence.** Assert the approval dossier cannot render the `conversion_equivalence` block for a converted version whose own `EvaluationRun` is absent or not `SUCCEEDED`, and assert the statement of `MOS-TRAIN-165` appears verbatim in the rendered block (`MOS-TRAIN-165`, `MOS-REG-104`).

11. **Preprocessing carries forward unchanged.** Assert every `ModelVersion` with non-null `derived_from` shares its source's `preprocessing_spec_ref`, `preprocessing_spec_digest` and `golden_fixture.output_tensor_sha256` byte for byte. Attempt to register a conversion with a different `PreprocessingSpec` version and `derived_from` set; expect refusal (`MOS-TRAIN-166`).

12. **No automated path from a training run to a serving deployment.** (a) Query the role grants and assert no non-human principal holds `evidence.report.issue`, `artifact.approve`, `deployment.approve_clinical`, `deployment.promote` or `deployment.gate.override`. (b) Run a call-graph analysis and assert no symbol reachable from the pipeline package transitively reaches any deployment role-mutation or `clinical_use_mode` transition function. (c) Drive a candidate end to end — seal, split, train, evaluate, gate `PASS` — with every automation enabled, and assert that after `evaluate_gate()` returns `PASS` the candidate deployment is at `role = STANDBY`, the incumbent is still `role = ACTIVE, state = SERVING`, and no `Job` has resolved to the candidate. (d) Attempt to set `promotion_policy.auto_promote: true` on a deployment in an environment where the same capability has a `clinical` deployment; expect refusal (`MOS-TRAIN-174`, `MOS-TRAIN-182`, `MOS-TRAIN-184`, `MOS-TRAIN-189`).

13. **Promotion mints, never mutates.** Connect as the application role and attempt `UPDATE deployments SET service_version_id = <other> WHERE state = 'SERVING'`. Expect a permission error or a trigger exception. Assert that a completed promotion left two `deployments` rows for the slot and that cutover was a single transaction swapping `role` (`MOS-TRAIN-180`, `MOS-REG-077`).

14. **Rollback is fast and keeps the predecessor warm.** After a cutover, assert the outgoing incumbent is at `role = STANDBY, state = SERVING` with a reserved GPU residency, assert a rollback completes in under 5 s of database time without re-entering `VERIFYING`, assert no `Result` produced by the promoted version was altered, and assert a promotion is refused at step 1 when the incumbent's residency reservation cannot be satisfied for the `rollback_window` (`MOS-TRAIN-185`–`MOS-TRAIN-187`, `MOS-REG-078`).

15. **The test partition is unreachable before evaluation.** Issue a cohort request for `partition: "test"` from the training job's principal. Assert HTTP 403 with a reason, not an empty set. Assert the training container's resolver rejects a filesystem path, a bucket prefix and a glob as a cohort argument (`MOS-TRAIN-120`, `MOS-TRAIN-141`).

16. **The incumbent is re-run, and `unpaired_runs` is not swallowed.** Grow a cohort, freeze a new split, and submit a candidate. Assert the pipeline created an incumbent `EvaluationRun` on the new `split_digest` using the incumbent's own `PreprocessingSpec` and backend. Then disable that step and assert the pipeline aborts with `unpaired_runs` surfaced, rather than taking the `no_incumbent_first_deployment` branch (`MOS-TRAIN-146`–`MOS-TRAIN-148`).

17. **The naive comparison is absent from the pipeline too.** Grep the pipeline package for a direct comparison of two aggregate metric values on any blocking path; expect zero hits. Assert the package imports Chapter 7's evaluator (`MOS-TRAIN-145`, `MOS-TRAIN-149`).

18. **Automation stops at the report.** Run the pipeline to completion with every automation enabled and assert the terminal state is: `EvaluationRun` `SUCCEEDED`, candidate at `lifecycle_status = VALIDATED`, a self-verified unsigned report payload with a computed digest, a rendered dossier, and **no** `validation_reports` row. Then attempt issuance with a service-account identity in `approver`; expect write-time rejection (`MOS-TRAIN-151`, `MOS-TRAIN-152`).

19. **The dossier is complete, offline and computes nothing.** Render the dossier on a host with no network route to the platform from the report bundle plus a registry export; assert all fifteen items of `MOS-TRAIN-176` are present, that every figure carries a link to an `evaluation_run_id`, that the incumbent's figures are labelled as coming from the same-cohort re-run, and that any historical figure is labelled historical (`MOS-TRAIN-176`–`MOS-TRAIN-178`).

20. **Reproducibility is pinned, not claimed.** Assert no `TrainingRun` reaches `SUCCEEDED` with a null in any field of `MOS-TRAIN-124` or with `code_dirty = true`. Assert each capability with a promoted candidate has a `seed_variance` record from at least three seed-varied runs, and assert the dossier renders `seed_variance.sd` adjacent to δ. Grep for any claim of bit-exact training reproducibility in documentation or UI strings; expect zero hits (`MOS-TRAIN-124`–`MOS-TRAIN-128`).

21. **nnU-Net does not smuggle a second preprocessing path.** Train a candidate with `training_backend.kind = "nnunet"`. Assert `plan_digest` is recorded, that the plan was derived only from `fit_partition`, that the plan's spacing, normalisation, patch size and crop rule appear in the registered `PreprocessingSpec`, and that no nnU-Net preprocessing module is present in the serving image's import closure. Attempt to register a 5-fold ensemble without a single exported artifact; expect refusal (`MOS-TRAIN-135`–`MOS-TRAIN-137`).

22. **The Python backend cannot reach clinical.** Attempt to create a `clinical_use_mode: clinical` deployment for a `ModelVersion` whose `runtime.backend` is `python`; expect refusal with the reason named. Assert the same version deploys successfully at `role = SHADOW, clinical_use_mode: research_only` (`MOS-TRAIN-155`, `MOS-REG-082`).

23. **The pipeline cannot see the acceptance cohort.** As the training orchestrator's service account, attempt to read a `DatasetVersion` whose `Dataset.purpose` is `acceptance` and one whose purpose is `monitoring`. Expect denial by RLS or grant in both cases, not an empty result (`MOS-TRAIN-206`, `MOS-TRAIN-207`).

24. **Training cannot evict serving.** With a `clinical` deployment resident on the shared GPU pool, submit a training run. Assert it is refused or scheduled onto the disjoint pool, and assert no `tritond` reservation or eviction was requested by any pipeline principal (`MOS-TRAIN-123`).

25. **A search cannot read the partition it will be judged on.** Run a 6-trial `ConfigurationSearch` to completion. Assert `read_partitions` is `{train,tune}` and that the `CHECK` constraint refuses an `UPDATE` inserting `test`. Issue a `partition: "test"` cohort request from the search driver, from a trial process and from the ensemble builder; assert HTTP 403 with a reason in all three, not an empty set. Assert exactly one trial has `nominated = true`, that the unique partial index refuses a second, that exactly one `ModelVersion` was registered, and that `test_exposure_count` for the split incremented by exactly one across the whole search (`MOS-TRAIN-214`, `MOS-TRAIN-216`, `MOS-TRAIN-219`).

26. **The fingerprint is frozen, not re-derived.** Train a candidate with `training_backend.kind = "auto3dseg"`. Assert `fingerprint_digest` is recorded and that every quantity in `MOS-TRAIN-223`'s mapping table appears in the registered `PreprocessingSpec` with `normalisation.statistics_source: spec`. Assert the derived training batch size appears in `hyperparameters` and **not** in `patch.batch_size`. Grep the serving image's resolved import closure for the planner and `DataAnalyzer` module names; expect zero hits. Then delete the fingerprint document from the packaged artifact, re-run the `MOS-IMG-054` self-test on a clean checkout, and assert `golden_fixture.output_tensor_sha256` still reproduces byte for byte (`MOS-TRAIN-223`–`MOS-TRAIN-226`).

27. **One ensemble, one version, one run, one Triton model.** Register a 3-member `mean_probability` ensemble. Assert it is a single `ModelVersion` with one `spec.weights.digest` whose bundle metadata names all three members, their checkpoint digests and the rule. Assert exactly one `EvaluationRun` on `test` exists for it and that no `capability_claims` row references a member run. Attempt to register the same ensemble as three `ModelVersion`s plus a runner-side averaging step; expect refusal. Attempt to load a `config.pbtxt` containing an `ensemble_scheduling` stanza; expect `tritond` refusal. Assert `medicalos.json`'s `weights_bytes` and `workspace_bytes_by_patch_batch` were measured on the combined graph and that the `MOS-OPS-084` residency check ran against those figures (`MOS-TRAIN-227`–`MOS-TRAIN-231`).

28. **The gate stays per-candidate, and the search cannot reach it.** Submit an `artifact.approve` request whose body carries an array of two `model_version_id`s; expect RFC 9457 `class: schema_violation`. Assert no bulk-approval route exists in the OpenAPI document for `artifact.approve`, `evidence.report.issue` or `deployment.approve_clinical`. Run the call-graph analysis of `MOS-TRAIN-189` extended to the search driver and assert no symbol reachable from it reaches an approval or deployment-mutation handler. Attempt to configure a promotion policy of the form "promote the top-ranked candidate"; expect refusal (`MOS-TRAIN-232`, `MOS-TRAIN-233`).

29. **The search is bounded and its trajectory reproduces.** Submit a search with `budget.max_gpu_hours` set below the space's cost. Assert it stops at the bound, records `cost.stop_reason: max_gpu_hours`, and still writes a valid `ConfigurationSearch` row. Submit a space document containing a Python callable or an expression string; expect refusal. Submit a space listing `target_spacing_mm` both in `derived_from_fingerprint` and as an axis; expect refusal. Re-run a `strategy: grid` search from its recorded `(space_digest, seed, fingerprint_digest)` and assert the identical ordered sequence of `config_digest` values; assert no documentation or UI string claims bit-exact reproduction of trial weights (`MOS-TRAIN-220`, `MOS-TRAIN-234`, `MOS-TRAIN-235`).

---

[← 16. Open Questions](16-open-questions.md) · [Index](../../MEDICALOS_SPEC.md) · [18. Standards and Regulatory Conformance →](18-conformance.md)
