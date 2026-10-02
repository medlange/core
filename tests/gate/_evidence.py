# SPDX-License-Identifier: Apache-2.0
"""The release-0.2.0 gate's world: a throwaway database, a sealed cohort, a frozen split.

WHY THIS IS NOT `tests/gate/conftest.py`'s WORLD
------------------------------------------------
The 0.1.0 gate's subject is the running `medos/deploy/compose` deployment, because `MOS-REL-001`
makes a release a set of container images and every one of those five checks is a claim
about what the images do at request time.

The 0.2.0 half of the gate cannot have that subject, and the reason is in the specification
rather than in convenience. `MOS-EVID-006`: "The evidence plane is not a sixth
architectural plane. It is a set of entities, a gate function and a report format, hosted
in the control plane. No component of the serving path depends on it at request time except
the gate lookup performed at deployment time (7.9) and the envelope check performed at
triage time (7.10)." There is no HTTP surface to drive, no queue to enqueue on, and no
worker that runs an `EvaluationRun`. The honest subject for `deployment-gate`,
`non-inferiority`, `per-case-metrics` and `leakage-check` is therefore the SCHEMA plus the
modules that write it -- and the shipped migrations are part of the release exactly as the
images are.

So this module creates a brand-new database on the deployment's own Postgres server and
applies `schema.sql` plus every migration in `medos/medos/db/migrations/` to it. That is a
stronger claim than running against the already-migrated deployment database, not a weaker
one: it proves 0006 through 0011 apply from empty, in order, on the server the release
runs on.

IT IS ALSO THE ONLY SAFE CHOICE ON THIS PROJECT. Two agents running pytest against the
shared deployment database truncated each other's tables and produced a spurious red gate.
A release gate that can be turned red by a colleague's unrelated test run is not a gate.
Nothing in this module truncates a table it did not create.

Spec: docs/spec/15-delivery.md section 15.1.2 (the 0.2.0 gate row) and section 15.2.5;
docs/spec/07-evidence.md sections 7.2-7.9; docs/spec/17-training-pipeline.md
MOS-TRAIN-088, MOS-TRAIN-115 to MOS-TRAIN-118.
"""

from __future__ import annotations

import random
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from medos.db.tenancy import DEFAULT_TENANT_ID
from medos.evidence import acceptance as ev_acc
from medos.evidence import criteria as ev_crit
from medos.evidence import deployment as ev_dep
from medos.evidence import digest as ev_digest
from medos.evidence import evaluation as ev_run
from medos.evidence import gate as ev_gate
from medos.evidence import metrics as ev_metrics
from medos.evidence import repo as ev_repo
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.metrics import CaseMetricRow
from medos.evidence.store import InMemoryManifestStore

BUCKET = "medos-evidence"
SALT = b"release-0-2-0-gate-salt-not-a-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"
CAPABILITY = "pleural_effusion"

#: `annotation_sets.label_definition_id` is a `capability_concepts` uuid (MOS-EVID-143),
#: and that registry is chapter 6's -- not shipped before 0.3.0. A derived uuid keeps the
#: column honest about its type without inventing a registry row.
LABEL_DEFINITION_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "medicalos:capability_concepts/effusion/v1")
)

#: A fixed clock, so a gate run in December and a gate run in June evaluate the same
#: validity windows. `MOS-EVID-115`'s expiry check is a real branch of `evaluate_gate`.
NOW = datetime(2026, 6, 1, tzinfo=UTC)

CANDIDATE = ev_gate.SubjectRef("model_version", "pulmoai.effusion", "2.2.0")
INCUMBENT = ev_gate.SubjectRef("model_version", "pulmoai.effusion", "2.1.0")

#: MOS-EVID-067's minimum plus the capability's declared clinical stratum. Section 7.8.2
#: gates `pleural_effusion` on `reference_volume_ml`, so the criteria validator has to be
#: told it is persisted or every worked-example criterion is refused.
KNOWN_STRATA = (*ev_crit.REQUIRED_STRATUM_FIELDS, "reference_volume_ml")

#: Migration 0008's four tables. The ONLY tables this package truncates, and only in the
#: throwaway database `tests/gate/conftest.py`'s `evidence_dsn` fixture created itself.
GATE_TABLES = (
    "deployment_gate_decisions",
    "deployments",
    "tenant_acceptance_bindings",
    "acceptance_criteria",
)


# ======================================================================================
# The cohort. A sealed DatasetVersion, a frozen patient-level split, a frozen reference.
# ======================================================================================
def _uid(nonce: int, *parts: int) -> str:
    return f"1.2.826.0.1.3680043.10.8888.{nonce}." + ".".join(str(p) for p in parts)


def series_record(patient: int, *, site: int, nonce: int) -> SeriesRecord:
    """One series. `nonce` makes every cohort a genuinely DIFFERENT cohort.

    `seal_dataset_version` is idempotent on `manifest_digest` (MOS-TRAIN-209: detect the
    collision and REUSE the existing DatasetVersion), so two cohorts built from identical
    records are one row -- and the second `freeze_split` then collides on
    `UNIQUE (dataset_version_id, name)`. That is the seal working correctly; a fixture has
    to stop pretending two identical corpora are two corpora.
    """
    return SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/GATE", f"P{nonce}-{patient:04d}"),
        study_instance_uid=_uid(nonce, patient, 1),
        series_instance_uid=_uid(nonce, patient, 1, 1),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(nonce, patient, 1, 1, i) for i in range(3)),
        series_pixel_digest="sha256:"
        + ev_digest.sha256_of(f"gate/{nonce}/{patient}".encode()).split(":")[-1],
        acquisition=Acquisition(
            slice_thickness_mm=1.25 if site else 2.5,
            pixel_spacing_mm=(0.703125, 0.703125),
            convolution_kernel="B30f" if site else "STANDARD",
            convolution_kernel_class="soft" if site else "standard",
            manufacturer="SIEMENS" if site else "GE MEDICAL SYSTEMS",
            manufacturer_model_name="Sensation 16" if site else "Discovery CT750 HD",
            kvp=120.0,
            contrast_phase="non_contrast",
            z_coverage_mm=332.5,
            image_type=("ORIGINAL", "PRIMARY", "AXIAL"),
            patient_age_years=60 + patient % 20,
            patient_sex="M" if patient % 2 else "F",
            body_part_examined="CHEST",
        ),
        institution_key=ev_digest.institution_key(SALT, f"SITE-{site}"),
        study_year=2022 + site,
        accession_number_hash=ev_digest.accession_number_hash(
            SALT, f"ACC{nonce}-{patient:04d}"
        ),
    )


def annotation_entries(records: Sequence[SeriesRecord]) -> list[dict[str, Any]]:
    return [
        {
            "case_key": r.study_instance_uid,
            "patient_key": r.patient_key,
            "study_instance_uid": r.study_instance_uid,
            "series_instance_uid": r.series_instance_uid,
            "reference_kind": "mask",
            "reference_bucket": BUCKET,
            "reference_object_key": f"ref/{r.series_instance_uid}",
            "reference_digest": "sha256:" + "d" * 64,
            "reference_volume_ml": 150.0,
            "per_reader": [
                {
                    "reader_id": "rdr_1",
                    "bucket": BUCKET,
                    "object_key": f"ref/{r.series_instance_uid}",
                    "digest": "sha256:" + "d" * 64,
                    "volume_ml": 150.0,
                }
            ],
            "annotation_provenance": "de_novo",
        }
        for r in records
    ]


READERS = [
    {
        "reader_id": "rdr_1",
        "role": "radiologist",
        "years_experience": 12,
        "board_certified": True,
        "specialty": "thoracic",
        "tool": "3D Slicer 5.6",
        "instructions_uri": "https://example.invalid/instructions/v1",
    }
]


class Cohort:
    """A sealed acceptance cohort, built through the 0006 repository, not by INSERT.

    40 patients, which is the release brief's number -- "fails on noise roughly half the
    time on a 40-case split" -- and also above `MOS-EVID-026`'s floor of 30, below which
    `medos.evidence.repo` refuses to seal an `acceptance` cohort at all because "no
    criterion in 7.8 can return anything but INDETERMINATE".

    Two sites, two scanners, two kernel classes: the minimum that passes the MOS-TRAIN-088
    corpus stratification checks the seal runs (C1, C2, C3, C7).
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        store: InMemoryManifestStore,
        *,
        n_patients: int = 40,
        split_name: str = "acceptance",
        assignments: Sequence[tuple[str, str]] | None = None,
    ) -> None:
        nonce = secrets.randbelow(10**9)
        records = [series_record(p, site=p % 2, nonce=nonce) for p in range(n_patients)]
        self.records = records
        self.store = store

        self.dataset = ev_repo.create_dataset(
            conn,
            slug=f"gate-cohort-{secrets.token_hex(4)}",
            display_name="release 0.2.0 gate cohort",
            purpose="acceptance",  # MOS-EVID-082: never training or tuning
            custodian="TCIA/GATE",
            created_by=OPERATOR,
        )
        self.version = ev_repo.seal_dataset_version(
            conn,
            dataset_id=self.dataset.id,
            records=records,
            store=store,
            bucket=BUCKET,
            sealed_by=OPERATOR,
            deidentification_status="public_deidentified",
            deid_policy_id="ps315-basic/v4",
            uid_mapping_table_id="uidmap/v1",
        )
        self.assignments = (
            list(assignments)
            if assignments is not None
            else [(r.patient_key, "test") for r in records]
        )
        self.split = ev_repo.freeze_split(
            conn,
            dataset_version_id=self.version.id,
            name=split_name,
            assignments=self.assignments,
            store=store,
            bucket=BUCKET,
            frozen_by=OPERATOR,
            assignment_method=(
                "every patient in test; this cohort exists to be gated on"
                if assignments is None
                else "declared by the gate check under test"
            ),
            records=records,
        )
        self.annotations = ev_repo.freeze_annotation_set(
            conn,
            dataset_version_id=self.version.id,
            name="reference",
            capability_id=CAPABILITY,
            label_definition_id=LABEL_DEFINITION_ID,
            annotation_type="mask",
            consensus_rule="single_reader",
            readers=READERS,
            entries=annotation_entries(records),
            store=store,
            bucket=BUCKET,
            frozen_by=OPERATOR,
            reference_of_record=True,
        )


def evaluation_run_id(
    conn: psycopg.Connection[Any], c: Cohort, *, model_version: str
) -> str:
    """A real `evaluation_runs` row, so `deployments.acceptance_run_id` points at one."""
    run = ev_run.create_run(
        conn,
        kind="site_acceptance",
        capability_id=CAPABILITY,
        model_version_id=str(uuid.uuid5(uuid.NAMESPACE_URL, model_version)),
        dataset_version_id=c.version.id,
        split_id=c.split.id,
        partition="test",
        annotation_set_id=c.annotations.id,
        code_commit="7e41b2c8a95d3f06b18e4c7a2d905f3b6c81e024",
        image_digest="sha256:" + "5" * 64,
        runner="release-0.2.0-gate",
        fp_volume_threshold_ml=10.0,
        preprocessing_spec_id="ct-lung/v3",
        preprocessing_spec_version=3,
        preprocessing_spec_digest="sha256:" + "9" * 64,
        operating_thresholds={
            "effusion_probability": {"value": 0.50, "selected_on": "tune"}
        },
    )
    return run.id


# ======================================================================================
# The criteria document. Section 7.8.2's worked example, every bound stated.
# ======================================================================================
def criteria_document(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "medicalos.io/v1",
        "kind": "AcceptanceCriteria",
        "metadata": {
            "capability_id": CAPABILITY,
            "version": 3,
            "effective_from": "2026-04-01T00:00:00Z",
            "authored_by": "clinical-council@medicalos.example",
            "supersedes": 2,
        },
        "spec": {
            "combine": "all_of",
            "fp_volume_threshold_ml": 10.0,
            "margin_rationale": (
                "0.02 mean-of-per-case overlap is the smallest loss the clinical council "
                "judged tolerable on a positive effusion; 0.05 on small effusions because "
                "they already sit near the detection floor; 0.20 as the per-case collapse "
                "threshold because a case falling that far has stopped segmenting rather "
                "than segmenting worse."
            ),
            "case_definition": {
                "positive_if": {"reference_volume_ml": {"op": ">=", "value": 10.0}}
            },
            "strata": [
                {"id": "all", "selector": {}},
                {
                    "id": "gt_positive",
                    "selector": {"reference_volume_ml": {"op": ">=", "value": 10.0}},
                },
                {
                    "id": "small_effusion",
                    "selector": {"reference_volume_ml": {"op": "<", "value": 100.0}},
                },
            ],
            "absolute": [
                {
                    "id": "dice_positive",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "bound": "ci_lower_95",
                    "op": ">=",
                    "value": 0.60,
                    "min_cases": 20,
                    "min_patients": 20,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    "id": "fp_on_negatives",
                    "metric": "empty_gt_false_positive_rate",
                    "stratum": "all",
                    "bound": "ci_upper_95",
                    "op": "<=",
                    "value": 0.50,
                    "min_cases": 5,
                    "min_patients": 5,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
            ],
            "regression": [
                {
                    "id": "ni_dice_positive",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "margin": 0.02,
                    "bound": "ci_lower_95",
                    "min_cases": 20,
                    "min_patients": 20,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    "id": "ni_dice_small",
                    "metric": "dice_mean_per_case",
                    "stratum": "small_effusion",
                    "margin": 0.05,
                    "bound": "ci_lower_95",
                    "min_cases": 6,
                    "min_patients": 6,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    "id": "no_catastrophic",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "margin": 0.20,
                    "bound": "catastrophic_count",
                    "op": "<=",
                    "value": 2,
                    "min_cases": 20,
                    "min_patients": 20,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
            ],
        },
    }
    doc["spec"].update(over.pop("spec", {}))
    doc["metadata"].update(over.pop("metadata", {}))
    return doc


DOC = criteria_document()

#: Every criterion id the document above declares. `MOS-EVID-113` requires one entry per
#: criterion in every decision record, and the gate checks assert the SET rather than a
#: count so that a criterion silently dropped from the document is a failure here.
CRITERION_IDS = frozenset(
    {"dice_positive", "fp_on_negatives", "ni_dice_positive", "ni_dice_small",
     "no_catastrophic"}
)


# ======================================================================================
# Per-case rows. MOS-EVID-065's point is that the gate needs only these.
# ======================================================================================
def case_row(
    index: int,
    value: float | None,
    reference_volume_ml: float,
    *,
    eligible: bool = True,
    pred_volume_ml: float | None = None,
    patient: int | None = None,
) -> CaseMetricRow:
    patient = index if patient is None else patient
    gt_voxels = 0 if reference_volume_ml <= 0.0 else 1000
    return CaseMetricRow(
        case_key=f"case_{index:04d}",
        patient_key=f"pt_{patient:04d}",
        study_instance_uid=f"1.2.826.0.1.3680043.10.9999.{index}",
        series_instance_uid=f"1.2.826.0.1.3680043.10.9999.{index}.1",
        metric="dice_mean_per_case",
        value=value,
        undefined_reason=None if eligible else "empty_ground_truth",
        eligible=eligible,
        gt_voxels=gt_voxels,
        pred_voxels=1000 if value else 0,
        intersection_voxels=int(1000 * (value or 0.0)),
        gt_volume_ml=reference_volume_ml,
        pred_volume_ml=(reference_volume_ml if pred_volume_ml is None else pred_volume_ml),
        strata={
            "reference_volume_ml": reference_volume_ml,
            "slice_thickness_mm": 1.25,
            "pixel_spacing_mm_max": 0.703125,
            "convolution_kernel_class": "soft",
            "manufacturer": "SIEMENS",
            "contrast_phase": "non_contrast",
            "patient_age_years": 60 + (index % 20),
            "patient_sex": "M" if index % 2 else "F",
        },
    )


def forty_case_split(
    n_positive: int = 30, n_small: int = 8, n_empty: int = 10, *, seed: int = 20260101
) -> list[CaseMetricRow]:
    """A 40-patient cohort, `n_small` of whose positives are small effusions.

    40 is the release brief's number, and the size at which the naive `new.dice < old.dice`
    comparison's defect is loudest: on a split this small the two arms of a rebuilt model
    differ by more than the noise floor roughly half the time.
    """
    rng = random.Random(seed)
    rows: list[CaseMetricRow] = []
    for i in range(n_positive):
        small = i < n_small
        rows.append(case_row(i, 0.80 + rng.uniform(-0.03, 0.03), 40.0 if small else 150.0))
    for j in range(n_empty):
        rows.append(
            case_row(n_positive + j, None, 0.0, eligible=False, pred_volume_ml=1.0)
        )
    return rows


def case_rows_for(
    records: Sequence[SeriesRecord],
    *,
    n_small: int = 8,
    n_empty: int = 10,
    seed: int = 20260101,
) -> list[CaseMetricRow]:
    """One per-case row per record of a REAL frozen cohort. The 40-case split, for real.

    `forty_case_split` above invents its own case and patient keys, which is right for the
    simulation that measures the naive comparison's false-block rate and wrong for a gate
    arm: §15.1.2 says "on a 40-case split", and a split is a frozen, sealed,
    leakage-checked object with patient keys in it. Taking the keys from the cohort's own
    records means the rows the gate reads and the split the binding names are the same
    forty patients, and the clustered bootstrap of `MOS-EVID-057` clusters on the keys that
    were actually frozen.

    The composition is the worked example's: `n_small` small effusions, `n_empty`
    reference-empty cases so that `empty_gt_false_positive_rate` has a denominator
    (MOS-EVID-052), and the rest large positives.
    """
    rng = random.Random(seed)
    ordered = sorted(records, key=lambda r: r.study_instance_uid)
    n_positive = len(ordered) - n_empty
    rows: list[CaseMetricRow] = []
    for i, record in enumerate(ordered):
        empty = i >= n_positive
        reference_volume_ml = 0.0 if empty else (40.0 if i < n_small else 150.0)
        value = None if empty else 0.80 + rng.uniform(-0.03, 0.03)
        rows.append(
            replace(
                case_row(
                    i,
                    value,
                    reference_volume_ml,
                    eligible=not empty,
                    pred_volume_ml=1.0 if empty else None,
                ),
                case_key=record.study_instance_uid,
                patient_key=record.patient_key,
                study_instance_uid=record.study_instance_uid,
                series_instance_uid=record.series_instance_uid,
            )
        )
    return rows


def perturb(rows: Sequence[CaseMetricRow], fn: Any) -> tuple[CaseMetricRow, ...]:
    """Apply `fn(row) -> new value` to every eligible row, keeping the counts consistent."""
    out = []
    for r in rows:
        if r.value is None:
            out.append(r)
            continue
        new_value = fn(r)
        out.append(
            replace(
                r,
                value=new_value,
                intersection_voxels=int(1000 * max(0.0, min(1.0, new_value))),
            )
        )
    return tuple(out)


def stratum_rows(
    rows: Sequence[CaseMetricRow], stratum_id: str, doc: dict[str, Any] | None = None
) -> list[CaseMetricRow]:
    """The rows a named stratum of `doc` selects. Used to state what a collapse cost."""
    selector = ev_crit.strata_of(doc or DOC)[stratum_id]
    return [r for r in rows if ev_crit.selector_matches(r.strata, selector)]


def mean_value(rows: Sequence[CaseMetricRow]) -> float:
    values = [r.value for r in rows if r.eligible and r.value is not None]
    return sum(values) / len(values)


# ======================================================================================
# Views. What `evaluate_gate` reads.
# ======================================================================================
def run_view(rows: Sequence[CaseMetricRow], **over: Any) -> ev_gate.RunView:
    defaults: dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "dataset_version_id": "dsv-fixture",
        "dataset_version_digest": "sha256:" + "a" * 64,
        "split_id": "spl-fixture",
        "split_digest": "sha256:" + "b" * 64,
        "partition": "test",
        "annotation_set_id": "ann-fixture",
        "annotation_digest": "sha256:" + "c" * 64,
        "case_metrics": tuple(rows),
        "operating_thresholds": {
            "effusion_probability": {"value": 0.50, "selected_on": "tune"}
        },
        "state": "SUCCEEDED",
    }
    defaults.update(over)
    return ev_gate.RunView(**defaults)


def report_view(
    run: ev_gate.RunView, subject: ev_gate.SubjectRef, **over: Any
) -> ev_gate.ReportView:
    defaults: dict[str, Any] = {
        "report_id": str(uuid.uuid4()),
        "subject": subject,
        "capability_id": CAPABILITY,
        "criteria_version": 3,
        "run": run,
        "signature_verified": True,
        "valid_until": NOW + timedelta(days=200),
        "status": "ACTIVE",
        "reference_of_record": True,
    }
    defaults.update(over)
    return ev_gate.ReportView(**defaults)


def binding_for(
    run: ev_gate.RunView, subject: ev_gate.SubjectRef = CANDIDATE
) -> ev_gate.GateBinding:
    return ev_gate.GateBinding(
        tenant_id=DEFAULT_TENANT_ID,
        capability_id=CAPABILITY,
        criteria_version=3,
        dataset_version_id=run.dataset_version_id,
        split_id=run.split_id,
        partition=run.partition,
        annotation_set_id=run.annotation_set_id,
        candidate_subject=subject,
    )


def paired_views(
    incumbent_rows: Sequence[CaseMetricRow],
    candidate_rows: Sequence[CaseMetricRow],
    *,
    cohort: Cohort | None = None,
    incumbent_run_id: str | None = None,
    candidate_run_id: str | None = None,
    incumbent_report_id: str | None = None,
) -> tuple[ev_gate.ReportView, ev_gate.ReportView, ev_gate.GateBinding]:
    """Two reports over the SAME frozen cohort, and the binding that pairs them.

    `MOS-EVID-086` makes the pairing a binding on the four cohort digests rather than on
    the run ids: after a restore-from-backup "which row" and "which content" stop being the
    same question. When a `Cohort` is supplied the digests are the real sealed ones, so an
    unpaired comparison is impossible to construct by accident.
    """
    cohort_kwargs: dict[str, Any] = {}
    if cohort is not None:
        cohort_kwargs = {
            "dataset_version_id": cohort.version.id,
            "dataset_version_digest": cohort.version.manifest_digest,
            "split_id": cohort.split.id,
            "split_digest": cohort.split.split_digest,
            "annotation_set_id": cohort.annotations.id,
            "annotation_digest": cohort.annotations.annotation_digest,
        }
    incumbent_run = run_view(
        incumbent_rows,
        **({"run_id": incumbent_run_id} if incumbent_run_id else {}),
        **cohort_kwargs,
    )
    candidate_run = run_view(
        candidate_rows,
        **({"run_id": candidate_run_id} if candidate_run_id else {}),
        **cohort_kwargs,
    )
    incumbent = report_view(
        incumbent_run,
        INCUMBENT,
        **({"report_id": incumbent_report_id} if incumbent_report_id else {}),
    )
    candidate = report_view(candidate_run, CANDIDATE)
    return incumbent, candidate, binding_for(candidate_run)


# ======================================================================================
# The slot: an incumbent live in clinical use, a candidate standing by.
# ======================================================================================
def publish_and_bind(conn: psycopg.Connection[Any], c: Cohort) -> None:
    ev_acc.publish_criteria(
        conn,
        document=DOC,
        authored_by="clinical-council@medicalos.example",
        tenant_id=DEFAULT_TENANT_ID,
        known_strata_fields=KNOWN_STRATA,
    )
    ev_acc.bind_tenant_acceptance(
        conn,
        tenant_id=DEFAULT_TENANT_ID,
        capability_id=CAPABILITY,
        criteria_version=3,
        dataset_version_id=c.version.id,
        split_id=c.split.id,
        annotation_set_id=c.annotations.id,
        bound_by=OPERATOR,
    )


def live_slot(conn: psycopg.Connection[Any], c: Cohort) -> dict[str, Any]:
    """An incumbent ACTIVE/SERVING in `clinical` mode, and a STANDBY candidate."""
    publish_and_bind(conn, c)

    incumbent_run = evaluation_run_id(conn, c, model_version="2.1.0")
    candidate_run = evaluation_run_id(conn, c, model_version="2.2.0")

    live = ev_dep.create_deployment(
        conn,
        tenant_id=DEFAULT_TENANT_ID,
        environment="production",
        capability_id=CAPABILITY,
        subject=INCUMBENT,
        created_by=OPERATOR,
        role="ACTIVE",
        state="SERVING",
        verification_ref="ver_incumbent",
    )
    # Put the incumbent in clinical mode the way the database requires: an approver, a run
    # and a report. MOS-STORE-261's clinical gate is checked on every write.
    incumbent_report_id = uuid.uuid4()
    conn.execute(
        "UPDATE deployments SET clinical_use_mode = 'clinical', approved_by = %s, "
        "approved_at = now(), acceptance_run_id = %s, validation_report_id = %s "
        "WHERE id = %s",
        (uuid.UUID(OPERATOR), incumbent_run, incumbent_report_id, uuid.UUID(live.id)),
    )
    conn.commit()

    candidate = ev_dep.create_deployment(
        conn,
        tenant_id=DEFAULT_TENANT_ID,
        environment="production",
        capability_id=CAPABILITY,
        subject=CANDIDATE,
        created_by=OPERATOR,
        role="STANDBY",
        state="SERVING",
        verification_ref="ver_candidate",
    )
    conn.commit()
    return {
        "live": live,
        "candidate": candidate,
        "incumbent_run": incumbent_run,
        "candidate_run": candidate_run,
        "incumbent_report_id": str(incumbent_report_id),
    }


__all__ = [
    "BUCKET",
    "CANDIDATE",
    "CAPABILITY",
    "CRITERION_IDS",
    "DOC",
    "GATE_TABLES",
    "INCUMBENT",
    "KNOWN_STRATA",
    "NOW",
    "OPERATOR",
    "READERS",
    "SALT",
    "Cohort",
    "annotation_entries",
    "binding_for",
    "case_row",
    "case_rows_for",
    "criteria_document",
    "evaluation_run_id",
    "forty_case_split",
    "live_slot",
    "mean_value",
    "paired_views",
    "perturb",
    "publish_and_bind",
    "report_view",
    "run_view",
    "series_record",
    "stratum_rows",
    "ev_acc",
    "ev_crit",
    "ev_dep",
    "ev_digest",
    "ev_gate",
    "ev_metrics",
    "ev_repo",
    "ev_run",
]
