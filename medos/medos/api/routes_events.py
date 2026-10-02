# SPDX-License-Identifier: Apache-2.0
"""`GET /api/v1/jobs/{job_id}/events` -- Server-Sent Events over `job_events`.

CONTRACT.md section 9: "SSE stream of `job_events` rows, resumable via `Last-Event-ID`
mapped to `seq`."

`seq` IS the resume token
-------------------------
`MOS-API-061` fixes the SSE `id:` field to the per-job monotonic `sequence`, and
`MOS-STORE-271` makes `job_events.seq` exactly that: assigned under the same row lock as
the state change, gapless from 1, ordered per job. So `Last-Event-ID: 4` means "I have
applied through seq 4", the server replays from 5, and there is no second cursor concept
to keep consistent. `medos.db.repo.list_events(after_seq=...)` is the whole read.

Two rules from chapter 10 that shape the code below
---------------------------------------------------
`MOS-API-060`: the stream is projected from PostgreSQL and MUST NOT be projected from
the message bus. PostgreSQL is the sole source of truth for job state; a bus-backed
stream reorders `RUNNING` after `COMPLETED` under ordinary consumer lag. This slice has
no bus at all (CONTRACT.md section 0), so the rule costs nothing here -- but the polling
loop is written against `job_events` deliberately rather than against a `LISTEN/NOTIFY`
shortcut, because `MOS-EXEC-037` makes polling the correctness path and NOTIFY only a
latency optimisation.

`MOS-API-059`: if the job does not exist the server MUST answer with a problem document
BEFORE upgrading to the stream -- never an empty `200` stream. The existence check below
therefore happens on the request connection, outside the generator, and returns a
`ProblemResponse`.

`MOS-API-062`: a client that connects AFTER the job terminated still receives every
event including the terminal one. That is why there is no race between the `202` from
`POST /api/v1/jobs` and the stream connect, and it is the reason the loop drains once
more after observing a terminal state instead of closing on the first empty read.

No PHI on the wire: `MOS-API-006` forbids it in an SSE payload and `MOS-STORE-272`
forbids it in the row. The envelope carries UIDs, states, phases and counts.

Spec: MOS-API-002, MOS-API-006, MOS-API-059, MOS-API-060, MOS-API-061, MOS-API-062,
MOS-API-063, MOS-API-064, MOS-API-066, MOS-API-069, MOS-EXEC-037, MOS-EXEC-078,
MOS-STORE-271, MOS-STORE-272, CONTRACT.md section 9.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Header, Path, Query
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from medos.api import connection_factory
from medos.api.authz import require
from medos.api.problems import (
    TRACE_HEADER,
    build_problem,
    json_safe,
    problem_response,
    trace_id_of,
)
from medos.api.routes_jobs import job_not_found_problem, reject_bad_job_id
from medos.db import repo

__all__ = ["router", "SSE_MEDIA_TYPE", "TERMINAL_EVENT_TYPES", "sse_frame", "event_envelope"]

log = logging.getLogger("medos.api")

router = APIRouter(prefix="/api/v1", tags=["jobs"])

# MOS-API-002 / MOS-API-059.
SSE_MEDIA_TYPE = "text/event-stream; charset=utf-8"

# MOS-API-063: the client-side reconnect delay, emitted as the first line of the stream.
RETRY_MS = 3000

# MOS-API-063: "a comment heartbeat at least every 15 seconds while the job is
# non-terminal". Ten, so one dropped tick does not breach the requirement.
HEARTBEAT_SECONDS = 10.0

# MOS-EXEC-037: polling is the correctness path. Fast enough that a state change is
# visible inside one animation frame's worth of latency; slow enough that an idle stream
# is ~2.5 statements/second on one connection.
POLL_SECONDS = 0.4

# Page size for the replay read. `list_events` is ordered by seq, so paging is a loop.
PAGE_SIZE = 200

# An upper bound on one connection's lifetime. Not a chapter 10 requirement -- it is an
# operational guard, since a job's `deadline_at` default is 6 hours (chapter 5 section
# 5.6.2) and an abandoned browser tab would otherwise hold a Postgres connection for it.
# The client reconnects with `Last-Event-ID` and loses nothing (MOS-API-062).
MAX_STREAM_SECONDS = 3600.0

# MOS-API-064. `job.cancelled` is listed because chapter 10 lists it; nothing in this
# slice produces it -- CONTRACT.md section 3 reserves CANCELLED and `job_state_transition`
# has no row reaching it, so the value is unreachable rather than merely unused.
TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {"job.completed", "job.failed", "job.rejected", "job.cancelled"}
)

TERMINAL_STATES: frozenset[str] = frozenset({"COMPLETED", "FAILED", "CANCELLED", "REJECTED"})

# MOS-API-069: `event_schema_version` starts at 1 and versions the `data` shape.
EVENT_SCHEMA_VERSION = 1


# =====================================================================================
# Framing
# =====================================================================================
def event_envelope(row: dict[str, Any], *, job_id: str, trace_id: str) -> dict[str, Any]:
    """One `job_events` row -> the `MOS-API-069` envelope.

    Two deliberate deviations from the table, both reported rather than papered over:

    * `tenant_id` is OMITTED. CONTRACT.md section 0 removes tenancy from this slice and
      section 8 refuses a fake single-tenant value; emitting `"tenant_id": null` would
      teach a consumer to accept null there permanently. An absent member is additive to
      restore (MOS-API-093).
    * `data` carries the row's own content -- `from_state`, `to_state`, `phase`, `actor`
      and the stored `payload` -- and NOT the `steps_completed` / `steps_total` pair that
      MOS-API-065's example shows. Those live on `jobs`, not on the event, so reading
      them here would report the counter's value NOW rather than at the moment the event
      committed. A progress number that silently means "some later time" is the
      fabricated signal MOS-API-051 exists to prevent. The step counters are read from
      `GET /api/v1/jobs/{id}`, where they are honestly current.

    `payload` is nested rather than merged so a future payload key cannot shadow
    `actor` or `phase`.
    """
    return {
        "event_id": str(row["event_id"]),
        "event_type": row["event_type"],
        "event_schema_version": EVENT_SCHEMA_VERSION,
        "occurred_at": json_safe(row["occurred_at"]),
        "sequence": int(row["seq"]),
        "subject": {"kind": "job", "id": job_id},
        "data": {
            "from_state": row["from_state"],
            "state": row["to_state"],
            "phase": row["phase"],
            "actor": row["actor"],
            "payload": json_safe(row["payload"]),
        },
        "trace_id": trace_id,
    }


def sse_frame(envelope: dict[str, Any]) -> str:
    """`MOS-API-061`: `id:` is the sequence, `event:` is the type, `data:` is ONE line of
    compact JSON. One line matters -- a pretty-printed body would be parsed by an SSE
    client as several `data:` fields concatenated with newlines."""
    body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    return f"id: {envelope['sequence']}\nevent: {envelope['event_type']}\ndata: {body}\n\n"


def _heartbeat() -> str:
    """An SSE comment. Keeps proxies from reaping an idle connection and is ignored by
    every conformant client (`MOS-API-063`)."""
    return f": heartbeat {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n\n"


# =====================================================================================
# GET /api/v1/jobs/{job_id}/events
# =====================================================================================
@router.get("/jobs/{job_id}/events", summary="Stream one job's events (SSE)")
async def stream_job_events(
    request: Request,
    job_id: Annotated[str, Path(max_length=64)],
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id_query: Annotated[str | None, Query(alias="last_event_id")] = None,
) -> Response:
    """Replay from `Last-Event-ID + 1`, then stream live, then close on the terminal event.

    The `last_event_id` QUERY parameter is accepted as well as the header. The header is
    what `EventSource` sends on its own reconnect and is authoritative when both are
    present; the query parameter exists because `curl` and the OHIF panel's initial fetch
    cannot set a request header on an `EventSource`, and because a resumable stream that
    can only be resumed by one particular client is not resumable.
    """
    # MOS-API-005: the registry declares `job.read` on this route. An SSE stream of a
    # job's lifecycle is a read of that job, and it enforced nothing.
    denied = require(request, "job.read")
    if denied is not None:
        return denied

    trace_id = trace_id_of(request)
    instance = request.url.path

    bad = reject_bad_job_id(job_id, instance=instance, trace_id=trace_id)
    if bad is not None:
        return bad  # type: ignore[no-any-return]

    raw = last_event_id_header if last_event_id_header is not None else last_event_id_query
    try:
        after_seq = _parse_last_event_id(raw)
    except ValueError:
        return problem_response(
            build_problem(
                status=400,
                code="INVALID_LAST_EVENT_ID",
                title="Malformed Last-Event-ID",
                detail="Last-Event-ID MUST be a non-negative integer job_events.seq.",
                problem_class="client_error",
                instance=instance,
                trace_id=trace_id,
            )
        )

    # MOS-API-059: answer with a problem document BEFORE upgrading to the stream. The
    # check runs on a short-lived connection of its own so the 404 path never opens the
    # long-lived streaming connection at all.
    connect = connection_factory(request)
    probe = connect()
    try:
        job = repo.get_job(probe, job_id)
        job_trace_id = str(job["trace_id"]) if job is not None else trace_id
    finally:
        probe.close()
    if job is None:
        return problem_response(
            job_not_found_problem(job_id, instance=instance, trace_id=trace_id)
        )

    headers = {
        # MOS-API-059: no buffering, no caching. `X-Accel-Buffering` is nginx's opt-out;
        # harmless elsewhere and the difference between a live stream and a 60-second
        # stall when this eventually sits behind an ingress.
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        TRACE_HEADER: trace_id,
    }
    return StreamingResponse(
        _stream(
            connect=connect,
            job_id=job_id,
            after_seq=after_seq,
            trace_id=job_trace_id,
            request=request,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers=headers,
    )


def _parse_last_event_id(raw: str | None) -> int:
    """`Last-Event-ID` -> `seq` (CONTRACT.md section 9).

    Absent or empty means "from the beginning", which `MOS-API-062` spells as sequence 1,
    i.e. `after_seq = 0`. A negative or non-integer value is a client defect and is
    refused rather than clamped: silently rewriting a resume cursor to 0 would replay an
    entire terminated job to a client that asked for three events.
    """
    if raw is None or raw.strip() == "":
        return 0
    value = int(raw.strip())  # ValueError propagates to the caller
    if value < 0:
        raise ValueError("Last-Event-ID MUST be non-negative")
    return value


async def _stream(
    *,
    connect: Any,
    job_id: str,
    after_seq: int,
    trace_id: str,
    request: Request,
) -> AsyncIterator[str]:
    """The generator. Owns one dedicated connection for the life of the stream.

    Dedicated and `autocommit=True`: a streaming read must not sit inside an open
    transaction for minutes (it would pin the snapshot and hold back vacuum), and it must
    see rows another connection committed a moment ago.

    Every database call goes through `anyio.to_thread.run_sync` because `psycopg` here is
    the synchronous driver and this is an async endpoint -- calling it inline would block
    the event loop for every other request in the process for the duration of the query.
    """
    conn = await anyio.to_thread.run_sync(lambda: connect(autocommit=True))
    started = time.monotonic()
    cursor = after_seq
    last_beat = time.monotonic()
    try:
        # MOS-API-063: `retry:` is the first line of the stream.
        yield f"retry: {RETRY_MS}\n\n"

        while True:
            if await request.is_disconnected():
                return

            rows = await anyio.to_thread.run_sync(
                lambda c=cursor: repo.list_events(conn, job_id, after_seq=c, limit=PAGE_SIZE)
            )
            for row in rows:
                envelope = event_envelope(row, job_id=job_id, trace_id=trace_id)
                cursor = envelope["sequence"]
                yield sse_frame(envelope)
                last_beat = time.monotonic()
                if row["event_type"] in TERMINAL_EVENT_TYPES:
                    # MOS-API-063: close the connection after emitting a terminal event.
                    return
            if len(rows) == PAGE_SIZE:
                continue  # a full page means there is more history; do not sleep on it

            if not rows:
                state = await anyio.to_thread.run_sync(lambda: repo.get_job(conn, job_id))
                if state is not None and state["state"] in TERMINAL_STATES:
                    # MOS-API-064: "a server receiving a reconnect for a terminal job MUST
                    # replay from Last-Event-ID and close immediately". One more read
                    # first, because the state and the event rows were read by two
                    # different statements: the terminal event could have committed
                    # between them, and closing here would drop it.
                    final = await anyio.to_thread.run_sync(
                        lambda c=cursor: repo.list_events(
                            conn, job_id, after_seq=c, limit=PAGE_SIZE
                        )
                    )
                    for row in final:
                        yield sse_frame(event_envelope(row, job_id=job_id, trace_id=trace_id))
                    return

            now = time.monotonic()
            if now - last_beat >= HEARTBEAT_SECONDS:
                yield _heartbeat()
                last_beat = now
            if now - started >= MAX_STREAM_SECONDS:
                # Not a terminal event: the client reconnects with `Last-Event-ID` and
                # resumes exactly where it stopped (MOS-API-062).
                yield ": stream-deadline reconnect\n\n"
                return
            await anyio.sleep(POLL_SECONDS)
    finally:
        conn.close()
