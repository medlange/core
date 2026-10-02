<!-- MedicalOS Specification v0.4.0 — chapter 14 of 19. Normative.
     87 requirements. Do not edit without a requirement-ID review. -->

[← 13. Observability, Deployment and Scaling](13-operations.md) · [Index](../../MEDICALOS_SPEC.md) · [15. Delivery Plan and Engineering Rules →](15-delivery.md)

---

## 14. Testing and Acceptance

The previous version of this document contained seven acceptance tests, three of which returned green on a broken system, and a validation stage named `DICOM TEST` with no contents. Both failures have the same cause: a test was written as a *name* rather than as a *named observable and a threshold*. This chapter fixes that structurally. Every check below states the surface it reads, the value it compares, the tolerance, and — for every acceptance test — a **mutant**: a specific, committed break that the test MUST detect. A test with no mutant is not a test in this project.

### 14.1 Purpose, evidence rules and traceability

#### 14.1.1 What testing is for here

MedicalOS has three distinct correctness problems and they need different evidence:

| problem | what can be proven in CI | what cannot |
|---|---|---|
| **Platform correctness** — does the job run once, land where it says, isolate tenants, write valid DICOM | Everything. This chapter is almost entirely about this. | — |
| **Service correctness** — does the vendor's model produce clinically adequate output | Nothing. Evidence is produced by the evidence plane (Chapter 7) against sealed `DatasetVersion`s, not by CI. | CI MUST NOT compute or publish a clinical metric. |
| **Deployment fitness** — does this service work at this site on this site's data | Site acceptance testing (Chapter 7), which reuses the fixture corpus and the DICOM battery defined here. | Clinical validation. The platform performs none. |

**MOS-TEST-001** — CI MUST NOT emit any number that could be read as a clinical performance claim. Sensitivity, specificity, Dice against clinical ground truth and AUC are produced only by an `EvaluationRun` (Chapter 7) and are never a CI assertion. Dice appears in CI exactly once, as an *identity* check on a DICOM round-trip (§14.4.3), where the expected value is exactly `1.0`.

#### 14.1.2 Rules of evidence

**MOS-TEST-002** — Every normative requirement in this document (`MOS-<AREA>-<NNN>`) MUST appear in `tests/traceability.yaml` mapped to at least one executable check id. A requirement mapped to zero checks fails the `traceability` CI job.

**MOS-TEST-003** — Every check MUST be falsifiable in writing: its definition MUST name (a) the surface read, (b) the expression evaluated, (c) the pass predicate including tolerance. A check whose pass predicate is a human judgement ("displays", "works", "is correct") MUST NOT be admitted to any gate.

**MOS-TEST-004 (the mutant rule)** — Every acceptance test (§14.7) and every check in the DICOM battery (§14.4) MUST ship with at least one declared mutant per surface it claims to check. A mutant is a committed patch (`tests/mutants/<check-id>-M<n>.diff` or `.sql`) that breaks exactly one thing. The `mutants` CI job applies each mutant to a scratch worktree, runs the owning check, and requires the check to **fail, naming the surface the mutant broke**. A check that passes under its own mutant is a defect of severity equal to the requirement it claims to cover, and blocks release.

**MOS-TEST-005** — No test stage may exist as a name without contents. A stage id declared in `tests/stages.yaml` with an empty check list fails the `stages` CI job. This applies to the validation pipeline stages referenced by Chapter 6 and Chapter 7.

**MOS-TEST-006** — Check ids are stable and namespaced: `L<level>-<area>-<nnn>` for levels L0–L4 (e.g. `L1-IMG-014`), `D<n>` for DICOM battery checks, `Q<nn>` for queue driver conformance, `C-<boundary>` for contract tests, `FT-<nn>` for failure tests, `AT-<nn>` for acceptance tests. Ids are never reused after deletion.

**MOS-TEST-007** — No test fixture, golden file, recorded HTTP interaction, or CI log may contain real PHI. The `phi-scan` CI job scans the repository and every built image for the token set defined in §14.6.6 and fails on any hit. The scanner's positive control is the `fx-phi-headers` fixture (§14.3.3): the `phi-scan` job MUST report a hit on that fixture when run in `--selftest` mode, and MUST fail if it does not. An unverified scanner is not a scanner.

#### 14.1.3 Review findings closed here

| finding | closed by |
|---|---|
| B9 — three acceptance tests pass on a broken system | MOS-TEST-004 (mutant rule); AT-05, AT-08, AT-11 rewritten in §14.7.3–§14.7.5 |
| B9 — `DICOM TEST` stage has no contents | §14.4, checks D1–D7; MOS-TEST-005 |
| B4 — no named fixture corpus | §14.3, corpus table and selector oracle |
| B8 — crash window undetectable | FT-07, §14.6.4 |
| B2 — duplicate SEG series on retry invisible to the test | AT-05 surface S2 |
| G1/G2 — tenancy stops at the API plane; provenance cannot reproduce | AT-08 (four planes), AT-10 (provenance replay) |
| §107 — "platform" is unfalsifiable | AT-18, §14.7.6 |

### 14.2 The test pyramid

#### 14.2.1 Levels

**MOS-TEST-008** — The following level definitions are binding. A check MUST be placed at the lowest level at which it is decidable. Placing a decidable-at-L1 check at L5 is a defect: it converts a 40 ms unit failure into a 15-minute stack failure with an ambiguous cause.

| level | name | runs against | examples | trigger | wall-clock budget |
|---|---|---|---|---|---|
| **L0** | Static | source only | lint, `mypy --strict`, `go vet`, JSON-Schema validity, generated-code drift, licence allowlist, `traceability`, `stages` | every push | ≤ 3 min |
| **L1** | Unit | pure functions, no I/O | slice sorting and canonical-volume construction, `SeriesSelector` predicate evaluation, deterministic UID derivation, de-identification profile application, `resolve()` (Chapter 6), UCUM/coded-concept lookup, content-safety token extraction | every push | ≤ 5 min, ≥ 3000 cases |
| **L2** | Component | one process + its real adapters, ephemeral containers | repository layer against real PostgreSQL with RLS enabled, gateway against real Orthanc, highdicom writers against real files, JobQueue driver conformance (§14.5.5) | every push | ≤ 12 min |
| **L3** | Contract | schema artifacts, both sides, no full stack | §14.5 | every push | ≤ 4 min |
| **L4** | Integration | 2–5 real components + fault injection | FT-01 … FT-18 (§14.6) | merge to `main` | ≤ 25 min |
| **L5** | Acceptance | the full `docker compose` stack | AT-01 … AT-24 (§14.7) | merge to `main`, release candidate | ≤ 60 min |
| **L6** | Evidence | real cohorts, GPU, non-CI cadence | `EvaluationRun`s, drift monitors, soak (§14.6.7) | nightly / weekly / release | unbounded |

#### 14.2.2 What must not be faked

**MOS-TEST-009** — At L2 and above the following MUST be the real implementation, never a mock, stub, in-memory substitute or fake:

| component | why a fake is forbidden |
|---|---|
| PostgreSQL | RLS, `FOR UPDATE SKIP LOCKED`, `LISTEN/NOTIFY` and transaction semantics are the thing under test. An in-memory repository proves nothing about MOS-EXEC-002. |
| The PACS (Orthanc) and the DICOM Gateway in front of it | STOW-RS partial-success semantics, QIDO-RS attribute support and instance counting are the surfaces AT-05 reads. |
| highdicom / pydicom | The output *is* the contract. A fake writer makes the battery vacuous. |
| The object store (MinIO) | Presigned-URL scoping is a tenancy surface (AT-08 S5). |
| The queue driver under test | Q01–Q14 are driver semantics. |

Permitted fakes: the inference backend (an `InferenceBackend` that returns a fixture tensor — this is what makes L4/L5 GPU-free and deterministic), external LLM providers, clock (via an injected `Clock` port), and outbound webhook receivers.

**MOS-TEST-010** — The inference fake used at L4/L5 MUST be the *same* `ServiceVersion` artifact as production, with only the model weights replaced by a `fixture-echo` backend declared in the `service.yaml` under `runtime.backend: fixture-echo`. Preprocessing, postprocessing, `ResultBundle` construction and the geometry inverse transform MUST run for real. Faking the whole service defeats the purpose of every check in §14.4.

#### 14.2.3 Determinism, flakes and mutation score

**MOS-TEST-011** — Every test process MUST run with a fixed seed set (`PYTHONHASHSEED=0`, `numpy.random.default_rng(20260913)`, `torch.manual_seed(20260913)`), with `torch.use_deterministic_algorithms(True)` and `CUBLAS_WORKSPACE_CONFIG=:4096:8` where torch is loaded, and with an injected clock. Wall-clock sleeps are forbidden; tests wait on conditions with a stated deadline.

**MOS-TEST-012** — A test observed to fail non-deterministically MUST be quarantined within one working day by adding it to `tests/quarantine.yaml` with an owner and an expiry date ≤ 14 days. CI fails if any quarantine entry is past expiry. A test covering a module in the `safety_critical` list (below) MUST NOT be quarantined; if it flakes, the release is blocked until it is fixed.

**MOS-TEST-013** — Modules in the `safety_critical` list MUST sustain a mutation score ≥ 0.80, measured weekly by `mutmut` (Python) / `go-mutesting` (Go) and recorded in `tests/mutation-score.json`. The list is exactly:

```yaml
# tests/safety-critical.yaml
safety_critical:
  - imaging/geometry            # canonical volume, sorting, inverse transform
  - imaging/uid                 # deterministic SeriesInstanceUID / SOPInstanceUID derivation
  - imaging/dicom_writer        # SEG / SR / SC construction
  - dataplane/deidentify        # PS3.15 profile application + UID remapping table
  - dataplane/triage            # SeriesSelector evaluation
  - registry/resolve            # capability resolution
  - security/rls                # tenant session binding
  - safety/content_postconditions  # narrative token set-membership (Chapter 11)
```

A score below 0.80 blocks the release, not the merge.

**MOS-TEST-014** — The geometry module MUST carry property-based tests (Hypothesis) over randomly generated but physically valid acquisitions: direction cosines from random orthonormal pairs, spacing in `[0.3, 5.0]` mm, 8–600 slices, ascending or descending `ImagePositionPatient`, `RescaleSlope ∈ [0.5, 2.0]`. The invariant asserted is `inverse(forward(v)) == v` on the index grid and `‖forward⁻¹(forward(p)) − p‖ < 1e-6 mm` on 256 random patient-space points per case.

**MOS-TEST-015** — All generated code (Go structs, Pydantic models, `openapi.yaml`, SDK clients) MUST be regenerated in CI and `git diff --exit-code` MUST be clean. Hand-edited generated files fail L0.

### 14.3 The fixture corpus

#### 14.3.1 Construction rules

**MOS-TEST-016** — There is exactly one fixture corpus, rooted at `tests/fixtures/corpus/`, described by one generated lock file `tests/fixtures/corpus.lock`. Every test at L2 and above draws its inputs from it by fixture id. Ad-hoc study downloads inside a test are forbidden.

**MOS-TEST-017** — Fixtures come in two tiers.

| tier | content | size | storage | used by |
|---|---|---|---|---|
| **H** (header) | DICOM instances with `PixelData` replaced by a single-voxel `0x0000` and `Rows`/`Columns` preserved in a companion `original_shape` sidecar; all other attributes byte-identical to source | ≤ 40 KB per instance | committed to the repository when the licence permits | triage, `SeriesSelector`, routing, tenancy, API, queue, event-envelope tests |
| **P** (pixel) | complete source instances | 5 MB – 1.2 GB per study | never committed; fetched by `medos/tools/fixtures/fetch.py` into `$MEDICALOS_FIXTURE_CACHE` and verified by SHA-256 | geometry, preprocessing, DICOM writing, the battery, the acceptance suite |

**MOS-TEST-018** — `corpus.lock` is generated by `medos/tools/fixtures/build.py` and MUST NOT be hand-edited. CI job `fixtures-lock` re-runs the generator against the recorded selection predicates and fails on any diff. `medos/tools/fixtures/verify.py` re-hashes every cached tier-P file against the lock before any L4/L5 job starts.

**MOS-TEST-019 (licence gate)** — Every fixture entry MUST record `licence`, `redistributable` and `derivative_restricted`. CI job `fixture-licence` fails if:

1. a fixture with `redistributable: false` has any file under `tests/fixtures/corpus/`;
2. any built container image contains a path matching a `redistributable: false` fixture id;
3. a derived fixture's parent has `derivative_restricted: true` and the derived fixture is committed;
4. `docs/ATTRIBUTION.md` does not contain one attribution block per distinct `collection` present in the lock.

**MOS-TEST-020** — Each fixture is `kind: natural` or `kind: derived`.
- A **natural** fixture MUST state the collection(s) searched and a machine-evaluable `predicate` over DICOM attributes, plus a deterministic tie-break (`matched_first_by`). If the predicate matches zero instances at build time, the build fails unless the entry declares a `fallback_recipe` naming a derived fixture.
- A **derived** fixture MUST state its `parent` fixture id and a `recipe` — a versioned, executable transform in `medos/tools/fixtures/recipes/`. Derived fixtures mint identity deterministically as `2.25.<int(uuid5(FIXTURE_NS, fixture_id))>` where `FIXTURE_NS = uuid5(NAMESPACE_URL, "urn:medicalos:fixture-corpus:1") = 48639f76-3532-5493-a914-bff952a475aa`, so the lock is reproducible on any machine.

#### 14.3.2 Lock file schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://medicalos.org/schemas/fixture-corpus/1.json",
  "type": "object",
  "required": ["schema_version", "fixture_namespace", "fixtures"],
  "additionalProperties": false,
  "properties": {
    "schema_version": {"const": 1},
    "fixture_namespace": {"const": "48639f76-3532-5493-a914-bff952a475aa"},
    "generated_at": {"type": "string", "format": "date-time"},
    "fixtures": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "required": ["id", "kind", "tier", "exercises", "study_instance_uid", "series"],
        "additionalProperties": false,
        "properties": {
          "id": {"type": "string", "pattern": "^fx-[a-z0-9-]+$"},
          "kind": {"enum": ["natural", "derived"]},
          "tier": {"enum": ["H", "P"]},
          "exercises": {"type": "array", "minItems": 1, "items": {"type": "string"}},
          "collection": {"type": "string"},
          "archive": {"enum": ["TCIA", "Qure.ai", "medicalos-derived"]},
          "licence": {"type": "string"},
          "redistributable": {"type": "boolean"},
          "derivative_restricted": {"type": "boolean"},
          "retrieved": {"type": "string", "format": "date"},
          "predicate": {"type": "string"},
          "matched_first_by": {"type": "string"},
          "parent": {"type": "string"},
          "recipe": {"type": "string"},
          "recipe_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
          "study_instance_uid": {"type": "string", "pattern": "^[0-9.]{1,64}$"},
          "series": {
            "type": "array", "minItems": 1,
            "items": {
              "type": "object",
              "required": ["series_instance_uid", "modality", "instances", "sha256"],
              "additionalProperties": false,
              "properties": {
                "series_instance_uid": {"type": "string", "pattern": "^[0-9.]{1,64}$"},
                "modality": {"type": "string"},
                "series_number": {"type": "integer"},
                "image_type": {"type": "array", "items": {"type": "string"}},
                "instances": {"type": "integer", "minimum": 1},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
              }
            }
          }
        }
      }
    }
  }
}
```

Derived-fixture identity is computable without the data, which is what makes the lock verifiable on a clean checkout:

```python
# medos/tools/fixtures/uid.py
import uuid
FIXTURE_NS = uuid.uuid5(uuid.NAMESPACE_URL, "urn:medicalos:fixture-corpus:1")
# -> 48639f76-3532-5493-a914-bff952a475aa

def fixture_uid(fixture_id: str) -> str:
    return "2.25." + str(uuid.uuid5(FIXTURE_NS, fixture_id).int)

# Frozen values, asserted by tests/fixtures/test_uid.py:
# fx-duplicate-series          -> 2.25.220470569187206757693201075958762274776
# fx-gantry-tilt-chest         -> 2.25.122788796479489700610114484090803732378
# fx-calib-seg                 -> 2.25.70185737467959023484399899046465634304
# fx-feet-first-prone          -> 2.25.20081527151531780125258503210573742613
# fx-corrupt-pixeldata         -> 2.25.176569371825613604415673501281747608408
# fx-nonuniform-spacing-synth  -> 2.25.26453428698312963428575526467666699957
```

#### 14.3.3 The named corpus

**MOS-TEST-021** — The following fixtures MUST exist in `corpus.lock`. Column *predicate / recipe* is the literal text stored in the lock entry.

| fixture id | exercises | kind / tier | collection · archive · licence | predicate / recipe |
|---|---|---|---|---|
| `fx-localizer-only` | triage must find no eligible series | natural · H | CPTAC-LUAD · TCIA · CC BY 4.0 (search order: CPTAC-LUAD, TCGA-LUAD, MIDRC-RICORD-1A) | study where every series has `'LOCALIZER' in ImageType` or `NumberOfSeriesRelatedInstances <= 3` |
| `fx-dose-report` | a `Modality=SR` series with no `PixelData` must never be selected or retrieved as pixels | natural · H | CPTAC-LUAD · TCIA · CC BY 4.0 | series where `SOPClassUID == '1.2.840.10008.5.1.4.1.1.88.67'` (X-Ray Radiation Dose SR) |
| `fx-coronal-reformat` | orientation filter; a reformat must not win the tie-break against the axial acquisition | natural · P | CPTAC-LUAD · TCIA · CC BY 4.0 | study containing both `'REFORMATTED' in ImageType` with `angle(slice_normal, [0,0,1]) > 60°` and an axial `ORIGINAL\PRIMARY\AXIAL` series |
| `fx-duplicate-series` | deterministic tie-break between two equally eligible series | derived · P | parent `fx-effusion-thin` · medicalos-derived | `recipes/duplicate_series.py`: copy the eligible series, mint `SeriesInstanceUID = 2.25.220470569187206757693201075958762274776`, keep pixels, geometry, kernel and thickness identical, set `SeriesNumber = parent + 1` |
| `fx-gapped-series` | missing slices must be detected, not silently interpolated | natural · P | LIDC-IDRI · TCIA · CC BY 3.0 | thoracic CT series where `max(diff(z)) > 1.5 * median(diff(z))` and `max(diff(z)) - median(diff(z)) > 0.5 mm` |
| `fx-nonuniform-spacing` | non-uniform `ImagePositionPatient` deltas | natural · P | LIDC-IDRI · TCIA · CC BY 3.0 | series where `std(diff(z)) / median(diff(z)) > 0.02` and no single gap exceeds `1.5 ×` median (i.e. drift, not a hole) |
| `fx-nonuniform-spacing-synth` | the same, bounded and exact, for L1 | derived · H | parent `fx-effusion-thin` · medicalos-derived | `recipes/perturb_spacing.py`: rewrite `ImagePositionPatient` z with `z_i = i*1.0 + 0.06*sin(i)`, `SeriesInstanceUID = 2.25.26453428698312963428575526467666699957` |
| `fx-gantry-tilt-head` | non-zero `GantryDetectorTilt (0018,1120)` in real acquisition | natural · P | CQ500 · Qure.ai · CC BY-NC-SA 4.0 (`redistributable: false`, `derivative_restricted: true`) | series where `abs(float(GantryDetectorTilt)) > 1.0` |
| `fx-gantry-tilt-chest` | tilt handling on the chest path without an NC-SA parent | derived · P | parent `fx-effusion-thin` · medicalos-derived | `recipes/apply_tilt.py`: set `GantryDetectorTilt = 12.0`, shear `ImagePositionPatient` accordingly, leave `ImageOrientationPatient` unchanged (the real-world hazard shape), `SeriesInstanceUID = 2.25.122788796479489700610114484090803732378` |
| `fx-single-instance` | a one-instance series must not be volumised | natural · H | CPTAC-LUAD · TCIA · CC BY 4.0 | series where `NumberOfSeriesRelatedInstances == 1` and `Modality == 'CT'` |
| `fx-multikernel-chest` | kernel-class selection between soft and sharp recons of one acquisition | natural · P | LIDC-IDRI · TCIA · CC BY 3.0 | study whose CT series expose ≥ 2 distinct `ConvolutionKernel` values over the same `FrameOfReferenceUID` |
| `fx-effusion-thin` | the happy path: thin axial soft-kernel chest CT | natural · P | LCTSC · TCIA · CC BY 3.0 | `Modality == 'CT'`, `'AXIAL' in ImageType`, `SliceThickness <= 1.5`, `BodyPartExamined in ('CHEST','LUNG','THORAX')` |
| `fx-lungseg-gt` | lung-mask reference geometry for battery round-trips (not a clinical metric) | natural · P | LCTSC · TCIA · CC BY 3.0 | study with an `RTSTRUCT` containing ROI names matching `^Lung_?[LR]$` |
| `fx-nodule-4reader` | `AnnotationSet` consensus rules; second-capability acceptance (AT-18) | natural · P | LIDC-IDRI · TCIA · CC BY 3.0 | cases with 4 independent reader XML annotations and ≥ 1 nodule ≥ 3 mm |
| `fx-foreign-seg` | our SEG reader must read a SEG we did not write | natural · P | NSCLC-Radiomics · TCIA · CC BY-NC 3.0 (`redistributable: false`) | instance where `SOPClassUID == '1.2.840.10008.5.1.4.1.1.66.4'` |
| `fx-foreign-sr` | our SR reader must parse a TID 1500 report we did not write | natural · P | QIN-HEADNECK · TCIA · CC BY 3.0 | instance where `SOPClassUID == '1.2.840.10008.5.1.4.1.1.88.34'` and `ContentTemplateSequence[0].TemplateIdentifier == '1500'` |
| `fx-phi-headers` | de-identification profile; `phi-scan` positive control | natural · P | Pseudo-PHI-DICOM-Data · TCIA · CC BY 4.0 | any instance in the collection |
| `fx-burned-in-phi` | pixel-burned-in PHI detection | natural · P | Pseudo-PHI-DICOM-Data · TCIA · CC BY 4.0 | instance where `BurnedInAnnotation == 'YES'` |
| `fx-feet-first-prone` | orientation canonicalisation; the mirrored-laterality failure | derived · P | parent `fx-effusion-thin` · medicalos-derived | `recipes/reorient.py`: set `PatientPosition = 'FFP'`, recompute `ImageOrientationPatient` and `ImagePositionPatient` consistently, do not touch pixels, `SeriesInstanceUID = 2.25.20081527151531780125258503210573742613` |
| `fx-rescale-nontrivial` | `RescaleSlope != 1` HU conversion | natural · H | MIDRC-RICORD-1A · TCIA · CC BY 4.0 | instance where `float(RescaleSlope) != 1.0` |
| `fx-enhanced-ct` | Enhanced CT: one instance, many frames | natural · P | MIDRC-RICORD-1A · TCIA · CC BY 4.0 | instance where `SOPClassUID == '1.2.840.10008.5.1.4.1.1.2.1'` |
| `fx-envelope-out` | applicability-envelope rejection (0.6 mm, sharp kernel, contrast) | natural · P | MIDRC-RICORD-1A · TCIA · CC BY 4.0 | `SliceThickness <= 0.75` and `ConvolutionKernel` in the sharp class and `ContrastBolusAgent` present |
| `fx-wrong-bodypart` | a head study offered to a chest capability | natural · P | CQ500 · Qure.ai · CC BY-NC-SA 4.0 (`redistributable: false`) | `BodyPartExamined == 'HEAD'`, `Modality == 'CT'` |
| `fx-corrupt-pixeldata` | truncated pixel data must fail, not silently zero-fill | derived · P | parent `fx-effusion-thin` · medicalos-derived | `recipes/truncate.py`: truncate `PixelData` of instance index 17 to 60 % of its declared length, `SeriesInstanceUID = 2.25.176569371825613604415673501281747608408` |
| `fx-prior-ai-output` | the platform MUST NOT consume its own output as input | derived · P | parent `fx-effusion-thin` · medicalos-derived | `recipes/inject_prior_result.py`: STOW a previously generated SEG (`SeriesNumber 9001`) into the study before triage runs |
| `fx-calib-seg` | viewport calibration markers for the render assertion (§14.7.5) | derived · P | parent `fx-effusion-thin` · medicalos-derived | `recipes/calibration_seg.py`: single-segment SEG whose only set voxels are `(0,0)`, `(0,C-1)`, `(R-1,0)`, `(R-1,C-1)`, `(R//2,C//2)` on the slice with `InstanceNumber == 100`, `SeriesInstanceUID = 2.25.70185737467959023484399899046465634304` |
| `fx-huge-study` | retrieval and volumisation throughput; 400+ slice budget | natural · P | LIDC-IDRI · TCIA · CC BY 3.0 | CT series where `NumberOfSeriesRelatedInstances >= 500` |

**MOS-TEST-022** — Recipes are pure and hermetic: same parent bytes in, same bytes out. `tests/fixtures/test_recipes.py` asserts that re-running every recipe reproduces the recorded `sha256` for every derived series.

#### 14.3.4 The selector oracle

**MOS-TEST-023** — For each fixture, the *expected* triage decision is data, not code. `tests/fixtures/selector_oracle.yaml` records, per (fixture, capability) pair, the expected outcome; `L2-DATA-030` asserts the real triage output equals it exactly. Rejection reason codes are the machine-readable set defined by Chapter 3 and carried on the Job per MOS-EXEC-001.

| fixture | offered to capability | expected selected series | expected Job terminal state | expected reason code |
|---|---|---|---|---|
| `fx-effusion-thin` | `pleural_effusion` | the axial ≤ 1.5 mm soft-kernel series | `COMPLETED` | — |
| `fx-localizer-only` | `pleural_effusion` | none | `REJECTED` | `no_eligible_series` |
| `fx-dose-report` | `pleural_effusion` | none | `REJECTED` | `no_eligible_series` |
| `fx-coronal-reformat` | `pleural_effusion` | the axial original series only | `COMPLETED` | — |
| `fx-duplicate-series` | `pleural_effusion` | exactly one series, the lower `SeriesNumber` | `COMPLETED` | — |
| `fx-gapped-series` | `pleural_effusion` | none | `REJECTED` | `geometry_unsupported` |
| `fx-nonuniform-spacing` | `pleural_effusion` | none | `REJECTED` | `spacing_non_uniform` |
| `fx-gantry-tilt-chest` | `pleural_effusion` | none unless the `ServiceVersion` declares `gantry_tilt: correct` | `REJECTED` | `geometry_unsupported` |
| `fx-single-instance` | `pleural_effusion` | none | `REJECTED` | `instance_count_below_minimum` |
| `fx-multikernel-chest` | `pleural_effusion` | the soft-kernel series only | `COMPLETED` | — |
| `fx-wrong-bodypart` | `pleural_effusion` | none | `REJECTED` | `body_part_mismatch` |
| `fx-envelope-out` | `pleural_effusion` | none | `REJECTED` | `outside_applicability_envelope` |
| `fx-prior-ai-output` | `pleural_effusion` | the original axial series only; `SeriesNumber >= 9000` excluded | `COMPLETED` | — |
| `fx-corrupt-pixeldata` | `pleural_effusion` | the series is selected; retrieval fails | `FAILED` | `source_data_unreadable` |
| `fx-nodule-4reader` | `lung_nodule` | the thin axial series | `COMPLETED` | — |

**MOS-TEST-024** — `fx-prior-ai-output` is a mandatory regression fixture: a platform that consumes its own DICOM output as input produces a self-amplifying feedback loop that no clinical review catches. `L2-DATA-031` asserts that no series whose `SeriesNumber >= 9000` **or** whose `DeviceSerialNumber` matches a registered `ServiceVersion` instance identity is ever placed in a Job's selected-series set.

### 14.4 The DICOM interoperability battery

#### 14.4.1 Definition and gate

**MOS-TEST-025** — The DICOM battery is the ordered check set `D1 … D7` defined below. It runs at L2 (generated objects on disk, D1–D4, D6–D7) and L4 (D5, against a live gateway + PACS). It is the contents of the `DICOM TEST` stage referenced by Chapter 6's deployment gate, and no `Deployment` of that `ServiceVersion` may leave `VERIFYING` for `state = SERVING` in any environment until every check passes on every applicable fixture (Chapter 6 §6.8, `MOS-REG-072`–`MOS-REG-075`). There is no `Deployment` state named `development`.

**MOS-TEST-037** — The battery MUST run as the cross-product `{every fixture whose oracle outcome is COMPLETED} × {every ServiceVersion in the CI service set}`. It MUST NOT be run once on one happy-path study.

**MOS-TEST-038** — Each battery run emits `artifacts/dicom-battery/<service>@<version>/<fixture>/report.json` with one record per check (`check_id`, `status`, `observed`, `expected`, `tolerance`, `tool`, `tool_version`). The report is attached to the `ValidationReport` (Chapter 7) and to the `Deployment` record (Chapter 6).

Tool versions are pinned in `tests/tools.lock`: `dicom3tools 1.00.snapshot.20250628` (`dciodvfy`, `dcentvfy`), `dcmtk 3.6.9` (`dsrdump`, `dcm2json`), `dcmqi 1.4.0` (`segimage2itkimage`), `highdicom 0.25.1`, `pydicom 3.0.1`, `SimpleITK 2.4.1`. `L0-OPS-002` asserts the installed versions equal the lock.

#### 14.4.2 D1 — structural validation

**MOS-TEST-026** — Every generated SEG, SR and SC MUST produce zero `Error` lines from `dciodvfy`.

```bash
# tests/dicom_battery/d1_structural.sh
set -euo pipefail
obj="$1"; allow="tests/dicom_battery/dciodvfy-allow.yaml"
dciodvfy -filename "$obj" > "$obj.dciodvfy.txt" 2>&1 || true
if grep -E '^Error' "$obj.dciodvfy.txt" >/dev/null; then
  echo "D1 FAIL: dciodvfy reported errors for $obj"; grep -E '^Error' "$obj.dciodvfy.txt"; exit 1
fi
python tests/dicom_battery/check_warnings.py --report "$obj.dciodvfy.txt" --allow "$allow"
```

**MOS-TEST-027** — `dciodvfy` warnings are not ignored. Each accepted warning MUST have an entry in `dciodvfy-allow.yaml` with a substring match, a justification and an expiry date; `check_warnings.py` fails on an unlisted warning or an expired entry.

```yaml
# tests/dicom_battery/dciodvfy-allow.yaml
allow:
  - match: "Retired Attribute <SeriesInstanceUID> Type 1"
    justification: "false positive in dicom3tools for SEG when SeriesInstanceUID is inherited via macro"
    applies_to: ["SEG"]
    expires: "2027-03-31"
    owner: "imaging"
```

**MOS-TEST-030 (part)** — `dcentvfy` MUST be run over the set {all source instances of the selected series} ∪ {generated SEG} ∪ {generated SR} ∪ {generated SC} and MUST report no unresolved UID reference:

```bash
dcentvfy "${SOURCE_FILES[@]}" "$SEG" "$SR" "$SC" 2>&1 | tee d3.txt
grep -qE '(Referenced .* not present|Error)' d3.txt && { echo "D3 FAIL"; exit 1; } || true
```

#### 14.4.3 D2 — SEG round-trip and geometry

**MOS-TEST-028** — Reading a generated SEG back and reconstructing it onto the source grid MUST reproduce the in-memory mask exactly: `numpy.array_equal` true **and** Dice exactly `1.0`. The check MUST fail as vacuous if both masks are empty.

**MOS-TEST-029** — The SEG's geometry MUST match the source series geometry within the following absolute tolerances: origin (first-frame `ImagePositionPatient`) `1e-4` mm; in-plane spacing and `SpacingBetweenSlices` `1e-4` mm; every direction-cosine component `1e-4` (dimensionless); `FrameOfReferenceUID` exact string equality.

```python
# tests/dicom_battery/d2_roundtrip.py
import numpy as np, pydicom

TOL_MM = 1e-4
TOL_COS = 1e-4

def seg_geometry(seg: pydicom.Dataset) -> dict:
    shared = seg.SharedFunctionalGroupsSequence[0]
    pm = shared.PixelMeasuresSequence[0]
    iop = np.asarray(shared.PlaneOrientationSequence[0].ImageOrientationPatient, dtype=np.float64)
    x_cos, y_cos = iop[0:3], iop[3:6]          # increasing column index, increasing row index
    z_cos = np.cross(x_cos, y_cos)
    dy, dx = float(pm.PixelSpacing[0]), float(pm.PixelSpacing[1])   # [between-rows, between-columns]
    dz = float(pm.SpacingBetweenSlices)
    return {"direction": np.column_stack([x_cos, y_cos, z_cos]),
            "spacing": np.array([dx, dy, dz], dtype=np.float64)}

def seg_to_source_grid(seg: pydicom.Dataset, source_positions: np.ndarray,
                       segment_number: int) -> tuple[np.ndarray, set]:
    """Rebuild the mask on the SOURCE grid by matching each frame's ImagePositionPatient.
    This deliberately does not trust frame order: frame ordering is part of what we test."""
    n_slices = len(source_positions)
    out = np.zeros((n_slices, int(seg.Rows), int(seg.Columns)), dtype=bool)
    frames = seg.pixel_array
    if frames.ndim == 2:
        frames = frames[np.newaxis]
    index = {tuple(np.round(p, 4)): i for i, p in enumerate(source_positions)}
    assert len(index) == n_slices, "source slice positions are not unique"
    seen: set = set()
    for fi, fg in enumerate(seg.PerFrameFunctionalGroupsSequence):
        if int(fg.SegmentIdentificationSequence[0].ReferencedSegmentNumber) != segment_number:
            continue
        ipp = tuple(np.round([float(v) for v in
                              fg.PlanePositionSequence[0].ImagePositionPatient], 4))
        assert ipp in index, f"D2 FAIL: frame {fi} at {ipp} is not a source slice position"
        assert ipp not in seen, f"D2 FAIL: two frames for slice position {ipp}"
        seen.add(ipp)
        out[index[ipp]] = frames[fi].astype(bool)
    return out, seen

def dice(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a.astype(bool), b.astype(bool)
    denom = int(a.sum()) + int(b.sum())
    assert denom > 0, "D2 VACUOUS: both masks empty; the round-trip assertion proves nothing"
    return 2.0 * int(np.logical_and(a, b).sum()) / denom

def check_d2(seg_path: str, reference_mask: np.ndarray, source_geom: dict,
             source_positions: np.ndarray, segment_number: int = 1) -> None:
    seg = pydicom.dcmread(seg_path)
    rebuilt, seen = seg_to_source_grid(seg, source_positions, segment_number)
    # sparse SEGs omit empty frames; every omitted slice must be empty in the reference
    for i, p in enumerate(source_positions):
        if tuple(np.round(p, 4)) not in seen:
            assert not reference_mask[i].any(), f"D2 FAIL: slice {i} non-empty but no frame written"
    assert np.array_equal(rebuilt, reference_mask.astype(bool)), "D2 FAIL: mask mismatch"
    assert dice(rebuilt, reference_mask) == 1.0, "D2 FAIL: Dice != 1.0"
    g = seg_geometry(seg)
    assert np.allclose(g["spacing"], source_geom["spacing"], atol=TOL_MM, rtol=0.0), \
        f"D2 FAIL: spacing {g['spacing']} vs source {source_geom['spacing']}"
    assert np.allclose(g["direction"], source_geom["direction"], atol=TOL_COS, rtol=0.0), \
        "D2 FAIL: direction cosines differ beyond 1e-4"
    first = np.asarray(seg.PerFrameFunctionalGroupsSequence[0]
                       .PlanePositionSequence[0].ImagePositionPatient, dtype=np.float64)
    assert np.allclose(first, source_positions[0], atol=TOL_MM, rtol=0.0) or \
           any(np.allclose(first, p, atol=TOL_MM, rtol=0.0) for p in source_positions), \
        "D2 FAIL: first frame origin is not a source slice origin"
    assert seg.FrameOfReferenceUID == source_geom["frame_of_reference_uid"], \
        "D2 FAIL: FrameOfReferenceUID differs from source"
```

Declared mutants for D2: **M1** replace `SpacingBetweenSlices` with the model-space spacing (must fail on spacing); **M2** write frames in acquisition order without `PlanePositionSequence` correction (must fail on position matching); **M3** shift the mask by one slice (must fail on `array_equal`); **M4** emit an all-zero mask (must fail as vacuous).

#### 14.4.4 D3 — frame of reference and source references

**MOS-TEST-030** — For every generated SEG:

| assertion | expression |
|---|---|
| frame of reference inherited | `seg.FrameOfReferenceUID == source_series.FrameOfReferenceUID` |
| every frame cites a real source instance | for each frame: `fg.DerivationImageSequence[0].SourceImageSequence[0].ReferencedSOPInstanceUID ∈ source_sop_uids` |
| no foreign references | `{referenced uids} ⊆ source_sop_uids` |
| frame count | `len(PerFrameFunctionalGroupsSequence) == n_segments_written * n_non_empty_slices` |
| segment identity | every `SegmentSequence` item has non-empty `SegmentedPropertyCategoryCodeSequence` and `SegmentedPropertyTypeCodeSequence`, `SegmentAlgorithmType == 'AUTOMATIC'`, non-empty `SegmentAlgorithmName` |
| study identity preserved | `seg.StudyInstanceUID == source.StudyInstanceUID` and `seg.PatientID == source.PatientID` |
| minted identity | `seg.SeriesInstanceUID != any source SeriesInstanceUID` and equals `derive_uid(...)` of MOS-IMG-062 evaluated with the job's full argument tuple — `org_root`, `tenant_id`, `idempotency_key`, `service_id`, `service_version`, `model_id`, `model_version`, `uid_space='source'`, `uid_kind='seg.series'`, `output_index` per MOS-IMG-065. The test MUST call the shipped implementation of MOS-IMG-062, not re-derive the UID from a restated tuple. |
| reserved series number | `int(seg.SeriesNumber) >= 9000` |
| equipment Type 1 | `Manufacturer`, `ManufacturerModelName`, `DeviceSerialNumber`, `SoftwareVersions` all present, non-empty, and equal to the `ServiceVersion.legal_manufacturer` block |
| cross-instance integrity | `dcentvfy` reports no unresolved reference (§14.4.2) |

**MOS-TEST-082 (verified-SR revision identity)** — Where `Deployment.emit_verified_sr_on_accept` is enabled, the battery MUST additionally run a two-round `ResultReview` cycle on `fx-effusion-thin` and assert, on the SR revisions issued under Chapter 9 `MOS-SAFE-065`:

| assertion | expression |
|---|---|
| the revision is a new instance | `sr_round1.SOPInstanceUID != sr_round2.SOPInstanceUID`, both equal to `derive_uid(...)` of MOS-IMG-062 with `uid_kind='sr.instance'` and the `output_index` MOS-IMG-065 allocates for each review round |
| no new series is minted | `sr_round1.SeriesInstanceUID == sr_round2.SeriesInstanceUID == ` the `sr.series` UID derived for the originating job |
| the superseded instance survives | the round-1 `SOPInstanceUID` is still retrievable by WADO-RS and byte-identical to the object stored before round 2 (`MOS-SAFE-065`: the original SR MUST NOT be deleted or overwritten) |
| the chain is navigable | `sr_round2.PredecessorDocumentsSequence` references exactly `sr_round1.SOPInstanceUID`; `VerificationFlag == 'VERIFIED'` |

Chapter 14 does not define the revision's identity: `review_round` is an `output_index` allocation owned by Chapter 4 `MOS-IMG-065`, and no argument is added to `derive_uid`. Declared mutant: allocate the same `output_index` for both rounds — the check MUST fail on the first row, naming the colliding `SOPInstanceUID`.

#### 14.4.5 D4 — SR parse and coded concepts

**MOS-TEST-031** — Every generated SR MUST: parse without error under `dsrdump +Ea` (exit 0) and under `highdicom.sr.srread`; carry `SOPClassUID == '1.2.840.10008.5.1.4.1.1.88.34'` (Comprehensive 3D SR); carry `ContentTemplateSequence[0].MappingResource == 'DCMR'` and `TemplateIdentifier == '1500'`; have a root `ConceptNameCodeSequence` of `(126000, DCM, "Imaging Measurement Report")`; contain exactly one `(126010, DCM)` "Imaging Measurements" container.

**MOS-TEST-032** — Every `NUM` content item MUST carry a non-empty `ConceptNameCodeSequence` and a `MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0]` whose `CodingSchemeDesignator == 'UCUM'`. Every `CODE` content item's `ConceptCodeSequence[0].CodingSchemeDesignator` MUST be in `{SCT, DCM, RADLEX, LN, UCUM}`. Every `(code_value, coding_scheme_designator)` pair used anywhere in the document MUST resolve in the platform code dictionary shipped at `data/coding/concepts.json` (Chapter 9); an unresolved pair fails D4.

**MOS-TEST-033** — Observer and algorithm identity MUST match the executing `ServiceVersion`, and the SR MUST be joinable to the SEG:

| content item | required value |
|---|---|
| `(121012, DCM)` Device Observer UID | the `ServiceVersion` instance UID recorded in the Job provenance |
| `(121013, DCM)` Device Observer Name | `service_id` |
| `(121014, DCM)` Device Observer Manufacturer | `legal_manufacturer.name` |
| `(121015, DCM)` Device Observer Model Name | `ManufacturerModelName` written into the SEG |
| `(121016, DCM)` Device Observer Serial Number | `DeviceSerialNumber` written into the SEG |
| `(111001, DCM)` Algorithm Name | `service_id` |
| `(111003, DCM)` Algorithm Version | `service_version` |
| `(112039, DCM)` Tracking Identifier | present on every measurement group |
| `(112040, DCM)` Tracking Unique Identifier | present, and the set of values equals the set of `TrackingUID`s on the SEG's `SegmentSequence` |
| `CurrentRequestedProcedureEvidenceSequence` | references exactly the source SOP Instance UIDs used, plus the generated SEG; no others |

```python
# tests/dicom_battery/d4_sr.py
import pydicom

ALLOWED_SCHEMES = {"SCT", "DCM", "RADLEX", "LN", "UCUM"}

def walk(node):
    for item in getattr(node, "ContentSequence", []):
        yield item
        yield from walk(item)

def code(cs_item) -> tuple[str, str]:
    return (str(cs_item.CodeValue), str(cs_item.CodingSchemeDesignator))

def check_d4(sr_path: str, dictionary: set[tuple[str, str]],
             expected: dict, source_sop_uids: set[str], seg_tracking_uids: set[str]) -> None:
    sr = pydicom.dcmread(sr_path)
    assert sr.SOPClassUID == "1.2.840.10008.5.1.4.1.1.88.34", "D4 FAIL: not Comprehensive 3D SR"
    tpl = sr.ContentTemplateSequence[0]
    assert (tpl.MappingResource, tpl.TemplateIdentifier) == ("DCMR", "1500"), "D4 FAIL: not TID 1500"
    assert code(sr.ConceptNameCodeSequence[0]) == ("126000", "DCM"), "D4 FAIL: wrong root concept"

    used, num_items, observed = set(), 0, {}
    for item in walk(sr):
        for cs in getattr(item, "ConceptNameCodeSequence", []):
            used.add(code(cs))
        if item.ValueType == "CODE":
            c = code(item.ConceptCodeSequence[0]); used.add(c)
            assert c[1] in ALLOWED_SCHEMES, f"D4 FAIL: coding scheme {c[1]} not allowed"
        if item.ValueType == "NUM":
            num_items += 1
            assert item.ConceptNameCodeSequence, "D4 FAIL: NUM item without a coded name"
            units = item.MeasuredValueSequence[0].MeasurementUnitsCodeSequence[0]
            assert units.CodingSchemeDesignator == "UCUM", \
                f"D4 FAIL: units {units.CodeValue} not UCUM"
        if item.ValueType in ("TEXT", "UIDREF"):
            name = code(item.ConceptNameCodeSequence[0])
            observed[name] = getattr(item, "TextValue", None) or getattr(item, "UID", None)

    assert num_items >= 1, "D4 VACUOUS: SR contains no measurement"
    unknown = used - dictionary
    assert not unknown, f"D4 FAIL: concepts absent from the platform dictionary: {sorted(unknown)}"
    for concept, want in expected.items():          # 121013..121016, 111001, 111003
        assert observed.get(concept) == want, \
            f"D4 FAIL: {concept} is {observed.get(concept)!r}, expected {want!r}"

    tracking = {str(i.UID) for i in walk(sr)
                if i.ValueType == "UIDREF" and code(i.ConceptNameCodeSequence[0]) == ("112040", "DCM")}
    assert tracking == seg_tracking_uids, \
        f"D4 FAIL: SR tracking UIDs {tracking} != SEG segment tracking UIDs {seg_tracking_uids}"

    evidence = {str(ref.ReferencedSOPInstanceUID)
                for study in sr.CurrentRequestedProcedureEvidenceSequence
                for series in study.ReferencedSeriesSequence
                for ref in series.ReferencedSOPSequence}
    assert source_sop_uids <= evidence, "D4 FAIL: evidence sequence omits source instances"
    assert evidence - source_sop_uids <= {expected["__seg_sop_uid__"]}, \
        "D4 FAIL: evidence sequence references instances outside the job's inputs"
```

#### 14.4.6 D5 — STOW-RS then QIDO-RS

**MOS-TEST-034** — Storage MUST be verified by reading back, not by trusting the write. All requests go through the DICOM Gateway (Chapter 3), never to the PACS directly.

```python
# tests/dicom_battery/d5_stow_qido.py
def check_d5(gateway, study_uid: str, objects: list, expected_series: dict[str, int]) -> None:
    """expected_series maps generated SeriesInstanceUID -> expected instance count."""
    resp = gateway.stow(study_uid, objects)
    assert resp.status_code == 200, f"D5 FAIL: STOW returned {resp.status_code}"
    body = resp.json()
    assert "00081198" not in body, f"D5 FAIL: FailedSOPSequence present: {body['00081198']}"
    stored = body["00081199"]["Value"]
    assert len(stored) == len(objects), \
        f"D5 FAIL: {len(stored)} instances referenced, {len(objects)} sent"

    for series_uid, want in expected_series.items():
        hits = gateway.qido(f"/studies/{study_uid}/series", SeriesInstanceUID=series_uid)
        assert len(hits) == 1, f"D5 FAIL: QIDO returned {len(hits)} series for {series_uid}"
        got = int(hits[0]["00201209"]["Value"][0])          # NumberOfSeriesRelatedInstances
        assert got == want, f"D5 FAIL: series {series_uid} has {got} instances, expected {want}"
        inst = gateway.qido(f"/studies/{study_uid}/series/{series_uid}/instances")
        assert {i["00080018"]["Value"][0] for i in inst} == \
               {o.SOPInstanceUID for o in objects if o.SeriesInstanceUID == series_uid}, \
            "D5 FAIL: stored SOPInstanceUIDs differ from the deterministically derived set"

    ai_series = gateway.qido(f"/studies/{study_uid}/series")
    generated = [s for s in ai_series if int(s["00200011"]["Value"][0]) >= 9000]
    assert len(generated) == len(expected_series), \
        f"D5 FAIL: {len(generated)} AI series in the study, expected {len(expected_series)}"
```

#### 14.4.7 D6 — independent-implementation read

**MOS-TEST-035** — At least one check in every battery run MUST be performed by an implementation the project did not write, so that a self-consistent-but-wrong writer is detectable.

| check | tool | assertion |
|---|---|---|
| D6.1 | `dcmqi segimage2itkimage --inputDICOM seg.dcm --outputDirectory out/` | the emitted NRRD, read with SimpleITK, has origin/spacing/direction equal to the source within `1e-4` and voxel-identical labels to the in-memory mask |
| D6.2 | `dsrdump +Ea sr.dcm` | exit 0; stdout contains every expected measurement's UCUM unit string |
| D6.3 | read `fx-foreign-seg` (NSCLC-Radiomics) with the platform SEG reader | parses; segment labels and geometry are recovered; no exception |
| D6.4 | read `fx-foreign-sr` (QIN-HEADNECK) with the platform SR reader | parses as TID 1500; every measurement's concept and unit resolve |

D6.3 and D6.4 are the checks that prove the reader is not merely round-tripping its own writer.

#### 14.4.8 D7 — AI-derived identification and RUO marking

**MOS-TEST-036** — Every generated object MUST be identifiable as AI-derived, and when the `Deployment` has `clinical_use_mode: research_only`, MUST additionally carry research marking (Chapter 9).

| assertion | SEG | SR | SC |
|---|---|---|---|
| AI-derived | `SeriesDescription` contains the configured AI marker token; `SegmentAlgorithmType == 'AUTOMATIC'` | `(111001/111003, DCM)` algorithm items present | `ConversionType == 'WSD'`; `SeriesDescription` contains the marker |
| research_only | `SeriesDescription` starts with the configured RUO prefix | SR document title concept is the research variant declared in Chapter 9 | RUO banner pixels present in the top 32 rows of every frame (asserted by mean-intensity delta ≥ 40 against the unbannered render) |
| clinical | RUO prefix MUST be absent | research title concept MUST be absent | banner MUST be absent |

D7 fails closed: if the `Deployment`'s `clinical_use_mode` cannot be read at check time, the check fails.

### 14.5 Contract tests

#### 14.5.1 Boundaries

**MOS-TEST-039** — Every boundary below MUST have a contract artifact and both a producer-side and a consumer-side check. A boundary with a check on only one side is not covered.

| id | boundary | producer | consumer | schema source | mechanism |
|---|---|---|---|---|---|
| `C-API` | HTTP API ↔ clients | control plane | SDKs, web UI, OHIF extension | `medos/schemas/api/*.json` → generated `openapi.yaml` | schemathesis against the live API; SDK round-trip |
| `C-QUEUE` | control plane ↔ `JobQueue` port | job creator | worker claimer | Go interface + `medos/schemas/queue/message.json` | driver conformance suite Q01–Q14 (§14.5.5) |
| `C-EVENT` | platform ↔ event bus | control plane, workers | SSE, webhooks, audit sink | `medos/schemas/events/envelope.json` + per-type payload schemas | golden samples both directions |
| `C-SVC` | platform ↔ Medical Service (Chapter 2) | platform (job input) / service (`ResultBundle`) | service / platform | `medos/schemas/service/job-input.json`, `medos/schemas/service/result-bundle.json`, `medos/schemas/service/service.yaml.json` | Service Conformance Kit (§14.5.6) |
| `C-DWEB` | platform ↔ DICOM Gateway | workers, OHIF, viewers | gateway | DICOM PS3.18 subset declared in `docs/dicomweb-conformance.md` | recorded-interaction tests + live gateway |
| `C-PACS` | gateway ↔ PACS | gateway | Orthanc (or replacement) | same subset | run the gateway suite against Orthanc and against a second DICOMweb server in nightly |
| `C-INFER` | worker ↔ inference backend | worker | Triton / in-process backend | `medos/schemas/inference/tensor-contract.json` (name, shape, dtype, layout, orientation) | tensor-contract assertion at worker startup and in CI |
| `C-PREP` | training ↔ serving preprocessing | one `PreprocessingSpec` implementation | both | `medos/schemas/preprocessing/spec.json` | golden-fixture tensor hash (AT-15) |
| `C-HOOK` | platform ↔ webhook receiver | control plane | tenant endpoint | `medos/schemas/events/webhook.json` | HMAC signature vectors + replay endpoint test |
| `C-MCP` (0.4) | platform ↔ MCP client | tool server | external agent | `medos/schemas/tools/*.json` | input/output schema conformance + tenancy binding assertion |

#### 14.5.2 Contract artifact layout

**MOS-TEST-040** — Each boundary owns a directory with a fixed shape:

```
medos/contracts/C-SVC/
  schema/result-bundle.json          # JSON Schema 2020-12, the single source
  samples/valid/effusion-minimal.json
  samples/valid/effusion-multi-finding.json
  samples/valid/empty-findings.json
  samples/invalid/missing-ucum-unit.json
  samples/invalid/mask-in-model-space.json
  samples/invalid/unknown-coded-concept.json
  samples/invalid/sop-uid-not-in-input.json
  README.md                          # who produces, who consumes, compatibility policy
```

**MOS-TEST-041 (producer obligation)** — During any L4 run, every message a producer emits on a covered boundary MUST be captured and validated against the boundary schema. Zero captured messages for a boundary exercised by the run fails the check — silence is not a pass.

**MOS-TEST-042 (consumer obligation)** — Every consumer MUST accept every file under `samples/valid/` and reject every file under `samples/invalid/`, with the rejection naming the violated schema keyword. `samples/invalid/` MUST contain at least one case per required field and per enumerated constraint.

**MOS-TEST-043 (compatibility gate)** — CI job `schema-compat` compares each schema against its form at the previous release tag. Within a major version, a change that removes a property, narrows a type, adds a `required` entry, or removes an `enum` value fails the job unless the PR carries the `breaking-change` label and a `docs/migrations/` entry.

**MOS-TEST-044** — Go structs, Pydantic models, `openapi.yaml` and all SDK clients MUST be generated from the JSON Schemas. A hand-written type on a covered boundary fails L0 via the drift check (MOS-TEST-015).

#### 14.5.3 API contract specifics

**MOS-TEST-049** — `C-API` MUST include: (a) schemathesis run over the generated `openapi.yaml` against the live API, with `--checks all`; (b) an assertion that every non-2xx response body validates against the RFC 9457 problem+json schema and carries a `class` member from the declared enum; (c) an assertion that `class: clinical_rejection` is returned for, and only for, the fixtures whose selector oracle expects `REJECTED`; (d) an assertion that every documented cursor-paginated collection returns a stable total ordering across pages under concurrent inserts.

**MOS-TEST-050** — Each SDK MUST have a test that performs the full AT-02 flow (create job, poll, stream events, fetch result) against the live stack and asserts the same values as the raw-HTTP version of the test. An SDK that compiles but cannot complete AT-02 is not shipped.

#### 14.5.4 Event envelope

**MOS-TEST-039 (continued)** — `C-EVENT` fixes the envelope; the payload schema is selected by `event_type`. The envelope contract test asserts that every emitted envelope carries a W3C `traceparent` that joins to the API span, a monotonically increasing `status_seq` per `job_id`, and an `attempt` counter — and that no envelope field or payload field carries a value present in the PHI token set (§14.6.6).

#### 14.5.5 JobQueue driver conformance suite

**MOS-TEST-045** — `C-QUEUE` is a single executable suite parameterised by driver. Every driver MUST pass every check, unmodified.

| id | check | pass condition |
|---|---|---|
| Q01 | single delivery under contention | 512 jobs, 32 concurrent claimers, no lease expiry: the union of claims is exactly the 512 jobs, each once |
| Q02 | lease exclusivity | after `Claim`, a second `Claim` for the same job returns nothing until `lease_ttl` elapses |
| Q03 | fencing | after lease expiry and re-claim by B, A's `Complete` is rejected with `stale_lease` and does not change job state |
| Q04 | heartbeat | `Heartbeat` extends the lease; total extensions are bounded by `max_lease_extensions`; exceeding it re-offers the job |
| Q05 | `Complete` idempotence | second `Complete` returns `already_complete` and performs no write |
| Q06 | retryable failure | `Fail(retryable=true)` re-offers after the declared backoff; `attempt` increments by exactly 1 |
| Q07 | terminal failure | `Fail(retryable=false)` moves the message to `<topic>.dlq` without further attempts |
| Q08 | load shed | a rejection caused by queue-full or admission control MUST NOT increment `attempt` |
| Q09 | enqueue idempotence | two `Enqueue` calls with the same `(tenant_id, idempotency_key)` yield one queue entry and one `Job` |
| Q10 | atomicity | with fault point `triage.after_job_insert_before_commit`, after crash either both the `Job` row and the queue entry exist, or neither |
| Q11 | per-key order | for one `<tenant_id>:<study_instance_uid>` key, claims occur in enqueue order |
| Q12 | cancel reserved | `Cancel` returns `unimplemented` in 0.1 and 0.2 (MOS-EXEC-001 reserves `CANCELLED`); the suite asserts the error, not the behaviour |
| Q13 | no loss under restart | 200 enqueued, broker/database restarted mid-flight, all 200 eventually claimed and completed |
| Q14 | poison message | a queue entry whose payload fails `medos/schemas/queue/message.json` goes to the DLQ on attempt 1 and does not block later entries on the same partition key |

**MOS-TEST-046 (driver parity)** — The 0.3 Kafka driver MUST pass Q01–Q14 with zero changes to the suite. Any check that needs a driver-specific variant indicates a leaky port and MUST be resolved by changing the port, not the test. Q13 for the Kafka driver additionally asserts that a consumer whose handler exceeds `max.poll.interval.ms` does not cause a rebalance: the suite runs a handler sleeping `2 × max.poll.interval.ms` and asserts exactly one claim, one completion and no duplicate delivery.

#### 14.5.6 The Service Conformance Kit

**MOS-TEST-047** — The platform MUST ship `medicalos-conformance`, a self-contained CLI a third-party vendor runs against their own image without access to the platform:

```bash
medicalos-conformance run \
  --image ghcr.io/acme/effusion@sha256:9f2b4c1d8e5a3706b2c9d4e1f8a7605b3c2d1e0f9a8b7c6d5e4f3a2b1c0d9e8f \
  --manifest service.yaml \
  --fixtures $MEDICALOS_FIXTURE_CACHE \
  --report conformance-report.json
```

It executes, in order:

| step | check |
|---|---|
| K1 | `service.yaml` validates against `medos/schemas/service/service.yaml.json`; `legal_manufacturer`, `intended_use`, `known_limitations`, `input_constraints` and `SeriesSelector` are present and non-empty |
| K2 | the image starts, answers the health endpoint within `startup_timeout_s`, and declares the same capabilities as the manifest |
| K3 | the preprocessing golden-fixture self-test passes (the tensor hash shipped with the artifact reproduces) |
| K4 | for each of `fx-effusion-thin`, `fx-multikernel-chest`, `fx-coronal-reformat`: the service returns a `ResultBundle` validating against `medos/schemas/service/result-bundle.json` |
| K5 | every label map in the bundle is in the canonical volume geometry supplied in the job input (origin/spacing/direction within `1e-4`) |
| K6 | every finding's `source_sop_instance_uids` is a subset of the instances supplied |
| K7 | every measurement carries a UCUM unit and a coded concept resolving in `data/coding/concepts.json` |
| K8 | for `fx-localizer-only`, `fx-single-instance`, `fx-wrong-bodypart`: the service returns a structured refusal, never a bundle |
| K9 | the service performs no egress: with the network policy `deny-all` except the platform callback, the run completes; any attempted connection to the PACS, database, broker or object store is recorded and fails the step |
| K10 | determinism: two runs on `fx-effusion-thin` produce bundles equal under the tolerance the manifest declares (`deterministic: true` ⇒ byte-identical masks; `deterministic: false` ⇒ Dice ≥ 0.999 and every measurement within 0.5 %) |

**MOS-TEST-048** — The conformance report is signed by the platform key and referenced by `ServiceVersion.conformance_report_id`. Chapter 6's deployment gate MUST refuse any `ServiceVersion` whose conformance report is missing, unsigned, or produced against a different image digest.

#### 14.5.7 The training side of `C-PREP` (Chapter 17)

**MOS-TEST-083** — `C-PREP` is a two-sided boundary and the training side is checked here. `L1-TRAIN-001` asserts the round-trip property of `MOS-TRAIN-066`: for every spec fixture under `medos/contracts/C-PREP/samples/valid/`, `build_chain(parse(serialize(spec)))` MUST produce a `Compose` whose transform class names, order and constructor arguments are structurally equal to those of `build_chain(spec)`. Mutant: drop one `PreprocessingSpec` field from `serialize` — the check must fail naming the field that never reached the chain.

**MOS-TEST-084** — `L1-TRAIN-002` asserts the orientation identity of `MOS-TRAIN-042` rather than assuming it: for every spec fixture, run `build_chain` on a synthetic volume with a known affine and assert `nibabel.orientations.aff2axcodes(out.affine) == spec.orientation_target`. This is an L1 check on purpose — a silently mirrored volume is the one failure that passes every downstream structural check, D1–D7 included. Mutant: insert an axis-flip conversion table into `build_chain` (which `MOS-TRAIN-041` forbids) — the check must fail on the axcodes comparison.

**MOS-TEST-085** — A scalar `Spacingd` `mode` in a chain that carries a label key is a CI failure, never a warning (`MOS-TRAIN-043`, `MOS-TRAIN-044`). `L0-TRAIN-003` is static over `medicalos_preprocessing`; it is paired with an L1 assertion that, for every spec fixture declaring a label key, the `mode` argument reaching `Spacingd` is a tuple aligned to `keys` whose label entries are an interpolator `MOS-IMG-034` permits. The defect this catches is a label value outside `io.label_set` surfacing as a rare `FAILED` job months after the model ships.

**MOS-TEST-086** — `L2-TRAIN-004` re-runs the `MOS-IMG-054` self-test on a **clean checkout**, in a container built from the pinned `backend.resampler` and `backend.numpy` versions, on CPU, and compares against the `golden_fixture.output_tensor_sha256` and `golden_fixture.output_shape` written by `record_golden` (`MOS-TRAIN-059`, `MOS-TRAIN-060`). It MUST block artifact signing in the Chapter 17 pipeline (`MOS-TRAIN-062`); it belongs to that pipeline and is not one of the four platform pipelines of §14.8. A mismatch MUST NOT be closed by re-recording the hash (`MOS-TRAIN-065`). Mutant: bump the pinned resampler version without a new spec version — the check must fail on the tensor hash.

### 14.6 Failure and fault-injection tests

#### 14.6.1 Fault points

**MOS-TEST-051** — Crash windows are tested by named, committed fault points, not by racing a `kill -9`. The registry is exhaustive and fixed:

| fault point id | site | what it proves |
|---|---|---|
| `gateway.after_deid_before_audit` | gateway, after de-identification, before writing the `AuditEvent` | audit is not silently lost; the request fails closed |
| `triage.after_job_insert_before_commit` | triage, inside the job+queue transaction | Q10 atomicity; no orphan queue entry |
| `worker.after_claim_before_heartbeat` | worker, after `Claim`, before the first `Heartbeat` | lease expiry re-offers the job |
| `worker.after_inference_before_store` | worker, after the `ResultBundle` is in memory | the retry re-runs inference and produces identical identity |
| `worker.after_stow_before_complete` | worker, after STOW-RS returns 200, before the `results` insert and `COMPLETED` transition | **the window the previous specification could not detect** |
| `worker.after_result_insert_before_commit` | worker, inside the result+transition transaction | no `COMPLETED` job without a `results` row, and no `results` row without `COMPLETED` |
| `relay.after_produce_before_mark_sent` | outbox relay (0.3 Kafka driver) | at-least-once produce, deduplicated by `event_id` |
| `review.after_decision_before_commit` | `ResultReview` write | a review is never half-recorded |

**MOS-TEST-052** — Fault points MUST be absent from release images. Go code lives behind `//go:build faultinject`; Python behind the separate distribution `medicalos-faultinject`, which is not installed in release images. CI job `no-fault-points-in-release` asserts, for every published image:

```bash
docker run --rm --entrypoint sh "$IMAGE" -c '
  ! strings /usr/local/bin/medicalos-core | grep -q "MEDICALOS_FAULT_INJECT" &&
  ! python -c "import medicalos_faultinject" 2>/dev/null
' || { echo "FAIL: fault-injection code present in release image $IMAGE"; exit 1; }
```

The activation syntax is `MEDICALOS_FAULT_INJECT="<point-id>:<action>[:<nth>]"` with actions `exit137`, `panic`, `hang`, `error`, and `<nth>` selecting which traversal triggers (default 1).

#### 14.6.2 Failure matrix

**MOS-TEST-053** — The following failure tests are mandatory at L4.

| id | fault | expected behaviour | asserted surfaces |
|---|---|---|---|
| FT-01 | PostgreSQL unavailable at job creation | `POST /jobs` returns 503 problem+json `class: system`; no queue entry; no event | API, queue table, bus |
| FT-02 | PACS unavailable during retrieval | job retries per policy, then `FAILED` with `class: system` and reason `pacs_unavailable`; no partial DICOM written | job row, PACS, bus |
| FT-03 | Inference backend unavailable | job `FAILED`, not `REJECTED`; distinct reason code; `REJECTED` count unchanged | job row, metrics |
| FT-04 | Inference OOM | job `FAILED` with `reason=resource_exhausted`; the worker survives and claims the next job within 30 s | job row, worker liveness |
| FT-05 | Broker/queue unavailable mid-run | worker finishes the in-flight job; state is still correct in PostgreSQL (MOS-EXEC-002); events replay on recovery | PostgreSQL, bus |
| FT-06 | Duplicate delivery of the same queue message | see AT-05 | three surfaces |
| FT-07 | `worker.after_stow_before_complete` | see §14.6.4 | three surfaces + reuse observable |
| FT-08 | `worker.after_result_insert_before_commit` | neither a `results` row nor a `COMPLETED` job exists after crash; the retry produces exactly one of each | PostgreSQL |
| FT-09 | Lease expiry with a live worker (split brain) | the slow worker's `Complete` is rejected with `stale_lease`; exactly one result on all three surfaces | queue, PostgreSQL, PACS |
| FT-10 | Corrupt source pixel data (`fx-corrupt-pixeldata`) | `FAILED` with `source_data_unreadable`; no SEG written; no zero-filled volume | job row, PACS |
| FT-11 | Service returns a `ResultBundle` failing schema validation | job `FAILED` with `class: system` and the violated schema keyword recorded; no DICOM written | job row, PACS |
| FT-12 | Service returns a mask in model space rather than canonical geometry | rejected by the geometry assertion before any DICOM is written | job row, PACS |
| FT-13 | Policy decision point unavailable | every gated operation is **denied**; no request proceeds; the circuit breaker MUST NOT be applied to the PDP path | API, audit |
| FT-14 | Audit sink write fails | the audited action fails closed and returns 503; no un-audited mutation is committed | PostgreSQL, audit |
| FT-15 | De-identification policy cannot be loaded | the gateway refuses all retrieval; no un-de-identified byte leaves the gateway | gateway, audit |
| FT-16 | Object store unavailable | artifact writes fail; job `FAILED`; no dangling artifact rows | PostgreSQL, object store |
| FT-17 | Clock skew of +90 s on one worker | leases and deadlines remain correct (absolute deadlines only); no premature expiry | queue |
| FT-18 | Queue full / admission control rejects | `attempt` unchanged (Q08); the job is retried later and completes | queue, PostgreSQL |

**MOS-TEST-060** — Timeout nesting MUST be asserted, not documented: `L1-EXEC-022` reads the effective configuration and fails unless every enclosing timeout exceeds the sum of its children plus the declared margin (HTTP client → inference call → job step → job → workflow).

#### 14.6.3 The reuse observable

**MOS-TEST-055** — Resumption after a crash is only testable if "produced now" and "reused from a previous attempt" are distinguishable from outside. The platform MUST expose at least one such observable; both of the following are required and asserted:

1. a `JobEvent` of type `result.dicom.reused` carrying `series_instance_uid` and `output_index`, emitted when Chapter 4's skip-if-present check finds the derived series already complete;
2. the counter `medicalos_inference_invocations_total{tenant_id,service_id,service_version}` (Chapter 13), which MUST increment exactly once per actual inference execution.

A platform where a retry silently re-runs inference and overwrites identical objects is indistinguishable from a correct one without (1) and (2), which is precisely how the previous specification's `§85` passed.

#### 14.6.4 FT-07 — the crash between store and complete

This is the window the previous specification's crash test could not reach: it killed the worker during inference, which is the easy case.

```yaml
# tests/failure/FT-07.yaml
id: FT-07
title: Crash after STOW-RS succeeds and before the result row and COMPLETED transition commit
level: L4
fixtures: [fx-effusion-thin]
service: pleural-effusion@1.2.0
fault: "worker.after_stow_before_complete:exit137"
timeout_s: 600
steps:
  - id: P1
    action: POST /api/v1/jobs
    body: {target: {kind: service, id: pleural-effusion, version: "1.2.0"},
           input: {study_instance_uid: "${fx-effusion-thin.study_instance_uid}",
                   prior_study_instance_uids: []},
           idempotency_key: "ft07-${run_id}"}
    assert: {status: 202, capture: {job_id: "$.job_id"}}
  - id: P2
    action: wait_until
    condition: "qido(study, SeriesNumber>=9000) length == 1"
    deadline_s: 240
  - id: P3                                  # the process is now dead, mid-window
    assert:
      - "sql: SELECT state FROM jobs WHERE id='${job_id}' -> 'RUNNING'"
      - "sql: SELECT count(*) FROM results WHERE job_id='${job_id}' -> 0"
      - "container_exit_code(worker) == 137"
  - id: P4
    action: restart_worker
    env: {MEDICALOS_FAULT_INJECT: ""}
  - id: P5
    action: wait_until
    condition: "sql: SELECT state FROM jobs WHERE id='${job_id}' -> 'COMPLETED'"
    deadline_s: 300
  - id: S1                                   # surface 1: control plane
    assert:
      - "sql: SELECT count(*) FROM result_dicom_objects rdo JOIN results r ON r.id=rdo.result_id
              WHERE r.job_id='${job_id}' AND rdo.object_kind='SEG' -> 1"
      - "sql: SELECT count(*) FROM result_dicom_objects rdo JOIN results r ON r.id=rdo.result_id
              WHERE r.job_id='${job_id}' AND rdo.object_kind='SR'  -> 1"
      - "constraint_exists: results UNIQUE (job_id, capability_id)"
  - id: S2                                   # surface 2: imaging plane
    assert:
      - "qido(study, SeriesNumber>=9000) length == 2"
      - "qido(study, SeriesInstanceUID=derived_uid(SEG)).NumberOfSeriesRelatedInstances == 1"
      - "qido(study, SeriesInstanceUID=derived_uid(SR)).NumberOfSeriesRelatedInstances == 1"
      - "sop_instance_uids(derived SEG series) == {derived_sop_uid(SEG, 0)}"
  - id: S3                                   # surface 3: transport plane
    assert:
      - "bus: count(medicalos.jobs.completed where job_id='${job_id}') == 1"
      - "sse: GET /api/v1/jobs/${job_id}/events yields exactly one terminal event"
      - "webhooks: distinct event_id count == 1 (delivery attempts may exceed 1)"
  - id: S4                                   # surface 4: resumption, not re-execution
    assert:
      - "job_events contains exactly one event of type 'result.dicom.reused'"
      - "delta(medicalos_inference_invocations_total{service_id='pleural-effusion'}) == 1"
mutants:
  - id: M1
    description: mint a random SeriesInstanceUID instead of deriving it deterministically
    patch: tests/mutants/FT-07-M1.diff
    must_fail_step: S2
  - id: M2
    description: insert the results row in a separate transaction from the COMPLETED transition
    patch: tests/mutants/FT-07-M2.diff
    must_fail_step: S1
  - id: M3
    description: skip the QIDO-RS skip-if-present check and always re-infer
    patch: tests/mutants/FT-07-M3.diff
    must_fail_step: S4
  - id: M4
    description: publish the completion event once per attempt
    patch: tests/mutants/FT-07-M4.diff
    must_fail_step: S3
```

**MOS-TEST-054** — FT-07 and its four mutants are release-blocking for 0.1.0 and every release thereafter.

#### 14.6.5 Fail-closed tests

**MOS-TEST-057** — FT-13, FT-14 and FT-15 assert *fail-closed*, which is the opposite of the default failure mode of a naive implementation. Each MUST additionally assert that no successful operation occurred during the outage window: `SELECT count(*) FROM audit_events WHERE ts BETWEEN outage_start AND outage_end AND outcome = 'success'` returns 0 for the gated action class.

**MOS-TEST-058** — DLQ semantics: a message may enter `<topic>.dlq` only after the declared attempt budget is exhausted by *retryable* failures. `FT-18` asserts that a load-shed rejection does not consume budget, and `L4-EXEC-041` asserts that every DLQ entry carries the full failure history (`attempt`, last error `class`, last error detail, first and last timestamps).

#### 14.6.6 PHI containment

**MOS-TEST-056** — The PHI token set for a fixture is `{PatientName, PatientID, AccessionNumber, PatientBirthDate, StudyInstanceUID(source), every source SeriesInstanceUID, every source SOPInstanceUID, InstitutionName, ReferringPhysicianName}` as present in the *pre-de-identification* file. During AT-09 the harness collects: all container stdout/stderr, all OTLP spans (attributes and events), all Prometheus metric label values, all event-bus payloads, all webhook bodies, and all outbound LLM request bodies; and asserts zero occurrences of any token. The `--selftest` positive control (MOS-TEST-007) is mandatory.

#### 14.6.7 Soak

**MOS-TEST-059** — A nightly 8-hour soak MUST run 2000 jobs across 3 tenants over the full corpus with a 2 % random fault injection drawn from the registry, and assert at the end: job count in a terminal state equals job count created; zero jobs in `RUNNING`; for every `COMPLETED` job, exactly one `results` row per `capability_id` and one derived series per generated DICOM object kind (`result_dicom_objects.object_kind`, chapter 12); RSS growth of every process < 10 % over the window; no `audit_events` gap in the per-tenant monotonic sequence.

### 14.7 Acceptance tests

#### 14.7.1 Format and registry

**MOS-TEST-061** — Every acceptance test is a file `acceptance/AT-<nn>.yaml` conforming to `medos/schemas/acceptance/test.json`, with the fields shown in FT-07 above plus `gates_release`, `requirements` (the `MOS-*` ids it covers) and `surfaces`. The prose in this chapter is a rendering of those files; the files are authoritative and the `acceptance-render` CI job asserts the table below matches them.

**MOS-TEST-062** — The registry is binding. A release MUST NOT be tagged while any acceptance test gating that release or an earlier one is failing, quarantined, or has a failing mutant.

**MOS-TEST-063** — Every acceptance test MUST name its surfaces from the closed set `{http_api, postgres, dicomweb, object_store, event_bus, sse, webhook, metrics, traces, logs, viewer, filesystem, network, git}`. An assertion that reads a surface the test did not declare fails the `acceptance-lint` job.

| id | title | gates | surfaces |
|---|---|---|---|
| AT-01 | Stack starts, self-reports ready, and every image is digest-pinned | 0.1.0 | http_api, postgres, dicomweb, filesystem |
| AT-02 | Job creation is one endpoint, returns 202, and is durable before the response | 0.1.0 | http_api, postgres, event_bus |
| AT-03 | Triage matches the selector oracle on the whole corpus | 0.1.0 | http_api, postgres |
| AT-04 | A native-mode run produces DICOM passing D1–D7 on every eligible fixture | 0.1.0 | dicomweb, filesystem |
| AT-05 | Duplicate delivery and the store/complete crash window yield exactly one result on three surfaces | 0.1.0 | postgres, dicomweb, event_bus, sse, webhook, metrics |
| AT-06 | Worker killed mid-inference: the job survives, retries and completes | 0.1.0 | postgres, dicomweb, event_bus |
| AT-07 | No eligible series ⇒ `REJECTED` with a machine-readable reason, visually distinct from `FAILED` | 0.1.0 | http_api, postgres, viewer |
| AT-08 | Tenant isolation holds on the API, the imaging plane, the database and the network | 0.1.0 | http_api, dicomweb, postgres, object_store, network |
| AT-09 | No PHI in logs, spans, metrics, events, webhooks or prompts | 0.1.0 | logs, traces, metrics, event_bus, webhook |
| AT-10 | A completed job replays from its provenance record alone | 0.1.0 | postgres, filesystem, dicomweb |
| AT-11 | The demo: the result renders aligned and the report hydrates in a pinned viewer | 0.1.0 | viewer, http_api, dicomweb |
| AT-12 | A `ServiceVersion` failing its `AcceptanceCriteria` cannot reach `lifecycle_status = APPROVED`, and no `Deployment` of it can reach `state = SERVING` with `clinical_use_mode = clinical` | 0.2.0 | http_api, postgres |
| AT-13 | `research_only` output is marked and excluded from the clinical read path | 0.2.0 | dicomweb, http_api, viewer |
| AT-14 | A `ValidationReport` verifies offline on a machine with no platform access | 0.2.0 | filesystem |
| AT-15 | Preprocessing golden-fixture mismatch causes the worker to refuse to serve | 0.2.0 | logs, http_api, metrics |
| AT-16 | `ResultReview` is recorded without delaying `COMPLETED` | 0.2.0 | http_api, postgres, event_bus |
| AT-17 | Resolution is pure, pinned at job creation and replayed unchanged on retry | 0.3.0 | http_api, postgres |
| AT-18 | **A second capability is added with zero core code changes** | 0.3.0 | git, http_api, postgres, dicomweb |
| AT-19 | A sealed-mode service is governed identically to a native one | 0.3.0 | network, dicomweb, postgres |
| AT-20 | The Kafka driver passes Q01–Q14 unmodified and AT-05 still holds | 0.3.0 | event_bus, postgres, dicomweb |
| AT-21 | An arriving study is triaged, analysed and stored unattended | 0.4.0 | dicomweb, postgres, event_bus |
| AT-22 | An independent viewer reads the SEG and the SR | 0.4.0 | filesystem, viewer |
| AT-23 | Narrative generation fails closed on an unmatched numeric or laterality token | 0.4.0 | http_api, postgres, logs |
| AT-24 | A passing training-pipeline run promotes nothing: the candidate stops at `VALIDATED` until a human approves | 0.2.0 | http_api, postgres |

#### 14.7.2 AT-07 — clinical rejection is not an error

**MOS-TEST-064** — AT-07 runs every fixture whose oracle expects `REJECTED` and asserts, per fixture: `jobs.state = 'REJECTED'`; `jobs.reject_reason_code` equals the oracle's reason code (the DB column is `state`, `MOS-EXEC-001`; `status` is the JSON field name of `MOS-API-049`); `GET /api/v1/jobs/{id}` returns the reason and a human-readable explanation; the RFC 9457 body for the corresponding synchronous error carries `class: clinical_rejection`; zero DICOM objects exist in the study with `SeriesNumber >= 9000`; and the viewer surface renders `REJECTED` with a different icon, colour token and label than `FAILED` (asserted by comparing the rendered DOM node's `data-status` attribute and computed colour between a `REJECTED` and a `FAILED` job — they MUST differ on both). Mutant: map `REJECTED` onto `FAILED` in the API serializer — AT-07 must fail on the status assertion and on the viewer assertion.

#### 14.7.3 AT-05 — exactly one result, on three surfaces

This test replaces the previous `§84`, which asserted "no duplicate result" without naming where it looked. It runs two scenarios against the same assertions: **A** the same queue message delivered three times; **B** the FT-07 crash window.

**MOS-TEST-065** — AT-05 MUST assert all of the following. Failing to read any one of the three planes is the defect being fixed.

| surface | assertion |
|---|---|
| **S1 — control plane (PostgreSQL)** | `SELECT count(*) FROM result_dicom_objects rdo JOIN results r ON r.id = rdo.result_id WHERE r.job_id = J GROUP BY rdo.object_kind` is 1 for each of `SEG`, `SR` — the DICOM object kind is `result_dicom_objects.object_kind` (chapter 12), never `results.result_kind`, which is the clinical output category; the constraint `UNIQUE (job_id, capability_id)` (`results_job_capability_uk`, chapter 12) exists in `pg_constraint`; `jobs.state = 'COMPLETED'`; exactly one `COMPLETED` transition row in `job_events` |
| **S2 — imaging plane (DICOMweb, through the gateway)** | QIDO-RS on the study returns exactly 2 series with `SeriesNumber >= 9000`; each returns `NumberOfSeriesRelatedInstances` equal to the expected count; the `SeriesInstanceUID` of each equals `derive_uid(...)` of MOS-IMG-062 with the job's full argument tuple and `uid_kind` of `seg.series` and `sr.series` respectively — the two MUST differ, which is the property `uid_kind` exists to guarantee; the set of `SOPInstanceUID`s in each equals the derived set; no instance in the study has a `SeriesInstanceUID` that is derived-but-unexpected |
| **S3 — transport plane (event bus, SSE, webhooks)** | exactly one envelope on `medicalos.jobs.completed` with `job_id = J`; the SSE replay of `GET /api/v1/jobs/J/events` contains exactly one terminal event; webhook deliveries may repeat but carry exactly one distinct `event_id` |
| **S4 — execution (metrics + job events)** | `delta(medicalos_inference_invocations_total) == 1` across the whole scenario; scenario B additionally has exactly one `result.dicom.reused` event |

Declared mutants: **M1** random `SeriesInstanceUID` (fails S2); **M2** result row and transition in separate transactions (fails S1); **M3** no skip-if-present check (fails S4); **M4** completion event published per attempt (fails S3); **M5** drop the `UNIQUE` constraint (fails S1).

#### 14.7.4 AT-08 — tenant isolation on four planes

This replaces the previous `§87`, which tested a 403 on the REST plane only — the plane where no patient pixels live.

**MOS-TEST-066** — Setup: tenants `A` and `B`; `B` owns `fx-effusion-thin` with a completed job and its generated SEG and SR; `A` owns `fx-multikernel-chest`. All requests use `A`'s credentials.

| plane | request | pass condition |
|---|---|---|
| **P1 REST** | `GET /api/v1/studies/{B.study_uid}`, `GET /api/v1/jobs/{B.job_id}`, `GET /api/v1/results/{B.result_id}` | see MOS-TEST-067 below; never 200; the response body contains none of `B`'s identifiers |
| **P2 DICOMweb — direct QIDO-RS for another tenant's study** | `GET {gateway}/dicom-web/studies?StudyInstanceUID={B.study_uid}` | zero matching results (HTTP 204, or 200 with an empty array). `A` MUST NOT learn that the study exists |
| **P2b DICOMweb — resource-addressed** | `GET {gateway}/dicom-web/studies/{B.study_uid}/series`, `.../series/{B.seg_series_uid}/instances/{B.seg_sop_uid}` (WADO-RS), and a STOW-RS of a new instance into `{B.study_uid}` | non-2xx, indistinguishable from a non-existent study (MOS-TEST-067); zero bytes of `B`'s pixel or header data in the response; the STOW is not persisted (verified by QIDO as `B`) |
| **P3 database (RLS)** | as the application role: `SET LOCAL app.tenant_id = '<A>'; SELECT count(*) FROM studies WHERE study_instance_uid = '<B.study_uid>'` | returns `0`. With no `app.tenant_id` set, the query MUST raise, not return rows |
| **P3b schema-wide RLS** | for every table in `information_schema.columns` having a `tenant_id` column: `pg_class.relrowsecurity AND pg_class.relforcerowsecurity` | true for every such table. This catches a new table added without a policy |
| **P4 network** | from inside the service container's network namespace and from the OHIF container: TCP connect to the PACS `4242`/`8042` and to PostgreSQL `5432` | connection refused or timed out. The gateway holds the only PACS credential (Chapter 3, Chapter 8) |
| **P5 object store** | a presigned URL issued to `A` used against a `B`-prefixed object key | 403 from the object store, not the application |
| **P6 audit** | after P1, P2, P2b | one `AuditEvent` per denied request with `outcome = 'denied'`, the requesting tenant, the requested resource identifier, and the `trace_id`; the events are visible to `SUPER_ADMIN` and not to `A` |

**MOS-TEST-067 (non-disclosure equivalence)** — For every resource-addressed cross-tenant request, the response MUST be byte-identical to the response for a resource that does not exist at all, except for the RFC 9457 `instance` member and any trace identifier. The test issues both requests and diffs the normalised bodies plus status codes. This makes the check independent of whether Chapter 8 chose 403 or 404, while closing the existence oracle that a naive `403 for exists / 404 for not-exists` split creates.

Declared mutants: **M1** disable RLS on `studies` (fails P3); **M2** make QIDO-RS return unfiltered results (fails P2); **M3** put OHIF on the same network as the PACS (fails P4); **M4** return 403 for existing and 404 for non-existing cross-tenant resources (fails P2b via MOS-TEST-067); **M5** drop the denial audit write (fails P6).

#### 14.7.5 AT-11 — render and hydrate, not "displays"

This replaces the previous `§109` step 11, "OHIF отображает результаты", which no automated system could fail. AT-11 is the automation of Chapter 4's pinned-viewer rendering check `~~MOS-IMG-157~~ MOS-IMG-157a`: its release gating is owned here — 0.1.0, release-blocking under MOS-TEST-062 — and Chapter 4 `MOS-IMG-159` MUST NOT be read as relaxing it.

**MOS-TEST-068 (pinning)** — The viewer under test MUST be pinned by digest. `medos/deploy/compose/ohif.lock` records `image`, `tag: v3.9.2`, `digest`, `mode: @ohif/mode-longitudinal`, and `extensions: ["@ohif/extension-cornerstone-dicom-seg", "@ohif/extension-cornerstone-dicom-sr"]`. CI job `viewer-pin` asserts the running container's `RepoDigest` equals the lock. Selectors used by the test live in `tests/e2e/viewer-selectors.yaml`, so a viewer upgrade changes a data file and a digest, never test logic.

**MOS-TEST-069 (render — self-calibrating alignment)** — "The overlay appears" is not sufficient; a SEG misregistered by one slice also appears. The test measures the viewport transform rather than assuming it:

1. Set the browser viewport to exactly 1600 × 1000 and load the study in a single-viewport layout at the slice with `InstanceNumber == 100`.
2. STOW `fx-calib-seg` — five single-voxel markers at the four image corners and the centre of that slice. Screenshot the viewport with the segmentation hidden and again with it shown (toggled via the segmentation panel's visibility control). Difference the two images; five connected components MUST be found.
3. Least-squares fit the 2-D affine mapping image pixel coordinates → canvas pixel coordinates from those five correspondences. Residual MUST be < 0.5 canvas px; otherwise the viewport is not in a measurable state and the test fails rather than guessing.
4. Remove the calibration SEG. Repeat the hide/show difference for the **real** generated SEG on the same slice. Map the reference mask through the fitted affine.
5. Assert: changed-pixel count > 0; centroid of the rendered overlay within **1.0 image pixel** of the mapped mask centroid; IoU of the rendered overlay region against the mapped mask ≥ **0.90**.
6. Repeat on two further slices chosen as the first and last non-empty slice of the mask, so a z-offset cannot hide in the middle of the volume.

Mandatory negative control: the same assertions run against a SEG shifted by exactly one slice in z MUST fail at step 5 on at least one of the three slices. Without this control the alignment assertion is not known to be sensitive.

**MOS-TEST-070 (hydrate)** — For the SR:

1. Open the SR series; activate the measurement-hydration control named in `viewer-selectors.yaml`.
2. Assert a WADO-RS `GET` for the SR `SOPInstanceUID` returned 200 before the panel populated (read from the page's network log).
3. Assert the measurement panel contains **exactly** `N` rows, where `N` is the number of measurements in the `Result` row for the job — not "at least one".
4. For each row: the displayed label is the `CodeMeaning` of that measurement's coded concept in `data/coding/concepts.json`; the displayed unit string is the UCUM code's display form; the displayed numeric value satisfies `|displayed − stored| ≤ 0.5 × 10^(−d)` where `d` is the number of decimals rendered.
5. Assert clicking a row navigates the image viewport to a slice within the referenced segment's z-extent.
6. Negative control: an SR whose `ContentTemplateSequence` has been rewritten to `TemplateIdentifier: "1410"` MUST produce zero rows, and the test MUST observe zero rows for it.

**MOS-TEST-071 (provenance panel)** — The provenance panel MUST render, and the test MUST assert the presence and exact value of every one of: `job_id`; `service_id`; `service_version`; `model_id` and `model_version` (native mode) or image digest (sealed mode); `preprocessing_spec_version`; `resolved_by` precedence rule that selected the version; `input_series_instance_uids`; `input_instance_count`; `evaluation_run_id` and `validation_report_id`; `clinical_use_mode`; `legal_manufacturer`; `started_at`, `completed_at`, `duration_ms`; `container_image_digest`; `accelerator` block. Each is compared against the value read from `GET /api/v1/jobs/{id}` — the panel MUST NOT be able to pass by rendering placeholders. A missing or empty field fails the test.

**MOS-TEST-072 (marking is visible)** — When `clinical_use_mode: research_only`, the series-panel entry for the generated SEG MUST display a `SeriesDescription` beginning with the configured RUO prefix, asserted on the rendered text, not the DICOM file.

Declared mutants for AT-11: **M1** shift the SEG by one slice (fails render); **M2** write measurements to the SR without UCUM units (fails hydrate step 4 and D4); **M3** render the provenance panel from a hard-coded object (fails MOS-TEST-071's value comparison); **M4** drop the RUO prefix (fails MOS-TEST-072).

#### 14.7.6 AT-18 — a second capability with zero core code changes

This is the platform acceptance test. `§107` of the previous specification declared capability resolution "the main criterion of the project" and specified it as a verb; nothing could fail. Here it is a `git diff`.

**MOS-TEST-073** — Procedure. The second capability is `lung_nodule` on `fx-nodule-4reader` (LIDC-IDRI), per the 0.3.0 release plan.

1. CI records `BASE = git rev-parse origin/main` before the branch's first commit.
2. On a branch, integrate the capability end to end using **only** the public surfaces: publish an OCI image and `service.yaml` to the artifact registry; `POST /api/v1/capabilities`; `POST /api/v1/service-versions`; create a `DatasetVersion`, `AnnotationSet` (≥2-reader consensus), `DatasetSplit`, run an `EvaluationRun`, record `AcceptanceCriteria` on the capability, produce a `ValidationReport`; `POST /api/v1/deployments`; submit a job.
3. The run MUST produce a SEG and an SR that pass D1–D7, and the selector oracle entry for `(fx-nodule-4reader, lung_nodule)` MUST be satisfied.

**MOS-TEST-074 (the falsifiable part)** — The following four assertions are the test:

```bash
# acceptance/AT-18/assert.sh
set -euo pipefail
BASE="$(cat .ci/at18-base-sha)"
CORE="$(tr '\n' ' ' < .ci/core-paths.txt)"

# A1. Zero changed lines under any core path.
if ! git diff --quiet "$BASE"...HEAD -- $CORE; then
  echo "AT-18 FAIL: core code changed"; git diff --stat "$BASE"...HEAD -- $CORE; exit 1
fi

# A2. Every changed path is inside the allowed set.
git diff --name-only "$BASE"...HEAD \
  | grep -vE '^(medos/services/lung-nodule/|tests/fixtures/|acceptance/AT-18/|docs/)' \
  | { ! grep . ; } || { echo "AT-18 FAIL: change outside medos/services/, fixtures/, docs/"; exit 1; }

# A3. No new database migration.
test "$(ls -1 control-plane/migrations | wc -l)" \
   = "$(git show "$BASE":control-plane/migrations --name-only | tail -n +3 | grep -c .)" \
  || { echo "AT-18 FAIL: a migration was required"; exit 1; }

# A4. Every HTTP request the integration made hit a documented public path.
python acceptance/AT-18/check_calls.py \
  --recorded artifacts/AT-18/http-calls.jsonl \
  --openapi docs/api/openapi.yaml \
  || { echo "AT-18 FAIL: integration used an undocumented or internal endpoint"; exit 1; }
```

`.ci/core-paths.txt` is itself binding and is exactly:

```
control-plane/
gateway/
worker/
imaging/
dataplane/
registry/
evidence/
security/
runtime/
sdk/
medos/web/
deploy/
medos/schemas/
```

**MOS-TEST-075** — `check_calls.py` asserts that every recorded request's `(method, path template)` exists in the generated `openapi.yaml` and that no request targeted a host other than the public API, the artifact registry or the DICOM Gateway. This is what prevents the integration from "succeeding" via a private patch, a direct database write, or an undocumented admin endpoint — the mechanism that makes "platform" mean something.

**MOS-TEST-076** — AT-18 MUST also be run in its *negative* form once per release: a deliberately unsupported integration (a capability requiring a `SeriesSelector` predicate the schema cannot express) MUST fail at step 2 with a schema validation error naming the missing field, and MUST NOT be workable by editing core. The negative form proves the test can distinguish "the platform supports this" from "the platform was edited to support this".

Declared mutants: **M1** add a `case "lung_nodule":` branch in the worker (fails A1); **M2** add a migration for a nodule-specific table (fails A3); **M3** call an internal admin endpoint during integration (fails A4).

#### 14.7.7 AT-10 — provenance replay

**MOS-TEST-077** — Given only the provenance record of a completed job, a replay tool MUST reconstruct the run. `medicalos replay --job <id> --out ./replay` reads the record, re-fetches the named source instances by SOP Instance UID, pins the same image digest, preprocessing spec version and resolved service version, and re-executes. Assertions:

- if `ServiceVersion.deterministic == true`: every generated object is byte-identical to the stored one after removing `(0008,0012)`, `(0008,0013)`, `(0008,0023)`, `(0008,0033)` — and only those tags;
- if `deterministic == false`: Dice between the replayed and the stored mask ≥ 0.999, every reported measurement within 0.5 %, and every coded concept identical;
- in both cases the derived `SeriesInstanceUID` and `SOPInstanceUID`s are byte-identical, because identity does not depend on pixels.

If this test cannot be made to pass, Chapter 9's reproducibility claim MUST be weakened rather than left standing.

#### 14.7.8 AT-24 — a passing run promotes nothing

**MOS-TEST-087** — AT-24 is the end-to-end negative test `MOS-TRAIN-018` requires: automation that produces a good candidate must still change nothing that serves. It drives a complete Chapter 17 pipeline run to a `PASS` verdict and asserts: the candidate `artifact` row is at `lifecycle_status = 'VALIDATED'` and was never written to `APPROVED`; the `deployments` table is byte-identical to a full snapshot taken before the run (a snapshot comparison, not a row count); the incumbent `ServiceVersion` is still the single `role = 'ACTIVE' AND state = 'SERVING'` row in its `(tenant_id, environment, capability_id)` slot; every job created after the run resolved to the incumbent and none to the candidate; and the pipeline's service account holds `artifact.publish` and `evidence.run` but not `artifact.approve` (`MOS-TRAIN-139`). The candidate begins receiving traffic only after an `artifact.approve` call by a principal with a human identity satisfying `MOS-EVID-117`, and the same call made with the pipeline's service-account identity MUST be refused. Mutant: grant `artifact.approve` to the pipeline service account — AT-24 must fail on the grant assertion and on the `VALIDATED` assertion.

### 14.8 CI topology and gates

**MOS-TEST-078** — Four pipelines, with fixed contents:

| pipeline | trigger | jobs | blocks |
|---|---|---|---|
| `pr` | every push to a PR | L0, L1, L2, L3, `traceability`, `stages`, `fixture-licence`, `fixtures-lock`, `schema-compat`, `viewer-pin`, `phi-scan` | merge |
| `main` | merge to `main` | everything in `pr`, plus L4 (FT-01…FT-18), L5 (all acceptance tests gating the current or an earlier release), `no-fault-points-in-release` | further merges if red for > 2 h |
| `nightly` | 02:00 UTC | `mutants` (every declared mutant), D6 against a second DICOMweb server, soak, `fx-huge-study` throughput, tier-P corpus re-verification | nothing directly; a red nightly blocks the next release |
| `release` | tag | everything above, plus mutation score, the AT-18 negative form, SBOM, image signing, `ValidationReport` offline verification | the tag |

**MOS-TEST-079** — Required status checks on `main` are exactly the `pr` pipeline jobs plus `acceptance-lint` and `acceptance-render`. A check may be added to this list at any time; removing one requires the same review as a specification change.

**MOS-TEST-080 (definition of done)** — A change is done only when: it is covered by a check at the lowest decidable level; any acceptance test it touches has an updated mutant that fails; `tests/traceability.yaml` maps every requirement it introduces; and no check was moved to a higher level or quarantined to make it pass.

**MOS-TEST-081** — CI artifacts MUST be retained for 365 days for `release` runs and 30 days for others, and MUST include: the DICOM battery reports, the conformance reports, the acceptance test YAML as executed, the mutant results, the recorded HTTP call log for AT-18, and the `corpus.lock` in force. A release whose artifacts are not retrievable cannot be audited and MUST NOT be cited in a `ValidationReport`.

### Acceptance criteria

1. `tests/traceability.yaml` maps every `MOS-<AREA>-<NNN>` id defined anywhere in this document to at least one check id, and the `traceability` job exits 0. Deleting any one mapping makes it exit non-zero.
2. `tests/stages.yaml` contains no stage with an empty check list, and the `stages` job exits 0. Adding a stage named `DICOM TEST` with zero checks makes it exit non-zero.
3. `make mutants` applies every declared mutant in `tests/mutants/`, runs the owning check, and reports 100 % of mutants detected. Any mutant that the owning check survives is printed as `SURVIVED <check-id> <mutant-id>` and the job exits non-zero.
4. `medos/tools/fixtures/build.py --verify` reproduces `tests/fixtures/corpus.lock` byte-for-byte on a clean checkout, and `tests/fixtures/test_uid.py` confirms the six frozen derived UIDs in §14.3.2.
5. `corpus.lock` contains an entry for every fixture id listed in the table of §14.3.3, each with a non-empty `exercises`, `licence`, `redistributable` and either (`predicate` and `matched_first_by`) or (`parent`, `recipe`, `recipe_sha256`).
6. The `fixture-licence` job exits non-zero when a file belonging to `fx-gantry-tilt-head`, `fx-wrong-bodypart`, `fx-foreign-seg` or any other `redistributable: false` fixture is placed under `tests/fixtures/corpus/` or baked into an image.
7. For every (fixture, capability) pair in `selector_oracle.yaml`, the triage output equals the recorded selected-series set, terminal state and reason code. Changing one reason code in the platform without changing the oracle makes `L2-DATA-030` fail.
8. The DICOM battery report for every (eligible fixture × ServiceVersion) pair contains a record for each of D1, D2, D3, D4, D5, D6, D7 with `status: pass`. A missing record is a failure, not a skip.
9. D1 exits non-zero on any `dciodvfy` line beginning with `Error`, and on any warning absent from `dciodvfy-allow.yaml` or past its `expires` date.
10. D2 reports `dice == 1.0` and `array_equal == true`, and fails with `D2 VACUOUS` when both masks are empty. With mutant M3 (one-slice shift) applied, D2 fails on the mask comparison; with M1 (model-space spacing) applied, D2 fails on the spacing comparison at tolerance `1e-4`.
11. D3 fails when the SEG's `FrameOfReferenceUID` is changed, when any frame's `SourceImageSequence` references a UID outside the selected series, when `SeriesNumber < 9000`, or when any of `Manufacturer`, `ManufacturerModelName`, `DeviceSerialNumber`, `SoftwareVersions` is empty. `dcentvfy` reports no unresolved reference over {source ∪ SEG ∪ SR ∪ SC}.
12. D4 fails when `TemplateIdentifier != "1500"`, when any `NUM` item's units are not UCUM, when any coded pair is absent from `data/coding/concepts.json`, when the SR tracking UID set differs from the SEG segment tracking UID set, or when `(111003, DCM)` Algorithm Version differs from the executing `service_version`.
13. D5 fails when the STOW-RS response carries `00081198`, when the referenced-instance count differs from the count sent, when QIDO-RS returns other than one series per derived `SeriesInstanceUID`, or when `NumberOfSeriesRelatedInstances` differs from the expected count.
14. D6.3 and D6.4 parse `fx-foreign-seg` and `fx-foreign-sr` without exception and recover geometry and coded concepts, proving the readers are not writer-coupled.
15. Every boundary in the §14.5.1 table has a `medos/contracts/<id>/` directory containing `schema/`, at least three files under `samples/valid/` and at least three under `samples/invalid/`, and both a producer-side and a consumer-side check id in `tests/traceability.yaml`.
16. The `JobQueue` conformance suite Q01–Q14 passes against the PostgreSQL driver, and (from 0.3.0) against the Kafka driver, with a byte-identical suite file. `git diff` of the suite between the two runs is empty.
17. `medicalos-conformance run` on the reference service image produces a report with K1–K10 all `pass`, and fails K9 when the service image is given network access to the PACS.
18. The `no-fault-points-in-release` job exits non-zero when a release image is built with the `faultinject` build tag or with `medicalos-faultinject` installed.
19. FT-07 passes and its four declared mutants each cause it to fail, each naming the surface it broke (`S1`, `S2`, `S3`, `S4` respectively).
20. AT-05 asserts on `postgres`, `dicomweb` and `event_bus` in both scenario A (triple delivery) and scenario B (crash window), and fails under mutant M1 with a message naming the imaging plane.
21. AT-08 includes a direct QIDO-RS request for another tenant's `StudyInstanceUID` and asserts zero matching results; it additionally asserts the four remaining planes (REST, database RLS including the schema-wide `relforcerowsecurity` check, network reachability, object store) and the denial audit events. It fails under mutant M2 (unfiltered QIDO) and under M4 (status codes that distinguish exists from not-exists).
22. AT-11 contains no assertion whose predicate is the word "displays". Its render check performs the five-marker calibration fit with residual < 0.5 canvas px, asserts overlay centroid within 1.0 image pixel and IoU ≥ 0.90 on three slices, and fails on the one-slice-shift negative control. Its hydrate check asserts an exact row count equal to the stored measurement count, per-row UCUM unit strings, per-row numeric agreement within display rounding, and zero rows for the corrupted-template negative control.
23. AT-11's provenance assertion compares every one of the fifteen fields named in MOS-TEST-071 against `GET /api/v1/jobs/{id}` and fails when any is empty or hard-coded.
24. AT-18 exits zero only when `git diff --quiet BASE...HEAD -- $(cat .ci/core-paths.txt)` is clean, no migration was added, every changed path is under `medos/services/`, `tests/fixtures/`, `acceptance/AT-18/` or `docs/`, and every recorded HTTP call resolves to a path template in the generated `openapi.yaml`. It exits non-zero under each of mutants M1, M2, M3.
25. AT-18's negative form fails at manifest validation with a message naming the unexpressible `SeriesSelector` field, and no core edit is made to work around it.
26. AT-10 replays a completed job from its provenance record alone and produces byte-identical DICOM after removing exactly the four tags `(0008,0012)`, `(0008,0013)`, `(0008,0023)`, `(0008,0033)` when the service declares `deterministic: true`.
27. The `phi-scan` job in `--selftest` mode reports a hit on `fx-phi-headers`, and reports zero hits across logs, spans, metric labels, event payloads and webhook bodies in the AT-09 run.
28. `tests/mutation-score.json` shows a score ≥ 0.80 for every module listed in `tests/safety-critical.yaml`, and no entry in `tests/quarantine.yaml` names a file under those modules or is past its `expires` date.
29. The `release` pipeline refuses to tag when any acceptance test gating that release or an earlier one is failing, quarantined, or has a surviving mutant.
30. Every one of AT-01 … AT-24 exists as a file under `acceptance/`, validates against `medos/schemas/acceptance/test.json`, declares at least one surface from the closed set, and declares at least one mutant per surface. `acceptance-render` confirms the table in §14.7.1 matches those files exactly.
31. With `emit_verified_sr_on_accept` enabled, a two-round `ResultReview` cycle produces two SR instances sharing one `SeriesInstanceUID` and carrying distinct `SOPInstanceUID`s, the round-1 instance is still retrievable and byte-identical after round 2, and round 2's `PredecessorDocumentsSequence` references round 1. The check fails under the mutant that allocates the same `output_index` to both rounds. (`MOS-TEST-082`)
32. `L1-TRAIN-001` and `L1-TRAIN-002` run over every spec fixture in `medos/contracts/C-PREP/samples/valid/`: `build_chain(parse(serialize(spec)))` is structurally equal to `build_chain(spec)`, and `aff2axcodes(out.affine)` equals `spec.orientation_target`. Removing one field from `serialize` fails the first; mirroring one axis of the synthetic affine fails the second. `L0-TRAIN-003` exits non-zero when a chain carrying a label key is built with a scalar `Spacingd` `mode`. `L2-TRAIN-004` reproduces `golden_fixture.output_tensor_sha256` and `golden_fixture.output_shape` on a clean checkout on CPU and blocks artifact signing on mismatch. (`MOS-TEST-083`–`MOS-TEST-086`)
33. AT-24 drives a training-pipeline run to a `PASS` verdict and ends with the candidate at `lifecycle_status = 'VALIDATED'`, a `deployments` snapshot identical to the pre-run one, the incumbent still the sole `ACTIVE`/`SERVING` row in its slot, and zero jobs routed to the candidate. The candidate serves only after an `artifact.approve` call by a human identity satisfying `MOS-EVID-117`; the same call from the pipeline's service account is refused. (`MOS-TEST-087`)

---

[← 13. Observability, Deployment and Scaling](13-operations.md) · [Index](../../MEDICALOS_SPEC.md) · [15. Delivery Plan and Engineering Rules →](15-delivery.md)
