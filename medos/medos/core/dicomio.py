# SPDX-License-Identifier: Apache-2.0
"""Source-DICOM reading: series scan, series selection, per-slice decode.

CONTRACT.md §1 assigns this module "series scan/select, DICOM read". It is the only
place in `medos.core` that calls pydicom on a source instance, and it sits BELOW
`geometry` in the import graph so that `geometry.build_canonical_volume` can compose
these readers without a cycle.

Lifted verbatim from `spikes/week0/build_volume.py` per CONTRACT.md §2. Behaviour is
unchanged; only the module boundary moved.

MOS-IMG-096 permits pydicom directly on the read path -- highdicom is required only when
WRITING. MOS-IMG-023/024/026/027 are implemented here per slice, never once per series:
RescaleSlope/Intercept MAY differ slice to slice and a series-level read silently
corrupts the HU of every slice that disagrees.

Spec: MOS-IMG-007, MOS-IMG-008, MOS-IMG-010, MOS-IMG-023, MOS-IMG-024, MOS-IMG-026,
MOS-IMG-027.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np

try:
    import pydicom
    from pydicom.dataset import Dataset
except ImportError as exc:  # pragma: no cover - environment problem, not a data problem
    raise SystemExit(
        "pydicom is required. Install with:  pip install pydicom==3.0.1\n"
        f"(import failed: {exc})"
    ) from exc

from medos.core.errors import GeometryRejection, SeriesSelectionError

__all__ = [
    "SUPPORTED_SOP_CLASSES",
    "ENHANCED_SOP_CLASSES",
    "scan_series",
    "select_series",
]


# MOS-IMG-007: the closed set of SOP Classes the volume builder accepts.
SUPPORTED_SOP_CLASSES: dict[str, str] = {
    "1.2.840.10008.5.1.4.1.1.2": "CT Image Storage",
    "1.2.840.10008.5.1.4.1.1.4": "MR Image Storage",
}


# Enhanced (multi-frame) classes are in the spec's table (MOS-IMG-008) but are explicitly
# NOT implemented by this spike; we reject them loudly rather than mis-handle per-frame
# functional groups.
ENHANCED_SOP_CLASSES: dict[str, str] = {
    "1.2.840.10008.5.1.4.1.1.2.1": "Enhanced CT Image Storage",
    "1.2.840.10008.5.1.4.1.1.4.1": "Enhanced MR Image Storage",
}


def _as_float_tuple(value: Any, name: str, sop_uid: str) -> tuple[float, ...]:
    if value is None:
        raise GeometryRejection(
            "geometry_missing_position",
            {"sop_instance_uid": sop_uid},
            f"{name} is absent on instance {sop_uid}",
        )
    return tuple(float(v) for v in value)


def _decode_stored(ds: Dataset) -> np.ndarray:
    """Decode stored pixel values honouring MOS-IMG-023.

    pydicom already applies BitsAllocated/PixelRepresentation, but it does not always
    mask the unused high bits when BitsStored < BitsAllocated. We do that explicitly,
    including sign extension for PixelRepresentation = 1, because a CT stored with
    BitsStored=12 in a 16-bit container decodes to garbage HU without it.
    """
    arr = ds.pixel_array
    bits_allocated = int(getattr(ds, "BitsAllocated", 16))
    bits_stored = int(getattr(ds, "BitsStored", bits_allocated))
    pixel_repr = int(getattr(ds, "PixelRepresentation", 0))

    if bits_stored >= bits_allocated:
        return arr.astype(np.int64, copy=False)

    mask = (1 << bits_stored) - 1
    stored = arr.astype(np.int64, copy=False) & mask
    if pixel_repr == 1:
        sign_bit = 1 << (bits_stored - 1)
        stored = (stored ^ sign_bit) - sign_bit
    return stored


def _apply_modality_lut(ds: Dataset, stored: np.ndarray) -> np.ndarray:
    """MOS-IMG-024: ModalityLUTSequence takes precedence, else slope/intercept.

    Read per slice, never once for the series: RescaleSlope/Intercept "MAY differ per
    slice and MUST be read per slice".
    """
    lut_seq = getattr(ds, "ModalityLUTSequence", None)
    if lut_seq:
        item = lut_seq[0]
        descriptor = list(item.LUTDescriptor)
        n_entries = int(descriptor[0]) or 65536
        first_mapped = int(descriptor[1])
        lut_data = np.asarray(item.LUTData, dtype=np.float64)
        if lut_data.size != n_entries:
            raise GeometryRejection(
                "geometry_unsupported_rescale",
                {
                    "rescale_type": "ModalityLUTSequence",
                    "sop_instance_uid": str(ds.SOPInstanceUID),
                },
                f"ModalityLUTSequence declares {n_entries} entries but carries "
                f"{lut_data.size}; refusing to guess",
            )
        idx = np.clip(stored - first_mapped, 0, n_entries - 1).astype(np.int64)
        return lut_data[idx].astype(np.float32)

    slope = float(getattr(ds, "RescaleSlope", 1.0))
    intercept = float(getattr(ds, "RescaleIntercept", 0.0))
    return (stored.astype(np.float32) * np.float32(slope) + np.float32(intercept)).astype(
        np.float32
    )


def _check_rescale_type(ds: Dataset, modality: str) -> bool:
    """MOS-IMG-026. Returns rescale_type_assumed."""
    if modality != "CT":
        return False
    rescale_type = getattr(ds, "RescaleType", None)
    if rescale_type in (None, ""):
        return False
    rescale_type = str(rescale_type).strip()
    if rescale_type == "HU":
        return False
    if rescale_type == "US":
        return True  # accepted, recorded as assumed
    raise GeometryRejection(
        "geometry_unsupported_rescale",
        {"rescale_type": rescale_type, "sop_instance_uid": str(ds.SOPInstanceUID)},
        f"RescaleType={rescale_type!r} on a CT instance; only absent, 'HU' or 'US' "
        "are accepted (MOS-IMG-026)",
    )


def _substitute_padding(ds: Dataset, stored: np.ndarray, hu: np.ndarray, pad_out: float) -> int:
    """MOS-IMG-027: replace PixelPaddingValue voxels AFTER rescale.

    Returns the number of substituted voxels. Leaving padding at its stored magnitude
    "corrupts every normalisation statistic and every HU threshold measurement".
    """
    pad_value = getattr(ds, "PixelPaddingValue", None)
    if pad_value is None:
        return 0
    pad_value = int(pad_value)
    limit = getattr(ds, "PixelPaddingRangeLimit", None)
    if limit is None:
        mask = stored == pad_value
    else:
        lo, hi = sorted((pad_value, int(limit)))
        mask = (stored >= lo) & (stored <= hi)
    n = int(mask.sum())
    if n:
        hu[mask] = np.float32(pad_out)
    return n


def scan_series(root: Path) -> dict[str, list[Path]]:
    """Group every readable DICOM file under `root` by SeriesInstanceUID.

    Deliberately tolerant on the *scan* (a study folder legitimately holds RTSTRUCT,
    SR, other series) and strict later inside the selected series, which is where
    MOS-IMG-007 applies ("Any other SOP Class in the selected series set").
    """
    if not root.exists():
        raise SeriesSelectionError(f"--series-dir does not exist: {root}")
    groups: dict[str, list[Path]] = {}
    candidates = [p for p in root.rglob("*") if p.is_file()]
    if not candidates:
        raise SeriesSelectionError(f"no files found under {root}")
    unreadable: list[str] = []
    for path in candidates:
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=False)
        except Exception as exc:  # noqa: BLE001 - we report, never swallow
            unreadable.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        uid = str(getattr(ds, "SeriesInstanceUID", "")) or "<no-series-uid>"
        groups.setdefault(uid, []).append(path)
    if not groups:
        raise SeriesSelectionError(
            f"no DICOM files parsed under {root}. First failures:\n  "
            + "\n  ".join(unreadable[:5])
        )
    return groups


def select_series(groups: dict[str, list[Path]], series_uid: str | None) -> tuple[str, list[Path]]:
    """Stand-in for chapter 3's SeriesSelector: pick one series, explicitly.

    MOS-DATA-004 forbids a service choosing its own series; this spike is the platform
    side, so an explicit --series-uid always wins and the auto-pick is only a
    developer convenience that prints what it did.
    """
    if series_uid:
        if series_uid not in groups:
            raise SeriesSelectionError(
                f"series {series_uid} not found. Present series:\n  "
                + "\n  ".join(f"{u} ({len(p)} files)" for u, p in groups.items())
            )
        return series_uid, groups[series_uid]

    image_series: list[tuple[str, list[Path]]] = []
    for uid, paths in groups.items():
        ds = pydicom.dcmread(str(paths[0]), stop_before_pixels=True)
        sop_class = str(getattr(ds, "SOPClassUID", ""))
        if sop_class in SUPPORTED_SOP_CLASSES or sop_class in ENHANCED_SOP_CLASSES:
            image_series.append((uid, paths))
    if not image_series:
        raise SeriesSelectionError(
            "no CT/MR image series found. Series present:\n  "
            + "\n  ".join(f"{u} ({len(p)} files)" for u, p in groups.items())
        )
    if len(image_series) > 1:
        image_series.sort(key=lambda kv: (-len(kv[1]), kv[0]))
        print(
            "[note] multiple image series present; auto-selected the largest. "
            "Pass --series-uid to be explicit.",
            file=sys.stderr,
        )
    return image_series[0]
