# SPDX-License-Identifier: Apache-2.0
"""`migration-drift` -- the check this project learned it needs the hard way.

NOT A §15.1.2 CHECK. Declared and argued in `tests/unit/test_gate_contract.py`'s
`LOCAL_EXTRA`, which is the only route by which `tests/gate/` may hold a module the
specification does not name.

WHAT WENT WRONG, AND WHY NO OTHER CHECK COULD SEE IT
------------------------------------------------------
The release-0.2.0 gate was fully green -- six checks, thirty-six passes, exit 0 -- while
the live deployment's database had none of 0.2.0's tables in it. Nothing was lying. Every
schema-shaped check in this package builds a THROWAWAY database and applies `schema.sql`
plus every migration to it from empty (`tests/gate/_evidence.py` argues at length that this
is the stronger claim, and it is: it proves the set applies in order, on the release's own
Postgres server, from nothing). What it cannot prove, and never claimed to, is that anyone
RAN them against the database the release actually serves from.

`MOS-STORE-214`'s `schema_migrations` ledger exists precisely so that question has an
answer. Nothing was asking it. So:

    the subject of this check is the DEPLOYED database and nothing else.

It is the only check in the 0.3.0 row that reads `MEDOS_TEST_DATABASE_URL` directly rather
than a database it created itself, and that is the entire point of it.

FOUR WAYS A LEDGER AND A TREE CAN DISAGREE, AND ALL FOUR ARE HERE
-------------------------------------------------------------------
  1. MISSING   a migration is on disk and not in the ledger -- the 0.2.0 failure exactly.
  2. EXTRA     a migration is in the ledger and not on disk: the deployment ran something
               this release does not ship, so the release cannot describe its own schema.
  3. DRIFTED   both, at different digests. `medos/medos/db/migrate.py` records `file_digest` so
               that editing an applied migration is detectable; a migration file is
               immutable once it has run ANYWHERE, and recovery from a bad forward
               migration is restore-from-backup, not an edit.
  4. PHANTOM   the ledger row is there and the DDL is not. A row can be written without
               its tables -- an `INSERT` by hand, a restore of the ledger alone, a
               migration that was marked applied to skip it. The ledger is then a
               statement nobody checked. So this module also asks the catalogue whether
               every table the migration set DECLARES actually exists.

(4) is what makes this a schema check rather than a bookkeeping check, and it is the form
in which the original defect would have been caught: 0.2.0's ledger rows and 0.2.0's tables
were both absent, but a ledger-only check can be satisfied by a ledger that was faked.

MOS-REL-009 RESPONSE WHEN THIS IS RED
--------------------------------------
Run the migrations. This is a Tier A failure of the DEPLOYMENT, not of the release's
contents, so the slip rule's cut list does not apply and nothing is cut: the failure names
the exact versions that are missing, extra or drifted, and the response is one command
against the named database.

Needs the deployment's Postgres. Needs no capability, no corpus and no docker CLI.

Spec: MOS-STORE-214, MOS-STORE-219, MOS-SEC-073, MOS-REL-004, MOS-REL-009, MOS-REL-012;
docs/spec/12-data-model.md §12.15.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest
from medos.db.conn import SCHEMA_PATH
from medos.db.migrate import BASELINE_VERSION, discover

from tests._support.skips import skip_infra
from tests.gate.conftest import DATABASE_URL

pytestmark = pytest.mark.gate_0_3_0

#: `CREATE TABLE [IF NOT EXISTS] <name>` in a migration's SQL. Schema-qualified names and
#: quoted names are admitted; the `...` of a comment block is not (the identifier pattern
#: requires a letter or underscore first).
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?",
    re.IGNORECASE,
)

#: Version strings are `NNNN_lower_snake`; the ledger's PRIMARY KEY is that text.
_VERSION_RE = re.compile(r"^\d{4}_[a-z0-9_]+$")


def _baseline_digest() -> str:
    """What `medos.db.migrate.apply_migrations` records for `0001_baseline`.

    `schema.sql` is migration 0001 and is recorded rather than reapplied, so its ledger
    digest is the digest of the file -- the same arithmetic, in the same place, that the
    runner does.
    """
    return "sha256:" + hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()


def _on_disk() -> dict[str, str]:
    """`{version: file_digest}` for the migration set this release ships."""
    out = {BASELINE_VERSION: _baseline_digest()}
    for migration in discover():
        out[migration.version] = migration.digest
    return out


def _declared_tables() -> dict[str, str]:
    """`{table: the version that creates it}` over `schema.sql` and every migration.

    A table created twice (0001 and a later `CREATE TABLE IF NOT EXISTS`) is attributed to
    the first version that declares it; the assertion below is existence, so which one owns
    it does not change the verdict.
    """
    out: dict[str, str] = {}
    sources = [(BASELINE_VERSION, SCHEMA_PATH.read_text(encoding="utf-8"))]
    sources += [(m.version, m.sql) for m in discover()]
    for version, sql in sources:
        for name in _CREATE_TABLE_RE.findall(sql):
            out.setdefault(name.lower(), version)
    return out


@pytest.fixture(scope="module")
def deployed_db() -> Any:
    """A read-only connection to the DEPLOYMENT's database. Not a throwaway.

    Autocommit and nothing but SELECT: this is the one place in the gate that touches the
    live database, and a check about whether somebody ran the migrations must not be the
    thing that runs them.
    """
    import psycopg
    from psycopg.rows import dict_row

    try:
        conn = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        skip_infra(
            f"no Postgres at {DATABASE_URL.rsplit('@', 1)[-1]}: {exc}. The deployed "
            f"schema cannot be compared with the shipped migration set.",
            dependency="postgres",
        )
    with conn:
        yield conn


def _ledger(conn: Any) -> dict[str, str]:
    row = conn.execute("SELECT to_regclass('public.schema_migrations') AS t").fetchone()
    assert row["t"] is not None, (
        "the deployed database has no `schema_migrations` table at all. MOS-STORE-214 "
        "makes the ledger part of every migration set, so a database without one has "
        "either never been migrated by this runner or was restored without it. Either "
        "way the deployed schema is undescribed."
    )
    rows = conn.execute(
        "SELECT version, file_digest FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {r["version"]: r["file_digest"] for r in rows}


# =====================================================================================
# The check
# =====================================================================================
def test_migration_drift_the_shipped_set_is_well_formed_and_contiguous() -> None:
    """The tree's half of the comparison, before it is compared with anything.

    A gap or a duplicate in the numbering makes "the ledger matches the set" ambiguous:
    two files claiming 0009 cannot both be the applied one, and a missing 0007 means an
    ordered runner would apply 0008 against a schema 0008 was not written for.
    """
    on_disk = _on_disk()
    bad = sorted(v for v in on_disk if not _VERSION_RE.match(v))
    assert not bad, f"migration versions that are not NNNN_lower_snake: {bad}"

    numbers = sorted(int(v.split("_", 1)[0]) for v in on_disk)
    assert numbers[0] == 1, f"the set does not begin at 0001 (MOS-STORE-214): {numbers[0]}"
    assert len(numbers) == len(set(numbers)), (
        f"two migrations share a number: {sorted(n for n in numbers if numbers.count(n) > 1)}"
    )
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"the migration numbers are not contiguous: {numbers}. An ordered runner cannot "
        f"tell a deliberate gap from a file that was never committed."
    )

    for migration in discover():
        down = migration.path.with_name(migration.version + ".down.sql")
        assert down.exists(), (
            f"{migration.version} has no `.down.sql`. The runner never executes one "
            f"(medos/medos/db/migrate.py says why), but the forward migration is tested "
            f"against a real database by reversing it, and a migration with no reverse has not "
            f"been tested that way."
        )


def test_migration_drift_the_deployed_ledger_is_the_shipped_set(deployed_db: Any) -> None:
    """THE CHECK. `schema_migrations` in the live database == the migrations on disk.

    Missing, extra and digest-drifted are reported separately because the responses
    differ: run them, find out what ran, and restore-from-backup respectively.
    """
    on_disk = _on_disk()
    ledger = _ledger(deployed_db)

    missing = sorted(set(on_disk) - set(ledger))
    extra = sorted(set(ledger) - set(on_disk))
    drifted = sorted(
        f"{v}: ledger {ledger[v][:20]}... disk {on_disk[v][:20]}..."
        for v in set(on_disk) & set(ledger)
        if ledger[v] != on_disk[v]
    )

    problems: list[str] = []
    if missing:
        problems.append(
            "NOT APPLIED to the deployed database (this is the 0.2.0 failure): "
            + ", ".join(missing)
        )
    if extra:
        problems.append(
            "applied to the deployed database but NOT SHIPPED by this release: "
            + ", ".join(extra)
        )
    if drifted:
        problems.append(
            "applied at a different digest -- the file was edited after it ran, which "
            "medos/medos/db/migrate.py treats as un-recoverable forward (MOS-STORE-214): "
            + "; ".join(drifted)
        )

    assert not problems, (
        f"the deployed schema does not match the shipped migration set at "
        f"{DATABASE_URL.rsplit('@', 1)[-1]}:\n  " + "\n  ".join(problems)
    )
    assert len(ledger) == len(on_disk) >= 2, (
        f"the ledger and the disk set agree at {len(ledger)} entries, which is too few "
        f"to be this release's migration set"
    )


def test_migration_drift_every_declared_table_exists_in_the_deployed_catalogue(
    deployed_db: Any,
) -> None:
    """The ledger is not taken at its word. See (4) in the module docstring.

    A `schema_migrations` row is a claim, and this asks the catalogue whether the claim is
    true. `to_regclass` and not `information_schema`: it answers for partitions and for
    tables the current role cannot select from, which is the case that matters when the
    gate connects as somebody other than the owner.
    """
    declared = _declared_tables()
    assert len(declared) > 30, (
        f"only {len(declared)} tables were parsed out of the migration set; the parser is "
        f"not reading the DDL and this check would pass by finding nothing to check"
    )
    absent = sorted(
        f"{table} (declared by {version})"
        for table, version in declared.items()
        if deployed_db.execute(
            "SELECT to_regclass(%s) AS t", (f"public.{table}",)
        ).fetchone()["t"]
        is None
    )
    assert not absent, (
        "the deployed database records these migrations as applied but does not have the "
        "tables they create:\n  "
        + "\n  ".join(absent)
        + "\nA ledger row is a claim about the schema; these are the ones the catalogue "
        "does not support."
    )
