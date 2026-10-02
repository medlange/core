# SPDX-License-Identifier: Apache-2.0
"""Machine-readable refusals for the evidence plane. Chapter 7, chapter 17 section 17.6.4.

WHY A REFUSAL IS A DATA STRUCTURE AND NOT A STRING

`MOS-TRAIN-088`: "A `fail` MUST block sealing." `MOS-EVID-034`: "A split MUST NOT be
frozen while any check is `fail` and unwaived." Neither says "log a warning", and both are
the kind of check whose value collapses the moment a human can read past it. A refusal
therefore carries the check id, a stable code, the observed value and the bound, so that

  * the caller can render it without parsing prose,
  * `MOS-TRAIN-091`'s waiver can name the exact `check_id` it waives, and
  * `MOS-EVID-036`'s "there is no silent waiver" has something to reproduce verbatim in
    the `ValidationReport` that cites the cohort.

The `class` member follows CONTRACT.md section 9 and chapter 10's RFC 9457 model: an
evidence refusal is a `clinical_rejection`-shaped outcome -- a correct, informative
verdict about the data -- and MUST NOT be reported as a transport or system failure. A
cohort that fails C1 is not an error; it is an answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EvidenceError",
    "Refusal",
    # The shared base of the refusal hierarchy. Exported because the model-preparation
    # product subclasses it seven times and catches it by name; see its docstring.
    "RefusalError",
    "SealRefused",
    "FreezeRefused",
    "EvaluationRefused",
]


class EvidenceError(RuntimeError):
    """Base for everything this package raises."""


@dataclass(frozen=True)
class Refusal:
    """One failed blocking check, in the form a report reproduces verbatim.

    `check_id` is the identifier the owning requirement uses -- `C1`..`C7` for the corpus
    stratification checks of `MOS-TRAIN-088`, `L1`..`L5` for the leakage checks of
    `MOS-EVID-034`, and the bare requirement id (`MOS-EVID-026`) for a gate that has no
    table of its own. It is what `MOS-TRAIN-091`'s and `MOS-EVID-036`'s waiver objects
    key on, so it MUST stay stable across releases.
    """

    check_id: str
    code: str
    message: str
    observed: Any = None
    bound: Any = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "code": self.code,
            "message": self.message,
            "observed": self.observed,
            "bound": self.bound,
            "detail": dict(self.detail),
        }


class RefusalError(EvidenceError):
    """Common behaviour: carry the refusals, render one readable line per refusal.

    PUBLIC, AND IT WAS NAMED `_RefusalError` UNTIL THE TWO PRODUCTS WERE SPLIT. The
    underscore said "internal to the evidence plane" and that was never true: eleven
    exception classes inherit from it, seven of them in `medos/medos/training/`, and
    `medos/medos/api/routes_training.py` catches it by name to render every refusal the
    model-preparation surface can raise. It is the shared base of the refusal hierarchy
    across BOTH deployables, which makes it about as public as a class gets.

    A private name reached across a product boundary is the shape of coupling that is
    invisible until somebody tries to separate the two, so it is exported now and
    `tests/unit/test_core_public_api.py` fails if Train reaches for anything that is
    not.
    """

    problem_type: str = "about:blank"
    title: str = "refused"

    def __init__(self, refusals: tuple[Refusal, ...] | list[Refusal]) -> None:
        self.refusals: tuple[Refusal, ...] = tuple(refusals)
        if not self.refusals:  # pragma: no cover - a refusal with no reason is a bug
            raise ValueError("a refusal MUST name at least one failed check")
        super().__init__(
            "; ".join(f"{r.check_id} {r.code}: {r.message}" for r in self.refusals)
        )

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(r.check_id for r in self.refusals)

    def as_problem(self) -> dict[str, Any]:
        """RFC 9457 `application/problem+json`, with the refusals as an extension member.

        `class` is `clinical_rejection` for the reason CONTRACT.md section 9 gives: to a
        caller, a refused seal and a dead database look identical unless the error model
        separates them, and only one of the two is information the caller must act on.
        """
        return {
            "type": self.problem_type,
            "title": self.title,
            "status": 422,
            "class": "clinical_rejection",
            "detail": str(self),
            "refusals": [r.as_dict() for r in self.refusals],
        }


class SealRefused(RefusalError):
    """A `DatasetVersion` MUST NOT be sealed. `MOS-TRAIN-088`, `MOS-TRAIN-208`.

    `MOS-TRAIN-208`: "A failure at any step MUST leave no `dataset_versions` row and no
    manifest object." Raising this is how that is honoured -- the row is never inserted
    and the manifest object, if it was written first, is removed by the compensating
    delete in `medos.evidence.repo.seal_dataset_version`.
    """

    problem_type = "https://medicalos.dev/problems/dataset-seal-refused"
    title = "the DatasetVersion was not sealed"


class FreezeRefused(RefusalError):
    """A `DatasetSplit` or `AnnotationSet` MUST NOT be frozen. `MOS-EVID-034`.

    "A split MUST NOT be frozen while any check is `fail` and unwaived." An unwaived L1
    hit is not a warning to be carried into the report; it is a split that does not exist.
    """

    problem_type = "https://medicalos.dev/problems/split-freeze-refused"
    title = "the split was not frozen"


class EvaluationRefused(RefusalError):
    """An `EvaluationRun` MUST NOT be bound, or MUST NOT reach `SUCCEEDED`.

    `MOS-EVID-061` ("a run missing any field MUST NOT reach state = SUCCEEDED"),
    `MOS-EVID-062` ("an evaluation run from an uncommitted working tree is not
    evidence") and `MOS-EVID-065` ("aggregates alone MUST NOT be stored") are all refusals
    of the same shape: the measurement may be arithmetically fine and is still not
    evidence, because something that makes it interpretable is absent. Raising rather
    than recording a warning is the difference between a run that cannot be cited and a
    run that can be cited by anyone who does not read the warning.
    """

    problem_type = "https://medicalos.dev/problems/evaluation-refused"
    title = "the evaluation run was refused"
