-- =====================================================================================
-- 0014_outbox.down.sql
--
-- `medos/db/migrate.py` NEVER runs a down migration. This file exists so the forward
-- migration can be tested against a real database -- applied, dropped, re-applied -- which
-- is how `tests/integration/test_queue_parity.py` proves that `0014_outbox.up.sql` is
-- idempotent under a rebuild and that its DO-block assertions actually run.
--
-- Dropping `job_outbox` is loss-bearing in a way worth stating: an unpublished row is a
-- dispatch that no consumer has seen, and `MOS-EXEC-041` clause 5 forbids deleting one.
-- DROP TABLE does not fire the row trigger that enforces that, so running this file
-- against a deployment with pending rows silently discards those dispatches; the jobs
-- stay QUEUED with nothing to deliver them. Recovery from a bad forward migration is
-- restore-from-backup (`medos/db/migrate.py`), not this file.
-- =====================================================================================

DROP TRIGGER IF EXISTS job_outbox_guard_trg ON job_outbox;
DROP TABLE IF EXISTS job_outbox;
DROP FUNCTION IF EXISTS job_outbox_guard();
