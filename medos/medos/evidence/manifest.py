# SPDX-License-Identifier: Apache-2.0
"""The three manifest formats. Chapter 7 sections 7.3.2, 7.4.1 and 7.5.

A manifest is the object that makes a cohort, a split or a reference standard
CONTENT-ADDRESSED rather than merely named. `MOS-EVID-016` is the rule this module
exists to make structural: "A DatasetVersion MUST NOT be created by reference to a live
query, a folder path, a DICOM query filter, or a database view. Only an explicit,
materialised list of instances is a DatasetVersion."

So `SeriesRecord` below is a value, not a query, and `seal_dataset_version()` takes a
sequence of them. There is no overload that takes a filter, and adding one would defeat
every downstream guarantee: a report cites a digest, and a digest over a query is a digest
over whatever the database happened to hold at read time.

THREE SORT ORDERS, EACH DECLARED BY ITS OWNING REQUIREMENT
----------------------------------------------------------
MOS-EVID-017  dataset manifest: `(patient_key, study_instance_uid, series_instance_uid)`,
              byte-wise ascending, one line per SERIES.
MOS-EVID-030  split manifest: ascending by `patient_key`, one line per PATIENT.
MOS-EVID-040  annotation manifest: `(patient_key, study_instance_uid,
              series_instance_uid)`, one line per annotated series.

Byte-wise ascending, not locale-aware: `sorted()` on `str` in Python orders by Unicode
code point, and every component of every key here is ASCII (`pk_` + base32, or a DICOM
UID, which PS3.5 restricts to digits and `.`), so code-point order and byte order
coincide. A locale-aware comparison would make the digest a function of the server's
`LC_COLLATE`, which is the same class of defect as digesting a dict in insertion order.

TWO MEMBERS THIS MODULE ADDS TO CHAPTER 7'S DATASET LINE, AND WHY
-----------------------------------------------------------------
`institution_key` and `study_year` are emitted at the TOP LEVEL of the dataset manifest
line, beside `patient_key`. Chapter 7 section 7.3.2's example does not show them, and
chapter 17 requires them:

  * `MOS-TRAIN-088` C1 needs the per-patient share by `institution_key`, C6 the share by
    `study_year` and C7 the `institution_key` cardinality;
  * `MOS-TRAIN-093` requires the stratification report to be "recomputable from the
    sealed manifest, so that a reader can verify it offline from the exported bundle".

A fact that must be recomputable from the manifest must be IN the manifest. They are
placed at the top level rather than inside `acquisition` because `MOS-EVID-020` says
`convolution_kernel_class` is "the only derived field" of `acquisition.*`, and both of
these are derived (an HMAC of `InstitutionName`, the year of `StudyDate`). Top level is
where `patient_key` -- the other pseudonymised header value -- already lives.
REPORTED as an addition to the chapter-7 line format.

`accession_number_hash` is deliberately NOT in the dataset manifest line: chapter 7
section 7.3.2's format does not carry it, and a reader verifying a digest offline must be
able to reproduce the line from the published format. It lives on the
`dataset_cases` row, where leakage check L5 reads it, and that row is sealed by trigger.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Acquisition",
    "SeriesRecord",
    "dataset_manifest_lines",
    "split_manifest_lines",
    "annotation_manifest_lines",
    "ACQUISITION_FIELDS",
]

# The `acquisition` members of chapter 7 section 7.3.2's line format, in one place so the
# manifest writer and the `acquisition_profile` computer cannot drift apart.
# MOS-EVID-020: copied from the source header WITHOUT imputation; a missing value is JSON
# null, never a default.
ACQUISITION_FIELDS: tuple[str, ...] = (
    "slice_thickness_mm",
    "pixel_spacing_mm",
    "convolution_kernel",
    "convolution_kernel_class",
    "manufacturer",
    "manufacturer_model_name",
    "kvp",
    "contrast_phase",
    "z_coverage_mm",
    "image_type",
    "patient_age_years",
    "patient_sex",
    "body_part_examined",
)


@dataclass(frozen=True)
class Acquisition:
    """The `acquisition` sub-object. Every member nullable; none defaulted. MOS-EVID-020.

    `convolution_kernel_class` is the ONLY derived member, and its mapping table version
    is recorded once per cohort in `acquisition_profile.kernel_class_map_version` rather
    than once per line -- a per-line copy of a cohort-wide constant is a place for two
    versions to coexist inside one sealed manifest.
    """

    slice_thickness_mm: float | None = None
    pixel_spacing_mm: tuple[float, float] | None = None
    convolution_kernel: str | None = None
    convolution_kernel_class: str | None = None
    manufacturer: str | None = None
    manufacturer_model_name: str | None = None
    kvp: float | None = None
    contrast_phase: str | None = None
    z_coverage_mm: float | None = None
    image_type: tuple[str, ...] | None = None
    patient_age_years: int | None = None
    patient_sex: str | None = None
    body_part_examined: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "slice_thickness_mm": self.slice_thickness_mm,
            "pixel_spacing_mm": (
                list(self.pixel_spacing_mm) if self.pixel_spacing_mm is not None else None
            ),
            "convolution_kernel": self.convolution_kernel,
            "convolution_kernel_class": self.convolution_kernel_class,
            "manufacturer": self.manufacturer,
            "manufacturer_model_name": self.manufacturer_model_name,
            "kvp": self.kvp,
            "contrast_phase": self.contrast_phase,
            "z_coverage_mm": self.z_coverage_mm,
            "image_type": list(self.image_type) if self.image_type is not None else None,
            "patient_age_years": self.patient_age_years,
            "patient_sex": self.patient_sex,
            "body_part_examined": self.body_part_examined,
        }


@dataclass(frozen=True)
class SeriesRecord:
    """One SERIES of one study of one patient: one manifest line, one `dataset_cases` row.

    `sop_instance_uids` MUST list every instance in the series in the canonical slice
    order of chapter 4, and its length MUST equal `instance_count` (`MOS-EVID-017`). That
    is asserted in `__post_init__` rather than only by the database CHECK, because the
    manifest is digested BEFORE the row is written and a digest over a wrong line is a
    permanent, content-addressed record of the wrong thing.

    `series_pixel_digest` is `MOS-EVID-018`'s digest over STORED pixel values, computed by
    the caller that holds the pixels -- this module never opens a DICOM file. Keeping the
    computation at the retrieval boundary is what lets `MOS-EVID-018`'s promise hold:
    dataset identity does not change when the geometry code changes, because no geometry
    code ran.

    `dhash64` is the 64-bit dHash of the normalised mid-axial slice (`MOS-EVID-034` L4).
    It is optional here because a cohort may be sealed from a manifest without pixels in
    reach; when it is absent, L4 reports `skipped` with an explicit reason at freeze time
    rather than `pass`, per `MOS-EVID-037`.
    """

    patient_key: str
    study_instance_uid: str
    series_instance_uid: str
    modality: str
    sop_class_uid: str
    sop_instance_uids: tuple[str, ...]
    series_pixel_digest: str
    lossy_compressed: bool = False
    acquisition: Acquisition = field(default_factory=Acquisition)
    institution_key: str | None = None
    study_year: int | None = None
    accession_number_hash: str | None = None
    corpus_generation: int = 0
    dhash64: int | None = None

    def __post_init__(self) -> None:
        if not self.sop_instance_uids:
            raise ValueError(
                f"series {self.series_instance_uid} has no instances; a series with no "
                "SOP Instance UIDs is not a manifest line (MOS-EVID-017)"
            )
        if len(set(self.sop_instance_uids)) != len(self.sop_instance_uids):
            raise ValueError(
                f"series {self.series_instance_uid} repeats a SOP Instance UID; "
                "`sop_instance_uids` is the canonical slice order, not a multiset "
                "(MOS-EVID-017)"
            )
        if self.corpus_generation not in (0, 1):
            # MOS-TRAIN-087: a case at generation 2 or above MUST NOT be used in any
            # partition, so a sealed cohort cannot contain one.
            raise ValueError(
                f"series {self.series_instance_uid} has corpus_generation="
                f"{self.corpus_generation}; a case at generation 2 or above MUST NOT be "
                "used in any partition (MOS-TRAIN-087)"
            )

    @property
    def instance_count(self) -> int:
        return len(self.sop_instance_uids)

    @property
    def case_key(self) -> str:
        """The study-level case handle. See `0006_evidence.up.sql` section 4.

        Chapter 12 section 12.12 keys `dataset_cases`, `evaluation_case_metrics` and
        `evaluation_case_scores` on `case_key` and no chapter defines it. Defined here,
        in this module only (CONTRACT.md section 0), as the study: a case is one study of
        one patient, which is the unit an evaluation scores and the unit `MOS-EVID-046`
        means by "a case present in the split but absent from the annotation manifest".
        REPORTED so that the chapter that should own it can adopt or correct it.
        """
        return self.study_instance_uid

    @property
    def sort_key(self) -> tuple[str, str, str]:
        """MOS-EVID-017's canonical order."""
        return (self.patient_key, self.study_instance_uid, self.series_instance_uid)

    def manifest_line(self) -> dict[str, Any]:
        """Chapter 7 section 7.3.2's line, plus the two members this module documents."""
        return {
            "patient_key": self.patient_key,
            "institution_key": self.institution_key,
            "study_instance_uid": self.study_instance_uid,
            "study_year": self.study_year,
            "series_instance_uid": self.series_instance_uid,
            "modality": self.modality,
            "sop_class_uid": self.sop_class_uid,
            "instance_count": self.instance_count,
            "sop_instance_uids": list(self.sop_instance_uids),
            "series_pixel_digest": self.series_pixel_digest,
            "lossy_compressed": self.lossy_compressed,
            "acquisition": self.acquisition.as_json(),
        }


def dataset_manifest_lines(records: Iterable[SeriesRecord]) -> list[dict[str, Any]]:
    """Sorted, canonical dataset manifest lines. MOS-EVID-017, MOS-TRAIN-208 step 6.

    Sorting happens HERE, once, and `manifest_digest()` never sorts -- so there is exactly
    one place that decides the order, and a caller who wants a different one has to change
    a requirement rather than a call site.
    """
    return [r.manifest_line() for r in sorted(records, key=lambda r: r.sort_key)]


def split_manifest_lines(
    assignments: Sequence[tuple[str, str]],
    *,
    folds: dict[str, int] | None = None,
    strata: dict[str, dict[str, Any]] | None = None,
    exclusion_reasons: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Chapter 7 `MOS-EVID-030`'s split lines, sorted ascending by `patient_key`.

    `assignments` is `[(patient_key, partition), ...]`. It is a SEQUENCE OF PAIRS and not
    a `dict[patient_key, partition]` on purpose: a dict cannot represent the defect L1
    exists to catch. One patient assigned to both `train` and `test` collapses to a single
    entry in a dict and the check then has nothing to find -- the bug would be silently
    repaired by the argument type, which is the worst place to repair it.

    `fold` is the optional cross-validation fold index WITHIN a partition. `MOS-EVID-030`:
    "it is not a synonym for `partition` and MUST NOT be used as one."
    """
    folds = folds or {}
    strata = strata or {}
    exclusion_reasons = exclusion_reasons or {}
    lines: list[dict[str, Any]] = []
    for pk, partition in sorted(assignments, key=lambda a: (a[0], a[1])):
        line: dict[str, Any] = {
            "patient_key": pk,
            "partition": partition,
            "fold": folds.get(pk),
            "stratum": strata.get(pk),
        }
        if partition == "excluded":
            # MOS-EVID-031: a split covering a subset MUST declare the excluded patients
            # explicitly with a reason; silent omission is a write-time error.
            line["exclusion_reason"] = exclusion_reasons.get(pk)
        lines.append(line)
    return lines


def annotation_manifest_lines(
    entries: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Chapter 7 `MOS-EVID-040`'s annotation lines, in `(patient, study, series)` order.

    Each entry is the line as section 7.5 prints it: `patient_key`,
    `study_instance_uid`, `series_instance_uid`, a `reference` object, a `per_reader`
    array and an `inter_reader` object. This function orders and returns them; it does
    not invent members, because the reference standard's content is the annotating
    pipeline's (`MOS-TRAIN-108`, `MOS-TRAIN-109`) and not this module's.
    """
    return sorted(
        (dict(e) for e in entries),
        key=lambda e: (
            str(e["patient_key"]),
            str(e["study_instance_uid"]),
            str(e["series_instance_uid"]),
        ),
    )
