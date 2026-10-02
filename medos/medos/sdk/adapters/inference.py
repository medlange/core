# SPDX-License-Identifier: Apache-2.0
"""The inference adapter: where a model actually runs.

`InferenceAdapter` is the seam between the SDK's pipeline and a model. The card says
WHAT to run (spec, weights, frameworks); the adapter says WHERE: a KServe v2 / Triton
server in another container, an embedded function in this process, or anything else a
site runs.

`EmbeddedInference` exists for tests, development and deterministic algorithms: the
"model" is a callable this process provides. `KServeV2Inference` is the shipped driver
for a model served over KServe v2 (Triton speaks it), reusing the platform's own driver
rather than growing a second one.

`ModelOutput` is what every adapter returns: the raw arrays the network emitted plus the
named metrics the card's outputs descriptor will turn into measurements. It is
deliberately plain -- adapters do not interpret; interpretation is the postprocessor's
job (`medos.sdk.postprocess`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from medos.sdk.adapters import DriverMissing

if TYPE_CHECKING:
    import numpy as np

    from medos.sdk.modelcard import ModelCard
    from medos.sdk.preprocess import Chain, ChainInput

__all__ = [
    "ModelOutput",
    "InferenceAdapter",
    "EmbeddedInference",
    "KServeV2Inference",
]


@dataclass(frozen=True)
class ModelOutput:
    """What the network emitted, uninterpreted.

    `arrays` holds channel-first maps as they came off the network (a label map under
    "pred" for a segmentation model); `metrics` holds named scalars (percentages, counts,
    confidences) the postprocessor lifts into measurements by name.
    """

    arrays: Mapping[str, np.ndarray] = field(default_factory=dict)
    metrics: Mapping[str, float] = field(default_factory=dict)


class InferenceAdapter(Protocol):
    """Run the card's model against one preprocessed input."""

    def infer(
        self,
        card: ModelCard,
        chain: Chain,
        prepared: ChainInput,
    ) -> ModelOutput:
        """`prepared` is the card's chain applied to the canonical volume.

        Applying the chain is the ADAPTER's job, not the pipeline's: a server-side model
        may prefer to run the transforms in its own runtime, an embedded model may want
        the arrays as-is. The pipeline hands over the chain and the adapter decides how
        to materialise it.
        """
        ...


@dataclass
class EmbeddedInference:
    """An in-process model: a callable `(arrays, spacing) -> ModelOutput`.

    The callable receives the chain-APPLIED arrays (the adapter runs
    `medos.sdk.preprocess.apply_chain` first -- it is pure numpy and ships with the SDK),
    so an embedded model sees exactly what a served model would.
    """

    run: Callable[[Mapping[str, np.ndarray], tuple[float, float, float]], ModelOutput]

    def infer(
        self,
        card: ModelCard,
        chain: Chain,
        prepared: ChainInput,
    ) -> ModelOutput:
        from medos.sdk.preprocess import apply_chain  # noqa: PLC0415

        arrays = apply_chain(chain, prepared)
        spacing = tuple(prepared.spacing_mm)
        return self.run(arrays, spacing)  # type: ignore[arg-type]


@dataclass
class KServeV2Inference:
    """`InferenceAdapter` over a KServe v2 server (Triton speaks the protocol).

    Construction imports the platform's driver lazily; `model_version` is pinned to the
    card's, because an unresolvable pin is a deployment defect, not a fallback
    (`MOS-OPS-083`'s shape, one layer down).
    """

    url: str = "http://127.0.0.1:8000"
    timeout_s: float = 300.0
    model_version: str | None = None

    def __post_init__(self) -> None:
        try:
            from medos.inference.triton import (  # noqa: PLC0415
                TritonBackend,
                TritonConfig,
            )
        except ImportError as exc:  # pragma: no cover - exercised by integration
            raise DriverMissing(
                "KServeV2Inference needs the platform's KServe v2 driver, which imports "
                "`requests`. Install the SDK with its server dependencies "
                "(`pip install medos[server]`) or pass an adapter whose transport you "
                "already carry."
            ) from exc
        self._backend: Any = TritonBackend(
            TritonConfig(url=self.url, timeout_s=self.timeout_s)
        )

    def infer(
        self,
        card: ModelCard,
        chain: Chain,
        prepared: ChainInput,
    ) -> ModelOutput:
        import numpy as np  # noqa: PLC0415

        from medos.sdk.preprocess import apply_chain  # noqa: PLC0415

        arrays = apply_chain(chain, prepared)
        tensor = np.stack([np.asarray(a, dtype=np.float32) for a in arrays.values()])
        result = self._backend.infer(
            tensor,
            card.model_id,
            self.model_version or card.model_version,
        )
        return ModelOutput(
            arrays={"pred": np.asarray(result)},
            metrics={},
        )
