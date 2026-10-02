# SPDX-License-Identifier: Apache-2.0
"""Native-mode inference: the provisional `InferenceBackend` seam and its one driver.

WHAT THIS PACKAGE IS FOR
    `docs/spec/15-delivery.md` §15.2.4: "the move off the weeks 1-2 in-process loader:
    native-mode inference runs on the single shared Triton that the 0.1.0 contents name,
    administered by `medicalos-tritond` (Chapter 13, §13.10).  The in-process
    PyTorch/MONAI loader survives only as a CPU-only test fixture and is not a shipped
    driver."

    Module map:

        backend.py     the PROVISIONAL port (OQ-10) + the `NativeModel` consumer contract
        triton.py      driver 1: admin client (tritond only) and inference client (workers)
        repository.py  §13.10.1 -- model naming, config.pbtxt, medicalos.json, the gates
        residency.py   §13.10.3/§13.10.4 -- budget, reservations, the eviction rotation
        tritond.py     `medicalos-tritond` -- staging from the object store, pins, health
        selftest.py    the one really-served model, and MOS-OPS-015's output-hash check
        dispatch.py    the single join point between a capability and the model server
        inprocess.py   TEST FIXTURE. NOT A SHIPPED DRIVER. See its docstring.

WHAT IS NOT DECIDED HERE
    OQ-10 (`MOS-OPEN-031`, decision due at gate `G-0.3.0`) asks whether the inference
    runtime is declared replaceable the way the PACS is.  Chapter 13 `MOS-OPS-008` says
    the seam "MUST NOT be treated as resolved by any chapter", and this package does not
    resolve it.  What ships is Triton, as the only driver, with one interface in front of
    it so that the question remains answerable.  Saying "we introduced a port, therefore
    the runtime is replaceable" would be answering OQ-10 by implementation, which is
    precisely what `MOS-OPS-008` forbids.

NOT NAMED BY CONTRACT.md §1
    §1 fixes the weeks 1-2 layout, which has no inference package because weeks 1-2 has no
    model server (§0: "NOT in this slice: ... Triton").  §1's rule for this case is
    "define it ONLY inside your own module and report it": this package defines nothing
    that belongs to another module, redefines nothing from §1, and the two names it adds
    to the worker's vocabulary (`WorkerDeps.inference`, `run_capability`) are reported.

Spec: MOS-REL-023, MOS-OPS-008, MOS-OPS-068 .. MOS-OPS-093, MOS-OPS-113, MOS-SVC-058,
OQ-10 / MOS-OPEN-031.
"""

from medos.inference.backend import (
    InferenceBackend,
    InferenceFailed,
    InferenceUnavailable,
    ModelNotResolved,
    ModelRef,
    NativeModel,
)
from medos.inference.dispatch import (
    NativeBackendMissing,
    build_backend,
    native_model_of,
    run_capability,
)
from medos.inference.repository import (
    ModelManifest,
    ModelRepositoryError,
    triton_model_name,
)
from medos.inference.triton import TritonAdminClient, TritonBackend, TritonConfig

__all__ = [
    "InferenceBackend",
    "NativeModel",
    "ModelRef",
    "ModelNotResolved",
    "InferenceUnavailable",
    "InferenceFailed",
    "NativeBackendMissing",
    "run_capability",
    "native_model_of",
    "build_backend",
    "TritonBackend",
    "TritonAdminClient",
    "TritonConfig",
    "ModelManifest",
    "ModelRepositoryError",
    "triton_model_name",
]
