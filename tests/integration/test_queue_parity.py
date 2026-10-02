# SPDX-License-Identifier: Apache-2.0
"""`queue-driver-parity` -- ONE conformance suite, parameterised by driver.

docs/spec/15-delivery.md section 15.1.2, the 0.3.0 gate row, names the check:

    `queue-driver-parity` (the `JobQueue` conformance suite passes identically against
     every registered driver, including the second driver this release introduces)

`MOS-EXEC-032` fixes the minimum content and `MOS-TEST-045` (chapter 14 section 14.5.5)
fixes the check list Q01-Q14. `MOS-TEST-046` fixes the rule that makes this file a suite
and not two files:

    "The 0.3 Kafka driver MUST pass Q01-Q14 with zero changes to the suite. Any check that
     needs a driver-specific variant indicates a leaky port and MUST be resolved by
     changing the port, not the test."

Every test below is therefore written once and run twice, over the `driver` fixture. No
test branches on `driver.name`. The four that are not parameterised are at the bottom
under a heading saying why: they are about the OUTBOX, which exists for driver 2 only by
`MOS-EXEC-040`, and one of them is the Q13 addendum `MOS-TEST-046` itself scopes to Kafka
("Q13 for the Kafka driver ADDITIONALLY asserts ...").

WHAT THE HARNESS IS ALLOWED TO DIFFER ON, AND WHAT IT IS NOT
--------------------------------------------------------------
Construction differs: driver 1 needs a connection, driver 2 needs a connection, a bus
consumer, and a relay running somewhere. That is deployment shape, the same class of
difference as a DSN, and `MOS-TEST-046` is about CHECKS.

Behaviour does not differ, and the one place it visibly could -- "how long after
`create_job_queued` is the job claimable" -- is absorbed by `_claim_within()`, which polls
like a real runner does (`MOS-EXEC-037`: "Every runner MUST also poll `Claim` at
`claim_poll_interval_s`"). Under driver 1 the first poll succeeds; under driver 2 the
first one may precede the relay. A runner cannot tell the difference and neither can this
suite.

WHICH BUS THIS RUNS AGAINST
---------------------------
`medos.bus.memory.LogBroker` unless `MEDOS_KAFKA_BOOTSTRAP` names a broker and
`confluent-kafka` is installed, in which case the SAME tests run against
`medos.bus.kafka.KafkaBus`. `medos/deploy/compose/docker-compose.yml` ships no broker
("no kafka / redpanda   the queue is Postgres. A broker is 0.3.0"), so today that is the
in-process log; the day a broker is added, nothing here changes. `medos/medos/bus/memory.py`
argues that choice at length and states what it does not prove.

Run:  pytest tests/integration/test_queue_parity.py -v
      (MEDOS_TEST_DATABASE_URL points at a Postgres; see conftest.py)

Spec: MOS-EXEC-002, MOS-EXEC-025 .. MOS-EXEC-050, MOS-EXEC-063, MOS-EXEC-064,
      MOS-EXEC-073, MOS-EXEC-075, MOS-EXEC-085, MOS-STORE-274, MOS-TEST-045,
      MOS-TEST-046, MOS-TEST-051, MOS-REL-004.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from medos.bus import kafka as kafka_bus
from medos.bus.envelope import ENVELOPE_SCHEMA, envelope_errors
from medos.bus.memory import LogBroker
from medos.bus.outbox import (
    ADVISORY_LOCK_SQL,
    LAG_ALERT_SECONDS,
    RETENTION_DAYS,
    OutboxRelay,
    RelayBatch,
)
from medos.bus.port import ConsumerConfig, ConsumerEvicted
from medos.bus.topics import ForbiddenTopic, validate_topic, work_topic
from medos.db import repo
from medos.db.queue import JobQueue, LeaseLost, PostgresJobQueue, make_worker_id
from medos.db.queue_kafka import KafkaJobQueue
from medos.db.repo import JobSpec
from medos.db.tenancy import DEFAULT_TENANT_ID, bind_current_tenant, tenant_context
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

STUDY = "1.2.826.0.1.3680043.8.498.22222222222222222222222222222222"
CAPS = ("lung_segmentation", "emphysema_laa")

# MOS-EXEC-032's conformance minimum fixes the CLAIMER count -- "exactly-once claim under
# 32 concurrent claimers" -- and `MOS-TEST-045` Q01 fixes the job count at 512. 512 jobs
# x 2 drivers x a relay hop is minutes of wall clock in the default run, which is how a
# check stops being run. The default here is 128, the shape is identical, and the nightly
# value is `MEDOS_PARITY_JOBS=512`. Stated rather than silently reduced.
Q01_CLAIMERS = 32
Q01_JOBS = int(os.environ.get("MEDOS_PARITY_JOBS", "128"))

# A claim poll budget. MOS-EXEC-037 makes polling the correctness path for driver 1;
# driver 2 additionally waits on the relay. Generous, because a slow CI host failing this
# on timing would be a false red on a gate check (MOS-REL-004 makes a red check a tag
# blocker).
CLAIM_TIMEOUT_S = 10.0

# An envelope that violates MOS-EXEC-075 the way a truncated producer would: the
# required fields are simply absent. Not random bytes -- malformed JSON is refused by
# `jsonb` before any driver sees it, which would test PostgreSQL rather than the
# platform.
_POISON = {
    "event_id": "00000000-0000-0000-0000-000000000000",
    "event_type": "job.dispatch",
    "corrupt": True,
}


def _spec(n: int = 0, study: str | None = None) -> JobSpec:
    return JobSpec(
        study_instance_uid=study or f"{STUDY[:-3]}{n:03d}",
        capability_ids=CAPS,
    )


# =====================================================================================
# The harness
# =====================================================================================
@dataclass
class Harness:
    """One driver, constructed and running. `name` is for test ids ONLY.

    No test below reads `.name` to decide what to assert. If one ever needs to,
    MOS-TEST-046 says that is a leaky port and the port is what must change.
    """

    name: str
    queue: JobQueue
    conn: psycopg.Connection[Any]
    _worker_factory: Callable[[psycopg.Connection[Any]], JobQueue]
    _settle: Callable[[], None]
    _poison: Callable[[str, str], None]
    broker: LogBroker | None = None
    relay: OutboxRelay | None = None
    _closers: list[Callable[[], None]] = field(default_factory=list)

    def worker_queue(self, conn: psycopg.Connection[Any]) -> JobQueue:
        """A second driver instance on its own connection -- a second runner PROCESS.

        Under driver 2 this also gets its own bus consumer, because two runners are two
        members of the consumer group. Sharing one would be modelling one process, which
        is not what a concurrency test is for.
        """
        return self._worker_factory(conn)

    def poison(self, job_id: str, partition_key: str) -> None:
        """Corrupt one pending dispatch in this driver's transport. Q14 only.

        A poison entry cannot be produced through the port: `MOS-EXEC-025` says "No
        component may write to `job_queue` or produce to a work topic directly",
        which is a real defence and is why this check has to reach around it. Driver
        1's transport is a row and driver 2's is a log record, so the reach differs;
        the CHECK that uses it does not (MOS-TEST-046).
        """
        self._poison(job_id, partition_key)

    def settle(self) -> None:
        """Block until every enqueued dispatch is claimable. No-op for driver 1.

        Driver 1 has no relay, so its dispatch is claimable at commit. Driver 2's relay is
        a separate process by `MOS-EXEC-041` clause 1, and this waits for it -- the same
        wait a runner performs implicitly by polling.

        Used only where a test must distinguish "nothing arrived" from "nothing has
        arrived YET". Every positive claim goes through `_claim_within()` instead.
        """
        self._settle()

    def close(self) -> None:
        for fn in reversed(self._closers):
            fn()


def _postgres_harness(
    pg_dsn: str, db: psycopg.Connection[Any]
) -> Iterator[Harness]:
    def poison(job_id: str, _partition_key: str) -> None:
        db.execute(
            "UPDATE job_queue SET envelope = %s "
            " WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)",
            (Jsonb(_POISON), job_id),
        )
        db.commit()

    yield Harness(
        name="postgres",
        queue=PostgresJobQueue(db),
        conn=db,
        _worker_factory=lambda c: PostgresJobQueue(c),
        _settle=lambda: None,
        _poison=poison,
    )


def _kafka_harness(pg_dsn: str, db: psycopg.Connection[Any]) -> Iterator[Harness]:
    """Driver 2, with a relay running exactly as `MOS-EXEC-041` clause 1 describes.

    A fresh `LogBroker` per test. A shared one would carry the previous test's records
    across the `db` fixture's TRUNCATE, and the job ids they name would no longer exist --
    which is a realistic condition (it is exactly what a dispatch for a purged job looks
    like) but not one every test should be paying for.
    """
    closers: list[Callable[[], None]] = []

    if kafka_bus.available():
        bus: Any = kafka_bus.KafkaBus()
        broker: LogBroker | None = None
        producer = bus.producer()
        consumer_factory = bus.consumer
    else:
        broker = LogBroker()
        producer = broker.producer()
        consumer_factory = broker.consumer

    closers.append(producer.close)

    relay_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    closers.append(relay_conn.close)
    relay = OutboxRelay(relay_conn, producer, tenants=(DEFAULT_TENANT_ID,))
    assert relay.start(interval_s=0.005), (
        "the relay did not win pg_try_advisory_lock(hashtext('mos.outbox.relay')); "
        "another relay holds it (MOS-EXEC-041 clause 1)"
    )
    closers.append(relay.stop)

    obs_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    closers.append(obs_conn.close)

    def settle() -> None:
        deadline = time.monotonic() + CLAIM_TIMEOUT_S
        while time.monotonic() < deadline:
            with tenant_context(DEFAULT_TENANT_ID):
                obs_conn.execute(
                    "SELECT set_config('medicalos.tenant_id', %s, false)",
                    (DEFAULT_TENANT_ID,),
                )
                row = obs_conn.execute(
                    "SELECT count(*) AS n FROM job_outbox WHERE published_at IS NULL"
                ).fetchone()
            if row is not None and row["n"] == 0:
                return
            time.sleep(0.005)
        raise AssertionError(
            "the outbox relay did not drain within "
            f"{CLAIM_TIMEOUT_S}s (medicalos_outbox_lag_seconds is rising; MOS-EXEC-041.6)"
        )

    def poison(_job_id: str, partition_key: str) -> None:
        settle()
        assert broker is not None, (
            "Q14 corrupts a record in place; against a real broker that is an admin "
            "operation this harness does not perform"
        )
        assert broker.corrupt_first(
            work_topic("medos.slice"), partition_key, _POISON
        )

    main_consumer = consumer_factory(ConsumerConfig())
    closers.append(main_consumer.close)

    def worker_factory(c: psycopg.Connection[Any]) -> JobQueue:
        cons = consumer_factory(ConsumerConfig())
        closers.append(cons.close)
        return KafkaJobQueue(c, cons)

    h = Harness(
        name="kafka",
        queue=KafkaJobQueue(db, main_consumer),
        conn=db,
        _worker_factory=worker_factory,
        _settle=settle,
        _poison=poison,
        broker=broker,
        relay=relay,
        _closers=closers,
    )
    try:
        yield h
    finally:
        h.close()


@pytest.fixture(params=["postgres", "kafka"])
def driver(
    request: pytest.FixtureRequest, pg_dsn: str, db: psycopg.Connection[Any]
) -> Iterator[Harness]:
    """THE parameterisation. Every check below runs twice, unchanged (MOS-TEST-046)."""
    maker = _postgres_harness if request.param == "postgres" else _kafka_harness
    yield from maker(pg_dsn, db)


# =====================================================================================
# Helpers used by every check. Driver-agnostic by construction.
# =====================================================================================
def _create(driver: Harness, spec: JobSpec) -> Any:
    """Create and enqueue one job, with one selected series.

    The series verdict is not decoration. `jobs_completed_consumed_series` is a CHECK --
    "`state <> 'COMPLETED' OR cardinality(selected_series_uids) >= 1`", the surviving half
    of `MOS-EXEC-086` -- so a job with no selected series cannot reach `COMPLETED`, and
    every check below that completes a job would fail on a constraint that has nothing to
    do with queueing. `MOS-STORE-270` is why it is a row and not a blob.
    """
    created = repo.create_job_queued(driver.conn, driver.queue, spec)
    if created.created:
        # `record_series_verdicts()` and NOT `create_job_queued(series_verdicts=...)`:
        # the latter writes the `job_series` rows only, while this one also denormalises
        # the selected set onto `jobs.selected_series_uids`, which is the column the CHECK
        # reads. Passing both would write the rows twice.
        repo.record_series_verdicts(
            driver.conn,
            created.job_id,
            (
                repo.SeriesVerdict(
                    series_instance_uid=f"{spec.study_instance_uid}.1",
                    decision="selected",
                    selector_name="parity",
                    rank=1,
                ),
            ),
        )
    driver.conn.commit()
    return created


def _claim_within(
    q: JobQueue, worker_id: str, *, lease_seconds: int = 60,
    timeout_s: float = CLAIM_TIMEOUT_S,
) -> str | None:
    """Poll `Claim` until it yields, or the budget runs out. What a runner does.

    MOS-EXEC-026: "`Claim` MUST NOT block. Runners obtain work by calling `Claim` when a
    notification arrives OR when the poll timer fires, whichever is first." So the loop is
    in the caller, in the suite as in the runner, and a driver whose `Claim` blocked would
    be a driver that fails MOS-EXEC-026 rather than one this helper accommodates.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        job_id = q.claim(worker_id, lease_seconds)
        if job_id is not None:
            return job_id
        time.sleep(0.01)
    return None


def _state(conn: psycopg.Connection[Any], job_id: str) -> str:
    conn.commit()   # refresh this connection's snapshot
    job = repo.get_job(conn, job_id)
    assert job is not None, f"{job_id} vanished"
    return str(job["state"])


# =====================================================================================
# Q01  single delivery under contention          (MOS-EXEC-032, MOS-TEST-045)
# =====================================================================================
def test_q01_single_delivery_under_32_concurrent_claimers(
    pg_dsn: str, driver: Harness
) -> None:
    """MOS-EXEC-032's first conformance requirement: "exactly-once claim under 32
    concurrent claimers". MOS-TEST-045 Q01: "the union of claims is exactly the N jobs,
    each once."

    The two drivers get there by different mechanisms and that is the point of running one
    suite over both: driver 1 by `SELECT ... FOR UPDATE SKIP LOCKED`, driver 2 by
    partition assignment plus the `job_queue` primary key absorbing an at-least-once
    redelivery. Neither mechanism is visible here, which is what a port is for.
    """
    expected: set[str] = set()
    for i in range(Q01_JOBS):
        expected.add(_create(driver, _spec(100 + i)).job_id)
    assert len(expected) == Q01_JOBS
    driver.settle()

    barrier = threading.Barrier(Q01_CLAIMERS)
    claims: list[tuple[str, str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def claimer() -> None:
        wid = make_worker_id("parity")
        # A new thread starts with an EMPTY contextvars context; the fixture's tenant
        # binding is invisible here. Same rule the worker's heartbeat thread follows
        # (medos/medos/worker/runner.py) and deliberately not hidden inside tenant_tx.
        bind_current_tenant(DEFAULT_TENANT_ID)
        conn = None
        try:
            conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
            wq = driver.worker_queue(conn)
            barrier.wait(timeout=60)
            idle = 0
            while idle < 2:
                job_id = wq.claim(wid, 60)
                if job_id is None:
                    idle += 1
                    time.sleep(0.02)
                    continue
                idle = 0
                with lock:
                    claims.append((wid, job_id))
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            with lock:
                errors.append(exc)
            try:
                barrier.abort()
            except Exception:
                pass
        finally:
            if conn is not None:
                conn.close()

    threads = [threading.Thread(target=claimer, name=f"q01-{i}")
               for i in range(Q01_CLAIMERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)

    assert not errors, f"claimers raised: {errors[:3]!r}"
    assert all(not t.is_alive() for t in threads), "a claimer did not finish"

    claimed = [job_id for _w, job_id in claims]
    dupes = [j for j, c in Counter(claimed).items() if c > 1]
    assert not dupes, f"the same job was claimed more than once: {dupes[:5]}"
    assert set(claimed) == expected, (
        f"{driver.name}: claimed {len(set(claimed))} of {Q01_JOBS}; "
        f"missing {sorted(expected - set(claimed))[:5]}"
    )

    driver.conn.commit()
    for _wid, job_id in claims[:20]:
        job = repo.get_job(driver.conn, job_id)
        assert job is not None and job["state"] == "RUNNING"
        # MOS-EXEC-035: the first delivery carries claim_count = 1 and attempt = 1, in
        # both drivers. Driver 2's redelivery absorption must not inflate it.
        assert job["attempt"] == 1, f"{job_id} is on attempt {job['attempt']}"


# =====================================================================================
# Q02  lease exclusivity                          (MOS-TEST-045)
# =====================================================================================
def test_q02_lease_exclusivity(pg_dsn: str, driver: Harness) -> None:
    """"after `Claim`, a second `Claim` for the same job returns nothing until `lease_ttl`
    elapses" (Q02).

    Asserted over an inbox holding exactly one job, so "returns nothing" is unambiguous:
    a second claimer that got anything at all got the leased job.
    """
    created = _create(driver, _spec(1))
    driver.settle()

    a = _claim_within(driver.queue, make_worker_id("a"), lease_seconds=60)
    assert a == created.job_id

    other = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    try:
        bind_current_tenant(DEFAULT_TENANT_ID)
        b_queue = driver.worker_queue(other)
        driver.settle()
        assert b_queue.claim(make_worker_id("b"), 60) is None, (
            "a leased job was handed to a second claimer"
        )
    finally:
        other.close()


# =====================================================================================
# Q03  fencing                                    (MOS-EXEC-027, MOS-EXEC-032)
# =====================================================================================
def test_q03_fencing_after_a_simulated_lease_expiry(pg_dsn: str, driver: Harness) -> None:
    """MOS-EXEC-032: "fencing after a simulated lease expiry". Q03: "after lease expiry
    and re-claim by B, A's `Complete` is rejected with `stale_lease` and does not change
    job state."

    MOS-EXEC-027 states the failure this prevents in one sentence: "a runner whose process
    froze for three minutes wakes up after its lease was reclaimed and another runner took
    the job, and then writes a result for a job it no longer owns."

    A one-second lease and a real sleep, rather than a clock injection: the predicate under
    test is `lease_expires_at > clock_timestamp()` evaluated inside PostgreSQL, and a
    Python-side clock would not reach it.
    """
    created = _create(driver, _spec(2))
    driver.settle()

    a_worker = make_worker_id("frozen")
    assert _claim_within(driver.queue, a_worker, lease_seconds=1) == created.job_id

    time.sleep(1.2)   # A's lease expires while A is "frozen"

    # The reclaimer releases it (T10) and B claims with fence_token + 1.
    other = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    try:
        bind_current_tenant(DEFAULT_TENANT_ID)
        b_queue = driver.worker_queue(other)
        reclaimed = b_queue.reclaim_expired()  # type: ignore[attr-defined]
        other.commit()
        assert [r.job_id for r in reclaimed] == [created.job_id]

        b_worker = make_worker_id("b")
        assert _claim_within(b_queue, b_worker, lease_seconds=60) == created.job_id
        other.commit()
    finally:
        other.close()

    driver.conn.commit()
    lease = driver.queue.lease_state(created.job_id)  # type: ignore[attr-defined]
    assert lease is not None and lease["fence_token"] == 2, (
        "MOS-EXEC-027: fence_token MUST increment on every claim"
    )

    # A wakes up and tries to finish. Both the owner check and the fence reject it.
    with pytest.raises(LeaseLost):
        driver.queue.complete(created.job_id, a_worker)
    driver.conn.rollback()

    assert _state(driver.conn, created.job_id) == "RUNNING", (
        "the frozen runner changed the state of a job it no longer owns"
    )


# =====================================================================================
# Q04  heartbeat                                  (MOS-EXEC-028)
# =====================================================================================
def test_q04_heartbeat_extends_the_lease_and_reports_loss(
    pg_dsn: str, driver: Harness
) -> None:
    """Q04: "`Heartbeat` extends the lease". MOS-EXEC-028 adds the half that matters more:
    a runner whose heartbeat returns false "MUST abort immediately, MUST NOT call
    `Complete` or `Fail`, MUST NOT STOW any DICOM object, and MUST release GPU memory."

    `max_lease_extensions` from Q04's pass condition has no column in chapter 12 section
    12.10 and no requirement in chapter 5 -- the bound on a runaway attempt is
    `jobs.attempt_deadline_at` (`MOS-EXEC-070`), which is absolute and is set at claim.
    The extension COUNT is therefore not asserted here and the divergence is in this
    component's report rather than invented as a column.
    """
    created = _create(driver, _spec(3))
    driver.settle()
    worker = make_worker_id("hb")
    assert _claim_within(driver.queue, worker, lease_seconds=2) == created.job_id

    before = driver.queue.lease_state(created.job_id)["lease_expires_at"]  # type: ignore[index]
    time.sleep(0.2)
    assert driver.queue.heartbeat(created.job_id, worker, 60) is True
    after = driver.queue.lease_state(created.job_id)["lease_expires_at"]  # type: ignore[index]
    assert after > before, "Heartbeat did not extend the lease (MOS-EXEC-028)"

    # A stranger's heartbeat is a lost lease, not an extension.
    assert driver.queue.heartbeat(created.job_id, make_worker_id("stranger"), 60) is False


def test_the_heartbeat_runs_from_a_separate_connection_under_both_drivers(
    pg_dsn: str, driver: Harness
) -> None:
    """MOS-EXEC-047 step 4 and MOS-EXEC-049, at the port.

    "Heartbeat the lease from a dedicated ticker that is INDEPENDENT of the inference
    call" -- and `medos/medos/worker/runner.py` implements that as a separate thread on a
    separate connection, because the claim's connection is inside the transaction that
    may still roll back.

    The property this pins is that the ticker needs nothing from the transport. Driver 2's
    heartbeat is driver 1's, inherited unchanged, so the heartbeat thread holds no bus
    consumer, joins no consumer group and cannot be evicted from one. That is why
    `MOS-EXEC-048`'s rebalance hazard cannot reach the lease: the two are on different
    wires.
    """
    created = _create(driver, _spec(20))
    driver.settle()
    worker = make_worker_id("ticker")
    assert _claim_within(driver.queue, worker, lease_seconds=3) == created.job_id
    driver.conn.commit()

    ticker_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    try:
        bind_current_tenant(DEFAULT_TENANT_ID)
        # Driver 1 deliberately, in BOTH cases: the heartbeat is a `job_queue` UPDATE and
        # nothing else, so a ticker that constructed a bus consumer would be joining a
        # consumer group to do no consuming.
        ticker = PostgresJobQueue(ticker_conn)
        for _ in range(4):
            time.sleep(0.15)
            assert ticker.heartbeat(created.job_id, worker, 3) is True

        driver.conn.commit()
        assert _state(driver.conn, created.job_id) == "RUNNING"

        # And the same ticker reports the loss, which is the half MOS-EXEC-028 makes a
        # MUST: a runner that keeps going after a false heartbeat writes a result for a
        # job it no longer owns.
        driver.queue.fail_ex(  # type: ignore[attr-defined]
            created.job_id, worker, retryable=False,
            failure_class="internal", failure_code="abandoned",
        )
        driver.conn.commit()
        assert ticker.heartbeat(created.job_id, worker, 3) is False
    finally:
        ticker_conn.close()


# =====================================================================================
# Q05  Complete is fenced and terminal            (MOS-EXEC-029, MOS-STORE-274)
# =====================================================================================
def test_q05_complete_deletes_the_queue_row_and_is_not_repeatable(
    driver: Harness,
) -> None:
    """Q05 asks for `already_complete` on a second `Complete`; the port has no such
    return, so the assertion is on the property Q05 is protecting: the second call
    performs NO WRITE.

    `LeaseLost` is what this port raises, and it is stronger than a soft
    `already_complete`: after `MOS-STORE-274` deletes the queue row there is no lease left
    to prove ownership with, so a second `Complete` cannot be distinguished from a
    stranger's and both are refused. Reported as a wording divergence, not implemented as
    a second return value.
    """
    created = _create(driver, _spec(4))
    driver.settle()
    worker = make_worker_id("done")
    assert _claim_within(driver.queue, worker) == created.job_id

    driver.queue.complete(created.job_id, worker)
    driver.conn.commit()
    assert _state(driver.conn, created.job_id) == "COMPLETED"
    assert driver.queue.lease_state(created.job_id) is None, (  # type: ignore[attr-defined]
        "MOS-STORE-274: the queue row is deleted at the terminal state"
    )

    with pytest.raises(LeaseLost):
        driver.queue.complete(created.job_id, worker)
    driver.conn.rollback()
    assert _state(driver.conn, created.job_id) == "COMPLETED"


# =====================================================================================
# Q06  retryable failure                          (MOS-EXEC-030, MOS-EXEC-035)
# =====================================================================================
def test_q06_retryable_failure_re_offers_and_attempt_increments_by_one(
    driver: Harness,
) -> None:
    """Q06: "`Fail(retryable=true)` re-offers after the declared backoff; `attempt`
    increments by exactly 1."

    This is the check that made driver 2 read `job_queue` before it reads the bus. A retry
    is not a redelivery: the dispatch record's offset was committed at the first claim, so
    if the re-offer were a transport concern the job would never come back. Postgres holds
    the re-offer in both drivers, which is `MOS-EXEC-002` doing its job.

    MOS-EXEC-035: `claim_count` is incremented by the CLAIM statement and nowhere else --
    "Neither `Fail`, nor the reclaimer, nor the reconciler may increment it" -- so the
    attempt moves from 1 to 2 on the RE-CLAIM, not on the failure.
    """
    created = _create(driver, _spec(5))
    driver.settle()
    worker = make_worker_id("flaky")
    assert _claim_within(driver.queue, worker) == created.job_id
    assert repo.get_job(driver.conn, created.job_id)["attempt"] == 1  # type: ignore[index]

    decision = driver.queue.fail_ex(  # type: ignore[attr-defined]
        created.job_id, worker, retryable=True,
        failure_class="transient_infrastructure", failure_code="pacs_timeout",
    )
    driver.conn.commit()
    assert decision.next_state == "QUEUED"
    assert decision.attempt == 1, "Fail must not consume budget (MOS-EXEC-035)"
    assert _state(driver.conn, created.job_id) == "QUEUED"

    # The backoff is real: the job is not claimable this instant.
    lease = driver.queue.lease_state(created.job_id)  # type: ignore[attr-defined]
    assert lease is not None and lease["lease_owner"] is None
    assert lease["claim_count"] == 1

    # Make it claimable without waiting out section 5.6.4's jittered backoff, which is up
    # to 5 s on attempt 1 and up to 600 s later. The backoff VALUE is driver 1's tested
    # elsewhere (`job_backoff()`); what this check is about is that the re-offer happens
    # at all and that the attempt then increments by exactly one.
    driver.conn.execute(
        "UPDATE job_queue SET available_at = clock_timestamp() "
        " WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)",
        (created.job_id,),
    )
    driver.conn.commit()

    assert _claim_within(driver.queue, make_worker_id("retry")) == created.job_id
    driver.conn.commit()
    assert repo.get_job(driver.conn, created.job_id)["attempt"] == 2  # type: ignore[index]


# =====================================================================================
# Q07  terminal failure                           (MOS-EXEC-030, MOS-EXEC-066)
# =====================================================================================
def test_q07_terminal_failure_is_terminal_and_not_re_offered(driver: Harness) -> None:
    """Q07: "`Fail(retryable=false)` moves the message to `<topic>.dlq` without further
    attempts."

    The "without further attempts" half is asserted. The DLQ DESTINATION is not, because
    neither driver has one: `job_dead_letter` (`MOS-EXEC-066`) is not a table in this
    deployment and `medos/medos/db/queue.py` has said so since weeks 1-2. Giving driver 2 a
    `<topic>.dlq` while driver 1 has nothing is precisely the asymmetry
    `queue-driver-parity` exists to forbid, so the gap is reported rather than
    half-closed. See this component's report.
    """
    created = _create(driver, _spec(6))
    driver.settle()
    worker = make_worker_id("doomed")
    assert _claim_within(driver.queue, worker) == created.job_id

    decision = driver.queue.fail_ex(  # type: ignore[attr-defined]
        created.job_id, worker, retryable=False,
        failure_class="invalid_result_bundle", failure_code="output_implausible",
    )
    driver.conn.commit()
    assert decision.next_state == "FAILED"
    assert _state(driver.conn, created.job_id) == "FAILED"
    assert driver.queue.lease_state(created.job_id) is None  # type: ignore[attr-defined]

    driver.settle()
    assert _claim_within(
        driver.queue, make_worker_id("vulture"), timeout_s=0.6
    ) is None, "a terminally failed job was re-offered"


# =====================================================================================
# Q08  load shed does not consume budget          (MOS-EXEC-064, MOS-EXEC-032)
# =====================================================================================
def test_q08_load_shed_does_not_consume_retry_budget(driver: Harness) -> None:
    """MOS-EXEC-032: "`Fail(load_shed)` not consuming budget". Q08 says the same.

    The mechanism is structural rather than conditional, and worth stating because it is
    why this passes without a special case in either driver: `claim_count` is written by
    the claim statement and by nothing else (`MOS-EXEC-035`), and `jobs.attempt` is copied
    from it. A shed therefore cannot consume budget even if someone wanted it to -- there
    is no statement that would.

    MOS-EXEC-064's first mechanism, "A runner MUST call `Claim` only when it has that many
    free execution slots", lives in `medos/medos/worker/runner.py`'s bounded semaphore and is
    the case where the shed happens BEFORE a claim; that one consumes nothing by never
    reaching the queue at all.
    """
    created = _create(driver, _spec(7))
    driver.settle()
    worker = make_worker_id("shed")
    assert _claim_within(driver.queue, worker) == created.job_id

    before = driver.queue.lease_state(created.job_id)["claim_count"]  # type: ignore[index]
    decision = driver.queue.fail_ex(  # type: ignore[attr-defined]
        created.job_id, worker, retryable=True,
        failure_class="load_shed", failure_code="no_free_slot",
    )
    driver.conn.commit()

    assert decision.next_state == "QUEUED"
    after = driver.queue.lease_state(created.job_id)["claim_count"]  # type: ignore[index]
    assert after == before, (
        f"MOS-EXEC-064: a load shed consumed retry budget ({before} -> {after})"
    )
    assert repo.get_job(driver.conn, created.job_id)["attempt"] == before  # type: ignore[index]


# =====================================================================================
# Q09  enqueue idempotence                        (MOS-EXEC-053, MOS-EXEC-032)
# =====================================================================================
def test_q09_enqueue_dedup_under_two_concurrent_creations(
    pg_dsn: str, driver: Harness
) -> None:
    """MOS-EXEC-032: "`Enqueue` dedup under two concurrent creations with the same derived
    key". Q09: "two `Enqueue` calls with the same `(tenant_id, idempotency_key)` yield one
    queue entry and one `Job`."

    CONCURRENT and not sequential, because the sequential case is answered by a SELECT and
    the concurrent one is answered by the unique index. Two connections, one barrier, one
    derived key.
    """
    spec = _spec(8)
    results: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)
    lock = threading.Lock()

    def creator() -> None:
        bind_current_tenant(DEFAULT_TENANT_ID)
        conn = None
        try:
            conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
            q = driver.worker_queue(conn)
            barrier.wait(timeout=30)
            created = repo.create_job_queued(conn, q, spec)
            conn.commit()
            with lock:
                results.append(created)
        except BaseException as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)
        finally:
            if conn is not None:
                conn.close()

    threads = [threading.Thread(target=creator) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, f"creators raised: {errors!r}"
    assert len(results) == 2
    assert results[0].job_id == results[1].job_id, "the derived key did not collapse"
    assert {r.created for r in results} == {True, False}, (
        "exactly one creation must win (MOS-EXEC-053)"
    )

    driver.conn.commit()
    n = driver.conn.execute("SELECT count(*) AS n FROM jobs").fetchone()
    assert n is not None and n["n"] == 1

    # One dispatch, not two. Under driver 1 that is one `job_queue` row; under driver 2
    # one `job_outbox` row and therefore one record on the log. `depth()` is the port's
    # answer to "how much work is pending" in both.
    driver.settle()
    assert driver.queue.depth() <= 1, (  # type: ignore[attr-defined]
        "a deduplicated creation produced a second dispatch"
    )


# =====================================================================================
# Q10  atomicity of creation                      (MOS-EXEC-034/040, MOS-TEST-051)
# =====================================================================================
@pytest.mark.parametrize("explode_after_dispatch", [False, True])
def test_q10_a_crash_inside_creation_leaves_neither_row(
    driver: Harness, explode_after_dispatch: bool
) -> None:
    """Q10, at chapter 14's fault point `triage.after_job_insert_before_commit`: "after
    crash either both the `Job` row and the queue entry exist, or neither."

    This is the check the outbox exists to let driver 2 pass. Driver 1 passes it because
    the job row and the queue row are one transaction (`MOS-EXEC-034`). Driver 2 passes it
    for the same reason with a different second row: the job row and the OUTBOX row are
    one transaction (`MOS-EXEC-040`). A driver 2 that produced to the broker inside
    `enqueue()` would fail the `explode_after_dispatch=True` case and would fail it
    unrecoverably, because a broker does not roll back.

    Both crash points are exercised: before the dispatch row is written (would leave a job
    no worker can ever see) and after it (would leave a dispatched job whose `jobs` row was
    never committed).
    """

    class ExplodingQueue:
        def __init__(self, real: JobQueue) -> None:
            self._real = real

        def enqueue(self, job_id: str) -> None:
            if explode_after_dispatch:
                self._real.enqueue(job_id)
            raise RuntimeError("simulated process death during enqueue")

    with pytest.raises(RuntimeError, match="simulated process death"):
        repo.create_job_queued(
            driver.conn, ExplodingQueue(driver.queue), _spec(9)
        )
    driver.conn.rollback()

    for table in ("jobs", "job_queue", "job_events", "job_outbox"):
        row = driver.conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        assert row is not None and row["n"] == 0, (
            f"{driver.name}: a {table} row survived a crash during creation"
        )

    # And the connection is usable afterwards: the rollback was clean, not a wedged
    # transaction every subsequent request would fail on.
    created = _create(driver, _spec(9))
    assert created.created is True and created.state == "QUEUED"


# =====================================================================================
# Q11  per-key order                              (MOS-EXEC-044)
# =====================================================================================
def test_q11_claims_of_one_partition_key_occur_in_enqueue_order(
    driver: Harness,
) -> None:
    """Q11: "for one `<tenant_id>:<study_instance_uid>` key, claims occur in enqueue
    order."

    MOS-EXEC-044 is why: "This co-locates all work for one study on one partition, which
    is what makes a 'one job per study is already running' check meaningful." Driver 1 gets
    the order from `ORDER BY q.priority, q.available_at, q.enqueued_at`; driver 2 gets it
    from one partition key landing on one partition with one in-flight record at a time.
    One claimer, so the assertion is about the QUEUE's order and not about scheduling.

    Three jobs on one study need three different derived idempotency keys, or
    `MOS-EXEC-053` collapses them into one. `requested_outputs` is part of the derivation
    material and is not part of the partition key, so varying it gives three distinct jobs
    on one study -- which is the shape Q11 is actually about.
    """
    study = f"{STUDY[:-3]}777"
    order: list[str] = []
    for outputs in (("SEG",), ("SR",), ("SEG", "SR")):
        spec = JobSpec(
            study_instance_uid=study, capability_ids=CAPS, requested_outputs=outputs
        )
        order.append(_create(driver, spec).job_id)
    assert len(set(order)) == 3
    driver.settle()

    worker = make_worker_id("ordered")
    got: list[str] = []
    for _ in range(3):
        job_id = _claim_within(driver.queue, worker)
        assert job_id is not None
        got.append(job_id)
        # Free the lease so the next claim can proceed; the ORDER is what is under test,
        # not concurrency. Completing is the cleanest release and it also deletes the
        # queue row, so a re-claim of the same job cannot masquerade as the next one.
        driver.queue.complete(job_id, worker)
        driver.conn.commit()

    assert got == order, f"{driver.name}: claims out of enqueue order: {got} != {order}"


# =====================================================================================
# Q12  cancel is reserved                         (MOS-EXEC-031, MOS-EXEC-073/074)
# =====================================================================================
def test_q12_cancel_is_reserved_in_both_drivers(driver: Harness) -> None:
    """Q12: "`Cancel` returns `unimplemented` ... the suite asserts the error, not the
    behaviour."

    REPORTED: section 5.10's prose says a truthful cancellation "is 0.3 work", while the
    normative bullets of `MOS-EXEC-073` say `JobQueue.Cancel` MUST return
    `ErrCancellationNotImplemented` with no version qualifier, `MOS-EXEC-074` requires CI
    to assert that no code path produces `CANCELLED` with no version qualifier, and
    section 15.2.6's contents list for 0.3.0 does not mention cancellation at all. Three
    normative statements against one aside, so cancellation stays reserved in driver 2 and
    the ambiguity is in this component's report.
    """
    created = _create(driver, _spec(10))
    with pytest.raises(NotImplementedError):
        driver.queue.cancel(created.job_id)
    driver.conn.rollback()
    assert _state(driver.conn, created.job_id) == "QUEUED", (
        "MOS-EXEC-031: Cancel MUST NOT mutate any row"
    )

    # MOS-EXEC-074, at the database: CANCELLED has no row in job_state_transition, so
    # job_transition() refuses it even if a driver tried.
    rows = driver.conn.execute(
        "SELECT count(*) AS n FROM job_state_transition WHERE to_state = 'CANCELLED'"
    ).fetchone()
    assert rows is not None and rows["n"] == 0


# =====================================================================================
# Q13  no loss under restart                      (MOS-TEST-045)
# =====================================================================================
def test_q13_no_loss_when_the_claiming_process_restarts_mid_flight(
    pg_dsn: str, driver: Harness
) -> None:
    """Q13: "N enqueued, broker/database restarted mid-flight, all N eventually claimed
    and completed."

    A killed claimer, which is the restart shape a queue driver actually has to survive:
    the process that consumed a dispatch dies before it completes the job. Under driver 1
    its lease expires and the reclaimer re-offers the row; under driver 2 the same, because
    the claim was persisted to `job_queue` before the offset was committed
    (`MOS-EXEC-047` step 2). The alternative -- offset committed first -- loses the
    dispatch here, permanently and silently, which is the whole reason that step is
    ordered the way it is.
    """
    n = 12
    expected = {_create(driver, _spec(200 + i)).job_id for i in range(n)}
    driver.settle()

    # A short-lived runner claims half of them and dies without completing anything.
    dead_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    bind_current_tenant(DEFAULT_TENANT_ID)
    dead_queue = driver.worker_queue(dead_conn)
    dead_worker = make_worker_id("doomed-runner")
    taken = 0
    while taken < n // 2:
        if _claim_within(dead_queue, dead_worker, lease_seconds=1, timeout_s=5.0) is None:
            break
        taken += 1
    dead_conn.commit()
    dead_conn.close()          # SIGKILL, as far as the queue is concerned
    assert taken == n // 2, f"only {taken} of {n // 2} were claimed before the kill"

    time.sleep(1.2)            # the dead runner's leases expire

    survivor = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    try:
        bind_current_tenant(DEFAULT_TENANT_ID)
        q = driver.worker_queue(survivor)
        completed: set[str] = set()
        deadline = time.monotonic() + 60
        while len(completed) < n and time.monotonic() < deadline:
            q.reclaim_expired()          # type: ignore[attr-defined]
            survivor.commit()
            job_id = _claim_within(q, make_worker_id("survivor"), timeout_s=1.0)
            if job_id is None:
                continue
            q.complete(job_id, q.lease_state(job_id)["lease_owner"])  # type: ignore[index]
            survivor.commit()
            completed.add(job_id)
        assert completed == expected, (
            f"{driver.name}: {len(expected - completed)} jobs were lost across the "
            f"restart: {sorted(expected - completed)[:5]}"
        )
    finally:
        survivor.close()


# =====================================================================================
# Q14  a malformed entry does not block its partition
# =====================================================================================
def test_q14_a_malformed_entry_does_not_block_later_entries_on_the_same_key(
    driver: Harness,
) -> None:
    """Q14: "a queue entry whose payload fails the message schema goes to the DLQ on
    attempt 1 AND does not block later entries on the same partition key."

    The second half is asserted for both drivers; the first is not implemented in either,
    for the reason Q07's docstring gives. What is asserted is the property that actually
    protects the platform: one bad entry on a partition key does not stop the good ones
    behind it. A blocked partition is how a single malformed dispatch becomes an outage
    for every study that hashes to it.

    The corruption is written directly into the transport row, which is the only way to
    produce one: `MOS-EXEC-025` forbids any component writing to `job_queue` or producing
    to a work topic directly, so a poison entry cannot arise through the port at all. That
    is a real defence and it is why this check has to cheat to exist.
    """
    study = f"{STUDY[:-3]}888"
    bad = _create(driver, JobSpec(study_instance_uid=study, capability_ids=CAPS,
                                  requested_outputs=("SEG",)))
    good = _create(driver, JobSpec(study_instance_uid=study, capability_ids=CAPS,
                                   requested_outputs=("SR",)))
    driver.settle()

    driver.poison(bad.job_id, f"{DEFAULT_TENANT_ID}:{study}")
    driver.settle()

    worker = make_worker_id("q14")
    seen: set[str] = set()
    deadline = time.monotonic() + 15
    while good.job_id not in seen and time.monotonic() < deadline:
        job_id = _claim_within(driver.queue, worker, timeout_s=1.0)
        if job_id is None:
            continue
        seen.add(job_id)
        driver.queue.complete(job_id, worker)
        driver.conn.commit()

    assert good.job_id in seen, (
        f"{driver.name}: a malformed entry blocked a later entry on partition key "
        f"<tenant>:{study}"
    )


# =====================================================================================
# Envelope parity: the two drivers store the SAME thing in job_queue.envelope
# =====================================================================================
def test_both_drivers_store_the_same_dispatch_envelope(driver: Harness) -> None:
    """`job_queue.envelope` is `MOS-EXEC-075`'s and it is read by whatever runs the job.

    Driver 1 writes it at enqueue; driver 2 reconstructs it at claim from the record it
    consumed (`medos.bus.envelope.from_bus_envelope`). If those two disagreed, a runner
    would behave differently under the two drivers while every check above still passed --
    the kind of parity hole a port-level suite is exactly able to miss. So the shape is
    pinned here, in a check that runs under both.
    """
    created = _create(driver, _spec(11))
    driver.settle()
    worker = make_worker_id("env")
    assert _claim_within(driver.queue, worker) == created.job_id
    driver.conn.commit()

    row = driver.conn.execute(
        "SELECT envelope FROM job_queue "
        " WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)",
        (created.job_id,),
    ).fetchone()
    assert row is not None
    env = row["envelope"]

    assert set(env) == {
        "schema_ref", "event_id", "event_type", "event_time", "producer", "queue",
        "job_id", "trace_id", "correlation_id", "payload",
    }, f"{driver.name} stored {sorted(env)}"
    assert env["event_type"] == "job.dispatch"
    assert env["job_id"] == created.job_id           # MOS-STORE-357: the PUBLIC id
    assert env["queue"] == work_topic("medos.slice")  # MOS-EXEC-033: one name, both drivers
    assert set(env["payload"]) == {
        "service_id", "service_version", "capability_ids", "study_instance_uid",
        "prior_study_instance_uids", "requested_outputs", "clinical_use_mode",
        "idempotency_key", "deadline_at",
    }
    # MOS-STORE-272 / MOS-EXEC-079: no PHI. DICOM UIDs are permitted; nothing else
    # patient-linked may appear, in either driver.
    forbidden = {"patient_name", "patient_id", "patient_birth_date", "accession_number",
                 "study_description", "series_description"}
    assert not (forbidden & set(env["payload"])), "PHI reached job_queue.envelope"


# =====================================================================================
# Driver 2 only: the outbox, which MOS-EXEC-040 gives to driver 2 and to nothing else
#
# These are NOT parameterised, and the reason is the requirement rather than convenience:
# "The outbox exists for driver 2 only; introducing it in 0.1 would be cost with no
# benefit" (MOS-EXEC-040). A parameterised outbox check would be asserting that driver 1
# has a thing the spec says it must not have.
# =====================================================================================
@pytest.fixture()
def kafka(pg_dsn: str, db: psycopg.Connection[Any]) -> Iterator[Harness]:
    yield from _kafka_harness(pg_dsn, db)


def test_outbox_relay_republishes_after_the_fault_point(
    pg_dsn: str, db: psycopg.Connection[Any]
) -> None:
    """Chapter 14's `relay.after_produce_before_mark_sent` (`MOS-TEST-051`).

    "at-least-once produce, deduplicated by `event_id`". The relay is killed between the
    broker's acknowledgement and the `published_at` UPDATE; on restart it republishes, so
    the consumer sees the dispatch TWICE. Exactly one job runs, because the consumer-side
    dedup is the `job_queue` primary key plus the `state = 'QUEUED'` guard, not a
    process-local seen-set that a restart would have forgotten.

    Chapter 5 section 5.13's crash-window table says the same of the other end: "After the
    `results` + `COMPLETED` commit, before the event is published (driver 2) -- relay
    publishes on restart -- consumer dedupes on `event_id`; exactly one webhook."
    """
    broker = LogBroker()
    producer = broker.producer()
    relay_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    obs = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    consumer = broker.consumer(ConsumerConfig())
    try:
        # A relay whose process dies after the produce and before the mark.
        class Killed(RuntimeError):
            pass

        def fault(_row: dict[str, Any]) -> None:
            raise Killed("relay.after_produce_before_mark_sent")

        dying = OutboxRelay(relay_conn, producer, tenants=(DEFAULT_TENANT_ID,),
                            fault=fault)
        q = KafkaJobQueue(db, consumer)
        created = repo.create_job_queued(db, q, _spec(12))
        db.commit()

        with pytest.raises(Killed):
            dying.drain_once()
        relay_conn.rollback()

        # The broker has the record; the row is still unpublished. That asymmetry IS the
        # at-least-once guarantee of MOS-EXEC-041 clause 4.
        topic = work_topic("medos.slice")
        assert len(broker.records(topic)) == 1
        obs.execute("SELECT set_config('medicalos.tenant_id', %s, false)",
                    (DEFAULT_TENANT_ID,))
        pending = obs.execute(
            "SELECT count(*) AS n FROM job_outbox WHERE published_at IS NULL"
        ).fetchone()
        assert pending is not None and pending["n"] == 1

        # Restart: no fault this time. The record is produced a SECOND time.
        restarted = OutboxRelay(relay_conn, producer, tenants=(DEFAULT_TENANT_ID,))
        batch = restarted.drain_once()
        assert batch.published == 1
        assert len(broker.records(topic)) == 2, (
            "the restarted relay did not republish; the guarantee would be at-most-once"
        )
        event_ids = {r["event_id"] for r in broker.records(topic)}
        assert len(event_ids) == 1, "MOS-EXEC-041: event_id is unique per EVENT"

        # And the duplicate is absorbed: one claim, then nothing.
        worker = make_worker_id("dedup")
        assert _claim_within(q, worker) == created.job_id
        db.commit()
        assert q.claim(worker, 60) is None, "a duplicated dispatch produced a second claim"
        db.commit()
        assert repo.get_job(db, created.job_id)["attempt"] == 1  # type: ignore[index]
    finally:
        consumer.close()
        obs.close()
        relay_conn.close()
        producer.close()


def test_an_unpublished_outbox_row_cannot_be_deleted(kafka: Harness) -> None:
    """`MOS-EXEC-041` clause 5, verbatim: "Never delete an unpublished row."

    Enforced by a trigger and not only by a GRANT, so it binds `medicalos_owner` and a
    superuser psql session too -- the two connections a 3 a.m. cleanup actually runs from.
    Deleting an unpublished row is deleting a dispatch no consumer has ever seen; the job
    stays QUEUED forever with nothing to deliver it.
    """
    kafka.relay.stop()  # type: ignore[union-attr]
    created = _create(kafka, _spec(13))
    assert kafka.queue.pending_dispatches() == 1  # type: ignore[attr-defined]

    with pytest.raises(psycopg.Error) as exc:
        kafka.conn.execute("DELETE FROM job_outbox")
    # `MOS05` is schema.sql's own SQLSTATE for an immutability refusal. Asserting the
    # code and not only the message is what stops a later refactor from turning this
    # into some other error that happens to mention MOS-EXEC-041.
    assert exc.value.sqlstate == "MOS05"
    assert "MUST NOT be deleted" in str(exc.value)
    kafka.conn.rollback()

    assert kafka.queue.pending_dispatches() == 1  # type: ignore[attr-defined]
    assert _state(kafka.conn, created.job_id) == "QUEUED"


def test_published_at_is_write_once_and_the_dispatch_is_immutable(
    kafka: Harness,
) -> None:
    """`MOS-EXEC-041` clause 4, as a constraint rather than as relay discipline.

    `published_at` records a broker acknowledgement, which is a fact about the past. A row
    that can be un-published is a row that can be produced again with no consumer-visible
    difference between at-least-once delivery and unbounded delivery.
    """
    _create(kafka, _spec(14))
    kafka.settle()

    with pytest.raises(psycopg.Error) as exc:
        kafka.conn.execute("UPDATE job_outbox SET published_at = NULL")
    assert exc.value.sqlstate == "MOS05" and "write-once" in str(exc.value)
    kafka.conn.rollback()

    with pytest.raises(psycopg.Error) as exc:
        kafka.conn.execute("UPDATE job_outbox SET topic = 'medicalos.events.system'")
    assert exc.value.sqlstate == "MOS05" and "immutable" in str(exc.value)
    kafka.conn.rollback()

    # The three columns the relay owns ARE writable -- a guard that blocked those would
    # have blocked the relay itself.
    kafka.conn.execute("UPDATE job_outbox SET attempts = attempts + 1, last_error = 'x'")
    kafka.conn.commit()


def test_the_sweep_deletes_only_published_rows(kafka: Harness) -> None:
    """`MOS-STORE-274`: "`job_outbox` rows are retained until published plus 7 days."

    chapter 12 section 12.16.1 wants this as a `retention_policies` row with
    `object_class = 'job_outbox'`. That table does not exist in this deployment, so the
    rule lives in the relay and the gap is reported. What is asserted here is the part that
    is dangerous to get wrong: the sweep never reaches an unpublished row, whatever its
    age.
    """
    _create(kafka, _spec(15))
    kafka.settle()
    kafka.relay.stop()  # type: ignore[union-attr]
    _create(kafka, _spec(16))   # unpublished, and the relay is stopped

    # The horizon is moved to now rather than the rows being aged: `published_at` is
    # write-once and `created_at` is immutable, and this component's own guard refuses
    # both -- which is correct, and was found by this check trying the other way
    # first. `RETENTION_DAYS` is asserted separately so the seven-day value stays
    # pinned.
    assert RETENTION_DAYS == 7   # MOS-STORE-274, MOS-EXEC-041 clause 5
    swept = kafka.relay.sweep_published(older_than_days=0)  # type: ignore[union-attr]
    assert swept == 1
    kafka.conn.commit()
    left = kafka.conn.execute(
        "SELECT count(*) AS n, count(published_at) AS pub FROM job_outbox"
    ).fetchone()
    assert left is not None and left["n"] == 1 and left["pub"] == 0


def test_the_advisory_lock_elects_exactly_one_relay(
    pg_dsn: str, kafka: Harness
) -> None:
    """`MOS-EXEC-041` clause 1: "Run as exactly one active instance, elected by a Postgres
    advisory lock (`pg_try_advisory_lock(hashtext('mos.outbox.relay'))`)."

    Session-scoped and try-, not blocking: a standby relay must stay a standby rather than
    become a queue of relays that all wake at handover. The fixture's relay already holds
    it, so a second one must lose.
    """
    other = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    try:
        second = OutboxRelay(other, kafka.broker.producer(),  # type: ignore[union-attr]
                             tenants=(DEFAULT_TENANT_ID,))
        assert second.acquire() is False, "two relays hold mos.outbox.relay"
        assert second.start() is False
    finally:
        other.close()

    # And the lock is really the one MOS-EXEC-041 names.
    assert "hashtext('mos.outbox.relay')" in ADVISORY_LOCK_SQL


def test_the_lag_gauge_is_zero_when_drained_and_rises_when_not(kafka: Harness) -> None:
    """`MOS-EXEC-041` clause 6: `medicalos_outbox_lag_seconds` =
    `now() - min(created_at) WHERE published_at IS NULL`, alerting above 60 s.

    Zero when nothing is pending is the honest value. A gauge reporting the age of the
    last PUBLISHED row would sit at a small, reassuring number while the relay was dead,
    which is the one condition it exists to reveal.
    """
    assert kafka.relay.lag_seconds() == 0.0  # type: ignore[union-attr]

    kafka.relay.stop()  # type: ignore[union-attr]
    _create(kafka, _spec(17))
    time.sleep(0.4)

    # `created_at` is immutable (this component's own guard), so the row cannot be
    # aged to 90 seconds; the gauge is instead shown to MEASURE, and the alert
    # threshold is asserted against a value.
    lag = kafka.relay.lag_seconds()  # type: ignore[union-attr]
    assert 0.3 < lag < 60, f"medicalos_outbox_lag_seconds reads {lag}"
    assert RelayBatch(0, 0, 1, lag).alerting is False
    assert RelayBatch(0, 0, 1, LAG_ALERT_SECONDS + 1).alerting is True
    assert LAG_ALERT_SECONDS == 60    # MOS-EXEC-041 clause 6


def test_a_produce_failure_leaves_the_row_pending_and_records_why(
    pg_dsn: str, db: psycopg.Connection[Any]
) -> None:
    """A broker that refuses the record must not cost the platform a job.

    `MOS-EXEC-041` clause 4 sets `published_at` only on an acknowledgement, and clause 5
    forbids deleting an unpublished row, so the only correct behaviour is: leave it,
    count the attempt, record the error, let the lag gauge rise. Marking the job failed
    would be the tempting alternative and it is wrong -- the job is fine; the transport is
    not (`MOS-EXEC-002`).
    """
    broker = LogBroker()
    producer = broker.producer()
    relay_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    consumer = broker.consumer(ConsumerConfig())
    try:
        q = KafkaJobQueue(db, consumer)
        created = repo.create_job_queued(db, q, _spec(18))
        db.commit()

        broker.fail_next_produces(1)
        relay = OutboxRelay(relay_conn, producer, tenants=(DEFAULT_TENANT_ID,))
        batch = relay.drain_once()
        assert batch.published == 0 and batch.failed == 1

        db.commit()
        row = db.execute(
            "SELECT published_at, attempts, last_error FROM job_outbox"
        ).fetchone()
        assert row is not None
        assert row["published_at"] is None
        assert row["attempts"] == 1
        assert "injected" in row["last_error"]
        assert _state(db, created.job_id) == "QUEUED", "a transport failure failed a job"

        # The next pass publishes it. Nothing was lost.
        assert relay.drain_once().published == 1
        db.commit()
        assert _claim_within(q, make_worker_id("late")) == created.job_id
    finally:
        consumer.close()
        relay_conn.close()
        producer.close()


def test_a_slow_handler_does_not_duplicate_the_job_when_the_consumer_is_evicted(
    pg_dsn: str, db: psycopg.Connection[Any]
) -> None:
    """`MOS-TEST-046`'s Kafka-only Q13 addendum, and `MOS-EXEC-048`'s whole subject.

        "the suite runs a handler sleeping 2 x max.poll.interval.ms and asserts exactly one
         claim, one completion and no duplicate delivery."

    MOS-EXEC-048 describes what would otherwise happen: the broker "evicts it from the
    group and rebalances the partition to another consumer -- which starts the same job
    again on a second GPU. The evicted consumer then finishes, writes DICOM to the PACS,
    and its `commitSync` throws `CommitFailedException` AFTER the side effect."

    The platform survives it because `claim_lease()` is `MOS-EXEC-047` steps 1 and 2 ONLY:
    consume, persist the claim, commit the offset, return. The inference happens after the
    handler has returned, on the runner's own execution slot, so the eviction below lands
    on a consumer that is no longer inside `poll()`. A second consumer that is handed the
    partition finds the job already RUNNING and claims nothing.

    The eviction is triggered directly rather than by sleeping ten real minutes; see
    `medos/medos/bus/memory.py` for why a hazard you can trigger on demand is one that keeps
    being tested.
    """
    broker = LogBroker()
    producer = broker.producer()
    relay_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    slow = broker.consumer(ConsumerConfig(), consumer_id="slow-runner")
    peer_conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    peer = broker.consumer(ConsumerConfig(), consumer_id="peer-runner")
    try:
        q = KafkaJobQueue(db, slow)
        created = repo.create_job_queued(db, q, _spec(19))
        repo.record_series_verdicts(db, created.job_id, (
            repo.SeriesVerdict(series_instance_uid="1.2.3.4.5", decision="selected",
                               selector_name="parity", rank=1),
        ))
        db.commit()
        OutboxRelay(relay_conn, producer, tenants=(DEFAULT_TENANT_ID,)).drain_once()

        worker = make_worker_id("slow")
        assert _claim_within(q, worker) == created.job_id
        db.commit()

        # The handler is now "running inference" for longer than max.poll.interval.ms.
        # The group gives up on it and rebalances the partition.
        broker.evict("slow-runner")

        bind_current_tenant(DEFAULT_TENANT_ID)
        peer_q = KafkaJobQueue(peer_conn, peer)
        assert peer_q.claim(make_worker_id("peer"), 60) is None, (
            "MOS-EXEC-048: a rebalance started the same job on a second runner"
        )
        peer_conn.commit()

        # The slow runner finishes. Its offset commit is refused -- the
        # CommitFailedException of MOS-EXEC-048 -- but the work is already durable and
        # the lease is still its own, so the job completes exactly once.
        q.complete(created.job_id, worker)
        db.commit()
        assert _state(db, created.job_id) == "COMPLETED"

        with pytest.raises(ConsumerEvicted):
            slow.commit()

        assert repo.get_job(db, created.job_id)["attempt"] == 1  # type: ignore[index]
        n = db.execute("SELECT count(*) AS n FROM job_queue").fetchone()
        assert n is not None and n["n"] == 0
    finally:
        peer.close()
        peer_conn.close()
        slow.close()
        relay_conn.close()
        producer.close()


# =====================================================================================
# The topic set: one rule, two statements, compared
# =====================================================================================
def test_the_database_and_python_agree_on_the_closed_topic_set(
    db: psycopg.Connection[Any],
) -> None:
    """`MOS-EXEC-003` and `MOS-EXEC-046`, stated in `job_outbox_topic_ck` and in
    `medos/medos/bus/topics.py`. Two statements of one rule drift; this is what stops it.

    The forbidden names are chapter 5's own examples plus its acceptance criterion 20
    word list. The permitted ones are section 5.8's table. `medicalos.svc.pulmo.effusion
    .work` is in the permitted list on purpose: criterion 20 allows a forbidden word "as a
    substring of a registered `service_id` inside `medicalos.svc.<service_id>.work`" and
    nowhere else, and a rule that forbade it would make a legitimately-named service
    unregistrable.
    """
    forbidden = [
        "medicalos.ct.requested", "medicalos.mr.requested", "medicalos.xr.completed",
        "medicalos.emphysema.detected", "medicalos.lung.nodule.found",
        "medicalos.jobs.running", "medicalos.chest.work",
        "medicalos.jobs.completed.extra", "medicalos.pet.requested", "kafka.topic",
    ]
    permitted = [
        "medicalos.jobs.requested", "medicalos.jobs.completed", "medicalos.jobs.failed",
        "medicalos.events.system", "medicalos.events.audit",
        "medicalos.svc.medos.slice.work", "medicalos.svc.pulmo.effusion.work",
        "medicalos.jobs.failed.dlq",
    ]

    # The CHECK constraint's own expression, applied to each name.
    def db_accepts(topic: str) -> bool:
        row = db.execute(
            "SELECT %s ~ ('^(' || 'medicalos\\.jobs\\.(requested|completed|failed)' "
            "|| '|' || 'medicalos\\.events\\.(system|audit)' || '|' || "
            "'medicalos\\.svc\\.[a-z0-9]([a-z0-9._-]{0,126}[a-z0-9])?\\.work' "
            "|| ')(\\.dlq)?$') AS ok",
            (topic,),
        ).fetchone()
        assert row is not None
        return bool(row["ok"])

    def python_accepts(topic: str) -> bool:
        try:
            validate_topic(topic)
            return True
        except ForbiddenTopic:
            return False

    for topic in forbidden:
        assert not python_accepts(topic), f"medos.bus.topics admitted {topic}"
        assert not db_accepts(topic), f"job_outbox_topic_ck admitted {topic}"
    for topic in permitted:
        assert python_accepts(topic), f"medos.bus.topics refused {topic}"
        assert db_accepts(topic), f"job_outbox_topic_ck refused {topic}"

    # And the CHECK the migration actually installed is the one just exercised.
    row = db.execute(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        " WHERE conname = 'job_outbox_topic_ck'"
    ).fetchone()
    assert row is not None, "job_outbox_topic_ck is missing"
    assert "medicalos" in row["def"] and "running" not in row["def"]


def test_every_outbox_envelope_conforms_to_the_wire_schema(kafka: Harness) -> None:
    """`MOS-EXEC-075`: "Every event on every topic ... MUST conform to the envelope
    schema." `MOS-TEST-041` (producer obligation): "every message a producer emits on a
    covered boundary MUST be captured and validated against the boundary schema. Zero
    captured messages for a boundary exercised by the run fails the check -- silence is
    not a pass."

    So the count is asserted before the conformance.
    """
    for i in range(3):
        _create(kafka, _spec(300 + i))
    kafka.settle()
    kafka.conn.commit()

    rows = kafka.conn.execute("SELECT envelope FROM job_outbox ORDER BY id").fetchall()
    assert len(rows) == 3, "silence is not a pass (MOS-TEST-041)"
    for row in rows:
        assert envelope_errors(row["envelope"]) == [], row["envelope"]

    topic = work_topic("medos.slice")
    produced = kafka.broker.records(topic)  # type: ignore[union-attr]
    assert len(produced) == 3
    for env in produced:
        assert envelope_errors(env) == []
        # MOS-EXEC-044, chapter 5 acceptance criterion 22.
        assert env["partition_key"].startswith(DEFAULT_TENANT_ID + ":")
        # MOS-EXEC-078: the ordering field is present and is a real job_events.seq.
        assert env["job_seq"] >= 1
    # Every field the schema declares is one the validator actually asserts.
    assert set(ENVELOPE_SCHEMA["required"]) <= set(ENVELOPE_SCHEMA["properties"])
