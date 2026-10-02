# SPDX-License-Identifier: Apache-2.0
"""Proof that the week-0 -> medos.core lift changed no behaviour.

CONTRACT.md §2 requires the spike's logic to be MOVED and DEDUPLICATED, not rewritten,
and requires a test asserting that `derive_uid` still produces byte-identical output to
the recorded values.

The three gates, in the order the contract states them:

  1. exactly ONE definition of `derive_uid` and ONE of `SourceGeometry` survive;
  2. `derive_uid` reproduces `uid_determinism.json` byte for byte;
  3. a canonical volume built from a real LCTSC case through `medos.core` reproduces the
     `pixel_digest` recorded in `build_volume_S1-101.json`.

THE SPIKE IS DELETED AND THESE RECORDINGS ARE WHAT REMAINS OF IT. They were
`spikes/week0/out/`; they are `tests/_recorded/` now, and they are the only reason gates 2
and 3 still mean anything -- a byte-identity check whose reference disappears with the
directory it lived in is a byte-identity check against the current code. The fourth gate,
"the frozen spike imports from `medos.core` rather than redefining them", went with the
spike: there is no second definition left to import from anywhere.

WHY gate 3 hashes pixels rather than comparing a shape. MOS-IMG-013 makes canonical order
a function of projected position, and MOS-IMG-023/024/027 make the HU of each voxel a
function of per-slice decode, Modality LUT and padding substitution. A lift that broke any
of those would still produce a (130, 512, 512) float32 array of entirely plausible
numbers. The digest is the only assertion that catches a silent reorder or a rescale
applied once per series instead of once per slice.

Gate 3 is skipped, never failed, when the read-only TCIA corpus is absent: the corpus is
not part of the repository and a machine without it must still be able to run the suite.

No PHI in this file or its output: UIDs, counts and digests only (CONTRACT.md §11).

Run:
    pytest tests/unit/test_core_lift.py -v
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
#: Gone. The lift's other half -- the recordings -- is `OUT` below.
#: (Was `REPO / "spikes" / "week0"`.)
#: The recordings the spike produced, moved out from under it. They are
#: `.gitignore`d -- "generated DICOM derived from patient studies" -- so they are in
#: no clone; a fixture is the test's, and should not be the last thing keeping a
#: directory alive. `tests/_support/skips.py` declares them as `recorded-fixtures`.
OUT = REPO / "tests" / "_recorded"
CORE = REPO / "medos" / "medos" / "core"

# The development corpus the recorded fixture was produced from. Read-only.
LCTSC_CASE = Path("F:/WorkSpace/PulmoAI/TCIA/LCTSC-Test-S1-101")

# `medos/` is a product directory now; the package is `medos/medos/`. Both roots go
# on the path, in the order `[tool.pytest.ini_options] pythonpath` uses.
sys.path.insert(0, str(REPO / "medos"))
sys.path.insert(0, str(REPO))

from medos.core.geometry import SourceGeometry, build_canonical_volume  # noqa: E402
from medos.core.uids import (  # noqa: E402
    JobIdentity,
    check_uid_length,
    deid_uid,
    derive_idempotency_key,
    derive_uid,
)

from tests._support.skips import skip_no_data  # noqa: E402

# ======================================================================================
# Gate 1 - the deduplication actually happened
# ======================================================================================

#: Was `("build_volume.py", "write_dicom_results.py", "verify_roundtrip.py")` -- the
#: three spike modules scanned alongside the platform for a second definition. With the
#: spike deleted the scan is the platform alone, which is a STRONGER statement than it
#: looks: the duplication CONTRACT.md section 2 named cannot recur in a tree that no
#: longer has the other copy.


def _toplevel_defs(path: Path) -> list[str]:
    """Names bound by a top-level def / class / assignment in `path`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


@pytest.mark.parametrize("symbol", ["derive_uid", "SourceGeometry"])
def test_symbol_is_defined_exactly_once_in_the_repository(symbol: str) -> None:
    """CONTRACT.md §2: both were defined TWICE in the spike. Now: once, in medos.core."""
    definers = [
        p.relative_to(REPO).as_posix()
        for p in sorted((REPO / "medos" / "medos").rglob("*.py"))
        if symbol in _toplevel_defs(p)
    ]
    assert definers == [
        {
            "derive_uid": "medos/medos/core/uids.py",
            "SourceGeometry": "medos/medos/core/geometry.py",
        }[symbol]
    ], f"{symbol} is defined in {definers}; CONTRACT.md §2 allows exactly one site"


def test_merged_source_geometry_carries_both_lineages() -> None:
    """The build_volume fields survive; the verify_roundtrip fields are folded in.

    The verify_roundtrip copy stored `origin_lps_mm` / `direction_lps` / `spacing_mm`;
    here they are derived, so there is one source of truth for the grid (MOS-IMG-033).
    """
    fields = {f.name for f in SourceGeometry.__dataclass_fields__.values()}
    # from build_volume.py -- the lineage that was kept
    assert {
        "paths",
        "sop_instance_uids",
        "pixel_spacing_mm",
        "delta_s_mm",
        "slice_thickness_mm",
        "rows",
        "columns",
        "image_position_patient",
        "image_orientation_patient",
        "convolution_kernel",
        "hu_array",
        "first_dataset_path",
        "dropped_duplicate_sop_instance_uids",
    } <= fields
    # from verify_roundtrip.py -- identity fields it alone had
    assert {
        "study_instance_uid",
        "series_instance_uid",
        "frame_of_reference_uid",
        "max_jitter_mm",
    } <= fields
    # ...and its stored grid, now derived rather than stored a second time
    for derived in ("origin_lps_mm", "direction_lps", "spacing_mm", "affine"):
        assert derived not in fields, f"{derived} must be a property, not a stored field"
        assert isinstance(getattr(SourceGeometry, derived), property)
    assert SourceGeometry.__dataclass_params__.frozen, "MOS-IMG-085: identity is immutable"


def test_merged_source_geometry_affine_matches_the_verify_roundtrip_formula() -> None:
    """The derived affine equals what the deleted copy computed from stored arrays."""
    geom = SourceGeometry.from_header_scan(
        sop_instance_uids=["1.2.3.1", "1.2.3.2", "1.2.3.3"],
        study_instance_uid="1.2.3",
        series_instance_uid="1.2.3.0",
        frame_of_reference_uid="1.2.3.9",
        rows=8,
        columns=8,
        image_position_patient=[(-10.0, -20.0, -30.0), (-10.0, -20.0, -27.5),
                                (-10.0, -20.0, -25.0)],
        image_orientation_patient=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
        pixel_spacing_mm=(0.5, 0.75),  # (delta_r, delta_c) per PS3.3
        delta_s_mm=2.5,
        max_jitter_mm=0.0,
    )
    # verbatim from the deleted verify_roundtrip.SourceGeometry.affine
    x_dir, y_dir = np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0])
    direction = np.column_stack([x_dir, y_dir, np.cross(x_dir, y_dir)])
    spacing = np.array([0.75, 0.5, 2.5])  # (delta_c, delta_r, delta_s) along (i, j, k)
    expected = np.eye(4)
    expected[:3, 0] = direction[:, 0] * spacing[0]
    expected[:3, 1] = direction[:, 1] * spacing[1]
    expected[:3, 2] = direction[:, 2] * spacing[2]
    expected[:3, 3] = np.array([-10.0, -20.0, -30.0])

    assert np.array_equal(geom.affine, expected)
    assert np.array_equal(geom.spacing_mm, spacing)
    assert np.array_equal(geom.direction_lps, direction)
    assert geom.hu_array is None  # header-only scan: no pixels, so no measurements


def test_derive_uid_refuses_bad_enums_with_a_diagnosable_message() -> None:
    """The merge kept verify_roundtrip's messages over `raise ValueError(uid_kind)`."""
    kwargs = {
        "org_root": None, "tenant_id": "t", "idempotency_key": "i", "service_id": "s",
        "service_version": "1", "model_id": "m", "model_version": "1", "output_index": 0,
    }
    with pytest.raises(ValueError, match=r"unknown uid_kind 'seg'"):
        derive_uid(uid_space="source", uid_kind="seg", **kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match=r"uid_space must be 'source' or 'deid'"):
        derive_uid(uid_space="anonymised", uid_kind="seg.series", **kwargs)  # type: ignore[arg-type]


# ======================================================================================
# Gate 2 - derive_uid reproduces out/uid_determinism.json byte for byte
# ======================================================================================

# The identity `verify_determinism()` in write_dicom_results.py recorded the fixture
# under. Transcribed here so this test does not import the spike: if the spike's defaults
# ever drift, this test must fail rather than drift with it.
RECORDED_IDENTITY = {
    "tenant_id": "t-1",
    "service_id": "svc.demo",
    "service_version": "1.0.0",
    "study_instance_uid": "1.2.3.4",
    "selected_series_uids": ("1.2.3.4.5", "1.2.3.4.6"),
}
RECORDED_MODEL = {"model_id": "m", "model_version": "1"}
RECORDED_ORG_ROOT = "1.2.826.0.1.3680043.10.999"
RECORDED_DEID_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
RECORDED_DEID_SOURCE_UID = "1.2.3.4.5"


@pytest.fixture(scope="module")
def uid_determinism() -> dict:
    path = OUT / "uid_determinism.json"
    if not path.exists():
        # NOT an assert. `.gitignore` excludes `tests/_recorded/` on purpose -- "generated
        # DICOM derived from patient studies" -- so this file is in NO clone of this
        # repository. A bare assert made the unit suite fail on every machine but the one
        # the recordings happen to sit on; measured, renaming that directory away turned
        # this file into 1 failure and 14 errors. `skip_no_data` is the taxonomy this
        # repository already has for exactly this: skipped by default and under
        # `--require-stack`, FAILED under `--require-corpus`, which is the nightly job
        # that mounts the data.
        skip_no_data(f"recorded fixture missing: {path}", corpus="recorded-fixtures")
    return json.loads(path.read_text(encoding="utf-8"))


def test_idempotency_key_matches_the_recording(uid_determinism: dict) -> None:
    """MOS-EXEC-053: the key is a pure function of the declared material set."""
    assert (
        derive_idempotency_key(**RECORDED_IDENTITY) == uid_determinism["idempotency_key"]
    )


@pytest.mark.parametrize(
    ("recorded_key", "uid_kind", "org_root", "uid_space"),
    [
        ("seg_series_uid", "seg.series", None, "source"),
        ("seg_instance_uid", "seg.instance", None, "source"),
        ("sr_series_uid", "sr.series", None, "source"),
        ("deid_space_seg_series_uid", "seg.series", None, "deid"),
        ("org_root_seg_series_uid", "seg.series", RECORDED_ORG_ROOT, "source"),
    ],
)
def test_derive_uid_reproduces_the_recording(
    uid_determinism: dict, recorded_key: str, uid_kind: str,
    org_root: str | None, uid_space: str,
) -> None:
    """CONTRACT.md §2's named assertion: byte-identical to out/uid_determinism.json.

    MOS-IMG-062/085. Covers both encodings (2.25. uuid5 and org-root SHA-256), both
    uid_spaces, and the series/instance split -- i.e. every branch of `derive_uid`.
    """
    produced = derive_uid(
        org_root=org_root,
        tenant_id=RECORDED_IDENTITY["tenant_id"],
        idempotency_key=derive_idempotency_key(**RECORDED_IDENTITY),
        service_id=RECORDED_IDENTITY["service_id"],
        service_version=RECORDED_IDENTITY["service_version"],
        model_id=RECORDED_MODEL["model_id"],
        model_version=RECORDED_MODEL["model_version"],
        uid_space=uid_space,
        uid_kind=uid_kind,
        output_index=0,
    )
    recorded = uid_determinism[recorded_key]
    assert produced == recorded, (
        f"{recorded_key}: derive_uid produced {produced!r}, the spike recorded "
        f"{recorded!r}. MOS-IMG-085 makes this a re-identification of every object "
        "the deployment has ever written."
    )
    assert produced.encode("ascii") == recorded.encode("ascii")  # byte-for-byte
    check_uid_length(produced, org_root)


def test_deid_uid_reproduces_the_recording(uid_determinism: dict) -> None:
    """MOS-DATA-031/032: the same source UID maps to the same surrogate forever."""
    produced = deid_uid(RECORDED_DEID_KEY, RECORDED_DEID_SOURCE_UID)
    assert produced == uid_determinism[f"deid_uid_of_{RECORDED_DEID_SOURCE_UID}"]


def test_job_identity_routes_through_the_single_derive_uid(uid_determinism: dict) -> None:
    """JobIdentity.uid() must agree with the free function it delegates to."""
    identity = JobIdentity(
        tenant_id=RECORDED_IDENTITY["tenant_id"],
        service_id=RECORDED_IDENTITY["service_id"],
        service_version=RECORDED_IDENTITY["service_version"],
        model_id=RECORDED_MODEL["model_id"],
        model_version=RECORDED_MODEL["model_version"],
        deployment_id="dep_week0_spike",
        institution_name="MedicalOS Week-0 Spike",
        legal_manufacturer_name="MedicalOS B.V.",
        medicalos_version="0.2.0",
        preprocessing_spec_version="0.1.0",
        preprocessing_spec_digest="sha256:" + "0" * 64,
        study_instance_uid=RECORDED_IDENTITY["study_instance_uid"],
        selected_series_uids=RECORDED_IDENTITY["selected_series_uids"],
        requested_outputs=("SEG", "SR"),  # derive_idempotency_key's default
        parameters={},
        uid_space="source",
        org_root=None,
        series_number_band=9000,
        clinical_use_mode="RESEARCH_ONLY",
        job_id="job_test",
    )
    assert identity.idempotency_key == uid_determinism["idempotency_key"]
    assert identity.uid("seg.series", 0) == uid_determinism["seg_series_uid"]
    assert identity.uid("sr.series", 0) == uid_determinism["sr_series_uid"]


def test_derive_uid_is_stable_across_processes(uid_determinism: dict) -> None:
    """MOS-DATA-031. A fresh interpreter must agree; PYTHONHASHSEED must not matter."""
    ident = {**RECORDED_IDENTITY, "selected_series_uids": list(
        RECORDED_IDENTITY["selected_series_uids"]
    )}
    code = (
        f"import sys,json;sys.path.insert(0,{str(REPO / 'medos')!r});"
        # The CHILD gets the package root too. Without it this subprocess imports
        # nothing, and the assertion below compares a CalledProcessError to a UID.
        "from medos.core.uids import derive_uid,derive_idempotency_key;"
        f"i=json.loads({json.dumps(ident)!r});m=json.loads({json.dumps(RECORDED_MODEL)!r});"
        "k=derive_idempotency_key(**i);"
        "print(derive_uid(org_root=None,idempotency_key=k,uid_space='source',"
        "uid_kind='seg.series',output_index=0,tenant_id=i['tenant_id'],"
        "service_id=i['service_id'],service_version=i['service_version'],**m))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == uid_determinism["seg_series_uid"]


# ======================================================================================
# Gate 3 - a real LCTSC case rebuilt through medos.core reproduces the pixel digest
# ======================================================================================


@pytest.fixture(scope="module")
def recorded_build() -> dict:
    path = OUT / "build_volume_S1-101.json"
    if not path.exists():
        # NOT an assert. `.gitignore` excludes `tests/_recorded/` on purpose -- "generated
        # DICOM derived from patient studies" -- so this file is in NO clone of this
        # repository. A bare assert made the unit suite fail on every machine but the one
        # the recordings happen to sit on; measured, renaming that directory away turned
        # this file into 1 failure and 14 errors. `skip_no_data` is the taxonomy this
        # repository already has for exactly this: skipped by default and under
        # `--require-stack`, FAILED under `--require-corpus`, which is the nightly job
        # that mounts the data.
        skip_no_data(f"recorded fixture missing: {path}", corpus="recorded-fixtures")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rebuilt(recorded_build: dict):
    """Rebuild LCTSC-Test-S1-101 through medos.core. The corpus is READ-ONLY."""
    if not LCTSC_CASE.exists():
        skip_no_data(
            f"TCIA corpus not present at {LCTSC_CASE}", corpus="lctsc-corpus"
        )
    from medos.core.dicomio import scan_series, select_series

    groups = scan_series(LCTSC_CASE)
    series_uid, paths = select_series(
        groups, recorded_build["series_instance_uid"]
    )
    volume, source, diagnostics = build_canonical_volume(paths)
    return volume, source, diagnostics


def test_pixel_digest_matches_the_recording(rebuilt, recorded_build: dict) -> None:
    """The gate CONTRACT.md §2 cares about most: identical voxels, not merely a
    identical shape.

    A digest mismatch means the lift changed the canonical slice order (MOS-IMG-013),
    the per-slice Modality LUT (MOS-IMG-024), or the padding substitution
    (MOS-IMG-027) -- each of which produces a plausible-looking, wrong volume.
    """
    volume, _source, _diag = rebuilt
    recorded = recorded_build["canonical_volume"]
    assert volume.pixel_digest == recorded["pixel_digest"]
    assert volume.builder_version == recorded["builder_version"]


def test_canonical_descriptor_matches_the_recording(rebuilt, recorded_build: dict) -> None:
    """MOS-IMG-036's field set, round-tripped through the same JSON form the spike wrote."""
    volume, _source, _diag = rebuilt
    produced = volume.descriptor_dict()
    recorded = recorded_build["canonical_volume"]

    # The recorded JSON rounds the affine and the spacings for readability; compare those
    # numerically and everything else exactly.
    numeric = {"affine", "spacing_mm", "origin_lps_mm", "direction_lps", "max_jitter_mm"}
    for key, expected in recorded.items():
        if key in numeric:
            assert np.allclose(
                np.asarray(produced[key], dtype=float),
                np.asarray(expected, dtype=float),
                atol=1e-6,
            ), key
        else:
            assert produced[key] == expected, key

    assert volume.anatomical_code == "SPL"  # MOS-IMG-038
    assert volume.spacing_class == "UNIFORM"  # MOS-IMG-017
    assert len(volume.sop_instance_uids) == 130


def test_source_geometry_matches_the_recording(rebuilt, recorded_build: dict) -> None:
    """The merged SourceGeometry reproduces every source-grid fact the spike recorded."""
    volume, source, _diag = rebuilt
    recorded = recorded_build["source_geometry"]

    assert list(source.pixel_spacing_mm) == pytest.approx(
        recorded["pixel_spacing_mm"], abs=1e-6
    )
    assert source.delta_s_mm == pytest.approx(recorded["delta_s_mm"], abs=1e-9)
    assert source.slice_thickness_mm == recorded["slice_thickness_tag_mm"]
    assert source.rows == recorded["rows"]
    assert source.columns == recorded["columns"]
    assert source.convolution_kernel == recorded["convolution_kernel"]
    assert volume.voxel_volume_mm3() == pytest.approx(
        recorded["voxel_volume_mm3"], rel=1e-12
    )
    assert source.hu_array is not None
    assert source.hu_array.shape == tuple(recorded_build["canonical_volume"]["shape"])

    # The merged properties must agree with the canonical volume's recorded grid.
    assert np.allclose(source.affine, volume.affine, atol=1e-9)
    assert np.allclose(source.origin_lps_mm, np.asarray(volume.origin_lps_mm), atol=1e-9)


def test_affine_roundtrip_selfcheck_still_passes(rebuilt, recorded_build: dict) -> None:
    """MOS-IMG-033: forward then inverse agrees within eps_aff = 1e-4 (MOS-IMG-044)."""
    volume, _source, _diag = rebuilt
    check = volume.roundtrip_selfcheck()
    recorded = recorded_build["affine_roundtrip_selfcheck"]
    assert check["passed"] is True
    assert check["n_points"] == recorded["n_points"]
    assert check["max_index_error"] <= recorded["eps_aff"]
    assert check["det_affine"] == pytest.approx(recorded["det_affine"], rel=1e-9)


def test_measurement_still_refuses_a_non_source_grid(rebuilt) -> None:
    """MOS-IMG-039 survived the lift: a mask on the wrong grid is refused, not resized."""
    from medos.core.measure import laa_percent, measure_laa_percent, measure_volume_ml

    assert laa_percent is measure_laa_percent  # CONTRACT.md §1 name + spike alias

    _volume, source, _diag = rebuilt
    wrong_grid = np.zeros((4, 8, 8), dtype=bool)
    with pytest.raises(ValueError, match="MOS-IMG-039"):
        measure_volume_ml(wrong_grid, source)


# ======================================================================================
# The two modules CONTRACT.md asked for that had no spike ancestor
# ======================================================================================


def test_result_bundle_matches_contract_section_5_exactly() -> None:
    """CONTRACT.md §5: "Use these exact field names." Field names AND order.

    Asserted mechanically because §5 is a cross-component contract: the writer, the
    worker and the API all destructure these, and a silently renamed field is exactly
    the class of drift the contract exists to prevent.
    """
    import dataclasses

    from medos.core import bundle

    expected = {
        "CodedConcept": ["scheme", "code", "meaning"],
        "Measurement": ["name", "value", "unit"],
        "Finding": ["kind", "present", "score", "measurements"],
        "LabelMap": ["array", "segments"],
        "CapabilityOutcome": [
            "capability_id", "findings", "label_map", "source_sop_instance_uids"
        ],
        "ResultBundle": ["outcomes"],
    }
    for name, fields in expected.items():
        cls = getattr(bundle, name)
        assert [f.name for f in dataclasses.fields(cls)] == fields, name
        assert cls.__dataclass_params__.frozen, f"{name} must be frozen"


def test_bundle_measurement_is_built_from_a_dictionary_code_not_a_literal() -> None:
    """MOS-IMG-112: the writer MUST raise rather than invent a code."""
    import dataclasses

    from medos.capabilities.base import load_concepts
    from medos.core.measure import Measurement, to_bundle_measurement

    # The dictionary moved INTO the package that reads it when the spike was deleted;
    # `default_concepts_path()` is the one place that knows where, and asking it here is
    # what keeps this test from being a second opinion about the location.
    concepts = load_concepts()
    source_side = Measurement(
        concept_key="quantity.laa_percent",
        value=12.5,
        ucum_unit="%",
        ucum_unit_meaning="%",
        computation_geometry="source",
        qualifiers={"threshold_hu": -950.0},
    )
    converted = to_bundle_measurement(source_side, concepts)
    assert converted.name.scheme == "99MEDOS"  # MOS-IMG-114: private, review each release
    assert converted.name.code == "LAA950"
    assert converted.value == 12.5
    assert converted.unit == "%"

    # A measurement from model space must be refused, not converted (CONTRACT.md §5).
    model_space = dataclasses.replace(source_side, computation_geometry="model")
    with pytest.raises(ValueError, match="MOS-IMG-039"):
        to_bundle_measurement(model_space, concepts)

    # An unknown concept must raise, never fall back to a guessed code.
    unknown = dataclasses.replace(source_side, concept_key="quantity.invented")
    with pytest.raises(KeyError, match="MOS-IMG-112"):
        to_bundle_measurement(unknown, concepts)


@pytest.mark.parametrize(
    ("exc_type", "problem_class", "status", "retryable"),
    [
        ("ClinicalRejection", "clinical_rejection", 422, False),
        ("TransportFailure", "transport_failure", 502, True),
        ("SystemFailure", "system_failure", 500, False),
    ],
)
def test_error_hierarchy_maps_to_the_rfc_9457_class_enum(
    exc_type: str, problem_class: str, status: int, retryable: bool
) -> None:
    """CONTRACT.md §9: the `class` field separates clinical from transport from system."""
    from medos.core import errors

    exc = getattr(errors, exc_type)("some_reason", {"series_instance_uid": "1.2.3"}, "msg")
    assert isinstance(exc, errors.MedosError)
    problem = exc.to_problem(instance="/api/v1/jobs/job_1")
    assert problem["class"] == problem_class
    assert problem["status"] == status
    assert problem["retryable"] is retryable
    assert problem["type"] == "urn:medos:problem:some_reason"
    assert problem["instance"] == "/api/v1/jobs/job_1"
    assert problem["detail_fields"] == {"series_instance_uid": "1.2.3"}


def test_geometry_rejection_is_a_clinical_rejection_not_a_failure() -> None:
    """CONTRACT.md §3 + MOS-IMG-011: REJECTED is an outcome, FAILED is an incident.

    This is the whole reason `GeometryRejection` moved under the hierarchy: the worker
    decides the terminal job state from the exception type, so a geometry rejection that
    inherited plain Exception would be indistinguishable from a crash.
    """
    from medos.core.errors import REJECTION_CODES, ClinicalRejection, GeometryRejection

    exc = GeometryRejection(
        "geometry_gantry_tilt", {"tilt_deg": 11.3}, "tilt exceeds the envelope"
    )
    assert isinstance(exc, ClinicalRejection)
    assert exc.problem_class == "clinical_rejection"
    assert exc.http_status == 422
    assert exc.retryable is False
    # lifted behaviour, unchanged
    assert exc.reason_code == "geometry_gantry_tilt"
    assert exc.detail == {"tilt_deg": 11.3}
    assert str(exc) == "[geometry_gantry_tilt] tilt exceeds the envelope"
    assert exc.to_dict()["reason_code"] == "geometry_gantry_tilt"
    # MOS-IMG-010 is a CLOSED enum: adding a code is a spec minor version.
    assert len(REJECTION_CODES) == 11
    with pytest.raises(ValueError, match="closed enum"):
        GeometryRejection("geometry_made_up", {}, "nope")
