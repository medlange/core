# SPDX-License-Identifier: Apache-2.0
"""`emphysema_laa` -- percent low-attenuation area below -950 HU inside the lung mask.

WHAT IT COMPUTES, STATED COMPLETELY
    threshold        -950 HU, fixed. `LAA_THRESHOLD_HU` below.
    quantity         100 * |{v in M : HU(v) < -950}| / |M|, where M is the lung mask of
                     the `lung_segmentation` outcome and HU is the SOURCE array.
    computation grid SOURCE geometry, per chapter 4. MOS-IMG-041 is explicit that
                     intensity-threshold work happens "on source HU values after the
                     Modality LUT and padding substitution, without any resampling";
                     MOS-IMG-039 calls a measurement computed anywhere else "a defect,
                     not an approximation".
    component rule   Whatever `lung_segmentation` kept, and nothing else. Concretely, and
                     IN THIS ORDER: 6-connected air-phase components at -1024..-400 HU,
                     minus components reaching an in-plane face (room air), minus those
                     below an absolute 50 ml floor, minus those spanning the whole scan
                     extent (table/immobilisation), and only THEN a relative floor of
                     0.2 x the largest SURVIVING component. The order is load-bearing: a
                     relative floor taken before the table is removed is a floor set by
                     the table, and on LCTSC-Test-S3-103 it deleted a whole lung from the
                     denominator. The denominator of every percentage below is exactly
                     that mask, so the parameters of `lung_segmentation` are part of the
                     meaning of this number and are echoed into the diagnostics rather
                     than left implicit.
    denominator      Per structure. `laa_950_lung` uses the union of both lungs, which is
                     NOT the mean of the two per-lung percentages -- it is voxel-weighted,
                     and reporting the unweighted mean would be a different quantity.

WHAT IT DOES NOT CLAIM
    It does not say "emphysema". No `Finding` with kind `emphysema` is emitted and the
    concept `finding.emphysema` is deliberately never resolved here, even though the
    dictionary carries it. Calling a %LAA value emphysema requires a diagnostic cut-off
    validated against a reference standard; this capability has neither
    (docs/spec/16-open-questions.md §(a) records that the reference standard for the
    IMPLEMENTATION of this measurement is itself still open). Every `Finding.score` is
    `None` for the same reason: there is no calibrated score, and `score=0.0` would read
    as one.

    `present` means "at least one voxel below the threshold exists in this structure" and
    nothing more. It is a statement about voxels, not about a patient.

HOW BIG THE DENOMINATOR EFFECT ACTUALLY IS -- MEASURED
    The upstream mask is a Hounsfield threshold, so it INCLUDES the tracheal and main-
    bronchial lumen (roughly 30-50 ml of pure air, every voxel of it below -950 HU) and
    EXCLUDES vessels and airway walls. Both shifts push %LAA up relative to the same
    quantity computed inside an anatomical lung contour.

    Measured on 45 LCTSC cases (24 Test + 36 Train, those producing a result and shipping
    a matching RTSTRUCT), comparing this capability's value against the same -950 HU
    measurement computed inside the RTSTRUCT `Lung_L`/`Lung_R` contours of the same
    series:

        delta (capability - RTSTRUCT-mask), percentage points
        min +0.108   median +0.629   max +1.330   mean +0.691   n=45, ALL POSITIVE

    The one-sidedness is the point: this is a systematic offset with a mechanical cause,
    not noise. On a case whose true %LAA is near zero (S2-102, S2-204, S2-104: RTSTRUCT
    reference 0.000 %) this capability reports 0.1-0.5 %, which is entirely airway. On a
    case with real low-attenuation disease (S2-202: 14.9 % reference) the same ~1 pp
    offset is a 8 % relative error.

    It is NOT corrected, because correcting it needs an airway segmentation this method
    does not have, and a fudge factor would be worse than a declared offset. It is
    declared as LAA-FM-005 and quantified here so that nobody compares a value from this
    capability against a literature %LAA computed inside an anatomical lung mask.

COMPARABILITY
    MOS-IMG-119: %LAA "is not comparable across kernels or slice thicknesses". The
    convolution kernel, dS and the threshold ride along as qualifiers on every
    `medos.core.measure.Measurement` this capability produces, and the applicability
    envelope bounds dS for the same reason. A %LAA compared across two reconstructions is
    a different number twice, not the same number measured twice.

HOW IT GETS THE MASK
    CONTRACT.md §7: "`emphysema_laa` depends on `lung_segmentation`'s mask. Express that
    as an explicit step ordering in `worker/steps.py`, not as a hidden import." This
    module imports NOTHING from `lung_segmentation` -- not the class, not the constants,
    not the segment order. It declares `depends_on` and receives the upstream
    `CapabilityOutcome` through `bind()`; the masks are then found by CODED CONCEPT
    (`SCT:3341006`, `SCT:44029006`) resolved through the same `ConceptDictionary`. So the
    coupling is to the concept dictionary, which is where MOS-IMG-111 puts it, and
    reordering the upstream segments cannot silently swap left for right.

    Running it unbound raises `MissingDependency` -- a wiring error in the worker, never a
    silent measurement over the whole volume.

NO LABEL MAP
    `label_map` is None. A SEG of the sub-threshold voxels would look like a segmentation
    of emphysema and is not: at -950 HU a meaningful fraction of the voxels below the
    threshold on a 3 mm reconstruction are noise, and the quantity that is defensible is
    the aggregate percentage, not the voxel set. CONTRACT.md §7 asks for a percentage.

Spec: MOS-IMG-016, MOS-IMG-039, MOS-IMG-041, MOS-IMG-111, MOS-IMG-112, MOS-IMG-119,
MOS-IMG-121, MOS-SAFE-012, MOS-SAFE-014, MOS-SAFE-015, MOS-EXEC-001.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from medos.capabilities.base import (
    CapabilityContext,
    CapabilityMetadata,
    CapabilityRejection,
    FailureMode,
    MissingDependency,
    coded,
    load_concepts,
    mask_for_segment,
    require_source_grid,
)
from medos.core.bundle import CapabilityOutcome, Finding, LabelMap
from medos.core.concepts import ConceptDictionary
from medos.core.geometry import CanonicalVolume
from medos.core.measure import measure_laa_percent, to_bundle_measurement

__all__ = [
    "EmphysemaLaa",
    "METADATA",
    "CAPABILITY_ID",
    "VERSION",
    "LAA_THRESHOLD_HU",
    "DEPENDS_ON",
    "LUNG_SEGMENT_CONCEPT_KEYS",
    "FINDING_KINDS",
    "laa_for_masks",
    "LaaResult",
]

CAPABILITY_ID = "emphysema_laa"
VERSION = "0.1.0"

# The one number this capability is named after. -950 HU is the conventional threshold for
# percent low-attenuation area on inspiratory CT; it is a CONVENTION, not a value derived
# from any cohort held here, and it is a module constant because MOS-IMG-119 makes it part
# of the meaning of every value produced.
LAA_THRESHOLD_HU = -950.0

# The upstream capability, by id. Deliberately a STRING and not an import: CONTRACT.md §7
# requires the dependency to be an explicit step ordering rather than a hidden import, and
# an import here would also make the two capabilities un-substitutable -- a real learned
# lung segmentation must be able to take `lung_segmentation`'s place without this file
# changing.
DEPENDS_ON: tuple[str, ...] = ("lung_segmentation",)

# The masks this capability looks for in the upstream label map, by concept KEY. Resolved
# through the ConceptDictionary to coded concepts, then matched on (scheme, code).
LUNG_SEGMENT_CONCEPT_KEYS: tuple[str, ...] = ("anatomy.lung_right", "anatomy.lung_left")

# `Finding.kind` names the measured quantity and its site, and is deliberately NOT a
# structure slug and NOT `emphysema`: a consumer matching on anatomy will not match these,
# and a consumer looking for a diagnosis will not find one.
FINDING_KINDS: Mapping[str, str] = {
    "anatomy.lung_right": "laa_950_lung_right",
    "anatomy.lung_left": "laa_950_lung_left",
    "total": "laa_950_lung",
}

MIN_DENOMINATOR_VOXELS = 1000  # ~1 ml at 1 mm^3; below this a percentage is noise

KNOWN_FAILURE_MODES: tuple[FailureMode, ...] = (
    FailureMode(
        id="LAA-FM-001",
        text=(
            "%LAA is not comparable across reconstruction kernels or slice thicknesses "
            "(MOS-IMG-119). A sharp kernel raises it; a thick slice lowers it. Two values "
            "from two reconstructions of the same acquisition are different quantities."
        ),
        detection="applicability_envelope",
        mitigation=(
            "The convolution kernel and dS are carried as qualifiers on every "
            "measurement, and the envelope bounds dS. Comparison across kernels is the "
            "consumer's error to avoid; nothing here can detect it after the fact."
        ),
    ),
    FailureMode(
        id="LAA-FM-002",
        text=(
            "The denominator is whatever the upstream lung mask contains. A mask that has "
            "swallowed room air or an immobilisation pad -- both far below -950 HU -- "
            "inflates %LAA arbitrarily while looking entirely plausible."
        ),
        detection="output_plausibility_gate",
        mitigation=(
            "The upstream capability's plausibility gate and scan-extent filter run "
            "first, and the upstream mask volume is echoed into this capability's "
            "diagnostics so the denominator is auditable."
        ),
    ),
    FailureMode(
        id="LAA-FM-003",
        text=(
            "Image noise alone places voxels below -950 HU on a low-dose or thick-slice "
            "reconstruction, so a small non-zero %LAA is not evidence of any tissue "
            "abnormality."
        ),
        detection="human_review",
        mitigation=(
            "No diagnostic threshold is applied and no emphysema finding is emitted; the "
            "value is reported as a measurement with its threshold attached."
        ),
    ),
    FailureMode(
        id="LAA-FM-005",
        text=(
            "The denominator includes the tracheal and main-bronchial lumen, whose voxels "
            "are all below -950 HU, because the upstream mask is a Hounsfield threshold "
            "with no airway model. Measured against the same measurement computed inside "
            "the LCTSC RTSTRUCT lung contours: +0.108 to +1.330 percentage points, median "
            "+0.629, mean +0.691, ALL POSITIVE, n=45."
        ),
        detection="none",
        mitigation=(
            "Declared and quantified, not corrected: removing the airway needs a "
            "segmentation this method does not have, and a fudge factor would be worse "
            "than a known offset. Do not compare these values against a literature %LAA "
            "computed inside an anatomical lung mask."
        ),
    ),
    FailureMode(
        id="LAA-FM-004",
        text=(
            "Inspiratory effort changes lung attenuation globally. An expiratory or "
            "poorly-inspired acquisition lowers %LAA substantially with no change in "
            "anatomy."
        ),
        detection="none",
        mitigation=(
            "Not detectable from the pixel data available here. Declared, not mitigated."
        ),
    ),
)

METADATA = CapabilityMetadata(
    capability_id=CAPABILITY_ID,
    version=VERSION,
    method_class="deterministic_algorithm",
    method_summary=(
        "Percentage of voxels in the upstream lung mask whose SOURCE Hounsfield value is "
        "strictly below -950 HU, computed on the source grid with no resampling. No "
        "model, no weights, no training data, no learned parameter and no diagnostic "
        "threshold."
    ),
    output_kinds=("SR", "MEASUREMENT"),
    computation_geometry="source",
    input_constraints=(
        "The output of lung_segmentation for the same series, plus that capability's own "
        "input constraints. Inspiratory, non-contrast CT in Hounsfield units; projected "
        "slice spacing 0.5-5.0 mm."
    ),
    not_validated_for=(
        "Any clinical use, and specifically any assertion that emphysema is present or "
        "absent. There is no validation cohort and no reference standard "
        "(docs/spec/16-open-questions.md records the reference standard for this "
        "measurement's implementation as an open question).",
        "Comparison of values across reconstruction kernels, slice thicknesses, scanners "
        "or inspiratory states.",
        "Paediatric studies: no case under 18 years was examined.",
        "Contrast-enhanced or expiratory acquisitions.",
    ),
    known_failure_modes=KNOWN_FAILURE_MODES,
    parameters={
        "threshold_hu": LAA_THRESHOLD_HU,
        "comparison": "strictly less than",
        "denominator": "voxels of the upstream lung_segmentation mask, per structure",
        "total_denominator": "union of both lungs (voxel-weighted, not the mean of sides)",
        "computed_on": "source_grid",
        "resampling": "none",
        "depends_on": list(DEPENDS_ON),
    },
    operating_point=None,  # no score, therefore no operating point (MOS-SVC-020)
)


def laa_for_masks(
    masks: Mapping[str, np.ndarray], ctx: CapabilityContext
) -> dict[str, Any]:
    """Compute %LAA for each named mask. Pure; no dependency on any capability object.

    Returns `{name: measurement_or_None}` plus a `diagnostics` entry. A mask with fewer
    than `MIN_DENOMINATOR_VOXELS` voxels yields `None` rather than a number: a percentage
    over a handful of voxels is quantisation noise, and an empty mask makes it 0/0. The
    caller reports the absence; it does NOT substitute 0.0, which would be a fabricated
    measurement (CONTRACT.md §7's rule for `pleural_effusion`, applied here for the same
    reason).
    """
    out: dict[str, Any] = {}
    for name, mask in masks.items():
        n = int(np.count_nonzero(mask))
        if n < MIN_DENOMINATOR_VOXELS:
            out[name] = None
            continue
        out[name] = measure_laa_percent(mask, ctx.source, LAA_THRESHOLD_HU)
    return out


@dataclass(frozen=True)
class LaaResult:
    """The richer return that `run()` narrows to a `CapabilityOutcome`.

    Same shape as `lung_segmentation.LungMasks` and for the same reason: CONTRACT.md §5
    owns `CapabilityOutcome`'s field list, so there is nowhere on it to put the per-
    structure denominators and qualifiers the worker writes into `job_steps.detail` and
    the provenance record. Returning them from the function UNDER `run()` keeps the
    capability frozen and free of per-job state.
    """

    outcome: CapabilityOutcome
    diagnostics: dict[str, Any]


@dataclass(frozen=True)
class EmphysemaLaa:
    """CONTRACT.md §6 `Capability` plus `base.DependentCapability`.

    The instance in `REGISTRY` is UNBOUND and stateless. `bind()` returns a new instance
    carrying one job's upstream outcome, so a registry singleton is never mutated
    (CONTRACT.md §11: no global mutable state) and two jobs can run concurrently against
    the same registry.
    """

    capability_id: str = CAPABILITY_ID
    version: str = VERSION
    metadata: CapabilityMetadata = METADATA
    depends_on: tuple[str, ...] = DEPENDS_ON
    concepts: ConceptDictionary = field(default_factory=load_concepts)
    upstream: Mapping[str, CapabilityOutcome] | None = None

    # -- dependency ------------------------------------------------------------------
    def bind(self, upstream: Mapping[str, CapabilityOutcome]) -> EmphysemaLaa:
        """Return a copy of this capability bound to one job's upstream outcomes.

        The worker calls this between step `service_invoke` for `lung_segmentation` and
        step `service_invoke` for this capability. Validates the hand-off here rather than
        inside `run`, so a mis-ordered pipeline fails at the seam with a message that
        names the missing capability instead of somewhere inside numpy.
        """
        missing = [dep for dep in self.depends_on if dep not in upstream]
        if missing:
            raise MissingDependency(
                f"{CAPABILITY_ID} requires the outcome of {missing}; "
                f"got {sorted(upstream)}. CONTRACT.md §7 makes this an explicit step "
                "ordering in worker/steps.py."
            )
        for dep in self.depends_on:
            if upstream[dep].label_map is None:
                raise MissingDependency(
                    f"{CAPABILITY_ID} requires a label map from {dep}, which produced "
                    "none; there is no mask to measure inside"
                )
        return replace(self, upstream=dict(upstream))

    def _label_map(self) -> LabelMap:
        if self.upstream is None:
            raise MissingDependency(
                f"{CAPABILITY_ID}.run() was called without bind(). The mask comes from "
                f"{list(DEPENDS_ON)} through an explicit step ordering (CONTRACT.md §7), "
                "never from a hidden import, so running unbound measures nothing and "
                "MUST NOT silently fall back to the whole volume."
            )
        label_map = self.upstream[DEPENDS_ON[0]].label_map
        assert label_map is not None  # bind() already refused a None label map
        return label_map

    # -- interface -------------------------------------------------------------------
    def applicable(self, vol: CanonicalVolume) -> str | None:
        report = self.applicability_report(vol)
        return None if report is None else report[0]

    def applicability_report(
        self, vol: CanonicalVolume
    ) -> tuple[str, dict[str, Any]] | None:
        """`(reason_code, detail)` or None.

        Deliberately re-checks the input constraints rather than trusting that the
        upstream capability was applicable: `emphysema_laa` must be substitutable onto a
        DIFFERENT lung segmentation later, and an envelope that is only enforced upstream
        is an envelope that disappears when the upstream changes.
        """
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
        delta_s = abs(vol.spacing_mm[0])
        if not (0.5 <= delta_s <= 5.0):
            return (
                "outside_applicability_envelope",
                {
                    "observed": {"delta_s_mm": delta_s},
                    "required": {"delta_s_mm_range": [0.5, 5.0]},
                    "why": (
                        "MOS-IMG-119: %LAA is not comparable across slice thicknesses"
                    ),
                },
            )
        if vol.resampled_from_source:
            return (
                "unsupported_geometry",
                {
                    "observed": {"resampled_from_source": True},
                    "required": {"resampled_from_source": False},
                    "why": "MOS-IMG-041 forbids resampling before an HU threshold",
                },
            )
        return None

    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome:
        """CONTRACT.md §6. Narrows `measure()`; the diagnostics go to the worker."""
        return self.measure(vol, ctx).outcome

    def measure(self, vol: CanonicalVolume, ctx: CapabilityContext) -> LaaResult:
        """`run()` plus the per-structure diagnostics."""
        report = self.applicability_report(vol)
        if report is not None:
            raise CapabilityRejection(
                report[0],
                report[1],
                f"{CAPABILITY_ID} is not applicable to series "
                f"{vol.series_instance_uid}: {report[0]}",
            )
        require_source_grid(vol, ctx)
        label_map = self._label_map()

        if label_map.array.shape != ctx.source.hu_array.shape:  # type: ignore[union-attr]
            raise MissingDependency(
                f"upstream label map {label_map.array.shape} is not on the source grid "
                f"{ctx.source.hu_array.shape}; CONTRACT.md §5 requires the SOURCE grid "  # type: ignore[union-attr]
                "and a mismatched mask would measure the wrong voxels"
            )

        masks: dict[str, np.ndarray] = {}
        for key in LUNG_SEGMENT_CONCEPT_KEYS:
            masks[key] = mask_for_segment(label_map, coded(self.concepts, key))
        total = np.zeros(label_map.array.shape, dtype=bool)
        for mask in masks.values():
            total |= mask
        masks["total"] = total

        measurements = laa_for_masks(masks, ctx)

        d_r, d_c = ctx.source.pixel_spacing_mm
        voxel_ml = d_r * d_c * ctx.source.delta_s_mm / 1000.0
        findings: list[Finding] = []
        diagnostics: dict[str, Any] = {
            "capability_id": CAPABILITY_ID,
            "version": VERSION,
            "method_class": METADATA.method_class,
            "threshold_hu": LAA_THRESHOLD_HU,
            "computed_on": "source_grid",
            "convolution_kernel": ctx.source.convolution_kernel,
            "delta_s_mm": ctx.source.delta_s_mm,
            "upstream_capability": DEPENDS_ON[0],
            "upstream_segments": [
                {"scheme": s.scheme, "code": s.code, "meaning": s.meaning}
                for s in label_map.segments
            ],
            "per_structure": {},
        }

        for key in (*LUNG_SEGMENT_CONCEPT_KEYS, "total"):
            kind = FINDING_KINDS[key]
            measurement = measurements[key]
            n_denominator = int(np.count_nonzero(masks[key]))
            if measurement is None:
                # No number. Not 0.0 -- see `laa_for_masks`.
                findings.append(
                    Finding(kind=kind, present=False, score=None, measurements=())
                )
                diagnostics["per_structure"][kind] = {
                    "denominator_voxels": n_denominator,
                    "denominator_ml": n_denominator * voxel_ml,
                    "laa_percent": None,
                    "reason": "denominator below MIN_DENOMINATOR_VOXELS; "
                    "a percentage would be fabricated",
                }
                continue
            n_below = int(measurement.qualifiers["n_voxels_below"])
            findings.append(
                Finding(
                    kind=kind,
                    # "At least one voxel below the threshold exists in this structure."
                    # NOT "emphysema is present" -- see the module docstring.
                    present=n_below > 0,
                    score=None,  # no calibrated score exists; 0.0 would claim one
                    measurements=(
                        to_bundle_measurement(measurement, self.concepts),
                    ),
                )
            )
            diagnostics["per_structure"][kind] = {
                "denominator_voxels": n_denominator,
                "denominator_ml": n_denominator * voxel_ml,
                "voxels_below_threshold": n_below,
                "laa_percent": float(measurement.value),
                "qualifiers": dict(measurement.qualifiers),
            }

        outcome = CapabilityOutcome(
            capability_id=CAPABILITY_ID,
            findings=tuple(findings),
            # No SEG: a label map of sub-threshold voxels would read as a segmentation of
            # emphysema, which the threshold does not establish. See the module docstring.
            label_map=None,
            source_sop_instance_uids=tuple(ctx.source.sop_instance_uids),
        )
        return LaaResult(outcome=outcome, diagnostics=diagnostics)
