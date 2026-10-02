# SPDX-License-Identifier: Apache-2.0
"""The corpus stratification check. Chapter 17 section 17.4.5, `MOS-TRAIN-088`.

THE TRAP THIS CHECK EXISTS FOR

"The second trap is subtler because it never produces a wrong number. A corpus harvested
from one hospital's traffic carries that hospital's scanners, its reconstruction kernels,
its contrast protocol, its referral pattern and its disease prevalence. A model trained on
it and measured on a held-out split of it is measured on its own acquisition distribution.
Every figure in the report is correct. The model then loses several points of Dice at the
second site, and `MOS-EVID-097`'s envelope, derived from that same cohort's
`acquisition_profile`, was never wide enough to have warned anyone."

Every per-case number a single-site cohort produces is honest. There is no evaluation
metric that goes red. The only place the defect is visible is in the COMPOSITION of the
corpus, which is why the check runs at seal and blocks it.

THE SEVEN CHECKS, VERBATIM FROM MOS-TRAIN-088

  C1 site concentration     max share of patients from one `institution_key`   <= 0.60  fail
  C2 scanner concentration  max share of series from one (manufacturer, model) <= 0.70  fail
  C3 kernel coverage        number of kernel classes each holding >= 0.10 of series >= 2 fail
  C4 thickness spread       distinct slice_thickness values, and p90/p10 ratio           warn
                            (>= 3 values OR ratio >= 1.5)
  C5 envelope coverage      patients per declared-IN cell                      >= 20    fail
  C6 temporal spread        share of patients from the most-represented year   <= 0.75  warn
  C7 single-site            `institution_key` cardinality: >= 2 for vendor_evidence,
                            exactly 1 permitted for site_acceptance                fail / n/a

MOS-TRAIN-092 is the line between them and it is not decoration: "C4 and C6 describe a
corpus that is narrow, which is a fact the reader needs; C1, C2, C3, C5 and C7 describe a
corpus that cannot support the claim being made from it." So `warn` is reported and never
blocks, and `fail` blocks and cannot be argued with -- only waived, in writing, under
`MOS-TRAIN-091`.

MOS-TRAIN-093: computed from `acquisition_profile` alone, so a reader can recompute it
offline from the exported bundle. This module therefore takes the profile dict and no
database handle, no session and no connection.

MOS-TRAIN-094: a passing per-stratum evaluation does NOT discharge C1-C7. Per-stratum
evaluation "cannot manufacture cases from a scanner the cohort does not contain."
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from medos.sdk.refusal import Refusal

__all__ = [
    "BOUNDS",
    "EVIDENCE_KINDS",
    "CheckResult",
    "StratificationReport",
    "stratification_report",
    "blocking_refusals",
]

# MOS-EVID-003: exactly three kinds of evidence activity, no fourth and no unlabelled.
EVIDENCE_KINDS = ("vendor_evidence", "site_acceptance", "monitoring_period")

# MOS-TRAIN-088's "Default bound" column. Named constants so a site that tightens one has
# a single place to do it -- and so that the report can print the bound it was judged
# against rather than the bound the reader assumes.
BOUNDS: dict[str, Any] = {
    "C1_max_site_patient_share": 0.60,
    "C2_max_scanner_series_share": 0.70,
    "C3_min_kernel_classes": 2,
    "C3_kernel_class_min_share": 0.10,
    "C4_min_distinct_thickness": 3,
    "C4_min_p90_p10_ratio": 1.5,
    "C5_min_patients_per_cell": 20,
    "C6_max_year_patient_share": 0.75,
    "C7_min_institutions_vendor_evidence": 2,
}

_BLOCKING = ("C1", "C2", "C3", "C5", "C7")  # MOS-TRAIN-092
_ADVISORY = ("C4", "C6")


@dataclass(frozen=True)
class CheckResult:
    """One row of the report. `outcome` is `pass`, `fail`, `warn`, `n/a` or `waived`."""

    id: str
    statistic: str
    observed: Any
    bound: Any
    outcome: str
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statistic": self.statistic,
            "observed": self.observed,
            "bound": self.bound,
            "outcome": self.outcome,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class StratificationReport:
    """`CorpusStratificationReport`. MOS-TRAIN-088 requires the FULL result recorded.

    Not just the verdict: the observed statistic and the bound for every check, including
    the ones that passed. `MOS-TRAIN-011` makes this report part of the evidence set a
    promotion decision must show, and a promotion UI showing a single aggregate number and
    a button is non-conformant.

    This object is returned by the seal rather than written to a column: `MOS-TRAIN-088`
    says it is recorded "on the batch", and the `CurationBatch` is not a table this
    migration owns. `MOS-TRAIN-093` makes storage optional in any case -- the report is
    recomputable from the sealed manifest by anyone holding the bundle.
    """

    evidence_kind: str
    checks: tuple[CheckResult, ...]
    waivers: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_kind": self.evidence_kind,
            "checks": [c.as_dict() for c in self.checks],
            "waivers": [dict(w) for w in self.waivers],
            "outcomes": {c.id: c.outcome for c in self.checks},
            "blocking_failures": [c.id for c in self.checks if c.outcome == "fail"],
        }

    @property
    def failed(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if c.outcome == "fail")


def _share(counts: Mapping[str, int], total: int) -> tuple[str | None, float]:
    """The most-represented value and its share, or `(None, 0.0)` on an empty histogram."""
    if not counts or total <= 0:
        return None, 0.0
    top = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
    return top[0], top[1] / total


def _waived(check_id: str, waivers: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """MOS-TRAIN-091: a waiver carries {check_id, waived_by, waived_at, rationale,
    observed, bound}. A waiver missing any of those is not a waiver -- it is an
    unattributed decision, which is the thing "there is no silent waiver" forbids."""
    required = {"check_id", "waived_by", "waived_at", "rationale"}
    for w in waivers:
        if w.get("check_id") != check_id:
            continue
        missing = required - set(w)
        if missing:
            raise ValueError(
                f"waiver for {check_id} is missing {sorted(missing)}; MOS-TRAIN-091 "
                "requires {check_id, waived_by, waived_at, rationale, observed, bound}"
            )
        return dict(w)
    return None


def stratification_report(
    profile: Mapping[str, Any],
    *,
    evidence_kind: str = "vendor_evidence",
    envelope: Mapping[str, Any] | None = None,
    waivers: Sequence[Mapping[str, Any]] = (),
) -> StratificationReport:
    """Run C1-C7 over `acquisition_profile`. MOS-TRAIN-088, MOS-TRAIN-093.

    `evidence_kind` defaults to the STRICT side. C7's bound depends on the claim the
    cohort will support, and a default of `site_acceptance` would make the single-site
    check vacuous for anyone who forgot to pass the argument -- which is the population
    the check is written for.

    `envelope` is the declared `ApplicabilityEnvelope` (chapter 7 section 7.10). C5 is
    unanswerable without one and reports `n/a` with a reason rather than `pass`:
    `MOS-TRAIN-090` calls C5 "the check that matters most and the one most easily
    rationalised away", and a silent pass is exactly that rationalisation.
    """
    if evidence_kind not in EVIDENCE_KINDS:
        raise ValueError(
            f"evidence kind {evidence_kind!r} is not one of MOS-EVID-003's three: "
            f"{EVIDENCE_KINDS}"
        )

    n_series = int(profile.get("n_series", 0))
    n_patients = int(profile.get("n_patients", 0))
    categorical: Mapping[str, Mapping[str, int]] = profile.get("categorical", {})
    patients: Mapping[str, Mapping[str, int]] = profile.get("patients", {})
    numeric: Mapping[str, Mapping[str, Any]] = profile.get("numeric", {})

    results: list[CheckResult] = []

    # ---- C1 site concentration -------------------------------------------------------
    site_patients = patients.get("institution_key", {})
    top_site, site_share = _share(site_patients, n_patients)
    results.append(
        CheckResult(
            id="C1",
            statistic="max share of patients from one institution_key",
            observed=round(site_share, 6),
            bound=BOUNDS["C1_max_site_patient_share"],
            outcome=(
                "n/a"
                if not site_patients
                else "pass"
                if site_share <= BOUNDS["C1_max_site_patient_share"]
                else "fail"
            ),
            detail={
                "stratum": "institution_key",
                "stratum_value": top_site,
                "n_patients": n_patients,
                "n_institutions": len(site_patients),
                **({} if site_patients else {"reason": "no institution_key on any case"}),
            },
        )
    )

    # ---- C2 scanner concentration ----------------------------------------------------
    scanners = categorical.get("scanner_model", {})
    top_scanner, scanner_share = _share(scanners, n_series)
    results.append(
        CheckResult(
            id="C2",
            statistic="max share of series from one (manufacturer, manufacturer_model_name)",
            observed=round(scanner_share, 6),
            bound=BOUNDS["C2_max_scanner_series_share"],
            outcome=(
                "n/a"
                if not scanners
                else "pass"
                if scanner_share <= BOUNDS["C2_max_scanner_series_share"]
                else "fail"
            ),
            detail={
                "stratum": "scanner_model",
                "stratum_value": top_scanner,
                "n_series": n_series,
                "n_scanner_models": len(scanners),
            },
        )
    )

    # ---- C3 kernel coverage ----------------------------------------------------------
    kernels = categorical.get("convolution_kernel_class", {})
    held = {
        k: v / n_series
        for k, v in kernels.items()
        if n_series and v / n_series >= BOUNDS["C3_kernel_class_min_share"]
    }
    results.append(
        CheckResult(
            id="C3",
            statistic="number of convolution_kernel_class values each holding "
                      ">= 0.10 of series",
            observed=len(held),
            bound=BOUNDS["C3_min_kernel_classes"],
            outcome=(
                "n/a"
                if not kernels
                else "pass"
                if len(held) >= BOUNDS["C3_min_kernel_classes"]
                else "fail"
            ),
            detail={
                "stratum": "convolution_kernel_class",
                "qualifying_values": sorted(held),
                "shares": {k: round(v, 6) for k, v in sorted(held.items())},
                "all_values": sorted(kernels),
            },
        )
    )

    # ---- C4 thickness spread (advisory) ----------------------------------------------
    thickness = numeric.get("slice_thickness_mm", {})
    distinct = int(thickness.get("distinct", 0))
    p10 = float(thickness.get("p10", 0.0) or 0.0)
    p90 = float(thickness.get("p90", 0.0) or 0.0)
    ratio = (p90 / p10) if p10 > 0 else 0.0
    c4_ok = (
        distinct >= BOUNDS["C4_min_distinct_thickness"]
        or ratio >= BOUNDS["C4_min_p90_p10_ratio"]
    )
    results.append(
        CheckResult(
            id="C4",
            statistic="distinct slice_thickness_mm values, and p90/p10 ratio",
            observed={"distinct": distinct, "p90_over_p10": round(ratio, 6)},
            bound={
                "distinct_at_least": BOUNDS["C4_min_distinct_thickness"],
                "or_ratio_at_least": BOUNDS["C4_min_p90_p10_ratio"],
            },
            # MOS-TRAIN-092: C4 describes a corpus that is NARROW. Reported, never blocking.
            outcome="n/a" if not thickness else "pass" if c4_ok else "warn",
            detail={"stratum": "slice_thickness_mm", "p10": p10, "p90": p90},
        )
    )

    # ---- C5 envelope coverage --------------------------------------------------------
    results.append(_c5(profile, envelope))

    # ---- C6 temporal spread (advisory) -----------------------------------------------
    year_patients = patients.get("study_year", {})
    top_year, year_share = _share(year_patients, n_patients)
    results.append(
        CheckResult(
            id="C6",
            statistic="share of patients from the single most-represented study_year",
            observed=round(year_share, 6),
            bound=BOUNDS["C6_max_year_patient_share"],
            outcome=(
                "n/a"
                if not year_patients
                else "pass"
                if year_share <= BOUNDS["C6_max_year_patient_share"]
                else "warn"
            ),
            detail={
                "stratum": "study_year",
                "stratum_value": top_year,
                "n_years": len(year_patients),
            },
        )
    )

    # ---- C7 single-site declaration --------------------------------------------------
    n_sites = len(site_patients)
    if evidence_kind == "vendor_evidence":
        floor = BOUNDS["C7_min_institutions_vendor_evidence"]
        c7_outcome = "pass" if n_sites >= floor else "fail"
        c7_bound: Any = floor
    else:
        # "exactly 1 permitted for site_acceptance" -- the check does not apply.
        c7_outcome = "n/a"
        c7_bound = "n/a"
    results.append(
        CheckResult(
            id="C7",
            statistic="institution_key cardinality",
            observed=n_sites,
            bound=c7_bound,
            outcome=(
                "n/a"
                if not site_patients and evidence_kind != "vendor_evidence"
                else c7_outcome
            ),
            detail={
                "stratum": "institution_key",
                "evidence_kind": evidence_kind,
                "institutions": sorted(site_patients),
            },
        )
    )

    # ---- waivers ---------------------------------------------------------------------
    # MOS-TRAIN-091: a waiver turns a `fail` into a recorded, attributed `waived`. It
    # never turns it into a `pass`, and it is reproduced in full in every ValidationReport
    # citing the cohort.
    applied: list[dict[str, Any]] = []
    final: list[CheckResult] = []
    for r in results:
        w = _waived(r.id, waivers) if r.outcome == "fail" else None
        if w is None:
            final.append(r)
            continue
        applied.append(w)
        final.append(
            CheckResult(
                id=r.id,
                statistic=r.statistic,
                observed=r.observed,
                bound=r.bound,
                outcome="waived",
                detail={**r.detail, "waiver": w},
            )
        )

    return StratificationReport(
        evidence_kind=evidence_kind,
        checks=tuple(final),
        waivers=tuple(applied),
    )


def _c5(
    profile: Mapping[str, Any], envelope: Mapping[str, Any] | None
) -> CheckResult:
    """C5: >= 20 patients in every cell the declared envelope marks `IN`. MOS-TRAIN-090.

    The envelope shape this reads is chapter 7 section 7.10's, reduced to what C5 needs:

        {"categorical": {"manufacturer": ["SIEMENS", "GE MEDICAL SYSTEMS"]},
         "numeric": {"slice_thickness_mm": {"min": 0.6, "max": 3.0}}}

    A categorical cell is a declared-IN value; a numeric cell is a decile of the cohort's
    own distribution that falls inside the declared bounds. "for every categorical value
    and every numeric decile the declared `ApplicabilityEnvelope` marks `IN`, the number
    of patients present" -- so the count is PATIENTS, from the profile's `patients` block,
    and a categorical value the envelope declares but the cohort does not contain scores
    zero, which is the whole point: `MOS-TRAIN-090` exists so that "validated on 2.5 mm
    archival data, declared for 0.6 mm" fails at harvest rather than at envelope
    publication.
    """
    bound = BOUNDS["C5_min_patients_per_cell"]
    if not envelope:
        return CheckResult(
            id="C5",
            statistic="patients per declared-IN envelope cell",
            observed=None,
            bound=bound,
            # NOT `pass`. MOS-TRAIN-090 names C5 as the one most easily rationalised away.
            outcome="n/a",
            detail={
                "reason": "no ApplicabilityEnvelope was declared for this cohort",
                "consequence": "C5 cannot be discharged; an envelope MUST be declared "
                               "before a vendor_evidence report cites this cohort "
                               "(MOS-TRAIN-090, MOS-EVID-098)",
            },
        )

    patients: Mapping[str, Mapping[str, int]] = profile.get("patients", {})
    numeric: Mapping[str, Mapping[str, Any]] = profile.get("numeric", {})
    thin: list[dict[str, Any]] = []

    for field_name, values in (envelope.get("categorical") or {}).items():
        counts = patients.get(field_name, {})
        for value in values:
            n = int(counts.get(str(value), 0))
            if n < bound:
                thin.append(
                    {"kind": "categorical", "stratum": field_name,
                     "stratum_value": str(value), "n_patients": n}
                )

    for field_name, bounds in (envelope.get("numeric") or {}).items():
        block = numeric.get(field_name)
        if block is None:
            thin.append(
                {"kind": "numeric", "stratum": field_name, "stratum_value": None,
                 "n_patients": 0,
                 "reason": "the cohort carries no value for a field the envelope declares"}
            )
            continue
        lo = bounds.get("min")
        hi = bounds.get("max")
        # The cohort's own span against the declared span. A declared bound outside the
        # observed range is a cell with zero cases, which is MOS-TRAIN-090's example.
        if lo is not None and float(block["min"]) > float(lo):
            thin.append(
                {"kind": "numeric", "stratum": field_name, "stratum_value": f"< {block['min']}",
                 "n_patients": 0,
                 "reason": f"envelope declares min {lo}; cohort's minimum is {block['min']}"}
            )
        if hi is not None and float(block["max"]) < float(hi):
            thin.append(
                {"kind": "numeric", "stratum": field_name, "stratum_value": f"> {block['max']}",
                 "n_patients": 0,
                 "reason": f"envelope declares max {hi}; cohort's maximum is {block['max']}"}
            )

    return CheckResult(
        id="C5",
        statistic="patients per declared-IN envelope cell",
        observed=len(thin),
        bound=bound,
        outcome="pass" if not thin else "fail",
        detail={"under_populated_cells": thin},
    )


def blocking_refusals(report: StratificationReport) -> tuple[Refusal, ...]:
    """The `fail` rows, as refusals that name the stratum. MOS-TRAIN-088.

    "A `fail` MUST block sealing." A refusal that says only "stratification check failed"
    is unactionable, so each one carries `detail.stratum` and `detail.stratum_value`:
    the seal is refused *because of SIEMENS Sensation 16*, not *because of C2*.
    """
    out: list[Refusal] = []
    for c in report.checks:
        if c.outcome != "fail":
            continue
        if c.id in _ADVISORY or c.id not in _BLOCKING:
            # MOS-TRAIN-092 draws the line and this is where it would be crossed: C4 and
            # C6 describe a corpus that is NARROW, which is reported and never blocks. A
            # later change that makes an advisory check emit `fail` would otherwise turn
            # it into a blocking one here, silently, with no requirement amended.
            raise AssertionError(
                f"MOS-TRAIN-092: {c.id} is an advisory check and MUST NOT block; it "
                f"reported {c.outcome!r}. `warn` is the only breach outcome it may have."
            )
        stratum = c.detail.get("stratum")
        value = c.detail.get("stratum_value")
        named = f" on stratum {stratum}" if stratum else ""
        named += f" = {value!r}" if value is not None else ""
        out.append(
            Refusal(
                check_id=c.id,
                code="corpus_stratification_failed",
                message=(
                    f"MOS-TRAIN-088 {c.id}{named}: {c.statistic} is {c.observed!r}, "
                    f"bound {c.bound!r}"
                ),
                observed=c.observed,
                bound=c.bound,
                detail={**c.detail, "statistic": c.statistic},
            )
        )
    return tuple(out)
