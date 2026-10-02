-- =====================================================================================
-- 0013_training.up.sql -- the training pipeline: runs, search, conversion, promotion.
--
-- docs/spec/15-delivery.md section 15.1.2 (0.3.0 contents) and chapter 17
-- `MOS-TRAIN-190`, which places in THIS release: "`TrainingRun`, `Orchestrator` port, one
-- driver", "MONAI Bundle as the native-mode artifact source form", "`ConversionRun`,
-- E1/E2/E3, tolerances", "Auto-configuration as the default backend, the fingerprint
-- freeze", "`ConfigurationSearch`, search provenance, budget bound, single nomination"
-- and "Approval dossier, the three-act promotion, rollback window".
--
-- SCOPE -- five tables, and two ALTERs against `deployments`:
--
--     training_runs             MOS-TRAIN-124   the reproducibility binding
--     configuration_searches    MOS-TRAIN-218   the search record and its single nomination
--     conversion_runs           MOS-TRAIN-156   checkpoint -> served plan, with E1/E2/E3
--     capability_seed_variance  MOS-TRAIN-127   sd of the primary metric across seeds
--     split_test_exposure       MOS-TRAIN-216   the `test` partition as a consumable
--
--     deployments  -- the UPDATE grant is narrowed to a column list (`MOS-TRAIN-180`)
--     deployments  -- `auto_promote` is refused by trigger (`MOS-TRAIN-184`)
--
-- ADDITIVE for every existing table's SHAPE: it creates tables, functions and triggers,
-- and ALTERs no column of any table 0001-0012 created. The two changes it does make to
-- `deployments` REMOVE authority (a table-level UPDATE grant becomes a column list) and
-- ADD a refusal; neither can make a previously-legal row illegal, so nothing already
-- applied changes behaviour except by refusing what `MOS-TRAIN-180`/`MOS-TRAIN-184`
-- require to be refused.
--
-- WHY THIS MIGRATION IS GENERATED FROM CHAPTER 17 AND NOT FROM CHAPTER 12
-- ----------------------------------------------------------------------
-- `MOS-STORE-201` says a migration MUST be generated from chapter 12's physical listing
-- and never from the owning chapter's semantic block. Chapter 12 has no listing for these
-- three records: section 12.12.1 stops at the six curation tables ("six of its records
-- had no storage here"), and section 12.17's entity map has no row for `TrainingRun`,
-- `ConversionRun` or `ConfigurationSearch`. `MOS-TRAIN-218` anticipates exactly this and
-- says "Chapter 12's conventions own the physical form" -- conventions, not a listing. So
-- every field name, type and value set below is chapter 17's, and every *convention* is
-- chapter 12's: plural table names, `uuid` primary keys with a `<prefix>_<ULID>` public
-- id, `text` + `CHECK` enums (`MOS-STORE-209`), `(bucket, object_key)` pairs,
-- `created_at`/`updated_at` (`MOS-STORE-210`), `UNIQUE (tenant_id, id)`, forced row
-- security (`MOS-STORE-229`) and the narrowed grants of `MOS-STORE-228`.
-- THE GAP IN SECTION 12.17 IS REPORTED, NOT PATCHED: chapter 12's acceptance check 1
-- ("`information_schema.tables` contains no table absent from section 12.17") goes red on
-- these five names until that table gains five rows. See this component's report.
--
-- FIVE DELIBERATE DIVERGENCES FROM CHAPTER 17'S FIELD TABLES, each at its point of use:
--   1. `training_runs.run_digest` is NOT NULL from insert, not merely on `SUCCEEDED`.
--      `MOS-TRAIN-124` calls it "digest over the full binding above", and the full
--      binding is known at submit -- a run that cannot be digested at submit is a run
--      whose inputs are not yet pinned, which is the one thing the record exists to
--      prevent. Section 2.
--   2. The `UNIQUE` on `run_digest` and `search_digest` is scoped to the tenant, on the
--      precedent 0006 and 0007 set: under FORCE RLS an unscoped unique index is a
--      cross-tenant existence oracle. Section 2.
--   3. `service_version_id` of `MOS-TRAIN-180` is spelled `subject_kind`/`subject_id`/
--      `subject_version` here, because that is how 0008 rendered chapter 12 section
--      12.9.2's deployment subject. One spelling, 0008's. Section 7.
--   4. `capability_seed_variance` is a table rather than a column on `Capability`
--      (`MOS-TRAIN-127` says "persisted on the Capability"): there is no `capabilities`
--      table in this deployment -- section 12.9.1's catalogue has not landed and 0006,
--      0008 and 0011 all spell a capability as `capability_id text`. Section 5.
--   5. `split_test_exposure` is a table `MOS-TRAIN-216` does not name; it says only that
--      the platform "MUST maintain, per `(capability_id, split_digest)`, a
--      `test_exposure_count` ... and MUST NOT allow it to be reset". A counter that
--      cannot be reset is a row with no DELETE grant and a monotonicity trigger, so it
--      is one. Section 6.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The `sha256_digest` domain, and this migration's append-only trigger function.
--
-- The domain is created guarded, on the 0005/0006/0011 precedent: several migrations in
-- this release block were authored in parallel and any of them may have created it. A
-- domain with no state is free to leave in place, and the down script does not drop it.
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


-- -------------------------------------------------------------------------------------
-- forbid_training_mutation() -- DELETE is not a verb any record here has.
--
-- A fourth cousin of `forbid_mutation()` (schema.sql), `forbid_evidence_mutation()`
-- (0006) and `forbid_curation_mutation()` (0011), for the reason 0011 gives: the HINT is
-- the whole value of the trigger, and each of the three existing ones points a developer
-- at the wrong subsystem when it fires here. The trigger is the layer a GRANT cannot
-- reach -- it binds `medicalos_owner`, `medicalos_migrator` through its membership, and a
-- superuser psql session. That is the case in which a candidate's provenance quietly
-- improves.
-- -------------------------------------------------------------------------------------
CREATE FUNCTION forbid_training_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'relation % is an append-only training record: % is not permitted',
        TG_TABLE_NAME, TG_OP
    USING ERRCODE = 'MOS05',
          HINT = 'MOS-TRAIN-124: a TrainingRun binds every input that can change the '
                 'artifact, and a deleted run is a ModelVersion with no producing run. '
                 'MOS-TRAIN-217: a non-nominated trial is RETAINED with its digest so a '
                 're-nomination is auditable rather than a re-run. MOS-TRAIN-216: the '
                 'test-exposure counter MUST NOT be resettable. Correct a run by '
                 'submitting another; its binding differs, so its run_digest differs.';
END;
$$;
ALTER FUNCTION forbid_training_mutation() OWNER TO medicalos_owner;


-- =====================================================================================
-- 2. training_runs -- chapter 17 section 17.7.2, `MOS-TRAIN-124`.
--
-- "It is the training-side analogue of `EvaluationRun` and it exists for the same reason:
-- it MUST bind every input that can change the artifact. A run missing any field below
-- MUST NOT reach `state = SUCCEEDED`, and its output MUST NOT be registrable as a
-- `ModelVersion`."
--
-- EVERY BINDING COLUMN IS `NOT NULL` AT INSERT, not merely at `SUCCEEDED`. 0007 made the
-- same call for `evaluation_runs` and the argument carries across unchanged: a row that
-- does not yet know which cohort it will fit against is not a pending run, it is a plan.
-- The one exception is `fingerprint_digest`, which an auto-configuring backend derives at
-- run start (`MOS-TRAIN-135`: "frozen at run start") -- so it is required from `RUNNING`
-- onward by the state CHECK below rather than at insert.
--
-- WHAT IS DELIBERATELY NOT HERE. No `val` partition and no fourth partition
-- (`MOS-TRAIN-214`); no `min_days_between_studies`, `study_level_split` or
-- `allow_same_patient` (`MOS-TRAIN-117`); no `test` anywhere in `fit_partition` or
-- `select_partition`, so `MOS-TRAIN-141`'s "MUST NOT be able to read the `test` partition
-- at all" is not expressible in the record rather than being policed in the driver.
-- =====================================================================================
CREATE TABLE training_runs (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- MOS-TRAIN-124: `tr_<ULID>`. Crockford base32, no I/L/O/U, as chapter 7 spells
  -- `evr_<ULID>` and chapter 6 spells `dep_<ULID>`.
  public_id text NOT NULL UNIQUE CHECK (public_id ~ '^tr_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id text NOT NULL CHECK (capability_id <> ''),

  -- ---- the cohort, the split, the reference standard -------------------------------
  -- ids AND digests, for the reason 0007 records: the id says which row, the digest says
  -- which CONTENT, and after a restore-from-backup those are not the same question.
  dataset_version_id uuid NOT NULL,
  dataset_version_digest sha256_digest NOT NULL,
  split_id uuid NOT NULL,
  split_digest sha256_digest NOT NULL,
  -- MOS-TRAIN-124's defaults, and MOS-TRAIN-141/MOS-TRAIN-214 as a CHECK: `test` is not
  -- a value either column can hold. An empty set is indistinguishable from "no such
  -- patient"; a column that cannot spell the partition is indistinguishable from nothing.
  fit_partition text NOT NULL DEFAULT 'train' CHECK (fit_partition = 'train'),
  select_partition text NOT NULL DEFAULT 'tune'
    CHECK (select_partition IN ('train','tune')),
  annotation_set_id uuid NOT NULL,
  annotation_digest sha256_digest NOT NULL,

  -- ---- preprocessing: chapter 4's spec, pinned by id, version AND digest ------------
  preprocessing_spec_id text NOT NULL CHECK (preprocessing_spec_id <> ''),
  preprocessing_spec_version integer NOT NULL CHECK (preprocessing_spec_version >= 1),
  preprocessing_spec_digest sha256_digest NOT NULL,

  -- ---- the code and the container --------------------------------------------------
  -- 40 hex is a SHA-1 object name, 64 a SHA-256 one; 0007 admits both and a repository
  -- that has migrated MUST NOT be unable to record its own commits.
  code_commit text NOT NULL CHECK (code_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  code_dirty boolean NOT NULL,
  image_digest sha256_digest NOT NULL,

  -- ---- the backend ------------------------------------------------------------------
  -- MOS-TRAIN-124's `{"kind":...,"version":...,"plan_digest":...}`. `auto3dseg` joins
  -- MOS-TRAIN-124's two literals because MOS-TRAIN-211 names it as one of the two
  -- permitted defaults for a `label` capability and MOS-TRAIN-223 gives it its own
  -- fingerprint mapping column-for-column.
  training_backend jsonb NOT NULL
    CHECK (training_backend->>'kind' IN ('monai_supervised','nnunet','auto3dseg')
           AND training_backend ? 'version'
           AND length(coalesce(training_backend->>'version','')) > 0),
  -- MOS-TRAIN-211: the rationale is required for a hand-configured run on a `label`
  -- capability, and "at least 20 characters". The DATABASE cannot know a capability's
  -- `io.output_kind` -- section 12.9.1's catalogue is not in this schema -- so the
  -- length floor and the backend pairing are here and the label rule is in
  -- `medos.training.runs`, which reads `medos.capabilities.REGISTRY`.
  backend_rationale text
    CHECK (backend_rationale IS NULL OR length(backend_rationale) >= 20),

  hyperparameters jsonb NOT NULL CHECK (jsonb_typeof(hyperparameters) = 'object'),
  hyperparameters_digest sha256_digest NOT NULL,

  -- ---- MOS-TRAIN-218/219: set when this run is a trial of a search ------------------
  search_id uuid,
  trial_index integer CHECK (trial_index IS NULL OR trial_index >= 0),
  nominated boolean NOT NULL DEFAULT false,
  -- MOS-TRAIN-222: a selection statistic, never a reported metric. It lives HERE and is
  -- forbidden in `evaluation_case_metrics`, `evaluation_runs.aggregate_metrics`,
  -- `capability_claims` and every UI surface that presents model performance.
  search_trial_score double precision,

  -- ---- reproducibility: MOS-TRAIN-124's four pinned jsonb blocks -------------------
  -- Each CHECK names every key the requirement's example carries. An absent key is not
  -- an open value; it is a question nobody answered, and MOS-TRAIN-126's whole claim --
  -- "the determinism settings actually used are recorded rather than asserted" -- is
  -- false the moment a block can be `{}`.
  seeds jsonb NOT NULL
    CHECK (seeds ?& array['python','numpy','torch','dataloader_worker_base']),
  determinism jsonb NOT NULL
    CHECK (determinism ?& array['torch_use_deterministic_algorithms','cudnn_benchmark',
                                'cublas_workspace_config','tf32_allowed']),
  hardware jsonb NOT NULL
    CHECK (hardware ?& array['gpu_model','gpu_count','driver','cuda','cudnn','nccl']),
  framework_versions jsonb NOT NULL
    CHECK (framework_versions ?& array['torch','monai','numpy','simpleitk']),

  -- MOS-TRAIN-223: the digest of the derived fingerprint document (`plans.json` for
  -- nnU-Net, `datastats.yaml` + `hyper_parameters.yaml` for Auto3DSeg). Retained for
  -- audit; MOS-TRAIN-225 forbids the document itself from reaching the serving image.
  fingerprint_digest sha256_digest,
  fingerprint_bucket text,
  fingerprint_object_key text,

  -- ---- lifecycle and output ---------------------------------------------------------
  state text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  failure_reason text,
  orchestrator_run_id text,               -- the `RunID` of MOS-TRAIN-122's port
  started_at timestamptz,
  finished_at timestamptz,
  runner text,

  -- MOS-TRAIN-129: a SUCCEEDED run's output artifact MUST be a MONAI Bundle.
  bundle_digest sha256_digest,
  bundle_bucket text,
  bundle_object_key text,
  -- MOS-TRAIN-138: set in the SAME transaction as the candidate's registration, "so that
  -- every candidate has exactly one producing run and every run has at most one
  -- candidate".
  candidate_model_version_id uuid REFERENCES artifacts(id) ON DELETE RESTRICT,

  -- Divergence 1: NOT NULL at insert. Divergence 2: the unique key is tenant-scoped.
  run_digest sha256_digest NOT NULL,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT training_runs_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT training_runs_run_digest_uk UNIQUE (tenant_id, run_digest),

  CONSTRAINT training_runs_split_fk FOREIGN KEY (tenant_id, split_id)
    REFERENCES dataset_splits (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT training_runs_dataset_version_fk FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT training_runs_annotation_set_fk FOREIGN KEY (tenant_id, annotation_set_id)
    REFERENCES annotation_sets (tenant_id, id) ON DELETE RESTRICT,

  -- MOS-TRAIN-125: "`code_dirty = true` MUST block `state = SUCCEEDED`". 0007 states the
  -- same rule for `evaluation_runs` under MOS-EVID-062, and the argument is the same one:
  -- a run from an uncommitted tree cannot be re-entered, and a candidate that cannot be
  -- re-entered cannot be diagnosed when it later fails in a subgroup nobody looked at.
  CONSTRAINT training_runs_clean_tree_to_succeed
    CHECK (state <> 'SUCCEEDED' OR code_dirty = false),

  -- MOS-TRAIN-124: `bundle_digest` non-null on SUCCEEDED; `started_at`, `finished_at`
  -- and `runner` "NOT NULL on terminal".
  CONSTRAINT training_runs_succeeded_shape
    CHECK (state <> 'SUCCEEDED'
           OR (bundle_digest IS NOT NULL AND runner IS NOT NULL
               AND started_at IS NOT NULL AND finished_at IS NOT NULL)),
  CONSTRAINT training_runs_terminal_shape
    CHECK (state NOT IN ('FAILED','CANCELLED') OR finished_at IS NOT NULL),
  CONSTRAINT training_runs_failure_reason
    CHECK ((state = 'FAILED') = (failure_reason IS NOT NULL)),
  CONSTRAINT training_runs_candidate_needs_success
    CHECK (candidate_model_version_id IS NULL OR state = 'SUCCEEDED'),

  -- MOS-TRAIN-135 / MOS-TRAIN-223: an auto-configuring backend freezes its plan at run
  -- start, so from RUNNING onward the fingerprint digest exists. A `monai_supervised`
  -- run derives nothing and has none.
  CONSTRAINT training_runs_fingerprint_frozen
    CHECK (state = 'PENDING'
           OR training_backend->>'kind' = 'monai_supervised'
           OR fingerprint_digest IS NOT NULL),
  CONSTRAINT training_runs_fingerprint_is_derived
    CHECK (training_backend->>'kind' <> 'monai_supervised'
           OR fingerprint_digest IS NULL),
  -- MOS-TRAIN-211: the rationale exists to answer "was this compared against the
  -- default, or did nobody run the default?", which is a question only a hand-configured
  -- run raises.
  CONSTRAINT training_runs_rationale_is_for_hand_configuration
    CHECK (training_backend->>'kind' = 'monai_supervised' OR backend_rationale IS NULL),

  -- MOS-TRAIN-219: a trial carries `search_id`, `trial_index` and `nominated` together.
  CONSTRAINT training_runs_trial_shape
    CHECK ((search_id IS NULL) = (trial_index IS NULL)),
  CONSTRAINT training_runs_nomination_needs_a_search
    CHECK (nominated = false OR search_id IS NOT NULL),
  CONSTRAINT training_runs_trial_score_needs_a_search
    CHECK (search_trial_score IS NULL OR search_id IS NOT NULL),

  CONSTRAINT training_runs_bundle_location
    CHECK ((bundle_object_key IS NULL) = (bundle_bucket IS NULL)),
  CONSTRAINT training_runs_fingerprint_location
    CHECK ((fingerprint_object_key IS NULL) = (fingerprint_bucket IS NULL))
);

-- MOS-TRAIN-219, verbatim: "`CREATE UNIQUE INDEX training_runs_one_nomination ON
-- training_runs (search_id) WHERE nominated;` MUST exist, so that MOS-TRAIN-216 is a
-- database property rather than a convention in the driver."
CREATE UNIQUE INDEX training_runs_one_nomination
  ON training_runs (search_id) WHERE nominated;

-- MOS-STORE-221: a tenant-filtered lookup leads with `tenant_id`.
CREATE INDEX training_runs_capability_idx
  ON training_runs (tenant_id, capability_id, state, created_at DESC);
CREATE INDEX training_runs_split_idx ON training_runs (tenant_id, split_digest);
CREATE INDEX training_runs_search_idx
  ON training_runs (search_id, trial_index) WHERE search_id IS NOT NULL;

COMMENT ON TABLE training_runs IS
  'MOS-TRAIN-124: the training-side analogue of EvaluationRun. It binds every input '
  'that can change the artifact -- cohort, split, annotation set, preprocessing spec, '
  'code commit, container digest, backend, hyperparameters, seeds, determinism, '
  'hardware and framework versions -- and a run missing any of them cannot SUCCEED.';
COMMENT ON COLUMN training_runs.search_trial_score IS
  'MOS-TRAIN-222: a SELECTION statistic. It MUST NOT be written into '
  'evaluation_case_metrics, evaluation_runs.aggregate_metrics, capability_claims, a '
  'ValidationReport aggregate, a ModelVersion field, or any UI surface presenting model '
  'performance. It MAY be rendered inside the dossier search block, labelled as such.';


-- =====================================================================================
-- 3. configuration_searches -- chapter 17 section 17.7.6, `MOS-TRAIN-218`.
--
-- The record exists because of one piece of arithmetic (`MOS-TRAIN-213`'s hazard note):
-- the maximum of N noisy estimates of the same quantity is biased upward, so a search
-- over 30 equal configurations is expected to report a winner about 0.021 Dice above the
-- truth -- "two Dice points, manufactured entirely out of noise, in the number everybody
-- will quote". Every column below exists so that a reader can see that arithmetic:
-- `trials_completed` says how many draws the maximum was over, `selection_margin` says
-- how far ahead the winner is, and `capability_seed_variance.sd` (section 5) says how far
-- apart two runs of the SAME code land.
--
-- `read_partitions` is the structural half of `MOS-TRAIN-214`: `test` is not expressible
-- in the record. The other half -- the cohort resolver returning 403 rather than an empty
-- set -- is `medos.training.cohort`.
-- =====================================================================================
CREATE TABLE configuration_searches (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  public_id text NOT NULL UNIQUE CHECK (public_id ~ '^cs_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id text NOT NULL CHECK (capability_id <> ''),

  -- MOS-TRAIN-217: a later nomination is a NEW row referencing the original, never a
  -- second nomination on the first. The self-reference is what makes that auditable.
  parent_search_id uuid,

  dataset_version_id uuid NOT NULL,
  split_id uuid NOT NULL,
  split_digest sha256_digest NOT NULL,
  annotation_digest sha256_digest NOT NULL,

  backend jsonb NOT NULL
    CHECK (backend ?& array['kind','version'] AND length(coalesce(backend->>'kind','')) > 0),
  fingerprint_digest sha256_digest NOT NULL,

  -- MOS-TRAIN-220: "a declarative document with a fixed grammar, digested under JCS. It
  -- MUST NOT be a Python callable, an expression string, a lambda or a template evaluated
  -- at search time" -- a space that is code has no digest that means anything.
  space jsonb NOT NULL CHECK (jsonb_typeof(space) = 'object'),
  space_digest sha256_digest NOT NULL,

  strategy text NOT NULL CHECK (strategy IN ('grid','random','adaptive')),
  seed bigint NOT NULL,

  -- MOS-TRAIN-214, verbatim: `text[] NOT NULL CHECK (read_partitions <@
  -- ARRAY['train','tune'])`. There is no fourth partition and `val` is not a value.
  read_partitions text[] NOT NULL DEFAULT ARRAY['train','tune']
    CHECK (read_partitions <@ ARRAY['train','tune']::text[]
           AND cardinality(read_partitions) >= 1),

  selection_metric text NOT NULL CHECK (selection_metric <> ''),
  selection_partition text NOT NULL CHECK (selection_partition IN ('tune','train')),
  -- MOS-TRAIN-215, verbatim.
  selection_folds integer[]
    CHECK ((selection_partition = 'train') = (selection_folds IS NOT NULL)),
  selection_rule text NOT NULL CHECK (length(selection_rule) >= 10),

  trials_planned integer NOT NULL CHECK (trials_planned >= 1),
  trials_completed integer NOT NULL DEFAULT 0 CHECK (trials_completed >= 0),
  trials_failed integer NOT NULL DEFAULT 0 CHECK (trials_failed >= 0),

  nominated_training_run_id uuid,
  runner_up_training_run_id uuid,
  selection_margin double precision,

  -- MOS-TRAIN-234: "an absent bound is a registration error, not an unlimited one." All
  -- five, or the row does not exist.
  budget jsonb NOT NULL
    CHECK (budget ?& array['max_trials','max_gpu_hours','max_wall_clock_hours',
                           'max_ensemble_members','max_footprint_bytes']),
  cost jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(cost) = 'object'),

  code_commit text NOT NULL CHECK (code_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  image_digest sha256_digest NOT NULL,

  state text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
  search_digest sha256_digest NOT NULL,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT configuration_searches_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT configuration_searches_digest_uk UNIQUE (tenant_id, search_digest),
  CONSTRAINT configuration_searches_parent_fk FOREIGN KEY (tenant_id, parent_search_id)
    REFERENCES configuration_searches (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT configuration_searches_split_fk FOREIGN KEY (tenant_id, split_id)
    REFERENCES dataset_splits (tenant_id, id) ON DELETE RESTRICT,

  -- MOS-TRAIN-218: `nominated_training_run_id`, `runner_up_training_run_id` "FK,
  -- non-null on `SUCCEEDED`". MOS-TRAIN-234 keeps the other branch legitimate: "a
  -- budget-terminated search that nominated none is a legitimate outcome", which is a
  -- FAILED or CANCELLED row, not a SUCCEEDED one with a null nomination.
  CONSTRAINT configuration_searches_succeeded_shape
    CHECK (state <> 'SUCCEEDED'
           OR (nominated_training_run_id IS NOT NULL
               AND runner_up_training_run_id IS NOT NULL
               AND selection_margin IS NOT NULL
               AND finished_at IS NOT NULL
               AND cost ? 'stop_reason')),
  CONSTRAINT configuration_searches_distinct_top_two
    CHECK (nominated_training_run_id IS NULL
           OR nominated_training_run_id <> runner_up_training_run_id),
  -- MOS-TRAIN-234: "The orchestrator MUST stop the search at the first bound reached."
  -- A completed count above `budget.max_trials` is a bound that was not honoured.
  CONSTRAINT configuration_searches_within_trial_budget
    CHECK (trials_completed + trials_failed <= (budget->>'max_trials')::integer),
  CONSTRAINT configuration_searches_stop_reason_vocabulary
    CHECK (NOT (cost ? 'stop_reason') OR cost->>'stop_reason' IN
           ('space_exhausted','max_trials','max_gpu_hours','max_wall_clock_hours',
            'cancelled','failed'))
);

CREATE INDEX configuration_searches_capability_idx
  ON configuration_searches (tenant_id, capability_id, state, created_at DESC);

-- The two run references are added AFTER `training_runs` exists, because the reference is
-- circular by design: a trial names its search and a search names its winner. One
-- direction has to be an ALTER, and this is the one whose absence is harmless while the
-- other's would let a trial exist with no search.
ALTER TABLE configuration_searches
  ADD CONSTRAINT configuration_searches_nominated_fk
    FOREIGN KEY (tenant_id, nominated_training_run_id)
    REFERENCES training_runs (tenant_id, id) ON DELETE RESTRICT,
  ADD CONSTRAINT configuration_searches_runner_up_fk
    FOREIGN KEY (tenant_id, runner_up_training_run_id)
    REFERENCES training_runs (tenant_id, id) ON DELETE RESTRICT;

ALTER TABLE training_runs
  ADD CONSTRAINT training_runs_search_fk FOREIGN KEY (tenant_id, search_id)
    REFERENCES configuration_searches (tenant_id, id) ON DELETE RESTRICT;

COMMENT ON TABLE configuration_searches IS
  'MOS-TRAIN-213: any procedure producing more than one trained artifact from one cohort '
  'and retaining a subset by a score computed on data -- whether or not the tool calls '
  'itself AutoML. MOS-TRAIN-216: exactly one trial may be nominated, enforced by '
  'training_runs_one_nomination.';


-- =====================================================================================
-- 4. conversion_runs -- chapter 17 section 17.9.3, `MOS-TRAIN-156`.
--
-- The trap this table exists for, in 17.9.1's words: "What was evaluated in 17.8 is a
-- PyTorch module running fp32 under a Python inferer inside a training container. What is
-- served is a serialised graph running under a Triton backend on a specific GPU
-- architecture, possibly at reduced precision, with a different kernel selection, a
-- different reduction order and a different memory layout. These are different numerical
-- artifacts that happen to share a weights file."
--
-- So a conversion is a NEW `ModelVersion` with its OWN `EvaluationRun` (`MOS-REG-102`,
-- `MOS-REG-103`, `MOS-EVID-064`) and the E1/E2/E3 block here is NOT that evidence --
-- `MOS-TRAIN-165`: "An E1/E2/E3 pass MUST NOT be reported, summarised, or displayed as
-- evidence of clinical equivalence. It is evidence that the conversion did not break the
-- graph."
-- =====================================================================================
CREATE TABLE conversion_runs (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  public_id text NOT NULL UNIQUE CHECK (public_id ~ '^cv_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

  -- MOS-REG-103: the `derived_from` of the output.
  source_model_version_id uuid NOT NULL REFERENCES artifacts(id) ON DELETE RESTRICT,
  target_model_version_id uuid REFERENCES artifacts(id) ON DELETE RESTRICT,

  target_format text NOT NULL
    CHECK (target_format IN ('torchscript','onnx','tensorrt_plan')),
  precision text NOT NULL CHECK (precision IN ('fp32','tf32','fp16','int8')),

  toolchain jsonb NOT NULL CHECK (jsonb_typeof(toolchain) = 'object'),
  -- MOS-TRAIN-167: "`built_for` MUST be read from the driver and runtime on the machine
  -- that performed the conversion, never taken from configuration or from a template."
  -- The database cannot tell an observed value from a configured one; what it can do is
  -- refuse a plan that declares none, so that Triton's load-time refusal
  -- (`MOS-OPS-070`) has something to compare against.
  built_for jsonb,

  -- MOS-TRAIN-157: int8 calibration draws from `tune`. "Calibrating on `test` is
  -- operating-point selection on the test set under another name."
  calibration_dataset_version_id uuid,
  calibration_partition text,

  -- MOS-TRAIN-159: at least 20 distinct patient_keys from the `tune` partition, frozen
  -- as a named subset with its own digest and reused UNCHANGED for every conversion of
  -- that model family, "so that two conversions are comparable to each other and a slow
  -- degradation across a series of conversions is visible".
  equivalence_cohort_digest sha256_digest NOT NULL,
  equivalence jsonb NOT NULL DEFAULT '{}'::jsonb
    CHECK (jsonb_typeof(equivalence) = 'object'),

  code_commit text NOT NULL CHECK (code_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  image_digest sha256_digest NOT NULL,

  state text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','RUNNING','SUCCEEDED','FAILED')),
  failure_reason text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT conversion_runs_tenant_id_uk UNIQUE (tenant_id, id),

  CONSTRAINT conversion_runs_built_for_for_plans
    CHECK (target_format <> 'tensorrt_plan'
           OR (built_for ?& array['cuda_compute_capability','tensorrt_version',
                                  'cuda_version'])),
  CONSTRAINT conversion_runs_int8_calibration
    CHECK ((precision = 'int8')
           = (calibration_dataset_version_id IS NOT NULL
              AND calibration_partition IS NOT NULL)),
  CONSTRAINT conversion_runs_calibration_partition
    CHECK (calibration_partition IS NULL OR calibration_partition = 'tune'),

  -- MOS-TRAIN-164: "A conversion failing any tolerance MUST NOT be registered." The
  -- structural half: a SUCCEEDED conversion has a target version and a recorded E1/E2/E3
  -- block; a failing one has neither and the remedy is a different conversion, never a
  -- relaxed tolerance on this one.
  CONSTRAINT conversion_runs_succeeded_shape
    CHECK (state <> 'SUCCEEDED'
           OR (target_model_version_id IS NOT NULL
               AND finished_at IS NOT NULL
               AND equivalence ?& array['E1','E2','E3','max_abs_logit_diff',
                                        'tolerances','n_cases','verdict']
               AND equivalence->>'verdict' = 'pass')),
  CONSTRAINT conversion_runs_not_registered_unless_succeeded
    CHECK (target_model_version_id IS NULL OR state = 'SUCCEEDED'),
  CONSTRAINT conversion_runs_failure_reason
    CHECK ((state = 'FAILED') = (failure_reason IS NOT NULL)),
  CONSTRAINT conversion_runs_source_is_not_target
    CHECK (target_model_version_id IS NULL
           OR target_model_version_id <> source_model_version_id)
);

CREATE INDEX conversion_runs_source_idx
  ON conversion_runs (tenant_id, source_model_version_id, created_at DESC);

COMMENT ON TABLE conversion_runs IS
  'MOS-TRAIN-156: conversion happens in a packaging job inside the pipeline, its output '
  'is signed, and Triton MUST NOT build an engine at load time (MOS-OPS-071). '
  'MOS-TRAIN-165: the equivalence block is evidence that the conversion did not break '
  'the graph, and is NEVER evidence of clinical equivalence.';
COMMENT ON COLUMN conversion_runs.equivalence IS
  'MOS-TRAIN-161: E1 max_abs_probability_diff, E2 post_threshold_dice, E3 '
  'volume_rel_diff, plus max_abs_logit_diff which MOS-REG-104 names and which MUST NOT '
  'be the gating quantity. MOS-TRAIN-163: E2 and E3 are per-case floors, never a mean.';


-- =====================================================================================
-- 5. capability_seed_variance -- chapter 17 `MOS-TRAIN-127`.  DIVERGENCE 4.
--
-- "Each capability MUST have a seed-variance characterisation recorded before its first
-- candidate is promoted: at least three `TrainingRun`s differing only in `seeds`, each
-- evaluated on the same `test` partition, with the observed standard deviation of the
-- primary metric persisted on the Capability."
--
-- `MOS-TRAIN-128` fixes what it is FOR, and it is not what it looks like: it is rendered
-- beside the declared non-inferiority margin d in the dossier, and it MUST NOT be used to
-- compute, adjust or justify d -- Chapter 7 forbids tuning d to make a candidate pass
-- (`MOS-EVID-087`). The dossier shows both so an approver can see, without doing
-- arithmetic, whether the gate they are about to rely on can distinguish a real change
-- from a re-run of the same code. No column here feeds any threshold anywhere.
-- =====================================================================================
CREATE TABLE capability_seed_variance (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id text NOT NULL CHECK (capability_id <> ''),

  metric text NOT NULL CHECK (metric <> ''),
  runs integer NOT NULL CHECK (runs >= 3),          -- "at least three"
  sd double precision NOT NULL CHECK (sd >= 0.0),
  training_run_ids uuid[] NOT NULL CHECK (cardinality(training_run_ids) >= 3),

  -- MOS-TRAIN-127: "It MUST be refreshed whenever `training_backend`,
  -- `hardware.gpu_count` or the hyperparameter set changes materially." Those three are
  -- recorded so that "is this characterisation still about the thing being promoted?" is
  -- answerable from the row rather than from memory.
  backend_kind text NOT NULL
    CHECK (backend_kind IN ('monai_supervised','nnunet','auto3dseg')),
  gpu_count integer NOT NULL CHECK (gpu_count >= 1),
  hyperparameters_digest sha256_digest NOT NULL,

  recorded_by uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT capability_seed_variance_tenant_id_uk UNIQUE (tenant_id, id)
);

-- One live characterisation per capability; a refreshed one supersedes rather than
-- overwrites, so the dossier of a past promotion still renders the figure it was decided
-- against.
CREATE UNIQUE INDEX capability_seed_variance_live_uk
  ON capability_seed_variance (tenant_id, capability_id) WHERE superseded_at IS NULL;

COMMENT ON TABLE capability_seed_variance IS
  'MOS-TRAIN-127. MOS-TRAIN-128: rendered beside the declared non-inferiority margin, '
  'and NEVER used to compute, adjust or justify it (MOS-EVID-087).';


-- =====================================================================================
-- 6. split_test_exposure -- chapter 17 `MOS-TRAIN-216`.  DIVERGENCE 5.
--
-- "The platform MUST maintain, per `(capability_id, split_digest)`, a
-- `test_exposure_count` -- the number of distinct `SUCCEEDED` `EvaluationRun`s on that
-- split's `test` partition -- MUST increment it on every such run, MUST render it in the
-- approval dossier, and MUST NOT allow it to be reset. A split's `test` partition is a
-- consumable; the counter is how a team finds out it has been spent."
--
-- "MUST NOT allow it to be reset" is three things and all three are here: no DELETE grant
-- (section 8), no UPDATE grant on any column but the counter and its timestamp, and the
-- monotonicity trigger below, which binds the owner and a superuser session too.
-- =====================================================================================
CREATE TABLE split_test_exposure (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id text NOT NULL CHECK (capability_id <> ''),
  split_digest sha256_digest NOT NULL,
  exposure_count integer NOT NULL DEFAULT 0 CHECK (exposure_count >= 0),
  first_exposed_at timestamptz,
  last_exposed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (tenant_id, capability_id, split_digest)
);

CREATE FUNCTION split_test_exposure_monotonic() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.exposure_count < OLD.exposure_count THEN
    RAISE EXCEPTION
      'test_exposure_count MUST NOT be reset (% -> %)',
      OLD.exposure_count, NEW.exposure_count
      USING ERRCODE = 'MOS05',
            HINT = 'MOS-TRAIN-216: a split''s test partition is a consumable and the '
                   'counter is how a team finds out it has been spent. Freeze a new '
                   'split; do not lower the number.';
  END IF;
  RETURN NEW;
END $$;
ALTER FUNCTION split_test_exposure_monotonic() OWNER TO medicalos_owner;

CREATE TRIGGER split_test_exposure_no_reset BEFORE UPDATE ON split_test_exposure
  FOR EACH ROW EXECUTE FUNCTION split_test_exposure_monotonic();
CREATE TRIGGER split_test_exposure_no_delete BEFORE DELETE ON split_test_exposure
  FOR EACH ROW EXECUTE FUNCTION forbid_training_mutation();
CREATE TRIGGER split_test_exposure_touch BEFORE UPDATE ON split_test_exposure
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER split_test_exposure_tenant_immutable BEFORE UPDATE ON split_test_exposure
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','capability_id',
    'split_digest','created_at');

COMMENT ON TABLE split_test_exposure IS
  'MOS-TRAIN-216: the number of distinct SUCCEEDED EvaluationRuns on a split''s test '
  'partition, per capability. Incremented, never reset, rendered in the dossier.';


-- =====================================================================================
-- 7. The seals, the state machines, and the append-only rule.
--
-- Two layers everywhere, on the precedent 0009 set for `validation_reports` and 0012 for
-- `artifacts`: a column-level GRANT that bounds `medicalos_app` (section 8), and a
-- trigger that also binds `medicalos_owner`, `medicalos_migrator` and a superuser psql
-- session. The GRANT is the routine defence; the trigger is the one that holds when
-- somebody is in `psql` at 2 a.m. fixing something.
-- =====================================================================================

-- -------------------------------------------------------------------------------------
-- training_runs: the binding is sealed at insert; the derived plan is sealed at start.
-- -------------------------------------------------------------------------------------
CREATE FUNCTION training_runs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  legal boolean;
BEGIN
  -- MOS-TRAIN-124: every input that can change the artifact. Changing one after the fact
  -- would mean the run's `run_digest` describes a binding the run did not have.
  IF (NEW.id, NEW.public_id, NEW.tenant_id, NEW.capability_id, NEW.dataset_version_id,
      NEW.dataset_version_digest, NEW.split_id, NEW.split_digest, NEW.fit_partition,
      NEW.select_partition, NEW.annotation_set_id, NEW.annotation_digest,
      NEW.preprocessing_spec_id, NEW.preprocessing_spec_version,
      NEW.preprocessing_spec_digest, NEW.code_commit, NEW.code_dirty, NEW.image_digest,
      NEW.hyperparameters, NEW.hyperparameters_digest, NEW.seeds, NEW.determinism,
      NEW.hardware, NEW.framework_versions, NEW.search_id, NEW.trial_index,
      NEW.backend_rationale, NEW.run_digest, NEW.created_at)
     IS DISTINCT FROM
     (OLD.id, OLD.public_id, OLD.tenant_id, OLD.capability_id, OLD.dataset_version_id,
      OLD.dataset_version_digest, OLD.split_id, OLD.split_digest, OLD.fit_partition,
      OLD.select_partition, OLD.annotation_set_id, OLD.annotation_digest,
      OLD.preprocessing_spec_id, OLD.preprocessing_spec_version,
      OLD.preprocessing_spec_digest, OLD.code_commit, OLD.code_dirty, OLD.image_digest,
      OLD.hyperparameters, OLD.hyperparameters_digest, OLD.seeds, OLD.determinism,
      OLD.hardware, OLD.framework_versions, OLD.search_id, OLD.trial_index,
      OLD.backend_rationale, OLD.run_digest, OLD.created_at)
  THEN
    RAISE EXCEPTION 'training_runs: the reproducibility binding is sealed at submit'
      USING ERRCODE = 'MOS05',
            HINT = 'MOS-TRAIN-124: a TrainingRun MUST bind every input that can change '
                   'the artifact. A re-run with a different input is a different run '
                   'with a different run_digest, not an edit of this one.';
  END IF;

  -- MOS-TRAIN-135 / MOS-TRAIN-223: the self-configured plan and its fingerprint are
  -- "frozen at run start". Before the run starts they may still be written; afterwards
  -- they are exactly as frozen, because MOS-TRAIN-225's whole argument is that a
  -- re-derivation is invisible -- byte-identical container, byte-identical weights, a
  -- materially different transform and no error anywhere.
  IF OLD.state <> 'PENDING' AND
     (NEW.training_backend, NEW.fingerprint_digest, NEW.fingerprint_bucket,
      NEW.fingerprint_object_key)
     IS DISTINCT FROM
     (OLD.training_backend, OLD.fingerprint_digest, OLD.fingerprint_bucket,
      OLD.fingerprint_object_key)
  THEN
    RAISE EXCEPTION 'training_runs: the derived configuration is frozen at run start'
      USING ERRCODE = 'MOS05',
            HINT = 'MOS-TRAIN-135: the self-configured plan MUST be frozen at run start '
                   'and recorded as training_backend.plan_digest. MOS-TRAIN-223: the '
                   'fingerprint document is digested and retained for audit.';
  END IF;

  -- MOS-TRAIN-124's `state` column, as a graph. A terminal state is terminal.
  legal := CASE OLD.state
             WHEN 'PENDING'   THEN NEW.state IN ('PENDING','RUNNING','FAILED','CANCELLED')
             WHEN 'RUNNING'   THEN NEW.state IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED')
             ELSE NEW.state = OLD.state
           END;
  IF NOT legal THEN
    RAISE EXCEPTION 'training_runs: % -> % is not a legal transition', OLD.state, NEW.state
      USING ERRCODE = 'MOS05',
            HINT = 'PENDING -> RUNNING -> SUCCEEDED | FAILED | CANCELLED. A terminal run '
                   'is not reopened; MOS-TRAIN-125 is why a FAILED run cannot be edited '
                   'into a SUCCEEDED one.';
  END IF;

  -- MOS-TRAIN-138: "The `TrainingRun.candidate_model_version_id` MUST be set in the same
  -- transaction, so that every candidate has exactly one producing run and every run has
  -- at most one candidate." Set once; never repointed.
  IF OLD.candidate_model_version_id IS NOT NULL
     AND NEW.candidate_model_version_id IS DISTINCT FROM OLD.candidate_model_version_id
  THEN
    RAISE EXCEPTION 'training_runs: candidate_model_version_id is set once'
      USING ERRCODE = 'MOS05',
            HINT = 'MOS-TRAIN-138: every run has at most one candidate. Repointing it '
                   'would give a ModelVersion a producing run it was not produced by.';
  END IF;

  RETURN NEW;
END $$;
ALTER FUNCTION training_runs_guard() OWNER TO medicalos_owner;

CREATE TRIGGER training_runs_sealed BEFORE UPDATE ON training_runs
  FOR EACH ROW EXECUTE FUNCTION training_runs_guard();
CREATE TRIGGER training_runs_touch BEFORE UPDATE ON training_runs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER training_runs_no_delete BEFORE DELETE ON training_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_training_mutation();

-- -------------------------------------------------------------------------------------
-- configuration_searches and conversion_runs: same shape, smaller sealed set.
-- -------------------------------------------------------------------------------------
CREATE TRIGGER configuration_searches_sealed BEFORE UPDATE ON configuration_searches
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('id','public_id','tenant_id',
    'capability_id','parent_search_id','dataset_version_id','split_id','split_digest',
    'annotation_digest','backend','fingerprint_digest','space','space_digest','strategy',
    'seed','read_partitions','selection_metric','selection_partition','selection_folds',
    'selection_rule','trials_planned','budget','code_commit','image_digest',
    'search_digest','created_at');
CREATE TRIGGER configuration_searches_touch BEFORE UPDATE ON configuration_searches
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER configuration_searches_no_delete BEFORE DELETE ON configuration_searches
  FOR EACH ROW EXECUTE FUNCTION forbid_training_mutation();

CREATE TRIGGER conversion_runs_sealed BEFORE UPDATE ON conversion_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('id','public_id','tenant_id',
    'source_model_version_id','target_format','precision','toolchain',
    'calibration_dataset_version_id','calibration_partition','equivalence_cohort_digest',
    'code_commit','image_digest','created_at');
CREATE TRIGGER conversion_runs_touch BEFORE UPDATE ON conversion_runs
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER conversion_runs_no_delete BEFORE DELETE ON conversion_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_training_mutation();

CREATE TRIGGER capability_seed_variance_sealed BEFORE UPDATE ON capability_seed_variance
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('id','tenant_id','capability_id',
    'metric','runs','sd','training_run_ids','backend_kind','gpu_count',
    'hyperparameters_digest','recorded_by','recorded_at','created_at');
CREATE TRIGGER capability_seed_variance_no_delete BEFORE DELETE
  ON capability_seed_variance
  FOR EACH ROW EXECUTE FUNCTION forbid_training_mutation();


-- =====================================================================================
-- 7b. deployments: MOS-TRAIN-180 and MOS-TRAIN-184.
--
-- MOS-TRAIN-180: "Promotion MUST mint a new `Deployment` row. It MUST NOT update
-- `service_version_id` or any model reference on a row whose `state` is `SERVING`. This
-- MUST be enforced structurally: `deployments.service_version_id` MUST be immutable after
-- insert, BY REVOKING `UPDATE` ON THE COLUMN FROM THE APPLICATION ROLE AND BY A `BEFORE
-- UPDATE` TRIGGER."
--
-- 0008 built the trigger half (`deployments_immutable_identity` covers `subject_kind`,
-- `subject_id` and `subject_version` -- divergence 3's spelling of the same reference) and
-- granted table-level `UPDATE`, which is the other half missing. This narrows the grant
-- to a column list. Nothing an application does today stops working: every column it
-- writes is still in the list, and the three it cannot write are the three the trigger
-- was already rejecting.
--
-- "A platform that can edit a serving row can change what is running without producing a
-- deployment record, and every provenance record written before that edit then names an
-- artifact that was not the one that ran -- an unfalsifiable condition that no later audit
-- can detect."
-- =====================================================================================
REVOKE UPDATE ON deployments FROM medicalos_app;
GRANT UPDATE (role, state, traffic_permille, clinical_use_mode, residency, pin_range,
              operating_point_id, promotion_policy, verification_ref, review_mode,
              review_sla_hours, emit_verified_sr_on_accept, state_reason,
              acceptance_run_id, validation_report_id, approved_by, approved_at,
              activated_at, retired_at, updated_at)
  ON deployments TO medicalos_app;

-- -------------------------------------------------------------------------------------
-- MOS-TRAIN-184. `MOS-REG-080` already forbids `auto_promote` while
-- `clinical_use_mode = clinical`. This chapter adds the second, wider rule:
--
--   "`auto_promote: true` MUST be refused for ANY deployment in a `(tenant,
--    environment)` where the same `capability_id` has any `clinical_use_mode: clinical`
--    deployment. A research canary configured to auto-promote, sharing a capability slot
--    with a clinical deployment, is one `clinical_use_mode` edit away from the automated
--    production-to-serving path this chapter exists to forbid."
--
-- Both directions are checked, because the hazard is symmetric: setting `auto_promote` on
-- a row beside a clinical one, and turning a row clinical beside an auto-promoting one,
-- reach the same state.
-- -------------------------------------------------------------------------------------
CREATE FUNCTION assert_no_auto_promote_beside_clinical() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  wants_auto boolean := coalesce((NEW.promotion_policy->>'auto_promote')::boolean, false);
  clash uuid;
BEGIN
  IF wants_auto AND NEW.clinical_use_mode = 'clinical' THEN
    RAISE EXCEPTION 'promotion_policy.auto_promote MUST be false for a clinical deployment'
      USING ERRCODE = '23514',
            HINT = 'MOS-REG-080 and MOS-TRAIN-184. MOS-TRAIN-182: the gate says the '
                   'candidate is PERMITTED; it does not say it should go NOW, and the '
                   'difference between those two statements is where every remaining '
                   'piece of clinical judgement lives.';
  END IF;

  IF wants_auto THEN
    SELECT d.id INTO clash FROM deployments d
     WHERE d.tenant_id = NEW.tenant_id
       AND d.environment = NEW.environment
       AND d.capability_id = NEW.capability_id
       AND d.clinical_use_mode = 'clinical'
       AND d.state <> 'RETIRED'
       AND d.id <> NEW.id
     LIMIT 1;
    IF clash IS NOT NULL THEN
      RAISE EXCEPTION
        'auto_promote is refused: capability % already has a clinical deployment in %/%',
        NEW.capability_id, NEW.tenant_id, NEW.environment
        USING ERRCODE = '23514',
              HINT = 'MOS-TRAIN-184: a research canary configured to auto-promote, '
                     'sharing a capability slot with a clinical deployment, is one '
                     'clinical_use_mode edit away from the automated production-to-'
                     'serving path chapter 17 exists to forbid.';
    END IF;
  END IF;

  IF NEW.clinical_use_mode = 'clinical' THEN
    SELECT d.id INTO clash FROM deployments d
     WHERE d.tenant_id = NEW.tenant_id
       AND d.environment = NEW.environment
       AND d.capability_id = NEW.capability_id
       AND d.state <> 'RETIRED'
       AND d.id <> NEW.id
       AND coalesce((d.promotion_policy->>'auto_promote')::boolean, false)
     LIMIT 1;
    IF clash IS NOT NULL THEN
      RAISE EXCEPTION
        'a deployment for capability % in %/% is set to auto_promote',
        NEW.capability_id, NEW.tenant_id, NEW.environment
        USING ERRCODE = '23514',
              HINT = 'MOS-TRAIN-184: clear promotion_policy.auto_promote on the sibling '
                     'deployment before promoting this one to clinical use.';
    END IF;
  END IF;

  RETURN NEW;
END $$;
ALTER FUNCTION assert_no_auto_promote_beside_clinical() OWNER TO medicalos_owner;

CREATE TRIGGER deployments_no_auto_promote
  BEFORE INSERT OR UPDATE OF promotion_policy, clinical_use_mode ON deployments
  FOR EACH ROW EXECUTE FUNCTION assert_no_auto_promote_beside_clinical();


-- =====================================================================================
-- 8. Row-level security.  MOS-STORE-229, MOS-SEC-077, MOS-TRAIN-071.
--
-- Every table here is tenant-owned. `MOS-TRAIN-071` forbids pooling studies across
-- tenants into one batch and the same reasoning reaches a training run: a run is bound to
-- one tenant's sealed cohort, one tenant's split and one tenant's annotation set, all
-- three of which already carry FORCE RLS. A run without it would be the one row in the
-- chain that any tenant could read.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['training_runs','configuration_searches','conversion_runs',
                           'capability_seed_variance','split_test_exposure'] LOOP
    EXECUTE format('ALTER TABLE %I OWNER TO medicalos_owner', t);
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY %I ON %I '
      'USING (tenant_id = current_tenant_id()) '
      'WITH CHECK (tenant_id = current_tenant_id())', t || '_tenant_isolation', t);
  END LOOP;
END $$;

-- `split_test_exposure` gets its tenant-immutability trigger in section 6; the other four
-- get theirs here, matching 0011's loop.
CREATE TRIGGER training_runs_tenant_immutable BEFORE UPDATE ON training_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id');
CREATE TRIGGER configuration_searches_tenant_immutable
  BEFORE UPDATE ON configuration_searches
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id');
CREATE TRIGGER conversion_runs_tenant_immutable BEFORE UPDATE ON conversion_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id');
CREATE TRIGGER capability_seed_variance_tenant_immutable
  BEFORE UPDATE ON capability_seed_variance
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id');


-- =====================================================================================
-- 9. Grants.  MOS-STORE-228, MOS-STORE-230, MOS-STORE-325.
--
-- NO DELETE ANYWHERE, for the reason 0011 gives and one more: `MOS-TRAIN-217` requires
-- the non-nominated trials' checkpoints to be retained with their digests recorded, "so
-- that a re-nomination is auditable rather than a re-run". A deletable trial is a search
-- whose losing arms can be made to disappear after the fact, which is selection on `test`
-- with the evidence removed.
--
-- The UPDATE lists are narrow on purpose. Everything a run learns AFTER submit is in
-- them; everything it was submitted WITH is not.
-- =====================================================================================
GRANT SELECT, INSERT ON training_runs TO medicalos_app;
GRANT UPDATE (state, failure_reason, orchestrator_run_id, started_at, finished_at,
              runner, bundle_digest, bundle_bucket, bundle_object_key,
              candidate_model_version_id, training_backend, fingerprint_digest,
              fingerprint_bucket, fingerprint_object_key, nominated, search_trial_score,
              updated_at)
  ON training_runs TO medicalos_app;

GRANT SELECT, INSERT ON configuration_searches TO medicalos_app;
GRANT UPDATE (state, trials_completed, trials_failed, nominated_training_run_id,
              runner_up_training_run_id, selection_margin, cost, started_at, finished_at,
              updated_at)
  ON configuration_searches TO medicalos_app;

GRANT SELECT, INSERT ON conversion_runs TO medicalos_app;
GRANT UPDATE (state, failure_reason, built_for, equivalence, target_model_version_id,
              started_at, finished_at, updated_at)
  ON conversion_runs TO medicalos_app;

GRANT SELECT, INSERT ON capability_seed_variance TO medicalos_app;
GRANT UPDATE (superseded_at) ON capability_seed_variance TO medicalos_app;

-- MOS-TRAIN-216: increment only. `exposure_count` is in the list because it goes up;
-- the primary key is not, because a reset is a row that names a different split.
GRANT SELECT, INSERT ON split_test_exposure TO medicalos_app;
GRANT UPDATE (exposure_count, first_exposed_at, last_exposed_at, updated_at)
  ON split_test_exposure TO medicalos_app;

REVOKE DELETE ON training_runs, configuration_searches, conversion_runs,
  capability_seed_variance, split_test_exposure FROM medicalos_app;

GRANT SELECT ON training_runs, configuration_searches, conversion_runs,
  capability_seed_variance, split_test_exposure TO medicalos_readonly;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 10. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================
DO $$
DECLARE
  n integer;
  bad text;
BEGIN
  -- Five tables, five policies, FORCE on all of them.
  SELECT count(*) INTO n FROM pg_class c
    JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = 'public' AND c.relforcerowsecurity
     AND c.relname IN ('training_runs','configuration_searches','conversion_runs',
                       'capability_seed_variance','split_test_exposure');
  IF n <> 5 THEN
    RAISE EXCEPTION 'expected FORCE ROW LEVEL SECURITY on 5 tables, found %', n;
  END IF;

  -- No DELETE grant on any of the five, for any role but the owner.
  SELECT string_agg(DISTINCT table_name, ', ') INTO bad
    FROM information_schema.role_table_grants
   WHERE table_schema = 'public' AND privilege_type = 'DELETE'
     AND grantee <> 'medicalos_owner'
     AND table_name IN ('training_runs','configuration_searches','conversion_runs',
                        'capability_seed_variance','split_test_exposure');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-TRAIN-217: DELETE is granted on %', bad;
  END IF;

  -- MOS-TRAIN-219, verbatim: the one-nomination index exists and is unique and partial.
  SELECT count(*) INTO n FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
   WHERE c.relname = 'training_runs_one_nomination'
     AND i.indisunique AND i.indpred IS NOT NULL;
  IF n <> 1 THEN
    RAISE EXCEPTION 'MOS-TRAIN-219: training_runs_one_nomination is not a partial '
                    'unique index';
  END IF;

  -- MOS-TRAIN-180: the application role no longer holds UPDATE on the deployment
  -- subject. This is the half 0008 left open and it is the point of section 7b.
  SELECT string_agg(column_name, ', ') INTO bad
    FROM information_schema.column_privileges
   WHERE table_schema = 'public' AND table_name = 'deployments'
     AND privilege_type = 'UPDATE' AND grantee = 'medicalos_app'
     AND column_name IN ('subject_kind','subject_id','subject_version');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-TRAIN-180: medicalos_app still holds UPDATE on deployments(%)',
                    bad;
  END IF;

  -- ... and still holds it on the columns a promotion legitimately writes, so that the
  -- narrowing did not break the gate it was not aimed at.
  SELECT count(*) INTO n
    FROM information_schema.column_privileges
   WHERE table_schema = 'public' AND table_name = 'deployments'
     AND privilege_type = 'UPDATE' AND grantee = 'medicalos_app'
     AND column_name IN ('role','state','clinical_use_mode','approved_by','approved_at',
                         'validation_report_id','acceptance_run_id','activated_at');
  IF n <> 8 THEN
    RAISE EXCEPTION 'the deployments UPDATE grant lost a column a promotion needs '
                    '(found % of 8)', n;
  END IF;

  -- MOS-TRAIN-214: `test` is not expressible in a search's read_partitions.
  BEGIN
    PERFORM 1 FROM configuration_searches
     WHERE read_partitions <@ ARRAY['train','tune','test']::text[] AND false;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'read_partitions is not a text[]';
  END;
END $$;
