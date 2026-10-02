# SPDX-License-Identifier: Apache-2.0
"""The inference adapter catalogue: every backend this image ships, and which model uses it.

OQ-10 IS DECIDED, AND THIS FILE IS WHAT THE DECISION COST
----------------------------------------------------------
`docs/spec/16-open-questions.md` OQ-10 / `MOS-OPEN-031` asked whether the inference runtime
is declared replaceable the way the PACS is. It is, as of the decision recorded in the
`OQ-10 resolved` section of `docs/adr/BUILD_VS_ADOPT.md`: `InferenceBackend` is a settled
port, Triton is one adapter, and the domain layer holds no Triton-specific logic.

What existed before this file was `dispatch.build_backend()`: one environment variable,
one hardcoded branch, and ONE BACKEND FOR THE WHOLE WORKER. A deployment could not serve
one model on Triton and another on a CPU runtime, which is the concrete thing
replaceability is for -- OQ-10's own branch table says a mandated Triton means "a hospital
with a different accelerator or a CPU-only site cannot be served".

WHY A STATIC MAPPING AND NOT `importlib`
------------------------------------------
`MOS-REL-108` is unqualified: "MedicalOS MUST NOT offer an in-process plugin API -- no
shared-library loading, NO DYNAMIC MODULE IMPORT, no user-supplied code executed inside a
platform process", and `MOS-CONF-109` leans on that clause as the IEC 62304 section 4.3
segregation argument that lets a publisher classify platform items at a lower safety class
-- "a segregation argument a reviewer can execute rather than read".

A backend registry that resolved a name out of configuration through `importlib` would be
exactly the mechanism that argument denies exists. This repository has already made that
mistake once and recorded it as entry 68 of `docs/spec/99-known-inconsistencies.md`, and
already fixed it once: `medos/services/catalogue.py` is a static mapping of selector name to
module, one ordinary top-level import per shipped provider, and
`medos/medos/capabilities/providers.py` does a dictionary lookup that refuses an unknown key by
listing the known ones.

THIS FILE IS THE SAME SHAPE FOR THE SAME REASON. The complete set of inference code a
configured deployment can run is the set of `import` statements below, fixed when the image
was built and readable with `grep import`. An operator SELECTS among shipped adapters. They
cannot introduce one; that is a line added here, a rebuild, a code review and a supply
chain, which is the whole difference between configuration and a plugin API.

WHAT IS NOT DECIDED HERE
-------------------------
Whether a given model CAN run on a given backend. The artifact manifest declares
`supported_runtimes`; selecting a backend an artifact does not declare must be refused when
the configuration is read, not discovered when a patient's study is already in flight.
`resolve()` reports the selection and the reason; it does not validate the artifact, which
belongs to the registry.

Spec: OQ-10 / MOS-OPEN-031, MOS-REL-108, MOS-CONF-109, MOS-OPS-008, MOS-SVC-058.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from medos.inference.backend import InferenceBackend

__all__ = [
    "BACKENDS",
    "BackendNotShipped",
    "BackendResolver",
    "backend_names",
    "build_named",
]


class BackendNotShipped(RuntimeError):
    """A deployment named a backend this image does not contain.

    Raised at CONFIGURATION time rather than at inference time, and it lists what the image
    does ship. The alternative -- discovering the name is unknown when a study is already
    being processed -- turns an operator's typo into a failed clinical job.
    """


def _triton() -> InferenceBackend | None:
    """Driver 1. KServe v2 over HTTP, hand-written over `requests`.

    `tritonclient` is deliberately not a dependency: it pulls a large transitive set for a
    protocol this codebase already speaks, and `medos/medos/inference/triton.py` says so.
    """
    url = os.environ.get("MEDOS_TRITON_URL")
    if not url:
        return None
    from medos.inference.triton import TritonBackend, TritonConfig

    return TritonBackend(TritonConfig.from_env())


def _onnxruntime() -> InferenceBackend | None:
    """ONNX Runtime Server, over the same KServe v2 protocol Triton serves."""
    from medos.inference.servers import build_onnxruntime

    return build_onnxruntime()


def _openvino() -> InferenceBackend | None:
    """OpenVINO Model Server -- the CPU and Intel-accelerator answer, which is the
    deployment OQ-10's branch table names when it says a mandated Triton means a
    CPU-only site cannot be served."""
    from medos.inference.servers import build_openvino

    return build_openvino()


#: Selector name -> a factory returning a configured backend, or None when this deployment
#: has not configured it.
#:
#: ONE ROW PER SHIPPED ADAPTER. The key is what an operator writes in
#: `MEDOS_INFERENCE_BACKEND` or in a per-model override; it is lower-case and contains no
#: comma or whitespace, because the parser below splits on both and a key containing either
#: could be shipped and never selectable.
#:
#: The in-process CPU loader (`medos/medos/inference/inprocess.py`) is ABSENT ON PURPOSE.
#: `docs/spec/15-delivery.md` section 15.2.9 labels it a test fixture and not a shipped
#: driver, and a catalogue that could select it from configuration is a catalogue that will
#: select it in production one misconfigured environment variable later. That was already
#: the stated reason `build_backend` had no branch for it, and widening the mechanism must
#: not quietly widen that.
BACKENDS: Mapping[str, Callable[[], InferenceBackend | None]] = MappingProxyType(
    {
        "triton": _triton,
        "onnxruntime": _onnxruntime,
        "openvino": _openvino,
    }
)


def backend_names() -> tuple[str, ...]:
    """What this image ships, for an error message that helps rather than scolds."""
    return tuple(sorted(BACKENDS))


def build_named(name: str) -> InferenceBackend | None:
    """Construct one shipped backend by selector name.

    A dictionary lookup. There is no import machinery here, and an unknown name is a
    refusal that names the known ones rather than an import attempt -- see the module
    docstring for why that distinction is load-bearing rather than stylistic.
    """
    key = name.strip().lower()
    factory = BACKENDS.get(key)
    if factory is None:
        raise BackendNotShipped(
            f"{name!r} is not an inference backend this image ships. "
            f"It ships: {', '.join(backend_names())}. A backend is added by an import in "
            f"medos/medos/inference/catalogue.py and a rebuild, never by configuration "
            f"(MOS-REL-108)."
        )
    return factory()


class BackendResolver:
    """Which backend serves which model.

    `MEDOS_INFERENCE_BACKEND` names the default. `MEDOS_INFERENCE_BACKEND_MAP` overrides it
    per model, as `model_id=backend` pairs separated by commas or whitespace:

        MEDOS_INFERENCE_BACKEND=triton
        MEDOS_INFERENCE_BACKEND_MAP=lung_segmentation=triton, nodule_detect=onnxruntime

    BACKENDS ARE BUILT ONCE AND REUSED. Each adapter holds a connection pool, so
    constructing one per inference would open a socket per study. The cache is keyed by
    selector name, not by model, because two models on one backend share one client.

    WHY THE KEY IS `model_id` AND NOT `(model_id, model_version)`. A version is the same
    model fitted differently; serving two versions of one model on two different runtimes
    is a comparison, not a deployment, and `MOS-OPEN-031` already says a backend conversion
    mints a NEW `ModelVersion` with its own `EvaluationRun`. So version-level routing would
    invite exactly the un-evaluated cross-runtime substitution that decision forbids.
    """

    def __init__(
        self,
        default: str | None = None,
        overrides: Mapping[str, str] | None = None,
    ) -> None:
        self._default = (default or "").strip().lower() or None
        self._overrides = {k: v.strip().lower() for k, v in (overrides or {}).items()}
        self._cache: dict[str, InferenceBackend | None] = {}

    @staticmethod
    def from_env() -> BackendResolver:
        """Read the deployment's selection.

        `MEDOS_INFERENCE_BACKEND` is absent in every deployment written before this file,
        and those deployments set `MEDOS_TRITON_URL`. Defaulting to `triton` when the URL
        is set keeps them working unchanged, which is the migration this file owes them --
        stated here rather than left as a surprise.
        """
        default = os.environ.get("MEDOS_INFERENCE_BACKEND", "").strip().lower()
        if not default and os.environ.get("MEDOS_TRITON_URL"):
            default = "triton"

        overrides: dict[str, str] = {}
        raw = os.environ.get("MEDOS_INFERENCE_BACKEND_MAP", "")
        for pair in raw.replace(",", " ").split():
            model, sep, backend = pair.partition("=")
            if not sep or not model or not backend:
                raise BackendNotShipped(
                    f"MEDOS_INFERENCE_BACKEND_MAP entry {pair!r} is not `model_id=backend`"
                )
            overrides[model] = backend
        return BackendResolver(default or None, overrides)

    def backend_name_for(self, model_id: str) -> str | None:
        return self._overrides.get(model_id, self._default)

    def resolve(self, model_id: str) -> InferenceBackend | None:
        """The backend serving this model, or None when this deployment configured none.

        None rather than raising, matching the contract `build_backend` already had: a
        deployment running only deterministic capabilities legitimately has no inference
        backend, and `run_capability` raises `NativeBackendMissing` at the moment that
        assumption is actually wrong -- which is when the operator needs to know.
        """
        name = self.backend_name_for(model_id)
        if not name:
            return None
        if name not in self._cache:
            self._cache[name] = build_named(name)
        return self._cache[name]

    def selections(self) -> dict[str, str | None]:
        """What this resolver would do, for a startup log line and for `medos doctor`.

        A deployment that cannot see its own routing discovers it one failed job at a time.
        """
        out: dict[str, str | None] = {"*": self._default}
        out.update(self._overrides)
        return out
