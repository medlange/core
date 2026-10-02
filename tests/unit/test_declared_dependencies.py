# SPDX-License-Identifier: Apache-2.0
"""Every third-party module the code imports by name is declared, or guarded, or named here.

WHY THIS EXISTS, AND WHAT IT COST TO FIND OUT
---------------------------------------------
`medos/medos/evidence/leakage.py` imports `scipy.ndimage.zoom` to compute MOS-EVID-037's
perceptual hash. `scipy` was declared nowhere. The import is unguarded and inside a
function, so a fresh `pip install -e ".[dev]"` did not fail at startup or at import --
it would have failed inside MOS-EVID-034's leakage check, at the moment the check ran.
A SAFETY GATE whose dependency is undeclared does not degrade; it raises, and it raises
in the one place nobody wants a surprise.

It was not alone. `nibabel` is imported by `tests/_support/phantom_cohort.py` and by
`medos/tools/ingest/nnunet_dataset.py`, and was undeclared: on a clean checkout
`tests/unit/test_segmentation_alignment.py` does not FAIL, it raises during COLLECTION,
which reads like a broken checkout rather than a missing package. `pydantic`,
`starlette` and `anyio` were reached transitively through fastapi while being imported
directly by name in 24 places.

None of this was visible from a green suite, because the machine that ran the suite had
all of them installed for other reasons. That is the general shape: A DEVELOPMENT
ENVIRONMENT ACCUMULATES PACKAGES, AND EVERY ONE IT ACCUMULATES IS A DECLARATION THE
PROJECT NO LONGER NEEDS TO GET RIGHT. The only way to keep it right is to derive the
answer from the source rather than from the interpreter.

WHAT THIS ASSERTS
-----------------
1. Every unguarded third-party import under `medos/medos/sdk/` is a CORE runtime
   dependency. The SDK is what a third party installs to read a model card and preprocess
   a volume; its closure is the `dependencies` list and nothing heavier. The REST of
   `medos/medos/` is the platform (the reference local-mode app): its unguarded imports
   may live in `dependencies` OR the `server` extra, which is how `pip install medos`
   stays the SDK while `pip install medos[server]` is the platform.
2. Every unguarded third-party import under `medos/tools/` and `tests/_support/` is declared
   somewhere in pyproject -- runtime or an extra. These are not the platform, so an
   extra is the right home, but "not declared at all" is not.
3. MOS-TRAIN-225, stated as an absence rather than a promise: `medos/medos/` imports neither
   `torch` nor `nnunetv2`, guarded or otherwise. The nnU-Net planner must stay out of
   the serving image's import closure, and the cheapest way to know it has is to look.
4. The scanner detects a planted violation, and does not report a properly guarded
   import as a violation. Without those two, this file is decoration.

WHAT IT DOES NOT COVER, DELIBERATELY
------------------------------------
`medos/examples/` is excluded: it is sample code for third parties and is allowed to
depend on whatever it likes. Saying so here beats a silent exclusion that reads as
coverage. `spikes/` was the other exclusion -- week-0 scratch that imported sibling files
as top-level modules -- and it is deleted, so the exclusion is gone rather than
re-justified.

Spec: MOS-REL-095 (a stated toolchain), MOS-TRAIN-225, MOS-EVID-034, MOS-EVID-037.
"""

from __future__ import annotations

import ast
import re
import sys
import sysconfig
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Directories whose imports must resolve to a declaration. See the docstring for what is
#: excluded and why.
SCANNED: tuple[str, ...] = ("medos/medos", "medos/tools", "tests/_support",
                            "medos/services")

#: Top-level names that are this repository, reached either as a package or through a
#: `sys.path` insertion. `permcheck` and `contracts` are `medos/tools/permcheck.py` and
#: `medos/tools/contracts.py`, imported by `tests/unit/test_permission_contract.py` after it
#: puts `medos/tools/` on the path.
FIRST_PARTY: frozenset[str] = frozenset(
    {
        "medos",
        # `MOS-IMG-003`'s package. First-party and in this repository: it is declared in
        # `[tool.setuptools.packages.find]` beside `medos*` and installed from the tree,
        # not from an index, so an unguarded import of it is not an undeclared dependency.
        # It is listed HERE rather than added to `[project] dependencies` because a
        # distribution that depends on itself is not a dependency, it is a packaging error
        # waiting for the day somebody splits the two into separate wheels -- and on that
        # day this line is the one that has to change, deliberately.
        "medos.sdk",
        "tools",
        "tests",
        "services",
        "examples",
        "api",
        "contracts",
        "schemas",
        "deploy",
        "web",
        "conftest",
        "permcheck",
    }
)

#: Distribution name in pyproject -> the name the code imports it as, where they differ.
IMPORT_NAME: dict[str, str] = {
    "pynrrd": "nrrd",
    "argon2-cffi": "argon2",
}

#: Modules that must NOT appear under `medos/medos/` at all. The value is the reason, and the
#: reason is the test's error message, because a bare name teaches nobody.
FORBIDDEN_IN_PLATFORM: dict[str, str] = {
    "torch": (
        "MOS-TRAIN-225: torch belongs to the trainer image. The platform image must be "
        "installable and servable without it, and an import here -- even a guarded one "
        "-- makes that a claim rather than a fact."
    ),
    "nnunetv2": (
        "MOS-TRAIN-225: the nnU-Net planner must stay out of the serving image's import "
        "closure. `medos/medos/training/chain.py` generates backend configuration AS DATA for "
        "exactly this reason; importing the backend here would undo it."
    ),
}


def _declared() -> dict[str, set[str]]:
    """Import names declared by pyproject, split into runtime and everything."""
    document = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]

    def names(specs: list[str]) -> set[str]:
        out = set()
        for spec in specs:
            dist = re.split(r"[<>=!\[;\s]", spec)[0].strip()
            out.add(IMPORT_NAME.get(dist, dist.lower().replace("-", "_")))
        return out

    runtime = names(project["dependencies"])
    everything = set(runtime)
    server = names(project.get("optional-dependencies", {}).get("server", []))
    for specs in project.get("optional-dependencies", {}).values():
        everything |= names(specs)
    return {"runtime": runtime, "server": server, "any": everything}


def _stdlib() -> frozenset[str]:
    extra = set()
    paths = sysconfig.get_paths()
    for key in ("stdlib", "platstdlib"):
        root = Path(paths[key])
        if root.is_dir():
            for entry in root.iterdir():
                is_pkg = entry.is_dir() and (entry / "__init__.py").exists()
                if entry.suffix == ".py" or is_pkg:
                    extra.add(entry.stem)
    return frozenset(set(sys.stdlib_module_names) | extra)


STDLIB = _stdlib()


def _parents(tree: ast.AST) -> None:
    for parent in ast.walk(tree):
        for _, value in ast.iter_fields(parent):
            for child in value if isinstance(value, list) else [value]:
                if isinstance(child, ast.AST):
                    child.medos_parent = parent  # type: ignore[attr-defined]


#: Exception names that catch an ImportError. `Exception` and `BaseException` are here
#: because they DO catch it -- reading only `ImportError` would have called a genuinely
#: guarded import unguarded, and the fix for that false positive is a declaration the
#: project does not need. Being right about the language beats being strict.
CATCHES_IMPORT_ERROR: frozenset[str] = frozenset(
    {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
)


def _guarded(node: ast.AST) -> bool:
    """True when an ancestor `try` has a handler that would catch an ImportError."""
    current: ast.AST | None = node
    while (current := getattr(current, "medos_parent", None)) is not None:
        if isinstance(current, ast.Try):
            for handler in current.handlers:
                if handler.type is None:  # bare `except:`
                    return True
                names = {n.id for n in ast.walk(handler.type) if isinstance(n, ast.Name)}
                caught = ast.walk(handler.type)
                names |= {n.attr for n in caught if isinstance(n, ast.Attribute)}
                if names & CATCHES_IMPORT_ERROR:
                    return True
    return False


def scan(paths: list[Path]) -> dict[str, list[tuple[str, int, bool]]]:
    """Third-party top-level imports -> [(file, line, guarded)]. The whole check rests on
    this function, which is why `test_the_scanner_sees_a_planted_import` exists."""
    found: dict[str, list[tuple[str, int, bool]]] = {}
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not present today
            continue
        _parents(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                modules = [node.module or ""]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if not top or top in STDLIB or top in FIRST_PARTY:
                    continue
                try:
                    where = path.relative_to(REPO).as_posix()
                except ValueError:
                    where = path.as_posix()
                found.setdefault(top, []).append((where, node.lineno, _guarded(node)))
    return found


def _sources(*roots: str) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        out.extend(sorted((REPO / root).rglob("*.py")))
    return out


# --------------------------------------------------------------------------------------
# the declarations
# --------------------------------------------------------------------------------------


SDK_ROOT = "medos/medos/sdk"


def test_every_unguarded_import_in_the_sdk_is_a_core_runtime_dependency() -> None:
    """`medos/medos/sdk/` is what `pip install medos` delivers to a third party.

    Everything it imports by name has to be in `[project] dependencies` -- not in an
    extra, and not merely transitive. This is the half of the split that keeps the SDK
    installable without a web stack, a database driver or a crypto toolkit.
    """
    declared = _declared()["runtime"]
    sdk_sources = [p for p in _sources("medos/medos") if SDK_ROOT in p.as_posix()]
    offenders = {
        module: [f"{f}:{line}" for f, line, guarded in hits if not guarded]
        for module, hits in scan(sdk_sources).items()
        if module not in declared and any(not guarded for _, _, guarded in hits)
    }
    assert not offenders, (
        "these modules are imported unguarded by medos.sdk and are not declared in "
        "[project] dependencies:\n"
        + "\n".join(f"  {m}: {', '.join(w)}" for m, w in sorted(offenders.items()))
        + "\nDeclare it in the core dependencies, or keep the import out of the SDK. A "
        "transitive dependency that the code names is undeclared."
    )


def test_every_unguarded_import_in_the_platform_is_a_runtime_or_server_dependency() -> None:
    """The platform half of `medos/medos/` (api, worker, gateway, evidence, training).

    An operator installing `medos[server]` gets `dependencies` + the `server` extra, so
    either list is a legitimate home here. The SDK subtree is checked separately and
    strictly above; this scan deliberately excludes it.
    """
    declared = _declared()
    allowed = declared["runtime"] | declared["server"]
    platform_sources = [
        p for p in _sources("medos/medos") if SDK_ROOT not in p.as_posix()
    ]
    offenders = {
        module: [f"{f}:{line}" for f, line, guarded in hits if not guarded]
        for module, hits in scan(platform_sources).items()
        if module not in allowed and any(not guarded for _, _, guarded in hits)
    }
    assert not offenders, (
        "these modules are imported unguarded by the platform and are declared in "
        "neither [project] dependencies nor the server extra:\n"
        + "\n".join(f"  {m}: {', '.join(w)}" for m, w in sorted(offenders.items()))
        + "\nDeclare it, or guard the import and make the caller handle absence. A "
        "transitive dependency that the code names is undeclared."
    )


def test_every_unguarded_import_in_tools_and_support_is_declared_somewhere() -> None:
    """`medos/tools/` and `tests/_support/` are not the platform, so an extra is the right home
    -- but a clean checkout still has to be able to install what they need without
    reading the source to discover it."""
    declared = _declared()["any"]
    forbidden = set(FORBIDDEN_IN_PLATFORM)
    offenders = {
        module: [f"{f}:{line}" for f, line, guarded in hits if not guarded]
        for module, hits in scan(_sources("tools", "tests/_support")).items()
        if module not in declared
        and module not in forbidden
        and any(not guarded for _, _, guarded in hits)
    }
    assert not offenders, (
        "imported unguarded by medos/tools/ or tests/_support/ and declared in no extra:\n"
        + "\n".join(f"  {m}: {', '.join(w)}" for m, w in sorted(offenders.items()))
    )


def test_the_test_suite_can_be_installed_from_the_declarations_alone() -> None:
    """The specific regressions, named so that removing either declaration says why it
    mattered rather than just which assertion moved."""
    declared = _declared()
    assert "nibabel" in declared["any"], (
        "tests/_support/phantom_cohort.py and medos/tools/ingest/nnunet_dataset.py import "
        "nibabel. Without it `pytest tests/unit` raises during collection."
    )
    assert "scipy" in declared["server"], (
        "medos/medos/evidence/leakage.py computes MOS-EVID-037's hash with scipy.ndimage. "
        "It is a dependency of a safety gate and belongs in the platform's server set, "
        "not off the declarations."
    )


# --------------------------------------------------------------------------------------
# MOS-TRAIN-225, as an absence
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("module", sorted(FORBIDDEN_IN_PLATFORM))
def test_the_platform_does_not_import_the_training_backend(module: str) -> None:
    hits = scan(_sources("medos/medos")).get(module, [])
    assert not hits, (
        f"medos/medos/ imports {module} at "
        + ", ".join(f"{f}:{line}" for f, line, _ in hits)
        + f"\n{FORBIDDEN_IN_PLATFORM[module]}"
    )




# --------------------------------------------------------------------------------------
# the scanner, checked against itself
# --------------------------------------------------------------------------------------


def test_the_scanner_sees_a_planted_import(tmp_path: Path) -> None:
    """A check that cannot fail is not a check. Plant one and watch it be found."""
    planted = tmp_path / "planted.py"
    planted.write_text("import definitely_not_a_real_package\n", encoding="utf-8")
    found = scan([planted])
    assert "definitely_not_a_real_package" in found
    assert found["definitely_not_a_real_package"][0][2] is False, "should read as unguarded"


def test_the_scanner_sees_a_planted_from_import(tmp_path: Path) -> None:
    planted = tmp_path / "planted_from.py"
    planted.write_text("from another_fake_package.sub import thing\n", encoding="utf-8")
    assert "another_fake_package" in scan([planted])


def test_a_guarded_import_reads_as_guarded(tmp_path: Path) -> None:
    """The other direction. `medos/medos/bus/kafka.py` imports confluent_kafka inside a try, and
    treating that as a violation would force a declaration the platform deliberately does
    not make."""
    planted = tmp_path / "guarded.py"
    planted.write_text(
        "try:\n"
        "    import some_optional_backend\n"
        "except ImportError:\n"
        "    some_optional_backend = None\n",
        encoding="utf-8",
    )
    hits = scan([planted])["some_optional_backend"]
    assert hits[0][2] is True, "an import inside try/except ImportError must read as guarded"


def test_a_bare_except_also_reads_as_guarded(tmp_path: Path) -> None:
    planted = tmp_path / "bare.py"
    planted.write_text(
        "try:\n    import x_optional\nexcept Exception:\n    pass\n", encoding="utf-8"
    )
    assert scan([planted])["x_optional"][0][2] is True


def test_a_relative_import_is_not_third_party(tmp_path: Path) -> None:
    """`from .sibling import thing` names no distribution and must never be reported."""
    planted = tmp_path / "rel.py"
    planted.write_text(
        "from .sibling import thing\nfrom ..pkg import other\n", encoding="utf-8"
    )
    assert scan([planted]) == {}


def test_the_stdlib_is_not_reported(tmp_path: Path) -> None:
    planted = tmp_path / "std.py"
    planted.write_text("import json, sqlite3\nfrom pathlib import Path\n", encoding="utf-8")
    assert scan([planted]) == {}


def test_first_party_is_not_reported(tmp_path: Path) -> None:
    planted = tmp_path / "fp.py"
    planted.write_text(
        "import medos.core\nfrom services.catalogue import CATALOGUE\n", encoding="utf-8"
    )
    assert scan([planted]) == {}


def test_the_scanned_roots_all_exist() -> None:
    """A typo in SCANNED would silently scan nothing and pass everything."""
    for root in SCANNED:
        assert (REPO / root).is_dir(), f"{root} is in SCANNED but is not a directory"
    assert _sources(*SCANNED), "SCANNED matched no Python files at all"
