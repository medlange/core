-- =====================================================================================
-- 0006_evidence.up.sql -- the sealed half of the evidence plane.
--
-- docs/spec/15-delivery.md section 15.2.5 (weeks 6-9, tag 0.2.0): "`datasets`, sealed
-- content-addressed `dataset_versions` with licence and de-identification status per
-- source, patient-level splits, annotation sets naming readers and the consensus rule".
-- Chapter 17 `MOS-TRAIN-190` places curation, sealing, split freezing and `AnnotationSet`
-- production in the SAME release, which is why the seal-time checks land with the tables
-- rather than after them.
--
-- SCOPE. Eight of the sixteen tables of chapter 12 section 12.12: the four entity
-- families chapter 7 sections 7.3-7.5 own, each with its detail table.
--
--     datasets               7.3.1   mutable metadata, one SOURCE, one licence
--     dataset_versions       7.3.1   SEALED, content-addressed         MOS-EVID-013/014
--     dataset_cases          7.3.2   the manifest, one row per SERIES  MOS-STORE-291
--     dataset_splits         7.4.1   FROZEN manifest, never a seed     MOS-EVID-028
--     dataset_split_members  7.4.1   PK (split_id, patient_key)        MOS-STORE-293
--     annotation_sets        7.5     FROZEN, names the consensus rule  MOS-EVID-039
--     annotation_readers     7.5     the per-reader detail table       MOS-EVID-038
--     annotations            7.5     the reference standard, per case  MOS-EVID-040
--
-- NOT in this migration, on purpose: `evaluation_runs`, `evaluation_case_metrics`,
-- `evaluation_case_scores`, `capability_claims`, `acceptance_criteria`,
-- `tenant_acceptance_bindings`, `deployment_gate_decisions`, `validation_reports`. They
-- are the measuring half of 15.2.5 and they all reference a sealed cohort, so they land
-- on top of this one. Declaring a stub for any of them here would be a second answer to
-- a question this migration does not own.
--
-- THE ONE VERB THAT MATTERS: SEALING.
--
--   MOS-EVID-013  "Sealing/freezing is enforced at the database level, not in
--                 application code". Section 10 below is that enforcement:
--                 `forbid_column_change` over the sealed column list of section 12.12,
--                 `forbid_mutation` on DELETE, and the grant-level revocation. A trigger
--                 binds the OWNER and a superuser session too, which a GRANT does not,
--                 and an evidence row that an operator can quietly edit is not evidence.
--   MOS-EVID-014  A sealed object MAY be marked defective but MUST NOT be edited. Hence
--                 the four-column column-level UPDATE grant on `dataset_versions` and a
--                 table-level UPDATE grant on nothing else.
--   MOS-EVID-016  A DatasetVersion MUST NOT be created by reference to a live query, a
--                 folder path, a DICOM query filter or a database view. `dataset_cases`
--                 is the materialised list; there is no view here and none may be added.
--
-- ADDITIVE, like 0002-0005: it creates tables and alters none of the existing ones.
-- `schema.sql` remains the baseline (MOS-STORE-214).
--
-- THREE DELIBERATE DIVERGENCES FROM CHAPTER 12 SECTION 12.12, each at the point of use:
--   1. `UNIQUE (tenant_id, manifest_digest)`, not `UNIQUE (manifest_digest)` -- section 3.
--   2. `public_id` carrying chapter 7's `ds_`/`dsv_`/`spl_`/`ann_` ULID identity, the
--      `MOS-STORE-357` construction applied to section 12.12 -- section 2.
--   3. `case_key`, which section 12.12 keys on and no chapter defines -- section 4.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The `sha256_digest` domain.  MOS-STORE-211, MOS-EVID-007.
--
-- "Digests use the domain `sha256_digest` -- lower-case hex prefixed `sha256:` -- so the
-- algorithm stays readable when a second one is introduced." MOS-EVID-007 closes the set
-- for 0.2.0: SHA-256 and no other algorithm, serialised as `"sha256:" + lowercase_hex`.
-- Nine digest columns in this migration depend on that spelling being checked ONCE
-- rather than restated as nine CHECK constraints that can drift apart.
--
-- Guarded, on the 0005 precedent for `uuid_generate_v7()`: several migrations in this
-- release block were authored in parallel and any of them may want the domain.
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
-- 2. datasets.  Chapter 7 section 7.3.1.
--
-- A mutable container. The LICENCE lives here and not on the version, and that placement
-- is the whole per-source licence story of 15.2.5: `custodian` IS chapter 7's `source_id`
-- (section 12.12 renames it, section 7.3.1 gives the mapping), so one `datasets` row is
-- one source, and its licence is that source's licence. A corpus assembled from TCIA
-- collections under CC BY 3.0 and CC BY-NC 3.0 is therefore TWO datasets, not one with a
-- licence array -- and a cohort drawn across both has no single `dataset_id` to hang a
-- licence on, which is a real limit of the chapter-7 model and is REPORTED, not patched
-- here with a table no chapter owns.
--
-- `public_id`.  Chapter 7 section 7.2 gives every entity a `<prefix>_<ULID>` identity and
-- the `ValidationReport` cohort block of section 7.12.1 prints it verbatim
-- ("dataset_version_id": "dsv_01JAY7N4K2ZP8QVCM3RXTD6WEB"). Chapter 12 section 12.12 is
-- normative for physical form and says `uuid` primary keys. Both are satisfiable at once
-- and MOS-STORE-357 already did it for `jobs`: the uuid is internal and never emitted,
-- `public_id` is the external identifier, assigned once and immutable. This is that
-- construction under four more prefixes. It is a fact chapter 7 requires that section
-- 12.12 has no column for, which section 12.12 itself says is to be resolved by adding
-- the column here.
-- =====================================================================================
CREATE TABLE datasets (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- Crockford base32, upper case, no I/L/O/U -- `medos.core.canonical.ULID_ALPHABET`.
  public_id     text NOT NULL UNIQUE CHECK (public_id ~ '^ds_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  slug          text NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9._-]{1,126}$'),
  display_name  text NOT NULL CHECK (display_name <> ''),
  -- MOS-EVID-026 is normative for this five-value set; mirrored exactly.
  purpose       text NOT NULL
    CHECK (purpose IN ('training','tuning','evaluation','acceptance','monitoring')),
  -- Chapter 7's `source_id` (section 7.3.1 gives this mapping explicitly). Carried
  -- verbatim into manifests and exported bundles, and the `issuer` fallback of
  -- MOS-EVID-010 when `IssuerOfPatientID` (0010,0021) is absent -- which is why it may
  -- not be empty: an empty issuer silently merges two collections' patient keys.
  custodian     text NOT NULL CHECK (custodian <> ''),
  -- MOS-EVID-012. Mirrored exactly.
  visibility    text NOT NULL DEFAULT 'tenant_private'
    CHECK (visibility IN ('tenant_private','tenant_shared','public_readonly')),
  licence_spdx  text,
  licence_text  text,
  licence_url   text,
  created_by    uuid NOT NULL,
  deleted_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  -- MOS-STORE-217: the redundant key every composite child FK points at.
  CONSTRAINT datasets_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT datasets_slug_uk      UNIQUE (tenant_id, slug),
  -- MOS-EVID-027, verbatim from section 12.12.
  CONSTRAINT datasets_licence_required CHECK (
    visibility = 'tenant_private' OR licence_spdx IS NOT NULL OR licence_text IS NOT NULL)
);

CREATE INDEX datasets_purpose_idx ON datasets (tenant_id, purpose)
  WHERE deleted_at IS NULL;

CREATE TRIGGER datasets_touch BEFORE UPDATE ON datasets
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- MOS-STORE-218: moving a row between tenants is not supported.
CREATE TRIGGER datasets_tenant_immutable BEFORE UPDATE ON datasets
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','public_id');

COMMENT ON TABLE datasets IS
  'Chapter 7 section 7.3: a named, mutable container. One row is ONE SOURCE '
  '(`custodian` = chapter 7 `source_id`) and carries that source''s licence of record '
  '(MOS-EVID-027).';


-- =====================================================================================
-- 3. dataset_versions.  SEALED.  Chapter 7 section 7.3.1, MOS-STORE-290.
--
-- Content-addressed: `manifest_digest` is `MOS-EVID-009`'s digest over the canonical
-- serialisation of the whole `dataset_cases` set, so two versions with the same digest
-- contain the same instances and a correction is a NEW version with `parent_version_id`
-- and `derivation` set (MOS-EVID-022, MOS-EVID-023).
--
-- DIVERGENCE 1, and the reason.  Section 12.12 writes `UNIQUE (manifest_digest)`,
-- unscoped. This migration writes `UNIQUE (tenant_id, manifest_digest)`. Two reasons,
-- both structural:
--
--   * Under FORCE row-level security an unscoped unique index is a cross-tenant
--     EXISTENCE ORACLE: tenant B sealing a cohort tenant A already holds gets 23505 on a
--     row B is not permitted to SELECT. That is an information flow the policy cannot
--     see, in the one table whose whole purpose is provenance.
--   * MOS-TRAIN-209 requires the collision to be RESOLVED by reuse -- "detect the
--     collision against the `UNIQUE` constraint on `manifest_digest` and reuse the
--     existing `DatasetVersion`". Reuse means SELECTing the colliding row. Across
--     tenants that SELECT returns nothing, so the only reachable outcome is a hard
--     failure with no remedy. Tenant-scoping makes the reuse path executable, which is
--     what the requirement asks for.
--
-- Nothing is lost: MOS-EVID-010 salts `patient_key` per tenant, so two tenants holding
-- byte-identical images already produce different manifest lines and different digests.
-- REPORTED as a divergence rather than made silently.
-- =====================================================================================
CREATE TABLE dataset_versions (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  public_id           text NOT NULL UNIQUE
    CHECK (public_id ~ '^dsv_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  dataset_id          uuid NOT NULL,
  version             integer NOT NULL CHECK (version >= 1),
  -- MOS-EVID-022: set whenever a version is derived from another by inclusion,
  -- exclusion or re-de-identification, and the derivation recorded alongside it.
  parent_version_id   uuid,
  derivation          jsonb,
  -- MOS-STORE-201's `(bucket, object_key)` pair for an object-store location.
  manifest_bucket     text NOT NULL CHECK (manifest_bucket <> ''),
  manifest_object_key text NOT NULL CHECK (manifest_object_key <> ''),
  manifest_digest     sha256_digest NOT NULL,
  manifest_line_count integer NOT NULL CHECK (manifest_line_count >= 0),
  case_count          integer NOT NULL CHECK (case_count >= 0),
  patient_count       integer NOT NULL CHECK (patient_count >= 0),
  study_count         integer NOT NULL CHECK (study_count >= 0),
  series_count        integer NOT NULL CHECK (series_count >= 0),
  instance_count      integer NOT NULL CHECK (instance_count >= 0),
  source_description  text NOT NULL,
  -- MOS-EVID-024: computed, never supplied. The SOLE permitted input to automatic
  -- envelope derivation (7.10.2) and, by MOS-TRAIN-093, the sole input to the corpus
  -- stratification check -- so that the check is recomputable offline from the bundle.
  acquisition_profile jsonb NOT NULL,
  -- MOS-EVID-012 / MOS-EVID-021 are normative for this three-value set. MOS-STORE-292a:
  -- this is NOT `patients.phi_state`, the two MUST NOT be conflated and a query MUST NOT
  -- join on them, notwithstanding the two shared tokens.
  deidentification_status text NOT NULL
    CHECK (deidentification_status IN ('identified','pseudonymised','public_deidentified')),
  deid_policy_id      text,
  -- MOS-EVID-021: the `(tenant_id, deid_key_version)` UID space of section 12.8 the
  -- version was sealed under. A version sealed under one UID mapping is not
  -- interchangeable with the same images under another.
  uid_mapping_table_id text,
  -- MOS-EVID-014. The only mutable evidence state on the row, with `defect_reason`.
  status              text NOT NULL DEFAULT 'SEALED'
    CHECK (status IN ('SEALED','DEFECTIVE')),
  defect_reason       text,
  -- MOS-STORE-292: mutable for erasure, never for content.
  erasure_state       text NOT NULL DEFAULT 'clean'
    CHECK (erasure_state IN ('clean','contains_erased_subject')),
  usable_for_new_runs boolean NOT NULL DEFAULT true,
  sealed_at           timestamptz NOT NULL DEFAULT now(),
  sealed_by           uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT dataset_versions_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT dataset_versions_lineage_uk   UNIQUE (tenant_id, dataset_id, version),
  CONSTRAINT dataset_versions_digest_uk    UNIQUE (tenant_id, manifest_digest),
  CONSTRAINT dataset_versions_dataset_fk FOREIGN KEY (tenant_id, dataset_id)
    REFERENCES datasets (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT dataset_versions_parent_fk FOREIGN KEY (tenant_id, parent_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT dataset_versions_counts CHECK (patient_count <= case_count),
  CONSTRAINT dataset_versions_deid_policy
    CHECK (deidentification_status = 'identified' OR deid_policy_id IS NOT NULL),
  CONSTRAINT dataset_versions_defect_reason
    CHECK ((status = 'DEFECTIVE') = (defect_reason IS NOT NULL)),
  -- MOS-EVID-022: a derived version records BOTH halves or neither. A `parent_version_id`
  -- with no `derivation` is a lineage edge whose operation nobody can state.
  CONSTRAINT dataset_versions_derivation
    CHECK ((parent_version_id IS NULL) = (derivation IS NULL))
);

CREATE INDEX dataset_versions_dataset_idx
  ON dataset_versions (tenant_id, dataset_id, version DESC);

CREATE TRIGGER dataset_versions_touch BEFORE UPDATE ON dataset_versions
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMENT ON TABLE dataset_versions IS
  'Chapter 7 section 7.3 / MOS-STORE-290: sealed at creation, content-addressed by '
  'manifest_digest. MOS-EVID-023: adding cases is not possible; it produces a new '
  'version.';


-- =====================================================================================
-- 4. dataset_cases.  The manifest, persisted.  MOS-STORE-291, chapter 7 section 7.3.2.
--
-- One row per SERIES, not per study and not per instance -- section 7.3.2 says "One line
-- per series" and this table is those lines.
--
-- MOS-STORE-291: it references DICOM UIDs and NOT foreign keys into `instances`,
-- because "the evidence must outlive the projection". So no FK to `studies`, none to
-- `series`, none to `instances`, and adding one later would couple a sealed manifest's
-- integrity to a rebuildable projection's lifecycle.
--
-- DIVERGENCE 3.  Section 12.12 keys this table `PK (dataset_version_id, case_key,
-- series_instance_uid)` and NO chapter defines `case_key`. It appears again on
-- `evaluation_case_metrics` and `evaluation_case_scores` alongside `patient_key`,
-- `study_instance_uid` and `series_instance_uid`, so it is neither of those. Defined
-- here, in this module only, per CONTRACT.md section 0, and REPORTED:
--
--     case_key IS THE STUDY-LEVEL CASE HANDLE. A case is one study of one patient --
--     the unit an evaluation scores and the unit MOS-EVID-046 means by "a case present
--     in the split but absent from the annotation manifest". It is populated with the
--     `study_instance_uid` and kept as a separate column so that a capability whose case
--     is not a study (a per-series screening read, say) has somewhere to put its own
--     handle without a schema change.
--
-- `corpus_generation`.  MOS-TRAIN-087: generation 0 is de novo, g+1 is seeded by a model
-- whose corpus reached g, and a case at 2 or above MUST NOT be used in any partition. The
-- CHECK is the structural form of that rule -- a sealed cohort cannot contain one, so the
-- prohibition cannot be forgotten at split time.
-- =====================================================================================
CREATE TABLE dataset_cases (
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  dataset_version_id  uuid NOT NULL,
  case_key            text NOT NULL CHECK (case_key <> ''),
  series_instance_uid dicom_uid NOT NULL,
  -- MOS-EVID-010: HMAC(tenant_salt, issuer|patient_id), 10 bytes, base32, lower case.
  -- 80 bits is exactly 16 base32 characters, so there is no padding to strip in a CHECK.
  patient_key         text NOT NULL CHECK (patient_key ~ '^pk_[a-z2-7]{16}$'),
  study_instance_uid  dicom_uid NOT NULL,
  modality            text NOT NULL,
  sop_class_uid       dicom_uid NOT NULL,
  instance_count      integer NOT NULL CHECK (instance_count >= 1),
  -- MOS-EVID-017: every instance in the series, in the canonical slice order of ch. 4.
  sop_instance_uids   dicom_uid[] NOT NULL CHECK (cardinality(sop_instance_uids) >= 1),
  -- MOS-EVID-018: over STORED pixel values, no rescale, no geometry normalisation, so
  -- dataset identity does not change when the geometry code changes.
  series_pixel_digest sha256_digest NOT NULL,
  -- MOS-EVID-019: a lossy series' digest is valid only for its exact stored
  -- representation and MUST NOT be relied on for cross-site equality.
  lossy_compressed    boolean NOT NULL,
  -- MOS-EVID-035: HMAC(tenant_salt, AccessionNumber), 10 bytes, base32. The RAW
  -- accession number MUST NOT be stored.
  accession_number_hash text CHECK (accession_number_hash ~ '^[a-z2-7]{16}$'),
  -- MOS-EVID-020: copied from the source header WITHOUT imputation. A missing value is
  -- JSON null, never a default. `convolution_kernel_class` is the only derived member.
  acquisition         jsonb NOT NULL,
  corpus_generation   smallint NOT NULL DEFAULT 0
    CHECK (corpus_generation BETWEEN 0 AND 1),
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT dataset_cases_pk
    PRIMARY KEY (dataset_version_id, case_key, series_instance_uid),
  CONSTRAINT dataset_cases_version_fk FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT dataset_cases_instance_count
    CHECK (cardinality(sop_instance_uids) = instance_count)
);

-- Section 12.12 names both of these. The second is what leakage check L3 scans.
CREATE INDEX dataset_cases_study_idx  ON dataset_cases (tenant_id, study_instance_uid);
CREATE INDEX dataset_cases_pixel_idx  ON dataset_cases (tenant_id, series_pixel_digest);
CREATE INDEX dataset_cases_patient_idx
  ON dataset_cases (tenant_id, dataset_version_id, patient_key);


-- =====================================================================================
-- 5. dataset_splits.  FROZEN.  Chapter 7 section 7.4.1.
--
-- MOS-EVID-028: "The platform MUST NOT store a seed in place of a manifest and MUST NOT
-- regenerate a split from a seed under any circumstance." There is therefore NO `seed`
-- column on this table and one MUST NOT be added; `assignment_method` is a descriptive
-- string, never re-executed, and MOS-TRAIN-112 puts any seed that was used INSIDE it.
--
-- MOS-EVID-029 / MOS-STORE-294: `partition_level` is constrained to the single value
-- 'patient'. A study-level split is not a configuration this schema can express, which
-- is the point -- the same patient's two studies landing in train and test is the most
-- common and most invisible form of leakage in medical imaging evaluation.
--
-- `leakage_report` is NOT NULL and is a sealed column, so the L1-L5 results and any
-- MOS-EVID-036 waiver are decided BEFORE the freeze and cannot be edited after it. That
-- is the structural form of "there is no silent waiver".
-- =====================================================================================
CREATE TABLE dataset_splits (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  public_id           text NOT NULL UNIQUE
    CHECK (public_id ~ '^spl_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  dataset_version_id  uuid NOT NULL,
  name                text NOT NULL CHECK (name <> ''),
  partition_level     text NOT NULL DEFAULT 'patient'
    CHECK (partition_level = 'patient'),
  partitions          text[] NOT NULL CHECK (cardinality(partitions) >= 1),
  partition_patients  jsonb NOT NULL,
  assignment_method   text NOT NULL CHECK (assignment_method <> ''),
  stratified_by       text[] NOT NULL DEFAULT '{}',
  leakage_report      jsonb NOT NULL,
  manifest_bucket     text NOT NULL CHECK (manifest_bucket <> ''),
  manifest_object_key text NOT NULL CHECK (manifest_object_key <> ''),
  -- Chapter 7's `split_digest`; section 12.12 spells the column `manifest_digest`.
  manifest_digest     sha256_digest NOT NULL,
  -- Chapter 7's `frozen_at` / `frozen_by`; section 12.12 spells them `sealed_*`.
  sealed_at           timestamptz NOT NULL DEFAULT now(),
  sealed_by           uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT dataset_splits_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT dataset_splits_name_uk      UNIQUE (dataset_version_id, name),
  CONSTRAINT dataset_splits_version_fk FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT
);

CREATE INDEX dataset_splits_version_idx
  ON dataset_splits (tenant_id, dataset_version_id);

COMMENT ON TABLE dataset_splits IS
  'Chapter 7 section 7.4 / MOS-EVID-028: a materialised manifest. Never a seed. There is '
  'no seed column and one MUST NOT be added.';


-- =====================================================================================
-- 6. dataset_split_members.  MOS-STORE-293.
--
-- `PRIMARY KEY (split_id, patient_key)` "makes patient-level leakage across partitions
-- STRUCTURALLY IMPOSSIBLE -- a patient appears in exactly one partition". The L1 check
-- of MOS-EVID-034 still runs at freeze time, over the manifest lines, because the
-- manifest is what a reader verifies offline and because L1 must be reported in
-- `leakage_report` whether or not the database could have caught it.
--
-- `partition` is chapter 7's four-value vocabulary mirrored exactly. `val` is NOT a
-- permitted value (MOS-EVID-031, MOS-EVID-033) and this column is never called `fold`:
-- `fold` is the optional cross-validation index WITHIN a partition (MOS-EVID-030) and
-- conflating the two is how a tuning fold becomes a test set.
-- =====================================================================================
CREATE TABLE dataset_split_members (
  tenant_id        uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  split_id         uuid NOT NULL,
  patient_key      text NOT NULL CHECK (patient_key ~ '^pk_[a-z2-7]{16}$'),
  partition        text NOT NULL
    CHECK (partition IN ('train','tune','test','excluded')),
  -- MOS-EVID-031: a split covering a subset MUST declare the excluded patients
  -- explicitly; silent omission is a write-time error.
  exclusion_reason text,
  fold             integer CHECK (fold IS NULL OR fold >= 0),
  stratum          jsonb,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT dataset_split_members_pk PRIMARY KEY (split_id, patient_key),
  CONSTRAINT dataset_split_members_split_fk FOREIGN KEY (tenant_id, split_id)
    REFERENCES dataset_splits (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT dataset_split_members_exclusion
    CHECK ((partition = 'excluded') = (exclusion_reason IS NOT NULL)),
  -- MOS-EVID-030: `fold` is an index within a partition, so an excluded patient has none.
  CONSTRAINT dataset_split_members_fold
    CHECK (partition <> 'excluded' OR fold IS NULL)
);

CREATE INDEX dataset_split_members_partition_idx
  ON dataset_split_members (tenant_id, split_id, partition);


-- =====================================================================================
-- 7. annotation_sets.  FROZEN.  Chapter 7 section 7.5.
--
-- "On LIDC-IDRI, whether you score against one reader, a >=2 consensus, the union or
-- STAPLE moves Dice more than any model change made in a year." Hence: the consensus
-- rule is a column, not a convention, and its six values are MOS-EVID-039's, mirrored
-- exactly and in that spelling.
--
-- `label_definition_id` is a `capability_concepts` id (MOS-EVID-143, MOS-REG-042,
-- MOS-STORE-256): chapter 7 owns NO code table and no artifact of kind
-- `code_dictionary`. `capability_concepts` is platform-global and does not exist yet, so
-- the column is created with its FK DEFERRED to the migration that creates that table --
-- the same sequencing `studies.deid_policy_id` uses in 0005. Platform-global parents take
-- a plain single-column FK, not a composite one (MOS-STORE-217's closed exception).
--
-- `reference_of_record` defaults FALSE. MOS-EVID-042: a set derived from the output of
-- the artifact being evaluated, or from anything sharing its weights, training data or
-- postprocessing chain, MUST NOT be one -- and an AcceptanceCriteria gate MUST refuse a
-- set whose flag is false. The safe default is the one that refuses.
-- =====================================================================================
CREATE TABLE annotation_sets (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  public_id           text NOT NULL UNIQUE
    CHECK (public_id ~ '^ann_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  dataset_version_id  uuid NOT NULL,
  name                text NOT NULL CHECK (name <> ''),
  -- The `medos.capabilities.REGISTRY` key, as on `results.capability_id`. Not a uuid:
  -- CONTRACT.md section 6 and schema.sql both carry the registry key here.
  capability_id       text NOT NULL CHECK (capability_id <> ''),
  label_definition_id uuid NOT NULL,
  annotation_type     text NOT NULL
    CHECK (annotation_type IN ('mask','bounding_box','point','case_label','measurement')),
  -- MOS-EVID-039 owns this six-value enum. Mirrored exactly; MUST NOT diverge.
  consensus_rule      text NOT NULL CHECK (consensus_rule IN (
    'single_reader','majority_at_least_2','union','intersection','staple','arbitrated')),
  consensus_params    jsonb NOT NULL DEFAULT '{}',
  -- MOS-EVID-038: a set with reader_count = 0 MUST be refused.
  reader_count        integer NOT NULL CHECK (reader_count >= 1),
  reference_of_record boolean NOT NULL DEFAULT false,
  manifest_bucket     text NOT NULL CHECK (manifest_bucket <> ''),
  manifest_object_key text NOT NULL CHECK (manifest_object_key <> ''),
  -- Chapter 7's `annotation_digest`.
  manifest_digest     sha256_digest NOT NULL,
  -- Chapter 7's `frozen_at` / `frozen_by`.
  sealed_at           timestamptz NOT NULL DEFAULT now(),
  sealed_by           uuid NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT annotation_sets_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT annotation_sets_name_uk      UNIQUE (dataset_version_id, name),
  CONSTRAINT annotation_sets_version_fk FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT,
  -- MOS-EVID-039, both halves, verbatim from section 12.12.
  CONSTRAINT annotation_sets_single_reader
    CHECK (consensus_rule <> 'single_reader' OR reader_count = 1),
  CONSTRAINT annotation_sets_majority
    CHECK (consensus_rule <> 'majority_at_least_2' OR reader_count >= 3)
);

CREATE INDEX annotation_sets_version_idx
  ON annotation_sets (tenant_id, dataset_version_id, capability_id);

COMMENT ON COLUMN annotation_sets.label_definition_id IS
  'MOS-EVID-143: a `capability_concepts` row (MOS-REG-042 / MOS-STORE-256). The FK is '
  'added by the migration that creates that platform-global table; chapter 7 owns no '
  'code table and MUST NOT be cited as the owner of one.';


-- =====================================================================================
-- 8. annotation_readers.  The per-reader detail table MOS-EVID-038 requires.
--
-- MOS-EVID-038 requires the set to NAME its readers; MOS-TRAIN-096 requires each
-- `reader_id` to be derived from an authenticated MedicalOS `User`, because a shared
-- login makes the naming requirement unsatisfiable after the fact. The `reader_id` here
-- is that pseudonymous, tenant-stable identity -- never a name, never an email
-- (CONTRACT.md section 11: never log or store a PHI value; a reader is not a patient,
-- but a named radiologist attached to a cohort is personal data all the same).
-- =====================================================================================
CREATE TABLE annotation_readers (
  tenant_id         uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  annotation_set_id uuid NOT NULL,
  reader_id         text NOT NULL CHECK (reader_id ~ '^rdr_[a-z0-9_]{1,60}$'),
  role              text NOT NULL
    CHECK (role IN ('radiologist','resident','algorithm','registry_extract')),
  years_experience  integer CHECK (years_experience IS NULL OR years_experience >= 0),
  board_certified   boolean,
  specialty         text,
  -- MOS-TRAIN-097: for the MONAI Label stack the literal form
  -- "MONAI Label 0.8.4 + 3D Slicer 5.6.2". Never empty: a reference standard whose
  -- production tool is unrecorded is not reproducible.
  tool              text NOT NULL CHECK (tool <> ''),
  instructions_uri  text NOT NULL CHECK (instructions_uri <> ''),
  -- MOS-EVID-038 / MOS-TRAIN-104: {'model_output','other_readers','clinical_report'}.
  blinded_to        text[] NOT NULL DEFAULT '{}',
  created_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT annotation_readers_pk PRIMARY KEY (annotation_set_id, reader_id),
  CONSTRAINT annotation_readers_set_fk FOREIGN KEY (tenant_id, annotation_set_id)
    REFERENCES annotation_sets (tenant_id, id) ON DELETE RESTRICT
);


-- =====================================================================================
-- 9. annotations.  The reference standard, one row per case per series.
--
-- MOS-EVID-041: reference masks are stored in SOURCE geometry, and the `geometry` column
-- is constrained to the single value 'source' so that an annotation in model space is
-- unwritable rather than merely discouraged.
--
-- MOS-EVID-043: with two or more readers, inter-reader agreement MUST be computed and
-- persisted per case -- "a Dice of 0.82 against a reference whose own inter-reader Dice
-- is 0.84 is a different statement from the same 0.82 against an inter-reader Dice of
-- 0.97". The CHECK makes the omission unwritable.
--
-- MOS-TRAIN-098 owns `annotation_provenance`'s three values and this column mirrors all
-- three; the second CHECK is that requirement's rule that an unreviewed model output may
-- exist only as a `seed` record and never as a member of an `AnnotationSet`.
-- =====================================================================================
CREATE TABLE annotations (
  tenant_id            uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  annotation_set_id    uuid NOT NULL,
  case_key             text NOT NULL CHECK (case_key <> ''),
  series_instance_uid  dicom_uid NOT NULL,
  patient_key          text NOT NULL CHECK (patient_key ~ '^pk_[a-z2-7]{16}$'),
  study_instance_uid   dicom_uid NOT NULL,
  reference_kind       text NOT NULL
    CHECK (reference_kind IN ('mask','bounding_box','point','case_label','measurement')),
  reference_bucket     text NOT NULL CHECK (reference_bucket <> ''),
  reference_object_key text NOT NULL CHECK (reference_object_key <> ''),
  reference_digest     sha256_digest NOT NULL,
  geometry             text NOT NULL DEFAULT 'source' CHECK (geometry = 'source'),
  -- MOS-EVID-044: computed in source geometry by the SAME code path that computes model
  -- volumes. Two implementations MUST NOT exist.
  reference_volume_ml  double precision,
  -- One {reader_id, bucket, object_key, digest, volume_ml} per reader.
  per_reader           jsonb NOT NULL CHECK (jsonb_array_length(per_reader) >= 1),
  inter_reader         jsonb,
  annotation_provenance text NOT NULL CHECK (annotation_provenance IN (
    'de_novo','model_seeded_corrected','model_output_unreviewed')),
  -- MOS-TRAIN-099's eight-member block.
  seed                 jsonb,
  corpus_generation    smallint NOT NULL DEFAULT 0
    CHECK (corpus_generation BETWEEN 0 AND 1),
  annotated_at         timestamptz NOT NULL DEFAULT now(),
  created_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT annotations_pk
    PRIMARY KEY (annotation_set_id, case_key, series_instance_uid),
  CONSTRAINT annotations_set_fk FOREIGN KEY (tenant_id, annotation_set_id)
    REFERENCES annotation_sets (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT annotations_inter_reader
    CHECK (jsonb_array_length(per_reader) < 2 OR inter_reader IS NOT NULL),
  CONSTRAINT annotations_seed
    CHECK ((annotation_provenance = 'model_seeded_corrected') = (seed IS NOT NULL)),
  CONSTRAINT annotations_no_unreviewed
    CHECK (annotation_provenance <> 'model_output_unreviewed')
);

CREATE INDEX annotations_patient_idx
  ON annotations (tenant_id, annotation_set_id, patient_key);


-- =====================================================================================
-- 10. THE SEAL.  MOS-EVID-013.
--
-- "Sealing/freezing is enforced at the database level, not in application code: the
-- evidence tables listed as sealed MUST have UPDATE and DELETE revoked from the
-- application role for all columns except the explicitly mutable status columns named in
-- this chapter, and a BEFORE UPDATE trigger MUST raise on any attempt to modify a digest
-- column."
--
-- Two layers, and they catch different things:
--
--   * the GRANT (section 12) bounds `medicalos_app`, which is what the API and the
--     workers connect as;
--   * the TRIGGER binds every role including `medicalos_owner`, `medicalos_migrator`
--     through its membership, and a superuser psql session. That is the case that
--     actually destroys evidence, and a GRANT cannot reach it.
--
-- Section 12.12's sealed-column list for `dataset_versions` is "all but `erasure_state`,
-- `usable_for_new_runs`, `status`, `defect_reason`"; `updated_at` is excluded too because
-- `touch_updated_at()` writes it on exactly those permitted updates. For the other six
-- tables the list is "all", which is expressed as a blanket `forbid_mutation` on UPDATE
-- rather than as a column enumeration -- an enumeration would have to be extended by hand
-- every time a column is added, and the one that is forgotten is the hole.
-- =====================================================================================
-- `forbid_mutation()` from `schema.sql` reaches the same invariant, but its HINT names
-- `job_events` and SSE resume regardless of the table it fires on -- which is correct for
-- the table it was written for and actively misleading here: a developer who tried to
-- edit a sealed cohort would be pointed at MOS-EXEC-013. The shared function is NOT
-- edited (it is baseline DDL, and `medos.db.migrate` refuses an applied migration whose
-- file digest changed), so this is a second, evidence-specific one. REPORTED as a defect
-- in the shared helper rather than fixed in place.
CREATE FUNCTION forbid_evidence_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'relation % is sealed evidence: % is not permitted',
        TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'MOS05',
          HINT = 'MOS-EVID-013/MOS-EVID-014: a sealed evidence object MAY be marked '
                 'defective but MUST NOT be edited, and MUST NOT be deleted. A '
                 'correction is a NEW version with parent_version_id and derivation set '
                 '(MOS-EVID-022).';
END;
$$;
ALTER FUNCTION forbid_evidence_mutation() OWNER TO medicalos_owner;

CREATE TRIGGER dataset_versions_sealed BEFORE UPDATE ON dataset_versions
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'id','public_id','tenant_id','dataset_id','version','parent_version_id','derivation',
    'manifest_bucket','manifest_object_key','manifest_digest','manifest_line_count',
    'case_count','patient_count','study_count','series_count','instance_count',
    'source_description','acquisition_profile','deidentification_status','deid_policy_id',
    'uid_mapping_table_id','sealed_at','sealed_by','created_at');

-- MOS-EVID-014: "A sealed object MAY be marked defective but MUST NOT be edited."
-- Deleted is not one of the two.
CREATE TRIGGER dataset_versions_no_delete BEFORE DELETE ON dataset_versions
  FOR EACH ROW EXECUTE FUNCTION forbid_evidence_mutation();

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['dataset_cases','dataset_splits','dataset_split_members',
                           'annotation_sets','annotation_readers','annotations'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION forbid_evidence_mutation()', t || '_frozen', t);
  END LOOP;
END $$;


-- =====================================================================================
-- 11. ENABLE + FORCE ROW LEVEL SECURITY, and the policies.
--     MOS-EVID-012, MOS-SEC-072, MOS-STORE-223, MOS-STORE-224, MOS-STORE-229.
--
-- Identical in shape to 0002 section 10 and for the same reasons: ownership moves to
-- `medicalos_owner` first, FORCE binds that owner to the policy, and the policy is
-- written TO PUBLIC so that a session with no tenant bound hits `current_tenant_id()`
-- and gets 42704 rather than a silently empty result set.
--
-- MOS-EVID-012's other half -- "a DatasetVersion whose `deidentification_status` is not
-- `public_deidentified` MUST NOT be set to any visibility other than `tenant_private`" --
-- is a cross-table rule (`visibility` is on `datasets`, `deidentification_status` on
-- `dataset_versions`) and is therefore enforced at the seal, in
-- `medos/evidence/seal.py`, not by a CHECK. A CHECK cannot see another table's row, and a
-- trigger that queries one under RLS would be a second, weaker copy of the seal gate.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['datasets','dataset_versions','dataset_cases',
                           'dataset_splits','dataset_split_members',
                           'annotation_sets','annotation_readers','annotations'] LOOP
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
-- 12. Grants.  MOS-EVID-013, MOS-SEC-073, MOS-STORE-228, MOS-STORE-230.
--
-- `medicalos_app` owns nothing and is NOBYPASSRLS. The grant answers "may this role
-- perform this verb", the policy answers "on whose rows".
--
-- No DELETE anywhere in this migration. An evidence row is never deleted: a defective
-- DatasetVersion is MARKED (MOS-EVID-014), an erased subject sets `erasure_state`
-- (MOS-STORE-292), and a superseded split is a new split.
-- =====================================================================================
GRANT SELECT, INSERT, UPDATE ON datasets TO medicalos_app;

-- MOS-EVID-014's four mutable columns, and nothing else. A column-level grant rather
-- than a table-level one, so that the trigger in section 10 is the SECOND line of
-- defence and not the only one.
GRANT SELECT, INSERT ON dataset_versions TO medicalos_app;
GRANT UPDATE (status, defect_reason, erasure_state, usable_for_new_runs, updated_at)
  ON dataset_versions TO medicalos_app;

GRANT SELECT, INSERT ON
  dataset_cases, dataset_splits, dataset_split_members,
  annotation_sets, annotation_readers, annotations
  TO medicalos_app;

REVOKE UPDATE, DELETE ON
  dataset_cases, dataset_splits, dataset_split_members,
  annotation_sets, annotation_readers, annotations
  FROM medicalos_app;

REVOKE DELETE ON datasets, dataset_versions FROM medicalos_app;

GRANT SELECT ON
  datasets, dataset_versions, dataset_cases, dataset_splits, dataset_split_members,
  annotation_sets, annotation_readers, annotations
  TO medicalos_readonly;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 13. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================

-- MOS-SEC-077 / MOS-STORE-229, verbatim and re-run: a table with a `tenant_id` column and
-- row security not both enabled and forced MUST NOT exist. 0002 asserted this over the
-- tables that existed then; eight more exist now.
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

-- MOS-EVID-013: every table this migration declares sealed carries a BEFORE UPDATE
-- trigger. Asserted rather than assumed, because the failure mode of a forgotten trigger
-- is an editable evidence row that nothing complains about.
DO $$
DECLARE t text; n integer;
BEGIN
  FOREACH t IN ARRAY ARRAY['dataset_versions','dataset_cases','dataset_splits',
                           'dataset_split_members','annotation_sets',
                           'annotation_readers','annotations'] LOOP
    SELECT count(*) INTO n
      FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
     WHERE c.relname = t AND NOT g.tgisinternal AND (g.tgtype & 16) <> 0;  -- UPDATE
    IF n = 0 THEN
      RAISE EXCEPTION 'MOS-EVID-013: % is sealed but has no BEFORE UPDATE guard', t
        USING ERRCODE = '42501';
    END IF;
  END LOOP;
END $$;

-- MOS-EVID-028: no seed column on a split, now or ever. The requirement is a prohibition
-- and a prohibition with no test is a comment.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'dataset_splits'
       AND column_name IN ('seed','rng_seed','random_seed','split_seed')
  ) THEN
    RAISE EXCEPTION 'MOS-EVID-028: dataset_splits MUST NOT store a seed in place of a '
                    'manifest' USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-EVID-031 / MOS-EVID-033: `val` is not a partition. Asserted against the CHECK
-- text, which is where a later migration would relax it.
DO $$
DECLARE def text;
BEGIN
  SELECT pg_get_constraintdef(oid) INTO def
    FROM pg_constraint WHERE conname = 'dataset_split_members_partition_check';
  IF def IS NULL OR def LIKE '%''val''%' THEN
    RAISE EXCEPTION 'MOS-EVID-031: the partition vocabulary is train/tune/test/excluded '
                    'and `val` is not a permitted value (got: %)', coalesce(def, 'no CHECK')
      USING ERRCODE = '42501';
  END IF;
END $$;
