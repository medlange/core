# Build-versus-adopt register

`MOS-REL-027`: "The register below MUST be maintained in the repository at
`docs/adr/BUILD_VS_ADOPT.md` and MUST be normative: no new subsystem MUST be built without
a row, and a row MUST NOT record 'build' without naming at least one incumbent that was
considered and the specific property that disqualified it."

## THIS FILE IS INCOMPLETE, AND THE GAP IS NAMED RATHER THAN HIDDEN

It was created in release 0.3.0 by the training-pipeline component, because
`MOS-TRAIN-122` ("The port MUST have a row in the build-versus-adopt register") and
`MOS-TRAIN-210` ("The rows below MUST be appended to the `MOS-REL-027` register table")
are obligations of that component and a missing file cannot be checked at all.

**What is present:** the six MONAI and nnU-Net rows `MOS-TRAIN-019` requires verbatim, the
four auto-configuration rows `MOS-TRAIN-210` requires verbatim, and the `Orchestrator`
port row `MOS-TRAIN-122` requires.

**What is owed, and by whom:** every row of chapter 15 §15.3.1 — Durable execution, DICOM
object writing, Model serving, PACS, Viewer, Policy, Workflow, De-identification, Object
storage, Event bus, Identity provider, Model weights (vendored), Telemetry — together with
the two honest rows of §15.3.2 (`MOS-REL-028`, `MOS-REL-031`) and the adoption trigger
`MOS-REL-029`. Those subsystems were built in 0.1.0 and 0.2.0 without a register, and
chapter 15 acceptance criterion 9 is RED until they are transcribed here. Adding a row for
a subsystem this component did not build would mean inventing a decision nobody recorded,
which is the failure `MOS-REL-027` exists to prevent ("the previous version stated 'use
existing open-source components' and named only the six components that were never in
question, which silently resolved every contested decision as 'build'").

---

## Chapter 17 §17.2 — MONAI is five products under one name (`MOS-TRAIN-019`)

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **MONAI Core** — transforms, `sliding_window_inference`, losses, metrics, networks | Apache-2.0 | **ADOPT** | The dictionary transform vocabulary (`Orientationd`, `Spacingd`, `ScaleIntensityRanged`, `NormalizeIntensityd`, `CropForegroundd`), `monai.inferers.sliding_window_inference`, `DiceCELoss`, the 3D network zoo | `medicalos-preprocessing`, which *constructs* a `Compose` from a `PreprocessingSpec` and is the only constructor in the codebase | `ForegroundCropToMinSized` (one transform, 17.3.4); the spec↔chain serialization; the golden-fixture hash | The chain is the serialization target for `MOS-IMG-049`. One chain, versioned once, executed by training and serving alike, is the mechanism that closes train/serve skew at its root rather than testing for it afterwards. Re-implementing spline resampling and Gaussian-blended sliding windows would be a research project with a worse determinism story. |
| **MONAI Bundle** — `configs/`, `models/`, `metadata.json` with a declared schema | Apache-2.0 | **ADOPT** | The bundle directory layout and `metadata.json` schema as the on-disk form of a native-mode `ModelVersion` artifact | The OCI packaging of `MOS-REG-084`, which carries the bundle as layers | The mapping from bundle fields to the `model_version` manifest of `MOS-REG-030`; the `PreprocessingSpec` and golden-fixture blobs, which a bundle has no slot for | A bundle already carries weights, an inference configuration and typed metadata under one directory with a published schema. That is 80 % of what `MOS-REG-030` needs, and it is the interchange format the Model Zoo speaks. |
| **MONAI Model Zoo** | per-bundle; varies | **ADOPT as a source** | Pretrained bundles as *inputs* to the pipeline | Re-packaging into a MedicalOS `ModelVersion` | Licence verification per `MOS-REL-038`; a mandatory own `EvaluationRun` | A zoo bundle is a starting point, never a `ModelVersion`. Its published metrics were measured on its own cohort under its own conventions and are not MedicalOS metrics (`MOS-EVID-071`). |
| **MONAI Label** — annotation server + viewer plugins | Apache-2.0 | **ADOPT** | The annotation server, its Slicer and OHIF clients, its scribble and interactive-segmentation apps | An authentication shim binding every session to a `User`; an exporter writing per-reader masks into an `AnnotationSet` manifest | Reader identity binding; the `annotation_provenance` record; the consensus computation, which the platform performs, not the tool | It is the only open annotation stack that already speaks DICOMweb, runs against a segmentation model for seeding, and has a real viewer story. It is the natural producer of `AnnotationSet`. Its own training loop is disabled — see `MOS-TRAIN-106`. |
| **MONAI Deploy / MAP** | Apache-2.0 | **REFUSE** | nothing | nothing | nothing | See `MOS-TRAIN-026` and `MOS-REL-033`. |
| **nnU-Net** (via MONAI's integration) | Apache-2.0; weights vary | **PERMIT as a training backend** | The self-configuring planner and the training recipe | A plan exporter that emits a `PreprocessingSpec` (`MOS-TRAIN-030`) | The refusal to run nnU-Net's own inference wrapper on the serving path | It is the strongest segmentation baseline available and the 0.1–0.3 corpus is segmentation-heavy. Not adopting it would mean hand-tuning architectures against a published baseline that beats them. |

**Where each of these lands in the tree.** `medos/medos/training/preprocess.py` and
`medos/medos/training/chain.py` are the MONAI Core wrapper; `medos/medos/training/bundle.py` is the
MONAI Bundle mapping; `medos/medos/training/autoconfig.py` is the nnU-Net / `Auto3DSeg` plan
exporter. `MOS-TRAIN-026`'s refusal of MAP is structural: no module in the tree builds,
publishes, accepts or executes one, and `execution.sealed.image` is chapter 2's HTTP
contract.

---

## Chapter 17 §17.7.6 — auto-configuration and search (`MOS-TRAIN-210`)

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **Dataset-fingerprint auto-configuration** — the nnU-Net planner; MONAI `Auto3DSeg` `DataAnalyzer` + `BundleGen` | Apache-2.0 | **ADOPT as the default training backend** for segmentation capabilities | The fingerprint computation and the published derivation rules for target spacing, intensity window, normalisation scheme, foreground crop and patch size | An exporter that transcribes the derived configuration into a `PreprocessingSpec` (`MOS-TRAIN-223`) and freezes it | The freeze; the refusal to re-derive at serving time (`MOS-TRAIN-225`); the fingerprint digest on the run | For 3D medical segmentation the auto-configured baseline reliably beats hand-tuning by a team that is not doing this full time, and the 0.1–0.4 corpus is segmentation-heavy. Hand-tuning here means spending months to arrive below a published default. |
| **`Auto3DSeg` `AutoRunner`** — multi-algorithm search and ensembling | Apache-2.0 | **PERMIT, bounded** | The algorithm templates, the trial loop and the ensembler as a *producer of candidates* | A `ConfigurationSearch` record (`MOS-TRAIN-218`); the nomination step (`MOS-TRAIN-216`); the single-artifact ensemble export (`MOS-TRAIN-227`) | The selection provenance, the budget bound, and the prohibition on reading `test` | It is the cheapest way to find out whether a second architecture family helps on a given capability. What it does not do is produce an unbiased estimate of the winner, which is why everything in this section attaches to it rather than to the planner. |
| **Neural architecture search** — `DiNTS` and equivalents | Apache-2.0 | **NOT ADOPTED as a default** | nothing by default | — | — | The search phase costs GPU-weeks per capability, and for 3D segmentation on cohorts of a few hundred patients the published margin over an auto-configured U-Net is small, inconsistent across datasets, and of the same order as the seed variance `MOS-TRAIN-127` already requires us to measure. Paying GPU-weeks for a difference we cannot distinguish from a re-run is not a build-versus-adopt decision, it is an arithmetic one. |
| **Generic large-scale hyperparameter search** — Bayesian, population-based or bandit optimisation over a wide space, tens to hundreds of trials | n/a | **NOT ADOPTED as a default** | nothing by default | — | — | The auto-configured defaults already sit near the optimum for learning rate, schedule and augmentation on this class of problem; the marginal metric gain is small while the selection bias of `MOS-TRAIN-213` grows with the number of trials. A wide search buys a small real improvement and a large imaginary one, and only the imaginary one is visible in the report. |

---

## Chapter 17 §17.7.1 — the `Orchestrator` port (`MOS-TRAIN-122`)

| Subsystem | Incumbents considered | Licence | What is **adopted** | What is **wrapped** | What is **genuinely added** | Justification for the build portion |
|---|---|---|---|---|---|---|
| **Training orchestration** | Argo Workflows; Airflow; Flyte; Prefect; Temporal; Kubeflow Pipelines | Apache-2.0 ×5; MIT (Temporal) | Nothing in 0.3.0. Chapter 15's **Workflow** register row already adopts a batch workflow engine for the offline evidence-plane path, and `MOS-TRAIN-121` states the orchestrator as four constraints rather than as a product (`MOS-REL-008`) | The `Orchestrator` port of `MOS-TRAIN-122` — `Submit` / `Poll` / `Cancel` / `Logs` — in `medos/medos/training/orchestrator.py`, with one shipped driver (`LocalProcessOrchestrator`, a separate OS process with a constructed environment) and one test fake (`FakeOrchestrator`, `MOS-REL-048`) | The four constraints C1–C4 as executable data; the GPU-pool guard of `MOS-TRAIN-123`, injected rather than imported so the orchestrator cannot request a residency reservation; the refusal of a fifth port method (`MOS-TRAIN-198`: "none of them acquires a route to a `Deployment`, because there is none to acquire") | **No orchestrator is built.** What the port disqualified every incumbent from being is the *default*: a site already running Argo or Airflow must not be made to run a second engine, and chapter 15's Orchestration-platform row forbids "orchestrator API calls from application code". A site adopting one writes a driver implementing these four methods; the platform never calls its API. The shipped driver exists so that `MOS-REL-048`'s two-implementations rule is satisfied by something a deployment can actually run, not by two fakes. |

**`MOS-TRAIN-123`, recorded as a constraint on any driver.** "Training GPUs MUST be a pool
disjoint from the serving pool, **or** the orchestrator MUST refuse to schedule while the
shared pool carries any `clinical_use_mode: clinical` deployment. The orchestrator MUST NOT
be able to request a reservation from `medicalos-tritond` or to cause an eviction." In this
tree the guard is a callable injected into the driver: nothing under `medos/medos/training/`
imports `medos.inference.residency`, and `tests/integration/test_training_run.py` asserts
the import closure.

---

## Chapter 15 §15.3.1 — the **Viewer** row (`MOS-REL-027`)

This row is one of the rows the header of this file records as *owed*. It is written now
because release 0.4.0 reverses `MOS-CORE-038` and a reversal without a register row would be
the exact failure `MOS-REL-027` exists to prevent. The reversal itself is recorded against
`MOS-CORE-036` in `docs/spec/01-overview.md`; this row is the engineering half of it.

**That sentence was false for the whole of specification 0.2.0.** The non-goals table carried no
annotation on the `MOS-CORE-038` row -- no strike-through, no reversal note, nothing. An ADR that
asserts a record which does not exist is the same defect as a register that omits a row, one
document further along, and it is worse in one respect: it stops the next reader looking. The
record was written at specification 0.3.0 and the sentence is true from there. It is left standing
rather than reworded because what it claimed is now the case, and because the gap between an ADR's
claim and the specification's own text is exactly what register entry 103 was about.

| Subsystem | Incumbents considered | Licence | What is **adopted** | What is **wrapped** | What is **genuinely added** | Justification for the build portion |
|---|---|---|---|---|---|---|
| **Clinician-surface viewer** | OHIF Viewer v3 (pinned `v3.9.2`, deployed since 0.1.0); Cornerstone3D as a library without the OHIF application shell; 3D Slicer; Weasis; dwv; the site's incumbent viewer | MIT (OHIF, Cornerstone3D, dwv); BSD-style (3D Slicer); EPL-2.0 (Weasis) | The DICOMweb wire format (`MOS-DATA-015`), the DICOM object model, and the standard's own definitions of SEG geometry (C.8.20) and TID 1500 — none of which is anybody's software | Nothing. This row records a build, and a build that claimed to wrap something would be describing a fork, which `MOS-UI-009` forbade and which this reversal does not re-permit | A WebGL2 stack renderer for uncompressed Explicit VR Little Endian CT; a DICOM SEG overlay resolved through `ReferencedSOPInstanceUID`; a TID 1500 measurement readout; the existing host-neutral `medos/web/ohif-extension/src/core/*` promoted to the MedicalOS surface API | **The disqualifying property is characterisability, and it is measured, not asserted.** See below. |

### The disqualifying property, stated precisely

`MOS-REL-027` requires "the specific property that disqualified" the incumbent. Three
candidate properties were considered. **Two of them do not disqualify OHIF and are recorded
here as rejected reasons**, because a register row that collected every motive would be
unfalsifiable:

- **Branding / white-labelling does NOT disqualify OHIF.** OHIF is MIT
  (`MOS-SAFE-089a` already records this). MIT permits rebranding, re-theming and commercial
  redistribution; the only surviving obligation is to retain the copyright notice, which is
  satisfied by a third-party-notices file and does not reach the rendered UI. The logo, title,
  favicon and the investigational-use banner are all `app-config.js` values. Anyone reaching
  this row for branding reasons should stop and change the configuration.
- **Licence does NOT disqualify OHIF**, for the same reason, and `MOS-REL-034`'s
  GPL/LGPL/AGPL/BSL in-process-linking rule does not reach the viewer.

- **What DOES disqualify it: the delivered surface cannot be bounded to the requirement
  without a fork, and `MOS-UI-009` forbids the fork.** Measured against the deployed
  `ohif/app:v3.9.2` image and the live archive on 2026-09-21:

  | Measurement | Value | How obtained |
  |---|---|---|
  | Bundle on disk | 54.2 MB over 127 files | `du` inside `medos-ohif` |
  | Transferred to render the study list, before any image | 20.29 MB over 51 requests | `performance.getEntriesByType('resource')` in the running page |
  | Same, were gzip enabled | ≈ 4 MB | main bundle 15.36 MB → 2.85 MB gzipped |
  | WASM image codecs shipped | 6.1 MB | libjpeg-turbo, OpenJPEG, CharLS |
  | Transfer syntaxes actually present in the archive | **Explicit VR Little Endian only**, for every CT, SEG, SR and RTSTRUCT object | file-meta read of every series through the Gateway |

  The codec payload decodes transfer syntaxes the archive does not contain. The same holds for
  slide microscopy, OIDC, 4D and the hanging-protocol engine. Under IEC 62304 §8.1.2 a SOUP
  item must be characterised and its anomaly list assessed; the characterisation cost scales
  with the delivered surface, not the used one, and the surface cannot be reduced without
  modifying the tree. **This is a 62304 argument, not an aesthetic one.**

### The counter-argument, recorded because it is strong

Building does **not** reduce regulatory burden by itself, and this row must not be read as
claiming it does:

1. A first-party viewer is a MedicalOS software item at the clinician surface's safety class,
   carrying the full §5 lifecycle — planning, architecture, unit verification, integration
   testing — where OHIF carried only §8.1.2 SOUP obligations.
2. `MOS-CORE-038`'s stated rationale transfers with it: *"the moment the platform ships its own
   authoring tool it owns a rendering correctness problem it has no evidence for."* That
   evidence must now be produced. `~~MOS-IMG-157~~ MOS-IMG-157a`/`MOS-IMG-158` check that MedicalOS **output**
   renders in a foreign viewer; the inverse — that the first-party viewer renders foreign input
   correctly — has no check today and MUST acquire one before the surface is shipped.
3. The bet only pays if the first-party viewer stays small enough to specify completely. If it
   grows codecs, MPR, 4D or any editing primitive, it becomes a worse SOUP item than the one it
   replaced, authored in-house. `MOS-UI-204`'s prohibition on annotation primitives survives
   this reversal unchanged and is the tripwire.

The decision is taken on the basis that the requirement is genuinely small — uncompressed CT
stack display, SEG overlay, SR readout, one action, one panel — and that transcoding is moved
to the Gateway so that no image codec ever enters the viewer's software item.

### Measured after the build, against the same deployment

The row above was written from measurements of OHIF alone. These are the two surfaces side
by side, on the same nginx, same archive, same browser, rendering the same 148-slice study:

| | OHIF v3.9.2 | MedicalOS Viewer |
|---|---|---|
| Application code transferred | 20 290 KB over 51 requests | **35.2 KB over 8 files** |
| DOMContentLoaded | 1 122 ms | **26 ms** |
| Image codecs in the delivered surface | 6.1 MB WASM (libjpeg-turbo, OpenJPEG, CharLS) | **none** |
| Redraw after a window/level change | re-encode and re-upload | **0.58 ms**, two shader uniforms |
| SOUP items under IEC 62304 8.1.2 | OHIF, Cornerstone3D, dcmjs, three codec libraries | **none** |

**The number that goes the wrong way, recorded because it does.** Pixel transfer for that
study is 81.4 MB, because the archive stores uncompressed Explicit VR LE and this viewer
asks for it unchanged. OHIF against a compressed archive would move a fraction of that.
This is the direct cost of the codec decision and it is a real trade, not a free win: the
viewer's software item is smaller and the network's job is larger. It is acceptable on a
LAN beside the PACS, which is where `MOS-CORE-037` says the platform installs, and it is
the first thing to re-examine if this viewer is ever put on the far side of a WAN.

**What is still owed before this surface may ship.** The counter-argument above is not
discharged by any of these numbers. `~~MOS-IMG-157~~ MOS-IMG-157a`/`MOS-IMG-158` check that MedicalOS
*output* renders in a foreign viewer; the inverse -- that this viewer renders foreign input
correctly -- has no check yet. Until it does, the honest status of the first-party viewer
is that it is fast, small and unverified.

### The reversal is larger than one requirement id — found after the row was written

The Viewer row above withdraws `MOS-CORE-038`. That is not sufficient, and the shortfall is
recorded here rather than discovered later. Chapter 19 §19.4 anticipated this exact request
in detail, and **four further requirements bind**:

| Requirement | What it says | Status against `viewer/` |
|---|---|---|
| `MOS-UI-202` | Five rows MUST be appended **verbatim** to this file, among them **"A first-party viewer or annotation tool — REFUSE"** and **"RadiAnt — REFUSE"** | **Those rows are absent from this file entirely** — a pre-existing gap. When added verbatim, the REFUSE row contradicts the Viewer row above head-on. One of the two must go. |
| `MOS-UI-200` | The RadiAnt preference "MUST be satisfied as an ergonomics requirement **over the adopted surface**" and "MUST NOT be satisfied … by building a look-alike" | The viewer implements RadiAnt's mouse model and preset set on a **first-party** surface, not over the adopted one. §19.4's scope is the *annotation* surface, so the conflict is arguable rather than flat — but it is not nothing, and it must be ruled on, not assumed away. |
| `MOS-UI-205` | "a second viewer MUST NOT be deployed alongside the first" | The compose stack now serves OHIF and this viewer on one origin. Directly contrary. |
| `MOS-UI-009` | MUST NOT "implement image decoding, windowing, stack scrolling, MPR, or any viewport rendering of pixel data" | `src/render/viewport.js` does windowing and stack scrolling. Directly contrary. |

**A claim in this file's first draft was wrong and is withdrawn.** The compose mount was
justified by `MOS-IMG-158`'s second-viewer gate. `MOS-UI-202`'s OHIF row states the opposite
in as many words — that gate is "designed to detect, not to institutionalise" two renderings
of the same SEG. The mount stands on a different and weaker footing: the first-party viewer
has no rendering-correctness evidence yet, and retiring the incumbent before it does would be
worse than the tension with `MOS-UI-205`.

**`MOS-UI-201` applies to this row.** Every capability claim here was verified by execution on
2026-09-21 against `ohif/app:v3.9.2` and the live archive, and the measurements name their
method. The one claim that is NOT verified is the one that matters most — that this viewer
renders foreign input correctly — and it is recorded as unverified above.

### What happened to those four conflicts — specification 0.4.0

The table above was written with all four open and none of them owned. All four are now
disposed of, in the chapter that states them rather than here, and the dispositions are not
uniform — which is the part worth reading.

| Requirement | Disposition at 0.4.0 |
|---|---|
| `MOS-UI-009` | **Withdrawn at 0.3.0**, replaced by `MOS-UI-009a`. Register entry 103. |
| `MOS-UI-205` | **Withdrawn**, replaced by `MOS-UI-205a`. Its premise, not merely its authority, is gone: no second viewer is deployed. |
| `MOS-UI-202`'s REFUSE row | **Moved**, which is what this file said had to happen — "One of the two must go". The row now reads "A first-party **annotation authoring** tool — REFUSE", because `MOS-CORE-045` is bounded and not reversed: building a viewer became permitted, producing an `AnnotationSet` did not. |
| `MOS-UI-200` | **Re-pointed** from "over the adopted surface" to whichever surface the reader works on. The four disqualifying properties of RadiAnt and the prohibition on building a look-alike are untouched. |

Register entry 106 records why this took a second pass: entry 103 closed on `MOS-UI-009`
alone and left the other three standing, and they were found by scoping the removal of OHIF
rather than by reading the register — the wrong direction to find a contradiction from.

### And the paragraph above it is overtaken — recorded, not edited

The "different and weaker footing" paragraph says the compose mount stands because "the
first-party viewer has no rendering-correctness evidence yet, and retiring the incumbent
before it does would be worse than the tension with `MOS-UI-205`". **The incumbent has been
retired.** The compose stack no longer runs `ohif/app:v3.9.2`; the service is a plain nginx
and the browser origin serves `viewer/` alone. The decision was the owner's and it is
recorded as such.

**What that decision did NOT do is discharge the evidence burden**, and this file is where
that has to stay visible, because the sentence it is displacing is this file's own. The
position is now:

- **The obligation moved rather than lapsed.** `MOS-IMG-157` was a pinned-OHIF rendering
  check; it is re-pointed at a pinned independent viewer — `MOS-IMG-157a` — drawn from the
  `MOS-REL-027` incumbent list, because a check whose renderer under test is also its oracle
  proves nothing. `MOS-UI-013b` is widened the same way and keeps its "has not been run"
  admission verbatim.
- **Two things were gained that did not exist on 2026-09-21.** Executable geometry evidence
  (`viewer/tests/js/planes.mjs`, `regions.mjs`, `cursor.mjs`, `cross_series.mjs` drive the shipped
  modules against real DICOM geometry), and exercise against foreign data — nineteen studies
  from three manufacturers, 9,430 instances, implicit-VR CT, reconstructed MPR series and
  DWI. Neither is a pixel comparison, and neither is claimed to be one.
- **The fallback is undeployed, not destroyed.** `ohif/app:v3.9.2` is a pinned tag and stays
  in this repository's history; it can be raised for a one-off comparison whenever
  `MOS-IMG-157a` is executed. What 0.4.0 removed is the deployment, not the possibility.
- **One mechanism broke with the host and is recorded rather than quietly re-pointed.**
  `MOS-UI-002`'s machine check — chapter 14's AT-08 plane P4 and mutant M3 — probes a
  container named `ohif` that the stack no longer starts. The MUST is unchanged; its evidence
  is not. A check that names a container nothing starts does not fail when the property is
  violated, it fails to run, which is the worse of the two. Register entry 107.

---

## A copyleft finding that is not about the viewer at all

The licence question that prompted this work pointed at OHIF, where there was nothing to find.
There is something to find one layer down, and it is recorded here because `MOS-REL-027` is
where licence consequences in this repository live.

**`MOS-REL-034`**: "No GPL-, LGPL-, AGPL- or BSL-licensed component MUST be statically linked,
dynamically linked, vendored into, **or loaded in-process by any first-party MedicalOS
binary**."

| Component | Licence | Where | Verified |
|---|---|---|---|
| **`psycopg` (3.x)** | **LGPL-3.0** | imported in **44 modules under `medos/medos/`**, in-process, in every first-party binary | `importlib.metadata` inside the running `medos-api` container, 2026-09-21 |
| Orthanc DICOMweb plugin | **AGPLv3+** | separate container, HTTP only — compliant | §15.3.1's PACS row calls Orthanc "GPLv3"; the plugin this stack loads is AGPL, which carries network-service obligations GPL does not. **The spec's licence label is wrong and should be corrected.** |
| MinIO | AGPLv3 | separate container, S3 wire API only — compliant | §15.3.1 already records this correctly |

**The psycopg row is a live contradiction between the repository and its own rule, and the
rule is probably the thing that is wrong.** The LGPL exists precisely to permit dynamic use by
non-LGPL software; a Python import is that. `MOS-REL-034` as written also forbids a large part
of the Python ecosystem and its own worked example — "a PACS in-process callback plugin" — is a
GPL concern, not an LGPL one. The likely correct fix is to narrow `MOS-REL-034` to GPL/AGPL/BSL
and state the LGPL conditions separately, not to replace the database driver. Either way it
MUST be decided rather than left standing, and `MOS-REL-038`'s licence-gated SBOM is the
artefact that would have caught it.

### Correction: chapter 19 §19.4.4 does not bind the clinician surface

The conflicts table above lists `MOS-UI-200` as an arguable conflict. That reading is now
resolved, and it resolves the other way: **§19.4.4 does not reach this viewer at all.**

`MOS-UI-200` sits in §19.4.1, whose subject is the annotation surface, and `MOS-UI-213`
binds the whole of §19.4.4 — `MOS-UI-207` through `MOS-UI-215` — to "OHIF configuration or
a module contributed from the extension package". `viewer/` declares itself
`clinical_viewer`, so none of that subsection binds it.

Two consequences, and the second is the uncomfortable one:

1. `MOS-UI-208`'s preset-file requirement and `MOS-UI-210`'s keystroke set were recorded
   earlier in this session as requirements this viewer met or owed. They are neither. They
   remain good engineering targets that were adopted deliberately, and `presets.json` is
   better for carrying the source of its values either way — but the work was not
   compliance and must not be counted as such.
2. `MOS-UI-009` is untouched by any of this and still forbids the clinician surface to
   implement windowing, stack scrolling, MPR or any viewport rendering of pixel data. It is
   the one requirement that actually binds, it has not been withdrawn, and every feature
   added to this viewer deepens the amount that a withdrawal would have to cover.

**This is the third time a §19.4.4 requirement has been cited as authority for building
something here** — `mpr.js` for `MOS-UI-211`, `sync.js` for `MOS-UI-200`, and the preset
work for `MOS-UI-208`. The pattern is worth naming: a requirement that describes what this
viewer should do is not thereby a requirement that binds this viewer, and reading it as one
manufactures permission that chapter 19 never gave. The honest register entry for every one
of these features is "chosen, and forbidden by `MOS-UI-009` until that is withdrawn".

---

## OQ-10 resolved: the inference runtime IS replaceable, and Triton is one adapter

`docs/spec/16-open-questions.md` carries **OQ-10 / MOS-OPEN-031**: *"Is the inference
runtime declared replaceable the way the PACS is?"*, owner spec-owner, due at gate
`G-0.3.0`. Its default was "declare the `InferenceBackend` port and keep Triton as the only
shipped driver through 0.3.0". `medos/medos/inference/backend.py` was written against that
default and says so in its own header: *"This file therefore introduces a PROVISIONAL seam
and says so plainly … It does NOT decide OQ-10."*

**It is decided now, toward replaceability.** The direction is the product owner's, and the
question's own branch table already argued it:

> Triton mandated → *"a hospital with a different accelerator or a CPU-only site cannot be
> served; contradicts the neutrality claim of Ch. 1."*

### What the decision commits to

| | |
|---|---|
| `InferenceBackend` is | a **settled** port, not a provisional seam. `MOS-OPS-008`'s "MUST NOT be treated as resolved by any chapter" is discharged by this row. |
| Triton is | one adapter, first-class and fully supported, with no privileged position in the domain layer. |
| The domain layer | contains no Triton call, no Triton type and no Triton-shaped error. `medos/medos/worker/steps.py` holds the port, never a client. |
| A backend conversion | still mints a new `ModelVersion` and still requires its own `EvaluationRun`. **This half of MOS-OPEN-031's default survives unchanged** — conversion changes numerics, and the artefact you validated is not the artefact you serve unless you say otherwise. |

### The constraint that shapes the adapter registry

`MOS-REL-108` forbids "an in-process plugin API — no shared-library loading, **no dynamic
module import**, no user-supplied code executed inside a platform process", and
`MOS-CONF-109` leans on that clause as the IEC 62304 §4.3 segregation argument. So the
registry **MUST NOT** resolve a backend name through `importlib`.

This repository has already solved exactly this problem once, and the solution is the
pattern to follow: `medos/services/catalogue.py` is a static mapping of selector name → module,
one ordinary top-level import per shipped provider, with `medos/medos/capabilities/providers.py`
doing a dictionary lookup and refusing an unknown key by listing the known ones. The set of
code a configured deployment can execute is the set a reviewer can read with `grep import`.
An inference adapter catalogue must have the same shape, for the same reason, and an
operator selects among shipped adapters rather than introducing one.

### What this does *not* decide

- **Which adapters ship.** TorchServe, ONNX Runtime, TF Serving, OpenVINO Model Server,
  vLLM and a generic HTTP/gRPC adapter are named as targets. Each needs its own row here
  when it lands, with its client library's licence checked against `MOS-REL-034` — psycopg
  is already an open LGPL contradiction in this repository and a second one should not be
  acquired by accident.
- **That every model runs on every backend.** The artifact manifest declares
  `supported_runtimes`; a deployment that selects a backend the artifact does not declare
  must be refused at configuration time, not discovered at inference time.
- **Where conversion happens.** `MOS-OPS-070` puts manifest production in packaging and
  `MOS-OPS-071` requires that the serving platform be *unable* to perform a backend
  conversion. So ONNX → TensorRT, or ONNX → OpenVINO IR, belongs in `medos-train` or a
  packaging tool, and never inside `medos/medos/inference`. A platform that can build a model can
  build one at load time, which is what `MOS-OPS-071` forbids.

### The spec amendment this owes

`docs/spec/16-open-questions.md` still lists OQ-10 as open and `docs/spec/15-delivery.md`
§15.2.9 still labels the seam "proposed, NOT settled". Both must be updated to point here,
and `MOS-OPS-008`'s "not settled by this chapter" sentence retired. Until that lands, this
row is the decision and the spec is stale — recorded that way round rather than pretending
the chapters already agree.


---

## Chapter 17 — the training-plan model and the pipeline port (`MOS-REL-032`)

`MOS-REL-032`: "A first-party component MUST NOT re-implement functionality available in an
adopted dependency in order to avoid the dependency, **unless a register row records the
reason**." This is that row. `MOS-REL-027` additionally requires that a row recording "build"
name at least one incumbent that was considered and the specific property that disqualified it,
and both are below.

**What this row does NOT propose.** nnU-Net's planner, its preprocessing, its augmentation
pipeline and its inference windowing stay ADOPTED, exactly as the `MOS-TRAIN-019` and
`MOS-TRAIN-210` rows above record them. Re-deriving the planner's heuristics would mean
re-deriving a calibration fitted across many datasets, and `MOS-REL-032`'s acceptance criterion
for a first-party re-implementation — matching the published one — would make our own test
"reproduces nnU-Net's plan", which is weeks spent to arrive where we started. The scope here is
narrower and is named precisely: the layer through which this platform READS, CONSTRUCTS and
MODIFIES a plan.

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **Training-plan model and pipeline port** — incumbents considered: nnU-Net's `PlansManager`/`ConfigurationManager`; MONAI Bundle `configs/` as the plan carrier; MONAI `Auto3DSeg` `BundleGen` configurations; a validate-only JSON schema over the raw document; and doing nothing, i.e. continuing to index the document by string | Apache-2.0 (incumbents) | **BUILD, for the model and the port only** | nnU-Net's plans DOCUMENT FORMAT, verbatim and as the interchange form, together with everything that derives it; `PlansManager`/`ConfigurationManager` remain the readers on nnU-Net's own side of the boundary | Nothing is forked. The nnU-Net document is transcribed to a typed model and back, and the transcription is the wrap | A typed `Plan` whose round trip to the document is LOSSLESS including keys with no consumer; construction and modification as first-party operations rather than string indexing; the architecture overlay with its own record; the configuration as a first-party choice rather than a module constant; and a departure record so a run states which of its settings are not the planner's | **The disqualifying property is measured, not asserted: `PlansManager` is a read-only accessor and there is no write path at all.** It exposes twelve read-only properties over `self.plans`, which is a public mutable plain `dict` (`plans_handler.py:226`), and offers no constructor from typed fields, no validation, and no modification API. Anything this platform must CHANGE — the architecture, the configuration, a recorded departure — can therefore only be changed by indexing that dict, which our tree does in seven files. Two consequences are already realised and neither is visible in any test: the configuration name is a module constant in two files while `plan.json` records a `configuration` member that nothing reads back, so a plan recorded for one configuration is fit under another; and swapping an architecture is done by bind-mounting a patched copy of the document over the read-only original. **The round-trip requirement is also measured:** `nnUNetTrainer.py:922` and `predict_from_raw_data.py:244,713` write the whole document verbatim into the results folder for inference to read back, so a model that dropped a key with zero consumers would break inference for a run whose training looked perfect. A validate-only schema was the closest incumbent and was rejected for the same reason: it can refuse a bad document but cannot produce a good one. |

**What this row commits to, in order, so that independence arrives without a flag day.** Each
step is usable on its own and none invalidates preprocessed data:

1. the typed `Plan` with a lossless round trip, gated on the real plan fixtures;
2. the pipeline port widened from five operations to the named stages — fingerprint, plan,
   preprocess, fit, infer, evaluate — with first-party types at the boundary;
3. the configuration as a value of that port rather than a module constant, which is also what
   admits `2d`, `3d_lowres` and the cascade;
4. a first-party training loop BEHIND the port, so it is a substitution and not a rewrite, with
   nnU-Net's own loop remaining as the reference to diff against;
5. a first-party planner, with nnU-Net's as the oracle: the acceptance criterion is "reproduces
   its plan on N fingerprints, and here is where it deliberately differs and why".

Steps 4 and 5 are NOT authorised by this row. Each needs its own row naming what disqualified the
adopted implementation, and step 5's cost is dominated by re-deriving a calibration rather than by
writing code — see the `Neural architecture search` row above for the same arithmetic applied to a
different search.

**Where this lands in the tree.** `trainer/medos_trainer/plan.py` is the typed model and the
transcription; `trainer/medos_trainer/port.py` is the pipeline port;
`trainer/medos_trainer/architectures.py` holds the planner presets, the architecture overlays and
the caveat each overlay records into the plan it patches.

### What this row owes

The register's own header names chapter 15 acceptance criterion 9 as RED until §15.3.1's rows are
transcribed here. This row does not change that: it is a chapter 17 row for a component that is
being built now, and transcribing a row for a subsystem somebody else built in 0.1.0 would mean
inventing a decision nobody recorded — which the header correctly identifies as the failure
`MOS-REL-027` exists to prevent.


---

## Chapter 17 — the training loop and the planner (`MOS-REL-027`, `MOS-REL-032`)

These are the two rows the training-plan row above deliberately did **not** authorise. It said so:
"Steps 4 and 5 are NOT authorised by this row. Each needs its own row naming what disqualified the
adopted implementation." These are those rows, and the disqualifying properties are measured on this
cohort rather than argued.

`MOS-REL-027` requires that a row recording "build" name at least one incumbent considered and the
specific property that disqualified it. `MOS-REL-032` requires a recorded reason before a first-party
component re-implements what an adopted dependency provides.

### The row for the training loop

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **Training loop and training configuration** — incumbents considered: nnU-Net's `nnUNetTrainer` (currently subclassed); MONAI `SupervisedTrainer` / Ignite engines; PyTorch Lightning; a bare PyTorch loop | Apache-2.0 (nnU-Net, MONAI); Apache-2.0 (Lightning) | **BUILD, behind the pipeline port** | The augmentation pipeline's PARAMETERS — 26 fitted constants, transcribed with attribution rather than re-derived, because they are numbers somebody fitted and not rules anybody can re-derive; the sliding-window Gaussian importance weighting; the resampling decision rules including the separate-z path; nnU-Net's own loop remains installed as the REFERENCE to diff against | Nothing is forked. `batchgenerators`' transforms stay as transforms; what is replaced is the loop that calls them and the object that holds the configuration | A declared `TrainingConfiguration` — schedule, loss shape, learning rate, deep-supervision scales — that can be validated and RECORDED before a fit starts; the loop behind the pipeline port, so a second backend is a substitution; the masked per-`(case, channel)` loss, which is already ours and which nnU-Net has no concept of | **Five disqualifying properties, each measured in this repository.** (1) The architecture contract is a hard-written attribute path `network.decoder.deep_supervision`, addressed by that exact chain by three separate writers, plus an output list whose length must equal `len(strides) - 1`; `DeepSupervisionWrapper` pairs heads to targets with `zip`, which TRUNCATES, so a wrong count pairs every head with another scale's target and raises nothing. (2) Training configuration reaches the trainer ONLY by attribute assignment after construction — six settings do this today (`num_epochs`, `num_iterations_per_epoch`, `num_val_iterations_per_epoch`, `save_every`, the focal shaping, the initial learning rate) — so there is no object to validate and nothing to record; two of those six were added in this session precisely because no declared home existed. (3) The learning-rate schedule is calibrated for nnU-Net's own architectures and cannot be varied per architecture: measured, `MedOSSegResNetDS` at 356.2M parameters produces `train_loss` NaN from epoch 0 at nnU-Net's 1e-2, diverges at 1e-3 around epoch 40 after descending to 0.842, and trains stably at 1e-4 — and nnU-Net offers no place to declare that. (4) The loss is constructed inside `initialize()`, which also builds the dataloaders, the augmenters and the optimiser, so anything shaping the loss must be set before one call that serves five unrelated concerns. (5) `get_network_from_plans` re-initialises weights only `if hasattr(network, 'initialize')`, and no MONAI network defines it — so every wrapped network silently receives a different initialisation from nnU-Net's own, and nothing records the difference. |

### The row for the planner

| Component | Licence | Decision | What is **adopted** | What is **wrapped** | What is **genuinely added** | Reason |
|---|---|---|---|---|---|---|
| **Experiment planner** — incumbents considered: nnU-Net's `ExperimentPlanner` and its `ResEnc` subclasses (currently the only route to a plan); MONAI `Auto3DSeg` `DataAnalyzer` + `BundleGen`; hand configuration | Apache-2.0 | **BUILD, with the incumbent as the ORACLE** | The published derivation rules over a four-key fingerprint (`spacings`, `shapes_after_crop`, `foreground_intensity_properties_per_channel`, `median_relative_size_after_cropping`), and the fitted constants they use, `ANISO_THRESHOLD = 3` among them; nnU-Net's planner stays installed as a TEST ORACLE in CI and is not a runtime dependency | The fingerprint computation is used as it stands; what is replaced is the sizing model | A size model that can answer for an ATTENTION architecture; the patch a network is bound to written back into the plan it is built from; stage sequencing, so the cascade the planner already proposes for this cohort becomes reachable | **The disqualifying property is that the planner cannot size two thirds of this catalogue, and that is not an opinion — it is why two wrappers refuse it in code.** The planner divides its VRAM target by `compute_conv_feature_map_size` and compares the ratio against `UNet_reference_val_3d`, a constant fitted on a plain conv U-Net. Measured at this cohort's patch: SwinUNETR's attention matrices are 4.02e9 elements against its own convolution term of 3.78e9, so a conv-shaped estimate underestimates the footprint by about half — in the direction that sizes the patch too LARGE, which surfaces as an out-of-memory error at the first epoch of a queued fit with nothing in the plan to point at. `MedOSSwinUNETR.compute_conv_feature_map_size` and `MedOSUNETR`'s therefore both RAISE rather than return a number the planner would act on. Second, UNETR cannot be resized after construction at all — `proj_feat` freezes the ViT token grid — so the planner's try-a-patch-and-shrink procedure cannot be applied to it, and the patch it settled on would have to be written back into `arch_kwargs`, which nothing upstream does. Third, the planner writes at most four configurations and this platform trains one; the cascade it proposes for this cohort — median image 413×512×512 against a 128×224×224 patch, so no full-resolution network ever sees the chest at once — is unreachable because there is no first-party stage sequencing to run it. |

### What these two rows do NOT claim

They do not claim the incumbents are poor at what they do. The plan that produced this cohort's best
measured result — `vertebral_body` Dice 0.933 [0.93–0.94] at 2.2 % volume error — is nnU-Net's own,
and the row above keeps its derivation rules and its constants. The claim is narrower and is the one
`MOS-REL-032` asks for: the adopted implementations cannot express things this platform has already
been forced to express by hand, and the hand-expression is what these rows replace.

They also do not authorise a FORK of either project, and that is a deliberate exclusion. A fork wins
when a component needs ninety per cent of an upstream and must change ten. Here the ratio is
inverted: of MONAI we use three networks and a handful of transforms, and it is exactly those we
would own, so a fork would hand us the whole of it to maintain while cutting off the upstream fixes
for the parts we do not touch. Named functions whose algorithm is small and hard — the Gaussian
importance map, the `force_separate_z` rule — are COPIED with attribution under Apache-2.0, with the
notice preserved, which is not a fork and is recorded here as the deliberate alternative to one.

### Order, and what is already done

1. the typed `Plan` with a lossless round trip — **done**, `trainer/medos_trainer/plan.py`, 21 gates;
2. the pipeline port widened to named stages — **next**, and it is the prerequisite for (4) rather
   than the luxury an earlier note called it: a first-party loop needs a seam to plug into, and
   without one it plugs into `nnUNetTrainer` again;
3. the configuration as a first-party choice — **done**, and it closed a defect nothing had caught:
   `derive_plan` recorded a `configuration` member that nothing read back, so a plan derived and
   preprocessed for one configuration was fit under whatever a module constant named;
4. the training loop — this row;
5. the planner — this row.
