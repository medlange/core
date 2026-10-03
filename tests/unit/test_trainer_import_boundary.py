# SPDX-License-Identifier: Apache-2.0
"""`trainer/` is TWO PROGRAMS, and only one of them still reaches into this platform.

WHAT IS IN THAT DIRECTORY
--------------------------
Two programs separated by an `argparse` branch rather than by a directory:

  THE FITTER -- `plan`, `fit`, `declare-environment`, `doctor`. It takes `--run-dir`,
  reads a directory, fits a model, writes a bundle. **It imports no `medos` at all.**
  Everything it needs from outside itself is `medos.sdk`, the package
  `MOS-IMG-003` requires and that this repository had built inside `medos/medos/` until the
  split. A different organisation can install it.

  THE SUPERVISOR -- `execute`, `execute --watch`. It opens `MEDOS_DATABASE_URL`, polls
  `training_runs` for `PENDING`, and writes `RUNNING`/`SUCCEEDED`/`FAILED` back. It
  contains no fitting code at all; the fit is a child process it forks of itself. It is
  this deployment's job-queue worker wearing the trainer's name, and `executor.py`'s own
  docstring says so: "It is deployment code: it knows this deployment's database URL,
  this deployment's image staging, and which orchestrator driver this deployment runs."

As shipped, the supervisor is the image's identity -- `medos/deploy/compose/docker-compose.yml`
runs `command: ["execute", "--watch", "15"]` with `restart: unless-stopped`.

WHY A GATE, AND WHAT CHANGED UNDER IT
--------------------------------------
NOTHING bounded the trainer's imports when this file was written: no allow-list in
`tests/unit/test_declared_dependencies.py`, none in `tests/unit/test_trainer_contract.py`,
none in `tests/integration/test_trainer_image.py`;
`tests/integration/test_trainer_boundary.py` is one-directional and asserts only that the
PLATFORM does not import the trainer.

The allow-list started with ten modules. Six of them were the fitter's -- a spec format, a
bundle layout, a phantom, a digest rule -- and they are now in `medos.sdk`,
so they left this list. **They left it by failing this gate**, on the
"declared, no longer imported" side: the half of a set comparison people leave out, and
the half that says the work finished.

Four remain. All the supervisor's, and all of them a database.

THE FOUR THINGS ASSERTED HERE
------------------------------
1. Every file in the package is classified as the fitter's, the supervisor's, or the one
   file the cut runs through.
2. **No fitter file imports `medos`.** Not "no `medos.db`" -- none of it. This is the
   assertion the whole separation exists to make, and it is TRUE TODAY.
3. The set of `medos.*` modules reached from the package equals the declared list, in both
   directions, so an entry nobody imports any more is a failure rather than a formality.
4. What the supervisor still drags in is measured, so the cost of it living here stays a
   number rather than an argument.

Spec: MOS-TRAIN-034, MOS-IMG-003 (`medicalos-preprocessing` as its own package).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
#: Where a dotted `medos.*` name resolves to a file. `medos/` is a PRODUCT directory
#: and the package is `medos/medos/`, so `medos.training.runs` is
#: `medos/medos/training/runs.py`. Resolving it against the repository root instead
#: silently fails to find the file, and this module's import walk then records the
#: PACKAGE (`medos.training`) where the MODULE (`medos.training.runs`) was imported --
#: a coarser answer that still looks like an answer.
PKG_ROOT = ROOT / "medos"
TRAINER = ROOT / "trainer" / "medos_trainer"

#: Which file belongs to which program. `__main__.py` is BOTH because the cut runs through
#: its `argparse` dispatch: `plan`/`fit`/`doctor`/`declare-environment` are the fitter's,
#: `execute` is the supervisor's.
#: `contract.py` LEFT THIS SET rather than changing sides. The run-directory exchange is a
#: contract between two programs, so it belongs to neither: it is
#: `medos.sdk/contract.py` now, which is also what lets the supervisor stop
#: importing the trainer to read it.
#:
#: THE EIGHT MODULES OF THE `BUILD_VS_ADOPT` ROW joined this set when the training-plan
#: pipeline port was built: `plan.py`, `port.py` and `architectures.py` are the row's named
#: deliverables, and `training.py`, `nets.py`, `masked.py`'s companions `detection.py`,
#: `overlap.py` and `evidence.py` are the fitter's own metric and model modules. All eight
#: are fitter-side by the row's own argument: they instantiate nothing the supervisor
#: touches, they reach no `medos.*` import, and
#: `test_no_fitter_file_imports_the_platform_at_all` below holds over them. Classified here
#: because the gate's own rule -- a new file with
#: no side is a new file nobody decided about -- made the suite red the day they landed;
#: the decision the other change left open is recorded as FITTER.
FITTER_FILES = {
    "__init__.py", "backend.py", "environment.py",
    "masked.py", "masked_trainer.py", "packaging.py", "stamp.py",
    "plan.py", "port.py", "architectures.py", "training.py", "nets.py",
    "detection.py", "overlap.py", "evidence.py",
}
#: EMPTY, AND THAT IS THE POINT. `executor.py` -- 540 lines of `training_runs` state
#: machine -- is `medos/medos/training/supervisor.py` now. It moved without a single change to
#: how anything is deployed: it imported no trainer module once the run-directory contract
#: became `medos.sdk/contract.py`, so the move was a rename plus two import
#: lines. What is left in this package of the supervisor is the `execute` BRANCH of
#: `__main__.py`, which calls into the platform rather than being it.
SUPERVISOR_FILES: set[str] = set()
SPLIT_FILES = {"__main__.py"}

FITTER, SUPERVISOR = "fitter", "supervisor"


def _is_sdk(module: str) -> bool:
    """`medos.sdk` is not the platform -- it is the SDK the fitter legitimately consumes.

    Before the pivot the gate could treat every `medos.*` import as a reach into the
    platform, because the contracts lived in a separate top-level package. They moved
    INSIDE the distribution (`medos/medos/sdk/`), so the fitter's `from medos.sdk import
    ...` lines are the designed dependency, not a boundary violation. Everything else
    under `medos.*` is still the platform and still governed by the allow-list below.
    """
    return module == "medos.sdk" or module.startswith("medos.sdk.")

#: Every `medos.*` module the trainer package may import, the program that may import it,
#: and why. All four are the supervisor's; the fitter's six left when
#: `medos.sdk` was created.
ALLOWED: dict[str, tuple[str, str]] = {
    "medos.training.supervisor": (
        SUPERVISOR,
        "the `training_runs` state machine, now on the PLATFORM side where its database "
        "is. `__main__.py`'s `execute` branch calls into it. This is the entry that the "
        "next step removes, by moving that branch after it",
    ),
    "medos.training.runs": (
        SUPERVISOR,
        "`__main__.py`'s `--watch` loop lists PENDING rows directly. The listing belongs "
        "with the supervisor it feeds",
    ),
    "medos.training.orchestrator": (
        SUPERVISOR,
        "`LocalProcessOrchestrator`, constructed by the `execute` branch. `MOS-REL-039` "
        "keeps the fit a CHILD PROCESS of this image rather than a second deployable, so "
        "the driver is instantiated here on purpose -- what has no business being here is "
        "the code that decides WHEN to instantiate it",
    ),
}


def _imports(path: Path, prefix: str = "medos") -> set[str]:
    """Every dotted `<prefix>.*` module this file imports, at any nesting depth.

    Function-level imports count. Most of this package's import sites are inside
    functions, and a gate that walked only the module body would see less than half of
    what it actually reaches.
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


def _sites() -> dict[str, set[str]]:
    """`medos.*` module -> the trainer filenames that import it.

    `medos.sdk.*` imports are excluded: they are the SDK, not platform reach. What this
    map exists to bound is the trainer's reach into the REST of the distribution.
    """
    out: dict[str, set[str]] = {}
    for path in sorted(TRAINER.glob("*.py")):
        for module in _imports(path):
            if _is_sdk(module):
                continue
            out.setdefault(module, set()).add(path.name)
    return out


def _closure(seeds: set[str]) -> set[str]:
    """Every `medos.*` module reachable from these, transitively, within this repository."""
    seen: set[str] = set()
    queue = list(seeds)
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        path = (PKG_ROOT / Path(*module.split("."))).with_suffix(".py")
        if not path.is_file():
            path = PKG_ROOT / Path(*module.split(".")) / "__init__.py"
        if not path.is_file():
            continue
        queue.extend(_imports(path))
    return seen


def test_the_trainer_package_exists_where_this_gate_looks() -> None:
    """Addressed by path, so it says out loud when the path moves.

    The viewer's move taught this twice in a day and the trainer's move repeated it: a
    check pointed at a directory that moved does not fail, it iterates over nothing and
    reports a pass.
    """
    assert TRAINER.is_dir(), f"{TRAINER} does not exist"
    files = {p.name for p in TRAINER.glob("*.py")}
    declared = FITTER_FILES | SUPERVISOR_FILES | SPLIT_FILES
    assert files == declared, (
        f"unclassified: {sorted(files - declared)}; declared but absent: "
        f"{sorted(declared - files)}. Every file in this package belongs to the fitter, "
        "to the supervisor, or is the one file the cut runs through -- a new file with no "
        "side is a new file nobody decided about"
    )


def test_no_fitter_file_imports_the_platform_at_all() -> None:
    """THE ASSERTION THE WHOLE SEPARATION EXISTS TO MAKE, and it holds today.

    Not "the fitter imports no database" -- the fitter imports no `medos` beyond the SDK.
    Everything it needs from outside itself comes from `medos.sdk`, the package the
    contracts were moved into when the platform became the SDK; both images install it,
    and `tests/unit/test_shared_package_is_pure.py` keeps it clean.

    `__main__.py` is exempt because the cut runs through it: its `execute` branch is the
    supervisor's. When that branch moves out, the exemption goes with it and this set
    becomes every file in the package.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(TRAINER.glob("*.py")):
        if path.name not in FITTER_FILES:
            continue
        reached = sorted(m for m in _imports(path) if not _is_sdk(m))
        if reached:
            offenders[path.name] = reached

    assert not offenders, (
        "a FITTER file imports the platform, so the part of this directory that could be "
        "installed by somebody with no MedicalOS installation no longer can:\n"
        + "\n".join(f"    {f}: {', '.join(m)}" for f, m in sorted(offenders.items()))
        + "\n  What it needs belongs in `medos.sdk`, which is installed "
        "beside it. `tests/unit/test_shared_package_is_pure.py` keeps that package clean."
    )


def test_the_trainer_reaches_exactly_the_declared_platform_modules() -> None:
    reached = set(_sites())
    declared = set(ALLOWED)

    assert reached == declared, (
        "the trainer's reach into this platform is not what is declared here.\n"
        f"    undeclared, now imported: {sorted(reached - declared)}\n"
        f"    declared, no longer imported: {sorted(declared - reached)}\n"
        "  The second list is not a formality: this allow-list is meant to SHRINK, and an "
        "entry nobody deleted is a separation nobody finished. Six entries left it when "
        "`medos.sdk` was created, by failing on exactly that line."
    )


def test_every_remaining_entry_is_the_supervisors() -> None:
    """When this list stops being all-supervisor, something went to the wrong side.

    The allow-list has one job left: to reach zero. Every entry is a module the SUPERVISOR
    needs, so a fitter-side entry appearing here would mean the fitter acquired a platform
    dependency and somebody wrote it down instead of removing it.
    """
    wrong = {m: side for m, (side, _) in ALLOWED.items() if side != SUPERVISOR}
    assert not wrong, (
        f"these are declared as something other than the supervisor's: {wrong}. After the "
        "`medos.sdk` split there is no legitimate fitter entry: the fitter "
        "imports no `medos` at all, and the test above asserts it"
    )

    sites = _sites()
    misplaced: dict[str, list[str]] = {}
    for module in ALLOWED:
        for filename in sorted(sites.get(module, set())):
            if filename in FITTER_FILES:
                misplaced.setdefault(filename, []).append(module)
    assert not misplaced, (
        "a fitter file imports a supervisor module:\n"
        + "\n".join(f"    {f}: {', '.join(m)}" for f, m in sorted(misplaced.items()))
    )


def test_what_the_supervisor_still_drags_in_is_measured_rather_than_argued() -> None:
    """The cost of the supervisor still living here, as a number that can be watched fall.

    MEASURED when the allow-list held ten entries: the supervisor's closure was 26 modules
    and ~11 000 lines against the fitter's 9 and ~4 000, and the two shared exactly three.
    The fitter's side of that measurement is now ZERO -- it reaches no `medos` at all --
    so what remains is the supervisor's alone.
    """
    closure = _closure(set(ALLOWED))

    assert {"medos.db.tenancy", "medos.db.audit"} <= closure, (
        "the supervisor no longer reaches the platform's tenancy and audit tables. If it "
        "moved out of `trainer/`, this whole file should shrink with it rather than pass "
        "-- the allow-list above still names four modules"
    )
    assert len(closure) > len(ALLOWED), (
        f"the supervisor's four declared modules reach only {len(closure)} modules in "
        "total, which would mean this closure walk stopped working rather than that the "
        "dependency shrank"
    )


# ======================================================================================
# What the documents claim about this boundary
# ======================================================================================
#: The two documents that describe the trainer's import boundary in prose. MEASURED before
#: this check existed: both said "Eight of its nine modules import no `medos` at all", and
#: the package held EIGHT modules of which seven imported nothing from the platform -- wrong
#: in both halves, and about to be wrong by four more as another session adds modules.
#:
#: The property the gate above actually enforces is not a count: it is that EXACTLY ONE
#: module may import the platform, and that it is `__main__.py`, because the cut runs through
#: its `execute` branch. That claim stays true at eight modules and at twelve. Both documents
#: state it that way now.
BOUNDARY_DOCS = ("trainer/README.md", "CONTRACT.md")

#: A count of this package's modules, written in prose. NOT FORBIDDEN -- a document may count
#: if it wants to, and forbidding the words would forbid the sentence that retracts them,
#: which is the failure three sibling gates in this suite record. What is required is that a
#: count, if stated, is the measured one.
#: SCOPED TO THE PACKAGE, and the scoping is the correction. The first version matched any
#: "<number> modules" and flagged `trainer/README.md`'s "In `tests/`, beside the code they
#: read -- four modules, 54 tests" -- a true claim about `trainer/tests/`, measured against
#: `medos_trainer/`'s headcount. A check that reads one population and matches a sentence
#: about another is not measuring the document; it is measuring its own pattern.
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


#: Where in each document the boundary claim lives. PER DOCUMENT AND EXPLICIT, because the
#: two sites are not the same shape: `trainer/README.md`'s whole subject IS the trainer, so
#: the file is the site; `CONTRACT.md` is 296 lines about the whole repository and says
#: "exactly one" in an unrelated paragraph about the spike lift, so searching it whole let
#: the old wrong count back in with the check still green. Anchoring on the word "trainer"
#: does not work either -- the README states the boundary in a sentence that never uses it.
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
def test_the_documents_name_the_single_module_that_may_import_the_platform(
    document: str,
) -> None:
    """The boundary, not a headcount. `__main__.py` must be named as the one exception."""
    # AT THE SITE, not across the file. The first version searched the whole document, and
    # `CONTRACT.md` says "exactly one" in an unrelated paragraph about the spike lift -- so
    # replacing the trainer sentence with the old wrong count left the check GREEN. Caught
    # by the proof-by-breaking case for exactly that mutation.
    window = _boundary_window(document)

    assert "__main__.py" in window, (
        f"{document} describes the trainer without naming `__main__.py` anywhere near it, "
        "and that is the one module permitted to import the platform. A reader cannot check "
        "the boundary against a claim that does not say where it runs."
    )
    assert re.search(r"(?i)exactly one", window), (
        f"{document} no longer states, where it describes the trainer, that exactly one "
        "module imports `medos`. That is the property this module asserts in code; a "
        "document stating a module COUNT instead drifts every time somebody adds a file, "
        "which is how it came to describe a nine-module package that held eight."
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
        # "exactly one module imports medos" is a claim about IMPORTERS, not the headcount
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
