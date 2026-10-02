-- =====================================================================================
-- 0006_evidence.down.sql -- the reverse of 0006_evidence.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- `medos.db.migrate` never runs a down-migration (see its module docstring) and the
-- supported recovery from a bad forward migration is restore-from-backup.
--
-- Read the consequence before running this anywhere real. `dataset_versions` is the
-- content address of every cohort any `EvaluationRun`, `ValidationReport` or
-- `DeploymentGateDecision` in the deployment was computed against (MOS-EVID-007,
-- MOS-EVID-015). Dropping it does not merely lose rows: every published number becomes
-- unattributable to a cohort, and MOS-EVID-014's rule that a defective version is MARKED
-- rather than destroyed exists precisely because the marking is what a reader of an old
-- report needs. Unlike a projection row, none of it can be recomputed -- the manifest
-- objects survive in the object store but nothing left in the database says which
-- version, which tenant or which licence they belonged to.
--
-- `sha256_digest` is deliberately NOT dropped. Several migrations in this release block
-- were authored in parallel and any of them may have created it or may depend on it; it
-- is a domain with no state and leaving it is free. Same reasoning as 0005's treatment of
-- `uuid_generate_v7()`.
-- =====================================================================================

SET lock_timeout = '3s';

-- Triggers first: `forbid_mutation` on DELETE would otherwise refuse nothing (DROP TABLE
-- fires no row triggers) but the explicit order documents the seal being lifted before
-- the tables it sealed.
DROP TRIGGER IF EXISTS annotations_frozen           ON annotations;
DROP TRIGGER IF EXISTS annotation_readers_frozen    ON annotation_readers;
DROP TRIGGER IF EXISTS annotation_sets_frozen       ON annotation_sets;
DROP TRIGGER IF EXISTS dataset_split_members_frozen ON dataset_split_members;
DROP TRIGGER IF EXISTS dataset_splits_frozen        ON dataset_splits;
DROP TRIGGER IF EXISTS dataset_cases_frozen         ON dataset_cases;
DROP TRIGGER IF EXISTS dataset_versions_no_delete   ON dataset_versions;
DROP TRIGGER IF EXISTS dataset_versions_sealed      ON dataset_versions;
DROP TRIGGER IF EXISTS dataset_versions_touch       ON dataset_versions;
DROP TRIGGER IF EXISTS datasets_tenant_immutable    ON datasets;
DROP TRIGGER IF EXISTS datasets_touch               ON datasets;

DROP POLICY IF EXISTS annotations_tenant_isolation           ON annotations;
DROP POLICY IF EXISTS annotation_readers_tenant_isolation    ON annotation_readers;
DROP POLICY IF EXISTS annotation_sets_tenant_isolation       ON annotation_sets;
DROP POLICY IF EXISTS dataset_split_members_tenant_isolation ON dataset_split_members;
DROP POLICY IF EXISTS dataset_splits_tenant_isolation        ON dataset_splits;
DROP POLICY IF EXISTS dataset_cases_tenant_isolation         ON dataset_cases;
DROP POLICY IF EXISTS dataset_versions_tenant_isolation      ON dataset_versions;
DROP POLICY IF EXISTS datasets_tenant_isolation              ON datasets;

-- Child before parent: every FK here is ON DELETE RESTRICT on purpose.
DROP TABLE IF EXISTS annotations;
DROP TABLE IF EXISTS annotation_readers;
DROP TABLE IF EXISTS annotation_sets;
DROP TABLE IF EXISTS dataset_split_members;
DROP TABLE IF EXISTS dataset_splits;
DROP TABLE IF EXISTS dataset_cases;
DROP TABLE IF EXISTS dataset_versions;
DROP TABLE IF EXISTS datasets;

DROP FUNCTION IF EXISTS forbid_evidence_mutation();
