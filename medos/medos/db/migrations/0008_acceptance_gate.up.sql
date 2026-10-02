-- =====================================================================================
-- 0008_acceptance_gate.up.sql -- AcceptanceCriteria, the tenant binding, Deployment,
--                                and the deployment gate's decision record.
--
-- docs/spec/15-delivery.md section 15.2.5 (weeks 6-9, tag 0.2.0): "`AcceptanceCriteria`
-- on the `Capability`. The deployment gate rewritten as an executable rule with a paired
-- non-inferiority regression". Section 15.1.3 puts "the deployment gate" in 0.2.0's
-- **Tier A** -- MUST NOT be cut under any circumstance.
--
-- SCOPE. Four of the sixteen tables of chapter 12 section 12.12, plus `deployments`,
-- which chapter 6 section 6.8 owns and chapter 12 section 12.9.2 gives the physical form
-- of. The gate needs all five and no other migration in this release block declares any
-- of them (0010_safety.up.sql says so explicitly in its own scope note).
--
--     acceptance_criteria         7.8     the CLINICAL bar, owned by the Capability
--     tenant_acceptance_bindings  7.8.3   the tenant's concrete cohort binding
--     deployments                 6.8     the SOLE owner of liveness   MOS-STORE-262
--     deployment_gate_decisions   7.9.3   every invocation, whatever the outcome
--
-- WHY `deployments` IS HERE AND NOT IN A REGISTRY MIGRATION
-- ---------------------------------------------------------
-- MOS-EVID-092 is the requirement this release has to be able to demonstrate: "On `FAIL`
-- or `INDETERMINATE` the incumbent ServiceVersion MUST remain live and serving; the gate
-- MUST NOT take the capability out of service to block a candidate." That sentence is
-- only checkable against a table that records which version is live, and MOS-REG-072
-- names exactly one: "`Deployment` is the only entity that determines whether a version
-- receives work." A gate with no deployment row to leave standing is a function returning
-- a string.
--
-- NOT in this migration, on purpose: `validation_reports` (the report/signing half of
-- 15.2.5, landing beside this one), `capabilities`, `service_versions`, `model_versions`,
-- `users`. The last four are chapter 6's registry and chapter 12 section 12.6's identity
-- tables, and section 6.1's phasing table puts the unified registry in 0.3.0. Stubbing
-- any of them to satisfy a foreign key would be a second answer to a question this
-- migration does not own -- the same reasoning 0007 and 0010 applied to the same tables.
--
-- ADDITIVE, like 0002-0007 and 0010: it creates tables and alters none of the existing
-- ones. `schema.sql` remains the baseline (MOS-STORE-214).
--
-- FOUR DELIBERATE DIVERGENCES FROM CHAPTERS 12 AND 7, each at the point of use:
--   1. `deployments` identifies its subject as `(subject_kind, subject_id,
--      subject_version)` rather than `service_version_id uuid REFERENCES
--      service_versions(id)`                                            -- section 4.
--   2. `deployment_gate_decisions.candidate_report_id` is NULLABLE, constrained so it may
--      be null only on a FAIL. MOS-EVID-091 says NOT NULL and chapter 7 acceptance check
--      23 requires a persisted FAIL for a promotion with NO report    -- section 5.
--   3. `acceptance_criteria` carries `margin_rationale` as its own column. MOS-EVID-087
--      requires the field and section 12.12 lists no column for it      -- section 2.
--   4. `deployments.public_id` uses the prefix `dep_`, which no chapter allocates; chapter
--      7 section 7.2 allocates prefixes for evidence entities only      -- section 4.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. Preconditions. This migration is meaningless without 0006's sealed cohort tables
--    and 0007's runs, and a missing one must fail HERE rather than as a confusing
--    foreign-key error two hundred lines down.
-- =====================================================================================
DO $$
DECLARE missing text;
BEGIN
  SELECT string_agg(t, ', ' ORDER BY t) INTO missing
    FROM unnest(ARRAY['tenants','dataset_versions','dataset_splits','annotation_sets',
                      'evaluation_runs','evaluation_case_metrics']) AS t
   WHERE to_regclass('public.' || t) IS NULL;
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION '0008 requires 0002, 0006 and 0007; missing: %', missing
      USING ERRCODE = '42P01';
  END IF;
END $$;


-- =====================================================================================
-- 2. acceptance_criteria.  Chapter 7 section 7.8.  SEALED per version.
--
-- OWNERSHIP IS THE POINT OF THE TABLE, AND IT IS NOT A FILING PREFERENCE.
--
--   MOS-EVID-075  "The **clinical** bar belongs to the `Capability` and is versioned with
--                 it. The **engineering** bar (p95 latency, GPU memory ceiling,
--                 ResultBundle schema validity, throughput) belongs to the
--                 `ModelVersion`/`ServiceVersion`."
--   MOS-REG-049   "`AcceptanceCriteria` (the clinical bar: sensitivity >= x on cohort y)
--                 belongs to the **Capability** ... The registry MUST reject a
--                 `service_version` that declares clinical acceptance thresholds in its
--                 own manifest."
--   MOS-STORE-297 "Engineering bars -- p95 latency, GPU ceiling, schema validity -- live
--                 in `model_versions.engineering_bars` and MUST NOT appear here."
--
-- A bar carried by the artifact is a bar the artifact's publisher sets, so every new
-- version ships with the bar it happens to pass and the gate is a tautology. Carried by
-- the Capability, the bar OUTLIVES the thing being measured, which is the only
-- arrangement in which "did this version meet the bar" has a possible answer of no.
-- `medos.evidence.acceptance.reject_clinical_acceptance_in_manifest` is the other half of
-- MOS-REG-049, and `test_deployment_gate.py` exercises both directions.
--
-- DUAL SCOPE (MOS-STORE-220, which names `acceptance_criteria` in its own list): a
-- Capability is platform-global in 0.1-0.2 -- `medos.capabilities.REGISTRY` -- so its
-- criteria carry `tenant_id IS NULL` and every tenant reads them. The read policy admits
-- NULL; the write policy does not, so the application can read the platform catalogue and
-- can never create a platform-global row. `UNIQUE NULLS NOT DISTINCT` is section 12.12's
-- own spelling and is what makes two platform-global versions of one capability collide
-- (a plain UNIQUE treats every NULL as distinct and would admit them).
--
-- `capability_id` is `text`: it is the `medos.capabilities.REGISTRY` key (CONTRACT.md
-- section 6), exactly as `results.capability_id` and `evaluation_runs.capability_id` are
-- spelled. There is no `capabilities` table before 0.3.0 and this migration does not
-- invent one.
--
-- DIVERGENCE 3. `margin_rationale`. MOS-EVID-087: delta "MUST be justified in a
-- `margin_rationale` free-text field on the criteria document ... and MUST NOT be tuned to
-- make a specific candidate pass". Section 12.12 lists no column. It lives in `spec` as
-- well (the document is stored verbatim), and is lifted to a column so that the
-- constraint below can be a CHECK rather than a code path: a regression criterion with no
-- justification is unwritable.
-- =====================================================================================
CREATE TABLE acceptance_criteria (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- Chapter 7 section 7.2 gives this entity the identity `(capability_id, version)` and
  -- no ULID prefix, so there is no `public_id` here -- unlike every other table in the
  -- evidence plane. The natural key IS the external identity.
  tenant_id      uuid REFERENCES tenants(id) ON DELETE RESTRICT,   -- NULL = platform-wide
  capability_id  text NOT NULL CHECK (capability_id <> ''),
  version        integer NOT NULL CHECK (version >= 1),
  supersedes     integer CHECK (supersedes IS NULL OR supersedes >= 1),

  -- The declarative document of MOS-EVID-076 and section 7.8.1, stored VERBATIM. Never
  -- an expression string, never a template, never reduced to a flat metric list
  -- (MOS-STORE-297): a sensitivity figure without its operating point is not a
  -- measurement (MOS-EVID-055), and a bar without `bound` and `min_cases` is not a gate
  -- (MOS-EVID-077).
  spec           jsonb NOT NULL,

  -- MOS-EVID-087, divergence 3 above.
  margin_rationale text,

  -- The cohort the criteria were authored against. Section 12.12 carries these four and
  -- fixes `partition` at exactly one value: the bar is a claim about held-out data, and a
  -- criteria document bound to `train` or `tune` measures the fit, not the performance.
  cohort_dataset_version_id uuid,
  split_id                  uuid,
  partition      text NOT NULL DEFAULT 'test' CHECK (partition = 'test'),
  annotation_set_id         uuid,

  effective_from timestamptz,
  authored_by    text NOT NULL CHECK (authored_by <> ''),
  -- The only two mutable columns (section 12.12: "all but `approved_by`, `approved_at`").
  approved_by    uuid,
  approved_at    timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),

  -- Section 12.12 verbatim.
  CONSTRAINT acceptance_criteria_uk
    UNIQUE NULLS NOT DISTINCT (tenant_id, capability_id, version),
  CONSTRAINT acceptance_criteria_tenant_id_uk UNIQUE NULLS NOT DISTINCT (tenant_id, id),
  -- Section 12.12 verbatim: at least one criterion. A document that gates nothing is not
  -- a bar, and an empty `absolute` with an empty `regression` passes everything.
  CONSTRAINT acceptance_criteria_nonempty CHECK (
    jsonb_array_length(coalesce(spec->'absolute', '[]'::jsonb))
    + jsonb_array_length(coalesce(spec->'regression', '[]'::jsonb)) >= 1),
  -- MOS-EVID-087. A regression criterion carries a clinical judgement about how much loss
  -- is tolerable; unjustified, the requirement's other half -- that delta MUST NOT be
  -- tuned to make a specific candidate pass -- is unauditable.
  CONSTRAINT acceptance_criteria_margin_rationale CHECK (
    jsonb_array_length(coalesce(spec->'regression', '[]'::jsonb)) = 0
    OR (margin_rationale IS NOT NULL AND margin_rationale <> '')),
  CONSTRAINT acceptance_criteria_supersedes_lower CHECK (
    supersedes IS NULL OR supersedes < version),
  CONSTRAINT acceptance_criteria_approval_pair CHECK (
    (approved_by IS NULL) = (approved_at IS NULL))
);

CREATE INDEX acceptance_criteria_capability_idx
  ON acceptance_criteria (capability_id, version DESC);

CREATE TRIGGER acceptance_criteria_touch BEFORE UPDATE ON acceptance_criteria
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Section 12.12's sealed-column list for this table is "all but `approved_by`,
-- `approved_at`". `updated_at` is excluded too, because `touch_updated_at()` writes it on
-- exactly the permitted update. MOS-EVID-087's "a change to delta MUST bump
-- `AcceptanceCriteria.version`" is this trigger: `spec` cannot be edited, so a new delta
-- is a new row.
CREATE TRIGGER acceptance_criteria_sealed BEFORE UPDATE ON acceptance_criteria
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'id','tenant_id','capability_id','version','supersedes','spec','margin_rationale',
    'cohort_dataset_version_id','split_id','partition','annotation_set_id',
    'effective_from','authored_by','created_at');

CREATE TRIGGER acceptance_criteria_no_delete BEFORE DELETE ON acceptance_criteria
  FOR EACH ROW EXECUTE FUNCTION forbid_evidence_mutation();

COMMENT ON TABLE acceptance_criteria IS
  'Chapter 7 section 7.8: the CLINICAL bar, owned by the Capability and versioned with it '
  '(MOS-EVID-075, MOS-REG-049). Engineering bars -- latency, GPU ceiling, schema validity '
  '-- belong to the version and MUST NOT appear here (MOS-STORE-297).';


-- =====================================================================================
-- 3. tenant_acceptance_bindings.  Chapter 7 section 7.8.3.  MUTABLE, deliberately.
--
-- MOS-EVID-080: "The Capability owns the criteria. A tenant MUST bind them to a concrete
-- cohort before they can be evaluated. The binding is one row per (tenant, capability)."
-- MOS-STORE-297a: "It is the only row in the evidence plane that is deliberately mutable,
-- because a site rebinds as its cohort grows."
--
-- MOS-EVID-082 is enforced here as a TRIGGER and not a CHECK, because it is a cross-table
-- rule: `Dataset.purpose` lives on `datasets` and the binding names a `dataset_versions`
-- row. "A tenant MUST NOT bind a `DatasetVersion` whose `Dataset.purpose` is `training`
-- or `tuning` to an acceptance binding" -- measuring a model on the data it was fitted to
-- is the single most common way a published number becomes meaningless, and a rule that
-- lives only in application code is one forgotten call site away from nothing.
--
-- MOS-EVID-081 (`threshold_overrides` may only TIGHTEN) is NOT enforced here: deciding
-- whether an override tightens requires parsing the criteria document and comparing in
-- the direction of each criterion's `op`, which is a grammar walk, not a constraint. It
-- lives in `medos.evidence.acceptance.apply_threshold_overrides`, is applied on the write
-- path, and chapter 7 acceptance check 17 tests both directions.
-- =====================================================================================
CREATE TABLE tenant_acceptance_bindings (
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id      text NOT NULL CHECK (capability_id <> ''),
  criteria_version   integer NOT NULL CHECK (criteria_version >= 1),
  dataset_version_id uuid NOT NULL,
  split_id           uuid NOT NULL,
  -- "the vocabulary of 7.4.1" -- and `test` is the only member of it an acceptance
  -- binding may name, for the reason `acceptance_criteria.partition` gives.
  partition          text NOT NULL CHECK (partition = 'test'),
  annotation_set_id  uuid NOT NULL,
  threshold_overrides jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(threshold_overrides) = 'object'),
  bound_by           uuid NOT NULL,
  bound_at           timestamptz NOT NULL DEFAULT now(),
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT tenant_acceptance_bindings_pk PRIMARY KEY (tenant_id, capability_id),
  -- MOS-STORE-217: composite foreign keys, so a cross-tenant reference is unwritable even
  -- with row-level security off.
  CONSTRAINT tenant_acceptance_bindings_dsv_fk
    FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT tenant_acceptance_bindings_split_fk
    FOREIGN KEY (tenant_id, split_id)
    REFERENCES dataset_splits (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT tenant_acceptance_bindings_ann_fk
    FOREIGN KEY (tenant_id, annotation_set_id)
    REFERENCES annotation_sets (tenant_id, id) ON DELETE RESTRICT
);

CREATE TRIGGER tenant_acceptance_bindings_touch BEFORE UPDATE ON tenant_acceptance_bindings
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER tenant_acceptance_bindings_tenant_immutable
  BEFORE UPDATE ON tenant_acceptance_bindings
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','capability_id');

-- MOS-EVID-082, in the database.
CREATE FUNCTION forbid_training_cohort_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE p text;
BEGIN
  SELECT d.purpose INTO p
    FROM dataset_versions dv JOIN datasets d ON d.id = dv.dataset_id
   WHERE dv.id = NEW.dataset_version_id AND dv.tenant_id = NEW.tenant_id;
  IF p IS NULL THEN
    RAISE EXCEPTION 'dataset version % has no dataset in this tenant',
          NEW.dataset_version_id USING ERRCODE = '23503';
  END IF;
  IF p IN ('training','tuning') THEN
    RAISE EXCEPTION 'MOS-EVID-082: dataset purpose % MUST NOT be bound for acceptance', p
      USING ERRCODE = 'MOS05',
            HINT = 'Measuring a model on the cohort it was fitted to is not evidence. '
                   'Bind a DatasetVersion whose Dataset.purpose is evaluation, '
                   'acceptance or monitoring (MOS-EVID-026).';
  END IF;
  RETURN NEW;
END;
$$;
ALTER FUNCTION forbid_training_cohort_binding() OWNER TO medicalos_owner;

CREATE TRIGGER tenant_acceptance_bindings_purpose
  BEFORE INSERT OR UPDATE ON tenant_acceptance_bindings
  FOR EACH ROW EXECUTE FUNCTION forbid_training_cohort_binding();

COMMENT ON TABLE tenant_acceptance_bindings IS
  'Chapter 7 section 7.8.3: the tenant''s concrete cohort binding for a capability''s '
  'criteria, one live row per (tenant, capability) (MOS-EVID-080). The only deliberately '
  'mutable row in the evidence plane (MOS-STORE-297a).';


-- =====================================================================================
-- 4. deployments.  Chapter 6 section 6.8, physical form chapter 12 section 12.9.2.
--
-- MOS-STORE-262: "`deployments` is the sole owner of liveness." MOS-REG-072:
-- "`ModelVersion.lifecycle_status` and `ServiceVersion.lifecycle_status` MUST NOT be
-- consulted to answer 'is this live'."
--
-- MOS-REG-073: `role` and `state` are ORTHOGONAL and both are required. `role` says what
-- traffic the row should get; `state` says whether it may get any. MOS-REG-074: at most
-- one `role = ACTIVE, state = SERVING` per `(tenant, environment, capability)`, enforced
-- by the partial unique index -- "two live actives is a configuration error, not a
-- load-balancing strategy". That index is also what makes MOS-EVID-092 demonstrable: a
-- refused promotion leaves the incumbent's row untouched, and the index proves no second
-- one took its place.
--
-- DIVERGENCE 1, and the reason. Section 12.9.2 writes
--     service_version_id uuid NOT NULL REFERENCES service_versions(id)
-- This migration writes `(subject_kind, subject_id, subject_version)`, all text. Three
-- reasons, in order of weight:
--
--   * There is no `service_versions` table and there MUST NOT be one here: section 6.1's
--     phasing table puts the unified registry in 0.3.0, and 0007 and 0010 both declined
--     to stub it for the same reason. A `uuid` column whose referent does not exist is a
--     foreign key to nothing -- strictly worse than a text identifier that means
--     something, because it LOOKS resolvable.
--   * A `ModelVersion` is a legitimate gate subject. `validation_reports.subject_kind`
--     and `capability_claims.subject_kind` both carry `service_version|model_version`;
--     MOS-EVID-064 makes a backend conversion a new ModelVersion needing its own run; and
--     the 0.2.0 gate check of section 15.1.2 is stated over "a deliberately regressed
--     `ModelVersion`". A single `service_version_id` cannot name one.
--   * The gate compares the deployment's subject against the ValidationReport's subject
--     (section 7.9.3 step 0, `subject_mismatch`). Section 7.12.1 spells the report's
--     subject `{kind, id, version}` with `id` a service identifier ("pulmoai.effusion")
--     and `version` a semver -- and the report is the PORTABLE artifact, verifiable on a
--     machine that has never seen this database (MOS-EVID-122). Two spellings of the same
--     fact is how a comparison silently becomes always-false.
--
-- When the artifact registry lands, `subject_id` gains a companion uuid foreign key. The
-- text identity stays, because the exported report carries it.
--
-- DIVERGENCE 4. `public_id` prefix `dep_`. Chapter 7 section 7.2 allocates prefixes for
-- evidence entities and Deployment is chapter 6's; no chapter allocates one. Defined in
-- this module only, per CONTRACT.md section 0, and REPORTED.
--
-- The four columns chapter 9 needs -- `review_mode`, `review_sla_hours`,
-- `emit_verified_sr_on_accept`, and the `clinical_use_mode` gate -- are declared here
-- because section 12.9.2 declares them here, and 0010_safety.up.sql's scope note says it
-- deliberately did not. `medos/safety/policy.py` resolves them through a port today; this
-- table is what that port's second driver reads.
-- =====================================================================================
CREATE TABLE deployments (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  public_id          text NOT NULL UNIQUE
    CHECK (public_id ~ '^dep_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  environment        text NOT NULL CHECK (environment IN ('dev','staging','production')),
  capability_id      text NOT NULL CHECK (capability_id <> ''),

  -- ---- the subject (divergence 1) ---------------------------------------------------
  subject_kind       text NOT NULL
    CHECK (subject_kind IN ('service_version','model_version')),
  subject_id         text NOT NULL CHECK (subject_id <> ''),
  subject_version    text NOT NULL CHECK (subject_version <> ''),

  -- ---- MOS-REG-073: orthogonal, both required --------------------------------------
  role               text NOT NULL CHECK (role IN ('ACTIVE','CANARY','SHADOW','STANDBY')),
  state              text NOT NULL DEFAULT 'PENDING' CHECK (state IN
                       ('PENDING','VERIFYING','SERVING','SUSPENDED','DRAINING','RETIRED')),
  -- MOS-REG-079: an absolute share of 1000 held by the CANARY row alone. Never a
  -- percentage and never a weight that must sum across rows.
  traffic_permille   integer NOT NULL DEFAULT 0
    CHECK (traffic_permille BETWEEN 0 AND 1000),

  -- MOS-SAFE-033: the default is `research_only`, and there is no tenant-wide,
  -- service-wide or environment-wide override. MOS-REG-083: the same signed version is
  -- deployable as `research_only` in one environment and `clinical` in another with no
  -- repackaging.
  clinical_use_mode  text NOT NULL DEFAULT 'research_only'
    CHECK (clinical_use_mode IN ('research_only','clinical')),

  -- Two residency classes, not three (section 12.9.2's own comment; chapter 13 section
  -- 13.10.3 defines exactly `resident` and `on_demand`).
  residency          text NOT NULL DEFAULT 'on_demand'
    CHECK (residency IN ('resident','on_demand')),
  pin_range          text NOT NULL DEFAULT '*',
  operating_point_id text,
  promotion_policy   jsonb,
  verification_ref   text,

  -- Chapter 9's review columns. MOS-SAFE-061 reserves `mandatory_pre_publication` and
  -- forbids it in 0.1-0.4, which is a CHECK and not a comment.
  review_mode        text NOT NULL DEFAULT 'optional' CHECK (review_mode IN
                       ('off','optional','mandatory_post_publication',
                        'mandatory_pre_publication')),
  review_sla_hours   integer CHECK (review_sla_hours IS NULL OR review_sla_hours > 0),
  emit_verified_sr_on_accept boolean NOT NULL DEFAULT false,

  state_reason       text,
  acceptance_run_id  uuid,
  validation_report_id uuid,
  approved_by        uuid,
  approved_at        timestamptz,
  activated_at       timestamptz,
  retired_at         timestamptz,
  created_by         uuid NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT deployments_tenant_id_uk UNIQUE (tenant_id, id),
  -- Section 12.9.2's `UNIQUE (tenant_id, environment, capability_id, service_version_id)`
  -- with the subject spelled as divergence 1 spells it.
  CONSTRAINT deployments_uk UNIQUE
    (tenant_id, environment, capability_id, subject_kind, subject_id, subject_version),
  CONSTRAINT deployments_serving_verified
    CHECK (state <> 'SERVING' OR verification_ref IS NOT NULL),          -- MOS-REG-075
  CONSTRAINT deployments_suspend_reason
    CHECK (state <> 'SUSPENDED' OR state_reason IS NOT NULL),
  CONSTRAINT deployments_review_mode_reserved
    CHECK (review_mode <> 'mandatory_pre_publication'),                  -- MOS-SAFE-061
  -- MOS-STORE-261: "a database-enforced clinical gate ... and the application cannot
  -- bypass it because the database checks it on every write." This is the structural half
  -- of MOS-EVID-090; `medos.evidence.deployment.promote` is the evidential half, and
  -- neither substitutes for the other.
  CONSTRAINT deployments_clinical_gate CHECK (
    clinical_use_mode = 'research_only'
    OR (approved_by IS NOT NULL AND approved_at IS NOT NULL
        AND acceptance_run_id IS NOT NULL AND validation_report_id IS NOT NULL)),
  CONSTRAINT deployments_acceptance_run_fk
    FOREIGN KEY (tenant_id, acceptance_run_id)
    REFERENCES evaluation_runs (tenant_id, id) ON DELETE RESTRICT
);

-- MOS-REG-074, and the index that makes MOS-EVID-092 checkable.
CREATE UNIQUE INDEX deployments_one_active_per_slot
  ON deployments (tenant_id, environment, capability_id)
  WHERE role = 'ACTIVE' AND state = 'SERVING';

-- MOS-STORE-221: a tenant-filtered lookup leads with `tenant_id`.
CREATE INDEX deployments_slot_idx
  ON deployments (tenant_id, environment, capability_id, role, state);

CREATE TRIGGER deployments_touch BEFORE UPDATE ON deployments
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TRIGGER deployments_immutable_identity BEFORE UPDATE ON deployments
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'id','public_id','tenant_id','environment','capability_id',
    'subject_kind','subject_id','subject_version','created_by','created_at');

COMMENT ON TABLE deployments IS
  'Chapter 6 section 6.8: the SOLE owner of liveness (MOS-REG-072, MOS-STORE-262). '
  '`role` says what traffic a row should get, `state` whether it may get any; both are '
  'required and they are orthogonal (MOS-REG-073).';


-- =====================================================================================
-- 5. deployment_gate_decisions.  Chapter 7 section 7.9.3.  APPEND-ONLY.
--
-- MOS-EVID-091: "Every invocation MUST be persisted, whatever the outcome."
-- MOS-STORE-302b: "Every gate invocation is persisted ... including the `SKIPPED`
-- criteria, so that 'why was this deployment allowed' is answerable from rows."
--
-- `verdict` is THREE-VALUED and MUST stay so. MOS-EVID-083: "`INDETERMINATE` arises only
-- from insufficient cases or a threshold mismatch, and it MUST be reported as its own
-- state, never folded into `FAIL`, because the two require different actions -- one needs
-- more data, the other needs a different model."
--
-- DIVERGENCE 2. `candidate_report_id` is NULLABLE. MOS-EVID-091's field table says
-- `NOT NULL`. Chapter 7 acceptance check 23 says: "Attempt a Deployment transition to
-- `clinical_use_mode: clinical` for a ServiceVersion with no ValidationReport. Expect
-- refusal and a `deployment_gate_decisions` row with verdict `FAIL`, reason
-- `signature_invalid` or `no_report`." Those two cannot both hold: a NOT NULL column
-- cannot record a decision about an absent report, so obeying the field table means the
-- refusal is unrecorded -- and an unrecorded refusal is exactly what MOS-EVID-091 exists
-- to prevent. The column is nullable and constrained so that NULL is reachable ONLY on a
-- FAIL. REPORTED as a specification defect rather than resolved silently in either
-- direction.
--
-- No foreign key to `validation_reports`: that table is declared by the report/signing
-- migration landing beside this one. Section 9 adds the constraint if the table is
-- already present, and the note there says what to do if it is not.
-- =====================================================================================
CREATE TABLE deployment_gate_decisions (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- Chapter 7 section 7.2: `gd_<ULID>`.
  public_id           text NOT NULL UNIQUE
    CHECK (public_id ~ '^gd_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  deployment_id       uuid NOT NULL,
  capability_id       text NOT NULL CHECK (capability_id <> ''),
  criteria_version    integer NOT NULL CHECK (criteria_version >= 1),
  candidate_report_id uuid,                    -- divergence 2
  incumbent_report_id uuid,                    -- null on first deployment
  verdict             text NOT NULL CHECK (verdict IN ('PASS','FAIL','INDETERMINATE')),
  -- One entry per criterion, including SKIPPED (MOS-EVID-113). An array, so that a
  -- decision naming no criterion is unwritable.
  criterion_results   jsonb NOT NULL
    CHECK (jsonb_typeof(criterion_results) = 'array'
           AND jsonb_array_length(criterion_results) >= 1),
  -- What the deployment was being moved TO. Not in MOS-EVID-091's field list, and needed:
  -- MOS-EVID-090 gives `research_only` and `clinical` DIFFERENT consequences for the same
  -- verdict -- one may proceed on FAIL, the other may not -- so a decision row that does
  -- not say which mode was requested cannot answer "why was this deployment allowed".
  requested_clinical_use_mode text NOT NULL
    CHECK (requested_clinical_use_mode IN ('research_only','clinical')),
  -- Did the deployment actually move? MOS-EVID-092 is about what happened to the
  -- INCUMBENT, and this column plus the incumbent's untouched row is the pair that
  -- answers it from rows alone.
  applied             boolean NOT NULL DEFAULT false,
  decided_at          timestamptz NOT NULL DEFAULT now(),
  decided_by          uuid NOT NULL,
  -- MOS-EVID-093: null unless an INDETERMINATE was overridden by a holder of
  -- `deployment.gate.override`, with a written rationale and a named approver.
  override            jsonb,
  created_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT deployment_gate_decisions_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT deployment_gate_decisions_deployment_fk
    FOREIGN KEY (tenant_id, deployment_id)
    REFERENCES deployments (tenant_id, id) ON DELETE RESTRICT,
  -- Divergence 2: NULL is reachable only on a refusal.
  CONSTRAINT deployment_gate_decisions_report_required CHECK (
    candidate_report_id IS NOT NULL OR verdict = 'FAIL'),
  -- MOS-EVID-093, in the database: "A `FAIL` MUST NOT be overridable in `clinical` mode
  -- by any role." Written as a CHECK rather than left to the service layer, because the
  -- one code path that skips the check is the one that matters.
  CONSTRAINT deployment_gate_decisions_no_override_of_fail CHECK (
    override IS NULL OR verdict = 'INDETERMINATE'),
  -- An override with no rationale and no named approver is an override nobody can audit.
  CONSTRAINT deployment_gate_decisions_override_shape CHECK (
    override IS NULL
    OR (jsonb_typeof(override) = 'object'
        AND coalesce(override->>'rationale', '') <> ''
        AND coalesce(override->>'approver', '') <> '')),
  -- MOS-EVID-090: `clinical` MUST refuse on FAIL or INDETERMINATE. An applied clinical
  -- promotion therefore has verdict PASS, or an overridden INDETERMINATE and nothing else.
  CONSTRAINT deployment_gate_decisions_clinical_refusal CHECK (
    applied = false
    OR requested_clinical_use_mode = 'research_only'
    OR verdict = 'PASS'
    OR (verdict = 'INDETERMINATE' AND override IS NOT NULL))
);

CREATE INDEX deployment_gate_decisions_deployment_idx
  ON deployment_gate_decisions (tenant_id, deployment_id, decided_at DESC);

-- Section 12.12: sealed columns "all". A blanket BEFORE UPDATE OR DELETE rather than a
-- column enumeration, on 0006 section 10's reasoning: an enumeration has to be extended
-- by hand and the column that is forgotten is the hole.
CREATE TRIGGER deployment_gate_decisions_frozen
  BEFORE UPDATE OR DELETE ON deployment_gate_decisions
  FOR EACH ROW EXECUTE FUNCTION forbid_evidence_mutation();

COMMENT ON TABLE deployment_gate_decisions IS
  'Chapter 7 section 7.9.3: every gate invocation, whatever the outcome (MOS-EVID-091). '
  'Append-only, three-valued verdict, one criterion_results entry per criterion including '
  'SKIPPED, so "why was this deployment allowed" is answerable from rows (MOS-STORE-302b).';


-- =====================================================================================
-- 6. ENABLE + FORCE ROW LEVEL SECURITY, and the policies.
--     MOS-SEC-072, MOS-STORE-223, MOS-STORE-224, MOS-STORE-229.
--
-- Identical in shape to 0002 section 10 and 0006 section 11. `acceptance_criteria` takes
-- the MOS-STORE-220 DUAL-SCOPE variant of section 12.5: the read policy admits
-- `tenant_id IS NULL`, the write policy does not, so the application can read the
-- platform catalogue and can never create a platform-global row.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['tenant_acceptance_bindings','deployments',
                           'deployment_gate_decisions'] LOOP
    EXECUTE format('ALTER TABLE %I OWNER TO medicalos_owner', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I '
      'USING (tenant_id = current_tenant_id()) '
      'WITH CHECK (tenant_id = current_tenant_id())', t || '_tenant_isolation', t);
  END LOOP;
END $$;

ALTER TABLE acceptance_criteria OWNER TO medicalos_owner;
ALTER TABLE acceptance_criteria ENABLE ROW LEVEL SECURITY;
ALTER TABLE acceptance_criteria FORCE  ROW LEVEL SECURITY;

-- ONE policy, not the read/write PAIR section 12.5's dual-scope variant prints, and the
-- difference is a real improvement rather than a shortcut.
--
-- Section 12.5 shows `service_versions_read FOR SELECT` beside `service_versions_write
-- FOR INSERT`. That pair leaves UPDATE and DELETE governed by NO policy at all, which
-- under `FORCE ROW LEVEL SECURITY` denies them outright -- fine for a catalogue nobody
-- updates, wrong here, because MOS-EVID-076's row has two mutable columns (`approved_by`,
-- `approved_at`) that a tenant MUST be able to write on its own rows.
--
-- A single `FOR ALL` policy with an ASYMMETRIC `USING`/`WITH CHECK` gives exactly the
-- semantics MOS-STORE-220 asks for, and gives them on all four verbs:
--
--   USING       admits `tenant_id IS NULL`  -> every tenant READS the platform catalogue.
--   WITH CHECK  does not                    -> no tenant CREATES a platform-global row,
--                                             and no tenant UPDATEs one either, because
--                                             PostgreSQL applies WITH CHECK to the NEW
--                                             row and a platform-global row's NEW
--                                             `tenant_id` is still NULL.
--
-- It also keeps the invariant `tests/integration/test_tenancy.py` has asserted since
-- weeks 3-5 -- every policy is named `*_tenant_isolation` and every `qual` names
-- `current_tenant_id()` (MOS-STORE-225) -- which the read/write pair breaks twice over:
-- a `FOR INSERT` policy has no `qual` at all, so an escape-hatch scan over `pg_policies`
-- cannot see what it permits.
CREATE POLICY acceptance_criteria_tenant_isolation ON acceptance_criteria
  FOR ALL
  USING (tenant_id IS NULL OR tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());


-- =====================================================================================
-- 7. Grants.  MOS-SEC-073, MOS-STORE-228, MOS-STORE-230.
--
-- `medicalos_app` owns nothing and is NOBYPASSRLS. No DELETE anywhere: a gate decision is
-- never deleted (it is the record that the gate ran), a criteria version is superseded
-- rather than removed, and a deployment is RETIRED rather than dropped -- MOS-REG-074's
-- state machine ends at `RETIRED`, not at absence, because a retired deployment is what a
-- past Result's provenance points at.
-- =====================================================================================
GRANT SELECT, INSERT ON acceptance_criteria TO medicalos_app;
-- The two mutable columns of section 12.12's sealed-column list, and nothing else. A
-- column-level grant so the trigger in section 2 is the SECOND line of defence.
GRANT UPDATE (approved_by, approved_at, updated_at) ON acceptance_criteria TO medicalos_app;

GRANT SELECT, INSERT, UPDATE ON tenant_acceptance_bindings TO medicalos_app;
GRANT SELECT, INSERT, UPDATE ON deployments TO medicalos_app;
GRANT SELECT, INSERT ON deployment_gate_decisions TO medicalos_app;

REVOKE UPDATE, DELETE ON deployment_gate_decisions FROM medicalos_app;
REVOKE DELETE ON acceptance_criteria, tenant_acceptance_bindings, deployments
  FROM medicalos_app;

GRANT SELECT ON acceptance_criteria, tenant_acceptance_bindings, deployments,
                deployment_gate_decisions
  TO medicalos_readonly;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 8. The three foreign keys this migration deliberately does NOT declare.
--
-- `deployments.validation_report_id`, `deployment_gate_decisions.candidate_report_id` and
-- `.incumbent_report_id` all point at `validation_reports`, which belongs to the
-- report/signing half of 15.2.5 and is authored in parallel with this migration. They are
-- bare `uuid` columns with no FK -- exactly as `api_keys.created_by` has been since 0004
-- and as `result_reviews.reviewer_user_id` is in 0010, for the same reason: a stub table
-- to satisfy a constraint is a second answer to a question this migration does not own.
--
-- THE MIGRATION THAT DECLARES `validation_reports` MUST ADD ALL THREE. Stated here rather
-- than guarded with a `to_regclass` check, because a constraint that silently never gets
-- created is indistinguishable from one that was never wanted, and a conditional one
-- makes the schema depend on migration ORDER rather than on migration CONTENT.
--
--   ALTER TABLE deployments ADD CONSTRAINT deployments_report_fk
--     FOREIGN KEY (tenant_id, validation_report_id)
--     REFERENCES validation_reports (tenant_id, id) ON DELETE RESTRICT;
--   ALTER TABLE deployment_gate_decisions ADD CONSTRAINT deployment_gate_decisions_candidate_fk
--     FOREIGN KEY (tenant_id, candidate_report_id)
--     REFERENCES validation_reports (tenant_id, id) ON DELETE RESTRICT;
--   ALTER TABLE deployment_gate_decisions ADD CONSTRAINT deployment_gate_decisions_incumbent_fk
--     FOREIGN KEY (tenant_id, incumbent_report_id)
--     REFERENCES validation_reports (tenant_id, id) ON DELETE RESTRICT;
--
-- `deployments.acceptance_run_id` is NOT in that list: `evaluation_runs` exists from 0007,
-- which runs before this migration, so that one is a real composite foreign key declared
-- in section 4. MOS-STORE-261 calls the clinical gate database-enforced, and a clinical
-- deployment whose `acceptance_run_id` points at nothing would not be.
-- =====================================================================================


-- =====================================================================================
-- 9. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================

-- MOS-SEC-077 / MOS-STORE-229, re-run over everything that now exists.
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

-- MOS-EVID-083: the verdict is three-valued. A two-valued pass/fail column "cannot
-- represent this chapter's verdict and MUST NOT be substituted" (section 7.12.1), and the
-- way that substitution happens is a later migration "simplifying" the CHECK. Asserted.
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n
    FROM pg_constraint
   WHERE conrelid = 'deployment_gate_decisions'::regclass
     AND contype = 'c'
     AND pg_get_constraintdef(oid) LIKE '%INDETERMINATE%';
  IF n = 0 THEN
    RAISE EXCEPTION 'MOS-EVID-083: deployment_gate_decisions.verdict must admit '
                    'INDETERMINATE as its own state' USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-STORE-297: engineering bars MUST NOT live on `acceptance_criteria`. The failure
-- mode is a well-meaning later column called `p95_latency_ms`, which moves the
-- engineering bar onto the clinical document and makes a latency regression a clinical
-- refusal. A prohibition with no test is a comment.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(column_name, ', ' ORDER BY column_name) INTO bad
    FROM information_schema.columns
   WHERE table_schema = 'public' AND table_name = 'acceptance_criteria'
     AND (column_name LIKE '%latency%' OR column_name LIKE '%gpu%'
          OR column_name LIKE '%throughput%' OR column_name LIKE '%memory%');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-EVID-075/MOS-STORE-297: engineering bars belong to the version, '
                    'not to the Capability''s criteria; found: %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-REG-074: the partial unique index exists and is partial. A non-partial index here
-- would forbid blue/green entirely; an absent one would admit two live actives.
DO $$
DECLARE pred text; is_unique boolean;
BEGIN
  SELECT pg_get_expr(i.indpred, i.indrelid), i.indisunique
    INTO pred, is_unique
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
   WHERE c.relname = 'deployments_one_active_per_slot';
  IF pred IS NULL OR is_unique IS NOT TRUE
     OR pred NOT LIKE '%ACTIVE%' OR pred NOT LIKE '%SERVING%' THEN
    RAISE EXCEPTION 'MOS-REG-074: deployments_one_active_per_slot must be a PARTIAL '
                    'UNIQUE index over role = ACTIVE AND state = SERVING (found: %)', pred
      USING ERRCODE = '42501';
  END IF;
END $$;
