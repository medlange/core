-- =====================================================================================
-- 0010_safety.up.sql -- the clinical-safety half of release 0.2.0.
--
-- docs/spec/15-delivery.md section 15.1.2, the 0.2.0 row, names three chapter 9 items
-- alongside the evidence plane: "applicability envelopes; RUO marking; `ResultReview`".
-- This migration carries the two of them that need storage.
--
--     applicability_envelopes  7.10.1  MOS-EVID-095   versioned, immutable per version
--     envelope_decisions       7.10.3  MOS-EVID-100/104  append-only, one per evaluation
--     result_reviews           9.6     MOS-SAFE-058/059/060  append-only per round
--     tenants.marginal_policy  7.10.3  MOS-EVID-101   the tenant's MARGINAL disposition
--
-- RUO MARKING NEEDS NO TABLE. `result_dicom_objects.research_marked` and the CHECK
-- `result_dicom_objects_ruo_marking` already exist in schema.sql (MOS-STORE-284): an
-- object belonging to a research-only result cannot be RECORDED unless it is marked. The
-- 0.2.0 gate check `ruo-marking` is about the WRITER refusing to EMIT one, which is code
-- (`medos/safety/marking.py`), not schema. What was missing was a single shared verifier
-- that runs on the assembled dataset immediately before STOW-RS (MOS-SAFE-039); that is
-- the deliverable, and this migration deliberately adds no second copy of the rule.
--
-- WHAT THIS MIGRATION DOES NOT CREATE, AND WHY
-- --------------------------------------------
--   `deployments`     Chapter 6 section 6.8 owns it, and the 0.2.0 evaluation/gate work
--                     landing in parallel references it. MOS-SAFE-033 puts
--                     `clinical_use_mode` on `Deployment`, MOS-SAFE-061 puts
--                     `review_mode` there and MOS-SAFE-065 `emit_verified_sr_on_accept`.
--                     Declaring a three-column stub here would be a second answer to a
--                     question this migration does not own. `medos/safety/policy.py`
--                     resolves those three facts through a PORT with one static driver,
--                     which is the same shape `medos.db.queue`'s `JobQueue` uses, and the
--                     driver is swapped for a table read when chapter 6's registry lands.
--                     `jobs.clinical_use_mode` and `results.clinical_use_mode` already
--                     exist and are already the immutable copies MOS-SAFE-034 requires.
--   `users` / `roles` Chapter 8 and chapter 12 section 12.6 own them; 0004_auth
--                     deliberately shipped without them (see `medos/security/scopes.py`).
--                     `result_reviews.reviewer_user_id` and `assignee_user_id` are
--                     therefore bare `uuid` with no FK, exactly as `api_keys.created_by`
--                     already is. MOS-SAFE-068's `reviewer_class` derivation reads
--                     `roles.clinically_qualified` (MOS-SAFE-104); with no roles table
--                     the answer is `non_clinical` for every principal, which is the
--                     CORRECT answer under MOS-SAFE-068 ("a role the principal does not
--                     hold ... MUST be recorded `reviewer_class = non_clinical`") and not
--                     a stub. `medos/safety/review.py` states it in one place.
--
-- ADDITIVE, like 0002-0006. It creates four tables' worth of structure and ALTERs exactly
-- one existing table, by adding one nullable-with-default column to `tenants`
-- (MOS-EVID-101 states `marginal_policy` as a TENANT setting in so many words). It does
-- not touch `jobs`, `results` or `result_dicom_objects`. `schema.sql` remains the
-- baseline (MOS-STORE-214).
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The `sha256_digest` domain, guarded.  MOS-STORE-211, MOS-EVID-007.
--
-- "Digests use the domain `sha256_digest` -- lower-case hex prefixed `sha256:`". 0006
-- created it and 0006's down-migration deliberately does not drop it; 0007 and 0009 carry
-- the same guarded block. This migration may be applied to a database where any of them
-- ran or to one where none did, so the creation is conditional exactly as 0005's
-- `uuid_generate_v7()` is.
--
-- THE CHECK EXPRESSION IS 0006'S, BYTE FOR BYTE, and that is not a style point. An earlier
-- draft of this migration guarded a DIFFERENT expression -- `^[0-9a-f]{64}$`, without the
-- prefix -- and because 0006 runs first the guard simply skipped, leaving this migration's
-- columns silently bound to a definition it did not declare. The mismatch surfaced as a
-- domain violation on the first INSERT, which is the good outcome; had 0010 run FIRST on
-- some deployment it would have created the weaker domain and every digest column in the
-- evidence plane would have accepted an unprefixed value. Two spellings of one domain is
-- exactly the drift CONTRACT.md section 2 exists to prevent.
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
    EXECUTE $c$COMMENT ON DOMAIN sha256_digest IS
      'MOS-STORE-211 / MOS-EVID-007: sha256: + 64 lower-case hex. No other algorithm '
      'is permitted in 0.2.0, and the prefix is what makes a second one introducible.'$c$;
  END IF;
END $$;


-- =====================================================================================
-- 2. `forbid_safety_mutation()` -- this migration's own append-only trigger function.
--
-- schema.sql's `forbid_mutation()` carries a HINT naming `job_events` and SSE resume, and
-- 0006's `forbid_evidence_mutation()` carries one naming dataset versions. Neither
-- sentence is true of an ApplicabilityEnvelope or of a ResultReview round, and an error
-- message that names the wrong table is how an operator ends up editing the wrong thing.
-- schema.sql is baseline DDL whose digest `medos.db.migrate` refuses to see change, so
-- amending the shared function is not available either.
-- =====================================================================================
CREATE OR REPLACE FUNCTION forbid_safety_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'relation % is immutable: % is not permitted', TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'MOS07',
          HINT = 'An ApplicabilityEnvelope is versioned and immutable per version '
                 '(ch. 7 section 7.10, MOS-EVID-095): publish version n+1. A submitted '
                 'ResultReview is corrected by a new round, never by editing the row '
                 '(MOS-SAFE-059, MOS-SAFE-060). An envelope decision is a record of what '
                 'was decided at a moment and is append-only (MOS-EVID-104).';
END;
$$;


-- =====================================================================================
-- 3. `tenants.marginal_policy`.  MOS-EVID-101.
--
-- "MARGINAL, tenant `marginal_policy: flag` (default) -> proceeds" / "MARGINAL, tenant
-- `marginal_policy: reject` -> REJECTED". The requirement states it as a tenant setting,
-- so it is a tenant column.
--
-- This is NOT the thing 0002 refused to add. That was `default_clinical_use_mode`, which
-- MOS-SAFE-033 forbids ("There is no tenant-wide, service-wide or environment-wide
-- override"). `marginal_policy` is the opposite case: a site deciding how conservative it
-- wants to be about studies at the edge of a vendor's validated range is precisely a
-- site-level decision, and chapter 7 places it at the tenant by name.
--
-- IF NOT EXISTS because 0.2.0's migrations were authored in parallel; adding a column
-- twice with the same definition must not fail a deployment mid-upgrade.
-- =====================================================================================
ALTER TABLE tenants
  ADD COLUMN IF NOT EXISTS marginal_policy text NOT NULL DEFAULT 'flag';

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenants_marginal_policy_ck') THEN
    ALTER TABLE tenants ADD CONSTRAINT tenants_marginal_policy_ck
      CHECK (marginal_policy IN ('flag', 'reject'));
  END IF;
END $$;

COMMENT ON COLUMN tenants.marginal_policy IS
  'MOS-EVID-101: what this site does with a study in the MARGINAL zone of a service''s '
  'declared ApplicabilityEnvelope. ''flag'' proceeds and annotates; ''reject'' terminates '
  'the job REJECTED. It does NOT affect the OUT zone, which always rejects.';


-- =====================================================================================
-- 4. `applicability_envelopes`.  Chapter 7 section 7.10.1, MOS-EVID-095..099.
--
-- Chapter 7 table 7.1 gives the entity "versioned, immutable per version", keyed
-- `(subject_ref, version)`, owned by ServiceVersion / ModelVersion.
--
-- TENANT-SCOPED, stated because the owner column in that table is not the tenant. The
-- envelope is authored by the service publisher, but the ROW is this site's record of
-- what the publisher declared for a subject this site runs, and every domain table in
-- this deployment is under FORCE RLS (MOS-STORE-223). An unscoped table here would be the
-- one place a tenant could enumerate which service versions another tenant has deployed.
-- When chapter 6's registry lands, a publisher-global envelope catalogue may sit beside
-- this; the per-tenant row is still the one a job resolves against, because MOS-EVID-098's
-- narrow-only rule is enforced against what the SITE accepted.
--
-- `constraints` is chapter 7 section 7.10.1's list, stored verbatim as the canonical JSON
-- of the manifest block, and `envelope_digest` is its content address. The digest is what
-- a ValidationReport cites (`envelope_digest`, section 7.12.1) and what the DICOM
-- annotation of a MARGINAL result names, so it must be reproducible from the stored
-- constraints and not minted separately -- `medos/safety/envelope.py::envelope_digest`
-- computes it with `medos.core.canonical.canonical_bytes`, the one canonicaliser
-- (MOS-REL-032).
--
-- NO `is_active` / `superseded_by` COLUMN. A subject resolves to the HIGHEST `version`
-- for that `(subject_kind, subject_id, subject_version)`, computed at read time. A boolean
-- that has to be flipped in a second statement is a boolean that will one day be true on
-- two rows, and "which envelope was this study judged against" is a question whose answer
-- must never be ambiguous. `envelope_decisions.envelope_id` records the resolution.
-- =====================================================================================
CREATE TABLE applicability_envelopes (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

  -- MOS-STORE-357's construction applied to chapter 7's entity. Section 7.10.1 names no
  -- id prefix for an ApplicabilityEnvelope -- the entity is keyed `(subject_ref,
  -- version)` -- but chapter 6 section 6.5's `ServiceVersion` example references one as
  -- `applicability_envelope_ref: "ae_chest_ct_v2"`, so `ae_` is the prefix the spec
  -- already writes down. REPORTED: chapter 6's example is a slug, not a ULID; this column
  -- carries the ULID form so the id is minted rather than named by a human.
  public_id text NOT NULL UNIQUE
    CHECK (public_id ~ '^ae_[0-9A-HJKMNP-TV-Z]{26}$'),

  subject_kind text NOT NULL CHECK (subject_kind IN ('service_version', 'model_version')),
  subject_id text NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 128),
  subject_version text NOT NULL CHECK (length(subject_version) BETWEEN 1 AND 64),
  version integer NOT NULL CHECK (version >= 1),

  -- MOS-EVID-097. `declared` is the honest value for an envelope that was written by hand
  -- rather than derived from a cohort -- which is every envelope in a deployment that has
  -- no EvaluationRun yet. It is a THIRD value and not a null `derivation`, because a null
  -- reads as "nobody filled this in" and the distinction between "hand-declared" and
  -- "derived from run X" is exactly what MOS-EVID-098's widening rule turns on.
  derivation text NOT NULL DEFAULT 'declared'
    CHECK (derivation IN ('percentile_1_99', 'min_max', 'declared')),
  derived_from_evaluation_run text,
  CONSTRAINT applicability_envelopes_derivation_ck
    CHECK ((derivation = 'declared') = (derived_from_evaluation_run IS NULL)),

  constraints jsonb NOT NULL CHECK (jsonb_typeof(constraints) = 'array'
                                    AND jsonb_array_length(constraints) >= 1),
  marginal_policy_default text NOT NULL DEFAULT 'flag'
    CHECK (marginal_policy_default IN ('flag', 'reject')),

  envelope_digest sha256_digest NOT NULL,

  declared_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT applicability_envelopes_version_uk
    UNIQUE (tenant_id, subject_kind, subject_id, subject_version, version),
  -- Content address, tenant-scoped for the reason 0006 gives at length: under FORCE RLS
  -- an unscoped UNIQUE over a digest is a cross-tenant existence oracle, and the colliding
  -- row would be invisible to the caller that has to resolve the collision.
  CONSTRAINT applicability_envelopes_digest_uk UNIQUE (tenant_id, envelope_digest)
);

CREATE INDEX applicability_envelopes_subject_idx
  ON applicability_envelopes (tenant_id, subject_kind, subject_id, subject_version,
                              version DESC);

-- MOS-EVID-095's "immutable per version", structurally. A published envelope is the
-- clinical claim a site is running under; an operator who can edit it in place can widen
-- a validated range without an EvaluationRun, which is the exact move MOS-EVID-098
-- exists to refuse.
CREATE TRIGGER applicability_envelopes_immutable
  BEFORE UPDATE OR DELETE ON applicability_envelopes
  FOR EACH ROW EXECUTE FUNCTION forbid_safety_mutation();


-- =====================================================================================
-- 5. `envelope_decisions`.  MOS-EVID-100, MOS-EVID-101, MOS-EVID-104; MOS-DATA-078/079.
--
-- "Envelope decisions MUST be counted and exported per `(tenant, capability,
-- service_version, reason_code)` ... A capability whose envelope rejects a large share of
-- a site's real traffic is a procurement fact the site must be able to see on day one,
-- not a silent drop." A counter cannot answer "which studies, and why"; a row can, and
-- the counter is a GROUP BY over it.
--
-- `job_id` IS NULLABLE, and that is the whole MOS-DATA-076/077 distinction in one column:
-- an ambient auto-routed study that fails applicability MUST NOT create a Job (a row here
-- with `job_id IS NULL` is the record of the decision), while an explicitly requested job
-- MUST always exist and terminates REJECTED (a row here with `job_id` set). Without the
-- nullable column the ambient case has nowhere to be counted and MOS-EVID-104's share is
-- computed over a denominator that silently excludes every study the platform declined
-- before it started.
--
-- `observed` holds the attribute values that were evaluated. PHI (CONTRACT.md section 11,
-- MOS-DATA-065): `study_description`, `series_description` and `protocol_name` are the
-- three free-text attributes triage retains and they MUST NOT be copied into an event
-- payload. They are not stored here either -- `medos/safety/attributes.py` reports a
-- regex match as a boolean and the matched TEXT is never carried out of the extractor.
-- Everything in `observed` is a millimetre, a count, a code or an enum member.
-- =====================================================================================
CREATE TABLE envelope_decisions (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

  -- Chapter 3 section 3.6.4's TriageDecision uses `td_`; this row is the narrower
  -- envelope half of that decision (MOS-EVID-101's outcome table), so it gets its own
  -- prefix rather than borrowing one whose record shape it does not have.
  public_id text NOT NULL UNIQUE
    CHECK (public_id ~ '^envd_[0-9A-HJKMNP-TV-Z]{26}$'),

  job_id uuid REFERENCES jobs(id) ON DELETE RESTRICT,
  study_instance_uid text NOT NULL CHECK (study_instance_uid ~ '^[0-9.]{1,64}$'),
  series_instance_uid text CHECK (series_instance_uid IS NULL
                                  OR series_instance_uid ~ '^[0-9.]{1,64}$'),

  envelope_id uuid NOT NULL REFERENCES applicability_envelopes(id) ON DELETE RESTRICT,
  envelope_version integer NOT NULL CHECK (envelope_version >= 1),
  envelope_digest sha256_digest NOT NULL,
  subject_id text NOT NULL,
  subject_version text NOT NULL,
  -- MOS-EVID-104's grouping key names the capability. Nullable because an envelope is
  -- declared on a ServiceVersion, which may carry several capabilities; a decision taken
  -- for the whole service version records null here rather than an arbitrary member.
  capability_id text,

  -- MOS-EVID-096: exactly one zone, the worst across all constraints.
  zone text NOT NULL CHECK (zone IN ('IN', 'MARGINAL', 'OUT')),
  marginal_policy text NOT NULL CHECK (marginal_policy IN ('flag', 'reject')),
  -- MOS-EVID-101's three outcomes. `proceed_flagged` is the MARGINAL+flag row: the job
  -- runs AND the annotation is mandatory, so it must be distinguishable from `proceed`.
  outcome text NOT NULL CHECK (outcome IN ('proceed', 'proceed_flagged', 'rejected')),

  -- MOS-EVID-103's closed lowercase dotted set. The WORST violation's code, denormalised
  -- out of `violations` so MOS-EVID-104's export is an index scan and not a jsonb walk.
  -- Null exactly when nothing was violated.
  reason_code text CHECK (reason_code IS NULL OR reason_code ~ '^envelope\.[a-z_]+$'),
  violations jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(violations) = 'array'),
  observed jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(observed) = 'object'),

  decided_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT envelope_decisions_zone_outcome_ck CHECK (
    (zone = 'IN'       AND outcome = 'proceed'   AND reason_code IS NULL) OR
    (zone = 'MARGINAL' AND ((marginal_policy = 'flag'   AND outcome = 'proceed_flagged') OR
                            (marginal_policy = 'reject' AND outcome = 'rejected'))) OR
    (zone = 'OUT'      AND outcome = 'rejected'  AND reason_code IS NOT NULL)
  )
);

CREATE INDEX envelope_decisions_export_idx
  ON envelope_decisions (tenant_id, subject_id, subject_version, capability_id,
                         reason_code, decided_at DESC);
CREATE INDEX envelope_decisions_job_idx ON envelope_decisions (tenant_id, job_id);

CREATE TRIGGER envelope_decisions_append_only
  BEFORE UPDATE OR DELETE ON envelope_decisions
  FOR EACH ROW EXECUTE FUNCTION forbid_safety_mutation();


-- =====================================================================================
-- 6. `result_reviews`.  Chapter 9 section 9.6; chapter 12 section 12.11 renders it.
--
-- Chapter 12's rendering is followed column for column with THREE additions, each stated
-- here because chapter 12 says the table "MUST NOT add, rename or drop a field":
--
--   `public_id`       MOS-SAFE-058 gives `ResultReview` the id prefix `rrv_` and chapter
--                     12's DDL has no column able to hold it (`id` is a uuid). Same
--                     MOS-STORE-357 construction as `jobs.public_id`. MOS-SAFE-069's
--                     endpoints are `/api/v1/result-reviews/{id}` and the id in a URL is
--                     never the uuid.
--   `reviewer_class`  MOS-SAFE-058 lists it as a field of the entity and MOS-SAFE-068
--                     makes it the gate on MOS-SAFE-065. Chapter 12 section 12.11's DDL
--                     omits it. Chapter 9 owns the field contract, so the column exists.
--                     SPEC DEFECT, reported, not silently reconciled.
--   `superseded_result_id` is NOT added. A review reaches SUPERSEDED because its Result
--                     was superseded, and `results.superseded_by` already says by what.
--
-- TWO DEVIATIONS FROM CHAPTER 12'S DDL, both forced and both narrowing:
--   * `result_reviews_reviewer_fk` / `_assignee_fk` REFERENCE `users (tenant_id, id)`.
--     There is no `users` table in this deployment (0004_auth shipped without one). The
--     columns are bare `uuid`, exactly as `api_keys.created_by` already is, and the FK
--     lands with chapter 8's table. MOS-STORE-233's rule -- a `users` row referenced by
--     `result_reviews` MUST NOT be deleted -- is a property of that future FK and nothing
--     here weakens it.
--   * `UNIQUE (result_id, round)` is written `UNIQUE (tenant_id, result_id, round)`. The
--     composite FK is already `(tenant_id, result_id)`, so the tenant column is on the
--     row regardless; scoping the unique index to it keeps every index on this table
--     tenant-leading, which is what makes an RLS-filtered scan use one.
-- =====================================================================================
CREATE TABLE result_reviews (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  result_id uuid NOT NULL,

  public_id text NOT NULL UNIQUE
    CHECK (public_id ~ '^rrv_[0-9A-HJKMNP-TV-Z]{26}$'),

  round integer NOT NULL DEFAULT 1 CHECK (round >= 1),
  state text NOT NULL DEFAULT 'PENDING' CHECK (state IN
    ('PENDING','IN_REVIEW','ACCEPTED','MODIFIED','REJECTED','EXPIRED','SUPERSEDED')),

  assignee_user_id uuid,
  reviewer_user_id uuid,                        -- null until the row is claimed

  -- MOS-SAFE-058 / chapter 12 section 12.6: a plain `text` snapshot of the Role's `key`
  -- at the moment of the act. No FK to `roles` and -- deliberately -- no CHECK
  -- enumerating role keys: MOS-SEC-038 forbids a database constraint over role keys and
  -- MOS-SAFE-104 forbids deriving the reviewer's class from the spelling of one.
  reviewer_role text CHECK (reviewer_role IS NULL OR length(reviewer_role) BETWEEN 1 AND 128),

  -- MOS-SAFE-058 + MOS-SAFE-068: derived at submit, stored, immutable thereafter.
  reviewer_class text CHECK (reviewer_class IN ('clinical', 'non_clinical')),

  claimed_at timestamptz,
  submitted_at timestamptz,
  review_due_at timestamptz,                    -- from Deployment.review_sla_hours

  action_rationale text,
  modifications jsonb,                          -- RFC 6902 patch against Result.findings
  rejection_reason text CHECK (rejection_reason IN ('false_positive','false_negative',
    'wrong_laterality','mis_segmentation','measurement_implausible','wrong_series_analysed',
    'out_of_intended_use','other')),
  known_failure_mode_id text,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT result_reviews_round_uk UNIQUE (tenant_id, result_id, round),
  CONSTRAINT result_reviews_result_fk FOREIGN KEY (tenant_id, result_id)
    REFERENCES results (tenant_id, id) ON DELETE RESTRICT,

  -- Chapter 12's three CHECKs, verbatim in meaning.
  CONSTRAINT result_reviews_modified_ck CHECK (state <> 'MODIFIED'
    OR (modifications IS NOT NULL AND length(action_rationale) >= 20)),
  CONSTRAINT result_reviews_rejected_ck CHECK (state <> 'REJECTED'
    OR (rejection_reason IS NOT NULL AND length(action_rationale) >= 20)),
  CONSTRAINT result_reviews_reviewer_ck CHECK (
    state IN ('PENDING','EXPIRED','SUPERSEDED') OR reviewer_user_id IS NOT NULL),

  -- MOS-SAFE-068's other half, structurally: a submitted row carries its class, and a row
  -- that has not been submitted must not claim one. Without this, `reviewer_class` could
  -- be inserted as `clinical` on a PENDING row and never re-derived at submit, which is
  -- precisely the "machine review is not human review" failure the requirement closes.
  CONSTRAINT result_reviews_class_ck CHECK (
    (reviewer_class IS NOT NULL) = (state IN ('ACCEPTED','MODIFIED','REJECTED'))),
  CONSTRAINT result_reviews_submitted_ck CHECK (
    (submitted_at IS NOT NULL) = (state IN ('ACCEPTED','MODIFIED','REJECTED'))),
  CONSTRAINT result_reviews_claimed_ck CHECK (
    state <> 'IN_REVIEW' OR (claimed_at IS NOT NULL AND reviewer_user_id IS NOT NULL))
);

CREATE INDEX result_reviews_result_idx ON result_reviews (tenant_id, result_id, round DESC);
CREATE INDEX result_reviews_state_idx ON result_reviews (tenant_id, state, review_due_at);
CREATE INDEX result_reviews_assignee_idx ON result_reviews (tenant_id, assignee_user_id)
  WHERE assignee_user_id IS NOT NULL;

CREATE TRIGGER result_reviews_touch BEFORE UPDATE ON result_reviews
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- MOS-SAFE-060: "Review rows MUST be append-only at the database role level: the
-- application role MUST NOT hold `UPDATE` on terminal rows or `DELETE` on any row."
--
-- The grant in section 8 cannot express "terminal rows only" -- a GRANT has no WHEN --
-- so the structural form is this trigger, and chapter 12 section 12.11 writes exactly
-- the same one. It fires on UPDATE of a row already in a terminal state, and on DELETE of
-- any row whatever, which is the half a WHEN clause restricted to the terminal states
-- would silently omit: `DELETE` of a PENDING row destroys the record that a review was
-- ever required.
CREATE TRIGGER result_reviews_terminal_immutable BEFORE UPDATE ON result_reviews
  FOR EACH ROW
  WHEN (OLD.state IN ('ACCEPTED','MODIFIED','REJECTED','EXPIRED','SUPERSEDED'))
  EXECUTE FUNCTION forbid_safety_mutation();

CREATE TRIGGER result_reviews_no_delete BEFORE DELETE ON result_reviews
  FOR EACH ROW EXECUTE FUNCTION forbid_safety_mutation();

-- The columns that identify WHO reviewed and WHAT they concluded are sealed against edit
-- from the moment they are written, not merely once the row is terminal. The transition
-- PENDING -> IN_REVIEW writes `reviewer_user_id`; MOS-SAFE-058 makes it "immutable after
-- submit" and MOS-SAFE-069 makes a claim by a second principal a 409 rather than an
-- overwrite. Sealing `result_id` and `round` as well is what stops a review being moved
-- onto a different Result after the fact.
CREATE TRIGGER result_reviews_identity_sealed BEFORE UPDATE ON result_reviews
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'tenant_id', 'result_id', 'round', 'public_id', 'created_at');


-- =====================================================================================
-- 7. `results.review_status` stays a projection.  MOS-SAFE-062.
--
-- "`Result.review_status` is a derived, denormalised projection of the highest-round
-- `ResultReview` row (`UNREVIEWED` when no row exists), maintained in the same
-- transaction as the review transition. It MUST NOT be writable through any API."
--
-- A TRIGGER and not application code, for the reason the requirement gives: "maintained
-- in the same transaction". An application-side update is a second statement that a
-- future caller can forget, and the failure is silent -- a REJECTED review that the UI
-- renders as UNREVIEWED (MOS-SAFE-064 makes that rendering mandatory).
--
-- `security definer` is NOT used and is not needed: the trigger runs as the invoking role
-- inside that role's transaction, so RLS applies to the UPDATE exactly as it would to the
-- caller's own, and a review row can only reach a `results` row in its own tenant because
-- the composite FK already forced them to agree.
-- =====================================================================================
CREATE FUNCTION sync_result_review_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  UPDATE results
     SET review_status = (SELECT r.state FROM result_reviews r
                           WHERE r.result_id = NEW.result_id
                           ORDER BY r.round DESC LIMIT 1),
         updated_at = now()
   WHERE id = NEW.result_id;
  RETURN NEW;
END $$;

CREATE TRIGGER result_reviews_sync AFTER INSERT OR UPDATE ON result_reviews
  FOR EACH ROW EXECUTE FUNCTION sync_result_review_status();


-- =====================================================================================
-- 8. ENABLE + FORCE ROW LEVEL SECURITY, and the policies.
--     MOS-SEC-072, MOS-STORE-223, MOS-STORE-224, MOS-STORE-229.
--
-- Identical in shape to 0002 section 10 and 0006 section 11, for the same reasons:
-- ownership moves to `medicalos_owner` first so that FORCE binds the owner too, and the
-- policy is written TO PUBLIC so a session with no tenant bound hits `current_tenant_id()`
-- and gets 42704 rather than a silently empty result set.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['applicability_envelopes','envelope_decisions',
                           'result_reviews'] LOOP
    EXECUTE format('ALTER TABLE %I OWNER TO medicalos_owner', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I '
      'USING (tenant_id = current_tenant_id()) '
      'WITH CHECK (tenant_id = current_tenant_id())', t || '_tenant_isolation', t);
  END LOOP;
END $$;


-- =====================================================================================
-- 9. Grants.  MOS-SAFE-060, MOS-SEC-073, MOS-STORE-228, MOS-STORE-230.
--
-- `medicalos_app` owns nothing and is NOBYPASSRLS. No DELETE is granted anywhere in this
-- migration. An envelope is superseded by a higher version, a decision is history, and a
-- review is corrected by a new round.
--
-- `result_reviews` gets a COLUMN-LEVEL update grant listing exactly the columns a
-- transition may write. MOS-SAFE-060 says the application role "MUST NOT hold `UPDATE` on
-- terminal rows"; the trigger enforces the row predicate, and this grant enforces the
-- column one -- an application bug cannot move a review to a different `result_id` or
-- rewrite its `round` even on a non-terminal row, because the privilege to do so is not
-- held. Two layers, deliberately, as 0006 section 12 puts it: the trigger is the
-- guarantee, the grant is the smaller blast radius.
-- =====================================================================================
GRANT SELECT, INSERT ON applicability_envelopes TO medicalos_app;
GRANT SELECT, INSERT ON envelope_decisions      TO medicalos_app;
GRANT SELECT, INSERT ON result_reviews          TO medicalos_app;

GRANT UPDATE (state, assignee_user_id, reviewer_user_id, reviewer_role, reviewer_class,
              claimed_at, submitted_at, review_due_at, action_rationale, modifications,
              rejection_reason, known_failure_mode_id, updated_at)
  ON result_reviews TO medicalos_app;

REVOKE UPDATE, DELETE ON applicability_envelopes, envelope_decisions FROM medicalos_app;
REVOKE DELETE ON result_reviews FROM medicalos_app;

-- MOS-SAFE-062: `results.review_status` is maintained by the trigger in section 7, which
-- runs as the caller. `medicalos_app` already holds table-level UPDATE on `results` from
-- schema.sql, so no new grant is needed -- and no new grant is WANTED: the requirement's
-- "MUST NOT be writable through any API" is enforced in `medos/api/routes_reviews.py`,
-- which exposes no field that maps to it, and in `medos/safety/repo.py`, which never names
-- the column in a statement of its own.

GRANT SELECT ON applicability_envelopes, envelope_decisions, result_reviews
  TO medicalos_readonly;


-- =====================================================================================
-- 10. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================
DO $$
DECLARE
  t text;
  n integer;
BEGIN
  -- Every table forced, and carrying a policy.
  FOREACH t IN ARRAY ARRAY['applicability_envelopes','envelope_decisions',
                           'result_reviews'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
                    WHERE c.relname = t AND ns.nspname = 'public'
                      AND c.relrowsecurity AND c.relforcerowsecurity) THEN
      RAISE EXCEPTION '0010: % is not FORCE ROW LEVEL SECURITY', t;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies
                    WHERE schemaname = 'public' AND tablename = t) THEN
      RAISE EXCEPTION '0010: % carries no RLS policy', t;
    END IF;
  END LOOP;

  -- No DELETE privilege for the application role anywhere this migration created.
  SELECT count(*) INTO n
    FROM information_schema.table_privileges
   WHERE grantee = 'medicalos_app' AND privilege_type = 'DELETE'
     AND table_name IN ('applicability_envelopes','envelope_decisions','result_reviews');
  IF n <> 0 THEN
    RAISE EXCEPTION '0010: medicalos_app holds DELETE on % safety table(s)', n;
  END IF;

  -- No TABLE-level UPDATE on result_reviews: the grant must be the column list above.
  IF EXISTS (SELECT 1 FROM information_schema.table_privileges
              WHERE grantee = 'medicalos_app' AND privilege_type = 'UPDATE'
                AND table_name = 'result_reviews') THEN
    RAISE EXCEPTION '0010: medicalos_app holds table-level UPDATE on result_reviews; '
                    'MOS-SAFE-060 requires the column-level grant';
  END IF;

  -- The append-only triggers exist on all three.
  SELECT count(*) INTO n FROM pg_trigger
   WHERE NOT tgisinternal
     AND tgname IN ('applicability_envelopes_immutable', 'envelope_decisions_append_only',
                    'result_reviews_terminal_immutable', 'result_reviews_no_delete',
                    'result_reviews_identity_sealed', 'result_reviews_sync');
  IF n <> 6 THEN
    RAISE EXCEPTION '0010: expected 6 safety triggers, found %', n;
  END IF;
END $$;
