-- =====================================================================================
-- 0012_artifacts.down.sql
--
-- `medos/db/migrate.py` NEVER runs this. Its module docstring says why: "No
-- down-migrations are ever run by it. The `.down.sql` files exist so the forward
-- migration can be tested against a real database ... not so a production deployment can
-- reverse one." Recovery from a bad forward migration is restore-from-backup.
--
-- For this migration the point is sharper than usual. Dropping `artifacts` destroys the
-- record of which signed model version produced which patient's result, and
-- `registry_changelog` is the only thing from which `MOS-REG-011`'s snapshot -- and
-- therefore `MOS-REG-071`'s offline replay of a job's resolution -- can be rebuilt. This
-- file exists for `test_artifacts.py`'s throwaway database and for nothing else.
--
-- The three domains are NOT dropped: they are shared with 0006-0011 and a domain with no
-- state is free to leave in place (the 0011 precedent).
-- =====================================================================================

DROP TRIGGER IF EXISTS artifacts_changelog_append ON artifacts;
DROP TRIGGER IF EXISTS artifacts_lifecycle ON artifacts;
DROP TRIGGER IF EXISTS artifacts_touch ON artifacts;
DROP TRIGGER IF EXISTS artifacts_immutable ON artifacts;
DROP TRIGGER IF EXISTS registry_changelog_append_only ON registry_changelog;

DROP TABLE IF EXISTS artifacts;
DROP TABLE IF EXISTS artifact_manifest_schemas;
DROP TABLE IF EXISTS registry_changelog;
DROP SEQUENCE IF EXISTS registry_epoch;

DROP FUNCTION IF EXISTS artifacts_changelog();
DROP FUNCTION IF EXISTS artifacts_lifecycle_guard();
DROP FUNCTION IF EXISTS artifacts_forbid_change();
DROP FUNCTION IF EXISTS registry_changelog_forbid_change();
