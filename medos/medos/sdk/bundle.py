# SPDX-License-Identifier: Apache-2.0
"""The MONAI Bundle: the source form of a native-mode model artifact. 17.7.3, 17.2.2.

`MOS-TRAIN-129`: "A `SUCCEEDED` run's output artifact MUST be a MONAI Bundle. The bundle
is the source form; the registered `ModelVersion` is the signed OCI artifact of
`MOS-REG-084`, and the mapping between them MUST be exactly:"

    | `model_version` OCI layer (`MOS-REG-084`) | Bundle path |
    | weights blob (`spec.weights`)             | `models/model.ts` or `models/model.onnx` |
    | `PreprocessingSpec` blob                  | `configs/preprocessing.json` |
    | golden-fixture volume blob                | `docs/golden_fixture.nii.gz` |
    | golden-fixture expected-tensor blob       | `docs/golden_fixture_tensor.f32` |
    | bundle metadata -> `artifact.manifest.spec.bundle` | `configs/metadata.json` |

A LIVE CONTRADICTION INSIDE CHAPTER 17, RESOLVED EXPLICITLY AND REPORTED
------------------------------------------------------------------------
`MOS-TRAIN-022` (17.2.2) gives a DIFFERENT layout for the same object and calls it fixed:
`medicalos/preprocessing.yaml`, `medicalos/golden_input.nii.gz`,
`medicalos/golden_tensor.f32`, weights at `models/model.pt`; and `MOS-TRAIN-023` then says
"The `medicalos/` subdirectory is a MedicalOS addition to the bundle layout and MUST be
present. A bundle without it MUST be rejected at packaging." Under `MOS-TRAIN-129`'s
layout there is no `medicalos/` subdirectory at all, so a bundle cannot satisfy both.

Five of the six paths differ, and one of the two is a `.yaml` where the other is a
`.json` -- so this is not a spelling difference that a lenient reader can absorb. Resolved
here in favour of `MOS-TRAIN-129`, for three reasons stated so the choice is reviewable:

  1. `MOS-TRAIN-190` places "MONAI Bundle as the native-mode artifact source form" in
     0.3.0 by reference to 17.7.3, which is `MOS-TRAIN-129`'s section.
  2. `MOS-TRAIN-129` is the one that states the OCI LAYER mapping, which is what
     `MOS-REG-084`'s signature actually covers; `MOS-TRAIN-022` maps to manifest FIELDS.
  3. `MOS-TRAIN-024` -- "`configs/inference.json` MUST NOT be the source of truth ...
     Those values live in `medicalos/preprocessing.yaml`" -- survives unchanged under
     `MOS-TRAIN-129` with `configs/preprocessing.json` as the source; the requirement is
     about WHICH FILE IS AUTHORITATIVE, not about which directory it sits in.

`verify()` therefore reads either convention, RECORDS which one it found, and refuses a
bundle that mixes them; `layout()` emits only `MOS-TRAIN-129`'s. REPORTED as a
specification defect rather than resolved silently in either direction.

WHAT `MOS-TRAIN-130` ADDS, AND WHY IT IS A REFUSAL AND NOT A WARNING
---------------------------------------------------------------------
"`configs/metadata.json` MUST declare `version`, `monai_version`, `pytorch_version`,
`numpy_version` and a complete `network_data_format` with `spatial_shape`, `dtype` and
`channel_def` for every input and output. The registry MUST reject a bundle whose
`network_data_format` disagrees with `ModelVersion.spec.io` ... Two declarations of the
tensor contract that are allowed to disagree are worse than one, because the mirrored
segmentation that results passes every structural check."

So `io_disagreements()` returns the field-level differences and `verify()` refuses on a
non-empty result. There is no tolerance and no `strict=False`.

Spec: MOS-TRAIN-022 to MOS-TRAIN-025, MOS-TRAIN-129 to MOS-TRAIN-134, MOS-TRAIN-227,
MOS-TRAIN-228, MOS-REG-030, MOS-REG-033, MOS-REG-036, MOS-REG-037, MOS-REG-084.
Pure: reads and writes a directory, touches no database and no network.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from medos.sdk.canonical import canonical_bytes, sha256_hex
from medos.sdk.chain import serialize_chain
from medos.sdk.errors import ChainRefused, Refusal, RunRefused
from medos.sdk.spec import PreprocessingSpec

__all__ = [
    "COMBINATION_RULES",
    "LAYOUT_MOS_TRAIN_022",
    "LAYOUT_MOS_TRAIN_129",
    "REQUIRED_METADATA_KEYS",
    "BundleReport",
    "bundle_digest",
    "ensemble_declaration",
    "io_disagreements",
    "layout",
    "metadata_document",
    "verify",
    "write_bundle",
]

#: `MOS-TRAIN-129`'s table, as `{role: path}`. The role names are the OCI layer roles
#: `MOS-REG-084` signs over, so a packager can walk this dict and never spell a path.
LAYOUT_MOS_TRAIN_129: Final[dict[str, str]] = {
    "metadata": "configs/metadata.json",
    "preprocessing": "configs/preprocessing.json",
    "inference": "configs/inference.json",
    "golden_volume": "docs/golden_fixture.nii.gz",
    "golden_tensor": "docs/golden_fixture_tensor.f32",
}

#: `MOS-TRAIN-022`'s table, for the same five roles. Read, never written. See the module
#: docstring for why both exist and which one this package emits.
LAYOUT_MOS_TRAIN_022: Final[dict[str, str]] = {
    "metadata": "configs/metadata.json",
    "preprocessing": "medicalos/preprocessing.yaml",
    "inference": "configs/inference.json",
    "golden_volume": "medicalos/golden_input.nii.gz",
    "golden_tensor": "medicalos/golden_tensor.f32",
}

#: `MOS-TRAIN-129`'s weights row: "one file, one digest". `model.pt` is the training
#: checkpoint and is explicitly "NOT the served artifact"; it MAY be present and is never
#: the weights layer.
_SERVED_WEIGHTS: Final[tuple[str, ...]] = (
    "models/model.ts", "models/model.onnx", "models/model.plan",
)
_CHECKPOINT: Final[str] = "models/model.pt"

#: `MOS-TRAIN-130`'s four declarations plus the tensor contract.
REQUIRED_METADATA_KEYS: Final[tuple[str, ...]] = (
    "version", "monai_version", "pytorch_version", "numpy_version",
    "network_data_format",
)
#: "with `spatial_shape`, `dtype` and `channel_def` for every input and output".
_TENSOR_KEYS: Final[tuple[str, ...]] = ("spatial_shape", "dtype", "channel_def")

#: `MOS-TRAIN-228`'s closed vocabulary. `staple` is in the table and is NOT permitted:
#: "the served function would differ per study, so no fixed artifact exists to evaluate".
COMBINATION_RULES: Final[dict[str, bool]] = {
    "mean_probability": True,           # the default
    "weighted_mean_probability": True,  # weights are a SELECTED quantity (MOS-TRAIN-215)
    "majority_vote": True,
    "staple": False,
}


def _refuse(refusals: Sequence[Refusal]) -> None:
    raise RunRefused(tuple(refusals))


def _one(check_id: str, code: str, message: str, **kw: Any) -> Refusal:
    return Refusal(check_id=check_id, code=code, message=message, **kw)


def layout() -> dict[str, str]:
    """The paths this packager writes. `MOS-TRAIN-129`, and nothing else."""
    return dict(LAYOUT_MOS_TRAIN_129)


# =====================================================================================
# metadata.json
# =====================================================================================
def metadata_document(
    *,
    version: str,
    monai_version: str,
    pytorch_version: str,
    numpy_version: str,
    inputs: Mapping[str, Mapping[str, Any]],
    outputs: Mapping[str, Mapping[str, Any]],
    ensemble: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`configs/metadata.json` in `MOS-TRAIN-130`'s shape. Refuses an incomplete tensor.

    `inputs` and `outputs` are `{name: {spatial_shape, dtype, channel_def, ...}}`. All
    three keys are required per tensor, for the reason `MOS-TRAIN-130` gives and
    `MOS-IMG-037` supplies the failure mode for: a model served with the wrong orientation
    returns "a plausible mirrored segmentation that passes every structural check".
    """
    problems: list[Refusal] = []
    for side, block in (("inputs", inputs), ("outputs", outputs)):
        if not block:
            problems.append(
                _one(
                    "MOS-TRAIN-130",
                    "network_data_format_incomplete",
                    f"network_data_format.{side} MUST declare at least one tensor",
                    observed=0,
                    bound=1,
                )
            )
        for name, tensor in block.items():
            missing = [k for k in _TENSOR_KEYS if k not in tensor]
            if missing:
                problems.append(
                    _one(
                        "MOS-TRAIN-130",
                        "tensor_contract_incomplete",
                        f"network_data_format.{side}.{name} is missing {missing}; "
                        "MOS-TRAIN-130 requires spatial_shape, dtype and channel_def for "
                        "every input and output",
                        observed=sorted(tensor.keys()),
                        bound=list(_TENSOR_KEYS),
                        detail={"side": side, "tensor": name, "missing": missing},
                    )
                )
    if ensemble is not None:
        problems.extend(_ensemble_problems(ensemble))
    if problems:
        _refuse(problems)

    document: dict[str, Any] = {
        "version": version,
        "monai_version": monai_version,
        "pytorch_version": pytorch_version,
        "numpy_version": numpy_version,
        "network_data_format": {
            "inputs": {k: dict(v) for k, v in inputs.items()},
            "outputs": {k: dict(v) for k, v in outputs.items()},
        },
    }
    if ensemble is not None:
        # `MOS-TRAIN-227`: the declaration "lives in the bundle's `configs/metadata.json`,
        # which MOS-TRAIN-129 already carries into `artifact.manifest.spec.bundle` and
        # which MOS-REG-084's signature therefore already covers".
        document["medicalos_ensemble"] = dict(ensemble)
    if extra:
        document.update({k: v for k, v in extra.items() if k not in document})
    return document


def ensemble_declaration(
    *,
    rule: str,
    members: Sequence[Mapping[str, Any]],
    weights: Sequence[float] | None = None,
) -> dict[str, Any]:
    """`MOS-TRAIN-227`'s declaration: members, their digests, and the combination rule.

    "it MUST name each member's trial `training_run_id`, its checkpoint digest, its weight
    if the rule is weighted, and the rule itself."

    `MOS-TRAIN-228` fixes where the rule is applied and this declaration records it
    explicitly rather than leaving it to the runner: "in **model space**, after the
    members' post-activation outputs and **before** the inverse transform of
    `MOS-IMG-032`, and before any post-processing selected under `MOS-TRAIN-033`".
    """
    declaration: dict[str, Any] = {
        "combination_rule": rule,
        "applied_in": "model_space",
        "applied_before": "inverse_transform",
        "members": [dict(m) for m in members],
    }
    if weights is not None:
        declaration["weights"] = [float(w) for w in weights]
    problems = _ensemble_problems(declaration)
    if problems:
        _refuse(problems)
    return declaration


def _ensemble_problems(declaration: Mapping[str, Any]) -> list[Refusal]:
    out: list[Refusal] = []
    rule = declaration.get("combination_rule")
    if rule not in COMBINATION_RULES:
        out.append(
            _one(
                "MOS-TRAIN-228",
                "combination_rule_not_in_vocabulary",
                f"the combination rule MUST be drawn from {sorted(COMBINATION_RULES)}",
                observed=rule,
                bound=sorted(COMBINATION_RULES),
            )
        )
    elif not COMBINATION_RULES[str(rule)]:
        out.append(
            _one(
                "MOS-TRAIN-228",
                "combination_rule_not_permitted",
                f"{rule!r} is estimated per case at inference time, so the served "
                "function would differ per study and no fixed artifact exists to "
                "evaluate. MOS-TRAIN-229 requires exactly one EvaluationRun measuring "
                "the ensemble AS SERVED, which a per-case fusion makes impossible",
                observed=rule,
                bound=[k for k, ok in COMBINATION_RULES.items() if ok],
            )
        )

    members = list(declaration.get("members") or ())
    if len(members) < 2:
        out.append(
            _one(
                "MOS-TRAIN-227",
                "ensemble_needs_members",
                "an ensemble declaration names every member; one member is not an "
                "ensemble and zero members is an artifact that resolves at serving time "
                "to whichever members are present (MOS-TRAIN-137)",
                observed=len(members),
                bound=2,
            )
        )
    for i, member in enumerate(members):
        missing = [k for k in ("training_run_id", "checkpoint_digest") if k not in member]
        if missing:
            out.append(
                _one(
                    "MOS-TRAIN-227",
                    "ensemble_member_unidentified",
                    f"member {i} is missing {missing}; MOS-TRAIN-227 requires each "
                    "member's trial training_run_id and its checkpoint digest",
                    observed=sorted(member.keys()),
                    bound=["training_run_id", "checkpoint_digest"],
                )
            )

    if declaration.get("combination_rule") == "weighted_mean_probability":
        weights = declaration.get("weights")
        if not weights or len(weights) != len(members):
            out.append(
                _one(
                    "MOS-TRAIN-228",
                    "weighted_rule_without_weights",
                    "weighted_mean_probability declares one fixed weight per member, and "
                    "the weights are a SELECTED quantity: they MUST be selected under "
                    "MOS-TRAIN-215 and recorded in selection_rule",
                    observed=len(weights or ()),
                    bound=len(members),
                )
            )
    elif "weights" in declaration:
        out.append(
            _one(
                "MOS-TRAIN-228",
                "weights_on_an_unweighted_rule",
                "only weighted_mean_probability carries weights; a weight beside "
                "mean_probability or majority_vote is a rule nobody declared",
                observed=declaration.get("combination_rule"),
            )
        )
    return out


def io_disagreements(
    metadata: Mapping[str, Any], spec_io: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Where `network_data_format` and `ModelVersion.spec.io` differ. `MOS-TRAIN-130`.

    `spec_io` is `MOS-REG-030`'s `spec.io` block -- `{"input": {...}, "output": {...}}`
    with `shape`, `dtype` and `layout`. The comparison is on the three quantities both
    sides name, and it is exact:

        bundle `spatial_shape`  <->  manifest `shape`   (the spatial suffix of it)
        bundle `dtype`          <->  manifest `dtype`
        bundle `channel_def`    <->  manifest `label_map`, when the manifest carries one

    Returns one dict per disagreement rather than raising, so a caller can render all of
    them at once. `verify()` is what refuses.
    """
    out: list[dict[str, Any]] = []
    fmt = dict(metadata.get("network_data_format") or {})

    for side, manifest_key in (("inputs", "input"), ("outputs", "output")):
        tensors = dict(fmt.get(side) or {})
        manifest = dict(spec_io.get(manifest_key) or {})
        if not tensors or not manifest:
            continue
        # One tensor per side is the shape chapter 6's manifest can express
        # (`spec.io.input` is an object, not an array), so the comparison is against the
        # first declared tensor and a bundle declaring more is reported rather than
        # silently half-checked.
        if len(tensors) > 1:
            out.append(
                {
                    "field": f"network_data_format.{side}",
                    "bundle": sorted(tensors),
                    "manifest": manifest_key,
                    "reason": "the manifest declares one tensor per side (MOS-REG-030) "
                              "and the bundle declares several; the registry cannot bind "
                              "them without guessing which is which",
                }
            )
            continue
        (_name, tensor), = tensors.items()

        m_shape = [int(x) for x in manifest.get("shape") or []]
        b_shape = [int(x) for x in tensor.get("spatial_shape") or []]
        if b_shape and m_shape and b_shape != m_shape[-len(b_shape):]:
            out.append(
                {
                    "field": f"{side}.spatial_shape",
                    "bundle": b_shape,
                    "manifest": m_shape,
                    "reason": "the bundle's spatial shape is not the spatial suffix of "
                              "the manifest's declared tensor shape",
                }
            )

        b_dtype = str(tensor.get("dtype") or "")
        m_dtype = str(manifest.get("dtype") or "")
        if b_dtype and m_dtype and b_dtype.replace("float", "fp") != m_dtype.replace(
            "float", "fp"
        ):
            out.append(
                {"field": f"{side}.dtype", "bundle": b_dtype, "manifest": m_dtype,
                 "reason": "two declarations of the element type that disagree"}
            )

        label_map = manifest.get("label_map")
        channel_def = tensor.get("channel_def")
        if label_map and isinstance(channel_def, Mapping):
            b_labels = {str(k): str(v) for k, v in channel_def.items()}
            m_labels = {str(k): str(v) for k, v in label_map.items()}
            if b_labels != m_labels:
                out.append(
                    {"field": f"{side}.channel_def", "bundle": b_labels,
                     "manifest": m_labels,
                     "reason": "the bundle's channel definition and the manifest's label "
                               "map name different segments for the same index"}
                )
    return tuple(out)


# =====================================================================================
# Writing and verifying a bundle on disk
# =====================================================================================
@dataclass(frozen=True)
class BundleReport:
    """What `verify()` observed. A record, never a verdict the caller must re-derive."""

    root: str
    layout_convention: str
    weights_path: str
    weights_digest: str
    files: Mapping[str, str]          # path -> sha256:...
    bundle_digest: str
    metadata: Mapping[str, Any]
    ensemble: Mapping[str, Any] | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_manifest_bundle_block(self) -> dict[str, Any]:
        """`artifact.manifest.spec.bundle` -- what `MOS-TRAIN-129` carries metadata into."""
        block: dict[str, Any] = {
            "layout": self.layout_convention,
            "bundle_digest": self.bundle_digest,
            "weights_path": self.weights_path,
            "weights_digest": self.weights_digest,
            "metadata": dict(self.metadata),
        }
        if self.ensemble is not None:
            block["ensemble"] = dict(self.ensemble)
        return block


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def bundle_digest(files: Mapping[str, str]) -> str:
    """One content digest over `{path: file digest}`. `MOS-TRAIN-137`.

    "An nnU-Net ensemble ... MUST be exported as a single artifact with one content
    digest, or MUST NOT be registered. A `ModelVersion` that resolves at serving time to
    'whichever fold directories are present' has no content digest."

    Computed over the canonical JSON of the sorted map, so it is reproducible from the
    listing alone and does not depend on a tar's timestamps or member order.
    """
    return "sha256:" + sha256_hex(canonical_bytes(dict(sorted(files.items()))))


def write_bundle(
    root: str | Path,
    *,
    spec: PreprocessingSpec,
    metadata: Mapping[str, Any],
    weights: bytes,
    weights_format: str,
    golden_volume: bytes,
    golden_tensor: bytes,
    checkpoint: bytes | None = None,
    extra_files: Mapping[str, bytes] | None = None,
) -> BundleReport:
    """Write `MOS-TRAIN-129`'s layout and return what was written.

    `configs/inference.json` is GENERATED here from `spec` and is never accepted as an
    argument. `MOS-TRAIN-131`: "It MUST be **generated** from the registered
    `PreprocessingSpec` by a single generator in `medicalos-preprocessing`. It MUST NOT be
    hand-written beside the spec, and it MUST be byte-reproducible from the spec alone."
    `MOS-TRAIN-024` says the same thing from the other side: "Two editable copies of
    `overlap` is exactly the skew this chapter exists to prevent."

    `configs/preprocessing.json` is the spec's own serialisation, byte-identical to the
    registered `preprocessing_spec` artifact (`MOS-REG-086`).
    """
    if weights_format not in ("torchscript", "onnx", "tensorrt_plan"):
        raise ValueError(
            f"weights_format {weights_format!r} is not one of the three serialised forms "
            "MOS-TRAIN-154's table admits"
        )
    weights_path = {
        "torchscript": "models/model.ts",
        "onnx": "models/model.onnx",
        "tensorrt_plan": "models/model.plan",
    }[weights_format]

    base = Path(root)
    paths = layout()
    # Raises `ChainRefused` naming the field when the spec cannot be represented exactly
    # (`MOS-TRAIN-132`). Deliberately not caught: a bundle whose inference config was
    # substituted is the failure this whole module exists to prevent.
    inference = serialize_chain(spec)

    payload: dict[str, bytes] = {
        paths["metadata"]: canonical_bytes(dict(metadata)) + b"\n",
        paths["preprocessing"]: canonical_bytes(spec.as_json()) + b"\n",
        paths["inference"]: canonical_bytes(inference) + b"\n",
        paths["golden_volume"]: golden_volume,
        paths["golden_tensor"]: golden_tensor,
        weights_path: weights,
    }
    if checkpoint is not None:
        # `MOS-TRAIN-129`: "`models/model.pt` -- training checkpoint, NOT the served
        # artifact." It is carried for diagnosis and is never the weights layer.
        payload[_CHECKPOINT] = checkpoint
    for name, data in (extra_files or {}).items():
        payload.setdefault(name, data)

    for rel, data in payload.items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    return verify(base)


def verify(root: str | Path, *, spec_io: Mapping[str, Any] | None = None) -> BundleReport:
    """Read a bundle from disk and refuse it if it is not one. `MOS-TRAIN-023/129/130`.

    Refuses, with every problem listed at once:
      * neither layout convention is complete, or both are partially present;
      * no served weights file, or more than one;
      * `configs/metadata.json` missing any of `MOS-TRAIN-130`'s declarations;
      * an ensemble declaration outside `MOS-TRAIN-228`'s vocabulary;
      * `network_data_format` disagreeing with `spec_io`, when one is supplied.
    """
    base = Path(root)
    problems: list[Refusal] = []
    notes: list[str] = []

    present_129 = {k: v for k, v in LAYOUT_MOS_TRAIN_129.items() if (base / v).is_file()}
    present_022 = {k: v for k, v in LAYOUT_MOS_TRAIN_022.items() if (base / v).is_file()}
    # `metadata` and `inference` are at the same path in both, so they cannot discriminate.
    discriminating_129 = {k for k in present_129 if k not in ("metadata", "inference")}
    discriminating_022 = {k for k in present_022 if k not in ("metadata", "inference")}

    if discriminating_129 and discriminating_022:
        problems.append(
            _one(
                "MOS-TRAIN-129",
                "bundle_layout_mixed",
                "the bundle carries both MOS-TRAIN-129's paths (configs/"
                "preprocessing.json, docs/golden_fixture*) and MOS-TRAIN-022's "
                "(medicalos/preprocessing.yaml, medicalos/golden_*). The two are "
                "different statements of the same fixed layout and chapter 17 contains "
                "both; a bundle MUST pick one, because a reader that finds two "
                "preprocessing documents has no rule for which one the model was trained "
                "with",
                observed=sorted(discriminating_129 | discriminating_022),
                detail={"MOS-TRAIN-129": sorted(discriminating_129),
                        "MOS-TRAIN-022": sorted(discriminating_022)},
            )
        )
        convention, chosen = "mixed", LAYOUT_MOS_TRAIN_129
    elif discriminating_022:
        convention, chosen = "MOS-TRAIN-022", LAYOUT_MOS_TRAIN_022
        notes.append(
            "read under MOS-TRAIN-022's layout; this packager emits MOS-TRAIN-129's. "
            "The two requirements disagree and the disagreement is reported, not patched."
        )
    else:
        convention, chosen = "MOS-TRAIN-129", LAYOUT_MOS_TRAIN_129

    missing = [v for v in chosen.values() if not (base / v).is_file()]
    if missing:
        problems.append(
            _one(
                "MOS-TRAIN-129",
                "bundle_layout_incomplete",
                f"the bundle is missing {missing}. MOS-TRAIN-023: a bundle without the "
                "MedicalOS additions MUST be rejected at packaging, because MOS-REG-036 "
                "requires the golden fixture and MOS-SVC-016 requires the "
                "PreprocessingSpec pin, and a stock MONAI Bundle has a slot for neither",
                observed=sorted(v for v in chosen.values() if (base / v).is_file()),
                bound=sorted(chosen.values()),
                detail={"layout": convention, "missing": missing},
            )
        )

    served = [p for p in _SERVED_WEIGHTS if (base / p).is_file()]
    if len(served) != 1:
        problems.append(
            _one(
                "MOS-TRAIN-129",
                "weights_not_one_file_one_digest",
                "MOS-TRAIN-129's weights row is 'one file, one digest'. "
                f"Found {served or 'none'}. models/model.pt is the training checkpoint "
                "and is explicitly NOT the served artifact",
                observed=served,
                bound=1,
            )
        )

    metadata: dict[str, Any] = {}
    meta_path = base / chosen["metadata"]
    if meta_path.is_file():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(
                _one("MOS-TRAIN-130", "metadata_not_json",
                     f"configs/metadata.json is not JSON: {exc}", observed=str(exc))
            )
        if metadata:
            absent = [k for k in REQUIRED_METADATA_KEYS if k not in metadata]
            if absent:
                problems.append(
                    _one(
                        "MOS-TRAIN-130",
                        "metadata_incomplete",
                        f"configs/metadata.json is missing {absent}",
                        observed=sorted(metadata.keys()),
                        bound=list(REQUIRED_METADATA_KEYS),
                    )
                )
            fmt = dict(metadata.get("network_data_format") or {})
            for side in ("inputs", "outputs"):
                for name, tensor in dict(fmt.get(side) or {}).items():
                    gaps = [k for k in _TENSOR_KEYS if k not in (tensor or {})]
                    if gaps:
                        problems.append(
                            _one(
                                "MOS-TRAIN-130",
                                "tensor_contract_incomplete",
                                f"network_data_format.{side}.{name} is missing {gaps}",
                                observed=sorted((tensor or {}).keys()),
                                bound=list(_TENSOR_KEYS),
                            )
                        )

    ensemble = metadata.get("medicalos_ensemble") if metadata else None
    if isinstance(ensemble, Mapping):
        problems.extend(_ensemble_problems(ensemble))

    if spec_io is not None and metadata:
        for gap in io_disagreements(metadata, spec_io):
            problems.append(
                _one(
                    "MOS-TRAIN-130",
                    "tensor_contract_disagrees_with_manifest",
                    f"{gap['field']}: bundle {gap['bundle']!r} vs manifest "
                    f"{gap['manifest']!r}. {gap['reason']}. Two declarations of the "
                    "tensor contract that are allowed to disagree are worse than one, "
                    "because the mirrored segmentation that results passes every "
                    "structural check",
                    observed=gap["bundle"],
                    bound=gap["manifest"],
                    detail=dict(gap),
                )
            )

    if problems:
        _refuse(problems)

    files = {
        str(p.relative_to(base)).replace("\\", "/"): _sha256_file(p)
        for p in sorted(base.rglob("*"))
        if p.is_file()
    }
    return BundleReport(
        root=str(base),
        layout_convention=convention,
        weights_path=served[0],
        weights_digest=files[served[0]],
        files=files,
        bundle_digest=bundle_digest(files),
        metadata=metadata,
        ensemble=dict(ensemble) if isinstance(ensemble, Mapping) else None,
        notes=tuple(notes),
    )


def inference_config_matches_spec(root: str | Path, spec: PreprocessingSpec) -> bool:
    """Is `configs/inference.json` byte-identical to what the generator emits? `MOS-TRAIN-131`.

    "byte-reproducible from the spec alone". A `False` here means somebody edited the
    config beside the spec, which is `MOS-TRAIN-024`'s "two editable copies of `overlap`".
    """
    base = Path(root)
    for candidate in {LAYOUT_MOS_TRAIN_129["inference"], LAYOUT_MOS_TRAIN_022["inference"]}:
        path = base / candidate
        if not path.is_file():
            continue
        try:
            emitted = canonical_bytes(serialize_chain(spec))
        except ChainRefused:
            return False
        on_disk = canonical_bytes(json.loads(path.read_text(encoding="utf-8")))
        return on_disk == emitted
    return False
