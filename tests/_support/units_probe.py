# SPDX-License-Identifier: Apache-2.0
"""Three instances whose pixels are identical and whose units are not.

The viewer wrote the literal "HU" in three places — the cursor readout, the on-image
annotation label and the measurements panel — and none of them consulted the modality.
(0008,0060) sat in the tag table read by nothing; (0054,1001) Units and (0028,1054)
RescaleType were not read at all.

WHY IDENTICAL PIXELS
----------------------
Every member carries the same stored values and the same rescale, so the NUMBER each
screen prints is the same in all three. Only the unit differs. A viewer that derives the
unit renders three different labels from one number; a viewer that hardcodes "HU" renders
the same label three times, and the pair of screenshots is the whole argument.

    CT   RescaleType HU              ->  "20 HU"
    PT   Units BQML                  ->  "20 BQML"
    MR   no Units, RescaleType US    ->  "20 · unit not recorded"

The third is the one that matters most. MR stored values are arbitrary; there is no
Hounsfield scale and no unit in the header at all. The viewer must not invent one — saying
"signal intensity" would be the platform asserting a vocabulary DICOM does not supply for
that field — and must not print a bare number either, because a bare number invites the
reader to supply "HU" from habit.

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""

import hashlib
import io
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    MRImageStorage,
    PositronEmissionTomographyImageStorage,
)

ROOT = "1.2.826.0.1.3680043.8.498."
ROWS = COLS = 64

# stored 1044 with intercept -1024 gives 20 in every member, so the number never varies.
STORED = 1044
INTERCEPT = -1024

VARIANTS = [
    # (key, modality, sop class, Units (0054,1001), RescaleType (0028,1054))
    ("ct", "CT", CTImageStorage, None, "HU"),
    ("pt", "PT", PositronEmissionTomographyImageStorage, "BQML", None),
    ("mr", "MR", MRImageStorage, None, "US"),
]


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-units-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def build(key: str, modality: str, sop_class: str, units, rescale_type) -> bytes:
    sop = uid("instance", key)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = sop
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.ImplementationClassUID = uid("implementation")

    ds = Dataset()
    ds.file_meta = meta
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = uid("study")
    ds.SeriesInstanceUID = uid("series", key)
    ds.FrameOfReferenceUID = uid("frame-of-reference", key)
    ds.Modality = modality
    ds.PatientName = "PHANTOM^UNITS^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-UNITS"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.SeriesDescription = f"{modality}, identical pixels, unit varies"

    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = ROWS, COLS
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.RescaleSlope = 1
    ds.RescaleIntercept = INTERCEPT
    if rescale_type is not None:
        ds.RescaleType = rescale_type
    if units is not None:
        ds.Units = units
    ds.WindowCenter = 40
    ds.WindowWidth = 400
    ds.PixelSpacing = [0.7, 0.7]
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.PixelData = np.full((ROWS, COLS), STORED, dtype=np.uint16).tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    for key, modality, sop_class, units, rescale_type in VARIANTS:
        blob = build(key, modality, sop_class, units, rescale_type)
        path = f"{out_dir}/units-{key}.dcm"
        with open(path, "wb") as handle:
            handle.write(blob)
        says = units or rescale_type or "nothing"
        print(f"{key}\t{modality}\theader says {says}\t{len(blob)} bytes\t{path}")


if __name__ == "__main__":
    main(sys.argv[1])
