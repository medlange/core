# SPDX-License-Identifier: Apache-2.0
"""Two stacks with the same slices, one of them missing a block in the middle.

`sliceSpacing` was `abs(frames[1].depth - frames[0].depth)` -- the gap between the first
two slices, applied to the whole stack. It is used as the reconstruction's craniocaudal
axis in `mpr.js`, as the SEG position-matching tolerance in `seg.js`, and as the HUD's
millimetres per slice. So a series with a gap reported the pitch of its first pair and
closed the gap silently: the coronal view drew the wide gap at the narrow spacing, anatomy
appeared at positions nothing was acquired at, and a caliper down that axis was short by
the whole of the missing block.

THE PAIR
---------
    even    z = 0, 2, 4, 6, 8, 10        every gap 2 mm
    gapped  z = 0, 2, 4, 14, 16, 18      one gap of 10 mm among 2 mm gaps

Same slice count, same pixels, same pitch on the first pair -- which is exactly what the
old code measured. The stacks are distinguishable only by looking at every gap.

A dropped instance is the common cause, and it is not exotic: a two-block acquisition, a
partial retrieve, or a transfer that lost one object all produce it, and none of them look
like an error anywhere else in the pipeline.

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""

import hashlib
import io
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

ROOT = "1.2.826.0.1.3680043.8.498."
SIZE = 32

POSITIONS = {
    "even": [0.0, 2.0, 4.0, 6.0, 8.0, 10.0],
    "gapped": [0.0, 2.0, 4.0, 14.0, 16.0, 18.0],
}


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-spacing-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def build(kind: str, index: int, z: float) -> bytes:
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
    ds.PatientName = "PHANTOM^SPACING^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-SPACING"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = 1 if kind == "even" else 2
    ds.InstanceNumber = index + 1
    ds.SeriesDescription = f"{kind} spacing, six slices"

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
    ds.PixelSpacing = [1.0, 1.0]
    ds.ImagePositionPatient = [0.0, 0.0, z]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    # Each slice a flat value that encodes its own index, so a reconstruction's rows can be
    # read back as "which slice landed here".
    ds.PixelData = np.full((SIZE, SIZE), 1024 + index * 100, dtype=np.uint16).tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    for kind, zs in POSITIONS.items():
        for index, z in enumerate(zs):
            blob = build(kind, index, z)
            path = f"{out_dir}/spacing-{kind}{index}.dcm"
            with open(path, "wb") as handle:
                handle.write(blob)
        gaps = [round(b - a, 2) for a, b in zip(zs, zs[1:])]
        print(f"{kind}\tz = {zs}\tgaps {gaps}")


if __name__ == "__main__":
    main(sys.argv[1])
