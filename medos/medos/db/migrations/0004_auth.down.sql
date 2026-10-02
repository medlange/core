-- =====================================================================================
-- 0004_auth.down.sql -- the reverse of 0004_auth.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- Dropping `api_keys` destroys every credential the deployment has issued. Nothing can
-- reconstruct them -- MOS-SEC-010 means the plaintexts are gone and the hashes are
-- one-way -- so the recovery from running this is "re-issue every key and re-point every
-- integration", not "restore the column". The supported recovery from a bad forward
-- migration is restore-from-backup (chapter 12 section 12.15).
--
-- Unlike 0002's down-migration this one has no guard on row count. There is no analogue
-- of the seeded backfill tenant here: an `api_keys` row is always something an operator
-- deliberately minted, so "is it safe to drop" has no structural answer, only a human
-- one.
-- =====================================================================================

SET lock_timeout = '3s';

DROP TRIGGER IF EXISTS api_keys_expiry_only_shortens ON api_keys;
DROP TRIGGER IF EXISTS api_keys_immutable ON api_keys;
DROP POLICY  IF EXISTS api_keys_tenant_isolation ON api_keys;

DROP TABLE IF EXISTS api_keys;

DROP FUNCTION IF EXISTS api_keys_expiry_only_shortens();
DROP FUNCTION IF EXISTS scope_is_wellformed(text[]);
