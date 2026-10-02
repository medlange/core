# SPDX-License-Identifier: Apache-2.0
"""The release records were the one regulatory artifact nothing checked.

MEASURED: `docs/releases/` is opened at exactly ONE site in the whole test tree --
`tests/unit/test_gate_contract.py`'s `_cut_section`, reached only for an `INAPPLICABLE`
entry, and that module forbids an `INAPPLICABLE` entry for an unbuilt release. So no test
has ever read a record's contents, and creating a new one breaks nothing.

THE ASYMMETRY IS THE DEFECT. `test_gate_contract.py` mechanically FORBIDS a premature gate
module: a `tests/gate/test_unattended_arrival.py` added before 0.4.0 is claimed turns the
suite red at two separate assertions, on the reasoning that it "is a check nobody agreed to
gate a release on". The mirror image -- a premature release RECORD, a
`docs/releases/0.4.0.md` written before release 0.4.0 happened -- was mechanically ignored.
Of the two, the record is the one a notified-body assessor reads: four files in
`docs/releases/`, three of them ending in an Approval table with a name and a date, read as
four releases whatever any individual cell inside the fourth says. This module is that
mirror.

WHAT IT DOES NOT DO. It does not check a record's content against today's tree, and it must
not. `docs/README.md` §"Release records are not to be edited" is the rule, and the reason is
recorded there: twice in one day a mechanical path rewrite ran through these files and
turned evidence into an address. The 0.3.0 record lists `ohif/app:v3.9.2` and the compose
file now runs `nginx:1.27-alpine`; that row is a TRUE statement about 0.3.0. Every record's
`| Specification | v0.2.0 |` cell is true of the version in force when it was written. A
check that compared either against HEAD would be demanding that history be rewritten.

So this module checks only what is invariant: which records may exist, that each names
itself consistently, that none claims a tag while `git tag` is empty, and that
`docs/releases/README.md` -- which is NOT a record and is written in the present tense --
still describes the repository it is in.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests._support import release_criteria
from tests.unit.test_gate_contract import IMPLEMENTED, NOT_YET

REPO = Path(__file__).resolve().parents[2]
RELEASES = REPO / "docs" / "releases"
README = RELEASES / "README.md"
COMPOSE = REPO / "medos" / "deploy" / "compose" / "docker-compose.yml"

#: Images whose registry namespace is this project's. Everything else is third-party.
FIRST_PARTY_PREFIX = "medicalos/"


def _records() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for path in sorted(RELEASES.glob("*.md")):
        if re.fullmatch(r"\d+\.\d+\.\d+", path.stem):
            out[path.stem] = path
    return out


RECORDS = _records()


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _compose_images() -> list[str]:
    doc = yaml.safe_load(_text(COMPOSE))
    return [
        svc["image"]
        for svc in doc.get("services", {}).values()
        if isinstance(svc, dict) and "image" in svc
    ]


# ======================================================================================
# Which records may exist. This is the mirror of test_gate_contract.py's rule that an
# unbuilt release's checks may have no modules.
# ======================================================================================
@pytest.mark.parametrize("release", sorted(NOT_YET))
def test_an_unbuilt_release_has_no_record(release: str) -> None:
    """A record for a release that has not happened is a claim that it has.

    `MOS-REL-003`'s six fields all presuppose a tag: the tag, the digests at it, the gate
    results at it, the items cut at it, the ids newly satisfied in its range, and the human
    who approved it. `git tag` is empty. `MOS-REL-004` does not merely leave the release
    untagged, it forbids the tag while any check in the row is red -- and 0.4.0's row is red
    by construction, because `tests/unit/test_gate_contract.py` declares the release
    `NOT_YET` and asserts none of its checks may have a module.

    If this test fails, the answer is not to delete the assertion. Either the release was
    built -- in which case move it into `IMPLEMENTED` there, with its marker and its
    modules, and this test stops applying -- or the file is a decision record about the
    specification, which belongs beside `docs/adr/BUILD_VS_ADOPT.md` under a name that is
    not a version number.
    """
    assert release not in RECORDS, (
        f"docs/releases/{release}.md exists, but {release} is declared NOT_YET by "
        f"tests/unit/test_gate_contract.py: none of its gate checks has a module, and none "
        f"may have one. A file at this path is read as a release that happened."
    )


@pytest.mark.parametrize("release", sorted(IMPLEMENTED))
def test_every_built_release_has_a_record(release: str) -> None:
    assert release in RECORDS, (
        f"{release} is declared IMPLEMENTED and has no record. `MOS-REL-003` requires one "
        f"per release, committed before the tag is pushed."
    )


def test_the_records_are_exactly_the_built_releases() -> None:
    assert sorted(RECORDS) == sorted(IMPLEMENTED), (
        f"docs/releases/ holds records for {sorted(RECORDS)}; the repository declares "
        f"{sorted(IMPLEMENTED)} built and {sorted(NOT_YET)} not yet built"
    )


def test_every_release_row_of_the_delivery_chapter_is_accounted_for() -> None:
    """No release row may be silently recordless: it is either built or declared unbuilt."""
    declared = set(IMPLEMENTED) | set(NOT_YET)
    assert set(release_criteria.RELEASES) <= declared, (
        f"§15.1.2 defines rows {sorted(release_criteria.RELEASES)}; "
        f"{sorted(set(release_criteria.RELEASES) - declared)} is neither IMPLEMENTED nor "
        "NOT_YET, so nothing decides whether it owes a record"
    )


# ======================================================================================
# What a record says about itself. Invariants only -- nothing compared against HEAD.
# ======================================================================================
@pytest.mark.parametrize("release", sorted(RECORDS))
def test_each_record_names_its_own_version(release: str) -> None:
    cell = re.search(r"^\| Version \| \*\*([0-9.]+)\*\* \|$", _text(RECORDS[release]), re.M)
    assert cell is not None, f"{RECORDS[release].name} has no `| Version |` cell"
    assert cell.group(1) == release, (
        f"{RECORDS[release].name} calls itself version {cell.group(1)}"
    )


@pytest.mark.parametrize("release", sorted(RECORDS))
def test_no_record_claims_a_tag_that_does_not_exist(release: str) -> None:
    """`MOS-REL-003` carries the tag; `git tag` is empty, so every record must say so.

    This is the one content check worth having against an immutable file, because it is the
    field that would become false without anybody editing the record: the day someone runs
    `git tag -a 0.3.0`, a record reading "*(not pushed)*" stops being history and starts
    being wrong. The test goes red then, which is the moment to write the amendment.
    """
    tags = subprocess.run(
        ["git", "tag"], cwd=REPO, capture_output=True, text=True,
    ).stdout.split()
    text = _text(RECORDS[release])
    cell = re.search(r"^\| Tag \| (.+?) \|$", text, re.M)
    assert cell is not None, f"{RECORDS[release].name} has no `| Tag |` cell"
    if release in tags:
        assert release in cell.group(1), (
            f"tag {release} exists but {RECORDS[release].name} does not name it"
        )
    else:
        assert "not pushed" in cell.group(1), (
            f"{RECORDS[release].name} states a tag ({cell.group(1)}) that `git tag` does "
            f"not list. Tags present: {tags or 'none'}"
        )


@pytest.mark.parametrize("release", sorted(RECORDS))
def test_each_record_carries_an_approval_section(release: str) -> None:
    text = _text(RECORDS[release])
    assert re.search(r"^## Approval$", text, re.M), (
        f"{RECORDS[release].name} has no Approval section; `MOS-REL-003` requires the "
        "named human who approved the tag"
    )
    assert re.search(r"^\| \*\*Approved by\*\* \| .+ \|$", text, re.M), (
        f"{RECORDS[release].name}'s Approval section names nobody"
    )


# ======================================================================================
# docs/releases/README.md, which is NOT a record and IS written in the present tense.
# ======================================================================================
def test_the_readme_names_every_record_that_exists() -> None:
    text = _text(README)
    missing = [r for r in RECORDS if not re.search(rf"\b{re.escape(r)}\b", text)]
    assert not missing, (
        f"the README does not name the record(s) for {missing}, so a reader arriving at "
        "docs/releases/ is told about fewer releases than the directory holds"
    )


def test_the_readme_does_not_promise_a_record_for_an_unbuilt_release() -> None:
    """It used to. The sentence was an inference, and the inference was wrong.

    "`docs/spec/01-overview.md` reads 'Document version: 0.4.0', so a fourth record is
    OWED" -- every step checkable, and the middle one false: `MOS-REL-003`'s subject is a
    release, and release 0.4.0 has not happened. `MOS-CORE-036`, which governs the only
    substantive decision taken at document version 0.4.0, asks for a version increment and
    a recorded decision naming the withdrawn requirement id. Both already hold, and it does
    not ask for a release record.

    THE FIRST VERSION OF THIS TEST FORBADE THE SUBSTRING, and it went red against the
    paragraph that RETRACTS the claim -- a check cannot tell a quotation from an assertion,
    and forbidding the words would have forced the correction to be written without naming
    what it corrects. So it asks at the site instead: the status HEADING is where the
    repository counts its releases, and the body must state the absence rather than promise
    a filling.
    """
    text = _text(README)
    heading = next(
        line for line in text.split("\n") if line.startswith("## Status:")
    )
    counted = re.findall(r"\b(one|two|three|four|five|six)\b", heading)
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
    assert counted, f"the status heading states no count: {heading!r}"
    for word in counted:
        assert words[word] == len(RECORDS), (
            f"the status heading says {word!r}; docs/releases/ holds {len(RECORDS)} "
            f"record(s): {sorted(RECORDS)}. The heading read 'three records for four "
            "releases' while counting a release that had not happened."
        )
    for release in sorted(NOT_YET):
        assert re.search(rf"^### There is no {re.escape(release)} record", text, re.M), (
            f"the README does not say, in a heading a reader will find, that no {release} "
            "record exists and why. An absence explained nowhere reads as an omission."
        )


def test_the_readme_splits_the_images_the_way_the_compose_file_does() -> None:
    """The README used to say "five of the seven images … the two MedicalOS images".

    Measured: nine distinct images across fifteen `image:` declarations, five third-party
    and four first-party. The numbers are gone from the prose rather than corrected,
    because a count written in prose is wrong again the next time a service is added. This
    test carries the split instead, where a change to the compose file can reach it.
    """
    images = _compose_images()
    distinct = sorted(set(images))
    first = [i for i in distinct if i.startswith(FIRST_PARTY_PREFIX)]
    third = [i for i in distinct if not i.startswith(FIRST_PARTY_PREFIX)]
    assert first and third, f"the compose file declares {distinct}"

    text = _text(README)
    assert "Five of the seven images" not in text, (
        f"the README still counts seven images; the compose file declares "
        f"{len(distinct)} distinct across {len(images)} declarations"
    )
    assert "The two MedicalOS images" not in text, (
        f"the README still counts two MedicalOS images; there are {len(first)}: {first}"
    )
    assert "**Publishing the MedicalOS images to a registry is a precondition" in text, (
        "the README no longer records that the first-party images are unpublished, which "
        "is the open condition on all three records"
    )


def test_the_readme_describes_the_coverage_register_that_exists_now() -> None:
    """It used to say the register "exists for 0.1.0 only". It classifies all four rows."""
    text = _text(README)
    assert "That register exists for" not in text and "exists for\n0.1.0 only" not in text

    counts = {r: len(release_criteria.for_release(r)) for r in release_criteria.RELEASES}
    assert counts["0.1.0"] > 0 and all(v > 0 for v in counts.values()), (
        f"the register classifies {counts}; the README's replacement paragraph claims "
        "every row is classified at least in part"
    )
    for release, n in counts.items():
        if release == "0.1.0":
            continue
        assert str(n) in text, (
            f"the README states no count for {release}; the register classifies {n} "
            "criteria for it, and the paragraph exists to be checkable"
        )


def test_the_readme_points_at_this_module() -> None:
    """A rule stated in prose and enforced nowhere is what this whole module is about."""
    assert "tests/unit/test_release_records.py" in _text(README), (
        "the README describes the no-premature-record rule without naming the file that "
        "enforces it, which is the shape of a claim that quietly stops being true"
    )


# ======================================================================================
# CHANGELOG.md, which is a VIEW over these records and must not become a second source
# ======================================================================================
#
# `MOS-REL-095` requires `CHANGELOG.md` in Keep-a-Changelog format, one section per release.
# Written 2026-09-26, and the reason it is a view rather than an independent document is the
# whole risk: two places stating what shipped in 0.2.0 is two places to correct, and only one of
# them is the artefact `MOS-REL-003` gates the tag on. So the sections are held to the records.
#
# The three checks below are the same shape as the ones above: a section for every record, no
# section for a release the repository declares NOT_YET -- which is the mistake register
# entry 125
# records about the record itself -- and every version heading carrying the date its record was
# approved rather than a date invented here.

CHANGELOG = REPO / "CHANGELOG.md"

#: `## [0.2.0] - approved 2026-09-17, not tagged`, with either dash.
CHANGELOG_SECTION = re.compile(
    r"^## \[(\d+\.\d+\.\d+)\][^\n]*?approved (\d{4}-\d{2}-\d{2})", re.M
)


def _changelog_sections() -> dict[str, str]:
    return {m.group(1): m.group(2) for m in CHANGELOG_SECTION.finditer(_text(CHANGELOG))}


def _approval_date(release: str) -> str | None:
    m = re.search(r"^\| \*\*Date\*\* \| (\d{4}-\d{2}-\d{2}) \|$", _text(RECORDS[release]), re.M)
    return m.group(1) if m else None


def test_the_changelog_has_a_section_for_every_record_and_no_others() -> None:
    sections = _changelog_sections()
    assert set(sections) == set(RECORDS), (
        f"CHANGELOG.md has sections for {sorted(sections)} and docs/releases/ holds "
        f"records for "
        f"{sorted(RECORDS)}. A section with no record states what shipped on no authority; a "
        f"record with no section is a release an upgrader cannot find from the changelog."
    )


@pytest.mark.parametrize("release", sorted(NOT_YET))
def test_the_changelog_has_no_section_for_an_unbuilt_release(release: str) -> None:
    """The mirror of `test_an_unbuilt_release_has_no_record`, one document over.

    A `## [0.4.0]` heading reads as a release that happened, exactly as a file at
    `docs/releases/0.4.0.md` does, and `MOS-REL-004` forbids the tag rather than merely
    withholding it. The 0.4.0 block belongs under `[Unreleased]`.
    """
    assert release not in _changelog_sections(), (
        f"CHANGELOG.md carries a section for {release}, which this repository declares "
        f"NOT_YET: "
        f"none of its gate checks has a module and none may have one. Put it under "
        f"`[Unreleased]`."
    )
    assert "## [Unreleased]" in _text(CHANGELOG), (
        "CHANGELOG.md has no `[Unreleased]` section, so there is nowhere for the unbuilt block "
        "to be described without reading as a release"
    )


@pytest.mark.parametrize("release", sorted(IMPLEMENTED))
def test_each_changelog_section_carries_the_date_its_record_was_approved(release: str) -> None:
    """A date invented here would be the only unsourced fact in the file."""
    stated = _changelog_sections().get(release)
    assert stated, f"CHANGELOG.md has no dated section for {release}"
    recorded = _approval_date(release)
    assert recorded, (
        f"docs/releases/{release}.md carries no `| **Date** |` row, so the changelog's "
        f"date has "
        f"nothing to rest on"
    )
    assert stated == recorded, (
        f"CHANGELOG.md dates {release} {stated}; its record says {recorded}. The changelog "
        f"quotes the record and does not date a release on its own authority."
    )


# ======================================================================================
# Chapter 15's own acceptance criteria, against the tree
# ======================================================================================
#
# §15.9.3's "Acceptance criteria" list says every check is "executable by a CI job or by a
# reviewer following a written procedure". MEASURED while writing the changelog: two of the 22
# name a path that does not exist, so a reviewer cannot execute them at all -- and `MOS-REL-012`
# makes an unexecuted acceptance criterion equivalent to an unsatisfied requirement.
#
# Criterion 5 is the sharp one. It reads "`docs/releases/spike-week0.md` exists", as a checkable
# fact, and the file is absent. `MOS-REL-014` requires that record. Nothing in the register, the
# test tree or this directory's own status page said so: measured, `spike-week0` occurred in the
# specification and the index and nowhere else. An unmet MUST recorded nowhere is
# indistinguishable from a satisfied one, which is the argument entry 126 makes for the frozen
# table shape used here.

#: Paths a §15 acceptance criterion names that do not exist, each with why it cannot
#: be executed.
#: FROZEN and asserted in both directions: a third one appearing is news, and so is one of these
#: being written.
CRITERIA_PATHS_ABSENT: dict[str, str] = {
    "docs/releases/spike-week0.md": (
        "criterion 5 asserts this document 'exists'. `MOS-REL-014` requires it -- two viewer "
        "observations with screenshots, the DICOM battery green in CI, a selector error count "
        "with a per-error cause -- and it was never written. Register entry 149."
    ),
    "docs/api/openapi.yaml": (
        "criterion 11 deletes a line from it to prove `make verify-generated` fails. It is a "
        "GENERATED document whose generator (`cmd/apigen`, `MOS-API-086`) does not exist, and "
        "its absence is already held by tests/unit/test_required_documents.py's MISSING table; "
        "what is NOT held elsewhere is that this criterion cannot be executed either."
    ),
}

CRITERION_PATH = re.compile(
    r"`((?:docs|tests|medos|acceptance|\.ci|\.github|data|internal|cmd|sdk)/[\w./-]+)`"
)


def _acceptance_criteria() -> list[tuple[str, str]]:
    chapter = _text(REPO / "docs" / "spec" / "15-delivery.md")
    block = re.search(r"^### Acceptance criteria\n(.*?)(?=^#{1,4} |\Z)", chapter, re.M | re.S)
    assert block, "chapter 15 no longer has an Acceptance criteria section"
    return re.findall(r"^(\d+)\. (.*)$", block.group(1), re.M)


def test_there_are_acceptance_criteria_naming_paths_to_check() -> None:
    criteria = _acceptance_criteria()
    assert len(criteria) > 15, f"only {len(criteria)} criteria parsed; the list shape changed"
    named = {p for _, text in criteria for p in CRITERION_PATH.findall(text)}
    assert named, "no criterion names a repository path any more, so the check below is vacuous"


def test_every_path_a_chapter_15_criterion_names_exists_or_is_declared() -> None:
    absent: dict[str, list[str]] = {}
    for number, text in _acceptance_criteria():
        for path in sorted(set(CRITERION_PATH.findall(text))):
            if not (REPO / path).exists():
                absent.setdefault(path, []).append(number)
    undeclared = sorted(set(absent) - set(CRITERIA_PATHS_ABSENT))
    assert not undeclared, (
        f"§15's acceptance criteria name {undeclared}, which do not exist, and this "
        f"module says "
        f"nothing about them: {  {p: absent[p] for p in undeclared} }. `MOS-REL-012` makes an "
        f"unexecuted criterion equivalent to an unsatisfied requirement, so an absent "
        f"path here "
        f"is a requirement nobody can check. Declare it with the reason, or write the document."
    )
    stale = sorted(set(CRITERIA_PATHS_ABSENT) - set(absent))
    assert not stale, (
        f"{stale} are declared absent and are now present, or no criterion names them "
        f"any more. "
        f"Delete the rows and close their register entries in the same change."
    )
