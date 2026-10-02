# SPDX-License-Identifier: Apache-2.0
"""Put a synthetic CT study into a development archive, so a first run shows something.

WHY THIS IS IN `medos/tools/` AND NOT IN THE PLATFORM
-----------------------------------------------
`MOS-IMG-069` forbids the platform to mint a `StudyInstanceUID`. Inside MedicalOS every
object is *derived from* an acquisition, so a study UID is something received and never
invented -- `medos/medos/core/uids.py::derive_uid` has no parameter for one and will not grow
one. A seeder has to invent one, so it lives out here with
`medos/tools/ingest/nrrd_to_dicom.py`, which is out here for exactly the same reason.

WHY THE IMAGES ARE SYNTHETIC, AND WHY THAT IS NOT A COMPROMISE
--------------------------------------------------------------
No patient data may ever ship in this repository. That constraint is not the reason this
phantom exists, though -- a phantom is the only thing that can be seeded *honestly*.
`tests/integration/test_deid_provenance_declaration.py` covers the root
`1.2.826.0.1.3680043.8.498.` (pydicom's registered root) with the recorded justification
"no patient behind them", so a study minted under it needs no corpus manifest and makes
no de-identification claim.

That matters more than it sounds. A manifest's `deidentification_status` comes from the
closed set {identified, pseudonymised, public_deidentified}, and NONE OF THE THREE IS
TRUE OF A VOLUME WITH NO PATIENT BEHIND IT. Seeding under the `2.25.` arc would force
this tool to write one of them into an immutable row that every downstream
`ValidationReport` cites -- a false statement, recorded permanently, to satisfy a check.
Register entry 93 records the same hole. So the root is not a convenience; it is the one
path that does not require lying.

WHAT IT REFUSES
---------------
`MEDOS_ENV` must be exactly `dev`. Not "unset means dev" -- the failure that actually
happens is an operator copying `docker-compose.yml` and setting nothing, and a seeder
that ran under that omission would put fabricated studies into an archive beside real
ones, where nothing downstream could tell them apart. `medos/medos/config/devmode.py` carries
the same inversion for the same reason.

THE GEOMETRY IS NOT ARBITRARY
-----------------------------
It is the INTERSECTION of the two shipped capabilities' declared envelopes, because a
demo whose one study every capability rejects teaches a developer that the platform is
broken:

    lung_nodule (medos/examples/lung-nodule/capability.json)
        n_slices 40-1200, delta_s 0.4-3.0 mm, pixel spacing 0.3-1.0 mm, tilt +/-1 deg
    lung_segmentation (medos/medos/capabilities/lung_segmentation.py)
        MIN_SLICES 16, PLAUSIBLE_TOTAL_ML (1500, 12000)

The existing test phantom in `tests/unit/test_capabilities.py` -- 32 slices at 1.5 mm
in-plane -- is rejected by `lung_nodule` on BOTH slice count and pixel spacing, which is
why this is a new phantom rather than a re-export of that one. It is also why the numbers
below are checked by `assert_inside_both_envelopes()` at build time rather than trusted.

Spec: MOS-IMG-069, MOS-EVID-021, MOS-SAFE-002, MOS-DATA-007.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: Everything this tool mints lives under pydicom's registered root. See the module
#: docstring: this is the only arc for which the declaration in `docker-compose.yml` is
#: true without a manifest.
UID_ROOT = "1.2.826.0.1.3680043.8.498."

#: Seeds the deterministic UIDs. Re-running the seeder produces the SAME study, so
#: `docker compose up` twice does not leave two copies in the archive -- an idempotent
#: seed is the difference between a demo and a slowly filling archive.
NAMESPACE = "medicalos-demo-corpus-v1"


@dataclass(frozen=True)
class Phantom:
    """A thorax-shaped CT volume and the geometry it is written with.

    Sized to the intersection of both shipped capabilities' envelopes; see the module
    docstring. The values are a declaration, and `assert_inside_both_envelopes` is what
    makes them a checked one.
    """

    n_slices: int = 64
    rows: int = 320
    cols: int = 448
    pixel_spacing_mm: float = 0.7
    slice_spacing_mm: float = 2.0

    # The body, inset so that room air -- and only room air -- reaches an in-plane face.
    # `lung_segmentation` step 1 discriminates lung from room air exactly there, so a body
    # touching the edge makes the phantom test nothing.
    body_j: tuple[int, int] = (25, 295)
    body_i: tuple[int, int] = (15, 433)

    # Two lungs, clear of both k-faces so the scan-extent filter keeps them, separated by
    # a 68-column tissue mediastinum so they are two connected components with an
    # identically-zero column profile between them.
    lung_k: tuple[int, int] = (3, 60)
    lung_j: tuple[int, int] = (50, 270)
    right_i: tuple[int, int] = (40, 190)  # low column index == low +x == patient RIGHT
    left_i: tuple[int, int] = (258, 408)

    # A solid sphere in the right lung, 8 mm across: comfortably above lung_nodule's
    # 4 mm floor, so the demo produces a FINDING rather than an empty result. A demo that
    # runs correctly and reports nothing looks identical to a broken one.
    nodule_centre: tuple[int, int, int] = (28, 150, 110)  # (k, j, i)
    nodule_radius_mm: float = 4.0

    hu_air: float = -1000.0
    hu_tissue: float = 20.0
    hu_lung: float = -820.0
    hu_nodule: float = 30.0

    @property
    def voxel_ml(self) -> float:
        return self.pixel_spacing_mm**2 * self.slice_spacing_mm / 1000.0

    @property
    def lung_volume_ml(self) -> float:
        slices = self.lung_k[1] - self.lung_k[0]
        rows = self.lung_j[1] - self.lung_j[0]
        cols = (self.right_i[1] - self.right_i[0]) + (self.left_i[1] - self.left_i[0])
        return slices * rows * cols * self.voxel_ml

    @property
    def mediastinum_columns(self) -> int:
        return self.left_i[0] - self.right_i[1]


#: `lung_nodule`'s declared constraints, transcribed from medos/examples/lung-nodule/
#: capability.json. Transcribed rather than loaded so that a change to the capability
#: makes this file's test fail and a human reads both.
NODULE_ENVELOPE = {
    "n_slices": (40, 1200),
    "delta_s_mm": (0.4, 3.0),
    "pixel_spacing_max_mm": (0.3, 1.0),
    "tilt_deg": (-1.0, 1.0),
}
#: `lung_segmentation`'s two that bear on geometry.
SEGMENTATION_MIN_SLICES = 16
SEGMENTATION_PLAUSIBLE_TOTAL_ML = (1500.0, 12000.0)


def assert_inside_both_envelopes(p: Phantom) -> None:
    """Refuse to build a phantom that either shipped capability would reject.

    Checked here rather than left to a test, because this tool is also run by hand and a
    seeded study that every capability refuses is the single most confusing thing a first
    run could produce: the stack is healthy, the study is there, and every job comes back
    `outside_applicability_envelope`.
    """
    problems: list[str] = []
    lo, hi = NODULE_ENVELOPE["n_slices"]
    if not lo <= p.n_slices <= hi:
        problems.append(f"n_slices {p.n_slices} outside lung_nodule's [{lo}, {hi}]")
    lo, hi = NODULE_ENVELOPE["delta_s_mm"]
    if not lo <= p.slice_spacing_mm <= hi:
        problems.append(f"slice spacing {p.slice_spacing_mm} outside [{lo}, {hi}]")
    lo, hi = NODULE_ENVELOPE["pixel_spacing_max_mm"]
    if not lo <= p.pixel_spacing_mm <= hi:
        problems.append(f"pixel spacing {p.pixel_spacing_mm} outside [{lo}, {hi}]")
    if p.n_slices < SEGMENTATION_MIN_SLICES:
        problems.append(f"n_slices {p.n_slices} below lung_segmentation's MIN_SLICES")
    lo, hi = SEGMENTATION_PLAUSIBLE_TOTAL_ML
    if not lo <= p.lung_volume_ml <= hi:
        problems.append(
            f"lung volume {p.lung_volume_ml:.0f} ml outside PLAUSIBLE_TOTAL_ML "
            f"[{lo:.0f}, {hi:.0f}] -- the absurdity gate would reject this study"
        )
    if p.mediastinum_columns < 32:
        problems.append(
            f"mediastinum is {p.mediastinum_columns} columns; the lungs may merge into "
            f"one connected component and the laterality split becomes meaningless"
        )
    if p.lung_k[0] < 1 or p.lung_k[1] > p.n_slices - 1:
        problems.append("lung touches a k-face; the scan-extent filter would drop it")
    if problems:
        raise SystemExit(
            "the demo phantom is outside a shipped capability's envelope:\n  "
            + "\n  ".join(problems)
        )


def build_volume(p: Phantom) -> np.ndarray:
    """`(k, j, i)` float32 HU. Room air, a soft-tissue body, two lungs, one nodule."""
    hu = np.full((p.n_slices, p.rows, p.cols), p.hu_air, dtype=np.float32)
    hu[:, p.body_j[0] : p.body_j[1], p.body_i[0] : p.body_i[1]] = p.hu_tissue
    for i0, i1 in (p.right_i, p.left_i):
        hu[
            p.lung_k[0] : p.lung_k[1],
            p.lung_j[0] : p.lung_j[1],
            i0:i1,
        ] = p.hu_lung

    ck, cj, ci = p.nodule_centre
    rk = max(1, int(round(p.nodule_radius_mm / p.slice_spacing_mm)))
    rj = max(1, int(round(p.nodule_radius_mm / p.pixel_spacing_mm)))
    kk, jj, ii = np.ogrid[
        -rk : rk + 1,
        -rj : rj + 1,
        -rj : rj + 1,
    ]
    ball = (
        (kk * p.slice_spacing_mm) ** 2
        + (jj * p.pixel_spacing_mm) ** 2
        + (ii * p.pixel_spacing_mm) ** 2
    ) <= p.nodule_radius_mm**2
    ks, js, iss = np.where(ball)
    hu[ks + ck - rk, js + cj - rj, iss + ci - rj] = p.hu_nodule
    return hu


def _uid(*parts: str) -> str:
    from pydicom.uid import generate_uid

    return generate_uid(prefix=UID_ROOT, entropy_srcs=[NAMESPACE, *parts])


def build_series(
    p: Phantom,
    hu: np.ndarray,
    *,
    variant: str = "",
    z_offset_mm: float = 0.0,
    y_offset_mm: float = 0.0,
    slices: int | None = None,
) -> tuple[list[tuple[str, bytes]], str, str]:
    """Part-10 bytes per slice. UIDs are deterministic, so re-seeding is idempotent.

    `variant` salts the SERIES and instance UIDs and neither the study nor the frame of
    reference. That is the arrangement every cross-series feature in the viewer is about --
    position linking, reference lines, and the refusals that fire when the arrangement does
    NOT hold -- and it is the one the surface can actually open, because `openStudy` takes
    one StudyInstanceUID and lays out a panel per image series of it. With a single series
    in the archive, `source === target` in every panel pair and none of those paths had ever
    run against real data.

    `z_offset_mm` shifts the companion along the slice axis. Deliberately NOT a multiple of
    the pitch: at 7 mm on a 2 mm series the nearest slice is 3.5 slices away, so a reader
    scrolling one panel exercises the rounding rather than an exact hit, and an off-by-one
    in the search or in the affine inversion shows up as a visible half-slice rather than
    as nothing.

    `y_offset_mm` shifts it IN PLANE, and is here because the first companion did not have
    it and that made two tests vacuous. With a shared in-plane origin the two studies have
    the same coronal and sagittal extent row for row, so linking a coronal to a coronal
    returned the source index unchanged -- the identity, which every implementation passes
    including one that ignores the geometry -- and no reconstructed plane of either could
    fall outside the other, so the branch that must refuse to claim a correspondence was
    unreachable. Two acquisitions of one patient do not agree in plane either: the table
    height and the patient's position between them differ. 5 mm on a 0.7 mm row pitch is
    7.14 rows, so it is not a whole number of rows and the rounding is exercised too.
    """
    import io

    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

    # THE STUDY IS SHARED AND THE SERIES IS NOT, and that asymmetry is the whole point.
    #
    # The companion was first written as a second STUDY, which is the more obvious reading
    # of "two acquisitions of one patient" and is useless here: `app.js::openStudy` takes
    # ONE StudyInstanceUID, lists that study's series and lays a panel out per image series.
    # A second study is not reachable from it at all, so the corpus that was built to make
    # the cross-series paths exercisable could not exercise them through the surface. The
    # viewer says what it expects in its own words -- "a study that really carries several
    # image series -- the T1/T2/STIR case -- opens with one each, which is the whole point
    # of the link" -- and that is the arrangement this builds.
    #
    # It is also the commoner one in practice: pre- and post-contrast, a thin and a thick
    # reconstruction, or a repeat for motion all live in one study by definition.
    study_uid = _uid("study")
    series_uid = _uid("series", variant) if variant else _uid("series")
    # SHARED, never salted. Two series that state the same (0020,0052) assert that their
    # positions are in one coordinate system, which is what makes them comparable at all.
    frame_uid = _uid("frame-of-reference")
    ps, dz = p.pixel_spacing_mm, p.slice_spacing_mm
    count = p.n_slices if slices is None else min(slices, p.n_slices)

    out: list[tuple[str, bytes]] = []
    for k in range(count):
        sop_uid = _uid("instance", variant, str(k)) if variant else _uid("instance", str(k))
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.ImplementationClassUID = _uid("implementation")

        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        # THERE IS NO PATIENT. The identity tags carry surrogates that say so in the
        # value itself, so anyone who greps the archive, reads a log or opens the viewer
        # sees immediately that this is not a person. An empty PatientName would be
        # ambiguous with a badly de-identified real study; this cannot be mistaken.
        ds.PatientID = "MEDOS-DEMO-PHANTOM"
        ds.PatientName = "PHANTOM^SYNTHETIC^NOT^A^PATIENT"
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        # STUDY-LEVEL AND THEREFORE IDENTICAL. These were briefly salted per variant, from
        # when the companion was its own study; two series of one study that disagree on
        # them are a malformed study, and an archive is entitled to reconcile the conflict
        # however it likes.
        ds.AccessionNumber = "MEDOSDEMO1"
        ds.StudyID = "DEMO1"
        ds.StudyDate = "20200101"
        ds.StudyTime = "120000"
        ds.SeriesDate = "20200101"
        ds.SeriesTime = "120000"
        ds.StudyDescription = "MedicalOS synthetic demo phantom"
        ds.SeriesDescription = (
            "Synthetic thorax, companion acquisition +7 mm" if variant
            else "Synthetic thorax, two lungs and one 8 mm nodule"
        )
        ds.InstitutionName = "SYNTHETIC"
        ds.Manufacturer = "MedicalOS"
        ds.ManufacturerModelName = "medos/tools/demo/seed_corpus.py"
        # MOS-SAFE-002: nothing here was acquired from a person and nothing derived from
        # it is a clinical claim. Stated on the object so it survives export.
        ds.ImageComments = "SYNTHETIC PHANTOM -- NOT A PATIENT -- NOT FOR CLINICAL USE"

        ds.SeriesNumber = 2 if variant else 1
        ds.InstanceNumber = k + 1
        ds.Rows = p.rows
        ds.Columns = p.cols
        ds.PixelSpacing = [ps, ps]
        ds.SliceThickness = dz
        ds.SpacingBetweenSlices = dz
        # Axial, no tilt: the envelope allows +/-1 degree and 0 is inside it.
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.ImagePositionPatient = [
            -(p.cols - 1) * ps / 2.0,
            -(p.rows - 1) * ps / 2.0 + y_offset_mm,
            k * dz + z_offset_mm,
        ]
        ds.SliceLocation = k * dz + z_offset_mm
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        # (0008,0008) is Type 1 for CT Image Storage and every real acquisition carries it.
        # The demo corpus omitted it, which is not cosmetic: MOS-DATA-041 lets an
        # ORIGINAL\PRIMARY series be presumed free of burned-in text without screening, so
        # an object that says nothing about its own type cannot take that presumption. The
        # viewer correctly warned about every phantom study until this was set — the corpus
        # was the unrealistic thing, not the warning.
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleSlope = 1.0
        ds.RescaleIntercept = -1024.0
        # `lung_nodule` refuses a series whose Modality LUT was not applied: stored values
        # are not HU, and thresholding those gives a number for every case and a correct
        # one for none.
        ds.RescaleType = "HU"
        ds.PixelData = np.clip(hu[k] + 1024.0, 0, 65535).astype(np.uint16).tobytes()

        buffer = io.BytesIO()
        pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
        out.append((sop_uid, buffer.getvalue()))
    return out, study_uid, series_uid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed a development archive with one synthetic CT study."
    )
    parser.add_argument(
        "--gateway",
        default=os.environ.get("MEDOS_GATEWAY_BASE", "http://medos-gateway:8043"),
        help="the gateway's DICOMweb root; the gateway is the only component permitted "
        "to hold a PACS credential (MOS-DATA-006), so seeding goes through it",
    )
    parser.add_argument(
        "--tenant",
        # THE TENANT UUID, NOT A NAME. MOS-DATA-009 makes the gateway compare the path
        # segment against the authenticated principal's tenant, so a friendly string
        # like "default" answers 403 TENANT_MISMATCH -- which reads like a permissions
        # problem and is really a spelling one. This matches MEDOS_TENANT_ID in
        # docker-compose.yml and the tenant 0002_tenancy.up.sql seeds.
        default=os.environ.get(
            "MEDOS_TENANT_ID", "00000000-0000-0000-0000-000000000000"
        ),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MEDOS_DICOMWEB_TOKEN", ""),
        help="bearer token for the gateway",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="write the Part-10 files here instead of STOWing them",
    )
    parser.add_argument(
        "--companion",
        action="store_true",
        help="also seed a SECOND SERIES in the same study, sharing its frame of "
        "reference and offset 7 mm along the slice axis and 5 mm in plane. Off by "
        "default, because the e2e suite asserts against the corpus this tool produces "
        "and a second series would change what it finds. On, it is what makes the "
        "viewer's cross-series paths reachable at all: the study opens with one panel "
        "per image series, so position linking, reference lines between panels and the "
        "marking of a measurement taken on the series that is not on screen all become "
        "things a reader can actually do.",
    )
    parser.add_argument(
        "--pet",
        action="store_true",
        help="also seed a co-registered PET series in the same study: 128x128 at 2.8 mm "
        "over 32 slices of 4 mm, against the CT's 320x448 at 0.7 mm over 64 of 2 mm. Off "
        "by default for the same reason as --companion -- the e2e suite asserts against "
        "the corpus this tool produces. On, it is the only thing in this archive that a "
        "fusion display can be drawn against, and the differing grid is deliberate: a "
        "PET written on the CT's own grid lets an overlay that ignores patient "
        "coordinates look correct.",
    )
    parser.add_argument(
        "--force-outside-dev",
        action="store_true",
        help=argparse.SUPPRESS,  # see the refusal below; deliberately undocumented
    )
    args = parser.parse_args(argv)

    from medos.config.devmode import ENV_VAR, is_dev_deployment

    if not is_dev_deployment() and not args.force_outside_dev:
        print(
            f"REFUSING: {ENV_VAR} is {os.environ.get(ENV_VAR, '<unset>')!r}, not 'dev'.\n"
            f"\n"
            f"This tool fabricates a study and puts it in an archive. On a deployment "
            f"holding patients that study would sit beside real ones with nothing "
            f"downstream able to tell them apart, and MOS-EVID-021 would record its "
            f"provenance in a row that is never updated.\n"
            f"\n"
            f"Unset is not dev. The failure this guards against is not somebody typing "
            f"{ENV_VAR}=dev on a clinical system; it is somebody copying "
            f"docker-compose.yml and setting nothing.",
            file=sys.stderr,
        )
        return 2

    phantom = Phantom()
    assert_inside_both_envelopes(phantom)
    print(
        f"phantom: {phantom.n_slices} slices, {phantom.rows}x{phantom.cols} at "
        f"{phantom.pixel_spacing_mm} mm, dS {phantom.slice_spacing_mm} mm, "
        f"lungs {phantom.lung_volume_ml:.0f} ml"
    )
    volume = build_volume(phantom)
    payloads, study_uid, series_uid = build_series(phantom, volume)
    print(f"study  : {study_uid}")
    print(f"series : {series_uid}  ({len(payloads)} instances)")

    if args.out_dir is not None:
        _write_files(args.out_dir, payloads)
        # `--companion` WITH `--out-dir` USED TO RETURN HERE, writing one study and
        # reporting success. A flag that is accepted, silently does nothing and exits 0 is
        # worse than one that is rejected: the corpus looks seeded. Both studies land, in
        # their own directories, because they share slice filenames.
        if args.companion:
            companion, c_study, c_series = build_series(
                phantom, volume, variant="companion",
                z_offset_mm=COMPANION_OFFSET_MM, y_offset_mm=COMPANION_Y_OFFSET_MM,
                slices=COMPANION_SLICES,
            )
            print(f"study  : {c_study}  (companion, +{COMPANION_OFFSET_MM:g} mm)")
            print(f"series : {c_series}  ({len(companion)} instances)")
            _write_files(args.out_dir / "companion", companion)
        if args.pet:
            pet, p_study, p_series = build_pet_series(phantom)
            print(f"study  : {p_study}  (the same study, PET)")
            print(f"series : {p_series}  ({len(pet)} instances)")
            _write_files(args.out_dir / "pet", pet)
        return 0

    base = f"{args.gateway.rstrip('/')}/dicomweb/{args.tenant}"
    failed = _stow(base, args, payloads, study_uid, series_uid)
    if failed:
        return failed

    if args.companion:
        failed = _seed_companion(phantom, volume, base, args)
        if failed:
            return failed
    if args.pet:
        return _seed_pet(phantom, base, args)
    return 0


def _stow(
    base: str,
    args: Any,
    payloads: list[tuple[str, bytes]],
    study_uid: str,
    series_uid: str,
) -> int:
    """Put one series into the archive if it is not already there. 0 on success.

    ONE PATH FOR BOTH STUDIES, and that is the whole reason it is a function. The main
    flow used to inline this and `return 0` from inside the already-present branch, so
    `--companion` against an archive that already held the first study printed "already
    seeded: 64 instances present, nothing to do" and exited 0 WITHOUT SEEDING THE
    COMPANION. That is the second time this flag has been silently dropped by an early
    return -- `--out-dir` did the same -- and a flag that is accepted, does nothing and
    reports success is worse than one that is rejected, because the corpus looks seeded.

    Idempotent by construction: the UIDs are derived, so a second run posts the same
    SOPInstanceUIDs and the origin replaces rather than duplicates. The check is still
    worth making -- it turns the common case into one QIDO round trip instead of sixty-four
    stores -- it just must not decide whether the REST of the run happens.
    """
    from medos.dicomweb.client import DicomWebClient

    with DicomWebClient(base, bearer_token=args.token or None) as client:
        try:
            existing = client.instance_uids(study_uid, series_uid)
        except Exception:  # noqa: BLE001 - an empty archive answers many ways
            existing = set()
        if len(existing) >= len(payloads):
            print(f"present: {len(existing)} instances already in {base}")
            return 0
        result = client.stow(payloads, study_instance_uid=study_uid)
    if not result.ok:
        print(f"STOW did not fully succeed: {result}", file=sys.stderr)
        return 1
    print(f"seeded : {len(payloads)} instances into {base}")
    return 0


def _write_files(directory: Path, payloads: list[tuple[str, bytes]]) -> None:
    """One slice per file, named by index so `ls` sorts into acquisition order."""
    directory.mkdir(parents=True, exist_ok=True)
    for index, (_, blob) in enumerate(payloads):
        (directory / f"slice{index:04d}.dcm").write_bytes(blob)
    print(f"wrote  : {directory}")


#: How far the companion sits along the slice axis, and why it is not a round multiple.
#: The pitch is 2.0 mm, so 7.0 mm is three and a half slices: a reader scrolling the first
#: study lands the second BETWEEN two of its slices, and the link has to round. An exact
#: multiple would pass whether the rounding were right, wrong or absent.
COMPANION_OFFSET_MM = 7.0

#: The in-plane shift, and why there is one at all. See `build_series`: without it the two
#: studies share every coronal and sagittal ordinate, so a coronal-to-coronal link is the
#: identity map and the reconstructed out-of-range branch cannot be reached. 5 mm is 7.14
#: rows at 0.7 mm, so it is not a whole number of rows either.
COMPANION_Y_OFFSET_MM = 5.0

#: Fewer slices than the source, so an index carried across unchanged is visibly wrong
#: rather than coincidentally right.
COMPANION_SLICES = 40

# =====================================================================================
# A CO-REGISTERED PET SERIES, ON A DELIBERATELY DIFFERENT GRID.
#
# WHY THE GRID DIFFERS AND WHY THAT IS THE POINT. Fusion draws two acquisitions in one
# viewport, and the only thing that makes it correct is that both are sampled in PATIENT
# coordinates rather than index for index. A PET written on the CT's own grid would let
# an implementation that simply overlays `pet[k][j][i]` on `ct[k][j][i]` look perfect --
# and be wrong on every real PET/CT, where the PET is coarser in plane and thicker
# through it. These numbers are a real scanner's shape rather than a convenience:
#
#     CT   320 x 448 @ 0.7 mm,  64 slices @ 2 mm     (the phantom above)
#     PET  128 x 128 @ 2.8 mm,  32 slices @ 4 mm
#
# Both are centred on the same patient origin and share the study's FrameOfReferenceUID,
# so they ARE co-registered -- the resampling is the whole of the work, and an off-by-one
# in it puts the hot spot somewhere the nodule is not.
#
# THE VALUES ARE NOT HU AND MUST NOT BE READ AS THEM. (0054,1001) says BQML, which
# `src/image/units.js` already reads: "on a PET the stored values are a concentration
# whose unit the header states in (0054,1001)". A window preset carrying `"unit": "HU"`
# is refused against this series by machinery that already exists.
# =====================================================================================
PET_MATRIX = 128
PET_PIXEL_MM = 2.8
PET_SLICE_MM = 4.0
PET_SLICES = 32

#: Background concentration in the body, and the nodule's, in BQML. The ratio is what a
#: reader looks at, not the absolute number: a lesion that does not stand out of its
#: background demonstrates nothing about a fusion display.
PET_BACKGROUND_BQML = 1200.0
PET_LESION_BQML = 24000.0

#: The hot spot's radius. LARGER THAN THE NODULE ON PURPOSE -- 6 mm against the CT's 4 mm
#: -- because PET's resolution is worse than CT's and a synthetic pair in which the two
#: agree exactly would hide the one thing a reader uses fusion FOR: deciding whether the
#: uptake belongs to the thing they can see on the CT.
PET_LESION_RADIUS_MM = 6.0


def build_pet_series(p: Phantom) -> tuple[list[tuple[str, bytes]], str, str]:
    """A PET acquisition of the same phantom, in the same study and coordinate system.

    THE GEOMETRY IS THE TEST. See the constants above for why the grid differs from the
    CT's; what this function has to get right is that the two describe the same patient.
    Both volumes are centred on x = y = 0 and start at z = 0, so a point in patient
    millimetres lands in the same place in each -- and a fusion display that resamples
    correctly puts the uptake on the nodule, while one that does not puts it elsewhere by
    a distance a reader can see.

    THE VALUES ARE A CONCENTRATION, in BQML, stated on the object. Nothing here is HU and
    nothing may read it as HU.
    """
    from pydicom import Dataset, FileMetaDataset
    import pydicom
    from pydicom.uid import ExplicitVRLittleEndian, PositronEmissionTomographyImageStorage
    import io

    study_uid = _uid("study")
    series_uid = _uid("series", "pet")
    # THE CT'S OWN FRAME OF REFERENCE, spelled exactly as `build_series` spells it. The
    # first version of this line said `_uid("frame")` and produced a DIFFERENT uid --
    # silently, because both are well-formed. The two series then asserted no shared
    # coordinate system, `positionLinkable` would have refused them, and a fusion drawn on
    # top would have been claiming a correspondence the headers denied. Caught by reading
    # the written files back rather than by reading this function.
    frame_uid = _uid("frame-of-reference")

    # The nodule, in patient millimetres, from the CT phantom's own index geometry. Read
    # from `p` rather than restated, so moving the nodule moves the uptake with it.
    k0, j0, i0 = p.nodule_centre
    ct_x0 = -(p.cols - 1) * p.pixel_spacing_mm / 2.0
    ct_y0 = -(p.rows - 1) * p.pixel_spacing_mm / 2.0
    lesion = (
        ct_x0 + i0 * p.pixel_spacing_mm,
        ct_y0 + j0 * p.pixel_spacing_mm,
        k0 * p.slice_spacing_mm,
    )

    pet_x0 = -(PET_MATRIX - 1) * PET_PIXEL_MM / 2.0
    pet_y0 = pet_x0

    # The body, as a box in patient millimetres, so the background sits inside the patient
    # rather than filling the room. Taken from the CT phantom's own body extent.
    body_x = (ct_x0 + p.body_i[0] * p.pixel_spacing_mm, ct_x0 + p.body_i[1] * p.pixel_spacing_mm)
    body_y = (ct_y0 + p.body_j[0] * p.pixel_spacing_mm, ct_y0 + p.body_j[1] * p.pixel_spacing_mm)

    xs = pet_x0 + np.arange(PET_MATRIX) * PET_PIXEL_MM
    ys = pet_y0 + np.arange(PET_MATRIX) * PET_PIXEL_MM
    gx, gy = np.meshgrid(xs, ys)   # (row, col) == (y, x)

    inside = (
        (gx >= body_x[0]) & (gx <= body_x[1]) & (gy >= body_y[0]) & (gy <= body_y[1])
    )

    out: list[tuple[str, bytes]] = []
    for k in range(PET_SLICES):
        z = k * PET_SLICE_MM
        bq = np.where(inside, PET_BACKGROUND_BQML, 0.0)
        r2 = (gx - lesion[0]) ** 2 + (gy - lesion[1]) ** 2 + (z - lesion[2]) ** 2
        bq = np.where(r2 <= PET_LESION_RADIUS_MM ** 2, PET_LESION_BQML, bq)

        sop_uid = _uid("instance", "pet", str(k))
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = PositronEmissionTomographyImageStorage
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.ImplementationClassUID = _uid("implementation")

        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID = PositronEmissionTomographyImageStorage
        ds.SOPInstanceUID = sop_uid
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        # THE SAME FRAME OF REFERENCE AS THE CT. Without it the two series assert no
        # shared coordinate system, `positionLinkable` refuses them, and a fusion built on
        # top would be claiming a correspondence the headers do not support.
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "PT"
        ds.PatientID = "MEDOS-DEMO-PHANTOM"
        ds.PatientName = "PHANTOM^SYNTHETIC^NOT^A^PATIENT"
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.AccessionNumber = "MEDOSDEMO1"
        ds.StudyID = "DEMO1"
        ds.StudyDate = "20200101"
        ds.StudyTime = "120000"
        ds.SeriesDate = "20200101"
        ds.SeriesTime = "120000"
        ds.StudyDescription = "MedicalOS synthetic demo phantom"
        ds.SeriesDescription = "Synthetic PET, co-registered with the CT"
        ds.InstitutionName = "SYNTHETIC"
        ds.Manufacturer = "MedicalOS"
        ds.ManufacturerModelName = "medos/tools/demo/seed_corpus.py"
        ds.ImageComments = "SYNTHETIC PHANTOM -- NOT A PATIENT -- NOT FOR CLINICAL USE"

        ds.SeriesNumber = 3
        ds.InstanceNumber = k + 1
        ds.Rows = PET_MATRIX
        ds.Columns = PET_MATRIX
        ds.PixelSpacing = [PET_PIXEL_MM, PET_PIXEL_MM]
        ds.SliceThickness = PET_SLICE_MM
        ds.SpacingBetweenSlices = PET_SLICE_MM
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.ImagePositionPatient = [pet_x0, pet_y0, z]
        ds.SliceLocation = z
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0

        # A SLOPE THAT USES THE RANGE RATHER THAN WASTING IT. Stored values are 16-bit
        # unsigned; the slope carries them to BQML. Chosen so the lesion lands well short
        # of 65535 -- a phantom that clips at its own brightest point would teach a reader
        # that the scale is saturated when it is the seeder that is.
        slope = PET_LESION_BQML / 40000.0
        ds.RescaleSlope = slope
        ds.RescaleIntercept = 0.0
        # (0054,1001). `src/image/units.js` reads this FIRST, before RescaleType and before
        # the modality default, which is why a PET value never comes out labelled HU.
        ds.Units = "BQML"
        ds.PixelData = np.clip(bq / slope, 0, 65535).astype(np.uint16).tobytes()

        buffer = io.BytesIO()
        pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
        out.append((sop_uid, buffer.getvalue()))
    return out, study_uid, series_uid


def _seed_pet(p: Phantom, base: str, args: Any) -> int:
    """The PET half of the phantom, so fusion has something true to be drawn against."""
    payloads, study_uid, series_uid = build_pet_series(p)
    print(f"study  : {study_uid}  (the same study)")
    print(f"series : {series_uid}  ({len(payloads)} PET instances, "
          f"{PET_MATRIX}x{PET_MATRIX} @ {PET_PIXEL_MM:g} mm, {PET_SLICE_MM:g} mm slices)")
    return _stow(base, args, payloads, study_uid, series_uid)


def _seed_companion(p: Phantom, volume: np.ndarray, base: str, args: Any) -> int:
    """A second series of one phantom, in the same study and coordinate system.

    Everything the viewer does ACROSS series needs two of them, and the archive has held
    one -- so `source === target` in every panel pair and the cross-stack branch of
    `sync.followIndex` has never run against a real stack. This is the smallest corpus
    change that makes it reachable.
    """
    payloads, study_uid, series_uid = build_series(
        p, volume,
        variant="companion",
        z_offset_mm=COMPANION_OFFSET_MM,
        y_offset_mm=COMPANION_Y_OFFSET_MM,
        slices=COMPANION_SLICES,
    )
    print(f"study  : {study_uid}  (the same study)")
    print(f"series : {series_uid}  ({len(payloads)} companion instances, "
          f"+{COMPANION_OFFSET_MM:g} mm along the slice axis, "
          f"+{COMPANION_Y_OFFSET_MM:g} mm in plane)")
    return _stow(base, args, payloads, study_uid, series_uid)


if __name__ == "__main__":
    raise SystemExit(main())
