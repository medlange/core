# SPDX-License-Identifier: Apache-2.0
"""A sentence that says "this is tested" and names a test that does not exist.

MEASURED: `docs/services/AUTHORING.md` — the document a third party reads to decide whether
they can build a capability against this platform — closed with:

    The composition mechanism itself is real and tested: … and
    `tests/integration/test_substituted_catalogue.py` is the test that a substituted
    `medos/services/` tree is honoured.

**No file of that name has ever existed.** The property is genuinely covered, by
`tests/integration/test_capability_providers.py` and its thirty tests; what was wrong was
the address, in the one sentence whose entire job is to say the seam can be trusted.

WHY THIS CLASS IS WORSE THAN A BROKEN LINK. `MOS-REL-012` makes an unexecuted acceptance
criterion equivalent to an unsatisfied requirement, and this project's whole discipline is
that a claim of verification must name something executable. A citation that resolves to
nothing does not degrade the claim, it inverts it: the reader who does not check concludes
the property holds, and the reader who does check finds the repository confused about its
own evidence. It is also the third instance in this sweep of the same shape -- register
entry 128 found four moved modules whose old addresses stayed behind in `trainer/README.md`,
entry 130 a compose service named in a live procedure.

MEASURED SCOPE, so the check is not mistaken for a broader one than it is: 70 test-file
citations and 9 test-function citations across the 21 documents outside `docs/spec/` and
`docs/releases/`. One was broken. This asserts addresses, never whether the test named
actually establishes the property the sentence claims -- no check can read that, and saying
so is the only honest way to have this one.

Register entry 131.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: `docs/spec/` is normative prose whose examples are illustrative, and `docs/releases/` is
#: history: a release record naming a test names the test as it was at that tag.
#: `docs/README.md` says so in terms.
SKIP = ("docs/spec/", "docs/releases/")

FILE_CITATION = re.compile(r"`([\w./-]*test_[\w./-]*\.py)`")
FUNC_CITATION = re.compile(r"`(test_\w+)`")

#: Test names a document cites in order to say they do NOT exist. Declared, because a check
#: that forbade the string would forbid the paragraph that retracts it -- the failure a
#: sibling gate in `tests/unit/test_product_readmes.py` hit and records.
CITED_AS_ABSENT: dict[str, str] = {
    "tests/integration/test_substituted_catalogue.py": (
        "docs/services/AUTHORING.md retracts it by name: 'This sentence named … until "
        "2026-09-26, and no such file has ever existed.' Naming it is how a reader who "
        "holds an older copy of that document finds out what happened to the claim."
    ),
    "viewer/tests/test_dose_notes.py": (
        "viewer/docs/plugin-template.md and viewer/docs/testing.md name it as the file "
        "THE READER creates for the template panel — the template is a doc artifact "
        "(its code is gate-checked by viewer/tests/test_plugin_template.py), and the "
        "citation says the test does not exist until the reader writes it. Declared "
        "rather than reworded so the sentence cannot quietly start naming a file that "
        "isn't the reader's to write."
    ),
}


def _tracked() -> list[str]:
    # UNTRACKED FILES COUNT, IN BOTH DIRECTIONS, and the second is the one that
    # bites: a document citing a test that exists but is not committed yet would be
    # called broken by a check reading only the index. Five such test files exist
    # right now, in another session's tree. The first direction matters too -- a new
    # document citing a test that never existed should fail before it is committed.
    return subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()


TRACKED = _tracked()


def _text(rel: str) -> str:
    return (ROOT / rel).read_bytes().decode("utf-8", errors="replace")


def _test_files() -> set[str]:
    return {
        p for p in TRACKED
        if re.search(r"(^|/)test_\w+\.py$", p) or p.endswith(".test.mjs")
    }


def _test_basenames() -> set[str]:
    return {Path(p).name for p in _test_files()}


def _test_functions() -> set[str]:
    names: set[str] = set()
    for rel in _test_files():
        for m in re.finditer(r"^\s*(?:async )?def (test_\w+)", _text(rel), re.M):
            names.add(m.group(1))
    return names


def _documents() -> list[str]:
    return [
        p for p in TRACKED
        if p.endswith(".md") and not any(p.startswith(s) for s in SKIP)
    ]


def test_there_are_test_citations_to_check() -> None:
    """A green run must not be reachable by finding no citations at all."""
    files = sum(len(FILE_CITATION.findall(_text(d))) for d in _documents())
    funcs = sum(len(FUNC_CITATION.findall(_text(d))) for d in _documents())
    assert files >= 40 and funcs >= 5, (
        f"only {files} test-file and {funcs} test-function citation(s) found across "
        f"{len(_documents())} documents; the matcher is broken, not the documentation"
    )


def test_every_documented_test_file_exists() -> None:
    basenames = _test_basenames()
    broken: list[str] = []
    for rel in _documents():
        for raw in FILE_CITATION.findall(_text(rel)):
            if raw in CITED_AS_ABSENT:
                continue
            if (ROOT / raw).exists() or Path(raw).name in basenames:
                continue
            broken.append(f"{rel}: {raw}")
    assert not broken, (
        f"{len(broken)} documented test file(s) do not exist:\n  " + "\n  ".join(broken)
        + "\n\nA sentence that says a property is tested and names a file that is not "
        "there inverts the claim rather than weakening it."
    )


def test_every_documented_test_function_exists() -> None:
    functions = _test_functions()
    basenames = _test_basenames()
    broken: list[str] = []
    for rel in _documents():
        for name in FUNC_CITATION.findall(_text(rel)):
            if name in functions or f"{name}.py" in basenames:
                continue
            broken.append(f"{rel}: {name}")
    assert not broken, (
        f"{len(broken)} documented test function(s) are defined nowhere:\n  "
        + "\n  ".join(broken)
    )


def test_nothing_cited_as_absent_has_been_created() -> None:
    """If somebody writes the file, the retraction has to go in the same change."""
    built = sorted(p for p in CITED_AS_ABSENT if (ROOT / p).exists())
    assert not built, (
        f"{built} now exist, and a document names each of them to say they never did. "
        f"Delete the retraction: {[CITED_AS_ABSENT[p] for p in built]}"
    )


def test_every_retraction_is_still_written_down() -> None:
    """A declared exception nothing uses reads as a rule somebody needed."""
    cited: set[str] = set()
    for rel in _documents():
        cited |= set(FILE_CITATION.findall(_text(rel)))
    dead = sorted(set(CITED_AS_ABSENT) - cited)
    assert not dead, (
        f"CITED_AS_ABSENT declares {dead}, which no document names any more. If the "
        "retraction was removed, remove the entry with it."
    )
