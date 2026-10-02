#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Turn a Slicer NRRD image/segmentation pair into a DICOM CT series and a DICOM SEG,
so that data which arrived as files can enter this platform the same way data from a
scanner does -- through the gateway, as studies -- rather than beside it.

WHY THIS LIVES IN `medos/tools/` AND NOT IN `medos/medos/`
----------------------------------------------
Because it mints `StudyInstanceUID`, and `MOS-IMG-069` forbids the platform to do that.
`medos/medos/core/uids.py::derive_uid` has no `StudyInstanceUID` parameter, refuses to grow
one, and CI asserts the name never appears in the same expression as that tag. The rule
is right: within the platform every object is *derived from* an acquisition, so a study
UID is something the platform receives and never something it invents. Inventing one
would let a derived object masquerade as the acquisition it came from.

This tool is on the other side of that boundary. It is not deriving an object from an
acquisition; it is reconstructing the acquisition's DICOM form from what survived of it.
That is an ingest concern, it happens before the platform sees anything, and so it may
mint a study UID -- but it must not do so using the platform's own machinery, because
then `derive_uid`'s guarantee ("same UID implies same declared inputs") would be
extended to cover something it was never designed to say. Hence: separate root, separate
namespace, separate file, no import from `medos.core.uids`.

WHAT THE SOURCE DATA IS, MEASURED RATHER THAN ASSUMED
-----------------------------------------------------
For `NRRD_DATASET_LUNG_CANCER` (100 cases, 200 files) every header field present across
the whole corpus is one of: dimension, encoding, endian, kinds, sizes, space, space
directions, space origin, type, and the `Segment*_`/`Segmentation_` family. There is no
patient name, no id, no accession, no date, no institution, no UID -- Slicer's NRRD
export carries none of them and nothing added them back. `medos/tools/deid/scrub_slicer_scenes.py`
scans all 200 files and reports 0 findings and 0 residual PHI, and that zero was checked
against an independent enumeration of the header keys rather than trusted.

So the surrogates this tool writes are not *replacing* identifiers. There are none to
replace. `PatientID` and `PatientName` are minted from the case index because DICOM
requires the tags to exist, and a minted value is the honest thing to put in a slot whose
true value was discarded by somebody else before the data reached us.

THE CONSEQUENCE THAT MATTERS, AND IT IS NOT A GOOD ONE
------------------------------------------------------
Because the original UIDs are gone, THE UIDS THIS TOOL WRITES ARE NOT THE UIDS OF THE
ACQUISITION. They are stable (same input, same UID, forever -- see `_uid`) and they are
unique, but they cannot be used to find this study in the clinic's PACS, and two exports
of the same patient made on different days will land here as two unrelated patients. Any
`MOS-EVID-034` leakage check over this corpus is therefore blind to the one duplication
it most wants to catch: the same person appearing twice. That is recorded as a limitation
of the corpus, not papered over -- see the manifest's `known_limitations`.

WHAT IT REFUSES TO DO
---------------------
It writes no `deid_policy_version` and makes no de-identification claim in the DICOM. The
claim about this corpus lives in one place -- the provenance manifest this tool emits
beside the images -- because a claim repeated in two places is a claim that will drift,
and this session has eight recorded instances of exactly that.

Spec: MOS-IMG-069 (why this is not in medos/medos/), MOS-EVID-018 (pixel identity),
MOS-EVID-021 (what a seal will later ask of this corpus). Register entry 93.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import numpy as np
except ImportError:  # pragma: no cover - import guard
    sys.exit("numpy is required.\n  pip install numpy")

try:
    import nrrd
except ImportError:  # pragma: no cover - import guard
    sys.exit("pynrrd is required.\n  pip install pynrrd")

try:
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import (
        CTImageStorage,
        ExplicitVRLittleEndian,
        SegmentationStorage,
    )
except ImportError:  # pragma: no cover - import guard
    sys.exit("pydicom is required.\n  pip install pydicom")


TOOL = "medicalos-nrrd-to-dicom"
VERSION = "1.0.0"

#: The arc every UID this tool mints is placed under.
#:
#: `2.25.` is the ISO/ITU arc for OIDs derived from a UUID. Anyone may use it without
#: registering, and that is the correct choice for an organisation that has no registered
#: root -- inventing a sub-arc of somebody else's registered root (pydicom's
#: 1.2.826.0.1.3680043.8.498, TCIA's 1.3.6.1.4.1.14519) would be claiming an identity that
#: is not ours.
#:
#: IT IS ALSO WHY THE ARCHIVE GUARD CANNOT TELL THESE STUDIES APART BY PREFIX. `2.25.` is
#: already a covered root in `tests/integration/test_deid_provenance_declaration.py`,
#: covering this platform's own derived SEG and SR objects. A clinic study minted here
#: would slip under that same prefix and inherit a `public_deidentified` / `tcia:*`
#: declaration that is false about it, and the guard would stay green. That hole is why
#: this tool emits a corpus manifest and why the guard was extended to read it: coverage
#: became a recorded fact about a named corpus instead of an inference from a prefix.
UID_ROOT = "2.25."

#: Fixed for the life of the corpus. Changing it re-identifies every object ever written
#: by this tool, exactly as MEDICALOS_UID_NAMESPACE would in the platform -- so it is a
#: constant here too, and never a flag.
INGEST_NAMESPACE = uuid.UUID("6f1b9c84-2a77-4e51-b0d9-3c8e5a1f7204")

IMPLEMENTATION_CLASS_UID = UID_ROOT + str(
    uuid.uuid5(INGEST_NAMESPACE, "implementation-class").int
)[:24]


def _uid(*parts: str) -> str:
    """A stable UID for a named thing.

    UUIDv5 over the tool's namespace, rendered as a decimal integer under `2.25.` per
    ITU-T X.667 §6.4.2. Deterministic: the same corpus converted twice yields byte-identical
    UIDs, which is what makes re-running this tool idempotent at the PACS rather than a
    source of duplicate studies.
    """
    value = UID_ROOT + str(uuid.uuid5(INGEST_NAMESPACE, "|".join(parts)).int)
    if len(value) > 64:  # DICOM UI is max 64 bytes
        value = value[:64]
    return value


def _surrogate_patient_id(corpus: str, case: str) -> str:
    """A patient id minted from the case index.

    NOT a pseudonym. A pseudonym is a reversible substitution for a real identifier held
    under a key; there is no real identifier here to substitute, so calling this a
    pseudonym would overstate what is known. It is a label for a case whose subject is
    unknown to this system and will stay unknown.
    """
    digest = hashlib.sha256(f"{corpus}|{case}".encode()).hexdigest()[:10].upper()
    return f"MOSI-{digest}"


@dataclass
class CaseResult:
    case: str
    study_instance_uid: str
    ct_series_uid: str
    seg_series_uid: str
    patient_id: str
    slices: int
    rows: int
    columns: int
    segments: list[dict[str, Any]]
    pixel_sha256: str
    files: int


@dataclass
class Corpus:
    """Everything the manifest needs to say about where a corpus came from."""

    corpus_id: str
    source_root: str
    deidentification_status: str
    deid_policy_id: str
    uid_mapping_table_id: str
    legal_basis: str
    legal_basis_holder: str
    known_limitations: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------------------


def _geometry(header: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(direction columns, voxel spacing, origin) in LPS.

    NRRD's `space directions` rows are the world-space vector of one step along each
    array axis, so their norms are the spacing and their unit vectors are the direction.
    `left-posterior-superior` is DICOM's own patient coordinate system, so no flip is
    applied; a corpus in RAS would need one, and this function refuses rather than
    silently mirroring the patient.
    """
    space = str(header.get("space", "")).strip().lower().replace("-", " ")
    if space not in ("left posterior superior", "lps"):
        raise ValueError(
            f"space is {header.get('space')!r}, not left-posterior-superior. Converting "
            f"it would mean mirroring the patient, and a left/right mirror in a lung "
            f"study is not a defect that announces itself. Refusing."
        )
    directions = np.asarray(header["space directions"], dtype=float)
    if directions.shape != (3, 3):
        raise ValueError(f"expected 3x3 space directions, got {directions.shape}")
    spacing = np.linalg.norm(directions, axis=1)
    if not np.all(spacing > 0):
        raise ValueError("a space direction has zero length")
    unit = directions / spacing[:, None]
    origin = np.asarray(header["space origin"], dtype=float)
    return unit, spacing, origin


# --------------------------------------------------------------------------------------
# segmentation header
# --------------------------------------------------------------------------------------


#: Slicer's auto-generated segment name. A segment called `Segment_1` has not been given
#: a name; it has been left at the default, which is the absence of a name rather than a
#: short one. 418 of the 1488 segments in this collection are in that state.
SLICER_DEFAULT_NAME = re.compile(r"^Segment_\d+$")

#: The terminology entry Slicer writes when nobody chose one. It is not a meaning.
SLICER_DEFAULT_TERMINOLOGY = "85756007"  # SCT Tissue

#: What a segment is called when its source did not name it. Deliberately not a word:
#: `UNNAMED-1` cannot be mistaken for a class in a label vocabulary, cannot be matched to
#: `neo` or `hydrothorax` by a normaliser, and shows up as itself in any downstream report.
#: The alternative -- naming it after the folder it came from -- is the one thing this
#: converter must not do, because that inference would then be indistinguishable from a
#: name the annotator actually typed.
UNNAMED_PREFIX = "UNNAMED-"


def _terminology(tags: str) -> tuple[str, str]:
    """(code, meaning) from a Slicer `TerminologyEntry`, or ('', '') if there is none.

    The entry is `~`-delimited and the segment's own type sits in the third field. Where
    it is present and not Slicer's default it is better evidence of meaning than the name,
    because it was chosen from a coded list -- but only better, not good: this collection
    contains chest segmentations coded `Body fat` and `Capillary`, which are picks from a
    list rather than statements about the anatomy. So it is RECORDED and never acted on.
    """
    match = re.search(r"TerminologyEntry:([^|]*)", tags or "")
    if not match:
        return "", ""
    fields = match.group(1).split("~")
    if len(fields) < 3:
        return "", ""
    parts = fields[2].split("^")
    if len(parts) < 3:
        return "", ""
    return parts[1].strip(), parts[2].strip()


def raw_segment_names(path: Path) -> dict[int, str]:
    """`Segment<N>_Name` exactly as the file's bytes spell it, before any reader touches it.

    `nrrd.read_header` strips whitespace around every value, so one case in this collection
    whose name is literally `' neo'` comes back as `'neo'`. That is a fold, performed by a
    library, with no declaration behind it -- and it is upstream of every check designed to
    catch exactly that. Reading the header text directly is the only place the distinction
    still exists.

    The NRRD header is ASCII lines terminated by the first blank line (NRRD0005 §4), so this
    needs no parser and cannot disagree with pynrrd about where the header ends.
    """
    with open(path, "rb") as handle:
        blob = handle.read(300_000)
    head = blob.split(b"\n\n", 1)[0].decode("utf-8", "replace")
    out: dict[int, str] = {}
    for line in head.split("\n"):
        match = re.match(r"^Segment(\d+)_Name:=(.*)$", line.rstrip("\r"))
        if match:
            out[int(match.group(1))] = match.group(2)
    return out


def _segments(
    header: dict[str, Any], raw_names: dict[int, str] | None = None
) -> list[dict[str, Any]]:
    """The segments a Slicer .seg.nrrd declares, in label order, with what is known about
    how far each one can be trusted.

    Read from the header rather than from the voxels: a segment that is declared and
    empty in one case must still occupy the same label number as in every other case, or
    a model trained across the corpus learns a different meaning for channel 2 depending
    on which case it is looking at.

    THREE TRUST SIGNALS ARE CARRIED FORWARD, and the reason each is carried rather than
    acted on is that measuring them across this collection showed each one lying:

      `named`        False when the name matches Slicer's `Segment_N` default. This is
                     the RELIABLE signal.
      `name_auto_generated_flag`
                     Slicer's own `NameAutoGenerated` field. UNRELIABLE, and measured to
                     be so: 10 segments in NRRD_DATASET_LUNG_CANCER and all 99 `pn`
                     segments in `lungs` carry the flag set while having a name the
                     annotator clearly typed. Slicer does not always clear it on rename.
                     Recorded because it is evidence; never used to decide, because it is
                     wrong about 109 segments in this collection alone.
      `status`       Slicer's `Segmentation.Status`. 1169 of 1488 segments here say
                     `inprogress` and 2 say `completed`, which almost certainly reflects
                     that Slicer sets `inprogress` on first edit and annotators rarely
                     close it -- so it is weak evidence of incompleteness rather than
                     strong. Weak evidence is still evidence and still gets recorded.

    None of the three is allowed to change what is written. They travel to the manifest so
    that whoever later assigns a meaning to `UNNAMED-1` does it knowing what the file did
    and did not say.
    """
    out: list[dict[str, Any]] = []
    index = 0
    while f"Segment{index}_LabelValue" in header:
        parsed_name = str(header.get(f"Segment{index}_Name", "")).strip()
        # The literal, before pynrrd stripped it. Falls back to the parsed value only when
        # the byte-level read found nothing, which would mean the header is not shaped the
        # way NRRD0005 describes.
        raw_name = (raw_names or {}).get(index, parsed_name)
        tags = str(header.get(f"Segment{index}_Tags", ""))
        code, meaning = _terminology(tags)
        status = re.search(r"Segmentation\.Status:([^|]*)", tags)
        named = bool(parsed_name) and not SLICER_DEFAULT_NAME.match(parsed_name)
        colour = str(header.get(f"Segment{index}_Color", "0.5 0.5 0.5")).split()
        label_value = int(header[f"Segment{index}_LabelValue"])
        out.append(
            {
                "label_value": label_value,
                # What goes in the DICOM SegmentLabel. DICOM LO does not preserve
                # leading or trailing spaces, so the parsed form is the honest one here.
                "name": parsed_name if named else f"{UNNAMED_PREFIX}{label_value}",
                "source_name": parsed_name,
                # What a channel declaration must be written against. THE LITERAL.
                "source_name_raw": raw_name,
                "source_name_was_stripped_by_the_reader": raw_name != parsed_name,
                "named": named,
                "name_auto_generated_flag": str(
                    header.get(f"Segment{index}_NameAutoGenerated", "0")
                ).strip() == "1",
                "status": (status.group(1).strip() if status else ""),
                "terminology_code": code,
                "terminology_meaning": meaning,
                "terminology_is_slicer_default": code == SLICER_DEFAULT_TERMINOLOGY,
                "layer": int(header.get(f"Segment{index}_Layer", 0)),
                "color": [float(c) for c in colour[:3]] if len(colour) >= 3 else [0.5, 0.5, 0.5],
            }
        )
        index += 1
    out.sort(key=lambda s: s["label_value"])
    return out


# --------------------------------------------------------------------------------------
# writers
# --------------------------------------------------------------------------------------


def _file_meta(sop_class_uid: str, sop_instance_uid: str) -> FileMetaDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class_uid
    meta.MediaStorageSOPInstanceUID = sop_instance_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = IMPLEMENTATION_CLASS_UID
    meta.ImplementationVersionName = f"MOSINGEST{VERSION.replace('.', '')}"[:16]
    return meta


def _identity(ds: Dataset, *, patient_id: str, corpus: Corpus) -> None:
    """The identity tags, and the reason each holds what it holds.

    `PatientName` is set equal to `PatientID` rather than left empty: an empty PN makes
    several viewers fall back to the file name, which would put the case index on screen
    in a place nobody audited. `PatientBirthDate` and `PatientSex` are written EMPTY --
    type 2, present and zero-length -- because the tags are required and the values are
    genuinely unknown. Writing a plausible-looking birth date would be fabricating a
    clinical fact, which is worse than an empty tag in every direction that matters.
    """
    ds.PatientID = patient_id
    ds.PatientName = patient_id
    ds.PatientBirthDate = ""
    ds.PatientSex = ""
    ds.IssuerOfPatientID = corpus.corpus_id
    # No PatientIdentityRemoved / DeidentificationMethod. See the module docstring: the
    # claim about this corpus is made once, in the manifest, and not echoed into 40,000
    # image headers where it cannot be revised.


def _write_ct(
    *,
    volume: np.ndarray,
    header: dict[str, Any],
    out_dir: Path,
    case: str,
    corpus: Corpus,
    study_uid: str,
    series_uid: str,
    frame_uid: str,
    patient_id: str,
    study_date: str,
) -> tuple[list[str], list[np.ndarray], int]:
    """One CT instance per slice. Returns (sop uids, per-slice arrays, files written)."""
    unit, spacing, origin = _geometry(header)
    columns, rows, depth = (int(v) for v in header["sizes"])

    # NRRD indexes [x, y, z]; a DICOM frame is [row, column] = [y, x].
    orientation = [*unit[0].tolist(), *unit[1].tolist()]
    pixel_spacing = [float(spacing[1]), float(spacing[0])]  # row spacing, column spacing

    sop_uids: list[str] = []
    slices: list[np.ndarray] = []
    written = 0
    for k in range(depth):
        frame = np.ascontiguousarray(volume[:, :, k].T.astype(np.int16))
        slices.append(frame)
        sop_uid = _uid(corpus.corpus_id, case, "ct.instance", str(k))
        sop_uids.append(sop_uid)

        ds = Dataset()
        ds.file_meta = _file_meta(CTImageStorage, sop_uid)
        ds.is_little_endian = True
        ds.is_implicit_VR = False

        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1
        ds.StudyID = "1"
        ds.AccessionNumber = ""
        ds.StudyDate = study_date
        ds.StudyTime = "000000"
        ds.SeriesDate = study_date
        ds.SeriesTime = "000000"
        ds.StudyDescription = "Lung nodule CT (file-ingested)"
        ds.SeriesDescription = "CT"
        ds.Manufacturer = "UNKNOWN"
        ds.PositionReferenceIndicator = ""
        _identity(ds, patient_id=patient_id, corpus=corpus)

        ds.ImageOrientationPatient = [float(v) for v in orientation]
        position = origin + unit[2] * spacing[2] * k
        ds.ImagePositionPatient = [float(v) for v in position.tolist()]
        ds.PixelSpacing = pixel_spacing
        ds.SliceThickness = float(spacing[2])
        ds.SpacingBetweenSlices = float(spacing[2])
        ds.SliceLocation = float(np.dot(position, unit[2]))

        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.Rows = rows
        ds.Columns = columns
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 1  # signed: the source is `short`, already in HU
        ds.RescaleIntercept = "0"
        ds.RescaleSlope = "1"
        ds.RescaleType = "HU"
        ds.WindowCenter = "-600"
        ds.WindowWidth = "1500"
        ds.ImageType = ["DERIVED", "SECONDARY", "AXIAL"]
        ds.PixelData = frame.tobytes()

        ds.save_as(out_dir / f"ct.{k + 1:05d}.dcm", enforce_file_format=True)
        written += 1

    return sop_uids, slices, written


def _write_seg(
    *,
    labels: np.ndarray,
    segments: Sequence[dict[str, Any]],
    out_dir: Path,
    case: str,
    corpus: Corpus,
    study_uid: str,
    series_uid: str,
    frame_uid: str,
    patient_id: str,
    study_date: str,
    ct_sop_uids: Sequence[str],
    header: dict[str, Any],
) -> tuple[str, int]:
    """A multi-frame DICOM SEG, one frame per (segment, slice) that is non-empty.

    Empty frames are omitted rather than written as zeros: a 407-slice case with a nodule
    on 11 of them produces 22 frames instead of 814, and a reader that honours
    PerFrameFunctionalGroups reconstructs the identical mask either way.
    """
    unit, spacing, origin = _geometry(header)
    columns, rows, depth = (int(v) for v in header["sizes"])
    sop_uid = _uid(corpus.corpus_id, case, "seg.instance", "0")

    ds = Dataset()
    ds.file_meta = _file_meta(SegmentationStorage, sop_uid)
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    ds.SOPClassUID = SegmentationStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.FrameOfReferenceUID = frame_uid
    ds.Modality = "SEG"
    ds.SeriesNumber = 100
    ds.InstanceNumber = 1
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.StudyDate = study_date
    ds.StudyTime = "000000"
    ds.SeriesDate = study_date
    ds.SeriesTime = "000000"
    ds.ContentDate = study_date
    ds.ContentTime = "000000"
    ds.SeriesDescription = "Reference segmentation (as provided)"
    ds.ContentLabel = "REFERENCE"
    ds.ContentDescription = "Segmentation as provided with the source corpus"
    ds.ContentCreatorName = ""
    ds.Manufacturer = "UNKNOWN"
    ds.SegmentationType = "BINARY"
    ds.ImageType = ["DERIVED", "PRIMARY"]
    ds.LossyImageCompression = "00"
    _identity(ds, patient_id=patient_id, corpus=corpus)

    ds.Rows = rows
    ds.Columns = columns
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 1
    ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelRepresentation = 0

    # --- segment descriptions -----------------------------------------------------
    # The coded meaning is left as the corpus's own term under a private scheme. A
    # SNOMED code would be a clinical assertion this tool is not entitled to make:
    # nobody told us that `neo` means 108369006 Neoplasm, that is an inference from a
    # three-letter abbreviation, and an inferred code in a SegmentedPropertyType is
    # indistinguishable downstream from one a clinician chose.
    seq = []
    for position, seg in enumerate(segments, start=1):
        item = Dataset()
        item.SegmentNumber = position
        item.SegmentLabel = seg["name"]
        item.SegmentAlgorithmType = "MANUAL"
        item.SegmentedPropertyCategoryCodeSequence = [
            _code("M-01000", "SRT", "Morphologically Altered Structure")
        ]
        # CodeValue is VR SH: 16 bytes. The corpus id does not fit beside the term and
        # does not belong there either -- the scheme designator is what says whose
        # vocabulary this is, and the manifest says which corpus.
        item.SegmentedPropertyTypeCodeSequence = [
            _code(seg["name"][:16], "99MEDICALOS", f"{corpus.corpus_id}:{seg['name']}")
        ]
        colour = seg["color"]
        item.RecommendedDisplayCIELabValue = _cielab(colour)
        seq.append(item)
    ds.SegmentSequence = seq

    # --- frames ---------------------------------------------------------------------
    orientation = [float(v) for v in [*unit[0].tolist(), *unit[1].tolist()]]
    pixel_spacing = [float(spacing[1]), float(spacing[0])]

    shared = Dataset()
    plane = Dataset()
    plane.ImageOrientationPatient = orientation
    shared.PlaneOrientationSequence = [plane]
    measures = Dataset()
    measures.PixelSpacing = pixel_spacing
    measures.SliceThickness = float(spacing[2])
    measures.SpacingBetweenSlices = float(spacing[2])
    shared.PixelMeasuresSequence = [measures]
    ds.SharedFunctionalGroupsSequence = [shared]

    per_frame: list[Dataset] = []
    planes: list[np.ndarray] = []
    for position, seg in enumerate(segments, start=1):
        mask = labels == seg["label_value"]
        for k in range(depth):
            frame = mask[:, :, k]
            if not frame.any():
                continue
            planes.append(np.ascontiguousarray(frame.T.astype(np.uint8)))

            item = Dataset()
            ident = Dataset()
            ident.ReferencedSegmentNumber = position
            item.SegmentIdentificationSequence = [ident]
            pos = Dataset()
            pos.ImagePositionPatient = [
                float(v) for v in (origin + unit[2] * spacing[2] * k).tolist()
            ]
            item.PlanePositionSequence = [pos]
            derivation = Dataset()
            source = Dataset()
            source.ReferencedSOPClassUID = CTImageStorage
            source.ReferencedSOPInstanceUID = ct_sop_uids[k]
            derivation.SourceImageSequence = [source]
            derivation.DerivationImageSequence = []
            item.DerivationImageSequence = [derivation]
            per_frame.append(item)

    if not per_frame:
        raise ValueError(
            f"case {case}: every declared segment is empty in the voxels. A SEG with no "
            f"frames is not a segmentation of nothing, it is an absent segmentation, and "
            f"writing one would put a label in the archive that asserts a reader looked "
            f"and found nothing. Refusing."
        )

    ds.NumberOfFrames = len(per_frame)
    ds.PerFrameFunctionalGroupsSequence = per_frame

    referenced_series = Dataset()
    referenced_series.SeriesInstanceUID = _uid(corpus.corpus_id, case, "ct.series")
    referenced_series.ReferencedInstanceSequence = []
    for uid in ct_sop_uids:
        ref = Dataset()
        ref.ReferencedSOPClassUID = CTImageStorage
        ref.ReferencedSOPInstanceUID = uid
        referenced_series.ReferencedInstanceSequence.append(ref)
    study_ref = Dataset()
    study_ref.StudyInstanceUID = study_uid
    study_ref.ReferencedSeriesSequence = [referenced_series]
    ds.ReferencedSeriesSequence = [referenced_series]

    stacked = np.stack(planes) if planes else np.zeros((0, rows, columns), dtype=np.uint8)
    ds.PixelData = np.packbits(stacked.reshape(-1), bitorder="little").tobytes()

    ds.save_as(out_dir / "seg.00001.dcm", enforce_file_format=True)
    return sop_uid, 1


def _code(value: str, scheme: str, meaning: str) -> Dataset:
    item = Dataset()
    item.CodeValue = value
    item.CodingSchemeDesignator = scheme
    item.CodeMeaning = meaning
    return item


def _cielab(rgb: Sequence[float]) -> list[int]:
    """sRGB (0..1) to DICOM's 16-bit-scaled CIELab, per PS3.3 C.10.7.1.1."""

    def _linear(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (_linear(max(0.0, min(1.0, float(c)))) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.9505
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.0890

    def _f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t) + (16 / 116)

    fx, fy, fz = _f(x), _f(y), _f(z)
    lightness = max(0.0, 116 * fy - 16)
    a_star = 500 * (fx - fy)
    b_star = 200 * (fy - fz)
    return [
        int(round(lightness / 100 * 65535)),
        int(round((a_star + 128) / 255 * 65535)),
        int(round((b_star + 128) / 255 * 65535)),
    ]


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------


def _cases(root: Path) -> Iterator[tuple[str, Path, Path]]:
    """Every (case, image, segmentation) triple under a corpus root, in numeric order."""
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: (len(p.parts), _natural(p.name)),
    ):
        images = sorted(
            p for p in directory.glob("*.nrrd") if not p.name.endswith(".seg.nrrd")
        )
        segs = sorted(directory.glob("*.seg.nrrd"))
        if len(images) == 1 and len(segs) == 1:
            yield directory.name, images[0], segs[0]


def _natural(name: str) -> tuple[int, str]:
    return (int(name), "") if name.isdigit() else (1 << 30, name)


def convert_case(
    *,
    case: str,
    image_path: Path,
    seg_path: Path,
    out_root: Path,
    corpus: Corpus,
    study_date: str,
) -> CaseResult:
    volume, header = nrrd.read(str(image_path))
    labels, seg_header = nrrd.read(str(seg_path))
    if volume.shape != labels.shape:
        raise ValueError(
            f"case {case}: image is {volume.shape} and segmentation is {labels.shape}. "
            f"Resampling one onto the other here would hide a mismatch that belongs in "
            f"front of whoever exported them. Refusing."
        )
    segments = _segments(seg_header, raw_segment_names(seg_path))
    if not segments:
        raise ValueError(f"case {case}: the .seg.nrrd declares no segments")

    patient_id = _surrogate_patient_id(corpus.corpus_id, case)
    study_uid = _uid(corpus.corpus_id, case, "study")
    ct_series_uid = _uid(corpus.corpus_id, case, "ct.series")
    seg_series_uid = _uid(corpus.corpus_id, case, "seg.series")
    frame_uid = _uid(corpus.corpus_id, case, "frame-of-reference")

    out_dir = out_root / corpus.corpus_id / case
    out_dir.mkdir(parents=True, exist_ok=True)

    ct_sop_uids, slices, ct_files = _write_ct(
        volume=volume,
        header=header,
        out_dir=out_dir,
        case=case,
        corpus=corpus,
        study_uid=study_uid,
        series_uid=ct_series_uid,
        frame_uid=frame_uid,
        patient_id=patient_id,
        study_date=study_date,
    )
    _seg_uid, seg_files = _write_seg(
        labels=labels,
        segments=segments,
        out_dir=out_dir,
        case=case,
        corpus=corpus,
        study_uid=study_uid,
        series_uid=seg_series_uid,
        frame_uid=frame_uid,
        patient_id=patient_id,
        study_date=study_date,
        ct_sop_uids=ct_sop_uids,
        header=header,
    )

    # MOS-EVID-018 asks dataset identity to rest on the pixels. This digest is over the
    # frames in the order they were written, so it changes if the conversion changes and
    # does not change if only a tag does.
    digest = hashlib.sha256()
    for frame in slices:
        digest.update(frame.tobytes())

    counts = {int(v): int((labels == v).sum()) for v in (s["label_value"] for s in segments)}
    return CaseResult(
        case=case,
        study_instance_uid=study_uid,
        ct_series_uid=ct_series_uid,
        seg_series_uid=seg_series_uid,
        patient_id=patient_id,
        slices=len(slices),
        rows=int(header["sizes"][1]),
        columns=int(header["sizes"][0]),
        segments=[dict(s, voxels=counts[s["label_value"]]) for s in segments],
        pixel_sha256=digest.hexdigest(),
        files=ct_files + seg_files,
    )


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog=TOOL,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--root", required=True, metavar="DIR", help="corpus root to read (never written)")
    p.add_argument("--out", required=True, metavar="DIR", help="where the DICOM is written")
    p.add_argument("--corpus-id", required=True, metavar="ID")
    p.add_argument("--limit", type=int, default=0, metavar="N", help="convert only the first N cases")
    p.add_argument("--study-date", default="19000101", metavar="YYYYMMDD",
                   help="the date written to every study (default: 19000101, a date that "
                        "is obviously not a real one)")
    p.add_argument("--deidentification-status", default="public_deidentified",
                   choices=("identified", "pseudonymised", "public_deidentified"))
    p.add_argument("--deid-policy-id", required=True, metavar="ID")
    p.add_argument("--uid-mapping-table-id", required=True, metavar="ID")
    p.add_argument("--legal-basis", required=True, metavar="TEXT")
    p.add_argument("--legal-basis-holder", required=True, metavar="NAME")
    p.add_argument("--manifest", metavar="FILE", help="where the provenance manifest is written")
    args = p.parse_args(argv)

    root = Path(args.root)
    out_root = Path(args.out)
    if not root.is_dir():
        return _die(f"--root {root} is not a directory")
    try:
        out_root.resolve().relative_to(root.resolve())
    except ValueError:
        pass
    else:
        return _die(
            f"--out {out_root} is inside --root {root}. The source corpus is read-only; "
            f"writing the conversion into it would modify data this tool has no right to "
            f"touch. Refusing."
        )

    corpus = Corpus(
        corpus_id=args.corpus_id,
        source_root=str(root),
        deidentification_status=args.deidentification_status,
        deid_policy_id=args.deid_policy_id,
        uid_mapping_table_id=args.uid_mapping_table_id,
        legal_basis=args.legal_basis,
        legal_basis_holder=args.legal_basis_holder,
        known_limitations=[
            "The source NRRD carried no DICOM UIDs, so the UIDs here are minted and do "
            "not identify the acquisition in the originating PACS.",
            "Because the subject is unknown, the same person exported twice appears as "
            "two unrelated patients, and MOS-EVID-034 L1 cannot detect it.",
            "PatientBirthDate and PatientSex are empty: the values are unknown, and a "
            "plausible value would be a fabricated clinical fact.",
            "SegmentedPropertyTypeCodeSequence uses the corpus's own terms under a "
            "private coding scheme; no SNOMED code was inferred from a label string.",
        ],
    )

    print("-" * 86)
    print(f"{TOOL} {VERSION}")
    print("-" * 86)
    print(f"  corpus     {corpus.corpus_id}")
    print(f"  read       {root}")
    print(f"  write      {out_root}")
    print(f"  status     {corpus.deidentification_status}")
    print("-" * 86)

    found = list(_cases(root))
    if args.limit:
        found = found[: args.limit]
    if not found:
        return _die(f"no case/<n>.nrrd + <n>.seg.nrrd pairs under {root}")

    results: list[CaseResult] = []
    files = 0
    for index, (case, image, seg) in enumerate(found, start=1):
        try:
            result = convert_case(
                case=case,
                image_path=image,
                seg_path=seg,
                out_root=out_root,
                corpus=corpus,
                study_date=args.study_date,
            )
        except Exception as exc:  # noqa: BLE001 - one bad case must not lose the rest
            print(f"  [{index:3d}/{len(found)}] case {case:>4s}  REFUSED  {exc}")
            continue
        results.append(result)
        files += result.files
        seg_summary = " ".join(
            f"{s['name']}={s['voxels']}" for s in result.segments
        )
        print(
            f"  [{index:3d}/{len(found)}] case {case:>4s}  "
            f"{result.slices:4d} slices  {result.rows}x{result.columns}  {seg_summary}"
        )

    print("-" * 86)
    print(f"  cases converted                  {len(results):>8d}")
    print(f"  cases refused                    {len(found) - len(results):>8d}")
    print(f"  DICOM instances written          {files:>8d}")
    print("-" * 86)

    manifest = {
        "tool": TOOL,
        "tool_version": VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "uid_root": UID_ROOT,
        "ingest_namespace": str(INGEST_NAMESPACE),
        "corpus": {
            "corpus_id": corpus.corpus_id,
            "source_root": corpus.source_root,
            "deidentification_status": corpus.deidentification_status,
            "deid_policy_id": corpus.deid_policy_id,
            "uid_mapping_table_id": corpus.uid_mapping_table_id,
            "legal_basis": corpus.legal_basis,
            "legal_basis_holder": corpus.legal_basis_holder,
            "known_limitations": corpus.known_limitations,
        },
        "cases": [
            {
                "case": r.case,
                "study_instance_uid": r.study_instance_uid,
                "ct_series_instance_uid": r.ct_series_uid,
                "seg_series_instance_uid": r.seg_series_uid,
                "patient_id": r.patient_id,
                "slices": r.slices,
                "rows": r.rows,
                "columns": r.columns,
                "segments": r.segments,
                "pixel_sha256": r.pixel_sha256,
            }
            for r in results
        ],
        "study_instance_uids": [r.study_instance_uid for r in results],
    }
    if args.manifest:
        path = Path(args.manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"  manifest : {path}")
        print("-" * 86)
    return 0 if results else 1


def _die(message: str) -> int:
    print(f"{TOOL}: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
