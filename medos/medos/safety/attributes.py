# SPDX-License-Identifier: Apache-2.0
"""The study attributes an `ApplicabilityEnvelope` is evaluated against.

`MOS-EVID-095`'s manifest constrains nine attributes. Every one of them is a field chapter
3 already defines: `MOS-DATA-059`'s per-series triage record and `MOS-DATA-061`'s
study-level fields, with the derivations of `MOS-DATA-060`. This module extracts exactly
those nine from the DICOM headers of the series a job selected, and nothing else.

WHY THIS IS NOT "TRIAGE"
------------------------
Chapter 3's triage projection is a cached, versioned, study-wide table (`MOS-DATA-062`'s
`triage_spec_version`, `MOS-DATA-063`'s cache key) that runs on QIDO metadata before any
pixel is pulled (`MOS-DATA-058`). None of that exists yet, and building it is chapter 3's
deliverable, not this one. What this module does is the narrow thing the envelope needs:
read the headers of the instances the pipeline has ALREADY pulled to disk and derive the
nine attributes from them, using `MOS-DATA-060`'s rules verbatim so that when the real
triage projection lands the two agree by construction rather than by coincidence.

`KERNEL_CLASSES_VERSION` and `BODY_PARTS_VERSION` below are the versions of the two data
files `MOS-DATA-062` folds into `triage_spec_version`. They are stated so that the day
those files move out of Python and into `kernel_classes.yaml` / `body_parts.yaml`, the
move is a data migration with a version to compare against and not a rewrite.

PHI. CONTRACT.md section 11 and `MOS-DATA-065`
----------------------------------------------
`study_description`, `series_description` and `protocol_name` are the only free-text
attributes triage retains, and `MOS-DATA-065` forbids copying them into "metric labels,
span attributes or event payloads". This module READS `series_description` and
`protocol_name` -- `MOS-DATA-060.5` derives `contrast_phase` from them by regex -- and
RETURNS NEITHER. `StudyAttributes` holds nine values: five numbers, three enum members and
one vendor string. `contrast_phase_matched_on` records WHICH field matched as a field
NAME, never its content. Nothing this module returns can be written into a log line, an
event payload or an `envelope_decisions` row and disclose a patient.

`patient_age_years` is a derived integer, is in `MOS-DATA-061`'s study-level triage field
list, and is what `MOS-EVID-095`'s envelope constrains. It is not a date of birth and it is
not `PatientAge`'s raw string.

Spec: MOS-DATA-059, MOS-DATA-060, MOS-DATA-061, MOS-DATA-062, MOS-DATA-065, MOS-EVID-095,
MOS-EVID-096, CONTRACT.md section 11.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pydicom
from pydicom.dataset import Dataset

__all__ = [
    "KERNEL_CLASSES_VERSION",
    "BODY_PARTS_VERSION",
    "PHASE_REGEXES_VERSION",
    "StudyAttributes",
    "study_attributes",
    "attributes_from_paths",
    "kernel_class",
    "normalise_body_part",
    "contrast_phase",
    "patient_age_years",
]

# `MOS-DATA-060.2`'s `kernel_classes.yaml` declares `version: 4`. Carried so
# `triage_spec_version` (`MOS-DATA-062`) has something to compose from.
KERNEL_CLASSES_VERSION = 4
BODY_PARTS_VERSION = 1
PHASE_REGEXES_VERSION = 1


# =======================================================================================
# MOS-DATA-060.2 -- kernel_classes.yaml, transcribed rule for rule.
#
# Keyed on `(manufacturer, convolution_kernel)`. Order matters: the first matching rule
# wins, so the SIEMENS numeric-bucket rule sits before the SIEMENS explicit list exactly as
# the requirement writes them, and `Bl57` -- which the numeric regex does not match --
# falls through to the list that names it.
# =======================================================================================
_KERNEL_RULES: tuple[dict[str, Any], ...] = (
    {
        "manufacturer_regex": r"^SIEMENS",
        "kernel_regex": r"^B[rfv]?(\d{2})[fsd]?$",
        "numeric_group": 1,
        "buckets": (("<40", "SOFT"), ("40-49", "STANDARD"), (">=50", "SHARP")),
    },
    {
        "manufacturer_regex": r"^SIEMENS",
        "kernel_in": ("Bl57", "Bl64", "Bl69", "Br69"),
        "class": "SHARP",
    },
    {"manufacturer_regex": r"^GE", "kernel_in": ("SOFT",), "class": "SOFT"},
    {"manufacturer_regex": r"^GE", "kernel_in": ("STANDARD", "STND"), "class": "STANDARD"},
    {
        "manufacturer_regex": r"^GE",
        "kernel_in": ("LUNG", "BONE", "BONEPLUS", "EDGE", "DETAIL"),
        "class": "SHARP",
    },
    {"manufacturer_regex": r"^Philips", "kernel_in": ("A", "B", "C"), "class": "SOFT"},
    {"manufacturer_regex": r"^Philips", "kernel_in": ("D", "EB", "FC"), "class": "STANDARD"},
    {
        "manufacturer_regex": r"^Philips",
        "kernel_in": ("L", "YA", "YB", "YC", "YD"),
        "class": "SHARP",
    },
    {
        "manufacturer_regex": r"^CANON|^TOSHIBA",
        "kernel_in": ("FC01", "FC02", "FC03", "FC07", "FC08"),
        "class": "SOFT",
    },
    {
        "manufacturer_regex": r"^CANON|^TOSHIBA",
        "kernel_in": ("FC30", "FC50", "FC51", "FC52", "FC55"),
        "class": "SHARP",
    },
)
_KERNEL_DEFAULT = "UNKNOWN"

# `MOS-DATA-060.1`'s synonym table. Non-exhaustive by design: "An unmapped non-empty value
# is kept verbatim; an empty value yields `null`, which is distinct from a mismatch."
_BODY_PART_SYNONYMS: Mapping[str, str] = {
    "THORAX": "CHEST",
    "TORAX": "CHEST",
    "LUNG": "CHEST",
    "LUNGS": "CHEST",
    "CHESTABDOMEN": "CHEST",
    "CHEST_ABDOMEN": "CHEST",
}

# `MOS-DATA-060.5`'s "tenant-configured phase regex set", with the four phases the
# requirement names. Matched against `series_description` or `protocol_name` -- the TEXT
# never leaves this module.
_PHASE_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PULMONARY_ARTERIAL", re.compile(r"(?i)\b(cta?\s*pulm|pulmonary\s*arter|pe\s*protocol)")),
    ("ARTERIAL", re.compile(r"(?i)\b(arterial|art\s*phase|early\s*phase)")),
    ("PORTAL_VENOUS", re.compile(r"(?i)\b(portal|portal[-_\s]*venous|pv\s*phase|venous)")),
    ("DELAYED", re.compile(r"(?i)\b(delay|late\s*phase|equilibrium)")),
)

_AGE_RE = re.compile(r"^(\d{1,3})Y$")


def kernel_class(manufacturer: str | None, convolution_kernel: str | None) -> str | None:
    """`MOS-DATA-060.2`. Returns `None` when the kernel attribute is ABSENT.

    The distinction between `None` and `"UNKNOWN"` is load-bearing and is the one thing
    this function exists to get right:

      * `None`  -- (0018,1210) ConvolutionKernel is not in the header. `MOS-EVID-096` makes
                   that `MARGINAL` with reason `attribute_absent`, "never `IN`".
      * `UNKNOWN` -- the kernel IS declared and no rule maps it. `MOS-DATA-060.2`: "`UNKNOWN`
                   MUST NOT satisfy a `kernel_class_in` constraint". It is an observed
                   value that simply is not in the envelope's `values`, so it lands `OUT`
                   with `envelope.kernel_not_supported`, which is the honest answer: the
                   platform knows what kernel was used and knows it is not one the service
                   was measured on.

    Collapsing the two would report a 2026 photon-counting scanner's unrecognised kernel as
    "we could not tell", which reads to a reviewer as a metadata gap rather than as an
    unvalidated reconstruction.
    """
    if convolution_kernel is None or str(convolution_kernel).strip() == "":
        return None
    kernel = str(convolution_kernel).strip()
    vendor = (manufacturer or "").strip()
    for rule in _KERNEL_RULES:
        if not re.search(rule["manufacturer_regex"], vendor):
            continue
        if "kernel_in" in rule:
            if kernel.upper() in {k.upper() for k in rule["kernel_in"]}:
                return str(rule["class"])
            continue
        match = re.match(str(rule["kernel_regex"]), kernel)
        if match is None:
            continue
        number = int(match.group(int(rule["numeric_group"])))
        for bucket, label in rule["buckets"]:
            if bucket.startswith("<") and number < int(bucket[1:]):
                return str(label)
            if bucket.startswith(">="):
                if number >= int(bucket[2:]):
                    return str(label)
            elif "-" in bucket:
                low, high = bucket.split("-", 1)
                if int(low) <= number <= int(high):
                    return str(label)
    return _KERNEL_DEFAULT


def normalise_body_part(value: str | None) -> str | None:
    """`MOS-DATA-060.1`: upper-case, strip, map through the synonym table, else verbatim."""
    if value is None:
        return None
    text = "".join(str(value).split()).upper()
    if not text:
        return None
    return _BODY_PART_SYNONYMS.get(text, text)


def contrast_phase(
    *,
    contrast_agent: str | None,
    series_description: str | None,
    protocol_name: str | None,
) -> tuple[str, str | None]:
    """`MOS-DATA-060.5`. Returns `(phase, matched_on)`; `matched_on` is a FIELD NAME.

    "`NONE` if `contrast_present` is false; otherwise `UNKNOWN` unless the tenant-configured
    phase regex set matches `series_description` or `protocol_name`."

    The requirement's refusal is implemented as a refusal: it "deliberately refuses to infer
    phase from (0018,1042) ContrastBolusStartTime versus (0008,0032) AcquisitionTime",
    because "the arithmetic is scanner- and protocol-dependent and a wrong phase silently
    routes a study to a model validated on a different one". Neither tag is read here, and
    adding them would be a specification change rather than an improvement.

    `matched_on` is returned so the decision record can say WHERE the phase came from
    without carrying the text that said it (`MOS-DATA-065`).
    """
    if contrast_agent is None or str(contrast_agent).strip() == "":
        return "NONE", None
    for field_name, text in (
        ("series_description", series_description),
        ("protocol_name", protocol_name),
    ):
        if not text:
            continue
        for phase, pattern in _PHASE_REGEXES:
            if pattern.search(str(text)):
                return phase, field_name
    return "UNKNOWN", None


def patient_age_years(value: Any) -> int | None:
    """`MOS-DATA-059`: "derived: parsed from the `nnnY` form, null otherwise".

    Null and not a best-effort conversion of `045M` or `012W`: a months-or-weeks age is a
    neonate or an infant, every envelope in this platform declares an adult lower bound,
    and silently converting 45 months to "3" would hand a paediatric study to a service
    that declares `min: 18` as a value that then reads `OUT` for the wrong reason -- or,
    worse, rounds into range. `None` lands `MARGINAL`/`attribute_absent`, which is the
    outcome `MOS-EVID-096` prescribes for an attribute the platform could not determine.
    """
    if value is None:
        return None
    match = _AGE_RE.match(str(value).strip().upper())
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class StudyAttributes:
    """The nine attributes `MOS-EVID-095` constrains, plus the provenance of two of them.

    PHI-FREE by construction, and that is checked rather than asserted:
    `tests/integration/test_safety_envelope.py` reads a real study and fails if any member
    of `as_mapping()` is a string that appears in the source `PatientName`, `PatientID`,
    `AccessionNumber`, `StudyDescription` or `SeriesDescription`.
    """

    slice_thickness_mm: float | None
    pixel_spacing_mm_max: float | None
    z_coverage_mm: float | None
    instance_count: int
    patient_age_years: int | None
    manufacturer: str | None
    convolution_kernel_class: str | None
    contrast_phase: str
    body_part_examined: str | None
    # Not constrainable. Carried for the decision record, per the note in `evaluate`.
    contrast_phase_matched_on: str | None = None
    kernel_classes_version: int = KERNEL_CLASSES_VERSION
    body_parts_version: int = BODY_PARTS_VERSION

    def as_mapping(self) -> dict[str, Any]:
        """The mapping `medos.safety.envelope.evaluate` takes."""
        return asdict(self)


def _first(values: Iterable[Any]) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _median(values: Sequence[float]) -> float | None:
    clean = sorted(v for v in values if v is not None and not math.isnan(v))
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return float(clean[mid])
    return float((clean[mid - 1] + clean[mid]) / 2.0)


def _mode(values: Sequence[str]) -> str | None:
    """`MOS-DATA-059`'s "copied, mode" aggregation, with a deterministic tie-break.

    Ties broken lexicographically rather than by first-seen: `MOS-DATA-081` requires
    selection to be "a pure function ... MUST NOT read ... the database ordering of the
    input rows", and the same reasoning applies to an attribute the envelope decision turns
    on. Two manufacturers in one series is a mixed-series defect either way; what must not
    happen is that two runs over the same instances disagree about which one it was.
    """
    clean = [v.strip() for v in values if v and str(v).strip()]
    if not clean:
        return None
    counts: dict[str, int] = {}
    for value in clean:
        counts[value] = counts.get(value, 0) + 1
    top = max(counts.values())
    return sorted(k for k, n in counts.items() if n == top)[0]


def _slice_normal(orientation: Sequence[float]) -> tuple[float, float, float] | None:
    if orientation is None or len(orientation) < 6:
        return None
    r = [float(x) for x in orientation[:3]]
    c = [float(x) for x in orientation[3:6]]
    return (
        r[1] * c[2] - r[2] * c[1],
        r[2] * c[0] - r[0] * c[2],
        r[0] * c[1] - r[1] * c[0],
    )


def study_attributes(headers: Sequence[Dataset]) -> StudyAttributes:
    """Derive the nine attributes from the headers of one selected series.

    `headers` are `pydicom` datasets; pixel data is not needed and callers are expected to
    have read them with `stop_before_pixels=True` (`attributes_from_paths` does). The
    envelope decision is metadata-only, which is the same property `MOS-DATA-058` requires
    of triage -- "no pixel pull at triage" -- carried into the one place in this release
    that re-reads the headers.
    """
    if not headers:
        raise ValueError(
            "no instances: an envelope decision over an empty series would evaluate every "
            "attribute to attribute_absent and report MARGINAL, which reads as 'we looked' "
            "when nothing was looked at"
        )

    thickness = _median([_float_of(h, "SliceThickness") for h in headers])

    spacings: list[float] = []
    for header in headers:
        pixel_spacing = getattr(header, "PixelSpacing", None)
        if pixel_spacing is not None and len(pixel_spacing) >= 2:
            spacings.append(max(float(pixel_spacing[0]), float(pixel_spacing[1])))
    pixel_spacing_max = max(spacings) if spacings else None

    manufacturer = _mode([str(getattr(h, "Manufacturer", "") or "") for h in headers])
    kernel_raw = _first(_kernel_of(h) for h in headers)

    return StudyAttributes(
        slice_thickness_mm=thickness,
        pixel_spacing_mm_max=pixel_spacing_max,
        z_coverage_mm=_z_coverage(headers, thickness),
        instance_count=len(headers),
        patient_age_years=patient_age_years(
            _first(getattr(h, "PatientAge", None) for h in headers)
        ),
        manufacturer=manufacturer,
        convolution_kernel_class=kernel_class(manufacturer, kernel_raw),
        **_phase_fields(headers),
        body_part_examined=normalise_body_part(
            _first(getattr(h, "BodyPartExamined", None) for h in headers)
        ),
    )


def _phase_fields(headers: Sequence[Dataset]) -> dict[str, Any]:
    phase, matched_on = contrast_phase(
        contrast_agent=_first(getattr(h, "ContrastBolusAgent", None) for h in headers),
        series_description=_first(getattr(h, "SeriesDescription", None) for h in headers),
        protocol_name=_first(getattr(h, "ProtocolName", None) for h in headers),
    )
    return {"contrast_phase": phase, "contrast_phase_matched_on": matched_on}


def _kernel_of(header: Dataset) -> str | None:
    kernel = getattr(header, "ConvolutionKernel", None)
    if kernel is None:
        return None
    # VR CS with VM 1-n: Siemens writes a two-element value for some recons.
    if isinstance(kernel, (list, tuple)) or (
        hasattr(kernel, "__len__") and not isinstance(kernel, str)
    ):
        members = [str(k).strip() for k in kernel if str(k).strip()]
        return members[0] if members else None
    text = str(kernel).strip()
    return text or None


def _float_of(header: Dataset, name: str) -> float | None:
    value = getattr(header, name, None)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _z_coverage(headers: Sequence[Dataset], thickness: float | None) -> float | None:
    """`MOS-DATA-060.3`: `z_extent_mm = max(proj) - min(proj) + slice_thickness_mm`.

    Projected onto the slice normal `n = r x c` rather than onto the patient Z axis,
    because a coronal or oblique acquisition's coverage along its own stack direction is
    what the envelope is about. The same construction `medos.core.geometry` uses to sort
    slices; it is repeated here rather than imported because `build_canonical_volume`
    returns a built volume and this runs on headers, and the four lines of arithmetic are
    cheaper to read twice than a shared helper whose two callers need different inputs.
    """
    normal = _first(_slice_normal(getattr(h, "ImageOrientationPatient", None)) for h in headers)
    if normal is None:
        return None
    projections: list[float] = []
    for header in headers:
        position = getattr(header, "ImagePositionPatient", None)
        if position is None or len(position) < 3:
            continue
        projections.append(
            sum(float(position[i]) * normal[i] for i in range(3))
        )
    if not projections:
        return None
    return float(max(projections) - min(projections) + (thickness or 0.0))


def attributes_from_paths(paths: Sequence[Path | str]) -> StudyAttributes:
    """`study_attributes` over Part 10 files, headers only.

    `stop_before_pixels=True`: an envelope decision that pulled pixel data would cost the
    whole series a second time for nine numbers, and would break the property
    `MOS-DATA-058` states of triage.
    """
    headers = [
        pydicom.dcmread(str(path), stop_before_pixels=True, force=False) for path in paths
    ]
    return study_attributes(headers)
