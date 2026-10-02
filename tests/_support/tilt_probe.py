# SPDX-License-Identifier: Apache-2.0
"""A tilted-gantry stack with a marker at a fixed point in the patient, and a flat control.

With the gantry tilted the table steps along the patient's z axis while the imaging plane's
normal leans away from it, so consecutive slices are offset from each other WITHIN the
plane. Stacking them at a common origin — a contiguous copy — packs a sheared volume. The
axial plane is unaffected, because it IS the acquired plane. The coronal and sagittal lean,
and a caliper across the lean measures a distance no anatomy has.

WHY THE MARKER IS THE WHOLE TEST
----------------------------------
A small bright marker is written at ONE patient position, and its row index is computed per
slice from the geometry — so in the images it walks up the frame as the table advances,
exactly as a fixed point does under a tilt:

    row(z) = row(0) − z · step · sin(tilt) / rowSpacing

A reconstruction that corrects the shear puts every one of those back on the same volume
row, so a coronal cut at that row shows the marker across the whole depth. One that does
not leaves the marker on a different row per slice, so the same cut catches it once.

The count of depth columns containing the marker is therefore the measurement: `depth` when
the correction is right, about 1 when it is absent. There is no judgement in reading it.

The flat member carries the same marker at a constant row with no tilt, and is what the
corrected tilted member has to match.

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""

import hashlib
import io
import math
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

ROOT = "1.2.826.0.1.3680043.8.498."
SIZE = 64
SLICES = 20
STEP_MM = 3.0          # table advance per slice, along patient z
TILT_DEG = 30.0        # gantry tilt about the patient's left-right axis
ROW_MM = COL_MM = 1.0
MARKER_ROW0 = 50       # marker row on slice 0
MARKER_COL = 32
MARKER_STORED = 3024   # 2000 HU against intercept -1024: unmistakable
BACKGROUND_STORED = 24  # -1000 HU, air


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-tilt-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def marker_row(index: int, tilt_deg: float) -> int:
    """Where a FIXED patient point lands in slice `index` under this tilt."""
    return int(round(MARKER_ROW0 - index * STEP_MM * math.sin(math.radians(tilt_deg)) / ROW_MM))


def build(kind: str, index: int) -> bytes:
    tilt = TILT_DEG if kind == "tilted" else 0.0
    theta = math.radians(tilt)

    sop = uid("instance", kind, str(index))
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("implementation")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = uid("study")
    ds.SeriesInstanceUID = uid("series", kind)
    ds.FrameOfReferenceUID = uid("frame-of-reference", kind)
    ds.Modality = "CT"
    ds.PatientName = "PHANTOM^TILT^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-TILT"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = 1 if kind == "flat" else 2
    ds.InstanceNumber = index + 1
    ds.SeriesDescription = f"{kind} gantry, marker at one patient point"
    if tilt:
        ds.GantryDetectorTilt = tilt

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = SIZE, SIZE
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.RescaleSlope = 1
    ds.RescaleIntercept = -1024
    ds.RescaleType = "HU"
    ds.WindowCenter = 40
    ds.WindowWidth = 400
    ds.PixelSpacing = [ROW_MM, COL_MM]

    # Row direction stays along patient x; the column direction leans by the tilt, which is
    # what makes the plane normal lean away from the table axis.
    ds.ImageOrientationPatient = [1, 0, 0, 0, math.cos(theta), math.sin(theta)]
    ds.ImagePositionPatient = [0.0, 0.0, index * STEP_MM]

    pixels = np.full((SIZE, SIZE), BACKGROUND_STORED, dtype=np.uint16)
    row = marker_row(index, tilt)
    if 0 <= row < SIZE:
        pixels[row - 1:row + 2, MARKER_COL - 1:MARKER_COL + 2] = MARKER_STORED
    ds.PixelData = pixels.tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    for kind in ("flat", "tilted"):
        for index in range(SLICES):
            blob = build(kind, index)
            with open(f"{out_dir}/tilt-{kind}{index}.dcm", "wb") as handle:
                handle.write(blob)
        tilt = TILT_DEG if kind == "tilted" else 0.0
        first, last = marker_row(0, tilt), marker_row(SLICES - 1, tilt)
        print(f"{kind}\ttilt {tilt}°\tmarker row {first} -> {last} across {SLICES} slices")


if __name__ == "__main__":
    main(sys.argv[1])
