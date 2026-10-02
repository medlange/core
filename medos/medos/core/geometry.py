# SPDX-License-Identifier: Apache-2.0
"""Canonical volume geometry: the descriptor, the transform pair, the builder.

CONTRACT.md §1: "CanonicalVolume, SourceGeometry, build/inverse". Lifted from
`spikes/week0/build_volume.py` per CONTRACT.md §2 -- a move and a deduplication, not a
rewrite. Every lifted body below is byte-identical to the spike.

------------------------------------------------------------------------------------
CONTRACT.md §2 defect #2: the two SourceGeometry definitions had DIVERGED
------------------------------------------------------------------------------------
`SourceGeometry` existed twice in the spike and, unlike `derive_uid`, the two copies were
not merely duplicated -- they had drifted into two different types describing the same
thing:

  build_volume.py      frozen; paths, hu_array, pixel_spacing_mm=(dR,dC), delta_s_mm,
                       slice_thickness_mm, per-slice IPP, IOP, convolution_kernel,
                       dropped duplicates. Feeds the DICOM writer and every measurement.

  verify_roundtrip.py  MUTABLE; study/series/FrameOfReference UIDs, origin_lps_mm,
                       direction_lps (3x3), spacing_mm=(dC,dR,dS) as numpy arrays,
                       max_jitter_mm, and an `affine` property. No pixels at all.

**Behaviour kept: the build_volume.py definition, in full.** Reasons, in order of weight:

1. It is frozen. MOS-IMG-085 makes object identity a pure function of the inputs and
   MOS-IMG-033 calls the canonical grid "recorded"; a mutable descriptor lets a field be
   changed between the SEG write and the SR write, producing two objects that claim a
   shared provenance they do not have.
2. It carries `hu_array` on the SOURCE grid, which is what makes MOS-IMG-039 enforceable
   ("a measurement computed anywhere else is a defect, not an approximation").
   `measure_volume_ml` shape-checks the mask against it; the verify_roundtrip copy could
   not express that check at all.
3. Its extra state is irreducible (file paths, per-slice positions, dropped duplicates),
   whereas every field unique to the verify_roundtrip copy is either an identifier or is
   DERIVABLE from the build_volume fields.

The verify_roundtrip fields are therefore folded in as (a) four defaulted identity
fields, and (b) `origin_lps_mm` / `direction_lps` / `spacing_mm` / `affine` as
**properties**, so the numbers exist exactly once. `SourceGeometry.from_header_scan`
reproduces the header-only construction path that copy was written for.

The one deliberate relaxation: `hu_array` and `first_dataset_path` are now
`... | None`, because the header-only path genuinely has no pixels. Nothing on the
build path passes None, so nothing on the build path changes.

Spec: MOS-IMG-005, MOS-IMG-009..MOS-IMG-044 (docs/spec/04-imaging-contracts.md
§4.1.1-§4.2.10).
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    import pydicom
except ImportError as exc:  # pragma: no cover - environment problem, not a data problem
    raise SystemExit(
        "pydicom is required. Install with:  pip install pydicom==3.0.1\n"
        f"(import failed: {exc})"
    ) from exc

from medos.core.dicomio import (
    ENHANCED_SOP_CLASSES,
    SUPPORTED_SOP_CLASSES,
    _apply_modality_lut,
    _as_float_tuple,
    _check_rescale_type,
    _decode_stored,
    _substitute_padding,
    scan_series,
    select_series,
)
from medos.core.errors import REJECTION_CODES, GeometryRejection, SeriesSelectionError

__all__ = [
    "CanonicalVolume",
    "SourceGeometry",
    "build_canonical_volume",
    "sort_order_evidence",
    "DEFAULTS",
    "BUILDER_VERSION",
    "EPS_POS_MM",
    "EPS_ANG_DEG",
    "EPS_TILT_DEG",
    "EPS_PIXEL_SPACING_MM",
    "EPS_AFFINE",
    "GAP_FACTOR",
    # re-exported so the single definition is reachable from one import site
    "GeometryRejection",
    "SeriesSelectionError",
    "REJECTION_CODES",
    "SUPPORTED_SOP_CLASSES",
    "ENHANCED_SOP_CLASSES",
    "scan_series",
    "select_series",
]


DEFAULTS: dict[str, Any] = {
    # Development corpus used to exercise this spike. Override with --series-dir.
    "series_dir": r"F:/WorkSpace/PulmoAI/TCIA/LCTSC-Test-S1-101",
    "report_path": None,  # default: <cwd>/build_volume_report.json
    # MOS-IMG-027: padding is substituted AFTER rescale with this value for CT.
    "padding_output_value": -1024.0,
    # SeriesSelector.min_instances (MOS-IMG-010 geometry_insufficient_instances).
    "min_instances": 2,
    # PreprocessingSpec.canonical_geometry.gantry_tilt.max_deg (MOS-IMG-021).
    "gantry_tilt_max_deg": 30.0,
}


BUILDER_VERSION = "medicalos-imaging-spike/0.0.1"


# MOS-IMG-044: the single tolerance table. No other epsilon may exist in the imaging path.
EPS_POS_MM = 0.01  # same-position threshold      (MOS-IMG-014)


EPS_ANG_DEG = 0.1  # orientation consistency      (MOS-IMG-015)


EPS_TILT_DEG = 0.1  # tilt detection               (MOS-IMG-020)


EPS_PIXEL_SPACING_MM = 1e-6  # PixelSpacing equality        (MOS-IMG-015)


EPS_AFFINE = 1e-4  # affine round-trip            (MOS-IMG-033)


GAP_FACTOR = 2.0  # gap factor                   (MOS-IMG-017)


def _uniform_tol(delta_s_med: float) -> float:
    """MOS-IMG-017 UNIFORM band: max(0.01 mm, 0.005 * dS_med)."""
    return max(0.01, 0.005 * abs(delta_s_med))


def _jitter_tol(delta_s_med: float) -> float:
    """MOS-IMG-017 JITTERED band: max(0.05 mm, 0.02 * dS_med)."""
    return max(0.05, 0.02 * abs(delta_s_med))


@dataclass(frozen=True)
class CanonicalVolume:
    """Verbatim field set of MOS-IMG-036, plus the transform pair of §4.2.7.

    Frozen because the platform re-derives this object independently to validate a
    returned ResultBundle (MOS-IMG-141); a mutable descriptor would make "the recorded
    canonical grid" of MOS-IMG-033 a moving target.
    """

    array: np.ndarray  # float32, C-order, (K, J, I) = [k, j, i]
    affine: np.ndarray  # float64, (4, 4), maps (i, j, k, 1) -> LPS mm
    shape: tuple[int, int, int]
    spacing_mm: tuple[float, float, float]  # (delta_s, delta_r, delta_c) along (k, j, i)
    origin_lps_mm: tuple[float, float, float]
    direction_lps: tuple[float, ...]  # 9 floats, column-major (X, Y, n)
    anatomical_code: str
    frame_of_reference_uid: str
    study_instance_uid: str
    series_instance_uid: str
    sop_instance_uids: tuple[str, ...]
    uid_space: str
    modality: str
    value_units: str
    spacing_class: str
    max_jitter_mm: float
    tilt_deg: float
    tilt_corrected: bool
    resampled_from_source: bool
    dropped_duplicate_sop_instance_uids: tuple[str, ...]
    rescale_type_assumed: bool
    pixel_digest: str
    builder_version: str

    # -- transforms ---------------------------------------------------------------

    def index_to_lps(self, ijk: Sequence[float] | np.ndarray) -> np.ndarray:
        """Forward transform (i, j, k) -> LPS mm.

        WHY the argument order is (i, j, k) while the array is [k, j, i]: MOS-IMG-005
        fixes both conventions and forbids silently swapping them, so the swap is made
        explicit here and nowhere else.
        """
        pts = np.atleast_2d(np.asarray(ijk, dtype=np.float64))
        if pts.shape[-1] != 3:
            raise ValueError(f"expected (...,3) index vectors, got {pts.shape}")
        homo = np.concatenate([pts, np.ones((pts.shape[0], 1))], axis=1)
        out = homo @ self.affine.T
        out = out[:, :3]
        return out[0] if np.ndim(ijk) == 1 else out

    def lps_to_index(self, lps: Sequence[float] | np.ndarray) -> np.ndarray:
        """Inverse transform LPS mm -> continuous (i, j, k).

        MOS-IMG-032/033: the inverse must target the *recorded* canonical grid. It is
        therefore computed from `self.affine` - the stored matrix - never from a matrix
        recomputed from the source headers at inverse time.
        """
        pts = np.atleast_2d(np.asarray(lps, dtype=np.float64))
        if pts.shape[-1] != 3:
            raise ValueError(f"expected (...,3) LPS vectors, got {pts.shape}")
        homo = np.concatenate([pts, np.ones((pts.shape[0], 1))], axis=1)
        out = homo @ np.linalg.inv(self.affine).T
        out = out[:, :3]
        return out[0] if np.ndim(lps) == 1 else out

    def voxel_volume_mm3(self) -> float:
        """MOS-IMG-028: V = dC * dR * dS, with dS the PROJECTED spacing, never ||v||."""
        d_s, d_r, d_c = self.spacing_mm
        return float(d_c * d_r * d_s)

    # -- self-check ---------------------------------------------------------------

    def roundtrip_selfcheck(self, n_samples: int = 4096, seed: int = 0) -> dict[str, Any]:
        """Round-trip random and corner indices through forward then inverse.

        MOS-IMG-033 requires the reconstructed geometry to agree element-wise within
        eps_aff = 1e-4 (MOS-IMG-044). A builder that fails this has produced an affine
        that is not invertible in the numerical regime the platform actually uses, which
        would silently corrupt every inverse-transformed label map.
        """
        k, j, i = self.shape
        rng = np.random.default_rng(seed)
        corners = np.array(
            [
                [0.0, 0.0, 0.0],
                [i - 1.0, 0.0, 0.0],
                [0.0, j - 1.0, 0.0],
                [0.0, 0.0, k - 1.0],
                [i - 1.0, j - 1.0, k - 1.0],
                [(i - 1) / 2.0, (j - 1) / 2.0, (k - 1) / 2.0],
            ],
            dtype=np.float64,
        )
        random_pts = np.stack(
            [
                rng.uniform(0.0, max(i - 1, 1e-9), n_samples),
                rng.uniform(0.0, max(j - 1, 1e-9), n_samples),
                rng.uniform(0.0, max(k - 1, 1e-9), n_samples),
            ],
            axis=1,
        )
        pts = np.concatenate([corners, random_pts], axis=0)

        lps = self.index_to_lps(pts)
        back = self.lps_to_index(lps)
        max_err = float(np.max(np.abs(back - pts)))

        det = float(np.linalg.det(self.affine))
        ok = max_err <= EPS_AFFINE and det > 0.0
        if not ok:
            raise AssertionError(
                "affine round-trip self-check FAILED (MOS-IMG-033 / MOS-IMG-030): "
                f"max_index_error={max_err:.3e} (eps_aff={EPS_AFFINE}), det(A)={det:.6g} "
                "(must be > 0 because n = X x Y and all spacings are positive)"
            )
        return {
            "n_points": int(pts.shape[0]),
            "max_index_error": max_err,
            "eps_aff": EPS_AFFINE,
            "det_affine": det,
            "passed": True,
        }

    def descriptor_dict(self) -> dict[str, Any]:
        """JSON-serialisable form (MOS-IMG-036: "serialised as JSON into the job input")."""
        out = dataclasses.asdict(self)
        out.pop("array")
        out["affine"] = self.affine.tolist()
        out["shape"] = list(self.shape)
        out["spacing_mm"] = list(self.spacing_mm)
        out["origin_lps_mm"] = list(self.origin_lps_mm)
        out["direction_lps"] = list(self.direction_lps)
        out["sop_instance_uids"] = list(self.sop_instance_uids)
        out["dropped_duplicate_sop_instance_uids"] = list(
            self.dropped_duplicate_sop_instance_uids
        )
        return out


@dataclass(frozen=True)
class SourceGeometry:
    """Source-grid facts the DICOM writer needs and MUST NOT re-derive from the canonical
    array (MOS-IMG-039: every measurement is computed on the source grid; MOS-IMG-106:
    the SEG carries source PixelSpacing and the projected dS).

    Kept separate from `CanonicalVolume` because MOS-IMG-036 fixes that dataclass's field
    list exactly, and because after a tilt correction or a non-uniform resample the two
    genuinely differ.
    """

    paths: tuple[Path, ...]  # canonical slice order, index k -> file
    sop_instance_uids: tuple[str, ...]
    pixel_spacing_mm: tuple[float, float]  # (delta_r, delta_c) as DICOM PixelSpacing
    delta_s_mm: float  # projected slice spacing (MOS-IMG-016)
    slice_thickness_mm: float | None  # tag value, reporting only (MOS-IMG-016)
    rows: int
    columns: int
    image_position_patient: tuple[tuple[float, float, float], ...]
    image_orientation_patient: tuple[float, ...]
    convolution_kernel: str | None
    hu_array: np.ndarray | None  # (K, J, I) float32 on the SOURCE grid
    first_dataset_path: Path | None
    # Carried here as well as on the descriptor because MOS-IMG-121 makes them an
    # exclusion rule for the SR evidence sequence, and the writer only ever sees this
    # object: "It MUST NOT reference instances that were selected but dropped as
    # duplicates; those appear only in provenance."
    dropped_duplicate_sop_instance_uids: tuple[str, ...] = ()

    # -- merged in from the second (diverged) definition that lived in
    #    spikes/week0/verify_roundtrip.py. CONTRACT.md §2 requires exactly one
    #    SourceGeometry; these are the fields only that copy had. They are defaulted so
    #    the build_canonical_volume construction site is unchanged, and so a caller that
    #    read headers with stop_before_pixels=True (no hu_array) can still describe a
    #    grid.
    study_instance_uid: str = ""
    series_instance_uid: str = ""
    frame_of_reference_uid: str = ""
    max_jitter_mm: float = 0.0

    # -- derived grid geometry (MOS-IMG-005, MOS-IMG-012) --------------------------
    # These were FIELDS on the verify_roundtrip copy and are PROPERTIES here. That is
    # the divergence fix: two stored copies of the same numbers is exactly how the
    # spec's "recorded canonical grid" (MOS-IMG-033) becomes a moving target. There is
    # now one source of truth -- image_position_patient, image_orientation_patient and
    # the spacings -- and everything else is computed from it.

    @property
    def origin_lps_mm(self) -> np.ndarray:
        """(3,) IPP of the first slice in canonical order."""
        return np.asarray(self.image_position_patient[0], dtype=np.float64)

    @property
    def direction_lps(self) -> np.ndarray:
        """(3, 3) with columns X, Y, n. MOS-IMG-012: n = X x Y, in that order."""
        iop = np.asarray(self.image_orientation_patient, dtype=np.float64)
        x_dir, y_dir = iop[0:3], iop[3:6]
        return np.column_stack([x_dir, y_dir, np.cross(x_dir, y_dir)])

    @property
    def spacing_mm(self) -> np.ndarray:
        """(delta_c, delta_r, delta_s) along (i, j, k).

        NOTE the ordering: PixelSpacing is [between rows, between columns] per PS3.3, so
        `pixel_spacing_mm` is (delta_r, delta_c) and this is its reverse plus the
        PROJECTED slice spacing of MOS-IMG-016.
        """
        delta_r, delta_c = self.pixel_spacing_mm
        return np.array([delta_c, delta_r, self.delta_s_mm], dtype=np.float64)

    @property
    def affine(self) -> np.ndarray:
        """(4,4) mapping index vector (i, j, k, 1) -> LPS mm, per MOS-IMG-005."""
        a = np.eye(4, dtype=np.float64)
        direction, spacing = self.direction_lps, self.spacing_mm
        a[:3, 0] = direction[:, 0] * spacing[0]  # increasing column i
        a[:3, 1] = direction[:, 1] * spacing[1]  # increasing row    j
        a[:3, 2] = direction[:, 2] * spacing[2]  # increasing slice  k
        a[:3, 3] = self.origin_lps_mm
        return a

    @classmethod
    def from_header_scan(
        cls,
        *,
        sop_instance_uids: Sequence[str],
        study_instance_uid: str,
        series_instance_uid: str,
        frame_of_reference_uid: str,
        rows: int,
        columns: int,
        image_position_patient: Sequence[Sequence[float]],
        image_orientation_patient: Sequence[float],
        pixel_spacing_mm: tuple[float, float],
        delta_s_mm: float,
        max_jitter_mm: float,
        paths: Sequence[Path] = (),
        slice_thickness_mm: float | None = None,
        convolution_kernel: str | None = None,
    ) -> SourceGeometry:
        """Build from headers alone -- no pixel data, so `hu_array` stays None.

        This is the construction path the verify_roundtrip copy existed for: it reads
        with stop_before_pixels=True because it only needs the grid to check a written
        SEG against. Measurements are NOT possible from an object built this way, and
        `measure_volume_ml` refuses it rather than silently measuring on nothing
        (MOS-IMG-039).
        """
        return cls(
            paths=tuple(paths),
            sop_instance_uids=tuple(sop_instance_uids),
            pixel_spacing_mm=pixel_spacing_mm,
            delta_s_mm=delta_s_mm,
            slice_thickness_mm=slice_thickness_mm,
            rows=rows,
            columns=columns,
            image_position_patient=tuple(
                tuple(float(v) for v in p) for p in image_position_patient
            ),
            image_orientation_patient=tuple(
                float(v) for v in image_orientation_patient
            ),
            convolution_kernel=convolution_kernel,
            hu_array=None,
            first_dataset_path=None,
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            frame_of_reference_uid=frame_of_reference_uid,
            max_jitter_mm=float(max_jitter_mm),
        )


@dataclass
class _SliceMeta:
    path: Path
    sop_instance_uid: str
    sop_class_uid: str
    ipp: np.ndarray  # (3,)
    iop: np.ndarray  # (6,)
    rows: int
    columns: int
    pixel_spacing: tuple[float, float]
    d: float = 0.0  # projected position on n_0


def _angle_deg(a: np.ndarray, b: np.ndarray) -> float:
    cos = float(np.clip(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)), -1.0, 1.0))
    return math.degrees(math.acos(cos))


def _anatomical_code(affine: np.ndarray) -> str:
    """MOS-IMG-038: direction of increasing (k, j, i) as three of {L,R,A,P,S,I}.

    Computed from the affine columns, never assumed: column 0 is the direction of
    increasing i, column 1 of increasing j, column 2 of increasing k.
    """
    letters = (("R", "L"), ("A", "P"), ("I", "S"))  # (negative, positive) per LPS axis
    out = []
    for col in (2, 1, 0):  # (k, j, i)
        v = affine[:3, col]
        axis = int(np.argmax(np.abs(v)))
        out.append(letters[axis][1] if v[axis] > 0 else letters[axis][0])
    return "".join(out)


def build_canonical_volume(
    paths: Iterable[Path],
    *,
    uid_space: str = "source",
    padding_output_value: float = DEFAULTS["padding_output_value"],
    min_instances: int = DEFAULTS["min_instances"],
    allow_tilt_correction: bool = False,
    gantry_tilt_max_deg: float = DEFAULTS["gantry_tilt_max_deg"],
    allow_resample_non_uniform: bool = False,
) -> tuple[CanonicalVolume, SourceGeometry, dict[str, Any]]:
    """Build the canonical volume from single-frame CT/MR instances.

    `allow_tilt_correction` / `allow_resample_non_uniform` stand in for
    PreprocessingSpec.canonical_geometry.{gantry_tilt, non_uniform_spacing}
    (MOS-IMG-017, MOS-IMG-021). Both default to False because MOS-IMG-021 makes
    *reject* the default behaviour; this spike implements the detection and the
    refusal, and does NOT implement the resampling arms (it raises NotImplementedError
    rather than pretending, because MOS-IMG-022 makes a wrong "correction" worse than
    a rejection).

    Returns (CanonicalVolume, SourceGeometry, diagnostics).
    """
    if uid_space not in ("source", "deid"):
        raise ValueError(f"uid_space must be 'source' or 'deid', got {uid_space!r}")

    paths = list(paths)
    diagnostics: dict[str, Any] = {}

    # --- pass 1: headers only -----------------------------------------------------
    metas: list[_SliceMeta] = []
    for path in paths:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        sop_uid = str(getattr(ds, "SOPInstanceUID", path.name))
        sop_class = str(getattr(ds, "SOPClassUID", ""))
        if sop_class in ENHANCED_SOP_CLASSES:
            raise GeometryRejection(
                "geometry_unsupported_sop_class",
                {"sop_class_uid": sop_class},
                f"{ENHANCED_SOP_CLASSES[sop_class]} is in the MOS-IMG-007 table but "
                "per-frame functional-group handling (MOS-IMG-008) is not implemented "
                "by this spike; implement it rather than treating the instance as "
                "single-frame",
            )
        if sop_class not in SUPPORTED_SOP_CLASSES:
            raise GeometryRejection(
                "geometry_unsupported_sop_class",
                {"sop_class_uid": sop_class},
                f"SOP Class {sop_class} is not in the MOS-IMG-007 table "
                f"(file: {path.name})",
            )
        ipp = _as_float_tuple(getattr(ds, "ImagePositionPatient", None), "ImagePositionPatient", sop_uid)
        iop = _as_float_tuple(getattr(ds, "ImageOrientationPatient", None), "ImageOrientationPatient", sop_uid)
        ps = getattr(ds, "PixelSpacing", None)
        if ps is None:
            raise GeometryRejection(
                "geometry_inconsistent_grid",
                {"rows": None, "columns": None, "pixel_spacing_values": [None]},
                f"PixelSpacing absent on {sop_uid}",
            )
        metas.append(
            _SliceMeta(
                path=path,
                sop_instance_uid=sop_uid,
                sop_class_uid=sop_class,
                ipp=np.asarray(ipp, dtype=np.float64),
                iop=np.asarray(iop, dtype=np.float64),
                rows=int(ds.Rows),
                columns=int(ds.Columns),
                pixel_spacing=(float(ps[0]), float(ps[1])),
            )
        )

    if len(metas) < 2:
        raise GeometryRejection(
            "geometry_insufficient_instances",
            {"instances_after_dedup": len(metas), "min_required": max(2, min_instances)},
            f"{len(metas)} instance(s) cannot define a slice spacing",
        )

    # --- MOS-IMG-012: n = X x Y, from the FIRST instance IN FILE ORDER ------------
    first = metas[0]
    x0 = first.iop[0:3] / np.linalg.norm(first.iop[0:3])
    y0 = first.iop[3:6] / np.linalg.norm(first.iop[3:6])
    n0 = np.cross(x0, y0)
    n0 = n0 / np.linalg.norm(n0)

    # --- MOS-IMG-015: grid + orientation consistency ------------------------------
    max_angle = 0.0
    worst_uid = first.sop_instance_uid
    for m in metas:
        if m.rows != first.rows or m.columns != first.columns:
            raise GeometryRejection(
                "geometry_inconsistent_grid",
                {
                    "rows": [first.rows, m.rows],
                    "columns": [first.columns, m.columns],
                    "pixel_spacing_values": [list(first.pixel_spacing), list(m.pixel_spacing)],
                },
                f"Rows/Columns differ: {first.rows}x{first.columns} vs "
                f"{m.rows}x{m.columns} on {m.sop_instance_uid}",
            )
        if (
            abs(m.pixel_spacing[0] - first.pixel_spacing[0]) > EPS_PIXEL_SPACING_MM
            or abs(m.pixel_spacing[1] - first.pixel_spacing[1]) > EPS_PIXEL_SPACING_MM
        ):
            raise GeometryRejection(
                "geometry_inconsistent_grid",
                {
                    "rows": first.rows,
                    "columns": first.columns,
                    "pixel_spacing_values": [list(first.pixel_spacing), list(m.pixel_spacing)],
                },
                f"PixelSpacing differs beyond eps_ps={EPS_PIXEL_SPACING_MM} mm on "
                f"{m.sop_instance_uid}",
            )
        ax = _angle_deg(m.iop[0:3], x0)
        ay = _angle_deg(m.iop[3:6], y0)
        if max(ax, ay) > max_angle:
            max_angle, worst_uid = max(ax, ay), m.sop_instance_uid
    if max_angle > EPS_ANG_DEG:
        raise GeometryRejection(
            "geometry_inconsistent_orientation",
            {"max_angle_deg": max_angle, "sop_instance_uid": worst_uid},
            f"direction cosines vary by {max_angle:.4f} deg > eps_ang={EPS_ANG_DEG} deg",
        )
    diagnostics["max_orientation_angle_deg"] = max_angle

    # --- MOS-IMG-013: sort by d = IPP . n_0 ---------------------------------------
    for m in metas:
        m.d = float(np.dot(m.ipp, n0))
    # Secondary key on SOPInstanceUID keeps the order total and reproducible; primary
    # key is d alone, per MOS-IMG-013. InstanceNumber / SliceLocation / filename are
    # deliberately never consulted.
    metas.sort(key=lambda m: (m.d, m.sop_instance_uid))

    file_order_uids = [m.sop_instance_uid for m in metas]
    diagnostics["input_file_order_was_sorted"] = file_order_uids == sorted(
        file_order_uids, key=lambda u: u
    )

    # --- MOS-IMG-014: ties --------------------------------------------------------
    dropped: list[str] = []
    kept: list[_SliceMeta] = []
    idx = 0
    while idx < len(metas):
        group = [metas[idx]]
        jdx = idx + 1
        while jdx < len(metas) and abs(metas[jdx].d - metas[idx].d) <= EPS_POS_MM:
            group.append(metas[jdx])
            jdx += 1
        if len(group) == 1:
            kept.append(group[0])
        else:
            digests = {}
            for m in group:
                ds = pydicom.dcmread(str(m.path))
                stored = _decode_stored(ds)
                hu = _apply_modality_lut(ds, stored)
                _substitute_padding(ds, stored, hu, padding_output_value)
                digests[m.sop_instance_uid] = hashlib.sha256(
                    hu.astype("<f4", copy=False).tobytes(order="C")
                ).hexdigest()
            unique = set(digests.values())
            if len(unique) > 1:
                raise GeometryRejection(
                    "geometry_duplicate_positions",
                    {
                        "projected_position_mm": group[0].d,
                        "sop_instance_uids": sorted(m.sop_instance_uid for m in group),
                    },
                    f"{len(group)} instances within eps_pos={EPS_POS_MM} mm of "
                    f"d={group[0].d:.4f} carry different pixels",
                )
            winner = min(group, key=lambda m: m.sop_instance_uid)
            kept.append(winner)
            dropped.extend(
                sorted(m.sop_instance_uid for m in group if m is not winner)
            )
        idx = jdx
    metas = kept

    if len(metas) < max(2, min_instances):
        raise GeometryRejection(
            "geometry_insufficient_instances",
            {"instances_after_dedup": len(metas), "min_required": max(2, min_instances)},
            f"{len(metas)} instances remain after de-duplication, "
            f"{max(2, min_instances)} required",
        )

    # --- MOS-IMG-016/017: spacing from positions ----------------------------------
    d_vals = np.array([m.d for m in metas], dtype=np.float64)
    deltas = np.diff(d_vals)
    if np.any(deltas <= 0):
        raise GeometryRejection(
            "geometry_duplicate_positions",
            {
                "projected_position_mm": float(d_vals[int(np.argmin(deltas))]),
                "sop_instance_uids": [m.sop_instance_uid for m in metas[:2]],
            },
            "non-increasing projected positions survived de-duplication; this is a "
            "builder defect, not a data problem",
        )
    delta_s_med = float(np.median(deltas))
    max_jitter = float(np.max(np.abs(deltas - delta_s_med)))

    if np.any(deltas > GAP_FACTOR * delta_s_med):
        bad = int(np.argmax(deltas))
        raise GeometryRejection(
            "geometry_gapped",
            {
                "gap_mm": float(deltas[bad]),
                "median_spacing_mm": delta_s_med,
                "after_sop_instance_uid": metas[bad].sop_instance_uid,
            },
            f"local spacing {deltas[bad]:.4f} mm exceeds {GAP_FACTOR} x median "
            f"{delta_s_med:.4f} mm; never interpolate across a gap (MOS-IMG-017)",
        )

    if max_jitter <= _uniform_tol(delta_s_med):
        spacing_class = "UNIFORM"
    elif max_jitter <= _jitter_tol(delta_s_med):
        spacing_class = "JITTERED"
    else:
        spacing_class = "NON_UNIFORM"
        if not allow_resample_non_uniform:
            raise GeometryRejection(
                "geometry_non_uniform_spacing",
                {"median_spacing_mm": delta_s_med, "max_jitter_mm": max_jitter},
                f"spacing jitter {max_jitter:.4f} mm exceeds the JITTERED tolerance "
                f"{_jitter_tol(delta_s_med):.4f} mm and the spec does not permit "
                "resampling",
            )
        raise NotImplementedError(
            "non_uniform_spacing='resample_to_uniform' (MOS-IMG-017) is not implemented "
            "by this spike. Implement the k-axis resample with the declared "
            "image_interpolator and set resampled_from_source=True, or drop the flag."
        )

    # --- MOS-IMG-020: tilt, both tests, geometric authoritative --------------------
    tag_tilt_deg: float | None = None
    ds_first = pydicom.dcmread(str(metas[0].path), stop_before_pixels=True)
    raw_tag = getattr(ds_first, "GantryDetectorTilt", None)
    if raw_tag is not None:
        tag_tilt_deg = abs(float(raw_tag))
    tag_tilt_present = tag_tilt_deg is not None and tag_tilt_deg > EPS_TILT_DEG

    thetas = []
    for a, b in zip(metas[:-1], metas[1:]):
        v = b.ipp - a.ipp
        norm = float(np.linalg.norm(v))
        if norm == 0.0:
            continue
        cos = float(np.clip(np.dot(v, n0) / norm, -1.0, 1.0))
        thetas.append(math.degrees(math.acos(cos)))
    geom_tilt_deg = float(max(thetas)) if thetas else 0.0
    geom_tilt_present = geom_tilt_deg > EPS_TILT_DEG

    diagnostics["tilt_tag_deg"] = tag_tilt_deg
    diagnostics["tilt_geometric_deg"] = geom_tilt_deg
    diagnostics["tilt_detection_disagreement"] = bool(tag_tilt_present != geom_tilt_present)

    tilt_deg = geom_tilt_deg  # geometric test is authoritative (MOS-IMG-020)
    tilt_corrected = False
    if geom_tilt_present or tag_tilt_present:
        if not allow_tilt_correction:
            raise GeometryRejection(
                "geometry_gantry_tilt",
                {
                    "tilt_deg": tilt_deg,
                    "detection": "geometric" if geom_tilt_present else "tag",
                },
                f"gantry tilt {tilt_deg:.4f} deg (tag: {tag_tilt_deg}) exceeds "
                f"eps_tilt={EPS_TILT_DEG} deg and the spec does not permit correction "
                "(MOS-IMG-021)",
            )
        if geom_tilt_deg > gantry_tilt_max_deg:
            raise GeometryRejection(
                "geometry_tilt_correction_out_of_range",
                {"tilt_deg": geom_tilt_deg, "max_deg": gantry_tilt_max_deg},
                f"tilt {geom_tilt_deg:.4f} deg exceeds gantry_tilt.max_deg="
                f"{gantry_tilt_max_deg}",
            )
        raise NotImplementedError(
            "gantry_tilt mode='correct' (MOS-IMG-021) is not implemented by this spike. "
            "MOS-IMG-022 forbids the cheap alternative (shearing the affine while "
            "leaving voxels in place), so this raises instead of approximating."
        )

    # --- pass 2: pixels -----------------------------------------------------------
    modality = str(getattr(ds_first, "Modality", "CT"))
    rows, cols = metas[0].rows, metas[0].columns
    hu = np.empty((len(metas), rows, cols), dtype=np.float32)
    rescale_type_assumed = False
    n_padded = 0
    for k, m in enumerate(metas):
        ds = pydicom.dcmread(str(m.path))
        rescale_type_assumed |= _check_rescale_type(ds, modality)
        stored = _decode_stored(ds)
        slice_hu = _apply_modality_lut(ds, stored)
        n_padded += _substitute_padding(ds, stored, slice_hu, padding_output_value)
        hu[k] = slice_hu
    diagnostics["padded_voxels_substituted"] = n_padded
    diagnostics["rescale_type_assumed"] = rescale_type_assumed

    # --- MOS-IMG-029: the affine --------------------------------------------------
    # PixelSpacing[0] is between ROWS (along Y) = delta_r; [1] is between COLUMNS
    # (along X) = delta_c. Getting this pair backwards on a non-square-pixel CT is
    # a silent, clinically meaningful error, so it is named here (MOS-IMG-028).
    delta_r, delta_c = metas[0].pixel_spacing
    origin = metas[0].ipp
    affine = np.eye(4, dtype=np.float64)
    affine[:3, 0] = x0 * delta_c
    affine[:3, 1] = y0 * delta_r
    affine[:3, 2] = n0 * delta_s_med
    affine[:3, 3] = origin

    det = float(np.linalg.det(affine))
    if det <= 0.0:
        raise AssertionError(
            f"det(A)={det:.6g} <= 0 (MOS-IMG-030). Because n = X x Y and all spacings "
            "are positive this is impossible for correct code: the builder has a defect."
        )

    pixel_digest = "sha256:" + hashlib.sha256(
        hu.astype("<f4", copy=False).tobytes(order="C")
    ).hexdigest()

    volume = CanonicalVolume(
        array=hu,
        affine=affine,
        shape=(hu.shape[0], hu.shape[1], hu.shape[2]),
        spacing_mm=(delta_s_med, delta_r, delta_c),
        origin_lps_mm=(float(origin[0]), float(origin[1]), float(origin[2])),
        direction_lps=tuple(float(v) for v in np.concatenate([x0, y0, n0])),
        anatomical_code=_anatomical_code(affine),
        frame_of_reference_uid=str(getattr(ds_first, "FrameOfReferenceUID", "")),
        study_instance_uid=str(ds_first.StudyInstanceUID),
        series_instance_uid=str(ds_first.SeriesInstanceUID),
        sop_instance_uids=tuple(m.sop_instance_uid for m in metas),
        uid_space=uid_space,
        modality=modality,
        value_units="HU" if modality == "CT" else "arbitrary",
        spacing_class=spacing_class,
        max_jitter_mm=max_jitter,
        tilt_deg=tilt_deg,
        tilt_corrected=tilt_corrected,
        resampled_from_source=False,
        dropped_duplicate_sop_instance_uids=tuple(dropped),
        rescale_type_assumed=rescale_type_assumed,
        pixel_digest=pixel_digest,
        builder_version=BUILDER_VERSION,
    )

    source = SourceGeometry(
        paths=tuple(m.path for m in metas),
        sop_instance_uids=tuple(m.sop_instance_uid for m in metas),
        pixel_spacing_mm=(delta_r, delta_c),
        delta_s_mm=delta_s_med,
        slice_thickness_mm=(
            float(ds_first.SliceThickness) if getattr(ds_first, "SliceThickness", None) else None
        ),
        rows=rows,
        columns=cols,
        image_position_patient=tuple(tuple(float(v) for v in m.ipp) for m in metas),
        image_orientation_patient=tuple(float(v) for v in metas[0].iop),
        convolution_kernel=(
            str(ds_first.ConvolutionKernel)
            if getattr(ds_first, "ConvolutionKernel", None)
            else None
        ),
        hu_array=hu,
        first_dataset_path=metas[0].path,
        dropped_duplicate_sop_instance_uids=tuple(dropped),
    )
    return volume, source, diagnostics


def sort_order_evidence(paths: Sequence[Path], sorted_uids: Sequence[str]) -> dict[str, Any]:
    """Quantify how badly on-disk order disagrees with geometric order.

    WHY this is reported and not merely asserted: the corpus survey found 1366/1409 TCIA
    series shuffled on disk, and MOS-IMG-013 exists precisely because InstanceNumber and
    filename orderings are the classic source of superior-inferior flips. Showing the
    disagreement is what makes the requirement visibly load-bearing.
    """
    by_uid: dict[str, tuple[int, int]] = {}
    for pos, path in enumerate(paths):
        ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        by_uid[str(ds.SOPInstanceUID)] = (pos, int(getattr(ds, "InstanceNumber", -1)))
    file_pos = [by_uid[u][0] for u in sorted_uids if u in by_uid]
    inst_num = [by_uid[u][1] for u in sorted_uids if u in by_uid]
    return {
        "filename_order_matches_geometric": file_pos == sorted(file_pos),
        "instance_number_order_matches_geometric": inst_num == sorted(inst_num),
        "instance_number_first_five_in_geometric_order": inst_num[:5],
        "n_filename_inversions": int(
            sum(1 for a, b in zip(file_pos[:-1], file_pos[1:]) if b < a)
        ),
    }
