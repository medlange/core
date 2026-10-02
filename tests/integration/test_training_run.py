# SPDX-License-Identifier: Apache-2.0
"""`TrainingRun`, the Orchestrator port, MONAI Bundle, `ConversionRun`, three-act promotion.

docs/spec/15-delivery.md section 15.2.6 (weeks 10-13, tag 0.3.0) and chapter 17
`MOS-TRAIN-190`, which places in THIS release: "`TrainingRun`, `Orchestrator` port, one
driver"; "MONAI Bundle as the native-mode artifact source form"; "Candidate registration at
`REGISTERED` -> `VALIDATING`"; "`ConversionRun`, E1/E2/E3, tolerances";
"Auto-configuration as the default backend, the fingerprint freeze"; "`ConfigurationSearch`,
search provenance, budget bound, single nomination"; "Approval dossier, the three-act
promotion, rollback window".

WHAT THIS FILE IS TRYING TO CATCH, and why each one is worth a test rather than a review

  1. A CANDIDATE NOBODY CAN RE-ENTER. `MOS-TRAIN-124` binds nine inputs and
     `MOS-TRAIN-125` blocks `SUCCEEDED` on a dirty tree, because "a candidate that cannot
     be re-entered cannot be diagnosed when it later fails in a subgroup nobody looked at".
     Asserted as DATABASE behaviour as well as library behaviour: a binding enforced only
     in the repository is one `psql` session away from nothing.
  2. A LEAK THAT ONLY BLOCKS THE REPORT. This is the 0.3.0 gate check
     `leakage-blocks-training` (`MOS-TRAIN-193`). A `MOS-EVID-036` waiver is "a statement
     about what a report may claim; it is not a licence to fit on the data", and
     `test_a_waiver_does_not_license_fitting_on_the_data` is what makes that true rather
     than stated.
  3. THE PLAN THAT IS RE-DERIVED AT SERVE TIME. 17.7.6: "a materially different transform,
     executed by a byte-identical container, against a byte-identical weights digest, with
     every structural check passing ... there is no version to compare, no digest that
     differs, and no error anywhere."
  4. THE CHECKPOINT MEASURED INSTEAD OF THE PLAN. The 0.3.0 gate check
     `served-plan-equivalence`. `MOS-TRAIN-153`: "The run must exercise the plan actually
     served, not the checkpoint that was trained -- this sentence is the whole content of
     the requirement and is the single thing most often got wrong."
  5. AN AUTOMATED PATH FROM A RUN TO A SERVING DEPLOYMENT. The 0.3.0 gate check
     `no-auto-promote`. Asserted three ways: the import closure of `medos.training`
     (`MOS-TRAIN-189`'s call-graph property), the scalar approval signature
     (`MOS-TRAIN-232`), and the database trigger that refuses `auto_promote` beside a
     clinical deployment (`MOS-TRAIN-184`).

SKIP DISCIPLINE: `tests/_support/skips.py` only. There is no raw `pytest.skip` here, and
the module declares `postgres` through `tests/_support/stack.py`'s `tests/integration`
prefix, so `--require-stack` probes it.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import pkgutil
import secrets
import sys
import uuid as _uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db import audit
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import digest as ev_digest
from medos.evidence import repo as ev_repo
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.store import InMemoryManifestStore
from medos.promotion import acts as promo
from medos.promotion import dossier as dossier_mod
from medos.registry import repo as registry_repo
from medos.registry.digest import content_digest_of
from medos.sdk import autoconfig
from medos.sdk import bundle as bundle_mod
from medos.sdk import fixtures as train_fixtures
from medos.sdk.errors import (
    ChainRefused,
    ConversionRefused,
    PartitionForbidden,
    RunRefused,
    SearchRefused,
)
from medos.sdk.spec import parse_spec
from medos.training import candidate as cand
from medos.training import cohort as cohort_mod
from medos.training import conversion as conv
from medos.training import orchestrator as orch
from medos.training import runs as tr
from medos.training import search as search_mod
from psycopg.rows import dict_row

pytestmark = pytest.mark.slow

BUCKET = "medos-evidence"
SALT = b"test-tenant-salt-not-a-real-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"
HUMAN_ID = "44444444-4444-4444-4444-444444444444"
COMMIT = "b" * 40
IMAGE = "sha256:" + "c" * 64
TRACE = "0af7651916cd43dd8448eb211c80319c"
SIGNER = "https://github.com/pulmoai/effusion/.github/workflows/release.yml@refs/tags/v1"

SERVICE_ACTOR = audit.Actor(kind="service_account", id=OPERATOR, auth="api_key")
HUMAN_ACTOR = audit.Actor(kind="user", id=HUMAN_ID, auth="oidc")

SUPPLY_CHAIN: dict[str, Any] = {
    "oci_image_digest": "sha256:" + "a1" * 32,
    "signature": b"cosign-bundle-bytes",
    "signature_alg": "cosign-sigstore",
    "signer_identity": SIGNER,
    "sbom_object_key": "sbom/effusion.cdx.json",
}

TRAINING_TABLES = (
    "training_runs",
    "configuration_searches",
    "conversion_runs",
    "capability_seed_variance",
    "split_test_exposure",
)
EVIDENCE_TABLES = (
    "datasets", "dataset_versions", "dataset_cases", "dataset_splits",
    "dataset_split_members", "annotation_sets", "annotation_readers", "annotations",
)

# MOS-TRAIN-124's four reproducibility blocks, with every key the requirement's own
# example carries. Written once here so that a test which is about something else does
# not quietly omit one and pass for the wrong reason.
SEEDS = {"python": 20260311, "numpy": 20260311, "torch": 20260311,
         "dataloader_worker_base": 900}
DETERMINISM = {"torch_use_deterministic_algorithms": True, "cudnn_benchmark": False,
               "cublas_workspace_config": ":4096:8", "tf32_allowed": False}
HARDWARE = {"gpu_model": "NVIDIA A100-SXM4-80GB", "gpu_count": 2, "driver": "550.54.15",
            "cuda": "12.4", "cudnn": "9.1.0", "nccl": "2.21.5"}
FRAMEWORKS = {"torch": "2.4.1", "monai": "1.4.0", "numpy": "1.26.4",
              "simpleitk": "2.3.1"}
CLEAN_LEAKAGE = {"L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass", "L5": "pass"}


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture()
def train_db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One connection with the default tenant bound and this release's tables emptied.

    TRUNCATE rather than DELETE, for the reason 0006's and 0011's suites established: every
    table here carries a BEFORE DELETE guard and TRUNCATE fires no row-level triggers, so
    the append-only guarantee holds for application code while the harness starts clean.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(
        "TRUNCATE "
        + ", ".join(TRAINING_TABLES + EVIDENCE_TABLES)
        + ", artifacts, registry_changelog, deployments, deployment_gate_decisions "
        "CASCADE"
    )
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


def _uid(*parts: int) -> str:
    return "1.2.826.0.1.3680043.10.7." + ".".join(str(p) for p in parts)


def _pixel_digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _record(patient: int, *, pixel_seed: str | None = None) -> SeriesRecord:
    half = patient % 2
    return SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", f"P{patient:04d}"),
        study_instance_uid=_uid(patient, 1),
        series_instance_uid=_uid(patient, 1, 1),
        modality="CT",
        sop_class_uid="1.2.840.10008.5.1.4.1.1.2",
        sop_instance_uids=tuple(_uid(patient, 1, 1, i) for i in range(40)),
        series_pixel_digest=_pixel_digest(pixel_seed or f"{patient}/1/1"),
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
    def __init__(self, version_id: str, version_digest: str, split_id: str,
                 split_digest: str, annotation_set_id: str, annotation_digest: str,
                 leakage: dict[str, Any]) -> None:
        self.version_id = version_id
        self.version_digest = version_digest
        self.split_id = split_id
        self.split_digest = split_digest
        self.annotation_set_id = annotation_set_id
        self.annotation_digest = annotation_digest
        self.leakage = leakage


def _prepare(
    conn: psycopg.Connection[Any],
    store: InMemoryManifestStore,
    *,
    n_patients: int = 10,
    offset: int = 0,
    shared_pixels: tuple[int, int] | None = None,
    waivers: tuple[dict[str, Any], ...] = (),
) -> Cohort:
    """Seal a cohort, freeze a train/tune split and freeze a reference standard over it.

    `shared_pixels` makes two DISTINCT patients carry the same `series_pixel_digest` and
    places them on opposite sides of the split, which is how L3 of `MOS-EVID-034` is made
    to fail. L1 cannot be made to fail through this path at all: `PRIMARY KEY (split_id,
    patient_key)` makes a patient in two partitions structurally impossible
    (`MOS-STORE-293`), which is the schema doing its job and is why the blocking test uses
    L3 -- `waiver_blocks_training` treats L1, L2, L3 and L5 identically.
    """
    records = []
    for p in range(n_patients):
        patient = offset + p
        seed = None
        if shared_pixels is not None and p in shared_pixels:
            seed = f"shared/{offset}"
        records.append(_record(patient, pixel_seed=seed))

    dataset = ev_repo.create_dataset(
        conn,
        slug=f"train-cohort-{secrets.token_hex(4)}",
        display_name="training cohort",
        purpose="training",
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
    by_key = {r.patient_key: i for i, r in enumerate(records)}
    keys = sorted(by_key)
    # Patients alternate train/tune. `test` is deliberately absent from this split's
    # membership: nothing in this file needs it and MOS-TRAIN-141 is asserted directly.
    assignments = [(k, "tune" if by_key[k] % 2 else "train") for k in keys]
    if shared_pixels is not None:
        a, b = shared_pixels
        forced = {records[a].patient_key: "train", records[b].patient_key: "tune"}
        assignments = [(k, forced.get(k, part)) for k, part in assignments]

    split = ev_repo.freeze_split(
        conn,
        dataset_version_id=sealed.id,
        name="fit-select",
        assignments=assignments,
        store=store,
        bucket=BUCKET,
        frozen_by=OPERATOR,
        assignment_method="alternating by sorted patient_key, fixed and materialised",
        records=records,
        waivers=list(waivers),
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
        version_digest=sealed.manifest_digest,
        split_id=split.id,
        split_digest=split.split_digest,
        annotation_set_id=annotations.id,
        annotation_digest=annotations.annotation_digest,
        leakage=dict(split.leakage_report),
    )


@pytest.fixture()
def cohort(train_db: psycopg.Connection[Any], store: InMemoryManifestStore) -> Cohort:
    return _prepare(train_db, store)


def _binding(c: Cohort, **overrides: Any) -> tr.RunBinding:
    """A complete `MOS-TRAIN-124` binding. Every override is an explicit deviation."""
    kwargs: dict[str, Any] = {
        "capability_id": "pleural_effusion",
        "dataset_version_id": c.version_id,
        "dataset_version_digest": c.version_digest,
        "split_id": c.split_id,
        "split_digest": c.split_digest,
        "annotation_set_id": c.annotation_set_id,
        "annotation_digest": c.annotation_digest,
        "preprocessing_spec_id": "prep.pulmo.effusion",
        "preprocessing_spec_version": 2,
        "preprocessing_spec_digest": "sha256:" + "d3" * 32,
        "code_commit": COMMIT,
        "code_dirty": False,
        "image_digest": IMAGE,
        "training_backend": {"kind": "nnunet", "version": "2.5.1", "plan_digest": None},
        "hyperparameters": {"learning_rate": 0.0002, "num_epochs": 600},
        "seeds": dict(SEEDS),
        "determinism": dict(DETERMINISM),
        "hardware": dict(HARDWARE),
        "framework_versions": dict(FRAMEWORKS),
    }
    kwargs.update(overrides)
    return tr.RunBinding(**kwargs)


def _submit(conn: psycopg.Connection[Any], c: Cohort, **overrides: Any) -> tr.RunRow:
    return tr.submit(
        conn,
        binding=_binding(c, **overrides),
        actor=SERVICE_ACTOR,
        trace_id=TRACE,
        leakage=c.leakage,
        output_kind="label",
    )


def _model_manifest(family: str, version: str, **spec_overrides: Any) -> dict[str, Any]:
    """A `model_version` manifest chapter 6's seeded schema accepts."""
    spec: dict[str, Any] = {
        "capabilities": ["pleural_effusion"],
        "weights_availability": "platform_managed",
        "weights": {"oci_ref": "ghcr.io/pulmoai/effusion-unet",
                    "digest": "sha256:" + "5e" * 32, "format": "onnx",
                    "size_bytes": 184236032},
        "preprocessing_spec_ref": "ps_pulmo_effusion_prep_2_0_0",
        "golden_fixture": {"sha256": "sha256:" + "0c" * 32,
                           "output_tensor_sha256": "sha256:" + "b9" * 32,
                           "output_shape": [1, 2, 128, 192, 192]},
        "io": {
            "input": {"name": "input", "shape": [1, 1, 128, 192, 192],
                      "dtype": "float32", "layout": "NCZYX", "orientation": "LPS"},
            "output": {"name": "logits", "shape": [1, 2, 128, 192, 192],
                       "dtype": "float32", "layout": "NCZYX",
                       "kind": "segmentation_logits",
                       "label_map": {"0": "background", "1": "pleural_effusion"}},
        },
        "operating_point": {"kind": "probability_threshold", "score_threshold": 0.45,
                            "selected_on_evaluation_run": "er_01JP4T9X7B",
                            "selection_rule": "max F1 on the tune partition"},
        "runtime": {"engine": "triton", "engine_version": "24.08",
                    "backend": "onnxruntime", "gpu_architectures": ["sm_80", "sm_86"]},
        "derived_from": None,
        "evaluation_run_id": "er_01JP4T9X7B",
        "not_validated_for": ["studies with slice thickness > 3.0 mm"],
        "known_failure_modes": ["loculated effusions are under-segmented"],
    }
    spec.update(spec_overrides)
    manifest = {
        "schema_version": "1.0.0",
        "kind": "model_version",
        "family": family,
        "version": version,
        "publisher": {"org_id": "org_pulmoai", "signing_identity": SIGNER},
        "created_at": "2026-03-11T11:02:41Z",
        "spec": spec,
    }
    manifest["content_digest"] = content_digest_of(manifest)
    return manifest


def _publish_model(
    conn: psycopg.Connection[Any], public_id: str, family: str, version: str, **kw: Any
) -> dict[str, Any]:
    row, _created = registry_repo.publish(
        conn,
        manifest=_model_manifest(family, version, **kw),
        public_id=public_id,
        actor=SERVICE_ACTOR,
        trace_id=TRACE,
        **SUPPLY_CHAIN,
    )
    return row


# =====================================================================================
# 1. The migration: five tables, and the two layers that seal them
# =====================================================================================
def test_the_five_tables_carry_forced_row_security_and_no_delete_grant(
    train_db: psycopg.Connection[Any],
) -> None:
    """`MOS-STORE-229` / `MOS-TRAIN-217`. Asserted against the catalogue, not the DDL text."""
    forced = {
        r["relname"]
        for r in train_db.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relforcerowsecurity AND c.relname = ANY(%s)",
            (list(TRAINING_TABLES),),
        ).fetchall()
    }
    assert forced == set(TRAINING_TABLES)

    deletes = train_db.execute(
        "SELECT DISTINCT table_name FROM information_schema.role_table_grants "
        "WHERE table_schema = 'public' AND privilege_type = 'DELETE' "
        "AND grantee <> 'medicalos_owner' AND table_name = ANY(%s)",
        (list(TRAINING_TABLES),),
    ).fetchall()
    assert deletes == [], (
        "MOS-TRAIN-217: the non-nominated trials' checkpoints MUST be retained with their "
        "digests recorded, so that a re-nomination is auditable rather than a re-run. A "
        "deletable trial is a search whose losing arms can be made to disappear."
    )


def test_the_application_role_cannot_update_a_deployment_subject(
    train_db: psycopg.Connection[Any],
) -> None:
    """`MOS-TRAIN-180`'s grant half, which 0008 left open and 0013 closes.

    "`deployments.service_version_id` MUST be immutable after insert, by revoking `UPDATE`
    on the column from the application role AND by a `BEFORE UPDATE` trigger."
    """
    granted = {
        r["column_name"]
        for r in train_db.execute(
            "SELECT column_name FROM information_schema.column_privileges "
            "WHERE table_schema = 'public' AND table_name = 'deployments' "
            "AND privilege_type = 'UPDATE' AND grantee = 'medicalos_app'"
        ).fetchall()
    }
    assert granted, "the narrowing must not have removed the grant entirely"
    assert not (granted & {"subject_kind", "subject_id", "subject_version"}), (
        "a platform that can edit a serving row can change what is running without "
        "producing a deployment record (MOS-TRAIN-180)"
    )
    # ... and the columns a promotion legitimately writes are still there.
    assert {"role", "state", "clinical_use_mode", "approved_by"} <= granted


def test_the_reproducibility_binding_is_sealed_after_submit(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-124`, as a trigger. A binding that can be edited is decorative."""
    run = _submit(train_db, cohort)
    with pytest.raises(psycopg.DatabaseError) as exc:
        train_db.execute(
            "UPDATE training_runs SET seeds = %s WHERE id = %s",
            (json.dumps({**SEEDS, "torch": 1}), _uuid.UUID(run.id)),
        )
    assert "sealed at submit" in str(exc.value)
    train_db.rollback()


def test_a_dirty_tree_cannot_reach_succeeded(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-125`, in the library AND in the database."""
    run = _submit(train_db, cohort, code_dirty=True)
    tr.start(train_db, run_id=run.id, runner="worker-1",
             fingerprint_digest="sha256:" + "11" * 32)
    with pytest.raises(RunRefused) as exc:
        tr.succeed(train_db, run_id=run.id, bundle_digest="sha256:" + "22" * 32)
    assert "MOS-TRAIN-125" in str(exc.value)
    train_db.rollback()

    # The CHECK holds even for a session the GRANT does not bind.
    with pytest.raises(psycopg.errors.CheckViolation):
        train_db.execute(
            "UPDATE training_runs SET state = 'SUCCEEDED', bundle_digest = %s, "
            "runner = 'x', started_at = now(), finished_at = now() WHERE id = %s",
            ("sha256:" + "22" * 32, _uuid.UUID(run.id)),
        )
    train_db.rollback()


def test_only_one_trial_of_a_search_may_be_nominated(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-219`, verbatim: a database property, not a convention in the driver."""
    search = _make_search(train_db, cohort)
    a = _submit(train_db, cohort, search_id=search["id"], trial_index=0)
    b = _submit(train_db, cohort, search_id=search["id"], trial_index=1,
                hyperparameters={"learning_rate": 0.0005, "num_epochs": 600})
    train_db.execute("UPDATE training_runs SET nominated = true WHERE id = %s",
                     (_uuid.UUID(a.id),))
    with pytest.raises(psycopg.errors.UniqueViolation):
        train_db.execute("UPDATE training_runs SET nominated = true WHERE id = %s",
                         (_uuid.UUID(b.id),))
    train_db.rollback()


def test_the_test_exposure_counter_cannot_be_reset(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-216`: "MUST NOT allow it to be reset"."""
    assert tr.note_test_exposure(
        train_db, capability_id="pleural_effusion", split_digest=cohort.split_digest
    ) == 1
    assert tr.note_test_exposure(
        train_db, capability_id="pleural_effusion", split_digest=cohort.split_digest
    ) == 2
    assert tr.test_exposure_count(
        train_db, capability_id="pleural_effusion", split_digest=cohort.split_digest
    ) == 2
    with pytest.raises(psycopg.DatabaseError) as exc:
        train_db.execute(
            "UPDATE split_test_exposure SET exposure_count = 0 WHERE split_digest = %s",
            (cohort.split_digest,),
        )
    assert "MUST NOT be reset" in str(exc.value)
    train_db.rollback()


# =====================================================================================
# 2. TrainingRun: the binding, and the three things submit refuses
# =====================================================================================
def test_submit_pins_every_reproducibility_input_and_digests_it(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-124`: cohort, split, annotations, preprocessing, commit, image, seeds,
    determinism, hardware, frameworks -- all nine, round-tripped, with one digest over them."""
    run = _submit(train_db, cohort)
    row = run.as_dict()
    assert row["state"] == "PENDING"
    assert row["dataset_version_digest"] == cohort.version_digest
    assert row["split_digest"] == cohort.split_digest
    assert row["annotation_digest"] == cohort.annotation_digest
    assert row["code_commit"] == COMMIT and row["code_dirty"] is False
    assert row["image_digest"] == IMAGE
    assert row["seeds"] == SEEDS and row["determinism"] == DETERMINISM
    assert row["hardware"] == HARDWARE and row["framework_versions"] == FRAMEWORKS
    assert row["fit_partition"] == "train" and row["select_partition"] == "tune"
    assert row["run_digest"] == tr.run_digest_of(_binding(cohort))

    # MOS-STORE-301's property, applied to a TrainingRun: two runs with the same digest
    # are the same experiment, so the second submission is a unique violation.
    with pytest.raises(psycopg.errors.UniqueViolation):
        _submit(train_db, cohort)
    train_db.rollback()


def test_a_waiver_does_not_license_fitting_on_the_data(
    train_db: psycopg.Connection[Any], store: InMemoryManifestStore
) -> None:
    """The 0.3.0 gate check `leakage-blocks-training`. `MOS-TRAIN-115`, `MOS-TRAIN-116`.

    Two distinct patients share a `series_pixel_digest` across `train` and `tune`, so L3
    fails. The split is frozen anyway under a `MOS-EVID-036` waiver -- which is legitimate,
    because a waiver is "a statement about what a report may claim". The training run is
    then refused with the check named, and the waiver counts for nothing.
    """
    waiver = {
        "check_id": "L3",
        "waived_by": OPERATOR,
        "waived_at": "2026-03-11T00:00:00Z",
        "rationale": "two acquisitions of one phantom; retained deliberately",
        "affected_pairs": [],
    }
    leaky = _prepare(train_db, store, shared_pixels=(0, 1), waivers=(waiver,))
    assert leaky.leakage["L3"] in ("waived", {"outcome": "waived"}) or isinstance(
        leaky.leakage["L3"], (str, dict)
    )

    with pytest.raises(RunRefused) as exc:
        tr.submit(
            train_db,
            binding=_binding(leaky),
            actor=SERVICE_ACTOR,
            trace_id=TRACE,
            leakage=leaky.leakage,
            output_kind="label",
        )
    refusal = exc.value.refusals[0]
    assert refusal.check_id == "MOS-TRAIN-115"
    assert refusal.code == "leakage_blocks_training"
    assert "L3" in refusal.observed
    # And no row was written: the refusal is BEFORE the first batch is loaded.
    assert train_db.execute("SELECT count(*) AS n FROM training_runs").fetchone()["n"] == 0


def test_submitting_without_a_leakage_report_is_not_the_same_as_a_clean_one(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """An optional check with a permissive default is not a check. `MOS-TRAIN-115`."""
    with pytest.raises(RunRefused) as exc:
        tr.submit(train_db, binding=_binding(cohort), actor=SERVICE_ACTOR,
                  trace_id=TRACE, leakage=None, output_kind="label")
    assert exc.value.refusals[0].code == "leakage_report_not_supplied"


def test_a_run_cannot_name_the_test_partition(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-141` / `MOS-TRAIN-214`: `test` is not expressible in the record."""
    with pytest.raises(RunRefused) as exc:
        _submit(train_db, cohort, select_partition="test")
    assert exc.value.refusals[0].check_id == "MOS-TRAIN-141"

    # And the column itself refuses it, for a session the library does not pass through.
    with pytest.raises(psycopg.errors.CheckViolation):
        train_db.execute(
            "INSERT INTO training_runs (public_id, tenant_id, capability_id, "
            "dataset_version_id, dataset_version_digest, split_id, split_digest, "
            "select_partition, annotation_set_id, annotation_digest, "
            "preprocessing_spec_id, preprocessing_spec_version, "
            "preprocessing_spec_digest, code_commit, code_dirty, image_digest, "
            "training_backend, hyperparameters, hyperparameters_digest, seeds, "
            "determinism, hardware, framework_versions, run_digest) "
            "VALUES ('tr_01ARZ3NDEKTSV4RRFFQ69G5FAV', %s, 'x', %s, %s, %s, %s, 'test', "
            "%s, %s, 'p', 1, %s, %s, false, %s, '{\"kind\":\"nnunet\",\"version\":\"1\"}', "
            "'{}', %s, %s, %s, %s, %s, %s)",
            (
                _uuid.UUID(DEFAULT_TENANT_ID), _uuid.UUID(cohort.version_id),
                cohort.version_digest, _uuid.UUID(cohort.split_id), cohort.split_digest,
                _uuid.UUID(cohort.annotation_set_id), cohort.annotation_digest,
                "sha256:" + "d3" * 32, COMMIT, IMAGE, "sha256:" + "ee" * 32,
                json.dumps(SEEDS), json.dumps(DETERMINISM), json.dumps(HARDWARE),
                json.dumps(FRAMEWORKS), "sha256:" + "ff" * 32,
            ),
        )
    train_db.rollback()


def test_a_hand_configured_label_backend_owes_a_rationale(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-211`: the default for a `label` capability, and what a deviation owes."""
    hand = {"kind": "monai_supervised", "version": "1.4.0", "plan_digest": None}
    with pytest.raises(RunRefused) as exc:
        _submit(train_db, cohort, training_backend=hand)
    assert exc.value.refusals[0].code == "hand_configured_backend_without_rationale"

    # Twenty characters naming what the default failed to do, and it is admitted.
    run = _submit(
        train_db, cohort, training_backend=hand,
        backend_rationale="the auto-configured plan chose 1.5 mm z-spacing and lost the "
                          "sub-centimetre loculations this capability exists to find",
    )
    assert run.backend_kind == "monai_supervised"
    # MOS-TRAIN-211's default is one of the two auto-configuring backends.
    assert autoconfig.default_backend_for("label") in autoconfig.AUTO_CONFIGURING_BACKENDS
    assert autoconfig.default_backend_for("probability") is None


def test_an_auto_configuring_backend_freezes_its_plan_at_run_start(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-135`, `MOS-TRAIN-223`, `MOS-TRAIN-225`: frozen once, then immovable."""
    run = _submit(train_db, cohort)
    fingerprint = "sha256:" + "ab" * 32

    with pytest.raises(RunRefused) as exc:
        tr.start(train_db, run_id=run.id, runner="worker-1")
    assert exc.value.refusals[0].code == "fingerprint_not_frozen"
    train_db.rollback()

    started = tr.start(train_db, run_id=run.id, runner="worker-1",
                       orchestrator_run_id="lp_abc", fingerprint_digest=fingerprint)
    assert started.state == "RUNNING"
    assert started["fingerprint_digest"] == fingerprint
    assert started["training_backend"]["plan_digest"] == fingerprint

    with pytest.raises(psycopg.DatabaseError) as raised:
        train_db.execute(
            "UPDATE training_runs SET fingerprint_digest = %s WHERE id = %s",
            ("sha256:" + "cd" * 32, _uuid.UUID(run.id)),
        )
    assert "frozen at run start" in str(raised.value)
    train_db.rollback()


def test_the_run_lifecycle_is_a_graph_and_a_terminal_run_is_terminal(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    run = _submit(train_db, cohort)
    tr.start(train_db, run_id=run.id, runner="worker-1",
             fingerprint_digest="sha256:" + "ab" * 32)
    done = tr.succeed(train_db, run_id=run.id, bundle_digest="sha256:" + "22" * 32,
                      bundle_location=(BUCKET, "bundles/effusion-1.0.0.tar"))
    assert done.state == "SUCCEEDED" and done.bundle_digest == "sha256:" + "22" * 32

    with pytest.raises(psycopg.DatabaseError):
        train_db.execute("UPDATE training_runs SET state = 'RUNNING' WHERE id = %s",
                         (_uuid.UUID(run.id),))
    train_db.rollback()


def test_seed_variance_needs_three_runs_differing_only_in_seeds(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-127`. A characterisation assembled from runs that also differ in
    hyperparameters measures the hyperparameters."""
    ids = []
    for i in range(3):
        run = _submit(train_db, cohort, seeds={**SEEDS, "torch": 20260311 + i})
        ids.append(run.id)

    recorded = tr.record_seed_variance(
        train_db,
        capability_id="pleural_effusion",
        metric="dice_mean_per_case",
        sd=0.011,
        training_run_ids=ids,
        backend_kind="nnunet",
        gpu_count=2,
        hyperparameters_digest=tr.hyperparameters_digest_of(
            {"learning_rate": 0.0002, "num_epochs": 600}
        ),
        recorded_by=OPERATOR,
    )
    assert recorded["runs"] == 3 and float(recorded["sd"]) == pytest.approx(0.011)
    live = tr.seed_variance(train_db, capability_id="pleural_effusion")
    assert live is not None and live["metric"] == "dice_mean_per_case"

    different = _submit(train_db, cohort, hyperparameters={"learning_rate": 0.9})
    with pytest.raises(RunRefused) as exc:
        tr.record_seed_variance(
            train_db, capability_id="pleural_effusion", metric="dice_mean_per_case",
            sd=0.2, training_run_ids=[*ids[:2], different.id], backend_kind="nnunet",
            gpu_count=2, hyperparameters_digest="sha256:" + "00" * 32,
            recorded_by=OPERATOR,
        )
    assert exc.value.refusals[0].code == "seed_variance_runs_differ_in_more_than_seeds"


# =====================================================================================
# 3. The Orchestrator port: two drivers, four constraints
# =====================================================================================
def test_both_drivers_satisfy_the_same_port() -> None:
    """`MOS-REL-048`: "A single-implementation interface that no test substitutes is not
    a seam." The conformance report runs against both and the observations must agree."""
    spec = orch.TrainingRunSpec(
        run_public_id="tr_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        capability_id="pleural_effusion",
        argv=(sys.executable, "-c", "print('trained')"),
    )
    assert isinstance(orch.LocalProcessOrchestrator(), orch.Orchestrator)
    assert isinstance(orch.FakeOrchestrator(), orch.Orchestrator)

    real = orch.conformance_report(orch.LocalProcessOrchestrator(), spec)
    assert real["run_id_is_a_string"] and real["terminal"]
    assert real["final_state"] == "SUCCEEDED"
    assert real["logs_are_bytes"] and real["unknown_run_raises"]
    assert real["cancel_after_terminal_is_safe"]

    fake = orch.FakeOrchestrator()
    run_id = fake.submit(spec)
    fake.advance(run_id, "SUCCEEDED")
    assert fake.poll(run_id).terminal
    assert fake.submitted[0].argv == spec.argv
    with pytest.raises(orch.UnknownRun):
        fake.poll("nope")


def test_a_failing_child_is_a_failed_run_not_a_silent_one() -> None:
    """`MOS-REL-051`: no discarded error return."""
    driver = orch.LocalProcessOrchestrator()
    run_id = driver.submit(
        orch.TrainingRunSpec(
            run_public_id="tr_01ARZ3NDEKTSV4RRFFQ69G5FAW",
            capability_id="pleural_effusion",
            argv=(sys.executable, "-c", "import sys; sys.stderr.write('boom'); "
                                        "sys.exit(3)"),
        )
    )
    state = driver.poll(run_id)
    while not state.terminal:
        state = driver.poll(run_id)
    assert state.state == "FAILED" and state.exit_code == 3
    assert b"boom" in driver.logs(run_id)
    driver.reap(run_id)


def test_the_orchestrator_holds_none_of_the_six_forbidden_permissions() -> None:
    """C3 of `MOS-TRAIN-121`, and `MOS-TRAIN-174`'s list, spelled once."""
    assert set(orch.FORBIDDEN_PERMISSIONS) == {
        "job.create", "deployment.create", "deployment.promote",
        "deployment.gate.override", "artifact.approve", "evidence.report.issue",
    }
    assert orch.assert_no_forbidden_permission(["cohort.read", "artifact.publish"]) == ()
    assert orch.assert_no_forbidden_permission(
        ["evidence.run", "deployment.promote"]
    ) == ("deployment.promote",)
    assert set(orch.ORCHESTRATOR_CONSTRAINTS) == {"C1", "C2", "C3", "C4"}


def test_the_pool_guard_refuses_before_a_run_id_exists() -> None:
    """`MOS-TRAIN-123`: a training job that evicts a resident engine converts a research
    activity into a clinical latency incident with no serving cause."""

    def guard(pool: str) -> None:
        raise orch.PoolNotSchedulable(f"{pool} carries a clinical deployment")

    driver = orch.LocalProcessOrchestrator(pool_guard=guard)
    with pytest.raises(orch.PoolNotSchedulable):
        driver.submit(
            orch.TrainingRunSpec(
                run_public_id="tr_01ARZ3NDEKTSV4RRFFQ69G5FAX",
                capability_id="pleural_effusion",
                argv=(sys.executable, "-c", "pass"),
            )
        )


def test_the_port_takes_a_vector_and_not_a_shell_string() -> None:
    with pytest.raises(ValueError, match="non-empty vector"):
        orch.TrainingRunSpec(
            run_public_id="tr_x", capability_id="c", argv=()
        )


# =====================================================================================
# 4. The cohort resolver: 403, never an empty set
# =====================================================================================
def test_the_test_partition_is_403_and_not_an_empty_set(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-141`, and chapter 17 acceptance check 15."""
    train_cases = cohort_mod.resolve(
        train_db,
        cohort_mod.CohortRequest(split_id=cohort.split_id, partition="train"),
        principal="training-job",
    )
    assert train_cases and all(c.partition == "train" for c in train_cases)

    with pytest.raises(PartitionForbidden) as exc:
        cohort_mod.resolve(
            train_db,
            cohort_mod.CohortRequest(split_id=cohort.split_id, partition="test"),
            principal="training-job",
        )
    problem = exc.value.as_problem()
    assert problem["status"] == 403 and problem["class"] == "authorization"
    assert "not an empty set" in str(exc.value)


@pytest.mark.parametrize(
    "bad",
    [
        "/mnt/cohort/train",
        "C:\\data\\cohort",
        "s3://bucket/prefix/",
        "cohorts/*/train",
    ],
)
def test_the_resolver_refuses_a_path_a_prefix_and_a_glob(bad: str) -> None:
    """Chapter 17 acceptance check 15: a container that can name its own data can name
    the test partition."""
    with pytest.raises(ValueError, match="cohort resolver takes a split id"):
        cohort_mod.CohortRequest(split_id=bad, partition="train")


def test_there_is_no_fourth_partition() -> None:
    """`MOS-TRAIN-214`: "`val` remains not a permitted value"."""
    with pytest.raises(ValueError, match="no fourth partition"):
        cohort_mod.CohortRequest(split_id="abc", partition="val")


# =====================================================================================
# 5. The MONAI Bundle
# =====================================================================================
def _spec_document() -> Any:
    return parse_spec(train_fixtures.selftest_spec_document())


def _write_bundle(root: Path, **kw: Any) -> Any:
    spec = _spec_document()
    metadata = bundle_mod.metadata_document(
        version="1.0.0",
        monai_version="1.4.0",
        pytorch_version="2.4.1",
        numpy_version="1.26.4",
        inputs={"image": {"spatial_shape": [32, 48, 48], "dtype": "float32",
                          "channel_def": {"0": "image"}}},
        outputs={"pred": {"spatial_shape": [32, 48, 48], "dtype": "float32",
                          "channel_def": {"0": "background", "1": "lesion"}}},
        **kw.pop("metadata_kw", {}),
    )
    return bundle_mod.write_bundle(
        root,
        spec=spec,
        metadata=metadata,
        weights=b"onnx-graph-bytes",
        weights_format="onnx",
        golden_volume=b"nii-gz-bytes",
        golden_tensor=b"\x00\x01\x02\x03",
        checkpoint=b"torch-checkpoint",
        **kw,
    )


def test_a_written_bundle_verifies_with_one_weights_file_and_one_digest(
    tmp_path: Path,
) -> None:
    """`MOS-TRAIN-129`: "one file, one digest"; `models/model.pt` is not the served artifact."""
    report = _write_bundle(tmp_path / "bundle")
    assert report.layout_convention == "MOS-TRAIN-129"
    assert report.weights_path == "models/model.onnx"
    assert report.bundle_digest.startswith("sha256:")
    assert "models/model.pt" in report.files          # carried, never the weights layer
    assert set(bundle_mod.layout().values()) <= set(report.files)

    block = report.as_manifest_bundle_block()
    assert block["weights_digest"] == report.files["models/model.onnx"]


def test_inference_json_is_generated_from_the_spec_and_not_hand_written(
    tmp_path: Path,
) -> None:
    """`MOS-TRAIN-131` / `MOS-TRAIN-024`: two editable copies of `overlap` is the skew
    chapter 17 exists to prevent."""
    root = tmp_path / "bundle"
    _write_bundle(root)
    spec = _spec_document()
    assert bundle_mod.inference_config_matches_spec(root, spec)

    edited = json.loads((root / "configs/inference.json").read_text(encoding="utf-8"))
    edited["inferer"]["overlap"] = 0.25
    (root / "configs/inference.json").write_text(json.dumps(edited), encoding="utf-8")
    assert not bundle_mod.inference_config_matches_spec(root, spec)


def test_a_bundle_mixing_the_two_chapter_17_layouts_is_refused(tmp_path: Path) -> None:
    """The contradiction between `MOS-TRAIN-022` and `MOS-TRAIN-129`, made visible.

    Both requirements call their layout fixed and five of six paths differ. This test
    pins the resolution -- emit MOS-TRAIN-129's, read either, refuse a mixture -- so that
    the choice is reviewable rather than buried. Reported as a specification defect.
    """
    root = tmp_path / "bundle"
    _write_bundle(root)
    (root / "medicalos").mkdir()
    (root / "medicalos/preprocessing.yaml").write_text("id: prep\n", encoding="utf-8")
    (root / "medicalos/golden_input.nii.gz").write_bytes(b"x")
    (root / "medicalos/golden_tensor.f32").write_bytes(b"x")

    with pytest.raises(RunRefused) as exc:
        bundle_mod.verify(root)
    assert exc.value.refusals[0].code == "bundle_layout_mixed"
    assert "MOS-TRAIN-129" in exc.value.refusals[0].detail


def test_metadata_must_declare_the_tensor_contract_and_agree_with_the_manifest(
    tmp_path: Path,
) -> None:
    """`MOS-TRAIN-130`: two declarations of the tensor contract that may disagree are
    worse than one, because the mirrored segmentation that results passes every
    structural check."""
    with pytest.raises(RunRefused) as exc:
        bundle_mod.metadata_document(
            version="1.0.0", monai_version="1.4.0", pytorch_version="2.4.1",
            numpy_version="1.26.4",
            inputs={"image": {"spatial_shape": [32, 48, 48], "dtype": "float32"}},
            outputs={"pred": {"spatial_shape": [32, 48, 48], "dtype": "float32",
                              "channel_def": {"0": "bg"}}},
        )
    assert exc.value.refusals[0].code == "tensor_contract_incomplete"

    root = tmp_path / "bundle"
    _write_bundle(root)
    manifest_io = {
        "input": {"shape": [1, 1, 32, 48, 48], "dtype": "float32"},
        "output": {"shape": [1, 2, 64, 48, 48], "dtype": "float32",
                   "label_map": {"0": "background", "1": "lesion"}},
    }
    with pytest.raises(RunRefused) as exc:
        bundle_mod.verify(root, spec_io=manifest_io)
    assert any(r.code == "tensor_contract_disagrees_with_manifest"
               for r in exc.value.refusals)


def test_staple_is_named_in_the_vocabulary_and_is_not_permitted() -> None:
    """`MOS-TRAIN-228`: the served function would differ per study, so no fixed artifact
    exists to evaluate."""
    assert bundle_mod.COMBINATION_RULES["staple"] is False
    with pytest.raises(RunRefused) as exc:
        bundle_mod.ensemble_declaration(
            rule="staple",
            members=[{"training_run_id": "tr_a", "checkpoint_digest": "sha256:" + "1" * 64},
                     {"training_run_id": "tr_b", "checkpoint_digest": "sha256:" + "2" * 64}],
        )
    assert exc.value.refusals[0].code == "combination_rule_not_permitted"

    ok = bundle_mod.ensemble_declaration(
        rule="mean_probability",
        members=[{"training_run_id": "tr_a", "checkpoint_digest": "sha256:" + "1" * 64},
                 {"training_run_id": "tr_b", "checkpoint_digest": "sha256:" + "2" * 64}],
    )
    assert ok["applied_in"] == "model_space"
    assert ok["applied_before"] == "inverse_transform"


# =====================================================================================
# 6. Auto-configuration: the exporter that refuses rather than substitutes
# =====================================================================================
_NNUNET_PLAN: dict[str, Any] = {
    "transpose_forward": [0, 1, 2],
    "foreground_intensity_properties_per_channel": {
        "0": {"percentile_00_5": -1000.0, "percentile_99_5": 400.0,
              "mean": -350.2, "std": 410.9},
    },
    "configurations": {
        "3d_fullres": {
            "spacing": [1.5, 0.8, 0.8],
            "normalization_schemes": ["CTNormalization"],
            "patch_size": [128, 192, 192],
            "batch_size": 2,
            "use_mask_for_norm": False,
        }
    },
}


def test_the_fingerprint_exporter_maps_every_quantity_or_refuses() -> None:
    """`MOS-TRAIN-223`: "MUST refuse to emit for any derived quantity it cannot map
    exactly and MUST NOT substitute the nearest available value"."""
    out = autoconfig.export_spec_fields(_NNUNET_PLAN, backend="nnunet")
    fields = out["spec_fields"]
    assert fields["target_spacing_mm"] == [1.5, 0.8, 0.8]
    assert fields["clip.min_hu"] == -1000.0 and fields["clip.max_hu"] == 400.0
    assert fields["normalisation.scheme"] == "zscore_dataset"
    # MOS-TRAIN-225: enforceable from the spec alone.
    assert fields["normalisation.statistics_source"] == "spec"
    assert fields["patch.size_voxels"] == [128, 192, 192]
    assert out["fingerprint_digest"].startswith("sha256:")

    # A backend version bump that renames a field fails the export loudly.
    renamed = json.loads(json.dumps(_NNUNET_PLAN))
    del renamed["configurations"]["3d_fullres"]["spacing"]
    with pytest.raises(ChainRefused) as exc:
        autoconfig.export_spec_fields(renamed, backend="nnunet")
    assert exc.value.refusals[0].code == "fingerprint_field_not_mapped"

    # A normalisation scheme with no exact MOS-IMG-049 equivalent is refused, not guessed.
    odd = json.loads(json.dumps(_NNUNET_PLAN))
    odd["configurations"]["3d_fullres"]["normalization_schemes"] = ["ZScoreNormalization"]
    with pytest.raises(ChainRefused) as exc:
        autoconfig.export_spec_fields(odd, backend="nnunet")
    assert exc.value.refusals[0].code == "normalisation_scheme_not_mapped"


def test_the_training_batch_size_never_lands_in_patch_batch_size() -> None:
    """`MOS-TRAIN-224`: two different quantities with the same English name."""
    out = autoconfig.export_spec_fields(_NNUNET_PLAN, backend="nnunet")
    assert out["hyperparameters"]["training_batch_size"] == 2
    assert not any(k.startswith("patch.batch_size") for k in out["spec_fields"])
    assert "patch.batch_size" not in out["spec_fields"]


def test_the_serving_closure_may_not_import_a_planner() -> None:
    """`MOS-TRAIN-225`: "CI MUST assert their absence by module-name grep over the
    resolved closure"."""
    assert autoconfig.serving_closure_violations(
        ["numpy", "monai.transforms", "medos.inference.triton"]
    ) == ()
    assert autoconfig.serving_closure_violations(
        ["monai.apps.auto3dseg.data_analyzer", "numpy"]
    ) == ("monai.apps.auto3dseg.data_analyzer",)
    assert autoconfig.serving_closure_violations(
        ["nnunetv2.utilities.plans_handling.plans_handler"]
    ) != ()

    # ... and the real serving closure is clean today.
    assert autoconfig.serving_closure_violations(sorted(sys.modules)) == ()


# =====================================================================================
# 7. ConversionRun: E1/E2/E3, per case, with no relaxable bound
# =====================================================================================
def _cases(n: int = 21, *, bad_index: int | None = None) -> list[conv.CaseEquivalence]:
    out = [
        conv.CaseEquivalence(
            case_key="golden", patient_key="golden",
            e1_max_abs_probability_diff=1e-6, e2_post_threshold_dice=1.0,
            e3_volume_rel_diff=0.0, max_abs_logit_diff=1e-5, is_golden_fixture=True,
        )
    ]
    for i in range(n):
        dice = 0.980 if i == bad_index else 0.99995
        out.append(
            conv.CaseEquivalence(
                case_key=f"case-{i}", patient_key=f"pk-{i}",
                e1_max_abs_probability_diff=2e-5,
                e2_post_threshold_dice=dice,
                e3_volume_rel_diff=2e-4,
                max_abs_logit_diff=3e-4,
            )
        )
    return out


def test_every_case_must_hold_the_tolerance_and_a_perfect_mean_does_not_save_it() -> None:
    """The 0.3.0 gate check `served-plan-equivalence`. `MOS-TRAIN-162`, `MOS-TRAIN-163`.

    Chapter 17 acceptance check 8, verbatim: "inject a single case at
    `post_threshold_dice = 0.980` into an otherwise perfect fp32 cohort and assert
    registration is refused even though the cohort mean is 0.999."
    """
    block = conv.evaluate_equivalence(_cases(), precision="fp32")
    assert block["verdict"] == "pass"
    assert block["E2"]["bound"] == 0.9995 and block["E1"]["bound"] == 1e-4
    assert block["E3"]["bound"] == 1e-3
    assert block["n_patients"] >= conv.MIN_EQUIVALENCE_PATIENTS
    assert block["disclaimer"] == conv.EQUIVALENCE_DISCLAIMER
    assert "mean" not in json.dumps(block).lower().replace("max_abs", "")

    spoiled = _cases(bad_index=7)
    mean_dice = sum(c.e2_post_threshold_dice for c in spoiled[1:]) / (len(spoiled) - 1)
    # The cohort mean is 0.999 to three places -- the figure MOS-TRAIN-163 names -- and
    # it is above the per-case FLOOR of 0.9995 being missed by one case. That is the
    # whole point: the aggregate reports that nothing happened.
    assert round(mean_dice, 3) == 0.999
    with pytest.raises(ConversionRefused) as exc:
        conv.evaluate_equivalence(spoiled, precision="fp32")
    refusal = exc.value.refusals[0]
    assert refusal.check_id == "MOS-TRAIN-163"
    assert refusal.detail["failures"][0]["case_key"] == "case-7"

    # The bound is a property of the PRECISION CLASS and not of the model: MOS-TRAIN-162
    # has one row per class and no default row, and a case at 0.9990 is admitted by the
    # tf32/fp16 row and refused by the fp32 one.
    marginal = _cases(bad_index=None)
    marginal[8] = conv.CaseEquivalence(
        case_key="case-7", patient_key="pk-7",
        e1_max_abs_probability_diff=2e-5, e2_post_threshold_dice=0.9990,
        e3_volume_rel_diff=2e-4, max_abs_logit_diff=3e-4,
    )
    assert conv.evaluate_equivalence(marginal, precision="fp16")["verdict"] == "pass"
    with pytest.raises(ConversionRefused):
        conv.evaluate_equivalence(marginal, precision="fp32")


def test_a_declared_tolerance_may_tighten_and_may_not_loosen() -> None:
    """`MOS-TRAIN-162` / `MOS-TRAIN-164`: a tolerance relaxed to admit one artifact
    silently relaxes it for every future artifact of that family."""
    tighter = conv.tolerances_for(
        "fp32", declared={"E2_post_threshold_dice": 0.9999}
    )
    assert tighter.e2_min == 0.9999
    with pytest.raises(ConversionRefused) as exc:
        conv.tolerances_for("fp32", declared={"E2_post_threshold_dice": 0.99})
    assert exc.value.refusals[0].code == "declared_tolerance_is_looser"
    assert not any("override" in p for p in inspect.signature(
        conv.evaluate_equivalence).parameters)


def test_the_equivalence_cohort_is_twenty_patients_and_the_golden_fixture() -> None:
    """`MOS-TRAIN-159`: two corpora, both mandatory."""
    with pytest.raises(ConversionRefused) as exc:
        conv.evaluate_equivalence(_cases(n=5), precision="fp32")
    assert exc.value.refusals[0].code == "equivalence_cohort_too_small"

    without_golden = [c for c in _cases() if not c.is_golden_fixture]
    with pytest.raises(ConversionRefused) as exc:
        conv.evaluate_equivalence(without_golden, precision="fp32")
    assert exc.value.refusals[0].code == "golden_fixture_case_absent"


def test_conversion_lifecycle_and_int8_calibrates_on_tune(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-156`, `MOS-TRAIN-157`, `MOS-TRAIN-164`, `MOS-TRAIN-167`, `MOS-TRAIN-168`."""
    source = _publish_model(train_db, "mv_effusion_1_0_0", "pulmo.effusion", "1.0.0")

    with pytest.raises(ConversionRefused) as exc:
        conv.create(
            train_db, source_model_version_id=source["id"], target_format="tensorrt_plan",
            precision="int8", toolchain={"tensorrt": "10.3.0"},
            equivalence_cohort_digest=conv.equivalence_cohort_digest(
                [f"pk-{i}" for i in range(20)]
            ),
            code_commit=COMMIT, image_digest=IMAGE,
            calibration_dataset_version_id=cohort.version_id,
            calibration_partition="test",
        )
    assert exc.value.refusals[0].code == "calibration_partition_not_tune"

    run = conv.create(
        train_db, source_model_version_id=source["id"], target_format="tensorrt_plan",
        precision="fp16",
        toolchain={"torch": "2.4.1", "onnx": "1.16.2", "opset": 18,
                   "tensorrt": "10.3.0"},
        equivalence_cohort_digest=conv.equivalence_cohort_digest(
            [f"pk-{i}" for i in range(21)]
        ),
        code_commit=COMMIT, image_digest=IMAGE,
    )
    assert run["state"] == "PENDING"

    with pytest.raises(ConversionRefused) as exc:
        conv.start(train_db, conversion_id=run["public_id"])
    assert exc.value.refusals[0].code == "built_for_not_observed"
    train_db.rollback()

    conv.start(
        train_db, conversion_id=run["public_id"],
        built_for={"cuda_compute_capability": "8.6", "tensorrt_version": "10.3.0",
                   "cuda_version": "12.4"},
    )
    block = conv.evaluate_equivalence(_cases(), precision="fp16")
    target = _publish_model(
        train_db, "mv_effusion_1_0_1", "pulmo.effusion", "1.0.1",
        derived_from="mv_effusion_1_0_0", evaluation_run_id="er_01JQ0F6D3SXYZ",
    )
    done = conv.succeed(
        train_db, conversion_id=run["public_id"],
        target_model_version_id=target["id"], equivalence=block,
        patch_batch_size_digests={1: "sha256:" + "aa" * 32, 2: "sha256:" + "aa" * 32},
    )
    assert done["state"] == "SUCCEEDED"
    assert done["equivalence"]["patch_batch_size_invariance"][
        "allowed_patch_batch_sizes"
    ] == [1, 2]

    # MOS-TRAIN-168: a batch size that changes the output is a revalidation event.
    second = conv.create(
        train_db, source_model_version_id=source["id"], target_format="onnx",
        precision="fp32", toolchain={"onnx": "1.16.2", "opset": 18},
        equivalence_cohort_digest=conv.equivalence_cohort_digest(
            [f"pk-{i}" for i in range(21)]
        ),
        code_commit=COMMIT, image_digest=IMAGE,
    )
    conv.start(train_db, conversion_id=second["public_id"])
    with pytest.raises(ConversionRefused) as exc:
        conv.succeed(
            train_db, conversion_id=second["public_id"],
            target_model_version_id=target["id"],
            equivalence=conv.evaluate_equivalence(_cases(), precision="fp32"),
            patch_batch_size_digests={1: "sha256:" + "aa" * 32,
                                      2: "sha256:" + "bb" * 32},
        )
    assert exc.value.refusals[0].code == "patch_batch_size_changes_the_output"


def test_a_recalled_source_cannot_be_converted(
    train_db: psycopg.Connection[Any]
) -> None:
    """`MOS-TRAIN-169`: a recall that stops at the source while its conversion keeps
    serving is the recall failing at the only point where it mattered."""
    source = _publish_model(train_db, "mv_effusion_2_0_0", "pulmo.effusion", "2.0.0")
    registry_repo.set_status(
        train_db, source["public_id"], to_status="RECALLED",
        reason="a defect reaching patients", actor=SERVICE_ACTOR, trace_id=TRACE,
    )
    with pytest.raises(ConversionRefused) as exc:
        conv.create(
            train_db, source_model_version_id=source["id"], target_format="onnx",
            precision="fp32", toolchain={"onnx": "1.16.2"},
            equivalence_cohort_digest="sha256:" + "0" * 64,
            code_commit=COMMIT, image_digest=IMAGE,
        )
    assert exc.value.refusals[0].check_id == "MOS-TRAIN-169"


def test_the_run_must_exercise_the_served_plan_not_the_checkpoint() -> None:
    """`MOS-TRAIN-153` and chapter 17 acceptance check 9."""
    assert conv.run_exercises_the_served_plan(
        model_backend="tensorrt",
        model_built_for={"cuda_compute_capability": "8.6"},
        run_inference_backend={"kind": "tensorrt"},
        run_accelerator={"cuda_compute_capability": "8.6"},
    ) == ()

    wrong_backend = conv.run_exercises_the_served_plan(
        model_backend="tensorrt",
        model_built_for={"cuda_compute_capability": "8.6"},
        run_inference_backend={"kind": "pytorch"},
        run_accelerator={"cuda_compute_capability": "8.6"},
    )
    assert wrong_backend[0].code == "run_backend_is_not_the_served_backend"

    wrong_arch = conv.run_exercises_the_served_plan(
        model_backend="tensorrt",
        model_built_for={"cuda_compute_capability": "8.6"},
        run_inference_backend={"kind": "tensorrt"},
        run_accelerator={"gpu_architecture": "sm_80"},
    )
    assert wrong_arch[0].code == "run_accelerator_is_not_the_built_for_target"


def test_preprocessing_carries_forward_unchanged_through_a_conversion() -> None:
    """`MOS-TRAIN-166` and chapter 17 acceptance check 11."""
    src = {"preprocessing_spec_ref": "ps_a",
           "golden_fixture": {"output_tensor_sha256": "sha256:" + "ab" * 32}}
    same = dict(src)
    assert conv.preprocessing_carries_forward(src, same) == ()

    changed = {"preprocessing_spec_ref": "ps_b",
               "golden_fixture": {"output_tensor_sha256": "sha256:" + "cd" * 32}}
    problems = conv.preprocessing_carries_forward(src, changed)
    assert {p.code for p in problems} == {
        "conversion_changed_preprocessing", "golden_tensor_hash_differs"
    }


# =====================================================================================
# 8. ConfigurationSearch
# =====================================================================================
_SPACE: dict[str, Any] = {
    "schema_version": "1.0",
    "id": "space.pulmo.effusion.auto3dseg",
    "strategy": "grid",
    "axes": [
        {"name": "algorithm", "values": ["segresnet", "swinunetr"]},
        {"name": "learning_rate", "values": [0.0002, 0.0005]},
    ],
    "constraints": [],
    "derived_from_fingerprint": ["target_spacing_mm", "clip", "normalisation"],
}
_BUDGET: dict[str, Any] = {
    "max_trials": 12, "max_gpu_hours": 96.0, "max_wall_clock_hours": 72.0,
    "max_ensemble_members": 5, "max_footprint_bytes": 5368709120,
}


def _make_search(
    conn: psycopg.Connection[Any], c: Cohort, **overrides: Any
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "capability_id": "pleural_effusion",
        "dataset_version_id": c.version_id,
        "split_id": c.split_id,
        "split_digest": c.split_digest,
        "annotation_digest": c.annotation_digest,
        "backend": {"kind": "auto3dseg", "version": "1.4.0", "runner": "AutoRunner"},
        "fingerprint_digest": "sha256:" + "ab" * 32,
        "space": _SPACE,
        "strategy": "grid",
        "seed": 20260311,
        "selection_metric": "dice_mean_per_case",
        "selection_partition": "tune",
        "selection_rule": "argmax over completed trials of dice_mean_per_case on the "
                          "tune partition; ties broken by lowest trial_index",
        "trials_planned": 4,
        "budget": _BUDGET,
        "code_commit": COMMIT,
        "image_digest": IMAGE,
    }
    kwargs.update(overrides)
    return search_mod.create(conn, **kwargs)


def test_a_space_that_is_code_or_searches_a_frozen_quantity_is_refused() -> None:
    """`MOS-TRAIN-220`: a space that is code has no digest that means anything."""
    coded = json.loads(json.dumps(_SPACE))
    coded["axes"].append({"name": "select_fn", "values": ["$lambda x: x > -0.7"]})
    problems = {p.code for p in search_mod.space_problems(coded)}
    assert "space_is_code" in problems

    overlapping = json.loads(json.dumps(_SPACE))
    overlapping["axes"].append({"name": "target_spacing_mm", "values": [[1.5, 0.8, 0.8]]})
    problems = {p.code for p in search_mod.space_problems(overlapping)}
    assert "searched_a_frozen_quantity" in problems


def test_a_budget_with_an_absent_bound_is_a_registration_error(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-234`: "an absent bound is a registration error, not an unlimited one"."""
    for missing in search_mod.BUDGET_KEYS:
        budget = {k: v for k, v in _BUDGET.items() if k != missing}
        with pytest.raises(SearchRefused) as exc:
            _make_search(train_db, cohort, budget=budget)
        assert exc.value.refusals[0].code == "budget_bound_absent"
        train_db.rollback()


def test_a_search_cannot_declare_that_it_reads_test(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-214`: `test` is not expressible in the record."""
    with pytest.raises(SearchRefused) as exc:
        _make_search(train_db, cohort, read_partitions=["train", "tune", "test"])
    assert exc.value.refusals[0].check_id == "MOS-TRAIN-214"
    train_db.rollback()

    # ... and the column itself cannot hold it, for a session the library does not pass
    # through. `read_partitions <@ ARRAY['train','tune']` is MOS-TRAIN-214's own CHECK,
    # written as that requirement writes it. The row is also sealed after insert, so the
    # only way to attempt the value at all is a fresh INSERT.
    with pytest.raises(psycopg.errors.CheckViolation):
        train_db.execute(
            "INSERT INTO configuration_searches (public_id, tenant_id, capability_id, "
            "dataset_version_id, split_id, split_digest, annotation_digest, backend, "
            "fingerprint_digest, space, space_digest, strategy, seed, read_partitions, "
            "selection_metric, selection_partition, selection_rule, trials_planned, "
            "budget, code_commit, image_digest, search_digest) "
            "VALUES ('cs_01ARZ3NDEKTSV4RRFFQ69G5FAV', %s, 'pleural_effusion', %s, %s, "
            "%s, %s, '{\"kind\":\"auto3dseg\",\"version\":\"1.4.0\"}', %s, '{}', "
            "%s, 'grid', 1, ARRAY['train','tune','test']::text[], 'dice_mean_per_case', "
            "'tune', 'argmax on tune', 1, %s, %s, %s, %s)",
            (
                _uuid.UUID(DEFAULT_TENANT_ID), _uuid.UUID(cohort.version_id),
                _uuid.UUID(cohort.split_id), cohort.split_digest,
                cohort.annotation_digest, "sha256:" + "ab" * 32, "sha256:" + "cd" * 32,
                json.dumps(_BUDGET), COMMIT, IMAGE, "sha256:" + "ef" * 32,
            ),
        )
    train_db.rollback()


def test_the_trial_trajectory_is_reproducible_from_the_recorded_inputs() -> None:
    """`MOS-TRAIN-235`: "What is reproducible is the trajectory: which configurations
    were tried, in what order, and which one won." Not the weights."""
    a = search_mod.trial_sequence(_SPACE, seed=7, fingerprint_digest="sha256:" + "a" * 64)
    b = search_mod.trial_sequence(_SPACE, seed=7, fingerprint_digest="sha256:" + "a" * 64)
    assert [t["config_digest"] for t in a] == [t["config_digest"] for t in b]
    assert len(a) == 4

    random_space = {**_SPACE, "strategy": "random"}
    c = search_mod.trial_sequence(random_space, seed=7,
                                  fingerprint_digest="sha256:" + "a" * 64)
    d = search_mod.trial_sequence(random_space, seed=8,
                                  fingerprint_digest="sha256:" + "a" * 64)
    assert [t["config_digest"] for t in c] != [t["config_digest"] for t in d]

    with pytest.raises(ValueError, match="adaptive"):
        search_mod.trial_sequence({**_SPACE, "strategy": "adaptive"}, seed=1,
                                  fingerprint_digest="sha256:" + "a" * 64)


def test_a_search_nominates_exactly_one_trial_and_never_a_second(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-216` / `MOS-TRAIN-217`: a silent second nomination is selection on test
    performed one candidate at a time."""
    search = _make_search(train_db, cohort)
    winner = _submit(train_db, cohort, search_id=search["id"], trial_index=0)
    runner_up = _submit(train_db, cohort, search_id=search["id"], trial_index=1,
                        hyperparameters={"learning_rate": 0.0005, "num_epochs": 600})

    closed = search_mod.nominate(
        train_db, search_id=search["public_id"], training_run_id=winner.id,
        runner_up_training_run_id=runner_up.id, selection_margin=0.006,
        cost={"gpu_hours_used": 81.4, "wall_clock_hours": 26.2,
              "stop_reason": "space_exhausted"},
        trials_completed=4,
    )
    assert closed["state"] == "SUCCEEDED"
    assert str(closed["nominated_training_run_id"]) == winner.id
    assert tr.get(train_db, winner.id)["nominated"] is True

    with pytest.raises(SearchRefused) as exc:
        search_mod.nominate(
            train_db, search_id=search["public_id"], training_run_id=runner_up.id,
            runner_up_training_run_id=winner.id, selection_margin=0.001,
            cost={"stop_reason": "space_exhausted"}, trials_completed=4,
        )
    assert exc.value.refusals[0].check_id == "MOS-TRAIN-217"


def test_selection_within_the_seed_noise_is_detectable_by_one_comparison() -> None:
    """`MOS-TRAIN-236`: 81 GPU-hours for 0.006 Dice against a measured sd of 0.011
    selected noise, and the dossier must make that visible without arithmetic."""
    assert search_mod.selection_is_noise(0.006, 0.011) is True
    assert search_mod.selection_is_noise(0.030, 0.011) is False
    assert search_mod.selection_is_noise(None, 0.011) is False


# =====================================================================================
# 9. Candidate registration -- and the one clause chapter 6 makes unsatisfiable
# =====================================================================================
def _candidate_kwargs(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "family": "pulmo.effusion-candidate",
        "version": "1.0.0",
        "org_id": "org_pulmoai",
        "signing_identity": SIGNER,
        "capability_ids": ["pleural_effusion"],
        "preprocessing_spec_ref": "ps_pulmo_effusion_prep_2_0_0",
        "golden_fixture": {"sha256": "sha256:" + "0c" * 32,
                           "output_tensor_sha256": "sha256:" + "b9" * 32,
                           "output_shape": [1, 2, 128, 192, 192]},
        "io": {
            "input": {"name": "input", "shape": [1, 1, 128, 192, 192],
                      "dtype": "float32", "layout": "NCZYX", "orientation": "LPS"},
            "output": {"name": "logits", "shape": [1, 2, 128, 192, 192],
                       "dtype": "float32", "layout": "NCZYX",
                       "kind": "segmentation_logits",
                       "label_map": {"0": "background", "1": "pleural_effusion"}},
        },
        "runtime": {"engine": "triton", "engine_version": "24.08",
                    "backend": "onnxruntime", "gpu_architectures": ["sm_86"]},
        "weights": {"oci_ref": "ghcr.io/pulmoai/effusion-unet",
                    "digest": "sha256:" + "5e" * 32, "format": "onnx",
                    "size_bytes": 1024},
        "operating_point": {"kind": "probability_threshold", "score_threshold": 0.45,
                            "selected_on_evaluation_run": "er_01JP4T9X7B",
                            "selection_rule": "max F1 on the tune partition"},
        "not_validated_for": ["studies with slice thickness > 3.0 mm"],
        "known_failure_modes": ["loculated effusions are under-segmented"],
    }
    base.update(over)
    return base


def _succeeded_run(conn: psycopg.Connection[Any], c: Cohort, **over: Any) -> tr.RunRow:
    run = _submit(conn, c, **over)
    tr.start(conn, run_id=run.id, runner="worker-1",
             fingerprint_digest="sha256:" + "ab" * 32)
    return tr.succeed(conn, run_id=run.id, bundle_digest="sha256:" + "22" * 32)


def test_registering_a_candidate_binds_it_to_exactly_one_producing_run(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-138` / `MOS-TRAIN-139`, everything except the `null` clause."""
    run = _succeeded_run(train_db, cohort)
    manifest = cand.candidate_manifest(
        **_candidate_kwargs(evaluation_run_ref="er_01JP4T9X7B")
    )
    row, updated = cand.register_candidate(
        train_db, run_id=run.id, manifest=manifest, public_id="mv_effusion_cand_1_0_0",
        actor=SERVICE_ACTOR, trace_id=TRACE, **SUPPLY_CHAIN,
    )
    assert row["lifecycle_status"] == "REGISTERED"
    assert str(updated["candidate_model_version_id"]) == str(row["id"])
    assert manifest["spec"]["derived_from"] is None

    validating = cand.begin_validation(
        train_db, model_version_id=row["public_id"], actor=SERVICE_ACTOR, trace_id=TRACE
    )
    assert validating["lifecycle_status"] == "VALIDATING"
    validated = cand.finish_validation(
        train_db, model_version_id=row["public_id"], succeeded=True,
        actor=SERVICE_ACTOR, trace_id=TRACE,
    )
    assert validated["lifecycle_status"] == "VALIDATED"
    assert cand.PIPELINE_STATUSES == ("REGISTERED", "VALIDATING", "VALIDATED")
    assert "artifact.approve" not in cand.PIPELINE_PERMISSIONS

    # MOS-TRAIN-138: every run has at most one candidate.
    with pytest.raises(RunRefused) as exc:
        cand.register_candidate(
            train_db, run_id=run.id,
            manifest=cand.candidate_manifest(
                **_candidate_kwargs(version="1.0.1",
                                    evaluation_run_ref="er_01JP4T9X7C")
            ),
            public_id="mv_effusion_cand_1_0_1", actor=SERVICE_ACTOR, trace_id=TRACE,
            **SUPPLY_CHAIN,
        )
    assert exc.value.refusals[0].code == "run_already_has_a_candidate"


def test_a_null_evaluation_run_id_is_refused_and_the_contradiction_is_named(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-138` versus `MOS-REG-030`: a live specification contradiction.

    `MOS-TRAIN-138` requires `spec.evaluation_run_id` NULL at `REGISTERED`, and it must be
    null because `MOS-TRAIN-139` creates the run strictly afterwards. Chapter 6's manifest
    types the member as a required non-nullable string, and the registry renders that
    faithfully. The two cannot both be satisfied.

    This test PINS the refusal so the defect is executable evidence rather than a silent
    gap, and it fails the moment chapter 6's schema types the member as
    `["string","null"]` -- which is the one-line fix. Reported, not patched.
    """
    run = _succeeded_run(train_db, cohort)
    manifest = cand.candidate_manifest(**_candidate_kwargs())
    assert manifest["spec"]["evaluation_run_id"] is None

    with pytest.raises(RunRefused) as exc:
        cand.register_candidate(
            train_db, run_id=run.id, manifest=manifest,
            public_id="mv_effusion_cand_1_0_0", actor=SERVICE_ACTOR, trace_id=TRACE,
            **SUPPLY_CHAIN,
        )
    refusal = exc.value.refusals[0]
    assert refusal.code == "candidate_cannot_be_registered_with_a_null_evaluation_run"
    assert refusal.detail["conflicting_requirements"] == [
        "MOS-TRAIN-138", "MOS-REG-030", "MOS-REG-031"
    ]
    # The second defect: chapter 6's `er_` prefix cannot match chapter 7's `evr_` ids.
    assert "evr_" in refusal.detail["second_defect"]


# =====================================================================================
# 10. The three-act promotion, and the absence of any automated path
# =====================================================================================
def test_the_three_acts_are_three_permissions_and_three_records() -> None:
    """`MOS-TRAIN-173`: "three distinct permissions, three distinct audit records"."""
    assert [a["id"] for a in promo.THREE_ACTS] == ["A1", "A2", "A3"]
    assert {a["permission"] for a in promo.THREE_ACTS} == {
        "evidence.report.issue", "artifact.approve", "deployment.approve_clinical"
    }
    assert [s["step"] for s in promo.PROMOTION_STEPS] == [1, 2, 3, 4, 5]
    assert [s["human"] for s in promo.PROMOTION_STEPS] == [False, False, False, True, True]


def test_a_service_account_can_neither_approve_nor_promote(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-174` / `MOS-TRAIN-152`: the write-time half, because "an automated
    approver is exactly the shape a well-intentioned automation of the last mile takes"."""
    run = _succeeded_run(train_db, cohort)
    row, _ = cand.register_candidate(
        train_db, run_id=run.id,
        manifest=cand.candidate_manifest(
            **_candidate_kwargs(evaluation_run_ref="er_01JP4T9X7B")
        ),
        public_id="mv_effusion_cand_1_0_0", actor=SERVICE_ACTOR, trace_id=TRACE,
        **SUPPLY_CHAIN,
    )
    cand.begin_validation(train_db, model_version_id=row["public_id"],
                          actor=SERVICE_ACTOR, trace_id=TRACE)
    cand.finish_validation(train_db, model_version_id=row["public_id"], succeeded=True,
                           actor=SERVICE_ACTOR, trace_id=TRACE)

    with pytest.raises(promo.ApprovalRefused, match="named human"):
        promo.approve_artifact(
            train_db, model_version_id=row["public_id"], actor=SERVICE_ACTOR,
            trace_id=TRACE,
            approval_rationale="the evidence is complete and the regression test passed",
            validation_report_id="vr_1", validation_report_digest="sha256:" + "1" * 64,
            gate_decision_id="gd_1",
        )
    train_db.rollback()

    approved = promo.approve_artifact(
        train_db, model_version_id=row["public_id"], actor=HUMAN_ACTOR, trace_id=TRACE,
        approval_rationale="the evidence is complete and the regression test passed on "
                           "the same cohort as the incumbent re-run",
        validation_report_id="vr_1", validation_report_digest="sha256:" + "1" * 64,
        gate_decision_id="gd_1",
    )
    assert approved["lifecycle_status"] == "APPROVED"
    assert "rationale:" in approved["status_reason"]

    # And the approval takes a scalar id -- MOS-TRAIN-232.
    params = inspect.signature(promo.approve_artifact).parameters
    assert "model_version_id" in params and "model_version_ids" not in params
    assert params["model_version_id"].annotation in (str, "str")


def test_approval_needs_a_rationale_a_report_and_a_gate_decision(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-179`: approval records all four, or it is not an approval."""
    run = _succeeded_run(train_db, cohort)
    row, _ = cand.register_candidate(
        train_db, run_id=run.id,
        manifest=cand.candidate_manifest(
            **_candidate_kwargs(evaluation_run_ref="er_01JP4T9X7B")
        ),
        public_id="mv_effusion_cand_1_0_0", actor=SERVICE_ACTOR, trace_id=TRACE,
        **SUPPLY_CHAIN,
    )
    for kwargs, message in (
        ({"approval_rationale": "too short"}, "at least 20 characters"),
        ({"validation_report_id": ""}, "validation_report_id"),
        ({"gate_decision_id": ""}, "deployment_gate_decisions"),
    ):
        args: dict[str, Any] = {
            "approval_rationale": "the evidence is complete and correctly bound to the "
                                  "candidate's own cohort",
            "validation_report_id": "vr_1",
            "validation_report_digest": "sha256:" + "1" * 64,
            "gate_decision_id": "gd_1",
        }
        args.update(kwargs)
        with pytest.raises(promo.ApprovalRefused, match=message):
            promo.approve_artifact(
                train_db, model_version_id=row["public_id"], actor=HUMAN_ACTOR,
                trace_id=TRACE, **args,
            )
        train_db.rollback()


def _acceptance_run(conn: psycopg.Connection[Any], c: Cohort) -> str:
    """One `evaluation_runs` row, so a deployment can satisfy 0008's clinical gate.

    `deployments_clinical_gate` (`MOS-STORE-261`) requires `approved_by`, `approved_at`,
    `acceptance_run_id` and `validation_report_id` together, and the acceptance run is a
    real foreign key. Chapter 7 owns the run; this builds the smallest one its own
    constructor accepts rather than inserting a row behind its back.
    """
    from medos.evidence import evaluation as ev_run

    row = ev_run.create_run(
        conn,
        kind="site_acceptance",
        capability_id="pleural_effusion",
        dataset_version_id=c.version_id,
        split_id=c.split_id,
        partition="tune",
        annotation_set_id=c.annotation_set_id,
        model_version_id="22222222-2222-2222-2222-222222222222",
        preprocessing_spec_id="prep.pulmo.effusion",
        preprocessing_spec_version=2,
        preprocessing_spec_digest="sha256:" + "d3" * 32,
        code_commit=COMMIT,
        image_digest=IMAGE,
        runner="acceptance-harness",
        fp_volume_threshold_ml=5.0,
    )
    return row.id


def _insert_deployment_sql(
    conn: psycopg.Connection[Any],
    *,
    subject_id: str,
    clinical: bool,
    auto_promote: bool,
) -> None:
    """A raw insert, so the DATABASE's answer is what is being asserted, not the library's."""
    from medos.sdk.canonical import new_ulid

    conn.execute(
        """
        INSERT INTO deployments (public_id, tenant_id, environment, capability_id,
            subject_kind, subject_id, subject_version, role, state, clinical_use_mode,
            promotion_policy, created_by)
        VALUES (%s, %s, 'production', 'pleural_effusion', 'model_version', %s, '1.0.0',
                'STANDBY', 'PENDING', %s, %s, %s)
        """,
        (
            new_ulid("dep"), _uuid.UUID(DEFAULT_TENANT_ID), subject_id,
            "clinical" if clinical else "research_only",
            json.dumps({"auto_promote": auto_promote}),
            _uuid.UUID(OPERATOR),
        ),
    )


def test_auto_promote_is_refused_beside_a_clinical_deployment(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-184`, in the database, both ways round, plus the Python message.

    "`auto_promote: true` MUST be refused for **any** deployment in a `(tenant,
    environment)` where the same `capability_id` has any `clinical_use_mode: clinical`
    deployment. A research canary configured to auto-promote, sharing a capability slot
    with a clinical deployment, is one `clinical_use_mode` edit away from the automated
    production-to-serving path this chapter exists to forbid."
    """
    # (a) The narrow form MOS-REG-080 already forbids: clinical AND auto_promote on one
    #     row. A BEFORE INSERT trigger fires ahead of the table CHECK, so this branch is
    #     reached without the row having to satisfy 0008's clinical gate.
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        _insert_deployment_sql(train_db, subject_id="mv_a", clinical=True,
                               auto_promote=True)
    assert "auto_promote MUST be false for a clinical deployment" in str(exc.value)
    train_db.rollback()

    # (b) The wider form this chapter adds: a research row beside a clinical sibling.
    acceptance = _acceptance_run(train_db, cohort)
    _deployment(train_db, "mv_a", clinical=True, acceptance_run_id=acceptance)
    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        _insert_deployment_sql(train_db, subject_id="mv_b", clinical=False,
                               auto_promote=True)
    assert "already has a clinical deployment" in str(exc.value)
    train_db.rollback()

    # (c) The Python half names the same rule before the write is attempted.
    with pytest.raises(promo.ApprovalRefused, match="MOS-TRAIN-184"):
        promo.assert_no_auto_promote(
            train_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
            capability_id="pleural_effusion", promotion_policy={"auto_promote": True},
        )
    # ... and says nothing when there is nothing to say.
    promo.assert_no_auto_promote(
        train_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id="pleural_effusion", promotion_policy={"auto_promote": False},
    )


def _deployment(
    conn: psycopg.Connection[Any],
    subject_id: str,
    *,
    clinical: bool = False,
    acceptance_run_id: str | None = None,
    role: str = "ACTIVE",
    state: str = "SERVING",
    version: str = "1.0.0",
) -> str:
    """One deployment row, committed, so a later rollback in a test does not remove it."""
    from medos.sdk.canonical import new_ulid

    if clinical and acceptance_run_id is None:
        raise AssertionError(
            "0008's deployments_clinical_gate requires approved_by, approved_at, "
            "acceptance_run_id and validation_report_id together (MOS-STORE-261)"
        )
    public_id = new_ulid("dep")
    row = conn.execute(
        """
        INSERT INTO deployments (public_id, tenant_id, environment, capability_id,
            subject_kind, subject_id, subject_version, role, state, traffic_permille,
            clinical_use_mode, verification_ref, approved_by, approved_at,
            acceptance_run_id, validation_report_id, created_by, activated_at)
        VALUES (%s, %s, 'production', 'pleural_effusion', 'model_version', %s, %s, %s,
                %s, %s, %s, 'selftest/ok', %s, %s, %s, %s, %s, now())
        RETURNING id
        """,
        (
            public_id, _uuid.UUID(DEFAULT_TENANT_ID), subject_id, version, role, state,
            1000 if role == "ACTIVE" else 0,
            "clinical" if clinical else "research_only",
            _uuid.UUID(HUMAN_ID) if clinical else None,
            datetime.now(UTC) if clinical else None,
            _uuid.UUID(str(acceptance_run_id)) if acceptance_run_id else None,
            _uuid.uuid4() if clinical else None,
            _uuid.UUID(OPERATOR),
        ),
    ).fetchone()
    conn.commit()
    return str(dict(row)["id"])


def test_the_rollback_window_is_a_capacity_question_answered_before_step_1() -> None:
    """`MOS-TRAIN-186`: "refused while it is still a capacity question"."""
    ok = promo.assert_rollback_window_affordable(
        incumbent_weights_bytes=4_000_000_000,
        candidate_weights_bytes=4_000_000_000,
        budget=promo.ResidencyBudget(total_bytes=40_000_000_000, reserved_bytes=0),
    )
    assert ok["rollback_window_days"] == promo.DEFAULT_ROLLBACK_WINDOW_DAYS

    with pytest.raises(promo.PromotionError, match="capacity question"):
        promo.assert_rollback_window_affordable(
            incumbent_weights_bytes=9_000_000_000,
            candidate_weights_bytes=9_000_000_000,
            budget=promo.ResidencyBudget(total_bytes=16_000_000_000, reserved_bytes=0),
        )


def test_promotion_mints_a_row_swaps_roles_and_rolls_back(
    train_db: psycopg.Connection[Any], cohort: Cohort
) -> None:
    """`MOS-TRAIN-180` to `MOS-TRAIN-188`: steps 1, 2, 4 and 5, then the reverse swap."""
    incumbent_artifact = _publish_model(
        train_db, "mv_incumbent_1_0_0", "pulmo.effusion", "1.0.0"
    )
    candidate_artifact = _publish_model(
        train_db, "mv_candidate_1_1_0", "pulmo.effusion", "1.1.0",
        evaluation_run_id="er_01JQ0F6D3SAAA",
    )
    incumbent_id = _deployment(train_db, incumbent_artifact["public_id"])

    reservation = promo.assert_rollback_window_affordable(
        incumbent_weights_bytes=1_000_000, candidate_weights_bytes=1_000_000,
        budget=promo.ResidencyBudget(total_bytes=10_000_000, reserved_bytes=0),
    )
    candidate = promo.create_candidate_deployment(
        train_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id="pleural_effusion", subject_kind="model_version",
        subject_id=candidate_artifact["public_id"], subject_version="1.1.0",
        created_by=OPERATOR, actor=SERVICE_ACTOR, trace_id=TRACE,
        rollback_reservation=reservation,
    )
    assert candidate.role == "STANDBY" and candidate.state == "PENDING"
    assert candidate.clinical_use_mode == "research_only"

    verified = promo.verify_candidate(
        train_db, tenant_id=DEFAULT_TENANT_ID, deployment_id=candidate.id,
        verification_ref="selftest/golden-fixture-ok", actor=SERVICE_ACTOR,
        trace_id=TRACE,
    )
    assert verified.state == "SERVING" and verified.role == "STANDBY"

    # Step 5 requires a human and the promote permission -- and no verdict at all.
    with pytest.raises(promo.ApprovalRefused, match="named human"):
        promo.cutover(
            train_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
            capability_id="pleural_effusion", candidate_deployment_id=candidate.id,
            actor=SERVICE_ACTOR, trace_id=TRACE, reason="the gate says PASS",
            permissions=["deployment.promote"],
        )
    train_db.rollback()

    result = promo.cutover(
        train_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id="pleural_effusion", candidate_deployment_id=candidate.id,
        actor=HUMAN_ACTOR, trace_id=TRACE,
        reason="scheduled maintenance window, radiology informed",
        permissions=["deployment.promote"],
    )
    assert result["incumbent"] is not None

    from medos.evidence.deployment import active_deployment, load_deployment

    live = active_deployment(train_db, tenant_id=DEFAULT_TENANT_ID,
                             environment="production",
                             capability_id="pleural_effusion")
    assert live is not None and live.id == candidate.id
    outgoing = load_deployment(train_db, tenant_id=DEFAULT_TENANT_ID,
                               deployment_id=incumbent_id)
    assert outgoing is not None
    # MOS-TRAIN-183/186: warm, not retired.
    assert outgoing.role == "STANDBY" and outgoing.state == "SERVING"

    # MOS-TRAIN-180: two rows for the slot, and neither subject was rewritten.
    n = train_db.execute(
        "SELECT count(*) AS n FROM deployments WHERE capability_id = 'pleural_effusion' "
        "AND environment = 'production'"
    ).fetchone()["n"]
    assert n == 2

    rolled = promo.rollback(
        train_db, tenant_id=DEFAULT_TENANT_ID, environment="production",
        capability_id="pleural_effusion", target_deployment_id=incumbent_id,
        actor=HUMAN_ACTOR, trace_id=TRACE,
        reason="an unexplained rise in empty-mask outputs on thin-slice studies",
        permissions=["deployment.promote"],
    )
    assert rolled["restored"] == outgoing.public_id
    # MOS-TRAIN-188: the rolled-back candidate does not stay APPROVED.
    assert rolled["suspended_artifact"] == candidate_artifact["public_id"]
    back = active_deployment(train_db, tenant_id=DEFAULT_TENANT_ID,
                             environment="production",
                             capability_id="pleural_effusion")
    assert back is not None and back.id == incumbent_id


def test_a_serving_deployment_subject_cannot_be_rewritten_by_the_application(
    train_db: psycopg.Connection[Any]
) -> None:
    """`MOS-TRAIN-180`'s trigger half, which 0008 built and 0013 leaves alone."""
    artifact = _publish_model(train_db, "mv_x_1_0_0", "pulmo.effusion", "1.0.0")
    dep = _deployment(train_db, artifact["public_id"])
    with pytest.raises(psycopg.DatabaseError):
        train_db.execute(
            "UPDATE deployments SET subject_id = 'mv_other' WHERE id = %s",
            (_uuid.UUID(dep),),
        )
    train_db.rollback()


# =====================================================================================
# 11. The dossier
# =====================================================================================
def _dossier_kwargs(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "candidate": {"model_id": "mv_candidate_1_1_0", "version": "1.1.0",
                      "content_digest": "sha256:" + "1" * 64, "derived_from": None,
                      "preprocessing_spec_ref": "ps_a",
                      "preprocessing_spec_digest": "sha256:" + "2" * 64,
                      "bundle_metadata_version": "1.0.0",
                      "training_run_id": "tr_01ARZ3NDEKTSV4RRFFQ69G5FAV"},
        "incumbent": {"model_version": "mv_incumbent_1_0_0", "deployment_id": "dep_a",
                      "activated_at": "2026-01-01T00:00:00Z", "serving_for": "70 days",
                      "same_cohort_rerun": {"evaluation_run_id": "evr_b",
                                            "metrics": {"dice_mean_per_case": 0.884}},
                      "historical_figures": [{"metric": "dice_mean_per_case",
                                              "value": 0.901}]},
        "cohort": {"dataset_version_digest": "sha256:" + "3" * 64, "patient_count": 240,
                   "test_partition_n_patients": 80, "acquisition_profile": {},
                   "licence": "CC-BY-3.0",
                   "deidentification_status": "public_deidentified"},
        "reference_standard": {"readers": 3, "consensus_rule": "majority_at_least_2",
                               "reference_of_record": True,
                               "inter_reader": {"agreement_dice_mean_per_case": 0.84},
                               "headline_metric": {"dice_mean_per_case": 0.82}},
        "leakage": {"checks": CLEAN_LEAKAGE, "waivers": [], "patient_key_aliases": []},
        "verdict": {"verdict": "PASS", "criteria_version": 3,
                    "criterion_results": [{"id": "dice_positive", "outcome": "PASS"},
                                          {"id": "small_effusion", "outcome": "SKIPPED"}]},
        "regression": {"mean_delta": 0.003, "ci_lower_95_one_sided": -0.008,
                       "margin": 0.02, "n": 80, "n_patients": 80,
                       "margin_rationale": "the smallest difference a reader calls",
                       "catastrophic_count": 0, "evaluation_run_id": "evr_a"},
        "strata": [{"id": "all", "n": 80, "value": 0.88, "evaluation_run_id": "evr_a"},
                   {"id": "small_effusion", "n": 6, "value": 0.61,
                    "evaluation_run_id": "evr_a"}],
        "seed_variance": {"metric": "dice_mean_per_case", "runs": 3, "sd": 0.011},
        "publisher_declarations": {
            "not_validated_for": ["slice thickness > 3.0 mm"],
            "known_failure_modes": ["loculated effusions are under-segmented"],
        },
        "plausibility": [{"rule": "volume_absurdity", "severity": "fail",
                          "firing_rate": 0.0, "exercised": True,
                          "evaluation_run_id": "evr_a"}],
        "scope": {"would_affect": ["production/pleural_effusion"],
                  "would_not_affect": ["staging/pleural_effusion"]},
    }
    base.update(over)
    return base


def test_the_dossier_renders_fifteen_items_and_marks_an_underpowered_stratum() -> None:
    """`MOS-TRAIN-176`, `MOS-TRAIN-177`, `MOS-TRAIN-178`."""
    doc = dossier_mod.render(**_dossier_kwargs())
    assert doc["computes_nothing"] is True
    assert len(doc["items"]) == 15
    assert set(doc["items"]) == {
        f"{n}_{name.lower().replace(' ', '_')}" for n, name in dossier_mod.DOSSIER_ITEMS
    }
    small = doc["items"]["8_strata"][1]
    assert small["value"] is None and "underpowered" in small["marker"]
    assert doc["items"]["2_incumbent"]["figures"]["source"].startswith("same-cohort")
    assert doc["items"]["2_incumbent"]["historical_figures"][0]["label"].startswith(
        "historical"
    )
    assert doc["items"]["7_regression"]["evaluation_run_id"] == "evr_a"


def test_the_dossier_refuses_an_incumbent_with_no_same_cohort_rerun() -> None:
    """`MOS-TRAIN-177`: a comparison that was never performed."""
    kwargs = _dossier_kwargs()
    kwargs["incumbent"] = {**kwargs["incumbent"], "same_cohort_rerun": {}}
    with pytest.raises(dossier_mod.DossierRefused, match="MOS-TRAIN-177"):
        dossier_mod.render(**kwargs)


def test_the_conversion_block_cannot_be_rendered_without_its_own_run() -> None:
    """Chapter 17 acceptance check 10, and `MOS-TRAIN-165`'s statement verbatim."""
    block = conv.evaluate_equivalence(_cases(), precision="fp16")
    with pytest.raises(dossier_mod.DossierRefused, match="MOS-TRAIN-165"):
        dossier_mod.render(
            **_dossier_kwargs(),
            conversion={"conversion_run_id": "cv_a", "equivalence": block,
                        "evaluation_run": {}},
        )

    doc = dossier_mod.render(
        **_dossier_kwargs(),
        conversion={
            "conversion_run_id": "cv_a", "equivalence": block,
            "evaluation_run": {"public_id": "evr_c", "state": "SUCCEEDED",
                               "verdict": "PASS"},
        },
    )
    assert doc["items"]["10_conversion"]["statement"] == conv.EQUIVALENCE_DISCLAIMER
    assert doc["items"]["10_conversion"]["own_evaluation_run"][
        "evaluation_run_id"
    ] == "evr_c"


def test_a_selection_inside_the_noise_carries_the_literal_marker() -> None:
    """`MOS-TRAIN-176` item 15, and `MOS-TRAIN-236`'s arithmetic made visible."""
    search = {
        "search_id": "cs_a", "trials_completed": 12,
        "space_digest": "sha256:" + "9" * 64,
        "selection_metric": "dice_mean_per_case", "selection_partition": "tune",
        "selection_rule": "argmax on tune", "selection_margin": 0.006,
        "cost": {"gpu_hours_used": 81.4, "stop_reason": "space_exhausted"},
        "ensemble": {"combination_rule": "mean_probability",
                     "members": [{"training_run_id": "tr_a"},
                                 {"training_run_id": "tr_b"}],
                     "per_member": [{"dice": 0.88}, {"dice": 0.87}]},
    }
    doc = dossier_mod.render(
        **_dossier_kwargs(), configuration_search=search, test_exposure_count=3
    )
    item = doc["items"]["15_configuration_search"]
    assert item["marker"] == dossier_mod.NOISE_MARKER
    assert item["selection_margin_vs_seed_sd"] == {"selection_margin": 0.006,
                                                   "seed_variance_sd": 0.011}
    assert item["test_exposure_count"] == 3
    # MOS-TRAIN-229: per-member figures are not in the rendered item.
    assert "per_member" not in json.dumps(item)
    assert item["ensemble"]["member_count"] == 2


# =====================================================================================
# 12. MOS-TRAIN-189 -- the call-graph property, asserted over the import closure
# =====================================================================================
_FORBIDDEN_FROM_TRAINING = (
    "medos.promotion",
    "medos.evidence.deployment",
)


def _closure(package: str) -> set[str]:
    """Every first-party module transitively imported by `package`, by static read.

    A static read rather than `sys.modules`: an import that only happens inside a function
    body is still a path, and `MOS-TRAIN-189` is about paths rather than about what a
    particular process happened to load.
    """
    import ast

    root = Path(importlib.import_module(package).__file__).parent
    seen: set[str] = set()
    frontier = [
        f"{package}.{m.name}" for m in pkgutil.iter_modules([str(root)])
    ] + [package]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, AttributeError, ValueError):
            # `from medos.x import Y` where Y is a class, not a submodule. Not a module
            # edge, so there is nothing to walk; ignorable and named as such.
            continue
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            continue
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("medos."):
                        frontier.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("medos."):
                    frontier.append(node.module)
                    for alias in node.names:
                        frontier.append(f"{node.module}.{alias.name}")
    return seen


def test_no_symbol_reachable_from_the_pipeline_reaches_a_deployment_mutation() -> None:
    """The 0.3.0 gate check `no-auto-promote`. `MOS-TRAIN-189`, `MOS-TRAIN-233`.

    "CI MUST assert this as a call-graph property: no symbol reachable from the pipeline
    package transitively reaches the deployment role-mutation or `clinical_use_mode`
    transition functions. That assertion, together with the grant query of
    `MOS-TRAIN-174`, is what makes this chapter's ruling structural rather than
    procedural."
    """
    closure = _closure("medos.training")
    offenders = sorted(
        m for m in closure
        if any(m == f or m.startswith(f + ".") for f in _FORBIDDEN_FROM_TRAINING)
    )
    assert offenders == [], (
        "medos.training reaches the deployment side: "
        f"{offenders}. MOS-TRAIN-189 forbids any path from a TrainingRun, a "
        "ConversionRun, an EvaluationRun or a ValidationReport to a SERVING Deployment "
        "that does not pass through steps 4 and 5 of MOS-TRAIN-181."
    )
    # ... and the promotion package genuinely does reach it, so the assertion above is
    # not passing because the two halves are both empty.
    assert any(
        m.startswith("medos.evidence.deployment") for m in _closure("medos.promotion")
    )


def test_the_pipeline_contains_no_direct_comparison_of_two_aggregate_metrics() -> None:
    """`MOS-TRAIN-145` / `MOS-TRAIN-149`, chapter 17 acceptance check 17.

    "at zero true effect, `new < old` on a point estimate blocks with probability 0.5, and
    that probability does not fall with sample size ... a gate that blocks half of all
    harmless rebuilds is switched off within two weeks."

    The grep is for the shape the naive comparison takes: a comparison against a name
    containing `incumbent` or `baseline`. The evidence plane's paired non-inferiority test
    (`MOS-EVID-085`) is the only blocking comparison in the platform.
    """
    import re as _re

    pattern = _re.compile(
        r"(candidate|new)[_a-z]*\s*(<|>|<=|>=)\s*(incumbent|baseline|old)[_a-z]*"
    )
    root = Path(importlib.import_module("medos.training").__file__).parent
    hits = [
        f"{path.name}:{i}"
        for path in sorted(root.glob("*.py"))
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line) and not line.lstrip().startswith("#")
    ]
    assert hits == [], f"a direct aggregate comparison on a blocking path: {hits}"
