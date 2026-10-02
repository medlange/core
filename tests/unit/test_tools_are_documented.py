# SPDX-License-Identifier: Apache-2.0
"""A tool nobody documented is a tool nobody can be told to run.

`medos/tools/` holds thirteen programs. Measured before this check existed, five of them
appeared in NO markdown file anywhere in the repository:

    capability_cohort_report.py     the proof harness for every registered capability
    publish_model.py                chapter 6's packaging step
    medicalos_verify.py             `medicalos-verify`, which MOS-EVID-124 names
    ingest/stratified_split.py      writes splits_final.json
    trainer/resume_preprocess.py    resumes an hours-long preprocessing run

and `permcheck.py` appeared only inside the known-inconsistencies register, which is where
a repository records what it has NOT done.

WHY THIS IS NOT PEDANTRY. `medicalos_verify.py` is the reader a third party uses to check
a ValidationReport without trusting the platform that wrote it -- chapter 7 §7.12.3 makes
that an obligation, and an obligation nobody can find the tool for is unmet in practice.
`permcheck.py` is how a person runs chapter 8's acceptance check 5 before opening a pull
request. Neither is discoverable by reading the documentation, which is the only thing a
new contributor has.

WHAT THIS ASSERTS, AND WHAT IT DELIBERATELY DOES NOT. It asserts that every tool is NAMED
somewhere a reader will look. It does not judge the description, because a check cannot,
and a check that tried would be satisfied by a sentence that says nothing. Naming is the
part that can be verified, and it is the part that was missing.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "medos" / "tools"

#: Where a reader looks. The register is NOT in this list on purpose: an entry there says
#: a thing is wrong, not that a tool exists to be run.
DOCUMENTS = ("README.md", "DEVELOPMENT.md", "CONTRIBUTING.md", "CONTRACT.md",
             "ARCHITECTURE.md", "docs/README.md")


def _tools() -> list[Path]:
    return sorted(
        p for p in TOOLS.rglob("*.py")
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    )


def _prose() -> str:
    parts = []
    for name in DOCUMENTS:
        path = ROOT / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    for path in sorted((ROOT / "docs" / "services").glob("*.md")):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_every_tool_is_named_in_a_document_a_reader_will_open() -> None:
    tools = _tools()
    assert len(tools) >= 10, (
        f"only {len(tools)} tools found under {TOOLS}; this check is reading the wrong "
        "directory and would pass for that reason alone"
    )

    prose = _prose()
    undocumented = [
        p.relative_to(ROOT).as_posix()
        for p in tools
        if p.relative_to(ROOT).as_posix() not in prose and p.name not in prose
    ]

    assert not undocumented, (
        f"{len(undocumented)} of {len(tools)} tools are named in none of "
        f"{list(DOCUMENTS)}:\n    " + "\n    ".join(undocumented) + "\n"
        "  A tool nobody documented is a tool nobody can be told to run, and two of the "
        "ones this check was written for -- `medicalos_verify.py` and `permcheck.py` -- "
        "are how a person discharges an obligation the specification places on them. "
        "DEVELOPMENT.md has a section for each kind; add a row and say what the tool "
        "will and will not do."
    )


def test_the_documents_this_reads_actually_exist() -> None:
    """A document list that names a file nobody wrote makes the check above weaker.

    Every missing entry silently shrinks the corpus this searches, and the failure mode
    is the opposite of loud: the check goes on passing while looking at less.
    """
    missing = [name for name in DOCUMENTS if not (ROOT / name).exists()]
    assert not missing, (
        f"{missing} named as documentation and absent. Either the file moved -- in which "
        "case this list points at nothing and the tool census is reading a smaller corpus "
        "than it claims -- or it was deleted and this line should go with it."
    )


# ======================================================================================
# The same rule for the ten hand-run probes, which this check did not reach
# ======================================================================================
#: MEASURED: `tests/_support/` holds ten `*_probe.py` DICOM generators, nine with a
#: `__main__` block, and `git grep -l "_probe" -- '*.md'` returned NOTHING. Ten instruments
#: a person is meant to run by hand and no document named one of them.
#:
#: That is this module's own subject, one directory over: "a tool nobody documented is a
#: tool nobody can be told to run". The check above reads `medos/tools/` because that is
#: where the tools were when it was written, which is register entry 129's lesson in
#: miniature -- a gate sees the sites it was given.
#:
#: `tests/README.md` is the document, not the root set above: the probes are instruments of
#: the suite, and a reader looking for them is already reading about the suite.
PROBES = ROOT / "tests" / "_support"
SUITE_DOC = ROOT / "tests" / "README.md"


def _probes() -> list[Path]:
    return sorted(PROBES.glob("*_probe.py"))


def test_every_hand_run_probe_is_named_in_the_suites_own_readme() -> None:
    probes = _probes()
    assert len(probes) >= 10, (
        f"only {len(probes)} probe(s) found under tests/_support/; this check is reading "
        "the wrong directory and would pass for that reason alone"
    )
    prose = SUITE_DOC.read_text(encoding="utf-8")
    undocumented = [p.name for p in probes if p.name not in prose]
    assert not undocumented, (
        f"{len(undocumented)} of {len(probes)} hand-run probes are named nowhere in "
        f"tests/README.md: {undocumented}. Each generates a DICOM control for a rendering "
        "defect that a structural test cannot see; one nobody can find is one nobody runs."
    )


def test_the_readme_does_not_claim_the_probes_are_automated() -> None:
    """The inventory must not read as coverage, because none of them is executed.

    `MOS-IMG-158` permits a manual procedure and requires its output attached to the release
    record; that has not been done, and `docs/adr/BUILD_VS_ADOPT.md` records the automated
    rendering harness as owed. A table of ten instruments with no such sentence beside it
    would be read as ten checks.
    """
    prose = SUITE_DOC.read_text(encoding="utf-8")
    # Whitespace-normalised, because the sentence wraps and a literal match would be
    # asserting where the line breaks fall rather than what the document says.
    flat = " ".join(prose.split())
    assert "**No test imports or runs any of them.**" in flat, (
        "tests/README.md's probe section no longer states that nothing runs them, so the "
        "inventory now reads as coverage"
    )
    imported = [
        p.name for p in _probes()
        if f"_support import {p.stem}" in prose or f"_support.{p.stem}" in prose
    ]
    assert not imported, (
        f"tests/README.md shows {imported} being imported; if a probe is now executed by a "
        "test, say so and drop it from the hand-run table"
    )
