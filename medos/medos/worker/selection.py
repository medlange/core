# SPDX-License-Identifier: Apache-2.0
"""Series selection: chapter 3's admission decision, reduced to this slice.

WHY THIS IS A WORKER MODULE AND NOT A `medos.dicomweb` ONE
    `medos/medos/dicomweb`'s own docstring draws the line -- the client "has no opinion about
    which series is interesting" -- and the line is the right one. The gateway is
    transport. Selection is a CLINICAL decision: it decides what a result is about, it has
    to be recorded as `job_series` rows (MOS-STORE-270), and a radiologist has to be able
    to read why their study did not run (MOS-EXEC-014). A transport module that also chose
    the series would put that decision somewhere nobody audits.

    CONTRACT.md section 1 lists two modules under `worker/`. This is a third, split out of
    `steps.py` so that the step executor stays a straight-line pipeline; it is REPORTED as
    a contract addition rather than folded in silently.

Spec: MOS-DATA-058, MOS-DATA-069, MOS-IMG-010, MOS-IMG-066, MOS-STORE-270, MOS-EXEC-014.
"""

from __future__ import annotations

from collections.abc import Sequence

from medos.core.errors import SeriesSelectionError
from medos.dicomweb.gateway import SeriesSummary

__all__ = [
    "select_ct_series",
    "SELECTOR_NAME",
    "SELECTABLE_MODALITIES",
    "MIN_INSTANCES",
]


# =====================================================================================
# Series selection (chapter 3's admission decision, reduced to this slice)
# =====================================================================================
# Selection lives HERE and not in `medos.dicomweb`. That package's own docstring draws the
# line -- "It has no opinion about which series is interesting" -- and the line is the
# right one: the gateway is transport, selection is a clinical decision that has to be
# recorded as `job_series` rows and shown to a radiologist.
#
# chapter 3's `SeriesRequirement`, reduced to what a single in-process CT service can
# honestly claim. `MIN_INSTANCES` is 2 because `build_canonical_volume` raises
# `geometry_insufficient_instances` below that (MOS-IMG-010).
SELECTABLE_MODALITIES = frozenset({"CT"})
MIN_INSTANCES = 2
SELECTOR_NAME = "ct_largest_axial_v1"


def select_ct_series(
    rows: Sequence[SeriesSummary],
) -> tuple[SeriesSummary, list[tuple[SeriesSummary, str, str]]]:
    """Choose the series this job runs on, and say why every other one lost.

    Returns `(winner, rejections)` where each rejection is
    `(row, reason_code, reason_detail)`. Raises `SeriesSelectionError` -- a
    `ClinicalRejection`, hence `REJECTED` and never `FAILED` -- when nothing is eligible.

    MOS-STORE-270 requires the reason for every rejected series to be a ROW, so the losers
    are RETURNED rather than dropped: the caller writes them to `job_series` before the
    job is rejected, and a radiologist asking "why did this not run on my study" gets an
    answer instead of an empty table.

    The rule is total and stable (MOS-IMG-066's principle applied to selection): modality
    CT, at least `MIN_INSTANCES` instances, then the largest instance count, then the
    lexicographically smallest SeriesInstanceUID. "Iteration order of a hash map is not a
    rule" -- and neither is "whichever the PACS listed first".
    """
    eligible: list[SeriesSummary] = []
    rejected: list[tuple[SeriesSummary, str, str]] = []
    for row in rows:
        if row.modality not in SELECTABLE_MODALITIES:
            rejected.append(
                (row, "modality_not_applicable", f"modality {row.modality!r} is not CT")
            )
        elif row.n_instances < MIN_INSTANCES:
            rejected.append(
                (
                    row,
                    "insufficient_instances",
                    f"{row.n_instances} instance(s); the selector requires {MIN_INSTANCES}",
                )
            )
        else:
            eligible.append(row)

    if not eligible:
        raise SeriesSelectionError(
            "no_eligible_series",
            {
                "series_evaluated": len(rows),
                "modalities_seen": sorted({r.modality for r in rows}),
                "required_modalities": sorted(SELECTABLE_MODALITIES),
                "min_instances": MIN_INSTANCES,
            },
            f"no series in this study satisfies the CT selector (evaluated {len(rows)}, "
            f"required modality in {sorted(SELECTABLE_MODALITIES)} with "
            f">= {MIN_INSTANCES} instances)",
        )

    eligible.sort(key=lambda r: (-r.n_instances, r.series_instance_uid))
    winner, *losers = eligible
    for row in losers:
        rejected.append(
            (
                row,
                "not_highest_ranked",
                f"eligible but ranked below {winner.series_instance_uid} "
                f"({row.n_instances} instances vs {winner.n_instances})",
            )
        )
    return winner, rejected


