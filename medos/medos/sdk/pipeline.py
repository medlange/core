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
from typing import Any, Protocol

from medos.sdk.adapters.inference import InferenceAdapter, ModelOutput
from medos.sdk.adapters.pacs import PacsAdapter, SeriesRef
from medos.sdk.modelcard import ModelCard
from medos.sdk.postprocess import PostprocessResult, run_postprocess

__all__ = [
    "ResultWriter",
    "PipelineError",
    "StudyTask",
    "PipelineResult",
    "SeriesExclusion",
    "Pipeline",
]


class PipelineError(RuntimeError):
    """A study cannot be processed: nothing to select, nothing fetchable, model refused.

    Not the training package's `TrainingError`: this module serves models, it does not
    train them, and one vocabulary per side keeps a serving refusal out of the corpus
    plane's reports.

    C5: a refusal is a DICTIONARY, not a bare exception. `code` is the stable
    identifier an external system matches on; `detail` is the human sentence; both
    ride `as_dict()`, which the CLI and the bus error mapping emit verbatim.
    """

    def __init__(
        self,
        detail: str,
        *,
        code: str = "pipeline_refused",
        study_uid: str = "",
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.study_uid = study_uid

    def as_dict(self) -> dict[str, Any]:
        return {
            "refused": {
                "code": self.code,
                "study_uid": self.study_uid,
                "detail": str(self),
            }
        }


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StudyTask:
    """One unit of work: run the card's model against this study."""

    study_uid: str
    task_id: str = ""


@dataclass(frozen=True)
class SeriesExclusion:
    """One listed series the selector dropped, WITH THE REASON (C5).

    "The selector kept none" used to be the whole story an operator got. A study
    that lists CT + SEG + SR and runs with the CT-only default now reports three
    rows: the SEG/SR excluded because they are derived results, and — for a custom
    deployment selector — the reason it owns. Selection is first-class data, the
    same claim MOS-API-054 makes for the platform's jobs.
    """

    series_uid: str
    modality: str
    reason: str


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
    stored: tuple[Path, ...] = ()
    source_files: tuple[Path, ...] = ()
    exclusions: tuple[SeriesExclusion, ...] = ()
    source_geometry: Any = field(default=None, repr=False)
    card: Any = field(default=None, repr=False)
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


class ResultWriter(Protocol):
    """Findings -> DICOM result objects (SEG/SR), ready to STOW.

    THE SEAM, not an implementation. The shipped driver builds on the platform's
    one writer (`medos.writer`, reached lazily the way the other adapters reach
    their drivers); a site with its own conventions implements this protocol
    against them. Writing a second SEG implementation is forbidden
    (`MOS-IMG-003`'s one-implementation rule covers the whole exchange).
    """

    def write(self, result: PipelineResult, *, into: Path) -> Sequence[Path]:
        """Write result DICOM files under `into` and return their paths."""
        ...


#: `(instance_paths) -> (volume, source_geometry, info)` -- the pure core's builder by
#: default; injectable for tests and for sites with their own loader.
VolumeBuilder = Callable[..., tuple[Any, Any, dict[str, Any]]]


def _default_volume_builder() -> VolumeBuilder:
    from medos.core.geometry import build_canonical_volume  # noqa: PLC0415

    return build_canonical_volume


#: Modalities that are RESULTS, never input: a pipeline fetching them would run
#: the model on its own outputs (measured on the live E2E of 2026-10-02).
_DERIVED_MODALITIES = frozenset({"SEG", "SR", "RTSTRUCT", "RTPLAN", "RTDOSE", "PR", "KO", "SC"})


def _image_series_only(refs: Sequence[SeriesRef]) -> Sequence[SeriesRef]:
    """The permissive selector, kept as an explicit opt-in: image modalities only.

    Deployments serving more than CT pass this (or their own callable) as
    `select_series`. It is NOT the default since C5: the default is CT-only.
    """
    return [r for r in refs if r.modality.upper() not in _DERIVED_MODALITIES]


def _ct_only_default(refs: Sequence[SeriesRef]) -> Sequence[SeriesRef]:
    """THE DEFAULT SELECTOR since C5: CT series only.

    The shipped cards are CT models (the trainer's registered specs say so), and
    an MR series fed to a CT model fails later and louder than a selector refusing
    it now. Every dropped series is recorded with its reason in the result's
    `exclusions` -- silent narrowing is how a deployment learns to distrust logs.
    """
    return [r for r in refs if r.modality.upper() == "CT"]


def _default_exclusion_reason(ref: SeriesRef) -> str:
    """Why the DEFAULT selector dropped this series. Custom selectors own their
    reasons; the pipeline records theirs generically."""
    m = ref.modality.upper()
    if m in _DERIVED_MODALITIES:
        return f"derived modality {m} is a result, not model input"
    return (
        f"default selector keeps CT only (got {m or 'unknown'}); "
        "pass select_series to serve other modalities"
    )


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
        writer: ResultWriter | None = None,
        keep_work_dir: bool = False,
    ) -> None:
        self.card = card
        self.pacs = pacs
        self.inference = inference
        # THE DEFAULT SELECTOR IS CT-ONLY SINCE C5, and every exclusion it makes is
        # recorded on the result. What came before, measured on the live E2E of
        # 2026-10-02: an image-only default, kept because a study that already
        # carries results lists its SEG/SR series too, and a pipeline that fetches
        # them runs the model on its own outputs. CT-only keeps that protection and
        # adds the recorded reason; deployments serving MR/PT pass their own
        # callable the way the platform's selectors do.
        self._selector_is_default = select_series is None
        self.select_series = select_series or _ct_only_default
        self.volume_builder = volume_builder or _default_volume_builder()
        self.writer = writer
        self.keep_work_dir = keep_work_dir

    def run_study(self, task: StudyTask | str) -> PipelineResult:
        """Fetch, preprocess, infer, postprocess; with a writer, store SEG/SR too.

        Refusals propagate; nothing is faked. When `writer` is set, the findings
        become DICOM result objects through the ONE writer implementation and are
        STOWed back into the study through the same PACS adapter that fetched it;
        a store failure is a PipelineError, because a result the archive does not
        hold is a result that does not exist for the next reader.
        """
        if isinstance(task, str):
            task = StudyTask(study_uid=task)

        download_started = _utcnow()
        listed = self.pacs.list_series(task.study_uid)
        selected = tuple(self.select_series(listed))
        kept = {r.series_uid for r in selected}
        if self._selector_is_default:
            exclusions = tuple(
                SeriesExclusion(r.series_uid, r.modality, _default_exclusion_reason(r))
                for r in listed
                if r.series_uid not in kept
            )
        else:
            exclusions = tuple(
                SeriesExclusion(
                    r.series_uid, r.modality,
                    "dropped by the deployment's series selector",
                )
                for r in listed
                if r.series_uid not in kept
            )
        if not selected:
            why = "; ".join(f"{e.modality or '?'}: {e.reason}" for e in exclusions[:5])
            raise PipelineError(
                f"study {task.study_uid}: the PACS lists {len(listed)} series and the "
                f"selector kept none"
                + (f" ({why})" if why else "")
                + "; there is nothing to run the model on",
                code="no_eligible_series",
                study_uid=task.study_uid,
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
        volume, source_geometry, _info = self.volume_builder(fetched)
        from medos.sdk.preprocess import from_canonical_volume  # noqa: PLC0415

        prepared = from_canonical_volume(volume)
        chain = self.card.chain()
        model_output = self.inference.infer(self.card, chain, prepared)
        findings = run_postprocess(self.card, model_output)
        process_finished = _utcnow()

        stored: tuple[Path, ...] = ()
        if self.writer is not None:
            results_dir = work_dir / "results"
            files = tuple(self.writer.write(
                PipelineResult(
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
                    source_files=tuple(fetched),
                    exclusions=exclusions,
                    source_geometry=source_geometry,
                    card=self.card,
                    work_dir=work_dir,
                ),
                into=results_dir,
            ))
            try:
                self.pacs.store(files, task.study_uid)
            except Exception as exc:
                raise PipelineError(
                    f"study {task.study_uid}: results were written but the store "
                    f"refused: {exc}",
                    code="store_refused",
                    study_uid=task.study_uid,
                ) from exc
            stored = files

        keep = self.keep_work_dir or bool(stored)
        kept_work_dir: Path | None = work_dir
        if not keep:
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
            stored=stored,
            source_files=tuple(fetched),
            exclusions=exclusions,
            source_geometry=source_geometry,
            card=self.card,
            work_dir=kept_work_dir,
        )
