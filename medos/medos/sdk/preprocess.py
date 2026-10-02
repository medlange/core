# SPDX-License-Identifier: Apache-2.0
"""`medicalos-preprocessing`: the ONE constructor of a MedicalOS preprocessing chain.

`MOS-TRAIN-034`: "`medos.sdk.build_chain(spec) -> monai.transforms.Compose`
MUST be the only function in the MedicalOS codebase that instantiates a MONAI transform
for the deterministic preprocessing path. Training scripts, evaluation runners, the
native-mode serving runner and the golden-fixture recorder MUST all obtain their chain
from it."

`MOS-TRAIN-036`: "`build_chain` MUST be **total and pure**: a valid `PreprocessingSpec`
maps to exactly one `Compose`, with no environment lookup, no feature flag, no
`if training:` branch, and no dependence on the input volume."

WHAT IS HERE, AND WHAT IS HONESTLY NOT
--------------------------------------
MONAI and torch are not installed in this deployment (`requirements-dev.txt` pins numpy,
pydicom, highdicom and no deep-learning stack), and `MOS-REL-023` defers the training
backend to 0.3.0. So `build_chain` returns a `Chain` -- an ordered, frozen list of
`(MONAI class name, MONAI keyword arguments)` -- rather than a live `Compose`, and this
module also EXECUTES that chain, in numpy, on CPU, single-threaded.

That is not a mock. It is the same object in two roles, and both roles are required in
0.2.0:

  * `medos.sdk.chain.serialize_chain` renders the `Chain` into the
    `configs/inference.json` document of `MOS-TRAIN-131`, which is the artifact a MONAI
    trainer loads. The transform names and keyword arguments in that document are MONAI's,
    exactly, because that is what the document is for.
  * this module executes the same `Chain` so that `MOS-TRAIN-133`'s byte-equality check
    has two tensors to compare in a deployment where no training run exists yet
    (`MOS-TRAIN-191` puts the check here anyway, "before a second model exists to skew").

WHAT THE EXECUTION DOES NOT PROVE, stated once so nobody reads more into it: it does not
prove that MONAI's `Orientationd` and this module's agree. It cannot, in a deployment with
no MONAI. `MOS-TRAIN-042`'s nibabel identity assertion and `MOS-TRAIN-057`'s installed-
version assertion are the checks that close that gap, and the second of them is
implemented here (`assert_backend`) and fires against `backend.resampler`.

THE RESAMPLER, AND WHY IT IS DELIBERATELY SMALL
-----------------------------------------------
`_INTERP` maps `MOS-IMG-049`'s interpolator names to spline orders exactly as
`MOS-TRAIN-039` does: `nearest` -> 0, `linear` -> 1, `bspline3` -> 3. Orders 0 and 1 are
implemented; order 3 is REFUSED at execution with the field named, because a cubic B-spline
resampler written here would be a second implementation of the thing `MOS-TRAIN-055`
spends a paragraph explaining is not bit-identical between two builds of MONAI itself.
`MOS-TRAIN-132`'s rule -- "MUST NOT substitute the nearest available option" -- applies to
this module as much as to the generator, and substituting linear for bspline3 is exactly
the silent substitution it forbids. A spec declaring `bspline3` still SERIALIZES (the
document is correct; a MONAI runtime executes it); it does not EXECUTE here, and the
refusal says which of the two happened.

DETERMINISM
-----------
`MOS-IMG-048`: "single code path for training and serving, no GPU kernels, no
thread-count-dependent reductions". All arithmetic is float32, elementwise, in a fixed
axis order, with no reduction whose result depends on partitioning. `MOS-TRAIN-056`'s
`torch.set_num_threads(1)` has no analogue here because there is no torch; numpy's
elementwise ufuncs are single-threaded.

Spec: MOS-IMG-031, MOS-IMG-048, MOS-IMG-049, MOS-IMG-054, MOS-IMG-056, MOS-IMG-058,
MOS-TRAIN-034, MOS-TRAIN-036, MOS-TRAIN-037, MOS-TRAIN-038, MOS-TRAIN-039, MOS-TRAIN-040,
MOS-TRAIN-041, MOS-TRAIN-043, MOS-TRAIN-045, MOS-TRAIN-047, MOS-TRAIN-048, MOS-TRAIN-049,
MOS-TRAIN-051, MOS-TRAIN-055, MOS-TRAIN-057, MOS-TRAIN-059.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from medos.sdk.errors import ChainRefused, Refusal
from medos.sdk.spec import PreprocessingSpec

__all__ = [
    "INTERP_ORDER",
    "BLEND",
    "RESAMPLER_ID",
    "Transform",
    "Chain",
    "ChainInput",
    "build_chain",
    "apply_chain",
    "model_space_tensor",
    "first_patch",
    "tensor_digest",
    "record_golden",
    "assert_backend",
    "from_canonical_volume",
]

# `MOS-TRAIN-039`'s `_INTERP`, verbatim. An integer mode selects a spline order.
INTERP_ORDER: dict[str, int] = {"nearest": 0, "linear": 1, "bspline3": 3}

# `MOS-TRAIN-052`'s `_BLEND`, verbatim.
BLEND: dict[str, str] = {"gaussian": "gaussian", "uniform": "constant"}

# What this deployment's `backend.resampler` must say. `MOS-IMG-049` permits any
# package-and-version string; `MOS-TRAIN-055` fixes the convention for a MONAI-serialized
# chain and explicitly leaves non-MONAI forms valid ("the `SimpleITK==2.3.1` form of
# MOS-IMG-052 remains valid for non-MONAI ones"). `interp=separable_linear` is the dispatch
# pin: it names the implementation whose fourth decimal place the golden hash records.
RESAMPLER_ID = "medos.sdk.preprocess==1;interp=separable_linear;align_corners=1"

_ANAT_AXIS = {"L": 0, "R": 0, "A": 1, "P": 1, "S": 2, "I": 2}
_OPPOSITE = {"L": "R", "R": "L", "A": "P", "P": "A", "S": "I", "I": "S"}


def _refuse(code: str, message: str, **detail: Any) -> None:
    raise ChainRefused(
        (
            Refusal(
                check_id=detail.pop("check_id", "MOS-TRAIN-132"),
                code=code,
                message=message,
                observed=detail.pop("observed", None),
                bound=detail.pop("bound", None),
                detail=detail,
            ),
        )
    )


# =====================================================================================
# The chain
# =====================================================================================
@dataclass(frozen=True)
class Transform:
    """One entry of the chain: a MONAI class name and its keyword arguments.

    `args` is a plain mapping of JSON-representable values, which is what makes the chain
    byte-reproducible from the spec alone (`MOS-TRAIN-131`) and what makes
    `build_chain(parse(serialize(spec)))` structurally comparable to `build_chain(spec)`
    (`MOS-TRAIN-066`). A live transform object would be comparable only by identity.
    """

    name: str
    args: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "args": dict(self.args)}


@dataclass(frozen=True)
class Chain:
    """The ordered transforms of `MOS-IMG-031` steps (1) through (5).

    `MOS-TRAIN-039`: "Its transform order MUST be the order of `MOS-IMG-031` steps (1)
    through (5); step (6) is the sliding window of 17.3.5 and is not part of the
    `Compose`."
    """

    transforms: tuple[Transform, ...]
    keys: tuple[str, ...]
    label_keys: tuple[str, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.transforms)

    def as_json(self) -> dict[str, Any]:
        return {
            "keys": list(self.keys),
            "label_keys": list(self.label_keys),
            "transforms": [t.as_json() for t in self.transforms],
        }


@dataclass(frozen=True)
class ChainInput:
    """What the chain is handed. `MOS-TRAIN-038`: the loader is an adapter OUTSIDE it.

    "`LoadImaged` and `EnsureChannelFirstd` MUST NOT appear in the serialized chain. At
    serving time the input is already a `CanonicalVolume` produced by `medicalos-imaging`
    (`MOS-IMG-036`); at training time and in the startup self-test the loader is
    `medicalos-imaging` reading the fixture (`MOS-IMG-054` step 2). The hashed chain
    therefore begins at `Orientationd`, and the loader is an adapter outside it."

    `arrays` are channel-first, `(C, K, J, I)`, matching `CanonicalVolume.array`'s
    `[k, j, i]` with a leading channel. `spacing_mm` is along `(K, J, I)`, the order
    `CanonicalVolume.spacing_mm` uses. `axcodes` is the volume's current anatomical code.
    """

    arrays: dict[str, np.ndarray]
    spacing_mm: tuple[float, float, float]
    axcodes: str


def from_canonical_volume(volume: Any, key: str = "image") -> ChainInput:
    """Adapter from `medos.core.geometry.CanonicalVolume`. `MOS-TRAIN-038`'s "outside it"."""
    array = np.asarray(volume.array, dtype=np.float32)
    if array.ndim == 3:
        array = array[np.newaxis, ...]
    return ChainInput(
        arrays={key: array},
        spacing_mm=tuple(float(s) for s in volume.spacing_mm),  # type: ignore[arg-type]
        axcodes=str(volume.anatomical_code),
    )


def _margin_voxels(
    margin_mm: Sequence[float], spacing_mm: Sequence[float]
) -> tuple[int, ...]:
    """`MOS-TRAIN-047`, transcribed.

    "Half-up, not numpy's banker's rounding: 0.5 mm at 1.0 mm spacing must be one voxel in
    every implementation, on every platform." Converted ONCE, at chain construction, at the
    declared `target_spacing_mm` -- never at the source spacing of an individual case,
    because the crop happens after resampling.
    """
    return tuple(int(math.floor(m / s + 0.5)) for m, s in zip(margin_mm, spacing_mm))


def build_chain(
    spec: PreprocessingSpec,
    keys: Sequence[str] = ("image",),
    label_keys: Sequence[str] = (),
) -> Chain:
    """`MOS-TRAIN-034`'s one constructor. Total and pure (`MOS-TRAIN-036`).

    "Training calls `build_chain(spec, keys=('image',), label_keys=('label',))`. Serving
    calls `build_chain(spec)`. That single argument difference is the entire difference
    between the training and serving preprocessing paths."

    `MOS-TRAIN-043`: `Spacingd`'s `mode` is a TUPLE ALIGNED TO KEYS, never a scalar,
    whenever a label key is present. "Spline-interpolating an integer label array at 1.5 mm
    target spacing invents label values that are not in `io.label_set` -- a voxel of value
    2.4 between lobe 2 and lobe 3."
    """
    keys = tuple(keys)
    label_keys = tuple(label_keys)
    all_keys = keys + label_keys
    if not keys:
        _refuse("no_image_key", "build_chain needs at least one image key")
    if set(keys) & set(label_keys):
        _refuse(
            "key_is_both_image_and_label",
            "a key cannot be both an image key and a label key; its interpolator would "
            "be ambiguous (MOS-TRAIN-043)",
            observed=sorted(set(keys) & set(label_keys)),
        )

    resample_mode = tuple(
        INTERP_ORDER[spec.image_interpolator] if k in keys
        else INTERP_ORDER[spec.label_interpolator]
        for k in all_keys
    )

    transforms: list[Transform] = [
        # (1) orientation permutation and flips. MOS-TRAIN-041: `orientation_target` and
        # MONAI's `axcodes` use the identical convention, so it is passed through
        # unmodified and there is NO conversion table here.
        Transform("Orientationd", {"keys": list(all_keys),
                                   "axcodes": spec.orientation_target}),
        # (2) resample to model spacing.
        Transform(
            "Spacingd",
            {
                "keys": list(all_keys),
                "pixdim": list(spec.target_spacing_mm),
                "mode": list(resample_mode),
                "padding_mode": "border",
                "align_corners": True,
                "dtype": "float32",
            },
        ),
    ]

    # (3) foreground crop. MOS-TRAIN-040: `foreground_crop.mode: none` means the transform
    # is OMITTED ENTIRELY -- "not a no-op transform; the chain differs and so does the
    # hash".
    if spec.foreground_crop.mode == "threshold_bbox":
        assert spec.foreground_crop.margin_mm is not None
        assert spec.foreground_crop.min_size_voxels is not None
        transforms.append(
            Transform(
                "ForegroundCropToMinSized",
                {
                    "keys": list(all_keys),
                    "source_key": keys[0],
                    # MOS-TRAIN-040: "closed over the threshold, not a lambda". The
                    # document renders this as a `_partial_` reference to a module-level
                    # function; `MOS-TRAIN-039` says a lambda "is not reproducible across
                    # a multiprocess DataLoader and is not inspectable in a failure
                    # report".
                    "select_fn": "above",
                    "threshold": spec.foreground_crop.threshold_hu,
                    "margin": list(
                        _margin_voxels(
                            spec.foreground_crop.margin_mm, spec.target_spacing_mm
                        )
                    ),
                    "min_size": list(spec.foreground_crop.min_size_voxels),
                    # MOS-TRAIN-049: the pad is in HU space, at
                    # canonical_geometry.padding_output_value, INSIDE step (3), so padded
                    # voxels traverse the clip of (4) and the normalisation of (5) exactly
                    # as real voxels do.
                    "pad_value": spec.canonical_geometry.padding_output_value,
                },
            )
        )
    elif spec.foreground_crop.mode == "mask":
        _refuse(
            "foreground_crop_mode_mask_unrepresentable",
            "foreground_crop.mode `mask` crops to an upstream label map named by "
            "`mask_artifact_role`, and a chain generated from the spec alone has no "
            "upstream artifact to bind it to. MOS-TRAIN-132: the generator refuses rather "
            "than substituting the nearest available option.",
            observed="mask",
            bound=["none", "threshold_bbox"],
            field="foreground_crop.mode",
        )

    # (4) intensity clip. MOS-TRAIN-045: a SEPARATE transform from (5) for every scheme
    # except `clip_scale`. `b_min = a_min`, `b_max = a_max`, `clip=True` is a pure clip,
    # "and that is the required form".
    if spec.normalisation.scheme == "clip_scale":
        # "the only scheme in which (4) and (5) are one transform" (MOS-TRAIN-040). The
        # output range comes from `io.input.value_range`, which MOS-IMG-049 does not
        # declare; refused rather than invented.
        _refuse(
            "clip_scale_output_range_undeclared",
            "normalisation.scheme `clip_scale` takes b_min/b_max from "
            "`io.input.value_range` (MOS-TRAIN-040), and MOS-IMG-049's schema declares no "
            "such field. The generator refuses rather than choosing a range.",
            observed="clip_scale",
            field="normalisation.scheme",
            check_id="MOS-TRAIN-132",
        )
    transforms.append(
        Transform(
            "ScaleIntensityRanged",
            {
                "keys": list(keys),
                "a_min": spec.clip.min_hu,
                "a_max": spec.clip.max_hu,
                "b_min": spec.clip.min_hu,
                "b_max": spec.clip.max_hu,
                "clip": True,
            },
        )
    )

    # (5) normalisation.
    if spec.normalisation.scheme == "zscore_dataset":
        if spec.normalisation.statistics_source != "spec":
            _refuse(
                "zscore_dataset_without_spec_statistics",
                "normalisation.scheme `zscore_dataset` with statistics_source "
                f"{spec.normalisation.statistics_source!r} has no literal mean/std to "
                "emit. MOS-TRAIN-046: the values MUST be written literally into the spec "
                "and the spec MUST set statistics_source: spec.",
                observed=spec.normalisation.statistics_source,
                bound="spec",
                field="normalisation.statistics_source",
            )
        transforms.append(
            Transform(
                "NormalizeIntensityd",
                {
                    "keys": list(keys),
                    "subtrahend": spec.normalisation.mean,
                    "divisor": spec.normalisation.std,
                    "nonzero": False,
                    "channel_wise": False,
                },
            )
        )
    elif spec.normalisation.scheme == "zscore_case":
        # MOS-TRAIN-040: `subtrahend=None`, `divisor=None`. Permitted only per
        # MOS-IMG-050, which this code cannot check and does not pretend to.
        transforms.append(
            Transform(
                "NormalizeIntensityd",
                {
                    "keys": list(keys),
                    "subtrahend": None,
                    "divisor": None,
                    "nonzero": False,
                    "channel_wise": False,
                },
            )
        )
    elif spec.normalisation.scheme == "zscore_foreground":
        transforms.append(
            Transform(
                "NormalizeIntensityd",
                {
                    "keys": list(keys),
                    "subtrahend": None,
                    "divisor": None,
                    "nonzero": True,
                    "channel_wise": False,
                },
            )
        )
    else:  # `minmax`
        _refuse(
            "normalisation_scheme_unmapped",
            f"normalisation.scheme {spec.normalisation.scheme!r} has no row in "
            "MOS-TRAIN-040's serialization table. MOS-TRAIN-040 is 'the complete "
            "serialization contract'; a scheme with no row cannot be emitted exactly, and "
            "MOS-TRAIN-132 forbids substituting the nearest available option.",
            observed=spec.normalisation.scheme,
            field="normalisation.scheme",
        )

    transforms.append(
        Transform(
            "EnsureTyped",
            {"keys": list(all_keys), "dtype": spec.io.input_dtype, "track_meta": True},
        )
    )

    chain = Chain(transforms=tuple(transforms), keys=keys, label_keys=label_keys)
    _assert_chain_invariants(chain)
    return chain


def _assert_chain_invariants(chain: Chain) -> None:
    """`MOS-TRAIN-037` and `MOS-TRAIN-038`, asserted at construction rather than in CI only.

    `MOS-TRAIN-037`: "CI MUST assert that no transform whose class name begins with `Rand`
    is ever emitted by `build_chain`. An augmentation that leaks into the spec is applied
    at serving time, non-deterministically, on every patient."

    `MOS-TRAIN-038`: `LoadImaged` and `EnsureChannelFirstd` MUST NOT appear.

    Both are also asserted by `tests/integration/test_curation.py`. Here as well because a
    CI-only assertion is one refactor away from being a CI-only assertion of nothing.
    """
    for name in chain.names:
        if name.startswith("Rand"):
            _refuse(
                "random_transform_in_chain",
                f"{name} is a random augmentation and MUST NOT appear in the serialized "
                "chain (MOS-TRAIN-037); it lives in the training augmentation module and "
                "is applied after build_chain's output",
                observed=name,
                check_id="MOS-TRAIN-037",
            )
        if name in ("LoadImaged", "EnsureChannelFirstd"):
            _refuse(
                "loader_in_chain",
                f"{name} MUST NOT appear in the serialized chain (MOS-TRAIN-038); the "
                "hashed chain begins at Orientationd and the loader is an adapter outside "
                "it",
                observed=name,
                check_id="MOS-TRAIN-038",
            )


# =====================================================================================
# Execution
# =====================================================================================
def _reorient(
    arrays: dict[str, np.ndarray],
    spacing: tuple[float, float, float],
    current: str,
    target: str,
) -> tuple[dict[str, np.ndarray], tuple[float, float, float]]:
    """`Orientationd`. Permutation and flips only -- no resampling, no interpolation.

    `MOS-TRAIN-041` fixes the convention on both sides, so this is a pure index operation:
    for each TARGET axis, find the current axis carrying the same anatomical axis, move it
    there, and flip it if the direction is opposite.
    """
    if len(current) != 3 or len(target) != 3:
        _refuse("bad_axcodes", f"axcodes must be three characters: {current!r} -> "
                               f"{target!r}", observed=[current, target])
    src_axis = {_ANAT_AXIS[c]: (i, c) for i, c in enumerate(current)}
    if len(src_axis) != 3:
        _refuse("degenerate_axcodes", f"{current!r} names one anatomical axis twice",
                observed=current)
    perm: list[int] = []
    flips: list[bool] = []
    for t in target:
        if _ANAT_AXIS[t] not in src_axis:
            _refuse(
                "axcode_axis_absent",
                f"target {target!r} asks for {t!r} and the input is {current!r}",
                observed=current, bound=target,
            )
        i, c = src_axis[_ANAT_AXIS[t]]
        perm.append(i)
        flips.append(c == _OPPOSITE[t])
    out: dict[str, np.ndarray] = {}
    for key, arr in arrays.items():
        # +1 on every spatial axis: the array is channel-first.
        moved = np.transpose(arr, (0,) + tuple(p + 1 for p in perm))
        for axis, flip in enumerate(flips):
            if flip:
                moved = np.flip(moved, axis=axis + 1)
        out[key] = np.ascontiguousarray(moved, dtype=np.float32)
    new_spacing = (spacing[perm[0]], spacing[perm[1]], spacing[perm[2]])
    return out, new_spacing


def _out_extent(in_n: int, in_sp: float, out_sp: float) -> int:
    """Output length for one axis. Half-up, per `MOS-TRAIN-047`'s tie-break rule.

    numpy's `round` is half-to-even, and the whole point of `MOS-TRAIN-047` is that a
    half-to-even tie-break makes two implementations differ by one voxel -- "which would
    change the tensor hash and nothing else". The same rule is used here so the module has
    exactly one rounding convention.
    """
    return max(1, int(math.floor(in_n * (in_sp / out_sp) + 0.5)))


def _resample_axis(arr: np.ndarray, axis: int, out_n: int, order: int) -> np.ndarray:
    """One separable 1-D pass. `align_corners=True`, `padding_mode='border'`.

    With `align_corners=True` the sample coordinates span exactly `[0, in_n - 1]`, so the
    border padding mode is never reached; it is declared because `MOS-TRAIN-049` requires
    the padding constant to be declared even where `MOS-TRAIN-051` makes it unreachable --
    "so that 'unreachable' is a checkable claim rather than an assumption".

    Separable 1-D passes: for order 0 and order 1 the separable form is exactly nearest-
    neighbour and exactly trilinear respectively, so this is not an approximation of the
    3-D operator, it is the 3-D operator.
    """
    in_n = arr.shape[axis]
    if in_n == out_n:
        return arr
    if out_n > 1:
        coords = np.arange(out_n, dtype=np.float64) * ((in_n - 1) / (out_n - 1))
    else:
        coords = np.zeros(1, dtype=np.float64)
    if order == 0:
        # Half-up, clamped. numpy's `round` is banker's and is deliberately not used.
        idx = np.clip(np.floor(coords + 0.5).astype(np.int64), 0, in_n - 1)
        return np.take(arr, idx, axis=axis)
    if order == 1:
        lo = np.clip(np.floor(coords).astype(np.int64), 0, in_n - 1)
        hi = np.clip(lo + 1, 0, in_n - 1)
        frac = (coords - lo).astype(np.float32)
        shape = [1] * arr.ndim
        shape[axis] = out_n
        w = frac.reshape(shape)
        a = np.take(arr, lo, axis=axis)
        b = np.take(arr, hi, axis=axis)
        return (a * (np.float32(1.0) - w) + b * w).astype(np.float32, copy=False)
    _refuse(
        "interpolator_not_implemented",
        f"spline order {order} (image_interpolator `bspline3`) is not implemented by "
        f"{RESAMPLER_ID}. MOS-TRAIN-132 forbids substituting the nearest available "
        "option, and MOS-TRAIN-055 explains why a cubic resampler is not portable between "
        "two builds of the same library. The chain SERIALIZES correctly; it does not "
        "execute in this deployment.",
        observed=order,
        bound=[0, 1],
        field="image_interpolator",
        check_id="MOS-TRAIN-057",
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _spacing(
    arrays: dict[str, np.ndarray],
    spacing: tuple[float, float, float],
    *,
    keys: Sequence[str],
    pixdim: Sequence[float],
    mode: Sequence[int],
) -> tuple[dict[str, np.ndarray], tuple[float, float, float]]:
    """`Spacingd`. `mode` is aligned to `keys` (`MOS-TRAIN-043`), never a scalar."""
    if len(mode) != len(keys):
        _refuse(
            "mode_not_aligned_to_keys",
            "Spacingd's mode MUST be a tuple aligned to keys whenever a label key is "
            "present (MOS-TRAIN-043); a scalar mode is a CI failure (MOS-TRAIN-044)",
            observed=[len(mode), len(keys)],
            check_id="MOS-TRAIN-043",
        )
    out = dict(arrays)
    first = out[keys[0]]
    extents = tuple(
        _out_extent(first.shape[a + 1], spacing[a], float(pixdim[a])) for a in range(3)
    )
    for key, order in zip(keys, mode):
        arr = out[key]
        for a in range(3):
            arr = _resample_axis(arr, a + 1, extents[a], int(order))
        out[key] = np.ascontiguousarray(arr, dtype=np.float32)
    return out, (float(pixdim[0]), float(pixdim[1]), float(pixdim[2]))


def _above(x: np.ndarray, threshold: float) -> np.ndarray:
    """`MOS-TRAIN-039`'s `_above`: module-level and picklable ON PURPOSE.

    "a lambda here is not reproducible across a multiprocess DataLoader and is not
    inspectable in a failure report."
    """
    return x > threshold


SELECT_FNS: dict[str, Any] = {"above": _above}


def _bounding_box(
    source: np.ndarray, *, threshold: float, margin: Sequence[int]
) -> tuple[list[int], list[int]]:
    """Step 1 of `MOS-TRAIN-048`: the foreground box, with margin, clamped to the array."""
    mask = _above(source[0], threshold)
    if not mask.any():
        _refuse(
            "empty_foreground",
            f"no voxel exceeds foreground_crop.threshold_hu={threshold}; the crop has no "
            "box and MONAI's generate_spatial_bounding_box with allow_smaller=False "
            "raises here too",
            observed=float(source.max()),
            bound=threshold,
            check_id="MOS-TRAIN-048",
        )
    start: list[int] = []
    end: list[int] = []
    for axis in range(3):
        others = tuple(a for a in range(3) if a != axis)
        present = np.any(mask, axis=others)
        idx = np.flatnonzero(present)
        lo = max(0, int(idx[0]) - int(margin[axis]))
        hi = min(mask.shape[axis], int(idx[-1]) + 1 + int(margin[axis]))
        start.append(lo)
        end.append(hi)
    return start, end


def _expand(
    start: list[int], end: list[int], shape: Sequence[int], min_size: Sequence[int]
) -> tuple[list[int], list[int]]:
    """Step 2 of `MOS-TRAIN-048`, transcribed from its reference implementation.

    "Odd deficit: the extra voxel goes high. This is the only tie-break in the crop and it
    is fixed here so two implementations cannot differ by one slice -- which would change
    the tensor hash and nothing else."
    """
    lo, hi = list(start), list(end)
    for a, want in enumerate(min_size):
        deficit = want - (hi[a] - lo[a])
        if deficit <= 0:
            continue
        low_take = min(lo[a], deficit // 2)
        lo[a] -= low_take
        hi[a] = min(shape[a], hi[a] + (deficit - low_take))
        still = want - (hi[a] - lo[a])
        if still > 0:
            lo[a] = max(0, lo[a] - still)  # residual is padded, not invented
    return lo, hi


def _crop_to_min_size(
    arrays: dict[str, np.ndarray],
    *,
    keys: Sequence[str],
    source_key: str,
    threshold: float,
    margin: Sequence[int],
    min_size: Sequence[int],
    pad_value: float,
) -> dict[str, np.ndarray]:
    """`ForegroundCropToMinSized`: the one transform MedicalOS adds (`MOS-TRAIN-048`)."""
    source = arrays[source_key]
    start, end = _bounding_box(source, threshold=threshold, margin=margin)
    shape = tuple(source.shape[1:])
    start, end = _expand(start, end, shape, min_size)
    out: dict[str, np.ndarray] = dict(arrays)
    for key in keys:
        arr = out[key]
        cropped = arr[
            :, start[0]:end[0], start[1]:end[1], start[2]:end[2]
        ]
        # Step 4: SpatialPad(method="symmetric", mode="constant", value=pad_value). The
        # extra voxel of an odd pad goes HIGH, the same tie-break `_expand` uses.
        pads = [(0, 0)]
        for a in range(3):
            total = max(0, int(min_size[a]) - cropped.shape[a + 1])
            half = total // 2
            pads.append((half, total - half))
        out[key] = np.ascontiguousarray(
            np.pad(cropped, pads, mode="constant", constant_values=np.float32(pad_value)),
            dtype=np.float32,
        )
    return out


def _scale_intensity_range(
    arrays: dict[str, np.ndarray],
    *,
    keys: Sequence[str],
    a_min: float,
    a_max: float,
    b_min: float,
    b_max: float,
    clip: bool,
) -> dict[str, np.ndarray]:
    """`ScaleIntensityRanged`. With `b == a` this is the pure clip `MOS-TRAIN-045` requires."""
    out = dict(arrays)
    scale = np.float32((b_max - b_min) / (a_max - a_min))
    for key in keys:
        arr = out[key]
        arr = (arr - np.float32(a_min)) * scale + np.float32(b_min)
        if clip:
            arr = np.clip(arr, np.float32(min(b_min, b_max)), np.float32(max(b_min, b_max)))
        out[key] = np.ascontiguousarray(arr, dtype=np.float32)
    return out


def _normalize_intensity(
    arrays: dict[str, np.ndarray],
    *,
    keys: Sequence[str],
    subtrahend: float | None,
    divisor: float | None,
    nonzero: bool,
) -> dict[str, np.ndarray]:
    """`NormalizeIntensityd`. `subtrahend=None` means per-case statistics (`zscore_case`)."""
    out = dict(arrays)
    for key in keys:
        arr = out[key]
        if subtrahend is None or divisor is None:
            pool = arr[arr != 0] if nonzero else arr
            if pool.size == 0:
                _refuse(
                    "empty_statistics_pool",
                    "per-case normalisation has no voxels to compute statistics over",
                    check_id="MOS-IMG-050",
                )
            mean = np.float32(pool.mean(dtype=np.float64))
            std = np.float32(pool.std(dtype=np.float64))
            if float(std) == 0.0:
                std = np.float32(1.0)
        else:
            mean = np.float32(subtrahend)
            std = np.float32(divisor)
        if nonzero and subtrahend is None:
            mask = arr != 0
            arr = np.where(mask, (arr - mean) / std, arr)
        else:
            arr = (arr - mean) / std
        out[key] = np.ascontiguousarray(arr, dtype=np.float32)
    return out


_DTYPES = {"float32": np.float32}


def apply_chain(chain: Chain, data: ChainInput) -> dict[str, np.ndarray]:
    """Execute the chain. One dispatch table, no branch on anything but the transform name.

    `MOS-IMG-031` steps (1)-(5). Step (6), the sliding window, is `first_patch`.
    """
    arrays = {k: np.asarray(v, dtype=np.float32) for k, v in data.arrays.items()}
    for arr in arrays.values():
        if arr.ndim != 4:
            _refuse(
                "input_not_channel_first",
                "the chain takes channel-first (C, K, J, I) arrays; "
                "EnsureChannelFirstd is an adapter outside the chain (MOS-TRAIN-038)",
                observed=list(arr.shape),
                check_id="MOS-TRAIN-038",
            )
    spacing = data.spacing_mm
    axcodes = data.axcodes
    for t in chain.transforms:
        if t.name == "Orientationd":
            arrays, spacing = _reorient(arrays, spacing, axcodes, t.args["axcodes"])
            axcodes = t.args["axcodes"]
        elif t.name == "Spacingd":
            arrays, spacing = _spacing(
                arrays,
                spacing,
                keys=t.args["keys"],
                pixdim=t.args["pixdim"],
                mode=t.args["mode"],
            )
        elif t.name == "ForegroundCropToMinSized":
            if t.args["select_fn"] not in SELECT_FNS:
                _refuse(
                    "unknown_select_fn",
                    f"select_fn {t.args['select_fn']!r} is not a registered predicate; "
                    "MOS-TRAIN-040 requires it to be closed over the threshold rather "
                    "than a lambda, which means it must be nameable",
                    observed=t.args["select_fn"],
                )
            arrays = _crop_to_min_size(
                arrays,
                keys=t.args["keys"],
                source_key=t.args["source_key"],
                threshold=float(t.args["threshold"]),
                margin=t.args["margin"],
                min_size=t.args["min_size"],
                pad_value=float(t.args["pad_value"]),
            )
        elif t.name == "ScaleIntensityRanged":
            arrays = _scale_intensity_range(
                arrays,
                keys=t.args["keys"],
                a_min=float(t.args["a_min"]),
                a_max=float(t.args["a_max"]),
                b_min=float(t.args["b_min"]),
                b_max=float(t.args["b_max"]),
                clip=bool(t.args["clip"]),
            )
        elif t.name == "NormalizeIntensityd":
            arrays = _normalize_intensity(
                arrays,
                keys=t.args["keys"],
                subtrahend=t.args["subtrahend"],
                divisor=t.args["divisor"],
                nonzero=bool(t.args["nonzero"]),
            )
        elif t.name == "EnsureTyped":
            dtype = _DTYPES.get(t.args["dtype"])
            if dtype is None:
                _refuse(
                    "unsupported_input_dtype",
                    f"io.input_dtype {t.args['dtype']!r}; float32 only in 0.2.0 "
                    "(MOS-TRAIN-040)",
                    observed=t.args["dtype"],
                    bound=["float32"],
                )
            arrays = {
                k: np.ascontiguousarray(v, dtype=dtype) for k, v in arrays.items()
            }
        else:  # pragma: no cover - build_chain emits no other name
            _refuse("unknown_transform", f"no executor for {t.name!r}", observed=t.name)
    return arrays


def model_space_tensor(
    spec: PreprocessingSpec, data: ChainInput, *, chain: Chain | None = None
) -> np.ndarray:
    """Steps (1)-(5), returning the model-space array for the image key, `(C, D, H, W)`."""
    chain = chain or build_chain(spec)
    out = apply_chain(chain, data)
    return out[chain.keys[0]]


def first_patch(spec: PreprocessingSpec, tensor: np.ndarray) -> np.ndarray:
    """`MOS-IMG-054` step 3's `T`: "the first patch of the deterministic sliding-window order".

    With TTA disabled and `patch.batch_size` forced to 1, and with `MOS-TRAIN-051`
    guaranteeing the crop is at least the patch on every axis, the first window is the
    origin corner. Returned as `NCDHW`, which is the layout `golden_fixture.output_shape`
    records.
    """
    d, h, w = spec.patch.size_voxels
    if tensor.shape[1] < d or tensor.shape[2] < h or tensor.shape[3] < w:
        _refuse(
            "model_space_smaller_than_patch",
            "the model-space tensor is smaller than patch.size_voxels, which "
            "MOS-TRAIN-051 exists to make impossible; the sliding window's own padding "
            "would be reached",
            observed=list(tensor.shape),
            bound=list(spec.patch.size_voxels),
            check_id="MOS-TRAIN-051",
        )
    patch = tensor[np.newaxis, :, :d, :h, :w]
    return np.ascontiguousarray(patch, dtype=np.float32)


def tensor_digest(tensor: np.ndarray) -> str:
    """`MOS-IMG-054` step 4, verbatim.

    `h = "sha256:" + sha256(numpy.ascontiguousarray(T, dtype="<f4").tobytes(order="C"))`

    `MOS-IMG-058` requires the training path to emit the golden hash "using the identical
    function of MOS-IMG-054 step 4", so there is exactly one of these and both the
    recorder and the comparator call it. `MOS-IMG-056` forbids comparing with a tolerance,
    which is why this returns a digest and not an array.
    """
    buf = np.ascontiguousarray(tensor, dtype="<f4").tobytes(order="C")
    return "sha256:" + hashlib.sha256(buf).hexdigest()


def record_golden(
    spec: PreprocessingSpec, data: ChainInput
) -> tuple[str, tuple[int, ...]]:
    """`MOS-TRAIN-059`: `record_golden(spec, fixture) -> (sha256, shape)`.

    "which MUST execute `MOS-IMG-054` steps 1 through 4 and differ from the worker's
    self-test only in that it returns the digest instead of comparing it. The comparison is
    the *only* line that may differ; a separate recording implementation is exactly the
    defect the test exists to catch."

    So this function contains no comparison, and `verify_golden` below contains nothing
    else.
    """
    tensor = first_patch(spec, model_space_tensor(spec, data))
    return tensor_digest(tensor), tuple(int(n) for n in tensor.shape)


def verify_golden(spec: PreprocessingSpec, data: ChainInput) -> dict[str, Any]:
    """`MOS-IMG-054` step 5 and `MOS-IMG-055`'s diagnostic payload.

    Byte equality, never a tolerance (`MOS-IMG-056`): "A tolerance-based comparison hides
    exactly the drift the test exists to catch." The worker that gets `ok: False` from this
    MUST NOT become ready and MUST NOT claim any job.
    """
    observed, shape = record_golden(spec, data)
    return {
        "ok": observed == spec.golden_fixture.output_tensor_sha256,
        "expected": spec.golden_fixture.output_tensor_sha256,
        "observed": observed,
        "shape": list(shape),
        "declared_shape": list(spec.golden_fixture.output_shape),
        "declared_resampler": spec.backend.resampler,
        "declared_numpy": spec.backend.numpy,
        "installed_numpy": f"numpy=={np.__version__}",
        "event": "preprocessing_selftest_failed" if observed
        != spec.golden_fixture.output_tensor_sha256 else "preprocessing_selftest_passed",
    }


def assert_backend(spec: PreprocessingSpec) -> None:
    """`MOS-TRAIN-057`: the version assertion, run BEFORE the hash comparison.

    "The version assertion is a better error message than the hash mismatch, not a
    substitute for it: both MUST run, in that order."

    Asserts the installed numpy against `backend.numpy` and the declared resampler against
    this module's `RESAMPLER_ID`. A spec declaring a MONAI chain (`MOS-TRAIN-055`'s
    `monai==...;torch==...` form) is refused HERE rather than producing a hash mismatch
    later, because a hash mismatch does not distinguish "the wrong library" from "the right
    library, changed".
    """
    installed = f"numpy=={np.__version__}"
    if spec.backend.numpy != installed:
        _refuse(
            "numpy_version_mismatch",
            f"backend.numpy declares {spec.backend.numpy!r}; {installed!r} is installed. "
            "MOS-IMG-046: a resample backend version bump produces a new "
            "PreprocessingSpec version, a new ModelVersion and its own EvaluationRun. "
            "There is no re-record operation (MOS-TRAIN-065).",
            observed=installed,
            bound=spec.backend.numpy,
            field="backend.numpy",
            check_id="MOS-TRAIN-057",
        )
    if spec.backend.resampler != RESAMPLER_ID:
        _refuse(
            "resampler_mismatch",
            f"backend.resampler declares {spec.backend.resampler!r} and this deployment "
            f"executes {RESAMPLER_ID!r}. MOS-TRAIN-055: an integer mode resolves to a "
            "compiled kernel or to a fallback and the two are not bit-identical at a "
            "cubic spline order, so the resampler is pinned and asserted rather than "
            "assumed.",
            observed=RESAMPLER_ID,
            bound=spec.backend.resampler,
            field="backend.resampler",
            check_id="MOS-TRAIN-057",
        )
