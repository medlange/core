# SPDX-License-Identifier: Apache-2.0
"""Unit tests for `medos.capabilities` and `medos.dicomweb`.

No containers, no PACS, no database (CONTRACT.md §11: "Unit tests need no containers").
Everything that needs a volume builds one: `_write_phantom_ct` emits real single-frame CT
Part-10 files into `tmp_path` and runs them through `build_canonical_volume`, so the tests
exercise the production read path rather than hand-constructing a `SourceGeometry` whose
25 fields would silently drift from the real one.

Tests that need the LCTSC corpus are marked `real_data` and skip when
`F:/WorkSpace/PulmoAI/TCIA` is absent. They are the ones that anchor the numbers in the
capability docstrings and that reproduce the week-0 spike's recorded measurements exactly.

Spec: MOS-IMG-039, MOS-IMG-040, MOS-IMG-041, MOS-IMG-111, MOS-IMG-112, MOS-IMG-121,
MOS-SAFE-012, MOS-SAFE-014, MOS-SAFE-015, MOS-SAFE-017, MOS-EXEC-001.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
from pathlib import Path

import numpy as np
import pytest
from medos.capabilities import (
    APPLICABILITY_REASON_CODES,
    REGISTRY,
    Capability,
    CapabilityContext,
    CapabilityRejection,
    DependentCapability,
    EmphysemaLaa,
    LungSegmentation,
    MissingDependency,
    PleuralEffusion,
    bind_dependencies,
    execution_order,
    load_concepts,
    mask_for_segment,
    metadata_for,
    segment_index,
)
from medos.capabilities import emphysema_laa as laa_mod
from medos.capabilities import lung_segmentation as seg_mod
from medos.capabilities import pleural_effusion as pe_mod
from medos.capabilities.base import DetectionMethod
from medos.core.bundle import CapabilityOutcome, CodedConcept, LabelMap
from medos.core.errors import ClinicalRejection
from medos.core.geometry import build_canonical_volume

from tests._support.skips import skip_no_data, skipif_no_data

LCTSC_ROOT = Path(os.environ.get("MEDOS_E2E_LCTSC_ROOT", "F:/WorkSpace/PulmoAI/TCIA"))
# `skipif_no_data` and not `pytest.mark.skipif`: a plain skipif cannot be made to FAIL
# under --require-corpus, and the nightly job that mounts the corpus needs exactly that.
REPO_ROOT = Path(__file__).resolve().parents[2]

real_data = skipif_no_data(
    not LCTSC_ROOT.exists(),
    f"LCTSC corpus not mounted at {LCTSC_ROOT} (set MEDOS_E2E_LCTSC_ROOT)",
    corpus="lctsc-corpus",
)


# =====================================================================================
# Phantom
# =====================================================================================
# Sized to sit INSIDE `lung_segmentation`'s own applicability envelope and above its
# plausibility floor: 1.5 mm in-plane (MAX_IN_PLANE_MM = 1.5), 3.0 mm slices
# (DELTA_S_MM_RANGE = 0.5-5.0), 32 slices (MIN_SLICES = 16), and 2612.7 ml of lung
# (PLAUSIBLE_TOTAL_ML = 1500-12000). A phantom that its own capability would reject tests
# the rejection, not the method.
#
# The lung volume is deliberately a PHYSIOLOGICAL one rather than the smallest number that
# clears the gate. An earlier revision of this file used a 258 ml phantom, which passed
# only because the absurdity gate's floor was then 250 ml -- and that floor was too low,
# admitting real LCTSC cases whose lungs had leaked into room air (Dice 0.000 to 0.314).
# Raising the floor to a defensible 1500 ml broke the phantom, which is the right way
# round: a fixture sized to the old bug is a fixture that argues for keeping it.
PHANTOM = {
    "n_slices": 32,
    "rows": 160,
    "cols": 224,
    "pixel_spacing": 1.5,
    "slice_spacing": 3.0,
    # Body box (rows, cols). Deliberately inside the image so that room air -- and only
    # room air -- reaches an in-plane face, which is the discriminator step 1 uses.
    "body_j": (12, 148),
    "body_i": (12, 212),
    # Lungs. Both stop short of k=0 and k=K-1 so the scan-extent filter keeps them, and
    # they are separated by a 32-column tissue mediastinum so they are two components:
    # this is the `lateral_separation == "connected_components"` branch, and the column
    # profile between them is identically zero, so the valley prominence ratio is 1.0.
    "lung_k": (2, 30),
    "lung_j": (24, 120),
    "right_i": (24, 96),  # low column index == low +x == patient RIGHT
    "left_i": (128, 200),
    # A block of emphysema-like voxels inside the RIGHT lung only.
    "laa_k": (4, 12),
    "laa_j": (30, 46),
    "laa_i": (30, 42),
    "hu_air": -1000.0,
    "hu_tissue": 0.0,
    "hu_lung": -800.0,
    "hu_laa": -980.0,
}
VOXEL_ML = (
    PHANTOM["pixel_spacing"] ** 2 * PHANTOM["slice_spacing"] / 1000.0
)  # 0.002 ml
LUNG_VOXELS = (
    (PHANTOM["lung_k"][1] - PHANTOM["lung_k"][0])
    * (PHANTOM["lung_j"][1] - PHANTOM["lung_j"][0])
    * (PHANTOM["right_i"][1] - PHANTOM["right_i"][0])
)
LAA_VOXELS = (
    (PHANTOM["laa_k"][1] - PHANTOM["laa_k"][0])
    * (PHANTOM["laa_j"][1] - PHANTOM["laa_j"][0])
    * (PHANTOM["laa_i"][1] - PHANTOM["laa_i"][0])
)


def _phantom_hu() -> np.ndarray:
    p = PHANTOM
    hu = np.full((p["n_slices"], p["rows"], p["cols"]), p["hu_air"], dtype=np.float32)
    hu[:, slice(*p["body_j"]), slice(*p["body_i"])] = p["hu_tissue"]
    k0, k1 = p["lung_k"]
    j0, j1 = p["lung_j"]
    for i0, i1 in (p["right_i"], p["left_i"]):
        hu[k0:k1, j0:j1, i0:i1] = p["hu_lung"]
    hu[
        p["laa_k"][0] : p["laa_k"][1],
        p["laa_j"][0] : p["laa_j"][1],
        p["laa_i"][0] : p["laa_i"][1],
    ] = p["hu_laa"]
    return hu


def _write_phantom_ct(directory: Path, hu: np.ndarray, *, flip_lr: bool = False) -> None:
    """Write `hu` as single-frame CT Part-10 files.

    `flip_lr` negates the row-direction cosine, i.e. makes a HIGHER column index mean a
    MORE NEGATIVE x. It exists so the laterality test can prove the sign is read from the
    affine and not assumed -- a mirrored series that still reports "right lung" on the
    left side is the one defect in this module that renders perfectly and is invisible.
    """
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    directory.mkdir(parents=True, exist_ok=True)
    study_uid, series_uid, frame_uid = (generate_uid() for _ in range(3))
    ps = PHANTOM["pixel_spacing"]
    dz = PHANTOM["slice_spacing"]
    rows, cols = hu.shape[1], hu.shape[2]
    row_x = -1.0 if flip_lr else 1.0
    # x0 chosen so the volume straddles x = 0 either way.
    x0 = (cols - 1) * ps / 2.0 if flip_lr else -(cols - 1) * ps / 2.0

    for k in range(hu.shape[0]):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.ImplementationClassUID = generate_uid()
        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        # Not PHI -- there is no patient. Deliberately unusual strings so
        # `test_no_phi_reaches_the_outcome_or_the_diagnostics` can search for them and
        # cannot collide with a legitimate value such as the convolution kernel.
        ds.PatientID = "ZZPHIIDZZ"
        ds.PatientName = "ZZPHINAMEZZ^Test"
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1
        ds.Rows = rows
        ds.Columns = cols
        ds.PixelSpacing = [ps, ps]
        ds.SliceThickness = dz
        ds.ImageOrientationPatient = [row_x, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.ImagePositionPatient = [x0, -(rows - 1) * ps / 2.0, k * dz]
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleSlope = 1.0
        ds.RescaleIntercept = -1024.0
        ds.RescaleType = "HU"
        ds.ConvolutionKernel = "TESTKERNEL"
        stored = np.clip(hu[k] + 1024.0, 0, 65535).astype(np.uint16)
        ds.PixelData = stored.tobytes()
        pydicom.dcmwrite(
            directory / f"slice{k:03d}.dcm", ds, enforce_file_format=True
        )


@pytest.fixture(scope="module")
def phantom(tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp("phantom")
    _write_phantom_ct(directory, _phantom_hu())
    vol, src, _ = build_canonical_volume(sorted(directory.glob("*.dcm")))
    ctx = CapabilityContext(
        job_id="job_phantom",
        series_instance_uid=vol.series_instance_uid,
        source=src,
        clinical_use_mode="research_only",
    )
    return vol, ctx


@pytest.fixture(scope="module")
def phantom_flipped(tmp_path_factory: pytest.TempPathFactory):
    directory = tmp_path_factory.mktemp("phantom_flipped")
    _write_phantom_ct(directory, _phantom_hu(), flip_lr=True)
    vol, src, _ = build_canonical_volume(sorted(directory.glob("*.dcm")))
    ctx = CapabilityContext(
        job_id="job_phantom_flipped",
        series_instance_uid=vol.series_instance_uid,
        source=src,
        clinical_use_mode="research_only",
    )
    return vol, ctx


# =====================================================================================
# The interface (CONTRACT.md §6)
# =====================================================================================
def test_registry_holds_exactly_the_three_contract_capabilities():
    """CONTRACT.md §7 names three, no more and no fewer."""
    assert set(REGISTRY) == {"lung_segmentation", "emphysema_laa", "pleural_effusion"}


@pytest.mark.parametrize("capability_id", sorted(REGISTRY))
def test_every_capability_satisfies_the_protocol(capability_id: str):
    capability = REGISTRY[capability_id]
    assert isinstance(capability, Capability)
    assert capability.capability_id == capability_id
    assert capability.version
    assert callable(capability.applicable)
    assert callable(capability.run)


@pytest.mark.parametrize("capability_id", sorted(REGISTRY))
def test_registry_entries_are_frozen(capability_id: str):
    """CONTRACT.md §11: no global mutable state. A registry entry is shared across jobs."""
    capability = REGISTRY[capability_id]
    assert dataclasses.is_dataclass(capability)
    with pytest.raises(dataclasses.FrozenInstanceError):
        capability.version = "9.9.9"  # type: ignore[misc]


def test_only_emphysema_declares_a_dependency():
    assert isinstance(REGISTRY["emphysema_laa"], DependentCapability)
    assert not isinstance(REGISTRY["lung_segmentation"], DependentCapability)
    assert not isinstance(REGISTRY["pleural_effusion"], DependentCapability)
    assert REGISTRY["emphysema_laa"].depends_on == ("lung_segmentation",)


def test_execution_order_puts_the_dependency_first():
    """CONTRACT.md §7's step ordering, derived rather than hard-coded."""
    order = execution_order(["emphysema_laa", "pleural_effusion", "lung_segmentation"])
    assert order.index("lung_segmentation") < order.index("emphysema_laa")
    assert set(order) == {"lung_segmentation", "emphysema_laa", "pleural_effusion"}
    # stable and idempotent
    assert execution_order(order) == order


def test_execution_order_refuses_an_unrequested_dependency():
    with pytest.raises(ValueError, match="did not request"):
        execution_order(["emphysema_laa"])


def test_execution_order_refuses_an_unknown_capability():
    with pytest.raises(KeyError, match="unknown capability_id"):
        execution_order(["lung_segmentation", "pneumothorax"])


def test_a_rejected_dependency_rejects_the_dependent_and_does_not_crash():
    """The state the cohort run actually reaches on 5 of 24 LCTSC-Test cases.

    When `lung_segmentation` REJECTS, `emphysema_laa` has no mask. That is a clinical
    fact about the study (chapter 5 T7), not a wiring bug, so the worker must be able to
    tell the two apart. `attempted` is the evidence: a dependency that RAN and produced
    nothing rejects its dependent; one that was never run is still `MissingDependency`.
    """
    with pytest.raises(CapabilityRejection) as excinfo:
        bind_dependencies(
            REGISTRY["emphysema_laa"], {}, attempted=["lung_segmentation"]
        )
    assert excinfo.value.reason_code == "input_constraint_unmet"
    assert excinfo.value.problem_class == "clinical_rejection"
    assert excinfo.value.detail["unsatisfied"] == ["lung_segmentation"]


def test_a_dependency_that_was_never_run_is_a_wiring_bug_not_a_rejection():
    with pytest.raises(MissingDependency):
        bind_dependencies(REGISTRY["emphysema_laa"], {}, attempted=[])
    # ...and the same is true when some OTHER capability was attempted.
    with pytest.raises(MissingDependency):
        bind_dependencies(
            REGISTRY["emphysema_laa"], {}, attempted=["pleural_effusion"]
        )


def test_bind_dependencies_passes_independent_capabilities_through_untouched():
    for capability_id in ("lung_segmentation", "pleural_effusion"):
        capability = REGISTRY[capability_id]
        assert bind_dependencies(capability, {}, attempted=["anything"]) is capability


# =====================================================================================
# Purity (CONTRACT.md §6: no database, no network, no PACS)
# =====================================================================================
FORBIDDEN_IMPORT_ROOTS = {
    "medos.db",
    "medos.dicomweb",
    "medos.api",
    "medos.worker",
    "requests",
    "psycopg",
    "psycopg2",
    "socket",
    "http",
    "urllib",
    "sqlite3",
    "asyncio",
    "subprocess",
}


@pytest.mark.parametrize(
    "module_path",
    sorted(Path("medos/medos/capabilities").glob("*.py")),
    ids=lambda p: p.name,
)
def test_no_capability_module_can_reach_io(module_path: Path):
    """A capability MUST NOT touch the database, the network, or the PACS.

    Checked statically on the import graph rather than by monkeypatching `requests`,
    because the guarantee has to hold for a reader of the source, not only for the code
    paths a test happens to execute.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    offenders = {
        name
        for name in imported
        for root in FORBIDDEN_IMPORT_ROOTS
        if name == root or name.startswith(root + ".")
    }
    assert not offenders, f"{module_path.name} imports {sorted(offenders)}"


# =====================================================================================
# Honesty metadata (chapter 9 MOS-SAFE-013/014/015/017)
# =====================================================================================
def test_lung_segmentation_is_not_labelled_as_a_model():
    """CONTRACT.md §7: "not a learned model; label it honestly"."""
    metadata = metadata_for("lung_segmentation")
    assert metadata.method_class == "deterministic_algorithm"
    assert metadata.operating_point is None
    summary = metadata.method_summary.lower()
    assert "no model" in summary and "no weights" in summary
    assert "hounsfield" in summary or "threshold" in summary


def test_emphysema_laa_is_not_labelled_as_a_model():
    metadata = metadata_for("emphysema_laa")
    assert metadata.method_class == "deterministic_algorithm"
    assert metadata.operating_point is None
    assert metadata.parameters["threshold_hu"] == -950.0
    assert metadata.parameters["computed_on"] == "source_grid"


def test_pleural_effusion_is_labelled_not_implemented():
    metadata = metadata_for("pleural_effusion")
    assert metadata.method_class == "not_implemented"
    assert metadata.parameters["implemented"] is False
    assert metadata.output_kinds == ()


@pytest.mark.parametrize("capability_id", sorted(REGISTRY))
def test_metadata_declares_limits_and_failure_modes(capability_id: str):
    """MOS-SAFE-015 fails closed: an empty list is a registration error."""
    metadata = metadata_for(capability_id)
    assert metadata.not_validated_for
    assert metadata.known_failure_modes
    assert metadata.computation_geometry == "source"  # MOS-IMG-039
    allowed = set(DetectionMethod.__args__)  # type: ignore[attr-defined]
    ids = [f.id for f in metadata.known_failure_modes]
    assert len(ids) == len(set(ids)), "MOS-SAFE-017: ids unique within the block"
    for failure in metadata.known_failure_modes:
        assert failure.detection in allowed
        assert failure.text and failure.mitigation


@pytest.mark.parametrize("capability_id", sorted(REGISTRY))
def test_metadata_is_json_serialisable(capability_id: str):
    """It has to reach the provenance record and the OHIF panel (MOS-SAFE-012)."""
    blob = json.dumps(metadata_for(capability_id).to_dict())
    assert json.loads(blob)["capability_id"] == capability_id


def test_metadata_refuses_an_empty_declaration():
    from medos.capabilities.base import CapabilityMetadata

    with pytest.raises(ValueError, match="MOS-SAFE-015"):
        CapabilityMetadata(
            capability_id="x",
            version="0",
            method_class="deterministic_algorithm",
            method_summary="",
            output_kinds=(),
            computation_geometry="source",
            input_constraints="",
            not_validated_for=(),
            known_failure_modes=(),
            parameters={},
        )


def test_metadata_refuses_a_non_source_computation_geometry():
    from medos.capabilities.base import CapabilityMetadata, FailureMode

    with pytest.raises(ValueError, match="MOS-IMG-039"):
        CapabilityMetadata(
            capability_id="x",
            version="0",
            method_class="deterministic_algorithm",
            method_summary="",
            output_kinds=(),
            computation_geometry="model",
            input_constraints="",
            not_validated_for=("nothing",),
            known_failure_modes=(
                FailureMode(id="a", text="t", detection="none", mitigation="m"),
            ),
            parameters={},
        )


# =====================================================================================
# Rejections (chapter 5 §5.3.1 + T7)
# =====================================================================================
def test_capability_rejection_is_a_clinical_rejection_not_a_failure():
    exc = CapabilityRejection("unsupported_geometry", {"tilt_deg": 12.4}, "tilted")
    assert isinstance(exc, ClinicalRejection)
    problem = exc.to_problem()
    assert problem["class"] == "clinical_rejection"
    assert problem["status"] == 422
    assert problem["retryable"] is False
    assert exc.to_dict()["reason_code"] == "unsupported_geometry"


def test_capability_rejection_refuses_an_invented_reason_code():
    with pytest.raises(ValueError, match="not a chapter 5 job-level rejection code"):
        CapabilityRejection("lungs_look_odd", {}, "nope")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("modality", "MR"),
        ("value_units", "raw"),
        ("spacing_mm", (12.0, 2.0, 2.0)),
        ("spacing_mm", (2.0, 4.0, 4.0)),
        ("tilt_deg", 12.4),
        ("resampled_from_source", True),
    ],
)
def test_applicable_returns_a_closed_vocabulary_code(phantom, field: str, value):
    vol, _ = phantom
    bad = dataclasses.replace(vol, **{field: value})
    for capability_id in ("lung_segmentation", "emphysema_laa"):
        verdict = REGISTRY[capability_id].applicable(bad)
        if verdict is not None:
            assert verdict in APPLICABILITY_REASON_CODES


def test_applicable_returns_none_for_the_phantom(phantom):
    vol, _ = phantom
    for capability_id in sorted(REGISTRY):
        assert REGISTRY[capability_id].applicable(vol) is None


def test_run_rejects_rather_than_trusting_the_caller_to_preflight(phantom):
    vol, ctx = phantom
    bad = dataclasses.replace(vol, modality="MR")
    with pytest.raises(CapabilityRejection) as excinfo:
        LungSegmentation().run(bad, ctx)
    assert excinfo.value.reason_code == "input_constraint_unmet"


def test_context_and_volume_must_name_the_same_series(phantom):
    vol, ctx = phantom
    other = dataclasses.replace(ctx, series_instance_uid="1.2.3.4")
    with pytest.raises(ValueError, match="MOS-IMG-121"):
        LungSegmentation().run(vol, other)


# =====================================================================================
# lung_segmentation on the phantom
# =====================================================================================
def test_lung_segmentation_measures_the_phantom_exactly(phantom):
    vol, ctx = phantom
    result = seg_mod.segment_lungs(vol, ctx)
    expected = LUNG_VOXELS * VOXEL_ML
    assert result.volume_ml["lung_right"] == pytest.approx(expected, rel=0, abs=1e-9)
    assert result.volume_ml["lung_left"] == pytest.approx(expected, rel=0, abs=1e-9)
    assert result.volume_ml["lung"] == pytest.approx(2 * expected, rel=0, abs=1e-9)
    assert result.diagnostics["computed_on"] == "source_grid"
    assert result.diagnostics["scan_extent"]["scan_extent_filter"] == "applied"


def test_phantom_lungs_separate_by_connectivity_not_by_the_plane(phantom):
    """The `connected_components` branch: no component straddles the cut."""
    vol, ctx = phantom
    diagnostics = seg_mod.segment_lungs(vol, ctx).diagnostics
    assert diagnostics["laterality"]["lateral_separation"] == "connected_components"
    assert diagnostics["laterality"]["n_components_straddling_cut"] == 0


def test_laterality_follows_the_affine_and_not_the_column_index(phantom, phantom_flipped):
    """A mirrored acquisition must swap which column range is the RIGHT lung.

    Both phantoms hold the SAME voxel array; only the row-direction cosine differs, so the
    anatomy is mirrored in patient space. The low-attenuation block is therefore in the
    right lung of one and the left lung of the other, and the capability has to follow the
    affine to see that. Getting this wrong produces a SEG that renders perfectly and is
    mirrored -- the one defect in this module that nothing downstream detects.
    """
    vol, ctx = phantom
    fvol, fctx = phantom_flipped
    normal = seg_mod.segment_lungs(vol, ctx)
    flipped = seg_mod.segment_lungs(fvol, fctx)
    assert normal.diagnostics["laterality"]["laterality_sign"] == 1
    assert flipped.diagnostics["laterality"]["laterality_sign"] == -1

    def laa_side(result, source):
        below = source.hu_array < np.float32(-950.0)
        return (
            int(np.count_nonzero(below & result.right)),
            int(np.count_nonzero(below & result.left)),
        )

    assert laa_side(normal, ctx.source) == (LAA_VOXELS, 0)
    assert laa_side(flipped, fctx.source) == (0, LAA_VOXELS)
    # ... and "the right lung" is a different set of image columns in each case.
    normal_cols = np.flatnonzero(normal.right.any(axis=(0, 1)))
    flipped_cols = np.flatnonzero(flipped.right.any(axis=(0, 1)))
    assert int(normal_cols.min()) == PHANTOM["right_i"][0]
    assert int(flipped_cols.min()) == PHANTOM["left_i"][0]


def test_label_map_is_uint8_on_the_source_grid_with_coded_segments(phantom):
    vol, ctx = phantom
    outcome = LungSegmentation().run(vol, ctx)
    label_map = outcome.label_map
    assert label_map is not None
    assert label_map.array.dtype == np.uint8
    assert label_map.array.shape == ctx.source.hu_array.shape  # SOURCE grid
    assert set(np.unique(label_map.array)) <= {0, 1, 2}
    assert [(s.scheme, s.code) for s in label_map.segments] == [
        ("SCT", "3341006"),  # Right lung
        ("SCT", "44029006"),  # Left lung
    ]
    # CONTRACT.md §5: index i+1 in the array == segments[i]
    assert segment_index(label_map, label_map.segments[0]) == 1
    assert segment_index(label_map, label_map.segments[1]) == 2
    right = mask_for_segment(label_map, label_map.segments[0])
    left = mask_for_segment(label_map, label_map.segments[1])
    assert not (right & left).any()  # the two segments are disjoint by construction
    assert int(right.sum()) == int(left.sum()) == LUNG_VOXELS
    # The right lung is the LOW column range, because the phantom's row cosine is +x.
    assert int(np.flatnonzero(right.any(axis=(0, 1))).min()) == PHANTOM["right_i"][0]
    assert int(np.flatnonzero(left.any(axis=(0, 1))).min()) == PHANTOM["left_i"][0]


def test_lung_segmentation_emits_no_finding_code_only_anatomy(phantom):
    """A threshold cannot assert that what it outlined is normal lung."""
    vol, ctx = phantom
    outcome = LungSegmentation().run(vol, ctx)
    kinds = [f.kind for f in outcome.findings]
    assert kinds == ["lung_right", "lung_left", "lung"]
    for finding in outcome.findings:
        assert finding.score is None  # no calibrated score exists
        assert finding.present is True
        assert len(finding.measurements) == 1
        measurement = finding.measurements[0]
        assert measurement.unit == "ml"
        assert (measurement.name.scheme, measurement.name.code) == ("SCT", "118565006")


def test_outcome_carries_the_consumed_instances(phantom):
    """MOS-IMG-121: the instances actually consumed, excluding dropped duplicates."""
    vol, ctx = phantom
    outcome = LungSegmentation().run(vol, ctx)
    assert outcome.source_sop_instance_uids == tuple(ctx.source.sop_instance_uids)
    assert len(outcome.source_sop_instance_uids) == PHANTOM["n_slices"]
    assert set(outcome.source_sop_instance_uids).isdisjoint(
        ctx.source.dropped_duplicate_sop_instance_uids
    )


def test_plausibility_gate_rejects_an_absurd_volume(phantom, monkeypatch):
    """MOS-SAFE-014 `output_plausibility_gate`, exercised at its own boundary."""
    vol, ctx = phantom
    monkeypatch.setattr(seg_mod, "PLAUSIBLE_TOTAL_ML", (10_000.0, 12_000.0))
    with pytest.raises(CapabilityRejection) as excinfo:
        seg_mod.segment_lungs(vol, ctx)
    assert excinfo.value.reason_code == "service_declined"
    assert "absurdity gate" in excinfo.value.message


def test_scan_extent_rejection_beats_reporting_hardware_as_lung(tmp_path):
    """The LCTSC-S3-102 failure, reproduced in miniature.

    One interior air component that spans the whole scan extent and no other: the earlier
    fallback reported it as a lung. It must reject instead.
    """
    p = PHANTOM
    hu = np.full((p["n_slices"], p["rows"], p["cols"]), p["hu_air"], dtype=np.float32)
    hu[:, slice(*p["body_j"]), slice(*p["body_i"])] = p["hu_tissue"]
    hu[:, 64:84, 16:128] = p["hu_lung"]  # a "couch": spans every slice, k=0 to k=K-1
    _write_phantom_ct(tmp_path, hu)
    vol, src, _ = build_canonical_volume(sorted(tmp_path.glob("*.dcm")))
    ctx = CapabilityContext("job_couch", vol.series_instance_uid, src, "research_only")
    with pytest.raises(CapabilityRejection) as excinfo:
        seg_mod.segment_lungs(vol, ctx)
    assert excinfo.value.reason_code == "service_declined"
    assert "craniocaudal" in excinfo.value.message
    assert excinfo.value.detail["n_spanning_scan_extent"] >= 1


def _write_blocks(tmp_path: Path, blocks: list[tuple[slice, slice, slice]]) -> tuple:
    """Phantom with an arbitrary set of air blocks inside a tissue body. Returns (vol, ctx)."""
    p = PHANTOM
    hu = np.full((p["n_slices"], p["rows"], p["cols"]), p["hu_air"], dtype=np.float32)
    hu[:, 10:158, 10:214] = p["hu_tissue"]
    for k_sl, j_sl, i_sl in blocks:
        hu[k_sl, j_sl, i_sl] = p["hu_lung"]
    _write_phantom_ct(tmp_path, hu)
    vol, src, _ = build_canonical_volume(sorted(tmp_path.glob("*.dcm")))
    ctx = CapabilityContext("job_blocks", vol.series_instance_uid, src, "research_only")
    return vol, ctx, src


def test_the_relative_size_floor_is_applied_after_the_table_is_removed(tmp_path):
    """LCTSC-Test-S3-103, reproduced in miniature: the couch must not delete a lung.

    `mask_from_hu_threshold` computes its relative floor as
    `MIN_COMPONENT_FRACTION * largest_interior_component`. When the largest interior
    component is an immobilisation pad rather than a lung, that floor deletes the smaller
    lung BEFORE `_scan_extent_filter` removes the pad that set it -- and on S3-103 it did
    exactly that, leaving a 1543 ml "pair of lungs" that was one lung cut in half.

    The capability therefore passes `THRESHOLD_STAGE_FRACTION` (zero) to the lifted
    function and applies the relative floor itself, after the pad is gone. This test
    asserts BOTH halves: that the spike's own parameterisation loses the small lung, and
    that the capability keeps it.
    """
    from medos.core.masks import _connected_components_3d, mask_from_hu_threshold

    # Sizes chosen so the two orderings disagree, and so the surviving pair clears
    # PLAUSIBLE_TOTAL_ML (1572.4 ml total):
    #   old floor = 0.2 * couch      = 509.8 ml  >  small (483.8) -> small DELETED
    #   new floor = 0.2 * big lung   = 217.7 ml  <  small (483.8) -> small KEPT
    # The couch stops at row 156 because row 157 is the last tissue row; reaching it would
    # make the couch 6-adjacent to room air and the face filter would drop it, which would
    # quietly remove the very thing this test is about.
    couch = (slice(0, 32), slice(98, 157), slice(12, 212))  # 2548.8 ml, spans k
    big = (slice(2, 30), slice(16, 96), slice(24, 96))  # 1088.6 ml
    small = (slice(2, 30), slice(16, 96), slice(152, 184))  # 483.8 ml
    vol, ctx, src = _write_blocks(tmp_path, [couch, big, small])
    voxel_ml = PHANTOM["pixel_spacing"] ** 2 * PHANTOM["slice_spacing"] / 1000.0

    # Half one: with the spike's own fraction, the relative floor is 0.2 * the COUCH, and
    # the small lung falls under it and is gone before anything can rescue it.
    spike_masks, _ = mask_from_hu_threshold(
        src,
        lung_hu_min=seg_mod.LUNG_HU_MIN,
        lung_hu_max=seg_mod.LUNG_HU_MAX,
        min_component_ml=seg_mod.MIN_COMPONENT_ML,
        min_component_fraction=seg_mod.MIN_COMPONENT_FRACTION,
    )
    _, n_spike = _connected_components_3d(spike_masks["lung"])
    assert n_spike == 2, "expected couch + big lung only; the small lung should be gone"
    assert not spike_masks["lung"][2:30, 16:80, 152:184].any()

    # Half two: the capability defers the relative floor and keeps both lungs.
    result = seg_mod.segment_lungs(vol, ctx)
    assert result.diagnostics["scan_extent"]["n_components_kept"] == 2
    assert result.volume_ml["lung_right"] == pytest.approx(
        28 * 80 * 72 * voxel_ml, rel=1e-9
    )
    assert result.volume_ml["lung_left"] == pytest.approx(
        28 * 80 * 32 * voxel_ml, rel=1e-9
    )


def test_the_cut_is_the_deepest_valley_not_a_split_about_the_centroid(tmp_path):
    """LCTSC-Test-S3-101, reproduced in miniature: an off-centre centroid must not matter.

    The right lung here is 3.6x the left, so the voxel-count centroid (column 105) falls
    INSIDE the right lung rather than between the lungs. The previous rule located its two
    peaks as `argmax` either side of the centroid, which on this profile returns two
    points of the SAME lung, collapses the search window into that lung, and puts the cut
    at column ~64 -- in the middle of the right lung, with the whole of its lateral half
    then reported as the left lung.
    """
    big = (slice(2, 30), slice(16, 80), slice(24, 140))  # cols 24..139
    small = (slice(2, 30), slice(16, 80), slice(176, 208))  # cols 176..207
    vol, ctx, _ = _write_blocks(tmp_path, [big, small])
    voxel_ml = PHANTOM["pixel_spacing"] ** 2 * PHANTOM["slice_spacing"] / 1000.0

    result = seg_mod.segment_lungs(vol, ctx)
    lat = result.diagnostics["laterality"]
    # The cut is in the mediastinal gap, not inside either lung.
    assert 140 <= lat["cut_column_index"] < 176
    assert lat["voxels_at_cut_column"] == 0
    assert lat["valley_prominence_ratio"] == pytest.approx(1.0)
    # ...so each lung is reported whole.
    assert result.volume_ml["lung_right"] == pytest.approx(
        28 * 64 * 116 * voxel_ml, rel=1e-9
    )
    assert result.volume_ml["lung_left"] == pytest.approx(
        28 * 64 * 32 * voxel_ml, rel=1e-9
    )


def test_a_mask_with_no_mediastinal_valley_is_rejected_not_split(tmp_path):
    """One lung-shaped blob has no correct sagittal cut, so there must be no cut.

    Any plane through a single structure yields two plausible per-lung volumes and two
    per-lung %LAA values, all of them measuring the wrong voxels, and no downstream gate
    detects it. The capability declines instead.
    """
    one_blob = (slice(2, 30), slice(16, 112), slice(24, 160))
    vol, ctx, _ = _write_blocks(tmp_path, [one_blob])
    with pytest.raises(CapabilityRejection) as excinfo:
        seg_mod.segment_lungs(vol, ctx)
    assert excinfo.value.reason_code == "service_declined"
    assert "mediastinal valley" in excinfo.value.message
    assert excinfo.value.detail["valley_prominence_ratio"] == pytest.approx(0.0)


def test_the_absurdity_gate_floor_is_above_every_leaked_lung_measured(tmp_path):
    """The 1500 ml floor, and why it is neither 250 ml nor 1000 ml.

    Over all 60 LCTSC cases the two populations do not overlap: masks agreeing with the
    RTSTRUCT contours run 2102.8-7426.4 ml, and the five whose lungs leaked into room air
    run 68.1-1151.0 ml. The floor sits in that gap. Both earlier values did not:
      250 ml  admitted S3-102 (307.9 ml, Dice 0.000) and S3-203 (604.7 ml, 0.252)
      1000 ml admitted Train-S3-002 (1151.0 ml, Dice 0.314)
    """
    low, high = seg_mod.PLAUSIBLE_TOTAL_ML
    assert low == 1500.0 and high == 12000.0
    assert low > 1151.0, "must reject the largest leaked-lung mask measured"
    assert low < 2102.8, "must accept the smallest genuine mask measured"

    # A scrap the size of a leaked remnant is refused rather than measured.
    scrap = (slice(2, 30), slice(16, 48), slice(24, 64))  # 28*32*40 = 241.9 ml
    vol, ctx, _ = _write_blocks(tmp_path, [scrap])
    with pytest.raises(CapabilityRejection) as excinfo:
        seg_mod.segment_lungs(vol, ctx)
    assert excinfo.value.reason_code == "service_declined"
    assert "absurdity gate" in excinfo.value.message


def test_no_lung_component_is_a_rejection_not_a_systemexit(tmp_path):
    """`mask_from_hu_threshold` signals this with SystemExit, which would kill a worker."""
    p = PHANTOM
    hu = np.full((p["n_slices"], p["rows"], p["cols"]), p["hu_air"], dtype=np.float32)
    # a solid body, no air inside it at all
    hu[:, slice(*p["body_j"]), slice(*p["body_i"])] = p["hu_tissue"]
    _write_phantom_ct(tmp_path, hu)
    vol, src, _ = build_canonical_volume(sorted(tmp_path.glob("*.dcm")))
    ctx = CapabilityContext("job_solid", vol.series_instance_uid, src, "research_only")
    with pytest.raises(CapabilityRejection) as excinfo:
        seg_mod.segment_lungs(vol, ctx)
    assert excinfo.value.reason_code == "service_declined"


# =====================================================================================
# emphysema_laa
# =====================================================================================
def _bound_laa(phantom) -> tuple[EmphysemaLaa, CapabilityOutcome]:
    vol, ctx = phantom
    upstream = LungSegmentation().run(vol, ctx)
    return EmphysemaLaa().bind({"lung_segmentation": upstream}), upstream


def test_emphysema_laa_measures_the_phantom_exactly(phantom):
    vol, ctx = phantom
    capability, _ = _bound_laa(phantom)
    outcome = capability.run(vol, ctx)
    values = {
        f.kind: (f.measurements[0].value if f.measurements else None)
        for f in outcome.findings
    }
    assert values["laa_950_lung_right"] == pytest.approx(
        100.0 * LAA_VOXELS / LUNG_VOXELS, rel=0, abs=1e-12
    )
    assert values["laa_950_lung_left"] == pytest.approx(0.0, abs=1e-12)
    assert values["laa_950_lung"] == pytest.approx(
        100.0 * LAA_VOXELS / (2 * LUNG_VOXELS), rel=0, abs=1e-12
    )


def test_emphysema_total_is_voxel_weighted_not_the_mean_of_sides(phantom):
    vol, ctx = phantom
    capability, _ = _bound_laa(phantom)
    outcome = capability.run(vol, ctx)
    values = {f.kind: f.measurements[0].value for f in outcome.findings}
    mean_of_sides = (
        values["laa_950_lung_right"] + values["laa_950_lung_left"]
    ) / 2.0
    # Equal here only because the two lungs have equal volume; assert the identity that
    # actually defines the total instead.
    assert values["laa_950_lung"] == pytest.approx(mean_of_sides, abs=1e-12)
    assert values["laa_950_lung"] == pytest.approx(
        100.0 * LAA_VOXELS / (2 * LUNG_VOXELS), abs=1e-12
    )


def test_emphysema_present_means_voxels_below_threshold_not_a_diagnosis(phantom):
    vol, ctx = phantom
    capability, _ = _bound_laa(phantom)
    outcome = capability.run(vol, ctx)
    by_kind = {f.kind: f for f in outcome.findings}
    assert by_kind["laa_950_lung_right"].present is True
    assert by_kind["laa_950_lung_left"].present is False  # zero voxels below -950
    for finding in outcome.findings:
        assert finding.score is None
        assert "emphysema" not in finding.kind


def test_emphysema_emits_no_label_map(phantom):
    vol, ctx = phantom
    capability, _ = _bound_laa(phantom)
    assert capability.run(vol, ctx).label_map is None


def test_emphysema_measurement_carries_its_qualifiers(phantom):
    """MOS-IMG-119: %LAA is not comparable across kernels or slice thicknesses."""
    vol, ctx = phantom
    capability, _ = _bound_laa(phantom)
    diagnostics = capability.measure(vol, ctx).diagnostics
    assert diagnostics["threshold_hu"] == -950.0
    assert diagnostics["computed_on"] == "source_grid"
    assert diagnostics["convolution_kernel"] == "TESTKERNEL"
    assert diagnostics["delta_s_mm"] == PHANTOM["slice_spacing"]
    right = diagnostics["per_structure"]["laa_950_lung_right"]
    assert right["voxels_below_threshold"] == LAA_VOXELS
    assert right["denominator_voxels"] == LUNG_VOXELS
    assert right["qualifiers"]["threshold_hu"] == -950.0


def test_emphysema_unbound_raises_instead_of_measuring_the_whole_volume(phantom):
    """CONTRACT.md §7: an explicit step ordering, never a hidden import."""
    vol, ctx = phantom
    with pytest.raises(MissingDependency, match="without bind"):
        EmphysemaLaa().run(vol, ctx)


def test_emphysema_bind_refuses_a_missing_or_maskless_dependency(phantom):
    vol, ctx = phantom
    with pytest.raises(MissingDependency, match="lung_segmentation"):
        EmphysemaLaa().bind({})
    maskless = CapabilityOutcome(
        capability_id="lung_segmentation",
        findings=(),
        label_map=None,
        source_sop_instance_uids=(),
    )
    with pytest.raises(MissingDependency, match="label map"):
        EmphysemaLaa().bind({"lung_segmentation": maskless})


def test_emphysema_binding_does_not_mutate_the_registry_entry(phantom):
    registry_entry = REGISTRY["emphysema_laa"]
    bound, _ = _bound_laa(phantom)
    assert registry_entry.upstream is None
    assert bound is not registry_entry
    assert bound.upstream is not None


def test_emphysema_resolves_masks_by_code_not_by_segment_position(phantom):
    """Reordering the upstream segments must not swap left for right."""
    vol, ctx = phantom
    upstream = LungSegmentation().run(vol, ctx)
    assert upstream.label_map is not None
    swapped_array = np.zeros_like(upstream.label_map.array)
    swapped_array[upstream.label_map.array == 1] = 2
    swapped_array[upstream.label_map.array == 2] = 1
    swapped = dataclasses.replace(
        upstream,
        label_map=LabelMap(
            array=swapped_array,
            segments=(
                upstream.label_map.segments[1],
                upstream.label_map.segments[0],
            ),
        ),
    )
    normal = EmphysemaLaa().bind({"lung_segmentation": upstream}).run(vol, ctx)
    reordered = EmphysemaLaa().bind({"lung_segmentation": swapped}).run(vol, ctx)
    assert {f.kind: f.measurements[0].value for f in normal.findings} == {
        f.kind: f.measurements[0].value for f in reordered.findings
    }


def test_emphysema_refuses_a_label_map_that_lacks_the_lung_codes(phantom):
    vol, ctx = phantom
    upstream = LungSegmentation().run(vol, ctx)
    assert upstream.label_map is not None
    wrong = dataclasses.replace(
        upstream,
        label_map=LabelMap(
            array=upstream.label_map.array,
            segments=(CodedConcept("SCT", "80891009", "Heart structure"),),
        ),
    )
    capability = EmphysemaLaa().bind({"lung_segmentation": wrong})
    with pytest.raises(MissingDependency, match="no segment coded"):
        capability.run(vol, ctx)


def test_emphysema_reports_no_number_rather_than_zero_on_a_tiny_mask(phantom):
    """0/0 is not 0.0 %. A fabricated percentage is worse than an absent one."""
    _, ctx = phantom
    tiny = np.zeros(ctx.source.hu_array.shape, dtype=bool)
    tiny[0, 0, 0] = True
    assert laa_mod.laa_for_masks({"tiny": tiny}, ctx)["tiny"] is None


def test_emphysema_applicability_is_independent_of_the_upstream(phantom):
    """It must stay substitutable onto a different lung segmentation later."""
    vol, _ = phantom
    thick = dataclasses.replace(vol, spacing_mm=(8.0, 2.0, 2.0))
    verdict = EmphysemaLaa().applicable(thick)
    assert verdict == "outside_applicability_envelope"


# =====================================================================================
# pleural_effusion
# =====================================================================================
def test_pleural_effusion_returns_an_explicitly_empty_finding(phantom):
    """CONTRACT.md §7, verbatim: present=False with a not_implemented note."""
    vol, ctx = phantom
    outcome = PleuralEffusion().run(vol, ctx)
    assert outcome.capability_id == "pleural_effusion"
    assert outcome.label_map is None
    assert len(outcome.findings) == 1
    finding = outcome.findings[0]
    assert finding.kind == "pleural_effusion.not_implemented"
    assert finding.present is False
    assert finding.score is None  # not 0.0: there is no calibrated score
    assert finding.measurements == ()


def test_pleural_effusion_kind_cannot_be_mistaken_for_the_real_capability():
    assert pe_mod.FINDING_KIND != "pleural_effusion"
    assert pe_mod.FINDING_KIND.startswith("pleural_effusion")
    assert "not_implemented" in pe_mod.FINDING_KIND


def test_pleural_effusion_kind_resolves_to_no_code(phantom):
    """MOS-IMG-112: a writer that tried to code this placeholder MUST raise."""
    concepts = load_concepts()
    with pytest.raises(KeyError, match="MOS-IMG-112"):
        concepts[pe_mod.FINDING_KIND]
    with pytest.raises(KeyError):
        concepts.profile(pe_mod.FINDING_KIND)


def test_pleural_effusion_note_says_what_present_false_means():
    note = pe_mod.NOT_IMPLEMENTED_NOTE
    assert "not implemented" in note.lower()
    assert "NOT" in note and "no pleural effusion is present" in note


def test_pleural_effusion_is_never_rejected(phantom):
    """MOS-SVC-011: a declared capability reports an outcome, never silence."""
    vol, _ = phantom
    assert PleuralEffusion().applicable(vol) is None
    assert PleuralEffusion().applicable(dataclasses.replace(vol, modality="MR")) is None


# =====================================================================================
# PHI (CONTRACT.md §11)
# =====================================================================================
def test_no_phi_reaches_the_outcome_or_the_diagnostics(phantom):
    """UIDs only. The phantom writes PatientName/PatientID so the test can look for them."""
    vol, ctx = phantom
    outcomes: dict[str, CapabilityOutcome] = {}
    blobs: list[str] = []
    for capability_id in execution_order(sorted(REGISTRY)):
        capability = bind_dependencies(REGISTRY[capability_id], outcomes)
        outcomes[capability_id] = capability.run(vol, ctx)
        blobs.append(
            json.dumps(
                [
                    {
                        "kind": f.kind,
                        "present": f.present,
                        "score": f.score,
                        "measurements": [
                            [m.name.scheme, m.name.code, m.name.meaning, m.value, m.unit]
                            for m in f.measurements
                        ],
                    }
                    for f in outcomes[capability_id].findings
                ]
            )
        )
    blobs.append(json.dumps(seg_mod.segment_lungs(vol, ctx).diagnostics, default=str))
    blobs.append(
        json.dumps(
            EmphysemaLaa()
            .bind(outcomes)
            .measure(vol, ctx)
            .diagnostics,
            default=str,
        )
    )
    haystack = " ".join(blobs)
    for phi in ("ZZPHIIDZZ", "ZZPHINAMEZZ"):
        assert phi not in haystack


# =====================================================================================
# Real LCTSC data
# =====================================================================================
@real_data
def test_spike_measurements_are_reproduced_exactly():
    """The week-0 spike's recorded numbers, recomputed through `medos.core`.

    `tests/_recorded/S1-101_write.json` records lung_left = 1781.651579823221 ml and
    LAA-950 = 0.600096670643759 % on LCTSC-Test-S1-101, measured inside the RTSTRUCT
    `Lung_L` contour. Those two values are the sanity anchor for this whole module: if
    they move, the measurement path changed, not the segmentation.

    NOTE this is the RTSTRUCT mask, NOT `lung_segmentation`'s mask. The capability is a
    Hounsfield threshold and gets a different -- and systematically smaller -- mask; see
    `test_lung_segmentation_against_rtstruct_on_s1_101`.
    """
    from medos.core.geometry import scan_series, select_series
    from medos.core.masks import find_rtstruct, mask_from_rtstruct
    from medos.core.measure import measure_laa_percent, measure_volume_ml

    case = LCTSC_ROOT / "LCTSC-Test-S1-101"
    _, paths = select_series(scan_series(case), None)
    vol, src, _ = build_canonical_volume(paths)
    concepts = load_concepts()
    rtstruct = find_rtstruct(case, vol.frame_of_reference_uid)
    masks, _ = mask_from_rtstruct(
        rtstruct, vol, src, concepts.rtstruct_roi_map, roi_names=["Lung_L", "Lung_R"]
    )
    # TWO ABSENCES, TWO GATES. `@real_data` above covers the LCTSC corpus; this file is
    # a different artifact -- a recording the week-0 spike WROTE from that corpus, which
    # `.gitignore` excludes ("generated DICOM derived from patient studies") and no clone
    # has. Having only the first gate meant this test failed rather than skipped wherever
    # the corpus was mounted but the recordings were not.
    #
    # Relative to the repository, not to the working directory: `Path("spikes/...")`
    # resolved against cwd, so the same test passed or failed depending on where pytest
    # was invoked from.
    recording = REPO_ROOT / "tests" / "_recorded" / "S1-101_write.json"
    if not recording.is_file():
        skip_no_data(f"recorded fixture missing: {recording}", corpus="recorded-fixtures")
    recorded = json.loads(recording.read_text(encoding="utf-8"))
    by_structure = {s["structure"]: s for s in recorded["segments"]}
    for structure, mask in (
        ("lung_left", masks["lung_left"]),
        ("lung_right", masks["lung_right"]),
    ):
        spike = {m["concept_key"]: m for m in by_structure[structure]["measurements"]}
        assert int(mask.sum()) == by_structure[structure]["n_voxels"]
        assert measure_volume_ml(mask, src).value == spike["quantity.volume"]["value"]
        assert (
            measure_laa_percent(mask, src, -950.0).value
            == spike["quantity.laa_percent"]["value"]
        )
    # The two numbers named in the task, spelled out.
    assert measure_volume_ml(masks["lung_left"], src).value == 1781.651579823221
    assert measure_laa_percent(masks["lung_left"], src, -950.0).value == (
        0.600096670643759
    )


@real_data
def test_lung_segmentation_against_rtstruct_on_s1_101():
    """The capability's own numbers on LCTSC-Test-S1-101, and its agreement.

    These are the values quoted in the module docstrings. They are asserted with a
    tolerance on the Dice only; the volumes are exact because the method is deterministic.
    """
    from medos.core.geometry import scan_series, select_series
    from medos.core.masks import find_rtstruct, mask_from_rtstruct

    case = LCTSC_ROOT / "LCTSC-Test-S1-101"
    _, paths = select_series(scan_series(case), None)
    vol, src, _ = build_canonical_volume(paths)
    ctx = CapabilityContext("job_s1_101", vol.series_instance_uid, src, "research_only")

    result = seg_mod.segment_lungs(vol, ctx)
    assert result.volume_ml["lung_right"] == pytest.approx(2097.62, abs=0.01)
    assert result.volume_ml["lung_left"] == pytest.approx(1709.58, abs=0.01)
    assert result.volume_ml["lung"] == pytest.approx(3807.20, abs=0.01)

    concepts = load_concepts()
    rtstruct = find_rtstruct(case, vol.frame_of_reference_uid)
    gt, _ = mask_from_rtstruct(
        rtstruct, vol, src, concepts.rtstruct_roi_map, roi_names=["Lung_L", "Lung_R"]
    )

    def dice(a: np.ndarray, b: np.ndarray) -> float:
        return 2.0 * int((a & b).sum()) / (int(a.sum()) + int(b.sum()))

    assert dice(result.total, gt["lung_left"] | gt["lung_right"]) > 0.95
    assert dice(result.right, gt["lung_right"]) > 0.94
    assert dice(result.left, gt["lung_left"]) > 0.95


@real_data
def test_emphysema_laa_on_s1_101_exceeds_the_contour_based_value():
    """LAA-FM-005, asserted rather than only declared.

    The threshold mask includes the tracheal and bronchial lumen, all of it below -950 HU,
    so the capability's %LAA is ABOVE the same measurement inside the RTSTRUCT contour --
    on every case measured. The test pins the direction and the order of magnitude, not
    the exact offset.
    """
    from medos.core.geometry import scan_series, select_series
    from medos.core.masks import find_rtstruct, mask_from_rtstruct
    from medos.core.measure import measure_laa_percent

    case = LCTSC_ROOT / "LCTSC-Test-S1-101"
    _, paths = select_series(scan_series(case), None)
    vol, src, _ = build_canonical_volume(paths)
    ctx = CapabilityContext("job_s1_101", vol.series_instance_uid, src, "research_only")

    upstream = LungSegmentation().run(vol, ctx)
    outcome = EmphysemaLaa().bind({"lung_segmentation": upstream}).run(vol, ctx)
    values = {f.kind: f.measurements[0].value for f in outcome.findings}
    assert values["laa_950_lung"] == pytest.approx(1.774, abs=0.001)

    concepts = load_concepts()
    rtstruct = find_rtstruct(case, vol.frame_of_reference_uid)
    gt, _ = mask_from_rtstruct(
        rtstruct, vol, src, concepts.rtstruct_roi_map, roi_names=["Lung_L", "Lung_R"]
    )
    reference = measure_laa_percent(
        gt["lung_left"] | gt["lung_right"], src, -950.0
    ).value
    delta = values["laa_950_lung"] - reference
    assert 0.0 < delta < 2.0, "LAA-FM-005: the offset is positive and around 1 pp"


@real_data
def test_the_whole_pipeline_runs_on_a_real_case():
    """Registry -> execution_order -> bind -> run, exactly as the worker will."""
    from medos.core.geometry import scan_series, select_series

    case = LCTSC_ROOT / "LCTSC-Test-S1-101"
    _, paths = select_series(scan_series(case), None)
    vol, src, _ = build_canonical_volume(paths)
    ctx = CapabilityContext("job_e2e", vol.series_instance_uid, src, "research_only")

    requested = ["emphysema_laa", "pleural_effusion", "lung_segmentation"]
    outcomes: dict[str, CapabilityOutcome] = {}
    for capability_id in execution_order(requested):
        capability = bind_dependencies(REGISTRY[capability_id], outcomes)
        assert capability.applicable(vol) is None
        outcomes[capability_id] = capability.run(vol, ctx)

    assert set(outcomes) == set(requested)
    assert outcomes["lung_segmentation"].label_map is not None
    assert outcomes["emphysema_laa"].label_map is None
    assert outcomes["pleural_effusion"].findings[0].present is False
    for outcome in outcomes.values():
        assert outcome.source_sop_instance_uids == tuple(src.sop_instance_uids)


@real_data
def test_a_leaked_lung_is_rejected_not_reported():
    """LCTSC-Test-S3-102: step 1 alone reports the couch as a 6185 ml lung, Dice 0.000."""
    from medos.core.geometry import scan_series, select_series

    case = LCTSC_ROOT / "LCTSC-Test-S3-102"
    if not case.exists():
        skip_no_data(
            f"LCTSC-Test-S3-102 not present under {LCTSC_ROOT}", corpus="lctsc-corpus"
        )
    _, paths = select_series(scan_series(case), None)
    vol, src, _ = build_canonical_volume(paths)
    ctx = CapabilityContext("job_s3_102", vol.series_instance_uid, src, "research_only")
    with pytest.raises(CapabilityRejection) as excinfo:
        seg_mod.segment_lungs(vol, ctx)
    assert excinfo.value.reason_code == "service_declined"
    assert excinfo.value.problem_class == "clinical_rejection"


# =====================================================================================
# medos.dicomweb -- the parts that need no server
# =====================================================================================
def test_credentials_never_render_their_value():
    from medos.dicomweb import PacsCredentials

    creds = PacsCredentials(user="svc", password="hunter2", bearer_token=None)
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in str(creds)
    assert "hunter2" not in f"{creds}"
    assert creds.scheme == "basic"
    assert PacsCredentials(bearer_token="tok").scheme == "bearer"
    assert PacsCredentials().scheme == "none"


def test_gateway_config_summary_carries_no_secret():
    from medos.dicomweb import DicomWebGateway, GatewayConfig, PacsCredentials

    gateway = DicomWebGateway(
        GatewayConfig(
            base_url="https://pacs.example/dicom-web",
            credentials=PacsCredentials(user="svc", password="hunter2"),
        )
    )
    blob = json.dumps(gateway.config_summary()) + repr(gateway)
    assert "hunter2" not in blob
    assert gateway.config_summary()["auth_scheme"] == "basic"
    gateway.close()


def test_gateway_reads_credentials_only_from_its_own_env_names():
    from medos.dicomweb import GatewayConfig

    config = GatewayConfig.from_env(
        {
            "MEDOS_DICOMWEB_URL": "https://pacs.example/dw",
            "MEDOS_DICOMWEB_USER": "svc",
            "MEDOS_DICOMWEB_PASSWORD": "hunter2",
            "MEDOS_DICOMWEB_VERIFY_TLS": "false",
        }
    )
    assert config.base_url == "https://pacs.example/dw"
    assert config.credentials.password == "hunter2"
    assert config.verify_tls is False
    assert GatewayConfig.from_env({}).credentials.scheme == "none"


def test_stow_result_implements_mos_img_084_and_not_the_http_rule():
    from medos.dicomweb.client import FailedInstance, StowResult

    ok = StowResult(200, ("1.2.3",), (), 10, 0.1)
    assert ok.ok
    partial = StowResult(
        200,
        ("1.2.3",),
        (FailedInstance("1.2.4", "1.2.840.10008.5.1.4.1.1.2", 272),),
        10,
        0.1,
    )
    assert partial.http_status == 200
    assert not partial.ok, "MOS-IMG-084: 200 with a non-empty FailedSOPSequence is a fail"
    assert not StowResult(202, ("1.2.3",), (), 10, 0.1).ok


def test_multipart_parser_round_trips_a_two_part_body():
    from medos.dicomweb.client import parse_multipart_related

    boundary = "abc123"
    body = (
        b"--abc123\r\nContent-Type: application/dicom\r\n\r\nAAAA\r\n"
        b"--abc123\r\nContent-Type: application/dicom\r\n\r\nBB\r\nBB\r\n"
        b"--abc123--\r\n"
    )
    parts = parse_multipart_related(
        body, f'multipart/related; type="application/dicom"; boundary={boundary}'
    )
    assert [p.content for p in parts] == [b"AAAA", b"BB\r\nBB"]
    assert parts[0].content_type == "application/dicom"


def test_multipart_parser_accepts_the_quoted_boundary_form():
    from medos.dicomweb.client import parse_multipart_related

    body = b"--b1\r\nContent-Type: application/dicom\r\n\r\nX\r\n--b1--\r\n"
    parts = parse_multipart_related(
        body, 'multipart/related; type="application/dicom"; boundary="b1"'
    )
    assert [p.content for p in parts] == [b"X"]


def test_multipart_stream_decoder_reassembles_across_chunk_boundaries():
    """The regression that shredded every instance larger than one socket chunk."""
    from medos.dicomweb.client import _MultipartStreamDecoder

    payloads = [bytes([i]) * 5000 for i in range(1, 4)]
    body = b""
    for blob in payloads:
        body += b"--B\r\nContent-Type: application/dicom\r\n\r\n" + blob + b"\r\n"
    body += b"--B--\r\n"
    for chunk_size in (1, 7, 64, 997, 4096, len(body)):
        decoder = _MultipartStreamDecoder(b"B")
        got = []
        for start in range(0, len(body), chunk_size):
            got.extend(p.content for p in decoder.feed(body[start : start + chunk_size]))
        decoder.finish()
        assert got == payloads, f"chunk_size={chunk_size}"


def test_multipart_stream_decoder_rejects_a_truncated_body():
    from medos.core.errors import TransportFailure
    from medos.dicomweb.client import _MultipartStreamDecoder

    decoder = _MultipartStreamDecoder(b"B")
    list(decoder.feed(b"--B\r\nContent-Type: application/dicom\r\n\r\nXYZ"))
    with pytest.raises(TransportFailure, match="closing boundary"):
        decoder.finish()


def test_sop_uids_from_qido_returns_a_set():
    """MOS-IMG-152 compares sets; a list invites a length comparison."""
    from medos.dicomweb.client import sop_uids_from_qido

    rows = [
        {"00080018": {"Value": ["1.2.3"]}},
        {"00080018": {"Value": ["1.2.4"]}},
        {"00080018": {"Value": ["1.2.3"]}},
        {},
    ]
    assert sop_uids_from_qido(rows) == {"1.2.3", "1.2.4"}
