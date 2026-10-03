# SPDX-License-Identifier: Apache-2.0
"""The result writer driver: pipeline findings -> DICOM SEG/SR through the ONE writer.

`PlatformWriter` is the shipped `medos.sdk.pipeline.ResultWriter` implementation. It
adds nothing of its own to the bytes: concepts resolve through the platform's concept
loader (`MOS-IMG-112` forbids inventing a code, and this driver has no table to invent
from), SEG and SR are built by `medos.writer` itself -- including attribute inheritance
and the AI marking -- and the label map is whatever the model produced, checked against
the source grid the writer's contract demands (`MOS-IMG-005`). A second SEG
implementation is forbidden; this module is a bridge, and the bridge names the
invariants it checks instead of assuming them.

THE ADAPTER-BOUNDARY CONTRACT this driver states explicitly: the label maps in
`ModelOutput.arrays["pred"]` must already be on the SOURCE grid. Mapping model space
back to source space is the inference adapter's job (the card's spec names the inverse
transform; a served model applies it in its own runtime). A mask whose shape disagrees
with the source geometry is refused, loudly, because a mis-grid mask written to the
PACS is a segmentation of the wrong patient geometry.

The construction is lazy for the same reason as the other drivers: nothing under
`medos.writer` is imported until `write` runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from medos.sdk.adapters import DriverMissing
from medos.sdk.pipeline import PipelineResult

if TYPE_CHECKING:
    pass

__all__ = ["PlatformWriter", "WriterRefused"]


class WriterRefused(RuntimeError):
    """The card, the model output, or the source material breaks a writer invariant.

    Named refusals, not bare exceptions, because a deployment driving this from a bus
    message needs the error descriptor to say WHICH invariant failed.
    """


def _segment_concepts(card: Any, concepts: Any) -> tuple[Any, ...]:
    """Resolve each segmentation descriptor's `structure` slug through the dictionary.

    The card names the structure (the slug the mask vocabulary uses); the dictionary
    owns the code (`MOS-IMG-112`). A descriptor without `structure`, or one the
    dictionary has no profile for, is refused by name.
    """
    from medos.core.bundle import CodedConcept

    resolved: list[CodedConcept] = []
    for descriptor in card.outputs:
        if descriptor.get("kind") != "segmentation":
            continue
        structure = descriptor.get("structure")
        if not structure:
            raise WriterRefused(
                f"segmentation descriptor {descriptor.get('name')!r} names no "
                f"'structure' slug; the writer resolves codes through the platform "
                f"dictionary and cannot guess one"
            )
        try:
            profile = concepts.profile(structure)
        except KeyError as exc:
            raise WriterRefused(
                f"structure {structure!r} has no segment profile in the platform "
                f"concept dictionary; MOS-IMG-112 forbids inventing one"
            ) from exc
        coded = concepts[profile["type"]]
        resolved.append(
            CodedConcept(
                scheme=str(coded["coding_scheme"]),
                code=str(coded["code_value"]),
                meaning=str(coded["code_meaning"]),
            )
        )
    return tuple(resolved)


def _label_map(card: Any, result: PipelineResult, segments: tuple[Any, ...]) -> Any:
    """Stack the model's per-value masks into the writer's LabelMap, grid-checked."""
    import numpy as np

    from medos.core.bundle import LabelMap

    if not segments:
        return None
    pred = result.model_output.arrays.get("pred")
    if pred is None:
        raise WriterRefused(
            "the card declares segments but the model output carries no 'pred' label map"
        )
    descriptors = [d for d in card.outputs if d.get("kind") == "segmentation"]
    array = np.zeros(pred.shape, dtype=np.uint8)
    for index, descriptor in enumerate(descriptors):
        value = int(descriptor["value"])
        mask = np.asarray(pred) == value
        array[mask] = np.uint8(index + 1)
    geometry = result.source_geometry
    grid = _source_grid_shape(geometry)
    if grid is not None and tuple(array.shape) != grid:
        raise WriterRefused(
            f"the label map is {tuple(array.shape)} but the source grid is "
            f"{grid}; masks must be on the SOURCE grid (MOS-IMG-005) -- mapping "
            f"model space back is the inference adapter's job"
        )
    return LabelMap(array=array, segments=segments)


def _findings(card: Any, result: PipelineResult, concepts: Any) -> tuple[Any, ...]:
    """One Finding carrying the card's measurements, coded through the dictionary."""
    from medos.core.bundle import CodedConcept, Measurement

    measurements = []
    for descriptor in card.outputs:
        if descriptor.get("kind") != "measurement":
            continue
        key = descriptor.get("concept_key")
        if not key:
            raise WriterRefused(
                f"measurement descriptor {descriptor.get('name')!r} names no "
                f"'concept_key' into the platform dictionary; the writer MUST raise "
                f"rather than invent a code (MOS-IMG-112)"
            )
        try:
            coded = concepts[key]
        except KeyError as exc:
            raise WriterRefused(
                f"concept {key!r} has no row in the platform concept dictionary"
            ) from exc
        source = descriptor.get("from") or descriptor.get("name")
        if source not in result.model_output.metrics:
            raise WriterRefused(
                f"measurement {key!r} reads {source!r}, which the model output "
                f"does not carry in metrics"
            )
        measurements.append(
            Measurement(
                name=CodedConcept(
                    scheme=str(coded["coding_scheme"]),
                    code=str(coded["code_value"]),
                    meaning=str(coded["code_meaning"]),
                ),
                value=float(result.model_output.metrics[source]),
                unit=str(descriptor.get("unit") or ""),
            )
        )
    from medos.core.bundle import Finding

    return (
        Finding(
            kind=card.model_id,
            present=True,
            score=None,
            measurements=tuple(measurements),
        ),
    )


def _source_grid_shape(geometry: Any) -> tuple[int, ...] | None:
    """The (K, J, I) the writer's contract demands, from what build_canonical_volume
    actually returns -- `SourceGeometry` carries `hu_array`, never `array`."""
    if geometry is None:
        return None
    hu = getattr(geometry, "hu_array", None)
    if hu is not None:
        return tuple(int(d) for d in hu.shape)
    rows = getattr(geometry, "rows", None)
    columns = getattr(geometry, "columns", None)
    paths = getattr(geometry, "paths", None)
    if rows and columns and paths is not None:
        return (len(paths), int(rows), int(columns))
    return None


def _requested_outputs(segments: tuple[Any, ...], findings: tuple[Any, ...]) -> tuple[str, ...]:
    has_measurements = any(f.measurements for f in findings)
    requested: list[str] = []
    if segments:
        requested.append("SEG")
    if has_measurements:
        requested.append("SR")
    return tuple(requested)


@dataclass
class PlatformWriter:
    """`ResultWriter` over the platform's one writer.

    `tenant_id` and `clinical_use_mode` are the deployment's legal statements; the
    shipped default is the research-only dev value, and a clinical deployment sets
    them deliberately. `service_id`/`service_version` say WHICH software produced the
    objects -- the SDK's name and version, overridable by a site that embeds it.
    """

    tenant_id: str = "00000000-0000-0000-0000-000000000000"
    clinical_use_mode: str = "RESEARCH_ONLY"
    service_id: str = "medos.sdk"
    service_version: str = "0.1.0"

    def write(self, result: PipelineResult, *, into: Path) -> Sequence[Path]:
        try:
            import medos.capabilities as _caps  # noqa: PLC0415,F401
            from medos.core.bundle import CapabilityOutcome, ResultBundle  # noqa: PLC0415
            from medos.writer.identity import build_job_identity, plan_outputs  # noqa: PLC0415
            from medos.writer.seg import build_seg  # noqa: PLC0415
            from medos.writer.sr import build_sr  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised by integration
            raise DriverMissing(
                "PlatformWriter needs the platform's writer modules. Install the SDK "
                "with its server dependencies (`pip install medos[server]`) or pass a "
                "ResultWriter whose implementation you already carry."
            ) from exc

        loader = getattr(_caps, "load_concepts", None)
        if not callable(loader):
            from medos.capabilities.base import load_concepts as loader  # noqa: PLC0415
        concepts = loader()

        segments = _segment_concepts(result.card, concepts)  # set by Pipeline below
        label_map = _label_map(result.card, result, segments)
        findings = _findings(result.card, result, concepts)
        requested = _requested_outputs(segments, findings)
        if not requested:
            raise WriterRefused(
                "the card declares no segmentation or measurement outputs, so there "
                "is nothing to write; the writer refuses to emit an empty study"
            )
        if not result.source_files:
            raise WriterRefused(
                "no source instance files on the result; the writer inherits "
                "attributes from the source study and cannot do that without it"
            )

        import pydicom  # noqa: PLC0415

        source_datasets = [pydicom.dcmread(str(p)) for p in result.source_files]

        outcome = CapabilityOutcome(
            capability_id=result.model_id,
            findings=findings,
            label_map=label_map,
            source_sop_instance_uids=tuple(
                str(u) for u in getattr(result.source_geometry, "sop_instance_uids", ())
            ),
        )
        bundle = ResultBundle(outcomes=(outcome,))

        job_id = result.task.task_id or f"sdk-{result.task.study_uid}-{result.model_id}"
        identity = build_job_identity(
            job_id=job_id,
            tenant_id=self.tenant_id,
            service_id=self.service_id,
            service_version=self.service_version,
            study_instance_uid=result.task.study_uid,
            capability_ids=(result.model_id,),
            requested_outputs=requested,
            clinical_use_mode=self.clinical_use_mode,
            model_version=result.model_version,
            expected_idempotency_key=None,
        )
        plan = plan_outputs(identity, bundle, concepts)

        into.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        seg = (
            build_seg(plan, source_datasets, result.source_geometry, concepts)
            if plan.writes_seg
            else None
        )
        if seg is not None:
            path = into / "result.seg.dcm"
            seg.save_as(str(path), enforce_file_format=True)
            written.append(path)
        sr = (
            build_sr(plan, seg, source_datasets, concepts)
            if plan.writes_sr
            else None
        )
        if sr is not None:
            path = into / "result.sr.dcm"
            sr.save_as(str(path), enforce_file_format=True)
            written.append(path)
        return written
