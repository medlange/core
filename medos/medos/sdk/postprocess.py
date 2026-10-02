# SPDX-License-Identifier: Apache-2.0
"""The declarative postprocessor: model output in, promised findings out.

Every model has its own outputs -- one segments lobes, another reports emphysema
percentages, a third does both. The card's `outputs` descriptor is the per-model
postprocessing contract: what segments exist (and which value of the label map each
claims) and which named metrics become measurements. `run_postprocess` interprets that
descriptor against the adapter's `ModelOutput`; it invents NOTHING the card did not
declare.

This is v1 and it is deliberately declarative: no arbitrary code from the card, only
the two descriptor kinds the SDK understands. A model whose outputs do not fit the two
kinds extends the descriptor vocabulary here, in one reviewed place -- never by loading
code out of a model directory.

Spec: the SDK pivot (per-model postprocessing is a first-class SDK operation).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from medos.sdk import errors

if TYPE_CHECKING:
    import numpy as np

    from medos.sdk.adapters.inference import ModelOutput
    from medos.sdk.modelcard import ModelCard

__all__ = [
    "Segment",
    "Measurement",
    "PostprocessResult",
    "run_postprocess",
]


class PostprocessError(errors.TrainingError):
    """A descriptor names an array or metric the model output does not carry."""


@dataclass(frozen=True)
class Segment:
    """One claimed segment: the value in the label map and the name the card gave it."""

    value: int
    name: str
    mask: np.ndarray  # boolean, same grid as the model's output


@dataclass(frozen=True)
class Measurement:
    """One named scalar lifted from the model's metrics."""

    name: str
    value: float
    unit: str = ""


@dataclass(frozen=True)
class PostprocessResult:
    """What the card promised, materialised. The writer's input (SEG/SR), one layer up."""

    segments: tuple[Segment, ...] = ()
    measurements: tuple[Measurement, ...] = ()
    metrics: Mapping[str, float] = field(default_factory=dict)


def run_postprocess(card: ModelCard, output: ModelOutput) -> PostprocessResult:
    """Interpret `card.outputs` against the model's raw output.

    Descriptor kinds:
      `{"kind": "segmentation", "value": int, "name": str}` -- boolean mask cut from the
        "pred" label map at that value.
      `{"kind": "measurement", "name": str, "unit": str?, "from": str}` -- the named
        metric copied out of `output.metrics`.
    Anything else is a vocabulary the SDK does not speak: refused, not guessed.
    """
    segments: list[Segment] = []
    measurements: list[Measurement] = []
    for descriptor in card.outputs:
        kind = descriptor.get("kind")
        if kind == "segmentation":
            try:
                value = int(descriptor["value"])
                name = str(descriptor["name"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PostprocessError(
                    f"segmentation descriptor is missing value or name: {descriptor!r}"
                ) from exc
            label_map = output.arrays.get("pred")
            if label_map is None:
                raise PostprocessError(
                    f"descriptor {name!r} cuts a segment from 'pred', which the model "
                    f"output does not carry"
                )
            segments.append(
                Segment(value=value, name=name, mask=(label_map == value))
            )
        elif kind == "measurement":
            source = descriptor.get("from") or descriptor.get("name")
            if not isinstance(source, str) or source not in output.metrics:
                raise PostprocessError(
                    f"measurement descriptor names {source!r}, which the model output "
                    f"does not carry in metrics"
                )
            measurements.append(
                Measurement(
                    name=str(descriptor.get("name") or source),
                    value=float(output.metrics[source]),
                    unit=str(descriptor.get("unit") or ""),
                )
            )
        else:
            raise PostprocessError(
                f"unknown outputs descriptor kind {kind!r}; v1 speaks 'segmentation' "
                f"and 'measurement' and refuses the rest rather than guessing"
            )
    return PostprocessResult(
        segments=tuple(segments),
        measurements=tuple(measurements),
        metrics=dict(output.metrics),
    )
