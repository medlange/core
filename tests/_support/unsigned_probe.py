# SPDX-License-Identifier: Apache-2.0
"""A full-range unsigned ramp, which renders as a sawtooth if the pipeline wraps.

(0028,0103) PixelRepresentation = 0 means the stored values are UNSIGNED, and with
BitsStored 16 they run to 65535. The viewer read the attribute and built the correct
Uint16Array, then handed it to `new Int16Array(...)` on the way to an R16I texture and to
an Int16Array MPR volume. That conversion is element-wise and wraps: 40000 becomes -25536.

WHY A FULL-RANGE RAMP MAKES THE DEFECT UNMISTAKABLE
----------------------------------------------------
Stored values run 0 on the left to 65535 on the right, windowed across that whole range.
Rendered correctly the row is monotonically increasing. If the pipeline wraps, everything
above 32767 turns large and negative, so the row climbs to white at the halfway point and
crashes straight to black -- a SAWTOOTH. That signature cannot be produced by a window
that is merely wrong, which is what makes it worth minting a file for.

The reader-facing version of this is worse than a stripe: a modality whose stored values
legitimately exceed 32767 shows its bright anatomy as black, and the HU readout under the
cursor reports a large negative number with complete confidence.

Affects unsigned 16-bit data generally -- ultrasound, some MR, PET count data and any
BitsStored 16 detector output. It does not affect CT, which is signed, which is exactly
why it survived every check made against the phantom.

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""

import hashlib
import io
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage

ROOT = "1.2.826.0.1.3680043.8.498."
ROWS = COLS = 256
PEAK = 65535


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-unsigned-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def build() -> bytes:
    ramp = np.tile(
        np.linspace(0, PEAK, COLS, dtype=np.float64).astype(np.uint16), (ROWS, 1)
    )

    sop = uid("instance")
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("implementation")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = uid("study")
    ds.SeriesInstanceUID = uid("series")
    ds.FrameOfReferenceUID = uid("frame-of-reference")
    ds.Modality = "OT"
    ds.PatientName = "PHANTOM^UNSIGNED^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-UNSIGNED"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.SeriesDescription = "unsigned ramp 0..65535 left-to-right"

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = ROWS, COLS
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0                     # UNSIGNED: 0..65535, not -32768..32767
    ds.RescaleSlope = 1
    ds.RescaleIntercept = 0
    ds.WindowCenter = PEAK // 2
    ds.WindowWidth = PEAK
    ds.PixelSpacing = [0.5, 0.5]
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelData = ramp.tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    blob = build()
    path = f"{out_dir}/unsigned-ramp.dcm"
    with open(path, "wb") as handle:
        handle.write(blob)
    print(f"unsigned ramp 0..{PEAK}\t{len(blob)} bytes\t{path}")


if __name__ == "__main__":
    main(sys.argv[1])
