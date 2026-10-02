# SPDX-License-Identifier: Apache-2.0
"""`per-case-metrics` -- the 0.2.0 gate check of docs/spec/15-delivery.md section 15.1.2.

    `per-case-metrics` (per-case rows persisted, not only aggregates)

Section 15.2.5 states it as an emphasis: "evaluation runs with **per-case metrics
persisted, not only aggregates**". Section 15.1.3 puts per-case metrics in 0.2.0's
**Tier A**: MUST NOT be cut. `MOS-EVID-065` is the requirement -- "Aggregates alone MUST
NOT be stored without them."

WHY A MEAN IS NOT EVIDENCE, AND WHY THIS IS TIER A
---------------------------------------------------
Three things become impossible the moment the per-case rows are gone, and all three of them
are this release:

  * the deployment gate. Section 7.9.2's test is PAIRED and per-case: there is no way to
    compute it from two aggregates, so a release that persisted only means would ship a
    `deployment-gate` check that could not run at all.
  * the stratified breakdown. `MOS-EVID-067`: "a stratum that is not persisted cannot be
    gated on" -- the collapse that `non-inferiority` catches is invisible in the mean by
    construction.
  * re-verification by anybody else. `report-offline-verify`'s check 5 recomputes the
    aggregates from `case_metrics.csv`; without the rows a `ValidationReport` is a claim
    with nothing behind it, which is `MOS-EVID-121`'s complaint exactly.

So this check asserts PERSISTENCE, not computation: the rows are in the database, they
carry the counts and the strata, the aggregate is derivable FROM them, a run cannot reach
SUCCEEDED without them, and nothing can be appended once it has.

Needs Postgres: a throwaway database created by `tests/gate/conftest.py::evidence_dsn`.

Spec: MOS-EVID-047, MOS-EVID-051, MOS-EVID-054, MOS-EVID-065, MOS-EVID-066, MOS-EVID-067,
MOS-EVID-068, MOS-STORE-299, MOS-REL-004, MOS-REL-012.
"""

from __future__ import annotations

import secrets
from typing import Any

import psycopg
import pytest
from medos.evidence import evaluation as ev_run
from medos.evidence import metrics as ev_metrics
from medos.sdk.refusal import EvaluationRefused

from tests.gate._evidence import CAPABILITY, Cohort

pytestmark = pytest.mark.gate_0_2_0

COMMIT = "7e41b2c8a95d3f06b18e4c7a2d905f3b6c81e024"
IMAGE = "sha256:" + "c" * 64
MODEL_VERSION_ID = "22222222-2222-2222-2222-222222222222"

#: `MOS-EVID-058`'s default is B = 2000. Lowered here because this check is about the rows
#: and not about the interval, and 2000 replicates per aggregate is a minute of pure Python
#: that buys this check nothing. `non-inferiority` uses a realistic B on both arms.
FAST_B = 120

#: 34 patients: above `MOS-EVID-026`'s 30-patient floor for an acceptance cohort, and above
#: every `min_cases` this module's criteria would need if it had any.
N_PATIENTS = 34


def _conventions(b: int = FAST_B) -> dict[str, Any]:
    return ev_metrics.metric_conventions(fp_volume_threshold_ml=10.0, bootstrap_b=b)


def _observation(record: Any, index: int, jitter: int = 0) -> ev_metrics.CaseObservation:
    """One measured case. Every stratum `MOS-EVID-067` requires, plus the clinical one.

    A sixth of the cases are reference-EMPTY. That is deliberate: `MOS-EVID-051` takes the
    empty-ground-truth case out of the Dice aggregate and reports it separately, and a
    cohort with no empty case cannot tell a correct implementation from one that scores
    them 0.0 and drags every mean down.

    `jitter` shifts every voxel count by a per-run amount, and it is NOT cosmetic.
    `evaluation_runs` is keyed `UNIQUE (tenant_id, run_digest)` and `run_digest` is the
    digest over the aggregate block (MOS-EVID-061), so two runs that produce numerically
    identical evidence are one row by design. Two tests in this module each need their own
    finished run, so their runs have to be genuinely different evidence -- which is the
    constraint behaving exactly as specified rather than an obstacle to work around.
    """
    empty = index % 6 == 5
    gt = 0 if empty else 100_000 + 5_000 * index + jitter
    pred = 900 if empty else gt + 4_000
    return ev_metrics.CaseObservation(
        case_key=record.case_key,
        patient_key=record.patient_key,
        study_instance_uid=record.study_instance_uid,
        series_instance_uid=record.series_instance_uid,
        gt_voxels=gt,
        pred_voxels=pred,
        intersection_voxels=0 if empty else int(gt * 0.88),
        gt_volume_ml=gt * 0.001,
        pred_volume_ml=pred * 0.001,
        strata={
            "slice_thickness_mm": record.acquisition.slice_thickness_mm,
            "pixel_spacing_mm_max": max(record.acquisition.pixel_spacing_mm),
            "convolution_kernel_class": record.acquisition.convolution_kernel_class,
            "manufacturer": record.acquisition.manufacturer,
            "contrast_phase": record.acquisition.contrast_phase,
            "patient_age_years": record.acquisition.patient_age_years,
            "patient_sex": record.acquisition.patient_sex,
            "reference_volume_ml": gt * 0.001,
        },
        case_score=0.70 + 0.005 * index,
        case_label=0 if empty else 1,
    )


def _create(conn: Any, cohort: Cohort, **over: Any) -> ev_run.RunRow:
    kwargs: dict[str, Any] = {
        "kind": "vendor_evidence",
        "capability_id": CAPABILITY,
        "dataset_version_id": cohort.version.id,
        "split_id": cohort.split.id,
        "partition": "test",
        "annotation_set_id": cohort.annotations.id,
        "model_version_id": MODEL_VERSION_ID,
        "preprocessing_spec_id": "ct-lung/v3",
        "preprocessing_spec_version": 3,
        "preprocessing_spec_digest": "sha256:" + "9" * 64,
        "code_commit": COMMIT,
        "image_digest": IMAGE,
        "runner": "release-0.2.0-gate",
        "fp_volume_threshold_ml": 10.0,
        "inference_backend": {"kind": "triton", "version": "25.03"},
        "accelerator": {"gpu_model": "NVIDIA A10", "driver": "550.90.07",
                        "cuda": "12.4", "trt": "10.0.1"},
        "operating_thresholds": {
            "effusion_probability": {"value": 0.50, "selected_on": "tune"}
        },
        "conventions": _conventions(),
    }
    kwargs.update(over)
    return ev_run.create_run(conn, **kwargs)


@pytest.fixture()
def measured(
    evidence_db: psycopg.Connection[Any], manifest_store: Any
) -> tuple[Cohort, ev_run.RunRow, ev_run.FinishResult, list[Any]]:
    """A cohort, a run over it, and the per-case rows persisted through the real path."""
    cohort = Cohort(evidence_db, manifest_store, n_patients=N_PATIENTS)
    evidence_db.commit()
    jitter = secrets.randbelow(10_000)
    observations = [
        _observation(r, i, jitter)
        for i, r in enumerate(sorted(cohort.records, key=lambda r: r.case_key))
    ]
    run = _create(evidence_db, cohort)
    ev_run.start_run(evidence_db, run_id=run.id)
    ev_run.record_cases(evidence_db, run_id=run.id, observations=observations)
    result = ev_run.finish_run(
        evidence_db, run_id=run.id, annotation_store=cohort.store
    )
    evidence_db.commit()
    return cohort, run, result, observations


# ======================================================================================
# 1. THE CHECK. The rows are in the database, with what MOS-EVID-067/068 require on them.
# ======================================================================================
def test_per_case_metrics_are_persisted_not_only_aggregates(
    evidence_db: psycopg.Connection[Any], measured: Any
) -> None:
    """`MOS-EVID-065`, read back out of the database by SQL.

    Read with SELECT rather than through `load_case_metrics`, because the claim is about
    what is IN the table: a repository function that reconstructed the rows from something
    held in memory would satisfy every assertion written against it and none written here.
    """
    _cohort, run, result, observations = measured

    rows = evidence_db.execute(
        "SELECT case_key, metric, value, eligible, undefined_reason, gt_voxels, "
        "       pred_voxels, intersection_voxels, strata "
        "  FROM evaluation_case_metrics WHERE evaluation_run_id = %s "
        " ORDER BY case_key, metric",
        (run.id,),
    ).fetchall()

    expected = len(observations) * len(ev_metrics.SEGMENTATION_PER_CASE_METRICS)
    assert len(rows) == expected, (
        f"{len(rows)} per-case rows for {len(observations)} cases and "
        f"{len(ev_metrics.SEGMENTATION_PER_CASE_METRICS)} metrics; expected {expected}"
    )
    assert {r["metric"] for r in rows} == set(ev_metrics.SEGMENTATION_PER_CASE_METRICS)

    # MOS-EVID-068: the voxel counts are persisted even where the VALUE is undefined,
    # which is the only way anyone can later tell "the model found nothing" apart from
    # "the reference was empty".
    for row in rows:
        assert row["gt_voxels"] is not None
        assert row["pred_voxels"] is not None
        assert row["intersection_voxels"] is not None

    # MOS-EVID-051: the empty-reference cases are marked ineligible with a REASON, not
    # scored 0.0 and quietly averaged in.
    ineligible = [r for r in rows if not r["eligible"]]
    assert ineligible, "the cohort contains no empty-reference case; see `_observation`"
    assert {r["undefined_reason"] for r in ineligible} == {"empty_ground_truth"}
    assert all(r["value"] is None for r in ineligible)

    # MOS-EVID-067: "a stratum that is not persisted cannot be gated on".
    for required in ev_metrics.__dict__.get("REQUIRED_STRATA", ()) or (
        "slice_thickness_mm", "pixel_spacing_mm_max", "convolution_kernel_class",
        "manufacturer", "contrast_phase", "patient_age_years", "patient_sex",
        "reference_volume_ml",
    ):
        assert all(required in r["strata"] for r in rows), required

    # MOS-STORE-299: one PRE-THRESHOLD score row per case, with the reference label. A
    # post-threshold decision cannot be re-thresholded, so an operating point could never
    # be moved without re-running inference.
    scores = evidence_db.execute(
        "SELECT case_key, case_score, case_label FROM evaluation_case_scores "
        " WHERE evaluation_run_id = %s",
        (run.id,),
    ).fetchall()
    assert len(scores) == len(observations)
    assert all(s["case_score"] is not None for s in scores)
    assert {s["case_label"] for s in scores} == {0, 1}

    # And the aggregate block exists BESIDE the rows, not instead of them.
    stored = ev_run.get_run(evidence_db, run.id)
    assert stored.state == "SUCCEEDED"
    assert isinstance(stored.aggregate_metrics, list) and stored.aggregate_metrics
    assert result.run_digest.startswith("sha256:")


def test_per_case_metrics_are_what_the_aggregate_is_derived_from(
    evidence_db: psycopg.Connection[Any], measured: Any
) -> None:
    """`MOS-EVID-066`: the published block is recomputable from the persisted rows ALONE.

    This is what makes persistence worth gating on. Rows that exist but do not reproduce
    the number the report quotes are worse than no rows: they look like an audit trail and
    they are a second, silently different, evaluation.

    `recompute_aggregates` reads the rows back from the database and recomputes; it is the
    same function `finish_run` used to produce the block, so "derivable from the per-case
    rows" is not a property the implementation must remember to preserve -- it is the only
    way the block is ever produced.
    """
    _cohort, run, _result, _obs = measured

    agrees, differences = ev_run.verify_aggregates(evidence_db, run_id=run.id)
    assert agrees, f"the stored aggregate block does not follow from the rows: {differences}"

    # Not vacuously true: there is a block, and it names the metrics the rows carry.
    stored = ev_run.get_run(evidence_db, run.id)
    metrics_in_block = {entry["metric"] for entry in stored.aggregate_metrics}
    assert "dice_mean_per_case" in metrics_in_block
    # MOS-EVID-047: mean-of-per-case and pooled are DIFFERENT conventions and both are
    # published, so nobody has to guess which one a bare "Dice" meant.
    assert "dice_pooled" in metrics_in_block

    recomputed = ev_run.recompute_aggregates(evidence_db, run_id=run.id)
    assert {e["metric"] for e in recomputed} == metrics_in_block


# ======================================================================================
# 2. A run cannot succeed without them, at the library AND at the database.
# ======================================================================================
def test_per_case_metrics_absent_means_the_run_cannot_succeed(
    evidence_db: psycopg.Connection[Any], manifest_store: Any
) -> None:
    """`MOS-EVID-065`: "Aggregates alone MUST NOT be stored without them."

    Asserted twice, at two layers, because a rule enforced only in the library is one
    forgotten call path away from nothing. The library refuses `finish_run`; the database
    refuses the shortcut -- "mark it SUCCEEDED and fill the numbers in later" -- with a
    CHECK constraint, so a hand-written UPDATE during an incident cannot produce a run
    that reads as evidence.
    """
    cohort = Cohort(evidence_db, manifest_store, n_patients=N_PATIENTS)
    evidence_db.commit()
    run = _create(evidence_db, cohort)
    ev_run.start_run(evidence_db, run_id=run.id)
    evidence_db.commit()

    with pytest.raises(EvaluationRefused) as exc:
        ev_run.finish_run(evidence_db, run_id=run.id, annotation_store=cohort.store)
    assert "MOS-EVID-065" in exc.value.check_ids
    evidence_db.rollback()
    assert ev_run.get_run(evidence_db, run.id).state == "RUNNING"

    with pytest.raises(psycopg.DatabaseError) as db_exc:
        evidence_db.execute(
            "UPDATE evaluation_runs SET state = 'SUCCEEDED', finished_at = now() "
            " WHERE id = %s",
            (run.id,),
        )
    # Asserted by SQLSTATE and not by exception class: psycopg maps the
    # implementation-defined classes onto the generic `DatabaseError`, so asserting on the
    # class would also pass on a typo'd column name, which is not the claim being made.
    assert db_exc.value.sqlstate == "23514"
    evidence_db.rollback()


def test_per_case_metrics_are_append_only_and_closed_once_the_run_finishes(
    evidence_db: psycopg.Connection[Any], measured: Any
) -> None:
    """Both tables are sealed, and the seal is at the DATABASE (MOS-EVID-013's shape).

    Two different holes, and the second is the one a library check would leave open:

      * an UPDATE or DELETE on a row the published aggregate was computed from. That is
        rewriting evidence after the fact, and it is refused.
      * an INSERT of a NEW case into a finished run. Append-only permits appending, which
        is exactly the wrong permission here: a case added afterwards is a case the
        published aggregate does not include and the row count no longer agrees with.
    """
    _cohort, run, _result, observations = measured

    for statement, params in (
        ("UPDATE evaluation_case_metrics SET value = 0.99 "
         " WHERE evaluation_run_id = %s", (run.id,)),
        ("DELETE FROM evaluation_case_metrics WHERE evaluation_run_id = %s", (run.id,)),
        ("UPDATE evaluation_case_scores SET case_score = 0.01 "
         " WHERE evaluation_run_id = %s", (run.id,)),
    ):
        with pytest.raises(psycopg.DatabaseError):
            evidence_db.execute(statement, params)
        evidence_db.rollback()

    smuggled = observations[0]
    with pytest.raises(psycopg.DatabaseError):
        evidence_db.execute(
            """
            INSERT INTO evaluation_case_metrics (
              tenant_id, evaluation_run_id, case_key, patient_key, study_instance_uid,
              series_instance_uid, metric, value, eligible, gt_voxels, pred_voxels,
              intersection_voxels, strata)
            VALUES (current_tenant_id(), %s, 'smuggled-in-afterwards', %s, %s, %s,
                    'dice_mean_per_case', 0.99, true, 1, 1, 1, '{}'::jsonb)
            """,
            (run.id, smuggled.patient_key, smuggled.study_instance_uid,
             smuggled.series_instance_uid),
        )
    evidence_db.rollback()

    # The row count is what it was, so the aggregate still follows from the rows.
    n = evidence_db.execute(
        "SELECT count(*) AS n FROM evaluation_case_metrics WHERE evaluation_run_id = %s",
        (run.id,),
    ).fetchone()["n"]
    assert n == len(observations) * len(ev_metrics.SEGMENTATION_PER_CASE_METRICS)


# ======================================================================================
# 3. There is one evaluation implementation, not two.
# ======================================================================================
def test_per_case_metrics_come_from_one_implementation(
    evidence_db: psycopg.Connection[Any], measured: Any
) -> None:
    """Section 15.2.5: "One evaluation implementation, not two."

    The failure this guards is silent by construction: a development number and a gate
    number that differ by less than anyone would notice and more than the gate is being
    asked to rule on, never printed side by side. `record_cases` does no arithmetic of its
    own -- every value comes from `medos.evidence.metrics.case_metric_rows` -- so the
    persisted row and the number a caller computes in memory for the same observation MUST
    be bit-identical, not merely close.
    """
    _cohort, run, _result, observations = measured

    persisted = {
        (r["case_key"], r["metric"]): r["value"]
        for r in ev_run.load_case_metrics(evidence_db, run.id)
    }
    for obs in observations:
        for row in ev_metrics.case_metric_rows(obs):
            stored = persisted[(row.case_key, row.metric)]
            if row.value is None:
                assert stored is None
            else:
                assert stored == row.value, (
                    f"{row.metric} on one case differs between the persisted row and a "
                    f"fresh computation from the same observation: {stored} vs "
                    f"{row.value}. That is two evaluation implementations."
                )
