# SPDX-License-Identifier: Apache-2.0
"""`EvaluationRun` and its per-case rows. Chapter 7 section 7.7.

Requirements implemented here
-----------------------------
MOS-EVID-003  every run is labelled with one of exactly three evidence kinds.
MOS-EVID-014  a run is INVALIDATED with a reason, never edited and never deleted; a
              DEFECTIVE DatasetVersion cascades INVALIDATED onto every run bound to it
              (the cascade itself is the database trigger 0007 section 8 installs).
MOS-EVID-033  the `test` partition MUST NOT be used to select an operating threshold, and
              no run may report a metric on the partition its threshold was selected on.
MOS-EVID-061  a run binds every input that can change a number.
MOS-EVID-062  `code_dirty = true` blocks SUCCEEDED. A run from an uncommitted working
              tree is not evidence.
MOS-EVID-065  per-case metrics are persisted; aggregates alone MUST NOT be stored.
MOS-EVID-066  `aggregate_metrics` is recomputable from the per-case rows by the reference
              aggregation function, to within 1e-9 relative.
MOS-EVID-070  a metric id absent from the recorded registry version INVALIDATES the run.
MOS-STORE-301a the defect cascade is a transaction, not a background job.

THE DIGESTS ARE READ, NEVER ACCEPTED
------------------------------------
`create_run` takes the three ids -- DatasetVersion, DatasetSplit, AnnotationSet -- and
reads `manifest_digest` off each row itself. It does not take the digests as arguments,
however convenient that would be for a caller that already has them. A digest supplied by
the caller is a digest that can be wrong, and the failure is invisible: the run row would
name cohort A and carry cohort B's content address, the gate would pair it with the wrong
incumbent (MOS-EVID-086 pairs on digests), and nothing would ever complain. The id says
which row; the digest says which CONTENT; only the database knows both.

MOS-EVID-066 AND THE COUNT-BASED FAMILY -- A SPECIFICATION TENSION, REPORTED
---------------------------------------------------------------------------
MOS-EVID-066 requires `aggregate_metrics` to be recomputable "from
`evaluation_case_metrics` alone". `sensitivity`, `specificity` and `ppv` are registered
with `per_case: no` (MOS-EVID-054), so they have no row in that table by construction --
their inputs are `case_score` and `case_label` in `evaluation_case_scores`, which
MOS-EVID-069 and MOS-STORE-299 exist to persist precisely so those figures stay
recomputable and the threshold stays re-selectable. Read literally, the three
requirements cannot all hold.

`recompute_aggregates()` therefore reads BOTH per-case tables and the run's recorded
operating point. Both tables are per-case, both are append-only and sealed, and the
property MOS-EVID-066 is protecting -- no aggregate is a number without persisted
per-case evidence behind it -- holds exactly. The divergence from the literal text is
reported rather than hidden.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import tenant_tx
from medos.evidence import aggregate as _agg
from medos.evidence import digest as _digest
from medos.evidence import metrics as _metrics
from medos.evidence.store import ManifestStore
from medos.sdk.canonical import canonical_bytes
from medos.sdk.refusal import EvaluationRefused, Refusal

__all__ = [
    "RunRow",
    "FinishResult",
    "EVIDENCE_KINDS",
    "EVALUABLE_PARTITIONS",
    "RUN_STATES",
    "create_run",
    "start_run",
    "record_cases",
    "finish_run",
    "fail_run",
    "invalidate_run",
    "get_run",
    "load_case_metrics",
    "load_case_scores",
    "recompute_aggregates",
    "verify_aggregates",
]

# MOS-EVID-003. There is no fourth kind and no unlabelled evidence.
EVIDENCE_KINDS = ("vendor_evidence", "site_acceptance", "monitoring_period")

# MOS-EVID-031: `excluded` is a disposition, not a partition anyone evaluates on.
# MOS-EVID-033: `val` has never been a partition and this module adds none.
EVALUABLE_PARTITIONS = ("train", "tune", "test")

# MOS-EVID-014 owns this enum. The column is `state`, never `status`.
RUN_STATES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "INVALIDATED")


def _row(r: Any) -> dict[str, Any]:
    if r is None:
        raise LookupError("expected a row, got none")
    return dict(r) if isinstance(r, Mapping) else r


@dataclass(frozen=True)
class RunRow:
    """The binding, as persisted. Chapter 7 section 7.7.1's table."""

    id: str
    public_id: str
    kind: str
    capability_id: str
    service_version_id: str | None
    model_version_id: str | None
    dataset_version_id: str
    dataset_version_digest: str
    split_id: str
    split_digest: str
    partition: str
    annotation_set_id: str
    annotation_digest: str
    state: str
    code_dirty: bool
    metric_conventions: dict[str, Any]
    metric_registry_version: int
    seed: int
    operating_thresholds: dict[str, Any]
    aggregate_metrics: list[dict[str, Any]] | None = None
    run_digest: str | None = None
    invalidation_reason: str | None = None


@dataclass(frozen=True)
class FinishResult:
    run_id: str
    public_id: str
    run_digest: str
    aggregates: list[dict[str, Any]]
    case_count: int
    patient_count: int
    gates: dict[str, str] = field(default_factory=dict)


# =====================================================================================
# create
# =====================================================================================
def create_run(
    conn: psycopg.Connection[Any],
    *,
    kind: str,
    capability_id: str,
    dataset_version_id: str,
    split_id: str,
    partition: str,
    annotation_set_id: str,
    code_commit: str,
    image_digest: str,
    runner: str,
    fp_volume_threshold_ml: float,
    model_version_id: str | None = None,
    service_version_id: str | None = None,
    preprocessing_spec_id: str | None = None,
    preprocessing_spec_version: int | None = None,
    preprocessing_spec_digest: str | None = None,
    internal_pipeline_digest: str | None = None,
    code_dirty: bool = False,
    evaluator_image_digest: str | None = None,
    inference_backend: Mapping[str, Any] | None = None,
    accelerator: Mapping[str, Any] | None = None,
    operating_thresholds: Mapping[str, Mapping[str, Any]] | None = None,
    conventions: Mapping[str, Any] | None = None,
) -> RunRow:
    """Bind a run. `MOS-EVID-061`, `MOS-EVID-063`, `MOS-EVID-033`.

    Raises `EvaluationRefused` -- never a warning and never a half-bound row -- when the
    binding is one that cannot produce evidence. The refusals here are the ones that are
    cheapest to catch before any GPU time is spent and most expensive to discover
    afterwards: a defective cohort, a threshold selected on the partition being reported,
    a reference standard belonging to a different cohort or a different capability.
    """
    refusals: list[Refusal] = []
    thresholds = {k: dict(v) for k, v in (operating_thresholds or {}).items()}

    if kind not in EVIDENCE_KINDS:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-003",
                code="unlabelled_evidence",
                message=f"kind MUST be one of {EVIDENCE_KINDS}; there is no fourth kind "
                        "and no unlabelled evidence",
                observed=kind, bound=list(EVIDENCE_KINDS),
            )
        )
    if partition not in EVALUABLE_PARTITIONS:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-031",
                code="not_an_evaluable_partition",
                message="a run reports on train, tune or test; `excluded` is a "
                        "disposition and `val` is not a partition",
                observed=partition, bound=list(EVALUABLE_PARTITIONS),
            )
        )
    if (service_version_id is None) == (model_version_id is None):
        refusals.append(
            Refusal(
                check_id="MOS-EVID-061",
                code="subject_not_exactly_one",
                message="exactly one of service_version_id and model_version_id MUST be "
                        "non-null; which one is what `sealed subject` versus `native "
                        "subject` means",
                observed={"service_version_id": service_version_id,
                          "model_version_id": model_version_id},
            )
        )
    if model_version_id is not None and not preprocessing_spec_digest:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-061", code="native_run_without_preprocessing",
                message="a native-mode run MUST bind preprocessing_spec_digest "
                        "(chapter 4): the preprocessing is part of the number",
            )
        )
    if service_version_id is not None and not internal_pipeline_digest:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-063", code="sealed_run_without_pipeline_digest",
                message="a sealed-mode run MUST bind the vendor-declared "
                        "internal_pipeline_digest, and the report MUST state that it is "
                        "vendor-asserted rather than platform-verified",
            )
        )

    # MOS-EVID-033. The classic silent leak: a threshold chosen on the same cases the
    # figure is reported over. "an undisclosed selected metric is byte-for-byte
    # indistinguishable from a held-out one, which is the specific way this failure stays
    # silent until the second site."
    for name, spec in thresholds.items():
        if "value" not in spec or "selected_on" not in spec:
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-055", code="threshold_without_provenance",
                    message=f"operating threshold {name!r} MUST record its value and the "
                            "partition it was selected on",
                    observed=sorted(spec), bound=["value", "selected_on"],
                )
            )
        elif spec["selected_on"] == partition:
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-033", code="threshold_selected_on_this_partition",
                    message=f"threshold {name!r} was selected on {partition!r} and this "
                            f"run reports on {partition!r}; selection MUST use the tune "
                            "partition or cross-validation folds within train",
                    observed=spec.get("selected_on"), bound=partition,
                )
            )

    if refusals:
        raise EvaluationRefused(refusals)

    conventions = dict(
        conventions
        or _metrics.metric_conventions(fp_volume_threshold_ml=fp_volume_threshold_ml)
    )
    public_id = _digest.new_evaluation_run_id()

    with tenant_tx(conn):
        version = _row(
            conn.execute(
                "SELECT id, manifest_digest, status, usable_for_new_runs "
                "FROM dataset_versions WHERE id = %s",
                (dataset_version_id,),
            ).fetchone()
        )
        split = _row(
            conn.execute(
                "SELECT id, manifest_digest, dataset_version_id, partitions "
                "FROM dataset_splits WHERE id = %s",
                (split_id,),
            ).fetchone()
        )
        annotation = _row(
            conn.execute(
                # 0006 spells chapter 7's `annotation_digest` as `manifest_digest`, the
                # section 12.12 name of record; aliased here so the run row's column and
                # the chapter's field name read the same in this module.
                "SELECT id, manifest_digest AS annotation_digest, dataset_version_id, "
                "capability_id, reference_of_record FROM annotation_sets WHERE id = %s",
                (annotation_set_id,),
            ).fetchone()
        )

        # MOS-EVID-014 / MOS-STORE-292: a cohort that is DEFECTIVE, or has been marked
        # unusable by an erasure, MUST NOT acquire new evidence. Every run already bound
        # to it is INVALIDATED; starting another one is starting evidence that is already
        # revoked.
        if version["status"] != "SEALED" or not version["usable_for_new_runs"]:
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-014", code="cohort_not_usable",
                    message="the DatasetVersion is not SEALED and usable for new runs; a "
                            "defective or erasure-affected cohort MUST NOT acquire new "
                            "evidence",
                    observed={"status": version["status"],
                              "usable_for_new_runs": version["usable_for_new_runs"]},
                )
            )
        if str(split["dataset_version_id"]) != str(version["id"]):
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-061", code="split_belongs_to_another_cohort",
                    message="the split MUST be a split OF the bound DatasetVersion",
                )
            )
        if str(annotation["dataset_version_id"]) != str(version["id"]):
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-061",
                    code="annotation_set_belongs_to_another_cohort",
                    message="the AnnotationSet MUST be bound to the same DatasetVersion",
                )
            )
        if annotation["capability_id"] != capability_id:
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-061", code="reference_standard_wrong_capability",
                    message="the AnnotationSet's capability MUST be the capability being "
                            "evaluated; a reference standard for one clinical function "
                            "does not measure another",
                    observed=annotation["capability_id"], bound=capability_id,
                )
            )
        if partition not in list(split["partitions"] or []):
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-031", code="partition_absent_from_split",
                    message=f"the split declares {list(split['partitions'] or [])} and "
                            f"does not contain {partition!r}",
                )
            )
        if refusals:
            raise EvaluationRefused(refusals)

        row = _row(
            conn.execute(
                """
                INSERT INTO evaluation_runs (
                  public_id, tenant_id, kind, service_version_id, model_version_id,
                  capability_id, dataset_version_id, dataset_version_digest, split_id,
                  split_digest, partition, annotation_set_id, annotation_digest,
                  preprocessing_spec_id, preprocessing_spec_version,
                  preprocessing_spec_digest, internal_pipeline_digest, code_commit,
                  code_dirty, evaluator_image_digest, image_digest, inference_backend,
                  accelerator, operating_thresholds, metric_conventions,
                  metric_registry_version, seed, runner)
                VALUES (%s, current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, public_id, state
                """,
                (
                    public_id, kind, service_version_id, model_version_id, capability_id,
                    dataset_version_id, version["manifest_digest"], split_id,
                    split["manifest_digest"], partition, annotation_set_id,
                    annotation["annotation_digest"], preprocessing_spec_id,
                    preprocessing_spec_version, preprocessing_spec_digest,
                    internal_pipeline_digest, code_commit, code_dirty,
                    evaluator_image_digest, image_digest,
                    Jsonb(dict(inference_backend or {})),
                    Jsonb(dict(accelerator or {})), Jsonb(thresholds),
                    Jsonb(conventions), int(conventions["metric_registry_version"]),
                    int(conventions["bootstrap_seed"]), runner,
                ),
            ).fetchone()
        )

    return RunRow(
        id=str(row["id"]), public_id=row["public_id"], kind=kind,
        capability_id=capability_id, service_version_id=service_version_id,
        model_version_id=model_version_id, dataset_version_id=dataset_version_id,
        dataset_version_digest=version["manifest_digest"], split_id=split_id,
        split_digest=split["manifest_digest"], partition=partition,
        annotation_set_id=annotation_set_id,
        annotation_digest=annotation["annotation_digest"], state=row["state"],
        code_dirty=code_dirty, metric_conventions=conventions,
        metric_registry_version=int(conventions["metric_registry_version"]),
        seed=int(conventions["bootstrap_seed"]), operating_thresholds=thresholds,
    )


def get_run(conn: psycopg.Connection[Any], run_id: str) -> RunRow:
    with tenant_tx(conn):
        row = _row(
            conn.execute(
                "SELECT * FROM evaluation_runs WHERE id = %s", (run_id,)
            ).fetchone()
        )
    return RunRow(
        id=str(row["id"]), public_id=row["public_id"], kind=row["kind"],
        capability_id=row["capability_id"],
        service_version_id=(
            str(row["service_version_id"]) if row["service_version_id"] else None
        ),
        model_version_id=(
            str(row["model_version_id"]) if row["model_version_id"] else None
        ),
        dataset_version_id=str(row["dataset_version_id"]),
        dataset_version_digest=row["dataset_version_digest"],
        split_id=str(row["split_id"]), split_digest=row["split_digest"],
        partition=row["partition"], annotation_set_id=str(row["annotation_set_id"]),
        annotation_digest=row["annotation_digest"], state=row["state"],
        code_dirty=row["code_dirty"], metric_conventions=dict(row["metric_conventions"]),
        metric_registry_version=int(row["metric_registry_version"]),
        seed=int(row["seed"]), operating_thresholds=dict(row["operating_thresholds"]),
        aggregate_metrics=row["aggregate_metrics"],
        run_digest=row["run_digest"], invalidation_reason=row["invalidation_reason"],
    )


# =====================================================================================
# the run
# =====================================================================================
def start_run(conn: psycopg.Connection[Any], *, run_id: str) -> None:
    """PENDING -> RUNNING. The transition itself is guarded by the database trigger."""
    with tenant_tx(conn):
        conn.execute(
            "UPDATE evaluation_runs SET state = 'RUNNING', started_at = now() "
            "WHERE id = %s AND state = 'PENDING'",
            (run_id,),
        )


def record_cases(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    observations: Sequence[_metrics.CaseObservation],
    metrics: Sequence[str] = _metrics.SEGMENTATION_PER_CASE_METRICS,
) -> int:
    """Persist the per-case rows. `MOS-EVID-065`, `MOS-EVID-068`, `MOS-EVID-069`.

    The values come from `medos.evidence.metrics.case_metric_rows` and from nowhere else:
    this function does no arithmetic of its own, which is what "one evaluation
    implementation" means in practice. A second place that decided what a per-case Dice
    is would be a second implementation however thin it looked.

    Returns the number of metric rows written. Writing is refused outright once the run
    has finished -- the database trigger `evaluation_case_metrics_closed` enforces it, so
    the refusal reaches a caller that bypasses this function too.
    """
    rows: list[tuple[Any, ...]] = []
    scores: list[tuple[Any, ...]] = []
    for obs in observations:
        for row in _metrics.case_metric_rows(obs, metrics=metrics):
            rows.append(
                (
                    run_id, row.case_key, row.patient_key, row.study_instance_uid,
                    row.series_instance_uid, row.metric, row.value,
                    row.undefined_reason, row.eligible, row.gt_voxels, row.pred_voxels,
                    row.intersection_voxels, row.gt_volume_ml, row.pred_volume_ml,
                    Jsonb(dict(row.strata)),
                )
            )
        scores.append(
            (
                run_id, obs.case_key, obs.patient_key, obs.case_score, obs.case_label,
                Jsonb([dict(c) for c in obs.candidates]),
            )
        )

    with tenant_tx(conn):
        conn.cursor().executemany(
            """
            INSERT INTO evaluation_case_metrics (
              tenant_id, evaluation_run_id, case_key, patient_key, study_instance_uid,
              series_instance_uid, metric, value, undefined_reason, eligible, gt_voxels,
              pred_voxels, intersection_voxels, gt_volume_ml, pred_volume_ml, strata)
            VALUES (current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s)
            """,
            rows,
        )
        conn.cursor().executemany(
            """
            INSERT INTO evaluation_case_scores (
              tenant_id, evaluation_run_id, case_key, patient_key, case_score,
              case_label, candidates)
            VALUES (current_tenant_id(), %s, %s, %s, %s, %s, %s)
            """,
            scores,
        )
    return len(rows)


def load_case_metrics(
    conn: psycopg.Connection[Any], run_id: str
) -> list[dict[str, Any]]:
    """The persisted per-case rows, ordered deterministically.

    Ordered by `(case_key, metric)` and NOT by insertion id: MOS-EVID-058 requires the
    confidence interval to be reproducible from the persisted rows alone, and the
    bootstrap's patient order follows the row order (see `medos.evidence.ci._grouped`).
    An order that depends on how the rows happened to be inserted would make the interval
    reproducible only on the machine that wrote them.
    """
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT case_key, patient_key, study_instance_uid, series_instance_uid, "
            "metric, value, undefined_reason, eligible, gt_voxels, pred_voxels, "
            "intersection_voxels, gt_volume_ml, pred_volume_ml, strata "
            "FROM evaluation_case_metrics WHERE evaluation_run_id = %s "
            "ORDER BY case_key, metric",
            (run_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def load_case_scores(conn: psycopg.Connection[Any], run_id: str) -> list[dict[str, Any]]:
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT case_key, patient_key, case_score, case_label, candidates "
            "FROM evaluation_case_scores WHERE evaluation_run_id = %s ORDER BY case_key",
            (run_id,),
        ).fetchall()
    return [_row(r) for r in rows]


def _operating_point(run: RunRow) -> dict[str, Any] | None:
    """The single declared threshold, in the persisted form of MOS-EVID-055.

    None when the run declares no threshold, which is correct for a pure segmentation
    run: `validate_aggregates` then refuses any count-based aggregate, because a
    `sensitivity` with no operating point is not a measurement.

    More than one declared threshold also returns None, and `finish_run` turns that into
    a refusal rather than letting it drop the count-based family silently. This release
    models one operating point per run; attributing a `sensitivity` to one of several
    declared thresholds would be a guess, and a guess recorded as `operating_point` is
    exactly the fiction MOS-EVID-055 exists to prevent.
    """
    if len(run.operating_thresholds) != 1:
        return None
    name, spec = next(iter(run.operating_thresholds.items()))
    return {"name": name, "value": spec["value"], "selected_on": spec["selected_on"]}


def recompute_aggregates(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    subgroups: Sequence[_agg.Subgroup] = (),
) -> list[dict[str, Any]]:
    """MOS-EVID-066: the aggregate block, recomputed from the persisted rows alone.

    Reads the rows back out of the database rather than reusing anything held in memory.
    That is the point: `finish_run` computes the block this way too, so "recomputable
    from the per-case rows" is not a property the implementation has to remember to
    preserve -- it is the only way the block is ever produced.
    """
    run = get_run(conn, run_id)
    return _agg.aggregate_metrics(
        load_case_metrics(conn, run_id),
        conventions=run.metric_conventions,
        case_scores=load_case_scores(conn, run_id),
        subgroups=subgroups,
        operating_point=_operating_point(run),
    )


def verify_aggregates(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    subgroups: Sequence[_agg.Subgroup] = (),
    rel_tol: float = 1e-9,
) -> tuple[bool, list[str]]:
    """Chapter 7 acceptance check 10, as a function CI can call on every run.

    `subgroups` MUST be the ones the run was finished with. They are not stored on the
    run and deliberately so: a subgroup is a criterion's filter over `strata`
    (MOS-EVID-067), and the AcceptanceCriteria version the ValidationReport cites is the
    record of which filters were applied -- keeping a second copy on the run would give
    two answers to "what was gated on". Passing a different set does not silently pass:
    the strata present in one block and absent from the other are reported as
    differences.
    """
    run = get_run(conn, run_id)
    if run.aggregate_metrics is None:
        return False, ["the run has no aggregate block"]
    recomputed = recompute_aggregates(conn, run_id=run_id, subgroups=subgroups)
    return _agg.aggregates_agree(run.aggregate_metrics, recomputed, rel_tol=rel_tol)


def _annotated_patients(
    conn: psycopg.Connection[Any],
    *,
    annotation_set_id: str,
    store: ManifestStore | None,
) -> set[str]:
    """Which patients the bound AnnotationSet covers. `MOS-EVID-046`.

    TWO SOURCES, AND THE ORDER IS DELIBERATE.

    MOS-EVID-046 is written over the annotation MANIFEST -- "A case present in the split
    but absent from the annotation manifest MUST cause the run to fail". The `annotations`
    table is the manifest's queryable copy and is preferred when it is populated, because
    a join is cheaper than an object fetch and because it is what the rest of the platform
    reads.

    As of this release `medos.evidence.repo.freeze_annotation_set` writes the
    `annotation_sets` row, the `annotation_readers` rows and the manifest OBJECT, but no
    `annotations` rows -- unlike `seal_dataset_version`, which materialises
    `dataset_cases`. That gap is REPORTED rather than patched here: populating the table
    would require this module to invent an `annotation_provenance` for every entry, and
    `de_novo` is a claim about how a reference standard was produced (MOS-TRAIN-099), not
    a default one component may pick on another's behalf.

    So the manifest object is read instead when the table is empty, and its digest is
    verified on the way -- an object that no longer matches the frozen digest is not a
    weaker source of coverage, it is a different reference standard. When neither source
    is available the run is REFUSED: coverage that cannot be checked has not been checked,
    and MOS-EVID-046 exists precisely because the failure it prevents looks exactly like
    an honest smaller cohort.
    """
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT DISTINCT patient_key FROM annotations WHERE annotation_set_id = %s",
            (annotation_set_id,),
        ).fetchall()
        if rows:
            return {str(_row(r)["patient_key"]) for r in rows}
        located = _row(
            conn.execute(
                "SELECT manifest_bucket, manifest_object_key, manifest_digest "
                "FROM annotation_sets WHERE id = %s",
                (annotation_set_id,),
            ).fetchone()
        )

    if store is None:
        raise EvaluationRefused(
            (
                Refusal(
                    check_id="MOS-EVID-046",
                    code="reference_standard_coverage_unverifiable",
                    message="the AnnotationSet has no persisted per-case rows and no "
                            "manifest store was supplied, so the coverage of the "
                            "evaluated partition cannot be established; pass "
                            "`annotation_store=` or materialise the `annotations` rows",
                    detail={"annotation_set_id": annotation_set_id},
                ),
            )
        )
    data = store.get(located["manifest_bucket"], located["manifest_object_key"])
    if _digest.sha256_of(data) != located["manifest_digest"]:
        raise EvaluationRefused(
            (
                Refusal(
                    check_id="MOS-EVID-007", code="annotation_manifest_digest_mismatch",
                    message="the annotation manifest object does not match the frozen "
                            "digest; it is a different reference standard, not a "
                            "damaged copy of this one",
                    observed=_digest.sha256_of(data),
                    bound=located["manifest_digest"],
                ),
            )
        )
    out: set[str] = set()
    for line in data.decode("utf-8").splitlines():
        if line:
            out.add(str(json.loads(line)["patient_key"]))
    return out


def finish_run(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    subgroups: Sequence[_agg.Subgroup] = (),
    annotation_store: ManifestStore | None = None,
    per_case_bucket: str | None = None,
    per_case_object_key: str | None = None,
) -> FinishResult:
    """RUNNING -> SUCCEEDED, with the aggregate block and the run digest.

    Every blocking gate in one place, because they all share one property: each of them,
    if skipped, produces a run that LOOKS like evidence.

      MOS-EVID-062  a dirty working tree. Refused, not warned.
      MOS-EVID-065  no per-case rows. An aggregate with nothing behind it is the exact
                    shape this release exists to make impossible.
      coverage      every patient in the evaluated partition was measured, and every
                    measured patient is in it. Chapter 7 acceptance check 6: "Assert the
                    run fails rather than SHRINKING n when an entry is removed." A run
                    that quietly measures 38 of 40 cases reports a real number about a
                    cohort nobody chose.
      leakage       no measured patient belongs to the split's `train` partition
                    (chapter 12 acceptance check 15).
      MOS-EVID-054  every metric id resolves in the registry recorded on the run.
      MOS-EVID-055  every threshold metric carries its operating point.

    `run_digest` is the digest over the aggregate block (MOS-EVID-061) and nothing else.
    """
    run = get_run(conn, run_id)
    refusals: list[Refusal] = []
    gates: dict[str, str] = {}

    if run.state != "RUNNING":
        raise EvaluationRefused(
            (
                Refusal(
                    check_id="MOS-EVID-014", code="run_not_running",
                    message="only a RUNNING evaluation can be finished; a SUCCEEDED run "
                            "is sealed and an INVALIDATED one is terminal",
                    observed=run.state, bound="RUNNING",
                ),
            )
        )
    if run.code_dirty:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-062", code="dirty_working_tree",
                message="an evaluation run from an uncommitted working tree is not "
                        "evidence; commit the harness and measure again",
                observed=True, bound=False,
            )
        )
    gates["MOS-EVID-062"] = "fail" if run.code_dirty else "pass"

    case_rows = load_case_metrics(conn, run_id)
    if not case_rows:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-065", code="aggregates_without_per_case_rows",
                message="per-case metrics MUST be persisted; aggregates alone MUST NOT "
                        "be stored without them",
                observed=0, bound=1,
            )
        )
        raise EvaluationRefused(refusals)
    gates["MOS-EVID-065"] = "pass"

    with tenant_tx(conn):
        partition_patients = {
            r["patient_key"]
            for r in conn.execute(
                "SELECT patient_key FROM dataset_split_members "
                "WHERE split_id = %s AND partition = %s",
                (run.split_id, run.partition),
            ).fetchall()
        }
        train_patients = {
            r["patient_key"]
            for r in conn.execute(
                "SELECT patient_key FROM dataset_split_members "
                "WHERE split_id = %s AND partition = 'train'",
                (run.split_id,),
            ).fetchall()
        }
    measured_for_coverage = {str(r["patient_key"]) for r in case_rows}
    try:
        annotated_patients = _annotated_patients(
            conn, annotation_set_id=run.annotation_set_id, store=annotation_store
        )
    except EvaluationRefused as exc:
        # An unverifiable or mismatched reference standard is one more refusal, not a
        # short circuit: a caller fixing a run wants every reason at once, and a dirty
        # working tree found three lines above MUST NOT be hidden by it.
        refusals.extend(exc.refusals)
        annotated_patients = partition_patients | measured_for_coverage

    measured = {str(r["patient_key"]) for r in case_rows}
    unmeasured = sorted(partition_patients - measured)
    foreign = sorted(measured - partition_patients)
    unannotated = sorted(partition_patients - annotated_patients)
    leaked = sorted(measured & train_patients)

    if unmeasured:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-065", code="partition_not_fully_measured",
                message=f"{len(unmeasured)} patient(s) in the {run.partition!r} "
                        "partition have no per-case row; a run reports on the whole "
                        "partition or it does not report",
                observed=len(measured), bound=len(partition_patients),
                detail={"unmeasured_patient_keys": unmeasured[:20]},
            )
        )
    if foreign:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-029", code="case_outside_the_partition",
                message="a case was measured whose patient is not in the evaluated "
                        "partition",
                observed=len(foreign), bound=0,
                detail={"foreign_patient_keys": foreign[:20]},
            )
        )
    if unannotated:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-046", code="reference_standard_incomplete",
                message="every patient in the evaluated partition MUST have an entry in "
                        "the bound AnnotationSet; the run fails rather than shrinking n",
                observed=len(partition_patients) - len(unannotated),
                bound=len(partition_patients),
                detail={"unannotated_patient_keys": unannotated[:20]},
            )
        )
    if leaked:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-034", code="train_patient_in_evaluation",
                message="a patient in the split's train partition was measured by a run "
                        "reporting on another partition",
                observed=len(leaked), bound=0,
                detail={"patient_keys": leaked[:20]},
            )
        )
    gates["coverage"] = "fail" if (unmeasured or foreign or unannotated) else "pass"
    gates["leakage"] = "fail" if leaked else "pass"

    scores = load_case_scores(conn, run_id)
    scored = [s for s in scores if s["case_score"] is not None]
    if scored and len(run.operating_thresholds) > 1:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-055", code="ambiguous_operating_point",
                message="this run declares more than one operating threshold and "
                        "persists per-case scores, so a count-based aggregate cannot be "
                        "attributed to one point; measure once per point",
                observed=sorted(run.operating_thresholds), bound=1,
            )
        )

    if refusals:
        raise EvaluationRefused(refusals)

    try:
        aggregates = _agg.aggregate_metrics(
            case_rows,
            conventions=run.metric_conventions,
            case_scores=scores,
            subgroups=subgroups,
            operating_point=_operating_point(run),
        )
    except (_metrics.UnknownMetric, _agg.AggregationError) as exc:
        raise EvaluationRefused(
            (
                Refusal(
                    check_id="MOS-EVID-054", code="aggregate_block_invalid",
                    message=str(exc),
                ),
            )
        ) from exc
    gates["MOS-EVID-054"] = "pass"
    gates["MOS-EVID-056"] = "pass"

    # MOS-EVID-061: "`run_digest` | digest over `aggregate_metrics`". Over the canonical
    # bytes of the block and nothing else, so a reader holding an exported report can
    # recompute it without the binding, the database or this repository.
    run_digest = _digest.sha256_of(canonical_bytes(aggregates))

    with tenant_tx(conn):
        conn.execute(
            "UPDATE evaluation_runs SET state = 'SUCCEEDED', finished_at = now(), "
            "aggregate_metrics = %s, run_digest = %s, per_case_bucket = %s, "
            "per_case_object_key = %s WHERE id = %s",
            (Jsonb(aggregates), run_digest, per_case_bucket, per_case_object_key,
             run_id),
        )

    return FinishResult(
        run_id=run_id, public_id=run.public_id, run_digest=run_digest,
        aggregates=aggregates,
        case_count=len({str(r["case_key"]) for r in case_rows}),
        patient_count=len(measured), gates=gates,
    )


def fail_run(conn: psycopg.Connection[Any], *, run_id: str, reason: str) -> None:
    """A run that could not produce a number. It keeps its rows and its binding."""
    if not reason:
        raise ValueError("a FAILED run MUST carry a reason")
    with tenant_tx(conn):
        conn.execute(
            "UPDATE evaluation_runs SET state = 'FAILED', failure_reason = %s, "
            "finished_at = now() WHERE id = %s",
            (reason, run_id),
        )


def invalidate_run(conn: psycopg.Connection[Any], *, run_id: str, reason: str) -> None:
    """`MOS-EVID-014` / `MOS-EVID-070`: marked, never edited and never deleted.

    The other path into this state is the database trigger of 0007 section 8, which fires
    when a DatasetVersion is marked DEFECTIVE. Both write the same column with a reason,
    because a run in INVALIDATED with no reason is indistinguishable from a bug.
    """
    if not reason:
        raise ValueError("MOS-EVID-014: an INVALIDATED run MUST carry a reason")
    with tenant_tx(conn):
        conn.execute(
            "UPDATE evaluation_runs SET state = 'INVALIDATED', invalidation_reason = %s "
            "WHERE id = %s",
            (reason, run_id),
        )
