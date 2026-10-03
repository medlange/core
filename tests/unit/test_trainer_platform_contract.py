# SPDX-License-Identifier: Apache-2.0
"""Two things the platform/trainer protocol says about itself that are not true.

Both are recorded in `docs/spec/99-known-inconsistencies.md` -- entries 111 and 112 -- and
both were measured rather than inferred. This file makes each of them EXECUTABLE.

WHY `xfail(strict=True)` AND NOT A RED TEST
--------------------------------------------
A test that is red for a defect nobody is fixing this week is a test that gets deleted, or
worse, a CI signal people learn to scroll past. A test that is simply absent is how entry
108 happened: an index promised to be generated, maintained by hand, drifted, and nothing
noticed.

`xfail(strict=True)` is neither. Each test below asserts the FIXED state. While the defect
stands the assertion fails and pytest reports `x` -- expected, quiet, and not a pass.
The moment somebody repairs the defect the assertion succeeds, and a STRICT xfail that
succeeds is a FAILURE. The suite then goes red with a message naming the register entry to
close.

So the register entry cannot go stale in either direction: it cannot be silently fixed,
and it cannot be silently forgotten. That is the whole construct, and it is worth more
here than a red line in CI.

WHAT IS DELIBERATELY NOT WITNESSED HERE
----------------------------------------
Register entry 113 -- the supervision map that is never written on the platform path --
has no witness in this file, on purpose. Its subject is `executor.py`, `backend.py` and
`masked_trainer.py`, which a concurrent change was editing when 113 was measured. A
witness over a moving target asserts the state of somebody's afternoon, not the state of
the repository. It is owed once those files settle, and this paragraph is the record of
that debt.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MEDOS = ROOT / "medos" / "medos"
TRAINER = ROOT / "trainer" / "medos_trainer"
SHARED = ROOT / "medos" / "medos" / "sdk"
REGISTER = ROOT / "docs" / "spec" / "99-known-inconsistencies.md"

#: Every register entry this file witnesses. The numbers are checked against the register
#: itself below -- a witness pointing at an entry that has been renumbered or closed is a
#: witness nobody can act on.
#: Entry 111 was CLOSED on 2026-10-02: a platform module now compares the exchange version
#: (the supervisor work that landed beside the SDK move), the strict xfail passed, and the
#: marker came off the witness below per the reason string's own instruction.
WITNESSED = (112,)


def _medos_sources() -> list[Path]:
    return sorted(
        p for p in MEDOS.rglob("*.py") if "__pycache__" not in p.parts
    )


def test_every_witness_here_names_a_register_entry_that_exists() -> None:
    """A citation is only useful while the thing it cites is findable.

    The register is a numbered list that grows by appending, so an entry number is stable
    -- but an entry can be CLOSED, and a witness for a closed entry is a witness for
    nothing. This keeps the two ends tied without copying the register's text.
    """
    text = REGISTER.read_text(encoding="utf-8")
    for number in WITNESSED:
        match = re.search(rf"^{number}\. (.+)$", text, re.M)
        assert match, (
            f"register entry {number} is not in {REGISTER.name}. Either it was renumbered "
            f"-- which the register's append-only convention forbids -- or this witness "
            f"outlived its subject and should be deleted with the entry"
        )
        assert "CLOSED" not in match.group(1)[:400], (
            f"register entry {number} is marked closed in its opening sentence, but its "
            f"witness in this file is still an xfail. One of the two is wrong: either the "
            f"defect is fixed (delete the witness) or the entry was closed early"
        )


def _contract_version_name() -> str:
    """The name the trainer gives its exchange-version constant, READ not spelled.

    `contract.py` is the declaring side, so the name comes from there. Spelling it here
    would make this witness agree with itself after a rename -- and a rename is one of the
    ways the platform side could come to name the constant without anybody noticing the
    witness had stopped checking.
    """
    source = (SHARED / "contract.py").read_text(encoding="utf-8")
    names = re.findall(r"^([A-Z][A-Z0-9_]*VERSION[A-Z0-9_]*)\s*[:=]", source, re.M)
    assert names, (
        "`medos.sdk/contract.py` declares no *VERSION constant, so this "
        "witness has lost its subject. If the exchange version was renamed or removed, "
        "register entry 111 needs re-reading, not this regex widening"
    )
    return names[0]


def test_the_trainer_still_declares_an_exchange_version() -> None:
    """The positive control for entry 111's witness, and it is here because it was MISSING.

    Found by breaking: narrowing `_contract_version_name`'s pattern so it could match
    nothing left the witness below reporting `xfail` -- unchanged, quiet, apparently
    fine. An `xfail` swallows the reason it failed for, so "the defect still stands" and
    "this test lost its subject" are the same colour on the terminal. The witness for
    entry 112 had this control from the start; this one did not, and the break test is
    what noticed.

    Split out so the subject's existence is asserted OUTSIDE the xfail, where a failure is
    a failure.
    """
    name = _contract_version_name()
    assert name, "no exchange-version constant found"


def test_the_platform_asserts_the_trainers_contract_version() -> None:
    """The version exists so a platform/trainer skew fails at the exchange, loudly.

    MEASURED when this was written: eleven occurrences under `trainer/`, zero under
    `medos/medos/`. The only producer and the only consumer were in the same package, in the
    same image, so the value was compared against itself and always agreed.

    THE FIRST VERSION OF THIS CHECK ASKED THE WRONG QUESTION, and a file move answered it.
    It asserted that something under `medos/medos/` NAMES the constant -- a proxy for "asserts
    it". Then `executor.py` became `medos/medos/training/supervisor.py`, carrying the line
    `"contract_version": CONTRACT_VERSION` with it, and the proxy was satisfied: the strict
    xfail passed, the suite went red, and for a moment it looked as though the defect had
    been repaired by a rename.

    It had not. The platform still writes the value and compares it to nothing. Naming a
    constant is not checking it, which is the same mistake as a gate that greps for a
    substring instead of asking for the construct at its site -- the third of those in one
    day, wearing a different costume each time.

    So the question is now the one that was meant: does any `medos` module COMPARE the
    version? A `Compare` node, or a keyword argument to a refusal, with the constant on one
    side. Writing it into a document does not count and must not.
    """
    name = _contract_version_name()
    comparing: list[str] = []
    for path in _medos_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            if any(isinstance(o, ast.Name) and o.id == name for o in operands) or any(
                isinstance(o, ast.Attribute) and o.attr == name for o in operands
            ):
                rel = str(path.relative_to(ROOT)).replace("\\", "/")
                comparing.append(f"{rel}:{node.lineno}")

    assert comparing, (
        f"nothing under `medos/medos/` COMPARES {name} against anything. The platform "
        "writes it into the run request and the trainer writes it into its result, and "
        "the two are the same constant from the same installed package -- so the field "
        "records a fact nobody checks, and a version bump changes no test's colour. What "
        "is missing is a comparison at the exchange: the side that reads a document "
        "refusing one whose version it does not implement"
    )


@pytest.mark.xfail(
    strict=True,
    reason="register entry 112: the supervisor polls training_runs for PENDING and never "
           "claims the row. Fixing this turns THIS TEST RED -- close register entry 112 "
           "and delete the xfail marker.",
)
def test_the_pending_training_run_selection_claims_the_row_it_returns() -> None:
    """Two supervisors against one PENDING row stage the same run twice.

    MEASURED: `medos/medos/training/runs.py` holds zero `FOR UPDATE` and zero `SKIP LOCKED`,
    while `medos/medos/db/queue.py`, `medos/medos/worker/runner.py`,
    `medos/medos/bus/outbox.py`, `medos/medos/security/store.py` and
    `medos/medos/db/schema.sql` all use them -- so the pattern is not unknown here, it is
    absent from this one table.

    The only exclusion on the path is the optimistic `WHERE id = %s AND state = 'PENDING'`
    inside `runs.start`, and that is reached AFTER the run directory is staged and the
    plan phase has run. The loser does the work and then loses.

    Latent rather than active today, because the deployment runs one replica. Which is
    precisely the condition under which adding a second looks like a configuration change.
    """
    runs = (MEDOS / "training" / "runs.py").read_text(encoding="utf-8")
    assert re.search(r"FOR\s+UPDATE", runs, re.I), (
        "`medos/medos/training/runs.py` selects PENDING runs with a bare SELECT. The "
        "module that owns the platform's other queue, `medos/medos/db/queue.py`, uses "
        "`SELECT ... FOR UPDATE SKIP LOCKED` for exactly this"
    )


def test_the_claim_pattern_this_repository_already_uses_is_still_there() -> None:
    """The positive control for the test above, and it is not decoration.

    An xfail whose assertion fails because the thing it greps for was renamed repository-
    wide would look identical to an xfail whose defect still stands. This pins the fact
    that `FOR UPDATE SKIP LOCKED` is still how this repository claims queued work, so the
    xfail above keeps meaning "training_runs does not do it" rather than "nothing does".
    """
    users = [
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in list(_medos_sources()) + sorted(MEDOS.rglob("*.sql"))
        if re.search(r"SKIP\s+LOCKED", p.read_text(encoding="utf-8"), re.I)
    ]
    assert len(users) >= 3, (
        "fewer than three modules under `medos/medos/` still claim rows with `SKIP LOCKED`. If "
        "this repository changed how it claims queued work, register entry 112 is stated "
        "against a convention that no longer exists and needs rewriting. Found: "
        f"{users or 'none'}"
    )
