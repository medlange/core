-- =====================================================================================
-- 0015_seal_runs.down.sql -- the reverse of 0015_seal_runs.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- `medos.db.migrate` never runs a down-migration and the supported recovery from a bad
-- forward migration is restore-from-backup.
--
-- Read the consequence before running this anywhere real. `seal_runs` is the ONLY record
-- of the act of sealing: which operator sealed which batch, which nine checks ran, which
-- of them were SKIPPED for want of pixel evidence (`MOS-EVID-037`), and what the
-- resulting `DatasetVersion` and `DatasetSplit` were. The sealed objects survive this
-- script and the record of how they came to exist does not -- so every cohort in the
-- database keeps looking sealed while the evidence that L3 and L4 did not run
-- disappears. That is precisely the quiet downgrade `MOS-API-112` designed the
-- `check_battery` to make impossible.
--
-- `sha256_digest` is deliberately NOT dropped, on 0006's and 0011's reasoning: a domain
-- with no state, possibly created by a sibling migration, free to leave in place.
-- =====================================================================================

SET lock_timeout = '3s';

DROP TRIGGER IF EXISTS seal_runs_tenant_immutable ON seal_runs;
DROP TRIGGER IF EXISTS seal_runs_identity_sealed  ON seal_runs;
DROP TRIGGER IF EXISTS seal_runs_touch            ON seal_runs;

DROP POLICY IF EXISTS seal_runs_tenant_isolation ON seal_runs;

DROP TABLE IF EXISTS seal_runs;

DROP FUNCTION IF EXISTS seal_runs_identity_sealed();
