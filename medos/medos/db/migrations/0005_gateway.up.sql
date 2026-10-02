-- =====================================================================================
-- 0005_gateway.up.sql -- the imaging-tenancy projection the DICOM Gateway reads.
--
-- docs/spec/15-delivery.md section 15.2.4 item 3: "The DICOM Gateway holding the only
-- PACS credential, with the viewer and every worker re-pointed at it. Built now because
-- retrofitting a proxy under a working viewer and three workers means re-pointing all of
-- them, and because it is the only place a per-patient PHI-access audit can exist."
--
-- WHAT THIS MIGRATION ADDS AND WHY IT HAS TO EXIST BEFORE THE GATEWAY DOES
--
--   MOS-DATA-010  "Tenancy of imaging data is held in the `studies` projection ... There
--                 is no separate `study_tenancy` table: the projection row *is* the
--                 tenancy record, and it, not the PACS, is authoritative for who may see
--                 a study." Without it, MOS-DATA-011's QIDO filter, MOS-DATA-012's STOW
--                 ownership check and MOS-DATA-013's 404 are all unimplementable, and a
--                 proxy that cannot answer "whose study is this" is not a Gateway.
--   MOS-DATA-022  the per-patient PHI-access audit row needs `patient_key`, "a
--                 tenant-scoped surrogate, never a `PatientID`". A surrogate that is
--                 stable across two accesses to the same patient needs a row to be
--                 stable against; `patients` is that row.
--
-- WHAT IT DOES NOT ADD, ON PURPOSE
--
--   `audit_events`.  Created by `0003_audit_provenance.up.sql`, together with
--   `audit_chain_state` and the append-only apparatus (MOS-STORE-228, MOS-SEC-151). The
--   Gateway writes rows into that table; it does not own it and does not re-declare it.
--   `series` and `instances`.  MOS-STORE-234 names four projection tables and the Gateway
--   reads exactly one, because MOS-DATA-010 puts tenancy on the STUDY. A series
--   projection that no code reads is a second copy of PACS metadata free to drift from
--   the PACS -- the failure MOS-STORE-243 is written about. It arrives with triage
--   (MOS-DATA-059), which is its first consumer.
--   `deid_policies`.  De-identification on egress (MOS-DATA-027..037) applies to the
--   `research_viewer`, `service` and `dataset_export` consumer classes. The two consumer
--   classes this deployment actually runs -- `clinical_viewer` and `platform_writer` --
--   are "De-identification on egress: none" in MOS-DATA-021's table, and a policy table
--   with no policy in it is worse than an absent one. `studies.deid_policy_id` is created
--   NULLable with its FK deferred to the migration that creates `deid_policies`, which is
--   how Chapter 12 section 12.9.2 sequences it.
--
-- ADDITIVE, like 0002 and 0003: it creates tables and alters none of the existing ones.
-- `schema.sql` remains the baseline (MOS-STORE-214).
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. uuid_generate_v7().  Chapter 12's declared default on every table it creates.
--
-- Guarded rather than unconditional: 0003 and this migration were authored in parallel
-- and either may land first, so the one that runs second must not fail on a function the
-- first already defined. Built-ins only -- `gen_random_bytes` would need pgcrypto, and
-- CREATE EXTENSION needs rights `medicalos_migrator` deliberately does not hold
-- (MOS-SEC-073). `gen_random_uuid` is a CSPRNG draw in core since PG 13, so the 74
-- random bits taken from it are as random as pgcrypto's would be.
--
-- Time-ordered ids are not cosmetic here: `studies` is append-heavy and range-scanned by
-- time, and a v4 default scatters every insert across the whole primary-key index.
-- =====================================================================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'uuid_generate_v7') THEN
    EXECUTE $fn$
      CREATE FUNCTION uuid_generate_v7() RETURNS uuid LANGUAGE plpgsql VOLATILE AS $body$
      DECLARE
        ts  bytea := substring(
                       int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint)
                       FROM 3 FOR 6);            -- 48-bit big-endian unix_ts_ms
        rnd bytea := uuid_send(gen_random_uuid());
        b   bytea;
      BEGIN
        b := ts || substring(rnd FROM 7 FOR 10);
        b := set_byte(b, 6, (get_byte(b, 6) & 15) | 112);   -- version 7
        b := set_byte(b, 8, (get_byte(b, 8) & 63) | 128);   -- variant 10xx
        RETURN encode(b, 'hex')::uuid;
      END $body$
    $fn$;
    EXECUTE 'ALTER FUNCTION uuid_generate_v7() OWNER TO medicalos_owner';
    EXECUTE $c$COMMENT ON FUNCTION uuid_generate_v7() IS
      'RFC 9562 UUIDv7. Chapter 12 names it as the default on every table it creates.'$c$;
  END IF;
END $$;


-- =====================================================================================
-- 2. pacs_backends.  MOS-STORE-219 class: platform-global, no tenant_id, no RLS, no PHI.
--
-- MOS-DATA-014 requires that "a single shared backend and one backend per tenant MUST
-- both be expressible without a Gateway code change". This table is what makes the second
-- form a row rather than a schema change: `studies.pacs_backend` records WHICH backend a
-- study was projected from, so the Gateway's `BackendResolver` has something to resolve.
--
-- NOTE WHAT IS ABSENT AND WILL STAY ABSENT: there is no credential column. MOS-DATA-005 --
-- "The credential MUST be delivered to the Gateway process from a secret store at start-up
-- and MUST NOT appear in any image layer, any Helm values file committed to the
-- repository, or any environment variable of any other deployment unit." A password column
-- here would put the PACS credential into every pg_dump, every read replica and every
-- backup, readable by `medicalos_readonly`, which holds SELECT on everything. A base URL
-- is topology; a password is not.
-- =====================================================================================
CREATE TABLE pacs_backends (
  id                   text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{2,31}$'),
  kind                 text NOT NULL CHECK (kind IN ('orthanc','dcm4chee','dicomweb')),
  dicomweb_base_url    text NOT NULL,
  supports_change_feed boolean NOT NULL DEFAULT false,
  partitioning         text NOT NULL
    CHECK (partitioning IN ('shared_gateway_filtered','per_tenant')),
  created_at           timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE pacs_backends IS
  'MOS-STORE-219 global table. Topology only -- MOS-DATA-005 forbids a credential column.';

-- The compose stack's Orthanc. `shared_gateway_filtered` is the honest partitioning
-- value: one Orthanc holds every tenant's pixels and the Gateway is the only thing
-- keeping them apart (MOS-DATA-011).
INSERT INTO pacs_backends (id, kind, dicomweb_base_url, partitioning)
VALUES ('orthanc-local', 'orthanc', 'http://orthanc:8042/dicom-web',
        'shared_gateway_filtered')
ON CONFLICT (id) DO NOTHING;


-- =====================================================================================
-- 3. patients.  MOS-STORE-234: a PROJECTION of what the PACS holds, not a master record.
--
-- The Gateway needs it for exactly one thing -- MOS-DATA-022's `patient_key`. Two tenants
-- holding the same hospital MRN must get two different surrogates, which is why the
-- surrogate is derived from a tenant-scoped row id and not from the MRN.
--
-- `patient_name` and `patient_birth_date` are Chapter 12's columns and are created here
-- so that the projection reconciler of a later block has somewhere to put them. `medos/
-- gateway` writes NEITHER and reads NEITHER: CONTRACT.md section 11 forbids a PHI value in
-- a log, an audit row is a log, and MOS-STORE-307 says the same one level up -- audit rows
-- carry "only the surrogate `patient_id` and, for imaging access, `study_instance_uid`".
-- =====================================================================================
CREATE TABLE patients (
  id                   uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  patient_id_value     text NOT NULL,                -- (0010,0020) as held by the PACS
  issuer_of_patient_id text NOT NULL DEFAULT '',     -- (0010,0021); '' never NULL
  patient_name         text,                         -- (0010,0010)
  patient_birth_date   date,                         -- (0010,0030)
  patient_sex          text CHECK (patient_sex IN ('M','F','O','')),
  phi_state            text NOT NULL
    CHECK (phi_state IN ('identified','pseudonymised','erased')),
  study_count          integer NOT NULL DEFAULT 0,
  first_seen_at        timestamptz NOT NULL DEFAULT now(),
  last_synced_at       timestamptz NOT NULL DEFAULT now(),
  erased_at            timestamptz,
  deleted_at           timestamptz,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT patients_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT patients_natural_uk
    UNIQUE (tenant_id, issuer_of_patient_id, patient_id_value),
  CHECK ((phi_state = 'erased') = (erased_at IS NOT NULL))
);

CREATE TRIGGER patients_touch BEFORE UPDATE ON patients
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- =====================================================================================
-- 4. studies.  MOS-DATA-010, MOS-STORE-356.
--
-- "`study_tenancy` (MOS-DATA-010, MOS-DATA-011, MOS-DATA-012) IS `studies`." MOS-STORE-356
-- forbids adding the other table: "a separate `study_tenancy` would be a second answer to
-- 'who owns this study' that `studies.tenant_id` could silently contradict".
-- =====================================================================================
CREATE TABLE studies (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id           uuid NOT NULL,
  patient_id          uuid NOT NULL,
  study_instance_uid  dicom_uid NOT NULL,
  accession_number    text,
  study_date          date,
  study_time          time,
  study_description   text,
  referring_physician text,
  modalities_in_study text[] NOT NULL DEFAULT '{}',
  series_count        integer NOT NULL DEFAULT 0,
  instance_count      integer NOT NULL DEFAULT 0,
  patient_age_years   integer,                       -- ch. 3 MOS-DATA-061
  patient_sex         text CHECK (patient_sex IN ('M','F','O','')),
  pacs_backend        text NOT NULL REFERENCES pacs_backends(id),
  phi_state           text NOT NULL
    CHECK (phi_state IN ('identified','pseudonymised','erased')),
  -- FK added by the migration that creates `deid_policies` (ch. 12 section 12.9.2).
  deid_policy_id      uuid,
  deid_policy_version integer,                       -- MOS-DATA-028
  first_seen_at       timestamptz NOT NULL DEFAULT now(),
  last_synced_at      timestamptz NOT NULL DEFAULT now(),
  projection_revision bigint NOT NULL DEFAULT 1,
  erased_at           timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT studies_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT studies_natural_uk   UNIQUE (tenant_id, study_instance_uid),
  CONSTRAINT studies_patient_fk FOREIGN KEY (tenant_id, patient_id)
    REFERENCES patients (tenant_id, id) ON DELETE RESTRICT,
  CHECK ((deid_policy_id IS NULL) = (deid_policy_version IS NULL))
);

CREATE INDEX studies_patient_idx ON studies (tenant_id, patient_id, study_date DESC);

-- MOS-STORE-356 / MOS-DATA-012: the one index in the schema that does NOT lead with
-- tenant_id, and an allow-listed MOS-STORE-221 exception because of it. The question it
-- answers is "does any OTHER tenant already own this StudyInstanceUID", which is
-- unanswerable inside one tenant's predicate. It is read by exactly one caller --
-- `study_owner_tenant()` below -- which returns a verdict and never a row.
CREATE INDEX studies_uid_global_idx ON studies (study_instance_uid);

CREATE TRIGGER studies_touch BEFORE UPDATE ON studies
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMENT ON TABLE studies IS
  'MOS-DATA-010 / MOS-STORE-356: this row IS the tenancy record for a study. The PACS is '
  'not authoritative for who may see it.';


-- =====================================================================================
-- 5. study_owner_tenant().  The ONE sanctioned cross-tenant read, and its blast radius.
--
-- MOS-DATA-012 makes the STOW admission path resolve a StudyInstanceUID ACROSS tenants,
-- and says why in the same breath: "a query that cannot see another tenant's row reports
-- the study as unknown and silently splits ownership". Under FORCE row security a
-- tenant-scoped SELECT cannot do that, and MOS-STORE-225 forbids the obvious workaround --
-- "a process-wide connection that reads across tenants MUST NOT exist outside the
-- break-glass path".
--
-- So the cross-tenant read is a SECURITY DEFINER function owned by `medicalos_owner`,
-- which is exactly what Chapter 12 means by "read only by the admission path, which runs
-- as `medicalos_owner` and returns an ownership verdict, never a row". What crosses the
-- boundary is one uuid: the owning tenant, or NULL. No study attribute, no patient
-- attribute, no count, no existence signal beyond the one MOS-DATA-012 requires the
-- admission path to have. MOS-DATA-013's "never a 403, never an existence oracle" is
-- unaffected: the retrieval paths do not call this, only STOW does, and STOW already
-- named the UID it is storing.
--
-- WHY A DEDICATED ROLE AND NOT `medicalos_owner`
-- Chapter 12's sentence says the admission path "runs as `medicalos_owner`", which was
-- written before 0002 chose FORCE ROW LEVEL SECURITY. FORCE binds the owner too -- that is
-- its entire purpose, and 0002 says so: "Without FORCE the guarantee is hollow: the owner
-- -- and `medicalos_migrator` through its membership, and any operator who connects with
-- owner rights -- reads every tenant silently." So a SECURITY DEFINER function owned by
-- `medicalos_owner` does not get a cross-tenant read; it gets 42704 from
-- `current_tenant_id()`, which is the design working.
--
-- The two ways out are `ALTER TABLE studies NO FORCE ROW LEVEL SECURITY`, which hands the
-- owner every column of every row of every tenant, and a NOLOGIN role with BYPASSRLS that
-- owns exactly one function returning exactly one uuid. The second is smaller by orders of
-- magnitude, so it is the one taken. `medicalos_study_oracle` cannot log in, owns no
-- table, and is named in exactly one place in the schema: the OWNER of the function below.
-- REPORTED: Chapter 12's "runs as medicalos_owner" should be re-worded, or MOS-STORE-223
-- should exempt this one function from FORCE; the two sentences as written cannot both
-- hold.
--
-- `search_path` is pinned. A SECURITY DEFINER function with an inherited search_path is
-- the textbook PostgreSQL privilege-escalation shape.
-- =====================================================================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medicalos_study_oracle') THEN
    BEGIN
      CREATE ROLE medicalos_study_oracle NOLOGIN BYPASSRLS;
    EXCEPTION WHEN insufficient_privilege THEN
      RAISE EXCEPTION
        'MOS-DATA-012 needs the role medicalos_study_oracle, and BYPASSRLS can only be '
        'granted by a superuser. Have a DBA run: '
        'CREATE ROLE medicalos_study_oracle NOLOGIN BYPASSRLS; '
        'then re-run this migration.'
        USING ERRCODE = '42501';
    END;
  END IF;
  IF NOT (SELECT rolbypassrls FROM pg_roles WHERE rolname = 'medicalos_study_oracle') THEN
    RAISE EXCEPTION
      'medicalos_study_oracle exists without BYPASSRLS; study_owner_tenant() would '
      'return NULL for every study owned by another tenant, and MOS-DATA-012 would '
      'silently split ownership. Fix the role, do not relax the check.';
  END IF;
END $$;

CREATE FUNCTION study_owner_tenant(p_study_instance_uid dicom_uid)
  RETURNS uuid
  LANGUAGE sql
  STABLE
  SECURITY DEFINER
  SET search_path = pg_catalog, public
AS $$
  SELECT s.tenant_id
    FROM studies s
   WHERE s.study_instance_uid = p_study_instance_uid
     AND s.erased_at IS NULL
   ORDER BY s.created_at
   LIMIT 1;
$$;

COMMENT ON FUNCTION study_owner_tenant(dicom_uid) IS
  'MOS-DATA-012: the STOW admission verdict. Returns the owning tenant id or NULL. '
  'SECURITY DEFINER on purpose; the only cross-tenant read in the schema, and it returns '
  'a uuid, never a row.';


-- =====================================================================================
-- 6. Ownership, row security, grants.  MOS-STORE-229: in the same migration as the table.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['patients','studies'] LOOP
    EXECUTE format('ALTER TABLE %I OWNER TO medicalos_owner', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I '
      'USING (tenant_id = current_tenant_id()) '
      'WITH CHECK (tenant_id = current_tenant_id())', t || '_tenant_isolation', t);
    -- 0002's rule, applied to the new tables: a row may not change tenant after insert.
    -- WITH CHECK alone does not stop it, because a row updated INTO the current tenant
    -- passes both halves of the policy.
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION forbid_column_change(%L)',
      t || '_tenant_immutable', t, 'tenant_id');
  END LOOP;
END $$;

ALTER TABLE pacs_backends OWNER TO medicalos_owner;   -- MOS-STORE-219: global, no RLS

-- The whole blast radius of BYPASSRLS in this schema: one function, one uuid out.
ALTER FUNCTION study_owner_tenant(dicom_uid) OWNER TO medicalos_study_oracle;
GRANT SELECT ON studies TO medicalos_study_oracle;

GRANT SELECT, INSERT, UPDATE ON patients, studies TO medicalos_app;
GRANT SELECT ON pacs_backends TO medicalos_app;
GRANT EXECUTE ON FUNCTION study_owner_tenant(dicom_uid) TO medicalos_app;
GRANT SELECT ON pacs_backends, patients, studies TO medicalos_readonly;

-- DELETE is granted on neither projection table, and that is not an omission:
-- MOS-STORE-239 makes erasure a tombstone (`erased_at`), never a row removal, because a
-- deleted `studies` row is a study whose audit trail can no longer be joined to anything
-- -- and MOS-STORE-307's whole point is that the trail survives erasure intact.


-- =====================================================================================
-- 7. Assertions. Same posture as 0002 section 13 and 0003 section 6: this migration
--    fails rather than leaving a hole for CI to find in a fortnight.
-- =====================================================================================
DO $$
DECLARE bad text;
BEGIN
  -- MOS-STORE-229 / MOS-SEC-072: both ENABLE and FORCE, on both tables. ENABLE without
  -- FORCE leaves `medicalos_owner` -- and `medicalos_migrator` through its membership --
  -- reading every tenant silently.
  SELECT string_agg(relname, ', ') INTO bad
    FROM pg_class
   WHERE relname IN ('patients','studies')
     AND NOT (relrowsecurity AND relforcerowsecurity);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-STORE-229: row security not forced on %', bad;
  END IF;

  -- MOS-STORE-239: no DELETE for the application role on either projection table.
  IF has_table_privilege('medicalos_app', 'studies', 'DELETE')
     OR has_table_privilege('medicalos_app', 'patients', 'DELETE') THEN
    RAISE EXCEPTION 'MOS-STORE-239: erasure is a tombstone; DELETE must not be granted'
      USING ERRCODE = '42501';
  END IF;

  -- MOS-DATA-010's key set, asserted rather than assumed: the Gateway's entire tenancy
  -- argument rests on `studies_natural_uk` being UNIQUE and `studies_uid_global_idx`
  -- existing and NOT being unique (a unique global index would forbid two tenants from
  -- ever holding the same StudyInstanceUID, which is a legitimate state -- two hospitals
  -- can both hold the same public dataset).
  IF NOT EXISTS (SELECT 1 FROM pg_constraint
                  WHERE conname = 'studies_natural_uk' AND contype = 'u') THEN
    RAISE EXCEPTION 'MOS-DATA-010: studies_natural_uk missing';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                  WHERE c.relname = 'studies_uid_global_idx' AND NOT i.indisunique) THEN
    RAISE EXCEPTION 'MOS-DATA-010: studies_uid_global_idx missing or unique';
  END IF;

  -- MOS-DATA-012: the oracle must actually be able to see across tenants, and must be
  -- the ONLY thing in the schema that can. Both halves are asserted: a SECURITY DEFINER
  -- function whose owner lost BYPASSRLS returns NULL for every foreign-owned study, which
  -- reads as "unknown" and splits ownership exactly as MOS-DATA-012 warns.
  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
     WHERE p.proname = 'study_owner_tenant' AND p.prosecdef AND r.rolbypassrls
  ) THEN
    RAISE EXCEPTION
      'MOS-DATA-012: study_owner_tenant() must be SECURITY DEFINER owned by a BYPASSRLS '
      'role, or its cross-tenant verdict is always NULL';
  END IF;
  SELECT string_agg(r.rolname, ', ') INTO bad
    FROM pg_roles r
   WHERE r.rolbypassrls AND NOT r.rolsuper
     AND r.rolname LIKE 'medicalos%' AND r.rolname <> 'medicalos_study_oracle';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-SEC-073: unexpected BYPASSRLS role(s): %', bad;
  END IF;

  -- MOS-DATA-005: no credential column on pacs_backends, now or ever.
  SELECT string_agg(attname, ', ') INTO bad
    FROM pg_attribute
   WHERE attrelid = 'pacs_backends'::regclass AND attnum > 0
     AND attname ~ '(password|secret|credential|token|api_key)';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-DATA-005: pacs_backends must hold no credential (found %)', bad;
  END IF;
END $$;
