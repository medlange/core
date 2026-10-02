-- =====================================================================================
-- 0003_audit_provenance.down.sql
--
-- Symmetric to the up migration, and honest about the one thing a down migration of an
-- append-only table cannot be: reversible without loss. Dropping `audit_events` destroys
-- the audit trail, which is exactly what MOS-SEC-154 forbids -- "a partition MUST NOT be
-- dropped; when retention expires it MUST be exported as a signed archive, the archive
-- digest recorded in a permanently retained `audit_archives` row, and only then
-- detached" -- and MOS-STORE-337 says the same for `result_provenance`.
--
-- This file therefore exists for ONE purpose: rolling back a failed migration in a
-- development database, before any row has been written. It refuses to run otherwise.
-- Removing these tables from a live deployment needs an export first, and that is an
-- operational procedure, not a down migration.
-- =====================================================================================

SET lock_timeout = '3s';

DO $$
DECLARE n bigint;
BEGIN
  SELECT count(*) INTO n FROM audit_events;
  IF n > 0 THEN
    RAISE EXCEPTION
      'refusing to drop audit_events: % row(s) present. MOS-SEC-154 requires a signed '
      'export before any audit data is detached; this down migration is for an empty '
      'development database only.', n
      USING ERRCODE = '42501';
  END IF;
  SELECT count(*) INTO n FROM result_provenance;
  IF n > 0 THEN
    RAISE EXCEPTION
      'refusing to drop result_provenance: % row(s) present. MOS-SAFE-082 makes the '
      'record the only thing that ties a Result to its exact inputs and outputs.', n
      USING ERRCODE = '42501';
  END IF;
END $$;

DROP TABLE IF EXISTS result_provenance;
DROP TABLE IF EXISTS audit_events;          -- partitions go with the parent

DROP FUNCTION IF EXISTS provenance_verify_chain(uuid, bigint);
DROP FUNCTION IF EXISTS result_provenance_chain();
DROP FUNCTION IF EXISTS provenance_genesis_hash(uuid);
DROP FUNCTION IF EXISTS audit_verify_chain(uuid, bigint);
DROP FUNCTION IF EXISTS audit_events_chain();
DROP FUNCTION IF EXISTS audit_events_ensure_partition(date);
DROP FUNCTION IF EXISTS audit_events_harden(text);
DROP FUNCTION IF EXISTS audit_events_immutable();
DROP FUNCTION IF EXISTS audit_row_hash(jsonb);
DROP FUNCTION IF EXISTS audit_genesis_hash(uuid);
DROP FUNCTION IF EXISTS jcs_canonical(jsonb);
