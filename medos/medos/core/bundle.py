# SPDX-License-Identifier: Apache-2.0
"""`ResultBundle` — the service return contract.

CONTRACT.md §5: "medos/medos/core/bundle.py owns these. Use these exact field names." The
dataclasses below are transcribed from that section verbatim; nothing here may be
renamed, reordered or extended without a contract change, because this is the ONLY
boundary at which a capability hands its answer back to the platform.

WHY it is a frozen dataclass tree and not a dict: chapter 2 makes the bundle the thing
the platform independently re-validates before it writes a SEG or an SR (MOS-IMG-141).
Re-validation against an untyped dict degrades into guessing which key meant what, and
a mutable bundle can be edited between validation and write — which would make the
validation a formality.

Three invariants that live in this module because nowhere else can enforce them:

  1. `Measurement` values MUST be computed in SOURCE geometry (CONTRACT.md §5,
     MOS-IMG-039). A measurement in model space is a defect, not an approximation:
     preprocessing resamples, and a volume measured on the resampled grid is wrong by
     the ratio of the voxel volumes while looking entirely plausible.
  2. `LabelMap.array` is on the SOURCE grid, `(n_slices, rows, cols)`, uint8. Index
     `i + 1` in the array is `segments[i]`; 0 is background. Off-by-one here relabels
     every segment in the written SEG.
  3. A capability MUST NOT touch the database, the network or the PACS (CONTRACT.md §6),
     so the bundle carries `source_sop_instance_uids` — the evidence the SR needs
     (MOS-IMG-121) — rather than expecting the writer to re-query for it.

No PHI: a bundle carries UIDs, codes and numbers. Never a name, an MRN or a date
(CONTRACT.md §11).

Spec: chapter 2 (service return contract), MOS-IMG-039, MOS-IMG-040, MOS-IMG-041,
MOS-IMG-111, MOS-IMG-112, MOS-IMG-121, MOS-IMG-141.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "CodedConcept",
    "Measurement",
    "Finding",
    "LabelMap",
    "CapabilityOutcome",
    "ResultBundle",
]


@dataclass(frozen=True)
class CodedConcept:
    """A clinical code, always from a named scheme (MOS-IMG-111).

    `scheme` is one of "SCT" | "DCM" | "RADLEX" | "99MEDOS". "99MEDOS" is the private
    scheme and its use is an admission that no standard term exists: MOS-IMG-114 requires
    each private code to be reviewed for promotion every minor release.

    MOS-IMG-112 forbids inventing a code, so instances of this class are resolved through
    `medos.core.concepts.ConceptDictionary`, never constructed from a literal in
    capability code.
    """

    scheme: str  # "SCT" | "DCM" | "RADLEX" | "99MEDOS"
    code: str
    meaning: str


@dataclass(frozen=True)
class Measurement:
    """One quantitative result, with its UCUM unit.

    MUST be computed in SOURCE geometry (chapter 4). Never in model space.

    `value` is the unrounded float (MOS-IMG-040): rounding belongs to presentation, and a
    value rounded at computation time can no longer be checked against the value
    recomputed from the stored SEG, which is the weeks 1-2 exit criterion
    (docs/spec/15-delivery.md §15.2.3).

    NOTE this is the RETURN-CONTRACT Measurement. The computation-side record, which also
    carries the qualifiers MOS-IMG-119 requires (convolution kernel, dS, threshold), is
    `medos.core.measure.Measurement`; convert with `measure.to_bundle_measurement`.
    """

    name: CodedConcept
    value: float
    unit: str  # UCUM, e.g. "ml", "%"
    # MUST be computed in SOURCE geometry (chapter 4). Never in model space.


@dataclass(frozen=True)
class Finding:
    """One assertion a capability makes about the study.

    `present` and `score` are separate because a capability that has not been given a
    model still has to answer honestly. CONTRACT.md §7 requires exactly that of
    `pleural_effusion` in this slice: `present=False` with a `not_implemented` note and
    `score=None`. `score=None` means "no calibrated score", which is not the same claim
    as `score=0.0`, and collapsing the two would let a placeholder read as a confident
    negative.
    """

    kind: str
    present: bool
    score: float | None
    measurements: tuple[Measurement, ...]


@dataclass(frozen=True)
class LabelMap:
    """A multi-label segmentation on the SOURCE grid.

    `array` is uint8 with 0 = background; value `i + 1` corresponds to `segments[i]`.
    The shape is the SOURCE grid `(n_slices, rows, cols)` — the same `(K, J, I)`
    ordering as `CanonicalVolume.array` (MOS-IMG-005) — because the inverse transform
    back to source space has already been applied by the time a bundle exists. A label
    map still in model space is the defect MOS-IMG-032/033 exist to prevent.
    """

    array: np.ndarray  # uint8, SOURCE grid shape (n_slices, rows, cols)
    segments: tuple[CodedConcept, ...]  # index i+1 in array == segments[i]


@dataclass(frozen=True)
class CapabilityOutcome:
    """Everything one capability produced for one job.

    `source_sop_instance_uids` are the instances actually CONSUMED. MOS-IMG-121 makes
    this an exclusion rule as well as an inclusion one: the SR evidence sequence "MUST
    NOT reference instances that were selected but dropped as duplicates; those appear
    only in provenance." The writer cannot reconstruct that distinction, so the
    capability states it.
    """

    capability_id: str
    findings: tuple[Finding, ...]
    label_map: LabelMap | None
    source_sop_instance_uids: tuple[str, ...]


@dataclass(frozen=True)
class ResultBundle:
    """What a job returns: one outcome per capability that ran.

    A tuple and not a dict keyed by `capability_id` because CONTRACT.md §7 makes ordering
    meaningful — `emphysema_laa` consumes `lung_segmentation`'s mask, and
    `worker/steps.py` expresses that as explicit step ordering. The bundle preserves the
    order the steps ran in.
    """

    outcomes: tuple[CapabilityOutcome, ...]
