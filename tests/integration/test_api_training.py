# SPDX-License-Identifier: Apache-2.0
"""The model-development surface over real HTTP. Table 10.2-B rows `R6`-`R17`.

WHAT THIS FILE IS TRYING TO CATCH, and why each one is a test rather than a review

  1. A ROUTE THAT DISAGREES WITH ITS OWN REGISTRY. `medos/api/v1/routes.train.yaml` now says
     `status: served` for twelve rows. `tests/unit/test_permission_contract.py` asserts
     the app serves exactly those paths; THIS file asserts that what comes back is the
     schema the registry names, member for member, because a path that answers 200 with
     the wrong body is a served row in the same sense that a stub is an implementation.

  2. A SILENT SUCCESS. Section 19.3.1 says what this surface refuses to be and 19.3.4 is
     titled "Seal -- the refusal surface". A no-code operator cannot audit a silent
     success, so every gate the engine enforces is provoked here and the RESPONSE BODY is
     asserted -- the status, the `class`, the machine-readable `code`, and the
     `refusals[]` array with the `check_id` that names the requirement. A refusal that
     arrives as a 500, or as a 200 with a null field, is the defect.

  3. `monai_supervised` REACHED BY A NO-CODE CLIENT. `MOS-UI-148` fixes the console to a
     dataset-fingerprint auto-configuring backend and forbids offering `monai_supervised`;
     `MOS-TRAIN-211` makes a hand-configured run record a rationale of 20+ characters
     naming what the auto-configured baseline failed to do. Three tests below hold the
     biconditional from both sides, so the console cannot reach the backend by accident
     and an expert cannot reach it without the sentence.

  4. A PROMOTION PATH THAT GREW BACK. 19.3.7 is blocked by design. `R12` is asserted to
     carry four consts, no metric, and no member whose name contains `url`, `href`,
     `link` or `action` -- the shape `MOS-UI-167` requires to be ABSENT rather than
     disabled, and `MOS-TRAIN-011` calls non-conformant when it is one number and a
     button.

  5. A CROSS-TENANT READ. Every query runs under the `medicalos.tenant_id` GUC with
     FORCE ROW LEVEL SECURITY, and a second tenant's run is asserted to be a 404 and not
     a 403 -- telling a caller that a row it may not read exists is the leak.

WHAT THIS FILE DOES NOT COVER, AND WHERE IT MOVED TO. This paragraph used to say there
was no HTTP path to open a harvest batch, decide a candidate or seal a cohort. All three
exist now -- `R26` since the change that served `R18`-`R31`, and `R1`-`R5` since the one
that served MOS-API-112's five (docs/spec/99-known-inconsistencies.md entry 88). The
cohort these tests need is still built through `medos.evidence.repo`, deliberately: this
file is about the RUN surface, and a fixture that drove the whole assembly chain would
make every test here fail for a reason upstream of the route it is about.
`tests/integration/test_api_curation_walk.py` drives the chain end to end over HTTP.
Everything from the submit onwards is real HTTP here.

SKIP DISCIPLINE: `tests/_support/skips.py` only, through the `tests/integration` prefix's
`postgres` declaration in `tests/_support/stack.py`.

Spec: MOS-API-004, MOS-API-015, MOS-API-035, MOS-API-036, MOS-API-037, MOS-API-046,
MOS-API-089, MOS-API-112, MOS-SEC-008, MOS-SEC-072, MOS-TRAIN-072, MOS-TRAIN-115,
MOS-TRAIN-124, MOS-TRAIN-127, MOS-TRAIN-211, MOS-TRAIN-216, MOS-TRAIN-218, MOS-TRAIN-234,
MOS-UI-102, MOS-UI-147, MOS-UI-148, MOS-UI-149, MOS-UI-163, MOS-UI-167, MOS-UI-168.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from medos.api.problems import PROBLEM_MEDIA_TYPE, WIRE_CLASSES
from medos.api.routes_training import ENVIRONMENT_VAR, PERMISSIONS

# THE TRAIN DEPLOYABLE'S FACTORY, not Core's. `medos.api.app:create_app` builds the
# PACS-and-models service, which does not mount the training or curation routers --
# the routes this module exercises are served by `medos-train-api`. Building Core
# here would 404 on every one of them.
from medos.api.training_plane import create_training_app as create_app  # noqa: E402
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import digest as ev_digest
from medos.evidence import repo as ev_repo
from medos.evidence.manifest import Acquisition, SeriesRecord
from medos.evidence.store import InMemoryManifestStore
from medos.training import policy as pol
from medos.training import runs as tr
from psycopg.rows import dict_row

pytestmark = pytest.mark.slow

BUCKET = "medos-evidence"
SALT = b"test-tenant-salt-not-a-real-secret"
OPERATOR = "11111111-1111-1111-1111-111111111111"
CAPABILITY = "pleural_effusion"
COMMIT = "b" * 40
IMAGE = "sha256:" + "c" * 64
SPEC_DIGEST = "sha256:" + "d3" * 32

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

#: The deployment declaration `medos/medos/api/routes_training.py` reads. Every member is a
#: `MOS-TRAIN-124` input that describes the DEPLOYMENT rather than the cohort; the module
#: docstring argues why they come from here and names the gap that remains.
ENVIRONMENT: dict[str, Any] = {
    "code_commit": COMMIT,
    "code_dirty": False,
    "image_digest": IMAGE,
    "backend_versions": {"nnunet": "2.5.1", "auto3dseg": "1.4.0",
                         "monai_supervised": "1.4.0"},
    "seeds": {"python": 20260311, "numpy": 20260311, "torch": 20260311,
              "dataloader_worker_base": 900},
    "determinism": {"torch_use_deterministic_algorithms": True, "cudnn_benchmark": False,
                    "cublas_workspace_config": ":4096:8", "tf32_allowed": False},
    "hardware": {"gpu_model": "NVIDIA A100-SXM4-80GB", "gpu_count": 2,
                 "driver": "550.54.15", "cuda": "12.4", "cudnn": "9.1.0",
                 "nccl": "2.21.5"},
    "framework_versions": {"torch": "2.4.1", "monai": "1.4.0", "numpy": "1.26.4",
                           "simpleitk": "2.3.1"},
    "preprocessing": {
        CAPABILITY: {
            "id": "prep.pulmo.effusion",
            "version": 2,
            "digest": SPEC_DIGEST,
            "output_kind": "label",
        },
        # A second capability whose output is not `label`, so that MOS-TRAIN-211's
        # "the default applies to a label capability and to no other" has a subject.
        "nodule_burden": {
            "id": "prep.pulmo.burden",
            "version": 1,
            "digest": "sha256:" + "e4" * 32,
            "output_kind": "probability",
        },
    },
}


# =====================================================================================
# Server, credentials and database fixtures
# =====================================================================================
def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def _running_server(app: Any) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def training_url(pg_dsn: str) -> Iterator[str]:
    """One app over the throwaway database. `MOS-REL-046`: an injected factory, never a
    module-level connection, which is what lets this server coexist with `test_api.py`'s
    in the same process."""

    def opener(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(pg_dsn, row_factory=dict_row, **kwargs)

    app = create_app(connect=opener, configure_logs=False)
    with _running_server(app) as url:
        yield url


def _issue(pg_dsn: str, scope: list[str], label: str) -> str:
    from medos.security import store

    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        minted, _record = store.issue(
            conn,
            tenant_id=DEFAULT_TENANT_ID,
            principal_kind="service_account",
            principal_id=OPERATOR,
            created_by=store.BOOTSTRAP_OPERATOR_ID,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scope=scope,
            label=label,
            env="dev",
        )
        conn.commit()
    return str(minted.plaintext)


@pytest.fixture(scope="session")
def scientist_key(pg_dsn: str) -> str:
    """A credential carrying all nine permissions these twelve rows bind.

    `medos/medos/security/scopes.py` is emphatic that a scope entry is a CEILING and not a
    grant (`MOS-SEC-027`, `MOS-SEC-045`): there is no roles table, so holding the entry is
    necessary and is not yet sufficient anywhere. What this fixture buys is the ability to
    assert the NECESSARY half -- see `test_every_route_refuses_a_credential_without_its
    _permission`, which drives the same routes with the ceiling removed.
    """
    return _issue(pg_dsn, sorted(PERMISSIONS), "training surface, full ceiling")


@pytest.fixture(scope="session")
def reader_key(pg_dsn: str) -> str:
    """`training_run.read` and nothing else: the ceiling of a console that may look."""
    return _issue(pg_dsn, ["training_run.read"], "training surface, read only")


@pytest.fixture()
def scientist(training_url: str, scientist_key: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=training_url,
        timeout=30.0,
        headers={"Authorization": f"Bearer {scientist_key}"},
    ) as c:
        yield c


@pytest.fixture()
def reader(training_url: str, reader_key: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=training_url,
        timeout=30.0,
        headers={"Authorization": f"Bearer {reader_key}"},
    ) as c:
        yield c


@pytest.fixture()
def declared_environment() -> Iterator[None]:
    """The deployment's training declaration, for the duration of one test.

    Set on `os.environ` and not on the app, because `load_training_environment()` reads it
    per request on purpose: an environment cached at import is one an operator cannot
    correct without a restart (CONTRACT.md section 11 forbids the module-level cache that
    would make the caching possible).
    """
    previous = os.environ.get(ENVIRONMENT_VAR)
    os.environ[ENVIRONMENT_VAR] = json.dumps(ENVIRONMENT)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENVIRONMENT_VAR, None)
        else:  # pragma: no cover - only when a developer exports it
            os.environ[ENVIRONMENT_VAR] = previous


@pytest.fixture()
def tdb(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One connection with the default tenant bound and this release's tables emptied.

    TRUNCATE rather than DELETE: every table here carries a BEFORE DELETE guard and
    TRUNCATE fires no row-level triggers, so the append-only guarantee holds for
    application code while the harness starts clean.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(
        "TRUNCATE " + ", ".join(TRAINING_TABLES + EVIDENCE_TABLES)
        + ", training_data_policies CASCADE"
    )
    conn.execute("UPDATE tenants SET training_use_allowed = false")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant

        reset_current_tenant(token)
        conn.close()


@pytest.fixture()
def permitted(tdb: psycopg.Connection[Any]) -> None:
    """A tenant that may contribute to a training corpus. `MOS-TRAIN-072`/`MOS-TRAIN-073`."""
    pol.record_policy(
        tdb,
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
    pol.set_training_use_allowed(tdb, True)
    tdb.commit()


# =====================================================================================
# The cohort. Built through the engine, because no HTTP path seals one -- see the
# module docstring.
# =====================================================================================
def _uid(*parts: int) -> str:
    return "1.2.826.0.1.3680043.10.9." + ".".join(str(p) for p in parts)


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
    def __init__(self, version_id: str, split_id: str, annotation_set_id: str,
                 split_digest: str, version_digest: str,
                 annotation_digest: str) -> None:
        self.version_id = version_id
        self.split_id = split_id
        self.annotation_set_id = annotation_set_id
        self.split_digest = split_digest
        self.version_digest = version_digest
        self.annotation_digest = annotation_digest

    def body(self, **overrides: Any) -> dict[str, Any]:
        """`MOS-UI-147`'s whole request: four ids, and nothing a client may get wrong."""
        body: dict[str, Any] = {
            "capability_id": CAPABILITY,
            "dataset_version_id": self.version_id,
            "split_id": self.split_id,
            "annotation_set_id": self.annotation_set_id,
        }
        body.update(overrides)
        return body


def _prepare(
    conn: psycopg.Connection[Any],
    store: InMemoryManifestStore,
    *,
    n_patients: int = 10,
    offset: int = 0,
    shared_pixels: tuple[int, int] | None = None,
    waivers: tuple[dict[str, Any], ...] = (),
) -> Cohort:
    """Seal a cohort, freeze a train/tune split, freeze a reference standard over it.

    `shared_pixels` puts one `series_pixel_digest` on two distinct patients on opposite
    sides of the split, which is how L3 of `MOS-EVID-034` is made to fail. L1 cannot be
    made to fail through this path -- `PRIMARY KEY (split_id, patient_key)` makes a
    patient in two partitions structurally impossible -- and `waiver_blocks_training`
    treats L1, L2, L3 and L5 identically, which is why L3 is the lever.
    """
    # `offset` shifts the patient identities, which shifts every digest. Two cohorts
    # built from IDENTICAL records are one `dataset_versions` row: `manifest_digest` is
    # unique, which is MOS-EVID-015 doing its job, so a test that needs two distinct
    # cohorts must give them distinct content rather than distinct names.
    records = []
    for p in range(n_patients):
        seed = f"shared/{offset}" if shared_pixels and p in shared_pixels else None
        records.append(_record(offset + p, pixel_seed=seed))

    dataset = ev_repo.create_dataset(
        conn,
        slug=f"api-train-{secrets.token_hex(4)}",
        display_name="training cohort over HTTP",
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
    conn.commit()
    return Cohort(
        sealed.id,
        split.id,
        annotations.id,
        split.split_digest,
        sealed.manifest_digest,
        annotations.annotation_digest,
    )


@pytest.fixture()
def store() -> InMemoryManifestStore:
    return InMemoryManifestStore()


@pytest.fixture()
def cohort(
    tdb: psycopg.Connection[Any], store: InMemoryManifestStore, permitted: None
) -> Cohort:
    return _prepare(tdb, store)


# =====================================================================================
# Assertions shared by every refusal test
# =====================================================================================
def assert_problem(
    response: httpx.Response, *, status: int, cls: str, code: str
) -> dict[str, Any]:
    """Every member `MOS-API-036` / `MOS-API-037` make mandatory, on every document.

    `type` is NOT asserted to start with `PROBLEM_BASE` here, and that is deliberate:
    `docs/spec/99-known-inconsistencies.md` entry 80 records that
    `medos/medos/training/errors.py` spells its types under `https://medicalos.dev/problems/`
    while `MOS-API-036` names `https://spec.medicalos.org/problems/`, and the handler
    emits WHAT THE ENGINE RAISES rather than resolving a platform-wide question inside one
    router. `test_the_refusal_type_is_the_one_the_engine_raises` pins the divergence so it
    cannot be closed by accident in either direction.
    """
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    doc = response.json()
    assert doc["status"] == status
    assert doc["class"] in WIRE_CLASSES  # MOS-API-038: the enum is closed
    assert doc["class"] == cls, doc
    assert doc["code"] == code, doc
    assert doc["retryable"] is False
    assert doc["trace_id"] == response.headers["MedicalOS-Trace-Id"]
    assert doc["instance"]
    assert doc["occurred_at"]
    assert doc["detail"]
    return dict(doc)


def check_ids(doc: dict[str, Any]) -> set[str]:
    return {r["check_id"] for r in doc.get("refusals", [])}


def codes(doc: dict[str, Any]) -> set[str]:
    return {r["code"] for r in doc.get("refusals", [])}


# =====================================================================================
# R6 -- the happy path, and what the record binds
# =====================================================================================
def test_submit_records_the_full_binding_from_four_client_supplied_ids(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-UI-147` and `MOS-TRAIN-124` in one assertion: four ids in, a bound run out.

    The point is not that a row was created; it is that EVERY member `MOS-TRAIN-124`
    requires is populated and NONE of them came from the request. A client that could
    supply `hardware.gpu_model` could record a GPU the run did not use, and that value
    ends up in a `ValidationReport`.
    """
    response = scientist.post("/api/v1/training-runs", json=cohort.body())
    assert response.status_code == 202, response.text
    run = response.json()

    assert run["training_run_id"].startswith("tr_")
    assert response.headers["location"] == f"/api/v1/training-runs/{run['training_run_id']}"
    assert run["state"] == "PENDING"

    # The three digests are the server's, read off the sealed and frozen rows.
    assert run["split_digest"] == cohort.split_digest
    assert run["dataset_version_digest"].startswith("sha256:")
    assert run["annotation_digest"].startswith("sha256:")

    # The deployment's half of the binding, from the declaration and not from the body.
    assert run["code_commit"] == COMMIT
    assert run["code_dirty"] is False
    assert run["image_digest"] == IMAGE
    assert run["preprocessing_spec_id"] == "prep.pulmo.effusion"
    assert run["preprocessing_spec_version"] == 2
    assert run["preprocessing_spec_digest"] == SPEC_DIGEST
    assert run["hardware"]["gpu_model"] == "NVIDIA A100-SXM4-80GB"
    assert run["seeds"]["torch"] == 20260311
    assert run["determinism"]["cudnn_benchmark"] is False
    assert run["framework_versions"]["torch"] == "2.4.1"

    # MOS-UI-148: the console omitted the member and got an auto-configuring backend.
    assert run["training_backend"]["kind"] == "nnunet"
    assert run["training_backend"]["version"] == "2.5.1"
    # MOS-TRAIN-135 freezes the derived plan at RUN START, so there is none yet.
    assert run["training_backend"]["plan_digest"] is None
    assert run["backend_rationale"] is None

    # MOS-TRAIN-141 on the wire: neither partition was ever the caller's to choose.
    assert run["fit_partition"] == "train"
    assert run["select_partition"] == "tune"
    assert run["run_digest"].startswith("sha256:")


def test_the_response_carries_every_member_of_the_training_run_schema_and_no_other(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """The registry names `TrainingRun` as R6's 202 body; this is that claim, checked.

    `additionalProperties: false` is half the schema's content and the omissions are the
    other half: `hyperparameters` (MOS-UI-149 forbids the batch size being readable off
    the wire), `search_trial_score` (MOS-TRAIN-222 forbids a selection statistic on a UI
    surface) and the four object-store addresses (MOS-TRAIN-141's argument applied to a
    client that can name its own objects).
    """
    schema = json.loads(
        (_repo_root() / "medos" / "schemas" / "training" / "training-run-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )
    run = scientist.post("/api/v1/training-runs", json=cohort.body()).json()
    assert set(run) == set(schema["required"])
    for forbidden in ("hyperparameters", "search_trial_score", "bundle_bucket",
                      "bundle_object_key", "fingerprint_bucket", "fingerprint_object_key",
                      "tenant_id", "id"):
        assert forbidden not in run, forbidden


def _repo_root() -> Any:
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


# =====================================================================================
# THE REFUSALS
# =====================================================================================
def test_training_use_not_permitted_is_the_403_mos_api_112_pins(
    scientist: httpx.Client,
    tdb: psycopg.Connection[Any],
    store: InMemoryManifestStore,
    declared_environment: None,
) -> None:
    """`MOS-TRAIN-072` / `MOS-API-112`: 403, `authz_error`, `TRAINING_USE_NOT_PERMITTED`,
    `retryable: false`.

    The `permitted` fixture is deliberately NOT requested: the tenant flag is false, which
    is the state `MOS-TRAIN-072` writes about -- "There is no per-study, per-user or
    per-environment override, and no platform-administrator bypass." A cohort is still
    built, so the refusal is about the tenant and not about a missing row.
    """
    pol.set_training_use_allowed(tdb, False)
    tdb.commit()
    cohort = _prepare(tdb, store)

    response = scientist.post("/api/v1/training-runs", json=cohort.body())
    doc = assert_problem(
        response, status=403, cls="authz_error", code="TRAINING_USE_NOT_PERMITTED"
    )
    assert check_ids(doc) == {"MOS-TRAIN-072"}
    assert codes(doc) == {"training_use_not_permitted"}
    assert "no per-study, per-user or per-environment override" in json.dumps(doc)
    # And nothing was created: a refusal that also wrote a row is not a refusal.
    assert tr.list_runs(tdb) == ()


def test_the_refusal_type_is_the_one_the_engine_raises_and_the_class_is_normalised(
    scientist: httpx.Client, tdb: psycopg.Connection[Any], store: InMemoryManifestStore,
    declared_environment: None,
) -> None:
    """Register entry 80, pinned from the wire so it cannot close by accident.

    The `type` authority is a PLATFORM-WIDE decision over thirteen problem types and this
    surface does not take it: the document carries `https://medicalos.dev/...`, which is
    what `medos/medos/training/errors.py` raises and therefore what a client sees today. The
    `class` is the half that CANNOT be carried through: `authorization` is not one of
    table 10.4-A's six values and `MOS-API-038` closes the enum, so the boundary maps it
    to `authz_error` -- which is independently what `MOS-API-112` pins for this slug.
    """
    pol.set_training_use_allowed(tdb, False)
    tdb.commit()
    cohort = _prepare(tdb, store)
    doc = scientist.post("/api/v1/training-runs", json=cohort.body()).json()

    assert doc["type"] == "https://medicalos.dev/problems/training-use-not-permitted"
    from medos.sdk.errors import HarvestRefused

    assert doc["type"] == HarvestRefused.problem_type
    assert doc["class"] == "authz_error"
    assert doc["class"] != "authorization"


def test_a_leaking_split_refuses_the_submit_before_a_run_exists(
    scientist: httpx.Client,
    tdb: psycopg.Connection[Any],
    store: InMemoryManifestStore,
    permitted: None,
    declared_environment: None,
) -> None:
    """`MOS-TRAIN-115` / `MOS-TRAIN-116`: the leakage check BLOCKS, and a waiver does not
    lift it -- "a waiver permits a REPORT to be written about the cohort; it does not
    permit a model to be fitted on it".

    Asserted over HTTP because that is where the operator meets it: `MOS-UI-151` requires
    the console to surface the refusal rather than let it arrive as a dead run, and a 422
    whose body names the failing check and the offending `patient_key`s is what makes that
    possible.
    """
    # The split is frozen under a MOS-EVID-036 waiver, which is LEGITIMATE -- a waiver is
    # a statement about what a report may claim -- and is what makes the point: the run
    # is refused anyway, and the waiver counts for nothing.
    waiver = {
        "check_id": "L3",
        "waived_by": OPERATOR,
        "waived_at": "2026-03-11T00:00:00Z",
        "rationale": "two acquisitions of one phantom; retained deliberately",
        "affected_pairs": [],
    }
    leaking = _prepare(tdb, store, shared_pixels=(0, 1), waivers=(waiver,))
    response = scientist.post("/api/v1/training-runs", json=leaking.body())
    doc = assert_problem(
        response, status=422, cls="clinical_rejection", code="TRAINING_RUN_REFUSED"
    )
    assert "MOS-TRAIN-115" in check_ids(doc)
    assert "leakage_blocks_training" in codes(doc)
    blob = json.dumps(doc)
    assert "L3" in blob
    assert "a waiver permits a REPORT" in blob or "does not permit" in blob
    assert tr.list_runs(tdb) == ()


def test_monai_supervised_without_a_rationale_is_refused_before_a_connection_opens(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-UI-148` and `MOS-TRAIN-211`, as a property of the API and not of a screen.

    This is the test that makes "the console MUST NOT offer `monai_supervised`" more than
    a styling rule: a client that reaches for the backend without the sentence
    `MOS-TRAIN-211` requires gets a 400 with a JSON pointer, and the run does not exist.
    """
    response = scientist.post(
        "/api/v1/training-runs",
        json=cohort.body(training_backend={"kind": "monai_supervised"}),
    )
    doc = assert_problem(
        response, status=400, cls="client_error", code="SCHEMA_VIOLATION"
    )
    assert "MOS-TRAIN-211" in json.dumps(doc)


def test_a_rationale_without_a_deviation_is_refused_too(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """The other side of the biconditional. A rationale supplied for an auto-configured
    run is refused rather than silently dropped: `backend_rationale` names what the
    auto-configured baseline failed to do, and an auto-configuring run has no baseline to
    have deviated from. 0013 carries the same pair as CHECKs."""
    response = scientist.post(
        "/api/v1/training-runs",
        json=cohort.body(
            backend_rationale="the auto-configured plan under-segmented the fissure"
        ),
    )
    assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")


def test_a_hand_configured_run_with_a_rationale_is_admitted_and_records_it(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-211` contemplates a hand-configured run as a LEGITIMATE expert act.

    The rationale "is not a gate; it exists so that the first question at the approval
    gate is answerable: was this compared against the default, or did nobody run the
    default?" So the expert path works, and the sentence is on the record.
    """
    rationale = "auto3dseg under-segmented the costophrenic recess on 12 of 40 cases"
    assert len(rationale) >= 20
    response = scientist.post(
        "/api/v1/training-runs",
        json=cohort.body(
            training_backend={"kind": "monai_supervised"}, backend_rationale=rationale
        ),
    )
    assert response.status_code == 202, response.text
    run = response.json()
    assert run["training_backend"]["kind"] == "monai_supervised"
    assert run["backend_rationale"] == rationale


def test_the_submit_body_has_no_member_a_no_code_client_could_tune(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-UI-149`'s six quantities, none of which exists on this API under any name.

    `extra="forbid"` is what turns "the screen does not offer a control" into "the control
    does not exist", and `fit_partition` is in the same list for `MOS-TRAIN-141`'s reason:
    a caller that can name its own partition can name `test`.
    """
    for member, value in (
        ("hyperparameters", {"batch_size": 4}),
        ("batch_size", 4),
        ("patch_size", [64, 192, 192]),
        ("target_spacing", [1.0, 1.0, 1.0]),
        ("fit_partition", "test"),
        ("select_partition", "test"),
        ("path", "s3://bucket/test/"),
    ):
        response = scientist.post(
            "/api/v1/training-runs", json=cohort.body(**{member: value})
        )
        assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")


def test_an_undeclared_training_environment_is_a_platform_fault_not_a_client_error(
    scientist: httpx.Client, cohort: Cohort
) -> None:
    """`MOS-UI-160`: the operator is told which side the fault is on.

    `MOS-TRAIN-124` binds inputs that describe the deployment and this platform has no
    table for them, so a deployment that never declared its training image cannot record
    a binding. Nothing the caller changes in the body will help, which is why the class is
    `system_failure` and `retryable` is false -- a 503 defaulting to `transport_failure`
    would tell a client to try again forever.
    """
    os.environ.pop(ENVIRONMENT_VAR, None)
    response = scientist.post("/api/v1/training-runs", json=cohort.body())
    doc = assert_problem(
        response,
        status=503,
        cls="system_failure",
        code="TRAINING_ENVIRONMENT_NOT_RECORDED",
    )
    assert doc["environment_variable"] == ENVIRONMENT_VAR
    assert "code_commit" in doc["missing"]
    assert "preprocessing" in doc["missing"]


def test_a_capability_with_no_auto_configuring_default_is_refused_with_its_reason(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-211` fixes a default for a `label` capability and for no other.

    The alternative -- picking `nnunet` anyway -- would be the API deciding something no
    requirement authorises and recording it as though a rule had. The refusal names the
    observed `output_kind`, so an operator can see why the console cannot train this one.
    """
    response = scientist.post(
        "/api/v1/training-runs", json=cohort.body(capability_id="nodule_burden")
    )
    doc = assert_problem(
        response, status=422, cls="client_error", code="TRAINING_BACKEND_NOT_DERIVABLE"
    )
    assert doc["output_kind"] == "probability"


def test_an_unknown_capability_is_named_as_an_undeclared_binding(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-IMG-045` makes the `PreprocessingSpec` the registered thing a run is fitted
    against; a capability this deployment has bound no spec for cannot have a
    `MOS-TRAIN-124` binding, and the refusal says which ones it HAS bound rather than
    leaving the operator to guess."""
    response = scientist.post(
        "/api/v1/training-runs", json=cohort.body(capability_id="no_such_capability")
    )
    doc = assert_problem(
        response,
        status=503,
        cls="system_failure",
        code="TRAINING_ENVIRONMENT_NOT_RECORDED",
    )
    assert doc["missing"] == ["preprocessing.no_such_capability"]
    assert CAPABILITY in doc["detail"]


def test_an_unknown_cohort_row_is_a_404_naming_which_one(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    absent = "00000000-0000-0000-0000-0000000000ff"
    for member, code in (
        ("dataset_version_id", "DATASET_VERSION_NOT_FOUND"),
        ("split_id", "DATASET_SPLIT_NOT_FOUND"),
        ("annotation_set_id", "ANNOTATION_SET_NOT_FOUND"),
    ):
        response = scientist.post(
            "/api/v1/training-runs", json=cohort.body(**{member: absent})
        )
        assert_problem(response, status=404, cls="client_error", code=code)


def test_resubmitting_the_same_binding_is_a_conflict_and_not_a_second_run(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None,
    tdb: psycopg.Connection[Any],
) -> None:
    """0013's `UNIQUE (tenant_id, run_digest)`, and `MOS-TRAIN-124` is what makes that
    digest mean "the same experiment".

    `medos/api/v1/routes.train.yaml` records that `Idempotency-Key` is only `optional` on R6
    because `MOS-API-022`'s enumeration does not reach it. This is the narrower guarantee that
    holds anyway: the same binding submitted twice is one run and a 409, not two runs
    nobody can tell apart.
    """
    first = scientist.post("/api/v1/training-runs", json=cohort.body())
    assert first.status_code == 202
    second = scientist.post("/api/v1/training-runs", json=cohort.body())
    assert_problem(
        second, status=409, cls="client_error", code="TRAINING_RUN_DIGEST_CONFLICT"
    )
    assert len(tr.list_runs(tdb)) == 1


# =====================================================================================
# R7, R8, R9 -- read, list, cancel
# =====================================================================================
def test_the_run_is_readable_and_listable_by_the_ids_the_submit_returned(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    created = scientist.post("/api/v1/training-runs", json=cohort.body()).json()
    rid = created["training_run_id"]

    one = scientist.get(f"/api/v1/training-runs/{rid}")
    assert one.status_code == 200
    assert one.json() == created

    page = scientist.get("/api/v1/training-runs", params={"capability_id": CAPABILITY})
    assert page.status_code == 200
    body = page.json()
    assert set(body) == {"items", "next_cursor", "has_more"}  # MOS-API-015, closed
    assert [i["training_run_id"] for i in body["items"]] == [rid]
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    # MOS-API-016: no `total`. Counting is an unbounded scan over a per-run table.
    assert "total" not in body


def test_the_list_filters_are_bound_columns_and_a_malformed_one_is_a_400(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    scientist.post("/api/v1/training-runs", json=cohort.body())
    assert scientist.get(
        "/api/v1/training-runs", params={"split_digest": cohort.split_digest}
    ).json()["items"]
    assert not scientist.get(
        "/api/v1/training-runs", params={"capability_id": "nodule_burden"}
    ).json()["items"]
    assert_problem(
        scientist.get("/api/v1/training-runs", params={"split_digest": "deadbeef"}),
        status=400,
        cls="client_error",
        code="INVALID_SPLIT_DIGEST",
    )


def test_has_more_is_honest_when_the_page_is_bounded(
    scientist: httpx.Client, cohort: Cohort, tdb: psycopg.Connection[Any],
    store: InMemoryManifestStore, declared_environment: None
) -> None:
    """`next_cursor` is always null and `has_more` is not a guess.

    `medos.training.runs.list_runs` takes `limit` and four bound columns and no cursor, so
    the continuation `MOS-API-017` describes cannot be served without a second query
    written in the HTTP layer -- the thing CONTRACT.md section 1 forbids. The page is
    therefore bounded and SAYS SO; a client seeing `has_more: true` narrows its filters.
    This test exists so that the gap is a recorded behaviour rather than a surprise.
    """
    second = _prepare(tdb, store, offset=100)
    scientist.post("/api/v1/training-runs", json=cohort.body())
    scientist.post("/api/v1/training-runs", json=second.body())
    page = scientist.get("/api/v1/training-runs", params={"limit": 1})
    body = page.json()
    assert len(body["items"]) == 1
    assert body["has_more"] is True
    assert body["next_cursor"] is None


def test_cancel_in_flight_is_an_act_and_a_second_cancel_is_a_409(
    scientist: httpx.Client, cohort: Cohort, tdb: psycopg.Connection[Any],
    declared_environment: None
) -> None:
    """`MOS-UI-156`: cancelling a run IN FLIGHT is a deliberate act attributed to a
    person, and a terminal run is not reopened.

    The run is started first because that is the case the requirement is about, and --
    see the next test -- it is also the only case this deployment can actually serve for
    an auto-configured backend. The body is empty and that is a decision: 0013 admits a
    `failure_reason` only on FAILED, so a cancellation reason has nowhere to be stored,
    and a field the server discards is a field a client will believe was recorded.
    """
    rid = scientist.post(
        "/api/v1/training-runs", json=cohort.body()
    ).json()["training_run_id"]
    tr.start(
        tdb, run_id=rid, runner="orchestrator/local",
        fingerprint_digest="sha256:" + "ab" * 32,
    )
    tdb.commit()

    cancelled = scientist.post(f"/api/v1/training-runs/{rid}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "CANCELLED"
    assert cancelled.json()["finished_at"] is not None

    again = scientist.post(f"/api/v1/training-runs/{rid}/cancel")
    doc = assert_problem(
        again, status=409, cls="client_error", code="TRAINING_RUN_NOT_CANCELLABLE"
    )
    assert "already terminal" in doc["detail"]


def test_a_hand_configured_run_can_be_cancelled_while_still_queued(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """The control case for the defect below: `monai_supervised` derives no plan, so
    0013's fingerprint CHECK has a branch for it and PENDING -> CANCELLED is admitted.
    Without this the next test would be asserting that cancel is broken generally, which
    it is not -- it is broken for exactly the backends the no-code console uses."""
    rid = scientist.post(
        "/api/v1/training-runs",
        json=cohort.body(
            training_backend={"kind": "monai_supervised"},
            backend_rationale="auto3dseg under-segmented the costophrenic recess",
        ),
    ).json()["training_run_id"]
    cancelled = scientist.post(f"/api/v1/training-runs/{rid}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "CANCELLED"


def test_cancelling_a_queued_auto_configured_run_is_a_named_platform_defect(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """A DEFECT THIS CHANGE FOUND BY SERVING R9, PINNED RATHER THAN HIDDEN.

    0013's `training_runs_fingerprint_frozen` is "state = 'PENDING' OR kind =
    'monai_supervised' OR fingerprint_digest IS NOT NULL", written on the argument that
    an auto-configuring backend freezes its plan at run START (MOS-TRAIN-135,
    MOS-TRAIN-223). CANCELLED-from-PENDING was not considered: the run leaves PENDING
    without passing through `start()`, has no digest, and the CHECK refuses it. So a
    QUEUED nnunet or auto3dseg run cannot be cancelled -- and MOS-UI-148 fixes the
    no-code console to exactly those two backends, which makes this the console's
    ordinary case rather than an edge one.

    The response is a `system_failure` and not a 409 on purpose: a 409 would say the
    platform refused deliberately, and `MOS-UI-156` requires the control. It is not a
    bare 500 either, because an operator told "internal error, quote the trace id"
    cannot tell this apart from a dead database and the two need different people.

    THIS TEST WILL FAIL WHEN THE DEFECT IS FIXED, which is the point: the fix is an edit
    to 0013 and to `medos/medos/training/runs.py`, and whoever makes it should have to come
    here and turn this into the 200 it ought to be.
    """
    rid = scientist.post(
        "/api/v1/training-runs", json=cohort.body()
    ).json()["training_run_id"]
    assert scientist.get(f"/api/v1/training-runs/{rid}").json()["state"] == "PENDING"

    response = scientist.post(f"/api/v1/training-runs/{rid}/cancel")
    doc = assert_problem(
        response, status=500, cls="system_failure", code="TRAINING_RUN_CANCEL_BLOCKED"
    )
    assert doc["blocking_constraint"] == "training_runs_fingerprint_frozen"
    assert "MOS-UI-156" in doc["detail"]
    # The run is untouched: a refusal that half-wrote the row would be worse than this.
    assert scientist.get(f"/api/v1/training-runs/{rid}").json()["state"] == "PENDING"


def test_a_malformed_or_absent_run_id_is_a_400_or_a_404_and_never_a_500(
    scientist: httpx.Client, declared_environment: None
) -> None:
    assert_problem(
        scientist.get("/api/v1/training-runs/not-a-ulid"),
        status=400,
        cls="client_error",
        code="INVALID_TRAINING_RUN_ID",
    )
    assert_problem(
        scientist.get("/api/v1/training-runs/tr_0123456789ABCDEFGHJKMNP0TV"),
        status=404,
        cls="client_error",
        code="TRAINING_RUN_NOT_FOUND",
    )


# =====================================================================================
# The walk: submit, watch, and the two reads 19.3.6 and 19.3.7 get
# =====================================================================================
def test_a_console_can_watch_a_run_reach_a_terminal_state(
    scientist: httpx.Client, cohort: Cohort, tdb: psycopg.Connection[Any],
    declared_environment: None
) -> None:
    """Submit over HTTP, then read the state change the orchestrator caused.

    `runs.start` and `runs.succeed` are driven through the engine deliberately: chapter 19
    gives the console no control that starts or completes a run, and `MOS-TRAIN-122` puts
    both in the orchestrator's hands. What the console does is exactly what this test does
    -- POST once, then GET until the state is terminal.
    """
    rid = scientist.post(
        "/api/v1/training-runs", json=cohort.body()
    ).json()["training_run_id"]
    assert scientist.get(f"/api/v1/training-runs/{rid}").json()["state"] == "PENDING"

    tr.start(
        tdb,
        run_id=rid,
        runner="orchestrator/local",
        fingerprint_digest="sha256:" + "ab" * 32,
    )
    tdb.commit()
    running = scientist.get(f"/api/v1/training-runs/{rid}").json()
    assert running["state"] == "RUNNING"
    assert running["runner"] == "orchestrator/local"
    assert running["fingerprint_digest"] == "sha256:" + "ab" * 32

    tr.succeed(tdb, run_id=rid, bundle_digest="sha256:" + "cd" * 32)
    tdb.commit()
    done = scientist.get(f"/api/v1/training-runs/{rid}").json()
    assert done["state"] == "SUCCEEDED"
    assert done["bundle_digest"] == "sha256:" + "cd" * 32
    assert done["finished_at"] is not None


def test_test_exposure_is_a_read_keyed_on_the_run_and_labelled_monotonic(
    scientist: httpx.Client, cohort: Cohort, tdb: psycopg.Connection[Any],
    declared_environment: None
) -> None:
    """Section 19.3.6's only row. `MOS-TRAIN-216`, `MOS-UI-163`.

    Zero before the first candidate run, which is the ordinary state of a freshly frozen
    split, and `monotonic` is a const the SERVER asserts rather than a sentence a console
    template claims. Keyed on the run because `MOS-UI-101` forbids making the operator
    compose a digest for a path segment.
    """
    rid = scientist.post(
        "/api/v1/training-runs", json=cohort.body()
    ).json()["training_run_id"]

    before = scientist.get(f"/api/v1/training-runs/{rid}/test-exposure")
    assert before.status_code == 200
    body = before.json()
    assert body["exposure_count"] == 0
    assert body["monotonic"] is True
    assert body["split_digest"] == cohort.split_digest
    assert body["capability_id"] == CAPABILITY
    assert body["first_exposed_at"] is None
    assert body["last_exposed_at"] is None

    tr.note_test_exposure(
        tdb, capability_id=CAPABILITY, split_digest=cohort.split_digest
    )
    tdb.commit()
    after = scientist.get(f"/api/v1/training-runs/{rid}/test-exposure").json()
    assert after["exposure_count"] == 1
    assert after["first_exposed_at"] is not None
    assert after["last_exposed_at"] is not None


def test_seed_variance_answers_200_not_404_when_there_is_no_characterisation(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-127` / `MOS-UI-157`. A 404 cannot tell "no characterisation" apart from
    "no such capability", and the console branches on exactly that difference: when none
    exists it offers to queue the two further runs that would produce one."""
    response = scientist.get(f"/api/v1/capabilities/{CAPABILITY}/seed-variance")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "capability_id": CAPABILITY,
        "characterised": False,
        "runs_required": 3,
        "characterisation": None,
    }


def test_seed_variance_projects_the_runs_it_is_of_and_never_a_margin(
    scientist: httpx.Client, cohort: Cohort, tdb: psycopg.Connection[Any],
    declared_environment: None
) -> None:
    """`MOS-TRAIN-128` renders `sd` BESIDE the declared margin in the approval dossier and
    `MOS-EVID-087` forbids tuning the margin to make a candidate pass; `MOS-UI-157`
    forbids the console computing, adjusting or presenting the margin from this figure.
    A member here named `delta`, `margin` or `non_inferiority_margin` would put the two
    numbers in one document and invite exactly the arithmetic all three forbid."""
    ids = []
    for seeds in (1, 2, 3):
        run = tr.submit(
            tdb,
            binding=_seeded_binding(cohort, seeds),
            actor=_engine_actor(),
            trace_id="0af7651916cd43dd8448eb211c80319c",
            leakage={"L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass",
                     "L5": "pass"},
            output_kind="label",
        )
        ids.append(run.public_id)
    tr.record_seed_variance(
        tdb,
        capability_id=CAPABILITY,
        metric="dice",
        sd=0.012,
        training_run_ids=ids,
        backend_kind="nnunet",
        gpu_count=2,
        hyperparameters_digest=tr.hyperparameters_digest_of({}),
        recorded_by=OPERATOR,
    )
    tdb.commit()

    body = scientist.get(f"/api/v1/capabilities/{CAPABILITY}/seed-variance").json()
    assert body["characterised"] is True
    assert body["runs_required"] == 3
    characterisation = body["characterisation"]
    assert characterisation["metric"] == "dice"
    assert characterisation["runs"] == 3
    assert characterisation["sd"] == pytest.approx(0.012)
    assert len(characterisation["training_run_ids"]) == 3
    assert characterisation["hyperparameters_digest"].startswith("sha256:")
    for forbidden in ("delta", "margin", "non_inferiority_margin"):
        assert forbidden not in json.dumps(body)


def _engine_actor() -> Any:
    from medos.db import audit

    return audit.Actor(kind="service_account", id=OPERATOR, auth="api_key")


def _seeded_binding(cohort: Cohort, seed: int) -> Any:
    """Three runs differing ONLY in `seeds`, which is what `MOS-TRAIN-127` measures."""
    env = ENVIRONMENT
    return tr.RunBinding(
        capability_id=CAPABILITY,
        dataset_version_id=cohort.version_id,
        dataset_version_digest=cohort.version_digest,
        split_id=cohort.split_id,
        split_digest=cohort.split_digest,
        annotation_set_id=cohort.annotation_set_id,
        annotation_digest=cohort.annotation_digest,
        preprocessing_spec_id="prep.pulmo.effusion",
        preprocessing_spec_version=2,
        preprocessing_spec_digest=SPEC_DIGEST,
        code_commit=COMMIT,
        code_dirty=False,
        image_digest=IMAGE,
        training_backend={"kind": "nnunet", "version": "2.5.1", "plan_digest": None},
        hyperparameters={},
        seeds={**env["seeds"], "torch": seed},
        determinism=dict(env["determinism"]),
        hardware=dict(env["hardware"]),
        framework_versions=dict(env["framework_versions"]),
    )


# =====================================================================================
# R12 -- section 19.3.7, blocked by design
# =====================================================================================
def test_the_candidate_projection_names_the_role_that_decides_and_offers_no_control(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-UI-167`, `MOS-UI-168`, `MOS-TRAIN-003`, `MOS-TRAIN-008`, `MOS-TRAIN-011`.

    The body is deliberately thin: an id, a status, a verdict, an exposure count and a
    sentence about who decides. Four consts and nothing that acts. The assertion that
    NO member name contains `url`, `href`, `link` or `action` is the one that would catch
    a well-meaning future change adding "a convenient link to the approval screen" --
    which is `MOS-TRAIN-011`'s non-conformant promotion UI arriving one member at a time.
    """
    rid = scientist.post(
        "/api/v1/training-runs", json=cohort.body()
    ).json()["training_run_id"]

    response = scientist.get(f"/api/v1/training-runs/{rid}/candidate")
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == {
        "training_run_id",
        "candidate_model_version_id",
        "lifecycle_status",
        "evaluation_run_id",
        "criteria_verdict",
        "test_exposure_count",
        "promotion",
    }
    assert body["training_run_id"] == rid
    # Nothing has been built yet, and the projection says so rather than inventing one.
    assert body["candidate_model_version_id"] is None
    assert body["lifecycle_status"] is None
    assert body["evaluation_run_id"] is None
    assert body["criteria_verdict"] is None
    assert body["test_exposure_count"] == 0

    assert body["promotion"] == {
        "performed_on_this_surface": False,
        "permission": "artifact.approve",
        "transition": "VALIDATED -> APPROVED",
        "requirement": "MOS-TRAIN-008",
    }

    def names(node: Any, out: list[str]) -> list[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                out.append(key)
                names(value, out)
        return out

    for name in names(body, []):
        assert not any(
            token in name.lower() for token in ("url", "href", "link", "action")
        ), name
    # MOS-UI-165 / MOS-EVID-056: no aggregate metric beside the verdict.
    for forbidden in ("dice", "metric", "score", "auc", "sensitivity"):
        assert forbidden not in json.dumps(body).lower()


def test_no_training_route_accepts_an_approval_a_deployment_or_a_clinical_use_mode(
) -> None:
    """`MOS-SEC-158` and `MOS-UI-167`, over the SERVED surface rather than the registry.

    `tests/unit/test_permission_contract.py` holds the same property over
    `medos/api/v1/routes.train.yaml`. This holds it over the paths the app actually mounts,
    because the registry is a claim and the router is the deployment -- and the two are only the
    same thing while somebody keeps them so.
    """
    from medos.api.routes_training import PERMISSIONS as TRAINING_PERMISSIONS
    from medos.api.training_plane import create_training_app as create_app
    from medos.training.candidate import FORBIDDEN_PIPELINE_PERMISSIONS
    from medos.training.orchestrator import FORBIDDEN_PERMISSIONS

    forbidden = set(FORBIDDEN_PERMISSIONS) | set(FORBIDDEN_PIPELINE_PERMISSIONS)
    assert forbidden, "the check has lost its subject; both tuples are empty"
    assert not (set(TRAINING_PERMISSIONS) & forbidden)

    # `configure_logs=False` IS LOAD-BEARING AND WAS FOUND BY A RED RUN. `create_app()`'s
    # default calls `configure_logging()`, which sets `logging.getLogger("medos.api")
    # .propagate = False` -- a process-wide change. A later test in this session
    # (`test_gateway.py::test_the_log_stream_carries_the_route_template_and_a_study_ref`)
    # captures the Gateway's access log by attaching a handler to the ROOT logger, and
    # with propagation switched off it captured nothing and failed with `assert 0 == 2`.
    # A route-shape assertion has no business reconfiguring the process's logging.
    banned = ("promote", "promotion", "deployment", "cutover", "clinical-use",
              "clinical_use", "approve", "dossier")
    for route in create_app(configure_logs=False).routes:
        path = str(getattr(route, "path", ""))
        if not path.startswith("/api/v1/training-runs") and not path.startswith(
            "/api/v1/configuration-searches"
        ) and not path.startswith("/api/v1/conversion-runs"):
            continue
        name = str(getattr(route, "name", ""))
        for token in banned:
            assert token not in path.lower(), path
            assert token not in name.lower(), name


# =====================================================================================
# R13, R14, R15 -- ConfigurationSearch
# =====================================================================================
def _space() -> dict[str, Any]:
    return {
        "axes": [
            {"name": "learning_rate", "values": [0.0001, 0.0002, 0.0005]},
            {"name": "num_epochs", "values": [400, 600]},
        ],
    }


def _budget(**overrides: Any) -> dict[str, Any]:
    budget = {
        "max_trials": 6,
        "max_gpu_hours": 48.0,
        "max_wall_clock_hours": 24.0,
        "max_ensemble_members": 1,
        "max_footprint_bytes": 8_000_000_000,
    }
    budget.update(overrides)
    return budget


def _declare(cohort: Cohort, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "capability_id": CAPABILITY,
        "dataset_version_id": cohort.version_id,
        "split_id": cohort.split_id,
        "annotation_set_id": cohort.annotation_set_id,
        "space": _space(),
        "strategy": "grid",
        "seed": 7,
        "selection_metric": "dice",
        "selection_partition": "tune",
        "selection_rule": "highest mean dice on tune",
        "trials_planned": 6,
        "budget": _budget(),
    }
    body.update(overrides)
    return body


def test_a_configuration_search_is_recorded_with_its_space_digested(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-218` / `MOS-TRAIN-220`. The space is the caller's declaration and the
    server digests it; the backend, the fingerprint and the image are the server's."""
    response = scientist.post("/api/v1/configuration-searches", json=_declare(cohort))
    assert response.status_code == 201, response.text
    search = response.json()
    assert search["configuration_search_id"].startswith("cs_")
    assert response.headers["location"].endswith(search["configuration_search_id"])
    assert search["space_digest"].startswith("sha256:")
    assert search["state"] == "PENDING"
    assert search["read_partitions"] == ["train", "tune"]
    assert search["backend"]["kind"] == "nnunet"
    assert search["code_commit"] == COMMIT
    assert search["nominated_training_run_id"] is None

    read = scientist.get(
        f"/api/v1/configuration-searches/{search['configuration_search_id']}"
    )
    assert read.status_code == 200
    assert read.json() == search


def test_an_absent_budget_bound_is_a_registration_error_not_an_unlimited_one(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-234`, in its own words. Caught by the request schema, so no row exists
    and no GPU hour is spent deciding it."""
    budget = _budget()
    del budget["max_gpu_hours"]
    response = scientist.post(
        "/api/v1/configuration-searches", json=_declare(cohort, budget=budget)
    )
    assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")


def test_planned_trials_beyond_the_budget_is_the_engines_422(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-234`: "the orchestrator MUST stop the search at the first bound
    reached", so a plan that already exceeds one is refused at registration. This is the
    engine's refusal rendered, `refusals[]` and all."""
    response = scientist.post(
        "/api/v1/configuration-searches",
        json=_declare(cohort, trials_planned=99, budget=_budget(max_trials=6)),
    )
    doc = assert_problem(
        response, status=422, cls="clinical_rejection", code="CONFIGURATION_SEARCH_REFUSED"
    )
    assert "MOS-TRAIN-234" in check_ids(doc)
    assert "planned_trials_exceed_the_budget" in codes(doc)


def test_a_space_that_is_code_is_refused_with_the_axis_named(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-220`: the space "MUST NOT be a Python callable, an expression string, a
    lambda or a template evaluated at search time" -- a space that is code has no digest
    that means anything."""
    space = {"axes": [{"name": "learning_rate",
                       "values": "lambda t: 10 ** -t"}]}
    response = scientist.post(
        "/api/v1/configuration-searches", json=_declare(cohort, space=space)
    )
    doc = assert_problem(
        response, status=422, cls="clinical_rejection", code="CONFIGURATION_SEARCH_REFUSED"
    )
    assert "MOS-TRAIN-220" in check_ids(doc)


def test_selection_on_train_without_folds_is_refused(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-TRAIN-215`: the selection metric is computed on `tune`, or on
    cross-validation folds within `train`. `test` is in neither branch and is not a
    value this member admits."""
    assert_problem(
        scientist.post(
            "/api/v1/configuration-searches",
            json=_declare(cohort, selection_partition="train"),
        ),
        status=400,
        cls="client_error",
        code="SCHEMA_VIOLATION",
    )
    assert_problem(
        scientist.post(
            "/api/v1/configuration-searches",
            json=_declare(cohort, selection_partition="test"),
        ),
        status=400,
        cls="client_error",
        code="SCHEMA_VIOLATION",
    )


def test_exactly_one_nomination_and_the_second_is_refused(
    scientist: httpx.Client, cohort: Cohort, tdb: psycopg.Connection[Any],
    declared_environment: None
) -> None:
    """`MOS-TRAIN-216` / `MOS-TRAIN-217`. The second nomination is not an edit; the remedy
    is a NEW search row referencing the original by `parent_search_id`, with a fresh
    increment of `test_exposure_count`, "because a silent second nomination off the back
    of a first test run is selection on test performed one candidate at a time"."""
    search = scientist.post(
        "/api/v1/configuration-searches", json=_declare(cohort)
    ).json()
    sid = search["configuration_search_id"]

    winner = tr.submit(
        tdb, binding=_seeded_binding(cohort, 11), actor=_engine_actor(),
        trace_id="0af7651916cd43dd8448eb211c80319c",
        leakage={"L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass", "L5": "pass"},
        output_kind="label",
    )
    runner_up = tr.submit(
        tdb, binding=_seeded_binding(cohort, 12), actor=_engine_actor(),
        trace_id="0af7651916cd43dd8448eb211c80319c",
        leakage={"L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass", "L5": "pass"},
        output_kind="label",
    )
    tdb.commit()

    nomination = {
        "training_run_id": winner.public_id,
        "runner_up_training_run_id": runner_up.public_id,
        "selection_margin": 0.031,
        "trials_completed": 6,
        "cost": {"stop_reason": "space_exhausted", "gpu_hours": 31.5},
    }
    first = scientist.post(
        f"/api/v1/configuration-searches/{sid}/nomination", json=nomination
    )
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "SUCCEEDED"
    assert first.json()["selection_margin"] == pytest.approx(0.031)

    second = scientist.post(
        f"/api/v1/configuration-searches/{sid}/nomination", json=nomination
    )
    doc = assert_problem(
        second, status=422, cls="clinical_rejection", code="CONFIGURATION_SEARCH_REFUSED"
    )
    assert "MOS-TRAIN-217" in check_ids(doc)
    assert "search_already_nominated" in codes(doc)


def test_a_nomination_naming_an_unknown_run_is_a_404(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    sid = scientist.post(
        "/api/v1/configuration-searches", json=_declare(cohort)
    ).json()["configuration_search_id"]
    response = scientist.post(
        f"/api/v1/configuration-searches/{sid}/nomination",
        json={
            "training_run_id": "tr_0123456789ABCDEFGHJKMNP0TV",
            "runner_up_training_run_id": "tr_0123456789ABCDEFGHJKMNP0TW",
            "selection_margin": 0.01,
            "trials_completed": 2,
            "cost": {"stop_reason": "max_trials"},
        },
    )
    assert_problem(
        response, status=404, cls="client_error", code="TRAINING_RUN_NOT_FOUND"
    )


# =====================================================================================
# R16, R17 -- ConversionRun
# =====================================================================================
def test_an_int8_conversion_must_name_a_tune_calibration_cohort(
    scientist: httpx.Client, declared_environment: None
) -> None:
    """`MOS-TRAIN-157`: "Calibrating on `test` is operating-point selection on the test
    set under another name." Both halves of the biconditional, and the `test` spelling,
    are refused by the request schema before a connection is opened."""
    base = {
        "source_model_version_id": "00000000-0000-0000-0000-0000000000ab",
        "target_format": "tensorrt_plan",
        "precision": "int8",
        "toolchain": {"tensorrt": "10.0.1", "onnx_opset": 17},
        "equivalence_cohort_digest": "sha256:" + "f1" * 32,
    }
    assert_problem(
        scientist.post("/api/v1/conversion-runs", json=base),
        status=400, cls="client_error", code="SCHEMA_VIOLATION",
    )
    assert_problem(
        scientist.post(
            "/api/v1/conversion-runs",
            json={**base, "precision": "fp16",
                  "calibration_dataset_version_id": base["source_model_version_id"],
                  "calibration_partition": "tune"},
        ),
        status=400, cls="client_error", code="SCHEMA_VIOLATION",
    )
    assert_problem(
        scientist.post(
            "/api/v1/conversion-runs",
            json={**base,
                  "calibration_dataset_version_id": base["source_model_version_id"],
                  "calibration_partition": "test"},
        ),
        status=400, cls="client_error", code="SCHEMA_VIOLATION",
    )


def test_there_is_no_tolerances_member_on_the_conversion_request(
    scientist: httpx.Client, declared_environment: None
) -> None:
    """`MOS-TRAIN-164`: "A tolerance relaxed to admit one artifact silently relaxes it for
    every future artifact of that family." There is no member to relax and no per-case
    skip, matching `medos.training.conversion`, which accepts neither."""
    for member, value in (
        ("tolerances", {"max_abs_diff": 1.0}),
        ("skip_cases", ["P0001"]),
        ("post_threshold_dice", 0.5),
    ):
        assert_problem(
            scientist.post(
                "/api/v1/conversion-runs",
                json={
                    "source_model_version_id": "00000000-0000-0000-0000-0000000000ab",
                    "target_format": "onnx",
                    "precision": "fp32",
                    "toolchain": {"onnx_opset": 17},
                    "equivalence_cohort_digest": "sha256:" + "f1" * 32,
                    member: value,
                },
            ),
            status=400, cls="client_error", code="SCHEMA_VIOLATION",
        )


def test_a_conversion_of_an_unknown_source_is_a_404(
    scientist: httpx.Client, declared_environment: None
) -> None:
    assert_problem(
        scientist.post(
            "/api/v1/conversion-runs",
            json={
                "source_model_version_id": "00000000-0000-0000-0000-0000000000ab",
                "target_format": "onnx",
                "precision": "fp32",
                "toolchain": {"onnx_opset": 17},
                "equivalence_cohort_digest": "sha256:" + "f1" * 32,
            },
        ),
        status=404, cls="client_error", code="MODEL_VERSION_NOT_FOUND",
    )


def test_a_malformed_conversion_id_is_a_400_and_an_absent_one_a_404(
    scientist: httpx.Client, declared_environment: None
) -> None:
    assert_problem(
        scientist.get("/api/v1/conversion-runs/nope"),
        status=400, cls="client_error", code="INVALID_CONVERSION_RUN_ID",
    )
    assert_problem(
        scientist.get("/api/v1/conversion-runs/cv_0123456789ABCDEFGHJKMNP0TV"),
        status=404, cls="client_error", code="CONVERSION_RUN_NOT_FOUND",
    )


# =====================================================================================
# Authorisation and tenancy
# =====================================================================================
def test_every_unsafe_route_refuses_a_credential_whose_ceiling_lacks_its_permission(
    reader: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-SEC-027` / `MOS-SEC-045`: the scope is a CEILING, and exceeding it is a real
    violation even though holding the entry is not yet a grant. The read-only credential
    carries `training_run.read` and nothing else, so every write and every read bound to
    another key is a 403 naming the permission it wanted."""
    nomination = {
        "training_run_id": "tr_0123456789ABCDEFGHJKMNP0TV",
        "runner_up_training_run_id": "tr_0123456789ABCDEFGHJKMNP0TW",
        "selection_margin": 0.01,
        "trials_completed": 2,
        "cost": {"stop_reason": "max_trials"},
    }
    conversion = {
        "source_model_version_id": "00000000-0000-0000-0000-0000000000ab",
        "target_format": "onnx",
        "precision": "fp32",
        "toolchain": {"onnx_opset": 17},
        "equivalence_cohort_digest": "sha256:" + "f1" * 32,
    }
    # The bodies are WELL FORMED. FastAPI validates a request body before the handler
    # runs, so an empty `{}` would be answered 400 SCHEMA_VIOLATION and would never
    # reach the permission check -- and this test would then be asserting nothing about
    # authorisation. That ordering is the framework's and is shared with every other
    # router in this tree; it is recorded here rather than worked around.
    cases: list[tuple[str, str, str, Any]] = [
        ("POST", "/api/v1/training-runs", "training_run.submit", cohort.body()),
        ("POST", "/api/v1/training-runs/tr_0123456789ABCDEFGHJKMNP0TV/cancel",
         "training_run.cancel", None),
        ("POST", "/api/v1/configuration-searches", "configuration_search.declare",
         _declare(cohort)),
        ("GET", "/api/v1/configuration-searches/cs_0123456789ABCDEFGHJKMNP0TV",
         "configuration_search.read", None),
        ("POST",
         "/api/v1/configuration-searches/cs_0123456789ABCDEFGHJKMNP0TV/nomination",
         "configuration_search.nominate", nomination),
        ("POST", "/api/v1/conversion-runs", "conversion_run.submit", conversion),
        ("GET", "/api/v1/conversion-runs/cv_0123456789ABCDEFGHJKMNP0TV",
         "conversion_run.read", None),
        ("GET", "/api/v1/training-runs/tr_0123456789ABCDEFGHJKMNP0TV/candidate",
         "model_version.read", None),
    ]
    for method, path, permission, body in cases:
        response = reader.request(method, path, json=body)
        doc = assert_problem(
            response, status=403, cls="authz_error", code="PERMISSION_DENIED"
        )
        assert doc["required_permission"] == permission, path


def test_the_read_routes_are_open_to_the_read_ceiling(
    reader: httpx.Client, scientist: httpx.Client, cohort: Cohort,
    declared_environment: None
) -> None:
    rid = scientist.post(
        "/api/v1/training-runs", json=cohort.body()
    ).json()["training_run_id"]
    assert reader.get("/api/v1/training-runs").status_code == 200
    assert reader.get(f"/api/v1/training-runs/{rid}").status_code == 200
    assert reader.get(f"/api/v1/training-runs/{rid}/test-exposure").status_code == 200
    assert reader.get(
        f"/api/v1/capabilities/{CAPABILITY}/seed-variance"
    ).status_code == 200


def test_an_unauthenticated_request_never_reaches_a_handler(
    training_url: str, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-SEC-008`: anonymous access exists on three paths and nowhere else."""
    with httpx.Client(base_url=training_url, timeout=30.0) as anonymous:
        response = anonymous.post("/api/v1/training-runs", json=cohort.body())
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["class"] == "authz_error"


@pytest.fixture(scope="session")
def app_role_url(pg_dsn: str) -> Iterator[str]:
    """A second app connecting as `medicalos_app`, which is NOBYPASSRLS.

    THIS FIXTURE IS THE TEST, AND WITHOUT IT THE ISOLATION ASSERTION BELOW IS EMPTY. The
    `training_url` app connects with the fixture's own credentials, which on a
    development cluster are the bootstrap superuser's -- and a superuser bypasses row
    level security entirely. `tests/integration/conftest.py` says so in terms ("a proof
    written on the fixture connection would prove nothing"), and this was observed while
    writing these tests: the cross-tenant read below returned 200 and the full run body
    until the app was moved onto the role the deployment actually uses
    (`medos/deploy/compose/docker-compose.yml` runs `medos-api` as `medicalos_app`).

    `MOS-SEC-073` is what makes the role the right subject: `medicalos_app` is
    NOBYPASSRLS and owns nothing, so FORCE ROW LEVEL SECURITY binds it.
    """
    import re as _re

    from tests.integration.conftest import APP_PASSWORD

    dsn = _re.sub(
        r"^postgresql://[^@]+@", f"postgresql://medicalos_app:{APP_PASSWORD}@", pg_dsn
    )

    def opener(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(dsn, row_factory=dict_row, **kwargs)

    app = create_app(connect=opener, configure_logs=False)
    with _running_server(app) as url:
        yield url


def test_a_second_tenants_run_is_a_404_and_not_a_403(
    pg_dsn: str, app_role_url: str, scientist: httpx.Client, cohort: Cohort,
    declared_environment: None, tdb: psycopg.Connection[Any]
) -> None:
    """`MOS-SEC-072` with FORCE ROW LEVEL SECURITY. Telling a caller that a row it may not
    read EXISTS is the leak; row-level security makes "no such run" and "not yours"
    indistinguishable to the handler by construction, which is the point."""
    rid = scientist.post(
        "/api/v1/training-runs", json=cohort.body()
    ).json()["training_run_id"]

    other_tenant = str(_second_tenant(pg_dsn))
    key = _issue_for(pg_dsn, other_tenant, sorted(PERMISSIONS))
    # `MEDOS_TENANTS` is the deployment's declaration of which tenants it serves, and
    # `ApiKeyAuthenticator` iterates exactly it. Without the second tenant in the list
    # the intruder's key is a 401 and the test would prove nothing about ISOLATION --
    # only that a key for an unserved tenant does not resolve, which is a different
    # (also correct) property.
    previous = os.environ.get("MEDOS_TENANTS")
    os.environ["MEDOS_TENANTS"] = f"{DEFAULT_TENANT_ID},{other_tenant}"
    try:
        with httpx.Client(
            base_url=app_role_url, timeout=30.0,
            headers={"Authorization": f"Bearer {key}"},
        ) as intruder:
            assert_problem(
                intruder.get(f"/api/v1/training-runs/{rid}"),
                status=404, cls="client_error", code="TRAINING_RUN_NOT_FOUND",
            )
            assert_problem(
                intruder.get(f"/api/v1/training-runs/{rid}/candidate"),
                status=404, cls="client_error", code="TRAINING_RUN_NOT_FOUND",
            )
            assert intruder.get("/api/v1/training-runs").json()["items"] == []
    finally:
        if previous is None:
            os.environ.pop("MEDOS_TENANTS", None)
        else:  # pragma: no cover
            os.environ["MEDOS_TENANTS"] = previous


def _second_tenant(pg_dsn: str) -> str:
    with psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            "INSERT INTO tenants (slug, display_name) VALUES (%s, %s) "
            "ON CONFLICT (slug) DO UPDATE SET display_name = EXCLUDED.display_name "
            "RETURNING id",
            ("intruder", "the other tenant"),
        ).fetchone()
    return str(dict(row)["id"])


def _issue_for(pg_dsn: str, tenant_id: str, scope: list[str]) -> str:
    from medos.security import store

    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        minted, _record = store.issue(
            conn,
            tenant_id=tenant_id,
            principal_kind="service_account",
            principal_id="22222222-2222-2222-2222-222222222222",
            created_by=store.BOOTSTRAP_OPERATOR_ID,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scope=scope,
            label="cross-tenant probe",
            env="dev",
        )
        conn.commit()
    return str(minted.plaintext)


def test_a_tenant_selecting_header_or_query_parameter_is_refused(
    scientist: httpx.Client, cohort: Cohort, declared_environment: None
) -> None:
    """`MOS-API-003`: there is no tenant path segment and no tenant query parameter, and
    naming one is refused rather than ignored -- a caller who believes
    `?tenant_id=other` worked because it returned 200 has been taught something false."""
    assert scientist.get(
        "/api/v1/training-runs", params={"tenant_id": "00000000-0000-0000-0000-00000000ff"}
    ).status_code == 403
    assert scientist.get(
        "/api/v1/training-runs",
        headers={"MedicalOS-Tenant-Id": "00000000-0000-0000-0000-0000000000ff"},
    ).status_code == 403
