# SPDX-License-Identifier: Apache-2.0
"""The exclusion vocabulary, and the rule that keeps it a statement about the image.

`MOS-TRAIN-203`: "Every excluded candidate MUST be retained with a `reason_code` drawn
from a closed vocabulary declared on the `Dataset`. Deleting an excluded row MUST be
refused. This is the rule that keeps a cohort honest: cases dropped one at a time because
'the model does badly on them' is the mechanism by which a training corpus becomes
optimistic, and it is invisible from the sealed manifest alone -- the sealed manifest
shows only what survived."

`MOS-TRAIN-204`: "`reason_code` MUST NOT include any value whose meaning is a model
outcome. The vocabulary is a statement about the *image or the patient* ... A curation
tool that offers `model_performed_poorly`, `outlier`, `hard_case` or an equivalent MUST be
treated as non-conformant."

TWO VOCABULARIES, AND WHY BOTH ARE HERE
---------------------------------------
Chapter 17 states the exclusion vocabulary twice, with different members:

  `MOS-TRAIN-080`  quality_artefact, wrong_anatomy, wrong_phase, prior_treatment,
                   duplicate_patient, geometry_unsupported, annotation_infeasible,
                   out_of_scope
  `MOS-TRAIN-204`  slice_thickness_out_of_spec, gantry_tilt_uncorrectable,
                   contrast_phase_unknown, prior_resection,
                   motion_artifact_reader_rejected, annotation_unavailable,
                   duplicate_of_included_series

Chapter 12 section 12.12.1 renders `MOS-TRAIN-080`'s set as the `CHECK` on
`curation_decisions.reason_code`, so that is the set the database enforces and
`PLATFORM_REASON_CODES` below. `MOS-TRAIN-204`'s members are reachable as a
`DECLARED_EXAMPLE` vocabulary and are NOT written to the column -- a candidate excluded
for `slice_thickness_out_of_spec` is `geometry_unsupported` in the column and carries the
finer code in `note`. The divergence is REPORTED as a spec defect rather than resolved by
widening the CHECK, because widening it here would make the two chapters disagree in the
schema instead of on paper.

WHY THE SCREEN IS A FUNCTION AND NOT A SECOND LIST
--------------------------------------------------
`MOS-TRAIN-204` does not forbid three strings; it forbids "any value whose meaning is a
model outcome ... or an equivalent". A denylist of three literals is satisfied by
`difficult_case`. So `screen_vocabulary` tokenises each candidate code and refuses any
code carrying a token from `_MODEL_OUTCOME_TOKENS`. That is a heuristic and it says so:
it cannot read intent, and a determined tool can still spell a model outcome in a way
this screen passes. It is the cheap half of the rule. The expensive half is
`MOS-TRAIN-081` -- exclusion reasons aggregated per cohort and reproduced in every
`ValidationReport` citing the resulting `DatasetVersion` -- which makes a mis-declared
vocabulary visible to the reader rather than merely absent from a CHECK.

NOT DECLARED HERE: a `datasets` column to hold the vocabulary. `MOS-TRAIN-203` says the
vocabulary is "declared on the `Dataset`" and migration 0006's `datasets` has no column
for it. REPORTED. Until that column exists, a vocabulary is an argument to the curation
functions, screened on the way in.

Spec: MOS-TRAIN-080, MOS-TRAIN-081, MOS-TRAIN-203, MOS-TRAIN-204, MOS-STORE-358.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from medos.sdk.errors import CurationRefused, Refusal

__all__ = [
    "DECISIONS",
    "PLATFORM_REASON_CODES",
    "DECLARED_EXAMPLE_VOCABULARY",
    "MODEL_OUTCOME_TOKENS",
    "screen_vocabulary",
    "assert_reason_code",
    "platform_code_for",
]

# `MOS-TRAIN-080`'s decision set. `defer` is not terminal, which is why
# `curation_decisions_settled_uk` in migration 0011 is a partial index over
# `decision <> 'defer'`: a candidate may be deferred repeatedly and settled once.
DECISIONS: tuple[str, ...] = ("include", "exclude", "defer")

# `MOS-TRAIN-080`'s closed set, in its order. This is the set the database enforces.
PLATFORM_REASON_CODES: tuple[str, ...] = (
    "quality_artefact",
    "wrong_anatomy",
    "wrong_phase",
    "prior_treatment",
    "duplicate_patient",
    "geometry_unsupported",
    "annotation_infeasible",
    "out_of_scope",
)

# `MOS-TRAIN-204`'s worked vocabulary. Conformant, finer-grained, and NOT a column value:
# see the module docstring. Present so that `screen_vocabulary` has a fixture that must
# pass, which is a stronger test of the screen than a list of things that must fail.
DECLARED_EXAMPLE_VOCABULARY: tuple[str, ...] = (
    "slice_thickness_out_of_spec",
    "gantry_tilt_uncorrectable",
    "contrast_phase_unknown",
    "prior_resection",
    "motion_artifact_reader_rejected",
    "annotation_unavailable",
    "duplicate_of_included_series",
)

# Tokens whose presence makes a reason code a statement about the MODEL rather than about
# the image or the patient. Matched on whole tokens after splitting on non-alphanumerics,
# so `wrong_anatomy` survives (`wrong` is not a member) and `model_performed_poorly`,
# `hard_case`, `outlier`, `low_dice`, `false_positive_heavy` do not.
MODEL_OUTCOME_TOKENS: frozenset[str] = frozenset(
    {
        # the artifact itself
        "model", "models", "net", "network", "algorithm", "ai",
        # what it emitted
        "prediction", "predictions", "predicted", "predict", "inference", "inferred",
        "output", "mask", "segmentation", "logit", "logits", "probability", "confidence",
        # how it scored
        "score", "scored", "scoring", "metric", "dice", "iou", "jaccard", "hausdorff",
        "sensitivity", "specificity", "precision", "recall", "auc",
        # how someone felt about how it scored
        "outlier", "outliers", "hard", "difficult", "easy", "poor", "poorly",
        "performed", "performance", "underperformed", "failure", "failing", "wrong",
        "disagreement", "disagreed", "mispredicted", "missed",
        # the two-token spellings
        "fp", "fn",
    }
)

_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# `wrong` is in the token set above and `wrong_anatomy` / `wrong_phase` are
# `MOS-TRAIN-080`'s own members. Both statements are true: `wrong_anatomy` is a statement
# about the image ("this is not the body part") and `wrong_prediction` is a statement
# about the model. The token alone cannot separate them, so the two chapter-17 members
# are exempted BY NAME rather than by weakening the token set -- an exemption list of two
# entries is auditable, and a token set with `wrong` removed silently admits
# `wrong_answer`.
_EXEMPT: frozenset[str] = frozenset({"wrong_anatomy", "wrong_phase"})


def _tokens(code: str) -> tuple[str, ...]:
    return tuple(t for t in _TOKEN_RE.split(code.strip().lower()) if t)


def screen_vocabulary(codes: Iterable[str]) -> tuple[str, ...]:
    """Return `codes` unchanged, or raise `CurationRefused`. `MOS-TRAIN-204`.

    Refuses the whole vocabulary rather than filtering it. A curation tool that offered
    four conformant codes and one model-outcome code is non-conformant as a tool --
    `MOS-TRAIN-204` says exactly that -- and silently dropping the fifth would ship the
    tool with a hole in the place the operator was about to click.
    """
    codes = tuple(codes)
    refusals: list[Refusal] = []
    for code in codes:
        if not code or not _TOKEN_RE.sub("_", code.strip().lower()).strip("_"):
            refusals.append(
                Refusal(
                    check_id="MOS-TRAIN-203",
                    code="empty_reason_code",
                    message="a reason code must be a word; the vocabulary carries an "
                            "empty member",
                    observed=repr(code),
                )
            )
            continue
        normalised = code.strip().lower()
        if normalised in _EXEMPT:
            continue
        hits = sorted(set(_tokens(normalised)) & MODEL_OUTCOME_TOKENS)
        if hits:
            refusals.append(
                Refusal(
                    check_id="MOS-TRAIN-204",
                    code="reason_code_is_a_model_outcome",
                    message=(
                        f"reason_code {normalised!r} names a model outcome via "
                        f"{hits}; MOS-TRAIN-204: the vocabulary is a statement about "
                        "the IMAGE or the PATIENT. Excluding cases because the model "
                        "does badly on them is how a training corpus becomes optimistic, "
                        "and it is invisible from the sealed manifest (MOS-TRAIN-203)."
                    ),
                    observed=normalised,
                    detail={"tokens": hits},
                )
            )
    if refusals:
        raise CurationRefused(refusals)
    return codes


def assert_reason_code(decision: str, reason_code: str | None) -> None:
    """The pairing rule of `MOS-TRAIN-080`, checked before the row reaches the CHECK.

    The database carries `CHECK ((decision = 'exclude') = (reason_code IS NOT NULL))` and
    that is the binding constraint. This function exists so the caller gets the
    requirement id and the remedy rather than a 23514 with a constraint name in it, and it
    MUST NOT be the only enforcement -- `MOS-EVID-013`'s posture (the database, not the
    application) is the house style for everything in the evidence plane.
    """
    if decision not in DECISIONS:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-080",
                    code="unknown_decision",
                    message=f"decision must be one of {DECISIONS}",
                    observed=decision,
                    bound=list(DECISIONS),
                ),
            )
        )
    if decision == "exclude" and not reason_code:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-203",
                    code="exclusion_without_reason",
                    message="every excluded candidate is retained WITH a reason code; "
                            "an exclusion with no reason is an absence, and "
                            "MOS-TRAIN-080 requires the queue to show an exclusion "
                            "rather than an absence",
                    observed=None,
                    bound=list(PLATFORM_REASON_CODES),
                ),
            )
        )
    if decision != "exclude" and reason_code:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-080",
                    code="reason_without_exclusion",
                    message=f"decision {decision!r} carries a reason code; only an "
                            "exclusion does",
                    observed=reason_code,
                ),
            )
        )
    if reason_code is not None and reason_code not in PLATFORM_REASON_CODES:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-080",
                    code="reason_code_outside_vocabulary",
                    message=(
                        f"reason_code {reason_code!r} is not in MOS-TRAIN-080's closed "
                        "set. A finer code from the Dataset's declared vocabulary "
                        "(MOS-TRAIN-203) belongs in `note`, beside the platform code, "
                        "until `datasets` carries a column for the vocabulary."
                    ),
                    observed=reason_code,
                    bound=list(PLATFORM_REASON_CODES),
                ),
            )
        )
    # `screen_vocabulary` is applied to the code actually being written as well as to a
    # declared vocabulary: MOS-TRAIN-080's set passes it, so this costs nothing on the
    # happy path and catches a caller who reached the column by another route.
    if reason_code is not None:
        screen_vocabulary((reason_code,))


def platform_code_for(declared: str, mapping: Sequence[tuple[str, str]]) -> str:
    """Map a Dataset-declared code to the platform code the column accepts.

    Present because `MOS-TRAIN-203`'s "declared on the `Dataset`" and `MOS-TRAIN-080`'s
    closed set are both normative and are different sets. `mapping` is the Dataset's own
    declaration, screened by `screen_vocabulary` before it gets here; an unmapped code is
    refused rather than defaulted, because a default would silently relabel every
    exclusion nobody mapped as `out_of_scope` and `MOS-TRAIN-081` reproduces that count in
    a report.
    """
    for declared_code, platform_code in mapping:
        if declared_code == declared:
            if platform_code not in PLATFORM_REASON_CODES:
                raise CurationRefused(
                    (
                        Refusal(
                            check_id="MOS-TRAIN-080",
                            code="mapping_target_outside_vocabulary",
                            message=f"{declared!r} maps to {platform_code!r}, which is "
                                    "not a platform reason code",
                            observed=platform_code,
                            bound=list(PLATFORM_REASON_CODES),
                        ),
                    )
                )
            return platform_code
    raise CurationRefused(
        (
            Refusal(
                check_id="MOS-TRAIN-203",
                code="declared_code_unmapped",
                message=f"the Dataset's vocabulary declares {declared!r} with no "
                        "platform reason code to record it under",
                observed=declared,
                bound=[d for d, _ in mapping],
            ),
        )
    )
