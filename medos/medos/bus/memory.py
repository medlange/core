# SPDX-License-Identifier: Apache-2.0
"""`LogBroker` -- an in-process partitioned log with Kafka's delivery semantics.

WHAT THIS IS, STATED PLAINLY SO NOBODY MISTAKES IT FOR SOMETHING ELSE
---------------------------------------------------------------------
It is not Kafka and it is not a Kafka emulator. It is an append-only partitioned log with
consumer groups, committed offsets, at-least-once delivery, per-partition ordering and
member eviction -- the five properties `JobQueue` driver 2 actually depends on -- and
nothing else. It has no replication, no ISR, no transactions, no compaction and no wire
protocol. `medos/medos/bus/kafka.py` is the driver a deployment runs.

WHY IT EXISTS AT ALL, WHICH IS THE PART WORTH ARGUING ABOUT
------------------------------------------------------------
`MOS-EXEC-048` names a specific, expensive failure: a handler that blocks inside `poll()`
past `max.poll.interval.ms` is evicted, its partition is rebalanced, "which starts the
same job again on a second GPU", and the evicted consumer's commit then throws AFTER it
has already written DICOM to the PACS. `MOS-TEST-046` requires the Kafka driver's Q13 to
assert the platform survives exactly that.

Against a real broker that assertion costs ten minutes of wall clock per run
(`2 x max.poll.interval.ms`), needs a broker in CI, and is nondeterministic. Here it costs
a millisecond and is exact, because eviction is a method call. A hazard you can trigger on
demand is one you can keep testing; a hazard that needs a ten-minute nightly is one that
gets deleted in six months.

The second reason is that the 0.3.0 gate check is named `queue-driver-parity`, and
`MOS-REL-008` forbids a gate check naming a product. A conformance suite that cannot run
without a broker is a gate that does not run.

WHAT IT DOES NOT PROVE
----------------------
That librdkafka is configured correctly, that the topics exist with the right partition
counts, or that a real broker behaves as modelled here. Those are deployment facts and
they are checked where deployment facts are checked -- `medos/medos/bus/kafka.py` asserts the
mandated settings against `confluent_kafka` at construction, and the same conformance
suite runs against a real broker when `MEDOS_KAFKA_BOOTSTRAP` is set. This class is what
makes that suite exist in the first place.

DELIVERY MODEL
--------------
* A topic has N partitions. A record's partition is `crc32(key) % N`, so one partition key
  -- `<tenant_id>:<study_instance_uid>`, MOS-EXEC-044 -- always lands on one partition and
  the per-key order of Q11 is a property of the log rather than of luck.
* A consumer group has one committed offset per partition. `poll()` hands out the record
  at that offset and marks the partition IN FLIGHT; no second record from that partition
  is delivered until the first is committed or its holder is evicted. That is the
  single-consumer-per-partition rule Kafka gets from assignment, expressed directly.
* `commit()` advances the offset past the in-flight record. A holder that never commits --
  because its process died -- leaves the record to be redelivered, which is at-least-once
  and is what `MOS-EXEC-041` clause 4 and the consumer-side dedup are built on.
* `evict(consumer)` makes the holder's in-flight records available again and makes its
  next `commit()` raise `ConsumerEvicted`. That is `max.poll.interval.ms` expiring,
  without the five minutes.

Spec: MOS-EXEC-041, MOS-EXEC-044, MOS-EXEC-047, MOS-EXEC-048, MOS-EXEC-050, MOS-TEST-046.
"""

from __future__ import annotations

import copy
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from medos.bus.port import (
    ConsumerConfig,
    ConsumerEvicted,
    ProduceFailed,
    ProducerConfig,
    Record,
    RecordMetadata,
)
from medos.bus.topics import TOPICS, validate_topic

__all__ = ["LogBroker", "MemoryProducer", "MemoryConsumer"]


# Section 5.8's partition counts, by topic shape. A topic created on first produce gets
# the count the spec assigns it rather than a default, because the partition count is what
# bounds consumer parallelism and a test that silently ran on one partition would prove
# nothing about ordering.
_DEFAULT_PARTITIONS = 6
_PARTITIONS_BY_NAME = {t.name: t.partitions for t in TOPICS}


@dataclass
class _Entry:
    key: str
    value: dict[str, Any]
    headers: dict[str, str]
    produced_at: float


@dataclass
class _Group:
    """One consumer group's cursor state over one topic."""

    committed: dict[int, int] = field(default_factory=dict)      # partition -> next offset
    inflight: dict[int, str] = field(default_factory=dict)       # partition -> consumer id


class LogBroker:
    """The broker. Thread-safe; one instance is shared by every producer and consumer."""

    def __init__(self, *, producer_config: ProducerConfig | None = None) -> None:
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._log: dict[str, list[list[_Entry]]] = {}     # topic -> partition -> entries
        self._groups: dict[tuple[str, str], _Group] = {}  # (group, topic) -> cursor
        self._evicted: set[str] = set()                   # consumer ids
        self._producer_config = producer_config or ProducerConfig()
        # Fault injection for the conformance suite: the next N produces raise.
        self._fail_next_produces = 0
        self._produced_total = 0

    # -- administration -----------------------------------------------------------
    def ensure_topic(self, topic: str, *, partitions: int | None = None) -> None:
        validate_topic(topic)
        with self._lock:
            if topic in self._log:
                return
            n = partitions or _PARTITIONS_BY_NAME.get(topic) or _DEFAULT_PARTITIONS
            self._log[topic] = [[] for _ in range(n)]

    def topics(self) -> list[str]:
        with self._lock:
            return sorted(self._log)

    def partition_of(self, topic: str, key: str) -> int:
        """`crc32(key) % partitions`.

        MOS-EXEC-044: one `<tenant_id>:<study_instance_uid>` maps to one partition, which
        "co-locates all work for one study on one partition ... and keeps tenants
        distributed across partitions". The hash is crc32 rather than Python's `hash()`
        because `hash()` of a str is salted per process and the co-location would then
        hold only within one process -- true in a test, false in a deployment.
        """
        self.ensure_topic(topic)
        with self._lock:
            return zlib.crc32(key.encode("utf-8")) % len(self._log[topic])

    def backlog(self, group_id: str, topic: str) -> int:
        """Records not yet committed by `group_id`. For assertions, not for control flow."""
        with self._lock:
            self.ensure_topic(topic)
            g = self._groups.setdefault((group_id, topic), _Group())
            return sum(
                len(part) - g.committed.get(p, 0)
                for p, part in enumerate(self._log[topic])
            )

    def records(self, topic: str) -> list[dict[str, Any]]:
        """Every record on `topic`, in partition then offset order. Tests only."""
        with self._lock:
            self.ensure_topic(topic)
            return [copy.deepcopy(e.value) for part in self._log[topic] for e in part]

    # -- fault injection ----------------------------------------------------------
    def fail_next_produces(self, n: int) -> None:
        """Make the next `n` produces raise `ProduceFailed`.

        MOS-EXEC-041 clause 4 is the requirement under test: the outbox row stays
        unpublished and the relay retries it. Chapter 14's fault point for the other half
        of that window -- after the produce, before `published_at` is written -- lives in
        `medos.bus.outbox.OutboxRelay`, because that is where the code is.
        """
        with self._lock:
            self._fail_next_produces = n

    def corrupt_first(self, topic: str, key: str, value: dict[str, Any]) -> bool:
        """Replace the earliest record under `key` with `value`. Conformance suite only.

        `MOS-TEST-045` Q14 needs a queue entry whose payload fails the message schema, and
        `MOS-EXEC-025` makes one unreachable through the port: "No component may write to
        `job_queue` or produce to a work topic directly." A poison entry therefore cannot
        arise in this platform except by reaching into the transport, which is what this
        does -- the bus-side twin of the conformance suite's `UPDATE job_queue SET
        envelope = ...` for driver 1. Both are the same act against two transports.
        """
        with self._lock:
            self.ensure_topic(topic)
            p = zlib.crc32(key.encode("utf-8")) % len(self._log[topic])
            for entry in self._log[topic][p]:
                if entry.key == key:
                    entry.value = copy.deepcopy(value)
                    return True
        return False

    def evict(self, consumer_id: str) -> None:
        """Evict a group member, as `max.poll.interval.ms` expiring would.

        MOS-EXEC-048: "the broker concludes the consumer is dead, evicts it from the group
        and rebalances the partition to another consumer -- which starts the same job again
        on a second GPU". Everything in flight for that consumer becomes available again,
        and its next `commit()` raises, which is `CommitFailedException`.
        """
        with self._lock:
            self._evicted.add(consumer_id)
            for g in self._groups.values():
                for p, owner in list(g.inflight.items()):
                    if owner == consumer_id:
                        del g.inflight[p]
            self._wake.notify_all()

    # -- producer side ------------------------------------------------------------
    def _produce(
        self, topic: str, key: str, value: dict[str, Any], headers: dict[str, str]
    ) -> RecordMetadata:
        validate_topic(topic)
        self.ensure_topic(topic)
        with self._lock:
            if self._fail_next_produces > 0:
                self._fail_next_produces -= 1
                raise ProduceFailed(
                    f"broker refused the record for {topic} (injected). "
                    "MOS-EXEC-041.4: published_at stays NULL and the relay retries."
                )
            p = zlib.crc32(key.encode("utf-8")) % len(self._log[topic])
            part = self._log[topic][p]
            offset = len(part)
            part.append(_Entry(key=key, value=copy.deepcopy(value),
                               headers=dict(headers), produced_at=time.monotonic()))
            self._produced_total += 1
            self._wake.notify_all()
            return RecordMetadata(topic=topic, partition=p, offset=offset)

    # -- consumer side ------------------------------------------------------------
    def _poll(
        self, consumer_id: str, group_id: str, topics: list[str], timeout_s: float
    ) -> Record | None:
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._wake:
            while True:
                if consumer_id in self._evicted:
                    # A rebalanced member gets nothing until it rejoins. Rejoining is
                    # `MemoryConsumer.rejoin()`, which is what a restarted process does.
                    rec = None
                else:
                    rec = self._take_locked(consumer_id, group_id, topics)
                if rec is not None:
                    return rec
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._wake.wait(min(remaining, 0.05))

    def _take_locked(
        self, consumer_id: str, group_id: str, topics: list[str]
    ) -> Record | None:
        for topic in topics:
            self.ensure_topic(topic)
            g = self._groups.setdefault((group_id, topic), _Group())
            for p, part in enumerate(self._log[topic]):
                if p in g.inflight:
                    continue            # one in-flight record per partition per group
                nxt = g.committed.get(p, 0)
                if nxt >= len(part):
                    continue
                g.inflight[p] = consumer_id
                e = part[nxt]
                return Record(topic=topic, partition=p, offset=nxt, key=e.key,
                              value=copy.deepcopy(e.value), headers=dict(e.headers))
        return None

    def _commit(self, consumer_id: str, group_id: str, rec: Record | None) -> None:
        with self._lock:
            if consumer_id in self._evicted:
                raise ConsumerEvicted(
                    f"consumer {consumer_id} was evicted from group {group_id}; "
                    "the offset commit is refused (MOS-EXEC-048). The claim is already "
                    "durable in job_queue and fencing (MOS-EXEC-027) is what keeps the "
                    "second runner's work correct."
                )
            if rec is None:
                return
            g = self._groups.setdefault((group_id, rec.topic), _Group())
            if g.inflight.get(rec.partition) != consumer_id:
                raise ConsumerEvicted(
                    f"consumer {consumer_id} no longer holds "
                    f"{rec.topic}[{rec.partition}]; offset commit refused"
                )
            del g.inflight[rec.partition]
            g.committed[rec.partition] = rec.offset + 1
            self._wake.notify_all()

    def _release(self, consumer_id: str, group_id: str, rec: Record | None) -> None:
        """Give an in-flight record back without committing it. A clean process exit.

        MOS-EXEC-050 rejects `consumer.pause()` as an alternative to handing off, "because
        it keeps the work in memory, so a process kill loses it". Releasing is the honest
        opposite: the record goes back on the log and someone else gets it.
        """
        with self._lock:
            if rec is None:
                return
            g = self._groups.get((group_id, rec.topic))
            if g is not None and g.inflight.get(rec.partition) == consumer_id:
                del g.inflight[rec.partition]
                self._wake.notify_all()

    def _rejoin(self, consumer_id: str) -> None:
        with self._lock:
            self._evicted.discard(consumer_id)

    # -- factories ----------------------------------------------------------------
    def producer(self) -> MemoryProducer:
        return MemoryProducer(self)

    def consumer(self, config: ConsumerConfig | None = None, *,
                 consumer_id: str | None = None) -> MemoryConsumer:
        return MemoryConsumer(self, config or ConsumerConfig(), consumer_id=consumer_id)


class MemoryProducer:
    """`medos.bus.port.Producer` over a `LogBroker`. Synchronous and acknowledged."""

    def __init__(self, broker: LogBroker) -> None:
        self._broker = broker
        self._closed = False

    def produce(
        self, topic: str, *, key: str, value: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> RecordMetadata:
        if self._closed:
            raise ProduceFailed("producer is closed")
        return self._broker._produce(topic, key, value, headers or {})

    def flush(self, timeout_s: float = 10.0) -> int:
        return 0    # every produce already blocked until acknowledged

    def close(self) -> None:
        self._closed = True


_CONSUMER_SEQ = [0]
_CONSUMER_SEQ_LOCK = threading.Lock()


class MemoryConsumer:
    """`medos.bus.port.Consumer` over a `LogBroker`.

    Holds at most one uncommitted record, because `max.poll.records = 1` is mandated
    (MOS-EXEC-048) and because "one job per poll; back-pressure is expressed by not
    polling" is how load-shedding avoids consuming retry budget (MOS-EXEC-064).
    """

    def __init__(self, broker: LogBroker, config: ConsumerConfig, *,
                 consumer_id: str | None = None) -> None:
        self._broker = broker
        self._config = config
        if consumer_id is None:
            with _CONSUMER_SEQ_LOCK:
                _CONSUMER_SEQ[0] += 1
                consumer_id = f"mem-consumer-{_CONSUMER_SEQ[0]}"
        self.consumer_id = consumer_id
        self._topics: list[str] = []
        self._inflight: Record | None = None
        self._closed = False

    @property
    def config(self) -> ConsumerConfig:
        return self._config

    def subscribe(self, topics: list[str]) -> None:
        for t in topics:
            validate_topic(t)
            self._broker.ensure_topic(t)
        self._topics = list(topics)

    def poll(self, timeout_s: float = 0.0) -> Record | None:
        if self._closed:
            return None
        if self._inflight is not None:
            raise RuntimeError(
                "poll() called with an uncommitted record in flight. max.poll.records is "
                "1 (MOS-EXEC-048); commit the offset after the claim is durable "
                "(MOS-EXEC-047 step 2) before polling again."
            )
        rec = self._broker._poll(
            self.consumer_id, self._config.group_id, self._topics, timeout_s
        )
        self._inflight = rec
        return rec

    def commit(self) -> None:
        self._broker._commit(self.consumer_id, self._config.group_id, self._inflight)
        self._inflight = None

    def release(self) -> None:
        """Drop the in-flight record without committing. Redelivered to someone else."""
        self._broker._release(self.consumer_id, self._config.group_id, self._inflight)
        self._inflight = None

    def rejoin(self) -> None:
        """Rejoin the group after an eviction -- what a restarted process does."""
        self._broker._rejoin(self.consumer_id)

    def close(self) -> None:
        self.release()
        self._closed = True
