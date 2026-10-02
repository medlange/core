# SPDX-License-Identifier: Apache-2.0
"""FastAPI app factory. CONTRACT.md section 1: "app.py -- FastAPI app factory".

A FACTORY and not a module-level `app`
--------------------------------------
CONTRACT.md section 11: "No global mutable state. Pass the connection; do not import a
singleton." `MOS-REL-046` states the testable form of the same rule: "A test MUST be able
to construct two independent instances of any component in one process." A module-level
`app = FastAPI()` bound to a module-level connection fails that, so there is none here.
Run it with:

    uvicorn medos.api.app:create_app --factory --port 8000

`create_app(connect=...)` takes the connection opener as an argument; `tests/integration/
test_api.py` passes one bound to a throwaway database, which is what makes the tests
independent of a developer's local DSN.

What this module owns
---------------------
Four things, all of them cross-cutting and none of them business logic:

  1. The trace-id middleware (`MOS-API-004`): accept an inbound W3C `traceparent`, mint a
     trace id otherwise, put it on the request scope, and return `MedicalOS-Trace-Id` on
     EVERY response including every error response. The same value appears as the
     `trace_id` member of every problem document and every SSE envelope.
  2. The RFC 9457 exception handlers (`medos.api.problems`), so no 4xx or 5xx can leave
     this process as an HTML page or a bare string (`MOS-API-035`).
  3. Structured JSON access logging under `MOS-SEC-105`'s `Structured logs` row, which
     admits P0 and P1 and forbids P2: the record names the route TEMPLATE and the
     allowlisted path parameters resolved by `medos.security.logsafe.route_fields`, never
     the resolved path. This middleware is mounted by `medos.gateway.app` as well, and
     `MOS-DATA-007`'s Gateway URL space is P2 end to end (`{st}` a `StudyInstanceUID`,
     `{se}` a `SeriesInstanceUID`, `{sop}` a `SOPInstanceUID`), so a path logged here is
     a per-request PHI disclosure on that surface. See `logsafe`'s module docstring for
     why a minted `2.25.…` UID is treated exactly like a hospital's.
  4. `/healthz` and `/readyz`, the two probes `MOS-API-001` permits outside `/api/v1`.

What it deliberately does NOT own: rate limiting, and CORS defaults wide enough to
matter. Authentication and the tenant binding arrived with weeks 3-5 items 1 and 2 and
live one file over, in `medos/medos/api/auth.py`: this module only decides WHERE in the
middleware stack the PEP sits, which is a cross-cutting decision and therefore belongs to
the factory.

Spec: MOS-API-001, MOS-API-002, MOS-API-004, MOS-API-006, MOS-API-035, MOS-REL-046,
MOS-REL-050, MOS-REL-052, MOS-REL-053, MOS-REL-084, MOS-SEC-105, MOS-SEC-106,
MOS-SEC-110, MOS-SEC-117, CONTRACT.md sections 0, 1 and 11.

REPORTED, NOT SILENTLY RESOLVED: CONTRACT.md section 11 reads "Never log a PHI value --
log UIDs only, never names or MRNs", which licenses exactly the disclosure `MOS-SEC-105`
forbids on this surface. `MOS-EXEC-079` licenses UIDs too, but only in a bus envelope or
payload, and closes with "Chapter 8 is authoritative for the full PHI class list". This
module follows chapter 8. Both texts are listed in the component report.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

import psycopg
from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from medos.api.auth import AuthenticationMiddleware
from medos.api.problems import (
    TRACE_HEADER,
    build_problem,
    install_exception_handlers,
    problem_response,
    trace_id_of,
)
from medos.api.routes_events import router as events_router
from medos.api.routes_identity import router as identity_router
from medos.api.routes_jobs import router as jobs_router
from medos.api.routes_registry import router as registry_router
from medos.api.routes_reviews import router as reviews_router
from medos.capabilities import providers as capability_providers
from medos.db import conn as dbconn
from medos.db.repo import new_trace_id
from medos.security.apikeys import ApiKeyAuthenticator
from medos.security.authn import Authenticator
from medos.security.keyformat import api_key_env
from medos.security.logsafe import install_uid_shape_redaction, route_fields

__all__ = [
    "create_app",
    "TraceContextMiddleware",
    "TenantContextMiddleware",  # tombstone; raises. See the class docstring.
    "configure_logging",
    "API_VERSION",
]

API_VERSION = "0.1.0"

#: The name the OpenAPI document gives the bearer scheme. One constant, because
#: `tests/unit/test_openapi_document.py` asserts against the served document and a
#: second spelling would make that test pass while a client still failed.
API_KEY_SCHEME = "MedicalOSApiKey"

# W3C Trace Context: `version-traceid-spanid-flags`. Only the 32-hex trace id is taken --
# a span id from another process is not this process's parent span and inventing that
# relationship would corrupt a trace rather than join it.
_TRACEPARENT_RE = re.compile(
    r"^[0-9a-f]{2}-(?P<trace_id>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$"
)

log = logging.getLogger("medos.api")


# =====================================================================================
# Logging
# =====================================================================================
class JsonLogFormatter(logging.Formatter):
    """CONTRACT.md section 11: "Log structured JSON. **Never log a PHI value**".

    There is no message interpolation and no `%s` formatting of arbitrary arguments: a
    record's `extra` fields are emitted as JSON members, which is what makes the PHI rule
    auditable. `MOS-REL-053` requires CI to scan log call sites for PHI-bearing field
    names, and that scan is only possible if the field names are literal.
    """

    _BUILTIN = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message",
        "asctime",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._BUILTIN and not key.startswith("_"):
                doc[key] = value
        if record.exc_info:
            # The traceback goes to the SERVER log only. MOS-API-042 keeps it out of the
            # response body; there is no rule against the operator seeing it.
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Attach the JSON formatter to `medos.api`, and arm `MOS-SEC-110`. Idempotent.

    `install_uid_shape_redaction()` is the SECOND control and is scoped to third-party
    loggers; the first is `route_fields` in the middleware below. Both are needed and
    neither substitutes for the other -- `MOS-SEC-110` says the shape filter "MUST NOT be
    the primary control", and it could not be one anyway, since it is a regex over a value
    space with no closed pattern (`2.25.9001` defeats the shape the requirement names).
    """
    install_uid_shape_redaction()
    logger = logging.getLogger("medos.api")
    logger.setLevel(level)
    if not any(getattr(h, "_medos_json", False) for h in logger.handlers):
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(JsonLogFormatter())
        handler._medos_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.propagate = False


# =====================================================================================
# Trace middleware
# =====================================================================================
class TraceContextMiddleware:
    """`MOS-API-004`, as raw ASGI rather than `BaseHTTPMiddleware`.

    Raw ASGI because `BaseHTTPMiddleware` pumps the response through an anyio stream,
    which adds a hop to every SSE frame written by `routes_events` and is the classic way
    a streaming endpoint starts buffering. This wrapper touches only the
    `http.response.start` message, so a stream stays a stream.

    It sits OUTSIDE Starlette's `ExceptionMiddleware`, so the trace header is added to
    handled error responses too; the unhandled-`Exception` path is served further out
    still by `ServerErrorMiddleware`, which is why `problem_response()` also sets the
    header itself.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        trace_id = self._inbound_trace_id(scope) or new_trace_id()
        scope.setdefault("state", {})["trace_id"] = trace_id
        started = time.perf_counter()
        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
                headers = list(message.get("headers", []))
                key = TRACE_HEADER.lower().encode("latin-1")
                if not any(h[0].lower() == key for h in headers):
                    headers.append((key, trace_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # MOS-SEC-105, `Structured logs` row: P2 is "no ... in any form, including
            # ... a URL". `scope["path"]` WAS logged here, and on the Gateway -- which
            # mounts this same middleware -- every one of MOS-DATA-007's routes carries a
            # StudyInstanceUID and most carry a Series and a SOP UID too, so the field was
            # a per-request P2 disclosure into the platform log index.
            #
            # `route_fields` runs AFTER the inner app, deliberately: the router has by
            # then written `path_params` onto this same scope dict, so the template and
            # the allowlisted parameters are both available without matching twice in the
            # request path. It yields `route` (MOS-SEC-117's template), the P0/P1
            # parameters on the allowlist, and MOS-SEC-106's `study_ref` in place of the
            # study UID -- a replacement, not a deletion, so per-study correlation
            # survives. `MOS-API-006`'s `job_<ULID>` is P1 and still appears, as `job_id`.
            log.info(
                "http_request",
                extra={
                    "trace_id": trace_id,
                    "method": scope.get("method"),
                    **route_fields(scope),
                    "status": status_holder.get("status"),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )

    @staticmethod
    def _inbound_trace_id(scope: Scope) -> str | None:
        for name, value in scope.get("headers", ()):
            if name.lower() == b"traceparent":
                match = _TRACEPARENT_RE.match(value.decode("latin-1").strip())
                if match and set(match["trace_id"]) != {"0"}:
                    return match["trace_id"]
        return None


class TenantContextMiddleware:
    """REMOVED at weeks 3-5 item 2. Kept as a tombstone so the deletion is reviewable.

    This middleware bound `MEDOS_TENANT_ID` -- deployment configuration -- as the tenant
    of every request, which was the honest shape of an install with no authentication.
    Item 2 of docs/spec/15-delivery.md 15.2.4 replaced it with
    `medos.api.auth.AuthenticationMiddleware`, which binds `principal.tenant_id` off the
    stored credential row (`MOS-API-003`: "The credential determines the `Tenant`").

    It is not deprecated-but-available. It is gone, and calling it raises, because a
    middleware that binds a CONFIGURED tenant is precisely the bypass the replacement
    exists to remove: one `app.add_middleware(TenantContextMiddleware, tenant_id=...)`
    left in a deployment would serve every unauthenticated request as that tenant.
    """

    def __init__(self, app: ASGIApp, tenant_id: str) -> None:
        raise RuntimeError(
            "TenantContextMiddleware was removed at weeks 3-5 item 2. The tenant comes "
            "from the authenticated principal: use medos.api.auth."
            "AuthenticationMiddleware (MOS-API-003, MOS-SEC-008)."
        )


# =====================================================================================
# The factory
# =====================================================================================
def create_app(
    *,
    connect: Callable[..., psycopg.Connection[Any]] | None = None,
    dsn: str | None = None,
    configure_logs: bool = True,
    authenticator: Authenticator | None = None,
    extra_routers: Sequence[APIRouter] = (),
) -> FastAPI:
    """Build one independent app instance.

    `connect` is the injected collaborator (CONTRACT.md section 11): a callable that
    opens a connection and accepts `medos.db.conn.connect`'s keyword arguments -- the SSE
    route passes `autocommit=True`. When it is omitted, one is bound from `dsn` (or
    `MEDOS_DATABASE_URL` via `dsn_from_env`). Two `create_app()` calls with different
    DSNs share nothing, which is `MOS-REL-046`.

    `authenticator` is the second injected collaborator and the `Authenticator` port of
    `MOS-SEC-009`. When omitted, the API-key driver is built over the same `connect`.
    There is deliberately NO `auth=False` and no `MEDOS_DISABLE_AUTH`: `MOS-SEC-008`
    admits anonymous access on three paths only, and a switch that removes the PEP is a
    switch somebody sets in a staging environment that later becomes production. A test
    that wants a different principal passes a different `Authenticator`, which is the
    port doing its job.

    `tenant_id` is GONE as a parameter. It used to configure `TenantContextMiddleware`;
    the tenant now comes from the authenticated principal and from nothing else
    (`MOS-API-003`).
    """
    # Armed unconditionally, outside the `configure_logs` branch -- see the same block in
    # `medos.gateway.app.create_app` for why a PHI control may not sit behind a flag.
    install_uid_shape_redaction()
    if configure_logs:
        configure_logging()

    # FAIL CLOSED AT STARTUP, before a socket is bound.
    #
    # `MEDOS_CAPABILITY_PROVIDERS` is this deployment's statement of which capabilities it
    # serves, and `medos.capabilities.providers.resolve()` is the ONE place it is read --
    # by this process and, from the identical value, by `medos-worker`. Resolving it here
    # means a provider that does not import, exposes no `worker_registry`, returns
    # something that is not a Mapping of Capability, or collides with a platform name
    # stops the API from starting and says which provider did it.
    #
    # The alternative -- discovering it on the first `POST /api/v1/jobs` -- is a
    # deployment that looks healthy, answers `/readyz`, and refuses (or worse, accepts)
    # jobs for a capability whose state nobody has been told about.
    #
    # The result is stashed on `app.state` below so an operator can see what this process
    # actually serves; the routes do NOT read it from there. They call the resolver again
    # (memoised on the configuration, so it costs nothing) precisely so the answer can
    # never be a stale copy taken at startup.
    capabilities = capability_providers.resolve()

    if connect is not None:
        opener: Callable[..., psycopg.Connection[Any]] = connect
    else:
        resolved = dsn or dbconn.dsn_from_env()

        def opener(**kwargs: Any) -> psycopg.Connection[Any]:
            return dbconn.connect(resolved, application_name="medos-api", **kwargs)

    app = FastAPI(
        title="MedicalOS control plane",
        version=API_VERSION,
        summary="Jobs, registries, curation and training over DICOMweb-sourced studies",
        # THIS TEXT SAID "No auth, no tenancy, no registries" AND WAS FALSE ON ALL THREE.
        # It described the weeks 1-2 slice and was never revised as the surface grew, so
        # `/docs` -- the first page a stranger opens, and one of the six paths served
        # WITHOUT a credential -- told them the opposite of what the code does:
        # authentication is `AuthenticationMiddleware` plus `ApiKeyAuthenticator` below,
        # tenancy is bound per request off the principal, and the registry router is
        # included. A public page describing a security posture the deployment does not
        # have is worse than no page.
        description=(
            "Every route under `/api/v1` requires a bearer API key except the six public "
            "paths listed in `medos.api.auth.PUBLIC_PATHS`, and every request is bound to "
            "the tenant on its principal. Mint a development key with "
            "`python -m medos.security.cli issue` and send it as "
            "`Authorization: Bearer mos_<env>_<key_id>_<secret>`.\n\n"
            "This document is generated from the running application, so it describes the "
            "surface this deployment actually serves. It is NOT the checked-in artefact "
            "`MOS-API-086` requires; that generator does not exist yet and the debt is "
            "recorded in `tests/unit/test_required_documents.py`."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    # Not global state: the factory's own argument, stored on the instance it built.
    app.state.medos_connect = opener
    app.state.medos_capabilities = capabilities

    authn = authenticator or ApiKeyAuthenticator(opener, env=api_key_env())
    app.state.medos_authenticator = authn

    # Starlette's add_middleware inserts at position 0, so the LAST one added is the
    # OUTERMOST.
    #
    # The order below is TraceContext (outermost) -> Authentication -> exception handlers
    # -> routers, and each boundary is deliberate:
    #
    #   * Authentication is INSIDE TraceContext so that a `401` carries a trace id in its
    #     header and in its problem document (`MOS-API-004` says "EVERY response
    #     including every error response", and a refusal a caller cannot quote back is
    #     the response an integrator most needs to quote back).
    #   * Authentication is OUTSIDE the routers and the exception handlers so that the
    #     tenant binding it establishes covers every handler, every exception handler and
    #     the SSE generator. A PEP the error path routes around is not a PEP.
    app.add_middleware(AuthenticationMiddleware, authenticator=authn)
    app.add_middleware(TraceContextMiddleware)
    install_exception_handlers(app)
    # Chapter 10 table 10.2-B row 7. Authn only, and Core: a surface must be able
    # to ask who it is signed in as without a training plane present.
    app.include_router(identity_router)
    app.include_router(jobs_router)
    app.include_router(events_router)
    # MOS-SAFE-069's eight rows. Mounted after the job routes and inside the same PEP:
    # `AuthenticationMiddleware` is added below the routers, so a review endpoint is
    # authenticated and tenant-bound on exactly the same path as a job endpoint.
    app.include_router(reviews_router)
    # Chapter 10 table 10.2-B rows 14-18 and 28-31, backed by the one `artifacts`
    # table of section 15.2.6. Mounted beside the others and inside the same PEP:
    # `MOS-REG-107` requires every registry read to pass the same RLS chokepoint as
    # the rest of the platform, and that chokepoint is the tenant binding this
    # middleware stack establishes.
    app.include_router(registry_router)
    # THE TRAINING AND CURATION ROUTERS USED TO BE MOUNTED HERE, BY NAME. They are not
    # part of this deployable any more: `medos/medos/api/app.py` builds the PACS-and-models
    # service, and the model-preparation service composes on top of it by passing its
    # own routers in. Chapter 10 table 10.2-B rows R6-R31 are served by that second
    # deployable, not by this one.
    #
    # COMPOSED AT BUILD TIME, NOT DISCOVERED AT RUN TIME. `MOS-REL-108` forbids dynamic
    # module import in a platform process on `MOS-CONF-109`'s IEC 62304 section 4.3
    # segregation argument, and an earlier version of this platform did resolve
    # providers with `importlib.import_module()` on an environment variable before that
    # was removed. So there is no plugin scan and no `MEDOS_ENABLE_TRAINING`: the caller
    # holds the routers already, having imported them itself, and the set of surfaces a
    # process serves is a property of which entrypoint was started.
    #
    # Mounted here rather than after `/healthz` so the relative order of the routers is
    # unchanged: a later router MUST NOT be able to shadow an earlier one's route, and
    # the training pair owns path prefixes disjoint from everything above. They land
    # inside the same PEP as every other surface, so a curation route is authenticated
    # and tenant-bound on exactly the same path as a job endpoint -- load-bearing beyond
    # the usual, because row R24 is the route closest to a patient on the platform.
    for extra in extra_routers:
        app.include_router(extra)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        """Liveness. Deliberately does NOT touch the database: a probe that fails when
        Postgres is down asks the orchestrator to restart a process that is working."""
        return JSONResponse({"status": "ok", "version": API_VERSION})

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request) -> Response:
        """Readiness. Touches the database, because a control plane that cannot reach
        `jobs` cannot admit one and should be taken out of rotation.

        `request` is here for one reason: `MOS-API-004` requires the trace id on every
        response INCLUDING every error response, and as the document's `trace_id` member.
        A probe that answers `503` with an empty one hands the operator nothing to quote.
        """
        trace_id = trace_id_of(request)
        try:
            conn = opener()
        except psycopg.Error as exc:
            return problem_response(_unready(type(exc).__name__, trace_id))
        try:
            # Deliberately NOT inside `tenant_tx`: `SELECT 1` reads no table, so it needs
            # no tenant context, and a readiness probe that required one would report the
            # API unready whenever the tenant binding was misconfigured -- hiding a
            # configuration error behind a database error. It answers exactly one
            # question: can this process reach Postgres.
            conn.execute("SELECT 1")
        except psycopg.Error as exc:
            return problem_response(_unready(type(exc).__name__, trace_id))
        finally:
            conn.close()
        return JSONResponse({"status": "ready", "version": API_VERSION})

    # THE SERVED DOCUMENT DECLARED NO SECURITY SCHEME, which is not a cosmetic omission:
    # a client generated from it sends no `Authorization` header and therefore fails on
    # every authenticated route, and `/docs` offers no way to enter a key. The surface was
    # authenticated and the document said nothing about it, so the document was wrong in
    # the direction that wastes a stranger's afternoon.
    #
    # Injected after the fact rather than passed to the constructor because FastAPI takes
    # no `security_schemes` argument; this is the documented override point.
    def _openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {})["securitySchemes"] = {
            API_KEY_SCHEME: {
                "type": "http",
                "scheme": "bearer",
                # Not `JWT`. `bearerFormat` is a free-text hint, and naming the real
                # format is the difference between a reader guessing and a reader
                # knowing. Chapter 8 section 8.2.2, `medos.security.keyformat`.
                "bearerFormat": "mos_<env>_<key_id>_<secret>",
                "description": (
                    "An API key issued by `python -m medos.security.cli issue`. The "
                    "secret is shown once, at issue; only its argon2id hash is stored."
                ),
            }
        }
        # Applied globally rather than per route. `PUBLIC_PATHS` is a closed list in
        # `medos.api.auth` and none of its members is `include_in_schema=True`, so there
        # is no operation here that should be exempt -- and a per-route opt-out is
        # exactly how an endpoint becomes anonymous by accident, which that module's
        # comment already refuses to allow.
        schema["security"] = [{API_KEY_SCHEME: []}]
        app.openapi_schema = schema
        return schema

    app.openapi = _openapi  # type: ignore[method-assign]

    return app


def _unready(reason: str, trace_id: str) -> dict[str, Any]:
    """`503` / `transport_failure`. The exception TYPE name only -- never its message,
    which in `psycopg` carries the DSN host and the database name (MOS-API-042)."""
    return build_problem(
        status=503,
        code="DATABASE_UNAVAILABLE",
        title="Database unavailable",
        detail="The control plane cannot reach its database.",
        problem_class="transport_failure",
        instance="/readyz",
        trace_id=trace_id,
        dependency="postgres",
        reason=reason,
    )
