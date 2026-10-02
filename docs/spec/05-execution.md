<!-- MedicalOS Specification v0.4.0 — chapter 5 of 19. Normative.
     85 requirements. Do not edit without a requirement-ID review. -->

[← 4. Imaging Contracts: Geometry, Preprocessing and DICOM Output](04-imaging-contracts.md) · [Index](../../MEDICALOS_SPEC.md) · [6. Registries, Capability Resolution and Deployment →](06-registries.md)

---

## 5. Execution: Jobs, Queue and Failure Handling

This chapter specifies the execution plane: the `Job` entity, its state machine, the `JobQueue` port and its two drivers, the event envelope, and the failure, retry and idempotency contracts that make a crashed run resumable rather than duplicative.

The v0.1 document stated that jobs must be idempotent, retried with backoff and never lost, and specified none of the three. It also left job state without a declared source of truth (review B8), left the event `payload` as `{}` (review G12), leaked worker-internal stages into the public status enum, and reported progress as an ungrounded float. Everything below is written so that a coding agent can implement it without inventing a semantic.

Chapter boundaries. Chapter 3 owns triage and decides *which* jobs exist; this chapter owns what a `Job` is once it exists. Chapter 6 owns capability resolution; this chapter owns the fact that the resolved result is pinned into the job row. Chapter 4 owns DICOM identity derivation; this chapter owns how that derivation composes with retries. Chapter 10 owns the HTTP surface, the status codes it returns and the RFC 9457 shapes it serialises; this chapter owns the state machine that surface projects. Chapter 12 is the canonical register of all tables and owns the physical schema of the execution-plane tables; this chapter does **not** restate its `CREATE TABLE` statements for `jobs`, `job_steps` and `job_events`, so that one migration creates each table once. What this chapter owns is their execution semantics — the state machine, the lease protocol, the transition function, the rollup trigger — and the field names, enum spellings and value sets those tables render. Every SQL fragment below uses chapter 12 §12.10's spellings: `jobs.id`, `jobs.finished_at`, `jobs.primary_study_id`.

---

### 5.1 The `Job` entity

#### 5.1.1 What a Job is

**MOS-EXEC-004.** A `Job` MUST represent exactly one tuple `(ServiceVersion, Study, selected Series set)`. A study that matches three deployed `ServiceVersion`s produces three `Job` rows, never one job with three targets. A `Job` MUST NOT be created for a `Service` without a resolved `ServiceVersion`.

**MOS-EXEC-005.** Every `Job` row MUST be committed to PostgreSQL **before** any dispatch signal is produced, in the same transaction as the queue row (MOS-EXEC-025). There is no code path that dispatches work for which no job row exists.

**MOS-EXEC-006.** `jobs.target_kind` records how the job named its target. It MUST carry the closed enum `service_version` (an explicit pin) and `capability` (resolved per spine §9 and `MOS-REG-055`, chapter 6) from 0.1.0; the request shape is `MOS-API-044` (chapter 10). Only `service_version` is **accepted at runtime** in 0.1 and 0.2, because spine §14 places capability resolution in 0.3.0. `capability` is nevertheless a member of the schema enum from 0.2.0 so that enabling it in 0.3.0 is not a closed-enum widening (chapter 10, table 10.9-A). A job creation request carrying `target_kind: capability` MUST be refused in 0.1–0.2 with `422`, `class: client_error`, `code: TARGET_KIND_NOT_AVAILABLE`, `detail: "capability targets require capability resolution, delivered in 0.3.0 (spine §14)"`. A request carrying any value outside the enum MUST be refused with `400`, `code: SCHEMA_VIOLATION`. The values `workflow_version` and `agent_version` are RESERVED for 0.4 (spine §13), are not members of the enum, and MUST be refused at admission; the column exists now so that adding them later is additive, not a breaking schema change. Whichever kind was requested, the resolved `jobs.resolved_service_version_id` is pinned into the row at creation and reused verbatim on every retry (MOS-EXEC-008).

**MOS-EXEC-007.** The study reference MUST be an internal surrogate key (`jobs.primary_study_id uuid`, chapter 12 §12.10), not the raw `StudyInstanceUID`, because two tenants ingesting the same public collection would otherwise collide. `studies` carries `UNIQUE (tenant_id, study_instance_uid)` (chapter 12). Prior studies MUST be expressible as an array, and there is exactly one spelling on each side: on the row they are the tail of `jobs.study_ids uuid[]`, whose first element is `primary_study_id` (`MOS-STORE-268`, chapter 12); on the wire and in the payload schemas of §5.14.1 they are `prior_study_instance_uids` beside a single `study_instance_uid`, the shape chapter 10 fixes in `MOS-API-045` and which this chapter MUST NOT respell. Prior-comparison is core chest-CT work and widening a scalar later is breaking.

**MOS-EXEC-008.** The resolution outcome from chapter 6 MUST be pinned into the job row at creation — `resolved_service_version_id`, `service_version`, and a `resolution_snapshot` JSON document recording the ordered candidate list and the reason the winner won (chapter 12 §12.10) — and MUST be reused verbatim on every retry of that job. A retry MUST NOT re-run resolution. This is what makes reproducibility survive a model release landing between attempt 1 and attempt 2.

#### 5.1.2 The types this chapter owns, and the facts the `jobs` row MUST carry

The physical `jobs` table is chapter 12 §12.10 and is not restated here. Chapter 12 owns the primary key (`jobs.id`), the terminal timestamp (`finished_at`), the study reference (`primary_study_id uuid` plus `study_ids uuid[]`, `MOS-STORE-268`), the rendering of every enumerated column as `text` with a `CHECK` rather than a native enum, and the tenant-composite foreign keys.

Two types are nevertheless created by the execution-plane migration and belong to this chapter, because `job_state_transition` and the signature of `job_transition()` are typed on them. `jobs.state` itself stays `text` (`MOS-STORE-266`, chapter 12) and `job_transition()` assigns to it through an explicit `p_to::text` cast:

```sql
CREATE TYPE job_state AS ENUM
  ('CREATED','QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','REJECTED');

CREATE TYPE job_actor AS ENUM
  ('control-plane','job-runner','queue-reclaimer','reconciler','operator');
```

**MOS-EXEC-086.** The `jobs` row MUST carry the execution-plane facts below. This chapter is normative for their names, meanings and value sets; chapter 12 §12.10 is normative for their physical rendering, and a fact for which §12.10 names no column MUST gain one there under the name given here. (New requirement; chapter 5's previous maximum was `MOS-EXEC-085`.)

| Column | What this chapter fixes about it |
|---|---|
| `state` | the closed seven-value enum of MOS-EXEC-001, and nothing else (§5.2) |
| `phase` | an open display string; clients MUST NOT branch on it (MOS-EXEC-019) |
| `steps_total`, `steps_completed` | derived from `job_steps` by the trigger of §5.4.2 and by nothing else (MOS-EXEC-020) |
| `target_kind` | `service_version` or `capability`, with `capability` refused at admission until 0.3.0 (MOS-EXEC-006) |
| `service_id`, `service_version` | the resolved service slug and its semver, denormalised: `service_id` names the work inbox (MOS-EXEC-033) and `service_version` is material for the derived key (MOS-EXEC-053) |
| `capability_ids text[]` | the capabilities the resolved version claims; chapter 6 owns the vocabulary |
| `study_instance_uid` | denormalised source study UID; the second component of the partition key (MOS-EXEC-044) |
| `selected_series_uids text[]` | the selected series set, denormalised, and material for the derived key. The per-series verdicts are `job_series` rows written in the creating transaction (chapter 12, `MOS-STORE-270`), never a `jsonb` blob on this row, and are served by `GET /api/v1/jobs/{job_id}/series-selection` (chapter 10, row 60) |
| `requested_outputs text[]` | default `'{SEG,SR}'`; values drawn from `{SEG, SR, SC}` exactly as spelled in the payload schemas of §5.14.1, with no `DICOM_` prefix |
| `idempotency_key`, `request_idempotency_key` | the platform-derived key (MOS-EXEC-053) and the client header echo (MOS-EXEC-054); they are different fields and MUST NOT be merged |
| `attempt`, `max_attempts`, `load_shed_count` | retry accounting; `attempt` is copied from `job_queue.claim_count` at claim (MOS-EXEC-035) and a shed does not raise it (MOS-EXEC-064) |
| `deadline_at`, `attempt_deadline_at` | absolute timestamps, never durations (MOS-EXEC-070) |
| `reject_reason_code`, `reject_reason_detail` | §5.3.1's closed job-level vocabulary and its free-text detail |
| `failure_class`, `failure_code`, `failure_detail` | §5.3.2's closed class list, the open code within it, and a PHI-safe message (MOS-EXEC-017) |
| `trace_id`, `correlation_id` | the W3C trace-id (32 lowercase hex characters) and the business-level grouping; both `NOT NULL`, and MUST NOT be conflated (MOS-EXEC-076) |
| `parent_job_id`, `root_job_id`, `depth` | job nesting, reserved for 0.4 with `depth = 0` throughout 0.1–0.2 (MOS-EXEC-084) |

Two further invariants are this chapter's and MUST be enforced structurally where §12.10 renders these columns: a job whose state is not `REJECTED` MUST have a non-empty `selected_series_uids`, and there is no `ended_at` — the terminal timestamp is `finished_at`.

**MOS-EXEC-085.** Every execution-plane table (`jobs`, `job_steps`, `job_events`, `job_queue`, `job_dead_letter`, `job_outbox`) MUST carry `tenant_id NOT NULL` and MUST have row-level security bound to `current_setting('medicalos.tenant_id')`. The reclaimer, reconciler and outbox relay run as a separate database role that bypasses RLS and MUST NOT be reachable from any request-handling code path. Chapter 8 owns the role definitions.

#### 5.1.3 Fields deliberately absent

| Absent field | Why |
|---|---|
| `progress float` | No producer, no definition, no transport. Replaced by `phase` + `steps_completed`/`steps_total` (§5.4). A number shown to a clinician must have a source. |
| `permissions[]` snapshot | A snapshot taken at creation outlives a role revocation for the life of the job. Permissions MUST be evaluated per call against the PDP (chapter 8). |
| `worker_id` | Transient claim state belongs in `job_queue.lease_owner`, not in the durable job record. Provenance records the runner build in the `Result` (chapter 9). |
| `timeout_ms` (relative) | Only an absolute deadline survives a queue hop. `deadline_at` and `attempt_deadline_at` are absolute (MOS-EXEC-070). |
| `POSTPROCESSING`, `WAITING` states | Internal stages. See MOS-EXEC-018. |

---

### 5.2 The job state machine

**MOS-EXEC-001.** The public `Job` state enum is exactly: `CREATED`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, `REJECTED`. `COMPLETED`, `FAILED`, `CANCELLED` and `REJECTED` are terminal. No other value may appear in any API response, event payload, database column or UI.

**MOS-EXEC-002.** PostgreSQL is the sole source of truth for job state. The event bus is a derived transport. No component may reconstruct, repair or infer job state by reading the bus, and `GET /api/v1/jobs/{id}` (chapter 10) MUST read the `jobs` table directly, never a projection.

#### 5.2.1 Actors

| Actor | What it is | May transition |
|---|---|---|
| `control-plane` | The API handler and the triage worker (chapter 3) that create jobs | into `CREATED`, `QUEUED`, `REJECTED` |
| `job-runner` | Platform-owned process that claims a lease and executes the step plan | `QUEUED`→`RUNNING`, and out of `RUNNING` |
| `queue-reclaimer` | Driver janitor that handles expired leases | `RUNNING`→`QUEUED`, `RUNNING`→`FAILED` |
| `reconciler` | Periodic invariant sweep | `CREATED`→`FAILED` |
| `operator` | A human with `job.retry` (chapter 8's permission catalogue), acting through the API | `FAILED`→`QUEUED` |

**MOS-EXEC-009.** The **Service** — vendor code, native or sealed — is never an actor. A service MUST NOT transition job state, MUST NOT hold a lease, and MUST NOT reach the database (spine §2). It receives an invocation from the `job-runner` and returns a `ResultBundle` or a typed outcome (chapter 2); the runner alone maps that to a transition. This is the single most important reading of "worker" that the v0.1 document left ambiguous.

#### 5.2.2 Transition table

**MOS-EXEC-010.** The following table is normative and exhaustive. Any transition not listed MUST be rejected by the database (MOS-EXEC-011).

| # | From | To | Actor | Trigger | Guard |
|---|---|---|---|---|---|
| T1 | — | `CREATED` | `control-plane` | `POST /api/v1/jobs`, or a triage match (chapter 3) | resolution returned ≥ 1 candidate; `UNIQUE (tenant_id, idempotency_key)` holds |
| T2 | `CREATED` | `QUEUED` | `control-plane` | same transaction as T1 | `job_queue` row inserted in this transaction |
| T3 | `CREATED` | `REJECTED` | `control-plane` | triage selected zero eligible series, or resolution returned zero candidates | `reject_reason_code` set |
| T4 | `CREATED` | `FAILED` | `reconciler` | still `CREATED` after `create_to_queue_deadline_s` (60 s) | `failure_class = 'internal'`, `failure_code = 'stuck_in_created'` |
| T5 | `QUEUED` | `RUNNING` | `job-runner` | `Claim` succeeded | `available_at <= now()`, lease granted, `fence_token` incremented |
| T6 | `QUEUED` | `FAILED` | `reconciler` | `deadline_at` passed while queued | `failure_code = 'deadline_exceeded'` |
| T7 | `RUNNING` | `REJECTED` | `job-runner` | applicability-envelope gate failed, geometry unsupported, or the service returned a `clinical_rejection` outcome | fence token current; `reject_reason_code` set |
| T8 | `RUNNING` | `COMPLETED` | `job-runner` | `ResultBundle` validated, DICOM stored, result row written | fence token current; result row inserted in the same transaction (MOS-EXEC-057) |
| T9 | `RUNNING` | `QUEUED` | `job-runner` | `Fail` with a retryable class and `claim_count < jobs.max_attempts` | fence token current; `available_at = now() + backoff(claim_count)` |
| T10 | `RUNNING` | `QUEUED` | `queue-reclaimer` | lease expired, `claim_count < jobs.max_attempts` | `lease_expires_at < now()` |
| T11 | `RUNNING` | `FAILED` | `job-runner` | `Fail` with a non-retryable class, or `claim_count >= jobs.max_attempts` | fence token current; DLQ row written in the same transaction |
| T12 | `RUNNING` | `FAILED` | `queue-reclaimer` | lease expired and `claim_count >= jobs.max_attempts` | DLQ row written |
| T13 | `FAILED` | `QUEUED` | `operator` | `POST /api/v1/jobs/{job_id}/retry` (chapter 10, table 10.2-B row 62), permission `job.retry` | clears `failure_class`/`failure_code`/`failure_detail` and `finished_at`, resets `attempt = 0`, audited (chapter 9) |
| T14 | `QUEUED` | `CANCELLED` | — | **RESERVED, unreachable in 0.1–0.2** (MOS-EXEC-073) | — |
| T15 | `RUNNING` | `CANCELLED` | — | **RESERVED, unreachable in 0.1–0.2** (MOS-EXEC-073) | — |

**MOS-EXEC-012.** `COMPLETED`, `REJECTED` and `CANCELLED` are absorbing: no transition out of them exists. `FAILED` is terminal for every automatic actor; the single exception is T13, an explicit, permissioned, audited operator retry. T13 exists because infrastructure failures are routinely fixed out of band, and because deterministic UIDs (MOS-EXEC-059) make a retry resume rather than duplicate. A retry MUST NOT recompute `idempotency_key` and MUST NOT re-run resolution.

#### 5.2.3 Enforcement

**MOS-EXEC-011.** The transition table MUST be stored as data and enforced by the database, not by application code. All state changes MUST go through `job_transition()`; no application role may hold `UPDATE (state)` on `jobs`.

```sql
CREATE TABLE job_state_transition (
  from_state job_state NOT NULL,
  to_state   job_state NOT NULL,
  actor      job_actor NOT NULL,
  label      text      NOT NULL,          -- 'T1' .. 'T13', diffed against the spec in CI
  PRIMARY KEY (from_state, to_state, actor)
);

INSERT INTO job_state_transition (from_state, to_state, actor, label) VALUES
  ('CREATED','QUEUED',   'control-plane',   'T2'),
  ('CREATED','REJECTED', 'control-plane',   'T3'),
  ('CREATED','FAILED',   'reconciler',      'T4'),
  ('QUEUED', 'RUNNING',  'job-runner',      'T5'),
  ('QUEUED', 'FAILED',   'reconciler',      'T6'),
  ('RUNNING','REJECTED', 'job-runner',      'T7'),
  ('RUNNING','COMPLETED','job-runner',      'T8'),
  ('RUNNING','QUEUED',   'job-runner',      'T9'),
  ('RUNNING','QUEUED',   'queue-reclaimer', 'T10'),
  ('RUNNING','FAILED',   'job-runner',      'T11'),
  ('RUNNING','FAILED',   'queue-reclaimer', 'T12'),
  ('FAILED', 'QUEUED',   'operator',        'T13');

CREATE OR REPLACE FUNCTION job_transition(
  p_job_id uuid,
  p_from   job_state[],
  p_to     job_state,
  p_actor  job_actor,
  p_fence  bigint,        -- NULL for actors that hold no lease
  p_detail jsonb
) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  v_from job_state;
  v_seq  integer;
  v_tid  uuid;
BEGIN
  SELECT state::job_state, tenant_id INTO v_from, v_tid FROM jobs WHERE id = p_job_id FOR UPDATE;
  IF v_from IS NULL THEN
    RAISE EXCEPTION 'job % not found', p_job_id USING ERRCODE = 'MOS01';
  END IF;
  IF NOT (v_from = ANY (p_from)) THEN
    RAISE EXCEPTION 'job %: expected one of %, found %', p_job_id, p_from, v_from
      USING ERRCODE = 'MOS02';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM job_state_transition t
                  WHERE t.from_state = v_from AND t.to_state = p_to AND t.actor = p_actor) THEN
    RAISE EXCEPTION 'transition %->% not permitted for actor %', v_from, p_to, p_actor
      USING ERRCODE = 'MOS03';
  END IF;
  IF p_fence IS NOT NULL
     AND EXISTS (SELECT 1 FROM job_queue q
                  WHERE q.job_id = p_job_id AND q.fence_token <> p_fence) THEN
    RAISE EXCEPTION 'stale fence token % for job %', p_fence, p_job_id USING ERRCODE = 'MOS04';
  END IF;

  UPDATE jobs
     SET state       = p_to::text,          -- `text` + CHECK in chapter 12 §12.10
         started_at  = CASE WHEN p_to = 'RUNNING' AND started_at IS NULL THEN now()
                            ELSE started_at END,
         queued_at   = CASE WHEN p_to = 'QUEUED' THEN now() ELSE queued_at END,
         finished_at = CASE WHEN p_to IN ('COMPLETED','FAILED','CANCELLED','REJECTED') THEN now()
                            ELSE NULL END
   WHERE id = p_job_id;

  SELECT coalesce(max(seq), 0) + 1 INTO v_seq FROM job_events WHERE job_id = p_job_id;
  INSERT INTO job_events (job_id, tenant_id, seq, event_type, from_state, to_state, actor, payload)
  VALUES (p_job_id, v_tid, v_seq, 'job.state_changed', v_from::text, p_to::text,
          p_actor::text, p_detail);
  RETURN v_seq;
END;
$$;
```

**MOS-EXEC-013.** `job_events` is append-only at the database role level: `REVOKE UPDATE, DELETE ON job_events FROM medicalos_app`. It is the backing store for `GET /api/v1/jobs/{id}/events` (chapter 10) and the ordering authority for `seq` (MOS-EXEC-078). Its physical definition is chapter 12 §12.10 — `id` primary key, `job_id`, `tenant_id`, `seq`, `event_id`, `event_type`, `from_state`, `to_state`, `phase`, `actor`, `payload`, `trace_id`, `occurred_at`, with `UNIQUE (job_id, seq)` and `UNIQUE (event_id)` — and is not restated here. This chapter defines the execution semantics of those columns: `seq` is per-job monotonic and assigned in the same transaction as the state change (`MOS-STORE-271`, chapter 12), `event_id` is the delivery identity consumers deduplicate on (MOS-EXEC-041), `payload` is the typed document of §5.14.1 and MUST NOT be empty (MOS-EXEC-077), and `from_state`/`to_state` carry the values of the `job_state` enum above.

---

### 5.3 `REJECTED` versus `FAILED`

**MOS-EXEC-014.** `REJECTED` means *this study was not analysed, and that is a correct outcome*. `FAILED` means *this study should have been analysed and the platform could not do it*. The distinction is clinical, not cosmetic: a radiologist who sees a red error where the truth is "no thin axial recon exists in this study" will either chase an IT ticket or, worse, assume the study was cleared.

**MOS-EXEC-015.** A `REJECTED` job MUST NOT be retried, MUST NOT consume retry budget, MUST NOT be dead-lettered, and MUST NOT raise an on-call alert. Its queue row is deleted in the same transaction as the transition.

**MOS-EXEC-016.** `REJECTED` MUST be surfaced as RFC 9457 `class: "clinical_rejection"` (chapter 10) and MUST be rendered with non-error affordances in every UI. A `FAILED` job MUST be surfaced with `class: "system_failure"` or `class: "transport_failure"`.

#### 5.3.1 `REJECTED` reason codes

The database column `jobs.reject_reason_code` (chapter 12) carries the code below, and `jobs.reject_reason_detail` carries the human-readable detail. The API member `Job.rejection` is an RFC 9457 problem document of class `clinical_rejection` (`MOS-API-040`, chapter 10), whose `code` is the SCREAMING_SNAKE form of this `reason_code` and whose `series_selection_href` points at `GET /api/v1/jobs/{job_id}/series-selection`. This chapter does not define the wire shape; chapter 10 does. The internal record is:

```json
{
  "reject_reason_code": "no_eligible_series",
  "reject_reason_detail": "study contains 4 series; 0 satisfy SeriesSelector of pulmo.effusion@1.4.2"
}
```

The per-series verdicts that justify the code are **not** a member of this record and not a member of the API's `rejection` document. They are persisted as `job_series` rows (chapter 12, `MOS-STORE-270`) and served by `GET /api/v1/jobs/{job_id}/series-selection` (chapter 10, table 10.2-B row 60), one row per evaluated series with `decision`, `reason_code` and `reason_detail`. The `reason_code` vocabulary for a *series* rejection is chapter 3's closed `SeriesRequirement` list (`MOS-DATA-069`), in lowercase snake_case — for example `image_type_excluded` or `slice_thickness_out_of_range`. The `job`-level vocabulary is the table below, and the two are different lists.

| `reason_code` | Example that produces it | Decided by |
|---|---|---|
| `no_eligible_series` | Study holds a localizer, a 5 mm soft-kernel recon and an X-Ray Radiation Dose SR; the selector demands a ≤1.5 mm lung-kernel axial recon | triage, chapter 3 (T3) |
| `no_candidate_service_version` | Resolution returned zero candidates — from 0.3.0, `capability: lung_nodule` requested in a tenant where no `lung_nodule` deployment exists; in 0.1–0.2, the pinned `ServiceVersion` is no longer deployed for the tenant (capability targets are refused at admission until 0.3.0, MOS-EXEC-006) | resolution, chapter 6 (T3) |
| `outside_applicability_envelope` | Selected series is 0.5 mm from a 2026 photon-counting scanner; the `ServiceVersion` envelope declares 1.0–3.0 mm from energy-integrating CT | runner pre-flight (T7) |
| `unsupported_geometry` | `GantryDetectorTilt = 12.4°` and the `ModelVersion` declares `gantry_tilt: reject` (chapter 4) | runner pre-flight (T7) |
| `input_constraint_unmet` | Service requires a portal-venous phase; triage classified the study as non-contrast | runner pre-flight (T7) |
| `policy_denied` | Deployment `clinical_use_mode = clinical` but the `ServiceVersion` carries no `legal_manufacturer` identity (chapter 9) | control-plane or runner (T3/T7) |
| `service_declined` | The service returned the typed `clinical_rejection` outcome of the medical service contract, e.g. "coverage insufficient: lung apices truncated" | service, mapped by runner (T7) |

#### 5.3.2 `FAILED` failure classes

A failure is recorded in three `jobs` columns — `jobs.failure_class`, `jobs.failure_code` and `jobs.failure_detail` (chapter 12's column names, §12.10) — not in a single `jsonb` member:

```json
{
  "failure_class": "dicom_store_failed",
  "failure_code": "stow_partial_failure",
  "failure_detail": "STOW-RS returned 202 with 3 FailedSOPSequence items (0110 processing failure)"
}
```

`step_key`, `attempt` and `max_attempts` are not duplicated into these columns: `attempt` and `max_attempts` are already columns on `jobs`, and the failing `step_key` is carried in the `job.failed` event payload (§5.14.1) and in the `job_dead_letter` row (§5.12.3). `retryable` is a pure function of `failure_class` per the table below and is therefore never stored. The class list is closed at twelve values; `failure_code` is open within a class, and that is where `output_implausible` lives (chapter 7 `MOS-EVID-110`/`MOS-EVID-111`, chapter 2 `MOS-SVC-097`), not in the class column.

| `failure_class` | Retryable | Counts against budget | Default `max_attempts` | Example |
|---|---|---|---|---|
| `transient_infrastructure` | yes | yes | 5 | Postgres connection reset mid-step |
| `gateway_unavailable` | yes | yes | 5 | DICOM Gateway returns 503 for the whole WADO-RS fetch |
| `service_unavailable` | yes | yes | 5 | Sealed-mode container not ready; deployment scaled to zero |
| `service_crashed` | yes | yes | 3 | Container OOM-killed at 14 GB during sliding-window inference |
| `lease_expired` | yes | yes | 3 | Runner node lost power mid-inference |
| `dicom_store_failed` | yes | yes | 5 | STOW-RS transport error or partial failure |
| `load_shed` | yes | **no** | n/a | Service returned 503 with `Retry-After: 30`; see MOS-EXEC-064 |
| `invalid_result_bundle` | no | — | 1 | `ResultBundle` fails JSON-Schema validation; label map is not in canonical geometry; or a plausibility rule returned `fail`, recorded as `failure_code = 'output_implausible'` plus the rule id (chapter 7, `MOS-EVID-110`) — no DICOM object is written and the bundle is quarantined |
| `preprocessing_selftest_failed` | no | — | 1 | Golden-fixture tensor hash mismatch at worker startup (chapter 4) |
| `dicom_write_failed` | no | — | 1 | `dciodvfy` reports Type 1 attribute absent in the generated SEG |
| `deadline_exceeded` | no | — | 1 | `deadline_at` passed |
| `internal` | no | — | 1 | Unhandled panic in the runner; `stuck_in_created` |

**MOS-EXEC-016a.** The API member `Job.error` (`MOS-API-040`, chapter 10) is **not** this object. It is an RFC 9457 problem document whose `class` is derived by this fixed map: `transient_infrastructure`, `gateway_unavailable`, `service_unavailable`, `service_crashed`, `lease_expired`, `dicom_store_failed`, `load_shed` → `transport_failure`; `invalid_result_bundle`, `preprocessing_selftest_failed`, `dicom_write_failed`, `deadline_exceeded`, `internal` → `system_failure`. Its `code` is the SCREAMING_SNAKE form of `failure_code`. The class values of the table above never appear on the wire. The twelve classes of §5.3.2 are the complete, closed set and `output_implausible` is **not** one of them: a plausibility failure is `failure_class = 'invalid_result_bundle'` with `failure_code = 'output_implausible'`, so it surfaces as `class: "system_failure"`, `code: "OUTPUT_IMPLAUSIBLE"`.

**MOS-EXEC-017.** `jobs.failure_detail`, and the `message` member of the `job.failed` payload that carries it, MUST be safe to log and to put on the event bus: no `PatientName`, `PatientID`, `PatientBirthDate`, `AccessionNumber`, `StudyDate`, `StudyDescription`, `InstitutionName`, and no burned-in text extract (chapter 8). DICOM UIDs are permitted (MOS-EXEC-079).

---

### 5.4 Phase and steps: reporting progress without inventing a number

**MOS-EXEC-018.** `POSTPROCESSING`, `WAITING`, `PREPROCESSING`, `UPLOADING` and every other internal stage MUST NOT appear in the public state enum. Adding a pipeline stage MUST NOT be a breaking API change.

**MOS-EXEC-019.** `jobs.phase` is an **open** string field. Clients MUST treat unknown values as opaque display text and MUST NOT branch on them. Clients MUST NOT infer progress from `phase`.

**MOS-EXEC-020.** `steps_total` and `steps_completed` MUST be derived from `job_steps` rows and from nothing else:

- `steps_total` = the number of `job_steps` rows written by the runner in the **plan transaction**, which is the same transaction as T5 (`QUEUED`→`RUNNING`).
- `steps_completed` = `count(*) WHERE status IN ('succeeded','skipped')`.

They are maintained by a trigger on `job_steps`, so the two can never drift from the rows they summarise.

**MOS-EXEC-021.** There is no float `progress` field anywhere in the system: not in the database, not in the API, not in the event envelope, not in the SDK. A client that wants a bar computes `steps_completed / steps_total` itself and MUST render it as a step counter when `steps_total = 0` (job not yet planned). A status-to-percentage lookup table is forbidden.

**MOS-EXEC-022.** The step plan is immutable once the plan transaction commits: `steps_total` MUST NOT change for the life of an attempt. A runner that discovers it needs a step it did not plan MUST fail the attempt with `class = 'internal'`, `code = 'step_plan_violation'` rather than mutate the plan. On a retry, a fresh plan is written with the same `step_key` set; `job_steps` rows are replaced, `attempt` on each row is set to the job's current attempt.

#### 5.4.1 Reserved platform step keys

**MOS-EXEC-023.** The following `step_key` values are reserved. Their `phase` strings are the platform's canonical display strings and MUST NOT be reused with a different meaning.

| `step_key` | `phase` | Owner | Skippable | Default timeout |
|---|---|---|---|---|
| `fetch_series` | `retrieving` | platform | no | 900 s |
| `build_volume` | `building_volume` | platform | no | 300 s |
| `envelope_check` | `checking_applicability` | platform | no | 30 s |
| `service_invoke` | `inferring` | service | no | 1800 s |
| `validate_bundle` | `validating_result` | platform | no | 60 s |
| `write_dicom` | `writing_dicom` | platform | **yes** — skipped when the derived series already exists (MOS-EXEC-059) | 300 s |
| `store_dicom` | `storing_dicom` | platform | **yes** — same condition | 600 s |
| `persist_result` | `persisting` | platform | no | 30 s |

For a job with `requested_outputs = {SEG, SR}` the plan is these eight rows, so `steps_total = 8`. De-identification is not a job step: it is performed by the DICOM Gateway inside `fetch_series` (chapter 3).

**MOS-EXEC-024.** A service MAY report sub-progress during `service_invoke` through the medical service contract (chapter 2). Such a report MUST update `jobs.phase` and `job_steps.detail` for the `service_invoke` row only, e.g. `{"service_phase": "sliding_window", "patches_done": 184, "patches_total": 512}`. It MUST NOT change `steps_total` or `steps_completed`. The job-level counters stay platform-owned so that a misbehaving vendor cannot fabricate platform progress.

#### 5.4.2 `job_steps` status values and the rollup trigger

The physical `job_steps` table is chapter 12 §12.10 — `UNIQUE (job_id, step_index)`, `UNIQUE (job_id, step_key)`, the `step_key`, `phase`, `owner`, `status`, `skip_reason`, `attempt`, `timeout_s`, `input_artifact_ids`, `output_artifact_ids` and `detail` columns, `started_at`/`finished_at`, `CHECK ((status = 'skipped') = (skip_reason IS NOT NULL))`, and the foreign key to `jobs` with `ON DELETE CASCADE` — and is not restated here. What this chapter fixes is the status value set and the rollup:

```sql
-- The five values are this chapter's and the list is closed. Chapter 12 §12.10 renders
-- `job_steps.status` as `text` with a CHECK over exactly these values; the type is created
-- by the execution-plane migration for the runner's own function signatures.
CREATE TYPE job_step_status AS ENUM ('pending','running','succeeded','skipped','failed');

CREATE OR REPLACE FUNCTION job_steps_rollup() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE jobs j
     SET steps_total     = (SELECT count(*) FROM job_steps s WHERE s.job_id = j.id),
         steps_completed = (SELECT count(*) FROM job_steps s
                             WHERE s.job_id = j.id AND s.status IN ('succeeded','skipped')),
         phase           = coalesce((SELECT s.phase FROM job_steps s
                                      WHERE s.job_id = j.id AND s.status = 'running'
                                      ORDER BY s.step_index LIMIT 1), j.phase)
   WHERE j.id = coalesce(NEW.job_id, OLD.job_id);
  RETURN NULL;
END;
$$;

CREATE TRIGGER job_steps_rollup_trg
AFTER INSERT OR UPDATE OR DELETE ON job_steps
FOR EACH ROW EXECUTE FUNCTION job_steps_rollup();
```

`input_artifact_ids` / `output_artifact_ids` reference the `ExecutionArtifact` rows that carry the canonical volume and label maps between steps, including spacing, origin, direction and source series UID (chapter 12). They are what lets `write_dicom` verify it is operating on the same grid `build_volume` produced.

---

### 5.5 The `JobQueue` port

**MOS-EXEC-025.** All dispatch MUST go through the `JobQueue` port. No component may write to `job_queue` or produce to a work topic directly. The port has exactly six methods; a driver MUST implement all six and MUST pass the conformance suite of MOS-EXEC-032.

```go
// Package queue defines the JobQueue port (MOS-EXEC-025).
// Drivers: postgres (0.1+, MOS-EXEC-034..038); kafka (0.3+, MOS-EXEC-039..042).
package queue

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// Tx is the caller's open transaction. Enqueue, Complete and Fail are transactional:
// they MUST execute inside the transaction that also writes the job row (Enqueue)
// or the result row (Complete). See MOS-EXEC-057.
type Tx = pgx.Tx

type Message struct {
	JobID        uuid.UUID
	TenantID     uuid.UUID
	ServiceID    string          // selects the work inbox: job_queue.queue = "medicalos.svc.<ServiceID>.work"
	PartitionKey string          // "<tenant_id>:<study_instance_uid>" — MOS-EXEC-044
	Priority     int16           // 0 highest .. 200 lowest; default 100
	VisibleAt    time.Time       // persisted as job_queue.available_at; now() for immediate dispatch
	MaxAttempts  int16           // recorded on jobs.max_attempts; job_queue holds no budget copy
	Envelope     json.RawMessage // events/job.dispatch/1-0-0.json — MOS-EXEC-075
}

type EnqueueResult struct {
	Enqueued bool      // false ⇒ (tenant_id, idempotency_key) already existed; no row written
	JobID    uuid.UUID // the winning job id (the pre-existing one when Enqueued == false)
}

type ClaimRequest struct {
	ServiceID     string
	RunnerID      string        // stable per process, e.g. "runner-7f3a@pod-effusion-2"
	Max           int           // 1..16; callers MUST pass their free-slot count
	LeaseDuration time.Duration // MUST be >= 4 * heartbeat interval
}

type LeaseToken struct {
	JobID      uuid.UUID
	RunnerID   string
	FenceToken int64 // monotonic per job; incremented on every claim — MOS-EXEC-027
}

type Lease struct {
	Token             LeaseToken
	TenantID          uuid.UUID
	Envelope          json.RawMessage
	Attempt           int16
	MaxAttempts       int16
	ExpiresAt         time.Time
	AttemptDeadlineAt time.Time // absolute — MOS-EXEC-070
}

type Progress struct {
	Phase          string
	StepsCompleted int16
	StepsTotal     int16
}

type HeartbeatResult struct {
	Valid           bool      // false ⇒ lease lost; the runner MUST abandon all work
	ExpiresAt       time.Time
	CancelRequested bool      // always false in 0.1–0.2 — MOS-EXEC-073
}

type FailureClass string

const (
	ClassTransientInfrastructure  FailureClass = "transient_infrastructure"
	ClassGatewayUnavailable       FailureClass = "gateway_unavailable"
	ClassServiceUnavailable       FailureClass = "service_unavailable"
	ClassServiceCrashed           FailureClass = "service_crashed"
	ClassLeaseExpired             FailureClass = "lease_expired"
	ClassDicomStoreFailed         FailureClass = "dicom_store_failed"
	ClassLoadShed                 FailureClass = "load_shed"
	ClassInvalidResultBundle      FailureClass = "invalid_result_bundle"
	ClassPreprocessingSelfTest    FailureClass = "preprocessing_selftest_failed"
	ClassDicomWriteFailed         FailureClass = "dicom_write_failed"
	ClassDeadlineExceeded         FailureClass = "deadline_exceeded"
	ClassInternal                 FailureClass = "internal"
	// There is deliberately no ClassOutputImplausible: a plausibility failure is
	// ClassInvalidResultBundle with Failure.Code == "output_implausible"
	// (chapter 7, MOS-EVID-110; chapter 2, MOS-SVC-097).
)

type Failure struct {
	Class      FailureClass
	Code       string
	Message    string        // MUST NOT contain PHI — MOS-EXEC-017
	StepKey    string
	RetryAfter time.Duration // honoured for load_shed and service_unavailable
}

type FailResult struct {
	NextState    string    // "QUEUED" or "FAILED"
	VisibleAt    time.Time // written to job_queue.available_at
	DeadLettered bool
}

var (
	ErrLeaseLost                  = errors.New("queue: lease lost or fenced")
	ErrNotFound                   = errors.New("queue: job not found")
	ErrCancellationNotImplemented = errors.New("queue: cancellation reserved until 0.3 (MOS-EXEC-073)")
)

type JobQueue interface {
	Enqueue(ctx context.Context, tx Tx, m Message) (EnqueueResult, error)
	Claim(ctx context.Context, req ClaimRequest) ([]Lease, error)
	Heartbeat(ctx context.Context, t LeaseToken, p Progress) (HeartbeatResult, error)
	Complete(ctx context.Context, tx Tx, t LeaseToken) error
	Fail(ctx context.Context, tx Tx, t LeaseToken, f Failure) (FailResult, error)
	Cancel(ctx context.Context, jobID uuid.UUID, reason string) error
}
```

#### 5.5.1 Method semantics

| Method | Transactional | Performs | Idempotent | Errors |
|---|---|---|---|---|
| `Enqueue` | **yes** — MUST be the caller's tx | T2 | yes, on `(tenant_id, idempotency_key)` | `ErrNotFound` if the job row is not in this tx |
| `Claim` | no (own tx) | T5 | n/a — returns 0..Max leases, never blocks | — |
| `Heartbeat` | no (own tx) | none | yes | `ErrLeaseLost` |
| `Complete` | **yes** — MUST be the tx that writes `results` | T8 | yes — a second call with the same fence is a no-op | `ErrLeaseLost` on stale fence |
| `Fail` | **yes** | T9 / T11 | yes | `ErrLeaseLost` |
| `Cancel` | n/a | none | n/a | always `ErrCancellationNotImplemented` in 0.1–0.2 |

**MOS-EXEC-026.** `Claim` MUST NOT block. Runners obtain work by calling `Claim` when a notification arrives *or* when the poll timer fires, whichever is first. `Claim` MUST return at most `req.Max` leases and MUST NOT return a lease for a job whose `available_at > now()`. A runner MUST call `Claim` only when it has that many free execution slots — this is how load-shedding avoids consuming retry budget (MOS-EXEC-064).

**MOS-EXEC-027.** Every driver MUST implement **fencing**. `fence_token` is a per-job monotonically increasing integer, incremented on every successful claim. `Heartbeat`, `Complete` and `Fail` MUST verify the token against the current queue-row value and MUST fail with `ErrLeaseLost` on mismatch. Without fencing, a runner whose process froze for three minutes wakes up after its lease was reclaimed and another runner took the job, and then writes a result for a job it no longer owns.

**MOS-EXEC-028.** `Heartbeat` extends the lease to `now() + LeaseDuration`, writes `Progress` into `jobs.phase`/`job_steps`, and returns `Valid = false` when the token is stale. A runner that receives `Valid = false` MUST abort immediately, MUST NOT call `Complete` or `Fail`, MUST NOT STOW any DICOM object, and MUST release GPU memory.

**MOS-EXEC-029.** `Complete` MUST be called inside the transaction that inserts the `results` row (MOS-EXEC-057). It deletes the queue row and performs T8.

**MOS-EXEC-030.** `Fail` decides the next state from the class table of §5.3.2, the current `attempt`, and `max_attempts`, and returns the decision to the caller. The caller MUST NOT make that decision itself.

**MOS-EXEC-031.** `Cancel` MUST return `ErrCancellationNotImplemented` in 0.1 and 0.2 and MUST NOT mutate any row. See §5.10.

**MOS-EXEC-032.** A single driver conformance suite MUST exist, parameterised over the driver, and both drivers MUST pass it unchanged. It MUST cover, at minimum: exactly-once claim under 32 concurrent claimers; fencing after a simulated lease expiry; `Enqueue` dedup under two concurrent creations with the same derived key; `Fail(load_shed)` not consuming budget; reclaim after a hard process kill; and the crash-point matrix of MOS-EXEC-060.

#### 5.5.2 The work inbox is a port concept, not a broker concept

**MOS-EXEC-033.** The spine's "per-service work inbox" has one name in both drivers, `medicalos.svc.<service_id>.work`: in driver 1 it is a value in `job_queue.queue` and the inbox is the row set `job_queue WHERE queue = 'medicalos.svc.<service_id>.work'`; in driver 2 it is the Kafka topic of that name. Because the driver-1 inbox is a column value and not a schema object, registering a `ServiceVersion` (chapter 6) MUST NOT require any broker or DDL operation under driver 1; this absence of provisioning is a deliberate reason to ship driver 1 first.

---

### 5.6 Driver 1 — PostgreSQL `SKIP LOCKED` (0.1+)

#### 5.6.1 `job_queue` DDL

```sql
CREATE TABLE job_queue (
  job_id           uuid        PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  tenant_id        uuid        NOT NULL,
  queue            text        NOT NULL,   -- 'medicalos.svc.<service_id>.work' — MOS-EXEC-033
  partition_key    text        NOT NULL,
  priority         smallint    NOT NULL DEFAULT 100,
  available_at     timestamptz NOT NULL DEFAULT now(),
  claim_count      smallint    NOT NULL DEFAULT 0,
  lease_owner      text,
  lease_expires_at timestamptz,
  fence_token      bigint      NOT NULL DEFAULT 0,
  enqueued_at      timestamptz NOT NULL DEFAULT now(),
  envelope         jsonb       NOT NULL,
  CONSTRAINT job_queue_lease_ok
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE INDEX job_queue_claim_idx
  ON job_queue (queue, priority, available_at, enqueued_at)
  WHERE lease_owner IS NULL;

CREATE INDEX job_queue_expiry_idx
  ON job_queue (lease_expires_at)
  WHERE lease_owner IS NOT NULL;

ALTER TABLE job_queue ENABLE ROW LEVEL SECURITY;
CREATE POLICY job_queue_tenant ON job_queue
  USING (tenant_id = current_setting('medicalos.tenant_id')::uuid);
```

Column names are chapter 12's (`queue`, `available_at`, `claim_count` — §12.10). Two columns of this table carry protocol semantics that exist nowhere else and that chapter 12 registers by reference to this section: `fence_token` (lease fencing, MOS-EXEC-027) and `envelope` (the dispatch payload, MOS-EXEC-075). The retry **budget** is not duplicated here: `claim_count` counts claims of this job, and the ceiling it is compared against is `jobs.max_attempts` (MOS-EXEC-063).

#### 5.6.2 Enqueue — one transaction, no outbox

**MOS-EXEC-034.** The job row and the queue row MUST be inserted in one transaction. This is the whole reason driver 1 ships first: there is no dual write, so there is no outbox and no crash window in which a job exists but is invisible to workers, or is dispatched without a row.

```sql
BEGIN;
  INSERT INTO jobs (id, tenant_id, target_kind, service_id, requested_service_version_id,
                    resolved_service_version_id, service_version, resolution_snapshot,
                    primary_study_id, study_ids, study_instance_uid,
                    selected_series_uids, requested_outputs, clinical_use_mode,
                    idempotency_key, max_attempts, deadline_at, root_job_id,
                    trace_id, correlation_id, created_by_kind, created_by_id)
  VALUES ($1, $2, 'service_version', 'pulmo.effusion', $3, $3, '1.4.2', $4, $5, ARRAY[$5], $6, $7,
          '{SEG,SR}', 'research_only', $8, 5, now() + interval '6 hours', $1,
          $9, $10, 'triage', 'triage-worker')
  ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
  RETURNING id;
  -- zero rows returned ⇒ the job already exists; the caller returns it and skips the enqueue.

  -- The per-series verdicts — one row per evaluated series, selected and rejected alike,
  -- with the chapter 3 reason code — are inserted into job_series in this same transaction
  -- (chapter 12, MOS-STORE-270). They are rows, never a jsonb blob on the job.

  INSERT INTO job_queue (job_id, tenant_id, queue, partition_key, envelope)
  VALUES ($1, $2, 'medicalos.svc.pulmo.effusion.work', $2 || ':' || $6, $11);

  SELECT job_transition($1, ARRAY['CREATED']::job_state[], 'QUEUED', 'control-plane', NULL,
                        '{"reason":"enqueued"}'::jsonb);
COMMIT;
```

#### 5.6.3 Claim

```sql
WITH candidate AS (
  SELECT q.job_id
    FROM job_queue q
   WHERE q.queue       = $1          -- 'medicalos.svc.<service_id>.work'
     AND q.lease_owner IS NULL
     AND q.available_at <= now()
   ORDER BY q.priority, q.available_at, q.enqueued_at
     FOR UPDATE SKIP LOCKED
   LIMIT $2
)
UPDATE job_queue q
   SET lease_owner      = $3,
       lease_expires_at = now() + ($4 || ' seconds')::interval,
       fence_token      = q.fence_token + 1,
       claim_count      = q.claim_count + 1
  FROM candidate c, jobs j
 WHERE q.job_id = c.job_id AND j.id = q.job_id
RETURNING q.job_id, q.tenant_id, q.fence_token, q.claim_count, j.max_attempts,
          q.lease_expires_at, q.envelope;
```

The caller then, in the same transaction, writes the step plan and calls `job_transition(..., 'RUNNING', 'job-runner', fence_token, ...)` for each returned row, and sets `jobs.attempt = claim_count` and `jobs.attempt_deadline_at = now() + attempt_deadline_s`.

**MOS-EXEC-035.** `job_queue.claim_count` MUST be incremented in exactly one statement in the entire system — the claim statement above, which also copies it into `jobs.attempt` in the same transaction. Neither `Fail`, nor the reclaimer, nor the reconciler may increment it. Single-sited accounting is what makes "load-shed must not consume retry budget" a checkable property rather than an aspiration. Consequently the first delivery of a job carries `claim_count = 1` and `jobs.attempt = 1`.

**MOS-EXEC-036.** `Claim` MUST NOT pick up rows whose lease has merely expired. Expired leases are the reclaimer's job. Mixing the two into one query hides the retry-budget decision inside a hot path and makes the DLQ transition unreachable in the common case.

#### 5.6.4 Reclaim

The reclaimer runs every `reclaim_interval_s` (default 15) as a privileged role.

```sql
-- (a) expired lease, budget remains: release with backoff  → T10
WITH expired AS (
  SELECT q.job_id
    FROM job_queue q
    JOIN jobs j ON j.id = q.job_id
   WHERE q.lease_owner IS NOT NULL
     AND q.lease_expires_at < now()
     AND q.claim_count < j.max_attempts
   ORDER BY q.lease_expires_at
     FOR UPDATE OF q SKIP LOCKED
   LIMIT 100
)
UPDATE job_queue q
   SET lease_owner      = NULL,
       lease_expires_at = NULL,
       available_at     = now() + job_backoff(q.claim_count)
  FROM expired e
 WHERE q.job_id = e.job_id
RETURNING q.job_id, q.claim_count, q.available_at;

-- (b) expired lease, budget exhausted: dead-letter        → T12
WITH dead AS (
  SELECT q.job_id
    FROM job_queue q
    JOIN jobs j ON j.id = q.job_id
   WHERE q.lease_owner IS NOT NULL
     AND q.lease_expires_at < now()
     AND q.claim_count >= j.max_attempts
   ORDER BY q.lease_expires_at
     FOR UPDATE OF q SKIP LOCKED
   LIMIT 100
)
DELETE FROM job_queue q USING dead d WHERE q.job_id = d.job_id
RETURNING q.job_id, q.tenant_id, q.claim_count, q.envelope;
```

For each row from (a) the reclaimer calls `job_transition(job_id, ARRAY['RUNNING'], 'QUEUED', 'queue-reclaimer', NULL, ...)`. For each row from (b) it inserts a `job_dead_letter` row and calls `job_transition(..., 'FAILED', 'queue-reclaimer', ...)` with `failure_class = 'lease_expired'`, both in one transaction.

```sql
CREATE OR REPLACE FUNCTION job_backoff(p_attempt integer)
RETURNS interval LANGUAGE sql VOLATILE AS $$
  SELECT make_interval(secs => (d / 2.0) + random() * (d / 2.0))
    FROM (SELECT least(600.0, 5.0 * power(2.0, greatest(p_attempt, 1) - 1)) AS d) s;
$$;
```

#### 5.6.5 `LISTEN` / `NOTIFY`

```sql
CREATE OR REPLACE FUNCTION job_queue_notify() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('mos_jobq_' || encode(digest(NEW.queue, 'sha256'), 'hex'), '');
  RETURN NULL;
END;
$$;

CREATE TRIGGER job_queue_notify_trg
AFTER INSERT ON job_queue FOR EACH ROW EXECUTE FUNCTION job_queue_notify();
```

**MOS-EXEC-037.** `LISTEN`/`NOTIFY` is a latency optimisation and MUST NOT be relied upon for correctness. Every runner MUST also poll `Claim` at `claim_poll_interval_s` (default 2, MUST be ≤ 5). Three reasons, all of which produce a permanently stuck job if ignored: (i) `NOTIFY` is delivered only to sessions listening at the moment of commit, so a runner restarting during an enqueue burst loses those notifications outright; (ii) no notification fires when a row becomes claimable because `available_at` elapsed, which is every backoff retry; (iii) the notify payload is capped at 8000 bytes and the channel name at 63 bytes, so the channel is a hash of the `queue` name and carries no job identity — a listener must query anyway.

#### 5.6.6 Operating envelope

**MOS-EXEC-038.** Driver 1 MUST be operated within a declared envelope, and exceeding it is the documented trigger for driver 2, not a reason to tune driver 1:

| Quantity | 0.1–0.2 target | Driver-1 limit |
|---|---|---|
| Job creation rate | ≤ 20 / min | ≤ 200 / s |
| Concurrent runner processes per service | ≤ 8 | ≤ 64 |
| `Claim` rate | ≤ 4 / s | ≤ 50 / s |
| `LISTEN` connections | ≤ 16 | ≤ 100 |
| Rows in `job_queue` | ≤ 10 000 | ≤ 1 000 000 |
| Independent durable consumers of lifecycle events | 1 (SSE, via `job_events`) | 1 |

The last row is the honest reason driver 2 exists: webhooks, the audit sink and the evidence collector each want an independent durable cursor, and `LISTEN`/`NOTIFY` gives none of them one. Until there is more than one such consumer, a broker is a liability.

---

### 5.7 Driver 2 — Kafka, deferred to 0.3, and the outbox it needs

**MOS-EXEC-039.** The Kafka driver is deferred to 0.3.0 (spine §14). It MUST NOT be a dependency of 0.1 or 0.2, MUST NOT appear in the 0.1 compose file, and its absence MUST NOT be worked around by any component reaching for Kafka directly.

**MOS-EXEC-040.** Driver 2 introduces a dual write — the job row goes to Postgres, the dispatch message goes to the broker — and therefore MUST use a transactional outbox. The outbox exists for driver 2 only; introducing it in 0.1 would be cost with no benefit.

```sql
-- Created by the 0.3.0 migration together with queue driver 2, NOT by the 0.1 bootstrap
-- migration (MOS-EXEC-039, MOS-EXEC-040; spine §5). Column names are chapter 12's (§12.10).
CREATE TABLE job_outbox (
  id            bigserial   PRIMARY KEY,
  tenant_id     uuid        NOT NULL,
  job_id        uuid        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  topic         text        NOT NULL,
  partition_key text        NOT NULL,
  envelope      jsonb       NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  last_error    text,
  attempts      smallint    NOT NULL DEFAULT 0
);

CREATE INDEX job_outbox_pending_idx ON job_outbox (id) WHERE published_at IS NULL;
```

**MOS-EXEC-041.** The relay MUST:

1. Run as exactly one active instance, elected by a Postgres advisory lock (`pg_try_advisory_lock(hashtext('mos.outbox.relay'))`).
2. Read pending rows in `id` order, in batches of ≤ 500, with `FOR UPDATE SKIP LOCKED`.
3. Produce with `enable.idempotence=true`, `acks=all`, `max.in.flight.requests.per.connection=5`, `compression.type=zstd`.
4. Set `published_at` only after the broker acknowledges, so the guarantee is at-least-once.
5. Never delete an unpublished row. Published rows are deleted after 7 days.
6. Emit `medicalos_outbox_lag_seconds` = `now() - min(created_at) WHERE published_at IS NULL`, with an alert above 60 s.

Consumers MUST deduplicate on `envelope.event_id`, which is unique per event (`job_events.event_id`, `UNIQUE`).

**MOS-EXEC-042.** Kafka MUST be operated in KRaft mode. ZooKeeper mode was removed in Kafka 4.0 and MUST NOT appear in any compose file, Helm chart or compatibility matrix.

---

### 5.8 Topics, partitioning, and why there are no modality or nosology topics

**MOS-EXEC-003.** The topic set is closed. Adding a topic is a broker-level operational change and MUST require a spec amendment.

| Topic | Event types carried | Partition key | Partitions | Retention | Producers | Consumers |
|---|---|---|---|---|---|---|
| `medicalos.jobs.requested` | `job.requested` | `<tenant_id>:<study_instance_uid>` | 12 | 7 d | control-plane (relay) | webhook dispatcher, metrics |
| `medicalos.jobs.completed` | `job.completed`, `job.rejected` | `<tenant_id>:<study_instance_uid>` | 12 | 7 d | job-runner (relay) | webhook dispatcher, evidence collector |
| `medicalos.jobs.failed` | `job.failed` | `<tenant_id>:<study_instance_uid>` | 12 | 30 d | job-runner, reclaimer (relay) | alerting, webhook dispatcher |
| `medicalos.events.system` | `job.state_changed`, `job.step_changed`, `queue.lease_expired` | `<tenant_id>:<job_id>` | 6 | 3 d | all | SSE fan-out, metrics |
| `medicalos.events.audit` | audit events (chapter 9 owns the payloads) | `<tenant_id>:<actor_id>` | 6 | per chapter 9 | all | audit sink |
| `medicalos.svc.<service_id>.work` | `job.dispatch` | `<tenant_id>:<study_instance_uid>` | 6 | 1 d | dispatcher | that service's runner consumer group |
| `<topic>.dlq` | the original envelope plus `dlq_metadata` | same as parent | = parent | 90 d | DLQ writer | operator tooling only |

**MOS-EXEC-043.** `job.rejected` is published to `medicalos.jobs.completed`, not to `.failed`, because `REJECTED` is a clinical outcome and not an error (MOS-EXEC-014). Consumers MUST switch on `envelope.event_type` and MUST NOT infer semantics from the topic name.

**MOS-EXEC-044.** The partition key MUST be `<tenant_id>:<study_instance_uid>` for every job-scoped topic. This co-locates all work for one study on one partition, which is what makes a "one job per study is already running" check meaningful, and keeps tenants distributed across partitions.

**MOS-EXEC-045.** There is no `medicalos.jobs.running` topic. Intermediate transitions are observable through `GET /api/v1/jobs/{id}/events` (chapter 10), backed by `job_events` and `LISTEN`/`NOTIFY` — never through the bus. A `.running` topic would add a third ordering-independent stream of the same fact that Postgres already holds authoritatively.

**MOS-EXEC-046. Modality topics and nosology topics are forbidden.** There MUST NOT be a `medicalos.ct.requested`, a `medicalos.mr.requested`, a `medicalos.emphysema.*`, or any topic whose name encodes a modality, body part, technique or clinical finding. Four reasons, each independently sufficient:

1. **Routing is data, not topology.** Modality, body part, `ConvolutionKernel` and slice thickness are *input constraints* that live in the `SeriesSelector` of a `ServiceVersion` (chapters 3 and 6). Nosology — `pleural_effusion`, `lung_nodule`, `emphysema_laa` — is an *output claim* that lives in `Capability` (chapter 6). Both are rows. Promoting a row to a topic means a registry edit becomes a broker migration.
2. **It breaks the platform acceptance test.** Spine §14 makes "add lung nodule detection as a second capability with zero core code changes" the 0.3.0 gate. A nosology topic makes that gate fail by construction: a new capability would require a new topic, a new consumer group, new ACLs and a Helm change.
3. **It forces broadcast-filtering.** Spine §6 requires the platform to triage once and create one `Job` per matching `(ServiceVersion, Study, series set)`. A modality topic fans one job out to every consumer that cares about CT, each of which must then filter — exactly the "services self-select" pattern the ownership boundary forbids, and a cross-tenant data-exposure surface besides.
4. **It multiplies DLQs and lag dashboards.** Each topic needs its own DLQ, retention, partition count, consumer group and alert. Eleven modalities × four lifecycle stages is 44 topics plus 44 DLQs describing nothing that `service_id` does not already describe.

The only per-work-unit topic is the per-service inbox `medicalos.svc.<service_id>.work`, whose name is the identity of the party that consumes it — which is topology, correctly expressed.

---

### 5.9 The long-inference consumer hazard

**MOS-EXEC-047.** A job whose inference runs for minutes MUST NOT be executed inside the message-delivery callback of any driver. The mandated pattern is **consume → persist → hand off → heartbeat**:

1. **Consume** one message (`max.poll.records = 1`, or one lease from `Claim`).
2. **Persist** the claim durably: the queue row is leased (driver 1) or the dispatch envelope is written to `job_queue` (driver 2), and the step plan and `RUNNING` transition commit. Only now is the Kafka offset committed.
3. **Hand off** the work to an internal execution slot — a goroutine with a bounded semaphore, sized to the GPU count — and **return from the callback immediately**.
4. **Heartbeat** the lease from a dedicated ticker that is independent of the inference call, every `heartbeat_interval_s`, until the slot finishes.

**MOS-EXEC-048.** The hazard is `max.poll.interval.ms`, whose Kafka default is 300 000 ms (5 minutes). A chest-CT lung segmentation followed by an effusion model takes 3–20 minutes on a single GPU. If the handler blocks inside `poll()`, the broker concludes the consumer is dead, evicts it from the group and rebalances the partition to another consumer — which starts the same job again on a second GPU. The evicted consumer then finishes, writes DICOM to the PACS, and its `commitSync` throws `CommitFailedException` *after* the side effect. The result is duplicated inference, duplicated store attempts and an offset that will be re-delivered a third time. Implementations MUST NOT respond to this by raising `max.poll.interval.ms`: that only lengthens the window during which a genuinely dead consumer holds a partition, and it does not help driver 1 at all.

Mandated consumer configuration for driver 2:

| Setting | Value | Why |
|---|---|---|
| `max.poll.interval.ms` | `300000` (default, unchanged) | The handler returns in well under 5 s because it only persists and hands off |
| `max.poll.records` | `1` | One job per poll; back-pressure is expressed by not polling |
| `enable.auto.commit` | `false` | The offset is committed only after the claim is durable in Postgres |
| `session.timeout.ms` | `45000` | Survives a GC pause without a rebalance |
| `heartbeat.interval.ms` | `3000` | Broker-group heartbeat; unrelated to the lease heartbeat |
| `isolation.level` | `read_committed` | Never see an aborted producer batch |
| `auto.offset.reset` | `earliest` | A new consumer group must not silently skip queued work |

**MOS-EXEC-049.** The same hazard exists in driver 1 as lease expiry, and the same discipline applies: `lease_duration_s` MUST be ≥ `4 × heartbeat_interval_s`, and the heartbeat MUST run on a thread or goroutine that is not blocked by the inference call. A Python service invoked over HTTP MUST NOT be called from the heartbeat thread. When the discipline fails anyway — a frozen node, a stop-the-world pause longer than the lease — fencing (MOS-EXEC-027) is what preserves correctness: the reclaimer re-queues, the second runner claims with `fence_token + 1`, and the first runner's eventual `Complete` is rejected with `ErrLeaseLost` before it can write anything.

**MOS-EXEC-050.** Pausing the partition (`consumer.pause()`) instead of handing off is explicitly rejected as an alternative: it keeps the work in memory, so a process kill loses it, which violates "a job must not be lost when a worker dies," and it still requires the durable persist of step 2.

---

### 5.10 Cancellation is reserved

**MOS-EXEC-073.** Cancellation of a running job is **not implemented** in 0.1 or 0.2. Stated plainly, and stated in four places so that no implementer discovers it by surprise:

- `CANCELLED` exists in the `job_state` enum but has **no row in `job_state_transition`**, so `job_transition()` rejects every attempt to reach it.
- The permission `job.cancel` is defined in the permission catalogue (chapter 8) and granted to no role in any built-in role binding.
- `JobQueue.Cancel` MUST return `ErrCancellationNotImplemented` and MUST NOT mutate any row.
- `POST /api/v1/jobs/{job_id}/cancel` MUST return `501 Not Implemented` with `problem.type = "https://spec.medicalos.org/problems/endpoint-reserved"`, `class: "client_error"`, `code: "ENDPOINT_RESERVED"` (chapter 10, `MOS-API-010`).

**MOS-EXEC-074.** CI MUST assert that no code path produces `CANCELLED`: a test that enumerates `job_state_transition` and asserts no row has `to_state = 'CANCELLED'`, and a `grep`-level check that `'CANCELLED'` appears in the codebase only in the enum definition, the API projection and this test.

The reason is a delivery-path problem, not a scheduling problem. Go `context` cancellation is in-process and does not cross into a Python service on another host or into a sealed vendor container. A truthful cancellation requires: a `cancel_requested` flag returned by `Heartbeat`, a cooperative checkpoint obligation in the medical service contract (chapter 2), a grace period, and a hard container kill with GPU reclamation for services that do not cooperate. That is 0.3 work. Shipping a cancel button that sets a flag nothing reads is worse than shipping no button.

---

### 5.11 Idempotency, end to end

#### 5.11.1 Deriving `idempotency_key`

**MOS-EXEC-053.** `jobs.idempotency_key` MUST be derived, never random and never client-supplied:

```python
import base64, hashlib, json

def derive_idempotency_key(job) -> str:
    material = {
        "v": 1,
        "tenant_id": job.tenant_id,                       # uuid string
        "service_id": job.service_id,                     # "pulmo.effusion"
        "service_version": job.service_version,           # "1.4.2" — the RESOLVED version
        "study_instance_uid": job.study_instance_uid,
        "series_instance_uids": sorted(job.selected_series_uids),
        "prior_study_instance_uids": sorted(job.prior_study_instance_uids),
        "requested_outputs": sorted(job.requested_outputs),   # ["SEG","SR"]
        "parameters": canonicalise(job.parameters),           # RFC 8785 JCS
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(blob).digest()
    return "ik_" + base64.b32encode(digest).decode("ascii").rstrip("=")[:26].lower()
```

Producing, for the worked example above: `ik_x4hq2mtn6pkz3a7fvy5rw9cdeb`.

| MUST be in the material | MUST NOT be in the material |
|---|---|
| `tenant_id` | `job_id` |
| `service_id`, resolved `service_version` | `attempt`, `created_at`, any timestamp |
| `study_instance_uid` | `trace_id`, `correlation_id` |
| sorted `selected_series_uids` | `created_by_id`, `user_id` |
| sorted prior study UIDs | `deadline_at` |
| sorted `requested_outputs` | `priority` |
| canonicalised `parameters` | the HTTP `Idempotency-Key` header |

Resolved `service_version` is in the material because two versions legitimately produce different results and therefore must get different DICOM UIDs. This ordering constraint is binding: resolution (chapter 6) MUST complete before the key is computed, which is consistent with the spine's requirement that resolution is pinned at creation.

**MOS-EXEC-054.** `idempotency_key` MUST NOT be settable by a client. The HTTP `Idempotency-Key` header is a *request*-deduplication concern owned by chapter 10; it is stored in `jobs.request_idempotency_key` and MUST NOT participate in DICOM UID derivation. Allowing a client to choose the key would let one tenant's caller collide two clinically distinct runs into one DICOM series identity.

**MOS-EXEC-055.** `UNIQUE (tenant_id, idempotency_key)` on `jobs` is the single enforcement point. `Enqueue` uses `ON CONFLICT DO NOTHING` and returns the existing `job_id` with `Enqueued = false`. The HTTP surface of that outcome is chapter 10's: the originally recorded response is replayed with its original status — `202` for `POST /api/v1/jobs` — plus `MedicalOS-Idempotent-Replay: true` (`MOS-API-026`). This chapter does not assign status codes.

#### 5.11.2 The single-transaction rule

**MOS-EXEC-056.** `results` MUST carry the uniqueness constraint defined in chapter 12 §12.11. The DICOM object kinds `SEG`/`SR`/`SC`/`PR` are `result_dicom_objects.object_kind`, not `results.result_kind`; `result_kind` is the clinical output category enumerated in chapter 12. One `ResultBundle` yields exactly one `results` row (chapter 1) and one `result_dicom_objects` row per generated object.

**MOS-EXEC-057.** The result row, its generated-object rows and the `RUNNING → COMPLETED` transition MUST commit in one transaction:

```sql
BEGIN;
  INSERT INTO results (id, tenant_id, job_id, study_id, result_kind, capability_id,
                       service_version_id, clinical_use_mode,
                       bundle_bucket, bundle_object_key, bundle_digest)
  VALUES ($1, $2, $3, $4, 'segmentation', $5, $6, 'research_only', $7, $8, $9)
  ON CONFLICT (job_id, result_kind) DO NOTHING;

  INSERT INTO result_dicom_objects (id, tenant_id, result_id, object_kind, sop_class_uid,
                                    series_instance_uid, sop_instance_uid, series_number,
                                    output_index, derivation_inputs, clinical_use_mode,
                                    research_marked, pacs_backend, stow_state, stored_at)
  VALUES ($10, $2, $1, 'SEG', $11, $12, $13, 9001, 0, $14, 'research_only', true, $15,
          'stored', now()),
         ($16, $2, $1, 'SR',  $17, $18, $19, 9002, 0, $20, 'research_only', true, $15,
          'stored', now())
  ON CONFLICT (result_id, object_kind, output_index) DO NOTHING;

  SELECT job_transition($3, ARRAY['RUNNING']::job_state[], 'COMPLETED', 'job-runner', $21,
                        '{"reason":"result_persisted"}'::jsonb);

  DELETE FROM job_queue WHERE job_id = $3 AND fence_token = $21;
COMMIT;
```

**MOS-EXEC-058.** That transaction MUST NOT contain a network call. No STOW-RS, no QIDO-RS, no object-store PUT, no HTTP call to the service. A DICOM store inside a database transaction holds locks for the duration of a multi-hundred-megabyte upload and turns a PACS timeout into a database incident.

#### 5.11.3 How it composes with deterministic DICOM UIDs

**MOS-EXEC-059.** Because the store happens *before* the commit, the safety property cannot come from the transaction; it comes from chapter 4's identity contract. `SeriesInstanceUID` and `SOPInstanceUID` are derived by `derive_uid(...)` of `MOS-IMG-062`, whose binding input tuple includes `idempotency_key`, `model_id`, `model_version` and `output_index` — chapter 4's signature governs and this chapter does not restate it — so a retry regenerates byte-identical identity. Before executing `write_dicom`, the runner MUST issue a QIDO-RS query for the derived `SeriesInstanceUID` through the DICOM Gateway and MUST mark both `write_dicom` and `store_dicom` as `skipped` with `skip_reason = 'derived_series_already_complete'` when the expected instance count is present. Retries therefore **resume**, never re-run, and a second inference pass — whose pixels may legitimately differ on different GPU hardware — never gets written under a UID that already names different content.

**MOS-EXEC-061.** `store_dicom` is the commit point of the pipeline and the last mutating step. Recovery past it is **forward-only**: there is no compensating delete, because deleting a study or instance is denied to the platform by the clinical-safety default-DENY table (chapter 9). A result that must be withdrawn is superseded, never deleted: `results.superseded_by` is set and chapter 9 owns the marking of the superseded DICOM objects. Combined with deterministic UIDs this makes the pipeline replayable and removes the need for compensation entirely.

#### 5.11.4 Crash-point recovery matrix

**MOS-EXEC-060.** The following is normative behaviour and MUST be covered test-by-test by the driver conformance suite.

| Crash point | Durable state after crash | What the retry observes | Outcome |
|---|---|---|---|
| After `POST /jobs` returns, before commit | nothing | client retries with the same header; key is derived identically | one job, `202` |
| Job + queue row committed, before any claim | `QUEUED` | normal claim | one job, `attempt = 1` |
| After claim, before any step ran | `RUNNING`, lease held | lease expires → reclaim (T10) → claim | `attempt = 2`, no side effect |
| After `fetch_series`, before `build_volume` | `RUNNING`, artifacts in object store | steps re-planned and re-executed; `build_volume` is pure | identical volume, no duplicate |
| After `service_invoke` returned a bundle, before `write_dicom` | `RUNNING`; bundle in the artifact store | service re-invoked; pixels MAY differ | permitted — nothing is in the PACS yet |
| After STOW-RS succeeded, before the `results` commit | `RUNNING`; SEG + SR **are in the PACS** | QIDO-RS on the derived `SeriesInstanceUID` returns the expected instance count | `write_dicom` and `store_dicom` `skipped`; result row written; `COMPLETED`. **Exactly one SEG series in the PACS.** |
| After the `results` + `COMPLETED` commit, before the event is published (driver 2) | `COMPLETED`; outbox row unpublished | relay publishes on restart | consumer dedupes on `event_id`; exactly one webhook |
| Runner frozen 5 min, lease reclaimed, runner wakes and calls `Complete` | job re-claimed by another runner, `fence_token + 1` | fence check fails | `ErrLeaseLost`; the zombie writes nothing |

---

### 5.12 Retries, backoff, budget and the dead-letter queue

#### 5.12.1 Backoff

**MOS-EXEC-062.** Retry delay MUST be exponential with equal jitter: `d = min(600, 5 · 2^(claim_count−1))` seconds, and `available_at = now() + uniform(d/2, d)`.

| `claim_count` (just consumed) | `d` (s) | `available_at` offset (s) |
|---|---|---|
| 1 | 5 | 2.5 – 5 |
| 2 | 10 | 5 – 10 |
| 3 | 20 | 10 – 20 |
| 4 | 40 | 20 – 40 |
| 5 | 80 | 40 – 80 |
| 6 | 160 | 80 – 160 |
| 7 | 320 | 160 – 320 |
| ≥ 8 | 600 | 300 – 600 |

Jitter is mandatory, not advisory: without it, a gateway restart re-queues every in-flight job with the same delay and the herd returns simultaneously.

**MOS-EXEC-063.** `max_attempts` defaults to the per-class value in §5.3.2 and MAY be overridden per `Deployment` (chapter 6) within `[1, 10]`. A `Failure` whose class is non-retryable MUST go straight to `FAILED` regardless of remaining budget.

#### 5.12.2 Load-shedding must not consume retry budget

**MOS-EXEC-064.** Capacity rejection MUST NOT count against `max_attempts`. Three mechanisms, one per admission point:

1. **Runner saturation (driver 1).** The queue is pull-based, so load-shedding is *not calling `Claim`*. No row is touched, `claim_count` is not incremented, and the property holds by construction. A runner MUST pass its free-slot count as `ClaimRequest.Max` and MUST NOT claim more than it can execute.
2. **Service saturation.** A service that answers the invocation with `503` and `Retry-After` produces `Fail(Failure{Class: ClassLoadShed, RetryAfter: d})`. The driver MUST then set `claim_count = greatest(0, claim_count - 1)` (and `jobs.attempt` with it), set `available_at = now() + clamp(RetryAfter, 1s, 300s)`, perform T9, increment `jobs.load_shed_count`, emit `job.state_changed` with `reason = "load_shed"`, and MUST NOT emit `job.failed`.
3. **API admission.** `POST /api/v1/jobs` rejected for tenant quota or global back-pressure returns `429` or `503` and MUST NOT create a job row (chapter 10). A job that does not exist has no budget to consume.

**MOS-EXEC-065.** Unbounded load-shedding is itself a failure. When `load_shed_count` exceeds `load_shed_cap` (default 50), the driver MUST convert the next shed into a real failure with `class = 'service_unavailable'`, `code = 'capacity_exhausted'`, and dead-letter it. Otherwise a permanently saturated deployment silently parks studies forever.

#### 5.12.3 Dead-letter queue

**MOS-EXEC-066.** The DLQ is a driver-level concept with one contract in both drivers: a `job_dead_letter` table in driver 1, and `<topic>.dlq` in driver 2 written by the same code path.

```sql
CREATE TABLE job_dead_letter (
  dlq_id        uuid        PRIMARY KEY,
  tenant_id     uuid        NOT NULL,
  job_id        uuid        NOT NULL REFERENCES jobs(id),
  service_id    text        NOT NULL,
  source        text        NOT NULL,     -- 'job_queue' | 'medicalos.svc.pulmo.effusion.work'
  attempts      smallint    NOT NULL,
  failure       jsonb       NOT NULL,     -- {failure_class, failure_code, failure_detail, step_key}
  attempt_log   jsonb       NOT NULL,     -- [{attempt, runner_id, step_key, failure_class, failure_code, at}]
  envelope      jsonb       NOT NULL,     -- the last dispatch envelope, verbatim
  dead_at       timestamptz NOT NULL DEFAULT now(),
  requeued_at   timestamptz,
  requeued_by   text,
  UNIQUE (job_id)
);
```

**MOS-EXEC-067.** DLQ policy:

- A job is dead-lettered **only** on T11 or T12, i.e. when it reaches `FAILED`. `REJECTED` jobs MUST NOT be dead-lettered.
- DLQ entries MUST NOT be retried automatically, ever. The only exit is an operator retry (T13, `POST /api/v1/jobs/{job_id}/retry`, permission `job.retry`), which sets `requeued_at`/`requeued_by` and is audited.
- Retention is 90 days; entries are deleted, not archived, and the `jobs` row survives them.
- A non-zero `medicalos_job_dead_lettered_total` rate over a 15-minute window MUST raise an alert (chapter 13). A silent DLQ is a data-loss mechanism with a reassuring name.
- DLQ entries carry no PHI beyond DICOM UIDs (MOS-EXEC-079).

---

### 5.13 Timeouts and deadlines

**MOS-EXEC-068 (the nested timeout rule).** Every enclosing timeout MUST exceed the sum of its children's worst-case budgets plus a margin. An enclosing timeout shorter than an inner one is the specific defect this rule exists to prevent: the outer layer gives up and retries while the inner request still holds a GPU, so load doubles every lap with no crash, no error and nothing in the log to look at.

**MOS-EXEC-069 (deadline propagation).** The runtime mechanism is absolute-deadline propagation, not fixed nested timeouts. Every downstream call's effective timeout MUST be `min(configured_timeout, attempt_deadline_at − now() − reserve)`, with `reserve = 5 s` per level. `attempt_deadline_at` MUST be carried in the dispatch envelope and in the invocation the runner makes to the service (chapter 2), so the service can bound its own work.

**MOS-EXEC-070.** `deadline_at` and `attempt_deadline_at` are absolute timestamps, never durations. `deadline_at` is job-wide and survives every queue hop and every retry; `attempt_deadline_at` is set at each claim to `now() + attempt_deadline_s`. The reconciler fails a job whose `deadline_at` has passed regardless of remaining attempts.

**MOS-EXEC-071.** Default ladder:

| Level | Config key | Default | Children | Invariant |
|---|---|---|---|---|
| Job (all attempts) | `job.deadline_s` | 21600 (6 h) | attempts | `> attempt_deadline_s + backoff_cap` |
| Attempt | `job.attempt_deadline_s` | 5400 (90 min) | all steps | `≥ Σ step timeouts + 300` |
| Step `fetch_series` | `step.fetch_series.timeout_s` | 900 | WADO-RS requests | internally budgeted, concurrency 8 |
| Step `build_volume` | `step.build_volume.timeout_s` | 300 | — | leaf |
| Step `envelope_check` | `step.envelope_check.timeout_s` | 30 | — | leaf |
| Step `service_invoke` | `step.service_invoke.timeout_s` | 1800 | inference call | `≥ service.infer.timeout_s + 120` |
| Step `validate_bundle` | `step.validate_bundle.timeout_s` | 60 | — | leaf |
| Step `write_dicom` | `step.write_dicom.timeout_s` | 300 | QIDO-RS probe | `≥ gateway.qido.timeout_s + 30` |
| Step `store_dicom` | `step.store_dicom.timeout_s` | 600 | STOW-RS | `≥ gateway.stow.timeout_s + 60` |
| Step `persist_result` | `step.persist_result.timeout_s` | 30 | — | leaf |
| Inference call | `service.infer.timeout_s` | 1500 | — | leaf |
| WADO-RS request | `gateway.wado.timeout_s` | 60 | — | leaf |
| QIDO-RS request | `gateway.qido.timeout_s` | 30 | — | leaf |
| STOW-RS request | `gateway.stow.timeout_s` | 300 | — | leaf |
| Lease | `queue.lease_duration_s` | 120 | — | `≥ 4 × heartbeat_interval_s`; `< attempt_deadline_s` |
| Lease heartbeat | `queue.heartbeat_interval_s` | 30 | — | leaf |
| Claim poll | `queue.claim_poll_interval_s` | 2 | — | `≤ 5` |
| Reclaim sweep | `queue.reclaim_interval_s` | 15 | — | `< lease_duration_s / 4` |
| Stuck-in-CREATED | `job.create_to_queue_deadline_s` | 60 | — | leaf |

Σ step timeouts = 900 + 300 + 30 + 1800 + 60 + 300 + 600 + 30 = **4020 s**; 4020 + 300 = 4320 ≤ 5400. ✓

**MOS-EXEC-072.** A CI test MUST load the shipped configuration and assert every invariant in the table above, including for any per-`Deployment` override. A configuration that violates one MUST fail the build, not warn.

**MOS-EXEC-084.** Nested jobs are reserved for 0.4 (spine §13). When they arrive, a child `Job` MUST be a real row with `parent_job_id` set, `depth = parent.depth + 1`, `depth ≤ 3` enforced by the `CHECK` constraint, `root_job_id` inherited, and `deadline_at ≤ parent.deadline_at`. Provenance is keyed by `root_job_id` (chapter 9). In 0.1–0.2 every job has `depth = 0`, `parent_job_id IS NULL` and `root_job_id = job_id`.

---

### 5.14 The event envelope

**MOS-EXEC-075.** Every event on every topic, and every `job_queue.envelope`, MUST conform to the envelope schema below. There is exactly one envelope schema and one typed payload schema per `event_type`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://spec.medicalos.org/schemas/v1/events/envelope/1-0-0.json",
  "title": "MedicalOS event envelope",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_ref", "event_id", "event_type", "event_time", "producer",
               "tenant_id", "job_id", "job_seq", "partition_key", "traceparent",
               "trace_id", "correlation_id", "idempotency_key", "attempt", "payload"],
  "properties": {
    "schema_ref":     {"type": "string", "format": "uri",
                       "description": "URI of the payload schema, e.g. https://spec.medicalos.org/schemas/v1/events/job.requested/1-0-0.json"},
    "event_id":       {"type": "string", "format": "uuid"},
    "event_type":     {"enum": ["job.requested", "job.dispatch", "job.state_changed",
                                "job.step_changed", "job.completed", "job.rejected",
                                "job.failed", "queue.lease_expired"]},
    "event_time":     {"type": "string", "format": "date-time"},
    "producer":       {"type": "string", "pattern": "^medicalos\\.[a-z-]+@[0-9]+\\.[0-9]+\\.[0-9]+(\\+[0-9a-f]{7})?$",
                       "examples": ["medicalos.control-plane@0.2.0+4c1f9ab"]},
    "tenant_id":      {"type": "string", "format": "uuid"},
    "job_id":         {"type": "string", "format": "uuid"},
    "job_seq":        {"type": "integer", "minimum": 1,
                       "description": "the value of job_events.seq — monotonic per job across all topics"},
    "partition_key":  {"type": "string", "pattern": "^[0-9a-f-]{36}:[0-9.]+$"},
    "traceparent":    {"type": "string",
                       "pattern": "^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"},
    "trace_id":       {"type": "string", "pattern": "^[0-9a-f]{32}$"},
    "correlation_id": {"type": "string", "minLength": 1, "maxLength": 128},
    "idempotency_key":{"type": "string", "pattern": "^ik_[a-z2-7]{26}$"},
    "attempt":        {"type": "integer", "minimum": 0, "maximum": 10},
    "payload":        {"type": "object", "minProperties": 1}
  }
}
```

**MOS-EXEC-076.** `trace_id` and `correlation_id` are different things and MUST NOT be conflated, which is exactly what v0.1 did. `traceparent` is the W3C Trace Context header value produced by OpenTelemetry, `trace_id` is its trace-id segment carried separately so it can be indexed, and together they are what makes a single trace span API → queue → runner → service → gateway. `correlation_id` is a business-level grouping: all jobs created from one study arrival, or one API request, share it. A consumer joining logs by `correlation_id` gets the clinical episode; joining by `trace_id` gets the causal call tree.

**MOS-EXEC-077.** `payload` MUST have at least one property. There is no event type with an empty payload object. Every `event_type` in the enum above has a typed schema below; adding an event type without a payload schema MUST fail CI.

**MOS-EXEC-078.** `job_seq` — the envelope's carriage of `job_events.seq` (`MOS-STORE-271`, chapter 12) — is the ordering mechanism. Kafka orders only within one partition of one topic, and the spine's lifecycle topics are three separate topics, so `job.completed` can be delivered before `job.state_changed(RUNNING)` under normal consumer lag. Consumers that maintain any per-job view MUST track the highest `job_seq` seen per `job_id` and MUST discard any event with a lower value. Consumers MUST NOT reconstruct job state from the bus at all (MOS-EXEC-002) — `job_seq` exists so that a stale event cannot make a *derived* view flap, not so that the bus can become authoritative.

#### 5.14.1 Payload schemas

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/job.requested/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["study_instance_uid", "series_instance_uids", "service_id", "service_version",
               "capability_ids", "clinical_use_mode", "requested_outputs", "resolution",
               "deadline_at", "created_by", "steps_total"],
  "properties": {
    "study_instance_uid":        {"type": "string", "pattern": "^[0-9.]{1,64}$"},
    "series_instance_uids":      {"type": "array", "minItems": 1,
                                  "items": {"type": "string", "pattern": "^[0-9.]{1,64}$"}},
    "prior_study_instance_uids": {"type": "array", "items": {"type": "string"}, "default": []},
    "service_id":                {"type": "string", "examples": ["pulmo.effusion"]},
    "service_version":           {"type": "string", "examples": ["1.4.2"]},
    "capability_ids":            {"type": "array", "items": {"type": "string"},
                                  "examples": [["pleural_effusion"]]},
    "clinical_use_mode":         {"enum": ["research_only", "clinical"]},
    "requested_outputs":         {"type": "array", "items": {"enum": ["SEG", "SR", "SC"]}},
    "resolution": {
      "type": "object", "additionalProperties": false,
      "required": ["strategy", "candidates_considered", "selected_reason"],
      "properties": {
        "strategy":              {"enum": ["explicit_pin", "tenant_pin", "deployment_state",
                                           "declared_metric", "semver_desc"]},
        "candidates_considered": {"type": "integer", "minimum": 1},
        "selected_reason":       {"type": "string"}
      }
    },
    "deadline_at": {"type": "string", "format": "date-time"},
    "steps_total": {"type": "integer", "minimum": 1},
    "created_by": {
      "type": "object", "additionalProperties": false,
      "required": ["kind", "id"],
      "properties": {"kind": {"enum": ["user", "service_account", "triage"]},
                     "id":   {"type": "string"}}
    }
  }
}
```

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/job.dispatch/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["service_id", "service_version", "image_digest", "study_instance_uid",
               "series_instance_uids", "requested_outputs", "attempt_deadline_at",
               "lease", "gateway", "uid_root"],
  "properties": {
    "service_id":          {"type": "string"},
    "service_version":     {"type": "string"},
    "image_digest":        {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
    "study_instance_uid":  {"type": "string"},
    "series_instance_uids":{"type": "array", "minItems": 1, "items": {"type": "string"}},
    "requested_outputs":   {"type": "array", "items": {"enum": ["SEG", "SR", "SC"]}},
    "attempt_deadline_at": {"type": "string", "format": "date-time"},
    "lease": {
      "type": "object", "additionalProperties": false,
      "required": ["duration_s", "heartbeat_interval_s", "fence_token"],
      "properties": {"duration_s": {"const": 120},
                     "heartbeat_interval_s": {"const": 30},
                     "fence_token": {"type": "integer", "minimum": 1}}
    },
    "gateway": {
      "type": "object", "additionalProperties": false,
      "required": ["base_url", "credential_ref"],
      "properties": {
        "base_url":       {"type": "string", "format": "uri",
                           "examples": ["https://gw.medicalos.svc.cluster.local/dicomweb"]},
        "credential_ref": {"type": "string", "pattern": "^secretref://[a-z0-9/_-]+$",
                           "description": "A reference, never a token. Chapter 8 owns resolution."}
      }
    },
    "uid_root": {"type": "string", "pattern": "^[0-9.]{1,32}$", "examples": ["1.2.826.0.1.3680043.10.1337"]}
  }
}
```

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/job.completed/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["result_id", "generated_object_kinds", "produced_series_instance_uids",
               "finding_count", "measurement_count", "service_version",
               "duration_ms", "steps_total", "steps_completed"],
  "properties": {
    "result_id":                    {"type": "string", "format": "uuid"},
    "generated_object_kinds":       {"type": "array", "items": {"enum": ["SEG","SR","SC","PR"]},
                                     "description": "result_dicom_objects.object_kind values written for this job (chapter 12)"},
    "produced_series_instance_uids":{"type": "array", "minItems": 1, "items": {"type": "string"}},
    "finding_count":                {"type": "integer", "minimum": 0},
    "measurement_count":            {"type": "integer", "minimum": 0},
    "service_version":              {"type": "string"},
    "duration_ms":                  {"type": "integer", "minimum": 0},
    "steps_total":                  {"type": "integer", "minimum": 1},
    "steps_completed":              {"type": "integer", "minimum": 1},
    "skipped_steps":                {"type": "array", "items": {"type": "string"}, "default": []}
  }
}
```

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/job.rejected/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["reason_code", "detail", "service_id", "service_version", "evaluated_series"],
  "properties": {
    "reason_code": {"enum": ["no_eligible_series", "no_candidate_service_version",
                             "outside_applicability_envelope", "unsupported_geometry",
                             "input_constraint_unmet", "policy_denied", "service_declined"]},
    "detail":      {"type": "string", "maxLength": 1024},
    "service_id":  {"type": "string"},
    "service_version": {"type": "string"},
    "evaluated_series": {
      "type": "array", "minItems": 0,
      "description": "projection of the job_series rows (chapter 12, MOS-STORE-270) carried for consumer convenience; the authoritative read is GET /api/v1/jobs/{job_id}/series-selection",
      "items": {
        "type": "object", "additionalProperties": false,
        "required": ["series_instance_uid", "verdict"],
        "properties": {
          "series_instance_uid": {"type": "string"},
          "verdict":             {"enum": ["selected", "rejected"],
                                  "description": "the job_series.decision value verbatim (chapter 12, MOS-STORE-270); there is no accepted-to-selected mapping"},
          "reason_code":         {"type": "string",
                                  "description": "chapter 3's closed SeriesRequirement vocabulary (MOS-DATA-069), lowercase snake_case"},
          "observed":            {"type": "object"},
          "required":            {"type": "object"}
        }
      }
    }
  }
}
```

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/job.failed/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["failure", "attempt", "max_attempts", "dead_lettered", "duration_ms"],
  "properties": {
    "failure": {
      "type": "object", "additionalProperties": false,
      "required": ["class", "code", "message", "retryable"],
      "properties": {
        "class":     {"enum": ["transient_infrastructure","gateway_unavailable","service_unavailable",
                               "service_crashed","lease_expired","dicom_store_failed",
                               "invalid_result_bundle","preprocessing_selftest_failed",
                               "dicom_write_failed","deadline_exceeded","internal"]},
        "code":      {"type": "string",
                      "examples": ["stow_partial_failure", "output_implausible"]},
        "message":   {"type": "string", "maxLength": 1024},
        "step_key":  {"type": "string"},
        "retryable": {"type": "boolean"}
      }
    },
    "attempt":       {"type": "integer", "minimum": 1},
    "max_attempts":  {"type": "integer", "minimum": 1},
    "dead_lettered": {"type": "boolean"},
    "duration_ms":   {"type": "integer", "minimum": 0}
  }
}
```

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/job.state_changed/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["from_state", "to_state", "actor", "transition", "phase",
               "steps_completed", "steps_total"],
  "properties": {
    "from_state":      {"enum": ["CREATED","QUEUED","RUNNING","COMPLETED","FAILED","CANCELLED","REJECTED"]},
    "to_state":        {"enum": ["CREATED","QUEUED","RUNNING","COMPLETED","FAILED","CANCELLED","REJECTED"]},
    "actor":           {"enum": ["control-plane","job-runner","queue-reclaimer","reconciler","operator"]},
    "transition":      {"type": "string", "pattern": "^T(1[0-3]|[1-9])$"},
    "reason":          {"type": "string"},
    "phase":           {"type": "string"},
    "steps_completed": {"type": "integer", "minimum": 0},
    "steps_total":     {"type": "integer", "minimum": 0},
    "visible_at":      {"type": "string", "format": "date-time"}
  }
}
```

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/job.step_changed/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["step_key", "step_index", "owner", "from_status", "to_status", "phase"],
  "properties": {
    "step_key":            {"type": "string"},
    "step_index":          {"type": "integer", "minimum": 0},
    "owner":               {"enum": ["platform", "service"]},
    "from_status":         {"enum": ["pending","running","succeeded","skipped","failed"]},
    "to_status":           {"enum": ["pending","running","succeeded","skipped","failed"]},
    "phase":               {"type": "string"},
    "skip_reason":         {"type": "string"},
    "duration_ms":         {"type": "integer", "minimum": 0},
    "output_artifact_ids": {"type": "array", "items": {"type": "string", "format": "uuid"}}
  }
}
```

```json
{
  "$id": "https://spec.medicalos.org/schemas/v1/events/queue.lease_expired/1-0-0.json",
  "type": "object", "additionalProperties": false,
  "required": ["lease_owner", "lease_expires_at", "attempt", "max_attempts", "action"],
  "properties": {
    "lease_owner":      {"type": "string"},
    "lease_expires_at": {"type": "string", "format": "date-time"},
    "attempt":          {"type": "integer", "minimum": 1},
    "max_attempts":     {"type": "integer", "minimum": 1},
    "action":           {"enum": ["released", "dead_lettered"]},
    "next_visible_at":  {"type": "string", "format": "date-time"}
  }
}
```

A complete envelope, as produced by the control plane:

```json
{
  "schema_ref": "https://spec.medicalos.org/schemas/v1/events/job.requested/1-0-0.json",
  "event_id": "0c2a41e6-9ad5-4f4e-8a27-1b1e5b0c4f51",
  "event_type": "job.requested",
  "event_time": "2026-09-13T09:41:07.318Z",
  "producer": "medicalos.control-plane@0.2.0+4c1f9ab",
  "tenant_id": "3f8b1d24-7c56-4a0e-9f11-2d6a0c4e8b73",
  "job_id": "a7d4c0f2-5e91-4b83-bc16-90f2e4a1d558",
  "job_seq": 1,
  "partition_key": "3f8b1d24-7c56-4a0e-9f11-2d6a0c4e8b73:1.2.840.113619.2.55.3.604688.101.1737412800",
  "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "correlation_id": "arrival-2026-09-13-0941-3f8b1d24",
  "idempotency_key": "ik_x4hq2mtn6pkz3a7fvy5rw9cdeb",
  "attempt": 0,
  "payload": {
    "study_instance_uid": "1.2.840.113619.2.55.3.604688.101.1737412800",
    "series_instance_uids": ["1.2.840.113619.2.55.3.604688.101.1737412800.7"],
    "prior_study_instance_uids": [],
    "service_id": "pulmo.effusion",
    "service_version": "1.4.2",
    "capability_ids": ["pleural_effusion"],
    "clinical_use_mode": "research_only",
    "requested_outputs": ["SEG", "SR"],
    "resolution": {
      "strategy": "deployment_state",
      "candidates_considered": 2,
      "selected_reason": "1.4.2 is the only ServiceVersion in state deployed for tenant 3f8b1d24"
    },
    "deadline_at": "2026-09-13T15:41:07Z",
    "steps_total": 8,
    "created_by": {"kind": "triage", "id": "triage-worker"}
  }
}
```

#### 5.14.2 PHI and schema evolution

**MOS-EXEC-079.** No envelope and no payload may carry PHI. DICOM `StudyInstanceUID`, `SeriesInstanceUID` and `SOPInstanceUID` **are** permitted — the spine mandates `study_instance_uid` inside the partition key, and results reference source SOP Instance UIDs — because within MedicalOS they are pseudonymous identifiers resolvable only through the tenant-scoped DICOM Gateway. The following field names are banned everywhere in the envelope tree: `patient_id`, `patient_name`, `patient_birth_date`, `patient_sex`, `patient_age`, `accession_number`, `study_date`, `study_time`, `study_description`, `series_description`, `institution_name`, `referring_physician_name`, `operator_name`, `other_patient_ids`. A CI test MUST walk every payload schema and every `examples` block and fail on any of these names. Chapter 8 is authoritative for the full PHI class list; this list is the execution-plane projection of it.

**MOS-EXEC-080.** `schema_ref` MUST point at an immutable, published schema document versioned `MAJOR-MINOR-PATCH` in its path, whose `$id` is absolute and under `https://spec.medicalos.org/schemas/v1/` — the single schema authority of chapter 10 `MOS-API-084`, matching the single problem-document authority of `MOS-API-036`. No other host is permitted for any event schema. Within a major version, changes MUST be additive: new optional properties only. Removing a property, narrowing an enum, or changing a type MUST bump the major version and MUST publish a new `event_type`-scoped schema URI; the old one MUST remain served for at least one release. `additionalProperties: false` is mandatory on every payload schema so that an unknown field is a loud failure rather than silent data loss.

---

### 5.15 Reconciliation, invariants and metrics

**MOS-EXEC-081.** A reconciler MUST run every 60 s and MUST:

1. Fail every job in `CREATED` older than `create_to_queue_deadline_s` (T4).
2. Fail every job in `QUEUED` or `RUNNING` whose `deadline_at` has passed (T6, or T11 via the runner's own deadline check).
3. Delete `job_queue` rows whose `jobs.state` is terminal (an invariant violation, counted).
4. Count jobs in `QUEUED` with no `job_queue` row and jobs in `RUNNING` with no lease — both are impossible under MOS-EXEC-034 and both MUST increment `medicalos_job_invariant_violation_total` and log at ERROR.

**MOS-EXEC-082.** `medicalos_job_invariant_violation_total > 0` is a release blocker, not a warning. It means the single-transaction rule was violated somewhere.

**MOS-EXEC-083.** The execution plane MUST expose these metrics (chapter 13 owns naming conventions, scrape and dashboards). Labels MUST NOT include any study, series, instance or patient identifier; `tenant_id` and `service_id` are permitted.

| Metric | Type | Labels |
|---|---|---|
| `medicalos_job_transitions_total` | counter | `tenant_id`, `service_id`, `from_state`, `to_state`, `actor` |
| `medicalos_job_queue_depth` | gauge | `service_id`, `visible` (`true`/`false`) |
| `medicalos_job_queue_oldest_visible_age_seconds` | gauge | `service_id` |
| `medicalos_job_claim_latency_seconds` | histogram | `service_id` |
| `medicalos_job_step_duration_seconds` | histogram | `service_id`, `step_key`, `status` |
| `medicalos_job_attempts_total` | counter | `service_id`, `failure_class` |
| `medicalos_job_load_shed_total` | counter | `service_id` |
| `medicalos_job_lease_expired_total` | counter | `service_id`, `action` |
| `medicalos_job_dead_lettered_total` | counter | `service_id`, `failure_class` |
| `medicalos_job_rejected_total` | counter | `service_id`, `reason_code` |
| `medicalos_job_invariant_violation_total` | counter | `invariant` |
| `medicalos_outbox_lag_seconds` | gauge | — (driver 2 only) |

`medicalos_job_rejected_total` is deliberately a separate counter from `medicalos_job_dead_lettered_total`: a dashboard that adds clinical rejections to system failures produces an error rate that rises whenever the platform is working correctly on unsuitable studies.

---

### Acceptance criteria

A reviewer or CI job can execute each of these against a running 0.2.0 deployment. Each is falsifiable and names the surface it checks.

1. **State enum closure.** `SELECT unnest(enum_range(NULL::job_state))` returns exactly `CREATED, QUEUED, RUNNING, COMPLETED, FAILED, CANCELLED, REJECTED`. A `grep` over the repository for `POSTPROCESSING`, `WAITING`, `PREPROCESSING` returns zero hits outside this spec.
2. **Transition table parity.** The rows of `job_state_transition` equal, as a set, the `(From, To, Actor)` triples of the table in §5.2.2. CI parses the Markdown table and diffs it against the seeded rows.
3. **Illegal transitions are refused by the database.** `SELECT job_transition(<completed job>, ARRAY['COMPLETED']::job_state[], 'RUNNING', 'job-runner', NULL, '{}')` raises SQLSTATE `MOS03`. No application role holds `UPDATE(state)` on `jobs`: `has_column_privilege('medicalos_app','jobs','state','UPDATE')` is false.
4. **`CANCELLED` is unreachable.** `SELECT count(*) FROM job_state_transition WHERE to_state = 'CANCELLED'` returns 0. `JobQueue.Cancel` returns `ErrCancellationNotImplemented`. `POST /api/v1/jobs/{job_id}/cancel` returns `501` with `code: "ENDPOINT_RESERVED"`.
5. **No float progress.** `grep -ri "progress" --include='*.go' --include='*.py' --include='*.sql' --include='*.json'` returns no field, column or JSON key named `progress`. `GET /api/v1/jobs/{id}` response contains `phase`, `steps_completed`, `steps_total` and no percentage.
6. **Steps are derived.** Insert a `job_steps` row and update its status; `jobs.steps_completed` changes without any application write. Setting `jobs.steps_total` directly and then touching any `job_steps` row restores the derived value.
7. **Enqueue atomicity.** Kill the control-plane process between the `jobs` INSERT and the `job_queue` INSERT by injecting a fault. After restart, `SELECT count(*) FROM jobs j LEFT JOIN job_queue q ON q.job_id = j.id WHERE j.state='QUEUED' AND q.job_id IS NULL` returns 0.
8. **Idempotent creation.** Submit the same study to the same service twice concurrently from 16 goroutines. Exactly one `jobs` row exists, 15 responses carry the same `job_id`, and `medicalos_job_transitions_total{to_state="CREATED"}` increased by 1.
9. **Derived key stability.** `derive_idempotency_key` over the fixture input returns `ik_x4hq2mtn6pkz3a7fvy5rw9cdeb` on three consecutive runs, in Go and in Python, and changes when `service_version` changes from `1.4.2` to `1.4.3`. Supplying an `Idempotency-Key` header does not change it.
10. **Exclusive claim.** 32 concurrent `Claim` calls against a queue of 10 claimable rows return 10 leases in total, with no `job_id` appearing twice, and every claimed row has `claim_count = 1`.
11. **Fencing.** Freeze runner A after claim; let the reclaimer release and runner B claim; unfreeze A and call `Complete`. A receives `ErrLeaseLost`, `SELECT count(*) FROM results WHERE job_id = $1` is 1, and a QIDO-RS query on the derived `SeriesInstanceUID` returns exactly one series.
12. **Claim count is single-sited.** `grep -rn "claim_count *= *[a-z.]*claim_count *+ *1" --include='*.sql' --include='*.go'` matches exactly one statement, the claim statement of §5.6.3, and no other statement writes `job_queue.claim_count` upward.
13. **Load-shed preserves budget.** Configure a stub service returning `503 Retry-After: 5`. After 10 sheds, `jobs.attempt` is ≤ 1, `jobs.load_shed_count` is 10, `medicalos_job_dead_lettered_total` is 0, and no `job.failed` event was produced. After `load_shed_cap` sheds the job is `FAILED` with `code = 'capacity_exhausted'`.
14. **Backoff is jittered.** Force 100 jobs to retry at `claim_count = 3`; the standard deviation of their `available_at` offsets is > 2 s and all offsets lie in [10 s, 20 s].
15. **Crash-point matrix.** Every row of MOS-EXEC-060 has a named test, and the STOW-before-commit row asserts all three surfaces: exactly one `results` row, exactly one `SeriesInstanceUID` per `result_dicom_objects.object_kind` via QIDO-RS, and exactly one `job.completed` event.
16. **Skip-if-present.** Run a job to completion, delete the `results` row, retry via T13. The second run marks `write_dicom` and `store_dicom` as `skipped` with `skip_reason = 'derived_series_already_complete'` and the PACS instance count is unchanged.
17. **Single transaction, no network.** A static check asserts that the function performing T8 contains no HTTP, DICOMweb or object-store call, and an integration test asserts that `results` INSERT and the `COMPLETED` transition share one transaction id (`SELECT txid_current()` recorded by a trigger).
18. **`REJECTED` is not an error.** A study containing only a localizer and a dose-report SR yields state `REJECTED`, `jobs.reject_reason_code = 'no_eligible_series'` in the database and `rejection.code == "NO_ELIGIBLE_SERIES"` with `class: "clinical_rejection"` in the `GET /api/v1/jobs/{job_id}` response, `GET /api/v1/jobs/{job_id}/series-selection` listing every evaluated series with a chapter 3 reason code, `medicalos_job_dead_lettered_total` unchanged, zero rows in `job_dead_letter`, and zero retries.
19. **DLQ is terminal and loud.** A job failing 5 times with `gateway_unavailable` produces exactly one `job_dead_letter` row whose `attempt_log` has 5 entries, is never retried automatically over a 10-minute observation, and fires the DLQ alert rule.
20. **Topic set is closed.** In a 0.3 deployment, `kafka-topics --list` returns only the names in the §5.8 table plus their `.dlq` siblings. No topic name matches `(?i)\b(ct|mr|xr|us|pet|chest|lung|emphysema|nodule|effusion|embolism)\b` except as a substring of a registered `service_id` inside `medicalos.svc.<service_id>.work`.
21. **No `.running` topic.** `medicalos.jobs.running` does not exist and is referenced nowhere in the repository.
22. **Partition key.** Every produced record's key matches `^[0-9a-f-]{36}:[0-9.]+$` and equals `tenant_id || ':' || study_instance_uid` for job-scoped topics.
23. **Long-inference safety.** With a stub service sleeping 7 minutes (`> max.poll.interval.ms`), one job runs to `COMPLETED`, no consumer-group rebalance occurs, `medicalos_job_lease_expired_total` is 0, and exactly one SEG series exists in the PACS.
24. **Heartbeat independence.** A test asserts the heartbeat ticker fires at least 12 times during a 7-minute blocking inference call, and that it runs on a different goroutine/thread than the invocation.
25. **Envelope validity.** Every event captured during the end-to-end acceptance run validates against `envelope/1-0-0.json` and against the `schema_ref` it names. Zero events have an empty `payload`. Every `event_type` in the envelope enum has a published payload schema.
26. **PHI denylist.** A CI test walks every payload schema, every fixture and every captured event and fails on any of the 15 banned field names of MOS-EXEC-079.
27. **Ordering guard.** Deliver `job.completed` (job_seq 9) to a consumer before `job.state_changed` (job_seq 4); the consumer's view shows `COMPLETED` and the late event is dropped, with `medicalos_consumer_stale_event_total` incremented.
28. **Timeout ladder.** The config-invariant test of MOS-EXEC-072 passes for the shipped defaults and fails when `step.service_invoke.timeout_s` is lowered below `service.infer.timeout_s + 120`.
29. **Deadline propagation.** A job created with `deadline_at = now() + 60s` and a stub service sleeping 300 s ends `FAILED` with `code = 'deadline_exceeded'` within 75 s, and the recorded effective timeout passed to the service call was ≤ 55 s.
30. **Reconciler.** A job forced into `CREATED` with no queue row reaches `FAILED` with `code = 'stuck_in_created'` within 120 s, and `medicalos_job_invariant_violation_total` increments.
31. **Driver parity.** The conformance suite of MOS-EXEC-032 passes unchanged against both the Postgres and (from 0.3) the Kafka driver. Swapping `MEDICALOS_QUEUE_DRIVER=postgres|kafka` changes no application code.
32. **Outbox (0.3).** Kill the relay between the `job_outbox` INSERT and the broker ack; after restart the event is published exactly once as observed by the consumer's `event_id` dedup, and `medicalos_outbox_lag_seconds` returns below 5 s.
33. **Tenant isolation.** With `medicalos.tenant_id` set to tenant A, `SELECT count(*) FROM jobs` and `FROM job_queue` return only A's rows; a direct `SELECT` for a known B `job_id` returns zero rows, not a permission error.
34. **One schema authority.** A CI grep for `https://[a-z.]+/(schemas|problems)/` over the repository and this specification returns only URIs under `https://spec.medicalos.org/schemas/v1/` and `https://spec.medicalos.org/problems/`. Every `$id` and every `schema_ref` in §5.14 matches the first prefix (MOS-EXEC-080, `MOS-API-084`, `MOS-API-036`).
35. **`target_kind` is closed and half-enabled.** `POST /api/v1/jobs` with `target.kind = "service_version"` returns `202`. The same request with `target.kind = "capability"` returns `422`, `class: "client_error"`, `code: "TARGET_KIND_NOT_AVAILABLE"` in a 0.2.x build, and no `jobs` row is created. `target.kind = "workflow_version"` returns `400`, `code: "SCHEMA_VIOLATION"`. The generated OpenAPI enum for `target.kind` contains exactly `service_version` and `capability` (MOS-EXEC-006, `MOS-API-044`).

---

[← 4. Imaging Contracts: Geometry, Preprocessing and DICOM Output](04-imaging-contracts.md) · [Index](../../MEDICALOS_SPEC.md) · [6. Registries, Capability Resolution and Deployment →](06-registries.md)
