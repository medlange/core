-- =====================================================================================
-- 0005_gateway.down.sql -- the reverse of 0005_gateway.up.sql.
--
-- IT EXISTS SO THE FORWARD MIGRATION CAN BE TESTED, NOT SO IT CAN BE RUN IN PRODUCTION.
-- `medos.db.migrate` never runs a down-migration (see its module docstring) and the
-- supported recovery from a bad forward migration is restore-from-backup.
--
-- Read the consequence before running this anywhere real: dropping `studies` destroys the
-- record of who OWNS each study (MOS-DATA-010), and unlike a job row that record cannot
-- be recomputed from the PACS afterwards, because the PACS never knew. The tenancy of
-- every study in the deployment would have to be re-established by a human.
--
-- `uuid_generate_v7()` is deliberately NOT dropped: 0003 and 0004 were authored in
-- parallel and either may have created it, so dropping it here would break the other.
-- It is a pure function with no state; leaving it is free.
-- =====================================================================================

SET lock_timeout = '3s';

DROP TRIGGER IF EXISTS studies_touch            ON studies;
DROP TRIGGER IF EXISTS studies_tenant_immutable ON studies;
DROP TRIGGER IF EXISTS patients_touch            ON patients;
DROP TRIGGER IF EXISTS patients_tenant_immutable ON patients;

DROP POLICY IF EXISTS studies_tenant_isolation  ON studies;
DROP POLICY IF EXISTS patients_tenant_isolation ON patients;

DROP FUNCTION IF EXISTS study_owner_tenant(dicom_uid);

DROP TABLE IF EXISTS studies;
DROP TABLE IF EXISTS patients;
DROP TABLE IF EXISTS pacs_backends;

-- Roles are cluster-wide, so the drop is guarded and tolerant: another database in the
-- same cluster may still hold a `studies` whose oracle this is.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'medicalos_study_oracle') THEN
    BEGIN
      DROP ROLE medicalos_study_oracle;
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'medicalos_study_oracle still owns objects elsewhere; left in place';
    END;
  END IF;
END $$;
