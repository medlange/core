# SPDX-License-Identifier: Apache-2.0
"""One ramp, three VOI LUT functions, identical windows.

(0028,1056) says which transfer function a window was authored for. The shader implemented
only LINEAR, which is the standard's default and the only one a CT normally carries — so a
SIGMOID window, which several MR vendors ship, rendered with the linear ramp.

WHAT THAT LOOKS LIKE
----------------------
LINEAR clips: everything below c-(w-1)/2 is pure black and everything above is pure white.
SIGMOID never reaches either end — it is soft-shouldered by construction, compressing
contrast away from the centre instead of discarding it. Rendered as LINEAR, a SIGMOID
window throws away exactly the tissue the acquisition chose that function to keep, and the
result is a plausible, slightly harsher picture with nothing on screen saying so.

So the measurement is what happens at the ends of the ramp:

    LINEAR         clamps to 0 and 255 well inside the range
    LINEAR_EXACT   clamps too, but at c ± w/2 rather than c-0.5 ± (w-1)/2
    SIGMOID        never reaches 0 or 255 anywhere

A stored ramp spanning several windows' worth of range makes all three visible at once, and
the three members differ ONLY in (0028,1056).

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""

import hashlib
import io
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage

ROOT = "1.2.826.0.1.3680043.8.498."
SIZE = 256
CENTER = 2048
WIDTH = 2048
FUNCTIONS = ["LINEAR", "LINEAR_EXACT", "SIGMOID"]


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-voi-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def build(function: str) -> bytes:
    sop = uid("instance", function)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = MRImageStorage
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("implementation")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = MRImageStorage
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = uid("study")
    ds.SeriesInstanceUID = uid("series", function)
    ds.FrameOfReferenceUID = uid("frame-of-reference", function)
    ds.Modality = "MR"
    ds.PatientName = "PHANTOM^VOI^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-VOI"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = FUNCTIONS.index(function) + 1
    ds.InstanceNumber = 1
    ds.SeriesDescription = f"ramp, VOILUTFunction {function}"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "OTHER"]

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = SIZE, SIZE
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.RescaleSlope = 1
    ds.RescaleIntercept = 0
    ds.RescaleType = "US"
    ds.WindowCenter = CENTER
    ds.WindowWidth = WIDTH
    ds.VOILUTFunction = function
    ds.PixelSpacing = [1.0, 1.0]
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]

    # 0 .. 4095 left to right: two windows' worth, so both shoulders are on screen.
    ramp = np.tile(np.linspace(0, 4095, SIZE, dtype=np.float64).astype(np.uint16), (SIZE, 1))
    ds.PixelData = ramp.tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    for function in FUNCTIONS:
        blob = build(function)
        path = f"{out_dir}/voi-{function.lower()}.dcm"
        with open(path, "wb") as handle:
            handle.write(blob)
        print(f"{function}\tC {CENTER} W {WIDTH}\t{len(blob)} bytes\t{path}")


if __name__ == "__main__":
    main(sys.argv[1])
