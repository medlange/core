# SPDX-License-Identifier: Apache-2.0
"""`ApiKeyAuthenticator` -- the 0.1 driver behind the `Authenticator` port.

docs/spec/15-delivery.md section 15.2.4 item 2. Chapter 8 section 8.2.2 is the credential
format; `medos/medos/security/authn.py` is the port; this is the one driver that exists today.

    MOS-SEC-009 -- one port, one method. Adding OIDC in 0.3 adds a sibling of this class
    and changes no PEP.

HOW THE TENANT IS RESOLVED, AND WHY IT IS A LOOP
------------------------------------------------
`api_keys` is an ordinary tenant-owned table under forced row-level security
(MOS-SEC-072), so a `SELECT ... WHERE key_id = $1` with no tenant bound raises 42704
(MOS-STORE-224). That looks circular -- authentication must find the row BEFORE it knows
the tenant -- and it is the same circularity MOS-STORE-345 solves for `ae_tenant_map` by
making it pre-tenancy. `api_keys` cannot take that route: MOS-STORE-345 closes the
pre-tenancy class at two tables, and MOS-SEC-011 requires `api_keys.tenant_id` by name.

So the resolution is the loop MOS-SEC-078 and MOS-STORE-225 already price in -- "The loop
is the price of the guarantee". `key_id` is UNIQUE across the whole table, so at most one
tenant can own a given key. This driver iterates the tenant set the deployment serves
(`serving_tenants()`, the same configuration a worker iterates) and runs the same
tenant-scoped probe under each, through the same chokepoint, `tenant_tx()`. First hit
wins; a miss under every tenant is an unknown key.

The alternative designs were all worse, and naming them is cheaper than re-deriving why:

  * a second database role with a permissive `FOR SELECT ... USING (true)` policy -- a
    real cross-tenant read path, which is exactly what MOS-STORE-225 forbids inventing;
  * a `SECURITY DEFINER` resolver -- FORCE row security binds the table owner too, so it
    does not work without `BYPASSRLS`, and a role with `BYPASSRLS` is MOS-SEC-073's
    explicit prohibition one rename away;
  * a third pre-tenancy lookup table -- amends MOS-STORE-345, whose class is closed.

The cost is O(tenants) index probes per CACHE MISS, and the cache below bounds that to
once per key per 5 s. When the tenant directory becomes a service, `_candidate_tenants`
becomes one call to it and nothing else in this file changes.

THE 5-SECOND CACHE
------------------
    MOS-SEC-015 -- "A key MUST be revocable with effect within 5 seconds across all
    replicas. Implementations MUST NOT cache key validity longer than 5 s."

`CREDENTIAL_CACHE_TTL_S = 5.0` is that ceiling and it is not configurable upwards; the
constructor clamps. Only POSITIVE outcomes are cached: caching a refusal would make a
freshly issued key fail for five seconds, which is an operator's first experience of the
platform. The cache exists because argon2id at m=64MiB costs ~240 ms per verification by
design (MOS-SEC-010), and a control plane that spends a quarter of a second of CPU on
every GET is a denial-of-service surface with a compliance certificate.

PHI AND SECRETS
---------------
Nothing in this module logs, formats, returns or raises a secret. `key_id` is logged
freely -- MOS-SEC-136: "A secret's `key_id` MAY be logged." No patient attribute is
reachable from here at all.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import psycopg

from medos.db.tenancy import serving_tenants
from medos.security import store
from medos.security.authn import (
    AuthenticatorUnavailable,
    Credential,
    ExpiredCredential,
    MalformedCredential,
    MissingCredential,
    Principal,
    RevokedCredential,
    SourceNotAllowed,
    UnknownCredential,
)
from medos.security.hashing import Argon2idHasher, SecretHasher
from medos.security.keyformat import MalformedKeyError, parse

__all__ = [
    "CREDENTIAL_CACHE_TTL_S",
    "BEARER_SCHEME",
    "ApiKeyAuthenticator",
]

log = logging.getLogger("medos.api")

# MOS-SEC-015. A ceiling, not a default to be tuned upwards.
CREDENTIAL_CACHE_TTL_S: Final[float] = 5.0

# MOS-API-003: "Every request MUST carry `Authorization: Bearer <credential>`."
BEARER_SCHEME: Final[str] = "bearer"

_CACHE_MAX_ENTRIES: Final[int] = 4096


class _Entry:
    __slots__ = ("principal", "secret_digest", "expires_monotonic")

    def __init__(self, principal: Principal, secret_digest: bytes, expires: float):
        self.principal = principal
        self.secret_digest = secret_digest
        self.expires_monotonic = expires


class ApiKeyAuthenticator:
    """The API-key driver. Implements `medos.security.authn.Authenticator`.

    Parameters
    ----------
    connect
        Opens a database connection. The same injected-collaborator shape
        `medos.api.app.create_app` uses (CONTRACT.md section 11: "Pass the connection; do
        not import a singleton"). One connection is opened per cache MISS and closed
        again; with the 5 s cache that is a handful per minute, not one per request.
    tenants
        A callable returning the tenant ids this deployment serves. Defaults to
        `medos.db.tenancy.serving_tenants`, which reads `MEDOS_TENANTS`. It is a CALLABLE
        and not a tuple so that a deployment which learns about a new tenant at runtime
        does not need the process restarted.
    env
        `prod` | `stg` | `dev`, or None to accept any. When set, a key minted for another
        environment is refused BEFORE the database is touched. A staging key presented to
        production is an operator error, and finding out from a 401 that says "invalid
        credential" costs an afternoon.
    hasher
        The `SecretHasher` port. Injected so a test can assert the driver calls it, never
        so a deployment can make it cheaper.
    cache_ttl_s
        Clamped to `[0, CREDENTIAL_CACHE_TTL_S]`. Pass 0 to disable (tests that assert
        revocation semantics do).
    """

    def __init__(
        self,
        connect: Callable[..., psycopg.Connection[Any]],
        *,
        tenants: Callable[[], Sequence[str]] | None = None,
        env: str | None = None,
        hasher: SecretHasher | None = None,
        cache_ttl_s: float = CREDENTIAL_CACHE_TTL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._connect = connect
        self._tenants = tenants or serving_tenants
        self._env = env
        self._hasher = hasher or Argon2idHasher()
        self._ttl = max(0.0, min(float(cache_ttl_s), CREDENTIAL_CACHE_TTL_S))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._decoy: bytes | None = None

    # -- the port ---------------------------------------------------------------------
    def authenticate(self, credential: Credential) -> Principal:
        """MOS-SEC-009. Resolve a credential to exactly one principal, or raise.

        Order of checks, and why it is this order:

          1. scheme, 2. format, 3. environment -- all free, all decidable without I/O.
          4. the row, 5. the SECRET. Nothing about the key's state is revealed before the
             secret verifies.
          6. revoked, 7. expired, 8. source address -- each is a distinct, actionable
             answer, and each is only ever seen by the legitimate holder of the key
             because step 5 already passed.

        Steps 6 and 7 in that order: a key that is both revoked and expired is REVOKED.
        "It expired, mint another the same way" and "somebody killed this credential" are
        different instructions to the person reading the error.
        """
        if not credential.secret:
            raise MissingCredential()
        if credential.scheme.lower() != BEARER_SCHEME:
            raise MalformedCredential(
                "Authorization must use the Bearer scheme (MOS-API-003)."
            )

        try:
            presented = parse(credential.secret)
        except MalformedKeyError:
            # Deliberately not chained: `raise ... from exc` would put the parse error --
            # which is generic, but whose traceback frame holds the raw credential in a
            # local -- into whatever renders this. MOS-SEC-136.
            raise MalformedCredential() from None

        if self._env is not None and not presented.matches_env(self._env):
            raise MalformedCredential(
                f"This deployment accepts {self._env} keys; the presented key was "
                "minted for another environment."
            )

        digest = hashlib.sha256(presented.secret.encode("utf-8")).digest()
        cached = self._cache_get(presented.key_id, digest)
        if cached is not None:
            return cached

        record = self._find(presented.key_id)
        if record is None:
            # Constant-ish work on the unknown-key path. Without it, "no such key_id"
            # answers in ~1 ms and "wrong secret" in ~240 ms, which turns the 401 into an
            # oracle for enumerating valid key ids. They are public (MOS-SEC-011), so
            # this is hygiene rather than a control -- but it costs one line.
            self._burn()
            raise UnknownCredential()

        stored = getattr(record, "secret_hash", b"")
        if not self._hasher.verify(stored, presented.secret):
            raise UnknownCredential()

        now = self._clock()
        if record.is_revoked:
            raise RevokedCredential()
        if record.is_expired(now):
            raise ExpiredCredential()
        if not record.allows_source(credential.source_ip):
            raise SourceNotAllowed()

        principal = Principal(
            kind=record.principal_kind,
            principal_id=record.principal_id,
            tenant_id=record.tenant_id,
            scope=record.scope,
            credential_id=record.id,
            credential_key_id=record.key_id,
            expires_at=record.expires_at,
            authenticated_at=now,
        )
        self._cache_put(presented.key_id, digest, principal)
        self._stamp_last_used(record.tenant_id, record.key_id, now)
        return principal

    # -- internals --------------------------------------------------------------------
    def _find(self, key_id: str) -> store.ApiKeyRecord | None:
        """The tenant loop. See the module docstring for why it is a loop."""
        try:
            conn = self._connect()
        except psycopg.Error as exc:
            log.warning(
                "authn_store_unreachable",
                extra={"reason": type(exc).__name__, "api_key_id": key_id},
            )
            raise AuthenticatorUnavailable() from None
        try:
            for tenant_id in self._tenants():
                record = store.lookup(conn, tenant_id, key_id)
                if record is not None:
                    return record
            return None
        except psycopg.Error as exc:
            log.warning(
                "authn_store_error",
                extra={"reason": type(exc).__name__, "api_key_id": key_id},
            )
            raise AuthenticatorUnavailable() from None
        finally:
            conn.close()

    def _stamp_last_used(self, tenant_id: str, key_id: str, when: datetime) -> None:
        """MOS-SEC-011's `last_used_at`, once per cache miss. Never fatal.

        A failure here MUST NOT fail the request: the caller presented a valid credential
        and the platform's inability to record that fact is the platform's problem. It is
        logged, because an authenticator that has silently stopped writing `last_used_at`
        makes "which integrations are still alive" quietly wrong.
        """
        try:
            conn = self._connect()
        except psycopg.Error:
            return
        try:
            store.touch_last_used(conn, tenant_id, key_id, when=when)
        except psycopg.Error as exc:  # pragma: no cover - needs an induced DB fault
            log.warning(
                "authn_last_used_not_recorded",
                extra={"reason": type(exc).__name__, "api_key_id": key_id},
            )
        finally:
            conn.close()

    def _burn(self) -> None:
        """One argon2id verification against a decoy hash. Timing hygiene only."""
        if self._decoy is None:
            self._decoy = self._hasher.hash("decoy")
        self._hasher.verify(self._decoy, "not-the-decoy")

    def _cache_get(self, key_id: str, digest: bytes) -> Principal | None:
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key_id)
            if entry is None:
                return None
            if entry.expires_monotonic <= now:
                del self._cache[key_id]
                return None
            # The cache is keyed on the PUBLIC key id, so a caller presenting the right
            # key_id with a wrong secret must not be handed the cached principal.
            if not hmac.compare_digest(entry.secret_digest, digest):
                return None
            self._cache.move_to_end(key_id)
            return entry.principal

    def _cache_put(self, key_id: str, digest: bytes, principal: Principal) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._cache[key_id] = _Entry(
                principal, digest, time.monotonic() + self._ttl
            )
            self._cache.move_to_end(key_id)
            while len(self._cache) > _CACHE_MAX_ENTRIES:
                self._cache.popitem(last=False)

    def invalidate(self, key_id: str | None = None) -> None:
        """Drop a cached credential (or all of them).

        For the process that just revoked a key, so its own next request does not have to
        wait out the 5 s. It is NOT the mechanism for MOS-SEC-015 -- that requirement is
        about "all replicas", and a method only reachable in one process cannot satisfy
        it. The TTL is the mechanism; this is a local courtesy.
        """
        with self._lock:
            if key_id is None:
                self._cache.clear()
            else:
                self._cache.pop(key_id, None)
