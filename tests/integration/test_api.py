# SPDX-License-Identifier: Apache-2.0
"""Integration tests for `medos.api`, driven over real HTTP against real PostgreSQL.

Over real HTTP and not `TestClient`: the two defects these tests exist to pin are both
wire-level. "A `REJECTED` job is a `200`, not an HTTP error" is a claim about a status
code, and "the SSE stream is resumable via `Last-Event-ID`" is a claim about chunked
transfer and request headers. An in-process ASGI shim can pass both while a real server
fails them, so every test below talks to a `uvicorn` server on a loopback port with
`httpx`.

The two defects, named
----------------------
1. CONTRACT.md section 9 / `MOS-API-029`: the client's `Idempotency-Key` header MUST NOT
   feed DICOM UID derivation. `test_client_idempotency_key_never_reaches_uid_derivation`
   submits one body under three different header values -- two distinct keys and none at
   all -- and asserts a single job row with one derived `idempotency_key`. If the header
   leaked into `derive_idempotency_key`, the three would be three jobs, and the mirror
   failure -- two different studies colliding onto one DICOM identity because they shared
   a header value -- would be reachable.

2. Chapter 10 `MOS-API-039` / `MOS-API-047`: a `REJECTED` job is a clinical answer and is
   served as `200 application/json` with a `rejection` member, never as a `4xx`.
   `test_rejected_job_is_a_200_with_a_rejection_body_not_an_http_error` pins the status,
   the media type and the `class`.

Spec: MOS-API-004, MOS-API-008, MOS-API-023, MOS-API-029, MOS-API-035, MOS-API-036,
MOS-API-037, MOS-API-039, MOS-API-040, MOS-API-042, MOS-API-045, MOS-API-046,
MOS-API-047, MOS-API-051, MOS-API-059, MOS-API-061, MOS-API-062, MOS-API-063,
MOS-API-064, MOS-API-111, CONTRACT.md sections 9, 10 and 11.
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from medos.api.app import create_app
from medos.api.problems import PROBLEM_BASE, PROBLEM_MEDIA_TYPE, WIRE_CLASSES
from medos.core.errors import GeometryRejection, TransportFailure
from medos.db import repo
from medos.db.queue import PostgresJobQueue, make_worker_id
from medos.db.tenancy import DEFAULT_TENANT_ID, tenant_context
from medos.security.authn import Principal
from psycopg.rows import dict_row

STUDY = "1.2.840.113619.2.55.3.604688119.971.1547034262.109"
SERIES = "1.2.840.113619.2.55.3.604688119.971.1547034262.117"
CAPS = ["lung_segmentation", "emphysema_laa"]

# MOS-API-063: the server MUST emit a heartbeat at least every 15 seconds. The route's
# own interval is 10; a live-stream test that waited for one would add 10 seconds to the
# suite, so the heartbeat is asserted by reading the constant and the frame shape instead
# of by sleeping. The live path is covered by `test_sse_streams_a_live_terminal_event`.
SSE_READ_TIMEOUT = 20.0


# =====================================================================================
# Server fixtures
# =====================================================================================
def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def running_server(app: Any) -> Iterator[str]:
    """Run one `uvicorn` server in a thread and yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _opener(dsn: str):
    """The injected connection factory (CONTRACT.md section 11). Bound to the throwaway
    database the session fixture created, which is what keeps these tests independent of
    any developer's local DSN."""

    def open_conn(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(dsn, row_factory=dict_row, **kwargs)

    return open_conn


@pytest.fixture(scope="session")
def base_url(pg_dsn: str) -> Iterator[str]:
    app = create_app(connect=_opener(pg_dsn), configure_logs=False)
    with running_server(app) as url:
        yield url


@pytest.fixture()
def client(base_url: str, api_key: str) -> Iterator[httpx.Client]:
    """An authenticated client. Weeks 3-5 item 2 made `/api/v1` non-anonymous.

    The `Authorization` header is a CLIENT default, so a per-request `headers=` in an
    individual test still merges with it (httpx merges, request wins on a clash) and the
    tests below that pass `Idempotency-Key` or `Last-Event-ID` are unchanged.

    The tenant these tests run as is NOT configured here and is not configurable: it is
    whatever `api_keys.tenant_id` says for this key (`MOS-API-003`). The fixture mints it
    for `DEFAULT_TENANT_ID`, which is the tenant `0002_tenancy.up.sql` backfilled every
    weeks 1-2 row onto -- so these tests see exactly the rows they did before.
    """
    with httpx.Client(
        base_url=base_url,
        timeout=SSE_READ_TIMEOUT,
        headers={"Authorization": f"Bearer {api_key}"},
    ) as c:
        yield c


# The `Authorization` header the two stub-authenticated apps below are driven with. Its
# VALUE is irrelevant -- `_StubAuthenticator` ignores it -- but it has to be PRESENT,
# because `medos.api.auth.AuthenticationMiddleware` refuses a request with no
# `Authorization` header before any driver is consulted (`MOS-SEC-008`: anonymous access
# exists on three paths and nowhere else). That belt is deliberately not unbucklable by a
# driver, which is why even a permissive stub cannot make a header-less request succeed.
STUB_AUTH = {"Authorization": "Bearer stub"}


class _StubAuthenticator:
    """An `Authenticator` that is not the API-key driver and touches no database.

    Two of the apps below deliberately cannot reach Postgres -- that is what they are
    for -- and the real `ApiKeyAuthenticator` would answer every request `503
    AUTHENTICATOR_UNAVAILABLE` before the route under test ran. Injecting a different
    driver is not a workaround: it is `MOS-SEC-009`'s port being used as intended ("Adding
    OIDC in 0.3 MUST NOT require a change to any PEP"), and these two fixtures are the
    proof that a second driver needs no change to `create_app`, to a route or to a
    handler.
    """

    def authenticate(self, credential: Any) -> Principal:
        return Principal(
            kind="service_account",
            principal_id="11111111-1111-1111-1111-111111111111",
            tenant_id=DEFAULT_TENANT_ID,
            scope=("job.create", "job.read"),
            credential_key_id="STUB00000000",
        )


@pytest.fixture(scope="session")
def broken_url() -> Iterator[str]:
    """A second, independent app whose database is unreachable.

    Two independent instances in one process is `MOS-REL-046`, and it is the only way to
    exercise the `503` and the unhandled-`500` paths without breaking the real database.
    """

    def refuse(**kwargs: Any) -> psycopg.Connection[Any]:
        raise psycopg.OperationalError("connection refused")

    app = create_app(
        connect=refuse, configure_logs=False, authenticator=_StubAuthenticator()
    )
    with running_server(app) as url:
        yield url


# =====================================================================================
# Helpers
# =====================================================================================
def submit(client: httpx.Client, **overrides: Any) -> httpx.Response:
    body: dict[str, Any] = {"study_instance_uid": STUDY, "capabilities": list(CAPS)}
    headers = overrides.pop("headers", None)
    body.update(overrides)
    return client.post("/api/v1/jobs", json=body, headers=headers)


def assert_problem(response: httpx.Response, *, status: int, cls: str, code: str) -> dict:
    """Every mandatory member of MOS-API-036 / MOS-API-037, on every problem document."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    doc = response.json()
    assert doc["type"].startswith(PROBLEM_BASE), doc["type"]  # MOS-API-036: one authority
    assert doc["status"] == status
    assert doc["class"] == cls and doc["class"] in WIRE_CLASSES  # MOS-API-038
    assert doc["code"] == code
    for member in ("title", "detail", "instance", "retryable", "trace_id", "occurred_at"):
        assert member in doc, member
    # MOS-API-004: on every response INCLUDING every error response, and as the
    # document's own member. Both halves, and both non-empty.
    assert re.fullmatch(r"[0-9a-f]{32}", doc["trace_id"]), doc["trace_id"]
    assert doc["trace_id"] == response.headers["MedicalOS-Trace-Id"]
    return doc


def drive_to_rejected(dsn: str, job_id: str) -> None:
    # This helper stands in for a worker process, and one of its callers runs it in a
    # background thread (`test_sse_streams_a_live_terminal_event`). A thread starts with
    # an empty contextvars context, so the tenant is bound here rather than inherited --
    # the same rule medos/medos/worker/runner.py::_Heartbeat._run follows (MOS-SEC-078).
    with tenant_context(DEFAULT_TENANT_ID), psycopg.connect(dsn, row_factory=dict_row) as conn:
        queue = PostgresJobQueue(conn)
        worker = make_worker_id("test")
        assert queue.claim(worker, 120) == job_id
        repo.plan_steps(conn, job_id, attempt=1)
        repo.record_series_verdicts(
            conn,
            job_id,
            [
                repo.SeriesVerdict(
                    series_instance_uid=SERIES,
                    decision="rejected",
                    reason_code="slice_thickness_out_of_range",
                    reason_detail="SliceThickness 5.0 mm exceeds max 1.5 mm",
                    instance_count=61,
                    modality="CT",
                )
            ],
        )
        queue.reject(
            job_id,
            worker,
            reason_code="no_eligible_series",
            reason_detail="No series satisfies the thin-axial selector.",
        )
        conn.commit()


def drive_to_failed(dsn: str, job_id: str) -> None:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        queue = PostgresJobQueue(conn)
        worker = make_worker_id("test")
        assert queue.claim(worker, 120) == job_id
        queue.fail_ex(
            job_id,
            worker,
            retryable=False,
            failure_class="gateway_unavailable",
            failure_code="QIDO_UNREACHABLE",
            failure_detail="DICOMweb gateway returned 503 for the series query.",
        )
        conn.commit()


def drive_to_completed(dsn: str, job_id: str) -> str:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        queue = PostgresJobQueue(conn)
        worker = make_worker_id("test")
        assert queue.claim(worker, 300) == job_id
        repo.plan_steps(conn, job_id, attempt=1)
        repo.record_series_verdicts(
            conn,
            job_id,
            [
                repo.SeriesVerdict(
                    series_instance_uid=SERIES,
                    decision="selected",
                    selector_name="chest-ct-thin-axial",
                    rank=1,
                    instance_count=3,
                    modality="CT",
                )
            ],
        )
        for key in (k for k, *_ in repo.STEP_PLAN):
            repo.start_step(conn, job_id, key)
            repo.finish_step(conn, job_id, key)
        conn.commit()
        ik = repo.get_job(conn, job_id)["idempotency_key"]
        result = repo.ResultRow(
            capability_id="lung_segmentation",
            capability_version="0.1.0",
            result_kind="segmentation",
            findings=[{"kind": "lung", "present": True, "score": None}],
            input_series_uids=(SERIES,),
            input_instance_uids=(f"{SERIES}.1", f"{SERIES}.2", f"{SERIES}.3"),
            preprocessing_version="medos-canonical-1",
            worker_version="0.1.0",
            runtime_version="python-3.11",
            geometry={"shape": [3, 512, 512], "spacing_mm": [0.7, 0.7, 2.5]},
            measurements=(
                repo.MeasurementRow(
                    concept_scheme="SCT",
                    concept_code="118565006",
                    concept_display="Volume",
                    value=4213.5,
                    ucum_unit="ml",
                    source_series_instance_uid=SERIES,
                    finding_index=0,
                ),
            ),
            dicom_objects=(
                repo.DicomObjectRow(
                    object_kind="SEG",
                    sop_class_uid="1.2.840.10008.5.1.4.1.1.66.4",
                    series_instance_uid="1.2.826.0.1.3680043.10.1338.2.9001",
                    sop_instance_uid="1.2.826.0.1.3680043.10.1338.2.9002",
                    series_number=9001,
                    output_index=0,
                    derivation_inputs=_derivation_inputs(ik, "seg_series"),
                    frame_count=3,
                ),
            ),
        )
        ids = repo.complete_job_with_results(conn, queue, job_id, worker, [result])
        conn.commit()
        return ids[0]


def _derivation_inputs(idempotency_key: str, uid_kind: str) -> dict[str, Any]:
    """MOS-STORE-286's nine-member tuple, as `result_dicom_objects` CHECKs it."""
    return {
        "tenant_id": repo.SLICE_TENANT_ID,
        "idempotency_key": idempotency_key,
        "service_id": "medos.slice",
        "service_version": "0.1.0",
        "model_id": "none",
        "model_version": "none",
        "uid_space": "medos",
        "uid_kind": uid_kind,
        "output_index": 0,
    }


def read_sse(client: httpx.Client, path: str, headers: dict[str, str] | None = None) -> list:
    """Read one SSE response to completion, returning `(id, event, data)` triples plus
    the raw comment lines. Relies on the server closing after a terminal event
    (MOS-API-063); a stream that never closes fails on the client read timeout."""
    events: list[tuple[int | None, str | None, dict | None]] = []
    comments: list[str] = []
    retry_lines: list[str] = []
    with client.stream("GET", path, headers=headers) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
        assert response.headers["cache-control"] == "no-store"
        cur: dict[str, Any] = {}
        for line in response.iter_lines():
            if line.startswith(":"):
                comments.append(line)
            elif line.startswith("retry:"):
                retry_lines.append(line)
            elif line.startswith("id:"):
                cur["id"] = int(line[3:].strip())
            elif line.startswith("event:"):
                cur["event"] = line[6:].strip()
            elif line.startswith("data:"):
                cur["data"] = json.loads(line[5:].strip())
            elif line == "" and cur:
                events.append((cur.get("id"), cur.get("event"), cur.get("data")))
                cur = {}
        if cur:
            events.append((cur.get("id"), cur.get("event"), cur.get("data")))
    return [events, comments, retry_lines]


# =====================================================================================
# Probes and the trace contract
# =====================================================================================
def test_healthz_is_live_without_touching_the_database(broken_url: str) -> None:
    """A liveness probe that fails when Postgres is down asks the orchestrator to restart
    a process that is working."""
    with httpx.Client(base_url=broken_url, timeout=10) as c:
        assert c.get("/healthz").status_code == 200


def test_readyz_is_503_transport_failure_when_the_database_is_unreachable(
    broken_url: str,
) -> None:
    with httpx.Client(base_url=broken_url, timeout=10) as c:
        doc = assert_problem(
            c.get("/readyz"),
            status=503,
            cls="transport_failure",
            code="DATABASE_UNAVAILABLE",
        )
    assert doc["retryable"] is True  # table 10.4-A
    assert "connection refused" not in json.dumps(doc)  # MOS-API-042: no driver message


def test_readyz_is_ready_against_the_real_database(client: httpx.Client) -> None:
    assert client.get("/readyz").json()["status"] == "ready"


def test_every_response_carries_a_trace_id(client: httpx.Client) -> None:
    """MOS-API-004, on a success and on an error."""
    ok = client.get("/healthz")
    assert len(ok.headers["MedicalOS-Trace-Id"]) == 32
    err = client.get("/api/v1/jobs/job_0000000000000000000000000")
    assert len(err.headers["MedicalOS-Trace-Id"]) == 32


def test_inbound_traceparent_is_adopted(client: httpx.Client) -> None:
    """MOS-API-004: "MUST accept and propagate a W3C `traceparent` request header"."""
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    response = client.get(
        "/healthz", headers={"traceparent": f"00-{trace}-00f067aa0ba902b7-01"}
    )
    assert response.headers["MedicalOS-Trace-Id"] == trace


# =====================================================================================
# POST /api/v1/jobs
# =====================================================================================
def test_post_returns_202_job_id_and_status(db: Any, client: httpx.Client) -> None:
    """`202` with `MOS-API-049`'s `status`, and CONTRACT.md section 9's `state` beside it.

    This test used to assert the body was EXACTLY `{job_id, state}`, which is CONTRACT.md
    section 9's two-member body. `MOS-API-049` names the field `status` -- `MOS-TEST-064`
    spells the distinction out: "the DB column is `state` (`MOS-EXEC-001`); `status` is the
    JSON field name of `MOS-API-049`" -- and chapter 19 `MOS-UI-021a` names the viewer
    consequence and rules that the divergence "MUST be closed in the surface, not in
    Chapter 10". Closing it in the surface requires the server to answer `status`, so the
    member was ADDED (`MOS-API-093`: additive response evolution) rather than renamed, and
    the assertion below is now that BOTH are present and that they cannot disagree.

    The set is still closed. A third status-like member would be a third answer to one
    question, which is how a client ends up branching on the wrong one.
    """
    response = submit(client)
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "status", "state"}
    assert body["status"] == "QUEUED"  # MOS-API-049, the field of record
    assert body["state"] == body["status"]  # CONTRACT.md section 9, never divergent
    job_id = body["job_id"]
    assert response.headers["location"] == f"/api/v1/jobs/{job_id}"  # MOS-API-046
    assert response.headers["retry-after"] == "2"
    assert repo.get_job(db, job_id) is not None


def test_client_idempotency_key_never_reaches_uid_derivation(
    db: Any, client: httpx.Client
) -> None:
    """CONTRACT.md section 9 / MOS-API-029 -- THE REGISTER DEFECT.

    One body, three different `Idempotency-Key` values (two distinct, one absent). The
    platform-derived `jobs.idempotency_key` -- which IS the DICOM UID seed material
    (MOS-IMG-062, MOS-EXEC-053) -- must be identical across all three, and there must be
    exactly one job row. A header that fed derivation would produce three keys here; the
    same leak read the other way would let two unrelated studies submitted under one
    header value collide onto the same SOPInstanceUID.
    """
    first = submit(client, headers={"Idempotency-Key": "client-key-A"})
    second = submit(client, headers={"Idempotency-Key": "a-completely-different-key"})
    third = submit(client)  # no header at all

    assert first.status_code == 202
    assert second.status_code == 200 and third.status_code == 200  # MOS-API-026: no dup
    job_ids = {r.json()["job_id"] for r in (first, second, third)}
    assert len(job_ids) == 1, job_ids

    job_id = job_ids.pop()
    row = repo.get_job(db, job_id)
    derived = row["idempotency_key"]
    assert derived.startswith("ik_")  # MOS-EXEC-053's grammar, not the client's
    assert "client-key-A" not in derived
    assert "a-completely-different-key" not in derived
    # The header is persisted for echo and audit, and ONLY there (MOS-API-029).
    assert row["request_idempotency_key"] == "client-key-A"
    count = db.execute(
        "SELECT count(*) AS n FROM jobs WHERE study_instance_uid = %s", (STUDY,)
    ).fetchone()["n"]
    assert count == 1

    # And the derived key is a function of the job's content: change the study, get a
    # different key. Without this half, "always the same key" would also pass.
    other = submit(client, study_instance_uid="1.2.840.113619.2.55.3.999")
    other_row = repo.get_job(db, other.json()["job_id"])
    assert other_row["idempotency_key"] != derived


def test_replay_echoes_the_clients_key_and_marks_the_replay(
    db: Any, client: httpx.Client
) -> None:
    submit(client, headers={"Idempotency-Key": "first"})
    replay = submit(client, headers={"Idempotency-Key": "second"})
    assert replay.status_code == 200
    assert replay.headers["MedicalOS-Idempotency-Key"] == "second"  # MOS-API-022
    assert replay.headers["MedicalOS-Idempotent-Replay"] == "true"  # MOS-API-026


def test_unknown_request_member_is_a_schema_violation(db: Any, client: httpx.Client) -> None:
    """MOS-API-008: `additionalProperties: false`, with a `violations[]` member."""
    response = client.post(
        "/api/v1/jobs",
        json={"study_instance_uid": STUDY, "capabilities": CAPS, "surprise": 1},
    )
    doc = assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")
    assert doc["violations"][0]["pointer"] == "/surprise"


def test_a_client_cannot_set_the_derived_idempotency_key_in_the_body(
    db: Any, client: httpx.Client
) -> None:
    """MOS-EXEC-054: `jobs.idempotency_key` "is not settable by any client". The body
    schema has no such member, so the attempt is a `400` rather than a silent drop --
    a silent drop would let a caller believe they had pinned the UID seed."""
    response = client.post(
        "/api/v1/jobs",
        json={
            "study_instance_uid": STUDY,
            "capabilities": CAPS,
            "idempotency_key": "ik_aaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
    )
    doc = assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")
    assert doc["violations"][0]["pointer"] == "/idempotency_key"


def test_malformed_study_uid_is_a_schema_violation(db: Any, client: httpx.Client) -> None:
    response = submit(client, study_instance_uid="1.2.840.113619.2.55.3.60468811X")
    doc = assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")
    assert doc["violations"][0]["pointer"] == "/study_instance_uid"


def test_unknown_capability_is_a_client_error_not_a_clinical_rejection(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-039: the two MUST NOT be conflated. A capability id that does not exist is
    a fact about the REQUEST; telling a radiologist the study was unsuitable would be
    false."""
    response = submit(client, capabilities=["lung_segmentation", "totally_made_up"])
    doc = assert_problem(response, status=400, cls="client_error", code="UNKNOWN_CAPABILITY")
    assert doc["violations"][0]["pointer"] == "/capabilities/1"
    assert "lung_segmentation" in doc["supported_capabilities"]


# =====================================================================================
# The MOS-API-043 envelope -- what the OHIF surface sends
# =====================================================================================
#
# Chapter 19 `MOS-UI-021a`: "A surface MUST send `target` + `input` per `MOS-API-043` and
# MUST read `Job.status` per `MOS-API-049`. The current extension client sends
# `{study_instance_uid, capabilities}` and reads `body.state` ... the divergence MUST be
# closed in the surface, not in Chapter 10."
#
# It cannot be closed in the surface against a server that refuses the Chapter 10 body, so
# `medos/medos/api/routes_jobs.py` accepts both envelopes. These tests pin the Chapter 10 half;
# every other test in this file still sends CONTRACT.md section 9's, which is what keeps
# "both" honest.
def test_the_chapter_10_envelope_creates_a_job(db: Any, client: httpx.Client) -> None:
    """MOS-API-043's body, with a `capability` target (MOS-API-044)."""
    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {"kind": "capability", "id": "lung_segmentation"},
            "input": {
                "study_instance_uid": STUDY,
                "prior_study_instance_uids": [],
                "series_instance_uids": [],
            },
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "QUEUED"  # MOS-API-049
    job = repo.get_job(db, body["job_id"])
    assert job is not None
    # MOS-API-043's `target` names ONE capability; the slice's job carries a SET. The
    # one-element mapping is the whole of the reconciliation and it is asserted, not
    # assumed -- see routes_jobs.py's header for the part that is NOT clean.
    assert list(job["capability_ids"]) == ["lung_segmentation"]
    assert job["study_instance_uid"] == STUDY


def test_a_service_version_target_runs_every_capability_it_implements(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-044: "When `kind` is `service_version`, `id` is a `ServiceVersion` id".

    This deployment pins exactly one, and it implements the whole registry. That is what
    lets the OHIF toolbar button submit the three-capability demo job in ONE POST, which
    is what `MOS-SAFE-089a` acceptance check 24 counts.
    """
    from medos.api.routes_jobs import SLICE_SERVICE_VERSION_ID, known_capability_ids

    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {"kind": "service_version", "id": SLICE_SERVICE_VERSION_ID},
            "input": {"study_instance_uid": "1.2.840.113619.2.55.3.888"},
        },
    )
    assert response.status_code == 202, response.text
    job = repo.get_job(db, response.json()["job_id"])
    assert job is not None
    assert sorted(job["capability_ids"]) == sorted(known_capability_ids())


def test_a_version_range_on_a_service_version_target_is_refused(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-044, verbatim: "When `kind` is `service_version` ... `version_range` MUST
    be absent". Accepting it would let a caller believe they had pinned a version when the
    member is meaningless for that kind."""
    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {
                "kind": "service_version",
                "id": "medos.slice/0.1.0",
                "version_range": ">=0.1 <1",
            },
            "input": {"study_instance_uid": STUDY},
        },
    )
    assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")


def test_an_unknown_target_kind_is_refused(db: Any, client: httpx.Client) -> None:
    """MOS-API-044: the enum is CLOSED. `service` -- chapter 14 FT-07's own mistake, and
    entry 20 of docs/spec/99-known-inconsistencies.md -- is not a member."""
    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {"kind": "service", "id": "pleural-effusion"},
            "input": {"study_instance_uid": STUDY},
        },
    )
    doc = assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")
    assert doc["violations"][0]["pointer"] == "/target/kind"


def test_mixing_the_two_envelopes_is_refused(db: Any, client: httpx.Client) -> None:
    """Two closed schemas, not one open one.

    A body carrying both `target` and `capabilities` has two answers to "what should run",
    and a server that picks one silently discards the other. `MOS-API-008` is the rule
    that makes this a `400` rather than a guess.
    """
    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {"kind": "capability", "id": "lung_segmentation"},
            "input": {"study_instance_uid": STUDY},
            "capabilities": CAPS,
        },
    )
    assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")


def test_an_unknown_member_inside_input_is_a_schema_violation(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-008's `additionalProperties: false` reaches INSIDE `input`, not just the
    top level. `idempotency_key` is the member that matters: MOS-API-043 says "There is no
    `idempotency_key` member. The client's key travels in the `Idempotency-Key` header",
    and MOS-EXEC-054 makes accepting one from a client the defect this route exists to
    prevent."""
    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {"kind": "capability", "id": "lung_segmentation"},
            "input": {"study_instance_uid": STUDY, "idempotency_key": "ik_x"},
        },
    )
    doc = assert_problem(response, status=400, cls="client_error", code="SCHEMA_VIOLATION")
    assert doc["violations"][0]["pointer"] == "/input/idempotency_key"


def test_pinning_the_series_set_is_refused_as_a_client_error(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-045 permits `input.series_instance_uids`; this release cannot honour a pin
    because selection happens at execution time (CONTRACT.md section 0).

    The refusal is `client_error` and NOT `clinical_rejection`, for the same reason
    `PRIORS_NOT_SUPPORTED` is: the platform cannot do this yet, which is a fact about the
    platform. Calling it clinical would tell a radiologist the SERIES was unsuitable,
    which `MOS-API-039` forbids conflating.
    """
    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {"kind": "capability", "id": "lung_segmentation"},
            "input": {"study_instance_uid": STUDY, "series_instance_uids": [SERIES]},
        },
    )
    assert_problem(
        response, status=422, cls="client_error", code="SERIES_PINNING_NOT_SUPPORTED"
    )


def test_priors_inside_input_are_refused(db: Any, client: httpx.Client) -> None:
    """MOS-API-045's `422` / `PRIORS_NOT_SUPPORTED`, reached through the Chapter 10
    envelope where the member actually lives."""
    response = client.post(
        "/api/v1/jobs",
        json={
            "target": {"kind": "capability", "id": "lung_segmentation"},
            "input": {
                "study_instance_uid": STUDY,
                "prior_study_instance_uids": ["1.2.840.113619.2.55.3.777"],
            },
        },
    )
    assert_problem(response, status=422, cls="client_error", code="PRIORS_NOT_SUPPORTED")


def test_priors_are_refused_until_inter_study_registration_exists(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-045: `422`, class `client_error`, code `PRIORS_NOT_SUPPORTED`."""
    response = submit(client, prior_study_instance_uids=["1.2.840.113619.2.55.3.777"])
    assert_problem(response, status=422, cls="client_error", code="PRIORS_NOT_SUPPORTED")


def test_malformed_idempotency_key_header_is_refused(db: Any, client: httpx.Client) -> None:
    """MOS-API-023: `^[A-Za-z0-9._~-]{1,255}$`."""
    response = submit(client, headers={"Idempotency-Key": "has spaces and $$$"})
    assert_problem(response, status=400, cls="client_error", code="IDEMPOTENCY_KEY_INVALID")


def test_unsupported_requested_output_is_refused(db: Any, client: httpx.Client) -> None:
    response = submit(client, requested_outputs=["SEG", "PDF"])
    assert_problem(response, status=400, cls="client_error", code="UNSUPPORTED_OUTPUT")


# =====================================================================================
# GET /api/v1/jobs/{job_id}
# =====================================================================================
def test_unknown_job_is_404_problem_json(db: Any, client: httpx.Client) -> None:
    response = client.get("/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ")
    assert_problem(response, status=404, cls="client_error", code="JOB_NOT_FOUND")


def test_lowercase_job_id_is_refused(db: Any, client: httpx.Client) -> None:
    """MOS-API-111: "Matching MUST be case-sensitive: a lowercase form ... MUST NOT be
    accepted in a path segment". Crockford base32 has two spellings of every value, so
    accepting both would make the id non-canonical."""
    job_id = submit(client).json()["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id.lower()}")
    assert_problem(response, status=400, cls="client_error", code="INVALID_JOB_ID")


def test_queued_job_reports_progress_as_steps_and_never_as_a_float(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-051 / MOS-EXEC-021: no float `progress` anywhere in the body."""
    job_id = submit(client).json()["job_id"]
    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["state"] == "QUEUED"
    assert body["steps_completed"] == 0 and body["steps_total"] == 0
    assert "progress" not in json.dumps(body)
    assert body["rejection"] is None and body["error"] is None


def test_rejected_job_is_a_200_with_a_rejection_body_not_an_http_error(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """CHAPTER 10'S NAMED DEFECT -- MOS-API-039, MOS-API-040, MOS-API-047.

    "This study was not analysed, and here is the clinical reason" and "the platform
    broke" are the same red X today and one of them is information a radiologist must
    act on. So: `200 application/json`, `rejection` populated with a
    `clinical_rejection` problem document, `error` null.
    """
    job_id = submit(client).json()["job_id"]
    drive_to_rejected(pg_dsn, job_id)

    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert not response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)

    body = response.json()
    assert body["state"] == "REJECTED"
    assert body["error"] is None  # MOS-API-040: never both
    rejection = body["rejection"]
    assert rejection["class"] == "clinical_rejection"
    assert rejection["code"] == "NO_ELIGIBLE_SERIES"
    assert rejection["status"] == 422  # the document's own status, not the response's
    assert rejection["retryable"] is False
    assert rejection["type"].startswith(PROBLEM_BASE)  # MOS-API-036
    assert rejection["series_selection_href"] == f"/api/v1/jobs/{job_id}/series-selection"


def test_the_rejections_series_selection_href_resolves(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """MOS-API-054: "Selection is first-class data, not a log line." An href the server
    emits and then 404s is a defect, not a nicety."""
    job_id = submit(client).json()["job_id"]
    drive_to_rejected(pg_dsn, job_id)
    href = client.get(f"/api/v1/jobs/{job_id}").json()["rejection"]["series_selection_href"]
    body = client.get(href).json()
    assert body["selected"] == []
    assert body["rejected"][0]["reason_code"] == "slice_thickness_out_of_range"


def test_failed_job_carries_error_and_not_rejection(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """MOS-API-040: a `FAILED` job carries `error` of class `transport_failure` or
    `system_failure`, and `rejection` null. Also a `200` -- the job is a resource that
    was read successfully; the FAILURE is its content."""
    job_id = submit(client).json()["job_id"]
    drive_to_failed(pg_dsn, job_id)
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "FAILED"
    assert body["rejection"] is None
    assert body["error"]["class"] == "transport_failure"  # MOS-EXEC-016a's map
    assert body["error"]["retryable"] is True
    assert body["error"]["type"].startswith(PROBLEM_BASE)


def test_completed_job_surfaces_results_and_the_full_provenance_record(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """CONTRACT.md section 10: "Every result MUST record: `job_id`,
    `study_instance_uid`, the series actually consumed, `capability_id` + `version`,
    `preprocessing_version`, the generated `SeriesInstanceUID` and `SOPInstanceUID` per
    object, `worker_version`, `runtime_version`, and timestamps. The
    `GET /api/v1/jobs/{id}` response surfaces this; the OHIF panel renders it."
    """
    job_id = submit(client).json()["job_id"]
    result_id = drive_to_completed(pg_dsn, job_id)

    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["state"] == "COMPLETED"
    assert body["steps_completed"] == body["steps_total"] == len(repo.STEP_PLAN)
    assert body["provenance"]["series_consumed"] == [SERIES]

    (result,) = body["results"]
    assert result["result_id"] == result_id
    prov = result["provenance"]
    for field in (
        "job_id",
        "study_instance_uid",
        "series_consumed",
        "capability_id",
        "capability_version",
        "preprocessing_version",
        "worker_version",
        "runtime_version",
        "created_at",
    ):
        assert prov[field] is not None, field
    assert prov["series_consumed"] == [SERIES]
    (obj,) = prov["generated_objects"]
    assert obj["series_instance_uid"] and obj["sop_instance_uid"]
    assert result["measurements"][0]["ucum_unit"] == "ml"
    assert result["measurements"][0]["value"] == pytest.approx(4213.5)
    # MOS-API-002: RFC 3339, milliseconds, UTC, literal Z.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", prov["created_at"])


# =====================================================================================
# GET /api/v1/jobs/{job_id}/events  (SSE)
# =====================================================================================
def test_sse_for_an_unknown_job_is_a_problem_document_not_an_empty_stream(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-059: "the server MUST answer with a problem document **before** upgrading
    to the stream ... never with an empty `200` stream"."""
    response = client.get("/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ/events")
    assert_problem(response, status=404, cls="client_error", code="JOB_NOT_FOUND")


def test_sse_replays_from_the_beginning_and_closes_on_the_terminal_event(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """MOS-API-061, MOS-API-062, MOS-API-063: `retry:` first, `id:` = gapless `seq` from
    1, `event:` = the registry type, terminal event last, then close."""
    job_id = submit(client).json()["job_id"]
    drive_to_rejected(pg_dsn, job_id)

    events, _comments, retry_lines = read_sse(client, f"/api/v1/jobs/{job_id}/events")
    assert retry_lines == ["retry: 3000"]
    ids = [e[0] for e in events]
    assert ids == list(range(1, len(ids) + 1))  # MOS-API-061: from 1, no gaps
    assert events[0][1] == "job.requested"
    assert events[-1][1] == "job.rejected"  # MOS-API-064: terminal, then closed
    envelope = events[-1][2]
    assert envelope["subject"] == {"kind": "job", "id": job_id}
    assert envelope["sequence"] == ids[-1]
    assert envelope["event_schema_version"] == 1
    assert envelope["data"]["state"] == "REJECTED"
    assert envelope["occurred_at"].endswith("Z")


def test_sse_resumes_from_last_event_id(db: Any, client: httpx.Client, pg_dsn: str) -> None:
    """CONTRACT.md section 9: "resumable via `Last-Event-ID` mapped to `seq`"."""
    job_id = submit(client).json()["job_id"]
    drive_to_rejected(pg_dsn, job_id)

    full, _c, _r = read_sse(client, f"/api/v1/jobs/{job_id}/events")
    cut = full[1][0]
    resumed, _c, _r = read_sse(
        client, f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": str(cut)}
    )
    assert [e[0] for e in resumed] == [e[0] for e in full if e[0] > cut]
    assert resumed[-1][1] == "job.rejected"


def test_sse_resume_past_the_terminal_event_closes_immediately(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """MOS-API-064: "a server receiving a reconnect for a terminal job MUST replay from
    `Last-Event-ID` and close immediately"."""
    job_id = submit(client).json()["job_id"]
    drive_to_rejected(pg_dsn, job_id)
    full, _c, _r = read_sse(client, f"/api/v1/jobs/{job_id}/events")
    last = full[-1][0]

    started = time.monotonic()
    events, _c, retry_lines = read_sse(
        client, f"/api/v1/jobs/{job_id}/events", headers={"Last-Event-ID": str(last)}
    )
    assert events == []
    assert retry_lines == ["retry: 3000"]
    assert time.monotonic() - started < 5.0


def test_sse_query_parameter_is_accepted_when_the_header_cannot_be_set(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """`EventSource` cannot set a request header on the initial connect, so a stream that
    is resumable only by header is not resumable from a browser."""
    job_id = submit(client).json()["job_id"]
    drive_to_rejected(pg_dsn, job_id)
    events, _c, _r = read_sse(client, f"/api/v1/jobs/{job_id}/events?last_event_id=2")
    assert [e[0] for e in events][0] == 3


def test_sse_rejects_a_malformed_last_event_id(db: Any, client: httpx.Client) -> None:
    """Refused rather than clamped: silently rewriting a resume cursor to 0 would replay
    a whole terminated job to a client that asked for three events."""
    job_id = submit(client).json()["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id}/events?last_event_id=banana")
    assert_problem(response, status=400, cls="client_error", code="INVALID_LAST_EVENT_ID")


def test_sse_streams_a_live_terminal_event(db: Any, client: httpx.Client, pg_dsn: str) -> None:
    """MOS-API-062: a client connected BEFORE the job terminates receives the terminal
    event on the open stream. This is the property that makes `POST` -> connect raceless."""
    job_id = submit(client).json()["job_id"]

    def worker() -> None:
        time.sleep(1.0)
        drive_to_rejected(pg_dsn, job_id)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        events, _c, _r = read_sse(client, f"/api/v1/jobs/{job_id}/events")
    finally:
        thread.join(timeout=20)
    assert events[0][1] == "job.requested"
    assert events[-1][1] == "job.rejected"


def test_sse_payload_carries_no_float_progress(
    db: Any, client: httpx.Client, pg_dsn: str
) -> None:
    """MOS-API-051 holds for events too: "The API MUST NOT expose any float `progress`
    field, on the `Job` or in any event"."""
    job_id = submit(client).json()["job_id"]
    drive_to_rejected(pg_dsn, job_id)
    events, _c, _r = read_sse(client, f"/api/v1/jobs/{job_id}/events")
    assert "progress" not in json.dumps([e[2] for e in events])


# =====================================================================================
# The error model as a whole
# =====================================================================================
def test_an_unhandled_exception_is_a_system_failure_with_no_internals(
    broken_url: str,
) -> None:
    """MOS-API-042: "A `system_failure` document MUST NOT include a stack trace, SQL text,
    internal hostname, file path, or any part of the request body. The `trace_id` is the
    entire diagnostic handoff."

    The driver error raised here escapes the handler unclassified, which is exactly the
    case the rule is about.
    """
    with httpx.Client(base_url=broken_url, timeout=10, headers=STUB_AUTH) as c:
        response = c.get("/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ")
    doc = assert_problem(response, status=500, cls="system_failure", code="INTERNAL_ERROR")
    text = json.dumps(doc)
    for leak in ("Traceback", "psycopg", "SELECT", ".py", "connection refused"):
        assert leak not in text, leak
    assert doc["retryable"] is False  # table 10.4-A: do not retry a bug


def test_a_removed_method_is_a_problem_document_not_an_html_page(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-035: "A bare string body, an HTML error page, or a `200` carrying
    `{"error": ...}` MUST NOT occur." Starlette's default 405 is a plain-text body; the
    handler must convert it."""
    response = client.request("DELETE", "/api/v1/jobs")
    assert response.status_code == 405
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    assert response.json()["class"] in WIRE_CLASSES


def test_every_problem_type_lives_under_the_one_authority(
    db: Any, client: httpx.Client
) -> None:
    """MOS-API-036: "`https://spec.medicalos.org/problems/` is the only permitted prefix.
    A `type` under any other host is a defect"."""
    responses = [
        client.get("/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ"),
        client.get("/api/v1/jobs/not-a-job-id"),
        client.post("/api/v1/jobs", json={"capabilities": []}),
        submit(client, headers={"Idempotency-Key": "bad key"}),
    ]
    for response in responses:
        assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE), response.text
        doc = response.json()
        assert doc["type"].startswith(PROBLEM_BASE), doc["type"]
        assert doc["class"] in WIRE_CLASSES
        assert doc["class"] != "clinical_rejection", (
            "MOS-API-047: a clinical rejection is never a synchronous error response"
        )


# =====================================================================================
# The MedosError branch of the handler chain
# =====================================================================================
@pytest.fixture(scope="session")
def raising_url() -> Iterator[str]:
    """A third independent app carrying one route per `medos.core.errors` class.

    `medos.api` itself never raises these -- a stored rejection arrives as a `jobs` row,
    not as an exception -- but `install_exception_handlers` registers the `MedosError`
    branch for the worker-facing paths that weeks 3-5 will add, and an untested handler
    is an unimplemented one. The app is built by `create_app`, so what is exercised is
    the real middleware and handler stack, not a hand-assembled one.
    """

    def refuse(**kwargs: Any) -> psycopg.Connection[Any]:  # pragma: no cover
        raise AssertionError("this app must not touch the database")

    app = create_app(
        connect=refuse, configure_logs=False, authenticator=_StubAuthenticator()
    )

    def raise_geometry() -> None:
        raise GeometryRejection(
            "geometry_non_uniform_spacing",
            {"series_instance_uid": SERIES, "spacing_mm": [2.5, 2.5, 5.0]},
            "Slice spacing varies by more than the tolerance.",
        )

    def raise_transport() -> None:
        raise TransportFailure(
            "gateway_timeout",
            {"dependency": "dicom-gateway", "budget_ms": 15000},
            "QIDO-RS series query exceeded its budget.",
        )

    app.add_api_route("/_probe/geometry", raise_geometry, methods=["GET"])
    app.add_api_route("/_probe/transport", raise_transport, methods=["GET"])
    with running_server(app) as url:
        yield url


def test_a_clinical_rejection_raised_in_a_handler_renders_as_422_clinical_rejection(
    raising_url: str,
) -> None:
    """`medos.core.errors` makes the classification a CLASS attribute, so the wire class
    is decided by which exception was raised (MOS-API-038). `GeometryRejection` is a
    `ClinicalRejection`, so it is `422` / `clinical_rejection` / not retryable."""
    with httpx.Client(base_url=raising_url, timeout=10, headers=STUB_AUTH) as c:
        response = c.get("/_probe/geometry")
    doc = assert_problem(
        response,
        status=422,
        cls="clinical_rejection",
        code="GEOMETRY_NON_UNIFORM_SPACING",
    )
    assert doc["retryable"] is False
    assert doc["type"] == PROBLEM_BASE + "geometry-non-uniform-spacing"
    # MOS-API-006: UIDs, counts and millimetres -- never a name, an MRN or a date.
    assert doc["detail_fields"]["series_instance_uid"] == SERIES
    assert doc["detail_fields"]["spacing_mm"] == [2.5, 2.5, 5.0]


def test_a_transport_failure_raised_in_a_handler_is_502_and_retryable(
    raising_url: str,
) -> None:
    """Table 10.4-A: `transport_failure` is `retryable: true`; the caller retries with
    backoff and the same `Idempotency-Key`. The contrast with the test above is the whole
    point of the enum -- one of these is a clinical answer and one is a broken hop."""
    with httpx.Client(base_url=raising_url, timeout=10, headers=STUB_AUTH) as c:
        response = c.get("/_probe/transport")
    doc = assert_problem(response, status=502, cls="transport_failure", code="GATEWAY_TIMEOUT")
    assert doc["retryable"] is True
    assert doc["detail_fields"]["dependency"] == "dicom-gateway"
