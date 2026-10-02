# SPDX-License-Identifier: Apache-2.0
"""Row access for jobs, steps, events, series verdicts and results.

CONTRACT.md section 1: "repo.py -- job/result/event row access".
CONTRACT.md section 11: "No global mutable state. Pass the connection; do not import a
singleton." Every function here takes `conn` first and none of them opens a connection.

This module is SQL and mapping, nothing else. It holds no business rules: the state
machine lives in `job_transition()` (MOS-EXEC-011 -- "stored as data and enforced by the
database, not by application code"), the retry decision lives in
`PostgresJobQueue.fail_ex()` (MOS-EXEC-030), and the step plan's ORDERING -- the fact
that `emphysema_laa` consumes `lung_segmentation`'s mask -- is `worker/steps.py`'s
(CONTRACT.md section 7). What is here is the "what rows does that produce" half.

Two transaction boundaries in this file are load-bearing and are the reason it exists at
all rather than being inlined into the API and the worker:

  `create_job_queued()`     the `jobs` INSERT, the `job_series` verdicts, the
                            `job_queue` INSERT and T2 in ONE transaction (MOS-EXEC-034).
  `complete_job_with_results()`
                            the `results` rows and T8 in ONE transaction
                            (CONTRACT.md section 8, MOS-EXEC-057, MOS-STORE-275).

Identity: every public function speaks `jobs.public_id` (`job_<ULID>`), never the
internal uuid (MOS-STORE-357). See the note in `queue.py`.

No PHI: CONTRACT.md section 11. Nothing in this module reads, writes or logs a
PatientName, PatientID, PatientBirthDate, AccessionNumber, StudyDate or
StudyDescription. The identifiers it handles are DICOM UIDs, which MOS-EXEC-079
explicitly permits.

Spec: MOS-EXEC-011, MOS-EXEC-020, MOS-EXEC-022, MOS-EXEC-023, MOS-EXEC-034,
MOS-EXEC-053, MOS-EXEC-054, MOS-EXEC-057, MOS-EXEC-078, MOS-STORE-270, MOS-STORE-271,
MOS-STORE-275, MOS-STORE-278, MOS-STORE-279, MOS-STORE-282, MOS-STORE-286,
MOS-STORE-357, MOS-SAFE-083, CONTRACT.md sections 8, 9 and 10.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.core.uids import derive_idempotency_key
from medos.db import audit
from medos.db.tenancy import tenant_tx

__all__ = [
    "SLICE_TENANT_ID",
    "STEP_PLAN",
    "JobSpec",
    "CreatedJob",
    "SeriesVerdict",
    "MeasurementRow",
    "DicomObjectRow",
    "ResultRow",
    "new_public_job_id",
    "new_trace_id",
    "create_job_queued",
    "get_job",
    "get_job_uuid",
    "find_job_by_idempotency_key",
    "job_view",
    "plan_steps",
    "start_step",
    "finish_step",
    "skip_step",
    "fail_step",
    "set_step_detail",
    "record_series_verdicts",
    "selected_series",
    "append_event",
    "list_events",
    "save_result",
    "complete_job_with_results",
    "list_results",
    "mark_dicom_stored",
]


# =====================================================================================
# The one derivation constant this slice has to invent, stated where it can be found
# =====================================================================================
# MOS-IMG-062's UID-derivation tuple and MOS-EXEC-053's idempotency-key material BOTH
# begin with `tenant_id`. There are no tenants in weeks 1-2 (CONTRACT.md section 0) and
# CONTRACT.md section 8 forbids a fake tenant_id COLUMN -- but the derivation functions
# take the value as an ARGUMENT, and something has to be passed.
#
# This is that value. It is a derivation constant, not a tenancy fiction:
#   - it never becomes a column, so the weeks 3-5 ADD COLUMN + RLS migration is unaffected;
#   - it is recorded verbatim in `result_dicom_objects.derivation_inputs` (MOS-STORE-286),
#     so every UID this deployment ever wrote stays recomputable;
#   - it is therefore FROZEN. MOS-IMG-085 makes "same UID implies same declared inputs"
#     an invariant the retry path depends on: changing this string re-identifies every
#     SEG and SR ever written by this deployment.
# CONTRACT.md section 8's "do not add a fake single-tenant value" is about the schema,
# and the schema has none. Reported as a contract addition.
SLICE_TENANT_ID = "00000000-0000-0000-0000-000000000000"

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


# MOS-EXEC-023. The eight reserved step keys with their canonical phase strings, owners
# and default timeouts, in plan order. "For a job with requested_outputs = {SEG, SR} the
# plan is these eight rows, so steps_total = 8."
STEP_PLAN: tuple[tuple[str, str, str, int], ...] = (
    ("fetch_series", "retrieving", "platform", 900),
    ("build_volume", "building_volume", "platform", 300),
    ("envelope_check", "checking_applicability", "platform", 30),
    ("service_invoke", "inferring", "service", 1800),
    ("validate_bundle", "validating_result", "platform", 60),
    ("write_dicom", "writing_dicom", "platform", 300),
    ("store_dicom", "storing_dicom", "platform", 600),
    ("persist_result", "persisting", "platform", 30),
)


# =====================================================================================
# Identifiers
# =====================================================================================
def new_public_job_id(*, now_ms: int | None = None) -> str:
    """`job_` + a 26-character Crockford base32 ULID in upper case (MOS-STORE-357).

    A ULID rather than a uuid4 because the public id is what an operator reads out of a
    log line and types into a URL, and a time-sortable id makes "the job from 09:12" a
    lexical range rather than a table scan. The 48-bit millisecond timestamp is not a
    clinical timestamp and MUST NOT be read as one -- `jobs.requested_at` is.

    26 characters is 130 bits of alphabet over 128 bits of value, so the leading
    character is always 0-7; the `^job_[0-9A-HJKMNP-TV-Z]{26}$` CHECK in schema.sql holds
    by construction.
    """
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    n = (ts << 80) | secrets.randbits(80)
    chars = [_ULID_ALPHABET[(n >> (5 * (25 - i))) & 0x1F] for i in range(26)]
    return "job_" + "".join(chars)


def new_trace_id() -> str:
    """A W3C trace-id: 32 lowercase hex characters, never all zero (MOS-EXEC-076).

    Separate from `correlation_id` and MUST NOT be conflated with it: the trace id groups
    the spans of one request, the correlation id groups the business activity, and an
    operator asking "what else happened to this study" needs the second.
    """
    while True:
        tid = secrets.token_hex(16)
        if set(tid) != {"0"}:
            return tid


# =====================================================================================
# Job creation
# =====================================================================================
@dataclass(frozen=True)
class JobSpec:
    """Everything `POST /api/v1/jobs` needs to create one job (CONTRACT.md section 9).

    The request body is `{"study_instance_uid": ..., "capabilities": [...]}`; everything
    else below is server-side. `request_idempotency_key` is the client's
    `Idempotency-Key` header and is stored for echo ONLY -- CONTRACT.md section 9: "That
    header MUST NOT feed UID derivation ... This was a register defect." It is absent
    from `idempotency_key`'s material by construction, because `derive_idempotency_key`
    is keyword-only over exactly the permitted fields (MOS-EXEC-054).
    """

    study_instance_uid: str
    capability_ids: tuple[str, ...]
    service_id: str = "medos.slice"
    service_version: str = "0.1.0"
    prior_study_instance_uids: tuple[str, ...] = ()
    requested_outputs: tuple[str, ...] = ("SEG", "SR")
    clinical_use_mode: str = "research_only"
    created_by_kind: str = "service_account"
    created_by_id: str = "medos-api"
    request_idempotency_key: str | None = None
    max_attempts: int = 5
    deadline_s: int = 6 * 3600  # chapter 5 section 5.6.2's `now() + interval '6 hours'`
    trace_id: str | None = None
    correlation_id: str | None = None
    # MOS-SEC-146: `request_id` "joins the authorization event to its effect event" and is
    # NOT NULL on `audit_events`. Threaded from the HTTP layer so that the `job.create`
    # audit row and the API request that caused it carry the same value; minted here when
    # the caller is not an HTTP request (a test, a triage worker) rather than left null,
    # because a nullable join key is a join that silently does not happen.
    request_id: str | None = None
    source_ip: str | None = None
    user_agent: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    def idempotency_key(self) -> str:
        """MOS-EXEC-053's material set, derived server-side.

        `selected_series_uids` is EMPTY here and that is a weeks 1-2 deviation worth
        stating: chapter 3 selects series at admission, so the key normally pins the
        chosen series. In this slice the pipeline starts at the DICOMweb pull
        (CONTRACT.md section 0) and selection happens inside `fetch_series`, so the key
        pins "this study, these capabilities, this service version" instead. Selection is
        deterministic given the study, so two requests that would select the same series
        still collide onto one job -- which is the property MOS-EXEC-053 exists for. What
        is temporarily lost is the ability to run the same service over a DIFFERENT
        series of the same study as a distinct job; nothing in this slice offers that.
        """
        return derive_idempotency_key(
            tenant_id=SLICE_TENANT_ID,
            service_id=self.service_id,
            service_version=self.service_version,
            study_instance_uid=self.study_instance_uid,
            selected_series_uids=(),
            prior_study_instance_uids=self.prior_study_instance_uids,
            requested_outputs=self.requested_outputs,
            parameters={"capability_ids": sorted(self.capability_ids), **self.parameters},
        )


def _creator_actor(spec: JobSpec) -> audit.Actor:
    """`jobs.created_by_kind` -> `MOS-SEC-146`'s closed `actor.kind` set.

    The two vocabularies are close but not identical and the mapping is written once,
    here, rather than guessed at each call site. `triage` is a `created_by_kind` and is
    NOT an actor kind: a study-arrival trigger is a platform process, so it becomes
    `workload`, which is exactly the kind chapter 12 section 12.13 says carries a null
    `source_ip` because it has no client address to record.
    """
    kind = {"user": "user", "service_account": "service_account", "triage": "workload"}[
        spec.created_by_kind
    ]
    return audit.Actor(
        kind=kind,
        id=spec.created_by_id,
        auth="api_key" if kind != "workload" else "internal",
    )


@dataclass(frozen=True)
class CreatedJob:
    job_id: str
    created: bool  # False => an existing job was returned (idempotent replay)
    state: str
    idempotency_key: str


@dataclass(frozen=True)
class SeriesVerdict:
    """One evaluated series. MOS-STORE-270: rows, never a jsonb blob on the job.

    `decision` is `selected` or `rejected` in the row AND on the wire -- there is no
    `accepted` spelling and a payload using one is the defect, not a synonym.
    `reason_code` for a rejected series comes from chapter 3's closed `SeriesRequirement`
    list (MOS-DATA-069), which is a DIFFERENT list from `jobs.reject_reason_code`'s.
    """

    series_instance_uid: str
    decision: str
    selector_name: str | None = None
    rank: int | None = None
    reason_code: str | None = None
    reason_detail: str | None = None
    instance_count: int | None = None
    modality: str | None = None


def create_job_queued(
    conn: psycopg.Connection[Any],
    queue: Any,  # medos.db.queue.JobQueue -- untyped here to keep the import one-way
    spec: JobSpec,
    *,
    series_verdicts: Sequence[SeriesVerdict] = (),
) -> CreatedJob:
    """Create the job and enqueue it in ONE transaction. MOS-EXEC-034.

    "The job row and the queue row MUST be inserted in one transaction. This is the whole
    reason driver 1 ships first: there is no dual write, so there is no outbox and no
    crash window in which a job exists but is invisible to workers, or is dispatched
    without a row."

    Idempotency (CONTRACT.md section 9, MOS-EXEC-053): `ON CONFLICT DO NOTHING` on
    `jobs_idempotency_uk`. Zero rows returned means the job already exists, and the
    caller returns the existing one and skips the enqueue -- exactly the comment chapter
    5 section 5.6.2 puts on that line. `created=False` is what lets the API answer `200`
    rather than `202` for a replay.

    NOTE the crash window this does NOT have: because the whole thing is one transaction,
    a process killed between the two INSERTs leaves no job at all, which is recoverable
    by retrying the request. The alternative -- a committed job with no queue row -- is
    invisible to every worker and needs a reconciler to find.
    """
    ik = spec.idempotency_key()
    public_id = new_public_job_id()
    trace_id = spec.trace_id or new_trace_id()
    correlation_id = spec.correlation_id or public_id

    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO jobs (
                public_id, service_id, service_version, capability_ids,
                clinical_use_mode, study_instance_uid, prior_study_instance_uids,
                requested_outputs, idempotency_key, request_idempotency_key,
                created_by_kind, created_by_id, deadline_at, max_attempts,
                trace_id, correlation_id, root_job_id
            )
            VALUES (
                %(public_id)s, %(service_id)s, %(service_version)s, %(capability_ids)s,
                %(clinical_use_mode)s, %(study_uid)s, %(priors)s,
                %(outputs)s, %(ik)s, %(req_ik)s,
                %(by_kind)s, %(by_id)s, now() + make_interval(secs => %(deadline_s)s),
                %(max_attempts)s, %(trace_id)s, %(correlation_id)s,
                -- root_job_id is NOT NULL and self-referential for a depth-0 job
                -- (MOS-EXEC-084); the DEFAULT cannot see the row's own id, so it is
                -- patched immediately below inside this same transaction.
                gen_random_uuid()
            )
            ON CONFLICT ON CONSTRAINT jobs_idempotency_uk DO NOTHING
            RETURNING id, public_id, state
            """,
            {
                "public_id": public_id,
                "service_id": spec.service_id,
                "service_version": spec.service_version,
                "capability_ids": list(spec.capability_ids),
                "clinical_use_mode": spec.clinical_use_mode,
                "study_uid": spec.study_instance_uid,
                "priors": list(spec.prior_study_instance_uids),
                "outputs": list(spec.requested_outputs),
                "ik": ik,
                "req_ik": spec.request_idempotency_key,
                "by_kind": spec.created_by_kind,
                "by_id": spec.created_by_id,
                "deadline_s": spec.deadline_s,
                "max_attempts": spec.max_attempts,
                "trace_id": trace_id,
                "correlation_id": correlation_id,
            },
        ).fetchone()

        if row is None:
            existing = conn.execute(
                "SELECT public_id, state FROM jobs WHERE idempotency_key = %s", (ik,)
            ).fetchone()
            if existing is None:  # pragma: no cover -- only reachable on a torn delete
                raise RuntimeError(
                    "jobs INSERT conflicted but no row carries the key; "
                    "jobs_idempotency_uk is not doing what it says"
                )
            return CreatedJob(existing["public_id"], False, existing["state"], ik)

        conn.execute("UPDATE jobs SET root_job_id = id WHERE id = %s", (row["id"],))

        conn.execute(
            "SELECT job_append_event(%s, 'job.requested', 'control-plane', %s)",
            (
                row["id"],
                Jsonb(
                    {
                        "study_instance_uid": spec.study_instance_uid,
                        "capability_ids": list(spec.capability_ids),
                        "service_id": spec.service_id,
                        "service_version": spec.service_version,
                        "requested_outputs": list(spec.requested_outputs),
                        "clinical_use_mode": spec.clinical_use_mode,
                    }
                ),
            ),
        )

        # MOS-STORE-270: the per-series verdicts go in "this same transaction". Empty in
        # this slice at creation time -- selection happens in `fetch_series` -- but the
        # parameter exists so a chapter 3 triage worker can fill it without a schema
        # change, and `record_series_verdicts` writes the same rows later.
        if series_verdicts:
            _insert_series_verdicts(conn, row["id"], series_verdicts)

        # MOS-SEC-149: "Where the audited action occurs inside a database transaction, the
        # AuditEvent MUST be inserted in that same transaction. A committed state change
        # with no audit row MUST NOT be possible." The job row, the queue row and this
        # audit row are one transaction, so "a job exists that nobody asked for" is not a
        # reachable state.
        #
        # MOS-SEC-147: the actor is FOUR fields and the resource is the P1 `study_ref`, not
        # the raw StudyInstanceUID -- MOS-SEC-106 forbids a P2 identifier on a surface the
        # matrix does not permit it on, and an audit export is such a surface. The raw UID
        # travels in the row's own column, which chapter 12 section 12.13 declares.
        audit.record(
            conn,
            action="job.create",
            action_class="clinical",  # MOS-SEC-148: mandatory on both allow and deny
            actor=_creator_actor(spec),
            resource=audit.Resource.study(SLICE_TENANT_ID, spec.study_instance_uid),
            outcome="allow",
            pep="dispatch.job",
            trace_id=trace_id,
            request_id=spec.request_id or audit.new_request_id(),
            job_id=row["public_id"],
            study_instance_uid=spec.study_instance_uid,
            source_ip=spec.source_ip,
            user_agent=spec.user_agent,
            detail={
                "capability_ids": sorted(spec.capability_ids),
                "service_id": spec.service_id,
                "service_version": spec.service_version,
                "requested_outputs": sorted(spec.requested_outputs),
                "clinical_use_mode": spec.clinical_use_mode,
                "idempotency_key": ik,
            },
        )

        # The enqueue joins THIS transaction (it never opens one of its own).
        queue.enqueue(row["public_id"])

        state = conn.execute(
            "SELECT state FROM jobs WHERE id = %s", (row["id"],)
        ).fetchone()
        assert state is not None
        return CreatedJob(row["public_id"], True, state["state"], ik)


# =====================================================================================
# Reads
# =====================================================================================
def get_job(conn: psycopg.Connection[Any], job_id: str) -> dict[str, Any] | None:
    """The whole `jobs` row, keyed by public id. MOS-EXEC-002: read the table, never a
    projection.

    Note what is NOT in the WHERE clause: `tenant_id`. MOS-SEC-076 is explicit -- "a query
    MUST NOT add `WHERE tenant_id = $1` as its isolation mechanism. RLS is the mechanism;
    an explicit predicate is permitted only as an index hint and MUST NOT be the only
    barrier." The isolation is the policy `jobs_tenant_isolation`, applied by PostgreSQL
    to this statement because `tenant_tx()` bound the transaction. A job belonging to
    another tenant returns None here, and it returns None for the same reason a job that
    does not exist does.
    """
    with tenant_tx(conn):
        return conn.execute(
            "SELECT * FROM jobs WHERE public_id = %s", (job_id,)
        ).fetchone()


def get_job_uuid(conn: psycopg.Connection[Any], job_id: str) -> Any:
    """public id -> internal uuid. The only place outside `medos.db` should ever need
    this is nowhere (MOS-STORE-357)."""
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT id FROM jobs WHERE public_id = %s", (job_id,)
        ).fetchone()
    return None if row is None else row["id"]


def find_job_by_idempotency_key(
    conn: psycopg.Connection[Any], idempotency_key: str
) -> dict[str, Any] | None:
    with tenant_tx(conn):
        return conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key = %s", (idempotency_key,)
        ).fetchone()


def job_view(conn: psycopg.Connection[Any], job_id: str) -> dict[str, Any] | None:
    """The `GET /api/v1/jobs/{job_id}` body (CONTRACT.md section 9).

    "job with `state`, `phase`, `steps_completed`, `steps_total`, `rejection` (when
    `REJECTED`), `results[]`, `provenance`".

    MOS-EXEC-021: `steps_completed`/`steps_total` and no float `progress`. A client that
    wants a bar computes the ratio itself and renders a step counter while
    `steps_total = 0`.

    `rejection` is an RFC 9457 problem document of class `clinical_rejection`
    (MOS-EXEC-016, `MOS-API-040`); the HTTP layer serialises it and adds nothing, because
    CONTRACT.md section 0 requires the handler to stay thin.
    """
    job = get_job(conn, job_id)
    if job is None:
        return None

    results = list_results(conn, job_id)
    # One scope for the two remaining reads. `get_job` and `list_results` opened their
    # own; nesting is a SAVEPOINT, so this is still one transaction per request.
    with tenant_tx(conn):
        steps_rows = conn.execute(
            """
            SELECT step_index, step_key, phase, owner, status, skip_reason, attempt,
                   started_at, finished_at, duration_ms, error_code, detail
              FROM job_steps WHERE job_id = %s ORDER BY step_index
            """,
            (job["id"],),
        ).fetchall()
        series_rows = conn.execute(
            """
            SELECT series_instance_uid, decision, selector_name, rank,
                   reason_code, reason_detail, instance_count, modality
              FROM job_series WHERE job_id = %s
             ORDER BY decision, rank NULLS LAST, series_instance_uid
            """,
            (job["id"],),
        ).fetchall()
    audit_rows = audit.trail_for_job(conn, job_id)
    view: dict[str, Any] = {
        "job_id": job["public_id"],
        "state": job["state"],
        "phase": job["phase"],
        "steps_completed": job["steps_completed"],
        "steps_total": job["steps_total"],
        "study_instance_uid": job["study_instance_uid"],
        "capability_ids": list(job["capability_ids"]),
        "requested_outputs": list(job["requested_outputs"]),
        "clinical_use_mode": job["clinical_use_mode"],
        "attempt": job["attempt"],
        "max_attempts": job["max_attempts"],
        "requested_at": job["requested_at"],
        "queued_at": job["queued_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "results": results,
        "steps": steps_rows,
        "series_selection": series_rows,
        # CONTRACT.md section 10: the job-level half of provenance. The per-result half
        # is on each `results` entry. Both are surfaced because the OHIF panel renders
        # "what ran, on what, and what did it write".
        "provenance": {
            "job_id": job["public_id"],
            "study_instance_uid": job["study_instance_uid"],
            "prior_study_instance_uids": list(job["prior_study_instance_uids"]),
            "series_consumed": list(job["selected_series_uids"]),
            "service_id": job["service_id"],
            "service_version": job["service_version"],
            "trace_id": job["trace_id"],
            "correlation_id": job["correlation_id"],
            "idempotency_key": job["idempotency_key"],
            "requested_at": job["requested_at"],
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
        },
        # MOS-SEC-155: "AuditEvent -> Job by job_id", as a surface rather than as a SQL
        # snippet in a runbook. This is the half of the answer to "who caused this
        # patient's images to be read" that a reviewer holding a job id can actually
        # reach: actor, delegation, action, outcome and the counts, in chain order.
        #
        # PHI (CONTRACT.md section 11): `resource_id` here is the P1 `study_ref`
        # (MOS-SEC-106), never the raw StudyInstanceUID, and `detail` is P0/P1 by
        # MOS-SEC-146.
        "audit_trail": audit_rows,
    }

    if job["state"] == "REJECTED":
        # MOS-EXEC-016: surfaced as `clinical_rejection`, and MUST be rendered with
        # non-error affordances in every UI. MOS-EXEC-014 is why: "a radiologist who sees
        # a red error where the truth is 'no thin axial recon exists in this study' will
        # either chase an IT ticket or, worse, assume the study was cleared."
        view["rejection"] = {
            "type": f"urn:medos:problem:{job['reject_reason_code']}",
            "title": job["reject_reason_detail"] or job["reject_reason_code"],
            "status": 422,
            "class": "clinical_rejection",
            "code": (job["reject_reason_code"] or "").upper(),
            "reason_code": job["reject_reason_code"],
            "retryable": False,
            "series_selection_href": f"/api/v1/jobs/{job['public_id']}/series-selection",
        }
    if job["state"] == "FAILED":
        # MOS-EXEC-016a's fixed map from the twelve internal classes onto the two wire
        # classes. The internal class names never appear on the wire.
        transport = {
            "transient_infrastructure",
            "gateway_unavailable",
            "service_unavailable",
            "service_crashed",
            "lease_expired",
            "dicom_store_failed",
            "load_shed",
        }
        cls = "transport_failure" if job["failure_class"] in transport else "system_failure"
        view["error"] = {
            "type": f"urn:medos:problem:{job['failure_code']}",
            "title": job["failure_detail"] or job["failure_code"],
            "status": 502 if cls == "transport_failure" else 500,
            "class": cls,
            "code": (job["failure_code"] or "").upper(),
            "retryable": cls == "transport_failure",
        }
    return view


# =====================================================================================
# Step plan  (chapter 5 section 5.4)
# =====================================================================================
def plan_steps(
    conn: psycopg.Connection[Any],
    job_id: str,
    *,
    attempt: int,
    plan: Sequence[tuple[str, str, str, int]] = STEP_PLAN,
) -> int:
    """Write the step plan. MOS-EXEC-022: immutable for the life of an attempt.

    "On a retry, a fresh plan is written with the same `step_key` set; `job_steps` rows
    are replaced, `attempt` on each row is set to the job's current attempt." Hence the
    DELETE: the previous attempt's rows are not history to preserve here -- the
    append-only `job_events` stream is the history -- and leaving them would make
    `steps_completed` count work from an attempt that no longer applies.

    Returns `steps_total`. The rollup trigger sets it on `jobs`; the return value is the
    same number, read back, so a caller can assert rather than trust.
    """
    uid = get_job_uuid(conn, job_id)
    if uid is None:
        raise LookupError(f"no such job: {job_id}")
    with tenant_tx(conn):
        conn.execute("DELETE FROM job_steps WHERE job_id = %s", (uid,))
        for index, (step_key, phase, owner, timeout_s) in enumerate(plan):
            conn.execute(
                """
                INSERT INTO job_steps
                       (job_id, step_index, step_key, phase, owner, timeout_s, attempt)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (uid, index, step_key, phase, owner, timeout_s, attempt),
            )
        row = conn.execute("SELECT steps_total FROM jobs WHERE id = %s", (uid,)).fetchone()
    assert row is not None
    return int(row["steps_total"])


def start_step(conn: psycopg.Connection[Any], job_id: str, step_key: str) -> None:
    """`pending` -> `running`. The rollup trigger moves `jobs.phase` to this step's."""
    _step_update(
        conn,
        job_id,
        step_key,
        "UPDATE job_steps SET status = 'running', started_at = now()"
        " WHERE job_id = %s AND step_key = %s",
    )
    _emit_step_event(conn, job_id, step_key, "running")


def finish_step(
    conn: psycopg.Connection[Any],
    job_id: str,
    step_key: str,
    *,
    detail: dict[str, Any] | None = None,
) -> None:
    """`running` -> `succeeded`, with `duration_ms` measured by the database clock."""
    _step_update(
        conn,
        job_id,
        step_key,
        """
        UPDATE job_steps
           SET status = 'succeeded', finished_at = now(),
               duration_ms = (EXTRACT(EPOCH FROM (now() - coalesce(started_at, now())))
                              * 1000)::integer,
               detail = coalesce(%s::jsonb, detail)
         WHERE job_id = %s AND step_key = %s
        """,
        extra=(Jsonb(detail) if detail is not None else None,),
        extra_first=True,
    )
    _emit_step_event(conn, job_id, step_key, "succeeded")


def skip_step(conn: psycopg.Connection[Any], job_id: str, step_key: str, reason: str) -> None:
    """`skipped`, with a reason. MOS-EXEC-020 binds the two: the CHECK
    `(status = 'skipped') = (skip_reason IS NOT NULL)` makes a reasonless skip
    unstorable. MOS-EXEC-059's case is `write_dicom`/`store_dicom` when the derived
    series already exists -- which is exactly the retry path deterministic UIDs buy."""
    if not reason:
        raise ValueError("a skipped step MUST carry a reason (MOS-EXEC-020)")
    _step_update(
        conn,
        job_id,
        step_key,
        "UPDATE job_steps SET status = 'skipped', skip_reason = %s, finished_at = now()"
        " WHERE job_id = %s AND step_key = %s",
        extra=(reason,),
        extra_first=True,
    )
    _emit_step_event(conn, job_id, step_key, "skipped", {"skip_reason": reason})


def fail_step(
    conn: psycopg.Connection[Any],
    job_id: str,
    step_key: str,
    *,
    error_code: str,
    error_detail: str = "",
) -> None:
    """`failed`, with a code. The CHECK forbids a failed step with no `error_code`; the
    detail must be PHI-safe (MOS-EXEC-017)."""
    _step_update(
        conn,
        job_id,
        step_key,
        """
        UPDATE job_steps
           SET status = 'failed', error_code = %s, error_detail = %s, finished_at = now(),
               duration_ms = (EXTRACT(EPOCH FROM (now() - coalesce(started_at, now())))
                              * 1000)::integer
         WHERE job_id = %s AND step_key = %s
        """,
        extra=(error_code, error_detail or None),
        extra_first=True,
    )
    _emit_step_event(conn, job_id, step_key, "failed", {"error_code": error_code})


def set_step_detail(
    conn: psycopg.Connection[Any], job_id: str, step_key: str, detail: dict[str, Any]
) -> None:
    """MOS-EXEC-024: a service's sub-progress bag, for the `service_invoke` row only.

    "It MUST NOT change `steps_total` or `steps_completed`. The job-level counters stay
    platform-owned so that a misbehaving vendor cannot fabricate platform progress." This
    writes `detail` and nothing else, which is the structural form of that sentence.
    """
    if step_key != "service_invoke":
        raise ValueError(
            f"MOS-EXEC-024 permits a sub-progress bag on service_invoke only, not "
            f"{step_key!r}"
        )
    _step_update(
        conn,
        job_id,
        step_key,
        "UPDATE job_steps SET detail = %s WHERE job_id = %s AND step_key = %s",
        extra=(Jsonb(detail),),
        extra_first=True,
    )


def _step_update(
    conn: psycopg.Connection[Any],
    job_id: str,
    step_key: str,
    sql: str,
    *,
    extra: tuple[Any, ...] = (),
    extra_first: bool = False,
) -> None:
    uid = get_job_uuid(conn, job_id)
    if uid is None:
        raise LookupError(f"no such job: {job_id}")
    params = (*extra, uid, step_key) if extra_first else (uid, step_key, *extra)
    with tenant_tx(conn):
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            # MOS-EXEC-022: a runner that touches a step it did not plan is a
            # `step_plan_violation`, not a row to create on the fly.
            raise LookupError(
                f"job {job_id} has no planned step {step_key!r} "
                "(MOS-EXEC-022: the plan is immutable for the life of an attempt)"
            )


def _emit_step_event(
    conn: psycopg.Connection[Any],
    job_id: str,
    step_key: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> None:
    uid = get_job_uuid(conn, job_id)
    payload = {"step_key": step_key, "status": status, **(extra or {})}
    with tenant_tx(conn):
        conn.execute(
            "SELECT job_append_event(%s, 'job.step_changed', 'job-runner', %s)",
            (uid, Jsonb(payload)),
        )


# =====================================================================================
# Series verdicts  (MOS-STORE-270)
# =====================================================================================
def record_series_verdicts(
    conn: psycopg.Connection[Any], job_id: str, verdicts: Sequence[SeriesVerdict]
) -> None:
    """Write one row per EVALUATED series and denormalise the selected set onto the job.

    MOS-STORE-270: "Selection results, including the reason every rejected series was
    rejected, are first-class rows in `job_series` -- not a log line and not a `jsonb`
    blob; a job whose selection produced no `selected` row MUST be `REJECTED` with
    `reject_reason_code` set."

    This function does not itself reject the job -- that is the runner's T7, because the
    runner is the actor the transition table permits. What it guarantees is that the
    evidence for that decision is on disk before the decision is taken.
    """
    uid = get_job_uuid(conn, job_id)
    if uid is None:
        raise LookupError(f"no such job: {job_id}")
    with tenant_tx(conn):
        _insert_series_verdicts(conn, uid, verdicts)
        selected = [v.series_instance_uid for v in verdicts if v.decision == "selected"]
        conn.execute(
            "UPDATE jobs SET selected_series_uids = %s WHERE id = %s", (selected, uid)
        )


def _insert_series_verdicts(
    conn: psycopg.Connection[Any], job_uuid: Any, verdicts: Iterable[SeriesVerdict]
) -> None:
    for v in verdicts:
        conn.execute(
            """
            INSERT INTO job_series (job_id, series_instance_uid, decision, selector_name,
                                    rank, reason_code, reason_detail, instance_count,
                                    modality)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id, series_instance_uid) DO UPDATE
              SET decision = EXCLUDED.decision, selector_name = EXCLUDED.selector_name,
                  rank = EXCLUDED.rank, reason_code = EXCLUDED.reason_code,
                  reason_detail = EXCLUDED.reason_detail,
                  instance_count = EXCLUDED.instance_count, modality = EXCLUDED.modality
            """,
            (
                job_uuid,
                v.series_instance_uid,
                v.decision,
                v.selector_name,
                v.rank,
                v.reason_code,
                v.reason_detail,
                v.instance_count,
                v.modality,
            ),
        )


def selected_series(conn: psycopg.Connection[Any], job_id: str) -> list[dict[str, Any]]:
    uid = get_job_uuid(conn, job_id)
    with tenant_tx(conn):
        return conn.execute(
            "SELECT * FROM job_series WHERE job_id = %s AND decision = 'selected' "
            "ORDER BY rank",
            (uid,),
        ).fetchall()


# =====================================================================================
# Events  (MOS-EXEC-013, MOS-STORE-271)
# =====================================================================================
def append_event(
    conn: psycopg.Connection[Any],
    job_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    actor: str = "job-runner",
    phase: str | None = None,
) -> int:
    """Append one non-transition event. Returns the assigned `seq`.

    State-change events are NOT appended here: `job_transition()` writes them, in the
    same transaction as the state change (MOS-STORE-271), and duplicating that here would
    let the two drift. `job_append_event()` takes the same `FOR UPDATE` lock on the jobs
    row, so `seq` stays monotonic across both producers.
    """
    if not payload:
        raise ValueError("MOS-EXEC-077: an event payload MUST have at least one property")
    uid = get_job_uuid(conn, job_id)
    if uid is None:
        raise LookupError(f"no such job: {job_id}")
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT job_append_event(%s, %s, %s, %s, %s) AS seq",
            (uid, event_type, actor, Jsonb(payload), phase),
        ).fetchone()
    assert row is not None
    return int(row["seq"])


def list_events(
    conn: psycopg.Connection[Any], job_id: str, *, after_seq: int = 0, limit: int = 500
) -> list[dict[str, Any]]:
    """The SSE backing read (CONTRACT.md section 9).

    `after_seq` is `Last-Event-ID` mapped to `seq` -- MOS-EXEC-078 makes `seq` the
    ordering mechanism precisely so a resumed stream cannot replay or skip, and
    MOS-STORE-271 requires consumers to order by `(job_id, seq)` and discard anything
    lower than already applied.
    """
    uid = get_job_uuid(conn, job_id)
    if uid is None:
        raise LookupError(f"no such job: {job_id}")
    with tenant_tx(conn):
        return conn.execute(
            """
            SELECT seq, event_id, event_type, from_state, to_state, phase, actor, payload,
                   occurred_at
              FROM job_events
             WHERE job_id = %s AND seq > %s
             ORDER BY seq
             LIMIT %s
            """,
            (uid, after_seq, limit),
        ).fetchall()


# =====================================================================================
# Results  (CONTRACT.md sections 8 and 10)
# =====================================================================================
@dataclass(frozen=True)
class MeasurementRow:
    """One `result_measurements` row. MOS-STORE-282: scheme, code and unit NOT NULL.

    The concept fields are the flattened `medos.core.bundle.CodedConcept`; CONTRACT.md
    section 5 spells its third member `meaning`, chapter 2 spells it `display`, and
    `concept_display` is where it lands. There is no third spelling.
    """

    concept_scheme: str
    concept_code: str
    concept_display: str
    value: float
    ucum_unit: str
    source_series_instance_uid: str
    finding_index: int | None = None
    geometry_space: str = "source"
    derivation_scheme: str | None = None
    derivation_code: str | None = None
    method_detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DicomObjectRow:
    """One written DICOM object. CONTRACT.md section 10 requires the generated
    `SeriesInstanceUID` and `SOPInstanceUID` per object; MOS-STORE-286 requires the tuple
    they were derived from, so a retry recomputes rather than re-derives."""

    object_kind: str  # SEG | SR | SC | PR
    sop_class_uid: str
    series_instance_uid: str
    sop_instance_uid: str
    series_number: int
    output_index: int
    derivation_inputs: dict[str, Any]
    research_marked: bool = True
    frame_count: int | None = None
    object_digest: str | None = None
    size_bytes: int | None = None
    pacs_backend: str = "orthanc"


@dataclass(frozen=True)
class ResultRow:
    """One `results` row plus its children, as one capability's whole answer.

    One of these per `(job_id, capability_id)` in the bundle (MOS-STORE-275a); the
    `UNIQUE (job_id, capability_id)` constraint of CONTRACT.md section 8 makes a
    duplicate impossible at the database level.
    """

    capability_id: str
    capability_version: str
    result_kind: str
    findings: list[dict[str, Any]]
    input_series_uids: tuple[str, ...]
    input_instance_uids: tuple[str, ...]
    preprocessing_version: str
    worker_version: str
    runtime_version: str
    measurements: tuple[MeasurementRow, ...] = ()
    dicom_objects: tuple[DicomObjectRow, ...] = ()
    # MOS-SAFE-082: "Exactly one provenance record MUST be written per Result, in the
    # SAME database transaction as the Result row and the terminal Job transition."
    #
    # The full MOS-SAFE-083 document, built by `medos.worker.result_rows.provenance_record`
    # and written by `save_result` one statement after the `results` INSERT. `result_id`
    # is the single member the builder cannot fill -- it does not exist until that INSERT
    # returns -- and it is the only one `save_result` writes into the document.
    #
    # `None` means NO record, which `save_result` treats as a defect rather than as an
    # option: a result observable through the API without provenance is what
    # MOS-STORE-278 forbids. It defaults to None only so that the ~40 weeks 1-2 fixtures
    # that construct a `ResultRow` by hand keep compiling; they go down the documented
    # legacy path and are named in `save_result`.
    provenance: dict[str, Any] | None = None
    geometry: dict[str, Any] = field(default_factory=dict)
    clinical_use_mode: str = "research_only"
    plausibility_state: str = "ok"
    out_of_distribution: bool = False
    input_pixel_digest: str | None = None
    preprocessing_digest: str | None = None
    platform_commit: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _uid_digest(sop_instance_uids: Sequence[str]) -> str:
    """MOS-SAFE-083 section B: sha256 over the SORTED SOP UID list.

    Sorted, so the digest is a property of the SET and not of the order the retrieval
    happened to return -- two attempts over the same instances must produce the same
    digest or the provenance check in MOS-STORE-279 fires on every retry.
    """
    joined = "\n".join(sorted(sop_instance_uids)).encode("ascii")
    return hashlib.sha256(joined).hexdigest()


def save_result(
    conn: psycopg.Connection[Any],
    job_id: str,
    result: ResultRow,
    *,
    study_instance_uid: str | None = None,
) -> str:
    """Insert one `results` row with its measurements and DICOM objects.

    MOS-STORE-275: "a retry that reaches the result stage again finds the constraint and
    MUST treat the violation as success, not as an error." So the insert is
    `ON CONFLICT (job_id, capability_id) DO NOTHING` and the existing id is returned.
    Deterministic UIDs (MOS-IMG-062) are what make that safe: the second attempt would
    have written the same SOPInstanceUIDs.

    Does NOT open its own transaction beyond the savepoint -- `complete_job_with_results`
    is the caller that binds it to T8 (CONTRACT.md section 8).
    """
    uid = get_job_uuid(conn, job_id)
    if uid is None:
        raise LookupError(f"no such job: {job_id}")
    if study_instance_uid is None:
        job = get_job(conn, job_id)
        assert job is not None
        study_instance_uid = job["study_instance_uid"]

    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO results (
                job_id, capability_id, capability_version, result_kind,
                clinical_use_mode, plausibility_state, out_of_distribution, findings,
                study_instance_uid, input_series_uids, input_instance_uids,
                input_instance_count, input_uid_digest, input_pixel_digest,
                preprocessing_version, preprocessing_digest, worker_version,
                runtime_version, platform_commit, geometry, started_at, finished_at)
            VALUES (%(job)s, %(cap)s, %(capv)s, %(kind)s,
                    %(mode)s, %(plaus)s, %(ood)s, %(findings)s,
                    %(study)s, %(series)s, %(instances)s,
                    %(n)s, %(digest)s, %(pixdigest)s,
                    %(prep)s, %(prepdig)s, %(worker)s,
                    %(runtime)s, %(commit)s, %(geom)s, %(started)s, %(finished)s)
            ON CONFLICT ON CONSTRAINT results_job_capability_uk DO NOTHING
            RETURNING id
            """,
            {
                "job": uid,
                "cap": result.capability_id,
                "capv": result.capability_version,
                "kind": result.result_kind,
                "mode": result.clinical_use_mode,
                "plaus": result.plausibility_state,
                "ood": result.out_of_distribution,
                "findings": Jsonb(result.findings),
                "study": study_instance_uid,
                "series": list(result.input_series_uids),
                "instances": list(result.input_instance_uids),
                "n": len(result.input_instance_uids),
                "digest": _uid_digest(result.input_instance_uids),
                "pixdigest": result.input_pixel_digest,
                "prep": result.preprocessing_version,
                "prepdig": result.preprocessing_digest,
                "worker": result.worker_version,
                "runtime": result.runtime_version,
                "commit": result.platform_commit,
                "geom": Jsonb(result.geometry),
                "started": result.started_at,
                "finished": result.finished_at,
            },
        ).fetchone()

        if row is None:
            existing = conn.execute(
                "SELECT id FROM results WHERE job_id = %s AND capability_id = %s",
                (uid, result.capability_id),
            ).fetchone()
            assert existing is not None
            return str(existing["id"])

        result_id = row["id"]
        for i, m in enumerate(result.measurements):
            conn.execute(
                """
                INSERT INTO result_measurements (
                    result_id, capability_id, finding_index, measurement_index,
                    concept_scheme, concept_code, concept_display, value, ucum_unit,
                    derivation_scheme, derivation_code, geometry_space,
                    source_series_instance_uid, method_detail)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    result_id,
                    result.capability_id,
                    m.finding_index,
                    i,
                    m.concept_scheme,
                    m.concept_code,
                    m.concept_display,
                    m.value,
                    m.ucum_unit,
                    m.derivation_scheme,
                    m.derivation_code,
                    m.geometry_space,
                    m.source_series_instance_uid,
                    Jsonb(m.method_detail),
                ),
            )
        for obj in result.dicom_objects:
            conn.execute(
                """
                INSERT INTO result_dicom_objects (
                    result_id, object_kind, sop_class_uid, series_instance_uid,
                    sop_instance_uid, series_number, output_index, derivation_inputs,
                    frame_count, clinical_use_mode, research_marked, object_digest,
                    size_bytes, pacs_backend)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    result_id,
                    obj.object_kind,
                    obj.sop_class_uid,
                    obj.series_instance_uid,
                    obj.sop_instance_uid,
                    obj.series_number,
                    obj.output_index,
                    Jsonb(obj.derivation_inputs),
                    obj.frame_count,
                    result.clinical_use_mode,
                    obj.research_marked,
                    obj.object_digest,
                    obj.size_bytes,
                    obj.pacs_backend,
                ),
            )

        # MOS-SAFE-082, and it is inside the `with tenant_tx(conn)` above on purpose:
        # "Exactly one provenance record MUST be written per Result, in the SAME database
        # transaction as the Result row and the terminal Job transition. A Result without
        # a provenance record MUST be impossible by FOREIGN-KEY constraint, not by
        # convention. If the record cannot be written, the Job MUST fail."
        #
        # So this raises rather than logs. `result_provenance` has a NOT NULL composite FK
        # to `results`, so the only way a result could exist without one is if this line
        # were skipped -- which is why the skip has to be loud.
        if result.provenance is not None:
            from medos.db import provenance as provenance_db

            record = {**result.provenance, "result_id": str(result_id)}
            provenance_db.save(
                conn,
                result_id=str(result_id),
                job_uuid=uid,
                root_job_uuid=(get_job(conn, job_id) or {}).get("root_job_id") or uid,
                record=record,
                capability_id=result.capability_id,
                capability_version=result.capability_version,
                service_id=str(record["execution"]["service_id"]),
                service_version=str(record["execution"]["service_version"]),
                worker_version=result.worker_version,
                runtime_version=result.runtime_version,
                clinical_use_mode=result.clinical_use_mode,
                input_series_uids=result.input_series_uids,
                input_instance_uids=result.input_instance_uids,
                input_uid_digest=_uid_digest(result.input_instance_uids),
                input_pixel_digest=str(record["input"]["pixel_digest"]),
                platform_commit=result.platform_commit,
            )
        return str(result_id)


def complete_job_with_results(
    conn: psycopg.Connection[Any],
    queue: Any,  # medos.db.queue.JobQueue
    job_id: str,
    worker_id: str,
    results: Sequence[ResultRow],
) -> list[str]:
    """The commit point. CONTRACT.md section 8, NON-NEGOTIABLE:

        "the results row and the terminal state transition MUST be written in ONE
         transaction"

    MOS-EXEC-057 and MOS-STORE-275 say the same from the execution and storage sides.
    MOS-STORE-276 adds why it matters operationally: "`StoreResults` is the pipeline's
    commit point and its last mutating step, and recovery past it is forward-only; with
    deterministic generated UIDs this removes the need for any compensating action --
    which matters, because platform policy denies deleting a study."

    So: one `with conn.transaction()`, the result rows, `queue.complete()` (which does
    the fenced T8 and deletes the queue row), and nothing between them that can fail
    without taking the whole thing down.
    """
    if not results:
        raise ValueError(
            "a COMPLETED job must carry at least one result row; a job with nothing to "
            "report is REJECTED with a reason (CONTRACT.md section 3), not COMPLETED"
        )
    consumed: list[str] = []
    for r in results:
        consumed.extend(r.input_series_uids)

    ids: list[str] = []
    with tenant_tx(conn):
        uid = get_job_uuid(conn, job_id)
        if uid is None:
            raise LookupError(f"no such job: {job_id}")
        # jobs_completed_consumed_series: a COMPLETED job must record what it consumed.
        conn.execute(
            """
            UPDATE jobs
               SET selected_series_uids = CASE
                     WHEN cardinality(selected_series_uids) > 0 THEN selected_series_uids
                     ELSE %s END
             WHERE id = %s
            """,
            (sorted(set(consumed)), uid),
        )
        job = get_job(conn, job_id)
        assert job is not None
        for r in results:
            ids.append(save_result(conn, job_id, r))
            # MOS-SEC-147, the whole point of the field set, in one call:
            #
            #     "actor + on_behalf_of MUST be able to express `pulmo.effusion@2.1.0`
            #      acting for `u_7d2f4a` in `job_01JB8N...`"
            #
            # The actor is the capability that produced the numbers -- a `service_version`
            # -- and NOT the worker process, because "which process wrote this" is not an
            # access question and "which model, on whose authority, in which execution"
            # is. `OnBehalfOf.from_job` supplies the delegation from the job's creator,
            # and returns None for a triage-created job, which acts for nobody.
            #
            # Same transaction as the result rows and T8 (MOS-SEC-149): a COMPLETED job
            # with results and no audit row is not a reachable state.
            audit.record(
                conn,
                action="result.create",
                action_class="clinical",
                actor=audit.Actor.for_capability(r.capability_id, r.capability_version),
                resource=audit.Resource(kind="result", id=ids[-1]),
                outcome="allow",
                pep="result.publish",
                trace_id=str(job["trace_id"]),
                request_id=f"req_{job['public_id']}",
                job_id=job_id,
                on_behalf_of=audit.OnBehalfOf.from_job(job),
                study_instance_uid=str(job["study_instance_uid"]),
                detail={
                    "capability_id": r.capability_id,
                    "capability_version": r.capability_version,
                    "result_kind": r.result_kind,
                    "series_consumed": list(r.input_series_uids),
                    "instances_consumed": len(r.input_instance_uids),
                    "measurements": len(r.measurements),
                    "generated_object_uids": [
                        o.sop_instance_uid for o in r.dicom_objects
                    ],
                    "clinical_use_mode": r.clinical_use_mode,
                    "worker_version": r.worker_version,
                },
            )
        _open_result_reviews(conn, ids, job)
        queue.complete(job_id, worker_id)
    return ids


def _open_result_reviews(
    conn: psycopg.Connection[Any], result_ids: Sequence[str], job: dict[str, Any]
) -> None:
    """MOS-SAFE-059 row 1: the platform's `PENDING` `ResultReview`, on Result creation.

    "-- | `PENDING` | platform, on `Result` creation when `Deployment.review_mode != off`
    | platform"

    Inside `complete_job_with_results`' transaction, deliberately. The requirement says "on
    Result creation", and a review row opened in a later transaction is a window in which a
    result exists with `review_status = UNREVIEWED` and no row behind it -- which is
    indistinguishable, to every reader, from a deployment with `review_mode: off`.

    Imported INSIDE the function. `medos.safety.repo` imports `medos.db.tenancy`, and a
    module-level import here would make `medos.db.repo` and `medos.safety.repo` a cycle the
    first time the safety module needs anything else from `medos.db`. The cost is one
    import lookup per completed job.

    Swallows nothing: a failure here fails the job's commit, because a result that exists
    without the review row its Deployment mandates is a result whose review obligation was
    silently dropped.
    """
    from medos.safety.policy import default_policy_source
    from medos.safety.repo import open_reviews_for_job

    policy = default_policy_source().policy_for(
        service_id=str(job["service_id"]),
        service_version=str(job["service_version"]),
    )
    open_reviews_for_job(conn, result_ids=list(result_ids), policy=policy)


def list_results(conn: psycopg.Connection[Any], job_id: str) -> list[dict[str, Any]]:
    """Every result of one job, with its measurements, written objects and provenance.

    This is what `GET /api/v1/jobs/{id}` embeds and what the OHIF provenance panel
    renders (CONTRACT.md section 10). MOS-STORE-278: "a result without provenance MUST
    NOT be observable through any API" -- here the provenance columns are on the same
    row, so that cannot happen by construction.
    """
    uid = get_job_uuid(conn, job_id)
    if uid is None:
        return []
    # Every read in one tenant scope, then the shaping outside it. Splitting the two is
    # not style: a `with` block that also builds the response would hold the transaction
    # open across pure Python, and the transaction is what carries the tenant binding.
    fetched: list[tuple[dict[str, Any], list[Any], list[Any]]] = []
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT * FROM results WHERE job_id = %s ORDER BY created_at, capability_id",
            (uid,),
        ).fetchall()
        for row in rows:
            fetched.append(
                (
                    row,
                    conn.execute(
                        """
                        SELECT concept_scheme, concept_code, concept_display, value,
                               ucum_unit, geometry_space, finding_index,
                               source_series_instance_uid, derivation_scheme,
                               derivation_code, method_detail
                          FROM result_measurements WHERE result_id = %s
                         ORDER BY measurement_index
                        """,
                        (row["id"],),
                    ).fetchall(),
                    conn.execute(
                        """
                        SELECT object_kind, sop_class_uid, series_instance_uid,
                               sop_instance_uid, series_number, output_index, stow_state,
                               stored_at, frame_count, research_marked, derivation_inputs
                          FROM result_dicom_objects WHERE result_id = %s
                         ORDER BY object_kind, output_index
                        """,
                        (row["id"],),
                    ).fetchall(),
                )
            )
        # MOS-SAFE-087 / MOS-STORE-278: "a result without provenance MUST NOT be
        # observable through any API". The full MOS-SAFE-083 record, read in the same
        # tenant scope as everything else on this page, keyed by result id.
        records = {
            str(r["result_id"]): dict(r["record"])
            for r in conn.execute(
                "SELECT result_id, record FROM result_provenance WHERE job_id = %s",
                (uid,),
            ).fetchall()
        }

    out: list[dict[str, Any]] = []
    for r, measurements, objects in fetched:
        out.append(
            {
                "result_id": str(r["id"]),
                "capability_id": r["capability_id"],
                "capability_version": r["capability_version"],
                "result_kind": r["result_kind"],
                "clinical_use_mode": r["clinical_use_mode"],
                "plausibility_state": r["plausibility_state"],
                "review_status": r["review_status"],
                "findings": r["findings"],
                "measurements": [
                    {**m, "value": float(m["value"])} for m in measurements
                ],
                "dicom_objects": objects,
                # CONTRACT.md section 10's per-result field set, in one place.
                "provenance": {
                    "job_id": job_id,
                    "study_instance_uid": r["study_instance_uid"],
                    "series_consumed": list(r["input_series_uids"]),
                    "instances_consumed": r["input_instance_count"],
                    "input_uid_digest": r["input_uid_digest"],
                    "input_pixel_digest": r["input_pixel_digest"],
                    "capability_id": r["capability_id"],
                    "capability_version": r["capability_version"],
                    "preprocessing_version": r["preprocessing_version"],
                    "preprocessing_digest": r["preprocessing_digest"],
                    "worker_version": r["worker_version"],
                    "runtime_version": r["runtime_version"],
                    "platform_commit": r["platform_commit"],
                    "geometry": r["geometry"],
                    "generated_objects": [
                        {
                            "object_kind": o["object_kind"],
                            "series_instance_uid": o["series_instance_uid"],
                            "sop_instance_uid": o["sop_instance_uid"],
                        }
                        for o in objects
                    ],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                    "created_at": r["created_at"],
                    # ------------------------------------------------ MOS-SAFE-083
                    # The FULL record, nested under the narrow weeks 1-2 field set rather
                    # than replacing it. Two readers already consume the flat keys -- the
                    # OHIF provenance panel (`medos/web/ohif-extension/src/core/provenance.js`'s
                    # REQUIRED_RESULT_FIELDS) and tests/e2e/test_demo.py -- and breaking
                    # them to add a better shape would be a migration nobody needs. The
                    # flat keys are a PROJECTION of `record`; `record` is the document
                    # that was hashed (MOS-SAFE-090) and the one a signed export carries.
                    #
                    # `None` is only possible for a row written before this migration, and
                    # MOS-SAFE-087's reader can tell the difference between "no record"
                    # and "an empty record", which a `{}` default would hide.
                    "record": records.get(str(r["id"])),
                    "record_schema_version": (
                        records.get(str(r["id"])) or {}
                    ).get("record_schema_version"),
                },
            }
        )
    return out


def mark_dicom_stored(
    conn: psycopg.Connection[Any],
    sop_instance_uid: str,
    *,
    state: str = "stored",
    object_digest: str | None = None,
    size_bytes: int | None = None,
) -> None:
    """Record the STOW outcome for one written object.

    The CHECK `(stow_state IN ('stored','verified')) = (stored_at IS NOT NULL)` is why
    `stored_at` is set here and not left to the caller: a row claiming `stored` with no
    timestamp would not insert, and one claiming `pending` with a timestamp would not
    either.
    """
    if state not in ("pending", "stored", "verified", "failed", "erased"):
        raise ValueError(f"unknown stow_state {state!r}")
    stamp = "now()" if state in ("stored", "verified") else "NULL"
    with tenant_tx(conn):
        conn.execute(
            f"""
            UPDATE result_dicom_objects
               SET stow_state = %s,
                   stored_at = {stamp},
                   verified_at = CASE WHEN %s = 'verified' THEN now() ELSE verified_at END,
                   object_digest = coalesce(%s, object_digest),
                   size_bytes = coalesce(%s, size_bytes)
             WHERE sop_instance_uid = %s
            """,
            (state, state, object_digest, size_bytes, sop_instance_uid),
        )


# Re-exported so callers do not each invent their own "how long is a lease" answer.
DEFAULT_LEASE_SECONDS = int(os.environ.get("MEDOS_LEASE_SECONDS", "120"))
DEFAULT_HEARTBEAT_SECONDS = DEFAULT_LEASE_SECONDS // 4  # MOS-EXEC-029: lease >= 4 * hb


def utcnow() -> datetime:
    return datetime.now(UTC)


def seconds(n: float) -> timedelta:
    return timedelta(seconds=n)
