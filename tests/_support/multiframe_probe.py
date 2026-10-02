# SPDX-License-Identifier: Apache-2.0
"""A paired control for multi-frame instances, spatial and temporal.

One DICOM instance can carry a whole acquisition in a single (7FE0,0010). Until the loader
read (0028,0008) it computed `rows * columns` and took exactly that many values, so an
Enhanced CT or MR instance produced a stack of depth 1 and the other N-1 frames were
discarded without a word. `seg.js` has always read the tag -- a SEG is always multi-frame --
so a multi-frame image carrying a segmentation would have matched a hundred SEG frames
against a stack one slice deep.

WHY THE PAIR IS SPATIAL VERSUS TEMPORAL, NOT PRESENT VERSUS ABSENT
--------------------------------------------------------------------
Reading N frames is the easy half. The half that decides whether a number means anything is
knowing what separates them:

    ENHANCED   PerFrameFunctionalGroupsSequence gives each frame its own position, so the
               frames are separated by DISTANCE and a reconstruction is meaningful.
    CINE       No per-frame position. The frames are separated by TIME. `mpr.js` would
               still resample them, using `sliceSpacing` as though it were millimetres --
               producing one image row plotted against seconds, drawn with a mm scale, and
               a caliper across it returns a distance for a duration.

Both members have identical pixel data and identical frame counts. Only the geometry
differs, so the pair isolates exactly the property under test.

FRAME k IS FILLED WITH k * 100
--------------------------------
So a probe that reads frame k's centre pixel proves two things at once: that all N frames
were extracted, and that they are in the right ORDER. A loader that read only the first
frame returns 0 for every k; one that mis-strides returns the wrong multiple.

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""

import hashlib
import io
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

ROOT = "1.2.826.0.1.3680043.8.498."
ROWS = COLS = 64
FRAMES = 12
SLICE_MM = 2.5


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-multiframe-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def pixel_block() -> bytes:
    """Frame k is uniformly k * 100. Order and stride are both readable from one sample."""
    stack = np.zeros((FRAMES, ROWS, COLS), dtype=np.uint16)
    for k in range(FRAMES):
        stack[k, :, :] = k * 100
    return stack.tobytes()


def build(kind: str) -> bytes:
    """`kind` is 'enhanced' (per-frame positions) or 'cine' (none)."""
    sop = uid("instance", kind)
    meta = FileMetaDataset()
    # Enhanced CT Image Storage for both, so the SOP class is not what distinguishes them.
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2.1"
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("implementation")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2.1"
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = uid("study")
    ds.SeriesInstanceUID = uid("series", kind)
    ds.FrameOfReferenceUID = uid("frame-of-reference", kind)
    ds.Modality = "CT"
    ds.PatientName = "PHANTOM^MULTIFRAME^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-MULTIFRAME"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = 1 if kind == "enhanced" else 2
    ds.InstanceNumber = 1
    ds.SeriesDescription = (
        f"{FRAMES} frames, "
        f"{'per-frame positions' if kind == 'enhanced' else 'no positions (cine)'}"
    )

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = ROWS, COLS
    ds.NumberOfFrames = FRAMES
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelSpacing = [0.7, 0.7]

    if kind == "cine":
        # Legacy multi-frame keeps its Modality LUT and window at the top level.
        ds.RescaleSlope = 1
        ds.RescaleIntercept = 0
        ds.WindowCenter = 600
        ds.WindowWidth = 1400

    if kind == "enhanced":
        # Shared: what every frame agrees on. Per-frame: what only this frame knows.
        measures = Dataset()
        measures.PixelSpacing = [0.7, 0.7]
        measures.SliceThickness = SLICE_MM
        orientation = Dataset()
        orientation.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]

        # THE MODALITY LUT AND THE WINDOW LIVE ONLY HERE, and that is the point of the
        # probe rather than an incidental detail. On a real Enhanced instance they are
        # absent from the top level, so a loader that reads geometry from the functional
        # groups and rescale from the dataset gets perfect positions and slope 1 /
        # intercept 0 -- raw stored values wearing a Hounsfield label.
        #
        # The values are chosen to be unmistakable: intercept -1024 cannot be confused
        # with the 0 default, and 350/1500 is not the 40/400 the viewer falls back to.
        transformation = Dataset()
        transformation.RescaleSlope = 1
        transformation.RescaleIntercept = -1024
        transformation.RescaleType = "HU"
        voi = Dataset()
        voi.WindowCenter = 350
        voi.WindowWidth = 1500

        shared = Dataset()
        shared.PixelMeasuresSequence = [measures]
        shared.PlaneOrientationSequence = [orientation]
        shared.PixelValueTransformationSequence = [transformation]
        shared.FrameVOILUTSequence = [voi]
        ds.SharedFunctionalGroupsSequence = [shared]

        per_frame = []
        for k in range(FRAMES):
            position = Dataset()
            position.ImagePositionPatient = [0.0, 0.0, k * SLICE_MM]
            group = Dataset()
            group.PlanePositionSequence = [position]
            per_frame.append(group)
        ds.PerFrameFunctionalGroupsSequence = per_frame
    # `cine` deliberately carries NO position at any level -- not per-frame, not shared and
    # not top-level. A frame that cannot say where it is must not be resampled as if it did.

    ds.PixelData = pixel_block()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    for kind in ("enhanced", "cine"):
        blob = build(kind)
        path = f"{out_dir}/multiframe-{kind}.dcm"
        with open(path, "wb") as handle:
            handle.write(blob)
        print(f"{kind}\t{FRAMES} frames\t{len(blob)} bytes\t{path}")


if __name__ == "__main__":
    main(sys.argv[1])
