# SPDX-License-Identifier: Apache-2.0
"""Where a capability meets the model server, and the only place that join happens.

THE PROBLEM THIS SOLVES
    CONTRACT.md §6: "A capability MUST NOT touch the database, the network, or the PACS."
    §6 also fixes `CapabilityContext` at four members, so a capability cannot be handed an
    `InferenceBackend` through its context either.  Native-mode inference
    (`docs/spec/15-delivery.md` §15.2.4) nevertheless has to reach Triton over HTTP.

    `run_capability` is the seam.  A capability that needs a served model exposes a
    `native_model` attribute satisfying `medos.inference.backend.NativeModel`; this
    function performs the three-step sandwich and returns the same `CapabilityOutcome` a
    deterministic capability would have returned:

        tensor_in  = native.preprocess(vol, ctx)
        tensor_out = backend.infer(tensor_in, native.model_id, native.model_version)
        outcome    = native.postprocess(tensor_out, vol, ctx)

    `medos.worker.steps.step_service_invoke` calls this for EVERY capability, native or
    not, so there is exactly one call site and the branch is visible in one function
    rather than spread across the executor.

WHY IT IS NOT IN `medos/medos/worker/steps.py`
    Two reasons.  It is testable without a database -- `steps.py` writes sub-progress to
    `job_steps.detail` on every iteration (MOS-EXEC-024), so exercising the native branch
    there requires a live Postgres and a job row for what is a pure dispatch decision.
    And it keeps the provisional `InferenceBackend` port (OQ-10) out of the executor: if
    OQ-10 resolves against a port at `G-0.3.0`, this file changes and `steps.py` does not.

Spec: MOS-EXEC-024, MOS-OPS-083, OQ-10 / MOS-OPEN-031, CONTRACT.md §6, §7.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from medos.core.errors import SystemFailure

if TYPE_CHECKING:  # pragma: no cover - typing only
    from medos.capabilities.base import CapabilityContext
    from medos.core.bundle import CapabilityOutcome
    from medos.core.geometry import CanonicalVolume
    from medos.inference.backend import InferenceBackend

__all__ = [
    "run_capability",
    "native_model_of",
    "build_backend",
    "build_resolver",
    "NativeBackendMissing",
]


class NativeBackendMissing(SystemFailure):
    """A native-mode capability was scheduled on a worker with no inference backend.

    A `SystemFailure` and never a `ClinicalRejection`: `MOS-OPS-083` draws exactly this
    line -- "`REJECTED` is a clinical outcome about the study ... and this is an operator
    error about the cluster".  A worker that silently fell back to an in-process
    implementation here would serve a model no `EvaluationRun` covers, which is the defect
    `MOS-OPS-071` exists to prevent, so there is no fallback.
    """


def native_model_of(capability: object) -> Any | None:
    """Return the capability's `NativeModel`, or None for a deterministic capability.

    Attribute-based rather than `isinstance`, because `medos.capabilities` must not import
    `medos.inference` -- a capability package that imports the inference package is one
    edit away from a capability that calls it.
    """
    return getattr(capability, "native_model", None)


def run_capability(
    capability: Any,
    vol: CanonicalVolume,
    ctx: CapabilityContext,
    backend: InferenceBackend | None = None,
) -> CapabilityOutcome:
    """Run one capability, routing native-mode inference through `backend`.

    For a deterministic capability this is `capability.run(vol, ctx)` and nothing else --
    the existing three capabilities take exactly the path they took in weeks 1-2, with no
    Triton dependency and no behaviour change.
    """
    native = native_model_of(capability)
    if native is None:
        return capability.run(vol, ctx)  # type: ignore[no-any-return]

    # PER-MODEL ROUTING. `backend` may be a single `InferenceBackend` -- the shape every
    # caller passed before OQ-10 was decided -- or a `BackendResolver`, which maps this
    # model to the adapter this deployment configured for it. Duck-typed on `.resolve`
    # rather than `isinstance`, so `medos.inference.dispatch` does not import the
    # catalogue and a test fake needs no base class.
    #
    # Resolution happens HERE, after the capability is known to be native, because a
    # deterministic capability must not cause a backend to be constructed at all.
    resolve = getattr(backend, "resolve", None)
    if callable(resolve):
        backend = resolve(native.model_id)

    if backend is None:
        raise NativeBackendMissing(
            "inference_backend_absent",
            {
                "capability_id": getattr(capability, "capability_id", "?"),
                "model_id": getattr(native, "model_id", "?"),
                "model_version": getattr(native, "model_version", "?"),
            },
            f"{getattr(capability, 'capability_id', '?')} is a native-mode capability and "
            "this worker has no InferenceBackend configured. There is deliberately no "
            "in-process fallback: it would serve a model no EvaluationRun covers.",
        )
    tensor_in = native.preprocess(vol, ctx)
    tensor_out = backend.infer(tensor_in, native.model_id, native.model_version)
    return native.postprocess(tensor_out, vol, ctx)  # type: ignore[no-any-return]


def build_backend() -> InferenceBackend | None:
    """The ONLY factory. Returns a `TritonBackend`, or None when none is configured.

    There is no branch here that can return `medos.inference.inprocess`: that module is a
    test fixture and not a shipped driver (`docs/spec/15-delivery.md` §15.2.9), and a
    factory that could select it from configuration is a factory that will select it in
    production one misconfigured environment variable later.

    Returns None rather than raising when `MEDOS_TRITON_URL` is unset, because a
    deployment running only deterministic capabilities legitimately has no Triton -- and
    `run_capability` raises `NativeBackendMissing` the moment that assumption is wrong,
    which is the point at which the operator actually needs to know.
    """
    from medos.inference.catalogue import BackendResolver

    resolver = BackendResolver.from_env()
    # The default route only. A caller wanting per-model routing passes the resolver itself
    # to `run_capability`; this function keeps the one-backend shape its callers were
    # written against and is retained for them.
    name = resolver.backend_name_for("*")
    return resolver.resolve("*") if name else None


def build_resolver() -> Any:
    """The deployment's per-model backend routing, read from the environment.

    Preferred over `build_backend()` for anything that serves more than one model: it is
    what makes OQ-10's decision real rather than declared, and OQ-10's own branch table
    gives the reason -- a mandated single runtime means "a hospital with a different
    accelerator or a CPU-only site cannot be served".
    """
    from medos.inference.catalogue import BackendResolver

    return BackendResolver.from_env()
