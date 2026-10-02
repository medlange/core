# SPDX-License-Identifier: Apache-2.0
"""The `Authenticator` port, the `Principal` it returns, and the refusals it raises.

docs/spec/15-delivery.md section 15.2.4 item 2: "API-key authentication behind an
`Authenticator` interface, so OIDC is a driver rather than a rewrite."

    MOS-SEC-009 -- "Authentication MUST be implemented behind one `Authenticator` port
    with one method, `Authenticate(ctx, credential) (Principal, error)`. Adding OIDC in
    0.3 MUST NOT require a change to any PEP."

One method. `authenticate(credential) -> Principal`, raising `AuthenticationError`. The
Go signature threads `ctx`; in Python the cancellation/deadline half of `ctx` is the
event loop's and the request-scoped half is a ContextVar, so a `ctx` parameter here would
be a parameter every caller passes `None` to.

    MOS-SEC-008 -- "Every request reaching any PEP MUST resolve to exactly one principal
    of exactly one kind. Anonymous access MUST NOT exist on any surface except
    `/healthz`, `/readyz` and the OpenAPI document."

That is why there is no `AnonymousPrincipal`, no `Principal.is_anonymous` and no
`authenticate()` return value meaning "nobody". The port either produces a principal or
raises.

THE LOAD-BEARING PROPERTY OF THIS FILE
--------------------------------------
`Principal.tenant_id` is the ONLY way a tenant enters a request. It is read off the
stored credential row and nothing else -- not a header, not a query parameter, not a body
field, not a path segment. Chapter 10 `MOS-API-003` states it as a wire rule ("The
credential determines the `Tenant`; there is no tenant path segment and no tenant query
parameter") and this dataclass is where it stops being a rule and becomes a type: the
only object that carries a tenant into `tenant_tx()` is one that came out of
`authenticate()`.

`Principal` is frozen for the same reason. A mutable principal is one `principal.tenant_id
= x` away from being the header the rule forbids.

Adding OIDC (0.3, MOS-SEC-016..019)
-----------------------------------
A second driver implementing this Protocol, constructed with a JWKS cache and a
per-tenant issuer map, returning the same `Principal`. `medos/medos/api/auth.py` gains a list of
authenticators keyed by credential scheme; no route, no handler and no repository
function changes. That is the whole point of the port, and it is testable today:
`tests/integration/test_auth.py` drives the middleware against a stub `Authenticator`
that is not the API-key driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, Protocol, runtime_checkable

__all__ = [
    "PrincipalKind",
    "PRINCIPAL_KINDS",
    "Credential",
    "Principal",
    "Authenticator",
    "AuthenticationError",
    "MissingCredential",
    "MalformedCredential",
    "UnknownCredential",
    "ExpiredCredential",
    "RevokedCredential",
    "SourceNotAllowed",
    "CrossTenantDenied",
    "AuthenticatorUnavailable",
]

# Chapter 8 table 8.2.1. `workload` and `service_version` authenticate with mTLS and a
# Job Token respectively and are NOT issuable as API keys; `platform_admin` needs a
# break-glass grant that does not exist in this block (0002_tenancy.up.sql section 11).
# The two kinds below are exactly the two chapter 12's `api_keys.principal_kind` CHECK
# admits, and the spellings are that CHECK's.
PrincipalKind = Literal["user", "service_account"]
PRINCIPAL_KINDS: Final[tuple[str, ...]] = ("user", "service_account")


@dataclass(frozen=True)
class Credential:
    """What a caller presented, normalised off the transport. Not yet trusted.

    `secret` is excluded from `repr` and from equality for the reasons in
    `medos.security.keyformat.PresentedKey`: `repr` is how a secret reaches a traceback
    and a pytest diff, and `==` on a secret is a timing oracle (MOS-SEC-136).

    `scheme` exists so that a second driver can be selected by it rather than by sniffing
    the token's shape. `source_ip` is the peer address, needed for MOS-SEC-011's
    `source_ip_allowlist`; it is `None` when the transport cannot supply one, and a
    non-empty allowlist then refuses -- fail closed, because "we could not tell where
    this came from" is not "it came from an allowed place".
    """

    scheme: str
    secret: str = field(repr=False, compare=False)
    source_ip: str | None = None
    presented_at: datetime | None = None

    def __str__(self) -> str:  # pragma: no cover
        return f"<Credential scheme={self.scheme} source_ip={self.source_ip}>"


@dataclass(frozen=True)
class Principal:
    """Exactly one principal of exactly one kind. MOS-SEC-008.

    Fields
    ------
    kind
        `user` or `service_account`.
    principal_id
        The id of the `User` or `ServiceAccount`. Chapter 12 splits this into
        `api_keys.user_id` / `api_keys.service_account_id` under `principal_kind`; the
        split is a storage concern and this is the joined form the rest of the platform
        uses.
    tenant_id
        THE tenant. Read from the credential row. See the module docstring.
    scope
        MOS-SEC-011's `scope text[]`. A CEILING, never a grant (MOS-SEC-027 says it of a
        Job Token's scope and MOS-SEC-045 generalises it to "a Job Token, an API key, an
        MCP session"): the effective permission set is the INTERSECTION of this, the
        delegating principal's current role grants, and the executing manifest's declared
        permissions. This platform has no roles table yet, so nothing here may be treated
        as a grant -- see `medos/medos/security/scopes.py`.
    credential_id / credential_key_id
        Which credential was used. `credential_key_id` is the public half and is SAFE TO
        LOG -- MOS-SEC-136: "A secret's `key_id` MAY be logged." It is what makes
        "revoke the key that did this" a possible sentence.
    expires_at
        When the credential dies. Carried so a caller can surface it; NOT a cache hint.
    """

    kind: PrincipalKind
    principal_id: str
    tenant_id: str
    scope: tuple[str, ...] = ()
    credential_id: str | None = None
    credential_key_id: str | None = None
    expires_at: datetime | None = None
    authenticated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.kind not in PRINCIPAL_KINDS:
            raise ValueError(
                f"principal kind {self.kind!r} is not one of {PRINCIPAL_KINDS} "
                "(chapter 8 table 8.2.1)"
            )

    def log_fields(self) -> dict[str, str]:
        """The subset of this principal that may appear in a log line or an audit row.

        No secret, and no display name or email even when a later block adds them --
        those are P2 data under section 8.6.1. Ids and the public key id only
        (MOS-SEC-105, MOS-SEC-136, CONTRACT.md section 11).
        """
        out = {
            "principal_kind": self.kind,
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
        }
        if self.credential_key_id:
            out["api_key_id"] = self.credential_key_id
        return out


@runtime_checkable
class Authenticator(Protocol):
    """MOS-SEC-009. One port, one method.

    Implementations MUST raise an `AuthenticationError` subclass rather than returning
    `None`, and MUST NOT return a principal for a credential they could not verify.
    """

    def authenticate(self, credential: Credential) -> Principal:
        """Resolve a credential to exactly one principal, or raise."""


# =====================================================================================
# Refusals
#
# Every one of these maps to an RFC 9457 problem document whose `class` is `authz_error`
# (chapter 10 table 10.4-A) -- `MOS-API-041`: "Authentication failure MUST be `401` and
# authorisation failure `403`".
#
# The `code` values are NOT all distinct, and that is the point. `UnknownCredential` and
# a wrong secret share `INVALID_CREDENTIAL`, because distinguishing them tells an
# attacker which half of a guessed key was right. `ExpiredCredential` and
# `RevokedCredential` are distinct and that is safe: both are raised only AFTER the
# secret has verified, so only the legitimate holder ever sees them -- and to that holder
# "your key expired" is the single most actionable thing the platform can say.
# =====================================================================================
class AuthenticationError(Exception):
    """Base class. Carries the HTTP status and the problem `code`, nothing else.

    Deliberately NOT a `MedosError` (medos/medos/core/errors.py): that hierarchy is the
    clinical/transport/system taxonomy of the job pipeline and an authentication refusal
    is none of those. `medos/medos/api/auth.py` renders these directly.
    """

    status: int = 401
    code: str = "INVALID_CREDENTIAL"
    title: str = "Authentication failed"
    #: The `detail` member. A CONSTANT per class, never interpolated from the request:
    #: MOS-SEC-136 forbids a secret in a problem+json `detail`, and the cheapest way to
    #: guarantee that is to never put request data in one.
    detail: str = "The presented credential is not valid."

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.detail)
        if detail is not None:
            self.detail = detail


class MissingCredential(AuthenticationError):
    """No `Authorization` header at all. MOS-SEC-008, MOS-API-003."""

    status = 401
    code = "AUTHENTICATION_REQUIRED"
    title = "Authentication required"
    detail = (
        "This endpoint requires Authorization: Bearer <credential>. Anonymous access "
        "exists only on /healthz, /readyz and the OpenAPI document."
    )


class MalformedCredential(AuthenticationError):
    """An `Authorization` header that is not a credential this platform issues."""

    status = 401
    code = "INVALID_CREDENTIAL"
    title = "Invalid credential"
    detail = "The presented credential is not valid."


class UnknownCredential(AuthenticationError):
    """No such `key_id`, or the secret did not verify. ONE class for both, on purpose."""

    status = 401
    code = "INVALID_CREDENTIAL"
    title = "Invalid credential"
    detail = "The presented credential is not valid."


class ExpiredCredential(AuthenticationError):
    """MOS-SEC-012. Raised only after the secret verified."""

    status = 401
    code = "CREDENTIAL_EXPIRED"
    title = "Credential expired"
    detail = "The presented API key has expired. Rotate it and retry with the new key."


class RevokedCredential(AuthenticationError):
    """MOS-SEC-015. Raised only after the secret verified."""

    status = 401
    code = "CREDENTIAL_REVOKED"
    title = "Credential revoked"
    detail = "The presented API key has been revoked."


class SourceNotAllowed(AuthenticationError):
    """MOS-SEC-011's `source_ip_allowlist`. A `403`, not a `401`.

    The credential IS valid -- re-authenticating will not help, which is exactly the
    distinction MOS-API-041 draws between 401 and 403.
    """

    status = 403
    code = "SOURCE_IP_NOT_ALLOWED"
    title = "Source address not permitted"
    detail = "This API key may not be used from this source address."


class CrossTenantDenied(AuthenticationError):
    """A request tried to select a tenant. MOS-API-003, MOS-API-041.

    MOS-API-041: "a cross-tenant reference MUST be `403` with code `CROSS_TENANT_DENIED`
    and MUST NOT leak whether the referenced id exists" -- hence a constant `detail` that
    never echoes the requested tenant.
    """

    status = 403
    code = "CROSS_TENANT_DENIED"
    title = "Cross-tenant access denied"
    detail = (
        "The tenant is determined by the credential. A request may not select a tenant "
        "through a header, a query parameter, a path segment or a body field."
    )


class AuthenticatorUnavailable(AuthenticationError):
    """The authenticator could not reach its store.

    A `503`, and NOT a `401`: answering "your key is invalid" when the credential
    directory is unreachable would have every integration in the hospital rotate a
    working key during an outage. `class` is `transport_failure`, not `authz_error`, and
    `medos/medos/api/auth.py` is the one place that mapping lives.
    """

    status = 503
    code = "AUTHENTICATOR_UNAVAILABLE"
    title = "Authentication unavailable"
    detail = "The platform cannot verify credentials right now. Retry."
