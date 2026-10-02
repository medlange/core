# SPDX-License-Identifier: Apache-2.0
"""Measurements, computed in SOURCE geometry and nowhere else.

CONTRACT.md §1: "Measurement, measure_volume_ml, laa_percent". Lifted verbatim from
`spikes/week0/write_dicom_results.py`.

MOS-IMG-040: V_ml = N_voxels * dR * dC * dS / 1000, with dS the PROJECTED slice spacing
of MOS-IMG-016 -- never SliceThickness and never ||v||. MOS-IMG-028 spells out the
failure mode: using the inter-slice vector norm inflates every volume by 1/cos(theta)
under gantry tilt, and nothing downstream can detect it.

NOTE on the name `Measurement`. CONTRACT.md lists a `Measurement` in both this module
and §5 (`bundle.py`), and they are NOT the same type:

  * `medos.core.measure.Measurement` -- the computation-side record. Carries
    `concept_key`, `computation_geometry` and the qualifiers MOS-IMG-119 requires
    (kernel, dS, threshold), because %LAA "is not comparable across kernels or slice
    thicknesses".
  * `medos.core.bundle.Measurement` -- the SERVICE RETURN CONTRACT (CONTRACT.md §5),
    with a `CodedConcept` name and a UCUM unit.

CONTRACT.md §5 says bundle.py owns the return contract, so the §5 field list is
authoritative there and untouched. `to_bundle_measurement` converts, resolving
`concept_key` through the `ConceptDictionary` so no code is ever invented (MOS-IMG-112).

Spec: MOS-IMG-016, MOS-IMG-028, MOS-IMG-039, MOS-IMG-040, MOS-IMG-041, MOS-IMG-119.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from medos.core.geometry import SourceGeometry

if TYPE_CHECKING:  # pragma: no cover - import cycle would be runtime-only
    from medos.core.bundle import Measurement as BundleMeasurement
    from medos.core.concepts import ConceptDictionary

__all__ = [
    "Measurement",
    "measure_volume_ml",
    "measure_laa_percent",
    "laa_percent",
    "to_bundle_measurement",
]


@dataclass
class Measurement:
    """One NUM content item's worth of value, with the geometry it was computed in.

    `computation_geometry` is carried explicitly because MOS-IMG-039 makes the frame part
    of the measurement's meaning: "A measurement computed anywhere else is a defect, not
    an approximation."
    """

    concept_key: str
    value: float
    ucum_unit: str
    ucum_unit_meaning: str
    computation_geometry: str
    qualifiers: dict[str, Any] = field(default_factory=dict)


def measure_volume_ml(mask: np.ndarray, source: SourceGeometry) -> Measurement:
    """MOS-IMG-040: V_ml = N_voxels * dR * dC * dS / 1000.

    dS is the PROJECTED spacing from MOS-IMG-016, not SliceThickness and not ||v||;
    MOS-IMG-028 spells out that using ||v|| inflates every volume by 1/cos(theta) under
    gantry tilt. The stored value is the unrounded float64 (MOS-IMG-040).
    """
    if source.hu_array is None:
        raise ValueError(
            "SourceGeometry carries no hu_array (built by from_header_scan); "
            "MOS-IMG-039 requires the measurement on the source grid, so there is "
            "nothing to measure against"
        )
    if mask.shape != source.hu_array.shape:
        raise ValueError(
            f"mask shape {mask.shape} != source grid {source.hu_array.shape}; "
            "MOS-IMG-039 requires the measurement on the source grid"
        )
    d_r, d_c = source.pixel_spacing_mm
    n_voxels = int(np.count_nonzero(mask))
    value = float(n_voxels) * d_r * d_c * source.delta_s_mm / 1000.0
    return Measurement(
        concept_key="quantity.volume",
        value=value,
        ucum_unit="ml",
        ucum_unit_meaning="mL",
        computation_geometry="source",
        qualifiers={
            "n_voxels": n_voxels,
            "pixel_spacing_mm": [d_r, d_c],
            "delta_s_mm": source.delta_s_mm,
        },
    )


def measure_laa_percent(
    mask: np.ndarray, source: SourceGeometry, threshold_hu: float
) -> Measurement:
    """MOS-IMG-041: LAA% on source HU after Modality LUT and padding substitution.

    Carries the threshold, the source ConvolutionKernel and dS as qualifiers because
    MOS-IMG-119 requires them: %LAA "is not comparable across kernels or slice
    thicknesses".
    """
    if source.hu_array is None:
        raise ValueError(
            "SourceGeometry carries no hu_array (built by from_header_scan); "
            "MOS-IMG-041 requires LAA% on source HU"
        )
    n_mask = int(np.count_nonzero(mask))
    if n_mask == 0:
        raise ValueError("LAA% is undefined on an empty mask")
    below = int(np.count_nonzero(source.hu_array[mask] < np.float32(threshold_hu)))
    return Measurement(
        concept_key="quantity.laa_percent",
        value=100.0 * below / n_mask,
        ucum_unit="%",
        ucum_unit_meaning="%",
        computation_geometry="source",
        qualifiers={
            "threshold_hu": threshold_hu,
            "convolution_kernel": source.convolution_kernel,
            "delta_s_mm": source.delta_s_mm,
            "n_voxels_below": below,
            "n_voxels_mask": n_mask,
        },
    )


# CONTRACT.md §1 names this function `laa_percent`; the spike named it
# `measure_laa_percent` and spikes/week0 is frozen. One function, two names -- the
# contract name is canonical, the spike name is the compatibility alias.
laa_percent = measure_laa_percent


def to_bundle_measurement(
    measurement: Measurement, concepts: ConceptDictionary
) -> BundleMeasurement:
    """Convert a computation-side Measurement into the CONTRACT.md §5 return type.

    The coded concept is resolved through the dictionary rather than constructed here,
    because MOS-IMG-112 forbids inventing a code and MOS-IMG-113 forbids materialising
    the code list in Python. A `concept_key` with no row raises.

    Refuses any measurement not computed in source geometry: CONTRACT.md §5 states the
    invariant directly ("MUST be computed in SOURCE geometry. Never in model space") and
    MOS-IMG-039 calls the alternative a defect rather than an approximation.
    """
    from medos.core.bundle import CodedConcept
    from medos.core.bundle import Measurement as BundleMeasurement

    if measurement.computation_geometry != "source":
        raise ValueError(
            f"measurement {measurement.concept_key!r} was computed in "
            f"{measurement.computation_geometry!r} geometry; CONTRACT.md §5 and "
            "MOS-IMG-039 require source geometry"
        )
    row = concepts[measurement.concept_key]
    return BundleMeasurement(
        name=CodedConcept(
            scheme=str(row["coding_scheme"]),
            code=str(row["code_value"]),
            meaning=str(row["code_meaning"]),
        ),
        value=float(measurement.value),
        unit=measurement.ucum_unit,
    )
