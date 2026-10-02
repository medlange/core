# SPDX-License-Identifier: Apache-2.0
"""The detector: two HU thresholds, a connected-component labelling, and five filters.

IT IS NOT A LEARNED MODEL AND EVERY SURFACE SAYS SO
----------------------------------------------------
There are no weights, no training set, no score and no operating point. `service.py`'s
`METADATA.method_class` is `deterministic_algorithm`, every `Finding.score` is `None`, and
the SR finding type is a private code whose MEANING contains the words "not clinically
validated". The rule this follows is the one that made `pleural_effusion` return
`present=False` instead of a plausible number: a fabricated probability is worse than no
probability, because nothing downstream can tell it from a measured one.

`score=None` and never `0.0`. `MOS-SVC-020` makes `score_threshold` the name of a
CALIBRATED operating point; `0.0` would assert a calibrated confidence of zero, which is a
different and false claim from "no score exists". `MOS-SVC-021` then forbids displaying a
sensitivity for this capability anywhere, because there is no threshold to report it at.

THE METHOD, EXACTLY
-------------------
Everything runs on the SOURCE HU array (`MOS-IMG-039`/`MOS-IMG-041`: a measurement computed
anywhere else "is a defect, not an approximation"). No resampling, no smoothing.

  1. AIR PHASE. `hu <= AIR_HU_MAX`. 6-connected labelling.
  2. LUNG FIELD. Of those components keep the ones that (a) touch no IN-PLANE face --
     room air surrounds the patient and always touches one, the lungs never do; (b) do not
     touch BOTH k faces -- a structure spanning the whole craniocaudal extent of a thoracic
     scan is the table or an immobilisation pad, not lung; and (c) are at least
     `MIN_LUNG_COMPONENT_ML`. (a) and (b) are `medos.core.masks.mask_from_hu_threshold`'s
     measured lessons, applied here rather than imported, for the reason in `_lung_field`.
  3. CROP. Take the lung field's bounding box, grown by `CROP_MARGIN_VOX`. Steps 4-5 run
     inside it. This is what makes the method affordable: it turns a 100-megavoxel
     labelling into a ~10-megavoxel one, and the crop face becomes the "reaches the outside
     world" test that step 4 needs.
  4. ENCLOSED STRUCTURES. Label `hu >= CANDIDATE_HU_MIN` inside the crop and DISCARD every
     component that touches a crop face. A SECOND, much higher threshold than step 1's, for
     the reason set out at `CANDIDATE_HU_MIN`: `~air` is not the candidate phase, it is the
     partial-volume shell around the whole vascular tree, and using it measures nothing.
     What is left is soft tissue completely surrounded by air. The chest wall, the
     mediastinum, the heart and the whole central vascular tree are one
     connected mass that reaches the crop boundary, so all of them are discarded in this
     one step -- and so, unavoidably, is every lesion touching them (`LN-FM-002`).
  5. ADJACENCY. Keep only components with a 6-neighbour in the lung field. A blob enclosed
     by air somewhere other than the lungs -- bowel gas in a low scan, a gap in the
     immobilisation foam -- is not a lung nodule candidate.
  6. FILTERS. Five, each with a reason, applied in this order (cheapest first, so a study
     with a thousand components does a thousand size comparisons and only a handful of
     `np.nonzero` extractions):
       * SIZE, as an equivalent spherical diameter in `[MIN_DIAMETER_MM, MAX_DIAMETER_MM]`.
         Below the floor a component is noise or a vessel branch at the resolution limit;
         above the ceiling it is a mass, and a mass is not what this reports.
       * SLICE SPAN `>= MIN_SLICE_SPAN`. A 4 mm lesion cannot live in one slice, and a
         one-slice component defeats the sphericity index (see there).
       * ASPECT RATIO of the PHYSICAL bounding box, `<= MAX_ASPECT_RATIO`. A vessel is a
         tube.
       * SPHERICITY, volume over the ellipsoid inscribed in the physical bounding box,
         `>= MIN_SPHERICITY`. A perfect sphere scores 1.0; a branching or tubular structure
         scores far below.
       * MEAN HU in `[MIN_MEAN_HU, MAX_MEAN_HU]`. Soft tissue and calcification; excludes
         a partial-volume shell of near-air voxels.
  7. PLAUSIBILITY GATE (`MOS-SAFE-014` `output_plausibility_gate`). More than
     `MAX_PLAUSIBLE_CANDIDATES` survivors is a broken threshold, not a patient, so the
     capability REJECTS rather than reporting a three-figure count.

WHAT THE METHOD CANNOT DO, STATED WHERE THE CODE IS
-----------------------------------------------------
Step 4 is hole-finding. It is the reason this is affordable and correct about what it does
find, and it is also `LN-FM-002`: a juxtapleural or juxtavascular lesion is contiguous with
the body, reaches the crop face, and is dropped SILENTLY -- no output of a run makes it
visible. Step 6's shape filters cannot separate a nodule from a vessel seen end-on
(`LN-FM-001`): both are compact, round and soft-tissue dense. Neither is a bug to be fixed
by tuning; they are properties of the method, and `medos/examples/lung-nodule/capability.json`
declares them in `known_failure_modes` so the registry carries them too.

OWNERSHIP (chapter 2 section 2.7)
----------------------------------
This module imports numpy and ONE pure array helper from the platform ABI. It opens no
socket, holds no credential, touches no database, consumes no bus, reads no file and writes
no DICOM. `MOS-SVC-038`'s import scan is asserted against this package by
`tests/integration/test_lung_nodule.py::test_service_plane_imports_nothing_it_must_not`.

Spec: MOS-IMG-039, MOS-IMG-040, MOS-IMG-041, MOS-SVC-021, MOS-SVC-038, MOS-SVC-042,
MOS-SVC-059, MOS-SAFE-012, MOS-SAFE-014.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# The platform's 6-connected run-length/union-find labeller. Imported and not reimplemented:
# `MOS-REL-032` names a hand-maintained duplicate of platform logic a defect, and a second
# labeller would be exactly that. It is spelled with a leading underscore because
# `medos/medos/core/masks.py` exports only the three mask builders that use it -- REPORTED as an
# ABI gap (the platform offers the service plane no public connected-components primitive)
# rather than worked around; `medos.capabilities.lung_segmentation` imports it the same way
# and documents the same choice.
from medos.core.masks import _connected_components_3d

__all__ = [
    "Candidate",
    "DetectionResult",
    "detect",
    "AIR_HU_MAX",
    "CANDIDATE_HU_MIN",
    "MIN_LUNG_COMPONENT_ML",
    "CROP_MARGIN_VOX",
    "MIN_DIAMETER_MM",
    "MAX_DIAMETER_MM",
    "MIN_SLICE_SPAN",
    "MAX_ASPECT_RATIO",
    "MIN_SPHERICITY",
    "MIN_MEAN_HU",
    "MAX_MEAN_HU",
    "MAX_PLAUSIBLE_CANDIDATES",
    "PARAMETERS",
]


# =====================================================================================
# Parameters. Every number the output depends on, in one block.
# =====================================================================================
# `MOS-REG-047` makes the computation parameters of a deterministic capability part of the
# Capability row and requires them to be versioned like any other clinically load-bearing
# value. `PARAMETERS` below is the projection that reaches `METADATA.parameters`, the
# provenance record and `result_measurements.method_detail`, so a stored candidate count can
# always be re-derived. Changing any of them changes the output and is a version bump.

#: Upper bound of the AIR phase, used only to find the lung field. Matches
#: `lung_segmentation.LUNG_HU_MAX` deliberately: two capabilities that disagree about where
#: lung ends would outline different lungs on the same study, and a reader comparing their
#: SEGs has no way to know which moved.
AIR_HU_MAX = -400.0

#: Lower bound of a CANDIDATE. A SECOND and much higher threshold, and the separation is the
#: single most important number in this file.
#:
#: MEASURED, on LIDC-IDRI-0001 and -0002. Taking `hu > AIR_HU_MAX` as the candidate phase --
#: the obvious one-threshold design -- yields 1113 enclosed components whose mean HU runs
#: -220 to -320 and whose sphericity is 0.03 to 0.32. Those are not lesions: they are the
#: partial-volume TRANSITION SHELLS between air and vessel, and at -400 HU they form sprawling
#: connected sheets through the whole vascular tree. Not one of them is a nodule, and every
#: shape filter downstream was measuring sampling noise.
#:
#: At -200 HU the shells vanish and what remains has soft-tissue density. The cost is stated
#: rather than hidden: a lesion whose density lies below -200 HU -- which is most ground-glass
#: and sub-solid nodules -- is INVISIBLE to this method, and that is the class where detection
#: matters most. `not_validated_for` in the Capability row says so in those words.
CANDIDATE_HU_MIN = -200.0

#: A lung is far larger than this; the floor exists to drop the trachea stump and small
#: pockets of trapped air that survive the face tests.
MIN_LUNG_COMPONENT_ML = 50.0

#: Voxels of slack around the lung bounding box before step 4's labelling. Enough that a
#: peripheral structure is not clipped by the crop itself; small enough that the chest wall
#: still reaches the crop face and is discarded.
CROP_MARGIN_VOX = 4

#: Fleischner's actionable range, floored where this method stops being able to see. Below
#: 4 mm a component is a handful of voxels at the spacings this capability admits.
MIN_DIAMETER_MM = 4.0
MAX_DIAMETER_MM = 30.0

#: Slices a candidate must span. A `MIN_DIAMETER_MM` lesion covers 1.3 slices at the
#: envelope's coarsest admitted spacing (4.0 / 3.0 mm) and 1.6 at the 2.5 mm that most of
#: LIDC-IDRI is acquired at, so a component confined to ONE slice cannot be one -- it is a
#: vessel cut transversely, or noise.
#:
#: The interaction with the envelope is stated rather than left to be discovered: at the
#: coarse end a 4 mm lesion spans two slices only when it straddles a slice boundary, so
#: near dS = 3 mm this filter costs real sensitivity at the smallest reported size. That is
#: a cost of coarse sampling, not a tuning error, and it is why the envelope stops at 3 mm.
#:
#: This is also what makes the sphericity index below trustworthy. A single-slice component
#: has a craniocaudal extent of exactly one voxel, which shrinks the reference ellipsoid and
#: hands it a spuriously HIGH score: the top two components on LIDC-IDRI-0001 scored 0.75 and
#: 0.51 while spanning one slice with an aspect ratio of 5.3 and 3.4. Both are flat slivers.
MIN_SLICE_SPAN = 2

#: Bounding-box longest-over-shortest, in MILLIMETRES and not in voxels. A vessel is a tube
#: and loses here.
MAX_ASPECT_RATIO = 2.5

#: Compactness: the component's volume over the volume of the ellipsoid inscribed in its
#: PHYSICAL bounding box, `V / ((pi/6) * eS * eR * eC)`. A solid sphere scores 1.0 by
#: construction; a branching or tubular structure scores far below.
#:
#: This replaced a bounding-BOX fill ratio, which was unusable and quietly so. Voxels over
#: box-voxels penalises anisotropy rather than shape: at 2.5 x 0.703 x 0.703 mm a PERFECT
#: 5.5 mm sphere spans 2.2 slices, which rounds up to 3, inflating the box by a third and
#: scoring it 0.36 -- a hair above the 0.35 floor that was meant to admit it. The threshold
#: was unreachable in practice and the filter rejected everything.
#:
#: 0.35 here is read off the measured separation, not guessed: on the two cases above the
#: compact components score 0.44 to 0.51 and the branching ones 0.03 to 0.11. Nothing
#: observed sits between 0.11 and 0.44.
MIN_SPHERICITY = 0.35

#: Mean HU of the component. Soft tissue through calcification. The floor sits at the
#: candidate threshold, so a component cannot pass on a handful of dense voxels dragged up by
#: a long low-density tail.
MIN_MEAN_HU = -200.0
MAX_MEAN_HU = 900.0

#: `MOS-SAFE-014` `output_plausibility_gate`. A chest CT does not have this many discrete
#: nodules; a run that produces more has a broken threshold, and reporting the number would
#: be reporting the bug as a measurement.
MAX_PLAUSIBLE_CANDIDATES = 60

PARAMETERS: dict[str, float | int | str] = {
    "method": "hu_threshold_connected_components",
    "air_hu_max": AIR_HU_MAX,
    "candidate_hu_min": CANDIDATE_HU_MIN,
    "min_lung_component_ml": MIN_LUNG_COMPONENT_ML,
    "crop_margin_vox": CROP_MARGIN_VOX,
    "min_diameter_mm": MIN_DIAMETER_MM,
    "max_diameter_mm": MAX_DIAMETER_MM,
    "min_slice_span": MIN_SLICE_SPAN,
    "max_aspect_ratio": MAX_ASPECT_RATIO,
    "min_sphericity": MIN_SPHERICITY,
    "min_mean_hu": MIN_MEAN_HU,
    "max_mean_hu": MAX_MEAN_HU,
    "max_plausible_candidates": MAX_PLAUSIBLE_CANDIDATES,
    "computed_on": "source_grid",
    "learned_model": "no",
}


# =====================================================================================
# Results
# =====================================================================================
@dataclass(frozen=True)
class Candidate:
    """One accepted connected component.

    `volume_ml` is `MOS-IMG-040`'s formula on the source grid.
    `equivalent_diameter_mm` is `(6V/pi)^(1/3)` and is NOT the long axis: see the
    `private_code_rationale` on `quantity.nodule_equivalent_diameter`, which is the reason
    it travels under a private code rather than a DICOM long-axis one.
    """

    index: int
    n_voxels: int
    volume_ml: float
    equivalent_diameter_mm: float
    aspect_ratio: float
    sphericity: float
    mean_hu: float
    centroid_index: tuple[float, float, float]


@dataclass(frozen=True)
class DetectionResult:
    """Accepted candidates, their union mask, and the audit trail of what was rejected.

    `mask` is on the SOURCE grid and is the SEG that gets written. `rejected` carries a
    count per filter so a run that finds nothing can say WHICH filter emptied it -- without
    that, "no candidates" and "the envelope was wrong" look identical from the outside.

    `lung_field` is carried out rather than recomputed by the caller. `service.py` draws it
    as the SEG's search-region segment, and a second call to `_lung_field` would be a
    second definition of "the lung field" in one service -- the kind of duplicate
    `MOS-REL-032` names a defect. It is a view of the same array the detection used.
    """

    mask: np.ndarray
    lung_field: np.ndarray
    candidates: tuple[Candidate, ...]
    lung_field_ml: float
    n_enclosed_components: int
    rejected: dict[str, int]
    implausible: bool


def _lung_field(hu: np.ndarray, voxel_ml: float) -> np.ndarray:
    """Step 1-2. The air phase, minus room air, minus the table.

    Written here rather than calling `medos.core.masks.mask_from_hu_threshold` for two
    reasons, both behavioural. That function raises `SystemExit` when nothing survives --
    correct for the spike CLI it was lifted from, wrong inside a capability, where the
    honest outcome is a `CapabilityRejection` the worker turns into a job-level `REJECTED`
    (chapter 5 T7). And it applies a RELATIVE size floor tuned for lung segmentation, which
    here would delete a small lung field and leave the detector measuring one side.
    `MOS-SVC-007` points the same way: a service's pipeline lives inside its own
    ServiceVersion.
    """
    air = hu <= np.float32(AIR_HU_MAX)
    labels, n_labels = _connected_components_3d(air)
    if n_labels == 0:
        return np.zeros(hu.shape, dtype=bool)

    counts = np.bincount(labels.ravel(), minlength=n_labels + 1)

    # Room air reaches an in-plane face on every slice; the lungs never do.
    touches_in_plane = np.zeros(n_labels + 1, dtype=bool)
    for face in (labels[:, 0, :], labels[:, -1, :], labels[:, :, 0], labels[:, :, -1]):
        touches_in_plane[np.unique(face)] = True

    # The k faces are the SCAN EXTENT, not an anatomy boundary -- the trachea reaches the
    # most superior slice -- so touching one is not disqualifying. Touching BOTH means the
    # component spans the whole acquisition, which a thoracic lung does not.
    touches_first_k = np.zeros(n_labels + 1, dtype=bool)
    touches_last_k = np.zeros(n_labels + 1, dtype=bool)
    touches_first_k[np.unique(labels[0, :, :])] = True
    touches_last_k[np.unique(labels[-1, :, :])] = True

    keep = np.zeros(n_labels + 1, dtype=bool)
    for lab in range(1, n_labels + 1):
        if touches_in_plane[lab]:
            continue
        if touches_first_k[lab] and touches_last_k[lab]:
            continue
        if float(counts[lab]) * voxel_ml < MIN_LUNG_COMPONENT_ML:
            continue
        keep[lab] = True
    keep[0] = False
    return keep[labels]


def _bbox(mask: np.ndarray) -> tuple[slice, slice, slice]:
    """Bounding box of a non-empty boolean mask, grown by `CROP_MARGIN_VOX`."""
    idx = [np.flatnonzero(mask.any(axis=tuple(a for a in range(3) if a != ax)))
           for ax in range(3)]
    out: list[slice] = []
    for ax in range(3):
        lo = max(0, int(idx[ax][0]) - CROP_MARGIN_VOX)
        hi = min(mask.shape[ax], int(idx[ax][-1]) + 1 + CROP_MARGIN_VOX)
        out.append(slice(lo, hi))
    return out[0], out[1], out[2]


def _touches_crop_face(labels: np.ndarray, n_labels: int) -> np.ndarray:
    """Which labels reach any face of the cropped sub-volume."""
    touches = np.zeros(n_labels + 1, dtype=bool)
    for face in (
        labels[0, :, :], labels[-1, :, :],
        labels[:, 0, :], labels[:, -1, :],
        labels[:, :, 0], labels[:, :, -1],
    ):
        touches[np.unique(face)] = True
    return touches


def _adjacent_labels(labels: np.ndarray, neighbour: np.ndarray) -> set[int]:
    """Labels with at least one 6-neighbour inside `neighbour`.

    Six shifted comparisons rather than a dilation: a dilation of a 10-megavoxel mask
    allocates another one, and this needs only the SET of touching labels.
    """
    found: set[int] = set()
    for axis in range(3):
        for shift in (1, -1):
            # `np.roll` already returns a fresh array, so this writes into a copy nobody
            # else holds -- no second allocation needed.
            rolled = np.roll(neighbour, shift, axis=axis)
            # `np.roll` WRAPS: without blanking the wrapped plane, the far face of the crop
            # is reported as adjacent to the near one, and a component at the top of the
            # crop would be "next to" lung at the bottom.
            sl: list[slice | int] = [slice(None)] * 3
            sl[axis] = 0 if shift == 1 else -1
            rolled[tuple(sl)] = False
            found.update(np.unique(labels[rolled]).tolist())
    found.discard(0)
    return found


def detect(hu: np.ndarray, spacing_mm: tuple[float, float, float]) -> DetectionResult:
    """Run the method on a SOURCE HU array.

    `spacing_mm` is `(dS, dR, dC)` -- the projected slice spacing of `MOS-IMG-016` first,
    matching `CanonicalVolume.spacing_mm`. `hu` is treated as read-only (`MOS-SVC-042`).
    """
    d_s, d_r, d_c = (float(v) for v in spacing_mm)
    voxel_mm3 = abs(d_s) * d_r * d_c
    voxel_ml = voxel_mm3 / 1000.0

    rejected = {
        "size": 0, "slice_span": 0, "aspect_ratio": 0, "sphericity": 0, "mean_hu": 0,
        "not_in_lung": 0,
    }
    empty = np.zeros(hu.shape, dtype=bool)

    lung = _lung_field(hu, voxel_ml)
    lung_field_ml = float(np.count_nonzero(lung)) * voxel_ml
    if not lung.any():
        return DetectionResult(
            mask=empty, lung_field=empty, candidates=(), lung_field_ml=0.0,
            n_enclosed_components=0, rejected=rejected, implausible=False,
        )

    box = _bbox(lung)
    hu_crop = hu[box]
    lung_crop = lung[box]

    # Step 4: soft tissue completely enclosed by air. `CANDIDATE_HU_MIN`, never
    # `> AIR_HU_MAX` -- see that constant for the measurement that forced the distinction.
    solid = hu_crop >= np.float32(CANDIDATE_HU_MIN)
    labels, n_labels = _connected_components_3d(solid)
    if n_labels == 0:
        return DetectionResult(
            mask=empty, lung_field=lung, candidates=(), lung_field_ml=lung_field_ml,
            n_enclosed_components=0, rejected=rejected, implausible=False,
        )

    open_to_outside = _touches_crop_face(labels, n_labels)
    enclosed = [lab for lab in range(1, n_labels + 1) if not open_to_outside[lab]]
    if not enclosed:
        return DetectionResult(
            mask=empty, lung_field=lung, candidates=(), lung_field_ml=lung_field_ml,
            n_enclosed_components=0, rejected=rejected, implausible=False,
        )

    # Step 5: inside the lungs and not somewhere else that happens to be surrounded by air.
    near_lung = _adjacent_labels(labels, lung_crop)

    counts = np.bincount(labels.ravel(), minlength=n_labels + 1)
    sums = np.bincount(labels.ravel(), weights=hu_crop.astype(np.float64).ravel(),
                       minlength=n_labels + 1)

    accepted: list[Candidate] = []
    accepted_labels: list[int] = []
    for lab in enclosed:
        if lab not in near_lung:
            rejected["not_in_lung"] += 1
            continue

        n_vox = int(counts[lab])
        volume_ml = float(n_vox) * voxel_ml
        # (6V/pi)^(1/3) with V in mm^3.
        d_eq = float((6.0 * n_vox * voxel_mm3 / np.pi) ** (1.0 / 3.0))
        if not (MIN_DIAMETER_MM <= d_eq <= MAX_DIAMETER_MM):
            rejected["size"] += 1
            continue

        where = np.nonzero(labels == lab)
        extents_vox = [int(a.max() - a.min()) + 1 for a in where]
        if extents_vox[0] < MIN_SLICE_SPAN:
            rejected["slice_span"] += 1
            continue

        extents_mm = [
            extents_vox[0] * abs(d_s), extents_vox[1] * d_r, extents_vox[2] * d_c,
        ]
        aspect = float(max(extents_mm) / min(extents_mm)) if min(extents_mm) > 0 else 1e9
        if aspect > MAX_ASPECT_RATIO:
            rejected["aspect_ratio"] += 1
            continue

        # Volume over the ellipsoid inscribed in the PHYSICAL bounding box. Isotropic by
        # construction, so it measures shape and not the sampling grid.
        ellipsoid_mm3 = (np.pi / 6.0) * extents_mm[0] * extents_mm[1] * extents_mm[2]
        sphericity = (
            float(n_vox * voxel_mm3 / ellipsoid_mm3) if ellipsoid_mm3 > 0 else 0.0
        )
        if sphericity < MIN_SPHERICITY:
            rejected["sphericity"] += 1
            continue

        mean_hu = float(sums[lab] / n_vox)
        if not (MIN_MEAN_HU <= mean_hu <= MAX_MEAN_HU):
            rejected["mean_hu"] += 1
            continue

        # Centroid in SOURCE index space, offset back out of the crop.
        centroid = (
            float(where[0].mean()) + box[0].start,
            float(where[1].mean()) + box[1].start,
            float(where[2].mean()) + box[2].start,
        )
        accepted_labels.append(lab)
        accepted.append(
            Candidate(
                index=len(accepted),
                n_voxels=n_vox,
                volume_ml=volume_ml,
                equivalent_diameter_mm=d_eq,
                aspect_ratio=aspect,
                sphericity=sphericity,
                mean_hu=mean_hu,
                centroid_index=centroid,
            )
        )

    # Largest first, so a reader and the SR see the biggest candidate as group 1.
    order = sorted(range(len(accepted)), key=lambda i: -accepted[i].volume_ml)
    accepted = [
        Candidate(
            index=rank,
            n_voxels=accepted[i].n_voxels,
            volume_ml=accepted[i].volume_ml,
            equivalent_diameter_mm=accepted[i].equivalent_diameter_mm,
            aspect_ratio=accepted[i].aspect_ratio,
            sphericity=accepted[i].sphericity,
            mean_hu=accepted[i].mean_hu,
            centroid_index=accepted[i].centroid_index,
        )
        for rank, i in enumerate(order)
    ]
    accepted_labels = [accepted_labels[i] for i in order]

    mask = np.zeros(hu.shape, dtype=bool)
    if accepted_labels:
        keep = np.zeros(n_labels + 1, dtype=bool)
        keep[np.asarray(accepted_labels, dtype=np.int64)] = True
        mask[box] = keep[labels]

    return DetectionResult(
        mask=mask,
        lung_field=lung,
        candidates=tuple(accepted),
        lung_field_ml=lung_field_ml,
        n_enclosed_components=len(enclosed),
        rejected=rejected,
        implausible=len(accepted) > MAX_PLAUSIBLE_CANDIDATES,
    )
