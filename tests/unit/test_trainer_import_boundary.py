# SPDX-License-Identifier: Apache-2.0
"""`trainer/` is ONE program, and it reaches into this platform ZERO times.

WHAT IS IN THAT DIRECTORY
--------------------------
A standalone vanilla-PyTorch framework: an nnU-Net-class trainer written
from scratch (own 3D UNet, own fingerprint-based planner, own masked-loss
training loop, own sliding-window inference), driven by a six-subcommand CLI
(`vanilla-plan`, `vanilla-fit`, `vanilla-import-nnunet`, `predict`,
`declare-environment`, `doctor`). Every input is a filesystem path and every
output is a file.

WHAT USED TO BE THERE, AND WHERE IT WENT
-----------------------------------------
This directory once held TWO programs: the fitter and a `execute`
supervisor that opened the platform's database and drove submitted runs.
The rewrite that replaced the nnU-Net/MONAI backend with the vanilla stack
removed the supervisor's only callers, and the supervisor went with them --
its body had already moved to the platform side, and the remaining
`execute` branch was deleted rather than left calling into a platform this
tree no longer imports. What is left imports no `medos` at all.

The allow-list below reached zero the way it was always meant to: not by
editing the list, but by deleting the imports -- the "declared, no longer
imported" side of the set comparison this file has failed on at every
previous shrink (10 -> 4 -> 3 -> 0).

THE THREE THINGS ASSERTED HERE
------------------------------
1. Every file in the package is classified as the fitter's or the one file
   the CLI lives in -- the old three-way cut (fitter / supervisor / split)
   collapsed into one side when the supervisor went.
2. **No file imports `medos`.** Not "no `medos.db`" -- none of it. This is
   the assertion the whole separation exists to make, and it is TRUE TODAY.
3. The set of `medos.*` modules reached from the package equals the declared
   list in both directions. Both are EMPTY: an empty reached-set against a
   non-empty declared list is a separation somebody finished; a non-empty
   reached-set against an empty list is a boundary crossed.

Spec: MOS-TRAIN-034, MOS-IMG-003 (the shared package as its own home).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
#: Where a dotted `medos.*` name resolves to a file. `medos/` is a PRODUCT
#: directory and the package is `medos/medos/`, so `medos.training.runs` is
#: `medos/medos/training/runs.py`. Resolving it against the repository root
#: instead would silently fail to find the file, and this module's import
#: walk then records the PACKAGE (`medos.training`) where the MODULE
#: (`medos.training.runs`) was imported -- a coarser answer that still looks
#: like an answer.
PKG_ROOT = ROOT / "medos"
TRAINER = ROOT / "trainer" / "medos_trainer"

#: Every top-level file of the package, and the side it belongs to. The
#: whole package is one side now: the vanilla stack is the fitter, and
#: `__main__.py` is just the CLI dispatch over it. The run-directory
#: contract LEFT this set when it became `medos.sdk/contract.py`, the
#: supervisor branch of `__main__.py` left when the vanilla rewrite deleted
#: the `execute` subcommand, and the backend modules (`backend.py`,
#: `port.py`, `plan.py`, `architectures.py`, `training.py`, `nets.py`,
#: `masked.py`, `masked_trainer.py`, `packaging.py`) left with the backend
#: itself. Classified here because the gate's own rule -- a new file with no
#: side is a new file nobody decided about -- made the suite red the day
#: they landed.
FITTER_FILES = {
    "__init__.py", "__main__.py", "environment.py", "stamp.py",
    "standalone.py",
    "detection.py", "overlap.py", "evidence.py",
}
#: EMPTY, AND THAT IS THE POINT. The supervisor's `execute` branch is gone
#: from `__main__.py`; nothing in this package polls the platform's database
#: any more, and nothing may again without a deliberate decision recorded
#: here.
SUPERVISOR_FILES: set[str] = set()
SPLIT_FILES: set[str] = set()

FITTER, SUPERVISOR = "fitter", "supervisor"


def _is_sdk(module: str) -> bool:
    """`medos.sdk` is the shared package, not the platform.

    Kept for the walk below: today NOTHING under the trainer imports even
    the SDK, but the distinction is the one the previous allow-list was
    built on, and an import of `medos.sdk` is still not a reach into the
    rest of the distribution.
    """
    return module == "medos.sdk" or module.startswith("medos.sdk.")


#: The `vanilla/` subpackage is fitter-side BY RULE rather than by per-file
#: listing: it is the framework itself, its files come and go with the
#: stack's development, and every file in it answers to the same property --
#: no `medos` import -- that `test_no_fitter_file_imports_the_platform_at_all`
#: asserts over the whole tree. A file under `vanilla/` failing that
#: assertion fails it by name below.
VANILLA_PREFIX = "vanilla/"


def _imports(path: Path, prefix: str = "medos") -> set[str]:
    """Every dotted `<prefix>.*` module this file imports, at any nesting depth.

    Function-level imports count. Most of this package's import sites are
    inside functions, and a gate that walked only the module body would see
    less than half of what it actually reaches.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == prefix or alias.name.startswith(prefix + "."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == prefix or node.module.startswith(prefix + "."):
                # `from medos.training import runs as tr` names a MODULE, not a symbol.
                for alias in node.names:
                    candidate = f"{node.module}.{alias.name}"
                    if (PKG_ROOT / Path(*candidate.split("."))).with_suffix(".py").is_file():
                        found.add(candidate)
                        continue
                    found.add(node.module)
    return found


def _all_package_files() -> list[Path]:
    """Every `.py` in the package, top level and `vanilla/`, for the import walk."""
    return sorted(
        p for p in TRAINER.rglob("*.py") if "__pycache__" not in p.parts
    )


def _relative(path: Path) -> str:
    return path.relative_to(TRAINER).as_posix()


def _is_classified(relative: str) -> bool:
    name = relative.rsplit("/", 1)[-1]
    return (
        relative.startswith(VANILLA_PREFIX)
        or name in FITTER_FILES
        or name in SUPERVISOR_FILES
        or name in SPLIT_FILES
    )


def _sites() -> dict[str, set[str]]:
    """`medos.*` module -> the trainer filenames that import it.

    `medos.sdk.*` imports are excluded: they are the shared package, not
    platform reach. What this map exists to bound is the trainer's reach
    into the REST of the distribution.
    """
    out: dict[str, set[str]] = {}
    for path in _all_package_files():
        for module in _imports(path):
            if _is_sdk(module):
                continue
            out.setdefault(module, set()).add(_relative(path))
    return out


def test_the_trainer_package_exists_where_this_gate_looks() -> None:
    """Addressed by path, so it says out loud when the path moves.

    The viewer's move taught this twice in a day and the trainer's move
    repeated it: a check pointed at a directory that moved does not fail, it
    iterates over nothing and reports a pass.
    """
    assert TRAINER.is_dir(), f"{TRAINER} does not exist"
    files = {p.name for p in TRAINER.glob("*.py")}
    declared = FITTER_FILES | SUPERVISOR_FILES | SPLIT_FILES
    assert files == declared, (
        f"unclassified: {sorted(files - declared)}; declared but absent: "
        f"{sorted(declared - files)}. Every file in this package belongs to "
        "the fitter or the one file the CLI lives in -- a new file with no "
        "side is a new file nobody decided about"
    )
    vanilla = sorted(p.name for p in (TRAINER / "vanilla").glob("*.py"))
    assert vanilla and "__init__.py" in vanilla, (
        "the vanilla/ subpackage is gone; the framework this gate describes "
        "no longer exists and the classification above is a frozen table"
    )


def test_no_fitter_file_imports_the_platform_at_all() -> None:
    """THE ASSERTION THE WHOLE SEPARATION EXISTS TO MAKE, and it holds today.

    Not "the fitter imports no database" -- the package imports no `medos`
    at all, the SDK included. That is what makes this tree installable by
    somebody with no MedicalOS installation at all: `pip install -r
    requirements.txt` and every subcommand works.
    """
    offenders: dict[str, list[str]] = {}
    for path in _all_package_files():
        if not _is_classified(_relative(path)):
            continue
        reached = sorted(m for m in _imports(path) if not _is_sdk(m))
        if reached:
            offenders[_relative(path)] = reached

    assert not offenders, (
        "a trainer file imports the platform, so the part of this directory "
        "that could be installed by somebody with no MedicalOS installation "
        "no longer can:\n"
        + "\n".join(f"    {f}: {', '.join(m)}" for f, m in sorted(offenders.items()))
    )


def test_the_trainer_reaches_exactly_the_declared_platform_modules() -> None:
    reached = set(_sites())
    declared: set[str] = set()

    assert reached == declared, (
        "the trainer's reach into this platform is not what is declared here.\n"
        f"    undeclared, now imported: {sorted(reached - declared)}\n"
        f"    declared, no longer imported: {sorted(declared - reached)}\n"
        "  Both are empty today. The second list is the one that did the work "
        "historically: this allow-list was meant to SHRINK, and every previous "
        "shrink (10 -> 4 -> 3) landed by failing on exactly that line. A new "
        "entry on the first list is a boundary crossed."
    )


def test_no_trainer_file_imports_even_the_sdk() -> None:
    """THE END STATE THE ALLOW-LIST WAS HEADED FOR, asserted directly.

    `medos.sdk` was the designed dependency -- the contracts' shared home --
    and importing it was never a boundary violation. But the vanilla stack
    replaced everything the trainer used the SDK for (the run directory,
    the bundle writer and the spec parser went with the platform pipeline),
    and a dependency nothing imports is a declaration the project no longer
    needs to get right. Reaching the SDK again would mean the trainer
    regrew a platform contract of its own; that is a decision to make out
    loud, not an import to add quietly.
    """
    offenders: dict[str, list[str]] = {}
    for path in _all_package_files():
        reached = sorted(_imports(path))  # the SDK included this time
        if reached:
            offenders[_relative(path)] = reached

    assert not offenders, (
        "a trainer file imports `medos` -- the SDK included. The vanilla stack "
        "needs nothing from the shared package; an import here is the trainer "
        "regrowing a platform contract of its own:\n"
        + "\n".join(f"    {f}: {', '.join(m)}" for f, m in sorted(offenders.items()))
    )


# ======================================================================================
# What the documents claim about this boundary
# ======================================================================================
#: The two documents that describe the trainer's import boundary in prose.
#: MEASURED before this check existed: both said "Eight of its nine modules
#: import no `medos` at all", and the package held EIGHT modules of which
#: seven imported nothing from the platform -- wrong in both halves. The
#: property the gate enforces is not a count: after the vanilla rewrite it
#: is that NO module imports `medos` at all -- the platform, the SDK, any
#: of it -- because the trainer is a standalone framework.
#:
#: The property stayed stated through every reorganization: "exactly one"
#: while the supervisor's branch lived in `__main__.py`, and "none" after
#: the rewrite removed it. Both documents state it that way now.
BOUNDARY_DOCS = ("trainer/README.md", "CONTRACT.md")

#: A count of this package's modules, written in prose. NOT FORBIDDEN -- a
#: document may count if it wants to, and forbidding the words would forbid
#: the sentence that retracts them, which is the failure three sibling gates
#: in this suite record. What is required is that a count, if stated, is the
#: measured one.
#: SCOPED TO THE PACKAGE, and the scoping is the correction. The first
#: version matched any "<number> modules" and flagged `trainer/README.md`'s
#: "In `tests/`, beside the code they read -- four modules, 54 tests" -- a
#: true claim about `trainer/tests/`, measured against `medos_trainer/`'s
#: headcount. A check that reads one population and matches a sentence about
#: another is not measuring the document; it is measuring its own pattern.
_COUNT_CLAIM = re.compile(
    r"\b(?:(\d+)|(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve))\s+"
    r"of\s+its\s+\w+\s+modules?\b",
    re.I,
)
_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _module_count() -> int:
    return len(list(TRAINER.glob("*.py")))


#: Where in each document the boundary claim lives. PER DOCUMENT AND
#: EXPLICIT, because the two sites are not the same shape: `trainer/README.md`'
#: s whole subject IS the trainer, so the file is the site; `CONTRACT.md` is
#: hundreds of lines about the whole repository and says "exactly one" in an
#: unrelated paragraph about the spike lift, so searching it whole let the
#: old wrong count back in with the check still green. Anchoring on the word
#: "trainer" does not work either -- the README states the boundary in a
#: sentence that never uses it.
BOUNDARY_SITE: dict[str, str | None] = {
    "trainer/README.md": None,  # the whole file
    "CONTRACT.md": r"^trainer/\s",  # the `trainer/` row of §1's layout block
}


def _boundary_window(document: str) -> str:
    text = (ROOT / document).read_text(encoding="utf-8").replace("\r\n", "\n")
    anchor = BOUNDARY_SITE[document]
    if anchor is None:
        return " ".join(text.split())
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if re.search(anchor, line)]
    assert starts, f"{document} no longer has the block {anchor!r} this check reads"
    return " ".join(" ".join(" ".join(lines[i:i + 8]) for i in starts).split())


@pytest.mark.parametrize("document", BOUNDARY_DOCS)
def test_the_documents_state_that_no_module_imports_the_platform(
    document: str,
) -> None:
    """The boundary, not a headcount: ZERO trainer modules import `medos`."""
    # AT THE SITE, not across the file. The first version searched the whole
    # document, and `CONTRACT.md` says "exactly one" in an unrelated paragraph
    # about the spike lift -- so replacing the trainer sentence with the old
    # wrong count left the check GREEN. Caught by the proof-by-breaking case
    # for exactly that mutation.
    window = _boundary_window(document)

    assert re.search(r"(?i)\bno module\b", window), (
        f"{document} describes the trainer without stating, where it describes "
        "it, that NO module imports `medos`. That is the property this module "
        "asserts in code after the vanilla rewrite; a reader cannot check the "
        "boundary against a claim that does not state it."
    )
    assert "medos" in window, (
        f"{document}'s trainer passage no longer names `medos`, so the no-import "
        "claim cannot be read as being about the platform package at all."
    )


@pytest.mark.parametrize("document", BOUNDARY_DOCS)
def test_any_module_count_a_document_states_is_the_measured_one(document: str) -> None:
    """A count is allowed and must be true. Measured at the site, not forbidden as a word."""
    text = (ROOT / document).read_text(encoding="utf-8")
    actual = _module_count()
    wrong: list[str] = []
    for m in _COUNT_CLAIM.finditer(" ".join(text.split())):
        digits, word = m.group(1), m.group(2)
        value = int(digits) if digits else _WORDS[word.lower()]
        # "no module imports medos" is a claim about IMPORTERS, not the headcount
        window = m.string[max(0, m.start() - 40):m.end() + 40].lower()
        if "import" in window and value == 1:
            continue
        if value != actual:
            wrong.append(f"{m.group(0)!r} (the package holds {actual})")
    assert not wrong, (
        f"{document} states a module count that is not the measured one: {wrong}. Either "
        "correct it, or say the property instead -- the boundary does not drift and a "
        "headcount does."
    )
