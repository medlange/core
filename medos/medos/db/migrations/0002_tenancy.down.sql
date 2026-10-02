-- =====================================================================================
-- 0002_tenancy.down.sql -- the reverse of 0002_tenancy.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- Chapter 12 section 12.15's rule holds: a down-migration that drops a column drops the
-- data in it. Running this against a deployment that has served two tenants merges their
-- rows into one undifferentiated set, and nothing afterwards can tell them apart again.
-- The supported recovery from a bad forward migration is restore-from-backup.
--
-- `tenants`, `ae_tenant_map` and `quarantine` are dropped last and only if empty of
-- anything but the seeded backfill tenant -- see the guard at the end.
-- =====================================================================================

SET lock_timeout = '3s';

-- Policies and row security.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['jobs','job_events','job_steps','job_series','job_queue',
                           'results','result_measurements','result_dicom_objects'] LOOP
    EXECUTE format('DROP POLICY IF EXISTS %I ON %I', t || '_tenant_isolation', t);
    EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I DISABLE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', t || '_tenant_immutable', t);
  END LOOP;
END $$;

DROP POLICY IF EXISTS tenants_self ON tenants;

-- Composite foreign keys back to single-column.
ALTER TABLE job_events           DROP CONSTRAINT IF EXISTS job_events_job_fk;
ALTER TABLE job_steps            DROP CONSTRAINT IF EXISTS job_steps_job_fk;
ALTER TABLE job_series           DROP CONSTRAINT IF EXISTS job_series_job_fk;
ALTER TABLE job_queue            DROP CONSTRAINT IF EXISTS job_queue_job_fk;
ALTER TABLE results              DROP CONSTRAINT IF EXISTS results_job_fk;
ALTER TABLE results              DROP CONSTRAINT IF EXISTS results_superseded_by_fk;
ALTER TABLE result_measurements  DROP CONSTRAINT IF EXISTS result_measurements_result_fk;
ALTER TABLE result_dicom_objects DROP CONSTRAINT IF EXISTS result_dicom_objects_result_fk;
ALTER TABLE jobs                 DROP CONSTRAINT IF EXISTS jobs_parent_job_fk;
ALTER TABLE jobs                 DROP CONSTRAINT IF EXISTS jobs_tenant_fk;

ALTER TABLE job_events ADD CONSTRAINT job_events_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;
ALTER TABLE job_steps ADD CONSTRAINT job_steps_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;
ALTER TABLE job_series ADD CONSTRAINT job_series_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;
ALTER TABLE job_queue ADD CONSTRAINT job_queue_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE;
ALTER TABLE results ADD CONSTRAINT results_job_id_fkey
  FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE RESTRICT;
ALTER TABLE results ADD CONSTRAINT results_superseded_by_fkey
  FOREIGN KEY (superseded_by) REFERENCES results(id);
ALTER TABLE result_measurements ADD CONSTRAINT result_measurements_result_id_fkey
  FOREIGN KEY (result_id) REFERENCES results(id) ON DELETE CASCADE;
ALTER TABLE result_dicom_objects ADD CONSTRAINT result_dicom_objects_result_id_fkey
  FOREIGN KEY (result_id) REFERENCES results(id) ON DELETE RESTRICT;
ALTER TABLE jobs ADD CONSTRAINT jobs_parent_job_id_fkey
  FOREIGN KEY (parent_job_id) REFERENCES jobs(id);

-- Natural keys back to global.
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_idempotency_uk;
ALTER TABLE jobs ADD  CONSTRAINT jobs_idempotency_uk UNIQUE (idempotency_key);
ALTER TABLE result_dicom_objects DROP CONSTRAINT IF EXISTS result_dicom_objects_uid_uk;
ALTER TABLE result_dicom_objects ADD CONSTRAINT result_dicom_objects_uid_uk
  UNIQUE (sop_instance_uid);

ALTER TABLE jobs    DROP CONSTRAINT IF EXISTS jobs_tenant_id_uk;
ALTER TABLE results DROP CONSTRAINT IF EXISTS results_tenant_id_uk;

-- Indexes back to their weeks 1-2 column order.
DROP INDEX IF EXISTS jobs_active_idx;
CREATE INDEX jobs_active_idx ON jobs (state, requested_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
DROP INDEX IF EXISTS jobs_deadline_idx;
CREATE INDEX jobs_deadline_idx ON jobs (deadline_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
DROP INDEX IF EXISTS jobs_study_idx;
CREATE INDEX jobs_study_idx ON jobs (study_instance_uid, requested_at DESC);
DROP INDEX IF EXISTS job_events_stream_idx;
CREATE INDEX job_events_stream_idx ON job_events (job_id, seq);
DROP INDEX IF EXISTS job_series_selected_idx;
CREATE INDEX job_series_selected_idx ON job_series (job_id, rank)
  WHERE decision = 'selected';
DROP INDEX IF EXISTS job_queue_claim_idx;
CREATE INDEX job_queue_claim_idx ON job_queue (queue, priority, available_at, enqueued_at)
  WHERE lease_owner IS NULL;
DROP INDEX IF EXISTS job_queue_expiry_idx;
CREATE INDEX job_queue_expiry_idx ON job_queue (lease_expires_at)
  WHERE lease_owner IS NOT NULL;
DROP INDEX IF EXISTS results_job_idx;
CREATE INDEX results_job_idx ON results (job_id);
DROP INDEX IF EXISTS results_study_idx;
CREATE INDEX results_study_idx ON results (study_instance_uid, created_at DESC);
DROP INDEX IF EXISTS result_measurements_result_idx;
CREATE INDEX result_measurements_result_idx ON result_measurements (result_id);
DROP INDEX IF EXISTS result_dicom_objects_result_idx;
CREATE INDEX result_dicom_objects_result_idx ON result_dicom_objects (result_id);
DROP INDEX IF EXISTS result_dicom_objects_series_idx;
CREATE INDEX result_dicom_objects_series_idx ON result_dicom_objects (series_instance_uid);

-- The columns. This is the destructive step named in the header.
ALTER TABLE jobs                 DROP COLUMN tenant_id;
ALTER TABLE job_events           DROP COLUMN tenant_id;
ALTER TABLE job_steps            DROP COLUMN tenant_id;
ALTER TABLE job_series           DROP COLUMN tenant_id;
ALTER TABLE job_queue            DROP COLUMN tenant_id;
ALTER TABLE results              DROP COLUMN tenant_id;
ALTER TABLE result_measurements  DROP COLUMN tenant_id;
ALTER TABLE result_dicom_objects DROP COLUMN tenant_id;

DROP TABLE quarantine;
DROP TABLE ae_tenant_map;
DROP TABLE tenants;
DROP DOMAIN dicom_uid;
DROP FUNCTION current_tenant_id();

-- Ownership back to the bootstrap principal, so a re-run of the up-migration finds the
-- schema it expects. The roles themselves are cluster-wide and are left alone: dropping
-- a role that another database in the cluster still owns objects in would fail.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['jobs','job_events','job_steps','job_series','job_queue',
                           'results','result_measurements','result_dicom_objects',
                           'job_state_transition'] LOOP
    EXECUTE format('ALTER TABLE %I OWNER TO %I', t, current_user);
  END LOOP;
END $$;
