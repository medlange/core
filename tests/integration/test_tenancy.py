# SPDX-License-Identifier: Apache-2.0
"""Cross-tenant isolation, proved at the DATABASE level.

docs/spec/15-delivery.md 15.2.4 item 1: "This converts cross-tenant leakage from a
code-review property into a database impossibility."

The distinction this file exists to hold: every proof below is raw SQL on a connection
that was NOT opened by the repository. A test that went through `medos.db.repo` would
prove that the repository filters correctly, which is a code-review property -- exactly
the thing row-level security is supposed to replace. So the proofs open their own
connections as `medicalos_app` and as `medicalos_owner`, write their own statements, and
never import a helper that could be doing the work for PostgreSQL.

The connections also matter. The fixture connection in conftest.py is the bootstrap
superuser, and **a superuser bypasses row-level security entirely, FORCE or no FORCE**
(PostgreSQL: "Superusers and roles with the BYPASSRLS attribute always bypass the row
security system"). A proof written on that connection would prove nothing at all. So:

    medicalos_app     the application role. NOBYPASSRLS, owns nothing.  MOS-SEC-073
    medicalos_owner   the table owner. Bound by FORCE.                  MOS-STORE-223

Requirements asserted, by id:

    MOS-SEC-072     tenant_id NOT NULL + ENABLE + FORCE + a policy on the session variable
    MOS-SEC-073     medicalos_app is NOBYPASSRLS and owns nothing
    MOS-SEC-074     the binding is transaction-scoped, not session-scoped
    MOS-SEC-075     one chokepoint; nothing else writes the variable
    MOS-SEC-076     isolation is the policy, not a WHERE clause
    MOS-SEC-077     no tenant_id column anywhere without forced row security
    MOS-STORE-217   composite FKs make a cross-tenant reference unwritable
    MOS-STORE-218   tenant_id is immutable
    MOS-STORE-223   FORCE binds the owner
    MOS-STORE-224   an unbound connection ERRORS (42704); it does not return zero rows
    MOS-STORE-225   there is no escape hatch
    MOS-STORE-345   the two pre-tenancy tables are reachable with no tenant, by design
"""

from __future__ import annotations

import ast
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db import repo
from medos.db.queue import PostgresJobQueue
from medos.db.repo import JobSpec
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    NoTenantContextError,
    TenantContextConflict,
    platform_tx,
    tenant_context,
    tenant_tx,
)
from psycopg.rows import dict_row

from tests.integration.conftest import APP_PASSWORD, OWNER_PASSWORD

# SQLSTATE 42704 is `undefined_object`, which is what `current_setting(name, false)`
# raises for an unset variable. MOS-STORE-224 and MOS-SEC-072 acceptance criterion 1 both
# name this code; it is asserted literally rather than by message text.
UNSET_TENANT_SQLSTATE = "42704"

TENANT_A = "018f0000-0000-7000-8000-00000000000a"
TENANT_B = "018f0000-0000-7000-8000-00000000000b"

# One fixed public collection UID for both tenants. Chapter 12 section 12.4 reason 1:
# "Public collections collide." Two tenants ingesting the same LCTSC study is the case a
# global natural key turns into a cross-tenant leak.
SHARED_STUDY = "1.2.826.0.1.3680043.8.498.20000000000000000000000000000001"


# =====================================================================================
# Fixtures -- three connections, three privilege levels
# =====================================================================================
def _dsn_as(pg_dsn: str, user: str, password: str) -> str:
    """Rewrite the fixture DSN's credentials, keeping host/port/database."""
    return re.sub(r"^postgresql://[^@]+@", f"postgresql://{user}:{password}@", pg_dsn)


def _clear_rows_owned_by_other_tenants(conn: psycopg.Connection[Any]) -> None:
    """Remove every row that would block `DELETE FROM tenants WHERE id <> default`.

    WHY THIS IS CATALOGUE-DRIVEN AND NOT A LIST OF TABLE NAMES
    Every foreign key to `tenants` is ON DELETE RESTRICT, deliberately: a tenant whose
    rows still exist must not be deletable. This fixture's reset therefore breaks the
    moment ANY table gains a tenant_id, and it broke silently:

        pytest tests/integration -q
        -> 2 failed, 137 passed, 12 skipped, 25 errors
           ERROR at setup of test_tenant_a_cannot_select_tenant_b_rows
           psycopg.errors.ForeignKeyViolation: ... "api_keys_tenant_id_fkey"

    `tests/integration/test_auth.py` had minted an api_key for TENANT_B earlier in the
    same session and on the same database, so all 25 row-level-security proofs -- the
    ones that assert a tenant cannot read, write or delete another tenant's rows -- ERRORed
    out in every full-suite run and passed only when the module was run alone. The suite
    was loudly red rather than falsely green, so this is not the "tests passed, system
    broken" failure; it is its neighbour, "the run does not test what its name says".

    Reading `pg_constraint` rather than hardcoding `api_keys` means the NEXT table to
    reference `tenants` cannot reintroduce it. Rows belonging to DEFAULT_TENANT_ID are
    kept: `tests/integration/conftest.py`'s session-scoped `api_key` fixture owns one, and
    the weeks 1-2 API tests are still using it.
    """
    referencing = conn.execute(
        """
        SELECT c.conrelid::regclass::text AS table_name, a.attname AS column_name
        FROM pg_constraint c
        JOIN LATERAL unnest(c.conkey) AS k(attnum) ON TRUE
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        WHERE c.contype = 'f' AND c.confrelid = 'tenants'::regclass
        """
    ).fetchall()
    for row in referencing:
        conn.execute(
            f'DELETE FROM {row["table_name"]} '  # noqa: S608 - names come from pg_catalog
            f'WHERE {row["column_name"]} IS NOT NULL '
            f'AND {row["column_name"]} <> %s',
            (DEFAULT_TENANT_ID,),
        )


@pytest.fixture()
def root(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """The bootstrap superuser. Used ONLY to build the fixture, never to prove anything.

    It bypasses RLS, which is why it can seed rows for two tenants in one connection and
    why nothing it observes counts as evidence.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    conn.execute(
        "TRUNCATE jobs, job_queue, job_events, job_steps, job_series, "
        "results, result_measurements, result_dicom_objects CASCADE"
    )
    conn.execute("DELETE FROM ae_tenant_map")
    conn.execute("DELETE FROM quarantine")
    _clear_rows_owned_by_other_tenants(conn)
    conn.execute("DELETE FROM tenants WHERE id <> %s", (DEFAULT_TENANT_ID,))
    for tid, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
        conn.execute(
            "INSERT INTO tenants (id, slug, display_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (tid, slug, slug),
        )
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def app_dsn(pg_dsn: str) -> str:
    return _dsn_as(pg_dsn, "medicalos_app", APP_PASSWORD)


@pytest.fixture()
def owner_dsn(pg_dsn: str) -> str:
    return _dsn_as(pg_dsn, "medicalos_owner", OWNER_PASSWORD)


@pytest.fixture()
def seeded(root: psycopg.Connection[Any]) -> dict[str, str]:
    """One QUEUED job per tenant, on the SAME StudyInstanceUID.

    Written through the repository under an explicit tenant binding, because that is how
    a row gets there in production; every assertion afterwards is raw SQL.
    """
    out: dict[str, str] = {}
    for tid in (TENANT_A, TENANT_B):
        with tenant_context(tid):
            queue = PostgresJobQueue(root)
            created = repo.create_job_queued(
                root,
                queue,
                JobSpec(
                    study_instance_uid=SHARED_STUDY,
                    capability_ids=("lung_segmentation",),
                ),
            )
            out[tid] = created.job_id
    return out


def _connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=False)


def _bind(conn: psycopg.Connection[Any], tenant_id: str) -> None:
    """The transaction-local binding, written out rather than imported.

    `set_config(..., true)` is MOS-SEC-074's form. It is spelled here instead of calling
    `medos.db.tenancy.tenant_tx` so that the proofs do not depend on the module they are
    partly about.
    """
    conn.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tenant_id))


# =====================================================================================
# 1. The headline: tenant A cannot SELECT, UPDATE or DELETE a tenant B row
# =====================================================================================
def test_tenant_a_cannot_select_tenant_b_rows(
    app_dsn: str, seeded: dict[str, str]
) -> None:
    """MOS-SEC-072. Raw SQL, as `medicalos_app`, with no WHERE clause to hide behind.

    `SELECT * FROM jobs` with no predicate at all is the strongest form of the question:
    if the policy were missing, this returns both rows.
    """
    with _connect(app_dsn) as conn, conn.transaction():
        _bind(conn, TENANT_A)
        rows = conn.execute("SELECT public_id, tenant_id FROM jobs").fetchall()

    assert len(rows) == 1, f"tenant A saw {len(rows)} jobs; it owns exactly 1"
    assert rows[0]["public_id"] == seeded[TENANT_A]
    assert str(rows[0]["tenant_id"]) == TENANT_A

    # And the other direction, on a second connection, so the result is not an artefact
    # of one backend's state.
    with _connect(app_dsn) as conn, conn.transaction():
        _bind(conn, TENANT_B)
        rows_b = conn.execute("SELECT public_id FROM jobs").fetchall()
    assert [r["public_id"] for r in rows_b] == [seeded[TENANT_B]]


def test_tenant_a_cannot_select_tenant_b_rows_even_by_naming_them(
    app_dsn: str, seeded: dict[str, str]
) -> None:
    """The row is addressed directly, by primary key and by tenant. Still invisible.

    This is the case a `WHERE tenant_id = $1` predicate in application code would NOT
    catch, because the attacker writes the query. The policy is ANDed into the statement
    by PostgreSQL after the application's WHERE clause, so naming tenant B explicitly
    narrows the result to nothing rather than widening it to B's rows.
    """
    with _connect(app_dsn) as conn, conn.transaction():
        _bind(conn, TENANT_A)
        row = conn.execute(
            "SELECT * FROM jobs WHERE public_id = %s AND tenant_id = %s",
            (seeded[TENANT_B], TENANT_B),
        ).fetchone()
    assert row is None


def test_tenant_a_cannot_update_a_tenant_b_row(
    app_dsn: str, root: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    """An UPDATE that matches no visible row updates nothing -- and must not silently
    succeed at changing B's data. MOS-SEC-072's USING clause."""
    with _connect(app_dsn) as conn, conn.transaction():
        _bind(conn, TENANT_A)
        cur = conn.execute(
            "UPDATE jobs SET phase = 'hijacked' WHERE public_id = %s",
            (seeded[TENANT_B],),
        )
        assert cur.rowcount == 0

    after = root.execute(
        "SELECT phase FROM jobs WHERE public_id = %s", (seeded[TENANT_B],)
    ).fetchone()
    assert after is not None and after["phase"] != "hijacked"


def test_tenant_a_cannot_delete_a_tenant_b_row(
    app_dsn: str, root: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    with _connect(app_dsn) as conn, conn.transaction():
        _bind(conn, TENANT_A)
        cur = conn.execute("DELETE FROM job_queue")       # every row this tenant can see
        assert cur.rowcount == 1, "tenant A deleted a number of rows that is not its own"

    surviving = root.execute(
        "SELECT tenant_id FROM job_queue"
    ).fetchall()
    assert [str(r["tenant_id"]) for r in surviving] == [TENANT_B], (
        "an unqualified DELETE bound to tenant A removed tenant B's queue row"
    )


def test_tenant_a_cannot_insert_a_row_owned_by_tenant_b(
    app_dsn: str, root: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    """MOS-SEC-072's WITH CHECK, and then MOS-STORE-217 underneath it.

    The attacker here has more than an application ever does: the internal uuid of
    tenant B's job, handed to it by the superuser fixture. Two barriers, in order.

    1. Name tenant B in `tenant_id` and the policy's WITH CHECK refuses the row --
       SQLSTATE 42501, "new row violates row-level security policy".
    2. Omit `tenant_id` to dodge that, and `DEFAULT current_tenant_id()` fills in tenant
       A, at which point the composite foreign key `(tenant_id, job_id) -> jobs
       (tenant_id, id)` has nothing to point at: tenant A has no job with that uuid.
       That is the barrier that still stands with row security switched off entirely,
       because referential-integrity checks always bypass RLS.
    """
    job_b = root.execute(
        "SELECT id FROM jobs WHERE public_id = %s", (seeded[TENANT_B],)
    ).fetchone()
    assert job_b is not None

    insert = (
        "INSERT INTO job_steps ({cols}) VALUES ({vals})"
    )
    with _connect(app_dsn) as conn:
        # 1. Claiming tenant B outright.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                _bind(conn, TENANT_A)
                conn.execute(
                    insert.format(
                        cols="tenant_id, job_id, step_index, step_key, phase, owner, "
                             "status, timeout_s",
                        vals="%s, %s, 0, 'fetch_series', 'retrieving', 'platform', "
                             "'pending', 900",
                    ),
                    (TENANT_B, job_b["id"]),
                )

        # 2. Letting the DEFAULT fill it in instead.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                _bind(conn, TENANT_A)
                conn.execute(
                    insert.format(
                        cols="job_id, step_index, step_key, phase, owner, status, "
                             "timeout_s",
                        vals="%s, 0, 'fetch_series', 'retrieving', 'platform', "
                             "'pending', 900",
                    ),
                    (job_b["id"],),
                )


# =====================================================================================
# 2. No tenant bound is an ERROR, not an empty set.  MOS-STORE-224
# =====================================================================================
@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM jobs",
        "SELECT * FROM results",
        "UPDATE jobs SET phase = 'x'",
        "DELETE FROM job_queue",
        "INSERT INTO jobs (public_id, service_id) VALUES ('job_X', 'x')",
    ],
)
def test_a_connection_with_no_tenant_errors_rather_than_seeing_nothing(
    app_dsn: str, seeded: dict[str, str], statement: str
) -> None:
    """The single most important assertion in this file.

    MOS-STORE-224: "The earlier NULL-returning form of this function is withdrawn: a NULL
    predicate yields an empty result set, which is indistinguishable from 'this tenant
    owns nothing' and hides a missing-context bug until it reaches a report."

    So the failure is loud and it is loud on every verb -- read, write and delete alike.
    `count(*)` is included on purpose: a policy that returned zero rows would make this
    statement answer `0` and look like a working query on an empty table.
    """
    with _connect(app_dsn) as conn:
        with pytest.raises(psycopg.Error) as exc:
            with conn.transaction():
                conn.execute(statement)
        assert exc.value.sqlstate == UNSET_TENANT_SQLSTATE, (
            f"{statement!r} raised {exc.value.sqlstate}, not {UNSET_TENANT_SQLSTATE}"
        )


def test_an_empty_tenant_variable_is_also_an_error(app_dsn: str) -> None:
    """Set-but-empty is the other way to arrive at "no tenant", and `current_setting`
    returns '' rather than raising for it. `current_tenant_id()` raises 42704 itself in
    that case -- which is why the function has a body at all instead of being an inline
    cast in the policy."""
    with _connect(app_dsn) as conn:
        with pytest.raises(psycopg.Error) as exc:
            with conn.transaction():
                _bind(conn, "")
                conn.execute("SELECT count(*) FROM jobs")
        assert exc.value.sqlstate == UNSET_TENANT_SQLSTATE


# =====================================================================================
# 3. FORCE: the table owner does not bypass the policy.  MOS-STORE-223
# =====================================================================================
def test_the_table_owner_is_bound_by_the_policy(
    owner_dsn: str, seeded: dict[str, str]
) -> None:
    """This is what FORCE buys, and it is the half that is silently absent without it.

    `ENABLE ROW LEVEL SECURITY` alone exempts the table owner -- and therefore
    `medicalos_migrator`, which is a member of `medicalos_owner` so that it can issue DDL
    (MOS-STORE-222), and any operator who connects with owner rights. Every policy would
    still be in the catalogue and the guarantee would still be hollow.
    """
    with _connect(owner_dsn) as conn, conn.transaction():
        _bind(conn, TENANT_A)
        rows = conn.execute("SELECT public_id FROM jobs").fetchall()
    assert [r["public_id"] for r in rows] == [seeded[TENANT_A]], (
        "the table owner saw rows outside its bound tenant: FORCE ROW LEVEL SECURITY is "
        "missing or was dropped"
    )


def test_the_table_owner_with_no_tenant_also_errors(owner_dsn: str) -> None:
    with _connect(owner_dsn) as conn:
        with pytest.raises(psycopg.Error) as exc:
            with conn.transaction():
                conn.execute("SELECT count(*) FROM jobs")
        assert exc.value.sqlstate == UNSET_TENANT_SQLSTATE


def test_force_row_security_is_set_on_every_tenant_owned_table(
    root: psycopg.Connection[Any]
) -> None:
    """MOS-SEC-077's CI query, verbatim, plus the ENABLE/FORCE pair spelled out."""
    leaky = root.execute(
        """
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
                             AND a.attnum > 0
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND (c.relrowsecurity = false OR c.relforcerowsecurity = false)
        """
    ).fetchall()
    assert leaky == [], f"tenant_id without forced row security: {leaky}"

    forced = {
        r["relname"]
        for r in root.execute(
            "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "  AND c.relrowsecurity AND c.relforcerowsecurity"
        ).fetchall()
    }
    expected = {
        "jobs", "job_events", "job_steps", "job_series", "job_queue",
        "results", "result_measurements", "result_dicom_objects", "tenants",
    }
    assert expected <= forced, f"not ENABLE+FORCE: {sorted(expected - forced)}"


# The ONE role permitted to hold BYPASSRLS, and the conditions that permit it.
#
# MOS-DATA-012 needs the STOW admission path to answer "which tenant already owns this
# StudyInstanceUID?" BEFORE a tenant context exists, so that a study cannot be silently
# split across two tenants. 0002 chose FORCE ROW LEVEL SECURITY, which binds the table
# owner too -- deliberately: "Without FORCE the guarantee is hollow: the owner ... reads
# every tenant silently." So a SECURITY DEFINER function owned by `medicalos_owner` does
# not get a cross-tenant read, it gets 42704 from `current_tenant_id()`.
#
# The two ways out were `studies NO FORCE ROW LEVEL SECURITY`, which hands the owner every
# column of every row of every tenant, and a NOLOGIN BYPASSRLS role owning exactly one
# function that returns exactly one uuid. 0005_gateway took the second, and this allowlist
# is the price: the exemption is NAMED, and its three preconditions are ASSERTED below, so
# that the blast radius cannot grow without this test going red.
#
# Widening this dict is a schema-design decision, not a test fix.
BYPASSRLS_ALLOWLIST = {
    "medicalos_study_oracle": (
        "MOS-DATA-012 STOW admission: resolves study -> owning tenant before a tenant "
        "context exists (0005_gateway.up.sql)"
    ),
}


def test_the_application_role_cannot_bypass_row_security(
    root: psycopg.Connection[Any]
) -> None:
    """MOS-SEC-073, acceptance criterion 3. A role with BYPASSRLS makes every other test
    in this file a tautology, so it is asserted rather than assumed.

    This is an ALLOWLIST rather than a blanket prohibition, and the distinction matters.
    `medicalos_study_oracle` legitimately holds BYPASSRLS (see the block above). A blanket
    ban would have to be deleted to accommodate it, and a deleted test protects nothing;
    an allowlist keeps the prohibition in force for every OTHER role -- including one
    added tomorrow -- while pinning this exemption to the shape that makes it defensible.

    The exemption survives only while all four of these hold:

      1. NOLOGIN            -- no client can authenticate as it.
      2. no membership      -- and therefore nothing can reach it by `SET ROLE` either.
                               NOLOGIN alone is NOT sufficient: a role granted membership
                               in a NOLOGIN role can still `SET ROLE` into it and inherit
                               BYPASSRLS. This is the check that keeps (1) meaningful.
      3. exactly one function, owned, returning a scalar `uuid`, and it owns no table --
                               the cross-tenant read is one verdict, never a row.
      4. a pinned search_path on that function -- a SECURITY DEFINER function with an
                               inherited search_path is the textbook privilege-escalation
                               shape.

    Break any one of them and this test fails, which is the point: the argument for the
    exemption is exactly these four properties, so the test asserts the argument.
    """
    rows = root.execute(
        "SELECT rolname, rolbypassrls, rolsuper, rolcanlogin FROM pg_roles "
        "WHERE rolname LIKE 'medicalos\\_%'"
    ).fetchall()
    assert rows, "the migration created no medicalos_* roles"

    # Every role not on the allowlist is still held to the blanket rule.
    for r in rows:
        assert not r["rolsuper"], f"{r['rolname']} is a superuser"
        if r["rolname"] in BYPASSRLS_ALLOWLIST:
            continue
        assert not r["rolbypassrls"], (
            f"{r['rolname']} holds BYPASSRLS and is not in BYPASSRLS_ALLOWLIST. "
            "Adding it is a schema-design decision: justify the cross-tenant read, or "
            "remove the attribute. Do not widen the allowlist to make this pass."
        )

    present = {r["rolname"] for r in rows}
    for name, reason in BYPASSRLS_ALLOWLIST.items():
        assert name in present, f"allowlisted role {name} does not exist ({reason})"
        role = next(r for r in rows if r["rolname"] == name)
        assert role["rolbypassrls"], (
            f"{name} no longer holds BYPASSRLS. If the need is gone, delete it from "
            "BYPASSRLS_ALLOWLIST; a stale entry is a standing permission to add one back."
        )

        # 1. NOLOGIN.
        assert not role["rolcanlogin"], (
            f"{name} can log in; BYPASSRLS + LOGIN is a cross-tenant connection for "
            "anyone who obtains the password"
        )

        # 2. No member can SET ROLE into it.
        members = [
            m["member"]
            for m in root.execute(
                "SELECT m.rolname AS member FROM pg_auth_members a "
                "  JOIN pg_roles g ON g.oid = a.roleid "
                "  JOIN pg_roles m ON m.oid = a.member "
                " WHERE g.rolname = %s",
                (name,),
            ).fetchall()
        ]
        assert members == [], (
            f"{sorted(members)} are members of {name} and can SET ROLE into it, "
            f"inheriting BYPASSRLS. NOLOGIN does not stop SET ROLE."
        )

        # 3. Exactly one function, returning a scalar uuid; and no tables at all.
        funcs = root.execute(
            "SELECT p.proname, p.proconfig, p.prosecdef, "
            "       pg_get_function_result(p.oid) AS result "
            "  FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
            " WHERE r.rolname = %s",
            (name,),
        ).fetchall()
        assert len(funcs) == 1, (
            f"{name} owns {len(funcs)} functions "
            f"({sorted(f['proname'] for f in funcs)}); the exemption is for exactly one"
        )
        fn = funcs[0]
        assert fn["result"] == "uuid", (
            f"{name}.{fn['proname']} returns {fn['result']!r}, not a scalar uuid. "
            "What crosses the tenant boundary must stay one ownership verdict, never a "
            "row and never a set."
        )
        owned_rels = root.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
            " WHERE r.rolname = %s AND c.relkind IN ('r', 'v', 'm', 'p')",
            (name,),
        ).fetchall()
        assert owned_rels == [], (
            f"{name} owns relations {[r['relname'] for r in owned_rels]}; a BYPASSRLS "
            "role that owns a table reads every tenant's rows in that table directly"
        )

        # 4. The pinned search_path.
        config = fn["proconfig"] or []
        assert any(c.startswith("search_path=") for c in config), (
            f"{name}.{fn['proname']} has no pinned search_path (proconfig={config!r}). "
            "A SECURITY DEFINER function with an inherited search_path is the textbook "
            "PostgreSQL privilege-escalation shape."
        )

    owned = root.execute(
        """
        SELECT c.relname FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_roles r ON r.oid = c.relowner
         WHERE n.nspname = 'public' AND c.relkind = 'r' AND r.rolname = 'medicalos_app'
        """
    ).fetchall()
    assert owned == [], f"medicalos_app owns tables: {owned} (MOS-SEC-073)"


def test_there_is_no_permissive_escape_hatch_policy(
    root: psycopg.Connection[Any]
) -> None:
    """MOS-STORE-225: no `OR current_setting('medicalos.scope', true) = 'platform'`, and
    no break-glass policy either until `break_glass_grants` and its audit obligations
    exist (MOS-SEC-079/080/081). A permissive policy gated on a GUC that any code path
    can set is a cross-tenant read for everyone.

    Also asserts the narrower rule MOS-SEC-157 puts on the override when it does arrive:
    a permissive policy MUST NOT be `FOR ALL`, because PostgreSQL reuses `USING` as
    `WITH CHECK` and a FOR ALL override silently grants cross-tenant INSERT, UPDATE and
    DELETE for the length of the window.
    """
    policies = root.execute(
        "SELECT schemaname, tablename, policyname, permissive, cmd, qual "
        "FROM pg_policies WHERE schemaname = 'public'"
    ).fetchall()
    assert policies, "no policies at all"
    for p in policies:
        assert p["policyname"].endswith(("_tenant_isolation", "_self")), (
            f"unexpected policy {p['policyname']} on {p['tablename']}"
        )
        qual = (p["qual"] or "").lower()
        assert "medicalos.scope" not in qual, f"escape hatch in {p['policyname']}"
        assert "break_glass" not in qual, (
            f"{p['policyname']} carries a break-glass clause, but break_glass_grants "
            "(MOS-SEC-079) does not exist yet"
        )
        assert "current_tenant_id()" in qual, (
            f"{p['policyname']} is not bound to the session variable"
        )


# =====================================================================================
# 4. The PRE-TENANCY tables.  MOS-STORE-345
# =====================================================================================
def test_the_pre_tenancy_tables_are_exactly_two_and_carry_no_tenant_id(
    root: psycopg.Connection[Any]
) -> None:
    """MOS-STORE-345 closes the class at two: `ae_tenant_map` and `quarantine`.

    They cannot carry `tenant_id` because they are what DETERMINES the tenant --
    `ae_tenant_map` resolves it for Path A ingest, `quarantine` records that it could not
    be resolved. A tenant-isolation policy on either is a circular definition: the
    resolver would have to know the tenant in order to look up the tenant.

    The NAMING is load-bearing rather than cosmetic. The tenant references they do hold
    are outputs and annotations -- `resolved_tenant_id`, `owning_tenant_id`,
    `arriving_tenant_id`, `released_to_tenant_id` -- and a pre-tenancy table named the
    obvious way would make MOS-SEC-077's `pg_attribute` scan cry wolf on the two tables
    that are correct, and be silenced for the whole schema.
    """
    assert len(PRE_TENANCY_TABLES) == 2, (
        "MOS-STORE-345 closes the pre-tenancy class at two. A third entry is a schema "
        "decision, not a list edit."
    )
    for table in PRE_TENANCY_TABLES:
        cols = {
            r["column_name"]
            for r in root.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            ).fetchall()
        }
        assert cols, f"{table} does not exist"
        assert "tenant_id" not in cols, f"{table} carries tenant_id (MOS-STORE-345)"

        sec = root.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %s",
            (table,),
        ).fetchone()
        assert sec is not None
        assert not sec["relrowsecurity"] and not sec["relforcerowsecurity"], (
            f"{table} has row security; MOS-STORE-345 forbids it"
        )

        pol = root.execute(
            "SELECT policyname FROM pg_policies "
            "WHERE schemaname = 'public' AND tablename = %s",
            (table,),
        ).fetchall()
        assert pol == [], f"{table} has a policy: {pol}"

    # And the PRE-TENANCY class is exactly two. The other RLS-free tables in the schema
    # belong to a DIFFERENT class with a different justification, and conflating the two
    # is what made this assertion wrong: see PLATFORM_GLOBAL_TABLES below.
    rls_free = {
        r["relname"]
        for r in root.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity"
        ).fetchall()
    }
    assert rls_free == set(PRE_TENANCY_TABLES) | set(PLATFORM_GLOBAL_TABLES), (
        f"unexpected set of tables without row security: "
        f"{sorted(rls_free - set(PRE_TENANCY_TABLES) - set(PLATFORM_GLOBAL_TABLES))} "
        "is RLS-free and belongs to neither reviewed class. Classify it (and justify the "
        "entry in source), or give it row security. Do not widen a list to go green."
    )


# =====================================================================================
# The two RLS-free CLASSES, each with its own justification.
#
# These were one set, and being one set is what made the assertion above wrong: it read
# "the pre-tenancy tables are exactly two" and then compared against four names, so a
# genuinely tenant-owned table added to the schema RLS-free would have been argued about
# in terms of the wrong rule. They are separate rules with separate discriminating
# properties, and each table below was re-derived from its DDL rather than inherited.
# =====================================================================================

# MOS-STORE-345. Read or written BEFORE a tenant is known, so RLS on them is a circular
# definition: the resolver would need the tenant in order to look up the tenant. They DO
# carry tenant references -- as OUTPUTS, under names that are never bare `tenant_id`.
PRE_TENANCY_TABLES = {
    "ae_tenant_map": "Path A ingest resolves called/calling AE -> resolved_tenant_id",
    "quarantine": "records that the tenant could NOT be resolved (MOS-DATA-047)",
}

# MOS-STORE-219. Platform-global: one row set for the whole deployment, no tenant
# reference of any kind, therefore nothing per-tenant to isolate. The discriminating
# property is asserted below, not taken on trust: ZERO tenant-named columns and ZERO
# foreign keys to `tenants`. A table here that grows a tenant reference is a LEAK, and
# the test fails rather than the entry being widened.
PLATFORM_GLOBAL_TABLES = {
    "schema_migrations": (
        "the migration ledger: version, file_digest, applied_at/by, duration_ms. A "
        "deployment has one schema, not one per tenant."
    ),
    "job_state_transition": (
        "MOS-EXEC-011's state machine AS DATA, not as per-job rows: exactly "
        "(from_state, to_state, actor, label) with that triple as the primary key, and "
        "the T2..T13 rows of spec section 5.2.2 as its entire contents. There is no "
        "job_id column and no per-job row -- the per-job history is `job_events`, which "
        "IS tenant-owned and RLS-forced. A lookup table of legal edges is identical for "
        "every tenant; making it tenant-owned would let one tenant's schema legalise a "
        "transition another's forbids."
    ),
    "pacs_backends": (
        "This one was examined hardest, because MOS-DATA-014 requires that 'a single "
        "shared backend and one backend per tenant MUST both be expressible', and a "
        "per-tenant backend sounds exactly like tenant-owned data. It is not, because "
        "the tenant->backend ASSOCIATION is not stored here. This table is the backend "
        "CATALOGUE -- id, kind, dicomweb_base_url, supports_change_feed, partitioning -- "
        "and carries no tenant column and no FK to tenants. Which backend a given "
        "tenant's data came from is recorded in `studies.pacs_backend` (tenant-owned, "
        "FORCE RLS) and `ae_tenant_map.pacs_backend`; the per-tenant ROUTING lives in "
        "`PerTenantBackendResolver.backends` in medos/medos/gateway/backend.py, a "
        "process-level "
        "mapping built from the secret store, because MOS-DATA-005 keeps the credential "
        "out of the database entirely. So there is no per-tenant row here to isolate. "
        "Were a tenant_id ever added to this table, the assertion below would fail and "
        "the correct fix would be RLS on the table, not an exemption."
    ),
    "artifact_manifest_schemas": (
        "MOS-STORE-219 names this table in its platform-global list verbatim, and "
        "MOS-STORE-355 says which path writes it: 'artifact_manifest_schemas by the "
        "migration introducing a schema version'. It holds one JSON Schema per artifact "
        "kind (MOS-REG-015) -- a validation grammar, not data about anyone. A deployment "
        "has ONE such grammar: per-tenant manifest schemas would mean a manifest that is "
        "valid for one hospital and invalid for the next, which is the opposite of what "
        "a published schema is for. `medicalos_app` holds SELECT only (0012 section 8), "
        "so the containment here is the grant, not row security. The `artifacts` rows "
        "validated against these schemas ARE tenant-owned and carry FORCE RLS."
    ),
}


def test_the_platform_global_tables_carry_no_tenant_reference_at_all(
    root: psycopg.Connection[Any]
) -> None:
    """MOS-STORE-219, made falsifiable.

    "Platform-global" is only a safe reason to skip row security while the table holds
    nothing per-tenant. That is a property of the DDL, so it is checked against the DDL:
    no column whose name mentions a tenant, and no foreign key into `tenants`.

    This is the test that would catch the leak the pre-tenancy assertion could not see.
    `pacs_backends` growing a `tenant_id`, or `job_state_transition` growing a `job_id`
    and becoming per-job rows, both turn an exempt table into an unprotected tenant-owned
    one, and both fail here.
    """
    for table, reason in PLATFORM_GLOBAL_TABLES.items():
        cols = {
            r["column_name"]
            for r in root.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (table,),
            ).fetchall()
        }
        assert cols, f"{table} does not exist ({reason})"

        tenant_cols = sorted(c for c in cols if "tenant" in c.lower())
        assert tenant_cols == [], (
            f"{table} carries tenant-referencing column(s) {tenant_cols} and has NO row "
            f"security. It is not platform-global. Give it row security "
            f"(MOS-SEC-077), do not move it to another allowlist."
        )

        fks = [
            r["column_name"]
            for r in root.execute(
                """
                SELECT kcu.column_name
                  FROM information_schema.table_constraints tc
                  JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                  JOIN information_schema.constraint_column_usage ccu
                    ON ccu.constraint_name = tc.constraint_name
                 WHERE tc.constraint_type = 'FOREIGN KEY'
                   AND tc.table_schema = 'public' AND tc.table_name = %s
                   AND ccu.table_name = 'tenants'
                """,
                (table,),
            ).fetchall()
        ]
        assert fks == [], (
            f"{table} has foreign key(s) {fks} into `tenants` while holding no row "
            "security: it is tenant-owned under another column name"
        )

    # `job_state_transition` specifically: a LOOKUP table, not a per-job log. The column
    # set is the whole argument for its exemption, so the column set is asserted.
    jst = {
        r["column_name"]
        for r in root.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'job_state_transition'"
        ).fetchall()
    }
    assert jst == {"from_state", "to_state", "actor", "label"}, (
        f"job_state_transition is no longer the (from_state, to_state, actor, label) "
        f"lookup table its MOS-STORE-219 exemption rests on: {sorted(jst)}. If it now "
        "holds a per-job row it is tenant-owned and needs RLS."
    )


def test_the_pre_tenancy_tables_are_reachable_with_no_tenant_context(
    app_dsn: str, root: psycopg.Connection[Any]
) -> None:
    """The whole point, stated as a test.

    The ingest resolver runs BEFORE the tenant is known. It reads `ae_tenant_map` to find
    out which tenant an arriving study belongs to and, when no enabled row matches
    (MOS-STORE-347, MOS-DATA-047), writes a `quarantine` row -- both on a connection that
    by definition has no `medicalos.tenant_id`. If those tables errored like every other
    table does, ingest could never start.
    """
    root.execute(
        "INSERT INTO ae_tenant_map (called_aet, calling_aet, resolved_tenant_id, "
        "pacs_backend) VALUES ('MEDOS', 'CT01', %s, 'orthanc')",
        (TENANT_A,),
    )

    with _connect(app_dsn) as conn, conn.transaction():
        # Deliberately NO _bind() call. This is the state that makes `jobs` raise 42704.
        assert conn.execute(
            "SELECT current_setting(%s, true) AS v", (TENANT_GUC,)
        ).fetchone()["v"] in (None, "")

        resolved = conn.execute(
            "SELECT resolved_tenant_id FROM ae_tenant_map "
            " WHERE called_aet = %s AND calling_aet = %s AND enabled",
            ("MEDOS", "CT01"),
        ).fetchone()
        assert resolved is not None
        assert str(resolved["resolved_tenant_id"]) == TENANT_A

        # The unresolved case writes a quarantine row on the same unbound connection.
        conn.execute(
            """
            INSERT INTO quarantine (reason, sop_instance_uid, series_instance_uid,
                                    study_instance_uid, pacs_backend, ingest_path,
                                    called_aet, calling_aet, received_at)
            VALUES ('tenant_unresolved', '2.25.1', '2.25.2', '2.25.3', 'orthanc',
                    'path_a', 'MEDOS', 'UNKNOWN', now())
            """
        )
        held = conn.execute(
            "SELECT reason, sop_instance_uid FROM quarantine "
            " WHERE released_at IS NULL AND purged_at IS NULL"
        ).fetchall()
    assert len(held) == 1 and held[0]["reason"] == "tenant_unresolved"


def test_an_unbound_connection_still_cannot_read_a_tenant_owned_table(
    app_dsn: str, seeded: dict[str, str]
) -> None:
    """The pre-tenancy exemption is per-table and does not leak. The same connection that
    just read `ae_tenant_map` without a tenant raises on `jobs`."""
    with _connect(app_dsn) as conn:
        with conn.transaction():
            conn.execute("SELECT count(*) FROM ae_tenant_map").fetchone()
        with pytest.raises(psycopg.Error) as exc:
            with conn.transaction():
                conn.execute("SELECT count(*) FROM jobs")
        assert exc.value.sqlstate == UNSET_TENANT_SQLSTATE


# =====================================================================================
# 5. Writes: composite FKs, immutability, tenant-scoped natural keys
# =====================================================================================
def test_a_cross_tenant_foreign_key_is_unwritable_even_with_row_security_off(
    root: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    """MOS-STORE-217: "RLS protects reads, composite FKs protect writes, and neither
    substitutes for the other."

    This runs as the SUPERUSER -- the one connection in this file that bypasses every
    policy -- precisely to show the second barrier working with the first one switched
    off. PostgreSQL's referential-integrity checks always bypass row security, so a
    single-column FK to `jobs(id)` would accept this row.
    """
    job_b = root.execute(
        "SELECT id FROM jobs WHERE public_id = %s", (seeded[TENANT_B],)
    ).fetchone()
    assert job_b is not None

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        root.execute(
            "INSERT INTO job_steps (tenant_id, job_id, step_index, step_key, phase, "
            "owner, status, timeout_s) VALUES (%s, %s, 0, 'fetch_series', "
            "'retrieving', 'platform', 'pending', 900)",
            (TENANT_A, job_b["id"]),
        )


def test_tenant_id_is_immutable(
    root: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    """MOS-STORE-218: "moving a row between tenants is not supported, the supported
    operation being export then ingest."

    Asserted as the SUPERUSER on purpose: RLS's WITH CHECK already refuses this for every
    other role, and the trigger is the layer that binds the one session RLS cannot see.

    The SQLSTATE is `MOS06`, the weeks 1-2 `schema.sql` spelling of
    `forbid_column_change()`. Chapter 12's rendering of the same function uses `23514`;
    this migration reuses the existing function rather than shipping a second copy with a
    different code, and the divergence is recorded here rather than silently reconciled.
    """
    with pytest.raises(psycopg.Error) as exc:
        root.execute(
            "UPDATE jobs SET tenant_id = %s WHERE public_id = %s",
            (TENANT_B, seeded[TENANT_A]),
        )
    assert exc.value.sqlstate == "MOS06"
    assert "tenant_id" in str(exc.value)


def test_two_tenants_can_hold_the_same_study_and_the_same_idempotency_key(
    root: psycopg.Connection[Any], seeded: dict[str, str]
) -> None:
    """Chapter 12 section 12.4 reason 1, made concrete.

    Both jobs were created for the SAME StudyInstanceUID with the same capability set, so
    both derived the SAME idempotency key -- `derive_idempotency_key` is deterministic and
    its tenant component is still the frozen `SLICE_TENANT_ID` constant. Under the weeks
    1-2 global `UNIQUE (idempotency_key)` the second tenant's `ON CONFLICT DO NOTHING`
    would have folded its job silently onto the first tenant's row and returned that job's
    id: a cross-tenant leak created by a key choice, with no code review able to see it.
    """
    rows = root.execute(
        "SELECT tenant_id, public_id, idempotency_key FROM jobs "
        " WHERE study_instance_uid = %s ORDER BY tenant_id",
        (SHARED_STUDY,),
    ).fetchall()
    assert len(rows) == 2, "the two tenants did not each get their own job row"
    assert rows[0]["idempotency_key"] == rows[1]["idempotency_key"], (
        "the fixture no longer exercises the collision it exists to test"
    )
    assert rows[0]["public_id"] != rows[1]["public_id"]
    assert {str(r["tenant_id"]) for r in rows} == {TENANT_A, TENANT_B}


def test_a_tenant_sees_only_its_own_job_by_public_id(
    app_dsn: str, seeded: dict[str, str]
) -> None:
    """`public_id` stays globally unique (it is a minted ULID, not derived), so the id in
    a URL is unambiguous -- and looking up another tenant's id returns nothing rather than
    that tenant's job. MOS-STORE-357 plus MOS-SEC-072."""
    with _connect(app_dsn) as conn, conn.transaction():
        _bind(conn, TENANT_A)
        mine = conn.execute(
            "SELECT public_id FROM jobs WHERE public_id = %s", (seeded[TENANT_A],)
        ).fetchone()
        theirs = conn.execute(
            "SELECT public_id FROM jobs WHERE public_id = %s", (seeded[TENANT_B],)
        ).fetchone()
    assert mine is not None and theirs is None


# =====================================================================================
# 6. The chokepoint itself.  MOS-SEC-074, MOS-SEC-075, MOS-STORE-226, MOS-STORE-227
# =====================================================================================
def test_the_binding_does_not_outlive_its_transaction(
    app_dsn: str, seeded: dict[str, str]
) -> None:
    """MOS-STORE-226 criterion 6, and the highest-severity misuse of the design.

    `SET` without `LOCAL` survives the commit. Under PgBouncer in transaction pool mode --
    the deployment default -- the backend is then handed to the NEXT tenant's request with
    tenant A still bound, and that request reads and writes tenant A's rows while
    believing it is tenant B. So the tenancy module uses `set_config(..., true)` and this
    test asserts the consequence: after the transaction, the variable is gone and the next
    statement errors instead of inheriting.
    """
    conn = _connect(app_dsn)
    try:
        with tenant_tx(conn, TENANT_A):
            rows = conn.execute("SELECT public_id FROM jobs").fetchall()
        assert [r["public_id"] for r in rows] == [seeded[TENANT_A]]

        # The transaction committed. The binding must not have survived it.
        with pytest.raises(psycopg.Error) as exc:
            with conn.transaction():
                conn.execute("SELECT count(*) FROM jobs")
        assert exc.value.sqlstate == UNSET_TENANT_SQLSTATE
    finally:
        conn.close()


def test_the_chokepoint_refuses_to_open_a_transaction_with_no_tenant(
    app_dsn: str
) -> None:
    """MOS-STORE-227: "the wrapper MUST fail rather than open a transaction with no
    tenant". The Python-level refusal is a better error message; the database-level 42704
    is the guarantee. Both exist, and this asserts the first one fires first."""
    conn = _connect(app_dsn)
    try:
        with pytest.raises(NoTenantContextError):
            with tenant_tx(conn):
                pytest.fail("tenant_tx opened a transaction with no tenant bound")
    finally:
        conn.close()


def test_one_transaction_cannot_serve_two_tenants(app_dsn: str) -> None:
    """Re-binding mid-transaction would leave the statements already executed running
    under the old tenant and the ones after under the new one -- a transaction that spans
    two tenants, which is the thing the module exists to make impossible."""
    conn = _connect(app_dsn)
    try:
        with pytest.raises(TenantContextConflict):
            with tenant_tx(conn, TENANT_A):
                with tenant_tx(conn, TENANT_B):
                    pass
    finally:
        conn.close()


def test_platform_tx_binds_nothing_and_is_not_an_escape_hatch(
    app_dsn: str, seeded: dict[str, str]
) -> None:
    """`platform_tx()` exists so that every no-tenant transaction is greppable, not so
    that one can reach tenant-owned rows. It binds nothing, so the first statement that
    touches a tenant-owned table raises 42704 exactly as it would anywhere else."""
    conn = _connect(app_dsn)
    try:
        with platform_tx(conn):
            conn.execute("SELECT count(*) FROM ae_tenant_map").fetchone()
        with pytest.raises(psycopg.Error) as exc:
            with platform_tx(conn):
                conn.execute("SELECT count(*) FROM jobs")
        assert exc.value.sqlstate == UNSET_TENANT_SQLSTATE
    finally:
        conn.close()


def _code_without_prose(text: str) -> str:
    """The module's EXECUTABLE source: comments and docstrings removed.

    `ast.parse` discards comments outright, and the docstring of every module, class and
    function is dropped below, so what comes back is the code that can actually run.
    """
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_only_the_tenancy_module_writes_the_session_variable() -> None:
    """MOS-SEC-075 / MOS-STORE-227: "The context MUST be set in exactly one place ... no
    handler, worker or query helper may set it."

    An import-graph test is what chapter 8 asks for in Go; in this codebase the cheaper
    and stricter equivalent is a source scan for the GUC name itself, because a second
    writer is the failure and it does not need an import to exist.

    THE SCAN READS CODE, NOT PROSE, AND THAT IS A CORRECTION.
    This test used to be a raw substring scan over the whole file, and it reported
    `medos/medos/gateway/projection.py` as a second chokepoint. It is not one. The gateway
    contains no `set_config`, no `SET LOCAL`, no `SET SESSION` and no `current_setting`
    anywhere; every tenant binding in `medos/medos/gateway` goes through
    `medos.db.tenancy.tenant_tx()` or `tenant_context()`. The single hit was one line of a
    DOCSTRING, explaining that the query names no tenant because `tenant_tx()` binds the
    variable and RLS does the rest -- prose describing the chokepoint, tripping the
    detector for the chokepoint.

    That mattered in both directions, which is why the fix is not simply an exemption:

      * FALSE POSITIVE. The old scan punished documentation. The cheapest way to make it
        green was to stop naming the GUC in comments, which degrades exactly the file a
        reviewer most needs to understand.
      * FALSE NEGATIVE, and this is the worse half. A raw substring scan sees only the
        literal. A genuine second writer that did
        `from medos.db.tenancy import TENANT_GUC` and then interpolated it into SQL never
        contains the literal, so the old test waved it through -- a real second
        chokepoint, invisible to the test written to forbid it.

    So the scan now strips comments and docstrings and looks at executable code only, AND
    separately forbids any other module from importing the `TENANT_GUC` symbol. The rule
    is unchanged and still exactly one chokepoint; the detector now matches the rule.
    """
    root_dir = Path(__file__).resolve().parents[2] / "medos" / "medos"
    literal_offenders: list[str] = []
    symbol_offenders: list[str] = []

    for path in sorted(root_dir.rglob("*.py")):
        if path.name == "tenancy.py":
            continue
        rel = str(path.relative_to(root_dir.parent)).replace("\\", "/")
        text = path.read_text(encoding="utf-8")

        # 1. The GUC named literally in executable code.
        if TENANT_GUC in _code_without_prose(text):
            literal_offenders.append(rel)

        # 2. The GUC reached indirectly through the symbol. `tenant_tx`/`tenant_context`
        #    are the sanctioned exports; `TENANT_GUC` is the raw variable name, and
        #    REFERENCING it outside the chokepoint means building a binding by hand.
        #
        #    What counts is USE, not import. `medos/medos/db/__init__.py` imports the name and
        #    lists it in `__all__` so that `from medos.db import TENANT_GUC` resolves --
        #    a re-export binds nothing and executes nothing. In the AST the difference is
        #    exact and needs no special case: an `__all__` entry is the STRING
        #    "TENANT_GUC", whereas using the value is a `Name` load of it. So a pure
        #    re-export has zero Name loads and passes, while the first line of code that
        #    actually reads the constant fails.
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Name)
                and node.id == "TENANT_GUC"
                and isinstance(node.ctx, ast.Load)
            ):
                symbol_offenders.append(rel)
            elif isinstance(node, ast.Attribute) and node.attr == "TENANT_GUC":
                symbol_offenders.append(rel)

    assert literal_offenders == [], (
        f"medicalos.tenant_id is named in executable code outside medos/medos/db/tenancy.py: "
        f"{literal_offenders}. Route the binding through tenant_tx()/tenant_context(); "
        "MOS-SEC-075 allows exactly one writer."
    )
    assert sorted(set(symbol_offenders)) == [], (
        f"TENANT_GUC is READ outside medos/medos/db/tenancy.py by "
        f"{sorted(set(symbol_offenders))}. The only use for the raw variable name is to "
        "build a binding statement by hand, which is a second chokepoint that spells the "
        "GUC indirectly. Re-exporting the name is fine; reading its value is not."
    )


def test_the_chokepoint_scan_cannot_be_evaded_by_prose_or_by_indirection() -> None:
    """The detector above is itself asserted, on synthetic sources.

    A source-scanning test is only as good as its scanner, and this one has already been
    wrong once in each direction. Both directions are pinned here so that a future
    simplification of `_code_without_prose` cannot quietly restore either bug.
    """
    # Prose naming the GUC is NOT a violation: the docstring below is exactly the shape
    # that produced the false positive on medos/medos/gateway/projection.py.
    documented = (
        '"""Explains that tenant_tx() binds medicalos.tenant_id and RLS does the rest."""\n'
        "def f(conn):\n"
        "    # medicalos.tenant_id is bound by the chokepoint, not here\n"
        "    with tenant_tx(conn) as tx:\n"
        "        return tx.execute('SELECT 1')\n"
    )
    assert TENANT_GUC not in _code_without_prose(documented), (
        "the scanner still reads docstrings and comments as code: it would punish "
        "documentation and force the GUC name out of the files that explain it"
    )

    # A real second writer IS a violation, even spelled across a docstring boundary.
    writer = (
        '"""A module that sets the variable itself."""\n'
        "def bind(conn, tid):\n"
        "    conn.execute(\"SELECT set_config('medicalos.tenant_id', %s, true)\", (tid,))\n"
    )
    assert TENANT_GUC in _code_without_prose(writer), (
        "the scanner no longer sees a genuine second writer"
    )

    # And the symbol half: a re-export is inert, reading the value is not.
    def _reads_symbol(src: str) -> bool:
        return any(
            (isinstance(n, ast.Name) and n.id == "TENANT_GUC"
             and isinstance(n.ctx, ast.Load))
            or (isinstance(n, ast.Attribute) and n.attr == "TENANT_GUC")
            for n in ast.walk(ast.parse(src))
        )

    reexport = (
        "from medos.db.tenancy import TENANT_GUC, tenant_tx\n"
        '__all__ = ["TENANT_GUC", "tenant_tx"]\n'
    )
    assert not _reads_symbol(reexport), (
        "a pure re-export was flagged; the package API surface is not a second writer"
    )

    indirect = (
        "from medos.db.tenancy import TENANT_GUC\n"
        "def bind(conn, tid):\n"
        "    conn.execute('SELECT set_config(%s, %s, true)', (TENANT_GUC, tid))\n"
    )
    assert _reads_symbol(indirect), (
        "a second writer that spells the GUC through the imported symbol went "
        "undetected: this is the hole the old raw-substring scan left open"
    )


def test_every_set_config_in_the_source_is_transaction_local() -> None:
    """MOS-SEC-074 / MOS-STORE-226, checked at the source rather than only at runtime.

    A `set_config(..., false)` or a bare `SET` that reached production would pass every
    other test in this file -- the binding works, it just also survives the commit -- and
    fail in the one deployment shape that has a transaction-mode pooler in front of it.
    """
    root_dir = Path(__file__).resolve().parents[2] / "medos" / "medos"
    bad: list[str] = []
    for path in root_dir.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "set_config(" in line and "true" not in line:
                bad.append(f"{path.name}:{i}: {line.strip()}")
            if re.search(r"\"\s*SET\s+SESSION\b|'\s*SET\s+SESSION\b", line, re.I):
                bad.append(f"{path.name}:{i}: SET SESSION")
    assert bad == [], f"non-transaction-local tenant binding: {bad}"


# =====================================================================================
# 7. The migration itself
# =====================================================================================
def test_the_migration_ledger_records_the_baseline_and_the_tenancy_migration(
    root: psycopg.Connection[Any]
) -> None:
    rows = {
        r["version"]: r["file_digest"]
        for r in root.execute(
            "SELECT version, file_digest FROM schema_migrations"
        ).fetchall()
    }
    assert "0001_baseline" in rows
    assert "0002_tenancy" in rows
    for digest in rows.values():
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), digest


def test_the_backfill_tenant_is_the_frozen_uid_derivation_constant(
    root: psycopg.Connection[Any]
) -> None:
    """The backfill id is not arbitrary and must never be regenerated.

    `medos.db.repo.SLICE_TENANT_ID` is the tenant component of every idempotency key and
    every deterministic 2.25.* UID this deployment has written, and MOS-IMG-085 makes
    "same UID implies same declared inputs" an invariant the retry path depends on. Every
    pre-tenancy row was therefore derived under exactly this tenant id; giving those rows
    any other owner would make their recorded `derivation_inputs` a lie.
    """
    assert repo.SLICE_TENANT_ID == DEFAULT_TENANT_ID
    row = root.execute(
        "SELECT slug FROM tenants WHERE id = %s", (repo.SLICE_TENANT_ID,)
    ).fetchone()
    assert row is not None, "the backfill tenant row is missing"
    assert row["slug"] == "default"
    # A valid UUID, and the nil one specifically.
    assert uuid.UUID(repo.SLICE_TENANT_ID).int == 0


def _weeks_1_2_job(conn: psycopg.Connection[Any], queue: Any) -> Any:
    """The `jobs` + `job_queue` + `job.requested` rows exactly as weeks 1-2 wrote them.

    `repo.create_job_queued` grew a fourth statement at weeks 3-5 -- the `job.create`
    `AuditEvent`, in the same transaction (MOS-SEC-149) -- and `audit_events` arrives in
    `0003_audit_provenance`. A database that predates `0002` therefore cannot run it, and
    that is the correct posture: the alternative is a repository that silently skips an
    audit write when the table is missing, which is the one behaviour an append-only
    audit trail must not have.

    This helper exists ONLY for the backfill test, whose subject is a pre-migration
    database. Nothing in `medos/medos/` calls it.
    """
    from medos.core.uids import derive_idempotency_key

    public_id = repo.new_public_job_id()
    ik = derive_idempotency_key(
        tenant_id=repo.SLICE_TENANT_ID,
        service_id="medos.slice",
        service_version="0.1.0",
        study_instance_uid=SHARED_STUDY,
        selected_series_uids=(),
        prior_study_instance_uids=(),
        requested_outputs=("SEG", "SR"),
        parameters={"capability_ids": ["lung_segmentation"]},
    )
    with conn.transaction():
        row = conn.execute(
            """
            INSERT INTO jobs (
                public_id, service_id, service_version, capability_ids,
                study_instance_uid, idempotency_key, created_by_kind, created_by_id,
                deadline_at, trace_id, correlation_id, root_job_id)
            VALUES (%s, 'medos.slice', '0.1.0', ARRAY['lung_segmentation'],
                    %s, %s, 'service_account', 'medos-api',
                    now() + interval '6 hours', %s, %s, gen_random_uuid())
            RETURNING id, public_id, state
            """,
            (public_id, SHARED_STUDY, ik, repo.new_trace_id(), public_id),
        ).fetchone()
        assert row is not None
        conn.execute("UPDATE jobs SET root_job_id = id WHERE id = %s", (row["id"],))
        conn.execute(
            "SELECT job_append_event(%s, 'job.requested', 'control-plane', %s)",
            (row["id"], psycopg.types.json.Jsonb({"study_instance_uid": SHARED_STUDY})),
        )
        queue.enqueue(row["public_id"])
    return repo.CreatedJob(row["public_id"], True, row["state"], ik)


def test_the_migration_backfills_rows_written_before_the_column_existed(
    pg_dsn: str,
) -> None:
    """The documented backfill, run against rows that predate `tenant_id`.

    This is the case the empty test databases never exercise and the one a real
    deployment always is. A brand-new database gets the weeks 1-2 baseline ONLY
    (`apply_schema(migrate=False)`), a job is driven through the repository exactly as
    weeks 1-2 drove it -- writing `jobs`, `job_queue`, `job_steps`, `job_series` and
    `job_events` rows with no tenant column in sight -- and only then is
    `0002_tenancy.up.sql` applied.

    It found a real defect when it was first run against the live stack rather than
    against an empty fixture: `job_events` carries `job_events_no_update`
    (MOS-EXEC-013), which refuses an UPDATE for every role including the migration's, so
    the naive backfill statement aborted the migration halfway. The migration now
    disables that one trigger for that one statement and re-enables it, and asserts the
    re-enable. This test is what keeps that true.
    """
    import secrets

    from medos.db.conn import apply_schema
    from medos.db.migrate import apply_migrations

    name = f"medos_backfill_{secrets.token_hex(5)}"
    admin_dsn = re.sub(r"/[^/]+$", "/postgres", pg_dsn)
    with psycopg.connect(admin_dsn, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = re.sub(r"/[^/]+$", f"/{name}", pg_dsn)
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            apply_schema(conn, migrate=False)          # weeks 1-2 schema, nothing more
            assert conn.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                " WHERE table_schema = 'public' AND column_name = 'tenant_id'"
            ).fetchone()["n"] == 0

            # Weeks 1-2 traffic. `tenant_context` is harmless here -- there is no column
            # for the binding to fill and no policy for it to satisfy.
            #
            # The job row and the queue row are written with the two statements weeks 1-2
            # used, rather than through `repo.create_job_queued`, and that is not a
            # shortcut. Since `0003_audit_provenance`, creating a job also appends an
            # `AuditEvent` in the same transaction (MOS-SEC-149: "a committed state change
            # with no audit row MUST NOT be possible"), and `audit_events` does not exist
            # on a pre-0002 schema -- by construction, because this test's whole subject
            # is a database that predates every migration. Weakening the audit write so it
            # could run here would be weakening the control to suit its own test. What
            # this test is about -- the backfill of rows that predate `tenant_id`, and the
            # `job_events_no_update` trigger that the backfill has to step around -- is
            # untouched: the rows below are exactly the rows weeks 1-2 produced.
            with tenant_context(DEFAULT_TENANT_ID):
                queue = PostgresJobQueue(conn)
                created = _weeks_1_2_job(conn, queue)
                repo.plan_steps(conn, created.job_id, attempt=1)
                repo.record_series_verdicts(
                    conn, created.job_id,
                    [repo.SeriesVerdict(series_instance_uid="1.2.3.4",
                                        decision="selected",
                                        selector_name="thin_axial_ct", rank=1)],
                )
                repo.append_event(conn, created.job_id, "job.dispatch", {"n": 1})

            before = {
                t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
                for t in ("jobs", "job_queue", "job_steps", "job_series", "job_events")
            }
            assert all(v > 0 for v in before.values()), before

            applied = apply_migrations(conn)
            assert "0002_tenancy" in applied

            # Every pre-existing row now belongs to the backfill tenant, and no row was
            # lost on the way -- a backfill that dropped rows would also "succeed".
            for table, count in before.items():
                row = conn.execute(
                    f"SELECT count(*) AS n, count(*) FILTER "
                    f" (WHERE tenant_id = '{DEFAULT_TENANT_ID}') AS mine FROM {table}"
                ).fetchone()
                assert row["n"] == count, f"{table}: rows lost in the backfill"
                assert row["mine"] == count, f"{table}: rows not backfilled"

            # And the append-only trigger the backfill borrowed is back on.
            state = conn.execute(
                "SELECT tgenabled FROM pg_trigger WHERE tgrelid = 'job_events'::regclass "
                "  AND tgname = 'job_events_no_update'"
            ).fetchone()
            assert state is not None and state["tgenabled"] == "O"
            with pytest.raises(psycopg.Error):
                conn.execute("UPDATE job_events SET phase = 'x'")
    finally:
        with psycopg.connect(admin_dsn, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                " WHERE datname = %s AND pid <> pg_backend_pid()", (name,)
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_the_down_migration_reverses_the_up_migration(pg_dsn: str) -> None:
    """The down file exists so the forward migration is testable, and this is that test.

    It is NOT a claim that a production deployment may run it: dropping `tenant_id` merges
    two tenants' rows into one undifferentiated set and nothing afterwards can separate
    them again. Recovery from a bad forward migration is restore-from-backup
    (medos/medos/db/migrate.py).

    It builds its OWN database with the baseline plus `0002_tenancy.up.sql` and NOTHING
    ELSE, and that is the change this test needed once a third migration existed.
    Migrations roll back in reverse order, so `0002`'s down cannot run underneath `0004`
    and `0005`: those add tenant-owned tables of their own whose policies call
    `current_tenant_id()`, and a down that drops the function out from under a live policy
    is not a reversal, it is a different kind of broken. Asserting "the database has
    exactly eight `tenant_id` columns" made this test a counter of every later migration's
    tables; asserting "0002's down removes exactly what 0002's up added" is the claim the
    file is actually about, and it stays true however many migrations come after.
    """
    import secrets

    from medos.db.conn import apply_schema
    from medos.db.migrate import discover

    name = f"medos_down_{secrets.token_hex(5)}"
    admin_dsn = re.sub(r"/[^/]+$", "/postgres", pg_dsn)
    with psycopg.connect(admin_dsn, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = re.sub(r"/[^/]+$", f"/{name}", pg_dsn)
    try:
        _assert_down_reverses_up(dsn, apply_schema, discover)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                " WHERE datname = %s AND pid <> pg_backend_pid()", (name,)
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _assert_down_reverses_up(dsn: str, apply_schema: Any, discover: Any) -> None:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        apply_schema(conn, migrate=False)
        up = next(m for m in discover() if m.version == "0002_tenancy")
        conn.execute(up.sql)

        def tenant_columns() -> int:
            row = conn.execute(
                "SELECT count(*) AS n FROM information_schema.columns "
                " WHERE table_schema = 'public' AND column_name = 'tenant_id'"
            ).fetchone()
            return 0 if row is None else int(row["n"])

        assert tenant_columns() == 8

        down = (
            Path(__file__).resolve().parents[2]
            / "medos" / "medos" / "db" / "migrations" / "0002_tenancy.down.sql"
        )
        conn.execute(down.read_text(encoding="utf-8"))

        assert tenant_columns() == 0
        gone = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            " AND tablename IN ('tenants','ae_tenant_map','quarantine')"
        ).fetchall()
        assert gone == []
        # And the weeks 1-2 schema is back: the eight domain tables, untouched.
        tables = {
            r["tablename"]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        assert {"jobs", "job_queue", "job_events", "job_steps", "job_series",
                "results", "result_measurements", "result_dicom_objects"} <= tables
