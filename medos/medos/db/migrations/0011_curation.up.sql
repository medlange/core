-- =====================================================================================
-- 0011_curation.up.sql -- the training corpus: permission, sampling, queue, decisions.
--
-- docs/spec/15-delivery.md section 15.2.5 (weeks 6-9, tag 0.2.0) and chapter 17
-- `MOS-TRAIN-190`, which places "`CurationBatch`, exclusion vocabulary,
-- `patient_key_aliases`" in THIS release, beside the evidence plane rather than after it.
-- Chapter 12 section 12.12.1 (`MOS-STORE-358`, `MOS-STORE-359`) is normative for the
-- physical form of all six tables; chapter 17 is normative for every field name, value
-- set and rule (`MOS-STORE-201`).
--
-- SCOPE -- the six tables of section 12.12.1, and nothing else:
--
--     training_data_policies       MOS-TRAIN-073   the legal basis, one live per tenant
--     sampling_plans               MOS-TRAIN-083   declared BEFORE any candidate is drawn
--     harvest_batches              MOS-TRAIN-078   the CurationBatch
--     harvest_candidates           MOS-TRAIN-079   UIDs, hashes and keys only
--     curation_decisions           MOS-TRAIN-080   a named human, append-only
--     corpus_stratification_reports MOS-TRAIN-088  C1-C7, recorded on the batch
--
-- `tenants.training_use_allowed` is NOT created here: 0002 section 149 already declares
-- it with exactly `MOS-TRAIN-072`'s column, type and default. What is added here is the
-- half of `MOS-TRAIN-073` that makes the flag mean something -- the deferred constraint
-- trigger that refuses it without a live instrument, and the revocation trigger that
-- writes it back to false in the same transaction (`MOS-TRAIN-075`, `MOS-TRAIN-076`).
--
-- NOT in this migration, on purpose: `patient_key_aliases` (`MOS-TRAIN-116`) -- the seal
-- in `medos/evidence/repo.py` refuses an identity defect and names the alias table as the
-- remedy, so the table belongs with whichever migration teaches the seal to APPLY it
-- (step 2 of `MOS-TRAIN-208`); a stub here would be a second answer. `training_runs` and
-- everything downstream of it are 0.3.0 (`MOS-TRAIN-190`).
--
-- ADDITIVE, like 0002-0006: it creates tables and functions and alters no existing table.
-- The one trigger it attaches to an existing table (`tenants`) adds a constraint that
-- 0002's own comment on `training_use_allowed` already anticipates.
--
-- FOUR DELIBERATE DIVERGENCES FROM CHAPTER 12 SECTION 12.12.1, each at its point of use:
--   1. `capability_id text`, not `uuid REFERENCES capabilities(id)` -- section 3.
--   2. no `users` foreign keys on `recorded_by` / `opened_by` / `decided_by` -- section 2.
--   3. `curation_decisions` carries the append-only trigger `MOS-STORE-359` requires and
--      section 12.12.1's DDL listing omits -- section 6.
--   4. `harvest_candidates` has no DELETE grant and its decisions cannot be deleted, so
--      `MOS-TRAIN-203`'s "deleting an excluded row MUST be refused" is structural rather
--      than procedural -- section 6.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The `sha256_digest` domain.  MOS-STORE-211.
--
-- Guarded, on the 0005/0006 precedent: several migrations in this release block were
-- authored in parallel and any of them may have created it. A domain with no state is
-- free to leave in place, and this migration's down script does not drop it.
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
-- 1b. forbid_curation_mutation() -- the append-only trigger for this migration's tables.
--
-- `forbid_mutation()` from `schema.sql` and `forbid_evidence_mutation()` from 0006 reach
-- the same invariant, and both carry a HINT that names the wrong subsystem when it fires
-- here: the first points a developer at `job_events` and SSE resume, the second at
-- versioning a sealed cohort. Neither is the remedy for "you tried to delete an
-- exclusion". Both are left alone -- `schema.sql` is baseline DDL whose digest
-- `medos.db.migrate` refuses to see change, and 0006 is applied -- so this is a third,
-- curation-specific one, on 0006's own precedent.
--
-- The trigger is the layer a GRANT cannot reach: it binds `medicalos_owner`,
-- `medicalos_migrator` through its membership, and a superuser psql session. That is the
-- case in which a cohort quietly becomes optimistic.
-- =====================================================================================
CREATE FUNCTION forbid_curation_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'relation % is an append-only curation record: % is not permitted',
        TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'MOS05',
          HINT = 'MOS-TRAIN-203: every excluded candidate is retained with its reason '
                 'code, and deleting an excluded row MUST be refused. MOS-STORE-359: an '
                 'editable curation decision or stratification report is not a record. '
                 'Correct a decision by appending another; a plan is corrected by a new '
                 'plan_version.';
END;
$$;
ALTER FUNCTION forbid_curation_mutation() OWNER TO medicalos_owner;


-- =====================================================================================
-- 2. training_data_policies -- chapter 17 `TrainingDataPolicy`, MOS-TRAIN-073.
--
-- MOS-TRAIN-074 fixes the platform's posture toward the two columns that matter: "The
-- platform MUST NOT interpret, validate against a registry, or assess the sufficiency of
-- `legal_basis` or `basis_reference`. It MUST store them, digest them, display them,
-- refuse the harvest without them". So `legal_basis` carries the closed enum and
-- `basis_reference` carries a non-empty CHECK, and there is no third constraint on
-- either: a platform that validated an approval number would have quietly assumed the
-- assertion, which is the site's to make.
--
-- DIVERGENCE 2. Section 12.12.1 writes
--     CONSTRAINT training_data_policies_recorder_fk FOREIGN KEY (tenant_id, recorded_by)
--       REFERENCES users (tenant_id, id)
-- and there is no `users` table in this deployment -- chapter 8's principal catalogue is
-- not built (CONTRACT.md section 0; 0004 created `api_keys` and stopped there). The
-- column keeps its name, its type and its NOT NULL; only the reference is absent, and it
-- is absent in exactly the way `datasets.created_by` is absent in 0006. The same applies
-- to `harvest_batches.opened_by` and `curation_decisions.decided_by`. When `users` lands,
-- the three FKs are a one-line ALTER each and this comment is the record of what is owed.
-- =====================================================================================
CREATE TABLE training_data_policies (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

  -- MOS-TRAIN-073's closed set, verbatim and in its order. A value outside it is not a
  -- basis the platform has an opinion about; it is a basis nobody declared.
  legal_basis text NOT NULL CHECK (legal_basis IN ('broad_consent',
    'research_ethics_approval','public_corpus_licence','data_processing_agreement',
    'national_derogation')),
  basis_reference text NOT NULL CHECK (length(basis_reference) > 0),
  basis_document_digest sha256_digest,

  -- MOS-TRAIN-073's `scope` object. All five keys present, because MOS-TRAIN-075 refuses
  -- a harvest "for any study outside `scope`" and an absent key is not an open bound --
  -- it is a question nobody answered. `date_to: null` is how "open-ended" is written.
  scope jsonb NOT NULL
    CHECK (scope ?& array['modalities','body_parts','capabilities','date_from','date_to']),

  permits_redistribution boolean NOT NULL DEFAULT false,
  recorded_by uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  revoked_at timestamptz,
  revocation_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT training_data_policies_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT training_data_policies_revocation_ck
    CHECK ((revoked_at IS NULL) = (revocation_reason IS NULL))
);

-- MOS-TRAIN-073 writes the key as `tenant_id PRIMARY KEY`. Under MOS-STORE-208 that is
-- rendered as a surrogate key plus this partial unique index: one live instrument per
-- tenant, and a withdrawn one stays readable rather than being overwritten, which is what
-- MOS-TRAIN-076 needs in order to say which candidates predate a revocation.
CREATE UNIQUE INDEX training_data_policies_live_uk ON training_data_policies (tenant_id)
  WHERE revoked_at IS NULL;

CREATE TRIGGER training_data_policies_touch BEFORE UPDATE ON training_data_policies
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- The instrument is the record of an assertion; its terms are not editable after the
-- fact. Withdrawal is `revoked_at`, correction is a new row (the live index permits it
-- once the old one is revoked), and expiry is `expires_at`. Nothing else moves.
CREATE TRIGGER training_data_policies_sealed BEFORE UPDATE ON training_data_policies
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('id','tenant_id','legal_basis',
    'basis_reference','basis_document_digest','scope','permits_redistribution',
    'recorded_by','recorded_at','created_at');

COMMENT ON TABLE training_data_policies IS
  'MOS-TRAIN-073: the instrument behind tenants.training_use_allowed. MOS-TRAIN-074: '
  'the platform stores, digests, displays and refuses-without; it never assesses.';


-- -------------------------------------------------------------------------------------
-- MOS-TRAIN-072 and MOS-TRAIN-073 are one fact held in two tables, so the flag and the
-- instrument are bound in BOTH directions. Section 12.12.1 creates both triggers here
-- rather than in 0002, because `training_data_policies` does not exist until now.
-- -------------------------------------------------------------------------------------
CREATE FUNCTION assert_training_policy_live() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.training_use_allowed AND NOT EXISTS (
       SELECT 1 FROM training_data_policies p
        WHERE p.tenant_id = NEW.id
          AND p.revoked_at IS NULL
          AND (p.expires_at IS NULL OR p.expires_at > now())) THEN
    RAISE EXCEPTION
      'training_use_allowed requires a live TrainingDataPolicy (MOS-TRAIN-073)'
      USING ERRCODE = '23514',
            HINT = 'Record the legal_basis and basis_reference first, in the same '
                   'transaction. MOS-TRAIN-072: there is no per-study, per-user or '
                   'per-environment override and no platform-administrator bypass.';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION assert_training_policy_live() OWNER TO medicalos_owner;

-- DEFERRED, so the flag and its instrument may be written in one transaction. A CHECK
-- cannot see another table and an immediate trigger would force the two writes into an
-- order, which is the kind of constraint operators route around by leaving the flag on.
CREATE CONSTRAINT TRIGGER tenants_training_policy_required
  AFTER INSERT OR UPDATE OF training_use_allowed ON tenants
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_training_policy_live();

-- MOS-TRAIN-076: revocation "MUST block every new harvest and MUST be recorded on the
-- tenant". Same transaction, so there is no window in which the flag says yes and the
-- instrument says no.
CREATE FUNCTION clear_training_use_on_revocation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.revoked_at IS NOT NULL AND OLD.revoked_at IS NULL THEN
    UPDATE tenants SET training_use_allowed = false, updated_at = now()
     WHERE id = NEW.tenant_id AND training_use_allowed;
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION clear_training_use_on_revocation() OWNER TO medicalos_owner;

CREATE TRIGGER training_data_policies_revocation
  AFTER UPDATE OF revoked_at ON training_data_policies
  FOR EACH ROW EXECUTE FUNCTION clear_training_use_on_revocation();


-- =====================================================================================
-- 3. sampling_plans -- chapter 17 `SamplingPlan`, MOS-TRAIN-083.
--
-- DIVERGENCE 1. Section 12.12.1 writes `capability_id uuid NOT NULL REFERENCES
-- capabilities(id)`. There is no `capabilities` table: chapter 6's registry is the other
-- half of 0.2.0 and has not landed, and 0006 already made this call for
-- `annotation_sets.capability_id`, where the column is the `medos.capabilities.REGISTRY`
-- key -- the same key `results.capability_id` has carried since weeks 1-2. One spelling
-- of a capability identity in this deployment, not two. When the registry lands, the
-- migration that introduces it owns the conversion.
-- =====================================================================================
CREATE TABLE sampling_plans (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id text NOT NULL CHECK (capability_id <> ''),
  plan_version integer NOT NULL CHECK (plan_version >= 1),

  -- One key per stratification dimension. MOS-TRAIN-083 fixes the minimum four and owns
  -- each dimension's sampling rule, which is stored under its key verbatim.
  strata jsonb NOT NULL CHECK (strata ?& array['score_band','review_outcome',
                                               'ran_on_platform','acquisition_bucket']),
  min_naive_fraction numeric(4,3) NOT NULL DEFAULT 0.200
    CHECK (min_naive_fraction >= 0 AND min_naive_fraction <= 1),
  de_novo_control_fraction numeric(4,3) NOT NULL DEFAULT 0.100
    CHECK (de_novo_control_fraction >= 0.100),            -- MOS-TRAIN-101
  spec_digest sha256_digest NOT NULL,
  sealed_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT sampling_plans_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT sampling_plans_uk UNIQUE (tenant_id, capability_id, plan_version)
);

-- MOS-STORE-359: "`sampling_plans` is sealed outright, because a plan edited after the
-- draw cannot describe the draw." A new plan is a new `plan_version` row.
CREATE TRIGGER sampling_plans_sealed BEFORE UPDATE ON sampling_plans
  FOR EACH ROW EXECUTE FUNCTION forbid_curation_mutation();

COMMENT ON TABLE sampling_plans IS
  'MOS-TRAIN-083: declared BEFORE any candidate is drawn. Sampling MUST NOT be '
  '"everything the service ran on" and MUST NOT be a random sample of it either.';


-- =====================================================================================
-- 4. harvest_batches -- chapter 17 `HarvestBatch` / `CurationBatch` (MOS-TRAIN-202).
--
-- `sampling_plan_id` is NOT NULL at insert. MOS-TRAIN-083 requires the plan to be
-- declared before any candidate is drawn, and once candidates are rows that sentence has
-- exactly one physical rendering: a nullable column would make it a convention.
--
-- `training_data_policy_id` is NOT NULL for the same reason one level up. The batch
-- names the instrument it was drawn under, so MOS-TRAIN-076 can say which candidates
-- predate a revocation without reconstructing the answer from timestamps.
-- =====================================================================================
CREATE TABLE harvest_batches (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id text NOT NULL CHECK (capability_id <> ''),
  sampling_plan_id uuid NOT NULL,
  training_data_policy_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'OPEN'
    CHECK (state IN ('OPEN','SAMPLED','CURATING','SEALED','ABANDONED')),
  candidate_count integer NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
  dataset_version_id uuid,                     -- set when MOS-TRAIN-082 seals from it
  opened_by uuid NOT NULL,
  opened_at timestamptz NOT NULL DEFAULT now(),
  sealed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT harvest_batches_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT harvest_batches_plan_fk FOREIGN KEY (tenant_id, sampling_plan_id)
    REFERENCES sampling_plans (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT harvest_batches_policy_fk FOREIGN KEY (tenant_id, training_data_policy_id)
    REFERENCES training_data_policies (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT harvest_batches_dataset_version_fk
    FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT harvest_batches_sealed_at_ck CHECK ((state = 'SEALED') = (sealed_at IS NOT NULL)),
  CONSTRAINT harvest_batches_sealed_version_ck
    CHECK (state <> 'SEALED' OR dataset_version_id IS NOT NULL)
);

CREATE TRIGGER harvest_batches_touch BEFORE UPDATE ON harvest_batches
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- MOS-STORE-359: the batch stays mutable -- it advances through its states -- but seals
-- its identity columns. A batch whose plan could be swapped after the draw is a batch
-- with no plan.
CREATE TRIGGER harvest_batches_identity_sealed BEFORE UPDATE ON harvest_batches
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','capability_id',
    'sampling_plan_id','training_data_policy_id','opened_by','opened_at');

COMMENT ON TABLE harvest_batches IS
  'MOS-TRAIN-202: the CurationBatch. A DatasetVersion MUST NOT be sealed from a query, '
  'a folder, a DICOMweb filter or a view (MOS-EVID-016); this is the materialised list.';


-- =====================================================================================
-- 5. harvest_candidates -- chapter 17 `HarvestCandidate`, MOS-TRAIN-079.
--
-- THE PHI RULE IS THE POINT OF THIS TABLE. MOS-TRAIN-079: a candidate "MUST NOT carry
-- `PatientName`, `PatientBirthDate`, `AccessionNumber`, institution free text, or any
-- source-space UID", and MOS-STORE-358 extends MOS-STORE-272's schema-review rule and CI
-- grep to it. Every column below is a de-identified UID, an HMAC key, a hash, a number
-- or a closed-vocabulary label. `institution_key` is MOS-TRAIN-089's
-- HMAC-SHA256(tenant_salt, InstitutionName) truncated to 10 bytes and base32-encoded --
-- never a name, here or in a split manifest or in a report (MOS-EVID-116).
--
-- Section 13 asserts the absence, so that adding a `patient_name` column to this table
-- fails the migration rather than passing review.
-- =====================================================================================
CREATE TABLE harvest_candidates (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  harvest_batch_id uuid NOT NULL,

  patient_key text NOT NULL CHECK (patient_key <> ''),   -- MOS-EVID-010
  study_instance_uid dicom_uid NOT NULL,                 -- de-identified UID space only
  series_instance_uids dicom_uid[] NOT NULL
    CHECK (cardinality(series_instance_uids) >= 1),
  instance_uids dicom_uid[] NOT NULL,

  geometry jsonb NOT NULL,                     -- the MOS-IMG-036 descriptor

  -- MOS-TRAIN-084's thirteen fields, copied from the source header WITHOUT imputation: a
  -- missing value is serialised as `null` and never as a default (MOS-EVID-020). Recorded
  -- at curation time and not at sealing time, because stratification has to be computable
  -- BEFORE the cohort is chosen.
  acquisition_profile jsonb NOT NULL
    CHECK (acquisition_profile ?& array['manufacturer','manufacturer_model_name',
      'convolution_kernel','convolution_kernel_class','slice_thickness_mm',
      'pixel_spacing_mm','kvp','exposure_mas','contrast_phase',
      'iterative_recon_strength','station_key','institution_key','study_year']),

  institution_key text NOT NULL CHECK (institution_key <> ''),   -- MOS-TRAIN-089
  deid_policy_version integer NOT NULL,                          -- MOS-TRAIN-069
  ran_on_platform boolean NOT NULL,

  -- The terminal `jobs.state` values (MOS-STORE-266) plus `NOT_APPLICABLE` from
  -- `triage_decision.outcome` (MOS-DATA-078). No value is invented here.
  platform_outcome text CHECK (platform_outcome IN
    ('COMPLETED','FAILED','CANCELLED','REJECTED','NOT_APPLICABLE')),
  review_outcome text CHECK (review_outcome IN ('UNREVIEWED','PENDING','IN_REVIEW',
    'ACCEPTED','MODIFIED','REJECTED','EXPIRED','SUPERSEDED')),

  score_band text,
  acquisition_bucket text,
  sampling_weight numeric(9,6) NOT NULL CHECK (sampling_weight > 0),   -- MOS-TRAIN-083

  -- MOS-TRAIN-087. A candidate MAY be generation 2 and be excluded FOR it; a sealed
  -- `dataset_cases` row may not be generation 2 at all. Deliberately different CHECKs,
  -- and this is the permissive half.
  corpus_generation smallint NOT NULL DEFAULT 0
    CHECK (corpus_generation BETWEEN 0 AND 2),

  -- MOS-TRAIN-080 permits auto-EXCLUSION on a mechanical predicate and forbids
  -- auto-inclusion. The predicate id and its digest are recorded so the exclusion is
  -- reproducible; the exclusion itself is still a `curation_decisions` row, because
  -- MOS-TRAIN-080 requires it to be "visible in the queue as an exclusion rather than an
  -- absence".
  auto_excluded_predicate_id text,
  auto_excluded_predicate_digest sha256_digest,

  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT harvest_candidates_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT harvest_candidates_uk UNIQUE (harvest_batch_id, study_instance_uid),
  CONSTRAINT harvest_candidates_batch_fk FOREIGN KEY (tenant_id, harvest_batch_id)
    REFERENCES harvest_batches (tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT harvest_candidates_ran_ck
    CHECK (ran_on_platform = (platform_outcome IS NOT NULL)),
  CONSTRAINT harvest_candidates_predicate_ck
    CHECK ((auto_excluded_predicate_id IS NULL) = (auto_excluded_predicate_digest IS NULL))
);

CREATE INDEX harvest_candidates_batch_idx
  ON harvest_candidates (tenant_id, harvest_batch_id, patient_key);

CREATE TRIGGER harvest_candidates_identity_sealed BEFORE UPDATE ON harvest_candidates
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','harvest_batch_id',
    'patient_key','study_instance_uid','sampling_weight');


-- =====================================================================================
-- 6. curation_decisions -- chapter 17 `CurationDecision`, MOS-TRAIN-080.
--
-- DIVERGENCE 3 AND 4, together, because they are one rule.
--
-- MOS-STORE-359 states that "`curation_decisions` and `corpus_stratification_reports` are
-- append-only ... an editable version of either is not a record", and section 12.12.1's
-- DDL listing attaches the trigger to the second table and not to the first. The prose is
-- normative and the omission is a transcription gap (REPORTED). The trigger is attached.
--
-- MOS-TRAIN-203: "Deleting an excluded row MUST be refused. This is the rule that keeps a
-- cohort honest: cases dropped one at a time because 'the model does badly on them' is
-- the mechanism by which a training corpus becomes optimistic, and it is invisible from
-- the sealed manifest alone." Section 12.12.1 renders the exclusion as a
-- `curation_decisions` row whose `decision = 'exclude'`, and a `harvest_candidates` FK
-- that is ON DELETE CASCADE. So the append-only trigger below is the whole enforcement,
-- and it is transitive: deleting the candidate cascades into this table, the cascade
-- fires this row trigger, and the delete of the candidate is refused with it. Deleting
-- the batch cascades one level further and is refused the same way. A candidate with NO
-- settled decision is still deletable, which is correct -- an undecided candidate is not
-- an exclusion, it is backlog.
--
-- THE VOCABULARY. MOS-TRAIN-080's closed set is the CHECK below. MOS-TRAIN-204 is the
-- rule ABOUT the set -- "`reason_code` MUST NOT include any value whose meaning is a
-- model outcome ... A curation tool that offers `model_performed_poorly`, `outlier`,
-- `hard_case` or an equivalent MUST be treated as non-conformant" -- and it is enforced
-- in `medos/training/vocabulary.py`, where a per-Dataset vocabulary (MOS-TRAIN-203) can
-- be screened before it reaches a column. A CHECK cannot screen a vocabulary it has
-- never seen; this one bounds what can be written today.
-- =====================================================================================
CREATE TABLE curation_decisions (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  candidate_id uuid NOT NULL,

  decision text NOT NULL CHECK (decision IN ('include','exclude','defer')),
  reason_code text CHECK (reason_code IN ('quality_artefact','wrong_anatomy','wrong_phase',
    'prior_treatment','duplicate_patient','geometry_unsupported','annotation_infeasible',
    'out_of_scope')),
  note text,                                   -- reviewer note; no PHI (MOS-STORE-272)
  review_seconds integer NOT NULL CHECK (review_seconds >= 0),
  decided_by uuid NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT curation_decisions_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT curation_decisions_candidate_fk FOREIGN KEY (tenant_id, candidate_id)
    REFERENCES harvest_candidates (tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT curation_decisions_reason_ck
    CHECK ((decision = 'exclude') = (reason_code IS NOT NULL))
);

-- `defer` is not terminal, so a candidate may accumulate deferrals and then settle once.
-- MOS-TRAIN-080 forbids auto-inclusion, so there is no default row: a candidate with no
-- settled decision is simply not in a cohort, and the absence is the queue's backlog.
CREATE UNIQUE INDEX curation_decisions_settled_uk ON curation_decisions (candidate_id)
  WHERE decision <> 'defer';

CREATE TRIGGER curation_decisions_append_only
  BEFORE UPDATE OR DELETE ON curation_decisions
  FOR EACH ROW EXECUTE FUNCTION forbid_curation_mutation();

COMMENT ON TABLE curation_decisions IS
  'MOS-TRAIN-080: every candidate receives an explicit decision by a NAMED HUMAN before '
  'it can enter a DatasetVersion. Auto-inclusion MUST NOT be implemented. MOS-TRAIN-203: '
  'deleting an excluded row MUST be refused -- hence the append-only trigger.';


-- =====================================================================================
-- 7. corpus_stratification_reports -- chapter 17 MOS-TRAIN-088.
--
-- The record that a cohort passed the check which permitted it to seal, and whose `fail`
-- would have blocked it. `checks` carries one entry per C1..C7 with its statistic, its
-- bound and its outcome -- MOS-TRAIN-088 requires the FULL result, not the verdict, and
-- MOS-TRAIN-011 makes this report part of the evidence set a promotion decision must
-- show. The computation lives in `medos/evidence/stratification.py`; this table is where
-- the seal parks it, because MOS-TRAIN-088 says "on the batch".
--
-- `dataset_version_id` is nullable: MOS-TRAIN-088 also runs the check when "issuing any
-- `vendor_evidence` report against a cohort so sealed", and the console of MOS-UI-131
-- runs the battery BEFORE it creates anything. A report computed by a dry run has no
-- version to point at, and it is still a record.
-- =====================================================================================
CREATE TABLE corpus_stratification_reports (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  harvest_batch_id uuid NOT NULL,
  dataset_version_id uuid,
  verdict text NOT NULL CHECK (verdict IN ('pass','warn','fail')),
  checks jsonb NOT NULL CHECK (checks ?& array['C1','C2','C3','C4','C5','C6','C7']),
  computed_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT corpus_stratification_reports_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT corpus_stratification_reports_batch_fk
    FOREIGN KEY (tenant_id, harvest_batch_id)
    REFERENCES harvest_batches (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT corpus_stratification_reports_dataset_version_fk
    FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT
);

CREATE TRIGGER corpus_stratification_reports_append_only
  BEFORE UPDATE OR DELETE ON corpus_stratification_reports
  FOR EACH ROW EXECUTE FUNCTION forbid_curation_mutation();

COMMENT ON TABLE corpus_stratification_reports IS
  'MOS-TRAIN-088: C1-C7 at seal. A fail blocks sealing. MOS-TRAIN-091: a waiver is '
  'written, never silent, and is reproduced in every ValidationReport citing the cohort.';


-- =====================================================================================
-- 7a. `curation_batch_items` -- MOS-TRAIN-202's shape, as a view over the two tables
--     chapter 12 renders it into.
--
-- MOS-TRAIN-202 fixes what a `CurationBatch` IS: "an explicit, materialised list of
-- candidate `(patient_key, study_instance_uid, series_instance_uid)` triples with a
-- per-candidate disposition", and prints a `curation_batch_items` table as its "Semantic
-- definition. Physical form follows Chapter 12's conventions." Section 12.12.1 is that
-- physical form, and it splits the record in two and changes its grain: a
-- `harvest_candidates` row is one STUDY carrying a `series_instance_uids` ARRAY, and the
-- disposition lives on a separate `curation_decisions` row.
--
-- Both are right, and nothing is reconciled by choosing. The tables stay exactly as
-- MOS-STORE-358 specifies -- they are what is written, indexed, sealed and granted -- and
-- this view is the SEMANTIC shape read back, one row per triple, which is the grain
-- chapter 17's acceptance criterion 4 and the operator queue of MOS-UI-130 are both
-- stated over. A view is the right form for it: it holds no state that could drift from
-- the tables, and it cannot be the target of a DELETE the grants do not permit.
--
-- `security_invoker` so the tenant-isolation policies of section 8 apply to the READER
-- rather than to the view's owner. Without it a view over an RLS table is a hole in the
-- isolation, which is the standard way this exact convenience is got wrong.
--
-- AND IT DOES NOT PROJECT `tenant_id`. A view carries no row-level security of its own, so
-- a `tenant_id` column on one is the shape MOS-SEC-077 is written against -- "no
-- `tenant_id` column anywhere without forced row security" -- even when, as here, the
-- invoker's policies make every visible row the reader's own anyway. It is also load
-- bearing beyond the principle: the test harness enumerates tenant-owned relations out of
-- `information_schema.columns` by that column name, which does not distinguish a view from
-- a table, and then issues a `DELETE` that a join view cannot accept. A redundant column
-- that turns a catalog sweep into an error is not worth the convenience of not joining.
-- =====================================================================================
CREATE VIEW curation_batch_items
  WITH (security_invoker = true) AS
SELECT c.harvest_batch_id                         AS batch_id,
       c.patient_key,
       c.study_instance_uid,
       s.series_instance_uid,
       d.decision                                 AS disposition,
       d.reason_code,
       d.note                                     AS reason_note,
       d.decided_by,
       d.decided_at,
       c.id                                       AS candidate_id,
       c.auto_excluded_predicate_id
  FROM harvest_candidates c
  CROSS JOIN LATERAL unnest(c.series_instance_uids) AS s(series_instance_uid)
  LEFT JOIN curation_decisions d
    ON d.tenant_id = c.tenant_id AND d.candidate_id = c.id AND d.decision <> 'defer';

COMMENT ON VIEW curation_batch_items IS
  'MOS-TRAIN-202: the CurationBatch as (patient_key, study_instance_uid, '
  'series_instance_uid) triples with a per-candidate disposition. Read-only; the tables '
  'of MOS-STORE-358 are what is written. A NULL disposition is the queue backlog -- '
  'MOS-TRAIN-080 forbids auto-inclusion, so an undecided candidate has no decision row.';

GRANT SELECT ON curation_batch_items TO medicalos_app, medicalos_readonly;


-- =====================================================================================
-- 7b. MOS-TRAIN-205 needs a `derivation` on a version that has no parent.
--
-- 0006 renders MOS-EVID-022 as
--     CONSTRAINT dataset_versions_derivation
--       CHECK ((parent_version_id IS NULL) = (derivation IS NULL))
-- and its comment states the reason for one half of that equality: "a `parent_version_id`
-- with no `derivation` is a lineage edge whose operation nobody can state." That half is
-- kept below, unchanged.
--
-- The OTHER half -- a `derivation` is forbidden unless there is a parent -- has no stated
-- reason, and MOS-TRAIN-205 contradicts it directly: "The exclusion set MUST be recorded
-- as the `derivation` object of MOS-EVID-022 on the sealed version", where the sealed
-- version is the FIRST version of a cohort drawn from a `CurationBatch` and has no parent
-- to point at. Chapter 17's acceptance criterion 4 then asks that "a sealed
-- `DatasetVersion` derived from a batch carries a `derivation` whose `removed_series`
-- equals the batch's `exclude` count". Under the equality that row cannot be written at
-- all, and the exclusions -- the whole point of MOS-TRAIN-203 -- would be visible only on
-- the batch and never on the cohort a `ValidationReport` cites.
--
-- So the constraint is REPLACED with the implication rather than dropped. MOS-EVID-022's
-- enforceable content survives; what is permitted additionally is exactly the case
-- MOS-TRAIN-205 describes. 0006 is not edited: `medos.db.migrate` refuses an applied
-- migration whose digest changed, and a migration that rewrites its predecessor's file is
-- a migration nobody can replay.
--
-- SPEC TENSION, reported upward rather than silently reconciled: MOS-EVID-022 scopes
-- `derivation` to version-to-version lineage; MOS-TRAIN-205 puts a batch's exclusion set
-- in the same field on a parentless version. Both cannot be read as an equality.
-- =====================================================================================
ALTER TABLE dataset_versions DROP CONSTRAINT dataset_versions_derivation;
ALTER TABLE dataset_versions ADD CONSTRAINT dataset_versions_derivation
  CHECK (parent_version_id IS NULL OR derivation IS NOT NULL);


-- =====================================================================================
-- 8. ENABLE + FORCE ROW LEVEL SECURITY, and the policies.
--     MOS-TRAIN-071, MOS-SEC-072, MOS-STORE-223, MOS-STORE-224, MOS-STORE-229.
--
-- Identical in shape to 0002 section 10 and 0006 section 11, and for the same reasons.
-- MOS-TRAIN-071 is the chapter-17 statement of why it matters here: "The harvest MUST NOT
-- pool studies across tenants into one `HarvestBatch`." Under FORCE RLS a cross-tenant
-- batch is not a policy anyone has to remember.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['training_data_policies','sampling_plans','harvest_batches',
                           'harvest_candidates','curation_decisions',
                           'corpus_stratification_reports'] LOOP
    EXECUTE format('ALTER TABLE %I OWNER TO medicalos_owner', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I '
      'USING (tenant_id = current_tenant_id()) '
      'WITH CHECK (tenant_id = current_tenant_id())', t || '_tenant_isolation', t);
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION forbid_column_change(''tenant_id'')',
      t || '_tenant_immutable', t);
  END LOOP;
END $$;


-- =====================================================================================
-- 9. Grants.  MOS-STORE-228, MOS-STORE-230, MOS-STORE-325, MOS-STORE-359.
--
-- `medicalos_app` owns nothing and is NOBYPASSRLS. The grant answers "may this role
-- perform this verb", the policy answers "on whose rows", and the triggers of sections
-- 2-7 bind the owner and a superuser session too.
--
-- NO DELETE ANYWHERE. MOS-TRAIN-203 is the reason for `curation_decisions`; the other
-- five follow it, because a curation record whose surrounding rows can be dropped is a
-- record of a decision about a case nobody can name any more.
--
-- NO UPDATE on `sampling_plans`, `curation_decisions` or `corpus_stratification_reports`:
-- MOS-STORE-359 puts all three on the table-level revocation list.
-- =====================================================================================
GRANT SELECT, INSERT ON training_data_policies TO medicalos_app;
-- MOS-TRAIN-075/076: withdrawal and expiry are the only edits. `updated_at` rides along
-- because `touch_updated_at()` writes it on exactly those updates.
GRANT UPDATE (revoked_at, revocation_reason, expires_at, updated_at)
  ON training_data_policies TO medicalos_app;

GRANT SELECT, INSERT ON sampling_plans TO medicalos_app;

GRANT SELECT, INSERT ON harvest_batches TO medicalos_app;
GRANT UPDATE (state, candidate_count, dataset_version_id, sealed_at, updated_at)
  ON harvest_batches TO medicalos_app;

GRANT SELECT, INSERT ON harvest_candidates TO medicalos_app;
-- MOS-TRAIN-080: auto-exclusion MAY annotate a candidate after it is drawn. Identity and
-- `sampling_weight` are sealed by trigger as well as absent from this list.
GRANT UPDATE (score_band, acquisition_bucket, review_outcome, platform_outcome,
              ran_on_platform, corpus_generation, auto_excluded_predicate_id,
              auto_excluded_predicate_digest)
  ON harvest_candidates TO medicalos_app;

GRANT SELECT, INSERT ON curation_decisions, corpus_stratification_reports
  TO medicalos_app;

REVOKE UPDATE, DELETE ON sampling_plans, curation_decisions,
  corpus_stratification_reports FROM medicalos_app;
REVOKE DELETE ON training_data_policies, harvest_batches, harvest_candidates
  FROM medicalos_app;

GRANT SELECT ON training_data_policies, sampling_plans, harvest_batches,
  harvest_candidates, curation_decisions, corpus_stratification_reports
  TO medicalos_readonly;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 10. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================
DO $$
DECLARE
  n integer;
  bad text;
BEGIN
  -- MOS-TRAIN-079 / MOS-STORE-272: the PHI columns that must never exist on a candidate.
  SELECT string_agg(column_name, ', ') INTO bad
    FROM information_schema.columns
   WHERE table_schema = 'public' AND table_name = 'harvest_candidates'
     AND column_name IN ('patient_name','patient_birth_date','patient_id',
                         'accession_number','institution_name','source_study_instance_uid',
                         'source_series_instance_uid','study_date');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-TRAIN-079: harvest_candidates carries PHI columns: %', bad;
  END IF;

  -- Six tables, six policies, FORCE on all of them.
  SELECT count(*) INTO n FROM pg_class c
    JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = 'public' AND c.relforcerowsecurity
     AND c.relname IN ('training_data_policies','sampling_plans','harvest_batches',
                       'harvest_candidates','curation_decisions',
                       'corpus_stratification_reports');
  IF n <> 6 THEN
    RAISE EXCEPTION 'expected FORCE ROW LEVEL SECURITY on 6 tables, found %', n;
  END IF;

  -- MOS-STORE-359 / MOS-TRAIN-203: no DELETE grant on any of the six, for any role.
  SELECT string_agg(DISTINCT table_name, ', ') INTO bad
    FROM information_schema.role_table_grants
   WHERE table_schema = 'public' AND privilege_type = 'DELETE'
     AND grantee <> 'medicalos_owner'
     AND table_name IN ('training_data_policies','sampling_plans','harvest_batches',
                        'harvest_candidates','curation_decisions',
                        'corpus_stratification_reports');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-TRAIN-203: DELETE is granted on %', bad;
  END IF;

  -- MOS-TRAIN-073: the deferred constraint trigger exists and is deferred.
  SELECT count(*) INTO n FROM pg_trigger
   WHERE tgname = 'tenants_training_policy_required' AND tgdeferrable AND tginitdeferred;
  IF n <> 1 THEN
    RAISE EXCEPTION 'MOS-TRAIN-073: tenants_training_policy_required is not deferred';
  END IF;
END $$;
