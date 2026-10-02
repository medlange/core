# SPDX-License-Identifier: Apache-2.0
"""The runtime workers: who drives the pipeline, in which mode.

Two modes, one pipeline (`medos.sdk.pipeline.Pipeline`):

  * `LocalWorker` -- the archive next door triggers processing. `submit(study_uid)` runs
    one study synchronously; a deployment wraps it in whatever trigger it has (an Orthanc
    webhook, a directory watch, a scheduled re-query of the worklist). Results go to the
    `sink` the deployment passes -- store SEG/SR, render, index; the worker does not
    decide.

  * `ExternalWorker` -- a message bus drives. `run_forever` polls the inbound topic,
    decodes each message through the inbound `MessageMapping`, runs the pipeline,
    publishes a `StudyResult` through the outbound mapping, and on failure publishes an
    error descriptor to the error topic if one is configured. Ack happens only after the
    result (or the error) is published, so a crash mid-study re-delivers instead of
    silently dropping.

Both workers take the SAME pipeline; the mode is which adapter pair and which loop
wraps it.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from medos.sdk.adapters.bus import (
    BusAdapter,
    MessageMapping,
    StudyRequested,
    StudyResult,
    decode_request,
    encode,
    encode_document,
)
from medos.sdk.pipeline import Pipeline, PipelineResult, StudyTask

__all__ = [
    "LocalWorker",
    "ExternalWorker",
]


@dataclass
class LocalWorker:
    """Synchronous, locally-triggered processing.

    `sink` receives each `PipelineResult`; the default returns it, which makes
    `submit` usable as a plain function.
    """

    pipeline: Pipeline
    sink: Callable[[PipelineResult], object] = lambda result: result

    def submit(self, study_uid: str) -> PipelineResult:
        """Fetch, process, hand the result to the sink. Refusals propagate."""
        result = self.pipeline.run_study(study_uid)
        self.sink(result)
        return result


@dataclass
class ExternalWorker:
    """Bus-driven processing: message in, study processed, result (or error) out.

    The mappings carry the EXTERNAL system's vocabulary; the worker itself speaks only
    the canonical events. `error_mapping` is optional: without it a failed study is
    still not acked (it re-delivers), which is the honest shape for a deployment that
    has not defined what an error looks like.
    """

    pipeline: Pipeline
    bus: BusAdapter
    inbound_topic: str
    inbound: MessageMapping
    outbound_topic: str
    outbound: MessageMapping
    error_topic: str = ""
    error_mapping: MessageMapping | None = None

    def run_once(self) -> bool:
        """Process one available message. False when the bus yielded nothing."""
        for message in self.bus.messages(self.inbound_topic):
            request = decode_request(self.inbound, message.payload)
            try:
                result = self.pipeline.run_study(
                    StudyTask(study_uid=request.study_uid, task_id=request.task_id)
                )
                self.bus.publish(
                    self.outbound_topic,
                    encode(self.outbound, self._result_of(request, result)),
                )
            except Exception as exc:  # noqa: BLE001 - one bad study must not stall the queue
                if self.error_topic and self.error_mapping is not None:
                    self.bus.publish(
                        self.error_topic,
                        encode_document(self.error_mapping, self._error_of(request, exc)),
                    )
                else:
                    raise
            finally:
                if message.ack is not None:
                    message.ack()
            return True
        return False

    def run_forever(self) -> None:
        """The deployment loop. Runs until the process is signalled."""
        while True:
            self.run_once()

    @staticmethod
    def _result_of(request: StudyRequested, result: PipelineResult) -> StudyResult:
        ai_result = bool(
            result.findings.segments or result.findings.measurements
            or result.model_output.metrics
        )
        return StudyResult(
            study_uid=result.task.study_uid,
            task_id=result.task.task_id,
            model_id=result.model_id,
            model_version=result.model_version,
            ai_result=ai_result,
            report="; ".join(
                f"{m.name}={m.value}{m.unit}" for m in result.findings.measurements
            ),
            conclusion="processed",
            metrics=dict(result.findings.metrics),
            download_started_at=result.download_started_at,
            download_finished_at=result.download_finished_at,
            process_started_at=result.process_started_at,
            process_finished_at=result.process_finished_at,
        )

    @staticmethod
    def _error_of(request: StudyRequested, exc: Exception) -> Mapping[str, object]:
        return {
            "study_uid": request.study_uid,
            "task_id": request.task_id,
            "error": type(exc).__name__,
            "description": str(exc),
            "traceback": traceback.format_exc(limit=3),
        }
