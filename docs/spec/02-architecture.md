<!-- MedicalOS Specification v0.4.0 — chapter 2 of 19. Normative.
     122 requirements. Do not edit without a requirement-ID review. -->

[← 1. Overview, Scope and Conventions](01-overview.md) · [Index](../../MEDICALOS_SPEC.md) · [3. Medical Data Plane: Gateway, De-identification and Triage →](03-medical-data-plane.md)

---

## 2. Architecture and the Medical Service Contract

This chapter defines two things and nothing else: how MedicalOS is decomposed into planes, and the complete contract a **Medical Service** implements. Everything a vendor needs in order to ship a service that MedicalOS can register, verify, deploy, invoke, and produce DICOM from is in this chapter. Where a referenced document has its own schema (SeriesSelector, PreprocessingSpec, intended-use, ValidationReport), this chapter fixes the *reference form and the obligations*, and names the chapter that owns the document body.

The previous version of this specification stated the service boundary in prose and left the contract unwritten, which is why four sections each claimed to own DICOM generation and none of them specified an invocation. This chapter replaces that with an ABI, a manifest, a return schema, and a lifecycle.

---

### 2.1 Planes

MedicalOS is decomposed into six planes. A plane is a responsibility boundary, not a deployment unit: several planes may run in one process in the 0.1.0 docker-compose profile (Chapter 13).

| Plane | Owns | MUST NOT know about | Chapter |
|---|---|---|---|
| **Presentation Plane** | Web UI, OHIF integration, provenance panel, API clients | PACS credentials, model identity, queue topology | 10, 13 |
| **Control Plane** | `Tenant`, `User`, RBAC, `Service`/`ServiceVersion` registry, `Capability`, `Deployment`, capability resolution, `Job` lifecycle and state, policy decisions, audit | Pixel data, GPU, tensor layouts, model weights | 5, 6, 8, 12 |
| **Medical Data Plane** | DICOM Gateway (the only PACS credential holder), de-identification, UID mapping, study triage, `SeriesSelector` evaluation, all DICOM object *writing* | Job scheduling, model versions, service internals | 3, 4 |
| **Execution Plane** | Queue driver, lease/heartbeat, service invocation, runner pods, Triton fleet, ResultBundle validation | Clinical meaning of a finding, tenant billing, UI state | 5, 13 |
| **Service Plane** | Third-party and first-party `ServiceVersion` code: volume consumption, preprocessing, inference, postprocessing, production of findings and label maps | PostgreSQL, the event bus, the object store, PACS credentials, other tenants, other services, its own Deployment state | **this chapter** |
| **Evidence Plane** | `Dataset`, `DatasetVersion`, `AnnotationSet`, `EvaluationRun`, `AcceptanceCriteria`, `ValidationReport` | The serving path; it MUST NOT be on the critical path of a job | 7 |

**MOS-SVC-001.** Every component MUST belong to exactly one plane. A component that would belong to two MUST be split.

**MOS-SVC-002.** The Service Plane MUST be the only plane that contains vendor-supplied code, and vendor-supplied code MUST NOT run in any other plane.

**MOS-SVC-003.** All access to pixel data by any plane — Presentation, Execution, or Service — MUST go through the DICOM Gateway (Chapter 3, MOS-DATA). No component other than the Gateway may hold a PACS credential, and no viewer may be pointed at the PACS directly.

**MOS-SVC-004.** The Control Plane MUST NOT perform inference, MUST NOT load model weights, and MUST NOT decode pixel data.

**MOS-SVC-005.** The Evidence Plane MUST NOT be consulted synchronously during job execution. Acceptance decisions are pre-computed into `Deployment` state and `ServiceVersion.lifecycle_status` (§2.9) and read from the Control Plane.

**MOS-SVC-006.** Exactly one component writes DICOM objects: the Medical Data Plane's DICOM writer (Chapter 4). No service, no runner, and no worker MUST write, mint, or STOW a DICOM object. The Gateway MUST reject a STOW-RS request presented with a service-scoped token.

**MOS-SVC-007.** A service pipeline that chains several models (for example: lung segmentation → low-attenuation-area measurement inside the resulting mask) MUST be implemented **inside one `ServiceVersion`**. MedicalOS 0.2 does not define a cross-service artifact handoff, and services MUST NOT exchange intermediate tensors with each other.

**MOS-SVC-008.** Chaining across services — the output of service A as the input of service B — is out of scope for 0.1.0–0.3.0 and MUST NOT be implemented by convention (shared buckets, shared scratch mounts, well-known filenames). If it is introduced, it is introduced as a specified artifact plane, not as a side effect.

---

### 2.2 `Service` and `ServiceVersion`

A **Service** is the unit vendors ship and MedicalOS integrates. It is not a bare model, not a container, and not an agent.

| Entity | Mutable | Carries |
|---|---|---|
| `Service` | yes | stable `id`, owning organisation, display name, capability *claims*, contact, lifecycle policy |
| `ServiceVersion` | **no** — immutable after `REGISTERED` | `version`, signed manifest, digest-pinned artifacts, declared capabilities, evidence references, `lifecycle_status` (Chapter 6, `MOS-REG-020`) |

**MOS-SVC-009.** `Service.id` MUST be a reverse-DNS identifier matching `^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?){1,5}$` (for example `com.pulmoai.chest-ct`). It MUST be globally unique within a MedicalOS installation and MUST NOT be reused after a service is deleted.

**MOS-SVC-010.** `ServiceVersion.version` MUST be a SemVer 2.0.0 version without build metadata. A given `(service_id, version)` MUST be registrable exactly once. Re-registering the same pair with a different manifest digest MUST fail with HTTP 409 and the problem type `service-version-immutable` (Chapter 10).

**MOS-SVC-011.** A `ServiceVersion` MUST declare at least one `Capability`. A capability a service declares but does not produce an output for, on an input that satisfied its `SeriesSelector` and applicability envelope, MUST be reported as a per-capability rejection (§2.8.6), never as silence.

**MOS-SVC-012.** MedicalOS MUST treat the `ServiceVersion` publisher named in `legal_manufacturer` as the **manufacturer** of every clinical claim the service makes. MedicalOS makes no clinical claim on a service's behalf (Chapter 9, MOS-SAFE).

---

### 2.3 The `service.yaml` manifest

`service.yaml` is the single canonical manifest. There is no second schema, no separate "agent manifest", and no per-endpoint rendering with different fields. API responses that expose a service are projections of this document.

**MOS-SVC-013.** The manifest MUST validate against the JSON Schema published at `https://spec.medicalos.org/schemas/v1/service-manifest/1.0.0.json`, which MUST be generated from the same type definitions that generate the Go structs, the Python models, and the OpenAPI document (Chapter 10, MOS-API). `https://spec.medicalos.org/schemas/v1/` is the only permitted `$id` authority for every schema named anywhere in this specification (Chapter 10, `MOS-API-084`); `schemas.medicalos.org` is not a second namespace and MUST NOT appear in any `$id` or `$ref`.

**MOS-SVC-014.** `schema_version` MUST be present and MUST be the first key. A platform that does not implement the declared major version MUST refuse registration rather than ignore unknown fields.

**MOS-SVC-015.** Unknown keys MUST cause registration to fail. Manifests are fail-closed; silently dropping a field a vendor believed was enforced is a safety defect.

#### 2.3.1 Complete manifest — sealed mode

```yaml
schema_version: "1.0.0"
kind: MedicalService

metadata:
  id: com.pulmoai.chest-ct
  version: "2.3.0"
  display_name: "PulmoAI Chest CT"
  summary: "Pleural effusion detection and quantification, lung segmentation and LAA-950 emphysema quantification on non-contrast chest CT."
  homepage: "https://pulmoai.example/products/chest-ct"
  support_email: "support@pulmoai.example"
  licence: "LicenseRef-PulmoAI-Commercial-1.0"
  built_at: "2026-08-14T09:12:03Z"
  source_revision: "git+https://git.pulmoai.example/chest-ct@4f1c8b0a9d2e5f7361ab0c4d8e9f1a2b3c4d5e6f"

legal_manufacturer:
  name: "PulmoAI GmbH"
  address:
    street: "Hansaallee 201"
    postal_code: "40549"
    city: "Duesseldorf"
    country: "DE"
  contact_email: "regulatory@pulmoai.example"
  identifiers:
    - scheme: "EUDAMED-SRN"
      value: "DE-MF-000012345"
    - scheme: "GS1-UDI-DI"
      value: "04012345678901"
  dicom_equipment:
    manufacturer: "PulmoAI GmbH"                 # (0008,0070) Type 1 in SEG
    manufacturer_model_name: "PulmoAI Chest CT"  # (0008,1090) Type 1 in SEG
    device_serial_number: "com.pulmoai.chest-ct" # (0018,1000) Type 1 in SEG
    software_versions: "2.3.0"                   # (0018,1020) Type 1 in SEG

execution:
  mode: sealed
  abi: "medicalos-service-abi/1.0"
  sealed:
    image: "ghcr.io/pulmoai/chest-ct@sha256:9f2c1d4b6a8e0f3572c9b1d0e4f6a8c2b5d7e9f1a3c5e7092b4d6f8a0c2e4b61"
    port: 8080
    scheme: http
    user: 65534
    read_only_root_filesystem: true
    scratch_mount: "/var/tmp/medicalos"

capabilities:
  - capability: pleural_effusion
    output_kinds: [finding, measurement, label_map]
    deterministic: false
    operating_points:
      - id: "balanced"
        default: true
        score_threshold: 0.42
        evidence: { validation_report_id: "vr_01J9ZK4Q2M7XW5N8T3B6H0PYCF" }
      - id: "high_sensitivity"
        default: false
        score_threshold: 0.21
        evidence: { validation_report_id: "vr_01J9ZK4Q2M7XW5N8T3B6H0PYCF" }
  - capability: lung_segmentation
    output_kinds: [label_map, measurement]
    deterministic: false
    operating_points:
      - id: "default"
        default: true
        score_threshold: 0.50
        evidence: { validation_report_id: "vr_01J9ZK6R8N1AB4D7F0H3K6M9QSV" }
  - capability: emphysema_laa
    output_kinds: [measurement]
    deterministic: true
    operating_points: []

inputs:
  roles:
    - name: primary_axial
      required: true
      cardinality: exactly_one
      series_selector_ref:
        id: "chest-ct-thin-axial-soft-kernel"
        version: "1.3.0"
        digest: "sha256:2b9d0c1e4f7a6835b2c1d0e9f8a7b6c5d4e3f201a9b8c7d6e5f4a3b2c1d0e9f8"
  applicability:
    slice_thickness_mm: { min: 0.5, max: 3.0 }
    pixel_spacing_mm: { min: 0.40, max: 1.00 }
    min_instances: 80
    z_extent_mm: { min: 180.0, max: 520.0 }
    max_slice_spacing_jitter_mm: 0.05
    max_gantry_tilt_deg: 0.0
    contrast_phase: ["none"]
    patient_age_years: { min: 18, max: 120 }
    rescale_type: ["HU"]

outputs:
  result_bundle_schema: "https://spec.medicalos.org/schemas/v1/result-bundle/1.0.0.json"
  label_map_format: "nifti-gzip"
  max_result_bundle_bytes: 4194304
  max_artifact_bytes: 268435456
  requested_dicom_kinds: [SEG, SR]

resources:
  cpu: { request: "2", limit: "8" }
  memory: { request: "8Gi", limit: "24Gi" }
  gpu:
    required: true
    count: 1
    memory_gb: 16
    compute_capability_min: "8.0"
  scratch_disk_gb: 20

timeouts:
  accept_ms: 5000
  analyze_ms: 900000
  artifact_fetch_ms: 120000
  heartbeat_interval_ms: 15000
  shutdown_grace_ms: 30000

concurrency:
  max_parallel_executions: 1
  max_queue_depth: 0

network:
  egress: ["dicom-gateway"]
  ingress: ["service-invoker"]

intended_use_ref:
  path: "intended-use.yaml"
  digest: "sha256:7c3e9a1b5d8f0246a8c0e2f4b6d8a0c2e4f6081a3c5e7b9d1f3a5c7e9b1d3f50"

evidence:
  validation_report_ids:
    - "vr_01J9ZK4Q2M7XW5N8T3B6H0PYCF"
    - "vr_01J9ZK6R8N1AB4D7F0H3K6M9QSV"

selftest:
  fixture:
    path: "fixtures/golden-chest-ct.tar.zst"
    digest: "sha256:5a1c7e3f9b0d2468ac0e2f4a6c8e0b2d4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b"
  expected_result_bundle_digest: "sha256:c4e6a8021f3b5d7092a4c6e8b0d2f4a6c8e0b2d4f6a8c0e2f4b6d8a0c2e4f6a8"
  max_runtime_ms: 180000
```

#### 2.3.2 The `execution.native` block

A native-mode service replaces `execution.sealed` with `execution.native`. All other blocks are identical.

```yaml
execution:
  mode: native
  abi: "medicalos-service-abi/1.0"
  native:
    distribution:
      package: "medicalos-svc-effusion"
      version: "0.4.2"
      wheel_digest: "sha256:e1f3a5c7092b4d6f8a0c2e4b61d8f0a2c4e6b8d0f2a4c6e8b0d2f4a6c8e0b2d4"
    entry_point: "medicalos_svc_effusion.service:PleuralEffusionService"
    python_requires: ">=3.12,<3.13"
    model_versions:
      - role: effusion_seg
        model_id: "mos.pleural-effusion"
        version: "1.2.0"
        artifact_digest: "sha256:0b2d4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e"
        backend:
          kind: tensorrt
          trt_version: "10.3.0"
          cuda_version: "12.6"
          gpu_arch: "sm_89"
        preprocessing_spec:
          id: "mos.pleural-effusion.prep"
          version: "1.1.0"
          digest: "sha256:4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a"
        io:
          input:
            name: "INPUT__0"
            dtype: float32
            shape: [1, 1, 96, 160, 160]
            layout: "NCZYX"
            voxel_order: "LPS"
          output:
            name: "OUTPUT__0"
            dtype: float32
            shape: [1, 2, 96, 160, 160]
            semantics: "softmax_probabilities"
            channel_labels: ["background", "pleural_effusion"]
```

**MOS-SVC-016.** In `native` mode, every entry of `execution.native.model_versions` MUST pin `artifact_digest` and MUST pin a `preprocessing_spec` by id, version and digest. A `ModelVersion` without a `PreprocessingSpec` MUST NOT be registrable.

**MOS-SVC-017.** `backend` MUST record the accelerator contract the artifact was built for (`kind`, runtime version, GPU architecture). A conversion to a different backend, TensorRT version, or GPU architecture produces a **new `ModelVersion` requiring its own `EvaluationRun`** (Chapter 7, MOS-EVID). Serving a TensorRT plan on a GPU architecture other than the declared one MUST be refused at runner startup.

**MOS-SVC-018.** `io.input.voxel_order` MUST be stated explicitly. A service or model that omits the orientation convention MUST NOT be registrable, because a mirrored segmentation passes every structural check.

**MOS-SVC-019.** `capabilities[].output_kinds` MUST be a non-empty subset of `{finding, measurement, label_map, key_image}`. The platform MUST reject a ResultBundle that carries an output kind the manifest did not declare for that capability.

**MOS-SVC-020.** `capabilities[].operating_points` declares the operating points the service *supports*. It MUST be empty when and only when `deterministic: true`. The clinical bar a capability must clear (`AcceptanceCriteria`) belongs to the `Capability`, not to this manifest (Chapter 6, Chapter 7); which operating point is *applied* is selected per `Deployment` (Chapter 6). Changing an operating point's `score_threshold` MUST require a new `ServiceVersion` and MUST re-enter the gate at §2.9.

**MOS-SVC-021.** Every reported sensitivity, specificity or PPV anywhere in the platform MUST carry the `operating_point.id` and `score_threshold` at which it was measured. A metric without a threshold MUST NOT be displayed or returned by the API. The member carrying that number is named `score_threshold` and is named `score_threshold` everywhere it travels: `capabilities[].operating_points[].score_threshold` in the manifest (MOS-SVC-020), `OperatingPoint.score_threshold` in the native ABI (§2.5.1), `operating_points.<capability>.score_threshold` in the sealed-mode execution request (§2.5.2), `findings[].operating_point.score_threshold` in the `ResultBundle` (MOS-SVC-089), and `result_findings.score_threshold` in storage (Chapter 12). Where another chapter carries the same number under a different member name, the mapping to `score_threshold` is stated once in Chapter 6 (`MOS-REG-050`). Which entity *selects* the value is a separate, open question (Chapter 16, OQ-02) that this requirement does not settle.

**MOS-SVC-022.** `inputs.roles[].series_selector_ref` MUST reference a `SeriesSelector` document by id, version and digest. The selector document's schema and evaluation semantics are defined in Chapter 3 (MOS-DATA). A service MUST NOT perform its own study-level series selection: it receives the series the platform selected and MUST use exactly those.

**MOS-SVC-023.** `inputs.applicability` declares the machine-checkable envelope outside which the service does not claim validity. The platform MUST evaluate it before dispatch and MUST produce a `REJECTED` job (Chapter 5, MOS-EXEC-001) rather than a `FAILED` job when it is not satisfied. The service MUST re-evaluate the envelope and MUST NOT rely on the platform having done so.

**MOS-SVC-024.** `timeouts` MUST satisfy the nesting rule: `analyze_ms` > `accept_ms` + the sum of all internal step budgets + 10 % margin, and the Execution Plane's job timeout (Chapter 5) MUST exceed `accept_ms + analyze_ms + artifact_fetch_ms + shutdown_grace_ms`. A configuration where an outer timeout is shorter than the sum of its children MUST fail deployment validation, because the outer layer retries while the inner request still holds the GPU.

**MOS-SVC-025.** `network.egress` MUST be a subset of `["dicom-gateway"]`. There is no manifest syntax for an arbitrary host. A service requiring outbound internet access MUST NOT be deployable in `clinical` mode (Chapter 9).

**MOS-SVC-026.** `selftest` MUST be present. `expected_result_bundle_digest` is the SHA-256 of the RFC 8785 (JCS) canonicalisation of the ResultBundle the service produces on the shipped golden fixture, with the volatile members `execution_id`, `job_id` and `diagnostics` removed. A runner MUST run the self-test once per pod after readiness and MUST refuse to serve on mismatch.

---

### 2.4 Packaging, signing and verification

A `ServiceVersion` is distributed as a single **OCI artifact**. This is the unit that is signed, transported, mirrored into an air-gapped site, and recorded in provenance.

| Component | OCI media type | Required |
|---|---|---|
| Artifact type | `application/vnd.medicalos.service.v1+json` | yes |
| Config blob (canonical manifest as JCS JSON) | `application/vnd.medicalos.service.config.v1+json` | yes |
| `service.yaml` | `application/vnd.medicalos.service.manifest.v1+yaml` | yes |
| `intended-use.yaml` | `application/vnd.medicalos.intended-use.v1+yaml` | yes |
| SeriesSelector document(s) | `application/vnd.medicalos.series-selector.v1+yaml` | yes |
| Self-test fixture | `application/vnd.medicalos.selftest-fixture.v1.tar+zstd` | yes |
| Model weights (native mode only) | `application/vnd.medicalos.model.v1.tar+zstd` | native only |
| PreprocessingSpec (native mode only) | `application/vnd.medicalos.preprocessing-spec.v1+yaml` | native only |
| Python wheel (native mode only) | `application/vnd.python.wheel` | native only |
| SBOM | `application/spdx+json` (SPDX 2.3) or `application/vnd.cyclonedx+json` (1.6) | yes |

**MOS-SVC-027.** `manifest_digest` is defined as `sha256` over the RFC 8785 JCS canonicalisation of the manifest converted to JSON. It MUST be computed by the platform at registration and MUST NOT be supplied by the vendor.

**MOS-SVC-028.** The OCI artifact MUST be signed with Sigstore cosign. Both key-based and keyless (Fulcio/Rekor) signing MUST be supported. The signature MUST cover the artifact's OCI descriptor digest.

**MOS-SVC-029.** In sealed mode, the runtime image referenced by `execution.sealed.image` MUST be independently signed by the same signing identity and MUST be referenced by digest. A tag reference (`:latest`, `:2.3.0`) MUST be rejected at registration.

**MOS-SVC-030.** The artifact MUST carry an in-toto SLSA v1.0 provenance attestation as an OCI referrer, naming the build platform and the `metadata.source_revision`.

**MOS-SVC-031.** Registration MUST verify, in this order, and MUST fail closed at the first failure: (1) signature validity; (2) signer identity is in the installation's or tenant's trust policy; (3) all layer digests resolve and match; (4) manifest schema validity; (5) `execution.sealed.image` (or `wheel_digest`) is digest-pinned and signed by the same identity; (6) referenced `SeriesSelector` and `intended-use` digests match the embedded layers; (7) SBOM is present and parseable.

**MOS-SVC-032.** The verification result — signer identity, Rekor entry (if keyless), every verified digest — MUST be persisted on the `ServiceVersion` row and MUST be reproducible offline from the artifact alone.

**MOS-SVC-033.** A `ServiceVersion` whose signature no longer verifies (revoked key, removed transparency entry) MUST transition to `SUSPENDED` (§2.9) on the next verification sweep and MUST NOT be dispatched to.

---

### 2.5 The two execution modes

Both modes use the same manifest, the same `ResultBundle`, the same lifecycle, the same governance, the same provenance, and the same platform-side DICOM writing. They differ only in **where code runs and who executes inference**.

| | `native` | `sealed` |
|---|---|---|
| Intended for | first-party services, open models | third-party proprietary services |
| Vendor ships | Python wheel + model weights + PreprocessingSpec | digest-pinned OCI image |
| Weights location | platform artifact store | inside the vendor image; never surrendered |
| Who executes inference | platform, on shared Triton | the service container, with its own runtime |
| Who builds the canonical volume | platform (Chapter 4) | the service, from Gateway DICOMweb |
| Who executes preprocessing | platform, from the pinned `PreprocessingSpec` | the service, per its own pinned spec |
| Invocation | in-process Python ABI call | HTTP ABI over the pod network |
| Pixel access path | platform runner → Gateway | service → Gateway with a scoped token |
| Label map transport | in-memory `numpy` array | HTTP `GET` artifact pull by the platform |
| GPU allocation | shared Triton fleet | dedicated to the service pod |
| Isolation boundary | process + import allowlist + no-egress netns | container + NetworkPolicy + read-only rootfs |
| Geometry verification by platform | direct (platform owns the grid) | recompute from DICOM headers, compare to declared geometry |
| First supported release | 0.1.0 | 0.3.0 |

**MOS-SVC-034.** The execution site MUST NOT move accountability. In both modes, the `legal_manufacturer` of the `ServiceVersion` is accountable for the clinical correctness of the `ResultBundle`, and MedicalOS is accountable for triage, selection, geometry, DICOM identity, provenance and policy.

**MOS-SVC-035.** A `ServiceVersion` MUST declare exactly one mode. Changing mode MUST require a new major `version`, because the evidence base does not transfer across a re-implementation.

#### 2.5.1 Native-mode ABI (`medicalos-service-abi/1.0`)

The platform publishes `medicalos-service-abi`, which a native service imports. The service implements one class.

```python
# medicalos_service_abi/v1.py  (published by the platform; vendors import, never vendor)
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence
import numpy as np


@dataclass(frozen=True, slots=True)
class CanonicalVolume:
    """Source-grid volume in LPS, built by the platform per MOS-IMG (Chapter 4)."""
    array: np.ndarray                          # float32, C-contiguous, shape (nz, ny, nx), modality units (HU for CT)
    spacing_mm: tuple[float, float, float]     # (dz, dy, dx), source PixelSpacing and slice spacing
    origin_lps_mm: tuple[float, float, float]
    direction_lps: tuple[float, ...]           # 9 floats, row-major 3x3, orthonormal
    frame_of_reference_uid: str
    series_instance_uid: str
    sop_instance_uids: tuple[str, ...]         # len == nz; index i is the SOP Instance UID of array[i]
    pixel_digest: str                          # "sha256:..." over array.tobytes(order="C")


@dataclass(frozen=True, slots=True)
class OperatingPoint:
    id: str
    score_threshold: float


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    execution_id: str
    job_id: str
    deadline_at: str                           # RFC 3339 UTC, absolute
    clinical_use_mode: str                     # "research_only" | "clinical"
    capabilities_requested: tuple[str, ...]
    operating_points: Mapping[str, OperatingPoint]   # capability -> selected operating point
    scratch_dir: str                           # writable, per-execution, deleted by the platform

    def heartbeat(self, phase: str, steps_completed: int, steps_total: int) -> None: ...
    def log(self, level: str, message: str, **fields: object) -> None: ...


class InferenceClient(Protocol):
    def infer(
        self,
        model_role: str,                       # a `role` from execution.native.model_versions
        inputs: Mapping[str, np.ndarray],
    ) -> Mapping[str, np.ndarray]: ...


class MedicalService(Protocol):
    ABI_VERSION: str = "1.0"

    def analyze(
        self,
        ctx: ExecutionContext,
        volumes: Mapping[str, CanonicalVolume],   # keyed by inputs.roles[].name
        infer: InferenceClient,
    ) -> dict: ...                                # a ResultBundle, §2.8
```

**MOS-SVC-036.** A native service MUST implement `analyze` and MUST NOT define any other public entry point. It MUST be importable without side effects: module import MUST NOT open sockets, read the filesystem outside the package, or allocate GPU memory.

**MOS-SVC-037.** A native service MUST obtain inference only through the injected `InferenceClient`. It MUST NOT construct a Triton client, MUST NOT open a socket, and MUST NOT load model weights itself.

**MOS-SVC-038.** At registration the platform MUST perform a static import scan of the wheel and MUST reject it if any module in the package's import closure imports, at module scope, any of: `socket`, `http`, `urllib`, `requests`, `httpx`, `aiohttp`, `psycopg`, `psycopg2`, `sqlalchemy`, `asyncpg`, `boto3`, `botocore`, `minio`, `kafka`, `confluent_kafka`, `redis`, `subprocess`, `ctypes`, `multiprocessing.connection`, `tritonclient`.

**MOS-SVC-039.** At runtime the native runner MUST execute `analyze` in a network namespace with no route other than loopback, and with `scratch_dir` as the only writable path. Static scanning is a fast fail, not the control.

**MOS-SVC-040.** `infer` resolves `model_role` to a Triton `(model_name, model_version)` pair maintained by the platform. The service MUST NOT know or depend on that mapping. The platform MUST reject an `infer` call for a role not declared in `execution.native.model_versions`.

**MOS-SVC-041.** `ctx.heartbeat()` MUST be called at least every `timeouts.heartbeat_interval_ms`. A native service that has not heartbeated within twice that interval MUST have its execution aborted by the runner and the job MUST fail with `service_timeout`.

**MOS-SVC-042.** `volumes[role].array` MUST be treated as read-only. A service that mutates it MUST be considered non-conformant; the runner SHOULD set the array non-writeable to make this a hard failure.

#### 2.5.2 Sealed-mode ABI (`medicalos-service-abi/1.0`)

A sealed service exposes exactly these HTTP endpoints on `execution.sealed.port`. There are no others; the platform MUST NOT call any path outside this table, and a service MUST NOT expose an administrative surface.

| Method | Path | Purpose | Success |
|---|---|---|---|
| `GET` | `/healthz` | liveness | `200` empty body |
| `GET` | `/readyz` | readiness; weights loaded | `200` empty body |
| `GET` | `/v1/manifest` | the manifest the image was built with | `200 application/yaml` |
| `POST` | `/v1/selftest` | run the shipped golden fixture | `200 application/json`, a ResultBundle |
| `POST` | `/v1/executions` | start one analysis | `202 application/json`, `{"execution_id": "..."}` |
| `GET` | `/v1/executions/{id}` | poll state, phase, result | `200 application/json` |
| `GET` | `/v1/executions/{id}/artifacts/{artifact_id}` | fetch one label map or key image | `200 application/octet-stream` |
| `DELETE` | `/v1/executions/{id}` | release execution resources | `204` |
| `POST` | `/v1/executions/{id}/cancel` | reserved | `501` in 0.1–0.2; implementable from 0.3 (MOS-SVC-117; spine §4) |

`POST /v1/executions` request body:

```json
{
  "schema_version": "1.0.0",
  "execution_id": "exe_01J9ZM7B4K2N6P8R0T3V5X7ZAC",
  "job_id": "job_01J9ZM7A9C1E3G5J7L9N1Q3S5U",
  "deadline_at": "2026-09-13T12:27:11Z",
  "clinical_use_mode": "research_only",
  "capabilities_requested": ["pleural_effusion", "lung_segmentation", "emphysema_laa"],
  "operating_points": {
    "pleural_effusion": { "id": "balanced", "score_threshold": 0.42 },
    "lung_segmentation": { "id": "default", "score_threshold": 0.50 }
  },
  "gateway": {
    "dicomweb_base_url": "https://gateway.medicalos.svc.cluster.local/tenants/t_01J8Q0/dicomweb",
    "access_token": "eyJhbGciOiJFZERTQSIsImtpZCI6ImdhdGV3YXktMjAyNi0wOSJ9....",
    "token_expires_at": "2026-09-13T12:27:11Z",
    "allowed_methods": ["GET"]
  },
  "input": {
    "study_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.1",
    "roles": [
      {
        "name": "primary_axial",
        "series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
        "frame_of_reference_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.2",
        "instance_count": 312,
        "sop_instance_uids": [
          "1.2.840.113619.2.55.3.604688119.868.1731000000.4.1",
          "1.2.840.113619.2.55.3.604688119.868.1731000000.4.2"
        ]
      }
    ]
  },
  "limits": {
    "max_result_bundle_bytes": 4194304,
    "max_artifact_bytes": 268435456
  }
}
```

`GET /v1/executions/{id}` response while running and after completion:

```json
{
  "schema_version": "1.0.0",
  "execution_id": "exe_01J9ZM7B4K2N6P8R0T3V5X7ZAC",
  "state": "running",
  "phase": "sliding_window_inference",
  "steps_completed": 3,
  "steps_total": 6,
  "started_at": "2026-09-13T12:12:14Z",
  "result_bundle": null,
  "error": null
}
```

```json
{
  "schema_version": "1.0.0",
  "execution_id": "exe_01J9ZM7B4K2N6P8R0T3V5X7ZAC",
  "state": "succeeded",
  "phase": "done",
  "steps_completed": 6,
  "steps_total": 6,
  "started_at": "2026-09-13T12:12:14Z",
  "finished_at": "2026-09-13T12:15:02Z",
  "result_bundle": { "schema_version": "1.0.0", "status": "completed" },
  "error": null
}
```

**MOS-SVC-043.** `state` MUST be one of `accepted`, `running`, `succeeded`, `failed`. `succeeded` means a well-formed `ResultBundle` is available; a bundle with `status: "rejected"` is still `state: "succeeded"`, because a clinical rejection is an outcome, not an error.

**MOS-SVC-044.** `phase` MUST be an open lowercase snake_case string. `steps_completed` and `steps_total` MUST be integers with `0 <= steps_completed <= steps_total`. A service MUST NOT return a float progress value, and the platform MUST NOT synthesise one (Chapter 5, MOS-EXEC-001).

**MOS-SVC-045.** `POST /v1/executions` MUST be idempotent on `execution_id`. A repeat with the same `execution_id` and an identical body MUST return `202` referring to the same execution. A repeat with the same `execution_id` and a different body MUST return `409`.

**MOS-SVC-046.** The service MUST accept within `timeouts.accept_ms` and MUST NOT perform inference inside the `POST` handler.

**MOS-SVC-047.** The service MUST use `gateway.access_token` only against `gateway.dicomweb_base_url`, MUST NOT persist it beyond the execution, MUST NOT log it, and MUST NOT issue any HTTP method other than those in `allowed_methods`. The token is a Gateway token scoped to one study; it is not a PACS credential.

**MOS-SVC-048.** All label maps and key images MUST be served from `GET /v1/executions/{id}/artifacts/{artifact_id}`. A service MUST NOT push artifacts anywhere. Presigned upload URLs are not part of this contract and MUST NOT be issued by the platform.

**MOS-SVC-049.** The platform MUST `DELETE /v1/executions/{id}` after it has fetched and verified every artifact, and the service MUST then be free to delete its scratch state. The service MUST retain the execution for at least `timeouts.artifact_fetch_ms` after reaching `succeeded` if no `DELETE` arrives.

**MOS-SVC-050.** The service container MUST run as a non-root user, with a read-only root filesystem, with no `CAP_NET_RAW` or `CAP_SYS_ADMIN`, and with a NetworkPolicy permitting egress only to the Gateway and ingress only from the invoker. Enforcement details are in Chapter 8 (MOS-SEC) and Chapter 13 (MOS-OPS).

**MOS-SVC-051.** The service MUST NOT write PHI to stdout, stderr, or any header. Its log stream is collected by the platform, and Chapter 8's PHI-in-logs prohibition applies to it verbatim.

---

### 2.6 Two sequence walkthroughs

Every message named below is defined in this chapter or in the chapter cited on the line.

#### 2.6.1 Native mode — `mos.chest-ct` 1.0.0, capability `pleural_effusion`

1. Study `1.2.840...1731000000.1` arrives at the Gateway; the platform triages once and evaluates every deployed `ServiceVersion`'s `SeriesSelector` (Chapter 3, MOS-DATA).
2. The selector `chest-ct-thin-axial-soft-kernel@1.3.0` matches series `...1731000000.4` (312 instances, 1.0 mm, kernel `STANDARD`, axial). The applicability envelope of §2.3.1 is evaluated and passes.
3. The Control Plane creates one `Job` per matching `(ServiceVersion, Study, selected Series set)`, pins the resolved `ServiceVersion` into the job row, and enqueues to `medicalos.svc.mos.chest-ct.work` (Chapter 5, MOS-EXEC-003).
4. A native runner pod claims the job with a lease. The pod image is the runner bundle materialised at deployment time: platform runner base image + the pinned wheel `medicalos-svc-effusion==0.4.2`, content-addressed by `(base_image_digest, wheel_digest)`.
5. At pod start the runner runs the golden-fixture self-test: it executes the pinned `PreprocessingSpec` on the model artifact's golden-fixture volume at `PreprocessingSpec.golden_fixture.path` (a NIfTI or NRRD volume, Chapter 4 MOS-IMG-053) and compares the model-space tensor hash to `golden_fixture.output_tensor_sha256` (Chapter 4, MOS-IMG-054). This is a different test from the `selftest.expected_result_bundle_digest` check of MOS-SVC-026, which runs on `fixtures/golden-chest-ct.tar.zst`. Mismatch ⇒ the pod refuses readiness; no job is claimed.
6. The runner retrieves the 312 instances via `GET {gateway}/studies/{study}/series/{series}/instances/{sop}` (WADO-RS, `multipart/related; type="application/dicom"`).
7. The runner builds the `CanonicalVolume`: LPS, slices sorted by `ImagePositionPatient` projected on the slice normal, `RescaleSlope`/`RescaleIntercept` applied, direction cosines honoured (Chapter 4). It computes `pixel_digest` over `array.tobytes(order="C")`.
8. The runner calls `service.analyze(ctx, {"primary_axial": vol}, infer)`.
9. The service calls `ctx.heartbeat("preprocessing", 1, 5)`; the runner renews the queue lease.
10. The service calls `infer("effusion_seg", {"INPUT__0": patch})` per sliding-window patch. The runner translates each call into a Triton `ModelInferRequest(model_name="mos_pleural_effusion", model_version="12")` over gRPC, and records cumulative GPU time.
11. The service applies its postprocessing, inverts the model-space transform back to the source grid, and returns a `ResultBundle` dict.
12. The runner validates the bundle against `result-bundle/1.0.0.json`, checks that every `source_sop_instance_uids` entry is a member of `vol.sop_instance_uids`, checks the declared `geometry` equals the `CanonicalVolume` geometry within `1e-4`, and checks every label map array shape equals `geometry.shape`.
13. The runner writes the bundle and the label maps to the object store, and records `diagnostics` into provenance (Chapter 9, MOS-SAFE; Chapter 12, MOS-DATA).
14. The Medical Data Plane writes DICOM SEG and SR with deterministically derived UIDs, re-mapping the de-identified UIDs back to source UIDs (Chapter 3, Chapter 4).
15. The Control Plane transitions the `Job` to `COMPLETED` in the same transaction that inserts the `Result` row (Chapter 5, MOS-EXEC-002).

#### 2.6.2 Sealed mode — `com.pulmoai.chest-ct` 2.3.0

1. Steps 1–3 are identical to §2.6.1; the work inbox is `medicalos.svc.com.pulmoai.chest-ct.work`.
2. A sealed-service invoker claims the job. The service pod is already running from the `Deployment` (Chapter 6): image `ghcr.io/pulmoai/chest-ct@sha256:9f2c...`, NetworkPolicy egress `dicom-gateway` only.
3. The invoker calls `GET /v1/manifest` and compares its `manifest_digest` (MOS-SVC-027) to the registered one. Mismatch ⇒ job `FAILED` with `service_manifest_mismatch`; the `ServiceVersion` transitions to `SUSPENDED`.
4. On first readiness after pod start the invoker calls `POST /v1/selftest` and compares the JCS digest of the returned bundle (minus `execution_id`, `job_id`, `diagnostics`) to `selftest.expected_result_bundle_digest`. Mismatch ⇒ the pod is drained; no job is dispatched to it.
5. The invoker mints a Gateway token scoped to `GET` on study `1.2.840...1731000000.1` with `exp` equal to the job deadline (Chapter 8, MOS-SEC), and `POST /v1/executions` with the body in §2.5.2.
6. The service pulls the 312 instances itself via WADO-RS through the Gateway and builds its own canonical volume.
7. The invoker polls `GET /v1/executions/{id}` every 15 000 ms (`timeouts.heartbeat_interval_ms`), renewing the queue lease on each successful poll and projecting `phase`/`steps_completed`/`steps_total` onto `JobStep` rows (Chapter 5).
8. On `state: "succeeded"` the invoker reads `result_bundle` inline (≤ 4 MiB) and fetches each declared artifact with `GET /v1/executions/{id}/artifacts/{artifact_id}`, streaming to the object store and computing SHA-256 on the way.
9. The invoker verifies: every artifact digest equals the bundle's declared `sha256`; every artifact is within `max_artifact_bytes`; the bundle's `geometry` equals the geometry the *platform* computed from the selected series' DICOM headers (origin, spacing, direction, shape — no pixel data needed) within `1e-4`; every label map's NIfTI affine agrees with `geometry` within `1e-4`; every `source_sop_instance_uids` entry was in the dispatched selection.
10. The invoker calls `DELETE /v1/executions/{id}`.
11. Steps 14–15 of §2.6.1 follow unchanged. The DICOM output is byte-for-byte governed by the same writer as native mode.

**MOS-SVC-052.** Steps 3, 4 and 9 of §2.6.2 are mandatory and MUST NOT be configurable off in `clinical` mode. A sealed service is untrusted code; the platform's only leverage is verification at the boundary.

---

### 2.7 The ownership boundary

This table is normative. Each row is a MUST.

| # | Concern | Owner | Rule |
|---|---|---|---|
| MOS-SVC-053 | Study triage and series inventory | Platform | A service MUST NOT enumerate a study's series to choose its own input. |
| MOS-SVC-054 | `SeriesSelector` evaluation | Platform | A service MUST use exactly the series it was handed, and MUST NOT retrieve a series outside `input.roles`. The Gateway MUST enforce this on the token scope. |
| MOS-SVC-055 | De-identification and UID mapping | Platform | A service MUST treat the UIDs it receives as opaque and MUST echo them verbatim in the `ResultBundle`. A service MUST NOT attempt to reverse a mapping or infer identity. |
| MOS-SVC-056 | Canonical volume geometry | Platform (native) / Service, platform-verified (sealed) | Measurements MUST be computed in source geometry (Chapter 4, MOS-IMG). Label maps MUST be returned on the canonical grid. |
| MOS-SVC-057 | Preprocessing execution | Platform (native) / Service (sealed) | In both modes the applied `PreprocessingSpec` MUST be versioned, digest-pinned and recorded in provenance. There MUST be exactly one implementation per spec, imported by serving and training alike. |
| MOS-SVC-058 | Inference | Platform on Triton (native) / Service (sealed) | Neither mode may bake model weights into a runner image (native) nor mutate them at runtime (both). |
| MOS-SVC-059 | Postprocessing and findings production | Service | The platform MUST NOT reinterpret, re-threshold or re-label a service's findings. |
| MOS-SVC-060 | DICOM object writing (SEG, SR, SC) | Platform | A service MUST NOT construct, mint or STOW a DICOM object. `requested_dicom_kinds` is a request, not a grant. |
| MOS-SVC-061 | DICOM UID derivation and attribute inheritance | Platform | A service MUST NOT supply a `SeriesInstanceUID`, `SOPInstanceUID`, `SeriesNumber` or `StudyInstanceUID` for an output. |
| MOS-SVC-062 | PACS credentials | Platform (Gateway only) | A service MUST NOT hold, receive, request or store a PACS credential. |
| MOS-SVC-063 | Database access | Platform | A service MUST NOT open a connection to PostgreSQL or any platform datastore. |
| MOS-SVC-064 | Event bus access | Platform | A service MUST NOT produce to or consume from the event bus. |
| MOS-SVC-065 | Object store access | Platform | A service MUST NOT read from or write to the object store. Artifacts move by platform pull (§2.5.2). |
| MOS-SVC-066 | Job lifecycle and state | Platform | A service MUST NOT assert a job state. It reports execution `state`, `phase` and step counts; the mapping to `Job` states is the platform's (Chapter 5). |
| MOS-SVC-067 | Policy and authorization | Platform | A service MUST NOT evaluate tenant policy, and MUST NOT be given a mechanism to bypass the Policy Decision Point (Chapter 8). |
| MOS-SVC-068 | Provenance and audit | Platform | A service MUST supply `diagnostics` (§2.8.8); it MUST NOT write audit events. |
| MOS-SVC-069 | Evidence and validation reports | Vendor (produced), platform (verified and served) | A service MUST NOT self-assert acceptance. `evidence.validation_report_ids` are references the platform verifies against signed reports (Chapter 7). |
| MOS-SVC-070 | Clinical claims, intended use, regulatory status | Vendor (`legal_manufacturer`) | MedicalOS MUST NOT add, widen or restate a clinical claim on a service's behalf (Chapter 9). |
| MOS-SVC-071 | Research-only marking | Platform | A service MUST NOT be able to influence whether its output is marked research-only; that follows the `Deployment`'s `clinical_use_mode` (Chapter 9). |
| MOS-SVC-072 | Tenancy | Platform | A service MUST be given data from exactly one tenant per execution and MUST NOT retain state across executions. Cross-execution caching of anything derived from pixel data MUST NOT occur. |
| MOS-SVC-073 | Narrative text | Deferred | A service MUST NOT emit free-text clinical narrative in 0.1.0–0.3.0. `ResultBundle.narrative` MUST be `null`. Narrative generation and its content-safety post-conditions are Chapter 11 (MOS-AGENT). |

**MOS-SVC-074.** The handoff of a schema-valid, geometry-verified `ResultBundle` to the platform is the **service's commit point**. Everything after it — DICOM writing, storage, result rows — is platform-side and forward-only. There is no compensating action a service can take, and none is required: a superseded result is marked `superseded_by` (Chapter 12), never deleted.

**MOS-SVC-075.** A service MUST be able to produce the same `ResultBundle` for the same `ExecutionRequest` on the same hardware. Where it cannot (non-deterministic kernels), it MUST declare `diagnostics.deterministic: false`, and the platform MUST rely on skip-if-present rather than re-inference for retries (Chapter 4, MOS-IMG).

---

### 2.8 The `ResultBundle` return contract

`ResultBundle` is the only thing a service returns. It is JSON, validated against JSON Schema 2020-12 at `https://spec.medicalos.org/schemas/v1/result-bundle/1.0.0.json`.

#### 2.8.1 Top-level shape

| Member | Type | Req. | Notes |
|---|---|---|---|
| `schema_version` | string | yes | SemVer of the bundle schema |
| `service` | object | yes | `{id, version, manifest_digest}` |
| `execution_id`, `job_id` | string | yes | echoed from the request |
| `status` | enum | yes | `completed` \| `rejected` |
| `rejection` | object \| null | yes | non-null iff `status == "rejected"` |
| `capability_outcomes` | array | yes | one entry per requested capability |
| `geometry` | object | yes | the canonical grid all voxel output is on |
| `label_maps` | array | yes | possibly empty |
| `findings` | array | yes | possibly empty |
| `measurements` | array | yes | possibly empty |
| `key_images` | array | yes | possibly empty |
| `narrative` | null | yes | MUST be `null` (MOS-SVC-073) |
| `diagnostics` | object | yes | provenance inputs |

**MOS-SVC-076.** `status` MUST be `rejected` if and only if every entry of `capability_outcomes` has `outcome: "rejected"`. A bundle with at least one `produced` capability MUST be `completed`, and the rejected capabilities MUST still be reported per-capability.

**MOS-SVC-077.** `service.manifest_digest` MUST equal the registered digest of the `ServiceVersion` that was dispatched. Mismatch MUST fail the job with `service_manifest_mismatch`.

#### 2.8.2 Coded concept

```json
{ "scheme": "SCT", "code": "60046008", "display": "Pleural effusion", "scheme_uri": "http://snomed.info/sct" }
```

**MOS-SVC-078.** `scheme` MUST be a DICOM PS3.16 Coding Scheme Designator (`SCT`, `DCM`, `RADLEX`, `LN`, `UCUM`) or a private designator beginning with `99`. A private designator MUST match `^99[A-Z0-9_]{1,14}$` (Chapter 11 §11.3.1) so that it fits the DICOM `CS` value representation. A private designator MUST be accompanied by a `coding_schemes` entry in `diagnostics` giving `name`, `uid` or `responsible_organization`, so the platform can emit a valid `CodingSchemeIdentificationSequence`. MedicalOS's own private designator is **`99MEDICALOS`** — that spelling exactly, and it is fixed here once for the whole specification: in this chapter's worked example (§2.8.7), in the DICOM objects written from it (Chapter 4), and in `capability_concepts.coding_scheme` (Chapter 12, `MOS-STORE-256`/`MOS-STORE-257`). `99MEDOS` is not an alternative spelling of it and MUST NOT be emitted or stored.

**MOS-SVC-079.** `scheme_uri` MUST be present for `SCT` (`http://snomed.info/sct`), `LN` (`http://loinc.org`), `DCM` (`http://dicom.nema.org/resources/ontology/DCM`) and `UCUM` (`http://unitsofmeasure.org`). It exists so a later FHIR `Observation.code` is a projection rather than a rebuild.

**MOS-SVC-080.** Every finding, every measurement, every label-map segment and every laterality statement MUST carry a coded concept. A free-text `type` string like `"emphysema"` MUST be rejected by schema validation, because a conformant TID 1500 SR cannot be written from it.

#### 2.8.3 Geometry

**MOS-SVC-081.** `geometry` MUST describe the canonical volume grid, which is the source grid (Chapter 4, MOS-IMG). `spacing_mm` MUST equal the source `PixelSpacing` and derived slice spacing. `direction_lps` MUST be the source direction cosines. `voxel_order` MUST be `"LPS"`.

**MOS-SVC-082.** Every label map MUST have array shape exactly equal to `geometry.shape` and an affine agreeing with `geometry` within `1e-4` in each component. The platform MUST verify this and MUST fail the job with `geometry_mismatch` otherwise.

**MOS-SVC-083.** A service producing output on a derived grid MUST set `geometry.derived: true` and MUST supply `geometry.derived_from_series_instance_uid`. Chapter 4 governs what the DICOM writer then does; the default path assumes `derived: false`.

#### 2.8.4 Label maps

**MOS-SVC-084.** A label map artifact MUST be gzip-compressed NIfTI-1 (`nifti-gzip`, media type `application/gzip`, `.nii.gz`) or gzip-encoded NRRD (`nrrd-gzip`), matching `outputs.label_map_format`. Voxel dtype MUST be `uint8` for ≤ 255 segments and `uint16` otherwise.

**MOS-SVC-085.** Label values MUST be positive integers; `0` MUST mean background. Each `segments[]` entry MUST give `label_value`, `category` (CID 7150), `type` (CID 7151/7166), `algorithm_type` ∈ `{AUTOMATIC, SEMIAUTOMATIC, MANUAL}`, and MAY give `anatomic_region` and `anatomic_region_modifier`.

**MOS-SVC-086.** Segments MUST be non-overlapping within one label map. A service needing overlapping segments MUST emit one label map per overlapping set.

**MOS-SVC-087.** `sha256` and `size_bytes` MUST be declared for every artifact and MUST be verified by the platform on fetch.

**MOS-SVC-121.** Every `label_maps[]` entry MUST carry `role`: a lowercase snake_case token, non-empty, unique within the bundle, naming what the map is for (for example `chest_structures`). It is a member of the label map and is a different field from `diagnostics.model_versions[].role` (§2.8.8), which names a declared model role in `execution.native.model_versions` and has its own value space; the two MUST NOT be conflated. `role` exists so that the total, stable label-map ordering `(label_maps[].role, label_maps[].artifact_id)` that Chapter 4 `MOS-IMG-066` sorts on is computable from the `ResultBundle` alone. Because that ordering feeds `series_index` and `segment_global_index`, changing a service's `role` values changes every derived `SeriesInstanceUID` it produces and is therefore a breaking change requiring a new `ServiceVersion` and a documented migration (Chapter 4, `MOS-IMG-064`).

#### 2.8.5 Findings and measurements

**MOS-SVC-088.** Every finding MUST carry `source_series_instance_uid` and a non-empty `source_sop_instance_uids`, naming the instances that evidence it. Every listed UID MUST be a member of the dispatched selection. This is what makes the SR's evidence sequence resolvable and what makes "which images did it look at" answerable.

**MOS-SVC-088a.** Every finding MUST carry `finding_sites`, a non-empty array of coded concepts (SCT body-structure codes) naming the anatomic site or sites the finding is located in. This is the source of the SR `finding_site` item that Chapter 4 MOS-IMG-117 requires for every measurement group; a finding without it MUST be rejected as `schema_invalid`. It is a distinct member from `label_maps[].segments[].anatomic_region` (MOS-SVC-085), which describes a segment rather than a finding.

**MOS-SVC-089.** A non-deterministic finding MUST carry `score` (float in `[0,1]`) and `operating_point` `{id, score_threshold}`, and `present` MUST equal `score >= score_threshold`. A deterministic capability MUST omit `score` and `operating_point` and MUST set `present` explicitly.

**MOS-SVC-090.** Absent findings MAY be reported with `present: false`. Where reported, they MUST carry the same coded concept and the same evidence fields as a present finding. A service MUST NOT report a finding it did not evaluate.

**MOS-SVC-091.** Every measurement MUST carry `value` (a JSON number), `unit` as a UCUM code with `scheme: "UCUM"`, a coded `concept`, `computed_in: "source"`, and `source_series_instance_uid`. `computed_in` ∈ {`source`, `derived_declared`} and has exactly one legal value in 0.2 — `source`, meaning the source grid of spine §7 and Chapter 4 MOS-IMG-039, which MOS-SVC-081 fixes as identical to the canonical volume grid. `derived_declared` is reserved for a `ModelVersion` that declares derived geometry and MUST be refused in 0.2; it exists so that a future derived-geometry measurement is a visible schema change, not a silent one.

**MOS-SVC-092.** A measurement that qualifies a finding MUST reference it through `finding_ref`. A measurement that qualifies a label-map segment MUST reference it through `label_map_ref` `{artifact_id, label_value}`.

**MOS-SVC-093.** Measurement `qualifiers[]` entries MUST each be `{concept, value, unit}` with UCUM units, so that an HU threshold, a component-size cutoff or a percentile is carried as data rather than being implicit in the display name.

**MOS-SVC-094.** A service MUST NOT report a measurement whose value it did not compute in this execution. Copying a value from a prior study, a default, or a population mean MUST NOT occur.

#### 2.8.6 Rejections

**MOS-SVC-095.** `rejection.reason_code` and `capability_outcomes[].reason_code` MUST come from this closed enum; the platform maps them to `Job.REJECTED` (Chapter 5) and to the `clinical_rejection` problem class (Chapter 10):

`no_eligible_series`, `geometry_unsupported`, `gantry_tilt_uncorrectable`, `spacing_non_uniform`, `insufficient_coverage`, `slice_thickness_out_of_envelope`, `pixel_spacing_out_of_envelope`, `kernel_unsupported`, `contrast_phase_unsupported`, `orientation_unsupported`, `instance_count_below_minimum`, `burned_in_annotation_present`, `patient_attribute_out_of_envelope`, `capability_not_supported_for_input`.

**MOS-SVC-096.** A rejection MUST carry `observed` and `allowed` values where the reason is an envelope breach, so the reason is legible to a technologist without reading the manifest.

**MOS-SVC-097.** A service MUST NOT signal a clinical rejection by returning an HTTP error, raising an exception, or producing an empty bundle. Transport and system failures are these, and only these: `service_unavailable`, `service_timeout`, `service_manifest_mismatch`, `selftest_failed`, `geometry_mismatch`, `schema_invalid`, `artifact_digest_mismatch`, `artifact_too_large`, `inference_backend_error`, `gateway_error`, `internal_error`, `result_dtype_invalid`, `result_label_out_of_range`, `result_input_drift`, `result_measurement_inconsistent` (Chapter 4 MOS-IMG-141/MOS-IMG-144) and `output_implausible` (Chapter 7 MOS-EVID-110). A chapter adding a check MUST add its code to this list; the list is closed at the document level, not at the chapter level. Every value in this list is a failure **code** — `jobs.failure_code` (Chapter 12 §12.10) — and never a `failure_class`: the `failure_class` enum is closed by Chapter 5 §5.3.2 alone, and Chapter 5 owns failure classification. In particular `output_implausible` is the `failure.code` carried under `failure.class: invalid_result_bundle` (Chapter 7, `MOS-EVID-110`/`MOS-EVID-111`), not a class of its own.

#### 2.8.7 Worked example — complete `ResultBundle`

Sealed service `com.pulmoai.chest-ct` 2.3.0 on a 312-slice non-contrast chest CT with a right-sided effusion.

```json
{
  "schema_version": "1.0.0",
  "service": {
    "id": "com.pulmoai.chest-ct",
    "version": "2.3.0",
    "manifest_digest": "sha256:1d3f5a7c9e0b2d4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f"
  },
  "execution_id": "exe_01J9ZM7B4K2N6P8R0T3V5X7ZAC",
  "job_id": "job_01J9ZM7A9C1E3G5J7L9N1Q3S5U",
  "status": "completed",
  "rejection": null,
  "capability_outcomes": [
    { "capability": "pleural_effusion", "outcome": "produced", "reason_code": null },
    { "capability": "lung_segmentation", "outcome": "produced", "reason_code": null },
    { "capability": "emphysema_laa", "outcome": "produced", "reason_code": null }
  ],
  "geometry": {
    "derived": false,
    "voxel_order": "LPS",
    "reference_series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
    "frame_of_reference_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.2",
    "shape": [312, 512, 512],
    "spacing_mm": [1.0, 0.703125, 0.703125],
    "origin_lps_mm": [-179.6484375, -179.6484375, -412.5],
    "direction_lps": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
    "slice_sop_instance_uids_digest": "sha256:8f0a2c4e6b8d0f2a4c6e8b0d2f4a6c8e0b2d4f6a8c0e2f4b6d8a0c2e4f6a8c0e"
  },
  "label_maps": [
    {
      "artifact_id": "lm_chest_structures",
      "role": "chest_structures",
      "media_type": "application/gzip",
      "format": "nifti-gzip",
      "dtype": "uint8",
      "sha256": "sha256:3c5e7092b4d6f8a0c2e4b61d8f0a2c4e6b8d0f2a4c6e8b0d2f4a6c8e0b2d4f6a",
      "size_bytes": 4812993,
      "segments": [
        {
          "label_value": 1,
          "name": "Left lung",
          "category": { "scheme": "SCT", "code": "123037004", "display": "Body structure", "scheme_uri": "http://snomed.info/sct" },
          "type": { "scheme": "SCT", "code": "39607008", "display": "Lung structure", "scheme_uri": "http://snomed.info/sct" },
          "anatomic_region": { "scheme": "SCT", "code": "39607008", "display": "Lung structure", "scheme_uri": "http://snomed.info/sct" },
          "anatomic_region_modifier": { "scheme": "SCT", "code": "7771000", "display": "Left", "scheme_uri": "http://snomed.info/sct" },
          "algorithm_type": "AUTOMATIC"
        },
        {
          "label_value": 2,
          "name": "Right lung",
          "category": { "scheme": "SCT", "code": "123037004", "display": "Body structure", "scheme_uri": "http://snomed.info/sct" },
          "type": { "scheme": "SCT", "code": "39607008", "display": "Lung structure", "scheme_uri": "http://snomed.info/sct" },
          "anatomic_region": { "scheme": "SCT", "code": "39607008", "display": "Lung structure", "scheme_uri": "http://snomed.info/sct" },
          "anatomic_region_modifier": { "scheme": "SCT", "code": "24028007", "display": "Right", "scheme_uri": "http://snomed.info/sct" },
          "algorithm_type": "AUTOMATIC"
        },
        {
          "label_value": 3,
          "name": "Right pleural effusion",
          "category": { "scheme": "SCT", "code": "49755003", "display": "Morphologically abnormal structure", "scheme_uri": "http://snomed.info/sct" },
          "type": { "scheme": "SCT", "code": "60046008", "display": "Pleural effusion", "scheme_uri": "http://snomed.info/sct" },
          "anatomic_region": { "scheme": "SCT", "code": "39607008", "display": "Lung structure", "scheme_uri": "http://snomed.info/sct" },
          "anatomic_region_modifier": { "scheme": "SCT", "code": "24028007", "display": "Right", "scheme_uri": "http://snomed.info/sct" },
          "algorithm_type": "AUTOMATIC"
        }
      ]
    }
  ],
  "findings": [
    {
      "finding_id": "f_effusion_right",
      "capability": "pleural_effusion",
      "concept": { "scheme": "SCT", "code": "60046008", "display": "Pleural effusion", "scheme_uri": "http://snomed.info/sct" },
      "laterality": { "scheme": "SCT", "code": "24028007", "display": "Right", "scheme_uri": "http://snomed.info/sct" },
      "finding_sites": [ { "scheme": "SCT", "code": "181231000", "display": "Pleural cavity structure", "scheme_uri": "http://snomed.info/sct" } ],
      "present": true,
      "score": 0.87,
      "operating_point": { "id": "balanced", "score_threshold": 0.42 },
      "label_map_ref": { "artifact_id": "lm_chest_structures", "label_value": 3 },
      "measurement_refs": ["m_effusion_right_volume"],
      "source_series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
      "source_sop_instance_uids": [
        "1.2.840.113619.2.55.3.604688119.868.1731000000.4.201",
        "1.2.840.113619.2.55.3.604688119.868.1731000000.4.226",
        "1.2.840.113619.2.55.3.604688119.868.1731000000.4.251"
      ],
      "bounding_box_ijk": { "min": [198, 289, 118], "max": [277, 421, 306] }
    },
    {
      "finding_id": "f_effusion_left",
      "capability": "pleural_effusion",
      "concept": { "scheme": "SCT", "code": "60046008", "display": "Pleural effusion", "scheme_uri": "http://snomed.info/sct" },
      "laterality": { "scheme": "SCT", "code": "7771000", "display": "Left", "scheme_uri": "http://snomed.info/sct" },
      "finding_sites": [ { "scheme": "SCT", "code": "181231000", "display": "Pleural cavity structure", "scheme_uri": "http://snomed.info/sct" } ],
      "present": false,
      "score": 0.09,
      "operating_point": { "id": "balanced", "score_threshold": 0.42 },
      "label_map_ref": null,
      "measurement_refs": [],
      "source_series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
      "source_sop_instance_uids": [
        "1.2.840.113619.2.55.3.604688119.868.1731000000.4.201"
      ],
      "bounding_box_ijk": null
    }
  ],
  "measurements": [
    {
      "measurement_id": "m_effusion_right_volume",
      "capability": "pleural_effusion",
      "concept": { "scheme": "SCT", "code": "118565006", "display": "Volume", "scheme_uri": "http://snomed.info/sct" },
      "value": 412.6,
      "unit": { "scheme": "UCUM", "code": "ml", "display": "milliliter", "scheme_uri": "http://unitsofmeasure.org" },
      "computed_in": "source",
      "finding_ref": "f_effusion_right",
      "label_map_ref": { "artifact_id": "lm_chest_structures", "label_value": 3 },
      "source_series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
      "qualifiers": []
    },
    {
      "measurement_id": "m_lung_volume_left",
      "capability": "lung_segmentation",
      "concept": { "scheme": "SCT", "code": "118565006", "display": "Volume", "scheme_uri": "http://snomed.info/sct" },
      "value": 2418.3,
      "unit": { "scheme": "UCUM", "code": "ml", "display": "milliliter", "scheme_uri": "http://unitsofmeasure.org" },
      "computed_in": "source",
      "finding_ref": null,
      "label_map_ref": { "artifact_id": "lm_chest_structures", "label_value": 1 },
      "source_series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
      "qualifiers": []
    },
    {
      "measurement_id": "m_lung_volume_right",
      "capability": "lung_segmentation",
      "concept": { "scheme": "SCT", "code": "118565006", "display": "Volume", "scheme_uri": "http://snomed.info/sct" },
      "value": 1793.1,
      "unit": { "scheme": "UCUM", "code": "ml", "display": "milliliter", "scheme_uri": "http://unitsofmeasure.org" },
      "computed_in": "source",
      "finding_ref": null,
      "label_map_ref": { "artifact_id": "lm_chest_structures", "label_value": 2 },
      "source_series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
      "qualifiers": []
    },
    {
      "measurement_id": "m_laa_950_both_lungs",
      "capability": "emphysema_laa",
      "concept": { "scheme": "99MEDICALOS", "code": "LAA-950", "display": "Low attenuation area below -950 HU, fraction of lung volume" },
      "value": 7.4,
      "unit": { "scheme": "UCUM", "code": "%", "display": "percent", "scheme_uri": "http://unitsofmeasure.org" },
      "computed_in": "source",
      "finding_ref": null,
      "label_map_ref": { "artifact_id": "lm_chest_structures", "label_value": 1 },
      "source_series_instance_uid": "1.2.840.113619.2.55.3.604688119.868.1731000000.4",
      "qualifiers": [
        {
          "concept": { "scheme": "99MEDICALOS", "code": "LAA-THRESHOLD", "display": "Attenuation threshold" },
          "value": -950,
          "unit": { "scheme": "UCUM", "code": "[hnsf'U]", "display": "Hounsfield unit", "scheme_uri": "http://unitsofmeasure.org" }
        },
        {
          "concept": { "scheme": "99MEDICALOS", "code": "LAA-MASK", "display": "Mask used for denominator" },
          "value": 3,
          "unit": { "scheme": "UCUM", "code": "1", "display": "unity", "scheme_uri": "http://unitsofmeasure.org" }
        }
      ]
    }
  ],
  "key_images": [],
  "narrative": null,
  "diagnostics": {
    "deterministic": true,
    "abi_version": "1.0",
    "image_digest": "sha256:9f2c1d4b6a8e0f3572c9b1d0e4f6a8c2b5d7e9f1a3c5e7092b4d6f8a0c2e4b61",
    "model_versions": [
      {
        "role": "effusion_seg",
        "model_id": "pulmoai.effusion",
        "version": "4.1.0",
        "artifact_digest": "sha256:2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b"
      },
      {
        "role": "lung_seg",
        "model_id": "pulmoai.lungseg",
        "version": "3.0.2",
        "artifact_digest": "sha256:6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a"
      }
    ],
    "preprocessing_specs": [
      { "id": "pulmoai.effusion.prep", "version": "2.0.1", "digest": "sha256:a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2" }
    ],
    "input_pixel_digest": "sha256:c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c2e4f6a8c0e2",
    "accelerator": { "gpu_model": "NVIDIA L40S", "driver": "560.35.03", "cuda": "12.6", "trt": "10.3.0" },
    "applicability_checks": [
      { "id": "slice_thickness_mm", "observed": 1.0, "allowed": "0.5..3.0", "pass": true },
      { "id": "pixel_spacing_mm", "observed": 0.703125, "allowed": "0.40..1.00", "pass": true },
      { "id": "min_instances", "observed": 312, "allowed": ">=80", "pass": true },
      { "id": "contrast_phase", "observed": "none", "allowed": "none", "pass": true }
    ],
    "coding_schemes": [
      {
        "designator": "99MEDICALOS",
        "name": "MedicalOS local measurement concepts",
        "uid": "1.2.826.0.1.3680043.10.1338.1",
        "responsible_organization": "PulmoAI GmbH"
      }
    ],
    "timings_ms": { "retrieve": 18240, "preprocess": 6110, "inference": 141980, "postprocess": 9860, "total": 176190 },
    "peak_gpu_memory_mb": 11284
  }
}
```

#### 2.8.8 `diagnostics`

**MOS-SVC-098.** `diagnostics` MUST carry every field shown in §2.8.7. The platform MUST copy it verbatim into the provenance record (Chapter 9, Chapter 12). A bundle missing `input_pixel_digest`, `model_versions`, `preprocessing_specs` or `accelerator` MUST be rejected as `schema_invalid`.

**MOS-SVC-099.** `input_pixel_digest` MUST be SHA-256 over the canonical volume's `array.tobytes(order="C")` after rescale, as `float32`. In sealed mode the platform MAY recompute it for spot checks; a mismatch MUST be logged as a `provenance_divergence` audit event, and in `clinical` mode MUST fail the job.

**MOS-SVC-100.** `diagnostics` MUST NOT contain PHI. Patient identifiers, dates, institution names and accession numbers MUST NOT appear in it.

---

### 2.9 Lifecycle: registration, verification, deployment, rollback

#### 2.9.1 `ServiceVersion` lifecycle status

`ServiceVersion.lifecycle_status` describes the *artifact*. Liveness — which version currently serves traffic, at what canary weight, in which environment — belongs to `Deployment` (Chapter 6 §6.8, `MOS-REG-072`). These are two different things and MUST NOT be merged. The field is named `lifecycle_status` everywhere in this specification, never `lifecycle_state`.

**MOS-SVC-101.** `ServiceVersion.lifecycle_status`, its permitted values and its transition graph are defined once, by `MOS-REG-020` and `MOS-REG-021` (Chapter 6). This chapter does not restate them, and a status value or a transition that Chapter 6 does not define MUST NOT be implemented. The site-local integrity, compatibility and functional smoke checks of §2.9.2 are **not** an artifact status transition: they are the `Deployment` transition `PENDING → VERIFYING → SERVING` (`MOS-REG-075`). The table below states only what a status means for *this* chapter's contract — whether a `Deployment` may target the version — and is the presentation of MOS-SVC-110.

| `lifecycle_status` (`MOS-REG-020`) | May a `Deployment` target it (MOS-SVC-110) |
|---|---|
| `DRAFT` | no |
| `REGISTERED` | no |
| `VALIDATING` | no |
| `VALIDATED` | yes, `clinical_use_mode: research_only` only (MOS-SVC-111) |
| `APPROVED` | yes, `research_only` and `clinical` |
| `DEPRECATED` | yes, ranked last and resolvable for at least 180 days (`MOS-REG-106`) |
| `SUSPENDED` | no |
| `RECALLED` | no |

**MOS-SVC-102.** Every transition MUST emit an `AuditEvent` carrying the actor, the `manifest_digest`, the previous and new `lifecycle_status`, and a free-text `status_reason` that is mandatory for `SUSPENDED` and `RECALLED`.

**MOS-SVC-103.** `RECALLED` MUST be preceded by an impact query returning every `Result` produced by that `ServiceVersion`, and the platform MUST mark those results `recalled_service_version` without deleting them (Chapter 12).

#### 2.9.2 Site verification gate (`Deployment`: `PENDING → VERIFYING → SERVING`)

The three checks below run at the *site*, take minutes to hours, and MUST NOT be named or described as clinical validation. MedicalOS performs no clinical validation and cannot confer any (Chapter 7, Chapter 9). They are **not** a transition of the artifact's `lifecycle_status`: their subjects are site-, cluster- and node-specific (cluster capacity, GPU compute capability, Triton backend availability), so they are the content of the `VERIFYING` state of a `Deployment` (Chapter 6, `MOS-REG-072`–`MOS-REG-075`). Their outcome is recorded in `deployment.verification_ref`, and they MUST be re-run for each deployment slot, including a second slot for an already-verified version.

| Check | What it does | Fails the gate when |
|---|---|---|
| Integrity check | re-verifies MOS-SVC-031 against the local mirror | any digest or signature mismatch |
| Compatibility check | ABI version, `resources` vs cluster capacity, GPU compute capability, `timeouts` nesting (MOS-SVC-024), Triton backend availability (native), `SeriesSelector` schema version | any unsatisfiable requirement |
| Functional smoke test | boots the service, `GET /v1/manifest`, `POST /v1/selftest`, then one execution against the site's fixture corpus; asserts schema validity, geometry agreement, digest agreement | any assertion fails |

**MOS-SVC-104.** The functional smoke test MUST run against the site's own fixture corpus (Chapter 14, MOS-TEST), not against fixtures shipped by the vendor alone, so that a vendor cannot pass the gate by shipping an easy fixture.

**MOS-SVC-105.** `VALIDATING → VALIDATED` (`MOS-REG-021`) MUST require, for every capability in `capabilities[]`, a `ValidationReport` that is signature-valid offline, references a content-addressed `DatasetVersion`, pins the same `manifest_digest`, and declares the `operating_point` used (Chapter 7, MOS-EVID).

**MOS-SVC-106.** `VALIDATED → APPROVED` (`MOS-REG-021`) MUST require an explicit action by a named human holding `artifact.approve` for the tenant, and MUST record the approver identity, timestamp and the `ValidationReport` ids reviewed. It MUST NOT be automatable by an API key or service account. The permission string is Chapter 8's to catalogue and Chapter 6's to name (`MOS-REG-108`); this chapter only fixes that the actor is a `User`.

#### 2.9.3 Registration

```
POST /api/v1/service-versions
Content-Type: application/json
Idempotency-Key: 6f6d2e1a-3b4c-4d5e-8f90-1a2b3c4d5e6f

{
  "artifact_reference": "oci://registry.hospital.example/medicalos/com.pulmoai.chest-ct@sha256:bd0f2a4c6e8b0d2f4a6c8e0b2d4f6a8c0e2f4b6d8a0c2e4f6a8c0e2f4b6d8a0c",
  "trust_policy_id": "tp_vendor_pulmoai"
}
```

```
202 Accepted
Location: /api/v1/service-versions/svv_01J9ZP2H5M8Q1T4W7Z0C3F6J9L
{ "service_version_id": "svv_01J9ZP2H5M8Q1T4W7Z0C3F6J9L", "lifecycle_status": "DRAFT" }
```

**MOS-SVC-107.** Registration MUST accept an OCI reference only, MUST NOT accept an inline manifest, and MUST NOT accept a file upload of `service.yaml` alone. The manifest is not trustworthy separated from the artifact that carries its signature.

**MOS-SVC-108.** Registration MUST be idempotent on `(artifact_reference)`. Re-posting the same digest MUST return the existing `service_version_id`.

**MOS-SVC-109.** Registration MUST create the service's work inbox `medicalos.svc.<service_id>.work` (Chapter 5, MOS-EXEC-003) and MUST NOT create any modality- or nosology-named topic.

#### 2.9.4 Deployment and rollback

`Deployment` binds a `ServiceVersion` to a tenant, an environment and a `clinical_use_mode`. Its full schema and the capability-resolution precedence are Chapter 6 (MOS-REG); this chapter fixes only the obligations that fall on the service contract.

**MOS-SVC-110.** A `Deployment` MUST target a `ServiceVersion` whose `lifecycle_status` is `APPROVED`, `VALIDATED` or `DEPRECATED`. Targeting `DRAFT`, `REGISTERED`, `VALIDATING`, `SUSPENDED` or `RECALLED` MUST be refused. This is the same predicate as resolution filter F3 (Chapter 6, `MOS-REG-055`) and MUST NOT diverge from it.

**MOS-SVC-111.** A `Deployment` with `clinical_use_mode: clinical` MUST target an `APPROVED` `ServiceVersion` whose `legal_manufacturer` is complete and whose `intended-use.yaml` declares a `regulatory_status` for the deployment's jurisdiction (Chapter 9).

**MOS-SVC-112.** A `Deployment` MUST select exactly one `operating_point.id` per non-deterministic capability, and it MUST be one the manifest declares. Changing it MUST create a new `Deployment` revision with a new `effective_at`, MUST emit an `AuditEvent`, and MUST NOT alter results already produced.

**MOS-SVC-113.** Rollback MUST be expressed as a new `Deployment` revision pointing at a previously `APPROVED` `ServiceVersion`. Rollback MUST NOT mutate, delete or re-derive any `Result`, and MUST NOT delete any DICOM object.

**MOS-SVC-114.** A `Deployment` revision MUST apply only to `Job`s created at or after its `effective_at`. Jobs already created keep the `ServiceVersion` pinned in their row and MUST use it on every retry (spine §9; Chapter 5, Chapter 6).

**MOS-SVC-115.** Rolling a `ServiceVersion` back MUST NOT be sufficient to stop it being dispatched to elsewhere. Stopping dispatch platform-wide is `SUSPENDED` or `RECALLED` (§2.9.1), and an incident response MUST use those, not a deployment edit.

**MOS-SVC-116.** Deleting a `Service` or a `ServiceVersion` record MUST be refused while any `Result` references it. The supported operations are `DEPRECATED`, `SUSPENDED` and `RECALLED`.

#### 2.9.5 Reserved and not implemented in 0.2

**MOS-SVC-117.** `POST /v1/executions/{id}/cancel` MUST return `501 Not Implemented` in 0.1.0–0.2.0 and MUST NOT be relied upon. Cancellation of a running inference is not implemented before 0.3.0; the `Job` state `CANCELLED` and the `job.cancel` permission are reserved (spine §4; Chapter 5).

**MOS-SVC-118.** `native` mode MUST be implemented in 0.1.0. `sealed` mode MUST NOT be claimed as available before 0.3.0 (spine §14). The manifest schema carries both from 0.1.0 so that no manifest migration is needed when sealed mode ships.

**MOS-SVC-119.** `ResultBundle.key_images` is defined and MUST validate, but no DICOM SC writing is required before 0.4.0 (Chapter 4). A service MAY emit key images earlier; the platform MUST store them and MUST NOT silently discard them.

**MOS-SVC-120.** FHIR projection of `findings` and `measurements` is out of scope for 0.1.0–0.4.0. `scheme_uri` exists so it remains a projection later; no FHIR client is required, and none MUST be built.

---

### Acceptance criteria

Each check is executable by CI or by a reviewer against a running installation.

1. **Manifest schema is generated, not hand-written.** `make generate` produces `medos/schemas/service-manifest/1.0.0.json`, the Go structs and the Python models from one source; CI fails if any generated file differs from the committed copy. CI additionally greps this specification and `medos/schemas/` for schema `$id` and `$ref` URIs and fails on any authority other than `https://spec.medicalos.org/schemas/v1/`, exactly as `MOS-API-036` greps `/problems/` URIs. (MOS-SVC-013; Chapter 10, `MOS-API-036`, `MOS-API-084`)
2. **Unknown-field rejection.** Registering a manifest with an extra top-level key `foo: bar` returns `422` with problem type `manifest-schema-invalid`. (MOS-SVC-015)
3. **Tag reference rejection.** Registering a manifest whose `execution.sealed.image` is `ghcr.io/pulmoai/chest-ct:2.3.0` returns `422`; the same manifest with an `@sha256:` reference registers. (MOS-SVC-029)
4. **Signature fail-closed.** Registering an artifact signed by an identity absent from `trust_policy_id` returns `422` and creates no `ServiceVersion` row. Corrupting one byte of any layer after signing produces the same result. (MOS-SVC-031)
5. **Offline verification.** `medicalosctl service verify --artifact <oci-layout-dir> --trust-policy tp_vendor_pulmoai` succeeds with no network access and prints the same `manifest_digest` the platform stored. (MOS-SVC-032)
6. **Digest determinism.** Computing `manifest_digest` twice over a manifest whose YAML key order is shuffled yields the same value (JCS canonicalisation). (MOS-SVC-027)
7. **Timeout nesting.** A manifest with `accept_ms: 5000`, `analyze_ms: 4000` fails deployment validation with `timeout-nesting-violation`. (MOS-SVC-024)
8. **Native import scan.** A wheel whose package imports `requests` at module scope is rejected at registration with the offending module named. (MOS-SVC-038)
9. **Native egress is impossible.** Inside `analyze`, `socket.create_connection(("1.1.1.1", 443), timeout=2)` raises `OSError` and the job still completes. (MOS-SVC-039)
10. **Sealed egress is impossible.** From the service pod, `curl https://example.com` fails and `curl $GATEWAY/studies/.../metadata` succeeds. (MOS-SVC-025, MOS-SVC-050)
11. **Sealed STOW is refused.** A `POST` to `{gateway}/studies` with the execution's service token returns `403` and writes an `AuditEvent` of type `service_stow_denied`. (MOS-SVC-006, MOS-SVC-047)
12. **Out-of-selection retrieval is refused.** A `GET` for a series in the same study that was not in `input.roles` returns `403`. (MOS-SVC-054)
13. **Manifest-digest mismatch halts dispatch.** Serve a `GET /v1/manifest` whose digest differs from the registered one: the job ends `FAILED` with `service_manifest_mismatch` and the `ServiceVersion` is `SUSPENDED`. (MOS-SVC-077, MOS-SVC-101)
14. **Self-test gate.** Perturb the pinned `PreprocessingSpec` so the golden fixture hash changes: the pod never reaches readiness and no job is claimed. (Chapter 4, MOS-IMG-054/MOS-IMG-055)
15. **Geometry verification.** Return a label map of shape `[311, 512, 512]` against `geometry.shape` `[312, 512, 512]`: the job ends `FAILED` with `geometry_mismatch`, and no DICOM object is written. (MOS-SVC-082)
16. **Affine verification.** Return a NIfTI whose affine differs from `geometry` by `2e-3` mm in origin: the job ends `FAILED` with `geometry_mismatch`. (MOS-SVC-082)
17. **Artifact digest verification.** Return an artifact whose bytes do not match the declared `sha256`: the job ends `FAILED` with `artifact_digest_mismatch`. (MOS-SVC-087)
18. **Evidence-UID membership.** Return a finding whose `source_sop_instance_uids` contains a UID not in the dispatched selection: the bundle is rejected as `schema_invalid` before any DICOM write. (MOS-SVC-088)
19. **Uncoded finding rejection.** Return `{"type": "emphysema", "probability": 0.94, "volume_ml": 1243}` as a finding: schema validation fails, naming the missing `concept`, `finding_sites` and `unit`. (MOS-SVC-080, MOS-SVC-088a, MOS-SVC-091)
20. **Threshold consistency.** Return `score: 0.30`, `operating_point.score_threshold: 0.42`, `present: true`: the bundle is rejected as `schema_invalid`. (MOS-SVC-089)
21. **Rejection is not failure.** Dispatch a 5 mm-slice study to `com.pulmoai.chest-ct`: the job ends `REJECTED` with `slice_thickness_out_of_envelope`, `observed: 5.0`, `allowed: "0.5..3.0"`, and the API problem `class` is `clinical_rejection`, not a transport error. (MOS-SVC-023, MOS-SVC-095, MOS-SVC-096)
22. **Partial capability rejection.** A study satisfying `pleural_effusion` but not `emphysema_laa` yields `status: "completed"` with one `produced` and one `rejected` entry in `capability_outcomes`. (MOS-SVC-076)
23. **No float progress.** `GET /api/v1/jobs/{id}` returns `phase`, `steps_completed` and `steps_total` and contains no `progress` key anywhere in the response. (MOS-SVC-044)
24. **Execution idempotency.** `POST /v1/executions` twice with the same `execution_id` and body yields the same `execution_id` and exactly one execution; with a different body yields `409`. (MOS-SVC-045)
25. **Immutability.** Re-registering `com.pulmoai.chest-ct` `2.3.0` from a different artifact digest returns `409 service-version-immutable`. (MOS-SVC-010)
26. **Native and sealed produce identical DICOM.** Implement the same trivial capability in both modes over the same study; the generated SEG and SR differ only in the equipment tags sourced from `legal_manufacturer`, and `dciodvfy` returns zero errors for both. (MOS-SVC-034, MOS-SVC-060)
27. **Clinical mode gate.** Creating a `Deployment` with `clinical_use_mode: clinical` against a `ServiceVersion` whose `lifecycle_status` is `VALIDATED` (not `APPROVED`) is refused; the same against `APPROVED` succeeds. (MOS-SVC-110, MOS-SVC-111)
28. **Approval is human.** Attempting `VALIDATED → APPROVED` with an API key returns `403`; the audit row for a successful approval names a `User`, not a `ServiceAccount`. (MOS-SVC-106)
29. **Illegal transition.** `DRAFT → APPROVED` returns `409` with the legal successors listed. (MOS-SVC-101; Chapter 6, `MOS-REG-021`)
30. **Rollback preserves results.** Roll a tenant from `2.3.0` to `2.2.0`; every `Result` produced by `2.3.0` remains readable, its DICOM objects remain in the PACS, and jobs created before `effective_at` still resolve to `2.3.0` on retry. (MOS-SVC-113, MOS-SVC-114)
31. **Recall is traceable.** Recalling `2.3.0` returns a non-empty impact list of `Result` ids, marks them `recalled_service_version`, deletes nothing, and refuses further dispatch. (MOS-SVC-103)
32. **Deletion is refused.** `DELETE /api/v1/service-versions/{id}` for a version with at least one `Result` returns `409`. (MOS-SVC-116)
33. **Provenance completeness.** For any completed job, the provenance record contains `input_pixel_digest`, every `model_versions[].artifact_digest`, every `preprocessing_specs[].digest`, `image_digest` (or `wheel_digest`), `accelerator`, the selected series and SOP Instance UIDs, and the generated `SeriesInstanceUID`s — with no field null. (MOS-SVC-098)
34. **No PHI in diagnostics.** A scanner over `diagnostics` for the tenant's patient identifiers, patient names, accession numbers and study dates finds zero matches across 100 completed jobs. (MOS-SVC-100)
35. **Narrative is null.** Across every completed job in 0.2, `ResultBundle.narrative` is `null`; a bundle with a non-null narrative is rejected as `schema_invalid`. (MOS-SVC-073)
36. **Cancel is honestly unimplemented.** `POST /v1/executions/{id}/cancel` returns `501`, and `job.cancel` is absent from every role's effective permission set. (MOS-SVC-117)
37. **Private coding scheme is declared.** A bundle using `99MEDICALOS` without a matching `diagnostics.coding_schemes` entry is rejected as `schema_invalid`. (MOS-SVC-078)
38. **Second capability, zero core changes.** Adding a second service for `lung_nodule` requires changes only under the service's own package and its manifest; `git diff --stat` over the control-plane, execution-plane and data-plane packages is empty. (MOS-SVC-002, MOS-SVC-007)
39. **Label-map ordering is computable from the bundle.** A bundle whose `label_maps[]` omits `role`, repeats a `role`, or carries a `role` that is not lowercase snake_case is rejected as `schema_invalid`; and a bundle with two label maps whose `artifact_id`s sort in the opposite order to their `role`s yields the same `series_index` and `segment_global_index` assignment on every run and on every replay of the stored bundle. (MOS-SVC-121; Chapter 4, `MOS-IMG-066`)

---

[← 1. Overview, Scope and Conventions](01-overview.md) · [Index](../../MEDICALOS_SPEC.md) · [3. Medical Data Plane: Gateway, De-identification and Triage →](03-medical-data-plane.md)
