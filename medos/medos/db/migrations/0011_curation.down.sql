-- =====================================================================================
-- 0011_curation.down.sql -- the reverse of 0011_curation.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- `medos.db.migrate` never runs a down-migration and the supported recovery from a bad
-- forward migration is restore-from-backup.
--
-- Read the consequence before running this anywhere real. `curation_decisions` is the
-- record of a named human's decision about a case (`MOS-TRAIN-080`) and
-- `corpus_stratification_reports` is the record that a cohort passed the check which
-- permitted it to seal (`MOS-TRAIN-088`). `MOS-TRAIN-081` requires the exclusion reasons
-- to be reproduced in every `ValidationReport` citing the resulting `DatasetVersion`, so
-- dropping these tables does not merely lose rows -- it makes every such report
-- unreproducible, permanently, while the reports themselves stay valid-looking. None of
-- it can be recomputed: the sealed manifest shows only what survived.
--
-- `training_use_allowed` is NOT reset here. It is 0002's column and this migration did
-- not create it; what IS dropped is the constraint trigger that keeps it honest, so a
-- deployment that runs this script is left with a tenant flag whose instrument no longer
-- exists. Set it to false by hand before running this, or do not run this.
--
-- `sha256_digest` is deliberately NOT dropped, on 0006's reasoning: a domain with no
-- state, possibly created by a sibling migration, free to leave in place.
-- =====================================================================================

SET lock_timeout = '3s';

-- Triggers first. DROP TABLE fires no row triggers, so the order is documentation of the
-- seal being lifted before the tables it sealed -- except `tenants_training_policy_required`,
-- which is attached to a table this migration does NOT drop and must go explicitly.
DROP TRIGGER IF EXISTS tenants_training_policy_required ON tenants;

DROP TRIGGER IF EXISTS corpus_stratification_reports_append_only
  ON corpus_stratification_reports;
DROP TRIGGER IF EXISTS corpus_stratification_reports_tenant_immutable
  ON corpus_stratification_reports;
DROP TRIGGER IF EXISTS curation_decisions_append_only      ON curation_decisions;
DROP TRIGGER IF EXISTS curation_decisions_tenant_immutable ON curation_decisions;
DROP TRIGGER IF EXISTS harvest_candidates_identity_sealed  ON harvest_candidates;
DROP TRIGGER IF EXISTS harvest_candidates_tenant_immutable ON harvest_candidates;
DROP TRIGGER IF EXISTS harvest_batches_identity_sealed     ON harvest_batches;
DROP TRIGGER IF EXISTS harvest_batches_tenant_immutable    ON harvest_batches;
DROP TRIGGER IF EXISTS harvest_batches_touch               ON harvest_batches;
DROP TRIGGER IF EXISTS sampling_plans_sealed               ON sampling_plans;
DROP TRIGGER IF EXISTS sampling_plans_tenant_immutable     ON sampling_plans;
DROP TRIGGER IF EXISTS training_data_policies_revocation   ON training_data_policies;
DROP TRIGGER IF EXISTS training_data_policies_sealed       ON training_data_policies;
DROP TRIGGER IF EXISTS training_data_policies_tenant_immutable ON training_data_policies;
DROP TRIGGER IF EXISTS training_data_policies_touch        ON training_data_policies;

DROP POLICY IF EXISTS corpus_stratification_reports_tenant_isolation
  ON corpus_stratification_reports;
DROP POLICY IF EXISTS curation_decisions_tenant_isolation     ON curation_decisions;
DROP POLICY IF EXISTS harvest_candidates_tenant_isolation     ON harvest_candidates;
DROP POLICY IF EXISTS harvest_batches_tenant_isolation        ON harvest_batches;
DROP POLICY IF EXISTS sampling_plans_tenant_isolation         ON sampling_plans;
DROP POLICY IF EXISTS training_data_policies_tenant_isolation ON training_data_policies;

-- The view first: it depends on two of the tables below.
DROP VIEW IF EXISTS curation_batch_items;

-- Child before parent.
DROP TABLE IF EXISTS corpus_stratification_reports;
DROP TABLE IF EXISTS curation_decisions;
DROP TABLE IF EXISTS harvest_candidates;
DROP TABLE IF EXISTS harvest_batches;
DROP TABLE IF EXISTS sampling_plans;
DROP TABLE IF EXISTS training_data_policies;

DROP FUNCTION IF EXISTS clear_training_use_on_revocation();
DROP FUNCTION IF EXISTS assert_training_policy_live();
DROP FUNCTION IF EXISTS forbid_curation_mutation();

-- Section 7b of the up migration, reversed: 0006's equality is restored verbatim. This is
-- the one object this migration alters rather than creates, and leaving the relaxed form
-- behind would mean a database that had been down-migrated was not the database 0006
-- built. A parentless `derivation` written while 0011 was applied would now fail the
-- restored CHECK, so the ALTER is written NOT VALID and validated separately: an operator
-- reversing this migration finds out about such a row instead of being blocked by it.
ALTER TABLE dataset_versions DROP CONSTRAINT IF EXISTS dataset_versions_derivation;
ALTER TABLE dataset_versions ADD CONSTRAINT dataset_versions_derivation
  CHECK ((parent_version_id IS NULL) = (derivation IS NULL)) NOT VALID;
