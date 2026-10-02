# SPDX-License-Identifier: Apache-2.0
"""Connection helper for the weeks 1-2 slice.

CONTRACT.md section 1: "conn.py -- connection helper".
CONTRACT.md section 11: "No global mutable state. Pass the connection; do not import a
singleton."

There is therefore no module-level connection, no module-level pool and no `get_conn()`
that reaches for one. Every function in `medos.db` takes a `psycopg.Connection` as its
first argument. The reason is not style: a singleton connection makes
`CONTRACT.md section 8`'s "the results row and the terminal state transition MUST be
written in ONE transaction" unenforceable, because two call sites that each fetch "the"
connection cannot tell whether they are inside one transaction or two. Passing the
connection makes the transaction boundary visible in the call graph.

Transaction discipline used across `medos.db`
---------------------------------------------
Every write helper wraps its work in `with conn.transaction():`. psycopg 3 makes that
one construct do the right thing in both positions:

  - called on a connection with no open transaction, it BEGINs and COMMITs on exit;
  - called inside a caller's open transaction, it is a SAVEPOINT and the caller commits.

So `PostgresJobQueue.enqueue()` composes into the caller's job-creation transaction
(MOS-EXEC-034 -- one transaction, no outbox), while `PostgresJobQueue.claim()` called
from a bare runner loop commits on its own (MOS-EXEC-025: Claim runs in its own
transaction). No method needs a `commit` flag and no caller needs to know which it got.

Spec: MOS-EXEC-034, MOS-EXEC-025, MOS-STORE-275, CONTRACT.md sections 8 and 11.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

__all__ = [
    "SCHEMA_PATH",
    "DEFAULT_DSN",
    "dsn_from_env",
    "connect",
    "apply_schema",
    "schema_sql",
]


SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Matches medos/deploy/compose/docker-compose.yml's postgres service. Overridden by
# MEDOS_DATABASE_URL, which is what CI and the integration tests set.
DEFAULT_DSN = "postgresql://medos:medos@127.0.0.1:5432/medos"


def dsn_from_env(env: dict[str, str] | None = None) -> str:
    """Resolve the connection string.

    `MEDOS_DATABASE_URL` wins; otherwise the `MEDOS_DB_*` parts are assembled; otherwise
    `DEFAULT_DSN`. The password is read from the environment and MUST NOT be logged --
    it is not PHI, but CONTRACT.md section 11's structured-JSON logging rule has no
    exception that would make printing it acceptable either.
    """
    env = dict(os.environ) if env is None else env
    url = env.get("MEDOS_DATABASE_URL")
    if url:
        return url
    if any(k.startswith("MEDOS_DB_") for k in env):
        host = env.get("MEDOS_DB_HOST", "127.0.0.1")
        port = env.get("MEDOS_DB_PORT", "5432")
        user = env.get("MEDOS_DB_USER", "medos")
        password = env.get("MEDOS_DB_PASSWORD", "medos")
        name = env.get("MEDOS_DB_NAME", "medos")
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    return DEFAULT_DSN


def connect(
    dsn: str | None = None,
    *,
    autocommit: bool = False,
    application_name: str = "medos",
    **kwargs: Any,
) -> psycopg.Connection[dict[str, Any]]:
    """Open one connection with the row factory the rest of `medos.db` assumes.

    `dict_row` and not the default tuple row: every helper in `repo.py` returns mappings,
    and a positional row factory turns "someone inserted a column in schema.sql" into a
    silent field shift instead of a KeyError.

    `autocommit=False` is the default on purpose -- see the module docstring. Pass
    `autocommit=True` only for DDL-ish work (`apply_schema`, CREATE DATABASE) and for a
    `LISTEN` session, which cannot hold a transaction open.
    """
    conn = psycopg.connect(
        dsn or dsn_from_env(),
        row_factory=dict_row,
        autocommit=autocommit,
        application_name=application_name,
        **kwargs,
    )
    return conn


def schema_sql() -> str:
    """The single DDL document, as text. CONTRACT.md section 8: there is no other."""
    return SCHEMA_PATH.read_text(encoding="utf-8")


def apply_schema(conn: psycopg.Connection[Any], *, migrate: bool = True) -> None:
    """Bootstrap an EMPTY database: `schema.sql`, then every migration in order.

    `schema.sql` is still the weeks 1-2 baseline and is still the only file that creates
    the eight domain tables (CONTRACT.md section 8). What changed at weeks 3-5 is that it
    is no longer the ONLY DDL: `0002_tenancy.up.sql` adds `tenant_id`, the `tenants`
    table, row-level security and the two pre-tenancy tables, and it does that by
    ALTERing what this file created rather than by rewriting it. Editing `schema.sql` in
    place would have been a lie about deployments that already hold rows.

    Running it twice still fails on `CREATE TYPE job_state`, which is still the correct
    outcome: it means the caller thought the database was empty and it was not. The
    migrations, by contrast, are recorded in `schema_migrations` and are skipped when
    already applied (`medos.db.migrate`).

    `migrate=False` stops after the baseline. It exists for one caller -- the test that
    proves the weeks 1-2 schema is exactly what `0002` migrates FROM -- and production
    code MUST NOT use it: a database with `tenant_id` columns and no row-level security
    is the one state MOS-SEC-077 exists to make impossible.
    """
    sql = schema_sql()
    if conn.autocommit:
        conn.execute(sql)  # type: ignore[arg-type]
    else:
        with conn.transaction():
            conn.execute(sql)  # type: ignore[arg-type]
    if migrate:
        from medos.db.migrate import apply_migrations

        apply_migrations(conn)


def iter_tables(conn: psycopg.Connection[Any]) -> Iterator[str]:
    """Table names in the public schema -- used by the tests to assert the table set."""
    cur = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    for row in cur.fetchall():
        yield row["tablename"] if isinstance(row, dict) else row[0]
