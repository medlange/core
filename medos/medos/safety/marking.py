# SPDX-License-Identifier: Apache-2.0
"""RUO marking: apply it, and refuse to emit an object that lacks it.

docs/spec/15-delivery.md section 15.1.2's 0.2.0 gate names one check for this whole
subject -- `ruo-marking`: "in `clinical_use_mode: research_only` the writer refuses to emit
an unmarked object". That sentence is `MOS-SAFE-039` (enforcement point E3) and
`MOS-IMG-136`, and this module is both halves of it.

WHY A SEPARATE VERIFIER AT ALL, WHEN THE WRITER ALREADY WRITES THE MARKS
------------------------------------------------------------------------
Because `MOS-SAFE-039` does not say "the writer writes the marks". It says:

    "the platform DICOM writer MUST NOT emit any object that lacks the complete RUO
     marker set of section 9.5. The check MUST run on the assembled dataset immediately
     before STOW-RS, MUST be implemented as a single function shared by the SEG, SR and
     SC writers, and MUST fail the Job rather than emit an unmarked object. There is no
     configuration flag that disables it."

Four separate claims, and the first is the one that makes a verifier necessary: the check
runs on the ASSEMBLED DATASET, not on the intent. A writer that sets `SeriesDescription`
and is then handed to a helper that truncates it, a highdicom version that drops an
attribute it does not recognise, an `apply_inherited_attributes` that copies a field over
the top -- every one of those produces an unmarked object out of correct writer code. The
check exists to be the thing that reads the bytes that are about to go to the PACS.

`MOS-SAFE-039`'s "immediately before STOW-RS" is honoured literally: `assert_marked` is
called in `medos.worker.steps.step_store_dicom` after the datasets are serialised and
before `gateway.store_files`, on the datasets re-read from the files that will be posted.
It is ALSO called at the end of `build_seg` and `build_sr`, because `MOS-IMG-136` says
"the writer MUST refuse to emit an object that violates any of them" and a writer that
hands a malformed object downstream has already emitted it as far as its own caller is
concerned. Two call sites, one function; the requirement asks for one function, not one
call.

`safety_marking_absent` is a `SystemFailure` and therefore `FAILED`, NOT `REJECTED`
--------------------------------------------------------------------------------------
`MOS-SAFE-035`'s E3 row spells the failure behaviour out: "object not emitted; Job
`FAILED`, `error.code = safety_marking_absent`." That is the correct classification and it
is worth saying why, because everything else in this module's neighbourhood is a clinical
rejection: an unmarked object does not mean the STUDY was unsuitable. It means the
platform's own writer is broken. `REJECTED` would tell a radiologist their data was the
problem, which is the exact inversion `MOS-EXEC-014` is about.

WHAT IS CHECKED, AND UNDER WHICH CHAPTER
-----------------------------------------
Chapter 4 (`MOS-IMG-133`/`134`/`136`/`137`) owns "attribute writing mechanics"; chapter 9
(`MOS-SAFE-048`/`049`/`050`) states "the safety-normative values" and says so in its own
preamble. Where they agree, this module checks the shared rule once. Where they disagree,
see the defects below: chapter 4's spelling wins, because chapter 9 explicitly delegates
the mechanics to chapter 4 and because 0.1.0 already writes chapter 4's form and is
gate-green against it.

SPEC DEFECTS FOUND HERE, REPORTED AND NOT SILENTLY FIXED
--------------------------------------------------------
D4. The `SeriesDescription` prefix is `[AI] ` / `[AI][RUO] ` in `MOS-IMG-133`/`136` and
    `AI ` / `AI RUO ` in `MOS-SAFE-048`. Two different strings for the one mark that a
    third-party viewer sees. This module checks chapter 4's, which is what
    `medos.writer.identity.ai_series_description` already writes.
D5. The research TEXT content item under the SR root is concept `finding` with value
    `RESEARCH USE ONLY - NOT FOR DIAGNOSTIC USE` in `MOS-IMG-136`, and concept
    (121106, DCM, "Comment") with value `RESEARCH USE ONLY. NOT FOR CLINICAL DECISION
    MAKING. This document must not be used to inform patient management.` in
    `MOS-SAFE-050`. `finding` is not a code, it is a category of code, and the concept
    dictionary this platform loads has no such key -- only `finding.pleural_effusion`,
    `finding.emphysema` and so on, each of which is a clinical assertion that would be
    FALSE on a research banner. So the coded concept is chapter 9's (121106, DCM,
    "Comment"), which is a real DCM code and is what chapter 9 -- the chapter that owns
    the safety-normative values -- writes down. The TEXT VALUE checked is chapter 4's
    substring `RESEARCH USE ONLY`, which both chapters' sentences begin with, so an object
    written to either chapter's wording passes.
D6. `MOS-SAFE-049` requires SEG `ContentLabel` = `AI_SEG` / `AI_RUO_SEG`;
    `MOS-IMG-090`-era code in this repository writes `MEDICALOS_AI`. `ContentLabel` is VR
    CS (16 chars, uppercase+underscore) and both are legal. Chapter 4 owns the attribute
    mechanics and does not constrain the value, so this module checks only that the label
    is non-empty and marks the object as AI-derived through the three standard mechanisms
    `MOS-IMG-133` requires, which is the property a downstream archive actually reads.

Spec: MOS-SAFE-033, MOS-SAFE-035, MOS-SAFE-039, MOS-SAFE-042, MOS-SAFE-047, MOS-SAFE-048,
MOS-SAFE-049, MOS-SAFE-050, MOS-SAFE-055, MOS-IMG-133, MOS-IMG-134, MOS-IMG-135,
MOS-IMG-136, MOS-IMG-137, MOS-IMG-138, MOS-EXEC-014, CONTRACT.md sections 3 and 11.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydicom.dataset import Dataset

from medos.core.errors import SystemFailure

__all__ = [
    "CLINICAL_USE_MODES",
    "RESEARCH_ONLY",
    "CLINICAL",
    "AI_PREFIX",
    "RUO_PREFIX",
    "RUO_SR_COMMENT",
    "PRIVATE_CREATOR",
    "ObjectKind",
    "MarkingViolation",
    "MarkingAbsent",
    "normalise_mode",
    "is_research_only",
    "marking_violations",
    "assert_marked",
    "research_comment_item",
    "apply_research_marking",
]

ObjectKind = Literal["SEG", "SR", "SC"]

# Chapter 9 stores the mode lower case (`jobs.clinical_use_mode`, `MOS-SAFE-033`); chapter
# 4's private attribute (0099,1002) is upper case (`MOS-IMG-137`). Both spellings reach
# this module -- the job row on one side, the assembled dataset on the other -- so it
# accepts either and normalises once, here, rather than leaving `.upper()` sprinkled at
# every comparison where one omission is a silently unenforced gate.
RESEARCH_ONLY = "RESEARCH_ONLY"
CLINICAL = "CLINICAL"
CLINICAL_USE_MODES: frozenset[str] = frozenset({RESEARCH_ONLY, CLINICAL})

AI_PREFIX = "[AI] "  # MOS-IMG-133
RUO_PREFIX = "[AI][RUO] "  # MOS-IMG-136
RUO_CONTENT_DESCRIPTION_PREFIX = "RESEARCH USE ONLY - "  # MOS-IMG-136, SEG
RUO_SR_COMMENT = (
    "RESEARCH USE ONLY - NOT FOR DIAGNOSTIC USE. "
    "NOT FOR CLINICAL DECISION MAKING. "
    "This document must not be used to inform patient management."
)
# The substring every conforming wording of the SR research item contains: chapter 4's
# sentence and chapter 9's sentence both open with it (defect D5).
RUO_SR_COMMENT_REQUIRED_SUBSTRING = "RESEARCH USE ONLY"

PRIVATE_CREATOR = "MEDICALOS_AI_1.0"  # MOS-IMG-137/138
_PRIVATE_GROUP = 0x0099
_AI_DERIVED_ELEMENT = 0x01
_CLINICAL_USE_MODE_ELEMENT = 0x02

# MOS-SAFE-049 / MOS-SAFE-050, the SR research comment's coded concept. See defect D5.
COMMENT_CONCEPT = ("121106", "DCM", "Comment")


def normalise_mode(mode: str) -> str:
    """`research_only` / `RESEARCH_ONLY` / `clinical` / `CLINICAL` -> the upper form.

    Raises on anything else. `MOS-SAFE-033` makes the enum exactly two values and makes
    `research_only` the default; a typo that fell through to "not research, therefore no
    RUO marks required" is the one failure this function must make impossible, so an
    unknown value is an error and never a permissive default.
    """
    normalised = str(mode).strip().upper()
    if normalised not in CLINICAL_USE_MODES:
        raise ValueError(
            f"clinical_use_mode {mode!r} is not one of {sorted(CLINICAL_USE_MODES)} "
            "(MOS-SAFE-033). There is no third value and no default other than "
            "research_only"
        )
    return normalised


def is_research_only(mode: str) -> bool:
    return normalise_mode(mode) == RESEARCH_ONLY


@dataclass(frozen=True)
class MarkingViolation:
    """One absent or wrong mark. Typed, so the failure names the tag and not a sentence."""

    rule: str  # the requirement id, e.g. "MOS-IMG-136"
    attribute: str  # the DICOM attribute or private element
    expected: str
    observed: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "attribute": self.attribute,
            "expected": self.expected,
            "observed": self.observed,
        }


class MarkingAbsent(SystemFailure):
    """`MOS-SAFE-035` E3: "object not emitted; Job `FAILED`, `error.code =
    safety_marking_absent`."

    A `SystemFailure` and therefore `FAILED` -- see the module docstring for why this one
    is not a clinical rejection. `medos.worker.steps.failure_class_for` maps it onto
    chapter 5 section 5.3.2's class; the `reason_code` carried here is the code
    `MOS-SAFE-035` names, verbatim.
    """

    def __init__(self, violations: Sequence[MarkingViolation], message: str) -> None:
        super().__init__(
            "safety_marking_absent",
            {"violations": [v.as_dict() for v in violations]},
            message,
        )
        self.violations = tuple(violations)


# =======================================================================================
# Applying the marks that the writers do not already apply
# =======================================================================================
def research_comment_item() -> Dataset:
    """The SR's research TEXT content item. `MOS-IMG-136` + `MOS-SAFE-050`.

    Built as a raw content item rather than through a highdicom template, deliberately: it
    is a free TEXT item under the document root, TID 1500 has no slot named for it, and
    passing it through a `MeasurementReport` template would either be refused or land it
    inside a measurement group -- where it would read as a comment ON a measurement rather
    than on the document. `RelationshipType HAS OBS CONTEXT` is likewise wrong; `CONTAINS`
    is what a root-level remark is.
    """
    concept = Dataset()
    concept.CodeValue = COMMENT_CONCEPT[0]
    concept.CodingSchemeDesignator = COMMENT_CONCEPT[1]
    concept.CodeMeaning = COMMENT_CONCEPT[2]

    item = Dataset()
    item.RelationshipType = "CONTAINS"
    item.ValueType = "TEXT"
    item.ConceptNameCodeSequence = [concept]
    item.TextValue = RUO_SR_COMMENT
    return item


def apply_research_marking(obj: Dataset, *, object_kind: ObjectKind, mode: str) -> None:
    """Add the per-object-type research marks chapter 4's writers do not add themselves.

    Idempotent: calling it twice adds one item, because a second copy of the banner in an
    SR is a document that looks like it was assembled twice. Callable in `clinical` mode,
    where it does nothing -- `MOS-SAFE-055` makes marking a write-time decision from the
    Job's pinned mode and forbids retroactive re-marking, so the mode is an argument and
    never a lookup.
    """
    if not is_research_only(mode):
        return
    if object_kind != "SR":
        # SEG's `ContentDescription` prefix and the `[AI][RUO] ` SeriesDescription are
        # applied by `medos.writer.seg` / `medos.writer.identity` at construction, where
        # they are constructor arguments and cannot be added afterwards.
        return
    existing = list(getattr(obj, "ContentSequence", []) or [])
    for item in existing:
        if RUO_SR_COMMENT_REQUIRED_SUBSTRING in str(getattr(item, "TextValue", "") or ""):
            return
    obj.ContentSequence = [*existing, research_comment_item()]


# =======================================================================================
# The check. MOS-SAFE-039's "single function shared by the SEG, SR and SC writers".
# =======================================================================================
def marking_violations(
    obj: Dataset, *, object_kind: ObjectKind, mode: str
) -> tuple[MarkingViolation, ...]:
    """Every mark `MOS-IMG-133`/`136`/`137` requires that this dataset does not carry.

    Returns a tuple rather than raising, and returns ALL of them rather than the first:
    `MOS-SAFE-036` establishes the house style for a safety gate -- "The response MUST
    enumerate every failed condition, not the first" -- and the reason generalises. An
    engineer fixing a writer defect one violation per CI run is an engineer who ships the
    fourth one to a hospital.
    """
    normalised = normalise_mode(mode)
    research = normalised == RESEARCH_ONLY
    out: list[MarkingViolation] = []

    # -- MOS-IMG-133 row 1, in both modes ------------------------------------------------
    series_description = str(getattr(obj, "SeriesDescription", "") or "")
    required_prefix = RUO_PREFIX if research else AI_PREFIX
    if not series_description.startswith(required_prefix):
        out.append(
            MarkingViolation(
                rule="MOS-IMG-136" if research else "MOS-IMG-133",
                attribute="SeriesDescription (0008,103E)",
                expected=f"begins with {required_prefix!r}",
                # The description is platform-authored (`ai_series_description`) and is not
                # a copied source descriptor, so echoing it discloses nothing
                # (`MOS-DATA-065` is about the SOURCE series description).
                observed=series_description or None,
            )
        )
    # In `clinical` mode the RUO prefix is not merely unnecessary, it is WRONG: a clinical
    # object carrying `[AI][RUO] ` tells a PACS administrator the opposite of the truth and
    # `MOS-SAFE-044` requires the two to be distinguishable "by the DICOM marker set
    # alone".
    if not research and series_description.startswith(RUO_PREFIX):
        out.append(
            MarkingViolation(
                rule="MOS-SAFE-044",
                attribute="SeriesDescription (0008,103E)",
                expected=f"a clinical object MUST NOT carry the {RUO_PREFIX!r} prefix",
                observed=series_description,
            )
        )

    # -- MOS-IMG-133 rows 5 and 6, MOS-IMG-134 -------------------------------------------
    if not str(getattr(obj, "Manufacturer", "") or "").strip():
        out.append(
            MarkingViolation(
                rule="MOS-IMG-133",
                attribute="Manufacturer (0008,0070)",
                expected="the AI legal manufacturer, not the scanner vendor",
                observed=None,
            )
        )
    out.extend(_contributing_equipment_violations(obj))

    # -- MOS-IMG-137: the private block. Supplementary, and checked anyway ---------------
    #
    # "It is supplementary: nothing may depend on it, because de-identification downstream
    # will remove it." Nothing here DEPENDS on it -- every rule above stands on standard
    # attributes, which is `MOS-IMG-133`'s whole point. But an object that leaves this
    # platform without it was not written by this platform's writer, and at the moment of
    # the check the block has not yet been anywhere that could strip it.
    out.extend(_private_block_violations(obj, research=research))

    # -- per-object-type ------------------------------------------------------------------
    if object_kind == "SEG":
        out.extend(_seg_violations(obj, research=research))
    elif object_kind == "SR":
        out.extend(_sr_violations(obj, research=research))
    elif object_kind == "SC":
        out.extend(_sc_violations(obj, research=research))
    else:  # pragma: no cover - the Literal makes this unreachable from typed callers
        raise ValueError(f"object_kind {object_kind!r} is not SEG, SR or SC")

    return tuple(out)


def _contributing_equipment_violations(obj: Dataset) -> list[MarkingViolation]:
    """`MOS-IMG-134`, and `MOS-SAFE-048`'s (0018,A001) row, which agree."""
    sequence = list(getattr(obj, "ContributingEquipmentSequence", []) or [])
    if not sequence:
        return [
            MarkingViolation(
                rule="MOS-IMG-134",
                attribute="ContributingEquipmentSequence (0018,A001)",
                expected="one item identifying MedicalOS as processing equipment",
                observed=None,
            )
        ]
    item = sequence[0]
    out: list[MarkingViolation] = []
    purpose = list(getattr(item, "PurposeOfReferenceCodeSequence", []) or [])
    code = str(getattr(purpose[0], "CodeValue", "")) if purpose else ""
    if code != "109102":
        out.append(
            MarkingViolation(
                rule="MOS-IMG-134",
                attribute="ContributingEquipmentSequence[0]"
                ".PurposeOfReferenceCodeSequence",
                expected='DCM 109102 "Processing Equipment"',
                observed=code or None,
            )
        )
    if not str(getattr(item, "ContributionDescription", "") or "").strip():
        out.append(
            MarkingViolation(
                rule="MOS-IMG-134",
                attribute="ContributingEquipmentSequence[0].ContributionDescription",
                expected="names the service and version that produced the object",
                observed=None,
            )
        )
    if not str(getattr(item, "DeviceSerialNumber", "") or "").strip():
        out.append(
            MarkingViolation(
                rule="MOS-IMG-134",
                attribute="ContributingEquipmentSequence[0].DeviceSerialNumber",
                expected="the Deployment id",
                observed=None,
            )
        )
    return out


def _private_block_violations(obj: Dataset, *, research: bool) -> list[MarkingViolation]:
    out: list[MarkingViolation] = []
    try:
        block = obj.private_block(_PRIVATE_GROUP, PRIVATE_CREATOR, create=False)
    except KeyError:
        return [
            MarkingViolation(
                rule="MOS-IMG-137",
                attribute=f"({_PRIVATE_GROUP:04X},0010) PrivateCreator",
                expected=PRIVATE_CREATOR,
                observed=None,
            )
        ]
    ai_derived = _private_value(block, _AI_DERIVED_ELEMENT)
    if ai_derived != "YES":
        out.append(
            MarkingViolation(
                rule="MOS-IMG-137",
                attribute=f"({_PRIVATE_GROUP:04X},1001) AIDerived",
                expected="YES",
                observed=ai_derived,
            )
        )
    mode_value = _private_value(block, _CLINICAL_USE_MODE_ELEMENT)
    expected_mode = RESEARCH_ONLY if research else CLINICAL
    if mode_value != expected_mode:
        out.append(
            MarkingViolation(
                rule="MOS-IMG-136" if research else "MOS-IMG-137",
                attribute=f"({_PRIVATE_GROUP:04X},1002) ClinicalUseMode",
                expected=expected_mode,
                observed=mode_value,
            )
        )
    return out


def _private_value(block: Any, element: int) -> str | None:
    try:
        return str(block[element].value)
    except (KeyError, IndexError, AttributeError):
        return None


def _seg_violations(obj: Dataset, *, research: bool) -> list[MarkingViolation]:
    out: list[MarkingViolation] = []

    image_type = [str(v) for v in (getattr(obj, "ImageType", []) or [])]
    if not image_type or image_type[0] != "DERIVED":
        out.append(
            MarkingViolation(
                rule="MOS-IMG-133",
                attribute="ImageType (0008,0008) value 1",
                expected="DERIVED",
                observed=image_type[0] if image_type else None,
            )
        )

    segments = list(getattr(obj, "SegmentSequence", []) or [])
    if not segments:
        out.append(
            MarkingViolation(
                rule="MOS-IMG-133",
                attribute="SegmentSequence (0062,0002)",
                expected="at least one segment",
                observed=None,
            )
        )
    for index, segment in enumerate(segments):
        algorithm = str(getattr(segment, "SegmentAlgorithmType", "") or "")
        if algorithm != "AUTOMATIC":
            out.append(
                MarkingViolation(
                    rule="MOS-IMG-133",
                    attribute=f"SegmentSequence[{index}].SegmentAlgorithmType (0062,0008)",
                    expected="AUTOMATIC",
                    observed=algorithm or None,
                )
            )

    description = str(getattr(obj, "ContentDescription", "") or "")
    if research and not description.startswith(RUO_CONTENT_DESCRIPTION_PREFIX):
        out.append(
            MarkingViolation(
                rule="MOS-IMG-136",
                attribute="ContentDescription (0070,0081)",
                expected=f"begins {RUO_CONTENT_DESCRIPTION_PREFIX!r}",
                observed=description or None,
            )
        )
    # See defect D6: the VALUE of ContentLabel is not fixed by chapter 4, but VR CS makes
    # an empty one a conformance defect and an unlabelled SEG renders as an anonymous
    # overlay in a third-party viewer.
    if not str(getattr(obj, "ContentLabel", "") or "").strip():
        out.append(
            MarkingViolation(
                rule="MOS-IMG-090",
                attribute="ContentLabel (0070,0080)",
                expected="a non-empty CS label",
                observed=None,
            )
        )
    return out


def _sr_violations(obj: Dataset, *, research: bool) -> list[MarkingViolation]:
    out: list[MarkingViolation] = []

    # MOS-IMG-133 row 4 / MOS-SAFE-049: the device observer carries the AI identity. TID
    # 1002's items live under the root ContentSequence as an OBSERVATION CONTEXT subtree;
    # the platform-level property being checked is that the SR names a DEVICE observer at
    # all, which is what distinguishes an AI document from one a person dictated.
    if not _has_device_observer(obj):
        out.append(
            MarkingViolation(
                rule="MOS-IMG-133",
                attribute="Device observer (TID 1002)",
                expected="a DEVICE Observer Type with the AI service identity",
                observed=None,
            )
        )

    # MOS-SAFE-042 (E6) and MOS-IMG-091: UNVERIFIED at issuance in EVERY mode, and in
    # research_only "No `ResultReview` outcome may change this". The absence of
    # VerifyingObserverSequence is checked as well as the flag, because a VERIFIED-looking
    # observer sequence beside an UNVERIFIED flag is what a viewer renders from.
    flag = str(getattr(obj, "VerificationFlag", "") or "")
    if flag != "UNVERIFIED":
        out.append(
            MarkingViolation(
                rule="MOS-SAFE-042" if research else "MOS-IMG-091",
                attribute="VerificationFlag (0040,A493)",
                expected="UNVERIFIED",
                observed=flag or None,
            )
        )
    if research and list(getattr(obj, "VerifyingObserverSequence", []) or []):
        out.append(
            MarkingViolation(
                rule="MOS-SAFE-042",
                attribute="VerifyingObserverSequence (0040,A073)",
                expected="absent in research_only mode",
                observed="present",
            )
        )

    if research and not _has_research_comment(obj):
        out.append(
            MarkingViolation(
                rule="MOS-IMG-136",
                attribute="root ContentSequence TEXT item",
                expected=(
                    f"a TEXT content item whose value contains "
                    f"{RUO_SR_COMMENT_REQUIRED_SUBSTRING!r}"
                ),
                observed=None,
            )
        )
    return out


def _sc_violations(obj: Dataset, *, research: bool) -> list[MarkingViolation]:
    """`MOS-SAFE-049`'s SC rows and `MOS-SAFE-051`'s banner.

    No SC is written in this release (`jobs.requested_outputs` defaults to `{SEG,SR}` and
    the pipeline builds two objects). The rules are implemented rather than deferred
    because `MOS-SAFE-039` names the SC writer as one of the three this single function
    serves, and a gate that is written the day the third writer lands is a gate that is
    written by whoever is in a hurry.

    `BurnedInAnnotation = YES` is checked as literally true rather than as a formality:
    `MOS-SAFE-051` says the banner "is the only channel by which the warning survives a
    screenshot, a PDF export or a viewer that ignores metadata", and the flag is the only
    machine-readable claim that it is there.
    """
    out: list[MarkingViolation] = []
    image_type = [str(v) for v in (getattr(obj, "ImageType", []) or [])]
    if not image_type or image_type[0] != "DERIVED":
        out.append(
            MarkingViolation(
                rule="MOS-IMG-133",
                attribute="ImageType (0008,0008) value 1",
                expected="DERIVED",
                observed=image_type[0] if image_type else None,
            )
        )
    if str(getattr(obj, "ConversionType", "") or "") != "SYN":
        out.append(
            MarkingViolation(
                rule="MOS-SAFE-049",
                attribute="ConversionType (0008,0064)",
                expected="SYN",
                observed=str(getattr(obj, "ConversionType", "") or "") or None,
            )
        )
    if research and str(getattr(obj, "BurnedInAnnotation", "") or "") != "YES":
        out.append(
            MarkingViolation(
                rule="MOS-SAFE-051",
                attribute="BurnedInAnnotation (0028,0301)",
                expected="YES -- the research banner is burned into the top 40 rows",
                observed=str(getattr(obj, "BurnedInAnnotation", "") or "") or None,
            )
        )
    return out


def _has_device_observer(obj: Dataset) -> bool:
    for item in _walk_content(obj):
        concept = list(getattr(item, "ConceptNameCodeSequence", []) or [])
        if concept and str(getattr(concept[0], "CodeValue", "")) == "121005":
            # (121005, DCM, "Observer Type")
            value = list(getattr(item, "ConceptCodeSequence", []) or [])
            if value and str(getattr(value[0], "CodeValue", "")) == "121007":
                return True  # (121007, DCM, "Device")
        # TID 1002's Device Observer UID is itself sufficient evidence of a device observer
        # and is what MOS-SAFE-049 names first.
        if concept and str(getattr(concept[0], "CodeValue", "")) == "121012":
            return True
    return False


def _has_research_comment(obj: Dataset) -> bool:
    for item in _walk_content(obj):
        if str(getattr(item, "ValueType", "")) != "TEXT":
            continue
        if RUO_SR_COMMENT_REQUIRED_SUBSTRING in str(getattr(item, "TextValue", "") or ""):
            return True
    return False


def _walk_content(obj: Any, depth: int = 0) -> Any:
    """Depth-first over an SR content tree. Bounded, because a cycle would hang the gate."""
    if depth > 12:
        return
    for item in list(getattr(obj, "ContentSequence", []) or []):
        yield item
        yield from _walk_content(item, depth + 1)


def assert_marked(obj: Dataset, *, object_kind: ObjectKind, mode: str) -> None:
    """`MOS-SAFE-039`: raise rather than let an unmarked object be emitted.

    NO PARAMETER DISABLES THIS. "There is no configuration flag that disables it" is a
    requirement about the signature as much as about the body: there is no `strict=`, no
    `warn_only=`, no environment variable, and `tests/integration/test_safety_envelope.py`
    greps this module for all three. A flag that exists is a flag that is set to False in
    an incident at 02:00 and never set back.
    """
    violations = marking_violations(obj, object_kind=object_kind, mode=mode)
    if not violations:
        return
    summary = "; ".join(f"{v.attribute}: expected {v.expected}" for v in violations)
    raise MarkingAbsent(
        violations,
        f"{object_kind} in clinical_use_mode={normalise_mode(mode).lower()} is missing "
        f"{len(violations)} required marking(s) and MUST NOT be emitted "
        f"(MOS-SAFE-039): {summary}",
    )
