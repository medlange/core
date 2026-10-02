# SPDX-License-Identifier: Apache-2.0
"""`lung_segmentation` -- an HU threshold with connected-component filtering.

IT IS NOT A LEARNED MODEL, AND IT SAYS SO EVERYWHERE
    CONTRACT.md §7: "not a learned model; label it honestly". That obligation is met in
    four places, because a label in only one of them is a label a UI can drop:

      * `METADATA.method_class == "deterministic_algorithm"` and `method_summary` names
        the actual method, so chapter 9's MOS-SAFE-012 surfaces cannot render it as "AI".
      * There is no `operating_point` and every `Finding.score` is `None`. There is no
        score to calibrate, and `score=0.0` would read as a confident one.
      * The segments are coded `SCT:3341006 Right lung` / `SCT:44029006 Left lung` --
        anatomical structures. No `finding.*` code and no morphology code is emitted,
        because a threshold cannot assert that what it outlined is normal lung.
      * `METADATA.not_validated_for` and `known_failure_modes` are populated from measured
        behaviour on real data, not from imagination. See `KNOWN_FAILURE_MODES`.

METHOD, EXACTLY
    1. Threshold the SOURCE HU array (never a resampled one, MOS-IMG-041) at
       `LUNG_HU_MIN <= HU <= LUNG_HU_MAX`, label it 6-connected, and discard every
       component that reaches an IN-PLANE face of the volume. Room air surrounds the
       patient and therefore always touches a face; the lungs never do. The k faces are
       the scan extent, not an anatomical boundary, so they are deliberately not tested
       -- the trachea reaches the most superior slice. Then keep every remaining
       component at or above `MIN_COMPONENT_ML`. This step is
       `medos.core.masks.mask_from_hu_threshold`, lifted from the spike (CONTRACT.md §2,
       §7), called with `min_component_fraction=THRESHOLD_STAGE_FRACTION` so that the
       RELATIVE floor is deferred to step 2 -- see `THRESHOLD_STAGE_FRACTION` for the
       case that forced it. The lifted body is unmodified; only an argument differs.
    2. Apply the SCAN-EXTENT filter, then the relative size floor
       `max(MIN_COMPONENT_ML, MIN_COMPONENT_FRACTION * largest_survivor)` -- in that
       order, so the floor is measured against a lung and not against a table.
    3. Plausibility-gate the total volume (MOS-SAFE-014 `output_plausibility_gate`).
    4. Split the mask into right and left lung at a single sagittal index, located as the
       deepest valley of the column profile, and REJECT if there is no such valley.

STEP 2, THE SCAN-EXTENT FILTER, AND WHY IT HAD TO BE ADDED
    Step 1 alone fails on the LCTSC S2 cohort. Measured, on the real data:

        case              RTSTRUCT lungs   step 1 only            + step 2
        LCTSC-Test-S2-101    3556.2 ml     7373.9 ml (Dice 0.549)  3073.0 ml (Dice 0.905)
        LCTSC-Test-S2-102    5099.4 ml     9607.8 ml (Dice 0.653)  4960.4 ml (Dice 0.954)

    The extra 4.3 L on S2-101 is the low-density immobilisation pad under the patient: an
    interior air-phase component that does not touch an in-plane face and is LARGER than
    the lungs. Reporting %LAA over a foam pad is precisely the plausible-looking wrong
    number this slice must not produce.

    The rule: **discard an interior component that touches BOTH k faces**. The lungs are
    bounded inferiorly by the diaphragm, which a thoracic acquisition contains; a
    structure spanning the entire craniocaudal extent of the scan is table or
    immobilisation hardware. If NOTHING survives the rule, the capability REJECTS rather
    than falling back -- see step 3.

    A pad that big also poisons the RELATIVE SIZE FLOOR, which is why the floor moved
    here from step 1. `mask_from_hu_threshold` computes it as `0.2 * largest_interior
    component`, and when the largest interior component is the pad, that floor deletes
    real lung. MEASURED on LCTSC-Test-S3-103: pad 7474.3 ml sets the floor at 1494.9 ml,
    which deleted the 447.3 ml LEFT LUNG outright. The capability then reported the right
    lung alone -- 1543.3 ml of "both lungs", which it proceeded to cut down the middle --
    and every gate passed it. Applying the floor against the largest SURVIVOR instead
    yields 1990.5 ml across two components, which is the anatomy. The two orderings are
    exactly equivalent whenever the largest interior component is itself a lung.

    Measured on the same cases, the four S1 cases are bit-identical with and without both
    changes: they alter only the cases they were written for.

STEP 3, AND THE CASE THAT MADE IT A REJECTION
    LCTSC-Test-S3-102 fails step 1 in the other direction: above the lung apices the body
    narrows at the shoulders, so the apical lung air is k-adjacent to room air and the
    whole lung merges into the room-air component, which step 1 then discards for touching
    an in-plane face. What is left is table, immobilisation hardware and scraps.

    An earlier version of this module fell back to the unfiltered mask when step 2 would
    empty it, on the principle that an auxiliary heuristic must not turn a usable answer
    into a rejection. On S3-102 that fallback reported the couch as a 6185 ml lung, with
    Dice 0.000 against the RTSTRUCT contours -- and the absurdity gate passed it, because
    6185 ml is a perfectly plausible lung volume. That is the exact failure this slice
    exists to avoid, so the fallback is gone.

    Two gates now catch this family, and BOTH are needed:
      * the scan-extent filter, when every retained component spans the scan extent;
      * `PLAUSIBLE_TOTAL_ML`, when what survives is scraps. This is the one that fires on
        this cohort, and it only fires correctly because its floor is 1500 ml. Both
        earlier values leaked: at 250 ml, LCTSC-Test-S3-102 (307.9 ml, Dice 0.000) and
        S3-203 (604.7 ml, 0.252) were reported as results; at 1000 ml,
        LCTSC-Train-S3-002 (1151.0 ml, Dice 0.314) still was.

    Outcome over all 60 LCTSC cases (24 Test + 36 Train):

        45  produced a result checkable against an RTSTRUCT -- and EVERY ONE of them
            agrees with it, Dice 0.905 to 0.982. No wrong answer survives the gates.
         2  produced a result on a case shipping no matching RTSTRUCT, so unverified
            -- LCTSC-Test-S3-101 and S3-103
        12  rejected by a capability gate
         1  rejected by series selection before any capability runs -- LCTSC-Test-S3-201
            holds no CT series

    The separation the floor sits in, measured over those 60 cases:

        agreeing with the contours       totals 2102.8 .. 7426.4 ml   (n=45)
        the lung leaked (Dice <= 0.314)  totals   68.1 .. 1151.0 ml   (n=5)

    Every capability rejection REPLACES a wrong answer. What the gate does NOT catch is
    a partial leak leaving more than 1.5 L of scraps; that is LS-FM-008, declared and not
    mitigated, because detecting it needs a body mask this method does not build.

THE OBVIOUS FIX FOR THE 12 REJECTIONS WAS TRIED, MEASURED, AND NOT ADOPTED
    12 of 60 rejections is a lot, and the textbook remedy for the leak of LS-FM-005 is to
    remove room air SLICE BY SLICE instead of in 3D: label the air-phase mask 4-connected
    within each axial slice, drop every 2D component touching the slice border, and only
    then run steps 1-4. A 3D leak through the trachea and larynx cannot survive it,
    because in any single axial slice the lung cross-section is not 4-connected to the
    image border. It was implemented and run against all 12 rejections. It is NOT in this
    module, and the measurement is why:

        recovered, agreeing with the contours   9 of 12   Dice 0.922 .. 0.972,
                                                          volume ratio 0.919 .. 1.017
        still rejected                          1 of 12   LCTSC-Train-S2-003
        SILENTLY WRONG, and inside the gate     2 of 12

    The two are the whole argument:

        case                  reported    contoured   ratio   Dice
        LCTSC-Train-S3-002     7085.5 ml   2819.2 ml  2.513  0.383
        LCTSC-Test-S3-203      3359.2 ml   2482.5 ml  1.353  0.681

    Both clear `PLAUSIBLE_TOTAL_ML`, so the change would trade 9 rejections for 2 wrong
    numbers that no gate in this module detects. Per-component HU statistics do not
    separate them either, which was the obvious next move and is also measured: on
    LCTSC-Train-S3-002 the false 5082.6 ml component has mean -857.3 HU with 4.4 % of its
    voxels below -950, against -763.9 HU and 1.8 % for the TRUE whole lung of
    LCTSC-Test-S1-101. The immobilisation and vacuum-bag interiors that survive the
    per-slice pass are foam and trapped air at parenchymal densities, not pure air, so
    "is this component parenchyma" is not answerable from its histogram.

    Conclusion, and it is the same one LS-FM-005 and LS-FM-008 reach from the other side:
    what is missing is a BODY MASK -- a bound on the lung field relative to the patient
    rather than to the image. Until this method builds one, a rejection is the correct
    output and 12 of 60 is the honest price.

STEP 4, LATERALITY, AND HOW GOOD IT ACTUALLY IS
    At 6-connectivity and 2.5-3 mm slices the two lungs are usually ONE component -- they
    join through the trachea and carina -- so laterality cannot come from connectivity
    alone, and a single sagittal cut is used. The cut is the DEEPEST VALLEY of the
    column-wise voxel-count profile, located by prominence (`_deepest_valley`), which is
    the mediastinum. `lateral_separation` in the diagnostics records whether any component
    actually straddled it.

    The cut is NOT located by splitting the profile about its centroid and taking a peak
    either side. That was the previous rule and it is wrong whenever the centroid falls
    inside a lung, which happens as soon as one lung is materially larger than the other.
    MEASURED on LCTSC-Test-S3-101 (right lung 3.4x the left): the centroid landed at
    column 219, inside the right lung, the "second peak" it found was that same lung's
    shoulder, and the cut was placed at column 219 where the profile still holds 5542
    voxels. The true valley is at column 274 and holds 71. The capability reported
    1451.99 / 679.90 ml where the correct split is 1649.22 / 482.67 ml, and both per-lung
    %LAA values were measuring the wrong voxels. Nothing detected it: the total was
    right, the gate passed, and S3-101 ships no RTSTRUCT to check against.

    If the deepest valley is not deep enough -- `MIN_VALLEY_PROMINENCE_RATIO` -- there is
    no plane that separates a right lung from a left one, and the capability REJECTS
    rather than reporting the two halves of an arbitrary cut as two lungs.

    Measured against the RTSTRUCT `Lung_L` / `Lung_R` contours over the 45 of 60 LCTSC
    cases that both produce a result and ship a matching RTSTRUCT:

        Dice, whole lung   min 0.905   median 0.955   max 0.982   mean 0.953
        Dice, right lung   min 0.901   median 0.954   max 0.981   mean 0.949
        Dice, left lung    min 0.907   median 0.955   max 0.982   mean 0.954
        volume / RTSTRUCT  min 0.864   median 0.965   max 1.054   mean 0.962

    The per-side figures are barely below the whole-lung figure, which is the useful
    result: the sagittal cut costs about half a Dice point, and the other five points are
    the threshold itself (LS-FM-003). The systematic 4 % volume deficit is the same
    effect -- vessels and airway walls are above -400 HU and the RTSTRUCT contours include
    them.

COMPUTATION GRID
    Everything is on the SOURCE grid (MOS-IMG-039, MOS-IMG-041, CONTRACT.md §5). The
    canonical volume is used for applicability only. `LabelMap.array` is uint8 on
    `(n_slices, rows, cols)`, value `i + 1` == `segments[i]`.

Spec: MOS-IMG-016, MOS-IMG-028, MOS-IMG-039, MOS-IMG-040, MOS-IMG-041, MOS-IMG-111,
MOS-IMG-112, MOS-IMG-121, MOS-SAFE-012, MOS-SAFE-014, MOS-SAFE-015, MOS-EXEC-001.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from medos.capabilities.base import (
    CapabilityContext,
    CapabilityMetadata,
    CapabilityRejection,
    FailureMode,
    coded,
    load_concepts,
    require_source_grid,
)
from medos.core.bundle import CapabilityOutcome, Finding, LabelMap
from medos.core.concepts import ConceptDictionary
from medos.core.geometry import CanonicalVolume

# `medos.core.masks` spells its component labeller `_connected_components_3d`, but
# CONTRACT.md §1 lists "components" among the names masks.py owns, so this IS the
# contract's public entry point under a private name. Importing it is deliberate and is
# reported as a contract friction point: the alternative -- a second run-length union-find
# in this file -- would recreate exactly the duplicate definition CONTRACT.md §2 exists to
# remove, and the two copies would drift on the first change to connectivity.
from medos.core.masks import _connected_components_3d, mask_from_hu_threshold
from medos.core.measure import measure_volume_ml, to_bundle_measurement

__all__ = [
    "LungSegmentation",
    "LungMasks",
    "METADATA",
    "CAPABILITY_ID",
    "VERSION",
    "LUNG_HU_MIN",
    "LUNG_HU_MAX",
    "MIN_COMPONENT_ML",
    "MIN_COMPONENT_FRACTION",
    "PLAUSIBLE_TOTAL_ML",
    "SEGMENT_CONCEPT_KEYS",
    "segment_lungs",
]

CAPABILITY_ID = "lung_segmentation"
# 0.2.0, not 0.1.0. The method CHANGED: the relative size floor moved after the
# scan-extent filter, the laterality cut moved from a centroid split to a prominence
# valley, and the absurdity floor moved from 250 ml to 1000 ml. On LCTSC-Test that alters
# the output of 4 of 24 cases -- 2 volumes, 2 new rejections -- so the same series no
# longer yields the same numbers. MOS-IMG-085 makes UID derivation and provenance a
# function of the service version; leaving it at 0.1.0 would make two different results
# indistinguishable in the record, which is the one thing a provenance chain may not do.
VERSION = "0.2.0"

# --------------------------------------------------------------------------------------
# Parameters. Every one of these is the week-0 spike's value, recorded in
# `tests/_recorded/S1-101_threshold_dryrun.json` (which was `spikes/week0/out/` until the
# spike was deleted and its recordings moved to the tests that read them). They are
# module constants and not
# defaults buried in a signature because MOS-IMG-119 makes them part of the meaning of
# every number this capability produces: a volume from a -400 HU threshold is not the same
# quantity as a volume from a -500 HU one.
# --------------------------------------------------------------------------------------
LUNG_HU_MIN = -1024.0
LUNG_HU_MAX = -400.0
MIN_COMPONENT_ML = 50.0
MIN_COMPONENT_FRACTION = 0.2

# The relative floor above is applied by THIS module, AFTER the scan-extent filter, and
# `mask_from_hu_threshold` is therefore called with a relative fraction of zero.
#
# MEASURED, on LCTSC-Test-S3-103. The lifted function computes its floor as
# `0.2 * largest_interior_component`, and on that case the largest interior component is
# the 7474 ml immobilisation pad, not a lung. The floor lands at 1494.9 ml, which deletes
# the 447.3 ml LEFT LUNG before `_scan_extent_filter` has had a chance to remove the pad
# that set the floor. The capability then reported the right lung alone -- 1543 ml of
# "both lungs", split down the middle of the one lung it had -- and every gate passed it.
#
# Deferring the relative floor is exactly equivalent whenever the largest interior
# component IS a lung (the floor is recomputed against the same component either way),
# and strictly better when it is not. `MIN_COMPONENT_ML` still applies inside the lifted
# call, so the absolute floor is unchanged. CONTRACT.md §2 forbids editing the spike, and
# this changes an ARGUMENT rather than the lifted body.
THRESHOLD_STAGE_FRACTION = 0.0

# A cut plane is only a mediastinum if the profile actually has a valley there. Prominence
# is `min(peak_left, peak_right) - col[cut]`; the ratio below is that prominence over the
# smaller of the two peaks, i.e. "how much of the smaller lung's peak height does the
# valley descend". A genuine mediastinal valley on this cohort scores 0.93-1.00; a cut
# through the middle of a single lung scores ~0. Below this, the two sides are not
# separable by any plane of constant column index and the capability declines the case
# rather than reporting a lung sliced in half as two lungs.
MIN_VALLEY_PROMINENCE_RATIO = 0.55

# MOS-SAFE-014 `output_plausibility_gate`. An ABSURDITY gate, not a clinical range: it
# catches a gross segmentation failure (a mask that is scraps, or one that has swallowed
# the scanner table) and it does NOT catch moderate over- or under-segmentation. Declared
# honestly as such in KNOWN_FAILURE_MODES rather than presented as a validity check.
#
# The lower bound is PHYSIOLOGICAL, not fitted. A thoracic CT is acquired at or above
# functional residual capacity, and adult FRC is roughly 2.5-3.0 L; a -400 HU threshold
# recovers most of that (measured: 0.96 of the contoured volume, LS-FM-003). An adult
# thorax showing under 1.5 L of aerated lung is therefore a failed segmentation, not a
# small patient. (Paediatric studies are already outside the envelope in
# `not_validated_for`, and this bound would reject one.)
#
# Corroborated over all 60 LCTSC cases (24 Test + 36 Train), where the two populations do
# not overlap:
#     Dice >= 0.90 against the RTSTRUCT contours   totals 2102.8 .. 7426.4 ml   (n=45)
#     Dice <= 0.314 (the lung leaked, LS-FM-005)   totals   68.1 .. 1151.0 ml   (n=5)
# a 1.8x gap with the bound inside it.
#
# HONEST NOTE ON HOW THIS NUMBER WAS ARRIVED AT. Two earlier values were too low and each
# was corrected by a case that got through, so the empirical half of this bound is no
# longer held-out evidence:
#   250 ml  admitted LCTSC-Test-S3-102 (307.9 ml, Dice 0.000) and S3-203 (604.7 ml, 0.252)
#   1000 ml admitted LCTSC-Train-S3-002 (1151.0 ml, Dice 0.314) -- three unrelated scraps
#           of 624.5 + 307.6 + 218.9 ml left after both lungs merged with room air
# The physiological argument is what justifies 1500; the cohort only confirms it
# contradicts no genuine case. A partial leak that happens to leave MORE than 1.5 L of
# scraps would still pass, and nothing here detects it -- declared as LS-FM-008.
PLAUSIBLE_TOTAL_ML = (1500.0, 12000.0)

# Applicability envelope. There is no validation cohort behind these numbers, so they are
# the envelope of what the METHOD can be reasoned about, not of what it was measured on:
# a CT in HU, axial-ish, thin enough that a 6-connected component is contiguous.
# MOS-IMG-119 and docs/spec/16-open-questions.md §(b) both record that %LAA is not
# comparable across kernels or slice thicknesses, which is why dS is bounded at all.
MIN_SLICES = 16
DELTA_S_MM_RANGE = (0.5, 5.0)
MAX_IN_PLANE_MM = 1.5
MAX_TILT_DEG = 5.0

# The laterality cut is a plane of constant column index, which is a SAGITTAL plane only
# when the column axis is the patient's left-right axis. cos(5 deg) = 0.99619.
LATERALITY_AXIS_MIN_COS = 0.99619

# Segment order in the label map: value 1 is the right lung, value 2 the left. Radiology
# convention lists right first; the order is fixed here so that a stored SEG's segment
# numbers are stable across runs, which MOS-IMG-153 (re-running derives the same object)
# depends on.
SEGMENT_CONCEPT_KEYS: tuple[str, ...] = ("anatomy.lung_right", "anatomy.lung_left")
# `Finding.kind` uses the structure slug that keys `segment_profiles` in the concept
# dictionary, so a writer resolves the finding type and finding site from data rather than
# from a mapping table in Python (MOS-IMG-113).
STRUCTURE_SLUGS: tuple[str, ...] = ("lung_right", "lung_left")
TOTAL_SLUG = "lung"

KNOWN_FAILURE_MODES: tuple[FailureMode, ...] = (
    FailureMode(
        id="LS-FM-001",
        text=(
            "A low-density immobilisation pad, mattress or table insert forms an interior "
            "air-phase component that does not touch an in-plane face. Measured on "
            "LCTSC-Test-S2-101 (4.3 L pad), S2-102 and S3-103 (7.5 L pad). Besides "
            "inflating the mask it poisons any size floor taken relative to the largest "
            "component: on S3-103 that floor deleted the 447.3 ml left lung."
        ),
        detection="output_plausibility_gate",
        mitigation=(
            "The scan-extent filter discards an interior component touching both k faces, "
            "and the relative size floor is applied only AFTER it, against the largest "
            "surviving component. Residual cases are caught by PLAUSIBLE_TOTAL_ML only "
            "when gross."
        ),
    ),
    FailureMode(
        id="LS-FM-002",
        text=(
            "Laterality comes from a single sagittal plane, so the anterior junction line "
            "and the retrocardiac medial margin are mis-assigned. Measured over 45 "
            "LCTSC cases: mean Dice 0.949 (right) and 0.954 (left) against the RTSTRUCT "
            "contours, versus 0.953 for the whole lung."
        ),
        detection="human_review",
        mitigation=(
            "Per-lung volumes carry this error; the total lung volume does not. Report the "
            "total when a per-lung figure is not required."
        ),
    ),
    FailureMode(
        id="LS-FM-003",
        text=(
            "A -400 HU threshold excludes vessels, airway walls and consolidated or "
            "atelectatic lung. Against the LCTSC RTSTRUCT contours the segmented volume "
            "is usually below the contoured volume: ratio min 0.864, median 0.965, "
            "max 1.054, mean 0.962 over 45 LCTSC cases. The bias is one-sided but the "
            "deficit is not universal -- one case (LCTSC-Train-S3-011) exceeds its "
            "contour at 1.054, so this is a bias, not a bound."
        ),
        detection="none",
        mitigation=(
            "This is a property of the method, not a defect. It is why the output is a "
            "threshold mask and not a lung segmentation claim."
        ),
    ),
    FailureMode(
        id="LS-FM-004",
        text=(
            "An acquisition cropped exactly to the lungs makes the lung component touch "
            "both k faces, so the scan-extent filter discards it and the capability "
            "rejects a study it could in principle have segmented."
        ),
        detection="output_plausibility_gate",
        mitigation=(
            "Deliberate: the same condition is produced by a lung that leaked into room "
            "air (LS-FM-005) and nothing here distinguishes the two, so the capability "
            "rejects rather than guessing. The rejection detail lists the spanning "
            "components and their volumes."
        ),
    ),
    FailureMode(
        id="LS-FM-005",
        text=(
            "Where the body narrows above the lung apices, apical lung air becomes "
            "k-adjacent to room air and the whole lung merges into the room-air component, "
            "which is then discarded for touching an in-plane face. Measured on 4 of the "
            "24 LCTSC-Test cases: S2-201, S3-102, S3-203, S3-204."
        ),
        detection="output_plausibility_gate",
        mitigation=(
            "Detected only indirectly, as 'every retained component spans the scan "
            "extent', and answered with a rejection. A real fix needs a body mask, which "
            "this method does not build. The cheaper fix -- removing room air per axial "
            "slice instead of in 3D -- was implemented and measured over all 12 "
            "rejections: it recovers 9 at Dice 0.922-0.972 but reports 7085.5 ml against "
            "a contoured 2819.2 ml on LCTSC-Train-S3-002 and 3359.2 ml against 2482.5 ml "
            "on LCTSC-Test-S3-203, both inside PLAUSIBLE_TOTAL_ML and undetectable. Not "
            "adopted. See the module docstring for the per-component HU statistics that "
            "rule out separating those components by density."
        ),
    ),
    FailureMode(
        id="LS-FM-006",
        text=(
            "The mask includes the trachea and the main bronchi: their lumen is air at the "
            "threshold and there is no airway model to remove it. Roughly 30-50 ml, and "
            "every voxel of it is below -950 HU, so it inflates any %LAA computed inside "
            "this mask (emphysema_laa LAA-FM-005)."
        ),
        detection="none",
        mitigation=(
            "Declared, not mitigated. The volume error is under 1.5 % of the total; the "
            "%LAA error is not, and is quantified in emphysema_laa's docstring."
        ),
    ),
    FailureMode(
        id="LS-FM-007",
        text=(
            "The sagittal cut is placed at the deepest valley of the column profile. On a "
            "mask with no mediastinal valley -- one lung only, or two lungs whose column "
            "ranges overlap -- there is no correct plane, and any cut yields two "
            "plausible per-lung volumes and two per-lung %LAA values measuring the wrong "
            "voxels. Nothing downstream detects it: the TOTAL stays correct, the "
            "plausibility gate passes, and a mirrored or mis-split SEG renders perfectly. "
            "Observed as a real defect on LCTSC-Test-S3-101, which ships no RTSTRUCT and "
            "so was invisible to the cohort Dice statistics."
        ),
        detection="output_plausibility_gate",
        mitigation=(
            "The valley prominence is computed and compared against "
            "MIN_VALLEY_PROMINENCE_RATIO; below it the capability rejects. The prominence "
            "ratio and the voxel count at the cut column are recorded in the diagnostics "
            "on every run, so a marginal split is auditable after the fact."
        ),
    ),
    FailureMode(
        id="LS-FM-008",
        text=(
            "A PARTIAL leak. Where only part of the lung merges with room air, what "
            "survives the in-plane-face filter is several unrelated scraps rather than "
            "nothing, and their total can clear the plausibility floor. MEASURED on "
            "LCTSC-Train-S3-002: three components of 624.5 + 307.6 + 218.9 ml summed to "
            "1151.0 ml against a contoured 2819.2 ml, Dice 0.314 overall and 0.000 on the "
            "right lung -- the mask spanned columns 25-442 where the lungs occupy 130-378, "
            "and the sagittal cut then split the surviving scraps into a confident "
            "307.6 ml 'right lung' lying entirely inside the true LEFT lung."
        ),
        detection="none",
        mitigation=(
            "NOT mitigated, and deliberately not papered over. PLAUSIBLE_TOTAL_ML catches "
            "this case at its 1500 ml floor, but that is the floor doing a job it was not "
            "designed for: a partial leak leaving more than 1.5 L of scraps would pass. "
            "Detecting it properly needs a body mask, so that the lung field can be "
            "bounded relative to the patient rather than to the image, and this method "
            "does not build one. Until then the mask extent is recorded in the "
            "diagnostics so the failure is auditable after the fact."
        ),
    ),
)

METADATA = CapabilityMetadata(
    capability_id=CAPABILITY_ID,
    version=VERSION,
    method_class="deterministic_algorithm",
    method_summary=(
        "Fixed Hounsfield-unit threshold on the source grid, 6-connected component "
        "labelling, removal of components reaching an in-plane face (room air) or "
        "spanning the full scan extent (table/immobilisation), a relative size floor, and "
        "a single-sagittal-plane laterality split at the mediastinal minimum. No model, "
        "no weights, no training data, no learned parameter."
    ),
    output_kinds=("SEG", "MEASUREMENT"),
    computation_geometry="source",
    input_constraints=(
        "Single-frame axial CT in Hounsfield units, at least 16 slices, projected slice "
        "spacing 0.5-5.0 mm, in-plane pixel spacing at most 1.5 mm, gantry tilt at most "
        "5 degrees, column axis within 5 degrees of the patient left-right axis, thorax "
        "in the field of view."
    ),
    not_validated_for=(
        "Any clinical use. There is no validation cohort, no reference standard and no "
        "EvaluationRun behind this capability (CONTRACT.md §0: no evidence plane).",
        "Paediatric studies: no case under 18 years was examined.",
        "Contrast-enhanced, expiratory, prone or decubitus acquisitions.",
        "Lungs with consolidation, large effusion, atelectasis or a mass: a threshold "
        "excludes non-aerated lung by construction.",
        "Any modality other than CT, and any CT not reconstructed in Hounsfield units.",
    ),
    known_failure_modes=KNOWN_FAILURE_MODES,
    parameters={
        "lung_hu_min": LUNG_HU_MIN,
        "lung_hu_max": LUNG_HU_MAX,
        "min_component_ml": MIN_COMPONENT_ML,
        "min_component_fraction": MIN_COMPONENT_FRACTION,
        "connectivity": "6-connected (face)",
        "scan_extent_filter": "drop interior components touching both k faces",
        "component_filter_order": (
            "in-plane-face filter, absolute floor, scan-extent filter, THEN the relative "
            "floor against the largest survivor"
        ),
        "laterality": "sagittal plane at the deepest valley of the column profile",
        "min_valley_prominence_ratio": MIN_VALLEY_PROMINENCE_RATIO,
        "plausible_total_ml": list(PLAUSIBLE_TOTAL_ML),
        "computed_on": "source_grid",
    },
    operating_point=None,  # no score is produced, so there is no threshold to declare
)


@dataclass(frozen=True)
class LungMasks:
    """The richer return that `run()` narrows to a `CapabilityOutcome`.

    The worker writes `diagnostics` into `job_steps.detail` and the provenance record; a
    capability has nowhere to put it in `CapabilityOutcome` (CONTRACT.md §5 owns that
    field list), so the detail is returned by the function underneath `run()` rather than
    smuggled onto the outcome or stashed on the instance.
    """

    right: np.ndarray  # bool, SOURCE grid
    left: np.ndarray  # bool, SOURCE grid
    volume_ml: dict[str, float]
    diagnostics: dict[str, Any]

    @property
    def total(self) -> np.ndarray:
        return self.right | self.left


# --------------------------------------------------------------------------------------
# Steps 2-4
# --------------------------------------------------------------------------------------
def _scan_extent_filter(
    mask: np.ndarray, voxel_ml: float
) -> tuple[np.ndarray, dict[str, Any]]:
    """Drop interior components that span the whole craniocaudal extent of the scan.

    See the module docstring for the measured justification. The relative size floor is
    recomputed after the drop: the floor that `mask_from_hu_threshold` applied was taken
    relative to a largest component that turned out not to be lung, so re-applying it
    against the largest SURVIVING component is what removes the small satellites the
    couch had been keeping alive.
    """
    labels, n_labels = _connected_components_3d(mask)
    if n_labels == 0:
        return mask, {"scan_extent_filter": "no_components", "n_components_in": 0}

    counts = np.bincount(labels.ravel(), minlength=n_labels + 1)
    touches_k0 = np.zeros(n_labels + 1, dtype=bool)
    touches_k1 = np.zeros(n_labels + 1, dtype=bool)
    touches_k0[np.unique(labels[0])] = True
    touches_k1[np.unique(labels[-1])] = True

    all_labels = list(range(1, n_labels + 1))
    spanning = [c for c in all_labels if touches_k0[c] and touches_k1[c]]
    survivors = [c for c in all_labels if c not in spanning]

    if not survivors:
        # MEASURED, on LCTSC-Test-S3-102: the lung leaked into the room-air component
        # through k-adjacency above the apices (where the body narrows at the shoulders),
        # so step 1 discarded the lungs as room air and the only interior component left
        # was the 6185 ml couch. An earlier version of this function fell back to the
        # unfiltered mask here "so an auxiliary heuristic never turns a usable answer into
        # a rejection"; on that case the fallback reported the couch as a 6185 ml lung
        # with Dice 0.000 against the RTSTRUCT contours, and the absurdity gate passed it.
        #
        # So: REJECT. When every surviving component spans the whole scan extent, either
        # the acquisition is cropped exactly to the lungs or the lung has leaked and what
        # remains is hardware -- and nothing available here distinguishes the two. A
        # rejection is a clinical answer the operator can act on; a 6185 ml "lung" is not.
        raise CapabilityRejection(
            "service_declined",
            {
                "n_components": n_labels,
                "n_spanning_scan_extent": len(spanning),
                "spanning_components": sorted(
                    (
                        {"label": int(c), "volume_ml": float(counts[c]) * voxel_ml}
                        for c in spanning
                    ),
                    key=lambda d: -d["volume_ml"],
                )[:8],
            },
            "every retained air-phase component spans the full craniocaudal extent of "
            "the scan, so none of them is an anatomical lung: either the lungs merged "
            "with room air or only table/immobilisation hardware survived the threshold",
        )

    largest_ml = max(float(counts[c]) * voxel_ml for c in survivors)
    floor_ml = max(MIN_COMPONENT_ML, MIN_COMPONENT_FRACTION * largest_ml)
    kept = [c for c in survivors if float(counts[c]) * voxel_ml >= floor_ml]
    if not kept:  # pragma: no cover - the largest survivor always meets its own floor
        raise CapabilityRejection(
            "service_declined",
            {"n_components": n_labels, "recomputed_floor_ml": floor_ml},
            "no air-phase component survived the recomputed size floor",
        )

    keep_mask = np.zeros(n_labels + 1, dtype=bool)
    keep_mask[kept] = True
    diag = {
        "scan_extent_filter": "applied",
        "n_components_in": n_labels,
        "n_components_kept": len(kept),
        "recomputed_floor_ml": floor_ml,
        "dropped_spanning_scan_extent": sorted(
            ({"label": int(c), "volume_ml": float(counts[c]) * voxel_ml} for c in spanning),
            key=lambda d: -d["volume_ml"],
        )[:8],
        "dropped_below_floor": sorted(
            (
                {"label": int(c), "volume_ml": float(counts[c]) * voxel_ml}
                for c in survivors
                if c not in kept
            ),
            key=lambda d: -d["volume_ml"],
        )[:8],
    }
    return keep_mask[labels], diag


def _laterality_sign(vol: CanonicalVolume) -> int:
    """+1 if a higher column index means a more +x (patient LEFT) position, else -1.

    Raises `CapabilityRejection("unsupported_geometry")` when the column axis is not the
    patient's left-right axis, because the split plane below is a plane of constant column
    index and is only sagittal when it is. Guessing here would relabel left as right,
    which is the worst failure this module can have and the one no downstream check
    detects (a mirrored SEG renders perfectly).
    """
    col_axis = np.asarray(vol.affine, dtype=np.float64)[:3, 0]
    norm = float(np.linalg.norm(col_axis))
    if norm <= 0.0:
        raise CapabilityRejection(
            "unsupported_geometry",
            {"affine_column_axis": col_axis.tolist()},
            "the canonical affine has a degenerate column axis",
        )
    cos_lr = abs(float(col_axis[0]) / norm)
    if cos_lr < LATERALITY_AXIS_MIN_COS:
        raise CapabilityRejection(
            "unsupported_geometry",
            {
                "cos_to_lr_axis": cos_lr,
                "required_min_cos": LATERALITY_AXIS_MIN_COS,
                "affine_column_axis": [round(v, 6) for v in col_axis.tolist()],
            },
            "the image column axis is not the patient left-right axis, so a "
            "constant-column-index plane is not sagittal and laterality cannot be "
            "assigned without guessing",
        )
    return 1 if col_axis[0] > 0 else -1


def _deepest_valley(
    col: np.ndarray, i_lo: int, i_hi: int
) -> tuple[int, float, int, int]:
    """The column index of the deepest valley in a profile, by PROMINENCE.

    For every candidate cut `c` in `(i_lo, i_hi]`, prominence is

        min(max(col[i_lo:c]), max(col[c:i_hi+1])) - col[c]

    -- how far the profile descends below the lower of the two peaks it separates. The
    cut maximising it is the mediastinum, and nothing about the answer depends on where
    the centroid happens to fall.

    WHY NOT THE PREVIOUS RULE. The earlier version located the two peaks as
    `argmax(col[:centroid])` and `argmax(col[centroid:])`. That is only correct when the
    centroid lies BETWEEN the lungs. MEASURED on LCTSC-Test-S3-101, where the right lung
    is 3.4x the left: the centroid (column 219) falls INSIDE the right lung,
    so `argmax` of the upper half returned column 219 -- the right lung's own shoulder,
    not the left lung's peak at 313 -- the search window collapsed to 22 columns, and the
    cut was placed at column 219 where the profile holds 5542 voxels. The real valley is
    at ~270 and holds ~91. The split therefore ran through the middle of the right lung
    and handed everything beyond it to the left, producing two confident per-lung volumes
    and two per-lung %LAA values that were measuring the wrong voxels.

    Returns `(cut, prominence_ratio, peak_left, peak_right)`, where the ratio is the
    prominence over the smaller of the two peaks: 1.0 is a valley that reaches zero,
    0.0 is no valley at all.
    """
    values = col.astype(np.int64)
    window = values[i_lo : i_hi + 1]
    # Running maxima of everything strictly left of c, and of everything at or right of c.
    pre = np.maximum.accumulate(window)
    suf = np.maximum.accumulate(window[::-1])[::-1]
    # Candidate c has index `t` in the window, t >= 1 so the left side is non-empty.
    left_peak = pre[:-1]  # max over [i_lo, c-1] for c = i_lo+1 .. i_hi
    right_peak = suf[1:]  # max over [c, i_hi]
    depth = np.minimum(left_peak, right_peak) - window[1:]
    best = np.flatnonzero(depth == depth.max())
    # A flat valley floor is resolved to the MIDPOINT of the widest contiguous run of
    # maximal prominence, so the answer does not depend on numpy's tie-breaking direction
    # and is (up to integer rounding) symmetric under mirroring the volume.
    breaks = np.flatnonzero(np.diff(best) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [best.size - 1]))
    widest = int(np.argmax(ends - starts))
    t = int((best[starts[widest]] + best[ends[widest]]) // 2)
    cut = i_lo + 1 + t
    peak_l = int(left_peak[t])
    peak_r = int(right_peak[t])
    smaller_peak = min(peak_l, peak_r)
    ratio = float(depth[t]) / smaller_peak if smaller_peak > 0 else 0.0
    return cut, ratio, peak_l, peak_r


def _split_lr(
    mask: np.ndarray, sign: int
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Split one lung mask into (right, left) at a single sagittal column index.

    The cut is the deepest valley of the column-wise voxel-count profile -- the
    mediastinum -- located by prominence (`_deepest_valley`). A fixed central band would
    be a magic number that fails on an off-centre patient, and the global minimum of the
    profile is at the edge of the lung field rather than between the lungs, which is why
    neither is used.

    If no valley is deep enough, the capability REJECTS. A plane cannot separate two
    structures whose column ranges overlap, and on a mask that is really only ONE lung
    there is no correct plane at all; in both cases a cut still produces two
    plausible-looking per-lung volumes, which is the output this slice exists to avoid.
    """
    col = mask.sum(axis=(0, 1)).astype(np.int64)
    nz = np.flatnonzero(col)
    if nz.size < 3:
        raise CapabilityRejection(
            "service_declined",
            {"n_nonzero_columns": int(nz.size)},
            "the lung mask spans fewer than three image columns; there is nothing to "
            "split",
        )
    i_lo, i_hi = int(nz[0]), int(nz[-1])
    cut, prominence_ratio, peak_l, peak_r = _deepest_valley(col, i_lo, i_hi)

    # Did any single component actually straddle the cut? If not, the split follows
    # anatomy (the two lungs were already separate) and only then is it more than a
    # geometric convenience. Recorded either way; never asserted falsely.
    labels, n_labels = _connected_components_3d(mask)
    straddling = 0
    for c in range(1, n_labels + 1):
        cols_c = np.flatnonzero((labels == c).any(axis=(0, 1)))
        if cols_c.size and cols_c.min() < cut <= cols_c.max():
            straddling += 1

    if prominence_ratio < MIN_VALLEY_PROMINENCE_RATIO:
        raise CapabilityRejection(
            "service_declined",
            {
                "cut_column_index": int(cut),
                "valley_prominence_ratio": prominence_ratio,
                "required_min_ratio": MIN_VALLEY_PROMINENCE_RATIO,
                "column_profile_peaks": [peak_l, peak_r],
                "voxels_at_cut_column": int(col[cut]),
                "column_extent": [i_lo, i_hi],
                "n_components": int(n_labels),
            },
            "the column profile of the air-phase mask has no mediastinal valley "
            f"(deepest prominence {prominence_ratio:.3f} of the smaller peak, at column "
            f"{cut} which still holds {int(col[cut])} voxels), so no plane of constant "
            "column index separates a right lung from a left one. Reporting the two "
            "sides of an arbitrary cut as two lungs would be a fabricated measurement.",
        )

    low = mask.copy()
    low[:, :, cut:] = False
    high = mask.copy()
    high[:, :, :cut] = False
    # sign > 0: a higher column index is more +x, i.e. more toward the patient's LEFT.
    right, left = (low, high) if sign > 0 else (high, low)

    diag = {
        "lateral_separation": (
            "connected_components" if straddling == 0 else "sagittal_plane_at_mediastinum"
        ),
        "cut_column_index": int(cut),
        "column_profile_peaks": [peak_l, peak_r],
        "valley_prominence_ratio": prominence_ratio,
        "column_extent": [i_lo, i_hi],
        "n_components_straddling_cut": int(straddling),
        "laterality_sign": int(sign),
        "voxels_at_cut_column": int(col[cut]),
    }
    return right, left, diag


# --------------------------------------------------------------------------------------
# The capability
# --------------------------------------------------------------------------------------
def segment_lungs(vol: CanonicalVolume, ctx: CapabilityContext) -> LungMasks:
    """The whole method, returning masks plus diagnostics.

    `LungSegmentation.run` narrows this to a `CapabilityOutcome`. Kept separate so the
    worker can persist the diagnostics -- and so a test can assert on them -- without
    either of them having to reach inside the capability object.
    """
    hu = require_source_grid(vol, ctx)
    source = ctx.source
    d_r, d_c = source.pixel_spacing_mm
    voxel_ml = d_r * d_c * source.delta_s_mm / 1000.0

    try:
        masks, threshold_diag = mask_from_hu_threshold(
            source,
            lung_hu_min=LUNG_HU_MIN,
            lung_hu_max=LUNG_HU_MAX,
            min_component_ml=MIN_COMPONENT_ML,
            # NOT MIN_COMPONENT_FRACTION -- see THRESHOLD_STAGE_FRACTION. The relative
            # floor is applied by `_scan_extent_filter`, after the components that are not
            # lungs have been removed, because the floor is meaningless while the largest
            # component is a table.
            min_component_fraction=THRESHOLD_STAGE_FRACTION,
        )
    except SystemExit as exc:
        # `mask_from_hu_threshold` is lifted spike code and signals "no lung component"
        # with SystemExit, which would tear down the worker process rather than reject one
        # job. Convert it at the boundary: no component is a clinical answer about this
        # study (chapter 5 T7), not a reason to stop claiming work.
        raise CapabilityRejection(
            "service_declined",
            {
                "hu_min": float(hu.min()),
                "hu_max": float(hu.max()),
                "lung_hu_min": LUNG_HU_MIN,
                "lung_hu_max": LUNG_HU_MAX,
            },
            f"the HU threshold produced no lung component ({exc})",
        ) from exc

    mask = masks["lung"]
    filtered, extent_diag = _scan_extent_filter(mask, voxel_ml)

    total_ml = float(np.count_nonzero(filtered)) * voxel_ml
    low, high = PLAUSIBLE_TOTAL_ML
    if not (low <= total_ml <= high):
        raise CapabilityRejection(
            "service_declined",
            {
                "total_lung_ml": total_ml,
                "plausible_total_ml": list(PLAUSIBLE_TOTAL_ML),
                "threshold_diagnostics": threshold_diag,
                "scan_extent_diagnostics": extent_diag,
            },
            f"segmented lung volume {total_ml:.1f} ml is outside the absurdity gate "
            f"[{low}, {high}] ml; the threshold has segmented something that is not lung",
        )

    sign = _laterality_sign(vol)
    right, left, split_diag = _split_lr(filtered, sign)

    volume_ml = {
        "lung_right": float(np.count_nonzero(right)) * voxel_ml,
        "lung_left": float(np.count_nonzero(left)) * voxel_ml,
        "lung": total_ml,
    }
    diagnostics: dict[str, Any] = {
        "capability_id": CAPABILITY_ID,
        "version": VERSION,
        "method_class": METADATA.method_class,
        "computed_on": "source_grid",
        "voxel_ml": voxel_ml,
        "threshold": threshold_diag,
        "scan_extent": extent_diag,
        "laterality": split_diag,
        "volume_ml": dict(volume_ml),
        "plausibility_gate_ml": list(PLAUSIBLE_TOTAL_ML),
    }
    return LungMasks(
        right=right, left=left, volume_ml=volume_ml, diagnostics=diagnostics
    )


@dataclass(frozen=True)
class LungSegmentation:
    """CONTRACT.md §6 `Capability`, implemented. Stateless and shareable across jobs."""

    capability_id: str = CAPABILITY_ID
    version: str = VERSION
    metadata: CapabilityMetadata = METADATA
    concepts: ConceptDictionary = field(default_factory=load_concepts)

    def applicable(self, vol: CanonicalVolume) -> str | None:
        """Return None if applicable, else a machine-readable rejection reason.

        The reason is a chapter 5 §5.3.1 job-level code; `applicability_report` returns
        the same verdict with the observed and required values, which is what
        `job_series` / the rejection detail need. `applicable` narrows it, because
        CONTRACT.md §6 fixes the return type as `str | None`.
        """
        report = self.applicability_report(vol)
        return None if report is None else report[0]

    def applicability_report(
        self, vol: CanonicalVolume
    ) -> tuple[str, dict[str, Any]] | None:
        """`(reason_code, detail)` or None. Detail carries observed vs required."""
        if vol.modality != "CT":
            return (
                "input_constraint_unmet",
                {"observed": {"modality": vol.modality}, "required": {"modality": "CT"}},
            )
        if vol.value_units != "HU":
            return (
                "input_constraint_unmet",
                {
                    "observed": {"value_units": vol.value_units},
                    "required": {"value_units": "HU"},
                },
            )
        n_slices = int(vol.shape[0])
        if n_slices < MIN_SLICES:
            return (
                "input_constraint_unmet",
                {
                    "observed": {"n_slices": n_slices},
                    "required": {"min_slices": MIN_SLICES},
                },
            )
        delta_s, d_r, d_c = vol.spacing_mm
        lo, hi = DELTA_S_MM_RANGE
        if not (lo <= abs(delta_s) <= hi):
            return (
                "outside_applicability_envelope",
                {
                    "observed": {"delta_s_mm": delta_s},
                    "required": {"delta_s_mm_range": [lo, hi]},
                },
            )
        if max(d_r, d_c) > MAX_IN_PLANE_MM:
            return (
                "outside_applicability_envelope",
                {
                    "observed": {"pixel_spacing_mm": [d_r, d_c]},
                    "required": {"max_in_plane_mm": MAX_IN_PLANE_MM},
                },
            )
        if abs(vol.tilt_deg) > MAX_TILT_DEG:
            return (
                "unsupported_geometry",
                {
                    "observed": {"tilt_deg": vol.tilt_deg},
                    "required": {"max_tilt_deg": MAX_TILT_DEG},
                },
            )
        if vol.resampled_from_source:
            return (
                "unsupported_geometry",
                {
                    "observed": {"resampled_from_source": True},
                    "required": {"resampled_from_source": False},
                },
            )
        col_axis = np.asarray(vol.affine, dtype=np.float64)[:3, 0]
        norm = float(np.linalg.norm(col_axis))
        cos_lr = abs(float(col_axis[0]) / norm) if norm > 0 else 0.0
        if cos_lr < LATERALITY_AXIS_MIN_COS:
            return (
                "unsupported_geometry",
                {
                    "observed": {"cos_column_axis_to_lr": cos_lr},
                    "required": {"min_cos_column_axis_to_lr": LATERALITY_AXIS_MIN_COS},
                },
            )
        return None

    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome:
        """Segment, measure in source geometry, and return the bundle outcome."""
        report = self.applicability_report(vol)
        if report is not None:
            # `applicable()` is the worker's pre-flight, but a capability that trusts the
            # caller to have called it will one day be called without it.
            raise CapabilityRejection(
                report[0],
                report[1],
                f"{CAPABILITY_ID} is not applicable to series "
                f"{vol.series_instance_uid}: {report[0]}",
            )
        result = segment_lungs(vol, ctx)

        label_map = np.zeros(result.total.shape, dtype=np.uint8)
        # Right first, left second -- SEGMENT_CONCEPT_KEYS order. Write left last so an
        # overlap (impossible by construction, since the two come from one disjoint cut)
        # would be visible as left rather than silently blended.
        label_map[result.right] = 1
        label_map[result.left] = 2
        segments = tuple(coded(self.concepts, key) for key in SEGMENT_CONCEPT_KEYS)

        findings: list[Finding] = []
        for slug in (*STRUCTURE_SLUGS, TOTAL_SLUG):
            mask = {
                "lung_right": result.right,
                "lung_left": result.left,
                "lung": result.total,
            }[slug]
            measurement = measure_volume_ml(mask, ctx.source)
            findings.append(
                Finding(
                    kind=slug,
                    # `present` = "the structure named by `kind` was delineated and is
                    # non-empty". It is NOT a claim that the lung is normal: no
                    # `finding.*` concept is emitted anywhere in this capability, only
                    # `anatomy.*` ones. A one-lung case (pneumonectomy, or a split that
                    # put everything on one side) reports the empty side as False rather
                    # than as a 0 ml lung.
                    present=bool(mask.any()),
                    # No score: this is a deterministic threshold, not a classifier.
                    # `score=0.0` would read as a calibrated confidence of zero, which is
                    # a different and false claim (CONTRACT.md §5 `Finding`).
                    score=None,
                    measurements=(to_bundle_measurement(measurement, self.concepts),),
                )
            )

        return CapabilityOutcome(
            capability_id=CAPABILITY_ID,
            findings=tuple(findings),
            label_map=LabelMap(array=label_map, segments=segments),
            # MOS-IMG-121: the instances actually CONSUMED, in canonical slice order.
            # `SourceGeometry.sop_instance_uids` already excludes the dropped duplicates,
            # which the same requirement forbids appearing in the SR evidence sequence.
            source_sop_instance_uids=tuple(ctx.source.sop_instance_uids),
        )
