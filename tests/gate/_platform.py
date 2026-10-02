# SPDX-License-Identifier: Apache-2.0
"""The release-0.3.0 gate's world: a registry, a sealed cohort, a frozen split.

WHAT THIS IS, AND WHY IT IS NOT AN IMPORT FROM `tests/integration/`
---------------------------------------------------------------------
Four of the nine checks in §15.1.2's 0.3.0 row need rows in a database:
`resolution-purity` needs a registry and a job, `leakage-blocks-training` needs a split
with a leak in it, `served-plan-equivalence` needs a published `ModelVersion`, and
`no-auto-promote` needs the `deployments` table. This module builds all four from the
platform's own public API and nothing else.

It does NOT import the integration suite's fixtures, for the reason `tests/gate/conftest.py`
gives about the 0.1.0 row and `tests/gate/_evidence.py` repeats for 0.2.0: a gate module
that reaches into `tests/integration/` inherits that suite's fixtures, its skip conditions
and every future edit to it, and then a release gate can be turned red -- or, far worse,
green -- by a change nobody made to the release. The shapes below are deliberately similar
to the integration suite's; the coupling is not.

WHAT IT RUNS AGAINST
---------------------
`tests/gate/conftest.py`'s `platform_db`: a throwaway database on the deployment's own
Postgres server, with `schema.sql` and migrations 0002-0014 applied from empty. That is a
second database from the 0.2.0 row's, and the conftest says why.

PHI (CONTRACT.md §11)
---------------------
Every identifier below is synthetic. `patient_key` is a salted digest of a made-up MRN, the
UIDs are in this project's test arc, and nothing here is read from a corpus.

Spec: MOS-EVID-016, MOS-EVID-034, MOS-EVID-036, MOS-REG-051..MOS-REG-071, MOS-TRAIN-115,
MOS-TRAIN-116, MOS-TRAIN-124, MOS-TRAIN-153, MOS-TRAIN-190, MOS-TRAIN-209.
"""

from __future__ import annotations

import hashlib
import random
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
from medos.db import audit
from medos.db.tenancy import DEFAULT_TENANT_ID
from medos.evidence import digest as ev_digest
from medos.evidence import repo as ev_repo
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.store import InMemoryManifestStore
from medos.registry import repo as registry_repo
from medos.registry.digest import content_digest_of
from medos.resolution.model import (
    Capability,
    DeploymentRow,
    GateConfig,
    MetricPoint,
    ModelVersionRow,
    PreprocRow,
    ServiceVersionRow,
    Snapshot,
    TenantPin,
    TenantProfile,
)
from medos.training import runs as tr

BUCKET = "medos-evidence"
SALT = b"release-0-3-0-gate-salt-not-a-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"
HUMAN_ID = "44444444-4444-4444-4444-444444444444"
TENANT = DEFAULT_TENANT_ID
TRACE = "0af7651916cd43dd8448eb211c80319c"
COMMIT = "b" * 40
IMAGE = "sha256:" + "c" * 64
SIGNER = (
    "https://github.com/pulmoai/lung-nodule/.github/workflows/release.yml@refs/tags/v1"
)

SERVICE_ACTOR = audit.Actor(kind="service_account", id=OPERATOR, auth="api_key")
HUMAN_ACTOR = audit.Actor(kind="user", id=HUMAN_ID, auth="oidc")

SUPPLY_CHAIN: dict[str, Any] = {
    "oci_image_digest": "sha256:" + "a1" * 32,
    "signature": b"cosign-bundle-bytes",
    "signature_alg": "cosign-sigstore",
    "signer_identity": SIGNER,
    "sbom_object_key": "sbom/gate.cdx.json",
}

#: `MOS-TRAIN-124`'s four reproducibility blocks, complete. Written once so that a check
#: about something else cannot omit one and pass for the wrong reason.
SEEDS = {
    "python": 20260311,
    "numpy": 20260311,
    "torch": 20260311,
    "dataloader_worker_base": 900,
}
DETERMINISM = {
    "torch_use_deterministic_algorithms": True,
    "cudnn_benchmark": False,
    "cublas_workspace_config": ":4096:8",
    "tf32_allowed": False,
}
HARDWARE = {
    "gpu_model": "NVIDIA A100-SXM4-80GB",
    "gpu_count": 2,
    "driver": "550.54.15",
    "cuda": "12.4",
    "cudnn": "9.1.0",
    "nccl": "2.21.5",
}
FRAMEWORKS = {"torch": "2.4.1", "monai": "1.4.0", "numpy": "1.26.4", "simpleitk": "2.3.1"}

CAPABILITY = "pleural_effusion"
FAMILY = "pulmo.pleural-effusion"

#: A fixed clock. A gate run in June and a gate run in December must resolve identically;
#: `MOS-REG-051` makes `snapshot.as_of` the only time source the resolver may read.
AS_OF = datetime(2026, 1, 14, 8, 22, 19, tzinfo=UTC)


# =====================================================================================
# The cohort -- a sealed DatasetVersion, a frozen split, a frozen reference standard
# =====================================================================================
def _uid(*parts: int) -> str:
    return "1.2.826.0.1.3680043.10.9." + ".".join(str(p) for p in parts)


def _pixel_digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def series_record(
    patient: int, *, pixel_seed: str | None = None, study_year: int | None = None
) -> SeriesRecord:
    half = patient % 2
    return SeriesRecord(
        patient_key=ev_digest.patient_key(SALT, "TCIA/GATE-030", f"P{patient:04d}"),
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
        study_year=study_year if study_year is not None else 2022 + half,
        accession_number_hash=ev_digest.accession_number_hash(SALT, f"ACC{patient:04d}"),
    )


class Cohort:
    """The four ids and four digests a `RunBinding` has to pin, plus the leakage report."""

    def __init__(
        self,
        version_id: str,
        version_digest: str,
        split_id: str,
        split_digest: str,
        annotation_set_id: str,
        annotation_digest: str,
        leakage: dict[str, Any],
    ) -> None:
        self.version_id = version_id
        self.version_digest = version_digest
        self.split_id = split_id
        self.split_digest = split_digest
        self.annotation_set_id = annotation_set_id
        self.annotation_digest = annotation_digest
        self.leakage = leakage


def prepare_cohort(
    conn: psycopg.Connection[Any],
    store: InMemoryManifestStore,
    *,
    n_patients: int = 10,
    offset: int = 0,
    shared_pixels: tuple[int, int] | None = None,
    leak_partitions: tuple[str, str] = ("train", "tune"),
    study_years: tuple[int, int] | None = None,
    waivers: Sequence[dict[str, Any]] = (),
) -> Cohort:
    """Seal a cohort, freeze a `train`/`tune` split, freeze a reference standard.

    `shared_pixels` gives two DISTINCT patients the same `series_pixel_digest` and puts
    them on opposite sides of the split, which is how L3 of `MOS-EVID-034` is made to
    fail. L1 cannot be made to fail through this path at all: `PRIMARY KEY (split_id,
    patient_key)` makes one patient in two partitions structurally impossible
    (`MOS-STORE-293`) -- the schema doing its job. `MOS-TRAIN-115` treats L1, L2, L3 and
    L5 identically, so the blocking claim is unaffected by which of them is provoked, and
    `test_leakage_blocks_training.py` asserts that equivalence rather than assuming it.
    """
    records = []
    for p in range(n_patients):
        patient = offset + p
        seed = None
        year = None
        if shared_pixels is not None and p in shared_pixels:
            seed = f"shared/{offset}"
            if study_years is not None:
                year = study_years[shared_pixels.index(p)]
        records.append(series_record(patient, pixel_seed=seed, study_year=year))

    dataset = ev_repo.create_dataset(
        conn,
        slug=f"gate030-{secrets.token_hex(4)}",
        display_name="release-0.3.0 gate cohort",
        purpose="training",
        custodian="TCIA/GATE-030",
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
    assignments = [(k, "tune" if by_key[k] % 2 else "train") for k in keys]
    if shared_pixels is not None:
        a, b = shared_pixels
        left, right = leak_partitions
        forced = {records[a].patient_key: left, records[b].patient_key: right}
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
        capability_id=CAPABILITY,
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
                    {
                        "reader_id": f"rdr_a{i}",
                        "uri": f"s3://x/a{i}.nrrd",
                        "digest": _pixel_digest(f"a{i}/{r.series_instance_uid}"),
                        "volume_ml": 284.0 + i,
                    }
                    for i in range(3)
                ],
                "inter_reader": {
                    "agreement_dice_mean_per_case": 0.938,
                    "volume_difference_ml": 9.1,
                },
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


def run_binding(cohort: Cohort, **overrides: Any) -> tr.RunBinding:
    """A complete `MOS-TRAIN-124` binding. Every override is an explicit deviation."""
    kwargs: dict[str, Any] = {
        "capability_id": CAPABILITY,
        "dataset_version_id": cohort.version_id,
        "dataset_version_digest": cohort.version_digest,
        "split_id": cohort.split_id,
        "split_digest": cohort.split_digest,
        "annotation_set_id": cohort.annotation_set_id,
        "annotation_digest": cohort.annotation_digest,
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


# =====================================================================================
# The registry -- manifests chapter 6's seeded schemas accept
# =====================================================================================
def model_manifest(family: str, version: str, **spec_overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "capabilities": [CAPABILITY],
        "weights_availability": "platform_managed",
        "weights": {
            "oci_ref": "ghcr.io/pulmoai/effusion-unet",
            "digest": "sha256:" + "5e" * 32,
            "format": "onnx",
            "size_bytes": 184236032,
        },
        "preprocessing_spec_ref": "ps_pulmo_effusion_prep_2_0_0",
        "golden_fixture": {
            "sha256": "sha256:" + "0c" * 32,
            "output_tensor_sha256": "sha256:" + "b9" * 32,
            "output_shape": [1, 2, 128, 192, 192],
        },
        "io": {
            "input": {
                "name": "input",
                "shape": [1, 1, 128, 192, 192],
                "dtype": "float32",
                "layout": "NCZYX",
                "orientation": "LPS",
            },
            "output": {
                "name": "logits",
                "shape": [1, 2, 128, 192, 192],
                "dtype": "float32",
                "layout": "NCZYX",
                "kind": "segmentation_logits",
                "label_map": {"0": "background", "1": "pleural_effusion"},
            },
        },
        "operating_point": {
            "kind": "probability_threshold",
            "score_threshold": 0.45,
            "selected_on_evaluation_run": "er_01JP4T9X7B",
            "selection_rule": "max F1 on the tune partition",
        },
        "runtime": {
            "engine": "triton",
            "engine_version": "24.08",
            "backend": "onnxruntime",
            "gpu_architectures": ["sm_80", "sm_86"],
        },
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
        "created_at": "2026-01-09T11:02:41Z",
        "spec": spec,
    }
    manifest["content_digest"] = content_digest_of(manifest)
    return manifest


def service_manifest(version: str, model_ref: str, *, family: str = FAMILY) -> dict[str, Any]:
    manifest = {
        "schema_version": "1.0.0",
        "kind": "service_version",
        "family": family,
        "version": version,
        "publisher": {"org_id": "org_pulmoai", "signing_identity": SIGNER},
        "created_at": "2026-01-09T11:02:41Z",
        "spec": {
            "mode": "native",
            "image": {
                "ref": "ghcr.io/pulmoai/pleural-effusion",
                "digest": "sha256:" + "a1" * 32,
            },
            "capabilities": [{"id": CAPABILITY, "outputs": ["segmentation", "measurement"]}],
            "modalities": ["CT"],
            "series_selector_ref": "sel_chest_ct_thin_axial_v3",
            "models": [{"ref": model_ref, "role": "primary"}],
            "preprocessing_specs": [],
            "resources": {
                "gpu_required": True,
                "gpu_memory_mib": 9216,
                "cpu_millicores": 4000,
                "memory_mib": 24576,
                "max_concurrent_jobs": 2,
            },
            "engineering_acceptance": {
                "p95_wall_clock_seconds": 180,
                "max_gpu_memory_mib": 11264,
                "result_bundle_schema_validity": 1.0,
            },
            "legal_manufacturer": {
                "id": "lm_pulmoai_gmbh",
                "name": "PulmoAI GmbH",
                "device_serial_number": "PULMO-EFF-0003",
                "software_versions": version,
            },
            "regulatory_status": [
                {"jurisdiction": "EU", "status": "not_a_medical_device", "evidence_ref": None}
            ],
            "compatibility": {
                "medicalos_api": ">=1.0 <2",
                "service_contract": "1.2",
                "runtime": {
                    "engine": "triton",
                    "engine_version": ">=24.08 <25.00",
                    "backend": "onnxruntime",
                    "gpu_architectures": ["sm_80", "sm_86"],
                    "cuda": ">=12.1 <13",
                    "driver_min": "535.104.05",
                    "gpu_memory_mib": 9216,
                },
            },
        },
    }
    manifest["content_digest"] = content_digest_of(manifest)
    return manifest


def publish(
    conn: psycopg.Connection[Any],
    manifest: dict[str, Any],
    public_id: str,
    *,
    status: str = "APPROVED",
) -> dict[str, Any]:
    """Register an artifact and walk it up the lifecycle to `status`."""
    row, _created = registry_repo.publish(
        conn,
        manifest=manifest,
        public_id=public_id,
        actor=SERVICE_ACTOR,
        trace_id=TRACE,
        **SUPPLY_CHAIN,
    )
    conn.commit()
    for step in ("VALIDATING", "VALIDATED", "APPROVED"):
        row = registry_repo.set_status(
            conn, public_id, to_status=step, reason=None, actor=SERVICE_ACTOR, trace_id=TRACE
        )
        if step == status:
            break
    conn.commit()
    return row


def deploy(
    conn: psycopg.Connection[Any],
    public_id: str,
    subject: str,
    version: str,
    *,
    role: str = "ACTIVE",
    state: str = "SERVING",
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO deployments (public_id, tenant_id, environment, capability_id,
            subject_kind, subject_id, subject_version, role, state, clinical_use_mode,
            pin_range, verification_ref, created_by)
        VALUES (%s, %s, 'production', %s, 'service_version', %s, %s, %s, %s,
                'research_only', %s, 'ver_gate_030', %s)
        """,
        (public_id, TENANT, CAPABILITY, subject, version, role, state, "^3.0.0", OPERATOR),
    )
    if commit:
        conn.commit()


# =====================================================================================
# The frozen snapshot, and the generator `resolution-purity` property-tests over
# =====================================================================================
def _service_row(
    sid: str,
    version: str,
    *,
    status: str = "APPROVED",
    family: str = FAMILY,
    models: tuple[str, ...] = ("mv_a",),
    modalities: tuple[str, ...] = ("CT",),
    capabilities: tuple[str, ...] = (CAPABILITY,),
) -> ServiceVersionRow:
    return ServiceVersionRow(
        id=sid,
        family=family,
        version=version,
        lifecycle_status=status,
        content_digest="sha256:" + sid.encode("utf-8").hex().ljust(64, "0")[:64],
        tenant_id=None,
        capabilities=capabilities,
        modalities=modalities,
        model_version_refs=models,
        preprocessing_spec_refs=("ps_a",),
        gpu_architectures=("sm_80",),
        gpu_required=False,
        gpu_memory_mib=9216,
        legal_manufacturer_id="lm_pulmoai_gmbh",
        regulatory_jurisdictions=("EU",),
        image_digest="sha256:" + "a1" * 32,
    )


def _deployment_row(
    did: str, subject: str, *, role: str = "ACTIVE", state: str = "SERVING", permille: int = 0
) -> DeploymentRow:
    return DeploymentRow(
        id=did,
        tenant_id=TENANT,
        environment="production",
        capability_id=CAPABILITY,
        subject_id=subject,
        role=role,
        state=state,
        traffic_permille=permille,
        clinical_use_mode="research_only",
    )


def random_snapshot(rng: random.Random, *, n: int = 6) -> Snapshot:
    """One random registry state. `MOS-REG-053`'s "10 000 random registry states".

    Every axis the resolver's filter stages read is varied: lifecycle status (F3),
    deployment role and state (the ranking's deployment_rank), traffic permille (the
    canary bucket), family (the pin's P2 key), modality (F4), capability (F1), the
    acceptance metric (P4, including its absence), the tenant pin and the two gateable
    filters. A generator that varied one axis would property-test one branch.
    """
    services: list[ServiceVersionRow] = []
    deployments: list[DeploymentRow] = []
    metrics: dict[str, MetricPoint] = {}
    families = (FAMILY, "pulmo.effusion-classic")
    for i in range(n):
        sid = f"sv_{i}"
        version = f"{rng.randint(0, 4)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}"
        services.append(
            _service_row(
                sid,
                version,
                status=rng.choice(
                    [
                        "APPROVED", "VALIDATED", "DEPRECATED", "SUSPENDED",
                        "RECALLED", "DRAFT", "REGISTERED", "VALIDATING",
                    ]
                ),
                family=rng.choice(families),
                models=rng.choice([("mv_a",), ("mv_suspended",), ("mv_a", "mv_b")]),
                modalities=rng.choice([("CT",), ("MR",), ("CT", "MR")]),
                capabilities=rng.choice([(CAPABILITY,), ("lung_segmentation",)]),
            )
        )
        if rng.random() < 0.8:
            deployments.append(
                _deployment_row(
                    f"dep_{i}",
                    sid,
                    role=rng.choice(["ACTIVE", "CANARY", "SHADOW", "STANDBY"]),
                    state=rng.choice(["SERVING", "PENDING", "DRAINING", "RETIRED"]),
                    permille=rng.choice([0, 100, 1000]),
                )
            )
        if rng.random() < 0.5:
            metrics[f"{sid}|{CAPABILITY}|dv_acceptance"] = MetricPoint(
                value=round(rng.random(), 4), metric="dice_coefficient"
            )
    pins: tuple[TenantPin, ...] = ()
    if rng.random() < 0.4:
        pins = (
            TenantPin(
                tenant_id=TENANT,
                capability_id=CAPABILITY,
                service_family=rng.choice(families),
                version_range=rng.choice(["", ">=2 <5", "^3.0.0", "=3.2.1"]),
            ),
        )
    return Snapshot(
        epoch=rng.randint(1, 10**6),
        as_of=AS_OF,
        capabilities={
            CAPABILITY: Capability(id=CAPABILITY, primary_metric="dice_coefficient")
        },
        services=tuple(services),
        models={
            "mv_a": ModelVersionRow("mv_a", "f", "1.0.0", "APPROVED"),
            "mv_b": ModelVersionRow("mv_b", "f", "1.1.0", "VALIDATED"),
            "mv_suspended": ModelVersionRow("mv_suspended", "f", "2.0.0", "SUSPENDED"),
        },
        preproc={"ps_a": PreprocRow("ps_a", "f", "2.0.0", "APPROVED")},
        deployments=tuple(deployments),
        tenant_pins=pins,
        metrics=metrics,
        tenants={TENANT: TenantProfile(TENANT, "EU")},
        acceptance_datasets={f"{TENANT}|{CAPABILITY}": "dv_acceptance"},
        gate_config=GateConfig(
            evidence_gate=rng.random() < 0.2, regulatory_gate=rng.random() < 0.2
        ),
    ).with_identity()


def context(**overrides: Any) -> dict[str, Any]:
    """The `{modality, tenant_id, environment, study_constraints}` argument."""
    ctx: dict[str, Any] = {
        "tenant_id": TENANT,
        "environment": "production",
        "modality": "CT",
        "study_constraints": {"study_id": "stu_4410", "slice_thickness_mm": 1.0},
    }
    ctx.update(overrides)
    return ctx
