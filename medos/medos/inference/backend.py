# SPDX-License-Identifier: Apache-2.0
"""The `InferenceBackend` seam, its errors, and the `NativeModel` consumer contract.

READ THIS BEFORE TREATING THE PORT AS SETTLED
    `docs/spec/15-delivery.md` §15.2.9 names the seam as
    `InferenceBackend: infer(volume, model_id, model_version) -> tensor` and immediately
    labels it **"proposed, NOT settled; see OQ-10 (Chapter 16)"**.  Chapter 13
    `MOS-OPS-008` says the same thing from the other side: "Whether Triton is replaceable
    the way the PACS is replaceable is **not settled by this chapter** ... the
    `InferenceBackend` seam ... MUST NOT be treated as resolved by any chapter."
    OQ-10's decision is due at gate `G-0.3.0`.

    **This file therefore introduces a PROVISIONAL seam and says so plainly.**  It exists
    because the alternative -- calling Triton's HTTP API from inside capability code --
    would violate CONTRACT.md §6 ("a capability MUST NOT touch the database, the network,
    or the PACS") and would leave nothing to swap if OQ-10 resolves towards
    replaceability.  It does NOT decide OQ-10: exactly one driver ships
    (`medos.inference.triton.TritonBackend`), the in-process CPU loader is explicitly
    labelled a test fixture and not a driver (§15.2.9, §15.2.4), and no second backend is
    implemented.  If OQ-10 resolves the other way at `G-0.3.0`, deleting this protocol and
    calling `TritonBackend` directly is a one-file change.

WHY `infer(tensor, ...)` AND NOT `infer(volume, ...)`
    §15.2.9 writes the signature as `infer(volume, model_id, model_version) -> tensor`.
    Taking a `CanonicalVolume` would put preprocessing -- windowing, spacing
    normalisation, patch extraction -- behind the port, which makes it part of the
    replaceability contract and means two drivers could preprocess differently while
    claiming the same `model_version`.  `MOS-OPS-015`'s preprocessing self-test exists
    precisely because preprocessing is a versioned, testable property of the MODEL and not
    of the runtime.  So the port carries tensors, and preprocessing lives in `NativeModel`
    (below) on the capability side of the seam, where it is covered by the model version.
    This is a deliberate deviation from the one-line sketch in §15.2.9 and it is reported
    rather than silently applied.

THE CONSUMER SIDE: `NativeModel`
    CONTRACT.md §6 fixes `CapabilityContext` at four members and forbids capability code
    from touching the network.  A capability that needs a served model therefore cannot
    hold a backend and cannot receive one through its context.  `NativeModel` splits the
    capability into the two pure halves either side of the network call:

        tensor_in  = model.preprocess(vol, ctx)          # pure
        tensor_out = backend.infer(tensor_in, id, ver)   # the worker does the I/O
        outcome    = model.postprocess(tensor_out, vol, ctx)   # pure

    `medos.worker.steps.step_service_invoke` is the only code that joins them, which keeps
    the I/O in the component that already owns I/O and leaves the capability testable on a
    fixture with no server running.

Spec: MOS-REL-023 (a deferred subsystem needs a named seam), OQ-10 / MOS-OPEN-031,
MOS-OPS-008, MOS-OPS-015, MOS-SVC-058, CONTRACT.md §6, §11.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

from medos.core.errors import SystemFailure, TransportFailure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from medos.capabilities.base import CapabilityContext
    from medos.core.bundle import CapabilityOutcome
    from medos.core.geometry import CanonicalVolume

__all__ = [
    "ModelRef",
    "InferenceBackend",
    "NativeModel",
    "ModelNotResolved",
    "InferenceUnavailable",
    "InferenceFailed",
    "MODEL_ID_RE",
    "SEMVER_RE",
]

# `pulmo.pleural-effusion` in chapter 13's worked example: dot-separated namespace, and
# hyphens inside a segment.  Anchored, because `triton_model_name` (MOS-OPS-068) is a
# textual substitution and an id containing `__` or `/` would collide with another
# model's directory or escape the model repository root.
MODEL_ID_RE = re.compile(r"^[a-z0-9]+(?:[-][a-z0-9]+)*(?:\.[a-z0-9]+(?:[-][a-z0-9]+)*)+$")

# Plain three-part semver. Pre-release and build metadata are rejected rather than
# mangled: MOS-OPS-068 maps `.` to `_` and nothing else, so `1.0.0-rc.1` and `1.0.0-rc_1`
# would produce the same Triton model name from two different ModelVersions.
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class ModelNotResolved(SystemFailure):
    """`(model_id, model_version)` names nothing the backend can serve.

    A `SystemFailure`, never a `ClinicalRejection`: chapter 5 `MOS-EXEC-001` reserves
    `REJECTED` for statements about the STUDY, and "the deployment pinned a version that
    was never published" is a statement about the cluster.  `MOS-OPS-083` makes the same
    distinction for the capacity case and is explicit that it "MUST ... never `REJECTED`".
    """


class InferenceUnavailable(TransportFailure):
    """The model server could not be reached, or is not ready to serve this model.

    `TransportFailure` and therefore retryable: Triton restarting, a model mid-load, or a
    reservation queued behind `MOS-OPS-086`'s rotation are all states that resolve on
    their own.  `MOS-OPS-085` is emphatic that the contended case must not fail a job.
    """


class InferenceFailed(SystemFailure):
    """The server answered, and the answer is unusable (wrong shape, wrong dtype, error).

    Not retryable: a model that returns `[1,2,16,16,16]` where its own `config.pbtxt`
    declares `[-1,1,16,16,16]` will do so again.
    """


@dataclass(frozen=True)
class ModelRef:
    """A model identity: `model_id` plus an exact `model_version`. Both, always.

    §15.2.9: "Loading by id and version means the registry contract is genuinely
    exercised from the first slice instead of being asserted."  There is deliberately no
    `latest`, no floating minor, and no default version -- a resolution that can silently
    move is a resolution that can serve a model no `EvaluationRun` covers
    (`MOS-OPS-071`: a conversion is a new `ModelVersion` "requiring its own
    `EvaluationRun`").
    """

    model_id: str
    model_version: str

    def __post_init__(self) -> None:
        if not MODEL_ID_RE.match(self.model_id):
            raise ValueError(
                f"model_id {self.model_id!r} is not a dotted lowercase identifier; "
                "MOS-OPS-068 derives the Triton model name from it by textual "
                "substitution, so the character set is part of the contract"
            )
        if not SEMVER_RE.match(self.model_version):
            raise ValueError(
                f"model_version {self.model_version!r} is not a plain X.Y.Z semver; "
                "MOS-OPS-068 maps '.' to '_' and nothing else, so a pre-release suffix "
                "would alias two ModelVersions onto one Triton model name"
            )

    def __str__(self) -> str:
        return f"{self.model_id}@{self.model_version}"


@runtime_checkable
class InferenceBackend(Protocol):
    """PROVISIONAL (OQ-10). One method that matters, plus readiness.

    `driver_name` is not decoration: the provenance record has to be able to say which
    runtime produced a tensor, and `MOS-OPS-071` makes "which backend" a numerics-relevant
    fact rather than an implementation detail.
    """

    driver_name: str

    def infer(
        self, tensor: np.ndarray, model_id: str, model_version: str
    ) -> np.ndarray:
        """Run one forward pass. `tensor` carries its batch dimension explicitly."""

    def is_ready(self, model_id: str, model_version: str) -> bool:
        """True when this exact `(model_id, model_version)` can serve a request now."""


@runtime_checkable
class NativeModel(Protocol):
    """The capability-side half of a native-mode inference: two pure functions.

    Neither half may touch the network -- CONTRACT.md §6 -- which is exactly what makes
    a `NativeModel` unit-testable against `medos.inference.inprocess` with no server.
    """

    model_id: str
    model_version: str

    def preprocess(self, vol: CanonicalVolume, ctx: CapabilityContext) -> np.ndarray:
        """`CanonicalVolume` -> the input tensor, batch dimension first."""

    def postprocess(
        self, tensor: np.ndarray, vol: CanonicalVolume, ctx: CapabilityContext
    ) -> CapabilityOutcome:
        """The output tensor -> a `CapabilityOutcome` on the SOURCE grid (MOS-IMG-039)."""


def require_shape(
    tensor: np.ndarray, expected: tuple[int, ...], *, what: str
) -> np.ndarray:
    """Assert a tensor's shape and dtype at the seam, with a message that names the model.

    Every backend calls this on the way out.  A shape mismatch discovered three functions
    later, inside a reshape in postprocessing, is the same defect with none of the
    context; `MOS-IMG-141` ("the platform independently re-validates what the service
    returned") is the same principle applied one layer up.
    """
    if tensor.shape != expected:
        raise InferenceFailed(
            "inference_shape_mismatch",
            {"what": what, "expected": list(expected), "got": list(tensor.shape)},
            f"{what}: expected tensor of shape {expected}, got {tensor.shape}",
        )
    if tensor.dtype != np.float32:
        raise InferenceFailed(
            "inference_dtype_mismatch",
            {"what": what, "expected": "float32", "got": str(tensor.dtype)},
            f"{what}: expected float32, got {tensor.dtype}",
        )
    return tensor
