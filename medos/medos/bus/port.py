# SPDX-License-Identifier: Apache-2.0
"""The message-bus port, and the producer/consumer settings chapter 5 mandates.

`JobQueue` driver 2 needs a broker. It does NOT need Kafka's API surface: it needs
"append this record to this topic under this key, and tell me when the broker has it", and
"give me at most one record, and let me commit the offset after I have made the claim
durable". That is this port, and it is deliberately six methods wide.

WHY A PORT AND NOT `confluent_kafka` DIRECTLY
----------------------------------------------
Two reasons, and the second is the one that matters.

1. `docs/spec/15-delivery.md` MOS-REL-008: a gate check MUST NOT name a product. The
   0.3.0 gate check is `queue-driver-parity`, and a conformance suite that imports
   `confluent_kafka` cannot run on a machine without a broker, which means it does not run
   and the gate is decorative.

2. `MOS-EXEC-048` is a statement about a CONSUMER'S BEHAVIOUR under a broker that evicts
   slow members -- "If the handler blocks inside `poll()`, the broker concludes the
   consumer is dead, evicts it from the group and rebalances the partition to another
   consumer -- which starts the same job again on a second GPU." Proving the platform
   survives that requires a broker that can be MADE to evict on demand. A real Kafka can,
   after `max.poll.interval.ms` = five minutes of wall clock. `medos/medos/bus/memory.py` can,
   in a millisecond, deterministically, which is the difference between testing the hazard
   and documenting it.

The two drivers are `medos.bus.memory.LogBroker` (in-process, the one CI runs) and
`medos.bus.kafka.KafkaBus` (`confluent_kafka`, the one a deployment runs). Both implement
this port; `tests/integration/test_queue_parity.py` is parameterised over the `JobQueue`
drivers above them.

Spec: MOS-EXEC-041, MOS-EXEC-042, MOS-EXEC-047, MOS-EXEC-048, MOS-EXEC-050, MOS-REL-008.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable

__all__ = [
    "BusError",
    "ConsumerEvicted",
    "ConsumerConfig",
    "Producer",
    "ProducerConfig",
    "ProduceFailed",
    "Record",
    "RecordMetadata",
    "Consumer",
]


class BusError(RuntimeError):
    """Any transport-level failure. Never a reason to change a job's state.

    MOS-EXEC-002 and MOS-EXEC-040: Postgres is the source of truth. A produce that fails
    leaves the `job_outbox` row unpublished and the relay retries it; it MUST NOT be
    resolved by marking the job failed, because the job is fine -- it is the transport
    that is not.
    """


class ProduceFailed(BusError):
    """The broker did not acknowledge. The outbox row stays unpublished (MOS-EXEC-041.4)."""


class ConsumerEvicted(BusError):
    """The group evicted this consumer; its offset commit is refused.

    The analogue of Kafka's `CommitFailedException`, and MOS-EXEC-048's whole subject:
    "The evicted consumer then finishes, writes DICOM to the PACS, and its `commitSync`
    throws `CommitFailedException` AFTER the side effect."

    In this platform that sequence is survivable and the reason is lease fencing, not the
    broker: the claim is durable in `job_queue` before the offset is committed, and a
    second consumer that re-claims the job takes `fence_token + 1`, so the evicted
    consumer's eventual `Complete` is rejected with `ErrLeaseLost` before it can write
    anything (MOS-EXEC-027, MOS-EXEC-049).
    """


@dataclass(frozen=True)
class Record:
    """One record as delivered to a consumer."""

    topic: str
    partition: int
    offset: int
    key: str
    value: dict[str, Any]   # the MOS-EXEC-075 envelope, already decoded
    headers: dict[str, str]

    @property
    def event_id(self) -> str:
        """MOS-EXEC-041: consumers MUST deduplicate on `envelope.event_id`."""
        return str(self.value.get("event_id", ""))


@dataclass(frozen=True)
class RecordMetadata:
    """What the broker acknowledged. Returned only after the ack (MOS-EXEC-041.4)."""

    topic: str
    partition: int
    offset: int


@dataclass(frozen=True)
class ProducerConfig:
    """MOS-EXEC-041 clause 3, verbatim, as values rather than prose.

        "Produce with `enable.idempotence=true`, `acks=all`,
         `max.in.flight.requests.per.connection=5`, `compression.type=zstd`."

    `enable.idempotence=true` is the clause that makes clause 4 meaningful: without it a
    producer retry after a lost ack writes the record twice, so `published_at` would be
    recording "the broker acknowledged at least one of my copies" rather than "the record
    is on the log once". `max.in.flight=5` is the ceiling at which librdkafka still
    preserves per-partition order under idempotence; raising it silently loses the
    ordering MOS-EXEC-044's partition key exists to buy.

    Frozen, and asserted by `tests/unit/test_bus_topics.py`, because these are the kind of
    settings that get "tuned" in a Helm values file by someone chasing throughput.
    """

    enable_idempotence: bool = True
    acks: str = "all"
    max_in_flight_requests_per_connection: int = 5
    compression_type: str = "zstd"
    # Not in MOS-EXEC-041; a bounded wait so a dead broker surfaces as a ProduceFailed the
    # relay can record in `last_error` rather than as a hung relay with a rising lag gauge
    # and no explanation.
    delivery_timeout_ms: int = 120_000

    def as_librdkafka(self, *, bootstrap_servers: str) -> dict[str, Any]:
        return {
            "bootstrap.servers": bootstrap_servers,
            "enable.idempotence": self.enable_idempotence,
            "acks": self.acks,
            "max.in.flight.requests.per.connection":
                self.max_in_flight_requests_per_connection,
            "compression.type": self.compression_type,
            "delivery.timeout.ms": self.delivery_timeout_ms,
        }


@dataclass(frozen=True)
class ConsumerConfig:
    """MOS-EXEC-048's mandated consumer configuration, verbatim.

        max.poll.interval.ms   300000 (default, unchanged)
        max.poll.records       1
        enable.auto.commit     false
        session.timeout.ms     45000
        heartbeat.interval.ms  3000
        isolation.level        read_committed
        auto.offset.reset      earliest

    `max.poll.interval.ms` is left at its default ON PURPOSE and MOS-EXEC-048 says so:
    "Implementations MUST NOT respond to this by raising `max.poll.interval.ms`: that only
    lengthens the window during which a genuinely dead consumer holds a partition, and it
    does not help driver 1 at all." The five-minute limit is survivable because the
    handler returns in well under five seconds -- it persists the claim and hands off
    (`MOS-EXEC-047`), and the inference happens on another thread entirely.

    `enable.auto.commit=false` is the setting that makes MOS-EXEC-047 step 2 possible:
    "Only now is the Kafka offset committed." With auto-commit the offset can advance
    before the `job_queue` row is durable, and a crash in that window loses the dispatch.
    """

    group_id: str = "medicalos.job-runner"
    max_poll_interval_ms: int = 300_000
    max_poll_records: int = 1
    enable_auto_commit: bool = False
    session_timeout_ms: int = 45_000
    heartbeat_interval_ms: int = 3_000
    isolation_level: str = "read_committed"
    auto_offset_reset: str = "earliest"

    def __post_init__(self) -> None:
        # MOS-EXEC-048 is a MUST NOT, so it is enforced rather than documented. The only
        # legitimate reason to construct a different value is a test proving the hazard,
        # and `medos.bus.memory` exposes an explicit eviction control for that instead.
        if self.max_poll_interval_ms != 300_000:
            raise ValueError(
                "MOS-EXEC-048: max.poll.interval.ms MUST stay at its 300000 ms default. "
                "Raising it lengthens the window in which a genuinely dead consumer holds "
                "a partition; the answer to a long inference is consume -> persist -> "
                "hand off -> heartbeat (MOS-EXEC-047), not a larger timeout."
            )
        if self.max_poll_records != 1:
            raise ValueError(
                "MOS-EXEC-048: max.poll.records MUST be 1. One job per poll; back-pressure "
                "is expressed by not polling (MOS-EXEC-026, MOS-EXEC-064)."
            )
        if self.enable_auto_commit:
            raise ValueError(
                "MOS-EXEC-048 / MOS-EXEC-047: enable.auto.commit MUST be false -- the "
                "offset is committed only after the claim is durable in Postgres."
            )
        if self.isolation_level != "read_committed":
            raise ValueError("MOS-EXEC-048: isolation.level MUST be read_committed")
        if self.auto_offset_reset != "earliest":
            raise ValueError(
                "MOS-EXEC-048: auto.offset.reset MUST be earliest -- a new consumer group "
                "must not silently skip queued work."
            )

    def as_librdkafka(self, *, bootstrap_servers: str) -> dict[str, Any]:
        return {
            "bootstrap.servers": bootstrap_servers,
            "group.id": self.group_id,
            "max.poll.interval.ms": self.max_poll_interval_ms,
            "enable.auto.commit": self.enable_auto_commit,
            "session.timeout.ms": self.session_timeout_ms,
            "heartbeat.interval.ms": self.heartbeat_interval_ms,
            "isolation.level": self.isolation_level,
            "auto.offset.reset": self.auto_offset_reset,
        }


@runtime_checkable
class Producer(Protocol):
    """Synchronous, acknowledged produce. There is no fire-and-forget on this port.

    `produce()` returns only after the broker has acknowledged, because MOS-EXEC-041
    clause 4 -- "Set `published_at` only after the broker acknowledges" -- is
    unimplementable against an asynchronous API without a callback the relay would then
    have to join on anyway. The at-least-once guarantee is bought by that one blocking
    call.
    """

    def produce(
        self, topic: str, *, key: str, value: dict[str, Any],
        headers: dict[str, str] | None = ...,
    ) -> RecordMetadata: ...

    def flush(self, timeout_s: float = ...) -> int: ...

    def close(self) -> None: ...


@runtime_checkable
class Consumer(Protocol):
    """One record at a time, with a manual offset commit.

    `poll()` takes a timeout and may return `None`; that is Kafka's shape and it is also
    what keeps `JobQueue.Claim` honest about MOS-EXEC-026 ("`Claim` MUST NOT block").
    `commit()` advances the group offset past the last polled record and MUST be called
    only after the claim is durable in Postgres (MOS-EXEC-047 step 2).
    """

    def subscribe(self, topics: list[str]) -> None: ...

    def poll(self, timeout_s: float = ...) -> Record | None: ...

    def commit(self) -> None: ...

    def close(self) -> None: ...


# MOS-EXEC-042: "Kafka MUST be operated in KRaft mode. ZooKeeper mode was removed in Kafka
# 4.0 and MUST NOT appear in any compose file, Helm chart or compatibility matrix." The
# constant exists so the assertion has something to compare against and so a grep for
# `zookeeper` in this repository has a single, explanatory hit.
KRAFT_ONLY: Final[bool] = True
FORBIDDEN_BROKER_SETTINGS: Final[frozenset[str]] = frozenset({
    "zookeeper.connect", "zookeeper.connection.timeout.ms", "zookeeper.session.timeout.ms",
})
