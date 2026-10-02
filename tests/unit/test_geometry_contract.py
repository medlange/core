# SPDX-License-Identifier: Apache-2.0
"""Every branch of the geometry contract, including the eight rejections nothing else ran.

MOVED OUT OF `spikes/week0/test_contracts.py::run_geometry_tests`, WHICH WAS THE ONLY
PLACE THEY EXISTED. Measured before the move, by grepping `tests/` for each member of
`MOS-IMG-010`'s closed enum: three of the eleven rejection codes appeared somewhere under
`tests/`; **eight appeared only in the spike**. Deleting `spikes/` without this file would
have removed eight executed rejection paths from a medical imaging platform and left the
suite green, which is the failure the spike's own docstring names:

    "a rejection path that has never executed is a rejection path that does not work"

WHY IT WAS EASY TO MISS. The spike was not dead code: `.github/workflows/tests.yml` ran
`python spikes/week0/test_contracts.py` in two separate steps, so these checks did run in
CI -- as a script, outside pytest, reporting `32/32 passed` to a JSON file rather than to
the suite. `tests/unit/test_suite_layout.py` knows about it and `tests/README.md` explains
it. What none of that survives is a directory being deleted: a script CI invokes by path
is a test nobody's coverage tool counts and nobody's collection error catches.

It had also stopped working. `spikes/week0/build_volume.py` does
`sys.path.insert(0, parents[2])` to import `medos.core` from a bare checkout, and `medos`
moved to `medos/medos/` when the platform became a product directory. From that commit
until this one, both CI steps would have died at `ModuleNotFoundError: No module named
'medos.core'` -- and the two of them are the only reason anyone would have noticed.

WHAT CHANGED IN THE MOVE. The fixture builder and every assertion are the spike's, with
the closures turned into test functions and `check(name, fn)` turned into the docstring
that name was. `expect_rejection` keeps its shape. Nothing about what is asserted moved.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import numpy as np
from medos.core.errors import GeometryRejection
from medos.core.geometry import EPS_AFFINE, build_canonical_volume
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, RTStructureSetStorage

STUDY_UID = "1.2.826.0.1.3680043.10.777.1"
SERIES_UID = "1.2.826.0.1.3680043.10.777.2"
FOR_UID = "1.2.826.0.1.3680043.10.777.3"


# ======================================================================================
# Fixture generation -- synthetic, because a corpus only exercises the accept path
# ======================================================================================
def make_slice(
    out: Path,
    *,
    index: int,
    ipp: tuple[float, float, float],
    iop: tuple[float, ...] = (1, 0, 0, 0, 1, 0),
    rows: int = 16,
    cols: int = 16,
    pixel_spacing: tuple[float, float] = (2.0, 1.5),
    slope: float = 1.0,
    intercept: float = -1024.0,
    rescale_type: str | None = "HU",
    sop_class: str = CTImageStorage,
    stored_fill: int | None = None,
    padding_value: int | None = None,
    instance_number: int | None = None,
    gantry_tilt: float | None = None,
    drop_ipp: bool = False,
) -> Path:
    """Write one minimal CT instance. Values are chosen to be verifiable by hand."""
    out.mkdir(parents=True, exist_ok=True)
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop_class
    sop_uid = f"1.2.826.0.1.3680043.10.777.4.{index}"
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = SERIES_UID
    ds.FrameOfReferenceUID = FOR_UID
    ds.PatientID = "SYNTH-001"
    ds.PatientName = "Synth^Fixture"
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"
    ds.StudyDate = "20250101"
    ds.StudyTime = "120000"
    ds.AccessionNumber = "ACC-777"
    ds.Modality = "CT"
    ds.PatientPosition = "HFS"
    ds.InstanceNumber = instance_number if instance_number is not None else index + 1
    if not drop_ipp:
        ds.ImagePositionPatient = list(ipp)
    ds.ImageOrientationPatient = list(iop)
    ds.PixelSpacing = list(pixel_spacing)
    ds.SliceThickness = 1.0
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.RescaleSlope = slope
    ds.RescaleIntercept = intercept
    if rescale_type is not None:
        ds.RescaleType = rescale_type
    if gantry_tilt is not None:
        ds.GantryDetectorTilt = gantry_tilt
    if padding_value is not None:
        ds.PixelPaddingValue = padding_value

    fill = index if stored_fill is None else stored_fill
    arr = np.full((rows, cols), fill, dtype=np.int16)
    arr[0, 0] = 2000  # a landmark so a sort error is visible in the digest
    if padding_value is not None:
        arr[-1, -1] = padding_value
    ds.PixelData = arr.tobytes()

    path = out / f"slice_{index:04d}.dcm"
    ds.save_as(str(path), enforce_file_format=True)
    return path


def uniform_series(out: Path, n: int = 10, spacing: float = 2.5, **kw: Any) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    return [
        make_slice(out, index=k, ipp=(-10.0, -20.0, -30.0 + k * spacing), **kw)
        for k in range(n)
    ]


def expect_rejection(paths: list[Path], code: str, **kw: Any) -> str:
    """The code, not merely that something was raised.

    `MOS-IMG-010`'s vocabulary is a closed enum of eleven; a test that accepts any
    rejection passes when the wrong branch fires, which is how a rejection path rots
    while its test stays green.
    """
    try:
        build_canonical_volume(paths, **kw)
    except GeometryRejection as rej:
        assert rej.reason_code == code, f"expected {code}, got {rej.reason_code}: {rej}"
        return f"{rej.reason_code} detail={json.dumps(rej.detail, default=str)[:90]}"
    raise AssertionError(f"expected rejection {code}, but the build succeeded")


# ======================================================================================
# The accept path
# ======================================================================================
def test_accept_baseline_axial_geometry(tmp_path: Path) -> None:
    """MOS-IMG-029/030/038."""
    paths = uniform_series(tmp_path / "baseline", n=10, spacing=2.5)
    vol, src, _ = build_canonical_volume(paths)
    assert vol.shape == (10, 16, 16), vol.shape
    # MOS-IMG-029: PixelSpacing[0]=2.0 is between ROWS (along Y = dR); [1]=1.5 along X.
    assert np.allclose(vol.spacing_mm, (2.5, 2.0, 1.5)), vol.spacing_mm
    assert np.allclose(vol.affine[:3, 0], (1.5, 0, 0)), vol.affine[:3, 0]
    assert np.allclose(vol.affine[:3, 1], (0, 2.0, 0)), vol.affine[:3, 1]
    assert np.allclose(vol.affine[:3, 2], (0, 0, 2.5)), vol.affine[:3, 2]
    assert np.allclose(vol.affine[:3, 3], (-10, -20, -30)), vol.affine[:3, 3]
    assert vol.anatomical_code == "SPL", vol.anatomical_code  # MOS-IMG-038
    assert vol.spacing_class == "UNIFORM", vol.spacing_class
    assert np.linalg.det(vol.affine) > 0  # MOS-IMG-030
    # MOS-IMG-024/028: stored k with slope 1, intercept -1024 -> HU = k - 1024
    assert vol.array[3, 5, 5] == np.float32(3 - 1024), vol.array[3, 5, 5]
    assert abs(vol.voxel_volume_mm3() - 2.5 * 2.0 * 1.5) < 1e-9
    chk = vol.roundtrip_selfcheck()
    assert chk["max_index_error"] <= EPS_AFFINE


def test_accept_sort_is_position_only(tmp_path: Path) -> None:
    """MOS-IMG-013: geometric order must not depend on file order or InstanceNumber."""
    d = tmp_path / "shuffle"
    paths = uniform_series(d, n=10, spacing=2.5)
    ref, _, _ = build_canonical_volume(paths)
    rng = random.Random(7)
    shuffled = paths[:]
    rng.shuffle(shuffled)
    assert shuffled != paths
    got, _, _ = build_canonical_volume(shuffled)
    assert got.pixel_digest == ref.pixel_digest, "shuffled input changed the volume"
    assert got.sop_instance_uids == ref.sop_instance_uids
    # And: descending InstanceNumber must not flip the volume either.
    d2 = tmp_path / "instnum_reversed"
    d2.mkdir(parents=True, exist_ok=True)
    rev = [
        make_slice(
            d2, index=k, ipp=(-10.0, -20.0, -30.0 + k * 2.5), instance_number=10 - k
        )
        for k in range(10)
    ]
    got2, _, _ = build_canonical_volume(rev)
    assert got2.pixel_digest == ref.pixel_digest, "InstanceNumber leaked into the sort"


def test_accept_the_jittered_band(tmp_path: Path) -> None:
    """MOS-IMG-017."""
    d = tmp_path / "jitter"
    d.mkdir(parents=True, exist_ok=True)
    # dS_med = 2.5 -> UNIFORM tol = 0.0125, JITTERED tol = 0.05. Use 0.03.
    offsets = [0.0, 2.5, 5.03, 7.5, 10.0, 12.5, 15.0, 17.5, 20.0, 22.5]
    paths = [
        make_slice(d, index=k, ipp=(-10.0, -20.0, -30.0 + z))
        for k, z in enumerate(offsets)
    ]
    vol, _, _ = build_canonical_volume(paths)
    assert vol.spacing_class == "JITTERED", vol.spacing_class
    assert 0.0125 < vol.max_jitter_mm <= 0.05, vol.max_jitter_mm


def test_accept_an_identical_duplicate_is_dropped(tmp_path: Path) -> None:
    """MOS-IMG-014(a): identical digests -> keep lexicographically-first UID, record."""
    d = tmp_path / "dup_same"
    paths = uniform_series(d, n=10, spacing=2.5)
    dup = make_slice(d, index=93, ipp=(-10.0, -20.0, -30.0 + 3 * 2.5), stored_fill=3)
    vol, _, _ = build_canonical_volume(paths + [dup])
    assert vol.shape[0] == 10, vol.shape
    assert len(vol.dropped_duplicate_sop_instance_uids) == 1, (
        vol.dropped_duplicate_sop_instance_uids
    )
    dropped = vol.dropped_duplicate_sop_instance_uids[0]
    assert dropped not in vol.sop_instance_uids
    # MOS-IMG-121 needs this on the object the writer sees, not only the descriptor
    _, src, _ = build_canonical_volume(paths + [dup])
    assert src.dropped_duplicate_sop_instance_uids == (dropped,)


def test_accept_oblique_direction_cosines(tmp_path: Path) -> None:
    """MOS-IMG-012/029: direction cosines honoured; n recomputed, never assumed."""
    d = tmp_path / "coronal"
    d.mkdir(parents=True, exist_ok=True)
    iop = (1.0, 0.0, 0.0, 0.0, 0.0, -1.0)  # X = +L, Y = -S  =>  n = X x Y = +P
    paths = [
        make_slice(d, index=k, ipp=(-10.0, -20.0 + k * 2.5, -30.0), iop=iop)
        for k in range(8)
    ]
    vol, _, _ = build_canonical_volume(paths)
    assert np.allclose(vol.affine[:3, 2], (0.0, 2.5, 0.0)), vol.affine[:3, 2]
    assert vol.anatomical_code == "PIL", vol.anatomical_code
    assert np.linalg.det(vol.affine) > 0
    chk = vol.roundtrip_selfcheck()
    assert chk["max_index_error"] <= EPS_AFFINE


def test_accept_pixel_padding_substitution(tmp_path: Path) -> None:
    """MOS-IMG-027: padding replaced AFTER rescale with padding_output_value."""
    d = tmp_path / "padding"
    paths = uniform_series(d, n=6, spacing=2.5, padding_value=-2000)
    vol, _, _ = build_canonical_volume(paths, padding_output_value=-1024.0)
    assert vol.array[0, -1, -1] == np.float32(-1024.0), vol.array[0, -1, -1]
    # without substitution it would have been -2000*1 + -1024 = -3024


def test_accept_rescale_type_us_recorded_as_assumed(tmp_path: Path) -> None:
    """MOS-IMG-026: RescaleType 'US' is accepted and recorded as assumed."""
    d = tmp_path / "rescale_us"
    paths = uniform_series(d, n=4, spacing=2.5, rescale_type="US")
    vol, _, _ = build_canonical_volume(paths)
    assert vol.rescale_type_assumed is True


# ======================================================================================
# The rejection path -- eleven codes, eight of which ran nowhere else
# ======================================================================================
def test_reject_gantry_tilt(tmp_path: Path) -> None:
    """MOS-IMG-020/021."""
    d = tmp_path / "tilt"
    d.mkdir(parents=True, exist_ok=True)
    # shear the positions ~11 deg off the slice normal
    paths = [
        make_slice(d, index=k, ipp=(-10.0, -20.0 + k * 0.5, -30.0 + k * 2.5))
        for k in range(8)
    ]
    expect_rejection(paths, "geometry_gantry_tilt")


def test_reject_tilt_beyond_max_deg(tmp_path: Path) -> None:
    """MOS-IMG-021."""
    d = tmp_path / "tilt_big"
    d.mkdir(parents=True, exist_ok=True)
    paths = [
        make_slice(d, index=k, ipp=(-10.0, -20.0 + k * 4.0, -30.0 + k * 2.5))
        for k in range(8)
    ]
    expect_rejection(
        paths,
        "geometry_tilt_correction_out_of_range",
        allow_tilt_correction=True,
        gantry_tilt_max_deg=30.0,
    )


def test_reject_non_uniform_spacing(tmp_path: Path) -> None:
    """MOS-IMG-017."""
    d = tmp_path / "nonuniform"
    d.mkdir(parents=True, exist_ok=True)
    offsets = [0.0, 2.5, 5.0, 7.9, 10.4, 12.9, 15.4, 17.9]  # 0.4 mm jitter >> 0.05
    paths = [
        make_slice(d, index=k, ipp=(-10.0, -20.0, -30.0 + z))
        for k, z in enumerate(offsets)
    ]
    expect_rejection(paths, "geometry_non_uniform_spacing")


def test_reject_a_gap(tmp_path: Path) -> None:
    """MOS-IMG-017."""
    d = tmp_path / "gapped"
    d.mkdir(parents=True, exist_ok=True)
    offsets = [0.0, 2.5, 5.0, 7.5, 20.0, 22.5, 25.0, 27.5]  # one 12.5 mm gap
    paths = [
        make_slice(d, index=k, ipp=(-10.0, -20.0, -30.0 + z))
        for k, z in enumerate(offsets)
    ]
    expect_rejection(paths, "geometry_gapped")


def test_reject_conflicting_duplicates(tmp_path: Path) -> None:
    """MOS-IMG-014(b): same position, different pixels -- the platform must not choose."""
    d = tmp_path / "dup_diff"
    paths = uniform_series(d, n=8, spacing=2.5)
    clash = make_slice(d, index=91, ipp=(-10.0, -20.0, -30.0 + 3 * 2.5), stored_fill=555)
    expect_rejection(paths + [clash], "geometry_duplicate_positions")


def test_reject_missing_image_position_patient(tmp_path: Path) -> None:
    """MOS-IMG-010."""
    d = tmp_path / "no_ipp"
    paths = uniform_series(d, n=6, spacing=2.5)
    bad = make_slice(d, index=90, ipp=(0, 0, 0), drop_ipp=True)
    expect_rejection(paths + [bad], "geometry_missing_position")


def test_reject_inconsistent_orientation(tmp_path: Path) -> None:
    """MOS-IMG-015."""
    d = tmp_path / "orient"
    paths = uniform_series(d, n=6, spacing=2.5)
    # 0.5 deg about z -> above eps_ang = 0.1 deg
    c, s = np.cos(np.radians(0.5)), np.sin(np.radians(0.5))
    bad = make_slice(
        d, index=90, ipp=(-10.0, -20.0, -30.0 + 6 * 2.5), iop=(c, s, 0, -s, c, 0)
    )
    expect_rejection(paths + [bad], "geometry_inconsistent_orientation")


def test_reject_inconsistent_grid(tmp_path: Path) -> None:
    """MOS-IMG-015."""
    d = tmp_path / "grid"
    paths = uniform_series(d, n=6, spacing=2.5)
    bad = make_slice(
        d, index=90, ipp=(-10.0, -20.0, -30.0 + 6 * 2.5), pixel_spacing=(2.0, 1.6)
    )
    expect_rejection(paths + [bad], "geometry_inconsistent_grid")


def test_reject_unsupported_sop_class(tmp_path: Path) -> None:
    """MOS-IMG-007."""
    d = tmp_path / "sopclass"
    paths = uniform_series(d, n=6, spacing=2.5)
    bad = make_slice(
        d,
        index=90,
        ipp=(-10.0, -20.0, -30.0 + 6 * 2.5),
        sop_class=RTStructureSetStorage,
    )
    expect_rejection(paths + [bad], "geometry_unsupported_sop_class")


def test_reject_unsupported_rescale_type(tmp_path: Path) -> None:
    """MOS-IMG-026."""
    d = tmp_path / "rescale_bad"
    paths = uniform_series(d, n=6, spacing=2.5, rescale_type="OD")
    expect_rejection(paths, "geometry_unsupported_rescale")


def test_reject_too_few_instances(tmp_path: Path) -> None:
    """MOS-IMG-010."""
    d = tmp_path / "few"
    paths = uniform_series(d, n=4, spacing=2.5)
    expect_rejection(paths, "geometry_insufficient_instances", min_instances=20)


# ======================================================================================
# The census, so a deleted test is a failure rather than a smaller number
# ======================================================================================
def test_every_rejection_code_in_the_closed_enum_is_exercised_here() -> None:
    """`MOS-IMG-010`'s vocabulary is closed; a member with no test is a dead branch.

    This is the check that made the move necessary. Before it, the answer to "which
    rejection codes does the suite execute" was a grep somebody had to think to run --
    and the answer was three of eleven, because the other eight lived in a script that
    CI invoked by path.
    """
    import ast

    from medos.core.errors import REJECTION_CODES

    # AST, not a grep. This file names ten of the eleven codes in prose as well, and a
    # substring check would have counted those -- reporting full coverage from the
    # docstrings alone. Only a literal passed to `expect_rejection` counts as executed.
    exercised: set[str] = set()
    for node in ast.walk(ast.parse(Path(__file__).read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "expect_rejection"):
            continue
        for arg in node.args[1:]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                exercised.add(arg.value)

    unknown = sorted(exercised - set(REJECTION_CODES))
    assert not unknown, (
        f"{unknown} is passed to expect_rejection and is not in MOS-IMG-010's enum. "
        "Either the enum lost a member or this file is asserting a code that cannot be "
        "raised, which would make its test unfailable."
    )
    missing = sorted(set(REJECTION_CODES) - exercised)
    assert not missing, (
        f"{len(missing)} of {len(REJECTION_CODES)} rejection codes have no "
        f"`expect_rejection` call in this file: {missing}. MOS-IMG-010's enum is closed, "
        "so an unexercised member is a branch of the geometry contract that nothing has "
        "ever run. Adding a code to the enum without a fixture here is the defect this "
        "check exists to report."
    )
