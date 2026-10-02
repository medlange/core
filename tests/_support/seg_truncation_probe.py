# SPDX-License-Identifier: Apache-2.0
"""A segmentation whose bitstream stops half way, and the CT it claims to cover.

`decodeSegmentation` walks the per-frame functional groups and unpacks one bit per pixel:

    if ((packed[bit >> 3] >> (bit & 7)) & 1) plane[p] = segNumber;

`packed` is a Uint8Array. Past its end that index is `undefined`, `undefined >> n` is 0,
and every remaining pixel unpacks to background. So a SEG whose (7FE0,0010) is short — a
truncated transfer, or a per-frame sequence that disagrees with (0028,0008) — renders fully
on the early slices and empty on the late ones, throws nothing, and increments nothing:
`matchedBy` still counts every frame as matched and `unmatched` stays 0, so the segments
panel prints "aligned: N by SOP reference, 0 unmatched" over a mask that stopped.

That is the reassurance being wrong at the exact moment it matters. `volume.js` already
refuses the equivalent truncation for images by name (`truncated_pixel_data`); the SEG path
did not, and the two have to agree.

WHAT THE PAIR IS
-----------------
Four CT slices, and a SEG that declares four frames covering all of them with a segment
that fills the frame edge to edge. Its PixelData carries only the first two frames' bytes.
A viewer that checks the length refuses the object; one that does not draws slices 0 and 1
and leaves 2 and 3 blank while reporting that nothing was unmatched.

Filling each frame COMPLETELY is deliberate: a partial mask could be mistaken for a model
that segmented less than expected, and the point of the probe is to remove that reading.

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""

import hashlib
import io
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, SegmentationStorage

ROOT = "1.2.826.0.1.3680043.8.498."
ROWS = COLS = 32
SLICES = 4
SLICE_MM = 2.0
#: Frames whose bytes are actually present. The other two are declared and absent.
FRAMES_PRESENT = 2


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-seg-truncation/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def _identity(ds: Dataset) -> None:
    ds.PatientName = "PHANTOM^SEGTRUNC^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-SEGTRUNC"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""


def build_ct(index: int, study: str, series: str, frame_of_reference: str) -> bytes:
    sop = uid("ct", str(index))
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("implementation")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = series
    ds.FrameOfReferenceUID = frame_of_reference
    ds.Modality = "CT"
    _identity(ds)
    ds.SeriesNumber = 1
    ds.InstanceNumber = index + 1
    ds.SeriesDescription = "four slices, for a SEG that covers all of them"

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = ROWS, COLS
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.RescaleSlope = 1
    ds.RescaleIntercept = -1024
    ds.RescaleType = "HU"
    ds.WindowCenter = 40
    ds.WindowWidth = 400
    ds.PixelSpacing = [1.0, 1.0]
    ds.ImagePositionPatient = [0.0, 0.0, index * SLICE_MM]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelData = np.full((ROWS, COLS), 1224, dtype=np.uint16).tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def build_seg(
    study: str, frame_of_reference: str, ct_sops: list[str], *, truncate: bool
) -> bytes:
    sop = uid("seg", "short" if truncate else "whole")
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SegmentationStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("implementation")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = SegmentationStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = uid("seg-series", "short" if truncate else "whole")
    ds.FrameOfReferenceUID = frame_of_reference
    ds.Modality = "SEG"
    _identity(ds)
    ds.SeriesNumber = 2
    ds.InstanceNumber = 1
    ds.SeriesDescription = (
        "declares 4 frames, carries 2" if truncate else "declares 4 frames, carries 4"
    )

    ds.SegmentationType = "BINARY"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = ROWS, COLS
    ds.NumberOfFrames = SLICES
    ds.BitsAllocated = 1
    ds.BitsStored = 1
    ds.HighBit = 0
    ds.PixelRepresentation = 0

    segment = Dataset()
    segment.SegmentNumber = 1
    segment.SegmentLabel = "Whole frame"
    segment.SegmentAlgorithmType = "AUTOMATIC"
    segment.SegmentAlgorithmName = "medos-probe"
    ds.SegmentSequence = [segment]

    per_frame = []
    for index, source_sop in enumerate(ct_sops):
        source = Dataset()
        source.ReferencedSOPClassUID = CTImageStorage
        source.ReferencedSOPInstanceUID = source_sop
        derivation = Dataset()
        derivation.SourceImageSequence = [source]
        identification = Dataset()
        identification.ReferencedSegmentNumber = 1
        position = Dataset()
        position.ImagePositionPatient = [0.0, 0.0, index * SLICE_MM]

        group = Dataset()
        group.DerivationImageSequence = [derivation]
        group.SegmentIdentificationSequence = [identification]
        group.PlanePositionSequence = [position]
        per_frame.append(group)
    ds.PerFrameFunctionalGroupsSequence = per_frame

    # Every bit set: the segment fills every frame it carries, edge to edge.
    whole = np.ones(SLICES * ROWS * COLS, dtype=np.uint8)
    packed = np.packbits(whole, bitorder="little").tobytes()
    if truncate:
        packed = packed[: len(packed) * FRAMES_PRESENT // SLICES]
    # Part 10 requires an even length; the shortfall is what the probe is about.
    if len(packed) % 2:
        packed += b"\x00"
    ds.PixelData = packed

    # A COMPLETE Part 10 object whose pixel data is honestly short. The file is well formed
    # -- preamble, DICM magic, file meta group length -- and the PixelData element's own
    # length matches the bytes that follow it. The only disagreement is between (0028,0008),
    # which says four frames, and the bytes, which cover two. That is the realistic shape:
    # a per-frame sequence longer than the pixels, not a corrupted file. A corrupted file
    # would be refused by the parser for a different reason and would prove nothing about
    # the unpack loop.
    ds.preamble = bytes(128)
    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    study = uid("study")
    series = uid("ct-series")
    frame_of_reference = uid("frame-of-reference")

    sops = []
    for index in range(SLICES):
        blob = build_ct(index, study, series, frame_of_reference)
        path = f"{out_dir}/segtrunc-ct{index}.dcm"
        with open(path, "wb") as handle:
            handle.write(blob)
        sops.append(uid("ct", str(index)))
        print(f"ct{index}\t{len(blob)} bytes\t{path}")

    for truncate, name in ((False, "whole"), (True, "short")):
        blob = build_seg(study, frame_of_reference, sops, truncate=truncate)
        path = f"{out_dir}/segtrunc-seg-{name}.dcm"
        with open(path, "wb") as handle:
            handle.write(blob)
        need = SLICES * ROWS * COLS // 8
        have = need * FRAMES_PRESENT // SLICES if truncate else need
        print(f"seg-{name}\tdeclares {SLICES} frames, carries {have}/{need} bytes\t{path}")


if __name__ == "__main__":
    main(sys.argv[1])
