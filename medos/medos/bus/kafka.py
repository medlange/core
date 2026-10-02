# SPDX-License-Identifier: Apache-2.0
"""The Kafka binding: `confluent_kafka` behind `medos.bus.port`.

`MOS-EXEC-039` defers the Kafka driver to 0.3.0 and adds three MUST NOTs that this module
respects by existing rather than by being imported: "It MUST NOT be a dependency of 0.1 or
0.2, MUST NOT appear in the 0.1 compose file, and its absence MUST NOT be worked around by
any component reaching for Kafka directly."

So `confluent_kafka` is imported INSIDE the constructors, not at module scope. Importing
this module costs nothing and requires nothing; constructing a `KafkaBus` against a
deployment that has no broker raises an error that names the two things missing. Every
other component reaches the broker through `medos.bus.port`, which is what "MUST NOT be
worked around by any component reaching for Kafka directly" means in code.

WHY `confluent_kafka` IS NOT IN `requirements-dev.txt`
------------------------------------------------------
There is no broker in `medos/deploy/compose/docker-compose.yml` -- its header says so in as many
words ("no kafka / redpanda   the queue is Postgres. A broker is 0.3.0") -- so the
dependency would be installed for nothing on every developer machine and in CI. It is
added when a broker is added, in the same change, and until then `MEDOS_KAFKA_BOOTSTRAP`
being unset is the honest signal that this deployment runs the in-process log.

The conformance suite is parameterised over the `JobQueue` drivers, not over the bus
drivers, so it runs unchanged the day a broker appears: point `MEDOS_KAFKA_BOOTSTRAP` at
it and the same `tests/integration/test_queue_parity.py` exercises this module instead of
`medos/medos/bus/memory.py`.

MOS-EXEC-042, ENFORCED AND NOT ASSUMED
---------------------------------------
"Kafka MUST be operated in KRaft mode. ZooKeeper mode was removed in Kafka 4.0 and MUST
NOT appear in any compose file, Helm chart or compatibility matrix." A ZooKeeper setting
in the config dict is refused at construction. It cannot be checked from the client side
whether the broker itself runs KRaft, and a check that cannot be made is not faked here.

Spec: MOS-EXEC-039, MOS-EXEC-041, MOS-EXEC-042, MOS-EXEC-047, MOS-EXEC-048.
"""

from __future__ import annotations

import json
import os
from typing import Any

from medos.bus.port import (
    FORBIDDEN_BROKER_SETTINGS,
    ConsumerConfig,
    ConsumerEvicted,
    ProduceFailed,
    ProducerConfig,
    Record,
    RecordMetadata,
)
from medos.bus.topics import validate_topic

__all__ = ["KafkaBus", "KafkaProducer", "KafkaConsumer", "bootstrap_servers", "available"]

_MISSING = (
    "the Kafka bus driver needs BOTH a broker and the client library:\n"
    "  * MEDOS_KAFKA_BOOTSTRAP must name the broker (KRaft mode -- MOS-EXEC-042)\n"
    "  * confluent-kafka must be installed\n"
    "Neither is present in this deployment: medos/deploy/compose/docker-compose.yml ships no\n"
    "broker (`no kafka / redpanda   the queue is Postgres. A broker is 0.3.0`). Until one\n"
    "is added, medos.bus.memory.LogBroker is the transport and the SAME conformance suite\n"
    "covers both (MOS-EXEC-032, queue-driver-parity)."
)


def bootstrap_servers(env: dict[str, str] | None = None) -> str:
    env = dict(os.environ) if env is None else env
    return env.get("MEDOS_KAFKA_BOOTSTRAP", "").strip()


def available(env: dict[str, str] | None = None) -> bool:
    """Is a real broker configured AND the client importable? No side effects."""
    if not bootstrap_servers(env):
        return False
    try:
        import confluent_kafka  # noqa: F401
    except ImportError:
        return False
    return True


def _require_client() -> Any:
    try:
        import confluent_kafka
    except ImportError as exc:  # pragma: no cover - exercised only without the library
        raise RuntimeError(_MISSING) from exc
    return confluent_kafka


def _reject_zookeeper(config: dict[str, Any]) -> None:
    bad = sorted(set(config) & FORBIDDEN_BROKER_SETTINGS)
    if bad:
        raise ValueError(
            f"MOS-EXEC-042: {', '.join(bad)} configures ZooKeeper mode, which was removed "
            "in Kafka 4.0 and MUST NOT appear in any compose file, Helm chart or "
            "compatibility matrix. Operate the broker in KRaft mode."
        )


class KafkaProducer:
    """Synchronous, acknowledged produce. MOS-EXEC-041 clause 3 and clause 4."""

    def __init__(self, *, servers: str, config: ProducerConfig | None = None) -> None:
        ck = _require_client()
        self._config = config or ProducerConfig()
        conf = self._config.as_librdkafka(bootstrap_servers=servers)
        _reject_zookeeper(conf)
        self._p = ck.Producer(conf)
        self._KafkaException = ck.KafkaException

    def produce(
        self, topic: str, *, key: str, value: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> RecordMetadata:
        """Produce and BLOCK until the broker acknowledges.

        MOS-EXEC-041 clause 4: "Set `published_at` only after the broker acknowledges, so
        the guarantee is at-least-once." The relay calls this and then writes
        `published_at`; a fire-and-forget produce would make that UPDATE a statement about
        a delivery callback that has not run yet.
        """
        validate_topic(topic)
        result: dict[str, Any] = {}

        def _on_delivery(err: Any, msg: Any) -> None:
            if err is not None:
                result["error"] = str(err)
            else:
                result["meta"] = RecordMetadata(
                    topic=msg.topic(), partition=msg.partition(), offset=msg.offset()
                )

        try:
            self._p.produce(
                topic,
                key=key.encode("utf-8"),
                value=json.dumps(value, separators=(",", ":")).encode("utf-8"),
                headers=list((headers or {}).items()),
                on_delivery=_on_delivery,
            )
            self._p.flush(self._config.delivery_timeout_ms / 1000.0)
        except self._KafkaException as exc:
            raise ProduceFailed(f"{topic}: {exc}") from exc

        if "error" in result or "meta" not in result:
            why = result.get(
                "error",
                f"no delivery report within {self._config.delivery_timeout_ms} ms",
            )
            raise ProduceFailed(f"{topic}: {why}")
        meta = result["meta"]
        assert isinstance(meta, RecordMetadata)
        return meta

    def flush(self, timeout_s: float = 10.0) -> int:
        return int(self._p.flush(timeout_s))

    def close(self) -> None:
        self._p.flush(5.0)


class KafkaConsumer:
    """One record per poll, manual offset commit. MOS-EXEC-047 and MOS-EXEC-048."""

    def __init__(self, *, servers: str, config: ConsumerConfig | None = None) -> None:
        ck = _require_client()
        self._config = config or ConsumerConfig()
        conf = self._config.as_librdkafka(bootstrap_servers=servers)
        _reject_zookeeper(conf)
        self._c = ck.Consumer(conf)
        self._KafkaError = ck.KafkaError
        self._KafkaException = ck.KafkaException
        self._inflight: Any = None
        self.consumer_id = f"kafka-{id(self):x}"

    @property
    def config(self) -> ConsumerConfig:
        return self._config

    def subscribe(self, topics: list[str]) -> None:
        for t in topics:
            validate_topic(t)
        self._c.subscribe(topics)

    def poll(self, timeout_s: float = 0.0) -> Record | None:
        msg = self._c.poll(timeout_s)
        if msg is None:
            return None
        if msg.error() is not None:
            if msg.error().code() == self._KafkaError._PARTITION_EOF:
                return None
            raise ConsumerEvicted(str(msg.error()))
        self._inflight = msg
        return Record(
            topic=msg.topic(),
            partition=msg.partition(),
            offset=msg.offset(),
            key=(msg.key() or b"").decode("utf-8"),
            value=json.loads((msg.value() or b"{}").decode("utf-8")),
            headers={k: (v or b"").decode("utf-8") for k, v in (msg.headers() or [])},
        )

    def commit(self) -> None:
        """Commit the offset. Called ONLY after the claim is durable in Postgres.

        MOS-EXEC-047 step 2: "the dispatch envelope is written to `job_queue` (driver 2),
        and the step plan and `RUNNING` transition commit. Only now is the Kafka offset
        committed."

        A `CommitFailedException` here means the group evicted this member while it held
        the record -- MOS-EXEC-048's sequence. It is raised, not swallowed: the caller must
        stop, and fencing (MOS-EXEC-027) is what makes the other runner's work correct.
        """
        if self._inflight is None:
            return
        try:
            self._c.commit(message=self._inflight, asynchronous=False)
        except self._KafkaException as exc:
            raise ConsumerEvicted(
                f"offset commit refused after the record was already handled: {exc} "
                "(MOS-EXEC-048; the claim is durable and the lease is fenced)"
            ) from exc
        finally:
            self._inflight = None

    def release(self) -> None:
        self._inflight = None

    def close(self) -> None:
        self._c.close()


class KafkaBus:
    """Factory pair, so a caller configures the broker once."""

    def __init__(self, *, servers: str | None = None) -> None:
        self.servers = servers or bootstrap_servers()
        if not self.servers:
            raise RuntimeError(_MISSING)
        _require_client()

    def producer(self, config: ProducerConfig | None = None) -> KafkaProducer:
        return KafkaProducer(servers=self.servers, config=config)

    def consumer(self, config: ConsumerConfig | None = None) -> KafkaConsumer:
        return KafkaConsumer(servers=self.servers, config=config)
