-- =====================================================================================
-- 0008_acceptance_gate.down.sql
--
-- NOT RUN IN PRODUCTION. `medos/db/migrate.py` never executes a `.down.sql`; the file
-- exists so `tests/integration/test_deployment_gate.py` can apply the forward migration
-- against a real database and take it back out again, which is the only way to prove the
-- forward one applies from empty more than once.
--
-- Recovery from a bad forward migration is restore-from-backup, and on THIS migration
-- that is not a preference. Dropping `deployment_gate_decisions` destroys the record that
-- the gate ran at all, which is MOS-EVID-091's whole subject ("Every invocation MUST be
-- persisted, whatever the outcome") and MOS-STORE-302b's ("so that 'why was this
-- deployment allowed' is answerable from rows"). Dropping `deployments` destroys the
-- answer to "which version was live when this Result was produced", which every
-- `result_provenance` row depends on. Neither is reconstructible from anything else in
-- the database.
--
-- Reverse creation order, so the foreign keys unwind cleanly.
-- =====================================================================================

DROP TABLE IF EXISTS deployment_gate_decisions;
DROP TABLE IF EXISTS deployments;
DROP TABLE IF EXISTS tenant_acceptance_bindings;
DROP TABLE IF EXISTS acceptance_criteria;

DROP FUNCTION IF EXISTS forbid_training_cohort_binding();

-- `forbid_evidence_mutation()`, `forbid_column_change()`, `touch_updated_at()` and
-- `current_tenant_id()` are NOT dropped: 0006, 0007 and schema.sql created them and other
-- tables still use them.
