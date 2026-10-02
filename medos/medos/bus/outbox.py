# SPDX-License-Identifier: Apache-2.0
"""The outbox relay. `MOS-EXEC-041`, clause by clause.

    1. Run as exactly one active instance, elected by a Postgres advisory lock
       (`pg_try_advisory_lock(hashtext('mos.outbox.relay'))`).
    2. Read pending rows in `id` order, in batches of <= 500, with `FOR UPDATE SKIP LOCKED`.
    3. Produce with `enable.idempotence=true`, `acks=all`,
       `max.in.flight.requests.per.connection=5`, `compression.type=zstd`.
    4. Set `published_at` only after the broker acknowledges, so the guarantee is
       at-least-once.
    5. Never delete an unpublished row. Published rows are deleted after 7 days.
    6. Emit `medicalos_outbox_lag_seconds` = `now() - min(created_at) WHERE published_at
       IS NULL`, with an alert above 60 s.

Clause 3 lives in `medos.bus.port.ProducerConfig`; clause 5's first half is a trigger in
`0014_outbox.up.sql`, because a Python relay cannot bind a psql session. Everything else
is here.

THE WINDOW THIS CLASS EXISTS TO SURVIVE
----------------------------------------
Chapter 14's fault point `relay.after_produce_before_mark_sent` names it exactly: the
broker has the record and `published_at` has not been written. A relay killed there
republishes on restart, so the consumer sees the dispatch twice. That is at-least-once and
it is the guarantee clause 4 chooses on purpose -- the alternative, writing `published_at`
first, is at-most-once and loses a dispatch to any crash in the same window.

The duplicate is absorbed at the consumer, on `envelope.event_id` (`MOS-EXEC-041`, closing
sentence). In this platform the consumer is `medos.db.queue_kafka.KafkaJobQueue.claim`,
and the dedup is not a cache: it is the `job_queue` primary key. A second delivery of the
same dispatch inserts nothing and claims nothing, because the job is no longer `QUEUED`.
`fault` below is the injection point, so the suite tests that rather than describing it.

TENANCY -- AND A CONTRADICTION REPORTED RATHER THAN PATCHED
------------------------------------------------------------
`MOS-EXEC-085` says the outbox relay "run[s] as a separate database role that bypasses
RLS". Chapter 12 section 12.5 says the opposite in as many words: "Retention sweeps, the
projection reconciler, the OUTBOX RELAY, blob GC and any migration backfill that touches
tenant-owned rows MUST iterate tenants, setting `medicalos.tenant_id` per tenant ... a
process-wide connection that reads across tenants MUST NOT exist outside the break-glass
path (`MOS-SEC-078`). The loop is the price of the guarantee." `MOS-STORE-222` closes the
role set at five, and chapter 12's acceptance criterion 7 asserts that `BYPASSRLS` appears
in the migrations only in `medicalos_backup`.

The tenant-iterating form satisfies three of those four; the BYPASSRLS role satisfies one
and breaks three, including a criterion that is already asserted by
`tests/integration/test_tenancy.py`. So this relay loops over `serving_tenants()` -- whose
docstring already cites this exact requirement -- and the conflict is in this component's
report. No sixth role is created.

Spec: MOS-EXEC-002, MOS-EXEC-039, MOS-EXEC-040, MOS-EXEC-041, MOS-EXEC-085,
      MOS-STORE-225, MOS-STORE-274, MOS-TEST-051.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from medos.bus.port import ProduceFailed, Producer
from medos.db.tenancy import serving_tenants, tenant_context, tenant_tx

__all__ = [
    "OutboxRelay",
    "RelayBatch",
    "ADVISORY_LOCK_SQL",
    "LAG_ALERT_SECONDS",
    "MAX_BATCH",
    "RETENTION_DAYS",
    "wait_for_drain",
]

log = logging.getLogger("medos.bus.outbox")

# MOS-EXEC-041 clause 1, verbatim. `hashtext` is deterministic across sessions and
# databases in one cluster, which is what makes it a leader election rather than a
# per-process coincidence.
ADVISORY_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtext('mos.outbox.relay')) AS got"
_ADVISORY_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtext('mos.outbox.relay'))"

MAX_BATCH = 500          # MOS-EXEC-041 clause 2
LAG_ALERT_SECONDS = 60   # MOS-EXEC-041 clause 6
RETENTION_DAYS = 7       # MOS-EXEC-041 clause 5, MOS-STORE-274


@dataclass(frozen=True)
class RelayBatch:
    """What one drain did. Returned so a caller can log or assert without re-querying."""

    published: int
    failed: int
    tenants: int
    lag_seconds: float

    @property
    def alerting(self) -> bool:
        """MOS-EXEC-041 clause 6: alert above 60 s."""
        return self.lag_seconds > LAG_ALERT_SECONDS


class OutboxRelay:
    """One relay. Owns its own connection; never shares the enqueuing transaction.

    Sharing would be the bug the outbox exists to prevent: the relay's UPDATE of
    `published_at` must commit independently of the job-creation transaction, or a
    rollback there would un-publish a record the broker already has.
    """

    def __init__(
        self,
        conn: psycopg.Connection[Any],
        producer: Producer,
        *,
        batch_size: int = MAX_BATCH,
        tenants: tuple[str, ...] | None = None,
        fault: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not 1 <= batch_size <= MAX_BATCH:
            raise ValueError(
                f"MOS-EXEC-041 clause 2: batches of <= {MAX_BATCH}, got {batch_size}"
            )
        if not conn.autocommit:
            # Found by this component's own conformance suite, and worth the paragraph
            # because the failure is silent.
            #
            # `medos.db.tenancy.tenant_tx()` wraps `conn.transaction()`, which is a real
            # BEGIN/COMMIT only when no transaction is already open and a SAVEPOINT when
            # one is. On a connection with `autocommit=False`, the very first statement --
            # here, `pg_try_advisory_lock` in `acquire()` -- opens an implicit transaction
            # that nothing closes. Every later `tenant_tx()` is then a savepoint that
            # RELEASEs without committing, so `published_at` is written and never becomes
            # visible to anyone else. The relay reports `published=N` from its own
            # snapshot, the lag gauge reads 0 on that same connection, and every other
            # process still sees the rows as pending -- a relay that looks perfectly
            # healthy and has published nothing.
            #
            # MOS-EXEC-041 clause 4 makes `published_at` the record of a broker
            # acknowledgement; a value only the writer can see is not a record.
            raise ValueError(
                "the outbox relay needs an autocommit connection of its own. With "
                "autocommit=False the advisory lock of clause 1 opens a transaction that "
                "never closes, every tenant_tx() inside it degrades to a SAVEPOINT, and "
                "published_at is written but never committed -- the relay then reports "
                "success while publishing nothing anyone else can see (MOS-EXEC-041.4)."
            )
        self._conn = conn
        self._producer = producer
        self._batch_size = batch_size
        self._tenants = tenants
        # Chapter 14's `relay.after_produce_before_mark_sent` (MOS-TEST-051). Called with
        # the row dict between the broker's ack and the `published_at` UPDATE. A test
        # raises from it; in a release image it is None and the branch is one `if`.
        # MOS-TEST-052 keeps fault-injection CODE out of release images; this hook holds
        # no fault logic of its own, only the seam.
        self._fault = fault
        self._holds_lock = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # One connection, and `run_forever()` runs on a thread. Clause 6's gauge is
        # scraped from somewhere else -- a metrics endpoint, an operator, this component's
        # conformance suite -- and every method here opens a transaction on `self._conn`.
        # Two transactions opened from two threads on one psycopg connection raise
        # `OutOfOrderTransactionNesting` at commit, which is how this lock came to exist:
        # the suite read the gauge while the relay was draining and the relay died with a
        # nesting error rather than a useful one.
        self._conn_lock = threading.RLock()

    # -- clause 1: leader election ------------------------------------------------
    def acquire(self) -> bool:
        """`pg_try_advisory_lock(hashtext('mos.outbox.relay'))`. Session-scoped.

        Session-scoped and not transaction-scoped: a transaction-level lock is released at
        every commit, so two relays would interleave batches and both would be "the one
        active instance" between statements. Returns False rather than blocking, so a
        standby relay stays a standby instead of becoming a queue of relays.
        """
        with self._conn_lock:
            row = self._conn.execute(ADVISORY_LOCK_SQL).fetchone()
            self._holds_lock = bool(row["got"] if isinstance(row, dict) else row[0])
            return self._holds_lock

    def release(self) -> None:
        with self._conn_lock:
            if self._holds_lock:
                self._conn.execute(_ADVISORY_UNLOCK_SQL)
                self._holds_lock = False

    @property
    def is_leader(self) -> bool:
        return self._holds_lock

    # -- clause 6: the lag gauge --------------------------------------------------
    def lag_seconds(self) -> float:
        """`now() - min(created_at) WHERE published_at IS NULL`, over every served tenant.

        MOS-EXEC-041 clause 6. Zero when nothing is pending, which is the honest value:
        a gauge that reported the age of the last published row would sit at a small
        number while the relay was dead.

        The tenant loop is MOS-STORE-225's, same as `drain_once()`. A gauge computed on a
        cross-tenant connection would be the escape hatch that requirement forbids, for
        a number.
        """
        worst = 0.0
        with self._conn_lock:
            for tenant in self._serving():
                with tenant_context(tenant), tenant_tx(self._conn):
                    row = self._conn.execute(
                        "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - min(created_at))), 0)"
                        " AS lag FROM job_outbox WHERE published_at IS NULL"
                    ).fetchone()
                if row is not None:
                    worst = max(
                        worst, float(row["lag"] if isinstance(row, dict) else row[0])
                    )
        return worst

    # -- clauses 2 and 4: the drain -----------------------------------------------
    def drain_once(self) -> RelayBatch:
        """Publish one batch per tenant. The whole of clauses 2 and 4.

        Per tenant, in one transaction:

          SELECT ... WHERE published_at IS NULL ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED

        then, per row, produce and -- only after the ack -- write `published_at`. The
        UPDATE is per row and not per batch on purpose: a batch-wide UPDATE would mark
        rows published that a mid-batch `ProduceFailed` never reached, which is the
        at-most-once behaviour clause 4 rules out.

        `FOR UPDATE SKIP LOCKED` in clause 2 is belt and braces next to the advisory lock
        of clause 1 -- and it is the brace that holds during the seconds around a leader
        handover, when the old relay's lock has not yet been released and the new one has
        already started.
        """
        published = failed = 0
        tenants = self._serving()
        with self._conn_lock:
            for tenant in tenants:
                with tenant_context(tenant):
                    published_t, failed_t = self._drain_tenant()
                published += published_t
                failed += failed_t
        return RelayBatch(
            published=published, failed=failed, tenants=len(tenants),
            lag_seconds=self.lag_seconds(),
        )

    def _drain_tenant(self) -> tuple[int, int]:
        published = failed = 0
        with tenant_tx(self._conn):
            rows = self._conn.execute(
                """
                SELECT id, job_id, topic, partition_key, envelope, attempts
                  FROM job_outbox
                 WHERE published_at IS NULL
                 ORDER BY id
                   FOR UPDATE SKIP LOCKED
                 LIMIT %s
                """,
                (self._batch_size,),
            ).fetchall()

            for row in rows:
                try:
                    self._producer.produce(
                        row["topic"],
                        key=row["partition_key"],
                        value=row["envelope"],
                        headers={
                            # MOS-EXEC-041's dedup key, lifted into a header so a consumer
                            # can drop a duplicate without deserialising the body.
                            "event_id": str(row["envelope"].get("event_id", "")),
                            "event_type": str(row["envelope"].get("event_type", "")),
                        },
                    )
                except ProduceFailed as exc:
                    # Clause 5's other half: the row is NOT deleted and NOT marked. It
                    # stays pending, `attempts` records how hard the relay has tried, and
                    # the lag gauge rises until someone looks.
                    self._conn.execute(
                        "UPDATE job_outbox SET attempts = attempts + 1, last_error = %s "
                        "WHERE id = %s",
                        (str(exc)[:1000], row["id"]),
                    )
                    failed += 1
                    log.warning("outbox row %s not published: %s", row["id"], exc)
                    continue

                # ---------------------------------------------------------------
                # `relay.after_produce_before_mark_sent` (MOS-TEST-051). The broker has
                # the record; `published_at` is not written. A relay killed here
                # republishes on restart and the consumer dedupes on `event_id`.
                # ---------------------------------------------------------------
                if self._fault is not None:
                    self._fault(dict(row))

                self._conn.execute(
                    "UPDATE job_outbox SET published_at = now(), last_error = NULL "
                    "WHERE id = %s",
                    (row["id"],),
                )
                published += 1
        return published, failed

    # -- clause 5: retention ------------------------------------------------------
    def sweep_published(self, *, older_than_days: int = RETENTION_DAYS) -> int:
        """Delete published rows older than 7 days. MOS-STORE-274, MOS-EXEC-041 clause 5.

        "Never delete an unpublished row. Published rows are deleted after 7 days." The
        `published_at IS NOT NULL` predicate below is the policy; `job_outbox_guard()` in
        the migration is the guarantee, and it binds the owner and a psql session too.

        chapter 12 section 12.16.1 wants this expressed as a `retention_policies` row with
        `object_class = 'job_outbox'`. That table does not exist in this deployment, so the
        rule lives here and the gap is in this component's report.
        """
        deleted = 0
        with self._conn_lock:
            for tenant in self._serving():
                with tenant_context(tenant), tenant_tx(self._conn):
                    cur = self._conn.execute(
                        "DELETE FROM job_outbox "
                        " WHERE published_at IS NOT NULL "
                        "   AND published_at < now() - make_interval(days => %s)",
                        (older_than_days,),
                    )
                    deleted += cur.rowcount
        return deleted

    # -- running it ---------------------------------------------------------------
    def run_forever(self, *, interval_s: float = 0.25) -> None:
        """Drain, sleep, repeat, until `stop()`. Only ever runs when it holds the lock."""
        if not self._holds_lock and not self.acquire():
            log.info("another relay holds mos.outbox.relay; standing by (MOS-EXEC-041.1)")
            return
        try:
            while not self._stop.is_set():
                try:
                    batch = self.drain_once()
                    if batch.alerting:
                        log.error(
                            "medicalos_outbox_lag_seconds=%.1f exceeds the %ss alert "
                            "threshold (MOS-EXEC-041.6)", batch.lag_seconds,
                            LAG_ALERT_SECONDS,
                        )
                except Exception:  # noqa: BLE001 - a relay that dies stops every dispatch
                    log.exception("outbox relay batch failed; retrying")
                self._stop.wait(interval_s)
        finally:
            self.release()

    def start(self, *, interval_s: float = 0.01) -> bool:
        """Run `run_forever` on a thread. Returns whether this instance won the election."""
        if not self.acquire():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, kwargs={"interval_s": interval_s},
            name="medos-outbox-relay", daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
            self._thread = None

    # -- internals ----------------------------------------------------------------
    def _serving(self) -> tuple[str, ...]:
        return self._tenants if self._tenants is not None else serving_tenants()


def wait_for_drain(relay: OutboxRelay, *, timeout_s: float = 5.0) -> bool:
    """Block until the relay reports no pending rows. For tests and for a clean shutdown.

    NOT for the serving path: a control-plane handler that waited for the relay would have
    converted the outbox back into the synchronous dual write it exists to avoid.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if relay.lag_seconds() == 0.0:
            return True
        time.sleep(0.005)
    return relay.lag_seconds() == 0.0
