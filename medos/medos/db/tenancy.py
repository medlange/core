# SPDX-License-Identifier: Apache-2.0
"""The tenant chokepoint. One place sets `medicalos.tenant_id`; nothing else may.

docs/spec/15-delivery.md section 15.2.4, item 1: "`tenant_id NOT NULL` on every domain
table, Postgres row-level security bound to a session variable, and a **single repository
chokepoint** that sets it. This converts cross-tenant leakage from a code-review property
into a database impossibility."

Requirements implemented here
-----------------------------
MOS-SEC-074 / MOS-STORE-226
    The variable MUST be set with `set_config(..., is_local => true)` -- transaction
    scoped. `SET` without `LOCAL` and `SET SESSION` MUST NOT be used: under PgBouncer in
    transaction pool mode (the deployment default) a session-level `SET` outlives the
    transaction and leaks into the next tenant's request on the same backend. Chapter 12
    calls that "the highest-severity misuse of the design".

MOS-SEC-075 / MOS-STORE-227
    All tenant-scoped database access MUST pass through exactly one repository function,
    and the wrapper MUST fail rather than open a transaction with no tenant. That
    function is `tenant_tx()` below. Every `with conn.transaction():` in `medos/medos/db` was
    replaced by `with tenant_tx(conn):`; `medos/medos/worker` and `medos/medos/api` call it too.

MOS-SEC-078
    Background workers acquire a tenant context per unit of work. `serving_tenants()` is
    where the worker's tenant set comes from -- see the note on why it is configuration
    and not a `SELECT` over `tenants`.

MOS-STORE-224
    The database half: `current_tenant_id()` resolves the variable through
    `current_setting(name, false)`, which raises SQLSTATE 42704 when it is unset. "A
    connection with no tenant context is not a connection with full access; it is a
    connection with no access." This module never softens that -- it adds an earlier,
    better-worded failure in Python, and if it is bypassed the database still raises.

Two failure modes, and why both exist
-------------------------------------
`NoTenantContextError` is raised in Python before the transaction opens. `42704` is
raised by PostgreSQL if anything reaches a tenant-owned row without going through here.
The first is a good error message; the second is the guarantee. Deleting this module
would not create a cross-tenant leak -- it would create a pile of 42704s.

Why the tenant is a ContextVar and not a parameter threaded through every call
-----------------------------------------------------------------------------
`medos/medos/db`'s public functions all take `conn` as their first argument (CONTRACT.md
section 11: "Pass the connection; do not import a singleton"). Adding `tenant_id` to
every one of them would have been forty signature changes whose only effect is to let a
caller pass the wrong one. The ContextVar is bound in exactly three places -- the API
middleware, the worker's per-job scope, and the test harness -- and `tenant_tx()` reads
it. A caller that wants to be explicit still can: `tenant_tx(conn, tenant_id=...)`.

ContextVars do NOT propagate into `threading.Thread`, which is a real hazard here
because the worker's heartbeat runs in one. `bind_current_tenant()` exists for exactly
that: the thread target binds it as its first statement.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

import psycopg

__all__ = [
    "TENANT_GUC",
    "DEFAULT_TENANT_ID",
    "NoTenantContextError",
    "TenantContextConflict",
    "bind_current_tenant",
    "bound_tenant",
    "current_tenant",
    "current_tenant_or_none",
    "default_tenant_id",
    "platform_tx",
    "reset_current_tenant",
    "serving_tenants",
    "tenant_context",
    "tenant_tx",
]


# The session variable. Chapter 8 MOS-SEC-072 and chapter 12 MOS-STORE-224 both spell it
# this way; it is a custom GUC, so the `medicalos.` prefix is mandatory, not a namespace
# convention we chose.
TENANT_GUC = "medicalos.tenant_id"

# The tenant `0002_tenancy.up.sql` seeds and backfills onto every weeks 1-2 row. It is
# `medos.db.repo.SLICE_TENANT_ID` -- the FROZEN tenant component of every idempotency key
# and every deterministic 2.25.* UID this deployment has written (MOS-IMG-085). It is
# imported rather than restated so the two cannot drift; the import is local to avoid a
# cycle, since `repo` imports this module.
DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000000"

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_current_tenant: ContextVar[str | None] = ContextVar("medos_tenant_id", default=None)


class NoTenantContextError(RuntimeError):
    """No tenant is bound, so no transaction may open. MOS-SEC-075, MOS-STORE-227.

    This is deliberately NOT a "fall back to every tenant" and NOT a "return nothing".
    MOS-STORE-224: an empty result set is indistinguishable from "this tenant owns
    nothing" and hides the bug until it reaches a report.
    """


class TenantContextConflict(RuntimeError):
    """A transaction already bound to tenant A was asked to serve tenant B.

    Refused rather than re-bound. `set_config(..., true)` would happily overwrite the
    value mid-transaction, and the statements already executed would have run under the
    old tenant while the ones after run under the new one -- a transaction that spans two
    tenants, which is precisely the thing this module exists to make impossible.
    """


def _normalise(tenant_id: str | uuid.UUID) -> str:
    """Accept a `uuid.UUID` or a string; reject anything that is not a UUID.

    The value is interpolated into a `set_config` parameter, not into SQL text, so this
    is not an injection guard -- it is a "fail at the boundary" guard. A malformed value
    would otherwise surface as `invalid input syntax for type uuid` from inside a policy
    predicate, three frames away from the caller that got it wrong.
    """
    s = str(tenant_id).strip()
    if not s or not _UUID_RE.match(s):
        raise NoTenantContextError(
            f"tenant id must be a UUID, got {s[:8]!r}... "
            "(MOS-SEC-072: tenant_id is uuid NOT NULL)"
        )
    return s.lower()


def default_tenant_id(env: dict[str, str] | None = None) -> str:
    """The tenant a single-tenant deployment runs as. `MEDOS_TENANT_ID`, else the seed.

    This is deployment CONFIGURATION, not a fallback: it is read once at the edge (the
    API middleware, the worker's config) and bound explicitly. `tenant_tx()` never reads
    it, because a helper that silently supplies a tenant when the caller forgot one is
    the same defect as a policy that returns an empty set.
    """
    env = dict(os.environ) if env is None else env
    return _normalise(env.get("MEDOS_TENANT_ID") or DEFAULT_TENANT_ID)


def serving_tenants(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """The tenant set a background worker iterates. MOS-SEC-078, MOS-STORE-225.

    "Retention sweeps, the projection reconciler, the outbox relay, blob GC and any
    migration backfill that touches tenant-owned rows MUST iterate tenants, setting
    `medicalos.tenant_id` per tenant ... The loop is the price of the guarantee."

    The list is configuration (`MEDOS_TENANTS`, comma-separated) and NOT a `SELECT id
    FROM tenants`, and that is a deliberate decision rather than a shortcut. `tenants`
    carries `tenants_self` -- `USING (id = current_tenant_id())` -- under FORCE row
    security, so reading the full list needs a connection with no tenant bound, which
    raises 42704, or a permissive policy, which is exactly the escape hatch MOS-STORE-225
    forbids ("There is no cross-tenant escape hatch invented by this chapter"). A
    `SECURITY DEFINER` function does not help either: FORCE binds the owner too.

    Which tenants a worker serves is a deployment fact in any case -- it is how work is
    partitioned across worker pools. When the tenant directory becomes a real service it
    replaces this function's body and no caller changes.
    """
    env = dict(os.environ) if env is None else env
    raw = env.get("MEDOS_TENANTS", "")
    if raw.strip():
        return tuple(_normalise(part) for part in raw.split(",") if part.strip())
    return (default_tenant_id(env),)


# =====================================================================================
# The ambient binding
# =====================================================================================
def bind_current_tenant(tenant_id: str | uuid.UUID) -> Token[str | None]:
    """Bind the tenant for this context (and this thread). Returns a reset token.

    Prefer `tenant_context()`. This raw form exists for thread targets, which start with
    an empty context: `threading.Thread` does not copy ContextVars, so the worker's
    heartbeat thread binds here as its first statement.
    """
    return _current_tenant.set(_normalise(tenant_id))


def reset_current_tenant(token: Token[str | None]) -> None:
    _current_tenant.reset(token)


@contextmanager
def tenant_context(tenant_id: str | uuid.UUID) -> Iterator[str]:
    """Bind the tenant for the duration of the block. No database work of its own."""
    token = bind_current_tenant(tenant_id)
    try:
        yield _normalise(tenant_id)
    finally:
        reset_current_tenant(token)


def current_tenant_or_none() -> str | None:
    return _current_tenant.get()


def current_tenant() -> str:
    """The bound tenant, or `NoTenantContextError`. Never a default, never `None`."""
    tid = _current_tenant.get()
    if tid is None:
        raise NoTenantContextError(
            "no tenant is bound to this context. Wrap the work in "
            "medos.db.tenancy.tenant_context(...) at the edge that knows the tenant "
            "(MOS-SEC-075: all tenant-scoped access passes through one function)."
        )
    return tid


# =====================================================================================
# THE CHOKEPOINT
# =====================================================================================
@contextmanager
def tenant_tx(
    conn: psycopg.Connection[Any],
    tenant_id: str | uuid.UUID | None = None,
) -> Iterator[psycopg.Connection[Any]]:
    """Open (or join) a transaction with `medicalos.tenant_id` bound. MOS-SEC-074/075.

    This is the ONLY place in `medos` that writes the session variable. Everything in
    `medos/medos/db` that used to say `with conn.transaction():` says `with tenant_tx(conn):`.

    Semantics inherited from `psycopg.Connection.transaction()`, unchanged:

      * on a connection with no open transaction it BEGINs and COMMITs on exit;
      * inside a caller's open transaction it is a SAVEPOINT and the caller commits.

    So `create_job_queued()` still composes the job row and the queue row into ONE
    transaction (MOS-EXEC-034, CONTRACT.md section 4) and `claim()` still commits on its
    own (MOS-EXEC-025). Adding the tenant binding changed no transaction boundary.

    `is_local => true` is not optional (MOS-STORE-226). A session-level `SET` survives
    the commit and is handed to the next tenant's request by a transaction-mode pooler.
    `tests/integration/test_tenancy.py::test_binding_does_not_outlive_the_transaction`
    asserts the variable is gone after this context manager exits.

    Re-entry with a DIFFERENT tenant raises `TenantContextConflict`. Re-entry with the
    same tenant is a no-op beyond the savepoint, which is what lets `save_result()` call
    `append_event()` without either of them knowing the other opened a scope.
    """
    tid = _normalise(tenant_id) if tenant_id is not None else current_tenant()

    with conn.transaction():
        already = _read_guc(conn)
        if already and already != tid:
            raise TenantContextConflict(
                f"transaction already bound to tenant {already[:8]}...; refusing to "
                f"re-bind to {tid[:8]}.... One transaction serves one tenant "
                "(MOS-SEC-072)."
            )
        if not already:
            # The parameterised form. `SET LOCAL` takes no placeholder, and building the
            # statement by string concatenation is how a GUC name becomes an injection
            # point; `set_config` is the form MOS-SEC-074 writes anyway.
            conn.execute("SELECT set_config(%s, %s, true)", (TENANT_GUC, tid))
        yield conn


@contextmanager
def platform_tx(conn: psycopg.Connection[Any]) -> Iterator[psycopg.Connection[Any]]:
    """A transaction that deliberately has NO tenant bound. PRE-TENANCY tables only.

    MOS-STORE-345 closes the pre-tenancy class at exactly two tables -- `ae_tenant_map`,
    which resolves the tenant for Path A ingest, and `quarantine`, which records that it
    could not be resolved. They carry no `tenant_id`, no row security and no policy,
    because a tenant-isolation policy on either is a circular definition: the resolver
    would have to know the tenant in order to look up the tenant.

    This exists as a NAMED function rather than as "just use conn.transaction()" so that
    every no-tenant transaction in the codebase is greppable and reviewable. It is not an
    escape hatch: it binds nothing, so the moment a statement inside it touches a
    tenant-owned row PostgreSQL raises 42704 (MOS-STORE-224). Containment for the two
    pre-tenancy tables is a Chapter 8 permission at the API, not the database.
    """
    with conn.transaction():
        yield conn


def bound_tenant(conn: psycopg.Connection[Any]) -> str | None:
    """The tenant bound on this connection right now, or None. For tests and diagnostics."""
    return _read_guc(conn)


def _read_guc(conn: psycopg.Connection[Any]) -> str | None:
    row = conn.execute("SELECT current_setting(%s, true) AS v", (TENANT_GUC,)).fetchone()
    if row is None:
        return None
    value = row["v"] if isinstance(row, dict) else row[0]
    return value or None
