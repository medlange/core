# SPDX-License-Identifier: Apache-2.0
"""Build and publish the `medos.selftest-affine` model artifacts. The packaging step.

This is chapter 6's packaging step in miniature, and it is a TOOL rather than part of
`medos/medos/` for a reason: `MOS-OPS-070` puts `medicalos.json` production in packaging, and
`MOS-OPS-071` requires that the serving platform be unable to perform a backend
conversion.  A platform that can build a model can build a model at load time, which is
exactly what `MOS-OPS-071` forbids.  So the graph builder lives here, `onnx` is a tooling
dependency and not a runtime one, and `medos/medos/inference` only ever READS artifacts.

    python medos/tools/publish_model.py build    --out medos/deploy/models
    python medos/tools/publish_model.py publish  --src medos/deploy/models
    python medos/tools/publish_model.py verify   --src medos/deploy/models

`build` needs `onnx` (`pip install onnx`); `publish` and `verify` need only the pinned
runtime set.  The split means a deployment can publish artifacts it did not build, which
is the normal case once a real model vendor exists.

MOS-OPS-078 IS ASSERTED AT PACKAGING TIME, HERE
    "The packaging step MUST assert bit-identical output across every value in
    `allowed_patch_batch_sizes` on the golden fixture."  `build` does that against the
    numpy oracle before it writes anything; `tests/integration/test_triton.py` then
    repeats the assertion against the real server, because an assertion made only against
    the oracle is an assertion about the oracle.

Spec: MOS-OPS-015, MOS-OPS-070, MOS-OPS-071, MOS-OPS-078, MOS-SVC-058.
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
    artifact_key,
    manifest_key,
    warmup_key,
)
from medos.inference.selftest import (  # noqa: E402
    ALLOWED_PATCH_BATCH_SIZES,
    INPUT_NAME,
    OUTPUT_NAME,
    PATCH_SHAPE,
    SELFTEST_MODEL_ID,
    SELFTEST_VERSIONS,
    AffineParams,
    build_manifest,
    golden_input,
    reference_forward,
    selftest_model_ref,
)
from medos.objectstore import S3Client, S3Config  # noqa: E402

GOLDEN_BATCH = 2


def build_onnx(params: AffineParams) -> bytes:
    """`OUTPUT = Relu(INPUT * scale + bias)` as an ONNX graph, opset 13.

    `scale` and `bias` are graph INITIALIZERS -- real weights inside the serialized
    artifact, not attributes of the runtime -- so that the digest in `medicalos.json`
    covers the numbers that determine the output.  Two versions differing only in those
    initializers therefore differ in digest, in Triton model name, and in output.

    Written with `onnx.helper` rather than exported from a framework because the graph is
    three nodes; pulling in torch to emit them would add 200 MB to a tooling environment
    for no gain in fidelity.
    """
    from onnx import TensorProto, helper, numpy_helper

    scale = numpy_helper.from_array(np.array(params.scale, dtype=np.float32), "scale")
    bias = numpy_helper.from_array(np.array(params.bias, dtype=np.float32), "bias")
    dims = ["batch", *PATCH_SHAPE]
    graph = helper.make_graph(
        nodes=[
            helper.make_node("Mul", [INPUT_NAME, "scale"], ["scaled"]),
            helper.make_node("Add", ["scaled", "bias"], ["shifted"]),
            helper.make_node("Relu", ["shifted"], [OUTPUT_NAME]),
        ],
        name="medos_selftest_affine",
        inputs=[helper.make_tensor_value_info(INPUT_NAME, TensorProto.FLOAT, dims)],
        outputs=[helper.make_tensor_value_info(OUTPUT_NAME, TensorProto.FLOAT, dims)],
        initializer=[scale, bias],
    )
    model = helper.make_model(
        graph,
        producer_name="medicalos-packaging",
        opset_imports=[helper.make_opsetid("", 13)],
    )
    # IR version 8 pairs with opset 13 and is what Triton 24.08's ONNX Runtime accepts
    # without a warning. Pinned rather than defaulted: a newer `onnx` package raises the
    # default IR version, and a model that loads on the packaging host but not on the
    # server is the failure this pin removes.
    model.ir_version = 8
    return model.SerializeToString()  # type: ignore[no-any-return]


def assert_batch_independence(params: AffineParams) -> None:
    """MOS-OPS-078, against the oracle. The same assertion runs against Triton in tests."""
    single = reference_forward(golden_input(1), params)
    for batch in ALLOWED_PATCH_BATCH_SIZES:
        stacked = np.concatenate([golden_input(1)] * batch, axis=0)
        out = reference_forward(stacked, params)
        for index in range(batch):
            if not np.array_equal(out[index], single[0]):
                raise SystemExit(
                    f"MOS-OPS-078 violated at patch_batch_size={batch}: output depends "
                    "on the batch dimension, so patch_batch_size is not a capacity knob "
                    "and every value would need its own EvaluationRun"
                )


def cmd_build(out_root: Path) -> int:
    for version, params in SELFTEST_VERSIONS.items():
        assert_batch_independence(params)
        blob = build_onnx(params)
        manifest = build_manifest(version, blob, golden_batch=GOLDEN_BATCH)
        target = out_root / SELFTEST_MODEL_ID / version
        (target / "warmup").mkdir(parents=True, exist_ok=True)
        (target / manifest.artifact_filename).write_bytes(blob)
        (target / "medicalos.json").write_text(manifest.to_json(), encoding="utf-8")
        golden = golden_input(GOLDEN_BATCH)
        (target / "warmup" / str(manifest.selftest["input_filename"])).write_bytes(
            np.ascontiguousarray(golden, dtype=np.float32).tobytes()
        )
        print(
            json.dumps(
                {
                    "built": f"{SELFTEST_MODEL_ID}@{version}",
                    "artifact_bytes": len(blob),
                    "artifact_digest": manifest.artifact_digest,
                    "selftest_output_sha256": manifest.selftest["output_sha256"],
                }
            )
        )
    return 0


def _iter_local(src_root: Path):  # type: ignore[no-untyped-def]
    for version in sorted(SELFTEST_VERSIONS):
        directory = src_root / SELFTEST_MODEL_ID / version
        manifest = ModelManifest.from_json(
            (directory / "medicalos.json").read_text(encoding="utf-8")
        )
        yield version, directory, manifest


def cmd_publish(src_root: Path) -> int:
    """Upload artifact + manifest + golden fixture. Verifies the digest before uploading.

    Publishing an artifact whose bytes do not match its own manifest would put a
    permanently unloadable model in the store -- `tritond.stage` refuses on digest
    mismatch (MOS-OPS-070) -- so the check happens on this side of the network too.
    """
    client = S3Client(S3Config.from_env())
    created = client.ensure_bucket()
    published: list[str] = []
    for version, directory, manifest in _iter_local(src_root):
        ref = selftest_model_ref(version)
        blob = (directory / manifest.artifact_filename).read_bytes()
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        if digest != manifest.artifact_digest:
            raise SystemExit(
                f"{ref}: local artifact digest {digest} != manifest "
                f"{manifest.artifact_digest}; refusing to publish"
            )
        client.put_object(artifact_key(ref, manifest.artifact_filename), blob)
        client.put_object(
            manifest_key(ref), manifest.to_json().encode(), content_type="application/json"
        )
        golden_name = str(manifest.selftest["input_filename"])
        client.put_object(
            warmup_key(ref, golden_name),
            (directory / "warmup" / golden_name).read_bytes(),
        )
        published.append(str(ref))
    print(
        json.dumps(
            {
                "bucket": client.config.bucket,
                "bucket_created": created,
                "published": published,
            }
        )
    )
    return 0


def cmd_verify(src_root: Path) -> int:
    """Confirm what is in the object store matches what is on disk, byte for byte."""
    client = S3Client(S3Config.from_env())
    report: list[dict[str, object]] = []
    for version, directory, manifest in _iter_local(src_root):
        ref = selftest_model_ref(version)
        remote = client.get_object(artifact_key(ref, manifest.artifact_filename))
        local = (directory / manifest.artifact_filename).read_bytes()
        report.append(
            {
                "model": str(ref),
                "artifact_matches": remote == local,
                "digest_matches": "sha256:" + hashlib.sha256(remote).hexdigest()
                == manifest.artifact_digest,
                "manifest_present": client.exists(manifest_key(ref)),
            }
        )
    print(json.dumps(report, indent=2))
    return 0 if all(r["artifact_matches"] and r["digest_matches"] for r in report) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publish_model")
    parser.add_argument("command", choices=["build", "publish", "verify"])
    parser.add_argument("--out", type=Path, default=Path("medos/deploy/models"))
    parser.add_argument("--src", type=Path, default=Path("medos/deploy/models"))
    args = parser.parse_args(argv)
    if args.command == "build":
        return cmd_build(args.out)
    if args.command == "publish":
        return cmd_publish(args.src)
    return cmd_verify(args.src)


if __name__ == "__main__":
    raise SystemExit(main())
