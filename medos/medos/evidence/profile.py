# SPDX-License-Identifier: Apache-2.0
"""`acquisition_profile` -- the cohort summary every sealed version carries.

MOS-EVID-024  "Every sealed DatasetVersion MUST carry a computed `acquisition_profile`
              summarising the cohort. This object is the sole permitted input to
              automatic envelope derivation (7.10.2)."
MOS-EVID-025  "Percentiles MUST be computed with the linear interpolation method
              (`numpy.quantile` default) over the SERIES population, and the patient-level
              counts MUST be reported separately as shown. Mixing the two is a reporting
              defect."
MOS-TRAIN-093 the corpus stratification report "MUST be computed from `acquisition_profile`
              alone and MUST be recomputable from the sealed manifest".

The last of those three is what fixes this object's shape. C1 asks for the max share of
PATIENTS from one `institution_key`; `MOS-EVID-024`'s example carries only series counts
under `categorical`. Reporting a patient share out of a series histogram is precisely the
mixing `MOS-EVID-025` calls a reporting defect, so this module emits a separate
`patients` block whose values are DISTINCT PATIENT COUNTS, and the stratification check
reads C1, C6 and C7 from there and C2, C3 from `categorical`. REPORTED as an addition to
the chapter-7 object.

Two other supersets of the published example, both additive and both load-bearing:

  * every numeric block carries `p10` and `p90` as well as `min/p01/p50/p99/max`,
    because C4 is stated as a "p90/p10 ratio" and cannot be computed from the five
    percentiles the example shows;
  * every numeric block carries `distinct`, the count of distinct non-null values,
    because C4's other half is "distinct `slice_thickness_mm` values".

`categorical.scanner_model` is the `(manufacturer, manufacturer_model_name)` pair C2 is
stated over, joined with a unit separator. A pair cannot be a JSON object key and two
separate histograms cannot answer a question about the pair -- a cohort with two
manufacturers and two models is not necessarily a cohort with four scanners.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

import numpy as np

from medos.evidence.manifest import SeriesRecord

__all__ = [
    "KERNEL_CLASS_MAP_VERSION",
    "acquisition_profile",
    "SCANNER_SEPARATOR",
]

# MOS-EVID-020: "`convolution_kernel_class` is the only derived field; its mapping table
# (vendor kernel string -> {sharp, standard, soft, unknown}) MUST be versioned and its
# version recorded in `acquisition_profile.kernel_class_map_version`." The map itself is
# the retrieval boundary's (it reads the vendor string off the header); this package
# records the version it was told, and this constant is the default for callers that have
# not yet pinned one. Bumping it is a cohort-identity change and MUST produce new sealed
# versions rather than a silent re-interpretation of existing ones.
KERNEL_CLASS_MAP_VERSION = 1

# A unit separator: legal in neither a DICOM Manufacturer nor a ManufacturerModelName, so
# the joined key is unambiguously decomposable.
SCANNER_SEPARATOR = "\x1f"

_NUMERIC_PERCENTILES = (1, 10, 50, 90, 99)


def _numeric_block(values: Sequence[float]) -> dict[str, Any] | None:
    """`{min,p01,p10,p50,p90,p99,max,distinct,n}` over the SERIES population.

    Returns None when every series is missing the value. MOS-EVID-020 forbids imputation,
    so a cohort in which nothing carries `kvp` reports no `kvp` block rather than a block
    of zeros -- an absent measurement and a measurement of zero are different facts and a
    downstream envelope derived from the second would be wrong in a way nobody can see.
    """
    finite = [float(v) for v in values if v is not None]
    if not finite:
        return None
    arr = np.asarray(finite, dtype=float)
    # MOS-EVID-025: numpy.quantile's DEFAULT method, which is linear interpolation.
    q = np.quantile(arr, [p / 100.0 for p in _NUMERIC_PERCENTILES])
    return {
        "n": int(arr.size),
        "min": float(arr.min()),
        "p01": float(q[0]),
        "p10": float(q[1]),
        "p50": float(q[2]),
        "p90": float(q[3]),
        "p99": float(q[4]),
        "max": float(arr.max()),
        "distinct": int(len({round(v, 6) for v in finite})),
    }


def _counts(values: Sequence[Any]) -> dict[str, int]:
    """Series-level histogram. A `None` is dropped, never bucketed as 'unknown'."""
    return dict(sorted(Counter(str(v) for v in values if v is not None).items()))


def _patient_counts(pairs: Sequence[tuple[str, Any]]) -> dict[str, int]:
    """DISTINCT PATIENT counts per value. MOS-EVID-025's "reported separately".

    A patient whose two series carry two different values counts once under each. That is
    the honest reading for C1 ("share of patients from one institution_key") on a cohort
    where a patient was scanned at two sites, and it is why the shares in this block may
    sum to more than 1.0 -- which the check accounts for by comparing each value's share
    against the bound rather than by normalising.
    """
    by_value: dict[str, set[str]] = {}
    for patient, value in pairs:
        if value is None:
            continue
        by_value.setdefault(str(value), set()).add(patient)
    return {k: len(v) for k, v in sorted(by_value.items())}


def acquisition_profile(
    records: Sequence[SeriesRecord],
    *,
    kernel_class_map_version: int = KERNEL_CLASS_MAP_VERSION,
) -> dict[str, Any]:
    """MOS-EVID-024's object, computed from the manifest records and nothing else.

    "Computed from the manifest records and nothing else" is not a stylistic preference:
    `MOS-TRAIN-093` requires the stratification report to be recomputable offline from the
    sealed manifest, and it is computed from this object, so this object must be a pure
    function of the manifest. A profile that reached into the imaging projection for one
    value would make every downstream check unverifiable outside this deployment.
    """
    if not records:
        raise ValueError(
            "an empty cohort has no acquisition_profile; MOS-EVID-016 requires a "
            "materialised list of instances"
        )

    acq = [r.acquisition for r in records]
    patients = {r.patient_key for r in records}
    lossy = sum(1 for r in records if r.lossy_compressed)

    # `pixel_spacing_mm_max` is the example's name for the larger of the two in-plane
    # spacings -- the one that bounds achievable detail, and the one an envelope bound is
    # written against.
    spacing_max = [
        max(a.pixel_spacing_mm) if a.pixel_spacing_mm else None for a in acq
    ]

    numeric_inputs: dict[str, list[Any]] = {
        "slice_thickness_mm": [a.slice_thickness_mm for a in acq],
        "pixel_spacing_mm_max": spacing_max,
        "z_coverage_mm": [a.z_coverage_mm for a in acq],
        "instance_count": [r.instance_count for r in records],
        "patient_age_years": [a.patient_age_years for a in acq],
        "kvp": [a.kvp for a in acq],
    }
    numeric = {
        name: block
        for name, values in numeric_inputs.items()
        if (block := _numeric_block(values)) is not None
    }

    scanner = [
        None
        if a.manufacturer is None and a.manufacturer_model_name is None
        else f"{a.manufacturer or ''}{SCANNER_SEPARATOR}{a.manufacturer_model_name or ''}"
        for a in acq
    ]

    categorical = {
        "manufacturer": _counts([a.manufacturer for a in acq]),
        "manufacturer_model_name": _counts([a.manufacturer_model_name for a in acq]),
        "scanner_model": _counts(scanner),
        "convolution_kernel_class": _counts([a.convolution_kernel_class for a in acq]),
        "contrast_phase": _counts([a.contrast_phase for a in acq]),
        "patient_sex": _counts([a.patient_sex for a in acq]),
        "body_part_examined": _counts([a.body_part_examined for a in acq]),
        "modality": _counts([r.modality for r in records]),
        "institution_key": _counts([r.institution_key for r in records]),
        "study_year": _counts([r.study_year for r in records]),
    }

    patient_level = {
        "institution_key": _patient_counts(
            [(r.patient_key, r.institution_key) for r in records]
        ),
        "study_year": _patient_counts([(r.patient_key, r.study_year) for r in records]),
        "patient_sex": _patient_counts(
            [(r.patient_key, a.patient_sex) for r, a in zip(records, acq)]
        ),
        "scanner_model": _patient_counts(
            [(r.patient_key, s) for r, s in zip(records, scanner)]
        ),
        "convolution_kernel_class": _patient_counts(
            [(r.patient_key, a.convolution_kernel_class) for r, a in zip(records, acq)]
        ),
    }

    return {
        "n_series": len(records),
        "n_patients": len(patients),
        "n_studies": len({r.study_instance_uid for r in records}),
        "kernel_class_map_version": int(kernel_class_map_version),
        # MOS-EVID-019: recorded on the version, surfaced in the report, and the reason a
        # cross-site digest comparison over a lossy cohort is not meaningful.
        "lossy_series_fraction": lossy / len(records),
        "numeric": numeric,
        "categorical": categorical,
        # The addition this module documents: patient-level counts, kept apart from the
        # series-level ones because MOS-EVID-025 calls mixing them a reporting defect.
        "patients": patient_level,
    }
