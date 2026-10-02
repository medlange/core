# SPDX-License-Identifier: Apache-2.0
"""The event envelope of `MOS-EXEC-075`, and the adapter that produces a conformant one.

docs/spec/05-execution.md section 5.14: "Every event on every topic, and every
`job_queue.envelope`, MUST conform to the envelope schema below. There is exactly one
envelope schema and one typed payload schema per `event_type`."

`ENVELOPE_SCHEMA` below is that schema, transcribed key for key from section 5.14. It is
not paraphrased and not relaxed.

WHY THERE IS A VALIDATOR HERE AND NOT AN IMPORT
------------------------------------------------
`medos/medos/registry/jsonschema.py` is a strict JSON Schema 2020-12 validator with a closed
keyword set, written for the artifact registry in this same release. It refuses a schema
carrying a keyword or a `format` it does not implement, which is the property that makes
it trustworthy -- and its `FORMATS` set is `{date-time, uri}`, while MOS-EXEC-075 uses
`format: uuid` on three fields. Reusing it would mean either widening another component's
module mid-release or editing chapter 5's schema text, and both are worse than eighty
lines here.

So this module validates the keywords MOS-EXEC-075 actually uses -- and
`tests/unit/test_bus_envelope.py` asserts that the set of keywords appearing anywhere in
`ENVELOPE_SCHEMA` is a subset of the set this validator implements. A keyword nobody
implements is then a test failure rather than a silently ignored constraint, which is the
failure mode the registry component's `MOS-REG-015` note is about.

TWO SPEC CONFLICTS, REPORTED AND NOT PATCHED
---------------------------------------------
1. `MOS-EXEC-075` types `job_id` as `format: uuid`. `MOS-STORE-357` (chapter 12) says
   "`jobs.public_id` ... is the only job id that appears in a URL, an EVENT ENVELOPE, a
   webhook body or an agent tool input". Those cannot both hold: `job_...<ULID>` is not a
   uuid and the schema is `additionalProperties: false`, so there is nowhere to put the
   other one. Chapter 5 owns the envelope, so `job_id` carries the uuid as MOS-EXEC-075
   writes it, and `payload.job_public_id` carries the public id for any consumer that
   needs to address the API -- `payload` is the one object the envelope leaves open. The
   conflict is in this component's report.

2. Driver 1's `job_queue.envelope` (`medos/medos/db/queue.py::_dispatch_envelope`, weeks 1-2)
   does NOT conform to this schema: it omits `tenant_id`, `job_seq`, `partition_key`,
   `traceparent`, `idempotency_key` and `attempt`, and carries an extra `queue` key that
   `additionalProperties: false` forbids. That is pre-existing and it is not repaired
   here, because rewriting driver 1's envelope shape would touch the runner, the API and
   four test modules for a reason unrelated to this component. `to_bus_envelope()` below
   is the adapter that upgrades it at the outbox boundary -- which is the boundary where
   it goes on a wire and therefore the boundary MOS-EXEC-075 is about. Also reported.

Spec: MOS-EXEC-044, MOS-EXEC-075, MOS-EXEC-076, MOS-EXEC-077, MOS-EXEC-078, MOS-EXEC-079,
      MOS-STORE-272, MOS-STORE-357.
"""

from __future__ import annotations

import os
import re
import secrets
from typing import Any, Final

from medos.bus.topics import EVENT_TYPES

__all__ = [
    "ENVELOPE_SCHEMA",
    "IMPLEMENTED_KEYWORDS",
    "EnvelopeInvalid",
    "envelope_errors",
    "from_bus_envelope",
    "producer_identity",
    "to_bus_envelope",
    "traceparent_for",
    "validate_envelope",
]


# =====================================================================================
# The schema. docs/spec/05-execution.md section 5.14, transcribed.
# =====================================================================================
ENVELOPE_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://spec.medicalos.org/schemas/v1/events/envelope/1-0-0.json",
    "title": "MedicalOS event envelope",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_ref", "event_id", "event_type", "event_time", "producer",
        "tenant_id", "job_id", "job_seq", "partition_key", "traceparent",
        "trace_id", "correlation_id", "idempotency_key", "attempt", "payload",
    ],
    "properties": {
        "schema_ref": {"type": "string", "format": "uri"},
        "event_id": {"type": "string", "format": "uuid"},
        "event_type": {"enum": list(EVENT_TYPES)},
        "event_time": {"type": "string", "format": "date-time"},
        "producer": {
            "type": "string",
            "pattern": r"^medicalos\.[a-z-]+@[0-9]+\.[0-9]+\.[0-9]+(\+[0-9a-f]{7})?$",
        },
        "tenant_id": {"type": "string", "format": "uuid"},
        "job_id": {"type": "string", "format": "uuid"},
        # MOS-EXEC-078: the carriage of `job_events.seq`. It is the ordering mechanism
        # across three separate lifecycle topics, not a nicety.
        "job_seq": {"type": "integer", "minimum": 1},
        # MOS-EXEC-044.
        "partition_key": {"type": "string", "pattern": r"^[0-9a-f-]{36}:[0-9.]+$"},
        # MOS-EXEC-076: traceparent, trace_id and correlation_id are three things.
        "traceparent": {"type": "string",
                        "pattern": r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"},
        "trace_id": {"type": "string", "pattern": r"^[0-9a-f]{32}$"},
        "correlation_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "idempotency_key": {"type": "string", "pattern": r"^ik_[a-z2-7]{26}$"},
        "attempt": {"type": "integer", "minimum": 0, "maximum": 10},
        # MOS-EXEC-077: "payload MUST have at least one property. There is no event type
        # with an empty payload object."
        "payload": {"type": "object", "minProperties": 1},
    },
}

# Every keyword this module's validator ASSERTS. `tests/unit/test_bus_envelope.py` asserts
# that ENVELOPE_SCHEMA uses nothing outside it, so a constraint cannot be added to the
# schema and then quietly ignored.
IMPLEMENTED_KEYWORDS: Final[frozenset[str]] = frozenset({
    "$schema", "$id", "title",                    # annotations
    "type", "enum", "required", "properties", "additionalProperties",
    "pattern", "minLength", "maxLength", "format",
    "minimum", "maximum", "minProperties",
})

_FORMATS: Final[dict[str, re.Pattern[str]]] = {
    "uuid": re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                       r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    "date-time": re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
                            r"(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"),
    "uri": re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:"),
}

_PY_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "object": (dict,), "array": (list,), "string": (str,),
    "number": (int, float), "integer": (int,), "boolean": (bool,),
}


class EnvelopeInvalid(ValueError):
    """The envelope does not conform to MOS-EXEC-075.

    Raised at the PRODUCER, before the row reaches `job_outbox`. `MOS-TEST-041` (producer
    obligation) requires every message on a covered boundary to be validated against the
    boundary schema, and `C-QUEUE` is such a boundary; catching it at the consumer instead
    would mean the poison message is already durable and already someone else's problem.
    """


def envelope_errors(envelope: Any) -> list[str]:
    """Every way `envelope` violates MOS-EXEC-075, as human-readable strings.

    Returns a LIST rather than raising on the first problem: a producer that emitted four
    wrong fields should learn about four, not be led through them one CI run at a time.
    """
    out: list[str] = []
    if not isinstance(envelope, dict):
        return [f"envelope must be an object, got {type(envelope).__name__}"]

    props: dict[str, Any] = ENVELOPE_SCHEMA["properties"]

    for key in ENVELOPE_SCHEMA["required"]:
        if key not in envelope:
            out.append(f"{key}: required (MOS-EXEC-075)")

    for key in sorted(envelope):
        if key not in props:
            out.append(
                f"{key}: not permitted -- the envelope is additionalProperties:false "
                "(MOS-EXEC-075)"
            )

    for key, schema in props.items():
        if key not in envelope:
            continue
        out.extend(f"{key}: {msg}" for msg in _field_errors(envelope[key], schema))

    return out


def _field_errors(value: Any, schema: dict[str, Any]) -> list[str]:
    out: list[str] = []

    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{value!r} is not one of {schema['enum']}")
        return out

    expected = schema.get("type")
    if expected is not None:
        types = _PY_TYPES[expected]
        # bool is a subclass of int and is not an integer here: `attempt: true` must not
        # pass `{"type": "integer"}`.
        if isinstance(value, bool) and expected in {"integer", "number"}:
            out.append(f"expected {expected}, got boolean")
            return out
        if not isinstance(value, types):
            out.append(f"expected {expected}, got {type(value).__name__}")
            return out

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            out.append(f"does not match {pattern}")
        fmt = schema.get("format")
        if fmt is not None and _FORMATS[fmt].match(value) is None:
            out.append(f"is not a {fmt}")
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            out.append(f"longer than maxLength {schema['maxLength']}")

    if isinstance(value, int) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{value} > maximum {schema['maximum']}")

    if isinstance(value, dict) and "minProperties" in schema:
        if len(value) < schema["minProperties"]:
            out.append(
                f"has {len(value)} properties, minProperties is {schema['minProperties']} "
                "(MOS-EXEC-077: there is no event type with an empty payload)"
            )

    return out


def validate_envelope(envelope: Any) -> dict[str, Any]:
    """Return the envelope, or raise `EnvelopeInvalid` naming every violation."""
    errors = envelope_errors(envelope)
    if errors:
        raise EnvelopeInvalid(
            "envelope violates MOS-EXEC-075:\n  " + "\n  ".join(errors)
        )
    assert isinstance(envelope, dict)
    return envelope


# =====================================================================================
# Producing a conformant envelope
# =====================================================================================
def producer_identity(component: str = "control-plane") -> str:
    """`medicalos.<component>@<major>.<minor>.<patch>` -- MOS-EXEC-075's `producer`.

    The version is `MEDOS_RELEASE` when set, else the first three numeric components of
    `medos.__version__`.

    REPORTED: `medos/medos/__init__.py` still carries `__version__ = "0.1.0.dev0"` at 0.3.0, so
    the default value here is stale on a 0.3.0 deployment. Bumping it is a one-line edit
    to a file every component imports, and doing that unilaterally mid-release is how two
    agents collide; `MEDOS_RELEASE` is the seam and the drift is in this component's
    report.
    """
    from medos import __version__

    raw = os.environ.get("MEDOS_RELEASE") or __version__
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", raw)
    if m is None:  # pragma: no cover - __version__ has always been PEP 440
        raise ValueError(f"cannot derive a semver producer version from {raw!r}")
    if not re.fullmatch(r"[a-z-]+", component):
        raise ValueError(
            f"MOS-EXEC-075: producer component must match [a-z-]+, got {component!r}"
        )
    return f"medicalos.{component}@{m.group(1)}.{m.group(2)}.{m.group(3)}"


def traceparent_for(trace_id: str, *, span_id: str | None = None) -> str:
    """A W3C Trace Context `traceparent` carrying `trace_id`. MOS-EXEC-076.

    "`traceparent` is the W3C Trace Context header value produced by OpenTelemetry,
    `trace_id` is its trace-id segment carried separately so it can be indexed." There is
    no OpenTelemetry SDK in this deployment, so the span id is minted here; the trace id
    is NOT, because inventing one would break the join from the API span that
    MOS-EXEC-076 exists to preserve.

    Flags are `01` (sampled): an event that reached the bus is one the platform decided to
    record, and marking it unsampled would drop the very spans an operator follows a job
    with.
    """
    if re.fullmatch(r"[0-9a-f]{32}", trace_id) is None:
        raise ValueError(f"MOS-EXEC-076: trace_id must be 32 hex characters, got {trace_id!r}")
    span = span_id or secrets.token_hex(8)
    if re.fullmatch(r"[0-9a-f]{16}", span) is None:
        raise ValueError(f"span id must be 16 hex characters, got {span!r}")
    return f"00-{trace_id}-{span}-01"


def to_bus_envelope(
    dispatch: dict[str, Any],
    *,
    tenant_id: str,
    job_uuid: str,
    job_seq: int,
    partition_key: str,
    attempt: int,
    idempotency_key: str,
    producer: str | None = None,
) -> dict[str, Any]:
    """Upgrade driver 1's internal dispatch envelope to a MOS-EXEC-075 wire envelope.

    The input is what `medos.db.queue.PostgresJobQueue._dispatch_envelope()` builds and
    stores in `job_queue.envelope` -- a weeks 1-2 shape that predates tenancy and predates
    the outbox. The output is the envelope that goes into `job_outbox` and onto the bus.

    The adapter exists rather than a rewrite of the driver-1 shape because the two
    envelopes answer different questions and only one of them is a wire contract; see the
    module docstring, conflict 2.

    MOS-STORE-272 / MOS-EXEC-079: no PHI crosses this boundary. Everything moved is a UID,
    an identifier, a count or a timestamp, and `payload` is carried through unchanged from
    a producer that is already bound by the same rule -- stated here because this is the
    last place a reviewer sees the payload before it is durable.
    """
    payload = dict(dispatch.get("payload") or {})
    # MOS-STORE-357's public id has nowhere else to go under additionalProperties:false,
    # and a consumer that wants to call the API needs it. See conflict 1.
    payload["job_public_id"] = dispatch["job_id"]
    # MOS-EXEC-033: the inbox name, so a consumer can tell which inbox delivered a record
    # without parsing the topic -- MOS-EXEC-043 forbids inferring semantics from a topic
    # name and this keeps that rule cheap to obey.
    payload["queue"] = dispatch.get("queue", "")

    envelope = {
        "schema_ref": dispatch["schema_ref"],
        "event_id": dispatch["event_id"],
        "event_type": dispatch["event_type"],
        "event_time": dispatch["event_time"],
        "producer": producer or producer_identity(),
        "tenant_id": str(tenant_id).lower(),
        "job_id": str(job_uuid).lower(),
        "job_seq": int(job_seq),
        "partition_key": partition_key,
        "traceparent": traceparent_for(dispatch["trace_id"]),
        "trace_id": dispatch["trace_id"],
        "correlation_id": dispatch["correlation_id"],
        "idempotency_key": idempotency_key,
        "attempt": int(attempt),
        "payload": payload,
    }
    return validate_envelope(envelope)


def from_bus_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    """The exact inverse of `to_bus_envelope()`: the driver-1 dispatch envelope.

    Driver 2's consumer writes `job_queue.envelope` when it persists a claim
    (`MOS-EXEC-047` step 2), and what it writes there MUST be what driver 1 writes, or the
    two drivers disagree about the contents of a row the conformance suite reads. So the
    bus envelope is unwrapped back to the internal shape rather than stored as-is, and
    `tests/integration/test_queue_parity.py` compares the two drivers' stored envelopes
    key for key.

    `producer` is restored to driver 1's literal rather than carried across. The two
    values answer different questions -- `medicalos.control-plane@0.3.0` is who put the
    record on the WIRE (MOS-EXEC-075's field, pattern-checked), `medos.control-plane` is
    what driver 1 has written into `job_queue.envelope` since weeks 1-2 -- and reconciling
    them is the pre-existing conformance gap named in this module's docstring, not
    something to paper over inside an inverse function.
    """
    payload = dict(envelope.get("payload") or {})
    public_id = payload.pop("job_public_id", None)
    queue = payload.pop("queue", "")
    if public_id is None:
        raise EnvelopeInvalid(
            "payload.job_public_id is missing; this envelope did not come from "
            "to_bus_envelope() and cannot be unwrapped (MOS-STORE-357)"
        )
    return {
        "schema_ref": envelope["schema_ref"],
        "event_id": envelope["event_id"],
        "event_type": envelope["event_type"],
        "event_time": envelope["event_time"],
        "producer": _DRIVER1_PRODUCER,
        "queue": queue,
        "job_id": public_id,
        "trace_id": envelope["trace_id"],
        "correlation_id": envelope["correlation_id"],
        "payload": payload,
    }


# `medos.db.queue.PostgresJobQueue._dispatch_envelope` writes this literal. Named here so
# the inverse above does not hard-code a string that could drift; a unit test asserts the
# two agree.
_DRIVER1_PRODUCER: Final[str] = "medos.control-plane"
