# SPDX-License-Identifier: Apache-2.0
"""`MOS-CONF-114` asks which components trace to a spike. After the deletion, this answers.

    "Any spike code that survives into 0.1.0 without that re-implementation is legacy
     software in the 62304 sense from the day the product ships. CI MUST be able to
     answer, per release, which components trace to a spike and which were
     re-implemented."

`spikes/week0/` is deleted (register entry 118). The answer is no longer "diff the module
against the spike" -- it is the sentence each module carries in its own header saying
where its body came from. Fourteen modules say it.

THOSE SENTENCES ARE EVIDENCE, NOT ADDRESSES, AND THAT DISTINCTION HAS COST THIS
REPOSITORY FIVE FALSIFICATIONS IN ONE DAY. `medos/medos/core/geometry.py` says it was
lifted from `spikes/week0/build_volume.py`. That path does not resolve and MUST NOT be
made to: it is a statement about where this code came from, in the same category as a
release record in `docs/releases/` and a comment inside an applied migration. A sweep that
"fixes" it destroys the only per-component answer `MOS-CONF-114` has.

So this check holds the count and the set. It does not care what the sentences say beyond
naming the spike; it cares that they are still there, because the failure mode is a
mechanical pass removing them all at once and nothing noticing.

WHY A COUNT AND NOT A DIFF. There is nothing left to diff against. That is the honest
consequence of the deletion and it is recorded in entry 118: what a reviewer can still do
is read `git show <the deleting commit>~1:spikes/week0/build_volume.py` beside
`medos/medos/core/geometry.py`. What CI can still do is say which fourteen modules make
the claim, which is the question `MOS-CONF-114` actually asks.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "medos" / "medos"

#: The marker. Every lifted module names the spike file its body came from, so the token
#: is the directory rather than any one file name.
MARKER = "spikes/week0"

#: The fourteen, as of the commit that deleted the spike. Pinned as a SET and not as a
#: number: a module losing its provenance line while another gains one keeps the count
#: and is exactly the drift this exists to catch.
LIFTED = frozenset({
    "medos/medos/capabilities/base.py",
    "medos/medos/capabilities/lung_segmentation.py",
    "medos/medos/core/__init__.py",
    "medos/medos/core/concepts.py",
    "medos/medos/core/dicomio.py",
    "medos/medos/core/errors.py",
    "medos/medos/core/geometry.py",
    "medos/medos/core/masks.py",
    "medos/medos/core/measure.py",
    "medos/medos/core/uids.py",
    "medos/medos/dicomweb/client.py",
    "medos/medos/writer/identity.py",
    "medos/medos/writer/seg.py",
    "medos/medos/writer/sr.py",
})


def _claimants() -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for path in PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
        and MARKER in path.read_text(encoding="utf-8", errors="replace")
    }


def test_every_module_lifted_from_the_spike_still_says_so() -> None:
    found = _claimants()

    lost = sorted(LIFTED - found)
    assert not lost, (
        f"{len(lost)} module(s) no longer say where their body came from: {lost}\n"
        "  These sentences are the per-component answer `MOS-CONF-114` requires, and the "
        "spike they name is deleted, so nothing else can reconstruct them. A path inside "
        "one of them is EVIDENCE -- it says where this code was, not where anything is "
        "now -- and a sweep that repaired it or removed it has destroyed a record. If a "
        "module was genuinely rewritten rather than lifted, say so in its header and "
        "remove it from LIFTED in the same commit, so the removal is a decision somebody "
        "made rather than a diff nobody read."
    )

    gained = sorted(found - LIFTED)
    assert not gained, (
        f"{gained} newly claims to have been lifted from the spike. The spike has been "
        "deleted since register entry 118, so a new claim cannot be verified against "
        "anything: either this is a copy of a lifted module (which would be the second "
        "definition CONTRACT.md section 2 exists to eliminate) or it is a comment that "
        "means something else. Add it to LIFTED deliberately if the claim is true."
    )


def test_the_spike_tree_is_gone_and_nothing_resolves_a_path_into_it() -> None:
    """Deleted, and no code reaches for it.

    The claim in the headers is history; a `Path(...)` or an `open(...)` naming the same
    directory is a runtime dependency on something that does not exist. The platform had
    one of those for months -- `default_concepts_path()` counted directories up out of
    the package to read `capability_concepts.json` out of a frozen spike -- and it was
    only visible because deleting the spike would have broken every DICOM write.
    """
    assert not (ROOT / "spikes").exists(), (
        "`spikes/` is back. If that is deliberate, entry 118 records what was moved out "
        "of it and where; re-creating the directory without moving those back leaves two "
        "copies of eight rejection paths."
    )

    # AST, not a line heuristic. The first version of this check looked for a quote
    # character on the line and flagged `identity.py:5` -- a MODULE DOCSTRING whose prose
    # happens to contain a quoted phrase. A docstring is a string node too; what tells
    # them apart is position in the tree, not punctuation.
    reaching: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Per file. `id()` is only unique among LIVE objects, and a set carried across
        # files can match a new node against a freed one from an earlier tree.
        docstrings: set[int] = set()
        for holder in [tree, *(n for n in ast.walk(tree) if isinstance(
                n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))]:
            doc = holder.body[0] if holder.body else None
            if isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant):
                docstrings.add(id(doc.value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or MARKER not in node.value:
                continue
            reaching.append(
                f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: "
                f"{node.value.strip()[:70]}"
            )

    assert not reaching, (
        "these lines put `spikes/week0` in a string literal rather than in prose:\n    "
        + "\n    ".join(reaching)
        + "\n  The directory does not exist. A provenance sentence belongs in a comment "
        "or a docstring; a string literal is a path somebody means to open."
    )
