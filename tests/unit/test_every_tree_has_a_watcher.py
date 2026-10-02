# SPDX-License-Identifier: Apache-2.0
"""Every tracked tree is read by at least one check, and the check is named here.

FOUR ROUNDS RUNNING, THE DEFECT HAS BEEN IN WHAT A CHECK READS RATHER THAN WHAT IT ASSERTS:

    entry 123  CI ran `pytest tests/unit viewer/tests trainer/tests` and linted only the
               first. `viewer` measured 36 findings and `trainer` 17 -- 217 tests of
               executable architecture rules and a whole product's suite, checked by nothing.
    entry 129  two gates took a hand-written list of sites, so eight stale sites of two
               claims existed and only three were inside a gate.
    entry 132  `test_tools_are_documented.py` walked `medos/tools/`, because that is where
               the tools were when it was written; ten hand-run probes in `tests/_support/`
               were outside it and no document named one of them.
    entry 134  a check read `git ls-files` and could not see itself until it was tracked --
               it passed, was committed, and the commit turned it red.
    entry 135  the same wrong population in seven more sites, including the SPDX licence
               check.

Every one of those is a root or a population chosen once, correct then, narrower than its
subject later. The general property -- "this check's root still contains its subject" -- is
not decidable, and a test that claimed to decide it would be the worst kind of reassurance.
One specific version IS decidable and is what this module asserts: **no tracked top-level
tree is read by nothing.** That is the shape entry 123 was, one level up, and it is the
question nobody was asking when `viewer/` and `trainer/` became products.

WHAT THIS IS NOT. It does not say a tree is adequately checked, or that the checks reading
it assert anything useful about it. It says a tree is not invisible. A tree with one watcher
that greps it for a single string passes, and should -- judging sufficiency is a reviewer's
job, and a check that pretended to do it would let the table below be read as a coverage
claim.

Register entry 136.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: tracked top-level tree -> checks that READ it, at least one each. Written out rather
#: than discovered, because "reads" cannot be decided by a regex over source: a module may
#: name a tree in a docstring while walking another, which is exactly how the roots in
#: entries 132 and 135 looked right. Each entry is verified three ways below -- the tree
#: exists, the module exists, and the module's SOURCE names the tree.
WATCHED_BY: dict[str, tuple[str, ...]] = {
    ".github": (
        "tests/unit/test_suite_layout.py",
        "tests/unit/test_testing_chapter_is_unbuilt.py",
    ),
    "docs": (
        "tests/unit/test_compose_profiles.py",
        "tests/unit/test_spec_navigation.py",
        "tests/unit/test_release_records.py",
    ),
    "medos": (
        "tests/unit/test_packaging_surface.py",
        "tests/unit/test_compose_profiles.py",
        # The SDK subtree (`medos/medos/sdk/`) used to be a top-level product tree,
        # `medicalos_preprocessing/`; it moved inside the distribution when the platform
        # became the SDK, and its watchers moved with it.
        "tests/unit/test_shared_package_is_pure.py",
        "tests/unit/test_product_readmes.py",
    ),
    "tests": (
        "tests/unit/test_suite_layout.py",
        "tests/unit/test_gate_contract.py",
    ),
}


def _tracked() -> list[str]:
    # Untracked files included, for the reason register entry 135 records: a tree added in
    # the change being reviewed is the tree most likely to have no watcher.
    return subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()


def _top_level_trees() -> set[str]:
    return {p.split("/")[0] for p in _tracked() if "/" in p}


TREES = sorted(_top_level_trees())


def test_the_table_names_exactly_the_trees_that_exist() -> None:
    """A new product tree with no watcher fails here, which is the whole point."""
    listed = sorted(WATCHED_BY)
    assert listed == TREES, (
        f"WATCHED_BY names {listed}; the repository has {TREES}.\n"
        f"  unwatched: {sorted(set(TREES) - set(listed))}\n"
        f"  gone     : {sorted(set(listed) - set(TREES))}\n"
        "  A tree nothing reads is the shape of register entry 123: viewer/ and trainer/ "
        "were tested by CI and linted by nothing for as long as nobody asked this."
    )


@pytest.mark.parametrize("tree", sorted(WATCHED_BY))
def test_every_named_watcher_exists(tree: str) -> None:
    missing = [m for m in WATCHED_BY[tree] if not (ROOT / m).exists()]
    assert not missing, f"{tree} names watcher(s) that do not exist: {missing}"


@pytest.mark.parametrize("tree", sorted(WATCHED_BY))
def test_every_named_watcher_really_reads_that_tree(tree: str) -> None:
    """Named is not enough: the module's source must mention the tree.

    This is the weakest honest form. It cannot tell a walk from a docstring mention, and
    saying so is the only way to have the check at all -- entries 132 and 135 are both cases
    where a root LOOKED right. What it does catch is a table entry that has drifted from the
    module it names, which is how a frozen list rots.
    """
    blind = []
    for module in WATCHED_BY[tree]:
        source = (ROOT / module).read_bytes().decode("utf-8", "replace")
        if not re.search(rf"[\"'/]{re.escape(tree)}[\"'/]", source):
            blind.append(module)
    assert not blind, (
        f"{blind} are listed as reading {tree!r} and their source never names it"
    )


# A FOURTH ASSERTION WAS WRITTEN HERE AND DELETED, AND THE REASON BELONGS IN THE FILE.
#
# It was to have said: a tree needs at least one watcher whose SUBJECT is that tree, because
# a module that reads every tree establishes nothing about any single one -- and a table
# satisfied by repository-wide checks alone would be a coverage claim with nothing behind it.
# The implementation counted how many trees a module names and called a module "universal"
# when it named all of them.
#
# MEASURED, THAT COUNT CANNOT FIRE. No module in the suite names all seven trees; the most
# is six, `tests/unit/test_product_readmes.py`, whose subject genuinely IS the four product
# READMEs and which therefore legitimately spans most of the repository. So the assertion
# passed and could not be shown red against any defect, which this project treats as an
# assertion that proves nothing (see `tests/README.md` and the proof-by-breaking discipline
# the register records from entry 116 on).
#
# Lowering the threshold to six would have made that module "universal" and the check green
# for the wrong reason. The property I wanted is a judgement about a module's SUBJECT, and no
# count of the paths it mentions distinguishes a repository-wide check from a narrow one that
# happens to cite several trees. That limit is real, so it is recorded here rather than
# papered over with a number chosen because it passes.


# ======================================================================================
# Why the platform's suite sits at the repository root
# ======================================================================================
#: `medos/README.md` argues that `tests/unit/` belongs at the root rather than inside
#: `medos/medos/`, and its evidence was a tally: "of the 38 modules in `tests/unit/`, 11 read
#: only `medos/medos/`. Twenty-seven read something else".
#:
#: MEASURED: the directory holds 54 modules -- 13 platform-only, 39 reading something else,
#: and 2 reading neither. The old sentence was wrong by sixteen AND had no third bucket,
#: while 11 + 27 = 38 kept its own arithmetic consistent. **A self-consistent sentence does
#: not read wrong**, which is why it survived a session in which eight modules were added to
#: that very directory -- by me.
#:
#: So the tally is a dated snapshot in the README now, and what is asserted here is the
#: PROPERTY the argument rests on: most of these modules read more than the platform package.
#: That does not drift when somebody adds a test, and it is the whole reason the suite cannot
#: live inside the package it tests.
UNIT = TESTS_DIR = ROOT / "tests" / "unit"

#: Things a unit test reaches for that are NOT the platform package.
_BEYOND_THE_PACKAGE = (
    r"docker-compose\.yml", r"medos/deploy/", r"docs/spec/", r"medos/web/",
    r"\btrainer/", r"\bviewer/", r"nginx\.conf\.template",
    r"99-known-inconsistencies", r"docs/releases/",
)
_PLATFORM = r"medos/medos|\bmedos\.[a-z]|from medos\b|import medos\b"


def _unit_modules() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         "tests/unit/test_*.py"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()
    return [ROOT / p for p in out]


def test_most_platform_unit_tests_read_more_than_the_platform_package() -> None:
    modules = _unit_modules()
    assert len(modules) >= 30, (
        f"only {len(modules)} module(s) found under tests/unit/; the glob is broken"
    )
    beyond = 0
    package_only = 0
    for path in modules:
        text = path.read_bytes().decode("utf-8", "replace")
        if any(re.search(p, text) for p in _BEYOND_THE_PACKAGE):
            beyond += 1
        elif re.search(_PLATFORM, text):
            package_only += 1
    assert beyond > package_only, (
        f"{beyond} of {len(modules)} unit modules read something outside "
        f"`medos/medos/` and {package_only} read only the package. medos/README.md argues "
        "from the first number being the larger one that the suite belongs at the "
        "repository root; if that has reversed, the argument has to be rewritten rather "
        "than this check relaxed."
    )


def test_the_readme_dates_the_tally_it_states() -> None:
    """A snapshot must say when it was taken, or it reads as a claim about today.

    The sentence this replaces stated 38/11/27 with no date, so there was nothing to tell a
    reader it had aged. `docs/releases/README.md` makes the same distinction for the release
    records: a number with a date is evidence, a number without one is a promise.
    """
    prose = " ".join(
        (ROOT / "medos" / "README.md").read_bytes().decode("utf-8").split()
    )
    tally = re.search(r"of (\d+) modules in `tests/unit/`", prose)
    assert tally, "medos/README.md no longer states the tally this check watches"
    window = prose[max(0, tally.start() - 200):tally.end() + 60]
    assert re.search(r"Measured on \d{4}-\d{2}-\d{2}", window), (
        "medos/README.md states a module tally for tests/unit/ with no measurement date "
        "beside it. The count drifts every time a test is added -- eight were added to that "
        "directory in the session that found the last figure stale by sixteen -- so the "
        "date is what keeps it a snapshot instead of a wrong claim about now."
    )


def test_the_readme_states_the_platform_only_count_once_or_consistently() -> None:
    """One document, one number for one set.

    MEASURED: two rounds ago I corrected `medos/README.md`'s tally to "13 read only
    `medos/medos/`" and left the argument two paragraphs below saying "Moving **eleven**
    files into `medos/medos/tests/`" -- the same set, two numbers, in one section, because I
    fixed the sentence I was reading and not the one that depended on it.

    That is the ordinary way a partial fix lands, and it is invisible for the same reason
    entry 138's paragraphs were: nothing compares a document to itself. This does.

    The check is deliberately narrow. It does not try to find every number a document repeats
    -- that needs to know which mentions are about the same set, which no matcher can decide.
    It knows about this one pair, because this pair is the one that broke.
    """
    prose = " ".join(
        (ROOT / "medos" / "README.md").read_bytes().decode("utf-8").split()
    )
    tally = re.search(r"\*\*(\d+) read only `medos/medos/`", prose)
    assert tally, (
        "medos/README.md no longer states how many unit modules read only the platform "
        "package; the argument below it depends on that number"
    )
    argument = re.search(
        r"Moving (?:those )?(\d+|\w+) files into\s*`medos/medos/tests/`", prose
    )
    assert argument, (
        "medos/README.md's argument for the suite's location no longer states how many "
        "files would move. It is the same set as the tally above it and has to agree with it."
    )
    words = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14}
    token = argument.group(1).lower()
    moved = int(token) if token.isdigit() else words.get(token)
    assert moved is not None, (
        f"medos/README.md's argument says \"Moving {token!r} files\" -- not a number this "
        "check can compare against the tally above it. Say how many, or say 'those N' and "
        "let the tally be the one place the number lives."
    )
    assert moved == int(tally.group(1)), (
        f"medos/README.md says {tally.group(1)} modules read only the platform package and "
        f"then that moving {argument.group(1)!r} files would split the suite. Same set, two "
        "numbers. Whichever is measured, both sentences describe it."
    )
