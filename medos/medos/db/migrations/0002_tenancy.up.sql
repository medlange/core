-- =====================================================================================
-- 0002_tenancy.up.sql -- weeks 3-5, item 1 of docs/spec/15-delivery.md section 15.2.4.
--
--   "tenant_id NOT NULL on every domain table, Postgres row-level security bound to a
--    session variable, and a single repository chokepoint that sets it. This converts
--    cross-tenant leakage from a code-review property into a database impossibility for
--    roughly an afternoon's work."
--
-- This migration is ADDITIVE. CONTRACT.md section 8 deliberately omitted `tenant_id`
-- rather than faking a single-tenant value -- "that is harder to migrate than an absent
-- column" -- so there is no fake value to unpick here, only a documented backfill
-- (section 5 below) of rows written before the column existed.
--
-- Requirements implemented, by id:
--
--   MOS-SEC-072   tenant_id uuid NOT NULL + ENABLE + FORCE RLS + a policy bound to the
--                 session variable `medicalos.tenant_id`, on every tenant-owned table.
--   MOS-SEC-073   five roles; `medicalos_app` owns nothing and is NOBYPASSRLS;
--                 migrations run as `medicalos_migrator`; no superuser process.
--   MOS-SEC-074   the variable is transaction-scoped (`set_config(..., true)`) -- see
--                 medos/db/tenancy.py; this file only makes the unset case fatal.
--   MOS-SEC-077   a table with a tenant_id column and no forced RLS fails CI. The query
--                 is run as an assertion at the END of this file (section 13), so the
--                 migration itself fails rather than CI discovering it later.
--   MOS-STORE-217 every FK between two tenant-owned tables is composite and includes
--                 tenant_id; each parent carries the redundant UNIQUE (tenant_id, id).
--   MOS-STORE-218 tenant_id is immutable, enforced by forbid_column_change('tenant_id').
--   MOS-STORE-221 every index serving a tenant-filtered query leads with tenant_id.
--   MOS-STORE-222 the five roles, spelled as chapter 12 spells them.
--   MOS-STORE-223 ENABLE **and** FORCE. FORCE is the whole point: without it the table
--                 owner -- and `medicalos_migrator` through its membership of
--                 `medicalos_owner` -- bypasses every policy silently.
--   MOS-STORE-224 current_tenant_id() resolves the variable through
--                 current_setting(name, false), which raises SQLSTATE 42704 when unset.
--                 "A connection with no tenant context is not a connection with full
--                 access; it is a connection with no access."
--   MOS-STORE-225 no escape hatch. See section 11 for why the break-glass policy is
--                 deliberately NOT created here.
--   MOS-STORE-228 job_events keeps its append-only posture at the grant level now that a
--                 non-owner application role exists (REVOKE UPDATE, DELETE).
--   MOS-STORE-229 RLS is enabled in the same migration that introduces the column.
--   MOS-STORE-345 the two PRE-TENANCY tables -- `ae_tenant_map` and `quarantine` -- carry
--                 no tenant_id, no RLS and no policy, because they are what DETERMINES
--                 the tenant. A policy on them would be circular: the resolver would
--                 have to know the tenant in order to look up the tenant.
--   MOS-STORE-346 `ae_tenant_map`, keyed UNIQUE (called_aet, calling_aet) (MOS-DATA-046).
--   MOS-STORE-348 `quarantine`, one row per held SOP instance (MOS-DATA-047/048).
--   MOS-STORE-212 the `dicom_uid` domain, introduced here for the two new tables.
--
-- DELIBERATE DEVIATIONS FROM CHAPTER 12, ALL NARROWER THAN THE SPEC:
--
--   * `uuid_generate_v7()` is not defined in this slice (schema.sql uses
--     gen_random_uuid()); the new tables follow the slice, not chapter 12's bootstrap.
--   * `pacs_backends` and `users` do not exist yet, so `ae_tenant_map.pacs_backend`,
--     `quarantine.pacs_backend`, `*.created_by`, `*.released_by` and `*.purged_by` are
--     plain columns rather than foreign keys. Named here so the FK is added with the
--     table rather than discovered missing.
--   * The break-glass policy of MOS-SEC-072 / MOS-STORE-225 is NOT created. See §11.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The predicate function.  MOS-STORE-224 / MOS-SEC-072.
--
-- current_setting(name, false) raises 42704 when the variable was never set in this
-- session or transaction. That error IS the design: the withdrawn NULL-returning form of
-- this function yields an empty result set, which is indistinguishable from "this tenant
-- owns nothing" and hides a missing-context bug until it reaches a report.
-- =====================================================================================
CREATE FUNCTION current_tenant_id() RETURNS uuid LANGUAGE plpgsql STABLE AS $$
DECLARE v text := current_setting('medicalos.tenant_id', false);
BEGIN
  IF v = '' THEN
    RAISE EXCEPTION 'medicalos.tenant_id is not set for this transaction'
      USING ERRCODE = '42704',
            HINT = 'Open the transaction through medos.db.tenancy.tenant_tx() '
                   '(MOS-SEC-075, MOS-STORE-227).';
  END IF;
  RETURN v::uuid;
END $$;

COMMENT ON FUNCTION current_tenant_id() IS
  'MOS-STORE-224: the RLS predicate. Raises 42704 when medicalos.tenant_id is unset.';

-- MOS-STORE-212. Introduced for the two pre-tenancy tables below; the existing UID
-- columns stay `text` + CHECK because converting them is a separate, behaviour-visible
-- change and this migration is additive.
CREATE DOMAIN dicom_uid AS varchar(64)
  CHECK (VALUE ~ '^[0-2](\.(0|[1-9][0-9]*))+$');


-- =====================================================================================
-- 2. Roles.  MOS-STORE-222 / MOS-SEC-073.  Five, and no others.
--
-- Roles are cluster-wide, not database-wide, so every CREATE is guarded: this migration
-- runs against one database in a cluster that may already hold another.
--
-- No password is set here. Credentials are deployment configuration, not schema
-- (medos/db/migrate.py::set_role_password does it from the environment), and a migration
-- that hardcodes one puts it in git.
-- =====================================================================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medicalos_owner') THEN
    CREATE ROLE medicalos_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medicalos_migrator') THEN
    CREATE ROLE medicalos_migrator LOGIN NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medicalos_app') THEN
    CREATE ROLE medicalos_app LOGIN NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medicalos_readonly') THEN
    CREATE ROLE medicalos_readonly LOGIN NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medicalos_backup') THEN
    CREATE ROLE medicalos_backup LOGIN REPLICATION NOBYPASSRLS;
  END IF;
END $$;

-- MOS-STORE-222: the migrator issues DDL through membership of the owner. That is
-- precisely why FORCE is not optional (MOS-STORE-223) -- membership would otherwise
-- carry the owner's silent policy bypass with it.
GRANT medicalos_owner TO medicalos_migrator;

-- The bootstrap principal must be able to hand objects over. A superuser can already;
-- a non-superuser bootstrap role needs the membership.
DO $$
BEGIN
  IF NOT pg_has_role(current_user, 'medicalos_owner', 'MEMBER') THEN
    EXECUTE format('GRANT medicalos_owner TO %I', current_user);
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO medicalos_app, medicalos_readonly, medicalos_migrator;


-- =====================================================================================
-- 3. tenants.  Chapter 12 section 12.6, trimmed to the columns this slice can honour.
--
-- Every column below is chapter 12's, spelling for spelling. The ones omitted --
-- `default_deid_policy_id`, `org_oid_root`'s dependants -- are omitted because the
-- tables they point at do not exist, not because they were reconsidered. There is
-- deliberately NO `default_clinical_use_mode` column: MOS-SAFE-033 forbids a tenant-wide
-- override of `clinical_use_mode`, and a tenant-level default is exactly that override.
-- =====================================================================================
CREATE TABLE tenants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active','suspended','closed')),
  -- MOS-SEC-121: exactly this column, type and default.
  external_llm_allowed boolean NOT NULL DEFAULT false,
  -- MOS-TRAIN-072: exactly this column, type and default. No per-study, per-user or
  -- per-environment override and no platform-administrator bypass.
  training_use_allowed boolean NOT NULL DEFAULT false,
  org_oid_root text CHECK (org_oid_root ~ '^[0-2](\.(0|[1-9][0-9]*))+$'),
  -- Surfaced to the policy engine as `Tenant.residency` (MOS-SEC-053). Deliberately not
  -- named `residency`: `deployments.residency` is an unrelated GPU residency class.
  data_region text NOT NULL DEFAULT 'on-prem',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz
);

CREATE TRIGGER tenants_touch BEFORE UPDATE ON tenants
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMENT ON TABLE tenants IS
  'MOS-STORE-223: the one table keyed on id rather than tenant_id; its policy is '
  'tenants_self (id = current_tenant_id()).';

-- -------------------------------------------------------------------------------------
-- The backfill tenant.  MOS-STORE-244 is the general rule (a rebuild preserves platform
-- state by matching on the tenant-scoped natural key); this is the one-off case of rows
-- that predate the key.
--
-- The id is NOT arbitrary and MUST NOT be regenerated. `medos.db.repo.SLICE_TENANT_ID`
-- is the frozen tenant component of every idempotency key and every deterministic
-- 2.25.* UID this deployment has ever written (MOS-IMG-085: "same UID implies same
-- declared inputs"). Every pre-existing row was therefore derived under exactly this
-- tenant id, and giving those rows any other owner would make their recorded
-- `derivation_inputs` a lie.
-- -------------------------------------------------------------------------------------
INSERT INTO tenants (id, slug, display_name)
VALUES ('00000000-0000-0000-0000-000000000000', 'default',
        'Default tenant (weeks 1-2 backfill; id is the frozen UID-derivation constant)')
ON CONFLICT (id) DO NOTHING;


-- =====================================================================================
-- 4. The PRE-TENANCY tables.  MOS-STORE-345.
--
-- Exactly two exist and the class is CLOSED: `ae_tenant_map` and `quarantine`. A third
-- joins it only by amending MOS-STORE-345.
--
-- Why they cannot carry tenant_id: every other table in the schema is read inside a
-- transaction that already knows its tenant. These two are the tables that ANSWER that
-- question -- `ae_tenant_map` resolves it for Path A ingest, `quarantine` records that it
-- could not be resolved. A tenant-isolation policy on either is a circular definition:
-- the resolver would have to know the tenant in order to look up the tenant.
--
-- So: no `tenant_id` column, no ENABLE/FORCE ROW LEVEL SECURITY, no policy. A tenant
-- reference they hold is an OUTPUT or an ANNOTATION, never an ownership discriminator,
-- and is therefore named for its role -- `resolved_tenant_id`, `owning_tenant_id`,
-- `arriving_tenant_id`, `released_to_tenant_id`. That naming is load-bearing: section
-- 13's MOS-SEC-077 assertion scans pg_attribute for a column literally named
-- `tenant_id` without forced row security, and a pre-tenancy table named the obvious way
-- would make that query cry wolf on the two tables that are correct.
--
-- Containment for these two is a Chapter 8 PERMISSION at the API, not the database
-- (MOS-STORE-345). MOS-STORE-219's platform-global SELECT-only list is NOT widened to
-- include them, because both are written on the ingest path.
-- =====================================================================================
CREATE TABLE ae_tenant_map (                       -- pre-tenancy (MOS-STORE-345)
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- DICOM AE titles: 16 bytes, default repertoire, no backslash, no control characters.
  -- `= btrim(...)` is not decoration: a modality that pads its AE title to 16 characters
  -- and a mapping row typed without the padding are the same AE to a human and two
  -- different strings to an equality lookup, and the failure surfaces as a quarantined
  -- study rather than as an error.
  called_aet text NOT NULL CHECK (
    length(called_aet) BETWEEN 1 AND 16 AND called_aet = btrim(called_aet)
    AND called_aet !~ '[[:cntrl:]]' AND strpos(called_aet, '\') = 0),
  calling_aet text NOT NULL CHECK (
    length(calling_aet) BETWEEN 1 AND 16 AND calling_aet = btrim(calling_aet)
    AND calling_aet !~ '[[:cntrl:]]' AND strpos(calling_aet, '\') = 0),
  -- The resolution OUTPUT. Deliberately not named `tenant_id` (MOS-STORE-345).
  resolved_tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  -- No FK: `pacs_backends` is chapter 12 section 12.7 and does not exist in this slice.
  pacs_backend text NOT NULL,
  ingest_class text NOT NULL DEFAULT 'clinical'      -- MOS-DATA-049
    CHECK (ingest_class IN ('clinical','corpus')),
  stability_seconds integer NOT NULL DEFAULT 60      -- MOS-DATA-051
    CHECK (stability_seconds >= 10),
  max_receive_window_seconds integer NOT NULL DEFAULT 1800   -- MOS-DATA-052
    CHECK (max_receive_window_seconds > stability_seconds),
  description text NOT NULL DEFAULT '',
  -- MOS-STORE-347: a disabled row MUST NOT match, and a lookup matching no enabled row
  -- MUST quarantine rather than fall back to any tenant. The absence of a nullable
  -- default-tenant column is the structural half of MOS-DATA-047's prohibition.
  enabled boolean NOT NULL DEFAULT true,
  created_by uuid,                                   -- no FK: no `users` table yet
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ae_tenant_map_uk UNIQUE (called_aet, calling_aet)   -- MOS-DATA-046
);

CREATE INDEX ae_tenant_map_tenant_idx ON ae_tenant_map (resolved_tenant_id);

CREATE TRIGGER ae_tenant_map_touch BEFORE UPDATE ON ae_tenant_map
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMENT ON TABLE ae_tenant_map IS
  'PRE-TENANCY (MOS-STORE-345/346): resolves the tenant for Path A ingest. No tenant_id '
  'column, no RLS, no policy -- a policy here would be circular.';


CREATE TABLE quarantine (                          -- pre-tenancy (MOS-STORE-345)
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  reason text NOT NULL
    CHECK (reason IN ('tenant_unresolved','tenant_conflict')),   -- MOS-DATA-047/048
  -- Identity ONLY. No patient name, birth date, accession number, description or pixel
  -- data: MOS-STORE-345 forbids them here, and a row about data nobody may look at is
  -- the worst possible place to keep a copy of the data.
  sop_instance_uid dicom_uid NOT NULL,
  series_instance_uid dicom_uid NOT NULL,
  study_instance_uid dicom_uid NOT NULL,
  sop_class_uid dicom_uid,
  modality text,
  pacs_backend text NOT NULL,                      -- no FK: no `pacs_backends` yet
  ingest_path text NOT NULL,
  calling_aet text, called_aet text,
  -- MOS-DATA-048: for `tenant_conflict`, `owning_tenant_id` is tenant A, which already
  -- owns the StudyInstanceUID, and `arriving_tenant_id` is tenant B, which the arriving
  -- route resolved to. Ownership MUST NOT be re-assigned to B. Both are NULL for
  -- `tenant_unresolved`, which is the whole point of the row.
  owning_tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
  arriving_tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
  received_at timestamptz NOT NULL,
  -- The exit (MOS-STORE-350). Nullable timestamps rather than a state enum because
  -- chapter 3 owns no release vocabulary and chapter 12 may not invent one.
  released_to_tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
  released_by uuid, released_at timestamptz,        -- no FK: no `users` table yet
  purged_by uuid, purged_at timestamptz,
  resolution_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT quarantine_instance_uk UNIQUE (pacs_backend, sop_instance_uid),
  CHECK ((reason = 'tenant_conflict')
         = (owning_tenant_id IS NOT NULL AND arriving_tenant_id IS NOT NULL)),
  CHECK (owning_tenant_id IS NULL OR owning_tenant_id <> arriving_tenant_id),
  CHECK ((released_at IS NULL) = (released_to_tenant_id IS NULL)),
  CHECK ((released_at IS NULL) = (released_by IS NULL)),
  CHECK ((purged_at IS NULL) = (purged_by IS NULL)),
  CHECK (released_at IS NULL OR purged_at IS NULL)
);

CREATE INDEX quarantine_open_idx ON quarantine (reason, received_at)
  WHERE released_at IS NULL AND purged_at IS NULL;
-- MOS-STORE-349: the Gateway subtracts every open row from every QIDO-RS response.
-- Study-keyed because a 1,131-instance chest CT from an unmapped AE is 1,131 rows.
CREATE INDEX quarantine_study_idx ON quarantine (study_instance_uid)
  WHERE released_at IS NULL AND purged_at IS NULL;
CREATE INDEX quarantine_sop_idx ON quarantine (sop_instance_uid)
  WHERE released_at IS NULL AND purged_at IS NULL;

CREATE TRIGGER quarantine_touch BEFORE UPDATE ON quarantine
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER quarantine_sealed BEFORE UPDATE ON quarantine
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('reason','sop_instance_uid',
    'series_instance_uid','study_instance_uid','pacs_backend','received_at',
    'owning_tenant_id','arriving_tenant_id');

COMMENT ON TABLE quarantine IS
  'PRE-TENANCY (MOS-STORE-345/348): one row per held SOP instance whose tenant could not '
  'be resolved. No tenant_id column, no RLS, no policy -- reachable without a tenant '
  'context by design, because it exists to record the absence of one.';


-- =====================================================================================
-- 5. tenant_id, added in three steps, and the BACKFILL.
--
-- Three steps and not one because `ADD COLUMN tenant_id uuid NOT NULL DEFAULT
-- current_tenant_id()` evaluates the default once per existing row, and this migration
-- runs with no tenant context -- which is correct: a migration is not a tenant.
--
--   5a. ADD COLUMN, nullable, no default      -- one catalogue rewrite, no table scan
--   5b. UPDATE every NULL to the backfill tenant  (section 3's documented id)
--   5c. SET NOT NULL, then SET DEFAULT current_tenant_id()
--
-- The DEFAULT is what makes every INSERT already written in medos/db work unchanged
-- AND fail loudly when the transaction has no tenant: the default evaluates
-- current_tenant_id(), which raises 42704 (MOS-STORE-224). It is not a convenience --
-- it closes the hole where a write with no tenant context would otherwise have to be
-- caught by a code reviewer noticing an omitted column.
-- =====================================================================================

-- 5a.
ALTER TABLE jobs                 ADD COLUMN tenant_id uuid;
ALTER TABLE job_events           ADD COLUMN tenant_id uuid;
ALTER TABLE job_steps            ADD COLUMN tenant_id uuid;
ALTER TABLE job_series           ADD COLUMN tenant_id uuid;
ALTER TABLE job_queue            ADD COLUMN tenant_id uuid;
ALTER TABLE results              ADD COLUMN tenant_id uuid;
ALTER TABLE result_measurements  ADD COLUMN tenant_id uuid;
ALTER TABLE result_dicom_objects ADD COLUMN tenant_id uuid;

-- 5b. The backfill. Child tables take the parent's tenant rather than the constant, so
-- the UPDATE is correct even on a database where section 3's INSERT found an existing
-- `default` row owning some but not all of the history. `jobs` and `results` are the
-- only two that take the constant directly.
UPDATE jobs    SET tenant_id = '00000000-0000-0000-0000-000000000000'
 WHERE tenant_id IS NULL;
UPDATE results r SET tenant_id = j.tenant_id
  FROM jobs j WHERE j.id = r.job_id AND r.tenant_id IS NULL;

-- `job_events` is append-only and means it: `job_events_no_update` (MOS-EXEC-013,
-- CONTRACT.md section 8) refuses every UPDATE, for every role, including this one. That
-- trigger is doing its job -- it is the reason the weeks 1-2 schema chose a trigger over
-- a GRANT, "unlike a GRANT it also holds against the owner and against a superuser
-- session, which is the case that actually destroys an audit trail".
--
-- A schema migration is the one context in which it must yield, and the yielding is
-- written out here rather than hidden: the trigger is disabled for exactly one statement
-- that sets exactly one previously-nonexistent column, and re-enabled immediately. The
-- assertion in section 13 fails the migration if it is left disabled.
--
-- This is the "ugly backfill later" that 15.2.4 names as the cost of not doing this in
-- weeks 1-2 -- and it is three lines, because CONTRACT.md section 8 left the column
-- absent instead of faking a value. Unpicking a fake tenant would have been the ugly one.
ALTER TABLE job_events DISABLE TRIGGER job_events_no_update;
UPDATE job_events e  SET tenant_id = j.tenant_id
  FROM jobs j WHERE j.id = e.job_id AND e.tenant_id IS NULL;
ALTER TABLE job_events ENABLE TRIGGER job_events_no_update;

UPDATE job_steps s   SET tenant_id = j.tenant_id
  FROM jobs j WHERE j.id = s.job_id AND s.tenant_id IS NULL;
UPDATE job_series x  SET tenant_id = j.tenant_id
  FROM jobs j WHERE j.id = x.job_id AND x.tenant_id IS NULL;
UPDATE job_queue q   SET tenant_id = j.tenant_id
  FROM jobs j WHERE j.id = q.job_id AND q.tenant_id IS NULL;

UPDATE result_measurements m  SET tenant_id = r.tenant_id
  FROM results r WHERE r.id = m.result_id AND m.tenant_id IS NULL;
UPDATE result_dicom_objects d SET tenant_id = r.tenant_id
  FROM results r WHERE r.id = d.result_id AND d.tenant_id IS NULL;

-- 5c.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['jobs','job_events','job_steps','job_series','job_queue',
                           'results','result_measurements','result_dicom_objects'] LOOP
    EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET NOT NULL', t);
    EXECUTE format('ALTER TABLE %I ALTER COLUMN tenant_id SET DEFAULT current_tenant_id()', t);
    EXECUTE format(
      'COMMENT ON COLUMN %I.tenant_id IS %L', t,
      'MOS-SEC-072. DEFAULT current_tenant_id() raises 42704 (MOS-STORE-224) when the '
      'transaction has no tenant context, so a write with no tenant errors rather than '
      'landing somewhere.');
  END LOOP;
END $$;


-- =====================================================================================
-- 6. Composite foreign keys.  MOS-STORE-217.
--
-- "RLS protects reads, composite FKs protect writes, and neither substitutes for the
-- other." Referential-integrity checks in PostgreSQL always bypass row security, so a
-- single-column FK to a parent id would let a row claim tenant A while pointing at
-- tenant B's parent. Every FK below therefore carries tenant_id, which makes a
-- cross-tenant reference unwritable even with RLS switched off entirely.
--
-- Each tenant-owned parent gets the redundant UNIQUE (tenant_id, id) the composite FK
-- needs as its target.
-- =====================================================================================
ALTER TABLE jobs    ADD CONSTRAINT jobs_tenant_id_uk    UNIQUE (tenant_id, id);
ALTER TABLE results ADD CONSTRAINT results_tenant_id_uk UNIQUE (tenant_id, id);

ALTER TABLE jobs ADD CONSTRAINT jobs_tenant_fk
  FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE RESTRICT;

ALTER TABLE job_events           DROP CONSTRAINT job_events_job_id_fkey;
ALTER TABLE job_steps            DROP CONSTRAINT job_steps_job_id_fkey;
ALTER TABLE job_series           DROP CONSTRAINT job_series_job_id_fkey;
ALTER TABLE job_queue            DROP CONSTRAINT job_queue_job_id_fkey;
ALTER TABLE results              DROP CONSTRAINT results_job_id_fkey;
ALTER TABLE results              DROP CONSTRAINT results_superseded_by_fkey;
ALTER TABLE result_measurements  DROP CONSTRAINT result_measurements_result_id_fkey;
ALTER TABLE result_dicom_objects DROP CONSTRAINT result_dicom_objects_result_id_fkey;
ALTER TABLE jobs                 DROP CONSTRAINT jobs_parent_job_id_fkey;

ALTER TABLE job_events ADD CONSTRAINT job_events_job_fk
  FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE job_steps ADD CONSTRAINT job_steps_job_fk
  FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE job_series ADD CONSTRAINT job_series_job_fk
  FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE job_queue ADD CONSTRAINT job_queue_job_fk
  FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE results ADD CONSTRAINT results_job_fk
  FOREIGN KEY (tenant_id, job_id) REFERENCES jobs (tenant_id, id) ON DELETE RESTRICT;
ALTER TABLE results ADD CONSTRAINT results_superseded_by_fk
  FOREIGN KEY (tenant_id, superseded_by) REFERENCES results (tenant_id, id);
ALTER TABLE result_measurements ADD CONSTRAINT result_measurements_result_fk
  FOREIGN KEY (tenant_id, result_id) REFERENCES results (tenant_id, id) ON DELETE CASCADE;
ALTER TABLE result_dicom_objects ADD CONSTRAINT result_dicom_objects_result_fk
  FOREIGN KEY (tenant_id, result_id) REFERENCES results (tenant_id, id) ON DELETE RESTRICT;
-- MOS-EXEC-084 keeps depth at 0 through 0.1-0.2, so this edge carries no rows yet; it is
-- made composite now because retrofitting it once it does is the expensive case.
ALTER TABLE jobs ADD CONSTRAINT jobs_parent_job_fk
  FOREIGN KEY (tenant_id, parent_job_id) REFERENCES jobs (tenant_id, id);


-- =====================================================================================
-- 7. Tenant-scoped natural keys.  MOS-STORE-215 / MOS-STORE-216.
--
-- Chapter 12 section 12.4 reason 1, made concrete: a public collection is a fixed string.
-- Two tenants that ingest LCTSC for different purposes derive the SAME idempotency key
-- and, because UID derivation is deterministic, the SAME SOP Instance UIDs. Under the
-- weeks 1-2 global UNIQUE the second tenant's job is silently folded onto the first
-- tenant's job -- "a cross-tenant leak created by a key choice".
--
-- `jobs.public_id` and `job_events.event_id` keep their GLOBAL uniqueness: both are
-- ULIDs minted per row rather than derived from content, so tenant-scoping them would
-- weaken a key that is already unique, and MOS-STORE-357 makes `public_id` the only job
-- id that appears in a URL -- a globally unique one cannot be confused across tenants.
--
-- The keys that are NOT widened, and why, so the omissions are decisions rather than
-- oversights. Every one of them is keyed on a column that is ALREADY tenant-scoped by a
-- composite foreign key added in section 6, so widening buys nothing and costs a
-- behaviour change:
--   results_job_capability_uk    UNIQUE (job_id, capability_id) -- CONTRACT.md section 8
--                                calls this non-negotiable, and job_id is tenant-scoped.
--   job_series PRIMARY KEY       (job_id, series_instance_uid) -- named by an ON CONFLICT
--                                inference in repo.py::_insert_series_verdicts.
--   job_events_seq_uk            (job_id, seq); job_steps_index_uk / job_steps_key_uk.
--   result_measurements_index_uk (result_id, measurement_index);
--   result_dicom_objects_output_uk (result_id, object_kind, output_index).
-- =====================================================================================
ALTER TABLE jobs DROP CONSTRAINT jobs_idempotency_uk;
ALTER TABLE jobs ADD  CONSTRAINT jobs_idempotency_uk UNIQUE (tenant_id, idempotency_key);

ALTER TABLE result_dicom_objects DROP CONSTRAINT result_dicom_objects_uid_uk;
ALTER TABLE result_dicom_objects ADD  CONSTRAINT result_dicom_objects_uid_uk
  UNIQUE (tenant_id, sop_instance_uid);


-- =====================================================================================
-- 8. Indexes lead with tenant_id.  MOS-STORE-221.
--
-- "One that does not turns a tenant-scoped lookup into a full scan with the RLS
-- predicate applied as a filter, the moment a second tenant exists." Column order after
-- tenant_id and every partial predicate are preserved exactly, so claim order, deadline
-- sweeps and the SSE stream keep the plans the weeks 1-2 tests pin.
-- =====================================================================================
DROP INDEX jobs_active_idx;
CREATE INDEX jobs_active_idx ON jobs (tenant_id, state, requested_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
DROP INDEX jobs_deadline_idx;
CREATE INDEX jobs_deadline_idx ON jobs (tenant_id, deadline_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
DROP INDEX jobs_study_idx;
CREATE INDEX jobs_study_idx ON jobs (tenant_id, study_instance_uid, requested_at DESC);

DROP INDEX job_events_stream_idx;
CREATE INDEX job_events_stream_idx ON job_events (tenant_id, job_id, seq);

-- `job_steps` has no non-unique index: `job_steps_index_uk (job_id, step_index)` serves
-- every read, and job_id is a uuid point lookup that the RLS predicate filters after,
-- not instead of. A tenant-leading duplicate would be a second index for no plan change.

DROP INDEX job_series_selected_idx;
CREATE INDEX job_series_selected_idx ON job_series (tenant_id, job_id, rank)
  WHERE decision = 'selected';

DROP INDEX job_queue_claim_idx;
CREATE INDEX job_queue_claim_idx
  ON job_queue (tenant_id, queue, priority, available_at, enqueued_at)
  WHERE lease_owner IS NULL;
DROP INDEX job_queue_expiry_idx;
CREATE INDEX job_queue_expiry_idx
  ON job_queue (tenant_id, lease_expires_at)
  WHERE lease_owner IS NOT NULL;

DROP INDEX results_job_idx;
CREATE INDEX results_job_idx ON results (tenant_id, job_id);
DROP INDEX results_study_idx;
CREATE INDEX results_study_idx ON results (tenant_id, study_instance_uid, created_at DESC);

DROP INDEX result_measurements_result_idx;
CREATE INDEX result_measurements_result_idx
  ON result_measurements (tenant_id, result_id);
DROP INDEX result_dicom_objects_result_idx;
CREATE INDEX result_dicom_objects_result_idx
  ON result_dicom_objects (tenant_id, result_id);
DROP INDEX result_dicom_objects_series_idx;
CREATE INDEX result_dicom_objects_series_idx
  ON result_dicom_objects (tenant_id, series_instance_uid);


-- =====================================================================================
-- 9. tenant_id is immutable.  MOS-STORE-218.
--
-- "Moving a row between tenants is not supported, the supported operation being export
-- then ingest." An UPDATE that changes tenant_id is refused with 23514 even for the
-- owner, because forbid_column_change() is a trigger and triggers bind every role.
--
-- This is the second layer under the RLS policy: the policy's WITH CHECK already refuses
-- an UPDATE that moves a row to another tenant (the new row fails the check) and its
-- USING already refuses one that moves a row FROM another tenant. The trigger catches
-- the case the policy cannot see -- a superuser or BYPASSRLS session.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['jobs','job_steps','job_series','job_queue',
                           'results','result_measurements','result_dicom_objects'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON %I FOR EACH ROW '
      'EXECUTE FUNCTION forbid_column_change(''tenant_id'')', t || '_tenant_immutable', t);
  END LOOP;
END $$;
-- `job_events` gets no such trigger: MOS-EXEC-013 already forbids every UPDATE on it
-- outright (`job_events_no_update`), and a second BEFORE UPDATE trigger would only
-- change which of the two errors is raised first.


-- =====================================================================================
-- 10. ENABLE + FORCE ROW LEVEL SECURITY, and the policies.
--     MOS-SEC-072, MOS-STORE-223, MOS-STORE-224.
--
-- Ownership moves to `medicalos_owner` first (MOS-STORE-222), then FORCE binds that
-- owner to the policy. Without FORCE the guarantee is hollow: the owner -- and
-- `medicalos_migrator` through its membership, and any operator who connects with owner
-- rights -- reads every tenant silently.
--
-- The policy is written WITHOUT a `TO` clause, i.e. TO PUBLIC. That is chapter 12's
-- rendering (MOS-STORE-223's DDL block) and it is the stronger one: a policy restricted
-- `TO medicalos_app` leaves the owner under FORCE with no applicable policy at all,
-- which in PostgreSQL means zero rows -- silent emptiness, the exact failure mode
-- MOS-STORE-224 exists to abolish. TO PUBLIC makes the owner hit current_tenant_id()
-- and get 42704 instead.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['jobs','job_events','job_steps','job_series','job_queue',
                           'results','result_measurements','result_dicom_objects'] LOOP
    EXECUTE format('ALTER TABLE %I OWNER TO medicalos_owner', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I '
      'USING (tenant_id = current_tenant_id()) '
      'WITH CHECK (tenant_id = current_tenant_id())', t || '_tenant_isolation', t);
  END LOOP;
END $$;

-- `tenants` is the one table keyed on id rather than tenant_id (MOS-STORE-223's DDL).
ALTER TABLE tenants OWNER TO medicalos_owner;
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE  ROW LEVEL SECURITY;
CREATE POLICY tenants_self ON tenants
  USING (id = current_tenant_id()) WITH CHECK (id = current_tenant_id());

-- Platform-global, MOS-STORE-219 class: no tenant_id, no RLS, no PHI, SELECT only for
-- the application role. `job_state_transition` is chapter 5's transition table as data.
ALTER TABLE job_state_transition OWNER TO medicalos_owner;

-- The two pre-tenancy tables are owned like everything else but get NO ENABLE, NO FORCE
-- and NO policy (MOS-STORE-345). Their reachability without a tenant context is the
-- point, not an oversight -- section 13 asserts it.
ALTER TABLE ae_tenant_map OWNER TO medicalos_owner;
ALTER TABLE quarantine    OWNER TO medicalos_owner;


-- =====================================================================================
-- 11. WHY THERE IS NO BREAK-GLASS POLICY HERE.
--
-- MOS-SEC-072 and MOS-STORE-225 define exactly one permitted override:
--
--     CREATE POLICY jobs_break_glass ON jobs FOR SELECT TO medicalos_app
--       USING (current_setting('medicalos.break_glass', true) = 'on');
--
-- It is deliberately NOT created in this migration, and that is a narrowing, not a
-- divergence. The policy is only a control when the things that gate it exist:
-- `break_glass_grants` with `expires_at - requested_at <= 60 minutes` (MOS-SEC-079), a
-- second approver holding `break_glass.invoke` (MOS-SEC-080), and `break_glass_id` on
-- every AuditEvent and PolicyDecision inside the window (MOS-SEC-081). None of those
-- exist in this block. Created now, the policy would be a cross-tenant read for anyone
-- who can execute one `set_config` -- which is every code path in the application.
--
-- It is added in the block that adds `break_glass_grants` and the audit obligations,
-- not before. Until then the platform has no cross-tenant read at all, which is the
-- strictest posture and the easiest one to relax later.
-- =====================================================================================


-- =====================================================================================
-- 12. Grants.  MOS-SEC-073, MOS-STORE-228.
--
-- `medicalos_app` owns nothing and is NOBYPASSRLS, so these grants are the only thing it
-- has -- and RLS is what bounds them to one tenant. MOS-STORE-230: the grant answers
-- "may this role perform this verb", the policy answers "on whose rows"; neither is an
-- authorisation control, which is Chapter 8's policy decision point.
-- =====================================================================================
GRANT SELECT, INSERT, UPDATE, DELETE ON
  jobs, job_steps, job_series, job_queue,
  results, result_measurements, result_dicom_objects
  TO medicalos_app;

-- MOS-STORE-228 / MOS-EXEC-013: `job_events` is append-only, and now that a non-owner
-- application role exists the GRANT is the primary control. `forbid_mutation()` stays as
-- the second layer, and unlike the grant it also binds the owner.
GRANT SELECT, INSERT ON job_events TO medicalos_app;
REVOKE UPDATE, DELETE ON job_events FROM medicalos_app;

-- Platform-global and tenancy catalogue: readable, never written by the app.
GRANT SELECT ON job_state_transition, tenants TO medicalos_app;

-- PRE-TENANCY (MOS-STORE-345): both are written on the INGEST path, which is why
-- MOS-STORE-219's SELECT-only platform-global list is not widened to include them.
-- `quarantine` gets no DELETE: `purged_at` is a tombstone, and the row is the record
-- that unattributed data reached the deployment, which outlives the bytes it describes.
GRANT SELECT, INSERT, UPDATE, DELETE ON ae_tenant_map TO medicalos_app;
GRANT SELECT, INSERT, UPDATE          ON quarantine   TO medicalos_app;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO medicalos_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 13. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================

-- MOS-SEC-077 / MOS-STORE-229, verbatim: a table with a tenant_id column and RLS not
-- both enabled and forced MUST NOT exist.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname) INTO bad
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0
   WHERE n.nspname = 'public' AND c.relkind = 'r'
     AND (c.relrowsecurity = false OR c.relforcerowsecurity = false);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-SEC-077: tenant_id without forced row security on: %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-STORE-345: the pre-tenancy class is exactly two tables, they carry no column named
-- tenant_id, and they carry no row security. Asserted in both directions so that adding
-- a third pre-tenancy table without amending MOS-STORE-345 fails here.
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
   WHERE c.relname IN ('ae_tenant_map','quarantine') AND a.attname = 'tenant_id';
  IF n <> 0 THEN
    RAISE EXCEPTION 'MOS-STORE-345: a pre-tenancy table has a tenant_id column'
      USING ERRCODE = '42501';
  END IF;

  SELECT count(*) INTO n FROM pg_class c
    JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = 'public' AND c.relname IN ('ae_tenant_map','quarantine')
     AND (c.relrowsecurity OR c.relforcerowsecurity);
  IF n <> 0 THEN
    RAISE EXCEPTION 'MOS-STORE-345: a pre-tenancy table has row security'
      USING ERRCODE = '42501';
  END IF;

  SELECT count(*) INTO n FROM pg_policy p
    JOIN pg_class c ON c.oid = p.polrelid
   WHERE c.relname IN ('ae_tenant_map','quarantine');
  IF n <> 0 THEN
    RAISE EXCEPTION 'MOS-STORE-345: a pre-tenancy table has a policy (circular by '
                    'definition: the resolver would have to know the tenant to look up '
                    'the tenant)'
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-SEC-073: the application role must not bypass what this migration just built.
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM pg_roles
   WHERE rolname IN ('medicalos_app','medicalos_migrator','medicalos_readonly')
     AND (rolbypassrls OR rolsuper);
  IF n <> 0 THEN
    RAISE EXCEPTION 'MOS-SEC-073: an application role holds BYPASSRLS or SUPERUSER'
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-EXEC-013: the append-only trigger the backfill borrowed is back on. A migration
-- that ended with it disabled would leave `job_events` mutable for the life of the
-- deployment, and nothing else in the system would ever notice.
DO $$
DECLARE state "char";
BEGIN
  SELECT tgenabled INTO state FROM pg_trigger
   WHERE tgrelid = 'job_events'::regclass AND tgname = 'job_events_no_update';
  IF state IS DISTINCT FROM 'O' THEN
    RAISE EXCEPTION 'MOS-EXEC-013: job_events_no_update is not enabled (tgenabled=%)',
      state USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-STORE-217: every FK out of a tenant-owned table includes tenant_id.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(c.conrelid::regclass::text || '.' || c.conname, ', ') INTO bad
    FROM pg_constraint c
   WHERE c.contype = 'f'
     AND c.connamespace = 'public'::regnamespace
     AND c.conrelid::regclass::text IN ('jobs','job_events','job_steps','job_series',
          'job_queue','results','result_measurements','result_dicom_objects')
     AND c.confrelid::regclass::text <> 'tenants'
     AND NOT EXISTS (
       SELECT 1 FROM pg_attribute a
        WHERE a.attrelid = c.conrelid AND a.attname = 'tenant_id'
          AND a.attnum = ANY (c.conkey));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-STORE-217: non-composite foreign key(s): %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;
