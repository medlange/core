# SPDX-License-Identifier: Apache-2.0
"""A paired control for display polarity, and the first brick of the owed harness.

`docs/adr/BUILD_VS_ADOPT.md` records that a rendering-correctness harness is owed. This is
the smallest useful piece of one, and it exists because a structural test cannot see the
defect it was written for: the viewer read every attribute it needed to draw a CT and did
not read (0028,0004), so MONOCHROME1 studies -- most CR, DX and MG -- rendered as their
own negatives. Bone black, air white, nothing broken-looking.

WHY A PAIR RATHER THAN A SINGLE IMAGE
---------------------------------------
Two instances with BYTE-IDENTICAL pixel data differing only in PhotometricInterpretation.
A viewer that reads the attribute renders them as negatives of each other; a viewer that
ignores it renders them identically. That is a difference no amount of window/level
confusion can fake, and it needs no reference image to compare against -- the control IS
the other member of the pair.

WHY A RAMP
-----------
Stored values run 0 on the left to 4095 on the right, so the reading is a direction rather
than a threshold. Sampling one row gives a monotonic sequence whose SIGN is the answer:

    MONOCHROME1 as stored    252 -> 239 -> 229 -> 220 -> 207     descending, min = white
    MONOCHROME2 as stored      3 ->  16 ->  26 ->  35 ->  48     ascending,  min = black

Sample well clear of the bright bar -- an earlier run of this probe sampled straight into
it, reported the polarity backwards, and the raw numbers are what caught it.

THERE IS NO PATIENT. Identity tags carry surrogates that say so in the value itself,
matching `medos/tools/demo/seed_corpus.py`. Nothing here is derived from clinical data.
"""
import hashlib
import io
import sys

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import DigitalXRayImageStorageForPresentation, ExplicitVRLittleEndian

ROOT = "1.2.826.0.1.3680043.8.498."

def uid(*parts):
    h = hashlib.sha256(("medos-mono1-probe/" + "/".join(parts)).encode()).hexdigest()
    return ROOT + str(int(h[:24], 16))[:28]

def build() -> list[tuple[str, str, bytes]]:
    """The pair, as `(tag, SeriesInstanceUID, bytes)`. Same order, same constants.

    Lifted out of module scope so this file can be IMPORTED. It could not be: the
    package carries an `__init__.py`, and every line below ran at import time with
    `sys.argv[1]` read on the way out, so `import tests._support.polarity_probe`
    raised `IndexError`. Nine sibling probes already had this shape.
    """
    ROWS = COLS = 256
    ramp = np.tile(np.linspace(0, 4095, COLS, dtype=np.uint16), (ROWS, 1))
    # A bright bar at a known place, so "which end is white" has a second witness.
    ramp[100:140, 20:60] = 4095

    polarities = [("MONOCHROME1", "mono1"), ("MONOCHROME2", "mono2")]
    study = uid("study")
    out = []
    for photometric, tag in polarities:
        series = uid("series", tag)
        sop = uid("instance", tag)
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = DigitalXRayImageStorageForPresentation
        meta.MediaStorageSOPInstanceUID = sop
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.ImplementationClassUID = uid("implementation")

        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID = DigitalXRayImageStorageForPresentation
        ds.SOPInstanceUID = sop
        ds.StudyInstanceUID = study
        ds.SeriesInstanceUID = series
        ds.FrameOfReferenceUID = uid("frame")
        ds.Modality = "DX"
        ds.PatientName = "PHANTOM^POLARITY^NOT^A^PATIENT"
        ds.PatientID = "MEDOS-PROBE-POLARITY"
        ds.PatientBirthDate = ""
        ds.PatientSex = "O"
        ds.StudyDate = "20200101"
        ds.StudyTime = "000000"
        ds.StudyID = "1"
        ds.AccessionNumber = ""
        ds.SeriesNumber = 1 if tag == "mono1" else 2
        ds.InstanceNumber = 1
        ds.StudyDescription = "MONOCHROME1 polarity probe (synthetic, not a person)"
        ds.SeriesDescription = f"ramp 0..4095 left-to-right, {photometric}"

        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = photometric
        ds.Rows, ds.Columns = ROWS, COLS
        ds.BitsAllocated = 16
        ds.BitsStored = 12
        ds.HighBit = 11
        ds.PixelRepresentation = 0                 # unsigned; volume.js defaults to SIGNED
        ds.RescaleSlope = 1
        ds.RescaleIntercept = 0
        ds.WindowCenter = 2048
        ds.WindowWidth = 4096
        ds.PixelSpacing = [0.2, 0.2]
        ds.ImagePositionPatient = [0.0, 0.0, 0.0]
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.PixelData = ramp.tobytes()

        buf = io.BytesIO()
        pydicom.dcmwrite(buf, ds, enforce_file_format=True)
        out.append((tag, series, buf.getvalue()))

    return out


def main(out_dir: str) -> None:
    for tag, series, blob in build():
        p = f"{sys.argv[1]}/polarity-{tag}.dcm"
        open(p, "wb").write(blob)
        print(f"{tag}\t{series}\t{len(blob)} bytes\t{p}")
    print("STUDY", uid("study"))


if __name__ == "__main__":
    main(sys.argv[1])
