# SPDX-License-Identifier: Apache-2.0
"""`lung_nodule` -- the Service Plane half of the second capability.

WHAT THIS IS, IN ONE LINE
--------------------------
A `medos.capabilities.Capability` (CONTRACT.md section 6): a pure function of
`(CanonicalVolume, CapabilityContext)` returning a `CapabilityOutcome`. It reads pixels out
of its arguments and returns findings and a label map. That is the whole contract.

WHAT IT DOES NOT DO -- chapter 2 section 2.7's ownership table, row by row
---------------------------------------------------------------------------
  MOS-SVC-053  It does not enumerate the study to choose its input. It is handed one
               series and measures that one.
  MOS-SVC-054  It does not retrieve anything. There is no gateway client here and no
               token; the worker fetched the pixels before this module was called.
  MOS-SVC-055  It treats every UID as opaque and echoes `source_sop_instance_uids`
               verbatim (MOS-IMG-121).
  MOS-SVC-056  Every measurement is computed in SOURCE geometry, and `require_source_grid`
               refuses the call if `ctx.source` and `vol` disagree.
  MOS-SVC-059  It produces findings. Nothing downstream re-thresholds them.
  MOS-SVC-060  It constructs NO DICOM object. It returns a `LabelMap` on the source grid;
               `medos.writer` mints the SEG and the SR.
  MOS-SVC-061  It supplies no SeriesInstanceUID, SOPInstanceUID or SeriesNumber.

And it holds no PACS credential, opens no socket, touches no database and consumes no bus.
`tests/integration/test_lung_nodule.py::test_service_plane_imports_nothing_it_must_not`
asserts that as `MOS-SVC-038`'s static import scan over this package's import closure,
rather than leaving it as a claim in a docstring.

WHY IT LIVES IN `medos/services/` AND NOT IN `medos/medos/capabilities/`
--------------------------------------------------------------
`MOS-SVC-002`: the Service Plane MUST be the only plane containing vendor code. The three
capabilities under `medos/medos/capabilities/` predate the platform layer and are first-party
code that happens to sit in the core tree; `MOS-REL-020` asks whether a FOURTH can be added
without touching it. It can: `medos.worker.steps.WorkerDeps.registry` is an injected
`Mapping[str, Capability]` -- "Injected rather than imported so that ... the capability
registry stays a parameter" -- and `medos/services/lung_nodule/registry.py` composes this
capability into it. `medos.capabilities.REGISTRY` is never mutated and never imported for
its contents here.

TWO PLATFORM LIMITS THIS CAPABILITY RAN INTO, DESIGNED AROUND RATHER THAN PATCHED
----------------------------------------------------------------------------------
L1. EXACTLY ONE LABEL MAP PER JOB. `medos.writer.identity.plan_outputs` fails with
    `multiple_label_maps_in_bundle` when two capabilities in one bundle both return one.
    So `lung_nodule` runs in its OWN job -- it cannot share one with `lung_segmentation`.
    STILL A LIMIT, and still the reason for the separate job.
L2. A DETECTOR THAT FINDS NOTHING IS THE COMMON CASE. This one has since been FIXED in
    the platform, and the note is kept because the design decision it explains stands on
    its own once the constraint is gone.

    What it used to be: combined with L1, a normal chest CT yielded zero candidates, an
    empty label map and `all_segments_empty` -- a `SystemFailure`, i.e. the platform
    reporting a MALFUNCTION for a legitimate negative result. `step_write_dicom` also
    built a SEG unconditionally and ignored `requested_outputs`, so an SR-only job was
    unexpressible too. `plan_outputs` is now total and honours `requested_outputs`: a
    capability with nothing to outline gets a plan with no SEG and a recorded reason
    (`medos.writer.identity.OmittedOutput`), and the job COMPLETES with a negative result.

    The answer here is STILL to segment the SEARCH REGION as well as the findings, and it
    is no longer a workaround. Segment 1 is the lung field the detector actually examined,
    coded with the existing `anatomy.lung` concept; segment 2 is the union of accepted
    candidates. The lung field is never empty inside the applicability envelope, so a
    zero-candidate study produces a valid SEG whose nodule segment is OMITTED and RECORDED
    (`MOS-IMG-098`) and an SR that says zero.

    It shows a reader WHAT WAS SEARCHED, which is the only way to see `LN-FM-002` -- the
    juxtapleural regions this method structurally cannot reach -- and it is the difference
    between a negative a radiologist can act on and one they cannot.
    `MOS-SVC-007` sanctions it explicitly ("a service pipeline that chains several models
    ... MUST be implemented INSIDE one ServiceVersion").

    `test_lung_nodule.py::test_a_detection_with_nothing_to_draw_is_a_result_not_a_failure`
    is the same test that used to pin the defect, rewritten to pin the fix on the same
    three inputs.

THE REGISTRY ROW IS LOAD-BEARING, NOT DECORATIVE
-------------------------------------------------
`medos/examples/lung-nodule/capability.json` is not documentation that shadows a Python copy.
The applicability envelope this class enforces IS that file's `applicability.constraints`,
evaluated at run time; the `clinical:` block this class declares IS that file's, converted;
and every code emitted is resolved out of the concept dictionary. `MOS-REG-016` forbids a
hand-maintained duplicate of a manifest shape, and two copies of an envelope would drift on
the first edit -- the second copy always being the one nobody updates.

Spec: MOS-SVC-002, MOS-SVC-007, MOS-SVC-011, MOS-SVC-020, MOS-SVC-021, MOS-SVC-022,
MOS-SVC-023, MOS-SVC-038, MOS-SVC-053..061, MOS-IMG-039, MOS-IMG-040, MOS-IMG-098,
MOS-IMG-112, MOS-IMG-121, MOS-REG-016, MOS-REG-042, MOS-REG-044, MOS-REG-047,
MOS-SAFE-012, MOS-SAFE-014, MOS-SAFE-015, MOS-EXEC-001.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
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
from medos.core.measure import Measurement as CoreMeasurement
from medos.core.measure import measure_volume_ml, to_bundle_measurement
from services.lung_nodule import detector

__all__ = [
    "LungNodule",
    "CAPABILITY_ID",
    "VERSION",
    "FINDING_KIND",
    "SEARCH_REGION_FINDING_KIND",
    "CANDIDATE_KIND_PREFIX",
    "MAX_REPORTED_CANDIDATES",
    "registration_path",
    "load_registration",
    "metadata_from_registration",
]

CAPABILITY_ID = "lung_nodule"

#: SemVer, and it MUST move when anything in `detector.PARAMETERS` moves. `MOS-REG-047`:
#: the computation parameters of a deterministic capability are clinically load-bearing and
#: "MUST be versioned like any other". 0.1.0 and not 1.0.0 -- nothing here is validated.
VERSION = "0.1.0"

#: The nodule segment's profile slug, and therefore the finding kind that
#: `medos.writer.identity._attach_measurements` binds to that segment.
FINDING_KIND = "lung_nodule"

#: The search-region segment's profile slug. `lung` is an EXISTING profile coded
#: `SCT:39607008 Lung structure`; no new anatomy code is invented for it (MOS-IMG-112).
SEARCH_REGION_FINDING_KIND = "lung"

#: Per-candidate findings. Deliberately NOT a profile slug, so each becomes its own SR
#: measurement group rather than being folded into a segment.
CANDIDATE_KIND_PREFIX = "lung_nodule.candidate."

#: How many candidates are enumerated individually in the SR. The COUNT is always the full
#: number; this caps only the per-candidate groups, so a study with 40 candidates does not
#: produce a 40-group report nobody reads. Stated in `METADATA.parameters`.
MAX_REPORTED_CANDIDATES = 10


# =====================================================================================
# The registry row
# =====================================================================================
def registration_path() -> Path:
    """`medos/examples/lung-nodule/capability.json` -- the Capability registry row.

    Resolved relative to this file, so a checkout in any directory works. Read at
    CONSTRUCTION and never at import: `MOS-SVC-036` requires a native service to be
    importable without side effects, and `medos.capabilities.lung_segmentation` loads its
    concept dictionary through the same `default_factory` seam for the same reason.
    """
    return (
        Path(__file__).resolve().parents[2] / "examples" / "lung-nodule" / "capability.json"
    )


def load_registration(path: Path | None = None) -> dict[str, Any]:
    row: dict[str, Any] = json.loads(
        (path or registration_path()).read_text(encoding="utf-8")
    )
    if row.get("capability_id") != CAPABILITY_ID:
        raise ValueError(
            f"{path or registration_path()} registers "
            f"{row.get('capability_id')!r}, not {CAPABILITY_ID!r}"
        )
    if row.get("version") != VERSION:
        raise ValueError(
            f"registry row version {row.get('version')!r} != code version {VERSION!r}; "
            "MOS-REG-047 makes the parameters versioned data and the two MUST agree"
        )
    return row


def metadata_from_registration(row: Mapping[str, Any]) -> CapabilityMetadata:
    """Chapter 9's honesty block, CONVERTED from the registry row rather than restated.

    `CapabilityMetadata.__post_init__` already refuses an empty `not_validated_for` or
    `known_failure_modes` (`MOS-SAFE-015`: "a publisher with nothing to declare MUST write
    the entry explicitly"), so a row that dropped either fails here at construction.
    """
    clinical = row["clinical"]
    envelope = ", ".join(
        f"{c['attribute']} "
        + (
            f"in [{c.get('min')}, {c.get('max')}]"
            if c["kind"] == "range"
            else f"in {c.get('values')}"
        )
        for c in row["applicability"]["constraints"]
    )
    return CapabilityMetadata(
        capability_id=row["capability_id"],
        version=row["version"],
        method_class=row["method_class"],
        method_summary=(
            "Two Hounsfield thresholds, 6-connected component labelling, and five filters "
            "(equivalent spherical diameter, slice span, physical bounding-box aspect "
            "ratio, sphericity against the inscribed ellipsoid, mean HU). Candidates are "
            "soft-tissue components at or above -200 HU that are fully enclosed by the air "
            "phase inside the lung field, which is itself the interior air below -400 HU. "
            "THERE IS NO MODEL: no weights, no training set, no score and no operating "
            "point."
        ),
        output_kinds=("SEG", "SR", "MEASUREMENT"),
        computation_geometry="source",
        input_constraints=envelope,
        not_validated_for=tuple(clinical["not_validated_for"]),
        known_failure_modes=tuple(
            FailureMode(
                id=f["id"], text=f["text"], detection=f["detection"],
                mitigation=f["mitigation"],
            )
            for f in clinical["known_failure_modes"]
        ),
        parameters={
            **detector.PARAMETERS,
            "max_reported_candidates": MAX_REPORTED_CANDIDATES,
            "capability_registration": row["capability_id"] + "@" + row["version"],
            "acceptance_criteria_status": row["acceptance_criteria"]["status"],
        },
        # MOS-SVC-020: `operating_points` MUST be empty when and only when the method is
        # deterministic. There is no score, so there is no point to select.
        operating_point=None,
    )


# =====================================================================================
# The applicability envelope, evaluated from the row
# =====================================================================================
def _observed(vol: CanonicalVolume) -> dict[str, Any]:
    """The attribute names the registry row's constraints are written against.

    One place, so an envelope attribute that this build cannot observe is a KeyError at
    evaluation rather than a constraint that silently never fires -- which is the failure
    mode of a hand-written envelope check.
    """
    d_s, d_r, d_c = vol.spacing_mm
    return {
        "modality": vol.modality,
        "value_units": vol.value_units,
        "n_slices": int(vol.shape[0]),
        "delta_s_mm": abs(float(d_s)),
        "pixel_spacing_max_mm": max(float(d_r), float(d_c)),
        "tilt_deg": float(vol.tilt_deg),
        "resampled_from_source": str(bool(vol.resampled_from_source)).lower(),
    }


def _evaluate_envelope(
    constraints: list[Mapping[str, Any]], observed: Mapping[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """First failing constraint as `(reason_code, detail)`, or None.

    Order is the row's order and it is normative in exactly one respect: the detail a
    reader sees names the FIRST thing wrong, so the cheapest and most fundamental
    constraints (modality, units) are written first.
    """
    for c in constraints:
        attribute = c["attribute"]
        if attribute not in observed:
            raise KeyError(
                f"applicability constraint on {attribute!r} cannot be evaluated: this "
                f"build observes {sorted(observed)}. A constraint nothing evaluates is a "
                f"claim of safety nobody checks (MOS-SVC-023)."
            )
        value = observed[attribute]
        if c["kind"] == "enum":
            if str(value) not in {str(v) for v in c["values"]}:
                return (
                    c["reason_code"],
                    {
                        "observed": {attribute: value},
                        "required": {attribute: list(c["values"])},
                        "rationale": c["rationale"],
                    },
                )
        else:
            lo, hi = float(c["min"]), float(c["max"])
            if not (lo <= float(value) <= hi):
                return (
                    c["reason_code"],
                    {
                        "observed": {attribute: value},
                        "required": {attribute: [lo, hi], "unit": c.get("unit")},
                        "rationale": c["rationale"],
                    },
                )
    return None


# =====================================================================================
# The capability
# =====================================================================================
@dataclass(frozen=True)
class LungNodule:
    """CONTRACT.md section 6 `Capability`. Frozen, stateless, shareable across jobs.

    No per-job state lives on the instance (CONTRACT.md section 11: no global mutable
    state), so one instance in one registry is safe under concurrency. It declares no
    `depends_on` and no `bind`: `MOS-SVC-008` puts cross-service chaining out of scope for
    0.1.0-0.3.0 and forbids implementing it by convention, so this capability derives its
    own lung field instead of consuming `lung_segmentation`'s.
    """

    capability_id: str = CAPABILITY_ID
    version: str = VERSION
    concepts: ConceptDictionary = field(default_factory=load_concepts)
    registration: Mapping[str, Any] = field(default_factory=load_registration)

    @property
    def metadata(self) -> CapabilityMetadata:
        """MOS-SAFE-012's block. Derived from the registry row on every read.

        A property and not a field: the row is the source and a cached copy taken at
        construction would be a second definition of the same facts, which is what
        `MOS-REG-016` forbids. It is read a handful of times per job.
        """
        return metadata_from_registration(self.registration)

    # -- applicability ------------------------------------------------------------------
    def applicable(self, vol: CanonicalVolume) -> str | None:
        report = self.applicability_report(vol)
        return None if report is None else report[0]

    def applicability_report(
        self, vol: CanonicalVolume
    ) -> tuple[str, dict[str, Any]] | None:
        """`(reason_code, detail)` or None, evaluated from the registry row."""
        return _evaluate_envelope(
            list(self.registration["applicability"]["constraints"]), _observed(vol)
        )

    def method_detail(self, ctx: CapabilityContext) -> dict[str, Any]:
        """What reaches `result_measurements.method_detail` (MOS-STORE-283).

        Every parameter the numbers depend on, so a stored candidate count can be
        re-derived years later without this source tree.
        """
        return {
            "capability_id": CAPABILITY_ID,
            "capability_version": VERSION,
            "method_class": self.registration["method_class"],
            "learned_model": False,
            "score_available": False,
            "parameters": dict(detector.PARAMETERS),
            "clinical_use_mode": ctx.clinical_use_mode,
        }

    # -- run ----------------------------------------------------------------------------
    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome:
        """Detect, measure in source geometry, and return the outcome.

        `require_source_grid` is the guard that makes "computed in source geometry" a
        checked property rather than a comment: it refuses a header-only context, a context
        whose series UID disagrees with the volume, a resampled volume, and a shape
        mismatch.
        """
        report = self.applicability_report(vol)
        if report is not None:
            # `applicable()` is the worker's pre-flight, but MOS-SVC-023 is explicit that
            # the service MUST re-evaluate and MUST NOT rely on the platform having done
            # so. A capability that trusts its caller will one day be called without it.
            raise CapabilityRejection(
                report[0],
                report[1],
                f"{CAPABILITY_ID} is not applicable to series "
                f"{vol.series_instance_uid}: {report[0]}",
            )

        hu = require_source_grid(vol, ctx)
        result = detector.detect(hu, vol.spacing_mm)

        if result.lung_field_ml <= 0.0:
            # No lung field means the threshold found no interior air component at all.
            # That is a statement about the INPUT, not a negative finding, so it is a
            # clinical rejection (chapter 5 T7) and not a result saying zero.
            raise CapabilityRejection(
                "input_constraint_unmet",
                {
                    "observed": {"lung_field_ml": 0.0},
                    "required": {"min_lung_field_ml": detector.MIN_LUNG_COMPONENT_ML},
                    "rationale": (
                        "no interior air component survived the room-air and scan-extent "
                        "filters; there is no lung field to search"
                    ),
                },
                f"{CAPABILITY_ID}: no lung field on series {vol.series_instance_uid}",
            )

        if result.implausible:
            # MOS-SAFE-014 `output_plausibility_gate`. Reporting the number would be
            # reporting the bug as a measurement.
            raise CapabilityRejection(
                "service_declined",
                {
                    "observed": {"n_candidates": len(result.candidates)},
                    "required": {
                        "max_plausible_candidates": detector.MAX_PLAUSIBLE_CANDIDATES
                    },
                    "rationale": (
                        "a chest CT does not contain this many discrete nodules; the "
                        "threshold is wrong for this series and the count would be a "
                        "plausible-looking wrong number"
                    ),
                },
                f"{CAPABILITY_ID}: implausible candidate count on series "
                f"{vol.series_instance_uid}",
            )

        # -- the label map. Segment 1 = search region, segment 2 = candidates. -----------
        # `segments[i]` is label VALUE `i + 1` (CONTRACT.md section 5). Candidates are
        # written LAST so that a candidate voxel -- which is by construction inside the
        # lung bounding box but NOT inside the air-phase lung field -- wins any overlap.
        label_map = np.zeros(hu.shape, dtype=np.uint8)
        lung_field = result.lung_field
        label_map[lung_field] = 1
        label_map[result.mask] = 2
        segments = (
            coded(self.concepts, "anatomy.lung"),
            coded(self.concepts, "morphology.nodule"),
        )

        findings = self._findings(result, lung_field, ctx)

        return CapabilityOutcome(
            capability_id=CAPABILITY_ID,
            findings=findings,
            label_map=LabelMap(array=label_map, segments=segments),
            # MOS-IMG-121: the instances actually consumed, in canonical slice order, with
            # dropped duplicates already excluded by `SourceGeometry`.
            source_sop_instance_uids=tuple(ctx.source.sop_instance_uids),
        )

    # -- internals ----------------------------------------------------------------------
    def _findings(
        self,
        result: detector.DetectionResult,
        lung_field: np.ndarray,
        ctx: CapabilityContext,
    ) -> tuple[Finding, ...]:
        """Study-level findings first, then at most `MAX_REPORTED_CANDIDATES` candidates."""
        findings: list[Finding] = []

        # The search region. `present` means "a lung field was delineated", never "the
        # lung is normal": no `finding.*` concept is emitted for it.
        findings.append(
            Finding(
                kind=SEARCH_REGION_FINDING_KIND,
                present=bool(lung_field.any()),
                score=None,
                measurements=(
                    to_bundle_measurement(
                        measure_volume_ml(lung_field, ctx.source), self.concepts
                    ),
                ),
            )
        )

        # The study-level nodule finding. `present=False` here means "this algorithm's
        # filters admitted nothing", NOT "there is no nodule" -- see `not_validated_for`.
        findings.append(
            Finding(
                kind=FINDING_KIND,
                present=bool(result.candidates),
                # No score. A threshold is not a classifier, and 0.0 would claim a
                # calibrated confidence of zero (MOS-SVC-020/021).
                score=None,
                measurements=(
                    to_bundle_measurement(
                        measure_volume_ml(result.mask, ctx.source), self.concepts
                    ),
                    to_bundle_measurement(
                        CoreMeasurement(
                            concept_key="quantity.nodule_candidate_count",
                            value=float(len(result.candidates)),
                            ucum_unit="1",
                            ucum_unit_meaning="(count)",
                            computation_geometry="source",
                            qualifiers={
                                "n_enclosed_components": result.n_enclosed_components,
                                "rejected_by_filter": dict(result.rejected),
                            },
                        ),
                        self.concepts,
                    ),
                ),
            )
        )

        for candidate in result.candidates[:MAX_REPORTED_CANDIDATES]:
            findings.append(
                Finding(
                    kind=f"{CANDIDATE_KIND_PREFIX}{candidate.index:02d}",
                    present=True,
                    score=None,
                    measurements=(
                        to_bundle_measurement(
                            CoreMeasurement(
                                concept_key="quantity.volume",
                                value=candidate.volume_ml,
                                ucum_unit="ml",
                                ucum_unit_meaning="mL",
                                computation_geometry="source",
                                qualifiers={"n_voxels": candidate.n_voxels},
                            ),
                            self.concepts,
                        ),
                        to_bundle_measurement(
                            CoreMeasurement(
                                concept_key="quantity.nodule_equivalent_diameter",
                                value=candidate.equivalent_diameter_mm,
                                ucum_unit="mm",
                                ucum_unit_meaning="mm",
                                computation_geometry="source",
                                qualifiers={
                                    "aspect_ratio": candidate.aspect_ratio,
                                    "sphericity": candidate.sphericity,
                                    "mean_hu": candidate.mean_hu,
                                    "centroid_index": list(candidate.centroid_index),
                                },
                            ),
                            self.concepts,
                        ),
                    ),
                )
            )

        return tuple(findings)
