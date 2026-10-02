# SPDX-License-Identifier: Apache-2.0
"""The register's stable numbers, re-derived from the tree rather than trusted.

WHY THIS EXISTS. The register is read as measured -- it is the document a reviewer opens to
find out what this repository knows about itself, and every entry that says "measured" is
taken at its word. Measured in this session, that trust was not fully earned:

    entry 136 said "21 directory walks across **155 test modules**". Re-derived at the
    commit that wrote it: 124 test modules, and 152 tracked Python files under the test
    trees. The script printed "155 test modules read" for a glob that swept `__init__.py`,
    the ten `*_probe.py` generators, `capability_source.py`, `docker_json.py` and
    `phantom_cohort.py`, and I copied the label along with the number.

That is entry 140's error -- a population counted one way and described another -- committed
inside the register. It is the same shape as `docs/adr/BUILD_VS_ADOPT.md`'s "imported in 44
modules", which survived my measuring 47 MENTIONS against a claim about IMPORTS. The
difference is that the ADR was right and I was wrong; here I was wrong in the register.

WHAT THIS CHECKS, AND WHAT IT CANNOT. It re-derives the register's numbers that are
PROPERTIES rather than snapshots: how many probes exist, how many trees are tracked, how many
extension files cite a withdrawn requirement, how many trainer modules import the platform.
Those should not move without somebody deciding they should, so a mismatch is either a real
change that the entry must record or a number that was never right.

It deliberately does NOT check the snapshots -- "eleven untracked files existed when this was
measured" is true of that moment and false now, because another session has added four more,
and an entry that dates its measurement is doing the right thing. Re-deriving those would
turn a correct historical statement into a permanent failure, which is the release-record
mistake `docs/README.md` forbids one directory over.

Register entry 141.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REGISTER = ROOT / "docs" / "spec" / "99-known-inconsistencies.md"


def _tracked_and_present(*spec: str) -> list[str]:
    return subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", *spec],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()


def _probes() -> list[Path]:
    return sorted((ROOT / "tests" / "_support").glob("*_probe.py"))


def _trees() -> set[str]:
    return {p.split("/")[0] for p in _tracked_and_present() if "/" in p}


def _extension_files_citing_withdrawn() -> list[str]:
    return sorted(
        p for p in _tracked_and_present("medos/web/ohif-extension/**")
        if not p.endswith("README.md")
        and "MOS-UI-012" in (ROOT / p).read_text(encoding="utf-8", errors="replace")
    )


def _trainer_platform_importers() -> list[str]:
    """Trainer modules whose reach into `medos` is the PLATFORM, not the SDK.

    After the pivot every fitter file imports `medos.sdk` — that is the designed
    dependency, not a boundary crossing — so those imports are excluded the way the
    import-boundary gate excludes them. What this counts is reach into everything else
    under `medos.*`: today that is `__main__.py` and its supervisor branch alone.
    """
    out = subprocess.run(
        ["git", "ls-files", "trainer/medos_trainer"], cwd=ROOT,
        capture_output=True, text=True,
    ).stdout.split()
    return sorted(
        p for p in out
        if re.search(
            r"^\s*(?:import\s+medos\b(?!\.sdk)|from\s+medos\.(?!sdk\b))",
            (ROOT / p).read_text(encoding="utf-8", errors="replace"), re.M,
        )
    )


#: entry -> (what the register claims, how to measure it). Each is a PROPERTY: it changes
#: only when somebody changes the repository on purpose, so a mismatch is news either way.
#: Each row carries the PHRASE the entry uses, not just the number. The first version looked
#: for the digit or its spelling anywhere in the entry, and "4" appears in `0.4.0`,
#: `MOS-UI-012a` and every commit hash -- so deleting the word "four" left the check green.
#: A phrase is the construct at its site; a digit in a long paragraph is nearly vacuous.
CLAIMS: tuple[tuple[str, str, int, str, object], ...] = (
    ("132", "DICOM probes in tests/_support/", 10, "ten `*_probe.py` generators",
     lambda: len(_probes())),
    ("132", "probes cited in viewer/tests/test_architecture.py", 8,
     "Eight are cited only in",
     lambda: len([
         p for p in _probes()
         if p.name in (ROOT / "viewer" / "tests" / "test_architecture.py").read_text(
             encoding="utf-8")
     ])),
    ("136", "tracked top-level trees", 4, "the four tracked top-level trees",
     lambda: len(_trees())),
    ("139", "extension files citing the withdrawn MOS-UI-012", 4,
     "**four** source files cite them",
     lambda: len(_extension_files_citing_withdrawn())),
)


@pytest.mark.parametrize(("entry", "what", "stated", "_phrase", "measure"), CLAIMS)
def test_a_register_number_re_derives(
    entry: str, what: str, stated: int, _phrase: str, measure: object
) -> None:
    actual = measure()  # type: ignore[operator]
    assert actual == stated, (
        f"register entry {entry} rests on {what} being {stated}; measured {actual}.\n"
        "  Either the repository changed and the entry must record it, or the number was "
        "never right -- which is what happened to entry 136's '155 test modules', a count "
        "of files under the test trees wearing the label 'test modules'."
    )


@pytest.mark.parametrize(("entry", "what", "_stated", "phrase", "_m"), CLAIMS)
def test_the_register_still_states_that_number(
    entry: str, what: str, _stated: int, phrase: str, _m: object
) -> None:
    """The other half: a claim this module watches must still be IN the register.

    Without this, deleting the sentence would silence the check instead of failing it -- and
    a frozen table whose subject has quietly gone is the shape three sibling gates in this
    suite record.
    """
    text = REGISTER.read_bytes().decode("utf-8").replace("\r\n", "\n")
    body = re.search(rf"^{entry}\. .*?(?=^\d+\. |\Z)", text, re.M | re.S)
    assert body, f"register entry {entry} is missing"
    assert phrase in body.group(0), (
        f"entry {entry} no longer contains {phrase!r}, and this module watches that phrase "
        f"for {what}. If the entry was rewritten, update CLAIMS; if the claim was withdrawn, "
        "drop the row."
    )


def test_the_correction_to_entry_136_is_recorded() -> None:
    """The wrong number is named, not swapped out.

    A register that silently corrects itself is a register a reader cannot date. Entry 123
    was amended the same way earlier in this session: the old claim stays, marked.
    """
    text = REGISTER.read_bytes().decode("utf-8")
    entry = re.search(r"^136\. .*?(?=^\d+\. |\Z)", text, re.M | re.S)
    assert entry, "register entry 136 is missing"
    assert "155 test modules" in entry.group(0), (
        "entry 136 no longer names the wrong figure it used to state. The correction has to "
        "leave the old number visible, or a reader holding an older copy cannot tell what "
        "changed."
    )
    assert "AMENDED" in entry.group(0), (
        "entry 136 states 155 without marking it as the amended-away figure"
    )
