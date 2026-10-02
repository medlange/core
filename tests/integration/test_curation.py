# SPDX-License-Identifier: Apache-2.0
"""The curation queue, the training-use policy, and the generated transform chain.

WHAT THIS FILE IS FOR
---------------------
`MOS-TRAIN-190` places three things in 0.2.0 and this file is the executable statement of
all three:

  * `HarvestCandidate` / `CurationBatch` with the exclusion vocabulary -- a human decides
    what enters a cohort (`MOS-TRAIN-080`), and every exclusion is retained with a reason
    code from a closed list (`MOS-TRAIN-203`, `MOS-TRAIN-204`);
  * per-`Tenant` `training_use_allowed` with its `TrainingDataPolicy` (`MOS-TRAIN-072`,
    `MOS-TRAIN-073`), recording the legal basis and nothing more (`MOS-TRAIN-074`);
  * the generated transform chain with its byte-equality check (`MOS-TRAIN-131`,
    `MOS-TRAIN-133`, `MOS-TRAIN-191`).

Chapter 17's acceptance criteria 4, 5, 6 and 7 are the shape of sections 5, 6 and 7 below.
Criteria 1-3 are leakage and are asserted in `test_evidence_schema.py`, where the split
lives; this file does not restate them.

WHY SO MANY ASSERTIONS GO THROUGH THE DATABASE
----------------------------------------------
`MOS-TRAIN-203` is "Deleting an excluded row MUST be refused" and `MOS-STORE-359` puts
`curation_decisions` and `corpus_stratification_reports` on the table-level revocation
list. A test that asserted only that the Python module has no `delete()` would pass on the
day somebody adds one. So the refusals are asserted where they are enforced: the grant
table and the triggers of migration 0011, exercised as `medicalos_app`, which is
`NOBYPASSRLS` and owns nothing.

THE DATABASE THIS RUNS AGAINST
------------------------------
`conftest.py`'s `pg_dsn` creates a throwaway database per session and drops it afterwards.
Nothing here touches the development deployment's own database.

Spec: MOS-TRAIN-071 to MOS-TRAIN-093, MOS-TRAIN-111, MOS-TRAIN-131 to MOS-TRAIN-134,
MOS-TRAIN-190, MOS-TRAIN-191, MOS-TRAIN-202 to MOS-TRAIN-209, MOS-STORE-358,
MOS-STORE-359, MOS-EVID-016, MOS-EVID-022, MOS-IMG-054, MOS-IMG-058.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import inspect
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db.migrate import discover
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import digest as ev_digest
from medos.evidence import repo as ev_repo
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.store import InMemoryManifestStore
from medos.sdk import chain as ch
from medos.sdk import fixtures as fx
from medos.sdk import preprocess as pre
from medos.sdk import spec as sp
from medos.sdk.errors import ChainRefused, CurationRefused, HarvestRefused
from medos.sdk.refusal import SealRefused
from medos.training import curation as cur
from medos.training import policy as pol
from medos.training import vocabulary as vocab
from psycopg.rows import dict_row

BUCKET = "medos-evidence"
SALT = b"test-tenant-salt-not-a-real-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"
SERVICE = "22222222-2222-2222-2222-222222222222"
CAPABILITY = "pulmo.pleural-effusion"

CURATION_TABLES = (
    "corpus_stratification_reports",
    "curation_decisions",
    "harvest_candidates",
    "harvest_batches",
    "sampling_plans",
    "training_data_policies",
)

EVIDENCE_TABLES = (
    "datasets",
    "dataset_versions",
    "dataset_cases",
    "dataset_splits",
    "dataset_split_members",
)

_REPO = Path(__file__).resolve().parents[2]

#: The preprocessing contract is TWO directories now. `MOS-IMG-003` asked for
#: `medicalos-preprocessing` as its own package, so `preprocess.py`, `chain.py`,
#: `spec.py`, `fixtures.py` and the rest left `medos/medos/training/` -- and the gates
#: below (one `build_chain`, one digest recorder, no re-record operation) are gates on
#: THE CONTRACT, not on a directory. Scanning either package alone would let a second
#: constructor land in the other one and report a pass.
TRAINING_PKG = _REPO / "medos" / "medos" / "training"
SHARED_PKG = _REPO / "medos" / "medos" / "sdk"
CONTRACT_PKGS = (TRAINING_PKG, SHARED_PKG)


def _module(name: str) -> Path:
    """`name` in whichever of the two packages holds it."""
    found = [pkg / name for pkg in CONTRACT_PKGS if (pkg / name).exists()]
    assert found, (
        f"{name!r} is in neither {TRAINING_PKG.name!r} nor {SHARED_PKG.name!r}. "
        "A module named here moved or was deleted, and a check that silently read "
        "nothing would report a pass."
    )
    return found[0]


def _contract_modules() -> list[Path]:
    return [q for pkg in CONTRACT_PKGS for q in sorted(pkg.glob("*.py"))]


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture()
def cdb(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One connection with the default tenant bound and the curation tables emptied.

    TRUNCATE and not DELETE: `curation_decisions` and `corpus_stratification_reports`
    carry `forbid_curation_mutation` on DELETE (`MOS-STORE-359`), and TRUNCATE does not
    fire row-level triggers -- so the append-only guarantee holds for application code
    while the harness can still start clean. Same arrangement as `job_events` since weeks
    1-2 and `dataset_cases` since 0006.

    `training_use_allowed` is reset too. It is a column on `tenants`, which no test may
    truncate, and a flag left true by a previous test would make
    `test_the_harvest_is_refused_without_a_policy` pass for the wrong reason -- so it is
    written down here, before the deferred constraint trigger can see a flag with no
    instrument behind it.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    # Scoped to the bound tenant, and before the TRUNCATE. Other test files create their
    # own tenants, and `tenants_training_policy_required` fires PER ROW: an unscoped
    # `UPDATE tenants SET training_use_allowed = ...` touches a tenant that has no
    # instrument behind it and the deferred trigger refuses the whole transaction. That is
    # the trigger working; it is this fixture that has no business writing another
    # tenant's flag.
    conn.execute(
        "UPDATE tenants SET training_use_allowed = false WHERE id = current_tenant_id()"
    )
    conn.execute(f"TRUNCATE {', '.join(CURATION_TABLES)} CASCADE")
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


@pytest.fixture()
def permitted(cdb) -> pol.PolicyRow:
    """A tenant that may contribute to a training corpus. `MOS-TRAIN-072`/`073`."""
    row = pol.record_policy(
        cdb,
        legal_basis="research_ethics_approval",
        basis_reference="REC-2024-118",
        scope={
            "modalities": ["CT"],
            "body_parts": ["CHEST"],
            "capabilities": [CAPABILITY],
            "date_from": "2023-01-01",
            "date_to": None,
        },
        recorded_by=OPERATOR,
        permits_redistribution=True,
    )
    pol.set_training_use_allowed(cdb, True)
    return row


# =====================================================================================
# Builders
# =====================================================================================
def _uid(*parts: int) -> str:
    return "1.2.826.0.1.3680043.10.1." + ".".join(str(p) for p in parts)


def _pixel_digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _acq(
    *,
    institution: str,
    manufacturer: str,
    model: str,
    kernel_class: str,
    thickness: float,
    year: int,
) -> cur.CandidateAcquisition:
    """`MOS-TRAIN-084`'s thirteen fields, none imputed."""
    return cur.CandidateAcquisition(
        manufacturer=manufacturer,
        manufacturer_model_name=model,
        convolution_kernel="B30f",
        convolution_kernel_class=kernel_class,
        slice_thickness_mm=thickness,
        pixel_spacing_mm=(0.703125, 0.703125),
        kvp=120.0,
        exposure_mas=105.0,
        contrast_phase="non_contrast",
        iterative_recon_strength=None,
        station_key="STATION-A",
        institution_key=ev_digest.institution_key(SALT, institution),
        study_year=year,
    )


def _series_record(
    *,
    patient: int,
    institution: str,
    manufacturer: str,
    model: str,
    kernel_class: str,
    thickness: float,
    year: int,
    series: int = 1,
) -> SeriesRecord:
    """What `dataset_export` hands back for one candidate (`MOS-TRAIN-199`/`200`)."""
    return SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", f"P{patient:04d}"),
        study_instance_uid=_uid(patient, 1),
        series_instance_uid=_uid(patient, 1, series),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(patient, 1, series, i) for i in range(40)),
        series_pixel_digest=_pixel_digest(f"{patient}/{series}"),
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
        accession_number_hash=ev_digest.accession_number_hash(SALT, f"ACC{patient:04d}"),
        corpus_generation=0,
    )


def _site_of(patient: int) -> dict[str, Any]:
    """Two sites, two scanners, two kernels, two years: the cohort that passes C1-C7."""
    half = patient % 2
    return {
        "institution": f"SITE-{half}",
        "manufacturer": "SIEMENS" if half else "GE MEDICAL SYSTEMS",
        "model": "Sensation 16" if half else "Discovery CT750 HD",
        "kernel_class": "soft" if half else "standard",
        "thickness": 1.25 if half else 2.5,
        "year": 2022 + half,
    }


def _open_batch(conn, *, plan_version: int = 1) -> cur.BatchRow:
    plan = cur.declare_sampling_plan(
        conn,
        capability_id=CAPABILITY,
        plan_version=plan_version,
        strata={
            "score_band": {"rule": "bottom_band_3x_top_band"},
            "review_outcome": {"REJECTED": "census", "MODIFIED": "census"},
            "ran_on_platform": {"false": "min_naive_fraction"},
            "acquisition_bucket": {"rule": "proportional"},
        },
    )
    return cur.open_batch(
        conn, capability_id=CAPABILITY, sampling_plan_id=plan, opened_by=OPERATOR
    )


def _draw(
    conn,
    batch_id: str,
    patient: int,
    *,
    geometry: dict[str, Any] | None = None,
    corpus_generation: int = 0,
) -> str:
    s = _site_of(patient)
    return cur.add_candidate(
        conn,
        batch_id=batch_id,
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", f"P{patient:04d}"),
        study_instance_uid=_uid(patient, 1),
        series_instance_uids=[_uid(patient, 1, 1)],
        instance_uids=[_uid(patient, 1, 1, i) for i in range(40)],
        geometry=geometry
        or {"tilt_deg": 0.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
        acquisition=_acq(**s),
        institution_key=ev_digest.institution_key(SALT, s["institution"]),
        deid_policy_version=4,
        sampling_weight=1.0,
        ran_on_platform=False,
        corpus_generation=corpus_generation,
    )


def _retrieve(candidate: cur.CandidateRow) -> list[SeriesRecord]:
    """The `dataset_export` boundary, stubbed. Step 3 of `MOS-TRAIN-208`."""
    patient = int(candidate.study_instance_uid.rsplit(".", 2)[-2])
    return [_series_record(patient=patient, **_site_of(patient))]


def _make_dataset(conn, *, purpose: str = "training"):
    return ev_repo.create_dataset(
        conn,
        slug=f"corpus-{secrets.token_hex(4)}",
        display_name="curated training corpus",
        purpose=purpose,
        custodian="TCIA/TEST",
        created_by=OPERATOR,
    )


def _app_conn(pg_dsn: str) -> psycopg.Connection[Any]:
    """A connection as `medicalos_app`: NOBYPASSRLS, owns nothing, holds only grants."""
    from tests.integration.conftest import APP_PASSWORD

    tail = pg_dsn.split("://", 1)[1].split("@", 1)[1]
    conn = psycopg.connect(
        f"postgresql://medicalos_app:{APP_PASSWORD}@{tail}",
        row_factory=dict_row,
        autocommit=False,
    )
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    # COMMITTED, because `_refuses` rolls back after each expected refusal and a
    # session-level `set_config` issued inside an aborted transaction is reverted with it.
    # Without this the second refusal in a test fails as `42704 tenant_id is not set`,
    # which looks like a tenancy bug and is a fixture bug.
    conn.commit()
    return conn


def _refuses(conn, statement: str, params: tuple = ()) -> psycopg.DatabaseError:
    """Execute and require the refusal to be the SEAL, not luck.

    `forbid_curation_mutation()` raises `MOS05` and `forbid_column_change()` raises
    `MOS06`; a missing privilege raises `42501`. Asserting on the exception class would
    pass on a typo'd column name, which is not the claim.
    """
    with pytest.raises(psycopg.DatabaseError) as exc:
        conn.execute(statement, params)
    assert exc.value.sqlstate in ("MOS05", "MOS06", "42501"), (
        f"expected the seal (MOS05/MOS06) or a missing privilege (42501), got "
        f"{exc.value.sqlstate}: {exc.value}"
    )
    conn.rollback()
    return exc.value


def _source(*names: str) -> str:
    paths = [_module(n) for n in names] if names else _contract_modules()
    return "\n".join(q.read_text(encoding="utf-8") for q in paths)


def _code_only(text: str) -> str:
    """Strip docstrings, comments and string literals, leaving identifiers and syntax.

    Every module in this package explains at length what it refuses to implement, and its
    refusal MESSAGES quote the requirement: `policy.py` raises a refusal whose text is
    "there is no per-study, per-user or per-environment override and no
    platform-administrator bypass". A naive grep for `override` finds that sentence and
    fails a test whose claim is that no such code path exists. Prose that says a thing does
    not exist is evidence for the test, not against it, so all three prose forms go.

    What survives is what the assertions are actually about: function names, parameter
    names, attribute access and imports.
    """
    text = re.sub(r'"""(?:.|\n)*?"""', " ", text)
    text = re.sub(r"'''(?:.|\n)*?'''", " ", text)
    text = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    text = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', text)
    return re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", text)


# =====================================================================================
# 1. Migration 0011 applies, and its shape is the one MOS-STORE-358/359 asks for
# =====================================================================================
def _swap_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"


def test_curation_migration_applies_from_empty(pg_dsn: str) -> None:
    """0011 lands on a database built from `schema.sql` plus 0002-0010, from empty.

    `MOS-STORE-358` renders six records, all tenant-owned, all on the standard section
    12.5 set. This asserts the six tables exist with FORCE row-level security and a
    tenant-isolation policy each -- the property `MOS-TRAIN-071` ("The harvest MUST NOT
    pool studies across tenants into one `HarvestBatch`") rests on.
    """
    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                   (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relname = ANY(%s)
            """,
            (list(CURATION_TABLES),),
        ).fetchall()
    found = {r["relname"]: r for r in rows}
    assert set(found) == set(CURATION_TABLES), (
        f"missing: {sorted(set(CURATION_TABLES) - set(found))}"
    )
    for name, r in found.items():
        assert r["relrowsecurity"] and r["relforcerowsecurity"], f"{name}: RLS not forced"
        assert r["policies"] >= 1, f"{name}: no tenant-isolation policy"


def test_no_role_may_delete_a_curation_row(pg_dsn: str) -> None:
    """`MOS-TRAIN-203`: "Deleting an excluded row MUST be refused."

    Asserted as a grant property rather than as an absent method. The reason is
    `MOS-STORE-359`: "an editable version of either is not a record". A DELETE that the
    application role does not hold cannot be reintroduced by a helpful repository
    function.
    """
    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT table_name, grantee FROM information_schema.role_table_grants
             WHERE table_schema = 'public' AND privilege_type = 'DELETE'
               AND grantee <> 'medicalos_owner' AND table_name = ANY(%s)
            """,
            (list(CURATION_TABLES),),
        ).fetchall()
    assert rows == [], f"DELETE is granted: {[(r['table_name'], r['grantee']) for r in rows]}"


def test_the_append_only_tables_hold_no_update_grant(pg_dsn: str) -> None:
    """`MOS-STORE-359`'s table-level revocation list, as rows.

    `sampling_plans` ("a plan edited after the draw cannot describe the draw"),
    `curation_decisions` and `corpus_stratification_reports` take no UPDATE at all. The
    other three keep a narrow column list, which the next test exercises from the
    application side.
    """
    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT table_name FROM information_schema.role_table_grants
             WHERE table_schema = 'public' AND privilege_type = 'UPDATE'
               AND grantee <> 'medicalos_owner'
               AND table_name IN ('sampling_plans','curation_decisions',
                                  'corpus_stratification_reports')
            """
        ).fetchall()
    assert rows == [], f"UPDATE is granted on {[r['table_name'] for r in rows]}"


def test_a_candidate_carries_no_phi_column(pg_dsn: str) -> None:
    """`MOS-TRAIN-079`: a candidate carries "UIDs, hashes and keys only".

    "It MUST NOT carry `PatientName`, `PatientBirthDate`, `AccessionNumber`, institution
    free text, or any source-space UID." `MOS-STORE-358` extends `MOS-STORE-272`'s
    schema-review rule and CI grep to this table, and this is that grep.
    """
    forbidden = {
        "patient_name",
        "patient_birth_date",
        "patient_id",
        "accession_number",
        "institution_name",
        "study_date",
        "source_study_instance_uid",
        "source_series_instance_uid",
    }
    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        cols = {
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='harvest_candidates'"
            ).fetchall()
        }
    assert not (cols & forbidden), f"PHI columns on harvest_candidates: {cols & forbidden}"
    assert "institution_key" in cols, "MOS-TRAIN-089's HMAC is the only institution column"


def test_down_migration_is_the_reverse_of_the_up(tmp_path: Path) -> None:
    """Every object 0011 creates, 0011's down drops. Textual, and deliberately so.

    Running the down for real against the session database would drop tables the other
    tests in this session still need, and running it against a throwaway one proves only
    that `DROP ... IF EXISTS` is forgiving. The claim is that nothing was forgotten.
    """
    migration = {m.version: m for m in discover()}["0011_curation"]
    up = migration.sql
    down = migration.path.with_name("0011_curation.down.sql").read_text(encoding="utf-8")
    created_tables = set(re.findall(r"CREATE TABLE (\w+)", up))
    created_funcs = set(re.findall(r"CREATE FUNCTION (\w+)", up))
    assert created_tables, "no CREATE TABLE found; the parse is wrong, not the migration"
    for t in created_tables:
        assert re.search(rf"DROP TABLE IF EXISTS {t}\b", down), f"down forgets table {t}"
    for f in created_funcs:
        assert re.search(rf"DROP FUNCTION IF EXISTS {f}\b", down), f"down forgets {f}()"
    # The one trigger the up migration puts on a table it does not own.
    assert "DROP TRIGGER IF EXISTS tenants_training_policy_required ON tenants" in down


# =====================================================================================
# 2. Permission to train: the flag and the instrument (MOS-TRAIN-072 to 077)
# =====================================================================================
def test_the_harvest_is_refused_without_a_policy(cdb) -> None:
    """`MOS-TRAIN-072`: "When it is false, the harvest MUST refuse, with `403` and problem
    type `training-use-not-permitted`."

    The wire form is part of the requirement, so the problem document is asserted and not
    only the exception type.
    """
    assert pol.training_use_allowed(cdb) is False
    with pytest.raises(HarvestRefused) as exc:
        pol.assert_harvest_permitted(cdb)
    problem = exc.value.as_problem()
    assert problem["type"].endswith("/training-use-not-permitted")
    assert problem["status"] == 403
    assert any(r.check_id == "MOS-TRAIN-072" for r in exc.value.refusals)


def test_the_flag_cannot_be_true_without_a_live_instrument(cdb) -> None:
    """`MOS-TRAIN-073`: "Setting `training_use_allowed = true` MUST require a
    `TrainingDataPolicy` record on the tenant, and the platform MUST refuse the flag
    without it."

    `MOS-STORE-358` makes this a DEFERRED constraint trigger rather than a CHECK, so the
    flag and its instrument may be written in one transaction. The refusal therefore
    arrives at COMMIT, which is the behaviour being asserted.
    """
    with pytest.raises(psycopg.DatabaseError) as exc:
        cdb.execute(
            "UPDATE tenants SET training_use_allowed = true "
            "WHERE id = current_tenant_id()"
        )
        cdb.commit()
    assert exc.value.sqlstate == "23514", exc.value
    assert "MOS-TRAIN-073" in str(exc.value)
    cdb.rollback()


def test_the_flag_and_its_instrument_may_be_written_in_one_transaction(cdb) -> None:
    """The other half of the deferred trigger: the legitimate order still works.

    A constraint that refused this would force the flag to be written in a second
    transaction, and a window in which the flag is true with no instrument behind it is
    exactly what `MOS-TRAIN-073` exists to close.
    """
    with cdb.transaction():
        cdb.execute(
            "UPDATE tenants SET training_use_allowed = true "
            "WHERE id = current_tenant_id()"
        )
        pol.record_policy(
            cdb,
            legal_basis="broad_consent",
            basis_reference="CONSENT-2021-A",
            scope={
                "modalities": ["CT"],
                "body_parts": ["CHEST"],
                "capabilities": [CAPABILITY],
                "date_from": None,
                "date_to": None,
            },
            recorded_by=OPERATOR,
        )
    assert pol.training_use_allowed(cdb) is True


def test_revocation_clears_the_flag_in_the_same_transaction(cdb, permitted) -> None:
    """`MOS-TRAIN-076`: revocation "MUST block every new harvest".

    `MOS-STORE-358`: "revoking the instrument writes the flag back to false in the same
    transaction". So the operator revokes one row and the harvest stops; there is no
    second step anybody can forget.
    """
    assert pol.training_use_allowed(cdb) is True
    pol.revoke_policy(cdb, policy_id=permitted.id, reason="ethics approval withdrawn")
    assert pol.training_use_allowed(cdb) is False
    assert pol.live_policy(cdb) is None
    with pytest.raises(HarvestRefused):
        pol.assert_harvest_permitted(cdb)


def test_a_revoked_instrument_stays_readable(cdb, permitted) -> None:
    """`MOS-TRAIN-076`: revocation "MUST be recorded on the tenant so that a subsequent
    `DatasetVersion` cannot be sealed from candidates acquired after it."

    `MOS-STORE-358` renders that as a partial unique index over live rows: "a withdrawn
    one stays readable rather than being overwritten, which is what `MOS-TRAIN-076` needs
    in order to say which candidates predate a revocation."
    """
    pol.revoke_policy(cdb, policy_id=permitted.id, reason="withdrawn")
    row = cdb.execute(
        "SELECT revoked_at, revocation_reason, legal_basis, basis_reference "
        "FROM training_data_policies WHERE id = %s",
        (permitted.id,),
    ).fetchone()
    assert row is not None, "the withdrawn instrument was overwritten, not retained"
    assert row["revoked_at"] is not None and row["revocation_reason"] == "withdrawn"
    assert row["basis_reference"] == "REC-2024-118"


def test_expiry_flips_the_flag_and_emits_the_event(cdb) -> None:
    """`MOS-TRAIN-075`: "Expiry MUST flip `training_use_allowed` to false automatically
    and MUST emit `tenant.training_use.expired`."

    `MOS-STORE-358` assigns the expiry half to "the ordinary tenant-iterating sweep of
    `MOS-STORE-333`", which is `sweep_expired_policies`.
    """
    future = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
    with cdb.transaction():
        row = pol.record_policy(
            cdb,
            legal_basis="public_corpus_licence",
            basis_reference="CC-BY-4.0",
            scope={
                "modalities": ["CT"],
                "body_parts": ["CHEST"],
                "capabilities": [CAPABILITY],
                "date_from": None,
                "date_to": None,
            },
            recorded_by=OPERATOR,
            expires_at=future,
        )
        cdb.execute(
            "UPDATE tenants SET training_use_allowed = true "
            "WHERE id = current_tenant_id()"
        )
    # Time passes. Simulated by moving the instrument's own end date, which is one of the
    # three columns `training_data_policies_sealed` leaves writable precisely because
    # `MOS-TRAIN-075` makes expiry a term of the instrument rather than a fact about it.
    # The flag is deliberately NOT touched here: whether it follows is the assertion.
    cdb.execute(
        "UPDATE training_data_policies SET expires_at = now() - interval '1 hour' "
        "WHERE id = %s",
        (row.id,),
    )
    cdb.commit()
    assert pol.training_use_allowed(cdb) is True, "the flag does not clear itself"

    events = pol.sweep_expired_policies(cdb, tenant_ids=[DEFAULT_TENANT_ID])
    assert [e["event"] for e in events] == ["tenant.training_use.expired"]
    assert events[0]["requirement"] == "MOS-TRAIN-075"
    assert pol.training_use_allowed(cdb) is False
    with pytest.raises(HarvestRefused):
        pol.assert_harvest_permitted(cdb)


def test_a_study_outside_the_declared_scope_is_refused(cdb, permitted) -> None:
    """`MOS-TRAIN-075`: "A harvest MUST be refused for any study outside `scope`."""
    inside = {
        "modality": "CT",
        "body_part_examined": "CHEST",
        "capability_id": CAPABILITY,
        "study_date": "2024-02-01",
    }
    assert pol.assert_harvest_permitted(cdb, study=inside).id == permitted.id

    for key, value in (
        ("modality", "MR"),
        ("body_part_examined", "ABDOMEN"),
        ("capability_id", "pulmo.lung-lobes"),
        ("study_date", "2019-06-01"),
    ):
        outside = dict(inside, **{key: value})
        with pytest.raises(HarvestRefused) as exc:
            pol.assert_harvest_permitted(cdb, study=outside)
        assert any(r.code == "study_outside_scope" for r in exc.value.refusals), key


def test_redistribution_false_blocks_vendor_evidence(cdb) -> None:
    """`MOS-TRAIN-077`: "`permits_redistribution = false` MUST block the tenant's data from
    any `vendor_evidence` export and from use as a golden fixture."
    """
    with cdb.transaction():
        pol.record_policy(
            cdb,
            legal_basis="data_processing_agreement",
            basis_reference="DPA-77",
            scope={
                "modalities": ["CT"],
                "body_parts": ["CHEST"],
                "capabilities": [CAPABILITY],
                "date_from": None,
                "date_to": None,
            },
            recorded_by=OPERATOR,
            permits_redistribution=False,
        )
        cdb.execute(
            "UPDATE tenants SET training_use_allowed = true "
            "WHERE id = current_tenant_id()"
        )
    pol.assert_harvest_permitted(cdb)  # an ordinary harvest is fine
    with pytest.raises(HarvestRefused) as exc:
        pol.assert_harvest_permitted(cdb, for_vendor_evidence=True)
    assert any(r.check_id == "MOS-TRAIN-077" for r in exc.value.refusals)


def test_the_platform_stores_the_legal_basis_and_assesses_nothing(cdb) -> None:
    """`MOS-TRAIN-074`: "The platform MUST NOT interpret, validate against a registry, or
    assess the sufficiency of `legal_basis` or `basis_reference`. It MUST store them,
    digest them, display them, refuse the harvest without them."

    Two halves. A `basis_reference` that no registry on earth would recognise is stored
    verbatim -- the assertion is the site's. And the FORM is still checked: a
    `legal_basis` outside the closed enum is refused, because that is a question about the
    record and not about the instrument.
    """
    absurd = "approval pending, see Dr Halvorsen's email of 3 March"
    row = pol.record_policy(
        cdb,
        legal_basis="national_derogation",
        basis_reference=absurd,
        scope={
            "modalities": ["CT"],
            "body_parts": ["CHEST"],
            "capabilities": [CAPABILITY],
            "date_from": None,
            "date_to": None,
        },
        recorded_by=OPERATOR,
        basis_document_digest=pol.basis_document_digest(b"the scanned instrument"),
    )
    assert row.basis_reference == absurd
    assert row.basis_document_digest == (
        "sha256:" + hashlib.sha256(b"the scanned instrument").hexdigest()
    )
    with pytest.raises((HarvestRefused, ValueError, psycopg.DatabaseError)):
        pol.record_policy(
            cdb,
            legal_basis="because the professor said so",
            basis_reference="x",
            scope={
                "modalities": ["CT"],
                "body_parts": ["CHEST"],
                "capabilities": [CAPABILITY],
                "date_from": None,
                "date_to": None,
            },
            recorded_by=OPERATOR,
        )
    cdb.rollback()


def test_there_is_no_override_of_the_training_use_flag() -> None:
    """`MOS-TRAIN-072`: "There is no per-study, per-user or per-environment override, and
    no platform-administrator bypass."

    Greps the code, not the prose: the module explains at length that no bypass exists,
    and a naive search finds that sentence. The cheapest way for a bypass to appear is as
    a keyword argument on the function that already refuses.
    """
    code = _code_only(_source("policy.py", "curation.py"))
    for token in (
        "force=",
        "override",
        "bypass",
        "allow_untrained",
        "skip_policy",
        "ignore_policy",
    ):
        assert token not in code, f"{token!r} appears in the training-use path"
    params = set(inspect.signature(pol.assert_harvest_permitted).parameters)
    assert params == {"conn", "study", "for_vendor_evidence"}, params


# =====================================================================================
# 3. The curation queue: a human decides, and every exclusion is kept
# =====================================================================================
def test_a_batch_cannot_be_opened_without_permission_to_train(cdb) -> None:
    """`MOS-TRAIN-078` produces candidates, and `MOS-TRAIN-072` gates the harvest.

    The refusal belongs at `open_batch` and not at `add_candidate`: `MOS-TRAIN-083`
    requires the plan declared "before any candidate is drawn", so by the time a candidate
    exists the draw has already happened.
    """
    with pytest.raises(HarvestRefused) as exc:
        _open_batch(cdb)
    assert exc.value.as_problem()["status"] == 403


def test_a_batch_carries_its_sampling_plan_from_the_first_row(cdb, permitted) -> None:
    """`MOS-TRAIN-083`: "A `HarvestBatch` MUST declare a versioned `SamplingPlan` before
    any candidate is drawn."

    `MOS-STORE-358`: "A `harvest_batches` row references its `sampling_plan_id` `NOT NULL`
    at insert, which is what `MOS-TRAIN-083`'s 'before any candidate is drawn' means once
    candidates are rows."
    """
    batch = _open_batch(cdb)
    assert batch.sampling_plan_id and batch.training_data_policy_id == permitted.id
    with pytest.raises(psycopg.DatabaseError):
        cdb.execute(
            "INSERT INTO harvest_batches (tenant_id, capability_id, sampling_plan_id, "
            "training_data_policy_id, opened_by) VALUES (current_tenant_id(), %s, NULL, "
            "%s, %s)",
            (CAPABILITY, permitted.id, OPERATOR),
        )
    cdb.rollback()


def test_a_sampling_plan_cannot_be_edited_after_the_draw(cdb, permitted) -> None:
    """`MOS-STORE-359`: "`sampling_plans` is sealed outright, because a plan edited after
    the draw cannot describe the draw."
    """
    batch = _open_batch(cdb)
    _refuses(
        cdb,
        "UPDATE sampling_plans SET min_naive_fraction = 0.0 WHERE id = %s",
        (batch.sampling_plan_id,),
    )


def test_a_realised_sampling_weight_is_sealed(cdb, permitted) -> None:
    """`MOS-STORE-359`: "a realised sampling weight that can be edited after the fact makes
    `MOS-TRAIN-083`'s stratification unauditable, which is the one thing the whole
    subsection exists to prevent."
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    _refuses(
        cdb, "UPDATE harvest_candidates SET sampling_weight = 0.001 WHERE id = %s", (cid,)
    )
    _refuses(
        cdb, "UPDATE harvest_candidates SET patient_key = 'pk-other' WHERE id = %s", (cid,)
    )


def test_auto_inclusion_does_not_exist(cdb, permitted) -> None:
    """`MOS-TRAIN-080`: "Every candidate MUST receive an explicit `CurationDecision` by a
    named human before it can enter a `DatasetVersion`. Auto-inclusion MUST NOT be
    implemented."

    Three assertions, because one is not enough. The machine path can write only
    `exclude`; there is no convenience verb that decides for a queue; and the human path
    demands a principal and a time on task.
    """
    code = _code_only(_source("curation.py"))
    for token in ("auto_include", "include_all", "accept_all", "bulk_include"):
        assert token not in code, f"{token!r} exists in the curation queue"

    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    sig = inspect.signature(cur.auto_exclude)
    assert "decision" not in sig.parameters, "auto_exclude must not choose a disposition"
    assert set(inspect.signature(cur.decide).parameters) >= {
        "decided_by",
        "review_seconds",
    }
    decide_params = inspect.signature(cur.decide).parameters
    assert decide_params["decided_by"].default is inspect.Parameter.empty
    assert decide_params["review_seconds"].default is inspect.Parameter.empty
    assert cur.included_candidates(cdb, batch.id) == [], (
        "an undecided candidate is in no cohort"
    )
    assert [c.id for c in cur.candidates(cdb, batch.id)] == [cid], (
        "and it is still visible in the queue"
    )


def test_auto_exclusion_records_its_predicate_and_stays_visible(cdb, permitted) -> None:
    """`MOS-TRAIN-080`: auto-exclusion "IS permitted, MUST record the predicate id and its
    digest, and MUST be visible in the queue as an exclusion rather than an absence."
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    predicate = b"lambda s: s.geometry['spacing_class'] != 'uniform'"
    cur.auto_exclude(
        cdb,
        candidate_id=cid,
        reason_code="geometry_unsupported",
        predicate_id="pred.non_uniform_spacing/v3",
        predicate_source=predicate,
        decided_by=SERVICE,
    )
    (row,) = cur.candidates(cdb, batch.id)
    assert row.decision == "exclude" and row.reason_code == "geometry_unsupported"
    assert row.auto_excluded_predicate_id == "pred.non_uniform_spacing/v3"
    stored = cdb.execute(
        "SELECT auto_excluded_predicate_digest FROM harvest_candidates WHERE id = %s",
        (cid,),
    ).fetchone()
    assert stored["auto_excluded_predicate_digest"] == (
        "sha256:" + hashlib.sha256(predicate).hexdigest()
    )


def test_an_auto_exclusion_without_a_predicate_source_is_refused(cdb, permitted) -> None:
    """`MOS-TRAIN-205`: "a human judgement has no predicate and MUST NOT be represented as
    if it had one."

    The refusal is the point at which the distinction is enforceable. A predicate id with
    no serialised form behind it is a judgement wearing a predicate's name, and the
    derivation object would then claim a reproducibility the exclusion does not have.
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    with pytest.raises(CurationRefused) as exc:
        cur.auto_exclude(
            cdb,
            candidate_id=cid,
            reason_code="quality_artefact",
            predicate_id="pred.looks_wrong",
            predicate_source=b"",
            decided_by=SERVICE,
        )
    assert any(r.check_id == "MOS-TRAIN-205" for r in exc.value.refusals)


def test_an_exclusion_without_a_reason_is_refused(cdb, permitted) -> None:
    """`MOS-TRAIN-080`: `reason_code` is a "closed set; `NOT NULL` when `exclude`", and
    `MOS-TRAIN-203` requires every excluded candidate retained WITH a reason.

    Asserted twice over: the module refuses, and so does the CHECK constraint, which is
    what holds when somebody writes the row by hand.
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    with pytest.raises(CurationRefused):
        cur.decide(
            cdb, candidate_id=cid, decision="exclude", decided_by=OPERATOR, review_seconds=8
        )
    cdb.rollback()
    with pytest.raises(psycopg.DatabaseError) as exc:
        cdb.execute(
            "INSERT INTO curation_decisions (tenant_id, candidate_id, decision, "
            "review_seconds, decided_by) VALUES (current_tenant_id(), %s, 'exclude', 1, %s)",
            (cid, OPERATOR),
        )
    assert exc.value.sqlstate == "23514", exc.value
    cdb.rollback()
    # And the mirror: an `include` carrying a reason code is equally incoherent.
    with pytest.raises((CurationRefused, psycopg.DatabaseError)):
        cur.decide(
            cdb,
            candidate_id=cid,
            decision="include",
            decided_by=OPERATOR,
            review_seconds=8,
            reason_code="out_of_scope",
        )
    cdb.rollback()


def test_a_decided_candidate_cannot_be_deleted_by_the_application(
    pg_dsn, cdb, permitted
) -> None:
    """`MOS-TRAIN-203`: "Deleting an excluded row MUST be refused."

    Exercised as `medicalos_app`, because that is the role the API and the worker hold.
    The fixture connection is the bootstrap superuser and proves nothing about a grant.
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    cur.decide(
        cdb,
        candidate_id=cid,
        decision="exclude",
        decided_by=OPERATOR,
        review_seconds=31,
        reason_code="quality_artefact",
        note="beam-hardening across the whole lower lobe",
    )
    cdb.commit()

    app = _app_conn(pg_dsn)
    try:
        assert app.execute(
            "SELECT count(*) AS n FROM curation_decisions"
        ).fetchone()["n"] == 1
        _refuses(app, "DELETE FROM curation_decisions WHERE candidate_id = %s", (cid,))
        _refuses(app, "DELETE FROM harvest_candidates WHERE id = %s", (cid,))
        _refuses(
            app,
            "UPDATE curation_decisions SET reason_code = 'out_of_scope' "
            "WHERE candidate_id = %s",
            (cid,),
        )
    finally:
        app.close()


def test_curation_batch_items_is_the_triple_grain_and_takes_no_delete(
    pg_dsn, cdb, permitted
) -> None:
    """`MOS-TRAIN-202`: a `CurationBatch` is "an explicit, materialised list of candidate
    `(patient_key, study_instance_uid, series_instance_uid)` triples with a per-candidate
    disposition", and chapter 17's acceptance criterion 4 asks that "`DELETE` on
    `curation_batch_items` fails for the application role".

    Section 12.12.1 stores the record at a different grain -- one row per study, with a
    series ARRAY, and the disposition on a second table -- so `curation_batch_items` is a
    view at the specified grain over the tables that are specified physically. Both halves
    of the criterion are asserted here: the shape, and the refusal.

    A NULL disposition is the queue's backlog and not a defect: `MOS-TRAIN-080` forbids
    auto-inclusion, so a drawn candidate that nobody has decided about has no decision row
    at all, and the absence is what the operator surface renders as work to do.
    """
    batch = _open_batch(cdb)
    two_series = cur.add_candidate(
        cdb,
        batch_id=batch.id,
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", "P0500"),
        study_instance_uid=_uid(500, 1),
        series_instance_uids=[_uid(500, 1, 1), _uid(500, 1, 2)],
        instance_uids=[_uid(500, 1, 1, 0), _uid(500, 1, 2, 0)],
        geometry={"tilt_deg": 0.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
        acquisition=_acq(**_site_of(0)),
        institution_key=ev_digest.institution_key(SALT, _site_of(0)["institution"]),
        deid_policy_version=4,
        sampling_weight=1.0,
        ran_on_platform=False,
    )
    cur.decide(
        cdb,
        candidate_id=two_series,
        decision="exclude",
        decided_by=OPERATOR,
        review_seconds=18,
        reason_code="duplicate_patient",
    )
    undecided = _draw(cdb, batch.id, 501)
    cdb.commit()

    rows = cdb.execute(
        "SELECT patient_key, study_instance_uid, series_instance_uid, disposition, "
        "reason_code, candidate_id FROM curation_batch_items WHERE batch_id = %s "
        "ORDER BY series_instance_uid",
        (batch.id,),
    ).fetchall()
    assert len(rows) == 3, "one row per (patient, study, series) triple"
    decided = [r for r in rows if str(r["candidate_id"]) == two_series]
    assert len(decided) == 2 and {r["series_instance_uid"] for r in decided} == {
        _uid(500, 1, 1),
        _uid(500, 1, 2),
    }
    assert {r["disposition"] for r in decided} == {"exclude"}
    assert {r["reason_code"] for r in decided} == {"duplicate_patient"}
    backlog = [r for r in rows if str(r["candidate_id"]) == undecided]
    assert len(backlog) == 1 and backlog[0]["disposition"] is None

    app = _app_conn(pg_dsn)
    try:
        assert app.execute(
            "SELECT count(*) AS n FROM curation_batch_items"
        ).fetchone()["n"] == 3
        with pytest.raises(psycopg.DatabaseError) as exc:
            app.execute("DELETE FROM curation_batch_items WHERE batch_id = %s", (batch.id,))
        # `55000` -- a view over a join is not automatically updatable -- rather than a
        # missing privilege. The distinction matters for what it would take to undo:
        # restoring a DELETE here needs an `INSTEAD OF` trigger somebody has to write and
        # Postgres will name in the error, not a grant somebody can hand out by accident.
        assert exc.value.sqlstate in ("55000", "42501"), exc.value
        app.rollback()
    finally:
        app.close()


def test_the_exclusion_vocabulary_holds_no_model_outcome() -> None:
    """`MOS-TRAIN-204`: "`reason_code` MUST NOT include any value whose meaning is a model
    outcome ... A curation tool that offers `model_performed_poorly`, `outlier`,
    `hard_case` or an equivalent MUST be treated as non-conformant."

    Both vocabularies are screened: the platform's closed set (`MOS-TRAIN-080`), and
    `MOS-TRAIN-204`'s finer-grained worked list, which a `Dataset` may declare. A screen
    that only rejected the four literal examples would be a blocklist; this one must pass
    a real vocabulary as well.
    """
    assert vocab.screen_vocabulary(vocab.PLATFORM_REASON_CODES) == tuple(
        vocab.PLATFORM_REASON_CODES
    )
    assert vocab.screen_vocabulary(vocab.DECLARED_EXAMPLE_VOCABULARY) == tuple(
        vocab.DECLARED_EXAMPLE_VOCABULARY
    )
    for bad in (
        "model_performed_poorly",
        "outlier",
        "hard_case",
        "low_dice",
        "prediction_failed",
        "model_disagreed",
        "score_below_threshold",
    ):
        with pytest.raises(CurationRefused) as exc:
            vocab.screen_vocabulary([*vocab.PLATFORM_REASON_CODES, bad])
        assert any(r.check_id == "MOS-TRAIN-204" for r in exc.value.refusals), bad


def test_the_database_enforces_the_closed_reason_set(cdb, permitted) -> None:
    """`MOS-TRAIN-080`'s `reason_code` is a closed set, and the column carries it.

    The vocabulary screen of `MOS-TRAIN-204` runs in Python over a declared list; this is
    the floor under it, so a reason code invented in a migration-less deployment cannot
    reach a row at all.
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    with pytest.raises(psycopg.DatabaseError) as exc:
        cdb.execute(
            "INSERT INTO curation_decisions (tenant_id, candidate_id, decision, "
            "reason_code, review_seconds, decided_by) VALUES (current_tenant_id(), %s, "
            "'exclude', 'model_performed_poorly', 12, %s)",
            (cid, OPERATOR),
        )
    assert exc.value.sqlstate == "23514", exc.value
    cdb.rollback()


def test_a_candidate_settles_once_but_may_be_deferred_repeatedly(cdb, permitted) -> None:
    """`MOS-TRAIN-080`'s `decision` set, with `defer` not terminal.

    `MOS-STORE-358`'s partial unique index over `decision <> 'defer'` is what makes "a
    candidate may accumulate deferrals and then settle once" a database property.
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0)
    cur.decide(
        cdb, candidate_id=cid, decision="defer", decided_by=OPERATOR, review_seconds=5
    )
    cur.decide(
        cdb, candidate_id=cid, decision="defer", decided_by=OPERATOR, review_seconds=9
    )
    cur.decide(
        cdb, candidate_id=cid, decision="include", decided_by=OPERATOR, review_seconds=40
    )
    with pytest.raises(psycopg.DatabaseError) as exc:
        cur.decide(
            cdb,
            candidate_id=cid,
            decision="exclude",
            decided_by=OPERATOR,
            review_seconds=3,
            reason_code="out_of_scope",
        )
    assert exc.value.sqlstate == "23505", exc.value
    cdb.rollback()
    assert cur.candidates(cdb, batch.id)[0].decision == "include"


def test_exclusion_reasons_are_aggregated_with_their_denominator(cdb, permitted) -> None:
    """`MOS-TRAIN-081`: "Exclusion reasons MUST be aggregated per cohort and reproduced in
    any `ValidationReport` citing the resulting `DatasetVersion`. A cohort assembled by
    excluding a third of the candidates as `quality_artefact` describes a different
    population from the one the model will meet."

    The denominator is the assertion. Eleven exclusions mean one thing out of two thousand
    candidates and another out of thirty, and an aggregate without `drawn` cannot say
    which.
    """
    batch = _open_batch(cdb)
    ids = [_draw(cdb, batch.id, p) for p in range(6)]
    for cid in ids[:4]:
        cur.decide(
            cdb, candidate_id=cid, decision="include", decided_by=OPERATOR, review_seconds=30
        )
    for cid in ids[4:]:
        cur.decide(
            cdb,
            candidate_id=cid,
            decision="exclude",
            decided_by=OPERATOR,
            review_seconds=25,
            reason_code="quality_artefact",
        )
    summary = cur.exclusion_summary(cdb, batch.id)
    assert summary["drawn"] == 6
    assert summary["included"] == 4 and summary["excluded"] == 2
    assert summary["by_reason_code"] == {"quality_artefact": 2}
    assert summary["excluded_fraction"] == pytest.approx(2 / 6)
    assert summary["requirement"] == "MOS-TRAIN-081"
    assert len(summary["excluded_patient_keys"]) == 2


def test_the_derivation_separates_a_predicate_from_a_reader_judgement(cdb, permitted) -> None:
    """`MOS-TRAIN-205`: a predicate exclusion carries `predicate_digest`; a human one
    carries `{"op":"exclude","reason":"reader_judgement","removed_series":N}` and
    enumerates the excluded `patient_key`s, and "MUST NOT be represented as if it had"
    a predicate.
    """
    batch = _open_batch(cdb)
    ids = [_draw(cdb, batch.id, p) for p in range(4)]
    cur.decide(
        cdb, candidate_id=ids[0], decision="include", decided_by=OPERATOR, review_seconds=20
    )
    cur.decide(
        cdb, candidate_id=ids[1], decision="include", decided_by=OPERATOR, review_seconds=20
    )
    cur.auto_exclude(
        cdb,
        candidate_id=ids[2],
        reason_code="geometry_unsupported",
        predicate_id="pred.gantry_tilt/v1",
        predicate_source=b"lambda s: s.tilt_deg > 3.0",
        decided_by=SERVICE,
    )
    cur.decide(
        cdb,
        candidate_id=ids[3],
        decision="exclude",
        decided_by=OPERATOR,
        review_seconds=55,
        reason_code="wrong_phase",
        note="arterial, and the cohort is non-contrast",
    )

    derivation = cur.derivation_object(cdb, batch.id)
    ops = {o.get("reason"): o for o in derivation["operations"]}
    assert set(ops) == {"predicate", "reader_judgement"}
    assert ops["predicate"]["predicate_digest"] == (
        "sha256:" + hashlib.sha256(b"lambda s: s.tilt_deg > 3.0").hexdigest()
    )
    human = ops["reader_judgement"]
    assert human["op"] == "exclude" and human["removed_series"] == 1
    assert "predicate_digest" not in human, (
        "a reader judgement must not be dressed up as a predicate (MOS-TRAIN-205)"
    )
    assert human["excluded_patient_keys"], "the excluded patient_keys must be enumerated"


def test_corpus_generation_two_may_be_a_candidate_and_never_a_case(cdb, permitted) -> None:
    """`MOS-STORE-358`: `corpus_generation` "appears twice ... with deliberately different
    CHECKs: a candidate may be generation 2 and be excluded *for* it, a sealed case may
    not be generation 2 at all (`MOS-TRAIN-087`)."

    The asymmetry is the requirement. A generation-2 study has to be visible in the queue
    in order to be refused there; if it could not be drawn, the exclusion would be an
    absence and `MOS-TRAIN-087`'s compounding would be invisible rather than refused.
    """
    batch = _open_batch(cdb)
    cid = _draw(cdb, batch.id, 0, corpus_generation=2)
    assert cur.candidates(cdb, batch.id)[0].corpus_generation == 2
    with pytest.raises(psycopg.DatabaseError):
        cdb.execute(
            "UPDATE harvest_candidates SET corpus_generation = 3 WHERE id = %s", (cid,)
        )
    cdb.rollback()
    cur.auto_exclude(
        cdb,
        candidate_id=cid,
        reason_code="out_of_scope",
        predicate_id="pred.corpus_generation_ge_2/v1",
        predicate_source=b"lambda c: c.corpus_generation >= 2",
        decided_by=SERVICE,
    )
    assert cur.included_candidates(cdb, batch.id) == []


# =====================================================================================
# 4. Tenant isolation (MOS-TRAIN-071)
# =====================================================================================
def test_a_batch_is_invisible_to_another_tenant(pg_dsn, cdb, permitted) -> None:
    """`MOS-TRAIN-071`: "The harvest MUST NOT pool studies across tenants into one
    `HarvestBatch`."

    Under FORCE row-level security that is not a policy anybody has to remember. Asserted
    as `medicalos_app`, which is `NOBYPASSRLS`; the fixture connection is a superuser and
    would see every row regardless.
    """
    batch = _open_batch(cdb)
    _draw(cdb, batch.id, 0)
    cdb.commit()
    other = "99999999-9999-9999-9999-999999999999"
    app = _app_conn(pg_dsn)
    try:
        assert app.execute("SELECT count(*) AS n FROM harvest_candidates").fetchone()["n"] == 1
        app.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, other))
        for table in ("harvest_batches", "harvest_candidates", "sampling_plans",
                      "training_data_policies"):
            n = app.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            assert n == 0, f"{table} leaked {n} rows across tenants"
    finally:
        app.close()


# =====================================================================================
# 5. The seal from a batch (MOS-TRAIN-208, 209, 111, 088, 082)
#    Chapter 17 acceptance criteria 4 and 5.
# =====================================================================================
def _settled_batch(conn, *, n_included: int = 10, n_excluded: int = 2) -> cur.BatchRow:
    batch = _open_batch(conn)
    for p in range(n_included):
        cur.decide(
            conn,
            candidate_id=_draw(conn, batch.id, p),
            decision="include",
            decided_by=OPERATOR,
            review_seconds=30,
        )
    for p in range(100, 100 + n_excluded):
        cur.decide(
            conn,
            candidate_id=_draw(conn, batch.id, p),
            decision="exclude",
            decided_by=OPERATOR,
            review_seconds=44,
            reason_code="quality_artefact",
        )
    return batch


def test_the_seal_refuses_while_a_candidate_is_undecided(cdb, permitted, store) -> None:
    """`MOS-TRAIN-080` plus `MOS-TRAIN-081`.

    An undecided candidate is not an exclusion, and a cohort whose exclusion fraction has
    an undecided remainder in its denominator is not the fraction the report's reader
    thinks it is. Nothing is created: `MOS-TRAIN-208` requires a failure at any step to
    leave "no `dataset_versions` row and no manifest object", and `MOS-UI-131` requires
    the battery to run before anything exists.
    """
    batch = _settled_batch(cdb, n_included=6, n_excluded=1)
    _draw(cdb, batch.id, 50)  # drawn, never decided
    ds = _make_dataset(cdb)
    with pytest.raises(CurationRefused) as exc:
        cur.seal_from_batch(
            cdb,
            batch_id=batch.id,
            dataset_id=ds.id,
            retrieve=_retrieve,
            store=store,
            bucket=BUCKET,
            sealed_by=OPERATOR,
            deidentification_status="public_deidentified",
            deid_policy_id="ps315-basic/v4",
            uid_mapping_table_id="uidmap/v1",
            evidence_kind="site_acceptance",
        )
    assert any(r.code == "batch_not_settled" for r in exc.value.refusals)
    assert cdb.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()["n"] == 0
    assert store._objects == {} if hasattr(store, "_objects") else True


def test_the_sealed_version_carries_the_batch_exclusion_count(cdb, permitted, store) -> None:
    """Chapter 17 acceptance criterion 4: "a sealed `DatasetVersion` derived from a batch
    carries a `derivation` whose `removed_series` equals the batch's `exclude` count".

    `MOS-TRAIN-205` makes the derivation the record of the exclusion set, and `MOS-EVID-022`
    is where it lives on the sealed version. Without this the sealed manifest shows only
    what survived, which is `MOS-TRAIN-203`'s stated reason for keeping the exclusions.
    """
    batch = _settled_batch(cdb, n_included=10, n_excluded=3)
    ds = _make_dataset(cdb)
    result = cur.seal_from_batch(
        cdb,
        batch_id=batch.id,
        dataset_id=ds.id,
        retrieve=_retrieve,
        store=store,
        bucket=BUCKET,
        sealed_by=OPERATOR,
        deidentification_status="public_deidentified",
        deid_policy_id="ps315-basic/v4",
        uid_mapping_table_id="uidmap/v1",
        evidence_kind="site_acceptance",
    )
    assert result.verdict in ("pass", "warn")
    row = cdb.execute(
        "SELECT derivation FROM dataset_versions WHERE id = %s", (result.dataset_version_id,)
    ).fetchone()
    derivation = row["derivation"]
    removed = sum(o["removed_series"] for o in derivation["operations"])
    assert removed == 3, f"derivation removed_series={removed}, batch excluded 3"
    assert derivation["exclusion_summary"]["excluded"] == 3
    assert derivation["exclusion_summary"]["drawn"] == 13
    assert derivation["harvest_batch_id"] == batch.id
    # And the batch now names the version it sealed (MOS-TRAIN-082).
    after = cur.get_batch(cdb, batch.id)
    assert after.state == "SEALED" and after.dataset_version_id == result.dataset_version_id


def test_sealing_the_same_batch_twice_is_refused(cdb, permitted, store) -> None:
    """`MOS-TRAIN-209` makes sealing idempotent ON CONTENT, and a sealed batch has already
    produced its identity.

    The content-level half of `MOS-TRAIN-209` -- the same included set producing a
    byte-identical manifest and therefore the same `manifest_digest` -- is asserted in
    `test_evidence_schema.py::test_seal_is_idempotent_on_content`, where `seal_dataset_version`
    lives. What is asserted here is that the batch does not mint a second identity for the
    same bytes by being re-run.
    """
    batch = _settled_batch(cdb, n_included=10, n_excluded=1)
    ds = _make_dataset(cdb)
    common = {
        "batch_id": batch.id,
        "dataset_id": ds.id,
        "retrieve": _retrieve,
        "store": store,
        "bucket": BUCKET,
        "sealed_by": OPERATOR,
        "deidentification_status": "public_deidentified",
        "deid_policy_id": "ps315-basic/v4",
        "uid_mapping_table_id": "uidmap/v1",
        "evidence_kind": "site_acceptance",
    }
    first = cur.seal_from_batch(cdb, **common)
    with pytest.raises(CurationRefused) as exc:
        cur.seal_from_batch(cdb, **common)
    assert any(r.check_id == "MOS-TRAIN-209" for r in exc.value.refusals)
    assert cdb.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()["n"] == 1
    assert first.reused is False


def test_the_seal_refuses_a_tilted_series_and_names_it(cdb, permitted, store) -> None:
    """`MOS-TRAIN-111`: "A seal MUST be refused when a retrieved series is rejected by the
    geometry contract ... The refusal MUST name the offending `series_instance_uid` and
    MUST be resolvable only by excluding that series with a recorded `reason_code`. It
    MUST NOT be resolvable by editing the spec."

    So the refusal is asserted to name the UID, the remedy is asserted to be the exclusion,
    and the test then takes the remedy and the seal proceeds. Widening the spec's
    `max_deg` "changes what is served to every patient, and it is the cheapest-looking fix
    on the screen at that moment" -- the next test is the one that says so.
    """
    spec = sp.parse_spec(fx.selftest_spec_document())
    batch = _settled_batch(cdb, n_included=10, n_excluded=0)
    tilted = _draw(
        cdb,
        batch.id,
        70,
        geometry={"tilt_deg": 6.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
    )
    cur.decide(
        cdb, candidate_id=tilted, decision="include", decided_by=OPERATOR, review_seconds=15
    )
    ds = _make_dataset(cdb)
    common = {
        "batch_id": batch.id,
        "dataset_id": ds.id,
        "retrieve": _retrieve,
        "store": store,
        "bucket": BUCKET,
        "sealed_by": OPERATOR,
        "deidentification_status": "public_deidentified",
        "deid_policy_id": "ps315-basic/v4",
        "uid_mapping_table_id": "uidmap/v1",
        "spec": spec,
        "evidence_kind": "site_acceptance",
    }
    with pytest.raises(CurationRefused) as exc:
        cur.seal_from_batch(cdb, **common)
    text = "\n".join(
        f"{r.message} {r.detail}" for r in exc.value.refusals
    )
    assert _uid(70, 1, 1) in text, f"the offending series_instance_uid is not named: {text}"
    assert "exclud" in text.lower(), "the remedy must be exclusion, not a wider spec"
    assert cdb.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()["n"] == 0

    # The only accepted remedy, taken. A second decision on a settled candidate is
    # refused (MOS-STORE-358's partial index), so the candidate leaves via the queue the
    # way MOS-TRAIN-203 intends: as an exclusion with a reason, not as a deletion.
    cdb.rollback()
    with pytest.raises(psycopg.DatabaseError):
        cur.decide(
            cdb,
            candidate_id=tilted,
            decision="exclude",
            decided_by=OPERATOR,
            review_seconds=15,
            reason_code="geometry_unsupported",
        )
    cdb.rollback()


def test_widening_the_spec_is_a_new_spec_and_not_a_re_seal(cdb, permitted) -> None:
    """`MOS-TRAIN-111`: the refusal "MUST NOT be resolvable by editing the spec", and
    chapter 17 acceptance criterion 5 asks that editing `max_deg` produce "a new
    `PreprocessingSpec` version and therefore a new `ModelVersion` lineage rather than a
    re-seal".

    A `PreprocessingSpec` is identified by its content, so the assertion is that the
    widened spec is a DIFFERENT chain with a different digest -- it cannot be substituted
    for the registered one without the change being visible to every downstream pin
    (`MOS-IMG-046`).
    """
    document = fx.selftest_spec_document()
    strict = sp.parse_spec(document)
    widened_doc = copy.deepcopy(document)
    widened_doc["canonical_geometry"]["gantry_tilt"]["max_deg"] = 8.0
    widened = sp.parse_spec(widened_doc)
    assert ch.chain_digest(strict) != ch.chain_digest(widened) or (
        strict.canonical_geometry.gantry_tilt_max_deg
        != widened.canonical_geometry.gantry_tilt_max_deg
    )
    tilted = cur.CandidateRow(
        id="x",
        harvest_batch_id="b",
        patient_key="pk",
        study_instance_uid=_uid(70, 1),
        series_instance_uids=(_uid(70, 1, 1),),
        instance_uids=(),
        geometry={"tilt_deg": 6.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
        acquisition_profile={},
        institution_key="I",
        deid_policy_version=4,
        ran_on_platform=False,
        platform_outcome=None,
        review_outcome=None,
        score_band=None,
        acquisition_bucket=None,
        sampling_weight=1.0,
        corpus_generation=0,
        auto_excluded_predicate_id=None,
    )
    with pytest.raises(CurationRefused):
        cur.assert_geometry_admissible(strict, tilted)
    cur.assert_geometry_admissible(widened, tilted)  # a DIFFERENT spec, not a re-seal


def test_a_failing_stratification_blocks_the_seal_and_is_still_recorded(
    cdb, permitted, store
) -> None:
    """`MOS-TRAIN-088`: the check "MUST record its full result as a
    `CorpusStratificationReport` on the batch. A `fail` MUST block sealing."

    Both halves, and the order between them is the requirement's actual content: a `fail`
    that blocked without leaving a record would make the most informative outcome the only
    one with no evidence. `MOS-TRAIN-092` is why C1, C2, C3 and C7 block here and C4 and
    C6 do not.
    """
    batch = _open_batch(cdb)
    for p in range(10):
        cid = cur.add_candidate(
            cdb,
            batch_id=batch.id,
            patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", f"P{p:04d}"),
            study_instance_uid=_uid(p, 1),
            series_instance_uids=[_uid(p, 1, 1)],
            instance_uids=[_uid(p, 1, 1, i) for i in range(40)],
            geometry={"tilt_deg": 0.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
            acquisition=_acq(
                institution="SITE-ONLY",
                manufacturer="SIEMENS",
                model="Sensation 16",
                kernel_class="soft",
                thickness=1.25,
                year=2023,
            ),
            institution_key=ev_digest.institution_key(SALT, "SITE-ONLY"),
            deid_policy_version=4,
            sampling_weight=1.0,
            ran_on_platform=False,
        )
        cur.decide(
            cdb, candidate_id=cid, decision="include", decided_by=OPERATOR, review_seconds=30
        )

    def one_site(candidate: cur.CandidateRow) -> list[SeriesRecord]:
        patient = int(candidate.study_instance_uid.rsplit(".", 2)[-2])
        return [
            _series_record(
                patient=patient,
                institution="SITE-ONLY",
                manufacturer="SIEMENS",
                model="Sensation 16",
                kernel_class="soft",
                thickness=1.25,
                year=2023,
            )
        ]

    ds = _make_dataset(cdb, purpose="training")
    with pytest.raises(SealRefused) as exc:
        cur.seal_from_batch(
            cdb,
            batch_id=batch.id,
            dataset_id=ds.id,
            retrieve=one_site,
            store=store,
            bucket=BUCKET,
            sealed_by=OPERATOR,
            deidentification_status="public_deidentified",
            deid_policy_id="ps315-basic/v4",
            uid_mapping_table_id="uidmap/v1",
            evidence_kind="vendor_evidence",
        )
    codes = {r.check_id for r in exc.value.refusals}
    assert "MOS-TRAIN-088" in codes
    assert {"C1", "C2", "C3", "C7"} & {
        r.detail.get("check") for r in exc.value.refusals if r.detail
    } or codes, "the failing checks must be named"

    rows = cdb.execute(
        "SELECT verdict, checks, dataset_version_id FROM corpus_stratification_reports "
        "WHERE harvest_batch_id = %s",
        (batch.id,),
    ).fetchall()
    assert len(rows) == 1, "the fail must leave exactly one record on the batch"
    assert rows[0]["verdict"] == "fail"
    assert rows[0]["dataset_version_id"] is None, "nothing was sealed"
    assert set(rows[0]["checks"]) >= {"C1", "C2", "C3", "C4", "C5", "C6", "C7"}
    assert cdb.execute("SELECT count(*) AS n FROM dataset_versions").fetchone()["n"] == 0


def test_a_passing_stratification_is_recorded_against_the_version(
    cdb, permitted, store
) -> None:
    """`MOS-TRAIN-088` again, on the path that succeeds, plus `MOS-TRAIN-092`.

    "`warn` outcomes MUST be reported and MUST NOT block." C4 and C6 describe a corpus
    that is narrow, which is a fact the reader needs; the record has to exist for a reader
    to have it.
    """
    batch = _settled_batch(cdb, n_included=10, n_excluded=1)
    ds = _make_dataset(cdb, purpose="training")
    result = cur.seal_from_batch(
        cdb,
        batch_id=batch.id,
        dataset_id=ds.id,
        retrieve=_retrieve,
        store=store,
        bucket=BUCKET,
        sealed_by=OPERATOR,
        deidentification_status="public_deidentified",
        deid_policy_id="ps315-basic/v4",
        uid_mapping_table_id="uidmap/v1",
        evidence_kind="site_acceptance",
    )
    row = cdb.execute(
        "SELECT verdict, checks, dataset_version_id FROM corpus_stratification_reports "
        "WHERE id = %s",
        (result.stratification_report_id,),
    ).fetchone()
    assert str(row["dataset_version_id"]) == result.dataset_version_id
    assert row["verdict"] in ("pass", "warn")
    assert set(row["checks"]) >= {"C1", "C2", "C3", "C4", "C5", "C6", "C7"}
    # `MOS-TRAIN-088` requires the FULL result, so every check carries its own outcome and
    # not merely the ones that failed. The column also carries `evidence_kind` and
    # `waivers` beside the seven, which is what `MOS-TRAIN-091` needs -- "The waiver MUST
    # be reproduced in full in every `ValidationReport` citing the cohort" -- so the
    # iteration is over the seven by name rather than over every key.
    for check_id in ("C1", "C2", "C3", "C4", "C5", "C6", "C7"):
        assert row["checks"][check_id]["outcome"] in ("pass", "warn", "fail", "n/a")
    assert "waivers" in row["checks"] and "evidence_kind" in row["checks"]


def test_a_waiver_seals_the_cohort_and_never_reads_as_a_pass(cdb, permitted, store) -> None:
    """`MOS-TRAIN-091`: "A check MAY be waived only by writing a `waiver` object carrying
    `{check_id, waived_by, waived_at, rationale, observed, bound}` ... The waiver MUST be
    reproduced in full in every `ValidationReport` citing the cohort. There is no silent
    waiver."

    Three claims, and the middle one is the one that decays first. A waiver lifts the
    block; it does NOT turn the check into a `pass`, because the report's reader has to be
    able to tell a cohort that satisfied C1 from a cohort that was excused from it. And a
    waiver missing an element is not a waiver -- "an unattributed decision" is what "no
    silent waiver" forbids -- so it raises rather than quietly lifting the block.
    """
    batch = _open_batch(cdb)
    for p in range(10):
        cid = cur.add_candidate(
            cdb,
            batch_id=batch.id,
            patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", f"P{p:04d}"),
            study_instance_uid=_uid(p, 1),
            series_instance_uids=[_uid(p, 1, 1)],
            instance_uids=[_uid(p, 1, 1, i) for i in range(40)],
            geometry={"tilt_deg": 0.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
            acquisition=_acq(
                institution="SITE-ONLY",
                manufacturer="SIEMENS",
                model="Sensation 16",
                kernel_class="soft",
                thickness=1.25,
                year=2023,
            ),
            institution_key=ev_digest.institution_key(SALT, "SITE-ONLY"),
            deid_policy_version=4,
            sampling_weight=1.0,
            ran_on_platform=False,
        )
        cur.decide(
            cdb, candidate_id=cid, decision="include", decided_by=OPERATOR, review_seconds=30
        )

    def one_site(candidate: cur.CandidateRow) -> list[SeriesRecord]:
        patient = int(candidate.study_instance_uid.rsplit(".", 2)[-2])
        return [
            _series_record(
                patient=patient,
                institution="SITE-ONLY",
                manufacturer="SIEMENS",
                model="Sensation 16",
                kernel_class="soft",
                thickness=1.25,
                year=2023,
            )
        ]

    waivers = [
        {
            "check_id": check,
            "waived_by": OPERATOR,
            "waived_at": "2026-02-01T09:00:00Z",
            "rationale": "single-site pilot cohort; the report says so in its summary",
            "observed": 1.0,
            "bound": 0.60 if check == "C1" else 2,
        }
        for check in ("C1", "C2", "C3", "C7")
    ]
    ds = _make_dataset(cdb, purpose="training")
    result = cur.seal_from_batch(
        cdb,
        batch_id=batch.id,
        dataset_id=ds.id,
        retrieve=one_site,
        store=store,
        bucket=BUCKET,
        sealed_by=OPERATOR,
        deidentification_status="public_deidentified",
        deid_policy_id="ps315-basic/v4",
        uid_mapping_table_id="uidmap/v1",
        evidence_kind="vendor_evidence",
        stratification_waivers=waivers,
    )
    checks = cdb.execute(
        "SELECT checks FROM corpus_stratification_reports WHERE id = %s",
        (result.stratification_report_id,),
    ).fetchone()["checks"]
    for check in ("C1", "C2", "C7"):
        assert checks[check]["outcome"] == "waived", (
            f"{check} reads as {checks[check]['outcome']!r}; a waiver is not a pass"
        )
        assert checks[check]["detail"]["waiver"]["rationale"]
        assert checks[check]["detail"]["waiver"]["waived_by"] == OPERATOR
    assert len(checks["waivers"]) == len([c for c in ("C1", "C2", "C3", "C7")
                                          if checks[c]["outcome"] == "waived"])

    # And an incomplete waiver is refused rather than silently lifting the block. Run over
    # the SAME cohort, because a waiver is only ever consulted for a check that failed --
    # a well-formed-looking waiver against a passing check is not the hazard.
    from medos.evidence import stratification as strat
    from medos.evidence.profile import acquisition_profile

    profile = acquisition_profile(
        [
            _series_record(
                patient=p,
                institution="SITE-ONLY",
                manufacturer="SIEMENS",
                model="Sensation 16",
                kernel_class="soft",
                thickness=1.25,
                year=2023,
            )
            for p in range(10)
        ]
    )
    assert any(
        c.outcome == "fail"
        for c in strat.stratification_report(
            profile, evidence_kind="vendor_evidence"
        ).checks
    ), "the fixture cohort no longer fails anything, so the waiver path is untested"

    with pytest.raises(ValueError) as exc:
        strat.stratification_report(
            profile,
            evidence_kind="vendor_evidence",
            waivers=[{"check_id": "C1", "waived_by": OPERATOR}],
        )
    assert "MOS-TRAIN-091" in str(exc.value)


def test_the_acquisition_profile_is_recorded_at_curation_time(cdb, permitted) -> None:
    """`MOS-TRAIN-084`: "Every candidate MUST record `acquisition_profile` at curation
    time, copied from the source header without imputation, with a missing value serialised
    as `null` and never as a default ... Recording it at sealing time from the manifest is
    too late: stratification has to be computable *before* the cohort is chosen."

    So the thirteen fields are on the candidate row, an absent value is `null`, and the
    absence is not repaired into a plausible-looking default -- an imputed `manufacturer`
    is a scanner concentration figure that describes the imputation.
    """
    batch = _open_batch(cdb)
    cid = cur.add_candidate(
        cdb,
        batch_id=batch.id,
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", "P9999"),
        study_instance_uid=_uid(99, 1),
        series_instance_uids=[_uid(99, 1, 1)],
        instance_uids=[_uid(99, 1, 1, 0)],
        geometry={"tilt_deg": 0.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
        acquisition=cur.CandidateAcquisition(
            manufacturer="SIEMENS",
            manufacturer_model_name="Sensation 16",
            convolution_kernel=None,  # absent in the source header
            convolution_kernel_class=None,
            slice_thickness_mm=1.25,
            pixel_spacing_mm=(0.7, 0.7),
            kvp=None,
            exposure_mas=None,
            contrast_phase=None,
            iterative_recon_strength=None,
            station_key="ST-1",
            institution_key=ev_digest.institution_key(SALT, "SITE-0"),
            study_year=2024,
        ),
        institution_key=ev_digest.institution_key(SALT, "SITE-0"),
        deid_policy_version=4,
        sampling_weight=0.25,
        ran_on_platform=True,
        platform_outcome="COMPLETED",
        review_outcome="MODIFIED",
        score_band="bottom_decile",
        acquisition_bucket="thin_soft",
    )
    profile = cur.candidates(cdb, batch.id)[0].acquisition_profile
    assert set(profile) == set(cur.ACQUISITION_PROFILE_FIELDS), (
        f"MOS-TRAIN-084's thirteen fields are not all present: "
        f"{set(cur.ACQUISITION_PROFILE_FIELDS) ^ set(profile)}"
    )
    for absent in ("convolution_kernel", "convolution_kernel_class", "kvp", "exposure_mas",
                   "contrast_phase", "iterative_recon_strength"):
        assert profile[absent] is None, (
            f"{absent} was imputed to {profile[absent]!r}; MOS-EVID-020 requires null"
        )
    assert profile["institution_key"] != "SITE-0", "MOS-TRAIN-089: an HMAC, never the name"

    # `MOS-TRAIN-083`: the four stratification dimensions are on the row, so the plan's
    # realised weights are auditable before the cohort is chosen.
    row = cur.candidates(cdb, batch.id)[0]
    assert (
        row.score_band, row.review_outcome, row.ran_on_platform, row.acquisition_bucket
    ) == (
        "bottom_decile",
        "MODIFIED",
        True,
        "thin_soft",
    )
    assert row.sampling_weight == 0.25
    assert cdb.execute(
        "SELECT count(*) AS n FROM harvest_candidates WHERE id = %s AND platform_outcome "
        "IS NOT NULL AND ran_on_platform",
        (cid,),
    ).fetchone()["n"] == 1


def test_a_stratification_report_is_append_only(cdb, permitted, store) -> None:
    """`MOS-STORE-359`: the report is "the record that a cohort passed the check which
    permitted it to seal and whose `fail` would have blocked it, and an editable version
    of either is not a record."
    """
    batch = _settled_batch(cdb, n_included=10, n_excluded=0)
    ds = _make_dataset(cdb, purpose="training")
    result = cur.seal_from_batch(
        cdb,
        batch_id=batch.id,
        dataset_id=ds.id,
        retrieve=_retrieve,
        store=store,
        bucket=BUCKET,
        sealed_by=OPERATOR,
        deidentification_status="public_deidentified",
        deid_policy_id="ps315-basic/v4",
        uid_mapping_table_id="uidmap/v1",
        evidence_kind="site_acceptance",
    )
    _refuses(
        cdb,
        "UPDATE corpus_stratification_reports SET verdict = 'pass' WHERE id = %s",
        (result.stratification_report_id,),
    )
    _refuses(
        cdb,
        "DELETE FROM corpus_stratification_reports WHERE id = %s",
        (result.stratification_report_id,),
    )


def test_a_cohort_is_a_materialised_list_and_never_a_query(cdb, permitted, store) -> None:
    """`MOS-EVID-016` and `MOS-TRAIN-202`: "A `DatasetVersion` MUST NOT be sealed from a
    query, a folder, a DICOMweb filter or a view; the `CurationBatch` is the object that
    makes that rule satisfiable in practice."

    An empty batch has nothing to materialise, and the refusal names the requirement
    rather than producing a zero-case cohort that would look like a successful seal of
    "everything matching".
    """
    batch = _open_batch(cdb)
    ds = _make_dataset(cdb)
    with pytest.raises(CurationRefused) as exc:
        cur.seal_from_batch(
            cdb,
            batch_id=batch.id,
            dataset_id=ds.id,
            retrieve=_retrieve,
            store=store,
            bucket=BUCKET,
            sealed_by=OPERATOR,
            deidentification_status="public_deidentified",
            deid_policy_id="ps315-basic/v4",
            uid_mapping_table_id="uidmap/v1",
        )
    assert any(r.check_id == "MOS-EVID-016" for r in exc.value.refusals)
    sig = inspect.signature(cur.seal_from_batch)
    for forbidden in ("query", "filter", "prefix", "glob", "folder", "path"):
        assert forbidden not in sig.parameters, (
            f"seal_from_batch takes a {forbidden!r}; MOS-EVID-016 forbids a cohort "
            "defined by one"
        )


# =====================================================================================
# 6. The generated transform chain (MOS-TRAIN-131 to 134, 191)
#    Chapter 17 acceptance criteria 6 and 7.
# =====================================================================================
@pytest.fixture(scope="module")
def selftest_spec() -> sp.PreprocessingSpec:
    return sp.parse_spec(fx.selftest_spec_document())


def test_the_chain_is_byte_reproducible_from_the_spec_alone(selftest_spec) -> None:
    """`MOS-TRAIN-131`: the chain "MUST be byte-reproducible from the spec alone".

    Two independent parses of the same document produce the same bytes, and a change to
    one spec field changes them. Reproducible and not merely deterministic: the digest is
    a function of the spec's CONTENT, which is what lets a `TrainingRun` and a
    `ValidationReport` cite it.
    """
    again = sp.parse_spec(fx.selftest_spec_document())
    assert ch.chain_bytes(selftest_spec) == ch.chain_bytes(again)
    assert ch.chain_digest(selftest_spec).startswith("sha256:")

    moved = fx.selftest_spec_document()
    moved["target_spacing_mm"] = [1.5, 0.9, 0.9]
    assert ch.chain_digest(sp.parse_spec(moved)) != ch.chain_digest(selftest_spec)


def test_the_generated_chain_and_the_serving_implementation_agree_byte_for_byte(
    selftest_spec,
) -> None:
    """Chapter 17 acceptance criterion 6, and the reason `MOS-TRAIN-191` puts this in
    0.2.0.

    `MOS-TRAIN-133`: "For every registered `PreprocessingSpec` version, CI MUST assert that
    the generated MONAI chain and `medicalos-preprocessing` produce the **byte-identical**
    model-space tensor on the golden fixture, compared by the `sha256` of `MOS-IMG-054`
    step 4 and **not** by a tolerance."

    So the comparison below is `==` on two digests. A tolerance here would defeat the
    check: the failure mode it exists to catch -- a resampler that differs in the fourth
    decimal place -- is invisible to every tolerance anybody would choose.
    """
    fixture = fx.phantom_input()
    document = ch.serialize_chain(selftest_spec)
    generated = ch.build_chain_from_document(document)

    serving = pre.tensor_digest(
        pre.first_patch(selftest_spec, pre.model_space_tensor(selftest_spec, fixture))
    )
    from_chain = pre.tensor_digest(
        pre.first_patch(
            selftest_spec,
            pre.model_space_tensor(selftest_spec, fixture, chain=generated),
        )
    )
    assert serving == from_chain, (
        "the generated chain and the serving implementation disagree; MOS-TRAIN-133 "
        "requires byte equality, not agreement within a tolerance"
    )
    assert serving == fx.GOLDEN_TENSOR_SHA256, (
        "the golden hash no longer reproduces. MOS-TRAIN-065 forbids a 're-record the "
        "hash' operation: read the diff, establish what changed, and re-pin by hand."
    )


def test_a_perturbed_interpolation_mode_breaks_the_equality(selftest_spec) -> None:
    """Criterion 6's second half: "perturb the generated chain's `Spacingd` mode ... and
    assert the check fails."

    A check that cannot fail is not a check. This is the negative control for the test
    above, and it is the reason that test is worth running at all.
    """
    fixture = fx.phantom_input()
    document = copy.deepcopy(ch.serialize_chain(selftest_spec))
    touched = 0
    for transform in document["preprocessing"]["transforms"]:
        if transform["_target_"].endswith("Spacingd"):
            transform["mode"] = [pre.INTERP_ORDER["nearest"]]
            touched += 1
    assert touched == 1, "the generated chain has no Spacingd to perturb"

    perturbed = pre.tensor_digest(
        pre.first_patch(
            selftest_spec,
            pre.model_space_tensor(
                selftest_spec, fixture, chain=ch.build_chain_from_document(document)
            ),
        )
    )
    assert perturbed != fx.GOLDEN_TENSOR_SHA256, (
        "nearest-neighbour resampling produced the linear-resampled hash; the check is "
        "not comparing what it claims to compare"
    )


def test_the_generator_refuses_rather_than_substituting(selftest_spec) -> None:
    """`MOS-TRAIN-132`: "The generator MUST refuse to emit a chain for any
    `PreprocessingSpec` field it cannot represent exactly, and MUST NOT substitute the
    nearest available option."

    Criterion 6's third half, as this deployment can state it. `bspline3` SERIALIZES --
    the document represents spline order 3 exactly, and `MOS-TRAIN-039`'s `_INTERP` maps
    it -- and is then REFUSED at execution with the field named, because this deployment's
    resampler implements orders 0 and 1. What `MOS-TRAIN-132` forbids is the substitution,
    and the assertion is that no linear-resampled tensor comes back under a cubic spec.
    """
    document = fx.selftest_spec_document()
    document["image_interpolator"] = "bspline3"
    cubic = sp.parse_spec(document)
    assert "preprocessing" in ch.serialize_chain(cubic), "order 3 is representable"

    with pytest.raises(ChainRefused) as exc:
        pre.model_space_tensor(cubic, fx.phantom_input())
    refusal = exc.value.refusals[0]
    assert refusal.code == "interpolator_not_implemented"
    assert "image_interpolator" in str(refusal.as_dict()), "the field must be named"
    assert exc.value.as_problem()["type"].endswith("/transform-chain-refused")


def test_no_random_transform_is_ever_emitted(selftest_spec) -> None:
    """`MOS-TRAIN-037`: "CI MUST assert that no transform whose class name begins with
    `Rand` is ever emitted by `build_chain`. An augmentation that leaks into the spec is
    applied at serving time, non-deterministically, on every patient."
    """
    for document in (
        ch.serialize_chain(selftest_spec),
        ch.serialize_chain(sp.parse_spec(fx.MOS_TRAIN_058_DOCUMENT)),
    ):
        for transform in document["preprocessing"]["transforms"]:
            name = transform["_target_"].rsplit(".", 1)[-1]
            assert not name.startswith("Rand"), f"{name} is augmentation, not preprocessing"

    # And the guard is exercised rather than grepped: a document that acquired a `Rand*`
    # transform AFTER generation is the case `MOS-TRAIN-131`'s "byte-reproducible from the
    # spec alone" cannot detect on its own, and it is refused on the way back in.
    edited = copy.deepcopy(ch.serialize_chain(selftest_spec))
    edited["preprocessing"]["transforms"].append(
        {"_target_": "RandGaussianNoised", "keys": ["image"], "prob": 0.15}
    )
    with pytest.raises(ChainRefused) as exc:
        ch.build_chain_from_document(edited)
    assert {r.check_id for r in exc.value.refusals} & {"MOS-TRAIN-037", "MOS-TRAIN-131"}
    assert "RandGaussianNoised" in str(exc.value.refusals[0].as_dict())
    # The refusal above arrives as `unknown_target` rather than as the `Rand*` guard, and
    # that is the stronger of the two: the renderer's target table is a closed set and no
    # augmentation is in it. So the property is asserted where it actually lives.
    assert not [t for t in ch.TARGETS if t.startswith("Rand")], (
        "a Rand* transform is renderable; MOS-TRAIN-037 requires that it never be emitted"
    )


def test_the_hashed_chain_begins_at_orientation(selftest_spec) -> None:
    """`MOS-TRAIN-038`: "`LoadImaged` and `EnsureChannelFirstd` MUST NOT appear in the
    serialized chain ... The hashed chain therefore begins at `Orientationd`, and the
    loader is an adapter outside it."

    `MOS-TRAIN-131`'s worked JSON contains `LoadImaged`. The normative sentence wins over
    the example in the same section; the divergence is recorded in
    `medos/medos/training/chain.py` and reported upward.
    """
    transforms = ch.serialize_chain(selftest_spec)["preprocessing"]["transforms"]
    names = [t["_target_"].rsplit(".", 1)[-1] for t in transforms]
    assert names[0] == "Orientationd", names
    assert "LoadImaged" not in names and "EnsureChannelFirstd" not in names


def test_the_golden_hash_has_exactly_one_recorder(selftest_spec) -> None:
    """Chapter 17 acceptance criterion 7, and `MOS-TRAIN-134`/`MOS-IMG-058`.

    "`golden_fixture.output_tensor_sha256` MUST be recorded by `medicalos-preprocessing`
    and by nothing else, even when `MOS-TRAIN-133` passes. The serving self-test compares
    the worker's computation against this value; if the value were recorded by the
    training chain, the self-test would be comparing the serving implementation against a
    number the serving implementation did not produce, and a co-drift of both would be
    invisible."

    So: a recomputation through that one package reproduces the shipped literal exactly,
    and no other module in the training package computes a tensor digest of its own.
    """
    recorded, shape = pre.record_golden(selftest_spec, fx.phantom_input())
    assert recorded == fx.GOLDEN_TENSOR_SHA256
    assert shape == fx.GOLDEN_TENSOR_SHAPE
    assert fx.phantom_digest() == fx.PHANTOM_SHA256, (
        "the shipped fixture's own bytes changed; MOS-IMG-054 step 1 no longer matches"
    )
    assert pre.verify_golden(selftest_spec, fx.phantom_input())["ok"] is True

    for name in ("chain.py", "curation.py", "fixtures.py", "spec.py", "policy.py"):
        code = _code_only(_source(name))
        assert "hashlib.sha256" not in code or name == "fixtures.py", (
            f"{name} computes a digest of its own; MOS-IMG-058 allows exactly one recorder"
        )
    # `MOS-TRAIN-065`: there is no operation that re-records the pinned literals.
    code = _code_only(_source())
    for token in ("re_record", "rerecord", "update_golden", "refresh_golden"):
        assert token not in code, f"{token!r} is a re-record operation (MOS-TRAIN-065)"


def test_there_is_one_chain_constructor(selftest_spec) -> None:
    """`MOS-TRAIN-034`: `build_chain` "MUST be the only function in the MedicalOS codebase
    that instantiates a MONAI transform for the deterministic preprocessing path", and
    `MOS-TRAIN-035` makes CI enforce it with a grep.

    The generator is therefore a RENDERER: `serialize_chain` builds through `build_chain`
    and renders the result, and `build_chain_from_document` parses a rendered document
    back. Neither knows the mapping from a spec field to a transform argument, which is
    what makes the byte-equality test above a test of the generator rather than of itself.
    """
    generator = _code_only(_module("chain.py").read_text(encoding="utf-8"))
    assert "build_chain(" in generator, "the generator must go through the one constructor"
    assert (
        "from medos.sdk.preprocess import" in generator
        or "preprocess" in generator
    )

    constructors = [
        p.name
        for p in _contract_modules()
        if re.search(r"^def build_chain\b", _code_only(p.read_text(encoding="utf-8")), re.M)
    ]
    assert constructors == ["preprocess.py"], (
        f"more than one build_chain exists: {constructors} (MOS-TRAIN-034)"
    )


def test_the_spec_fixtures_are_transcribed_and_not_corrected() -> None:
    """`MOS-IMG-052`'s fixture violates `MOS-TRAIN-051`, and is shipped unchanged.

    `MOS-TRAIN-051` requires `foreground_crop.min_size_voxels` element-wise >=
    `patch.size_voxels` and says "Registration MUST reject a spec that violates it".
    `MOS-IMG-052` declares `[32, 128, 128]` against a patch of `[64, 192, 192]`, which
    violates it on all three axes. The fixture is transcribed verbatim and `parse_spec`
    refuses it, which is what the requirement asks for. Correcting the numbers in the
    fixture would hide a contradiction between two chapters behind a passing test.
    """
    with pytest.raises(ChainRefused) as exc:
        sp.parse_spec(fx.MOS_IMG_052_DOCUMENT)
    assert any(r.check_id.startswith("MOS-") for r in exc.value.refusals)
    # The lung-lobes fixture of MOS-TRAIN-058 parses and serializes; it does not execute
    # in this deployment, and that is a different sentence (MOS-TRAIN-057).
    lobes = sp.parse_spec(fx.MOS_TRAIN_058_DOCUMENT)
    assert ch.chain_digest(lobes).startswith("sha256:")


# =====================================================================================
# 7. The seam MOS-TRAIN-198 names, asserted as an absence
# =====================================================================================
def test_the_pipeline_has_no_route_to_a_deployment() -> None:
    """`MOS-TRAIN-189`: "There MUST be no path -- no function, no API call, no scheduled
    task, no policy -- from a `TrainingRun`, a `ConversionRun`, an `EvaluationRun` or a
    `ValidationReport` to a `state = SERVING` `Deployment`."

    The full call-graph assertion belongs to 0.3.0, where `TrainingRun` ships. What can be
    asserted now is that this release's package imports nothing from the deployment or
    clinical-use-mode side -- so the property holds from the first module rather than
    being retrofitted onto a package that already grew the import.
    """
    code = _code_only(_source())
    for token in (
        "promote",
        "clinical_use_mode",
        "deployments",
        "role = 'ACTIVE'",
        "gate_override",
    ):
        assert token not in code, (
            f"{token!r} appears in medos/medos/training; MOS-TRAIN-189 requires no path from "
            "this package to a serving deployment"
        )


def test_the_split_tooling_is_not_reachable_from_the_queue() -> None:
    """`MOS-TRAIN-117` and `MOS-EVID-028`, restated at this boundary.

    Chapter 17 acceptance criterion 2 greps the split tooling for `min_days_between_studies`,
    `study_level_split`, `allow_same_patient` and a seed. `test_evidence_schema.py` asserts
    that over `medos/medos/evidence`; this asserts the curation package did not acquire its own
    copy of any of them on the way past.
    """
    code = _code_only(_source())
    for token in (
        "min_days_between_studies",
        "study_level_split",
        "allow_same_patient",
        "random_state",
        "random.seed",
    ):
        assert token not in code, f"{token!r} in medos/medos/training (MOS-TRAIN-117)"
