# SPDX-License-Identifier: Apache-2.0
"""The `JobQueue` port and its PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` driver.

CONTRACT.md section 4 is the port, verbatim, and section 4 is also the reason there is
only one driver here:

    Driver 1 is `PostgresJobQueue` using `SELECT ... FOR UPDATE SKIP LOCKED`.
    **The job row and the queue row MUST be committed in ONE transaction** -- this is why
    the dual-write/outbox problem does not exist in this slice.
    Expired leases MUST be reclaimable. A reclaim increments `attempt`.

MOS-EXEC-040 makes that explicit from the other side: the transactional outbox exists for
the Kafka driver only, and "introducing it in 0.1 would be cost with no benefit". The
whole design below follows from that one sentence -- `enqueue()` never opens a
transaction of its own, so it can only ever run inside the caller's job-creation
transaction.

Identity: every method takes and returns a `jobs.public_id` (`job_<ULID>`), never the
internal `jobs.id` uuid
-----------------------------------------------------------------------------------
MOS-STORE-357: "`jobs.id` is the internal uuid and is never emitted; `jobs.public_id` is
the external identifier, and it is the only job id that appears in a URL, an event
envelope, a webhook body or an agent tool input." The port's `job_id: str` is therefore
the public id throughout `medos.db`, and the uuid never leaves this package. The claim
statement already joins `jobs` for `max_attempts` (chapter 5 section 5.6.3), so the
translation costs nothing.

What this driver implements from chapter 5, and what it deliberately does not
----------------------------------------------------------------------------
Implemented: MOS-EXEC-025 (all dispatch through the port), MOS-EXEC-026 (Claim never
blocks), MOS-EXEC-027 (fencing), MOS-EXEC-028 (Heartbeat extends the lease and reports
loss), MOS-EXEC-034 (one-transaction enqueue), MOS-EXEC-035 (claim_count incremented in
exactly one statement), MOS-EXEC-036 (Claim never picks up a merely-expired lease),
MOS-EXEC-073/074 (CANCELLED unreachable).

Not implemented, because CONTRACT.md section 0 removes what they depend on: the Kafka
driver (MOS-EXEC-039), the outbox (MOS-EXEC-040), the dead-letter queue and load-shed
budget accounting (MOS-EXEC-064/066 -- `job_dead_letter` is not one of CONTRACT.md
section 8's eight tables), `LISTEN`/`NOTIFY` consumption (the trigger fires, nothing
subscribes yet; MOS-EXEC-037 makes polling the correctness path anyway), and tenancy.

Two readings this module had to settle, both reported rather than buried
-----------------------------------------------------------------------
1. "A reclaim increments `attempt`" (CONTRACT.md section 4) versus MOS-EXEC-035
   ("`job_queue.claim_count` MUST be incremented in exactly one statement in the entire
   system -- the claim statement ... Neither `Fail`, nor the reclaimer, nor the
   reconciler may increment it"). Both are satisfied by incrementing at the claim only:
   a reclaimed job is released to `QUEUED`, the next claim increments `claim_count` and
   copies it into `jobs.attempt`, so a job whose worker died resumes on attempt N+1 --
   which is the property CONTRACT.md section 4 states and `tests/integration/
   test_queue.py::test_expired_lease_is_reclaimed_and_attempt_increments` asserts. The
   alternative -- incrementing in `reclaim_expired()` too -- would double-count every
   crash and make MOS-EXEC-064's "a shed must not consume retry budget" uncheckable,
   which is the exact failure MOS-EXEC-035 was written to prevent.

2. The port identifies a lease by `worker_id` (CONTRACT.md section 4:
   `heartbeat(job_id, worker_id, lease_seconds)`), while MOS-EXEC-027 identifies it by a
   `fence_token`. Both are used. `fence_token` is incremented on every claim and checked
   by `job_transition()` for `complete`/`fail`, so the T8/T9/T11 transitions are fenced
   exactly as MOS-EXEC-027 requires. `worker_id` is the port-level lease check, and it is
   sound ON THE CONDITION that no two processes share a worker id -- which is why
   `make_worker_id()` exists and why callers should use it rather than a hostname. The
   hazardous sequence (A claims, freezes, is reclaimed, B claims, A wakes and completes)
   is caught by both mechanisms independently: A's `lease_owner = A` predicate no longer
   matches, and A's stale fence token would be rejected by `job_transition`.

3. `clock_timestamp()`, not `now()`, for every lease and availability deadline.

   Chapter 5 sections 5.6.3 and 5.6.4 write `now()`. In PostgreSQL `now()` is
   `transaction_timestamp()` -- it is frozen at the first statement of the transaction --
   and every one of those SQL fragments is written on the assumption that it runs in a
   short transaction of its own. Two of this driver's methods do not: `complete()` and
   `fail()` run inside the caller's transaction, which by CONTRACT.md section 8 is the
   same transaction that writes the `results` rows and which may therefore have been
   open since before the inference started.

   With `now()` that is a live correctness bug and it was caught by
   `test_expired_lease_is_reclaimed_and_attempt_increments` rather than reasoned about:
   `lease_expires_at > now()` inside a long transaction compares against a timestamp from
   minutes ago, so a lease that has genuinely expired reads as valid and the runner is
   allowed to write a result for a job another worker now owns -- exactly what
   MOS-EXEC-028 forbids. The reclaimer has the mirror bug: `lease_expires_at < now()`
   never fires.

   `clock_timestamp()` is the real wall clock at statement execution, so the predicate
   means what chapter 5's prose says it means regardless of the caller's transaction
   shape. `now()` is kept where the value is a RECORD of when this transaction happened
   (`job_transition()`'s `started_at` / `queued_at` / `finished_at`, `created_at`
   defaults), because there the transaction timestamp is the correct semantics: all rows
   written by one transaction should agree on when it was.

Spec: MOS-EXEC-025 .. MOS-EXEC-038, MOS-EXEC-053, MOS-EXEC-073, MOS-EXEC-074,
MOS-EXEC-075, MOS-STORE-271, MOS-STORE-274, MOS-STORE-357, CONTRACT.md sections 3 and 4.
"""

from __future__ import annotations

import os
import secrets
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

import psycopg
from psycopg.types.json import Jsonb

from medos.core.errors import SystemFailure
from medos.db.tenancy import tenant_tx

__all__ = [
    "JobQueue",
    "PostgresJobQueue",
    "Lease",
    "FailDecision",
    "Reclaimed",
    "LeaseLost",
    "QueueError",
    "queue_name",
    "make_worker_id",
    "DEFAULT_SERVICE_ID",
    "DISPATCH_SCHEMA_REF",
]


# The weeks 1-2 slice runs one in-process service holding all three capabilities of
# CONTRACT.md section 7. There is no registry to resolve a slug from (CONTRACT.md
# section 0), so the slug is a constant and the queue name derives from it.
DEFAULT_SERVICE_ID = "medos.slice"

# MOS-EXEC-080: the single schema authority. The document is not published in this slice;
# the reference is still emitted so that a consumer added later fails loudly on a missing
# schema rather than silently accepting an unversioned envelope.
DISPATCH_SCHEMA_REF = "https://spec.medicalos.org/schemas/v1/events/job.dispatch/1-0-0.json"


class QueueError(SystemFailure):
    """A queue-driver invariant was violated.

    Under `SystemFailure` and not `TransportFailure`: if the claim loop's own bookkeeping
    is wrong, retrying re-runs the bug (`medos.core.errors`).
    """


class LeaseLost(QueueError):
    """MOS-EXEC-028: the lease is gone; the runner MUST abort immediately.

    "A runner that receives `Valid = false` MUST abort immediately, MUST NOT call
    `Complete` or `Fail`, MUST NOT STOW any DICOM object, and MUST release GPU memory."
    `heartbeat()` returns `False` rather than raising, because that is the shape
    CONTRACT.md section 4 fixes; `complete()` and `fail()` raise, because there is no
    boolean in their signatures to carry the fact and silently doing nothing would let a
    runner believe it had committed a result.
    """

    def __init__(self, job_id: str, worker_id: str) -> None:
        super().__init__(
            "lease_lost",
            detail={"job_id": job_id, "worker_id": worker_id},
            message=(
                f"worker {worker_id} no longer holds the lease on {job_id}; "
                "abort without writing results or STOWing any object (MOS-EXEC-028)"
            ),
        )


def queue_name(service_id: str = DEFAULT_SERVICE_ID) -> str:
    """MOS-EXEC-033: the per-service work inbox is `medicalos.svc.<service_id>.work`.

    Under driver 1 this is a column VALUE and not a schema object, which is why
    registering a service version needs no broker and no DDL -- MOS-EXEC-033 calls that
    absence of provisioning "a deliberate reason to ship driver 1 first".
    """
    return f"medicalos.svc.{service_id}.work"


def make_worker_id(prefix: str = "runner") -> str:
    """A worker id that is unique per PROCESS, not per host.

    Two runners on one host sharing a worker id would defeat the port-level lease check
    (see the module docstring, reading 2), so the pid and 8 random hex characters are
    both included: the pid alone recycles, and a container restart can reuse it inside
    the lease window.
    """
    return f"{prefix}-{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------------
# The port. CONTRACT.md section 4, verbatim.
# ---------------------------------------------------------------------------------
@runtime_checkable
class JobQueue(Protocol):
    """CONTRACT.md section 4. Six methods; a driver MUST implement all six.

    MOS-EXEC-025: "All dispatch MUST go through the `JobQueue` port. No component may
    write to `job_queue` or produce to a work topic directly."
    """

    def enqueue(self, job_id: str) -> None: ...
    def claim(self, worker_id: str, lease_seconds: int) -> str | None: ...  # job_id or None
    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool: ...
    def complete(self, job_id: str, worker_id: str) -> None: ...
    def fail(self, job_id: str, worker_id: str, *, retryable: bool) -> None: ...
    def cancel(self, job_id: str) -> None: ...  # raises NotImplementedError


# ---------------------------------------------------------------------------------
# Value objects the driver hands back. None of them carry PHI: UIDs, counts, timestamps.
# ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Lease:
    """One granted lease. The richer return `claim()` discards to match the port."""

    job_id: str  # jobs.public_id
    fence_token: int  # MOS-EXEC-027
    claim_count: int  # == jobs.attempt after this claim (MOS-EXEC-035)
    max_attempts: int  # jobs.max_attempts; the budget ceiling (MOS-EXEC-063)
    lease_expires_at: datetime
    envelope: dict[str, Any]  # MOS-EXEC-075
    worker_id: str

    @property
    def attempts_remaining(self) -> int:
        return max(0, self.max_attempts - self.claim_count)


@dataclass(frozen=True)
class FailDecision:
    """MOS-EXEC-030: "`Fail` decides the next state ... and returns the decision to the
    caller. The caller MUST NOT make that decision itself."

    `fail()` returns `None` because CONTRACT.md section 4 fixes that signature;
    `fail_ex()` returns this so a runner can log the outcome without re-deriving it.
    """

    job_id: str
    next_state: Literal["QUEUED", "FAILED"]
    attempt: int
    max_attempts: int
    failure_class: str
    failure_code: str
    retry_at: datetime | None


@dataclass(frozen=True)
class Reclaimed:
    """One job whose lease expired and which the janitor released or gave up on."""

    job_id: str
    claim_count: int
    outcome: Literal["requeued", "failed"]
    available_at: datetime | None
    previous_owner: str


# ---------------------------------------------------------------------------------
# Driver 1
# ---------------------------------------------------------------------------------
class PostgresJobQueue:
    """`SELECT ... FOR UPDATE SKIP LOCKED` over `job_queue` (chapter 5 section 5.6).

    Holds a caller-owned connection and never commits on its own initiative: every
    statement runs inside `with conn.transaction()`, which is a SAVEPOINT when the caller
    already has a transaction open and a real BEGIN/COMMIT when it does not (see
    `medos.db.conn`). That single fact is what makes `enqueue()` composable into the job
    INSERT -- MOS-EXEC-034 -- and `claim()` standalone -- MOS-EXEC-025 -- with no flag.
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        *,
        service_id: str = DEFAULT_SERVICE_ID,
        attempt_deadline_s: int = 3600,
    ) -> None:
        self._conn = conn
        self._service_id = service_id
        self._queue = queue_name(service_id)
        self._attempt_deadline_s = attempt_deadline_s

    # -- properties ---------------------------------------------------------------
    @property
    def queue(self) -> str:
        return self._queue

    @property
    def connection(self) -> psycopg.Connection[Any]:
        return self._conn

    # -- enqueue ------------------------------------------------------------------
    def enqueue(self, job_id: str) -> None:
        """Insert the `job_queue` row and perform T2 `CREATED` -> `QUEUED`.

        MOS-EXEC-034, which is the reason driver 1 ships first: "The job row and the
        queue row MUST be inserted in one transaction ... there is no dual write, so
        there is no outbox and no crash window in which a job exists but is invisible to
        workers, or is dispatched without a row."

        This method therefore MUST be called inside the transaction that inserted the
        `jobs` row. It never commits (`conn.transaction()` degrades to a SAVEPOINT when a
        transaction is already open), so a caller that forgets to commit loses BOTH rows
        together -- which is the failure mode you want, rather than a job with no queue
        row.

        `medos.db.repo.create_job_queued()` is the intended caller and does exactly that.

        The queue name comes from the JOB's `service_id`, not from this instance: the
        control plane enqueues for whichever service resolution picked, while a runner
        claims only from its own inbox.
        """
        with tenant_tx(self._conn):
            row = self._conn.execute(
                """
                SELECT id, public_id, service_id, service_version, state,
                       study_instance_uid, prior_study_instance_uids, capability_ids,
                       requested_outputs, clinical_use_mode, idempotency_key,
                       deadline_at, trace_id, correlation_id
                  FROM jobs
                 WHERE public_id = %s
                 FOR UPDATE
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise QueueError(
                    "job_not_found",
                    detail={"job_id": job_id},
                    message=f"cannot enqueue {job_id}: no such job",
                )
            if row["state"] != "CREATED":
                # T2's guard. A second enqueue of a QUEUED job would reset the lease
                # bookkeeping of a job a worker may already be running.
                raise QueueError(
                    "job_not_enqueueable",
                    detail={"job_id": job_id, "state": row["state"]},
                    message=(
                        f"job {job_id} is {row['state']}; only CREATED may be enqueued "
                        "(T2, MOS-EXEC-010)"
                    ),
                )

            envelope = self._dispatch_envelope(row)
            self._conn.execute(
                """
                INSERT INTO job_queue (job_id, queue, partition_key, envelope)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    row["id"],
                    queue_name(row["service_id"]),
                    # MOS-EXEC-044. chapter 12 spells it <tenant_id>:<study_instance_uid>;
                    # with no tenant the study UID alone keeps every job on one study in
                    # one partition when driver 2 arrives.
                    row["study_instance_uid"],
                    Jsonb(envelope),
                ),
            )
            self._conn.execute(
                """
                SELECT job_transition(%s, ARRAY['CREATED']::job_state[], 'QUEUED',
                                      'control-plane', NULL, %s)
                """,
                (row["id"], Jsonb({"reason": "enqueued", "queue": envelope["queue"]})),
            )

    def _dispatch_envelope(self, row: dict[str, Any]) -> dict[str, Any]:
        """MOS-EXEC-075: one envelope schema, one typed payload per `event_type`.

        MOS-STORE-272 / MOS-EXEC-079: no PHI. The only patient-linked values here are
        DICOM UIDs, which are explicitly permitted; there is no PatientName, PatientID,
        AccessionNumber, StudyDate or StudyDescription and none may be added.
        """
        deadline = row["deadline_at"]
        return {
            "schema_ref": DISPATCH_SCHEMA_REF,
            "event_id": str(uuid.uuid4()),
            "event_type": "job.dispatch",
            "event_time": datetime.now(UTC).isoformat(),
            "producer": "medos.control-plane",
            "queue": queue_name(row["service_id"]),
            "job_id": row["public_id"],
            "trace_id": row["trace_id"],
            "correlation_id": row["correlation_id"],
            "payload": {
                "service_id": row["service_id"],
                "service_version": row["service_version"],
                "capability_ids": list(row["capability_ids"]),
                "study_instance_uid": row["study_instance_uid"],
                "prior_study_instance_uids": list(row["prior_study_instance_uids"]),
                "requested_outputs": list(row["requested_outputs"]),
                "clinical_use_mode": row["clinical_use_mode"],
                # MOS-EXEC-053's platform-derived key. The client's header echo
                # (`request_idempotency_key`) is deliberately NOT here: MOS-EXEC-054 and
                # CONTRACT.md section 9 keep it out of everything that feeds UID
                # derivation, and the envelope is read by the runner that derives them.
                "idempotency_key": row["idempotency_key"],
                "deadline_at": deadline.isoformat() if deadline is not None else None,
            },
        }

    # -- claim --------------------------------------------------------------------
    def claim(self, worker_id: str, lease_seconds: int) -> str | None:
        """CONTRACT.md section 4. Returns a `jobs.public_id` or `None`."""
        lease = self.claim_lease(worker_id, lease_seconds)
        return None if lease is None else lease.job_id

    def claim_lease(self, worker_id: str, lease_seconds: int) -> Lease | None:
        """chapter 5 section 5.6.3, with `LIMIT 1` because the port claims one job.

        MOS-EXEC-026: MUST NOT block. `SKIP LOCKED` is what makes that true under N
        concurrent claimers -- a claimer that finds a row locked walks past it instead of
        waiting, so two workers never contend for one job and never serialise behind each
        other.

        MOS-EXEC-036: rows whose lease has merely EXPIRED are not picked up here. They
        are `reclaim_expired()`'s. "Mixing the two into one query hides the retry-budget
        decision inside a hot path and makes the DLQ transition unreachable in the common
        case."

        MOS-EXEC-035: the `claim_count + 1` below is the only statement in the system
        that increments it, and `jobs.attempt` is set from it in this same transaction --
        so the first delivery of a job carries `claim_count = 1` and `attempt = 1`.
        """
        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds must be positive, got {lease_seconds}")

        with tenant_tx(self._conn):
            row = self._conn.execute(
                """
                WITH candidate AS (
                  SELECT q.job_id
                    FROM job_queue q
                    JOIN jobs j ON j.id = q.job_id
                   WHERE q.queue        = %(queue)s
                     AND q.lease_owner  IS NULL
                     AND q.available_at <= clock_timestamp()
                     AND j.state        = 'QUEUED'
                   ORDER BY q.priority, q.available_at, q.enqueued_at
                     FOR UPDATE OF q SKIP LOCKED
                   LIMIT 1
                )
                UPDATE job_queue q
                   SET lease_owner      = %(worker)s,
                       lease_expires_at = clock_timestamp() + make_interval(secs => %(lease)s),
                       fence_token      = q.fence_token + 1,
                       claim_count      = q.claim_count + 1
                  FROM candidate c, jobs j
                 WHERE q.job_id = c.job_id AND j.id = q.job_id
                RETURNING q.job_id, j.public_id, q.fence_token, q.claim_count,
                          j.max_attempts, q.lease_expires_at, q.envelope
                """,
                {"queue": self._queue, "worker": worker_id, "lease": lease_seconds},
            ).fetchone()
            if row is None:
                return None
            return self._finish_claim(row, worker_id, lease_seconds)

    def _finish_claim(
        self, row: dict[str, Any], worker_id: str, lease_seconds: int
    ) -> Lease:
        """The tail of a claim: `jobs.attempt`, the deadline, T5, and the `Lease`.

        Extracted from `claim_lease()` so that driver 2 (`medos.db.queue_kafka`) performs
        the IDENTICAL tail after it has persisted a dispatch consumed from the bus.
        MOS-EXEC-032 requires one conformance suite to pass against both drivers
        unchanged; two copies of this tail would be two chances for the drivers to
        disagree about what a claim IS, and the suite could not tell them apart.

        `row` is the `RETURNING` of the claim UPDATE: job_id (uuid), public_id,
        fence_token, claim_count, max_attempts, lease_expires_at, envelope.
        """
        with tenant_tx(self._conn):
            # chapter 5 section 5.6.3: "The caller then, in the same transaction, ...
            # calls job_transition(..., 'RUNNING', ...) and sets jobs.attempt =
            # claim_count and jobs.attempt_deadline_at = now() + attempt_deadline_s."
            # MOS-EXEC-070: absolute, never a duration.
            self._conn.execute(
                """
                UPDATE jobs
                   SET attempt = %s,
                       attempt_deadline_at = clock_timestamp() + make_interval(secs => %s)
                 WHERE id = %s
                """,
                (row["claim_count"], self._attempt_deadline_s, row["job_id"]),
            )
            self._conn.execute(
                """
                SELECT job_transition(%s, ARRAY['QUEUED']::job_state[], 'RUNNING',
                                      'job-runner', %s, %s)
                """,
                (
                    row["job_id"],
                    row["fence_token"],
                    Jsonb(
                        {
                            "reason": "claimed",
                            "worker_id": worker_id,
                            "attempt": row["claim_count"],
                            "max_attempts": row["max_attempts"],
                            "lease_seconds": lease_seconds,
                        }
                    ),
                ),
            )
            return Lease(
                job_id=row["public_id"],
                fence_token=row["fence_token"],
                claim_count=row["claim_count"],
                max_attempts=row["max_attempts"],
                lease_expires_at=row["lease_expires_at"],
                envelope=row["envelope"],
                worker_id=worker_id,
            )

    # -- heartbeat ----------------------------------------------------------------
    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        """MOS-EXEC-028: extend the lease by `lease_seconds`; `False` if the lease is lost.

        CONTRACT.md section 4: "heartbeat extends the lease and returns False if the
        worker lost it." A `False` is not an error to log and continue past -- the runner
        MUST abort immediately, MUST NOT call `complete()` or `fail()`, and MUST NOT STOW
        any DICOM object.

        MOS-EXEC-029's guidance is that `LeaseDuration >= 4 * heartbeat interval`; that
        is the runner's choice and is not enforced here, because this method cannot see
        the interval its caller uses.
        """
        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds must be positive, got {lease_seconds}")
        with tenant_tx(self._conn):
            row = self._conn.execute(
                """
                UPDATE job_queue q
                   SET lease_expires_at = clock_timestamp() + make_interval(secs => %(lease)s)
                  FROM jobs j
                 WHERE j.id = q.job_id
                   AND j.public_id       = %(job)s
                   AND q.lease_owner     = %(worker)s
                   AND q.lease_expires_at > clock_timestamp()
                RETURNING q.fence_token
                """,
                {"lease": lease_seconds, "job": job_id, "worker": worker_id},
            ).fetchone()
        return row is not None

    # -- complete -----------------------------------------------------------------
    def complete(self, job_id: str, worker_id: str) -> None:
        """T8 `RUNNING` -> `COMPLETED`, fenced, and the queue row deleted.

        MOS-EXEC-057 / CONTRACT.md section 8: "the results row and the terminal state
        transition MUST be written in ONE transaction". This method does NOT open one of
        its own -- it joins the caller's. `medos.db.repo.complete_job_with_results()` is
        the intended caller: it writes `results`, `result_measurements` and
        `result_dicom_objects` and then calls this, all inside one `with
        conn.transaction()`.

        MOS-STORE-274: the `job_queue` row is deleted at the terminal state; the `jobs`
        row is the durable record.
        """
        with tenant_tx(self._conn):
            fence = self._lock_lease(job_id, worker_id)
            self._conn.execute(
                """
                SELECT job_transition(
                  (SELECT id FROM jobs WHERE public_id = %s),
                  ARRAY['RUNNING']::job_state[], 'COMPLETED', 'job-runner', %s, %s)
                """,
                (job_id, fence, Jsonb({"reason": "completed", "worker_id": worker_id})),
            )
            self._delete_queue_row(job_id)

    # -- reject -------------------------------------------------------------------
    def reject(
        self,
        job_id: str,
        worker_id: str,
        *,
        reason_code: str,
        reason_detail: str = "",
    ) -> None:
        """T7 `RUNNING` -> `REJECTED`. Not in the port; it is the clinical terminal.

        CONTRACT.md section 3: "`REJECTED` is a **clinical** outcome, not an error ... It
        MUST carry a machine-readable reason." MOS-EXEC-015: a rejected job "MUST NOT be
        retried, MUST NOT consume retry budget, MUST NOT be dead-lettered, and MUST NOT
        raise an on-call alert. Its queue row is deleted in the same transaction as the
        transition."

        It is a method on the queue rather than on the repo because deleting the queue
        row in that same transaction is a queue-driver concern, and because MOS-EXEC-025
        forbids any other component writing to `job_queue`.
        """
        with tenant_tx(self._conn):
            fence = self._lock_lease(job_id, worker_id)
            self._conn.execute(
                """
                UPDATE jobs SET reject_reason_code = %s, reject_reason_detail = %s
                 WHERE public_id = %s
                """,
                (reason_code, reason_detail, job_id),
            )
            self._conn.execute(
                """
                SELECT job_transition(
                  (SELECT id FROM jobs WHERE public_id = %s),
                  ARRAY['RUNNING']::job_state[], 'REJECTED', 'job-runner', %s, %s)
                """,
                (
                    job_id,
                    fence,
                    Jsonb(
                        {
                            "reason": "clinical_rejection",
                            "reject_reason_code": reason_code,
                            "reject_reason_detail": reason_detail,
                            "worker_id": worker_id,
                        }
                    ),
                ),
            )
            self._delete_queue_row(job_id)

    # -- fail ---------------------------------------------------------------------
    def fail(self, job_id: str, worker_id: str, *, retryable: bool) -> None:
        """CONTRACT.md section 4. The decision is available from `fail_ex()`."""
        self.fail_ex(job_id, worker_id, retryable=retryable)

    def fail_ex(
        self,
        job_id: str,
        worker_id: str,
        *,
        retryable: bool,
        failure_class: str | None = None,
        failure_code: str | None = None,
        failure_detail: str = "",
    ) -> FailDecision:
        """T9 (`RUNNING` -> `QUEUED` with backoff) or T11 (`RUNNING` -> `FAILED`).

        MOS-EXEC-030: "`Fail` decides the next state from the class table of section
        5.3.2, the current `attempt`, and `max_attempts`, and returns the decision to the
        caller. The caller MUST NOT make that decision itself." So the branch below lives
        here and nowhere else.

        CONTRACT.md section 4 reduces section 5.3.2's twelve-class table to one boolean.
        The mapping used when the caller gives no class is deliberately conservative:
        `retryable=True` -> `transient_infrastructure` (the class whose whole meaning is
        "the hop failed, the study is fine"), `retryable=False` -> `internal`. A caller
        that knows better -- `dicom_store_failed`, `service_crashed`,
        `invalid_result_bundle` -- SHOULD pass it, because that class is what an operator
        reads first.

        MOS-EXEC-035 again: `claim_count` is NOT touched here. The retry budget is spent
        by claiming, not by failing.
        """
        if failure_class is None:
            failure_class = "transient_infrastructure" if retryable else "internal"
        if failure_code is None:
            failure_code = "retryable_failure" if retryable else "unretryable_failure"

        with tenant_tx(self._conn):
            fence = self._lock_lease(job_id, worker_id)
            row = self._conn.execute(
                """
                SELECT q.job_id, q.claim_count, j.max_attempts
                  FROM job_queue q JOIN jobs j ON j.id = q.job_id
                 WHERE j.public_id = %s
                """,
                (job_id,),
            ).fetchone()
            assert row is not None  # _lock_lease already proved the row exists
            budget_left = row["claim_count"] < row["max_attempts"]

            if retryable and budget_left:
                # T9. section 5.6.4's backoff: full jitter over [d/2, d),
                # d = min(600, 5 * 2^(n-1)).
                back = self._conn.execute(
                    """
                    UPDATE job_queue q
                       SET lease_owner      = NULL,
                           lease_expires_at = NULL,
                           available_at     = clock_timestamp() + job_backoff(q.claim_count)
                     WHERE q.job_id = %s
                    RETURNING q.available_at
                    """,
                    (row["job_id"],),
                ).fetchone()
                assert back is not None
                # The failure columns are NOT written on a retry. `jobs.failure_class` is
                # the record of why a job is FAILED (the CHECK binds the two together);
                # leaving it set on a QUEUED job would make every operator dashboard that
                # filters on it show a job that is about to run again. The attempt's
                # failure is recorded in the append-only event stream instead, which is
                # where per-attempt history belongs.
                self._conn.execute(
                    """
                    SELECT job_transition(%s, ARRAY['RUNNING']::job_state[], 'QUEUED',
                                          'job-runner', %s, %s)
                    """,
                    (
                        row["job_id"],
                        fence,
                        Jsonb(
                            {
                                "reason": "retry",
                                "failure_class": failure_class,
                                "failure_code": failure_code,
                                "attempt": row["claim_count"],
                                "max_attempts": row["max_attempts"],
                                "worker_id": worker_id,
                            }
                        ),
                    ),
                )
                return FailDecision(
                    job_id=job_id,
                    next_state="QUEUED",
                    attempt=row["claim_count"],
                    max_attempts=row["max_attempts"],
                    failure_class=failure_class,
                    failure_code=failure_code,
                    retry_at=back["available_at"],
                )

            # T11. chapter 5 writes a `job_dead_letter` row in this transaction;
            # `job_dead_letter` is not one of CONTRACT.md section 8's eight tables, so in
            # this slice the forensic record is the `jobs` row plus the append-only
            # `job_events` stream. Reported, not silently dropped.
            self._conn.execute(
                "UPDATE jobs SET failure_class = %s, failure_code = %s, failure_detail = %s"
                " WHERE id = %s",
                (failure_class, failure_code, failure_detail or None, row["job_id"]),
            )
            self._conn.execute(
                """
                SELECT job_transition(%s, ARRAY['RUNNING']::job_state[], 'FAILED',
                                      'job-runner', %s, %s)
                """,
                (
                    row["job_id"],
                    fence,
                    Jsonb(
                        {
                            "reason": "failed",
                            "failure_class": failure_class,
                            "failure_code": failure_code,
                            "message": failure_detail,
                            "attempt": row["claim_count"],
                            "max_attempts": row["max_attempts"],
                            "retryable": retryable,
                            "worker_id": worker_id,
                        }
                    ),
                ),
            )
            self._delete_queue_row(job_id)
            return FailDecision(
                job_id=job_id,
                next_state="FAILED",
                attempt=row["claim_count"],
                max_attempts=row["max_attempts"],
                failure_class=failure_class,
                failure_code=failure_code,
                retry_at=None,
            )

    # -- cancel -------------------------------------------------------------------
    def cancel(self, job_id: str) -> None:
        """CONTRACT.md section 4: "cancel() raises NotImplementedError".

        CONTRACT.md section 3: "`CANCELLED` is RESERVED in this slice -- the column and
        enum value exist, nothing produces it." MOS-STORE-269 says the same, and
        MOS-EXEC-073/074 reach it structurally: `CANCELLED` has no row in
        `job_state_transition`, so `job_transition()` would refuse it even if this method
        tried. The raise is the honest surface of that; a silent no-op would let a caller
        believe a running inference had been stopped.
        """
        raise NotImplementedError(
            "CANCELLED is reserved in weeks 1-2 (CONTRACT.md section 3, MOS-STORE-269, "
            "MOS-EXEC-073). Cancellation of a running inference is not implemented "
            f"before 0.3; job {job_id} is unaffected."
        )

    # -- reclaimer ----------------------------------------------------------------
    def reclaim_expired(self, *, limit: int = 100) -> list[Reclaimed]:
        """chapter 5 section 5.6.4. The janitor for leases whose holder stopped.

        CONTRACT.md section 4: "Expired leases MUST be reclaimable. A reclaim increments
        `attempt`." It does -- via the next claim, not here; see the module docstring,
        reading 1, and MOS-EXEC-035's "neither Fail, nor the reclaimer, nor the
        reconciler may increment it".

        (a) budget remains  -> release with backoff, T10 `RUNNING` -> `QUEUED`
        (b) budget exhausted -> T12 `RUNNING` -> `FAILED`, `failure_class =
            'lease_expired'`, queue row deleted

        The actor is `queue-reclaimer`, which is a different row of
        `job_state_transition` from `job-runner` even for the same state pair -- that is
        how the transition table distinguishes "the runner gave up" from "the runner
        vanished". `p_fence` is NULL here because the reclaimer holds no lease
        (MOS-EXEC-027).
        """
        out: list[Reclaimed] = []
        with tenant_tx(self._conn):
            expired = self._conn.execute(
                """
                WITH expired AS (
                  SELECT q.job_id, q.lease_owner AS prev_owner
                    FROM job_queue q
                    JOIN jobs j ON j.id = q.job_id
                   WHERE q.lease_owner      IS NOT NULL
                     AND q.lease_expires_at < clock_timestamp()
                     AND q.claim_count      < j.max_attempts
                     AND q.queue            = %(queue)s
                   ORDER BY q.lease_expires_at
                     FOR UPDATE OF q SKIP LOCKED
                   LIMIT %(limit)s
                )
                UPDATE job_queue q
                   SET lease_owner      = NULL,
                       lease_expires_at = NULL,
                       available_at     = clock_timestamp() + job_backoff(q.claim_count)
                  FROM expired e
                 WHERE q.job_id = e.job_id
                RETURNING q.job_id, q.claim_count, q.available_at, e.prev_owner,
                          (SELECT public_id FROM jobs WHERE id = q.job_id) AS public_id
                """,
                {"queue": self._queue, "limit": limit},
            ).fetchall()

            for row in expired:
                self._conn.execute(
                    "SELECT job_append_event(%s, 'queue.lease_expired', 'queue-reclaimer', %s)",
                    (
                        row["job_id"],
                        Jsonb(
                            {
                                "reason": "lease_expired",
                                "claim_count": row["claim_count"],
                                "outcome": "requeued",
                                "previous_owner": row["prev_owner"],
                                "available_at": row["available_at"].isoformat(),
                            }
                        ),
                    ),
                )
                self._conn.execute(
                    """
                    SELECT job_transition(%s, ARRAY['RUNNING']::job_state[], 'QUEUED',
                                          'queue-reclaimer', NULL, %s)
                    """,
                    (
                        row["job_id"],
                        Jsonb(
                            {
                                "reason": "lease_expired",
                                "attempt": row["claim_count"],
                                "next_available_at": row["available_at"].isoformat(),
                            }
                        ),
                    ),
                )
                out.append(
                    Reclaimed(
                        job_id=row["public_id"],
                        claim_count=row["claim_count"],
                        outcome="requeued",
                        available_at=row["available_at"],
                        previous_owner=row["prev_owner"],
                    )
                )

            dead = self._conn.execute(
                """
                SELECT q.job_id, q.claim_count, q.lease_owner, j.public_id, j.max_attempts
                  FROM job_queue q
                  JOIN jobs j ON j.id = q.job_id
                 WHERE q.lease_owner      IS NOT NULL
                   AND q.lease_expires_at < clock_timestamp()
                   AND q.claim_count      >= j.max_attempts
                   AND q.queue            = %(queue)s
                 ORDER BY q.lease_expires_at
                   FOR UPDATE OF q SKIP LOCKED
                 LIMIT %(limit)s
                """,
                {"queue": self._queue, "limit": limit},
            ).fetchall()

            for row in dead:
                self._conn.execute(
                    """
                    UPDATE jobs
                       SET failure_class = 'lease_expired',
                           failure_code  = 'lease_expired',
                           failure_detail = %s
                     WHERE id = %s
                    """,
                    (
                        f"lease expired after {row['claim_count']} of "
                        f"{row['max_attempts']} attempts",
                        row["job_id"],
                    ),
                )
                self._conn.execute(
                    """
                    SELECT job_transition(%s, ARRAY['RUNNING']::job_state[], 'FAILED',
                                          'queue-reclaimer', NULL, %s)
                    """,
                    (
                        row["job_id"],
                        Jsonb(
                            {
                                "reason": "lease_expired",
                                "failure_class": "lease_expired",
                                "failure_code": "lease_expired",
                                "attempt": row["claim_count"],
                                "max_attempts": row["max_attempts"],
                            }
                        ),
                    ),
                )
                self._conn.execute("DELETE FROM job_queue WHERE job_id = %s", (row["job_id"],))
                out.append(
                    Reclaimed(
                        job_id=row["public_id"],
                        claim_count=row["claim_count"],
                        outcome="failed",
                        available_at=None,
                        previous_owner=row["lease_owner"],
                    )
                )
        return out

    # -- internals ----------------------------------------------------------------
    def _lock_lease(self, job_id: str, worker_id: str) -> int:
        """Take a row lock on the queue row, prove the lease, return the fence token.

        `FOR UPDATE OF q` and not a bare SELECT: `complete()` and `fail()` run inside the
        caller's transaction alongside the `results` INSERT, and without the lock a
        concurrent reclaim could release the lease between the check and the transition.

        The lease predicate is `lease_owner = worker_id AND
        lease_expires_at > clock_timestamp()` -- see the module docstring, reading 3, for
        why it is not `now()`.
        The expiry half matters as much as the owner half: a worker whose process froze
        past its lease MUST NOT be allowed to write a result, even if the reclaimer has
        not run yet (MOS-EXEC-028).
        """
        with tenant_tx(self._conn):
            row = self._conn.execute(
                """
                SELECT q.fence_token
                  FROM job_queue q
                  JOIN jobs j ON j.id = q.job_id
                 WHERE j.public_id        = %s
                   AND q.lease_owner      = %s
                   AND q.lease_expires_at > clock_timestamp()
                   FOR UPDATE OF q
                """,
                (job_id, worker_id),
            ).fetchone()
        if row is None:
            raise LeaseLost(job_id, worker_id)
        return int(row["fence_token"])

    def _delete_queue_row(self, job_id: str) -> None:
        """MOS-STORE-274: the queue row is deleted when the job reaches a terminal state.

        MOS-EXEC-015 says the same for `REJECTED` specifically, and adds that it happens
        "in the same transaction as the transition" -- which it does, because every
        caller of this is already inside one.
        """
        with tenant_tx(self._conn):
            self._conn.execute(
                "DELETE FROM job_queue "
                "WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)",
                (job_id,),
            )

    # -- introspection used by the runner loop and by tests ------------------------
    def depth(self) -> int:
        """Claimable rows in this inbox right now. MOS-EXEC-038's operating envelope
        caps `job_queue` at 10 000 rows in 0.1-0.2; a runner that sees more should be
        reading that table, not tuning."""
        with tenant_tx(self._conn):
            row = self._conn.execute(
                """
                SELECT count(*) AS n FROM job_queue
                 WHERE queue = %s AND lease_owner IS NULL
                   AND available_at <= clock_timestamp()
                """,
                (self._queue,),
            ).fetchone()
        return 0 if row is None else int(row["n"])

    def lease_state(self, job_id: str) -> dict[str, Any] | None:
        """The queue row for one job, or `None` once it has been deleted."""
        with tenant_tx(self._conn):
            return self._conn.execute(
                """
                SELECT q.queue, q.priority, q.available_at, q.claim_count, q.lease_owner,
                       q.lease_expires_at, q.fence_token, q.enqueued_at
                  FROM job_queue q JOIN jobs j ON j.id = q.job_id
                 WHERE j.public_id = %s
                """,
                (job_id,),
            ).fetchone()


# A structural check that costs nothing at import time and fails loudly in CI if the
# driver drifts from CONTRACT.md section 4. `runtime_checkable` verifies method presence
# only -- signatures are checked by mypy/ruff -- but presence is the half that a rename
# breaks.
def _assert_driver_implements_port() -> None:
    assert issubclass(PostgresJobQueue, JobQueue), (
        "PostgresJobQueue no longer satisfies the JobQueue port of CONTRACT.md section 4"
    )


_assert_driver_implements_port()
