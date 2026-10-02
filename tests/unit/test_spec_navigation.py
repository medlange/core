# SPDX-License-Identifier: Apache-2.0
"""The specification has to be able to say true things about itself.

Measured before this check existed, on a tree nobody had touched for the purpose:

    chapter 19 (Operator Surfaces, 237 requirements) had NO header comment and NO
        navigation line at either end. Every other chapter had both. Chapter 18's footer
        ended at `[Index]` with nothing after it, so the chapter chain stopped at 18 and
        19 hung off nothing: a reader walking the document from chapter 1 never arrived,
        and the only way in was to type the filename.
    ten chapters carried the SAME footer block twice, four lines apart.
    chapter 16 carried a third, truncated footer stranded mid-file, left behind when
        "Questions raised by chapter 17" was appended after what used to be the end.
    all eighteen headers read "chapter N of 18". There are nineteen numbered chapters and
        MEDICALOS_SPEC.md's own table says "Chapters | 19" and lists all nineteen.
    chapter 4's header said 152 requirements; the index table said 151 and the requirement
        index holds 151 rows for chapter 4.
    the index's Size column was stale in all twenty rows -- 61 KB claimed for a file
        measuring 223 KB.

WHY THIS MATTERS MORE THAN TIDINESS. Chapter 19 is not decoration. Chapter 15's release
rows delegate to it by requirement id -- `MOS-UI-365` places operator-surface elements in
every release, `MOS-UI-370` adds checks to the 0.2.0 and 0.3.0 gate rows, `MOS-UI-371`
assigns their tiers -- and `tests/_support/release_criteria.py` classifies those elements
as release contents. A chapter carrying that weight was unreachable from the document's own
navigation and absent from its own metadata, and nothing in the repository could say so.

WHAT THIS ASSERTS. The document's statements ABOUT ITSELF: how many chapters there are,
where the next one is, how many requirements each chapter claims. Every one is checked
against another place the same fact is written, so the check fails on a disagreement rather
than on an opinion. It asserts nothing about any requirement's text -- that is the
requirement-ID review the headers demand, and it is a reviewer's job, not this file's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "spec"
INDEX = ROOT / "MEDICALOS_SPEC.md"

#: A navigation line: the one-line breadcrumb carrying the index link. Written as a line
#: shape rather than a substring, because "MEDICALOS_SPEC.md" also appears inside prose.
NAV = re.compile(r"^\[(?:← \d+\. |Index\]\(\.\./\.\./MEDICALOS_SPEC\.md\))")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
HEADER = re.compile(
    r"^<!-- MedicalOS Specification v(?P<version>[0-9.]+) — "
    r"chapter (?P<n>\d+) of (?P<total>\d+)\. Normative\.\s*\n"
    r"\s*(?P<count>\d+) requirements\."
)


def _chapters() -> dict[int, Path]:
    out: dict[int, Path] = {}
    for path in sorted(SPEC.glob("*.md")):
        m = re.match(r"^(\d\d)-", path.name)
        if m and int(m.group(1)) != 99:
            out[int(m.group(1))] = path
    return out


CHAPTERS = _chapters()
NUMBERS = sorted(CHAPTERS)
APPENDIX = SPEC / "99-known-inconsistencies.md"


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _lines(path: Path) -> list[str]:
    return _text(path).split("\n")


def _nav_positions(path: Path) -> list[int]:
    return [i for i, line in enumerate(_lines(path)) if NAV.match(line)]


def _index_rows() -> dict[int, tuple[str, str, int]]:
    """chapter number -> (title, href, stated requirement count), from the index table."""
    rows: dict[int, tuple[str, str, int]] = {}
    for line in _lines(INDEX):
        m = re.match(r"^\| (\d+) \| \[([^\]]+)\]\(([^)]+)\) \| (\d+) \|$", line)
        if m:
            rows[int(m.group(1))] = (m.group(2), m.group(3), int(m.group(4)))
    return rows


# ======================================================================================
# The header comment
# ======================================================================================
def test_every_chapter_carries_a_header_comment() -> None:
    missing = [p.name for p in CHAPTERS.values() if not HEADER.match(_text(p))]
    assert not missing, (
        f"{len(missing)} chapter(s) carry no header comment in the shape every other "
        f"chapter uses: {missing}. Chapter 19 was in this state -- 237 requirements, "
        "cited by chapter 15's release rows, with no version, no chapter number and no "
        "requirement count anywhere in the file."
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_each_header_names_its_own_chapter_number(number: int) -> None:
    m = HEADER.match(_text(CHAPTERS[number]))
    assert m is not None, f"{CHAPTERS[number].name} has no header comment"
    assert int(m.group("n")) == number, (
        f"{CHAPTERS[number].name} calls itself chapter {m.group('n')}"
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_each_header_counts_the_chapters_that_exist(number: int) -> None:
    m = HEADER.match(_text(CHAPTERS[number]))
    assert m is not None
    assert int(m.group("total")) == len(CHAPTERS), (
        f"{CHAPTERS[number].name} says there are {m.group('total')} chapters; "
        f"{len(CHAPTERS)} numbered chapter files exist in docs/spec/. Every header read "
        '"of 18" for as long as chapter 19 existed.'
    )


def test_every_header_states_the_same_specification_version() -> None:
    seen = {HEADER.match(_text(p)).group("version") for p in CHAPTERS.values()}
    assert len(seen) == 1, (
        f"chapters disagree about the specification version: {sorted(seen)}"
    )


# ======================================================================================
# Navigation
# ======================================================================================
@pytest.mark.parametrize("number", NUMBERS)
def test_each_chapter_has_exactly_two_navigation_lines(number: int) -> None:
    path = CHAPTERS[number]
    positions = _nav_positions(path)
    assert len(positions) == 2, (
        f"{path.name} carries {len(positions)} navigation line(s) at lines "
        f"{[i + 1 for i in positions]}; a chapter carries exactly two, one at each end. "
        "Ten chapters carried the footer block twice and chapter 16 carried a third one "
        "stranded mid-file."
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_the_navigation_sits_at_the_two_ends(number: int) -> None:
    path = CHAPTERS[number]
    lines = _lines(path)
    head, foot = _nav_positions(path)
    assert head < 12, (
        f"{path.name}'s first navigation line is at line {head + 1}, below the header"
    )
    last = max(i for i, line in enumerate(lines) if line.strip())
    assert foot == last, (
        f"{path.name}'s last navigation line is at line {foot + 1} but the file's last "
        f"non-blank line is {last + 1}: something was appended after the footer"
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_the_two_navigation_lines_are_identical(number: int) -> None:
    lines = _lines(CHAPTERS[number])
    head, foot = _nav_positions(CHAPTERS[number])
    assert lines[head] == lines[foot], (
        f"{CHAPTERS[number].name} offers different routes at its two ends:\n"
        f"  top:    {lines[head]}\n  bottom: {lines[foot]}"
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_the_chapter_chain_is_connected(number: int) -> None:
    """Chapter N's breadcrumb names N-1 and N+1, and both files exist."""
    path = CHAPTERS[number]
    nav = _lines(path)[_nav_positions(path)[0]]
    targets = [href for _, href in LINK.findall(nav)]
    want = []
    if number > min(NUMBERS):
        want.append(CHAPTERS[number - 1].name)
    want.append("../../MEDICALOS_SPEC.md")
    if number < max(NUMBERS):
        want.append(CHAPTERS[number + 1].name)
    assert targets == want, (
        f"{path.name} navigates to {targets}; the chain requires {want}. Chapter 18's "
        "breadcrumb ended at the index, so chapter 19 was reachable only by typing its "
        "filename."
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_every_navigation_target_resolves(number: int) -> None:
    path = CHAPTERS[number]
    nav = _lines(path)[_nav_positions(path)[0]]
    for label, href in LINK.findall(nav):
        assert (path.parent / href).resolve().exists(), (
            f"{path.name} links to {href!r} ({label}), which is not a file"
        )


@pytest.mark.parametrize("number", NUMBERS)
def test_each_breadcrumb_uses_the_neighbours_own_title(number: int) -> None:
    """A breadcrumb that names a chapter by a title it no longer has is a wrong label."""
    path = CHAPTERS[number]
    nav = _lines(path)[_nav_positions(path)[0]]
    for label, href in LINK.findall(nav):
        if href == "../../MEDICALOS_SPEC.md":
            continue
        neighbour = path.parent / href
        heading = next(
            line for line in _lines(neighbour) if line.startswith("## ")
        )[len("## "):].strip()
        shown = label.lstrip("← ").rstrip(" →").strip()
        assert shown == heading, (
            f"{path.name} calls {href} {shown!r}; that chapter's own heading reads "
            f"{heading!r}"
        )


# ======================================================================================
# MOS-CORE-033, which had no executable check and one violation
# ======================================================================================
@pytest.mark.parametrize("number", NUMBERS)
def test_each_chapter_ends_with_its_acceptance_criteria(number: int) -> None:
    """`MOS-CORE-033`: every chapter ends with a single `### Acceptance criteria`.

    Chapter 16 did not. Its acceptance criteria sat at line 601 with two sections after
    them -- "Questions raised by chapter 17" and "Questions raised by the AutoML section",
    both appended after what used to be the end of the file. The same append left a
    navigation footer stranded mid-chapter, which is how this was noticed at all: nothing
    in the repository asked either question.
    """
    sections = [
        line for line in _lines(CHAPTERS[number])
        if re.match(r"^#{2,3} ", line)
    ]
    found = [s for s in sections if s == "### Acceptance criteria"]
    assert len(found) == 1, (
        f"chapter {number} has {len(found)} `### Acceptance criteria` sections"
    )
    assert sections[-1] == "### Acceptance criteria", (
        f"chapter {number} ends with {sections[-1]!r}, not with its acceptance criteria"
    )


def test_the_appendix_links_back_to_the_index_at_both_ends() -> None:
    positions = _nav_positions(APPENDIX)
    lines = _lines(APPENDIX)
    assert len(positions) == 2, (
        f"{APPENDIX.name} carries {len(positions)} navigation line(s); it carried one, "
        "at the top only"
    )
    assert lines[positions[0]] == lines[positions[1]] == "[Index](../../MEDICALOS_SPEC.md)"


# ======================================================================================
# The index, which is the other place every one of these facts is written
# ======================================================================================
def test_the_index_lists_exactly_the_chapters_that_exist() -> None:
    rows = _index_rows()
    assert sorted(rows) == NUMBERS, (
        f"MEDICALOS_SPEC.md lists chapters {sorted(rows)}; docs/spec/ holds {NUMBERS}"
    )
    for number, (_, href, _) in rows.items():
        assert (ROOT / href).exists(), f"the index links chapter {number} to {href!r}"
        assert Path(href).name == CHAPTERS[number].name


def test_the_index_states_the_number_of_chapters_it_lists() -> None:
    stated = re.search(r"^\| Chapters \| (\d+) \|$", _text(INDEX), re.M)
    assert stated is not None, "MEDICALOS_SPEC.md no longer states a chapter count"
    assert int(stated.group(1)) == len(CHAPTERS), (
        f"the index says {stated.group(1)} chapters and lists {len(CHAPTERS)}"
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_the_index_and_the_header_agree_on_the_requirement_count(number: int) -> None:
    rows = _index_rows()
    header = int(HEADER.match(_text(CHAPTERS[number])).group("count"))
    assert header == rows[number][2], (
        f"chapter {number}: its header claims {header} requirements, the index table "
        f"claims {rows[number][2]}. These are the only two places the number is written "
        "as a number, so a disagreement means one of them was not updated."
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_the_requirement_index_holds_as_many_rows_as_the_header_claims(
    number: int,
) -> None:
    """The third place the number is written, and the only one written one row at a time.

    MEDICALOS_SPEC.md's requirement index carries one row per requirement, each citing its
    chapter as `[N](docs/spec/NN-....md)`. Counting those citations per chapter reproduces
    every header count exactly -- which is what settled chapter 4 at 151 against a header
    that said 152. The chapter table can be edited as a summary; this cannot, because a
    row is a requirement.
    """
    cited = len(re.findall(
        rf"\[{number}\]\(docs/spec/{number:02d}-[a-z0-9-]+\.md\)", _text(INDEX)
    ))
    header = int(HEADER.match(_text(CHAPTERS[number])).group("count"))
    assert cited == header, (
        f"chapter {number}: {cited} requirement rows cite it, its header claims {header}"
    )


@pytest.mark.parametrize("number", NUMBERS)
def test_the_index_uses_each_chapters_own_heading(number: int) -> None:
    rows = _index_rows()
    heading = next(
        line for line in _lines(CHAPTERS[number]) if line.startswith("## ")
    )[len("## "):].strip()
    assert heading == f"{number}. {rows[number][0]}", (
        f"the index calls chapter {number} {rows[number][0]!r}; the file's heading reads "
        f"{heading!r}"
    )


def test_the_docs_index_counts_the_chapters_too() -> None:
    """`docs/README.md`'s table is the third place the chapter count is written.

    It read "Twenty chapters plus the register" against nineteen chapter files and an index
    saying nineteen. Every count of the same thing in this repository disagreed with at
    least one other until they were all measured together.
    """
    text = (ROOT / "docs" / "README.md").read_bytes().decode("utf-8")
    words = {17: "Seventeen", 18: "Eighteen", 19: "Nineteen", 20: "Twenty",
             21: "Twenty-one"}
    want = f"{words[len(CHAPTERS)]} chapters plus the register"
    assert want in text, (
        f"docs/README.md does not read {want!r}; docs/spec/ holds {len(CHAPTERS)} "
        "numbered chapters"
    )


def test_the_index_carries_no_size_column() -> None:
    """A byte count is wrong on the next edit and nothing in this repository recomputes it.

    The column claimed 61 KB for a file measuring 223 KB and was stale in all twenty rows.
    Restoring it means either accepting a number that is false by construction or writing
    a check that goes red on every edit to the specification; neither is worth having, so
    the column is gone and this test is what keeps it gone.
    """
    header = next(line for line in _lines(INDEX) if line.startswith("| # | Chapter |"))
    assert "Size" not in header, (
        "the chapter table has a Size column again: " + header
    )
    stale = [
        line for line in _lines(INDEX)
        if re.match(r"^\| (?:\d+|—) \|", line) and re.search(r"\| \d+ KB \|", line)
    ]
    assert not stale, f"{len(stale)} index row(s) state a file size: {stale[:2]}"


# ---------------------------------------------------------------------------------------
# THE INDEX WAS COMPARED ONLY WITH ITSELF
# ---------------------------------------------------------------------------------------
#
# Three checks above assert that a chapter's header count, the Chapters table's count and the
# number of index rows citing that chapter all agree. All three are the index, or a number
# copied beside it. None of them was ever compared with the ids the chapter TEXT defines, and
# measured on 2026-09-26 that gap held **86 requirements defined in nine chapters with no index
# row at all** -- 39 of them in chapter 15, nearly a third of its own, and 15 in chapter 14
# including `MOS-TEST-004`, "the mutant rule", which register entry 127 quotes as normative.
#
# The index is the entry point a conformance reviewer uses to find a requirement, and it QUOTES
# each requirement rather than linking to it, so a missing row is a requirement that cannot be
# found from the front of the document.
#
# TWO POPULATION TRAPS, both of which bit before this check was right.
#
# `\*\*(MOS-[A-Z]+-\d+[a-z]?)\*\*` requires the id to be the whole bold span, and fifteen of
# chapter 14's requirements are written `**MOS-TEST-004 (the mutant rule)**`. With that pattern
# the chapter appeared to define 72, exactly matching the index, and the gap was invisible.
#
# Chapters 2 and 11 define requirements in TABLE ROWS rather than bold headings, so a
# bold-only matcher reported them as having MORE index rows than requirements -- 21 and 30
# "extra" -- which reads as the opposite defect. Both forms are matched here, and with both
# the count is exact in both directions: nothing defined lacks a row, nothing has a row
# without a definition.

#: A requirement defined in a chapter: a bold heading whose first token is the id, or a table
#: row whose first cell is. Anything narrower under-counts, and this module has the scars.
DEFINED_BOLD = re.compile(r"\*\*(MOS-[A-Z]+-\d+[a-z]?)\b[^*]*\*\*")
DEFINED_TABLE = re.compile(r"^\|\s*`?(MOS-[A-Z]+-\d+[a-z]?)`?\s*\|", re.M)
INDEX_ROW = re.compile(
    r"^\| `(MOS-[A-Z]+-\d+[a-z]?)` \| .*? \| \[(\d+)\]\(docs/spec/([^)]+)\) \|$", re.M
)


def _defined_per_chapter() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for chapter in sorted(SPEC.glob("[0-9][0-9]-*.md")):
        if chapter.name.startswith("99-"):
            continue  # the appendix is non-normative and defines no requirement
        body = _text(chapter)
        out[chapter.name] = set(DEFINED_BOLD.findall(body)) | set(DEFINED_TABLE.findall(body))
    return out


def _indexed_per_chapter() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for m in INDEX_ROW.finditer(_text(INDEX)):
        out.setdefault(m.group(3), set()).add(m.group(1))
    return out


def test_the_chapters_define_requirements_for_this_check_to_compare() -> None:
    """Both matchers must find something, or the comparison passes by finding nothing."""
    defined = _defined_per_chapter()
    assert sum(len(v) for v in defined.values()) > 2000, (
        "the definition matchers found almost nothing; a comparison over an empty population "
        "is not a check"
    )
    tables = {
        name for name, ids in defined.items()
        if DEFINED_TABLE.search(_text(SPEC / name))
    }
    assert tables, (
        "no chapter defines a requirement in a table row any more. If that is true, drop "
        "DEFINED_TABLE; while it is false, dropping it hides two chapters' requirements."
    )


def test_every_requirement_a_chapter_defines_has_an_index_row() -> None:
    defined, indexed = _defined_per_chapter(), _indexed_per_chapter()
    missing: list[str] = []
    for name, ids in defined.items():
        for rid in sorted(ids - indexed.get(name, set())):
            missing.append(f"{name}: {rid}")
    assert not missing, (
        f"{len(missing)} requirement(s) are defined in a chapter and absent from "
        f"MEDICALOS_SPEC.md's requirement index:\n  " + "\n  ".join(missing[:25])
        + ("\n  ..." if len(missing) > 25 else "")
        + "\nThe index quotes each requirement rather than linking to it, so a missing row is "
        "a requirement a reader cannot find from the front of the document. Add the row in the "
        "namespace section for its prefix, in id order, with the requirement's opening text "
        "truncated at a word boundary to 200 characters."
    )


def test_every_index_row_names_a_requirement_its_chapter_defines() -> None:
    """The other direction, and it is not symmetrical noise.

    A row whose requirement no longer exists is worse than a missing row: it answers a
    reviewer's search with text no chapter carries. Measured at 2,542 rows, there are none.
    """
    defined, indexed = _defined_per_chapter(), _indexed_per_chapter()
    orphans: list[str] = []
    for name, ids in indexed.items():
        if name.startswith("99-"):
            continue
        for rid in sorted(ids - defined.get(name, set())):
            orphans.append(f"{name}: {rid}")
    assert not orphans, (
        f"{len(orphans)} index row(s) cite a chapter that does not define them:\n  "
        + "\n  ".join(orphans[:25])
        + "\nEither the requirement was withdrawn and its row must go, or it was renumbered "
        "and the row must follow it."
    )
