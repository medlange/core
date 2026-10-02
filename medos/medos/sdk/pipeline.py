# SPDX-License-Identifier: Apache-2.0
"""The serving pipeline: one study in, postprocessed model outputs out.

`Pipeline` is the SDK's core runtime and the whole reason the card exists:

    study (PACS) -> fetched instances -> canonical volume -> the card's
    preprocessing chain -> the model (inference adapter) -> the card's
    outputs descriptor (postprocess) -> structured findings

Two deployment modes drive the SAME pipeline through the same two adapters:

  * LOCAL -- a worker polls or is triggered by the archive it sits next to;
  * EXTERNAL -- a bus message names a study in a remote archive; the worker fetches,
    processes, and a later stage stores SEG/SR back and notifies (the writer and the bus
    live one layer up: `medos.sdk.runtime`, and the SEG/SR write is the writer's job).

`Pipeline.run_study` stops at the structured findings. What happens to them -- storing
to the PACS, notifying a bus, rendering in a viewer -- is the caller's composition, and
the timings on the result are the MosMed-shaped download/process window a caller needs
for either mode.

Spec: the SDK pivot; MOS-IMG-031 (the chain's steps); MOS-TRAIN-034 (one constructor).
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from medos.sdk.adapters.inference import InferenceAdapter, ModelOutput
from medos.sdk.adapters.pacs import PacsAdapter, SeriesRef
from medos.sdk.modelcard import ModelCard
from medos.sdk.postprocess import PostprocessResult, run_postprocess

__all__ = [
    "PipelineError",
    "StudyTask",
    "PipelineResult",
    "Pipeline",
]


class PipelineError(RuntimeError):
    """A study cannot be processed: nothing to select, nothing fetchable, model refused.

    Not the training package's `TrainingError`: this module serves models, it does not
    train them, and one vocabulary per side keeps a serving refusal out of the corpus
    plane's reports.
"""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StudyTask:
    """One unit of work: run the card's model against this study."""

    study_uid: str
    task_id: str = ""


@dataclass(frozen=True)
class PipelineResult:
    """What one run produced, with the timings both deployment modes report."""

    task: StudyTask
    series: tuple[SeriesRef, ...]
    model_output: ModelOutput
    findings: PostprocessResult
    model_id: str = ""
    model_version: str = ""
    download_started_at: datetime | None = None
    download_finished_at: datetime | None = None
    process_started_at: datetime | None = None
    process_finished_at: datetime | None = None
    work_dir: Path | None = field(default=None, repr=False)

    @property
    def download_seconds(self) -> float:
        return (
            self.download_finished_at - self.download_started_at
        ).total_seconds()

    @property
    def process_seconds(self) -> float:
        return (
            self.process_finished_at - self.process_started_at
        ).total_seconds()


#: `(instance_paths) -> (volume, source_geometry, info)` -- the pure core's builder by
#: default; injectable for tests and for sites with their own loader.
VolumeBuilder = Callable[..., tuple[Any, Any, dict[str, Any]]]


def _default_volume_builder() -> VolumeBuilder:
    from medos.core.geometry import build_canonical_volume  # noqa: PLC0415

    return build_canonical_volume


class Pipeline:
    """`card` + `pacs` + `inference`, run against studies.

    The series selector is a callable over what the PACS lists; the default takes every
    series the adapter returns, and a deployment narrows it (CT only, axial, largest)
    the same way the platform's selectors do.
    """

    def __init__(
        self,
        card: ModelCard,
        *,
        pacs: PacsAdapter,
        inference: InferenceAdapter,
        select_series: Callable[[Sequence[SeriesRef]], Sequence[SeriesRef]] | None = None,
        volume_builder: VolumeBuilder | None = None,
        keep_work_dir: bool = False,
    ) -> None:
        self.card = card
        self.pacs = pacs
        self.inference = inference
        self.select_series = select_series or (lambda refs: refs)
        self.volume_builder = volume_builder or _default_volume_builder()
        self.keep_work_dir = keep_work_dir

    def run_study(self, task: StudyTask | str) -> PipelineResult:
        """Fetch, preprocess, infer, postprocess. Refusals propagate; nothing is faked."""
        if isinstance(task, str):
            task = StudyTask(study_uid=task)

        download_started = _utcnow()
        listed = self.pacs.list_series(task.study_uid)
        selected = tuple(self.select_series(listed))
        if not selected:
            raise PipelineError(
                f"study {task.study_uid}: the PACS lists {len(listed)} series and the "
                f"selector kept none; there is nothing to run the model on"
            )
        work_dir = Path(
            tempfile.mkdtemp(prefix=f"medos-{self.card.model_id}-")
        )
        fetched: list[Path] = []
        for ref in selected:
            target = work_dir / ref.series_uid
            fetched.extend(self.pacs.fetch_series(task.study_uid, ref.series_uid, target))
        download_finished = _utcnow()

        process_started = _utcnow()
        volume, _geometry, _info = self.volume_builder(fetched)
        from medos.sdk.preprocess import from_canonical_volume  # noqa: PLC0415

        prepared = from_canonical_volume(volume)
        chain = self.card.chain()
        model_output = self.inference.infer(self.card, chain, prepared)
        findings = run_postprocess(self.card, model_output)
        process_finished = _utcnow()

        kept_work_dir: Path | None = work_dir
        if not self.keep_work_dir:
            import shutil

            shutil.rmtree(work_dir, ignore_errors=True)
            kept_work_dir = None

        return PipelineResult(
            task=task,
            series=selected,
            model_output=model_output,
            findings=findings,
            model_id=self.card.model_id,
            model_version=self.card.model_version,
            download_started_at=download_started,
            download_finished_at=download_finished,
            process_started_at=process_started,
            process_finished_at=process_finished,
            work_dir=kept_work_dir,
        )
