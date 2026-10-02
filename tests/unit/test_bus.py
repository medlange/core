# SPDX-License-Identifier: Apache-2.0
"""The bus adapter: canonical events <-> external schemas, and the bus-driven worker.

The schema-flexibility promise is tested at its seam: an EXTERNAL shape (the MosMed-style
profile worked example) decodes into the SDK's canonical `StudyRequested`, and a canonical
`StudyResult` encodes into that shape's JSON -- and every way a mapping and a payload
disagree is a `CodecError` naming what is absent, never a guess.

`ExternalWorker.run_once` is tested against an in-memory bus and the same fakes the
pipeline tests use: message in -> study processed -> result published -> acked; a failing
study publishes the error descriptor to the error topic and acks too, so a crash does not
re-deliver forever.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from medos.sdk.adapters.bus import (
    BusMessage,
    CodecError,
    MessageMapping,
    StudyResult,
    decode_request,
    encode,
)
from medos.sdk.adapters.inference import EmbeddedInference
from medos.sdk.fixtures import selftest_spec_document
from medos.sdk.modelcard import ModelCard
from medos.sdk.pipeline import Pipeline
from medos.sdk.runtime import ExternalWorker, LocalWorker

from tests.unit.test_pipeline import (
    FakePacs,
    _card_root,
    _embedded_model,
    _volume_builder,
)

PROFILE = Path(__file__).resolve().parents[2] / "examples" / "bus" / "mosmed" / "profile.yaml"


#: The inbound mapping, as the example profile declares it.
INBOUND = MessageMapping(
    fields={
        "studyIUID": "study_uid",
        "taskId": "task_id",
        "modelId": "model_id",
        "modelVersion": "model_version",
    }
)

#: The outbound mapping's template, as the example profile declares it.
OUTBOUND = MessageMapping(
    template={
        "studyIUID": "{study_uid}",
        "taskId": "{task_id}",
        "aiResult": {
            "modelId": "{model_id}",
            "pathologyFlag": "{ai_result}",
            "report": "{report}",
            "dateTimeParams": {
                "downloadStartDT": "{download_started_at}",
                "processEndDT": "{process_finished_at}",
            },
            "metrics": "{metrics}",
        },
    }
)


def _result() -> StudyResult:
    from datetime import datetime

    at = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    return StudyResult(
        study_uid="1.2.3",
        task_id="task-9",
        model_id="pulmo.emphysema",
        model_version="1.0.0",
        ai_result=True,
        report="emphysema_percent=12.5%",
        conclusion="processed",
        metrics={"emphysema_percent": 12.5},
        download_started_at=at,
        download_finished_at=at,
        process_started_at=at,
        process_finished_at=at,
    )


def test_the_example_inbound_shape_decodes_to_the_canonical_request() -> None:
    request = decode_request(
        INBOUND,
        {
            "studyIUID": "1.2.40.0.13.1",
            "taskId": "task-9",
            "modelId": "1000",
            "modelVersion": "1.0.0",
        },
    )
    assert request.study_uid == "1.2.40.0.13.1"
    assert request.task_id == "task-9"
    assert request.model_id == "1000"


def test_the_example_outbound_shape_encodes_from_the_canonical_result() -> None:
    document = encode(OUTBOUND, _result())
    assert document["studyIUID"] == "1.2.3"
    assert document["aiResult"]["modelId"] == "pulmo.emphysema"
    assert document["aiResult"]["dateTimeParams"]["processEndDT"].startswith("2026-10-02")
    assert document["aiResult"]["metrics"] == {"emphysema_percent": 12.5}


def test_a_missing_inbound_field_is_a_codec_error_naming_it() -> None:
    with pytest.raises(CodecError, match="studyIUID"):
        decode_request(INBOUND, {"taskId": "task-9"})


def test_a_missing_template_field_is_a_codec_error_naming_it() -> None:
    mapping = MessageMapping(template={"x": "{metrics.emphysema_both}"})
    with pytest.raises(CodecError, match="emphysema_both"):
        encode(mapping, _result())


class MemoryBus:
    """One in-memory bus: queued inbound messages, recorded publications."""

    def __init__(self, inbound: list[Mapping[str, Any]]) -> None:
        self._inbound = list(inbound)
        self.published: list[tuple[str, Mapping[str, Any]]] = []
        self.acked = 0

    def messages(self, topic: str) -> Iterator[BusMessage]:
        if not self._inbound:
            return
        payload = self._inbound.pop(0)
        yield BusMessage(
            topic=topic, payload=payload, ack=lambda: self.__setattr__("acked", self.acked + 1)
        )

    def publish(self, topic: str, payload: Mapping[str, Any]) -> None:
        self.published.append((topic, payload))


ERROR_MAPPING = MessageMapping(
    template={
        "studyIUID": "{study_uid}",
        "aiResult": {"error": "{error}", "description": "{description}"},
    }
)


def _worker(tmp_path: Path, bus: MemoryBus, **kwargs) -> ExternalWorker:
    spec_document = selftest_spec_document()
    card = ModelCard.load(_card_root(tmp_path))
    pipeline = Pipeline(
        card,
        pacs=FakePacs(),
        inference=EmbeddedInference(_embedded_model),
        volume_builder=_volume_builder(spec_document),
    )
    return ExternalWorker(
        pipeline,
        bus=bus,
        inbound_topic="in",
        inbound=INBOUND,
        outbound_topic="out",
        outbound=OUTBOUND,
        **kwargs,
    )


def test_one_message_runs_the_study_and_publishes_the_result(tmp_path: Path) -> None:
    bus = MemoryBus([{
        "studyIUID": "1.2.3",
        "taskId": "task-1",
        "modelId": "pulmo.emphysema",
        "modelVersion": "1.0.0",
    }])
    worker = _worker(tmp_path, bus)

    assert worker.run_once() is True

    topics = [topic for topic, _ in bus.published]
    assert topics == ["out"]
    payload = bus.published[0][1]
    assert payload["studyIUID"] == "1.2.3"
    assert payload["aiResult"]["metrics"]["emphysema_percent"] == 12.5
    assert bus.acked == 1


def test_an_empty_bus_runs_nothing(tmp_path: Path) -> None:
    worker = _worker(tmp_path, MemoryBus([]))
    assert worker.run_once() is False
    assert worker.bus.published == []


def test_a_failing_study_publishes_the_error_descriptor_and_acks(tmp_path: Path) -> None:
    bus = MemoryBus([{
        "studyIUID": "1.2.3",
        "taskId": "task-1",
        "modelId": "pulmo.emphysema",
        "modelVersion": "1.0.0",
    }])
    spec_document = selftest_spec_document()
    card = ModelCard.load(_card_root(tmp_path))

    def boom(arrays, spacing):
        raise RuntimeError("model exploded")

    worker = ExternalWorker(
        Pipeline(
            card,
            pacs=FakePacs(),
            inference=EmbeddedInference(boom),
            volume_builder=_volume_builder(spec_document),
        ),
        bus=bus,
        inbound_topic="in",
        inbound=INBOUND,
        outbound_topic="out",
        outbound=OUTBOUND,
        error_topic="errors",
        error_mapping=ERROR_MAPPING,
    )

    assert worker.run_once() is True

    topics = [topic for topic, _ in bus.published]
    assert topics == ["errors"]
    payload = bus.published[0][1]
    assert payload["aiResult"]["error"] == "RuntimeError"
    assert "model exploded" in payload["aiResult"]["description"]
    assert bus.acked == 1


def test_local_worker_submits_synchronously(tmp_path: Path) -> None:
    spec_document = selftest_spec_document()
    card = ModelCard.load(_card_root(tmp_path))
    seen = []
    worker = LocalWorker(
        Pipeline(
            card,
            pacs=FakePacs(),
            inference=EmbeddedInference(_embedded_model),
            volume_builder=_volume_builder(spec_document),
        ),
        sink=seen.append,
    )

    result = worker.submit("1.2.3")

    assert seen == [result]
    assert result.task.study_uid == "1.2.3"
