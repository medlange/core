# SPDX-License-Identifier: Apache-2.0
"""Triton: the admin client `tritond` uses, and the `InferenceBackend` driver 1.

TWO CLIENTS, ONE SERVER, AND THE REASON THEY ARE SEPARATE
    Chapter 13 §13.10: "the service container is Triton's *client* and the platform is
    Triton's *administrator*.  `medicalos-tritond` is the only holder of Triton's
    model-control credential."  Those are two different privilege levels against one HTTP
    surface, so they are two classes:

      * `TritonAdminClient` -- repository index, load, unload, server metadata, readiness.
        Used ONLY by `medos.inference.tritond`.  Holds the model-control credential.
      * `TritonBackend` -- `infer` and `is_ready`, and nothing else.  This is what a
        worker gets.  It physically cannot load or unload a model, which is what makes
        "tritond is the only holder" a property of the code rather than of a convention.

    A worker that could call `/v2/repository/models/{name}/load` would be able to defeat
    every residency decision in §13.10.3 by loading a model into a budget it never
    reserved against, and the starvation in §13.10.4 would become an out-of-memory.

WIRE FORMAT
    KServe v2 over HTTP, with the binary tensor extension for both directions.  Not JSON
    tensors: one 128³ fp32 patch is 8.39 MB, and §13.10.2's batch-8 input tensor is
    67.1 MB -- as a JSON array of decimal floats that is roughly 800 MB of text, parsed
    twice.  The binary extension is `Inference-Header-Content-Length` plus raw
    little-endian payload appended to the JSON header, and it is about forty lines.

    `tritonclient` is deliberately not a dependency: it pulls `grpcio`, `protobuf`, and
    `python-rapidjson` into an image whose entire runtime dependency set is eight pinned
    packages (CONTRACT.md §11, `medos/deploy/compose/requirements.txt`), for an HTTP call this
    module makes with `requests`.

NO RETRIES HERE
    `infer` does not retry.  §13.10.3 puts the waiting in the residency reservation
    (`202 queued`, `retry_after_seconds`) and `MOS-OPS-085` requires a contended job to
    stay `QUEUED` rather than consume retry budget.  A retry loop inside `infer` would
    burn lease time (`MOS-OPS-081`'s exact complaint about claiming first and waiting
    second) and hide contention from the metric that is supposed to show it.

Spec: MOS-OPS-068, MOS-OPS-069, MOS-OPS-074, MOS-OPS-075, MOS-OPS-091, MOS-OPS-093,
MOS-OPS-015, OQ-10 / MOS-OPEN-031 (the port this implements is provisional).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import requests

from medos.inference.backend import (
    InferenceFailed,
    InferenceUnavailable,
    ModelNotResolved,
)
from medos.inference.kserve_v2 import KServeV2Backend
from medos.inference.repository import triton_model_name

__all__ = [
    "TritonAdminClient",
    "TritonBackend",
    "TritonConfig",
    "REQUIRED_SERVER_FLAGS",
]

# MOS-OPS-074, verbatim.  `tritond` asserts these against the running server at startup
# (see `TritonAdminClient.assert_server_flags`) instead of trusting the compose file:
# a flag that is only in a YAML file is a flag that is one `docker run` away from absent.
REQUIRED_SERVER_FLAGS: dict[str, str] = {
    "model_control_mode": "explicit",
    "strict_readiness": "true",
    "exit_on_error": "true",
    "response_cache_byte_size": "0",
}

_DTYPE_TO_TRITON = {np.dtype("float32"): "FP32"}
_TRITON_TO_DTYPE = {"FP32": np.dtype("float32")}


@dataclass(frozen=True)
class TritonConfig:
    """Where Triton is. No credentials: this deployment's Triton is mesh-internal.

    §13.10's model-control credential is expressed here as the SEPARATION between
    `TritonAdminClient` and `TritonBackend` plus the network placement of the server
    (compose `internal` network), not as a bearer token -- Triton has no native
    authentication, and inventing one in this module would be a security control that
    looks real and is not.  The honest statement is: Triton is reachable only from
    `medicalos-tritond` and the workers, and only `tritond` constructs an admin client.
    """

    url: str = "http://127.0.0.1:8000"
    timeout_s: float = 300.0
    connect_timeout_s: float = 5.0

    @staticmethod
    def from_env(prefix: str = "MEDOS_TRITON") -> TritonConfig:
        return TritonConfig(
            url=os.environ.get(f"{prefix}_URL", "http://127.0.0.1:8000").rstrip("/"),
            timeout_s=float(os.environ.get(f"{prefix}_TIMEOUT_S", "300")),
            connect_timeout_s=float(os.environ.get(f"{prefix}_CONNECT_TIMEOUT_S", "5")),
        )


class _TritonHttp:
    """Shared transport. Not public: the privilege split above is the public surface."""

    def __init__(self, config: TritonConfig | None = None) -> None:
        self.config = config or TritonConfig.from_env()
        self._session = requests.Session()

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
            response = self._session.request(
                method,
                url,
                data=body,
                json=json_body,
                headers=headers,
                timeout=(self.config.connect_timeout_s, timeout or self.config.timeout_s),
            )
        except requests.RequestException as exc:
            raise InferenceUnavailable(
                "triton_unreachable",
                {"url": url, "method": method},
                f"{method} {path}: {type(exc).__name__}",
            ) from exc
        return response

    def close(self) -> None:
        self._session.close()


class TritonAdminClient(_TritonHttp):
    """The model-control surface. `tritond` only.

    Every method here corresponds to something §13.10 requires the platform to be able to
    do: enumerate what is loaded, load a pinned version, evict one for the rotation of
    `MOS-OPS-086`, and measure the load duration `MOS-OPS-091` defines.
    """

    def server_metadata(self) -> dict[str, Any]:
        response = self._call("GET", "/v2", timeout=10)
        if response.status_code != 200:
            raise InferenceUnavailable(
                "triton_not_ready",
                {"status": response.status_code},
                f"GET /v2 -> {response.status_code}",
            )
        return dict(response.json())

    def is_live(self) -> bool:
        try:
            return self._call("GET", "/v2/health/live", timeout=5).status_code == 200
        except InferenceUnavailable:
            return False

    def is_server_ready(self) -> bool:
        """`--strict-readiness=true` (MOS-OPS-074) makes this mean "every loaded model is
        ready", not merely "the process is up"."""
        try:
            return self._call("GET", "/v2/health/ready", timeout=5).status_code == 200
        except InferenceUnavailable:
            return False

    def server_flags(self) -> dict[str, str]:
        """Read the flags Triton reports in its metadata `extensions`/config where it can.

        Triton does not expose its full command line over HTTP, so the observable proxies
        are used: the presence of the `model_repository(unload)` extension implies
        `--model-control-mode=explicit`, and the absence of any cached response implies
        the cache is off.  `assert_server_flags` documents which of MOS-OPS-074's four
        flags are OBSERVED and which are only DECLARED in the compose file, because the
        difference matters to anyone auditing this.
        """
        metadata = self.server_metadata()
        extensions = {str(e) for e in metadata.get("extensions", ())}
        # Triton advertises the unload sub-extension ONLY when the repository can be
        # mutated at runtime, which is `--model-control-mode=explicit`. The exact spelling
        # varies by release (24.05 says `model_repository(unload_dependents)`), so the
        # prefix is matched rather than the whole token.
        explicit = any(e.startswith("model_repository(unload") for e in extensions)
        return {
            "model_control_mode": "explicit" if explicit else "unknown",
            "server_version": str(metadata.get("version", "")),
            "extensions": ",".join(sorted(extensions)),
        }

    def assert_server_flags(self) -> dict[str, Any]:
        """Verify what CAN be verified of MOS-OPS-074 over HTTP; report the rest honestly.

        Raises when the one observable flag is wrong -- an implicit/poll-mode Triton would
        load every directory it finds, which defeats §13.10.3's budget entirely because a
        model can become resident without ever passing through a reservation.
        """
        flags = self.server_flags()
        if flags["model_control_mode"] != "explicit":
            raise InferenceFailed(
                "triton_model_control_mode",
                {"observed": flags},
                "MOS-OPS-074 requires --model-control-mode=explicit; this server "
                "advertises no model_repository(unload...) extension, which means it "
                "will load every directory it finds and residency accounting is void",
            )
        return {
            "observed": {"model_control_mode": "explicit"},
            "declared_only": [
                # Not exposed over HTTP by Triton.  Asserted in the compose command line
                # and re-stated here so an auditor knows this check did not cover them.
                "strict_readiness",
                "exit_on_error",
                "response_cache_byte_size",
            ],
            "server_version": flags["server_version"],
        }

    def index(self) -> list[dict[str, Any]]:
        """`POST /v2/repository/index` -- every model in the repository and its state."""
        response = self._call("POST", "/v2/repository/index", json_body={}, timeout=30)
        if response.status_code != 200:
            raise InferenceFailed(
                "triton_index_failed",
                {"status": response.status_code, "body": response.text[:400]},
                f"POST /v2/repository/index -> {response.status_code}",
            )
        return list(response.json())

    def is_model_ready(self, name: str) -> bool:
        try:
            response = self._call("GET", f"/v2/models/{name}/ready", timeout=10)
        except InferenceUnavailable:
            return False
        return response.status_code == 200

    def load(self, name: str, *, ready_timeout_s: float = 120.0) -> float:
        """Load one model and return its load duration in seconds (MOS-OPS-091).

        "measured from the Triton `POST /v2/repository/models/{name}/load` call to
        model-ready, including `model_warmup`" -- so the clock starts before the call and
        stops after readiness is observed, not when the HTTP call returns.  Triton's load
        is synchronous by default, so the readiness poll normally completes on its first
        iteration; it is still polled, because `--strict-readiness` plus a warm-up that is
        still running is exactly the window in which "loaded" and "ready" differ.

        MOS-OPS-093: this duration MUST NOT include format conversion.  It cannot here --
        MOS-OPS-071's accelerator stanza is rejected at registration by
        `repository.validate_config_pbtxt`, so there is no conversion to include.
        """
        started = time.monotonic()
        response = self._call(
            "POST", f"/v2/repository/models/{name}/load", json_body={}, timeout=ready_timeout_s
        )
        if response.status_code != 200:
            raise ModelNotResolved(
                "triton_load_failed",
                {"triton_model_name": name, "status": response.status_code,
                 "body": response.text[:600]},
                f"POST /v2/repository/models/{name}/load -> {response.status_code}",
            )
        deadline = started + ready_timeout_s
        while time.monotonic() < deadline:
            if self.is_model_ready(name):
                return time.monotonic() - started
            time.sleep(0.2)
        raise ModelNotResolved(
            "triton_load_not_ready",
            {"triton_model_name": name, "ready_timeout_s": ready_timeout_s},
            f"{name} loaded but never became ready within {ready_timeout_s}s",
        )

    def unload(self, name: str, *, unload_dependents: bool = False) -> None:
        """Evict one model. The rotation of MOS-OPS-086 calls this and nothing else does."""
        response = self._call(
            "POST",
            f"/v2/repository/models/{name}/unload",
            json_body={"parameters": {"unload_dependents": unload_dependents}},
            timeout=60,
        )
        if response.status_code != 200:
            raise InferenceFailed(
                "triton_unload_failed",
                {"triton_model_name": name, "status": response.status_code},
                f"POST /v2/repository/models/{name}/unload -> {response.status_code}",
            )

    def model_config(self, name: str) -> dict[str, Any]:
        response = self._call("GET", f"/v2/models/{name}/config", timeout=15)
        if response.status_code != 200:
            raise ModelNotResolved(
                "triton_model_unknown",
                {"triton_model_name": name, "status": response.status_code},
                f"GET /v2/models/{name}/config -> {response.status_code}",
            )
        return dict(response.json())


class TritonBackend(KServeV2Backend):
    """`InferenceBackend` driver 1 (§15.2.9). Inference only -- no model control.

    THE PROTOCOL IS NOT TRITON'S, AND NO LONGER LIVES HERE. KServe v2 over HTTP with the
    binary tensor extension is an open predict protocol that ONNX Runtime Server and
    OpenVINO Model Server also serve, so the request assembly, the response walk, the dtype
    handling and the error taxonomy moved to `medos/medos/inference/kserve_v2.py`. A port with
    one driver has only ever been proven against one implementation; `MOS-OPS-008` says the
    seam "MUST NOT be treated as resolved by any chapter", and one way to stop asserting
    that is to have a second driver need nothing the first did not already provide.

    What stayed is the one thing that is genuinely Triton's: `MOS-OPS-068`'s model-name
    derivation. The error reason codes are unchanged -- `KServeV2Backend` builds them from
    `driver_name`, so this class still emits `triton_unreachable`, `triton_model_unknown`
    and the rest, because an operator reading a failed job needs to know WHICH server
    failed and a generic code would make two running servers indistinguishable.

    `driver_name` is `"triton"` and is written into the provenance of anything this
    produces, because `MOS-OPS-071` makes the runtime a numerics-relevant fact.
    """

    driver_name = "triton"
    env_prefix = "MEDOS_TRITON"

    def _resolve(self, model_id: str, model_version: str) -> str:
        """`(model_id, model_version)` -> Triton model name. The registry contract, run.

        §15.2.9: "Loading by id and version means the registry contract is genuinely
        exercised from the first slice instead of being asserted."  There is no default
        version and no fallback to another one: an unresolvable pin is `ModelNotResolved`,
        which is a `SystemFailure` and therefore never a clinical `REJECTED`
        (`MOS-OPS-083`'s distinction).
        """
        return triton_model_name(model_id, model_version)
