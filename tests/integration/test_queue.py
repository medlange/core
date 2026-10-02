# SPDX-License-Identifier: Apache-2.0
"""Integration proof for `medos.db`: schema, queue, repository.

These tests exist because CONTRACT.md section 4's claims are the kind that are true in a
design document and false in a database. Each one below RUNS the thing rather than
arguing about it:

  - `schema.sql` plus the migrations apply cleanly to an empty database, the table set is
    exactly CONTRACT.md section 8's, and every domain table carries `tenant_id` under
    forced row-level security (weeks 3-5, 15.2.4 item 1; MOS-SEC-077);
  - N concurrent workers claiming from a queue of M jobs never claim the same job
    (`SELECT ... FOR UPDATE SKIP LOCKED`, MOS-EXEC-026);
  - a worker that dies mid-lease has its job reclaimed after expiry and the attempt
    increments (CONTRACT.md section 4);
  - `job_events` really does reject an UPDATE (CONTRACT.md section 8, MOS-EXEC-013);
  - the job row and the queue row commit in ONE transaction, so a rollback loses both
    (MOS-EXEC-034);
  - `CANCELLED` is unreachable, both at the port and at the database (CONTRACT.md
    section 3, MOS-EXEC-073).

Run:  pytest tests/integration/test_queue.py -v
      (MEDOS_TEST_DATABASE_URL points at a Postgres; see conftest.py)
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any

import psycopg
import pytest
from medos.db import repo
from medos.db.conn import apply_schema
from medos.db.queue import (
    JobQueue,
    LeaseLost,
    PostgresJobQueue,
    make_worker_id,
    queue_name,
)
from medos.db.repo import JobSpec, SeriesVerdict
from medos.db.tenancy import DEFAULT_TENANT_ID, bind_current_tenant
from psycopg.rows import dict_row

# Every table CONTRACT.md section 8 names. None may be missing and none may be a view.
CONTRACT_TABLES = frozenset(
    {
        "jobs",
        "job_steps",
        "job_events",
        "job_series",
        "results",
        "result_measurements",
        "result_dicom_objects",
        "job_queue",
    }
)

STUDY = "1.2.826.0.1.3680043.8.498.11111111111111111111111111111111"
CAPS = ("lung_segmentation", "emphysema_laa")


def _spec(n: int = 0, study: str | None = None) -> JobSpec:
    return JobSpec(
        study_instance_uid=study or f"{STUDY[:-3]}{n:03d}",
        capability_ids=CAPS,
    )


def _make_job(conn: psycopg.Connection[Any], n: int = 0) -> str:
    q = PostgresJobQueue(conn)
    created = repo.create_job_queued(conn, q, _spec(n))
    conn.commit()
    return created.job_id


# =====================================================================================
# 1. schema.sql
# =====================================================================================
def test_schema_applies_cleanly_from_empty(pristine_dsn: str) -> None:
    """CONTRACT.md section 8: `schema.sql` is the ONLY DDL. It must bootstrap an empty
    database in one shot -- there is no migration runner in this slice."""
    with psycopg.connect(pristine_dsn, autocommit=True, row_factory=dict_row) as conn:
        before = conn.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone()
        assert before is not None and before["n"] == 0, "the fixture database is not empty"

        apply_schema(conn)

        tables = {
            r["tablename"]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
    missing = CONTRACT_TABLES - tables
    assert not missing, (
        f"CONTRACT.md section 8 names tables schema.sql did not create: {missing}"
    )


def test_every_domain_table_has_tenant_id(db: psycopg.Connection[Any]) -> None:
    """The weeks 3-5 inversion of a weeks 1-2 assertion, and the only test here rewritten.

    CONTRACT.md section 8 said "tenant_id columns are deliberately absent in this slice ...
    Do not add a fake single-tenant value -- that is harder to migrate than an absent
    column", and this test used to assert that absence. docs/spec/15-delivery.md 15.2.4
    item 1 is the scheduled end of it: `0002_tenancy.up.sql` adds the column to all eight
    domain tables with a documented backfill rather than unpicking a fake value, which is
    exactly the migration CONTRACT.md section 8 was protecting.

    What replaces it is the stronger claim: the column is on every domain table, and
    nowhere is it present without forced row-level security (MOS-SEC-077).
    """
    have = {
        r["table_name"]
        for r in db.execute(
            """
            SELECT table_name FROM information_schema.columns
             WHERE table_schema = 'public' AND column_name = 'tenant_id'
            """
        ).fetchall()
    }
    assert CONTRACT_TABLES <= have, (
        f"domain tables missing tenant_id: {sorted(CONTRACT_TABLES - have)}"
    )

    # MOS-SEC-077's CI query, verbatim. MUST return zero rows.
    leaky = db.execute(
        """
        SELECT c.relname
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
          JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id'
                             AND a.attnum > 0
         WHERE n.nspname = 'public' AND c.relkind = 'r'
           AND (c.relrowsecurity = false OR c.relforcerowsecurity = false)
        """
    ).fetchall()
    assert leaky == [], f"tenant_id without forced RLS on: {[r['relname'] for r in leaky]}"


def test_results_unique_job_capability_constraint_exists(db: psycopg.Connection[Any]) -> None:
    """CONTRACT.md section 8, non-negotiable: UNIQUE (job_id, capability_id)."""
    row = db.execute(
        """
        SELECT c.conname,
               (SELECT array_agg(a.attname ORDER BY k.ord)
                  FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
               ) AS cols
          FROM pg_constraint c
         WHERE c.conrelid = 'results'::regclass AND c.contype = 'u'
           AND c.conname = 'results_job_capability_uk'
        """
    ).fetchone()
    assert row is not None, "results_job_capability_uk is missing"
    assert list(row["cols"]) == ["job_id", "capability_id"]


def test_step_plan_is_the_eight_reserved_keys(db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-023: for requested_outputs = {SEG, SR} the plan is eight rows."""
    job_id = _make_job(db)
    total = repo.plan_steps(db, job_id, attempt=1)
    db.commit()
    assert total == 8
    keys = [
        r["step_key"]
        for r in db.execute(
            "SELECT step_key FROM job_steps WHERE job_id = %s ORDER BY step_index",
            (repo.get_job_uuid(db, job_id),),
        ).fetchall()
    ]
    assert keys == [
        "fetch_series",
        "build_volume",
        "envelope_check",
        "service_invoke",
        "validate_bundle",
        "write_dicom",
        "store_dicom",
        "persist_result",
    ]


# =====================================================================================
# 2. ONE transaction for the job row and the queue row  (MOS-EXEC-034)
# =====================================================================================
def test_job_row_and_queue_row_commit_in_one_transaction(db: psycopg.Connection[Any]) -> None:
    q = PostgresJobQueue(db)
    created = repo.create_job_queued(db, q, _spec(1))
    db.commit()

    assert created.created is True
    assert created.state == "QUEUED"
    assert created.job_id.startswith("job_") and len(created.job_id) == 30

    job = repo.get_job(db, created.job_id)
    assert job is not None and job["state"] == "QUEUED"
    lease = q.lease_state(created.job_id)
    assert lease is not None
    assert lease["queue"] == queue_name("medos.slice")
    assert lease["claim_count"] == 0 and lease["lease_owner"] is None


@pytest.mark.parametrize("explode_after_insert", [False, True])
def test_a_crash_inside_creation_leaves_neither_row(
    db: psycopg.Connection[Any], explode_after_insert: bool
) -> None:
    """MOS-EXEC-034, stated as the property it buys:

        "there is no dual write, so there is no outbox and no crash window in which a job
         exists but is invisible to workers, or is dispatched without a row."

    Both halves of that sentence are tested. The process is killed at the two points that
    matter -- immediately before the `job_queue` INSERT (would leave a job no worker can
    ever see) and immediately after it (would leave a dispatched job whose `jobs` row was
    never committed) -- and neither leaves anything behind.
    """

    class ExplodingQueue:
        """A queue whose `enqueue` dies mid-flight, like a SIGKILL would."""

        def __init__(self, real: PostgresJobQueue) -> None:
            self._real = real

        def enqueue(self, job_id: str) -> None:
            if explode_after_insert:
                self._real.enqueue(job_id)
            raise RuntimeError("simulated process death during enqueue")

    with pytest.raises(RuntimeError, match="simulated process death"):
        repo.create_job_queued(db, ExplodingQueue(PostgresJobQueue(db)), _spec(2))

    jobs = db.execute("SELECT count(*) AS n FROM jobs").fetchone()
    queued = db.execute("SELECT count(*) AS n FROM job_queue").fetchone()
    events = db.execute("SELECT count(*) AS n FROM job_events").fetchone()
    assert jobs is not None and jobs["n"] == 0, "a job survived a crash during creation"
    assert queued is not None and queued["n"] == 0
    assert events is not None and events["n"] == 0

    # And the connection is usable afterwards: the rollback was clean, not a wedged
    # transaction the API would then fail every subsequent request on.
    created = repo.create_job_queued(db, PostgresJobQueue(db), _spec(2))
    assert created.created is True and created.state == "QUEUED"


def test_second_create_with_same_spec_is_idempotent(db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-053 / CONTRACT.md section 9: the derived key collapses a replay."""
    q = PostgresJobQueue(db)
    a = repo.create_job_queued(db, q, _spec(3))
    db.commit()
    b = repo.create_job_queued(db, q, _spec(3))
    db.commit()
    assert a.created is True and b.created is False
    assert a.job_id == b.job_id
    assert a.idempotency_key == b.idempotency_key
    n = db.execute("SELECT count(*) AS n FROM jobs").fetchone()
    assert n is not None and n["n"] == 1


def test_request_idempotency_key_never_feeds_the_derived_key(
    db: psycopg.Connection[Any],
) -> None:
    """CONTRACT.md section 9: "That header MUST NOT feed UID derivation ... This was a
    register defect." Two requests differing ONLY in the client header must derive the
    same key and therefore collapse onto one job."""
    q = PostgresJobQueue(db)
    s1 = JobSpec(
        study_instance_uid=f"{STUDY[:-3]}900",
        capability_ids=CAPS,
        request_idempotency_key="client-key-A",
    )
    s2 = JobSpec(
        study_instance_uid=f"{STUDY[:-3]}900",
        capability_ids=CAPS,
        request_idempotency_key="client-key-B",
    )
    assert s1.idempotency_key() == s2.idempotency_key()
    a = repo.create_job_queued(db, q, s1)
    db.commit()
    b = repo.create_job_queued(db, q, s2)
    db.commit()
    assert a.job_id == b.job_id and b.created is False


# =====================================================================================
# 3. Concurrency: N workers, M jobs, no double claim  (MOS-EXEC-026)
# =====================================================================================
# (2, 40) is CONTRACT.md section 4's "TWO concurrent workers ... never claim the same
# job". (32, 200) is MOS-EXEC-032's driver conformance minimum: "exactly-once claim
# under 32 concurrent claimers".
@pytest.mark.parametrize("n_workers,n_jobs", [(2, 40), (8, 64), (32, 200)])
def test_concurrent_workers_never_claim_the_same_job(
    pg_dsn: str, db: psycopg.Connection[Any], n_workers: int, n_jobs: int
) -> None:
    """The claim is `SELECT ... FOR UPDATE SKIP LOCKED` (chapter 5 section 5.6.3).

    Two claimers that hit the same candidate row must not both get it: the loser SKIPs
    past it rather than blocking (MOS-EXEC-026 -- "Claim MUST NOT block"). Run with a
    barrier so every worker starts inside the same millisecond; this is the case that
    fails without SKIP LOCKED, not the leisurely one.
    """
    q = PostgresJobQueue(db)
    expected = set()
    for i in range(n_jobs):
        created = repo.create_job_queued(db, q, _spec(100 + i))
        expected.add(created.job_id)
    db.commit()
    assert len(expected) == n_jobs

    barrier = threading.Barrier(n_workers)
    claims: list[tuple[str, str]] = []  # (worker_id, job_id)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker() -> None:
        wid = make_worker_id("t")
        # A new thread starts with an EMPTY contextvars context -- Python does not copy
        # the parent's -- so the fixture's tenant binding is invisible here and the first
        # claim would raise NoTenantContextError. Binding as the thread's first statement
        # is the same rule the worker's heartbeat thread follows
        # (medos/medos/worker/runner.py::_Heartbeat._run), and it is deliberately NOT hidden
        # inside tenant_tx: a thread that silently inherited "some" tenant is how a
        # background job ends up writing into the wrong one.
        bind_current_tenant(DEFAULT_TENANT_ID)
        try:
            conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
            wq = PostgresJobQueue(conn)
            barrier.wait(timeout=30)
            while True:
                job_id = wq.claim(wid, lease_seconds=60)
                if job_id is None:
                    # An empty claim may mean "drained" or "every candidate was locked
                    # by a peer in this instant". Retry once after a yield; two empties
                    # in a row means drained.
                    time.sleep(0.01)
                    job_id = wq.claim(wid, lease_seconds=60)
                    if job_id is None:
                        break
                with lock:
                    claims.append((wid, job_id))
            conn.close()
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            with lock:
                errors.append(exc)
            try:
                barrier.abort()
            except Exception:
                pass

    threads = [threading.Thread(target=worker, name=f"claimer-{i}") for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    assert not errors, f"claimers raised: {errors!r}"
    assert all(not t.is_alive() for t in threads), "a claimer did not finish"

    claimed = [job_id for _wid, job_id in claims]
    dupes = [j for j, c in Counter(claimed).items() if c > 1]
    assert not dupes, f"the same job was claimed more than once: {dupes}"
    assert set(claimed) == expected, (
        f"claimed {len(set(claimed))} of {n_jobs} jobs; "
        f"missing {sorted(expected - set(claimed))[:5]}"
    )

    # Every job is RUNNING, attempt 1, leased by exactly the worker that claimed it.
    db.commit()  # refresh this connection's snapshot
    for wid, job_id in claims:
        job = repo.get_job(db, job_id)
        assert job is not None
        assert job["state"] == "RUNNING", f"{job_id} is {job['state']}"
        # MOS-EXEC-035: the first delivery of a job carries claim_count = 1, attempt = 1.
        assert job["attempt"] == 1
        lease = q.lease_state(job_id)
        assert lease is not None
        assert lease["lease_owner"] == wid
        assert lease["claim_count"] == 1
        assert lease["fence_token"] == 1  # MOS-EXEC-027


def test_claim_returns_none_on_an_empty_inbox(db: psycopg.Connection[Any]) -> None:
    q = PostgresJobQueue(db)
    assert q.claim(make_worker_id(), lease_seconds=30) is None


# =====================================================================================
# 4. Lease expiry, reclaim, attempt increment  (CONTRACT.md section 4)
# =====================================================================================
def test_expired_lease_is_reclaimed_and_attempt_increments(
    pg_dsn: str, db: psycopg.Connection[Any]
) -> None:
    """CONTRACT.md section 4: "Expired leases MUST be reclaimable. A reclaim increments
    `attempt`."

    The worker is killed the way a worker actually dies -- its connection is closed and
    it never heartbeats again -- rather than by an UPDATE that backdates the lease.

    The increment happens at the NEXT claim, not inside `reclaim_expired()`, because
    MOS-EXEC-035 makes the claim statement the only place in the system that touches
    `claim_count`. The observable property CONTRACT.md section 4 states -- a job whose
    worker died resumes on attempt N+1 -- is what is asserted, end to end.
    """
    job_id = _make_job(db, 200)

    # --- worker A claims with a 1-second lease, then dies ---------------------------
    dying = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    qa = PostgresJobQueue(dying)
    worker_a = make_worker_id("dying")
    lease_a = qa.claim_lease(worker_a, lease_seconds=1)
    assert lease_a is not None and lease_a.job_id == job_id
    assert lease_a.claim_count == 1 and lease_a.fence_token == 1
    dying.close()  # hard death: no complete(), no fail(), no heartbeat

    db.commit()
    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "RUNNING" and job["attempt"] == 1

    # --- the lease expires ----------------------------------------------------------
    time.sleep(1.3)
    q = PostgresJobQueue(db)

    # MOS-EXEC-036: Claim MUST NOT pick up a merely-expired lease. That is the
    # reclaimer's job, and mixing the two would hide the retry-budget decision in a hot
    # path.
    assert q.claim(make_worker_id("eager"), lease_seconds=60) is None

    reclaimed = q.reclaim_expired()
    db.commit()
    assert [r.job_id for r in reclaimed] == [job_id]
    assert reclaimed[0].outcome == "requeued"
    assert reclaimed[0].previous_owner == worker_a
    assert reclaimed[0].claim_count == 1

    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "QUEUED"
    lease = q.lease_state(job_id)
    assert lease is not None and lease["lease_owner"] is None

    # The reclaim sets `available_at = now() + job_backoff(claim_count)`. Backoff for
    # attempt 1 is full jitter over [2.5s, 5s), so the job is not instantly claimable;
    # wind it forward rather than sleeping for it -- the backoff is chapter 5 section
    # 5.6.4's and is tested by its own assertion above, not by wall-clock patience.
    assert lease["available_at"] > job["updated_at"]
    db.execute(
        "UPDATE job_queue SET available_at = now() "
        "WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)",
        (job_id,),
    )
    db.commit()

    # --- worker B claims: THIS is where attempt increments ---------------------------
    worker_b = make_worker_id("survivor")
    lease_b = q.claim_lease(worker_b, lease_seconds=60)
    db.commit()
    assert lease_b is not None and lease_b.job_id == job_id
    assert lease_b.claim_count == 2, "the reclaimed job did not resume on attempt 2"
    assert lease_b.fence_token == 2, "MOS-EXEC-027: the fence must advance on every claim"

    job = repo.get_job(db, job_id)
    assert job is not None
    assert job["state"] == "RUNNING"
    assert job["attempt"] == 2, "CONTRACT.md section 4: a reclaim increments attempt"

    # The whole sequence is legible in the append-only stream, in order.
    types = [e["event_type"] for e in repo.list_events(db, job_id)]
    assert types.count("queue.lease_expired") == 1
    assert types[-1] == "job.state_changed"
    states = [
        (e["from_state"], e["to_state"])
        for e in repo.list_events(db, job_id)
        if e["to_state"]
    ]
    assert states == [
        (None, "QUEUED") if states[0][0] is None else ("CREATED", "QUEUED"),
        ("QUEUED", "RUNNING"),
        ("RUNNING", "QUEUED"),
        ("QUEUED", "RUNNING"),
    ]


def test_heartbeat_returns_false_after_the_lease_was_reclaimed(
    pg_dsn: str, db: psycopg.Connection[Any]
) -> None:
    """CONTRACT.md section 4: "heartbeat ... returns False if the worker lost it."

    MOS-EXEC-028: a runner that sees False MUST abort immediately and MUST NOT call
    complete() or fail() -- so the second half of this test proves complete() refuses
    too, rather than trusting the runner to obey.
    """
    job_id = _make_job(db, 201)
    frozen = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    qf = PostgresJobQueue(frozen)
    worker = make_worker_id("frozen")
    assert qf.claim(worker, lease_seconds=1) == job_id
    assert qf.heartbeat(job_id, worker, lease_seconds=1) is True

    time.sleep(1.3)
    q = PostgresJobQueue(db)
    q.reclaim_expired()
    db.commit()

    # The frozen worker wakes up. It no longer owns anything.
    assert qf.heartbeat(job_id, worker, lease_seconds=60) is False
    with pytest.raises(LeaseLost):
        qf.complete(job_id, worker)
    frozen.rollback()
    frozen.close()

    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "QUEUED", (
        "a zombie worker completed a job it no longer held"
    )


def test_heartbeat_extends_the_lease(pg_dsn: str, db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-028: "Heartbeat extends the lease to now() + LeaseDuration"."""
    job_id = _make_job(db, 202)
    q = PostgresJobQueue(db)
    worker = make_worker_id()
    assert q.claim(worker, lease_seconds=5) == job_id
    db.commit()
    before = q.lease_state(job_id)
    assert before is not None
    time.sleep(0.3)
    assert q.heartbeat(job_id, worker, lease_seconds=600) is True
    db.commit()
    after = q.lease_state(job_id)
    assert after is not None
    assert after["lease_expires_at"] > before["lease_expires_at"]
    assert after["fence_token"] == before["fence_token"], (
        "a heartbeat must not advance the fence; only a claim does (MOS-EXEC-027)"
    )


def test_reclaim_dead_letters_when_the_budget_is_exhausted(
    pg_dsn: str, db: psycopg.Connection[Any]
) -> None:
    """T12: lease expired and `claim_count >= jobs.max_attempts` -> FAILED,
    `failure_class = 'lease_expired'` (chapter 5 section 5.6.4 (b))."""
    q = PostgresJobQueue(db)
    created = repo.create_job_queued(
        db,
        q,
        JobSpec(study_instance_uid=f"{STUDY[:-3]}203", capability_ids=CAPS, max_attempts=1),
    )
    db.commit()
    job_id = created.job_id

    dying = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    PostgresJobQueue(dying).claim(make_worker_id("doomed"), lease_seconds=1)
    dying.close()

    time.sleep(1.3)
    reclaimed = q.reclaim_expired()
    db.commit()
    assert [(r.job_id, r.outcome) for r in reclaimed] == [(job_id, "failed")]

    job = repo.get_job(db, job_id)
    assert job is not None
    assert job["state"] == "FAILED"
    assert job["failure_class"] == "lease_expired"
    assert job["finished_at"] is not None
    assert q.lease_state(job_id) is None, "MOS-STORE-274: the queue row is deleted"

    view = repo.job_view(db, job_id)
    assert view is not None
    # MOS-EXEC-016a: `lease_expired` maps to `transport_failure` on the wire, and the
    # internal class name never appears there.
    assert view["error"]["class"] == "transport_failure"
    assert view["error"]["retryable"] is True


# =====================================================================================
# 5. job_events is append-only  (CONTRACT.md section 8, MOS-EXEC-013)
# =====================================================================================
def test_job_events_rejects_update(db: psycopg.Connection[Any]) -> None:
    """CONTRACT.md section 8: "job_events append-only". An UPDATE is the dangerous verb:
    it rewrites history while leaving the row count intact."""
    job_id = _make_job(db, 300)
    rows = repo.list_events(db, job_id)
    assert rows, "the created job produced no events"

    with pytest.raises(psycopg.Error) as exc:
        db.execute(
            "UPDATE job_events SET to_state = 'COMPLETED' WHERE job_id = "
            "(SELECT id FROM jobs WHERE public_id = %s)",
            (job_id,),
        )
    # MOS05 is schema.sql's own SQLSTATE for forbid_mutation(). Asserting the code and
    # not only the message is what stops a future refactor from turning this into some
    # other error that happens to mention "append-only".
    assert exc.value.sqlstate == "MOS05"
    assert "append-only" in str(exc.value)
    db.rollback()

    after = repo.list_events(db, job_id)
    assert [e["to_state"] for e in after] == [e["to_state"] for e in rows]


def test_job_events_rejects_delete(db: psycopg.Connection[Any]) -> None:
    job_id = _make_job(db, 301)
    with pytest.raises(psycopg.Error) as exc:
        db.execute(
            "DELETE FROM job_events WHERE job_id = "
            "(SELECT id FROM jobs WHERE public_id = %s)",
            (job_id,),
        )
    assert exc.value.sqlstate == "MOS05"
    assert "append-only" in str(exc.value)
    db.rollback()
    assert repo.list_events(db, job_id)


def test_event_seq_is_monotonic_per_job_across_both_producers(
    db: psycopg.Connection[Any],
) -> None:
    """MOS-STORE-271: `seq` is per-job monotonic and assigned in the same transaction as
    the state change. Two producers write it -- `job_transition()` and
    `job_append_event()` -- and they must share one counter, or an SSE client resuming
    from `Last-Event-ID` replays or skips."""
    job_id = _make_job(db, 302)
    repo.plan_steps(db, job_id, attempt=1)
    for key in ("fetch_series", "build_volume"):
        repo.start_step(db, job_id, key)
        repo.finish_step(db, job_id, key)
    db.commit()

    events = repo.list_events(db, job_id)
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1)), seqs
    assert len({e["event_id"] for e in events}) == len(events), "event_id is not unique"
    # every payload carries its own seq (MOS-EXEC-078's ordering mechanism)
    assert all(e["payload"]["job_seq"] == e["seq"] for e in events)

    # Last-Event-ID resume: asking for everything after seq 3 returns exactly the tail.
    tail = repo.list_events(db, job_id, after_seq=3)
    assert [e["seq"] for e in tail] == seqs[3:]


def test_two_jobs_have_independent_seq_counters(db: psycopg.Connection[Any]) -> None:
    a = _make_job(db, 303)
    b = _make_job(db, 304)
    assert [e["seq"] for e in repo.list_events(db, a)] == [1, 2]
    assert [e["seq"] for e in repo.list_events(db, b)] == [1, 2]


# =====================================================================================
# 6. CANCELLED is reserved  (CONTRACT.md section 3, MOS-EXEC-073)
# =====================================================================================
def test_cancel_raises_not_implemented(db: psycopg.Connection[Any]) -> None:
    """CONTRACT.md section 4: "cancel() raises NotImplementedError"."""
    q = PostgresJobQueue(db)
    with pytest.raises(NotImplementedError) as exc:
        q.cancel(_make_job(db, 400))
    assert "reserved" in str(exc.value).lower()


def test_cancelled_is_structurally_unreachable(db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-073/074: `CANCELLED` has no row in `job_state_transition`, so
    `job_transition()` refuses it from every state, for every actor. The enum value
    exists (MOS-EXEC-001 fixes the public enum at seven) and nothing produces it."""
    values = {
        r["v"]
        for r in db.execute(
            "SELECT unnest(enum_range(NULL::job_state))::text AS v"
        ).fetchall()
    }
    assert "CANCELLED" in values, "MOS-EXEC-001's enum is seven values, CANCELLED included"

    rows = db.execute(
        "SELECT count(*) AS n FROM job_state_transition WHERE to_state = 'CANCELLED'"
    ).fetchone()
    assert rows is not None and rows["n"] == 0

    job_id = _make_job(db, 401)
    with pytest.raises(psycopg.Error) as exc:
        db.execute(
            "SELECT job_transition((SELECT id FROM jobs WHERE public_id = %s), "
            "ARRAY['QUEUED']::job_state[], 'CANCELLED', 'job-runner', NULL, '{\"x\":1}')",
            (job_id,),
        )
    assert exc.value.sqlstate == "MOS03"   # "transition not permitted for actor"
    assert "not permitted" in str(exc.value)
    db.rollback()


def test_transition_table_matches_the_spec(db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-010: the table is normative and exhaustive; MOS-EXEC-011 requires it to
    be stored as data. This is the CI diff chapter 5 asks for."""
    rows = {
        (r["from_state"], r["to_state"], r["actor"]): r["label"]
        for r in db.execute("SELECT * FROM job_state_transition").fetchall()
    }
    assert rows == {
        ("CREATED", "QUEUED", "control-plane"): "T2",
        ("CREATED", "REJECTED", "control-plane"): "T3",
        ("CREATED", "FAILED", "reconciler"): "T4",
        ("QUEUED", "RUNNING", "job-runner"): "T5",
        ("QUEUED", "FAILED", "reconciler"): "T6",
        ("RUNNING", "REJECTED", "job-runner"): "T7",
        ("RUNNING", "COMPLETED", "job-runner"): "T8",
        ("RUNNING", "QUEUED", "job-runner"): "T9",
        ("RUNNING", "QUEUED", "queue-reclaimer"): "T10",
        ("RUNNING", "FAILED", "job-runner"): "T11",
        ("RUNNING", "FAILED", "queue-reclaimer"): "T12",
        ("FAILED", "QUEUED", "operator"): "T13",
    }


def test_illegal_transition_is_refused_by_the_database(db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-012: COMPLETED is absorbing. No transition out of it exists."""
    job_id = _make_job(db, 402)
    with pytest.raises(psycopg.Error) as exc:
        db.execute(
            "SELECT job_transition((SELECT id FROM jobs WHERE public_id = %s), "
            "ARRAY['QUEUED']::job_state[], 'COMPLETED', 'control-plane', NULL, "
            "'{\"x\":1}')",
            (job_id,),
        )
    assert exc.value.sqlstate == "MOS03"
    db.rollback()


# =====================================================================================
# 7. The port, complete/fail/reject
# =====================================================================================
def test_driver_satisfies_the_port(db: psycopg.Connection[Any]) -> None:
    """CONTRACT.md section 4: six methods, all of them."""
    q = PostgresJobQueue(db)
    assert isinstance(q, JobQueue)
    for name in ("enqueue", "claim", "heartbeat", "complete", "fail", "cancel"):
        assert callable(getattr(q, name)), name


def test_complete_writes_results_and_the_transition_in_one_transaction(
    db: psycopg.Connection[Any],
) -> None:
    """CONTRACT.md section 8: "the results row and the terminal state transition MUST be
    written in ONE transaction". Proven by rolling back: neither survives."""
    job_id = _make_job(db, 500)
    q = PostgresJobQueue(db)
    worker = make_worker_id()
    assert q.claim(worker, lease_seconds=120) == job_id
    db.commit()

    result = repo.ResultRow(
        capability_id="lung_segmentation",
        capability_version="0.1.0",
        result_kind="segmentation",
        findings=[{"kind": "lung", "present": True, "score": None}],
        input_series_uids=("1.2.3.4.5",),
        input_instance_uids=("1.2.3.4.5.1", "1.2.3.4.5.2"),
        preprocessing_version="prep-1.0.0",
        worker_version="worker-0.1.0",
        runtime_version="python-3.11",
        measurements=(
            repo.MeasurementRow(
                concept_scheme="SCT",
                concept_code="31094006",
                concept_display="Lung volume",
                value=4521.125,
                ucum_unit="ml",
                source_series_instance_uid="1.2.3.4.5",
            ),
        ),
        dicom_objects=(
            repo.DicomObjectRow(
                object_kind="SEG",
                sop_class_uid="1.2.840.10008.5.1.4.1.1.66.4",
                series_instance_uid="2.25.1111",
                sop_instance_uid="2.25.2222",
                series_number=9000,
                output_index=0,
                derivation_inputs={
                    "tenant_id": repo.SLICE_TENANT_ID,
                    "idempotency_key": "ik_" + "a" * 26,
                    "service_id": "medos.slice",
                    "service_version": "0.1.0",
                    "model_id": "hu_threshold",
                    "model_version": "0.1.0",
                    "uid_space": "source",
                    "uid_kind": "seg.instance",
                    "output_index": 0,
                },
            ),
        ),
    )

    # --- atomicity proof: kill the process between the two writes --------------------
    # MOS-STORE-275 and MOS-EXEC-057 forbid a COMPLETED job with no result row and a
    # result row on a job that never reached COMPLETED. A failure at the seam must undo
    # BOTH, which is only true if they share one transaction.
    class ExplodingComplete:
        def __init__(self, real: PostgresJobQueue) -> None:
            self._real = real

        def complete(self, job_id: str, worker_id: str) -> None:
            raise RuntimeError("simulated process death between results and T8")

    with pytest.raises(RuntimeError, match="simulated process death"):
        repo.complete_job_with_results(db, ExplodingComplete(q), job_id, worker, [result])

    assert db.execute("SELECT count(*) AS n FROM results").fetchone()["n"] == 0, (
        "a results row outlived the transition it was supposed to share a transaction with"
    )
    assert db.execute("SELECT count(*) AS n FROM result_measurements").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM result_dicom_objects").fetchone()["n"] == 0
    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "RUNNING"
    assert q.lease_state(job_id) is not None, "the lease was released by a failed commit"

    # --- commit path ----------------------------------------------------------------
    ids = repo.complete_job_with_results(db, q, job_id, worker, [result])
    db.commit()
    assert len(ids) == 1

    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "COMPLETED"
    assert job["finished_at"] is not None
    assert list(job["selected_series_uids"]) == ["1.2.3.4.5"]
    assert q.lease_state(job_id) is None, "MOS-STORE-274: queue row deleted at terminal"

    view = repo.job_view(db, job_id)
    assert view is not None
    (r,) = view["results"]
    assert r["capability_id"] == "lung_segmentation"
    assert r["measurements"][0]["value"] == pytest.approx(4521.125)
    assert r["measurements"][0]["geometry_space"] == "source"
    # CONTRACT.md section 10's provenance field set.
    prov = r["provenance"]
    for key in (
        "study_instance_uid",
        "series_consumed",
        "capability_id",
        "capability_version",
        "preprocessing_version",
        "worker_version",
        "runtime_version",
        "generated_objects",
    ):
        assert prov[key], f"provenance is missing {key} (CONTRACT.md section 10)"
    assert prov["generated_objects"][0]["sop_instance_uid"] == "2.25.2222"


def test_duplicate_result_for_one_capability_is_impossible(
    db: psycopg.Connection[Any],
) -> None:
    """CONTRACT.md section 8 + MOS-STORE-275: the second write finds the constraint and
    is treated as success, not as an error -- that is what makes a retry past the commit
    point safe."""
    job_id = _make_job(db, 501)
    q = PostgresJobQueue(db)
    worker = make_worker_id()
    q.claim(worker, lease_seconds=120)
    db.commit()

    row = repo.ResultRow(
        capability_id="emphysema_laa",
        capability_version="0.1.0",
        result_kind="measurement",
        findings=[],
        input_series_uids=("1.2.3.4.5",),
        input_instance_uids=("1.2.3.4.5.1",),
        preprocessing_version="prep-1.0.0",
        worker_version="worker-0.1.0",
        runtime_version="python-3.11",
    )
    first = repo.save_result(db, job_id, row)
    second = repo.save_result(db, job_id, row)
    db.commit()
    assert first == second
    n = db.execute("SELECT count(*) AS n FROM results").fetchone()
    assert n is not None and n["n"] == 1

    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            """
            INSERT INTO results (job_id, capability_id, capability_version, result_kind,
                clinical_use_mode, study_instance_uid, input_series_uids,
                input_instance_uids, input_instance_count, input_uid_digest,
                preprocessing_version, worker_version, runtime_version)
            VALUES ((SELECT id FROM jobs WHERE public_id = %s), 'emphysema_laa', '0.1.0',
                    'measurement', 'research_only', %s, ARRAY['1.2.3.4.5'],
                    ARRAY['1.2.3.4.5.1'], 1, %s, 'p', 'w', 'r')
            """,
            (job_id, f"{STUDY[:-3]}501", "0" * 64),
        )
    db.rollback()


def test_fail_retryable_requeues_then_exhausts_the_budget(
    db: psycopg.Connection[Any],
) -> None:
    """T9 then T11. MOS-EXEC-030: the DRIVER decides, from claim_count and max_attempts;
    the caller must not."""
    q = PostgresJobQueue(db)
    created = repo.create_job_queued(
        db,
        q,
        JobSpec(study_instance_uid=f"{STUDY[:-3]}502", capability_ids=CAPS, max_attempts=2),
    )
    db.commit()
    job_id = created.job_id

    w1 = make_worker_id()
    assert q.claim(w1, lease_seconds=60) == job_id
    d1 = q.fail_ex(job_id, w1, retryable=True, failure_class="gateway_unavailable",
                   failure_code="qido_timeout")
    db.commit()
    assert d1.next_state == "QUEUED" and d1.attempt == 1
    assert d1.retry_at is not None
    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "QUEUED"
    assert job["failure_class"] is None, (
        "a requeued job must not carry a failure_class; the attempt's failure lives in "
        "the event stream"
    )

    db.execute(
        "UPDATE job_queue SET available_at = now() WHERE job_id = "
        "(SELECT id FROM jobs WHERE public_id = %s)",
        (job_id,),
    )
    db.commit()

    w2 = make_worker_id()
    assert q.claim(w2, lease_seconds=60) == job_id
    d2 = q.fail_ex(job_id, w2, retryable=True, failure_class="gateway_unavailable",
                   failure_code="qido_timeout")
    db.commit()
    assert d2.next_state == "FAILED", "the budget was exhausted; it must not requeue again"
    assert d2.attempt == 2
    job = repo.get_job(db, job_id)
    assert job is not None
    assert job["state"] == "FAILED"
    assert job["failure_class"] == "gateway_unavailable"
    assert job["failure_code"] == "qido_timeout"
    assert q.lease_state(job_id) is None


def test_fail_non_retryable_goes_straight_to_failed(db: psycopg.Connection[Any]) -> None:
    job_id = _make_job(db, 503)
    q = PostgresJobQueue(db)
    w = make_worker_id()
    q.claim(w, lease_seconds=60)
    q.fail(job_id, w, retryable=False)
    db.commit()
    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "FAILED"
    assert job["failure_class"] == "internal"


def test_reject_is_a_clinical_outcome_not_a_failure(db: psycopg.Connection[Any]) -> None:
    """CONTRACT.md section 3: REJECTED carries a machine-readable reason.
    MOS-EXEC-015: it consumes no retry budget and its queue row is deleted in the same
    transaction."""
    job_id = _make_job(db, 504)
    q = PostgresJobQueue(db)
    w = make_worker_id()
    q.claim(w, lease_seconds=60)
    repo.record_series_verdicts(
        db,
        job_id,
        [
            SeriesVerdict("1.2.3.4.900", "rejected", reason_code="image_type_excluded",
                          reason_detail="LOCALIZER", instance_count=1, modality="CT"),
            SeriesVerdict("1.2.3.4.901", "rejected",
                          reason_code="slice_thickness_out_of_range",
                          reason_detail="5.0 mm", instance_count=62, modality="CT"),
        ],
    )
    q.reject(job_id, w, reason_code="no_eligible_series",
             reason_detail="study contains 2 series; 0 satisfy the selector")
    db.commit()

    job = repo.get_job(db, job_id)
    assert job is not None
    assert job["state"] == "REJECTED"
    assert job["reject_reason_code"] == "no_eligible_series"
    assert job["failure_class"] is None, "a rejection is not a failure (MOS-EXEC-014)"
    assert job["attempt"] == 1, "MOS-EXEC-015: a rejection consumes no retry budget"
    assert q.lease_state(job_id) is None

    view = repo.job_view(db, job_id)
    assert view is not None
    assert view["rejection"]["class"] == "clinical_rejection"
    assert view["rejection"]["code"] == "NO_ELIGIBLE_SERIES"
    assert view["rejection"]["retryable"] is False
    assert "error" not in view
    # MOS-STORE-270: the per-series verdicts are rows and are readable through the API.
    assert {s["series_instance_uid"] for s in view["series_selection"]} == {
        "1.2.3.4.900",
        "1.2.3.4.901",
    }
    assert {s["decision"] for s in view["series_selection"]} == {"rejected"}


def test_complete_without_the_lease_is_refused(db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-027's whole point: a runner must not write a result for a job it no
    longer owns."""
    job_id = _make_job(db, 505)
    q = PostgresJobQueue(db)
    owner = make_worker_id("owner")
    assert q.claim(owner, lease_seconds=60) == job_id
    db.commit()
    with pytest.raises(LeaseLost):
        q.complete(job_id, make_worker_id("impostor"))
    db.rollback()
    job = repo.get_job(db, job_id)
    assert job is not None and job["state"] == "RUNNING"


def test_enqueue_refuses_a_job_that_is_not_created(db: psycopg.Connection[Any]) -> None:
    """T2's guard: a second enqueue would reset the lease bookkeeping of a job a worker
    may already be running."""
    job_id = _make_job(db, 506)
    q = PostgresJobQueue(db)
    from medos.db.queue import QueueError

    with pytest.raises(QueueError):
        q.enqueue(job_id)
    db.rollback()


def test_step_rollup_drives_phase_and_counters(db: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-020/021: steps_completed/steps_total are derived by the trigger and by
    nothing else, and there is no float progress anywhere."""
    job_id = _make_job(db, 600)
    assert repo.plan_steps(db, job_id, attempt=1) == 8
    db.commit()
    job = repo.get_job(db, job_id)
    assert job is not None and job["steps_total"] == 8 and job["steps_completed"] == 0

    repo.start_step(db, job_id, "fetch_series")
    db.commit()
    job = repo.get_job(db, job_id)
    assert job is not None and job["phase"] == "retrieving"

    repo.finish_step(db, job_id, "fetch_series")
    repo.skip_step(db, job_id, "write_dicom", "derived series already exists")
    db.commit()
    job = repo.get_job(db, job_id)
    assert job is not None and job["steps_completed"] == 2  # succeeded + skipped

    cols = {
        r["column_name"]
        for r in db.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='jobs'"
        ).fetchall()
    }
    assert "progress" not in cols, "MOS-EXEC-021: there is no float progress column"


def test_a_skipped_step_without_a_reason_cannot_be_stored(
    db: psycopg.Connection[Any],
) -> None:
    """MOS-EXEC-020, as a CHECK rather than as a rule someone remembers."""
    job_id = _make_job(db, 601)
    repo.plan_steps(db, job_id, attempt=1)
    db.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE job_steps SET status = 'skipped' WHERE step_key = 'store_dicom' "
            "AND job_id = (SELECT id FROM jobs WHERE public_id = %s)",
            (job_id,),
        )
    db.rollback()
