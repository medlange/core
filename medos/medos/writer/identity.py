# SPDX-License-Identifier: Apache-2.0
"""Attribute inheritance, SeriesNumber allocation and the deterministic output plan.

CONTRACT.md section 1: `writer/identity.py` -- "attribute inheritance + SeriesNumber
allocation". Lifted from `spikes/week0/write_dicom_results.py` per CONTRACT.md section 2:
`normalise_pn`, `ai_series_description`, `apply_inherited_attributes`, `apply_ai_marking`,
`_pin_dimension_organization_uid` and the `SegmentPlan`/`OutputPlan` shape are that file's
bodies, moved rather than rewritten.

ONE behavioural change in the move, and it is the point of this component
-------------------------------------------------------------------------
The spike's `plan_outputs()` RECOMPUTED every measurement from the masks
(`measure_volume_ml`, `measure_laa_percent`). This version takes them from the
`ResultBundle` instead.

That is not tidying. MOS-IMG-120 requires the value in the SR to be byte-identical to the
value in the Result, and two independent computations of the same number are two places
for it to drift -- which is precisely how the round-trip path ended up with a SEG and no
SR worth hydrating. The bundle is now the single source of every measured value: the
capability computes it once, in source geometry (CONTRACT.md section 5), the SR renders
it, and `result_measurements` stores it.

Segment / measurement attribution
---------------------------------
CONTRACT.md section 5's `Finding` carries no reference to the segment it describes, so the
attribution rule is stated here and is deterministic:

  1. a finding whose `kind` equals a segment's structure slug attaches to that SEGMENT
     and becomes a TID 1411 volumetric ROI measurement group referencing it;
  2. anything left over -- a measurement computed over the union of several segments,
     which is exactly what `emphysema_laa`'s %LAA is -- becomes a SEPARATE, non-volumetric
     TID 1501 measurement group that references no single segment;
  3. with exactly one segment, rule 2 collapses into rule 1, because "the union" and "that
     segment" are then the same region.

Rule 2 is the part worth defending. The tempting alternatives are both wrong: attaching a
whole-lung %LAA to the right-lung segment asserts a per-lung number nobody computed, and
attaching it to BOTH segments double-counts it for any consumer that sums measurement
groups. A measurement whose region is not one ROI is modelled as a measurement group with
no referenced segment, which is what TID 1500 provides for exactly this.

That `Finding` carries no segment reference at all is a REPORTED contract gap: with it,
rule 1 would not need to match on a string.

A negative result is a RESULT
----------------------------
`plan_outputs` is total: every bundle, including one with no label map and one whose
every segment is empty, yields a plan. "There is nothing to outline" is the modal
outcome of a detection capability and is not a platform malfunction; see that function's
docstring for the argument and for the three judgements it deliberately does not make.

REPORTED, chapter 4 is silent and its own toolchain forces the answer. Nothing in chapter
4 says whether a negative result writes an SR with no measurements, an empty SEG, or no
object at all; MOS-IMG-098 and MOS-IMG-143 settle the SEG by implication (zero label maps
map to zero SEG series, and a SEG with no segment has an empty Type 1 `SegmentSequence`),
but the SR half has no rule. TID 1500 permits a Measurement Report with no Imaging
Measurements container and MOS-IMG-109 marks both group templates conditional, so the
object is legal; highdicom refuses to construct it and MOS-IMG-094 forbids assembling one
by hand. The consequence is clinical and belongs in the hazard register rather than in a
comment: a capability whose negative carries no NUMBER produces no DICOM object at all,
so its negative is legible in this platform's API and invisible in the hospital's PACS --
chapter 18's HZ-13 residual, unchanged. A capability makes its negative legible there by
reporting the number (a count of zero, a burden of 0 ml). Nothing requires it to.

Spec: MOS-IMG-062..069, MOS-IMG-072, MOS-IMG-073, MOS-IMG-075..079, MOS-IMG-085,
MOS-IMG-089, MOS-IMG-090, MOS-IMG-092, MOS-IMG-095, MOS-IMG-098, MOS-IMG-109,
MOS-IMG-120, MOS-IMG-127, MOS-IMG-133..138, MOS-IMG-143, MOS-EXEC-014, MOS-EXEC-023,
MOS-EXEC-053, MOS-SVC-011, MOS-SVC-090.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from pydicom.dataset import Dataset

from medos.core.bundle import CapabilityOutcome, Measurement, ResultBundle
from medos.core.concepts import ConceptDictionary
from medos.core.errors import SystemFailure
from medos.core.uids import MEDICALOS_UID_NAMESPACE, JobIdentity

__all__ = [
    "SegmentPlan",
    "MeasurementGroupPlan",
    "OutputPlan",
    "plan_outputs",
    "OmittedOutput",
    "label_map_owner",
    "label_map_owners",
    "producing_outcome",
    "build_job_identity",
    "preprocessing_spec_digest",
    "normalise_pn",
    "ai_series_description",
    "apply_inherited_attributes",
    "apply_ai_marking",
    "guard_deid_uid_space",
    "MEDICALOS_VERSION",
    "PREPROCESSING_SPEC_VERSION",
    "DEPLOYMENT_ID",
    "INSTITUTION_NAME",
    "LEGAL_MANUFACTURER_NAME",
    "SERIES_NUMBER_BAND",
    "MODEL_ID",
]


# Deployment identity. In the real platform these are `ServiceVersion` and `Deployment`
# rows (chapter 6); CONTRACT.md section 0 removes the registries from this slice, so they
# are constants here. MOS-IMG-076 forbids writing a placeholder manufacturer, so they are
# REQUIRED inputs either way and `validate_equipment_identity()` checks them before the
# first object is built.
MEDICALOS_VERSION = "0.2.0"
PREPROCESSING_SPEC_VERSION = "0.1.0"
DEPLOYMENT_ID = "dep_medos_slice"  # MOS-IMG-078: never a hostname or an IP
INSTITUTION_NAME = "MedicalOS Weeks 1-2 Slice"
LEGAL_MANUFACTURER_NAME = "MedicalOS B.V."
SERIES_NUMBER_BAND = 9000  # MOS-IMG-072: {9000, 9010, ..., 9890}

# `derive_uid` takes a model identity (MOS-IMG-062) and CONTRACT.md section 7 is explicit
# that none of these capabilities is a learned model. Naming the rule is the honest answer:
# a UID derived from "rule.hu_threshold" claims exactly what produced it.
MODEL_ID = "rule.hu_threshold"

# `tracking` output_index base for job-level measurement groups. Segments use
# 0..N-1; 1000 keeps the two ranges disjoint so no group can ever collide with a
# segment's TrackingUID (MOS-IMG-065's output_index discipline, applied to a kind
# MOS-IMG-062's UID_KINDS table does not enumerate separately).
_GROUP_TRACKING_OFFSET = 1000


# =====================================================================================
# Lifted, unchanged: MOS-IMG-090 attribute inheritance
# =====================================================================================
_COPY_REQUIRED = [  # MOS-IMG-090 "copy": absent on the source is a defect upstream
    "PatientName",
    "PatientID",
    "StudyInstanceUID",
    "StudyDate",
    "StudyTime",
]
_COPY_OR_EMPTY = [  # MOS-IMG-090 "copy-or-empty": zero-length Type 2 when absent
    "PatientBirthDate",
    "PatientSex",
    "StudyID",
    "AccessionNumber",
    "ReferringPhysicianName",
]
_COPY_IF_PRESENT = ["IssuerOfPatientID", "BodyPartExamined", "PositionReferenceIndicator"]


def normalise_pn(name: str) -> str:
    """MOS-IMG-092: ContentCreatorName is VR PN. One component group, truncated at 64."""
    cleaned = name.replace("^", "").replace("=", "").replace("\\", "")
    return cleaned[:64]


def ai_series_description(text: str, clinical_use_mode: str) -> str:
    """MOS-IMG-133 + MOS-IMG-135 + MOS-IMG-136.

    SeriesDescription is VR LO (64). The `[AI] ` / `[AI][RUO] ` prefix is one of the three
    mandatory standard AI markings, so the truncation must eat the description, never the
    prefix.
    """
    prefix = "[AI][RUO] " if clinical_use_mode == "RESEARCH_ONLY" else "[AI] "
    room = 64 - len(prefix)
    return prefix + text[:room]


def apply_inherited_attributes(obj: Dataset, src: Dataset, *, is_seg: bool) -> list[str]:
    """MOS-IMG-090 copy / copy-or-empty, applied to a highdicom-produced object.

    StudyInstanceUID is COPIED and never minted -- MOS-IMG-069 calls minting one "the
    single most common AI-integration defect".

    NOTE what this function moves: PatientName and PatientID are copied from the source
    dataset into the derived object. That is the one place in `medos/medos/` that touches a PHI
    value, it is mandated by MOS-IMG-090, and it is a copy between two in-memory datasets
    -- the values are never logged, never put in an event payload and never stored in a
    table (CONTRACT.md section 11).
    """
    applied = []
    for name in _COPY_REQUIRED:
        value = getattr(src, name, None)
        if value is None:
            raise ValueError(
                f"source instance lacks {name}, which MOS-IMG-090 marks 'copy'; "
                "the writer must not substitute a value"
            )
        setattr(obj, name, value)
        applied.append(name)
    for name in _COPY_OR_EMPTY:
        setattr(obj, name, getattr(src, name, "") or "")
        applied.append(name)
    for name in _COPY_IF_PRESENT:
        value = getattr(src, name, None)
        if value not in (None, ""):
            setattr(obj, name, value)
            applied.append(name)
    if is_seg:
        for name in ("PatientPosition", "Laterality"):
            value = getattr(src, name, None)
            if value not in (None, ""):
                setattr(obj, name, value)
                applied.append(name)
    obj.SpecificCharacterSet = "ISO_IR 192"
    obj.TimezoneOffsetFromUTC = "+0000"  # MOS-IMG-090: objects are written in UTC
    return applied


def guard_deid_uid_space(src: Dataset, identity: JobIdentity) -> None:
    """Refuse to stamp `PatientIdentityRemoved = YES` onto copied identifiers.

    LIFTED FROM `spikes/week0/write_dicom_results.py`, WHERE IT HAD STAYED. CONTRACT.md
    section 2 says the spike's logic is lifted into the platform by moving, not
    rewriting -- and this function was not moved with the rest. The platform kept the two
    halves it guards and not the guard:

        apply_inherited_attributes  copies PatientName and PatientID verbatim
                                    (MOS-IMG-090 marks them "copy")
        apply_ai_marking            writes PatientIdentityRemoved = "YES" whenever
                                    uid_space is anything but "source" (MOS-IMG-089)

    Run together on identified source instances, those two produce an object that CLAIMS
    to be de-identified while carrying the patient's name -- worse than either honest
    alternative, because a downstream consumer that trusts the flag has been told it may
    stop being careful. `MOS-DATA-020` is why the default is safe: the platform writer
    operates in the SOURCE uid space precisely because its objects must reference the
    study as the hospital knows it, and `uid_space = "source"` writes `"NO"`.

    THE HAZARD WAS LATENT, NOT ACTIVE: nothing in this repository constructs a
    `JobIdentity` with `uid_space = "deid"` today. That is the reason this lift changes no
    behaviour and the reason it was easy to miss -- a guard for a path nobody takes looks
    like dead code until somebody takes it.

    NO `--source-is-deidentified` ESCAPE, AND THAT IS DELIBERATE. The spike is a CLI and
    offered the operator a flag for a known-de-identified-but-untagged corpus (TCIA,
    LIDC-IDRI, MIDRC -- the `corpus` class of `MOS-DATA-048`'s table). The platform is not
    a CLI and has a better place for that fact: `MOS-DATA-036` translates the UIDs before
    the writer is reached, and a corpus's de-identification status is a recorded
    provenance declaration (`medos/deploy/provenance/`), not a per-call argument. A writer
    flag that overrides a safety check is the per-job override `MOS-TRAIN-072` refuses by
    name elsewhere in this platform; it does not get to exist here either.
    """
    if identity.uid_space == "source":
        return
    if str(getattr(src, "PatientIdentityRemoved", "")).upper() == "YES":
        return
    raise SystemFailure(
        "deid_uid_space_over_identified_source",
        {
            "uid_space": identity.uid_space,
            "source_patient_identity_removed": str(
                getattr(src, "PatientIdentityRemoved", "")
            ),
            "study_instance_uid": identity.study_instance_uid,
        },
        f"refusing to write in uid_space {identity.uid_space!r}: the source instances do "
        "not carry PatientIdentityRemoved=YES, so the PatientName and PatientID copied "
        "under MOS-IMG-090 may be real identifiers and MOS-IMG-089 would label the object "
        "as de-identified anyway. Write in the source UID space (the default, "
        "MOS-DATA-020), or feed this writer from a pipeline that has already translated "
        "the UIDs and tagged the instances (MOS-DATA-036).",
    )


def apply_ai_marking(
    obj: Dataset,
    identity: JobIdentity,
    plan: OutputPlan,
    *,
    is_seg: bool,
    src: Dataset,
) -> None:
    """MOS-IMG-133/134/137: ContributingEquipmentSequence + the private block.

    `src` is REQUIRED rather than defaulted, because this function is the one that writes
    `PatientIdentityRemoved`, and a guard that a caller can omit is a guard that a caller
    will omit. Both writers already hold `source_datasets[0]` on the line above.
    """
    guard_deid_uid_space(src, identity)
    purpose = Dataset()
    purpose.CodeValue = "109102"
    purpose.CodingSchemeDesignator = "DCM"
    purpose.CodeMeaning = "Processing Equipment"

    contributing = Dataset()
    contributing.PurposeOfReferenceCodeSequence = [purpose]
    contributing.Manufacturer = "MedicalOS"
    contributing.ManufacturerModelName = "MedicalOS Control Plane"
    contributing.SoftwareVersions = identity.medicalos_version
    contributing.DeviceSerialNumber = identity.deployment_id
    contributing.ContributionDescription = (
        f"AI result generated by {identity.service_id}@{identity.service_version}"
    )
    obj.ContributingEquipmentSequence = [contributing]

    # MOS-IMG-089: source UID space means the identity was NOT removed.
    obj.PatientIdentityRemoved = "NO" if identity.uid_space == "source" else "YES"
    if identity.uid_space == "deid":
        obj.DeidentificationMethod = "MedicalOS tenant de-identification profile"

    # MOS-IMG-137/138: group 0099 is odd, therefore a valid private group.
    block = obj.private_block(0x0099, "MEDICALOS_AI_1.0", create=True)
    block.add_new(0x01, "CS", "YES")  # AIDerived
    block.add_new(0x02, "LO", identity.clinical_use_mode)  # ClinicalUseMode
    block.add_new(0x03, "LO", identity.service_id)
    block.add_new(0x04, "LO", identity.service_version)
    block.add_new(0x05, "LO", identity.job_id)
    block.add_new(0x06, "UI", plan.provenance_record_uid)
    # SPEC INCONSISTENCY carried over from the spike, still unfixed upstream: MOS-IMG-137
    # gives (0099,1007) VR LO, but the value it mandates is "sha256:" + 64 hex = 71
    # characters and LO caps at 64. VR UT here, because truncating a digest destroys the
    # only thing it is for. Chapter 4 needs to change the VR.
    block.add_new(0x07, "UT", identity.preprocessing_spec_digest)
    block.add_new(0x08, "LO", identity.medicalos_version)

    if is_seg:
        obj.LossyImageCompression = "00"  # MOS-IMG-061/090
        obj.BurnedInAnnotation = "NO"
        _pin_dimension_organization_uid(obj)


# SPEC GAP, carried over from the spike and still open: MOS-IMG-062 forbids random UID
# generation "anywhere in the DICOM writing path", but highdicom mints
# DimensionOrganizationUID (0020,9164) with pydicom's generate_uid(), and UID_KINDS has no
# member covering it. Pinned to a value derived from the SEG SOPInstanceUID -- itself
# derived -- so the object is byte-reproducible across attempts.
_DIMENSION_ORG_LABEL = "MOS-IMG-UID-v1|dimension_organization"


def _pin_dimension_organization_uid(seg: Dataset) -> None:
    derived = "2.25." + str(
        uuid.uuid5(MEDICALOS_UID_NAMESPACE, f"{_DIMENSION_ORG_LABEL}|{seg.SOPInstanceUID}").int
    )
    for item in getattr(seg, "DimensionOrganizationSequence", []) or []:
        item.DimensionOrganizationUID = derived
    for item in getattr(seg, "DimensionIndexSequence", []) or []:
        if "DimensionOrganizationUID" in item:
            item.DimensionOrganizationUID = derived


# =====================================================================================
# The output plan
# =====================================================================================
@dataclass(frozen=True)
class SegmentPlan:
    """One SEG segment and everything the SR needs to talk about it."""

    structure: str  # `capability_concepts.json` segment-profile key, e.g. "lung"
    label_value: int  # value in LabelMap.array
    segment_number: int  # 1-based DICOM SegmentNumber
    segment_global_index: int  # index into the label map's `segments`, for UID derivation
    tracking_uid: str  # MOS-IMG-117: the SAME uid appears in the SEG and the SR
    tracking_id: str
    profile: dict[str, Any]
    measurements: tuple[Measurement, ...]  # CONTRACT.md section 5 values, verbatim
    mask: np.ndarray
    n_voxels: int
    producing_capability_ids: tuple[str, ...]


@dataclass(frozen=True)
class MeasurementGroupPlan:
    """Measurements that belong to the whole result rather than to one segment.

    Rendered as a TID 1501 `MeasurementsAndQualitativeEvaluations` group with a tracking
    identifier and no `ReferencedSegment` -- see this module's docstring, rule 2.
    """

    group_key: str  # the `Finding.kind` it came from; stable across attempts
    tracking_uid: str
    tracking_id: str
    measurements: tuple[Measurement, ...]
    producing_capability_ids: tuple[str, ...]
    covers_segments: tuple[str, ...]  # the structure slugs the region is the union of


@dataclass(frozen=True)
class OmittedOutput:
    """One object the job could have carried and will not, with the reason WHY.

    MOS-SVC-011's rule is "never as silence". It is written about capabilities, but the
    same argument applies one layer down: a plan that quietly contains no SEG is
    indistinguishable, from the outside, from a plan whose SEG was lost. So every object
    the job asked for and is not getting is named here, with a `reason_code` a consumer
    can switch on, and `step_write_dicom` puts the list in `job_steps.detail`.

    `reason_code` is a closed set:

      `not_requested`      the kind is absent from `jobs.requested_outputs`
      `no_label_map`       no capability in the bundle returned one, so MOS-IMG-143's
                           `label_maps[] -> one SEG series each` maps zero to zero
      `all_segments_empty` a label map whose every segment is empty on the source grid.
                           MOS-IMG-098 omits each such segment; omitting all of them
                           leaves `SegmentSequence` empty, which is a Type 1 attribute,
                           so there is no conformant object left to write
      `no_measurements`    nothing to put in a TID 1500 measurement group. REPORTED as a
                           toolchain limit rather than a standard one -- see `plan_outputs`
      `not_implemented`    the kind is real (SC) and this build writes none
    """

    kind: str  # "SEG" | "SR" | "SC"
    reason_code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "reason_code": self.reason_code, "detail": self.detail}


@dataclass(frozen=True)
class OutputPlan:
    identity: JobIdentity
    seg_series_index: int
    seg_series_instance_uid: str
    seg_sop_instance_uid: str
    seg_series_number: int
    sr_series_index: int
    sr_series_instance_uid: str
    sr_sop_instance_uid: str
    sr_series_number: int
    device_observer_uid: str
    provenance_record_uid: str
    segments: tuple[SegmentPlan, ...]
    empty_segments: tuple[str, ...]
    extra_groups: tuple[MeasurementGroupPlan, ...] = ()
    # WHICH objects this job actually writes. Both were unconditionally true before, and
    # that is the defect: it made "this detector examined the study and found nothing" --
    # the modal outcome of any detection capability -- unrepresentable, because the only
    # way out of the writer was a SEG with at least one non-empty segment.
    #
    # The UIDs above are still derived either way. They are a pure function of the
    # identity tuple (MOS-IMG-062) and cost nothing to compute; deriving them only
    # sometimes would mean a retry of a job whose findings changed could not tell whether
    # a series UID was absent because nothing was written or because the numbering moved.
    writes_seg: bool = True
    writes_sr: bool = True
    omitted_outputs: tuple[OmittedOutput, ...] = ()

    @property
    def written_kinds(self) -> tuple[str, ...]:
        """The DICOM object kinds this plan will produce, in write order."""
        return tuple(
            k for k, on in (("SEG", self.writes_seg), ("SR", self.writes_sr)) if on
        )

    @property
    def has_findings_to_draw(self) -> bool:
        """True when some capability returned a segment with voxels in it.

        The negation is NOT "the study is normal" -- it is "there is no region to
        outline", which is a statement about the output, not about the patient
        (`MOS-UI-030b`). The clinical polarity lives in `Finding.present`, which the
        `Result` carries whatever this says.
        """
        return bool(self.segments)

    def manifest(self, frame_count: int) -> dict[str, Any]:
        """MOS-IMG-079: the ordered list of planned objects, persisted before the first
        write so a retry reconciles (MOS-IMG-080/081) instead of re-running inference.

        Only the objects that will actually be written are listed, plus `omitted`, which
        says what is missing and why. A manifest promising a SEG this job never intends
        to write would make MOS-IMG-080's reconciliation permanently unsatisfiable: the
        retry would look for a series that is absent by design and conclude the store is
        incomplete.
        """
        objects: list[dict[str, Any]] = []
        if self.writes_seg:
            objects.append(
                {
                    "kind": "seg",
                    "series_index": self.seg_series_index,
                    "series_instance_uid": self.seg_series_instance_uid,
                    "expected_sop_instance_uids": [self.seg_sop_instance_uid],
                    "segment_count": len(self.segments),
                    "frame_count": frame_count,
                }
            )
        if self.writes_sr:
            objects.append(
                {
                    "kind": "sr",
                    "series_index": self.sr_series_index,
                    "series_instance_uid": self.sr_series_instance_uid,
                    "expected_sop_instance_uids": [self.sr_sop_instance_uid],
                    "segment_count": 0,
                    "frame_count": 0,
                }
            )
        return {
            "objects": objects,
            "omitted": [o.to_dict() for o in self.omitted_outputs],
        }


def preprocessing_spec_digest(*, mask_source: str = "hu_threshold") -> str:
    """`sha256:` + 64 hex over the canonical PreprocessingSpec.

    Material in the SEG's algorithm parameters (MOS-IMG-104) and in the private block
    (MOS-IMG-137). Stands in for the real spec artifact, which chapter 6 owns and
    CONTRACT.md section 0 removes from this slice.
    """
    return "sha256:" + hashlib.sha256(
        json.dumps(
            {
                "version": PREPROCESSING_SPEC_VERSION,
                "canonical_geometry": {
                    "gantry_tilt": {"mode": "reject"},
                    "non_uniform_spacing": "reject",
                    "padding_output_value": -1024.0,
                },
                "mask_source": mask_source,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_job_identity(
    *,
    job_id: str,
    tenant_id: str,
    service_id: str,
    service_version: str,
    study_instance_uid: str,
    capability_ids: Sequence[str],
    requested_outputs: Sequence[str],
    clinical_use_mode: str,
    model_version: str,
    expected_idempotency_key: str | None = None,
    uid_space: str = "source",
    org_root: str | None = None,
    mask_source: str = "hu_threshold",
) -> JobIdentity:
    """Resolve the identity tuple once per job, and PROVE it matches the stored key.

    `expected_idempotency_key` is `jobs.idempotency_key`. When it is supplied, the
    identity's own derivation MUST reproduce it, and this function raises if it does not.

    That check is the whole reason this constructor exists rather than a literal
    `JobIdentity(...)` at the call site. `derive_uid` seeds every generated
    SeriesInstanceUID and SOPInstanceUID from the idempotency key (MOS-IMG-062), and
    MOS-IMG-085 makes "same UID implies same declared inputs" an invariant the retry path
    depends on. If the worker's identity tuple ever drifts from the control plane's --
    a different `service_version` default, a stray parameter, a reordered capability list
    -- then attempt 2 of a crashed job writes a DIFFERENT SEG into the PACS instead of
    reconciling with the one attempt 1 already stored. Failing loudly here costs one job;
    not checking costs a duplicated series per crash, forever, and platform policy denies
    deleting it (MOS-EXEC-061).

    `selected_series_uids=()` and `parameters={"capability_ids": ...}` mirror
    `medos.db.repo.JobSpec.idempotency_key()` exactly -- see its docstring for why
    selection is not in the key in this slice.
    """
    # `requested_outputs` is a CLOSED, CASE-SENSITIVE vocabulary and this is the first
    # place that can say so. Chapter 5 section 5.6.2 spells the values `{SEG, SR, SC}`
    # "exactly as spelled in the payload schemas of section 5.14.1", `MOS-EXEC-053` puts
    # them in the idempotency-key material, and `schema.sql` renders them as
    # `CHECK (requested_outputs <@ ARRAY['SEG','SR','SC'])`.
    #
    # Checked here rather than shrugged off, because the two consequences of a member
    # outside the set are both silent. `sorted(requested_outputs)` goes into the key, so
    # `["seg"]` derives a DIFFERENT idempotency key -- and therefore a different
    # SeriesInstanceUID -- from `["SEG"]` for the same job; and `plan_outputs` reads the
    # tuple to decide which objects to write, so a misspelled member now yields a job
    # that writes nothing at all. Before `plan_outputs` honoured the field, the second
    # consequence did not exist and a misspelling was invisible.
    bad = [o for o in requested_outputs if o not in ("SEG", "SR", "SC")]
    if bad or not requested_outputs:
        raise SystemFailure(
            "requested_outputs_invalid",
            {"requested_outputs": list(requested_outputs), "unknown": bad},
            f"requested_outputs {list(requested_outputs)!r} is not a non-empty subset of "
            "{SEG, SR, SC} as chapter 5 section 5.6.2 spells them. The value is in the "
            "MOS-EXEC-053 idempotency-key material and decides which DICOM objects this "
            "job writes; a member outside the set silently forks the UID space and can "
            "produce a job that writes no object at all.",
        )
    identity = JobIdentity(
        tenant_id=tenant_id,
        service_id=service_id,
        service_version=service_version,
        model_id=MODEL_ID,
        model_version=model_version,
        deployment_id=DEPLOYMENT_ID,
        institution_name=INSTITUTION_NAME,
        legal_manufacturer_name=LEGAL_MANUFACTURER_NAME,
        medicalos_version=MEDICALOS_VERSION,
        preprocessing_spec_version=PREPROCESSING_SPEC_VERSION,
        preprocessing_spec_digest=preprocessing_spec_digest(mask_source=mask_source),
        study_instance_uid=study_instance_uid,
        selected_series_uids=(),
        requested_outputs=tuple(requested_outputs),
        parameters={"capability_ids": sorted(capability_ids)},
        uid_space=uid_space,
        org_root=org_root,
        series_number_band=SERIES_NUMBER_BAND,
        clinical_use_mode=clinical_use_mode,
        job_id=job_id,
    )
    if (
        expected_idempotency_key is not None
        and identity.idempotency_key != expected_idempotency_key
    ):
        raise SystemFailure(
            "idempotency_key_drift",
            {
                "job_id": job_id,
                "stored": expected_idempotency_key,
                "derived": identity.idempotency_key,
            },
            "the worker's identity tuple does not reproduce jobs.idempotency_key; every "
            "UID this job would derive is therefore different from the one a previous "
            "attempt derived (MOS-IMG-062, MOS-IMG-085). Refusing to write.",
        )
    return identity


def label_map_owner(bundle: ResultBundle) -> CapabilityOutcome | None:
    """The ONE outcome in this bundle that carries the label map a SEG is written from,
    or `None` when no capability returned one.

    Named and exported because two callers need the same answer and must not disagree
    about it. `plan_outputs` needs the SEGMENTS; `worker/steps.py` needs the identity of
    the model that PRODUCED them, to fill `JobIdentity.model_version`.

    Why that has to be per-result rather than per-job: a job may run several capabilities
    (CONTRACT.md section 7 -- `emphysema_laa` runs after `lung_segmentation`), so "the
    job's model version" is not a well-defined quantity in general. "The version that
    produced the label map being written" is, because `plan_outputs` refuses a bundle with
    two label maps, so there is exactly one such version per set of written objects.
    MOS-IMG-062 puts `model_version` into the UID seed and MOS-IMG-085 turns that into the
    invariant the retry path depends on -- "same UID implies same declared inputs" -- so a
    version that does not move when the producing method moves breaks it. The same string
    is recorded in `dicom_objects.derivation_inputs` (MOS-STORE-286), and the provenance
    record already answers this question per-OUTCOME
    (`execution.models[] = [{model_id: capability_id, version: capability.version}]`,
    MOS-SAFE-083 section D / MOS-SVC-098): a global answer here made one job's two
    provenance records contradict each other.

    AMBIGUITY is still a `SystemFailure` and has no fallback: a SEG written with the wrong
    producing version is a provenance record that is silently false, which is strictly
    worse than a job that stops.

    ABSENCE is not. `None` means no capability returned a label map, which is the honest
    output of any detector that found nothing, and it is the caller's job to decide what
    that means -- see `producing_outcome` and `plan_outputs`.
    """
    owners = label_map_owners(bundle)
    if not owners:
        return None
    return owners[0]


def label_map_owners(bundle: ResultBundle) -> tuple[CapabilityOutcome, ...]:
    """Every outcome carrying a label map; raises unless there is at most one.

    The composition check is here, at the single place both callers pass through, rather
    than duplicated in each. Two label maps in one bundle is a `SystemFailure` because
    MOS-IMG-066 needs a total ordering across the combined segment set and CONTRACT.md
    section 5 does not define how two label maps compose. Zero is not a failure of any
    kind and this function says so by returning an empty tuple.
    """
    owners = tuple(o for o in bundle.outcomes if o.label_map is not None)
    if len(owners) > 1:
        raise SystemFailure(
            "multiple_label_maps_in_bundle",
            {"capability_ids": [o.capability_id for o in owners]},
            "more than one capability returned a LabelMap. MOS-IMG-066 needs a total "
            "ordering across the combined segment set and CONTRACT.md section 5 does not "
            "define how two label maps compose; refusing to invent one.",
        )
    return owners


def producing_outcome(bundle: ResultBundle) -> CapabilityOutcome:
    """The outcome whose capability version stamps this job's derived objects.

    The label map's owner when there is one -- that is the object with a producing model
    in the ordinary sense. When there is none, the job still writes an SR, the SR still
    carries `SoftwareVersions` and a `DeviceObserver`, and MOS-IMG-062 still needs a
    `model_version` in the UID seed, so "there is no label map" cannot be allowed to mean
    "there is no answer".

    The answer is then the FIRST outcome in bundle order. Bundle order is
    `worker/steps.py`'s resolved dependency order (CONTRACT.md section 7), which is
    deterministic for a given capability set, so the derived UIDs are reproducible across
    attempts -- which is the only property MOS-IMG-085 actually requires of this choice.

    REPORTED, because the rule is under-determined rather than wrong: an SR is job-level
    and may carry measurements from several capabilities, so "the version that produced
    this SR" is not a well-defined quantity for a multi-capability bundle. Chapter 4's
    MOS-IMG-062 takes a single `model_version` per job and chapter 9's MOS-SAFE-083
    section D records `execution.models[]` as a LIST per result; the two do not compose,
    and no chapter says which model version a multi-capability SR should name. It is
    invisible today because every multi-capability job in this build has exactly one label
    map, so this branch is reached only by single-capability bundles.
    """
    owner = label_map_owner(bundle)
    if owner is not None:
        return owner
    if not bundle.outcomes:
        raise SystemFailure(
            "empty_bundle",
            {},
            "the bundle carries no outcomes at all, so no capability ran and there is "
            "nothing to attribute the written objects to",
        )
    return bundle.outcomes[0]


def plan_outputs(
    identity: JobIdentity,
    bundle: ResultBundle,
    concepts: ConceptDictionary,
) -> OutputPlan:
    """Allocate identity for every object and segment, deterministically.

    MOS-IMG-066 requires a total, stable ordering rule. Here it is the order of
    `LabelMap.segments`, which CONTRACT.md section 5 binds to the label VALUES in the
    array (`index i+1 == segments[i]`) -- so the ordering rule is a property of the data
    the capability returned, not of any iteration order in this function.

    MOS-IMG-098: a segment with no voxels on the source grid is OMITTED and recorded,
    never written as a segment with no frames.

    TOTAL over every bundle, and that is the fix
    --------------------------------------------
    This function used to raise `SystemFailure` twice: `no_label_map_in_bundle` when no
    capability returned a label map, and `all_segments_empty` when every segment it did
    return was empty. Those are the same clinical event seen from the writer -- a
    capability ran and there is nothing to outline -- and for any DETECTION capability
    that is the modal outcome. A chest CT with no nodules on it is the common case, not a
    malfunction. CONTRACT.md section 5 already typed the field `label_map: LabelMap |
    None`, so `None` was always a legal return; the writer was refusing a value the
    binding return contract permits.

    Chapter 5 is unambiguous about what the old behaviour claimed. `MOS-EXEC-014`:
    `FAILED` means "this study should have been analysed and the platform could not do
    it". `MOS-EXEC-016a` surfaces it as `class: "system_failure"`, `MOS-EXEC-015` lets it
    consume retry budget and page someone, and `MOS-UI-033` renders it with a trace id for
    a support ticket. None of that is true of a negative read, and the harm is the one
    `MOS-EXEC-014` names for the `REJECTED`/`FAILED` confusion one level up: a radiologist
    who sees a red error either chases IT or assumes the study was cleared.

    So the writer now answers the question it was actually asked -- "which objects does
    this job write" -- for every bundle including the empty one, and records WHY whenever
    the answer is smaller than `requested_outputs` (`OmittedOutput`, never silence:
    `MOS-SVC-011`). Three judgements it does NOT make, each owned elsewhere:

      * whether the STUDY is negative. That is `Finding.present` (`MOS-SVC-089`/`090`),
        which the capability sets and the `Result` carries. Nothing here reads it, and an
        empty `segments` tuple is a statement about the OUTPUT, not about the patient.
      * whether the capability MALFUNCTIONED by returning nothing. Deciding that needs the
        capability's declared `output_kinds`, which this module does not have and must not
        guess. `worker/steps.py::step_validate_bundle` owns it and still fails the job.
      * whether the study should have been analysed at all. That is the envelope gate,
        upstream, and its answer is `REJECTED`.

    `requested_outputs` is honoured here too (`MOS-EXEC-023`, chapter 5 section 5.6.2:
    the column "drives the step plan's write/store steps"). A job asking for `{SR}` gets
    no SEG and does not fail for want of one.
    """
    identity.validate_equipment_identity()

    owner = label_map_owner(bundle)
    label_map = None if owner is None else owner.label_map

    slug_by_code = _structure_slug_index(concepts)

    segments: list[SegmentPlan] = []
    empty: list[str] = []
    segment_number = 0
    for global_index, concept in enumerate(() if label_map is None else label_map.segments):
        structure = slug_by_code.get((concept.scheme, concept.code))
        if structure is None:
            raise SystemFailure(
                "unmapped_segment_concept",
                {"scheme": concept.scheme, "code": concept.code},
                f"segment concept {concept.scheme}:{concept.code} has no segment profile "
                "in capability_concepts.json; MOS-IMG-112 forbids inventing one",
            )
        assert label_map is not None  # the loop is empty when it is None
        mask = label_map.array == np.uint8(global_index + 1)
        n_voxels = int(np.count_nonzero(mask))
        if n_voxels == 0:
            empty.append(structure)  # MOS-IMG-098
            continue
        segment_number += 1
        segments.append(
            SegmentPlan(
                structure=structure,
                label_value=global_index + 1,
                segment_number=segment_number,
                segment_global_index=global_index,
                tracking_uid=identity.uid("tracking", global_index),
                tracking_id=f"medicalos:{identity.job_id}:seg:{global_index}",
                profile=concepts.profile(structure),
                measurements=(),
                mask=mask,
                n_voxels=n_voxels,
                producing_capability_ids=(),
            )
        )
    segments, extra_groups = _attach_measurements(segments, bundle, identity)

    requested = set(identity.requested_outputs)
    omitted: list[OmittedOutput] = []

    # --- SEG -------------------------------------------------------------------------
    # MOS-IMG-143 maps `label_maps[] -> one SEG series each`, so zero label maps is zero
    # SEG series by the same total mapping that makes one label map one series. There is
    # no separate rule to appeal to and none is needed.
    writes_seg = "SEG" in requested and bool(segments)
    if "SEG" not in requested:
        omitted.append(
            OmittedOutput(
                "SEG",
                "not_requested",
                "SEG is absent from the job's requested_outputs",
            )
        )
    elif label_map is None:
        omitted.append(
            OmittedOutput(
                "SEG",
                "no_label_map",
                "no capability in this bundle returned a LabelMap, so there is no region "
                "to outline. This is a statement about the output, not about the study: "
                "the clinical polarity is Finding.present on the Result (MOS-SVC-090).",
            )
        )
    elif not segments:
        omitted.append(
            OmittedOutput(
                "SEG",
                "all_segments_empty",
                "every segment of the returned LabelMap is empty on the source grid. "
                f"MOS-IMG-098 omits each one and records it ({', '.join(empty)}); "
                "omitting all of them leaves SegmentSequence empty, which is Type 1, so "
                "no conformant SEG remains to be written.",
            )
        )

    # --- SR --------------------------------------------------------------------------
    # Written whenever it was asked for AND there is at least one measurement group to
    # put in it. A negative result with a NUMBER in it -- "candidate count: 0", "nodule
    # burden: 0 ml" -- is one group and gets an SR that says so in the PACS, which is the
    # object chapter 18's HZ-13 needs to exist ("absence is indistinguishable from
    # negative"). A result with no number at all has no report to write.
    #
    # THAT SECOND HALF IS FORCED BY THE TOOLCHAIN, NOT CHOSEN, and it is REPORTED below
    # and in this module's docstring. TID 1500's Imaging Measurements container is
    # conditional -- MOS-IMG-109's table marks TID 1411 "MUST for segment-derived
    # measurements" and TID 1501 "MUST for study-level measurements", neither
    # unconditional -- so a Measurement Report carrying only the four MUST rows (language,
    # device observation context, procedure reported, Image Library) is a conformant
    # document, and it would be a good one: it says "these instances were examined". But
    # highdicom raises `TypeError: Argument 'imaging_measurements' is required` and its
    # own comment calls that a limitation of the library rather than of the standard,
    # while MOS-IMG-094 forbids assembling the object any other way ("constructing a
    # generated SEG, SR or SC by assembling a pydicom.Dataset element by element ... is
    # forbidden") and MOS-IMG-095 limits post-processing to three named additions. So the
    # object cannot be built inside the imaging contract, and the honest thing is to omit
    # it with a reason rather than to breach MOS-IMG-094 quietly.
    has_sr_content = bool(segments) or bool(extra_groups)
    writes_sr = "SR" in requested and has_sr_content
    if "SR" not in requested:
        omitted.append(
            OmittedOutput(
                "SR", "not_requested", "SR is absent from the job's requested_outputs"
            )
        )
    elif not has_sr_content:
        omitted.append(
            OmittedOutput(
                "SR",
                "no_measurements",
                "no capability returned a measurement or a segment, so there is no TID "
                "1500 measurement group to report. The Result still carries this job's "
                "findings; what has no DICOM carrier is a negative with no number in it. "
                "A capability that wants its negative legible in the PACS reports the "
                "number -- a count of zero, a burden of 0 ml (MOS-SVC-090, MOS-IMG-110).",
            )
        )

    # --- SC --------------------------------------------------------------------------
    # A legal member of `jobs.requested_outputs` (chapter 5 section 5.6.2) that this build
    # writes none of. Recorded rather than ignored, for the same reason as the rest: a
    # requested output that simply never appears is indistinguishable from one that was
    # lost. MOS-IMG-127 makes an SC a display artefact only, so its absence removes no
    # clinical content.
    if "SC" in requested:
        omitted.append(
            OmittedOutput(
                "SC",
                "not_implemented",
                "this build writes no Secondary Capture; MOS-IMG-127 makes SC a display "
                "artefact that may never be the sole carrier of a result, so nothing "
                "clinical is missing from the objects that were written",
            )
        )

    return OutputPlan(
        identity=identity,
        seg_series_index=0,
        seg_series_instance_uid=identity.uid("seg.series", 0),
        # MOS-IMG-065: seg.instance output_index = series_index * 1000 + 0
        seg_sop_instance_uid=identity.uid("seg.instance", 0),
        seg_series_number=identity.series_number("seg", 0),
        sr_series_index=0,
        sr_series_instance_uid=identity.uid("sr.series", 0),
        # MOS-IMG-065: sr.instance output_index = series_index * 1000 + review_round(=0)
        sr_sop_instance_uid=identity.uid("sr.instance", 0),
        sr_series_number=identity.series_number("sr", 0),
        device_observer_uid=identity.uid("device_observer", 0),  # MOS-IMG-124/125
        provenance_record_uid=identity.uid("tracking", 0),  # MOS-IMG-137 (0099,1006)
        segments=tuple(segments),
        empty_segments=tuple(empty),
        extra_groups=tuple(extra_groups),
        writes_seg=writes_seg,
        writes_sr=writes_sr,
        omitted_outputs=tuple(omitted),
    )


def _structure_slug_index(concepts: ConceptDictionary) -> dict[tuple[str, str], str]:
    """`(scheme, code)` of a profile's segmented-property TYPE -> the profile slug.

    Built from the dictionary rather than hard-coded, because MOS-IMG-113 forbids
    materialising the clinical code list in code as a table or a constant map.
    """
    index: dict[tuple[str, str], str] = {}
    for slug, profile in concepts.segment_profiles.items():
        if slug.startswith("_"):
            continue
        row = concepts[profile["type"]]
        index[(str(row["coding_scheme"]), str(row["code_value"]))] = slug
    return index


def _attach_measurements(
    segments: list[SegmentPlan], bundle: ResultBundle, identity: JobIdentity
) -> tuple[list[SegmentPlan], list[MeasurementGroupPlan]]:
    """Distribute every bundle measurement over the segments, or over a job-level group.

    The rule is in this module's docstring. Nothing is dropped and nothing is duplicated:
    every measurement in the bundle lands in exactly one group, which is what makes
    "the SR carries the same numbers as `result_measurements`" checkable by counting.
    """
    from dataclasses import replace as _replace

    by_slug: dict[str, list[Measurement]] = {
        s.structure: list(s.measurements) for s in segments
    }
    caps: dict[str, list[str]] = {s.structure: [] for s in segments}
    spare: dict[str, list[Measurement]] = {}
    spare_caps: dict[str, list[str]] = {}

    for outcome in bundle.outcomes:
        for finding in outcome.findings:
            if not finding.measurements:
                continue
            if finding.kind in by_slug:
                by_slug[finding.kind].extend(finding.measurements)
                if outcome.capability_id not in caps[finding.kind]:
                    caps[finding.kind].append(outcome.capability_id)
            elif len(segments) == 1:
                target = segments[0].structure
                by_slug[target].extend(finding.measurements)
                if outcome.capability_id not in caps[target]:
                    caps[target].append(outcome.capability_id)
            else:
                spare.setdefault(finding.kind, []).extend(finding.measurements)
                bucket = spare_caps.setdefault(finding.kind, [])
                if outcome.capability_id not in bucket:
                    bucket.append(outcome.capability_id)

    placed = [
        _replace(
            s,
            measurements=tuple(by_slug[s.structure]),
            producing_capability_ids=tuple(caps[s.structure]),
        )
        for s in segments
    ]

    groups: list[MeasurementGroupPlan] = []
    # `sorted` and an index-derived tracking UID: MOS-IMG-062 forbids a random UID
    # anywhere on the writing path, and MOS-IMG-117 needs the identifier stable across
    # attempts. The offset keeps these UIDs out of the per-segment `tracking` range.
    for index, kind in enumerate(sorted(spare)):
        output_index = _GROUP_TRACKING_OFFSET + index
        groups.append(
            MeasurementGroupPlan(
                group_key=kind,
                tracking_uid=identity.uid("tracking", output_index),
                tracking_id=f"medicalos:{identity.job_id}:group:{index}",
                measurements=tuple(spare[kind]),
                producing_capability_ids=tuple(spare_caps[kind]),
                covers_segments=tuple(s.structure for s in segments),
            )
        )
    return placed, groups
