# SPDX-License-Identifier: Apache-2.0
"""The transform-chain GENERATOR: `PreprocessingSpec` -> `configs/inference.json`.

`MOS-TRAIN-131`: "The MONAI transform chain in `configs/inference.json` MUST be
**generated** from the registered `PreprocessingSpec` by a single generator in
`medicalos-preprocessing`. It MUST NOT be hand-written beside the spec, and it MUST be
byte-reproducible from the spec alone."

`MOS-TRAIN-191` places this in 0.2.0 "even though no training run exists before 0.3.0. It
depends only on `PreprocessingSpec`, which ships in 0.1.0, and it closes the train/serve
skew hazard **before** a second model exists to skew. Deferring it to 0.3.0 would mean the
first model produced by this pipeline is also the first test of the mechanism meant to
protect it."

WHY THIS MODULE IS A RENDERER AND NOT A SECOND CONSTRUCTOR
-----------------------------------------------------------
`MOS-TRAIN-034` allows exactly one constructor of a chain, and it is
`medos.sdk.preprocess.build_chain`. So `serialize_chain` builds the chain through
that function and RENDERS it; `parse_chain_document` reads a rendered document back into
the same `Chain` type. Neither function knows the mapping from a spec field to a transform
argument -- only `build_chain` does -- and that is what makes
`build_chain_from_document(serialize_chain(spec))` a real test of the generator rather
than of itself: the renderer and the parser are the only code between the two chains, and
if either loses a field the tensors differ.

BYTE REPRODUCIBILITY
--------------------
`chain_bytes` goes through `medos.sdk.canonical.canonical_bytes`, the project's
one
canonicaliser (`MOS-REL-032`), so the document's bytes are a function of the spec's
CONTENT and not of anybody's key order. `chain_digest` is the digest a `TrainingRun` and a
`ValidationReport` cite.

A DELIBERATE DIVERGENCE FROM `MOS-TRAIN-131`'s WORKED EXAMPLE, REPORTED
-----------------------------------------------------------------------
The JSON printed in `MOS-TRAIN-131` contains `LoadImaged` with `ensure_channel_first`,
`CropForegroundd` with a `"$lambda x: x > -0.7"` select function, `CropForegroundd` placed
AFTER `ScaleIntensityRanged`, and a `ScaleIntensityRanged` that rescales to `[-1, 1]`.
Each of those contradicts a normative requirement in the same section:

  * `MOS-TRAIN-038` -- "`LoadImaged` and `EnsureChannelFirstd` MUST NOT appear in the
    serialized chain ... The hashed chain therefore begins at `Orientationd`."
  * `MOS-TRAIN-039` -- "The constructor below is normative. Its transform order MUST be
    the order of `MOS-IMG-031` steps (1) through (5)", which puts the crop at (3) and the
    clip at (4).
  * `MOS-TRAIN-040` / `MOS-TRAIN-048` -- the crop transform is
    `ForegroundCropToMinSized`, because `CropForegroundd` "has no minimum-extent
    guarantee, and `MOS-IMG-049` requires one".
  * `MOS-TRAIN-040` -- `select_fn` is "closed over the threshold, not a lambda", and
    `MOS-TRAIN-039` gives the reason.
  * `MOS-TRAIN-045` -- the clip is `b_min = a_min`, `b_max = a_max`, `clip=True`; a
    rescale to `[-1, 1]` is the `clip_scale` scheme, not the pleural-effusion spec's
    `zscore_dataset`.

The normative constructor wins, the example is REPORTED as a spec defect, and this
module emits the constructor's chain.

Spec: MOS-TRAIN-034, MOS-TRAIN-037, MOS-TRAIN-038, MOS-TRAIN-039, MOS-TRAIN-040,
MOS-TRAIN-052, MOS-TRAIN-053, MOS-TRAIN-054, MOS-TRAIN-055, MOS-TRAIN-066,
MOS-TRAIN-131, MOS-TRAIN-132, MOS-TRAIN-191, MOS-REL-032.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from medos.sdk.canonical import canonical_bytes, sha256_hex
from medos.sdk.errors import ChainRefused, Refusal
from medos.sdk.preprocess import BLEND, Chain, Transform, build_chain
from medos.sdk.spec import PreprocessingSpec

__all__ = [
    "TARGETS",
    "serialize_chain",
    "chain_bytes",
    "chain_digest",
    "parse_chain_document",
    "build_chain_from_document",
]

# `_target_` for each transform this generator emits. MONAI's own transforms are named
# bare, the way a MONAI bundle config names them; the one transform MedicalOS adds
# (`MOS-TRAIN-048`) is fully qualified, because a bare name would be resolved against
# `monai.transforms` and silently fail to be the MedicalOS one.
TARGETS: dict[str, str] = {
    "Orientationd": "Orientationd",
    "Spacingd": "Spacingd",
    "ForegroundCropToMinSized":
        "medos.sdk.transforms.ForegroundCropToMinSized",
    "ScaleIntensityRanged": "ScaleIntensityRanged",
    "NormalizeIntensityd": "NormalizeIntensityd",
    "EnsureTyped": "EnsureTyped",
}
_FROM_TARGET = {v: k for k, v in TARGETS.items()}

# `MOS-TRAIN-039`'s `_above`, referenced by path rather than inlined as a lambda.
SELECT_FN_TARGETS: dict[str, str] = {"above": "medos.sdk.chain._above"}
_FROM_SELECT_FN = {v: k for k, v in SELECT_FN_TARGETS.items()}


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


def _render(t: Transform) -> dict[str, Any]:
    """One `Compose` entry. `_target_` plus the transform's keyword arguments."""
    target = TARGETS.get(t.name)
    if target is None:  # pragma: no cover - build_chain emits no other name
        _refuse("unknown_transform", f"no _target_ registered for {t.name!r}",
                observed=t.name)
    out: dict[str, Any] = {"_target_": target}
    for key, value in t.args.items():
        if t.name == "ForegroundCropToMinSized" and key == "select_fn":
            # `MOS-TRAIN-040`: closed over the threshold, not a lambda. MONAI's
            # `_partial_` convention renders exactly that: a reference to a module-level
            # function with one argument bound.
            out["select_fn"] = {
                "_target_": SELECT_FN_TARGETS[str(value)],
                "_partial_": True,
                "threshold": t.args["threshold"],
            }
            continue
        if t.name == "ForegroundCropToMinSized" and key == "threshold":
            continue  # carried inside select_fn, in exactly one place
        out[key] = value
    return out


def _inferer(spec: PreprocessingSpec) -> dict[str, Any]:
    """Step (6). `MOS-TRAIN-052`: "every argument taken from the spec".

    `cval` is "the normalised image of the HU padding value; `MOS-TRAIN-051` makes it
    unreachable, and it is declared anyway so that 'unreachable' is a checkable claim
    rather than an assumption". It is computable only when the normalisation statistics
    are literals in the spec; for `zscore_case` there is no mean or std at generation time,
    so the generator REFUSES rather than emitting a `cval` it invented -- `MOS-TRAIN-132`.
    """
    if spec.normalisation.mean is None or spec.normalisation.std is None:
        _refuse(
            "cval_not_computable",
            "the sliding window's `cval` is the normalised image of "
            "canonical_geometry.padding_output_value (MOS-TRAIN-052), and "
            f"normalisation.scheme {spec.normalisation.scheme!r} carries no literal mean "
            "and std. The generator refuses rather than substituting a value.",
            observed=spec.normalisation.scheme,
            field="normalisation.scheme",
        )
    assert spec.normalisation.mean is not None and spec.normalisation.std is not None
    clamped = min(
        max(spec.canonical_geometry.padding_output_value, spec.clip.min_hu),
        spec.clip.max_hu,
    )
    cval = (clamped - spec.normalisation.mean) / spec.normalisation.std
    out: dict[str, Any] = {
        "_target_": "SlidingWindowInferer",
        "roi_size": list(spec.patch.size_voxels),
        "sw_batch_size": spec.patch.batch_size,
        "overlap": spec.patch.sliding_window_overlap,
        "mode": BLEND[spec.patch.blend],
        "padding_mode": "constant",
        "cval": cval,
        "progress": False,
    }
    if spec.patch.blend == "gaussian":
        out["sigma_scale"] = spec.patch.gaussian_sigma_scale
    return out


def _tta(spec: PreprocessingSpec) -> dict[str, Any]:
    """`MOS-TRAIN-040`'s `tta` row. NOT part of the hashed chain.

    `MOS-IMG-054` step 3 runs the self-test "with TTA disabled", so mirroring never enters
    the golden tensor. It is emitted because `MOS-TRAIN-054` requires the declared axes to
    correspond to the recorded training augmentation and a reader of the artifact has to be
    able to see them. "The lung-lobe spec below therefore declares `tta.axes: []`" -- an
    empty list means no TTA, and it is written rather than omitted.
    """
    out: dict[str, Any] = {
        "_target_": "monai.transforms.Flip",
        "axes": [list(a) for a in spec.tta.axes],
    }
    if spec.tta.reduction is not None:
        out["reduction"] = spec.tta.reduction
    return out


def serialize_chain(
    spec: PreprocessingSpec,
    keys: Sequence[str] = ("image",),
    label_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """`MOS-TRAIN-131`'s `configs/inference.json`, generated from the spec alone.

    Four blocks, and every one of them earns its place from a row of `MOS-TRAIN-040`'s
    serialization table:

      `preprocessing`  the `Compose` -- `MOS-IMG-031` steps (1)-(5)
      `inferer`        step (6), `sliding_window_inference` -- the `patch.*` rows
      `tta`            the `tta.axes` / `tta.reduction` row
      `backend`        the `backend.resampler` row, "records the dispatch, MOS-TRAIN-055"

    Nothing else: a block the table does not have a row for would be a field the chain
    documents and the model does not have, which is what `MOS-TRAIN-066` calls a defect.
    """
    chain = build_chain(spec, keys=keys, label_keys=label_keys)
    return {
        "preprocessing": {
            "_target_": "Compose",
            "unpack_items": False,
            "log_stats": False,
            "transforms": [_render(t) for t in chain.transforms],
        },
        "inferer": _inferer(spec),
        "tta": _tta(spec),
        "backend": {
            "resampler": spec.backend.resampler,
            "numpy": spec.backend.numpy,
        },
    }


def chain_bytes(spec: PreprocessingSpec, **kwargs: Any) -> bytes:
    """The document's canonical bytes.

    `MOS-TRAIN-131`: "byte-reproducible from the spec alone".
    """
    return canonical_bytes(serialize_chain(spec, **kwargs))


def chain_digest(spec: PreprocessingSpec, **kwargs: Any) -> str:
    """`sha256:<hex>` of `chain_bytes`. What a `TrainingRun` pins and a report cites."""
    return "sha256:" + sha256_hex(chain_bytes(spec, **kwargs))


def parse_chain_document(document: Mapping[str, Any]) -> Chain:
    """Read a rendered document back into a `Chain`. The inverse of `_render`.

    This is the TRAINING side of `MOS-TRAIN-133`: a training run receives
    `configs/inference.json` inside the bundle and builds its chain from that document,
    with no access to the `PreprocessingSpec` object the generator held. If the renderer
    drops a field or the parser misreads one, the chain this function returns differs from
    the one `build_chain` produced and the two tensors do not match.
    """
    pre = document.get("preprocessing")
    if not isinstance(pre, Mapping) or pre.get("_target_") != "Compose":
        _refuse(
            "document_has_no_compose",
            "configs/inference.json must carry a `preprocessing` block whose _target_ is "
            "Compose",
            observed=(pre or {}).get("_target_") if isinstance(pre, Mapping) else None,
            check_id="MOS-TRAIN-131",
        )
    assert isinstance(pre, Mapping)
    transforms: list[Transform] = []
    keys: tuple[str, ...] = ()
    all_keys: tuple[str, ...] = ()
    for entry in pre.get("transforms", ()):
        e = dict(entry)
        target = e.pop("_target_", None)
        name = _FROM_TARGET.get(str(target))
        if name is None:
            _refuse(
                "unknown_target",
                f"_target_ {target!r} is not a transform this generator emits; a "
                "hand-written chain beside the spec is what MOS-TRAIN-131 forbids",
                observed=target,
                check_id="MOS-TRAIN-131",
            )
        assert name is not None
        if name == "ForegroundCropToMinSized":
            sel = dict(e.pop("select_fn"))
            fn = _FROM_SELECT_FN.get(str(sel.get("_target_")))
            if fn is None or not sel.get("_partial_"):
                _refuse(
                    "select_fn_not_a_bound_predicate",
                    "select_fn must be a `_partial_` reference to a module-level "
                    "predicate closed over the threshold, not a lambda (MOS-TRAIN-040)",
                    observed=sel.get("_target_"),
                )
            e["select_fn"] = fn
            e["threshold"] = sel["threshold"]
        if name == "ScaleIntensityRanged":
            keys = tuple(e["keys"])
        if name == "Orientationd":
            all_keys = tuple(e["keys"])
        transforms.append(Transform(name=name, args=e))
    if not keys:
        keys = all_keys
    label_keys = tuple(k for k in all_keys if k not in keys)
    return Chain(transforms=tuple(transforms), keys=keys, label_keys=label_keys)


def build_chain_from_document(document: Mapping[str, Any]) -> Chain:
    """`parse_chain_document`, with `MOS-TRAIN-037`/`MOS-TRAIN-038` re-asserted.

    A document is an artifact that travels: it is signed with the weights, shipped to a
    site and loaded by a trainer that may not be this code. Re-checking that it carries no
    `Rand*` transform and no loader on the way IN is the same check `build_chain` makes on
    the way out, and the two are not redundant -- the second one catches a document that
    was edited after it was generated, which is precisely what `MOS-TRAIN-131` forbids and
    cannot otherwise detect without the spec.
    """
    chain = parse_chain_document(document)
    for name in chain.names:
        if name.startswith("Rand"):
            _refuse(
                "random_transform_in_document",
                f"{name} is a random augmentation; an augmentation that leaks into the "
                "spec is applied at serving time, non-deterministically, on every patient",
                observed=name,
                check_id="MOS-TRAIN-037",
            )
        if name in ("LoadImaged", "EnsureChannelFirstd"):
            _refuse(
                "loader_in_document",
                f"{name} MUST NOT appear in the serialized chain (MOS-TRAIN-038)",
                observed=name,
                check_id="MOS-TRAIN-038",
            )
    return chain
