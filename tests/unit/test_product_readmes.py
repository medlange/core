# SPDX-License-Identifier: Apache-2.0
"""Each product's README describes its own tree, and one of them had stopped.

MEASURED before this check existed, over the four product READMEs:

    trainer/README.md line 3   said the platform package is `medicalos/medos`. There is no
                               `medicalos/` directory; it is `medos/medos`.
    trainer/README.md line 5   said `medos/medos/training/chain.py` "generates MONAI Bundle
                               configs as data" -- in the paragraph explaining what the
                               trainer does and does not import, which is the paragraph a
                               reader trusts most. The file is
                               `medos.sdk/chain.py` and has been since
                               MOS-IMG-003's package was built. medos/README.md names it
                               correctly, so the two READMEs disagreed about one file.
    trainer/README.md ×4       cited `medos/medos/training/autoconfig.py`, now
                               `medos.sdk/autoconfig.py` -- once in a table
                               row making a substantive claim about what
                               `DERIVED_QUANTITIES` contains.
    trainer/README.md line 252 cited `medos/medos/training/spec.py::parse_spec`, now
                               `medos.sdk/spec.py`.

All four modules moved OUT of the platform when `medos.sdk` was built, which
is the move that makes the trainer installable by somebody with no MedicalOS -- the exact
property that README's third paragraph exists to explain. The citations stayed behind.

viewer/README.md, medos/README.md and medos.sdk/README.md were measured
clean, including medos/README.md's 21-row package table (exact in both directions) and
medos.sdk/README.md's importer counts (35 and 6, both exact).

WHY A PATH AND NOT THE PROSE. A check cannot tell whether a sentence about a module is
still true. It can tell whether the module is there, and every one of the defects above was
of that kind: not a wrong description, a description of something at an address that has
nothing at it. That is the half that is decidable, and moving a file is how it happens.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: product directory -> its README
READMES: dict[str, Path] = {
    "medos": ROOT / "medos" / "README.md",
    "viewer": ROOT / "viewer" / "README.md",
    "trainer": ROOT / "trainer" / "README.md",
    # The SDK README moved with the package: `medicalos_preprocessing/README.md` became
    # `medos/medos/sdk/README.md` when the contracts moved inside the distribution.
    "medos.sdk": ROOT / "medos" / "medos" / "sdk" / "README.md",
}

#: A backticked repository-looking path, with an optional `::symbol` suffix.
PATH_IN_DOC = re.compile(r"`((?:[\w.-]+/)+[\w.-]*)(?:::[\w.]+)?`")

#: Paths that are NOT repository paths and must not be resolved as such. Each is named with
#: what it is inside, because a silent skip list is how a check stops checking: a typo in a
#: repository path that happened to look like one of these would be waved through.
#: THE FOUR MONAI-BUNDLE ENTRIES WENT WITH THE TRAINER REWRITE: they existed to cover
#: the old trainer README's bundle-internal citations, and no product README names
#: `configs/metadata.json`, `configs/preprocessing.json`, `models/model.ts` or `images/`
#: any more. `configs/inference.json` stays because medos.sdk/README.md -- core product
#: documentation this gate may not edit -- still names it for what it is.
NOT_REPOSITORY_PATHS: dict[str, str] = {
    "configs/inference.json": "a file inside a MONAI Bundle, not in this repository",
    # INSIDE A FIT'S OUTPUT BUNDLE, not the repository: `vanilla-fit --out DIR`
    # writes DIR/checkpoints/epoch-<n>/ top-K snapshot bundles and
    # DIR/checkpoints/index.json ranking them — trainer/README.md documents
    # that layout to explain --resume-from and snapshot retention. They exist
    # only after a fit runs; no checkout of this repository contains them.
    "checkpoints/": (
        "a directory a vanilla-fit run creates inside its OUT bundle "
        "(top-K checkpoint retention), cited by trainer/README.md"
    ),
    "checkpoints/index.json": (
        "the top-K index a vanilla-fit run writes inside its OUT bundle, "
        "cited by trainer/README.md"
    ),
}

#: Paths a README names in order to say they do NOT exist. A document is allowed to do
#: this, and the first version of this check called one a defect: medos/README.md argues
#: that the platform's suite belongs at the repository root, and names `medos/medos/tests/`
#: twice to say what it would cost to move it there. A check that forbade naming an absent
#: path would forbid making that argument.
NAMED_TO_SAY_IT_DOES_NOT_EXIST: dict[str, str] = {
    "medos/medos/tests/": (
        "medos/README.md's argument for why the platform's suite sits at the repository "
        "root: 'a `medos/medos/tests/` would have to…', 'Moving eleven files into "
        "`medos/medos/tests/` would…'. If this directory is ever created, the argument is "
        "settled the other way and both sentences have to go."
    ),
}

#: medos.sdk/README.md's "Who imports it" table. Exact, not toleranced: the
#: numbers carry an argument ("35 modules were repointed at it"), and a new importer is a
#: one-line README edit. Same treatment the register size gets in test_compose_profiles.py.
#: THE TRAINER ROW IS GONE, AND THAT IS THE ASSERTION'S SHAPE NOW: the vanilla-stack
#: rewrite made the trainer standalone, and it imports `medos.sdk` zero times -- a count
#: the SDK README's table no longer states, so there is nothing to compare it against.
#: The trainer's independence is held by tests/unit/test_trainer_import_boundary.py,
#: which asserts the trainer reaches no `medos` module at all, the SDK included.
IMPORTER_COUNTS: dict[str, int] = {"medos/medos": 35}

#: medos/README.md's package table states a line count per subpackage. TOLERANCED at 10%,
#: and MOS-TEST-003 requires a tolerance to be named rather than implied: an exact assertion
#: would go red on every edit to the platform and be deleted within a week, and the claim
#: the table actually makes is about relative size. The DIRECTORY SET is exact -- that is
#: the part that fails when a subpackage is added or removed without the table being
#: touched, which is the failure worth catching.
LINE_COUNT_TOLERANCE = 0.10


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _paths_named(readme: Path) -> list[str]:
    return sorted(set(PATH_IN_DOC.findall(_text(readme))))


def _tracked() -> list[str]:
    # UNTRACKED FILES COUNT: this list decides whether a path a README cites
    # RESOLVES, and a README documenting a module added in the same change would
    # otherwise be told its own citation is broken.
    return subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()


TRACKED = _tracked()
TOP_LEVEL = {p.split("/")[0] for p in TRACKED}
#: every directory NAME that occurs anywhere in a tracked path, which is what makes a
#: row-relative citation like `api/routes_training.py` recognisable as one.
DIR_NAMES = {part for p in TRACKED for part in p.split("/")[:-1]}


def _verdict(raw: str, product: str) -> str:
    """One of: ok, absent, unchecked, or a declared skip reason.

    Six cases, each decidable without reading the sentence around the path:

      1. `../x`            resolve against the README's own directory
      2. declared          a bundle-internal path, or one named to say it is not there
      3. root-relative     first segment is a tracked top-level entry -> resolve from root
      4. product-relative  resolves against the README's own product directory
      5. row-relative      first segment names a directory that exists SOMEWHERE, so the
                           path is relative to whatever the table row's subject is, which
                           no matcher can recover -> unchecked, and counted
      6. otherwise         nothing in this repository is called that: a defect
    """
    target = raw.rstrip("/")
    here = READMES[product].parent
    if raw.startswith("../"):
        return "ok" if (here / target).exists() else "absent"
    if raw in NOT_REPOSITORY_PATHS or raw in NAMED_TO_SAY_IT_DOES_NOT_EXIST:
        return "declared"
    first = target.split("/")[0]
    # Either reading counts. `viewer/README.md` writes `tests/test_independence.py` for its
    # OWN suite while writing `../tests/unit/test_viewer_deployment.py` for the platform's,
    # so a first segment that happens to name a top-level directory does not settle which
    # root was meant. The question this check answers is whether anything is at the address,
    # not which of two addresses the author had in mind.
    if first in TOP_LEVEL:
        if (ROOT / target).exists() or (here / target).exists():
            return "ok"
        return "absent"
    if (here / target).exists():
        return "ok"
    if first in DIR_NAMES:
        return "unchecked"
    return "absent"


def _tracked_py(prefix: str) -> list[Path]:
    # UNTRACKED FILES COUNT: a new module's lines belong to its package's total the
    # moment it exists, and the table's tolerance is wide enough that one file does
    # not move it -- so the honest reading costs nothing and the stale one drifts.
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", prefix],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()
    return [ROOT / p for p in out if p.endswith(".py")]


def _lines(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        total += len(path.read_bytes().decode("utf-8", "replace").splitlines())
    return total


# ======================================================================================
# Every path every product README names
# ======================================================================================
@pytest.mark.parametrize("product", sorted(READMES))
def test_every_path_a_product_readme_names_resolves(product: str) -> None:
    readme = READMES[product]
    named = _paths_named(readme)
    assert len(named) >= 5, (
        f"only {len(named)} path(s) extracted from {readme.name}; the matcher is broken, "
        "not the README"
    )
    absent = [raw for raw in named if _verdict(raw, product) == "absent"]
    assert not absent, (
        f"{readme.relative_to(ROOT).as_posix()} names {len(absent)} path(s) with nothing "
        f"at them: {absent}. Four of these were modules that moved out of the platform "
        "into medos.sdk and left their citations behind, and one was "
        "`medicalos/medos` for a package that is `medos/medos`."
    )


def test_the_unchecked_remainder_is_visible_rather_than_silent() -> None:
    """Case 5 of `_verdict`: a path relative to a table row's subject.

    `medos.sdk/README.md`'s importer table names `api/routes_training.py`
    against a row whose subject is `medos/medos/`, so the path is right and no matcher can
    know it. Those are left unchecked -- and COUNTED, so the unchecked half cannot grow
    quietly, which is the treatment `UNREGISTERED_CONTENTS` gets in
    `tests/_support/release_criteria.py`.
    """
    unchecked = {
        product: sorted(
            raw for raw in _paths_named(readme)
            if _verdict(raw, product) == "unchecked"
        )
        for product, readme in READMES.items()
    }
    total = sum(len(v) for v in unchecked.values())
    assert total <= 8, (
        f"{total} path(s) across the product READMEs are relative to something this check "
        f"cannot recover, up from 8: {unchecked}. Write them root-relative and they become "
        "checkable."
    )


def test_the_skip_list_is_not_hiding_a_repository_path() -> None:
    """A skip list that starts matching real paths stops being a skip list."""
    wrong = sorted(p for p in NOT_REPOSITORY_PATHS if (ROOT / p.rstrip("/")).exists())
    assert not wrong, (
        f"{wrong} are declared NOT repository paths and now exist in the repository. "
        "Remove them from NOT_REPOSITORY_PATHS so they are checked like any other path."
    )


def test_neither_declared_list_is_carrying_a_dead_entry() -> None:
    mentioned: set[str] = set()
    for readme in READMES.values():
        mentioned |= set(_paths_named(readme))
    for label, declared in (
        ("NOT_REPOSITORY_PATHS", NOT_REPOSITORY_PATHS),
        ("NAMED_TO_SAY_IT_DOES_NOT_EXIST", NAMED_TO_SAY_IT_DOES_NOT_EXIST),
    ):
        dead = sorted(set(declared) - mentioned)
        assert not dead, (
            f"{label} declares {dead}, which no product README names any more. An "
            "exception nothing uses reads as a rule somebody needed."
        )


def test_nothing_named_to_say_it_is_absent_has_been_created() -> None:
    """If `medos/medos/tests/` appears, medos/README.md's argument lost.

    It has to be rewritten in the same change rather than left arguing against a directory
    that now exists.
    """
    built = sorted(
        p for p in NAMED_TO_SAY_IT_DOES_NOT_EXIST if (ROOT / p.rstrip("/")).exists()
    )
    assert not built, (
        f"{built} now exist, and a README names each of them to say they do not. Rewrite "
        f"the argument: {[NAMED_TO_SAY_IT_DOES_NOT_EXIST[p] for p in built]}"
    )


# ======================================================================================
# medos/README.md's package table
# ======================================================================================
def _package_table() -> dict[str, int]:
    rows = re.findall(
        r"^([a-z_]+)/\s{2,}(?:.+?)\s{2,}([\d  ]+)$", _text(READMES["medos"]), re.M
    )
    return {
        name: int(count.replace(" ", "").replace(" ", ""))
        for name, _, count in [(n, None, c) for n, c in rows]
    }


def _packages_on_disk() -> list[str]:
    pkg = ROOT / "medos" / "medos"
    return sorted(
        d.name for d in pkg.iterdir()
        if d.is_dir() and not d.name.startswith(("_", "."))
        and (d / "__init__.py").exists()
    )


def test_the_package_table_lists_exactly_the_packages_that_exist() -> None:
    claimed = _package_table()
    assert claimed, "medos/README.md's package table no longer parses"
    on_disk = _packages_on_disk()
    assert sorted(claimed) == on_disk, (
        f"medos/README.md's table lists {sorted(claimed)};\n"
        f"medos/medos/ holds {on_disk}\n"
        f"  missing from the table: {sorted(set(on_disk) - set(claimed))}\n"
        f"  in the table, not on disk: {sorted(set(claimed) - set(on_disk))}"
    )


def test_the_package_table_line_counts_are_within_tolerance() -> None:
    claimed = _package_table()
    drifted: list[str] = []
    for name, stated in sorted(claimed.items()):
        actual = _lines(_tracked_py(f"medos/medos/{name}"))
        if stated and abs(actual - stated) / stated > LINE_COUNT_TOLERANCE:
            drifted.append(
                f"{name}: table says {stated}, measured {actual} "
                f"({(actual - stated) / stated * 100:+.0f}%)"
            )
    assert not drifted, (
        f"{len(drifted)} row(s) of medos/README.md's package table are more than "
        f"{LINE_COUNT_TOLERANCE:.0%} from the tracked Python they describe:\n  "
        + "\n  ".join(drifted)
    )


# ======================================================================================
# medos.sdk/README.md's importer table
# ======================================================================================
@pytest.mark.parametrize("prefix", sorted(IMPORTER_COUNTS))
def test_the_importer_counts_are_exact(prefix: str) -> None:
    """`MOS-IMG-003` made this its own package and 35 modules were repointed at it.

    The count is the argument, not decoration, and the README says so: "the package was
    created, 35 modules were repointed at it, and neither Dockerfile copied it".
    """
    stated = IMPORTER_COUNTS[prefix]
    out = subprocess.run(
        ["git", "grep", "-l", "medos.sdk", "--", prefix,
         ":(exclude)medos/medos/sdk"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()
    assert len(out) == stated, (
        f"{len(out)} module(s) under {prefix} import medos.sdk; "
        f"medos.sdk/README.md's table says {stated}. The SDK's own sibling imports "
        "under medos/medos/sdk are the package talking to itself and are excluded -- "
        "the count is the argument about EXTERNAL importers."
    )
    assert f"**{stated}**" in _text(READMES["medos.sdk"]), (
        f"the README no longer states {stated} for {prefix}; IMPORTER_COUNTS and the "
        "README have come apart"
    )


# ======================================================================================
# viewer/README.md's shipped-surface claim
# ======================================================================================
#: What "64 files, ~20 000 lines" counts: everything tracked under `viewer/` except its own
#: suite and its own README. MEASURED: 64 files exactly, 22,189 lines.
#:
#: WHY THIS ONE IS GATED AND THE PACKAGE TABLE'S COUNTS ARE TOLERANCED. The file count is a
#: claim about the SOUP surface, and `docs/adr/BUILD_VS_ADOPT.md` rests the IEC 62304 §8.1.2
#: argument on it: characterisation cost "scales with what it *ships*, not with what is
#: *used*". A number that carries that argument is worth failing on exactly. The line figure
#: carries no argument beyond scale, is written with a `~`, and a tilde with no stated
#: tolerance means nothing -- so the tolerance is stated here. It was "~17 000" and correct
#: until `viewer/i18n/` arrived with 2,256 lines, which is drift with a cause rather than
#: rot, and is why the tolerance is wide.
VIEWER_SHIPPED_FILES = 64
VIEWER_LINE_TOLERANCE = 0.15


def _viewer_shipped() -> tuple[int, int]:
    # TRACKED ONLY, AND DELIBERATELY -- the one site of the ten that entry 134's fix
    # must not reach. The 48 is a claim about the SHIPPED surface, and what ships is
    # what a clean checkout holds: `git archive` and an image built from one carry
    # the index, not the working tree. An uncommitted file in this directory is not
    # part of the repository yet, so counting it would make the number disagree with
    # every build. It would also turn this check red right now -- another session has
    # an untracked `viewer/src/ui/measurements-labels.js` open -- for work in flight
    # rather than for a defect, which register entry 123 is the standing argument
    # against. The count becomes 49 when they commit it, and that is when the README
    # should say 49.
    tracked = subprocess.run(
        ["git", "ls-files", "viewer"], cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()
    ship = [
        p for p in tracked
        if not p.startswith("viewer/tests/") and p != "viewer/README.md"
    ]
    lines = sum(
        len((ROOT / p).read_bytes().decode("utf-8", "replace").splitlines())
        for p in ship
    )
    return len(ship), lines


def test_the_viewer_ships_the_number_of_files_its_readme_states() -> None:
    files, _ = _viewer_shipped()
    stated = re.search(r"(\d+) files, ~", _text(READMES["viewer"]))
    assert stated, "viewer/README.md no longer states a shipped-file count"
    assert int(stated.group(1)) == files == VIEWER_SHIPPED_FILES, (
        f"viewer/README.md says {stated.group(1)} shipped files, this module was written "
        f"against {VIEWER_SHIPPED_FILES}, and {files} are tracked under viewer/ outside "
        "its suite and its README. This count carries the IEC 62304 8.1.2 argument in "
        "docs/adr/BUILD_VS_ADOPT.md -- characterisation cost scales with what ships -- so "
        "it is exact rather than approximate. Update the README in the change that adds "
        "or removes the file."
    )


def test_the_viewer_line_figure_is_within_its_stated_tolerance() -> None:
    _, lines = _viewer_shipped()
    stated = re.search(r"~(\d[\d  ]*\d) lines", _text(READMES["viewer"]))
    assert stated, "viewer/README.md no longer states a line figure"
    claimed = int(stated.group(1).replace(" ", "").replace(" ", ""))
    drift = abs(lines - claimed) / claimed
    assert drift <= VIEWER_LINE_TOLERANCE, (
        f"viewer/README.md says ~{claimed} lines and the shipped tree measures {lines} "
        f"({drift:.0%} away, tolerance {VIEWER_LINE_TOLERANCE:.0%}). A `~` with no stated "
        "tolerance means nothing, which is why this check states one."
    )
