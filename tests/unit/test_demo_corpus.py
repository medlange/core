# SPDX-License-Identifier: Apache-2.0
"""The demo seeder fabricates a study, so what it refuses matters more than what it makes.

WHAT IT IS FOR
--------------
`docker compose up -d` produced a healthy stack with an empty archive, so the first thing
a developer saw was a viewer with nothing in it and no way to tell a working platform
from a broken one. `medos/tools/demo/seed_corpus.py --profile demo` puts one synthetic CT study
in, and the study is built to be USEFUL: `lung_segmentation` returns right 1843 ml, left
1843 ml, total 3687 ml on it, and there is an 8 mm nodule so `lung_nodule` has something
to find. A demo that runs correctly and reports nothing looks exactly like a broken one.

WHAT IS DANGEROUS ABOUT IT
--------------------------
It invents patients. Not real ones -- there is no person behind the pixels -- but a
DICOM study with a StudyInstanceUID, sitting in an archive, indistinguishable at a glance
from an acquisition. Two things follow, and both are tested here rather than described:

  1. IT MUST REFUSE OUTSIDE A DEV DEPLOYMENT, and `MEDOS_ENV` unset must count as
     outside. The failure that actually happens is not somebody typing `MEDOS_ENV=dev` on
     a clinical system; it is somebody copying `docker-compose.yml`, changing the
     database URL, and setting nothing else. If unset meant dev, that deployment would
     seed fabricated studies into an archive holding patients.

  2. IT MUST STAY UNDER pydicom's ROOT. `tests/integration/test_deid_provenance_declaration
     .py` covers `1.2.826.0.1.3680043.8.498.` with the recorded justification "no patient
     behind them", so a study minted there needs no corpus manifest. Under `2.25.` it
     would need one, and a manifest's `deidentification_status` is drawn from
     {identified, pseudonymised, public_deidentified} -- NONE of which is true of a
     volume with no patient. The tool would have to write a false statement into an
     immutable row to satisfy a check. Register entry 93 records the same shape.

AND ONE THING THAT IS MERELY EXPENSIVE
--------------------------------------
The geometry has to sit inside BOTH shipped capabilities' envelopes. The existing test
phantom does not -- 32 slices at 1.5 mm, against `lung_nodule`'s 40-slice floor and
1.0 mm ceiling -- so a seeder that reused it would produce a study every job rejects with
`outside_applicability_envelope`, on a healthy stack, with no indication that the
PHANTOM was the problem.

Spec: MOS-IMG-069, MOS-EVID-021, MOS-DATA-009, MOS-SAFE-002.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.demo import seed_corpus  # noqa: E402

# --------------------------------------------------------------------------------------
# the refusal
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "prod", "production", "Dev", "DEV", "dev "])
def test_the_seeder_refuses_outside_a_dev_deployment(monkeypatch, tmp_path, value) -> None:
    """`None` is the important row. Every other value is somebody being explicit; unset
    is somebody copying a file, and that is the one that happens."""
    if value is None:
        monkeypatch.delenv("MEDOS_ENV", raising=False)
    else:
        monkeypatch.setenv("MEDOS_ENV", value)
    target = tmp_path / "out"
    code = seed_corpus.main(["--out-dir", str(target)])
    assert code == 2, f"MEDOS_ENV={value!r} should refuse, got exit {code}"
    assert not target.exists(), (
        f"MEDOS_ENV={value!r} refused but still wrote {target}. A refusal that happens "
        f"after the side effect is not a refusal."
    )


def test_exactly_dev_is_accepted(monkeypatch, tmp_path) -> None:
    """The other direction: if the guard refused everything the tool would be dead code
    and the test above would pass forever."""
    monkeypatch.setenv("MEDOS_ENV", "dev")
    target = tmp_path / "out"
    assert seed_corpus.main(["--out-dir", str(target)]) == 0
    assert len(list(target.glob("*.dcm"))) == seed_corpus.Phantom().n_slices


# --------------------------------------------------------------------------------------
# the identifiers
# --------------------------------------------------------------------------------------


def test_every_minted_uid_is_under_the_covered_root() -> None:
    """Under any other arc this tool would owe a corpus manifest, and no value of
    `deidentification_status` would be true. See the module docstring."""
    phantom = seed_corpus.Phantom()
    payloads, study_uid, series_uid = seed_corpus.build_series(
        phantom, seed_corpus.build_volume(phantom)
    )
    for uid in (study_uid, series_uid, *(sop for sop, _ in payloads)):
        assert uid.startswith(seed_corpus.UID_ROOT), uid
        assert len(uid) <= 64, f"UID longer than DICOM permits: {len(uid)}"


def test_the_root_is_the_one_the_deid_declaration_covers() -> None:
    """Transcribed in two places, so make them agree here rather than at 3 a.m."""
    covered = (REPO / "tests/integration/test_deid_provenance_declaration.py").read_text(
        encoding="utf-8"
    )
    assert f'"{seed_corpus.UID_ROOT}"' in covered, (
        f"{seed_corpus.UID_ROOT} is not in COVERED_ROOTS. Seeding under an uncovered "
        f"root turns the integration suite red and obliges a manifest this corpus "
        f"cannot honestly write."
    )


def test_the_uids_are_deterministic_so_reseeding_is_idempotent() -> None:
    """An archive that gains a study on every `up` is worse than an empty one: the second
    study looks like a second patient."""
    phantom = seed_corpus.Phantom()
    volume = seed_corpus.build_volume(phantom)
    first = seed_corpus.build_series(phantom, volume)
    second = seed_corpus.build_series(phantom, volume)
    assert first[1] == second[1], "StudyInstanceUID is not stable across runs"
    assert first[2] == second[2], "SeriesInstanceUID is not stable across runs"
    assert [s for s, _ in first[0]] == [s for s, _ in second[0]]


def test_instance_uids_are_distinct() -> None:
    """Determinism is easy to get by making everything equal."""
    phantom = seed_corpus.Phantom()
    payloads, study_uid, series_uid = seed_corpus.build_series(
        phantom, seed_corpus.build_volume(phantom)
    )
    sops = [s for s, _ in payloads]
    assert len(set(sops)) == len(sops)
    assert study_uid != series_uid


# --------------------------------------------------------------------------------------
# the envelope
# --------------------------------------------------------------------------------------


def test_the_default_phantom_is_inside_both_shipped_envelopes() -> None:
    seed_corpus.assert_inside_both_envelopes(seed_corpus.Phantom())


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("n_slices", 32, "outside lung_nodule"),
        ("pixel_spacing_mm", 1.5, "pixel spacing"),
        ("slice_spacing_mm", 5.0, "slice spacing"),
    ],
)
def test_a_phantom_outside_an_envelope_is_refused(field, value, expected) -> None:
    """THE EXISTING TEST PHANTOM'S NUMBERS, one at a time. 32 slices at 1.5 mm is what
    `tests/unit/test_capabilities.py` uses, and it is why this file exists instead of a
    re-export: both values are outside `lung_nodule`'s declared envelope."""
    with pytest.raises(SystemExit, match=expected):
        seed_corpus.assert_inside_both_envelopes(
            replace(seed_corpus.Phantom(), **{field: value})
        )


def test_a_phantom_whose_lungs_are_implausible_is_refused() -> None:
    """`PLAUSIBLE_TOTAL_ML` is an absurdity gate with a floor of 1500 ml. A phantom that
    slipped under it would be rejected by the capability at run time, on a healthy stack,
    for a reason that points at the data rather than at this file."""
    tiny = replace(seed_corpus.Phantom(), lung_j=(50, 60))
    with pytest.raises(SystemExit, match="PLAUSIBLE_TOTAL_ML"):
        seed_corpus.assert_inside_both_envelopes(tiny)


def test_the_lungs_are_two_components_with_a_tissue_mediastinum() -> None:
    """`lung_segmentation` splits laterality by connected components. Lungs that touch
    are one component and the right/left finding becomes arbitrary."""
    phantom = seed_corpus.Phantom()
    assert phantom.mediastinum_columns >= 32, phantom.mediastinum_columns
    merged = replace(seed_corpus.Phantom(), left_i=(195, 345))
    with pytest.raises(SystemExit, match="mediastinum"):
        seed_corpus.assert_inside_both_envelopes(merged)


def test_the_lungs_clear_both_k_faces() -> None:
    """A lung touching the first or last slice is dropped by the scan-extent filter."""
    phantom = seed_corpus.Phantom()
    assert phantom.lung_k[0] >= 1
    assert phantom.lung_k[1] <= phantom.n_slices - 1
    with pytest.raises(SystemExit, match="k-face"):
        seed_corpus.assert_inside_both_envelopes(
            replace(seed_corpus.Phantom(), lung_k=(0, 60))
        )


# --------------------------------------------------------------------------------------
# the pixels
# --------------------------------------------------------------------------------------


def test_the_volume_has_room_air_lung_and_tissue_where_it_says() -> None:
    phantom = seed_corpus.Phantom()
    hu = seed_corpus.build_volume(phantom)
    assert hu.shape == (phantom.n_slices, phantom.rows, phantom.cols)
    assert hu[0, 0, 0] == pytest.approx(phantom.hu_air), "corner should be room air"
    mid_k = (phantom.lung_k[0] + phantom.lung_k[1]) // 2
    mid_j = (phantom.lung_j[0] + phantom.lung_j[1]) // 2
    mid_i = (phantom.right_i[0] + phantom.right_i[1]) // 2
    assert hu[mid_k, mid_j, mid_i] == pytest.approx(phantom.hu_lung)
    gap_i = (phantom.right_i[1] + phantom.left_i[0]) // 2
    assert hu[mid_k, mid_j, gap_i] == pytest.approx(phantom.hu_tissue), "mediastinum"


def test_room_air_reaches_an_in_plane_face() -> None:
    """`lung_segmentation` step 1 tells lung from room air by reachability from an
    in-plane face. A body that touches the edge makes the phantom test nothing."""
    phantom = seed_corpus.Phantom()
    hu = seed_corpus.build_volume(phantom)
    mid_k = (phantom.lung_k[0] + phantom.lung_k[1]) // 2
    assert np.all(hu[mid_k, 0, :] == phantom.hu_air)
    assert np.all(hu[mid_k, -1, :] == phantom.hu_air)
    assert np.all(hu[mid_k, :, 0] == phantom.hu_air)
    assert np.all(hu[mid_k, :, -1] == phantom.hu_air)


def test_the_nodule_is_present_and_large_enough_to_report() -> None:
    """`lung_nodule` reports nothing under 4 mm. A demo whose one study yields no finding
    is indistinguishable from a broken pipeline."""
    phantom = seed_corpus.Phantom()
    hu = seed_corpus.build_volume(phantom)
    voxels = int(np.count_nonzero(hu == phantom.hu_nodule))
    assert voxels > 0, "no nodule in the volume"
    volume_mm3 = voxels * phantom.pixel_spacing_mm**2 * phantom.slice_spacing_mm
    diameter_mm = 2.0 * (3.0 * volume_mm3 / (4.0 * np.pi)) ** (1 / 3)
    assert diameter_mm >= 4.0, f"nodule is {diameter_mm:.1f} mm across, below the floor"


def test_the_identity_tags_say_there_is_no_patient() -> None:
    """A blank PatientName is ambiguous with a badly de-identified real study. These
    values cannot be mistaken for one, which is the point."""
    import pydicom

    phantom = seed_corpus.Phantom()
    payloads, _, _ = seed_corpus.build_series(phantom, seed_corpus.build_volume(phantom))
    import io

    ds = pydicom.dcmread(io.BytesIO(payloads[0][1]))
    assert "PHANTOM" in str(ds.PatientName).upper()
    assert "NOT" in str(ds.PatientName).upper() and "PATIENT" in str(ds.PatientName).upper()
    assert "DEMO" in str(ds.PatientID).upper()
    assert "NOT FOR CLINICAL USE" in str(ds.ImageComments).upper()


def test_the_series_carries_hu_and_not_stored_values() -> None:
    """`lung_nodule` refuses a series whose Modality LUT was not applied: its thresholds
    are Hounsfield units, and on stored values they produce a number for every case and a
    correct one for none."""
    import io

    import pydicom

    phantom = seed_corpus.Phantom()
    payloads, _, _ = seed_corpus.build_series(phantom, seed_corpus.build_volume(phantom))
    ds = pydicom.dcmread(io.BytesIO(payloads[0][1]))
    assert ds.RescaleType == "HU"
    assert float(ds.RescaleIntercept) == -1024.0
    assert float(ds.RescaleSlope) == 1.0
    assert ds.Modality == "CT"


def test_the_geometry_is_axial_and_untilted() -> None:
    import io

    import pydicom

    phantom = seed_corpus.Phantom()
    payloads, _, _ = seed_corpus.build_series(phantom, seed_corpus.build_volume(phantom))
    ds = pydicom.dcmread(io.BytesIO(payloads[0][1]))
    assert [float(v) for v in ds.ImageOrientationPatient] == [1, 0, 0, 0, 1, 0]


# --------------------------------------------------------------------------------------
# The demo actually demos something
# --------------------------------------------------------------------------------------
#
# Everything above checks the phantom against numbers this repository writes down. This
# checks it against what the CAPABILITY returns, which is a different claim and the one
# the README makes: "right lung 1843.14 ml, left 1843.38 ml, total 3686.52 ml".
#
# Without this, a change to the phantom's geometry could keep every arithmetic assertion
# green while quietly turning the demo into a study that segments to nothing -- and the
# only place that would show is a developer's first ten minutes.


def _run_lung_segmentation(tmp_path):
    from medos.capabilities import REGISTRY, CapabilityContext
    from medos.core.geometry import build_canonical_volume

    assert seed_corpus.main(["--out-dir", str(tmp_path)]) == 0
    volume, source, _ = build_canonical_volume(sorted(tmp_path.glob("*.dcm")))
    context = CapabilityContext(
        job_id="job_demo_corpus_test",
        series_instance_uid=volume.series_instance_uid,
        source=source,
        clinical_use_mode="research_only",
    )
    return REGISTRY["lung_segmentation"].run(volume, context)


def _volume_ml(outcome, kind: str) -> float:
    for finding in outcome.findings:
        if finding.kind == kind:
            return float(finding.measurements[0].value)
    raise AssertionError(f"no {kind} finding in {[f.kind for f in outcome.findings]}")


def test_the_shipped_capability_segments_the_demo_study(monkeypatch, tmp_path) -> None:
    """THE CLAIM THE README MAKES, checked against the capability rather than against
    this file's own arithmetic."""
    monkeypatch.setenv("MEDOS_ENV", "dev")
    outcome = _run_lung_segmentation(tmp_path)

    assert {f.kind for f in outcome.findings} == {"lung_right", "lung_left", "lung"}
    assert all(f.present for f in outcome.findings)

    right = _volume_ml(outcome, "lung_right")
    left = _volume_ml(outcome, "lung_left")
    total = _volume_ml(outcome, "lung")

    # Loose bounds on purpose. Pinning 1843.1438199999996 would make this a change
    # detector for floating-point noise; what matters is that the study segments to two
    # physiological lungs and clears the absurdity gate.
    assert 1500 <= total <= 12000, f"total {total} ml is outside PLAUSIBLE_TOTAL_ML"
    assert right == pytest.approx(left, rel=0.05), (
        f"the phantom's lungs are symmetric by construction; {right} vs {left} means the "
        f"laterality split found something other than the two boxes it was given"
    )
    assert total == pytest.approx(right + left, rel=1e-6)


def test_the_capability_measurement_matches_the_declared_geometry(
    monkeypatch, tmp_path
) -> None:
    """`Phantom.lung_volume_ml` is arithmetic this repository does; the capability's
    number comes from counting segmented voxels. They should agree, and if they stop
    agreeing then either the phantom is not what it says or the segmentation is losing
    voxels -- both worth knowing, and neither visible from the other tests."""
    monkeypatch.setenv("MEDOS_ENV", "dev")
    outcome = _run_lung_segmentation(tmp_path)
    declared = seed_corpus.Phantom().lung_volume_ml
    measured = _volume_ml(outcome, "lung")
    assert measured == pytest.approx(declared, rel=0.02), (
        f"declared {declared:.0f} ml, capability measured {measured:.0f} ml"
    )


def test_the_two_lungs_are_labelled_right_and_left_by_position(
    monkeypatch, tmp_path
) -> None:
    """Laterality is read from the affine, not assumed. A mirrored series that still
    reports 'right lung' on the left side renders perfectly and is invisible, which is
    why the segments are checked by coded concept rather than by count."""
    monkeypatch.setenv("MEDOS_ENV", "dev")
    outcome = _run_lung_segmentation(tmp_path)
    meanings = [segment.meaning for segment in outcome.label_map.segments]
    assert meanings == ["Right lung", "Left lung"], meanings


def test_the_pet_series_stands_where_the_ct_nodule_does() -> None:
    """The whole point of a synthetic PET is that its geometry is checkable.

    FUSION IS CORRECT EXACTLY WHEN THE RESAMPLING IS, and nothing else about it can be
    verified by looking. So the two acquisitions are written on DELIBERATELY DIFFERENT
    grids -- the CT 320x448 at 0.7 mm over 64 slices of 2 mm, the PET 128x128 at 2.8 mm
    over 32 of 4 mm -- and the uptake is placed at the nodule's patient coordinates. An
    overlay that ignores patient space and draws `pet[k][j][i]` over `ct[k][j][i]` puts
    the hot spot somewhere the nodule is not, by a distance this test measures.

    THE CENTROID, NOT THE ARGMAX. The lesion is a filled sphere of one value, so
    `argmax` returns the first voxel of the plateau -- its corner. A probe written that
    way reported a 3.15 mm offset that does not exist, which is worth recording because
    the same mistake reads as a real finding.
    """
    pydicom = pytest.importorskip("pydicom")

    p = seed_corpus.Phantom()
    payloads, study_uid, series_uid = seed_corpus.build_pet_series(p)
    assert len(payloads) == seed_corpus.PET_SLICES

    k0, j0, i0 = p.nodule_centre
    ct_x0 = -(p.cols - 1) * p.pixel_spacing_mm / 2.0
    ct_y0 = -(p.rows - 1) * p.pixel_spacing_mm / 2.0
    want = (
        ct_x0 + i0 * p.pixel_spacing_mm,
        ct_y0 + j0 * p.pixel_spacing_mm,
        k0 * p.slice_spacing_mm,
    )

    import io

    peak = 0.0
    frames = []
    for _uid, blob in payloads:
        ds = pydicom.dcmread(io.BytesIO(blob))
        arr = ds.pixel_array.astype(float) * float(ds.RescaleSlope) + float(ds.RescaleIntercept)
        frames.append((arr, [float(v) for v in ds.ImagePositionPatient], ds))
        peak = max(peak, float(arr.max()))

    wx = wy = wz = w = 0.0
    for arr, pos, ds in frames:
        hot = arr >= peak * 0.99
        if not hot.any():
            continue
        jj, ii = np.nonzero(hot)
        ps = float(ds.PixelSpacing[0])
        wx += float((pos[0] + ii * ps).sum())
        wy += float((pos[1] + jj * ps).sum())
        wz += pos[2] * len(ii)
        w += len(ii)
    assert w, "the PET carries no uptake at all"
    got = (wx / w, wy / w, wz / w)

    # WITHIN ONE PET VOXEL. The residual is the PET grid's own quantisation and nothing
    # else; measured at 0.10 mm in x and y and exactly 0 in z.
    for axis, a, b, tol in (
        ("x", got[0], want[0], seed_corpus.PET_PIXEL_MM),
        ("y", got[1], want[1], seed_corpus.PET_PIXEL_MM),
        ("z", got[2], want[2], seed_corpus.PET_SLICE_MM),
    ):
        assert abs(a - b) <= tol, (
            f"the uptake is {abs(a - b):.2f} mm from the nodule along {axis}, which is "
            f"more than one PET voxel ({tol:g} mm). A fusion display verified against "
            "this corpus would be verified against the wrong answer."
        )

    ct_payloads, ct_study, _ct_series = seed_corpus.build_series(p, seed_corpus.build_volume(p))
    ct = pydicom.dcmread(io.BytesIO(ct_payloads[0][1]))
    pet = frames[0][2]
    assert str(pet.FrameOfReferenceUID) == str(ct.FrameOfReferenceUID), (
        "the PET and the CT assert different coordinate systems, so `positionLinkable` "
        "refuses them and a fusion drawn on top would claim a correspondence the headers "
        "deny. The first version of the seeder spelled this uid `_uid('frame')` against "
        "the CT's `_uid('frame-of-reference')` -- both well-formed, and different."
    )
    assert study_uid == ct_study, "the PET is not in the CT's study"
    # `getattr`, NOT `pet.Units`. pydicom raises AttributeError for an absent element, so
    # the attribute access fails BEFORE the assertion and the test goes red with a
    # traceback instead of the sentence explaining what is wrong -- which is a test that
    # reports "AttributeError" where it meant to report a category error about units.
    assert str(pet.Modality) == "PT" and getattr(pet, "Units", None) == "BQML", (
        "the PET does not state its own unit, so the viewer would fall back to the "
        "modality default and a concentration would be rendered as Hounsfield"
    )

    # THE SPACING, NOT ONLY THE MATRIX, and the first version of this assertion checked
    # only the matrix. Breaking the seeder to `PET_MATRIX = 320` -- the CT's row count --
    # left it green, because 320x320 is still not 320x448. A matrix can differ while the
    # sampling does not; what makes the resampling real is that the STEP differs, in
    # plane and through it.
    assert float(pet.PixelSpacing[0]) != p.pixel_spacing_mm, (
        "the PET is sampled at the CT's in-plane pitch, so an overlay that ignores "
        "patient coordinates would look correct and this corpus would verify nothing"
    )
    assert float(pet.SliceThickness) != p.slice_spacing_mm, (
        "the PET is sampled at the CT's slice pitch, so the through-plane half of the "
        "resampling is never exercised"
    )
    assert series_uid != _ct_series
