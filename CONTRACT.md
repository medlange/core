# MedicalOS — Weeks 1–2 build contract (BINDING on all implementers)

This file is the single source of truth for module layout, names and interfaces during the
weeks 1–2 vertical slice. It exists because parallel authoring without a locked contract
produced 147 cross-chapter conflicts in the specification. Do not repeat that in code.

**If you need something this file does not define, define it ONLY inside your own module and
report it. Never redefine anything below.**

Spec reference: chapter 15 §15.2.3 (weeks 1–2), chapter 5 (jobs), chapter 4 (geometry/DICOM
output), chapter 3 (gateway/selection). Cite requirement IDs in docstrings.

---

## 0. What weeks 1–2 is, and is not

Per `docs/spec/15-delivery.md` §15.2.3, this slice is deliberately architecturally wrong:

**NOT in this slice:** auth, tenancy, RLS, registries, capability resolution, the broker,
Triton, sealed services, the evidence plane, agents, policy engine.

**IN this slice:** DICOMweb pull → series selection → canonical volume → in-process inference
→ inverse transform → `ResultBundle` → SEG + SR → STOW, plus `POST /api/v1/jobs`,
`GET /api/v1/jobs/{id}`, a `jobs` table, a `SELECT ... FOR UPDATE SKIP LOCKED` claim loop,
an OHIF toolbar button and a provenance panel.

Language: **Python** for this slice. The Go control plane replaces the HTTP surface at weeks
3–5 (`MOS-REL-084`). Therefore the HTTP layer MUST stay thin — no business logic in handlers.

---

## 1. Repository layout (authoritative)

**AMENDED.** This section described a two-product repository with `medos/` as a Python
package and `api/`, `schemas/`, `services/`, `tools/`, `web/`, `deploy/` and `contracts/`
beside it at the root. That tree no longer exists, and a section calling itself
authoritative about a tree that does not exist is worse than no section: it is the first
place somebody looks.

### Three products

```
viewer/                  a standalone DICOMweb viewer. No build step, no bundler, no
                         runtime dependency. Its tests read `viewer/` and nothing else
                         and run with no repository around them.
trainer/                 a standalone model fitter. EXACTLY ONE of its modules imports
                         the platform -- `__main__.py`, whose `execute` branch is the
                         supervisor calling into `medos/medos/training/supervisor.py`;
                         everything else in it imports only `medos.sdk`, the SDK.
medos/                   the platform AND the SDK. A PRODUCT DIRECTORY, not a package --
                         the package is `medos/medos/` inside it, and the SDK is that
                         package's `sdk/` subtree (`medos.sdk`): the spec format, the
                         bundle layout, the phantom, the digest rule and the
                         run-directory exchange. `MOS-IMG-003` requires exactly one
                         implementation of those contracts; both images install it, and
                         it imports neither of them back.
```

**This heading said FOUR.** The fourth was `medicalos_preprocessing/`, the contracts'
top-level home; the contracts moved INSIDE the distribution as `medos/medos/sdk/` when
the platform became the SDK, and the count went with the tree.

### Two that belong to no product

**This heading said THREE.** The third was `spikes/week0/  FROZEN. See section 2.`, and the
spike was deleted; the entry went with it and the count did not. A layout section that
calls itself authoritative and counts a directory that is not there is the failure the
amendment note above this describes, one size smaller.

```
docs/                    the specification. It binds every product -- `MOS-UI-009a` is the
                         viewer's, `MOS-TRAIN-034` the trainer's, `MOS-IMG-003` the shared
                         package's, and the rest is the platform's -- so it cannot live
                         inside one of them.
tests/                   the platform's suite AND the tests whose subject is the
                         RELATIONSHIP between products: `test_viewer_deployment.py`,
                         `test_trainer_import_boundary.py`,
                         `test_shared_package_is_pure.py`.
```

### Inside `medos/`

```
medos/medos/             the platform package -- what ships in the wheel
  core/                  pure, no I/O to DB or HTTP: geometry, dicomio, uids, masks,
                         measure, concepts, bundle (the return contract), errors
  api/                   FastAPI, thin. MOS-API-008 validation and RFC 9457 problems
  db/                    psycopg 3, written-out SQL, RLS tenancy, the job queue
  evidence/              manifests, leakage, validation reports, DSSE signatures
  training/              cohorts, policy, splits, run records, the seal, the supervisor
  safety/ inference/ gateway/ sealed/ capabilities/ worker/ resolution/
  security/ registry/ bus/ writer/ dicomweb/ promotion/ cli/ objectstore/ config/
deploy/                  the compose stack, the self-test models, the sealed reference
schemas/                 the JSON schemas the API validates against
web/                     the withdrawn OHIF extension (the training console it also held
                         was withdrawn at specification 0.4.0, register entry 150)
tools/                   scripts that are NOT installed with the platform
                         (`MOS-IMG-069`: it must never mint a StudyInstanceUID)
services/                the Service Plane and its one shipped capability
examples/                the lung-nodule capability's envelope and coded concepts
api/                     `v1/routes.core.yaml`, `v1/routes.train.yaml` -- route
                         contracts, not Python. Note the collision with `medos/medos/api/`
                         above: one is a directory of YAML, the other is the HTTP surface.
contracts/               `permissions.yaml` and its generated companion
```

**THE PACKAGE IS NESTED AND THAT WAS NOT COSMETIC.** `tools/` sits beside the package
rather than inside it because section 11 of this contract and `pyproject.toml` both say it
is not installed with the platform, and folding it in would have reversed that decision
silently. `pyproject.toml` carries two roots for the same reason:
`packages.find where = [".", "medos"]`, with `include` keeping `tools` and `services` out
of the wheel.

---

## 2. Migration rule for the spike code — DISCHARGED, and the spike is deleted

`spikes/week0/` held ~5,200 lines of working, tested week-0 code. This section required
its logic to be lifted into `medos/medos/core/` by **moving and deduplicating**, not
rewriting behaviour, and named two duplications to fix on the way: `derive_uid` and
`SourceGeometry` were each defined in BOTH `write_dicom_results.py` and
`verify_roundtrip.py`. After the lift there was to be exactly one definition of each, in
`medos/medos/core/uids.py` and `medos/medos/core/geometry.py`, with the spike importing
them rather than redefining them, and a test asserting byte-identical `derive_uid` output
against the recorded values.

**All of that happened, and then the directory was deleted.** What the rule protected now
lives where it is used:

| what it was | where it is |
|---|---|
| `spikes/week0/out/*.json` — the recordings the byte-identity gates compare against | `tests/_recorded/` (gitignored: generated from patient studies) |
| `test_contracts.py::run_geometry_tests` — 19 checks, eight rejection codes exercised nowhere else | [`tests/unit/test_geometry_contract.py`](tests/unit/test_geometry_contract.py) |
| `normalise_pn` / `validate_equipment_identity` checks | [`tests/unit/test_writer_identity_contract.py`](tests/unit/test_writer_identity_contract.py) |
| `guard_deid_uid_space` — never lifted at all | `medos/medos/writer/identity.py`, with [`tests/unit/test_deid_uid_space_guard.py`](tests/unit/test_deid_uid_space_guard.py) |
| `capability_concepts.json` — the ONE coded-concept dictionary, which the platform's runtime read out of the spike | `medos/medos/capabilities/capability_concepts.json`, declared as package data |

`tests/unit/test_core_lift.py` still holds the gates this section asked for. It compares
against `tests/_recorded/` rather than against a second copy of the code, which is what
the deduplication was for in the first place.

**Not carried across:** `run_corpus_test`'s four checks against the real LCTSC tree, and
`verify_roundtrip.py`'s fixture generator. Register entry 118 says what that costs.

---

## 3. Job state machine — single definition (chapter 5 `MOS-EXEC-001`)

```python
JobState = Literal["CREATED","QUEUED","RUNNING","COMPLETED","FAILED","CANCELLED","REJECTED"]
```

- `REJECTED` is a **clinical** outcome, not an error: no eligible series, outside the
  applicability envelope, unsupported geometry. It MUST carry a machine-readable reason.
- `CANCELLED` is RESERVED in this slice — the column and enum value exist, nothing produces it.
- No `POSTPROCESSING`, no `WAITING`, no float `progress`. Progress is `phase: str` plus
  `steps_completed` / `steps_total` derived from `job_steps`.

---

## 4. `JobQueue` port (chapter 5)

```python
class JobQueue(Protocol):
    def enqueue(self, job_id: str) -> None: ...
    def claim(self, worker_id: str, lease_seconds: int) -> str | None: ...   # job_id or None
    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool: ...
    def complete(self, job_id: str, worker_id: str) -> None: ...
    def fail(self, job_id: str, worker_id: str, *, retryable: bool) -> None: ...
    def cancel(self, job_id: str) -> None: ...                                # raises NotImplementedError
```

Driver 1 is `PostgresJobQueue` using `SELECT ... FOR UPDATE SKIP LOCKED`.
**The job row and the queue row MUST be committed in ONE transaction** — this is why the
dual-write/outbox problem does not exist in this slice.

Expired leases MUST be reclaimable. A reclaim increments `attempt`.

---

## 5. `ResultBundle` — the service return contract (chapter 2)

`medos/medos/core/bundle.py` owns these. Use these exact field names.

```python
@dataclass(frozen=True)
class CodedConcept:
    scheme: str          # "SCT" | "DCM" | "RADLEX" | "99MEDOS"
    code: str
    meaning: str

@dataclass(frozen=True)
class Measurement:
    name: CodedConcept
    value: float
    unit: str            # UCUM, e.g. "ml", "%"
    # MUST be computed in SOURCE geometry (chapter 4). Never in model space.

@dataclass(frozen=True)
class Finding:
    kind: str
    present: bool
    score: float | None
    measurements: tuple[Measurement, ...]

@dataclass(frozen=True)
class LabelMap:
    array: np.ndarray            # uint8, SOURCE grid shape (n_slices, rows, cols)
    segments: tuple[CodedConcept, ...]   # index i+1 in array == segments[i]

@dataclass(frozen=True)
class CapabilityOutcome:
    capability_id: str
    findings: tuple[Finding, ...]
    label_map: LabelMap | None
    source_sop_instance_uids: tuple[str, ...]

@dataclass(frozen=True)
class ResultBundle:
    outcomes: tuple[CapabilityOutcome, ...]
```

---

## 6. `Capability` interface

```python
@dataclass(frozen=True)
class CapabilityContext:
    job_id: str
    series_instance_uid: str
    source: SourceGeometry
    clinical_use_mode: str        # "research_only" in this slice

class Capability(Protocol):
    capability_id: str
    version: str
    def applicable(self, vol: CanonicalVolume) -> str | None:
        """Return None if applicable, else a machine-readable rejection reason."""
    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome: ...
```

`medos/medos/capabilities/__init__.py` exposes `REGISTRY: dict[str, Capability]`.
A capability MUST NOT touch the database, the network, or the PACS.

---

## 7. Capabilities in this slice

| capability_id | source | notes |
|---|---|---|
| `lung_segmentation` | HU threshold + largest-components, from the spike | not a learned model; label it honestly |
| `emphysema_laa` | deterministic % voxels < −950 HU inside the lung mask | no model, no training data |
| `pleural_effusion` | **placeholder — returns `present=False` with a `not_implemented` note** | real model needs the clinic data, which is PHI-bearing and out of scope here |

`emphysema_laa` depends on `lung_segmentation`'s mask. Express that as an explicit step
ordering in `worker/steps.py`, not as a hidden import.

---

## 8. Database (no ORM, no tenancy in this slice)

`medos/medos/db/schema.sql` is the only DDL. Tables: `jobs`, `job_steps`, `job_events`,
`job_series`, `results`, `result_measurements`, `result_dicom_objects`, `job_queue`.

Non-negotiable constraints:
- `UNIQUE (job_id, capability_id)` on `results`
- `job_events` append-only; `seq` monotonic per job
- the results row and the terminal state transition MUST be written in ONE transaction

`tenant_id` columns are **deliberately absent** in this slice; weeks 3–5 adds them with RLS.
Do not add a fake single-tenant value — that is harder to migrate than an absent column.

---

## 9. API (thin — replaced by Go at weeks 3–5)

- `POST /api/v1/jobs` → `202` `{"job_id": "...", "state": "QUEUED"}`
  body: `{"study_instance_uid": "...", "capabilities": ["lung_segmentation","emphysema_laa"]}`
  Accepts an `Idempotency-Key` header. **That header MUST NOT feed UID derivation** — the
  UID seed is derived server-side from job identity (chapter 4). This was a register defect.
- `GET /api/v1/jobs/{job_id}` → job with `state`, `phase`, `steps_completed`, `steps_total`,
  `rejection` (when `REJECTED`), `results[]`, `provenance`
- `GET /api/v1/jobs/{job_id}/events` → SSE stream of `job_events` rows, resumable via
  `Last-Event-ID` mapped to `seq`

Errors: RFC 9457 `application/problem+json` with a `class` field whose enum separates
`clinical_rejection` from `transport_failure` and `system_failure`.

---

## 10. Provenance (chapter 9)

Every result MUST record: `job_id`, `study_instance_uid`, the **series actually consumed**,
`capability_id` + `version`, `preprocessing_version`, the generated `SeriesInstanceUID` and
`SOPInstanceUID` per object, `worker_version`, `runtime_version`, and timestamps.
The `GET /api/v1/jobs/{id}` response surfaces this; the OHIF panel renders it.

---

## 11. Conventions

- Python 3.11, type hints everywhere, `ruff` clean.
- No global mutable state. Pass the connection; do not import a singleton.
- Every module that implements a spec requirement cites the ID in its docstring.
- Log structured JSON. **Never log a PHI value** — log UIDs only, never names or MRNs.
- Tests: `pytest`. Unit tests need no containers. Integration tests may assume Postgres and
  Orthanc from `medos/deploy/compose/docker-compose.yml`.
