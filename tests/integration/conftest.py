# SPDX-License-Identifier: Apache-2.0
"""Postgres fixtures for the `medos.db` integration tests.

CONTRACT.md section 11: "Integration tests may assume Postgres and Orthanc from
`medos/deploy/compose/docker-compose.yml`." This conftest does not start a container -- it
connects to whatever `MEDOS_TEST_DATABASE_URL` (or `MEDOS_DATABASE_URL`) points at,
creates a throwaway database per session, applies `schema.sql` to it, and drops it
afterwards. A throwaway database rather than a throwaway schema because
`test_schema_applies_cleanly_from_empty` has to prove exactly that: from empty.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from medos.db.conn import apply_schema
from medos.db.migrate import set_role_password
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from psycopg.rows import dict_row

from tests._support import stack
from tests._support.skips import skip_infra

# ONE DEFINITION, AND THIS FILE IS NOT IT. `tests/_support/stack.py` owns the answer to
# "where is this deployment's Postgres" because it is also what the stack PROBE reads, and
# the two must not be able to disagree. They did: this file carried its own fallback of
# 127.0.0.1:55433 while `stack.py` fell back to 5432, so on a machine with neither variable
# set the probe reported "postgres READY" and this suite skipped 455 tests as "no Postgres"
# in the same run -- a green wall of skips with a healthy database three lines away.
#
# `stack.py`'s own docstring already names this exact shape as the first incident on this
# project ("a stale 127.0.0.1:55433 in one file and a live 5432 everywhere else"). It
# survived here because the note was written next to the copy that was RIGHT. Deferring
# rather than re-stating is the only fix that cannot drift again.
ADMIN_URL = stack.database_url()


# Dev-only credentials for the two roles `0002_tenancy.up.sql` creates without one. They
# exist so the tenancy test can open a connection that is NOT the bootstrap superuser --
# a superuser bypasses row-level security entirely, so a proof written on the fixture
# connection would prove nothing.
#
# APP_PASSWORD DEFAULTS TO THE COMPOSE DEFAULT ON PURPOSE. `ALTER ROLE ... PASSWORD` is
# CLUSTER-wide, not database-wide: the throwaway database this fixture creates does not
# contain the change, the server does. So a run against the development stack's Postgres
# rewrites the credential that `medos-api` and `medos-worker` are already using, and with
# any value other than docker-compose.yml's `${MEDOS_APP_PASSWORD:-medos_app}` the suite
# goes green while leaving both containers unable to log in -- precisely the "tests
# passed, system broken" failure this directory's skip taxonomy exists to stop. Set
# MEDOS_APP_DB_PASSWORD when the stack uses a non-default credential.
#
# A DEFAULT IS NOT A GUARANTEE, WHICH IS WHY `_adopt_role_password` EXISTS.
# Measured on the development stack while writing this:
#
#     ALTER ROLE medicalos_app PASSWORD 'deployment_secret'   -- i.e. the stack was
#                                                             -- started with one
#     pytest tests/integration/test_queue.py::... -q --require-stack
#     -> 1 passed, exit 0
#     medicalos_app/deployment_secret: REFUSED   <- after the green run
#     medicalos_app/medos_app:         OK
#
# One passing test silently rewrote the credential of a running deployment. medos-api,
# medos-worker and medos-gateway could no longer open a connection, and the suite said
# everything was fine. So the fixture no longer writes a credential it has not first
# established is safe to write.
APP_PASSWORD = os.environ.get("MEDOS_APP_DB_PASSWORD", "medos_app")
OWNER_PASSWORD = os.environ.get("MEDOS_OWNER_DB_PASSWORD", "medos_owner_test")

# Which env var an operator should set, per role, when the guard below refuses.
_PASSWORD_ENV = {
    "medicalos_app": "MEDOS_APP_DB_PASSWORD",
    "medicalos_owner": "MEDOS_OWNER_DB_PASSWORD",
}


def _admin_conn() -> psycopg.Connection[Any]:
    return psycopg.connect(ADMIN_URL, autocommit=True, row_factory=dict_row)


def _role_can_log_in(role: str, password: str) -> tuple[bool, str]:
    """Can `role` log in to the admin database with `password`? (ok, why-not)."""
    url = ADMIN_URL.split("://", 1)[-1].split("@", 1)[-1]
    try:
        psycopg.connect(f"postgresql://{role}:{password}@{url}", connect_timeout=10).close()
    except psycopg.OperationalError as exc:
        return False, f"{getattr(exc, 'sqlstate', None) or '?'}: {exc}"
    return True, ""


def _adopt_role_password(conn: psycopg.Connection[Any], role: str, password: str) -> None:
    """Set `role`'s password, but never OVERWRITE one the deployment is depending on.

    `ALTER ROLE ... PASSWORD` is cluster-wide. The throwaway database this session
    creates is not a sandbox for it: the change lands on the server that `medos-api`,
    `medos-worker` and `medos-gateway` are connected to, and it outlives the run.

    Three cases, and only the first two write anything:

      * the role has no password at all -- what `0002_tenancy.up.sql` leaves behind on a
        fresh cluster, and what CI gets. Nothing can be depending on it. Set it.
      * the role's password is already the one we want. Nothing to do, so do nothing:
        the safest write is the one that is not issued.
      * the role has a DIFFERENT password. Something put it there. Refuse, loudly, and
        name the environment variable that resolves it -- because the alternative is a
        green suite and three containers that cannot log in.
    """
    row = conn.execute(
        "SELECT rolpassword IS NOT NULL AS has_password FROM pg_authid WHERE rolname = %s",
        (role,),
    ).fetchone()
    if row is None:
        raise AssertionError(f"role {role!r} does not exist; the migration did not run")

    if row["has_password"]:
        ok, why = _role_can_log_in(role, password)
        if ok:
            return  # already exactly right
        raise AssertionError(
            f"REFUSING to overwrite the cluster-wide password of {role!r}.\n"
            f"  The role already has a credential and it is not the one this suite would\n"
            f"  set, so something else put it there -- on the development stack that is\n"
            f"  medos-api, medos-worker and medos-gateway, all of which would stop being\n"
            f"  able to log in the moment this fixture ran. ALTER ROLE is cluster-wide and\n"
            f"  the change outlives the run.\n"
            f"  probe:  {why}\n"
            f"  fix:    set {_PASSWORD_ENV.get(role, 'the role password env var')} to the\n"
            f"          value the deployment uses, or point MEDOS_TEST_DATABASE_URL at a\n"
            f"          throwaway cluster instead of the one serving the stack."
        )

    set_role_password(conn, role, password)


def _swap_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """A brand-new, empty database with `schema.sql` applied."""
    name = f"medos_test_{secrets.token_hex(6)}"
    try:
        admin = _admin_conn()
    except psycopg.OperationalError as exc:  # pragma: no cover
        skip_infra(
            f"no Postgres at {ADMIN_URL.rsplit('@', 1)[-1]}: {exc} "
            f"(set MEDOS_TEST_DATABASE_URL to point at a live server)",
            dependency="postgres",
        )
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _swap_dbname(ADMIN_URL, name)
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        # `apply_schema` is now schema.sql + every migration in medos/medos/db/migrations/,
        # so this database has tenant_id on every domain table, forced row-level
        # security, and the two pre-tenancy tables (0002_tenancy.up.sql).
        apply_schema(conn)
        # Credentials are deployment configuration, not schema: the migration creates
        # `medicalos_app` with LOGIN and no password on purpose. tests/integration/
        # test_tenancy.py connects as that role to prove the isolation at the DATABASE
        # level rather than through the repository.
        _adopt_role_password(conn, "medicalos_app", APP_PASSWORD)
        # LOGIN first: the migration creates the owner NOLOGIN, and `_adopt_role_password`
        # establishes "is this already the right credential?" by logging in. A NOLOGIN
        # role cannot answer that question, and a guard that cannot check is a guard that
        # waves things through.
        conn.execute("ALTER ROLE medicalos_owner LOGIN")
        _adopt_role_password(conn, "medicalos_owner", OWNER_PASSWORD)
        conn.execute(f'GRANT CONNECT ON DATABASE "{name}" TO medicalos_app')
    try:
        yield dsn
    finally:
        with _admin_conn() as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="session")
def pristine_dsn() -> Iterator[str]:
    """An empty database with NO schema applied, for the bootstrap test."""
    name = f"medos_bootstrap_{secrets.token_hex(6)}"
    try:
        admin = _admin_conn()
    except psycopg.OperationalError as exc:  # pragma: no cover
        skip_infra(
            f"no Postgres at {ADMIN_URL.rsplit('@', 1)[-1]}: {exc} "
            f"(set MEDOS_TEST_DATABASE_URL to point at a live server)",
            dependency="postgres",
        )
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _swap_dbname(ADMIN_URL, name)
    finally:
        with _admin_conn() as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(scope="session")
def api_key(pg_dsn: str) -> str:
    """One live API key for `DEFAULT_TENANT_ID`, as a `mos_dev_..._...` plaintext.

    Weeks 3-5 item 2 made the HTTP surface authenticated: `MOS-SEC-008` admits anonymous
    access on `/healthz`, `/readyz` and the OpenAPI document and nowhere else. Every test
    that talks to `/api/v1` therefore needs a credential, and this is it.

    Session-scoped because argon2id at `MOS-SEC-010`'s parameters costs ~240 ms per
    HASH as well as per verification, and minting one per test would add minutes to the
    run for no coverage. The key is issued with a 1-day expiry: long enough for any
    plausible suite, short enough that a copy leaking out of a CI log is worthless.

    `tests/integration/test_auth.py` mints its OWN keys for everything it asserts. This
    fixture exists so the weeks 1-2 API tests -- the control group for this change --
    keep testing what they were written to test.
    """
    from datetime import UTC, datetime, timedelta

    from medos.security import store

    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        minted, _record = store.issue(
            conn,
            tenant_id=DEFAULT_TENANT_ID,
            principal_kind="service_account",
            principal_id="11111111-1111-1111-1111-111111111111",
            created_by=store.BOOTSTRAP_OPERATOR_ID,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scope=["job.create", "job.read"],
            label="integration test suite",
            env="dev",
        )
        conn.commit()
    return minted.plaintext


@pytest.fixture()
def db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One caller-owned connection, with every table emptied first.

    TRUNCATE and not DELETE: `job_events` carries a BEFORE DELETE trigger that raises
    (MOS-EXEC-013), and TRUNCATE does not fire row-level triggers -- so the append-only
    guarantee holds for application code while the test harness can still start clean.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    # The tenant binding for the whole fixture connection.
    #
    # Production code MUST NOT do this -- MOS-STORE-226 requires `set_config(..., true)`
    # inside the transaction doing the work, because under PgBouncer in transaction pool
    # mode a session-level setting outlives the commit and is handed to the next tenant's
    # request on the same backend. `medos.db.tenancy.tenant_tx()` is the only production
    # path and it uses the transaction-local form;
    # test_tenancy.py::test_the_binding_does_not_outlive_its_transaction asserts that.
    #
    # It is correct HERE for a reason that does not generalise: this connection is owned
    # by one test, is never pooled, and serves one tenant for its whole life. The
    # alternative -- wrapping every one of the ~200 weeks 1-2 assertions in a transaction
    # helper -- would have rewritten the tests that are supposed to be the control group
    # for this change.
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute("TRUNCATE jobs, job_queue, job_events, job_steps, job_series, "
                 "results, result_measurements, result_dicom_objects CASCADE")
    conn.commit()
    # And the ambient binding for the repository chokepoint, which reads a ContextVar.
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant

        reset_current_tenant(token)
        conn.close()
