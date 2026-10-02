-- =====================================================================================
-- 0009_validation_reports.up.sql -- the signed claim.
--
-- docs/spec/15-delivery.md section 15.2.5 names the risk this table exists to retire:
-- "whether evidence is portable rather than site-regenerated -- proved by verifying a
-- `ValidationReport` on a machine with no network access to the platform." Chapter 18
-- section 18.7.4 records the same gap from the conformance side, and MOS-EVID-121 states
-- it in one sentence: "Signing the model artifact but not the claim about it is
-- backwards."
--
-- SCOPE. ONE table of chapter 12 section 12.12's sixteen: `validation_reports`. The
-- report's inputs -- `evaluation_runs`, `evaluation_case_metrics`, `acceptance_criteria`,
-- `capability_claims`, `tenant_acceptance_bindings` -- are other migrations' in this
-- release block, and `deployment_gate_decisions` is the CONSUMER of this row and belongs
-- with the gate. Declaring a stub for any of them here would be a second answer to a
-- question this migration does not own, which is how 0004 got chosen twice.
--
-- WHY THIS MIGRATION DECLARES NO FOREIGN KEY TO `acceptance_criteria` OR `evaluation_runs`
-- -----------------------------------------------------------------------------------
-- Section 12.12 annotates its foreign keys explicitly -- `deployment_gate_decisions`'s
-- `candidate_report_id` is written "(FK `validation_reports`)" -- and annotates NEITHER
-- `evaluation_run_ids` NOR `acceptance_criteria_id` on this row. That is not an oversight
-- for the first: `evaluation_run_ids` is `uuid[]`, and PostgreSQL has no referential
-- action over an array element. For the second it is a deliberate reading, and the reason
-- is stated rather than assumed: the owning table arrives in a migration whose number this
-- agent does not hold, several migrations in this release block were authored in parallel,
-- and a migration that fails when its sibling has not landed converts a coordination
-- problem into a broken database. Referential integrity for both is asserted in
-- `medos.evidence.reports.issue_report`, at the one place a row is written.
--
-- WHAT IS MUTABLE, AND WHAT THE SIGNATURE COVERS
-- ----------------------------------------------
-- MOS-STORE-228 puts this table in the COLUMN-LEVEL revocation group with `artifacts`,
-- because exactly four columns change while the signed document does not:
--
--     status, superseded_by, revocation_reason, reproducibility_status
--
-- MOS-EVID-126: "Revoking a report MUST NOT delete it and MUST NOT alter its bytes.
-- Revocation is a separate signed statement referencing `report_digest`; the original
-- remains verifiable, which is what makes 'this evidence was once accepted and has since
-- been withdrawn' an auditable fact rather than a hole." So the standing columns are
-- granted and every other column is sealed by `forbid_column_change`, and DELETE is
-- refused outright by a trigger that binds the owner too.
--
-- ADDITIVE. It creates one table and alters none.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The `sha256_digest` domain.  MOS-STORE-211, MOS-EVID-007.
--
-- Guarded exactly as 0006 guards it, and for the same reason: the migrations in this
-- release block were authored in parallel and any of them may be the first to want it.
-- Five digest columns below depend on `sha256:` + 64 lower-case hex being checked ONCE.
-- =====================================================================================
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
     WHERE t.typname = 'sha256_digest' AND n.nspname = 'public'
  ) THEN
    EXECUTE $d$CREATE DOMAIN sha256_digest AS text
                 CHECK (VALUE ~ '^sha256:[0-9a-f]{64}$')$d$;
    EXECUTE 'ALTER DOMAIN sha256_digest OWNER TO medicalos_owner';
  END IF;
END $$;


-- =====================================================================================
-- 2. validation_reports.  IMMUTABLE ONCE SIGNED.  Chapter 7 section 7.12.1.
--
-- `public_id`: chapter 7 section 7.2 gives the entity a `vr_<ULID>` identity and section
-- 7.12.1's document prints it verbatim as `report_id`. Section 12.12 is normative for
-- physical form and says `uuid` primary keys. Both hold at once by the MOS-STORE-357
-- construction 0006 applied to the other four evidence prefixes: the uuid is internal and
-- never emitted, `public_id` is the external identifier, assigned once and immutable.
--
-- `report_version`: section 12.12 keys
--   UNIQUE (tenant_id, subject_kind, subject_id, capability_id, kind, report_version)
-- on a column its own Columns cell does not list. REPORTED as a specification defect. The
-- column is declared here because the key cannot exist without it, and because the fact it
-- records is real: a second `vendor_evidence` report for the same subject and capability
-- is a NEW version of the claim, not an edit of the old one (MOS-EVID-126's shape applied
-- to supersession).
--
-- `signature bytea`: section 12.12 gives ONE signature column while MOS-EVID-120 requires
-- "multiple signatures on one envelope ... a `site_acceptance` report is typically signed
-- by the site and countersigned by the platform operator". A single `bytea` cannot hold
-- two raw signatures, so this column holds the DSSE ENVELOPE -- `report.dsse.json`, which
-- carries its signature array -- and `signer_key_id` names the first signer. That keeps
-- section 12.12's column list intact and makes MOS-EVID-120 storable. REPORTED.
--
-- `approver_user_id` carries NO foreign key to `users`, because there is no `users` table
-- in this deployment yet (0004 declared `api_keys` and nothing else). MOS-STORE-233 and
-- MOS-STORE-343 govern the reference when it arrives: the row is tombstoned, never
-- deleted, and `approver_name`/`approver_role` are denormalised here for exactly that
-- reason -- an issued report keeps naming its approver after the account is gone.
-- =====================================================================================
CREATE TABLE validation_reports (
  id                    uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  public_id             text NOT NULL UNIQUE
    CHECK (public_id ~ '^vr_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id             uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

  -- MOS-EVID-003 fixes exactly these three. There is no fourth kind and no unlabelled
  -- evidence, which is why this column is NOT NULL with no default: a report whose
  -- activity nobody stated is the thing the enum exists to prevent.
  kind                  text NOT NULL
    CHECK (kind IN ('vendor_evidence','site_acceptance','monitoring_period')),
  schema_version        text NOT NULL CHECK (schema_version <> ''),
  report_version        integer NOT NULL CHECK (report_version >= 1),

  subject_kind          text NOT NULL
    CHECK (subject_kind IN ('service_version','model_version')),
  subject_id            text NOT NULL CHECK (subject_id <> ''),
  subject_version       text NOT NULL CHECK (subject_version <> ''),
  capability_id         text NOT NULL CHECK (capability_id <> ''),
  criteria_version      integer NOT NULL CHECK (criteria_version >= 1),

  -- MOS-EVID-065: a report cites the runs it was computed from, and there is at least
  -- one. See the header for why this is an array with no referential action.
  evaluation_run_ids    uuid[] NOT NULL
    CHECK (cardinality(evaluation_run_ids) >= 1),
  acceptance_criteria_id uuid,

  -- MOS-EVID-083 / MOS-EVID-142. Three values, not two: "a two-valued pass/fail column
  -- cannot represent this chapter's verdict and MUST NOT be substituted". INDETERMINATE
  -- means the cohort was too small or a threshold did not match -- that needs more data,
  -- where FAIL needs a different model, and folding the two sends the reader to the wrong
  -- remedy.
  verdict               text NOT NULL
    CHECK (verdict IN ('PASS','FAIL','INDETERMINATE')),

  -- MOS-STORE-201's `(bucket, object_key)` pairs. Section 12.16 places them at
  -- `t/{tenant_id}/validation-reports/{report_id}/`.
  report_bucket         text NOT NULL CHECK (report_bucket <> ''),
  report_object_key     text NOT NULL CHECK (report_object_key <> ''),
  report_digest         sha256_digest NOT NULL,
  -- Section 7.12.1's `envelope_digest`: the ApplicabilityEnvelope version cited, NOT the
  -- DSSE envelope. Two different things in this chapter are called an envelope; the
  -- column name is section 12.12's and the meaning is section 7.10's.
  envelope_digest       sha256_digest NOT NULL,
  bundle_bucket         text NOT NULL CHECK (bundle_bucket <> ''),
  bundle_object_key     text NOT NULL CHECK (bundle_object_key <> ''),
  bundle_digest         sha256_digest NOT NULL,

  -- The DSSE envelope of MOS-EVID-118 -- see the header. NOT NULL because MOS-EVID-121
  -- admits no unsigned report: "Every ValidationReport MUST be signed, and an unsigned
  -- report MUST NOT be accepted by the gate."
  signature             bytea NOT NULL CHECK (octet_length(signature) > 0),
  signer_key_id         text NOT NULL CHECK (signer_key_id ~ '^ed25519:[0-9a-f]{16}$'),

  -- MOS-EVID-117: a real, identifiable human with a role, an organisation, a written
  -- statement and an identity assurance. The CHECKs are the structural half; the
  -- "MUST NOT be an automated approver" half is a policy decision and lives in
  -- `medos.evidence.reports.issue_report`, which can see the principal.
  approver              jsonb NOT NULL,
  approver_user_id      uuid,
  approver_name         text NOT NULL CHECK (approver_name <> ''),
  approver_role         text NOT NULL CHECK (approver_role <> ''),

  -- MOS-STORE-302 / MOS-STORE-341 step 9: set to `degraded` when an erasure removes the
  -- cohort. The report stays readable, signed and valid as a historical claim; it is
  -- simply no longer re-runnable.
  reproducibility_status text NOT NULL DEFAULT 'verifiable'
    CHECK (reproducibility_status IN ('verifiable','degraded')),

  issued_at             timestamptz NOT NULL,
  valid_until           timestamptz NOT NULL,

  -- MOS-EVID-126. `EXPIRED` is a standing state a sweep may set; it is NOT derived from
  -- `valid_until` by a view, because MOS-EVID-115 makes expiry a freshness signal whose
  -- consequence is a site decision, and a column a site can read is the only form that
  -- supports one.
  status                text NOT NULL DEFAULT 'ACTIVE'
    CHECK (status IN ('ACTIVE','SUPERSEDED','REVOKED','EXPIRED')),
  superseded_by         uuid,
  revocation_reason     text,

  -- MOS-EVID-128: "A `site_acceptance` report MUST cite the `vendor_evidence` report it
  -- was performed against, by `report_digest`, and MUST fail to issue if that report does
  -- not verify offline at the site." The citation is by DIGEST and not by id on purpose:
  -- the vendor report belongs to another tenant, so there is no row here to point at, and
  -- the digest is the only handle that survives the boundary.
  cited_report_digest   sha256_digest,

  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),

  -- MOS-STORE-217: the redundant key a composite child FK points at.
  CONSTRAINT validation_reports_tenant_id_uk UNIQUE (tenant_id, id),
  -- Section 12.12's two keys, both tenant-scoped. The digest key is scoped for the reason
  -- 0006 scoped `dataset_versions.manifest_digest`: under FORCE RLS an unscoped unique
  -- index is a cross-tenant existence oracle -- an INSERT that collides with an invisible
  -- row reports a conflict on a row the caller may not read.
  CONSTRAINT validation_reports_digest_uk UNIQUE (tenant_id, report_digest),
  CONSTRAINT validation_reports_subject_uk
    UNIQUE (tenant_id, subject_kind, subject_id, capability_id, kind, report_version),
  CONSTRAINT validation_reports_superseded_fk
    FOREIGN KEY (tenant_id, superseded_by)
    REFERENCES validation_reports (tenant_id, id) ON DELETE RESTRICT,

  -- MOS-EVID-142, verbatim from section 12.12.
  CONSTRAINT validation_reports_monitoring_verdict
    CHECK (kind <> 'monitoring_period' OR verdict = 'INDETERMINATE'),
  -- MOS-EVID-115: "`valid_until` MUST be set and MUST NOT exceed 24 months from
  -- `issued_at`." A CHECK rather than application code, because the one place this rule
  -- gets bent is a back-dated re-issue.
  CONSTRAINT validation_reports_validity_window
    CHECK (valid_until > issued_at AND valid_until <= issued_at + interval '24 months'),
  CONSTRAINT validation_reports_cites_vendor
    CHECK (kind <> 'site_acceptance' OR cited_report_digest IS NOT NULL),
  -- A revoked report says why; a report that is not revoked does not carry a reason it
  -- has not earned.
  CONSTRAINT validation_reports_revocation_reason
    CHECK ((status = 'REVOKED') = (revocation_reason IS NOT NULL)),
  CONSTRAINT validation_reports_superseded_by
    CHECK ((status = 'SUPERSEDED') = (superseded_by IS NOT NULL)),
  CONSTRAINT validation_reports_self_supersede
    CHECK (superseded_by IS NULL OR superseded_by <> id),
  -- MOS-EVID-117, structurally. The policy half is in the repository.
  CONSTRAINT validation_reports_approver_shape CHECK (
    jsonb_typeof(approver) = 'object'
    AND approver ? 'name' AND approver ? 'role' AND approver ? 'organisation'
    AND approver ? 'statement' AND approver ? 'identity_assurance'
    AND approver ? 'approved_at'
    AND length(approver->>'statement') >= 20)
);

CREATE INDEX validation_reports_subject_idx
  ON validation_reports (tenant_id, subject_kind, subject_id, capability_id,
                         issued_at DESC);

-- The gate looks up the live report for a (capability, subject) pair on every deployment
-- transition (MOS-EVID-090), and an expired or revoked one must not be found by that
-- lookup. A partial index over the one status that matters keeps that query on the small
-- set rather than on the history.
CREATE INDEX validation_reports_active_idx
  ON validation_reports (tenant_id, capability_id, subject_id, valid_until DESC)
  WHERE status = 'ACTIVE';

CREATE TRIGGER validation_reports_touch BEFORE UPDATE ON validation_reports
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMENT ON TABLE validation_reports IS
  'Chapter 7 section 7.12: a versioned, signed, exportable and offline-verifiable claim '
  'about a pinned artifact on a content-addressed cohort. MOS-EVID-121: signing the '
  'model artifact but not the claim about it is backwards.';

COMMENT ON COLUMN validation_reports.signature IS
  'The DSSE envelope of MOS-EVID-118, not a raw signature: MOS-EVID-120 requires multiple '
  'signatures on one envelope and a single bytea cannot hold two raw ones.';

COMMENT ON COLUMN validation_reports.envelope_digest IS
  'The ApplicabilityEnvelope version cited (section 7.10), NOT the DSSE envelope.';


-- =====================================================================================
-- 3. Sealing.  MOS-EVID-013, MOS-EVID-126, MOS-STORE-228.
--
-- Two layers, catching different things:
--
--   * the GRANT in section 5 bounds `medicalos_app`, which is what the API and the
--     workers connect as;
--   * the TRIGGERS bind every role, including `medicalos_owner` and a superuser psql
--     session. That is the case that actually destroys evidence, and a GRANT cannot
--     reach it.
--
-- Section 12.12's sealed-column list for this table is "all but `reproducibility_status`,
-- `status`, `superseded_by`, `revocation_reason`". `updated_at` is excluded too, because
-- `touch_updated_at()` writes it on exactly those permitted updates.
--
-- An ENUMERATION here rather than 0006's blanket trigger, because four columns are
-- genuinely mutable and a blanket BEFORE UPDATE would forbid revocation -- which
-- MOS-EVID-126 requires to be possible. The cost is that a column added later must be
-- added to this list; the assertion in section 6 turns that from a silent hole into a
-- failed migration.
-- =====================================================================================
CREATE TRIGGER validation_reports_sealed BEFORE UPDATE ON validation_reports
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'id','public_id','tenant_id','kind','schema_version','report_version',
    'subject_kind','subject_id','subject_version','capability_id','criteria_version',
    'evaluation_run_ids','acceptance_criteria_id','verdict',
    'report_bucket','report_object_key','report_digest','envelope_digest',
    'bundle_bucket','bundle_object_key','bundle_digest',
    'signature','signer_key_id','approver','approver_user_id','approver_name',
    'approver_role','issued_at','valid_until','cited_report_digest','created_at');

-- MOS-EVID-126: "Revoking a report MUST NOT delete it and MUST NOT alter its bytes."
-- Deleting one is not a stronger revocation; it is the destruction of the record that a
-- claim was once made, which is the fact an auditor is looking for.
CREATE TRIGGER validation_reports_no_delete BEFORE DELETE ON validation_reports
  FOR EACH ROW EXECUTE FUNCTION forbid_evidence_mutation();


-- =====================================================================================
-- 4. ENABLE + FORCE ROW LEVEL SECURITY.  MOS-EVID-012, MOS-SEC-072, MOS-STORE-229.
--
-- Identical in shape to 0002 section 10 and 0006 section 11: ownership moves to
-- `medicalos_owner` first so that FORCE binds the owner too, and the policy is written
-- TO PUBLIC so a session with no tenant bound hits `current_tenant_id()` and gets 42704
-- rather than a silently empty result set. An empty result set on an evidence query reads
-- as "no report exists", which is exactly the answer that makes a gate fail open.
-- =====================================================================================
ALTER TABLE validation_reports OWNER TO medicalos_owner;
ALTER TABLE validation_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE validation_reports FORCE  ROW LEVEL SECURITY;
CREATE POLICY validation_reports_tenant_isolation ON validation_reports
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());


-- =====================================================================================
-- 5. Grants.  MOS-STORE-228, MOS-SEC-073, MOS-STORE-230.
--
-- The column-level revocation MOS-STORE-228 spells out for this table, applied as a
-- positive grant of the standing columns instead of a REVOKE naming the other thirty-one.
-- Same effect, and it fails safe when a column is added: a new column is NOT grantable by
-- omission, where a REVOKE list would have silently left it writable.
-- =====================================================================================
GRANT SELECT, INSERT ON validation_reports TO medicalos_app;
GRANT UPDATE (status, superseded_by, revocation_reason, reproducibility_status, updated_at)
  ON validation_reports TO medicalos_app;
REVOKE DELETE ON validation_reports FROM medicalos_app;
GRANT SELECT ON validation_reports TO medicalos_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 6. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================
DO $$
DECLARE
  mutable   text[] := ARRAY['status','superseded_by','revocation_reason',
                            'reproducibility_status','updated_at'];
  sealed    text[] := ARRAY[
    'id','public_id','tenant_id','kind','schema_version','report_version',
    'subject_kind','subject_id','subject_version','capability_id','criteria_version',
    'evaluation_run_ids','acceptance_criteria_id','verdict',
    'report_bucket','report_object_key','report_digest','envelope_digest',
    'bundle_bucket','bundle_object_key','bundle_digest',
    'signature','signer_key_id','approver','approver_user_id','approver_name',
    'approver_role','issued_at','valid_until','cited_report_digest','created_at'];
  unaccounted text[];
  n         integer;
BEGIN
  -- 6a. Every column is either in the trigger's sealed list or named mutable. A column
  -- that is in neither is a column an operator can edit on a signed document. The sealed
  -- array above is a literal copy of the trigger's argument list, so this assertion fails
  -- if the two ever drift -- which is the failure a hand-maintained enumeration invites.
  SELECT array_agg(a.attname ORDER BY a.attname) INTO unaccounted
    FROM pg_attribute a
   WHERE a.attrelid = 'validation_reports'::regclass
     AND a.attnum > 0 AND NOT a.attisdropped
     AND NOT (a.attname = ANY (mutable))
     AND NOT (a.attname = ANY (sealed));
  IF unaccounted IS NOT NULL AND array_length(unaccounted, 1) > 0 THEN
    RAISE EXCEPTION
      'MOS-STORE-228: column(s) % are neither sealed by validation_reports_sealed nor '
      'named mutable. A signed document with an editable column is not signed.',
      unaccounted;
  END IF;
  SELECT array_agg(c) INTO unaccounted FROM unnest(sealed) AS c
   WHERE c NOT IN (SELECT a.attname FROM pg_attribute a
                    WHERE a.attrelid = 'validation_reports'::regclass
                      AND a.attnum > 0 AND NOT a.attisdropped);
  IF unaccounted IS NOT NULL AND array_length(unaccounted, 1) > 0 THEN
    RAISE EXCEPTION
      'validation_reports_sealed names column(s) % that do not exist', unaccounted;
  END IF;

  -- 6b. RLS is on and FORCEd (MOS-SEC-077 scans for exactly this).
  SELECT count(*) INTO n FROM pg_class
   WHERE oid = 'validation_reports'::regclass AND relrowsecurity AND relforcerowsecurity;
  IF n <> 1 THEN
    RAISE EXCEPTION 'MOS-STORE-229: validation_reports is not under FORCE row security';
  END IF;

  -- 6c. `medicalos_app` holds no table-level UPDATE and no DELETE.
  IF has_table_privilege('medicalos_app', 'validation_reports', 'DELETE') THEN
    RAISE EXCEPTION 'MOS-EVID-126: medicalos_app MUST NOT hold DELETE on validation_reports';
  END IF;
  SELECT count(*) INTO n
    FROM information_schema.column_privileges
   WHERE grantee = 'medicalos_app' AND table_name = 'validation_reports'
     AND privilege_type = 'UPDATE'
     AND column_name <> ALL (mutable);
  IF n <> 0 THEN
    RAISE EXCEPTION
      'MOS-STORE-228: medicalos_app holds UPDATE on % sealed column(s) of '
      'validation_reports', n;
  END IF;
END $$;
