<!-- SPDX-License-Identifier: Apache-2.0 -->
# Development

> MedicalOS integrates, governs and evidences medical AI services. It does not diagnose,
> does not replace a PACS, and is not itself a medical device.

## Toolchain

| | |
|---|---|
| Python | 3.11 (`CONTRACT.md` §11, `pyproject.toml` `requires-python`) |
| numpy | 1.26.4 in the trainer image — **pinned, and load-bearing**: `medos/medos/training/fixtures.py` records two byte-exact hashes under "numpy 1.26.4, CPython 3.11" that `MOS-TRAIN-133` compares against, so a different minor version makes the golden fixture a different fixture |
| Postgres | 16, via compose |
| torch / nnU-Net | `2.5.1+cu124` / `2.5.1`, **in the trainer image only** — the platform carries neither (`MOS-TRAIN-225`) |
| Lint | ruff, line length 96 |

## Setting up

```bash
python -m venv .venv && . .venv/Scripts/activate   # Linux/macOS: . .venv/bin/activate
pip install -e ".[dev]"
```

**Use the virtualenv's interpreter for everything below.** This is not a style preference.
A shell whose `python` resolves elsewhere — an Anaconda base environment is the common case
on Windows — does not fail the suite, it fails to *collect* it, and seventeen
`ModuleNotFoundError` lines read like a broken checkout rather than a wrong interpreter.
If you are unsure, `python -c "import sys; print(sys.executable)"` settles it.

`pip install -e ".[dev]"` is sufficient for `tests/unit` and `tests/gate`, and
`tests/unit/test_declared_dependencies.py` is what keeps that true: it derives every
third-party import from the source and fails if one is undeclared. It exists because
`scipy` was missing from the declarations for a long time while every developer machine
happened to have it — and `scipy` is what computes the hash behind the MOS-EVID-034
leakage check, so the first person to install cleanly would have met it inside a safety
gate. The `tools` extra (`pip install -e ".[dev,tools]"`) adds what `medos/tools/` needs.

## Running it

```bash
docker compose -f medos/deploy/compose/docker-compose.yml up -d
python -m medos.cli doctor
```

```bash
pytest                           # everything testpaths names -- ~2590 tests
pytest tests/unit tests/gate     # no containers needed
pytest viewer/tests              # the viewer's own suite; reads viewer/ and nothing else
pytest trainer/tests             # needs the torch / nnunetv2 pins installed locally
pytest tests/integration         # needs the compose stack
pytest -m gate_0_1_0             # a named release gate, and only it
```

**Zero skips is the standard.** Every run prints a skip taxonomy at the end, and a skip is
a test that did not run. `skip_infra` is for a genuinely absent container and nothing else
— it exists so that `--require-stack` can turn those same skips red in CI.

Note `nnUNetv2_train` **exits 0 on failure.** Check the log, never the exit code.

---

## Where development data comes from, and how it is de-identified

`MOS-REL-095` requires this section, and for this platform it is the section a reviewer
reads first.

### What ships in the repository

**No images.** Every volume the suite uses is generated at run time. There is no patient
behind any of them, so no de-identification claim about them can be false.

Two different generators, and the distinction matters when you go looking for one:

| | |
|---|---|
| Synthetic **DICOM** | `tests/unit/test_capabilities.py` and `tests/integration/test_dicomweb.py` build series with `pydicom.uid.generate_uid()`, so every identifier lands under the `1.2.826.0.1.3680043.8.498.` arc (pydicom's registered root). This is what `tests/integration/test_deid_provenance_declaration.py` recognises by root. |
| Synthetic **NIfTI** | `tests/_support/phantom_cohort.py` writes `image.nii.gz` / `label.nii.gz` pairs in the `directory` stager's layout, for the training path. It mints **no UIDs at all** — the string `UID` does not occur in the file — because nothing in that path needs one. |

This paragraph named `tests/_support/` as the generator working under the pydicom arc
until somebody checked. It writes NIfTI and mints nothing, and it is the wrong file to
send a reviewer to in the one section `MOS-REL-095` says a reviewer reads first.

### What a developer may add, and what happens if they do

Nothing prevents you STOWing your own studies into the dev stack. Two things then apply:

1. **`tests/integration/test_deid_provenance_declaration.py` goes red** the moment the
   archive holds a study that no declaration covers. `MEDOS_DEID_PROVENANCE` in
   `docker-compose.yml` asserts something about *data*, and ingesting an undeclared study
   makes it false — permanently, because `MOS-EVID-021` writes it into an immutable row
   that every downstream `ValidationReport` cites. The red suite is the point.

2. **You must declare the corpus.** `medos/tools/ingest/nrrd_to_dicom.py --manifest` writes a
   manifest naming every study it produced, its de-identification status, the policy and
   UID-mapping ids behind that status, and who holds the legal basis. Point
   `MEDOS_PROVENANCE_DIR` at the directory holding it.

### The tools, and what each will and will not do

| | |
|---|---|
| `medos/tools/deid/scrub_slicer_scenes.py` | Dry run by default; `--apply` required. HMACs identifiers under a salt it generates and warns about. **It does not make data safe** — it removes what it can recognise. |
| `medos/tools/ingest/nrrd_to_dicom.py` | NRRD → DICOM CT + SEG. Mints its own UIDs because `MOS-IMG-069` forbids the platform to. Records in `known_limitations` that the source UIDs are gone, so the same person exported twice appears as two unrelated patients and `MOS-EVID-034` L1 cannot see it. |
| `medos/tools/ingest/audit_dicom_phi.py` | Audits the **artefact**, not the argument. Every PS3.15 E.1-1 tag by number, every element by pattern, every UID against the declared root. Prints tag names and counts, never values. |
| `medos/tools/ingest/channel_map_stub.py` | Lists every raw segment name a corpus contains and **proposes nothing** — every `channel` comes out null. |

### One measured example, because the general statement is not much use

For `NRRD_DATASET_LUNG_CANCER` (100 cases, 200 files) the scrubber reported 0 findings and
0 residual PHI. That zero was **not** taken on trust: an independent enumeration of every
header key across all 200 files found nothing outside the geometry and Slicer-segmentation
families, and after conversion `audit_dicom_phi.py` over 1,500 written DICOM files
reported only the four surrogate-or-empty identity tags, no pattern hit, and no foreign
UID.

The identifiers in that collection are in the `.mrml` Slicer scene files — which the
converter never opens, and which are never converted. That is why the conversion path is
clean, and it is a fact about which files are read rather than a property of the tooling.

**None of this makes a corpus safe to publish.** It makes one corpus's conversion path
auditable, and the audit tool is checked in so you can run it on yours.

---

## The rest of `medos/tools/`, which the table above does not cover

That table is about data handling — the four tools that touch a corpus. There are nine
more, and until this section existed **five of them appeared in no document at all**:
`capability_cohort_report.py`, `publish_model.py`, `medicalos_verify.py`,
`ingest/stratified_split.py` and `trainer/resume_preprocess.py`.

`medos/tools/` is deliberately NOT part of the installed distribution — `pyproject.toml`
excludes it, and `MOS-REL-107` is why a capability's code is somebody else's. So these run
from a checkout, with `pip install -e ".[dev,tools]"`.

### Checking the repository against its own specification

| | |
|---|---|
| `medos/tools/permcheck.py` | Chapter 8 acceptance check 5, as far as this repository can state it: every identifier in `medos/contracts/permissions.yaml` matches `MOS-SEC-031`'s grammar and carries one of the six classes, and `permissions:` is EXACTLY §8.3.2's table. `--emit` regenerates `permissions.generated.json`; without it, exit 1 and every finding printed. |
| `medos/tools/contracts.py` | The readers for the two normative contract files and the spec tables they mirror. Everything above and several gates read the spec through this, so a table that moves is one parser to repoint rather than twelve greps. |

### Producing and checking artifacts

| | |
|---|---|
| `medos/tools/publish_model.py` | Chapter 6's packaging step in miniature. `build` / `publish` / `verify` are separate subcommands on purpose: `MOS-OPS-070` puts `medicalos.json` production in packaging and `MOS-OPS-071` requires the serving platform to be unable to perform it, so a deployment must be able to publish artifacts it did not build. `build` needs `onnx`; the other two need only the pinned runtime set. |
| `medos/tools/deploy_model.py` | The G-C2 command: a trainer run's `modelcard.json` → a §13.10.1 Triton model directory (`--modelcard <run> --repo <repo>`). Refuses a TorchScript-only card, a digest mismatch, or a golden fixture that disagrees with the card — the normative serving artifact is ONNX and the server must not convert (`MOS-OPS-071`). Writes through `medos.inference.repository`, so the refusal gates are satisfied by construction. |
| `medos/tools/medicalos_verify.py` | `medicalos-verify` — checks a ValidationReport bundle **with no MedicalOS in sight**, which is the whole point: chapter 7 §7.12.3 requires a reader to be able to verify a report without trusting the platform that wrote it. Seven numbered steps to a `VERDICT`, and it says out loud that revocation status is NOT checked offline. `MOS-EVID-124` forbids a socket anywhere in its import closure. |

### Reading what the platform actually did

| | |
|---|---|
| `medos/tools/capability_cohort_report.py` | The proof harness for `medos/medos/capabilities/`. **It is not a test** — it asserts nothing. It runs every registered capability over a real CT cohort exactly as `worker/steps.py` will (registry → `execution_order` → `bind_dependencies` → `run`) and prints what came back. Where a case ships an RTSTRUCT whose FrameOfReferenceUID matches the selected series, it recomputes the same quantities inside the contours and reports both — that reference is a DIFFERENT measurement, not ground truth. Prints UIDs, never a name, an MRN or a date. |

### Preparing a training corpus

| | |
|---|---|
| `medos/tools/ingest/nnunet_dataset.py` | Builds an nnU-Net raw dataset from a Slicer NRRD corpus, with a per-case supervision mask — which is what makes partial supervision expressible at all. |
| `medos/tools/ingest/stratified_split.py` | Writes `splits_final.json` so every channel is measurable in every fold. A fold in which a channel never appears is a fold whose score for it is undefined, and a macro-average over undefined is the defect register entry 102 is about. |
| `medos/tools/trainer/resume_preprocess.py` | Resumes nnU-Net preprocessing instead of starting it over. Preprocessing a corpus is measured in hours; losing it to an interrupted shell is a cost with no information in it. |

`tests/unit/test_tools_are_documented.py` asserts this list stays complete: a tool added to
`medos/tools/` and named in no document fails a check rather than becoming folklore.

## Conventions that will surprise you

**A check that cannot fail is not a check.** Break the thing your guard guards and watch
it go red. Several suites do this to themselves:
`tests/unit/test_permission_contract.py` reintroduces §19.3.7's violation as a mutation
because the committed files cannot exhibit it, and
`tests/unit/test_dev_mode_cannot_reach_production.py` exists in its current form because
one of its own mutations escaped on the first attempt.

**Refusals are the product.** When something cannot be established, the platform says so
rather than choosing. `medos/medos/config/devmode.py`, `medos/medos/training/channelmap.py` and
`load_deid_provenance` are the pattern: no defaults, a named variable, and a message that
says what would have to become true.

**`docs/spec/99-known-inconsistencies.md`** is where a defect goes when fixing it is
larger than the change in hand — with what it costs and what would resolve it. 153 entries.
Adding one is not a way to avoid fixing something; it is a way to avoid pretending
something is fixed.

## What `up -d` starts, and what it does not

**MedicalOS is two products out of one repository**, and the profile table below is how
you choose between them.

| | |
|---|---|
| **Core** — `medos-api`, image `medicalos/medos` | The PACS-and-models service. DICOM in and out through the credentialed gateway, jobs, capabilities, the service and model registries, results, reviews. **26 `/api/v1` paths.** |
| **Train** — `medos-train-api`, image `medicalos/medos-train` | Core plus model preparation: harvesting, curation, dataset versions, splits, annotation sets, training runs, configuration searches, conversion runs. **53 `/api/v1` paths**, a strict superset. |

Train is Core plus two routers, composed at build time by
`medos.api.training_plane:create_training_app`. There is no `MEDOS_ENABLE_TRAINING`:
`MOS-REL-108` forbids dynamic module import in a platform process, so which surfaces a
process serves is decided by which entrypoint was started.

**Core cannot train, and that is enforced three ways rather than promised.**
`tests/gate/test_core_train_boundary.py` walks the static import closure of
`medos.api.app` and fails if it reaches `medos.training`. The Dockerfile's `core` target
then *deletes* `medos/medos/training` and both routers. And that stage builds the app inside
the image and fails the build if anything still imports them — so `find_spec
('medos.training')` returns `None` in the Core image and `51` paths become `24`.

Fifteen services are declared; **seven** start by default. The other eight sit behind two
profiles, and `tests/unit/test_compose_profiles.py` fails if that stops being true.

| Profile | Services | Needs |
|---|---|---|
| *(default)* | postgres, orthanc, medos-gateway, medos-api, medos-worker, ohif, medos-sealed-service | nothing |
| `inference` | triton, minio, medos-model-publish, medos-tritond | ~8.1 GB of pull. **No GPU** |
| `training` | medos-train-api, medos-trainer-environment, medos-trainer | the trainer pair needs an NVIDIA runtime and `trainer/build.sh` run first; `medos-train-api` needs neither |
| `demo` | medos-seed-corpus | nothing. A one-shot; it stores one synthetic study and exits |

**Triton does not need a GPU**, and this document claimed for a long time that it was the
reason a GPU-less laptop struggled. It carries no device reservation and
`medos/medos/inference/repository.py` writes `KIND_CPU` instance groups; it is a *download*
cost. The GPU reservation is on `medos-trainer-environment`, and because `medos-api`
used to wait on that service completing — and `ohif` waits on `medos-api` — a missing
GPU took out the API and the viewer, which is the whole user-facing surface. That edge
is gone; `medos/medos/api/routes_training.py` answers `503 TRAINING_ENVIRONMENT_NOT_RECORDED`
instead, and because the environment is read **per request** the 503 stops on its own
once the declaration exists.

The trainer image is not on any registry, so `--profile training` builds it, and the
build refuses an empty `MEDOS_CODE_COMMIT` by design. Run `trainer/build.sh`
first; it computes the commit and the dirty flag rather than letting you assert them.

## Known development friction

- `medos/tools/demo/seed_corpus.py` seeds ONE study by default. It is enough to see the
  pipeline work and nowhere near enough to evaluate anything: there is no cohort, no
  variation and no second case, so nothing that depends on a population — leakage
  checks, splits, non-inferiority — has anything to chew on.

  `--companion` adds a SECOND study of the same phantom, sharing its FrameOfReferenceUID
  and offset 7 mm along the slice axis and 5 mm in plane. That is not a cohort either; it
  exists because everything the viewer does ACROSS series needs two of them, and with one
  in the archive `source === target` in every panel pair. Both offsets are deliberately
  not whole multiples of their pitch — 3.5 slices and 7.14 rows — so the rounding is
  exercised rather than coincidentally right. Off by default, because `tests/e2e` asserts
  against the corpus this tool produces.

  `tests/integration/test_viewer_geometry_executes.py` runs the viewer's own geometry
  modules under node against exactly that pair, which is how two defects invisible to the
  static gates were found: a link badged `position-linked / exact` with the panels 41 mm
  apart, and a feet-first series linked at a reported 0.000 mm with the panels 83.30 mm
  apart on opposite sides of the midline.
- The MedicalOS extension loads but answers `401` on every job route until you supply
  `MEDOS_API_VIEWER_AUTHORIZATION`. (The training console that used to be the second
  served surface is withdrawn at specification 0.4.0, register entry 150.)
- On Windows, `nnUNetv2_plan_and_preprocess --verify_dataset_integrity` over a bind mount
  from NTFS can appear to hang — see register entry 99. It is reading, slowly.
