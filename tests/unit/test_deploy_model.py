# SPDX-License-Identifier: Apache-2.0
"""`medos.tools.deploy_model` — the G-C2 command, card in, model repository out.

The normative path these tests pin: a trainer run (modelcard.json + bundle + ONNX next
to the card) becomes a §13.10.1 Triton model directory through
`medos.inference.repository.write_model_directory`, with the refusal gates doing their
job BEFORE anything is written: no ONNX named, digest mismatch, bundle/card fixture
disagreement. The gates exist because a model repository that half-agrees with its own
manifest is a server that loads a model and serves the wrong tensor with a straight
face.

No torch, no server: the ONNX is a three-node helper graph (same construction as
`medos/tools/publish_model.py`), the card is built from the SDK's self-test spec, and
"the server" is `ModelManifest.from_json` reading back what was written.

Spec: MOS-OPS-070/071/072/075/078; G-C2.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from medos.inference.repository import (
    ModelManifest,
    triton_model_name,
    validate_config_pbtxt,
)
from medos.sdk.fixtures import selftest_spec_document
from medos.sdk.modelcard import document_for
from tools.deploy_model import DeployRefused, deploy  # noqa: E402

pytest.importorskip("onnx")
from onnx import TensorProto, helper  # noqa: E402

PATCH = (32, 48, 48)


def _onnx_blob(input_name: str = "input", output_name: str = "output") -> bytes:
    dims = ["batch", 1, *PATCH]
    graph = helper.make_graph(
        nodes=[helper.make_node("Relu", [input_name], [output_name])],
        name="deploy_test",
        inputs=[helper.make_tensor_value_info(input_name, TensorProto.FLOAT, dims)],
        outputs=[helper.make_tensor_value_info(output_name, TensorProto.FLOAT, dims)],
    )
    model = helper.make_model(
        graph,
        producer_name="deploy-model-test",
        opset_imports=[helper.make_opsetid("", 13)],
    )
    model.ir_version = 8
    return model.SerializeToString()  # type: ignore[no-return-value]


def _card_dir(tmp_path: Path, *, with_onnx: bool = True, digest_ok: bool = True) -> Path:
    """A trainer-run-shaped directory: bundle with the golden fixture, ONNX next to the
    card, card naming both."""
    import hashlib

    root = tmp_path / "run"
    bundle = root / "bundle"
    (bundle / "docs").mkdir(parents=True)
    fixture = np.zeros((1, 1, *PATCH), dtype=np.float32)
    (bundle / "docs" / "golden_fixture_tensor.f32").write_bytes(fixture.tobytes())

    spec = selftest_spec_document()
    blob = _onnx_blob()
    digest = "sha256:" + hashlib.sha256(blob).hexdigest()
    weights: dict[str, object] = {
        "path": "bundle",
        "weights_file": "models/model.ts",
        "format": "torchscript",
        "digest": "sha256:" + "0" * 64,
    }
    if with_onnx:
        (root / "model.onnx").write_bytes(blob)
        weights["onnx_file"] = "model.onnx"
        weights["onnx_digest"] = digest if digest_ok else "sha256:" + "f" * 64
    document = document_for(
        model_id=spec["model_id"],
        model_version=spec["model_version"],
        spec_document=spec,
        weights=weights,
        frameworks={"torch": "2.7.1", "numpy": "1.26.4"},
        outputs=({"kind": "segmentation", "value": 1, "name": "lung"},),
        stamp={"producer": "test"},
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "modelcard.json").write_text(json.dumps(document), encoding="utf-8")
    return root


def test_a_card_deploys_to_a_triton_model_directory(tmp_path: Path) -> None:
    card_dir = _card_dir(tmp_path)
    repo = tmp_path / "repo"
    summary = deploy(card_dir, repo)

    name = triton_model_name("medos.selftest-affine", "1.0.0")
    assert summary["triton_model_name"] == name
    model_dir = repo / name
    # §13.10.1's layout, nothing missing: config, manifest, version dir, warmup.
    config = (model_dir / "config.pbtxt").read_text(encoding="utf-8")
    validate_config_pbtxt(config)
    assert (model_dir / "1" / "model.onnx").read_bytes() == _onnx_blob()
    assert (model_dir / "warmup" / "golden_patch_batch1.fp32.bin").is_file()

    manifest = ModelManifest.from_json((model_dir / "medicalos.json").read_text("utf-8"))
    assert manifest.input_dims == (-1, 1, *PATCH)
    assert manifest.allowed_patch_batch_sizes == (1,), (
        "MOS-OPS-078's bit-identity assertion belongs to ConversionRun; until it "
        "exists, batch 1 is the only honestly declared size"
    )
    assert manifest.selftest["output_sha256"] is None, (
        "the served-output hash is ConversionRun's evidence; a value invented at "
        "deploy time would look measured and be neither"
    )
    assert manifest.backend == "onnxruntime_onnx"


def test_the_config_names_the_graphs_real_tensors(tmp_path: Path) -> None:
    """Input/output names are read from the exported graph, not assumed: the config
    Triton serves must name real graph tensors or every infer answers 400."""
    import hashlib

    root = tmp_path / "run"
    bundle = root / "bundle"
    (bundle / "docs").mkdir(parents=True)
    (bundle / "docs" / "golden_fixture_tensor.f32").write_bytes(
        np.zeros((1, 1, *PATCH), dtype=np.float32).tobytes()
    )
    spec = selftest_spec_document()
    blob = _onnx_blob(input_name="patch_in", output_name="mask_out")
    document = document_for(
        model_id=spec["model_id"],
        model_version=spec["model_version"],
        spec_document=spec,
        weights={
            "path": "bundle",
            "weights_file": "models/model.ts",
            "format": "torchscript",
            "digest": "sha256:" + "0" * 64,
            "onnx_file": "model.onnx",
            "onnx_digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
        },
        frameworks={"torch": "2.7.1", "numpy": "1.26.4"},
        outputs=({"kind": "segmentation", "value": 1, "name": "lung"},),
        stamp={"producer": "test"},
    )
    (root / "model.onnx").write_bytes(blob)
    (root / "modelcard.json").write_text(json.dumps(document), encoding="utf-8")

    deploy(root, tmp_path / "repo")
    manifest = ModelManifest.from_json(
        (tmp_path / "repo" / triton_model_name("medos.selftest-affine", "1.0.0")
         / "medicalos.json").read_text("utf-8")
    )
    assert manifest.input_name == "patch_in"
    assert manifest.output_name == "mask_out"


def test_a_torchscript_only_card_is_refused(tmp_path: Path) -> None:
    with pytest.raises(DeployRefused, match="onnx_file"):
        deploy(_card_dir(tmp_path, with_onnx=False), tmp_path / "repo")


def test_a_digest_mismatch_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    with pytest.raises(DeployRefused, match="digest"):
        deploy(_card_dir(tmp_path, digest_ok=False), tmp_path / "repo")
    assert not (tmp_path / "repo").exists()


def test_a_bundle_card_fixture_disagreement_is_refused(tmp_path: Path) -> None:
    card_dir = _card_dir(tmp_path)
    (card_dir / "bundle" / "docs" / "golden_fixture_tensor.f32").write_bytes(
        np.zeros(7, dtype=np.float32).tobytes()
    )
    with pytest.raises(DeployRefused, match="disagree"):
        deploy(card_dir, tmp_path / "repo")
