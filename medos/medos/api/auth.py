# SPDX-License-Identifier: Apache-2.0
"""The authentication PEP: one ASGI middleware, one `Authenticator`, one tenant binding.

docs/spec/15-delivery.md section 15.2.4 item 2. Chapter 8 is the credential; chapter 10
is the wire.

    MOS-SEC-008 -- "Every request reaching any PEP MUST resolve to exactly one principal
    of exactly one kind. Anonymous access MUST NOT exist on any surface except
    `/healthz`, `/readyz` and the OpenAPI document."

    MOS-API-003 -- "Every request MUST carry `Authorization: Bearer <credential>`. The
    credential determines the `Tenant`; there is no tenant path segment and no tenant
    query parameter. A `MedicalOS-Tenant-Id` request header MAY be honoured **only** for
    a principal holding the platform-administrator role, MUST be rejected with `403` for
    every other principal ..."

    MOS-API-041 -- "Authentication failure MUST be `401` and authorisation failure
    `403`; a cross-tenant reference MUST be `403` with code `CROSS_TENANT_DENIED` and
    MUST NOT leak whether the referenced id exists."

THE LOAD-BEARING LINE IN THIS FILE
----------------------------------
    bind_current_tenant(principal.tenant_id)

`principal` came out of `Authenticator.authenticate()`, which read it off the stored
credential row. That is the ONLY assignment of a tenant on the request path, and the
value has never been touched by anything the caller controls. `TenantContextMiddleware`
used to bind `MEDOS_TENANT_ID` here; this middleware replaces it, which is exactly the
"next" that `medos/medos/api/app.py` predicted.

Belt and braces, in three layers, because one of them is a code-review property and two
are not:

  1. This middleware refuses a request that NAMES a tenant -- the `MedicalOS-Tenant-Id`
     header, or a `tenant` / `tenant_id` / `tenant_slug` query parameter -- with `403`
     `CROSS_TENANT_DENIED`, after authenticating, per MOS-API-003's ordering.
  2. A tenant in the BODY is not refused here, because nothing reads it: this middleware
     never parses the body, `JobCreateRequest` is `extra="forbid"` (MOS-API-008), and no
     repository function takes a tenant from a handler argument.
  3. If both of those were wrong, `tenant_tx()` raises `TenantContextConflict` the moment
     a second tenant is offered inside a bound transaction, and PostgreSQL's forced RLS
     answers 42704 or an empty set below that. `tests/integration/test_auth.py` proves
     the whole stack, not just layer 1.

WHY RAW ASGI AND NOT `BaseHTTPMiddleware` OR A `Depends`
--------------------------------------------------------
The same two reasons `TraceContextMiddleware` gives. `BaseHTTPMiddleware` pumps the
response through an anyio stream, which turns the SSE endpoint of `routes_events` into a
buffered one. A `Depends` binds inside the handler, which leaves the exception handlers
and the SSE generator outside the tenant binding -- and a PEP that the error path can
route around is not a PEP.

WHY THE VERIFICATION RUNS IN A THREAD
-------------------------------------
`Authenticator.authenticate()` is synchronous and does two blocking things: a database
probe and ~240 ms of argon2id (MOS-SEC-010's parameters are not negotiable). Calling it
directly from an async middleware would block the event loop for a quarter of a second
per cache miss, stalling every other in-flight request including the SSE streams.
`anyio.to_thread.run_sync` copies the context, so the trace id is still visible inside.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Final
from urllib.parse import parse_qsl

import anyio.to_thread
from starlette.types import ASGIApp, Receive, Scope, Send

from medos.api.problems import build_problem, problem_response
from medos.db.tenancy import bind_current_tenant, reset_current_tenant
from medos.security.authn import (
    AuthenticationError,
    Authenticator,
    AuthenticatorUnavailable,
    Credential,
    CrossTenantDenied,
    MissingCredential,
    Principal,
)
from medos.security.logsafe import route_fields

__all__ = [
    "PUBLIC_PATHS",
    "TENANT_SELECTING_HEADERS",
    "TENANT_SELECTING_QUERY_PARAMS",
    "AuthenticationMiddleware",
    "principal_of",
    "problem_for",
]

log = logging.getLogger("medos.api")

# MOS-SEC-008's closed list, plus the two paths FastAPI serves the OpenAPI document from.
# `/docs` is the Swagger SHELL -- it contains no API data, it fetches `/openapi.json` in
# the browser -- so it is part of "the OpenAPI document" for this purpose. Nothing else
# is public, and this tuple is the whole surface: there is no per-route `public=True`
# decorator, because a route-local opt-out is how an endpoint becomes anonymous by
# accident.
PUBLIC_PATHS: Final[frozenset[str]] = frozenset(
    {"/healthz", "/readyz", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)

# MOS-API-003. `medicalos-tenant-id` is the header the spec reserves for a platform
# administrator; it is refused for everyone else, and this block can issue no
# platform-administrator credential at all (`api_keys.principal_kind` admits only `user`
# and `service_account`, because a platform admin needs the break-glass grant that
# 0002_tenancy.up.sql section 11 explains was deliberately not created). So today it is
# refused for everyone, which is the strictest reading and the easiest to relax.
TENANT_SELECTING_HEADERS: Final[frozenset[str]] = frozenset(
    {"medicalos-tenant-id", "x-tenant-id", "x-medicalos-tenant"}
)

# "there is no tenant path segment and no tenant query parameter" (MOS-API-003). There
# being no such parameter, naming one is refused rather than ignored: a caller who
# believes `?tenant_id=other` worked because it returned 200 has been taught something
# false about the platform.
TENANT_SELECTING_QUERY_PARAMS: Final[frozenset[str]] = frozenset(
    {"tenant", "tenant_id", "tenantid", "tenant_slug"}
)


def principal_of(scope_or_request: Any) -> Principal | None:
    """The authenticated principal for this request, or None on a public path.

    Accepts a raw ASGI `scope` or a Starlette `Request`, so a handler can call it without
    knowing which it has.
    """
    state = getattr(scope_or_request, "state", None)
    if state is not None and not isinstance(scope_or_request, dict):
        return getattr(state, "principal", None)
    if isinstance(scope_or_request, dict):
        return scope_or_request.get("state", {}).get("principal")
    return None  # pragma: no cover


def problem_for(
    exc: AuthenticationError, *, instance: str, trace_id: str
) -> dict[str, Any]:
    """Render a refusal as RFC 9457. Chapter 10 table 10.4-A.

    `class` is `authz_error` for every refusal except `AuthenticatorUnavailable`, which
    is `transport_failure` -- the caller's credential is not the problem and the correct
    client behaviour is to RETRY, which is the one thing `authz_error` tells it not to do
    (table 10.4-A marks `authz_error` `retryable: false`).

    `detail` comes from the exception CLASS, never from the request. MOS-SEC-136: a
    secret MUST NOT appear "in a problem+json `detail`", and the only way to guarantee
    that for every future refusal is to never interpolate request data into one.
    """
    transport = isinstance(exc, AuthenticatorUnavailable)
    return build_problem(
        status=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        problem_class="transport_failure" if transport else "authz_error",
        instance=instance,
        trace_id=trace_id,
    )


class AuthenticationMiddleware:
    """The PEP. Authenticate, refuse a tenant-selecting request, bind the tenant.

    Placement: OUTSIDE `TraceContextMiddleware` is wrong (the refusal would have no trace
    id) and INSIDE the exception handlers is wrong (a handler that touches the database
    would run unbound). `medos.api.app.create_app` adds it between them -- see the
    ordering comment there.
    """

    def __init__(
        self,
        app: ASGIApp,
        authenticator: Authenticator,
        *,
        public_paths: Iterable[str] = PUBLIC_PATHS,
    ) -> None:
        self.app = app
        self.authenticator = authenticator
        self.public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.public_paths:
            # No principal, and -- deliberately -- no tenant binding either. `/readyz`
            # runs `SELECT 1`, which reads no table and therefore needs no tenant
            # (medos/medos/api/app.py explains why that is the right probe). Anything on a
            # public path that DID touch a tenant-owned table would raise 42704, which is
            # the correct outcome: it would mean a tenant-scoped read had been added to
            # an anonymous surface.
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        trace_id = state.get("trace_id", "")

        try:
            principal = await self._authenticate(scope)
            self._refuse_tenant_selection(scope, principal)
        except AuthenticationError as exc:
            await self._refuse(exc, scope, receive, send, path=path, trace_id=trace_id)
            return

        state["principal"] = principal
        # THE line. See the module docstring.
        token = bind_current_tenant(principal.tenant_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_tenant(token)

    # -- internals --------------------------------------------------------------------
    async def _authenticate(self, scope: Scope) -> Principal:
        scheme, secret = _authorization(scope)
        if not secret:
            raise MissingCredential()
        credential = Credential(
            scheme=scheme,
            secret=secret,
            source_ip=_peer_address(scope),
        )
        return await anyio.to_thread.run_sync(
            self.authenticator.authenticate, credential
        )

    @staticmethod
    def _refuse_tenant_selection(scope: Scope, principal: Principal) -> None:
        """MOS-API-003, checked AFTER authentication because that is what it says.

        The header is refused "for every other principal" -- i.e. the rule is about the
        principal, so the principal has to exist before it can be applied. An
        unauthenticated request carrying the header gets `401`, not `403`, and that
        ordering is deliberate: `403` would confirm that the header is a real feature to
        someone who has not proved they are anybody.
        """
        for name, _value in scope.get("headers", ()):
            if name.decode("latin-1").lower() in TENANT_SELECTING_HEADERS:
                log.warning(
                    "tenant_selection_refused",
                    extra={"via": "header", **principal.log_fields()},
                )
                raise CrossTenantDenied()

        raw_qs = scope.get("query_string", b"")
        if raw_qs:
            for key, _value in parse_qsl(raw_qs.decode("latin-1"), keep_blank_values=True):
                if key.lower() in TENANT_SELECTING_QUERY_PARAMS:
                    log.warning(
                        "tenant_selection_refused",
                        extra={"via": "query", **principal.log_fields()},
                    )
                    raise CrossTenantDenied()

    async def _refuse(
        self,
        exc: AuthenticationError,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        path: str,
        trace_id: str,
    ) -> None:
        # `route_fields` and not `path`: MOS-SEC-105's `Structured logs` row forbids P2
        # "in any form, including ... a URL", and this middleware is generic -- it must
        # not assume the surface it guards has MOS-API-006's PHI-free path space. It runs
        # BEFORE the router, so `path_params` is not on the scope yet and only the
        # template survives, which is the fail-closed direction. `instance` below keeps
        # the full path: that is the caller's own request URI echoed back to the caller
        # (MOS-API-006 permits a StudyInstanceUID there), not a publication into the
        # platform log index, which is the whole asymmetry MOS-SEC-105 turns on.
        log.info(
            "authentication_refused",
            extra={"trace_id": trace_id, "code": exc.code, "status": exc.status,
                   **route_fields(scope)},
        )
        headers = {}
        if exc.status == 401:
            # RFC 9110 section 11.6.1: a 401 MUST carry WWW-Authenticate. No `realm`
            # and no `error_description`: a realm is a login prompt in a browser and
            # this is a machine surface, and an error description here would be a second
            # place a refusal reason is spelled (the problem document is the first).
            headers["WWW-Authenticate"] = "Bearer"
        response = problem_response(
            problem_for(exc, instance=path, trace_id=trace_id), headers=headers
        )
        await response(scope, receive, send)


def _authorization(scope: Scope) -> tuple[str, str]:
    """`(scheme, credential)` from the `Authorization` header. `("", "")` when absent."""
    for name, value in scope.get("headers", ()):
        if name.decode("latin-1").lower() == "authorization":
            raw = value.decode("latin-1").strip()
            scheme, _, rest = raw.partition(" ")
            return scheme, rest.strip()
    return "", ""


def _peer_address(scope: Scope) -> str | None:
    """The peer address, for MOS-SEC-011's `source_ip_allowlist`.

    Deliberately the TRANSPORT peer and NOT `X-Forwarded-For`. A forwarded header is
    caller-controlled unless every hop in front of this process is known to rewrite it,
    and trusting it by default would turn an IP allowlist into a string the attacker
    chooses. When a reverse proxy is placed in front, uvicorn's `--proxy-headers` with an
    explicit `--forwarded-allow-ips` is the supported way to make `scope["client"]` the
    real peer -- one configuration decision, made by the operator, in the place that
    knows the topology.
    """
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return None
