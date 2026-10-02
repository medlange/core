# SPDX-License-Identifier: Apache-2.0
"""RFC 9457 `application/problem+json` rendering. CONTRACT.md section 1: "problems.py --
RFC 9457 responses".

CONTRACT.md section 9 fixes the requirement:

    Errors: RFC 9457 `application/problem+json` with a `class` field whose enum separates
    `clinical_rejection` from `transport_failure` and `system_failure`.

Chapter 10 owns the wire detail (`MOS-API-035` .. `MOS-API-042`) and this module is the
one place it is implemented. Nothing else in `medos.api` builds a problem document by
hand, and no handler returns a bare `{"error": ...}` body -- `MOS-API-035` forbids both a
string body and a `200` carrying an error.

The distinction this module exists to keep
------------------------------------------
`MOS-API-039`: a `clinical_rejection` MUST NOT be rendered with the same affordance as a
`client_error`, a `transport_failure` or a `system_failure`. "This study was not
analysed, and here is the clinical reason" and "the platform broke" are the same red X
today and one of them is information a radiologist must act on.

The structural half of that rule is `MOS-API-047`, and it is the defect chapter 10 calls
out by name: **`POST /api/v1/jobs` MUST NOT return a `clinical_rejection` synchronously.**
A study with no eligible series produces a persisted `REJECTED` job -- an HTTP `202`, then
a `200` from `GET /api/v1/jobs/{id}` carrying a `rejection` member. A clinical non-answer
is evidence and must be a queryable, auditable row, not a status code a client can drop on
the floor. `normalize_problem()` below is the only path by which a stored rejection reaches
a client, and `routes_jobs.get_job` -- its one caller for that purpose -- answers `200`.

PHI
---
`MOS-API-006` / `MOS-API-042` / CONTRACT.md section 11. A problem document carries UIDs,
counts and millimetres. It MUST NOT carry a PatientName, PatientID, AccessionNumber,
StudyDescription, a stack trace, SQL text, an internal hostname, a file path, or any part
of the request body. `unhandled_problem()` is the enforcement point for the last four:
it discards the exception entirely and hands back only `trace_id`.

Spec: MOS-API-002, MOS-API-004, MOS-API-006, MOS-API-035, MOS-API-036, MOS-API-037,
MOS-API-038, MOS-API-039, MOS-API-040, MOS-API-041, MOS-API-042, MOS-API-047,
MOS-EXEC-016, MOS-EXEC-016a, CONTRACT.md sections 9 and 11.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from medos.core.errors import MedosError
from medos.security.logsafe import route_fields

__all__ = [
    "PROBLEM_BASE",
    "PROBLEM_MEDIA_TYPE",
    "TRACE_HEADER",
    "WIRE_CLASSES",
    "RETRYABLE_CLASSES",
    "ProblemResponse",
    "rfc3339",
    "json_safe",
    "problem_type_uri",
    "build_problem",
    "normalize_problem",
    "problem_from_medos_error",
    "unhandled_problem",
    "class_for_status",
    "problem_response",
    "install_exception_handlers",
    "now_rfc3339",
    "trace_id_of",
]

log = logging.getLogger("medos.api")

# MOS-API-036: the ONLY permitted `type` prefix. A `type` under any other authority is a
# defect and CI greps for it. The URI is an identifier, never dereferenced at runtime.
PROBLEM_BASE = "https://spec.medicalos.org/problems/"

# MOS-API-002 / RFC 9457 section 3.
PROBLEM_MEDIA_TYPE = "application/problem+json"

# MOS-API-004: returned on EVERY response, including every error response, and repeated
# as the `trace_id` member of the document itself.
TRACE_HEADER = "MedicalOS-Trace-Id"

# Table 10.4-A, verbatim and closed (MOS-API-038). CONTRACT.md section 9 names three of
# these six and requires them to be separated; the other three are chapter 10's and are
# needed because this surface has to answer a malformed body and a missing job. Only
# `clinical_rejection`, `client_error`, `transport_failure` and `system_failure` are
# reachable in this slice -- there is no auth and no rate limiter (CONTRACT.md section 0)
# -- but the enum is spelled in full so that validation is against the real closed set.
WIRE_CLASSES: frozenset[str] = frozenset(
    {
        "clinical_rejection",
        "client_error",
        "authz_error",
        "rate_limit",
        "transport_failure",
        "system_failure",
    }
)

# Table 10.4-A's `retryable` column. Not a per-instance judgement: the class decides.
RETRYABLE_CLASSES: frozenset[str] = frozenset({"rate_limit", "transport_failure"})


class ProblemResponse(JSONResponse):
    """A `JSONResponse` that declares `application/problem+json` (MOS-API-035)."""

    media_type = PROBLEM_MEDIA_TYPE


# =====================================================================================
# Serialisation primitives shared with the job/event shaping in routes_*.py
# =====================================================================================
def rfc3339(value: Any) -> Any:
    """RFC 3339, millisecond precision, UTC, literal `Z` (MOS-API-002).

    Not `datetime.isoformat()`: that emits `+00:00` and microseconds, and chapter 10
    fixes the shape so that two services do not disagree about what a timestamp looks
    like. A naive datetime is read as UTC -- every timestamp in `schema.sql` is
    `timestamptz`, so a naive one can only come from a caller that dropped the tzinfo.
    """
    if not isinstance(value, datetime):
        return value
    moment = value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def json_safe(value: Any) -> Any:
    """Make a `psycopg` row tree JSON-encodable without losing precision silently.

    `Decimal` -> `float` is the one lossy conversion and it is deliberate: chapter 10
    bodies are JSON and JSON has one number type. `result_measurements.value` is
    `numeric` in `schema.sql` so the DATABASE keeps full precision; what crosses the wire
    is the IEEE double a client would have parsed anyway.
    """
    if isinstance(value, datetime):
        return rfc3339(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, memoryview | bytes | bytearray):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def now_rfc3339() -> str:
    return rfc3339(datetime.now(UTC))


# =====================================================================================
# Document construction
# =====================================================================================
def problem_type_uri(code: str) -> str:
    """`SCREAMING_SNAKE_CASE` code -> the stable `type` URI (MOS-API-036).

    One mechanical mapping rather than a hand-maintained table, because a table is how
    `type` and `code` drift apart -- and `MOS-API-037` requires `code` to be stable *per*
    `type`, which is only checkable if one is a function of the other.
    """
    slug = code.strip().lower().replace("_", "-").strip("-")
    return PROBLEM_BASE + (slug or "unspecified")


def class_for_status(status: int) -> str:
    """Default `class` for a bare HTTP status (table 10.4-A).

    Used only where the raising site did not classify -- a Starlette `HTTPException` from
    routing, for instance. Every deliberate error in `medos.api` passes its class
    explicitly, because a status code is not a classification: `422` is both
    `clinical_rejection` and `client_error` in table 10.4-A and only the raiser knows
    which. Note the asymmetry that follows: this function MUST NOT return
    `clinical_rejection`, since a clinical rejection is never a synchronous error
    response (MOS-API-047).
    """
    if status in (401,):
        return "authz_error"
    if status == 403:
        return "authz_error"
    if status == 429:
        return "rate_limit"
    if status in (502, 503, 504):
        return "transport_failure"
    if status >= 500:
        return "system_failure"
    return "client_error"


def build_problem(
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    problem_class: str | None = None,
    instance: str | None = None,
    trace_id: str | None = None,
    occurred_at: str | None = None,
    retryable: bool | None = None,
    **extensions: Any,
) -> dict[str, Any]:
    """One RFC 9457 document with every member MOS-API-036/037 makes mandatory.

    Standard members: `type`, `title`, `status`, `detail`, `instance`.
    Extension members, all required on every document: `class`, `code`, `retryable`,
    `trace_id`, `occurred_at`.
    """
    cls = problem_class or class_for_status(status)
    if cls not in WIRE_CLASSES:
        # MOS-API-038: "a `class` word minted outside this table is a defect". Caught
        # here rather than shipped, because the enum is closed and a consumer branches
        # on it first.
        raise ValueError(f"{cls!r} is not a table 10.4-A class; see MOS-API-038")
    doc: dict[str, Any] = {
        "type": problem_type_uri(code),
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance or "",
        "class": cls,
        "code": code,
        "retryable": (cls in RETRYABLE_CLASSES) if retryable is None else retryable,
        "trace_id": trace_id or "",
        "occurred_at": occurred_at or now_rfc3339(),
    }
    for key, value in extensions.items():
        if value is not None:
            doc[key] = json_safe(value)
    return doc


def normalize_problem(
    doc: dict[str, Any],
    *,
    instance: str,
    trace_id: str,
    occurred_at: Any = None,
    default_class: str = "system_failure",
) -> dict[str, Any]:
    """Bring a problem document produced OUTSIDE this module onto the chapter 10 wire.

    Two producers need this and neither is an HTTP component, which is exactly why the
    normalisation lives at the HTTP boundary instead of leaking upward:

      * `medos.core.errors.MedosError.to_problem()` emits `urn:medos:problem:<code>`.
      * `medos.db.repo.job_view()` emits the same URN shape for `jobs.rejection` and
        `jobs.error`, built from the `reject_reason_code` / `failure_class` columns.

    Both are correct RFC 9457 -- section 4.2 permits a non-retrievable URI -- but
    `MOS-API-036` fixes ONE authority for `type`, so the boundary rewrites the URN to it.
    Rewriting here and not in `medos.core` keeps `medos.core` free of a URL constant that
    belongs to the API version, and keeps `medos.db` emitting a shape the worker can
    persist verbatim.

    Everything else is carried through untouched, including extension members such as
    `series_selection_href` and `reason_code`, per MOS-API-008's "clients MUST ignore
    unknown response members".
    """
    out = dict(doc)
    code = str(out.get("code") or out.get("reason_code") or "unspecified").upper()
    out["code"] = code
    out["type"] = problem_type_uri(code)
    cls = str(out.get("class") or default_class)
    if cls not in WIRE_CLASSES:
        raise ValueError(f"{cls!r} is not a table 10.4-A class; see MOS-API-038")
    out["class"] = cls
    out.setdefault("status", 422 if cls == "clinical_rejection" else 500)
    out.setdefault("title", code.replace("_", " ").title())
    # MOS-API-036 makes `detail` mandatory. The producers above put the instance-specific
    # sentence in `title`; copying it into `detail` is the honest widening -- inventing a
    # second sentence here would put words in the raising site's mouth.
    out.setdefault("detail", str(out.get("title")))
    if "retryable" not in out:
        out["retryable"] = cls in RETRYABLE_CLASSES
    out["instance"] = instance
    out["trace_id"] = trace_id
    out["occurred_at"] = rfc3339(occurred_at) if occurred_at is not None else now_rfc3339()
    return json_safe(out)


def problem_from_medos_error(
    exc: MedosError, *, instance: str, trace_id: str
) -> dict[str, Any]:
    """`MedosError` -> problem document, classification taken from the exception type.

    `MedosError.problem_class` is a CLASS attribute (see `medos.core.errors`), so the
    mapping is decided by which exception was raised and never by the handler. That is
    what keeps `ClinicalRejection` from being rendered as a crash somewhere downstream.
    """
    doc = exc.to_problem(instance=instance)
    detail_fields = doc.pop("detail_fields", None)
    out = normalize_problem(
        doc,
        instance=instance,
        trace_id=trace_id,
        default_class=exc.problem_class,
    )
    if detail_fields:
        # MOS-API-006: UIDs, counts and millimetres only. `medos.core.errors` states the
        # same rule at the raising site; this is the boundary restating it, not checking
        # it -- a PHI value put into `detail` upstream is a defect there.
        out["detail_fields"] = json_safe(detail_fields)
    return out


def unhandled_problem(*, instance: str, trace_id: str) -> dict[str, Any]:
    """The `500` for an exception nobody classified (MOS-API-042).

    The exception object is not an argument on purpose. A `system_failure` document MUST
    NOT include a stack trace, SQL text, an internal hostname, a file path or any part of
    the request body, and the cheapest way to guarantee that is to have nothing to leak:
    `trace_id` is the entire diagnostic handoff. The exception is logged server-side with
    the same trace id.
    """
    return build_problem(
        status=500,
        code="INTERNAL_ERROR",
        title="Internal error",
        detail="The request could not be completed. Quote trace_id when reporting this.",
        problem_class="system_failure",
        instance=instance,
        trace_id=trace_id,
    )


# =====================================================================================
# Responses and handlers
# =====================================================================================
def trace_id_of(request: Request) -> str:
    """The trace id the middleware put on the scope (MOS-API-004)."""
    return str(getattr(request.state, "trace_id", "") or "")


def problem_response(doc: dict[str, Any], *, headers: dict[str, str] | None = None) -> Response:
    """Serialise a problem document with its status, media type and trace header.

    The header is set here only when the document actually carries a trace id. Setting it
    unconditionally would write `MedicalOS-Trace-Id: ` on a document built without one,
    and `TraceContextMiddleware` only fills the header in when it is ABSENT -- so an empty
    string would win over the real value and breach `MOS-API-004`, which requires the
    header on every response including every error response. That is not hypothetical: it
    is what `/readyz` did before this branch existed.
    """
    hdrs: dict[str, str] = {}
    trace_id = str(doc.get("trace_id", ""))
    if trace_id:
        hdrs[TRACE_HEADER] = trace_id
    if headers:
        hdrs.update(headers)
    return ProblemResponse(status_code=int(doc["status"]), content=doc, headers=hdrs)


def _violations(errors: list[Any]) -> list[dict[str, Any]]:
    """Pydantic validation errors -> MOS-API-036's `violations[]` extension member.

    The pointer is a JSON Pointer into the REQUEST BODY, so the leading `body` element of
    pydantic's `loc` is dropped. The offending input value is NOT echoed: `MOS-API-042`
    forbids returning part of the request body, and a malformed `study_instance_uid` is
    the one field on this surface whose value could plausibly be mistaken for an
    identifier worth redacting.
    """
    out: list[dict[str, Any]] = []
    for err in errors:
        loc = [str(p) for p in err.get("loc", ()) if str(p) != "body"]
        out.append(
            {
                "pointer": "/" + "/".join(loc) if loc else "",
                "code": str(err.get("type", "invalid")).upper(),
                "detail": str(err.get("msg", "invalid value")),
            }
        )
    return out


def install_exception_handlers(app: FastAPI) -> None:
    """Register the four handlers that make MOS-API-035 true for every 4xx and 5xx.

    Registered against the base classes, so a `ClinicalRejection` raised anywhere under a
    handler is rendered by the `MedosError` branch with its own class and status rather
    than falling through to the `500`.
    """

    @app.exception_handler(MedosError)
    async def _medos_error(request: Request, exc: MedosError) -> Response:
        doc = problem_from_medos_error(
            exc, instance=request.url.path, trace_id=trace_id_of(request)
        )
        # MOS-REL-051: an error is never swallowed silently. MOS-SEC-105's `Structured
        # logs` row is why the record names the ROUTE and not `request.url.path`: a UID
        # is P2 and these handlers are installable on any surface, the Gateway's
        # UID-shaped URL space (MOS-DATA-007) included. `instance` in the document keeps
        # the path -- that goes to the caller who sent it, not to the log index.
        log.warning(
            "medos_error",
            extra={
                "trace_id": doc["trace_id"],
                "problem_class": doc["class"],
                "code": doc["code"],
                **route_fields(request),
            },
        )
        return problem_response(doc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        doc = build_problem(
            status=400,
            code="SCHEMA_VIOLATION",
            title="Request body failed schema validation",
            detail=f"{len(exc.errors())} violation(s) against the request schema.",
            problem_class="client_error",
            instance=request.url.path,
            trace_id=trace_id_of(request),
            violations=_violations(list(exc.errors())),
        )
        return problem_response(doc)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        detail = exc.detail
        if isinstance(detail, dict) and "class" in detail:
            # A route raised HTTPException(detail=<already-built problem document>).
            doc = normalize_problem(
                detail,
                instance=request.url.path,
                trace_id=trace_id_of(request),
                default_class=class_for_status(exc.status_code),
            )
        else:
            doc = build_problem(
                status=exc.status_code,
                code=_code_for_status(exc.status_code),
                title=str(detail) if detail else _title_for_status(exc.status_code),
                detail=str(detail) if detail else _title_for_status(exc.status_code),
                instance=request.url.path,
                trace_id=trace_id_of(request),
            )
        return problem_response(doc, headers=dict(exc.headers or {}))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        trace_id = trace_id_of(request)
        # The full exception goes to the server log, never to the client (MOS-API-042).
        log.exception(
            "unhandled_exception",
            extra={"trace_id": trace_id, **route_fields(request)},
        )
        return problem_response(unhandled_problem(instance=request.url.path, trace_id=trace_id))


_STATUS_CODES = {
    400: ("BAD_REQUEST", "Bad request"),
    404: ("NOT_FOUND", "Resource not found"),
    405: ("METHOD_NOT_ALLOWED", "Method not allowed"),
    409: ("CONFLICT", "Conflict"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "Unsupported media type"),
    422: ("UNPROCESSABLE_CONTENT", "Unprocessable content"),
    500: ("INTERNAL_ERROR", "Internal error"),
    503: ("SERVICE_UNAVAILABLE", "Service unavailable"),
}


def _code_for_status(status: int) -> str:
    return _STATUS_CODES.get(status, ("HTTP_ERROR", "HTTP error"))[0]


def _title_for_status(status: int) -> str:
    return _STATUS_CODES.get(status, ("HTTP_ERROR", "HTTP error"))[1]
