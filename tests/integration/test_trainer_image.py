# SPDX-License-Identifier: Apache-2.0
"""The built `medos-trainer` image, asked what it is. Register entry 82, end to end.

WHAT NEEDS A CONTAINER AND WHAT DOES NOT
------------------------------------------
`tests/unit/test_trainer_environment.py` holds the arithmetic: the stamp refuses a
placeholder commit, the content digest is a pure function of content, the nine keys
match the router's. None of that needs docker.

Three things do, and they are here:

  1. THE STAMP DESCRIBES THIS IMAGE. Entry 82's whole complaint is a provenance value
     that stops matching the thing it describes. So the image is asked to recompute its
     own inventory digest and the answer is compared with the one stored at build. They
     agree exactly when the image has not changed since it was stamped -- which is what
     "correct after a rebuild without anybody editing a file" means, checked in ten
     seconds instead of by building twice.
  2. THE PLATFORM PARSES WHAT THE TRAINER EMITS. `declare-environment` writes a
     document; `medos/medos/api/routes_training.py::load_training_environment` is the reader
     that answers `503` when it cannot. Running the real emitter into the real reader is
     the only way to know the two agree, and the first thing that would break silently.
  3. THE REFUSAL WITHOUT A GPU IS A REFUSAL. A trainer that quietly falls back to CPU is
     the defect this whole change exists to remove, one layer down.

It skips, never fails, when the image has not been built: `trainer/build.sh` is a
6 GB download the first time and CI may not have run it.

Spec: MOS-TRAIN-124, MOS-TRAIN-126, MOS-REL-037, entry 82.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._support.docker_json import last_json_object
from tests._support.skips import skip_infra

_TRAINER_TREE = Path(__file__).resolve().parents[2] / "trainer"
if not (_TRAINER_TREE / "requirements.txt").is_file():
    pytest.skip(
        "the trainer tree is not present in this checkout (core split) -- "
        "the same gates run in medlange/trainer's own CI",
        allow_module_level=True,
    )


IMAGE = "medicalos/trainer:0.3.0.dev0"
_TIMEOUT = 600

pytestmark = pytest.mark.slow


def _docker(*args: str, gpu: bool = False) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "run", "--rm"]
    if gpu:
        argv += ["--gpus", "all"]
    argv += [IMAGE, *args]
    return subprocess.run(  # noqa: S603 - argv vector, no shell
        argv, capture_output=True, text=True, timeout=_TIMEOUT, check=False
    )


@pytest.fixture(scope="module")
def image() -> str:
    probe = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],  # noqa: S607
        capture_output=True, text=True, timeout=120, check=False,
    )
    if probe.returncode != 0:
        skip_infra(
            f"{IMAGE} is not built on this host. `trainer/build.sh` builds it; "
            "it is a multi-gigabyte CUDA download the first time, so this suite skips "
            "rather than failing when a checkout has not paid for it",
            dependency="medos-trainer-image",
        )
    return probe.stdout.strip()


#: The trainer prints one JSON document at the end of a stream that also carries
#: numexpr's and nnU-Net's own chatter. `tests/_support/docker_json.py` explains why the
#: obvious one-liner for this is wrong and what it cost here.
_json_tail = last_json_object


# =====================================================================================
# 1. The stamp describes THIS image
# =====================================================================================
def test_the_image_carries_the_build_stamp_entry_82_asks_for(image: str) -> None:
    result = _docker("doctor")
    assert result.returncode in (0, 2), result.stderr[-2000:]
    stamp = _json_tail(result.stdout)["stamp"]
    assert "error" not in stamp, stamp
    assert len(stamp["code_commit"]) in (40, 64), stamp["code_commit"]
    assert stamp["image_digest"].startswith("sha256:")
    assert isinstance(stamp["code_dirty"], bool)


def test_the_stamp_still_describes_the_image_it_was_stamped_into(image: str) -> None:
    """Entry 82's property, checked without building twice.

    The stamp was written during `docker build` by computing an inventory of the image's
    own content. Recomputing it now must give the same answer -- and it gives a DIFFERENT
    answer as soon as any of the interpreter, the installed Python distributions, the
    dpkg package set or the copied source changes, which is exactly when a rebuild has
    produced a different image.

    This is the fast form of "rebuild and re-read". The slow form was run by hand while
    this change was made and is recorded in `trainer/README.md`: adding `gcc` to
    the image moved the recorded digest, and the OCI image id moved on every build
    including the ones that changed nothing -- which is the argument for recording the
    inventory digest rather than the image id.
    """
    recomputed = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "run", "--rm", "--entrypoint", "python", IMAGE, "-c",  # noqa: S607
         "import json;from medos_trainer.stamp import content_digest, read_stamp;"
         "print(json.dumps({'stored': read_stamp()['image_digest'],"
         "'recomputed': content_digest()[0]}))"],
        capture_output=True, text=True, timeout=_TIMEOUT, check=False,
    )
    assert recomputed.returncode == 0, recomputed.stderr[-2000:]
    answer = _json_tail(recomputed.stdout)
    assert answer["stored"] == answer["recomputed"], (
        "the image's recorded image_digest no longer describes the image. That is "
        "register entry 82's defect -- a provenance value that stopped matching the "
        "thing it describes -- and it would be recorded in the binding of every "
        "training run this image performs"
    )


# =====================================================================================
# 2. The platform parses what the trainer emits
# =====================================================================================
def test_the_platform_reads_the_declaration_the_trainer_writes(image: str) -> None:
    """`declare-environment` -> `load_training_environment`, with no hand editing.

    This is the whole of entry 82's fix in one assertion: the nine keys
    `medos/medos/api/routes_training.py` refuses `503` without are produced by the image that
    would do the training, read by the code that would refuse, and nobody typed any of
    them into `docker-compose.yml`.
    """
    from medos.api.routes_training import ENVIRONMENT_VAR, load_training_environment

    # `--allow-cpu` so the assertion is about the DECLARATION and not about whether this
    # host has a GPU; the refusal without one is its own test below.
    result = _docker("declare-environment", "--allow-cpu")
    assert result.returncode == 0, result.stderr[-2000:]
    document = _json_tail(result.stdout)

    environment = load_training_environment({ENVIRONMENT_VAR: json.dumps(document)})
    assert environment.code_commit == document["code_commit"]
    assert environment.image_digest.startswith("sha256:")
    # `MOS-REL-037`: an exact pin, and it is the version that is installed.
    assert environment.version_of("nnunet") == document["backend_versions"]["nnunet"]
    # And the backend this image did NOT install is refused rather than invented.
    from medos.api.routes_training import EnvironmentNotRecorded

    with pytest.raises(EnvironmentNotRecorded, match="backend_versions.auto3dseg"):
        environment.version_of("auto3dseg")


def test_the_declaration_binds_a_preprocessing_spec_the_router_accepts(image: str) -> None:
    """`MOS-IMG-045`'s registered artifact, with all four keys `_SPEC_KEYS` demands."""
    from medos.api.routes_training import ENVIRONMENT_VAR, load_training_environment

    result = _docker("declare-environment", "--allow-cpu")
    assert result.returncode == 0, result.stderr[-2000:]
    document = _json_tail(result.stdout)
    environment = load_training_environment({ENVIRONMENT_VAR: json.dumps(document)})

    bound = environment.spec_for("lung_segmentation")
    assert bound["digest"].startswith("sha256:")
    assert bound["output_kind"] == "label"
    assert int(bound["version"]) >= 1


def test_the_declaration_is_written_as_a_file_the_api_reads_with_an_at_path(
    image: str, tmp_path: Any
) -> None:
    """`MEDOS_TRAINING_ENVIRONMENT=@<path>`, which is the form compose uses.

    `load_training_environment` supports both a JSON literal and `@<path>`; the compose
    stack uses the file, because the nine keys are a document this deployment's YAML
    does not contain and must not.
    """
    from medos.api.routes_training import ENVIRONMENT_VAR, load_training_environment

    result = _docker("declare-environment", "--allow-cpu")
    assert result.returncode == 0, result.stderr[-2000:]
    target = tmp_path / "training-environment.json"
    target.write_text(json.dumps(_json_tail(result.stdout)), encoding="utf-8")

    environment = load_training_environment({ENVIRONMENT_VAR: f"@{target}"})
    assert environment.code_commit


# =====================================================================================
# 3. Without a GPU it refuses, and the refusal says so
# =====================================================================================
def test_without_a_gpu_the_trainer_refuses_rather_than_falling_back_to_cpu(
    image: str,
) -> None:
    """The container is started with no device, which is what `_docker` does by default.

    A trainer that silently ran a 3D segmentation fit on CPU would take days and record
    a `hardware` block naming a GPU it never used -- false inside `MOS-TRAIN-124`'s
    binding, in the one record `MOS-TRAIN-126` exists to make trustworthy.
    """
    result = _docker("declare-environment")
    assert result.returncode != 0, (
        "declare-environment succeeded with no GPU attached. Either this host leaked a "
        "device into a container that asked for none, or the refusal has been removed"
    )
    combined = result.stdout + result.stderr
    assert "no CUDA device is visible" in combined, combined[-2000:]
    assert "MEDOS_TRAINER_ALLOW_CPU" in combined, "the refusal does not name the opt-in"


def test_an_explicitly_permitted_cpu_deployment_is_recorded_as_one(image: str) -> None:
    """The opt-in does not pretend. `gpu_count: 0` and `accelerator: cpu`, in the record."""
    result = _docker("declare-environment", "--allow-cpu")
    assert result.returncode == 0, result.stderr[-2000:]
    hardware = _json_tail(result.stdout)["hardware"]
    assert hardware["gpu_count"] == 0
    assert hardware["gpu_model"] == "absent"
    assert hardware["accelerator"] == "cpu"
    assert hardware["cpu_run_explicitly_permitted"] is True


# =====================================================================================
# 4. The image is the one the boundary test says it is
# =====================================================================================
#: `trainer/requirements.txt` -- the pin itself, not a copy of it.
#:
#: This assertion used to restate `2.5.1` in this file. The requirements file moved to
#: `2.7.1+cu128` for Blackwell (`sm_120`; the cu124 wheels carry no kernel for it and its
#: sm_90 PTX does not JIT across a major architecture), the copy here did not, and the
#: test then failed against an image that was exactly right. A pinned version asserted in
#: two places is a pin in neither.
_REQUIREMENTS = Path(__file__).resolve().parents[2] / "trainer" / "requirements.txt"


def _pinned(package: str) -> str:
    for line in _REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        head = line.split("#", 1)[0].strip()
        if head.startswith(f"{package}=="):
            return head.split("==", 1)[1]
    raise AssertionError(
        f"{package!r} is not pinned in {_REQUIREMENTS.name}. It is installed into the "
        "trainer image by something this check cannot see, so the version the record "
        "names is unverifiable."
    )


def test_the_trainer_image_carries_the_toolchain_the_platform_must_not(image: str) -> None:
    """The other half of `tests/integration/test_trainer_boundary.py`.

    That module asserts `medicalos/medos` CANNOT import torch. Without this one it would
    pass on a deployment where nothing can train at all.
    """
    result = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "run", "--rm", "--entrypoint", "python", IMAGE, "-c",  # noqa: S607
         "import json, torch, nnunetv2;"
         "print(json.dumps({'torch': torch.__version__,"
         "'nnunetv2': nnunetv2.__file__ is not None}))"],
        capture_output=True, text=True, timeout=_TIMEOUT, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    answer = _json_tail(result.stdout)
    assert answer["torch"] == _pinned("torch"), (
        f"the image carries torch {answer['torch']}, `trainer/requirements.txt` pins "
        f"{_pinned('torch')}. `MOS-REL-037` forbids a range on a recorded version and "
        "`backend_versions` in MEDOS_TRAINING_ENVIRONMENT is one, so these are equal or "
        "the record is wrong about what trained the model."
    )
    assert answer["nnunetv2"] is True
