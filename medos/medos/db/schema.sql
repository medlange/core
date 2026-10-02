-- =====================================================================================
-- medos/db/schema.sql -- the ONLY DDL in MedicalOS weeks 1-2. No ORM, no models.
--
-- CONTRACT.md section 8:
--   Tables: jobs, job_steps, job_events, job_series, results, result_measurements,
--           result_dicom_objects, job_queue.
--   Non-negotiable:
--     - UNIQUE (job_id, capability_id) on results
--     - job_events append-only; seq monotonic per job
--     - the results row and the terminal state transition in ONE transaction
--   tenant_id columns are DELIBERATELY ABSENT. Weeks 3-5 adds them with RLS.
--   "Do not add a fake single-tenant value -- that is harder to migrate than an
--    absent column."
--
-- Spec: MOS-EXEC-001 (state enum), MOS-EXEC-010/011 (transition table as data),
--       MOS-EXEC-013 (job_events append-only), MOS-EXEC-020 (step rollup),
--       MOS-EXEC-023 (reserved step keys), MOS-EXEC-027 (fencing),
--       MOS-EXEC-034 (one-transaction enqueue), MOS-EXEC-035 (single-sited claim_count),
--       MOS-EXEC-036 (Claim never picks up expired leases), MOS-EXEC-037 (NOTIFY),
--       MOS-EXEC-073/074 (CANCELLED unreachable), MOS-EXEC-086 (facts the job row carries),
--       MOS-STORE-266, MOS-STORE-269, MOS-STORE-270, MOS-STORE-271, MOS-STORE-274,
--       MOS-STORE-275, MOS-STORE-282, MOS-STORE-284, MOS-STORE-285, MOS-STORE-286,
--       MOS-STORE-357, MOS-SAFE-083 (provenance field set), CONTRACT.md section 10.
--
-- =====================================================================================
-- WHAT THIS SLICE DROPS, AND WHY THE COLUMN IS ABSENT RATHER THAN FAKED
-- =====================================================================================
-- chapter 12 renders these tables inside a full multi-tenant schema with `tenants`,
-- `users`, `studies`, `series`, `capabilities`, `services`, `service_versions`,
-- `deployments` and `pacs_backends`. None of those exist in weeks 1-2 (CONTRACT.md
-- section 0: no auth, no tenancy, no registries). Every foreign key into a table that
-- does not exist is therefore replaced by the natural key it would have resolved:
--
--   chapter 12 column          weeks 1-2 column               why
--   -------------------------  -----------------------------  ----------------------------
--   tenant_id uuid NOT NULL    (absent)                       CONTRACT.md section 8
--   primary_study_id uuid      study_instance_uid text        no `studies` table
--   study_ids uuid[]           prior_study_instance_uids[]    ditto; wire spelling kept
--   job_series.series_id uuid  series_instance_uid text       no `series` table
--   capability_id uuid FK      capability_id text             REGISTRY key, not a row
--   service_version_id uuid    service_version text           no `service_versions` table
--   deployment_* (4 columns)   (absent)                       no `deployments` table
--   pacs_backend uuid FK       pacs_backend text              no `pacs_backends` table
--   bundle_bucket/object_key   (absent)                       no object store in this slice
--   result_provenance (table)  columns on `results`           not in CONTRACT.md section 8
--   result_findings  (table)   results.findings jsonb         not in CONTRACT.md section 8
--   job_dead_letter  (table)   (absent)                       not in CONTRACT.md section 8
--   execution_artifacts        (absent)                       in-process slice; no hand-off
--
-- Reinstating each is an ADD COLUMN plus a backfill from the natural key, which is the
-- migration chapter 12 is written for. A fake uuid would have to be un-faked first.
-- =====================================================================================

-- gen_random_uuid() is in core PostgreSQL from 13; chapter 12's uuid_generate_v7() has no
-- in-core implementation in PostgreSQL 16, so v4 is used and the id stays opaque. Nothing
-- in this slice depends on key locality.

-- -------------------------------------------------------------------------------------
-- Types owned by chapter 5 (section 5.1.2, 5.4.2). `jobs.state` and `job_steps.status`
-- are still `text` + CHECK per MOS-STORE-266 / MOS-STORE-209; the enum types exist
-- because `job_state_transition` and the `job_transition()` signature are typed on them.
-- -------------------------------------------------------------------------------------
CREATE TYPE job_state AS ENUM
  ('CREATED','QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','REJECTED');

CREATE TYPE job_actor AS ENUM
  ('control-plane','job-runner','queue-reclaimer','reconciler','operator');

CREATE TYPE job_step_status AS ENUM
  ('pending','running','succeeded','skipped','failed');


-- -------------------------------------------------------------------------------------
-- Shared triggers
-- -------------------------------------------------------------------------------------
CREATE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

-- MOS-EXEC-013: `job_events` is append-only. Chapter 8 gets there with
-- `REVOKE UPDATE, DELETE ON job_events FROM medicalos_app`, which needs the role
-- catalogue chapter 8 owns and this slice does not have (CONTRACT.md section 0). A
-- trigger reaches the same invariant with one role, and unlike a GRANT it also holds
-- against the owner and against a superuser session -- which is the case that actually
-- destroys an audit trail.
CREATE FUNCTION forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'relation % is append-only: % is not permitted',
        TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'MOS05',
          HINT = 'job_events is the ordering authority for SSE resume (MOS-EXEC-013, '
                 'MOS-STORE-271). Correct a wrong event by appending another.';
END;
$$;


-- =====================================================================================
-- jobs -- chapter 12 section 12.10, minus tenancy and the registry foreign keys.
-- The execution-plane facts are MOS-EXEC-086's; their physical rendering is chapter 12's.
-- =====================================================================================
CREATE TABLE jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- MOS-STORE-357: the internal `id` is never emitted; `public_id` is the only job id
  -- that appears in a URL, an event envelope or a UI. Chapter 10 owns the grammar --
  -- the literal prefix `job_` then a 26-character Crockford base32 ULID in upper case
  -- (Crockford excludes I, L, O and U, which is what the character class spells out).
  -- The whole of `medos.db`'s public surface speaks this id and never the uuid.
  public_id text NOT NULL UNIQUE
    CHECK (public_id ~ '^job_[0-9A-HJKMNP-TV-Z]{26}$'),

  -- MOS-EXEC-001 / MOS-STORE-266: the closed seven-value enum, value-for-value.
  -- CANCELLED is present and RESERVED (MOS-STORE-269, MOS-EXEC-073): it has no row in
  -- `job_state_transition`, so `job_transition()` refuses every attempt to reach it.
  -- Nothing in this slice produces it, and the guard is structural rather than a rule.
  state text NOT NULL DEFAULT 'CREATED' CHECK (state IN
    ('CREATED','QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','REJECTED')),

  -- MOS-EXEC-019: an open display string; clients MUST NOT branch on it.
  -- MOS-EXEC-021: there is no float `progress` column and one MUST NOT be added.
  phase text NOT NULL DEFAULT '',
  steps_total integer NOT NULL DEFAULT 0 CHECK (steps_total >= 0),
  steps_completed integer NOT NULL DEFAULT 0 CHECK (steps_completed >= 0),

  -- MOS-EXEC-006: `target_kind = 'capability'` is refused at admission until 0.3.0, so
  -- this slice carries only the resolved service identity, denormalised (MOS-EXEC-086).
  service_id text NOT NULL CHECK (service_id ~ '^[a-z0-9][a-z0-9_.-]{0,62}$'),
  service_version text NOT NULL,
  capability_ids text[] NOT NULL CHECK (cardinality(capability_ids) >= 1),

  clinical_use_mode text NOT NULL DEFAULT 'research_only'
    CHECK (clinical_use_mode IN ('research_only','clinical')),

  -- MOS-STORE-268's wire spelling, kept because there is no `studies` table to resolve
  -- a surrogate key against. `study_instance_uid` is also the second component of the
  -- partition key (MOS-EXEC-044).
  study_instance_uid text NOT NULL CHECK (study_instance_uid ~ '^[0-9.]{1,64}$'),
  prior_study_instance_uids text[] NOT NULL DEFAULT '{}',

  -- MOS-EXEC-086. NOTE the weeks 1-2 deviation, stated in full because it is load-bearing
  -- for the idempotency key: chapter 3 selects series at admission, so MOS-EXEC-086 can
  -- require this to be non-empty for any non-REJECTED job. In this slice the pipeline
  -- starts at the DICOMweb pull (CONTRACT.md section 0) and selection happens inside the
  -- `fetch_series` step, so the column is empty until the job is RUNNING. The invariant
  -- that survives, and that IS enforced below, is the one that matters clinically: a
  -- COMPLETED job must record which series it consumed.
  selected_series_uids text[] NOT NULL DEFAULT '{}',

  requested_outputs text[] NOT NULL DEFAULT '{SEG,SR}'
    CHECK (requested_outputs <@ ARRAY['SEG','SR','SC']
           AND cardinality(requested_outputs) >= 1),

  -- MOS-EXEC-053 / MOS-EXEC-054 / CONTRACT.md section 9: two different fields that MUST
  -- NOT be merged. `idempotency_key` is derived server-side by
  -- `medos.core.uids.derive_idempotency_key`; `request_idempotency_key` is the echo of
  -- the client's header and NEVER feeds UID derivation.
  idempotency_key text NOT NULL CHECK (idempotency_key ~ '^ik_[a-z2-7]{26}$'),
  request_idempotency_key text
    CHECK (request_idempotency_key IS NULL
           OR request_idempotency_key ~ '^[A-Za-z0-9._~-]{1,255}$'),

  created_by_kind text NOT NULL CHECK (created_by_kind IN ('user','service_account','triage')),
  created_by_id text NOT NULL,

  requested_at timestamptz NOT NULL DEFAULT now(),
  queued_at timestamptz,
  started_at timestamptz,
  finished_at timestamptz,
  -- MOS-EXEC-070: absolute timestamps, never durations. A relative timeout does not
  -- survive a queue hop.
  deadline_at timestamptz NOT NULL,
  attempt_deadline_at timestamptz,

  -- MOS-EXEC-035: `attempt` is copied from `job_queue.claim_count` at claim and nowhere
  -- else. MOS-EXEC-064: a load shed does not raise it.
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  max_attempts integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 10),
  load_shed_count integer NOT NULL DEFAULT 0 CHECK (load_shed_count >= 0),

  -- section 5.3.1's closed job-level vocabulary. Distinct from the per-series list on
  -- `job_series.reason_code`, which is chapter 3's (MOS-DATA-069) -- MOS-STORE-270 is
  -- explicit that the two lists are different and neither may borrow from the other.
  reject_reason_code text CHECK (reject_reason_code IN
    ('no_eligible_series','no_candidate_service_version','outside_applicability_envelope',
     'unsupported_geometry','input_constraint_unmet','policy_denied','service_declined')),
  reject_reason_detail text,

  -- section 5.3.2's closed twelve-value class list (MOS-EXEC-017). `output_implausible`
  -- is NOT a class: it is a `failure_code` under `invalid_result_bundle`.
  failure_class text CHECK (failure_class IN
    ('transient_infrastructure','gateway_unavailable','service_unavailable','service_crashed',
     'lease_expired','dicom_store_failed','load_shed','invalid_result_bundle',
     'preprocessing_selftest_failed','dicom_write_failed','deadline_exceeded','internal')),
  failure_code text,
  -- MOS-EXEC-017 / MOS-EXEC-079: PHI-safe. DICOM UIDs are permitted; a PatientName,
  -- PatientID, AccessionNumber, StudyDate, StudyDescription or burned-in text extract
  -- is not. CONTRACT.md section 11 is the same rule for logs.
  failure_detail text,

  -- MOS-EXEC-076: the W3C trace-id and the business grouping; both NOT NULL and they
  -- MUST NOT be conflated.
  trace_id text NOT NULL CHECK (trace_id ~ '^[0-9a-f]{32}$'),
  correlation_id text NOT NULL,

  -- MOS-EXEC-084: nesting is reserved for 0.4; depth stays 0 throughout 0.1-0.2.
  parent_job_id uuid REFERENCES jobs(id),
  root_job_id uuid NOT NULL,
  depth smallint NOT NULL DEFAULT 0 CHECK (depth BETWEEN 0 AND 3),

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  -- chapter 12's UNIQUE (tenant_id, idempotency_key), with the tenant dropped. This is
  -- the constraint POST /api/v1/jobs relies on for ON CONFLICT DO NOTHING.
  CONSTRAINT jobs_idempotency_uk UNIQUE (idempotency_key),

  CONSTRAINT jobs_terminal_finished_at
    CHECK ((state IN ('COMPLETED','FAILED','CANCELLED','REJECTED')) = (finished_at IS NOT NULL)),
  CONSTRAINT jobs_rejected_has_reason
    CHECK (state <> 'REJECTED' OR reject_reason_code IS NOT NULL),
  CONSTRAINT jobs_failed_has_class
    CHECK (state <> 'FAILED' OR (failure_class IS NOT NULL AND failure_code IS NOT NULL)),
  -- The surviving half of MOS-EXEC-086's selected_series_uids rule; see the column note.
  CONSTRAINT jobs_completed_consumed_series
    CHECK (state <> 'COMPLETED' OR cardinality(selected_series_uids) >= 1),
  CONSTRAINT jobs_steps_le_total CHECK (steps_completed <= steps_total)
);

CREATE INDEX jobs_active_idx ON jobs (state, requested_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
CREATE INDEX jobs_deadline_idx ON jobs (deadline_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
CREATE INDEX jobs_study_idx ON jobs (study_instance_uid, requested_at DESC);

CREATE TRIGGER jobs_touch BEFORE UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- =====================================================================================
-- job_state_transition -- MOS-EXEC-011: "stored as data and enforced by the database,
-- not by application code".
--
-- The rows are section 5.2.2's table verbatim. T1 (-> CREATED) is the INSERT itself and
-- has no row. T14/T15 (-> CANCELLED) have no row either, and their absence is the whole
-- mechanism by which CONTRACT.md section 3's "CANCELLED is RESERVED in this slice --
-- the column and enum value exist, nothing produces it" is enforced rather than merely
-- documented (MOS-EXEC-073, MOS-EXEC-074, MOS-STORE-269).
-- =====================================================================================
CREATE TABLE job_state_transition (
  from_state job_state NOT NULL,
  to_state   job_state NOT NULL,
  actor      job_actor NOT NULL,
  label      text      NOT NULL,   -- 'T2'..'T13'; diffed against the spec in CI
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


-- =====================================================================================
-- job_events -- chapter 12 section 12.10. Append-only, `seq` monotonic per job.
-- =====================================================================================
CREATE TABLE job_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,

  -- MOS-STORE-271: per-job monotonic, assigned in the same transaction as the state
  -- change. Consumers order by (job_id, seq) and discard anything lower than applied.
  -- It is also what `Last-Event-ID` maps to for SSE resume (CONTRACT.md section 9).
  seq integer NOT NULL CHECK (seq >= 1),

  -- MOS-EXEC-041: the delivery identity consumers deduplicate on.
  event_id uuid NOT NULL DEFAULT gen_random_uuid(),

  event_type text NOT NULL CHECK (event_type IN
    ('job.requested','job.dispatch','job.state_changed','job.step_changed',
     'job.completed','job.rejected','job.failed','queue.lease_expired')),
  from_state text,
  to_state text,
  phase text,
  actor text NOT NULL,
  -- MOS-EXEC-077: payload MUST have at least one property; there is no event type with
  -- an empty payload. MOS-STORE-272: no PHI, UIDs only.
  payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object' AND payload <> '{}'::jsonb),
  trace_id text,
  occurred_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT job_events_seq_uk UNIQUE (job_id, seq),
  CONSTRAINT job_events_event_id_uk UNIQUE (event_id)
);

CREATE INDEX job_events_stream_idx ON job_events (job_id, seq);

-- CONTRACT.md section 8: "job_events append-only". Both DML verbs, not just DELETE: an
-- UPDATE that rewrites `to_state` is the more dangerous of the two because it leaves the
-- row count intact.
CREATE TRIGGER job_events_no_update BEFORE UPDATE ON job_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER job_events_no_delete BEFORE DELETE ON job_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();


-- =====================================================================================
-- job_steps -- chapter 12 section 12.10 + chapter 5 section 5.4.
-- =====================================================================================
CREATE TABLE job_steps (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  step_index integer NOT NULL CHECK (step_index >= 0),
  -- MOS-EXEC-023 reserves these eight keys and their phase strings. A job with
  -- requested_outputs = {SEG, SR} plans exactly these, so steps_total = 8.
  step_key text NOT NULL CHECK (step_key IN
    ('fetch_series','build_volume','envelope_check','service_invoke',
     'validate_bundle','write_dicom','store_dicom','persist_result')),
  phase text NOT NULL,
  owner text NOT NULL CHECK (owner IN ('platform','service')),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','succeeded','skipped','failed')),
  skip_reason text,
  -- MOS-EXEC-022: "`attempt` on each row is set to the job's current attempt".
  -- The domain is therefore `jobs.attempt`'s, which starts at 0 for a job planned
  -- before its first claim -- not >= 1. Copying the job's value verbatim is what
  -- makes "which attempt produced this step row" answerable after a retry.
  attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  timeout_s integer NOT NULL CHECK (timeout_s > 0),
  -- MOS-EXEC-024: the sub-progress bag a service may write, for the service_invoke row
  -- only. It MUST NOT change steps_total or steps_completed.
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at timestamptz,
  finished_at timestamptz,
  duration_ms integer,
  error_code text,
  error_detail text,

  CONSTRAINT job_steps_index_uk UNIQUE (job_id, step_index),
  CONSTRAINT job_steps_key_uk UNIQUE (job_id, step_key),
  CONSTRAINT job_steps_failed_has_code CHECK (status <> 'failed' OR error_code IS NOT NULL),
  -- MOS-EXEC-020, verbatim.
  CONSTRAINT job_steps_skip_reason CHECK ((status = 'skipped') = (skip_reason IS NOT NULL))
);

-- section 5.4.2, with the tenant-free `jobs` lookup.
CREATE FUNCTION job_steps_rollup() RETURNS trigger LANGUAGE plpgsql AS $$
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


-- =====================================================================================
-- job_series -- MOS-STORE-270: "first-class rows, not a log line and not a jsonb blob".
-- One row per EVALUATED series, selected and rejected alike, with the chapter 3 reason.
-- =====================================================================================
CREATE TABLE job_series (
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  -- chapter 12 keys this on `series_id uuid` into the imaging projection. There is no
  -- projection in this slice, so the natural key stands in; the DICOM UID is what
  -- survives a projection rebuild anyway (MOS-STORE-279).
  series_instance_uid text NOT NULL CHECK (series_instance_uid ~ '^[0-9.]{1,64}$'),

  -- MOS-STORE-270: `selected`/`rejected` is ONE value set, in the row and on the wire.
  -- A payload spelling the positive value `accepted` is the defect, not a synonym.
  decision text NOT NULL CHECK (decision IN ('selected','rejected')),
  selector_name text,
  rank integer,
  -- chapter 3's closed SeriesRequirement list (MOS-DATA-069) in lowercase snake_case,
  -- e.g. `image_type_excluded`, `slice_thickness_out_of_range`. Deliberately NOT a CHECK:
  -- chapter 3 owns that vocabulary and this slice must not fork it into a second list.
  reason_code text,
  reason_detail text,
  instance_count integer CHECK (instance_count IS NULL OR instance_count >= 0),
  modality text,

  PRIMARY KEY (job_id, series_instance_uid),
  CONSTRAINT job_series_selected_shape
    CHECK ((decision = 'selected') = (selector_name IS NOT NULL AND rank IS NOT NULL)),
  CONSTRAINT job_series_rejected_shape
    CHECK ((decision = 'rejected') = (reason_code IS NOT NULL))
);

CREATE INDEX job_series_selected_idx ON job_series (job_id, rank)
  WHERE decision = 'selected';


-- =====================================================================================
-- job_queue -- chapter 5 section 5.6.1, minus tenancy and RLS.
-- =====================================================================================
CREATE TABLE job_queue (
  job_id uuid PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  -- MOS-EXEC-033: 'medicalos.svc.<service_id>.work'. Under driver 1 the inbox is a
  -- column value and not a schema object, which is why registering a service needs no
  -- broker and no DDL.
  queue text NOT NULL CHECK (queue ~ '^medicalos\.svc\.[a-z0-9][a-z0-9_.-]{0,62}\.work$'),
  -- MOS-EXEC-044. chapter 12 spells it '<tenant_id>:<study_instance_uid>'; with no
  -- tenant the study UID alone is the ordering key, which keeps two jobs on one study
  -- on the same partition when driver 2 arrives.
  partition_key text NOT NULL,
  priority smallint NOT NULL DEFAULT 100,
  available_at timestamptz NOT NULL DEFAULT now(),
  -- MOS-EXEC-035: incremented in exactly ONE statement in the entire system -- the
  -- claim. Not by Fail, not by the reclaimer, not by the reconciler. Single-sited
  -- accounting is what makes "a load shed must not consume retry budget" checkable.
  claim_count smallint NOT NULL DEFAULT 0 CHECK (claim_count >= 0),
  lease_owner text,
  lease_expires_at timestamptz,
  -- MOS-EXEC-027: per-job monotonically increasing, incremented on every successful
  -- claim, verified by Heartbeat/Complete/Fail. Without it a runner whose process froze
  -- for three minutes wakes after its lease was reclaimed and writes a result for a job
  -- it no longer owns.
  fence_token bigint NOT NULL DEFAULT 0,
  enqueued_at timestamptz NOT NULL DEFAULT now(),
  -- MOS-EXEC-075: the dispatch payload, verbatim.
  envelope jsonb NOT NULL,

  CONSTRAINT job_queue_lease_ok
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE INDEX job_queue_claim_idx
  ON job_queue (queue, priority, available_at, enqueued_at)
  WHERE lease_owner IS NULL;

CREATE INDEX job_queue_expiry_idx
  ON job_queue (lease_expires_at)
  WHERE lease_owner IS NOT NULL;

-- section 5.6.4, verbatim. Full jitter over [d/2, d) with d = min(600, 5 * 2^(n-1)).
CREATE FUNCTION job_backoff(p_attempt integer)
RETURNS interval LANGUAGE sql VOLATILE AS $$
  SELECT make_interval(secs => (d / 2.0) + random() * (d / 2.0))
    FROM (SELECT least(600.0, 5.0 * power(2.0, greatest(p_attempt, 1) - 1)) AS d) s;
$$;

-- section 5.6.5. MOS-EXEC-037: a latency optimisation only -- every runner MUST also
-- poll, because NOTIFY reaches only sessions listening at commit, nothing fires when
-- `available_at` merely elapses, and the 63-byte channel limit means the channel is a
-- hash of the queue name and carries no job identity.
-- chapter 5 writes `encode(digest(NEW.queue,'sha256'),'hex')`, which needs pgcrypto.
-- md5() is in core and the channel is a routing token, not a security boundary; the
-- substitution is noted here so the 0.3 migration can restore the spec spelling with
-- the extension.
CREATE FUNCTION job_queue_notify() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('mos_jobq_' || md5(NEW.queue), '');
  RETURN NULL;
END;
$$;

CREATE TRIGGER job_queue_notify_trg
AFTER INSERT ON job_queue FOR EACH ROW EXECUTE FUNCTION job_queue_notify();


-- =====================================================================================
-- job_transition() -- section 5.2.3, tenant removed.
--
-- MOS-EXEC-011: all state changes go through this function. It is the single place the
-- transition table is consulted, the fence token is verified and the `job_events` row
-- with its monotonic `seq` is appended -- so "the state change and its event are in one
-- transaction" is structural rather than a convention each caller must remember.
-- =====================================================================================
CREATE FUNCTION job_transition(
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
  v_type text;
BEGIN
  -- FOR UPDATE is what serialises concurrent transitions on one job, and therefore also
  -- what makes the max(seq)+1 below race-free (MOS-STORE-271).
  SELECT state::job_state INTO v_from FROM jobs WHERE id = p_job_id FOR UPDATE;
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
  -- MOS-EXEC-027. NULL means "this actor holds no lease" and skips the check; a non-NULL
  -- token that disagrees with the queue row means the lease was reclaimed underneath us.
  IF p_fence IS NOT NULL
     AND EXISTS (SELECT 1 FROM job_queue q
                  WHERE q.job_id = p_job_id AND q.fence_token <> p_fence) THEN
    RAISE EXCEPTION 'stale fence token % for job %', p_fence, p_job_id USING ERRCODE = 'MOS04';
  END IF;

  UPDATE jobs
     SET state       = p_to::text,
         started_at  = CASE WHEN p_to = 'RUNNING' AND started_at IS NULL THEN now()
                            ELSE started_at END,
         queued_at   = CASE WHEN p_to = 'QUEUED' THEN now() ELSE queued_at END,
         finished_at = CASE WHEN p_to IN ('COMPLETED','FAILED','CANCELLED','REJECTED')
                            THEN now() ELSE NULL END
   WHERE id = p_job_id;

  -- MOS-EXEC-043: a rejection is a clinical outcome, so it gets its own event type and
  -- never rides the failure channel. Consumers switch on event_type, never on topic.
  v_type := CASE p_to
              WHEN 'COMPLETED' THEN 'job.completed'
              WHEN 'REJECTED'  THEN 'job.rejected'
              WHEN 'FAILED'    THEN 'job.failed'
              ELSE 'job.state_changed'
            END;

  SELECT coalesce(max(seq), 0) + 1 INTO v_seq FROM job_events WHERE job_id = p_job_id;
  INSERT INTO job_events (job_id, seq, event_type, from_state, to_state, phase, actor, payload)
  SELECT p_job_id, v_seq, v_type, v_from::text, p_to::text, j.phase, p_actor::text,
         -- MOS-EXEC-077: never an empty payload.
         coalesce(p_detail, '{}'::jsonb) || jsonb_build_object('job_seq', v_seq)
    FROM jobs j WHERE j.id = p_job_id;
  RETURN v_seq;
END;
$$;


-- job_append_event() -- non-transition events (job.step_changed, queue.lease_expired).
-- Takes the same jobs-row lock so `seq` stays monotonic across BOTH producers.
CREATE FUNCTION job_append_event(
  p_job_id uuid,
  p_event_type text,
  p_actor text,
  p_payload jsonb,
  p_phase text DEFAULT NULL
) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  v_seq integer;
BEGIN
  PERFORM 1 FROM jobs WHERE id = p_job_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'job % not found', p_job_id USING ERRCODE = 'MOS01';
  END IF;
  SELECT coalesce(max(seq), 0) + 1 INTO v_seq FROM job_events WHERE job_id = p_job_id;
  INSERT INTO job_events (job_id, seq, event_type, phase, actor, payload)
  VALUES (p_job_id, v_seq, p_event_type, p_phase, p_actor,
          coalesce(p_payload, '{}'::jsonb) || jsonb_build_object('job_seq', v_seq));
  RETURN v_seq;
END;
$$;


-- =====================================================================================
-- results -- chapter 12 section 12.11, with `result_provenance` folded in.
--
-- CONTRACT.md section 8 names eight tables and `result_provenance` is not among them,
-- but CONTRACT.md section 10 requires every result to record the provenance field set.
-- Folding those columns into `results` keeps the table list exactly as the contract
-- gives it; the 1:1 split-out in weeks 3-5 is a mechanical CREATE TABLE ... SELECT,
-- because MOS-STORE-278 already makes the cardinality exactly one-to-one.
-- =====================================================================================
CREATE TABLE results (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,

  -- capability_id is the `medos.capabilities.REGISTRY` key (CONTRACT.md section 6), not
  -- a uuid: there is no `capabilities` table in this slice.
  capability_id text NOT NULL,
  capability_version text NOT NULL,          -- CONTRACT.md section 10

  result_kind text NOT NULL CHECK (result_kind IN
    ('segmentation','measurement','detection','classification','narrative')),
  clinical_use_mode text NOT NULL
    CHECK (clinical_use_mode IN ('research_only','clinical')),

  -- MOS-EVID-110/111 via MOS-STORE-266: 'flagged' is chapter 7's `warn`; a `fail`
  -- terminates the job before any results row exists.
  plausibility_state text NOT NULL DEFAULT 'ok'
    CHECK (plausibility_state IN ('ok','flagged','failed')),
  out_of_distribution boolean NOT NULL DEFAULT false,
  review_status text NOT NULL DEFAULT 'UNREVIEWED'
    CHECK (review_status IN ('UNREVIEWED','PENDING','IN_REVIEW','ACCEPTED','MODIFIED',
                             'REJECTED','EXPIRED','SUPERSEDED')),
  superseded_by uuid REFERENCES results(id),

  -- CONTRACT.md section 5 has no `result_findings` table in section 8's list either, and
  -- a Finding is a small closed record with no independent identity in this slice. It is
  -- stored as the JSON projection of `CapabilityOutcome.findings`; the QUANTITATIVE half
  -- is normalised into `result_measurements` because MOS-STORE-282 requires a conformant
  -- TID 1500 SR to be generatable from rows without a second, un-modelled dictionary.
  findings jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(findings) = 'array'),

  -- ------------------------------------------------------------------ provenance
  -- CONTRACT.md section 10: "Every result MUST record: job_id, study_instance_uid, the
  -- series actually consumed, capability_id + version, preprocessing_version, the
  -- generated SeriesInstanceUID and SOPInstanceUID per object, worker_version,
  -- runtime_version, and timestamps."
  --   - job_id, capability_id, capability_version: above
  --   - the generated UIDs: `result_dicom_objects` rows, one per written object
  --   - the rest: here
  -- MOS-STORE-279: both the ids and the DICOM UIDs would be recorded in the full schema;
  -- with no projection to hold ids, the UIDs are the record, and they are the half that
  -- survives a rebuild, a PACS migration and an export.
  study_instance_uid text NOT NULL CHECK (study_instance_uid ~ '^[0-9.]{1,64}$'),
  input_series_uids text[] NOT NULL CHECK (cardinality(input_series_uids) >= 1),
  input_instance_uids text[] NOT NULL,
  input_instance_count integer NOT NULL CHECK (input_instance_count >= 1),
  -- MOS-SAFE-083 section B: sha256 over the sorted SOP UID list, so the input set is
  -- checkable without re-reading the pixels.
  input_uid_digest text NOT NULL CHECK (input_uid_digest ~ '^[0-9a-f]{64}$'),
  input_pixel_digest text CHECK (input_pixel_digest IS NULL
                                 OR input_pixel_digest ~ '^[0-9a-f]{64}$'),

  preprocessing_version text NOT NULL,       -- CONTRACT.md section 10
  preprocessing_digest text,
  worker_version text NOT NULL,              -- CONTRACT.md section 10
  runtime_version text NOT NULL,             -- CONTRACT.md section 10
  platform_commit text,

  -- MOS-STORE-273's spatial record, carried on the result rather than on an
  -- `execution_artifacts` row: this slice runs in one process and hands no volume
  -- between steps through storage, so the grid the measurements were computed on has to
  -- be recorded somewhere or the weeks 1-2 exit check ("the measurement in the SR equals
  -- the measurement recomputed from the stored SEG in source geometry") is unverifiable.
  geometry jsonb NOT NULL DEFAULT '{}'::jsonb,

  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  -- CONTRACT.md section 8, NON-NEGOTIABLE. MOS-STORE-275: a retry that reaches the
  -- result stage again finds this constraint and MUST treat the violation as success.
  CONSTRAINT results_job_capability_uk UNIQUE (job_id, capability_id),
  CONSTRAINT results_not_self_superseded CHECK (superseded_by IS NULL OR superseded_by <> id)
);

CREATE INDEX results_job_idx ON results (job_id);
CREATE INDEX results_study_idx ON results (study_instance_uid, created_at DESC);

CREATE TRIGGER results_touch BEFORE UPDATE ON results
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- MOS-STORE-276 / MOS-SAFE-083: provenance is evidence. A result row may be superseded
-- and may be reviewed; the provenance columns are sealed against edit after the fact.
CREATE FUNCTION forbid_column_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  col text;
BEGIN
  FOREACH col IN ARRAY TG_ARGV LOOP
    IF to_jsonb(OLD) -> col IS DISTINCT FROM to_jsonb(NEW) -> col THEN
      RAISE EXCEPTION 'column %.% is sealed after insert', TG_TABLE_NAME, col
        USING ERRCODE = 'MOS06';
    END IF;
  END LOOP;
  RETURN NEW;
END;
$$;

CREATE TRIGGER results_provenance_sealed BEFORE UPDATE ON results
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'job_id','capability_id','capability_version','study_instance_uid',
    'input_series_uids','input_instance_uids','input_instance_count','input_uid_digest',
    'input_pixel_digest','preprocessing_version','preprocessing_digest',
    'worker_version','runtime_version','geometry');


-- =====================================================================================
-- result_measurements -- chapter 12 section 12.11.
-- =====================================================================================
CREATE TABLE result_measurements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  result_id uuid NOT NULL REFERENCES results(id) ON DELETE CASCADE,
  capability_id text NOT NULL,
  -- index into results.findings; NULL for a measurement that belongs to the outcome
  -- rather than to one finding.
  finding_index integer CHECK (finding_index IS NULL OR finding_index >= 0),
  measurement_index integer NOT NULL CHECK (measurement_index >= 0),

  -- MOS-STORE-282: scheme, code and unit are NOT NULL, so "a bare number with a
  -- free-text label" cannot be stored. That is exactly what makes a conformant TID 1500
  -- SR generatable from these rows.
  -- MOS-STORE-282a: the flattened form of chapter 2 section 2.8.2's coded concept, which
  -- is `medos.core.bundle.CodedConcept` here -- {scheme, code, meaning}. `meaning` is
  -- CONTRACT.md section 5's spelling of chapter 2's `display`; it lands in
  -- `concept_display` and no third spelling exists.
  concept_scheme text NOT NULL CHECK (concept_scheme IN ('SCT','DCM','RADLEX','99MEDOS')),
  concept_code text NOT NULL,
  concept_display text NOT NULL,
  concept_scheme_uri text,

  -- MOS-IMG-040: the unrounded value, stored losslessly.
  --
  -- This column was numeric(18,6) and that was a defect: the authoritative value is an
  -- IEEE-754 double (medos/core/bundle.py Measurement.value), and numeric(18,6) silently
  -- rounded it to six decimal places on the way in. The DICOM SR carried the full float
  -- while this table carried the rounded one, so the value a clinician reads and the value
  -- the platform records disagreed -- by 2.5e-06 relative on an LAA-950 percentage, caught
  -- by tests/integration/test_worker.py::test_end_to_end_completes_with_a_seg_and_an_sr_stored.
  --
  -- double precision is the same type the value already is, so the round-trip is exact and
  -- the exit-check comparison against the value recomputed from the stored SEG is an
  -- equality rather than a tolerance.
  value double precision NOT NULL,
  -- MOS-STORE-282b: this stores `unit.code`; `unit.scheme` is always UCUM and is not
  -- stored redundantly. CONTRACT.md section 5 spells it `Measurement.unit`.
  ucum_unit text NOT NULL,

  derivation_scheme text,
  derivation_code text,

  -- MOS-STORE-283 / CONTRACT.md section 5: "MUST be computed in SOURCE geometry. Never
  -- in model space." The column exists so the exception is visible in the data rather
  -- than implicit in code; its two values are exactly ResultBundle.measurements[].
  -- computed_in's (MOS-SVC-091).
  geometry_space text NOT NULL DEFAULT 'source'
    CHECK (geometry_space IN ('source','derived_declared')),
  source_series_instance_uid text NOT NULL,
  -- MOS-IMG-119's qualifiers (convolution kernel, dS, threshold) live here.
  method_detail jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT result_measurements_index_uk UNIQUE (result_id, measurement_index)
);

CREATE INDEX result_measurements_result_idx ON result_measurements (result_id);


-- =====================================================================================
-- result_dicom_objects -- chapter 12 section 12.11.
-- =====================================================================================
CREATE TABLE result_dicom_objects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  result_id uuid NOT NULL REFERENCES results(id) ON DELETE RESTRICT,
  object_kind text NOT NULL CHECK (object_kind IN ('SEG','SR','SC','PR')),
  sop_class_uid text NOT NULL CHECK (sop_class_uid ~ '^[0-9.]{1,64}$'),
  -- CONTRACT.md section 10: the generated SeriesInstanceUID and SOPInstanceUID per
  -- object. Both are derived, never random (MOS-IMG-062).
  series_instance_uid text NOT NULL CHECK (series_instance_uid ~ '^[0-9.]{1,64}$'),
  sop_instance_uid text NOT NULL CHECK (sop_instance_uid ~ '^[0-9.]{1,64}$'),
  -- MOS-IMG-072/073: the band is 9000-9890 for services; 9900-9999 is reserved for
  -- platform-generated objects.
  series_number integer NOT NULL CHECK (series_number >= 9000),
  output_index integer NOT NULL CHECK (output_index >= 0),

  -- MOS-STORE-286: the exact tuple the deterministic UIDs were derived from, so a retry
  -- can recompute the identity without re-deriving it from live state, and so CI can
  -- detect a stored/recomputed mismatch. The nine members are the complete MOS-IMG-062
  -- argument tuple less the platform-constant org_root, and the set MUST NOT be
  -- shortened -- without `uid_kind`, a job producing one SEG series and one SR series
  -- derives the same SeriesInstanceUID twice.
  --
  -- `tenant_id` appears as a MEMBER of this document and that is not a tenancy column:
  -- it records the literal value passed to `derive_uid`, which must stay verbatim or
  -- every object ever written is re-identified (MOS-IMG-085).
  derivation_inputs jsonb NOT NULL CHECK (
    derivation_inputs ?& array['tenant_id','idempotency_key','service_id','service_version',
                               'model_id','model_version','uid_space','uid_kind',
                               'output_index']),
  frame_count integer CHECK (frame_count IS NULL OR frame_count >= 1),

  -- MOS-STORE-285: exists to be queried and asserted; cannot be false, because every row
  -- here is by definition an AI-derived object.
  ai_derived boolean NOT NULL DEFAULT true CHECK (ai_derived),
  clinical_use_mode text NOT NULL
    CHECK (clinical_use_mode IN ('research_only','clinical')),
  research_marked boolean NOT NULL,

  stow_state text NOT NULL DEFAULT 'pending'
    CHECK (stow_state IN ('pending','stored','verified','failed','erased')),
  pacs_backend text NOT NULL DEFAULT 'orthanc',
  object_digest text CHECK (object_digest IS NULL OR object_digest ~ '^[0-9a-f]{64}$'),
  size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes > 0),
  stored_at timestamptz,
  verified_at timestamptz,
  erased_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT result_dicom_objects_uid_uk UNIQUE (sop_instance_uid),
  CONSTRAINT result_dicom_objects_output_uk UNIQUE (result_id, object_kind, output_index),
  -- MOS-STORE-284: research-use marking enforced in the database. An object belonging to
  -- a research-only result cannot be recorded at all unless it is marked. The marking
  -- itself (SeriesDescription prefix, SR title concept, ConversionType, SC banner) is
  -- chapters 4 and 9; this makes skipping the step and still having a row impossible.
  CONSTRAINT result_dicom_objects_ruo_marking
    CHECK (clinical_use_mode <> 'research_only' OR research_marked),
  CONSTRAINT result_dicom_objects_stored_at
    CHECK ((stow_state IN ('stored','verified')) = (stored_at IS NOT NULL)),
  CONSTRAINT result_dicom_objects_erased_at
    CHECK ((stow_state = 'erased') = (erased_at IS NOT NULL))
);

CREATE INDEX result_dicom_objects_result_idx ON result_dicom_objects (result_id);
CREATE INDEX result_dicom_objects_series_idx ON result_dicom_objects (series_instance_uid);
