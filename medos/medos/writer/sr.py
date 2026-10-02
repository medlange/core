# SPDX-License-Identifier: Apache-2.0
"""DICOM SR (TID 1500) writing (CONTRACT.md section 1: `writer/sr.py`).

Lifted from `spikes/week0/write_dicom_results.py::build_sr` per CONTRACT.md section 2.

THE GAP THIS FILE CLOSES
------------------------
The spike could build an SR, and `verify_roundtrip.py` never wrote one: the round-trip
path stored a SEG and stopped, so OHIF's measurement panel had a segmentation to render
and no measurements to hydrate from. A SEG alone says "here is a shape"; the numbers --
lung volume in ml, %LAA at -950 HU -- live only in a TID 1500 SR, and without it the
weeks 1-2 exit check ("the measurement reported in the SR equals the measurement
recomputed from the stored SEG", docs/spec/15-delivery.md section 15.2.3) has no left-hand
side. `worker/steps.py::store_dicom` now STOWs both objects, always.

MOS-IMG-120: the value written here is the value the capability computed, passed through
`OutputPlan` unchanged. Nothing in this module recomputes a measurement -- see
`writer/identity.py`'s module docstring for why that is load-bearing rather than tidy.

Spec: MOS-IMG-060, MOS-IMG-091, MOS-IMG-109, MOS-IMG-115, MOS-IMG-116, MOS-IMG-117,
MOS-IMG-120, MOS-IMG-121, MOS-IMG-122, MOS-IMG-124, MOS-IMG-125, MOS-IMG-126.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian

from medos.core.concepts import ConceptDictionary
from medos.core.uids import SOP_CLASS_SEG
from medos.safety.marking import apply_research_marking, assert_marked
from medos.writer.identity import (
    OutputPlan,
    ai_series_description,
    apply_ai_marking,
    apply_inherited_attributes,
)
from medos.writer.seg import coded, require_highdicom

__all__ = ["build_sr", "ucum_display"]


# UCUM *display* strings for the units this slice emits. NOT a clinical code list --
# MOS-IMG-113's prohibition is on materialising the SNOMED/DCM/RadLex concept table in
# code, and these are the human-readable renderings of a UCUM unit code, whose scheme
# designator is always "UCUM" (MOS-IMG-115). Unknown units fall back to the code itself,
# which is always a legal CodeMeaning, so a new unit degrades to "less pretty" and never
# to "wrong".
_UCUM_DISPLAY = {"ml": "mL", "mL": "mL", "%": "%", "mm": "mm", "mm3": "mm3"}


def ucum_display(unit_code: str) -> str:
    return _UCUM_DISPLAY.get(unit_code, unit_code)


def build_sr(
    plan: OutputPlan,
    seg_dataset: Dataset | None,
    source_datasets: Sequence[Dataset],
    concepts: ConceptDictionary,
) -> Dataset:
    """Build the TID 1500 Comprehensive 3D SR. Shape follows MOS-IMG-126.

    `seg_dataset` is `None` when this job writes no SEG -- because none was requested, or
    because no capability returned a region to outline. MOS-IMG-121 fixes the evidence
    sequence as "the source study, every source series consumed, every source
    SOPInstanceUID ... plus every SEG instance written by the same job", so when the job
    writes no SEG the evidence is the source instances and nothing else. There is no SEG
    to reference and therefore no TID 1411 group either: `plan.segments` is empty in that
    case, so the loop below produces none and MOS-IMG-117's "same TrackingUID as the
    segment" has nothing to bind.

    This function is never called with NOTHING to report. A report with no measurement
    group at all would be a conformant TID 1500 document -- MOS-IMG-109's table requires
    TID 1411 only "for segment-derived measurements" and TID 1501 only "for study-level
    measurements", both conditional -- and it would be a useful one, since the four
    unconditional rows (language, device observation context, procedure reported, TID
    1600 Image Library) already say "these instances were examined", which is what
    chapter 18's HZ-13 needs to exist in the PACS. highdicom refuses to construct it
    (`TypeError: Argument 'imaging_measurements' is required`, a limitation its own source
    comment offers to relax) and MOS-IMG-094 forbids assembling the object any other way,
    so `plan_outputs` does not plan an SR in that case and records `no_measurements`
    instead. REPORTED in `medos/medos/writer/identity.py` and in the known-inconsistencies
    register; nothing in this module works around it.
    """
    hd = require_highdicom()
    from pydicom.sr.codedict import codes

    identity = plan.identity
    source_datasets = list(source_datasets)

    # MOS-IMG-124: a DEVICE observer, and no person observer. MOS-IMG-139 is the reason:
    # MedicalOS must never write an object that claims a human author.
    observation_context = hd.sr.ObservationContext(
        observer_device_context=hd.sr.ObserverContext(
            observer_type=codes.DCM.Device,
            observer_identifying_attributes=hd.sr.DeviceObserverIdentifyingAttributes(
                uid=plan.device_observer_uid,  # MOS-IMG-125: derived, never random
                name=identity.service_display_name,
                manufacturer_name=identity.legal_manufacturer_name,
                model_name=identity.service_display_name,
                serial_number=identity.device_serial_number,
            ),
        ),
    )

    source_image_refs = [
        hd.sr.SourceImageForSegmentation(
            referenced_sop_class_uid=str(ds.SOPClassUID),
            referenced_sop_instance_uid=str(ds.SOPInstanceUID),
        )
        for ds in source_datasets
    ]

    groups = []
    for seg_plan in plan.segments:
        measurements = []
        for m in seg_plan.measurements:
            measurements.append(
                hd.sr.Measurement(
                    # MOS-IMG-116: a coded name. The CodedConcept comes from the bundle,
                    # where the capability resolved it through the ConceptDictionary
                    # (MOS-IMG-112) -- it is not re-resolved here, because re-resolution
                    # is another place for the code to differ from the one the Result
                    # carries.
                    name=hd.sr.CodedConcept(
                        value=m.name.code,
                        scheme_designator=m.name.scheme,
                        meaning=m.name.meaning,
                    ),
                    value=m.value,  # MOS-IMG-120: byte-identical to the Result
                    unit=hd.sr.CodedConcept(  # MOS-IMG-115: UCUM
                        value=m.unit,
                        scheme_designator="UCUM",
                        meaning=ucum_display(m.unit),
                    ),
                )
            )
        profile = seg_plan.profile
        tracking = hd.sr.TrackingIdentifier(
            uid=seg_plan.tracking_uid,  # MOS-IMG-117: same TrackingUID as the segment
            identifier=seg_plan.tracking_id,
        )
        finding_type = coded(hd, concepts[profile["finding_type"]])
        finding_sites = [
            hd.sr.FindingSite(anatomic_location=coded(hd, concepts[key]))
            for key in profile["finding_sites"]
        ]
        if seg_dataset is None:
            # The region was computed but no SEG is being written -- an SR-only job
            # (`requested_outputs` without SEG). MOS-IMG-122 makes an SR that "references
            # instances absent from the destination" a battery failure, so a
            # `ReferencedSegment` pointing at a SOPInstanceUID this job never STOWs is not
            # an option: the reference would dangle in every viewer that resolved it.
            #
            # The measurement is real either way -- MOS-IMG-120 still binds its value to
            # the Result -- so it is written as a TID 1501 group carrying the same
            # TrackingUID, the same finding type and the same finding sites, and losing
            # only the pointer to a drawing nobody asked for.
            groups.append(
                hd.sr.MeasurementsAndQualitativeEvaluations(
                    tracking_identifier=tracking,
                    source_images=[
                        hd.sr.SourceImageForMeasurementGroup(
                            referenced_sop_class_uid=str(ds.SOPClassUID),
                            referenced_sop_instance_uid=str(ds.SOPInstanceUID),
                        )
                        for ds in source_datasets
                    ],
                    finding_type=finding_type,
                    finding_sites=finding_sites,
                    measurements=measurements,
                )
            )
            continue
        groups.append(
            hd.sr.VolumetricROIMeasurementsAndQualitativeEvaluations(
                tracking_identifier=tracking,
                referenced_segment=hd.sr.ReferencedSegment(  # MOS-IMG-116
                    sop_class_uid=SOP_CLASS_SEG,
                    sop_instance_uid=plan.seg_sop_instance_uid,
                    segment_number=seg_plan.segment_number,
                    source_images=source_image_refs,
                ),
                finding_type=finding_type,
                finding_sites=finding_sites,
                measurements=measurements,
            )
        )

    # TID 1501 groups for measurements whose region is not one segment (see
    # `writer/identity.py`, rule 2). No `ReferencedSegment`: a %LAA over the union of both
    # lungs is not a property of either segment, and claiming otherwise in a
    # `VolumetricROIMeasurements...` group would tell a reader that a per-lung number
    # exists. The TrackingUID is derived (MOS-IMG-062) and carries into the SR only.
    for group in plan.extra_groups:
        groups.append(
            hd.sr.MeasurementsAndQualitativeEvaluations(
                tracking_identifier=hd.sr.TrackingIdentifier(
                    uid=group.tracking_uid, identifier=group.tracking_id
                ),
                source_images=[
                    hd.sr.SourceImageForMeasurementGroup(
                        referenced_sop_class_uid=str(ds.SOPClassUID),
                        referenced_sop_instance_uid=str(ds.SOPInstanceUID),
                    )
                    for ds in source_datasets
                ],
                measurements=[
                    hd.sr.Measurement(
                        name=hd.sr.CodedConcept(
                            value=m.name.code,
                            scheme_designator=m.name.scheme,
                            meaning=m.name.meaning,
                        ),
                        value=m.value,  # MOS-IMG-120
                        unit=hd.sr.CodedConcept(
                            value=m.unit,
                            scheme_designator="UCUM",
                            meaning=ucum_display(m.unit),
                        ),
                    )
                    for m in group.measurements
                ],
            )
        )

    report = hd.sr.MeasurementReport(
        observation_context=observation_context,
        procedure_reported=coded(hd, concepts["procedure.ct_chest"]),
        # `None`, not `[]`: highdicom emits the TID 1500 "Imaging Measurements" container
        # for a sequence it is given, and a container with no measurement group in it is
        # a content item asserting an empty collection where the template's own answer is
        # to omit the row (MOS-IMG-109 marks both group templates conditional).
        imaging_measurements=groups or None,
        title=codes.DCM.ImagingMeasurementReport,  # MOS-IMG-109: DCM 126000
        language_of_content_item_and_descendants=hd.sr.LanguageOfContentItemAndDescendants(
            hd.sr.CodedConcept("eng", "RFC5646", "English")  # TID 1204
        ),
        # TID 1600 Image Library, one entry per consumed source instance. MOS-IMG-109
        # marks it MUST and MOS-IMG-122 makes an empty one a battery failure; highdicom
        # omits the library entirely unless this argument is supplied.
        referenced_images=source_datasets,
    )

    sr = hd.sr.Comprehensive3DSR(  # MOS-IMG-060: always Comprehensive 3D, never 88.33
        # MOS-IMG-121: source instances plus every SEG this job wrote -- of which there
        # are none when `seg_dataset` is None.
        evidence=source_datasets + ([] if seg_dataset is None else [seg_dataset]),
        content=report[0],
        series_instance_uid=plan.sr_series_instance_uid,
        series_number=plan.sr_series_number,
        sop_instance_uid=plan.sr_sop_instance_uid,
        instance_number=1,
        manufacturer=identity.legal_manufacturer_name,
        manufacturer_model_name=identity.service_display_name,
        software_versions=identity.software_versions,
        device_serial_number=identity.device_serial_number,
        institution_name=identity.institution_name,
        series_description=ai_series_description(
            "Organ-at-risk measurements (MedicalOS)", identity.clinical_use_mode
        ),
        is_complete=True,  # CompletionFlag COMPLETE
        is_verified=False,  # MOS-IMG-091: UNVERIFIED on every SR a job writes
        transfer_syntax_uid=ExplicitVRLittleEndian,
    )
    sr.StationName = identity.deployment_id
    apply_inherited_attributes(sr, source_datasets[0], is_seg=False)
    apply_ai_marking(sr, identity, plan, is_seg=False, src=source_datasets[0])

    # MOS-IMG-136 / MOS-SAFE-050: in research_only an SR MUST carry a TEXT content item
    # under the root saying so. This was the gap the 0.2.0 gate's `ruo-marking` check
    # exists to catch -- the SEG's ContentDescription already carried the prefix, the
    # private ClinicalUseMode was written, and the SR's banner was simply absent, so an
    # SR opened in a viewer that renders the document tree showed no research marking at
    # all. Applied AFTER `apply_ai_marking` because it appends to `ContentSequence`, which
    # highdicom populates at construction.
    apply_research_marking(sr, object_kind="SR", mode=identity.clinical_use_mode)

    # MOS-SAFE-039 / MOS-IMG-136: "the writer MUST refuse to emit an object that violates
    # any of them". Raises `MarkingAbsent` -> `FAILED`, `error.code =
    # safety_marking_absent` (MOS-SAFE-035 E3), never REJECTED: an unmarked object is a
    # defect in this writer, not a statement about the study. The same function runs again
    # in `worker/steps.py::store_dicom` immediately before the STOW, on the bytes that are
    # about to be posted, which is where MOS-SAFE-039 puts the check.
    assert_marked(sr, object_kind="SR", mode=identity.clinical_use_mode)
    return sr
