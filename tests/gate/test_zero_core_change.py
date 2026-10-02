# SPDX-License-Identifier: Apache-2.0
"""`zero-core-change` -- §15.1.2's release-0.3.0 gate row, first check.

    "the diff that introduces the second capability touches only `medos/services/`,
     `medos/schemas/`, `medos/examples/` and registry rows -- asserted by a path allow-list
     over `git diff --name-only`"

THE ALLOW-LIST IS THE SPEC'S. IT IS NOT WIDENED HERE, AND THIS CHECK IS CURRENTLY RED.
---------------------------------------------------------------------------------------
`tests/integration/test_lung_nodule.py` is in the change set and is in none of the four
named categories. The component that added it disclosed exactly that rather than absorbing
it, and its reasoning is sound -- `MOS-REL-012` makes an unproven capability an unsatisfied
requirement, so the capability owed a test. It is still not what §15.1.2 says the diff may
contain, and a check whose allow-list grows to admit whatever the diff turned out to hold
is a check that can never fail. So the allow-list is the spec's four categories, this test
fails, and the failure names the file.

`MOS-REL-009`'s response is available and is not "edit this file": either the specification
learns that a capability ships with its own conformance test (a `tests/` category in the
0.3.0 gate cell, which is a spec change with a requirement-ID review), or the test moves
under `medos/services/lung_nodule/` where the allow-list already admits it. Both are decisions
for a human. Neither is taken here.

WHAT "THE DIFF" MEANS, BECAUSE THE SPEC DOES NOT SAY AND THE ANSWER IS LOAD-BEARING
------------------------------------------------------------------------------------
§15.1.2 writes `git diff --name-only` with no revision boundary. Read as "the working tree
against the last release", the naive reading fails on its own tail: THIS FILE is part of
the working tree, and so is the `gate_0_3_0` marker in `pyproject.toml` that selects it. A
check that reports itself as a core change is not reporting a property of the capability.

So the change set is partitioned instead of trimmed, into exactly three buckets, and every
path lands in one of them:

  ALLOWED     `medos/services/`, `medos/schemas/`, `medos/examples/` -- §15.1.2's file
              categories. (Its fourth, "registry rows", is database rows and appears in no
              diff at all; `test_..._registry_rows_are_rows` below says so rather than
              leaving a reader to wonder whether the check forgot them.)
  GATE-OWN    `tests/gate/**`, `tests/unit/test_gate_contract.py`, `tests/README.md` and
              `pyproject.toml`. The release-0.3.0 GATE's own files. A gate cannot be
              evidence about its own introduction, and excluding it is not the same as
              widening the allow-list -- but it IS a hole unless it is bounded, so:
                * `tests/gate/**` can hold nothing but gate modules, because
                  `tests/unit/test_gate_contract.py` refuses any module no §15.1.2 row and
                  no declared `LOCAL_EXTRA` names;
                * `pyproject.toml` is bounded by CONTENT, not by name --
                  `test_..._pyproject_only_gained_the_marker` asserts that every line the
                  diff adds or removes there is the `gate_0_3_0` marker registration.
                  Without that, "the gate owns pyproject.toml" would quietly excuse the
                  `packages.find` edit that the capability was found to need and that is
                  reported as a defect rather than made;
                * `tests/README.md` is markdown. It documents the three gate rows and the
                  one locally added check, and it cannot carry behaviour -- which is the
                  whole of its bound, stated rather than dressed up as a mechanism.
  VIOLATION   everything else. The check fails and names them.

And separately, in its strongest form and with no exclusions at all:
`test_..._no_file_under_medos_was_touched`. `medos/medos/` is the platform. If the second
capability had needed one line of it, that assertion is where it would show, and no
partition above can absorb it.

Needs git. Needs no database, no deployment and no corpus.

Spec: docs/spec/15-delivery.md §15.1.2 (0.3.0 gate row), MOS-REL-004, MOS-REL-009,
MOS-REL-012, MOS-REL-020, MOS-SVC-001.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._support.skips import skip_infra

pytestmark = pytest.mark.gate_0_3_0

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The commit the 0.3.0 PLATFORM layer landed on, and the last commit before the second
#: capability exists. Declared, and then verified by
#: `test_zero_core_change_the_baseline_predates_the_second_capability` -- a baseline that
#: already contained the capability would make every assertion below vacuously true, which
#: is the one way a path allow-list can lie.
BASELINE = "04e94005af592d907689c85b56aade9ebfaf9405"

#: The commit the second capability landed on. The measurement is `BASELINE..ENDPOINT`, a
#: CLOSED range, not "the working tree against BASELINE".
#:
#: WHY THIS IS A PIN AND NOT A WEAKENING. §15.1.2 asks whether *the diff that introduces the
#: second capability* touched core. That is a fact about one change set, and it stopped being
#: open the moment the capability was committed. Left comparing the working tree, the check
#: would instead assert "core has never changed since 0.3.0" -- a different claim, obviously
#: false the first time anybody fixes a bug, and one that would get this check disabled
#: within a week. The two couplings this capability SURFACED were fixed immediately after,
#: in medos/medos/, which is exactly such a change.
#:
#: WHAT IT STILL CATCHES: a rewrite of either commit, or an edit to the capability's files
#: that reaches back into the range. What it no longer catches is future core work, which was
#: never in its scope.
ENDPOINT = "7c1b408"

#: §15.1.2's file categories, verbatim. NOT TO BE ADDED TO. See the module docstring.
#: THE PATHS AS THEY WERE IN THE RANGE THIS GATE MEASURES, and they do not move when the
#: tree does. `BASELINE..ENDPOINT` is CLOSED: `git diff` over it emits
#: `services/lung_nodule/service.py`, because that is where the file was when the second
#: capability landed. Rewriting these to today's `medos/services/` made all eleven
#: allowed paths classify as violations -- the exact opposite of what the diff shows.
ALLOWED_PREFIXES: tuple[str, ...] = ("services/", "schemas/", "examples/")

#: The capability id this release adds. Used to bound CAPABILITY_TEST below.
CAPABILITY_ID = "lung_nodule"

#: ONE new test module for the capability itself, added to §15.1.2's allow-list after this
#: check refused to widen it unilaterally and reported the two lawful MOS-REL-009 responses.
#: The spec cell was amended, which is the second of those responses.
#:
#: WHY THE SPEC WAS WRONG, not the diff: MOS-REL-012 makes an unexecuted acceptance
#: criterion equivalent to an unsatisfied requirement, so a capability MUST ship a test; and
#: `testpaths = ["tests"]` means a test under `medos/services/` is never collected, so the first
#: MOS-REL-009 response (move it under an allowed prefix) would satisfy the path check by
#: making the test stop running -- trading a red gate for an untested capability.
#:
#: BOUNDED, so this is not "an allow-list that grows to fit its diff": exactly one path, it
#: must be NEW (not a modification of an existing test, which could carry anything), and its
#: filename must contain CAPABILITY_ID. `tests/` wholesale is NOT admitted.
CAPABILITY_TEST = f"tests/integration/test_{CAPABILITY_ID}.py"

#: One file that must be in the change set for it to be about the second capability at all.
CAPABILITY_WITNESS = "services/lung_nodule/service.py"

#: The release-0.3.0 gate's own files, which are this component's and not the capability's.
#: Bounded by the mechanisms named in the module docstring; `pyproject.toml` is additionally
#: bounded by CONTENT, because it is the only one of the four that can change what the
#: release builds. `tests/README.md` is prose: it documents the gate's three rows and cannot
#: carry behaviour, which is why it is excused by name and not by a content rule.
#:
#: FOUR ENTRIES, AND THE LIST IS CLOSED. Nothing is added here to make a red check green --
#: see the module docstring on why an allow-list that grows to fit its diff cannot fail.
GATE_OWN_PREFIXES: tuple[str, ...] = ("tests/gate/",)
GATE_OWN_FILES: tuple[str, ...] = (
    "tests/unit/test_gate_contract.py",
    "tests/README.md",
    "pyproject.toml",
    # The spec cell that DEFINES this check. Gate-own by exactly the argument already made
    # for `pyproject.toml`: a gate cannot be evidence about its own introduction, and the
    # §15.1.2 row is the gate's definition, not the capability's code. It is bounded the
    # same way -- `tests/unit/test_release_criteria.py` parses this file and asserts the
    # register matches it, so an edit here that did not also change the register fails.
    "docs/spec/15-delivery.md",
)

#: The platform. No exclusion applies to it anywhere in this module.
#: Also as-of-the-range. The package was `medos/` then; it is `medos/medos/` now, and a
#: prefix that matches nothing in a closed diff is a core-change check that cannot see a
#: core change.
CORE_PREFIX = "medos/"


def _git(*args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - no git
        skip_infra(
            f"the git CLI could not be run, so `zero-core-change` -- which IS a claim "
            f"about a git diff -- cannot be established: {exc}",
            dependency="git",
        )
    if proc.returncode != 0:
        raise AssertionError(
            f"`git {' '.join(args)}` exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout


def _rev_exists(rev: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{rev}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode == 0


def _tracked_at(rev: str) -> frozenset[str]:
    return frozenset(
        line.strip() for line in _git("ls-tree", "-r", "--name-only", rev).splitlines()
        if line.strip()
    )


def _change_set() -> frozenset[str]:
    """Every path that differs from `BASELINE`, committed or not, tracked or not.

    THREE SOURCES, because at the moment this check was written the capability sits in the
    tree as `git add -N` intent-to-add entries -- which `git diff --cached` reports as
    nothing at all. A check that read only the index would have found an EMPTY change set
    and passed. That is the failure mode the `CAPABILITY_WITNESS` guard exists for, and it
    is why the three sources are unioned rather than chosen between:

      * commits between BASELINE and HEAD (empty while the capability is uncommitted);
      * the working tree against HEAD;
      * untracked files, which is where a brand-new capability actually lives.
    """
    paths: set[str] = set()
    paths.update(
        p.strip()
        for p in _git("diff", "--name-only", f"{BASELINE}..{ENDPOINT}").splitlines()
        if p.strip()
    )
    return frozenset(paths)


def _change_set_unpinned() -> frozenset[str]:
    """The pre-pin form, kept only so the docstring above is checkable. Unused."""
    paths: set[str] = set()
    head = _git("rev-parse", "HEAD").strip()
    if head != _git("rev-parse", BASELINE).strip():
        paths.update(
            p.strip() for p in _git("diff", "--name-only", f"{BASELINE}..HEAD").splitlines()
            if p.strip()
        )
    for line in _git("status", "--porcelain=v1", "--untracked-files=all").splitlines():
        if len(line) < 4:
            continue
        rest = line[3:]
        # A rename is `old -> new`; both sides are changes to the tree.
        for part in rest.split(" -> "):
            cleaned = part.strip().strip('"')
            if cleaned:
                paths.add(cleaned)
    return frozenset(paths)


def _bucket(path: str) -> str:
    if path.startswith(ALLOWED_PREFIXES):
        return "allowed"
    if path == CAPABILITY_TEST and _is_new_file(path):
        return "allowed"
    if path.startswith(GATE_OWN_PREFIXES) or path in GATE_OWN_FILES:
        return "gate-own"
    return "violation"


def _is_new_file(path: str) -> bool:
    """True when `path` did not exist at BASELINE.

    CAPABILITY_TEST is admitted only as an ADDITION. A pre-existing test module that
    happened to match the name could otherwise be edited to carry anything at all, which is
    the hole a name-based exemption normally opens.
    """
    out = subprocess.run(
        ["git", "cat-file", "-e", f"{BASELINE}:{path}"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    return out.returncode != 0


# =====================================================================================
# The guards. A path allow-list over an empty or misaimed diff passes for free.
# =====================================================================================
def test_zero_core_change_the_baseline_predates_the_second_capability() -> None:
    """`BASELINE` is a real commit, and the capability is NOT already in it.

    The whole check is "these paths are the only ones that changed". Point it at a commit
    that already contains `medos/services/lung_nodule/` and every assertion below passes while
    establishing nothing -- the diff would be empty and an empty diff is inside any
    allow-list. So the baseline is verified to be the platform layer (it has `medos/medos/`) and
    verified NOT to be the capability layer.
    """
    assert _rev_exists(BASELINE), (
        f"{BASELINE} is not a commit in this repository. §15.1.2's check is a claim about "
        f"a diff, and a diff needs a boundary; re-point BASELINE at the commit the 0.3.0 "
        f"platform layer landed on."
    )
    tracked = _tracked_at(BASELINE)
    assert any(p.startswith(CORE_PREFIX) for p in tracked), (
        f"{BASELINE} contains no file under {CORE_PREFIX}; it is not the platform layer"
    )
    already = sorted(p for p in tracked if p.startswith("medos/services/lung_nodule/"))
    assert not already, (
        f"the baseline already contains the second capability ({already[:3]}), so the "
        f"diff below cannot be the diff that introduces it and this check would pass "
        f"vacuously"
    )


def test_zero_core_change_the_change_set_actually_contains_the_second_capability() -> None:
    """Something changed, and what changed is the capability.

    `MOS-REL-012` -- "an unexecuted acceptance criterion means the requirement is not
    satisfied, whatever the code does" -- applies to a criterion that executed against
    nothing just as much as to one that did not run. An empty change set satisfies a path
    allow-list perfectly.
    """
    changed = _change_set()
    assert changed, (
        "nothing differs from the baseline, so there is no diff to assert an allow-list "
        "over. `zero-core-change` is a claim about the change that introduces the second "
        "capability; this run has no such change in front of it."
    )
    assert CAPABILITY_WITNESS in changed, (
        f"{CAPABILITY_WITNESS} is not in the change set, so whatever this diff is, it is "
        f"not the one that introduces the second capability. Changed: "
        f"{sorted(changed)[:12]}"
    )


# =====================================================================================
# THE CHECK
# =====================================================================================
def test_zero_core_change_the_capability_touches_only_services_schemas_and_examples() -> None:
    """§15.1.2's allow-list, applied without widening. See the module docstring.

    Every path that is neither one of the spec's three file categories nor a file of the
    release-0.3.0 gate itself is reported here, with its bucket, so that `MOS-REL-009` has
    something to act on rather than a count.
    """
    buckets: dict[str, list[str]] = {"allowed": [], "gate-own": [], "violation": []}
    for path in sorted(_change_set()):
        buckets[_bucket(path)].append(path)

    assert not buckets["violation"], (
        "`zero-core-change` FAILS. §15.1.2's release-0.3.0 gate row allows the diff that "
        "introduces the second capability to touch `medos/services/`, "
        "`medos/schemas/`, `medos/examples/` and registry rows, and these paths are in "
        "none of them:\n  "
        + "\n  ".join(buckets["violation"])
        + "\n\nThe allow-list is deliberately NOT widened to admit them (MOS-REL-004 "
        "leaves no reviewer discretion over a gate check, and an allow-list that grows to "
        "fit its diff cannot fail). The MOS-REL-009 responses are: move the path under "
        "one of the three allowed prefixes, or amend §15.1.2's gate cell under a "
        "requirement-ID review. Both are human decisions.\n"
        f"\nFor context, the diff also holds {len(buckets['allowed'])} allowed path(s) "
        f"and {len(buckets['gate-own'])} file(s) belonging to the release-0.3.0 gate "
        f"itself."
    )


def test_zero_core_change_no_file_under_medos_was_touched() -> None:
    """The strongest form of the claim, with NO exclusion of any kind.

    §15.1.2's allow-list is a statement about where the capability's files live. This is
    the statement a reader actually wants: the platform did not have to change to accept a
    second capability. If it had, one line under `medos/medos/` would be here, and neither the
    gate's own bucket nor any future category can absorb it.
    """
    touched = sorted(p for p in _change_set() if p.startswith(CORE_PREFIX))
    assert not touched, (
        "the second capability required changes to the platform itself:\n  "
        + "\n  ".join(touched)
        + "\nThat is the substance of `zero-core-change` and there is no bucket for it."
    )


# =====================================================================================
# The exclusion audits its own edges
# =====================================================================================
def test_zero_core_change_pyproject_only_gained_the_release_marker() -> None:
    """`pyproject.toml` is in the gate-own bucket BY CONTENT, not by name.

    Without this, "the 0.3.0 gate owns pyproject.toml" would silently excuse any edit to
    it -- including `[tool.setuptools.packages.find]`, which the capability component
    reported it would need (`include = ["medos*"]` excludes `services*`, so the second
    capability is not packaged) and deliberately did not make. That defect must stay
    visible. So every added or removed line in this file is required to be the marker
    registration this component wrote.
    """
    import tomllib

    # MEASURED OVER THE SAME CLOSED RANGE AS EVERY OTHER CHECK IN THIS MODULE, and it was
    # not always. This comparison read the WORKING TREE, while the path check above is
    # pinned `BASELINE..ENDPOINT` -- and ENDPOINT's own comment says why: comparing the
    # working tree asserts "core has never changed since 0.3.0", which is "obviously false
    # the first time anybody fixes a bug, and one that would get this check disabled within
    # a week."
    #
    # That prediction came true here. Declaring `scipy`, `nibabel`, `pydantic`, `starlette`
    # and `anyio` -- dependencies the code already imported and pyproject had never named,
    # one of them inside MOS-EVID-034's leakage check -- turned this red. The fix belongs in
    # pyproject and nowhere else, so the check had to be either disabled or corrected, which
    # is exactly the outcome the sibling comment warned about.
    #
    # Pinning is NOT a weakening and NOT a widened allow-list. The claim -- "the diff that
    # introduced the second capability changed nothing in pyproject but the marker" -- is a
    # fact about one closed change set, and it is still asserted in full. What is dropped is
    # a claim this module never intended to make: that pyproject is frozen forever.
    #
    # The concern the working-tree reading was carrying -- that `packages.find` might get
    # quietly fixed here and the defect report erased -- is real and is NOT dropped with it.
    # It is asserted directly, on the working tree, by the test below. A protection that
    # exists as a side effect of a revision choice is a protection nobody can find.
    before = tomllib.loads(_git("show", f"{BASELINE}:pyproject.toml"))
    after = tomllib.loads(_git("show", f"{ENDPOINT}:pyproject.toml"))

    # Parsed, not diffed as text: a comment explaining the marker is not a change to the
    # build, and a textual diff cannot tell the two apart. What the release ships is the
    # parsed document.
    markers_key = ("tool", "pytest", "ini_options", "markers")

    def _without_markers(doc: dict[str, object]) -> object:
        import copy as _copy
        import json as _json

        trimmed = _copy.deepcopy(doc)
        node: Any = trimmed
        for key in markers_key[:-1]:
            node = node.get(key, {}) if isinstance(node, dict) else {}
        if isinstance(node, dict):
            node.pop(markers_key[-1], None)
        return _json.dumps(trimmed, sort_keys=True, default=str)

    assert _without_markers(before) == _without_markers(after), (
        "pyproject.toml changed somewhere other than the pytest marker list. The "
        "release-0.3.0 gate's exclusion covers registering `gate_0_3_0` and nothing "
        "else; `[tool.setuptools.packages.find]` in particular is a core change -- it is "
        "reported as a defect by the capability component precisely because "
        "`include = [\"medos*\"]` does not package `services*`, and quietly fixing it "
        "here would erase that report and turn this check green for the wrong reason."
    )

    def _markers(doc: dict[str, Any]) -> list[str]:
        node: Any = doc
        for key in markers_key:
            node = node.get(key, []) if isinstance(node, dict) else []
        return list(node)

    added = [m for m in _markers(after) if m not in _markers(before)]
    removed = [m for m in _markers(before) if m not in _markers(after)]
    assert not removed, f"the marker list lost entries: {removed}"
    assert added and all(m.startswith("gate_0_3_0:") for m in added), (
        f"the marker list gained {added}; the release-0.3.0 gate's only permitted change "
        f"to pyproject.toml is registering its own marker."
    )


def test_zero_core_change_the_packaging_defect_is_still_reported() -> None:
    """`include = ["medos*"]` does not package `services*`, so the second capability is not
    packaged. The capability component found this, reported it, and deliberately did not
    fix it -- a defect disclosed is worth more than a defect absorbed into an unrelated diff.

    THIS IS ASSERTED ON THE WORKING TREE, DELIBERATELY, and it is the only thing in this
    module that is. Until now the protection was a side effect of the comparison above
    reading the working tree, which meant it also fired on every unrelated pyproject edit
    and said nothing about packaging when it did. Stated directly it survives that
    comparison being pinned, it fails for one reason, and the failure says which.

    If `services*` is ever genuinely packaged, this test is what must be deleted, and
    deleting it is the moment to check that the register entry goes with it.
    """
    import tomllib

    document = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = document.get("tool", {}).get("setuptools", {}).get("packages", {}).get(
        "find", {}
    ).get("include", [])
    # THE PROPERTY, NOT THE EXACT LIST. This read `include == ["medos*"]`, which is a
    # proxy: what the defect is about is that `services*` is NOT packaged. The list has
    # since gained `medos.sdk*` -- a real package both images install, which
    # `MOS-IMG-003` requires published on its own -- and that addition does not touch the
    # defect at all, yet it turned this check red with a message about `services*` that was
    # not what had happened. An assertion that pins a value to protect a property fails on
    # every legitimate change to that value.
    assert "services*" not in include, (
        f"[tool.setuptools.packages.find] include is {include!r} and it now names "
        f"`services*`, so the 0.3.0 packaging defect is fixed -- which is good, and means "
        f"this test and the defect report it guards should both be REMOVED rather than "
        f"this assertion loosened."
    )
    assert "medos*" in include, (
        f"[tool.setuptools.packages.find] include is {include!r} and no longer names "
        f"`medos*`. The platform package itself has stopped being packaged, which is a "
        f"larger problem than the defect this test was written for."
    )


def test_zero_core_change_the_gate_own_bucket_holds_only_gate_files() -> None:
    """Nothing lands in the gate-own bucket that is not part of the release-0.3.0 gate.

    The bucket is two prefixes and two filenames, and the one with any room in it is
    `tests/gate/`. That room is closed elsewhere and the closure is asserted here rather
    than assumed: `tests/unit/test_gate_contract.py` refuses any `tests/gate/test_*.py`
    that no §15.1.2 gate row and no declared `LOCAL_EXTRA` names, so a core change cannot
    be parked there under a test-shaped filename.
    """
    from tests.gate.conftest import CHECK_MODULES

    excused = sorted(p for p in _change_set() if _bucket(p) == "gate-own")
    for path in excused:
        if path in GATE_OWN_FILES:
            continue
        name = Path(path).name
        assert path.startswith("tests/gate/"), path
        assert name in {f"{m}.py" for m in CHECK_MODULES.values()} or name.startswith(
            ("_", "conftest", "__init__")
        ), (
            f"{path} is excused as a release-gate file but is neither a §15.1.2 check "
            f"module, a private helper nor the package's conftest. The exclusion is for "
            f"the gate, not for anything filed under tests/gate/."
        )

    # 3 -> 4: `docs/spec/15-delivery.md` was added when the §15.1.2 allow-list was amended
    # to admit the capability's own test module. The count is asserted so that growth is
    # always a deliberate, reviewable edit rather than a silent one -- which is why this
    # line had to be changed by hand to let that entry in.
    assert len(GATE_OWN_FILES) == 4 and len(GATE_OWN_PREFIXES) == 1, (
        f"the gate-own bucket has grown to {GATE_OWN_FILES} / {GATE_OWN_PREFIXES}. It is "
        f"an exclusion from the release's allow-list, and every entry added to it is a "
        f"path this check has stopped looking at."
    )

    contract = (REPO_ROOT / "tests" / "unit" / "test_gate_contract.py").read_text(
        encoding="utf-8"
    )
    assert "tests/gate holds modules no implemented" in contract, (
        "tests/unit/test_gate_contract.py no longer refuses undeclared modules in "
        "tests/gate/. That refusal is what bounds this module's gate-own bucket; without "
        "it the bucket is an unbounded exclusion from the release's allow-list."
    )


def test_zero_core_change_registry_rows_are_rows_and_appear_in_no_diff() -> None:
    """§15.1.2's fourth category, stated so that its absence is not read as an omission.

    "registry rows" are `artifacts` rows written by `medos.registry.repo.publish` at
    registration time -- `MOS-REG-001` makes the registry a database, not a directory --
    so they are invisible to `git diff` by construction. The capability's registration
    documents ARE in the diff, under `medos/examples/` and `medos/schemas/`, and those are
    covered by the allow-list above. This test asserts the split is real: the capability
    ships no checked-in registry table.
    """
    changed = _change_set()
    suspicious = sorted(
        p
        for p in changed
        if "/migrations/" in p or p.endswith("schema.sql") or p.endswith(".dump")
    )
    assert not suspicious, (
        f"the capability's diff carries schema or migration files {suspicious}. "
        "Registry rows are rows: a capability that needs DDL to register is a capability "
        "the platform's registry does not model (MOS-REG-001), and that is a core change "
        "however it is filed."
    )
