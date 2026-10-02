# SPDX-License-Identifier: Apache-2.0
"""The bus adapter: event-driven control, schema-flexible by design.

An external management system (the MosMed AI shape is the worked example:
`medos/examples/bus/mosmed/`) drives the SDK over a message bus: a message names a study, the
SDK pulls it from the archive the message implies, processes it, and publishes the
result in the shape the EXTERNAL system defined. The shapes differ per integration --
the schemas in the example directory are an EXAMPLE, not a contract this adapter knows.

So the adapter has three layers, and only the bottom one knows Kafka:

  1. `StudyRequested` / `StudyResult` -- the SDK's CANONICAL events. Everything the
     pipeline produces, in SDK vocabulary, with the download/process timings both
     deployment modes report.
  2. `MessageMapping` -- one per external message shape. A template with `{field}`
     placeholders (encode) and a field table (decode) translates between the canonical
     event and whatever JSON the external system speaks. Missing fields are an error
     NAMING the field; unknown fields are ignored.
  3. `KafkaBus` / `RabbitBus` -- poll-based drivers over `confluent-kafka` / `pika`,
     imported lazily so the base SDK install carries neither.

`runtime.ExternalWorker` composes the three layers with `Pipeline`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from medos.sdk.adapters import DriverMissing

__all__ = [
    "StudyRequested",
    "StudyResult",
    "BusMessage",
    "BusAdapter",
    "MessageMapping",
    "CodecError",
    "encode",
    "encode_document",
    "decode_request",
    "KafkaBus",
    "RabbitBus",
]


class CodecError(RuntimeError):
    """A mapping and a payload disagree: a template field is absent, a type is wrong."""


@dataclass(frozen=True)
class StudyRequested:
    """The canonical inbound event: process this study with this model."""

    study_uid: str
    task_id: str = ""
    model_id: str = ""
    model_version: str = ""
    received_at: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )


@dataclass(frozen=True)
class StudyResult:
    """The canonical outbound event: what one run produced, timings included."""

    study_uid: str
    task_id: str
    model_id: str
    model_version: str
    ai_result: bool
    report: str
    conclusion: str
    metrics: Mapping[str, float]
    download_started_at: datetime
    download_finished_at: datetime
    process_started_at: datetime
    process_finished_at: datetime

    def iso(self, value: datetime) -> str:
        return value.isoformat()


@dataclass(frozen=True)
class BusMessage:
    """One polled message. `ack` commits the offset; dropping without ack re-delivers."""

    topic: str
    payload: Mapping[str, Any]
    ack: Any = None


class BusAdapter(Protocol):
    """Poll-based consumer + fire-and-forget publisher."""

    def messages(self, topic: str) -> Iterator[BusMessage]:
        """Block for the next batch; yield each message exactly once."""
        ...

    def publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        ...


# --------------------------------------------------------------------------------------
# the mapping layer
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageMapping:
    """One external message shape, in both directions.

    `template` is the outbound JSON with `{field}` placeholders drawn from the event's
    own fields (`study_uid`, `task_id`, `metrics.emphysema_percent`, ...). `fields` maps
    an inbound payload's keys to canonical event fields (`studyIUID -> study_uid`).
    Both directions validate: a missing template field or a missing inbound key is a
    `CodecError` naming what is absent.
    """

    template: Mapping[str, Any] = field(default_factory=dict)
    fields: Mapping[str, str] = field(default_factory=dict)


def _resolve(template: Any, source: Mapping[str, Any], where: str) -> Any:
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        path = template[1:-1].split(".")
        value: Any = source
        for part in path:
            if not isinstance(value, Mapping) or part not in value:
                raise CodecError(
                    f"{where}: template names {{{'.'.join(path)}}} but the event "
                    f"carries no {'/'.join(path)}"
                )
            value = value[part]
        return value
    if isinstance(template, Mapping):
        return {k: _resolve(v, source, where) for k, v in template.items()}
    if isinstance(template, list):
        return [_resolve(v, source, where) for v in template]
    return template


def _event_document(event: Any) -> Mapping[str, Any]:
    if isinstance(event, StudyResult):
        return {
            "study_uid": event.study_uid,
            "task_id": event.task_id,
            "model_id": event.model_id,
            "model_version": event.model_version,
            "ai_result": event.ai_result,
            "report": event.report,
            "conclusion": event.conclusion,
            "metrics": dict(event.metrics),
            "download_started_at": event.download_started_at.isoformat(),
            "download_finished_at": event.download_finished_at.isoformat(),
            "process_started_at": event.process_started_at.isoformat(),
            "process_finished_at": event.process_finished_at.isoformat(),
        }
    if isinstance(event, StudyRequested):
        return {
            "study_uid": event.study_uid,
            "task_id": event.task_id,
            "model_id": event.model_id,
            "model_version": event.model_version,
        }
    raise CodecError(f"mapping an event of type {type(event).__name__}; the codec "
                     f"speaks StudyRequested and StudyResult")


def encode_document(mapping: MessageMapping, document: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a mapping's template against ANY flat-or-nested document.

    `encode` is the event-flavoured wrapper; the error path in
    `medos.sdk.runtime.ExternalWorker` resolves the same templates against its error
    descriptor, which is a plain mapping, not a canonical event.
    """
    return dict(_resolve(dict(mapping.template), document, "encode"))


def encode(mapping: MessageMapping, event: Any) -> dict[str, Any]:
    """Canonical event -> external JSON, per the mapping's template."""
    return encode_document(mapping, _event_document(event))


def decode_request(mapping: MessageMapping, payload: Mapping[str, Any]) -> StudyRequested:
    """External JSON -> canonical inbound event, per the mapping's field table."""
    values: dict[str, Any] = {}
    for external, canonical in mapping.fields.items():
        if external not in payload:
            raise CodecError(
                f"decode: payload carries no {external!r}, which the mapping needs "
                f"for {canonical!r}"
            )
        values[canonical] = payload[external]
    study_uid = values.get("study_uid")
    if not isinstance(study_uid, str) or not study_uid:
        raise CodecError("decode: study_uid resolved to nothing; a request without "
                         "a study is not a request")
    return StudyRequested(
        study_uid=study_uid,
        task_id=str(values.get("task_id") or ""),
        model_id=str(values.get("model_id") or ""),
        model_version=str(values.get("model_version") or ""),
    )


# --------------------------------------------------------------------------------------
# the drivers
# --------------------------------------------------------------------------------------


@dataclass
class KafkaBus:
    """`BusAdapter` over Apache Kafka (`confluent-kafka`, lazy import)."""

    bootstrap_servers: str
    group_id: str = "medos-sdk"
    poll_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        try:
            from confluent_kafka import (  # type: ignore[import-not-found]  # noqa: PLC0415
                Consumer,
                Producer,
            )
        except ImportError as exc:  # pragma: no cover - exercised by integration
            raise DriverMissing(
                "KafkaBus needs confluent-kafka. Install the SDK with its bus extra: "
                "`pip install medos[bus-kafka]`."
            ) from exc
        self._consumer_cls = Consumer
        self._producer = Producer({"bootstrap.servers": self.bootstrap_servers})
        self._consumer: Any = None
        self._bootstrap_servers = self.bootstrap_servers

    def messages(self, topic: str) -> Iterator[BusMessage]:
        if self._consumer is None:
            self._consumer = self._consumer_cls({
                "bootstrap.servers": self._bootstrap_servers,
                "group.id": self.group_id,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            })
            self._consumer.subscribe([topic])
        while True:
            polled = self._consumer.poll(self.poll_timeout_s)
            if polled is None:
                continue
            if polled.error():
                continue
            payload = json.loads(polled.value().decode("utf-8"))
            yield BusMessage(
                topic=topic,
                payload=payload,
                ack=lambda polled=polled: self._consumer.commit(
                    polled, asynchronous=False
                ),
            )

    def publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        self._producer.produce(
            topic, json.dumps(payload, default=str).encode("utf-8")
        )
        self._producer.flush()


@dataclass
class RabbitBus:
    """`BusAdapter` over RabbitMQ (`pika`, lazy import).

    One queue per topic; `messages` yields from a basic_get loop so the worker stays
    poll-based and testable without a broker thread.
    """

    url: str
    poll_timeout_s: float = 1.0

    def __post_init__(self) -> None:
        try:
            import pika  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised by integration
            raise DriverMissing(
                "RabbitBus needs pika. Install the SDK with its bus extra: "
                "`pip install medos[bus-rabbit]`."
            ) from exc
        self._pika = pika
        self._connection: Any = None

    def _channel(self, topic: str) -> Any:
        if self._connection is None or self._connection.is_closed:
            params = self._pika.URLParameters(self.url)
            self._connection = self._pika.BlockingConnection(params)
        channel = self._connection.channel()
        channel.queue_declare(queue=topic, durable=True)
        return channel

    def messages(self, topic: str) -> Iterator[BusMessage]:
        channel = self._channel(topic)
        while True:
            method, _props, body = channel.basic_get(queue=topic, auto_ack=False)
            if method is None:
                continue
            payload = json.loads(body.decode("utf-8"))
            yield BusMessage(
                topic=topic,
                payload=payload,
                ack=lambda method=method: channel.basic_ack(method.delivery_tag),
            )

    def publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        channel = self._channel(topic)
        channel.basic_publish(
            exchange="",
            routing_key=topic,
            body=json.dumps(payload, default=str).encode("utf-8"),
        )
