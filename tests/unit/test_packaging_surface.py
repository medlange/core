# SPDX-License-Identifier: Apache-2.0
"""What `pip install .` actually ships, asserted against what the platform actually is.

WHAT HAPPENED
-------------
`medos/` became a product directory and the package moved down to `medos/medos/`. The
package configuration kept two discovery roots:

    where   = [".", "medos"]
    include = ["medos*", "medos.sdk*"]

`include` is applied to the MERGED result of both roots, so from the root `.` every
subdirectory of the PRODUCT directory matched `medos*` as a namespace package. Measured:
the resolved list held **78 names**, among them `medos.deploy.compose`,
`medos.schemas.lung-nodule`, `medos.web.ohif-extension.src.panels` and `medos.contracts`.
setuptools then infers `package-dir = {"medos": "medos/medos"}` from the second root, so
each of those resolved to a path under `medos/medos/` that does not exist, and

    pip install -e .

failed outright with `error: package directory 'medos\\medos\\contracts' does not exist`.
Every `python -m medos.…` command this repository documents — `medos.cli doctor`,
`medos.security.cli issue`, `medos.gateway.reconcile` — failed from the repository root
with `ModuleNotFoundError: No module named 'medos.security'`, because the stale editable
install still mapped `medos` to the product directory.

WHY NOTHING CAUGHT IT
---------------------
The image builds. `medos/deploy/compose/Dockerfile` does `COPY medos/medos /app/medos`, so
under `/app` the name `medos` IS the package, the root `.` finds it directly, and there is
no product directory for the second root to trip over. The Dockerfile says `pip install .`
"is what proves pyproject.toml's `[tool.setuptools.packages.find]` actually resolves" —
and it proves it for one of the two layouts. The suite never noticed either, because
`pytest` puts `medos/` on `pythonpath` itself, so an import that needs the install works
without it.

WHAT THIS ASSERTS
-----------------
That the distribution is EXACTLY the platform package and the shared package: every
importable subpackage of `medos/medos/`, plus `medos.sdk`, and nothing from
beside them. A new directory next to `medos/medos/` fails here instead of quietly joining
the wheel; a new subpackage inside it fails here instead of quietly being left out of one.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "medos" / "medos"

#: Directories beside the package that are the PRODUCT's, not the distribution's. Each is
#: data or somebody else's code, and the configuration excludes it by name.
NOT_SHIPPED = ("tools", "services", "api/v1", "contracts", "deploy", "examples",
               "schemas", "web")


def _resolved() -> set[str]:
    pytest.importorskip("setuptools")
    from setuptools.config.pyprojecttoml import read_configuration

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = read_configuration(str(ROOT / "pyproject.toml"))
    packages = cfg["tool"]["setuptools"]["packages"]
    assert isinstance(packages, list) and packages, f"no packages resolved: {packages!r}"
    return set(packages)


def _expected() -> set[str]:
    """Every importable subpackage of `medos/medos/`, by walking the tree."""
    names = {"medos.sdk", "medos"}
    for init in PACKAGE.rglob("__init__.py"):
        rel = init.parent.relative_to(PACKAGE)
        if not rel.parts or "__pycache__" in rel.parts:
            continue
        names.add("medos." + ".".join(rel.parts))
    return names


def test_the_distribution_is_the_platform_and_nothing_beside_it() -> None:
    got, want = _resolved(), _expected()

    # `medos.db.migrations` holds SQL and no `__init__.py`; it is discovered as a
    # namespace portion and it SHOULD ship -- `migrate.py` reads those files by path.
    want.add("medos.db.migrations")

    spurious = sorted(got - want)
    assert not spurious, (
        f"{len(spurious)} name(s) resolve into the distribution that are not part of the "
        f"platform package:\n    {spurious}\n"
        "  These are almost certainly directories beside `medos/medos/` that `include = "
        '["medos*"]` matched from the `.` discovery root. `package-dir` sends each of '
        "them to a path under `medos/medos/` that does not exist, so the next "
        "`pip install .` fails with \"package directory ... does not exist\" -- or, worse, "
        "succeeds and ships the product directory inside the wheel. Add an `exclude` "
        "entry in pyproject.toml and say in one line why that directory is not the "
        "platform's."
    )

    missing = sorted(want - got)
    assert not missing, (
        f"{len(missing)} subpackage(s) of `medos/medos/` are NOT in the distribution: "
        f"{missing}\n"
        "  An `exclude` pattern has caught a real package. The name may exist under both "
        "discovery roots -- `medos/api/` is the route tables and `medos/medos/api/` is "
        "the routers -- in which case `package-dir` already sends it to the right one and "
        "it must not be excluded."
    )


@pytest.mark.parametrize("directory", NOT_SHIPPED)
def test_each_excluded_directory_is_really_beside_the_package(directory: str) -> None:
    """The exclusions name real directories, so the list cannot rot into decoration.

    An `exclude` entry for a directory that no longer exists is not harmless: it reads as
    a considered decision about something that is not there, and it hides the fact that
    the thing it was protecting against has moved.
    """
    path = ROOT / "medos" / directory
    assert path.is_dir(), (
        f"pyproject.toml excludes `medos.{directory.replace('/', '.')}*` from the "
        f"distribution, but {path.relative_to(ROOT)} does not exist. Either the directory "
        "moved -- in which case the exclusion is now silently protecting nothing -- or it "
        "was deleted and the line should go with it."
    )
    assert not (path / "__init__.py").exists() or directory in ("tools", "services"), (
        f"{path.relative_to(ROOT)} carries an __init__.py, so it is a real package being "
        "excluded. Only `tools` and `services` are excluded despite being importable, and "
        "that is a recorded decision: tools are not part of the platform and a capability "
        "is somebody else's code (MOS-REL-107)."
    )
