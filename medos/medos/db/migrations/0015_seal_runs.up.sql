-- =====================================================================================
-- 0015_seal_runs.up.sql -- the ACT of sealing, as a row.
--
-- WHY THIS TABLE EXISTS, AND WHY IT IS NOT A `Job`.
-- `api/v1/routes.yaml` rows R26 and R27 and `MOS-API-112` settle the shape: the seal is
-- asynchronous, polled, driven through the `Orchestrator` port of `MOS-TRAIN-122`, and
-- is NOT a `Job`. `MOS-API-001` makes `POST /api/v1/jobs` the only endpoint that may
-- create one, and chapter 17 forbids the pipeline reaching a Job independently --
-- `MOS-TRAIN-121` C3 excludes `job.create` from the orchestrator account outright, C2
-- forbids the pipeline appearing between a Job and its Result, C4 requires a total
-- orchestrator outage to change the outcome of no Job at all. `EvaluationRun` (table
-- 10.2-B row 42) is the precedent `MOS-API-056` already names for long work that is not
-- a Job, and this entity takes the same shape: a resource with a status field, a
-- `Location` at creation and `Retry-After: 30` (`MOS-API-057`).
--
-- 0006_evidence carries the sealed OBJECTS (`dataset_versions`, `dataset_splits`) and
-- 0011_curation carries the BATCH. Neither carries the act, so before this migration a
-- seal that was running, that refused, or that crashed left no record at all -- and
-- `MOS-UI-132` forbids leaving the operator unable to tell whether a `DatasetVersion`
-- was created, which is exactly the question a missing row cannot answer.
--
-- WHY THE CHECK BATTERY IS A COLUMN AND NOT A JOIN.
-- `MOS-UI-133` fixes nine rows and `MOS-API-112` requires all nine present in every
-- response: "a missing check is not a pass". A child table admits a battery with eight
-- rows in it; a single jsonb document written once, by the driver, in one statement,
-- does not. The `seal_runs_battery_complete` CHECK below is the structural form of that
-- sentence -- a terminal run whose battery does not carry nine entries cannot be stored.
--
-- WHY `state` SEPARATES `REFUSED` FROM `FAILED`.
-- `MOS-UI-158` and `MOS-UI-160`: a refusal is the platform declining for a stated reason
-- the operator can act on; a failure is a platform fault that is not something they did.
-- A console that rendered a blocked C1 in the register of a crash would teach the
-- operator that the gates are unreliable. There is no `CANCELLED`: no route cancels a
-- seal (`MOS-UI-156` asks for a cancel control on a `TrainingRun`, which runs for days,
-- and chapter 19 asks for none here), and a value no path can reach is a state no reader
-- can trust.
--
-- NO PHI. Every column here is an identifier, a counter, a state or a check outcome.
-- `refusals` carries the engine's own `Refusal` array, whose `detail` may name a
-- `patient_key` (`MOS-EVID-010`'s HMAC, P1) and a de-identified `study_instance_uid`
-- (`MOS-API-006` permits it) and nothing else -- `MOS-TRAIN-079` keeps a name, a birth
-- date, an accession number and every source-space UID off the candidate rows these
-- refusals are computed from, so there is nothing of that class here to leak.
--
-- ADDITIVE: creates one table, its policy, its triggers and its grants. It ALTERs no
-- column any migration 0001-0014 created.
--
-- Spec: MOS-API-001, MOS-API-011, MOS-API-056, MOS-API-057, MOS-API-112, MOS-EVID-013,
-- MOS-EVID-015, MOS-EVID-034, MOS-EVID-037, MOS-SEC-072, MOS-STORE-208, MOS-STORE-209,
-- MOS-STORE-210, MOS-STORE-223, MOS-STORE-224, MOS-TRAIN-088, MOS-TRAIN-121,
-- MOS-TRAIN-122, MOS-TRAIN-208, MOS-TRAIN-209, MOS-UI-130, MOS-UI-131, MOS-UI-132,
-- MOS-UI-133, MOS-UI-134, MOS-UI-158, MOS-UI-160.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. seal_runs
--
-- Chapter 12's conventions, not a chapter 12 listing: there is no physical listing for
-- this record anywhere in the specification, so `MOS-STORE-201`'s "generate from chapter
-- 12" cannot be followed literally and 0013's precedent applies -- every field is
-- chapter 19's and chapter 10's, every CONVENTION is chapter 12's (plural table name,
-- `uuid` surrogate key with a `<prefix>_<ULID>` public id, `text` + CHECK enums,
-- `created_at`/`updated_at`, `UNIQUE (tenant_id, id)`, forced row-level security).
-- =====================================================================================
CREATE TABLE seal_runs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  public_id         text NOT NULL UNIQUE
                      CHECK (public_id ~ '^slr_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id         uuid NOT NULL REFERENCES tenants(id),

  harvest_batch_id  uuid NOT NULL,
  dataset_id        uuid NOT NULL REFERENCES datasets(id),

  -- MOS-UI-158 / MOS-UI-160: REFUSED and FAILED are different answers to the operator.
  state             text NOT NULL DEFAULT 'PENDING'
                      CHECK (state IN ('PENDING','RETRIEVING','CHECKING','SEALING',
                                       'SUCCEEDED','REFUSED','FAILED')),

  -- MOS-UI-134's "named, progress-reported step with the cohort size shown". A counter
  -- per phase rather than a percentage: a percentage of an unknown total is a spinner
  -- with a number on it.
  phase             text NOT NULL DEFAULT 'queued'
                      CHECK (phase IN ('queued','freezing_batch','retrieving_pixels',
                                       'computing_profile','running_checks',
                                       'sealing_version','freezing_split','finished')),
  patients_total      integer NOT NULL DEFAULT 0 CHECK (patients_total      >= 0),
  series_total        integer NOT NULL DEFAULT 0 CHECK (series_total        >= 0),
  series_retrieved    integer NOT NULL DEFAULT 0 CHECK (series_retrieved    >= 0),
  instances_total     integer NOT NULL DEFAULT 0 CHECK (instances_total     >= 0),
  instances_retrieved integer NOT NULL DEFAULT 0 CHECK (instances_retrieved >= 0),

  -- Whether the run actually held pixels. MOS-EVID-018 makes the pixel digest the
  -- caller's to compute and MOS-EVID-034 L4 makes the perceptual hash optional on a
  -- manifest record, so the difference between a cohort whose images were fetched and
  -- one assembled from metadata is INVISIBLE in the sealed DatasetVersion -- and it
  -- decides two of the nine checks. Recorded here so it survives the run.
  consumer_class    text NOT NULL DEFAULT 'dataset_export'
                      CHECK (consumer_class = 'dataset_export'),
  series_with_pixel_digest    integer NOT NULL DEFAULT 0
                      CHECK (series_with_pixel_digest    >= 0),
  series_with_perceptual_hash integer NOT NULL DEFAULT 0
                      CHECK (series_with_perceptual_hash >= 0),

  -- MOS-UI-133's nine rows, written once by the driver. See the header.
  check_battery     jsonb NOT NULL DEFAULT '[]'::jsonb
                      CHECK (jsonb_typeof(check_battery) = 'array'),
  refusals          jsonb NOT NULL DEFAULT '[]'::jsonb
                      CHECK (jsonb_typeof(refusals) = 'array'),

  -- MOS-UI-132: both objects or neither. Null until SUCCEEDED.
  dataset_version_id uuid REFERENCES dataset_versions(id),
  dataset_split_id   uuid REFERENCES dataset_splits(id),
  manifest_digest    sha256_digest,
  split_digest       sha256_digest,
  -- MOS-TRAIN-088 records the FULL C1-C7 result whether it passes or blocks, so this is
  -- non-null on a REFUSED run as well as on a SUCCEEDED one.
  corpus_stratification_report_id uuid REFERENCES corpus_stratification_reports(id),
  reused            boolean NOT NULL DEFAULT false,

  -- MOS-TRAIN-080's named human, from the credential and never from a request body.
  submitted_by      uuid NOT NULL,
  submitted_at      timestamptz NOT NULL DEFAULT now(),
  started_at        timestamptz,
  finished_at       timestamptz,
  -- MOS-API-042: the exception TYPE only on a FAILED run, never its message.
  failure_reason    text,

  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),

  UNIQUE (tenant_id, id),

  -- MOS-UI-132, as two constraints rather than one equality, and the difference is the
  -- case that actually happens.
  --
  -- A SPLIT WITHOUT A VERSION IS IMPOSSIBLE and is refused outright: `dataset_splits`
  -- has a NOT NULL `dataset_version_id`, so a row claiming one and not the other is a
  -- record of something that cannot have occurred.
  --
  -- A VERSION WITHOUT A SPLIT IS POSSIBLE, IS THE PARTIAL OUTCOME, AND MUST BE
  -- RECORDABLE. The first draft of this table wrote the two as an equality -- "both or
  -- neither" -- which reads like `MOS-UI-132` and does the opposite of what it asks:
  -- that requirement says a partial outcome "MUST be reported as a platform fault under
  -- MOS-UI-160", and an equality makes the one row that could report it unstorable. The
  -- DatasetVersion is immutable (`MOS-EVID-013`) and undeletable (`MOS-API-011` answers
  -- 409), so when a split freeze fails after a seal the object EXISTS; refusing to
  -- record which one leaves the operator with an orphan nobody can name. So it is legal
  -- in exactly two states: `SEALING`, the window between the two writes, and `FAILED`,
  -- where it is the fault being reported.
  CONSTRAINT seal_runs_split_implies_version CHECK (
    dataset_split_id IS NULL OR dataset_version_id IS NOT NULL
  ),
  CONSTRAINT seal_runs_orphan_version_is_a_fault CHECK (
    dataset_version_id IS NULL
    OR dataset_split_id IS NOT NULL
    OR state IN ('SEALING', 'FAILED')
  ),
  CONSTRAINT seal_runs_succeeded_names_both CHECK (
    state <> 'SUCCEEDED' OR (dataset_version_id IS NOT NULL
                             AND dataset_split_id IS NOT NULL
                             AND manifest_digest IS NOT NULL
                             AND split_digest IS NOT NULL)
  ),
  -- MOS-API-112: "a missing check is not a pass". A terminal run carries all nine rows
  -- of MOS-UI-133's table; a run still in flight has not run them yet and carries none.
  CONSTRAINT seal_runs_battery_complete CHECK (
    state NOT IN ('SUCCEEDED','REFUSED')
    OR jsonb_array_length(check_battery) = 9
  ),
  -- MOS-UI-105: a refusal with no refusals in it cannot produce the actual observed
  -- value and the actual bound that requirement demands.
  CONSTRAINT seal_runs_refused_says_why CHECK (
    state <> 'REFUSED' OR jsonb_array_length(refusals) >= 1
  ),
  CONSTRAINT seal_runs_failed_names_a_reason CHECK (
    (state = 'FAILED') = (failure_reason IS NOT NULL)
  ),
  CONSTRAINT seal_runs_terminal_is_finished CHECK (
    (state IN ('SUCCEEDED','REFUSED','FAILED')) = (finished_at IS NOT NULL)
  ),
  CONSTRAINT seal_runs_retrieved_within_total CHECK (
    series_retrieved <= series_total AND instances_retrieved <= instances_total
  ),
  CONSTRAINT seal_runs_pixel_evidence_within_total CHECK (
    series_with_pixel_digest <= series_total
    AND series_with_perceptual_hash <= series_total
  )
);

-- MOS-TRAIN-209 makes sealing idempotent on CONTENT, not on the batch: a second seal of
-- the same batch is refused by `seal_from_batch` because the batch is already SEALED.
-- What this index buys is the poll route's lookup and the console's "is there a seal in
-- flight for this batch" question, which MOS-UI-138 needs to answer after a refusal.
CREATE INDEX seal_runs_batch_idx  ON seal_runs (tenant_id, harvest_batch_id,
                                                submitted_at DESC);
CREATE INDEX seal_runs_state_idx  ON seal_runs (tenant_id, state, submitted_at DESC);

CREATE TRIGGER seal_runs_touch BEFORE UPDATE ON seal_runs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- =====================================================================================
-- 2. Identity is sealed. MOS-STORE-359's reasoning, applied to the act.
--
-- A seal run whose batch, dataset or submitter can be rewritten after the fact is a
-- record of a seal nobody can attribute. The MUTABLE half is the progress and the
-- outcome, which is what the driver writes as it runs.
-- =====================================================================================
CREATE OR REPLACE FUNCTION seal_runs_identity_sealed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.public_id        IS DISTINCT FROM OLD.public_id
     OR NEW.tenant_id     IS DISTINCT FROM OLD.tenant_id
     OR NEW.harvest_batch_id IS DISTINCT FROM OLD.harvest_batch_id
     OR NEW.dataset_id    IS DISTINCT FROM OLD.dataset_id
     OR NEW.submitted_by  IS DISTINCT FROM OLD.submitted_by
     OR NEW.submitted_at  IS DISTINCT FROM OLD.submitted_at THEN
    RAISE EXCEPTION
      'seal_runs identity is immutable (MOS-STORE-359): a seal run whose batch, dataset '
      'or submitter can be rewritten is not a record of who sealed what';
  END IF;
  -- MOS-EVID-013: the sealed objects are immutable, so the pointer to them is written
  -- once. Re-pointing a finished seal run at a different DatasetVersion would make the
  -- one durable record of the act say something it did not do.
  IF OLD.dataset_version_id IS NOT NULL
     AND NEW.dataset_version_id IS DISTINCT FROM OLD.dataset_version_id THEN
    RAISE EXCEPTION
      'seal_runs.dataset_version_id is written once (MOS-EVID-013)';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER seal_runs_identity_sealed BEFORE UPDATE ON seal_runs
  FOR EACH ROW EXECUTE FUNCTION seal_runs_identity_sealed();


-- =====================================================================================
-- 3. Row-level security. Identical in shape to 0006 section 11 and 0011 section 8.
-- =====================================================================================
ALTER TABLE seal_runs OWNER TO medicalos_owner;
ALTER TABLE seal_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE seal_runs FORCE  ROW LEVEL SECURITY;

CREATE POLICY seal_runs_tenant_isolation ON seal_runs
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

CREATE TRIGGER seal_runs_tenant_immutable BEFORE UPDATE ON seal_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id');


-- =====================================================================================
-- 4. Grants. NO DELETE: MOS-API-011 puts the sealed objects on the append-only set and
-- the record of the act that made them belongs there with them -- a seal run that can be
-- deleted is a REFUSED seal an operator can make disappear.
-- =====================================================================================
GRANT SELECT, INSERT ON seal_runs TO medicalos_app;
GRANT UPDATE (state, phase, patients_total, series_total, series_retrieved,
              instances_total, instances_retrieved, series_with_pixel_digest,
              series_with_perceptual_hash, check_battery, refusals,
              dataset_version_id, dataset_split_id, manifest_digest, split_digest,
              corpus_stratification_report_id, reused, started_at, finished_at,
              failure_reason, updated_at)
  ON seal_runs TO medicalos_app;
REVOKE DELETE ON seal_runs FROM medicalos_app;
GRANT SELECT ON seal_runs TO medicalos_readonly;


-- =====================================================================================
-- 5. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM pg_policies
   WHERE tablename = 'seal_runs' AND policyname = 'seal_runs_tenant_isolation';
  IF n <> 1 THEN
    RAISE EXCEPTION 'seal_runs has no tenant isolation policy (MOS-SEC-072)';
  END IF;

  SELECT count(*) INTO n FROM pg_class
   WHERE relname = 'seal_runs' AND relrowsecurity AND relforcerowsecurity;
  IF n <> 1 THEN
    RAISE EXCEPTION 'seal_runs does not FORCE row level security (MOS-STORE-224)';
  END IF;

  SELECT count(*) INTO n FROM information_schema.table_privileges
   WHERE table_name = 'seal_runs' AND grantee = 'medicalos_app'
     AND privilege_type = 'DELETE';
  IF n <> 0 THEN
    RAISE EXCEPTION 'medicalos_app may DELETE a seal_runs row (MOS-API-011)';
  END IF;
END $$;
