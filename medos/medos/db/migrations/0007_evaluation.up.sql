-- =====================================================================================
-- 0007_evaluation.up.sql -- the measuring half of the evidence plane.
--
-- docs/spec/15-delivery.md section 15.2.5 (weeks 6-9, tag 0.2.0): "evaluation runs with
-- **per-case metrics persisted, not only aggregates**. One evaluation implementation,
-- not two." The 0.2.0 gate of 15.1.2 names `per-case-metrics` as its own check, so the
-- per-case rows are not an implementation detail of the aggregate: they are the artifact.
--
-- SCOPE. Three of the sixteen tables of chapter 12 section 12.12, the ones chapter 7
-- section 7.7 owns:
--
--     evaluation_runs          7.7.1   the BINDING; append-only, terminal states
--     evaluation_case_metrics  7.7.2   one row per (run, case, metric)  MOS-EVID-065
--     evaluation_case_scores   7.7.2   one row per (run, case)          MOS-STORE-299
--
-- NOT in this migration, on purpose: `capability_claims`, `acceptance_criteria`,
-- `tenant_acceptance_bindings`, `deployment_gate_decisions`, `validation_reports`. A
-- claim cites a run, a gate decision cites two, and a report cites all of it -- they sit
-- ON TOP of this table and each is owned by the chapter section that defines its verb
-- (7.7.3, 7.8, 7.9, 7.12). Declaring a stub for any of them here would be a second answer
-- to a question this migration does not own. 0006 declined the same way and for the same
-- reason.
--
-- WHY THE BINDING IS THE TABLE.
--
--   MOS-EVID-061  "An EvaluationRun MUST bind every input that can change a number." A
--                 number whose cohort, split, partition, reference standard,
--                 preprocessing, harness commit, container digest, backend, accelerator
--                 and operating point are not all recorded is not reproducible, and an
--                 irreproducible number that has been published is worse than no number,
--                 because the next measurement disagrees with it and nobody can say why.
--   MOS-EVID-065  "Per-case metrics MUST be persisted. Aggregates alone MUST NOT be
--                 stored without them." A mean hides the stratum that collapsed; the gate
--                 of 7.9.2 is a PAIRED test over per-case differences and cannot run at
--                 all without the rows. Section 5's INSERT guard is what stops the rows
--                 and the aggregate from ever describing different case sets.
--   MOS-EVID-062  `code_dirty = true` blocks SUCCEEDED, as a CHECK and not as a lint.
--
-- ADDITIVE, with one exception stated up front: section 3 adds two UNIQUE constraints to
-- `dataset_splits` and `annotation_sets` (0006's tables) so that this migration's foreign
-- keys can carry `dataset_version_id` through them. Nothing is dropped, renamed or
-- widened; the constraints are strictly new and the down-migration removes exactly them.
--
-- FOUR DELIBERATE DIVERGENCES FROM CHAPTER 12 SECTION 12.12, each at the point of use:
--   1. `UNIQUE (tenant_id, run_digest)`, not `UNIQUE (run_digest)` -- section 2, on the
--      precedent 0006 set for `manifest_digest` and for the same cross-tenant reason.
--   2. `tenant_id` and `coalesce(service_version_id, model_version_id)` in the binding
--      index -- section 2. Without the coalesce the index is DECORATIVE: one of the two
--      subject columns is always NULL and NULL is never equal to NULL, so every row would
--      be unique no matter what it duplicated.
--   3. `public_id` carrying chapter 7 section 7.2's `evr_` ULID identity beside the uuid
--      PK -- the MOS-STORE-357 construction 0006 applied to the other four entities.
--   4. `partition` is CHECKed to `train`/`tune`/`test`: `excluded` is a disposition, not
--      an evaluable partition (MOS-EVID-031), and `val` has never been a partition
--      (MOS-EVID-033).
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The `sha256_digest` domain.  MOS-STORE-211, MOS-EVID-007.
--
-- Guarded exactly as 0006 guards it, and for the same reason: several migrations in this
-- release block were authored in parallel, any of them may have created it first, and a
-- migration that fails because a sibling won the race is a false failure. Eleven digest
-- columns below depend on the spelling being checked once.
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
-- 2. evaluation_runs.  Chapter 7 section 7.7.1, MOS-STORE-262's row of section 12.12.
--
-- Every column of section 12.12's `evaluation_runs` row is here, under section 12.12's
-- spelling. The five constraints it states are reproduced verbatim as named CHECKs so
-- that `pg_get_constraintdef` can be diffed against the specification text.
--
-- `state`, NEVER `status` (section 12.12 says so in terms: "the column is `state`, never
-- `status`, and MUST NOT diverge"). `dataset_versions` next door uses `status`. The two
-- differ because the enums differ, and a shared name over two different value sets is how
-- a query that filters one ends up silently filtering the other.
--
-- SUBJECT: exactly one of `service_version_id` / `model_version_id`. Neither has a
-- foreign key because neither table exists yet -- chapter 6's registry lands with the
-- artifacts work of 0.3.0 (15.2.6). They are `uuid` columns with the CHECK that matters
-- (MOS-EVID-061's "exactly one") and the FK is REPORTED as owed rather than faked with a
-- stub table nobody owns. `capability_id` is `text` because it is the
-- `medos.capabilities.REGISTRY` key and not a row (CONTRACT.md section 6, schema.sql
-- line 40), which is also how `results.capability_id` is spelled.
-- =====================================================================================
CREATE TABLE evaluation_runs (
  id                  uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- Chapter 7 section 7.2: `evr_<ULID>`. Crockford base32, no I/L/O/U.
  public_id           text NOT NULL UNIQUE
    CHECK (public_id ~ '^evr_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

  -- MOS-EVID-003 fixes exactly these three. There is no fourth kind and no unlabelled
  -- evidence, so the column is NOT NULL with no default: a caller who has not decided
  -- which activity this is has not decided what the number means (MOS-STORE-303).
  kind                text NOT NULL
    CHECK (kind IN ('vendor_evidence','site_acceptance','monitoring_period')),

  -- ---- the subject -----------------------------------------------------------------
  service_version_id  uuid,
  model_version_id    uuid,
  capability_id       text NOT NULL CHECK (capability_id <> ''),

  -- ---- the cohort, the split and the reference standard ----------------------------
  -- All NOT NULL. MOS-EVID-061 phrases the requirement as "a run missing any field MUST
  -- NOT reach state = SUCCEEDED", which would admit a PENDING row with no cohort; that
  -- reading is rejected here, because the binding is what a run IS. A row that does not
  -- yet know which cohort it will measure is not a pending evaluation, it is a plan.
  -- The DIGESTS are copied from the parent rows at insert and are what the gate pairs on
  -- (MOS-EVID-086) -- the id says which row, the digest says which CONTENT, and after a
  -- restore-from-backup those are not the same question.
  dataset_version_id      uuid NOT NULL,
  dataset_version_digest  sha256_digest NOT NULL,
  split_id                uuid NOT NULL,
  split_digest            sha256_digest NOT NULL,
  partition               text NOT NULL CHECK (partition IN ('train','tune','test')),
  annotation_set_id       uuid NOT NULL,
  annotation_digest       sha256_digest NOT NULL,

  -- ---- how the predictions were produced -------------------------------------------
  -- Native mode (chapter 4) declares the preprocessing spec; sealed mode declares the
  -- vendor's opaque `internal_pipeline_digest` (MOS-EVID-063), which the report MUST
  -- mark vendor-asserted rather than platform-verified.
  preprocessing_spec_id       text,
  preprocessing_spec_version  integer CHECK (preprocessing_spec_version IS NULL
                                             OR preprocessing_spec_version >= 1),
  preprocessing_spec_digest   sha256_digest,
  internal_pipeline_digest    sha256_digest,

  -- MOS-EVID-062. 40 hex is a SHA-1 object name, 64 a SHA-256 one; git accepts both and
  -- a repository that has migrated MUST NOT be unable to record its own commits.
  code_commit         text NOT NULL CHECK (code_commit ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
  code_dirty          boolean NOT NULL,
  -- Section 12.12 carries BOTH: the harness that computed the metrics and the container
  -- that produced the predictions. They are different images and conflating them loses
  -- the ability to say which of the two changed when a number moves.
  evaluator_image_digest  sha256_digest,
  image_digest            sha256_digest NOT NULL,
  -- MOS-EVID-064: a run MUST NOT be carried across `inference_backend` or
  -- `accelerator.trt`. Recording them is what makes that enforceable at the gate.
  inference_backend   jsonb NOT NULL,
  accelerator         jsonb NOT NULL,

  -- ---- the conventions, recorded on the row ----------------------------------------
  -- MOS-EVID-055: `{name: {value, selected_on}}`. MOS-EVID-033 forbids `selected_on` from
  -- naming the partition being reported; that is a cross-column rule over a jsonb map's
  -- VALUES, which a CHECK cannot iterate, so it is a blocking refusal in
  -- `medos.evidence.evaluation.create_run` and asserted by the integration test.
  operating_thresholds  jsonb NOT NULL DEFAULT '{}'
    CHECK (jsonb_typeof(operating_thresholds) = 'object'),
  -- The section 7.6 block, recorded verbatim (MOS-EVID-047, MOS-EVID-051). The two
  -- CHECKs below are the whole point of the column: both conventions are IRREVERSIBLE
  -- once a number is published, so the database refuses a row that claims a different
  -- one rather than leaving the pinning to whichever code path wrote it.
  metric_conventions    jsonb NOT NULL,
  metric_registry_version integer NOT NULL CHECK (metric_registry_version >= 1),
  -- MOS-EVID-058: "The RNG seed MUST be recorded on the run and the interval MUST be
  -- reproducible from the persisted per-case rows alone."
  seed                bigint NOT NULL,
  runner              text NOT NULL CHECK (runner <> ''),

  -- ---- state and result ------------------------------------------------------------
  state               text NOT NULL DEFAULT 'PENDING'
    CHECK (state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','INVALIDATED')),
  invalidation_reason text,
  failure_reason      text,
  -- MOS-EVID-066: "recomputable from `evaluation_case_metrics` alone". The serialised
  -- form is the `aggregates` ARRAY of section 7.12.1's report.json, so the column is an
  -- array and not an object: `{"dice": 0.91}` is the shape MOS-EVID-056 exists to make
  -- impossible, and an object-shaped column invites exactly it back.
  aggregate_metrics   jsonb,
  run_digest          sha256_digest,
  -- The per-case rows, exported for the offline bundle of section 7.12.3.
  per_case_bucket     text,
  per_case_object_key text,
  started_at          timestamptz,
  finished_at         timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT evaluation_runs_tenant_id_uk UNIQUE (tenant_id, id),

  -- DIVERGENCE 1. Section 12.12 says `UNIQUE (run_digest)`. Scoped to the tenant here,
  -- on the precedent 0006 set for `dataset_versions.manifest_digest` and for the same two
  -- reasons: under FORCE RLS an unscoped unique index is a cross-tenant existence oracle
  -- (an INSERT that fails on a row the caller cannot SELECT proves the row exists), and
  -- the colliding row is invisible, so the caller cannot be told what to do about it.
  CONSTRAINT evaluation_runs_digest_uk UNIQUE (tenant_id, run_digest),

  CONSTRAINT evaluation_runs_subject CHECK (
    (service_version_id IS NULL) <> (model_version_id IS NULL)),
  CONSTRAINT evaluation_runs_native_preprocessing CHECK (
    model_version_id IS NULL OR preprocessing_spec_digest IS NOT NULL),
  CONSTRAINT evaluation_runs_sealed_pipeline CHECK (
    service_version_id IS NULL OR internal_pipeline_digest IS NOT NULL),
  -- DIVERGENCE 5, FORCED, AND IT IS A SPECIFICATION DEFECT RATHER THAN A PREFERENCE.
  --
  -- Section 12.12 writes this as an EQUIVALENCE:
  --     CHECK ((state='SUCCEEDED') = (finished_at IS NOT NULL AND run_digest IS NOT NULL))
  -- which makes MOS-EVID-014 unimplementable. Marking a DatasetVersion DEFECTIVE MUST
  -- cascade INVALIDATED onto every run bound to it (MOS-STORE-301a, in the same
  -- transaction) -- and a SUCCEEDED run carries `finished_at` and `run_digest`, which it
  -- MUST keep, because the evidence is REVOKED and not destroyed: a reader of an old
  -- report needs to see what was measured and that it no longer stands. Under the
  -- equivalence, the moment `state` becomes INVALIDATED the row violates its own CHECK
  -- and the cascade aborts. Measured, not reasoned about: the integration test hit it.
  --
  -- Split into the two implications that were actually meant, so the equivalence holds
  -- for every state the result columns are being written in, and INVALIDATED keeps what
  -- it had:
  --   * a SUCCEEDED run has both;
  --   * a run that has not finished has neither (no half-written result).
  CONSTRAINT evaluation_runs_succeeded CHECK (
    state <> 'SUCCEEDED' OR (finished_at IS NOT NULL AND run_digest IS NOT NULL)),
  CONSTRAINT evaluation_runs_unfinished CHECK (
    state NOT IN ('PENDING','RUNNING') OR run_digest IS NULL),
  -- MOS-EVID-062: "An evaluation run from an uncommitted working tree is not evidence."
  CONSTRAINT evaluation_runs_clean_tree CHECK (
    state <> 'SUCCEEDED' OR code_dirty = false),
  CONSTRAINT evaluation_runs_invalidation CHECK (
    (state = 'INVALIDATED') = (invalidation_reason IS NOT NULL)),
  CONSTRAINT evaluation_runs_failure CHECK (
    failure_reason IS NULL OR state IN ('FAILED','INVALIDATED')),
  -- MOS-EVID-065: aggregates alone MUST NOT be stored, and MOS-EVID-056: a bare scalar
  -- MUST fail validation. A SUCCEEDED run therefore carries its aggregate block, and an
  -- unfinished one carries none -- a half-written result is not a result. Stated as two
  -- implications rather than an equivalence for the reason above: an INVALIDATED run
  -- keeps the block it was measured with.
  CONSTRAINT evaluation_runs_aggregates CHECK (
    state <> 'SUCCEEDED' OR aggregate_metrics IS NOT NULL),
  CONSTRAINT evaluation_runs_no_early_aggregates CHECK (
    state NOT IN ('PENDING','RUNNING','FAILED') OR aggregate_metrics IS NULL),
  CONSTRAINT evaluation_runs_aggregates_array CHECK (
    aggregate_metrics IS NULL OR jsonb_typeof(aggregate_metrics) = 'array'),
  -- MOS-EVID-049: "The unqualified word `dice` MUST NOT be used as a field name, API key,
  -- report label or UI string ... A ValidationReport containing a key named `dice` MUST
  -- fail schema validation." The report is the serialisation of this column, so the
  -- refusal belongs where the value is written and not only where it is exported. A text
  -- match rather than a key walk: it also catches `"dice"` appearing as a metric VALUE,
  -- which is the same defect wearing the other hat.
  CONSTRAINT evaluation_runs_no_bare_dice CHECK (
    aggregate_metrics IS NULL OR aggregate_metrics::text NOT LIKE '%"dice"%'),
  -- MOS-EVID-047 and MOS-EVID-051 / MOS-STORE-296, pinned. "Both of the conventions below
  -- are irreversible the moment a number is published." A row claiming `pooled` as the
  -- primary aggregation, or `score_one` for empty ground truth, is not a differently
  -- configured run: it is a number that cannot be compared with any other number this
  -- platform has ever emitted.
  CONSTRAINT evaluation_runs_conventions_pinned CHECK (
    metric_conventions->>'dice_aggregation' = 'mean_of_per_case'
    AND metric_conventions->>'empty_gt_policy' = 'exclude_and_report_separately'),
  -- MOS-EVID-053: the threshold is declared per capability and RECORDED ON THE RUN.
  CONSTRAINT evaluation_runs_fp_threshold CHECK (
    metric_conventions ? 'fp_volume_threshold_ml'),
  -- One fact, two spellings, reconciled by the database instead of by discipline: both
  -- section 12.12 (the columns) and section 7.12.1 (the conventions block it prints)
  -- carry the seed and the registry version, and two copies that can disagree are one
  -- copy plus a bug. Compared as TEXT so a malformed value is a constraint violation
  -- rather than a cast error from a different SQLSTATE class.
  CONSTRAINT evaluation_runs_seed_agrees CHECK (
    metric_conventions->>'bootstrap_seed' = seed::text),
  CONSTRAINT evaluation_runs_registry_agrees CHECK (
    metric_conventions->>'metric_registry_version' = metric_registry_version::text),

  CONSTRAINT evaluation_runs_dataset_version_fk
    FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT
);

-- DIVERGENCE 2, and it is the difference between a constraint and a comment.
--
-- Section 12.12: "a partial unique index over the full binding while `state = SUCCEEDED`
-- ... so that one binding yields one successful run and a second attempt is a duplicate,
-- not a second opinion." Written literally over `(service_version_id, model_version_id,
-- ...)` the index does NOTHING: `evaluation_runs_subject` guarantees one of the two is
-- always NULL, and a btree unique index treats every NULL as distinct, so no two rows
-- ever collide however identical their bindings. `coalesce` is what makes the index
-- express the rule it is written for.
--
-- `capability_id` is deliberately NOT a key column, following section 12.12's list
-- exactly: adding a column WIDENS a unique index, and `annotation_digest` already
-- determines the capability (an AnnotationSet names exactly one, 0006 section 7).
CREATE UNIQUE INDEX evaluation_runs_binding_uk ON evaluation_runs (
  tenant_id,
  coalesce(service_version_id, model_version_id),
  dataset_version_digest,
  split_digest,
  partition,
  annotation_digest,
  image_digest,
  coalesce(preprocessing_spec_digest, internal_pipeline_digest),
  operating_thresholds,
  metric_conventions
) WHERE state = 'SUCCEEDED';

CREATE INDEX evaluation_runs_subject_idx
  ON evaluation_runs (tenant_id, capability_id, state);
CREATE INDEX evaluation_runs_cohort_idx
  ON evaluation_runs (tenant_id, dataset_version_id, partition);

COMMENT ON TABLE evaluation_runs IS
  'Chapter 7 section 7.7 / MOS-EVID-061: binds every input that can change a number. '
  'Append-only; a bad run is INVALIDATED with a reason, never edited and never deleted '
  '(MOS-EVID-014).';


-- =====================================================================================
-- 3. The cohort-coherence keys.  ADDITIVE ALTERs to two of 0006's tables.
--
-- A run names a DatasetVersion, a split and an AnnotationSet. Nothing said so far stops
-- it from naming split X of cohort A together with cohort B: three independent foreign
-- keys, three satisfied constraints, one meaningless run. The pairing rule of
-- MOS-EVID-086 and the annotation-coverage rule of chapter 7 acceptance check 6 both
-- assume the three agree, and an assumption that the schema does not hold is a defect
-- waiting for the first hurried caller.
--
-- Two new UNIQUE constraints give the composite foreign keys below something to point at.
-- They are strictly additive -- no column changes type, nothing is dropped, and both
-- tables already have a superset key -- and the down-migration removes exactly them.
-- =====================================================================================
ALTER TABLE dataset_splits
  ADD CONSTRAINT dataset_splits_version_id_uk UNIQUE (tenant_id, dataset_version_id, id);
ALTER TABLE annotation_sets
  ADD CONSTRAINT annotation_sets_version_id_uk UNIQUE (tenant_id, dataset_version_id, id);

ALTER TABLE evaluation_runs
  ADD CONSTRAINT evaluation_runs_split_fk
    FOREIGN KEY (tenant_id, dataset_version_id, split_id)
    REFERENCES dataset_splits (tenant_id, dataset_version_id, id) ON DELETE RESTRICT,
  ADD CONSTRAINT evaluation_runs_annotation_set_fk
    FOREIGN KEY (tenant_id, dataset_version_id, annotation_set_id)
    REFERENCES annotation_sets (tenant_id, dataset_version_id, id) ON DELETE RESTRICT;


-- =====================================================================================
-- 4. evaluation_case_metrics.  MOS-EVID-065, MOS-EVID-067, MOS-EVID-068, MOS-STORE-296.
--
-- THE TABLE THE 0.2.0 GATE'S `per-case-metrics` CHECK IS ABOUT. One row per
-- (run, case, metric). Aggregates are derived from it and never the other way round.
--
-- `strata` (MOS-EVID-067) carries the acquisition and clinical stratum of the case: "a
-- stratum that is not persisted cannot be gated on". It is `jsonb NOT NULL` rather than a
-- column per stratum because the capability declares its own clinical strata
-- (`reference_volume_ml` for pleural effusion) and a column per capability is a migration
-- per capability.
--
-- `gt_voxels` / `pred_voxels` / `intersection_voxels` are NOT NULL EVEN WHEN `value` IS
-- NULL (MOS-EVID-068): `dice_pooled` has no per-case decomposition and MOS-EVID-060
-- requires it and its bootstrap to be recomputed from these three counts. A NULL count on
-- an empty-GT case would silently drop that case from the pooled denominator, which is
-- the arithmetic the pooled figure exists to expose.
-- =====================================================================================
CREATE TABLE evaluation_case_metrics (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  evaluation_run_id   uuid NOT NULL,
  -- The study-level case handle, the same one 0006 section 4 defines for `dataset_cases`.
  case_key            text NOT NULL CHECK (case_key <> ''),
  patient_key         text NOT NULL CHECK (patient_key ~ '^pk_[a-z2-7]{16}$'),
  study_instance_uid  dicom_uid NOT NULL,
  -- "the series UID is what the pairing function of 7.9.2 joins on" -- section 12.12.
  series_instance_uid dicom_uid NOT NULL,
  -- A registry id (MOS-EVID-054). The database does not hold the registry -- it is a
  -- Python table with a version, and duplicating it in a CHECK would produce two
  -- registries that drift -- but MOS-EVID-049's ONE prohibition is absolute and costs a
  -- single comparison: the unqualified word is never a metric id.
  metric              text NOT NULL CHECK (metric <> '' AND metric <> 'dice'),
  -- NULL means "not defined for this case", never "zero" and never "missing".
  value               double precision,
  undefined_reason    text CHECK (undefined_reason IN
                        ('empty_ground_truth','empty_prediction','excluded','error')),
  -- MOS-EVID-051: counted in the aggregate for this metric, or not.
  eligible            boolean NOT NULL,
  gt_voxels           bigint NOT NULL CHECK (gt_voxels >= 0),
  pred_voxels         bigint NOT NULL CHECK (pred_voxels >= 0),
  intersection_voxels bigint NOT NULL CHECK (intersection_voxels >= 0),
  gt_volume_ml        double precision NOT NULL CHECK (gt_volume_ml >= 0),
  pred_volume_ml      double precision NOT NULL CHECK (pred_volume_ml >= 0),
  strata              jsonb NOT NULL CHECK (jsonb_typeof(strata) = 'object'),
  created_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT evaluation_case_metrics_uk
    UNIQUE (evaluation_run_id, case_key, metric),
  CONSTRAINT evaluation_case_metrics_run_fk
    FOREIGN KEY (tenant_id, evaluation_run_id)
    REFERENCES evaluation_runs (tenant_id, id) ON DELETE RESTRICT,
  -- Verbatim from section 12.12 and chapter 7 section 7.7.2.
  CONSTRAINT evaluation_case_metrics_defined
    CHECK (value IS NOT NULL OR undefined_reason IS NOT NULL),
  -- An intersection larger than either mask is arithmetically impossible, and a pooled
  -- Dice computed from impossible counts is worse than no pooled Dice: it is a plausible
  -- number nobody can trace to a defect.
  CONSTRAINT evaluation_case_metrics_intersection
    CHECK (intersection_voxels <= least(gt_voxels, pred_voxels)),
  -- CONVENTION 2, MADE UNWRITABLE.  MOS-EVID-051 / MOS-STORE-296.
  --
  -- "A column offering `score_one` or `score_zero` MUST NOT be added: the choice would
  -- make a published Dice a function of cohort composition." There is no such column --
  -- and this CHECK closes the same hole one level down, where it would actually be
  -- opened: by writing `value = 1.0, eligible = true` on an empty-GT case. Row 3 and row
  -- 4 of the MOS-EVID-051 table say `null` and `no`; this is that row, enforced.
  --
  -- Scoped to `dice_mean_per_case` and NOT generalised to every metric, because a case
  -- with zero reference voxels is a legitimate, eligible NEGATIVE for `brier`,
  -- `specificity` and every other classification metric. The empty-GT exclusion is a
  -- statement about overlap, not about the case.
  CONSTRAINT evaluation_case_metrics_empty_gt CHECK (
    metric <> 'dice_mean_per_case'
    OR gt_voxels > 0
    OR (eligible = false AND value IS NULL AND undefined_reason = 'empty_ground_truth'))
);

CREATE INDEX evaluation_case_metrics_run_idx
  ON evaluation_case_metrics (tenant_id, evaluation_run_id, metric);
CREATE INDEX evaluation_case_metrics_patient_idx
  ON evaluation_case_metrics (tenant_id, evaluation_run_id, patient_key);
-- The join key of the pairing function of section 7.9.2.
CREATE INDEX evaluation_case_metrics_series_idx
  ON evaluation_case_metrics (tenant_id, evaluation_run_id, series_instance_uid);

COMMENT ON TABLE evaluation_case_metrics IS
  'Chapter 7 section 7.7.2 / MOS-EVID-065: per-case metrics are persisted, not only '
  'aggregates. Append-only and fully sealed (MOS-STORE-228). MOS-EVID-066: '
  'evaluation_runs.aggregate_metrics MUST be recomputable from these rows alone.';


-- =====================================================================================
-- 5. evaluation_case_scores.  MOS-EVID-069, MOS-STORE-299.
--
-- One row per case, not per metric: "a case has one score and many metrics". The
-- continuous, PRE-THRESHOLD score and the reference label are what make a ROC or PR curve
-- recomputable and an operating threshold RE-SELECTABLE WITHOUT RE-RUNNING INFERENCE --
-- "the property that stops a threshold change from becoming a GPU project".
-- =====================================================================================
CREATE TABLE evaluation_case_scores (
  tenant_id           uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  evaluation_run_id   uuid NOT NULL,
  case_key            text NOT NULL CHECK (case_key <> ''),
  -- The clustering unit for every bootstrap (MOS-EVID-057).
  patient_key         text NOT NULL CHECK (patient_key ~ '^pk_[a-z2-7]{16}$'),
  case_score          double precision,
  case_label          smallint CHECK (case_label IN (0, 1)),
  -- MOS-EVID-069: one object per predicted candidate, with score, centroid, volume and
  -- the matched reference. Default '[]' and NOT NULL, so "no candidates" is a fact the
  -- row states rather than a fact it omits.
  candidates          jsonb NOT NULL DEFAULT '[]'
    CHECK (jsonb_typeof(candidates) = 'array'),
  created_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT evaluation_case_scores_pk PRIMARY KEY (evaluation_run_id, case_key),
  CONSTRAINT evaluation_case_scores_run_fk
    FOREIGN KEY (tenant_id, evaluation_run_id)
    REFERENCES evaluation_runs (tenant_id, id) ON DELETE RESTRICT
);

CREATE INDEX evaluation_case_scores_patient_idx
  ON evaluation_case_scores (tenant_id, evaluation_run_id, patient_key);


-- =====================================================================================
-- 6. THE SEAL.  MOS-EVID-013, MOS-EVID-014, MOS-STORE-228.
--
-- Three layers, and they catch three different things.
--
--   (a) The GRANT (section 8) bounds `medicalos_app`. `evaluation_case_metrics` and
--       `evaluation_case_scores` are named in MOS-STORE-228's table-level revocation
--       list; `evaluation_runs` is not, because its `state` legitimately advances.
--   (b) `forbid_evidence_mutation()` (0006 section 10) binds every role INCLUDING the
--       owner and a superuser psql session, which is the case that actually destroys
--       evidence and which a GRANT cannot reach.
--   (c) `forbid_evaluation_rewrite()` below is the one this table needs that 0006 did
--       not: `evaluation_runs` is neither immutable nor freely mutable. It is APPEND-ONLY
--       WITH A STATE MACHINE -- the binding never changes, a result is written once, and
--       a bad run is INVALIDATED rather than corrected. Chapter 7 acceptance check 1
--       names `evaluation_runs.run_digest` alongside the three sealed digests, so the
--       digest is immutable ONCE SET while remaining writable exactly once.
-- =====================================================================================
CREATE FUNCTION forbid_evaluation_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  permitted text[];
BEGIN
  -- ---- the binding is immutable, in every state ------------------------------------
  -- MOS-EVID-061: a run binds every input that can change a number. If the binding can be
  -- edited after the fact then it binds nothing -- the row would say one cohort while the
  -- number came from another, and no reader could tell.
  IF (NEW.tenant_id, NEW.kind, NEW.capability_id,
      NEW.service_version_id, NEW.model_version_id,
      NEW.dataset_version_id, NEW.dataset_version_digest,
      NEW.split_id, NEW.split_digest, NEW.partition,
      NEW.annotation_set_id, NEW.annotation_digest,
      NEW.preprocessing_spec_id, NEW.preprocessing_spec_version,
      NEW.preprocessing_spec_digest, NEW.internal_pipeline_digest,
      NEW.code_commit, NEW.code_dirty, NEW.evaluator_image_digest, NEW.image_digest,
      NEW.inference_backend, NEW.accelerator, NEW.operating_thresholds,
      NEW.metric_conventions, NEW.metric_registry_version, NEW.seed,
      NEW.id, NEW.public_id, NEW.created_at)
     IS DISTINCT FROM
     (OLD.tenant_id, OLD.kind, OLD.capability_id,
      OLD.service_version_id, OLD.model_version_id,
      OLD.dataset_version_id, OLD.dataset_version_digest,
      OLD.split_id, OLD.split_digest, OLD.partition,
      OLD.annotation_set_id, OLD.annotation_digest,
      OLD.preprocessing_spec_id, OLD.preprocessing_spec_version,
      OLD.preprocessing_spec_digest, OLD.internal_pipeline_digest,
      OLD.code_commit, OLD.code_dirty, OLD.evaluator_image_digest, OLD.image_digest,
      OLD.inference_backend, OLD.accelerator, OLD.operating_thresholds,
      OLD.metric_conventions, OLD.metric_registry_version, OLD.seed,
      OLD.id, OLD.public_id, OLD.created_at)
  THEN
    RAISE EXCEPTION 'evaluation_runs %: the binding of an EvaluationRun is immutable',
                    OLD.public_id
      USING ERRCODE = 'MOS06',
            HINT = 'MOS-EVID-061: an EvaluationRun binds every input that can change a '
                   'number. A different binding is a DIFFERENT RUN. A run that measured '
                   'the wrong thing is INVALIDATED with a reason (MOS-EVID-014).';
  END IF;

  -- ---- a result is written once ----------------------------------------------------
  -- Chapter 7 acceptance check 1 attempts `UPDATE ... SET run_digest = 'sha256:0'` and
  -- requires a refusal. NULL -> value is the write that finishes the run; value -> value'
  -- is the rewrite of a published number.
  IF OLD.run_digest IS NOT NULL AND NEW.run_digest IS DISTINCT FROM OLD.run_digest THEN
    RAISE EXCEPTION 'evaluation_runs %: run_digest is sealed once written', OLD.public_id
      USING ERRCODE = 'MOS06',
            HINT = 'MOS-EVID-007/MOS-EVID-066: the digest addresses the aggregate block. '
                   'Re-measuring produces a NEW run under the same binding, which the '
                   'partial unique index then refuses as a duplicate rather than '
                   'accepting as a second opinion.';
  END IF;
  IF OLD.aggregate_metrics IS NOT NULL
     AND NEW.aggregate_metrics IS DISTINCT FROM OLD.aggregate_metrics THEN
    RAISE EXCEPTION 'evaluation_runs %: aggregate_metrics is sealed once written',
                    OLD.public_id
      USING ERRCODE = 'MOS06',
            HINT = 'MOS-EVID-066: the aggregate block is recomputable from the per-case '
                   'rows, which are themselves append-only. Editing it would make the '
                   'two disagree, which is the one thing the recomputation check exists '
                   'to detect.';
  END IF;

  -- ---- the state machine -----------------------------------------------------------
  -- MOS-EVID-014 for a run: a bad run MAY be invalidated but MUST NOT be revived. Every
  -- state may go to INVALIDATED, because MOS-STORE-301a cascades it from a DEFECTIVE
  -- cohort onto EVERY run bound to that cohort regardless of how each one ended.
  IF NEW.state IS DISTINCT FROM OLD.state THEN
    permitted := CASE OLD.state
      WHEN 'PENDING'     THEN ARRAY['RUNNING','FAILED','INVALIDATED']
      WHEN 'RUNNING'     THEN ARRAY['SUCCEEDED','FAILED','INVALIDATED']
      WHEN 'SUCCEEDED'   THEN ARRAY['INVALIDATED']
      WHEN 'FAILED'      THEN ARRAY['INVALIDATED']
      WHEN 'INVALIDATED' THEN ARRAY[]::text[]
    END;
    IF NOT (NEW.state = ANY (permitted)) THEN
      RAISE EXCEPTION 'evaluation_runs %: % -> % is not a permitted transition',
                      OLD.public_id, OLD.state, NEW.state
        USING ERRCODE = 'MOS06',
              HINT = 'MOS-EVID-014 owns this enum. INVALIDATED is terminal: a run that '
                     'has been invalidated is not repaired, it is superseded by a new '
                     'run.';
    END IF;
  END IF;

  RETURN NEW;
END;
$$;
ALTER FUNCTION forbid_evaluation_rewrite() OWNER TO medicalos_owner;

CREATE TRIGGER evaluation_runs_append_only BEFORE UPDATE ON evaluation_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_evaluation_rewrite();

-- MOS-EVID-014: deleted is not one of the permitted states, here or on the per-case rows.
CREATE TRIGGER evaluation_runs_no_delete BEFORE DELETE ON evaluation_runs
  FOR EACH ROW EXECUTE FUNCTION forbid_evidence_mutation();

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['evaluation_case_metrics','evaluation_case_scores'] LOOP
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION forbid_evidence_mutation()', t || '_frozen', t);
  END LOOP;
END $$;


-- =====================================================================================
-- 7. THE INSERT GUARD, and it is the one that makes MOS-EVID-066 true.
--
-- "`evaluation_runs.aggregate_metrics` MUST be recomputable from `evaluation_case_metrics`
-- alone, to within 1e-9 relative." That holds at the instant the aggregate is computed.
-- It stops holding the moment anyone appends a case row to a finished run -- and nothing
-- said so far forbids it, because both tables are append-only and appending is exactly
-- what they permit. The aggregate would then describe a strictly smaller case set than
-- the rows, with no edit anywhere and no trigger fired.
--
-- So: a per-case row may be written only while its run is PENDING or RUNNING. After that
-- the case set is closed, which is the same guarantee sealing gives a cohort.
-- =====================================================================================
CREATE FUNCTION forbid_case_rows_after_finish() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  run_state text;
  run_public text;
BEGIN
  SELECT state, public_id INTO run_state, run_public
    FROM evaluation_runs WHERE id = NEW.evaluation_run_id;
  IF run_state IS NULL THEN
    -- Not reachable through the foreign key; reachable if RLS hides the parent row, which
    -- is a tenant-confusion bug and MUST NOT be reported as a missing run.
    RAISE EXCEPTION 'evaluation run % is not visible in this tenant', NEW.evaluation_run_id
      USING ERRCODE = 'MOS05';
  END IF;
  IF run_state NOT IN ('PENDING','RUNNING') THEN
    RAISE EXCEPTION 'evaluation_runs % is %: per-case rows are closed',
                    run_public, run_state
      USING ERRCODE = 'MOS05',
            HINT = 'MOS-EVID-066: the aggregate block is recomputable from the per-case '
                   'rows. Appending a case to a finished run would make the two describe '
                   'different case sets with nothing edited and nothing to detect it. '
                   'Measure again as a NEW run.';
  END IF;
  RETURN NEW;
END;
$$;
ALTER FUNCTION forbid_case_rows_after_finish() OWNER TO medicalos_owner;

CREATE TRIGGER evaluation_case_metrics_closed BEFORE INSERT ON evaluation_case_metrics
  FOR EACH ROW EXECUTE FUNCTION forbid_case_rows_after_finish();
CREATE TRIGGER evaluation_case_scores_closed BEFORE INSERT ON evaluation_case_scores
  FOR EACH ROW EXECUTE FUNCTION forbid_case_rows_after_finish();


-- =====================================================================================
-- 8. THE CASCADE.  MOS-EVID-014, MOS-STORE-301a.
--
-- "Marking a DatasetVersion `DEFECTIVE` MUST cascade `INVALIDATED` onto every
-- EvaluationRun bound to it and MUST set `validation_reports.status = 'REVOKED'` on every
-- report citing those runs." MOS-STORE-301a: "The cascade is a transaction, not a
-- background job: a defective cohort must never leave a live report standing on it."
--
-- A TRIGGER AND NOT A FUNCTION IN `medos.evidence`, deliberately. The requirement is
-- "in the same transaction" and the danger is a path that marks the version without
-- calling the cascade -- an operator with psql, a repair script, a second repository
-- function written next quarter. The trigger binds all of them. It is the same argument
-- MOS-EVID-013 makes for putting sealing in the database, applied to the other direction.
--
-- The `validation_reports` half is written dynamically and is a NO-OP until that table
-- exists: it is created by a later migration in this release block, owned by section
-- 7.12, and this migration does not declare other people's tables. What it does do is
-- make the cascade complete the moment the table appears, instead of depending on a
-- second agent remembering a requirement stated in a third chapter.
-- =====================================================================================
CREATE FUNCTION cascade_dataset_version_defect() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  reason    text;
  link      text;
  predicate text;
BEGIN
  reason := format('dataset_version %s marked DEFECTIVE: %s',
                   NEW.id, coalesce(NEW.defect_reason, 'no reason recorded'));

  UPDATE evaluation_runs
     SET state = 'INVALIDATED',
         invalidation_reason = reason
   WHERE dataset_version_id = NEW.id
     AND state <> 'INVALIDATED';

  -- The `validation_reports` half. That table is created by a LATER migration in this
  -- release block and owned by chapter 7 section 7.12, so this migration does not
  -- declare it -- but MOS-STORE-301a's rule is stated in terms of both tables at once
  -- ("a defective cohort must never leave a live report standing on it") and a cascade
  -- that half-fires is worse than one that does not exist, because the reports look
  -- checked.
  --
  -- Which column carries the link is the report table's business, and it has chosen
  -- `evaluation_run_ids uuid[]` (one report cites several runs). Both spellings are
  -- handled: the array with `&&`, a scalar with `IN`. Absent both, the cascade is a
  -- no-op rather than an error -- an evidence plane with no report table yet is a
  -- legitimate state, an unrevoked report over a defective cohort is not.
  IF to_regclass('public.validation_reports') IS NOT NULL
     AND EXISTS (SELECT 1 FROM information_schema.columns
                  WHERE table_schema = 'public' AND table_name = 'validation_reports'
                    AND column_name = 'revocation_reason')
  THEN
    SELECT column_name INTO link
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'validation_reports'
       AND column_name IN ('evaluation_run_ids', 'evaluation_run_id')
     ORDER BY length(column_name) DESC
     LIMIT 1;

    IF link = 'evaluation_run_ids' THEN
      predicate := format(
        'evaluation_run_ids && ARRAY(SELECT id FROM evaluation_runs '
        'WHERE dataset_version_id = %L)', NEW.id);
    ELSIF link = 'evaluation_run_id' THEN
      predicate := format(
        'evaluation_run_id IN (SELECT id FROM evaluation_runs '
        'WHERE dataset_version_id = %L)', NEW.id);
    END IF;

    IF predicate IS NOT NULL THEN
      EXECUTE format(
        'UPDATE validation_reports SET status = %L, revocation_reason = %L '
        'WHERE status <> %L AND %s', 'REVOKED', reason, 'REVOKED', predicate);
    END IF;
  END IF;

  RETURN NULL;
END;
$$;
ALTER FUNCTION cascade_dataset_version_defect() OWNER TO medicalos_owner;

CREATE TRIGGER dataset_versions_cascade_defect AFTER UPDATE OF status ON dataset_versions
  FOR EACH ROW
  WHEN (NEW.status = 'DEFECTIVE' AND OLD.status IS DISTINCT FROM 'DEFECTIVE')
  EXECUTE FUNCTION cascade_dataset_version_defect();


-- =====================================================================================
-- 9. ENABLE + FORCE ROW LEVEL SECURITY, and the policies.
--     MOS-EVID-012, MOS-SEC-072, MOS-STORE-223, MOS-STORE-224, MOS-STORE-229.
--
-- Identical in shape to 0002 section 10 and 0006 section 11: ownership to
-- `medicalos_owner` first, FORCE so the owner is bound by the policy too, and the policy
-- written TO PUBLIC so a session with no tenant bound gets 42704 from
-- `current_tenant_id()` rather than a silently empty result set. A per-case metric row is
-- patient-linked data (`patient_key`, `study_instance_uid`) and a query that quietly
-- returns nothing is how a cross-tenant bug looks like an empty cohort.
-- =====================================================================================
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['evaluation_runs','evaluation_case_metrics',
                           'evaluation_case_scores'] LOOP
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
-- 10. Grants.  MOS-STORE-228, MOS-SEC-073, MOS-STORE-230.
--
-- MOS-STORE-228 names `evaluation_case_metrics` and `evaluation_case_scores` in its
-- table-level revocation list: no UPDATE, no DELETE, at the grant level, for
-- `medicalos_app`. `evaluation_runs` takes a COLUMN-level grant instead, for the same
-- reason `validation_reports` and `artifacts` do: a small, named set of columns changes
-- while the binding does not.
--
-- The permitted set is the state machine's working set and nothing else. `run_digest` and
-- `aggregate_metrics` are in it because they are written ONCE, at the transition to
-- SUCCEEDED; section 6's trigger is what makes "once" true, and this grant is what keeps
-- the other twenty-six columns out of reach even of a bug.
-- =====================================================================================
GRANT SELECT, INSERT ON evaluation_runs TO medicalos_app;
GRANT UPDATE (state, invalidation_reason, failure_reason, started_at, finished_at,
              aggregate_metrics, run_digest, per_case_bucket, per_case_object_key)
  ON evaluation_runs TO medicalos_app;
REVOKE DELETE ON evaluation_runs FROM medicalos_app;

GRANT SELECT, INSERT ON evaluation_case_metrics, evaluation_case_scores TO medicalos_app;
REVOKE UPDATE, DELETE ON evaluation_case_metrics, evaluation_case_scores
  FROM medicalos_app;

GRANT SELECT ON evaluation_runs, evaluation_case_metrics, evaluation_case_scores
  TO medicalos_readonly;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 11. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================

-- MOS-SEC-077 / MOS-STORE-229, re-run over the whole database: a table with a `tenant_id`
-- column and row security not both enabled and forced MUST NOT exist.
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

-- MOS-EVID-071: "`ModelVersion` and `ServiceVersion` MUST NOT carry a free-form metrics
-- map ... Published performance exists only as rows in `capability_claims`." Neither
-- table exists yet, so what this migration can assert is the half it owns: no table in
-- this database grows a column that re-opens the hole. The check is cheap, runs over the
-- whole schema, and will still be here when the registry tables arrive.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(format('%s.%s', table_name, column_name), ', ' ORDER BY table_name)
    INTO bad
    FROM information_schema.columns
   WHERE table_schema = 'public'
     AND table_name IN ('model_versions','service_versions','artifacts')
     AND column_name IN ('metrics','performance','accuracy','dice','sensitivity');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-EVID-071: performance is not a manifest field: %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-EVID-051 / MOS-STORE-296: the empty-ground-truth policy is not configurable. A
-- later migration adding `empty_gt_policy`, `score_one` or `score_zero` as a COLUMN
-- would make the published Dice a function of cohort composition again, which is the
-- defect the convention exists to prevent. Asserted as a prohibition, because a
-- prohibition with no test is a comment.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name IN ('evaluation_runs','evaluation_case_metrics','annotation_sets')
       AND column_name IN ('empty_gt_policy','score_one','score_zero',
                           'dice_aggregation_policy')
  ) THEN
    RAISE EXCEPTION 'MOS-EVID-051/MOS-STORE-296: the empty-GT policy is fixed '
                    'platform-wide and MUST NOT become a column' USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-EVID-065: the per-case tables exist and are guarded. A forgotten trigger here is an
-- editable evidence row that nothing complains about.
DO $$
DECLARE t text; n integer;
BEGIN
  FOREACH t IN ARRAY ARRAY['evaluation_runs','evaluation_case_metrics',
                           'evaluation_case_scores'] LOOP
    SELECT count(*) INTO n
      FROM pg_trigger g JOIN pg_class c ON c.oid = g.tgrelid
     WHERE c.relname = t AND NOT g.tgisinternal AND (g.tgtype & 16) <> 0;  -- UPDATE
    IF n = 0 THEN
      RAISE EXCEPTION 'MOS-EVID-013: % is append-only but has no BEFORE UPDATE guard', t
        USING ERRCODE = '42501';
    END IF;
  END LOOP;
END $$;

-- The binding index is worthless if it was created over raw NULLable subject columns
-- (divergence 2 above). Asserted against the index definition, which is where a later
-- migration would "restore" the literal section 12.12 form and silently disable it.
DO $$
DECLARE def text;
BEGIN
  SELECT indexdef INTO def FROM pg_indexes
   WHERE schemaname = 'public' AND indexname = 'evaluation_runs_binding_uk';
  IF def IS NULL OR def NOT LIKE '%COALESCE(service_version_id, model_version_id)%' THEN
    RAISE EXCEPTION 'MOS-EVID-061: the binding index must coalesce the subject columns, '
                    'or every NULL makes every row unique (got: %)',
                    coalesce(def, 'no index') USING ERRCODE = '42501';
  END IF;
END $$;
