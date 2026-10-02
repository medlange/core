# SPDX-License-Identifier: Apache-2.0
"""Enough NIfTI-1 to hold a vendor to `MOS-SVC-082`. A reader, never a writer.

Section 2.6.2 step 9 is a list of five things the invoker verifies before a sealed
service's output becomes a DICOM object, and two of them are about this file format:

    "every artifact digest equals the bundle's declared `sha256`; every artifact is within
     `max_artifact_bytes`; the bundle's `geometry` equals the geometry the *platform*
     computed from the selected series' DICOM headers ... within `1e-4`; **every label
     map's NIfTI affine agrees with `geometry` within `1e-4`**; every
     `source_sop_instance_uids` entry was in the dispatched selection."

and `MOS-SVC-082` states the consequence: "The platform MUST verify this and MUST fail the
job with `geometry_mismatch` otherwise." A platform that declares a label map is on the
canonical grid, without opening it, has verified the declaration and not the array --
which is chapter 2's acceptance checks 15 and 16 exactly (a `[311,512,512]` array against
a `[312,512,512]` grid; an affine off by 2e-3 mm).

WHY A HEADER READER AND NOT A LIBRARY

nibabel is not a dependency of this project and adding one mid-release to a shared
environment is the thing that breaks every other agent's venv. That is the practical
reason. The structural one is better: the NIfTI-1 header is a FIXED 348-byte record with a
published layout, this module reads eleven fields out of it, and it never writes one. A
parser that cannot produce output cannot be turned into a second DICOM-adjacent writer by
a later change, which is the boundary `MOS-SVC-006` cares about.

Read-only also bounds the risk: the artifact arrives from untrusted vendor code, and every
read below is a fixed offset into a length-checked buffer with no allocation driven by a
header field.

LPS AND RAS ARE NOT THE SAME FRAME, AND THIS IS WHERE THAT BITES

`MOS-SVC-081` fixes `geometry.voxel_order` as `"LPS"`, which is DICOM's patient frame.
NIfTI's `srow_*` affine maps voxel indices to **RAS+**. The two differ by a sign on the
first two axes, so a comparison that forgot the conversion would reject every correct
label map and accept a mirrored one. `affine_from_geometry` does the conversion in one
place and says so.

Spec: chapter 2 sections 2.6.2 step 9, 2.8.4; `MOS-SVC-081`, `MOS-SVC-082`, `MOS-SVC-084`,
`MOS-SVC-085`, `MOS-SVC-087`; chapter 4 `MOS-IMG-039`.
"""

from __future__ import annotations

import gzip
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "NIFTI1_HEADER_BYTES",
    "Nifti1Header",
    "affine_from_geometry",
    "geometry_problems",
    "read_nifti1_header",
]

NIFTI1_HEADER_BYTES = 348

#: `MOS-SVC-085`: "Voxel dtype MUST be `uint8` for <= 255 segments and `uint16` otherwise."
#: NIfTI codes: 2 = uint8, 512 = uint16. Nothing else is a conformant label map, and a
#: float label map is `result_dtype_invalid` rather than a rounding question.
_LABEL_DTYPES = {2: "uint8", 512: "uint16"}


@dataclass(frozen=True, slots=True)
class Nifti1Header:
    shape: tuple[int, ...]
    datatype: int
    dtype_name: str
    bitpix: int
    pixdim: tuple[float, ...]
    sform_code: int
    qform_code: int
    #: The 3x4 voxel -> RAS mapping (`srow_x`, `srow_y`, `srow_z`), as read.
    srow: tuple[tuple[float, float, float, float], ...]
    vox_offset: int
    little_endian: bool


def read_nifti1_header(blob: bytes) -> Nifti1Header:
    """Parse the 348-byte header of a `.nii` or `.nii.gz` payload.

    Endianness is decided by `sizeof_hdr`, which is the file's own self-check: the field
    reads 348 in the writer's byte order and 1 543 569 408 in the other. Guessing from the
    magic instead would accept a big-endian file read as little-endian, and every affine
    would then compare as wrong for a reason nobody could see.
    """
    if blob[:2] == b"\x1f\x8b":
        # A `nifti-gzip` artifact (`MOS-SVC-084`). Decompressed with a ceiling: the caller
        # has already bounded the COMPRESSED size against `max_artifact_bytes`, and a gzip
        # bomb is the one way a bounded download becomes an unbounded allocation.
        blob = _gunzip_capped(blob, limit=512 * 1024 * 1024)
    if len(blob) < NIFTI1_HEADER_BYTES:
        raise ValueError(f"a NIfTI-1 header is {NIFTI1_HEADER_BYTES} bytes; got {len(blob)}")

    little = struct.unpack_from("<i", blob, 0)[0] == NIFTI1_HEADER_BYTES
    if not little and struct.unpack_from(">i", blob, 0)[0] != NIFTI1_HEADER_BYTES:
        raise ValueError("sizeof_hdr is not 348 in either byte order; not a NIfTI-1 file")
    e = "<" if little else ">"

    magic = blob[344:348]
    if magic not in (b"n+1\x00", b"ni1\x00"):
        raise ValueError(f"NIfTI magic is {magic!r}, not n+1 or ni1")

    dim = struct.unpack_from(e + "8h", blob, 40)
    ndim = dim[0]
    if not 1 <= ndim <= 7:
        raise ValueError(f"dim[0] is {ndim}; a NIfTI-1 array has 1 to 7 dimensions")
    datatype, bitpix = struct.unpack_from(e + "2h", blob, 70)
    pixdim = struct.unpack_from(e + "8f", blob, 76)
    vox_offset = struct.unpack_from(e + "f", blob, 108)[0]
    qform_code, sform_code = struct.unpack_from(e + "2h", blob, 252)
    srow = tuple(
        tuple(struct.unpack_from(e + "4f", blob, offset))
        for offset in (280, 296, 312)
    )
    return Nifti1Header(
        shape=tuple(int(d) for d in dim[1 : ndim + 1]),
        datatype=int(datatype),
        dtype_name=_LABEL_DTYPES.get(int(datatype), f"code_{int(datatype)}"),
        bitpix=int(bitpix),
        pixdim=tuple(float(p) for p in pixdim),
        sform_code=int(sform_code),
        qform_code=int(qform_code),
        srow=srow,  # type: ignore[arg-type]
        vox_offset=int(vox_offset),
        little_endian=little,
    )


def _gunzip_capped(blob: bytes, *, limit: int) -> bytes:
    out = bytearray()
    with gzip.GzipFile(fileobj=_BytesReader(blob)) as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            out += chunk
            if len(out) > limit:
                raise ValueError(
                    f"the label map decompresses past {limit} bytes; refusing to continue"
                )
    return bytes(out)


class _BytesReader:
    """`gzip.GzipFile` wants a file object; `io.BytesIO` would do and adds an import."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        self._pos = offset if whence == 0 else (
            self._pos + offset if whence == 1 else len(self._data) + offset
        )
        return self._pos

    def tell(self) -> int:
        return self._pos


def affine_from_geometry(geometry: Mapping[str, Any]) -> list[list[float]]:
    """The voxel -> RAS affine a conformant label map for this geometry must carry.

    `geometry` is `MOS-SVC-081`'s block: `shape` `(nz, ny, nx)`, `spacing_mm` `(dz, dy, dx)`,
    `origin_lps_mm`, and `direction_lps` as nine row-major direction cosines whose COLUMNS
    are the unit vectors of the i, j and k axes in LPS.

    The last step is the frame conversion: LPS to RAS negates the x and y axes, so rows 0
    and 1 of the assembled matrix change sign. This is the one line that makes a mirrored
    label map fail rather than pass.
    """
    shape = list(geometry.get("shape") or ())
    spacing = list(geometry.get("spacing_mm") or ())
    origin = list(geometry.get("origin_lps_mm") or ())
    direction = list(geometry.get("direction_lps") or geometry.get("direction") or ())
    if len(shape) != 3 or len(spacing) != 3 or len(origin) != 3 or len(direction) != 9:
        raise ValueError("geometry lacks shape/spacing_mm/origin_lps_mm/direction_lps")

    # `spacing_mm` is (dz, dy, dx) and the array is (nz, ny, nx); the NIfTI axes are
    # (i, j, k) = (x, y, z). Reversing once, here, is why nothing below has to remember it.
    step = [float(spacing[2]), float(spacing[1]), float(spacing[0])]
    cosines = [[float(direction[r * 3 + c]) for c in range(3)] for r in range(3)]

    affine = [[0.0] * 4 for _ in range(3)]
    for row in range(3):
        for col in range(3):
            affine[row][col] = cosines[row][col] * step[col]
        affine[row][3] = float(origin[row])
    for col in range(4):  # LPS -> RAS
        affine[0][col] = -affine[0][col]
        affine[1][col] = -affine[1][col]
    return affine


def geometry_problems(
    header: Nifti1Header,
    geometry: Mapping[str, Any],
    *,
    tolerance: float = 1e-4,
) -> list[str]:
    """`MOS-SVC-082`, evaluated. Empty list means the label map is on the canonical grid.

    `shape` is compared in the bundle's own order -- `geometry.shape` is `(nz, ny, nx)` and
    NIfTI `dim` is `(nx, ny, nz)`, so one of them is reversed here and the reversal is
    stated rather than left to the reader to infer from a passing test.
    """
    out: list[str] = []
    declared = [int(v) for v in (geometry.get("shape") or ())]
    if len(declared) != 3:
        return ["geometry.shape is not three-dimensional"]
    if list(header.shape) != list(reversed(declared)):
        out.append(
            f"label map dim is {list(header.shape)} (x,y,z); geometry.shape is "
            f"{declared} (z,y,x) -- MOS-SVC-082 requires them to be the same grid"
        )
    if header.datatype not in _LABEL_DTYPES:
        out.append(
            f"label map dtype code {header.datatype} is neither uint8 nor uint16 "
            "(MOS-SVC-085); a non-integer label map is result_dtype_invalid"
        )
    if header.sform_code == 0 and header.qform_code == 0:
        out.append(
            "the label map declares neither an sform nor a qform, so it states no "
            "position in the patient at all (MOS-SVC-082)"
        )
        return out
    try:
        expected = affine_from_geometry(geometry)
    except ValueError as exc:
        return [*out, f"the bundle's geometry is unusable: {exc}"]

    worst = 0.0
    for row in range(3):
        for col in range(4):
            worst = max(worst, abs(header.srow[row][col] - expected[row][col]))
    if worst > tolerance:
        out.append(
            f"the label map affine differs from the bundle geometry by {worst:.6g} mm, "
            f"over the {tolerance:g} tolerance of section 2.6.2 step 9 (MOS-SVC-082)"
        )
    return out


def sequence_close(
    left: Sequence[float], right: Sequence[float], *, tolerance: float = 1e-4
) -> bool:
    """Elementwise comparison for the geometry-vs-platform check of step 9."""
    if len(left) != len(right):
        return False
    return all(abs(float(a) - float(b)) <= tolerance for a, b in zip(left, right, strict=True))
