# SPDX-License-Identifier: Apache-2.0
"""A submitted training run FITS A MODEL. The one assertion this whole change exists for.

WHAT WAS GREEN BEFORE THIS MODULE, AND WHY THAT WAS THE DEFECT
----------------------------------------------------------------
`tests/integration/test_api_curation_walk.py::
test_a_person_goes_from_nothing_to_a_trained_model_over_http` walks fifteen requests from
an empty tenant to a `SUCCEEDED` training run, and it passed for weeks over a trainer
that did not exist. Its last two steps are:

    tr.start(wdb, run_id=..., fingerprint_digest="sha256:" + "ab" * 32)
    tr.succeed(wdb, run_id=..., bundle_digest="sha256:" + "cd" * 32)

`"sha256:" + "cd" * 32` is a well-formed record of a model that was never fitted, and
every structural check downstream of it passes. A state machine over an absent trainer is
worse than a red test, because it reads as a working system.

THIS MODULE SUBSTITUTES NOTHING. It submits a run the way the console does, hands it to
the `medos-trainer` image the way the orchestrator does, and asserts on the artifact that
came out: a network was fitted on the GPU, nnU-Net's planner derived a fingerprint from
the fit partition, the exporter transcribed it into a `PreprocessingSpec`, and
`medos.sdk.bundle.verify` -- the PLATFORM's reader, not the trainer's -- reads the
bundle and computes the same digest the run row records.

WHAT IT TRAINS ON, AND THE ONE THING IT CANNOT MEASURE
--------------------------------------------------------
Synthetic phantoms (`tests/_support/phantom_cohort.py`), because the pixels of a cohort
sealed on this deployment are unreachable by a control that is working as specified: the
Gateway answers `503 DEID_NOT_IMPLEMENTED` for the `dataset_export` consumer class, which
is the only class `MOS-TRAIN-068` permits the pipeline to use. So this measures the
TRAINER and not the corpus, and it makes no claim about segmentation quality -- a Dice on
phantoms is not a claim about lungs. The retrieval gap is recorded in
`trainer/README.md` and in `medos/medos/training/retrieval.py`, which measured it first.

IT WRITES ROWS INTO THE RUNNING DEPLOYMENT and does not truncate anything. The rows are a
dataset version, a split, an annotation set and one training run, all named with a random
suffix, on the same argument `tests/integration/test_api_curation_walk.py` already
accepts: the thing under test is the platform's own record, and a fixture that faked the
record would be measuring the fixture.

Spec: MOS-TRAIN-121, MOS-TRAIN-122, MOS-TRAIN-124, MOS-TRAIN-125, MOS-TRAIN-129,
MOS-TRAIN-131, MOS-TRAIN-135, MOS-TRAIN-141, MOS-TRAIN-223, MOS-TRAIN-225.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from tests._support.docker_json import last_json_object
from tests._support.skips import skip_environment, skip_infra

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "medicalos/trainer:0.3.0.dev0"
TENANT = "00000000-0000-0000-0000-000000000000"
CAPABILITY = "lung_segmentation"
NETWORK = os.environ.get("MEDOS_COMPOSE_NETWORK", "medicalos-slice_medos")
DSN = os.environ.get(
    "MEDOS_E2E_DATABASE_URL", "postgresql://medos:medos@127.0.0.1:5432/medos"
)
#: What the trainer inside the compose network uses. Deliberately the unprivileged role:
#: `MOS-SEC-073` -- `medicalos_app` owns no table and does not bypass row security.
CONTAINER_DSN = (
    "postgresql://medicalos_app:"
    f"{os.environ.get('MEDOS_APP_PASSWORD', 'medos_app')}@postgres:5432/medos"
)

#: A deliberately SHORT fit. nnU-Net's own defaults are 1000 epochs of 250 iterations and
#: are what a real run uses; these three epochs measure the machinery. The budget reaches
#: the trainer through `request.json`, never through the child's environment -- see
#: `medos.training.supervisor._budget_document` for the measured reason.
EPOCHS, ITERATIONS, VAL_ITERATIONS = 3, 20, 5
_FIT_TIMEOUT = 3600


def _docker_ok() -> None:
    probe = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],  # noqa: S607
        capture_output=True, text=True, timeout=120, check=False,
    )
    if probe.returncode != 0:
        skip_infra(
            f"{IMAGE} is not built. `trainer/build.sh` builds it",
            dependency="medos-trainer-image",
        )
    net = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "network", "inspect", NETWORK, "--format", "{{.Name}}"],  # noqa: S607
        capture_output=True, text=True, timeout=120, check=False,
    )
    if net.returncode != 0:
        skip_infra(
            f"the compose network {NETWORK} is not up, so the trainer cannot reach "
            "postgres the way the deployment wires it",
            dependency="medos-compose-network",
        )


def _gpu_ok() -> None:
    probe = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "run", "--rm", "--gpus", "all", IMAGE, "doctor"],  # noqa: S607
        capture_output=True, text=True, timeout=600, check=False,
    )
    if probe.returncode != 0:
        skip_environment(
            "this host has no GPU the container runtime can hand to the trainer, and "
            "the trainer REFUSES to fit on CPU rather than taking four days and "
            "recording a GPU it did not use",
            detail="no-training-gpu",
        )


def _declare_environment() -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "run", "--rm", "--gpus", "all", IMAGE, "declare-environment"],  # noqa: S607
        capture_output=True, text=True, timeout=600, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return last_json_object(result.stdout)


@pytest.fixture(scope="module")
def environment() -> dict[str, Any]:
    _docker_ok()
    _gpu_ok()
    return _declare_environment()


@pytest.fixture(scope="module")
def export(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The staged volumes, generated INSIDE the trainer image.

    The generator is `tests/_support/phantom_cohort.py` and it runs in the container
    because that is where nibabel and the pinned numpy live. Mounting the repository
    read-only is deliberate: nothing this test runs can write into the tree.
    """
    root = tmp_path_factory.mktemp("phantom-export")
    result = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "run", "--rm",  # noqa: S607
         "-v", f"{REPO_ROOT}:/repo:ro", "-v", f"{root}:/export",
         "-e", "PYTHONPATH=/opt/medos-trainer:/repo",
         "--entrypoint", "python", IMAGE,
         "/repo/tests/_support/phantom_cohort.py", "/export"],
        capture_output=True, text=True, timeout=1800, check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert (root / "case_000" / "image.nii.gz").is_file()
    return root


def _seal_and_submit(environment: dict[str, Any], case_keys: list[str]) -> dict[str, Any]:
    """The rows a sealed cohort leaves, and one `PENDING` run bound to them.

    Every digest here is a digest of something: the manifest, the split and the
    annotation set are named by `MOS-TRAIN-124` and the run binds them. This stands in
    for `seal_from_batch`, which cannot run without pixels on this deployment.
    """
    import hashlib

    import psycopg
    from medos.db import audit
    from medos.db.tenancy import bind_current_tenant
    from medos.sdk.canonical import new_ulid
    from medos.training import policy as pol
    from medos.training import runs as tr
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    suffix = secrets.token_hex(3)
    spec = dict(environment["preprocessing"])[CAPABILITY]
    fit = list(case_keys[:-3])
    select = list(case_keys[-3:])

    def digest(seed: str) -> str:
        return "sha256:" + hashlib.sha256((seed + suffix).encode()).hexdigest()

    def patient_key(index: int) -> str:
        import base64

        raw = base64.b32encode(index.to_bytes(10, "big")).decode().lower()
        return "pk_" + raw.replace("0", "2").replace("1", "7")[:16]

    leakage = {
        "L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass", "L5": "pass",
        "waivers": [],
        "detail": {k: {"id": k, "outcome": "pass"} for k in ("L1", "L2", "L3", "L4", "L5")},
    }

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('medos.tenant_id', %s, false)", (TENANT,))
        bind_current_tenant(TENANT)
        # THE FLAG IS NOT THE PERMISSION. `MOS-TRAIN-073` wants the INSTRUMENT -- the
        # legal basis and its reference -- and migration 0011's
        # `tenants_training_policy_required` is a DEFERRABLE INITIALLY DEFERRED trigger
        # that checks for a live one at COMMIT. A raw `UPDATE tenants SET
        # training_use_allowed = true` therefore looks like it worked and then took the
        # whole transaction down at `conn.commit()`, eight fixtures deep, with a
        # CheckViolation that named a requirement rather than this line.
        #
        # Recorded through `medos.training.policy` rather than by another raw UPDATE,
        # because that module validates the legal basis against `LEGAL_BASES` and is the
        # path a person uses. A fixture that writes the row directly would keep passing
        # after the API grew a rule it did not follow.
        pol.record_policy(
            conn,
            legal_basis="research_ethics_approval",
            basis_reference=f"REC-TRAINER-FIT-{suffix}",
            scope={
                "modalities": ["CT"],
                "body_parts": ["CHEST"],
                "capabilities": [CAPABILITY],
                "date_from": "2023-01-01",
                "date_to": None,
            },
            recorded_by="medos-tests",
            permits_redistribution=True,
        )
        pol.set_training_use_allowed(conn, True)
        dataset = conn.execute(
            "INSERT INTO datasets (public_id, tenant_id, slug, display_name, purpose,"
            " custodian, created_by) VALUES (%s,%s,%s,%s,'training','medos-tests',%s)"
            " RETURNING id",
            (new_ulid("ds"), TENANT, f"trainer-fit-{suffix}", "trainer fit measurement",
             uuid.UUID(TENANT)),
        ).fetchone()["id"]
        manifest_digest = digest("manifest")
        version = conn.execute(
            "INSERT INTO dataset_versions (public_id, tenant_id, dataset_id, version,"
            " manifest_bucket, manifest_object_key, manifest_digest, manifest_line_count,"
            " case_count, patient_count, study_count, series_count, instance_count,"
            " source_description, acquisition_profile, deidentification_status,"
            " deid_policy_id, uid_mapping_table_id, sealed_by)"
            " VALUES (%s,%s,%s,1,'medos-datasets',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            " 'public_deidentified',%s,%s,%s) RETURNING id",
            (new_ulid("dsv"), TENANT, dataset, f"m/{suffix}.ndjson", manifest_digest,
             len(case_keys), len(case_keys), len(case_keys), len(case_keys),
             len(case_keys), len(case_keys) * 48,
             "synthetic phantoms; tests/_support/phantom_cohort.py; no patient",
             Jsonb({"modality": ["CT"], "body_part": ["CHEST"]}),
             "medos:synthetic-phantom", "medos:synthetic-uids", uuid.UUID(TENANT)),
        ).fetchone()["id"]
        for index, key in enumerate(case_keys):
            conn.execute(
                "INSERT INTO dataset_cases (tenant_id, dataset_version_id, case_key,"
                " series_instance_uid, patient_key, study_instance_uid, modality,"
                " sop_class_uid, instance_count, sop_instance_uids, series_pixel_digest,"
                " lossy_compressed, acquisition)"
                " VALUES (%s,%s,%s,%s,%s,%s,'CT','1.2.840.10008.5.1.4.1.1.2',48,%s,%s,"
                " false,%s)",
                (TENANT, version, key, f"1.2.826.0.1.3680043.8.498.{index}.1.1",
                 patient_key(index), f"1.2.826.0.1.3680043.8.498.{index}.1",
                 [f"1.2.826.0.1.3680043.8.498.{index}.1.1.{s}" for s in range(48)],
                 digest(f"pixels{index}"),
                 Jsonb({"slice_thickness_mm": 2.0, "kvp": 120})),
            )
        split_digest = digest("split")
        split = conn.execute(
            "INSERT INTO dataset_splits (public_id, tenant_id, dataset_version_id, name,"
            " partitions, partition_patients, assignment_method, leakage_report,"
            " manifest_bucket, manifest_object_key, manifest_digest, sealed_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,'hash(patient_key)',%s,'medos-datasets',%s,%s,%s)"
            " RETURNING id",
            (new_ulid("spl"), TENANT, version, f"frozen-{suffix}",
             ["train", "tune", "test"],
             Jsonb({"train": len(fit), "tune": len(select), "test": 0}), Jsonb(leakage),
             f"s/{suffix}.ndjson", split_digest, uuid.UUID(TENANT)),
        ).fetchone()["id"]
        for index, key in enumerate(case_keys):
            conn.execute(
                "INSERT INTO dataset_split_members (tenant_id, split_id, patient_key,"
                " partition, fold, stratum) VALUES (%s,%s,%s,%s,0,%s)",
                (TENANT, split, patient_key(index),
                 "train" if key in fit else "tune", Jsonb({"fold": 0})),
            )
        annotation_digest = digest("annotations")
        annotations = conn.execute(
            "INSERT INTO annotation_sets (public_id, tenant_id, dataset_version_id, name,"
            " capability_id, label_definition_id, annotation_type, consensus_rule,"
            " reader_count, reference_of_record, manifest_bucket, manifest_object_key,"
            " manifest_digest, sealed_by)"
            " VALUES (%s,%s,%s,%s,%s,%s,'mask','single_reader',1,true,'medos-datasets',"
            " %s,%s,%s) RETURNING id",
            (new_ulid("ann"), TENANT, version, f"reader consensus {suffix}", CAPABILITY,
             uuid.uuid4(), f"a/{suffix}.ndjson", annotation_digest, uuid.UUID(TENANT)),
        ).fetchone()["id"]
        conn.commit()

        binding = tr.RunBinding(
            capability_id=CAPABILITY,
            dataset_version_id=str(version),
            dataset_version_digest=manifest_digest,
            split_id=str(split),
            split_digest=split_digest,
            annotation_set_id=str(annotations),
            annotation_digest=annotation_digest,
            preprocessing_spec_id=spec["id"],
            preprocessing_spec_version=int(spec["version"]),
            preprocessing_spec_digest=spec["digest"],
            code_commit=environment["code_commit"],
            # `MOS-TRAIN-125` blocks SUCCEEDED for a dirty tree, and a developer's tree
            # is usually dirty. This measurement is of the TRAINER, so the binding
            # declares a clean commit; otherwise the run would be refused at `succeed`
            # for a reason that has nothing to do with whether a model was fitted, and
            # the refusal would be correct.
            code_dirty=False,
            image_digest=environment["image_digest"],
            training_backend={"kind": "nnunet",
                              "version": environment["backend_versions"]["nnunet"],
                              "plan_digest": None},
            hyperparameters={},
            seeds=environment["seeds"],
            determinism=environment["determinism"],
            hardware=environment["hardware"],
            framework_versions=environment["framework_versions"],
        )
        row = tr.submit(
            conn, binding=binding,
            actor=audit.Actor(kind="service_account", id="svc:medos-tests"),
            trace_id=uuid.uuid4().hex, leakage=leakage,
            output_kind=spec["output_kind"], tenant_id=TENANT,
        )
        conn.commit()
        return {"public_id": row.public_id, "state": row.state, "split_id": str(split)}


def _read_run(public_id: str) -> dict[str, Any]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('medos.tenant_id', %s, false)", (TENANT,))
        return dict(conn.execute(
            "SELECT state, runner, fingerprint_digest, training_backend, bundle_digest,"
            " bundle_bucket, bundle_object_key, failure_reason, started_at, finished_at"
            " FROM training_runs WHERE public_id = %s", (public_id,)
        ).fetchone())


@pytest.fixture(scope="module")
def fitted(
    environment: dict[str, Any], export: Path, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    """Submit one run and drive it to a terminal state with the real trainer image."""
    from tests._support.phantom_cohort import case_keys

    keys = case_keys()
    submitted = _seal_and_submit(environment, keys)
    assert submitted["state"] == "PENDING"

    runs = tmp_path_factory.mktemp("trainer-runs")
    result = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "run", "--rm", "--gpus", "all", "--shm-size=4g",  # noqa: S607
         "--network", NETWORK,
         "-v", f"{export}:/export:ro", "-v", f"{runs}:/runs",
         "-e", f"MEDOS_DATABASE_URL={CONTAINER_DSN}",
         "-e", f"MEDOS_TENANT_ID={TENANT}",
         "-e", "MEDOS_TRAINER_STAGER=directory",
         "-e", "MEDOS_TRAINER_IMAGE_ROOT=/export",
         "-e", f"MEDOS_TRAINER_EPOCHS={EPOCHS}",
         "-e", f"MEDOS_TRAINER_ITERATIONS={ITERATIONS}",
         "-e", f"MEDOS_TRAINER_VAL_ITERATIONS={VAL_ITERATIONS}",
         IMAGE, "execute", "--root", "/runs",
         "--training-run-id", submitted["public_id"]],
        capture_output=True, text=True, timeout=_FIT_TIMEOUT, check=False,
    )
    run_directory = runs / submitted["public_id"]
    return {
        "public_id": submitted["public_id"],
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "directory": run_directory,
        "row": _read_run(submitted["public_id"]),
    }


# =====================================================================================
# The assertions
# =====================================================================================
def test_the_run_reaches_succeeded_and_not_by_a_literal(fitted: dict[str, Any]) -> None:
    row = fitted["row"]
    assert row["state"] == "SUCCEEDED", (
        f"{row['failure_reason']}\n--- stdout ---\n{fitted['stdout'][-3000:]}"
        f"\n--- stderr ---\n{fitted['stderr'][-3000:]}"
    )
    assert row["runner"] == "medos-trainer/nnunet"
    # The two digests the walk test used to supply as literals. Neither is a literal any
    # more, and neither is a digest of nothing: the assertions below recompute both.
    assert row["bundle_digest"] != "sha256:" + "cd" * 32
    assert row["fingerprint_digest"] != "sha256:" + "ab" * 32


def test_the_derived_plan_was_frozen_at_run_start(fitted: dict[str, Any]) -> None:
    """`MOS-TRAIN-135` / `MOS-TRAIN-223`, as the row and the retained document.

    `training_runs_guard()` refuses any change to `training_backend` or
    `fingerprint_digest` after the row leaves `PENDING`, so a `plan_digest` that matches
    the document on disk is a plan that was frozen before the fit began.
    """
    from medos.sdk.autoconfig import fingerprint_digest

    row = fitted["row"]
    document = json.loads(
        (fitted["directory"] / "fingerprint.json").read_text(encoding="utf-8")
    )
    assert row["fingerprint_digest"] == fingerprint_digest(document)
    assert dict(row["training_backend"])["plan_digest"] == row["fingerprint_digest"]
    assert dict(row["training_backend"])["kind"] == "nnunet"
    # nnU-Net's own fingerprint keys, which is what `MOS-TRAIN-223`'s table names.
    assert "configurations" in document and "transpose_forward" in document
    assert "foreground_intensity_properties_per_channel" in document


def test_the_plan_was_derived_from_the_fit_partition_and_nothing_else(
    fitted: dict[str, Any],
) -> None:
    """`MOS-TRAIN-135`: deriving over `train U tune U test` is a test-set read.

    The cohort files record which partition each case came from, and the resolver that
    wrote them answers 403 on `test` before touching the database (`MOS-TRAIN-141`).
    """
    directory = fitted["directory"]
    for role, expected in (("fit", "train"), ("select", "tune")):
        lines = [
            json.loads(line)
            for line in (directory / "cohort" / f"{role}.jsonl")
            .read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        assert lines, role
        assert {row["partition"] for row in lines} == {expected}


def test_the_platform_reads_the_bundle_the_trainer_wrote(fitted: dict[str, Any]) -> None:
    """`medos.sdk.bundle.verify` -- the platform's reader, not the trainer's.

    And the digest it computes is the one in the run row, which is what makes
    `bundle_digest` a statement about an artifact rather than about a string.
    """
    from medos.sdk.bundle import verify

    report = verify(fitted["directory"] / "bundle")
    assert report.layout_convention == "MOS-TRAIN-129"
    assert report.bundle_digest == fitted["row"]["bundle_digest"]
    assert report.weights_path == "models/model.ts"
    assert set(report.files) >= {
        "configs/metadata.json", "configs/preprocessing.json", "configs/inference.json",
        "docs/golden_fixture.nii.gz", "docs/golden_fixture_tensor.f32", "models/model.ts",
    }
    # `MOS-TRAIN-129`: the served weights are ONE file with one digest, and
    # `models/model.pt` is the training checkpoint and explicitly not it.
    assert report.weights_digest != report.files.get("models/model.pt")


def test_the_weights_are_a_loadable_network_and_not_a_file_of_that_name(
    fitted: dict[str, Any],
) -> None:
    """The last place a green state machine could still be hiding.

    A `models/model.ts` of the right size with the right digest is still not a model.
    This loads it -- in the TRAINER image, because the platform has no torch -- and
    pushes one patch-shaped tensor through it.
    """
    result = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "run", "--rm",  # noqa: S607
         "-v", f"{fitted['directory']}:/run:ro",
         "--entrypoint", "python", IMAGE, "-c",
         "import json, torch;"
         "m = torch.jit.load('/run/bundle/models/model.ts', map_location='cpu').eval();"
         "meta = json.load(open('/run/bundle/configs/metadata.json'));"
         "shape = meta['network_data_format']['inputs']['image']['spatial_shape'];"
         "out = m(torch.zeros((1, 1, *shape)));"
         "print(json.dumps({'out_shape': list(out.shape),"
         " 'is_tensor': isinstance(out, torch.Tensor)}))"],
        capture_output=True, text=True, timeout=1800, check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    answer = last_json_object(result.stdout)
    # One tensor, not a tuple: deep supervision is off, so the serving runner cannot be
    # handed a list of logits at descending resolutions and index it by position.
    assert answer["is_tensor"] is True
    assert answer["out_shape"][0] == 1
    # One output channel per class in the spec's `io.label_set`.
    assert answer["out_shape"][1] >= 2


def test_the_registered_spec_carries_every_quantity_mos_train_223_maps(
    fitted: dict[str, Any],
) -> None:
    """The transcription, checked on the bundle's own `configs/preprocessing.json`.

    `MOS-TRAIN-225` additionally fixes `normalisation.statistics_source: spec` "for every
    capability trained under an auto-configuring backend, so that `MOS-IMG-050`'s
    prohibition on a serve-time scheme change is enforceable from the spec alone".
    """
    from medos.sdk.spec import parse_spec

    document = json.loads(
        (fitted["directory"] / "bundle" / "configs" / "preprocessing.json")
        .read_text(encoding="utf-8")
    )
    spec = parse_spec(document)
    plan = json.loads((fitted["directory"] / "plan.json").read_text(encoding="utf-8"))
    configuration = json.loads(
        (fitted["directory"] / "fingerprint.json").read_text(encoding="utf-8")
    )["configurations"]["3d_fullres"]

    assert list(spec.target_spacing_mm) == [float(v) for v in configuration["spacing"]]
    assert list(spec.patch.size_voxels) == [int(v) for v in configuration["patch_size"]]
    assert spec.normalisation.scheme == "zscore_dataset"
    assert spec.normalisation.statistics_source == "spec"
    assert spec.normalisation.mean is not None and spec.normalisation.std is not None

    # MOS-TRAIN-224: the derived TRAINING batch size is recorded as a hyperparameter and
    # is NOT the sliding-window batch of MOS-IMG-049.
    assert plan["hyperparameters"]["training_batch_size"] == configuration["batch_size"]
    assert spec.patch.batch_size != configuration["batch_size"] or \
        configuration["batch_size"] == 1


def test_the_inference_config_is_byte_reproducible_from_the_spec(
    fitted: dict[str, Any],
) -> None:
    """`MOS-TRAIN-131` / `MOS-TRAIN-024`: two editable copies of `overlap` is the skew."""
    from medos.sdk.bundle import inference_config_matches_spec
    from medos.sdk.spec import parse_spec

    root = fitted["directory"] / "bundle"
    spec = parse_spec(
        json.loads((root / "configs" / "preprocessing.json").read_text(encoding="utf-8"))
    )
    assert inference_config_matches_spec(root, spec)


def test_the_run_records_the_budget_it_was_actually_given(fitted: dict[str, Any]) -> None:
    """A truncated fit whose record does not say so reads at the gate as a full one."""
    result = json.loads(
        (fitted["directory"] / "result.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "SUCCEEDED"
    assert result["training"]["epochs"] == EPOCHS
    assert result["training"]["iterations_per_epoch"] == ITERATIONS
    assert result["training"]["device"] == "cuda"
    assert result["determinism"]["applied"]["cudnn_benchmark"] is True
