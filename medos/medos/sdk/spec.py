# SPDX-License-Identifier: Apache-2.0
"""`PreprocessingSpec`: the schema of `MOS-IMG-049`, parsed, validated and round-trippable.

`MOS-IMG-049`: "The schema below is complete. Every field is required unless marked
optional; an absent required field is a registration error, not a default."

That sentence is the whole design of this module. Every conditionally-required field
(`gantry_tilt.max_deg` when the mode is `correct`, `normalisation.mean` when the
statistics source is `spec`, `foreground_crop.margin_mm` when the crop is not `none`) is
checked against its condition and NONE of them has a fallback value, because
`MOS-IMG-046` makes a spec the record of what training did: "There is no 'compatible
edit'." A default written here would be a training-time fact this code invented.

WHY IT IS A DATACLASS AND NOT A DICT
------------------------------------
`MOS-TRAIN-036`: `build_chain` "MUST be **total and pure**: a valid `PreprocessingSpec`
maps to exactly one `Compose`". A dict makes "valid" a property nobody checks and a typo
in a key an absent field with a silent default. The parse is the validation, it happens
once, and everything downstream reads attributes.

THE TWO REGISTRATION CHECKS THAT LIVE HERE RATHER THAN IN THE GENERATOR
-----------------------------------------------------------------------
`MOS-TRAIN-051`: "`foreground_crop.min_size_voxels` MUST be element-wise greater than or
equal to `patch.size_voxels`. This makes `sliding_window_inference`'s own padding
unreachable, which removes a second, undeclared padding implementation from the path.
Registration MUST reject a spec that violates it."

`MOS-IMG-049`'s digest encoding: "Every field typed `sha256 digest` above is encoded as
`"sha256:"` followed by 64 lower-case hex characters ... A bare hex string is invalid."
`MOS-TRAIN-058`'s own fixture writes bare hex in `golden_fixture.sha256` and
`golden_fixture.output_tensor_sha256`, which contradicts it. REPORTED; the prefix is
required here, because `MOS-IMG-049` is the schema and `MOS-TRAIN-058` is an example of it.

Spec: MOS-IMG-045, MOS-IMG-046, MOS-IMG-049, MOS-IMG-050, MOS-IMG-051, MOS-IMG-052,
MOS-TRAIN-036, MOS-TRAIN-051, MOS-TRAIN-055, MOS-TRAIN-058, MOS-TRAIN-065.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from medos.sdk.canonical import canonical_bytes, sha256_hex
from medos.sdk.errors import ChainRefused, Refusal

__all__ = [
    "SCHEMA_VERSION",
    "INTERPOLATORS",
    "NORMALISATION_SCHEMES",
    "CanonicalGeometry",
    "Clip",
    "Normalisation",
    "ForegroundCrop",
    "Patch",
    "Tta",
    "Io",
    "Inverse",
    "Backend",
    "GoldenFixture",
    "PreprocessingSpec",
    "parse_spec",
]

SCHEMA_VERSION = "1.0"

INTERPOLATORS: tuple[str, ...] = ("nearest", "linear", "bspline3")
LABEL_INTERPOLATORS: tuple[str, ...] = ("nearest", "onehot_linear_argmax")
PROBABILITY_INTERPOLATORS: tuple[str, ...] = ("nearest", "linear")
NORMALISATION_SCHEMES: tuple[str, ...] = (
    "zscore_dataset",
    "zscore_case",
    "zscore_foreground",
    "minmax",
    "clip_scale",
)
STATISTICS_SOURCES: tuple[str, ...] = ("spec", "case", "foreground")
CROP_MODES: tuple[str, ...] = ("none", "threshold_bbox", "mask")
BLENDS: tuple[str, ...] = ("uniform", "gaussian")
OUTPUT_KINDS: tuple[str, ...] = ("label", "probability", "logit")
AGGREGATES: tuple[str, ...] = ("gaussian_weighted_mean", "uniform_mean", "max")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ORIENT_RE = re.compile(r"^[LRAPSI]{3}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# `ε_aff` = 1e-4 exactly (`MOS-IMG-049`, `MOS-IMG-033`).
AFFINE_TOLERANCE = 1e-4


def _refuse(field: str, message: str, observed: Any = None, bound: Any = None) -> None:
    raise ChainRefused(
        (
            Refusal(
                check_id="MOS-IMG-049",
                code="preprocessing_spec_invalid",
                message=f"{field}: {message}",
                observed=observed,
                bound=bound,
                detail={"field": field},
            ),
        )
    )


def _req(d: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in d:
        _refuse(
            f"{path}{key}",
            "required by MOS-IMG-049; an absent required field is a registration error, "
            "not a default",
        )
    return d[key]


def _enum(value: Any, allowed: Sequence[str], path: str) -> str:
    if value not in allowed:
        _refuse(path, f"must be one of {tuple(allowed)}", observed=value,
                bound=list(allowed))
    return str(value)


def _floats(value: Any, n: int, path: str) -> tuple[float, ...]:
    try:
        out = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        _refuse(path, f"must be {n} numbers", observed=value)
        raise  # unreachable; _refuse raises
    if len(out) != n:
        _refuse(path, f"must be {n} numbers", observed=value, bound=n)
    return out


def _ints(value: Any, n: int, path: str) -> tuple[int, ...]:
    try:
        out = tuple(int(v) for v in value)
    except (TypeError, ValueError):
        _refuse(path, f"must be {n} integers", observed=value)
        raise
    if len(out) != n:
        _refuse(path, f"must be {n} integers", observed=value, bound=n)
    return out


def _digest(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        _refuse(
            path,
            "must be 'sha256:' followed by 64 lower-case hex characters "
            "(MOS-IMG-049/MOS-STORE-211); a bare hex string is invalid",
            observed=value,
        )
    return str(value)


@dataclass(frozen=True)
class CanonicalGeometry:
    """`canonical_geometry.*`. The tilt pair is flattened; the nesting carried no meaning.

    `MOS-IMG-049` writes `canonical_geometry.gantry_tilt.mode` and
    `canonical_geometry.gantry_tilt.max_deg`. Two fields under one key is a JSON shape, not
    a type, and `as_json()` restores the nesting exactly, so the serialized form is
    unchanged. `MOS-TRAIN-111` reads both of these and `non_uniform_spacing`.
    """

    gantry_tilt_mode: str
    gantry_tilt_max_deg: float
    non_uniform_spacing: str
    padding_output_value: float

    def as_json(self) -> dict[str, Any]:
        return {
            "gantry_tilt": {
                "mode": self.gantry_tilt_mode,
                "max_deg": self.gantry_tilt_max_deg,
            },
            "non_uniform_spacing": self.non_uniform_spacing,
            "padding_output_value": self.padding_output_value,
        }


@dataclass(frozen=True)
class Clip:
    min_hu: float
    max_hu: float

    def as_json(self) -> dict[str, Any]:
        return {"min_hu": self.min_hu, "max_hu": self.max_hu}


@dataclass(frozen=True)
class Normalisation:
    """`MOS-IMG-050`: `zscore_case` "MUST NOT be used unless it was the scheme used at
    training time. The spec is the record of what training did; it is not a place to make
    a new choice." Nothing here can check that, and nothing here pretends to.
    """

    scheme: str
    statistics_source: str
    mean: float | None = None
    std: float | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "scheme": self.scheme,
            "statistics_source": self.statistics_source,
        }
        if self.mean is not None:
            out["mean"] = self.mean
        if self.std is not None:
            out["std"] = self.std
        return out


@dataclass(frozen=True)
class ForegroundCrop:
    mode: str
    threshold_hu: float | None = None
    margin_mm: tuple[float, float, float] | None = None
    min_size_voxels: tuple[int, int, int] | None = None
    mask_artifact_role: str | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"mode": self.mode}
        if self.threshold_hu is not None:
            out["threshold_hu"] = self.threshold_hu
        if self.margin_mm is not None:
            out["margin_mm"] = list(self.margin_mm)
        if self.min_size_voxels is not None:
            out["min_size_voxels"] = list(self.min_size_voxels)
        if self.mask_artifact_role is not None:
            out["mask_artifact_role"] = self.mask_artifact_role
        return out


@dataclass(frozen=True)
class Patch:
    size_voxels: tuple[int, int, int]
    sliding_window_overlap: float
    blend: str
    batch_size: int
    gaussian_sigma_scale: float | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "size_voxels": list(self.size_voxels),
            "sliding_window_overlap": self.sliding_window_overlap,
            "blend": self.blend,
            "batch_size": self.batch_size,
        }
        if self.gaussian_sigma_scale is not None:
            out["gaussian_sigma_scale"] = self.gaussian_sigma_scale
        return out


@dataclass(frozen=True)
class Tta:
    axes: tuple[tuple[int, ...], ...]
    reduction: str | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"axes": [list(a) for a in self.axes]}
        if self.reduction is not None:
            out["reduction"] = self.reduction
        return out


@dataclass(frozen=True)
class Io:
    input_dtype: str
    input_layout: str
    channels: int
    output_kind: str
    label_set: tuple[dict[str, Any], ...] | None = None

    def as_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "input_dtype": self.input_dtype,
            "input_layout": self.input_layout,
            "channels": self.channels,
            "output_kind": self.output_kind,
        }
        if self.label_set is not None:
            out["label_set"] = [dict(e) for e in self.label_set]
        return out


@dataclass(frozen=True)
class Inverse:
    aggregate: str
    undo_crop: str
    target_grid: str
    assert_shape_equal: bool
    affine_tolerance: float

    def as_json(self) -> dict[str, Any]:
        return {
            "aggregate": self.aggregate,
            "undo_crop": self.undo_crop,
            "target_grid": self.target_grid,
            "assert_shape_equal": self.assert_shape_equal,
            "affine_tolerance": self.affine_tolerance,
        }


@dataclass(frozen=True)
class Backend:
    """`MOS-TRAIN-055` fixes the VALUE CONVENTION for a MONAI-serialized chain:
    `monai==<exact>;torch==<exact>;USE_COMPILED=<0|1>;interp=<grid_pull|map_coordinates>`.
    `MOS-IMG-049` owns the field and "permits any package-and-version string", and the
    `SimpleITK==2.3.1` form of `MOS-IMG-052` "remains valid for non-MONAI ones". So this
    is a free-text field with a non-empty check, and `medos.sdk.preprocess`
    asserts
    the INSTALLED versions against it at execution time per `MOS-TRAIN-057`.
    """

    resampler: str
    numpy: str

    def as_json(self) -> dict[str, Any]:
        return {"resampler": self.resampler, "numpy": self.numpy}


@dataclass(frozen=True)
class GoldenFixture:
    path: str
    sha256: str
    output_tensor_sha256: str
    output_shape: tuple[int, ...]
    recorded_at: str
    recorded_by_commit: str

    def as_json(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "output_tensor_sha256": self.output_tensor_sha256,
            "output_shape": list(self.output_shape),
            "recorded_at": self.recorded_at,
            "recorded_by_commit": self.recorded_by_commit,
        }


@dataclass(frozen=True)
class PreprocessingSpec:
    """`MOS-IMG-049`'s document. Frozen: `MOS-TRAIN-065` forbids editing one in place.

    "A change to any `PreprocessingSpec` field, including `backend.resampler`, MUST
    produce a new spec version, a new `ModelVersion` and a new `EvaluationRun`. The
    pipeline MUST NOT offer a 're-record the hash' operation, because that operation's only
    effect is to make a real behavioural change pass a test designed to catch it."
    """

    schema_version: str
    id: str
    version: str
    model_id: str
    model_version: str
    canonical_geometry: CanonicalGeometry
    orientation_target: str
    axis_order: tuple[str, str, str]
    target_spacing_mm: tuple[float, float, float]
    image_interpolator: str
    label_interpolator: str
    probability_interpolator: str
    clip: Clip
    normalisation: Normalisation
    foreground_crop: ForegroundCrop
    patch: Patch
    tta: Tta
    io: Io
    inverse: Inverse
    backend: Backend
    golden_fixture: GoldenFixture

    def as_json(self) -> dict[str, Any]:
        """The serialized form. `parse_spec(spec.as_json()) == spec` (`MOS-TRAIN-066`)."""
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "version": self.version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "canonical_geometry": self.canonical_geometry.as_json(),
            "orientation_target": self.orientation_target,
            "axis_order": list(self.axis_order),
            "target_spacing_mm": list(self.target_spacing_mm),
            "image_interpolator": self.image_interpolator,
            "label_interpolator": self.label_interpolator,
            "probability_interpolator": self.probability_interpolator,
            "clip": self.clip.as_json(),
            "normalisation": self.normalisation.as_json(),
            "foreground_crop": self.foreground_crop.as_json(),
            "patch": self.patch.as_json(),
            "tta": self.tta.as_json(),
            "io": self.io.as_json(),
            "inverse": self.inverse.as_json(),
            "backend": self.backend.as_json(),
            "golden_fixture": self.golden_fixture.as_json(),
        }

    @property
    def digest(self) -> str:
        """Content digest of the spec, `MOS-IMG-045`'s "pinned by content digest".

        Over `medos.sdk.canonical.canonical_bytes`, not over a YAML file's bytes:
        two
        renderings of the same document with different key order are the same spec, and a
        digest that disagreed would make `MOS-IMG-046`'s "no compatible edit" fire on a
        reformat.
        """
        return "sha256:" + sha256_hex(canonical_bytes(self.as_json()))


def parse_spec(document: Mapping[str, Any]) -> PreprocessingSpec:
    """Validate and build. Raises `ChainRefused` naming the offending field.

    The refusal names a FIELD because `MOS-TRAIN-132` requires the generator to do so and
    a spec that fails here never reaches the generator; a caller should get the same
    quality of answer from either.
    """
    d = dict(document)
    schema_version = str(_req(d, "schema_version", ""))
    if schema_version != SCHEMA_VERSION:
        _refuse("schema_version", f"this implementation reads {SCHEMA_VERSION!r} only",
                observed=schema_version, bound=SCHEMA_VERSION)

    cg_raw = dict(_req(d, "canonical_geometry", ""))
    tilt_raw = dict(_req(cg_raw, "gantry_tilt", "canonical_geometry."))
    tilt_mode = _enum(
        _req(tilt_raw, "mode", "canonical_geometry.gantry_tilt."),
        ("reject", "correct"),
        "canonical_geometry.gantry_tilt.mode",
    )
    if tilt_mode == "correct" and "max_deg" not in tilt_raw:
        _refuse(
            "canonical_geometry.gantry_tilt.max_deg",
            "required when the mode is `correct` (MOS-IMG-049)",
        )
    canonical_geometry = CanonicalGeometry(
        gantry_tilt_mode=tilt_mode,
        gantry_tilt_max_deg=float(tilt_raw.get("max_deg", 0.0)),
        non_uniform_spacing=_enum(
            _req(cg_raw, "non_uniform_spacing", "canonical_geometry."),
            ("reject", "resample_to_uniform"),
            "canonical_geometry.non_uniform_spacing",
        ),
        padding_output_value=float(
            _req(cg_raw, "padding_output_value", "canonical_geometry.")
        ),
    )

    orientation_target = str(_req(d, "orientation_target", ""))
    if not _ORIENT_RE.match(orientation_target):
        _refuse(
            "orientation_target",
            "a three-character code over {L,R,A,P,S,I} giving, for each array axis in "
            "increasing-index order, the anatomical direction it points toward "
            "(MOS-IMG-038, MOS-TRAIN-041)",
            observed=orientation_target,
        )
    axes = set(orientation_target)
    if any(len(axes & set(pair)) > 1 for pair in ("LR", "AP", "SI")):
        _refuse("orientation_target", "names one anatomical axis twice",
                observed=orientation_target)

    axis_order_raw = list(_req(d, "axis_order", ""))
    if sorted(str(a) for a in axis_order_raw) != ["i", "j", "k"]:
        _refuse("axis_order", "must be a permutation of ['k','j','i']",
                observed=axis_order_raw)
    axis_order = (str(axis_order_raw[0]), str(axis_order_raw[1]), str(axis_order_raw[2]))

    clip_raw = dict(_req(d, "clip", ""))
    clip = Clip(
        min_hu=float(_req(clip_raw, "min_hu", "clip.")),
        max_hu=float(_req(clip_raw, "max_hu", "clip.")),
    )
    if clip.max_hu <= clip.min_hu:
        _refuse("clip", "max_hu must exceed min_hu", observed=[clip.min_hu, clip.max_hu])

    norm_raw = dict(_req(d, "normalisation", ""))
    scheme = _enum(
        _req(norm_raw, "scheme", "normalisation."), NORMALISATION_SCHEMES,
        "normalisation.scheme",
    )
    stats_source = _enum(
        _req(norm_raw, "statistics_source", "normalisation."), STATISTICS_SOURCES,
        "normalisation.statistics_source",
    )
    if stats_source == "spec":
        for k in ("mean", "std"):
            if k not in norm_raw:
                _refuse(
                    f"normalisation.{k}",
                    "required when statistics_source is `spec`. MOS-TRAIN-046: these are "
                    "computed over the `train` partition of the frozen split ONLY, after "
                    "steps (1)-(4), over voxels inside the foreground crop -- statistics "
                    "over the whole cohort are the cheapest leak in medical imaging to "
                    "commit and the hardest to see in a report",
                )
        if float(norm_raw["std"]) == 0.0:
            _refuse("normalisation.std", "must be non-zero", observed=0.0)
    normalisation = Normalisation(
        scheme=scheme,
        statistics_source=stats_source,
        mean=float(norm_raw["mean"]) if "mean" in norm_raw else None,
        std=float(norm_raw["std"]) if "std" in norm_raw else None,
    )

    fc_raw = dict(_req(d, "foreground_crop", ""))
    crop_mode = _enum(_req(fc_raw, "mode", "foreground_crop."), CROP_MODES,
                      "foreground_crop.mode")
    if crop_mode == "threshold_bbox" and "threshold_hu" not in fc_raw:
        _refuse("foreground_crop.threshold_hu", "required when mode is `threshold_bbox`")
    if crop_mode == "mask" and not fc_raw.get("mask_artifact_role"):
        _refuse("foreground_crop.mask_artifact_role", "required when mode is `mask`")
    if crop_mode != "none":
        for k in ("margin_mm", "min_size_voxels"):
            if k not in fc_raw:
                _refuse(f"foreground_crop.{k}", "required when mode is not `none`")
    foreground_crop = ForegroundCrop(
        mode=crop_mode,
        threshold_hu=(
            float(fc_raw["threshold_hu"]) if "threshold_hu" in fc_raw else None
        ),
        margin_mm=(
            _floats(fc_raw["margin_mm"], 3, "foreground_crop.margin_mm")
            if "margin_mm" in fc_raw else None
        ),
        min_size_voxels=(
            _ints(fc_raw["min_size_voxels"], 3, "foreground_crop.min_size_voxels")
            if "min_size_voxels" in fc_raw else None
        ),
        mask_artifact_role=fc_raw.get("mask_artifact_role"),
    )

    p_raw = dict(_req(d, "patch", ""))
    blend = _enum(_req(p_raw, "blend", "patch."), BLENDS, "patch.blend")
    if blend == "gaussian" and "gaussian_sigma_scale" not in p_raw:
        _refuse("patch.gaussian_sigma_scale", "required when blend is `gaussian`")
    overlap = float(_req(p_raw, "sliding_window_overlap", "patch."))
    if not 0.0 <= overlap < 1.0:
        _refuse("patch.sliding_window_overlap", "must be in [0, 1)", observed=overlap)
    batch_size = int(_req(p_raw, "batch_size", "patch."))
    if batch_size < 1:
        _refuse("patch.batch_size", "must be >= 1", observed=batch_size)
    patch = Patch(
        size_voxels=_ints(_req(p_raw, "size_voxels", "patch."), 3, "patch.size_voxels"),
        sliding_window_overlap=overlap,
        blend=blend,
        batch_size=batch_size,
        gaussian_sigma_scale=(
            float(p_raw["gaussian_sigma_scale"])
            if "gaussian_sigma_scale" in p_raw else None
        ),
    )

    tta_raw = dict(_req(d, "tta", ""))
    axes_raw = list(_req(tta_raw, "axes", "tta."))
    axes = tuple(tuple(int(a) for a in group) for group in axes_raw)
    if axes and "reduction" not in tta_raw:
        _refuse("tta.reduction", "required when tta.axes is non-empty")
    tta = Tta(
        axes=axes,
        reduction=(
            _enum(tta_raw["reduction"], ("mean", "max"), "tta.reduction")
            if "reduction" in tta_raw else None
        ),
    )

    io_raw = dict(_req(d, "io", ""))
    output_kind = _enum(_req(io_raw, "output_kind", "io."), OUTPUT_KINDS, "io.output_kind")
    if output_kind == "label" and "label_set" not in io_raw:
        _refuse(
            "io.label_set",
            "required when output_kind is `label`; it is a CLOSED set and a value "
            "outside it is a FAILED job (MOS-IMG-049)",
        )
    io = Io(
        input_dtype=_enum(_req(io_raw, "input_dtype", "io."), ("float32",),
                          "io.input_dtype"),
        input_layout=str(_req(io_raw, "input_layout", "io.")),
        channels=int(_req(io_raw, "channels", "io.")),
        output_kind=output_kind,
        label_set=(
            tuple(dict(e) for e in io_raw["label_set"]) if "label_set" in io_raw else None
        ),
    )
    if io.channels < 1:
        _refuse("io.channels", "must be >= 1", observed=io.channels)

    inv_raw = dict(_req(d, "inverse", ""))
    affine_tolerance = float(_req(inv_raw, "affine_tolerance", "inverse."))
    if affine_tolerance != AFFINE_TOLERANCE:
        _refuse(
            "inverse.affine_tolerance",
            f"MUST equal eps_aff = {AFFINE_TOLERANCE} (MOS-IMG-049, MOS-IMG-033)",
            observed=affine_tolerance,
            bound=AFFINE_TOLERANCE,
        )
    inverse = Inverse(
        aggregate=_enum(_req(inv_raw, "aggregate", "inverse."), AGGREGATES,
                        "inverse.aggregate"),
        undo_crop=_enum(_req(inv_raw, "undo_crop", "inverse."), ("pad_background",),
                        "inverse.undo_crop"),
        target_grid=_enum(_req(inv_raw, "target_grid", "inverse."), ("canonical",),
                          "inverse.target_grid"),
        assert_shape_equal=bool(_req(inv_raw, "assert_shape_equal", "inverse.")),
        affine_tolerance=affine_tolerance,
    )
    if inverse.assert_shape_equal is not True:
        _refuse("inverse.assert_shape_equal", "is the constant true (MOS-IMG-033)",
                observed=inverse.assert_shape_equal, bound=True)

    b_raw = dict(_req(d, "backend", ""))
    for k in ("resampler", "numpy"):
        if not str(_req(b_raw, k, "backend.")).strip():
            _refuse(f"backend.{k}", "must name a package and an exact version")
    backend = Backend(resampler=str(b_raw["resampler"]), numpy=str(b_raw["numpy"]))

    g_raw = dict(_req(d, "golden_fixture", ""))
    commit = str(_req(g_raw, "recorded_by_commit", "golden_fixture."))
    if not _COMMIT_RE.match(commit):
        _refuse(
            "golden_fixture.recorded_by_commit",
            "is a git object id: 40 lower-case hex characters, and carries no "
            "`sha256:` prefix (MOS-IMG-049)",
            observed=commit,
        )
    golden_fixture = GoldenFixture(
        path=str(_req(g_raw, "path", "golden_fixture.")),
        sha256=_digest(_req(g_raw, "sha256", "golden_fixture."), "golden_fixture.sha256"),
        output_tensor_sha256=_digest(
            _req(g_raw, "output_tensor_sha256", "golden_fixture."),
            "golden_fixture.output_tensor_sha256",
        ),
        output_shape=_ints(
            _req(g_raw, "output_shape", "golden_fixture."), 5,
            "golden_fixture.output_shape",
        ),
        recorded_at=str(_req(g_raw, "recorded_at", "golden_fixture.")),
        recorded_by_commit=commit,
    )

    spec = PreprocessingSpec(
        schema_version=schema_version,
        id=str(_req(d, "id", "")),
        version=str(_req(d, "version", "")),
        model_id=str(_req(d, "model_id", "")),
        model_version=str(_req(d, "model_version", "")),
        canonical_geometry=canonical_geometry,
        orientation_target=orientation_target,
        axis_order=axis_order,
        target_spacing_mm=_floats(_req(d, "target_spacing_mm", ""), 3,
                                  "target_spacing_mm"),
        image_interpolator=_enum(_req(d, "image_interpolator", ""), INTERPOLATORS,
                                 "image_interpolator"),
        label_interpolator=_enum(_req(d, "label_interpolator", ""), LABEL_INTERPOLATORS,
                                 "label_interpolator"),
        probability_interpolator=_enum(
            _req(d, "probability_interpolator", ""), PROBABILITY_INTERPOLATORS,
            "probability_interpolator",
        ),
        clip=clip,
        normalisation=normalisation,
        foreground_crop=foreground_crop,
        patch=patch,
        tta=tta,
        io=io,
        inverse=inverse,
        backend=backend,
        golden_fixture=golden_fixture,
    )

    # MOS-TRAIN-051, a REGISTRATION check: "Registration MUST reject a spec that violates
    # it." Element-wise, so a spec that is larger on two axes and smaller on one is
    # refused -- that is the axis on which the sliding window would pad.
    if foreground_crop.mode != "none":
        assert foreground_crop.min_size_voxels is not None
        short = [
            (i, m, p)
            for i, (m, p) in enumerate(
                zip(foreground_crop.min_size_voxels, patch.size_voxels)
            )
            if m < p
        ]
        if short:
            _refuse(
                "foreground_crop.min_size_voxels",
                "MUST be element-wise >= patch.size_voxels (MOS-TRAIN-051). This makes "
                "sliding_window_inference's own padding unreachable, which removes a "
                "second, undeclared padding implementation from the path. Axes "
                f"{[i for i, _, _ in short]} are short.",
                observed=list(foreground_crop.min_size_voxels),
                bound=list(patch.size_voxels),
            )
    # MOS-TRAIN-040: `zscore_foreground` "requires `foreground_crop.mode != none`".
    if normalisation.scheme == "zscore_foreground" and foreground_crop.mode == "none":
        _refuse(
            "normalisation.scheme",
            "`zscore_foreground` requires foreground_crop.mode != none (MOS-TRAIN-040)",
            observed=normalisation.scheme,
        )
    return spec
