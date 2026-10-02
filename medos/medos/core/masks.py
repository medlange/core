# SPDX-License-Identifier: Apache-2.0
"""Mask sources: HU-threshold lung segmentation and RTSTRUCT rasterisation.

CONTRACT.md §1: "mask_from_hu_threshold, mask_from_rtstruct, components". Lifted verbatim
from `spikes/week0/write_dicom_results.py`.

Both produce masks on the **SOURCE** grid, never on a resampled array. MOS-IMG-039 makes
the frame part of the measurement's meaning: "A measurement computed anywhere else is a
defect, not an approximation."

CONTRACT.md §7 labels the threshold segmentation honestly -- it is not a learned model.

Spec: MOS-IMG-032, MOS-IMG-033, MOS-IMG-039, MOS-IMG-041.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pydicom

from medos.core.geometry import CanonicalVolume, SourceGeometry

__all__ = [
    "mask_from_hu_threshold",
    "mask_from_rtstruct",
    "find_rtstruct",
]


def _connected_components_3d(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """6-connected labelling in pure numpy, via run-length encoding + union-find.

    WHY not scipy.ndimage.label: the spec's dependency budget for this path is
    numpy/pydicom/pynrrd/highdicom/requests. Run-based union-find keeps the Python-level
    loop proportional to the number of runs (~10^5 for a lung mask) instead of the number
    of voxels (~3.4x10^7), which is what makes the pure-numpy version tractable.

    Returns (labels int32 with 0 = background, n_labels).
    """
    k_dim, j_dim, i_dim = mask.shape
    flat = mask.reshape(k_dim * j_dim, i_dim)
    padded = np.zeros((flat.shape[0], i_dim + 2), dtype=bool)
    padded[:, 1:-1] = flat
    diff = padded[:, 1:].astype(np.int8) - padded[:, :-1].astype(np.int8)
    starts = np.argwhere(diff == 1)
    ends = np.argwhere(diff == -1)
    if starts.size == 0:
        return np.zeros(mask.shape, dtype=np.int32), 0
    run_row = starts[:, 0].astype(np.int64)
    run_i0 = starts[:, 1].astype(np.int64)
    run_i1 = ends[:, 1].astype(np.int64)  # exclusive
    n_runs = run_row.size

    # runs are already ordered by (row, i0); index them per row with searchsorted
    row_start = np.searchsorted(run_row, np.arange(k_dim * j_dim), side="left")
    row_end = np.searchsorted(run_row, np.arange(k_dim * j_dim), side="right")

    parent = np.arange(n_runs, dtype=np.int64)

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    def link_rows(row_a: int, row_b: int) -> None:
        a0, a1 = row_start[row_a], row_end[row_a]
        b0, b1 = row_start[row_b], row_end[row_b]
        if a0 == a1 or b0 == b1:
            return
        ia, ib = a0, b0
        while ia < a1 and ib < b1:
            if run_i0[ia] < run_i1[ib] and run_i0[ib] < run_i1[ia]:
                union(int(ia), int(ib))
            if run_i1[ia] <= run_i1[ib]:
                ia += 1
            else:
                ib += 1

    nonempty = np.flatnonzero(row_end > row_start)
    for row in nonempty:
        row = int(row)
        k, j = divmod(row, j_dim)
        if j + 1 < j_dim:
            link_rows(row, row + 1)  # same slice, next row
        if k + 1 < k_dim:
            link_rows(row, row + j_dim)  # next slice, same row

    roots = np.array([find(int(r)) for r in range(n_runs)], dtype=np.int64)
    uniq, compact = np.unique(roots, return_inverse=True)
    labels_flat = np.zeros(k_dim * j_dim * i_dim, dtype=np.int32)
    base = run_row * i_dim
    for r in range(n_runs):
        labels_flat[base[r] + run_i0[r] : base[r] + run_i1[r]] = compact[r] + 1
    return labels_flat.reshape(mask.shape), int(uniq.size)


def mask_from_hu_threshold(
    source: SourceGeometry,
    *,
    lung_hu_min: float,
    lung_hu_max: float,
    min_component_ml: float,
    min_component_fraction: float,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Threshold lung segmentation on the SOURCE grid.

    Method: label the air phase (HU <= lung_hu_max) and discard every component that
    reaches a face of the volume. Room air surrounds the patient and therefore always
    touches a face; the lungs never do. WHY this rather than a per-row "inside the body"
    bracket: the bracket keeps air in the corners of the body's bounding box, which on
    LCTSC-Test-S1-101 admitted 2.2 L of room air and reported a 6.1 L "lung".

    Deliberately computed on `source.hu_array` and never on a resampled array: MOS-IMG-041
    says intensity-threshold work happens "on source HU values after the Modality LUT and
    padding substitution, without any resampling".
    """
    hu = source.hu_array
    air = (hu >= np.float32(lung_hu_min)) & (hu <= np.float32(lung_hu_max))

    labels, n_labels = _connected_components_3d(air)
    d_r, d_c = source.pixel_spacing_mm
    voxel_ml = d_r * d_c * source.delta_s_mm / 1000.0

    keep = np.zeros(n_labels + 1, dtype=bool)
    stats: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if n_labels:
        counts = np.bincount(labels.ravel(), minlength=n_labels + 1)
        # Only the IN-PLANE faces. The k faces are the scan extent, not an anatomical
        # boundary: the trachea reaches the most superior slice, so testing k=0/k=K-1
        # discards the lungs along with it (observed on LCTSC-Test-S1-101, which then
        # yielded zero components).
        touches_face = np.zeros(n_labels + 1, dtype=bool)
        for face in (
            labels[:, 0, :], labels[:, -1, :],
            labels[:, :, 0], labels[:, :, -1],
        ):
            touches_face[np.unique(face)] = True

        interior = [
            lab
            for lab in range(1, n_labels + 1)
            if not touches_face[lab] and float(counts[lab]) * voxel_ml >= min_component_ml
        ]
        largest_ml = max((float(counts[l]) * voxel_ml for l in interior), default=0.0)
        floor_ml = max(min_component_ml, min_component_fraction * largest_ml)
        for lab in interior:
            ml = float(counts[lab]) * voxel_ml
            if ml >= floor_ml:
                keep[lab] = True
                stats.append({"label": int(lab), "volume_ml": ml})
            else:
                rejected.append({"label": int(lab), "volume_ml": ml})
    mask = keep[labels]
    if not mask.any():
        raise SystemExit(
            "the HU threshold produced no lung component. Check --lung-hu-max against "
            f"the series HU range [{float(hu.min()):.0f}, {float(hu.max()):.0f}]."
        )

    diag = {
        "method": "hu_threshold",
        "lung_hu_min": lung_hu_min,
        "lung_hu_max": lung_hu_max,
        "min_component_ml": min_component_ml,
        "min_component_fraction": min_component_fraction,
        "n_components_total": n_labels,
        "n_components_kept": int(keep.sum()),
        "kept_components": sorted(stats, key=lambda s: -s["volume_ml"])[:8],
        "rejected_interior_components": sorted(
            rejected, key=lambda s: -s["volume_ml"]
        )[:8],
        "computed_on": "source_grid",
    }
    return {"lung": mask}, diag


def find_rtstruct(root: Path, frame_of_reference_uid: str) -> Path:
    """Locate the RTSTRUCT whose referenced Frame of Reference matches the CT series.

    Matching on FrameOfReferenceUID rather than on directory layout: the contour points
    are in the patient LPS frame that UID names, so a mismatch means the polygon-to-index
    transform below would be applied in the wrong frame.
    """
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        except Exception:  # noqa: BLE001 - a non-DICOM file in the tree is not an error
            continue
        if str(getattr(ds, "Modality", "")) != "RTSTRUCT":
            continue
        for item in getattr(ds, "ReferencedFrameOfReferenceSequence", []) or []:
            if str(getattr(item, "FrameOfReferenceUID", "")) == frame_of_reference_uid:
                return path
        candidates.append(path)
    raise SystemExit(
        f"no RTSTRUCT under {root} references FrameOfReferenceUID "
        f"{frame_of_reference_uid}. RTSTRUCTs found: "
        + (", ".join(p.name for p in candidates) or "none")
    )


def _rasterise_polygon(
    xs: np.ndarray, ys: np.ndarray, rows: int, cols: int
) -> np.ndarray:
    """Even-odd scanline fill of one closed planar contour into a (rows, cols) bool mask.

    Pixel centres are at integer (i, j); a contour point mapped to index space therefore
    tests against integer scanlines. Even-odd (rather than nonzero) so that a contour
    nested inside another on the same slice punches a hole, which is how RTSTRUCT encodes
    them.
    """
    out = np.zeros((rows, cols), dtype=bool)
    n = xs.size
    if n < 3:
        return out
    x0, y0 = xs, ys
    x1, y1 = np.roll(xs, -1), np.roll(ys, -1)
    j_lo = max(0, int(np.ceil(ys.min())))
    j_hi = min(rows - 1, int(np.floor(ys.max())))
    for j in range(j_lo, j_hi + 1):
        crosses = ((y0 <= j) & (y1 > j)) | ((y1 <= j) & (y0 > j))
        if not crosses.any():
            continue
        ya, yb = y0[crosses], y1[crosses]
        xa, xb = x0[crosses], x1[crosses]
        xint = np.sort(xa + (j - ya) * (xb - xa) / (yb - ya))
        for a, b in zip(xint[0::2], xint[1::2]):
            i_lo = max(0, int(np.ceil(a)))
            i_hi = min(cols - 1, int(np.floor(b)))
            if i_hi >= i_lo:
                out[j, i_lo : i_hi + 1] = ~out[j, i_lo : i_hi + 1]
    return out


def mask_from_rtstruct(
    rtstruct_path: Path,
    volume: CanonicalVolume,
    source: SourceGeometry,
    roi_map: dict[str, str],
    *,
    roi_names: Sequence[str] | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Rasterise RTSTRUCT contours onto the SOURCE grid via the canonical inverse transform.

    Uses `volume.lps_to_index` - the recorded affine's inverse (MOS-IMG-032/033) - rather
    than a per-slice re-derivation from ImagePositionPatient. That is the whole point of
    keeping the inverse next to the forward transform: contour LPS millimetres and label
    voxels must agree to the same 1e-4 the round-trip self-check asserts.
    """
    ds = pydicom.dcmread(str(rtstruct_path))
    roi_number_to_name = {
        int(r.ROINumber): str(r.ROIName) for r in ds.StructureSetROISequence
    }
    k_dim, j_dim, i_dim = volume.shape
    masks: dict[str, np.ndarray] = {}
    per_roi: list[dict[str, Any]] = []

    for contour_roi in ds.ROIContourSequence:
        name = roi_number_to_name.get(int(contour_roi.ReferencedROINumber), "")
        if roi_names and name not in roi_names:
            continue
        structure = roi_map.get(name)
        if structure is None:
            per_roi.append({"roi_name": name, "skipped": "no rtstruct_roi_map entry"})
            continue
        mask = np.zeros(volume.shape, dtype=bool)
        n_contours = 0
        max_plane_dev = 0.0
        for item in getattr(contour_roi, "ContourSequence", []) or []:
            if str(item.ContourGeometricType) != "CLOSED_PLANAR":
                raise SystemExit(
                    f"ROI {name}: ContourGeometricType={item.ContourGeometricType} is "
                    "not CLOSED_PLANAR; refusing to guess a rasterisation for it."
                )
            pts = np.asarray(item.ContourData, dtype=np.float64).reshape(-1, 3)
            idx = volume.lps_to_index(pts)  # -> (i, j, k)
            k_float = idx[:, 2]
            k = int(np.rint(np.median(k_float)))
            dev = float(np.max(np.abs(k_float - k)))
            max_plane_dev = max(max_plane_dev, dev)
            if dev > 0.25:
                raise SystemExit(
                    f"ROI {name}: contour points span {dev:.3f} slice indices; it is not "
                    "planar in this series' grid, so the geometry assumption is wrong."
                )
            if not (0 <= k < k_dim):
                continue
            poly = _rasterise_polygon(idx[:, 0], idx[:, 1], j_dim, i_dim)
            mask[k] ^= poly  # XOR: nested contours on one slice are holes
            n_contours += 1
        if not mask.any():
            per_roi.append({"roi_name": name, "structure": structure, "empty": True})
            continue
        masks[structure] = mask
        per_roi.append(
            {
                "roi_name": name,
                "structure": structure,
                "n_contours": n_contours,
                "n_slices": int((mask.any(axis=(1, 2))).sum()),
                "n_voxels": int(mask.sum()),
                "max_out_of_plane_index_deviation": max_plane_dev,
            }
        )

    if not masks:
        raise SystemExit(
            f"no ROI in {rtstruct_path.name} produced a mask. Present ROIs: "
            + ", ".join(sorted(roi_number_to_name.values()))
        )
    diag = {
        "method": "rtstruct",
        "rtstruct": str(rtstruct_path),
        "structure_set_label": str(getattr(ds, "StructureSetLabel", "")),
        "rois": per_roi,
        "computed_on": "source_grid",
    }
    return masks, diag
