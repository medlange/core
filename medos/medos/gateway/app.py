# SPDX-License-Identifier: Apache-2.0
"""`medicalos-gateway` -- the DICOMweb proxy that holds the only PACS credential.

docs/spec/15-delivery.md section 15.2.4 item 3:

    "The DICOM Gateway holding the only PACS credential, with the viewer and every worker
    re-pointed at it. Built now because retrofitting a proxy under a working viewer and
    three workers means re-pointing all of them, and because it is the only place a
    per-patient PHI-access audit can exist."

Requirements implemented in this module
---------------------------------------
MOS-DATA-002/005  the PACS credential is read by `medos.gateway.backend.backend_from_env`
                  and by nothing else in the deployment. Every other component now points
                  at this service.
MOS-DATA-007      the exposed surface is exactly the twelve routes of the table, declared
                  one per handler.
MOS-DATA-008      no DELETE, no native PACS API, no catch-all. An unmatched path is `404`
                  with `problem+json` and is NOT forwarded -- `_unmatched` below is the
                  handler that makes that true rather than a claim.
MOS-DATA-009      the effective tenant is the principal's; `{t}` is compared for equality
                  and a mismatch is `403` plus a `gateway.tenant_mismatch` audit row.
MOS-DATA-011      QIDO-RS is filtered AFTER the backend answers, by intersection with the
                  tenant's owned set. The tenancy predicate is never pushed into the
                  backend query.
MOS-DATA-012      STOW-RS resolves every StudyInstanceUID in the payload across tenants,
                  rejects the whole request with `409` if another tenant owns one, and
                  creates the `studies` row in the same transaction as the audit row.
MOS-DATA-013      a study outside the caller's tenant set is `404`, never `403`.
MOS-DATA-021      consumer class is resolved per request and recorded on every audit row.
MOS-DATA-022      one `AuditEvent` per PHI-bearing response, written when the body ends.
MOS-DATA-023      WADO-RS streams; per-principal concurrency limit and per-response
                  instance cap, both `429` with `Retry-After`.
MOS-DATA-024      quarantined studies are subtracted from every QIDO-RS response.
MOS-DATA-025      fail closed: a projection failure is `503` and never a passthrough.
MOS-DATA-026      the five required metrics, with no UID or identifier in any label.

WHAT THIS BLOCK DOES NOT DO, AND WHERE IT SAYS SO
--------------------------------------------------
De-identification on egress (MOS-DATA-027..037) is NOT implemented. That is a real gap and
it is bounded by MOS-DATA-021's own table: `clinical_viewer` and `platform_writer` are
"De-identification on egress: none", and those are the only two consumer classes this
deployment's principals resolve to. Any OTHER class reaching a retrieval route is refused
with `503 deid_not_implemented` rather than served identified pixels -- MOS-DATA-037's
fail-closed rule, applied to an unimplemented stage instead of a failing one. A `service`
principal is the conservative default of MOS-DATA-021, so a mis-configured principal fails
closed by construction. REPORTED as the largest single gap in this component.

Pixel-PHI screening (MOS-DATA-038..042) is absent for the same reason and refuses the same
way.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from medos.api.app import TraceContextMiddleware, configure_logging
from medos.api.problems import (
    build_problem,
    problem_response,
    trace_id_of,
)
from medos.db import conn as dbconn
from medos.db.audit import new_request_id
from medos.db.tenancy import bind_current_tenant, reset_current_tenant
from medos.gateway import audit as gwaudit
from medos.gateway.auth import (
    Principal,
    StaticApiKeyAuthenticator,
    TenantMismatch,
    credential_from_headers,
    resolve_consumer_class,
)
from medos.gateway.backend import (
    BackendResolver,
    PacsBackend,
    SharedBackendResolver,
    UnknownBackend,
    backend_from_env,
)
from medos.gateway.metrics import Metrics
from medos.gateway.projection import (
    ProjectionUnavailable,
    admit_study,
    is_owned,
    owned_subset,
    quarantined_study_uids,
    study_owner,
    study_row,
)
from medos.security.authn import AuthenticationError
from medos.security.logsafe import install_uid_shape_redaction, route_fields

__all__ = ["create_app", "GatewayConfig", "DEFAULT_PORT"]

log = logging.getLogger("medos.gateway")

DEFAULT_PORT = 8043

DICOM_JSON = "application/dicom+json"
MULTIPART_DICOM = 'multipart/related; type="application/dicom"'

# MOS-DATA-021: the two classes whose egress row reads "De-identification on egress: none".
# Everything else needs a stage this block does not ship, and is refused rather than served.
_NO_DEID_CLASSES: frozenset[str] = frozenset({"clinical_viewer", "platform_writer"})

# The study-identifier tag in DICOM JSON. MOS-DATA-011's intersection is over this key.
TAG_STUDY_UID = "0020000D"
TAG_SERIES_UID = "0020000E"
TAG_SOP_UID = "00080018"

# Hop-by-hop headers (RFC 9110 section 7.6.1) plus the two a proxy must own. Forwarding
# `Content-Length` alongside a re-chunked body is how a proxy truncates a response.
_HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailer", "transfer-encoding", "upgrade",
        "content-length", "content-encoding",
    }
)

# Headers a caller may influence that MUST NOT reach the backend. `authorization` is the
# whole point: the caller's Gateway token must never be replayed at the PACS, and the
# PACS credential must never be derivable from what the caller sent (MOS-DATA-002).
_STRIP_FROM_REQUEST = frozenset(
    {"authorization", "cookie", "host", "content-length", "connection", "x-forwarded-for"}
)

# Response headers that name the PACS PRODUCT rather than its address. MOS-SEC-091: the
# deployment shape behind the Gateway "MUST NOT be visible to any caller". Dropped rather
# than scrubbed, because there is nothing in `Server: Orthanc/1.12.4` a caller may keep.
_STRIP_FROM_RESPONSE = frozenset({"server", "x-powered-by", "via"})


@dataclass
class GatewayConfig:
    """Everything the Gateway process needs. Read once, at start-up (MOS-DATA-005)."""

    max_inflight_per_principal: int = 4       # MOS-DATA-023's default
    max_instances_per_response: int = 5000    # MOS-DATA-023's default
    max_stow_bytes: int = 512 * 1024 * 1024
    retry_after_seconds: int = 5
    chunk_bytes: int = 256 * 1024
    # The public origin callers reach this Gateway on, used as the replacement when a
    # backend URL is scrubbed out of a response. "" no longer DISABLES the scrub -- see
    # `_gateway_uri_base`: unset now means "rewrite to a relative reference", because the
    # confidentiality of the PACS address (MOS-SEC-088) may not depend on an optional
    # environment variable being set.
    public_base: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> GatewayConfig:
        src = dict(os.environ) if env is None else env
        return cls(
            max_inflight_per_principal=int(src.get("MEDOS_GATEWAY_MAX_INFLIGHT") or 4),
            max_instances_per_response=int(src.get("MEDOS_GATEWAY_MAX_INSTANCES") or 5000),
            max_stow_bytes=int(src.get("MEDOS_GATEWAY_MAX_STOW_BYTES") or 512 * 1024 * 1024),
            public_base=(src.get("MEDOS_GATEWAY_PUBLIC_BASE") or "").rstrip("/"),
        )


class _InFlight:
    """MOS-DATA-023's per-principal concurrency limit. A counter, not a semaphore.

    A semaphore would BLOCK the fifth retrieval until a slot frees. The requirement says
    "exceeding either returns `429` with `Retry-After`", which is a refusal, not a queue --
    and a queue is worse here: a viewer whose fifth thumbnail request hangs for ninety
    seconds looks broken, while a 429 with `Retry-After` is something a client can act on.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def acquire(self, key: str) -> bool:
        with self._lock:
            n = self._counts.get(key, 0)
            if n >= self.limit:
                return False
            self._counts[key] = n + 1
            return True

    def release(self, key: str) -> None:
        with self._lock:
            n = self._counts.get(key, 0) - 1
            if n <= 0:
                self._counts.pop(key, None)
            else:
                self._counts[key] = n


@dataclass
class _Ctx:
    """The per-request facts every route needs, resolved once by `_authorise`."""

    principal: Principal
    consumer_class: str
    backend: PacsBackend
    trace_id: str
    request_id: str
    source_ip: str | None
    user_agent: str | None
    instance: str
    operation: str
    conn: psycopg.Connection[Any]
    # MOS-DATA-009 / MOS-SEC-075: the ContextVar `tenant_tx()` reads, bound from the
    # PRINCIPAL and never from the URL. Reset by the route's `finally` -- `_Ctx.release()`.
    tenant_token: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def release(self) -> None:
        """Close the request connection and unbind the tenant. Idempotent."""
        try:
            self.conn.close()
        finally:
            if self.tenant_token is not None:
                reset_current_tenant(self.tenant_token)
                self.tenant_token = None


# =====================================================================================
# The factory
# =====================================================================================
def create_app(
    *,
    connect: Callable[..., psycopg.Connection[Any]] | None = None,
    dsn: str | None = None,
    authenticator: Any | None = None,
    resolver: BackendResolver | None = None,
    config: GatewayConfig | None = None,
    session: requests.Session | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    """Build one Gateway instance. Every collaborator is injected (CONTRACT.md section 11).

    `resolver` is `MOS-DATA-014`'s `BackendResolver`. It defaults to a
    `SharedBackendResolver` over `backend_from_env()`, which is the compose stack's
    `shared_gateway_filtered` deployment; passing a `PerTenantBackendResolver` instead is
    the whole of what "one backend per tenant MUST be expressible without a Gateway code
    change" means, and no line below this one reads a URL or a credential directly.
    """
    # MOS-SEC-110 is armed BEFORE the `configure_logs` branch and outside it, on purpose.
    # `configure_logs` decides whether this process owns the JSON handler on `medos.api`
    # -- an output-format decision a test may take over. It MUST NOT decide whether a PHI
    # control runs: that is the `MEDOS_DISABLE_AUTH` shape `medos.api.app.create_app`'s
    # docstring rejects, "a switch that removes the PEP is a switch somebody sets in a
    # staging environment that later becomes production". Every library in this process
    # that renders a URL into a log message -- `urllib3.connectionpool` at DEBUG,
    # `uvicorn.access`, `httpx` under a TestClient -- writes MOS-DATA-007's UID-shaped
    # paths, and no flag may turn the scrubber off.
    install_uid_shape_redaction()
    if configure_logs:
        configure_logging()

    cfg = config or GatewayConfig.from_env()
    auth = authenticator or StaticApiKeyAuthenticator.from_env()
    backends = resolver or SharedBackendResolver(backend_from_env())
    metrics = Metrics()
    inflight = _InFlight(cfg.max_inflight_per_principal)

    if connect is not None:
        opener: Callable[..., psycopg.Connection[Any]] = connect
    else:
        resolved_dsn = dsn or dbconn.dsn_from_env()

        def opener(**kwargs: Any) -> psycopg.Connection[Any]:
            # autocommit=True, and it is not a shortcut. `medos.db.tenancy.tenant_tx()`
            # documents itself as "on a connection with no open transaction it BEGINs and
            # COMMITs on exit; inside a caller's open transaction it is a SAVEPOINT and
            # the caller commits." On a non-autocommit psycopg connection, the FIRST bare
            # `conn.execute()` -- `study_owner()`'s cross-tenant probe, for instance --
            # opens an implicit transaction, and every `tenant_tx()` after it silently
            # degrades to a savepoint that nothing ever commits. The symptom is a STOW-RS
            # that admits a study, writes its audit row, returns 200 and persists neither.
            # With autocommit, every `tenant_tx()` is a real BEGIN/COMMIT and `_stow`'s
            # explicit `conn.transaction()` is the one place a multi-statement unit exists.
            kwargs.setdefault("autocommit", True)
            return dbconn.connect(resolved_dsn, application_name="medos-gateway", **kwargs)

    http = session or requests.Session()

    app = FastAPI(
        title="MedicalOS DICOM Gateway",
        version="0.1.0",
        summary="QIDO-RS / WADO-RS / STOW-RS, tenancy-filtered and PHI-audited",
        description=(
            "The only component permitted to hold a PACS credential (MOS-DATA-002). "
            "Exposes exactly the surface of MOS-DATA-007 and nothing else."
        ),
        docs_url=None,       # MOS-DATA-008: the surface is the table, not a docs page
        openapi_url=None,
    )
    app.state.medos_connect = opener
    app.state.authenticator = auth
    app.state.resolver = backends
    app.state.config = cfg
    app.state.metrics = metrics
    app.state.inflight = inflight
    app.state.http = http

    app.add_middleware(TraceContextMiddleware)

    # -- operational surface ---------------------------------------------------------
    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Response:
        """Liveness. Touches neither the database nor the PACS, for the reason
        `medos.api.app` gives: a probe that fails when a dependency is down asks the
        orchestrator to restart a process that is working."""
        return JSONResponse({"status": "ok", "service": "medos-gateway"})

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request) -> Response:
        """Readiness. MOS-DATA-025's fail-closed posture, as a probe.

        The projection is checked and the PACS is NOT. A Gateway that cannot reach the
        projection MUST refuse every request (503), so it is not ready. A Gateway whose
        backend is down still correctly answers 404 for a foreign study and still refuses
        a tenant mismatch, so it is ready-but-degraded, and flapping the whole service out
        of the load balancer because a PACS rebooted would be the wrong reaction.
        """
        try:
            conn = request.app.state.medos_connect()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"status": "unready", "reason": "database_unreachable",
                 "detail": type(exc).__name__}, status_code=503)
        try:
            conn.execute("SELECT 1 FROM pacs_backends LIMIT 1").fetchone()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"status": "unready", "reason": "projection_unreachable",
                 "detail": type(exc).__name__}, status_code=503)
        finally:
            conn.close()
        return JSONResponse({"status": "ok"})

    @app.get("/metrics", include_in_schema=False)
    def metrics_route(request: Request) -> Response:
        """MOS-DATA-026's five metric families, Prometheus text format.

        No `prometheus_client` dependency: five counters and one histogram is less code
        than the import, and `medos/deploy/compose/requirements.txt` pins a deliberately small
        runtime set.
        """
        return Response(
            request.app.state.metrics.render(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    _install_routes(app)
    _install_problem_handlers(app)
    return app


# =====================================================================================
# MOS-DATA-007: the exposed surface. Twelve routes, declared one per line of the table.
# =====================================================================================
def _install_routes(app: FastAPI) -> None:
    # Literal segments are declared BEFORE the parameterised siblings they would
    # otherwise be captured by: Starlette matches in declaration order, so
    # `/studies/{st}/metadata` must precede nothing here, but `/studies/{st}/series/{se}
    # /rendered` must precede `/studies/{st}/series/{se}/instances/{sop}`... it does not,
    # because the segment counts differ. The one genuine ordering constraint is that
    # `POST /studies` precede `POST /studies/{st}` only if `{st}` could match the empty
    # string, which it cannot. Order below follows the table for readability.

    # ---- QIDO-RS -------------------------------------------------------------------
    @app.get("/dicomweb/{t}/studies")
    def qido_studies(t: str, request: Request) -> Response:
        """QIDO-RS study search, tenancy-filtered. MOS-DATA-011.

        The query is forwarded to the backend UNCHANGED and the tenancy predicate is
        applied to the response. MOS-DATA-011 is explicit about why the obvious
        optimisation is forbidden: "The Gateway MUST NOT delegate the tenancy predicate to
        the backend query, because a PACS that has no tenancy model will silently ignore
        an unknown matching key and return everything."
        """
        return _qido(request, t, "QIDO-RS.studies", "/studies", filter_studies=True)

    @app.get("/dicomweb/{t}/studies/{st}/series")
    def qido_series(t: str, st: str, request: Request) -> Response:
        return _qido(
            request, t, "QIDO-RS.series", f"/studies/{st}/series", study_instance_uid=st
        )

    @app.get("/dicomweb/{t}/studies/{st}/series/{se}/instances")
    def qido_instances(t: str, st: str, se: str, request: Request) -> Response:
        return _qido(
            request,
            t,
            "QIDO-RS.instances",
            f"/studies/{st}/series/{se}/instances",
            study_instance_uid=st,
            series_instance_uid=se,
        )

    # ---- WADO-RS metadata (no pixel data) ------------------------------------------
    @app.get("/dicomweb/{t}/studies/{st}/metadata")
    def wado_study_metadata(t: str, st: str, request: Request) -> Response:
        return _qido(
            request,
            t,
            "WADO-RS.study_metadata",
            f"/studies/{st}/metadata",
            study_instance_uid=st,
        )

    @app.get("/dicomweb/{t}/studies/{st}/series/{se}/metadata")
    def wado_series_metadata(t: str, st: str, se: str, request: Request) -> Response:
        return _qido(
            request,
            t,
            "WADO-RS.series_metadata",
            f"/studies/{st}/series/{se}/metadata",
            study_instance_uid=st,
            series_instance_uid=se,
        )

    # ---- WADO-RS retrieve (pixel data) ---------------------------------------------
    @app.get("/dicomweb/{t}/studies/{st}")
    def wado_study(t: str, st: str, request: Request) -> Response:
        return _wado(request, t, "WADO-RS.study", f"/studies/{st}", study_instance_uid=st)

    @app.get("/dicomweb/{t}/studies/{st}/series/{se}")
    def wado_series(t: str, st: str, se: str, request: Request) -> Response:
        return _wado(
            request,
            t,
            "WADO-RS.series",
            f"/studies/{st}/series/{se}",
            study_instance_uid=st,
            series_instance_uid=se,
        )

    @app.get("/dicomweb/{t}/studies/{st}/series/{se}/instances/{sop}")
    def wado_instance(t: str, st: str, se: str, sop: str, request: Request) -> Response:
        return _wado(
            request,
            t,
            "WADO-RS.instance",
            f"/studies/{st}/series/{se}/instances/{sop}",
            study_instance_uid=st,
            series_instance_uid=se,
            sop_instance_uid=sop,
        )

    @app.get("/dicomweb/{t}/studies/{st}/series/{se}/instances/{sop}/frames/{f}")
    def wado_frames(
        t: str, st: str, se: str, sop: str, f: str, request: Request
    ) -> Response:
        return _wado(
            request,
            t,
            "WADO-RS.frames",
            f"/studies/{st}/series/{se}/instances/{sop}/frames/{f}",
            study_instance_uid=st,
            series_instance_uid=se,
            sop_instance_uid=sop,
        )

    @app.get("/dicomweb/{t}/studies/{st}/series/{se}/rendered")
    def wado_rendered(t: str, st: str, se: str, request: Request) -> Response:
        """Rendered frame, viewer thumbnails only (MOS-DATA-007).

        Refused with `501` when the resolved backend declares
        `supports_rendered = false`, rather than forwarded and 404'd by the backend: the
        `PacsBackend` port carries `SupportsRendered()` precisely so this is a Gateway
        decision (MOS-DATA-014).
        """
        return _wado(
            request,
            t,
            "WADO-RS.rendered",
            f"/studies/{st}/series/{se}/rendered",
            study_instance_uid=st,
            series_instance_uid=se,
            needs_rendered=True,
        )

    # ---- STOW-RS -------------------------------------------------------------------
    # These two are `async def` while every route above is `def`, and the asymmetry is
    # load-bearing: `_stow` needs the whole body before it may admit anything
    # (MOS-DATA-012), and the only non-hacky way to read it is `await request.body()`.
    # The blocking work is then handed to the threadpool Starlette would have used anyway.
    @app.post("/dicomweb/{t}/studies")
    async def stow_studies(t: str, request: Request) -> Response:
        return await _stow_async(request, t, None)

    @app.post("/dicomweb/{t}/studies/{st}")
    async def stow_into_study(t: str, st: str, request: Request) -> Response:
        return await _stow_async(request, t, st)


async def _stow_async(
    request: Request, path_tenant: str, path_study_uid: str | None
) -> Response:
    from starlette.concurrency import run_in_threadpool

    cfg: GatewayConfig = request.app.state.config
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cfg.max_stow_bytes:
        return _too_large(request, cfg)
    payload = await request.body()
    if len(payload) > cfg.max_stow_bytes:
        return _too_large(request, cfg)
    return await run_in_threadpool(_stow, request, path_tenant, path_study_uid, payload)


def _too_large(request: Request, cfg: GatewayConfig) -> Response:
    return problem_response(
        build_problem(
            status=413,
            code="STOW_PAYLOAD_TOO_LARGE",
            title="Payload too large",
            detail=f"A STOW-RS body above {cfg.max_stow_bytes} bytes is refused.",
            problem_class="client_error",
            instance=request.url.path,
            trace_id=trace_id_of(request),
        )
    )


def _install_problem_handlers(app: FastAPI) -> None:
    """MOS-DATA-008: an unmatched path is `404` `problem+json` and is NOT forwarded.

    "The Gateway MUST NOT expose ... any path that proxies an unmatched URL to the
    backend." There is deliberately no catch-all route in `_install_routes`; this handler
    is what answers everything the twelve routes did not claim, including every `DELETE`,
    `PUT` and `PATCH` against a path that exists for `GET`.
    """
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        status = exc.status_code
        code = {
            404: "ROUTE_NOT_EXPOSED",
            405: "METHOD_NOT_EXPOSED",
        }.get(status, "GATEWAY_ERROR")
        detail = (
            "The DICOM Gateway exposes exactly the DICOMweb surface of MOS-DATA-007 and "
            "does not proxy unmatched paths to the backend (MOS-DATA-008)."
            if status in (404, 405)
            else str(exc.detail)
        )
        return problem_response(
            build_problem(
                status=status,
                code=code,
                title="Not part of the Gateway surface",
                detail=detail,
                problem_class="client_error",
                instance=request.url.path,
                trace_id=trace_id_of(request),
            )
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        # MOS-SEC-105: never `request.url.path`. A 500 on this service is raised from a
        # route whose URL is three DICOM UIDs (MOS-DATA-007), so the one log line that
        # used to carry the most PHI was the one written when something went wrong.
        log.exception("gateway_unhandled", extra=route_fields(request))
        return problem_response(
            build_problem(
                status=500,
                code="GATEWAY_INTERNAL_ERROR",
                title="The Gateway failed to complete the request",
                detail="An unexpected error occurred. The trace id identifies it in the logs.",
                problem_class="system_failure",
                instance=request.url.path,
                trace_id=trace_id_of(request),
            )
        )


# =====================================================================================
# Authorisation -- run identically by every route before a single byte moves
# =====================================================================================
class _Refused(Exception):
    """An authorisation outcome that is already a `Response`. Carries no PHI."""

    def __init__(self, response: Response) -> None:
        super().__init__("refused")
        self.response = response


def _authorise(
    request: Request,
    path_tenant: str,
    operation: str,
    *,
    study_instance_uid: str | None,
    write: bool = False,
) -> _Ctx:
    """Authenticate, check the tenant segment, check the scope, check study ownership.

    THE ORDER IS THE REQUIREMENT, not a style choice:

      1. authenticate          -> 401. Nothing is known about the deployment yet.
      2. tenant segment        -> 403 (MOS-DATA-009). Reveals nothing: a spoofed segment
                                  is a fact about the REQUEST, not about what is stored.
      3. scope                 -> 403 (MOS-DATA-013's second sentence: "`403` is reserved
                                  for MOS-DATA-009 and for a caller whose own tenant owns
                                  the study but who lacks `study.read`").
      4. study ownership       -> 404 (MOS-DATA-013). This is LAST because every earlier
                                  refusal must not depend on whether the study exists.

    Getting 3 and 4 the other way round would turn the scope check into an existence
    oracle for a caller with no scope at all.
    """
    app = request.app
    trace_id = trace_id_of(request)
    request_id = new_request_id()
    source_ip = _client_ip(request)
    user_agent = (request.headers.get("user-agent") or "")[:200] or None
    instance = request.url.path
    metrics = app.state.metrics

    # 1 ------------------------------------------------------------------------------
    try:
        credential = credential_from_headers(request.headers, source_ip=source_ip)
        principal: Principal = app.state.authenticator.authenticate(credential)
    except AuthenticationError as exc:
        metrics.denial(reason="unauthenticated")
        metrics.request(operation=operation, consumer_class="unknown", status=exc.status)
        raise _Refused(
            problem_response(
                build_problem(
                    status=exc.status,
                    code=exc.code,
                    title="Authentication failed",
                    detail=str(exc) or "no valid credential was presented",
                    problem_class="authz_error",
                    instance=instance,
                    trace_id=trace_id,
                ),
                headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else None,
            )
        ) from exc

    consumer_class = resolve_consumer_class(
        principal,
        clinical_use_mode=getattr(app.state.authenticator, "clinical_use_mode", "clinical"),
    )
    # MOS-DATA-009: "The effective tenant of a request is derived from the authenticated
    # principal, never from the URL." This is the binding, and it is the ONLY one in the
    # Gateway; `tenant_tx()` reads it and nothing here writes the session variable itself.
    tenant_token = bind_current_tenant(principal.tenant_id)
    conn = app.state.medos_connect()

    def refuse(
        status: int,
        code: str,
        title: str,
        detail: str,
        *,
        problem_class: str,
        reason_code: str,
        audit_action: str = "phi.access",
        audit_uid: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Refused:
        metrics.denial(reason=reason_code)
        metrics.request(operation=operation, consumer_class=consumer_class, status=status)
        try:
            gwaudit.record_denial(
                conn,
                tenant_id=principal.tenant_id,
                principal=principal,
                operation=operation,
                consumer_class=consumer_class,
                trace_id=trace_id,
                request_id=request_id,
                reason_code=reason_code,
                action=audit_action,
                study_instance_uid=audit_uid,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        except Exception:  # noqa: BLE001 - a failed audit must not become a 500 leak
            log.exception("gateway_denial_audit_failed", extra={"reason": reason_code})
        finally:
            conn.close()
            reset_current_tenant(tenant_token)
        return _Refused(
            problem_response(
                build_problem(
                    status=status,
                    code=code,
                    title=title,
                    detail=detail,
                    problem_class=problem_class,
                    instance=instance,
                    trace_id=trace_id,
                ),
                headers=headers,
            )
        )

    # 2 ------------------------------------------------------------------------------
    try:
        from medos.gateway.auth import require_tenant_match

        require_tenant_match(principal, path_tenant)
    except TenantMismatch as exc:
        raise refuse(
            403,
            exc.code,
            "Tenant mismatch",
            "The tenant segment of the path is not the authenticated principal's tenant "
            "(MOS-DATA-009).",
            problem_class="authz_error",
            reason_code="tenant_mismatch",
            audit_action="gateway.tenant_mismatch",
        ) from exc

    # 3 ------------------------------------------------------------------------------
    needed = "study.write" if write else "study.read"
    if not principal.has(needed):
        raise refuse(
            403,
            "INSUFFICIENT_SCOPE",
            "Insufficient scope",
            f"This principal does not hold {needed}.",
            problem_class="authz_error",
            reason_code="scope_denied",
        )

    # MOS-DATA-021 / MOS-DATA-037: a consumer class whose egress needs de-identification
    # cannot be served by this block, and is refused rather than served identified pixels.
    if not write and consumer_class not in _NO_DEID_CLASSES:
        raise refuse(
            503,
            "DEID_NOT_IMPLEMENTED",
            "De-identified egress is not available",
            f"Consumer class {consumer_class!r} requires de-identification on egress "
            "(MOS-DATA-021); this release implements none, and MOS-DATA-037 requires the "
            "egress to fail closed rather than emit identified data.",
            problem_class="system_failure",
            reason_code="deid_not_implemented",
        )

    # 4 ------------------------------------------------------------------------------
    if study_instance_uid is not None:
        try:
            owned = is_owned(conn, study_instance_uid)
            quarantined = study_instance_uid in quarantined_study_uids(conn)
        except ProjectionUnavailable as exc:
            conn.close()
            reset_current_tenant(tenant_token)
            metrics.request(operation=operation, consumer_class=consumer_class, status=503)
            raise _Refused(
                problem_response(
                    build_problem(
                        status=503,
                        code="PROJECTION_UNAVAILABLE",
                        title="The tenancy projection is unreachable",
                        detail="MOS-DATA-025: the Gateway fails closed rather than "
                               "falling back to unfiltered passthrough.",
                        problem_class="transport_failure",
                        instance=instance,
                        trace_id=trace_id,
                    ),
                    headers={"Retry-After": "5"},
                )
            ) from exc
        # MOS-DATA-024: quarantine is invisible to everything without `phi.admin`.
        if quarantined and not principal.has("phi.admin"):
            owned = False
        # MOS-DATA-018: a token scoped to named studies learns nothing about the rest.
        if owned and not principal.may_see_study(study_instance_uid):
            owned = False
        if not owned:
            raise refuse(
                404,
                "STUDY_NOT_FOUND",
                "No such study",
                "No study with that identifier is available to this principal.",
                problem_class="client_error",
                reason_code="study_not_in_tenant",
                audit_uid=study_instance_uid,
            )

    try:
        backend = app.state.resolver.resolve(principal.tenant_id)
    except UnknownBackend as exc:
        raise refuse(
            503,
            "BACKEND_UNRESOLVED",
            "No PACS backend for this tenant",
            "MOS-DATA-025: the Gateway fails closed.",
            problem_class="transport_failure",
            reason_code="backend_unresolved",
            headers={"Retry-After": "30"},
        ) from exc

    return _Ctx(
        extra={"public_base": app.state.config.public_base},
        tenant_token=tenant_token,
        principal=principal,
        consumer_class=consumer_class,
        backend=backend,
        trace_id=trace_id,
        request_id=request_id,
        source_ip=source_ip,
        user_agent=user_agent,
        instance=instance,
        operation=operation,
        conn=conn,
    )


# =====================================================================================
# QIDO-RS and metadata: buffered, filtered, returned as application/dicom+json
# =====================================================================================
def _qido(
    request: Request,
    path_tenant: str,
    operation: str,
    upstream_path: str,
    *,
    study_instance_uid: str | None = None,
    series_instance_uid: str | None = None,
    filter_studies: bool = False,
) -> Response:
    """A JSON DICOMweb response, buffered so it can be filtered. MOS-DATA-011.

    Buffering is correct here and forbidden one function down: MOS-DATA-023's no-buffer
    rule is about a SERIES of instances, and a QIDO result set or a metadata document is
    attribute data whose size is bounded by the instance count, not by the pixel data.

    `rewrite_bulkdata: bool` USED TO BE A PARAMETER HERE and is gone rather than defaulted
    to True. It selected which two of the five routes through this function had their
    backend URLs scrubbed, and a switch that turns a PHI/topology control off per route is
    a switch the next route forgets to set -- which is exactly how the STOW-RS leak got
    in. The scrub is now unconditional; see `_scrub_backend_origin`.
    """
    try:
        ctx = _authorise(
            request, path_tenant, operation, study_instance_uid=study_instance_uid
        )
    except _Refused as refused:
        return refused.response

    metrics = request.app.state.metrics
    access = gwaudit.AccessRecord(
        tenant_id=ctx.principal.tenant_id,
        principal=ctx.principal,
        operation=operation,
        consumer_class=ctx.consumer_class,
        trace_id=ctx.trace_id,
        request_id=ctx.request_id,
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        job_id=ctx.principal.job_id,
        source_ip=ctx.source_ip,
        user_agent=ctx.user_agent,
    )
    try:
        upstream = _forward(
            request, ctx, upstream_path, accept=DICOM_JSON, stream=False
        )
        if upstream.status_code == 204 or not upstream.content:
            rows: list[dict[str, Any]] = []
        else:
            try:
                rows = upstream.json()
            except ValueError:
                # Not JSON: the backend answered something this route cannot filter, and
                # MOS-DATA-011 forbids returning an unfiltered body. Fail closed.
                metrics.request(
                    operation=operation, consumer_class=ctx.consumer_class, status=502
                )
                return problem_response(
                    build_problem(
                        status=502,
                        code="BACKEND_MALFORMED_RESPONSE",
                        title="The PACS returned a body this route cannot filter",
                        detail="A QIDO-RS response that is not application/dicom+json "
                               "cannot have the tenancy predicate applied to it, and "
                               "MOS-DATA-011 forbids returning it unfiltered.",
                        problem_class="transport_failure",
                        instance=ctx.instance,
                        trace_id=ctx.trace_id,
                    )
                )
        if upstream.status_code >= 400:
            metrics.request(
                operation=operation,
                consumer_class=ctx.consumer_class,
                status=upstream.status_code,
            )
            return problem_response(
                build_problem(
                    status=502 if upstream.status_code >= 500 else 404,
                    code="BACKEND_REFUSED",
                    title="The PACS did not answer the query",
                    detail=f"The backend answered {upstream.status_code}.",
                    problem_class="transport_failure"
                    if upstream.status_code >= 500
                    else "client_error",
                    instance=ctx.instance,
                    trace_id=ctx.trace_id,
                )
            )

        if filter_studies:
            # MOS-DATA-011: intersection, applied AFTER the backend responded.
            candidates = [_tag_value(r, TAG_STUDY_UID) for r in rows]
            owned = owned_subset(ctx.conn, [c for c in candidates if c])
            if not ctx.principal.has("phi.admin"):
                owned -= quarantined_study_uids(ctx.conn)   # MOS-DATA-024
            if ctx.principal.study_scope:
                owned &= set(ctx.principal.study_scope)     # MOS-DATA-018
            before = len(rows)
            rows = [
                r
                for r, uid in zip(rows, candidates, strict=True)
                if uid and uid in owned
            ]
            access.extra["filtered_out"] = before - len(rows)

        # Unconditional, and `rewrite_bulkdata` no longer gates it. The flag used to mean
        # "this route returns a metadata document, which is the one that carries
        # BulkDataURI"; but a QIDO-RS answer carries `(0008,1190) RetrieveURL` on every
        # matched study, so the two routes the flag excluded were publishing the origin
        # too. Scrubbing a document that contains no backend URL costs one failed
        # `str.replace` and cannot be forgotten by a future route.
        payload = _scrub_backend_origin(
            json.dumps(rows, separators=(",", ":")), ctx, path_tenant
        ).encode("utf-8")

        access.instance_count = len(rows)
        access.bytes_out = len(payload)
        access.patient_key = _patient_key_for(ctx, study_instance_uid)
        _write_access(request.app, access)
        metrics.request(
            operation=operation, consumer_class=ctx.consumer_class, status=200
        )
        if not rows:
            # QIDO-RS answers an empty result set with 204 and no body (PS3.18). A 200
            # with `[]` is what makes OHIF render an empty study list as an error.
            return Response(status_code=204)
        return Response(payload, media_type=DICOM_JSON, status_code=200)
    except ProjectionUnavailable:
        metrics.request(operation=operation, consumer_class=ctx.consumer_class, status=503)
        return problem_response(
            build_problem(
                status=503,
                code="PROJECTION_UNAVAILABLE",
                title="The tenancy projection is unreachable",
                detail="MOS-DATA-025: the Gateway fails closed.",
                problem_class="transport_failure",
                instance=ctx.instance,
                trace_id=ctx.trace_id,
            ),
            headers={"Retry-After": "5"},
        )
    except requests.RequestException as exc:
        metrics.request(operation=operation, consumer_class=ctx.consumer_class, status=502)
        log.warning("gateway_backend_error", extra={"error": type(exc).__name__})
        return problem_response(
            build_problem(
                status=502,
                code="BACKEND_UNREACHABLE",
                title="The PACS did not answer",
                detail=f"The backend transport failed ({type(exc).__name__}).",
                problem_class="transport_failure",
                instance=ctx.instance,
                trace_id=ctx.trace_id,
            ),
            headers={"Retry-After": "5"},
        )
    finally:
        ctx.release()


# =====================================================================================
# WADO-RS retrieve: streamed. MOS-DATA-023.
# =====================================================================================
def _wado(
    request: Request,
    path_tenant: str,
    operation: str,
    upstream_path: str,
    *,
    study_instance_uid: str,
    series_instance_uid: str | None = None,
    sop_instance_uid: str | None = None,
    needs_rendered: bool = False,
) -> Response:
    """Stream a PHI-bearing retrieval and record one audit row when the body ends.

    MOS-DATA-023: "WADO-RS retrieval MUST stream: the Gateway MUST NOT buffer a whole
    series in memory." The generator below holds one socket chunk (256 KiB) and a boundary
    carry of a few dozen bytes, never the 237-instance body.
    """
    try:
        ctx = _authorise(
            request, path_tenant, operation, study_instance_uid=study_instance_uid
        )
    except _Refused as refused:
        return refused.response

    app = request.app
    cfg: GatewayConfig = app.state.config
    metrics = app.state.metrics
    inflight: _InFlight = app.state.inflight

    if needs_rendered and not ctx.backend.supports_rendered:
        ctx.release()
        metrics.request(operation=operation, consumer_class=ctx.consumer_class, status=501)
        return problem_response(
            build_problem(
                status=501,
                code="RENDERED_NOT_SUPPORTED",
                title="This backend does not render",
                detail="The resolved PacsBackend declares SupportsRendered() = false "
                       "(MOS-DATA-014).",
                problem_class="client_error",
                instance=ctx.instance,
                trace_id=ctx.trace_id,
            )
        )

    # MOS-DATA-023's per-principal concurrency limit.
    slot = ctx.principal.principal_id
    if not inflight.acquire(slot):
        ctx.release()
        metrics.request(operation=operation, consumer_class=ctx.consumer_class, status=429)
        metrics.denial(reason="concurrency_limit")
        return problem_response(
            build_problem(
                status=429,
                code="RETRIEVAL_CONCURRENCY_LIMIT",
                title="Too many concurrent retrievals",
                detail=f"At most {inflight.limit} retrievals may be in flight per "
                       "principal (MOS-DATA-023).",
                problem_class="rate_limit",
                instance=ctx.instance,
                trace_id=ctx.trace_id,
            ),
            headers={"Retry-After": str(cfg.retry_after_seconds)},
        )

    access = gwaudit.AccessRecord(
        tenant_id=ctx.principal.tenant_id,
        principal=ctx.principal,
        operation=operation,
        consumer_class=ctx.consumer_class,
        trace_id=ctx.trace_id,
        request_id=ctx.request_id,
        study_instance_uid=study_instance_uid,
        series_instance_uid=series_instance_uid,
        sop_instance_uid=sop_instance_uid,
        job_id=ctx.principal.job_id,
        source_ip=ctx.source_ip,
        user_agent=ctx.user_agent,
    )
    access.patient_key = _patient_key_for(ctx, study_instance_uid)

    try:
        upstream = _forward(
            request,
            ctx,
            upstream_path,
            accept=request.headers.get("accept") or MULTIPART_DICOM,
            stream=True,
        )
    except requests.RequestException as exc:
        inflight.release(slot)
        ctx.release()
        metrics.request(operation=operation, consumer_class=ctx.consumer_class, status=502)
        return problem_response(
            build_problem(
                status=502,
                code="BACKEND_UNREACHABLE",
                title="The PACS did not answer",
                detail=f"The backend transport failed ({type(exc).__name__}).",
                problem_class="transport_failure",
                instance=ctx.instance,
                trace_id=ctx.trace_id,
            ),
            headers={"Retry-After": "5"},
        )

    if upstream.status_code >= 400:
        status = upstream.status_code
        upstream.close()
        inflight.release(slot)
        ctx.release()
        metrics.request(operation=operation, consumer_class=ctx.consumer_class, status=status)
        return problem_response(
            build_problem(
                status=404 if status == 404 else 502,
                code="BACKEND_REFUSED" if status != 404 else "STUDY_NOT_FOUND",
                title="The PACS did not return the object",
                detail=f"The backend answered {status}.",
                problem_class="client_error" if status == 404 else "transport_failure",
                instance=ctx.instance,
                trace_id=ctx.trace_id,
            )
        )

    content_type = upstream.headers.get("content-type", "application/octet-stream")
    boundary = _boundary_of(content_type)
    # The stream outlives the handler, so the request connection is closed here and the
    # audit write opens its own. The tenant binding goes with it: the generator runs on a
    # different task, which is why `medos.gateway.audit` re-binds explicitly.
    ctx.release()

    def body() -> Iterator[bytes]:
        counter = _PartCounter(boundary)
        total = 0
        outcome = "allow"
        reason: str | None = None
        try:
            for chunk in upstream.iter_content(chunk_size=cfg.chunk_bytes):
                if not chunk:
                    continue
                total += len(chunk)
                counter.feed(chunk)
                if counter.count > cfg.max_instances_per_response:
                    # MOS-DATA-023's per-response instance cap. The status line is long
                    # gone by the time this is known, so the stream is ABORTED and the
                    # audit row says why. STATED DEVIATION: the requirement asks for 429,
                    # which is only expressible before the first byte. A client sees a
                    # truncated body; the deployment sees `instance_cap_exceeded`.
                    outcome = "error"
                    reason = "instance_cap_exceeded"
                    break
                yield chunk
        except Exception:  # noqa: BLE001 - a client disconnect must still audit
            outcome = "error"
            reason = "stream_interrupted"
            raise
        finally:
            upstream.close()
            inflight.release(slot)
            access.instance_count = counter.count or (1 if total else 0)
            access.bytes_out = total
            access.outcome = outcome
            access.reason_code = reason
            _write_access(app, access)
            metrics.request(
                operation=operation,
                consumer_class=ctx.consumer_class,
                status=200 if outcome == "allow" else 500,
            )

    # The retrieval BODY is DICOM bytes and is streamed untouched (MOS-DATA-023). Its
    # HEADERS are not: a DICOMweb server may answer a retrieval with `Content-Location`
    # or a `Link` naming itself, and this dict was previously forwarded verbatim, which
    # made the streaming routes a second publisher of the PACS origin (MOS-SEC-088).
    headers = _safe_response_headers(upstream, ctx, path_tenant)
    return StreamingResponse(body(), media_type=content_type, headers=headers)


# =====================================================================================
# STOW-RS. MOS-DATA-012, MOS-DATA-020.
# =====================================================================================
def _stow(
    request: Request,
    path_tenant: str,
    path_study_uid: str | None,
    payload: bytes,
) -> Response:
    """Store instances, after resolving every StudyInstanceUID in the payload.

    MOS-DATA-012 in full: "On STOW-RS the Gateway MUST resolve every `StudyInstanceUID` in
    the payload against the `studies` projection. If a study is owned by a different
    tenant, the whole request MUST be rejected with `409` and none of its instances
    stored. If a study is unknown, the Gateway MUST create the `studies` row for the
    calling principal's tenant in the same transaction that records the STOW attempt."

    THE BODY IS BUFFERED, DELIBERATELY. MOS-DATA-023's no-buffer rule is about retrieval;
    a store cannot be admitted without reading the UIDs it carries, and forwarding first
    and checking afterwards would store another tenant's instance and then apologise.
    `max_stow_bytes` bounds the exposure; `medos.dicomweb.client.stow_files` already
    batches at 20 instances (~5 MB) per request, so the bound is never approached by the
    platform's own writer.
    """
    try:
        ctx = _authorise(
            request,
            path_tenant,
            "STOW-RS.study",
            study_instance_uid=None,   # ownership is resolved below, not here
            write=True,
        )
    except _Refused as refused:
        return refused.response

    app = request.app
    metrics = app.state.metrics

    def problem(status: int, code: str, title: str, detail: str, klass: str) -> Response:
        metrics.request(
            operation="STOW-RS.study", consumer_class=ctx.consumer_class, status=status
        )
        return problem_response(
            build_problem(
                status=status,
                code=code,
                title=title,
                detail=detail,
                problem_class=klass,
                instance=ctx.instance,
                trace_id=ctx.trace_id,
            )
        )

    try:
        content_type = request.headers.get("content-type", "")
        uids = _study_uids_in_payload(payload, content_type)
        if path_study_uid:
            uids.add(path_study_uid)
        if not uids:
            return problem(
                400, "STOW_NO_STUDY_UID", "No StudyInstanceUID in the payload",
                "MOS-DATA-012 requires every StudyInstanceUID in a STOW-RS payload to be "
                "resolved against the studies projection; none could be read.",
                "client_error",
            )

        # MOS-DATA-012 PASS ONE: resolve EVERY StudyInstanceUID before admitting ANY.
        # "the whole request MUST be rejected with 409 and none of its instances stored"
        # -- a loop that admits as it goes leaves a `studies` row behind for the UIDs that
        # sorted before the offending one, which is a partial effect of a request the
        # requirement says has none.
        foreign = [
            uid
            for uid in sorted(uids)
            if (owner := study_owner(ctx.conn, uid)) is not None
            and str(owner) != str(ctx.principal.tenant_id)
        ]
        if foreign:
            metrics.denial(reason="study_owned_by_other_tenant")
            gwaudit.record_denial(
                ctx.conn,
                tenant_id=ctx.principal.tenant_id,
                principal=ctx.principal,
                operation="STOW-RS.study",
                consumer_class=ctx.consumer_class,
                trace_id=ctx.trace_id,
                request_id=ctx.request_id,
                reason_code="study_owned_by_other_tenant",
                study_instance_uid=foreign[0],
                source_ip=ctx.source_ip,
                user_agent=ctx.user_agent,
            )
            return problem(
                409, "STUDY_OWNED_BY_ANOTHER_TENANT",
                "This study belongs to a different tenant",
                "MOS-DATA-012: the whole request is rejected and none of its "
                "instances are stored.",
                "client_error",
            )

        # PASS TWO, and MOS-DATA-012's other half: "the Gateway MUST create the `studies`
        # row for the calling principal's tenant IN THE SAME TRANSACTION that records the
        # STOW attempt."
        with ctx.conn.transaction():
            for uid in sorted(uids):
                admit_study(
                    ctx.conn,
                    study_instance_uid=uid,
                    tenant_id=ctx.principal.tenant_id,
                    pacs_backend=ctx.backend.backend_id,
                )
            access = gwaudit.AccessRecord(
                tenant_id=ctx.principal.tenant_id,
                principal=ctx.principal,
                operation="STOW-RS.study",
                consumer_class=ctx.consumer_class,
                trace_id=ctx.trace_id,
                request_id=ctx.request_id,
                study_instance_uid=sorted(uids)[0],
                job_id=ctx.principal.job_id,
                source_ip=ctx.source_ip,
                user_agent=ctx.user_agent,
                bytes_out=len(payload),
            )
            access.extra["studies_in_payload"] = len(uids)
            gwaudit.record_phi_access(ctx.conn, access)

        upstream = _forward(
            request,
            ctx,
            f"/studies/{path_study_uid}" if path_study_uid else "/studies",
            accept=DICOM_JSON,
            stream=False,
            method="POST",
            data=payload,
            content_type=content_type,
        )
        metrics.request(
            operation="STOW-RS.study",
            consumer_class=ctx.consumer_class,
            status=upstream.status_code,
        )
        # THE BODY IS REWRITTEN, NOT FORWARDED. This is the STOW-RS half of MOS-SEC-088.
        # A DICOMweb store response is a dataset, and PS3.18 puts a URL in three places
        # in it: `(0008,1190) RetrieveURL` at the top level, one per item of
        # `(0008,1199) ReferencedSOPSequence`, and one per item of
        # `(0008,1198) FailedSOPSequence`. Orthanc builds all of them from its own base,
        # so the reply to every successful store used to read
        # `http://orthanc:8042/dicom-web/studies/...` -- the address MOS-DATA-006 has
        # just made unroutable from the caller, handed to the caller.
        #
        # `_scrub_backend_origin` is a substring replacement over the serialised body,
        # which is why it covers all three attributes and any fourth a backend invents,
        # and why it does not need the body to be JSON: a `application/dicom+xml` reply
        # is scrubbed by the same call. A non-UTF-8 body is left alone and reported
        # below rather than mangled.
        body = upstream.content
        try:
            scrubbed = _scrub_backend_origin(
                body.decode("utf-8"), ctx, path_tenant
            ).encode("utf-8")
        except UnicodeDecodeError:
            # STATED DEVIATION: a STOW-RS reply that is not text cannot be rewritten
            # here. MOS-DATA-007 lists no such response and Orthanc sends none; if one
            # ever arrives it is refused rather than forwarded unscrubbed, because
            # MOS-DATA-025's posture is that the Gateway fails closed rather than passes
            # something through it could not inspect.
            log.warning(
                "gateway_stow_response_not_text",
                extra={"operation": "STOW-RS.study"},
            )
            return problem(
                502, "BACKEND_MALFORMED_RESPONSE",
                "The PACS returned a store response this route cannot scrub",
                "A STOW-RS response body that is not UTF-8 text cannot have the backend "
                "origin removed from it, and MOS-SEC-088 forbids returning it as it is.",
                "transport_failure",
            )
        return Response(
            scrubbed,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", DICOM_JSON),
            headers=_safe_response_headers(upstream, ctx, path_tenant),
        )
    except ProjectionUnavailable:
        return problem(
            503, "PROJECTION_UNAVAILABLE", "The tenancy projection is unreachable",
            "MOS-DATA-025: the Gateway fails closed.", "transport_failure",
        )
    except requests.RequestException as exc:
        log.warning("gateway_stow_backend_error", extra={"error": type(exc).__name__})
        return problem(
            502, "BACKEND_UNREACHABLE", "The PACS did not answer",
            f"The backend transport failed ({type(exc).__name__}).", "transport_failure",
        )
    finally:
        ctx.release()


# =====================================================================================
# Plumbing
# =====================================================================================
def _forward(
    request: Request,
    ctx: _Ctx,
    upstream_path: str,
    *,
    accept: str,
    stream: bool,
    method: str = "GET",
    data: bytes | None = None,
    content_type: str | None = None,
) -> requests.Response:
    """One outbound call to the backend, carrying the ONLY PACS credential. MOS-DATA-002.

    The caller's `Authorization` header is stripped and replaced, never forwarded: a
    Gateway that passes the caller's token through is a Gateway whose tenancy filter can
    be bypassed by talking to the PACS directly with the same token.
    """
    backend = ctx.backend
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _STRIP_FROM_REQUEST
    }
    headers["Accept"] = accept
    if content_type:
        headers["Content-Type"] = content_type
    cred = backend.credential
    auth = None
    if cred.bearer_token:
        headers["Authorization"] = f"Bearer {cred.bearer_token}"
    elif cred.user:
        auth = (cred.user, cred.password or "")

    url = f"{backend.base_url}{upstream_path}"
    session: requests.Session = request.app.state.http
    started = time.monotonic()
    try:
        return session.request(
            method,
            url,
            params=dict(request.query_params) if method == "GET" else None,
            headers=headers,
            data=data,
            auth=auth,
            stream=stream,
            timeout=backend.timeout_s,
        )
    finally:
        log.info(
            "gateway_upstream",
            extra={
                "operation": ctx.operation,
                "backend_id": backend.backend_id,
                "ms": int((time.monotonic() - started) * 1000),
            },
        )


def _study_uids_in_payload(payload: bytes, content_type: str) -> set[str]:
    """Every StudyInstanceUID in a STOW-RS body. MOS-DATA-012's "every".

    The UIDs are read from the DATASETS, not from a `Content-Location` header or the URL:
    the header is the sender's claim and the dataset is the object, and an admission check
    that trusts the claim admits an instance whose actual study belongs elsewhere.

    A part that cannot be parsed contributes no UID and is NOT silently allowed through --
    the caller gets `STOW_NO_STUDY_UID` if that leaves the set empty, and if other parts
    did parse, the unparsed one still reaches the backend inside a body whose other
    studies were admitted. REPORTED: strictly, MOS-DATA-012 wants an unreadable part to
    reject the whole request; doing that needs the Gateway to re-serialise the multipart
    body, which is a change to the forwarding path rather than to this function.
    """
    import io

    import pydicom

    from medos.dicomweb.client import parse_multipart_related

    uids: set[str] = set()
    try:
        parts = parse_multipart_related(payload, content_type)
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return uids
    for part in parts:
        try:
            ds = pydicom.dcmread(
                io.BytesIO(part.content), stop_before_pixels=True, force=False
            )
        except Exception:  # noqa: BLE001
            continue
        uid = str(getattr(ds, "StudyInstanceUID", "") or "")
        if uid:
            uids.add(uid)
    return uids


class _PartCounter:
    """Counts multipart parts in a stream without assembling them. MOS-DATA-023.

    Holds `len(delimiter) - 1` bytes of carry so a boundary split across two socket chunks
    is still counted, and nothing else. This is what lets the instance cap be enforced on
    a 237-instance retrieval whose body never exists in memory.
    """

    def __init__(self, boundary: bytes | None) -> None:
        self._delim = b"--" + boundary if boundary else None
        self._carry = b""
        self._delimiters = 0

    def feed(self, chunk: bytes) -> None:
        if self._delim is None:
            return
        data = self._carry + chunk
        self._delimiters += data.count(self._delim)
        keep = len(self._delim) - 1
        self._carry = data[-keep:] if keep > 0 else b""

    @property
    def count(self) -> int:
        """Parts seen so far.

        `--<boundary>` occurs once per part AND once more in the closing
        `--<boundary>--`, so the raw delimiter count is one too many on a complete body.
        Subtracting it here rather than at the call sites keeps the instance cap and the
        audit row counting the same thing. On a body still in flight the value is
        therefore one low for a moment, which is the right direction for a cap.
        """
        return max(0, self._delimiters - 1)


def _boundary_of(content_type: str) -> bytes | None:
    for token in content_type.split(";"):
        key, _, value = token.strip().partition("=")
        if key.strip().lower() == "boundary":
            return value.strip().strip('"').encode("ascii", "ignore")
    return None


def _client_ip(request: Request) -> str | None:
    """The peer address, or None when there is not a real one.

    MOS-SEC-146 makes `source_ip` nullable and Chapter 12 says why: "a fabricated address
    is indistinguishable from a real connection, which is exactly the confusion an audit
    trail exists to prevent". An ASGI transport that reports a name rather than an address
    -- Starlette's own `TestClient` reports the literal string `testclient` -- is exactly
    that case, so it becomes NULL rather than a `0.0.0.0` stand-in.
    """
    import ipaddress

    host = request.client.host if request.client else None
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return host


def _tag_value(row: dict[str, Any], tag: str) -> str:
    value = row.get(tag, {}).get("Value") if isinstance(row.get(tag), dict) else None
    if isinstance(value, list) and value:
        return str(value[0])
    return ""


def _backend_origins(base_url: str) -> tuple[str, ...]:
    """Every spelling of the backend that a body or a header could carry, longest first.

    `MEDOS_GATEWAY_PACS_URL` is a DICOMweb ROOT (`http://orthanc:8042/dicom-web`), but a
    PACS does not restrict itself to that prefix when it writes a URL back: Orthanc's
    STOW-RS response builds `(0008,1190) RetrieveURL` from its own configured base, and
    its native API lives at the bare origin. Replacing only the configured root therefore
    leaves `http://orthanc:8042/...` standing on any route that does not use it, which is
    the same disclosure one path segment to the left.

    Longest first so the root wins over the bare origin and the replacement is not applied
    twice to the same URL.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    candidates = {base_url.rstrip("/")} if base_url else set()
    if parts.scheme and parts.netloc:
        candidates.add(f"{parts.scheme}://{parts.netloc}")
    return tuple(sorted((c for c in candidates if c), key=len, reverse=True))


def _gateway_uri_base(ctx: _Ctx, path_tenant: str) -> str:
    """What a backend URL is rewritten TO: this Gateway's DICOMweb root for this tenant.

    FAILS CLOSED WHEN `MEDOS_GATEWAY_PUBLIC_BASE` IS UNSET. The previous behaviour was to
    return the body untouched, which made the PACS origin's confidentiality depend on an
    optional environment variable -- an operator who never set it published
    `http://orthanc:8042/...` to every viewer on every metadata response, and nothing said
    so. With no configured base the replacement is the RELATIVE reference
    `/dicomweb/{t}`, which RFC 3986 section 4.2 resolves against the document's own
    retrieval URI: the caller is already talking to the Gateway, so the link still works
    and the origin is gone. A redaction that keeps the link usable is strictly better than
    a disclosure that does not.
    """
    base = str(ctx.extra.get("public_base") or "")
    return f"{base}/dicomweb/{path_tenant}" if base else f"/dicomweb/{path_tenant}"


def _scrub_backend_origin(body: str, ctx: _Ctx, path_tenant: str) -> str:
    """Point every URL the backend wrote at the Gateway, not at the PACS.

    MOS-SEC-088: "OHIF, every worker, every service and every operator tool MUST address
    the DICOM Gateway, never the PACS. The PACS network address MUST NOT be resolvable or
    routable from `Z-EDGE`, `Z-SERVICE` or `Z-PLATFORM`." A response body that names
    `http://orthanc:8042` hands that address to a `Z-EDGE` caller in the one channel the
    NetworkPolicy of MOS-DATA-006 cannot filter, and MOS-SEC-091 adds that the deployment
    shape behind the Gateway "MUST NOT be visible to any caller". MOS-DATA-002 is the
    layer underneath: the Gateway is the only component that may hold a PACS address at
    all, so publishing one is the credential boundary leaking its other half.

    It is also, and separately, broken: MOS-DATA-006 has just made that URL unroutable
    from everything that could read it, so an un-scrubbed body is a dead link.

    THIS APPLIES TO EVERY ATTRIBUTE, NOT TO `BulkDataURI`. The earlier version of this
    function was named for the one attribute the metadata routes needed, and the STOW-RS
    route's `(0008,1190) RetrieveURL` -- and `(0008,1199) ReferencedSOPSequence`'s
    per-instance copy of it, and `(0008,1198) FailedSOPSequence`'s -- went out verbatim.
    A string replacement over the whole document rather than a walk of the JSON tree is
    what makes the coverage attribute-agnostic: a URL attribute can appear at arbitrary
    sequence depth (the same problem MOS-DATA-034 describes for VR `UI`), and the
    substring replaced is a full scheme-plus-origin, which cannot occur inside an
    attribute value of a document produced by the backend it names.
    """
    replacement = _gateway_uri_base(ctx, path_tenant)
    for origin in _backend_origins(ctx.backend.base_url):
        body = body.replace(origin, replacement)
    return body


def _safe_response_headers(
    upstream: requests.Response, ctx: _Ctx, path_tenant: str
) -> dict[str, str]:
    """Hop-by-hop dropped, product identity dropped, backend origin scrubbed.

    A header is the second channel the origin travels in and it was never filtered:
    Orthanc answers a STOW-RS with a `Location`, and a DICOMweb server may send
    `Content-Location` or a `Link`. Scrubbing every remaining value rather than a named
    list of headers is deliberate -- the named list is a denylist, and MOS-SEC-105's
    prohibition is "in any form".

    `server` and `x-powered-by` are DROPPED rather than scrubbed. MOS-SEC-091 says the
    PACS deployment choice "MUST NOT be visible to any caller"; `Server: Orthanc/1.12.4`
    names the product, which is a smaller disclosure than the address and the same kind.
    STATED as a judgement call: no requirement spells out the `Server` header.
    """
    out: dict[str, str] = {}
    for key, value in upstream.headers.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP or lowered in _STRIP_FROM_RESPONSE:
            continue
        out[key] = _scrub_backend_origin(value, ctx, path_tenant)
    return out


def _patient_key_for(ctx: _Ctx, study_instance_uid: str | None) -> str | None:
    """MOS-DATA-022's surrogate, resolved from the projection and never from the PACS."""
    if not study_instance_uid:
        return None
    try:
        row = study_row(ctx.conn, study_instance_uid)
    except ProjectionUnavailable:
        return None
    return row.patient_key if row else None


def _write_access(app: Any, access: gwaudit.AccessRecord) -> None:
    """Write the MOS-DATA-022 row on its own connection.

    Its own connection because this runs from a streaming generator's `finally`, after the
    request-scoped connection has been closed and possibly on a different thread. A failed
    audit write is logged and swallowed: the bytes have already left, and raising here
    would turn a completed retrieval into a 500 while leaving the trail just as empty.
    REPORTED as a known weakness -- MOS-SEC-149's "a committed state change with no audit
    row MUST NOT be possible" is satisfiable for the STOW path (one transaction) and is
    NOT satisfiable for a retrieval, where the state change is a TCP stream.
    """
    conn = None
    try:
        conn = app.state.medos_connect()
        gwaudit.record_phi_access(conn, access)
    except Exception:  # noqa: BLE001
        log.exception(
            "gateway_phi_access_audit_failed",
            extra={"operation": access.operation, "trace_id": access.trace_id},
        )
    finally:
        if conn is not None:
            conn.close()
