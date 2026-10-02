# SPDX-License-Identifier: Apache-2.0
"""`pytest` -- the bare command -- must collect this repository's tests.

WHAT WAS WRONG, AND HOW LONG IT WENT UNNOTICED
-----------------------------------------------
`tests/README.md` opens with `pytest`. That command did not work:

    ERROR tests/unit/test_migration_planes.py
    !!!!!!!! Interrupted: 1 error during collection !!!!!!!!
    2520 tests collected, 1 error

pytest imports a test module under a name it derives from the file's PATH -- but only as
far up as `__init__.py` files go. With none under `tests/`, the walk stopped at the file
itself and the name was the BASENAME: `test_migration_planes`. Two directories held a file
by that name, `tests/unit/` and `tests/integration/`, so the second one collected was the
first one again under a different `__file__`, which pytest refuses.

The suite was green throughout. CI and every contributor ran directories one at a time --
`pytest tests/unit`, `pytest tests/integration --require-stack` -- and each of those
commands collects one of the two files and never meets the other. What was red was the
entry point, and the ten tests in `tests/integration/test_migration_planes.py` were not
in the count anybody quoted.

WHY MARKERS AND NOT RENAMES
---------------------------
pytest's own hint is `use a unique basename for your test file modules`. That is the wrong
half of the choice. `test_migration_planes.py` in `tests/unit/` reads the migration files;
`test_migration_planes.py` in `tests/integration/` runs them against a real schema. Same
subject, two ways -- which is what a directory split is FOR. Renaming one of them would put
a tool's limitation in a test's name, and would fix this one collision while leaving the
next one to be discovered the same way.

THERE WAS A SECOND EFFECT, QUIETER. A name that is not a path is not unique, and the suite
imports across its own directories:

    tests/gate/test_capability_reachable.py:
        from tests.integration import test_viewer_capability_list as viewer_list
        from tests.gate.conftest import JOB_TABLES, Api, Ingested, why

`tests/gate/` DID carry an `__init__.py`, so pytest walked one level and stopped: it named
that conftest `gate.conftest` while the import asked for `tests.gate.conftest`. Measured on
a collect-only run of that single file, `sys.modules` held `tests/gate/conftest.py` under
BOTH names -- two module objects, two copies of every constant and every fixture function,
from one file. Nothing had failed because of it yet. That is not a guarantee; it is a
coincidence about which copy each reader happened to reach.

WHAT THIS TEST ASSERTS
----------------------
Not "the basenames are unique" -- they are allowed not to be, and that is the point. This
computes the module name pytest ITSELF will derive, by pytest's rule, and requires it to be
the file's path: `tests.unit.test_suite_layout`, never `test_suite_layout`. A missing
`__init__.py` on the way up from any test module -- including one in a directory added next
year -- breaks that equality and fails here, with the directory named.

Its reach stops where the collision does. A directory holding no `test_*.py` is invisible
to this test, because such a directory names no module and can collide with nothing:
`tests/js/` held only `__init__.py` and a node suite outside pytest's collection, and
removing ITS marker today left both tests green -- which was measured rather than
assumed, and is the shape of this entry's absence now that the whole directory is gone
with the training console it tested (register entry 150). The marker was there for the
day a test module joined it, and this docstring is the record of both the day and the
deletion.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent

#: Directories holding `test_*.py` that `pytest` is NOT expected to collect, each with the
#: reason it is excluded. An entry here is a deliberate exemption; anything else that holds
#: a test module and is outside `testpaths` is the defect the third test below catches.
#: EMPTY, and that is the repair. Its one entry was `spikes/week0`, whose
#: `test_contracts.py` held eight of MOS-IMG-010's eleven rejection codes and ran as a
#: script CI invoked by path -- outside pytest, outside coverage, and outside the reach of
#: a collection error. This map is what let that be a considered decision rather than an
#: oversight, and it is also what a considered decision looks like right up until the
#: directory is deleted and nobody notices what left with it. The checks are
#: `tests/unit/test_geometry_contract.py` now.
NOT_COLLECTED: dict[str, str] = {}


def _pytest_module_name(path: Path) -> str:
    """The name pytest gives this file, by pytest's own rule (`rootdir`-independent).

    `_pytest.pathlib.resolve_pkg_root_and_module_name`: walk up from the file while each
    directory holds an `__init__.py`; the first directory that does NOT is the package
    root, and the module name is the remaining path, dotted.
    """
    parts = [path.stem]
    parent = path.parent
    while (parent / "__init__.py").is_file():
        parts.append(parent.name)
        parent = parent.parent
    return ".".join(reversed(parts))


def _dotted_path(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts)


def _testpaths() -> list[Path]:
    """The directories a bare `pytest` collects, read from `pyproject.toml`.

    Read rather than spelled, because a gate holding its own copy of the list is a gate
    that keeps agreeing with itself after the list changes.
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    return [ROOT / p for p in config["tool"]["pytest"]["ini_options"]["testpaths"]]


def _test_modules() -> list[Path]:
    """Every test module a bare `pytest` collects -- across ALL of `testpaths`.

    This used to glob `tests/` alone. When the viewer's suite moved to `viewer/tests/`,
    that made its five modules -- 214 tests -- invisible to the two checks below rather than
    failing either of them, which is the same shape of defect they exist to catch.
    """
    return sorted(
        p
        for root in _testpaths()
        for p in root.rglob("test_*.py")
        if "__pycache__" not in p.parts
    )


def _package_modules() -> list[Path]:
    """The modules of the PLATFORM's suite, which is a package and imports across itself.

    `viewer/tests/` is deliberately NOT one. Making it a package would name its modules
    `tests.test_fusion` -- a second package called `tests` on the same path as the
    platform's, resolved by whichever is found first -- and making `viewer/` a package
    would declare a JavaScript tree importable Python. The viewer's suite imports nothing
    and is imported by nothing, so the rule below has no work to do there; the collision
    check that follows covers it and covers it globally.
    """
    return [p for p in _test_modules() if p.is_relative_to(TESTS)]


def test_every_test_module_is_named_after_its_path_and_not_its_basename() -> None:
    modules = _package_modules()
    assert len(modules) > 50, f"only {len(modules)} test modules found; the glob is wrong"

    mismatched: dict[str, tuple[str, str]] = {}
    for path in modules:
        derived, wanted = _pytest_module_name(path), _dotted_path(path)
        if derived != wanted:
            mismatched[str(path.relative_to(ROOT)).replace("\\", "/")] = (derived, wanted)

    if mismatched:
        missing = sorted(
            {
                str(d.relative_to(ROOT)).replace("\\", "/") + "/__init__.py"
                for name in mismatched
                for d in [(ROOT / name).parent]
                if not (d / "__init__.py").is_file()
            }
            | ({"tests/__init__.py"} if not (TESTS / "__init__.py").is_file() else set())
        )
        detail = "\n".join(
            f"    {name}\n        pytest imports it as {derived!r}, not {wanted!r}"
            for name, (derived, wanted) in sorted(mismatched.items())[:8]
        )
        raise AssertionError(
            f"{len(mismatched)} test module(s) are named after their basename rather than "
            f"their path:\n{detail}\n"
            f"  ADD: {', '.join(missing) or '(a marker was removed higher up)'}\n"
            "  Two files sharing a basename then collide and `pytest` stops at collection "
            "before running a test; and a cross-directory `from tests.x import y` imports "
            "a SECOND copy of a module pytest already loaded under a shorter name."
        )


def test_no_two_collected_modules_answer_to_one_name() -> None:
    """The collision itself, stated directly rather than through its cause.

    The test above is the invariant that PREVENTS this; this one is the symptom, so a
    future pytest whose naming rule differs from the one reimplemented above still cannot
    let the interrupted-collection error back in unnoticed.
    """
    seen: dict[str, list[str]] = {}
    for path in _test_modules():
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        seen.setdefault(_pytest_module_name(path), []).append(rel)

    clashes = {name: files for name, files in seen.items() if len(files) > 1}
    assert not clashes, (
        "two test files import under one module name, which is a collection ERROR -- "
        "`pytest` reports `Interrupted: N errors during collection` and runs nothing:\n"
        + "\n".join(
            f"    {name}: {' and '.join(files)}" for name, files in sorted(clashes.items())
        )
    )


def test_no_suite_in_this_repository_sits_outside_testpaths() -> None:
    """A suite that leaves `testpaths` does not go red. It stops being counted.

    MEASURED, in the change that made this test necessary. Moving the viewer out of
    `web/viewer/` carried its 214 tests to `viewer/tests/`, which `testpaths = ["tests"]`
    does not name. A bare `pytest` went from 2583 collected to 2369 and reported a clean
    green run, because every remaining test still passed. Nothing was broken and nothing
    was failing; two hundred and eleven checks had simply stopped being asked, and the
    only visible trace was a number nobody compares between runs.

    That is the same failure as the control-byte scan that looped over roots which no
    longer existed and reported a pass, one level up: a check addressed by path, pointed
    at nothing, silent about it.
    """
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
            "site-packages", ".tox", "build", "dist", "htmlcov", ".mypy_cache",
            "medos.egg-info"}
    covered = _testpaths()
    exempt = {ROOT / p for p in NOT_COLLECTED}

    stranded: dict[str, int] = {}
    for path in ROOT.rglob("test_*.py"):
        if skip & set(path.parts):
            continue
        if any(path.is_relative_to(root) for root in covered):
            continue
        if any(path.is_relative_to(root) for root in exempt):
            continue
        rel = str(path.parent.relative_to(ROOT)).replace("\\", "/")
        stranded[rel] = stranded.get(rel, 0) + 1

    assert not stranded, (
        "test modules live outside `testpaths`, so `pytest` with no arguments does not "
        "run them and their absence looks like a shorter green run:\n"
        + "\n".join(f"    {d}/  ({n} module(s))" for d, n in sorted(stranded.items()))
        + "\n  ADD the directory to `testpaths` in pyproject.toml, or -- if it is "
        "deliberately not collected -- to NOT_COLLECTED in this file WITH THE REASON. "
        "An exemption that has to be written down is one somebody has to mean."
    )


def test_ci_actually_runs_every_suite_testpaths_names() -> None:
    """`testpaths` is what a BARE `pytest` collects. CI never runs a bare `pytest`.

    Every job in `.github/workflows/tests.yml` passes explicit paths, and an explicit path
    OVERRIDES `testpaths` completely. So the entry above, and the check above it, were
    protecting a mechanism the only automated consumer does not use.

    MEASURED when this was written:

        pytest tests/unit          1158 collected   <- what CI ran
        pytest                     2590 collected   <- what testpaths names

    `viewer/tests` and `trainer/tests` had just been added to `testpaths`, a gate had been
    written to keep anything from leaving it, and 268 tests ran in NO CI JOB AT ALL. The
    gate asked whether the suites were listed. Nobody asked whether the list was read.

    This asks the second question: the union of the paths CI hands to pytest must cover
    every entry in `testpaths`. It does not care which job covers which -- a suite run
    only in the nightly is a decision somebody can defend; a suite run nowhere is not.
    """
    import re

    workflow = ROOT / ".github" / "workflows" / "tests.yml"
    assert workflow.is_file(), f"{workflow} is gone; this check has lost its subject"
    text = workflow.read_text(encoding="utf-8")

    #: Every `pytest ...` command line in a `run:` step, with its path arguments. A line
    #: inside a comment is not a command, and several of this file's comments quote one.
    invocations: list[list[str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        match = re.search(r"(?:run:\s*|^\s*-\s+)pytest\s+(.*)$", stripped)
        if not match:
            continue
        args = [
            a for a in match.group(1).split()
            if not a.startswith("-") and not a.startswith("$")
        ]
        # `-m gate_0_1_0` puts the marker in the argument list; drop anything that is not
        # a path that exists.
        invocations.append([a for a in args if (ROOT / a).exists()])

    assert invocations, (
        "no `pytest` invocation was found in the workflow, so this check would pass over "
        "nothing. Either the file changed shape or the parser did"
    )

    covered: list[Path] = [ROOT / a for inv in invocations for a in inv]
    uncovered = [
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in _testpaths()
        if not any(p == c or p.is_relative_to(c) for c in covered)
    ]

    assert not uncovered, (
        "these are in `testpaths` and no CI job runs them:\n"
        + "\n".join(f"    {d}" for d in uncovered)
        + "\n  An explicit path argument overrides `testpaths` entirely, so adding a suite "
        "to that list does NOT put it in CI. What CI runs:\n"
        + "\n".join(f"    pytest {' '.join(inv)}" for inv in invocations if inv)
    )


# ======================================================================================
# `tests/_support/` is an importable package, so every module in it must import
# ======================================================================================
#: MEASURED: `tests/_support/` carries an `__init__.py`, so `import tests._support.<name>`
#: is a reachable spelling — `tests/gate/test_capability_reachable.py` uses exactly that
#: form for its siblings. One module could not be imported:
#:
#:     >>> import tests._support.polarity_probe
#:     IndexError: list index out of range
#:
#: It did all its work at module scope and read `sys.argv[1]` on the way out, so importing
#: it ran it and then died. Its nine sibling probes guard with
#: `if __name__ == "__main__": main(sys.argv[1])` and all nine import cleanly; it is the
#: tenth now. Nothing had broken, because nothing imported it — which is also why its own
#: docstring's claim to be "the first brick of the owed harness" could not have been built
#: on: the brick would not load.
#:
#: WHY THIS IS A LAYOUT CHECK AND NOT A STYLE ONE. A module that executes on import inside a
#: package pytest imports is a collection error waiting for the first `from tests._support
#: import …` that reaches it, and the suite that discovers it will report a failure in
#: whatever test wrote that import rather than in the file at fault. Same class as the
#: basename collision at the top of this file: green everywhere until one command meets it.
SUPPORT = TESTS / "_support"


def _support_modules() -> list[Path]:
    return sorted(
        p for p in SUPPORT.glob("*.py")
        if p.name != "__init__.py"
    )


def test_every_support_module_can_be_imported() -> None:
    import importlib
    import sys as _sys

    modules = _support_modules()
    assert len(modules) >= 10, (
        f"only {len(modules)} module(s) found under {SUPPORT.name}/; the glob is broken"
    )
    broken: list[str] = []
    saved = _sys.argv
    try:
        # argv with no [1], which is what a test process looks like to a module that reads
        # it at import time. `pytest` itself is argv[0] plus this repository's own flags, so
        # a module reading argv[1] under pytest gets a PATH, not a directory it may write
        # to — either way, reading argv at import time is the defect.
        _sys.argv = ["import-check"]
        for path in modules:
            name = f"tests._support.{path.stem}"
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001 — the failure is the finding
                broken.append(f"{path.name}: {type(exc).__name__}: {exc}")
    finally:
        _sys.argv = saved
    assert not broken, (
        f"{len(broken)} module(s) under tests/_support/ cannot be imported:\n  "
        + "\n  ".join(broken)
        + "\n\n  A module in an importable package that runs on import is a collection "
        "error waiting for the first import that reaches it. Give it the "
        "`build()` / `main(out_dir)` / `if __name__ == \"__main__\"` shape its siblings "
        "have."
    )


def test_every_runnable_support_script_guards_its_entry_point() -> None:
    """The property behind the import check, asserted at its site rather than by outcome.

    A module that takes a command line must say so with `if __name__ == "__main__"`. The
    import check above would pass for a module that read `sys.argv[1]` and happened not to
    crash; this one asks for the guard.
    """
    missing: list[str] = []
    for path in _support_modules():
        text = path.read_bytes().decode("utf-8")
        if "sys.argv" not in text:
            continue
        if '__name__ == "__main__"' not in text:
            missing.append(path.name)
    assert not missing, (
        f"{missing} read `sys.argv` with no `__main__` guard, so importing one runs it"
    )
