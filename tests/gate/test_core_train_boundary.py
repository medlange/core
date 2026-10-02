# SPDX-License-Identifier: Apache-2.0
"""Core cannot train, and the check is a static import closure rather than a promise.

THE TWO DEPLOYABLES
-------------------
MedicalOS ships as two services out of one repository:

  * **Core** (`medos.api.app:create_app`) -- the PACS-and-models service. DICOM in and
    out through the credentialed gateway, jobs, capabilities, the service and model
    registries, results, reviews.
  * **Train** (`medos.api.training_plane:create_training_app`) -- Core plus the
    model-preparation surface: harvesting, curation, dataset versions, splits,
    annotation sets, training runs, configuration searches, conversion runs.

Train is Core plus routers. That is the cheap direction, and this file is what stops it
drifting into a fork.

WHY A CLOSURE AND NOT A ROUTE COUNT
-----------------------------------
Counting Core's routes would catch a router mounted by accident and nothing else. The
property that matters is stronger and quieter: a Core process must never IMPORT the
training engine. An import is what pulls `medos.training` -- and through it the cohort,
seal and conversion machinery -- into the address space of a service whose whole claim
is that it does not do those things. It is also what a future convenience import would
add without anyone noticing, because nothing at run time would change.

`MOS-REL-108` forbids dynamic module import in a platform process on `MOS-CONF-109`'s
IEC 62304 section 4.3 segregation argument, so the composition is static and this check
can be static too. It reads source, not `sys.modules`: an import inside a function body
is still a path, and a deferred import is the most likely shape of an accidental one.

THE ONE DELIBERATE EXCEPTION, and why it is not a hole: `medos/medos/cli/doctor.py` imports
`medos.training.channelmap` inside a function, guarded, to report on channel tables. It
is a CLI and not the served app, the import is caught and reported as a plane that is
absent, and `medos.api.app` does not reach it. The assertion below is on the APP's
closure for exactly that reason -- widening it to the whole `medos` package would catch
a diagnostic and teach the next person to delete the check.

Spec: MOS-REL-108, MOS-CONF-109, MOS-TRAIN-189, MOS-TRAIN-225.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.gate_0_3_0

#: The training engine, and the two routers that surface it. A Core process must import
#: none of them.
TRAIN_ONLY: tuple[str, ...] = (
    "medos.training",
    "medos.api.routes_training",
    "medos.api.routes_curation",
    "medos.api.training_plane",
)

#: Where each deployable is built. These two strings are the product boundary.
CORE_FACTORY = ("medos.api.app", "create_app")
TRAIN_FACTORY = ("medos.api.training_plane", "create_training_app")


def _closure(module: str) -> set[str]:
    """Every first-party module transitively reachable from `module`, by static read.

    The same shape as `tests/gate/test_no_auto_promote.py::_closure`, over a single
    module rather than a package: the subject here is one entrypoint, not a directory.
    """
    seen: set[str] = set()
    frontier = [module]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, AttributeError, ValueError):
            continue  # `from medos.x import Y` where Y is a class, not a module edge
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            continue
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                frontier += [a.name for a in node.names if a.name.startswith("medos.")]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("medos."):
                    frontier.append(node.module)
                    # `from medos.pkg import submodule` is an edge to `medos.pkg.submodule`
                    frontier += [f"{node.module}.{a.name}" for a in node.names]
    return seen


@pytest.fixture(scope="module")
def core_closure() -> set[str]:
    return _closure(CORE_FACTORY[0])


@pytest.mark.parametrize("forbidden", TRAIN_ONLY)
def test_core_train_boundary_the_core_app_never_imports_the_training_plane(
    core_closure: set[str], forbidden: str
) -> None:
    reached = sorted(
        m for m in core_closure if m == forbidden or m.startswith(forbidden + ".")
    )
    assert not reached, (
        f"the Core app's import closure reaches {reached}.\n"
        f"Core is the PACS-and-models service and must not carry the training engine. "
        f"If a Core surface genuinely needs something from there, the thing it needs "
        f"belongs in a package both planes depend on -- `medos/medos/core/statements.py` "
        f"exists because one string did."
    )


def test_core_train_boundary_train_does_import_it_so_the_check_can_fail() -> None:
    """If the training plane were unreachable from anywhere, the assertions above would
    pass for the wrong reason. Train imports it; that is the point of Train."""
    closure = _closure(TRAIN_FACTORY[0])
    for expected in TRAIN_ONLY:
        assert any(m == expected or m.startswith(expected + ".") for m in closure), (
            f"the Train app does not reach {expected}; has the plane moved?"
        )


def test_core_train_boundary_both_factories_exist_and_are_callable() -> None:
    """A typo in either name would make every closure test above vacuous."""
    for module_name, attribute in (CORE_FACTORY, TRAIN_FACTORY):
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{module_name}:{attribute}"


def test_core_train_boundary_core_is_a_strict_subset_of_train() -> None:
    """Train is Core PLUS routers. If Core ever served a path Train does not, the two
    have started to fork and the cheap split has stopped being cheap."""
    from medos.api.app import create_app
    from medos.api.training_plane import create_training_app

    def paths(app) -> set[tuple[str, str]]:  # noqa: ANN001
        out = set()
        for route in app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", "")
            if path.startswith("/api/v1"):
                out |= {(m, path) for m in methods - {"HEAD", "OPTIONS"}}
        return out

    core = paths(create_app(connect=lambda **_: None))
    train = paths(create_training_app(connect=lambda **_: None))
    assert core < train, (
        f"Core serves {sorted(core - train)} which Train does not. Train must be Core "
        f"plus routers, never a different application."
    )
    assert len(train - core) > 20, (
        f"Train adds only {len(train - core)} routes over Core; the training surface is "
        f"table 10.2-B rows R6-R31 and should be substantially larger than that."
    )


def test_core_train_boundary_core_serves_no_route_that_prepares_a_model() -> None:
    """The route-level statement of the same boundary, in the vocabulary an operator
    reads. A closure check would not notice a hand-written handler."""
    from medos.api.app import create_app

    app = create_app(connect=lambda **_: None)
    served = {getattr(r, "path", "") for r in app.routes}
    forbidden = (
        "training-runs",
        "harvest-batches",
        "harvest-candidates",
        "dataset-versions",
        "dataset-splits",
        "annotation-sets",
        "sampling-plans",
        "configuration-searches",
        "conversion-runs",
        "seal-runs",
    )
    offenders = sorted(p for p in served if any(f in p for f in forbidden))
    assert not offenders, (
        f"the Core service serves {offenders}. Those are model-preparation routes and "
        f"belong to the Train deployable."
    )
