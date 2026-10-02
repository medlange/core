<!-- MedicalOS Specification v0.4.0 — chapter 10 of 19. Normative.
     118 requirements. Do not edit without a requirement-ID review. -->

[← 9. Clinical Safety, Regulatory Posture and Provenance](09-clinical-safety.md) · [Index](../../MEDICALOS_SPEC.md) · [11. Agentic Layer, Chat and LLM Integration →](11-agentic-layer.md)

---

## 10. API and SDKs

This chapter specifies the public HTTP surface of MedicalOS, its error model, its asynchronous patterns, the single event namespace shared by the server-sent-event stream, webhooks and the message bus, the mechanism by which the OpenAPI document is generated, and the two first-party SDKs.

It specifies *transport and contract only*. The meaning of `Job`, `ServiceVersion`, `Deployment`, `ResultReview`, `ValidationReport`, permissions and events is fixed by Chapters 2, 5, 6, 7, 8 and 9; this chapter says how they are addressed, serialised, paged, retried, streamed and evolved.

### 10.1. Surface, invariants and the endpoints that no longer exist

#### 10.1.1. Base invariants

**MOS-API-001** — All resource endpoints MUST be served under the base path `/api/v1`. Job creation MUST be exactly one endpoint: `POST /api/v1/jobs`. No other path in the system may create a `Job`. Operational probes (`/healthz`, `/readyz`, `/metrics`), the DICOMweb Gateway (`/dicomweb/...`, Chapter 3) and the mesh-internal control surface (`/internal/v1/...`, `MOS-API-001a`) are the only paths outside `/api/v1`, and they MUST NOT expose any resource in table 10.2-B.

**MOS-API-001a** — `/internal/v1/...` is the mesh-internal control surface and is not public API. It carries exactly the paths the owning chapters define — Chapter 3's study-arrival ingest callbacks (`/internal/v1/ingest/...`) and Chapter 13's GPU-residency reservations and pseudonym resolution (`/internal/v1/residency/...`, `/internal/v1/pseudoref/...`, `MOS-OPS-007`, `MOS-OPS-037`) — and this chapter owns only the boundary around them. Every such listener MUST bind to the mesh interface, MUST live inside `Z-PLATFORM` or `Z-GATEWAY`, and MUST NOT be reachable from `Z-EDGE` (Chapter 8). It MUST authenticate by workload identity or a component-scoped key (Chapter 3's `X-MedicalOS-Ingest-Key` and its Chapter 13 equivalents) and MUST NOT accept a tenant credential; `MedicalOS-Tenant-Id` (MOS-API-003) has no meaning on it. It MUST NOT appear in the generated OpenAPI document (§10.8) and MUST NOT be surfaced by either SDK (§10.10). It MUST NOT serve, mirror or proxy any resource of table 10.2-B, and no path under it may create a `Job` (MOS-API-001). Its error responses MUST still be the problem documents of §10.4. Table 10.2-B's completeness claim (MOS-API-012) is a claim about `/api/v1` alone.

**MOS-API-002** — Request and response bodies MUST use `application/json; charset=utf-8` unless a row of table 10.2-B declares otherwise. Error bodies MUST use `application/problem+json` (§10.4). The event stream MUST use `text/event-stream; charset=utf-8` (§10.6). Timestamps MUST be RFC 3339 with millisecond precision in UTC with a literal `Z` suffix (`2026-09-13T09:14:02.113Z`). Durations MUST be integers with a unit suffix in the field name (`timeout_ms`, `lease_seconds`), never ISO 8601 durations.

**MOS-API-003** — Every request MUST carry `Authorization: Bearer <credential>`. The credential determines the `Tenant`; there is no tenant path segment and no tenant query parameter. A `MedicalOS-Tenant-Id` request header MAY be honoured **only** for a principal holding the platform-administrator role, MUST be rejected with `403` for every other principal, and MUST emit an `AuditEvent` whose `on_behalf_of` names the impersonated tenant. Credential formats and role definitions are Chapter 8's.

**MOS-API-004** — The server MUST accept and propagate a W3C `traceparent` request header, MUST return `MedicalOS-Trace-Id: <32-hex trace id>` on every response including every error response, and MUST include the same value as the `trace_id` member of every problem document, every event envelope and every `AuditEvent`. `correlation_id` as a distinct concept is deleted.

**MOS-API-005** — Every route in table 10.2-B MUST declare exactly one permission. A route with no declared permission MUST fail the build (§10.8). There is exactly **one** permission namespace, of the form `<resource>.<action>` (table 10.2-A); the parallel tool-level spelling used by the previous specification (`dicom.study.read`, `dicom.study.write`) is deleted and MUST NOT appear anywhere in code, manifests or policy.

**MOS-API-006** — Direct-identifier PHI MUST NOT appear in a request path, a query string, a problem document, a response header, a webhook payload or an SSE payload. Specifically forbidden in those positions: `PatientID`, `PatientName`, `PatientBirthDate`, `AccessionNumber`, `StudyDescription`, `SeriesDescription` and any free-text field originating from a DICOM header. `StudyInstanceUID` and `SeriesInstanceUID` are permitted, consistently with the bus partition key `<tenant_id>:<study_instance_uid>` fixed by the spine; a URL containing a patient identifier is a defect, not a style preference.

**MOS-API-007** — Pixel data, DICOM Part 10 byte streams and DICOMweb responses MUST NOT be served from `/api/v1`. Imaging bytes are reachable only through the DICOM Gateway (Chapter 3), which holds the only PACS credential. `/api/v1` returns *metadata and references*; references to generated objects are returned as Gateway-relative WADO-RS hrefs.

**MOS-API-008** — Request bodies MUST be validated against the endpoint's JSON Schema with `additionalProperties: false`; an unknown request member MUST produce `400` with class `client_error` and a `violations[]` member. Clients, conversely, MUST ignore unknown *response* members. Strict on the way in, lenient on the way out — this asymmetry is what makes MOS-API-093 (additive evolution) safe.

**MOS-API-111** — The public identifier of a `Job` is `job_` followed by the 26-character canonical **uppercase** Crockford base32 encoding of a ULID — the `<prefix>_<ULID>` shape of `MOS-CORE-028`, with the case fixed here because Crockford base32 has two spellings of every value. The grammar is exactly `^job_[0-9A-HJKMNP-TV-Z]{26}$` (Crockford omits `I`, `L`, `O` and `U`). Matching MUST be case-sensitive: a lowercase form MUST NOT be minted, MUST NOT be accepted in a path segment, query parameter or request body, and MUST NOT be stored. This is the value carried by `Job.job_id`, by the `Location` header of `POST /api/v1/jobs`, by `subject.id` in every `job.*` event envelope, and by the `{job_id}` segment of every row of table 10.2-B. Any other chapter that validates a `Job` identifier — in particular Chapter 11's tool input schemas — MUST use this pattern verbatim rather than restate it, and Chapter 12 MUST persist the value as `jobs.public_id` beside the internal `uuid` primary key, exactly as it already does for `audit_events` and `policy_decisions`. The platform-derived `jobs.idempotency_key` (`^ik_[a-z2-7]{26}$`, MOS-API-048) is a content digest, not a resource identifier, and keeps its own grammar.

#### 10.1.2. The job endpoint: three spellings collapsed into one

The previous specification spelled job creation three different ways in three different places, and dropped the version prefix in a fourth. Because §82 was an *acceptance test* — the most normative form a requirement takes — an implementer had no way to tell which spelling was real.

**Table 10.1-B — retired paths**

| Old spec | Spelling | Status in 0.2.0 |
|---|---|---|
| §51 | `POST /api/v1/jobs` | **Kept. The only job-creation endpoint.** |
| §53 | `POST /api/v1/agents/{agent_id}/execute` | Removed. `Agent` is a 0.4.0 concept (Chapter 11) and never creates jobs. |
| §82 | `POST /api/v1/ai/jobs` | Removed. There is no `/ai` namespace. |
| §55 | `/jobs/{job_id}/events` (no version prefix) | Corrected to `GET /api/v1/jobs/{job_id}/events`. |

**MOS-API-009** — The server MUST route `POST /api/v1/agents/{agent_id}/execute` and `POST /api/v1/ai/jobs` and MUST answer both with `410 Gone`, class `client_error`, code `ENDPOINT_REMOVED`, and an extension member `replaced_by: "POST /api/v1/jobs"`. Routing a removed path to a working handler, or to a bare 404, MUST fail the acceptance check in §10.11 item 2.

**MOS-API-010** — `POST /api/v1/jobs/{job_id}/cancel` MUST exist in the route table with permission `job.cancel` and MUST return `501 Not Implemented`, class `client_error`, code `ENDPOINT_RESERVED`, in every 0.1.x and 0.2.x build. Cancellation of a running inference is not implemented before 0.3.0 (spine §4). The endpoint exists so that the reservation is an executable fact rather than a sentence in prose.

**MOS-API-010a** — `POST /api/v1/jobs/{job_id}/retry` MUST be accepted only when the job is in `FAILED`; any other state MUST produce `409`, class `client_error`, code `JOB_NOT_RETRYABLE`. It MUST NOT recompute `idempotency_key` and MUST NOT re-run resolution (`MOS-EXEC-012`); the pinned `resolved` block is reused unchanged, which is what makes the retry resume rather than duplicate DICOM identity. The transition itself — `FAILED` → `QUEUED`, transition T13 — is Chapter 5's; this chapter contributes only the path, the verb and the permission `job.retry` (Chapter 8's catalogue).

**MOS-API-011** — The surface MUST expose deletion for the two resources a development or pilot site must be able to un-ingest: `DELETE /api/v1/studies/{study_id}` (permission `study.delete`; removes the tenant's projection and instructs the Gateway to purge the tenant's copy) and `DELETE /api/v1/dataset-versions/{dataset_version_id}` (permission `dataset.delete`; MUST return `409` class `client_error` code `DATASET_VERSION_SEALED` if the version is sealed). No other resource is deletable in 0.2.0; `Job`, `Result`, `AuditEvent`, `ValidationReport`, `ServiceVersion` and `Deployment` are append-only.

**MOS-API-011b** — A `ResultReview` row is created by the platform on `Result` creation when `Deployment.review_mode != off` (Chapter 9, `MOS-SAFE-059`). There is no client-facing create; `POST /api/v1/result-reviews` MUST NOT exist. `Result.review_status` is derived and MUST NOT be writable through any endpoint (`MOS-SAFE-062`). The state set, the transitions and the field contract of `ResultReview` are Chapter 9's (`MOS-SAFE-058`); this chapter registers only the routes that drive them.

### 10.2. The resource surface

#### 10.2.1. Permission namespace

**Table 10.2-A — permissions referenced by the route table** (Chapter 8 owns the authoritative registry; these are the spellings the API layer binds to)

| Permission | Grants |
|---|---|
| `tenant.read`, `tenant.write` | Read / mutate `Tenant` |
| `user.read`, `user.write` | Read / mutate `User`, `Role` bindings |
| `service.read`, `service.write`, `service.publish` | Read `Service`; mutate `Service`; publish an immutable `ServiceVersion` |
| `capability.read`, `capability.update` | Read `Capability`; mutate `Capability` and its `AcceptanceCriteria` |
| `harvest_batch.open` | Open a `HarvestBatch` against a sealed `SamplingPlan` and draw its candidates (`MOS-TRAIN-078`) |
| `harvest_candidate.read` | Read `HarvestCandidate` rows and the settled decision on one (`MOS-TRAIN-079`) |
| `curation_decision.record` | Record a `CurationDecision` on a candidate as the named human (`MOS-TRAIN-080`) |
| `corpus_stratification_report.read` | Read a batch's `CorpusStratificationReport` (`MOS-TRAIN-088`) |
| `training_data_policy.record` | Record the tenant's `TrainingDataPolicy` and set `training_use_allowed` with it (`MOS-TRAIN-072`, `MOS-TRAIN-073`) |
| `training_data_policy.read` | Read whether the tenant's `TrainingDataPolicy` permits training use (`MOS-TRAIN-072`). A `read`-class spelling distinct from `training_data_policy.record`, which is `governance` and which Ch. 19 `MOS-UI-102` therefore keeps off the console: `MOS-UI-110` requires the first screen to render `training_use_allowed` in the operator's own words and forbids it rendering the `403`, and a surface whose only way to learn the flag is to be refused cannot obey both sentences |
| `sampling_plan.read`, `sampling_plan.declare` | Read a versioned `SamplingPlan`; declare one before any candidate is drawn (`MOS-TRAIN-083`). `declare` and not `create`: `MOS-STORE-359` seals the row outright — *a plan edited after the draw cannot describe the draw* — so a `create` spelling would owe `MOS-SEC-034` a removal disposition on a resource that has none, which is Chapter 8's edit and not this table's |
| `harvest_batch.read` | Read a `HarvestBatch`, its state and the composition of the candidate set drawn into it (`MOS-TRAIN-078`, Ch. 19 `MOS-UI-115`). Separate from `harvest_candidate.read`, which is `phi`: a batch row carries a capability, a plan, a state and counts, and a principal who may see that a cohort is being assembled need not see the studies in it |
| `dataset_version.read`, `dataset_version.create` | Read a sealed `DatasetVersion`; seal one (Ch. 7 `MOS-EVID-015`). Ch. 19 `MOS-UI-104` fixes `dataset_version.create` as the spelling the seal binds and forbids this surface propagating row 38's `dataset.write`, which §8.3.2 registers as a **non**-permission that a build MUST fail on |
| `dataset_split.read`, `dataset_split.freeze` | Read a frozen `DatasetSplit`; freeze one (Ch. 7 `MOS-EVID-028`). `freeze` is reachable only as the second half of row `R26`'s single seal action (`MOS-UI-130`), never on its own: a freeze route would have to accept `assignments`, and a caller that names which patients are in `test` is `MOS-TRAIN-141`'s *container that can name its own data* with an HTTP client in front of it |
| `annotation_set.read`, `annotation_set.create` | Read a frozen `AnnotationSet`; freeze one (Ch. 7 `MOS-EVID-038`, Ch. 17 `MOS-TRAIN-110`). No `annotation_set.update` is bound by any row: `MOS-TRAIN-110` makes adding a reader or a case a **new** set bound to the same `DatasetVersion`, never an edit, and the `annotation_sets_frozen` trigger refuses the other reading |
| `training_run.read`, `training_run.submit`, `training_run.cancel` | Read a `TrainingRun` and the reproducibility binding of `MOS-TRAIN-124`; submit one against a sealed `DatasetVersion`, a frozen `DatasetSplit` and a frozen `AnnotationSet`; cancel one in flight (Ch. 17 `MOS-TRAIN-122`; Ch. 19 `MOS-UI-147`, `MOS-UI-156`). There is no `training_run.delete` and no `training_run.update`: `MOS-TRAIN-124` seals the binding at submit and 0013's `forbid_training_mutation` refuses the delete, so neither spelling would name an act the platform can perform |
| `configuration_search.read`, `configuration_search.declare`, `configuration_search.nominate` | Read a `ConfigurationSearch`; register one with a digested declarative space and the bounded budget `MOS-TRAIN-234` requires; nominate the single trial `MOS-TRAIN-216` admits. `nominate` is separate from `declare` because it spends the `test` partition (`MOS-TRAIN-217`) and a grant that bundled the two would let whoever may start a search also spend the consumable |
| `conversion_run.read`, `conversion_run.submit` | Read a `ConversionRun` and its E1/E2/E3 equivalence result; submit one against a registered `ModelVersion` (Ch. 17 `MOS-TRAIN-156`, `MOS-TRAIN-161`). No permission admits a tolerance: `MOS-TRAIN-164` makes the remedy for a failing conversion a different conversion, never a relaxed bound |
| `model.read`, `model.write` | Read / register `Model`, `ModelVersion`, `PreprocessingSpec` |
| `model_version.read` | Read one `ModelVersion` and its pinned `PreprocessingSpec` — Chapter 8 §8.3.2 registers this spelling separately from the catalogue-level `model.read` that rows 28–31 bind, and `MOS-UI-102` names it, not `model.read`, in the `evidence_scientist` reference principal. Row `R12` binds it |
| `dataset.read`, `dataset.write`, `dataset.delete` | Read / mutate / delete `Dataset`, `DatasetVersion`, `AnnotationSet`, `DatasetSplit` |
| `evaluation.read`, `evaluation.run` | Read `EvaluationRun`; start one |
| `validation.read`, `validation.approve` | Read `ValidationReport`; record the named human approval |
| `deployment.read`, `deployment.create`, `deployment.update`, `deployment.promote`, `deployment.suspend`, `deployment.retire` | Read a `Deployment`; create one; change its tunables; move it between `role`/`state` values. Names and classes are Chapter 8's table; `deployment.write` is not a permission. |
| `job.create`, `job.read`, `job.retry`, `job.cancel` | Create `Job`; read `Job`, `JobStep`, `JobEvent`, provenance; re-dispatch a `FAILED` job (Chapter 5, T13); cancel (reserved) |
| `result.read`, `result.read.research` | Read `Result` and `ResultBundle`; include `research_only` results in list endpoints (MOS-API-019a, Chapter 9 `MOS-SAFE-041`) |
| `result.review.read`, `result.review.submit`, `result.review.assign`, `result.review.reopen` | Read `ResultReview` rows and their round history; claim, release and submit a review; set `assignee_user_id`; open a new round (Chapter 9 `MOS-SAFE-067`) |
| `study.read`, `study.ingest`, `study.delete` | Read the `Study`/`Series`/`Instance` projection; manually ingest or re-triage a study; purge a study |
| `audit.read` | Read `AuditEvent` |
| `webhook.read`, `webhook.write` | Read / mutate webhook endpoints, rotate secrets, trigger replays |

#### 10.2.2. Route table

Schema names are `$id` basenames under `https://spec.medicalos.org/schemas/v1/`. `Page<T>` is the pagination envelope of §10.3.1. Every `4xx`/`5xx` response is a `Problem` (§10.4) and is therefore not repeated per row.

**Table 10.2-B — the complete `/api/v1` surface**

| # | Method | Path | Purpose | Permission | Request | Success |
|---|---|---|---|---|---|---|
| 1 | GET | `/tenants` | List tenants | `tenant.read` | query filters | `200 Page<Tenant>` |
| 2 | POST | `/tenants` | Create tenant | `tenant.write` | `TenantCreateRequest` | `201 Tenant` |
| 3 | GET | `/tenants/{tenant_id}` | Read tenant | `tenant.read` | — | `200 Tenant` |
| 4 | PATCH | `/tenants/{tenant_id}` | Update tenant settings incl. `external_llm_allowed` | `tenant.write` | `TenantPatch` (`If-Match`) | `200 Tenant` |
| 5 | GET | `/users` | List users | `user.read` | query filters | `200 Page<User>` |
| 6 | POST | `/users` | Create user | `user.write` | `UserCreateRequest` | `201 User` |
| 7 | GET | `/users/me` | Identity and effective permissions of the caller | — (authn only) | — | `200 Principal` |
| 8 | GET | `/users/{user_id}` | Read user | `user.read` | — | `200 User` |
| 9 | PATCH | `/users/{user_id}` | Update user / role bindings | `user.write` | `UserPatch` (`If-Match`) | `200 User` |
| 10 | GET | `/services` | List services | `service.read` | query filters | `200 Page<Service>` |
| 11 | POST | `/services` | Create service identity | `service.write` | `ServiceCreateRequest` | `201 Service` |
| 12 | GET | `/services/{service_id}` | Read service | `service.read` | — | `200 Service` |
| 13 | PATCH | `/services/{service_id}` | Update owner, description | `service.write` | `ServicePatch` (`If-Match`) | `200 Service` |
| 14 | GET | `/service-versions` | List service versions | `service.read` | query filters | `200 Page<ServiceVersion>` |
| 15 | POST | `/service-versions` | Publish an immutable signed release | `service.publish` | `ServiceVersionCreateRequest` (`Idempotency-Key` **required**) | `201 ServiceVersion` |
| 16 | GET | `/service-versions/{service_version_id}` | Read service version | `service.read` | — | `200 ServiceVersion` |
| 17 | GET | `/service-versions/{service_version_id}/manifest` | Raw `service.yaml` as published | `service.read` | — | `200 application/yaml` |
| 18 | GET | `/service-versions/{service_version_id}/attestation` | Image digest, signature, SBOM reference, `legal_manufacturer` | `service.read` | — | `200 Attestation` |
| 18a | GET | `/service-versions/{service_version_id}/clinical` | The published `clinical` block verbatim plus `intended_use_digest` (Ch. 9 `MOS-SAFE-022`) | `service.read` | — | `200 ClinicalBlock` |
| 18b | GET | `/service-versions/{service_version_id}/results-produced` | The `result_id` values this version produced, so a recall can enumerate the affected records (Ch. 9 `MOS-SAFE-032`) | `result.read` | cursor params | `200 Page<ResultRef>` |
| 18c | GET | `/service-versions/{service_version_id}/review-statistics` | Aggregate review outcomes per `(service_id, service_version, tenant_id, month)`; a monitoring signal, never a performance metric (Ch. 9 `MOS-SAFE-071`, `MOS-SAFE-072`) | `result.review.read` | — | `200 ReviewStatistics` |
| 19 | GET | `/capabilities` | List capabilities | `capability.read` | query filters | `200 Page<Capability>` |
| 20 | POST | `/capabilities` | Create capability | `capability.update` | `CapabilityCreateRequest` | `201 Capability` |
| 21 | GET | `/capabilities/{capability_id}` | Read capability | `capability.read` | — | `200 Capability` |
| 22 | GET | `/capabilities/{capability_id}/acceptance-criteria` | Clinical bar owned by the capability | `capability.read` | — | `200 AcceptanceCriteria` |
| 23 | PUT | `/capabilities/{capability_id}/acceptance-criteria` | Set the clinical bar | `capability.update` | `AcceptanceCriteria` (`If-Match`) | `200 AcceptanceCriteria` |
| 24 | GET | `/capabilities/{capability_id}/resolution` | Side-effect-free dry run of the resolution function (Chapter 6, `MOS-REG-110`) | `capability.read` | query: `modality`, `environment`, `study_instance_uid`, `version_range`, `epoch` | `200 ResolutionOutcome` |
| 25 | GET | `/models` | List models | `model.read` | query filters | `200 Page<Model>` |
| 26 | POST | `/models` | Create model identity | `model.write` | `ModelCreateRequest` | `201 Model` |
| 27 | GET | `/models/{model_id}` | Read model | `model.read` | — | `200 Model` |
| 28 | GET | `/model-versions` | List model versions | `model.read` | query filters | `200 Page<ModelVersion>` |
| 29 | POST | `/model-versions` | Register a model version | `model.write` | `ModelVersionCreateRequest` (`Idempotency-Key` **required**) | `201 ModelVersion` |
| 30 | GET | `/model-versions/{model_version_id}` | Read model version incl. `metrics` FK into `EvaluationRun` | `model.read` | — | `200 ModelVersion` |
| 31 | GET | `/model-versions/{model_version_id}/preprocessing-spec` | The pinned `PreprocessingSpec` | `model.read` | — | `200 PreprocessingSpec` |
| 32 | GET | `/datasets` | List datasets | `dataset.read` | query filters | `200 Page<Dataset>` |
| 33 | POST | `/datasets` | Create dataset | `dataset.write` | `DatasetCreateRequest` | `201 Dataset` |
| 34 | GET | `/datasets/{dataset_id}` | Read dataset | `dataset.read` | — | `200 Dataset` |
| 35 | GET | `/dataset-versions` | List dataset versions | `dataset.read` | query filters | `200 Page<DatasetVersion>` |
| 36 | POST | `/dataset-versions` | Create an unsealed dataset version | `dataset.write` | `DatasetVersionCreateRequest` (`Idempotency-Key` **required**) | `201 DatasetVersion` |
| 37 | GET | `/dataset-versions/{dataset_version_id}` | Read dataset version | `dataset.read` | — | `200 DatasetVersion` |
| 38 | POST | `/dataset-versions/{dataset_version_id}/seal` | Seal: freeze manifest, compute content digest | `dataset.write` | `{}` (`Idempotency-Key` **required**) | `200 DatasetVersion` |
| 39 | GET | `/dataset-versions/{dataset_version_id}/manifest` | Content-addressed SOP Instance UID manifest | `dataset.read` | — | `200 DatasetManifest` |
| 40 | DELETE | `/dataset-versions/{dataset_version_id}` | Delete an **unsealed** version | `dataset.delete` | — | `204` |
| 41 | GET | `/evaluations` | List evaluation runs | `evaluation.read` | query filters | `200 Page<EvaluationRun>` |
| 42 | POST | `/evaluations` | Start an evaluation run (long-running, §10.5.3) | `evaluation.run` | `EvaluationRunCreateRequest` (`Idempotency-Key` **required**) | `202 EvaluationRun` + `Location` |
| 43 | GET | `/evaluations/{evaluation_run_id}` | Read run status and aggregates | `evaluation.read` | — | `200 EvaluationRun` |
| 44 | GET | `/evaluations/{evaluation_run_id}/cases` | Per-case metrics, paged | `evaluation.read` | query filters | `200 Page<EvaluationCaseMetric>` |
| 45 | GET | `/validation-reports` | List validation reports | `validation.read` | query filters | `200 Page<ValidationReport>` |
| 46 | POST | `/validation-reports` | Publish and sign a report | `validation.approve` | `ValidationReportCreateRequest` (`Idempotency-Key` **required**) | `201 ValidationReport` |
| 47 | GET | `/validation-reports/{validation_report_id}` | Read report | `validation.read` | — | `200 ValidationReport` |
| 48 | POST | `/validation-reports/{validation_report_id}/approve` | Record the named human approver | `validation.approve` | `ApprovalRequest` | `200 ValidationReport` |
| 49 | GET | `/validation-reports/{validation_report_id}/export` | Offline-verifiable signed bundle | `validation.read` | — | `200 application/vnd.medicalos.validation-report+jwt` |
| 50 | GET | `/deployments` | List deployments | `deployment.read` | query filters | `200 Page<Deployment>` |
| 51 | POST | `/deployments` | Deploy a service version into one `(tenant, environment, capability)` slot at a `role` (Ch. 6 §6.8) | `deployment.create` | `DeploymentCreateRequest` (`Idempotency-Key` **required**) | `201 Deployment`, `state: PENDING` |
| 52 | GET | `/deployments/{deployment_id}` | Read deployment | `deployment.read` | — | `200 Deployment` |
| 52a | GET | `/deployments/{deployment_id}/clinical-gate` | The E1 condition set with pass/fail per condition, computed live whether or not the deployment is `clinical` (Ch. 9 `MOS-SAFE-045`) | `deployment.read` | — | `200 ClinicalGateReport` |
| 53 | PATCH | `/deployments/{deployment_id}` | Change `traffic_permille`, `pin_range`, `promotion_policy`, `residency`, or `clinical_use_mode` (Ch. 9 gate E1 applies to `clinical`). `role` and `state` are changed by rows 54/54a/54b only. | `deployment.update` | `DeploymentPatch` (`If-Match` **required**) | `200 Deployment` |
| 54 | POST | `/deployments/{deployment_id}/promote` | Canary → `ACTIVE`, `STANDBY` ↔ `ACTIVE` swap, or `SUSPENDED` → `SERVING` (Ch. 6 `MOS-REG-077`, `MOS-REG-078`, `MOS-REG-081`) | `deployment.promote` | `PromoteRequest` (approver required when `auto_promote: false`) | `200 Deployment` |
| 54a | POST | `/deployments/{deployment_id}/suspend` | Reversible stop: `state = SUSPENDED`, no new dispatch, in-flight jobs finish | `deployment.suspend` | `SuspendRequest` (`reason` **required**) | `200 Deployment` |
| 54b | POST | `/deployments/{deployment_id}/retire` | `state = DRAINING`, then `RETIRED` automatically when no non-terminal job references it | `deployment.retire` | — | `202 Deployment` |
| 55 | POST | `/jobs` | **Create a job. The only such endpoint.** | `job.create` | `JobCreateRequest` (`Idempotency-Key` **required**) | `202 Job` + `Location` |
| 56 | GET | `/jobs` | List jobs | `job.read` | query filters | `200 Page<Job>` |
| 57 | GET | `/jobs/{job_id}` | Read job | `job.read` | — | `200 Job` |
| 58 | GET | `/jobs/{job_id}/events` | SSE lifecycle stream (§10.6) | `job.read` | `Last-Event-ID` | `200 text/event-stream` |
| 59 | GET | `/jobs/{job_id}/steps` | `JobStep` rows behind `steps_completed`/`steps_total` | `job.read` | — | `200 Page<JobStep>` |
| 60 | GET | `/jobs/{job_id}/series-selection` | Selected and rejected series with reasons | `job.read` | — | `200 SeriesSelection` |
| 61 | GET | `/jobs/{job_id}/provenance` | Full provenance record | `job.read` | — | `200 ProvenanceRecord` |
| 62 | POST | `/jobs/{job_id}/retry` | Operator re-dispatch of a `FAILED` job on its pinned resolution (Chapter 5, T13) | `job.retry` | `{}` (`Idempotency-Key` **required**) | `202 Job` + `Location` |
| 63 | POST | `/jobs/{job_id}/cancel` | **Reserved — MUST return 501 in 0.1–0.2** | `job.cancel` | `{}` | — |
| 64 | GET | `/results` | List results | `result.read` | query filters | `200 Page<Result>` |
| 65 | GET | `/results/{result_id}` | Read result incl. generated DICOM object references | `result.read` | — | `200 Result` |
| 66 | GET | `/results/{result_id}/bundle` | The `ResultBundle` returned by the service | `result.read` | — | `200 ResultBundle` |
| 66a | GET | `/results/{result_id}/provenance` | Provenance record for one result; `tree=true` adds every descendant job, ordered by `job_depth` then `started_at` (Ch. 9 `MOS-SAFE-087`) | `result.read` | query: `tree` | `200 ProvenanceRecord` |
| 66b | GET | `/results/{result_id}/provenance/export` | Offline-verifiable signed provenance bundle with its hash-chain segment; `tree=true` covers the whole tree under one `tree_digest` (Ch. 9 `MOS-SAFE-089`, `MOS-SAFE-101`) | `result.read` | query: `tree` | `200 application/vnd.medicalos.provenance-export+jws` |
| 67 | GET | `/result-reviews` | List reviews | `result.review.read` | query filters | `200 Page<ResultReview>` |
| 68 | POST | `/result-reviews/{result_review_id}/claim` | `PENDING`→`IN_REVIEW` (Ch. 9 `MOS-SAFE-059`) | `result.review.submit` | `{}` | `200 ResultReview` |
| 68a | POST | `/result-reviews/{result_review_id}/release` | `IN_REVIEW`→`PENDING`; only by the current claimant | `result.review.submit` | `{}` | `200 ResultReview` |
| 68b | POST | `/result-reviews/{result_review_id}/submit` | `IN_REVIEW`→`ACCEPTED` \| `MODIFIED` \| `REJECTED` | `result.review.submit` | `ResultReviewSubmitRequest` (`Idempotency-Key` **required**) | `200 ResultReview` |
| 68c | POST | `/result-reviews/{result_review_id}/assign` | Set `assignee_user_id` | `result.review.assign` | `{assignee_user_id}` | `200 ResultReview` |
| 68d | POST | `/result-reviews/{result_review_id}/reopen` | New round, `round + 1`, `PENDING` | `result.review.reopen` | `{action_rationale}` | `201 ResultReview` |
| 69 | GET | `/result-reviews/{result_review_id}` | Read review, including every round via `rounds[]` | `result.review.read` | — | `200 ResultReview` |
| 69a | GET | `/results/{result_id}/review` | Convenience projection of the highest-round review | `result.review.read` | — | `200 ResultReview` |
| 70 | GET | `/studies` | List the study projection | `study.read` | query filters | `200 Page<Study>` |
| 71 | GET | `/studies/{study_id}` | Read study projection | `study.read` | — | `200 Study` |
| 71a | POST | `/studies/{study_id}/ingest` | Manual re-ingest / re-triage of a study already in the backend (Chapter 3, path C) | `study.ingest` | `StudyIngestRequest` (`Idempotency-Key` **required**) | `202 StudyIngest` + `Location` |
| 72 | GET | `/studies/{study_id}/series` | Series inventory from triage | `study.read` | — | `200 Page<Series>` |
| 73 | GET | `/studies/{study_id}/eligibility` | Per-deployed-ServiceVersion admissibility, no job created | `study.read` | — | `200 EligibilityReport` |
| 74 | DELETE | `/studies/{study_id}` | Purge the tenant's copy of a study | `study.delete` | — | `204` |
| 75 | GET | `/audit` | Query audit events | `audit.read` | query filters | `200 Page<AuditEvent>` |
| 76 | GET | `/audit/{audit_event_id}` | Read one audit event | `audit.read` | — | `200 AuditEvent` |
| 77 | GET | `/webhooks` | List webhook endpoints | `webhook.read` | query filters | `200 Page<WebhookEndpoint>` |
| 78 | POST | `/webhooks` | Create endpoint; secret returned once | `webhook.write` | `WebhookCreateRequest` | `201 WebhookEndpointWithSecret` |
| 79 | GET | `/webhooks/{webhook_id}` | Read endpoint | `webhook.read` | — | `200 WebhookEndpoint` |
| 80 | PATCH | `/webhooks/{webhook_id}` | Update URL, subscriptions, active flag | `webhook.write` | `WebhookPatch` (`If-Match`) | `200 WebhookEndpoint` |
| 81 | DELETE | `/webhooks/{webhook_id}` | Delete endpoint | `webhook.write` | — | `204` |
| 82 | POST | `/webhooks/{webhook_id}/rotate-secret` | Begin dual-secret rotation | `webhook.write` | `{}` | `200 WebhookEndpointWithSecret` |
| 83 | GET | `/webhooks/{webhook_id}/deliveries` | Delivery log with attempts and responses | `webhook.read` | query filters | `200 Page<WebhookDelivery>` |
| 84 | POST | `/webhooks/{webhook_id}/deliveries/{delivery_id}/replay` | Replay one delivery | `webhook.write` | `{}` | `202 WebhookDelivery` |
| 85 | POST | `/webhooks/{webhook_id}/replay` | Bulk replay over a window | `webhook.write` | `WebhookBulkReplayRequest` | `202 WebhookReplay` |
| 86 | GET | `/openapi.yaml` | Generated OpenAPI 3.1 for the running build | — (authn only) | — | `200 application/yaml` |
| 87 | GET | `/openapi.json` | The same document as JSON | — (authn only) | — | `200 application/json` |
| R1 | POST | `/harvest-batches` | Open a `HarvestBatch` against a sealed `SamplingPlan` and draw its candidates (Ch. 17 `MOS-TRAIN-078`, `MOS-TRAIN-083`) | `harvest_batch.open` | `HarvestBatchCreateRequest` | `201 HarvestBatch` + `Location` |
| R2 | GET | `/harvest-candidates/{harvest_candidate_id}/decision` | The settled `CurationDecision` on one candidate (Ch. 17 `MOS-TRAIN-079`, `MOS-TRAIN-080`) | `harvest_candidate.read` | — | `200 CurationDecision` |
| R3 | POST | `/harvest-candidates/{harvest_candidate_id}/decision` | Record a `CurationDecision` as the named human, one candidate per request; auto-inclusion MUST NOT be implemented (Ch. 17 `MOS-TRAIN-080`, `MOS-TRAIN-204`) | `curation_decision.record` | `CurationDecisionRequest` | `201 CurationDecision` |
| R4 | PUT | `/tenants/{tenant_id}/training-policy` | Record the tenant's `TrainingDataPolicy` as the named human and set `training_use_allowed` with it, in one transaction (Ch. 17 `MOS-TRAIN-072`, `MOS-TRAIN-073`) | `training_data_policy.record` | `TrainingDataPolicyPutRequest` | `200 TrainingDataPolicy` |
| R5 | GET | `/harvest-batches/{harvest_batch_id}/stratification` | The `CorpusStratificationReport` recorded on a batch (Ch. 17 `MOS-TRAIN-088`) | `corpus_stratification_report.read` | — | `200 CorpusStratificationReport` |
| R6 | POST | `/training-runs` | **SERVED** — **Train, as one HTTP action** (Ch. 19 §19.3.5). `MOS-UI-147` makes Train a single action with no configuration over a read-only summary of four things, so the body is those four as ids and nothing else. Everything else in `MOS-TRAIN-124`'s binding is **server-derived**: the three digests from the sealed and frozen rows, `preprocessing_spec_*` from the capability's registered spec, `code_commit` / `code_dirty` / `image_digest` from the deployed image, `seeds` / `determinism` / `hardware` / `framework_versions` from the run environment, and the whole derived plan from the cohort fingerprint. A value a client may supply is a value a client may supply wrongly, and a binding recording a client's claim about the GPU it ran on is a binding that lies in a `ValidationReport`. `hyperparameters` is not a member, so `MOS-UI-149`'s six hidden quantities are unreachable rather than merely unoffered; `fit_partition` and `select_partition` are not members, which is `MOS-TRAIN-141` on the wire — a caller that can name its own partition can name `test` | `training_run.submit` | `TrainingRunSubmitRequest` | `202 TrainingRun` + `Location` |
| R7 | GET | `/training-runs` | **SERVED** — list runs. `MOS-UI-152` renders a run in progress by capability, cohort, reference standard, state and time since submission, and a surface that cannot find its own runs after a page reload has none of that | `training_run.read` | query filters | `200 Page<TrainingRun>` |
| R8 | GET | `/training-runs/{training_run_id}` | **SERVED** — read one run and the binding of `MOS-TRAIN-124`. `search_trial_score` and `hyperparameters` are not projected: `MOS-TRAIN-222` forbids a selection statistic on a surface presenting model performance and `MOS-TRAIN-224` puts the batch size inside `hyperparameters`, which `MOS-UI-149` forbids revealing | `training_run.read` | — | `200 TrainingRun` |
| R9 | POST | `/training-runs/{training_run_id}/cancel` | **SERVED** — `Orchestrator.Cancel` (`MOS-TRAIN-122`), which `MOS-UI-156` requires the console to offer and to render as a deliberate act attributed to a person rather than as a failure. The body is empty: 0013 admits a `failure_reason` only on `FAILED`, so a reason has nowhere to be stored, and a field the server discards is a field a client will believe was recorded. The actor comes from the credential and the audit event | `training_run.cancel` | `{}` | `200 TrainingRun` |
| R10 | GET | `/capabilities/{capability_id}/seed-variance` | **SERVED** — the live `capability_seed_variance` row (`MOS-TRAIN-127`). `MOS-UI-157` requires the console to display whether the characterisation exists and to offer the two further seed-varied runs when it does not, because an operator who does not know this is owed will produce one candidate and discover the obligation at a gate they cannot reach. `200` with `characterised: false` rather than `404`, which cannot separate *no characterisation* from *no such capability*. Bound to `training_run.read` and not `capability.read` because the row names its `training_run_ids`. The response carries no non-inferiority margin and no member from which one could be computed (`MOS-EVID-087`, `MOS-TRAIN-128`) | `training_run.read` | — | `200 CapabilitySeedVariance` |
| R11 | GET | `/training-runs/{training_run_id}/test-exposure` | **SERVED** — **this is the whole of §19.3.6 on the wire, and it is a read**. `MOS-UI-162` makes evaluation automatic on `SUCCEEDED` and states it MUST NOT be an action the operator initiates: `MOS-TRAIN-138` registers the bundle and `MOS-TRAIN-140` creates the candidate `EvaluationRun` on `partition: "test"` inside the pipeline, with no client. So §19.3.6 gets no `POST`, the run is read through rows 43 and 44, and row 42 is **not** the path for it. What no row carried is the counter `MOS-TRAIN-216` requires the platform to maintain per `(capability_id, split_digest)`, increment on every `SUCCEEDED` `test` run and never reset, and `MOS-UI-163` requires the console to display and label as a count that only goes up. Keyed on a run rather than on the pair, because a path segment holding a digest would make the operator compose one and `MOS-UI-101` forbids that outright | `training_run.read` | — | `200 SplitTestExposure` |
| R12 | GET | `/training-runs/{training_run_id}/candidate` | **SERVED** — **everything §19.3.7 gets, and it acts on nothing**. `MOS-UI-167` requires the promotion control to be absent, not disabled; `MOS-TRAIN-003` makes the pipeline's terminal state a candidate at `VALIDATED` and says it MUST NOT be *capable* of producing more; `MOS-TRAIN-008` puts `VALIDATED → APPROVED` in a human's hands and `MOS-SEC-158` enforces that by the absence of the grant. No approval, deployment, cutover or `clinical_use_mode` row is declared on this surface, and none may be. A read exists because `MOS-UI-168` requires the candidate's terminal state to be rendered as what it is and the deciding role to be **named**, and a console that joined rows 30 and 43 itself would render a state no server asserted. It is not a dossier: `MOS-TRAIN-011` and `MOS-UI-170` put that on a different surface under separation of duties, and §19.3.7 adds no path to it. The body carries no link, no action and no deployment reference | `model_version.read` | — | `200 TrainingRunCandidate` |
| R13 | POST | `/configuration-searches` | **SERVED** — register a `ConfigurationSearch` (`MOS-TRAIN-218`). **Not a console route**: `MOS-UI-150` forbids the no-code surface launching one, because a budget, a space and a nomination are three decisions that operator has no basis for making while spending a resource they cannot see being spent. Declared because `MOS-TRAIN-213` catches *any* procedure producing more than one artifact and keeping a subset by a score computed on data, and a platform serving `TrainingRun` and not this invites the search to be run outside the API and recorded afterwards. The body carries `MOS-TRAIN-234`'s five bounds as required members, so *unbounded* is not expressible | `configuration_search.declare` | `ConfigurationSearchDeclareRequest` | `201 ConfigurationSearch` + `Location` |
| R14 | GET | `/configuration-searches/{configuration_search_id}` | **SERVED** — read the search, its space, its budget, its cost and its nomination | `configuration_search.read` | — | `200 ConfigurationSearch` |
| R15 | POST | `/configuration-searches/{configuration_search_id}/nomination` | **SERVED** — nominate the one trial `MOS-TRAIN-216` admits. Its own route and its own permission because it spends the `test` partition: `MOS-TRAIN-217` makes a later nomination an explicit recorded act with a new row and a fresh increment of `test_exposure_count`, since a silent second nomination off the back of a first `test` run is selection on `test` performed one candidate at a time. There is no `PATCH` on the resource for the same reason | `configuration_search.nominate` | `ConfigurationSearchNominationRequest` | `200 ConfigurationSearch` |
| R16 | POST | `/conversion-runs` | **SERVED** — record and start a `ConversionRun` (`MOS-TRAIN-156`). The body has no `tolerances` member and no per-case skip: `MOS-TRAIN-164` makes the remedy for a failing conversion a different conversion and never a relaxed bound on that version, because a tolerance relaxed to admit one artifact silently relaxes it for every future artifact of that family | `conversion_run.submit` | `ConversionRunSubmitRequest` | `202 ConversionRun` + `Location` |
| R17 | GET | `/conversion-runs/{conversion_run_id}` | **SERVED** — read the run and the full E1/E2/E3 equivalence result, not only its verdict (`MOS-TRAIN-161`, `MOS-REG-104`) | `conversion_run.read` | — | `200 ConversionRun` |
| R18 | GET | `/tenants/{tenant_id}/training-policy` | **SERVED** — the recorded `TrainingDataPolicy` and the `training_use_allowed` flag it set (`MOS-TRAIN-072`, `MOS-TRAIN-073`). The read half of `R4`, and it exists because `MOS-UI-110` requires the console's **first** screen to state that harvesting is not permitted, in the operator's vocabulary, and forbids it rendering an HTTP status, a problem document or the string `403`. Without this row the only way to learn the flag is to be refused by `R1`, so the console would have to render a refusal it is forbidden to render, or infer one it cannot distinguish from a network fault. A `read`-class permission, not `training_data_policy.record`, which is `governance` and which `MOS-UI-102` keeps off this surface entirely | `training_data_policy.read` | — | `200 TrainingDataPolicy` |
| R19 | POST | `/sampling-plans` | **SERVED** — declare the versioned `SamplingPlan` that `MOS-TRAIN-083` requires **before any candidate is drawn**, and that `R1` names. This is the cohort builder's only write: `MOS-UI-111` forbids a query language, a free-text metadata search, a regular expression, a boolean expression builder and a saved-query editor, so `strata` is a closed object of the facets `MOS-UI-112` fixes and each facet carries one member, `include`, holding the values the operator chose. There is no `exclude`, no `min`, no `max`, no `pattern` and no operator member anywhere in the body, which is `MOS-UI-111` rendered structurally rather than trusted to the client. `null` is a permitted element of `include` and means `MOS-TRAIN-084`'s *not recorded*, which `MOS-UI-112` requires be a distinct value and never merged into a default. `min_naive_fraction` and `de_novo_control_fraction` are **not** members: `MOS-TRAIN-083` sets the naive floor and `MOS-TRAIN-101` the de-novo control floor, and a client that may supply either may supply zero | `sampling_plan.declare` | `SamplingPlanDeclareRequest` | `201 SamplingPlan` + `Location` |
| R20 | GET | `/sampling-plans` | **SERVED** — list the plans declared for this tenant so that `R1` can be given one by selection. `MOS-UI-101` forbids the operator composing a UID or a digest in order to complete a task the console offers, and a plan id typed by hand is exactly that | `sampling_plan.read` | query filters | `200 Page<SamplingPlan>` |
| R21 | GET | `/sampling-plans/{sampling_plan_id}` | **SERVED** — read one plan, its strata and its `spec_digest`. `MOS-STORE-359` seals the row, so there is no `PATCH` and no `DELETE` here: a new plan is a new `plan_version`, and the digest is what makes a version-2 plan byte-identical to version 1 visible as such | `sampling_plan.read` | — | `200 SamplingPlan` |
| R22 | GET | `/harvest-batches` | **SERVED** — list batches. The same argument row `R7` makes for runs: a surface whose operator closes the tab mid-curation and cannot find the batch again has taught them that curation work is lost work, and `MOS-UI-138` requires the working set to survive a refusal | `harvest_batch.read` | query filters | `200 Page<HarvestBatch>` |
| R23 | GET | `/harvest-batches/{harvest_batch_id}` | **SERVED** — read one batch, its state and its counts | `harvest_batch.read` | — | `200 HarvestBatch` |
| R24 | GET | `/harvest-batches/{harvest_batch_id}/candidates` | **SERVED** — the curation queue. `MOS-UI-119` requires every candidate to receive an explicit `CurationDecision` by the named operator before it can enter the cohort, and `R2`/`R3` act on one candidate at a time, so without this row the queue cannot be shown and the decisions cannot be made. `MOS-TRAIN-089`'s HMAC `institution_key` is projected and the raw name is not, per `MOS-UI-113`; no field `MOS-TRAIN-079` forbids on a candidate — `PatientName`, `PatientBirthDate`, `AccessionNumber`, institution free text, any source-space UID — is a member, per `MOS-UI-122`. `phi`-class, because `patient_key` and the de-identified study UID are on the row | `harvest_candidate.read` | query filters | `200 Page<HarvestCandidate>` |
| R25 | GET | `/harvest-batches/{harvest_batch_id}/split-preview` | **SERVED** — the *projected split* half of `MOS-UI-115`'s composition panel, which `MOS-UI-116` requires to come from **a server-side evaluation of `assign()`** and forbids being reimplemented in the browser, because a second implementation disagrees with the authoritative one on exactly the cohorts that sit near a bound. `MOS-UI-117` makes it a preview and the seal-time result authoritative, so the body declares itself a preview rather than leaving the console to add the word. The C1–C7 half of the same panel is `R5` and is not duplicated here | `harvest_batch.read` | — | `200 SplitPreview` |
| R26 | POST | `/harvest-batches/{harvest_batch_id}/seal` | **SERVED** — **Seal, as one operator action** (Ch. 19 §19.3.4), and the long-running-operation pattern of `MOS-API-056`: `202` with a `Location` naming a resource that has a status field, polled on `R27` with the `Retry-After: 30` hint of `MOS-API-057`. It is **not** a `Job` and MUST NOT create one — `MOS-API-001` is not an obstacle here but the reason this entity exists, because `MOS-TRAIN-121` C3 forbids the orchestrator account holding `job.create` at all and C2 and C4 keep the pipeline off the path between a `Job` and its `Result`. `EvaluationRun` (row 42) is the precedent `MOS-API-056` already names for long work that is not a `Job`. One row and not two, because `MOS-UI-130` makes sealing the `DatasetVersion` and freezing the `DatasetSplit` one action and forbids the console exposing that it spans two, and `MOS-UI-132` makes the outcome atomic from the operator's view. **No `assignments` member and no split-freeze row anywhere**: a caller that names which patients are in `test` is `MOS-TRAIN-141`'s container that can name its own data, so the server assigns by `MOS-TRAIN-112`'s pure function and the body carries no partition, no fraction and no seed (`MOS-EVID-028`). No `waivers` member (`MOS-UI-135`), no `preprocessing_spec` member (`MOS-UI-137`), no `deidentification_status` and no `source_description`: the first two are controls the operator must not be offered, and the last two are a client asserting that images are de-identified and free text landing in an immutable, undeletable manifest | `dataset_version.create` | `SealRunSubmitRequest` | `202 SealRun` + `Location` |
| R27 | GET | `/seal-runs/{seal_run_id}` | **SERVED** — poll the seal. `MOS-UI-134` requires L3 and L4 to be run *as a named, progress-reported step with the cohort size shown, never as a silent wait*, which is a requirement no synchronous `POST` can satisfy: `progress` carries the phase, the series retrieved and the cohort size. The body's load-bearing member is `check_battery`, one entry per row of `MOS-UI-133`'s nine-row table, each carrying `outcome` ∈ `pass` `fail` `warn` `skipped`. A `skipped` entry MUST carry `skipped_reason` and `operator_disclosure`, and an entry whose `outcome` is `pass` MUST report `series_without_pixel_evidence: 0` — so a seal performed without pixels in reach **cannot** be rendered as one that passed L3 or L4. `MOS-EVID-037` already says L4 reports `skipped` with an explicit reason rather than `pass`; this row is that sentence made unrepresentable to break. A cohort sealed without near-duplicate detection and presented as sealed is the failure `MOS-UI-144` and `MOS-EVID-037` are both written against | `dataset_version.read` | — | `200 SealRun` |
| R28 | GET | `/dataset-splits/{dataset_split_id}` | **SERVED** — read the frozen split, its per-partition patient counts and its **full** `leakage_report` (`MOS-EVID-034`, `MOS-EVID-035`). The whole report and not the verdict: `MOS-EVID-037` makes a `skipped` L4 a permanent property of the split that `MOS-EVID-036` requires reproduced in every `ValidationReport` citing it, and a projection carrying only *frozen* would hide it one screen after `R27` made it visible. There is no `POST /api/v1/dataset-splits`: see row `R26` | `dataset_split.read` | — | `200 DatasetSplit` |
| R29 | POST | `/dataset-versions/{dataset_version_id}/annotation-sets` | **SERVED** — close the annotation campaign and freeze the `AnnotationSet` (`MOS-TRAIN-110`), which `MOS-UI-129` requires to be an explicit action stated in advance to be unappendable. The body carries the campaign's identity — capability, label definition, annotation type, consensus rule and parameters, the readers as `User` principals with `MOS-UI-124`'s structured fields, and `reference_of_record` — and it carries **no per-case annotation payload**. A client that supplies the reference standard on the wire is a client that can supply one no reader produced, and `MOS-EVID-038`'s named readers would then be an assertion rather than a record; `MOS-TRAIN-096` authenticates every annotation session as a `User` precisely so that it is not. `tool` is not a member either: `MOS-UI-124` says the platform fills it from the deployed annotation stack | `annotation_set.create` | `AnnotationSetFreezeRequest` | `201 AnnotationSet` + `Location` |
| R30 | GET | `/dataset-versions/{dataset_version_id}/annotation-sets` | **SERVED** — the reference standards frozen against one cohort, so that `R6`'s `annotation_set_id` is chosen from a list rather than composed (`MOS-UI-101`) and `MOS-UI-147`'s read-only summary can name the one selected | `annotation_set.read` | query filters | `200 Page<AnnotationSet>` |
| R31 | GET | `/annotation-sets/{annotation_set_id}` | **SERVED** — read one frozen set, its readers and its `annotation_digest`. The target of row `R29`'s `Location`. `MOS-EVID-043` requires inter-reader agreement to appear in every report citing a multi-reader set, and `MOS-UI-125` requires the console to have said so before the campaign started, so `reader_count` and the per-reader rows are projected rather than reduced to a name | `annotation_set.read` | — | `200 AnnotationSet` |

**MOS-API-012** — Table 10.2-B is the complete public surface of release 0.2.0: 101 routes, numbered 1–87 with the lettered sub-rows `18a`–`18c`, `52a`, `54a`, `54b`, `66a`, `66b`, `68a`–`68d`, `69a` and `71a`. Every endpoint Chapter 9 requires (`MOS-SAFE-069`) has a row here. A handler that is reachable but absent from the generated route table MUST fail the acceptance check in §10.11 item 3.

**MOS-API-112** — Chapter 17's model-development pipeline needs a control surface, and table 10.2-B now carries it as the reserved rows `R1`–`R5`: `POST /api/v1/harvest-batches`, `GET` and `POST /api/v1/harvest-candidates/{harvest_candidate_id}/decision`, `PUT /api/v1/tenants/{tenant_id}/training-policy`, and `GET /api/v1/harvest-batches/{harvest_batch_id}/stratification`. The three preconditions this requirement set for them are **discharged**. Each has a row in table 10.2-B. Each request and response body has a schema in `medos/schemas/` under the one authority `MOS-API-084` permits. Each has a `permission:` in `medos/api/v1/routes.train.yaml` naming a key that exists in Chapter 8's catalogue — `harvest_batch.open`, `harvest_candidate.read`, `curation_decision.record`, `training_data_policy.record` and `corpus_stratification_report.read`, registered in §8.3.2 and carried in `medos/contracts/permissions.yaml` (`MOS-SEC-032`) — so `MOS-API-005` is satisfied where it previously could not be. The `R` numbering sits outside `MOS-API-012`'s enumeration of rows `1`–`87` and their lettered sub-rows: a reserved row is not part of the served surface that requirement counts, and adding these five does not change its total.

What is discharged is the reservation, not the implementation, and the difference is a statement of fact rather than a prohibition: these five are unserved because no handler exists, and the change that registers one against the `permission:` value of its own row (`MOS-API-089`) serves them. An earlier draft of this paragraph wrote that as **MUST NOT be served until a handler is registered**, which is circular — it forbids serving them until they are served — and attributed the rule to `MOS-API-089`, which carries no reservation clause at all but the opposite kind of rule, that a handler and its permission come from one row so documentation cannot drift from enforcement. Nothing in this specification requires the curation surface to stay unreachable, and nothing does: **all five are now served**, by `medos/medos/api/routes_curation.py`, and `medos/api/v1/routes.train.yaml` marks all five `status: served` — which is checked in both directions against the running ASGI app, so the marker is a claim about the deployment and not an intention. Two obligations travelled with them and are recorded here as they stand. `MOS-API-022`'s enumeration of the rows on which `Idempotency-Key` is **required** still does not reach `R1` or `R3`, so both remain `optional`; what the handlers DO honour is that requirement's second sentence, which is a MUST and reaches every unsafe method — when the header is absent the server generates one and echoes it in `MedicalOS-Idempotency-Key` — on `R1`, `R3` and on `R19`, `R26` and `R29`, which had been serving without it. Widening the **required** list is an edit to this requirement that the change serving `R1`–`R5` deliberately did not make on its own authority; the residual exposure is that `R1` can open two batches from two deliveries of one request, which an operator sees as two batches and not as a wrong cohort, while `R3` cannot double-decide (`curation_decisions_settled_uk` answers `409`) and `R4` is a `PUT` whose second identical call is a no-op. `MOS-API-085`'s "one entry per row of table 10.2-B" is still not true of `medos/api/v1/routes.train.yaml`, which declares the routes the platform actually serves and states the shortfall in its own `coverage:` block rather than declaring handlers that do not exist; serving these five moved no row out of that debt, because the debt counts rows `1`–`87` and the `R`-series sits outside them.

Chapter 19's `MOS-UI-103` and `MOS-UI-368` are written against the state this requirement has now left entirely: both say that no harvest, curation-decision or training-policy permission is registered and that the engineering surface therefore has no API to be a client of. Of the permissions, the rows **and** the serving that is no longer true. Chapter 19 owns those two requirements and this chapter does not edit them; what a reader should take from `MOS-UI-103` today is its second half, which still binds — the console MUST be built against `/api/v1` only, MUST NOT call the orchestrator port of `MOS-TRAIN-122` directly, MUST NOT read the evidence tables directly, and MUST NOT reach the batch environment by any path that bypasses the API's authorisation.

**The surface upstream of the seal, and why it was still missing.** Rows `R6`–`R17` gave §19.3.5 to §19.3.7 a control surface, and `POST /api/v1/training-runs` takes a `dataset_version_id`, a `split_id` and an `annotation_set_id`. None of the three had a producer on `/api/v1`: `declare_sampling_plan`, the curation queue, the seal itself, the split freeze and the annotation-set freeze appeared in no chapter's route table, so the served surface began one step after the operator's first step ended. Rows `R18`–`R31` are that gap closed, as **fourteen reserved rows**: the policy read `MOS-UI-110` needs, the `SamplingPlan` `MOS-TRAIN-083` requires before any candidate is drawn, the batch and candidate reads the curation queue of `MOS-UI-119` sits on, the split preview `MOS-UI-116` requires to be computed server-side, the seal and its polling resource, the frozen split, and the freeze and reads of the `AnnotationSet`. They stand on `MOS-API-089` rather than on this requirement, on the same terms as `R6`–`R17`, and like `R6`–`R17` they are **served**: `medos/api/v1/routes.train.yaml` marks `R1`–`R31` `status: served`, which is the whole of the R-series with nothing left reserved, and `tests/unit/test_permission_contract.py` fails if a row's marker here and its `status` there disagree. An earlier draft of this paragraph said all fourteen were reserved and mounted by nothing; that was true when it was written and stopped being true in the same change, and the same has now happened to `R1`–`R5`.

**The seal is asynchronous, it is polled, and it is not a `Job`. This is the decision the rest of the surface turns on.** Three shapes were available and two are refused here by name.

A **synchronous** seal is refused twice over. `MOS-EVID-018`'s `series_pixel_digest` is a digest over *stored pixel values* computed by the caller that holds the pixels — `medos/medos/evidence/manifest.py` says in terms that the module never opens a DICOM file — and `MOS-EVID-034` L4's `dhash64` is a hash of the normalised mid-axial slice. Both oblige the seal to retrieve every instance of every series of the cohort through the Gateway as the `dataset_export` consumer class (Ch. 17 `MOS-TRAIN-068`, `MOS-TRAIN-199`), and `MOS-TRAIN-114`'s floor of thirty `test` patients puts the smallest sealable cohort at roughly a hundred and fifty. That is minutes. The arithmetic is not even the binding reason: `MOS-UI-134` requires L3 and L4 to be run *as a named, progress-reported step with the cohort size shown, never as a silent wait*, and a synchronous `POST` has nowhere to report progress from. A socket that is either open or closed cannot tell an operator whether a `DatasetVersion` was created, which is precisely the state `MOS-UI-132` forbids leaving them in.

A **`Job`** is refused because `MOS-API-001` means what it says and because Chapter 17 forbids it independently. `MOS-TRAIN-121` C3 excludes `job.create` from the orchestrator account outright; C2 forbids the pipeline appearing between a `Job` and its `Result`; C4 requires a total orchestrator outage to change the outcome of no `Job` at all. A seal that created a `Job` would need a grant the pipeline identity MUST NOT hold and would put a training-plane operation onto the lifecycle the clinical dispatcher watches. `MOS-API-001` is therefore not an obstacle this row works around; it is the reason the row has its own entity.

What remains is `MOS-API-056`'s general long-running-operation pattern, which already names a long operation that is not a `Job` — `EvaluationRun`, row 42. `R26` returns `202` with a `Location` naming a `SealRun`, a resource with a status field, and `R27` is polled on it with the `Retry-After: 30` hint `MOS-API-057` fixes. The work is driven through the `Orchestrator` port of `MOS-TRAIN-122`, whose four methods — `Submit`, `Poll`, `Cancel`, `Logs` — are the shape this pair of rows renders on the wire, and the sentence in this requirement that the pipeline *is driven through its own offline orchestrator, not through `/api/v1`* is the state these rows are written to end.

**The third option — declaring the pixel-dependent checks skipped — is legitimate, is what this deployment will do on the day `R26` is first served, and is admissible only because the wire form makes it impossible to hide.** `medos/medos/evidence/repo.py::load_records` returns `dhash64` as `None` for every rehydrated record, no adapter in the platform computes either quantity from retrieved pixels today, and `MOS-EVID-037` already rules that L4 then reports `skipped` with an explicit reason rather than `pass`. The risk is not that L4 is skipped; it is that a cohort sealed without near-duplicate detection is presented to a no-code operator as *sealed* and reads to them as *checked*. So `SealRun.check_battery` carries one entry per row of `MOS-UI-133`'s nine-row table and no entry may be omitted; `outcome` is a closed four-value enum; an entry whose `outcome` is `skipped` MUST carry both a `skipped_reason` and an `operator_disclosure` written in the register `MOS-UI-105` fixes; and an entry whose `outcome` is `pass` MUST report `series_without_pixel_evidence: 0`, counted separately from `MOS-EVID-037`'s structural fewer-than-three-instances skip, which may coexist with a `pass` because `medos/medos/evidence/leakage.py` passes over the eligible remainder. A handler that retrieved no pixels **cannot** emit a schema-valid `SealRun` claiming L3 or L4 passed. `MOS-UI-144`'s *the console MUST show the skip rather than a pass* is thereby a property of the response rather than an instruction to the browser.

**What the change that serves `R26` and `R27` owes, stated here rather than discovered there.** There is no `seal_runs` table: `0006_evidence` carries the sealed objects and `0011_curation` the batch, and neither carries a row for the act of sealing, so the migration is part of serving these two rows. There is no implementation of `MOS-TRAIN-112`'s `assign()` in the platform — `medos/medos/evidence/repo.py::freeze_split` takes `assignments` from its caller — and `R25` and `R26` both need it; it MUST be written against that requirement's reference implementation and MUST NOT acquire a `seed` parameter (`MOS-EVID-028`). There is no `dataset_export` retrieval adapter that computes `series_pixel_digest` and `dhash64`, which is what decides whether the battery reports `pass` or `skipped` for L3 and L4. And there is no store the reference standard of `R29` is frozen *from*: `freeze_annotation_set` takes its entries from its caller in-process, and the annotation server `MOS-TRAIN-095` names is unbuilt (Ch. 19 §19.4.3). **AMENDED at specification 0.4.0: three of those four have landed, and the mechanism named here no longer exists.** ~~Each is named in `medos/api/v1/routes.train.yaml` on the row that needs it, under `engine_absent:`~~ — measured on both registries, `engine_absent` is not a key any row carries; the rows use `engine:` and `engine_also:` and each closed absence is recorded in the registry's own comment on its row, naming where it landed. The `seal_runs` table is `medos/medos/db/migrations/0015_seal_runs.up.sql`, `MOS-TRAIN-112`'s `assign()` is `medos/medos/training/split.py`, and the `dataset_export` retrieval adapter is `medos/medos/training/retrieval.py` — which exists and RETRIEVES NOTHING ON THIS DEPLOYMENT, and that is not a gap in the adapter: `medos/medos/gateway/app.py` answers every retrieval whose consumer class is neither `clinical_viewer` nor `platform_writer` with `503 DEID_NOT_IMPLEMENTED`, because the de-identification stage is unbuilt and `MOS-DATA-037` requires the egress database. What remains owed is the fourth: there is still no store the reference standard of `R29` is frozen from, and Chapter 19 §19.4.3 inventories its absence. Serving a row whose engine does not exist stays a decision somebody takes in the open; it is taken in the row's comment rather than in a key.

Two further obligations travel with `R18`–`R31`. `MOS-API-022`'s enumeration of the rows on which `Idempotency-Key` is **required** does not reach `R19`, `R26` or `R29`, so three more creating endpoints stand with an optional key — the exposure is smallest on `R26`, where `MOS-TRAIN-209` makes the seal idempotent on content and re-running after a fix reuses the identity. And `MOS-API-005` admits exactly one permission per row, which `R26` cannot honestly satisfy: `MOS-UI-130` makes it one action spanning `dataset_version.create` and `dataset_split.freeze`, and binding only the first would let a principal who may seal a version freeze a split they may not. The row declares `dataset_version.create` and records `dataset_split.freeze` under `also_requires:`, both are named in table 10.2-A, and widening `MOS-API-005` to admit a conjunction is this chapter's edit to its own prose rather than a row.

One binding was fixed before the rest of this surface existed, because it is an error classification and this chapter owns those, and it is unchanged: the refusal of `MOS-TRAIN-072`, when a tenant's `training_use_allowed` is `false`, MUST be `403` with `type: https://spec.medicalos.org/problems/training-use-not-permitted`, `class: authz_error`, `code: TRAINING_USE_NOT_PERMITTED` and `retryable: false`. `medos/schemas/training/training-use-not-permitted-1.0.0.json` pins all four as `const`. THAT BINDS THE SCHEMA TO THIS SENTENCE AND NOTHING BINDS THE CODE, which is the half a client sees: `medos/medos/training/errors.py` raises the only implementation of this refusal and spells the type `https://medicalos.dev/problems/training-use-not-permitted`, under a different authority from the one this requirement fixes. Register entry 80 records the split, which is platform-wide rather than local to this refusal, and MUST be resolved as one decision rather than by editing this constant alone. Until it is, a conforming client matching on `type` will not match what this deployment emits. The schema cannot drift from this sentence without failing the build.

#### 10.2.3. The `Deployment` resource on the wire

The route rows 50–54b address a `Deployment`; the entity itself — its columns, its `role` and `state` value spaces, its slot key and its lifecycle — is defined once, in Chapter 6 §6.8, and is never redefined here. The two requirements below bind that definition to the HTTP surface.

**MOS-API-054a** — The wire representation of a `Deployment` carries `capability_id`, `environment`, `role`, `state`, `traffic_permille`, `clinical_use_mode` and `residency`, with the value spaces of Chapter 6 §6.8. `traffic_permille` (integer, 0–1000) is the **only** traffic field; `traffic_share` and `traffic_weight` MUST NOT appear in any request or response body, and are not aliases. `Deployment.role` and `Deployment.state` are closed enums under `MOS-API-093`.

**MOS-API-054b** — There is no `DELETE /api/v1/deployments/{id}`. A `Deployment` row is permanent provenance: `Job.deployment_id`, `Result.deployment_id` and DICOM `(0018,1000) DeviceSerialNumber` (Chapter 9 `MOS-SAFE-044`, §9.5) reference it after the deployment has stopped serving. Taking a version out of service is `suspend` (reversible) or `retire` (terminal), never a row deletion; stopping a version platform-wide is `SUSPENDED` or `RECALLED` on the artifact (Chapter 2 `MOS-SVC-115`), not a deployment edit. This adds `Deployment` to the append-only set named in `MOS-API-011`.

### 10.3. Request and response conventions

#### 10.3.1. Cursor pagination

**MOS-API-013** — Every collection endpoint MUST paginate with an opaque forward cursor. Offset/limit pagination MUST NOT be offered.

**MOS-API-014** — Query parameters: `limit` (integer, default `50`, maximum `200`; a larger value is clamped and the response MUST set `MedicalOS-Limit-Clamped: true`) and `cursor` (opaque string, absent on the first page).

**MOS-API-015** — The response envelope MUST be exactly:

```json
{
  "items": [],
  "next_cursor": "eyJrIjoiMjAyNi0wOS0xM1QwOToxNDowMi4xMTNaIiwiaWQiOiJqb2JfMDFKOUYzTTJLOFFXRVJUWTBBQkNERUYiLCJmIjoiYTNmOWMyZDQifQ",
  "has_more": true
}
```

`next_cursor` MUST be `null` when `has_more` is `false`. No other members are permitted in the envelope.

**MOS-API-016** — A total count MUST NOT be returned by any collection endpoint. Counting is an unbounded scan on tables that grow per study; a client that needs a count MUST use a purpose-built aggregate endpoint, of which 0.2.0 ships none.

**MOS-API-017** — The cursor MUST be base64url of a JSON object carrying the sort key `k`, the tie-break id `id`, and `f`, a hex fingerprint of the normalised filter set. The server MUST reject a cursor whose `f` does not match the current request's filters with `400`, class `client_error`, code `CURSOR_FILTER_MISMATCH`. The cursor is opaque to clients: an SDK MUST NOT parse, construct or mutate it.

**MOS-API-018** — Default ordering for every collection MUST be `created_at DESC, id DESC` (for `/audit`, `occurred_at DESC, id DESC`). Ordering is not client-selectable in 0.2.0. Pagination MUST be stable under concurrent inserts: because the sort key is descending on creation time with an id tie-break, rows created after the first page was fetched MUST NOT appear on a later page.

#### 10.3.2. Filtering

**MOS-API-019** — Filtering MUST use an explicit per-collection allowlist of `field[__op]=value` parameters. There is no query language, no `q=`, no free-text search and no `OR`. Multiple parameters combine with `AND`. An unknown or non-allowlisted filter MUST produce `400`, class `client_error`, code `UNKNOWN_FILTER`, naming the field.

**MOS-API-020** — Supported operators are the bare form (equality), `__in` (comma-separated), `__ne`, `__gte`, `__lte`. `__in` MUST accept at most 50 values.

**MOS-API-019a** — `include_research` on `/api/v1/results` is a visibility switch, not a field filter, and is the sole exception to the `field[__op]=value` grammar. Default `false`. `true` MUST be honoured only for a principal holding `result.read.research`; without that permission the parameter MUST be ignored and research results MUST remain excluded — it MUST NOT produce `403`, because the existence of research results is itself information the caller is not entitled to (Chapter 9, `MOS-SAFE-041`). Every returned item MUST carry `clinical_use_mode`.

**Table 10.3-A — filter allowlist**

| Collection | Allowed filters |
|---|---|
| `/jobs` | `status__in`, `phase`, `service_id`, `service_version_id`, `capability_id`, `study_instance_uid`, `clinical_use_mode`, `created_at__gte`, `created_at__lte`, `labels.<key>` |
| `/results` | `job_id`, `service_version_id`, `review_status__in`, `result_kind`, `clinical_use_mode__in`, `created_at__gte`, `created_at__lte`, `include_research` (see MOS-API-019a) |
| `/result-reviews` | `result_id`, `state__in`, `reviewer_user_id`, `assignee_user_id`, `overdue`, `created_at__gte` |
| `/audit` | `actor_id`, `action`, `subject_kind`, `subject_id`, `job_id`, `trace_id`, `occurred_at__gte`, `occurred_at__lte` |
| `/service-versions` | `service_id`, `status__in`, `capability_id`, `semver__gte`, `semver__lte`, `jurisdiction`, `cleared` (Chapter 9, `MOS-SAFE-030`) |
| `/model-versions` | `model_id`, `status__in`, `capability_id` |
| `/dataset-versions` | `dataset_id`, `sealed`, `created_at__gte` |
| `/evaluations` | `model_version_id`, `dataset_version_id`, `status__in`, `created_at__gte` |
| `/validation-reports` | `service_version_id`, `capability_id`, `approved`, `published_at__gte` |
| `/deployments` | `environment`, `service_version_id`, `state__in`, `clinical_use_mode` |
| `/studies` | `study_instance_uid`, `modality`, `received_at__gte`, `received_at__lte` |
| `/webhooks` | `active`, `event_type` |
| `/webhooks/{id}/deliveries` | `event_type`, `status__in`, `event_id`, `created_at__gte` |

**MOS-API-021** — Example, showing pagination and filtering together:

```http
GET /api/v1/jobs?status__in=COMPLETED,REJECTED&created_at__gte=2026-09-01T00:00:00Z&limit=50 HTTP/1.1
Host: medicalos.stmarys.example
Authorization: Bearer mos_live_7Kq2xR9pLt4WcYb1
Accept: application/json
```

#### 10.3.3. Idempotency

**MOS-API-022** — `Idempotency-Key` MUST be supplied by the client on the endpoints marked **required** in table 10.2-B (rows 15, 29, 36, 38, 42, 46, 51, 55, 62, 68b, 71a). Omitting it MUST produce `400`, class `client_error`, code `IDEMPOTENCY_KEY_REQUIRED`. Other unsafe methods MAY accept the header; when absent the server MUST generate one and MUST echo it in `MedicalOS-Idempotency-Key`.

**MOS-API-023** — The key MUST match `^[A-Za-z0-9._~-]{1,255}$`.

**MOS-API-024** — The idempotency scope MUST be the triple `(tenant_id, operation_id, idempotency_key)`. Two different endpoints may legitimately use the same key.

**MOS-API-025** — The server MUST store, against that triple: a SHA-256 fingerprint of the canonicalised request body (RFC 8785 JSON Canonicalization Scheme), the response status, the response body, and the created resource id.

**MOS-API-026** — Repeat with the **same** fingerprint MUST replay the stored response verbatim, with the original status code, plus `MedicalOS-Idempotent-Replay: true`. It MUST NOT create a second resource.

**MOS-API-027** — Repeat with a **different** fingerprint MUST return `409`, class `client_error`, code `IDEMPOTENCY_KEY_CONFLICT`, with extension members `original_fingerprint` and `original_resource_id`.

**MOS-API-028** — A repeat arriving while the first request is still in flight MUST return `409`, code `IDEMPOTENCY_KEY_IN_FLIGHT`, with `Retry-After: 1`.

**MOS-API-029** — The stored response MUST be retained for at least 7 days and the key itself for the life of the resource, for audit. The client-supplied `Idempotency-Key` is a **request**-deduplication token only. It MUST be persisted as `jobs.request_idempotency_key`, MUST NOT be written to `jobs.idempotency_key`, and MUST NOT participate in DICOM UID derivation. `jobs.idempotency_key` is derived by the platform from the resolved job content under `MOS-EXEC-053` and is not settable by any client (`MOS-EXEC-054`); a client that reuses a key with a different body therefore cannot reach another study's DICOM identity, and MOS-API-027 remains a correctness control on response replay, not the patient-safety control.

**MOS-API-030** — `jobs.idempotency_key` is computed by the platform for every job regardless of origin — HTTP submission under MOS-API-043, agentic `job.submit` (`MOS-AGENT-043`), or study-arrival triage (Chapter 3) — by the single function `derive_idempotency_key` of `MOS-EXEC-053`, after resolution has been pinned into the job row. This chapter defines no second derivation and no origin-specific variant. Re-submission or re-triage of the same study against the same resolved service version, output set and parameters therefore reaches the same job row (`UNIQUE (tenant_id, idempotency_key)`, `MOS-EXEC-055`) and the same DICOM identity (`MOS-IMG-062`).

#### 10.3.4. Concurrency, rate limits and response headers

**MOS-API-031** — Mutable resources (`Tenant`, `User`, `Service`, `Capability`, `AcceptanceCriteria`, `Deployment`, `WebhookEndpoint`) MUST carry a strong `ETag`. `PATCH` and `PUT` on `Deployment` and `AcceptanceCriteria` MUST require `If-Match`; a missing header MUST produce `428 Precondition Required` and a stale one `412 Precondition Failed`, both class `client_error`.

**MOS-API-032** — Immutable resources (`ServiceVersion`, `ModelVersion`, a sealed `DatasetVersion`, `EvaluationRun`, `ValidationReport`, `Job`, `Result`, `AuditEvent`) MUST be served with `Cache-Control: private, max-age=31536000, immutable` once terminal, and with `Cache-Control: no-store` while non-terminal.

**MOS-API-033** — Every response MUST carry `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` (seconds until the window resets). `429` MUST additionally carry `Retry-After`.

**MOS-API-034** — Default per-credential limits, overridable per tenant: 100 requests/second sustained with a burst of 200 for safe methods; 20 requests/second for `POST /api/v1/jobs`; at most 50 concurrent SSE streams per tenant. Exceeding the SSE limit MUST produce `429` with code `STREAM_LIMIT_EXCEEDED`.

### 10.4. Error model (RFC 9457)

**MOS-API-035** — Every `4xx` and `5xx` response MUST be an RFC 9457 problem document with media type `application/problem+json`. A bare string body, an HTML error page, or a `200` carrying `{"error": ...}` MUST NOT occur.

**MOS-API-036** — Standard members MUST be populated: `type` (absolute URI under `https://spec.medicalos.org/problems/`, stable for the life of the major version, never dereferenced at runtime by clients), `title` (short, human-readable, stable per `type`), `status`, `detail` (instance-specific, PHI-free), `instance` (the request path). `https://spec.medicalos.org/problems/` is the only permitted prefix. A `type` under any other host is a defect; CI MUST grep the repository and this specification for `/problems/` URIs and fail on any other authority.

**MOS-API-037** — The following extension members MUST be present on every problem document: `class` (closed enum, table 10.4-A), `code` (open enum, `SCREAMING_SNAKE_CASE`, machine-readable, stable per `type`), `retryable` (boolean), `trace_id`, `occurred_at`.

**Table 10.4-A — the `class` enum**

| `class` | Meaning | Typical status | `retryable` | Client behaviour |
|---|---|---|---|---|
| `clinical_rejection` | The request was understood, well-formed and authorised, and the **clinical** answer is no: the study is outside the applicability envelope, no series qualifies, no candidate resolves, or the deployment is not permitted to operate clinically. Not an error. | `422` | `false` | Surface as information, never as a failure. Do not retry. |
| `client_error` | Malformed, schema-invalid, conflicting, unsupported or removed. | `400` `404` `409` `410` `412` `415` `422` `428` `501` | `false` | Fix the request. |
| `authz_error` | Unauthenticated, unauthorised, or crossing a tenant boundary. | `401` `403` | `false` | Re-authenticate or stop. |
| `rate_limit` | Throttled or over quota. | `429` | `true` | Wait `Retry-After`, then retry the same `Idempotency-Key`. |
| `transport_failure` | A dependency the platform owns the call to — PACS via the Gateway, object store, queue, inference backend — was unreachable, timed out, or returned an unusable response. | `502` `503` `504` | `true` | Retry with backoff and the same `Idempotency-Key`. |
| `system_failure` | An internal invariant was violated. A bug. | `500` | `false` | Do not retry; report `trace_id`. |

**MOS-API-038** — `class` is a **closed** enum. Adding a value is a breaking change (§10.9). `code` and `type` are **open**: clients MUST branch on `class` first and MAY branch on `code`, and MUST tolerate an unrecognised `code`. Chapters that surface an error condition MUST map it onto one of the six values above and express the specificity in `code`; a `class` word minted outside this table is a defect.

**MOS-API-039** — `clinical_rejection` MUST NOT be rendered by any client, SDK or UI with the same affordance as `client_error`, `transport_failure` or `system_failure`. "This study was not analysed, and here is the clinical reason" and "the platform broke" are the same red X today and one of them is information a radiologist must see. The SDK rule that enforces this is MOS-API-100.

**MOS-API-040** — A `Job` that reaches `REJECTED` MUST carry, in its `rejection` member, a problem document of class `clinical_rejection` with the same `type`/`code` vocabulary used synchronously. A `Job` that reaches `FAILED` MUST carry an `error` member of class `transport_failure` or `system_failure`. A `Job` MUST NOT carry both. The internal failure taxonomy behind `error` and the database columns behind both members are Chapter 5's (§5.3.1, §5.3.2) and Chapter 12's; neither shape appears on the wire, and the mapping from the internal class to these two is `MOS-EXEC-016a`.

**MOS-API-041** — Authentication failure MUST be `401` and authorisation failure `403`; a cross-tenant reference MUST be `403` with code `CROSS_TENANT_DENIED` and MUST NOT leak whether the referenced id exists.

#### 10.4.1. Worked example — `clinical_rejection`

Deploying a service version into `clinical` mode when its publisher carries no `legal_manufacturer` identity (Chapter 9):

```http
POST /api/v1/deployments HTTP/1.1
Content-Type: application/json
Idempotency-Key: deploy-effusion-3.2.1-prod-01

{"service_version_id":"sv_01J8ZK5Q3N7C2T0M4RXB9WYD","capability_id":"cap_pleural_effusion","environment":"production","role":"ACTIVE","traffic_permille":1000,"clinical_use_mode":"clinical"}
```

```http
HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json
MedicalOS-Trace-Id: 4bf92f3577b34da6a3ce929d0e0e4736

{
  "type": "https://spec.medicalos.org/problems/clinical-mode-requires-manufacturer",
  "title": "Clinical deployment requires a declared legal manufacturer",
  "status": 422,
  "detail": "ServiceVersion sv_01J8ZK5Q3N7C2T0M4RXB9WYD declares no legal_manufacturer and no regulatory_status for jurisdiction 'GB'. It may be deployed with clinical_use_mode 'research_only' only.",
  "instance": "/api/v1/deployments",
  "class": "clinical_rejection",
  "code": "CLINICAL_MODE_REQUIRES_MANUFACTURER",
  "retryable": false,
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "occurred_at": "2026-09-13T09:02:11.884Z",
  "missing_fields": ["legal_manufacturer", "regulatory_status.GB"],
  "permitted_modes": ["research_only"]
}
```

The asynchronous form of the same class, embedded in a `REJECTED` job, is shown in §10.5.2.

#### 10.4.2. Worked example — `client_error`

```http
HTTP/1.1 400 Bad Request
Content-Type: application/problem+json

{
  "type": "https://spec.medicalos.org/problems/schema-violation",
  "title": "Request body failed schema validation",
  "status": 400,
  "detail": "2 violations against JobCreateRequest.",
  "instance": "/api/v1/jobs",
  "class": "client_error",
  "code": "SCHEMA_VIOLATION",
  "retryable": false,
  "trace_id": "9c1a0f6b2d834e77a5b0c3e1d4f68a92",
  "occurred_at": "2026-09-13T09:14:01.002Z",
  "schema": "https://spec.medicalos.org/schemas/v1/JobCreateRequest.json",
  "violations": [
    {"pointer": "/input/study_instance_uid", "code": "PATTERN", "detail": "not a valid DICOM UID: '1.2.840.113619.2.55.3.60468811X'"},
    {"pointer": "/target/version_range", "code": "NOT_ALLOWED", "detail": "version_range MUST be absent when target.kind is 'service_version'"}
  ]
}
```

#### 10.4.3. Worked example — `authz_error`

```http
HTTP/1.1 403 Forbidden
Content-Type: application/problem+json

{
  "type": "https://spec.medicalos.org/problems/cross-tenant-denied",
  "title": "Resource belongs to another tenant",
  "status": 403,
  "detail": "The presented credential is scoped to tenant tnt_stmarys and may not address this resource.",
  "instance": "/api/v1/jobs/job_01J9E7V4P2M6K8R0T3W5Y7ZA",
  "class": "authz_error",
  "code": "CROSS_TENANT_DENIED",
  "retryable": false,
  "trace_id": "e21b7c9044f34a1c8db3f0a6c5e28b13",
  "occurred_at": "2026-09-13T09:16:40.551Z"
}
```

#### 10.4.4. Worked example — `rate_limit`

```http
HTTP/1.1 429 Too Many Requests
Content-Type: application/problem+json
Retry-After: 3
RateLimit-Limit: 20
RateLimit-Remaining: 0
RateLimit-Reset: 3

{
  "type": "https://spec.medicalos.org/problems/rate-limited",
  "title": "Rate limit exceeded",
  "status": 429,
  "detail": "Job submission limit of 20 requests per second exceeded for credential mos_live_7Kq2...b1.",
  "instance": "/api/v1/jobs",
  "class": "rate_limit",
  "code": "RATE_LIMIT_EXCEEDED",
  "retryable": true,
  "trace_id": "7a44d2e1c0b849f5a2e6d8b3f1c07a55",
  "occurred_at": "2026-09-13T09:17:02.310Z",
  "retry_after_ms": 3000
}
```

#### 10.4.5. Worked example — `transport_failure`

```http
HTTP/1.1 504 Gateway Timeout
Content-Type: application/problem+json
Retry-After: 10

{
  "type": "https://spec.medicalos.org/problems/dependency-timeout",
  "title": "Upstream dependency timed out",
  "status": 504,
  "detail": "QIDO-RS series query to the DICOM Gateway exceeded the 15000 ms budget.",
  "instance": "/api/v1/studies/std_01J9E2B7K4N8Q1S3U5W7Y9AB/series",
  "class": "transport_failure",
  "code": "GATEWAY_TIMEOUT",
  "retryable": true,
  "trace_id": "c83f5b1a7d2e4906b1c4a7e0f2d95836",
  "occurred_at": "2026-09-13T09:18:55.207Z",
  "dependency": "dicom-gateway",
  "retry_after_ms": 10000
}
```

#### 10.4.6. Worked example — `system_failure`

```http
HTTP/1.1 500 Internal Server Error
Content-Type: application/problem+json

{
  "type": "https://spec.medicalos.org/problems/internal-error",
  "title": "Internal error",
  "status": 500,
  "detail": "The request could not be completed. Quote trace_id when reporting this.",
  "instance": "/api/v1/results/res_01J9F3M4Q9K1M3P5R7T9V2XZ/bundle",
  "class": "system_failure",
  "code": "INTERNAL_ERROR",
  "retryable": false,
  "trace_id": "1f0d6a8c3b5e47d2914a6c0e8b27f435",
  "occurred_at": "2026-09-13T09:22:14.006Z"
}
```

**MOS-API-042** — A `system_failure` document MUST NOT include a stack trace, SQL text, internal hostname, file path, or any part of the request body. The `trace_id` is the entire diagnostic handoff.

### 10.5. Job submission and the long-running-operation pattern

#### 10.5.1. `POST /api/v1/jobs`

**MOS-API-043** — `JobCreateRequest`:

```json
{
  "target": {
    "kind": "capability",
    "id": "pleural_effusion",
    "version_range": ">=3.2 <4"
  },
  "input": {
    "study_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.109",
    "prior_study_instance_uids": [],
    "series_instance_uids": []
  },
  "requested_outputs": ["SEG", "SR"],
  "labels": {"trial_arm": "A", "reader_session": "rs_2026_09_13_am"}
}
```

There is no `idempotency_key` member. The client's key travels in the `Idempotency-Key` header (MOS-API-022) and is echoed onto the job as `request_idempotency_key`; `jobs.idempotency_key` is derived by the platform (MOS-API-030, `MOS-EXEC-053`).

**MOS-API-044** — `target.kind` MUST be `capability` or `service_version`; the enum is closed. When `kind` is `service_version`, `id` is a `ServiceVersion` id, `version_range` MUST be absent, and the target is the highest-precedence explicit pin in the resolution order (Chapter 6). When `kind` is `capability`, `version_range` MAY carry a semver range such as `">=3.2 <4"`.

**MOS-API-045** — `input.study_instance_uid` is required and names exactly one primary study, consistent with the one-job-per-(ServiceVersion, Study, selected Series set) rule. `input.prior_study_instance_uids` is an array, possibly empty, present **from 0.1.0** so that prior-comparison work never requires a breaking widening. The schema shape is fixed at gate `G-0.1.0` by OQ-13 (Chapter 16, `MOS-OPEN-034`) and this chapter MUST NOT re-date it. Until the prior-selection rule (Chapter 3) and inter-study registration (Chapter 4) exist, a non-empty `prior_study_instance_uids` MUST be refused with `422`, class `client_error`, code `PRIORS_NOT_SUPPORTED`; full prior support is deferred to 0.4.0. The array is present from the first release so that enabling it later is additive (table 10.9-A). `input.series_instance_uids`, when non-empty, pins the series set; a pinned set that does not satisfy the service version's `SeriesSelector` MUST produce a `REJECTED` job and MUST NOT be silently re-selected.

**MOS-API-046** — A successful submission MUST return `202` with the complete `Job` representation, a `Location` header naming the job resource, and `Retry-After` in seconds as a polling hint:

```http
HTTP/1.1 202 Accepted
Location: /api/v1/jobs/job_01J9F3M2K8QWERTY0ABCDEF
Retry-After: 2
Content-Type: application/json; charset=utf-8
MedicalOS-Trace-Id: 4bf92f3577b34da6a3ce929d0e0e4736
MedicalOS-Idempotency-Key: pilot-2026-09-13-effusion-0001
```

**MOS-API-047** — `POST /api/v1/jobs` MUST NOT return a `clinical_rejection` synchronously. Zero resolution candidates, an empty eligible series set, or an out-of-envelope study all produce a persisted `Job` in state `REJECTED` carrying the reason. This is deliberate: a clinical non-answer is evidence and must be a queryable, auditable row, not an HTTP status a client can drop on the floor. Synchronous `clinical_rejection` occurs only on endpoints that evaluate clinical admissibility *without* creating a job — row 51 (`POST /deployments`) and row 73 (`GET /studies/{study_id}/eligibility`).

#### 10.5.2. The `Job` resource

**MOS-API-048** — The `Job` representation:

```json
{
  "job_id": "job_01J9F3M2K8QWERTY0ABCDEF",
  "tenant_id": "tnt_stmarys",
  "status": "RUNNING",
  "phase": "inference",
  "steps_completed": 2,
  "steps_total": 5,
  "target": {"kind": "capability", "id": "pleural_effusion", "version_range": ">=3.2 <4"},
  "resolved": {
    "service_id": "svc_pulmoai_effusion",
    "service_version_id": "sv_01J8ZK5Q3N7C2T0M4RXB9WYD",
    "service_version": "3.2.1",
    "execution_mode": "native",
    "model_version_ids": ["mv_01J8ZK6R0P4A1S9Q2VTC8XNE"],
    "preprocessing_spec_version_id": "psp_01J8ZK6R2B5D3F7H9KMN1QRS",
    "resolution_reason": "deployment_state"
  },
  "input": {
    "study_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.109",
    "prior_study_instance_uids": [],
    "selected_series_instance_uids": ["1.2.840.113619.2.55.3.604688119.971.1547034262.117"]
  },
  "requested_outputs": ["SEG", "SR"],
  "clinical_use_mode": "research_only",
  "idempotency_key": "ik_x4hq2mtn6pkz3a7fvy5rw9cdeb",
  "request_idempotency_key": "pilot-2026-09-13-effusion-0001",
  "result_ids": [],
  "rejection": null,
  "error": null,
  "labels": {"trial_arm": "A", "reader_session": "rs_2026_09_13_am"},
  "sequence": 7,
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "created_at": "2026-09-13T09:14:02.113Z",
  "queued_at": "2026-09-13T09:14:02.140Z",
  "started_at": "2026-09-13T09:14:07.903Z",
  "terminated_at": null
}
```

`idempotency_key` is platform-derived and read-only (`MOS-EXEC-053`), matching `^ik_[a-z2-7]{26}$`; `request_idempotency_key` echoes the caller's header and has no effect on DICOM identity. A client MUST NOT send either member in a request body.

**MOS-API-049** — `status` MUST be exactly one of `CREATED`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `REJECTED`. It is a **closed** enum. Internal stages such as postprocessing or waiting MUST NOT appear in it.

**MOS-API-050** — `phase` is an **open**, free-form lowercase string (`admitted`, `triage`, `retrieving`, `preprocessing`, `inference`, `inverse_transform`, `writing_dicom`, `storing`). Clients MUST render an unrecognised phase verbatim and MUST NOT branch on it for correctness. Adding a phase is not a breaking change.

**MOS-API-051** — The API MUST NOT expose any float `progress` field, on the `Job` or in any event. Progress is `steps_completed` and `steps_total`, derived from `JobStep` rows. A status-to-number lookup shown to a clinician is a fabricated signal.

**MOS-API-052** — `resolved` MUST be populated at job creation and MUST NOT change over the life of the job, including across retries. This is what makes a result reproducible when a newer service version is approved a month later.

**MOS-API-053** — A `REJECTED` job. Note that `status` is terminal, `error` is `null`, and the reason is a full problem document:

```json
{
  "job_id": "job_01J9F4N7T3R5V7X9Z1B3D5FG",
  "status": "REJECTED",
  "phase": "triage",
  "steps_completed": 1,
  "steps_total": 5,
  "rejection": {
    "type": "https://spec.medicalos.org/problems/no-eligible-series",
    "title": "No eligible series in study",
    "status": 422,
    "detail": "Study contains 5 series; none satisfies SeriesSelector 'chest-ct-thin-axial' of service version sv_01J8ZK5Q3N7C2T0M4RXB9WYD.",
    "instance": "/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FG",
    "class": "clinical_rejection",
    "code": "NO_ELIGIBLE_SERIES",
    "retryable": false,
    "trace_id": "b7e3c9a1052f4d68b4a9e2c70d31f846",
    "occurred_at": "2026-09-13T09:31:08.774Z",
    "series_selection_href": "/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FG/series-selection"
  },
  "error": null,
  "terminated_at": "2026-09-13T09:31:08.774Z"
}
```

**MOS-API-054** — `GET /api/v1/jobs/{job_id}/series-selection` MUST return the selected set and every rejected series with a machine-readable reason. Selection is first-class data, not a log line:

```json
{
  "job_id": "job_01J9F4N7T3R5V7X9Z1B3D5FG",
  "selector_id": "chest-ct-thin-axial",
  "service_version_id": "sv_01J8ZK5Q3N7C2T0M4RXB9WYD",
  "selected": [],
  "rejected": [
    {"series_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.111", "series_number": 1, "reason": "IMAGE_TYPE_EXCLUDED", "detail": "ImageType contains LOCALIZER"},
    {"series_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.114", "series_number": 2, "reason": "SLICE_THICKNESS_OUT_OF_RANGE", "detail": "SliceThickness 5.0 mm exceeds max 1.5 mm"},
    {"series_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.118", "series_number": 4, "reason": "ORIENTATION_OUT_OF_TOLERANCE", "detail": "coronal reformat, 89.7 deg from axial"},
    {"series_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.121", "series_number": 5, "reason": "INSTANCE_COUNT_BELOW_MIN", "detail": "2 instances, minimum 40"},
    {"series_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.900", "series_number": 99, "reason": "MODALITY_MISMATCH", "detail": "Modality SR (X-Ray Radiation Dose Report)"}
  ]
}
```

**MOS-API-055** — `GET /api/v1/results/{result_id}` MUST name every generated DICOM object, so that "where did the output go" is answerable from the API alone, and MUST carry the AI-derived marking fields of Chapter 9 `MOS-SAFE-053` at the top level:

```json
{
  "result_id": "res_01J9F3M4Q9K1M3P5R7T9V2XZ",
  "job_id": "job_01J9F3M2K8QWERTY0ABCDEF",
  "tenant_id": "tnt_stmarys",
  "service_version_id": "sv_01J8ZK5Q3N7C2T0M4RXB9WYD",
  "service_id": "pulmo.pleural-effusion",
  "service_version": "3.2.1",
  "legal_manufacturer_name": "PulmoAI GmbH",
  "derivation": "ai_derived",
  "result_kind": "segmentation",
  "review_status": "UNREVIEWED",
  "clinical_use_mode": "research_only",
  "bundle_href": "/api/v1/results/res_01J9F3M4Q9K1M3P5R7T9V2XZ/bundle",
  "dicom_objects": [
    {
      "kind": "SEG",
      "sop_class_uid": "1.2.840.10008.5.1.4.1.1.66.4",
      "sop_instance_uid": "2.25.104312887701946225329553481150947762233",
      "series_instance_uid": "2.25.88711204563308891274465932208877310096",
      "series_number": 9001,
      "wado_rs_href": "/dicomweb/studies/1.2.840.113619.2.55.3.604688119.971.1547034262.109/series/2.25.88711204563308891274465932208877310096/instances/2.25.104312887701946225329553481150947762233"
    },
    {
      "kind": "SR",
      "sop_class_uid": "1.2.840.10008.5.1.4.1.1.88.34",
      "sop_instance_uid": "2.25.317744029861108733920514477339051229714",
      "series_instance_uid": "2.25.51229038847701122649930055417720394881",
      "series_number": 9002,
      "wado_rs_href": "/dicomweb/studies/1.2.840.113619.2.55.3.604688119.971.1547034262.109/series/2.25.51229038847701122649930055417720394881/instances/2.25.317744029861108733920514477339051229714"
    }
  ],
  "superseded_by": null,
  "created_at": "2026-09-13T09:21:44.309Z"
}
```

`result_kind` is the clinical output category enumerated by Chapter 12 §12.11 and is a closed enum here (`MOS-API-093`); the DICOM object kinds `SEG`/`SR`/`SC`/`PR` are `dicom_objects[].kind`, a different vocabulary. `sop_class_uid` values are Chapter 4's — the SR SOP Class is always Comprehensive 3D SR `1.2.840.10008.5.1.4.1.1.88.34` (`MOS-IMG-060`), never Comprehensive SR. `review_status` is the derived projection of the highest-round `ResultReview` (Chapter 9 `MOS-SAFE-062`) and MUST NOT be writable through any endpoint.

#### 10.5.3. The general long-running-operation pattern

**MOS-API-056** — Every long-running operation MUST follow one pattern: a `POST` returning `202` with a `Location` header naming a **resource with a `status` field**, never a bare operation handle. `Job` (row 55) and `EvaluationRun` (row 42) are the two such operations in 0.2.0.

**MOS-API-057** — Only `Job` exposes an SSE stream in 0.2.0. `EvaluationRun` is polled on its own resource; `Retry-After: 30` MUST be returned on creation as the polling hint. A client MUST NOT poll faster than the returned `Retry-After` and, absent the header, no faster than every 2 seconds.

**MOS-API-058** — `GET /api/v1/capabilities/{capability_id}/resolution` MUST be side-effect-free, MUST NOT create a `Job`, and MUST run the production resolver against the current snapshot — or, when the optional `epoch` query parameter is supplied, against the replayed historical snapshot — so that the resolution function is testable and explainable without spending a study:

```json
{
  "capability_id": "pleural_effusion",
  "query": {"modality": "CT", "environment": "production", "version_range": ">=3.2 <4", "epoch": 171402},
  "decision": "SELECTED",
  "resolver_version": "1.0.0",
  "snapshot_id": "sha256:9c1f2ab7d0e4358f61b7c09ad3e5f1826b40c79d5aa2e618f3c47b09d2e18a55",
  "epoch": 171402,
  "inputs_hash": "sha256:4f77e1b0a95c3d28e6417ba0dc5931e7f2806b4ad91c5e30742fb8c1069ae3d4",
  "selected": {
    "service_version_id": "sv_01J8ZK5Q3N7C2T0M4RXB9WYD",
    "service_family": "pulmo.pleural-effusion",
    "version": "3.2.1",
    "deployment_id": "dep_01JQ90A4TT",
    "deployment_role": "ACTIVE",
    "rank_key": "0|0|1|0.913|0|3.2.1"
  },
  "ranked": [
    {"service_version_id": "sv_01J8ZK5Q3N7C2T0M4RXB9WYD", "version": "3.2.1", "deployment_role": "ACTIVE", "rank_key": "0|0|1|0.913|0|3.2.1"},
    {"service_version_id": "sv_01J7RM2H8F3B6D9G1K4N7QSV", "version": "3.2.0", "deployment_role": "STANDBY", "rank_key": "0|0|1|0.881|0|3.2.0"}
  ],
  "excluded": [
    {"service_version_id": "sv_01JR2M8W0K", "stage": "F3", "reason_code": "version_suspended", "detail": "mv_pulmo_effusion_unet_3_3_0 SUSPENDED 2026-01-11"}
  ],
  "reason_code": null
}
```

The body is Chapter 6's `Outcome` (`MOS-REG-110`): `ranked[]`, `excluded[]` with per-candidate stage and reason code, `rank_key` per candidate, `snapshot_id` and `epoch`. Chapter 6 owns the payload; this chapter owns only the path, verb and permission. `ResolutionPreview` is deleted as a type name.

### 10.6. The event stream (SSE)

**MOS-API-059** — `GET /api/v1/jobs/{job_id}/events` MUST serve `text/event-stream; charset=utf-8` with `Cache-Control: no-store` and MUST NOT buffer. If the job does not exist or is not readable, the server MUST answer with a problem document **before** upgrading to the stream (`404` or `403`), never with an empty `200` stream.

**MOS-API-060** — The stream MUST be projected from the `JobEvent` rows in PostgreSQL. It MUST NOT be projected from the message bus. PostgreSQL is the sole source of truth for job state (spine §4); a stream built on the bus would reorder `RUNNING` after `COMPLETED` under ordinary consumer lag.

**MOS-API-061** — The SSE `id:` field MUST be the per-job monotonic `sequence` integer, starting at 1 with no gaps. The `event:` field MUST be the `event_type` from the shared registry (table 10.7-A). The `data:` field MUST be one line of compact JSON carrying the full event envelope.

**MOS-API-062** — On connect, the server MUST replay the retained history from `Last-Event-ID + 1`, or from sequence 1 when the header is absent, before streaming live events. A client that connects **after** the job has already terminated MUST still receive every event including the terminal one. There is therefore no race between the `202` from `POST /api/v1/jobs` and the stream connect, and `wait()` (§10.10) needs no pre-poll.

**MOS-API-063** — The server MUST emit `retry: 3000` as the first line of the stream, MUST emit a comment heartbeat at least every 15 seconds while the job is non-terminal, and MUST close the connection after emitting a terminal event.

**MOS-API-064** — Terminal event types are `job.completed`, `job.failed`, `job.rejected` and `job.cancelled`. A client MUST NOT reconnect after receiving one; a server receiving a reconnect for a terminal job MUST replay from `Last-Event-ID` and close immediately.

**MOS-API-065** — A complete stream:

```text
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store
MedicalOS-Trace-Id: 4bf92f3577b34da6a3ce929d0e0e4736

retry: 3000

id: 1
event: job.requested
data: {"event_id":"evt_01J9F3M2KA1B3C5D7E9F1G3H","event_type":"job.requested","event_schema_version":1,"occurred_at":"2026-09-13T09:14:02.113Z","tenant_id":"tnt_stmarys","sequence":1,"subject":{"kind":"job","id":"job_01J9F3M2K8QWERTY0ABCDEF"},"data":{"status":"CREATED","phase":"admitted","steps_completed":0,"steps_total":5},"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"}

id: 2
event: job.started
data: {"event_id":"evt_01J9F3M7PB2C4D6E8F0G2H4J","event_type":"job.started","event_schema_version":1,"occurred_at":"2026-09-13T09:14:07.903Z","tenant_id":"tnt_stmarys","sequence":2,"subject":{"kind":"job","id":"job_01J9F3M2K8QWERTY0ABCDEF"},"data":{"status":"RUNNING","phase":"retrieving","steps_completed":0,"steps_total":5},"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"}

: heartbeat 2026-09-13T09:14:22.903Z

id: 5
event: job.progress
data: {"event_id":"evt_01J9F3PQ8D5E7F9G1H3J5K7L","event_type":"job.progress","event_schema_version":1,"occurred_at":"2026-09-13T09:17:31.220Z","tenant_id":"tnt_stmarys","sequence":5,"subject":{"kind":"job","id":"job_01J9F3M2K8QWERTY0ABCDEF"},"data":{"status":"RUNNING","phase":"inference","steps_completed":2,"steps_total":5},"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"}

id: 8
event: result.created
data: {"event_id":"evt_01J9F3R2M6N8P0Q2R4S6T8UV","event_type":"result.created","event_schema_version":1,"occurred_at":"2026-09-13T09:21:44.309Z","tenant_id":"tnt_stmarys","sequence":8,"subject":{"kind":"result","id":"res_01J9F3M4Q9K1M3P5R7T9V2XZ"},"data":{"result_id":"res_01J9F3M4Q9K1M3P5R7T9V2XZ","job_id":"job_01J9F3M2K8QWERTY0ABCDEF","result_kind":"segmentation","dicom_object_count":2},"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"}

id: 9
event: job.completed
data: {"event_id":"evt_01J9F3R4V8W0X2Y4Z6A8B0CD","event_type":"job.completed","event_schema_version":1,"occurred_at":"2026-09-13T09:21:44.502Z","tenant_id":"tnt_stmarys","sequence":9,"subject":{"kind":"job","id":"job_01J9F3M2K8QWERTY0ABCDEF"},"data":{"status":"COMPLETED","phase":"stored","steps_completed":5,"steps_total":5,"result_ids":["res_01J9F3M4Q9K1M3P5R7T9V2XZ"]},"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736"}
```

**MOS-API-066** — Clients MUST ignore an unrecognised `event:` type rather than erroring, and MUST discard any event whose `sequence` is not strictly greater than the highest sequence already applied for that subject. This is the only ordering guarantee that survives a reconnect or a duplicate delivery.

**MOS-API-067** — `JobEvent` retention MUST be at least 30 days, so replay from `Last-Event-ID` is satisfiable for any job a client can still read.

### 10.7. Events and webhooks

#### 10.7.1. One event namespace

The previous specification had two namespaces that already disagreed: five webhook event names in §76 and eight bus topics in §23, with `result.created` having no topic and `job.requested` having no webhook. They would have diverged further with every release.

**MOS-API-068** — There MUST be exactly one `event_type` registry. The SSE stream, webhook deliveries and the message bus MUST all use the same `event_type` values and the same envelope. A topic is transport routing; `event_type` is semantics. Consumers MUST branch on `event_type`, never on the topic they received the message from.

**MOS-API-069** — The event envelope, identical on all three transports. There is no execution-specific, bus-specific or webhook-specific variant, and no additional members:

| Member | Type | Meaning |
|---|---|---|
| `event_id` | string | Globally unique; the deduplication key for receivers |
| `event_type` | string | A row of table 10.7-A; **open** enum |
| `event_schema_version` | integer | Version of the `data` shape for this `event_type`; starts at 1 |
| `occurred_at` | RFC 3339 | When the state change committed in PostgreSQL |
| `tenant_id` | string | Owning tenant |
| `sequence` | integer | Monotonic per `subject.id`, gapless, starting at 1 |
| `subject` | `{kind, id}` | `kind` ∈ `job`, `result`, `result_review`, `service_version`, `deployment`, `evaluation_run`, `validation_report`, `dataset_version`, `study`, `provenance_record`, `webhook_endpoint` |
| `data` | object | Type-specific, PHI-free per MOS-API-006 |
| `trace_id` | string | W3C trace id of the causing operation |

Bus producers additionally set the Kafka record key `<tenant_id>:<study_instance_uid>` and the W3C `traceparent` as a record header; neither is an envelope member.

**Table 10.7-A — the event registry**

| `event_type` | Emitted when | Bus topic | Webhook | SSE |
|---|---|---|---|---|
| `job.requested` | `Job` row committed in `CREATED` | `medicalos.jobs.requested` | yes | yes |
| `job.started` | first transition to `RUNNING` | — | yes | yes |
| `job.progress` | `phase` or `steps_completed` changes | — | no | yes |
| `job.step.completed` | a `JobStep` row reaches a terminal state | — | no | yes |
| `job.dispatch` | job handed to a service work inbox | `medicalos.svc.<service_id>.work` | no | no |
| `queue.lease_expired` | a runner lease expired and the job was reclaimed | `medicalos.events.system` | no | yes |
| `job.completed` | terminal `COMPLETED` | `medicalos.jobs.completed` | yes | yes |
| `job.rejected` | terminal `REJECTED` | `medicalos.jobs.completed` | yes | yes |
| `job.failed` | terminal `FAILED` | `medicalos.jobs.failed` | yes | yes |
| `job.cancelled` | terminal `CANCELLED` — **reserved, not emitted in 0.1–0.2** | `medicalos.jobs.completed` | yes | yes |
| `result.created` | `Result` row and all DICOM objects committed | `medicalos.events.system` | yes | yes |
| `result.review.created` | platform creates the `PENDING` row on `Result` creation (`MOS-SAFE-059`) | `medicalos.events.system` | yes | no |
| `result.review.claimed` | `PENDING`→`IN_REVIEW` | `medicalos.events.system` | yes | no |
| `result.review.released` | `IN_REVIEW`→`PENDING` | `medicalos.events.system` | yes | no |
| `result.review.submitted` | `IN_REVIEW`→`ACCEPTED`/`MODIFIED`/`REJECTED` | `medicalos.events.system` | yes | no |
| `result.review.expired` | `review_due_at` elapsed | `medicalos.events.system` | yes | no |
| `result.review.reopened` | new round opened | `medicalos.events.system` | yes | no |
| `result.review.superseded` | the `Result` was superseded | `medicalos.events.system` | yes | no |
| `service_version.registered` | `ServiceVersion` published | `medicalos.events.system` | yes | no |
| `service_version.regulatory_status.expired` | `certificate_valid_until` passed (`MOS-SAFE-026`) | `medicalos.events.system` | yes | no |
| `deployment.updated` | `Deployment` created, patched, promoted, suspended or retired | `medicalos.events.system` | yes | no |
| `deployment.clinical_use_mode.promoted` | E1 gate passed (`MOS-SAFE-037`) | `medicalos.events.system` | yes | no |
| `deployment.clinical_use_mode.demoted` | automatic or operator demotion (`MOS-SAFE-027`) | `medicalos.events.system` | yes | no |
| `deployment.no_research_destination` | Deployment created with no `accepts_research` destination (`MOS-SAFE-040`) | `medicalos.events.system` | yes | no |
| `dataset_version.sealed` | `DatasetVersion` sealed | `medicalos.events.system` | yes | no |
| `evaluation_run.completed` | `EvaluationRun` terminal | `medicalos.events.system` | yes | no |
| `validation_report.published` | report signed and stored | `medicalos.events.system` | yes | no |
| `validation_report.approved` | named human approver recorded | `medicalos.events.system` | yes | no |
| `provenance.chain.broken` | daily hash-chain verification failed (`MOS-SAFE-091`) | `medicalos.events.system` | yes | no |
| `study.receiving` | `study_ingest` enters `RECEIVING` (`MOS-DATA-057`) | `medicalos.events.system` | yes | no |
| `study.stable` | `study_ingest` enters `STABLE` (`MOS-DATA-057`) | `medicalos.events.system` | yes | no |
| `study.triaged` | `study_ingest` enters `TRIAGED` (`MOS-DATA-057`) | `medicalos.events.system` | yes | no |
| `study.quarantined` | `study_ingest` enters `QUARANTINED` (`MOS-DATA-057`) | `medicalos.events.system` | yes | no |
| `webhook.endpoint_suspended` | endpoint suspended after repeated failure | `medicalos.events.system` | no | no |

**MOS-API-070** — `job.rejected` rides `medicalos.jobs.completed` because `REJECTED` is a terminal **non-error** outcome and the spine fixes the lifecycle topic set at three. It MUST NOT be published to `medicalos.jobs.failed`. A consumer that treats the `.failed` topic as "everything that did not succeed" is wrong by construction; MOS-API-068 is the rule that prevents it.

**MOS-API-071** — Adding an `event_type` requires adding a row to table 10.7-A and its `data` schema to `medos/schemas/`. A code path emitting an unregistered `event_type` MUST fail the acceptance check in §10.11 item 8. Chapters 3, 5, 6, 7, 9 and 13 decide *when* an event fires; the name it travels under and the envelope it travels in are this table's.

**MOS-API-072** — `AuditEvent` records are **not** webhook events. They are published to `medicalos.events.audit` and read through `GET /api/v1/audit`. Audit is not a notification channel.

#### 10.7.2. Webhook delivery

**MOS-API-073** — Webhook delivery MUST be driven from the persisted event log in PostgreSQL, not from the message bus. This is what makes the single namespace physically inseparable: a webhook cannot be delivered for an event the bus never carried, and an event cannot be added to the bus without a registry row.

**MOS-API-074** — Endpoint creation:

```json
{
  "url": "https://ris.stmarys.example/hooks/medicalos",
  "event_types": ["job.completed", "job.rejected", "job.failed", "result.review.submitted"],
  "description": "RIS worklist updater",
  "active": true
}
```

`event_types` MAY use a single trailing wildcard segment (`job.*`). An empty array MUST be rejected with `400` code `NO_EVENT_TYPES`. The response returns `secret` exactly once, prefixed `whsec_`, 32 bytes of CSPRNG output base64url-encoded; the platform MUST store only a hash-equivalent it can use for signing and MUST NOT return it again.

**MOS-API-075** — The `url` MUST use `https` when the tenant has any `Deployment` in `clinical_use_mode: clinical`. The dispatcher MUST NOT follow redirects, MUST reject URLs resolving to loopback, link-local (`169.254.0.0/16`, including `169.254.169.254`), or unique-local addresses unless the tenant explicitly allowlists the CIDR, and MUST cap the response body it reads at 8 KiB. At most 20 endpoints per tenant.

**MOS-API-076** — Request headers on every delivery:

```http
POST /hooks/medicalos HTTP/1.1
Host: ris.stmarys.example
Content-Type: application/json; charset=utf-8
User-Agent: MedicalOS-Webhooks/0.2.0
MedicalOS-Webhook-Id: whk_01J8T4C7E9G1J3L5N7Q9S1UW
MedicalOS-Delivery-Id: dlv_01J9F3R5X7Z9B1D3F5H7K9MN
MedicalOS-Delivery-Attempt: 3
MedicalOS-Event-Id: evt_01J9F3R4V8W0X2Y4Z6A8B0CD
MedicalOS-Event-Type: job.completed
MedicalOS-Signature: t=1757755304,v1=9f1c8a2b4d6e0f1325476981acbdef0213547698badcfe0123456789abcdef01,v1=3ab7f0c95e2148d6b7a0c3e5f90124687dacbe0135792468acef013579bdf24
```

**MOS-API-077** — The signature base string MUST be the UTF-8 concatenation `"<t>" + "." + <raw response body bytes>`, where `t` is the integer Unix seconds also carried in the header. `v1` MUST be HMAC-SHA256 over that base string, keyed by the endpoint secret, rendered lowercase hex. During the 24-hour rotation window both the new and the old secret MUST produce a `v1` value and both MUST appear in the header.

**MOS-API-078** — A receiver MUST: recompute the HMAC over the *raw* body (never a re-serialised one), compare in constant time against each `v1` value, reject when `|now − t| > 300` seconds, and deduplicate on `MedicalOS-Event-Id`. Deliveries are NOT ordered; a receiver MUST use the envelope `sequence` per `subject.id` to discard stale state.

**MOS-API-079** — The delivery body is the envelope of MOS-API-069. There is no separate webhook schema:

```json
{
  "event_id": "evt_01J9F3R4V8W0X2Y4Z6A8B0CD",
  "event_type": "job.completed",
  "event_schema_version": 1,
  "occurred_at": "2026-09-13T09:21:44.502Z",
  "tenant_id": "tnt_stmarys",
  "sequence": 9,
  "subject": {"kind": "job", "id": "job_01J9F3M2K8QWERTY0ABCDEF"},
  "data": {
    "job_id": "job_01J9F3M2K8QWERTY0ABCDEF",
    "status": "COMPLETED",
    "service_version_id": "sv_01J8ZK5Q3N7C2T0M4RXB9WYD",
    "capability_id": "pleural_effusion",
    "study_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.109",
    "result_ids": ["res_01J9F3M4Q9K1M3P5R7T9V2XZ"],
    "clinical_use_mode": "research_only"
  },
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

**MOS-API-080** — Delivery succeeds on any `2xx` received within a 10-second response timeout. Payloads MUST NOT exceed 256 KiB; an event whose `data` would exceed that MUST be truncated to identifiers plus a `truncated: true` member.

**Table 10.7-B — retry schedule** (delays measured from the previous attempt, each with full jitter up to ±20 %)

| Attempt | Delay | Cumulative |
|---|---|---|
| 1 | 0 s | 0 s |
| 2 | 10 s | 10 s |
| 3 | 30 s | 40 s |
| 4 | 2 min | 2 min 40 s |
| 5 | 5 min | 7 min 40 s |
| 6 | 15 min | 22 min 40 s |
| 7 | 1 h | 1 h 22 min |
| 8 | 3 h | 4 h 22 min |
| 9 | 6 h | 10 h 22 min |

**MOS-API-081** — A `410 Gone` from the receiver MUST disable the endpoint immediately with no further attempts. A `429` or `503` carrying `Retry-After` MUST be honoured when it is ≤ 1 hour, overriding the table for that attempt only. After attempt 9 the delivery MUST be marked `EXHAUSTED`. After 20 consecutive exhausted deliveries the endpoint MUST move to `SUSPENDED`, and `webhook.endpoint_suspended` MUST be emitted to `medicalos.events.system`.

**MOS-API-082** — Replay. `POST /api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}/replay` MUST create a new `MedicalOS-Delivery-Id` while preserving the original `MedicalOS-Event-Id`, so a correctly-implemented receiver deduplicates it. Bulk replay:

```json
{
  "from": "2026-09-13T00:00:00.000Z",
  "to": "2026-09-13T12:00:00.000Z",
  "event_types": ["job.completed", "job.rejected"],
  "only_failed": true
}
```

The window MUST NOT exceed 7 days and MUST NOT expand to more than 10 000 events; either violation MUST produce `400` code `REPLAY_WINDOW_TOO_LARGE`.

**MOS-API-083** — `GET /api/v1/webhooks/{webhook_id}/deliveries` MUST expose, per delivery: `delivery_id`, `event_id`, `event_type`, `status` ∈ `PENDING|DELIVERED|RETRYING|EXHAUSTED|DISABLED`, `attempt_count`, `last_attempt_at`, `next_attempt_at`, `response_status`, and the first 512 bytes of the response body. A webhook debugging session must not require server logs.

### 10.8. Schema source of truth and OpenAPI generation

**MOS-API-084** — The single source of truth for every request body, response body, event `data` payload and problem extension MUST be JSON Schema 2020-12 documents in `medos/schemas/`, one file per type, each carrying an absolute `$id` under `https://spec.medicalos.org/schemas/v1/`. Go structs, Pydantic models and the OpenAPI document are **outputs**, never inputs. No other `$id` authority is permitted, for any schema in any chapter: `https://schemas.medicalos.org/...` is retired and MUST NOT appear. CI MUST grep the repository and this specification for `$id` and `$ref` values and fail on any authority other than `https://spec.medicalos.org`, exactly as MOS-API-036 requires for `/problems/` URIs.

**MOS-API-085** — **AMENDED at specification 0.4.0.** The route table MUST be declared ~~in `medos/api/v1/routes.yaml`~~ in one registry file per PRODUCT — `medos/api/v1/routes.core.yaml` for the PACS-and-models service and `medos/api/v1/routes.train.yaml` for the model-preparation service — with one entry per row of table 10.2-B, each row in the registry of the product that OWNS it. `medos/tools/permcheck.py` MUST check each app against the registries describing what THAT app serves, which is not one file each: the PACS-and-models app (`medos.api.app:create_app`) against `routes.core.yaml`, and the model-preparation app (`medos.api.training_plane:create_training_app`), which serves the PACS-and-models routes as well as its own, against both. A registry checked against an app that does not serve it is a check nobody can act on, in either direction. The entry shape is unchanged:

```yaml
version: v1
routes:
  - operation_id: createJob
    method: POST
    path: /jobs
    summary: Create a job
    permission: job.create
    idempotency: required
    request:
      media_type: application/json
      schema: JobCreateRequest
    responses:
      "202":
        schema: Job
        headers: [Location, Retry-After, MedicalOS-Idempotency-Key]
    problems: [schema-violation, idempotency-key-conflict, rate-limited, cross-tenant-denied]

  - operation_id: streamJobEvents
    method: GET
    path: /jobs/{job_id}/events
    summary: Job lifecycle event stream
    permission: job.read
    idempotency: not_applicable
    responses:
      "200":
        media_type: text/event-stream
        schema: EventEnvelope
        headers: [Cache-Control]
    problems: [not-found, cross-tenant-denied, stream-limit-exceeded]
```

**MOS-API-086** — **AMENDED at specification 0.4.0.** A generator, `cmd/apigen`, invoked by `make api`, MUST read `medos/schemas/` and ~~`medos/api/v1/routes.yaml`~~ both registries of `MOS-API-085` and emit:

| Output | Content |
|---|---|
| `docs/api/openapi.yaml` | OpenAPI 3.1 (chosen because 3.1 uses JSON Schema 2020-12 natively, so schemas are embedded by `$ref` with no lossy translation) |
| `internal/api/types_gen.go` | Go request/response structs with JSON tags |
| `internal/api/routes_gen.go` | Handler registration bound to the `permission` field of the same row |
| `sdk/go/medicalos/types_gen.go` | Public Go SDK types |
| `sdk/python/medicalos/_models.py` | Pydantic v2 models |
| `docs/api/problems.md` | The problem-type catalogue rendered from the `problems` fields |

**Why `MOS-API-085` is amended and what the amendment does not change.** The single registry was replaced by one file per served app at commit `b39ea3b`, and the reason is stated in both registry headers and in `medos/tools/contracts.py`: `permcheck.py` checks a registry against an app, so one file could only ever be right about one of the two products. MEASURED at the split rather than argued — read against the PACS-and-models service, the model-preparation rows report as declared-but-unserved on a correct deployment. So does the converse, and it is the half a one-file-per-app reading gets wrong: the model-preparation app SERVES the PACS-and-models routes as well as its own, and checking it against `routes.train.yaml` alone reported all 28 of those routes as unregistered — true of the file and false of the product. Ownership and accountability are therefore not the same partition, and the requirement now says both. The requirement had not followed the code, so it named a file that has not existed since, and a reviewer executing it literally would record a failure against a repository doing the right thing. What is NOT changed: the entry shape, the one-row-per-table-10.2-B-row rule, the `permission:` binding of `MOS-API-089`, and the generator contract of `MOS-API-086` beyond the name of its input. The cut between the two files is not arbitrary and is not a matter of taste either: all 34 schemas and all 15 problem types of the single registry were referenced only by model-preparation routes, which is why `routes.core.yaml` carries no `schemas` block, and that too was measured at the split. Register entry 114 holds the prose citations that followed the old name, and the historical ones among them MUST NOT be rewritten.

**MOS-API-087** — Every generated file MUST begin with `// Code generated by medicalos-apigen. DO NOT EDIT.` (or the `#` form for Python/YAML). Hand-editing a generated file is forbidden. `docs/api/openapi.yaml` MUST NOT be authored by hand under any circumstance; the previous specification's hand-maintained, CI-validated `openapi.yaml` is deleted.

**MOS-API-088** — CI MUST run `make api && git diff --exit-code`. Any drift between the committed generated artifacts and the schemas fails the build. `CODEOWNERS` MUST route all generated paths to the API owners.

**MOS-API-089** — Because `routes_gen.go` registers each handler together with the `permission` value from the same YAML row, the permission enforced at runtime and the permission documented in OpenAPI are physically the same string. An endpoint cannot exist without a permission, and documentation cannot drift from enforcement.

**MOS-API-090** — `GET /api/v1/openapi.yaml` MUST serve the document generated for the running build and MUST be byte-identical to `docs/api/openapi.yaml` at that commit. The response MUST carry `MedicalOS-API-Build: <git sha>`.

### 10.9. Versioning and backward compatibility

**MOS-API-091** — The API major version lives in the path (`/api/v1`). Minor and patch releases of MedicalOS MUST NOT change it. At most one major version is developed at a time; when `/api/v2` ships, `/api/v1` MUST remain served for at least 12 months.

**MOS-API-092** — Within a major version, only additive changes are permitted.

**Table 10.9-A — the backward-compatibility rule**

| Allowed without a major bump | Forbidden without a major bump |
|---|---|
| Add a new endpoint | Remove or rename an endpoint |
| Add an **optional** request member | Add a required request member |
| Add a response member | Remove or rename a response member |
| Add a value to an **open** enum | Add a value to a **closed** enum |
| Add a new `problem.type` / `code` | Add a `problem.class` value, or change an existing `type`'s `class` |
| Add a new `event_type` | Remove an `event_type`, or change its `data` shape in place |
| Relax a validation constraint | Tighten a validation constraint |
| Add an optional query filter | Remove a filter, or change its operator semantics |
| Add a response header | Change a default value, or change a field's JSON type |

**MOS-API-093** — Enum classification MUST be declared in the schema with the custom annotation `x-medicalos-enum: closed | open`.

| Closed | Open |
|---|---|
| `Job.status`, `problem.class`, `target.kind`, `ResultReview.state`, `clinical_use_mode`, `execution_mode`, `Deployment.state`, `Deployment.role`, `Deployment.environment`, `result_kind` | `Job.phase`, `problem.code`, `problem.type`, `event_type`, series rejection `reason`, `resolution_reason` |

**MOS-API-094** — Clients and SDKs MUST tolerate unknown values in open enums: an unknown `phase` is rendered verbatim, an unknown `event_type` is ignored, an unknown `code` falls back to its `class`. An SDK that raises on an unknown open-enum value is non-conformant.

**MOS-API-095** — Deprecation of an endpoint or member MUST be announced with `Deprecation` and `Sunset` response headers (RFC 9745 / RFC 8594) plus `Link: <https://spec.medicalos.org/deprecations/...>; rel="deprecation"`, at least 180 days before removal, and MUST be recorded in `docs/api/CHANGELOG.md`:

```http
Deprecation: @1789123456
Sunset: Wed, 11 Mar 2027 00:00:00 GMT
Link: <https://spec.medicalos.org/deprecations/result-probability-scalar>; rel="deprecation"
```

**MOS-API-096** — Every response MUST carry `MedicalOS-API-Version: <schema date>` (for example `2026-09-13`), identifying the additive schema generation of the running build. It is informational; clients MUST NOT use it for content negotiation. There is no version request header and no date-based version pinning in 0.2.0.

### 10.10. SDKs

#### 10.10.1. Conformance rules for every first-party SDK

**MOS-API-097** — SDK model types MUST be generated (MOS-API-086). A hand-written model type in an SDK MUST fail the drift check.

**MOS-API-098** — SDKs MUST retry **only** responses of class `transport_failure` and `rate_limit`, and only for idempotent requests or requests carrying an `Idempotency-Key`. Backoff MUST be exponential with full jitter, base 500 ms, cap 30 s, at most 5 attempts, and MUST honour `Retry-After` when present. `client_error`, `authz_error`, `system_failure` and `clinical_rejection` MUST NOT be retried.

**MOS-API-099** — SDKs MUST generate a UUIDv4 `Idempotency-Key` when the caller supplies none on a required endpoint, MUST expose the value used, and MUST reuse the same key across their own internal retries. That key is the HTTP request-deduplication token only; it never becomes `Job.idempotency_key` (MOS-API-029).

**MOS-API-100** — A terminal `REJECTED` job MUST NOT raise an exception or return a non-nil error in any SDK. `wait()` returns the job in whatever terminal state it reached; only transport failure, timeout and context cancellation are raised. This is the code-level enforcement of MOS-API-039: an SDK that throws on a clinical rejection guarantees that every downstream application renders a clinical answer as a crash.

**MOS-API-101** — `class` is a reserved word in Python. The Python SDK MUST expose it as `error_class` with a Pydantic alias of `class`; the Go SDK MUST expose it as `Class`. No other member may be renamed.

**MOS-API-102** — SDKs MUST NOT log request or response bodies at any level by default, MUST NOT log the `Authorization` header or webhook secrets under any setting, and MUST redact `study_instance_uid` from any log line emitted at a level above `DEBUG`.

**MOS-API-103** — Collection accessors MUST return an auto-paginating lazy iterator that follows `next_cursor` and never exposes the cursor to the caller.

**MOS-API-104** — SDKs MUST send `User-Agent: medicalos-python/<version>` or `medicalos-go/<version>`, and MUST propagate an ambient `traceparent` when the host application has one.

**MOS-API-105** — `jobs.wait()` MUST consume the SSE stream, MUST resume with `Last-Event-ID` after a dropped connection, and MUST fall back to polling `GET /api/v1/jobs/{job_id}` at the server-advertised `Retry-After` when SSE is unavailable (a proxy that buffers, a `429 STREAM_LIMIT_EXCEEDED`).

#### 10.10.2. Python — the three flows

```python
import os
from medicalos import Client

client = Client(
    base_url="https://medicalos.stmarys.example/api/v1",
    api_key=os.environ["MEDICALOS_API_KEY"],
    timeout_s=30.0,
)

# ---- Flow 1: submit a job and await it -------------------------------------
job = client.jobs.create(
    target={"kind": "capability", "id": "pleural_effusion", "version_range": ">=3.2 <4"},
    input={"study_instance_uid": "1.2.840.113619.2.55.3.604688119.971.1547034262.109"},
    requested_outputs=["SEG", "SR"],
    idempotency_key="pilot-2026-09-13-effusion-0001",   # HTTP header only
)
print(job.job_id, job.status)            # job_01J9F3M2K8QWERTY0ABCDEF CREATED
print(job.idempotency_key)               # ik_x4hq2mtn6pkz3a7fvy5rw9cdeb — platform-derived
print(job.request_idempotency_key)       # pilot-2026-09-13-effusion-0001 — the header echo
print(job.resolved.service_version, job.resolved.resolution_reason)   # 3.2.1 deployment_state

final = client.jobs.wait(job.job_id, timeout_s=1800)   # SSE, auto-resume, poll fallback

if final.status == "COMPLETED":
    result = client.results.get(final.result_ids[0])
    bundle = client.results.bundle(final.result_ids[0])
    for finding in bundle.findings:
        print(finding.concept.code_meaning,
              [(m.value, m.unit.code) for m in finding.measurements])
    for obj in result.dicom_objects:
        print(obj.kind, obj.series_number, obj.sop_instance_uid)

elif final.status == "REJECTED":
    # Clinical outcome. Never raised. Render as information, not as failure.
    print("not analysed:", final.rejection.code, "-", final.rejection.detail)
    selection = client.jobs.series_selection(final.job_id)
    for rejected in selection.rejected:
        print("  series", rejected.series_number, rejected.reason, rejected.detail)

elif final.status == "FAILED":
    print("platform failure:", final.error.error_class, final.error.code,
          "retryable=", final.error.retryable, "trace=", final.error.trace_id)

# ---- Flow 2: register a service version ------------------------------------
sv = client.service_versions.create(
    service_id="svc_pulmoai_effusion",
    version="3.2.1",
    image_digest="sha256:9c6f0a41b7d2e83c5f0a19b4d6e82c7139fb0a5d3c8e71426ab90df3c15e8207",
    manifest_path="service.yaml",
    signature_path="service.yaml.sig",
    idempotency_key="sv-pulmoai-effusion-3.2.1",
)
print(sv.service_version_id, sv.status)              # sv_01J8ZK5Q3N7C2T0M4RXB9WYD REGISTERED
print(sv.legal_manufacturer.name, sv.legal_manufacturer.registration_number)

att = client.service_versions.attestation(sv.service_version_id)
assert att.image_digest == sv.image_digest
assert att.signature_verified is True

# ---- Flow 3: fetch a validation report and verify it offline ---------------
report = client.validation_reports.get("vr_01J9A7X2C4E6G8J0L2N4Q6SU")
print(report.capability_id, report.service_version_id, report.approved_by.name)
for criterion in report.acceptance_criteria_results:
    print(criterion.metric, criterion.threshold, criterion.observed, criterion.passed)

jws = client.validation_reports.export("vr_01J9A7X2C4E6G8J0L2N4Q6SU")   # bytes
with open("effusion-3.2.1.validation.jws", "wb") as fh:
    fh.write(jws)

from medicalos.evidence import verify_report          # performs no network I/O
verified = verify_report(jws, trust_bundle_path="/etc/medicalos/trust/publishers.pem")
assert verified.dataset_version_content_digest == report.dataset_version.content_digest
assert verified.preprocessing_spec_version_id == report.preprocessing_spec_version_id
```

**MOS-API-106** — `medicalos.evidence.verify_report` MUST verify the signature, the embedded dataset content digest and the pinned version identifiers using only the local trust bundle and the bytes of the export. It MUST NOT contact the platform. A validation report that can only be verified by asking the platform that issued it is not evidence.

#### 10.10.3. Go — the three flows

```go
package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"time"

	"github.com/medicalos/medicalos-go/medicalos"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	cl, err := medicalos.New(medicalos.Options{
		BaseURL: "https://medicalos.stmarys.example/api/v1",
		APIKey:  os.Getenv("MEDICALOS_API_KEY"),
		Timeout: 30 * time.Second,
	})
	if err != nil {
		panic(err)
	}

	// ---- Flow 1: submit a job and await it ------------------------------
	job, err := cl.Jobs.Create(ctx, medicalos.JobCreateRequest{
		Target: medicalos.JobTarget{
			Kind:         medicalos.TargetCapability,
			ID:           "pleural_effusion",
			VersionRange: ">=3.2 <4",
		},
		Input: medicalos.JobInput{
			StudyInstanceUID: "1.2.840.113619.2.55.3.604688119.971.1547034262.109",
		},
		RequestedOutputs: []string{"SEG", "SR"},
	}, medicalos.WithIdempotencyKey("pilot-2026-09-13-effusion-0001"))
	if err != nil {
		var apiErr *medicalos.APIError
		if errors.As(err, &apiErr) {
			fmt.Println(apiErr.Class, apiErr.Code, apiErr.Retryable, apiErr.TraceID)
		}
		return
	}
	fmt.Println(job.IdempotencyKey, job.RequestIdempotencyKey) // derived, then the header echo

	final, err := cl.Jobs.Wait(ctx, job.JobID) // SSE + Last-Event-ID resume
	if err != nil {
		return // transport or context only; a clinical rejection is never an error
	}

	switch final.Status {
	case medicalos.JobCompleted:
		bundle, err := cl.Results.Bundle(ctx, final.ResultIDs[0])
		if err != nil {
			return
		}
		for _, f := range bundle.Findings {
			fmt.Println(f.Concept.CodeMeaning, len(f.Measurements))
		}

	case medicalos.JobRejected:
		fmt.Println("not analysed:", final.Rejection.Code, final.Rejection.Detail)
		sel, err := cl.Jobs.SeriesSelection(ctx, final.JobID)
		if err == nil {
			for _, r := range sel.Rejected {
				fmt.Printf("  series %d %s: %s\n", r.SeriesNumber, r.Reason, r.Detail)
			}
		}

	case medicalos.JobFailed:
		fmt.Println("platform failure:", final.Error.Class, final.Error.Code,
			final.Error.Retryable, final.Error.TraceID)
	}

	// ---- Flow 2: register a service version -----------------------------
	sv, err := cl.ServiceVersions.Create(ctx, medicalos.ServiceVersionCreateRequest{
		ServiceID:     "svc_pulmoai_effusion",
		Version:       "3.2.1",
		ImageDigest:   "sha256:9c6f0a41b7d2e83c5f0a19b4d6e82c7139fb0a5d3c8e71426ab90df3c15e8207",
		ManifestPath:  "service.yaml",
		SignaturePath: "service.yaml.sig",
	}, medicalos.WithIdempotencyKey("sv-pulmoai-effusion-3.2.1"))
	if err != nil {
		return
	}
	fmt.Println(sv.ServiceVersionID, sv.Status, sv.LegalManufacturer.Name)

	// ---- Flow 3: fetch a validation report and verify it offline --------
	report, err := cl.ValidationReports.Get(ctx, "vr_01J9A7X2C4E6G8J0L2N4Q6SU")
	if err != nil {
		return
	}
	for _, c := range report.AcceptanceCriteriaResults {
		fmt.Println(c.Metric, c.Threshold, c.Observed, c.Passed)
	}

	jws, err := cl.ValidationReports.Export(ctx, report.ValidationReportID)
	if err != nil {
		return
	}
	if err := os.WriteFile("effusion-3.2.1.validation.jws", jws, 0o600); err != nil {
		return
	}
	verified, err := medicalos.VerifyReport(jws, "/etc/medicalos/trust/publishers.pem")
	if err != nil {
		return
	}
	fmt.Println(verified.DatasetVersionContentDigest == report.DatasetVersion.ContentDigest)
}
```

**MOS-API-107** — `*medicalos.APIError` MUST implement `error`, MUST expose `Status`, `Class`, `Code`, `Detail`, `Retryable`, `TraceID` and `Problem` (the raw document), and MUST be discoverable through `errors.As`. Sentinel errors `medicalos.ErrNotFound`, `medicalos.ErrForbidden` and `medicalos.ErrConflict` MUST be wrapped so `errors.Is` works.

**MOS-API-108** — Every Go SDK method MUST take `context.Context` as its first parameter and MUST honour cancellation, including inside `Jobs.Wait`.

**MOS-API-109** — Pagination in Go MUST be an iterator, not a slice:

```go
it := cl.Jobs.List(ctx, medicalos.JobFilter{
	StatusIn:     []medicalos.JobStatus{medicalos.JobFailed, medicalos.JobRejected},
	CreatedAtGTE: time.Date(2026, 9, 1, 0, 0, 0, 0, time.UTC),
})
for it.Next() {
	j := it.Value()
	fmt.Println(j.JobID, j.Status, j.Phase)
}
if err := it.Err(); err != nil {
	return err
}
```

**MOS-API-110** — Both SDKs MUST ship a webhook-signature verifier so that receivers never hand-roll the HMAC: `medicalos.webhooks.verify(raw_body, headers, secrets, tolerance_s=300)` in Python and `medicalos.VerifyWebhook(rawBody []byte, header http.Header, secrets []string, tolerance time.Duration) (*medicalos.EventEnvelope, error)` in Go. Both MUST take the **raw** body, MUST compare in constant time, and MUST reject on timestamp skew before parsing the JSON.

### Acceptance criteria

A reviewer or CI job can execute each of these against a running 0.2.0 build and a checkout of the repository.

1. **One job endpoint.** `grep -rn "agents/[^/]*/execute\|/api/v1/ai/jobs" --include=*.go --include=*.py --include=*.yaml .` returns no route definition or SDK call site. `medos/api/v1/routes.core.yaml` contains exactly one route whose effect is creating a `Job`, and its `method`/`path` are `POST /jobs`.
2. **Removed paths answer correctly.** `POST /api/v1/agents/x/execute` and `POST /api/v1/ai/jobs` each return `410`, `application/problem+json`, `class: "client_error"`, `code: "ENDPOINT_REMOVED"`, and `replaced_by: "POST /api/v1/jobs"`.
3. **Route table completeness.** Every route registered by `routes_gen.go` appears in `docs/api/openapi.yaml`, and every OpenAPI operation has a non-empty `permission` in the route registry of its own product (`MOS-API-085`). A test that registers a handler without a registry row fails the build. `medos/api/v1/routes.core.yaml` contains no `DELETE /deployments/{deployment_id}` and no `POST /result-reviews` (MOS-API-054b, MOS-API-011b).
4. **Cancel is reserved; retry is not.** `POST /api/v1/jobs/{any}/cancel` returns `501` with `code: "ENDPOINT_RESERVED"` on a 0.2.x build. `POST /api/v1/jobs/{job_id}/retry` on a `COMPLETED` job returns `409` `JOB_NOT_RETRYABLE`; on a `FAILED` job it returns `202` and the returned job's `resolved` block and `idempotency_key` are byte-identical to the original.
5. **Every error is a problem document.** A fuzz pass over all 101 routes of table 10.2-B with malformed bodies, bad credentials, unknown ids and oversized payloads produces `Content-Type: application/problem+json` on 100 % of non-2xx responses, each with `class`, `code`, `retryable`, `trace_id` and `occurred_at` present, and `class` drawn from the six values of table 10.4-A. Every `type` in every response and in `docs/api/problems.md` is prefixed `https://spec.medicalos.org/problems/`.
6. **Clinical rejection is not a failure.** Submitting a job for a study whose only series are a localizer and a dose-report SR yields `202`, then a `Job` with `status: "REJECTED"`, `error: null`, and `rejection.class: "clinical_rejection"`. The Python SDK's `jobs.wait()` returns that job without raising, and the Go SDK's `Jobs.Wait` returns a nil error.
7. **Series selection is data.** For that same job, `GET /api/v1/jobs/{job_id}/series-selection` returns a `rejected[]` entry for every series in the study, each with a `reason` from Chapter 3's code list.
8. **One event namespace.** The union of `event_type` values emitted on SSE, delivered by webhooks and produced to the bus over a full end-to-end run equals the set in table 10.7-A; no code path emits a type absent from the registry. `job.rejected` is observed on `medicalos.jobs.completed` and never on `medicalos.jobs.failed`. Every event on every transport validates against the single envelope of MOS-API-069, with no additional members.
9. **Idempotency.** `POST /api/v1/jobs` twice with the same `Idempotency-Key` and identical bodies yields one `Job` row, the second response carrying `MedicalOS-Idempotent-Replay: true`. The same key with a changed `study_instance_uid` yields `409` `IDEMPOTENCY_KEY_CONFLICT`. Two concurrent requests with the same key yield exactly one `202` and one `409` `IDEMPOTENCY_KEY_IN_FLIGHT`. In every returned `Job`, `request_idempotency_key` equals the header the client sent and `idempotency_key` matches `^ik_[a-z2-7]{26}$` and was never supplied by the client (MOS-API-029, MOS-API-030).
10. **SSE replay has no race.** Submit a job, wait for it to reach a terminal state, then connect to `GET /api/v1/jobs/{job_id}/events` with no `Last-Event-ID`: the client receives sequences 1..N ending in a terminal event, then the connection closes. Reconnecting with `Last-Event-ID: 3` yields sequences 4..N.
11. **No hand-maintained OpenAPI.** `make api && git diff --exit-code` is clean on a fresh checkout. Deleting one line from `docs/api/openapi.yaml` and re-running it fails. Every generated file's first line matches `Code generated by medicalos-apigen. DO NOT EDIT.`
12. **Docs match enforcement.** For every route, the permission string in `docs/api/openapi.yaml` equals the permission the middleware checks at runtime, verified by a test that calls each route with a credential lacking exactly that permission and asserts `403`. Every permission named in table 10.2-A resolves in Chapter 8's catalogue.
13. **Webhook signature.** A delivery to a test receiver verifies: `MedicalOS-Signature` parses to a `t` within 300 s and at least one `v1`; HMAC-SHA256 of `"<t>.<raw body>"` under the endpoint secret equals a `v1` value. Mutating one byte of the body fails verification. During a rotation window two distinct `v1` values are present and each secret verifies one of them.
14. **Webhook retry and replay.** A receiver returning `500` produces 9 attempts with the delays of table 10.7-B (±20 % jitter), then `status: "EXHAUSTED"`. A receiver returning `410` produces exactly 1 attempt and a disabled endpoint. Replaying an exhausted delivery produces a new `MedicalOS-Delivery-Id` and the original `MedicalOS-Event-Id`.
15. **Pagination.** Listing 500 jobs at `limit=50` returns 10 pages, no duplicate `job_id` across pages, no `total` member in any envelope, and reusing a page-3 cursor with a changed `status__in` filter returns `400` `CURSOR_FILTER_MISMATCH`.
16. **No PHI on the wire.** A test corpus of studies carrying distinctive `PatientName`, `PatientID` and `AccessionNumber` values is processed end to end; grepping the recorded HTTP paths, query strings, problem documents, SSE payloads and webhook bodies for those values returns zero matches, while `StudyInstanceUID` is present as expected.
17. **Backward compatibility.** `apigen --check-compat <previous tag>` compares the committed schemas against the previous release and fails on any row in the forbidden column of table 10.9-A: a removed member, a tightened constraint, a changed JSON type, or a new value in an `x-medicalos-enum: closed` enum.
18. **SDK conformance.** The Python and Go SDK test suites run against a mock server that returns, for each of the six `class` values, the worked example of §10.4: retries occur only for `rate_limit` and `transport_failure`, the same `Idempotency-Key` is reused on every retry, no SDK raises on `clinical_rejection`, and each SDK ignores an injected unknown `event_type` and an unknown `phase` without error.
19. **Research visibility.** `GET /api/v1/results?include_research=true` as a principal without `result.read.research` returns zero `research_only` items and `200`, never `403`; with the permission, the research items appear and every item in both responses carries `clinical_use_mode` (MOS-API-019a, Chapter 9 `MOS-SAFE-041`).
20. **One schema namespace.** `grep -rn "schemas\.medicalos\.org" .` over the repository and over this specification returns nothing; every `$id` in `medos/schemas/` begins `https://spec.medicalos.org/schemas/v1/`, and every `$ref` in every schema and in every chapter resolves under that authority to a file in `medos/schemas/` (MOS-API-084).

---

[← 9. Clinical Safety, Regulatory Posture and Provenance](09-clinical-safety.md) · [Index](../../MEDICALOS_SPEC.md) · [11. Agentic Layer, Chat and LLM Integration →](11-agentic-layer.md)
