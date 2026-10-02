# SPDX-License-Identifier: Apache-2.0
"""`EvaluationRun` and per-case metrics: migration 0007, the two pinned conventions.

docs/spec/15-delivery.md section 15.2.5 (tag 0.2.0) and chapter 7 sections 7.6 and 7.7.

WHAT THIS FILE IS TRYING TO CATCH, in the order the release brief names them:

  1. AGGREGATES WITHOUT PER-CASE ROWS. "per-case metrics persisted, not only aggregates"
     is a named check of the 0.2.0 gate because a mean hides the stratum that collapsed,
     and because the deployment gate of section 7.9.2 is a PAIRED test that cannot run at
     all without the rows. So the tests here assert the rows exist, that the aggregate is
     recomputable from them (MOS-EVID-066), that a run cannot succeed without them, and
     that nothing can be appended to them afterwards.
  2. TWO EVALUATION IMPLEMENTATIONS. The failure is silent by construction: the
     development number and the gate number differ by less than anyone would notice and
     more than the gate is being asked to rule on, and the two are never printed side by
     side. `test_one_evaluation_implementation_not_two` greps for a second one.
  3. THE TWO CONVENTIONS QUIETLY DIFFERING BETWEEN RUNS. Mean-of-per-case versus pooled
     Dice (MOS-EVID-047) and the empty-ground-truth policy (MOS-EVID-051) are
     irreversible once a number is published. Both are asserted here as DATABASE
     behaviour, not as library behaviour, because a convention enforced only in the
     library is one forgotten call path away from nothing.
  4. A BINDING THAT CAN BE EDITED AFTER THE FACT. MOS-EVID-061 binds every input that can
     change a number; if the row can be edited the binding is decorative.

SKIP DISCIPLINE: `tests/_support/skips.py` only. There is no raw `pytest.skip` here, and
the module declares `postgres` through `tests/_support/stack.py`'s `tests/integration`
prefix, so `--require-stack` probes it.

RUNTIME. Every fixture cohort is small and the bootstrap replicate count is lowered to
`_FAST_B` wherever the test is about something other than the interval, because B = 2000
per aggregate over a dozen aggregates is a minute of pure Python per test and buys
nothing. `test_confidence_intervals_are_reproducible_and_clustered` uses the real default.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db.conn import apply_schema
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import aggregate as ev_agg
from medos.evidence import ci as ev_ci
from medos.evidence import digest as ev_digest
from medos.evidence import evaluation as ev_run
from medos.evidence import metrics as ev_metrics
from medos.evidence import repo as ev_repo
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.store import InMemoryManifestStore
from medos.sdk.refusal import EvaluationRefused
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

BUCKET = "medos-evidence"
SALT = b"test-tenant-salt-not-a-real-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"
COMMIT = "b" * 40
IMAGE = "sha256:" + "c" * 64
MODEL_VERSION = "22222222-2222-2222-2222-222222222222"
SERVICE_VERSION = "33333333-3333-3333-3333-333333333333"

# MOS-EVID-058's default is B = 2000. Lowered everywhere the test is about something
# other than the interval; the reproducibility test uses the real one.
_FAST_B = 120

EVALUATION_TABLES = (
    "evaluation_runs",
    "evaluation_case_metrics",
    "evaluation_case_scores",
)
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


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture()
def eval_db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One connection with the default tenant bound and the eleven tables emptied.

    TRUNCATE and not DELETE, for the reason 0006's suite already established: every table
    here carries a BEFORE DELETE guard, and TRUNCATE fires no row-level triggers -- so
    the seal holds for application code while the harness can still start clean.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(f"TRUNCATE {', '.join(EVALUATION_TABLES + EVIDENCE_TABLES)} CASCADE")
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
# Cohort builders.  A cohort that passes the seal-time stratification check, because
# this file is not about that check and a cohort that trips it would fail every test for
# the wrong reason.
# =====================================================================================
def _uid(*parts: int) -> str:
    return "1.2.826.0.1.3680043.10.7." + ".".join(str(p) for p in parts)


def _pixel_digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _record(patient: int) -> SeriesRecord:
    half = patient % 2
    return SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", f"P{patient:04d}"),
        study_instance_uid=_uid(patient, 1),
        series_instance_uid=_uid(patient, 1, 1),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(patient, 1, 1, i) for i in range(40)),
        series_pixel_digest=_pixel_digest(f"{patient}/1/1"),
        acquisition=Acquisition(
            slice_thickness_mm=1.25 if half else 2.5,
            pixel_spacing_mm=(0.703125, 0.703125),
            convolution_kernel="B30f",
            convolution_kernel_class="soft" if half else "standard",
            manufacturer="SIEMENS" if half else "GE MEDICAL SYSTEMS",
            manufacturer_model_name="Sensation 16" if half else "Discovery CT750 HD",
            kvp=120.0,
            contrast_phase="non_contrast",
            z_coverage_mm=332.5,
            image_type=("ORIGINAL", "PRIMARY", "AXIAL"),
            patient_age_years=60 + patient,
            patient_sex="M" if patient % 2 else "F",
            body_part_examined="CHEST",
        ),
        institution_key=ev_digest.institution_key(SALT, f"SITE-{half}"),
        study_year=2022 + half,
        accession_number_hash=ev_digest.accession_number_hash(SALT, f"ACC{patient:04d}"),
    )


class Cohort:
    """A sealed cohort with a frozen split and a frozen reference standard."""

    def __init__(
        self,
        version_id: str,
        split_id: str,
        annotation_set_id: str,
        records: list[SeriesRecord],
        test_records: list[SeriesRecord],
        store: InMemoryManifestStore,
    ) -> None:
        self.version_id = version_id
        self.split_id = split_id
        self.annotation_set_id = annotation_set_id
        self.records = records
        self.test_records = test_records
        # MOS-EVID-046's coverage check reads the annotation MANIFEST, because
        # `freeze_annotation_set` does not (yet) materialise the `annotations` rows --
        # see `medos.evidence.evaluation._annotated_patients`. The store is therefore
        # part of the cohort as far as a run is concerned.
        self.store = store


def _prepare(
    conn: psycopg.Connection[Any],
    store: InMemoryManifestStore,
    *,
    n_patients: int = 10,
    purpose: str = "evaluation",
    offset: int = 0,
) -> Cohort:
    """Seal a cohort, freeze a split and freeze a reference standard over it.

    `offset` shifts the patient numbering. It is not cosmetic: sealing is idempotent on
    CONTENT (MOS-TRAIN-209), so a second `_prepare` with the same records returns the
    FIRST version's row under a new `datasets` row, and the second split then collides on
    `(dataset_version_id, name)`. A test that needs two genuinely different cohorts has
    to give them different cases, which is the seal behaving exactly as specified.
    """
    records = [_record(offset + p) for p in range(n_patients)]
    dataset = ev_repo.create_dataset(
        conn,
        slug=f"cohort-{secrets.token_hex(4)}",
        display_name="evaluation cohort",
        purpose=purpose,
        custodian="TCIA/TEST",
        created_by=OPERATOR,
    )
    sealed = ev_repo.seal_dataset_version(
        conn,
        dataset_id=dataset.id,
        records=records,
        store=store,
        bucket=BUCKET,
        sealed_by=OPERATOR,
        deidentification_status="public_deidentified",
        deid_policy_id="ps315-basic/v4",
        uid_mapping_table_id="uidmap/v1",
    )
    # Patients alternate tune/test so that an operating threshold has a partition to be
    # selected on that is not the one the run reports (MOS-EVID-033).
    keys = sorted({r.patient_key for r in records})
    assignments = [(k, "test" if i % 2 else "tune") for i, k in enumerate(keys)]
    test_keys = {k for k, part in assignments if part == "test"}
    split = ev_repo.freeze_split(
        conn,
        dataset_version_id=sealed.id,
        name="holdout",
        assignments=assignments,
        store=store,
        bucket=BUCKET,
        frozen_by=OPERATOR,
        assignment_method="alternating by sorted patient_key, fixed and materialised",
        records=records,
    )
    annotations = ev_repo.freeze_annotation_set(
        conn,
        dataset_version_id=sealed.id,
        name="consensus-3",
        capability_id="pleural_effusion",
        label_definition_id="00000000-0000-0000-0000-0000000000aa",
        annotation_type="mask",
        consensus_rule="majority_at_least_2",
        consensus_params={"min_agreeing": 2},
        readers=[
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
            for i in range(3)
        ],
        entries=[
            {
                "patient_key": r.patient_key,
                "study_instance_uid": r.study_instance_uid,
                "series_instance_uid": r.series_instance_uid,
                "reference": {
                    "kind": "mask",
                    "uri": "s3://x/ref.nrrd",
                    "digest": _pixel_digest(f"ref/{r.series_instance_uid}"),
                    "geometry": "source",
                    "reference_volume_ml": 286.4,
                },
                "per_reader": [
                    {"reader_id": f"rdr_a{i}", "uri": f"s3://x/a{i}.nrrd",
                     "digest": _pixel_digest(f"a{i}/{r.series_instance_uid}"),
                     "volume_ml": 284.0 + i}
                    for i in range(3)
                ],
                "inter_reader": {"agreement_dice_mean_per_case": 0.938,
                                 "volume_difference_ml": 9.1},
            }
            for r in records
        ],
        store=store,
        bucket=BUCKET,
        frozen_by=OPERATOR,
        reference_of_record=True,
    )
    return Cohort(
        version_id=sealed.id,
        split_id=split.id,
        annotation_set_id=annotations.id,
        records=records,
        test_records=[r for r in records if r.patient_key in test_keys],
        store=store,
    )


def _observation(
    record: SeriesRecord,
    *,
    gt_voxels: int,
    pred_voxels: int,
    intersection_voxels: int,
    voxel_ml: float = 0.001,
    case_score: float | None = None,
    case_label: int | None = None,
) -> ev_metrics.CaseObservation:
    return ev_metrics.CaseObservation(
        case_key=record.case_key,
        patient_key=record.patient_key,
        study_instance_uid=record.study_instance_uid,
        series_instance_uid=record.series_instance_uid,
        gt_voxels=gt_voxels,
        pred_voxels=pred_voxels,
        intersection_voxels=intersection_voxels,
        gt_volume_ml=gt_voxels * voxel_ml,
        pred_volume_ml=pred_voxels * voxel_ml,
        # MOS-EVID-067's minimum, plus the capability's declared clinical stratum.
        strata={
            "slice_thickness_mm": record.acquisition.slice_thickness_mm,
            "pixel_spacing_mm_max": max(record.acquisition.pixel_spacing_mm),
            "convolution_kernel_class": record.acquisition.convolution_kernel_class,
            "manufacturer": record.acquisition.manufacturer,
            "contrast_phase": record.acquisition.contrast_phase,
            "patient_age_years": record.acquisition.patient_age_years,
            "patient_sex": record.acquisition.patient_sex,
            "reference_volume_ml": gt_voxels * voxel_ml,
        },
        case_score=case_score,
        case_label=case_label,
    )


def _good_observations(cohort: Cohort) -> list[ev_metrics.CaseObservation]:
    """A plausible segmentation run: every case has a finding and the model finds it."""
    out = []
    for i, record in enumerate(sorted(cohort.test_records, key=lambda r: r.case_key)):
        gt = 100_000 + 10_000 * i
        pred = gt + 4_000
        out.append(
            _observation(
                record,
                gt_voxels=gt,
                pred_voxels=pred,
                intersection_voxels=int(gt * 0.9),
                case_score=0.80 + 0.01 * i,
                case_label=1,
            )
        )
    return out


def _conventions(b: int = _FAST_B) -> dict[str, Any]:
    return ev_metrics.metric_conventions(fp_volume_threshold_ml=10.0, bootstrap_b=b)


def _create(
    conn: psycopg.Connection[Any], cohort: Cohort, **overrides: Any
) -> ev_run.RunRow:
    kwargs: dict[str, Any] = {
        "kind": "vendor_evidence",
        "capability_id": "pleural_effusion",
        "dataset_version_id": cohort.version_id,
        "split_id": cohort.split_id,
        "partition": "test",
        "annotation_set_id": cohort.annotation_set_id,
        "model_version_id": MODEL_VERSION,
        "preprocessing_spec_id": "ct-chest-1mm",
        "preprocessing_spec_version": 3,
        "preprocessing_spec_digest": "sha256:" + "d" * 64,
        "code_commit": COMMIT,
        "image_digest": IMAGE,
        "runner": "ci@build-42",
        "fp_volume_threshold_ml": 10.0,
        "inference_backend": {"kind": "triton", "version": "25.03"},
        "accelerator": {"gpu_model": "NVIDIA A10", "driver": "550.90.07",
                        "cuda": "12.4", "trt": "10.0.1"},
        "operating_thresholds": {
            "effusion_probability": {"value": 0.50, "selected_on": "tune"}
        },
        "conventions": _conventions(),
    }
    kwargs.update(overrides)
    return ev_run.create_run(conn, **kwargs)


def _measure(
    conn: psycopg.Connection[Any],
    cohort: Cohort,
    observations: list[ev_metrics.CaseObservation],
    **overrides: Any,
) -> tuple[ev_run.RunRow, ev_run.FinishResult]:
    subgroups = overrides.pop("subgroups", ())
    run = _create(conn, cohort, **overrides)
    ev_run.start_run(conn, run_id=run.id)
    ev_run.record_cases(conn, run_id=run.id, observations=observations)
    return run, ev_run.finish_run(
        conn, run_id=run.id, subgroups=subgroups, annotation_store=cohort.store
    )


def _refuses(conn, statement: str, params: tuple = (), *codes: str) -> Any:
    """Execute and require the refusal to come from the named SQLSTATEs.

    `MOS05` is `forbid_evidence_mutation`, `MOS06` is `forbid_column_change` and
    `forbid_evaluation_rewrite`, `23514` is a CHECK and `23505` a unique violation. All
    are asserted by CODE and not by exception class: psycopg maps the implementation-
    defined classes to the generic `DatabaseError`, so asserting on the class would pass
    on a typo'd column name, which is not the claim being made.
    """
    expected = codes or ("MOS05", "MOS06")
    with pytest.raises(psycopg.DatabaseError) as exc:
        conn.execute(statement, params)
    assert exc.value.sqlstate in expected, (
        f"expected {expected}, got SQLSTATE {exc.value.sqlstate}: {exc.value}"
    )
    conn.rollback()
    return exc.value


def _entry(aggregates: list[dict[str, Any]], metric: str, stratum: str = "all") -> dict:
    for e in aggregates:
        if e["metric"] == metric and e["stratum"] == stratum:
            return e
    raise AssertionError(
        f"no aggregate {metric!r} on stratum {stratum!r}; present: "
        f"{sorted((e['metric'], e['stratum']) for e in aggregates)}"
    )


# =====================================================================================
# 1. The migration applies from EMPTY and carries the constraints it claims
# =====================================================================================
def _swap_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"


def test_evaluation_migration_applies_from_empty(pg_dsn: str) -> None:
    """0001..0007 from an empty database, then the structural invariants.

    Applied to a BRAND NEW database rather than to the session one: the previous release
    block's defect was a migration set that no longer applied from empty, which a suite
    running against an already-migrated database cannot see.
    """
    name = f"medos_eval_{secrets.token_hex(6)}"
    admin_url = _swap_dbname(pg_dsn, "postgres")
    with psycopg.connect(admin_url, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _swap_dbname(pg_dsn, name)
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            apply_schema(conn)

            applied = [
                r["version"]
                for r in conn.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
            assert "0007_evaluation" in applied, applied

            # MOS-EVID-012 / MOS-STORE-229: forced row security on every one of them.
            for table in EVALUATION_TABLES:
                row = dict(
                    conn.execute(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = %s",
                        (table,),
                    ).fetchone()
                )
                assert row["relrowsecurity"] and row["relforcerowsecurity"], table

            # MOS-STORE-228: no table-level UPDATE or DELETE on the per-case tables for
            # the application role, and no DELETE anywhere in this migration.
            grants = {
                (r["table_name"], r["privilege_type"])
                for r in conn.execute(
                    "SELECT table_name, privilege_type FROM "
                    "information_schema.role_table_grants "
                    "WHERE grantee = 'medicalos_app' AND table_name = ANY(%s)",
                    (list(EVALUATION_TABLES),),
                ).fetchall()
            }
            for table in EVALUATION_TABLES:
                assert (table, "DELETE") not in grants, f"DELETE granted on {table}"
            assert ("evaluation_case_metrics", "UPDATE") not in grants
            assert ("evaluation_case_scores", "UPDATE") not in grants
            # ... but the run's state machine needs its column-level grant.
            columns = {
                r["column_name"]
                for r in conn.execute(
                    "SELECT column_name FROM information_schema.column_privileges "
                    "WHERE grantee = 'medicalos_app' AND table_name = "
                    "'evaluation_runs' AND privilege_type = 'UPDATE'"
                ).fetchall()
            }
            assert columns == {
                "state", "invalidation_reason", "failure_reason", "started_at",
                "finished_at", "aggregate_metrics", "run_digest", "per_case_bucket",
                "per_case_object_key",
            }, sorted(columns)
            assert "dataset_version_digest" not in columns

            # MOS-EVID-061: the binding index has to coalesce the subject columns or it
            # matches nothing, ever. The migration asserts this too; asserted again here
            # because the migration's own assertion is the thing a careless edit removes.
            index = dict(
                conn.execute(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = "
                    "'evaluation_runs_binding_uk'"
                ).fetchone()
            )["indexdef"]
            assert "COALESCE(service_version_id, model_version_id)" in index
            assert "state = 'SUCCEEDED'" in index

            # MOS-EVID-014 owns the state enum; the column is `state`, never `status`.
            check = dict(
                conn.execute(
                    "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
                    "WHERE conname = 'evaluation_runs_state_check'"
                ).fetchone()
            )["def"]
            for state in ev_run.RUN_STATES:
                assert f"'{state}'" in check, state
            assert not conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name = "
                "'evaluation_runs' AND column_name = 'status'"
            ).fetchall()
    finally:
        with psycopg.connect(admin_url, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_down_migration_is_the_reverse_of_the_up() -> None:
    """Every table, policy, trigger and added constraint 0007 creates is dropped.

    Textual, not executed: the runner never runs a down-migration. A down-migration that
    forgets a table is discovered when someone tries to test the forward migration and
    the second attempt collides, which is exactly when it is most expensive.
    """
    # parents[3], not [2]. `ev_repo` is `medos/medos/evidence/repo.py`; two levels up is
    # the PRODUCT directory `medos/`, and the literal below already carries `medos/medos/`.
    root = Path(ev_repo.__file__).resolve().parents[3]
    up = (root / "medos/medos/db/migrations/0007_evaluation.up.sql").read_text("utf-8")
    down = (root / "medos/medos/db/migrations/0007_evaluation.down.sql").read_text("utf-8")
    for table in EVALUATION_TABLES:
        assert f"CREATE TABLE {table} (" in up
        assert f"DROP TABLE IF EXISTS {table};" in down
        assert f"{table}_tenant_isolation" in down
    for function in (
        "forbid_evaluation_rewrite",
        "forbid_case_rows_after_finish",
        "cascade_dataset_version_defect",
    ):
        assert f"CREATE FUNCTION {function}()" in up
        assert f"DROP FUNCTION IF EXISTS {function}();" in down
    # The two constraints 0007 adds to 0006's tables, and only those two.
    for constraint in ("dataset_splits_version_id_uk", "annotation_sets_version_id_uk"):
        assert f"ADD CONSTRAINT {constraint}" in up
        assert f"DROP CONSTRAINT IF EXISTS {constraint}" in down


# =====================================================================================
# 2. PER-CASE METRICS PERSISTED, NOT ONLY AGGREGATES.  The 0.2.0 gate's named check.
# =====================================================================================
def test_per_case_rows_are_persisted_not_only_aggregates(eval_db, store) -> None:
    """MOS-EVID-065, and the `per-case-metrics` row of the 15.1.2 gate.

    One row per (case, metric), every one of them carrying the voxel counts MOS-EVID-068
    requires even where the value is defined, and the strata MOS-EVID-067 requires --
    "a stratum that is not persisted cannot be gated on".
    """
    cohort = _prepare(eval_db, store)
    observations = _good_observations(cohort)
    run, result = _measure(eval_db, cohort, observations)

    rows = eval_db.execute(
        "SELECT case_key, metric, value, eligible, gt_voxels, pred_voxels, "
        "intersection_voxels, strata FROM evaluation_case_metrics "
        "WHERE evaluation_run_id = %s ORDER BY case_key, metric",
        (run.id,),
    ).fetchall()
    expected = len(observations) * len(ev_metrics.SEGMENTATION_PER_CASE_METRICS)
    assert len(rows) == expected
    assert {r["metric"] for r in rows} == set(ev_metrics.SEGMENTATION_PER_CASE_METRICS)
    assert all(r["gt_voxels"] > 0 and r["intersection_voxels"] > 0 for r in rows)
    for required in ("slice_thickness_mm", "convolution_kernel_class", "manufacturer",
                     "contrast_phase", "patient_age_years", "patient_sex",
                     "reference_volume_ml"):
        assert all(required in r["strata"] for r in rows), required

    # MOS-STORE-299: one score row per case, pre-threshold, with the reference label.
    scores = eval_db.execute(
        "SELECT case_key, case_score, case_label, candidates FROM "
        "evaluation_case_scores WHERE evaluation_run_id = %s",
        (run.id,),
    ).fetchall()
    assert len(scores) == len(observations)
    assert all(s["case_score"] is not None and s["case_label"] == 1 for s in scores)

    # And the aggregate block exists BESIDE them, not instead of them.
    assert result.run_digest.startswith("sha256:")
    stored = ev_run.get_run(eval_db, run.id)
    assert stored.state == "SUCCEEDED"
    assert isinstance(stored.aggregate_metrics, list)


def test_a_run_cannot_succeed_without_per_case_rows(eval_db, store) -> None:
    """MOS-EVID-065: "Aggregates alone MUST NOT be stored without them."."""
    cohort = _prepare(eval_db, store)
    run = _create(eval_db, cohort)
    ev_run.start_run(eval_db, run_id=run.id)

    with pytest.raises(EvaluationRefused) as exc:
        ev_run.finish_run(eval_db, run_id=run.id)
    assert "MOS-EVID-065" in exc.value.check_ids
    assert ev_run.get_run(eval_db, run.id).state == "RUNNING"

    # The database refuses the shortcut too: a SUCCEEDED row MUST carry an aggregate
    # block and a digest, so "mark it done and fill the numbers in later" is unwritable.
    eval_db.rollback()
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET state = 'SUCCEEDED', finished_at = now() "
        "WHERE id = %s",
        (run.id,),
        "23514",
    )


def test_per_case_rows_are_closed_once_the_run_is_finished(eval_db, store) -> None:
    """The hole MOS-EVID-066 leaves open, closed.

    Both tables are append-only, so appending is exactly what they permit -- and a case
    row appended to a finished run makes the aggregate describe a strictly smaller case
    set than the rows, with no edit anywhere and nothing to detect it.
    """
    cohort = _prepare(eval_db, store)
    observations = _good_observations(cohort)
    run, _result = _measure(eval_db, cohort, observations)

    extra = cohort.test_records[0]
    _refuses(
        eval_db,
        """
        INSERT INTO evaluation_case_metrics (
          tenant_id, evaluation_run_id, case_key, patient_key, study_instance_uid,
          series_instance_uid, metric, value, eligible, gt_voxels, pred_voxels,
          intersection_voxels, gt_volume_ml, pred_volume_ml, strata)
        VALUES (current_tenant_id(), %s, 'smuggled', %s, %s, %s, 'dice_mean_per_case',
                0.99, true, 10, 10, 10, 0.01, 0.01, '{}'::jsonb)
        """,
        (run.id, extra.patient_key, extra.study_instance_uid,
         extra.series_instance_uid),
        "MOS05",
    )

    ok, differences = ev_run.verify_aggregates(eval_db, run_id=run.id)
    assert ok, differences


def test_per_case_rows_are_append_only(eval_db, store) -> None:
    """MOS-STORE-228: no UPDATE, no DELETE, on either per-case table.

    Editing one per-case value is the highest-leverage, lowest-visibility way to move a
    published number: one row in a hundred, inside the tolerance of any plausible
    re-measurement, and the aggregate moves.
    """
    cohort = _prepare(eval_db, store)
    run, _result = _measure(eval_db, cohort, _good_observations(cohort))

    _refuses(
        eval_db,
        "UPDATE evaluation_case_metrics SET value = 0.99 WHERE evaluation_run_id = %s",
        (run.id,),
        "MOS05",
    )
    _refuses(
        eval_db,
        "DELETE FROM evaluation_case_metrics WHERE evaluation_run_id = %s",
        (run.id,),
        "MOS05",
    )
    _refuses(
        eval_db,
        "UPDATE evaluation_case_scores SET case_score = 0.01 WHERE "
        "evaluation_run_id = %s",
        (run.id,),
        "MOS05",
    )
    _refuses(
        eval_db, "DELETE FROM evaluation_runs WHERE id = %s", (run.id,), "MOS05"
    )


# =====================================================================================
# 3. CONVENTION 1 -- mean-of-per-case versus pooled.  MOS-EVID-047, MOS-EVID-048.
# =====================================================================================
def test_both_dice_conventions_are_reported_and_they_disagree(eval_db, store) -> None:
    """MOS-EVID-047 and MOS-EVID-048, on the cohort shape that makes them differ.

    The rationale in section 7.6.1 is the test: "On a pleural-effusion cohort whose
    volumes span 10 mL to 2 L, the largest decile of cases contributes more than half the
    denominator, so pooled Dice can stay above 0.90 while the model scores near zero on
    every small effusion."

    So: four small effusions the model misses entirely, one large one it segments almost
    perfectly. Pooled stays high because the large case dominates the denominator;
    mean-of-per-case collapses because each patient counts once. Both numbers are
    persisted, both carry n, n_patients and an interval, and NEITHER is called `dice`.
    """
    cohort = _prepare(eval_db, store)
    records = sorted(cohort.test_records, key=lambda r: r.case_key)
    observations = [
        # four small effusions, missed
        *[
            _observation(r, gt_voxels=8_000, pred_voxels=0, intersection_voxels=0,
                         case_score=0.10, case_label=1)
            for r in records[:4]
        ],
        # one large effusion, found
        _observation(records[4], gt_voxels=2_000_000, pred_voxels=2_000_000,
                     intersection_voxels=1_960_000, case_score=0.99, case_label=1),
    ]
    _run_row, result = _measure(eval_db, cohort, observations)

    mean_of_per_case = _entry(result.aggregates, "dice_mean_per_case")
    pooled = _entry(result.aggregates, "dice_pooled")

    assert mean_of_per_case["value"] == pytest.approx(0.196, abs=1e-3)
    assert pooled["value"] > 0.90
    assert pooled["value"] - mean_of_per_case["value"] > 0.70, (
        "the two conventions must be able to disagree by this much, or the fixture is "
        "not exercising the reason both are required"
    )
    # MOS-EVID-056's four companions, on every aggregate that HAS a value. An entry
    # with n = 0 is the collapsed-stratum report of acceptance check 22 -- it carries no
    # interval because there is nothing to have an interval about, and inventing one
    # would be the fabrication MOS-EVID-056 is written against.
    for entry in result.aggregates:
        assert entry["n"] is not None and entry["n_patients"] is not None
        if entry["value"] is None:
            assert entry["n"] == 0 and entry["note"] == "no_eligible_cases"
            continue
        assert entry["ci_low"] is not None and entry["ci_high"] is not None
    assert mean_of_per_case["n"] == 5 and mean_of_per_case["n_patients"] == 5

    # MOS-EVID-049: no key and no value in the persisted block is the bare word.
    stored = ev_run.get_run(eval_db, _run_row.id)
    assert '"dice"' not in str(stored.aggregate_metrics)


def test_the_database_refuses_an_aggregate_block_naming_bare_dice(eval_db, store) -> None:
    """MOS-EVID-049, enforced where the value is WRITTEN and not only where it is read.

    "A ValidationReport containing a key named `dice` MUST fail schema validation." The
    report is the serialisation of this column, so a block that would fail the report
    schema must not be storable in the first place.
    """
    cohort = _prepare(eval_db, store)
    run = _create(eval_db, cohort)
    ev_run.start_run(eval_db, run_id=run.id)
    eval_db.commit()
    # The one write that finishes a run, carrying the block section 7.6 exists to make
    # unprintable. Everything else about the row is valid, which is the point: the
    # refusal is about the NAME.
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET state = 'SUCCEEDED', finished_at = now(), "
        "run_digest = %s, aggregate_metrics = %s WHERE id = %s",
        (
            "sha256:" + "2" * 64,
            Jsonb([{"metric": "dice_mean_per_case", "stratum": "all", "value": 0.91,
                    "ci_low": 0.88, "ci_high": 0.94, "n": 5, "n_patients": 5,
                    "dice": 0.91}]),
            run.id,
        ),
        "23514",
    )


def test_the_conventions_are_pinned_on_the_row(eval_db, store) -> None:
    """MOS-EVID-047 and MOS-EVID-051: irreversible the moment a number is published.

    A row claiming a different aggregation or a different empty-GT policy is not a
    differently configured run. It is a number that cannot be compared with any other
    number this platform has ever emitted, and the database is where that has to be
    refused, because the library is one forgotten call path away from nothing.
    """
    cohort = _prepare(eval_db, store)
    run = _create(eval_db, cohort)

    # The library will not even build one.
    with pytest.raises(TypeError):
        ev_metrics.metric_conventions(  # type: ignore[call-arg]
            fp_volume_threshold_ml=10.0, empty_gt_policy="score_one"
        )

    conventions = dict(run.metric_conventions)
    conventions["empty_gt_policy"] = "score_one"
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET metric_conventions = %s WHERE id = %s",
        (Jsonb(conventions), run.id),
        "MOS06", "23514",
    )
    conventions = dict(run.metric_conventions)
    conventions["dice_aggregation"] = "pooled"
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET metric_conventions = %s WHERE id = %s",
        (Jsonb(conventions), run.id),
        "MOS06", "23514",
    )
    # And a fresh row cannot be inserted with a different one either.
    assert eval_db.execute(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = 'evaluation_runs_conventions_pinned'"
    ).fetchone()["def"].count("mean_of_per_case") == 1


# =====================================================================================
# 4. CONVENTION 2 -- the empty ground truth.  MOS-EVID-051, MOS-EVID-052.
# =====================================================================================
def test_empty_ground_truth_is_excluded_and_reported_separately(
    eval_db, store
) -> None:
    """Chapter 7 acceptance check 9, both halves, verbatim.

    "Construct a fixture cohort with 10 non-empty and 10 empty ground-truth cases where
    the model predicts empty everywhere. Assert `dice_mean_per_case` is computed over 10
    eligible cases and equals 0.0, that `empty_gt_case_count` is 10, and that
    `empty_gt_false_positive_rate` is 0.0. Change the model to predict a 50 mL blob on
    every case and assert `empty_gt_false_positive_rate` becomes 1.0 while
    `dice_mean_per_case` does not rise."
    """
    cohort = _prepare(eval_db, store, n_patients=40)
    records = sorted(cohort.test_records, key=lambda r: r.case_key)
    assert len(records) == 20

    silent = [
        *[
            _observation(r, gt_voxels=120_000, pred_voxels=0, intersection_voxels=0,
                         case_score=0.05, case_label=1)
            for r in records[:10]
        ],
        *[
            _observation(r, gt_voxels=0, pred_voxels=0, intersection_voxels=0,
                         case_score=0.05, case_label=0)
            for r in records[10:]
        ],
    ]
    _run_a, result_a = _measure(eval_db, cohort, silent)

    dice_a = _entry(result_a.aggregates, "dice_mean_per_case")
    assert dice_a["n"] == 10
    assert dice_a["value"] == 0.0
    assert _entry(result_a.aggregates, "empty_gt_case_count", "gt_empty")["value"] == 10
    assert _entry(
        result_a.aggregates, "empty_gt_false_positive_rate", "gt_empty"
    )["value"] == 0.0

    # Same cohort, same reference standard, a model that now paints 50 mL on every case.
    # 50_000 voxels at 0.001 mL is 50 mL, five times the declared threshold.
    noisy = [
        *[
            _observation(r, gt_voxels=120_000, pred_voxels=50_000,
                         intersection_voxels=0, case_score=0.95, case_label=1)
            for r in records[:10]
        ],
        *[
            _observation(r, gt_voxels=0, pred_voxels=50_000, intersection_voxels=0,
                         case_score=0.95, case_label=0)
            for r in records[10:]
        ],
    ]
    _run_b, result_b = _measure(
        eval_db, cohort, noisy, image_digest="sha256:" + "e" * 64
    )
    dice_b = _entry(result_b.aggregates, "dice_mean_per_case")
    assert dice_b["n"] == 10
    assert dice_b["value"] <= dice_a["value"], (
        "predicting a blob everywhere MUST NOT raise the headline figure; if it does, "
        "the empty-GT policy has been changed to score_one and the number is now a "
        "function of cohort composition"
    )
    assert _entry(
        result_b.aggregates, "empty_gt_false_positive_rate", "gt_empty"
    )["value"] == 1.0
    assert _entry(
        result_b.aggregates, "empty_gt_max_fp_volume_ml", "gt_empty"
    )["value"] == pytest.approx(50.0)


def test_the_database_refuses_an_empty_gt_case_scored_as_a_hit(eval_db, store) -> None:
    """MOS-EVID-051 rows 3 and 4, as a CHECK.

    `score_one` is not offered as a column (MOS-STORE-296) -- and this is the hole one
    level down, where it would actually be opened: by writing `value = 1.0, eligible =
    true` on a case with no reference voxels. A fraction f of negatives would then put a
    floor of f under the published figure.
    """
    cohort = _prepare(eval_db, store)
    run = _create(eval_db, cohort)
    ev_run.start_run(eval_db, run_id=run.id)
    record = cohort.test_records[0]
    _refuses(
        eval_db,
        """
        INSERT INTO evaluation_case_metrics (
          tenant_id, evaluation_run_id, case_key, patient_key, study_instance_uid,
          series_instance_uid, metric, value, eligible, gt_voxels, pred_voxels,
          intersection_voxels, gt_volume_ml, pred_volume_ml, strata)
        VALUES (current_tenant_id(), %s, %s, %s, %s, %s, 'dice_mean_per_case',
                1.0, true, 0, 0, 0, 0.0, 0.0, '{}'::jsonb)
        """,
        (run.id, record.case_key, record.patient_key, record.study_instance_uid,
         record.series_instance_uid),
        "23514",
    )


def test_a_missed_finding_scores_zero_and_counts(eval_db, store) -> None:
    """MOS-EVID-051's deliberate asymmetry, which is the half people get wrong.

    An empty PREDICTION against a non-empty reference is not "undefined". It is the
    failure of the thing being measured, it scores 0.0, and it is counted -- otherwise a
    model that finds nothing at all has an undefined Dice over an empty case set and a
    published figure of "n/a" rather than zero.
    """
    cohort = _prepare(eval_db, store)
    records = sorted(cohort.test_records, key=lambda r: r.case_key)
    observations = [
        _observation(records[0], gt_voxels=90_000, pred_voxels=0, intersection_voxels=0),
        *[
            _observation(r, gt_voxels=90_000, pred_voxels=90_000,
                         intersection_voxels=81_000)
            for r in records[1:]
        ],
    ]
    run, result = _measure(eval_db, cohort, observations)

    row = dict(
        eval_db.execute(
            "SELECT value, eligible, undefined_reason FROM evaluation_case_metrics "
            "WHERE evaluation_run_id = %s AND case_key = %s AND metric = "
            "'dice_mean_per_case'",
            (run.id, records[0].case_key),
        ).fetchone()
    )
    assert row["value"] == 0.0
    assert row["eligible"] is True
    assert row["undefined_reason"] is None
    assert _entry(result.aggregates, "dice_mean_per_case")["n"] == len(records)


# =====================================================================================
# 5. The binding.  MOS-EVID-061 to MOS-EVID-064.
# =====================================================================================
def test_the_binding_is_immutable_and_the_digest_is_sealed(eval_db, store) -> None:
    """Chapter 7 acceptance check 1, the `evaluation_runs.run_digest` clause.

    Also the binding itself: a run whose cohort digest can be edited afterwards binds
    nothing, because the row would say one cohort while the number came from another.
    """
    cohort = _prepare(eval_db, store)
    run, result = _measure(eval_db, cohort, _good_observations(cohort))

    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET run_digest = %s WHERE id = %s",
        ("sha256:" + "0" * 64, run.id),
        "MOS06",
    )
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET dataset_version_digest = %s WHERE id = %s",
        ("sha256:" + "0" * 64, run.id),
        "MOS06",
    )
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET aggregate_metrics = %s WHERE id = %s",
        (Jsonb([{"metric": "dice_mean_per_case", "stratum": "all", "value": 0.99,
                 "ci_low": 0.98, "ci_high": 1.0, "n": 5, "n_patients": 5}]), run.id),
        "MOS06",
    )
    # The digest still addresses the block that was measured.
    stored = ev_run.get_run(eval_db, run.id)
    assert stored.run_digest == result.run_digest


def test_one_binding_yields_one_run_not_a_second_opinion(eval_db, store) -> None:
    """The partial unique index of section 7.7.1, over a binding that actually collides.

    Two runs, same subject, same cohort, same split, same partition, same reference
    standard, same image, same thresholds, same conventions -- and different numbers.
    Exactly the shape of "run it again until it passes". The second SUCCEEDED row is a
    duplicate, not a second opinion.
    """
    cohort = _prepare(eval_db, store)
    _first, _result = _measure(eval_db, cohort, _good_observations(cohort))

    second = _create(eval_db, cohort)
    ev_run.start_run(eval_db, run_id=second.id)
    worse = [
        _observation(r, gt_voxels=90_000, pred_voxels=20_000, intersection_voxels=18_000)
        for r in sorted(cohort.test_records, key=lambda r: r.case_key)
    ]
    ev_run.record_cases(eval_db, run_id=second.id, observations=worse)
    with pytest.raises(psycopg.errors.UniqueViolation):
        ev_run.finish_run(eval_db, run_id=second.id, annotation_store=cohort.store)
    eval_db.rollback()


def test_a_dirty_working_tree_cannot_produce_evidence(eval_db, store) -> None:
    """MOS-EVID-062 and chapter 7 acceptance check 14, in both layers."""
    cohort = _prepare(eval_db, store)
    run = _create(eval_db, cohort, code_dirty=True)
    ev_run.start_run(eval_db, run_id=run.id)
    ev_run.record_cases(
        eval_db, run_id=run.id, observations=_good_observations(cohort)
    )
    with pytest.raises(EvaluationRefused) as exc:
        ev_run.finish_run(eval_db, run_id=run.id, annotation_store=cohort.store)
    assert "MOS-EVID-062" in exc.value.check_ids
    assert exc.value.as_problem()["class"] == "clinical_rejection"

    eval_db.rollback()
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET state = 'SUCCEEDED', finished_at = now(), "
        "run_digest = %s, aggregate_metrics = '[]'::jsonb WHERE id = %s",
        ("sha256:" + "1" * 64, run.id),
        "23514",
    )


def test_a_threshold_selected_on_the_reported_partition_is_refused(
    eval_db, store
) -> None:
    """MOS-EVID-033 and chapter 7 acceptance check 13.

    "an undisclosed selected metric is byte-for-byte indistinguishable from a held-out
    one, which is the specific way this failure stays silent until the second site."
    """
    cohort = _prepare(eval_db, store)
    with pytest.raises(EvaluationRefused) as exc:
        _create(
            eval_db,
            cohort,
            operating_thresholds={
                "effusion_probability": {"value": 0.5, "selected_on": "test"}
            },
        )
    assert "MOS-EVID-033" in exc.value.check_ids
    assert eval_db.execute("SELECT count(*) AS n FROM evaluation_runs").fetchone()[
        "n"
    ] == 0


def test_the_subject_is_exactly_one_artifact(eval_db, store) -> None:
    """MOS-EVID-061: "exactly one MUST be non-null", in the library and in the schema."""
    cohort = _prepare(eval_db, store)
    with pytest.raises(EvaluationRefused) as exc:
        _create(eval_db, cohort, service_version_id=SERVICE_VERSION)
    assert "MOS-EVID-061" in exc.value.check_ids

    with pytest.raises(EvaluationRefused):
        _create(eval_db, cohort, model_version_id=None)

    # A sealed-mode run binds the vendor-declared pipeline digest (MOS-EVID-063).
    with pytest.raises(EvaluationRefused) as exc:
        _create(
            eval_db, cohort, model_version_id=None, service_version_id=SERVICE_VERSION,
            preprocessing_spec_digest=None,
        )
    assert "MOS-EVID-063" in exc.value.check_ids

    sealed = _create(
        eval_db, cohort, model_version_id=None, service_version_id=SERVICE_VERSION,
        preprocessing_spec_id=None, preprocessing_spec_version=None,
        preprocessing_spec_digest=None,
        internal_pipeline_digest="sha256:" + "a" * 64,
    )
    assert sealed.service_version_id == SERVICE_VERSION


def test_a_reference_standard_from_another_cohort_cannot_be_bound(
    eval_db, store
) -> None:
    """MOS-EVID-061. Three satisfied foreign keys, one meaningless run.

    The pairing rule of MOS-EVID-086 and the coverage rule of acceptance check 6 both
    assume the cohort, the split and the reference standard agree.
    """
    first = _prepare(eval_db, store)
    second = _prepare(eval_db, store, offset=100)
    with pytest.raises(EvaluationRefused) as exc:
        _create(eval_db, first, annotation_set_id=second.annotation_set_id)
    assert "MOS-EVID-061" in exc.value.check_ids

    with pytest.raises(EvaluationRefused):
        _create(eval_db, first, split_id=second.split_id)

    # And the schema refuses it too, without the library's help.
    _refuses(
        eval_db,
        """
        INSERT INTO evaluation_runs (
          public_id, tenant_id, kind, model_version_id, capability_id,
          dataset_version_id, dataset_version_digest, split_id, split_digest, partition,
          annotation_set_id, annotation_digest, preprocessing_spec_digest, code_commit,
          code_dirty, image_digest, inference_backend, accelerator, metric_conventions,
          metric_registry_version, seed, runner)
        SELECT 'evr_01ARZ3NDEKTSV4RRFFQ69G5FAV', current_tenant_id(), 'vendor_evidence',
               %s, 'pleural_effusion', v.id, v.manifest_digest, %s, s.manifest_digest,
               'test', %s, a.manifest_digest, %s, %s, false, %s,
               '{}'::jsonb, '{}'::jsonb, %s, 1, 20260101, 'x'
          FROM dataset_versions v, dataset_splits s, annotation_sets a
         WHERE v.id = %s AND s.id = %s AND a.id = %s
        """,
        (MODEL_VERSION, first.split_id, second.annotation_set_id,
         "sha256:" + "d" * 64, COMMIT, IMAGE, Jsonb(_conventions()),
         first.version_id, first.split_id, second.annotation_set_id),
        "23503",
    )


def test_the_capability_of_the_reference_standard_must_match(eval_db, store) -> None:
    """A reference standard for one clinical function does not measure another."""
    cohort = _prepare(eval_db, store)
    with pytest.raises(EvaluationRefused) as exc:
        _create(eval_db, cohort, capability_id="lung_segmentation")
    assert any("capability" in r.code for r in exc.value.refusals)


# =====================================================================================
# 6. Coverage and leakage at finish time
# =====================================================================================
def test_the_run_fails_rather_than_shrinking_n(eval_db, store) -> None:
    """Chapter 7 acceptance check 6, stated as the failure it prevents.

    A run that quietly measures 4 of 5 cases reports a real number about a cohort nobody
    chose, and the number is indistinguishable from an honest one -- it has an n, an
    interval and a cohort digest. The only thing wrong with it is the thing the row does
    not say.
    """
    cohort = _prepare(eval_db, store)
    run = _create(eval_db, cohort)
    ev_run.start_run(eval_db, run_id=run.id)
    ev_run.record_cases(
        eval_db, run_id=run.id, observations=_good_observations(cohort)[:-1]
    )
    with pytest.raises(EvaluationRefused) as exc:
        ev_run.finish_run(eval_db, run_id=run.id, annotation_store=cohort.store)
    assert "MOS-EVID-065" in exc.value.check_ids
    assert "partition_not_fully_measured" in {r.code for r in exc.value.refusals}


def test_a_train_patient_measured_by_a_test_run_is_refused(eval_db, store) -> None:
    """Chapter 12 acceptance check 15, at the moment the run would become evidence."""
    cohort = _prepare(eval_db, store)
    # Re-freeze a split that has a train partition, so the check has something to catch.
    keys = sorted({r.patient_key for r in cohort.records})
    assignments = [
        (k, "train" if i < 4 else ("tune" if i < 6 else "test"))
        for i, k in enumerate(keys)
    ]
    split = ev_repo.freeze_split(
        eval_db,
        dataset_version_id=cohort.version_id,
        name="three-way",
        assignments=assignments,
        store=InMemoryManifestStore(),
        bucket=BUCKET,
        frozen_by=OPERATOR,
        assignment_method="materialised, first four to train",
        records=cohort.records,
    )
    test_keys = {k for k, part in assignments if part == "test"}
    train_key = assignments[0][0]
    records = [r for r in cohort.records if r.patient_key in test_keys]
    smuggled = next(r for r in cohort.records if r.patient_key == train_key)

    run = _create(eval_db, cohort, split_id=split.id)
    ev_run.start_run(eval_db, run_id=run.id)
    ev_run.record_cases(
        eval_db,
        run_id=run.id,
        observations=[
            *[
                _observation(r, gt_voxels=90_000, pred_voxels=90_000,
                             intersection_voxels=81_000)
                for r in records
            ],
            _observation(smuggled, gt_voxels=90_000, pred_voxels=90_000,
                         intersection_voxels=89_000),
        ],
    )
    with pytest.raises(EvaluationRefused) as exc:
        ev_run.finish_run(eval_db, run_id=run.id, annotation_store=cohort.store)
    assert "MOS-EVID-034" in exc.value.check_ids
    assert "train_patient_in_evaluation" in {r.code for r in exc.value.refusals}


# =====================================================================================
# 7. Recomputation and uncertainty.  MOS-EVID-057 to MOS-EVID-060, MOS-EVID-066.
# =====================================================================================
def test_aggregates_are_recomputable_from_the_persisted_rows(eval_db, store) -> None:
    """Chapter 7 acceptance check 10: within 1e-9 relative, for every run.

    And the negative half, which is the part that makes the check able to fail: a block
    built from a DIFFERENT case set does not agree, and the disagreement names the metric.
    """
    cohort = _prepare(eval_db, store)
    run, result = _measure(eval_db, cohort, _good_observations(cohort))

    ok, differences = ev_run.verify_aggregates(eval_db, run_id=run.id)
    assert ok, differences

    rows = ev_run.load_case_metrics(eval_db, run.id)
    truncated = ev_agg.aggregate_metrics(
        [r for r in rows if r["case_key"] != rows[0]["case_key"]],
        conventions=_conventions(),
        case_scores=ev_run.load_case_scores(eval_db, run.id),
        operating_point={"name": "effusion_probability", "value": 0.5,
                         "selected_on": "tune"},
    )
    agreed, differences = ev_agg.aggregates_agree(result.aggregates, truncated)
    assert not agreed
    assert any("dice_mean_per_case" in d for d in differences)


def test_confidence_intervals_are_reproducible_and_clustered() -> None:
    """Chapter 7 acceptance check 11, both halves. MOS-EVID-057, MOS-EVID-058.

    Bit-identical on recomputation from the same rows and seed -- not "close", identical,
    because a CI that moves between two recomputations of the same rows cannot be
    verified offline by a reader holding the bundle (section 7.12.3).

    And the half that makes clustering falsifiable: on a fixture where one patient
    contributes four studies, resampling CASES gives a different interval from resampling
    PATIENTS. If the two agreed, nothing in the implementation would be doing the
    clustering and the requirement would be decorative.
    """
    values = [0.90, 0.20, 0.25, 0.22, 0.88, 0.86, 0.91, 0.87, 0.85, 0.89]
    # One patient with four studies, six patients with one each.
    patients = ["pk_" + p for p in
                ["aaaaaaaaaaaaaaaa"] * 4 + [f"bbbbbbbbbbbbbbb{i}" for i in range(6)]]

    first = ev_ci.cluster_bootstrap_ci(values, patients, b=2000, seed=20260101)
    again = ev_ci.cluster_bootstrap_ci(values, patients, b=2000, seed=20260101)
    assert first == again, "the interval MUST be bit-identical on recomputation"

    wrong = ev_ci.case_bootstrap_ci(values, b=2000, seed=20260101)
    assert first != wrong, (
        "resampling cases instead of patients produced the same interval, which means "
        "the clustering is not actually applied (MOS-EVID-057)"
    )
    # THE DIRECTION IS DELIBERATELY NOT ASSERTED. "Clustering widens the interval" is the
    # usual summary and it is not a theorem: on this fixture the four correlated studies
    # sit at one end of the range, so the clustered interval is marginally NARROWER
    # (measured while writing this: 0.375 wide against 0.382). MOS-EVID-057 is about the
    # independence assumption being false, not about the width moving one way -- and a
    # test asserting the folklore would have gone red here for the right reason and been
    # "fixed" by relaxing the real claim.


def test_the_pooled_interval_is_recomputable_from_the_persisted_counts(
    eval_db, store
) -> None:
    """MOS-EVID-060, which is why MOS-EVID-068 persists the counts on every case.

    `dice_pooled` has no per-case value to resample. If the three voxel counts were not
    persisted -- and it is tempting not to persist them on a case whose per-case metric is
    NULL -- the pooled figure would have no interval, which MOS-EVID-056 forbids.
    """
    cohort = _prepare(eval_db, store)
    run, result = _measure(eval_db, cohort, _good_observations(cohort))
    pooled = _entry(result.aggregates, "dice_pooled")
    assert pooled["ci_low"] < pooled["value"] < pooled["ci_high"]

    rows = [
        r for r in ev_run.load_case_metrics(eval_db, run.id)
        if r["metric"] == "dice_mean_per_case" and r["eligible"]
    ]
    lo, hi = ev_ci.cluster_bootstrap_ratio_ci(
        [2.0 * r["intersection_voxels"] for r in rows],
        [r["gt_voxels"] + r["pred_voxels"] for r in rows],
        [r["patient_key"] for r in rows],
        b=_FAST_B,
        seed=20260101,
    )
    assert (lo, hi) == (pooled["ci_low"], pooled["ci_high"])


def test_an_unregistered_metric_id_is_refused(eval_db, store) -> None:
    """MOS-EVID-054 and MOS-EVID-070. A metric nobody registered is a number nobody
    can interpret in five years, and the registry version is what makes that decidable.
    """
    with pytest.raises(ev_metrics.UnknownMetric):
        ev_metrics.case_metric_rows(
            _observation(_record(0), gt_voxels=10, pred_voxels=10,
                         intersection_voxels=9),
            metrics=("dice_at_my_operating_point",),
        )
    with pytest.raises(ev_metrics.UnknownMetric):
        ev_agg.aggregate_metrics(
            [
                {
                    "case_key": "c", "patient_key": "pk_aaaaaaaaaaaaaaaa",
                    "series_instance_uid": "1.2.3", "metric": "made_up",
                    "value": 0.9, "undefined_reason": None, "eligible": True,
                    "gt_voxels": 1, "pred_voxels": 1, "intersection_voxels": 1,
                    "gt_volume_ml": 0.1, "pred_volume_ml": 0.1, "strata": {},
                }
            ],
            conventions=_conventions(),
        )


def test_a_threshold_metric_without_its_operating_point_is_refused() -> None:
    """MOS-EVID-055: "A number without an operating point is not a measurement."."""
    with pytest.raises(ev_agg.AggregationError) as exc:
        ev_agg.validate_aggregates(
            [{"metric": "sensitivity", "stratum": "all", "value": 0.91,
              "ci_low": 0.88, "ci_high": 0.95, "n": 40, "n_patients": 40}]
        )
    assert "MOS-EVID-055" in str(exc.value)

    with pytest.raises(ev_agg.AggregationError) as exc:
        ev_agg.validate_aggregates([{"metric": "dice_mean_per_case", "value": 0.91}])
    assert "MOS-EVID-056" in str(exc.value)


def test_a_subgroup_that_collapsed_is_reported_with_n_zero(eval_db, store) -> None:
    """MOS-EVID-067 and acceptance check 22: absent is not the same as empty.

    A criterion over a stratum that vanished must come back INDETERMINATE, and it cannot
    do that if the aggregate is simply missing from the block -- the gate would find
    nothing to evaluate and say nothing.
    """
    cohort = _prepare(eval_db, store)
    subgroups = (
        # `_good_observations` gives 100, 110, 120, 130 and 140 mL, so the bound
        # selects the first three -- a subgroup that is neither everything nor nothing.
        ev_agg.Subgroup("small_effusion", {"reference_volume_ml": {"op": "lt",
                                                                  "value": 125.0}}),
        ev_agg.Subgroup("paediatric", {"patient_age_years": {"op": "lt", "value": 18}}),
    )
    _run_row, result = _measure(
        eval_db, cohort, _good_observations(cohort), subgroups=subgroups
    )
    small = _entry(result.aggregates, "dice_mean_per_case", "small_effusion")
    assert small["n"] == 3 and small["value"] is not None
    empty = _entry(result.aggregates, "dice_mean_per_case", "paediatric")
    assert empty["n"] == 0 and empty["value"] is None
    assert empty["note"] == "no_eligible_cases"


# =====================================================================================
# 8. The defect cascade.  MOS-EVID-014, MOS-STORE-301a.
# =====================================================================================
def test_marking_the_cohort_defective_invalidates_every_run(eval_db, store) -> None:
    """MOS-EVID-014 and MOS-STORE-301a: one transaction, not a background job.

    Enforced by a trigger and not by the repository function, so it also binds the
    operator with psql and the repair script written next quarter -- which is the same
    argument MOS-EVID-013 makes for putting sealing in the database, pointed the other
    way.
    """
    cohort = _prepare(eval_db, store)
    run, _result = _measure(eval_db, cohort, _good_observations(cohort))
    assert ev_run.get_run(eval_db, run.id).state == "SUCCEEDED"

    ev_repo.mark_defective(
        eval_db, version_id=cohort.version_id, reason="reference standard re-read"
    )
    invalidated = ev_run.get_run(eval_db, run.id)
    assert invalidated.state == "INVALIDATED"
    assert "DEFECTIVE" in (invalidated.invalidation_reason or "")
    assert "reference standard re-read" in (invalidated.invalidation_reason or "")

    # The per-case rows survive: the evidence is revoked, not destroyed. A reader of an
    # old report needs to see what was measured and that it no longer stands.
    assert eval_db.execute(
        "SELECT count(*) AS n FROM evaluation_case_metrics WHERE "
        "evaluation_run_id = %s",
        (run.id,),
    ).fetchone()["n"] > 0

    # And a defective cohort acquires no new evidence.
    with pytest.raises(EvaluationRefused) as exc:
        _create(eval_db, cohort, image_digest="sha256:" + "f" * 64)
    assert "MOS-EVID-014" in exc.value.check_ids


def test_an_invalidated_run_is_terminal(eval_db, store) -> None:
    """MOS-EVID-014: a run that has been invalidated is superseded, never repaired."""
    cohort = _prepare(eval_db, store)
    run, _result = _measure(eval_db, cohort, _good_observations(cohort))
    ev_run.invalidate_run(eval_db, run_id=run.id, reason="wrong reference standard")
    eval_db.commit()

    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET state = 'SUCCEEDED' WHERE id = %s",
        (run.id,),
        "MOS06",
    )
    _refuses(
        eval_db,
        "UPDATE evaluation_runs SET state = 'RUNNING' WHERE id = %s",
        (run.id,),
        "MOS06",
    )


# =====================================================================================
# 9. ONE EVALUATION IMPLEMENTATION, NOT TWO
# =====================================================================================
def test_one_evaluation_implementation_not_two() -> None:
    """docs/spec/15-delivery.md section 15.2.5: "One evaluation implementation, not two."

    The defect this is written against is invisible by construction: the development
    number and the gate number differ by less than anyone notices and more than the gate
    is being asked to rule on, and the two are never printed side by side. A grep is a
    crude instrument, but it is the only one that can see a SECOND implementation -- a
    test of the first one passes just as happily when a second exists.

    Four claims, each stated as "this decision is made in exactly one module":

      1. the PER-CASE value -- what a case's Dice IS (MOS-EVID-047, MOS-EVID-051);
      2. the ELIGIBILITY decision -- what `undefined_reason` a case gets, which is the
         empty-GT convention in executable form;
      3. the cluster bootstrap (MOS-EVID-057), which every interval in the platform is
         computed by;
      4. the registry version (MOS-EVID-054), so "which metrics exist" has one answer.

    What it deliberately does NOT claim is that the word `intersection` appears once: the
    POOLED formula of MOS-EVID-048 is a sum over the same persisted counts, and a reader
    recomputing a bundle offline (section 7.12.3) legitimately re-derives it from the
    rows. The line worth defending is the one where a JUDGEMENT is made -- what counts,
    what is excluded, and what a value means -- not every arithmetic expression.
    """
    # parents[3], not [2]. `ev_repo` is `medos/medos/evidence/repo.py`, so two levels up
    # is the PRODUCT directory and `root / "medos" / "medos"` became `medos/medos/medos`
    # -- a glob over nothing. The scan returned an empty list and the assertion read
    # "one definition expected, none found", which looks like a deletion.
    root = Path(ev_repo.__file__).resolve().parents[3]
    sources = {
        path: path.read_text(encoding="utf-8")
        for path in (root / "medos" / "medos").rglob("*.py")
    }

    def _defining(pattern: str) -> list[str]:
        # MULTILINE: the claim is about a top-level DEFINITION, which is a `^def` on its
        # own line -- not about the name appearing somewhere in the file, which every
        # caller does.
        rx = re.compile(pattern, re.MULTILINE)
        return sorted(
            path.relative_to(root).as_posix()
            for path, text in sources.items()
            if rx.search(text)
        )

    assert _defining(r"^def case_dice\(") == ["medos/medos/evidence/metrics.py"], (
        "the per-case Dice is defined in more than one place; MOS-EVID-047 fixes one "
        "definition and two implementations of it drift invisibly"
    )
    # An ASSIGNMENT of one of the four reasons, not a comparison against one: reading
    # `undefined_reason == 'empty_ground_truth'` back off a persisted row is what every
    # consumer does, and deciding it is what only one module may do.
    assert _defining(r"(?<![=!<>])=\s*['\"]empty_ground_truth['\"]") == [
        "medos/medos/evidence/metrics.py"
    ], (
        "MOS-EVID-051 is one convention, decided in one place; a second module assigning "
        "an undefined_reason is a second empty-ground-truth policy"
    )
    assert _defining(r"^def cluster_bootstrap_ci\(") == ["medos/medos/evidence/ci.py"]
    assert _defining(r"^METRIC_REGISTRY_VERSION\s*=") == ["medos/medos/evidence/metrics.py"]

    callers = sorted(
        path.relative_to(root).as_posix()
        for path, text in sources.items()
        if "case_bootstrap_ci(" in text and path.name != "ci.py"
    )
    assert callers == [], (
        f"MOS-EVID-057 forbids resampling cases independently; called from {callers}"
    )


def test_the_registry_covers_every_metric_the_block_can_name() -> None:
    """MOS-EVID-054, and the specification defect it collides with.

    MOS-EVID-052 requires the five-metric empty-GT block to be persisted; MOS-EVID-054
    forbids persisting a metric id absent from its registry table, which lists only one
    of the five. Registering all five is the only reading under which both requirements
    can hold, and this test pins that decision so the next reader finds it deliberate.
    """
    for metric in ev_metrics.EMPTY_GT_METRICS:
        assert metric in ev_metrics.REGISTRY, metric
    for metric in ev_metrics.SEGMENTATION_PER_CASE_METRICS:
        assert ev_metrics.REGISTRY[metric].per_case, metric
    assert "dice" not in ev_metrics.REGISTRY
    assert ev_metrics.METRIC_REGISTRY_VERSION >= 1
    # MOS-EVID-050: there is no default. Both ids exist and neither is named `dice`.
    assert {"dice_mean_per_case", "dice_pooled"} <= set(ev_metrics.REGISTRY)
