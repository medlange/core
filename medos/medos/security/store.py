# SPDX-License-Identifier: Apache-2.0
"""`api_keys` row access. Every statement here goes through `tenant_tx()`.

CONTRACT.md section 1 puts row access in `medos/medos/db`; this module is the exception and it
is a deliberate one. `medos/medos/db/repo.py` is the job pipeline's row access and importing
the credential directory into it would put `secret_hash` one autocomplete away from every
function that builds a provenance record. The credential directory is its own bounded
context, so it lives next to the authenticator that reads it.

What it does NOT do differently: it uses `medos.db.tenancy.tenant_tx()` for every
statement, exactly as `medos/medos/db/repo.py` does. MOS-SEC-075 says "all tenant-scoped
database access MUST pass through exactly one repository function", and `tenant_tx()` is
that function. This module sets no session variable of its own.

Requirements implemented
------------------------
MOS-SEC-010  `issue()` hashes the secret and returns the plaintext EXACTLY ONCE, on the
             `MintedKey` it returns. Nothing here writes the plaintext anywhere and there
             is no function that returns a stored secret, because there is none stored.
MOS-SEC-011  the column set; lookup by `key_id`.
MOS-SEC-012  `<= 365 days` and "no expiry is a refusal", checked in Python at the call
             site for a good error message and in the schema for the guarantee.
MOS-SEC-015  `revoke()` writes `revoked_at = now()`; the <= 5 s bound on effect is the
             authenticator's cache TTL, not this module's.
MOS-SEC-031/033  scope grammar, via `medos.security.scopes.validate_scope`.
MOS-SEC-136  no function in this module returns, logs or formats a secret.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from medos.db.tenancy import tenant_tx
from medos.security.authn import PRINCIPAL_KINDS, PrincipalKind
from medos.security.hashing import Argon2idHasher, SecretHasher
from medos.security.keyformat import MintedKey, mint
from medos.security.scopes import validate_scope

__all__ = [
    "MAX_LIFETIME_DAYS",
    "BOOTSTRAP_OPERATOR_ID",
    "ApiKeyRecord",
    "KeyLifetimeError",
    "issue",
    "lookup",
    "list_keys",
    "revoke",
    "rotate",
    "touch_last_used",
]

# MOS-SEC-012.
MAX_LIFETIME_DAYS = 365

# `api_keys.created_by` is NOT NULL (chapter 12) but `users` does not exist yet, so the
# first key of a deployment has no user to name. This is the documented placeholder the
# CLI writes when `--bootstrap` is passed, and it is the SAME nil-uuid convention 0002
# used for the backfill tenant. It is a different namespace: a user id, not a tenant id.
BOOTSTRAP_OPERATOR_ID = "00000000-0000-0000-0000-000000000000"


class KeyLifetimeError(ValueError):
    """MOS-SEC-012: no expiry, an expiry in the past, or one beyond 365 days."""


@dataclass(frozen=True)
class ApiKeyRecord:
    """One `api_keys` row WITHOUT its `secret_hash`, for listing and for audit.

    The hash is excluded from this type rather than merely from `repr`. A record type
    that carries the hash is a record type somebody eventually serialises into a
    management API response; `_Row` below is the internal form the authenticator uses and
    it never leaves this module.
    """

    id: str
    tenant_id: str
    key_id: str
    principal_kind: PrincipalKind
    principal_id: str
    scope: tuple[str, ...]
    expires_at: datetime
    created_at: datetime
    created_by: str
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
    source_ip_allowlist: tuple[str, ...] = ()
    label: str | None = None
    rotated_from: str | None = None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or datetime.now(UTC))

    def allows_source(self, source_ip: str | None) -> bool:
        """MOS-SEC-011's `source_ip_allowlist`, evaluated in Python.

        FAILS CLOSED on an unknown peer address: an empty allowlist means "unrestricted"
        (chapter 12's default), but a NON-empty allowlist with no address to check means
        the platform cannot tell where the call came from, and that is not the same as
        "it came from an allowed place".
        """
        if not self.source_ip_allowlist:
            return True
        if not source_ip:
            return False
        try:
            addr = ipaddress.ip_address(source_ip)
        except ValueError:
            return False
        for cidr in self.source_ip_allowlist:
            try:
                if addr in ipaddress.ip_network(cidr, strict=False):
                    return True
            except ValueError:  # pragma: no cover - the column is typed cidr[]
                continue
        return False


@dataclass(frozen=True)
class _Row(ApiKeyRecord):
    """`ApiKeyRecord` plus the hash. Internal to this module and to the authenticator."""

    secret_hash: bytes = b""


_COLUMNS = (
    "id, tenant_id, key_id, principal_kind, user_id, service_account_id, scope, "
    "expires_at, created_at, created_by, last_used_at, revoked_at, revoked_reason, "
    "source_ip_allowlist, label, rotated_from"
)


def _record(row: dict[str, Any], *, with_hash: bool = False) -> ApiKeyRecord:
    kind = row["principal_kind"]
    principal_id = row["user_id"] if kind == "user" else row["service_account_id"]
    common = {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "key_id": row["key_id"],
        "principal_kind": kind,
        "principal_id": str(principal_id),
        "scope": tuple(row["scope"] or ()),
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "created_by": str(row["created_by"]),
        "last_used_at": row["last_used_at"],
        "revoked_at": row["revoked_at"],
        "revoked_reason": row["revoked_reason"],
        "source_ip_allowlist": tuple(str(c) for c in (row["source_ip_allowlist"] or ())),
        "label": row["label"],
        "rotated_from": str(row["rotated_from"]) if row["rotated_from"] else None,
    }
    if with_hash:
        return _Row(**common, secret_hash=bytes(row["secret_hash"]))
    return ApiKeyRecord(**common)


def _check_lifetime(expires_at: datetime, now: datetime) -> None:
    """MOS-SEC-012, in Python, so the CLI can say WHY rather than quoting a constraint."""
    if expires_at.tzinfo is None:
        raise KeyLifetimeError("expires_at must be timezone-aware (UTC)")
    if expires_at <= now:
        raise KeyLifetimeError(
            f"expires_at {expires_at.isoformat()} is not in the future. A key with no "
            "usable lifetime MUST be rejected at creation (MOS-SEC-012)."
        )
    if expires_at - now > timedelta(days=MAX_LIFETIME_DAYS):
        raise KeyLifetimeError(
            f"expires_at {expires_at.isoformat()} is more than {MAX_LIFETIME_DAYS} days "
            "away. MOS-SEC-012 caps an API key's lifetime at 365 days; rotate instead "
            "of extending."
        )


def issue(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    principal_kind: PrincipalKind,
    principal_id: str,
    created_by: str,
    expires_at: datetime,
    scope: Iterable[str] | None = None,
    source_ip_allowlist: Sequence[str] = (),
    label: str | None = None,
    env: str = "dev",
    hasher: SecretHasher | None = None,
    rotated_from: str | None = None,
    now: datetime | None = None,
) -> tuple[MintedKey, ApiKeyRecord]:
    """Mint one key, store its hash, return the plaintext ONCE. MOS-SEC-010.

    The returned `MintedKey` is the only object in the platform that has ever held the
    plaintext and it holds it for the length of the caller's statement. Nothing is
    returned from which the secret could be derived later: the row holds argon2id only.

    `tenant_id` is passed explicitly to `tenant_tx()` rather than taken from the ambient
    context, because issuance is an OPERATOR action against a tenant that is named on the
    command line -- there is no request whose credential could have bound one.
    """
    now = now or datetime.now(UTC)
    if principal_kind not in PRINCIPAL_KINDS:
        raise ValueError(
            f"principal_kind must be one of {PRINCIPAL_KINDS} (chapter 8 table 8.2.1); "
            f"platform_admin needs a break-glass grant that does not exist in this block"
        )
    _check_lifetime(expires_at, now)
    clean_scope = validate_scope(scope)
    minted = mint(env)
    secret_hash = (hasher or Argon2idHasher()).hash(minted.secret)

    user_id = principal_id if principal_kind == "user" else None
    service_account_id = principal_id if principal_kind == "service_account" else None

    with tenant_tx(conn, tenant_id=tenant_id) as tx:
        row = tx.execute(
            f"""
            INSERT INTO api_keys (
              tenant_id, principal_kind, user_id, service_account_id,
              key_id, secret_hash, scope, expires_at, created_by,
              source_ip_allowlist, label, rotated_from, created_at
            ) VALUES (
              %(tenant_id)s, %(principal_kind)s, %(user_id)s, %(service_account_id)s,
              %(key_id)s, %(secret_hash)s, %(scope)s, %(expires_at)s, %(created_by)s,
              %(allowlist)s, %(label)s, %(rotated_from)s, %(created_at)s
            )
            RETURNING {_COLUMNS}
            """,
            {
                "tenant_id": tenant_id,
                "principal_kind": principal_kind,
                "user_id": user_id,
                "service_account_id": service_account_id,
                "key_id": minted.key_id,
                "secret_hash": secret_hash,
                "scope": list(clean_scope),
                "expires_at": expires_at,
                "created_by": created_by,
                "allowlist": list(source_ip_allowlist),
                "label": label,
                "rotated_from": rotated_from,
                "created_at": now,
            },
        ).fetchone()
    assert row is not None
    return minted, _record(row)


def lookup(
    conn: psycopg.Connection[Any], tenant_id: str, key_id: str
) -> ApiKeyRecord | None:
    """The authenticator's probe: one key, inside one tenant. MOS-SEC-011.

    Returns the internal `_Row` (carrying `secret_hash`) so the authenticator can verify;
    the type is `ApiKeyRecord` because no caller outside `medos.security` should be
    holding the hash, and the ones inside it know to downcast.

    `WHERE key_id = %s` is an INDEX HINT, not the isolation mechanism -- MOS-SEC-076.
    The tenant predicate is supplied by `api_keys_tenant_isolation`, which is why this
    returns None rather than another tenant's row when the key belongs elsewhere.
    """
    with tenant_tx(conn, tenant_id=tenant_id) as tx:
        row = tx.execute(
            f"SELECT {_COLUMNS}, secret_hash FROM api_keys WHERE key_id = %s",
            (key_id,),
        ).fetchone()
    return _record(row, with_hash=True) if row else None


def list_keys(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    *,
    include_dead: bool = False,
) -> list[ApiKeyRecord]:
    """Metadata for one tenant's keys. NEVER the secret -- `api_key.read` (class `read`).

    `secret_hash` is not even selected. A list endpoint that fetches the hash and then
    remembers not to serialise it is one refactor away from serialising it.
    """
    clause = "" if include_dead else (
        " WHERE revoked_at IS NULL AND expires_at > now()"
    )
    with tenant_tx(conn, tenant_id=tenant_id) as tx:
        rows = tx.execute(
            f"SELECT {_COLUMNS} FROM api_keys{clause} ORDER BY created_at DESC"
        ).fetchall()
    return [_record(r) for r in rows]


def revoke(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    key_id: str,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> ApiKeyRecord | None:
    """MOS-SEC-015. Idempotent: revoking an already-revoked key keeps the first timestamp.

    Not a DELETE. The row is the record that the credential existed, and "which key did
    this" must stay answerable after the key is dead (chapter 12 MOS-STORE-343).
    """
    now = now or datetime.now(UTC)
    with tenant_tx(conn, tenant_id=tenant_id) as tx:
        row = tx.execute(
            f"""
            UPDATE api_keys
               SET revoked_at = COALESCE(revoked_at, %(now)s),
                   revoked_reason = COALESCE(revoked_reason, %(reason)s)
             WHERE key_id = %(key_id)s
            RETURNING {_COLUMNS}
            """,
            {"now": now, "reason": reason, "key_id": key_id},
        ).fetchone()
    return _record(row) if row else None


def rotate(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    key_id: str,
    *,
    expires_at: datetime | None = None,
    grace: timedelta = timedelta(0),
    reason: str = "rotated",
    env: str | None = None,
    hasher: SecretHasher | None = None,
    now: datetime | None = None,
) -> tuple[MintedKey, ApiKeyRecord, ApiKeyRecord]:
    """Issue a successor to `key_id` and retire the original. ONE transaction.

    Rotation is a NEW ROW, never an UPDATE of `secret_hash` -- the `api_keys_immutable`
    trigger makes that structural. The successor inherits principal, scope, allowlist and
    label; it does NOT inherit `expires_at`, because a rotation that keeps the old expiry
    produces a key that dies the day it is born, and a rotation that keeps the old
    *duration* silently re-extends a credential past MOS-SEC-012's ceiling measured from
    the original creation. The caller states the new expiry or gets the old key's
    remaining lifetime, capped at 365 days.

    `grace` is the overlap window: the old key stops working `grace` after now, so a
    fleet can be re-pointed without a coordinated restart -- the same shape MOS-SEC-137
    gives the Job Token signing key. `grace=0` (the default) is immediate revocation,
    which is what a COMPROMISE rotation wants. The grace may never push the old key past
    its own `expires_at`: `api_keys_expiry_only_shortens` enforces that in the database,
    and this function does not try.

    Both writes are inside ONE `tenant_tx()`, so a successor without a retired
    predecessor -- two live credentials where the operator asked for one -- cannot exist.
    """
    now = now or datetime.now(UTC)
    with tenant_tx(conn, tenant_id=tenant_id) as tx:
        old_row = tx.execute(
            f"SELECT {_COLUMNS} FROM api_keys WHERE key_id = %s FOR UPDATE",
            (key_id,),
        ).fetchone()
        if old_row is None:
            raise LookupError(f"no api key {key_id} in this tenant")
        old = _record(old_row)

        if expires_at is None:
            remaining = max(old.expires_at - now, timedelta(days=1))
            expires_at = now + min(remaining, timedelta(days=MAX_LIFETIME_DAYS))

        minted, successor = issue(
            tx,
            tenant_id=tenant_id,
            principal_kind=old.principal_kind,
            principal_id=old.principal_id,
            created_by=old.created_by,
            expires_at=expires_at,
            scope=old.scope,
            source_ip_allowlist=old.source_ip_allowlist,
            label=old.label,
            env=env or "dev",
            hasher=hasher,
            rotated_from=old.id,
            now=now,
        )

        cutoff = min(now + grace, old.expires_at)
        retired_row = tx.execute(
            f"""
            UPDATE api_keys
               SET expires_at = LEAST(expires_at, %(cutoff)s),
                   revoked_at = CASE WHEN %(cutoff)s <= %(now)s
                                     THEN COALESCE(revoked_at, %(now)s)
                                     ELSE revoked_at END,
                   revoked_reason = CASE WHEN %(cutoff)s <= %(now)s
                                         THEN COALESCE(revoked_reason, %(reason)s)
                                         ELSE revoked_reason END
             WHERE key_id = %(key_id)s
            RETURNING {_COLUMNS}
            """,
            {"cutoff": cutoff, "now": now, "reason": reason, "key_id": key_id},
        ).fetchone()
    assert retired_row is not None
    return minted, successor, _record(retired_row)


def touch_last_used(
    conn: psycopg.Connection[Any],
    tenant_id: str,
    key_id: str,
    *,
    when: datetime | None = None,
) -> None:
    """MOS-SEC-011's `last_used_at`. Best effort, and deliberately NOT per request.

    A write on every authenticated request turns a read-only GET into a read-write
    transaction and puts one row of every tenant's `api_keys` in permanent lock
    contention. The authenticator stamps this once per credential-cache miss -- i.e. at
    most once per 5 s per key (MOS-SEC-015) -- which is the resolution the column is
    actually used at ("has this integration gone quiet?").

    `LEAST`-free and monotonic by construction: two concurrent stamps race to the same
    second and either winner is correct.
    """
    when = when or datetime.now(UTC)
    with tenant_tx(conn, tenant_id=tenant_id) as tx:
        tx.execute(
            "UPDATE api_keys SET last_used_at = GREATEST(COALESCE(last_used_at, %(w)s), "
            "%(w)s) WHERE key_id = %(k)s",
            {"w": when, "k": key_id},
        )
