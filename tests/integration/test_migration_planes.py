# SPDX-License-Identifier: Apache-2.0
"""Core's migration set applies to an empty database with Train's absent. Proven.

WHY THIS IS AN INTEGRATION TEST AND NOT A STRUCTURAL ONE
---------------------------------------------------------
`tests/unit/test_migration_planes.py` asserts what is true of the FILES: that every
migration belongs to exactly one product, that the declaration matches who writes each
table, and that no Core migration so much as names a table Train creates. All of that
can be read off the source.

None of it proves the set APPLIES. A migration can reference a type, a function, a
trigger or a role that an omitted migration created, and no amount of grepping for
table names finds it -- the first attempt at this partition failed on
`touch_updated_at()`, a function from the bootstrap, and that failure was invisible to
every static check. So this file starts Postgres, creates an empty database, applies
`schema.sql` plus Core's eleven migrations, and looks at what is there.

WHAT IT ESTABLISHES
-------------------
That the PACS-and-models product can bootstrap a database without shipping a single
line of the model-preparation schema, and that the model-preparation product layers on
top without reordering anything. That is the whole claim the split rests on, and it is
the one a second repository would have to hold.

Spec: MOS-STORE-214, MOS-CONF-109.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import psycopg
import pytest
from medos.db.conn import apply_schema
from medos.db.migrate import PLANE_MIGRATIONS, apply_migrations, discover
from psycopg.rows import dict_row

# `tests/integration` is not a package, so the conftest is reached the way its
# siblings reach shared helpers: through `tests._support.stack`, which is where
# the DSN comes from in the first place.
from tests._support import stack
from tests._support.skips import skip_infra

ADMIN_URL = stack.database_url()


def _swap_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"

#: Tables the PACS-and-models product must have, and tables it must NOT. Named rather
#: than counted: a count tells you something changed and not what.
CORE_TABLES = ("jobs", "results", "deployments", "evaluation_runs", "dataset_versions")
TRAIN_TABLES = ("training_runs", "harvest_batches", "seal_runs")


@pytest.fixture
def empty_database() -> Iterator[str]:
    """A database with nothing in it. Not `pg_dsn`: that one arrives with every
    migration already applied, which is the state this test exists to avoid."""
    name = f"medos_planes_{secrets.token_hex(6)}"
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, row_factory=dict_row)
    except psycopg.OperationalError as exc:  # pragma: no cover
        skip_infra(f"no Postgres at {ADMIN_URL.rsplit('@', 1)[-1]}: {exc}",
                   dependency="postgres")
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _swap_dbname(ADMIN_URL, name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True, row_factory=dict_row) as adm:
            adm.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            adm.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _exists(conn: psycopg.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()[0])


def test_the_core_set_bootstraps_a_database_on_its_own(empty_database: str) -> None:
    """THE CLAIM THE SPLIT RESTS ON. No Train migration is present and none is needed."""
    with psycopg.connect(empty_database, autocommit=True) as conn:
        apply_schema(conn, migrate=False)
        applied = apply_migrations(conn, planes=("core",))

        assert set(applied) == PLANE_MIGRATIONS["core"], (
            f"applied {sorted(applied)}, expected {sorted(PLANE_MIGRATIONS['core'])}"
        )
        for table in CORE_TABLES:
            assert _exists(conn, table), f"core table {table} is missing"
        for table in TRAIN_TABLES:
            assert not _exists(conn, table), (
                f"{table} exists in a core-only database. Either a Core migration "
                f"creates it, or PLANE_MIGRATIONS puts its migration on the wrong side."
            )


def test_the_train_set_layers_on_top_without_reordering(empty_database: str) -> None:
    """Train applies Core's migrations and then its own, in the same global lexical
    order. Nothing is renumbered, so 0011 lands between 0010 and 0012 exactly as it
    always did -- the partition removes files, it never moves them."""
    with psycopg.connect(empty_database, autocommit=True) as conn:
        apply_schema(conn, migrate=False)
        apply_migrations(conn, planes=("core",))
        later = apply_migrations(conn, planes=("core", "train"))

        assert set(later) == PLANE_MIGRATIONS["train"], (
            f"a second pass applied {sorted(later)}; it should apply exactly the train "
            f"set, because Core's were already in the ledger"
        )
        for table in CORE_TABLES + TRAIN_TABLES:
            assert _exists(conn, table), f"{table} missing after both sets"


def test_applying_everything_at_once_reaches_the_same_schema(empty_database: str) -> None:
    """Core-then-Train and all-at-once must agree. If they did not, the split would
    have changed what a full deployment gets, which is the one thing it must not do."""
    with psycopg.connect(empty_database, autocommit=True) as conn:
        apply_schema(conn, migrate=False)
        apply_migrations(conn)  # no planes: every migration, as before the split
        one_shot = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            ).fetchall()
        }

    name = f"medos_planes_{secrets.token_hex(6)}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as adm:
        adm.execute(f'CREATE DATABASE "{name}"')
    staged_dsn = _swap_dbname(ADMIN_URL, name)
    try:
        with psycopg.connect(staged_dsn, autocommit=True) as conn:
            apply_schema(conn, migrate=False)
            apply_migrations(conn, planes=("core",))
            apply_migrations(conn, planes=("core", "train"))
            staged = {
                r[0]
                for r in conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                ).fetchall()
            }
        assert staged == one_shot, (
            f"staged-only: {sorted(staged - one_shot)}\n"
            f"one-shot-only: {sorted(one_shot - staged)}"
        )
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as adm:
            adm.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            adm.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_the_core_set_is_smaller_than_the_whole(empty_database: str) -> None:
    """A guard against the partition quietly becoming everything. If `planes=("core",)`
    ever returned the full set, every assertion above would still pass."""
    assert len(discover(planes=("core",))) < len(discover())
    assert len(discover(planes=("train",))) >= 1
