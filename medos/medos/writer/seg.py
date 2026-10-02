# SPDX-License-Identifier: Apache-2.0
"""DICOM SEG writing (CONTRACT.md section 1: `writer/seg.py`).

Lifted from `spikes/week0/write_dicom_results.py::build_seg` per CONTRACT.md section 2.
The body is that function's; what changed is where the masks come from -- the
`OutputPlan`'s `SegmentPlan.mask`, sliced out of the bundle's `LabelMap`, instead of a
`dict[str, ndarray]` the caller assembled.

MOS-IMG-093/094: highdicom writes the object. Hand-assembling a SEG from pydicom Datasets
as a fallback is a CI failure, not a degraded mode, so a missing highdicom raises with the
install command rather than silently taking another path.

Spec: MOS-IMG-013, MOS-IMG-059, MOS-IMG-061, MOS-IMG-090, MOS-IMG-093, MOS-IMG-094,
MOS-IMG-100, MOS-IMG-101, MOS-IMG-103, MOS-IMG-104, MOS-IMG-107, MOS-IMG-108,
MOS-IMG-136.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian

from medos.core.concepts import ConceptDictionary
from medos.core.geometry import SourceGeometry
from medos.safety.marking import assert_marked
from medos.writer.identity import (
    OutputPlan,
    ai_series_description,
    apply_ai_marking,
    apply_inherited_attributes,
    normalise_pn,
)

__all__ = ["build_seg", "HighDicomUnavailable", "require_highdicom"]


class HighDicomUnavailable(RuntimeError):
    pass


def require_highdicom() -> Any:
    """MOS-IMG-093 pins highdicom; MOS-IMG-094 forbids hand-assembling a Dataset."""
    try:
        import highdicom as hd
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise HighDicomUnavailable(
            "highdicom is not installed, and MOS-IMG-094 forbids hand-assembling a SEG "
            "or SR from pydicom Datasets as a fallback.\n"
            "    pip install highdicom==0.28.1 pydicom==3.0.2\n"
            f"(import failed: {exc})"
        ) from exc
    return hd


def coded(hd: Any, concept: dict[str, Any]) -> Any:
    return hd.sr.CodedConcept(
        value=concept["code_value"],
        scheme_designator=concept["coding_scheme"],
        meaning=concept["code_meaning"],
    )


def build_seg(
    plan: OutputPlan,
    source_datasets: Sequence[Dataset],
    source: SourceGeometry,
    concepts: ConceptDictionary,
) -> Dataset:
    """Build the DICOM SEG. Shape follows the MOS-IMG-108 reference implementation."""
    hd = require_highdicom()
    identity = plan.identity

    algorithm_identification = hd.AlgorithmIdentificationSequence(
        name=identity.service_id,  # MOS-IMG-104
        version=identity.service_version,
        family=hd.sr.CodedConcept("123105", "DCM", "Artificial Intelligence"),
        source=f"medicalos://service/{identity.service_id}@{identity.service_version}",
        parameters={
            "preprocessing_spec_digest": identity.preprocessing_spec_digest,
            "medicalos_version": identity.medicalos_version,
        },
    )

    descriptions = []
    # (frames, rows, cols) with one channel per segment; label values are 1..N in the
    # MOS-IMG-066 order `plan_outputs` fixed.
    stacked = np.zeros(
        (len(source.sop_instance_uids), source.rows, source.columns, len(plan.segments)),
        dtype=np.uint8,
    )
    for idx, seg_plan in enumerate(plan.segments):
        stacked[..., idx] = seg_plan.mask.astype(np.uint8)
        profile = seg_plan.profile
        descriptions.append(
            hd.seg.SegmentDescription(
                segment_number=seg_plan.segment_number,
                segment_label=profile["label"][:64],
                segmented_property_category=coded(hd, concepts[profile["category"]]),
                segmented_property_type=coded(hd, concepts[profile["type"]]),
                algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,  # MOS-IMG-101
                algorithm_identification=algorithm_identification,
                tracking_id=seg_plan.tracking_id,  # MOS-IMG-103
                tracking_uid=seg_plan.tracking_uid,
                anatomic_regions=[coded(hd, concepts[profile["anatomic_region"]])],
            )
        )

    content_description = "AI-derived organ-at-risk segmentation"
    if identity.clinical_use_mode == "RESEARCH_ONLY":  # MOS-IMG-136
        content_description = "RESEARCH USE ONLY - " + content_description

    seg = hd.seg.Segmentation(
        source_images=list(source_datasets),  # ordered per MOS-IMG-013
        pixel_array=stacked,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,  # MOS-IMG-100
        segment_descriptions=descriptions,
        series_instance_uid=plan.seg_series_instance_uid,
        series_number=plan.seg_series_number,
        sop_instance_uid=plan.seg_sop_instance_uid,
        instance_number=1,  # MOS-IMG-090
        manufacturer=identity.legal_manufacturer_name,  # MOS-IMG-075
        manufacturer_model_name=identity.service_display_name,
        software_versions=identity.software_versions,
        device_serial_number=identity.device_serial_number,
        institution_name=identity.institution_name,
        series_description=ai_series_description(
            "Organ-at-risk segmentation (MedicalOS)", identity.clinical_use_mode
        ),
        content_label="MEDICALOS_AI",  # MOS-IMG-090
        content_description=content_description[:64],
        content_creator_name=normalise_pn(identity.legal_manufacturer_name),
        transfer_syntax_uid=ExplicitVRLittleEndian,  # MOS-IMG-061
        omit_empty_frames=True,  # MOS-IMG-107
    )
    seg.StationName = identity.deployment_id  # MOS-IMG-075/078
    apply_inherited_attributes(seg, source_datasets[0], is_seg=True)
    apply_ai_marking(seg, identity, plan, is_seg=True, src=source_datasets[0])

    # MOS-SAFE-039 / MOS-IMG-136: "the writer MUST refuse to emit an object that violates
    # any of them". One function, shared by the SEG, SR and SC writers, which is what
    # MOS-SAFE-039 asks for -- see `medos/medos/safety/marking.py` for why the check reads the
    # ASSEMBLED dataset rather than trusting the arguments that built it.
    assert_marked(seg, object_kind="SEG", mode=identity.clinical_use_mode)
    return seg
