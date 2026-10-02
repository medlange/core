# SPDX-License-Identifier: Apache-2.0
"""The five leakage checks, and the patient-identity check that precedes them.

MOS-EVID-034  "The following five checks MUST be executed at freeze time and their results
              stored verbatim in `leakage_report`. A split MUST NOT be frozen while any
              check is `fail` and unwaived."

  L1 Patient disjointness   patient_key sets of any two non-excluded partitions disjoint
  L2 Study disjointness     every study_instance_uid appears in exactly one partition
  L3 Pixel-identity dupes   no series_pixel_digest occurs in two partitions
  L4 Near-duplicate images  cross-partition 64-bit dHash Hamming distance > 6
  L5 Accession disjointness every non-null accession_number_hash in exactly one partition

WHAT EACH ONE ACTUALLY CATCHES, because the list reads as five variations on one theme
and it is not:

  L1  the basic error.
  L2  a patient re-registered under two MRNs within one source -- two `patient_key`s for
      one human, which L1 structurally cannot see (`MOS-TRAIN-118`).
  L3  the same images re-anonymised with fresh UIDs, "endemic in public collections".
  L4  the same acquisition re-reconstructed, or a re-scan minutes apart.
  L5  one study ingested twice through different routes.

MOS-TRAIN-117 is the rule that makes L1 worth anything: it MUST be evaluated over
`patient_key` alone, across ALL studies and ALL dates, and MUST NOT be relaxed by a time
window. "There is no interval after which two studies of one patient become independent
observations, and a split tool that offers a 'minimum days between studies' option is
offering a way to defeat this check." There is no such option in this module and one MUST
NOT be added; `tests/integration/test_evidence_schema.py` greps for the four spellings.

MOS-TRAIN-118 is the rule that makes L2 and L5 worth anything: an L2 or L5 hit is a
DEFECT IN PATIENT IDENTITY, not an independent finding, and "resolving an L2 or L5 hit by
moving the offending study across partitions MUST be refused -- it leaves two identities
for one patient, L1 still passes, and the next version of the cohort leaks again in a new
place." Every L2/L5 refusal this module raises therefore carries the prescribed remedy and
names the refused one.

MOS-TRAIN-115: L1, L2, L3 and L5 may NOT be waived for a training run. A waiver "is a
statement about what a report may claim; it is not a licence to fit on the test set." This
module accepts waivers at freeze time because `MOS-EVID-036` permits them for reporting;
`waiver_blocks_training()` below is what the 0.3.0 training job calls, and it refuses the
same waiver.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from medos.evidence.manifest import SeriesRecord
from medos.sdk.refusal import Refusal

__all__ = [
    "L4_MAX_HAMMING",
    "LeakageCheck",
    "LeakageReport",
    "dhash64",
    "leakage_report",
    "patient_identity_report",
    "blocking_refusals",
    "waiver_blocks_training",
]

# MOS-EVID-034 L4's bound, and MOS-TRAIN-115's: "64-bit dHash Hamming <= 6".
L4_MAX_HAMMING = 6

# MOS-TRAIN-115: a waiver on any of these MUST NOT permit a training run to proceed.
_UNWAIVABLE_FOR_TRAINING = ("L1", "L2", "L3", "L5")


def dhash64(mid_slice_hu: Any) -> int:
    """MOS-EVID-034's 64-bit dHash of the normalised mid-axial slice.

    Transcribed from the chapter's reference implementation and NOT reinvented: L4's
    verdict is a function of these exact constants (the -1000/400 HU clip, the /1400
    normalisation, the 9x8 resize, the row-wise difference), and a cohort hashed under one
    variant is not comparable with one hashed under another.

    `MOS-EVID-037`: computed over the mid-axial slice of the CANONICAL volume (chapter 4)
    so that it is orientation-invariant. The caller supplies that slice; this function
    does not resample, because resampling here would be a second geometry implementation.
    """
    import numpy as np
    from scipy.ndimage import zoom

    x = np.clip(mid_slice_hu, -1000.0, 400.0)
    x = (x + 1000.0) / 1400.0
    f = [9 / x.shape[0], 8 / x.shape[1]]
    small = zoom(x, f, order=1)[:9, :8]
    bits = (small[1:, :] > small[:-1, :]).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v


@dataclass(frozen=True)
class LeakageCheck:
    """One of L1-L5. `outcome` is `pass`, `fail`, `skipped` or `waived`."""

    id: str
    outcome: str
    hits: tuple[dict[str, Any], ...] = ()
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "outcome": self.outcome,
                               "hits": [dict(h) for h in self.hits]}
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass(frozen=True)
class LeakageReport:
    """The object stored verbatim in `dataset_splits.leakage_report`. MOS-EVID-034.

    `as_dict()` emits the five checks at the TOP LEVEL under their ids, exactly as the
    `ValidationReport` cohort block of section 7.12.1 prints them -- `{"L1": "pass", ...,
    "waivers": []}` -- so the report generator copies this object rather than
    re-rendering it into a second shape that can disagree with the first.
    """

    checks: tuple[LeakageCheck, ...]
    waivers: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {c.id: c.outcome for c in self.checks}
        out["waivers"] = [dict(w) for w in self.waivers]
        out["detail"] = {c.id: c.as_dict() for c in self.checks}
        return out

    @property
    def failed(self) -> tuple[LeakageCheck, ...]:
        return tuple(c for c in self.checks if c.outcome == "fail")


def _by_partition(
    assignments: Sequence[tuple[str, str]],
) -> dict[str, set[str]]:
    """`{partition: {patient_key}}`, excluding the `excluded` partition.

    `MOS-EVID-034` L1 is about "any two non-`excluded` partitions": an excluded patient is
    in no partition, so it cannot leak across one.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for pk, partition in assignments:
        if partition == "excluded":
            continue
        out[partition].add(pk)
    return dict(out)


def _partition_of(assignments: Sequence[tuple[str, str]]) -> dict[str, set[str]]:
    """`{patient_key: {partition, ...}}` -- a SET, so a double assignment survives."""
    out: dict[str, set[str]] = defaultdict(set)
    for pk, partition in assignments:
        out[pk].add(partition)
    return dict(out)


def _l1(assignments: Sequence[tuple[str, str]]) -> LeakageCheck:
    """L1: patient disjointness, over `patient_key` alone. MOS-TRAIN-116, MOS-TRAIN-117.

    No date argument, no window, no per-study relaxation. A patient with a 2019 screening
    CT in `train` and a 2024 follow-up in `test` is a leak, and this function cannot be
    told otherwise because it never sees a date.
    """
    hits = []
    for pk, parts in sorted(_partition_of(assignments).items()):
        real = sorted(p for p in parts if p != "excluded")
        if len(real) > 1:
            hits.append({"patient_key": pk, "partitions": real})
    return LeakageCheck(
        id="L1",
        outcome="fail" if hits else "pass",
        hits=tuple(hits),
        detail={
            "rule": "patient_key sets of any two non-excluded partitions are disjoint",
            "scope": "all studies, all dates (MOS-TRAIN-117)",
        },
    )


def _cross_partition_by_value(
    records: Sequence[SeriesRecord],
    partition_of: Mapping[str, str],
    value_of,
    label: str,
) -> list[dict[str, Any]]:
    """Shared body of L2, L3 and L5: one value must not span two partitions."""
    seen: dict[Any, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in records:
        value = value_of(r)
        if value is None:
            continue
        part = partition_of.get(r.patient_key)
        if part is None or part == "excluded":
            continue
        seen[value][part].add(r.patient_key)
    hits = []
    for value, parts in sorted(seen.items(), key=lambda kv: str(kv[0])):
        if len(parts) > 1:
            hits.append(
                {
                    label: value,
                    "partitions": sorted(parts),
                    "patient_keys": sorted({pk for s in parts.values() for pk in s}),
                }
            )
    return hits


def _l3(
    records: Sequence[SeriesRecord],
    hits: Sequence[dict[str, Any]],
    without_pixel_evidence: Sequence[str],
) -> LeakageCheck:
    """L3, with `MOS-EVID-037`'s discipline extended from L4 to the pixel digest.

    A `fail` is reported whatever the evidence: a collision found among the series that
    WERE hashed is a real finding and suppressing it in favour of `skipped` would hide a
    genuine leak. Only the `pass` direction is conditional, and that is the direction
    that can lie.
    """
    absent = sorted(set(without_pixel_evidence))
    detail: dict[str, Any] = {
        "rule": "no series_pixel_digest occurs in two partitions",
        "catches": "the same images re-anonymised with fresh UIDs",
        "n_evaluated": max(0, len(records) - len(absent)),
        "n_skipped": len(absent),
    }
    if hits:
        return LeakageCheck(id="L3", outcome="fail", hits=tuple(hits), detail=detail)
    if absent:
        detail["skipped"] = [
            {"series_instance_uid": uid,
             "skipped_reason": "series_pixel_digest was not computed from pixels "
                               "(MOS-EVID-018); the value stored is not a pixel digest"}
            for uid in absent[:50]
        ]
        detail["skipped_reason"] = (
            f"{len(absent)} of {len(records)} series carry no pixel-derived "
            "series_pixel_digest, so the comparison examined nothing for them "
            "(MOS-EVID-018, MOS-EVID-037)"
        )
        return LeakageCheck(id="L3", outcome="skipped", hits=(), detail=detail)
    return LeakageCheck(id="L3", outcome="pass", hits=(), detail=detail)


def _l4(
    records: Sequence[SeriesRecord], partition_of: Mapping[str, str]
) -> LeakageCheck:
    """L4: no cross-partition pair within Hamming 6. MOS-EVID-034, MOS-EVID-037.

    MOS-EVID-037: a series with fewer than 3 instances MUST be SKIPPED with an explicit
    `skipped_reason` rather than silently passed. The same discipline is applied, and
    REPORTED, to the case this deployment actually hits: a cohort sealed from a manifest
    whose records carry no `dhash64` because the pixels were not in reach at freeze time.
    Such a split reports L4 `skipped` with the reason naming the count -- never `pass`.
    A `skipped` L4 is not a `fail`, so it does not block the freeze (`MOS-EVID-034` blocks
    on `fail`), but it is carried verbatim into every `ValidationReport` citing the split,
    which is what stops it from being invisible.
    """
    eligible = []
    skipped: list[dict[str, Any]] = []
    for r in records:
        part = partition_of.get(r.patient_key)
        if part is None or part == "excluded":
            continue
        if r.instance_count < 3:
            skipped.append(
                {"series_instance_uid": r.series_instance_uid,
                 "skipped_reason": "fewer than 3 instances (MOS-EVID-037)"}
            )
            continue
        if r.dhash64 is None:
            skipped.append(
                {"series_instance_uid": r.series_instance_uid,
                 "skipped_reason": "no perceptual hash supplied with the manifest record"}
            )
            continue
        eligible.append((part, r))

    hits: list[dict[str, Any]] = []
    for i in range(len(eligible)):
        pa, a = eligible[i]
        for j in range(i + 1, len(eligible)):
            pb, b = eligible[j]
            if pa == pb:
                continue
            assert a.dhash64 is not None and b.dhash64 is not None
            distance = bin(a.dhash64 ^ b.dhash64).count("1")
            if distance <= L4_MAX_HAMMING:
                hits.append(
                    {"a": a.series_instance_uid, "b": b.series_instance_uid,
                     "partitions": sorted([pa, pb]), "hamming": distance}
                )

    if hits:
        outcome = "fail"
    elif not eligible:
        outcome = "skipped"
    else:
        outcome = "pass"
    return LeakageCheck(
        id="L4",
        outcome=outcome,
        hits=tuple(hits),
        detail={
            "max_hamming": L4_MAX_HAMMING,
            "n_compared": len(eligible),
            "n_skipped": len(skipped),
            "skipped": skipped[:50],
        },
    )


def _apply_waivers(
    checks: Sequence[LeakageCheck], waivers: Sequence[Mapping[str, Any]]
) -> tuple[tuple[LeakageCheck, ...], tuple[dict[str, Any], ...]]:
    """MOS-EVID-036: `{check_id, waived_by, waived_at, rationale, affected_pairs}`.

    "A waiver MUST be reproduced in full in any ValidationReport that cites the split.
    There is no silent waiver." A waiver missing any required member raises here rather
    than being accepted as a partial one -- an unattributed waiver is the silent kind.
    """
    required = {"check_id", "waived_by", "waived_at", "rationale"}
    applied: list[dict[str, Any]] = []
    out: list[LeakageCheck] = []
    for c in checks:
        match = next((w for w in waivers if w.get("check_id") == c.id), None)
        if c.outcome != "fail" or match is None:
            out.append(c)
            continue
        missing = required - set(match)
        if missing:
            raise ValueError(
                f"waiver for {c.id} is missing {sorted(missing)}; MOS-EVID-036 requires "
                "{check_id, waived_by, waived_at, rationale, affected_pairs}"
            )
        applied.append(dict(match))
        out.append(
            LeakageCheck(
                id=c.id, outcome="waived", hits=c.hits,
                detail={**(c.detail or {}), "waiver": dict(match)},
            )
        )
    return tuple(out), tuple(applied)


def leakage_report(
    records: Sequence[SeriesRecord],
    assignments: Sequence[tuple[str, str]],
    *,
    waivers: Sequence[Mapping[str, Any]] = (),
    series_without_pixel_evidence: Sequence[str] = (),
) -> LeakageReport:
    """Run L1-L5 at freeze time. MOS-EVID-034.

    `assignments` is `[(patient_key, partition), ...]` -- pairs, not a mapping, so that a
    patient assigned to two partitions is representable and therefore findable.

    `series_without_pixel_evidence` NAMES THE SERIES WHOSE `series_pixel_digest` WAS NOT
    COMPUTED FROM PIXELS, and L3 then reports `skipped` for them rather than `pass`.

    WHY THIS ARGUMENT EXISTS, AND WHY IT IS NOT A FIELD ON `SeriesRecord`.
    `MOS-EVID-018` makes the digest "computed by the caller that holds the pixels", and
    the column that stores it is `sha256_digest NOT NULL` -- so a caller with no pixels
    must supply SOMETHING well-formed or no cohort can be sealed at all.
    `medos/medos/training/retrieval.py` supplies a digest over the series IDENTITY under its
    own namespace, which is the honest choice available to it, and the consequence is
    that L3's comparison can never collide: distinct series have distinct UIDs. Without
    this argument `_l3` would report `pass` over a check that examined nothing, and
    `MOS-EVID-035` would store that `pass` verbatim in a FROZEN, immutable split
    (`MOS-EVID-013`) -- a permanent record that near-identical images had been ruled out
    when nobody looked.

    `_l4` already refuses that shape for the perceptual hash: `MOS-EVID-037` requires a
    `skipped` with an explicit reason, and `dhash64` is OPTIONAL on the record so the
    absence is self-describing. `series_pixel_digest` is not optional, so the absence
    has to be told. An argument rather than a `SeriesRecord` member because the record
    is chapter 7's value type and its shape is what a reader reproduces offline from the
    published manifest format; adding a member to it changes that format for every
    reader. This is a fact about one FREEZE, which is where it is passed.

    DEFAULT-OFF AND BEHAVIOUR-PRESERVING: with no series named, every outcome is exactly
    what it was before this argument existed. REPORTED so that chapter 7 can adopt,
    rename or replace it.
    """
    partition_of: dict[str, str] = {}
    for pk, partition in assignments:
        # First assignment wins for the value-scoped checks; L1 is what reports the
        # conflict, and L2/L3/L5 must still run rather than crash on it.
        partition_of.setdefault(pk, partition)

    l2 = _cross_partition_by_value(
        records, partition_of, lambda r: r.study_instance_uid, "study_instance_uid"
    )
    l3 = _cross_partition_by_value(
        records, partition_of, lambda r: r.series_pixel_digest, "series_pixel_digest"
    )
    l5 = _cross_partition_by_value(
        records, partition_of, lambda r: r.accession_number_hash, "accession_number_hash"
    )

    checks = (
        _l1(assignments),
        LeakageCheck(
            id="L2", outcome="fail" if l2 else "pass", hits=tuple(l2),
            detail={"rule": "every study_instance_uid appears in exactly one partition",
                    "on_hit": "MOS-TRAIN-118: a defect in PATIENT IDENTITY, not a "
                              "partition defect"},
        ),
        _l3(records, l3, series_without_pixel_evidence),
        _l4(records, partition_of),
        LeakageCheck(
            id="L5", outcome="fail" if l5 else "pass", hits=tuple(l5),
            detail={"rule": "every non-null accession_number_hash appears in exactly "
                            "one partition",
                    "on_hit": "MOS-TRAIN-118: a defect in PATIENT IDENTITY, not a "
                              "partition defect"},
        ),
    )
    final, applied = _apply_waivers(checks, waivers)
    return LeakageReport(checks=final, waivers=applied)


def patient_identity_report(records: Sequence[SeriesRecord]) -> LeakageReport:
    """The seal-time half of MOS-TRAIN-118, before any partition exists.

    L2 and L5 detect "a patient re-registered under two MRNs" only once a split exists to
    span. The same defect is visible in the COHORT, with no partitions at all: one
    `study_instance_uid`, or one `accession_number_hash`, carried by two distinct
    `patient_key`s. Catching it at seal is strictly better than catching it at freeze,
    because `MOS-TRAIN-119`'s remedy is to re-seal anyway -- "an alias discovered after
    sealing MUST produce a new `DatasetVersion` ... it MUST NOT mutate the sealed one".

    Reported under the ids `L2` and `L5` so that the remedy a reader is given is the same
    one in both places, and so that a waiver keys on the same `check_id`.
    """
    by_study: dict[str, set[str]] = defaultdict(set)
    by_accession: dict[str, set[str]] = defaultdict(set)
    for r in records:
        by_study[r.study_instance_uid].add(r.patient_key)
        if r.accession_number_hash:
            by_accession[r.accession_number_hash].add(r.patient_key)

    l2 = [
        {"study_instance_uid": uid, "patient_keys": sorted(keys)}
        for uid, keys in sorted(by_study.items())
        if len(keys) > 1
    ]
    l5 = [
        {"accession_number_hash": h, "patient_keys": sorted(keys)}
        for h, keys in sorted(by_accession.items())
        if len(keys) > 1
    ]
    remedy = {
        "remedy": "record the keys as aliases in patient_key_aliases, re-seal the "
                  "DatasetVersion with the alias applied at step 2 of MOS-TRAIN-208, "
                  "and re-freeze the split (MOS-TRAIN-118, MOS-TRAIN-119)",
        "refused_remedy": "moving the offending study across partitions -- it leaves two "
                          "identities for one patient, L1 still passes, and the next "
                          "version of the cohort leaks again in a new place",
    }
    return LeakageReport(
        checks=(
            LeakageCheck(
                id="L2", outcome="fail" if l2 else "pass", hits=tuple(l2),
                detail={"scope": "cohort (pre-split identity coherence)", **remedy},
            ),
            LeakageCheck(
                id="L5", outcome="fail" if l5 else "pass", hits=tuple(l5),
                detail={"scope": "cohort (pre-split identity coherence)", **remedy},
            ),
        )
    )


def blocking_refusals(report: LeakageReport) -> tuple[Refusal, ...]:
    """The `fail` rows, as refusals naming the patients and partitions. MOS-EVID-034."""
    codes = {
        "L1": "patient_in_two_partitions",
        "L2": "study_in_two_partitions",
        "L3": "identical_pixels_in_two_partitions",
        "L4": "near_duplicate_across_partitions",
        "L5": "accession_in_two_partitions",
    }
    out: list[Refusal] = []
    for c in report.failed:
        first = c.hits[0] if c.hits else {}
        out.append(
            Refusal(
                check_id=c.id,
                code=codes.get(c.id, "leakage_check_failed"),
                message=(
                    f"MOS-EVID-034 {c.id}: {len(c.hits)} hit(s); first: "
                    f"{', '.join(f'{k}={v!r}' for k, v in first.items())}"
                ),
                observed=len(c.hits),
                bound=0,
                detail={"hits": [dict(h) for h in c.hits[:50]], **(c.detail or {})},
            )
        )
    return tuple(out)


def waiver_blocks_training(report: LeakageReport) -> tuple[str, ...]:
    """The checks whose waiver MUST NOT permit a training run. MOS-TRAIN-115.

    Returns the ids that are `fail` or `waived` among L1, L2, L3 and L5. The 0.3.0
    training job calls this before the first batch is loaded and aborts on a non-empty
    result. It lives here, with the checks, rather than in the training package, so that
    the rule and the thing it constrains cannot drift apart -- and so that it exists
    before the training package does.
    """
    return tuple(
        c.id
        for c in report.checks
        if c.id in _UNWAIVABLE_FOR_TRAINING and c.outcome in ("fail", "waived")
    )
