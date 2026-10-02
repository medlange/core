# SPDX-License-Identifier: Apache-2.0
"""What a request is allowed to put in a log record, and the shape filter behind it.

WHY THIS MODULE EXISTS
----------------------
`MOS-SEC-105` is a normative matrix and its `Structured logs` row reads:

    | Surface           | P0  | P1  | P2 | P3 | P4 | SEC |
    | Structured logs   | yes | yes | no | no | no | no  |

and `MOS-SEC-102` classifies `StudyInstanceUID`, `SeriesInstanceUID` and
`SOPInstanceUID` as **P2**. `MOS-SEC-105`'s own preamble makes the prohibition total:
"'no' means MUST NOT appear, in any form, including inside a serialized error, an
exception message, a stack trace or a URL."

The DICOM Gateway is the one service whose entire URL space is P2 by construction
(`MOS-DATA-007`'s route table: `{st}` is a `StudyInstanceUID`, `{se}` a
`SeriesInstanceUID`, `{sop}` a `SOPInstanceUID`), so any middleware that logs a resolved
request path publishes P2 on every request. That is the defect this module removes, and
chapter 3 acceptance check 14 is the check that holds it:

    14. **PHI-free telemetry.** Over a full ingest -> triage -> job run, the structured
    log stream, the OTLP span attributes and the `/metrics` scrape contain zero
    occurrences of the fixture's `PatientName`, `PatientID`, any `SeriesDescription`
    string, and any `StudyInstanceUID`. (MOS-DATA-022, 026, 065)

MINTED UIDs ARE TREATED IDENTICALLY TO SOURCE UIDs. THAT IS A DECISION, NOT AN OVERSIGHT
-----------------------------------------------------------------------------------------
A `2.25.<uuid-as-int>` UID minted by MedicalOS (`MOS-DATA-031`'s de-identified UID space,
and the derived UIDs `medos/medos/core/uids.py` writes) is *not* a hospital's identifier, and it
is tempting to log it. Four reasons it is handled exactly like a source UID here:

  1. `MOS-SEC-102` classifies by FIELD, not by provenance: "Every field the platform
     handles MUST be assigned exactly one class. The class, not the field name,
     determines where it may travel." `StudyInstanceUID` has one class, P2, whoever
     minted the value.
  2. A minted UID is a **linkable pseudonym**, not an anonymous token. `MOS-SEC-104`
     makes the de-identification UID mapping table class `SEC` and gates every read on
     `phi.reidentify` with an `AuditEvent`. A value whose whole purpose is to be
     resolvable back to a source UID through a `SEC` table is one authorised lookup from
     the identifier it stands in for; publishing it in a log index that `MOS-SEC-105`
     keeps below the PHI boundary moves the lookup key outside the boundary that protects
     the mapping.
  3. `MOS-SEC-106` names exactly one permitted substitute for a P2 identifier —
     `study_ref`, tenant-scoped and destroyable with the tenant's pepper
     (`MOS-SEC-103`). A minted UID is not that: it is global, it is permanent, and
     destroying a tenant leaves every minted UID ever logged still joinable.
  4. Operationally decisive: the discriminator would have to run on a path segment, and
     a path segment does not say who minted it. A logging control that must first decide
     "is this one ours?" is a denylist with a guess in the middle — exactly the shape
     `MOS-SEC-107` rejects ("Redaction by denylist fails the first time someone adds a
     field"). The closed allowlist below cannot express the unsafe case.

    Minted UIDs are still a PACS lookup key: `2.25.…` is what the Gateway's own URL space
    takes for `{st}`, so anyone holding one can ask the Gateway for the study.

TWO CONTROLS, AND THE SPEC IS EXPLICIT ABOUT WHICH IS WHICH
-----------------------------------------------------------
**Primary (`route_fields`).** `MOS-SEC-117` states the rule for span names — "Span names
MUST be route templates or fixed operation names. `GET /api/v1/studies/1.2.840.113619...`
MUST be recorded as `GET /api/v1/studies/{study_id}`" — and `MOS-SEC-116` binds span
attributes to the metric-label allowlist. A log record is the same surface with the same
matrix row, so a caller-supplied path never reaches a record: what reaches it is the route
TEMPLATE plus the path parameters whose NAMES are on `LOGGABLE_PATH_PARAMS`, a closed
allowlist. A new route with a new parameter is excluded by default, which is the
fail-closed half of `MOS-SEC-107`/`MOS-SEC-109` expressed without a Go type system.

`MOS-SEC-106` requires the correlation the UID used to provide to be REPLACED rather than
deleted — "A P2 identifier needed for correlation MUST be replaced by `study_ref` (P1)" —
so `route_fields` emits `study_ref` whenever the route carried a study UID. An operator
keeps a per-study join key; the log index never sees the UID.

**Secondary (`install_uid_shape_redaction`).** `MOS-SEC-110`: "A second, independent
redaction filter SHOULD run in the log shipper, matching DICOM UID shapes
(`^\\d(\\.\\d+){3,}$`), the `mos_` key prefix, and common identifier patterns, and
replacing matches with `[redacted:<class>]`. It is defence in depth and MUST NOT be the
primary control."

It runs here rather than in a shipper because this deployment has no shipper, and it is
deliberately scoped to loggers OUTSIDE `medos.` — `httpx`, `urllib3.connectionpool`,
`uvicorn.access` and every other library that renders a URL into a message. MedicalOS's
own loggers are excluded ON PURPOSE: a scrubber over our own records would make the
shape filter the primary control and would silently absorb the next defect of exactly the
kind this module was written to remove. `medos.*` records must be correct by construction
or fail a test.

STRICTER THAN `MOS-SEC-110`'s LITERAL SHAPE, AND WHY IT MUST BE
---------------------------------------------------------------
`^\\d(\\.\\d+){3,}$` needs four or more numeric components. `2.25.9001` — a legal
ISO/IEC 9834-8 derived UID and the series UID the Gateway suite uses — has three and
would pass that filter untouched. So the second shape below matches the `2.25.` root at
any length. This is also the clearest evidence that the shape filter cannot be the
primary control: it is a pattern over a value space that does not have a closed pattern.

Spec: MOS-SEC-102, MOS-SEC-104, MOS-SEC-105, MOS-SEC-106, MOS-SEC-107, MOS-SEC-109,
MOS-SEC-110, MOS-SEC-116, MOS-SEC-117, MOS-DATA-007, MOS-DATA-022, MOS-DATA-026,
chapter 3 acceptance check 14.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from medos.sdk.canonical import study_ref

__all__ = [
    "UNMATCHED_ROUTE",
    "REDACTED_P2",
    "LOGGABLE_PATH_PARAMS",
    "STUDY_UID_PARAMS",
    "SERIES_UID_PARAMS",
    "SOP_UID_PARAMS",
    "route_template",
    "route_fields",
    "redact_p2",
    "install_uid_shape_redaction",
]

# What a record says instead of a path when no route claimed the request. A 404's path is
# entirely caller-controlled and is the one string an attacker can put anything into, so
# it is not echoed at all: the method and the status already say what happened.
UNMATCHED_ROUTE = "<unmatched>"

# MOS-SEC-110's literal replacement spelling: "replacing matches with `[redacted:<class>]`".
REDACTED_P2 = "[redacted:P2]"


# =====================================================================================
# The closed allowlist. A path parameter reaches a log record only by being in here.
#
# The value is the FIELD NAME the record gets; the comment is the MOS-SEC-102 class that
# makes it admissible under the MOS-SEC-105 `Structured logs` row (P0 and P1 only).
# =====================================================================================
LOGGABLE_PATH_PARAMS: dict[str, str] = {
    "t": "tenant_id",            # P1 -- MOS-DATA-007's `{t}`
    "tenant_id": "tenant_id",    # P1
    "job_id": "job_id",          # P1 -- MOS-SEC-107 names JobID as a constructible Attr
    "f": "frame",                # P0 -- MOS-DATA-007's `{f}`, an ordinal
}

# P2 by MOS-SEC-102. Never logged; the study is represented by MOS-SEC-106's `study_ref`.
STUDY_UID_PARAMS: tuple[str, ...] = ("st", "study_instance_uid", "study_uid")
SERIES_UID_PARAMS: tuple[str, ...] = ("se", "series_instance_uid", "series_uid")
SOP_UID_PARAMS: tuple[str, ...] = ("sop", "sop_instance_uid", "sop_uid")


# =====================================================================================
# The primary control: template + allowlisted parameters + MOS-SEC-106's replacement.
# =====================================================================================
def _scope_of(target: Any) -> dict[str, Any]:
    """Accept an ASGI scope, a Starlette `Request`, or anything carrying `.scope`."""
    scope = getattr(target, "scope", target)
    return scope if isinstance(scope, dict) else {}


def route_template(target: Any) -> str:
    """The matched route's TEMPLATE, e.g. `/dicomweb/{t}/studies/{st}/metadata`.

    `MOS-SEC-117`'s rule, applied to the log surface that shares the `MOS-SEC-105` row
    with span names. Resolved from the application's own route table rather than from a
    scope key, because Starlette 0.41 sets `endpoint` and `path_params` on the scope and
    does NOT set `route`: reading a key that may or may not be there would fail OPEN into
    the raw path on a version bump, and this function must fail closed.

    Returns `UNMATCHED_ROUTE` when nothing matched, which is every `404` on a Gateway that
    `MOS-DATA-008` forbids a catch-all.
    """
    scope = _scope_of(target)
    if scope.get("type") != "http":
        return UNMATCHED_ROUTE
    app = scope.get("app")
    routes = getattr(app, "routes", None) or ()
    partial: str | None = None
    try:
        from starlette.routing import Match
    except Exception:  # pragma: no cover - starlette is a hard dependency
        return UNMATCHED_ROUTE
    for route in routes:
        try:
            match, _child = route.matches(scope)
        except Exception:  # noqa: BLE001 - a route that cannot match is not a leak
            continue
        template = getattr(route, "path_format", None) or getattr(route, "path", None)
        if not isinstance(template, str) or not template:
            continue
        if match == Match.FULL:
            return template
        if match == Match.PARTIAL and partial is None:
            # A 405: the path IS one of ours, only the method is not. The template is
            # known and is the more useful thing to log.
            partial = template
    return partial or UNMATCHED_ROUTE


def route_fields(target: Any) -> dict[str, Any]:
    """Everything a log record may say about WHICH resource a request addressed.

    `route` is the template (`MOS-SEC-117`). Each path parameter on
    `LOGGABLE_PATH_PARAMS` is emitted under its allowlisted field name; every other
    parameter is dropped, UIDs included. When the route carried a study UID, `study_ref`
    is emitted in its place -- `MOS-SEC-106`: "A P2 identifier needed for correlation MUST
    be replaced by `study_ref` (P1) on every surface where the matrix forbids P2."

    The `study_ref` is computed against the tenant in the URL, which for a refused request
    (`MOS-DATA-009`'s spoofed `{t}`) is the tenant the caller CLAIMED. That is the honest
    value at this layer: the middleware runs outside authentication and has no principal,
    and a ref is tenant-scoped precisely so that a ref minted under the wrong tenant joins
    nothing. Both facts are P1, so neither widens the matrix row.
    """
    scope = _scope_of(target)
    fields: dict[str, Any] = {"route": route_template(scope)}
    params = scope.get("path_params") or {}
    if not isinstance(params, dict):
        return fields
    for name, value in params.items():
        field = LOGGABLE_PATH_PARAMS.get(str(name))
        if field:
            fields[field] = str(value)
    study_uid = next(
        (str(params[name]) for name in STUDY_UID_PARAMS if params.get(name)), None
    )
    if study_uid:
        fields["study_ref"] = study_ref(str(fields.get("tenant_id") or ""), study_uid)
    return fields


# =====================================================================================
# The secondary control: MOS-SEC-110's shape filter, over third-party loggers only.
# =====================================================================================
_UID_SHAPES: tuple[re.Pattern[str], ...] = (
    # MOS-SEC-110's own shape, unanchored so it matches inside a URL or a message.
    re.compile(r"(?<![\w.\-])\d+(?:\.\d+){3,}(?![\w.\-])"),
    # The ISO/IEC 9834-8 derived-UID root. `2.25.9001` has three components and slips
    # through the shape above; every `2.25.<n>` is a UID regardless of length.
    re.compile(r"(?<![\w.\-])2\.25\.\d+(?![\w.\-])"),
)

_OUR_LOGGER_PREFIX = "medos"

_installed = False


def _is_ipv4(token: str) -> bool:
    """A dotted quad, which `MOS-SEC-110`'s shape cannot tell from a four-component UID.

    `0.0.0.0` and `10.1.2.3` both satisfy `^\\d(\\.\\d+){3,}$`. Redacting them would erase
    a bind address from a start-up line and the peer address from a client log -- and
    `source_ip` is a field `MOS-SEC-146` requires the audit trail to CARRY, so it is not a
    value this control is meant to remove.

    The residual, stated: a DICOM UID of exactly four short components (`1.2.3.4`) is left
    alone by this filter. That costs nothing, because the filter is `MOS-SEC-110`'s
    defence in depth and not the control that holds UIDs -- `route_fields` is, and it
    never sees a UID's value at all.
    """
    parts = token.split(".")
    if len(parts) != 4:
        return False
    return all(p.isdigit() and len(p) <= 3 and int(p) <= 255 for p in parts)


def _replace(match: re.Match[str]) -> str:
    token = match.group(0)
    return token if _is_ipv4(token) else REDACTED_P2


def redact_p2(text: str) -> str:
    """Replace every UID-shaped token with `MOS-SEC-110`'s `[redacted:<class>]`."""
    for shape in _UID_SHAPES:
        text = shape.sub(_replace, text)
    return text


def _redact_arg(arg: Any) -> Any:
    """Redact one `%`-format argument without changing how it formats.

    A numeric argument is returned untouched: a UID-shaped token is never an `int` or a
    `float`, and turning one into a string would break a `%d`. A non-primitive (httpx
    passes an `httpx.URL` object, not a string) is rendered only to test it, and is
    replaced by the redacted rendering only when the rendering actually contained a UID,
    so `%s` formatting is unchanged in every other case.
    """
    if isinstance(arg, str):
        return redact_p2(arg)
    if isinstance(arg, (bool, int, float)) or arg is None:
        return arg
    try:
        rendered = str(arg)
    except Exception:  # noqa: BLE001 - a __str__ that raises is not our problem to fix
        return arg
    redacted = redact_p2(rendered)
    return redacted if redacted != rendered else arg


def install_uid_shape_redaction() -> None:
    """`MOS-SEC-110`'s defence-in-depth filter, as a `LogRecord` factory. Idempotent.

    A factory and not a `logging.Filter`, because a filter attached to a logger runs only
    for records that ORIGINATE at that logger -- `logging.Logger.handle` calls
    `self.filter(record)` on the originating logger and never on its ancestors. Covering
    the libraries that render URLs would therefore mean naming `httpx`, `httpcore`,
    `urllib3.connectionpool`, `uvicorn.access` and whatever the next dependency calls its
    logger, and missing one is a silent leak. The record factory sees every record from
    every logger in the process, so the list is closed the other way round: everything
    that is not `medos.*`.

    `medos.*` IS EXCLUDED DELIBERATELY. `MOS-SEC-110` ends "It is defence in depth and
    MUST NOT be the primary control". If this scrubbed our own records, the next call
    site that logged a raw path would be invisible to
    `tests/integration/test_gateway.py::test_no_dicom_uid_reaches_the_gateway_log_stream`
    and the control that actually holds -- `route_fields` -- would rot untested.

    Only `msg` and `args` are rewritten. `extra` members are attached by
    `Logger.makeRecord` AFTER the factory returns, which is out of reach here; that is
    acceptable because `extra` is a MedicalOS-side idiom and `medos.*` records are held by
    the primary control and by the test above. REPORTED as the residual: a third-party
    library that logs a UID through `extra` would not be caught here.
    """
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        name = getattr(record, "name", "") or ""
        if name == _OUR_LOGGER_PREFIX or name.startswith(_OUR_LOGGER_PREFIX + "."):
            return record
        if isinstance(record.msg, str):
            record.msg = redact_p2(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(_redact_arg(a) for a in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
        return record

    logging.setLogRecordFactory(factory)
    _installed = True
