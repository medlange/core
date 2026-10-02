# SPDX-License-Identifier: Apache-2.0
"""The evidence schema: migration 0006, sealing, split freezing, annotation sets.

docs/spec/15-delivery.md section 15.2.5 (tag 0.2.0), chapter 7 sections 7.2-7.5, and the
chapter 17 elements `MOS-TRAIN-190` places in the same release.

WHAT THIS FILE IS TRYING TO CATCH, in the order the release brief names them:

  1. A migration set that no longer applies from empty. The previous block's defect was
     found exactly this way, so `test_evidence_migration_applies_from_empty` builds a
     brand-new database, runs `schema.sql` plus every migration including 0006, and then
     asserts the structural invariants rather than trusting that `CREATE TABLE` returning
     no error means the schema is right.
  2. A sealed `DatasetVersion` that can be edited. `MOS-EVID-013` puts the enforcement in
     the database precisely because application-level sealing is one forgotten code path
     away from nothing, so every assertion here goes through SQL and not through the
     repository.
  3. A split with one patient on both sides. `MOS-EVID-034` L1.
  4. An unbalanced cohort. `MOS-TRAIN-088`, and the refusal must NAME the stratum --
     "C2 failed" is not actionable, "C2 on stratum scanner_model = SIEMENS|Sensation 16"
     is.

SKIP DISCIPLINE: `tests/_support/skips.py` only. There is no raw `pytest.skip` here, and
the module declares `postgres` through `tests/_support/stack.py`'s `tests/integration`
prefix, so `--require-stack` probes it.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db.conn import apply_schema
from medos.db.migrate import discover
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import digest as ev_digest
from medos.evidence import leakage as ev_leak
from medos.evidence import repo as ev_repo
from medos.evidence import stratification as ev_strat
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.profile import acquisition_profile
from medos.evidence.store import InMemoryManifestStore
from medos.sdk.refusal import FreezeRefused, SealRefused
from psycopg.rows import dict_row

BUCKET = "medos-evidence"
SALT = b"test-tenant-salt-not-a-real-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"

EVIDENCE_TABLES = (
    "datasets",
    "dataset_versions",
    "dataset_cases",
    "dataset_splits",
    "dataset_split_members",
    "annotation_sets",
    "annotation_readers",
    "annotations",
)

# Section 12.12's "sealed columns" column, for the tables this migration owns.
FULLY_SEALED = (
    "dataset_cases",
    "dataset_splits",
    "dataset_split_members",
    "annotation_sets",
    "annotation_readers",
    "annotations",
)


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture()
def evidence_db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One connection with the default tenant bound and the eight tables emptied.

    TRUNCATE and not DELETE: every table here carries `forbid_mutation` on DELETE
    (`MOS-EVID-013`), and TRUNCATE does not fire row-level triggers -- so the seal holds
    for application code while the harness can still start clean. That is the same
    arrangement `job_events` has had since weeks 1-2.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(f"TRUNCATE {', '.join(EVIDENCE_TABLES)} CASCADE")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant

        reset_current_tenant(token)
        conn.close()


@pytest.fixture()
def store() -> InMemoryManifestStore:
    return InMemoryManifestStore()


# =====================================================================================
# Cohort builders
# =====================================================================================
def _uid(*parts: int) -> str:
    """A syntactically valid DICOM UID for the `dicom_uid` domain."""
    return "1.2.826.0.1.3680043.10.1." + ".".join(str(p) for p in parts)


def _pixel_digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _record(
    *,
    patient: int,
    study: int = 1,
    series: int = 1,
    institution: str,
    manufacturer: str,
    model: str,
    kernel_class: str,
    thickness: float = 1.25,
    year: int = 2023,
    instances: int = 40,
    pixel_seed: str | None = None,
    accession: str | None = None,
    corpus_generation: int = 0,
) -> SeriesRecord:
    pk = ev_digest.patient_key(SALT, "TCIA/TEST", f"P{patient:04d}")
    return SeriesRecord(
        patient_key=pk,
        study_instance_uid=_uid(patient, study),
        series_instance_uid=_uid(patient, study, series),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(patient, study, series, i) for i in range(instances)),
        series_pixel_digest=_pixel_digest(
            pixel_seed or f"{patient}/{study}/{series}"
        ),
        acquisition=Acquisition(
            slice_thickness_mm=thickness,
            pixel_spacing_mm=(0.703125, 0.703125),
            convolution_kernel="B30f",
            convolution_kernel_class=kernel_class,
            manufacturer=manufacturer,
            manufacturer_model_name=model,
            kvp=120.0,
            contrast_phase="non_contrast",
            z_coverage_mm=332.5,
            image_type=("ORIGINAL", "PRIMARY", "AXIAL"),
            patient_age_years=60 + patient,
            patient_sex="M" if patient % 2 else "F",
            body_part_examined="CHEST",
        ),
        institution_key=ev_digest.institution_key(SALT, institution),
        study_year=year,
        accession_number_hash=(
            ev_digest.accession_number_hash(SALT, accession) if accession else None
        ),
        corpus_generation=corpus_generation,
    )


def balanced_cohort(n_patients: int = 10) -> list[SeriesRecord]:
    """A cohort that PASSES C1, C2, C3 and C7. Two sites, two scanners, two kernels.

    Deliberately the minimum that passes, so that a change to any default bound in
    `MOS-TRAIN-088` shows up here as a failure rather than being absorbed by slack.
    """
    out = []
    for p in range(n_patients):
        half = p % 2
        out.append(
            _record(
                patient=p,
                institution=f"SITE-{half}",
                manufacturer="SIEMENS" if half else "GE MEDICAL SYSTEMS",
                model="Sensation 16" if half else "Discovery CT750 HD",
                kernel_class="soft" if half else "standard",
                thickness=1.25 if half else 2.5,
                year=2022 + half,
                accession=f"ACC{p:04d}",
            )
        )
    return out


def single_site_cohort(n_patients: int = 10) -> list[SeriesRecord]:
    """One site, one scanner, one kernel class: the corpus `MOS-TRAIN-088` exists for.

    Every per-case number this cohort would produce is honest. The only place the defect
    is visible is in its composition, which is why the check runs at seal.
    """
    return [
        _record(
            patient=p,
            institution="SITE-ONLY",
            manufacturer="SIEMENS",
            model="Sensation 16",
            kernel_class="soft",
            accession=f"ACC{p:04d}",
        )
        for p in range(n_patients)
    ]


def _make_dataset(conn, *, purpose: str = "evaluation", slug: str | None = None):
    return ev_repo.create_dataset(
        conn,
        slug=slug or f"cohort-{secrets.token_hex(4)}",
        display_name="test cohort",
        purpose=purpose,
        custodian="TCIA/TEST",
        created_by=OPERATOR,
    )


def _assert_database_refuses(conn, statement: str, params: tuple = ()) -> Any:
    """Execute, and require the refusal to come from the SEAL and not from luck.

    `forbid_evidence_mutation()` raises SQLSTATE `MOS05` and `forbid_column_change()`
    raises `MOS06`. Both are in the implementation-defined class, so psycopg maps them to
    the generic `DatabaseError` rather than to a named subclass -- asserting on the
    exception CLASS would pass on a typo'd column name or a foreign-key violation, which
    is not the claim. The SQLSTATE is the claim.
    """
    with pytest.raises(psycopg.DatabaseError) as exc:
        conn.execute(statement, params)
    assert exc.value.sqlstate in ("MOS05", "MOS06"), (
        f"expected the seal (MOS05/MOS06) to refuse this, got SQLSTATE "
        f"{exc.value.sqlstate}: {exc.value}"
    )
    conn.rollback()
    return exc.value


def _seal(conn, store, records, *, purpose="evaluation", **kw):
    ds = kw.pop("dataset", None) or _make_dataset(conn, purpose=purpose)
    return ds, ev_repo.seal_dataset_version(
        conn,
        dataset_id=ds.id,
        records=records,
        store=store,
        bucket=BUCKET,
        sealed_by=OPERATOR,
        deidentification_status=kw.pop("deidentification_status", "public_deidentified"),
        deid_policy_id=kw.pop("deid_policy_id", "ps315-basic/v4"),
        uid_mapping_table_id=kw.pop("uid_mapping_table_id", "uidmap/v1"),
        **kw,
    )


# =====================================================================================
# 1. The migration applies from EMPTY
# =====================================================================================
def _swap_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"


def test_evidence_migration_applies_from_empty(pg_dsn: str) -> None:
    """schema.sql then every migration, on a database created seconds ago.

    MOS-STORE-214: "every migration set begins at the bootstrap DDL". The last release
    block's defect was a migration set that worked on a database that had grown into it
    and failed from scratch, which is the only state a new deployment is ever in.

    A dedicated throwaway database rather than the session's `pristine_dsn`, because that
    fixture is session-scoped and `test_queue.py` has already applied a schema to it: a
    second "from empty" test sharing it would be testing "from whatever the first test
    left", which is the opposite of the claim. The session DSN is used only to reach the
    server -- taking the credentials that already work rather than re-deriving them is
    how this test avoids becoming a second definition of the harness's connection string.
    """
    name = f"medos_evidence_{secrets.token_hex(6)}"
    with psycopg.connect(pg_dsn, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _swap_dbname(pg_dsn, name)
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            before = conn.execute(
                "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public'"
            ).fetchone()
            assert before["n"] == 0, "the fixture database is not empty"

            apply_schema(conn)  # schema.sql (0001_baseline) + every migration in order

            # --- the ledger records 0006, and every migration on disk ran -------------
            applied = {
                r["version"]
                for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            on_disk = {m.version for m in discover()}
            assert "0006_evidence" in applied, (
                f"0006_evidence did not run from empty; applied = {sorted(applied)}"
            )
            assert on_disk <= applied, f"migrations on disk that did not run: "\
                                       f"{sorted(on_disk - applied)}"

            # --- the eight tables exist ---------------------------------------------
            tables = {
                r["tablename"]
                for r in conn.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                ).fetchall()
            }
            missing = set(EVIDENCE_TABLES) - tables
            assert not missing, f"0006 did not create: {sorted(missing)}"

            # --- MOS-STORE-211 / MOS-EVID-007: the digest domain ---------------------
            dom = conn.execute(
                "SELECT count(*) AS n FROM pg_type WHERE typname = 'sha256_digest'"
            ).fetchone()
            assert dom["n"] == 1

            # --- MOS-EVID-012 / MOS-SEC-077: forced row security on all eight --------
            rls = {
                r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"])
                for r in conn.execute(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = ANY(%s)",
                    (list(EVIDENCE_TABLES),),
                ).fetchall()
            }
            for t in EVIDENCE_TABLES:
                assert rls[t] == (True, True), (
                    f"{t} carries tenant_id without ENABLE + FORCE row security "
                    "(MOS-SEC-077, MOS-EVID-012)"
                )

            # --- MOS-EVID-013: every sealed table has a BEFORE UPDATE guard ----------
            for t in ("dataset_versions", *FULLY_SEALED):
                n = conn.execute(
                    "SELECT count(*) AS n FROM pg_trigger g JOIN pg_class c "
                    "ON c.oid = g.tgrelid WHERE c.relname = %s AND NOT g.tgisinternal "
                    "AND (g.tgtype & 16) <> 0",
                    (t,),
                ).fetchone()
                assert n["n"] >= 1, f"{t} is sealed but has no BEFORE UPDATE guard"

            # --- MOS-EVID-028: a split stores a manifest, never a seed ---------------
            seeds = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'dataset_splits' AND column_name IN "
                "('seed','rng_seed','random_seed','split_seed')"
            ).fetchall()
            assert seeds == [], (
                "MOS-EVID-028: the platform MUST NOT store a seed in place of a manifest"
            )

            # --- MOS-EVID-031: `val` is not a partition ------------------------------
            check = conn.execute(
                "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                "WHERE conname = 'dataset_split_members_partition_check'"
            ).fetchone()
            assert check is not None and "'val'" not in check["d"]
            for value in ("train", "tune", "test", "excluded"):
                assert f"'{value}'" in check["d"], f"{value} is not in the partition CHECK"

            # --- MOS-STORE-294: partition_level is pinned to 'patient' ---------------
            level = conn.execute(
                "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                "WHERE conname = 'dataset_splits_partition_level_check'"
            ).fetchone()
            assert level is not None and "'patient'" in level["d"]
    finally:
        with psycopg.connect(pg_dsn, autocommit=True, row_factory=dict_row) as admin2:
            admin2.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin2.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_down_migration_is_the_reverse_of_the_up(tmp_path: Path) -> None:
    """Every table, policy and trigger 0006 creates is named in 0006_evidence.down.sql.

    Not run against a database: the runner never executes a down-migration (see
    `medos.db.migrate`'s docstring). This is a textual completeness check, which is what
    the file is actually for -- a down-migration that forgets a table is discovered when
    someone tries to test the forward migration and the second attempt collides.
    """
    # parents[3], not [2]. `ev_repo` is `medos/medos/evidence/repo.py`; two levels up is
    # the PRODUCT directory `medos/`, and the literal below already carries `medos/medos/`,
    # so this resolved to `medos/medos/medos/db/` and raised FileNotFoundError.
    root = Path(ev_repo.__file__).resolve().parents[3]
    up = (root / "medos/medos/db/migrations/0006_evidence.up.sql").read_text(encoding="utf-8")
    down = (root / "medos/medos/db/migrations/0006_evidence.down.sql").read_text(
        encoding="utf-8"
    )
    for table in EVIDENCE_TABLES:
        assert f"CREATE TABLE {table} (" in up, f"0006 up does not create {table}"
        assert f"DROP TABLE IF EXISTS {table};" in down, (
            f"0006 down does not drop {table}"
        )
        assert f"{table}_tenant_isolation" in down, (
            f"0006 down does not drop {table}'s RLS policy"
        )


# =====================================================================================
# 2. A sealed version rejects mutation
# =====================================================================================
def test_sealed_version_rejects_mutation(evidence_db, store) -> None:
    """MOS-EVID-013 / MOS-EVID-014, asserted through SQL and not through the repository.

    "Sealing/freezing is enforced at the database level, not in application code." A test
    that went through `medos.evidence.repo` would prove only that the repository has no
    update method, which is a property of the repository and not of the seal.
    """
    _ds, sealed = _seal(evidence_db, store, balanced_cohort())
    conn = evidence_db

    # The content address itself.
    err = _assert_database_refuses(
        conn,
        "UPDATE dataset_versions SET manifest_digest = %s WHERE id = %s",
        ("sha256:" + "0" * 64, sealed.id),
    )
    assert "sealed after insert" in str(err)

    # And the counts, the profile and the de-identification provenance.
    for column, value in (
        ("case_count", 1),
        ("acquisition_profile", "{}"),
        ("deidentification_status", "identified"),
        ("sealed_by", "22222222-2222-2222-2222-222222222222"),
        ("manifest_object_key", "elsewhere"),
        ("dataset_id", "22222222-2222-2222-2222-222222222222"),
        ("version", 99),
    ):
        _assert_database_refuses(
            conn,
            f"UPDATE dataset_versions SET {column} = %s WHERE id = %s",
            (value, sealed.id),
        )

    # MOS-EVID-014: it may be MARKED defective. That is the one permitted transition.
    ev_repo.mark_defective(conn, version_id=sealed.id, reason="manifest object corrupted")
    row = conn.execute(
        "SELECT status, defect_reason, usable_for_new_runs FROM dataset_versions "
        "WHERE id = %s",
        (sealed.id,),
    ).fetchone()
    assert row["status"] == "DEFECTIVE"
    assert row["defect_reason"] == "manifest object corrupted"
    assert row["usable_for_new_runs"] is False
    conn.rollback()

    # And it is never deleted.
    err = _assert_database_refuses(
        conn, "DELETE FROM dataset_versions WHERE id = %s", (sealed.id,)
    )
    assert "sealed evidence" in str(err)
    assert "MUST NOT be edited" in str(err), (
        "the refusal must say what the rule is; the shared forbid_mutation() hint points "
        "at SSE resume, which is why 0006 declares its own"
    )


def test_the_seal_holds_for_the_application_role_as_well(
    evidence_db, store, pg_dsn: str
) -> None:
    """MOS-EVID-013's OTHER layer: the grant, proved as `medicalos_app`.

    "the evidence tables listed as sealed MUST have `UPDATE` and `DELETE` revoked from
    the application role for all columns except the explicitly mutable status columns".

    Every other assertion in this file runs as the bootstrap superuser, which bypasses
    row security and holds every grant -- so it proves the TRIGGER and nothing about the
    GRANT. This one connects as the role the API and the workers actually use, which is
    `NOBYPASSRLS` and owns nothing, and checks the two layers separately:

      * a column-level `UPDATE` grant lets it mark a version `DEFECTIVE` (MOS-EVID-014);
      * no grant at all lets it touch `case_count`, or `dataset_cases`, at all.

    And the tenant predicate, on the same connection: the same `SELECT` under a different
    `medicalos.tenant_id` returns nothing rather than another tenant's cohort.
    """
    from tests.integration.conftest import APP_PASSWORD

    _ds, sealed = _seal(evidence_db, store, balanced_cohort())
    evidence_db.commit()

    head = pg_dsn.split("://", 1)[-1].split("@", 1)[-1]
    app_dsn = f"postgresql://medicalos_app:{APP_PASSWORD}@{head}"
    with psycopg.connect(app_dsn, row_factory=dict_row, autocommit=False) as app:
        with app.transaction():
            app.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, DEFAULT_TENANT_ID))
            visible = app.execute(
                "SELECT count(*) AS n FROM dataset_versions"
            ).fetchone()
            assert visible["n"] == 1, "the app role cannot see its own tenant's cohort"

            # MOS-EVID-014's four mutable columns.
            app.execute(
                "UPDATE dataset_versions SET status = 'DEFECTIVE', "
                "defect_reason = 'marked by the app role' WHERE id = %s",
                (sealed.id,),
            )

        # Everything else is refused by the GRANT, before any trigger runs.
        for statement, params in (
            ("UPDATE dataset_versions SET case_count = 1 WHERE id = %s", (sealed.id,)),
            ("UPDATE dataset_cases SET instance_count = 1 WHERE dataset_version_id = %s",
             (sealed.id,)),
            ("DELETE FROM dataset_cases WHERE dataset_version_id = %s", (sealed.id,)),
            ("DELETE FROM dataset_versions WHERE id = %s", (sealed.id,)),
        ):
            with app.transaction():
                app.execute("SELECT set_config(%s, %s, true)",
                            (TENANT_GUC, DEFAULT_TENANT_ID))
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    app.execute(statement, params)

        # MOS-EVID-012: the same statement, another tenant, nothing at all.
        with app.transaction():
            app.execute(
                "SELECT set_config(%s, %s, true)",
                (TENANT_GUC, "99999999-9999-9999-9999-999999999999"),
            )
            other = app.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()
            assert other["n"] == 0, (
                "MOS-EVID-012: another tenant's binding sees this tenant's sealed cohort"
            )
            n_cases = app.execute("SELECT count(*) AS n FROM dataset_cases").fetchone()
            assert n_cases["n"] == 0


def test_manifest_rows_are_frozen_outright(evidence_db, store) -> None:
    """`dataset_cases`' sealed-column list in section 12.12 is "all"."""
    _ds, sealed = _seal(evidence_db, store, balanced_cohort())
    conn = evidence_db
    for statement, params in (
        ("UPDATE dataset_cases SET instance_count = 1 WHERE dataset_version_id = %s",
         (sealed.id,)),
        ("UPDATE dataset_cases SET series_pixel_digest = %s WHERE dataset_version_id = %s",
         ("sha256:" + "1" * 64, sealed.id)),
        ("DELETE FROM dataset_cases WHERE dataset_version_id = %s", (sealed.id,)),
    ):
        _assert_database_refuses(conn, statement, params)


def test_seal_is_idempotent_on_content(evidence_db, store) -> None:
    """MOS-TRAIN-209: re-running the seal on the same included set REUSES the version.

    "the pipeline MUST detect the collision against the `UNIQUE` constraint on
    `manifest_digest` and reuse the existing `DatasetVersion` rather than mint a second
    identity for the same bytes."
    """
    records = balanced_cohort()
    ds, first = _seal(evidence_db, store, records)
    _ds, second = _seal(evidence_db, store, records, dataset=ds)

    assert second.reused is True
    assert second.id == first.id
    assert second.manifest_digest == first.manifest_digest
    n = evidence_db.execute(
        "SELECT count(*) AS n FROM dataset_versions WHERE dataset_id = %s", (ds.id,)
    ).fetchone()
    assert n["n"] == 1, "a second identity was minted for the same bytes"


def test_manifest_object_verifies_and_a_corrupted_one_does_not(evidence_db, store) -> None:
    """MOS-EVID-015's reader-side rule, both directions."""
    _ds, sealed = _seal(evidence_db, store, balanced_cohort())
    assert ev_repo.verify_manifest(evidence_db, version_id=sealed.id, store=store) is True

    store.put(sealed.manifest_bucket, sealed.manifest_object_key, b'{"tampered":true}\n')
    assert ev_repo.verify_manifest(evidence_db, version_id=sealed.id, store=store) is False


# =====================================================================================
# 3. The seal's blocking gates
# =====================================================================================
def test_seal_refuses_an_unbalanced_cohort_and_names_the_stratum(evidence_db, store) -> None:
    """MOS-TRAIN-088: "A `fail` MUST block sealing." And the refusal must be actionable.

    A single-site, single-scanner, single-kernel cohort breaches C1, C2, C3 and C7. The
    assertion that matters beyond the refusal itself is that the reason NAMES the stratum
    and its value -- `MOS-TRAIN-011` forbids a promotion surface that shows one aggregate
    number, and the same standard applies to a refusal.
    """
    ds = _make_dataset(evidence_db, purpose="training")
    with pytest.raises(SealRefused) as exc:
        _seal(evidence_db, store, single_site_cohort(), dataset=ds)

    refused = exc.value
    assert {"C1", "C2", "C3", "C7"} <= set(refused.check_ids), refused.check_ids

    by_id = {r.check_id: r for r in refused.refusals}
    assert by_id["C1"].detail["stratum"] == "institution_key"
    assert by_id["C1"].detail["stratum_value"] == ev_digest.institution_key(
        SALT, "SITE-ONLY"
    )
    assert by_id["C1"].observed == 1.0 and by_id["C1"].bound == 0.60
    assert by_id["C2"].detail["stratum"] == "scanner_model"
    assert "Sensation 16" in str(by_id["C2"].detail["stratum_value"])
    assert by_id["C7"].observed == 1 and by_id["C7"].bound == 2

    # MOS-TRAIN-208: "A failure at any step MUST leave no `dataset_versions` row and no
    # manifest object."
    n = evidence_db.execute(
        "SELECT count(*) AS n FROM dataset_versions WHERE dataset_id = %s", (ds.id,)
    ).fetchone()
    assert n["n"] == 0
    assert len(store) == 0

    # And the refusal is machine-readable, not a string a caller has to parse.
    problem = refused.as_problem()
    assert problem["class"] == "clinical_rejection"
    assert {r["check_id"] for r in problem["refusals"]} == set(refused.check_ids)


def test_warn_checks_do_not_block(evidence_db, store) -> None:
    """MOS-TRAIN-092: C4 and C6 describe a corpus that is NARROW. Reported, never blocking.

    The balanced cohort carries two study years with a 50/50 split, so C6 passes; C4 is
    exercised directly against a cohort with one thickness, which is a `warn` and must
    still seal.
    """
    records = [
        _record(
            patient=p,
            institution=f"SITE-{p % 2}",
            manufacturer="SIEMENS" if p % 2 else "GE MEDICAL SYSTEMS",
            model="Sensation 16" if p % 2 else "Discovery CT750 HD",
            kernel_class="soft" if p % 2 else "standard",
            thickness=1.25,           # one value, no spread -> C4 warn
            year=2023,                # one year -> C6 warn (share 1.0 > 0.75)
            accession=f"ACC{p:04d}",
        )
        for p in range(10)
    ]
    report = ev_strat.stratification_report(acquisition_profile(records))
    outcomes = {c.id: c.outcome for c in report.checks}
    assert outcomes["C4"] == "warn" and outcomes["C6"] == "warn"
    assert outcomes["C1"] == "pass" and outcomes["C2"] == "pass"

    _ds, sealed = _seal(evidence_db, store, records, purpose="training")
    assert sealed.reused is False
    assert sealed.stratification["outcomes"]["C4"] == "warn"
    assert sealed.stratification["blocking_failures"] == []


def test_c5_without_an_envelope_is_not_a_pass() -> None:
    """MOS-TRAIN-090: C5 is "the one most easily rationalised away"."""
    report = ev_strat.stratification_report(acquisition_profile(balanced_cohort()))
    c5 = next(c for c in report.checks if c.id == "C5")
    assert c5.outcome == "n/a", "an undeclared envelope MUST NOT read as a passing C5"
    assert "no ApplicabilityEnvelope" in c5.detail["reason"]


def test_c5_fails_when_a_declared_bound_has_no_cases() -> None:
    """MOS-TRAIN-090's worked example: validated on 2.5 mm, declared for 0.6 mm."""
    profile = acquisition_profile(balanced_cohort())
    report = ev_strat.stratification_report(
        profile,
        envelope={"numeric": {"slice_thickness_mm": {"min": 0.6, "max": 3.0}}},
    )
    c5 = next(c for c in report.checks if c.id == "C5")
    assert c5.outcome == "fail"
    cells = c5.detail["under_populated_cells"]
    reasons = " ".join(str(cell.get("reason", "")) for cell in cells)
    assert "0.6" in reasons


def test_seal_refuses_an_acceptance_cohort_below_the_floor(evidence_db, store) -> None:
    """MOS-EVID-026: below 30 patients no criterion in 7.8 can return anything but
    INDETERMINATE, so the platform refuses to seal for that purpose."""
    ds = _make_dataset(evidence_db, purpose="acceptance")
    with pytest.raises(SealRefused) as exc:
        _seal(evidence_db, store, balanced_cohort(10), dataset=ds)
    r = next(r for r in exc.value.refusals if r.check_id == "MOS-EVID-026")
    assert r.observed == 10 and r.bound == 30
    assert r.code == "acceptance_cohort_below_floor"

    # 30 patients seals. The floor is a floor, not a discouragement.
    _ds, sealed = _seal(evidence_db, store, balanced_cohort(30), dataset=ds)
    assert sealed.patient_count == 30


def test_seal_refuses_a_patient_identity_defect(evidence_db, store) -> None:
    """MOS-TRAIN-118: two `patient_key`s sharing one study is an IDENTITY defect.

    "Resolving an L2 or L5 hit by moving the offending study across partitions MUST be
    refused." The refusal therefore carries both the prescribed remedy and the refused
    one -- a refusal that leaves the reader to invent a fix invites the wrong fix.
    """
    records = balanced_cohort(6)
    twin = SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", "P0000-SECOND-MRN"),
        study_instance_uid=records[0].study_instance_uid,   # the same study
        series_instance_uid=_uid(999, 1, 1),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=(_uid(999, 1, 1, 0), _uid(999, 1, 1, 1), _uid(999, 1, 1, 2)),
        series_pixel_digest=_pixel_digest("twin"),
        acquisition=records[0].acquisition,
        institution_key=records[0].institution_key,
        study_year=2023,
    )
    with pytest.raises(SealRefused) as exc:
        _seal(evidence_db, store, [*records, twin])
    r = next(r for r in exc.value.refusals if r.check_id == "L2")
    assert r.code == "patient_identity_defect"
    assert "patient_key_aliases" in r.detail["remedy"]
    assert "moving the offending study across partitions" in r.detail["refused_remedy"]


def test_corpus_generation_two_cannot_reach_a_cohort() -> None:
    """MOS-TRAIN-087: "A case with `corpus_generation >= 2` MUST NOT be used in any
    partition", so a sealed cohort cannot contain one.

    Refused by the value type, before any database is involved: compounding "is the part
    of this trap that has no natural brake", and a check that runs only at INSERT is one
    code path away from a cohort that was already built.
    """
    with pytest.raises(ValueError, match="generation 2 or above"):
        _record(
            patient=1, institution="S", manufacturer="M", model="X",
            kernel_class="soft", corpus_generation=2,
        )


def test_seal_refuses_an_incomplete_deidentification_record(evidence_db, store) -> None:
    """MOS-EVID-021: a pseudonymised version names its policy AND its UID mapping table."""
    with pytest.raises(SealRefused) as exc:
        _seal(
            evidence_db, store, balanced_cohort(),
            deidentification_status="pseudonymised", uid_mapping_table_id=None,
        )
    r = next(r for r in exc.value.refusals if r.check_id == "MOS-EVID-021")
    assert "uid_mapping_table_id" in r.observed


def test_seal_refuses_to_share_a_version_that_is_not_public_deidentified(
    evidence_db, store
) -> None:
    """MOS-EVID-012's cross-table half, which no CHECK constraint can see."""
    ds = ev_repo.create_dataset(
        evidence_db,
        slug=f"shared-{secrets.token_hex(4)}",
        display_name="a shared cohort",
        purpose="evaluation",
        custodian="TCIA/TEST",
        created_by=OPERATOR,
        visibility="tenant_shared",
        licence_spdx="CC-BY-3.0",
    )
    with pytest.raises(SealRefused) as exc:
        _seal(
            evidence_db, store, balanced_cohort(), dataset=ds,
            deidentification_status="pseudonymised",
        )
    assert "MOS-EVID-012" in exc.value.check_ids


def test_a_shared_dataset_must_carry_a_licence(evidence_db) -> None:
    """MOS-EVID-027, and the per-source licence shape of 15.2.5.

    The licence lives on the `Dataset`, which IS one source (`custodian` is chapter 7's
    `source_id`). That is what makes "licence per source" true without a licence array --
    and is also why a corpus mixing CC BY 3.0 and CC BY-NC 3.0 is two datasets.
    """
    with pytest.raises(ValueError, match="MOS-EVID-027"):
        ev_repo.create_dataset(
            evidence_db,
            slug=f"unlicensed-{secrets.token_hex(4)}",
            display_name="no licence",
            purpose="evaluation",
            custodian="TCIA/TEST",
            created_by=OPERATOR,
            visibility="public_readonly",
        )

    # Two sources, two rows, two licences -- and they are different rows on purpose.
    cc_by = ev_repo.create_dataset(
        evidence_db, slug=f"ccby-{secrets.token_hex(4)}", display_name="CC BY source",
        purpose="training", custodian="TCIA/COLLECTION-A", created_by=OPERATOR,
        visibility="tenant_shared", licence_spdx="CC-BY-3.0",
    )
    cc_by_nc = ev_repo.create_dataset(
        evidence_db, slug=f"ccbync-{secrets.token_hex(4)}",
        display_name="CC BY-NC source", purpose="training",
        custodian="TCIA/COLLECTION-B", created_by=OPERATOR,
        visibility="tenant_shared", licence_spdx="CC-BY-NC-3.0",
    )
    assert cc_by.licence_spdx != cc_by_nc.licence_spdx
    assert cc_by.custodian != cc_by_nc.custodian


# =====================================================================================
# 4. The split: patient-level, frozen, leak-checked
# =====================================================================================
def _even_split(records) -> list[tuple[str, str]]:
    keys = sorted({r.patient_key for r in records})
    out = []
    for i, k in enumerate(keys):
        out.append((k, "test" if i % 2 else "train"))
    return out


def test_split_with_one_patient_on_both_sides_is_refused(evidence_db, store) -> None:
    """MOS-EVID-034 L1, and MOS-TRAIN-117's scope: patient_key alone, all dates.

    The leaked patient is named, both partitions are named, and no split row and no split
    manifest object survive the refusal.
    """
    records = balanced_cohort()
    _ds, sealed = _seal(evidence_db, store, records)
    objects_before = len(store)

    assignments = _even_split(records)
    leaked = assignments[0][0]
    assignments.append((leaked, "test"))   # the same patient, a second partition

    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_split(
            evidence_db, dataset_version_id=sealed.id, name="leaky",
            assignments=assignments, store=store, bucket=BUCKET, frozen_by=OPERATOR,
            assignment_method="hash_mod_10(patient_key)", records=records,
        )

    r = next(r for r in exc.value.refusals if r.check_id == "L1")
    assert r.code == "patient_in_two_partitions"
    hit = r.detail["hits"][0]
    assert hit["patient_key"] == leaked
    assert hit["partitions"] == ["test", "train"]

    assert evidence_db.execute(
        "SELECT count(*) AS n FROM dataset_splits WHERE dataset_version_id = %s",
        (sealed.id,),
    ).fetchone()["n"] == 0
    assert len(store) == objects_before, "a refused freeze left a manifest object behind"


def test_l1_is_not_relaxed_by_five_years_between_studies(evidence_db, store) -> None:
    """MOS-TRAIN-117: "There is no interval after which two studies of one patient become
    independent observations."

    The two studies are five years apart and the outcome is unchanged, because the check
    never sees a date -- there is no date to pass it.
    """
    records = balanced_cohort(6)
    p = records[0]
    follow_up = SeriesRecord(
        patient_key=p.patient_key,
        study_instance_uid=_uid(0, 2),
        series_instance_uid=_uid(0, 2, 1),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(0, 2, 1, i) for i in range(40)),
        series_pixel_digest=_pixel_digest("follow-up-2024"),
        acquisition=p.acquisition,
        institution_key=p.institution_key,
        study_year=2024,
    )
    cohort = [*records, follow_up]
    _ds, sealed = _seal(evidence_db, store, cohort)

    assignments = _even_split(cohort)
    assignments.append((p.patient_key, "tune"))
    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_split(
            evidence_db, dataset_version_id=sealed.id, name="five-years",
            assignments=assignments, store=store, bucket=BUCKET, frozen_by=OPERATOR,
            assignment_method="manual", records=cohort,
        )
    assert "L1" in exc.value.check_ids


def test_the_split_tooling_offers_no_way_to_defeat_l1() -> None:
    """MOS-TRAIN-117: "a split tool that offers a 'minimum days between studies' option is
    offering a way to defeat this check." Chapter 17 acceptance criterion 2 greps for it;
    this is that grep, over the package that owns split freezing."""
    root = Path(ev_repo.__file__).resolve().parent
    forbidden = (
        "min_days_between_studies",
        "study_level_split",
        "allow_same_patient",
        "max_days_between_studies",
    )
    hits = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            # The docstrings that NAME the prohibition are the one legitimate occurrence,
            # so a hit only counts outside a comment or a string of prose. Assignment and
            # keyword use both produce `name=`; a parameter produces `name:`.
            if f"{name}=" in text or f"{name}:" in text or f"def {name}" in text:
                hits.append(f"{path.name}:{name}")
    assert hits == [], f"MOS-TRAIN-117: a relaxation option exists: {hits}"


def test_l3_catches_the_same_images_re_anonymised(evidence_db, store) -> None:
    """MOS-EVID-034 L3 -- "endemic in public collections"."""
    records = balanced_cohort(6)
    duplicate = SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", "P9999"),
        study_instance_uid=_uid(99, 1),
        series_instance_uid=_uid(99, 1, 1),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(99, 1, 1, i) for i in range(40)),
        # The same pixels as patient 0, under fresh UIDs and a fresh patient identity.
        series_pixel_digest=records[0].series_pixel_digest,
        acquisition=records[0].acquisition,
        institution_key=records[1].institution_key,
        study_year=2023,
    )
    cohort = [*records, duplicate]
    _ds, sealed = _seal(evidence_db, store, cohort)

    assignments = [
        (records[0].patient_key, "train"),
        (duplicate.patient_key, "test"),
        *[(r.patient_key, "train") for r in records[1:]],
    ]
    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_split(
            evidence_db, dataset_version_id=sealed.id, name="pixel-dupe",
            assignments=assignments, store=store, bucket=BUCKET, frozen_by=OPERATOR,
            assignment_method="manual", records=cohort,
        )
    r = next(r for r in exc.value.refusals if r.check_id == "L3")
    assert r.code == "identical_pixels_in_two_partitions"


def test_split_must_cover_every_patient_in_the_cohort(evidence_db, store) -> None:
    """MOS-EVID-031: "silent omission MUST be a write-time error"."""
    records = balanced_cohort()
    _ds, sealed = _seal(evidence_db, store, records)
    partial = _even_split(records)[:-2]

    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_split(
            evidence_db, dataset_version_id=sealed.id, name="partial",
            assignments=partial, store=store, bucket=BUCKET, frozen_by=OPERATOR,
            assignment_method="manual", records=records,
        )
    r = next(r for r in exc.value.refusals if r.code == "split_does_not_cover_cohort")
    assert r.observed == 2

    # Declaring them excluded, with a reason, is the sanctioned way to cover a subset.
    keys = sorted({r_.patient_key for r_ in records})
    assignments = partial + [(k, "excluded") for k in keys[-2:]]
    frozen = ev_repo.freeze_split(
        evidence_db, dataset_version_id=sealed.id, name="partial",
        assignments=assignments, store=store, bucket=BUCKET, frozen_by=OPERATOR,
        assignment_method="manual", records=records,
        exclusion_reasons=dict.fromkeys(keys[-2:], "gantry tilt > 3 deg"),
    )
    assert frozen.partition_patients["excluded"] == 2

    rows = evidence_db.execute(
        "SELECT partition, exclusion_reason FROM dataset_split_members "
        "WHERE split_id = %s AND partition = 'excluded'",
        (frozen.id,),
    ).fetchall()
    assert len(rows) == 2 and all(r_["exclusion_reason"] for r_ in rows)


def test_a_frozen_split_is_frozen(evidence_db, store) -> None:
    """Section 12.12's sealed-column list for `dataset_splits` is "all" -- including
    `leakage_report`, so a waiver cannot be added after the fact (MOS-EVID-036)."""
    records = balanced_cohort()
    _ds, sealed = _seal(evidence_db, store, records)
    frozen = ev_repo.freeze_split(
        evidence_db, dataset_version_id=sealed.id, name="clean",
        assignments=_even_split(records), store=store, bucket=BUCKET,
        frozen_by=OPERATOR, assignment_method="hash_mod_10(patient_key)",
        stratified_by=["manufacturer"], records=records,
    )
    assert frozen.leakage_report["L1"] == "pass"
    assert frozen.leakage_report["L3"] == "pass"
    # MOS-EVID-037: L4 is SKIPPED with a reason, never silently passed, when the cohort
    # carries no perceptual hashes.
    assert frozen.leakage_report["L4"] == "skipped"
    assert frozen.leakage_report["detail"]["L4"]["detail"]["n_skipped"] > 0

    for statement, params in (
        ("UPDATE dataset_splits SET leakage_report = '{}'::jsonb WHERE id = %s",
         (frozen.id,)),
        ("UPDATE dataset_split_members SET partition = 'test' WHERE split_id = %s",
         (frozen.id,)),
        ("DELETE FROM dataset_split_members WHERE split_id = %s", (frozen.id,)),
        ("DELETE FROM dataset_splits WHERE id = %s", (frozen.id,)),
    ):
        _assert_database_refuses(evidence_db, statement, params)


def test_the_database_refuses_a_val_partition(evidence_db, store) -> None:
    """MOS-EVID-031 / MOS-EVID-033: `val` is not a permitted value, at the column."""
    records = balanced_cohort()
    _ds, sealed = _seal(evidence_db, store, records)
    frozen = ev_repo.freeze_split(
        evidence_db, dataset_version_id=sealed.id, name="clean",
        assignments=_even_split(records), store=store, bucket=BUCKET,
        frozen_by=OPERATOR, assignment_method="manual", records=records,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        evidence_db.execute(
            "INSERT INTO dataset_split_members (tenant_id, split_id, patient_key, "
            "partition) VALUES (current_tenant_id(), %s, %s, 'val')",
            (frozen.id, ev_digest.patient_key(SALT, "TCIA/TEST", "P8888")),
        )
    evidence_db.rollback()


def test_l4_hits_block_and_a_waiver_still_blocks_training() -> None:
    """MOS-EVID-034 L4, and MOS-TRAIN-115's asymmetry between reporting and training.

    A waiver is "a statement about what a report may claim; it is not a licence to fit on
    the test set", so L1/L2/L3/L5 stay blocking for a training run even when waived.
    """
    a = _record(patient=1, institution="S1", manufacturer="M", model="X",
                kernel_class="soft")
    b = _record(patient=2, institution="S2", manufacturer="M", model="X",
                kernel_class="soft")
    a = SeriesRecord(**{**a.__dict__, "dhash64": 0xA5A5A5A5A5A5A5A5})
    b = SeriesRecord(**{**b.__dict__, "dhash64": 0xA5A5A5A5A5A5A5A7})  # Hamming 1
    report = ev_leak.leakage_report(
        [a, b], [(a.patient_key, "train"), (b.patient_key, "test")]
    )
    l4 = next(c for c in report.checks if c.id == "L4")
    assert l4.outcome == "fail" and l4.hits[0]["hamming"] <= ev_leak.L4_MAX_HAMMING

    waived = ev_leak.leakage_report(
        [a, b],
        [(a.patient_key, "train"), (a.patient_key, "test"), (b.patient_key, "test")],
        waivers=[{"check_id": "L1", "waived_by": "u1", "waived_at": "2026-01-01T00:00:00Z",
                  "rationale": "known duplicate registration", "affected_pairs": []}],
    )
    assert next(c for c in waived.checks if c.id == "L1").outcome == "waived"
    assert "L1" in ev_leak.waiver_blocks_training(waived)


# =====================================================================================
# 5. AnnotationSet: the readers and the consensus rule
# =====================================================================================
def _readers(n: int) -> list[dict[str, Any]]:
    return [
        {
            "reader_id": f"rdr_a{i}",
            "role": "radiologist",
            "years_experience": 10 + i,
            "board_certified": True,
            "specialty": "thoracic_radiology",
            "tool": "MONAI Label 0.8.4 + 3D Slicer 5.6.2",
            "instructions_uri": "s3://medos-evidence/instructions/effusion-v3.pdf",
            "blinded_to": ["model_output", "other_readers", "clinical_report"],
        }
        for i in range(n)
    ]


def _annotation_entries(records) -> list[dict[str, Any]]:
    return [
        {
            "patient_key": r.patient_key,
            "study_instance_uid": r.study_instance_uid,
            "series_instance_uid": r.series_instance_uid,
            "reference": {"kind": "mask", "uri": "s3://x/ref.nrrd",
                          "digest": _pixel_digest(f"ref/{r.series_instance_uid}"),
                          "geometry": "source", "reference_volume_ml": 286.4},
            "per_reader": [
                {"reader_id": "rdr_a0", "uri": "s3://x/a0.nrrd",
                 "digest": _pixel_digest("a0"), "volume_ml": 291.0},
                {"reader_id": "rdr_a1", "uri": "s3://x/a1.nrrd",
                 "digest": _pixel_digest("a1"), "volume_ml": 281.9},
                {"reader_id": "rdr_a2", "uri": "s3://x/a2.nrrd",
                 "digest": _pixel_digest("a2"), "volume_ml": 284.2},
            ],
            "inter_reader": {"dice": 0.938, "volume_difference_ml": 9.1},
        }
        for r in records
    ]


def test_annotation_set_names_its_readers_and_its_consensus_rule(
    evidence_db, store
) -> None:
    """MOS-EVID-038 and MOS-EVID-039.

    "On LIDC-IDRI, whether you score against one reader, a >=2 consensus, the union or
    STAPLE moves Dice more than any model change made in a year."
    """
    records = balanced_cohort()
    _ds, sealed = _seal(evidence_db, store, records)
    result = ev_repo.freeze_annotation_set(
        evidence_db, dataset_version_id=sealed.id, name="consensus-3",
        capability_id="pleural_effusion",
        label_definition_id="00000000-0000-0000-0000-0000000000aa",
        annotation_type="mask", consensus_rule="majority_at_least_2",
        consensus_params={"min_agreeing": 2}, readers=_readers(3),
        entries=_annotation_entries(records), store=store, bucket=BUCKET,
        frozen_by=OPERATOR, reference_of_record=True,
    )
    assert result.reader_count == 3
    assert result.annotation_digest.startswith("sha256:")

    rows = evidence_db.execute(
        "SELECT reader_id, role, tool, blinded_to FROM annotation_readers "
        "WHERE annotation_set_id = %s ORDER BY reader_id",
        (result.id,),
    ).fetchall()
    assert [r["reader_id"] for r in rows] == ["rdr_a0", "rdr_a1", "rdr_a2"]
    assert all("model_output" in r["blinded_to"] for r in rows)

    # Frozen: MOS-TRAIN-110 -- adding a reader produces a NEW AnnotationSet.
    _assert_database_refuses(
        evidence_db,
        "UPDATE annotation_sets SET reference_of_record = false WHERE id = %s",
        (result.id,),
    )
    _assert_database_refuses(
        evidence_db,
        "DELETE FROM annotation_readers WHERE annotation_set_id = %s",
        (result.id,),
    )


def test_annotation_set_refuses_an_incoherent_consensus_rule(evidence_db, store) -> None:
    """MOS-EVID-039's per-rule parameter requirements, which a CHECK cannot express."""
    records = balanced_cohort()
    _ds, sealed = _seal(evidence_db, store, records)
    common = {
        "dataset_version_id": sealed.id,
        "capability_id": "pleural_effusion",
        "label_definition_id": "00000000-0000-0000-0000-0000000000aa",
        "annotation_type": "mask",
        "entries": _annotation_entries(records),
        "store": store,
        "bucket": BUCKET,
        "frozen_by": OPERATOR,
    }

    # `majority_at_least_2` requires reader_count >= 3.
    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_annotation_set(
            evidence_db, name="two-readers", consensus_rule="majority_at_least_2",
            consensus_params={"min_agreeing": 2}, readers=_readers(2), **common,
        )
    assert "MOS-EVID-039" in exc.value.check_ids

    # `staple` MUST record the five parameters that make the reduction reproducible.
    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_annotation_set(
            evidence_db, name="staple", consensus_rule="staple",
            consensus_params={"iterations": 30}, readers=_readers(3), **common,
        )
    r = next(r for r in exc.value.refusals if r.code == "consensus_params_incomplete")
    assert "rng_seed" in r.observed or "rng_seed" in r.bound

    # MOS-EVID-038: a set with no reader at all.
    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_annotation_set(
            evidence_db, name="no-readers", consensus_rule="single_reader",
            readers=[], **common,
        )
    assert "MOS-EVID-038" in exc.value.check_ids


def test_an_algorithmic_reference_is_never_the_reference_of_record(
    evidence_db, store
) -> None:
    """MOS-EVID-042: "an `AcceptanceCriteria` gate MUST refuse an AnnotationSet with
    `reference_of_record = false`", so the flag must never be true for one."""
    records = balanced_cohort()
    _ds, sealed = _seal(evidence_db, store, records)
    readers = _readers(1)
    readers[0]["role"] = "algorithm"
    with pytest.raises(FreezeRefused) as exc:
        ev_repo.freeze_annotation_set(
            evidence_db, dataset_version_id=sealed.id, name="algo",
            capability_id="pleural_effusion",
            label_definition_id="00000000-0000-0000-0000-0000000000aa",
            annotation_type="mask", consensus_rule="single_reader", readers=readers,
            entries=_annotation_entries(records), store=store, bucket=BUCKET,
            frozen_by=OPERATOR, reference_of_record=True,
        )
    assert "MOS-EVID-042" in exc.value.check_ids


# =====================================================================================
# 6. Content addressing
# =====================================================================================
def test_manifest_digest_is_order_sensitive_and_stable() -> None:
    """MOS-EVID-009: the digest is over the JSONL bytes in the declared order.

    Both halves matter. Stable, or a re-seal of the same content mints a second identity
    (`MOS-TRAIN-209`); order-sensitive, or two callers who disagree about the canonical
    order agree about the digest and the disagreement stays hidden.
    """
    lines = [{"b": 1, "a": 2}, {"a": 3}]
    assert ev_digest.manifest_digest(lines) == ev_digest.manifest_digest(
        [{"a": 2, "b": 1}, {"a": 3}]
    )
    assert ev_digest.manifest_digest(lines) != ev_digest.manifest_digest(
        list(reversed(lines))
    )
    body = ev_digest.manifest_bytes(lines)
    assert body.endswith(b"\n") and b"\n\n" not in body
    assert ev_digest.sha256_of(body) == ev_digest.manifest_digest(lines)


def test_patient_key_is_tenant_scoped_and_not_reversible_by_enumeration() -> None:
    """MOS-EVID-010 / MOS-EVID-011.

    The key is an HMAC and not a digest precisely because `issuer|patient_id` is
    enumerable: a plain SHA-256 of a guessable input is reversible by anyone holding the
    patient list, which would make `MOS-EVID-011`'s "MUST NOT be reversible without the
    tenant salt" false.
    """
    a = ev_digest.patient_key(SALT, "TCIA/LIDC-IDRI", "LIDC-IDRI-0001")
    b = ev_digest.patient_key(b"a-different-tenant-salt", "TCIA/LIDC-IDRI", "LIDC-IDRI-0001")
    assert a != b, "the key is comparable across tenants; the salt is not doing its job"
    assert a == ev_digest.patient_key(SALT, "TCIA/LIDC-IDRI", "LIDC-IDRI-0001")
    assert a.startswith("pk_") and len(a) == 19

    plain = hashlib.sha256(b"TCIA/LIDC-IDRI|LIDC-IDRI-0001").hexdigest()
    assert plain[:16] not in a, "the key is a plain digest of a guessable input"

    with pytest.raises(ValueError, match="issuer"):
        ev_digest.patient_key(SALT, "", "LIDC-IDRI-0001")


def test_the_cohort_is_a_materialised_list_and_never_a_query(evidence_db, store) -> None:
    """MOS-EVID-016, asserted against the signature rather than against prose."""
    import inspect

    sig = inspect.signature(ev_repo.seal_dataset_version)
    assert sig.parameters["records"].annotation is not None
    forbidden = {"query", "filter", "folder", "path", "view", "predicate"}
    assert not (forbidden & set(sig.parameters)), (
        "MOS-EVID-016: a DatasetVersion MUST NOT be created by reference to a live query, "
        "a folder path, a DICOM query filter, or a database view"
    )
    with pytest.raises(SealRefused) as exc:
        _seal(evidence_db, store, [])
    assert "MOS-EVID-016" in exc.value.check_ids


def test_the_acquisition_profile_separates_series_and_patient_counts() -> None:
    """MOS-EVID-025: "Mixing the two is a reporting defect"."""
    records = balanced_cohort(10)
    profile = acquisition_profile(records)
    assert profile["n_series"] == 10 and profile["n_patients"] == 10
    # Series-level histogram and patient-level histogram are different objects.
    assert "institution_key" in profile["categorical"]
    assert "institution_key" in profile["patients"]
    assert profile["numeric"]["slice_thickness_mm"]["distinct"] == 2
    assert profile["lossy_series_fraction"] == 0.0
    # MOS-TRAIN-093: the report is a pure function of the profile, so it can be recomputed
    # offline from the sealed manifest.
    first = ev_strat.stratification_report(profile).as_dict()
    second = ev_strat.stratification_report(acquisition_profile(records)).as_dict()
    assert first == second
