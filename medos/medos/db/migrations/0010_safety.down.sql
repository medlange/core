-- =====================================================================================
-- 0010_safety.down.sql -- the reverse of 0010_safety.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- `medos.db.migrate` never runs a down-migration and the supported recovery from a bad
-- forward migration is restore-from-backup.
--
-- Read the consequence before running this anywhere real:
--
--   * `result_reviews` is the record that a qualified human looked at a clinical result
--     and what they concluded (MOS-SAFE-058). MOS-SAFE-064 says a reviewer's disagreement
--     is data, not an erasure; dropping the table erases every disagreement ever
--     recorded, and `results.review_status` is left frozen at whatever the last trigger
--     firing wrote -- a result that reads ACCEPTED with nothing behind it.
--   * `applicability_envelopes` is the clinical claim each running service version was
--     judged against. Every `envelope_decisions` row and every `ValidationReport` that
--     cites an `envelope_digest` becomes unresolvable.
--
-- `sha256_digest` and `uuid_generate_v7()` are deliberately NOT dropped: several
-- migrations in this release block were authored in parallel, any of them may have
-- created or may depend on either, and both are stateless. Same reasoning as 0005's
-- treatment of `uuid_generate_v7()` and 0006's of `sha256_digest`.
--
-- `tenants.marginal_policy` IS dropped, because this migration is the only thing that
-- reads it and a column with no reader is worse than no column.
-- =====================================================================================

SET lock_timeout = '3s';

-- Triggers first. DROP TABLE fires no row triggers, so this changes nothing mechanically;
-- it documents the seal being lifted before the tables it sealed, and it makes a partial
-- run (down to here) leave a coherent database rather than a table nobody may write.
DROP TRIGGER IF EXISTS result_reviews_sync                ON result_reviews;
DROP TRIGGER IF EXISTS result_reviews_identity_sealed     ON result_reviews;
DROP TRIGGER IF EXISTS result_reviews_no_delete           ON result_reviews;
DROP TRIGGER IF EXISTS result_reviews_terminal_immutable  ON result_reviews;
DROP TRIGGER IF EXISTS result_reviews_touch               ON result_reviews;
DROP TRIGGER IF EXISTS envelope_decisions_append_only     ON envelope_decisions;
DROP TRIGGER IF EXISTS applicability_envelopes_immutable  ON applicability_envelopes;

-- `envelope_decisions` references `applicability_envelopes`, so it goes first.
DROP TABLE IF EXISTS result_reviews;
DROP TABLE IF EXISTS envelope_decisions;
DROP TABLE IF EXISTS applicability_envelopes;

DROP FUNCTION IF EXISTS sync_result_review_status();
DROP FUNCTION IF EXISTS forbid_safety_mutation();

ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_marginal_policy_ck;
ALTER TABLE tenants DROP COLUMN IF EXISTS marginal_policy;
