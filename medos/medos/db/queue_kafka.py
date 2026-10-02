# SPDX-License-Identifier: Apache-2.0
"""`JobQueue` driver 2 -- Kafka, with the transactional outbox `MOS-EXEC-040` requires.

WHAT ACTUALLY DIFFERS BETWEEN THE TWO DRIVERS, WHICH IS LESS THAN IT LOOKS
--------------------------------------------------------------------------
`MOS-EXEC-002`: Postgres is the sole source of truth in both drivers, and the bus is never
read to reconstruct state. So driver 2 is not "Kafka instead of Postgres". It is driver 1
with ONE hop replaced: the dispatch.

    driver 1   BEGIN; INSERT jobs; INSERT job_queue; T2 CREATED->QUEUED; COMMIT
               ... a runner SELECTs job_queue FOR UPDATE SKIP LOCKED and leases a row

    driver 2   BEGIN; INSERT jobs; INSERT job_outbox; T2 CREATED->QUEUED; COMMIT
               ... the relay publishes the outbox row to medicalos.svc.<id>.work
               ... a runner CONSUMES one record, INSERTs job_queue, leases it, and only
                   then commits the offset

`Heartbeat`, `Complete`, `Fail`, `Cancel` and the reclaimer are inherited UNCHANGED,
because they operate on `job_queue` and `jobs`, which driver 2 has not moved. Inheritance
here is the statement that MOS-EXEC-002 holds: if driver 2 had needed its own `Complete`,
the bus would have become authoritative for something.

WHY `Claim` STILL READS `job_queue` FIRST
------------------------------------------
A retry is not a redelivery. `Fail(retryable=True)` sets `job_queue.available_at =
now() + backoff` and leaves the row (chapter 5 section 5.6.4), and the reclaimer does the
same for an expired lease -- in BOTH drivers, because in both drivers that row is the
truth. The bus record that originally delivered the job has long since had its offset
committed. So `claim()` looks at the local queue first and only then polls the bus, and
Q06 ("`Fail(retryable=true)` re-offers after the declared backoff") passes identically
under both drivers without the suite knowing which one it is running.

Re-publishing to the broker on every retry was the obvious alternative and it is the wrong
one: it would make the backoff a property of the transport, it would put a second dispatch
for one job on the log, and it would need the bus to be consulted to answer "is this job
already running" -- which is `MOS-EXEC-002` violated in three ways for no gain.

THE CONSUMER HAZARD, AND WHERE THIS CLASS SITS IN IT
-----------------------------------------------------
`MOS-EXEC-047` mandates consume -> persist -> hand off -> heartbeat, and `MOS-EXEC-048`
explains the cost of getting it wrong: a handler that blocks in `poll()` past
`max.poll.interval.ms` is evicted, its partition is rebalanced, and the same job starts on
a second GPU while the first writes DICOM and then fails its commit.

`claim_lease()` below is steps 1 and 2 and nothing else. It consumes one record, persists
the claim, commits the offset, and returns. It never touches inference. Step 3 -- the
bounded execution slot -- is `medos/medos/worker/runner.py`'s `BoundedSemaphore`, and step 4 --
the lease heartbeat on its own connection and its own thread -- is that module's
`_Heartbeat`. Both predate this driver and neither needed changing, which is the point:
the hazard was designed out at the port, so driver 2 inherits the answer.

`MOS-EXEC-050` rejects `consumer.pause()` as an alternative, and this driver offers no
pause: "it keeps the work in memory, so a process kill loses it".

Spec: MOS-EXEC-002, MOS-EXEC-025 .. MOS-EXEC-033, MOS-EXEC-039 .. MOS-EXEC-048,
      MOS-EXEC-050, MOS-EXEC-075, MOS-STORE-274, MOS-TEST-045, MOS-TEST-046.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.bus.envelope import (
    EnvelopeInvalid,
    from_bus_envelope,
    to_bus_envelope,
    validate_envelope,
)
from medos.bus.port import Consumer
from medos.bus.topics import job_partition_key, validate_topic, work_topic
from medos.db.queue import (
    DEFAULT_SERVICE_ID,
    JobQueue,
    Lease,
    PostgresJobQueue,
    QueueError,
)
from medos.db.tenancy import current_tenant, tenant_tx

__all__ = ["KafkaJobQueue", "PoisonDispatch"]

log = logging.getLogger("medos.db.queue_kafka")


class PoisonDispatch(QueueError):
    """A consumed record whose envelope violates `MOS-EXEC-075`.

    `MOS-TEST-045` Q14: "a queue entry whose payload fails `medos/schemas/queue/message.json`
    goes to the DLQ on attempt 1 and does not block later entries on the same partition
    key." The second half is implemented -- the offset is committed so the partition
    advances -- and the first half is not, because `job_dead_letter` (`MOS-EXEC-066`) does
    not exist in this deployment and driver 1 has no dead-letter destination either.
    Giving driver 2 one and not driver 1 is the exact asymmetry `queue-driver-parity`
    forbids, so the gap is reported rather than half-closed. See this component's report.
    """


class KafkaJobQueue(PostgresJobQueue):
    """Driver 2. Same port, same six methods, same source of truth.

    Construction takes a `Consumer` and NOT a `Producer`: producing is the relay's job
    (`MOS-EXEC-041` clause 1 elects exactly one active relay instance), and a driver that
    could produce would be a second producer by construction.
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        consumer: Consumer,
        *,
        service_id: str = DEFAULT_SERVICE_ID,
        attempt_deadline_s: int = 3600,
        poll_timeout_s: float = 0.25,
    ) -> None:
        super().__init__(conn, service_id=service_id, attempt_deadline_s=attempt_deadline_s)
        self._consumer = consumer
        # Kafka's `poll()` always takes a timeout; a zero one returns nothing on a freshly
        # assigned partition, so a claim loop with timeout 0 would spin. This is the
        # consumer-side wait, NOT a lock: `MOS-EXEC-026`'s "Claim MUST NOT block" is about
        # never waiting for another claimer, which `SKIP LOCKED` gives driver 1 and
        # partition assignment gives driver 2. A bounded poll of a quarter second is what
        # every Kafka consumer loop does.
        self._poll_timeout_s = poll_timeout_s
        self._consumer.subscribe([self._queue])

    @property
    def consumer(self) -> Consumer:
        return self._consumer

    # =================================================================================
    # Enqueue -- the dual write, made single by the outbox. MOS-EXEC-040.
    # =================================================================================
    def enqueue(self, job_id: str) -> None:
        """Write `job_outbox` and perform T2, in the caller's transaction.

        MOS-EXEC-040: "Driver 2 introduces a dual write -- the job row goes to Postgres,
        the dispatch message goes to the broker -- and therefore MUST use a transactional
        outbox."

        No `job_queue` row is written here, and that is the whole difference. Under driver
        1 the queue row IS the dispatch; under driver 2 the dispatch is a record on a log
        and the queue row is what the CONSUMER writes when it takes the work
        (`MOS-EXEC-047` step 2). Writing one here as well would give driver 2 two
        independent dispatch paths for one job and reintroduce, inside the platform, the
        duplicate delivery the whole design is arranged to absorb.

        Like driver 1's, this method opens no transaction of its own, so it can only
        commit together with the `jobs` INSERT that `medos.db.repo.create_job_queued()`
        wraps it in. A crash anywhere inside leaves neither row -- Q10.
        """
        with tenant_tx(self._conn):
            row = self._conn.execute(
                """
                SELECT id, public_id, service_id, service_version, state,
                       study_instance_uid, prior_study_instance_uids, capability_ids,
                       requested_outputs, clinical_use_mode, idempotency_key,
                       deadline_at, trace_id, correlation_id, tenant_id, attempt
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
                raise QueueError(
                    "job_not_enqueueable",
                    detail={"job_id": job_id, "state": row["state"]},
                    message=(
                        f"job {job_id} is {row['state']}; only CREATED may be enqueued "
                        "(T2, MOS-EXEC-010)"
                    ),
                )

            dispatch = self._dispatch_envelope(row)
            topic = work_topic(row["service_id"])
            validate_topic(topic)   # MOS-EXEC-003 / MOS-EXEC-046, before it is durable

            # T2 first, because `job_transition()` returns `job_events.seq` and
            # MOS-EXEC-078 makes that the envelope's ordering field: "`job_seq` -- the
            # envelope's carriage of `job_events.seq` -- is the ordering mechanism. Kafka
            # orders only within one partition of one topic, and the spine's lifecycle
            # topics are three separate topics, so `job.completed` can be delivered before
            # `job.state_changed(RUNNING)` under normal consumer lag."
            seq_row = self._conn.execute(
                """
                SELECT job_transition(%s, ARRAY['CREATED']::job_state[], 'QUEUED',
                                      'control-plane', NULL, %s) AS seq
                """,
                (row["id"], Jsonb({"reason": "enqueued", "queue": topic})),
            ).fetchone()
            assert seq_row is not None
            job_seq = int(seq_row["seq"])

            envelope = to_bus_envelope(
                dispatch,
                tenant_id=str(row["tenant_id"]),
                job_uuid=str(row["id"]),
                job_seq=job_seq,
                partition_key=job_partition_key(
                    str(row["tenant_id"]), row["study_instance_uid"]
                ),
                attempt=int(row["attempt"] or 0),
                idempotency_key=row["idempotency_key"],
            )

            self._conn.execute(
                """
                INSERT INTO job_outbox (tenant_id, job_id, topic, partition_key, envelope)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    row["tenant_id"],
                    row["id"],
                    topic,
                    envelope["partition_key"],
                    Jsonb(envelope),
                ),
            )

    # =================================================================================
    # Claim -- consume, persist, commit the offset. MOS-EXEC-047 steps 1 and 2.
    # =================================================================================
    def claim_lease(self, worker_id: str, lease_seconds: int) -> Lease | None:
        """One lease, or None. Never blocks on another claimer (MOS-EXEC-026).

        Two sources, in this order and for the reason in the module docstring:

          1. `job_queue` -- a job re-offered by `Fail(retryable)` or by the reclaimer. The
             row is already local; the bus has nothing to say about it.
          2. the bus -- a first delivery. Consume one record, persist the claim, commit
             the offset.

        The ORDER of the two commits in step 2 is the requirement, not a detail.
        MOS-EXEC-047: "the dispatch envelope is written to `job_queue` (driver 2), and the
        step plan and `RUNNING` transition commit. Only now is the Kafka offset
        committed." Reversed, a crash between them loses the dispatch outright. In this
        order a crash between them redelivers it, and the redelivery is absorbed by the
        `job_queue` primary key and the `state = 'QUEUED'` guard below.
        """
        if lease_seconds <= 0:
            raise ValueError(f"lease_seconds must be positive, got {lease_seconds}")

        local = super().claim_lease(worker_id, lease_seconds)
        if local is not None:
            return local

        record = self._consumer.poll(self._poll_timeout_s)
        if record is None:
            return None

        try:
            envelope = validate_envelope(record.value)
        except EnvelopeInvalid as exc:
            # Q14's second half: do not block the partition. The offset advances so later
            # entries on the same partition key are delivered; the record itself is lost
            # rather than dead-lettered, which is the gap `PoisonDispatch` documents.
            log.error("poison dispatch on %s[%s]@%s: %s",
                      record.topic, record.partition, record.offset, exc)
            self._consumer.commit()
            return None

        lease = self._persist_claim(envelope, worker_id, lease_seconds)

        # Only now (MOS-EXEC-047 step 2). A `ConsumerEvicted` here means the group took
        # the partition back while we held the record -- MOS-EXEC-048's sequence. The
        # claim is already durable and fenced, so it is re-raised for the runner to abort
        # on rather than swallowed: `MOS-EXEC-028` requires a runner that has lost its
        # standing to write nothing at all.
        self._consumer.commit()
        return lease

    def _persist_claim(
        self, envelope: dict[str, Any], worker_id: str, lease_seconds: int
    ) -> Lease | None:
        """Write the `job_queue` row from the dispatch and lease it, in one transaction.

        The INSERT is `ON CONFLICT (job_id) DO NOTHING` and the lease is guarded by
        `jobs.state = 'QUEUED'`. Together those two are the consumer-side deduplication
        `MOS-EXEC-041` requires -- "Consumers MUST deduplicate on `envelope.event_id`" --
        expressed as a primary key and a state guard rather than as a seen-set.

        A seen-set was the alternative and it is worse in the way that matters: it is
        process-local, so it forgets on restart, which is precisely when the relay is
        republishing the window it died in. The `job_queue` primary key does not forget,
        and the state guard catches the case where the job has already run to completion
        and its queue row is gone (`MOS-STORE-274`).
        """
        job_uuid = envelope["job_id"]
        stored = from_bus_envelope(envelope)

        with tenant_tx(self._conn):
            # TWO statements and not one data-modifying CTE. PostgreSQL runs the
            # sub-statements of a `WITH` against the same snapshot, so an UPDATE in the
            # outer statement cannot see a row its own CTE just inserted -- the claim
            # would find nothing and every first delivery would be silently dropped. They
            # are in one transaction, which is what the atomicity actually requires.
            self._conn.execute(
                """
                INSERT INTO job_queue (job_id, tenant_id, queue, partition_key, envelope)
                SELECT j.id, j.tenant_id, %(queue)s, %(pkey)s, %(envelope)s
                  FROM jobs j
                 WHERE j.id = %(job)s AND j.state = 'QUEUED'
                ON CONFLICT (job_id) DO NOTHING
                """,
                {
                    "queue": self._queue,
                    "pkey": envelope["partition_key"],
                    "envelope": Jsonb(stored),
                    "job": job_uuid,
                },
            )
            row = self._conn.execute(
                """
                UPDATE job_queue q
                   SET lease_owner      = %(worker)s,
                       lease_expires_at = clock_timestamp()
                                          + make_interval(secs => %(lease)s),
                       fence_token      = q.fence_token + 1,
                       claim_count      = q.claim_count + 1
                  FROM jobs j
                 WHERE q.job_id        = %(job)s
                   AND j.id            = q.job_id
                   AND j.state         = 'QUEUED'
                   AND q.lease_owner   IS NULL
                   AND q.available_at <= clock_timestamp()
                RETURNING q.job_id, j.public_id, q.fence_token, q.claim_count,
                          j.max_attempts, q.lease_expires_at, q.envelope
                """,
                {"job": job_uuid, "worker": worker_id, "lease": lease_seconds},
            ).fetchone()

            if row is None:
                # A duplicate delivery of a job that is no longer QUEUED, or one another
                # claimer took between the INSERT and the UPDATE. Both are ordinary under
                # at-least-once and neither is an error: the offset is committed by the
                # caller and the record is discarded. MOS-EXEC-002 -- the bus never gets
                # to say what state the job is in.
                log.debug("dispatch for job %s is already claimed or terminal", job_uuid)
                return None

            return self._finish_claim(row, worker_id, lease_seconds)

    # =================================================================================
    # Introspection
    # =================================================================================
    def depth(self) -> int:
        """Claimable work for this inbox: local queue rows plus unpublished dispatches.

        Driver 1's `depth()` counts `job_queue` rows, which under driver 2 is only the
        work that has already been consumed. A dispatch still sitting in `job_outbox` is
        just as real and just as pending -- it is the reason `medicalos_outbox_lag_seconds`
        exists -- so it is counted, and MOS-EXEC-038's operating-envelope row for queue
        depth keeps meaning the same thing under both drivers.

        UNDERCOUNTS BY THE RECORDS IN FLIGHT: a dispatch the relay has published and no
        consumer has yet taken is on the log and in neither number. Reading it would mean
        consuming it, which a depth probe must not do. The broker's own consumer-group lag
        is the metric for that window, and it belongs to the broker; `depth()` is honest
        about the two things Postgres can answer.
        """
        local = super().depth()
        with tenant_tx(self._conn):
            row = self._conn.execute(
                "SELECT count(*) AS n FROM job_outbox "
                " WHERE published_at IS NULL AND topic = %s",
                (self._queue,),
            ).fetchone()
        return local + (0 if row is None else int(row["n"]))

    def pending_dispatches(self) -> int:
        """Unpublished `job_outbox` rows for this inbox. The relay's backlog."""
        with tenant_tx(self._conn):
            row = self._conn.execute(
                "SELECT count(*) AS n FROM job_outbox "
                " WHERE published_at IS NULL AND topic = %s",
                (self._queue,),
            ).fetchone()
        return 0 if row is None else int(row["n"])

    def close(self) -> None:
        self._consumer.close()


def _assert_driver_implements_port() -> None:
    """The same import-time check driver 1 carries. MOS-EXEC-025: all six methods.

    `runtime_checkable` verifies presence only, which is the half a rename breaks; the
    half it does not check -- that both drivers MEAN the same thing by each method -- is
    what `tests/integration/test_queue_parity.py` is for, and is why that suite is
    parameterised over the driver rather than written twice.
    """
    assert issubclass(KafkaJobQueue, JobQueue), (
        "KafkaJobQueue no longer satisfies the JobQueue port (MOS-EXEC-025)"
    )
    # Driver 2 must not quietly diverge on the methods it inherits. Anything overridden
    # here other than the dispatch hop is a parity risk and should be argued for in this
    # module's docstring first.
    overridden = {
        name for name in ("enqueue", "claim", "claim_lease", "heartbeat", "complete",
                          "fail", "cancel", "reclaim_expired")
        if getattr(KafkaJobQueue, name, None)
        is not getattr(PostgresJobQueue, name, None)
    }
    assert overridden == {"enqueue", "claim_lease"}, (
        "driver 2 overrides "
        f"{sorted(overridden)}; only the dispatch hop (enqueue, claim_lease) differs "
        "between the drivers, because MOS-EXEC-002 keeps Postgres the source of truth "
        "for everything else"
    )


_assert_driver_implements_port()


# `current_tenant` is imported for its side effect on readers, not on code: every method
# above runs inside `tenant_tx()`, which raises `NoTenantContextError` when no tenant is
# bound (MOS-SEC-075). The relay and the runner bind it; this driver never does, and never
# should -- a queue driver that could choose a tenant is a queue driver that could choose
# the wrong one.
_ = current_tenant
