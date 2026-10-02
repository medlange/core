# SPDX-License-Identifier: Apache-2.0
"""What Train is allowed to reach for in Core, enumerated, so it cannot grow quietly.

WHY
---
MedicalOS ships as two products out of one repository, and the question that decides
whether they could ever ship out of TWO repositories is not the database -- of 45
tables, 29 are written only by Core and 12 only by Train, and no Core table has a
foreign key into a Train table. It is this: **Core has no declared public API.**

Train reaches into Core at 47 names across 21 modules. Inside one repository a Core
refactor that moves one of them breaks Train and a test says so in the same run. Across
two repositories it breaks at a version boundary, later, for somebody else, and the only
defence is knowing which names were load-bearing. This file is that knowledge, written
down and checked.

WHAT IT ASSERTS, AND WHY BOTH HALVES ARE NEEDED
-----------------------------------------------
1. **Every name Train imports from Core appears in that module's `__all__`.** A module's
   `__all__` is the only machine-readable statement this codebase makes about what is
   public, and every Core module Train touches already had one -- they simply were not
   enforced at the boundary. Enforcing them found
   `medos.sdk.refusal._RefusalError`: a PRIVATE name that eleven exception
   classes inherit from, seven of them in `medos/medos/training/`, and that
   `routes_training.py` catches by name to render every refusal the model-preparation
   surface can raise. It is now `RefusalError` and exported, which is what it always was.

2. **The reach set itself is frozen.** `__all__` alone would let Train start depending
   on thirty more public names without anyone deciding. `FROZEN` below is the surface as
   it stands; adding to it is a deliberate edit with a reviewer, which is the whole
   point. Removing from it is equally a change -- a Core export nothing reaches for is a
   promise nobody needs to keep.

WHAT THIS IS NOT
----------------
Not a semver contract, and it does not pretend to be one. It makes the surface VISIBLE
and CONSTANT. Versioning it is the step after splitting the repositories, and doing it
before there are two release cadences would be ceremony.

Spec: MOS-CONF-109 (segregation), MOS-REL-046.
"""

from __future__ import annotations

import ast
import importlib
from collections import defaultdict
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: The Train product's source. Two of these are router modules that still live under
#: `medos/medos/api/` -- `medos/tools/permcheck.py` requires every `minted_by` to name a
#: file there, and moving them into `medos/medos/training/` would open the promotion edge
#: `MOS-TRAIN-189` keeps closed. They are Train's by ownership, not by directory.
TRAIN_SOURCES: tuple[str, ...] = (
    "medos/medos/training",
    "medos/medos/api/routes_training.py",
    "medos/medos/api/routes_curation.py",
    "medos/medos/api/training_plane.py",
)

#: Module prefixes that ARE Train, so an import between them is not a boundary crossing.
TRAIN_PREFIXES: tuple[str, ...] = (
    "medos.training",
    "medos.api.routes_training",
    "medos.api.routes_curation",
    "medos.api.training_plane",
)

#: EVERY NAME TRAIN IMPORTS FROM CORE. Adding a line here is how you declare a new
#: dependency of the model-preparation product on the PACS-and-models product, and it is
#: meant to be an edit somebody reads.
#:
#: Values ending in a submodule name (`medos.evidence: repo`) are `from package import
#: submodule`, which `__all__` does not govern -- a package cannot hide a submodule that
#: exists. They are listed anyway, because the point is the surface and not the
#: mechanism.
FROZEN: dict[str, frozenset[str]] = {
    "medos.api": frozenset({"db_connection"}),
    "medos.api.app": frozenset({"create_app"}),
    "medos.api.auth": frozenset({"principal_of"}),
    "medos.api.problems": frozenset(
        {
            "RETRYABLE_CLASSES",
            "build_problem",
            "json_safe",
            "now_rfc3339",
            "problem_response",
            "trace_id_of",
        }
    ),
    "medos.capabilities": frozenset({"metadata_for"}),
    "medos.config.devmode": frozenset({"refuse_dev_value_outside_dev"}),
    # `medos.core.canonical` USED TO BE HERE and is not any more. It did not stop being
    # imported -- it stopped being CORE. `MOS-IMG-003` requires `medicalos-preprocessing`
    # as its own package and this repository had built it inside `medos/medos/`; the digest rule
    # went with it, because both the platform and the trainer digest against it and
    # `MOS-REL-032` allows exactly one canonicaliser. A package below both is not a
    # boundary Train crosses, so freezing it here would be declaring a dependency that is
    # no longer the one this file is about.
    "medos.core.statements": frozenset({"EQUIVALENCE_DISCLAIMER"}),
    "medos.db": frozenset({"audit"}),
    "medos.db.tenancy": frozenset(
        {"bind_current_tenant", "current_tenant", "reset_current_tenant", "tenant_tx"}
    ),
    "medos.dicomweb.gateway": frozenset(
        {"DicomWebGateway", "GatewayConfig", "PacsCredentials"}
    ),
    "medos.evidence": frozenset(
        {"acceptance", "digest", "leakage", "repo", "stratification"}
    ),
    # `medos.evidence.errors` left for the same reason as `canonical`, one line above: the
    # refusal base types are what the shared package raises, so they went with it as
    # `medos.sdk.refusal`. Renamed rather than kept, because a flat package
    # cannot hold two modules called `errors`.
    "medos.evidence.leakage": frozenset(
        {"LeakageCheck", "LeakageReport", "dhash64", "waiver_blocks_training"}
    ),
    "medos.evidence.manifest": frozenset(
        {"ACQUISITION_FIELDS", "Acquisition", "SeriesRecord"}
    ),
    "medos.evidence.profile": frozenset({"acquisition_profile"}),
    "medos.evidence.store": frozenset(
        {"InMemoryManifestStore", "ManifestStore", "S3ManifestStore"}
    ),
    "medos.objectstore.s3": frozenset({"S3Client", "S3Config"}),
    "medos.registry": frozenset({"repo"}),
    "medos.registry.digest": frozenset({"content_digest_of"}),
    "medos.registry.errors": frozenset({"InvalidManifest"}),
    "medos.security.scopes": frozenset({"scope_permits"}),
}


def _train_files() -> list[Path]:
    out: list[Path] = []
    for entry in TRAIN_SOURCES:
        path = REPO / entry
        if path.is_file():
            out.append(path)
        else:
            out += [p for p in path.rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(out)


def _actual_reach() -> dict[str, set[str]]:
    """Core module -> names Train imports from it, by static read of Train's source."""
    reach: dict[str, set[str]] = defaultdict(set)
    for file in _train_files():
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if not node.module.startswith("medos."):
                    continue
                # `medos.sdk` is the SDK, not Core: the package the contracts live in
                # moved INSIDE the distribution when the platform became the SDK, and a
                # package below both products is not a boundary Train crosses. Freezing
                # its names here would declare a dependency this file is not about.
                if node.module.startswith("medos.sdk"):
                    continue
                if node.module.startswith(TRAIN_PREFIXES):
                    continue
                for alias in node.names:
                    reach[node.module].add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("medos.sdk"):
                        continue
                    if alias.name.startswith("medos.") and not alias.name.startswith(
                        TRAIN_PREFIXES
                    ):
                        reach[alias.name].add("<module>")
    return dict(reach)


def _submodules_of(module_name: str) -> set[str]:
    module = importlib.import_module(module_name)
    origin = getattr(module, "__file__", None)
    if not origin or Path(origin).name != "__init__.py":
        return set()
    return {p.stem for p in Path(origin).parent.glob("*.py") if p.stem != "__init__"}


# --------------------------------------------------------------------------------------
# the surface is what it says it is
# --------------------------------------------------------------------------------------


def test_train_reaches_for_nothing_outside_the_frozen_surface() -> None:
    """A new dependency of Train on Core is a decision, not a diff nobody read."""
    extra: list[str] = []
    for module, names in sorted(_actual_reach().items()):
        allowed = FROZEN.get(module, frozenset())
        extra += [f"{module}.{n}" for n in sorted(names - allowed)]
    assert not extra, (
        "Train imports these from Core and FROZEN does not list them:\n  "
        + "\n  ".join(extra)
        + "\n\nIf the dependency is intended, add it here -- that edit is the review. "
        "If it is not, the thing Train needs probably belongs in a package both planes "
        "depend on; `medos/medos/core/statements.py` exists because one string did."
    )


def test_the_frozen_surface_has_no_rows_nobody_uses() -> None:
    """The other direction. A declared dependency that no longer exists is a promise
    Core is keeping for nobody, and it makes the surface look larger than it is -- which
    matters, because the size of this surface is the cost of splitting the repositories."""
    actual = _actual_reach()
    stale: list[str] = []
    for module, names in sorted(FROZEN.items()):
        reached = actual.get(module, set())
        stale += [f"{module}.{n}" for n in sorted(names - reached)]
    assert not stale, (
        "FROZEN lists these and Train no longer imports them:\n  "
        + "\n  ".join(stale)
        + "\n\nDelete the rows. Core owes nothing to a caller that went away."
    )


# --------------------------------------------------------------------------------------
# and everything on it is actually public
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", sorted(FROZEN))
def test_every_frozen_name_is_exported_by_its_module(module: str) -> None:
    """`__all__` is the only machine-readable statement this codebase makes about what
    is public. Enforcing it at this boundary is what found `_RefusalError`."""
    imported = importlib.import_module(module)
    declared = set(getattr(imported, "__all__", []) or [])
    submodules = _submodules_of(module)
    missing = sorted(
        name
        for name in FROZEN[module]
        if name not in declared and name not in submodules and name != "<module>"
    )
    assert not missing, (
        f"{module} does not export {missing} but Train imports them.\n"
        f"Either add them to {module}.__all__ -- which is a decision that they are "
        f"public and will stay -- or stop reaching for them."
    )


def test_no_frozen_name_is_private() -> None:
    """The specific regression. `medos.sdk.refusal._RefusalError` was
    reached across the product boundary by eleven classes; the underscore said internal
    and it was not. A leading underscore on this surface means somebody declared a private
    name public by using it, which is the opposite of the order those two things should
    happen in."""
    private = sorted(
        f"{module}.{name}"
        for module, names in FROZEN.items()
        for name in names
        if name.startswith("_") and name != "<module>"
    )
    assert not private, (
        f"the frozen surface contains private names: {private}. Rename them, or decide "
        f"they are public and drop the underscore -- reaching for one across the "
        f"Core/Train boundary is coupling that is invisible until somebody tries to "
        f"separate the two."
    )


def test_every_frozen_name_actually_resolves() -> None:
    """A typo here would silently shrink the surface this file claims to guard."""
    broken: list[str] = []
    for module, names in sorted(FROZEN.items()):
        imported = importlib.import_module(module)
        submodules = _submodules_of(module)
        for name in sorted(names):
            if name == "<module>" or name in submodules:
                continue
            if not hasattr(imported, name):
                broken.append(f"{module}.{name}")
    assert not broken, f"FROZEN names that do not exist: {broken}"


def test_the_scanner_finds_the_train_sources() -> None:
    """If `TRAIN_SOURCES` stopped matching anything, every assertion above would pass
    over an empty reach set.

    The floor was 20 and is 15. Six modules left `medos/medos/training/` for
    `medos.sdk` -- spec, chain, preprocess, bundle, autoconfig, fixtures --
    so Train is genuinely smaller, and the count went 23 -> 17. Lowering a floor because
    the tree shrank is the honest edit; lowering it because a glob broke is the failure
    this assertion exists to catch, which is why the two are worth telling apart out loud.
    """
    files = _train_files()
    assert len(files) > 15, f"only {len(files)} Train source files found"
    reach = _actual_reach()
    assert len(reach) > 15, f"Train appears to import from only {len(reach)} Core modules"


def test_the_surface_is_small_enough_to_be_worth_freezing() -> None:
    """Not a style rule -- a budget. This number IS the cost of splitting the
    repositories, and it should be argued down rather than allowed to drift up."""
    total = sum(len(v) for v in FROZEN.values())
    assert total <= 60, (
        f"Train now depends on {total} Core names across {len(FROZEN)} modules. Every "
        f"one is something a second repository would have to pin a version against."
    )
