-- =====================================================================================
-- 0009_validation_reports.down.sql
--
-- NOT A PRODUCTION PATH. `medos.db.migrate` never runs a down migration and says so in
-- its module docstring: recovery from a bad forward migration is restore-from-backup.
-- This file exists so that `0009_validation_reports.up.sql` can be applied, torn down and
-- applied again against a REAL database inside one test, which is the only way to prove a
-- forward migration is repeatable rather than accidentally idempotent.
--
-- Dropping this table destroys signed evidence and every record that a claim was ever
-- made about a clinical artifact. MOS-EVID-126 forbids deleting a single report; dropping
-- the table is that operation applied to all of them at once. Running this against a
-- deployment is data destruction, not a rollback.
--
-- `sha256_digest` is NOT dropped: 0006 created it under the same guard and other tables
-- in this release block depend on it. Dropping a shared domain because the migration that
-- happened to guard-create it is being reversed is how one teardown takes out four tables.
-- =====================================================================================

DROP TABLE IF EXISTS validation_reports;
