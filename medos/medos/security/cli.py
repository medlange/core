# SPDX-License-Identifier: Apache-2.0
"""`python -m medos.security.cli` -- issue, rotate, revoke and list API keys.

docs/spec/15-delivery.md section 15.2.4 item 2 delivers the `Authenticator`; a credential
system with no way to mint a credential is a demo. There is no HTTP surface for this yet
on purpose: `api_key.create` is a class-`admin` permission (chapter 8 section 8.3.2), the
RBAC tables that would decide who holds it do not exist in this block, and an unprotected
`POST /api/v1/api-keys` would be a credential factory reachable by anyone who can reach
the port. A CLI on the deployment host is gated by "can you get a shell on the host",
which is a real control and an honest one.

    MOS-SEC-010 -- "The plaintext MUST be returned exactly once, at creation, and MUST
    NOT be recoverable."

`issue` and `rotate` print the key to STDOUT and everything else to STDERR, so
`... issue ... 2>/dev/null | some-secret-store` is the supported way to hand it over
without it passing through a shell history or a log. The key is printed once; there is no
`show` subcommand and no way to get it back, because there is no column it could come
from.

    MOS-SEC-136 -- "Secret values MUST never be logged, echoed in an API response,
    included in a problem+json `detail`, or written to a trace. A secret's `key_id` MAY
    be logged."

Every human-readable line this module writes names `key_id` and never the secret.

Usage
-----
    python -m medos.security.cli issue \\
        --tenant 00000000-0000-0000-0000-000000000000 \\
        --principal-kind service_account --principal-id <uuid> \\
        --scope job.create --scope job.read \\
        --expires-in-days 90 --label "ohif viewer" --bootstrap

    python -m medos.security.cli rotate --tenant <uuid> --key-id 7Q2XK4M9AB0C \\
        --grace-hours 24
    python -m medos.security.cli revoke --tenant <uuid> --key-id 7Q2XK4M9AB0C \\
        --reason "laptop lost"
    python -m medos.security.cli list   --tenant <uuid> [--all]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from medos.db import conn as dbconn
from medos.security import store
from medos.security.keyformat import api_key_env
from medos.security.scopes import ScopeError, validate_scope
from medos.security.store import MAX_LIFETIME_DAYS

__all__ = ["build_parser", "main"]


def _uuid(value: str) -> str:
    """Argparse type: a UUID, normalised. Refuses early so the error names the flag."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a UUID") from None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m medos.security.cli",
        description="MedicalOS API-key issuance, rotation and revocation "
                    "(chapter 8 section 8.2.2, MOS-SEC-010..015).",
    )
    p.add_argument("--dsn", default=None,
                   help="Database DSN. Default: MEDOS_DATABASE_URL.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON on stdout. The secret appears in "
                        "the `plaintext` member of `issue` and `rotate` output and "
                        "nowhere else.")
    sub = p.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="Mint a key. Prints the plaintext ONCE.")
    issue.add_argument("--tenant", required=True, type=_uuid)
    issue.add_argument("--principal-kind", required=True,
                       choices=("user", "service_account"))
    issue.add_argument("--principal-id", required=True, type=_uuid)
    issue.add_argument("--scope", action="append", default=[],
                       help="Repeatable. A permission identifier (MOS-SEC-031). "
                            "Wildcards are refused (MOS-SEC-033).")
    issue.add_argument("--expires-in-days", type=int, default=90,
                       help=f"1..{MAX_LIFETIME_DAYS}. MOS-SEC-012 caps this at "
                            f"{MAX_LIFETIME_DAYS}; there is no 'never expires'.")
    issue.add_argument("--allow-cidr", action="append", default=[],
                       help="Repeatable. MOS-SEC-011 source_ip_allowlist. "
                            "Empty means unrestricted.")
    issue.add_argument("--label", default=None)
    issue.add_argument("--created-by", type=_uuid, default=None,
                       help="The user minting this key.")
    issue.add_argument("--bootstrap", action="store_true",
                       help="Record the nil uuid as created_by, for the FIRST key of a "
                            "deployment, before `users` exists. Must be typed; it is "
                            "never a default.")
    issue.add_argument("--env", default=None, choices=("prod", "stg", "dev"),
                       help="Key environment. Default: MEDOS_ENV, else dev.")

    rot = sub.add_parser("rotate", help="Issue a successor and retire the original.")
    rot.add_argument("--tenant", required=True, type=_uuid)
    rot.add_argument("--key-id", required=True)
    rot.add_argument("--grace-hours", type=float, default=0.0,
                     help="Overlap window during which the OLD key still works. 0 (the "
                          "default) revokes it immediately, which is what a compromise "
                          "rotation wants.")
    rot.add_argument("--expires-in-days", type=int, default=None,
                     help="Lifetime of the successor. Default: the predecessor's "
                          f"remaining lifetime, capped at {MAX_LIFETIME_DAYS} days.")
    rot.add_argument("--reason", default="rotated")
    rot.add_argument("--env", default=None, choices=("prod", "stg", "dev"))

    rev = sub.add_parser("revoke", help="Kill a key. Effective within 5 s (MOS-SEC-015).")
    rev.add_argument("--tenant", required=True, type=_uuid)
    rev.add_argument("--key-id", required=True)
    rev.add_argument("--reason", default=None)

    lst = sub.add_parser("list", help="Key METADATA for one tenant. Never the secret.")
    lst.add_argument("--tenant", required=True, type=_uuid)
    lst.add_argument("--all", action="store_true",
                     help="Include revoked and expired keys.")
    return p


def _connect(dsn: str | None) -> psycopg.Connection[Any]:
    return dbconn.connect(dsn or dbconn.dsn_from_env(), application_name="medos-keys")


def _record_dict(rec: store.ApiKeyRecord) -> dict[str, Any]:
    """The metadata view. There is no branch here that could add the secret."""
    return {
        "id": rec.id,
        "key_id": rec.key_id,
        "tenant_id": rec.tenant_id,
        "principal_kind": rec.principal_kind,
        "principal_id": rec.principal_id,
        "scope": list(rec.scope),
        "label": rec.label,
        "created_at": rec.created_at.isoformat(),
        "created_by": rec.created_by,
        "expires_at": rec.expires_at.isoformat(),
        "last_used_at": rec.last_used_at.isoformat() if rec.last_used_at else None,
        "revoked_at": rec.revoked_at.isoformat() if rec.revoked_at else None,
        "revoked_reason": rec.revoked_reason,
        "source_ip_allowlist": list(rec.source_ip_allowlist),
        "rotated_from": rec.rotated_from,
    }


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _emit_new_key(
    plaintext: str, rec: store.ApiKeyRecord, *, as_json: bool, note: str
) -> None:
    """The ONE place a plaintext is written. MOS-SEC-010.

    stdout gets the key (and only the key, unless --json); stderr gets the human
    narration. That split is what makes `2>/dev/null | secret-put` safe.
    """
    if as_json:
        print(json.dumps({**_record_dict(rec), "plaintext": plaintext}, indent=2))
    else:
        print(plaintext)
    _err(f"{note} key_id={rec.key_id} tenant={rec.tenant_id} "
         f"expires_at={rec.expires_at.isoformat()} scope={list(rec.scope) or '[]'}")
    _err("This is the only time the key will ever be shown. It is stored as argon2id "
         "and is not recoverable (MOS-SEC-010).")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = datetime.now(UTC)

    try:
        conn = _connect(args.dsn)
    except psycopg.Error as exc:
        _err(f"cannot reach the database: {type(exc).__name__}")
        return 2

    try:
        with conn:
            if args.command == "issue":
                return _issue(conn, args, now)
            if args.command == "rotate":
                return _rotate(conn, args, now)
            if args.command == "revoke":
                return _revoke(conn, args)
            if args.command == "list":
                return _list(conn, args)
    except ScopeError as exc:
        _err(f"refused: {exc}")
        return 3
    except store.KeyLifetimeError as exc:
        _err(f"refused: {exc}")
        return 3
    except LookupError as exc:
        _err(f"not found: {exc}")
        return 4
    except psycopg.errors.CheckViolation as exc:
        # The schema is the second layer under every Python check above; if one of them
        # is ever weakened, this is the message an operator sees instead of a traceback.
        detail = exc.diag.constraint_name or exc.diag.message_primary
        _err(f"refused by the database: {detail}")
        return 3
    except psycopg.Error as exc:
        _err(f"database error: {type(exc).__name__}")
        return 2
    finally:
        conn.close()
    return 0  # pragma: no cover - argparse requires a subcommand


def _issue(conn: psycopg.Connection[Any], args: Any, now: datetime) -> int:
    if args.created_by is None and not args.bootstrap:
        _err("--created-by is required (or --bootstrap for the first key of a "
             "deployment, before `users` exists). MOS-SEC-011 makes created_by NOT "
             "NULL and a credential nobody minted is a credential nobody can revoke "
             "with confidence.")
        return 3
    if args.created_by is not None and args.bootstrap:
        _err("--created-by and --bootstrap are mutually exclusive.")
        return 3
    if not 1 <= args.expires_in_days <= MAX_LIFETIME_DAYS:
        _err(f"--expires-in-days must be 1..{MAX_LIFETIME_DAYS} (MOS-SEC-012).")
        return 3

    scope = validate_scope(args.scope)
    if not scope:
        # Not an error. `scope_permits` treats an empty scope as permitting NOTHING
        # (see medos/medos/security/scopes.py), so this key authenticates and authorises
        # nothing -- which is a legitimate thing to mint while the RBAC layer is being
        # built, and a trap if the operator thought empty meant "everything".
        _err("note: --scope is empty. An empty scope is a ceiling of zero permissions, "
             "not unrestricted access.")

    minted, rec = store.issue(
        conn,
        tenant_id=args.tenant,
        principal_kind=args.principal_kind,
        principal_id=args.principal_id,
        created_by=args.created_by or store.BOOTSTRAP_OPERATOR_ID,
        expires_at=now + timedelta(days=args.expires_in_days),
        scope=scope,
        source_ip_allowlist=args.allow_cidr,
        label=args.label,
        env=args.env or api_key_env(),
        now=now,
    )
    _emit_new_key(minted.plaintext, rec, as_json=args.json, note="issued")
    return 0


def _rotate(conn: psycopg.Connection[Any], args: Any, now: datetime) -> int:
    expires_at = (
        now + timedelta(days=args.expires_in_days)
        if args.expires_in_days is not None else None
    )
    if expires_at is not None and not 1 <= args.expires_in_days <= MAX_LIFETIME_DAYS:
        _err(f"--expires-in-days must be 1..{MAX_LIFETIME_DAYS} (MOS-SEC-012).")
        return 3

    minted, successor, retired = store.rotate(
        conn,
        args.tenant,
        args.key_id,
        expires_at=expires_at,
        grace=timedelta(hours=max(0.0, args.grace_hours)),
        reason=args.reason,
        env=args.env or api_key_env(),
        now=now,
    )
    _emit_new_key(minted.plaintext, successor, as_json=args.json, note="rotated to")
    if retired.revoked_at is not None:
        _err(f"retired key_id={retired.key_id} revoked_at="
             f"{retired.revoked_at.isoformat()}")
    else:
        _err(f"retired key_id={retired.key_id} keeps working until "
             f"{retired.expires_at.isoformat()} (grace window)")
    return 0


def _revoke(conn: psycopg.Connection[Any], args: Any) -> int:
    rec = store.revoke(conn, args.tenant, args.key_id, reason=args.reason)
    if rec is None:
        _err(f"no key {args.key_id} in tenant {args.tenant}")
        return 4
    if args.json:
        print(json.dumps(_record_dict(rec), indent=2))
    when = rec.revoked_at.isoformat() if rec.revoked_at else "?"
    _err(f"revoked key_id={rec.key_id} at {when} -- effective within 5 s across "
         "replicas (MOS-SEC-015)")
    return 0


def _list(conn: psycopg.Connection[Any], args: Any) -> int:
    records = store.list_keys(conn, args.tenant, include_dead=args.all)
    if args.json:
        print(json.dumps([_record_dict(r) for r in records], indent=2))
        return 0
    if not records:
        _err("no keys")
        return 0
    print(f"{'KEY_ID':<14} {'KIND':<16} {'EXPIRES':<26} {'STATE':<8} LABEL")
    for r in records:
        state = "revoked" if r.is_revoked else ("expired" if r.is_expired() else "live")
        print(f"{r.key_id:<14} {r.principal_kind:<16} "
              f"{r.expires_at.isoformat():<26} {state:<8} {r.label or ''}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
