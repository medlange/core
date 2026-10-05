# SPDX-License-Identifier: Apache-2.0
"""The documents an adopter needs, and the decisions nobody has made yet.

`MOS-REL-095`'s table has **16** rows that MUST exist -- 14 paths and 2 filename patterns
(`docs/adr/NNNN-<slug>.md`, `docs/releases/<version>.md`) -- and requires CI to fail when one
is absent. **6** of the 14 paths do not exist. (It was seven until `CHANGELOG.md` was written on
2026-09-26; its row came out of `MISSING` in the same commit, which the set equality below
requires.) This module does NOT simply assert all of them
and go red, and the reason is the same one that runs through this repository: a check that is
permanently red is a check people learn to scroll past, and then it cannot report the day
something real breaks.

THOSE TWO NUMBERS WERE WRONG HERE, in the module whose subject is whether the documentation is
complete. It said seventeen documents and fourteen absent; measured, the table has sixteen rows
and seven paths are absent. They are derived now rather than written -- see
`test_this_modules_own_counts_are_derived_from_the_specification` at the foot of the file.

So the absent documents are ENUMERATED. `MISSING` below is the list of what is not written
yet, and the test asserts the set of absent documents is EXACTLY that list. Writing one
without removing it from `MISSING` goes red. Losing one that exists goes red. Adding a new
required document goes red. What cannot happen is the set quietly drifting while the suite
stays green, which is what a bare `assert all exist` would do the moment somebody added a
skip to get past it.

THE SAME SHAPE FOR THE OPEN DECISIONS. `SECURITY.md` and `CODE_OF_CONDUCT.md` are written
but carry `UNRESOLVED` markers where a human has to decide something -- a reporting
channel, a response commitment, an enforcement contact. Those are not engineering
questions. A security policy with a plausible but unmonitored address is worse than an
absent one, because the reporter believes they have discharged their duty and nobody is
reading. So the markers stay, and `OPEN_DECISIONS` enumerates them: a new one that nobody
declared turns this red, and so does resolving one without deleting its row.

Spec: MOS-REL-095, MOS-REL-097, MOS-REL-099, MOS-REL-100, MOS-SAFE-001.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: MOS-REL-095's table, transcribed. The paths, in its order.
REQUIRED: tuple[str, ...] = (
    "README.md",
    "ARCHITECTURE.md",
    "DEVELOPMENT.md",
    "DEPLOYMENT.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "GOVERNANCE.md",
    "CHANGELOG.md",
    "MIGRATIONS.md",
    "docs/api/openapi.yaml",
    "docs/dicom/CONFORMANCE.md",
    "docs/evidence/VALIDATION_REPORT.md",
    "docs/services/AUTHORING.md",
)

#: Of those, the ones not written yet -- each with what an adopter cannot do without it.
#:
#: This is a debt register, not an excuse. Every row is a thing somebody evaluating this
#: platform will look for and not find, and the entry says what that costs them.
MISSING: dict[str, str] = {
    "DEPLOYMENT.md": (
        "the configuration reference exists only as comments in docker-compose.yml, and "
        "`python -m medos.cli doctor` is currently the only way to see which declarations "
        "a deployment is missing"
    ),
    "GOVERNANCE.md": (
        "nobody can tell who decides, or how a decision is recorded, before depending on "
        "the project"
    ),
    "MIGRATIONS.md": (
        "fourteen migrations exist -- 0002 through 0015, there is no 0001 -- and not one "
        "of them says whether it is breaking or what the upgrade action is. MEASURED: no "
        "file under medos/medos/db/migrations/ contains the word breaking in any case"
    ),
    "docs/api/openapi.yaml": (
        "GENERATED document. The API is served and self-describing at /docs, but there is "
        "no checked-in artefact to diff across releases"
    ),
    "docs/dicom/CONFORMANCE.md": (
        "a site cannot check MedicalOS against its own PACS without a conformance "
        "statement, and MOS-EVID-129 SAT-3 requires exactly that check"
    ),
    "docs/evidence/VALIDATION_REPORT.md": (
        "the FORMAT is specified nowhere a reader can find before they hold a bundle. The "
        "PROCEDURE is documented, and this row used to say it was not: medos/medos/evidence/"
        "bundle.py prints MOS-EVID-123's seven checks and their exit codes into every "
        "exported bundle's README.txt. So the gap is narrower than it read -- an adopter "
        "deciding whether to trust the artefact cannot read the format first, though anyone "
        "holding one can follow the steps"
    ),
}

#: Decisions that are a human's to make, not an engineer's. Each is marked `UNRESOLVED`
#: in its document, and the marker is deliberately left in place rather than filled with
#: something plausible.
OPEN_DECISIONS: dict[str, tuple[str, ...]] = {
    "SECURITY.md": (
        "the reporting channel",
        "the response commitment against MOS-SEC-143's 7/30/90 budget",
        "the supported-version window",
    ),
    "CODE_OF_CONDUCT.md": ("the enforcement contact",),
    "CONTRIBUTING.md": ("OQ-19: DCO or CLA",),
}

#: MOS-SAFE-001, verbatim. Any drift from this string is a drift in what the project
#: claims about itself, which is the one sentence that must not paraphrase.
POSITIONING = (
    "MedicalOS integrates, governs and evidences medical AI services. It does not "
    "diagnose, does not replace a PACS, and is not itself a medical device."
)

#: MOS-REL-097: words the documentation set may not use about the platform itself.
FORBIDDEN = ("clinically validated", "clinical validation of", "FDA cleared", "CE marked")


def _text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------------------
# the documents
# --------------------------------------------------------------------------------------


def test_the_set_of_absent_documents_is_exactly_the_declared_debt() -> None:
    """Enumerated rather than asserted, so the list cannot drift while the suite is green.

    Writing one of these and forgetting to remove its row fails here, which is the
    intended annoyance: the register is only useful while it is true.
    """
    absent = {path for path in REQUIRED if not (REPO / path).exists()}
    declared = set(MISSING)
    newly_missing = absent - declared
    newly_written = declared - absent
    assert not newly_missing, (
        f"required documents are absent and not declared in MISSING: {sorted(newly_missing)}. "
        f"MOS-REL-095 requires them; add the row with what an adopter cannot do without it, "
        f"or write the document."
    )
    assert not newly_written, (
        f"these are declared MISSING but now exist: {sorted(newly_written)}. Delete their "
        f"rows -- a debt register that lists paid debts stops being read."
    )


def test_every_missing_document_says_what_it_costs() -> None:
    """A row with no consequence is a row nobody prioritises."""
    for path, cost in MISSING.items():
        assert path in REQUIRED, f"{path} is declared missing but is not required"
        assert len(cost.split()) >= 10, f"{path}: the cost is too short to be one"


def test_the_licence_is_present_and_is_apache_2() -> None:
    """MOS-REL-099. The README claimed Apache-2.0 for the life of this repository while no
    LICENSE file existed, which is both a credibility problem and a real one: a permissive
    claim with no licence text grants nothing."""
    assert (REPO / "LICENSE").exists(), (
        "MOS-REL-099 requires Apache-2.0 and there is no LICENSE"
    )
    text = _text("LICENSE")
    assert "Apache License" in text and "Version 2.0" in text
    assert "http://www.apache.org/licenses/" in text


# --------------------------------------------------------------------------------------
# the open decisions
# --------------------------------------------------------------------------------------


def test_every_unresolved_marker_is_a_declared_decision() -> None:
    """An `UNRESOLVED` marker nobody enumerated is a hole somebody left, not one somebody
    chose to leave. The difference is whether a reader can find out that it is open."""
    for path, decisions in OPEN_DECISIONS.items():
        if not (REPO / path).exists():
            continue
        found = _text(path).count("UNRESOLVED")
        assert found == len(decisions), (
            f"{path} carries {found} UNRESOLVED marker(s) and OPEN_DECISIONS declares "
            f"{len(decisions)}: {list(decisions)}.\n"
            f"If a decision was made, delete its marker AND its row here. If a new one "
            f"was found, declare it. The two must agree or this register is decoration."
        )


def test_a_resolved_document_does_not_keep_its_row() -> None:
    """The other direction: a document with no markers left must not still be listed."""
    for path, decisions in OPEN_DECISIONS.items():
        if (REPO / path).exists() and "UNRESOLVED" not in _text(path):
            pytest.fail(
                f"{path} has no UNRESOLVED markers left but OPEN_DECISIONS still lists "
                f"{list(decisions)}. Remove the row."
            )


# --------------------------------------------------------------------------------------
# what the documents may and may not say
# --------------------------------------------------------------------------------------


def test_the_readme_carries_the_positioning_statement_verbatim() -> None:
    """MOS-SAFE-001 requires this sentence unchanged in the README, every release
    artefact, the API landing document and the web UI footer. Verbatim, because a
    paraphrase of "is not itself a medical device" is how a platform ends up implying it
    is one."""
    readme = re.sub(r"[*_>`\s]+", " ", _text("README.md"))
    expected = re.sub(r"\s+", " ", POSITIONING)
    assert expected in readme, (
        "README.md does not carry MOS-SAFE-001's statement verbatim. It reads:\n"
        f"  {POSITIONING}"
    )


@pytest.mark.parametrize("path", ["README.md", "SECURITY.md", "CONTRIBUTING.md"])
def test_no_document_claims_clearance_the_project_does_not_have(path: str) -> None:
    """MOS-REL-097 and MOS-SEC-007. The project publishes software; it does not publish a
    device, and it does not provide HIPAA, GDPR or MDR *compliance* -- only controls a
    covered entity can use inside its own programme."""
    if not (REPO / path).exists():
        pytest.skip(f"{path} is in the declared debt register")
    lowered = _text(path).lower()
    for phrase in FORBIDDEN:
        assert phrase.lower() not in lowered, f"{path} contains {phrase!r}"


def test_contributing_carries_the_sign_off_regime() -> None:
    """MOS-REL-095's CI check for this document is literally "contains a sign-off section"."""
    text = _text("CONTRIBUTING.md")
    assert "Signed-off-by" in text
    assert "git commit -s" in text
    assert "developercertificate.org" in text


# --------------------------------------------------------------------------------------
# MOS-REL-100: every first-party source file carries the identifier
# --------------------------------------------------------------------------------------

#: Trees excluded from the SPDX requirement, each with the reason.
#:
#: `third_party/` is not ours to mark -- MOS-REL-101 requires the upstream LICENSE to
#: travel with it instead. `spikes/` was the other one, exempt because it was frozen
#: byte-identical by CONTRACT.md section 2 and a licence header would have been the first
#: edit to it; it is deleted, so the hole closes rather than being justified again.
SPDX_EXEMPT_TREES = ("third_party/",)


def _tracked_python() -> list[str]:
    import subprocess

    # UNTRACKED FILES COUNT. `MOS-REL-100` is read by licence scanners, and a
    # marking has to be there BEFORE the file is committed -- which is the moment
    # the author can still add it for free. Reading only the index meant a new
    # source was unmarked-and-unchecked for exactly as long as it was easy to fix.
    # Measured when this changed: eight untracked `.py` existed and all eight
    # already carried the line, so nothing was hiding here -- the gap was.
    out = subprocess.run(["git", "ls-files", "--cached", "--others",
                          "--exclude-standard", "*.py"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    # FILES MUST EXIST TO CARRY A HEADER: the index still lists paths whose deletion
    # is part of an in-flight change, and a deleted file has no first three lines to
    # read. The deletion itself is recorded by the commit; this check reads bytes.
    return [
        p for p in out.stdout.split()
        if not p.startswith(SPDX_EXEMPT_TREES) and (REPO / p).is_file()
    ]


def test_every_first_party_source_file_carries_the_spdx_identifier() -> None:
    """MOS-REL-100, and it is not decoration.

    Licence scanners read this line per file. A repository claiming Apache-2.0 in its
    README while shipping unmarked sources is one whose licence a downstream compliance
    tool cannot establish -- which for an adopter in a regulated setting is a blocker
    discovered late, by their lawyers, rather than early by them.

    Checked in the FIRST THREE LINES specifically. Below that it is still a valid marker
    to a scanner but invisible to the cheap grep this check is, and a rule that cannot be
    checked cheaply is one that decays.
    """
    missing = []
    for path in _tracked_python():
        head = "".join((REPO / path).read_text(encoding="utf-8", errors="replace")
                       .splitlines(keepends=True)[:3])
        if "SPDX-License-Identifier: Apache-2.0" not in head:
            missing.append(path)
    assert not missing, (
        f"{len(missing)} first-party source file(s) carry no SPDX identifier in their "
        f"first three lines: {missing[:8]}{' ...' if len(missing) > 8 else ''}"
    )


def test_the_spdx_exemptions_are_narrow_and_reasoned() -> None:
    """An exemption list is a hole; this asserts it stays a small one."""
    assert set(SPDX_EXEMPT_TREES) == {"third_party/"}, (
        "a tree was exempted from MOS-REL-100. Every first-party file must carry the "
        "identifier; the only defensible exemption left is code that is not ours to mark. "
        "The other one -- `spikes/`, frozen by CONTRACT.md -- closed when the spike was "
        "deleted, and re-opening a closed exemption needs more than a new tree name."
    )



# ======================================================================================
# The counts in this module's own prose, derived rather than trusted
# ======================================================================================
#
# This module's mechanism derives everything: the absent set is compared by set equality, the
# open decisions by marker. Its PROSE carried three counts, and three of them were wrong --
# "seventeen documents" for a 16-row table, "fourteen absent" for seven, and "sixteen
# migrations" for fourteen. A count written beside a mechanism that derives everything else is
# the liability register entry 140 records, and the module whose subject is whether the
# documentation is complete is a poor place for it.

#: Number words this file uses, so the derivation reads either form.
_WORDS = {
    2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
    9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen",
}


def nl_blank() -> str:
    """The paragraph separator, whichever line ending this module was written with."""
    doc = __doc__ or ""
    return (chr(13) + chr(10)) * 2 if chr(13) + chr(10) in doc else chr(10) * 2


def _states(text: str, value: int) -> bool:
    """`text` states `value`, as a digit or as this file's own number word."""
    word = _WORDS.get(value, "")
    return bool(
        re.search(rf"\*?\*?{value}\*?\*?\b", text)
        or (word and re.search(rf"\b{word}\b", text, re.I))
    )


def test_this_modules_own_counts_are_derived_from_the_specification() -> None:
    import tests.unit.test_required_documents as me  # noqa: PLC0415

    # THE FIRST PARAGRAPH ONLY, and that is not tidiness either. The paragraph after it
    # RECORDS the correction -- "it said seventeen documents and fourteen absent; measured, the
    # table has sixteen rows and seven paths are absent" -- so a search over the whole docstring
    # is satisfied by the sentence describing the defect. Caught before this check was proved:
    # changing the obligation sentence back to seventeen left it GREEN, because the correction
    # two paragraphs down still said sixteen. Ask for the construct at its own site.
    paragraphs = (me.__doc__ or "").strip().split(nl_blank())
    stating = [q for q in paragraphs if "MOS-REL-095" in q and "rows" in q]
    assert len(stating) == 1, (
        f"{len(stating)} paragraphs of this docstring state MOS-REL-095's table size; this "
        "check needs exactly one to ask at, because a second one describing the old wrong "
        "number would satisfy a search over the whole docstring"
    )
    prose = stating[0]
    spec = (REPO / "docs" / "spec" / "15-delivery.md").read_text(encoding="utf-8")
    block = re.search(r"\*\*MOS-REL-095\*\*.*?(?=\n\n\*\*MOS-|\n\n#|\Z)", spec, re.S)
    assert block, "MOS-REL-095 is no longer in chapter 15"
    rows = re.findall(r"^\| `([^`]+)` \|", block.group(0), re.M)
    absent = [p for p in REQUIRED if not (REPO / p).exists()]

    assert _states(prose, len(rows)), (
        f"MOS-REL-095's table has {len(rows)} rows and this module's docstring does not "
        f"say so. "
        f"It said 'seventeen' for a table of sixteen, which is how a reader learns the "
        f"wrong "
        f"size of the obligation."
    )
    assert _states(prose, len(absent)), (
        f"{len(absent)} of the {len(REQUIRED)} required paths are absent and this module's "
        f"docstring does not say so. It said 'fourteen', which is the length of REQUIRED."
    )
    assert set(absent) == set(MISSING), (
        "the derivation above disagrees with MISSING, which the main test already asserts; if "
        "this fires alone, REQUIRED and MOS-REL-095's table have come apart"
    )


def test_the_migration_count_this_module_quotes_is_the_one_on_disk() -> None:
    """`MIGRATIONS.md`'s row states a number, and it was wrong by two."""
    migrations = sorted((REPO / "medos" / "medos" / "db" / "migrations").glob("*.up.sql"))
    assert migrations, "no migrations found; the MIGRATIONS.md row's whole premise is gone"
    assert _states(MISSING["MIGRATIONS.md"], len(migrations)), (
        f"{len(migrations)} migrations exist and the MIGRATIONS.md row does not say so. "
        f"It said "
        f"'sixteen' where {len(migrations)} is right -- the numbering starts at 0002 and "
        f"there "
        f"is no 0001, which is how the count was reached by reading the highest name."
    )
    breaking = [
        p.name for p in (REPO / "medos" / "medos" / "db" / "migrations").glob("*.sql")
        if "breaking" in p.read_text(encoding="utf-8", errors="replace").lower()
    ]
    assert not breaking, (
        f"{breaking} now mention 'breaking', so the row's claim that not one of them says "
        "whether it is breaking has gone false. MIGRATIONS.md is what should carry it."
    )
