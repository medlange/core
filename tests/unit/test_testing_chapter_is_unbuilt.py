# SPDX-License-Identifier: Apache-2.0
"""Chapter 14 specifies a test infrastructure this repository does not have.

MEASURED, on a tree nobody had touched for the purpose: of the **36 repository paths**
chapter 14 ("Testing and Acceptance", normative, 87 requirements) names, **35 do not
exist**. Among them:

The two figures above were both wrong when this module was written, and the corrections
are recorded rather than swapped in. It said 35 paths and 34 absent because
`PATH_IN_SPEC` was anchored on the directory roots the repository HAS, so a path in a
root that does not exist was invisible: `data/coding/concepts.json` was named by the
chapter and never extracted. And it said 72 requirements, which is the number of INDEX
ROWS -- the chapter defines 87, and fifteen of them are written
`**MOS-TEST-004 (the mutant rule)**`, which the id-only bold pattern could not see. The
index was missing those fifteen too, so the two wrong numbers agreed with each other.

    tests/traceability.yaml     MOS-TEST-002 makes every normative requirement MUST appear
                                here mapped to a check id, and fails the `traceability` job
                                on a requirement mapped to zero. 2,542 requirements are
                                defined across the chapters; the file does not exist.
    tests/mutants/              MOS-TEST-004, "the mutant rule": every acceptance test and
                                every DICOM battery check MUST ship a declared mutant, and
                                a check that survives its own mutant "is a defect of
                                severity equal to the requirement it claims to cover, and
                                blocks release".
    acceptance/                 acceptance criterion 30: "Every one of AT-01 … AT-24 exists
                                as a file under `acceptance/`". None does.
    tests/fixtures/corpus.lock  §14.3.2 specifies its schema field by field.
    tests/stages.yaml           MOS-TEST-005.
    tests/safety-critical.yaml, tests/quarantine.yaml, tests/mutation-score.json,
    medos/deploy/compose/ohif.lock, .ci/core-paths.txt, and nine JSON schemas.

§14.8's `MOS-TEST-078` declares four CI pipelines -- `pr`, `main`, `nightly`, `release` --
with fixed job contents naming twelve jobs. `.github/workflows/` holds ONE file with three
jobs (`unit`, `stack`, `nightly-corpus`), and **not one of the twelve exists**. The
`release` pipeline is the one that would have caught register entry 126's unshipped CLI.

ONE of the six check-id namespaces `MOS-TEST-006` declares is in use, and the first
version of this paragraph said none was. `Q<nn>` is used BY NAME:
`tests/integration/test_queue_parity.py` defines `test_q01_` through `test_q14_`, fourteen
of them over one `driver` fixture, added at `04e9400` on 2026-09-16 -- TEN DAYS before
this module was written at `f42f226`. The claim was false at its own commit, which is the
never-right class rather than the overtaken-by-events class, and `MOS-TEST-045` and
`MOS-TEST-046` are therefore satisfied by name and not merely in substance.

The other five -- `L<level>-<area>-<nnn>`, `D<n>`, `C-<boundary>`, `FT-<nn>`, `AT-<nn>` --
are not, and one of them nearly went the other way: `test_l1_`, `test_l3_` and `test_l4_`
in `tests/integration/test_evidence_schema.py` are `MOS-EVID-034`'s leakage LEVELS, not
`L<level>-<area>-<nnn>` ids. A matcher that reads a prefix and calls it an id would have
reported three namespaces in use. This module still asserts on PATHS rather than on
id-shaped substrings, and that is now the reason rather than the excuse.

WHAT THIS IS NOT. It is not a claim that the repository is untested. It had 1,467 unit
tests when this was written and 1,560 now -- a dated snapshot, left dated rather than
chased -- and `tests/gate/` holds **23** test modules, not the ten this paragraph claimed;
it held 23 at this module's own commit too. They are wired to §15.1.2's release rows
through a marker per release, with a
`--require-stack` switch that turns an unreachable dependency into a failure, and a
discipline of proving every gate red against its own defect. That suite is real and it
works. What is true is that it has NO RELATIONSHIP to the one chapter that specifies
testing: two parallel schemes, one normative and unbuilt, one built and unspecified, and
until this module nothing in the repository compared them.

WHY A FROZEN SET AND NOT A RED SUITE. Failing on all 34 absences would make CI permanently
red and would be turned off within a week. The shape here is the one
`tests/_support/release_criteria.py` uses for its unregistered rows and
`tests/unit/test_gate_contract.py` uses for a check it may not implement: the gap is
recorded exactly, so it CANNOT GROW without a test failure, and it cannot shrink silently
either -- building one of these paths fails this module and forces the register entry to be
updated in the same change.

Register entry 127.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CHAPTER = ROOT / "docs" / "spec" / "14-testing.md"
WORKFLOWS = ROOT / ".github" / "workflows"

#: A backticked repository path inside the chapter.
#:
#: THE ROOT LIST IS THE POPULATION, AND IT WAS ANCHORED ON THE WRONG THING. It read "the
#: directory roots the repository actually has", which makes a path in a root the
#: repository does NOT have invisible -- exactly the paths this module exists to count.
#: `data/coding/concepts.json` (`MOS-TEST-032`) was named by the chapter and never
#: extracted. The list is now the roots the CHAPTER names.
#:
#: Four other roots appear in backticked slash-bearing tokens and are deliberately NOT
#: here, each for a measured reason: `recipes/*.py` (7) are fixture recipes relative to
#: the absent `tests/fixtures/` tree, so counting them would count children of an absence
#: already recorded; `samples/valid/`, `samples/invalid/` and `schema/` are relative to
#: `medos/contracts/<id>/` by §14.5.1's own sentence; and `LISTEN/NOTIFY` is not a path.
PATH_IN_SPEC = re.compile(r"`((?:tests|acceptance|medos|\.ci|docs|data)/[\w./-]+)`")

#: Every path chapter 14 names that does not exist. FROZEN, and asserted exactly in both
#: directions. Adding a name here is recording a new gap; removing one is recording that
#: the chapter started being implemented. Neither may happen by accident.
UNBUILT: tuple[str, ...] = (
    ".ci/core-paths.txt",
    "acceptance/AT-18/",
    # MOS-TEST-032 requires every coded pair to resolve in this file. The path is WRONG as
    # well as unbuilt: the dictionary that exists is `medos/medos/core/concepts.py`.
    "data/coding/concepts.json",
    "docs/ATTRIBUTION.md",
    "docs/dicomweb-conformance.md",
    "docs/migrations/",
    "medos/contracts/C-PREP/samples/valid/",
    "medos/deploy/compose/ohif.lock",
    "medos/schemas/acceptance/test.json",
    "medos/schemas/events/envelope.json",
    "medos/schemas/events/webhook.json",
    "medos/schemas/inference/tensor-contract.json",
    "medos/schemas/preprocessing/spec.json",
    "medos/schemas/queue/message.json",
    "medos/schemas/service/job-input.json",
    "medos/schemas/service/result-bundle.json",
    "medos/schemas/service/service.yaml.json",
    "medos/tools/fixtures/build.py",
    "medos/tools/fixtures/fetch.py",
    "medos/tools/fixtures/recipes/",
    "medos/tools/fixtures/verify.py",
    "tests/e2e/viewer-selectors.yaml",
    "tests/fixtures/",
    "tests/fixtures/corpus.lock",
    "tests/fixtures/corpus/",
    "tests/fixtures/selector_oracle.yaml",
    "tests/fixtures/test_recipes.py",
    "tests/fixtures/test_uid.py",
    "tests/mutants/",
    "tests/mutation-score.json",
    "tests/quarantine.yaml",
    "tests/safety-critical.yaml",
    "tests/stages.yaml",
    "tests/tools.lock",
    "tests/traceability.yaml",
)

#: The CI jobs §14.8's pipeline table names. FROZEN as unbuilt for the same reason.
#: `pr` and `main` are excluded: they are pipeline names that MOS-TEST-079 refers to as a
#: set, not jobs.
UNBUILT_CI_JOBS: tuple[str, ...] = (
    "acceptance-lint",
    "acceptance-render",
    "fixture-licence",
    "fixtures-lock",
    "fx-huge-study",
    "mutants",
    "no-fault-points-in-release",
    "phi-scan",
    "schema-compat",
    "stages",
    "traceability",
    "viewer-pin",
)

#: The pipelines MOS-TEST-078 declares.
PIPELINES: tuple[str, ...] = ("pr", "main", "nightly", "release")


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _named_paths() -> list[str]:
    return sorted(set(PATH_IN_SPEC.findall(_text(CHAPTER))))


def _workflow_jobs() -> set[str]:
    jobs: set[str] = set()
    for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml")):
        doc = yaml.safe_load(_text(path))
        jobs |= set((doc or {}).get("jobs", {}))
    return jobs


# ======================================================================================
# The paths
# ======================================================================================
def test_the_chapter_still_names_paths_to_check() -> None:
    """A green run must not be reachable by the extraction finding nothing."""
    named = _named_paths()
    assert len(named) >= 30, (
        f"only {len(named)} repository path(s) extracted from {CHAPTER.name}; the matcher "
        "is broken, not the chapter"
    )


def test_no_path_the_chapter_names_is_newly_absent() -> None:
    """The gap may shrink. It may not grow."""
    named = _named_paths()
    absent = [p for p in named if not (ROOT / p).exists()]
    surprises = sorted(set(absent) - set(UNBUILT))
    assert not surprises, (
        f"chapter 14 names {len(surprises)} path(s) that do not exist and that UNBUILT "
        f"does not record: {surprises}. Either the path was deleted -- in which case the "
        "chapter now names something that has never been there and the register needs an "
        "entry -- or the chapter was amended to name something new that was not built with "
        "it."
    )


def test_nothing_recorded_as_unbuilt_has_quietly_been_built() -> None:
    """Building one of these is good news, and it must be recorded rather than absorbed."""
    built = sorted(p for p in UNBUILT if (ROOT / p).exists())
    assert not built, (
        f"{len(built)} path(s) recorded as unbuilt now exist: {built}. Remove them from "
        "UNBUILT and say so in register entry 127 in the same change -- the count in that "
        "entry is the thing a reader trusts."
    )


def test_every_unbuilt_path_is_one_the_chapter_actually_names() -> None:
    """A frozen set that drifts from its source stops describing anything."""
    named = set(_named_paths())
    stale = sorted(set(UNBUILT) - named)
    assert not stale, (
        f"UNBUILT records {stale}, which chapter 14 no longer names. If the chapter was "
        "amended to drop them, drop them here too."
    )


def test_the_recorded_size_of_the_gap_is_what_the_register_says() -> None:
    """Register entry 127 states a number. This is what keeps it true."""
    named = _named_paths()
    absent = [p for p in named if not (ROOT / p).exists()]
    register = _text(ROOT / "docs" / "spec" / "99-known-inconsistencies.md")
    entry = re.search(r"^127\. .*$", register, re.M)
    assert entry, "register entry 127 is missing"
    claim = f"{len(absent)} do not exist"
    assert claim in entry.group(0), (
        f"register entry 127 does not say {claim!r}; measured {len(absent)} absent of "
        f"{len(named)} named"
    )


# ======================================================================================
# The CI topology
# ======================================================================================
def test_the_pipelines_the_chapter_declares_are_still_four() -> None:
    text = _text(CHAPTER)
    declared = [p for p in PIPELINES if re.search(rf"^\| `{p}` \|", text, re.M)]
    assert declared == list(PIPELINES), (
        f"§14.8's pipeline table declares {declared}; this module was written against "
        f"{list(PIPELINES)}"
    )


def test_no_ci_job_the_chapter_names_is_newly_absent() -> None:
    actual = _workflow_jobs()
    assert actual, "no workflow job was parsed; the reader is broken, not the workflow"
    missing = sorted(j for j in UNBUILT_CI_JOBS if j not in actual)
    assert missing == sorted(UNBUILT_CI_JOBS), (
        f"{sorted(set(UNBUILT_CI_JOBS) - set(missing))} now exist as workflow job(s). "
        "Remove them from UNBUILT_CI_JOBS and update register entry 127: the chapter's CI "
        "topology has started to be built, which is the news."
    )


def test_the_suites_own_readme_describes_the_jobs_that_exist() -> None:
    """The other direction, and it had drifted.

    `tests/README.md` §CI described the `stack` job as ending "then the week-0 contract
    script" and `nightly-corpus` as running "the contract script with `--require-corpus`".
    That script went with `spikes/week0/` (register entry 118) and the workflow's own
    comment says so; this file did not. A document about a moving tree stops being true in
    exactly that way, and the workflow is parsed here already, so the check costs nothing.
    """
    readme = _text(ROOT / "tests" / "README.md")
    described = set(re.findall(r"^\* `([a-z][a-z0-9-]+)` —", readme, re.M))
    actual = _workflow_jobs()
    assert described == actual, (
        f"tests/README.md describes {sorted(described)}; "
        f".github/workflows/ defines {sorted(actual)}"
    )
    assert "week-0 contract script" not in readme.split(
        "**Both bullets used to end")[0], (
        "the CI section describes a step that was deleted with spikes/week0/"
    )


def test_the_workflow_jobs_that_exist_are_named_nowhere_in_the_chapter() -> None:
    """The two schemes do not overlap, and that is the finding rather than a detail.

    `unit`, `stack` and `nightly-corpus` are what CI runs. Chapter 14 names none of them.
    If this ever fails, the chapter and the workflow have started to converge -- which is
    the outcome this module exists to make visible.
    """
    text = _text(CHAPTER)
    overlapping = sorted(j for j in _workflow_jobs() if f"`{j}`" in text)
    assert not overlapping, (
        f"chapter 14 now names the workflow job(s) {overlapping}. The chapter and the "
        "workflow have begun to describe the same CI; record it in entry 127."
    )
