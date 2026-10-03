# SPDX-License-Identifier: Apache-2.0
"""Deploy a trained model card to a Triton model repository. The G-C2 command.

    python medos/tools/deploy_model.py --modelcard <run-dir> --repo <model-repo>

`--modelcard` names the directory holding `modelcard.json` (what the trainer writes next
to the bundle, format `medlange.modelcard/1`); `--repo` names a Triton model repository
root, which the command creates or updates in place. One model directory per
(model_id, version), in §13.10.1's layout, written by `medos.inference.repository` so
the gates (MOS-OPS-071/072/073/074) are satisfied by construction rather than reviewed
after the fact.

WHY THIS TOOL EXISTS. The platform's serving stack stages from an object store
(`medos.inference.tritond` reads `medicalos.json` + the artifact from S3), which is the
production path. The gap G-C2 names is the step BEFORE the store: a trained run on disk
— card, bundle, no `medicalos.json` — had no normative way to become servable. This is
that step: card in, model repository out. From here the standard paths apply unchanged:
mount the repository at Triton (`--profile inference` in compose, or any server with the
model repo), or publish the same layout to the store with the platform's own S3 tooling.

WHAT IS REFUSED, and why:

  * a card with no `weights.onnx_file` — the normative serving artifact is ONNX
    (MOS-OPS-071 forbids server-side conversion; the TorchScript bundle is not served).
    Export it with the trainer (`onnx_bytes`) or convert explicitly;
  * a digest mismatch between the card and the bytes on disk — a card that names bytes
    that are not there is the exact defect `ModelCard.load` exists to catch, restated
    for the serving artifact;
  * a golden fixture whose element count does not match the card's input shape — the
    warmup input (MOS-OPS-075) is the training-time preprocessing fixture, and a
    mismatch means the bundle and the card disagree, which must fail here, loudly,
    rather than warm the server with the wrong tensor.

WHAT IS MEASURED, NOT GUESSED: `weights_bytes` is the artifact size;
`workspace_bytes_by_patch_batch` is the input+output fp32 arithmetic for the declared
patch; `built_for.onnx_opset` is read from the exported graph itself when the `onnx`
tooling package is present. `allowed_patch_batch_sizes` is conservative — `(1,)` —
because the bit-identity assertion of MOS-OPS-078 belongs to ConversionRun, and a
capacity knob asserted by nobody is a lie in a budget document.

Spec: MOS-OPS-068/069/070/072/075/078, MOS-TRAIN-159–169 (ConversionRun owns the
equivalence evidence this tool does not produce).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medos.inference.repository import (  # noqa: E402
    ModelManifest,
    write_model_directory,
)
from medos.sdk.modelcard import ModelCard  # noqa: E402

ONNX_INPUT_NAME = "input"
ONNX_OUTPUT_NAME = "output"


class DeployRefused(RuntimeError):
    """The card, the bytes, or the bundle is not something this tool will deploy."""


def _onnx_facts(blob: bytes) -> tuple[str, str, int]:
    """(input_name, output_name, opset) from the graph, or the trainer's export
    convention when the `onnx` tooling package is absent. Names are read from the
    graph rather than assumed because the config.pbtxt Triton serves must name the
    graph's real tensors; a convention drift here is a model that loads and answers
    400 on every infer."""
    try:
        import onnx
    except ImportError:
        return ONNX_INPUT_NAME, ONNX_OUTPUT_NAME, 17
    model = onnx.load_model_from_string(blob)
    graph = model.graph
    if len(graph.input) != 1 or len(graph.output) != 1:
        raise DeployRefused(
            f"the ONNX graph names {len(graph.input)} inputs and {len(graph.output)} "
            "outputs; the served contract is exactly one of each"
        )
    opset = max((op.version for op in model.opset_import), default=17)
    return str(graph.input[0].name), str(graph.output[0].name), int(opset)


def _warmup_bytes(card: ModelCard, input_shape: tuple[int, ...]) -> tuple[bytes, str]:
    """The training-time preprocessing fixture as the warmup input (MOS-OPS-075).

    Returns (bytes, filename). The fixture is part of the verified bundle; its element
    count must match the card's declared input shape (without the batch dim), which is
    the check that catches a bundle/card disagreement before the server warms with a
    wrong tensor.
    """
    assert card.directory is not None
    fixture = card.directory / str(card.weights["path"]) / "docs" / "golden_fixture_tensor.f32"
    if not fixture.is_file():
        raise DeployRefused(
            f"{fixture} does not exist; the warmup input is the training-time golden "
            "fixture and a bundle without one cannot be served"
        )
    blob = fixture.read_bytes()
    elements = int(np.prod(input_shape))
    if blob[:2] == b"\x1f\x8b":
        raise DeployRefused(f"{fixture} is gzip, not a raw fp32 tensor")
    count = len(blob) // 4
    if count != elements:
        raise DeployRefused(
            f"{fixture} holds {count} fp32 values and the card's input shape "
            f"{input_shape} needs {elements}; the bundle and the card disagree"
        )
    return blob, "golden_patch_batch1.fp32.bin"


def build_manifest(card: ModelCard, onnx_blob: bytes) -> ModelManifest:
    """`medicalos.json` (MOS-OPS-070) derived from the card — the same manifest shape
    `medos.inference.selftest.build_manifest` produces for the platform self-test, with
    the self-test output hash left absent rather than invented: that hash is
    ConversionRun's evidence, and a number written to look measured is worse than no
    number."""
    patch = tuple(int(d) for d in card.spec.patch.size_voxels)
    channels = int(card.spec.io.channels)
    label_set = card.spec.io.label_set or ()
    if label_set:
        classes = max(int(entry["value"]) for entry in label_set) + 1
    else:
        classes = len(card.outputs)
    input_name, output_name, opset = _onnx_facts(onnx_blob)
    voxels = int(np.prod(patch))
    batch_sizes = (1,)
    return ModelManifest(
        model_id=card.model_id,
        model_version=card.model_version,
        artifact_digest="sha256:" + hashlib.sha256(onnx_blob).hexdigest(),
        artifact_filename="model.onnx",
        preprocessing_version=f"{card.spec.id}@{card.spec.version}",
        backend="onnxruntime_onnx",
        built_for={
            "onnx_opset": opset,
            "backend": "onnxruntime_onnx",
            "precision": "fp32",
        },
        input_name=input_name,
        output_name=output_name,
        input_dims=(-1, channels, *patch),
        output_dims=(-1, classes, *patch),
        allowed_patch_batch_sizes=batch_sizes,
        weights_bytes=len(onnx_blob),
        workspace_bytes_by_patch_batch={
            str(batch): batch * voxels * 4 * (channels + classes)
            for batch in batch_sizes
        },
        selftest={
            "input_filename": "golden_patch_batch1.fp32.bin",
            "input_shape": [1, channels, *patch],
            "batch": 1,
            "output_sha256": None,
            "note": (
                "Warmup input is the training-time preprocessing fixture. The served "
                "output hash is ConversionRun's evidence (MOS-TRAIN-159-169) and is "
                "absent until an equivalence run records it."
            ),
        },
    )


def deploy(modelcard_dir: Path, repo: Path) -> dict[str, object]:
    card = ModelCard.load(modelcard_dir)
    assert card.directory is not None
    onnx_name = card.weights.get("onnx_file")
    if not onnx_name:
        raise DeployRefused(
            "the card names no weights.onnx_file; the normative serving artifact is "
            "ONNX (MOS-OPS-071 forbids server-side conversion). Export it with the "
            "trainer's packaging.onnx_bytes, or convert explicitly and record "
            "onnx_file/onnx_digest in the card's weights mapping"
        )
    onnx_path = card.directory / str(onnx_name)
    if not onnx_path.is_file():
        raise DeployRefused(
            f"{onnx_path} does not exist; the card names bytes that are not there"
        )
    onnx_blob = onnx_path.read_bytes()
    declared = card.weights.get("onnx_digest")
    actual = "sha256:" + hashlib.sha256(onnx_blob).hexdigest()
    if declared and str(declared) != actual:
        raise DeployRefused(
            f"card digest {declared} != actual {actual}; the ONNX next to the card is "
            "not the artifact the card promises"
        )

    manifest = build_manifest(card, onnx_blob)
    warmup, warmup_name = _warmup_bytes(card, manifest.input_dims[1:])
    model_dir = write_model_directory(
        repo,
        manifest,
        onnx_blob,
        warmup_bytes=warmup,
        warmup_filename=warmup_name,
        warmup_batch=1,
    )
    return {
        "model": f"{manifest.model_id}@{manifest.model_version}",
        "triton_model_name": manifest.triton_model_name,
        "repository": str(model_dir),
        "artifact_digest": manifest.artifact_digest,
        "config_pbtxt_gates": "validated-by-construction",
        "next": (
            "serve the repository with Triton: "
            "`docker compose --profile inference up -d` mounts <repo> at /models, or "
            "point any Triton at it directly"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_model")
    parser.add_argument(
        "--modelcard",
        type=Path,
        required=True,
        help="directory holding modelcard.json (a trainer run root)",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="Triton model repository root to create or update",
    )
    args = parser.parse_args(argv)
    try:
        summary = deploy(args.modelcard, args.repo)
    except DeployRefused as exc:
        print(json.dumps({"refused": str(exc)}))
        return 2
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
