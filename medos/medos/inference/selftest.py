# SPDX-License-Identifier: Apache-2.0
"""The one real served model: `medos.selftest-affine`, and the MOS-OPS-015 self-test.

WHY A SELF-TEST MODEL AND NOT A CLINICAL ONE
    All three capabilities in this codebase are deterministic algorithms -- an HU
    threshold, a percentage of voxels below -950 HU, and a declared placeholder.  None of
    them needs a model server, and inventing a "learned" clinical capability to justify
    one would be exactly the dishonesty `medos.capabilities.base.CapabilityMetadata`
    exists to prevent: a `method_class` of `learned_model` is a claim about provenance and
    validation, and there is no `EvaluationRun`, no cohort and no `AcceptanceCriteria`
    behind it (chapter 7; `docs/spec/15-delivery.md` §15.2.5 schedules all of that at
    0.2.0).

    So the model that is really served, really pinned, really loaded and really inferred
    against is a PLATFORM model, and the specification already requires one:

        MOS-OPS-075 -- "`model_warmup` ... is a latency measure only; it does not replace
        the MOS-IMG preprocessing self-test, which compares an output tensor hash against
        the training-time value and is run by the serving component at startup
        (MOS-OPS-015)."

    `medos.selftest-affine` is that fixture.  It exercises every part of the native
    inference path that a clinical model would -- object-store artifact, digest
    verification, `config.pbtxt` gates, `(id, version)` resolution, load, warm-up,
    readiness, binary-tensor inference, eviction -- and it asserts something true rather
    than something invented.

THE MATHS, AND WHY IT IS DELIBERATELY BORING
        OUTPUT = Relu(INPUT * scale + bias)

    `scale` and `bias` are POWERS OF TWO and the golden input is integral, which makes
    every intermediate exactly representable in binary32.  That is not an aesthetic
    choice: `MOS-OPS-015` compares a SHA-256 of the output tensor, so the comparison is
    bit-exact, and a graph containing (say) a sigmoid would differ in the last ULP between
    ONNX Runtime and the numpy oracle -- and between one ONNX Runtime build and the next.
    A self-test that fails on a 1-ULP difference would be switched off within a week, at
    which point the platform has a self-test that is never run.  Choosing arithmetic that
    is exact on both sides makes the hash comparison a real gate.

    It also makes `MOS-OPS-078`'s claim checkable: the output for a given voxel is
    independent of the batch dimension, so every value in `allowed_patch_batch_sizes`
    produces bit-identical output, which is what lets `patch_batch_size` be a capacity
    knob rather than a revalidation event.

TWO VERSIONS, ON PURPOSE
    `1.0.0` and `1.1.0` differ only in `scale`/`bias`, so the same input produces
    demonstrably different output.  Both are pinned and both are resident simultaneously
    under the distinct Triton model names `MOS-OPS-068` derives.  That turns
    §15.2.9's "loading by id and version means the registry contract is genuinely
    exercised ... instead of being asserted" into something a test can fail.

Spec: MOS-OPS-015, MOS-OPS-068, MOS-OPS-069, MOS-OPS-070, MOS-OPS-075, MOS-OPS-078,
MOS-SVC-058, MOS-SAFE-012 (this is NOT a clinical capability and says so).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

from medos.inference.backend import ModelRef
from medos.inference.repository import ModelManifest

__all__ = [
    "SELFTEST_MODEL_ID",
    "SELFTEST_VERSIONS",
    "AffineParams",
    "reference_forward",
    "golden_input",
    "build_manifest",
    "INPUT_NAME",
    "OUTPUT_NAME",
    "PATCH_SHAPE",
    "ALLOWED_PATCH_BATCH_SIZES",
]

SELFTEST_MODEL_ID = "medos.selftest-affine"

INPUT_NAME = "INPUT__0"
OUTPUT_NAME = "OUTPUT__0"

# (channels, k, j, i).  16³ rather than §13.10.2's 128³ because this runs on a laptop CPU
# in a test: the shape is a fixture parameter, and every rule this model exercises
# (MOS-OPS-072's explicit leading -1, MOS-OPS-078's batch independence) is shape-agnostic.
PATCH_SHAPE: tuple[int, int, int, int] = (1, 16, 16, 16)

# MOS-OPS-078: the packaging step MUST assert bit-identical output across every value
# here. `tests/integration/test_triton.py` performs exactly that assertion against the
# real server rather than taking the manifest's word for it.
ALLOWED_PATCH_BATCH_SIZES: tuple[int, ...] = (1, 2, 4)


@dataclass(frozen=True)
class AffineParams:
    """One version's weights. Powers of two -- see the module docstring."""

    scale: float
    bias: float


# The two published versions. Adding a third is a new object-store prefix and a new pin;
# it is never an edit to an existing one (MOS-SVC-058: weights are not mutated at runtime).
SELFTEST_VERSIONS: dict[str, AffineParams] = {
    "1.0.0": AffineParams(scale=0.0078125, bias=0.25),    # 2^-7, 2^-2
    "1.1.0": AffineParams(scale=0.015625, bias=-0.5),     # 2^-6, -2^-1
}


def reference_forward(tensor: np.ndarray, params: AffineParams) -> np.ndarray:
    """The numpy oracle. `medos.inference.inprocess` serves this; Triton must match it.

    Written out here rather than inside the fixture backend because BOTH the publisher
    (which records `output_sha256` in `medicalos.json`) and the cross-check test need it,
    and two copies of an oracle is two oracles.
    """
    array = np.asarray(tensor, dtype=np.float32)
    return np.maximum(
        array * np.float32(params.scale) + np.float32(params.bias), np.float32(0.0)
    ).astype(np.float32)


def golden_input(batch: int = 2, seed: int = 20260914) -> np.ndarray:
    """The golden fixture of MOS-OPS-075, generated deterministically.

    Integral values in [-2048, 2047]: multiplied by a power of two they stay exactly
    representable, which is what makes the `output_sha256` comparison bit-exact on any
    conforming binary32 implementation.  The seed is fixed and recorded in the manifest,
    so the fixture can be regenerated from the manifest alone.
    """
    rng = np.random.default_rng(seed)
    return rng.integers(-2048, 2048, size=(batch, *PATCH_SHAPE)).astype(np.float32)


def build_manifest(
    version: str,
    artifact_bytes: bytes,
    *,
    artifact_filename: str = "model.onnx",
    backend: str = "onnxruntime_onnx",
    golden_batch: int = 2,
    seed: int = 20260914,
) -> ModelManifest:
    """Produce `medicalos.json` for one version (MOS-OPS-070), including the self-test.

    `weights_bytes` is the real artifact size and `workspace_bytes_by_patch_batch` is the
    real activation arithmetic for this graph (input + output tensors at that batch), not
    a guess: §13.10.3's budget is only meaningful if the numbers in it were measured.
    `built_for` records what is actually true of a portable ONNX graph -- the opset and
    the required backend -- rather than the `cuda_compute_capability` of §13.10.1's
    TensorRT example, which has no referent here (see `repository.check_built_for`).
    """
    params = SELFTEST_VERSIONS[version]
    reference = reference_forward(golden_input(golden_batch, seed), params)
    voxels = int(np.prod(PATCH_SHAPE))
    workspace = {
        # input tensor + output tensor at that batch, fp32. Both are live simultaneously.
        str(batch): batch * voxels * 4 * 2
        for batch in ALLOWED_PATCH_BATCH_SIZES
    }
    return ModelManifest(
        model_id=SELFTEST_MODEL_ID,
        model_version=version,
        artifact_digest="sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
        artifact_filename=artifact_filename,
        preprocessing_version="1.0.0",
        backend=backend,
        built_for={"onnx_opset": 13, "backend": backend, "precision": "fp32"},
        input_name=INPUT_NAME,
        output_name=OUTPUT_NAME,
        # MOS-OPS-072: leading -1 is the batch dimension, declared explicitly.
        input_dims=(-1, *PATCH_SHAPE),
        output_dims=(-1, *PATCH_SHAPE),
        allowed_patch_batch_sizes=ALLOWED_PATCH_BATCH_SIZES,
        weights_bytes=len(artifact_bytes),
        workspace_bytes_by_patch_batch=workspace,
        selftest={
            "input_filename": f"golden_patch_batch{golden_batch}.fp32.bin",
            "input_shape": [golden_batch, *PATCH_SHAPE],
            "input_seed": seed,
            "batch": golden_batch,
            # MOS-OPS-015: the training-time output hash. Bit-exact by construction.
            "output_sha256": "sha256:"
            + hashlib.sha256(np.ascontiguousarray(reference).tobytes()).hexdigest(),
            "note": (
                "Platform self-test model. NOT a clinical capability: no EvaluationRun, "
                "no cohort, no AcceptanceCriteria. See medos/medos/inference/selftest.py."
            ),
        },
    )


def fixture_models() -> dict[tuple[str, str], object]:
    """The `(model_id, version) -> callable` map for `InProcessCpuBackend`. Tests only."""
    return {
        (SELFTEST_MODEL_ID, version): (
            lambda tensor, params=params: reference_forward(tensor, params)
        )
        for version, params in SELFTEST_VERSIONS.items()
    }


def selftest_model_ref(version: str) -> ModelRef:
    return ModelRef(model_id=SELFTEST_MODEL_ID, model_version=version)
