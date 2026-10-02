# SPDX-License-Identifier: Apache-2.0
"""Drivers for the other servers that speak KServe v2: ONNX Runtime and OpenVINO.

WHAT THESE TWO FILES-WORTH OF CODE ARE FOR
--------------------------------------------
Before this module, `InferenceBackend` had ONE driver. A port with one driver has only ever
been proven against one implementation, and the interface it ends up with is the interface
that implementation happened to need -- which is exactly the objection `MOS-OPS-008` raises
when it says the seam "MUST NOT be treated as resolved by any chapter".

These are the cheapest possible second and third drivers, and the fact that they ARE cheap
is the evidence the port is real: each contributes a `driver_name`, an env prefix and a
naming rule, and inherits the whole protocol from `kserve_v2.KServeV2Backend`. If a second
driver had needed to override `infer`, the port would have been Triton-shaped and this file
would have said so instead.

WHAT THIS IS NOT
-----------------
These are not validated deployments. Shipping a driver means the platform CAN talk to that
server; it does not mean any model has been evaluated on it. `MOS-OPEN-031` is unchanged
and binding: **a backend conversion mints a new `ModelVersion` and requires its own
`EvaluationRun`.** Serving an artifact built for Triton through OpenVINO without that is
serving a model no evidence covers, which is the failure the whole evidence plane exists to
prevent. The catalogue lets an operator select a backend; the registry is what must refuse
a model that has not been evaluated on it, and that check is owed.

WHY NOT TORCHSERVE, TENSORFLOW SERVING OR vLLM HERE
-----------------------------------------------------
They do not speak KServe v2 over HTTP with the binary tensor extension:
  * TorchServe has its own REST shape (`/predictions/{model}`) and its own management API;
  * TensorFlow Serving speaks its own `/v1/models/{name}:predict` JSON, and its gRPC
    surface is TensorFlow's own;
  * vLLM serves an OpenAI-compatible chat/completions API and is a text runtime -- it has
    no place on a path that carries a float32 image tensor, and pretending otherwise would
    be listing it for the sake of the list.
Each is a real driver's worth of work rather than a naming rule, and each belongs in its
own module with its own tests when a deployment actually needs it. Adding a stub here that
raises `NotImplementedError` would make the catalogue advertise something the image cannot
do, which is the `enforced_in_code: false` pattern in a new costume.

Spec: OQ-10 / MOS-OPEN-031, MOS-OPS-008, MOS-OPS-071, MOS-REL-108, MOS-SVC-058.
"""

from __future__ import annotations

from medos.inference.kserve_v2 import KServeConfig, KServeV2Backend

__all__ = ["OnnxRuntimeBackend", "OpenVinoBackend"]


class OnnxRuntimeBackend(KServeV2Backend):
    """ONNX Runtime Server.

    Its model names are plain -- the server serves whatever directory name it was given --
    so the pin becomes `{model_id}` and the VERSION travels in the URL the way KServe v2
    intends (`/v2/models/{name}/versions/{n}`) rather than being folded into the name the
    way Triton requires. That difference is precisely why `_resolve` is the subclass's and
    not the base class's.

    A NOTE ON THE VERSION, because it is a real divergence rather than a detail. This
    driver does NOT append `/versions/{n}`: `KServeV2Backend` builds its paths as
    `/v2/models/{name}/...`, so a deployment serving two versions of one model through ONNX
    Runtime must give them distinct directory names. Recorded here rather than papered
    over, because the alternative -- silently serving whichever version the server happens
    to have marked latest -- is the "no default version and no fallback" rule that
    `medos/medos/inference/triton.py` already states for the same reason.
    """

    driver_name = "onnxruntime"
    env_prefix = "MEDOS_ONNXRUNTIME"

    def _resolve(self, model_id: str, model_version: str) -> str:
        return f"{model_id}_{model_version.replace('.', '_')}"


class OpenVinoBackend(KServeV2Backend):
    """OpenVINO Model Server.

    OVMS serves KServe v2 over HTTP at the same paths and honours the binary tensor
    extension. Its model names come from its own config file, so the same
    name-carries-the-version convention applies, and for the same reason: an unversioned
    name would let a config change swap the served weights under a pinned `ModelVersion`.

    OVMS is the CPU-and-Intel-accelerator answer, which is the deployment OQ-10's branch
    table names when it says a mandated Triton means "a hospital with a different
    accelerator or a CPU-only site cannot be served".
    """

    driver_name = "openvino"
    env_prefix = "MEDOS_OPENVINO"

    def _resolve(self, model_id: str, model_version: str) -> str:
        return f"{model_id}_{model_version.replace('.', '_')}"


def build_onnxruntime() -> OnnxRuntimeBackend | None:
    """Catalogue factory. None when this deployment has not configured the server."""
    import os

    if not os.environ.get("MEDOS_ONNXRUNTIME_URL"):
        return None
    return OnnxRuntimeBackend(KServeConfig.from_env("MEDOS_ONNXRUNTIME"))


def build_openvino() -> OpenVinoBackend | None:
    import os

    if not os.environ.get("MEDOS_OPENVINO_URL"):
        return None
    return OpenVinoBackend(KServeConfig.from_env("MEDOS_OPENVINO"))
