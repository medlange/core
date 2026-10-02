# SPDX-License-Identifier: Apache-2.0
"""The `Capability` interface, its context, and the honesty metadata that rides with it.

CONTRACT.md §6 fixes `CapabilityContext` and the `Capability` protocol; both are
transcribed here verbatim and MUST NOT be renamed or extended. CONTRACT.md §1 does not
name this file -- it names `capabilities/__init__.py` plus the three capability modules.
This module exists because the three capability modules and `__init__.py` all need the
protocol, and defining it in `__init__.py` would make every capability import its own
package mid-initialisation. `__init__.py` re-exports everything here, so
`from medos.capabilities import Capability, CapabilityContext` works exactly as §6
implies; nothing outside the package needs to know this file exists.

THE THREE RULES A CAPABILITY LIVES BY
  1. CONTRACT.md §6: "A capability MUST NOT touch the database, the network, or the
     PACS." It is a pure function of `(CanonicalVolume, CapabilityContext)`. Everything
     it needs is in its arguments -- that is what makes it testable on a fixture and
     what makes the worker, not the capability, responsible for I/O and for ordering.
  2. CONTRACT.md §5 / MOS-IMG-039: every measurement is computed in SOURCE geometry.
     `ctx.source` carries the source grid and its HU array; `vol` is the canonical volume
     and is used for APPLICABILITY, not for arithmetic. MOS-IMG-039 calls a measurement
     computed anywhere else "a defect, not an approximation".
  3. CONTRACT.md §11 / MOS-IMG-112: no code is ever invented. Coded concepts are resolved
     through `ConceptDictionary`, and a concept with no row raises.

DEPENDENCIES BETWEEN CAPABILITIES
  CONTRACT.md §7: "`emphysema_laa` depends on `lung_segmentation`'s mask. Express that as
  an explicit step ordering in `worker/steps.py`, not as a hidden import." §6's
  `CapabilityContext` has exactly four members and none of them is a place to put an
  upstream result, so the hand-off needs a seam that is neither a context field nor an
  import. `DependentCapability` is that seam: the capability DECLARES `depends_on`, and
  the worker calls `bind()` with the outcomes it has already produced, receiving a new
  capability instance. Nothing is mutated, nothing is imported, and a capability run
  without its dependency raises instead of quietly measuring the wrong thing.

CLINICAL HONESTY
  Chapter 9 (`MOS-SAFE-013`/`014`) makes a `clinical:` block mandatory on every
  `ServiceVersion` manifest, with `not_validated_for[]` and `known_failure_modes[]` each
  required to be non-empty (`MOS-SAFE-015`: "A publisher with nothing to declare MUST
  write the entry explicitly"). There are no registries and no manifests in this slice
  (CONTRACT.md §0), so `CapabilityMetadata` is the pre-registry stand-in for the subset
  of that block which is true of code rather than of a business: what the method IS, what
  it is NOT validated for, and how each failure mode is detected. The `detection`
  vocabulary is MOS-SAFE-014's closed enum, unchanged, so that when the registry lands
  these values move rather than being re-derived.

Spec: MOS-IMG-039, MOS-IMG-041, MOS-IMG-112, MOS-IMG-113, MOS-IMG-121, MOS-SAFE-012,
MOS-SAFE-014, MOS-SAFE-015, MOS-SAFE-017, MOS-EXEC-001.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

import numpy as np

from medos.core.bundle import CapabilityOutcome, CodedConcept, LabelMap
from medos.core.concepts import ConceptDictionary
from medos.core.errors import ClinicalRejection
from medos.core.geometry import CanonicalVolume, SourceGeometry

__all__ = [
    "CapabilityContext",
    "Capability",
    "DependentCapability",
    "CapabilityMetadata",
    "FailureMode",
    "DetectionMethod",
    "APPLICABILITY_REASON_CODES",
    "CapabilityRejection",
    "MissingDependency",
    "CLINICAL_USE_MODES",
    "default_concepts_path",
    "load_concepts",
    "coded",
    "segment_index",
    "mask_for_segment",
    "require_source_grid",
]


# --------------------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------------------
# MOS-SAFE-014's closed `detection` enum, copied exactly. Adding a member is a spec
# change, not a code change.
DetectionMethod = Literal[
    "applicability_envelope",
    "output_plausibility_gate",
    "human_review",
    "none",
]

# Chapter 5 §5.3.1's closed JOB-level rejection vocabulary, restricted to the codes a
# capability is in a position to decide. `no_eligible_series` and
# `no_candidate_service_version` are triage's (T3) and are deliberately absent:
# by the time a capability sees a volume, a series has already been selected.
APPLICABILITY_REASON_CODES: frozenset[str] = frozenset(
    {
        "outside_applicability_envelope",
        "unsupported_geometry",
        "input_constraint_unmet",
        "service_declined",
    }
)

# CONTRACT.md §6: "research_only" in this slice. Chapter 9 owns the enum.
CLINICAL_USE_MODES: frozenset[str] = frozenset({"research_only", "clinical"})


class CapabilityRejection(ClinicalRejection):
    """A capability declines to answer for this input. Terminal state `REJECTED`.

    Chapter 5 transition T7 covers exactly this: `RUNNING -> REJECTED` when "the
    applicability-envelope gate failed, geometry unsupported, or the service returned a
    `clinical_rejection` outcome". It is NOT `FAILED`: nothing malfunctioned, and every
    UI must render the two differently (`MOS-EXEC-001`, CONTRACT.md §3).

    `reason_code` is constrained to `APPLICABILITY_REASON_CODES` so a capability cannot
    invent a job-level rejection code that no consumer switches on.
    """

    def __init__(
        self, reason_code: str, detail: dict[str, object], message: str
    ) -> None:
        if reason_code not in APPLICABILITY_REASON_CODES:
            raise ValueError(
                f"{reason_code!r} is not a chapter 5 job-level rejection code a "
                f"capability may decide; allowed: {sorted(APPLICABILITY_REASON_CODES)}"
            )
        super().__init__(reason_code, dict(detail), message)


class MissingDependency(RuntimeError):
    """A dependent capability was run without `bind()`.

    A programming error in the worker's step ordering, so it is NOT a `MedosError`: it
    must not be renderable as a clinical rejection or as a transport failure. It means
    the pipeline is wired wrong, and the fix is code, not a retry.
    """


# --------------------------------------------------------------------------------------
# Metadata (pre-registry stand-in for chapter 9's `clinical:` block)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FailureMode:
    """One entry of MOS-SAFE-014's `known_failure_modes[]`.

    `id` is stable across versions when the text is unchanged (MOS-SAFE-017): provenance
    references these ids, so renumbering them breaks historical records.
    """

    id: str
    text: str
    detection: DetectionMethod
    mitigation: str


@dataclass(frozen=True)
class CapabilityMetadata:
    """What a capability declares about itself, in the vocabulary chapter 9 will demand.

    `method_class` is the field CONTRACT.md §7 is really asking for when it says "not a
    learned model; label it honestly". A threshold algorithm described as a "model" in a
    UI is a claim about provenance and validation that is simply untrue, and a clinician
    reading "AI segmentation" applies a different prior than one reading "HU threshold".

    `not_validated_for` and `known_failure_modes` are non-empty by construction
    (MOS-SAFE-015 fails closed on an empty list); `__post_init__` enforces it here so a
    capability cannot ship with nothing declared.
    """

    capability_id: str
    version: str
    method_class: Literal["deterministic_algorithm", "learned_model", "not_implemented"]
    method_summary: str
    output_kinds: tuple[str, ...]  # subset of SEG, SR, SC, MEASUREMENT (MOS-SAFE-014)
    computation_geometry: str  # "source" -- MOS-IMG-039
    input_constraints: str  # prose twin of the SeriesSelector (MOS-SAFE-018)
    not_validated_for: tuple[str, ...]
    known_failure_modes: tuple[FailureMode, ...]
    parameters: Mapping[str, object]  # every number the method depends on
    operating_point: float | None = None  # MOS-SVC-020 `score_threshold`; None = no score

    def __post_init__(self) -> None:
        if not self.not_validated_for:
            raise ValueError(
                f"{self.capability_id}: MOS-SAFE-015 requires at least one "
                "not_validated_for entry; an empty list is a registration error"
            )
        if not self.known_failure_modes:
            raise ValueError(
                f"{self.capability_id}: MOS-SAFE-015 requires at least one "
                "known_failure_modes entry; an empty list is a registration error"
            )
        if self.computation_geometry != "source":
            raise ValueError(
                f"{self.capability_id}: MOS-IMG-039 requires source geometry, got "
                f"{self.computation_geometry!r}"
            )

    def to_dict(self) -> dict[str, object]:
        """JSON projection, for the provenance record and the OHIF panel (MOS-SAFE-012)."""
        return {
            "capability_id": self.capability_id,
            "version": self.version,
            "method_class": self.method_class,
            "method_summary": self.method_summary,
            "output_kinds": list(self.output_kinds),
            "computation_geometry": self.computation_geometry,
            "input_constraints": self.input_constraints,
            "not_validated_for": list(self.not_validated_for),
            "known_failure_modes": [
                {
                    "id": f.id,
                    "text": f.text,
                    "detection": f.detection,
                    "mitigation": f.mitigation,
                }
                for f in self.known_failure_modes
            ],
            "parameters": dict(self.parameters),
            "operating_point": self.operating_point,
        }


# --------------------------------------------------------------------------------------
# The interface (CONTRACT.md §6, verbatim)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CapabilityContext:
    """CONTRACT.md §6, verbatim. Do not add fields.

    `source` rather than only `vol` because every measurement MUST be computed on the
    source grid (MOS-IMG-039/040/041) and only `SourceGeometry` carries it, including the
    projected slice spacing dS of MOS-IMG-016 that the volume formula needs.
    """

    job_id: str
    series_instance_uid: str
    source: SourceGeometry
    clinical_use_mode: str  # "research_only" in this slice


@runtime_checkable
class Capability(Protocol):
    """CONTRACT.md §6, verbatim."""

    capability_id: str
    version: str

    def applicable(self, vol: CanonicalVolume) -> str | None:
        """Return None if applicable, else a machine-readable rejection reason."""

    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome: ...


@runtime_checkable
class DependentCapability(Protocol):
    """A capability that consumes another capability's outcome.

    NOT part of CONTRACT.md §6 -- see this module's docstring for why it has to exist and
    why it is not a `CapabilityContext` field. The worker's step ordering (CONTRACT.md §7)
    is what calls `bind`; `depends_on` is what tells it the order.

    `bind` returns a NEW capability rather than mutating: the registry instance stays
    stateless and shareable across jobs, which is what CONTRACT.md §11's "no global
    mutable state" requires of anything reachable from a module-level `REGISTRY`.
    """

    depends_on: tuple[str, ...]

    def bind(self, upstream: Mapping[str, CapabilityOutcome]) -> Capability: ...


# --------------------------------------------------------------------------------------
# Concept dictionary plumbing
# --------------------------------------------------------------------------------------
_CONCEPTS_ENV = "MEDOS_CAPABILITY_CONCEPTS"


def default_concepts_path() -> Path:
    """Where the coded-concept rows live: beside this module, in this package.

    It used to be `spikes/week0/capability_concepts.json`, reached by counting directories
    up out of the package. That made a FROZEN SPIKE a runtime dependency of the platform.
    `MOS-IMG-112` forbids inventing a code and `MOS-IMG-113` forbids materialising the
    list in Python, so a deployment without that file cannot write a DICOM object at all
    -- and the file was not in the distribution, which is why the image carried a `COPY`
    and a `MEDOS_CAPABILITY_CONCEPTS` override to put it back.

    Moving it here is not a second copy. It is the same file, `git mv`'d, declared as
    package data in `pyproject.toml`, and the spike that held it is gone. There is still
    exactly one, which is what CONTRACT.md section 2 asked for; it is now inside the thing
    that reads it.

    `MEDOS_CAPABILITY_CONCEPTS` still overrides, and that remains the documented way a
    deployment points at the real `capability_concepts` table export (`MOS-IMG-111`).
    """
    override = os.environ.get(_CONCEPTS_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "capability_concepts.json"


def load_concepts(path: Path | None = None) -> ConceptDictionary:
    return ConceptDictionary(path or default_concepts_path())


def coded(concepts: ConceptDictionary, concept_key: str) -> CodedConcept:
    """Resolve a `CodedConcept` from the dictionary. Never construct one from a literal.

    MOS-IMG-112: "The writer MUST raise rather than invent a code." A guessed SNOMED code
    in a SEG is a clinical assertion nobody made, so this is the only constructor of
    `CodedConcept` that capability code may use.
    """
    row = concepts[concept_key]
    return CodedConcept(
        scheme=str(row["coding_scheme"]),
        code=str(row["code_value"]),
        meaning=str(row["code_meaning"]),
    )


# --------------------------------------------------------------------------------------
# Label-map helpers (how a downstream capability reads an upstream mask)
# --------------------------------------------------------------------------------------
def segment_index(label_map: LabelMap, concept: CodedConcept) -> int:
    """The uint8 value in `label_map.array` that means `concept`.

    `segments[i]` is value `i + 1` (CONTRACT.md §5). Resolving by CODE rather than by
    position is the whole point: it means `emphysema_laa` is coupled to
    `lung_segmentation` through the concept dictionary and not through its segment
    ordering, so reordering the upstream segments cannot silently swap left for right.
    """
    for index, segment in enumerate(label_map.segments):
        if (segment.scheme, segment.code) == (concept.scheme, concept.code):
            return index + 1
    raise MissingDependency(
        f"no segment coded {concept.scheme}:{concept.code} ({concept.meaning}) in the "
        f"upstream label map; it carries "
        f"{[(s.scheme, s.code, s.meaning) for s in label_map.segments]}"
    )


def mask_for_segment(label_map: LabelMap, concept: CodedConcept) -> np.ndarray:
    """The boolean mask of one segment of an upstream label map."""
    return label_map.array == np.uint8(segment_index(label_map, concept))


def require_source_grid(vol: CanonicalVolume, ctx: CapabilityContext) -> np.ndarray:
    """Validate the (volume, context) pair and return the source HU array.

    Four checks, each of which has a specific way of going wrong silently:

    * `ctx.source.hu_array is None` -- a `SourceGeometry` built by `from_header_scan` has
      no pixels. Measuring it would raise deep inside numpy; MOS-IMG-039 wants the
      refusal at the boundary.
    * UID agreement -- a context for job A applied to volume B produces a result whose
      provenance names the wrong series, which MOS-IMG-121's evidence rules cannot detect
      after the fact.
    * `vol.resampled_from_source` -- if the canonical volume is not the source grid then
      a label map computed on the source grid and a `vol`-derived one have different
      shapes, and CONTRACT.md §5 requires the SOURCE grid. This slice never resamples
      (MOS-IMG-021 makes reject the default), so the condition is an assertion, not a
      branch.
    * shape agreement -- the last line of defence for the two above.
    """
    if ctx.clinical_use_mode not in CLINICAL_USE_MODES:
        raise ValueError(
            f"clinical_use_mode={ctx.clinical_use_mode!r} is not one of "
            f"{sorted(CLINICAL_USE_MODES)} (chapter 9)"
        )
    hu = ctx.source.hu_array
    if hu is None:
        raise ValueError(
            "CapabilityContext.source carries no hu_array (header-only scan); "
            "MOS-IMG-039 requires the computation on the source grid, so there is "
            "nothing to compute on"
        )
    if ctx.series_instance_uid != vol.series_instance_uid:
        raise ValueError(
            f"context series {ctx.series_instance_uid} != volume series "
            f"{vol.series_instance_uid}; the provenance of any result would name the "
            "wrong series (MOS-IMG-121)"
        )
    if vol.resampled_from_source:
        raise ValueError(
            "the canonical volume was resampled from source; a capability in this slice "
            "computes on the source grid and has no inverse transform to apply "
            "(MOS-IMG-032/033)"
        )
    if hu.shape != vol.shape:
        raise ValueError(
            f"source grid {hu.shape} != canonical volume {vol.shape}; "
            "CONTRACT.md §5 requires the label map on the SOURCE grid"
        )
    return hu
