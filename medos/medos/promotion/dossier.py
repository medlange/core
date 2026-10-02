# SPDX-License-Identifier: Apache-2.0
"""The approval dossier: fifteen items, rendered offline, computing nothing. `MOS-TRAIN-176`.

"The dossier MUST be generated, MUST be renderable offline from the report bundle of
`MOS-EVID-122` plus registry rows with no network access, and MUST contain exactly the
items below. An approver deciding from a metric pasted into a chat message is the failure
this object exists to prevent; an approver deciding from a forty-page appendix is the same
failure with better manners."

WHY THIS MODULE HAS NO ARITHMETIC IN IT
----------------------------------------
`MOS-TRAIN-178`: "Every number in the dossier MUST be rendered from a `capability_claims`
row or a persisted `evaluation_runs` field and MUST link to its `evaluation_run_id`
(`MOS-EVID-072`). The dossier MUST NOT compute a figure of its own. A rendering layer that
can compute is a second evaluation implementation with no run behind it."

So `render()` takes already-persisted values and arranges them. There is no mean, no
interval, no delta and no percentage computed anywhere in this file. The ONE comparison it
makes is `selection_margin <= seed_variance.sd`, which `MOS-TRAIN-176` item 15 requires
verbatim and which is not a figure -- it decides whether to print a fixed sentence.

THE TWO THINGS THE RENDERER REFUSES TO PRODUCE
------------------------------------------------
`MOS-TRAIN-165` / chapter 17 acceptance check 10: "the approval dossier cannot render the
`conversion_equivalence` block for a converted version whose own `EvaluationRun` is absent
or not `SUCCEEDED`", and the statement of `MOS-TRAIN-165` must appear verbatim in the
rendered block. `render()` raises rather than emitting a block without its run.

`MOS-TRAIN-229`: for an ensemble, "per-member figures MUST NOT" appear -- "a member table
beside an ensemble figure invites an approver to pick a member, which is a model-selection
decision made at the gate on data the gate was not given." The renderer drops any
`per_member` key it is handed and says so in the item.

`MOS-TRAIN-177` is the item-2 rule: "The dossier MUST present the incumbent's figures
computed on the **same** cohort -- the re-run of `MOS-TRAIN-146` -- and MUST label any
historical published figure for the incumbent separately and as historical. Placing a
candidate's fresh number beside an incumbent's year-old number from a different cohort is
the most common way an approval is obtained for a comparison that was never performed."

Spec: MOS-TRAIN-127, MOS-TRAIN-128, MOS-TRAIN-146, MOS-TRAIN-165, MOS-TRAIN-176 to
MOS-TRAIN-178, MOS-TRAIN-216, MOS-TRAIN-229, MOS-TRAIN-236, MOS-EVID-072, MOS-EVID-122.
Pure: no database, no network, no clock beyond the one the caller passes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

# From `medos/medos/core/`, NOT from `medos/medos/training/`: the promotion dossier is a core
# concern and must not import the training engine to read one string. See that
# module's docstring for why neither side can own it.
from medos.core.statements import EQUIVALENCE_DISCLAIMER

__all__ = [
    "DOSSIER_ITEMS",
    "NOISE_MARKER",
    "UNDERPOWERED_STRATUM_FLOOR",
    "DossierRefused",
    "render",
]


class DossierRefused(RuntimeError):
    """The dossier cannot be rendered from what it was given, and MUST NOT be approximated."""


#: `MOS-TRAIN-176`'s fifteen items, in its order, as the dossier's own table of contents.
DOSSIER_ITEMS: Final[tuple[tuple[int, str], ...]] = (
    (1, "Candidate identity"),
    (2, "Incumbent"),
    (3, "Cohort"),
    (4, "Reference standard"),
    (5, "Leakage"),
    (6, "Verdict"),
    (7, "Regression"),
    (8, "Strata"),
    (9, "Seed variance"),
    (10, "Conversion"),
    (11, "Envelope diff"),
    (12, "Publisher declarations"),
    (13, "Plausibility"),
    (14, "Scope of the decision"),
    (15, "Configuration search"),
)

#: `MOS-TRAIN-176` item 15, verbatim: "when `selection_margin <= seed_variance.sd` the
#: item MUST carry the literal marker".
NOISE_MARKER: Final[str] = "selection not distinguishable from run-to-run noise"

#: `MOS-TRAIN-176` item 8: "every stratum whose `n < 10` explicitly marked underpowered
#: rather than rendered as a number".
UNDERPOWERED_STRATUM_FLOOR: Final[int] = 10


def _linked(value: Any, evaluation_run_id: str | None) -> dict[str, Any]:
    """Every figure carries its run. `MOS-TRAIN-178`, chapter 17 acceptance check 19."""
    return {"value": value, "evaluation_run_id": evaluation_run_id}


def render(
    *,
    candidate: Mapping[str, Any],
    incumbent: Mapping[str, Any] | None,
    cohort: Mapping[str, Any],
    reference_standard: Mapping[str, Any],
    leakage: Mapping[str, Any],
    verdict: Mapping[str, Any],
    regression: Mapping[str, Any],
    strata: Sequence[Mapping[str, Any]],
    seed_variance: Mapping[str, Any] | None,
    publisher_declarations: Mapping[str, Any],
    plausibility: Sequence[Mapping[str, Any]],
    scope: Mapping[str, Any],
    envelope_diff: Sequence[Mapping[str, Any]] = (),
    conversion: Mapping[str, Any] | None = None,
    configuration_search: Mapping[str, Any] | None = None,
    test_exposure_count: int | None = None,
) -> dict[str, Any]:
    """The dossier document. Raises `DossierRefused` rather than rendering a partial one.

    Every argument is already-persisted material: registry rows, the report bundle of
    `MOS-EVID-122`, `capability_claims` rows and the three 0013 tables. Nothing is fetched
    and nothing is derived.
    """
    problems: list[str] = []

    # ---- item 10, checked first because it is the one that REFUSES ------------------
    conversion_block: dict[str, Any] | None = None
    if conversion is not None:
        run = dict(conversion.get("evaluation_run") or {})
        if not run or run.get("state") != "SUCCEEDED":
            raise DossierRefused(
                "MOS-TRAIN-165 / MOS-REG-104: the conversion_equivalence block MUST NOT "
                "be rendered for a converted version whose own EvaluationRun is absent or "
                "not SUCCEEDED. The clinical question is answered only by that run "
                "against the AcceptanceCriteria, and quantisation moves behaviour most in "
                "exactly the small, low-contrast cases the aggregate metric weights least"
            )
        equivalence = dict(conversion.get("equivalence") or {})
        conversion_block = {
            "conversion_run_id": conversion.get("conversion_run_id"),
            "precision": equivalence.get("precision") or conversion.get("precision"),
            "E1": equivalence.get("E1"),
            "E2": equivalence.get("E2"),
            "E3": equivalence.get("E3"),
            "tolerances": equivalence.get("tolerances"),
            # Verbatim, per chapter 17 acceptance check 10.
            "statement": EQUIVALENCE_DISCLAIMER,
            "own_evaluation_run": _linked(run.get("verdict"), run.get("public_id")),
        }

    # ---- item 2 ---------------------------------------------------------------------
    incumbent_block: dict[str, Any]
    if incumbent is None:
        incumbent_block = {
            "present": False,
            "note": "no incumbent: this is a first deployment for the capability in this "
                    "(tenant, environment) slot. MOS-TRAIN-147: that branch is taken "
                    "because no incumbent EXISTS, never because an incumbent re-run was "
                    "skipped",
        }
    else:
        rerun = dict(incumbent.get("same_cohort_rerun") or {})
        if not rerun.get("evaluation_run_id"):
            problems.append(
                "MOS-TRAIN-177: the incumbent's figures MUST come from the same-cohort "
                "re-run of MOS-TRAIN-146. Placing a candidate's fresh number beside an "
                "incumbent's year-old number from a different cohort is the most common "
                "way an approval is obtained for a comparison that was never performed"
            )
        incumbent_block = {
            "present": True,
            "model_version": incumbent.get("model_version"),
            "deployment_id": incumbent.get("deployment_id"),
            "activated_at": incumbent.get("activated_at"),
            "serving_for": incumbent.get("serving_for"),
            "figures": {
                "source": "same-cohort re-run (MOS-TRAIN-146)",
                "evaluation_run_id": rerun.get("evaluation_run_id"),
                "metrics": rerun.get("metrics"),
            },
            "historical_figures": [
                {**dict(h), "label": "historical -- measured on a different cohort"}
                for h in (incumbent.get("historical_figures") or ())
            ],
        }

    # ---- item 8 ---------------------------------------------------------------------
    strata_rows: list[dict[str, Any]] = []
    for stratum in strata:
        n = int(stratum.get("n") or 0)
        row = {
            "stratum": stratum.get("id"),
            "n": n,
            "evaluation_run_id": stratum.get("evaluation_run_id"),
        }
        if n < UNDERPOWERED_STRATUM_FLOOR:
            row["value"] = None
            row["marker"] = (
                f"underpowered (n = {n} < {UNDERPOWERED_STRATUM_FLOOR}); MOS-TRAIN-176 "
                "item 8 requires this to be marked rather than rendered as a number"
            )
        else:
            row["value"] = stratum.get("value")
        strata_rows.append(row)

    # ---- item 9, and its pairing with item 15 ---------------------------------------
    sd = float(seed_variance["sd"]) if seed_variance and "sd" in seed_variance else None
    seed_block = {
        "recorded": seed_variance is not None,
        "seed_variance": dict(seed_variance) if seed_variance else None,
        "declared_margin_delta": regression.get("margin"),
        # MOS-TRAIN-128, stated on the surface that renders both: the dossier shows them
        # side by side and neither one computes the other.
        "note": "MOS-TRAIN-128: seed_variance.sd is rendered beside the declared "
                "non-inferiority margin and MUST NOT be used to compute, adjust or "
                "justify it (MOS-EVID-087)",
    }
    if seed_variance is None:
        problems.append(
            "MOS-TRAIN-127: each capability MUST have a seed-variance characterisation "
            "recorded BEFORE its first candidate is promoted"
        )

    # ---- item 15 --------------------------------------------------------------------
    search_block: dict[str, Any] | None = None
    if configuration_search is not None:
        margin = configuration_search.get("selection_margin")
        search_block = {
            "search_id": configuration_search.get("search_id"),
            "trials_completed": configuration_search.get("trials_completed"),
            "space_digest": configuration_search.get("space_digest"),
            "selection_metric": configuration_search.get("selection_metric"),
            "selection_partition": configuration_search.get("selection_partition"),
            "selection_rule": configuration_search.get("selection_rule"),
            # MOS-TRAIN-176 item 15: on the SAME line as seed_variance.sd.
            "selection_margin_vs_seed_sd": {
                "selection_margin": margin,
                "seed_variance_sd": sd,
            },
            "cost_gpu_hours_used": (configuration_search.get("cost") or {}).get(
                "gpu_hours_used"
            ),
            "cost_stop_reason": (configuration_search.get("cost") or {}).get(
                "stop_reason"
            ),
            "test_exposure_count": test_exposure_count,
        }
        if margin is not None and sd is not None and float(margin) <= sd:
            search_block["marker"] = NOISE_MARKER
        ensemble = configuration_search.get("ensemble")
        if ensemble:
            search_block["ensemble"] = {
                "member_count": len(list(ensemble.get("members") or ())),
                "combination_rule": ensemble.get("combination_rule"),
                "note": "MOS-TRAIN-229: per-member figures are omitted. A member table "
                        "beside an ensemble figure invites an approver to pick a member, "
                        "which is a model-selection decision made at the gate on data the "
                        "gate was not given",
            }
        if test_exposure_count is None:
            problems.append(
                "MOS-TRAIN-216: the dossier MUST render test_exposure_count for this "
                "split. A split's test partition is a consumable; the counter is how a "
                "team finds out it has been spent"
            )

    if problems:
        raise DossierRefused(" | ".join(problems))

    return {
        "schema": "medicalos.approval-dossier/1",
        "renderable_offline": True,
        "computes_nothing": True,      # MOS-TRAIN-178
        "items": {
            "1_candidate_identity": {
                "model_id": candidate.get("model_id"),
                "version": candidate.get("version"),
                "content_digest": candidate.get("content_digest"),
                "derived_from": candidate.get("derived_from"),
                "preprocessing_spec_ref": candidate.get("preprocessing_spec_ref"),
                "preprocessing_spec_digest": candidate.get("preprocessing_spec_digest"),
                "bundle_metadata_version": candidate.get("bundle_metadata_version"),
                "training_run_id": candidate.get("training_run_id"),
            },
            "2_incumbent": incumbent_block,
            "3_cohort": {
                "dataset_version_digest": cohort.get("dataset_version_digest"),
                "patient_count": cohort.get("patient_count"),
                "test_partition_n_patients": cohort.get("test_partition_n_patients"),
                "acquisition_profile": cohort.get("acquisition_profile"),
                "licence": cohort.get("licence"),
                "deidentification_status": cohort.get("deidentification_status"),
            },
            "4_reference_standard": {
                "readers": reference_standard.get("readers"),
                "consensus_rule": reference_standard.get("consensus_rule"),
                "reference_of_record": reference_standard.get("reference_of_record"),
                # MOS-TRAIN-176 item 4: "the approver must see both numbers on the same
                # line" -- a Dice of 0.82 against a reference whose own inter-reader Dice
                # is 0.84 is a different statement from the same figure against 0.97.
                "inter_reader_agreement": reference_standard.get("inter_reader"),
                "headline_metric": reference_standard.get("headline_metric"),
            },
            "5_leakage": {
                "checks": dict(leakage.get("checks") or {}),
                "waivers": list(leakage.get("waivers") or ()),
                "patient_key_aliases_applied": list(
                    leakage.get("patient_key_aliases") or ()
                ),
            },
            "6_verdict": {
                "verdict": verdict.get("verdict"),
                "criteria_version": verdict.get("criteria_version"),
                # MOS-EVID-113: one row per criterion INCLUDING every SKIPPED and
                # INDETERMINATE.
                "criterion_results": list(verdict.get("criterion_results") or ()),
            },
            "7_regression": {
                "mean_delta": regression.get("mean_delta"),
                "ci_lower_95_one_sided": regression.get("ci_lower_95_one_sided"),
                "margin": regression.get("margin"),
                "n": regression.get("n"),
                "n_patients": regression.get("n_patients"),
                "margin_rationale": regression.get("margin_rationale"),
                "catastrophic_count": regression.get("catastrophic_count"),
                "evaluation_run_id": regression.get("evaluation_run_id"),
            },
            "8_strata": strata_rows,
            "9_seed_variance": seed_block,
            "10_conversion": conversion_block,
            "11_envelope_diff": [
                {**dict(d), "widened": bool(d.get("widened"))} for d in envelope_diff
            ],
            "12_publisher_declarations": {
                "not_validated_for": list(
                    publisher_declarations.get("not_validated_for") or ()
                ),
                "known_failure_modes": list(
                    publisher_declarations.get("known_failure_modes") or ()
                ),
            },
            "13_plausibility": [
                {
                    "rule": p.get("rule"),
                    "severity": p.get("severity"),
                    "firing_rate": p.get("firing_rate"),
                    "exercised": p.get("exercised"),
                    "evaluation_run_id": p.get("evaluation_run_id"),
                }
                for p in plausibility
            ],
            "14_scope_of_the_decision": {
                "would_affect": list(scope.get("would_affect") or ()),
                "would_not_affect": list(scope.get("would_not_affect") or ()),
            },
            "15_configuration_search": search_block,
        },
        "three_acts_remaining": [
            "A1 evidence.report.issue",
            "A2 artifact.approve",
            "A3 deployment.approve_clinical",
        ],
    }
