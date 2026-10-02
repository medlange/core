<!-- MedicalOS Specification v0.4.0 — chapter 13 of 19. Normative.
     114 requirements. Do not edit without a requirement-ID review. -->

[← 12. Data Model and Storage](12-data-model.md) · [Index](../../MEDICALOS_SPEC.md) · [14. Testing and Acceptance →](14-testing.md)

---

## 13. Observability, Deployment and Scaling

This chapter specifies what MedicalOS emits about itself, how it is deployed, and how it absorbs load. It covers three things the previous version left as headings: a metric catalogue with real names and labels, a telemetry redaction contract that makes the PHI rule of spine §8 enforceable rather than aspirational, and a GPU residency contract that says what actually happens when more models are deployed than fit on the card.

**Vocabulary warning.** "Deployment" in this chapter means *infrastructure topology* — which processes run where, in what container, with what probes. The entity `Deployment` (a `ServiceVersion` made active for a tenant in an environment, with `role`, `state`, `clinical_use_mode` and `residency` class) is defined in Chapter 6 and is never redefined here. Where this chapter needs it, it is written `Deployment` in code font and cross-referenced.

**Telemetry is derived, never authoritative.** PostgreSQL is the sole source of truth for job state (MOS-EXEC-002). Metrics, logs and traces are lossy, sampled and droppable by design. No component may read them back to make a decision, and no operator procedure may reconstruct state from them.

- **MOS-OPS-001** — Metrics, logs and traces MUST NOT be used as an input to any control decision (scheduling, retry, quota, gating, state reconstruction). The one permitted exception is horizontal autoscaling (§13.11), which is advisory and whose failure degrades throughput only.
- **MOS-OPS-002** — Every MedicalOS-authored process MUST expose `/healthz`, `/readyz`, `/startupz`, `/metrics` and `/version` (§13.3) and MUST emit structured JSON logs (§13.5) and OTLP traces (§13.6) **from its first commit**, before any business handler exists. CI MUST fail a build that adds a new service directory under `medos/services/` whose binary does not answer all five endpoints in the service skeleton test. This closes the contradiction in the previous version between a Definition of Done requiring metrics and a build plan adding observability at step 18 of 20.
- **MOS-OPS-003** — The telemetry stack MUST be self-hostable with no mandatory proprietary cloud dependency: OpenTelemetry Collector, Prometheus, Tempo, Loki, Grafana. A hosted backend MAY be configured per site as an additional exporter on the Collector; no MedicalOS process may hold credentials for it (MOS-OPS-064).

### 13.1. Process inventory

These are the only MedicalOS-authored processes. Everything else in a deployment is third-party.

| process | image | owns | GPU | stateless | scaling signal |
|---|---|---|---|---|---|
| `medicalos-api` | `ghcr.io/medicalos/api` | `/api/v1`, triage, `SeriesSelector` evaluation, capability resolution, job creation, SSE events, webhooks, admission control | no | yes | API request rate |
| `medicalos-gateway` | `ghcr.io/medicalos/gateway` | DICOMweb proxy; holds the ONLY PACS credential; tenancy filter; de-identification; UID mapping table; pseudoref resolution | no | yes | retrieved bytes/s |
| `medicalos-worker` | `ghcr.io/medicalos/worker` | job claim/lease/heartbeat, canonical volume build, `ResultBundle` validation, geometry inversion, DICOM SEG/SR/SC writing, STOW, terminal transitions | no | yes | `medicalos_queue_depth` |
| `medicalos-tritond` | `ghcr.io/medicalos/tritond` | GPU residency manager; sole holder of Triton's model-control credential; one per GPU node | co-resident | no (node-pinned) | fixed: 1 per GPU node |
| `svc-<service_id>` | vendor or first-party | a deployed `ServiceVersion`: series→volume consumption, preprocessing, inference, postprocessing, `ResultBundle` production | sealed: yes; native: no | yes | per-service queue depth |

Third-party processes in a standard deployment: `postgres`, `minio`, `orthanc` (or the site PACS), `ohif`, `triton`, `otel-collector`, `prometheus`, `tempo`, `loki`, `grafana`, and — from 0.3.0 only — `kafka`.

- **MOS-OPS-004** — `medicalos-worker` MUST NOT require a GPU. All GPU work happens inside `svc-<service_id>` (sealed mode) or inside `triton` (native mode). A worker replica that cannot reach any GPU node MUST still be able to serve CPU-only capabilities such as `emphysema_laa`.
- **MOS-OPS-005** — The PACS (`orthanc` or site equivalent) MUST be reachable only from `medicalos-gateway` at the network layer, not merely by convention. In docker compose this is an `internal: true` network; in Kubernetes a `NetworkPolicy` whose only ingress peer is the gateway's pod selector. OHIF, workers and service containers MUST be configured with the gateway's DICOMweb base URL and MUST have no route to the PACS.
- **MOS-OPS-006** — `triton` MUST NOT be exposed outside its node's network. Its only permitted clients are the co-resident `medicalos-tritond` (control API) and native-mode service containers scheduled on that node (inference API). Triton MUST remain ignorant of tenants, users, jobs, studies and clinical policy.
- **MOS-OPS-007** — Exactly one `medicalos-tritond` MUST run per GPU node, identified by the environment variable `MEDICALOS_NODE_ID`. No leader election protocol is required because the singleton is a deployment property; `tritond` MUST refuse to start if `GET /internal/v1/residency/state` on its own `MEDICALOS_TRITOND_ADDR` already answers from another process.
- **MOS-OPS-008** — Whether Triton is replaceable the way the PACS is replaceable is **not settled by this chapter**. §13.10 specifies Triton management concretely because a single Triton is in the shipped topology from 0.1.0 (spine §14) and `native` mode is defined as inference on shared Triton (spine §2); the `InferenceBackend` seam that would make it replaceable is raised as open question OQ-10 in Chapter 16 (`MOS-OPEN-031`, decision due at gate `G-0.3.0`) and MUST NOT be treated as resolved by any chapter.
- **MOS-OPS-113** — This chapter defines the contents of exactly two mesh-internal surfaces and no others: `/internal/v1/residency/...` on `medicalos-tritond` (§13.10.3) and `POST /internal/v1/pseudoref/resolve` on `medicalos-gateway` (MOS-OPS-037). `/internal/v1/...` is not public API, and Chapter 10 owns its boundary (`MOS-API-001a`), under which the surface MUST be bound to the mesh interface only, MUST authenticate by workload identity or a component-scoped key rather than a tenant credential, MUST NOT be reachable from the edge zone `Z-EDGE` (Chapter 8), and MUST NOT appear in the generated OpenAPI document or in either SDK. Where such a call additionally acts for a human — `POST /internal/v1/pseudoref/resolve` — the `phi.reidentify` check of MOS-OPS-037 is in addition to that workload authentication, never instead of it. No `svc-*` container may reach either surface; this is the MOS-SVC ownership boundary of MOS-OPS-060 restated for the internal control plane.

### 13.2. Configuration contract

- **MOS-OPS-009** — All configuration MUST arrive as environment variables, or as files at paths named by environment variables. There is no configuration file discovered by convention, no service-discovery client, and no configuration API.
- **MOS-OPS-010** — Every environment variable MUST be read exactly once, at startup, into a typed config struct that is validated before any listener binds. A process whose config fails validation MUST exit non-zero with a message naming every offending variable. Runtime re-read of configuration is forbidden; a configuration change is a restart.
- **MOS-OPS-011** — Secrets MUST be supplied as file paths (`MEDICALOS_DB_PASSWORD_FILE`, `MEDICALOS_PACS_PASSWORD_FILE`, `MEDICALOS_TENANT_PSEUDONYM_KEY_FILE`), never as inline values, so that neither `docker inspect` nor a pod spec dump reveals them. Chapter 8 owns the secret store.

Load-bearing variables shared by all MedicalOS processes:

| variable | example | meaning |
|---|---|---|
| `MEDICALOS_ENV` | `prod` | `dev` \| `staging` \| `prod`; becomes `deployment.environment.name` |
| `MEDICALOS_HTTP_ADDR` | `:8080` | business port |
| `MEDICALOS_METRICS_ADDR` | `:9090` | metrics/admin port, separate listener |
| `MEDICALOS_DB_DSN` | `postgres://medicalos@postgres:5432/medicalos?sslmode=verify-full` | password injected from `MEDICALOS_DB_PASSWORD_FILE` |
| `MEDICALOS_JOBQUEUE_DRIVER` | `postgres` | `postgres` (0.1+) \| `kafka` (0.3+) |
| `MEDICALOS_GATEWAY_URL` | `http://medicalos-gateway:8080` | the only DICOMweb base URL any component knows |
| `MEDICALOS_PHI_GUARD_MODE` | `redact` | `strict` (panic) \| `redact`; `strict` mandatory in `dev` and CI |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | the only telemetry egress |
| `OTEL_SERVICE_NAME` | `medicalos-worker` | resource attribute |
| `MEDICALOS_BUILD_COMMIT` | `a3f19c2` | baked at build time, surfaced by `/version` |
| `MEDICALOS_IMAGE_DIGEST` | `sha256:71bf…` | baked at build time, surfaced by `/version`, copied into provenance |

### 13.3. Health, readiness and lifecycle endpoints

| endpoint | port | semantics | checks dependencies | failure action |
|---|---|---|---|---|
| `GET /healthz` | `MEDICALOS_HTTP_ADDR` | process liveness only | **no** | restart the container |
| `GET /readyz` | `MEDICALOS_HTTP_ADDR` | may this replica receive work now | yes, bounded | remove from rotation / stop claiming |
| `GET /startupz` | `MEDICALOS_HTTP_ADDR` | one-time initialisation complete | yes | hold off liveness probes |
| `GET /metrics` | `MEDICALOS_METRICS_ADDR` | Prometheus text exposition | no | — |
| `GET /version` | `MEDICALOS_METRICS_ADDR` | build identity | no | — |

- **MOS-OPS-012** — `/healthz` MUST NOT call PostgreSQL, the gateway, the object store, Triton or any other dependency. It returns `200 {"status":"ok"}` whenever the HTTP server is scheduling. A dependency check here converts a 30-second database blip into a cluster-wide restart storm.
- **MOS-OPS-013** — `/readyz` MUST check each *required* dependency with a per-dependency timeout of 200 ms and a result cache of 1 s, and MUST return `200` or `503` with a body enumerating every dependency and its state:

```json
{"status":"unready","checks":[
  {"name":"postgres","state":"ok","latency_ms":3},
  {"name":"object_store","state":"ok","latency_ms":11},
  {"name":"gateway","state":"fail","latency_ms":200,"detail":"dial timeout"},
  {"name":"preprocessing_selftest","state":"ok","detail":"3/3 pinned models verified"}]}
```

| process | required for readiness | explicitly NOT required |
|---|---|---|
| `medicalos-api` | postgres | gateway, object store, Triton, any service container |
| `medicalos-gateway` | postgres (UID map), PACS DICOMweb `GET /studies?limit=1` | object store |
| `medicalos-worker` | postgres, object store, gateway | Triton, any specific service container, queue depth |
| `medicalos-tritond` | Triton `GET /v2/health/ready`, postgres | any specific model being loaded |

- **MOS-OPS-014** — Readiness MUST NOT be a function of load. A worker with 10 000 queued jobs is ready; a worker at its in-flight concurrency limit is ready. Making saturation look like unreadiness removes the only replicas that could drain the queue.
- **MOS-OPS-015** — `/startupz` MUST return `503` until (a) the schema version the binary requires is present in `schema_migrations`, and (b) for `medicalos-worker` and native-mode service containers, the pinned `PreprocessingSpec` self-test of MOS-IMG has passed for every model the replica may serve — both of its parts, in the order `MOS-TRAIN-057` fixes: first the package-version assertion (installed `monai`, `torch` and `numpy`, and the observed `monai.config.USE_COMPILED`, against the values encoded in `backend.resampler` and `backend.numpy`), then the golden-fixture tensor-hash comparison of `MOS-IMG-054`. Either part failing is the same deployment failure through the same path: one `preprocessing_selftest_failed` event, one counter, one alert, and no separate version-mismatch outcome. A failed self-test MUST leave `/startupz` at `503` permanently, MUST log `error_class=preprocessing_selftest_failed`, and MUST NOT be retried into success — serving with preprocessing skew is worse than not serving. The `preprocessing_selftest_failed` line MUST carry, besides the mandatory fields of MOS-OPS-034, `model_id`, `model_version`, `preprocessing_version`, `selftest_stage` ∈ `version_assertion`\|`tensor_hash`, and the declared and observed `backend.resampler` and `backend.numpy` values — which for a MONAI chain means the `monai`, `torch` and `numpy` versions and `USE_COMPILED` (`MOS-TRAIN-055`, `MOS-TRAIN-057`) — plus, for a `tensor_hash` failure, the expected and observed digests, `T.shape` and `T.dtype` that `MOS-IMG-055` requires. `medicalos_preprocessing_selftest_total{outcome="fail"}` MUST be incremented once per failing model whichever part failed, so that `PreprocessingSelfTestFailed` (§13.7) fires identically for both.
- **MOS-OPS-016** — `/metrics` MUST bind to a separate listener from business traffic and MUST NOT be exposed through any ingress. It carries no PHI by construction (§13.4) but it does carry tenant identifiers and operational shape.
- **MOS-OPS-017** — `/version` MUST return exactly these fields, and the same values MUST appear in the provenance record of every job the replica executes (Chapter 9):

```json
{"service":"medicalos-worker","version":"0.2.0","git_commit":"a3f19c2d4e",
 "build_time":"2026-09-02T09:14:07Z","image_digest":"sha256:71bfa0c9e2d1f4...",
 "schema_version":"0037","go_version":"go1.24.3"}
```

- **MOS-OPS-018** — On `SIGTERM` a process MUST: stop accepting new work (workers stop calling `Claim`; HTTP servers stop accepting new connections and return `503` from `/readyz`), finish in-flight work up to `MEDICALOS_DRAIN_TIMEOUT_SECONDS`, flush the OTLP and log exporters, then exit `0`. It MUST NOT abandon an in-flight job by exiting early; if the drain timeout expires it stops heartbeating and exits, letting the lease expire per Chapter 5.
- **MOS-OPS-019** — `MEDICALOS_DRAIN_TIMEOUT_SECONDS` MUST be ≥ the largest job timeout the replica may take on, plus 60 s. The orchestrator's grace period (`terminationGracePeriodSeconds`, `stop_grace_period`) MUST be ≥ `MEDICALOS_DRAIN_TIMEOUT_SECONDS` + 10 s. For 0.2.0 with a 1800 s job timeout this is 1860 s and 1870 s respectively. A grace period shorter than the drain timeout silently converts every rolling update into a burst of lease expiries.

### 13.4. Metric catalogue

- **MOS-OPS-020** — All MedicalOS metrics MUST use the `medicalos_` prefix, Prometheus base units (seconds, bytes, ratio), and the `_total` suffix on counters. Third-party exporter metrics keep their own names and are mapped by recording rules (MOS-OPS-030).
- **MOS-OPS-021** — The following label keys are **forbidden on every metric**, without exception: `patient_id`, `patient_name`, `accession_number`, `study_instance_uid`, `series_instance_uid`, `sop_instance_uid`, `study_ref`, `series_ref`, `patient_ref`, `job_id`, `user_id`, `file_path`, `url`, `error_message`. A metric whose active series count grows with the number of studies, patients or jobs is forbidden regardless of label names.
- **MOS-OPS-022** — Every label value MUST come from a closed enum or a bounded identifier set (`tenant_id`, `service_id`, `capability`, `model_id`, `model_version`, `gpu_uuid`, `node`). Total active series per process MUST stay below 10 000; CI MUST scrape a synthetic-load instance and fail the build above that bound.

#### 13.4.1. Queue and lease

| metric | type | labels | notes |
|---|---|---|---|
| `medicalos_queue_depth` | gauge | `tenant_id`, `service_id`, `state` | `state` ∈ `queued`\|`running`; sampled from PostgreSQL every 10 s by `medicalos-api` |
| `medicalos_queue_oldest_age_seconds` | gauge | `service_id` | head-of-line wait; the autoscaling and starvation signal |
| `medicalos_queue_enqueued_total` | counter | `tenant_id`, `service_id` | |
| `medicalos_queue_claim_total` | counter | `service_id`, `outcome` | `outcome` ∈ `claimed`\|`empty`\|`not_admissible` |
| `medicalos_job_lease_expired_total` | counter | `service_id`, `reason` | `reason` ∈ `heartbeat_timeout`\|`worker_terminated`\|`drain_timeout` |
| `medicalos_job_lease_age_seconds` | histogram | `service_id` | buckets `[30,60,120,300,600,1200,1800,3600]` |

- **MOS-OPS-023** — `medicalos_job_lease_expired_total` MUST be incremented by the reconciler that performs the expiry, in the same transaction that returns the job to `QUEUED`, and MUST NOT be inferred from a heartbeat gap.

#### 13.4.2. Job outcomes — `REJECTED` is not an error

| metric | type | labels |
|---|---|---|
| `medicalos_job_terminal_total` | counter | `tenant_id`, `service_id`, `capability`, `terminal_state`, `reason_code` |
| `medicalos_job_duration_seconds` | histogram | `service_id`, `terminal_state` |
| `medicalos_job_phase_duration_seconds` | histogram | `service_id`, `phase` |
| `medicalos_job_attempt_total` | counter | `service_id`, `attempt_bucket` |

`terminal_state` ∈ `COMPLETED` \| `FAILED` \| `CANCELLED` \| `REJECTED` (MOS-EXEC-001). `reason_code` is `none` for `COMPLETED`; for `REJECTED` it is the clinical rejection enum of Chapter 5 §5.3.1; for `FAILED` it is the closed `failure.class` enum of Chapter 5 §5.3.2. Both enums are owned by Chapter 5 and are never extended here. `phase` is the open string of MOS-EXEC-001; the metric MUST map any unrecognised phase to the literal `other` to keep cardinality bounded.

- **MOS-OPS-024** — `REJECTED` MUST be reportable separately from `FAILED` everywhere. Two recording rules are mandatory and dashboards and alerts MUST consume them rather than aggregating `terminal_state` themselves:

```yaml
groups:
  - name: medicalos.jobs
    interval: 30s
    rules:
      - record: medicalos:job_error_ratio:rate30m
        expr: |
          sum by (tenant_id, service_id) (rate(medicalos_job_terminal_total{terminal_state="FAILED"}[30m]))
          /
          clamp_min(sum by (tenant_id, service_id) (
            rate(medicalos_job_terminal_total{terminal_state=~"COMPLETED|FAILED"}[30m])), 1e-9)
      - record: medicalos:job_rejection_ratio:rate30m
        expr: |
          sum by (tenant_id, service_id, reason_code) (rate(medicalos_job_terminal_total{terminal_state="REJECTED"}[30m]))
          /
          clamp_min(sum by (tenant_id, service_id) (rate(medicalos_job_terminal_total[30m])), 1e-9)
```

- **MOS-OPS-025** — `medicalos:job_error_ratio:rate30m` MUST exclude `REJECTED` from both numerator and denominator. A site whose case mix is 40 % ineligible is operating correctly; folding that into an error rate either hides real failures behind a large denominator or pages an engineer about radiology.

#### 13.4.3. Inference and GPU

| metric | type | labels | notes |
|---|---|---|---|
| `medicalos_inference_duration_seconds` | histogram | `model_id`, `model_version`, `backend` | buckets below |
| `medicalos_inference_total` | counter | `model_id`, `model_version`, `outcome` | `outcome` ∈ `ok`\|`error`\|`timeout` |
| `medicalos_inference_patches_total` | counter | `model_id`, `model_version` | sliding-window patches submitted |
| `medicalos_gpu_utilisation_ratio` | gauge | `node`, `gpu_uuid` | recording rule over DCGM |
| `medicalos_gpu_memory_used_bytes` | gauge | `node`, `gpu_uuid` | recording rule over DCGM |
| `medicalos_gpu_memory_total_bytes` | gauge | `node`, `gpu_uuid` | recording rule over DCGM |
| `medicalos_gpu_residency_budget_bytes` | gauge | `node`, `gpu_uuid` | from `tritond` |
| `medicalos_gpu_residency_committed_bytes` | gauge | `node`, `gpu_uuid` | from `tritond` |
| `medicalos_model_resident_bytes` | gauge | `node`, `gpu_uuid`, `model_id`, `model_version`, `residency_class` | |
| `medicalos_model_load_total` | counter | `model_id`, `model_version`, `outcome` | `outcome` ∈ `loaded`\|`evicted`\|`unsatisfiable`\|`load_error` |
| `medicalos_model_load_duration_seconds` | histogram | `model_id`, `model_version` | cold start |
| `medicalos_model_load_wait_seconds` | histogram | `service_id`, `model_id` | time a job waited for a residency slot |
| `medicalos_gpu_seconds_consumed_total` | counter | `tenant_id`, `node` | feeds the quota ledger |

- **MOS-OPS-026** — `medicalos_inference_duration_seconds` and `medicalos_job_duration_seconds` MUST declare explicit buckets. Prometheus' default buckets top out at 10 s, which places every 3D volumetric inference in `+Inf` and makes every latency percentile a fabrication:

```go
var inferenceBuckets = []float64{1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 600, 1200}
var jobBuckets       = []float64{5, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 3600}
```

  Native histograms MAY be enabled additionally; the explicit classic buckets MUST remain so a site without native-histogram support still gets correct percentiles.
- **MOS-OPS-027** — `medicalos_gpu_utilisation_ratio` (DCGM `DCGM_FI_DEV_GPU_UTIL`) MUST NOT be used as a capacity or scaling signal, and dashboards MUST label it "kernel residency, not occupancy". It reports the fraction of sampling intervals in which any kernel was resident; a single patch-batched 3D U-Net pins it near 1.0 while leaving memory and throughput headroom, and a memory-starved node can show 1.0 while doing nothing useful. The capacity signals are `medicalos_gpu_residency_committed_bytes / medicalos_gpu_residency_budget_bytes` and `medicalos_queue_oldest_age_seconds`.

#### 13.4.4. Medical data plane

| metric | type | labels | notes |
|---|---|---|---|
| `medicalos_gateway_request_total` | counter | `op`, `outcome`, `http_status` | `op` ∈ `qido`\|`wado`\|`stow`\|`wado_metadata` |
| `medicalos_gateway_request_duration_seconds` | histogram | `op` | |
| `medicalos_gateway_bytes_total` | counter | `op`, `direction` | `direction` ∈ `in`\|`out` |
| `medicalos_gateway_retrieval_wait_seconds` | histogram | `tenant_id` | time blocked on the concurrency semaphore (§13.12) |
| `medicalos_deid_instances_total` | counter | `tenant_id`, `profile` | |
| `medicalos_deid_duration_seconds` | histogram | `tenant_id`, `profile` | |
| `medicalos_deid_failure_total` | counter | `tenant_id`, `profile`, `failure_kind` | **critical**; see below |
| `medicalos_deid_uid_map_entries` | gauge | `tenant_id` | growth of the reversible UID mapping table |
| `medicalos_series_selection_total` | counter | `service_id`, `outcome`, `rejection_code` | `outcome` ∈ `selected`\|`rejected` |
| `medicalos_dicom_write_total` | counter | `sop_class`, `outcome` | `sop_class` ∈ `SEG`\|`SR`\|`SC`; `outcome` ∈ `stored`\|`skipped_idempotent`\|`failed` |
| `medicalos_dicom_write_failure_total` | counter | `sop_class`, `stage`, `http_status` | `stage` ∈ `encode`\|`validate`\|`stow`\|`verify` |
| `medicalos_dicom_write_duration_seconds` | histogram | `sop_class`, `stage` | |

- **MOS-OPS-028** — `failure_kind` on `medicalos_deid_failure_total` is a closed enum: `burned_in_phi_suspected`, `unmapped_uid`, `uid_map_write_failed`, `private_tag_policy_violation`, `profile_unsupported_sop_class`, `parse_error`. Every one of these is a potential PHI disclosure or a silent invalidation of previously written results, so any non-zero rate is a page (MOS-OPS-046). Chapter 3 owns the de-identification contract that produces these outcomes.
- **MOS-OPS-029** — `medicalos_dicom_write_total{outcome="skipped_idempotent"}` MUST be incremented when the QIDO-RS pre-check of MOS-IMG finds the deterministically derived series already complete. A deployment where this counter is always zero across retries is evidence that deterministic UID derivation is not working, and CI MUST assert it is non-zero in the retry acceptance test.

#### 13.4.5. Evidence, policy and platform

| metric | type | labels |
|---|---|---|
| `medicalos_validation_gate_total` | counter | `capability`, `model_id`, `model_version`, `gate`, `outcome` |
| `medicalos_evaluation_run_duration_seconds` | histogram | `dataset_version`, `model_id` |
| `medicalos_preprocessing_selftest_total` | counter | `model_id`, `model_version`, `outcome` |
| `medicalos_policy_decision_total` | counter | `decision`, `rule_id` |
| `medicalos_quota_denied_total` | counter | `tenant_id`, `quota_key` |
| `medicalos_quota_overage_seconds_total` | counter | `tenant_id` |
| `medicalos_rate_limit_throttled_total` | counter | `tenant_id`, `scope` |
| `medicalos_log_redaction_violation_total` | counter | `service`, `field` |
| `medicalos_llm_tokens_total` | counter | `tenant_id`, `provider`, `direction` |

- **MOS-OPS-030** — `outcome` on `medicalos_validation_gate_total` MUST distinguish three values, not two: `pass`, `fail`, `error`. A gate that *errored* is not a gate that *passed*, and the previous version's failure mode was precisely a gate whose implementation could never return `fail`. `gate` values are owned by Chapter 7. Deployment promotion MUST treat `error` as `fail`.
- **MOS-OPS-031** — DCGM metrics MUST be mapped into the `medicalos_` namespace by recording rules so that dashboards and alerts do not depend on the exporter's naming:

```yaml
  - name: medicalos.gpu
    interval: 15s
    rules:
      - record: medicalos_gpu_utilisation_ratio
        expr: DCGM_FI_DEV_GPU_UTIL / 100
      - record: medicalos_gpu_memory_used_bytes
        expr: DCGM_FI_DEV_FB_USED * 1024 * 1024
      - record: medicalos_gpu_memory_total_bytes
        expr: (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE) * 1024 * 1024
```

### 13.5. Structured logging and PHI redaction

- **MOS-OPS-032** — Every process MUST write logs as one JSON object per line to stdout, UTF-8, no ANSI colour in `MEDICALOS_ENV != dev`. No process writes log files, rotates logs, or ships logs itself; collection is the platform's job.
- **MOS-OPS-033** — `msg` MUST be a compile-time constant string with no interpolated values, so that log lines group. Every variable goes in a typed field.
- **MOS-OPS-034** — The following fields are mandatory on every line: `ts` (RFC 3339 with milliseconds, UTC), `level` ∈ `debug`\|`info`\|`warn`\|`error`, `msg`, `service`, `version`, `commit`, `instance`, `env`. When a tenant context exists, `tenant_id` is mandatory. When a job context exists, `job_id`, `attempt` and `phase` are mandatory. When a span is active, `trace_id` and `span_id` are mandatory. When the line describes a job failure, `error_class` MUST carry the `jobs.failure_class` value of Chapter 5 §5.3.2 verbatim — that enum is closed and owned there — and `error_code` MUST carry the corresponding `failure_code`, which is the open field. The same binding applies to the span attribute `medicalos.error.class` (MOS-OPS-047). A near-miss spelling is a defect, not a variant: a dashboard correlating `medicalos.error.class` with `jobs.failure_class` must join, and `dicom_write_failure` is not `dicom_write_failed`. Outside a job context `error_class` is open-vocabulary, and it MUST NOT reuse a Chapter 5 member for a different meaning.

```json
{"ts":"2026-09-13T12:04:31.118Z","level":"error","msg":"stow_rs rejected generated segmentation",
 "service":"medicalos-worker","version":"0.2.0","commit":"a3f19c2d4e","instance":"worker-7d9c8f-lq2xv",
 "env":"prod","tenant_id":"tnt_01J9F3KQ","job_id":"job_01J9F3XQ2M","attempt":2,"phase":"dicom_write",
 "trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7",
 "service_id":"svc_pleural_effusion","service_version":"3.2.1",
 "study_ref":"S-k1-7QK3M2XA9RBTV4ZC","series_ref":"R-k1-M4P2XN8CQJ7DLB0E",
 "error_class":"dicom_store_failed","error_code":"stow_failed_sop","http_status":409,
 "sop_class":"SEG","failed_sop_count":1}
```

#### 13.5.1. Pseudonymous references

PHI must not appear in logs, but an operator must still be able to answer "which study failed". Both are satisfied by a keyed, tenant-scoped, reversible-under-audit reference.

- **MOS-OPS-035** — Wherever a log line, span attribute or event payload would carry a `StudyInstanceUID`, `SeriesInstanceUID`, `SOPInstanceUID`, `PatientID` or `AccessionNumber`, it MUST instead carry a **pseudoref**:

```
pseudoref(kind, tenant_id, value) =
    prefix(kind) + "-" + key_id + "-" +
    base32_crockford_nopad( HMAC_SHA256( tenant_pseudonym_key, kind || 0x1F || value )[0:10] )

prefix: study -> "S", series -> "R", instance -> "I", patient -> "P", accession -> "A"
```

  `tenant_pseudonym_key` is a 32-byte per-tenant secret held by `medicalos-gateway` and distributed to other MedicalOS processes through the secret store (Chapter 8). `key_id` (`k1`, `k2`, …) prefixes the value so a key rotation does not break historical joins; rotation SHOULD be avoided because it splits log history.
- **MOS-OPS-036** — A pseudoref MUST be stable for the life of a `key_id` within a tenant and MUST NOT be comparable across tenants. It MUST NOT be derived from a truncated hash shorter than 80 bits.
- **MOS-OPS-037** — Reversal MUST be possible only through `POST /internal/v1/pseudoref/resolve` on `medicalos-gateway`, MUST require the `phi.reidentify` permission (Chapter 8), and MUST write an `AuditEvent` per resolved value. No other process may hold the reverse mapping.

#### 13.5.2. The PHI guard

An allow-list of fields is necessary but not sufficient, because PHI arrives inside error strings from DICOM libraries and HTTP clients. A second, value-level guard is mandatory.

- **MOS-OPS-038** — Every log encoder MUST pass each emitted string value through `phiguard.Scan` before serialisation. `phiguard.Scan` matches:

| pattern | regex | rationale |
|---|---|---|
| DICOM UID | `\b\d{1,5}(\.\d{1,12}){4,}\b` | ≥ 5 components; chosen so semver (`3.2.1`) and spacing (`0.703`) never match |
| DICOM PN | `[A-Za-z' -]{1,64}\^[A-Za-z' -]{1,64}(\^[A-Za-z' -]{0,64}){0,3}` | caret-delimited person name |
| DICOM DA | `\b(19\|20)\d{2}(0[1-9]\|1[0-2])(0[1-9]\|[12]\d\|3[01])\b` | 8-digit date |
| tenant PatientID formats | per-tenant configured regex list | site-specific MRN shapes |

- **MOS-OPS-039** — On a match, the guard MUST replace the entire field value with `"[redacted:phi_pattern]"`, increment `medicalos_log_redaction_violation_total{service,field}`, and — when `MEDICALOS_PHI_GUARD_MODE=strict` — panic. `strict` MUST be the value in `dev` and in every CI run; `redact` is the production default so that a guard false positive degrades a log line rather than crashing a worker mid-study.
- **MOS-OPS-040** — Any `error` value that crosses into a log line, a span attribute or an API response MUST pass through `SanitiseError`, which (a) truncates to 512 bytes, (b) applies `phiguard.Scan`, and (c) strips URL query strings and request bodies. Wrapping an upstream error with `%w` and logging the chain verbatim is forbidden.
- **MOS-OPS-041** — A non-zero `medicalos_log_redaction_violation_total` in production is a defect, not an operating condition. It MUST raise a ticket-severity alert and the offending field MUST be fixed at source; leaving the guard to catch it in perpetuity is not compliance.
- **MOS-OPS-042** — Logs emitted by sealed-mode service containers MUST be treated as untrusted. The collector MUST route them to a separate Loki stream labelled `origin="vendor"`, apply `phiguard` to them, and MUST NOT allow them to set `tenant_id`, `job_id`, `trace_id` or any `medicalos.*` field; those are re-stamped by the collector from the invocation context.

### 13.6. Distributed tracing

- **MOS-OPS-043** — Context propagation MUST use W3C Trace Context (`traceparent`, `tracestate`) over HTTP and, across the queue, as columns/headers on the queue record. The previous version's `correlation_id` is not a trace context and MUST NOT be used as one; a `correlation_id` field MAY survive as a human-supplied grouping key but carries no tracing semantics.

#### 13.6.1. Span layout

A job produces **two traces**, not one, joined by a span link and by the `medicalos.job.id` attribute.

*Trace A — synchronous admission, in `medicalos-api`:*

```
SERVER  POST /api/v1/jobs                         http.route=/api/v1/jobs
  INTERNAL authn.verify
  INTERNAL policy.evaluate
  INTERNAL quota.reserve                          medicalos.quota.key=gpu_hours_per_day
  CLIENT   gateway.qido.series                 ──► SERVER gateway.qido        (medicalos-gateway)
  INTERNAL triage.evaluate_selectors              medicalos.series.count=7
  INTERNAL resolve.capability                     medicalos.capability=pleural_effusion
  PRODUCER jobqueue.enqueue                       messaging.operation=publish
```

*Trace B — one per job attempt, rooted in `medicalos-worker`, with `link → jobqueue.enqueue`:*

```
INTERNAL job.attempt                              medicalos.job.attempt=2   [root]
  CLIENT   gateway.wado.retrieve               ──► SERVER gateway.wado      (medicalos-gateway)
                                                     INTERNAL deid.apply
  INTERNAL volume.build                           medicalos.volume.shape=512x512x431
  CLIENT   service.invoke                      ──► SERVER svc.execute       (svc-pleural-effusion)
                                                     INTERNAL svc.preprocess
                                                     CLIENT   svc.infer  ──► triton (gRPC)
                                                     INTERNAL svc.postprocess
  INTERNAL result.validate
  INTERNAL geometry.invert
  INTERNAL dicom.write                            child spans: dicom.write.seg, dicom.write.sr
  CLIENT   gateway.stow                        ──► SERVER gateway.stow
  INTERNAL job.complete                           medicalos.job.terminal_state=COMPLETED
```

- **MOS-OPS-044** — The worker-side trace MUST be a new root with a span link to the producer span, not a child of it. A job may sit queued for hours and may be attempted several times; making the consumer a child produces a trace whose duration is the queue latency, which no tracing backend renders usefully, and which merges unrelated attempts into one tree.
- **MOS-OPS-045** — Both traces MUST carry `medicalos.job.id` as a span attribute on their root span so that an operator can retrieve the complete story of a job with a single attribute query.
- **MOS-OPS-046** — Every span that crosses a process boundary MUST be `CLIENT`/`SERVER` or `PRODUCER`/`CONSUMER` paired. Fire-and-forget instrumentation that emits only one side is forbidden; the gateway half is exactly where PHI-access auditing and latency live.

#### 13.6.2. Trace attribute allow-list

- **MOS-OPS-047** — Span attributes are an **allow-list**. Any key not in the table below MUST be dropped by the collector before storage. Application code MUST NOT set an attribute outside this list; the SDK wrapper exposes typed setters only.

| key | type | example |
|---|---|---|
| `medicalos.tenant.id` | string | `tnt_01J9F3KQ` |
| `medicalos.job.id` | string | `job_01J9F3XQ2M` |
| `medicalos.job.attempt` | int | `2` |
| `medicalos.job.phase` | string | `dicom_write` |
| `medicalos.job.terminal_state` | string | `REJECTED` |
| `medicalos.rejection.reason_code` | string | `no_eligible_series` |
| `medicalos.error.class` | string | `dicom_write_failed` |
| `medicalos.service.id` / `.version` | string | `svc_pleural_effusion` / `3.2.1` |
| `medicalos.service.execution_mode` | string | `native` |
| `medicalos.capability` | string | `pleural_effusion` |
| `medicalos.model.id` / `.version` / `.artifact_digest` | string | `pulmo.pleural-effusion` / `3.2.1` / `sha256:9c1f…` |
| `medicalos.preprocessing.version` | string | `2.4.0` |
| `medicalos.study.ref` / `.series.ref` / `.patient.ref` | string | `S-k1-7QK3M2XA9RBTV4ZC` |
| `medicalos.series.count` / `medicalos.instance.count` | int | `7` / `431` |
| `medicalos.volume.shape` | string | `512x512x431` |
| `medicalos.volume.spacing_mm` | string | `0.703x0.703x1.000` |
| `medicalos.deid.profile` | string | `PS3.15-E-basic+clean-pixel` |
| `medicalos.gpu.uuid` | string | `GPU-8f3c1d22-0b4a-4f1e-9a77-2c5ee01d4b90` |
| `medicalos.residency.reservation_id` | string | `res_01J9F3XR44` |
| `medicalos.quota.key` | string | `gpu_hours_per_day` |
| `medicalos.dicom.sop_class` | string | `SEG` |
| OTel semconv (permitted subset) | | `service.name`, `service.version`, `deployment.environment.name`, `http.request.method`, `http.route`, `http.response.status_code`, `server.address`, `rpc.system`, `rpc.method`, `messaging.system`, `messaging.operation`, `otel.status_code`, `otel.status_description` |

- **MOS-OPS-048** — `url.full`, `url.path`, `url.query` and `http.target` are **forbidden**. DICOMweb request paths embed Study, Series and SOP Instance UIDs, so a resolved path is PHI. Instrumentation MUST set `http.route` with the template only: `/dicomweb/studies/{study}/series/{series}/instances/{instance}`.
- **MOS-OPS-049** — `otel.status_description` MUST be produced by `SanitiseError` (MOS-OPS-040).
- **MOS-OPS-050** — The allow-list MUST be enforced in the collector, not only in the SDK, so that a sealed vendor container cannot exfiltrate PHI through span attributes. Spans arriving from a sealed service MUST additionally have their names prefixed `svc.` and every `medicalos.*` attribute re-stamped by the collector from the invocation context.

#### 13.6.3. Collector configuration

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }

processors:
  transform/span_allowlist:
    error_mode: propagate
    trace_statements:
      - context: span
        statements:
          - keep_keys(attributes, [
              "medicalos.tenant.id","medicalos.job.id","medicalos.job.attempt",
              "medicalos.job.phase","medicalos.job.terminal_state","medicalos.rejection.reason_code",
              "medicalos.error.class","medicalos.service.id","medicalos.service.version",
              "medicalos.service.execution_mode","medicalos.capability",
              "medicalos.model.id","medicalos.model.version","medicalos.model.artifact_digest",
              "medicalos.preprocessing.version","medicalos.study.ref","medicalos.series.ref",
              "medicalos.patient.ref","medicalos.series.count","medicalos.instance.count",
              "medicalos.volume.shape","medicalos.volume.spacing_mm","medicalos.deid.profile",
              "medicalos.gpu.uuid","medicalos.residency.reservation_id","medicalos.quota.key",
              "medicalos.dicom.sop_class",
              "service.name","service.version","deployment.environment.name",
              "http.request.method","http.route","http.response.status_code","server.address",
              "rpc.system","rpc.method","messaging.system","messaging.operation",
              "otel.status_code","otel.status_description"])
  tail_sampling:
    decision_wait: 30s
    num_traces: 50000
    policies:
      - name: keep-all-non-completed
        type: string_attribute
        string_attribute:
          key: medicalos.job.terminal_state
          values: ["FAILED", "REJECTED", "CANCELLED"]
      - name: keep-errors
        type: status_code
        status_code: { status_codes: [ERROR] }
      - name: keep-slow
        type: latency
        latency: { threshold_ms: 60000 }
      - name: sample-the-rest
        type: probabilistic
        probabilistic: { sampling_percentage: 10 }
  batch:
    timeout: 5s
    send_batch_size: 1024

exporters:
  otlp/tempo: { endpoint: tempo:4317, tls: { insecure: true } }
  prometheusremotewrite: { endpoint: http://prometheus:9090/api/v1/write }
  loki: { endpoint: http://loki:3100/loki/api/v1/push }

service:
  pipelines:
    traces:  { receivers: [otlp], processors: [transform/span_allowlist, tail_sampling, batch], exporters: [otlp/tempo] }
    metrics: { receivers: [otlp], processors: [batch], exporters: [prometheusremotewrite] }
    logs:    { receivers: [otlp], processors: [transform/span_allowlist, batch], exporters: [loki] }
```

- **MOS-OPS-051** — The allow-list processor MUST run with `error_mode: propagate` so that a span the processor cannot evaluate is dropped rather than forwarded unredacted. Fail-closed is mandatory for redaction.
- **MOS-OPS-052** — Tail sampling MUST retain 100 % of traces whose job attempt did not end `COMPLETED`, 100 % of traces containing an `ERROR` span, and 100 % of traces slower than 60 s. The residual probabilistic rate MAY be tuned per site; it MUST NOT be the only policy.
- **MOS-OPS-053** — The OpenTelemetry Collector MUST be the sole telemetry egress point. No MedicalOS process and no service container may be configured with credentials for an external telemetry backend (MOS-OPS-003).

### 13.7. Alerting and service level objectives

- **MOS-OPS-054** — Alerts MUST be routed by *owner*, not by severity alone. A rise in `REJECTED` is a clinical or configuration signal routed to the deployment owner; a rise in `FAILED` is an engineering signal routed to on-call. Conflating them is how the previous version's `REJECTED`-as-error problem reappears in operations.

```yaml
groups:
  - name: medicalos.alerts
    rules:
      - alert: DeidentificationFailure
        expr: increase(medicalos_deid_failure_total[5m]) > 0
        for: 0m
        labels: { severity: critical, owner: security }
        annotations:
          summary: "De-identification failed ({{ $labels.failure_kind }}) for tenant {{ $labels.tenant_id }}"
          runbook: "https://docs.medicalos.dev/runbooks/deid-failure"

      - alert: PreprocessingSelfTestFailed
        expr: increase(medicalos_preprocessing_selftest_total{outcome="fail"}[10m]) > 0
        for: 0m
        labels: { severity: critical, owner: oncall }
        annotations: { summary: "Preprocessing skew detected for {{ $labels.model_id }}@{{ $labels.model_version }}; replica will not serve" }

      - alert: DicomWriteFailing
        expr: rate(medicalos_dicom_write_failure_total[15m]) > 0.01
        for: 15m
        labels: { severity: page, owner: oncall }

      - alert: JobErrorRatioHigh
        expr: medicalos:job_error_ratio:rate30m > 0.05
        for: 30m
        labels: { severity: page, owner: oncall }

      - alert: ClinicalRejectionRatioHigh
        expr: sum by (tenant_id, service_id, reason_code) (medicalos:job_rejection_ratio:rate30m) > 0.30
        for: 2h
        labels: { severity: ticket, owner: deployment }
        annotations: { summary: "{{ $labels.service_id }} rejects >30% of studies ({{ $labels.reason_code }}) — check SeriesSelector against site protocol" }

      - alert: LeaseExpiryRateHigh
        expr: rate(medicalos_job_lease_expired_total[15m]) > 0.02
        for: 15m
        labels: { severity: page, owner: oncall }

      - alert: QueueHeadOfLineStalled
        expr: medicalos_queue_oldest_age_seconds > 1800
        for: 10m
        labels: { severity: page, owner: oncall }

      - alert: GpuResidencyStarvation
        expr: |
          max by (service_id) (medicalos_queue_oldest_age_seconds) > 900
          and on() (max(medicalos_gpu_residency_committed_bytes / medicalos_gpu_residency_budget_bytes) > 0.95)
        for: 15m
        labels: { severity: ticket, owner: capacity }
        annotations: { summary: "{{ $labels.service_id }} starved of GPU residency; see 13.10.4 for the three remedies" }

      - alert: ValidationGateErroring
        expr: increase(medicalos_validation_gate_total{outcome="error"}[1h]) > 0
        for: 0m
        labels: { severity: ticket, owner: evidence }

      - alert: LogRedactionViolation
        expr: increase(medicalos_log_redaction_violation_total[1h]) > 0
        for: 0m
        labels: { severity: ticket, owner: security }
```

- **MOS-OPS-055** — Every alert MUST carry a `runbook` annotation or a `summary` naming the remedy. An alert with neither is a notification, not an alert, and MUST fail the alert-rule lint in CI.
- **MOS-OPS-056** — For 0.2.0 the following SLOs are declared targets, measured monthly, reported on the operations dashboard, and explicitly **not** clinical claims:

| SLO | target | measurement |
|---|---|---|
| `POST /api/v1/jobs` availability | 99.5 % | non-5xx over total, excluding 429/403 admission responses |
| `POST /api/v1/jobs` latency p95 | ≤ 800 ms | `http_server_duration_seconds` on the route |
| Study-to-result p95, 400-slice chest CT, one concurrent study, single NVIDIA T4 | ≤ 900 s | `medicalos_job_duration_seconds{terminal_state="COMPLETED"}` |
| Job loss rate | 0 | jobs in `CREATED`/`QUEUED` older than 24 h with no worker attempt |
| DICOM write success | ≥ 99.9 % | `stored`+`skipped_idempotent` over all outcomes |

### 13.8. Development deployment: docker compose

Docker compose is normative for development and for a single-node 0.1.0/0.2.0 pilot install. Kafka is absent from the default profile because the 0.1+ queue driver is PostgreSQL (MOS-EXEC); it appears only under the `kafka` profile from 0.3.0.

- **MOS-OPS-057** — The repository MUST contain exactly one `compose.yaml` that starts a complete working system with `docker compose up` after `cp .env.example .env`. No second "full" or "minimal" compose file.
- **MOS-OPS-058** — Any Kafka service MUST run in KRaft mode. ZooKeeper MUST NOT appear anywhere in the repository, charts or documentation; ZooKeeper mode was removed in Kafka 4.0.
- **MOS-OPS-059** — The images used in compose MUST be byte-identical to the images used in Kubernetes. There is no `-dev` image variant, no in-image mode switch, and no alternate entrypoint.

```yaml
name: medicalos-dev

networks:
  medos: {}
  phi:
    internal: true          # MOS-OPS-005: only the gateway bridges phi <-> medos

volumes: { pgdata: {}, miniodata: {}, orthancdata: {}, models: {}, promdata: {}, grafanadata: {} }

x-medicalos-common: &medicalos-common
  environment: &medicalos-env
    MEDICALOS_ENV: dev
    MEDICALOS_METRICS_ADDR: ":9090"
    MEDICALOS_DB_DSN: "postgres://medicalos@postgres:5432/medicalos?sslmode=disable"
    MEDICALOS_DB_PASSWORD_FILE: /run/secrets/db_password
    MEDICALOS_TENANT_PSEUDONYM_KEY_FILE: /run/secrets/pseudonym_key
    MEDICALOS_OBJECT_STORE_ENDPOINT: "http://minio:9000"
    MEDICALOS_PHI_GUARD_MODE: strict
    OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector:4317"
    OTEL_TRACES_SAMPLER: parentbased_always_on
  restart: unless-stopped

services:

  postgres:
    image: postgres:16.4-alpine
    environment:
      POSTGRES_USER: medicalos
      POSTGRES_DB: medicalos
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    secrets: [db_password]
    command: ["postgres","-c","max_connections=300","-c","shared_preload_libraries=pg_stat_statements"]
    healthcheck:
      test: ["CMD-SHELL","pg_isready -U medicalos -d medicalos"]
      interval: 5s
      timeout: 3s
      retries: 30
    volumes: [ "pgdata:/var/lib/postgresql/data" ]
    networks: [medos]

  minio:
    image: quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z
    command: ["server","/data","--console-address",":9001"]
    environment:
      MINIO_ROOT_USER: medicalos
      MINIO_ROOT_PASSWORD_FILE: /run/secrets/minio_password
    secrets: [minio_password]
    healthcheck:
      test: ["CMD","mc","ready","local"]
      interval: 10s
      retries: 20
    volumes: [ "miniodata:/data" ]
    networks: [medos]

  orthanc:
    image: orthancteam/orthanc:25.2.0
    environment:
      ORTHANC__DICOM_WEB__ENABLE: "true"
      ORTHANC__AUTHENTICATION_ENABLED: "true"
      ORTHANC__REGISTERED_USERS: '{"medicalos":"${ORTHANC_PASSWORD:?set ORTHANC_PASSWORD in .env}"}'
    volumes: [ "orthancdata:/var/lib/orthanc/db" ]
    networks: [phi]          # no ports:, no medos — unreachable except via the gateway

  medicalos-gateway:
    <<: *medicalos-common
    image: ghcr.io/medicalos/gateway:0.2.0
    environment:
      <<: *medicalos-env
      OTEL_SERVICE_NAME: medicalos-gateway
      MEDICALOS_HTTP_ADDR: ":8080"
      MEDICALOS_PACS_DICOMWEB_URL: "http://orthanc:8042/dicom-web"
      MEDICALOS_PACS_USERNAME: medicalos
      MEDICALOS_PACS_PASSWORD_FILE: /run/secrets/orthanc_password
      MEDICALOS_GATEWAY_MAX_CONCURRENT_RETRIEVALS: "8"
      MEDICALOS_DEID_DEFAULT_PROFILE: "PS3.15-E-basic+clean-pixel"
    secrets: [db_password, pseudonym_key, orthanc_password]
    networks: [medos, phi]
    depends_on: { postgres: { condition: service_healthy } }

  medicalos-api:
    <<: *medicalos-common
    image: ghcr.io/medicalos/api:0.2.0
    environment:
      <<: *medicalos-env
      OTEL_SERVICE_NAME: medicalos-api
      MEDICALOS_HTTP_ADDR: ":8080"
      MEDICALOS_JOBQUEUE_DRIVER: postgres
      MEDICALOS_GATEWAY_URL: "http://medicalos-gateway:8080"
      MEDICALOS_ADMISSION_MAX_QUEUED_JOBS_DEFAULT: "500"
      MEDICALOS_DRAIN_TIMEOUT_SECONDS: "60"
    secrets: [db_password, pseudonym_key]
    ports: [ "8080:8080" ]
    networks: [medos]
    depends_on: { postgres: { condition: service_healthy }, minio: { condition: service_healthy } }

  medicalos-worker:
    <<: *medicalos-common
    image: ghcr.io/medicalos/worker:0.2.0
    environment:
      <<: *medicalos-env
      OTEL_SERVICE_NAME: medicalos-worker
      MEDICALOS_HTTP_ADDR: ":8080"
      MEDICALOS_JOBQUEUE_DRIVER: postgres
      MEDICALOS_GATEWAY_URL: "http://medicalos-gateway:8080"
      MEDICALOS_TRITOND_URL: "http://medicalos-tritond:8081"
      MEDICALOS_WORKER_MAX_INFLIGHT_JOBS: "2"
      MEDICALOS_RESIDENCY_POLL_INTERVAL_SECONDS: "2"
      MEDICALOS_JOB_TIMEOUT_SECONDS: "1800"
      MEDICALOS_DRAIN_TIMEOUT_SECONDS: "1860"
    stop_grace_period: 1870s
    secrets: [db_password, pseudonym_key]
    networks: [medos]
    depends_on: { postgres: { condition: service_healthy } }

  triton:
    image: nvcr.io/nvidia/tritonserver:25.02-py3
    command: >
      tritonserver --model-repository=/models
      --model-control-mode=explicit
      --strict-readiness=true --exit-on-error=true
      --response-cache-byte-size=0
      --allow-metrics=true --metrics-port=8002
      --log-format=default --log-verbose=0
    volumes: [ "models:/models:ro" ]
    networks: [medos]
    deploy:
      resources:
        reservations:
          devices: [ { driver: nvidia, count: 1, capabilities: [gpu] } ]

  medicalos-tritond:
    <<: *medicalos-common
    image: ghcr.io/medicalos/tritond:0.2.0
    environment:
      <<: *medicalos-env
      OTEL_SERVICE_NAME: medicalos-tritond
      MEDICALOS_HTTP_ADDR: ":8081"
      MEDICALOS_NODE_ID: "gpu-node-local"
      MEDICALOS_TRITON_URL: "http://triton:8000"
      MEDICALOS_TRITON_MODEL_REPO: /models
      MEDICALOS_GPU_HEADROOM_RATIO: "0.90"
      MEDICALOS_GPU_RUNTIME_RESERVED_BYTES: "1288490188"
      MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS: "300"
    secrets: [db_password]
    volumes: [ "models:/models" ]
    networks: [medos]
    depends_on: [triton]

  svc-pleural-effusion:
    image: ghcr.io/pulmoai/svc-pleural-effusion:3.2.1
    environment:
      MEDICALOS_SERVICE_HTTP_ADDR: ":8080"
      MEDICALOS_TRITON_URL: "http://triton:8000"
      MEDICALOS_EXECUTION_MODE: native
      OTEL_EXPORTER_OTLP_ENDPOINT: "http://otel-collector:4317"
      OTEL_SERVICE_NAME: svc-pleural-effusion
    networks: [medos]        # no route to postgres, minio or orthanc — MOS-SVC boundary

  ohif:
    image: ohif/app:v3.11.0
    environment:
      APP_CONFIG_DICOMWEB_URL: "http://localhost:8081/dicomweb"   # the gateway, never orthanc
    ports: [ "3000:80" ]
    networks: [medos]

  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.121.0
    command: ["--config=/etc/otelcol/config.yaml"]
    volumes: [ "./deploy/otel/config.yaml:/etc/otelcol/config.yaml:ro" ]
    networks: [medos]

  prometheus:
    image: prom/prometheus:v3.2.1
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --web.enable-remote-write-receiver
      - --storage.tsdb.retention.time=15d
    volumes:
      - "./deploy/prometheus:/etc/prometheus:ro"
      - "promdata:/prometheus"
    networks: [medos]

  tempo:
    image: grafana/tempo:2.7.1
    command: ["-config.file=/etc/tempo/tempo.yaml"]
    volumes: [ "./deploy/tempo/tempo.yaml:/etc/tempo/tempo.yaml:ro" ]
    networks: [medos]

  loki:
    image: grafana/loki:3.4.2
    command: ["-config.file=/etc/loki/loki.yaml"]
    volumes: [ "./deploy/loki/loki.yaml:/etc/loki/loki.yaml:ro" ]
    networks: [medos]

  grafana:
    image: grafana/grafana:11.5.2
    environment:
      GF_SECURITY_ADMIN_PASSWORD__FILE: /run/secrets/grafana_password
      GF_USERS_ALLOW_SIGN_UP: "false"
    secrets: [grafana_password]
    volumes:
      - "./deploy/grafana/provisioning:/etc/grafana/provisioning:ro"
      - "grafanadata:/var/lib/grafana"
    ports: [ "3001:3000" ]
    networks: [medos]

  kafka:
    profiles: [kafka]                       # 0.3.0 only; KRaft, never ZooKeeper
    image: apache/kafka:4.0.0
    environment:
      KAFKA_NODE_ID: "1"
      KAFKA_PROCESS_ROLES: "broker,controller"
      KAFKA_CONTROLLER_QUORUM_VOTERS: "1@kafka:9093"
      KAFKA_LISTENERS: "PLAINTEXT://:9092,CONTROLLER://:9093"
      KAFKA_ADVERTISED_LISTENERS: "PLAINTEXT://kafka:9092"
      KAFKA_CONTROLLER_LISTENER_NAMES: "CONTROLLER"
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: "1"
    networks: [medos]

secrets:
  db_password:       { file: ./.secrets/db_password }
  minio_password:    { file: ./.secrets/minio_password }
  orthanc_password:  { file: ./.secrets/orthanc_password }
  pseudonym_key:     { file: ./.secrets/pseudonym_key }
  grafana_password:  { file: ./.secrets/grafana_password }
```

- **MOS-OPS-060** — The `svc-*` container MUST be attached only to the `medos` network and MUST NOT receive credentials for PostgreSQL, the object store, the queue or the PACS. This is the network-level expression of the MOS-SVC ownership boundary; compose is where it is first enforced, and a developer who adds such a credential MUST fail the topology test of MOS-OPS-100.

### 13.9. Production deployment: Kubernetes, and the three agnosticism constraints

Kubernetes charts land at 0.4.0, or earlier if a site requires them (Chapter 15, MOS-REL-023). The constraints below are binding from 0.1.0, because they are what make a later chart a packaging exercise instead of a rewrite.

- **MOS-OPS-061 — Constraint 1: configuration is environment variables and mounted files only.** Application code MUST NOT read a ConfigMap or Secret through the Kubernetes API, MUST NOT depend on the downward API beyond values injected as plain environment variables, and MUST NOT watch for configuration changes. Everything a process needs is in its environment at `exec` time (MOS-OPS-009, MOS-OPS-010).
- **MOS-OPS-062 — Constraint 2: one process per container, in the foreground, owning PID 1.** No supervisor (`supervisord`, `s6`), no in-container cron, no sidecar on which correctness depends, no shell wrapper that swallows signals. The container's entrypoint MUST be the binary itself so that `SIGTERM` reaches it directly and MOS-OPS-018 applies.
- **MOS-OPS-063 — Constraint 3: zero Kubernetes API calls.** No `client-go`, no `kubernetes` Python client, no read of `/var/run/secrets/kubernetes.io/serviceaccount`, no in-cluster service discovery, no `Lease`-based leader election, no runtime creation of Jobs or Pods. Peers are reached by DNS names supplied in environment variables. Where mutual exclusion is required it uses a PostgreSQL advisory lock. CI MUST enforce this with a dependency check that fails on any import of `k8s.io/client-go`, `sigs.k8s.io/controller-runtime` or `kubernetes` in `medos/services/`.

A direct consequence of Constraint 3, stated because it is load-bearing in §13.10: **MedicalOS never starts or stops a container at runtime.** Sealed-mode service containers are long-running deployments created by Helm or compose, never per-job pods created by the platform.

- **MOS-OPS-064** — MedicalOS MUST NOT implement a GPU scheduler. GPU placement is delegated entirely to Kubernetes with the NVIDIA GPU Operator and device plugin; MedicalOS declares requirements (`nvidia.com/gpu: 1`, node selector, toleration) and manages only *model residency within an allocated GPU* (§13.10).

Workload kinds:

| component | workload | replicas | notes |
|---|---|---|---|
| `medicalos-api` | Deployment + HPA | 2 … 10 | HPA on request rate; PDB `minAvailable: 1` |
| `medicalos-gateway` | Deployment + HPA | 2 … 8 | HPA on `medicalos_gateway_bytes_total` rate; PDB `minAvailable: 1` |
| `medicalos-worker` | Deployment + HPA | 1 … 20 | HPA on `medicalos_queue_depth`; `terminationGracePeriodSeconds: 1870` |
| `medicalos-tritond` | DaemonSet on GPU nodes | 1 per node | `MEDICALOS_NODE_ID` from `spec.nodeName` via the downward API as a plain env var |
| `triton` | DaemonSet on GPU nodes | 1 per node | `nvidia.com/gpu: 1` |
| `svc-<id>` native | Deployment + HPA | 1 … N | no GPU request; calls Triton on its node |
| `svc-<id>` sealed | Deployment | fixed | `nvidia.com/gpu: 1`; permanently holds residency budget (MOS-OPS-089) |

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: medicalos-worker, namespace: medicalos }
spec:
  replicas: 2
  selector: { matchLabels: { app: medicalos-worker } }
  template:
    metadata: { labels: { app: medicalos-worker } }
    spec:
      terminationGracePeriodSeconds: 1870          # MOS-OPS-019: drain 1860 + 10
      securityContext: { runAsNonRoot: true, runAsUser: 65532, fsGroup: 65532 }
      containers:
        - name: worker
          image: ghcr.io/medicalos/worker@sha256:71bfa0c9e2d1f4a8b3c05e7d9a1246ff0b8e3c5d7a9f1b2c4d6e8f0a1b3c5d7e
          args: []                                  # MOS-OPS-062: entrypoint is the binary
          ports:
            - { name: http, containerPort: 8080 }
            - { name: metrics, containerPort: 9090 }
          env:
            - { name: MEDICALOS_ENV, value: prod }
            - { name: MEDICALOS_HTTP_ADDR, value: ":8080" }
            - { name: MEDICALOS_METRICS_ADDR, value: ":9090" }
            - { name: MEDICALOS_GATEWAY_URL, value: "http://medicalos-gateway.medicalos.svc.cluster.local:8080" }
            - { name: MEDICALOS_JOB_TIMEOUT_SECONDS, value: "1800" }
            - { name: MEDICALOS_DRAIN_TIMEOUT_SECONDS, value: "1860" }
            - { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "http://otel-collector.observability.svc.cluster.local:4317" }
            - { name: OTEL_SERVICE_NAME, value: medicalos-worker }
            - name: MEDICALOS_NODE_ID
              valueFrom: { fieldRef: { fieldPath: spec.nodeName } }   # plain env var, not an API call
          volumeMounts:
            - { name: secrets, mountPath: /run/secrets, readOnly: true }
          startupProbe:
            httpGet: { path: /startupz, port: http }
            periodSeconds: 5
            failureThreshold: 60
          livenessProbe:
            httpGet: { path: /healthz, port: http }
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet: { path: /readyz, port: http }
            periodSeconds: 5
            failureThreshold: 2
          resources:
            requests: { cpu: "2", memory: "8Gi" }
            limits:   { cpu: "8", memory: "24Gi" }
      volumes:
        - name: secrets
          projected:
            sources:
              - secret: { name: medicalos-db, items: [ { key: password, path: db_password } ] }
              - secret: { name: medicalos-pseudonym, items: [ { key: key, path: pseudonym_key } ] }
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: { name: medicalos-worker, namespace: medicalos }
spec:
  minAvailable: 1
  selector: { matchLabels: { app: medicalos-worker } }
```

- **MOS-OPS-065** — Every image reference in a production manifest MUST be a digest, never a tag. The digest MUST match the `image_digest` the running process reports at `/version` and the digest recorded in job provenance (Chapter 9).
- **MOS-OPS-066** — A `NetworkPolicy` MUST exist that permits ingress to the PACS only from `app: medicalos-gateway`, and egress from `svc-*` pods only to `triton` on the same node and to the platform's service-invocation endpoint. This is MOS-OPS-005 and MOS-OPS-060 restated for the production topology.
- **MOS-OPS-067** — `medicalos-worker` MUST NOT set a CPU limit lower than 4× its request when volumetric resampling is on its critical path; CPU throttling during a sliding-window inverse transform inflates p95 job duration far more than it saves.

### 13.10. Triton and GPU residency in native mode

In `native` mode the platform loads the declared model onto shared Triton; the service container is Triton's *client* and the platform is Triton's *administrator*. `medicalos-tritond` is the only holder of Triton's model-control credential.

#### 13.10.1. Model repository layout and configuration

- **MOS-OPS-068** — The Triton model name MUST be derived deterministically from the `ModelVersion` identity, so that two versions of one model can be resident simultaneously (required for canary and blue/green in Chapter 6):

```
triton_model_name = replace(model_id, ".", "_") + "__" + replace(semver, ".", "_")
pulmo.pleural-effusion @ 3.2.1  ->  pulmo_pleural-effusion__3_2_1
```

- **MOS-OPS-069** — The Triton version directory MUST always be `1`. Triton's integer versioning MUST NOT be used to express `ModelVersion`; semver lives in the model name.

```
/models/
  pulmo_pleural-effusion__3_2_1/
    config.pbtxt
    warmup/
      golden_patch_batch8.fp32.bin          # 8 x 1 x 128^3 fp32, 268435456 bytes
    1/
      model.plan
    medicalos.json                          # artifact digest, compute capability, TRT version, footprint
```

```json
{
  "model_id": "pulmo.pleural-effusion",
  "model_version": "3.2.1",
  "artifact_digest": "sha256:9c1f4a7e08b2d63f5c1e9a04b7d28f16c3a5e097b4d2f8a6c0e1b3d5f7a9c2e4",
  "preprocessing_version": "2.4.0",
  "backend": "tensorrt_plan",
  "built_for": { "cuda_compute_capability": "7.5", "tensorrt_version": "10.8.0", "cuda_version": "12.8" },
  "allowed_patch_batch_sizes": [2, 4, 8],
  "weights_bytes": 224395264,
  "workspace_bytes_by_patch_batch": { "2": 912261120, "4": 1795162112, "8": 3563061248 }
}
```

- **MOS-OPS-070** — `medicalos.json` MUST be produced by the packaging step (Chapter 6), MUST be covered by the same signature as the weights, and MUST record `built_for`. `tritond` MUST refuse to load a `tensorrt_plan` whose `built_for.cuda_compute_capability` does not equal the GPU's compute capability or whose `built_for.tensorrt_version` does not equal the server's, MUST increment `medicalos_model_load_total{outcome="load_error"}`, and MUST surface the mismatch as a deployment error. A platform-level `min_triton: "2.x"` is meaningless for a serialized plan and MUST NOT be used as the compatibility statement.
- **MOS-OPS-071** — A backend conversion (PyTorch → ONNX → TensorRT plan) changes numerics and therefore produces a **new `ModelVersion` requiring its own `EvaluationRun`** (Chapter 7). Triton MUST be configured so it cannot perform such a conversion at load time: `optimization { execution_accelerators { ... } }` stanzas that build a TensorRT engine on load are forbidden, and `tritond` MUST reject a `config.pbtxt` containing one.

```
name: "pulmo_pleural-effusion__3_2_1"
platform: "tensorrt_plan"
max_batch_size: 0

input [
  { name: "INPUT__0", data_type: TYPE_FP32, dims: [ -1, 1, 128, 128, 128 ] }
]
output [
  { name: "OUTPUT__0", data_type: TYPE_FP32, dims: [ -1, 2, 128, 128, 128 ] }
]

instance_group [ { kind: KIND_GPU, count: 1, gpus: [ 0 ] } ]

version_policy { specific { versions: [ 1 ] } }

model_warmup [
  {
    name: "golden_patch_batch8"
    batch_size: 0
    count: 1
    inputs {
      key: "INPUT__0"
      value {
        data_type: TYPE_FP32
        dims: [ 8, 1, 128, 128, 128 ]
        input_data_file: "golden_patch_batch8.fp32.bin"
      }
    }
  }
]
```

- **MOS-OPS-072** — `max_batch_size` MUST be `0` and the batch dimension MUST be declared explicitly as the leading `-1`. Triton's dynamic batcher MUST NOT be enabled for volumetric models (§13.10.2).
- **MOS-OPS-073** — `instance_group.count` MUST default to `1`. A second instance of a patch-batched 3D model buys no throughput because one instance already saturates the SMs, and it multiplies `workspace_bytes` by `count`. Any `count > 1` MUST be reflected in the declared footprint and MUST be justified by a measured throughput gain recorded on the `ModelVersion`.
- **MOS-OPS-074** — Triton MUST run with `--model-control-mode=explicit`, `--strict-readiness=true`, `--exit-on-error=true` and `--response-cache-byte-size=0`. The response cache is forbidden outright: a cache keyed on input tensor bytes can return one patient's mask for another patient's identical-looking patch, and it silently breaks per-job timing and provenance.
- **MOS-OPS-075** — `model_warmup` MUST be configured with the golden fixture shipped in the artifact, executed once at load. This is a latency measure only; it does not replace the MOS-IMG preprocessing self-test, which compares an output tensor hash against the training-time value and is run by the serving component at startup (MOS-OPS-015).

#### 13.10.2. Why naive dynamic batching does not apply

Triton's dynamic batcher exists to coalesce many small, independent, low-occupancy requests. Volumetric medical inference has the opposite shape, and the arithmetic is decisive.

A 512×512×431 chest CT at the model's 128³ patch size with 0.5 sliding-window overlap yields roughly 7 × 7 × 6 = 294 patches. The worker already submits these as batches of `patch_batch_size` — **the batching is inside the request**, on its leading dimension.

| quantity | value |
|---|---|
| one 128³ fp32 patch, 1 channel | 8.39 MB |
| input tensor at `patch_batch_size=8` | 67.1 MB |
| measured peak activation workspace at batch 8 (`pulmo.pleural-effusion 3.2.1`) | 3.56 GB |
| patches per 400-slice study at overlap 0.5 | ≈ 294 |
| Triton requests per study at batch 8 | ≈ 37 |

Three consequences:

- **MOS-OPS-076** — Coalescing two studies' requests MUST NOT be attempted. SM occupancy is already near-saturated by a single batch-8 3D request, so a second study contributes no throughput; it only adds `max_queue_delay_microseconds` of latency to the first request and doubles peak workspace from 3.56 GB to 7.12 GB — memory that §13.10.3 needs for residency. Batching trades a resource that is scarce for a gain that is zero.
- **MOS-OPS-077** — Whole-volume (non-patch) models have study-dependent input shapes (`512×512×431` vs `512×512×287`), so no batch can be formed at all without padding, and padding a volumetric input changes normalisation statistics and therefore output. Dynamic batching MUST NOT be enabled for any model whose input shape varies per study.
- **MOS-OPS-078** — `patch_batch_size` is an engineering knob that MUST NOT change outputs. The packaging step MUST assert bit-identical output across every value in `allowed_patch_batch_sizes` on the golden fixture, and the serving component MUST refuse a configured value outside that list. This is what makes "reduce `patch_batch_size` to fit another model" (MOS-OPS-088) a capacity decision rather than a revalidation event.

#### 13.10.3. The residency contract

`tritond` maintains a memory budget per GPU and grants reservations against it.

```
budget_bytes        = (gpu_total_bytes - MEDICALOS_GPU_RUNTIME_RESERVED_BYTES) * MEDICALOS_GPU_HEADROOM_RATIO
footprint_bytes(m)  = weights_bytes(m) + workspace_bytes_by_patch_batch(m)[configured_patch_batch] * instance_count(m)
committed_bytes     = sum of footprint_bytes over resident + granted-but-not-yet-loaded models
                      + sum of declared footprint over sealed services scheduled on this node
```

- **MOS-OPS-079** — `MEDICALOS_GPU_RUNTIME_RESERVED_BYTES` MUST be **measured** at `tritond` startup (free memory before any model load, subtracted from total), not estimated, and re-measured after every Triton restart. The default in the chart (1 288 490 188 B = 1.2 GiB) is a starting value only.
- **MOS-OPS-080** — `MEDICALOS_GPU_HEADROOM_RATIO` MUST default to `0.90` and MUST NOT exceed `0.95`. TensorRT and cuDNN allocate transient scratch outside the declared workspace; a budget at 1.0 produces out-of-memory failures mid-study, which are the worst possible failure timing.

Residency classes, taken from the `Deployment.residency` column (Chapter 6, `MOS-REG-076a`). The column and its allowed values belong to Chapter 6; the class semantics, the eviction rules and the `footprint_bytes` arithmetic below belong to this chapter:

| class | loaded | may be evicted | counts against budget |
|---|---|---|---|
| `resident` | at `tritond` startup and when a `Deployment` enters `state = SERVING` | never | always |
| `on_demand` | on first reservation | yes, after its minimum hold window | while resident |
| `evictable` | on first reservation | yes, as soon as it holds no active reservation — no hold window | while resident |
| sealed service | never (it is not in Triton) | never | always, from the declared footprint |

- **MOS-OPS-114** — `evictable` is the third value of `Deployment.residency` and differs from `on_demand` in exactly one respect: it has no minimum hold window. An `evictable` model is loaded on first reservation, counts against `committed_bytes` only while resident, and becomes an eviction candidate the instant `active_reservations` reaches `0`; `MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS` does not apply to it and `tritond` MUST NOT clamp a hold window onto it (MOS-OPS-087 governs `on_demand` only). The rotation of MOS-OPS-086 MUST exhaust every `evictable` victim before it evicts any `on_demand` model. The class is for a model whose slot is worth more to a waiting capability than its warmth is worth to the next job; it MUST NOT be declared for a model whose p95 load duration exceeds 30 s, which MOS-OPS-092 already forces to `residency = 'resident'`, because no hold window on top of a slow load is exactly the thrash MOS-OPS-087 exists to prevent. These three values are the whole of the column (Chapter 6, `MOS-REG-076a`); the `sealed service` row above is a footprint category, not a value of `residency`.

The reservation API, served by `tritond` on `MEDICALOS_HTTP_ADDR` and reachable only from workers on the cluster network:

```http
POST /internal/v1/residency/reservations
{"model_id":"pulmo.pleural-effusion","model_version":"3.2.1","job_id":"job_01J9F3XQ2M",
 "patch_batch_size":8,"ttl_seconds":1800}

201 {"reservation_id":"res_01J9F3XR44","state":"granted","gpu_uuid":"GPU-8f3c1d22-0b4a-4f1e-9a77-2c5ee01d4b90",
     "triton_model_name":"pulmo_pleural-effusion__3_2_1","expires_at":"2026-09-13T12:34:31Z"}

202 {"state":"queued","reason":"gpu_memory_contention","position":2,"retry_after_seconds":20}

409 {"state":"unsatisfiable","reason":"footprint_exceeds_budget",
     "footprint_bytes":9663676416,"budget_bytes":14281216000,"resident_pinned_bytes":12884901888}
```

```http
POST   /internal/v1/residency/reservations/{id}/renew   {"ttl_seconds":600}
DELETE /internal/v1/residency/reservations/{id}
GET    /internal/v1/residency/state
```

```json
{"node_id":"gpu-node-01","gpu_uuid":"GPU-8f3c1d22-0b4a-4f1e-9a77-2c5ee01d4b90",
 "budget_bytes":14281216000,"committed_bytes":7412793344,
 "resident":[
   {"model_id":"pulmo.lung-seg","model_version":"2.1.0","class":"on_demand",
    "footprint_bytes":3489660928,"last_used_at":"2026-09-13T12:03:02Z",
    "held_until":"2026-09-13T12:08:02Z","active_reservations":0},
   {"model_id":"pulmo.pleural-effusion","model_version":"3.2.1","class":"on_demand",
    "footprint_bytes":3923132416,"last_used_at":"2026-09-13T12:04:31Z",
    "held_until":"2026-09-13T12:09:31Z","active_reservations":1}],
 "waiting":[{"model_id":"lidc.nodule-det","model_version":"1.0.0","queued_jobs":4,
             "waiting_since":"2026-09-13T11:58:10Z"}],
 "admissible_service_ids":["svc_lung_seg","svc_pleural_effusion"]}
```

- **MOS-OPS-081** — A worker MUST call `Claim` only for inboxes whose `service_id` is in `admissible_service_ids`, refreshed every `MEDICALOS_RESIDENCY_POLL_INTERVAL_SECONDS` (default 2). It MUST NOT claim a job and then wait for a residency slot; claiming first burns lease time, produces spurious `medicalos_job_lease_expired_total`, and starves other workers of that job.
- **MOS-OPS-082** — A `granted` reservation MUST be renewed at least every `ttl_seconds / 3` while the job runs, MUST be released with `DELETE` on every terminal outcome, and MUST be released by `tritond` when its TTL expires with no renewal. Reservation release MUST be idempotent and keyed by `job_id`, so that a crashed worker's reservation is reclaimed by TTL and a late `DELETE` from a resurrected worker is harmless.
- **MOS-OPS-083** — `409 unsatisfiable` means the model can never run on this node even with everything evictable evicted. It MUST produce job `FAILED` with `failure.class = 'internal'` and `failure.code = 'capacity_configuration'` — the job failure enum is closed and owned by Chapter 5 §5.3.2, and `code` is the open field — and **never `REJECTED`**, because `REJECTED` is a clinical outcome about the study (MOS-EXEC-001) and this is an operator error about the cluster. It MUST also raise a ticket-severity alert, because it means a `Deployment` reached `state = SERVING` that cannot execute.
- **MOS-OPS-084** — The platform MUST refuse to transition a `Deployment` to `state = SERVING` (Chapter 6 §6.8) when the sum of `footprint_bytes` over all `resident`-class models plus all sealed services on every eligible node would exceed that node's `budget_bytes`. This check belongs to Chapter 6's Deployment gate (`MOS-REG-076a`, on the `PENDING → VERIFYING` transition); §13.10 supplies the numbers. Catching over-commitment at deploy time is the difference between a rejected deployment and an unrunnable capability discovered at 2 a.m.

#### 13.10.4. Three capabilities, two slots

This is the case the previous version left unanswered. Concretely, on one NVIDIA T4 (16 GB, compute capability 7.5), with `MEDICALOS_GPU_RUNTIME_RESERVED_BYTES = 1.2 GiB` and `MEDICALOS_GPU_HEADROOM_RATIO = 0.90`:

```
budget_bytes = (17179869184 - 1288490188) * 0.90 = 14302841096 ≈ 13.32 GiB
```

| model | patch | weights | workspace | footprint |
|---|---|---|---|---|
| `pulmo.lung-seg 2.1.0` | 128³ × 8 | 196 MiB | 3.13 GiB | **3.25 GiB** |
| `pulmo.pleural-effusion 3.2.1` | 128³ × 8 | 214 MiB | 3.32 GiB | **3.65 GiB** |
| `lidc.nodule-det 1.0.0` | 192³ × 4 | 384 MiB | 8.27 GiB | **8.65 GiB** |

Any two fit (3.25 + 3.65 = 6.90; 3.65 + 8.65 = 12.30; 3.25 + 8.65 = 11.90). All three need 15.55 GiB against a 13.32 GiB budget. **What happens is this, and nothing else:**

- **MOS-OPS-085** — Nothing fails. Jobs for the non-resident capability remain `QUEUED`. No job is `REJECTED`, no job is `FAILED`, no retry budget is consumed, and no partial result is written. The condition is visible as `medicalos_queue_oldest_age_seconds{service_id="svc_nodule_det"}` rising and `medicalos_model_load_wait_seconds` accumulating.
- **MOS-OPS-086** — `tritond` MUST rotate residency on a FIFO-by-wait-time schedule that cannot starve:

```text
every MEDICALOS_RESIDENCY_TICK_SECONDS (default 2):
    if waiting is empty: return
    promote := argmax(waiting_since) over waiting where queued_jobs > 0
    if footprint(promote) > budget_bytes: mark promote unsatisfiable; alert; return
    while (budget_bytes - committed_bytes) < footprint(promote):
        victim := argmin(last_used_at) over resident where
                      class == evictable and active_reservations == 0
        if victim is nil:
            victim := argmin(last_used_at) over resident where
                          class == on_demand and active_reservations == 0 and now >= held_until
        if victim is nil: return            # everything is pinned, in use, or inside its hold window
        unload(victim); committed_bytes -= footprint(victim)
        metric medicalos_model_load_total{outcome="evicted"}++
    load(promote)
    promote.held_until := now + (0 if promote.class == evictable else MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS)
    committed_bytes += footprint(promote)
```

- **MOS-OPS-087** — `MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS` (default 300) MUST be ≥ the p95 of `medicalos_job_duration_seconds` for the services using that model. `tritond` MUST clamp a smaller configured value upward and log a warning. Without the hold window, three capabilities on two slots thrash: each load costs `medicalos_model_load_duration_seconds` and evicts the model the next job needs.
- **MOS-OPS-088** — When `GpuResidencyStarvation` fires, exactly three remedies exist and the runbook MUST name all three with their arithmetic:
  1. **Add a GPU node.** Linear, costs money, no revalidation.
  2. **Unpin a `resident`-class model** so the rotation can evict it. Costs cold start (`medicalos_model_load_duration_seconds` p95, typically 2–6 s for a TensorRT plan) per rotation.
  3. **Reduce `patch_batch_size`** for the largest model, within `allowed_patch_batch_sizes`. Taking `lidc.nodule-det` from 4 to 2 halves workspace to 4.14 GiB, giving a footprint of 4.51 GiB and a three-model total of 11.41 GiB ≤ 13.32 GiB — all three resident, at roughly 1.6× per-study inference wall time. Because MOS-OPS-078 requires bit-identical output across allowed batch sizes, this changes **no** clinical result and requires **no** new `EvaluationRun`.
- **MOS-OPS-089** — Sealed-mode services hold their declared footprint permanently, because MedicalOS never starts containers at runtime (MOS-OPS-063). Choosing sealed mode therefore costs a permanent residency slot on every node where the service is scheduled. This MUST be stated in the deployment documentation for sealed services, because it is the largest hidden operational cost of the sealed execution mode.
- **MOS-OPS-090** — `tritond` MUST reconcile declared against observed GPU memory every 60 s by scraping `nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits` and attributing usage to processes. If observed usage for a sealed service exceeds its declared footprint by more than 15 % for 5 consecutive minutes, `tritond` MUST raise `SealedServiceFootprintDrift`, and the platform MUST refuse to admit a further `Deployment` to `state = SERVING` on that node until it is resolved.

#### 13.10.5. Cold start

- **MOS-OPS-091** — `medicalos_model_load_duration_seconds` MUST be measured from the Triton `POST /v2/repository/models/{name}/load` call to model-ready, including `model_warmup`.
- **MOS-OPS-092** — A model whose p95 load duration exceeds 30 s MUST be declared `residency = 'resident'` on its `Deployment` (Chapter 6, `MOS-REG-076a`), or the `Deployment` MUST be refused. A 30 s cold start inside the rotation of §13.10.4 converts contention into unbounded latency.
- **MOS-OPS-093** — Load duration MUST NOT include format conversion (MOS-OPS-071). If a load exceeds 30 s and the artifact is not a pre-built plan, the correct fix is to build the plan in packaging, not to raise the timeout.

### 13.11. Scaling and backpressure

- **MOS-OPS-094** — Each process in §13.1 MUST scale horizontally and independently. Scaling `medicalos-worker` from 2 to 10 MUST have no effect on `medicalos-api` or `medicalos-gateway` replica counts, and MUST require no configuration change in any other component.
- **MOS-OPS-095** — `medicalos-worker` autoscaling MUST use `medicalos_queue_depth` and `medicalos_queue_oldest_age_seconds`, never CPU or memory. A worker waiting on a 400-slice WADO-RS retrieval consumes almost no CPU while being the exact replica that needs company.

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: medicalos-worker, namespace: medicalos }
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: medicalos-worker }
  minReplicas: 1
  maxReplicas: 20
  metrics:
    - type: External
      external:
        metric:
          name: medicalos_queue_depth
          selector: { matchLabels: { state: "queued" } }
        target: { type: AverageValue, averageValue: "3" }      # ~3 queued jobs per replica
    - type: External
      external:
        metric: { name: medicalos_queue_oldest_age_seconds }
        target: { type: Value, value: "600" }
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 60
      policies: [ { type: Pods, value: 4, periodSeconds: 60 } ]
    scaleDown:
      stabilizationWindowSeconds: 1800                          # >= job timeout: never scale down mid-study
      policies: [ { type: Pods, value: 1, periodSeconds: 300 } ]
```

- **MOS-OPS-096** — `scaleDown.stabilizationWindowSeconds` MUST be ≥ `MEDICALOS_JOB_TIMEOUT_SECONDS`. Otherwise a queue that drains quickly triggers a scale-down that terminates a worker mid-study; the drain contract (MOS-OPS-018) then makes the pod live for another 31 minutes, and the autoscaler, seeing the replica still present, scales down again.
- **MOS-OPS-097** — Scaling `medicalos-worker` does **not** scale GPU capacity. Worker replicas beyond the number of residency slots add queue-draining concurrency for CPU-bound phases only (retrieval, volume build, DICOM writing, STOW), which is genuinely useful because those dominate wall time for short inferences. GPU capacity scales only by adding GPU nodes (MOS-OPS-088).

Backpressure is a chain of four points. Each sheds load at a different depth and each has a defined, non-terminal outcome:

| # | point | trigger | mechanism | outcome |
|---|---|---|---|---|
| 1 | `POST /api/v1/jobs` admission | `queue_depth(tenant) > max_queued_jobs`; rate limit; quota | reject **before** creating a `Job` row | `429` with `class=rate_limit` (Chapter 10, table 10.4-A) and `Retry-After`; `code` distinguishes `RATE_LIMIT_EXCEEDED`, `QUEUE_DEPTH_EXCEEDED`, `QUOTA_EXCEEDED` |
| 2 | gateway retrieval | in-flight retrievals ≥ `MEDICALOS_GATEWAY_MAX_CONCURRENT_RETRIEVALS` | in-process semaphore; caller blocks | delay, measured by `medicalos_gateway_retrieval_wait_seconds` |
| 3 | GPU residency | `service_id ∉ admissible_service_ids` | worker does not `Claim` | job stays `QUEUED` |
| 4 | worker saturation | all replicas at `MEDICALOS_WORKER_MAX_INFLIGHT_JOBS` | no `Claim` issued | job stays `QUEUED`; autoscaler reacts |

- **MOS-OPS-098** — **No backpressure mechanism may consume a job's retry budget, change a job's state, or produce a terminal state.** Point 1 acts before a `Job` exists; points 2–4 produce delay only. This is absolute: the previous version's failure mode was load-shedding that dead-lettered good studies, and by the time it is noticed the studies are already gone.
- **MOS-OPS-099** — Admission rejection at point 1 MUST return `Retry-After` in seconds, computed from `medicalos_queue_oldest_age_seconds` for the tenant's services, and MUST be idempotent with respect to the client's `idempotency_key`: a retry after the queue drains MUST create the job, not a duplicate.
- **MOS-OPS-100** — A topology test MUST assert, for compose from 0.1.0 and for the Helm chart from the release in which the chart ships (0.4.0), that (a) no process other than `medicalos-gateway` can open a connection to the PACS, (b) no `svc-*` container has credentials or a route to PostgreSQL, the object store or the queue, and (c) no process other than `medicalos-tritond` can reach Triton's model-control endpoints.

### 13.12. Quotas and rate limits

Two different controls with two different enforcement points and two different stores.

All problem documents below carry a `class` from the closed RFC 9457 enum of Chapter 10 (`MOS-API-037`, table 10.4-A); throttling and quota exhaustion are both `rate_limit` at status `429`, and the open `code` member carries the specific reason. Chapter 13 defines no problem class of its own.

| limit | scope | window | store | enforcement point | on exceed |
|---|---|---|---|---|---|
| `requests_per_minute` | `ApiKey` | 60 s sliding | PostgreSQL counter | `medicalos-api` middleware, all write routes | `429`, `class=rate_limit`, `code=RATE_LIMIT_EXCEEDED` |
| `jobs_per_day` | `Tenant` | calendar day, tenant TZ | `tenant_quota_ledger` | `POST /api/v1/jobs`, before job creation | `429`, `class=rate_limit`, `code=QUOTA_EXCEEDED` |
| `gpu_hours_per_day` | `Tenant` | calendar day, tenant TZ | `tenant_quota_ledger` | job creation (reserve) + worker (top-up) + terminal (settle) | `429`, `class=rate_limit`, `code=QUOTA_EXCEEDED` at admission; never terminates clinical work |
| `storage_gb` | `Tenant` | absolute | `tenant_quota_ledger` | gateway STOW, object store writes | `429`, `class=rate_limit`, `code=QUOTA_EXCEEDED` |
| `max_queued_jobs` | `Tenant` | instantaneous | `medicalos_queue_depth` read from PostgreSQL | `POST /api/v1/jobs` | `429`, `class=rate_limit`, `code=QUEUE_DEPTH_EXCEEDED` |
| `max_concurrent_retrievals` | `Tenant` × replica | instantaneous | in-process semaphore | `medicalos-gateway` | block (not an error) |

- **MOS-OPS-101** — `max_concurrent_retrievals` is enforced **per gateway replica**, and the documentation and API MUST say so. A shared exact counter is not worth a round trip on the bulk-transfer path; concurrency, not rate, is the correct control for a path whose unit of work is a 400-slice retrieval.
- **MOS-OPS-102** — Every other limit MUST be exact and shared, backed by PostgreSQL. At 0.1–0.3 volumes (tens of jobs per hour) an `INSERT ... ON CONFLICT DO UPDATE` per check is free; introducing Redis or Valkey for this is unnecessary infrastructure and MUST NOT be added before a measured need.

#### 13.12.1. The GPU-hours ledger

The unit is **GPU-seconds**, stored as an integer to avoid float drift. The window is keyed into the row, so no scheduled job is needed to reset it — which is what keeps MOS-OPS-062 (no in-container cron) satisfiable.

```sql
CREATE TABLE tenant_quota_ledger (
    tenant_id       text        NOT NULL,
    quota_key       text        NOT NULL,          -- 'gpu_seconds_per_day' | 'jobs_per_day' | 'storage_bytes'
    window_start    date        NOT NULL,          -- in the tenant's configured timezone
    limit_units     bigint      NOT NULL,
    reserved_units  bigint      NOT NULL DEFAULT 0,
    consumed_units  bigint      NOT NULL DEFAULT 0,
    overage_units   bigint      NOT NULL DEFAULT 0,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, quota_key, window_start),
    CHECK (reserved_units >= 0 AND consumed_units >= 0 AND overage_units >= 0)
);

CREATE TABLE tenant_quota_reservation (
    reservation_id  text        PRIMARY KEY,
    tenant_id       text        NOT NULL,
    quota_key       text        NOT NULL,
    window_start    date        NOT NULL,
    job_id          text        NOT NULL UNIQUE,   -- one live reservation per job; makes release idempotent
    reserved_units  bigint      NOT NULL,
    settled         boolean     NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, quota_key, window_start)
        REFERENCES tenant_quota_ledger (tenant_id, quota_key, window_start)
);
```

**Reserve, at job creation.** The estimate comes from the resolved `ModelVersion`'s engineering bar `p95_gpu_seconds` on its `EvaluationRun` (Chapter 7), with a 25 % margin, summed over every resolved model pinned into the job:

```
estimate_gpu_seconds = ceil( 1.25 * Σ p95_gpu_seconds(resolved model) )
```

- **MOS-OPS-103** — The reservation and the `Job` row MUST be written in **one transaction**. If `reserved_units + consumed_units + estimate > limit_units`, the transaction MUST abort, **no `Job` row is created**, and the API returns `429` with `Retry-After` set to the seconds remaining until `window_resets_at`, and a body of:

```json
{"type":"https://spec.medicalos.org/problems/quota-exceeded","title":"Tenant quota exceeded",
 "status":429,"class":"rate_limit","code":"QUOTA_EXCEEDED","retryable":true,
 "detail":"Tenant gpu_seconds_per_day quota exhausted for the current window.",
 "instance":"/api/v1/jobs",
 "trace_id":"7a44d2e1c0b849f5a2e6d8b3f1c07a55","occurred_at":"2026-09-13T12:04:31.118Z",
 "quota_key":"gpu_seconds_per_day",
 "limit_units":360000,"consumed_units":352180,"reserved_units":9400,
 "requested_units":1425,"window_resets_at":"2026-09-14T00:00:00+02:00"}
```

  The `class` member is drawn from the closed RFC 9457 enum of Chapter 10 (`MOS-API-037`, table 10.4-A), in which `rate_limit` covers "throttled or over quota" at status `429`; `code` is the open member that carries the quota-specific meaning (`MOS-API-038`). This chapter MUST NOT add a value to that enum.

  Because no `Job` exists, no retry budget is consumed (MOS-OPS-098).

- **MOS-OPS-104** — A `ModelVersion` with no `p95_gpu_seconds` on any `EvaluationRun` MUST use `MEDICALOS_QUOTA_DEFAULT_ESTIMATE_GPU_SECONDS` (default 600) and MUST log at `warn`. It MUST NOT be treated as zero; an unmeasured model is the one most likely to be slow.

**Meter, during the job.** GPU-seconds are measured as **exclusive-reservation wall time on the GPU**, not an SM-utilisation integral. A tenant pays for holding the card.

- **MOS-OPS-105** — For native mode, GPU-seconds accrue from `residency reservation granted` to `released`, attributed to the reservation's `job_id`. For sealed mode, they accrue for the wall time the sealed container holds its GPU reservation for that job. The unit MUST be stated in the tenant-facing documentation, because the alternative definitions differ by a factor of two or more.
- **MOS-OPS-106** — The worker MUST top up its reservation every `MEDICALOS_QUOTA_TOPUP_INTERVAL_SECONDS` (default 60) while the job runs:

```sql
UPDATE tenant_quota_ledger
   SET reserved_units = reserved_units + $topup, updated_at = now()
 WHERE tenant_id = $1 AND quota_key = 'gpu_seconds_per_day' AND window_start = $2
RETURNING limit_units, reserved_units, consumed_units;
```

**Mid-job overrun — the rule that matters.**

- **MOS-OPS-107** — Quotas throttle **admission**, never in-flight clinical work. When a top-up pushes the tenant past `limit_units`, the job MUST be allowed to run to its terminal state. The excess MUST be recorded in `overage_units`, `medicalos_quota_overage_seconds_total{tenant_id}` MUST be incremented, and the **next** admission for that tenant MUST be denied under MOS-OPS-103. Killing a job at 90 % completion wastes the GPU-seconds already spent, leaves the study unanalysed, and — if the commit point has been passed — risks a partially written result.
- **MOS-OPS-108** — A per-tenant flag `quota_hard_stop` (default `false`) MAY be set only on deployments whose `clinical_use_mode` is `research_only` (Chapter 9). When true, the worker MUST abort at the next `JobStep` boundary **before** the `StoreResults` commit point, terminate `FAILED` with `failure.class = 'deadline_exceeded'` and `failure.code = 'quota_hard_stop'` (the job failure enum is closed — §5.3.2, Chapter 5 — and `code` is the open field), and MUST NOT write any DICOM object. It MUST NOT be settable on a `clinical` deployment.

**Settle, at terminal state.**

- **MOS-OPS-109** — On every terminal transition the worker MUST, in the **same transaction** as the terminal state write (MOS-EXEC-002):

```sql
WITH r AS (
  UPDATE tenant_quota_reservation SET settled = true
   WHERE job_id = $job_id AND settled = false
  RETURNING tenant_id, quota_key, window_start, reserved_units)
UPDATE tenant_quota_ledger l
   SET reserved_units = l.reserved_units - r.reserved_units,
       consumed_units = l.consumed_units + $actual_gpu_seconds,
       overage_units  = l.overage_units
                      + GREATEST(0, l.consumed_units + $actual_gpu_seconds - l.limit_units),
       updated_at     = now()
  FROM r
 WHERE l.tenant_id = r.tenant_id AND l.quota_key = r.quota_key AND l.window_start = r.window_start;
```

  The `settled = false` predicate makes settlement idempotent under retry.
- **MOS-OPS-110** — The same reconciler that expires a job lease (Chapter 5) MUST release the job's quota reservation, settling `actual_gpu_seconds` at the measured value if known and at `0` otherwise. A crashed worker MUST NOT leave `reserved_units` permanently inflated; a tenant whose reservations leak is a tenant locked out of the platform by an infrastructure fault.
- **MOS-OPS-111** — `window_start` MUST be computed in the tenant's configured timezone at the moment of the check. Reads MUST filter to the current window only; rows for past windows are retained as the billing and audit record and MUST NOT be deleted by any automated process.
- **MOS-OPS-112** — Every quota denial MUST increment `medicalos_quota_denied_total{tenant_id,quota_key}` and MUST write an `AuditEvent`. A tenant locked out by quota is an operational event a site administrator must be able to reconstruct.

### Acceptance criteria

Each check is executable by CI or by a reviewer against a running system. "The dev stack" means `docker compose up` from a clean checkout after `cp .env.example .env` and secret generation.

1. **Skeleton gate.** For every directory under `medos/services/`, the built binary answers `200` on `/healthz`, `/metrics` and `/version`, and answers `/readyz` and `/startupz` with a JSON body of the shape in MOS-OPS-013. Adding a new directory without these fails the build. (MOS-OPS-002)
2. **Liveness independence.** With `postgres` stopped, `/healthz` on `medicalos-api` still returns `200` and `/readyz` returns `503` naming `postgres`. No container restarts within 120 s. (MOS-OPS-012, MOS-OPS-013)
3. **Readiness is not load.** Enqueue 5 000 jobs against one worker replica; `/readyz` returns `200` throughout. (MOS-OPS-014)
4. **Self-test fail-closed.** Corrupt one byte of a pinned model's golden fixture; the worker's `/startupz` stays `503` indefinitely, `medicalos_preprocessing_selftest_total{outcome="fail"}` is ≥ 1, and no job is claimed. Repeat with the fixture intact but the image rebuilt against a `monai` wheel whose `monai.config.USE_COMPILED` differs from the value encoded in `backend.resampler`: the same `503` on `/startupz`, the same counter, the same `PreprocessingSelfTestFailed` alert, and a log line whose `selftest_stage` is `version_assertion` and which names the declared and observed `monai`, `torch` and `numpy` versions and `USE_COMPILED`. (MOS-OPS-015, MOS-IMG-055, MOS-TRAIN-057)
5. **Cardinality bound.** Scrape `/metrics` from every MedicalOS process after a 500-job synthetic run across 3 tenants and 3 services; each process reports fewer than 10 000 active series, and no sample carries a label key from the MOS-OPS-021 forbidden list. (MOS-OPS-021, MOS-OPS-022)
6. **REJECTED is separable.** Run 10 jobs where 4 end `REJECTED` and 1 ends `FAILED`. Assert `medicalos:job_error_ratio:rate30m` ≈ 1/6 (not 5/10) and that `medicalos:job_rejection_ratio:rate30m` reports 4/10 broken down by `reason_code`. (MOS-OPS-024, MOS-OPS-025)
7. **Histogram buckets.** Assert `medicalos_inference_duration_seconds_bucket` contains `le="600"` and `le="1200"`, and that a 420 s inference lands in a finite bucket rather than only `+Inf`. (MOS-OPS-026)
8. **Idempotent write is observable.** Run a job, then re-run it with the same `idempotency_key`. Assert `medicalos_dicom_write_total{outcome="skipped_idempotent"}` increased and `{outcome="stored"}` did not. (MOS-OPS-029)
9. **Gate error ≠ gate pass.** Inject an exception into one validation gate; assert `medicalos_validation_gate_total{outcome="error"}` increments and the `Deployment` promotion is blocked. (MOS-OPS-030)
10. **PHI guard, strict.** With `MEDICALOS_PHI_GUARD_MODE=strict`, a unit test that logs a field containing `1.2.840.113619.2.55.3.604688.1` panics; the same value in a field named `study_ref` after `pseudoref()` does not. `3.2.1` and `0.703x0.703x1.000` never trip the guard. (MOS-OPS-038, MOS-OPS-039)
11. **Log corpus is PHI-free.** Replay a 20-study fixture corpus through the dev stack; grep the entire Loki stream for the DICOM-UID, PN and DA regexes of MOS-OPS-038. Zero matches outside fields whose value is a pseudoref. (MOS-OPS-035, MOS-OPS-039)
12. **Pseudoref properties.** The same `StudyInstanceUID` under two different tenants yields two different pseudorefs; under one tenant it yields the identical pseudoref across processes and across restarts; `POST /internal/v1/pseudoref/resolve` without `phi.reidentify` returns `403` and with it writes an `AuditEvent`. (MOS-OPS-035, MOS-OPS-036, MOS-OPS-037)
13. **Trace shape.** For one completed job, Tempo contains exactly two traces carrying `medicalos.job.id`; the worker-side trace is a root with a link to the `jobqueue.enqueue` span; the span names and kinds match §13.6.1 exactly. (MOS-OPS-044, MOS-OPS-045)
14. **Trace allow-list, collector-enforced.** Emit a span with attribute `url.full` containing a Study Instance UID and a span with attribute `patient.name`; both attributes are absent from the stored span. Repeat with the span originating from a sealed service container. (MOS-OPS-047, MOS-OPS-048, MOS-OPS-050)
15. **Tail sampling retains failures.** With `sampling_percentage: 10`, run 100 `COMPLETED` and 10 `REJECTED` jobs; all 10 `REJECTED` traces are present in Tempo. (MOS-OPS-052)
16. **Alert lint.** Every rule in `deploy/prometheus/alerts.yaml` passes `promtool check rules` and carries either a `runbook` or a `summary` annotation. (MOS-OPS-055)
17. **No ZooKeeper.** `grep -ri zookeeper` over the repository, charts and docs returns zero matches; the `kafka` compose profile starts `apache/kafka:4.0.0` in KRaft mode. (MOS-OPS-058)
18. **PACS isolation.** From the `medicalos-worker`, `svc-pleural-effusion` and `ohif` containers, `curl http://orthanc:8042/dicom-web/studies` fails to resolve or connect. From `medicalos-gateway` it returns `200`. (MOS-OPS-005, MOS-OPS-100)
19. **Service boundary.** The `svc-pleural-effusion` container has no environment variable or mounted file containing a PostgreSQL DSN, object-store credential or PACS credential, and cannot open a TCP connection to `postgres:5432` or `minio:9000`. (MOS-OPS-060, MOS-OPS-100)
20. **Kubernetes agnosticism.** A dependency scan of `medos/services/` finds no import of `k8s.io/client-go`, `sigs.k8s.io/controller-runtime` or the `kubernetes` Python package, and no source reference to `/var/run/secrets/kubernetes.io`. Every container's entrypoint is the binary. (MOS-OPS-061, MOS-OPS-062, MOS-OPS-063)
21. **Drain arithmetic.** `terminationGracePeriodSeconds` ≥ `MEDICALOS_DRAIN_TIMEOUT_SECONDS` + 10 ≥ `MEDICALOS_JOB_TIMEOUT_SECONDS` + 70 in every chart value file and in `compose.yaml`. A `SIGTERM` to a worker mid-job lets the job reach `COMPLETED` and produces no lease expiry. (MOS-OPS-018, MOS-OPS-019)
22. **Triton hardening.** The running Triton process has `--model-control-mode=explicit` and `--response-cache-byte-size=0`; a `config.pbtxt` containing an `execution_accelerators` stanza is rejected by `tritond` at load; a plan whose `built_for.cuda_compute_capability` differs from the device's is rejected with `medicalos_model_load_total{outcome="load_error"}`. (MOS-OPS-070, MOS-OPS-071, MOS-OPS-074)
23. **No dynamic batching.** Every generated `config.pbtxt` has `max_batch_size: 0`, no `dynamic_batching` block, and `instance_group.count: 1` unless a measured justification is recorded on the `ModelVersion`. (MOS-OPS-072, MOS-OPS-073)
24. **Batch invariance.** For each model, inference on the golden fixture at every value in `allowed_patch_batch_sizes` produces bit-identical output tensors. A configured value outside the list is refused at startup. (MOS-OPS-078)
25. **Three models, two slots.** Deploy the three models of §13.10.4 on one 16 GB GPU. Assert: no job reaches `FAILED` or `REJECTED`; `medicalos_queue_oldest_age_seconds{service_id="svc_nodule_det"}` rises; over a 30-minute mixed workload every model is loaded at least once (no starvation); no `on_demand` model is evicted within `MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS` of being loaded; `medicalos_model_load_total{outcome="evicted"}` stays below one eviction per 300 s per model (no thrash). (MOS-OPS-085, MOS-OPS-086, MOS-OPS-087)
26. **Unsatisfiable is FAILED, not REJECTED.** Deploy a model whose footprint exceeds `budget_bytes`; the job terminates `FAILED` with `failure.class = 'internal'` and `failure.code = 'capacity_configuration'`, and no job anywhere in the run is `REJECTED`. (MOS-OPS-083)
27. **Deploy-time over-commitment check.** A `Deployment` whose pinned `resident` footprints exceed `budget_bytes` on every eligible node is refused before it can reach `state = SERVING`, with the shortfall in bytes in the error body. (MOS-OPS-084)
28. **Claim ordering.** Instrument `Claim`: while `svc_nodule_det ∉ admissible_service_ids`, the worker issues zero `Claim` calls for that inbox and `medicalos_job_lease_expired_total` does not increase. (MOS-OPS-081)
29. **Reservation reclaim.** `SIGKILL` a worker holding a residency reservation and a quota reservation; within `ttl_seconds` the residency slot is released, and within the lease-expiry interval `reserved_units` returns to its pre-job value. (MOS-OPS-082, MOS-OPS-110)
30. **Backpressure never terminates.** Drive the system to all four backpressure points simultaneously (queue over `max_queued_jobs`, gateway semaphore saturated, GPU contended, all workers at max in-flight). Assert: zero jobs terminal; zero increments of any job attempt counter attributable to shedding; admission responses are `429` of `class=rate_limit` carrying `Retry-After`, with no `Job` row created. (MOS-OPS-098)
31. **Scale-down safety.** With `scaleDown.stabilizationWindowSeconds: 1800` and a 1800 s job timeout, drain a queue during a running job; no worker pod is terminated before the job completes. (MOS-OPS-096)
32. **Quota reserve/settle round trip.** Submit a job with `p95_gpu_seconds = 180`; assert `reserved_units` increased by 225 at creation and, at terminal state, `reserved_units` returned to baseline and `consumed_units` increased by the measured GPU-seconds, both in a single transaction with the terminal state write. (MOS-OPS-103, MOS-OPS-109)
33. **Quota does not kill clinical work.** With `clinical_use_mode: clinical` and a tenant at 99 % of `gpu_hours_per_day`, start a job that overruns. Assert the job reaches `COMPLETED`, `overage_units` and `medicalos_quota_overage_seconds_total` increase, and the *next* `POST /api/v1/jobs` for that tenant returns `429` with `class=rate_limit` and `code=QUOTA_EXCEEDED`. Assert `quota_hard_stop = true` is rejected on a `clinical` deployment. (MOS-OPS-107, MOS-OPS-108)
34. **Window rollover needs no cron.** Advance the clock past midnight in the tenant's timezone; a new `tenant_quota_ledger` row is created on first use and the tenant's available quota is full, with no scheduled task in any container. (MOS-OPS-111)
35. **Digest pinning (compose from 0.1.0; Helm chart from 0.4.0).** Every image reference in the compose file and, once the chart ships, in the Helm chart is a `sha256:` digest, and the digest reported by `/version` on each running pod equals the digest in the manifest and the digest recorded in the provenance record of jobs that pod executed. (MOS-OPS-017, MOS-OPS-065)
36. **Evictable is evicted first, and without a hold window.** On one GPU, hold `pulmo.lung-seg` at `residency = 'evictable'` and `pulmo.pleural-effusion` at `residency = 'on_demand'`, both resident with no active reservation, and touch them so that the `evictable` model is the *more* recently used of the two. Submit a job for `lidc.nodule-det`. Assert that `pulmo.lung-seg` is the model unloaded although least-recently-used alone would have chosen `pulmo.pleural-effusion`, that it is unloaded before `MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS` has elapsed since its load, and that `pulmo.pleural-effusion` stays resident. (MOS-OPS-086, MOS-OPS-114)

---

[← 12. Data Model and Storage](12-data-model.md) · [Index](../../MEDICALOS_SPEC.md) · [14. Testing and Acceptance →](14-testing.md)
