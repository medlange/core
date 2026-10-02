# SPDX-License-Identifier: Apache-2.0
"""Three series that differ only in what (0028,0301) says about their own pixels.

Every other identifier this viewer handles is a header field it chooses whether to render.
A name burned into the pixels arrives with the anatomy, and MOS-DATA-040 permits exactly
that: `pixel_phi.action: ALLOW` is "no modification; permitted only when the consumer class
is clinical_viewer". The platform deliberately lets unredacted pixels reach this surface,
so the surface is where a reader has to be told.

ABSENT IS NOT "NO"
--------------------
(0028,0301) is Type 1C. A study that never declares it is not a study that declared itself
clean — most secondary captures and re-photographed films say nothing at all. The three
members exist to keep those apart:

    yes       declares YES        the pixels DO carry identifiers
    no        declares NO         the pixels do not
    silent    omits the tag       nobody has said, and this surface has not screened

A viewer that folds `silent` into `no` reports a clean study it never checked. One that
folds it into `yes` cries wolf on every secondary capture. The wording has to hold three
states because the data does.

WHAT THE PIXELS ACTUALLY CONTAIN
----------------------------------
A bright rectangle in the corner where burned-in text sits, and nothing resembling text.
The probe is about the DECLARATION, and writing something that looked like a name would put
a fake identifier into the repository for no gain.

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
SIZE = 64

VARIANTS = {"yes": "YES", "no": "NO", "silent": None}


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-burnedin-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def build(key: str, declared) -> bytes:
    sop = uid("instance", key)
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
    ds.SeriesInstanceUID = uid("series", key)
    ds.FrameOfReferenceUID = uid("frame-of-reference", key)
    ds.Modality = "OT"
    ds.PatientName = "PHANTOM^BURNEDIN^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-BURNEDIN"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.SeriesDescription = f"(0028,0301) {declared or 'absent'}"

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
    ds.WindowCenter = 2048
    ds.WindowWidth = 4096
    ds.PixelSpacing = [0.5, 0.5]
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    if declared is not None:
        ds.BurnedInAnnotation = declared

    pixels = np.full((SIZE, SIZE), 400, dtype=np.uint16)
    # A bright block where burned-in text would sit. Deliberately not text.
    pixels[4:12, 4:40] = 3800
    ds.PixelData = pixels.tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    for key, declared in VARIANTS.items():
        blob = build(key, declared)
        path = f"{out_dir}/burnedin-{key}.dcm"
        with open(path, "wb") as handle:
            handle.write(blob)
        print(f"{key}\t(0028,0301) = {declared or 'absent'}\t{len(blob)} bytes\t{path}")


if __name__ == "__main__":
    main(sys.argv[1])
