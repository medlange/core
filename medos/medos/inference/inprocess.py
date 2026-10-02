# SPDX-License-Identifier: Apache-2.0
"""The in-process CPU loader. **NOT A SHIPPED DRIVER.** A test fixture, and only that.

READ THE LABEL
    `docs/spec/15-delivery.md` §15.2.9, `InferenceBackend` row: "The in-process
    PyTorch/MONAI loader exists only for the weeks 0-2 spike and for CPU-only test
    fixtures and is **NOT a shipped driver**."  §15.2.4 says it again: "The in-process
    PyTorch/MONAI loader survives only as a CPU-only test fixture and is not a shipped
    driver."

    This module is that fixture.  It is not selected by `medos.inference.build_backend`,
    it is not reachable from any configuration value, and no code under `medos/medos/worker`,
    `medos/medos/api` or `medos/medos/capabilities` imports it.  The only way to obtain one is
    to construct it in a test, and construction REQUIRES a written reason (see
    `fixture_reason`) that is carried in the driver name -- so a stray instance shows up
    in provenance as `inprocess-fixture:<reason>` and cannot be mistaken for Triton.

WHY IT STILL EXISTS
    Two jobs, neither of which Triton can do:

      1. It is the numerics ORACLE.  `tests/integration/test_triton.py` asserts that
         Triton's answer equals this module's answer bit-for-bit on the golden fixture.
         Without an independent implementation, "Triton returned a tensor" is the only
         thing a test can assert, and that is compatible with Triton returning the wrong
         tensor.
      2. It makes `NativeModel.preprocess` / `postprocess` testable with no server, which
         is exactly the property CONTRACT.md §6 buys by keeping capabilities pure.

WHY THERE IS NO PyTorch AND NO MONAI HERE
    Because there is no learned model in this deployment yet.  Adding `torch` (~2.5 GB
    with CUDA, ~200 MB CPU-only) to an eight-package runtime dependency set to run an
    affine map would be a cost with no purchase.  If a MONAI bundle is ever served, this
    module gains a second implementation behind the same class and the docstring above
    stops mentioning numpy -- the LABEL does not change, because the label is about
    shipping status, not about which library is inside.

Spec: MOS-REL-023, §15.2.4, §15.2.9, OQ-10 / MOS-OPEN-031.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from medos.inference.backend import ModelNotResolved, ModelRef

__all__ = ["InProcessCpuBackend", "NOT_A_SHIPPED_DRIVER"]

# Imported by tests and by nothing else. Present so that `grep NOT_A_SHIPPED_DRIVER`
# finds every use site in one command.
NOT_A_SHIPPED_DRIVER = True


class InProcessCpuBackend:
    """An `InferenceBackend` over pure-numpy reference implementations. Fixture only.

    `models` maps `(model_id, model_version)` to a callable `f(tensor) -> tensor`.  The
    key is the PAIR, never the id alone: a fixture that resolved by id would not be able
    to reproduce the version-pinning behaviour it exists to cross-check.
    """

    def __init__(
        self,
        models: dict[tuple[str, str], Callable[[np.ndarray], np.ndarray]],
        *,
        fixture_reason: str,
    ) -> None:
        if not fixture_reason or not fixture_reason.strip():
            raise ValueError(
                "InProcessCpuBackend requires a written fixture_reason. This is not a "
                "shipped driver (docs/spec/15-delivery.md §15.2.9); an instance with no "
                "stated reason is an instance someone is about to use in production."
            )
        self._models = dict(models)
        self.fixture_reason = fixture_reason.strip()
        self.driver_name = f"inprocess-fixture:{self.fixture_reason}"
        self.calls = 0

    def is_ready(self, model_id: str, model_version: str) -> bool:
        return (model_id, model_version) in self._models

    def infer(self, tensor: np.ndarray, model_id: str, model_version: str) -> np.ndarray:
        ref = ModelRef(model_id=model_id, model_version=model_version)
        function = self._models.get((model_id, model_version))
        if function is None:
            raise ModelNotResolved(
                "fixture_model_unknown",
                {"model": str(ref), "known": sorted(f"{a}@{b}" for a, b in self._models)},
                f"{ref} is not in this fixture backend; resolution is by id AND version",
            )
        self.calls += 1
        return np.ascontiguousarray(
            function(np.asarray(tensor, dtype=np.float32)), dtype=np.float32
        )
