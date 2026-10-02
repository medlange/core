# SPDX-License-Identifier: Apache-2.0
"""A migration's BYTES are its identity, and this repository changed them with a comment.

WHAT HAPPENED
-------------
`medos/medos/db/migrate.py` records `sha256(path.read_bytes())` in `schema_migrations`
beside each applied version, and refuses to go forward when the file on disk no longer
digests to what the ledger says:

    "A migration is immutable once applied" -- migrate.py, and it means the bytes.

The pass that nested `medos/` inside its product directory rewrote every path it found,
including the ones inside migration comments -- `medos/db/tenancy.py` became
`medos/medos/db/tenancy.py` in 0002's header -- and, on the files it rewrote whole,
flipped 823 lines of `schema.sql` from LF to CRLF. Sixteen applied migrations and the
baseline changed digest. Every deployment that had already run them was bricked forward:
not "a comment is stale" but `migrate.py` refusing to apply anything else, ever.

The only check that could see it needed a live database (`tests/gate/test_migration_drift.py`),
so a unit run said nothing and the defect travelled in a commit whose subject line was
about directories.

WHY THE ADDRESSES INSIDE ARE NOT REPAIRED
-----------------------------------------
Some of those paths really are stale now. They stay stale. A migration file is the same
kind of object as a release record: what is written in it is WHAT WAS TRUE WHEN IT RAN,
and there is no edit that both fixes the address and leaves the digest alone. Register
entry 116 carries the list; `medos/medos/db/migrations/README.md` says it at the site.

WHAT THIS TEST ASKS
-------------------
Not "has the history been clean" -- it has not, and rewriting history is not the repair.
It asks the only question that can be answered about the present: **does each migration
still hold the bytes it held in the commit that added it.** A file being authored now is
untracked, has no adding commit, and is not asked about. The moment it is committed it is
frozen, which is exactly the rule `migrate.py` enforces at run time against a database
this suite does not have.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests._support.skips import skip_environment

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "medos" / "medos" / "db" / "migrations"

#: `0001_baseline` in the ledger. `migrate.py` digests this file the same way and records
#: it under `BASELINE_VERSION`, so it is a migration for this rule even though it is not
#: in the numbered directory.
BASELINE = ROOT / "medos" / "medos" / "db" / "schema.sql"


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - argv vector, no shell
        ["git", *args],  # noqa: S607
        cwd=str(ROOT), capture_output=True, check=False,
    )


def _adding_commit(path: Path) -> tuple[str, str] | None:
    """`(commit, path-as-it-was-named-then)` for the commit that added `path`.

    `--follow` is what makes this survive the move into `medos/medos/`: a rename is not a
    content change and must not read as one. `None` means the file has never been
    committed, which is a migration somebody is writing right now.
    """
    rel = path.relative_to(ROOT).as_posix()
    out = _git("log", "--follow", "--diff-filter=A", "--format=%H", "--name-only", "--", rel)
    lines = [line for line in out.stdout.decode("utf-8", "replace").splitlines() if line]
    if out.returncode != 0 or len(lines) < 2:
        return None
    # `git log` walks newest first; the ADD is the last pair in the output.
    return lines[-2], lines[-1]


def _files() -> list[Path]:
    return [BASELINE, *sorted(MIGRATIONS.glob("*.sql"))]


def test_no_applied_migration_holds_different_bytes_than_when_it_was_added() -> None:
    if _git("rev-parse", "--git-dir").returncode != 0:
        skip_environment(
            "not a git checkout, so the bytes a migration was added with cannot be read",
            detail="no-git",
        )

    files = _files()
    assert files, f"no migration files under {MIGRATIONS}; this check reads nothing"

    changed: list[str] = []
    unborn: list[str] = []
    for path in files:
        origin = _adding_commit(path)
        if origin is None:
            unborn.append(path.name)
            continue
        commit, then = origin
        shown = _git("show", f"{commit}:{then}")
        if shown.returncode != 0:
            unborn.append(path.name)
            continue
        if shown.stdout != path.read_bytes():
            changed.append(
                f"    {path.name}: added as {then} in {commit[:9]}, edited since "
                f"({len(shown.stdout)} bytes then, {len(path.read_bytes())} now)"
            )

    assert not changed, (
        "these migrations no longer hold the bytes they were added with:\n"
        + "\n".join(changed)
        + "\n  `medos/medos/db/migrate.py` records sha256 of each file in "
        "`schema_migrations` and REFUSES to apply anything further when the file on disk "
        "disagrees. Every database that already ran one of these is now stuck forward. A "
        "comment, a path, a line ending -- the digest does not distinguish them.\n"
        "  If an address inside one of these files is wrong, it stays wrong: see "
        "`medos/medos/db/migrations/README.md`."
    )
    assert len(unborn) < len(files), (
        "not one migration could be traced to the commit that added it, so this check "
        "compared nothing. `--follow` may have failed, or this is a shallow clone."
    )


def test_the_check_above_reads_the_baseline_too() -> None:
    """`schema.sql` is `0001_baseline` in the ledger, and it is not in the directory.

    It was the first file the rewrite changed and the one `test_migration_drift` named.
    A check that globbed only `migrations/*.sql` would have reported a pass on it.
    """
    assert BASELINE in _files()
    assert BASELINE.exists(), BASELINE
    assert "0001_baseline" in (
        (ROOT / "medos" / "medos" / "db" / "migrate.py").read_text(encoding="utf-8")
    ), "migrate.py no longer records the baseline under this version"
