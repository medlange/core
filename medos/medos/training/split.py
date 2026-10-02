# SPDX-License-Identifier: Apache-2.0
"""`MOS-TRAIN-112`'s `assign()`. The one function rows R25 and R26 both stand on.

`medos/api/v1/routes.yaml` recorded this as absent under `engine_absent:` on both rows:
"MOS-TRAIN-112's `assign()` HAS NO IMPLEMENTATION IN THIS REPOSITORY. Chapter 17 prints a
reference implementation; `medos/medos/evidence/repo.py::freeze_split` takes `assignments` from
its caller and generates none, and a repository-wide search for an assignment generator
returns nothing." This module is that gap closed, and it is deliberately the SMALLEST
module that closes it: one pure function, one preview projection over it, and no
database.

FOUR PARAMETERS THIS FUNCTION DOES NOT HAVE, AND THE REQUIREMENT THAT REMOVES EACH
----------------------------------------------------------------------------------
  `seed`                      `MOS-EVID-028`. "A seed is an input to that one evaluation
                              and a fact recorded in `assignment_method`; it is never the
                              split." The stable input here is `seed_label`, a STRING
                              recorded verbatim in `assignment_method`, so the manifest is
                              the split and the label is provenance. A numeric `seed`
                              parameter would invite a caller to re-roll until the test
                              partition looked kind.
  `min_days_between_studies`  `MOS-TRAIN-117`. "There is no interval after which two
                              studies of one patient become independent observations, and
                              a split tool that offers a 'minimum days between studies'
                              option is offering a way to defeat this check."
  `study_level_split`         `MOS-TRAIN-117`, same sentence. The unit of assignment is
                              `patient_key` and nothing else (`MOS-EVID-029`).
  `allow_same_patient`        `MOS-TRAIN-117`, same sentence.

`tests/integration/test_curation.py` already greps `seal_from_batch` for all four;
`tests/integration/test_api_curation.py` greps THIS function for them too, because the
grep that protects the caller does not protect the generator the caller acquired.

WHY THE REFERENCE IMPLEMENTATION IS FOLLOWED RATHER THAN IMPROVED
------------------------------------------------------------------
Chapter 17 prints the algorithm in full. It is followed line for line -- the same bucket
key, the same `sha256(f"{seed_label}|{pk}")` ordering, the same `(n * upto) // 100`
cut arithmetic -- because `MOS-UI-116`'s argument applies with more force to the server
than to the browser: two implementations of a stratified split disagree on exactly the
strata that sit near a cut, and those are the strata where the subgroup floor
(`MOS-EVID-088`) is decided. A "better" rounding rule here would silently change which
patients a previously-frozen split would have held, and `MOS-EVID-013` makes the frozen
one immutable -- so the two could never be reconciled afterwards.

ONE DEPARTURE, STATED, AND IT IS A RETURN TYPE AND NOT AN ALGORITHM. The reference
returns manifest LINES (`{"patient_key", "partition", "fold", "stratum"}`);
`medos/medos/evidence/repo.py::freeze_split` takes `assignments: Sequence[tuple[str, str]]`
plus a separate `strata` mapping, and takes PAIRS rather than a dict for the reason its
own docstring gives -- a dict cannot represent the defect L1 exists to catch. `assign()`
below returns an `Assignment` carrying both shapes off one evaluation, so no caller
re-derives either from the other. Every arithmetic step is the reference's, unchanged.

WHERE INTEGER DIVISION SENDS THE REMAINDER, MEASURED RATHER THAN ASSUMED. The cut
percentages sum to 100, so the final slice absorbs whatever the earlier `//` discarded
and no patient is ever left unplaced. The absorbing partition is `test`: a stratum of 7
splits 4 / 1 / 2. The block comment at that line records why it is not corrected.

Spec: MOS-EVID-028, MOS-EVID-029, MOS-EVID-031, MOS-EVID-033, MOS-TRAIN-112,
MOS-TRAIN-113, MOS-TRAIN-114, MOS-TRAIN-117, MOS-UI-116, MOS-UI-117, MOS-UI-136.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "PARTITIONS",
    "STRATIFIED_BY",
    "TEST_PARTITION_FLOOR",
    "Assignment",
    "assign",
    "assignment_method",
    "seed_label_for",
    "stratum_of",
]

#: `MOS-TRAIN-112`'s 70/10/20, in the requirement's own order. Chapter 7's four-value
#: partition vocabulary is `train`/`tune`/`test`/`excluded` (`MOS-EVID-031`,
#: `MOS-EVID-033`); `excluded` is not generated here because `MOS-UI-136` forbids this
#: surface offering split-level exclusion -- "exclusion before seal is a
#: `CurationDecision` and nothing else".
PARTITIONS: Final[tuple[tuple[str, int], ...]] = (
    ("train", 70),
    ("tune", 10),
    ("test", 20),
)

#: `MOS-TRAIN-114`'s floor, and the number `MOS-UI-142`'s refusal does arithmetic with.
TEST_PARTITION_FLOOR: Final[int] = 30

#: What the split is stratified on, recorded on the frozen row as `stratified_by`.
#:
#: `MOS-TRAIN-113` requires the capability's declared CLINICAL stratum -- for
#: `pleural_effusion`, the `reference_volume_ml` decile of `MOS-EVID-067` -- and SHOULD
#: additionally stratify on `acquisition.manufacturer` and a `slice_thickness_mm` band.
#: The clinical stratum IS NOT AVAILABLE HERE and its absence is recorded rather than
#: approximated: the decile is a property of the REFERENCE STANDARD, and at seal time the
#: `AnnotationSet` does not exist yet (row R29 is frozen against the sealed
#: `DatasetVersion`, which this split is frozen alongside). Substituting a proxy for it
#: would produce a `stratified_by` naming a stratum nobody stratified on, which is worse
#: than a shorter list: `MOS-TRAIN-113` says "`stratified_by` MUST record whatever was
#: used". REPORTED as an honest gap on the seal route rather than filled with a guess.
STRATIFIED_BY: Final[tuple[str, ...]] = ("manufacturer", "slice_thickness_band")

#: The `slice_thickness_mm` bands of `STRATIFIED_BY`. Fixed here rather than derived from
#: the cohort, because a band edge computed from the data moves when the data moves, and
#: two seals of overlapping cohorts would then stratify on different things while
#: recording the same word.
_THICKNESS_BANDS: Final[tuple[tuple[float, str], ...]] = (
    (1.0, "<=1.0mm"),
    (2.0, "<=2.0mm"),
    (3.0, "<=3.0mm"),
    (5.0, "<=5.0mm"),
)


def _thickness_band(value: float | None) -> str:
    """The band label for one slice thickness. `None` is its own band and not a default.

    `MOS-EVID-020` forbids imputation: a series whose header carried no
    `SliceThickness` is not a 1 mm series, and folding it into one would put it in a
    stratum it does not belong to on both sides of the cut.
    """
    if value is None:
        return "unknown"
    for upper, label in _THICKNESS_BANDS:
        if float(value) <= upper:
            return label
    return ">5.0mm"


def stratum_of(acquisition: Mapping[str, Any]) -> dict[str, str]:
    """One patient's stratum, from `MOS-TRAIN-084`'s acquisition profile.

    Missing values become the literal string `"unknown"` rather than being dropped: a
    patient with no recorded manufacturer belongs to a stratum, and a bucket key built by
    omitting the member would collide with a different cohort's key.
    """
    manufacturer = acquisition.get("manufacturer")
    return {
        "manufacturer": str(manufacturer) if manufacturer else "unknown",
        "slice_thickness_band": _thickness_band(
            acquisition.get("slice_thickness_mm")  # type: ignore[arg-type]
        ),
    }


def seed_label_for(*, capability_id: str, harvest_batch_id: str) -> str:
    """`MOS-TRAIN-112`'s `seed_label`: "a stable string".

    Derived from the capability and the batch, so it is a FACT about what is being split
    rather than a number a caller chose. `MOS-EVID-028` permits recording it and forbids
    it being the split; recording a value nobody picked is the strongest form of that.
    """
    return f"{capability_id}/{harvest_batch_id}"


def assignment_method(
    seed_label: str,
    stratified_by: Sequence[str] = STRATIFIED_BY,
    partitions: Sequence[tuple[str, int]] = PARTITIONS,
) -> str:
    """The `assignment_method` string stored on the frozen row (`MOS-EVID-028`).

    `stratified_by` IS A PARAMETER BECAUSE IT WAS A LIE. This function composed the
    string with the module constant while `assign()` stratifies on whatever strata its
    CALLER hands it -- so the first caller to pass anything else got a method string
    naming a stratum nobody stratified on. The comment above `STRATIFIED_BY` calls that
    outcome "worse" than naming none, and it was reachable from the public API the whole
    time; it simply had no caller yet.

    `Assignment.method` derives the names from the strata that were actually used, so
    the default here only serves a caller composing the string without an assignment.

    `partitions` IS A PARAMETER FOR THE SAME REASON, FOUND THE SAME WAY. The string said
    `70/10/20` unconditionally while `assign()` cuts on whatever `partitions` its caller
    hands it. The first caller to pass anything else -- an 80/20 split with no held-out
    `test`, for a technical run that never opens one -- got a method string describing a
    three-way split that was never performed, recorded on the row as the account of how
    the patients were placed. One field over from the lie this docstring already names,
    and reachable from the same public API.
    """
    shape = "/".join(str(int(pct)) for _name, pct in partitions)
    return (
        f"MOS-TRAIN-112 stratified patient-level {shape}; "
        f"seed_label={seed_label}; stratified_by={','.join(stratified_by)}"
    )


@dataclass(frozen=True)
class Assignment:
    """One evaluation of `assign()`. `MOS-TRAIN-112`: "evaluated once, whose output is
    the manifest"."""

    #: `(patient_key, partition)` PAIRS, the shape `freeze_split` takes. A sequence and
    #: not a mapping, for the reason that function's docstring gives.
    pairs: tuple[tuple[str, str], ...]
    #: `patient_key -> stratum`, the shape `freeze_split`'s `strata` argument takes.
    strata: dict[str, dict[str, str]] = field(default_factory=dict)
    seed_label: str = ""
    #: The stratum KEYS this assignment actually bucketed on, derived from `strata`
    #: rather than declared. A caller cannot set it to something it did not do.
    stratified_by: tuple[str, ...] = ()
    #: The cuts this assignment actually used, recorded for the same reason and set by
    #: `assign` from its own argument. Empty means "the module default", which is what
    #: an `Assignment` constructed by hand in a test carries.
    partitions: tuple[tuple[str, int], ...] = ()

    @property
    def partition_patients(self) -> dict[str, int]:
        """Patients per partition, with every value of `PARTITIONS` present at zero.

        Present-at-zero and not omitted: `MOS-UI-114`'s argument about facet values
        applies here too -- the difference between *this cohort has no tune patients* and
        *the projection dropped the key* is the difference between two remedies.
        """
        counts = {name: 0 for name, _ in (self.partitions or PARTITIONS)}
        counts["excluded"] = 0
        for _pk, partition in self.pairs:
            counts[partition] = counts.get(partition, 0) + 1
        return counts

    @property
    def method(self) -> str:
        return assignment_method(
            self.seed_label,
            self.stratified_by or STRATIFIED_BY,
            self.partitions or PARTITIONS,
        )


def assign(
    patient_keys: Sequence[str],
    strata: Mapping[str, Mapping[str, str]],
    seed_label: str,
    partitions: Sequence[tuple[str, int]] = PARTITIONS,
) -> Assignment:
    """`MOS-TRAIN-112`'s reference implementation, returning both shapes off one pass.

    Deterministic, stratified, patient-level. Called with the same three arguments it
    returns the same assignment, on any machine, in any process, for ever -- which is
    what `MOS-EVID-028` means by the manifest being the split.

    Raises `KeyError` when a `patient_key` has no stratum, rather than assigning it to an
    `"unknown"` bucket of this function's invention: the caller knows which cohort it is
    splitting and a silently-invented stratum is a patient placed by accident.
    """
    buckets: dict[tuple[tuple[str, str], ...], list[str]] = {}
    for pk in sorted(set(patient_keys)):
        try:
            stratum = strata[pk]
        except KeyError as exc:  # pragma: no cover - a caller defect, raised loudly
            raise KeyError(
                f"patient {pk!r} has no stratum; MOS-TRAIN-112 buckets by stratum before "
                "it orders, and a patient with none cannot be placed"
            ) from exc
        key = tuple(sorted((str(k), str(v)) for k, v in stratum.items()))
        buckets.setdefault(key, []).append(pk)

    cuts: list[tuple[str, int]] = []
    acc = 0
    for name, pct in partitions:
        acc += pct
        cuts.append((name, acc))

    pairs: list[tuple[str, str]] = []
    out_strata: dict[str, dict[str, str]] = {}
    for key, members in sorted(buckets.items()):
        ordered = sorted(
            members,
            key=lambda pk: hashlib.sha256(f"{seed_label}|{pk}".encode()).hexdigest(),
        )
        n = len(ordered)
        start = 0
        for name, upto in cuts:
            end = (n * upto) // 100
            for pk in ordered[start:end]:
                pairs.append((pk, name))
                out_strata[pk] = dict(key)
            start = end
        # THE REMAINDER, AND WHERE MOS-TRAIN-112 PUTS IT. Nowhere: the cut percentages
        # sum to 100, so the last cut's `end` is `(n * 100) // 100 == n` and the final
        # slice absorbs everything integer division left behind. Every patient is
        # placed exactly once, which is what `MOS-EVID-031` requires of the manifest
        # ("silent omission MUST be a write-time error") and what `freeze_split` would
        # otherwise refuse.
        #
        # OBSERVED AND NOT CHANGED: the absorbing partition is the LAST one, `test`. A
        # stratum of 7 splits 4 / 1 / 2 -- 57 % / 14 % / 29 % -- so a cohort made of
        # many small strata holds a `test` partition somewhat larger than 20 %. That is
        # the reference implementation's behaviour and it is left alone deliberately:
        # `MOS-UI-116`'s argument is that a second implementation disagrees with the
        # authoritative one on exactly the cohorts that sit near a bound, and
        # `MOS-TRAIN-114`'s floor is a floor, so the drift is in the safe direction.
        # Recorded here rather than corrected, because correcting it would change which
        # patients a previously-frozen split would have held and `MOS-EVID-013` makes
        # the frozen one immutable -- the two could never be reconciled afterwards.
        assert start == n, (
            "MOS-TRAIN-112's cuts no longer sum to 100 %, so some patients were left "
            "unplaced and MOS-EVID-031 would refuse the freeze"
        )

    # DERIVED, NOT DECLARED. The union of stratum keys across every patient that was
    # actually bucketed, so `method` describes the pass that produced these pairs and
    # cannot name a stratum this call did not use.
    used = tuple(sorted({k for stratum in out_strata.values() for k in stratum}))
    return Assignment(
        pairs=tuple(sorted(pairs, key=lambda p: (p[0], p[1]))),
        strata=out_strata,
        seed_label=seed_label,
        stratified_by=used,
        partitions=tuple((str(n), int(p)) for n, p in partitions),
    )
