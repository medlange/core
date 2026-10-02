# SPDX-License-Identifier: Apache-2.0
"""A synthetic, PHI-free cohort on disk, for measuring the trainer end to end.

WHY A SYNTHETIC COHORT AND NOT THE SEALED ONE
-----------------------------------------------
The pixels of a cohort sealed on this deployment are UNREACHABLE, by a control that is
working exactly as specified. `MOS-TRAIN-068` and `MOS-TRAIN-199` permit the training
pipeline to acquire imaging only from the de-identified side of the Gateway, as the
`dataset_export` consumer class; `medos/medos/gateway/app.py` answers `503
DEID_NOT_IMPLEMENTED` for that class because the de-identification stage does not exist,
and `MOS-DATA-037` requires the egress to fail closed rather than emit identified data.
`medos/medos/training/retrieval.py` states the same thing at length and calls the alternative --
borrowing the worker's `platform_writer` credential -- "the worst combination available".

So a test that wanted real pixels would have to defeat that control. This module supplies
something else instead: volumes with no patient behind them at all. `MOS-TRAIN-058`'s
argument for the golden fixture applies unchanged -- "It MUST NOT come from `clinical`-class
ingest under any circumstances" -- and `medos.sdk.fixtures.phantom` is already the
platform's own deterministic phantom, built from integer HU values with no RNG.

WHAT IT DOES AND DOES NOT MEASURE
-----------------------------------
It measures the TRAINER: that the planner derives a fingerprint from the fit partition,
that the exporter transcribes it, that a network fits on the GPU, that a MONAI Bundle
comes out and that `medos.sdk.bundle.verify` reads it. It measures NOTHING about
segmentation quality, and a Dice on phantoms is not a claim about lungs.

The label is derived from the HU values the phantom was built from, so the task is
learnable in a handful of iterations -- which is the point: the measurement is of the
machinery, and a task the network cannot move on would confound "the trainer is broken"
with "three epochs is not enough".

Spec: MOS-TRAIN-058, MOS-TRAIN-068, MOS-TRAIN-141, MOS-TRAIN-199, MOS-EVID-021.
Runs where numpy and nibabel are importable -- the trainer image, or a host that has both.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SHAPE: tuple[int, int, int] = (48, 72, 72)
FIT_CASES = 8
SELECT_CASES = 3


def case_keys(total: int = FIT_CASES + SELECT_CASES) -> list[str]:
    """The case identifiers, which are the ONLY name written into a path.

    `MOS-SEC-033` -- never log a PHI value -- applies to filenames as much as to log
    lines, and nnU-Net prints every case name it loads. A `case_key` is an opaque
    identifier of a case (`MOS-EVID-010`); no accession number, name, MRN or study
    description appears anywhere in this layout.
    """
    return [f"case_{index:03d}" for index in range(total)]


def volume_and_label(index: int) -> tuple[Any, Any]:
    """One phantom with a deterministic per-case jitter, and its lung mask.

    The jitter is a pure function of `index` -- no RNG, for the reason
    `medos.sdk.fixtures.phantom` gives: "a seed would make the fixture depend on
    numpy's generator version". It exists because nnU-Net's fingerprint reads intensity
    percentiles ACROSS the cohort, and eight copies of one volume is a cohort with no
    variance for the planner to read.
    """
    import numpy as np
    from medos.sdk.fixtures import phantom

    array = phantom(SHAPE)[0]
    shift = float(20 * ((index % 5) - 2))
    array = np.roll(array + np.float32(shift), index % 3, axis=1).astype(np.float32)

    label = np.zeros(array.shape, dtype=np.uint8)
    interior = (array < np.float32(-500.0 + shift)) & _interior(
        array > np.float32(-900.0 + shift)
    )
    mid = array.shape[2] // 2
    # The values are `selftest_spec_document()`'s `io.label_set`: 1 lung_right, 2
    # lung_left. The spec's label set is authoritative (`MOS-TRAIN-136`), not nnU-Net's.
    label[..., :mid][interior[..., :mid]] = 1
    label[..., mid:][interior[..., mid:]] = 2
    return array, label


def _interior(body: Any) -> Any:
    """Everything between the first and last body voxel of each row. Crude and enough."""
    import numpy as np

    out = np.zeros_like(body)
    for k in range(body.shape[0]):
        for j in range(body.shape[1]):
            row = np.flatnonzero(body[k, j])
            if row.size:
                out[k, j, row[0]: row[-1] + 1] = True
    return out


def write_export(root: str | Path) -> list[str]:
    """`<root>/<case_key>/{image,label}.nii.gz` -- the `directory` stager's layout."""
    import nibabel as nib
    import numpy as np
    from medos.sdk.fixtures import PHANTOM_SPACING_MM

    base = Path(root)
    affine = np.diag([*PHANTOM_SPACING_MM, 1.0]).astype(np.float64)
    keys = case_keys()
    for index, key in enumerate(keys):
        volume, label = volume_and_label(index)
        target = base / key
        target.mkdir(parents=True, exist_ok=True)
        nib.save(nib.Nifti1Image(volume, affine), str(target / "image.nii.gz"))
        nib.save(nib.Nifti1Image(label, affine), str(target / "label.nii.gz"))
    (base / "manifest.json").write_text(
        json.dumps({
            "provenance": "synthetic_phantom",
            "generator": "tests/_support/phantom_cohort.py",
            "patient_behind_these_images": None,
            "fit_cases": FIT_CASES,
            "select_cases": SELECT_CASES,
            "case_keys": keys,
        }, indent=2),
        encoding="utf-8",
    )
    return keys


if __name__ == "__main__":  # pragma: no cover - run inside the trainer image
    print(json.dumps(write_export(sys.argv[1])))
