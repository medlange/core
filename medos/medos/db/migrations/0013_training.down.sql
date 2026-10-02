-- =====================================================================================
-- 0013_training.down.sql -- the reverse of 0013_training.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- `medos.db.migrate` never runs a down-migration and the supported recovery from a bad
-- forward migration is restore-from-backup.
--
-- READ THE CONSEQUENCE BEFORE RUNNING THIS ANYWHERE REAL. `training_runs` is the ONLY
-- record of how a served `ModelVersion` came to exist (`MOS-TRAIN-124`), and
-- `MOS-TRAIN-192` makes the absence of such a record a sentence that a `ValidationReport`
-- must carry for the artifact's whole life. Dropping this table does not merely lose
-- rows: every `ModelVersion` produced by this pipeline silently becomes an artifact with
-- no producing run, indistinguishable from the 0.1.0 pleural-effusion model that was
-- trained outside the pipeline -- and its reports keep saying otherwise. None of it is
-- recomputable.
--
-- `split_test_exposure` is worse in one specific way: `MOS-TRAIN-216` says the counter
-- "MUST NOT be reset", and dropping the table is a reset. A deployment that runs this
-- script has spent its test partitions and no longer knows it.
--
-- THE TWO CHANGES TO `deployments` ARE REVERSED TO 0008'S STATE, which means the
-- table-level `UPDATE` grant comes back and `MOS-TRAIN-180`'s grant half is open again.
-- The trigger half (`deployments_immutable_identity`, 0008) is NOT dropped here, because
-- this migration did not create it; so a reversed deployment is still protected by the
-- trigger and no longer by the grant. That is 0008's posture, restored exactly.
--
-- `sha256_digest` is deliberately NOT dropped, on 0006's and 0011's reasoning: a domain
-- with no state, possibly created by a sibling migration, free to leave in place.
-- =====================================================================================

SET lock_timeout = '3s';

-- Triggers on tables this migration does NOT drop must go explicitly. DROP TABLE fires
-- no row triggers, so everything else below is documentation of the seal being lifted
-- before the tables it sealed.
DROP TRIGGER IF EXISTS deployments_no_auto_promote ON deployments;

DROP TRIGGER IF EXISTS capability_seed_variance_tenant_immutable
  ON capability_seed_variance;
DROP TRIGGER IF EXISTS capability_seed_variance_sealed    ON capability_seed_variance;
DROP TRIGGER IF EXISTS capability_seed_variance_no_delete ON capability_seed_variance;
DROP TRIGGER IF EXISTS conversion_runs_tenant_immutable   ON conversion_runs;
DROP TRIGGER IF EXISTS conversion_runs_sealed             ON conversion_runs;
DROP TRIGGER IF EXISTS conversion_runs_touch              ON conversion_runs;
DROP TRIGGER IF EXISTS conversion_runs_no_delete          ON conversion_runs;
DROP TRIGGER IF EXISTS configuration_searches_tenant_immutable ON configuration_searches;
DROP TRIGGER IF EXISTS configuration_searches_sealed      ON configuration_searches;
DROP TRIGGER IF EXISTS configuration_searches_touch       ON configuration_searches;
DROP TRIGGER IF EXISTS configuration_searches_no_delete   ON configuration_searches;
DROP TRIGGER IF EXISTS training_runs_tenant_immutable     ON training_runs;
DROP TRIGGER IF EXISTS training_runs_sealed               ON training_runs;
DROP TRIGGER IF EXISTS training_runs_touch                ON training_runs;
DROP TRIGGER IF EXISTS training_runs_no_delete            ON training_runs;
DROP TRIGGER IF EXISTS split_test_exposure_tenant_immutable ON split_test_exposure;
DROP TRIGGER IF EXISTS split_test_exposure_no_reset       ON split_test_exposure;
DROP TRIGGER IF EXISTS split_test_exposure_no_delete      ON split_test_exposure;
DROP TRIGGER IF EXISTS split_test_exposure_touch          ON split_test_exposure;

DROP POLICY IF EXISTS split_test_exposure_tenant_isolation      ON split_test_exposure;
DROP POLICY IF EXISTS capability_seed_variance_tenant_isolation ON capability_seed_variance;
DROP POLICY IF EXISTS conversion_runs_tenant_isolation          ON conversion_runs;
DROP POLICY IF EXISTS configuration_searches_tenant_isolation   ON configuration_searches;
DROP POLICY IF EXISTS training_runs_tenant_isolation            ON training_runs;

-- The circular reference has to be cut before either table can be dropped.
ALTER TABLE IF EXISTS training_runs DROP CONSTRAINT IF EXISTS training_runs_search_fk;

DROP TABLE IF EXISTS split_test_exposure;
DROP TABLE IF EXISTS capability_seed_variance;
DROP TABLE IF EXISTS conversion_runs;
DROP TABLE IF EXISTS configuration_searches;
DROP TABLE IF EXISTS training_runs;

DROP FUNCTION IF EXISTS assert_no_auto_promote_beside_clinical();
DROP FUNCTION IF EXISTS split_test_exposure_monotonic();
DROP FUNCTION IF EXISTS training_runs_guard();
DROP FUNCTION IF EXISTS forbid_training_mutation();

-- Section 7b of the up migration, reversed: 0008's grant is restored verbatim. Leaving
-- the narrowed column list behind would mean a database that had been down-migrated was
-- not the database 0008 built.
REVOKE UPDATE ON deployments FROM medicalos_app;
GRANT UPDATE ON deployments TO medicalos_app;
