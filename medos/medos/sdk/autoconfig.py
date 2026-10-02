# SPDX-License-Identifier: Apache-2.0
"""Dataset-fingerprint auto-configuration: the default backend, and the freeze. 17.7.6.

`MOS-TRAIN-211`: "For a capability whose `io.output_kind` is `label` (`MOS-IMG-049`), the
default `training_backend.kind` MUST be a dataset-fingerprint auto-configuring backend --
`nnunet` or `auto3dseg`."

THE HAZARD THIS MODULE EXISTS FOR, IN 17.7.6'S OWN WORDS
---------------------------------------------------------
"Auto-configuration's whole value is that it reads the cohort and picks constants. That is
also its whole hazard, because the code that picks them is the same code at training time
and at serving time, and only the data differ. A re-derivation at serve time against a
different cohort produces a different target spacing, a different intensity window and
different normalisation statistics -- a materially different transform, executed by a
byte-identical container, against a byte-identical weights digest, with every structural
check passing. This is train/serve skew in its most deceptive form: there is no version to
compare, no digest that differs, and no error anywhere. The model receives inputs it was
never trained on and returns plausible masks."

So this module does exactly two things and refuses to do a third:

  `export_spec_fields()`  transcribes a fingerprint document into `PreprocessingSpec`
                          fields, by `MOS-TRAIN-223`'s fixed mapping, refusing anything it
                          cannot map exactly (`MOS-TRAIN-132`'s rule, applied here).
  `fingerprint_digest()`  digests the fingerprint document under JCS, for the audit trail.

It does NOT derive a fingerprint. Deriving one means reading the cohort, and the two tools
that do it -- nnU-Net's planner and `Auto3DSeg`'s `DataAnalyzer` -- are adopted whole
(`MOS-TRAIN-210`'s register row). Re-implementing the derivation here would be a
`MOS-REL-032` violation and would also put the derivation in the platform's import
closure, which is what `MOS-TRAIN-225` forbids.

MOS-TRAIN-225, AND WHY IT IS A GREP
------------------------------------
"The nnU-Net planner, `nnUNetPlansManager`, `Auto3DSeg`'s `DataAnalyzer` and any
equivalent MUST NOT be present in the serving image's import closure, and CI MUST assert
their absence by module-name grep over the resolved closure." `FORBIDDEN_SERVING_MODULES`
is that list, spelled once, and `serving_closure_violations()` is the check. It is here and
not in the test file so that the list and the requirement live together.

MOS-TRAIN-224, THE TWO QUANTITIES WITH ONE ENGLISH NAME
--------------------------------------------------------
"The derived *training* batch size MUST be recorded in `TrainingRun.hyperparameters` and
MUST NOT be written into `PreprocessingSpec.patch.batch_size`. They are different
quantities with the same English name." `export_spec_fields()` returns the training batch
size under `hyperparameters`, in its own sub-dict, and never under `patch`. "Conflating
them makes a serving knob look like a trained property -- at which point changing it
appears to require revalidation -- or makes a trained property look like a knob, which is
worse."

Spec: MOS-TRAIN-135, MOS-TRAIN-136, MOS-TRAIN-210 to MOS-TRAIN-212, MOS-TRAIN-223 to
MOS-TRAIN-226, MOS-IMG-049, MOS-IMG-050, MOS-REL-032. Pure: no I/O, no database.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from medos.sdk.canonical import canonical_bytes, sha256_hex
from medos.sdk.errors import ChainRefused, Refusal

__all__ = [
    "AUTO_CONFIGURING_BACKENDS",
    "DERIVED_QUANTITIES",
    "FORBIDDEN_SERVING_MODULES",
    "default_backend_for",
    "export_spec_fields",
    "fingerprint_digest",
    "serving_closure_violations",
]

AUTO_CONFIGURING_BACKENDS: Final[tuple[str, ...]] = ("nnunet", "auto3dseg")

#: `MOS-TRAIN-225`'s grep list. Module-name prefixes, matched on the dotted path, so
#: `nnunetv2.experiment_planning.anything` is caught by `nnunetv2.experiment_planning`.
FORBIDDEN_SERVING_MODULES: Final[tuple[str, ...]] = (
    "nnunet.experiment_planning",
    "nnunetv2.experiment_planning",
    "nnunet.inference",                    # MOS-TRAIN-031: not on the serving path
    "nnunetv2.inference",
    "nnunetv2.utilities.plans_handling",   # nnUNetPlansManager lives here
    "monai.apps.auto3dseg",                # DataAnalyzer, BundleGen, AutoRunner
    "monai.auto3dseg",
)

#: `MOS-TRAIN-223`'s table, as data: one row per derived quantity, with the source key in
#: each backend's fingerprint document and the `PreprocessingSpec` field it lands in.
#: `None` as a source means the backend's document does not carry that quantity under a
#: single key, and the exporter refuses rather than composing one.
DERIVED_QUANTITIES: Final[dict[str, dict[str, Any]]] = {
    "target_spacing": {
        "spec_field": "target_spacing_mm",
        "nnunet": "configurations.3d_fullres.spacing",
        "auto3dseg": "hyper_parameters.resample_resolution",
    },
    "axis_permutation": {
        # "composed into `axis_order` and `orientation_target`; never carried as a
        # separate runtime step."
        "spec_field": "axis_order",
        "nnunet": "transpose_forward",
        "auto3dseg": "hyper_parameters.canonical_axis_order",
    },
    "intensity_window_min": {
        "spec_field": "clip.min_hu",
        "nnunet": "foreground_intensity_properties_per_channel.0.percentile_00_5",
        "auto3dseg": "hyper_parameters.intensity_bounds.0",
    },
    "intensity_window_max": {
        "spec_field": "clip.max_hu",
        "nnunet": "foreground_intensity_properties_per_channel.0.percentile_99_5",
        "auto3dseg": "hyper_parameters.intensity_bounds.1",
    },
    "normalisation_scheme": {
        "spec_field": "normalisation.scheme",
        "nnunet": "configurations.3d_fullres.normalization_schemes",
        "auto3dseg": "hyper_parameters.normalize_mode",
    },
    "dataset_mean": {
        "spec_field": "normalisation.mean",
        "nnunet": "foreground_intensity_properties_per_channel.0.mean",
        "auto3dseg": "stats_summary.image_foreground_stats.intensity.mean",
    },
    "dataset_std": {
        "spec_field": "normalisation.std",
        "nnunet": "foreground_intensity_properties_per_channel.0.std",
        "auto3dseg": "stats_summary.image_foreground_stats.intensity.stdev",
    },
    "foreground_crop": {
        "spec_field": "foreground_crop.mode",
        "nnunet": "configurations.3d_fullres.use_mask_for_norm",
        "auto3dseg": "hyper_parameters.crop_mode",
    },
    "patch_size": {
        "spec_field": "patch.size_voxels",
        "nnunet": "configurations.3d_fullres.patch_size",
        "auto3dseg": "hyper_parameters.patch_size",
    },
    "training_batch_size": {
        # MOS-TRAIN-224: **not** a PreprocessingSpec field.
        "spec_field": None,
        "nnunet": "configurations.3d_fullres.batch_size",
        "auto3dseg": "hyper_parameters.num_patches_per_iter",
    },
}

#: `MOS-IMG-049`'s normalisation schemes, and what each backend's own word maps to.
#: `MOS-TRAIN-223`: "`normalisation.scheme: zscore_dataset` with
#: `normalisation.statistics_source: spec`."
_NORMALISATION_MAP: Final[dict[str, str]] = {
    "CTNormalization": "zscore_dataset",
    "ct": "zscore_dataset",
    "range": "clip_scale",
}


def _refuse(code: str, message: str, **detail: Any) -> None:
    raise ChainRefused(
        (
            Refusal(
                check_id=detail.pop("check_id", "MOS-TRAIN-223"),
                code=code,
                message=message,
                observed=detail.pop("observed", None),
                bound=detail.pop("bound", None),
                detail=detail,
            ),
        )
    )


def default_backend_for(output_kind: str) -> str | None:
    """`MOS-TRAIN-211`'s default, or `None` when the requirement does not apply.

    Returns `'nnunet'` for a `label` capability -- the first of the two permitted
    auto-configuring backends, in the order `MOS-TRAIN-211` writes them -- and `None`
    otherwise. It returns a DEFAULT, never a mandate: a hand-configured run "MAY be
    submitted and MUST record a `backend_rationale`", which `medos.training.runs` enforces.
    """
    return "nnunet" if output_kind == "label" else None


def fingerprint_digest(document: Mapping[str, Any]) -> str:
    """`sha256:` over the JCS form of the fingerprint document. `MOS-TRAIN-223`.

    "The fingerprint document itself -- `plans.json` for nnU-Net, `datastats.yaml` plus the
    per-algorithm `hyper_parameters.yaml` for `Auto3DSeg` -- MUST be digested as
    `fingerprint_digest` and retained for audit, and MUST NOT be shipped inside the
    `ModelVersion` artifact as an executable configuration."
    """
    return "sha256:" + sha256_hex(canonical_bytes(dict(document)))


def _dig(document: Mapping[str, Any], dotted: str) -> Any:
    """Read `a.b.0.c` out of a nested mapping/sequence. Raises `KeyError` when absent."""
    node: Any = document
    for part in dotted.split("."):
        if isinstance(node, Mapping):
            if part not in node:
                raise KeyError(dotted)
            node = node[part]
        elif isinstance(node, Sequence) and not isinstance(node, str):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                raise KeyError(dotted) from None
        else:
            raise KeyError(dotted)
    return node


def export_spec_fields(
    document: Mapping[str, Any], *, backend: str
) -> dict[str, Any]:
    """Transcribe a fingerprint document into `PreprocessingSpec` fields. `MOS-TRAIN-223`.

    Returns `{"spec_fields": {...}, "hyperparameters": {...}, "fingerprint_digest": ...}`.
    `spec_fields` uses dotted `PreprocessingSpec` paths exactly as `DERIVED_QUANTITIES`
    names them, so a caller merges them into a spec document without this module having to
    know the spec's object graph.

    REFUSES rather than substitutes, on `MOS-TRAIN-223`'s closing paragraph: "The exporter
    MUST refuse to emit for any derived quantity it cannot map exactly and MUST NOT
    substitute the nearest available value, by the same argument `MOS-TRAIN-132` makes for
    the transform-chain generator. A backend version bump that changes a fingerprint field
    name MUST fail the export loudly rather than silently drop the field to a schema
    default."
    """
    if backend not in AUTO_CONFIGURING_BACKENDS:
        _refuse(
            "unknown_auto_configuring_backend",
            f"backend {backend!r} is not one of {AUTO_CONFIGURING_BACKENDS}; only a "
            "dataset-fingerprint auto-configuring backend has a fingerprint to export",
            observed=backend,
            bound=list(AUTO_CONFIGURING_BACKENDS),
        )

    spec_fields: dict[str, Any] = {}
    hyperparameters: dict[str, Any] = {}
    missing: list[str] = []

    for name, row in DERIVED_QUANTITIES.items():
        source = row.get(backend)
        if source is None:
            missing.append(f"{name} (no {backend} source key is fixed for it)")
            continue
        try:
            value = _dig(document, str(source))
        except KeyError:
            missing.append(f"{name} <- {source}")
            continue

        if name == "training_batch_size":
            # MOS-TRAIN-224. It lands in hyperparameters and NOWHERE near patch.
            hyperparameters["training_batch_size"] = int(value)
            continue

        field = str(row["spec_field"])
        spec_fields[field] = _coerce(name, field, value, backend=backend)

    if missing:
        _refuse(
            "fingerprint_field_not_mapped",
            "the fingerprint document does not carry " + "; ".join(missing) + ". "
            "MOS-TRAIN-223: the exporter MUST refuse to emit for any derived quantity it "
            "cannot map exactly and MUST NOT substitute the nearest available value. A "
            "backend version bump that changes a field name fails here rather than "
            "silently dropping the field to a schema default",
            observed=missing,
            bound=sorted(DERIVED_QUANTITIES),
        )

    # MOS-TRAIN-225: "`normalisation.statistics_source` MUST be `spec` for every capability
    # trained under an auto-configuring backend, so that MOS-IMG-050's prohibition on a
    # serve-time scheme change is enforceable from the spec alone."
    spec_fields["normalisation.statistics_source"] = "spec"

    return {
        "spec_fields": spec_fields,
        "hyperparameters": hyperparameters,
        "fingerprint_digest": fingerprint_digest(document),
        "backend": backend,
    }


def _coerce(name: str, field: str, value: Any, *, backend: str) -> Any:
    """Turn one fingerprint value into the spec's own type, or refuse. No substitution."""
    if name == "target_spacing":
        spacing = _as_floats(value, 3, field)
        return list(spacing)

    if name == "axis_permutation":
        # nnU-Net writes a permutation of (0,1,2); Auto3DSeg writes axis codes. Both are
        # accepted as what they are, and neither is guessed into the other.
        if isinstance(value, Sequence) and not isinstance(value, str):
            items = list(value)
            if all(isinstance(v, int) for v in items) and sorted(items) == [0, 1, 2]:
                return [("z", "y", "x")[i] for i in items]
            if all(isinstance(v, str) and len(v) == 1 for v in items) and len(items) == 3:
                return [str(v) for v in items]
        _refuse(
            "axis_permutation_not_representable",
            f"{field}: a {backend} axis permutation MUST be a permutation of (0,1,2) or "
            "three single-character axis codes; anything else would have to be guessed",
            observed=value,
        )

    if name in ("intensity_window_min", "intensity_window_max", "dataset_mean",
                "dataset_std"):
        try:
            return float(value)
        except (TypeError, ValueError):
            _refuse(
                "intensity_statistic_not_a_number",
                f"{field}: {value!r} is not a number",
                observed=value,
            )

    if name == "normalisation_scheme":
        raw = value[0] if isinstance(value, Sequence) and not isinstance(value, str) else value
        scheme = _NORMALISATION_MAP.get(str(raw))
        if scheme is None:
            _refuse(
                "normalisation_scheme_not_mapped",
                f"{field}: {raw!r} has no exact MOS-IMG-049 equivalent. MOS-TRAIN-223 "
                "fixes CTNormalization -> zscore_dataset and nothing else; substituting "
                "the nearest scheme would change what the model was trained on",
                observed=raw,
                bound=sorted(_NORMALISATION_MAP),
            )
        return scheme

    if name == "foreground_crop":
        # nnU-Net's crop-to-nonzero, and Auto3DSeg's `crop_mode`. MOS-IMG-049's vocabulary
        # is `none` / `threshold_bbox` / `mask`.
        if isinstance(value, bool):
            return "mask" if value else "threshold_bbox"
        text = str(value)
        if text in ("none", "threshold_bbox", "mask"):
            return text
        _refuse(
            "crop_mode_not_mapped",
            f"{field}: {text!r} is not one of MOS-IMG-049's foreground_crop modes",
            observed=text,
            bound=["none", "threshold_bbox", "mask"],
        )

    if name == "patch_size":
        return list(_as_ints(value, 3, field))

    _refuse("unmapped_quantity", f"{name} has no coercion rule", observed=name)


def _as_floats(value: Any, n: int, field: str) -> tuple[float, ...]:
    try:
        out = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        _refuse("not_a_vector", f"{field} MUST be {n} numbers", observed=value)
        raise
    if len(out) != n:
        _refuse("wrong_arity", f"{field} MUST be {n} numbers", observed=value, bound=n)
    return out


def _as_ints(value: Any, n: int, field: str) -> tuple[int, ...]:
    try:
        out = tuple(int(v) for v in value)
    except (TypeError, ValueError):
        _refuse("not_a_vector", f"{field} MUST be {n} integers", observed=value)
        raise
    if len(out) != n:
        _refuse("wrong_arity", f"{field} MUST be {n} integers", observed=value, bound=n)
    return out


def serving_closure_violations(modules: Iterable[str]) -> tuple[str, ...]:
    """Which of `modules` are things `MOS-TRAIN-225` forbids in the serving image.

    "CI MUST assert their absence by module-name grep over the resolved closure, in the
    same check that `MOS-TRAIN-136` already requires for nnU-Net preprocessing."

    Takes the closure rather than computing it, so the same function can be run against a
    live `sys.modules`, against a container's `pip freeze`, or against an import graph
    produced offline -- and so that this module has no reason to import anything it is
    checking for.
    """
    out: list[str] = []
    for name in modules:
        for forbidden in FORBIDDEN_SERVING_MODULES:
            if name == forbidden or name.startswith(forbidden + "."):
                out.append(name)
                break
    return tuple(sorted(set(out)))
