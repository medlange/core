# SPDX-License-Identifier: Apache-2.0
"""The release-criteria register, held to the specification and to itself.

WHY THIS IS A UNIT TEST
-----------------------
`tests/_support/release_criteria.py` is a claim about what the 0.1.0 gate covers. A claim
that lives only in a comment rots, and a rotted coverage register is worse than none: it
tells a reviewer an item is gated when nothing gates it, which is the same failure as the
one that produced it -- `MOS-SAFE-089a` reaching a green gate because nobody had written
down that the only check for it was a human with a browser.

So the register is parsed against `docs/spec/15-delivery.md` on every default run. It
needs no container: it reads two files.

FOUR ROWS NOW, AND THEY ARE HELD TO TWO DIFFERENT STANDARDS ON PURPOSE
-----------------------------------------------------------------------
15.1.2 and 15.1.3 now carry chapter 19's operator-surface elements in all four release
rows, ingested from `MOS-UI-365` and tiered from `MOS-UI-371`. The register classifies
0.1.0 in full, as it always did, and for 0.2.0, 0.3.0 and 0.4.0 it classifies the
chapter-19 elements and nothing else.

Holding those three rows to "exactly and in order" would mean inventing verdicts for the
evidence plane, the training pipeline and the MCP surface in one sitting, which is the
shape of guess this module exists to refuse. Holding them to nothing would let the next
chapter-19 element arrive unclassified -- which is EXACTLY the defect being repaired here,
since `MOS-UI-365` spent three releases placing work in rows that never listed it. So the
standard for those rows is: every entry citing a `MOS-UI-` requirement is registered, in
table order, in the spec's own words; and the number of entries that are NOT registered is
frozen, so the unclassified half can shrink but cannot grow.

WHAT EACH TEST PINS
-------------------
  * the register covers 15.1.2's contents column and 15.1.3's tier row for 0.1.0 EXACTLY,
    in order, in the spec's own words -- so a spec revision that adds, removes or rewords
    an item fails here and names it;
  * for every other row, every chapter-19 element is registered in table order, and the
    unregistered remainder is exactly the frozen count;
  * every ingested contents item has exactly one tier-table twin carrying the same tier,
    so `MOS-REL-005`'s "exactly one tier per contents item" is mechanical for the half of
    the table this ingestion added rather than a reading;
  * every Tier A item without a gate check is one of the three already known, so existing
    debt stays visible and NEW debt is a failure;
  * a Tier A item can never be marked CUT (`MOS-REL-005`: "MUST NOT be cut under any
    circumstance"), and a Tier B cut must name where it reappears;
  * every non-GATE criterion gives a reason, and every GATE criterion names what covers
    it -- a register entry that asserts coverage without naming it is a guess;
  * the gate's terminal summary actually prints the report, so the loud half cannot be
    quietly deleted while the register stays.

Spec: MOS-REL-004, MOS-REL-005, MOS-REL-009, MOS-REL-012.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests._support.release_criteria import (
    CRITERIA,
    RELEASE,
    RELEASES,
    TIER_A_WITHOUT_GATE_COVERAGE,
    UNREGISTERED_CONTENTS,
    UNREGISTERED_TIERS,
    UNTIERED_CONTENTS,
    VERDICT_MEANING,
    Criterion,
    for_release,
    ingested_marker,
    parse_contents_items,
    parse_tier_items,
    report_lines,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_CONFTEST = REPO_ROOT / "tests" / "gate" / "conftest.py"

#: The three rows the register classifies only in part -- see the module docstring.
PARTIAL = tuple(r for r in RELEASES if r != RELEASE)


def _contents(release: str = RELEASE) -> tuple[Criterion, ...]:
    return for_release(release, "15.1.2")


def _tiers(release: str = RELEASE) -> tuple[Criterion, ...]:
    return for_release(release, "15.1.3")


# ======================================================================================
# The register cannot drift behind the specification
# ======================================================================================
def test_the_register_covers_the_contents_column_exactly_and_in_order() -> None:
    """Every item of 15.1.2's `0.1.0` contents cell, none invented, none dropped."""
    spec = parse_contents_items(RELEASE)
    registered = [c.text for c in _contents()]
    assert registered == spec, (
        "the register and 15.1.2's contents column for "
        f"{RELEASE} disagree.\n"
        f"  only in the spec:     {[s for s in spec if s not in registered]}\n"
        f"  only in the register: {[r for r in registered if r not in spec]}\n"
        "Every release contents item needs a recorded answer to 'would the gate go red "
        "without it?' -- MOS-REL-012."
    )


def test_the_register_covers_the_tier_table_exactly_and_in_order() -> None:
    """Every row of 15.1.3's `0.1.0` tier row, with the tier the spec assigns."""
    spec = parse_tier_items(RELEASE)
    registered = [(c.tier, c.text) for c in _tiers()]
    assert registered == spec, (
        "the register and 15.1.3's tier table for "
        f"{RELEASE} disagree.\n"
        f"  only in the spec:     {[s for s in spec if s not in registered]}\n"
        f"  only in the register: {[r for r in registered if r not in spec]}"
    )


def test_every_criterion_slug_is_unique() -> None:
    slugs = [c.slug for c in CRITERIA]
    duplicated = sorted({s for s in slugs if slugs.count(s) > 1})
    assert not duplicated, f"duplicate criterion slugs: {duplicated}"


@pytest.mark.parametrize("release", PARTIAL)
def test_every_chapter_19_element_of_every_other_row_is_registered(release: str) -> None:
    """`MOS-UI-365`'s elements, in 15.1.2's own words and its own order.

    The defect this repairs is not that chapter 19's elements were classified wrongly. It
    is that they were in no release row at all, so `MOS-REL-009` had no item to select a
    response for and both 0.2.0 and 0.3.0 were declared complete with chapter 19's whole
    assignment to them missing and no gate able to say so. Matching on the requirement id
    rather than on a hand-kept list is what stops that recurring: an element added to a
    row citing a `MOS-UI-` requirement and left unclassified fails here, by name.
    """
    marker = ingested_marker()
    spec = [item for item in parse_contents_items(release) if marker in item]
    registered = [c.text for c in _contents(release)]
    assert registered == spec, (
        f"the register and 15.1.2's chapter-19 elements for {release} disagree.\n"
        f"  only in the spec:     {[s for s in spec if s not in registered]}\n"
        f"  only in the register: {[r for r in registered if r not in spec]}"
    )


@pytest.mark.parametrize("release", PARTIAL)
def test_every_chapter_19_tier_row_of_every_other_row_is_registered(release: str) -> None:
    """The same, for 15.1.3, with the tier the specification assigns."""
    marker = ingested_marker()
    spec = [pair for pair in parse_tier_items(release) if marker in pair[1]]
    registered = [(c.tier, c.text) for c in _tiers(release)]
    assert registered == spec, (
        f"the register and 15.1.3's chapter-19 entries for {release} disagree.\n"
        f"  only in the spec:     {[s for s in spec if s not in registered]}\n"
        f"  only in the register: {[r for r in registered if r not in spec]}"
    )


@pytest.mark.parametrize("release", RELEASES)
def test_the_unregistered_remainder_of_each_row_is_exactly_the_frozen_count(
    release: str,
) -> None:
    """Honest debt, frozen so it can shrink and cannot grow.

    The 0.2.0 Release Decision Record asked for this in terms: "there is no coverage
    register for this release ... a green `-m gate_0_2_0` therefore states that its six
    named checks passed and states NOTHING about which of this release's contents those
    checks stand behind". This does not close that -- it bounds it. A new unclassified
    item in any row is now a failure here rather than a discovery later, and the day
    somebody classifies the evidence plane these numbers go down.
    """
    contents_gap = len(parse_contents_items(release)) - len(_contents(release))
    tiers_gap = len(parse_tier_items(release)) - len(_tiers(release))
    assert (contents_gap, tiers_gap) == (
        UNREGISTERED_CONTENTS[release],
        UNREGISTERED_TIERS[release],
    ), (
        f"release {release}: {contents_gap} contents items and {tiers_gap} tier entries "
        f"are unclassified; the register records "
        f"{UNREGISTERED_CONTENTS[release]} and {UNREGISTERED_TIERS[release]}. If an item "
        f"was added, classify it. If one was classified, lower the count."
    )


@pytest.mark.parametrize("release", RELEASES)
def test_every_ingested_contents_item_carries_exactly_one_tier_row(release: str) -> None:
    """`MOS-REL-005`: exactly one tier per contents item, made mechanical.

    15.3's conformance item 1 fails a parser on any contents item with no tier, and six
    0.1.0 items already have none (`UNTIERED_CONTENTS`, register entry 61). That defect is
    pinned elsewhere and not repeated here: every item the chapter-19 ingestion added
    carries a tier-table twin with the SAME tier, named `tier-<slug>`, so the two tables
    cannot drift apart for the half of them this change is responsible for.
    """
    for criterion in _contents(release):
        if not criterion.slug.startswith("ui0"):
            continue
        twin = [c for c in _tiers(release) if c.slug == f"tier-{criterion.slug}"]
        assert len(twin) == 1, (
            f"{criterion.slug} is a chapter-19 contents item with {len(twin)} tier-table "
            f"twins; MOS-REL-005 requires exactly one tier per contents item"
        )
        assert twin[0].tier == criterion.tier, (
            f"{criterion.slug} is tier {criterion.tier} in 15.1.2's register entry and "
            f"{twin[0].tier} in 15.1.3's. One item cannot carry two tiers."
        )


# ======================================================================================
# The tier rules of MOS-REL-005
# ======================================================================================
def test_the_untiered_contents_items_are_exactly_the_ones_the_spec_leaves_untiered() -> None:
    """A REPORTED specification defect, frozen so it cannot quietly grow or vanish.

    `MOS-REL-005` requires every contents item to be assigned exactly one tier, and 15.3's
    conformance item 1 says a parser over 15.1.2 and 15.1.3 "fails if any contents item
    has no tier". Six do. This test does not fix that -- inventing tiers for safety-
    relevant items is precisely the silent scope decision `MOS-REL-006` exists to prevent
    -- it pins it, so that a spec revision which tiers them shows up here as a failure
    asking for the register to be updated.
    """
    untiered = tuple(c.slug for c in _contents() if c.tier is None)
    assert untiered == UNTIERED_CONTENTS, (
        f"the set of untiered 15.1.2 contents items changed: {untiered} vs the recorded "
        f"{UNTIERED_CONTENTS}. If the specification now assigns tiers, update the register "
        f"and the spec-defect note in tests/_support/release_criteria.py."
    )


def test_no_tier_a_criterion_is_ever_marked_cut() -> None:
    """`MOS-REL-005`: Tier A "MUST NOT be cut under any circumstance"."""
    offenders = [c.slug for c in CRITERIA if c.tier == "A" and c.verdict == "CUT"]
    assert not offenders, (
        f"Tier A criteria marked CUT: {offenders}. MOS-REL-005 admits no circumstance for "
        f"this, and MOS-REL-009 offers no response that cuts one."
    )


def test_a_cut_criterion_records_its_response_and_where_it_reappears() -> None:
    """`MOS-REL-009` requires the response recorded; `MOS-REL-005` requires the return.

    Written while nothing was marked CUT, and no longer vacuous: the chapter-19 ingestion
    records thirty-six, because `MOS-UI-365` assigned elements to 0.1.0, 0.2.0 and 0.3.0
    that those releases were declared complete without. It was written so that the first cut
    could not be a bare verdict change: a Tier B item that is cut "MUST reappear as Tier A
    or B of the next release", and an item cut with no named return is scope that has
    silently shrunk, which is the exact failure `MOS-REL-006` names. Every Tier B cut
    below now names its return, and the chain of returns is what puts the no-code training
    surface in 0.4.0 rather than nowhere.
    """
    for criterion in CRITERIA:
        if criterion.verdict != "CUT":
            continue
        assert criterion.cut, (
            f"{criterion.slug} is CUT but records no MOS-REL-009 response and no Release "
            f"Decision Record reference"
        )
        if criterion.tier == "B":
            assert criterion.reappears_in, (
                f"{criterion.slug} is a Tier B cut with no `reappears_in`; MOS-REL-005 "
                f"requires it to reappear as Tier A or B of the next release"
            )
            # "the NEXT release", not some later one. A cut that names a return two rows
            # away is a cut that has silently slipped a release on its own authority.
            nxt = RELEASES.index(criterion.release) + 1
            assert nxt < len(RELEASES), (
                f"{criterion.slug} is a Tier B cut taken at {criterion.release}, which is "
                f"the last row 15.1.2 defines, so MOS-REL-005's return has no release to "
                f"land in. That is a decision for the plan's owner, not a verdict."
            )
            assert criterion.reappears_in == RELEASES[nxt], (
                f"{criterion.slug} is cut at {criterion.release} and claims to return in "
                f"{criterion.reappears_in}; MOS-REL-005 says the NEXT release, "
                f"{RELEASES[nxt]}"
            )


# ======================================================================================
# The debt is frozen: visible, and unable to grow
# ======================================================================================
def test_the_tier_a_items_without_gate_coverage_are_exactly_the_recorded_set() -> None:
    """A NEW Tier A gap is a failure; the existing ones are on the record.

    Failing on the recorded set would fail the gate for test placement rather than
    product behaviour, and a red with no MOS-REL-009 response is a red that gets bypassed.
    Failing on one MORE is right: it means a Tier A item that used to be gated stopped
    being, or an uncuttable item arrived with no check, and either is a decision somebody
    must make rather than discover.

    The set went from three to thirteen with the chapter-19 ingestion, and none of the ten
    is a new gap -- each was already uncovered inside a chapter whose placements 15.1.2
    had never read. Three of the ten are worse than uncovered and the register says so in
    their `gap` field: `ui010-safe012-adjacency-set`,
    `ui020-ruo-badge-and-research-gating` and `ui030-honest-metric-rule` are uncuttable
    items that were not built. This test deliberately does NOT fail on them. Failing here
    would put the whole default suite red over a decision only the plan's owner can take,
    and `MOS-REL-009` offers no response that cuts a Tier A item, so there would be no way
    to act on the red except by retiering -- which is the one move that would make the
    record dishonest. They are frozen, loud, and in every gate run's terminal summary.
    """
    gaps = tuple(sorted(c.slug for c in CRITERIA if c.tier == "A" and c.verdict != "GATE"))
    assert gaps == tuple(sorted(TIER_A_WITHOUT_GATE_COVERAGE)), (
        f"Tier A items without a gate check changed.\n"
        f"  now:      {gaps}\n"
        f"  recorded: {tuple(sorted(TIER_A_WITHOUT_GATE_COVERAGE))}\n"
        "A new entry means an uncuttable item the release gate cannot see. Close it with a "
        "gate check, or record it here with the reason it cannot be closed yet."
    )


@pytest.mark.parametrize("criterion", CRITERIA, ids=lambda c: c.slug)
def test_every_criterion_justifies_its_verdict(criterion: Criterion) -> None:
    """A verdict with no evidence behind it is an opinion in a table.

    GATE must name what gates it; anything weaker must say what is missing. Both
    directions have bitten this repository: an unnamed pass is unauditable, and an
    unexplained gap is one nobody can act on.
    """
    assert criterion.verdict in VERDICT_MEANING, criterion.verdict
    if criterion.verdict == "GATE":
        assert criterion.covered_by, (
            f"{criterion.slug} claims GATE coverage but names no check. Name the check, or "
            f"the claim is that somebody remembers one."
        )
    else:
        assert criterion.gap, (
            f"{criterion.slug} is {criterion.verdict} and gives no reason. MOS-REL-012 "
            f"makes an unexecuted criterion an unsatisfied requirement; a reviewer needs to "
            f"know which one and why."
        )


def test_the_report_names_every_criterion_the_gate_does_not_cover() -> None:
    """The printed report and the register must agree, in full.

    Truncating the report to the first few entries would reintroduce the original defect
    at one remove: a green gate whose output implies the uncovered set is small.
    """
    text = "\n".join(report_lines())
    for criterion in CRITERIA:
        if criterion.verdict == "GATE":
            continue
        assert criterion.text in text, (
            f"the coverage report does not name {criterion.slug!r}, which the gate does "
            f"not cover"
        )


# ======================================================================================
# The loud half cannot be deleted while the register stays
# ======================================================================================
def test_the_gate_conftest_prints_the_coverage_report() -> None:
    """`tests/gate/conftest.py` must call `report_lines` from its terminal summary.

    Parsed with `ast` rather than by importing: importing that conftest pulls in `psycopg`
    and `requests` and reads the deployment's environment, and a unit test that needs the
    stack's libraries to check that a function is called is in the wrong directory.

    This is the guard on the guard. A register nobody prints is a register nobody reads,
    and deleting one line of a terminal-summary hook is the cheapest possible way to go
    back to a gate that reports only its own passes.
    """
    tree = ast.parse(GATE_CONFTEST.read_text(encoding="utf-8"), filename=str(GATE_CONFTEST))
    summaries = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_terminal_summary"
    ]
    assert summaries, "tests/gate/conftest.py defines no pytest_terminal_summary hook"

    dump = ast.dump(summaries[0])
    assert "report_lines" in dump, (
        "tests/gate/conftest.py's pytest_terminal_summary no longer calls "
        "tests._support.release_criteria.report_lines, so a gate run prints its passes and "
        "stays silent about everything it does not check (MOS-REL-012)."
    )
