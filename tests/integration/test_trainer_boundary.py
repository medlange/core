# SPDX-License-Identifier: Apache-2.0
"""The platform image carries no training toolchain, and this is what holds it there.

THE PROPERTY, AND WHY IT IS WORTH A TEST FILE
-----------------------------------------------
`medos/medos/training/chain.py` generates MONAI Bundle configs as DATA -- it emits
`{"_target_": "monai.transforms.Flip"}` and never imports MONAI, and it deliberately
fully-qualifies the one MedicalOS transform "because a bare name would be resolved
against `monai.transforms` and silently fail to be the MedicalOS one". `MOS-TRAIN-225`
forbids the nnU-Net planner, `nnUNetPlansManager` and `Auto3DSeg`'s `DataAnalyzer` from
the serving image's import closure BY NAME, and requires "CI MUST assert their absence by
module-name grep over the resolved closure". `MOS-REL-108` forbids in-process plugin
loading, so there is no supported way for a training backend to arrive later either.

Adding `trainer/` is exactly the change that could have broken this, which is why
the assertions arrived with it. The trainer installs `medos`; `medos` must never install
the trainer, and the direction of that arrow is the whole separation.

FOUR CHECKS, THREE OF WHICH NEED NOTHING RUNNING
--------------------------------------------------
  1. The declared dependency sets name no training package. Cheap, and it is the check
     that fails on the pull request rather than on the deployment.
  2. `medos`'s own resolved import closure contains nothing on `MOS-TRAIN-225`'s list.
  3. Nothing under `medos/medos/` imports `medos_trainer`.
  4. The RUNNING platform image cannot import torch, MONAI or nnU-Net. This one needs the
     stack and skips without it, and it is the only one that is about the artifact rather
     than about the source.

Spec: MOS-TRAIN-136, MOS-TRAIN-225, MOS-REL-039, MOS-REL-108.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests._support.roots import REPO_ROOT, child_pythonpath
from tests._support.skips import skip_infra

_TRAINER_TREE = REPO_ROOT / "trainer"
if not (_TRAINER_TREE / "requirements.txt").is_file():
    pytest.skip(
        "the trainer tree is not present in this checkout (core split) -- "
        "the same gates run in medlange/trainer's own CI",
        allow_module_level=True,
    )


#: The distributions that must never appear in the platform image. `nnunetv2` and
#: `monai` are `MOS-TRAIN-225`'s subject; `torch` is the one that carries both and is the
#: 2.5 GB that makes the separation worth having.
FORBIDDEN_IN_PLATFORM: tuple[str, ...] = (
    "torch", "monai", "nnunet", "nnunetv2", "batchgenerators",
    "dynamic-network-architectures", "acvl-utils",
)


def _lines(path: Path) -> list[str]:
    return [
        line.split("#", 1)[0].strip().lower()
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_the_platform_declares_no_training_dependency() -> None:
    """`pyproject.toml` and the medos image's pins. The check that fails early."""
    for relative in ("pyproject.toml", "medos/deploy/compose/requirements.txt"):
        text = "\n".join(_lines(REPO_ROOT / relative))
        for package in FORBIDDEN_IN_PLATFORM:
            assert package not in text, (
                f"{relative} names {package!r}. MOS-TRAIN-225 keeps the nnU-Net planner "
                "out of the serving image's import closure and medos/medos/training/chain.py's "
                "whole design is that MONAI Bundle configs are DATA the platform "
                "generates and never imports. The trainer is trainer/"
            )


def test_the_trainer_declares_the_training_dependencies_and_the_platform_does_not() -> None:
    """The positive control. Without it the check above passes on an empty repository."""
    text = "\n".join(_lines(REPO_ROOT / "trainer" / "requirements.txt"))
    assert "torch==" in text and "nnunetv2==" in text, (
        "trainer/requirements.txt pins neither torch nor nnU-Net, so the "
        "assertion above is passing vacuously"
    )


def test_nothing_under_medos_imports_the_trainer() -> None:
    """One-way, and the direction is the separation.

    `trainer` installs `medos` so that `MOS-TRAIN-129`'s bundle layout and
    `MOS-TRAIN-131`'s config generator have exactly one implementation. The reverse
    import would put torch in the platform's closure through the back door.
    """
    # An IMPORT, not a mention. `medos/medos/training/orchestrator.py` names
    # `medos.sdk/contract.py` in prose, because the two halves of the
    # run directory have to point at each other for a reader; naming a module is not
    # importing it, and a check that could not tell the difference would push the
    # cross-reference out of the comment where it is useful.
    importing = re.compile(r"^\s*(?:from\s+medos_trainer|import\s+medos_trainer)", re.M)
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "medos" / "medos").rglob("*.py"))
        if importing.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, f"{offenders} import medos_trainer"


def test_the_platform_import_closure_carries_no_forbidden_module() -> None:
    """`MOS-TRAIN-225`'s grep, run against the closure `medos.training` actually pulls.

    `medos.sdk.autoconfig.serving_closure_violations` is the check and it lives
    beside the list on purpose ("so that the list and the requirement live together").

    IN A FRESH PROCESS, AND THAT IS NOT A DETAIL. This used to read the ambient
    `sys.modules` of the pytest process, which means its verdict depended on what had been
    imported BEFORE it by something else entirely. Measured: `pytest tests/integration`
    alone -> green; `pytest trainer/tests tests/integration/test_trainer_boundary.py` ->
    RED, naming seven `nnunetv2` modules, because the trainer's own suite imports
    `nnunetv2` at module level and every module it touches stays in `sys.modules` for the
    rest of the run.

    Nothing was wrong with the platform on either run. The default collection order
    happened to put `tests/integration` before any suite that imports the toolchain, so
    the check had been passing BY LUCK -- and luck runs in both directions. A gate whose
    answer depends on what ran before it is not measuring the thing it names.

    A subprocess has one importer, one `sys.modules`, and nothing in it but what this
    check asked for.
    """
    import json
    import os
    import subprocess
    import sys

    probe = (
        "import importlib, json, sys\n"
        "for m in ("
        "'medos.sdk.autoconfig','medos.sdk.bundle',"
        "'medos.sdk.chain','medos.sdk.preprocess',"
        "'medos.training.runs','medos.training.orchestrator','medos.training.supervisor',"
        "'medos.api.routes_training'):\n"
        "    importlib.import_module(m)\n"
        "from medos.sdk.autoconfig import serving_closure_violations\n"
        "print(json.dumps(list(serving_closure_violations(list(sys.modules)))))\n"
    )
    # `cwd` is not enough to import this repository any more: `medos` is `medos/medos/`
    # and `medos.sdk` is at the root, so the child needs both on its path.
    # Without them this probe raised ModuleNotFoundError and the assertion below read
    # "the closure could not be resolved", which is true but says nothing about MOS-TRAIN-225.
    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath(inherit=env.get("PYTHONPATH"))
    run = subprocess.run(  # noqa: S603 - argv vector, no shell
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=str(REPO_ROOT), check=False, env=env,
    )
    assert run.returncode == 0, (
        "the platform's import closure could not be resolved in a fresh interpreter, "
        f"which is a failure in itself:\n{run.stderr[-2000:]}"
    )
    violations = json.loads(run.stdout.strip().splitlines()[-1])
    assert not violations, (
        f"{violations} are in the platform's resolved import closure. MOS-TRAIN-225: "
        "the planner, nnUNetPlansManager and DataAnalyzer MUST NOT be present, because "
        "a re-derivation at serve time is 'a materially different transform, executed "
        "by a byte-identical container, against a byte-identical weights digest, with "
        "every structural check passing'"
    )


@pytest.mark.slow
@pytest.mark.parametrize("module", ["torch", "monai", "nnunetv2"])
def test_the_running_platform_image_cannot_import_the_training_toolchain(
    module: str,
) -> None:
    """The artifact, not the source. Needs the stack; skips without it.

    This is the assertion the change that added `trainer/` had to keep true, and
    the only way to keep it honestly is to ask the image.
    """
    probe = subprocess.run(  # noqa: S603 - argv vector, no shell
        ["docker", "exec", "medos-api", "python", "-c", f"import {module}"],  # noqa: S607
        capture_output=True, text=True, timeout=120, check=False,
    )
    if probe.returncode != 0 and "No such container" in (probe.stderr or ""):
        skip_infra(
            "medos-api is not running, so the platform image cannot be asked what it "
            "can import. The source-level checks in this module still ran",
            dependency="medos-api",
        )
    assert probe.returncode != 0, (
        f"medicalos/medos imported {module}. The whole reason trainer/ is a "
        "second image is that this import must fail (MOS-TRAIN-225, MOS-REL-108)"
    )
    assert "ModuleNotFoundError" in (probe.stderr or ""), probe.stderr[:400]
