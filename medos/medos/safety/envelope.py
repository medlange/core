# SPDX-License-Identifier: Apache-2.0
"""`ApplicabilityEnvelope` -- the declared clinical envelope, and its evaluation.

docs/spec/07-evidence.md section 7.10 owns this entity outright: the manifest shape
(`MOS-EVID-095`), the three-zone evaluation (`MOS-EVID-096`), the narrow-only derivation
rule (`MOS-EVID-097`/`098`/`099`), the outcome table (`MOS-EVID-101`), the structured
rejection object (`MOS-EVID-102`) and the closed `reason_code` set (`MOS-EVID-103`).
docs/spec/15-delivery.md section 15.1.2's 0.2.0 row names "applicability envelopes" as a
release deliverable and chapter 5 section 5.3.1 fixes what a violation DOES: the job
terminates `REJECTED` with `reason_code = outside_applicability_envelope`, at the runner
pre-flight (T7), never `FAILED`.

WHAT AN ENVELOPE IS, AND WHY IT IS DATA
---------------------------------------
`medos/medos/capabilities/*.py` already refuse volumes they cannot reason about -- see
`lung_segmentation.applicable()`. That is a capability's own opinion about its method,
expressed in code, and it stays. An `ApplicabilityEnvelope` is a different claim by a
different party: it is the range of acquisitions the SERVICE VERSION was measured on,
derived from the cohort behind an `EvaluationRun` (`MOS-EVID-097`), and MOS-EVID-100 puts
its evaluation in the PLATFORM, before dispatch, where "the service MUST NOT be able to
influence it".

So the envelope is declared data with a content address, the evaluator is a pure function
of `(envelope, attributes)`, and neither imports a capability module. `MOS-EVID-098` is
the reason the distinction is worth the file: "a publisher MUST NOT widen a bound beyond
the derived value without a new EvaluationRun". A bound that lives in a capability's
`applicable()` can be widened in a commit; a bound that lives in a versioned, immutable,
content-addressed row cannot be widened without producing a new version whose digest a
`ValidationReport` either cites or does not.

THE THREE ZONES, AND WHY `MARGINAL` IS NOT A ROUNDING ERROR
-----------------------------------------------------------
`MOS-EVID-096`: an attribute is `IN`, `MARGINAL` or `OUT`, and the study's zone is the
WORST across all constraints. `MOS-EVID-101` then makes `MARGINAL` a tenant decision --
`flag` (proceed, annotate the object) or `reject` -- while `OUT` always rejects.

The temptation is to collapse `MARGINAL` into `IN` with a warning log. The reason not to
is in the outcome table's third column: a flagged result must reach the reader carrying
`[OUTSIDE VALIDATED RANGE]` on the SEG's `SeriesDescription` and a coded comment in the SR
naming the attribute, the observed value and the bound. A warning log reaches nobody who
is looking at the image.

`MOS-EVID-096`'s last sentence is the one that is easy to get backwards: "A missing
attribute evaluates to `MARGINAL` with reason `attribute_absent`, never to `IN`." An
absent `ConvolutionKernel` is not evidence that the kernel was fine.

WHAT THIS MODULE REFUSES AT LOAD TIME, AND WHY THAT IS THE RIGHT PLACE
----------------------------------------------------------------------
`MOS-EVID-103` makes the `reason_code` set CLOSED in 0.2.0. An envelope that constrains an
attribute with no member in that set could produce a rejection that carries no code, and a
clinical rejection whose machine-readable reason is absent is the failure CONTRACT.md
section 3 exists to prevent. So `ApplicabilityEnvelope.from_manifest` refuses an unknown
attribute, exactly as `MOS-DATA-073` refuses a `SeriesSelector` carrying an unknown `match`
key at registration. The same call refuses a categorical value outside the vocabulary
chapter 3 derives (`MOS-DATA-060`), for the same reason in the other direction: a declared
value that no observation can ever equal is a constraint that silently rejects everything.

SPEC DEFECTS FOUND HERE, REPORTED AND NOT SILENTLY FIXED
--------------------------------------------------------
D1. `MOS-EVID-100` puts envelope evaluation "at triage, after `SeriesSelector` match and
    before job dispatch". Chapter 5 section 5.3.1 puts `outside_applicability_envelope` at
    the "runner pre-flight (T7)", and `MOS-DATA-077` requires an explicitly requested job
    to EXIST and terminate `REJECTED` -- which a pre-dispatch check cannot produce. The
    two are only compatible if the check runs in both places. This release implements the
    runner pre-flight, because that is where `MOS-DATA-077`'s answerable job id comes
    from and where this codebase's `envelope_check` step already sits; the ambient
    auto-routing half (`MOS-DATA-076`, no Job at all) has no triage stage to hang off yet
    and `envelope_decisions.job_id` is nullable so that it can be added without a schema
    change.
D2. `MOS-EVID-103` assigns `envelope.coverage_insufficient` to "`z_coverage_mm` OUT low"
    and assigns NO member to `z_coverage_mm` OUT high, which `MOS-EVID-095`'s own example
    makes reachable (`max: 450.0`, no `marginal_max`). Since the set is closed, this module
    emits `envelope.coverage_insufficient` for both directions and relies on
    `MOS-EVID-102`'s `observed` and `bound` members -- which are machine-readable and
    unambiguous -- to carry the direction. The LABEL is the specification's; the DATA is
    correct.
D3. `MOS-EVID-095`'s example manifest spells categorical values in a vocabulary that is not
    the one chapter 3 derives: `convolution_kernel_class: [soft, standard]` against
    `MOS-DATA-060.2`'s `SOFT`/`STANDARD`/`SHARP`/`UNKNOWN`, and `contrast_phase:
    [non_contrast, venous]` against `MOS-DATA-060.5`'s `NONE`/`ARTERIAL`/`PORTAL_VENOUS`/
    `DELAYED`/`PULMONARY_ARTERIAL`/`UNKNOWN`. Chapter 3 owns the derivation and therefore
    the value space, so this module normalises case and refuses any declared value outside
    chapter 3's set, naming the permitted members. `soft` loads as `SOFT`; `venous` is
    refused with a message pointing at `PORTAL_VENOUS`. A silent case-insensitive
    best-effort match would make `venous` match nothing and reject every study.

Spec: MOS-EVID-095, MOS-EVID-096, MOS-EVID-097, MOS-EVID-098, MOS-EVID-100, MOS-EVID-101,
MOS-EVID-102, MOS-EVID-103, MOS-EVID-104, MOS-DATA-060, MOS-DATA-067, MOS-DATA-076,
MOS-DATA-077, MOS-DATA-079, MOS-DATA-080, MOS-EXEC-014, MOS-REL-032, CONTRACT.md
sections 0, 3 and 11.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from medos.core.errors import ClinicalRejection
from medos.evidence.digest import sha256_of
from medos.sdk.canonical import canonical_bytes

__all__ = [
    "Zone",
    "MarginalPolicy",
    "REASON_CODES",
    "CATEGORICAL_VOCABULARIES",
    "NUMERIC_ATTRIBUTES",
    "Constraint",
    "RangeConstraint",
    "EnumConstraint",
    "ApplicabilityEnvelope",
    "ConstraintVerdict",
    "EnvelopeVerdict",
    "EnvelopeViolation",
    "EnvelopeDeclarationError",
    "evaluate",
    "envelope_digest",
    "worst_zone",
]

Zone = Literal["IN", "MARGINAL", "OUT"]
MarginalPolicy = Literal["flag", "reject"]

# `MOS-EVID-096`'s ordering, written down once so "worst zone across all constraints" is a
# max() over an integer and not a chain of ifs that one day disagrees with itself.
_ZONE_RANK: dict[str, int] = {"IN": 0, "MARGINAL": 1, "OUT": 2}

# ---------------------------------------------------------------------------------------
# MOS-EVID-103, verbatim. CLOSED in 0.2.0: adding a member is a specification change.
#
# The mapping is attribute -> reason_code, which is the direction the evaluator needs and
# the direction the requirement's table is written in. `envelope.attribute_absent` has no
# attribute of its own -- it is the code for ANY attribute that is missing when the tenant
# policy is `reject` -- so it is a constant beside the table rather than a row in it.
# ---------------------------------------------------------------------------------------
REASON_CODES: Mapping[str, str] = {
    "slice_thickness_mm": "envelope.slice_thickness_out_of_range",
    "pixel_spacing_mm_max": "envelope.pixel_spacing_out_of_range",
    "z_coverage_mm": "envelope.coverage_insufficient",
    "instance_count": "envelope.instance_count_out_of_range",
    "convolution_kernel_class": "envelope.kernel_not_supported",
    "manufacturer": "envelope.manufacturer_unvalidated",
    "contrast_phase": "envelope.contrast_phase_unsupported",
    "body_part_examined": "envelope.body_part_mismatch",
    "patient_age_years": "envelope.age_out_of_range",
}

ABSENT_REASON_CODE = "envelope.attribute_absent"

# The attributes MOS-EVID-095's manifest constrains with `type: range`. Split from the
# categorical set because `from_manifest` refuses a `range` on a categorical attribute and
# an `enum_in` on a numeric one: `{attribute: slice_thickness_mm, type: enum_in, values:
# [1.0, 1.25]}` is a float equality test on a DICOM-derived number, which is a constraint
# that rejects on the last decimal place of a scanner's own rounding.
NUMERIC_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "slice_thickness_mm",
        "pixel_spacing_mm_max",
        "z_coverage_mm",
        "instance_count",
        "patient_age_years",
    }
)

# The value spaces chapter 3 DERIVES. See defect D3 in the module docstring: chapter 7's
# example manifest uses a different spelling, chapter 3 owns the derivation, and a declared
# value outside these sets is refused rather than silently never matched.
#
# `manufacturer` is deliberately ABSENT from this mapping: (0008,0070) is free text written
# by the scanner vendor and has no closed value space. It is compared case-insensitively
# and otherwise verbatim.
CATEGORICAL_VOCABULARIES: Mapping[str, frozenset[str]] = {
    # MOS-DATA-060.2's `kernel_classes.yaml` buckets, plus its `default: UNKNOWN`.
    "convolution_kernel_class": frozenset({"SOFT", "STANDARD", "SHARP", "UNKNOWN"}),
    # MOS-DATA-060.5's closed set. `UNKNOWN` is "a first-class value and a selector that
    # requires a phase MUST reject it" -- so it is declarable and, being absent from a
    # typical `values` list, lands OUT, which is the requirement's own outcome.
    "contrast_phase": frozenset(
        {"NONE", "ARTERIAL", "PORTAL_VENOUS", "DELAYED", "PULMONARY_ARTERIAL", "UNKNOWN"}
    ),
}

_CATEGORICAL_ATTRIBUTES: frozenset[str] = frozenset(REASON_CODES) - NUMERIC_ATTRIBUTES


class EnvelopeDeclarationError(ValueError):
    """A manifest this module refuses to load. Never a clinical outcome.

    A declaration error is a PUBLISHING defect -- the analogue of `MOS-DATA-073`'s
    registration-time rejection of a malformed `SeriesSelector` -- and it must not be
    reachable as a job state. A job never sees one: `from_manifest` runs when an envelope
    is declared, and an envelope that would not load was never stored.
    """


class EnvelopeViolation(ClinicalRejection):
    """A study the declared envelope refuses. Terminal state `REJECTED`, never `FAILED`.

    `MOS-EVID-102`: "`REJECTED` for an envelope violation is a **clinical** outcome, not a
    failure (Chapter 5). It MUST be visually distinct from `FAILED` in every UI and MUST
    carry a structured reason object, one entry per violated constraint."

    `reason_code` on the exception is chapter 5's JOB-level code
    (`outside_applicability_envelope`, the member `jobs.reject_reason_code`'s CHECK
    admits); MOS-EVID-103's finer dotted codes are one per violated constraint and live in
    `detail["violations"]`, which is where `MOS-EVID-102` puts them. The two vocabularies
    are not merged: `medos.worker.steps.reject_reason_code` maps the exception TYPE onto
    the job column and `job_steps.error_code` carries the fine code, so the machine-
    readable reason survives at full resolution without widening a CHECK constraint.
    """

    def __init__(self, detail: Mapping[str, Any], message: str) -> None:
        super().__init__("outside_applicability_envelope", dict(detail), message)


# =======================================================================================
# The declaration
# =======================================================================================
@dataclass(frozen=True)
class Constraint:
    """One row of `MOS-EVID-095`'s `spec.constraints`. Subclassed, never used directly."""

    attribute: str

    @property
    def reason_code(self) -> str:
        return REASON_CODES[self.attribute]

    def zone_of(self, observed: Any) -> tuple[Zone, dict[str, Any], str | None]:
        raise NotImplementedError

    def bound(self) -> dict[str, Any]:
        raise NotImplementedError

    def as_manifest(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True)
class RangeConstraint(Constraint):
    """`{attribute, type: range, min, max, marginal_min, marginal_max}`.

    All four bounds are optional and independently so. `min`/`max` delimit `IN`;
    `marginal_min`/`marginal_max` widen the interval to the outside of which is `OUT`. A
    side with a bound but no marginal bound steps straight from `IN` to `OUT` there, which
    is what `MOS-EVID-095`'s own `z_coverage_mm` row does on its upper side.

    `__post_init__` refuses `marginal_min > min` and `marginal_max < max`: a marginal band
    that is NARROWER than the validated band is not a tolerance, it is a typo that quietly
    rejects studies the publisher declared valid.
    """

    min: float | None = None
    max: float | None = None
    marginal_min: float | None = None
    marginal_max: float | None = None

    def __post_init__(self) -> None:
        if self.min is None and self.max is None:
            raise EnvelopeDeclarationError(
                f"{self.attribute}: a range constraint with neither min nor max "
                "constrains nothing; omit the constraint instead"
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise EnvelopeDeclarationError(
                f"{self.attribute}: min {self.min} exceeds max {self.max}"
            )
        if self.marginal_min is not None:
            if self.min is None:
                raise EnvelopeDeclarationError(
                    f"{self.attribute}: marginal_min without min has no band to widen"
                )
            if self.marginal_min > self.min:
                raise EnvelopeDeclarationError(
                    f"{self.attribute}: marginal_min {self.marginal_min} is above min "
                    f"{self.min}; a marginal band narrower than the validated band "
                    "rejects studies the publisher declared valid"
                )
        if self.marginal_max is not None:
            if self.max is None:
                raise EnvelopeDeclarationError(
                    f"{self.attribute}: marginal_max without max has no band to widen"
                )
            if self.marginal_max < self.max:
                raise EnvelopeDeclarationError(
                    f"{self.attribute}: marginal_max {self.marginal_max} is below max "
                    f"{self.max}; a marginal band narrower than the validated band "
                    "rejects studies the publisher declared valid"
                )

    def bound(self) -> dict[str, Any]:
        return {
            k: v
            for k, v in (
                ("min", self.min),
                ("max", self.max),
                ("marginal_min", self.marginal_min),
                ("marginal_max", self.marginal_max),
            )
            if v is not None
        }

    def zone_of(self, observed: Any) -> tuple[Zone, dict[str, Any], str | None]:
        if observed is None:
            return "MARGINAL", self.bound(), "attribute_absent"
        value = float(observed)
        below_in = self.min is not None and value < self.min
        above_in = self.max is not None and value > self.max
        if not below_in and not above_in:
            return "IN", self.bound(), None
        if below_in:
            floor = self.marginal_min
            if floor is not None and value >= floor:
                return "MARGINAL", self.bound(), "below_validated_range"
            return "OUT", self.bound(), "below_validated_range"
        ceiling = self.marginal_max
        if ceiling is not None and value <= ceiling:
            return "MARGINAL", self.bound(), "above_validated_range"
        return "OUT", self.bound(), "above_validated_range"

    def as_manifest(self) -> dict[str, Any]:
        return {"attribute": self.attribute, "type": "range", **self.bound()}


@dataclass(frozen=True)
class EnumConstraint(Constraint):
    """`{attribute, type: enum_in, values, marginal_values, marginal_values_allowed}`.

    `marginal_values_allowed: true` is `MOS-EVID-095`'s escape hatch on `manufacturer`:
    "any value when `marginal_values_allowed: true`" evaluates `MARGINAL` rather than
    `OUT`. It is a claim that the attribute is not a hard gate, not that it is unchecked --
    the study still lands `MARGINAL` and still triggers the tenant's `marginal_policy` and
    the mandatory annotation of `MOS-EVID-101`.
    """

    values: tuple[str, ...] = ()
    marginal_values: tuple[str, ...] = ()
    marginal_values_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.values:
            raise EnvelopeDeclarationError(
                f"{self.attribute}: an enum_in constraint with no values admits nothing; "
                "omit the constraint instead"
            )
        overlap = set(self.values) & set(self.marginal_values)
        if overlap:
            raise EnvelopeDeclarationError(
                f"{self.attribute}: {sorted(overlap)} appears in both values and "
                "marginal_values; a value cannot be both validated and tolerated"
            )

    def bound(self) -> dict[str, Any]:
        out: dict[str, Any] = {"values": list(self.values)}
        if self.marginal_values:
            out["marginal_values"] = list(self.marginal_values)
        if self.marginal_values_allowed:
            out["marginal_values_allowed"] = True
        return out

    def zone_of(self, observed: Any) -> tuple[Zone, dict[str, Any], str | None]:
        if observed is None or observed == "":
            return "MARGINAL", self.bound(), "attribute_absent"
        # Case-insensitive because (0008,0070) Manufacturer is vendor free text
        # ("SIEMENS", "GE MEDICAL SYSTEMS", "Philips") and because chapter 7's example
        # manifest lower-cases values chapter 3 derives upper-case (defect D3).
        folded = str(observed).strip().casefold()
        if any(folded == v.casefold() for v in self.values):
            return "IN", self.bound(), None
        if any(folded == v.casefold() for v in self.marginal_values):
            return "MARGINAL", self.bound(), "tolerated_value"
        if self.marginal_values_allowed:
            return "MARGINAL", self.bound(), "undeclared_value_tolerated"
        return "OUT", self.bound(), "undeclared_value"

    def as_manifest(self) -> dict[str, Any]:
        return {"attribute": self.attribute, "type": "enum_in", **self.bound()}


def _normalise_categorical(attribute: str, value: Any) -> str:
    text = str(value).strip()
    vocabulary = CATEGORICAL_VOCABULARIES.get(attribute)
    if vocabulary is None:
        return text
    upper = text.upper().replace("-", "_").replace(" ", "_")
    if upper not in vocabulary:
        raise EnvelopeDeclarationError(
            f"{attribute}: {value!r} is not a value chapter 3 derives. "
            f"MOS-DATA-060 admits {sorted(vocabulary)}; a declared value outside that "
            "set can never equal an observation, so the constraint would reject every "
            "study rather than the ones the publisher meant"
        )
    return upper


@dataclass(frozen=True)
class ApplicabilityEnvelope:
    """One version of one subject's declared envelope. `MOS-EVID-095`.

    Immutable per version (chapter 7 table 7.1) and content-addressed: `digest` is the
    sha256 of the canonical JSON of the manifest form, so two sites holding the same
    envelope agree on its identity without a registry, and a `ValidationReport` citing an
    `envelope_digest` (section 7.12.1) resolves to exactly one declaration.
    """

    subject_kind: Literal["service_version", "model_version"]
    subject_id: str
    subject_version: str
    version: int
    constraints: tuple[Constraint, ...]
    marginal_policy_default: MarginalPolicy = "flag"
    derivation: Literal["percentile_1_99", "min_max", "declared"] = "declared"
    derived_from_evaluation_run: str | None = None

    def __post_init__(self) -> None:
        if self.version < 1:
            raise EnvelopeDeclarationError("version starts at 1")
        if not self.constraints:
            raise EnvelopeDeclarationError(
                "an envelope with no constraints declares nothing. MOS-EVID-095 requires "
                "every ServiceVersion that declares a capability to carry an "
                "ApplicabilityEnvelope; an empty one is the absence it forbids"
            )
        seen = [c.attribute for c in self.constraints]
        duplicates = sorted({a for a in seen if seen.count(a) > 1})
        if duplicates:
            raise EnvelopeDeclarationError(
                f"attribute(s) {duplicates} constrained twice; MOS-EVID-096 evaluates an "
                "attribute to exactly one zone and two constraints on one attribute make "
                "that ill-defined"
            )
        if (self.derivation == "declared") != (self.derived_from_evaluation_run is None):
            raise EnvelopeDeclarationError(
                "derivation 'declared' means no EvaluationRun and every other derivation "
                "MUST name one (MOS-EVID-097)"
            )

    # -- construction ------------------------------------------------------------------
    @classmethod
    def from_manifest(cls, manifest: Mapping[str, Any]) -> ApplicabilityEnvelope:
        """Load `MOS-EVID-095`'s YAML/JSON document. Refuses anything it cannot evaluate.

        Accepts both the full `{apiVersion, kind, metadata, spec}` document and the bare
        `{subject_kind, ..., constraints}` mapping, because the row stored in
        `applicability_envelopes` is the bare form and the file a publisher ships is the
        full one. One loader, so the two cannot diverge in what they accept.
        """
        meta = dict(manifest.get("metadata") or {})
        spec = dict(manifest.get("spec") or {})
        flat = {**manifest, **meta, **spec}
        kind = manifest.get("kind")
        if kind is not None and kind != "ApplicabilityEnvelope":
            raise EnvelopeDeclarationError(
                f"kind {kind!r} is not ApplicabilityEnvelope (MOS-EVID-095)"
            )

        raw_constraints = flat.get("constraints")
        if not isinstance(raw_constraints, Sequence) or isinstance(raw_constraints, str):
            raise EnvelopeDeclarationError("spec.constraints MUST be a list")

        constraints: list[Constraint] = []
        for index, raw in enumerate(raw_constraints):
            if not isinstance(raw, Mapping):
                raise EnvelopeDeclarationError(f"constraints[{index}] is not a mapping")
            constraints.append(_constraint_from(dict(raw), index))

        subject_kind = str(flat.get("subject_kind", "service_version"))
        if subject_kind not in ("service_version", "model_version"):
            raise EnvelopeDeclarationError(
                f"subject_kind {subject_kind!r}: chapter 7 table 7.1 owns an "
                "ApplicabilityEnvelope at a ServiceVersion or a ModelVersion"
            )
        policy = str(flat.get("marginal_policy_default", "flag"))
        if policy not in ("flag", "reject"):
            raise EnvelopeDeclarationError(
                f"marginal_policy_default {policy!r} is not flag or reject (MOS-EVID-101)"
            )
        derivation = str(flat.get("derivation", "declared"))
        if derivation not in ("percentile_1_99", "min_max", "declared"):
            raise EnvelopeDeclarationError(
                f"derivation {derivation!r} is not one of percentile_1_99, min_max, "
                "declared (MOS-EVID-097)"
            )
        return cls(
            subject_kind=subject_kind,  # type: ignore[arg-type]
            subject_id=str(flat["subject_id"]),
            subject_version=str(flat["subject_version"]),
            version=int(flat["version"]),
            constraints=tuple(constraints),
            marginal_policy_default=policy,  # type: ignore[arg-type]
            derivation=derivation,  # type: ignore[arg-type]
            derived_from_evaluation_run=(
                None
                if flat.get("derived_from_evaluation_run") is None
                else str(flat["derived_from_evaluation_run"])
            ),
        )

    # -- projections -------------------------------------------------------------------
    def constraint_manifest(self) -> list[dict[str, Any]]:
        """The `spec.constraints` list, in declaration order. Stored verbatim."""
        return [c.as_manifest() for c in self.constraints]

    def as_manifest(self) -> dict[str, Any]:
        return {
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "subject_version": self.subject_version,
            "version": self.version,
            "derivation": self.derivation,
            "derived_from_evaluation_run": self.derived_from_evaluation_run,
            "marginal_policy_default": self.marginal_policy_default,
            "constraints": self.constraint_manifest(),
        }

    @property
    def digest(self) -> str:
        return envelope_digest(self.as_manifest())

    def to_stratification_shape(self) -> dict[str, Any]:
        """The reduced form `medos.evidence.stratification.stratification_report` reads.

        That module's C5 check (`MOS-TRAIN-090`, `MOS-EVID-097`) asks "does the cohort hold
        >= 20 patients in every cell the declared envelope marks `IN`", and it reads
        `{"categorical": {field: [values]}, "numeric": {field: {min, max}}}`. That shape is
        a lossy reduction of section 7.10.1's constraint list -- it has no marginal band,
        because a marginal band is not a cell a cohort has to cover.

        The projection lives HERE, on the full declaration, so there is exactly one place
        an envelope is written down and the evidence plane consumes a view of it. The
        alternative -- a publisher maintaining the reduced form by hand beside the real one
        -- is two declarations that agree until the day they do not, and the day they do
        not is the day a seal passes C5 against a cohort that does not cover the envelope
        the runtime is enforcing.
        """
        categorical: dict[str, list[str]] = {}
        numeric: dict[str, dict[str, float]] = {}
        for constraint in self.constraints:
            if isinstance(constraint, EnumConstraint):
                categorical[constraint.attribute] = list(constraint.values)
            elif isinstance(constraint, RangeConstraint):
                bounds: dict[str, float] = {}
                if constraint.min is not None:
                    bounds["min"] = constraint.min
                if constraint.max is not None:
                    bounds["max"] = constraint.max
                numeric[constraint.attribute] = bounds
        return {"categorical": categorical, "numeric": numeric}


def _constraint_from(raw: dict[str, Any], index: int) -> Constraint:
    attribute = str(raw.get("attribute", ""))
    if attribute not in REASON_CODES:
        raise EnvelopeDeclarationError(
            f"constraints[{index}].attribute {attribute!r} has no reason_code in "
            f"MOS-EVID-103's closed set. Permitted: {sorted(REASON_CODES)}. A constraint "
            "with no code could reject a study with no machine-readable reason, which "
            "CONTRACT.md section 3 forbids"
        )
    ctype = str(raw.get("type", ""))
    if ctype == "range":
        if attribute not in NUMERIC_ATTRIBUTES:
            raise EnvelopeDeclarationError(
                f"constraints[{index}]: {attribute} is categorical; use type enum_in"
            )
        return RangeConstraint(
            attribute=attribute,
            min=_opt_float(raw.get("min")),
            max=_opt_float(raw.get("max")),
            marginal_min=_opt_float(raw.get("marginal_min")),
            marginal_max=_opt_float(raw.get("marginal_max")),
        )
    if ctype == "enum_in":
        if attribute not in _CATEGORICAL_ATTRIBUTES:
            raise EnvelopeDeclarationError(
                f"constraints[{index}]: {attribute} is numeric; use type range. An "
                "enum_in over a DICOM-derived float is an equality test on the scanner's "
                "own rounding"
            )
        values = tuple(
            _normalise_categorical(attribute, v) for v in (raw.get("values") or ())
        )
        marginal = tuple(
            _normalise_categorical(attribute, v)
            for v in (raw.get("marginal_values") or ())
        )
        return EnumConstraint(
            attribute=attribute,
            values=values,
            marginal_values=marginal,
            marginal_values_allowed=bool(raw.get("marginal_values_allowed", False)),
        )
    raise EnvelopeDeclarationError(
        f"constraints[{index}].type {ctype!r} is not range or enum_in (MOS-EVID-095)"
    )


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def envelope_digest(manifest: Mapping[str, Any]) -> str:
    """`sha256:<hex>` over the canonical JSON of the manifest form. `MOS-EVID-007`/`095`.

    TWO REUSES, both deliberate, and the second was a real defect caught by the database:

    `medos.evidence.digest.sha256_of` and not a local hasher. `MOS-EVID-007`: "every digest
    is SHA-256, serialised `"sha256:" + lowercase_hex`", and the `sha256_digest` DOMAIN
    that `0006_evidence` created enforces `^sha256:[0-9a-f]{64}$`. This function first
    returned bare hex, and the column refused it -- which is the domain doing exactly what
    a domain is for. Chapter 7 section 7.12.1's `ValidationReport` prints
    `"applicability_envelope": {"version": 4, "digest": "sha256:18c6f2e0..."}`, so the
    prefixed form is also the form a report cites.

    `medos.sdk.canonical.canonical_bytes` and not a local
    `json.dumps(sort_keys=True)`: `MOS-REL-032` admits exactly one canonicaliser, and a
    digest computed by a second one is a digest that disagrees with every other digest in
    the platform on the day someone passes it a float.
    """
    return sha256_of(canonical_bytes(dict(manifest)))


# =======================================================================================
# Evaluation
# =======================================================================================
@dataclass(frozen=True)
class ConstraintVerdict:
    """One attribute's zone, with everything `MOS-EVID-102`'s object needs."""

    attribute: str
    zone: Zone
    observed: Any
    bound: dict[str, Any]
    detail: str | None  # below_validated_range | undeclared_value | attribute_absent | ...

    @property
    def reason_code(self) -> str:
        if self.detail == "attribute_absent":
            return ABSENT_REASON_CODE
        return REASON_CODES[self.attribute]


@dataclass(frozen=True)
class EnvelopeVerdict:
    """The study's zone, the per-attribute verdicts, and the derived outcome.

    `outcome` is `MOS-EVID-101`'s row for `(zone, marginal_policy)` and is computed once,
    here, so the worker, the provenance record and the `envelope_decisions` row cannot
    each re-derive it slightly differently.
    """

    envelope: ApplicabilityEnvelope
    marginal_policy: MarginalPolicy
    verdicts: tuple[ConstraintVerdict, ...]
    observed: Mapping[str, Any] = field(default_factory=dict)

    @property
    def zone(self) -> Zone:
        return worst_zone(v.zone for v in self.verdicts)

    @property
    def outcome(self) -> Literal["proceed", "proceed_flagged", "rejected"]:
        zone = self.zone
        if zone == "IN":
            return "proceed"
        if zone == "OUT":
            return "rejected"
        return "proceed_flagged" if self.marginal_policy == "flag" else "rejected"

    @property
    def rejected(self) -> bool:
        return self.outcome == "rejected"

    @property
    def in_envelope(self) -> bool:
        """`MOS-SAFE-083` section `execution.applicability.in_envelope`.

        True only for `IN`. A `MARGINAL` study that a site's `flag` policy allowed through
        is NOT in the envelope -- it is outside it and was permitted anyway, and a
        provenance record that says `in_envelope: true` for it is a false statement about
        the evidence behind the number.
        """
        return self.zone == "IN"

    def failing(self) -> tuple[ConstraintVerdict, ...]:
        """Every verdict that is not `IN`, worst first, then in declaration order."""
        return tuple(
            sorted(
                (v for v in self.verdicts if v.zone != "IN"),
                key=lambda v: -_ZONE_RANK[v.zone],
            )
        )

    def violations(self) -> list[dict[str, Any]]:
        """`MOS-EVID-102`'s structured reason object, one entry per violated constraint.

        Emitted for every non-`IN` verdict, not only the `OUT` ones, because a `MARGINAL`
        study that proceeds still has to carry "a coded comment naming each attribute,
        observed value and bound" into the SR (`MOS-EVID-101`), and that is this list.
        """
        out: list[dict[str, Any]] = []
        for verdict in self.failing():
            out.append(
                {
                    "class": "clinical_rejection",
                    "code": "ENVELOPE_VIOLATION",
                    "reason_code": verdict.reason_code,
                    "attribute": verdict.attribute,
                    "zone": verdict.zone,
                    "observed": verdict.observed,
                    "bound": verdict.bound,
                    "subject": {
                        "id": self.envelope.subject_id,
                        "version": self.envelope.subject_version,
                    },
                    "envelope_version": self.envelope.version,
                    "message": _message_for(verdict, self.envelope),
                }
            )
        return out

    @property
    def reason_code(self) -> str | None:
        """The worst violated constraint's `MOS-EVID-103` code, or None when `IN`.

        Denormalised onto `envelope_decisions.reason_code` so `MOS-EVID-104`'s export --
        counts per `(tenant, capability, service_version, reason_code)` -- is an index scan
        rather than a jsonb walk over every decision the site has ever taken.
        """
        failing = self.failing()
        if not failing:
            return None
        if self.zone == "MARGINAL" and self.marginal_policy == "flag":
            return None
        return failing[0].reason_code

    def as_problem_detail(self) -> dict[str, Any]:
        """What `EnvelopeViolation.detail` carries into the RFC 9457 body and the event."""
        return {
            "envelope": {
                "subject_kind": self.envelope.subject_kind,
                "subject_id": self.envelope.subject_id,
                "subject_version": self.envelope.subject_version,
                "version": self.envelope.version,
                "digest": self.envelope.digest,
            },
            "zone": self.zone,
            "marginal_policy": self.marginal_policy,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "violations": self.violations(),
            "observed": dict(self.observed),
        }

    def raise_if_rejected(self) -> None:
        """Turn a rejecting verdict into the `REJECTED` path 0.1.0 already built.

        `MOS-DATA-077`: an explicitly requested job "MUST always be created, and an
        `applicability` failure terminates it in `REJECTED` with reason
        `outside_applicability_envelope`." `EnvelopeViolation` is a `ClinicalRejection`,
        so `medos.worker.runner` takes transition T7 and writes `jobs.state = REJECTED`
        with `reject_reason_code`; nothing new in the runner is needed and nothing here
        decides its own terminal state.
        """
        if not self.rejected:
            return
        failing = self.failing()
        summary = "; ".join(
            f"{v.attribute}={v.observed!r} is {v.zone}" for v in failing
        )
        raise EnvelopeViolation(
            self.as_problem_detail(),
            f"study is outside the declared applicability envelope of "
            f"{self.envelope.subject_id}@{self.envelope.subject_version} "
            f"(envelope version {self.envelope.version}): {summary}",
        )


def _message_for(verdict: ConstraintVerdict, envelope: ApplicabilityEnvelope) -> str:
    """The human sentence of `MOS-EVID-102`. The UI renders it; the API returns the data.

    MOS-DATA-079: "`observed` and `expected` MUST contain real values from the triage
    record and the selector, not a rendered sentence." The sentence is IN ADDITION to the
    data, never instead of it, and it names no PHI -- an attribute name, a number and a
    bound.
    """
    subject = f"{envelope.subject_id} {envelope.subject_version}"
    if verdict.detail == "attribute_absent":
        return (
            f"{verdict.attribute} is absent from the study; {subject} declares a bound "
            f"for it, and a missing attribute is not evidence that it was within range"
        )
    bound = ", ".join(f"{k}={v}" for k, v in verdict.bound.items())
    verb = "is validated for" if verdict.zone == "OUT" else "tolerates"
    return (
        f"{verdict.attribute} observed {verdict.observed!r}; {subject} {verb} {bound}"
    )


def worst_zone(zones: Any) -> Zone:
    """`MOS-EVID-096`: "The study's zone is the worst zone across all constraints"."""
    worst = "IN"
    for zone in zones:
        if _ZONE_RANK[zone] > _ZONE_RANK[worst]:
            worst = zone
    return worst  # type: ignore[return-value]


def evaluate(
    envelope: ApplicabilityEnvelope,
    attributes: Mapping[str, Any],
    *,
    marginal_policy: MarginalPolicy | None = None,
) -> EnvelopeVerdict:
    """`MOS-EVID-096`. A pure function of `(envelope, attributes)` and nothing else.

    No clock, no database, no capability import. `MOS-EVID-100` makes this the platform's
    decision and puts it beyond the service's reach: "It MUST NOT be re-evaluated inside
    the service, and the service MUST NOT be able to influence it." A function with no
    collaborators is the enforceable form of that sentence -- there is no seam through
    which a service could reach it.

    `marginal_policy` is the TENANT's (`tenants.marginal_policy`, `MOS-EVID-101`), falling
    back to the envelope's own default. The tenant wins because the requirement states the
    policy at the tenant; the publisher's default exists so a fresh tenant is not silently
    the most permissive thing.

    `attributes` carries `None` for an attribute the study does not have. An attribute the
    envelope does not constrain is IGNORED and carried through to `observed` for the
    record, because `MOS-EVID-104`'s monitoring is more useful when the decision row shows
    what was measured and not only what was breached.
    """
    policy = marginal_policy or envelope.marginal_policy_default
    verdicts: list[ConstraintVerdict] = []
    for constraint in envelope.constraints:
        observed = attributes.get(constraint.attribute)
        zone, bound, detail = constraint.zone_of(observed)
        verdicts.append(
            ConstraintVerdict(
                attribute=constraint.attribute,
                zone=zone,
                observed=observed,
                bound=bound,
                detail=detail,
            )
        )
    return EnvelopeVerdict(
        envelope=envelope,
        marginal_policy=policy,
        verdicts=tuple(verdicts),
        observed=dict(attributes),
    )
