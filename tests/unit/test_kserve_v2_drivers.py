# SPDX-License-Identifier: Apache-2.0
"""A second driver needs nothing the first did not already provide.

WHAT THIS FILE IS EVIDENCE FOR
--------------------------------
`MOS-OPS-008` says the `InferenceBackend` seam "MUST NOT be treated as resolved by any
chapter", and OQ-10's resolution declares it settled. The gap between those two sentences is
evidence, and this is it: a port with ONE driver has only ever been proven against one
implementation, and the interface such a port ends up with is the interface that
implementation happened to need.

So the test is not "does OpenVINO work" -- no OpenVINO server is running and asserting
otherwise would be a lie. The test is STRUCTURAL: every driver contributes a `driver_name`,
an env prefix and a naming rule, and overrides NOTHING ELSE. The moment a driver has to
override `infer`, the protocol in `kserve_v2.py` was Triton-shaped after all, and this file
fails and says so.

Spec: OQ-10 / MOS-OPEN-031, MOS-OPS-008, MOS-OPS-071, MOS-OPS-083.
"""

from __future__ import annotations

import numpy as np
import pytest
from medos.inference.backend import InferenceBackend, InferenceFailed
from medos.inference.kserve_v2 import KServeConfig, KServeV2Backend
from medos.inference.servers import OnnxRuntimeBackend, OpenVinoBackend
from medos.inference.triton import TritonBackend

DRIVERS = (TritonBackend, OnnxRuntimeBackend, OpenVinoBackend)

#: The protocol members a driver inherits and must not reimplement. `_resolve` is absent on
#: purpose: it is the one thing a driver DOES contribute.
INHERITED = ("infer", "is_ready", "model_metadata", "_decode", "_call", "close")


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda d: d.driver_name)
def test_a_driver_contributes_only_a_name_and_a_naming_rule(driver: type) -> None:
    """THE EVIDENCE. If this fails, the port is shaped like whichever driver was first."""
    overridden = [
        member for member in INHERITED
        if member in vars(driver)
    ]
    assert not overridden, (
        f"{driver.__name__} overrides {overridden}, which are the KServe v2 protocol itself. "
        f"A driver contributes `driver_name`, `env_prefix` and `_resolve`. If this server "
        f"genuinely needs a different wire format it is not a KServe v2 driver and belongs "
        f"in its own module -- see medos/medos/inference/servers.py on TorchServe and vLLM."
    )
    assert "_resolve" in vars(driver), (
        f"{driver.__name__} inherits `_resolve`, which raises. A base class guessing a "
        f"naming rule resolves to a name some server accepts and another silently does not."
    )


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda d: d.driver_name)
def test_every_driver_satisfies_the_port(driver: type) -> None:
    instance = driver(KServeConfig(url="http://unused.invalid"))
    assert isinstance(instance, InferenceBackend)


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda d: d.driver_name)
def test_every_driver_names_itself_for_provenance(driver: type) -> None:
    """`MOS-OPS-071` makes the runtime a numerics-relevant fact: two drivers serving one
    artifact are not interchangeable in the record even when they agree to the bit."""
    assert driver.driver_name, f"{driver.__name__} has no driver_name"
    assert driver.driver_name == driver.driver_name.lower().strip()
    assert driver.env_prefix.startswith("MEDOS_")


def test_a_driver_without_a_name_refuses_to_construct() -> None:
    class Nameless(KServeV2Backend):
        def _resolve(self, model_id: str, model_version: str) -> str:
            return model_id

    with pytest.raises(TypeError, match="driver_name"):
        Nameless(KServeConfig(url="http://unused.invalid"))


def test_the_base_refuses_to_guess_a_naming_rule() -> None:
    class Unnamed(KServeV2Backend):
        driver_name = "probe"

    with pytest.raises(NotImplementedError, match="server-side name"):
        Unnamed(KServeConfig(url="http://unused.invalid"))._resolve("m", "1.0.0")


def test_triton_keeps_its_own_error_reason_codes() -> None:
    """The reasons are built from `driver_name`, so the extraction did not rename them.

    An operator reading a failed job needs to know WHICH server failed. A generic
    `inference_unreachable` would make two running servers indistinguishable in exactly the
    deployment this work exists to enable.
    """
    import requests
    from medos.inference.backend import InferenceUnavailable

    # The transport is stubbed rather than pointed at an unroutable address: this
    # environment has an HTTP proxy that ACCEPTS a connection to 127.0.0.1:1 and then
    # blocks, so a "nothing is listening" test spent 15 seconds proving the proxy exists.
    # A test that needs the network to be a particular shape is a test that fails on
    # somebody else's laptop for a reason unrelated to the code.
    backend = TritonBackend(KServeConfig(url="http://unused.invalid"))

    def refuse(*_a, **_k):
        raise requests.ConnectionError("stubbed")

    backend._session.request = refuse
    with pytest.raises(InferenceUnavailable) as caught:
        backend._call("GET", "/v2/health/ready")
    assert caught.value.reason_code == "triton_unreachable"


def test_a_non_float_tensor_is_refused_rather_than_coerced() -> None:
    """fp32 is the port's contract. A float64 volume from numpy arithmetic is a caller
    convenience and is converted; an integer tensor is a defect and is refused."""
    backend = TritonBackend(KServeConfig(url="http://unused.invalid"))
    with pytest.raises(InferenceFailed, match="FP32 only"):
        backend.infer(np.ones((1, 1, 2, 2, 2), dtype=np.int32), "medos.lung-seg", "1.0.0")


def test_the_naming_rules_differ_and_carry_the_version() -> None:
    """A name that dropped the version would let a config change swap the served weights
    under a pinned `ModelVersion`."""
    for driver in DRIVERS:
        name = driver(KServeConfig(url="http://unused.invalid"))._resolve(
            "medos.lung-seg", "1.2.0"
        )
        assert "1_2_0" in name or "1.2.0" in name, (
            f"{driver.driver_name}: {name!r} loses the version"
        )


def test_is_ready_is_false_rather_than_raising_when_the_server_is_down() -> None:
    """Readiness is a question, not an assertion. A worker polling readiness during startup
    must not crash because the server is not up yet."""
    import requests

    backend = TritonBackend(KServeConfig(url="http://unused.invalid"))
    backend._session.request = lambda *a, **k: (_ for _ in ()).throw(
        requests.ConnectionError("down")
    )
    assert backend.is_ready("medos.lung-seg", "1.0.0") is False
