# Medlange — refinement roadmap

> Recorded 2026-10-02 following the user E2E and the product split.
> Umbrella: **Medlange**. Products: **Core** (SDK, `medos.sdk`), **Trainer**
> (nnU-Net analog), **Viewer** (DICOMweb framework).

## Goal

- **Medlange Core — a full-fledged SDK**: model card → pipeline → adapters
  (PACS / bus / inference) → post-processing → results in the archive. Installed
  via `pip install medos`, configured with a deployment profile, runs in local and
  external (bus-driven) modes.
- **Medlange Viewer — a full-fledged customizable framework in the OHIF class**:
  modular core, documented extension API (panels, tools, actions, protocols),
  configuration without a rebuild, branding, i18n. Developers add functionality
  through stable extension points, not by editing the core.

## Phase U — UI/UX (first priority)

- **U1. Out-of-the-box setup.** The `MEDOS_API_VIEWER_AUTHORIZATION` trap (a raw
  key silently returns 401; a `Bearer ` prefix is required) — either accept both
  forms, or fail with a diagnosis. Goal: `docker compose up` → a single
  `medos doctor`-level scenario that says by itself what to configure, + an
  optional seed of a demo study.
- **U2. Navigation.** Today the analysis lives on a separate page (`/medicalos/standalone/`),
  the "Open the viewer" link opens in the same tab and loses the study context.
  Goal: a single shell — viewer and AI panel as one environment; links open with
  context preserved (study in the URL); breadcrumbs.
- **U3. AI flow inside the viewer.** An "Analyze" button on the study screen (not
  on a separate page), capability selection, a progress indicator, auto-reload
  of SEG/SR as an overlay when ready. ✅ 2026-10-03 (G-U3; the closure of
  capability dependencies via `GET /api/v1/capabilities` and the series cache
  on reload were also closed)
- **U4. DICOM upload via the web UI.** ✅ 2026-10-03 (G-U4): an "Upload DICOM"
  button + drag-drop in the worklist → STOW-RS `{dicomweb}/studies` (multipart/related
  assembled by hand, FailedSOPSequence is shown to the reader); live: LCTSC-Test-S1-201
  from PulmoAI (118 files) → 200 → study appears in the worklist. The credential
  is selected by HTTP method via an nginx map: reads — read-only viewer key,
  POST — uploader key (study.write); if not set — 401 (a deployment decision).
  Requested by the user 2026-10-03.

## Phase V — Viewer as a framework

- **V1. Extension API.** Documented points: panel, toolbar tool, action, layout
  protocol; a stable JS API on top of `core/state`; a plugin example.
- **V2. Configuration without a build.** `viewer.config` (enabled panels/modules,
  theme, branding, routing) — an extension of the current `presets.json` /
  `protocols.json` / `build.json`. ✅ 2026-10-03 (G-V2)
- **V3. Developer documentation.** ✅ 2026-10-03: `docs/getting-started.md`
  ("a panel in 30 lines", steps 1–6), `docs/plugin-template.md` (a production
  template with gates in the comments), `docs/testing.md` (suite anatomy, 5 rules,
  test skeleton); `tests/test_plugin_template.py` runs the gates against the code
  from the docs — the guide cannot silently go stale; dogfood: the panel from the
  guide was mounted in the browser (right rail, "Series") and passed the suite,
  then reverted.

## Phase C — Core as a full-fledged SDK

- **C1. Closing the results loop.** `Pipeline(..., store=...)` → SEG/SR via a
  unified writer (`medos.writer`, lazy adapter import) → `PacsAdapter.store`.
  External mode then stores the results in the archive by itself.
- **C2. Model serving.** Card → staging into a Triton model repository; linkage
  with `ConversionRun`; the canonical path "trainer's trained model onto Triton".
  ✅ 2026-10-03 (G-C2; the blocker of the last step — nvcr.io 403, recorded in
  e2e-staging/g-c2)
- **C3. Deployment profiles.** YAML profile of adapters + `python -m medos.sdk run
  --profile local|mosmed`; publishing `medos` to PyPI. ✅ 2026-10-03 (G-C3; PyPI — not yet)
- **C4. Documentation + examples.** ✅ 2026-10-03: cookbook
  `medos/examples/profiles/README.md` — three live recipes (local → JSON result;
  mosmed/Kafka: KAFKA-MESSAGE → `python -m medos.sdk run --once` → DICOMREPORTNOTIFY
  with genuine output; card→Triton) + a debugging table; gates in
  `tests/unit/test_cookbook.py`; along the way, the direction of `inbound.fields`
  (external key → canonical field) was fixed in the mosmed profile.
- **C5. Resilience (from E2E 2026-10-02).** Fixed: QIDO DICOM JSON Model in the
  PACS adapter. ✅ 2026-10-04: the default pipeline selector is CT-only, every
  exclusion is recorded (`PipelineResult.exclusions`: series_uid, modality,
  reason — derived/non-CT/custom selector); pipeline failures are a dict
  (`PipelineError.code/study_uid/as_dict()`: `no_eligible_series`, `store_refused`),
  the CLI prints the dict; the profile-level `image_only` is an explicit opt-in,
  not a silent default.

## Found along the way (defects and debts)

- **W21, trainer defect (open; the texture theory disproved by recurrence):**
  a deadlock in AMP synchronization (`GradScaler` → `found_inf.item()`, GPU 0%,
  two byte-identical faulthandler dumps 10 minutes apart). First occurrence
  (2026-10-11 ~00:20, full-parity): producer in gaussian-blur conv3d.
  Second occurrence (2026-10-11 ~01:40, INTENSITY-ONLY — the "safe" config):
  producer in `queue.put` backpressure, same main-thread stack →
  the producer is collateral, the hang is the device sync itself. Suspect:
  AMP scaler × torch 2.14.1+cu130 × RTX 5090 (all pre-AMP runs completed,
  both hangs at the scaler site, non-deterministically). Mitigation:
  W21b (blur on scipy — kept, the better implementation) + seed-0 restarted
  with fp32+TF32/native allocator without GradScaler; seed 1 remains on AMP as
  the comparison arm. Root cause not proven: minimal repro + bug report to torch
  — open; do not enable AMP on this machine until repro. Report: W21/W21c.
- **OHIF extension** (`medos/web/ohif-extension`) sends a single `target` —
  selecting `emphysema_laa` still fails there; it needs the same descriptor call
  as in the native viewer (core).
- **Tracer defect:** `instance_norm` in shipped bundles is baked with `train=True`
  (the ONNX conversions are correct, the flag reaches the original trace; it
  affects the serving numbers). File it in trainer as a bug item.
- **Triton unblock:** `docker login nvcr.io` (or a mirror of the image
  `nvcr.io/nvidia/tritonserver`) — after which the `inference` profile comes up
  without any code changes.

## Phase T-vanilla — Trainer without MONAI and nnU-Net (owner decision 2026-10-04)

The Trainer is being rewritten on its own stack (pure PyTorch): the goal is a
self-standing framework that people adopt on its own merits, not a derivative of
someone else's. Basis — the audit `docs/audits/trainer-2026-10-04.md`. Steps:

- **T1. Networks from scratch:** our own 3D UNet (stem strides from the plan,
  instance norm, deep supervision) instead of wrappers over MONAI architectures;
  overlays are no longer "written but not hooked up" — this is the only path.
- **T2. Data from scratch:** a case loader (NIfTI/native format), weighted patch
  sampling, augmentations (mirror/rotate/scale/intensity) — our own, without the
  nnU-Net pipeline.
- **T3. Training:** our own loop (masked region loss is preserved — the signature
  subsystem), LR plateau, checkpoints, determinism (the existing environment/stamp
  move over as-is).
- **T4. Inference from scratch:** sliding window with Gaussian blending —
  immediately closes both the external predict CLI and the known kserve_v2 gap
  in core (no reverse patch→source mapping).
- **T5. Planning:** fingerprint→plan (patch/spacing/batch sized to VRAM) — our
  own logic instead of nnU-Net plans.
- **T6. Standalone onboarding:** a run-dir generator from the nnU-Net dataset
  structure + `trainer/examples/` — an external researcher gets to a trained
  model without the platform.
- **T7. Cutting away the old:** nnunetv2/monai are removed from requirements and
  imports; README/CI/docs are brought in line with reality ("4 modules/54 tests"
  → the actual numbers).

Order: T1→T2→T3 (vertical slice on synthetic data, ✅ 2026-10-04) → T4 ✅ (push
7c296a3, 2026-10-05: sliding window + served-twin + predict CLI) → T5 ✅ (push
8c0209d: fingerprint→plan with reasoned decisions) → T6 ✅ (push
99d68af+2e7c2ee: vanilla-plan/vanilla-fit/vanilla-import-nnunet +
examples/toy_pipeline.py) → T7 ✅ (push bb6d6ac: −14,373 lines, nnunetv2/monai/
medos.sdk fully removed from the trainer; suites: trainer 127 tests CPU,
monorepo 1542 passed). Parity phases (✅ 2026-10-06/07): ensemble predictor +
ensemble evaluation in crossval (W11), poly-LR (W12), coarse→fine cascades
(W13), DDP via torchrun (W14) — push bb520a5, 174 tests. Benchmark on a real
dataset (W15, docs/benchmark-pulmo-2026-10-07.md): 20 hydrothorax cases
(PulmoAI), one split/budget, one evaluator — nnU-Net 0.764 fg Dice vs Medlange
0.000 at 5 epochs. Found and fixed along the way: per-preset physical patch
(8× GPU context deficit), rot90 for non-square patches, --batch-size
override, --device for evaluate, pytest pythonpath. CONCLUSION: W16 —
intensity normalization from fingerprint statistics (z-score as in nnU-Net
CTNormalization), resampling to the median spacing, dataloader workers, restart
at 100 epochs. Only after W16 does the claim "better than nnU-Net" make sense
to verify with a repeated benchmark.

The platform run-dir contract (plan/fit/execute via medos.sdk) is deliberately
cut — integration of the vanilla backend with core (autoconfig mapping,
modelcard) is a separate phase after C-next, if the owner decides to bring back
the platform training path.

- **T8 (owner proposal 2026-10-04): Triton kernels** (OpenAI Triton, GPU) —
  an optional accelerator of architectures: fused conv+norm, fused softmax-Dice.
  The pure PyTorch path remains the reference: CPUs and GPU-less machines must
  be able to train and run inference without Triton; the kernels sit behind
  capability detection and a plan flag. Mind the homonym: this is NOT the
  NVIDIA Triton Inference Server (that one is in core/C2).

## Phase V-next — Viewer as a platform (per audit docs/audits/viewer-2026-10-04.md)

1. OVERLAY: implement the consumer or remove the view from the registry.
2. Network seam: export the configured DicomWebClient (facade in src/core),
   pass through authHeaderProvider from viewer-config.js — secured PACS without
   proxy injection.
3. Plugin manifest (viewer.plugins.js) — registration without editing app.js;
   a gate on tool hotkey collisions.
4. Fix docs/extensions.md (the real TOOL API) + the API reference (state,
   contribution points, ctx, CSS tokens, JSON schemas).
5. An honest story about transfer syntax (uncompressed-only in README/getting-started;
   an optional WASM codec behind a flag — as a separate decision).

## Phase C-next — Core as a production SDK (per audit docs/audits/core-2026-10-04.md)

1. Bus resilience: ExternalWorker does not commit the offset on CodecError/
   failure without an error topic; backoff in run_forever; poison → error topic
   or DLQ.
2. Write idempotency: thread expected_idempotency_key through to PlatformWriter
   (currently always None — duplicate SEGs on rerun).
3. Card 1.1: the trainer writes structure/concept_key; a public JSON schema of
   the card in medos/schemas/; weight digest verification in ModelCard.load.
4. A real kserve_v2 path: client-side sliding window + reverse mapping into the
   source grid (trainer T4 provides the reference implementation); e2e against
   the Triton repository from deploy_model.
5. Multi-model support (routing by the request's model_id or refusal on
   mismatch), observability (logging/metrics), PyPI publication, SDK-only
   packaging.

## Migration to the medlange repositories

Status 2026-10-04: the `github.com/medlange` organization is set up —
`medlange/{core,trainer,viewer}` (public, `main`, commit author ATMZR) and the
`medlange/.github` profile are created and pushed; clean clones verified
(viewer: 252 tests; core/trainer: imports). Product audits are recorded in
`docs/audits/`. The `MedOS` monorepo remains the source of truth for
development; sync to the splits → push is the working cycle until the parked
work of the peer session is sorted out.

## Goals (measurable, verifiable)

- **G-U3 (the user's main pain point 2026-10-03):** a radiologist opens a study
  in the viewer and runs an analysis without leaving the viewer: an "Analyze"
  button → a dialog with model selection from the configuration → progress →
  the result as an overlay. Due: 2026-10.
- **G-V1:** a third-party developer adds a panel/action with one module + one
  registration, without editing `app.js`; a plugin example and documentation
  ≤30 lines per contribution. Due: 2026-10.
- **G-C1b ✅ (2026-10-03):** `Pipeline(writer=)` stores SEG/SR in the archive
  via the unified `medos.writer` (card schema 1.1 with concept codes); external-mode
  e2e without manual steps.
- **G-U1 ✅** the raw credential key works out of the box (verified 200).
- **G-V2 ✅ (2026-10-03):** `viewer.config.json` — panels/theme/branding/routing
  (`?study=` deep link) without a rebuild; the default ships in the tree, the
  deployment mounts over it (the Medlange palette live in the browser).
- **G-C2 ✅ (2026-10-03, with the external blocker of the last step recorded):**
  `tools/deploy_model.py --modelcard <run> --repo <repo>` — the trainer's card
  (`medlange.modelcard/1`) → Triton model repository in one command: ONNX export
  (`packaging.onnx_bytes`), digest verification, gates MOS-OPS-071-075 by
  construction, warmup from a golden fixture. Run on a trained model
  (control-585): a real Triton 2.51 accepted the repository/config/manifest,
  live inference of the artifact is bit-identical to local ORT. Blocker: the
  `onnxruntime` backend for Triton lives only inside the nvcr.io image (403
  without NGC login) — `unable to find backend library for backend
  'onnxruntime'`. Evidence: `D:/PycharmProjects/e2e-staging/g-c2/EVIDENCE.md`.
- **G-C3 ✅ (2026-10-03):** `python -m medos.sdk run --profile <yaml>` — a YAML
  profile (closed schema: card/pacs/inference/writer/mode, secrets from env,
  failures as a profile dict) + a CLI with batch discipline; profiles
  `medos/examples/profiles/{local,mosmed}.yaml`; e2e on the live stack: LCTSC
  S1-104 from `F:/WorkSpace/PulmoAI` → 1 segment, exit 0.
- **G-U4 ✅ (2026-10-03):** DICOM upload via the viewer UI: S1-201 (118 files
  from PulmoAI) → STOW-RS → 200 → worklist 23→24; credential by method (reads
  read-only, POST — uploader); 8 gate tests, viewer suite 244 green.

## Order

U1 → C1 → V1 → (U2, U3) → V2 → C2 → C3 → U4 → V3 → C4 → C5 — ✅ all closed
(2026-10-03/04). Next: debts (OHIF closure, instance_norm in the tracer,
nvcr-login for Triton, PyPI publication of medos) — as their area is touched;
the next planning phase is up to the user.

## The "is it harder than in OHIF?" question — the answer

Inside the viewer repository, development is simpler than in OHIF (vanilla
ES modules, no build, 217 executable architecture tests). But OHIF has what
the viewer lacks: a stable third-party extension API (modes/extensions) and an
ecosystem. Phase V closes this gap — that is exactly the "framework like OHIF".
