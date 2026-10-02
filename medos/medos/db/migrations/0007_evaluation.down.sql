-- =====================================================================================
-- 0007_evaluation.down.sql -- the reverse of 0007_evaluation.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- `medos.db.migrate` never runs a down-migration (see its module docstring) and the
-- supported recovery from a bad forward migration is restore-from-backup.
--
-- Read the consequence before running this anywhere real. `evaluation_case_metrics` is
-- the only place a per-case number exists (MOS-EVID-065): the aggregate block on the run
-- is DERIVED from these rows and nothing else in the deployment can reconstruct them --
-- not the model, not the cohort, not the annotation set, because reproducing a per-case
-- Dice requires re-running inference on the exact image, container, backend and
-- accelerator the run bound. Dropping these three tables turns every published figure in
-- the deployment into an unverifiable scalar, which is precisely the state MOS-EVID-056
-- was written to end.
--
-- `sha256_digest` and `forbid_evidence_mutation()` are deliberately NOT dropped: 0006
-- creates both, may still be depending on them, and 0007 only guarded the domain against
-- a parallel sibling having created it first. Dropping another migration's objects on the
-- way out is how a down-migration breaks the migration that ran before it.
-- =====================================================================================

SET lock_timeout = '3s';

-- The cascade first: it is a trigger on 0006's `dataset_versions` that reaches into this
-- migration's `evaluation_runs`, so it MUST go before the table it writes to.
DROP TRIGGER IF EXISTS dataset_versions_cascade_defect ON dataset_versions;
DROP FUNCTION IF EXISTS cascade_dataset_version_defect();

DROP TRIGGER IF EXISTS evaluation_case_scores_closed  ON evaluation_case_scores;
DROP TRIGGER IF EXISTS evaluation_case_metrics_closed ON evaluation_case_metrics;
DROP TRIGGER IF EXISTS evaluation_case_scores_frozen  ON evaluation_case_scores;
DROP TRIGGER IF EXISTS evaluation_case_metrics_frozen ON evaluation_case_metrics;
DROP TRIGGER IF EXISTS evaluation_runs_no_delete      ON evaluation_runs;
DROP TRIGGER IF EXISTS evaluation_runs_append_only    ON evaluation_runs;

DROP POLICY IF EXISTS evaluation_case_scores_tenant_isolation  ON evaluation_case_scores;
DROP POLICY IF EXISTS evaluation_case_metrics_tenant_isolation ON evaluation_case_metrics;
DROP POLICY IF EXISTS evaluation_runs_tenant_isolation         ON evaluation_runs;

-- Child before parent: every FK here is ON DELETE RESTRICT on purpose.
DROP TABLE IF EXISTS evaluation_case_scores;
DROP TABLE IF EXISTS evaluation_case_metrics;
DROP TABLE IF EXISTS evaluation_runs;

DROP FUNCTION IF EXISTS forbid_case_rows_after_finish();
DROP FUNCTION IF EXISTS forbid_evaluation_rewrite();

-- The two additive keys section 3 of the up-migration put on 0006's tables, and only
-- those two. `dataset_splits_tenant_id_uk` and `annotation_sets_tenant_id_uk` belong to
-- 0006 and are left alone.
ALTER TABLE annotation_sets DROP CONSTRAINT IF EXISTS annotation_sets_version_id_uk;
ALTER TABLE dataset_splits  DROP CONSTRAINT IF EXISTS dataset_splits_version_id_uk;
