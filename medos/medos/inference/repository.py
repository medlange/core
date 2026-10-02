# SPDX-License-Identifier: Apache-2.0
"""The Triton model repository: naming, `config.pbtxt`, `medicalos.json`, and the gates.

This module is chapter 13 §13.10.1 turned into code.  It owns four things and no I/O
beyond writing the repository directory that `tritond` hands to Triton:

  * `triton_model_name` -- the deterministic name derivation (`MOS-OPS-068`);
  * `ModelManifest` -- the `medicalos.json` of `MOS-OPS-070`, parsed and validated;
  * `render_config_pbtxt` -- generation of a config that satisfies `MOS-OPS-072/073/075`
    by construction;
  * `validate_config_pbtxt` -- the refusal gates of `MOS-OPS-071` and `MOS-OPS-072`,
    applied to any config, including one that arrived inside a published artifact.

WHY NAME DERIVATION IS A FUNCTION AND NOT A COLUMN
    `MOS-OPS-068` requires two versions of one model to be resident simultaneously, which
    is what makes canary and blue/green possible at all (chapter 6).  Triton has exactly
    one namespace -- the model name -- so the version has to live inside it.  Deriving it
    means a mis-typed mapping row cannot make `3.2.1` serve `3.2.0`'s weights;
    `MOS-OPS-069` then forbids using Triton's own integer versioning for the same purpose,
    because Triton's version ordering would silently promote `2` over `1`.

CPU-MODE DEVIATION, STATED
    §13.10.1's worked example is a `tensorrt_plan` on a GPU.  This deployment has no GPU
    (`docs/spec/15-delivery.md` §15.2.4 asks only that native inference move onto the
    shared Triton; the compose profile is CPU).  Two consequences are carried explicitly
    rather than hidden:

      * `platform` is `onnxruntime_onnx`, and `instance_group.kind` is `KIND_CPU`.
      * `MOS-OPS-070`'s refusal gate ("`built_for.cuda_compute_capability` does not equal
        the GPU's") is `tensorrt_plan`-specific by its own wording.  It is IMPLEMENTED for
        that backend and is inapplicable to a portable ONNX graph; `check_built_for`
        below applies the gate that IS meaningful for ONNX -- that the declared opset and
        backend are ones the server actually has -- and says which gate it skipped and
        why.  Nothing is quietly waived.

Spec: MOS-OPS-068, MOS-OPS-069, MOS-OPS-070, MOS-OPS-071, MOS-OPS-072, MOS-OPS-073,
MOS-OPS-075, MOS-OPS-078, MOS-SVC-058.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from medos.inference.backend import ModelRef

__all__ = [
    "TRITON_VERSION_DIR",
    "triton_model_name",
    "ModelManifest",
    "ModelRepositoryError",
    "ConfigRejected",
    "render_config_pbtxt",
    "validate_config_pbtxt",
    "write_model_directory",
    "manifest_key",
    "artifact_key",
    "warmup_key",
]

# MOS-OPS-069: "The Triton version directory MUST always be `1`."  A constant, not a
# parameter: making it configurable is how a deployment ends up expressing ModelVersion
# through Triton's integer versioning, which is the exact thing MOS-OPS-069 forbids.
TRITON_VERSION_DIR = "1"

_FORBIDDEN_STANZAS = (
    # MOS-OPS-071: a config that builds a TensorRT engine at load time performs a backend
    # conversion, which changes numerics and therefore produces a model no EvaluationRun
    # covers.  The stanza is rejected by NAME, at registration, before Triton sees it.
    "execution_accelerators",
    # MOS-OPS-074: "The response cache is forbidden outright: a cache keyed on input
    # tensor bytes can return one patient's mask for another patient's identical-looking
    # patch."  Per-model enablement is a second door onto the same defect.
    "response_cache",
)

_DYNAMIC_BATCHING_RE = re.compile(r"^\s*dynamic_batching\s*(\{|$)", re.MULTILINE)
_MAX_BATCH_RE = re.compile(r"^\s*max_batch_size\s*:\s*(\d+)", re.MULTILINE)
# Scoped to the `instance_group` stanza rather than matching every `count:` in the file.
# `model_warmup` also carries a `count`, and it legitimately IS 1 (MOS-OPS-075 runs the
# golden fixture once); a file-wide match would conflate the two rules and reject a
# perfectly conformant warm-up block under MOS-OPS-073's name.
_INSTANCE_GROUP_RE = re.compile(r"instance_group\s*\[(.*?)\]", re.DOTALL)
_COUNT_RE = re.compile(r"\bcount\s*:\s*(\d+)")


class ModelRepositoryError(RuntimeError):
    """The published artifact is not something this platform will serve."""


class ConfigRejected(ModelRepositoryError):
    """A `config.pbtxt` trips one of §13.10.1's refusal gates. Carries the rule id."""

    def __init__(self, rule: str, message: str) -> None:
        super().__init__(f"{rule}: {message}")
        self.rule = rule


def triton_model_name(model_id: str, model_version: str) -> str:
    """MOS-OPS-068, verbatim:

        triton_model_name = replace(model_id, ".", "_") + "__" + replace(semver, ".", "_")
        pulmo.pleural-effusion @ 3.2.1  ->  pulmo_pleural-effusion__3_2_1

    `ModelRef` validates the character set first, so the substitution cannot collide.
    """
    ref = ModelRef(model_id=model_id, model_version=model_version)
    return ref.model_id.replace(".", "_") + "__" + ref.model_version.replace(".", "_")


# -----------------------------------------------------------------------------------
# Object-store layout.  One prefix per (model_id, version); nothing is ever overwritten.
# -----------------------------------------------------------------------------------
def _prefix(ref: ModelRef) -> str:
    return f"models/{ref.model_id}/{ref.model_version}"


def manifest_key(ref: ModelRef) -> str:
    return f"{_prefix(ref)}/medicalos.json"


def artifact_key(ref: ModelRef, filename: str) -> str:
    return f"{_prefix(ref)}/{filename}"


def warmup_key(ref: ModelRef, filename: str) -> str:
    return f"{_prefix(ref)}/warmup/{filename}"


@dataclass(frozen=True)
class ModelManifest:
    """`medicalos.json` (MOS-OPS-070), parsed.

    Every field below appears in §13.10.1's worked example except `selftest`, which is
    `MOS-OPS-015`'s preprocessing self-test made checkable: a golden input file plus the
    SHA-256 of the output tensor recorded at training time.  §13.10.1 mentions that
    self-test in `MOS-OPS-075`'s note ("it does not replace the MOS-IMG preprocessing
    self-test, which compares an output tensor hash against the training-time value") but
    puts the value nowhere; without a field it cannot be compared, so it is carried here
    and the addition is reported.
    """

    model_id: str
    model_version: str
    artifact_digest: str
    artifact_filename: str
    preprocessing_version: str
    backend: str
    built_for: dict[str, Any]
    input_name: str
    output_name: str
    input_dims: tuple[int, ...]
    output_dims: tuple[int, ...]
    allowed_patch_batch_sizes: tuple[int, ...]
    weights_bytes: int
    workspace_bytes_by_patch_batch: dict[str, int]
    selftest: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> ModelRef:
        return ModelRef(model_id=self.model_id, model_version=self.model_version)

    @property
    def triton_model_name(self) -> str:
        return triton_model_name(self.model_id, self.model_version)

    def footprint_bytes(self, patch_batch_size: int, instance_count: int = 1) -> int:
        """MOS-OPS-079's arithmetic, verbatim:

            footprint_bytes(m) = weights_bytes(m)
                               + workspace_bytes_by_patch_batch(m)[patch_batch] * count

        `MOS-OPS-078` makes `patch_batch_size` an engineering knob that must not change
        outputs, which is what lets this be a capacity decision; a value outside
        `allowed_patch_batch_sizes` is refused here rather than quietly clamped, because
        the bit-identity assertion was only made for the listed values.
        """
        if patch_batch_size not in self.allowed_patch_batch_sizes:
            raise ModelRepositoryError(
                f"{self.model_id}@{self.model_version}: patch_batch_size="
                f"{patch_batch_size} is outside allowed_patch_batch_sizes="
                f"{list(self.allowed_patch_batch_sizes)}; MOS-OPS-078 only asserts "
                "bit-identical output across the listed values"
            )
        workspace = self.workspace_bytes_by_patch_batch.get(str(patch_batch_size))
        if workspace is None:
            raise ModelRepositoryError(
                f"{self.model_id}@{self.model_version}: no workspace_bytes recorded for "
                f"patch_batch_size={patch_batch_size}"
            )
        return int(self.weights_bytes) + int(workspace) * int(instance_count)

    @staticmethod
    def from_json(data: bytes | str | dict[str, Any]) -> ModelManifest:
        raw: dict[str, Any]
        if isinstance(data, dict):
            raw = data
        else:
            raw = json.loads(data)
        required = (
            "model_id",
            "model_version",
            "artifact_digest",
            "artifact_filename",
            "preprocessing_version",
            "backend",
            "built_for",
            "input_name",
            "output_name",
            "input_dims",
            "output_dims",
            "allowed_patch_batch_sizes",
            "weights_bytes",
            "workspace_bytes_by_patch_batch",
        )
        missing = [key for key in required if key not in raw]
        if missing:
            raise ModelRepositoryError(
                f"medicalos.json is missing {missing}; MOS-OPS-070 makes the manifest "
                "part of the signed artifact, so an incomplete one is a packaging defect"
            )
        if not str(raw["artifact_digest"]).startswith("sha256:"):
            raise ModelRepositoryError(
                "artifact_digest must be 'sha256:<hex>'; §13.10.1's example is explicit "
                "about the algorithm prefix"
            )
        return ModelManifest(
            model_id=str(raw["model_id"]),
            model_version=str(raw["model_version"]),
            artifact_digest=str(raw["artifact_digest"]),
            artifact_filename=str(raw["artifact_filename"]),
            preprocessing_version=str(raw["preprocessing_version"]),
            backend=str(raw["backend"]),
            built_for=dict(raw["built_for"]),
            input_name=str(raw["input_name"]),
            output_name=str(raw["output_name"]),
            input_dims=tuple(int(d) for d in raw["input_dims"]),
            output_dims=tuple(int(d) for d in raw["output_dims"]),
            allowed_patch_batch_sizes=tuple(
                int(b) for b in raw["allowed_patch_batch_sizes"]
            ),
            weights_bytes=int(raw["weights_bytes"]),
            workspace_bytes_by_patch_batch={
                str(k): int(v) for k, v in raw["workspace_bytes_by_patch_batch"].items()
            },
            selftest=dict(raw.get("selftest", {})),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "model_id": self.model_id,
                "model_version": self.model_version,
                "artifact_digest": self.artifact_digest,
                "artifact_filename": self.artifact_filename,
                "preprocessing_version": self.preprocessing_version,
                "backend": self.backend,
                "built_for": self.built_for,
                "input_name": self.input_name,
                "output_name": self.output_name,
                "input_dims": list(self.input_dims),
                "output_dims": list(self.output_dims),
                "allowed_patch_batch_sizes": list(self.allowed_patch_batch_sizes),
                "weights_bytes": self.weights_bytes,
                "workspace_bytes_by_patch_batch": self.workspace_bytes_by_patch_batch,
                "selftest": self.selftest,
            },
            indent=2,
            sort_keys=True,
        )


# Backends this platform will serve, and the `platform:` string each maps to.  A closed
# set: MOS-OPS-071 makes the backend a numerics-relevant property of the ModelVersion, so
# an unknown value is a refusal rather than a pass-through to Triton.
_PLATFORM_BY_BACKEND = {
    "tensorrt_plan": "tensorrt_plan",
    "onnxruntime_onnx": "onnxruntime_onnx",
    "pytorch_libtorch": "pytorch_libtorch",
}

_DEFAULT_ARTIFACT_FILENAME = {
    "tensorrt_plan": "model.plan",
    "onnxruntime_onnx": "model.onnx",
    "pytorch_libtorch": "model.pt",
}


def render_config_pbtxt(
    manifest: ModelManifest,
    *,
    kind: str = "KIND_CPU",
    instance_count: int = 1,
    warmup_filename: str | None = None,
    warmup_batch: int | None = None,
) -> str:
    """Generate a `config.pbtxt` that satisfies §13.10.1 by construction.

    Generated rather than published-and-trusted because three of the rules are about what
    must be ABSENT (`MOS-OPS-071`'s accelerator stanza, `MOS-OPS-072`'s dynamic batcher,
    `MOS-OPS-074`'s response cache) and absence is not something a publisher can be relied
    on to get right.  The published artifact still gets `validate_config_pbtxt` run over
    it if it ships one -- generation and validation are both applied, not either/or.
    """
    platform = _PLATFORM_BY_BACKEND.get(manifest.backend)
    if platform is None:
        raise ModelRepositoryError(
            f"backend {manifest.backend!r} is not one this platform serves "
            f"({sorted(_PLATFORM_BY_BACKEND)}); MOS-OPS-071 makes the backend part of "
            "the ModelVersion identity, so an unknown one is refused, not forwarded"
        )
    if instance_count != 1:
        # MOS-OPS-073 permits count > 1 only with a measured throughput gain recorded on
        # the ModelVersion.  Nothing here records one, so the only honest default is 1.
        raise ModelRepositoryError(
            "MOS-OPS-073: instance_group.count > 1 multiplies workspace_bytes by count "
            "and MUST be justified by a measured throughput gain recorded on the "
            "ModelVersion; no such measurement exists for this deployment"
        )

    def _dims(dims: tuple[int, ...]) -> str:
        return ", ".join(str(d) for d in dims)

    lines = [
        f'name: "{manifest.triton_model_name}"',
        f'platform: "{platform}"',
        # MOS-OPS-072: `0`, with the batch dimension declared explicitly as the leading -1.
        "max_batch_size: 0",
        "",
        "input [",
        f'  {{ name: "{manifest.input_name}", data_type: TYPE_FP32, '
        f"dims: [ {_dims(manifest.input_dims)} ] }}",
        "]",
        "output [",
        f'  {{ name: "{manifest.output_name}", data_type: TYPE_FP32, '
        f"dims: [ {_dims(manifest.output_dims)} ] }}",
        "]",
        "",
        # MOS-OPS-073: count 1.
        f"instance_group [ {{ kind: {kind}, count: {instance_count} }} ]",
        "",
        # MOS-OPS-069: the version directory is always 1, and the policy says so rather
        # than leaving Triton's "latest" policy to pick.
        "version_policy { specific { versions: [ 1 ] } }",
    ]
    if warmup_filename is not None:
        batch = warmup_batch or manifest.allowed_patch_batch_sizes[-1]
        warm_dims = (batch, *manifest.input_dims[1:])
        # MOS-OPS-075: warm-up with the golden fixture shipped in the artifact, once, at
        # load.  A latency measure only -- it is NOT the MOS-OPS-015 self-test, which
        # tritond runs separately and which compares an output hash.
        lines += [
            "",
            "model_warmup [",
            "  {",
            f'    name: "golden_patch_batch{batch}"',
            "    batch_size: 0",
            "    count: 1",
            "    inputs {",
            f'      key: "{manifest.input_name}"',
            "      value {",
            "        data_type: TYPE_FP32",
            f"        dims: [ {_dims(warm_dims)} ]",
            f'        input_data_file: "{Path(warmup_filename).name}"',
            "      }",
            "    }",
            "  }",
            "]",
        ]
    return "\n".join(lines) + "\n"


def validate_config_pbtxt(text: str) -> None:
    """Apply §13.10.1's refusal gates to a `config.pbtxt`. Raises `ConfigRejected`.

    Textual rather than protobuf-parsed on purpose: the rules are about the PRESENCE of
    named stanzas, the parser for Triton's config proto is not available outside the
    server, and a regex that over-matches fails closed (a refusal) rather than open.
    """
    stripped = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    for stanza in _FORBIDDEN_STANZAS:
        if re.search(rf"\b{stanza}\b", stripped):
            rule = "MOS-OPS-071" if stanza == "execution_accelerators" else "MOS-OPS-074"
            raise ConfigRejected(
                rule,
                f"config.pbtxt contains a {stanza!r} stanza. "
                + (
                    "Building a TensorRT engine at load time is a backend conversion; it "
                    "changes numerics and produces a new ModelVersion that requires its "
                    "own EvaluationRun. Build the plan in packaging."
                    if stanza == "execution_accelerators"
                    else "A response cache keyed on input tensor bytes can return one "
                    "patient's mask for another patient's identical-looking patch."
                ),
            )
    if _DYNAMIC_BATCHING_RE.search(stripped):
        raise ConfigRejected(
            "MOS-OPS-072",
            "config.pbtxt enables dynamic_batching. The batching for a volumetric model "
            "is INSIDE the request, on its leading dimension; coalescing two studies "
            "adds queue delay for zero throughput and doubles peak workspace.",
        )
    match = _MAX_BATCH_RE.search(stripped)
    if match is None or match.group(1) != "0":
        raise ConfigRejected(
            "MOS-OPS-072",
            f"max_batch_size must be 0 with the batch dimension declared explicitly as "
            f"the leading -1; got {match.group(1) if match else '<absent>'}",
        )
    counts = [
        count
        for block in _INSTANCE_GROUP_RE.findall(stripped)
        for count in _COUNT_RE.findall(block)
    ]
    for count in counts:
        if int(count) != 1:
            raise ConfigRejected(
                "MOS-OPS-073",
                f"instance_group.count={count}: a second instance of a patch-batched 3D "
                "model buys no throughput and multiplies workspace_bytes by count. "
                "count > 1 requires a measured throughput gain on the ModelVersion.",
            )


def check_built_for(manifest: ModelManifest, server: dict[str, Any]) -> None:
    """MOS-OPS-070's load-time compatibility refusal, applied per backend.

    For `tensorrt_plan` the rule is literal and absolute: a serialized plan is bound to a
    GPU architecture and a TensorRT build, and loading one built for another is undefined
    behaviour dressed as a model.  For `onnxruntime_onnx` the rule as written has no
    referent -- an ONNX graph is portable and has no `cuda_compute_capability` -- so the
    check applied is the one that IS meaningful: the server must actually carry the
    backend.  Skipping the plan check is recorded in the raised message so that an
    operator reading a failure never has to guess which gates ran.
    """
    backend = manifest.backend
    if backend == "tensorrt_plan":
        built = manifest.built_for
        for key, observed_key in (
            ("cuda_compute_capability", "cuda_compute_capability"),
            ("tensorrt_version", "tensorrt_version"),
        ):
            declared = str(built.get(key, ""))
            observed = str(server.get(observed_key, ""))
            if not declared or not observed or declared != observed:
                raise ModelRepositoryError(
                    f"MOS-OPS-070: {manifest.model_id}@{manifest.model_version} declares "
                    f"built_for.{key}={declared!r}; this server reports {observed!r}. "
                    "A serialized plan is bound to its build; refusing to load."
                )
        return
    available = {str(b) for b in server.get("backends", ())}
    if available and backend not in available:
        raise ModelRepositoryError(
            f"MOS-OPS-070 (backend-availability form): {manifest.model_id}@"
            f"{manifest.model_version} needs backend {backend!r}; this server carries "
            f"{sorted(available)}. The tensorrt_plan compute-capability gate is "
            "inapplicable to a portable ONNX graph and was not run."
        )


def write_model_directory(
    root: Path,
    manifest: ModelManifest,
    artifact_bytes: bytes,
    *,
    warmup_bytes: bytes | None = None,
    warmup_filename: str = "golden_input.fp32.bin",
    warmup_batch: int | None = None,
    kind: str = "KIND_CPU",
) -> Path:
    """Materialise §13.10.1's directory for one ModelVersion. Returns the model directory.

        /models/<triton_model_name>/
          config.pbtxt
          warmup/<warmup_filename>
          1/<artifact filename>
          medicalos.json

    The write is NOT atomic against a concurrently-scanning Triton, which is why the
    caller (`tritond`) runs with `--model-control-mode=explicit` (MOS-OPS-074): Triton
    reads a model directory only when it is told to load it, so "written but not yet
    loaded" is a state the server never observes.  With polling mode this function would
    need a staging directory and a rename.
    """
    name = manifest.triton_model_name
    model_dir = root / name
    (model_dir / TRITON_VERSION_DIR).mkdir(parents=True, exist_ok=True)
    filename = manifest.artifact_filename or _DEFAULT_ARTIFACT_FILENAME[manifest.backend]
    (model_dir / TRITON_VERSION_DIR / filename).write_bytes(artifact_bytes)
    if warmup_bytes is not None:
        (model_dir / "warmup").mkdir(parents=True, exist_ok=True)
        (model_dir / "warmup" / warmup_filename).write_bytes(warmup_bytes)
    config = render_config_pbtxt(
        manifest,
        kind=kind,
        warmup_filename=warmup_filename if warmup_bytes is not None else None,
        warmup_batch=warmup_batch,
    )
    validate_config_pbtxt(config)  # generation and validation, not either/or
    (model_dir / "config.pbtxt").write_text(config, encoding="utf-8")
    (model_dir / "medicalos.json").write_text(manifest.to_json(), encoding="utf-8")
    return model_dir
