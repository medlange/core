# SPDX-License-Identifier: Apache-2.0
"""The schema partitions between the two products, and a new migration must choose.

WHY THE SETS EXIST
------------------
MedicalOS ships two deployables. Of the 45 tables the migration set creates, 29 are
written only by Core, 12 only by `medos/medos/training/`, and 4 by nobody yet -- and there is
NOT ONE foreign key from a Core table into a Train table. The dependency runs one way,
which is what makes the partition possible at all: Core's subset stands alone and
Train's three layer on top of it.

Ownership is DERIVED here, from who writes each table, and compared against the
declaration in `medos/medos/db/migrate.py`. A hand-kept list would drift the first time
somebody added a table; this file recomputes the answer and fails when the two differ.

WHAT IS NOT DONE, AND WHY IT WOULD COST EVERY DEPLOYMENT
--------------------------------------------------------
The files are NOT renumbered. `version` is the whole `NNNN_name` string and the ledger
stores it beside a sha256 of the file's bytes (`MOS-STORE-214`), so renaming
`0011_curation` makes every existing database report that version missing and a new one
unapplied -- the ledger turned into noise, everywhere, to make a number look tidy. Gaps
cost nothing: `discover()` sorts lexically and does not care that Core skips 0011
between 0010 and 0012.

THE PROOF IS NOT HERE. That Core's set actually applies to an empty database is a claim
about Postgres, and it lives in `tests/integration/test_migration_planes.py` where a
database exists. This file asserts the things that are true of the FILES.

Spec: MOS-STORE-214, MOS-CONF-109.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from medos.db.migrate import BASELINE_VERSION, PLANE_MIGRATIONS, discover, plane_of

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "medos" / "medos" / "db" / "migrations"


def _created_tables(text: str) -> set[str]:
    return set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-z_]+)", text, re.I))


def _all_tables() -> set[str]:
    out: set[str] = set()
    for f in MIGRATIONS.glob("*.up.sql"):
        out |= _created_tables(f.read_text(encoding="utf-8"))
    return out


def _writer_plane(table: str) -> str:
    """Which package writes the table. `git grep` rather than an import: the question
    is about source, and importing every module to inspect SQL strings would be slower
    and no more true."""
    found = subprocess.run(
        [
            "git",
            "grep",
            "-lE",
            rf"INSERT INTO {table}\b|UPDATE {table}\b",
            "--",
            "medos/medos/",
        ],
        capture_output=True,
        text=True,
        cwd=REPO,
    ).stdout.split()
    # [2], not [1]. `git grep` returns `medos/medos/training/runs.py` now that `medos/`
    # is a product directory, so index 1 is the word "medos" for every hit and every
    # table read as Core. The failure was quiet in the worst way: three migrations
    # declared `train` were reported as writing Core tables, which reads like a
    # boundary violation rather than like an off-by-one in a path split.
    packages = {p.split("/")[2] for p in found if p.startswith("medos/medos/")}
    if packages == {"training"}:
        return "train"
    return "core" if packages else "unwritten"


# --------------------------------------------------------------------------------------
# the partition is complete and exclusive
# --------------------------------------------------------------------------------------


def test_every_migration_belongs_to_exactly_one_plane() -> None:
    """A new migration that nobody classified would default into Core and silently
    widen what the PACS-and-models product ships."""
    on_disk = {m.version for m in discover()}
    declared = set().union(*PLANE_MIGRATIONS.values())
    assert on_disk == declared, (
        f"unclassified: {sorted(on_disk - declared)}\n"
        f"declared but absent: {sorted(declared - on_disk)}\n"
        "Add it to medos.db.migrate.PLANE_MIGRATIONS. Whether the model-preparation "
        "product owns a table is a decision, not a default."
    )
    overlap = PLANE_MIGRATIONS["core"] & PLANE_MIGRATIONS["train"]
    assert not overlap, f"a migration is in both planes: {sorted(overlap)}"


def test_the_baseline_is_in_neither_plane() -> None:
    """`schema.sql` is not in either set because it is in both: every database begins
    from it, whichever product is being deployed."""
    for plane, versions in PLANE_MIGRATIONS.items():
        assert BASELINE_VERSION not in versions, f"{BASELINE_VERSION} is in {plane}"


@pytest.mark.parametrize("plane", sorted(PLANE_MIGRATIONS))
def test_discover_returns_exactly_that_planes_migrations(plane: str) -> None:
    assert {m.version for m in discover(planes=(plane,))} == PLANE_MIGRATIONS[plane]


def test_filtering_removes_files_and_never_reorders_them() -> None:
    """Core applying 0010 then 0012 must land where it would have if 0011 had been
    applied and dropped. Lexical order is what makes the gap harmless."""
    everything = [m.version for m in discover()]
    core = [m.version for m in discover(planes=("core",))]
    assert core == [v for v in everything if v in PLANE_MIGRATIONS["core"]]
    assert core == sorted(core)


def test_plane_of_refuses_an_unclassified_version() -> None:
    with pytest.raises(KeyError, match="belongs to no plane"):
        plane_of("9999_something_nobody_declared")


# --------------------------------------------------------------------------------------
# the declaration matches who actually writes the tables
# --------------------------------------------------------------------------------------


def test_each_migration_creates_tables_of_only_one_plane() -> None:
    """A migration that created a Core table and a Train table could not be assigned to
    either product without shipping the other's schema. None does today, and this is
    what says so when one is written."""
    mixed: list[str] = []
    for migration in discover():
        created = _created_tables(migration.path.read_text(encoding="utf-8"))
        planes = {_writer_plane(t) for t in created} - {"unwritten"}
        if len(planes) > 1:
            mixed.append(f"{migration.version}: {sorted(planes)} in {sorted(created)}")
    assert not mixed, (
        "these migrations create tables belonging to both products:\n  "
        + "\n  ".join(mixed)
        + "\nSplit the migration, or move the table."
    )


def test_the_declared_plane_matches_who_writes_the_tables() -> None:
    """The declaration in `migrate.py` is derived knowledge, so derive it again and
    compare. A table that changes hands should fail here rather than quietly leaving
    the schema on the wrong side of the boundary."""
    wrong: list[str] = []
    for migration in discover():
        created = _created_tables(migration.path.read_text(encoding="utf-8"))
        planes = {_writer_plane(t) for t in created} - {"unwritten"}
        if not planes:
            continue  # creates only tables nobody writes yet; declaration stands
        derived = planes.pop()
        declared = plane_of(migration.version)
        if derived != declared:
            wrong.append(f"{migration.version}: declared {declared}, writers say {derived}")
    assert not wrong, "\n  ".join(["declaration disagrees with the code:"] + wrong)


def test_no_core_migration_mentions_a_train_table() -> None:
    """THE PROPERTY THE PARTITION RESTS ON. Core's set must be applicable with Train's
    absent, so no Core migration may reference a table Train creates -- not in a foreign
    key, not in an index, not in a grant."""
    train_tables: set[str] = set()
    for version in sorted(PLANE_MIGRATIONS["train"]):
        train_tables |= _created_tables(
            (MIGRATIONS / f"{version}.up.sql").read_text(encoding="utf-8")
        )
    assert train_tables, "the train migrations create no tables; has the set moved?"

    offenders: list[str] = []
    for migration in discover(planes=("core",)):
        text = migration.path.read_text(encoding="utf-8")
        # comments are prose and may legitimately discuss the other product
        code = "\n".join(
            line for line in text.split("\n") if not line.lstrip().startswith("--")
        )
        for table in sorted(train_tables):
            if re.search(rf"\b{table}\b", code):
                offenders.append(f"{migration.version} references {table}")
    assert not offenders, (
        "a Core migration names a table the Train product creates:\n  "
        + "\n  ".join(offenders)
        + "\nCore must apply to an empty database with no Train migration present."
    )


def test_every_train_migration_has_a_down_file() -> None:
    """Splitting a set is only reversible if each half is. A Train migration with no
    `.down.sql` cannot be removed from a database that later drops the product."""
    missing = [
        v for v in sorted(PLANE_MIGRATIONS["train"])
        if not (MIGRATIONS / f"{v}.down.sql").exists()
    ]
    assert not missing, f"train migrations with no down file: {missing}"
