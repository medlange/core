# SPDX-License-Identifier: Apache-2.0
"""A CT whose out-of-field area is padded, and the same image without the declaration.

(0028,0120) PixelPaddingValue marks pixels that carry no measurement — on CT, the area
outside the reconstruction circle. An ROI that overlaps that area and averages it has its
mean pulled toward a number no scanner produced, and nothing on screen says so.

THE PAIR
---------
Identical pixels in both members: a disc of tissue at stored 1044 (20 HU against intercept
−1024) on a background at stored 0 (−1024 HU). One declares (0028,0120) = 0 and the other
declares nothing. An ROI covering roughly half tissue and half background therefore reads:

    declared      20 HU, with the padded pixels named and excluded
    undeclared    about −500 HU, which is the average of tissue and not-tissue

The second number is arithmetically correct and describes nothing. It is also the shape of
a real reading error: the value looks like fat or fluid, and a reader with no reason to
doubt it has no way to tell it apart from a genuine measurement.

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
SIZE = 64
TISSUE_STORED = 1044          # 20 HU against intercept -1024
PADDING_STORED = 0            # -1024 HU, the out-of-field value
DISC_RADIUS = 20


def uid(*parts: str) -> str:
    h = hashlib.sha256(("medos-padding-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]


def build(declare_padding: bool) -> bytes:
    key = "declared" if declare_padding else "undeclared"
    sop = uid("instance", key)
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
    ds.SeriesInstanceUID = uid("series", key)
    ds.FrameOfReferenceUID = uid("frame-of-reference", key)
    ds.Modality = "CT"
    ds.PatientName = "PHANTOM^PADDING^NOT^A^PATIENT"
    ds.PatientID = "MEDOS-PROBE-PADDING"
    ds.PatientBirthDate = ""
    ds.PatientSex = "O"
    ds.StudyDate = "20200101"
    ds.StudyTime = "000000"
    ds.StudyID = "1"
    ds.AccessionNumber = ""
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.SeriesDescription = f"disc on padded background, padding {key}"

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
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    if declare_padding:
        ds.PixelPaddingValue = PADDING_STORED

    y, x = np.ogrid[:SIZE, :SIZE]
    # The disc sits left of centre, so an ROI centred on the image straddles its edge.
    inside = (x - 22) ** 2 + (y - 32) ** 2 <= DISC_RADIUS ** 2
    pixels = np.where(inside, TISSUE_STORED, PADDING_STORED).astype(np.uint16)
    ds.PixelData = pixels.tobytes()

    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def main(out_dir: str) -> None:
    for declare in (True, False):
        key = "declared" if declare else "undeclared"
        blob = build(declare)
        path = f"{out_dir}/padding-{key}.dcm"
        with open(path, "wb") as handle:
            handle.write(blob)
        says = f"(0028,0120) = {PADDING_STORED}" if declare else "no padding attribute"
        print(f"{key}\t{says}\t{len(blob)} bytes\t{path}")


if __name__ == "__main__":
    main(sys.argv[1])
