# SPDX-License-Identifier: Apache-2.0
"""KServe v2 over HTTP: the protocol, once, for every server that speaks it.

WHY THIS FILE EXISTS
---------------------
`OQ-10` is decided (see `docs/adr/BUILD_VS_ADOPT.md`): the inference runtime is replaceable
and Triton is one adapter. `medos/medos/inference/catalogue.py` made SELECTION real. This file
makes REPLACEABILITY real, because a port with one driver is a port that has only ever been
proven against one implementation -- and the interface such a port ends up with is the
interface that implementation happened to need.

`medos/medos/inference/triton.py` already contained a complete, careful KServe v2 client: the
binary-tensor-extension header assembly, the two-part request body, the offset walk over
the response outputs. **None of that is Triton's.** KServe v2 is the open predict protocol,
and NVIDIA Triton, ONNX Runtime Server and OpenVINO Model Server all serve it at the same
paths (`/v2/models/{name}`, `/v2/models/{name}/ready`, `/v2/models/{name}/infer`) with the
same binary extension. So the protocol lives here and a driver contributes the one thing
that genuinely differs: **how a `(model_id, model_version)` pin becomes a server-side model
name.**

WHAT A SUBCLASS OWES, AND WHAT IT MUST NOT TOUCH
--------------------------------------------------
    driver_name   recorded in the provenance of everything the model produces, because
                  `MOS-OPS-071` makes the runtime a numerics-relevant fact. Two drivers
                  serving one artifact are not interchangeable in the record even when they
                  agree to the bit.
    _resolve      the naming rule. Triton's is `MOS-OPS-068`'s `.`-to-`_` substitution;
                  another server will have its own.

Everything else -- dtype handling, the fp32 contract, shape verification through
`require_shape`, the error taxonomy -- is fixed here ON PURPOSE. If a subclass could change
how a tensor is encoded, then "the same model on two backends" would stop being a statement
about the model, and `MOS-OPEN-031`'s rule that a backend conversion mints a new
`ModelVersion` with its own `EvaluationRun` would have nothing to attach to.

THE ERROR REASON CARRIES THE DRIVER
-------------------------------------
Reason codes are built as `f"{driver_name}_unreachable"`, so Triton keeps the exact
`triton_unreachable` / `triton_model_unknown` strings it emitted before this extraction and
a second driver gets its own. An operator reading a failed job needs to know WHICH server
failed; a generic `inference_unreachable` would have made the two indistinguishable in
precisely the deployment this work exists to enable -- one where two servers are running.

Spec: OQ-10 / MOS-OPEN-031, MOS-OPS-008, MOS-OPS-071, MOS-OPS-072 (no server-side dynamic
batching -- batching is in-request on the leading dimension and is NOT a port concern),
MOS-OPS-083, MOS-SVC-058.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import numpy as np
import requests

from medos.inference.backend import (
    InferenceFailed,
    InferenceUnavailable,
    ModelNotResolved,
    ModelRef,
    require_shape,
)

__all__ = ["KServeConfig", "KServeV2Backend"]

#: numpy dtype -> KServe v2 `datatype`. FP32 only on the way in: the port's contract is
#: fp32 and a model wanting anything else is a different model.
_DTYPE_TO_KSERVE: dict[Any, str] = {np.dtype("float32"): "FP32"}

#: KServe v2 `datatype` -> numpy dtype, for the way back. Wider than the input map because
#: a segmentation head legitimately returns an integer label map.
_KSERVE_TO_DTYPE: dict[str, Any] = {
    "FP32": np.dtype("float32"),
    "FP64": np.dtype("float64"),
    "FP16": np.dtype("float16"),
    "INT8": np.dtype("int8"),
    "INT16": np.dtype("int16"),
    "INT32": np.dtype("int32"),
    "INT64": np.dtype("int64"),
    "UINT8": np.dtype("uint8"),
    "UINT16": np.dtype("uint16"),
    "UINT32": np.dtype("uint32"),
    "UINT64": np.dtype("uint64"),
    "BOOL": np.dtype("bool"),
}


@dataclass(frozen=True)
class KServeConfig:
    """Where the server is, and how long to wait.

    NO CREDENTIALS, and that is a statement rather than an omission. A KServe v2 server in
    this architecture is mesh-internal and reachable only from the workers; the control
    surface is separated by PROCESS (only `tritond` builds an admin client) and by network
    placement, not by a bearer token. Inventing one here would be a security control that
    looks real and is not -- the objection `medos/medos/inference/triton.py` already records for
    `MOS-OPS-...`'s model-control credential.
    """

    url: str = "http://127.0.0.1:8000"
    timeout_s: float = 300.0
    connect_timeout_s: float = 5.0

    @staticmethod
    def from_env(prefix: str) -> KServeConfig:
        return KServeConfig(
            url=os.environ.get(f"{prefix}_URL", "http://127.0.0.1:8000").rstrip("/"),
            timeout_s=float(os.environ.get(f"{prefix}_TIMEOUT_S", "300")),
            connect_timeout_s=float(os.environ.get(f"{prefix}_CONNECT_TIMEOUT_S", "5")),
        )


class KServeV2Backend:
    """An `InferenceBackend` over the KServe v2 predict protocol.

    Not abstract in the `abc` sense, because `InferenceBackend` is a `Protocol` and this is
    a base class rather than a hierarchy -- but a subclass that does not set `driver_name`
    and does not override `_resolve` will refuse to construct, below, rather than serve
    under a name that means nothing in a provenance record.
    """

    #: Set by every driver. Written into provenance (`MOS-OPS-071`).
    driver_name: str = ""

    #: Prefix for `from_env`, e.g. `MEDOS_TRITON`. Set by every driver.
    env_prefix: str = ""

    def __init__(self, config: KServeConfig | None = None) -> None:
        if not self.driver_name:
            raise TypeError(
                f"{type(self).__name__} does not set `driver_name`. It is recorded in the "
                f"provenance of every result this backend produces (MOS-OPS-071); a driver "
                f"without one cannot say which runtime computed a number."
            )
        self.config = config or KServeConfig.from_env(self.env_prefix or "MEDOS_INFERENCE")
        self._session = requests.Session()

    # -- naming ------------------------------------------------------------------------

    def _resolve(self, model_id: str, model_version: str) -> str:
        """`(model_id, model_version)` -> the server's model name.

        The ONE thing that genuinely differs between servers speaking this protocol. There
        is deliberately no default: a base class guessing a naming rule would resolve to a
        name some server accepts and another silently does not, and the failure would be
        `model not found` at inference time on a study already in flight.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must say how a model pin becomes a server-side name"
        )

    # -- readiness and metadata --------------------------------------------------------

    def is_ready(self, model_id: str, model_version: str) -> bool:
        name = self._resolve(model_id, model_version)
        try:
            response = self._call("GET", f"/v2/models/{name}/ready", timeout=10)
        except InferenceUnavailable:
            return False
        return response.status_code == 200

    def model_metadata(self, model_id: str, model_version: str) -> dict[str, Any]:
        name = self._resolve(model_id, model_version)
        response = self._call("GET", f"/v2/models/{name}", timeout=15)
        if response.status_code != 200:
            raise ModelNotResolved(
                f"{self.driver_name}_model_unknown",
                {
                    "model": str(ModelRef(model_id, model_version)),
                    "server_model_name": name,
                    "status": response.status_code,
                },
                f"{model_id}@{model_version} ({name}) is not loaded on this server",
            )
        return dict(response.json())

    # -- inference ---------------------------------------------------------------------

    def infer(
        self,
        tensor: np.ndarray,
        model_id: str,
        model_version: str,
        *,
        input_name: str | None = None,
        output_name: str | None = None,
    ) -> np.ndarray:
        """One forward pass. The binary tensor extension, both directions.

        `input_name`/`output_name` default to the model's own metadata, so a caller that
        knows only `(model_id, model_version)` -- which is all `InferenceBackend` promises
        -- does not have to know the tensor names. One metadata round trip per call is
        ~1 ms against a payload measured in tens of megabytes, and it buys the property
        that the port really is `(id, version) -> tensor`.
        """
        # THE ORDER HERE IS THE CONTRACT, AND IT WAS WRONG BEFORE THIS FILE EXISTED.
        # `medos/medos/inference/triton.py` coerced FIRST and looked the dtype up SECOND, with a
        # comment saying "a non-float dtype IS a defect and is refused by the lookup below".
        # It could not be: `astype(np.float32)` had already made every input float32, so the
        # lookup never failed and an int32 label tensor was silently reinterpreted as
        # intensities. The comment described behaviour the code did not have.
        #
        # So the KIND is checked before anything is converted. A float64 volume arriving
        # from numpy arithmetic is a caller convenience and is narrowed; an integer or
        # boolean tensor is a defect and is refused.
        if tensor.dtype.kind != "f":
            raise InferenceFailed(
                "inference_dtype_unsupported",
                {"dtype": str(tensor.dtype)},
                f"this backend serves FP32 only; got {tensor.dtype}. An integer tensor is "
                f"not narrowed to fp32 silently: if these are labels they are not model "
                f"input, and if they are intensities the caller should say so in fp32.",
            )
        if tensor.dtype != np.float32:
            tensor = tensor.astype(np.float32, copy=False)
        if not tensor.flags["C_CONTIGUOUS"]:
            tensor = np.ascontiguousarray(tensor)
        # float32 by construction now, so the lookup is a constant.
        datatype = _DTYPE_TO_KSERVE[np.dtype("float32")]

        name = self._resolve(model_id, model_version)
        if input_name is None or output_name is None:
            metadata = self.model_metadata(model_id, model_version)
            input_name = input_name or str(metadata["inputs"][0]["name"])
            output_name = output_name or str(metadata["outputs"][0]["name"])

        payload = tensor.tobytes()
        header = {
            "inputs": [
                {
                    "name": input_name,
                    "shape": list(tensor.shape),
                    "datatype": datatype,
                    "parameters": {"binary_data_size": len(payload)},
                }
            ],
            "outputs": [{"name": output_name, "parameters": {"binary_data": True}}],
        }
        header_bytes = json.dumps(header).encode()
        response = self._call(
            "POST",
            f"/v2/models/{name}/infer",
            body=header_bytes + payload,
            headers={
                "Inference-Header-Content-Length": str(len(header_bytes)),
                "Content-Type": "application/octet-stream",
            },
        )
        if response.status_code == 400 and "unknown model" in response.text.lower():
            raise ModelNotResolved(
                f"{self.driver_name}_model_unknown",
                {"model": str(ModelRef(model_id, model_version)), "server_model_name": name},
                f"{model_id}@{model_version} is not loaded on this server",
            )
        if response.status_code != 200:
            raise InferenceFailed(
                f"{self.driver_name}_infer_failed",
                {
                    "model": str(ModelRef(model_id, model_version)),
                    "server_model_name": name,
                    "status": response.status_code,
                    "body": response.text[:600],
                },
                f"POST /v2/models/{name}/infer -> {response.status_code}",
            )
        return self._decode(response, output_name, model_id, model_version)

    # -- transport ---------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
    ) -> requests.Response:
        url = f"{self.config.url}{path}"
        try:
            return self._session.request(
                method,
                url,
                data=body,
                json=json_body,
                headers=headers,
                timeout=(self.config.connect_timeout_s, timeout or self.config.timeout_s),
            )
        except requests.RequestException as exc:
            raise InferenceUnavailable(
                f"{self.driver_name}_unreachable",
                {"url": url, "method": method},
                f"{method} {path}: {type(exc).__name__}",
            ) from exc

    def _decode(
        self,
        response: requests.Response,
        output_name: str,
        model_id: str,
        model_version: str,
    ) -> np.ndarray:
        raw_len = response.headers.get("Inference-Header-Content-Length")
        if raw_len is None:
            raise InferenceFailed(
                f"{self.driver_name}_response_not_binary",
                {"model": f"{model_id}@{model_version}"},
                f"{self.driver_name} answered without Inference-Header-Content-Length; the "
                f"binary tensor extension was requested and the server did not honour it",
            )
        header_len = int(raw_len)
        content = response.content
        header = json.loads(content[:header_len])
        body = content[header_len:]
        offset = 0
        for output in header.get("outputs", ()):
            size = int(output.get("parameters", {}).get("binary_data_size", 0))
            if str(output.get("name")) == output_name:
                dtype = _KSERVE_TO_DTYPE.get(str(output.get("datatype")))
                if dtype is None:
                    raise InferenceFailed(
                        f"{self.driver_name}_output_dtype",
                        {"datatype": output.get("datatype")},
                        f"unsupported output datatype {output.get('datatype')!r}",
                    )
                shape = tuple(int(d) for d in output["shape"])
                array = np.frombuffer(body[offset : offset + size], dtype=dtype)
                return require_shape(
                    array.reshape(shape),
                    shape,
                    what=f"{model_id}@{model_version} output {output_name!r}",
                ).copy()
            offset += size
        raise InferenceFailed(
            f"{self.driver_name}_output_missing",
            {
                "requested": output_name,
                "returned": [o.get("name") for o in header.get("outputs", ())],
            },
            f"{self.driver_name} returned no output named {output_name!r}",
        )

    def close(self) -> None:
        self._session.close()
