<!-- MedicalOS Specification v0.4.0 — chapter 6 of 19. Normative.
     116 requirements. Do not edit without a requirement-ID review. -->

[← 5. Execution: Jobs, Queue and Failure Handling](05-execution.md) · [Index](../../MEDICALOS_SPEC.md) · [7. Evidence Plane: Datasets, Evaluation and Validation Reports →](07-evidence.md)

---

## 6. Registries, Capability Resolution and Deployment

### 6.1 Scope, ownership and release phasing

This chapter defines **what the platform knows about the things it can run, and how it decides which one to run**. It owns three registries (Artifact, Capability, Deployment), the pure resolution function that maps a clinical function to a concrete runnable version, the deployment model that owns liveness, and the packaging/signing unit that supply-chain requirements attach to.

It does **not** define: the runtime semantics of `service.yaml` or the `ResultBundle` return contract (Chapter 2, `MOS-SVC`); `SeriesSelector` matching and study triage (Chapter 3, `MOS-DATA`); the semantics of `PreprocessingSpec` fields or geometry (Chapter 4, `MOS-IMG`); `Job` states, retry and queueing (Chapter 5, `MOS-EXEC`); `EvaluationRun`, `AcceptanceCriteria`, `ValidationReport` (Chapter 7, `MOS-EVID`); RBAC, key custody, RLS (Chapter 8, `MOS-SEC`); regulatory metadata semantics and AI-derived marking (Chapter 9, `MOS-SAFE`); HTTP conventions, pagination and problem+json (Chapter 10, `MOS-API`); node inventory collection (Chapter 13, `MOS-OPS`).

**MOS-REG-001** — The registry plane MUST be the only source of truth for *which artifact versions exist*, *what they claim*, and *which of them may receive work in a given (tenant, environment)*. No component MAY dispatch work to an artifact version that is not resolvable from a registry snapshot.

**MOS-REG-002** — Registries MUST be readable without executing any artifact. Resolution MUST NOT require pulling an image, loading weights, or contacting a vendor endpoint.

**MOS-REG-003** — Every mutation of an Artifact, Capability or Deployment row MUST emit an `AuditEvent` (Chapter 8) carrying the actor, the permission exercised, the previous and new row digests, and the resulting `registry_epoch`.

#### Release phasing

The unified registry and the resolver land in 0.3.0 (spine §14). The **Job-side contract is binding from 0.1.0** so that the `Job` schema never breaks.

| element | 0.1.0 | 0.2.0 | 0.3.0 | 0.4.0 |
|---|---|---|---|---|
| `artifact` / `artifact_family` tables, kind discriminator, JSON-Schema validation | single-kind (`model_version`) table, schema validated | + `dataset_version`, `annotation_set` | unified, all kinds | unchanged |
| Capability registry + coded-concept dictionary | 3 seeded rows, code dictionary live (consumed by Chapter 4 SR writing) | + `AcceptanceCriteria` binding | full CRUD + deprecation | unchanged |
| `Resolve()` | **not shipped** — spine §14 puts capability resolution in 0.3.0; the dispatcher writes the single configured `ServiceVersion` pin directly | not shipped | full precedence, ranges, canary bucketing, evidence and regulatory gates | unchanged |
| `job.resolution` pin record written at creation and replayed on retry | **REQUIRED**, `resolved_at` + `selected` only | as 0.1.0 | **REQUIRED**, full §6.7.5 record (`epoch`, `snapshot_id`, `inputs_hash`, `gate_config`, `alternatives`, `excluded`) | unchanged |
| Deployment rows, roles, blue/green, canary | `production` only, role `ACTIVE` | + `clinical_use_mode`, promotion gate | + `CANARY`, `STANDBY`, `SHADOW` | unchanged |
| Signing, SBOM, attestation verification at admission | image digest recorded, verification warn-only | signature verification enforced | + SBOM and attestation required | + referrer rescan |

**MOS-REG-004** — From 0.1.0 every `Job` row MUST carry a non-null `resolution` object holding at minimum `resolved_at` and `selected` (§6.7.5), even when only one `ServiceVersion` is configured and no resolver exists. The resolver-derived members of §6.7.5 — `epoch`, `snapshot_id`, `inputs_hash`, `gate_config`, `alternatives`, `excluded` — become REQUIRED at 0.3.0 with `Resolve()` (spine §14). A release MUST NOT ship a code path that dispatches a job without a `resolution` object.

**MOS-REG-005** — From 0.3.0, when `Resolve()` ships, the gates that a release has not yet built (evidence gate `F8`, regulatory gate `F9`) MUST be implemented as explicit `Exclusion` stages that are configured **off** per environment, and their configuration state MUST appear in the pinned resolution record as `gate_config`. They MUST NOT be absent from the code. Before 0.3.0 there is no resolver and therefore no `Exclusion` stage: the 0.2.0 evidence and regulatory gates are enforced at deployment creation instead (§6.8, the preconditions on `— → PENDING`), and `gate_config` is absent from the degenerate pin record of `MOS-REG-004`.

**MOS-REG-006** — Adding a second capability (`lung_nodule`, spine §14, 0.3.0) MUST require zero changes to the resolver, the registry schemas, the Job schema and the dispatch path. This is the platform acceptance test; it is executed as acceptance check 14 of this chapter.

---

### 6.2 Three registries, three mutability classes

**MOS-REG-007** — The registry plane consists of exactly three registries, distinguished by mutability. No fourth registry may be introduced without a MAJOR platform version.

| registry | rows | mutability | signed | who writes |
|---|---|---|---|---|
| **Artifact** | immutable versions: `ServiceVersion`, `ModelVersion`, `PreprocessingSpec`, `DatasetVersion`, `AnnotationSet`, `WorkflowVersion` | append-only; manifest and digest immutable after publish; only `lifecycle_status` changes | yes — digest + detached signature + attestations | publisher service account (`artifact.publish`) |
| **Capability** | the clinical-function vocabulary (nosology axis) and its coded-concept dictionary | versioned, change-controlled, append-only per `capability_id` | no; change-controlled by `capability.update` + audit | platform curator |
| **Deployment** | the binding of one `ServiceVersion` to one `(tenant, environment, capability)` with a role and a state | fully mutable, this is runtime state | no | tenant deployment admin |

**MOS-REG-008** — `Capability` MUST NOT be an artifact kind. It is a curated vocabulary, not a shipped unit; a vendor publishes an artifact that *claims* capabilities, it never publishes a capability.

**MOS-REG-009** — `Deployment` MUST NOT be an artifact kind. It is mutable liveness state and is deliberately outside the signed set.

#### Registry snapshot and epoch

**MOS-REG-010** — Every write to the `artifact`, `artifact_family`, `capability`, `deployment`, `tenant_pin` or `node_profile` tables MUST, in the same transaction, append a row to the append-only `registry_changelog` table and advance the monotonic `registry_epoch` sequence.

```sql
CREATE TABLE registry_changelog (
  epoch        bigint PRIMARY KEY DEFAULT nextval('registry_epoch'),
  table_name   text        NOT NULL CHECK (table_name IN
                 ('artifact','artifact_family','capability','deployment','tenant_pin','node_profile')),
  row_id       text        NOT NULL,
  op           text        NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
  row_json     jsonb       NOT NULL,
  actor        text        NOT NULL,
  written_at   timestamptz NOT NULL
);
REVOKE UPDATE, DELETE ON registry_changelog FROM medicalos_app;
```

**MOS-REG-011** — A **registry snapshot** is the set of all registry rows as of a given `epoch`. Its identity is `snapshot_id = "sha256:" || hex(sha256(canonical_json(rows_as_of(epoch))))`, where `canonical_json` is RFC 8785 JSON Canonicalization Scheme over rows sorted by `(table_name, row_id)`. The snapshot MUST be reconstructible from `registry_changelog` alone.

**MOS-REG-012** — `Resolve()` MUST be called with a snapshot, never with a live database handle. Replaying a recorded `(epoch, snapshot_id)` and re-running `Resolve()` MUST yield a byte-identical `Outcome` except for the `resolver_version` field when the resolver itself changed.

---

### 6.3 The Artifact registry: kind discriminator and a schema per kind

#### Tables

```sql
-- Semantic definition. The column names, the kind enums, the immutability rules and the
-- unique keys are binding here; the migration-ready physical form is Chapter 12 §12.9.
CREATE TABLE artifact_family (
  id           text PRIMARY KEY,                 -- 'af_pulmo_pleural_effusion'
  kind         text NOT NULL CHECK (kind IN
                 ('service','model','preprocessing','dataset','annotation','workflow',
                  'policy_set')),
  slug         text NOT NULL,                    -- 'pulmo.pleural-effusion'
  display_name text NOT NULL,
  owner_org_id text NOT NULL REFERENCES org(id),
  visibility   text NOT NULL CHECK (visibility IN ('private','org','public')),
  created_at   timestamptz NOT NULL,
  UNIQUE (kind, slug)
);

CREATE TABLE artifact (
  id               text PRIMARY KEY,             -- 'sv_01JQ8Z3K2M'
  family_id        text NOT NULL REFERENCES artifact_family(id),
  kind             text NOT NULL CHECK (kind IN
                     ('service_version','model_version','preprocessing_spec',
                      'dataset_version','annotation_set','workflow_version',
                      'policy_set_version')),
  version          text NOT NULL,                -- '3.2.1'
  version_major    int  NOT NULL,
  version_minor    int  NOT NULL,
  version_patch    int  NOT NULL,
  version_pre      text NOT NULL DEFAULT '',     -- '' when not a prerelease
  manifest         jsonb NOT NULL,
  manifest_schema  text NOT NULL,                -- 'model_version/1.0.0'
  content_digest   text NOT NULL CHECK (content_digest ~ '^sha256:[0-9a-f]{64}$'),
  oci_ref          text,                         -- NULL for dataset_version/annotation_set
                                                 -- and policy_set_version (MOS-REG-112)
  lifecycle_status text NOT NULL,
  status_reason    text,
  published_at     timestamptz NOT NULL,
  published_by     text NOT NULL,
  UNIQUE (family_id, version),
  UNIQUE (content_digest)
);
```

The two blocks above are the **semantic** definition: the column names, the kind enums, the immutability rules and the unique keys are binding here. Physical form is Chapter 12's (`MOS-STORE-201`): one table named `artifacts` alongside `artifact_manifest_schemas`, `uuid` v7 primary keys, `text` + `CHECK` in place of every enum (`MOS-STORE-209`), and `created_at`/`updated_at` per `MOS-STORE-210`. `artifacts.kind` carries the **family-level** value where this chapter writes the version-level one; the mapping is the identity plus the `_version`/`_spec`/`_set` suffix and is fixed (`MOS-STORE-253`), so `policy_set_version` here is `policy_set` there. §12.9 carries the migration-ready form: a migration MUST be generated from it and never from the block above, and the two MUST agree column-for-column.

**MOS-REG-013** — `artifact.kind` is the discriminator. The enum is closed; a manifest whose `kind` is not in the enum MUST be rejected with HTTP 422 and `class: validation_error`.

**MOS-REG-112** — `policy_set` is an artifact kind. A `PolicySet` (Chapter 8, `MOS-SEC-062`) MUST be registered as an `artifact_family` of kind `policy_set` whose versions are `artifact` rows of kind `policy_set_version`, so that `policy_activations.policy_set_digest` (Chapter 12, `MOS-STORE-311`) resolves to a real artifact row rather than to nothing. Its bundle is a content-addressed tar signed with the platform operations key, not an OCI image (Chapter 8 §8.8), so `oci_ref` MUST be NULL and the packaging table of §6.9 does not apply to it; immutability (`MOS-REG-018`), `content_digest` (`MOS-REG-017`), schema validation (`MOS-REG-014`) and the lifecycle graph (`MOS-REG-021`) apply unchanged. Chapter 8 owns the bundle's content, its signing and its activation; this chapter owns only its registration.

**MOS-REG-014** — `artifact.manifest` MUST validate against the JSON Schema named by `artifact.manifest_schema` before the row is written. Validation failure MUST be a hard reject; there is no "store and validate later" path.

**MOS-REG-015** — Schemas are JSON Schema 2020-12, published at `https://schemas.medicalos.org/artifact/<kind>/<schema_version>.json`, versioned independently per kind, and MUST set `"additionalProperties": false` at every object level. Unknown fields are a reject, not a warning.

**MOS-REG-016** — All manifest schemas MUST be generated from a single in-repo schema source and MUST be the source from which Go structs, SDK models and the OpenAPI document are generated (Chapter 10). A hand-maintained duplicate of any manifest shape is forbidden.

#### The common envelope

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://schemas.medicalos.org/artifact/envelope/1.0.0.json",
  "title": "MedicalOS artifact envelope",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "kind", "family", "version",
               "content_digest", "publisher", "created_at", "spec"],
  "properties": {
    "schema_version": { "type": "string", "pattern": "^1\\.[0-9]+\\.[0-9]+$" },
    "kind": {
      "enum": ["service_version", "model_version", "preprocessing_spec",
               "dataset_version", "annotation_set", "workflow_version",
               "policy_set_version"]
    },
    "family": {
      "type": "string", "maxLength": 128,
      "pattern": "^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
    },
    "version": {
      "type": "string",
      "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$"
    },
    "content_digest": { "type": "string", "pattern": "^sha256:[0-9a-f]{64}$" },
    "publisher": {
      "type": "object",
      "additionalProperties": false,
      "required": ["org_id", "signing_identity"],
      "properties": {
        "org_id": { "type": "string" },
        "signing_identity": { "type": "string" }
      }
    },
    "created_at": { "type": "string", "format": "date-time" },
    "spec": { "type": "object" }
  },
  "allOf": [
    { "if":   { "properties": { "kind": { "const": "model_version" } } },
      "then": { "properties": { "spec": {
                "$ref": "https://schemas.medicalos.org/artifact/model_version/1.0.0.json" } } } },
    { "if":   { "properties": { "kind": { "const": "service_version" } } },
      "then": { "properties": { "spec": {
                "$ref": "https://schemas.medicalos.org/artifact/service_version/1.0.0.json" } } } }
  ]
}
```

**MOS-REG-017** — `content_digest` MUST be computed by the registry over the RFC 8785 canonicalisation of the manifest *excluding* the `content_digest` field itself, and MUST NOT be accepted from the publisher. A publisher-supplied value MUST be compared and a mismatch rejected.

**MOS-REG-018** — An `artifact` row's `manifest`, `content_digest`, `version` and `oci_ref` MUST be immutable after insert. Enforced by `REVOKE UPDATE (manifest, content_digest, version, oci_ref) ON artifact FROM medicalos_app;` plus a `BEFORE UPDATE` trigger. Only `lifecycle_status` and `status_reason` are mutable.

**MOS-REG-019** — Republishing an existing `(family_id, version)` with different content MUST return HTTP 409 with the existing `content_digest` in the problem document. Republishing with byte-identical content MUST return HTTP 200 and be a no-op.

#### Lifecycle status per kind

**MOS-REG-020** — `artifact.lifecycle_status` values and their permitted use per kind:

| kind | permitted statuses |
|---|---|
| `service_version`, `model_version` | `DRAFT`, `REGISTERED`, `VALIDATING`, `VALIDATED`, `APPROVED`, `DEPRECATED`, `SUSPENDED`, `RECALLED` |
| `preprocessing_spec`, `workflow_version`, `policy_set_version` | `REGISTERED`, `APPROVED`, `DEPRECATED`, `SUSPENDED`, `RECALLED` |
| `dataset_version`, `annotation_set` | `SEALED`, `DEFECTIVE` — set and cascaded by Chapter 7 (`MOS-EVID-014`); the registry stores but never originates these |

**MOS-REG-021** — The status graph is:

| from | to | permission | effect |
|---|---|---|---|
| `DRAFT` | `REGISTERED` | `artifact.publish` | manifest frozen, digest assigned, signature verified |
| `REGISTERED` | `VALIDATING` | `evidence.run` | an `EvaluationRun` (Chapter 7) has started against it |
| `VALIDATING` | `VALIDATED` / `REGISTERED` | `evidence.run` | run finished / run failed or was abandoned |
| `VALIDATED` | `APPROVED` | `artifact.approve` | a named human approver signed a `ValidationReport` (Chapter 7) |
| `VALIDATED`, `APPROVED` | `DEPRECATED` | `artifact.status.set` | resolvable, but ranks below every non-deprecated candidate |
| `REGISTERED`, `VALIDATING`, `VALIDATED`, `APPROVED`, `DEPRECATED` | `SUSPENDED` | `artifact.suspend` | **reversible**; no new dispatch |
| `SUSPENDED` | previous status | `artifact.suspend` | requires `status_reason` naming the resolved issue |
| any | `RECALLED` | `artifact.recall` | **irreversible**; no new dispatch, all deployments forced to `DRAINING` |

Chapter 2 restates this graph as `MOS-SVC-101` for the service contract; the two lists MUST be identical, and a divergence is a defect in Chapter 2, not here.

**MOS-REG-022** — `RECALLED` MUST be irreversible. A recalled defect that is later fixed becomes a new version, never a status reversal. The registry MUST reject any transition out of `RECALLED` with HTTP 409.

---

### 6.4 Service and ServiceVersion

`Service` is the `artifact_family` row with `kind = 'service'`. `ServiceVersion` is the `artifact` row with `kind = 'service_version'`. The runtime meaning of every field below is Chapter 2 (`MOS-SVC`); this section defines what the registry stores, indexes, enforces and exposes to `Resolve()`.

**MOS-REG-023** — The registry MUST maintain the following derived index columns on every `service_version` row, computed from the manifest at publish time and used as hard filters by the resolver: `capabilities[]`, `mode` (`native` | `sealed`), `modalities[]`, `model_version_refs[]`, `preprocessing_spec_refs[]`, `gpu_architectures[]`, `legal_manufacturer_id`, `regulatory_jurisdictions[]`.

```yaml
# ServiceVersion manifest — spec block, registry-relevant fields in full.
# Runtime semantics: Chapter 2 (MOS-SVC). Validated against
# https://schemas.medicalos.org/artifact/service_version/1.0.0.json
schema_version: "1.0.0"
kind: service_version
family: pulmo.pleural-effusion
version: "3.2.1"
publisher:
  org_id: org_pulmoai
  signing_identity: "https://github.com/pulmoai/pleural-effusion/.github/workflows/release.yml@refs/tags/v3.2.1"
created_at: "2026-01-09T11:02:41Z"
spec:
  mode: native
  image:
    ref: "ghcr.io/pulmoai/pleural-effusion"
    digest: "sha256:a1c9f0b47d2e5183aa6c04ef7b1d9c3e5f28a704bd6613c2f0a95e77c41b8d02"
  capabilities:
    - id: pleural_effusion
      outputs: [segmentation, measurement]
  modalities: ["CT"]
  series_selector_ref: "sel_chest_ct_thin_axial_v3"   # Chapter 3, MOS-DATA
  models:
    - ref: "mv_pulmo_effusion_unet_3_2_1"
      role: primary
    - ref: "mv_pulmo_lung_seg_1_4_0"
      role: dependency
  preprocessing_specs:
    - ref: "ps_pulmo_effusion_prep_2_0_0"
  resources:
    gpu_required: true
    gpu_memory_mib: 11264
    cpu_millicores: 4000
    memory_mib: 24576
    max_concurrent_jobs: 2
  engineering_acceptance:
    p95_wall_clock_seconds: 180
    max_gpu_memory_mib: 11264
    result_bundle_schema_validity: 1.0
  legal_manufacturer:
    id: "lm_pulmoai_gmbh"
    name: "PulmoAI GmbH"
    device_serial_number: "PULMO-EFF-0003"
    software_versions: "3.2.1"
  regulatory_status:
    - jurisdiction: "EU"
      status: "not_a_medical_device"
      evidence_ref: null
    - jurisdiction: "US"
      status: "not_cleared"
      evidence_ref: null
  compatibility:
    medicalos_api: ">=1.0 <2"
    service_contract: "1.2"
```

**MOS-REG-024** — `spec.image.digest` MUST be a resolved `sha256` digest. A tag-only reference MUST be rejected. The registry MUST NOT resolve a tag on the publisher's behalf.

**MOS-REG-025** — In `mode: native`, `spec.models[]` MUST be non-empty and every `ref` MUST resolve to an existing `model_version` artifact with `lifecycle_status` in `{VALIDATED, APPROVED}` at publish time. Dangling refs MUST be rejected at publish, not at dispatch.

**MOS-REG-026** — In `mode: sealed`, `spec.models[]` MUST still contain exactly one `model_version` ref with `role: primary`. That `ModelVersion` carries `weights_availability: vendor_sealed` and no `weights_digest`; it exists so that `EvaluationRun`, `ValidationReport` and provenance have a stable subject (Chapter 7, Chapter 9). A sealed service without a declared `ModelVersion` MUST be rejected; otherwise its evidence has nothing to bind to.

**MOS-REG-027** — `spec.legal_manufacturer` MUST be present and complete. Its `name`, `device_serial_number` and `software_versions` are the source of the DICOM Enhanced General Equipment Type 1 attributes written by the platform (Chapter 4, `MOS-IMG`). A `service_version` missing any of these three fields MUST be rejected at publish, because the DICOM writer cannot emit a conformant SEG without them.

**MOS-REG-028** — The registry MUST verify that `publisher.signing_identity` is one of the identities registered for `legal_manufacturer.id`. An artifact signed by an identity not bound to the declared legal manufacturer MUST be rejected — otherwise the equipment attributes written into a patient record are unattested.

**MOS-REG-029** — `spec.capabilities[].id` MUST reference a Capability row whose `status` is `supported`. Claiming a `reserved` or `deprecated` capability MUST be rejected at publish.

---

### 6.5 Model and ModelVersion

**MOS-REG-030** — `ModelVersion` is the unit that evidence binds to (Chapter 7) and that provenance names (Chapter 9). It MUST exist for every mode, including `sealed`.

```yaml
# ModelVersion manifest — spec block, complete.
# Validated against https://schemas.medicalos.org/artifact/model_version/1.0.0.json
schema_version: "1.0.0"
kind: model_version
family: pulmo.effusion-unet
version: "3.2.1"
publisher:
  org_id: org_pulmoai
  signing_identity: "https://github.com/pulmoai/pleural-effusion/.github/workflows/release.yml@refs/tags/v3.2.1"
created_at: "2026-01-09T10:41:07Z"
spec:
  capabilities: ["pleural_effusion"]
  weights_availability: platform_managed        # platform_managed | vendor_sealed
  weights:
    oci_ref: "ghcr.io/pulmoai/effusion-unet"
    digest: "sha256:5e02b7143c8ad90f6e4172b8c05d3ea91b7fd6c40a2e8813f5b0c96ad7412e6b"
    format: onnx                                 # onnx | torchscript | tensorrt_plan | safetensors
    size_bytes: 184236032
  preprocessing_spec_ref: "ps_pulmo_effusion_prep_2_0_0"
  golden_fixture:                                # field names and digest encoding fixed by
                                                 # Chapter 4, MOS-IMG-049 — bare 64-hex
    sha256: "0c47ab9f2d1e6b83f5490ad72c1e8b60743fd29ca5b8e01274f6390dbc5a12e7"
    output_tensor_sha256: "b91d3c7e408a2f56c1d9740eb32a68f0517c4d9ba26e83f10c5d7a94e26b3018"
  io:
    input:
      name: "input"
      shape: [1, 1, 128, 192, 192]               # N, C, Z, Y, X
      dtype: float32
      layout: NCZYX
      orientation: LPS                           # MUST match MOS-IMG canonical geometry
      value_range: [-1.0, 1.0]
    output:
      name: "logits"
      shape: [1, 2, 128, 192, 192]
      dtype: float32
      layout: NCZYX
      kind: segmentation_logits                  # segmentation_logits | segmentation_binary |
                                                 # segmentation_fractional | detection_boxes | scalar
      label_map: { 0: background, 1: pleural_effusion }
  operating_point:
    kind: probability_threshold
    score_threshold: 0.45
    selected_on_evaluation_run: "er_01JP4T9X7B"
    selection_rule: "max F1 on the sealed validation split"
  runtime:
    engine: triton
    engine_version: ">=24.08 <25.00"
    backend: onnxruntime                          # onnxruntime | pytorch | tensorrt | python
    gpu_architectures: ["sm_80", "sm_86", "sm_89", "sm_90"]
    cuda: ">=12.1 <13"
    driver_min: "535.104.05"
    gpu_memory_mib: 9216
  derived_from: null
  evaluation_run_id: "er_01JP4T9X7B"              # FK into EvaluationRun, Chapter 7
  applicability_envelope_ref: "ae_chest_ct_v2"    # Chapter 3, MOS-DATA
  not_validated_for:
    - "studies with slice thickness > 3.0 mm"
    - "paediatric patients under 18 years"
    - "post-pneumonectomy anatomy"
  known_failure_modes:
    - "large hepatic cysts adjacent to the right hemidiaphragm are occasionally included in the right effusion label"
    - "loculated effusions with thick septations are under-segmented"
```

**MOS-REG-031** — `spec.evaluation_run_id` MUST be a foreign key into `EvaluationRun` (Chapter 7). A free-form `metrics` map on `ModelVersion` is forbidden. The registry MUST expose metrics only by dereferencing the evaluation run, together with its dataset version, split and annotation set.

**MOS-REG-032** — `spec.not_validated_for` and `spec.known_failure_modes` MUST each contain at least one entry. An empty list MUST be rejected. "None known" is expressible only as the literal string entry `"no known failure modes have been characterised"`, which is a claim the publisher makes explicitly and which appears verbatim in the provenance panel.

**MOS-REG-033** — `spec.io.output.kind` MUST discriminate binary from fractional segmentation. `segmentation_logits` and `segmentation_fractional` MUST be accompanied by a non-null `spec.operating_point`.

**MOS-REG-034** — Every reported sensitivity, specificity or F1 anywhere in the platform MUST be accompanied by the `operating_point.score_threshold` at which it was measured. The registry MUST reject an `EvaluationRun` reference whose recorded operating point differs from `spec.operating_point.score_threshold`.

**MOS-REG-035** — A change to `spec.operating_point.score_threshold` MUST produce a new `ModelVersion` with at least a MINOR bump, MUST carry its own `evaluation_run_id`, and MUST re-pass the Capability's `AcceptanceCriteria` exactly as a weights change does. A threshold change MUST NOT be shipped as configuration.

**MOS-REG-113** — The operating threshold has exactly **one** member name across the platform: `score_threshold`. It is Chapter 2's spelling — `capabilities[].operating_points[].score_threshold` (`MOS-SVC-020`) and the `ResultBundle` finding member `operating_point.score_threshold` (`MOS-SVC-089`) — and it is what Chapter 12 persists as `result_findings.score_threshold`. This chapter therefore spells its own member `spec.operating_point.score_threshold` (`MOS-REG-033`). No manifest, schema, column, API body or tool argument MAY introduce a further name for this number. The value travels this path unrenamed:

| stage | member | owner |
|---|---|---|
| `ServiceVersion` manifest — the points the service supports | `spec.capabilities[].operating_points[].score_threshold` | ch. 2 `MOS-SVC-020` |
| `ModelVersion` manifest — the point the trained artifact was evaluated at | `spec.operating_point.score_threshold` | ch. 6 `MOS-REG-033` |
| `Deployment` — which supported point this slot applies | `operating_point_id` (an identifier, never the number) | ch. 2 `MOS-SVC-112`, ch. 12 `MOS-STORE-262` |
| `ResultBundle` finding — the point applied to this finding | `operating_point.score_threshold` | ch. 2 `MOS-SVC-089` |
| persisted result | `result_findings.score_threshold` | ch. 12 |

The remaining spellings in this document — Chapter 9's `clinical.operating_point.threshold` (§9.2) and `operating_threshold` (`MOS-SAFE-054`), and Chapter 12's `results.operating_threshold` — denote this same number; their owning chapters MUST rename them to `score_threshold` rather than carry a synonym alongside it. This requirement fixes the *name* only: which entity *selects* the value remains the open question recorded at `MOS-REG-050`.

**MOS-REG-036** — `spec.golden_fixture` MUST be present for `weights_availability: platform_managed`. Its two digests are the input to the worker startup self-test defined in Chapter 4 (`MOS-IMG-054`); a worker that cannot reproduce `golden_fixture.output_tensor_sha256` MUST refuse to serve, and the platform MUST move the affected `Deployment` to `SUSPENDED`. The digest encoding convention is stated once, here: every member of `golden_fixture` is a **bare lowercase 64-character hex string**, because Chapter 4 owns those fields and types them so (`MOS-IMG-049`). The `sha256:` prefix carried by this chapter's own digest fields — `artifact.content_digest`, `spec.image.digest`, `spec.weights.digest`, `conversion_equivalence.fixture_digest` — MUST NOT be written into a `golden_fixture` member, and a validator generated from Chapter 4's types MUST reject it.

**MOS-REG-037** — `spec.io.input.orientation` MUST be stated explicitly. It MUST NOT default. A model served with the wrong orientation returns a plausible mirrored segmentation that passes every structural check, so orientation is a required declaration, not an assumption.

**MOS-REG-038** — `ModelVersion.lifecycle_status` is **lifecycle-only**. It MUST NOT encode environment, traffic, or whether the version is live. The values `STAGING`, `DEPLOYED` and `PRODUCTION` MUST NOT appear in this enum; they are Deployment concepts (§6.8).

**MOS-REG-039** — `weights_availability: vendor_sealed` implies `spec.weights` is null and `spec.golden_fixture` is null. The registry MUST reject a sealed ModelVersion that declares weights, and MUST reject a `platform_managed` ModelVersion that omits them.

**MOS-REG-040** — On `RECALLED`, the registry MUST expose `GET /api/v1/model-versions/{id}/impact` and `GET /api/v1/service-versions/{id}/impact`, returning the paginated set of `job_id`, `result_id`, generated `SeriesInstanceUID` and `tenant_id` for every result the version produced, computed from the pinned `job.resolution` records (§6.7.5) and the provenance record (Chapter 9). This endpoint MUST be available within the same release as `RECALLED`; a recall state with no impact query is not a recall.

---

### 6.6 The Capability registry

`Capability` is the **nosology axis**: what clinical function is being performed, independent of who performs it. Modality, body part and technique are *input* constraints and live in `SeriesSelector` (Chapter 3); they MUST NOT be encoded in a capability id.

**MOS-REG-041** — A `capability_id` MUST match `^[a-z][a-z0-9_]{2,47}$`, MUST be immutable once created, and MUST NOT be reused for a different clinical meaning. Renaming a capability is forbidden; superseding it is done with `superseded_by`.

```sql
-- Semantic definition. The column names, the enum values and the unique key are binding
-- here; the migration-ready physical form is Chapter 12 §12.9.1.
CREATE TABLE capability (
  id                 text PRIMARY KEY,
  revision           int  NOT NULL,
  kind               text NOT NULL CHECK (kind IN
                       ('segmentation','detection','measurement','classification')),
  status             text NOT NULL CHECK (status IN ('reserved','supported','deprecated')),
  display_name       text NOT NULL,
  definition         text NOT NULL,
  primary_code       jsonb NOT NULL,     -- {system, code, meaning, system_version,
                                         --  narrative_synonyms[]}
  additional_codes   jsonb NOT NULL DEFAULT '[]',
  measurements       jsonb NOT NULL DEFAULT '[]',
  acceptance_criteria_ref text,          -- FK into AcceptanceCriteria, Chapter 7
  superseded_by      text REFERENCES capability(id),
  created_at         timestamptz NOT NULL,
  UNIQUE (id, revision)
);
```

The block above is the **semantic** definition: the column names, the enum values and the unique key are binding here. Physical form is Chapter 12's (`MOS-STORE-201`): the row is split across `capabilities` — where the primary key is a `uuid` v7 surrogate, this chapter's `capability_id` is the `slug` column and this chapter's `kind` is `output_kind` — and `capability_concepts`, the normalised one-row-per-code form of `primary_code`, `additional_codes` and `measurements[]` (`MOS-STORE-256`). §12.9.1 carries the migration-ready form; the two MUST agree value-for-value, and where they disagree the defect is Chapter 12's. `capability_concepts` is the physical table the requirement below calls the single source of coded concepts.

**MOS-REG-042** — The Capability row MUST be the single source of coded concepts for that clinical function. The DICOM SR/SEG writer (Chapter 4), the FHIR projection (deferred) and the API response body MUST all read codes from here. A second code table anywhere in the platform is forbidden.

**MOS-REG-043** — Every code entry MUST carry `system`, `code`, `meaning` and `system_version` (e.g. `"SCT"`, `"60046008"`, `"Pleural effusion"`, `"SNOMED CT International Edition 2026-01-31"`). A code without a pinned `system_version` MUST be rejected.

**MOS-REG-114** — Every code entry MUST additionally carry `narrative_synonyms[]`: the surface forms, in the display language, under which that concept may legitimately appear in generated narrative. The array MAY be empty; it MUST NOT be absent, because an absent array and an empty one are not the same claim. Entries MUST be compared case-folded and whitespace-normalised, and MUST NOT be changed without incrementing `capability.revision` and auditing the change (`MOS-REG-048`), because widening the set widens what a generated report is permitted to say. Chapter 12 persists the member as a `capability_concepts` column (`MOS-STORE-256`). This array, together with the concept's `meaning`, is the **only** source of the finding-label lexicon `A_lbl` whose set-membership Chapter 11's hallucination post-condition asserts (`MOS-AGENT-077`); there is no code dictionary in Chapter 9, and a second synonym list anywhere in the platform is forbidden by `MOS-REG-042`.

```yaml
primary_code:
  system: SCT
  code: "60046008"
  meaning: "Pleural effusion"
  system_version: "SNOMED CT International Edition 2026-01-31"
  narrative_synonyms: ["pleural effusion", "pleural fluid", "fluid in the pleural space"]
```

**MOS-REG-044** — CI MUST validate every `primary_code` and `additional_codes` entry against the bundled terminology subset for the pinned `system_version`, and MUST fail the build on an unknown or inactive concept. The seed values in the starter list below are seeds subject to this check; they MUST NOT be treated as verified merely because they are written here.

**MOS-REG-045** — Every measurement in `capability.measurements` MUST declare a UCUM `unit`, a coded `concept`, and `computation_geometry: source` (Chapter 4, `MOS-IMG`). A measurement without a UCUM unit MUST be rejected; a bare number cannot be written into a conformant TID 1500 SR.

**MOS-REG-046** — A capability MUST NOT leave `reserved` until it has a `primary_code`, at least one measurement (for `kind` in `measurement`, `segmentation`), and an `acceptance_criteria_ref`. `reserved` rows exist to freeze the identifier; they are not resolvable and MUST be excluded by filter `F1`.

#### Starter list — chest CT

| capability_id | kind | primary code (system, code, meaning) | measurements (UCUM) | status at 0.3.0 |
|---|---|---|---|---|
| `lung_segmentation` | segmentation | SCT, `39607008`, Lung structure | `lung_volume_total` (`mL`), `lung_volume_left` (`mL`), `lung_volume_right` (`mL`) | supported |
| `pleural_effusion` | segmentation | SCT, `60046008`, Pleural effusion | `effusion_volume_left` (`mL`), `effusion_volume_right` (`mL`), `effusion_max_depth` (`mm`) | supported |
| `emphysema_laa` | measurement | SCT, `87433001`, Pulmonary emphysema | `laa_950_percent_total` (`%`), `laa_950_percent_left` (`%`), `laa_950_percent_right` (`%`), `hu_percentile_15` (`[hnsf'U]`) | supported |
| `lung_nodule` | detection | SCT, `427359005`, Solitary nodule of lung | `nodule_long_axis` (`mm`), `nodule_short_axis` (`mm`), `nodule_volume` (`mm3`), `nodule_count` (`1`) | supported (0.3.0) |
| `pneumothorax` | segmentation | SCT, `36118008`, Pneumothorax | `pneumothorax_volume` (`mL`) | reserved |
| `bronchiectasis` | classification | SCT, `12295008`, Bronchiectasis | `broncho_arterial_ratio` (`1`) | reserved |
| `pulmonary_embolism` | detection | SCT, `59282003`, Pulmonary embolism | `clot_burden_index` (`%`) | reserved |
| `lung_mass` | detection | not yet assigned | not yet assigned | reserved |

**MOS-REG-047** — `emphysema_laa` MUST be served in 0.1.0 by a deterministic measurement rather than a learned model (spine §14). The Capability row MUST therefore carry the computation parameters in `measurements[].parameters`, and they MUST be versioned like any other clinically load-bearing value:

```yaml
id: emphysema_laa
revision: 3
kind: measurement
status: supported
primary_code:
  system: SCT
  code: "87433001"
  meaning: "Pulmonary emphysema"
  system_version: "SNOMED CT International Edition 2026-01-31"
  narrative_synonyms: ["emphysema", "pulmonary emphysema", "low attenuation area"]
measurements:
  - id: laa_950_percent_total
    concept: { system: SCT, code: "87433001", meaning: "Pulmonary emphysema",
               system_version: "SNOMED CT International Edition 2026-01-31",
               narrative_synonyms: ["emphysema", "pulmonary emphysema"] }
    unit: "%"
    computation_geometry: source
    parameters:
      threshold_hu: -950
      threshold_unit: "[hnsf'U]"
      mask_source_capability: lung_segmentation
      connected_component_filter: none
      requires_kernel_class: soft
      requires_slice_thickness_mm_max: 1.5
acceptance_criteria_ref: "ac_emphysema_laa_v1"
```

**MOS-REG-048** — A change to any value under `measurements[].parameters` MUST increment `capability.revision`, MUST invalidate the `acceptance_criteria_ref` until a new `EvaluationRun` is recorded, and MUST be audited. Changing an HU threshold or a component filter changes the clinical output and MUST pass the same gate as a weights change.

**MOS-REG-049** — `AcceptanceCriteria` (the clinical bar: sensitivity ≥ x on cohort y) belongs to the **Capability** and is referenced by `acceptance_criteria_ref`. Engineering bars (p95 latency, GPU ceiling, `ResultBundle` schema validity) belong to `ServiceVersion.spec.engineering_acceptance` (§6.4). The registry MUST reject a `service_version` that declares clinical acceptance thresholds in its own manifest.

**MOS-REG-050** — Which entity *selects* the operating threshold (the Capability, as a clinical policy, or the ModelVersion, as a property of the trained artifact) is an open question recorded in Chapter 16. This chapter fixes only where the value is *stored* (`ModelVersion.spec.operating_point`, `MOS-REG-033`) and what changing it costs (`MOS-REG-035`). Implementations MUST NOT resolve the ownership question by adding a second threshold field.

---

### 6.7 Capability resolution

#### 6.7.1 Signature

**MOS-REG-051** — Resolution MUST be a pure function with exactly this shape. There MUST be exactly one implementation, in Go, in the control plane. A second implementation in any language — including an SDK-side mirror for dry runs — is forbidden; dry runs go through the endpoint in `MOS-REG-110`.

```go
// Package resolve implements MOS-REG-051..MOS-REG-068.
// Build constraint (enforced by a CI import check): this package MUST NOT import
// net/http, database/sql, os, math/rand, or call time.Now.
package resolve

type Environment string // "dev" | "staging" | "production"

type Request struct {
    CapabilityID string       // "pleural_effusion"
    TenantID     string       // "tnt_7f3a"
    Environment  Environment  // "production"
    Modality     string       // "CT"
    Study        StudyProfile // triage output, Chapter 3 (MOS-DATA)
    JobPin       *VersionPin  // nil when the caller named only a capability
}

type VersionPin struct {
    ServiceFamily string // "pulmo.pleural-effusion"; "" = any family serving the capability
    Range         string // ">=3.2 <4", "=3.2.1", "^3.2.0"
}

type Snapshot struct {
    SnapshotID   string                 // "sha256:9c1f..."
    Epoch        int64                  // 184213
    AsOf         time.Time              // the only time source the resolver may read
    Capabilities map[string]Capability
    Services     []ServiceVersionRow
    Models       map[string]ModelVersionRow
    Preproc      map[string]PreprocRow
    Deployments  []DeploymentRow
    TenantPins   []TenantPin
    Nodes        []NodeProfile          // accelerator inventory per environment (Chapter 13)
    Metrics      map[string]MetricPoint // key: serviceVersionID+"|"+capabilityID+"|"+datasetVersionID
    Licences     []LicenceGrant
    GateConfig   GateConfig             // which optional filters are enabled (MOS-REG-005)
}

type Candidate struct {
    ServiceVersionID   string
    ServiceFamily      string
    Version            string
    ImageDigest        string
    ModelVersionIDs    []string
    PreprocSpecIDs     []string
    DeploymentID       string
    DeploymentRole     string   // "ACTIVE" | "CANARY" | "SHADOW" | "STANDBY"
    RankKey            RankKey  // the ordered tuple that produced this position
}

type Exclusion struct {
    ServiceVersionID string
    Stage            string // "F1".."F9"
    ReasonCode       string
    Detail           string
}

type Outcome struct {
    Decision        string      // "SELECTED" | "ZERO_CANDIDATES"
    Selected        *Candidate  // nil iff ZERO_CANDIDATES
    Ranked          []Candidate // full surviving set, ordered
    Excluded        []Exclusion // every filtered row, with a reason
    ReasonCode      string      // set iff ZERO_CANDIDATES
    ResolverVersion string      // "1.0.0"
    SnapshotID      string
    Epoch           int64
    InputsHash      string      // sha256 over RFC 8785 canonical JSON of Request
}

func Resolve(req Request, snap Snapshot) Outcome
```

**MOS-REG-052** — `Resolve` MUST NOT return an error. Every outcome is either a selection or a `ZERO_CANDIDATES` decision with a reason code; a malformed input is caught by schema validation before the call.

**MOS-REG-053** — `Resolve` MUST be deterministic: same `Request`, same `Snapshot` ⇒ byte-identical `Outcome`. Map iteration MUST be sorted before it can affect output. This MUST be asserted by a property test running 10 000 random registry states twice each.

**MOS-REG-054** — Both job entry paths use the same function. The study-arrival path (Chapter 3) creates one Job per matching deployed `ServiceVersion` and therefore calls `Resolve` with `JobPin{ServiceFamily: <family>, Range: "=<version>"}`. There MUST NOT be a dispatch path that bypasses `Resolve`; a `RECALLED` version must be unreachable from the arrival path exactly as it is from the capability path.

#### 6.7.2 Hard filters (stage F)

**MOS-REG-055** — Filters run in order. Each removes candidates and records an `Exclusion`. A candidate surviving all filters enters ranking.

| stage | filter | reason code on exclusion |
|---|---|---|
| F1 | Capability exists, `status = supported`, and is claimed by the `ServiceVersion` | `capability_not_claimed`, `capability_reserved`, `capability_deprecated` |
| F2 | Tenant visibility and a non-expired `LicenceGrant` at `snap.AsOf` | `not_visible_to_tenant`, `licence_expired` |
| F3 | `lifecycle_status` of the `ServiceVersion` **and** every referenced `ModelVersion` and `PreprocessingSpec` ∉ {`SUSPENDED`, `RECALLED`, `DRAFT`, `REGISTERED`, `VALIDATING`} | `version_suspended`, `version_recalled`, `version_not_validated` |
| F4 | A `Deployment` exists for `(tenant, environment, capability, service_version)` with `state = SERVING` and `role` ∈ {`ACTIVE`, `CANARY`} | `no_deployment`, `deployment_not_serving`, `deployment_standby` |
| F5 | Version range satisfaction against the effective pin (job pin, else tenant pin, else unconstrained) | `range_unsatisfied`, `pinned_version_not_found` |
| F6 | `Modality` match and `applicability_envelope` admits `req.Study` (Chapter 3) | `modality_mismatch`, `envelope_mismatch` |
| F7 | At least one `NodeProfile` in the environment satisfies `runtime` (§6.10) | `runtime_unsatisfiable` |
| F8 | *(gateable)* A `ValidationReport` exists meeting the Capability's `AcceptanceCriteria` on the tenant's acceptance dataset | `evidence_gate_failed` |
| F9 | *(gateable)* If `Deployment.clinical_use_mode = clinical`: `legal_manufacturer` present and `regulatory_status` declared for the tenant's jurisdiction (Chapter 9) | `regulatory_gate_failed` |

**MOS-REG-056** — F3 MUST run before F5. An explicit pin MUST NOT resurrect a `SUSPENDED` or `RECALLED` version: pinning to one yields `ZERO_CANDIDATES` with `pinned_version_recalled` / `pinned_version_suspended`, never a dispatch.

**MOS-REG-057** — `SUSPENDED` MUST stop *new* dispatch only. A job already in `RUNNING` against a suspended version MUST be allowed to complete; the platform MUST NOT kill in-flight inference on suspension. `RECALLED` behaves identically for in-flight work, but the produced `Result` MUST be flagged `produced_by_recalled_version: true` (Chapter 9).

#### 6.7.3 Ranking (the precedence order)

**MOS-REG-058** — Surviving candidates MUST be ordered by the following tuple, compared left to right. This is the written form of the spine §9 precedence.

| position | key | direction | definition |
|---|---|---|---|
| P1 | `job_pin_family_match` | desc (1 before 0) | 1 when `req.JobPin.ServiceFamily` is non-empty and equals the candidate's family |
| P2 | `tenant_pin_family_match` | desc | 1 when a `TenantPin` for this `(tenant, capability)` names this family |
| P3 | `deployment_rank` | desc | canary-aware, see `MOS-REG-059` |
| P4 | `acceptance_metric` | desc | value of the Capability's `AcceptanceCriteria.primary_metric` for this `ServiceVersion` on the **tenant's** acceptance `DatasetVersion`; `-Inf` when absent |
| P5 | `deprecated` | asc (0 before 1) | 1 when `lifecycle_status = DEPRECATED` |
| P6 | `semver` | desc | major, then minor, then patch; a prerelease sorts below its release |
| P7 | `content_digest` | asc | lexicographic; the final total-order tie-break |

**MOS-REG-059** — `deployment_rank` MUST make canary work in a queue-driven system without breaking purity:

```
bucket = crc32c(tenant_id || 0x00 || study_id) mod 1000     // CRC-32/Castagnoli, internal study_id
for each surviving candidate c:
    if c.DeploymentRole == "CANARY" and bucket < c.Deployment.TrafficPermille:  deployment_rank = 2
    elif c.DeploymentRole == "ACTIVE":                                          deployment_rank = 1
    elif c.DeploymentRole == "CANARY":                                          deployment_rank = 0
```

`study_id` is the platform's internal study identifier, never the `StudyInstanceUID`, so no PHI enters the hash. Using the study id rather than the job id guarantees that every attempt of every job for one study lands in the same bucket, and therefore that a retry cannot cross the canary boundary.

**MOS-REG-060** — A missing acceptance metric (P4) MUST rank `-Inf`, never `0` and never "pass". When gate F8 is enabled, a missing metric has already excluded the candidate; when F8 is disabled it MUST merely rank last, and `Outcome.Ranked[i].RankKey` MUST record `acceptance_metric_present: false` so the pinned record shows the metric was absent.

**MOS-REG-061** — `Outcome.Ranked` MUST be a total order. A resolver that can return two candidates with an identical `RankKey` is non-conformant; P7 exists to make this impossible.

#### 6.7.4 Version range syntax

**MOS-REG-062** — The range grammar is closed and small. Anything outside it is a parse error surfaced at manifest/pin validation time, not at dispatch time.

```
range        := clause ( SP+ clause )*          ; clauses are ANDed
clause       := op? partial
op           := ">=" | ">" | "<=" | "<" | "=" | "^" | "~"
partial      := num ( "." num ( "." num ( "-" prerelease )? )? )?
num          := "0" | [1-9] [0-9]*
prerelease   := [0-9A-Za-z.-]+
```

| written | means | matches | does not match |
|---|---|---|---|
| `=3.2.1` | exact | `3.2.1` | `3.2.2` |
| `3.2.1` | exact (bare is exact, **not** caret) | `3.2.1` | `3.2.2` |
| `>=3.2 <4` | `>=3.2.0` and `<4.0.0` | `3.2.0`, `3.9.7` | `3.1.9`, `4.0.0` |
| `^3.2.0` | `>=3.2.0 <4.0.0` | `3.4.0` | `4.0.0` |
| `~3.2.0` | `>=3.2.0 <3.3.0` | `3.2.9` | `3.3.0` |
| `^0.4.1` | `>=0.4.1 <0.5.0` (0.x caret is minor-locked) | `0.4.9` | `0.5.0` |

**MOS-REG-063** — Disjunction (`||`) MUST NOT be supported. `*`, `latest`, `x` and empty ranges MUST be rejected by the parser. A caller wanting "any" omits the pin entirely, which is a visible, auditable choice.

**MOS-REG-064** — Prerelease versions MUST NOT satisfy a range unless the range names a prerelease in the same `(major, minor, patch)` tuple. `>=3.2 <4` MUST NOT match `3.5.0-rc.1`.

**MOS-REG-065** — A `Deployment` whose `clinical_use_mode = clinical` MUST reject an unconstrained pin at deployment-creation time: a clinical tenant MUST express at least a `^` or `~` bound. Unbounded resolution in a clinical environment is exactly the drift mechanism §6.7.8 demonstrates.

#### 6.7.5 Pinning at job creation

**MOS-REG-066** — The resolved set — the transitive closure of `ServiceVersion`, its `ModelVersion`s, its `PreprocessingSpec`s and the `Deployment` — MUST be written into the `Job` row **in the same transaction that creates the job**, before any enqueue. The record is:

```json
{
  "resolver_version": "1.0.0",
  "snapshot_id": "sha256:9c1f2ab7d0e4358f61b7c09ad3e5f1826b40c79d5aa2e618f3c47b09d2e18a55",
  "epoch": 171402,
  "resolved_at": "2026-01-14T08:22:19Z",
  "capability_id": "pleural_effusion",
  "inputs_hash": "sha256:4f77e1b0a95c3d28e6417ba0dc5931e7f2806b4ad91c5e30742fb8c1069ae3d4",
  "gate_config": { "evidence_gate": true, "regulatory_gate": false },
  "selected": {
    "service_version_id": "sv_01JQ8Z3K2M",
    "service_family": "pulmo.pleural-effusion",
    "version": "3.2.1",
    "image_digest": "sha256:a1c9f0b47d2e5183aa6c04ef7b1d9c3e5f28a704bd6613c2f0a95e77c41b8d02",
    "model_version_ids": ["mv_pulmo_effusion_unet_3_2_1", "mv_pulmo_lung_seg_1_4_0"],
    "preprocessing_spec_ids": ["ps_pulmo_effusion_prep_2_0_0"],
    "deployment_id": "dep_01JQ90A4TT",
    "deployment_environment": "production",
    "deployment_state": "SERVING",
    "deployment_role": "ACTIVE",
    "clinical_use_mode": "research_only",
    "canary_bucket": 613
  },
  "alternatives": [
    { "service_version_id": "sv_01JN7Y1C4P", "version": "3.1.4", "rank_key": "0|0|1|0.881|0|3.1.4" }
  ],
  "excluded": [
    { "service_version_id": "sv_01JR2M8W0K", "stage": "F3",
      "reason_code": "version_suspended", "detail": "mv_pulmo_effusion_unet_3_3_0 SUSPENDED 2026-01-11" }
  ]
}
```

**MOS-REG-115** — `selected` MUST carry `deployment_environment` and `deployment_state` alongside `deployment_id`, `deployment_role` and `clinical_use_mode`. `environment` is immutable for the life of a `Deployment` row — it is part of the slot key (§6.8) — so the pinned `deployment_environment` is by construction the environment the job executed in, and it is the pinned source Chapter 9 requires for `Result.deployment_environment` (`MOS-SAFE-046`). `state` and `role` are mutable: the pinned values are those at resolution, and Chapter 9's `deployment_state_at_execution` and `deployment_role_at_execution` MUST be read from the `deployments` row at the moment the worker begins executing, not copied from the pin. That read is a provenance snapshot only; it MUST NOT change which `ServiceVersion`, `ModelVersion` or `Deployment` the attempt runs (`MOS-REG-067`), and it MUST NOT be widened into a re-resolution. Both pairs MUST be retained: a divergence between the pinned pair and the executed pair is the record that the deployment changed between job creation and execution, and it MUST NOT be reconciled by overwriting either.

**MOS-REG-067** — On **every** retry and every requeue, the dispatcher MUST use `job.resolution.selected` verbatim and MUST NOT call `Resolve` again. Re-resolution on retry MUST be impossible by construction: the retry path MUST read the pin and MUST NOT have the snapshot in scope.

**MOS-REG-068** — If a pinned version has become `SUSPENDED` or `RECALLED` between attempts, the retry MUST NOT run. The job transitions to `REJECTED` (spine §4 — no eligible service is a clinical outcome, not a transport error) with `reason_code = "pinned_version_recalled"` or `"pinned_version_suspended"`. Re-analysis requires a **new** Job carrying `supersedes_job_id`, which re-resolves against the current snapshot and is therefore visible as a distinct clinical event.

#### 6.7.6 Zero candidates

**MOS-REG-069** — `Decision: ZERO_CANDIDATES` MUST terminate the job in `REJECTED`, never `FAILED`, and MUST never fall back to another version. The reason code MUST be one of the F-stage codes in `MOS-REG-055` plus `capability_unknown`, and MUST be surfaced in the API as problem+json with `class: clinical_rejection` (Chapter 10) and in every UI as visually distinct from a failure (spine §4).

**MOS-REG-070** — `Outcome.Excluded` MUST be persisted on the Job and returned by `GET /api/v1/jobs/{id}`. "No service was available" without the per-candidate reason is not an acceptable clinical rejection message.

#### 6.7.7 Worked example: the same request, resolving differently

This is the failure mode that makes pinning non-optional. One tenant (`tnt_7f3a`), one capability (`pleural_effusion`), one declaration (`">=3.2 <4"`), one study (`stu_4410`).

**State A — 14 January 2026, epoch 171402.** Registry holds `pulmo.pleural-effusion` `3.1.4` (DEPRECATED) and `3.2.1` (APPROVED, deployment `dep_01JQ90A4TT`, role ACTIVE). Job `job_9f21` is created; the record of `MOS-REG-066` is written. The service runs and reports **right effusion 642 mL**. The platform writes one SEG series; its `SeriesInstanceUID` derives from `(idempotency_key, model_id, model_version, output_index)` (spine §7), so it is a function of `mv_pulmo_effusion_unet_3_2_1`.

**State B — 2 March 2026, epoch 184213.** `3.4.0` is published: new weights, `operating_point` moved `0.45 → 0.38`, `EvaluationRun er_01JT5R2Q8N`, `ValidationReport vr_0117`. It is deployed ACTIVE; `3.2.1` is moved to `DEPRECATED` and its deployment role to `STANDBY`.

**3 March 2026, 06:12** — the worker host running `job_9f21` is lost. `MOS-EXEC` requeues attempt 2.

*Without pinning*, attempt 2 resolves against epoch 184213:

```json
{ "epoch": 184213,
  "selected": { "service_version_id": "sv_01JS6H4N1D", "version": "3.4.0",
                "model_version_ids": ["mv_pulmo_effusion_unet_3_4_0"] } }
```

Three things go wrong simultaneously, and none of them raises an exception:

1. **The number changes under a stable job id.** The same `job_9f21` now reports **705 mL** instead of 642 mL. A clinician who looked at this study in January and again in March sees two volumes for one exam with one job id.
2. **The provenance record becomes a lie.** The panel shows `ValidationReport vr_0091` (for 3.2.1) if it reads the job's declaration, or `vr_0117` if it reads the worker's report — and the two halves of the record now disagree about which artifact produced the pixels.
3. **Idempotency breaks in PACS.** Because the derived UID is a function of `model_version`, attempt 2 mints a *different* `SeriesInstanceUID`. The QIDO-RS skip-if-present check (spine §7) does not match, the retry re-runs instead of resuming, and the study accumulates **two overlapping effusion SEG series** with no way to tell which one the report's volume came from. The deterministic-UID contract is defeated not by the DICOM writer but by the resolver.

*With pinning* (`MOS-REG-067`), attempt 2 reads `job.resolution.selected` and runs `3.2.1`. The derived UIDs are identical, QIDO-RS finds the series, the retry resumes, and the study carries exactly one SEG. The move to `3.4.0` becomes visible only where it belongs: as a **new** job, created deliberately, with its own resolution record showing `epoch 184213`, its own provenance and its own `vr_0117` — and a `GET /api/v1/jobs?study_id=stu_4410` shows two jobs with two versions and two volumes, which is a legible clinical record rather than a silent contradiction.

**MOS-REG-071** — Both resolution records above MUST be reproducible offline: given `(epoch, snapshot_id, inputs_hash)` and the `registry_changelog`, re-running `Resolve` MUST reproduce `Outcome` byte-for-byte. This is acceptance check 8.

---

### 6.8 Deployment

**MOS-REG-072** — `Deployment` is the only entity that determines whether a version receives work. `ModelVersion.lifecycle_status` and `ServiceVersion.lifecycle_status` MUST NOT be consulted to answer "is this live"; they are consulted only to answer "may this be deployed at all" (filter F3).

```sql
-- Semantic definition. Column names, enum values, the composite unique key and the
-- partial unique index are binding here; the physical form is Chapter 12 §12.9.2.
CREATE TABLE deployment (
  id                 text PRIMARY KEY,
  tenant_id          text NOT NULL,
  environment        text NOT NULL CHECK (environment IN ('dev','staging','production')),
  capability_id      text NOT NULL REFERENCES capability(id),
  service_version_id text NOT NULL REFERENCES artifact(id),
  role               text NOT NULL CHECK (role IN ('ACTIVE','CANARY','SHADOW','STANDBY')),
  traffic_permille   int  NOT NULL DEFAULT 0 CHECK (traffic_permille BETWEEN 0 AND 1000),
  state              text NOT NULL CHECK (state IN
                       ('PENDING','VERIFYING','SERVING','SUSPENDED','DRAINING','RETIRED')),
  clinical_use_mode  text NOT NULL CHECK (clinical_use_mode IN ('research_only','clinical')),
  residency          text NOT NULL DEFAULT 'on_demand'
                       CHECK (residency IN ('resident','on_demand')),
  pin_range          text NOT NULL,
  promotion_policy   jsonb,
  verification_ref   text,
  created_by         text NOT NULL,
  activated_at       timestamptz,
  UNIQUE (tenant_id, environment, capability_id, service_version_id)
);
CREATE UNIQUE INDEX one_active_per_slot ON deployment (tenant_id, environment, capability_id)
  WHERE role = 'ACTIVE' AND state = 'SERVING';
```

The block above is the **semantic** definition: the column names, the enum values, the composite unique key and the partial unique index are binding. Physical form is Chapter 12's (`MOS-STORE-201`): the table is named `deployments`, primary keys are `uuid` v7, enums are `text` + `CHECK` (`MOS-STORE-209`), and `created_at`/`updated_at` are present per `MOS-STORE-210`. The two chapters MUST agree column-for-column; §12.9.2 carries the migration-ready form. That form additionally carries the approval and audit columns that Chapter 9's clinical gate and Chapter 8's audit trail require — `state_reason`, `acceptance_run_id`, `validation_report_id`, `approved_by`, `approved_at`, `retired_at` — which are structure for obligations those chapters own and which this chapter therefore does not restate.

**MOS-REG-073** — `role` and `state` are orthogonal and MUST both exist. `role` says what traffic the deployment *should* get; `state` says whether it *may* get any. A candidate is eligible only when `state = SERVING` and `role` ∈ {`ACTIVE`, `CANARY`} (filter F4).

**MOS-REG-074** — There MUST be at most one `role = ACTIVE, state = SERVING` deployment per `(tenant, environment, capability)`, enforced by the partial unique index above. Two live actives is a configuration error, not a load-balancing strategy.

#### State machine

| from | to | trigger | preconditions |
|---|---|---|---|
| — | `PENDING` | `deployment.create` | F1–F3, F7, F9 pass for the target version |
| `PENDING` | `VERIFYING` | automatic | image pulled and signature + SBOM + attestations verified (§6.9) |
| `VERIFYING` | `SERVING` | automatic | worker startup self-test passes (`MOS-REG-036`) **and** the site smoke suite passes (Chapter 7) |
| `VERIFYING` | `PENDING` | automatic | any verification step failed; `verification_ref` records which |
| `SERVING` | `SUSPENDED` | `deployment.suspend`, or artifact `SUSPENDED`/`RECALLED`, or self-test failure at worker restart | — |
| `SUSPENDED` | `SERVING` | `deployment.promote` | re-run `VERIFYING` |
| `SERVING`/`SUSPENDED` | `DRAINING` | `deployment.retire`, or artifact `RECALLED` | no new dispatch; in-flight jobs finish |
| `DRAINING` | `RETIRED` | automatic | zero jobs referencing this deployment in a non-terminal state |

**MOS-REG-075** — A deployment MUST NOT reach `SERVING` without a recorded `verification_ref`. A `SERVING` row with `verification_ref IS NULL` is a conformance violation and MUST be caught by a CI invariant test against the schema.

#### Blue/green

**MOS-REG-076** — Blue/green is two deployments in the same slot: the live one at `role = ACTIVE, state = SERVING`, the candidate at `role = STANDBY, state = SERVING`. The standby is fully verified and warm but receives no jobs (F4 excludes `STANDBY`).

**MOS-REG-076a** — `residency` declares the GPU residency class the deployment demands of `tritond`; the class semantics, the eviction rules and the `footprint_bytes` arithmetic are Chapter 13 §13.10.3. The `PENDING → VERIFYING` transition MUST refuse a deployment whose pinned `resident`-class footprints would exceed `budget_bytes` on every eligible node (Chapter 13 `MOS-OPS-084`), and MUST refuse a deployment whose model's p95 load exceeds 30 s unless `residency = 'resident'` (`MOS-OPS-092`). This is the Deployment gate those two requirements name. The value set is exactly the two classes §13.10.3 defines, `resident` and `on_demand`, and a third value MUST NOT be added to the CHECK until that section states its load point, its eviction rule and how it counts against `budget_bytes`: a storable residency value with no defined behaviour is a value `tritond` cannot act on. (`evictable` was such a value and is removed here; an `on_demand` deployment is already evictable after its minimum hold window, which is the behaviour the name appeared to promise.)

**MOS-REG-077** — Cutover MUST be a single transaction swapping the two `role` values. It MUST NOT involve deleting, recreating or re-verifying either row.

**MOS-REG-078** — Rollback is the same swap in reverse and MUST be executable without re-running `VERIFYING`, provided the target version's `lifecycle_status` is not `SUSPENDED` or `RECALLED`. Rollback into a recalled version MUST be refused with HTTP 409. Rollback MUST complete in under 5 seconds of database time; that is the whole point of keeping the standby warm.

#### Canary

**MOS-REG-079** — A canary is a deployment at `role = CANARY, state = SERVING` with `traffic_permille > 0`, selected by the deterministic bucket of `MOS-REG-059`. Canary traffic is therefore **study-sticky**: every job and every retry for one study resolves to the same side of the split.

**MOS-REG-080** — `promotion_policy` MUST be declarative and MUST NOT auto-promote in `clinical_use_mode: clinical`:

```yaml
promotion_policy:
  min_jobs: 200
  max_failed_fraction: 0.01
  max_rejected_fraction: 0.15
  max_disagreement_vs_active:
    metric: dice
    capability_scope: pleural_effusion
    threshold: 0.08            # mean per-case Dice gap against the ACTIVE version
  min_observation_hours: 72
  auto_promote: false          # MUST be false when clinical_use_mode = clinical
  approver_role: clinical_lead
```

**MOS-REG-081** — When `auto_promote: false`, the platform MUST compute and display the policy's outcome but MUST require an explicit `deployment.promote` call by a holder of `approver_role`, and MUST record the approver identity on the resulting audit event.

#### Shadow

**MOS-REG-082** — A `role = SHADOW` deployment runs against the same studies as the ACTIVE one, in a **separate Job** carrying `shadow_of_job_id`. Its `ResultBundle` MUST be persisted as a `Result` with `visibility = shadow`, MUST NOT be converted to DICOM, and MUST NOT be written to any PACS. Shadow results are the substrate for continuous site monitoring — disagreement rate and drift (spine §10) — and are the only mechanism by which a candidate accumulates local evidence without clinical exposure.

**MOS-REG-083** — `clinical_use_mode` lives on `Deployment`, not on `Tenant` and not on the artifact (spine §11). The same signed `ServiceVersion` MUST be deployable as `research_only` in one environment and `clinical` in another with no repackaging. The value MUST be copied into `job.resolution.selected.clinical_use_mode` at job creation so the DICOM writer (Chapter 4) and the safety controls (Chapter 9) read a pinned value, not a live lookup.

---

### 6.9 Packaging, signing and supply chain

**MOS-REG-084** — The signed unit is an **OCI image manifest whose config blob is the MedicalOS artifact manifest**. This is the noun that `signed images`, `signed model artifacts`, `SBOM` and the install flow all attach to; before this section, those requirements had no unit to apply to.

| artifact kind | OCI artifact type | config blob media type | layers |
|---|---|---|---|
| `service_version` | runnable image | `application/vnd.medicalos.service.manifest.v1+json` | standard image layers |
| `model_version` | non-runnable OCI artifact | `application/vnd.medicalos.model.manifest.v1+json` | weights blob, `PreprocessingSpec` blob, golden-fixture volume blob, golden-fixture expected-tensor blob |
| `preprocessing_spec` | non-runnable OCI artifact | `application/vnd.medicalos.preprocessing.manifest.v1+json` | spec JSON blob |

**MOS-REG-085** — Model weights MUST NOT be baked into a service image. A `service_version` image whose layers contain a file matching the weights digest of any referenced `model_version` MUST be rejected at admission. Weights arrive at runtime by id and digest; this is what makes runtime model resolution meaningful at all.

**MOS-REG-086** — The `PreprocessingSpec` blob inside the model artifact MUST have a digest equal to `artifact.content_digest` of the corresponding `preprocessing_spec` registry row. Co-location under one signature and registry identity by reference MUST be the same bytes (spine §7).

#### Attached evidence (OCI 1.1 referrers)

**MOS-REG-087** — The following MUST be attached to the artifact digest via the OCI Referrers API, with `subject` set to that digest:

| referrer `artifactType` | content | required from |
|---|---|---|
| `application/vnd.dev.cosign.simplesigning.v1+json` | keyless or key-based signature over the manifest digest | 0.1.0 (warn) / 0.2.0 (enforced) |
| `application/vnd.cyclonedx+json` (CycloneDX 1.6) | SBOM of the image or the model artifact | 0.3.0 |
| `application/vnd.in-toto+json` with predicate `https://slsa.dev/provenance/v1` | build provenance: source repo, commit, builder id | 0.3.0 |
| `application/vnd.in-toto+json` with predicate `https://medicalos.org/attestation/validation-report/v1` | `{ "validation_report_id", "validation_report_digest", "evaluation_run_id", "dataset_version_digest", "approver" }` | 0.3.0 for `clinical_use_mode: clinical` |

**MOS-REG-088** — The validation-report attestation exists because signing the model and leaving the *claim about* the model in mutable storage is backwards. The `ValidationReport` (Chapter 7) MUST be offline-verifiable from this attestation alone: digest match plus signature chain, with no call to the issuing platform.

#### What §63-style supply-chain requirements attach to

**MOS-REG-089** — Each supply-chain requirement MUST name its unit, its verifier and its enforcement point. A requirement without all three is not implementable.

| requirement | unit | verification | enforcement point |
|---|---|---|---|
| signed images | `service_version` OCI manifest digest | `cosign verify` against the identity registered for `legal_manufacturer.id` (`MOS-REG-028`) | registry admission **and** node pull |
| signed model artifacts | `model_version` OCI manifest digest | same | registry admission **and** worker startup, before weights are loaded |
| SBOM | CycloneDX referrer | present, parses, ≥ 1 component, `metadata.component.version` equals `artifact.version` | registry admission |
| dependency scanning | SBOM components | scanner run against the advisory DB; result stored on the artifact row | publish time and a daily rescan of every `APPROVED` artifact |
| vulnerability scanning | image layers | container scanner; critical findings block `PENDING → VERIFYING` | deployment creation |
| provenance | SLSA attestation | builder id in the allowed set; source repo matches `artifact_family.owner_org_id` | registry admission |
| signed claims | validation-report attestation | digest equals the stored `ValidationReport` digest; approver identity resolvable | registry admission, gate F8 |

**MOS-REG-090** — Verification MUST fail closed at every enforcement point. A verification error MUST NOT degrade to a warning, and MUST NOT be bypassable by configuration in `environment: production`.

**MOS-REG-091** — A daily rescan producing a new critical finding MUST move the artifact to `SUSPENDED` automatically and MUST NOT move it to `RECALLED`; `RECALLED` is a human decision (`MOS-REG-021`).

**MOS-REG-092** — Key or identity revocation MUST invalidate future verifications only. Already-produced results MUST NOT be retroactively invalidated by revocation; the response to a compromised publisher identity is `RECALLED` plus the impact query of `MOS-REG-040`.

**MOS-REG-093** — The technical boundary between core and proprietary components is the OCI artifact plus manifest, out-of-process, communicating only over the Medical Service Contract (Chapter 2). Licence text is not a boundary; the packaging format is. A proprietary service MUST be installable and governable with no source access and no in-process plugin loading.

**MOS-REG-094** — The install flow for a third-party artifact is exactly: `fetch → verify signature → verify SBOM and attestations → validate manifest against its JSON Schema → compatibility check against node inventory → create Deployment (PENDING) → VERIFYING (self-test + smoke suite) → SERVING`. No step in this flow performs, confers or implies clinical validation; the platform MUST NOT label any install step "clinical validation".

---

### 6.10 Versioning and the compatibility matrix

**MOS-REG-095** — Every artifact version is semver `MAJOR.MINOR.PATCH`. The bump rules are normative, not stylistic:

| change | `ServiceVersion` | `ModelVersion` | additional obligation |
|---|---|---|---|
| output schema field removed or renamed | MAJOR | MAJOR | — |
| capability removed from claims | MAJOR | MAJOR | — |
| `io` tensor shape, dtype, layout or orientation changed | MAJOR | MAJOR | new `EvaluationRun` |
| new capability claimed | MINOR | MINOR | new `EvaluationRun` for the new capability |
| weights changed | — | ≥ MINOR | new `EvaluationRun`; re-pass `AcceptanceCriteria` |
| `operating_point` changed | ≥ MINOR | ≥ MINOR | new `EvaluationRun`; re-pass `AcceptanceCriteria` |
| `PreprocessingSpec` ref changed | ≥ MINOR | ≥ MINOR | new `EvaluationRun` |
| `SeriesSelector` or applicability envelope narrowed | ≥ MINOR | — | — |
| runtime backend conversion (§6.10.2) | ≥ PATCH | ≥ PATCH | new `EvaluationRun`; `derived_from` set |
| dependency bump with no numeric effect | PATCH | PATCH | golden-fixture equivalence recorded |

**MOS-REG-096** — The registry MUST reject a publish whose declared bump is smaller than the rule above requires, computed by diffing the new manifest against the highest existing version in the same family. This is a mechanical check on the manifest, not a judgement call.

#### 6.10.1 The compatibility block

**MOS-REG-097** — `compatibility` MUST be expressed with the range grammar of `MOS-REG-062` for software and with **exact pins** for anything a serialized artifact is compiled against.

```yaml
compatibility:
  medicalos_api: ">=1.0 <2"
  service_contract: "1.2"
  runtime:
    engine: triton
    engine_version: ">=24.08 <25.00"
    backend: tensorrt
    tensorrt_version: "10.3.0"        # EXACT — a plan is not forward-compatible
    cuda: "12.4"                      # EXACT for tensorrt_plan
    driver_min: "550.54.15"
    gpu_architectures: ["sm_86", "sm_89"]
    gpu_memory_mib: 11264
```

**MOS-REG-098** — A floor such as `min_triton: "2.x"` is **insufficient and MUST be rejected** for `weights.format: tensorrt_plan`. A serialized TensorRT plan is compiled for a specific TensorRT version and a specific set of GPU architectures; a floor expresses neither. For `tensorrt_plan` the registry MUST require: exact `tensorrt_version`, exact `cuda`, non-empty `gpu_architectures`, and a `driver_min`.

**MOS-REG-099** — Filter F7 MUST match `gpu_architectures` by **exact set membership** against the environment's `NodeProfile` inventory, and MUST compare `tensorrt_version` and `cuda` by exact equality when `backend: tensorrt`. A plan built for `sm_86` MUST NOT be dispatched to an `sm_90` node; it will either fail to deserialize or, worse, be silently rebuilt by the runtime, producing an artifact that is not the one that was evaluated.

**MOS-REG-100** — `NodeProfile` rows are supplied by the deployment plane (Chapter 13) and MUST carry at minimum `node_id`, `environment`, `gpu_model`, `gpu_architecture`, `gpu_memory_mib`, `driver_version`, `cuda_version`, `tensorrt_version`, `engine_version`. A node with an incomplete profile MUST be treated as satisfying nothing.

**MOS-REG-101** — The inference runtime MUST be addressed through the `engine` discriminator rather than assumed. Whether the runtime itself is replaceable in the sense that the DICOM store is replaceable remains an open question (Chapter 16); this chapter MUST NOT be read as settling it. What is settled: no manifest field, no filter and no resolution key may be named after a specific inference server.

#### 6.10.2 Backend conversion is a new ModelVersion

**MOS-REG-102** — Converting a model to a different serving backend — ONNX → TensorRT plan, FP32 → FP16 or INT8, a different TensorRT or CUDA version, or a different GPU architecture target — MUST produce a **new `ModelVersion`**. It MUST NOT be published as a variant, a build flavour, an artifact tag, or a per-node configuration of an existing version.

**MOS-REG-103** — The converted version MUST set `derived_from` to the source `model_version_id`, MUST carry its **own** `evaluation_run_id`, and MUST re-pass the Capability's `AcceptanceCriteria`. Copying the source version's metrics MUST be rejected: the registry MUST refuse a publish whose `evaluation_run_id` is already referenced by another `ModelVersion`.

**MOS-REG-104** — A golden-fixture numeric equivalence check (maximum absolute difference and post-threshold Dice between source and converted outputs) MUST be recorded on the converted version as `conversion_equivalence`, and MUST NOT be accepted as a substitute for the evaluation run. Quantisation changes clinical behaviour in exactly the small, low-contrast cases that matter most.

```yaml
# Converted version: same weights, different serving numerics, its own evidence.
family: pulmo.effusion-unet
version: "3.2.2"
spec:
  derived_from: "mv_pulmo_effusion_unet_3_2_1"
  evaluation_run_id: "er_01JQ0F6D3S"      # distinct from er_01JP4T9X7B
  weights:
    oci_ref: "ghcr.io/pulmoai/effusion-unet-trt"
    digest: "sha256:c3b8102f9e57ad46b0d1e29f7c64a385107bd2e94fa60c718e35d9b046af2c91"
    format: tensorrt_plan
    size_bytes: 212930560
  conversion_equivalence:
    source_model_version_id: "mv_pulmo_effusion_unet_3_2_1"
    fixture_digest: "sha256:0c47ab9f2d1e6b83f5490ad72c1e8b60743fd29ca5b8e01274f6390dbc5a12e7"
    max_abs_logit_diff: 0.0037
    post_threshold_dice: 0.9991
  runtime:
    engine: triton
    engine_version: ">=24.08 <25.00"
    backend: tensorrt
    tensorrt_version: "10.3.0"
    cuda: "12.4"
    driver_min: "550.54.15"
    gpu_architectures: ["sm_86"]
    gpu_memory_mib: 8192
```

**MOS-REG-105** — Because `3.2.1` and `3.2.2` are distinct `ModelVersion`s, the deterministic DICOM UID derivation (spine §7) yields distinct `SeriesInstanceUID`s. A backend conversion is therefore visible in PACS as a distinct result lineage, which is correct: the numerics differ.

**MOS-REG-106** — Deprecation and end-of-life: a `DEPRECATED` version MUST remain resolvable (ranked last by P5) for at least 180 days unless suspended or recalled, so that pinned jobs can be replayed. The registry MUST expose `deprecated_at` and `eol_at` on the artifact row and MUST refuse to `RETIRE` the last `SERVING` deployment of a capability in a `clinical_use_mode: clinical` environment without an explicit force flag and an audit event.

---

### 6.11 Registry API invariants

Chapter 10 owns HTTP conventions, pagination, authentication and the problem+json shape, and its table 10.2-B is the complete `/api/v1` surface (`MOS-API-012`). The table below is the registry-owned subset of that surface; it is complete for the five deployment write routes that drive the state machine of §6.8 (10.2-B rows 51, 53, 54, 54a, 54b), and where a path, a verb or a permission differs between the two tables, Chapter 10 is normative. The registry adds these semantics.

| route | semantics |
|---|---|
| `POST /api/v1/service-versions` | 201 with `content_digest`; identical republish → 200; conflicting republish → 409 (`MOS-REG-019`) |
| `PATCH /api/v1/service-versions/{id}` | 405 — manifests are immutable (`MOS-REG-018`) |
| `POST /api/v1/service-versions/{id}/status` | body `{ "status": "SUSPENDED", "reason": "self-test regression on sm_89" }`; transitions per `MOS-REG-021` |
| `GET /api/v1/service-versions/{id}/impact` | affected jobs, results and `SeriesInstanceUID`s (`MOS-REG-040`) |
| `POST /api/v1/deployments` | creates in `PENDING`; verification is asynchronous |
| `PATCH /api/v1/deployments/{id}` | changes `traffic_permille`, `pin_range`, `promotion_policy`, `residency` and `clinical_use_mode` only; `role` and `state` are never edited here (`MOS-REG-073`) |
| `POST /api/v1/deployments/{id}/promote` | canary → active, standby ↔ active swap, or `SUSPENDED` → `SERVING` via `VERIFYING` (`MOS-REG-077`, `MOS-REG-078`) |
| `POST /api/v1/deployments/{id}/suspend` | `state` → `SUSPENDED`; reversible, `reason` required; no new dispatch, in-flight jobs finish (§6.8) |
| `POST /api/v1/deployments/{id}/retire` | `state` → `DRAINING`, then `RETIRED` automatically when no non-terminal job references the row (§6.8); there is no delete route |
| `GET /api/v1/capabilities/{capability_id}/resolution` | **dry-run resolution** (Chapter 10, table 10.2-B row 24) |

**MOS-REG-107** — Every registry read MUST be tenant-scoped through the same RLS chokepoint as the rest of the platform (Chapter 8). Public artifacts are visible cross-tenant; `private` and `org` artifacts MUST NOT be enumerable by a non-entitled tenant, including through 404-vs-403 timing.

**MOS-REG-108** — Registry write endpoints MUST require the permissions named in this chapter: `artifact.publish`, `artifact.approve`, `artifact.suspend`, `artifact.recall`, `artifact.status.set`, `capability.update`, `deployment.create`, `deployment.update`, `deployment.promote`, `deployment.suspend`, `deployment.retire`. Chapter 8 owns their assignment to roles.

**MOS-REG-109** — `artifact.recall` MUST be separately grantable from `artifact.status.set`. Recall is irreversible and has a patient-facing consequence; it MUST NOT be reachable by the routine status-management permission.

**MOS-REG-110** — `GET /api/v1/capabilities/{capability_id}/resolution` (Chapter 10 row 24) MUST run the production resolver against the current snapshot and return the full `Outcome` — `Ranked`, `Excluded` with reason codes, `RankKey`s, `snapshot_id` and `epoch` — without creating a job. It MUST accept an optional `epoch` query parameter to replay a historical snapshot. All parameters are query parameters; the endpoint is `GET` because it is side-effect-free (`MOS-API-058`). Chapter 10 owns the path, verb and permission; this chapter owns the payload. This endpoint is what makes resolution debuggable without a second implementation (`MOS-REG-051`).

**MOS-REG-111** — The OpenAPI document for every route in this chapter MUST be generated from the same schema source as the manifests (`MOS-REG-016`). A hand-edited registry path in `openapi.yaml` MUST fail CI.

---

### Acceptance criteria

Each check is executable by CI or by a reviewer against a running 0.3.0 deployment.

1. **Schema closure.** For every value of `artifact.kind`, a JSON Schema exists at `https://schemas.medicalos.org/artifact/<kind>/<version>.json`, sets `additionalProperties: false` at every object level, and is generated — not hand-written — from the in-repo schema source. Adding an unknown field to any manifest fixture yields HTTP 422. (`MOS-REG-013`–`MOS-REG-016`)
2. **Immutability.** Publishing `pulmo.pleural-effusion 3.2.1`, then publishing the same version with one changed byte, returns 409 quoting the existing `content_digest`; republishing the identical bytes returns 200. A direct `UPDATE artifact SET manifest = ...` as `medicalos_app` fails at the database level. (`MOS-REG-018`, `MOS-REG-019`)
3. **Resolver purity.** A CI import check proves the `resolve` package imports none of `net/http`, `database/sql`, `os`, `math/rand`, and contains no `time.Now` call. A property test over 10 000 generated snapshots runs `Resolve` twice per case and asserts byte-identical `Outcome`. (`MOS-REG-051`, `MOS-REG-053`)
4. **Total order.** Over the same 10 000 generated snapshots, no two entries of `Outcome.Ranked` share a `RankKey`. (`MOS-REG-061`)
5. **Precedence.** A fixture registry with four versions (a tenant-pinned `3.1.4`, a higher-metric `3.3.0`, a newer `3.4.0`, and a `3.5.0` whose model is `SUSPENDED`) resolves to `3.1.4`; removing the tenant pin resolves to `3.3.0`; removing its acceptance metric resolves to `3.4.0`. The `SUSPENDED` candidate never appears in `Ranked` and always appears in `Excluded` with stage `F3`. (`MOS-REG-055`, `MOS-REG-058`)
6. **Range grammar.** A table-driven test asserts every row of the `MOS-REG-062` table, and asserts that `*`, `latest`, `1.x` and `>=1 || >=3` are parse errors. `>=3.2 <4` does not match `3.5.0-rc.1`. (`MOS-REG-062`–`MOS-REG-064`)
7. **Pin cannot resurrect a recall.** Pinning `=3.2.1` after `3.2.1` is `RECALLED` returns `ZERO_CANDIDATES` with `pinned_version_recalled`, and the job terminates `REJECTED`, not `FAILED`, with problem+json `class: clinical_rejection`. (`MOS-REG-056`, `MOS-REG-069`)
8. **Replay.** For a completed job, rebuilding the snapshot at `job.resolution.epoch` from `registry_changelog` reproduces `job.resolution.snapshot_id`, and re-running `Resolve` with `job.resolution.inputs_hash` reproduces `Outcome` byte-for-byte. (`MOS-REG-011`, `MOS-REG-012`, `MOS-REG-071`)
9. **No re-resolution on retry.** Kill a worker mid-job, publish a newer approved version, let the job retry: the second attempt's provenance names the originally pinned `service_version_id` and `model_version_ids`, and QIDO-RS shows exactly one SEG `SeriesInstanceUID` for the study. A static check proves the retry code path has no `Snapshot` in scope. (`MOS-REG-067`)
10. **Drift is visible, not silent.** Reproduce §6.7.7 end to end: the January job and a new March job for the same study both exist, resolve to different versions, report different volumes, and each carries its own `ValidationReport` reference. No single `job_id` ever reports two different volumes. (`MOS-REG-066`–`MOS-REG-071`)
11. **Liveness ownership.** A grep of the codebase finds no occurrence of `STAGING`, `DEPLOYED` or `PRODUCTION` as a value of any artifact `lifecycle_status`, and no dispatch-path read of `lifecycle_status` other than filter `F3`. A schema invariant test asserts no `deployments` row has `state = 'SERVING'` with `verification_ref IS NULL`, and no `(tenant, environment, capability)` has two `ACTIVE`/`SERVING` rows. (`MOS-REG-038`, `MOS-REG-072`, `MOS-REG-074`, `MOS-REG-075`)
12. **Blue/green and rollback.** A cutover swaps roles in one transaction; the subsequent job resolves to the new version; rollback swaps back in under 5 s without re-entering `VERIFYING`; rollback into a `RECALLED` version returns 409. (`MOS-REG-077`, `MOS-REG-078`)
13. **Canary stickiness.** With `traffic_permille: 100`, over 10 000 synthetic studies the canary share is within ±1.5 percentage points of 10 %, and for every study every attempt of every job resolves to the same side. (`MOS-REG-059`, `MOS-REG-079`)
14. **Second capability, zero core changes.** Adding `lung_nodule` — a Capability row, a `ServiceVersion`, a `ModelVersion`, a `PreprocessingSpec`, a `SeriesSelector`, a `Deployment` — and running it end to end produces a diff touching no file under the resolver, the registry schema, the Job schema or the dispatch path. (`MOS-REG-006`)
15. **Supply chain fails closed.** In `environment: production`: an unsigned image, an image signed by an identity not bound to its declared `legal_manufacturer`, a missing CycloneDX referrer, and a missing validation-report attestation each block `PENDING → VERIFYING` with a distinct reason, and none is bypassable by configuration. (`MOS-REG-028`, `MOS-REG-087`, `MOS-REG-089`, `MOS-REG-090`)
16. **No weights in service images.** Publishing a `service_version` whose layers contain a blob matching a referenced model's `weights.digest` is rejected at admission. (`MOS-REG-085`)
17. **TensorRT pinning.** A `tensorrt_plan` manifest with `tensorrt_version: ">=10 <11"` or an empty `gpu_architectures` is rejected. A plan declaring `["sm_86"]` yields `ZERO_CANDIDATES` with `runtime_unsatisfiable` in an environment whose only nodes report `sm_90`. (`MOS-REG-098`, `MOS-REG-099`)
18. **Conversion is a new version with its own evidence.** Publishing a converted model that reuses the source's `evaluation_run_id` is rejected; publishing it with `derived_from` set and a distinct `evaluation_run_id` succeeds and appears in PACS under a distinct `SeriesInstanceUID`. (`MOS-REG-102`–`MOS-REG-105`)
19. **Bump enforcement.** Publishing a version whose weights digest changed but whose version differs from the previous one only in PATCH is rejected with the required minimum bump named in the problem document. Publishing a changed `operating_point` under an unchanged version is rejected. (`MOS-REG-035`, `MOS-REG-096`)
20. **Capability dictionary is single-source.** A grep finds exactly one table containing SNOMED/RadLex codes; the DICOM SR writer's coded concepts are read from it at runtime; CI validates every code against the pinned terminology release and fails on an unknown concept; every measurement has a non-empty UCUM `unit`; every code entry carries a `narrative_synonyms[]` array, and for a fixture `Result` the `A_lbl` set Chapter 11 builds equals the union of that `Result`'s concept meanings and their `narrative_synonyms[]` entries in this table. (`MOS-REG-042`–`MOS-REG-045`, `MOS-REG-114`)
21. **Recall has an impact query.** After recalling a version that produced results, `GET /api/v1/service-versions/{id}/impact` returns every affected `job_id`, `result_id` and `SeriesInstanceUID`, and the count matches a direct query over pinned `job.resolution` records. (`MOS-REG-040`)
22. **Dry-run parity.** For 100 recorded historical jobs, `GET /api/v1/capabilities/{capability_id}/resolution?epoch=...` with the job's `epoch` and inputs returns an `Outcome` equal to the job's stored `resolution`. (`MOS-REG-110`)

---

[← 5. Execution: Jobs, Queue and Failure Handling](05-execution.md) · [Index](../../MEDICALOS_SPEC.md) · [7. Evidence Plane: Datasets, Evaluation and Validation Reports →](07-evidence.md)
