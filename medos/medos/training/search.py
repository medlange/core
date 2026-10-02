# SPDX-License-Identifier: Apache-2.0
"""`ConfigurationSearch`: the record, the space, the budget, the single nomination. 17.7.6.

`MOS-TRAIN-213` defines the thing broadly on purpose: "A `ConfigurationSearch` is any
procedure that produces more than one trained artifact from one cohort and retains a subset
of them by a score computed on data. This includes an `AutoRunner` sweep, an nnU-Net
configuration comparison across `2d` / `3d_fullres` / `3d_lowres` / cascade, a learning-rate
or augmentation sweep, a checkpoint-epoch selection across more than one candidate epoch,
and an ensemble-membership choice ... Any procedure meeting this definition MUST be
recorded as a `ConfigurationSearch`, whether or not the tool that performed it calls itself
AutoML."

THE ARITHMETIC, WHICH IS THE WHOLE REASON THIS RECORD EXISTS
--------------------------------------------------------------
"A search that trains N configurations and keeps the one with the best score on partition P
has performed a maximum over N noisy estimates of the same underlying quantity, and the
maximum of N noisy estimates is biased upward ... a search over 30 configurations that were
*all genuinely equal* is expected to report a winner about 0.021 Dice above the truth. Two
Dice points, manufactured entirely out of noise, in the number everybody will quote."

"That number does not reproduce ... The first place it fails to reproduce is the second
site, where it presents as an unexplained two-point drop with no cause anywhere in the
deployment: the image digest matches, the golden fixture matches, the envelope fits, the
conversion equivalence passes. Nothing is broken. The vendor figure was never real."

So every field this module writes exists to make that arithmetic visible to a reader:
`trials_completed` is the N the maximum was taken over, `selection_margin` is how far ahead
the winner is, and `capability_seed_variance.sd` (`MOS-TRAIN-127`) is how far apart two runs
of the same code land. `MOS-TRAIN-176` item 15 requires the first two to be rendered on the
same line as the third, with the literal marker `selection not distinguishable from
run-to-run noise` when the margin does not exceed the noise.

FOUR THINGS THIS MODULE REFUSES
--------------------------------
  `MOS-TRAIN-216`  a second nomination. The database enforces it too
                   (`training_runs_one_nomination`); this is the message.
  `MOS-TRAIN-220`  a space that is code, and a space that searches over a quantity the
                   fingerprint froze. "A quantity cannot be both derived from the cohort
                   and searched over, and a space that does both silently overwrites the
                   frozen value with the searched one."
  `MOS-TRAIN-234`  a budget with an absent bound. "an absent bound is a registration error,
                   not an unlimited one."
  `MOS-TRAIN-214`  `test` in `read_partitions`. The column cannot hold it; this refuses
                   before the column has to.

WHAT IS NOT HERE
-----------------
No ranking function, no `promote_best()`, no `approve_all()`. `MOS-TRAIN-233`: "A `PASS`
verdict on a trial MUST NOT nominate it, and a ranking view MUST NOT carry a promotion
control ... 'promote the top-ranked candidate', 'promote any candidate exceeding the
incumbent by d', and 'promote automatically when exactly one candidate passes' are all the
forbidden edge of `MOS-TRAIN-005` with a sweep in front of it." `nominate()` takes an
explicit `training_run_id` and a rationale from a caller; it cannot compute one.

Spec: MOS-TRAIN-212 to MOS-TRAIN-222, MOS-TRAIN-234 to MOS-TRAIN-236, MOS-EVID-008,
MOS-EVID-030.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import current_tenant, tenant_tx
from medos.sdk.canonical import canonical_bytes, new_ulid, sha256_hex
from medos.sdk.errors import Refusal, SearchRefused, TrainingError

__all__ = [
    "BUDGET_KEYS",
    "READABLE_PARTITIONS",
    "STOP_REASONS",
    "STRATEGIES",
    "create",
    "get",
    "nominate",
    "selection_is_noise",
    "space_digest",
    "space_problems",
    "trial_sequence",
]

STRATEGIES: Final[tuple[str, ...]] = ("grid", "random", "adaptive")
READABLE_PARTITIONS: Final[frozenset[str]] = frozenset({"train", "tune"})

#: `MOS-TRAIN-234`, verbatim: "`budget` MUST declare `max_trials`, `max_gpu_hours`,
#: `max_wall_clock_hours`, `max_ensemble_members` and `max_footprint_bytes`."
BUDGET_KEYS: Final[tuple[str, ...]] = (
    "max_trials", "max_gpu_hours", "max_wall_clock_hours", "max_ensemble_members",
    "max_footprint_bytes",
)

#: `MOS-TRAIN-218`'s `cost.stop_reason` comment, transcribed.
STOP_REASONS: Final[tuple[str, ...]] = (
    "space_exhausted", "max_trials", "max_gpu_hours", "max_wall_clock_hours",
    "cancelled", "failed",
)

_COLUMNS = """
    id, public_id, tenant_id, capability_id, parent_search_id, dataset_version_id,
    split_id, split_digest, annotation_digest, backend, fingerprint_digest,
    space, space_digest, strategy, seed, read_partitions, selection_metric,
    selection_partition, selection_folds, selection_rule, trials_planned,
    trials_completed, trials_failed, nominated_training_run_id,
    runner_up_training_run_id, selection_margin, budget, cost, code_commit,
    image_digest, state, search_digest, started_at, finished_at, created_at, updated_at
"""


def _refuse(refusals: Sequence[Refusal]) -> None:
    raise SearchRefused(tuple(refusals))


def _one(check_id: str, code: str, message: str, **kw: Any) -> Refusal:
    return Refusal(check_id=check_id, code=code, message=message, **kw)


def space_digest(space: Mapping[str, Any]) -> str:
    """`sha256:` over the JCS form. `MOS-TRAIN-220`: digested under JCS (`MOS-EVID-008`)."""
    return "sha256:" + sha256_hex(canonical_bytes(dict(space)))


def space_problems(space: Mapping[str, Any]) -> tuple[Refusal, ...]:
    """Everything wrong with a search space. `MOS-TRAIN-220`.

    "It MUST NOT be a Python callable, an expression string, a lambda or a template
    evaluated at search time, for the reason `MOS-EVID-076` gives for `AcceptanceCriteria`:
    a space that is code has no digest that means anything. A field named in
    `derived_from_fingerprint` MUST NOT also appear as a search axis, and registration of a
    space violating that MUST be refused."
    """
    out: list[Refusal] = []
    axes = list(space.get("axes") or ())
    if not axes:
        out.append(
            _one("MOS-TRAIN-220", "space_has_no_axes",
                 "a declarative space names at least one axis", observed=0, bound=1)
        )

    axis_names: list[str] = []
    for i, axis in enumerate(axes):
        if not isinstance(axis, Mapping) or "name" not in axis or "values" not in axis:
            out.append(
                _one("MOS-TRAIN-220", "axis_is_not_declarative",
                     f"axis {i} MUST be {{name, values}}; an expression, a callable or a "
                     "template is not a space that can be digested",
                     observed=repr(axis)[:120])
            )
            continue
        axis_names.append(str(axis["name"]))
        values = axis["values"]
        if not isinstance(values, Sequence) or isinstance(values, str) or not values:
            out.append(
                _one("MOS-TRAIN-220", "axis_values_are_not_a_list",
                     f"axis {axis['name']!r} MUST enumerate its values",
                     observed=repr(values)[:120])
            )
            continue
        for value in values:
            if callable(value) or (
                isinstance(value, str) and (value.startswith("$") or "lambda" in value)
            ):
                out.append(
                    _one(
                        "MOS-TRAIN-220",
                        "space_is_code",
                        f"axis {axis['name']!r} carries {value!r}, which is an expression "
                        "evaluated at search time. A space that is code has no digest "
                        "that means anything",
                        observed=repr(value)[:120],
                    )
                )

    frozen = {str(f) for f in (space.get("derived_from_fingerprint") or ())}
    overlap = sorted(frozen.intersection(axis_names))
    if overlap:
        out.append(
            _one(
                "MOS-TRAIN-220",
                "searched_a_frozen_quantity",
                f"{overlap} appear both in derived_from_fingerprint and as search axes. A "
                "quantity cannot be both derived from the cohort and searched over, and a "
                "space that does both silently overwrites the frozen value with the "
                "searched one (MOS-TRAIN-223, MOS-TRAIN-225)",
                observed=overlap,
                bound=[],
            )
        )
    return tuple(out)


def trial_sequence(
    space: Mapping[str, Any], *, seed: int, fingerprint_digest: str
) -> tuple[dict[str, Any], ...]:
    """The configurations a `grid` or `random` search will try, in order. `MOS-TRAIN-235`.

    "For `strategy: grid` or `random`, the sequence of trial configurations MUST be a pure
    function of `(space_digest, seed, fingerprint_digest)`, and re-running MUST produce the
    identical sequence of `config_digest` values in the identical order."

    `adaptive` is deliberately NOT computable here: trial *k* depends on the scores of
    trials 1..k-1, "which is why `search_trial_score` MUST be persisted per trial rather
    than only for the winner". A caller asking for an adaptive sequence in advance is
    asking for something that does not exist, and gets a `ValueError` rather than a list.

    `MOS-TRAIN-235` also fixes what is NOT claimed: "Bit-exact reproduction of trial
    *weights* MUST NOT be required and MUST NOT be claimed ... What is reproducible is the
    trajectory: which configurations were tried, in what order, and which one won."
    """
    strategy = str(space.get("strategy") or "grid")
    if strategy not in ("grid", "random"):
        raise ValueError(
            f"strategy {strategy!r} has no sequence computable in advance; an adaptive "
            "search's trial k depends on the scores of trials 1..k-1 (MOS-TRAIN-235)"
        )
    problems = space_problems(space)
    if problems:
        _refuse(problems)

    axes = [(str(a["name"]), list(a["values"])) for a in space["axes"]]
    constraints = list(space.get("constraints") or ())

    points: list[dict[str, Any]] = [{}]
    for name, values in axes:
        points = [{**p, name: v} for p in points for v in values]

    kept = [p for p in points if not _forbidden(p, constraints)]

    if strategy == "random":
        # A deterministic permutation, keyed exactly as MOS-TRAIN-235 requires. `sorted`
        # on a digest is a pure function of the three inputs and needs no RNG whose
        # implementation could change between releases.
        key = f"{space_digest(space)}|{seed}|{fingerprint_digest}"
        kept.sort(key=lambda p: sha256_hex((key + canonical_bytes(p).decode()).encode()))

    return tuple(
        {
            "trial_index": i,
            "config": p,
            "config_digest": "sha256:" + sha256_hex(canonical_bytes(p)),
        }
        for i, p in enumerate(kept)
    )


def _forbidden(point: Mapping[str, Any], constraints: Sequence[Any]) -> bool:
    """`{when: {...}, forbid: {...}}` -- the one constraint form `MOS-TRAIN-220` shows."""
    for rule in constraints:
        if not isinstance(rule, Mapping):
            continue
        when = dict(rule.get("when") or {})
        forbid = dict(rule.get("forbid") or {})
        if all(point.get(k) == v for k, v in when.items()) and all(
            point.get(k) == v for k, v in forbid.items()
        ):
            return True
    return False


def create(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str,
    dataset_version_id: str,
    split_id: str,
    split_digest: str,
    annotation_digest: str,
    backend: Mapping[str, Any],
    fingerprint_digest: str,
    space: Mapping[str, Any],
    strategy: str,
    seed: int,
    selection_metric: str,
    selection_partition: str,
    selection_rule: str,
    trials_planned: int,
    budget: Mapping[str, Any],
    code_commit: str,
    image_digest: str,
    read_partitions: Sequence[str] = ("train", "tune"),
    selection_folds: Sequence[int] | None = None,
    parent_search_id: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Record a `ConfigurationSearch` at `PENDING`. `MOS-TRAIN-218`."""
    problems: list[Refusal] = list(space_problems(space))

    if strategy not in STRATEGIES:
        problems.append(
            _one("MOS-TRAIN-218", "unknown_strategy",
                 f"strategy MUST be one of {STRATEGIES}", observed=strategy,
                 bound=list(STRATEGIES))
        )

    outside = sorted(set(read_partitions) - READABLE_PARTITIONS)
    if outside or not read_partitions:
        problems.append(
            _one(
                "MOS-TRAIN-214",
                "search_read_partition_not_permitted",
                "a ConfigurationSearch and every process it spawns MUST be confined to "
                "the train and tune partitions of the frozen split. A search that has "
                "read test has not produced a candidate; it has produced a number about "
                "itself",
                observed=list(read_partitions),
                bound=sorted(READABLE_PARTITIONS),
            )
        )

    if selection_partition not in ("tune", "train"):
        problems.append(
            _one("MOS-TRAIN-215", "selection_partition_not_permitted",
                 "the selection metric MUST be computed on tune, or on cross-validation "
                 "folds within train",
                 observed=selection_partition, bound=["tune", "train"])
        )
    if (selection_partition == "train") != (selection_folds is not None):
        problems.append(
            _one("MOS-TRAIN-215", "selection_folds_binding",
                 "selection_folds is set exactly when the selection partition is train",
                 observed={"partition": selection_partition, "folds": selection_folds})
        )

    absent = [k for k in BUDGET_KEYS if k not in budget]
    if absent:
        problems.append(
            _one(
                "MOS-TRAIN-234",
                "budget_bound_absent",
                f"budget is missing {absent}. An absent bound is a registration error, "
                "not an unlimited one",
                observed=sorted(budget.keys()),
                bound=list(BUDGET_KEYS),
            )
        )
    elif int(budget["max_trials"]) < int(trials_planned):
        problems.append(
            _one("MOS-TRAIN-234", "planned_trials_exceed_the_budget",
                 "the orchestrator MUST stop the search at the first bound reached",
                 observed=trials_planned, bound=budget["max_trials"])
        )

    if problems:
        _refuse(problems)

    digest = "sha256:" + sha256_hex(
        canonical_bytes(
            {
                "capability_id": capability_id,
                "split_digest": split_digest,
                "annotation_digest": annotation_digest,
                "backend": dict(backend),
                "fingerprint_digest": fingerprint_digest,
                "space_digest": space_digest(space),
                "strategy": strategy,
                "seed": int(seed),
                "read_partitions": sorted(read_partitions),
                "selection_metric": selection_metric,
                "selection_partition": selection_partition,
                "selection_folds": list(selection_folds) if selection_folds else None,
                "budget": dict(budget),
                "code_commit": code_commit,
                "image_digest": image_digest,
                "parent_search_id": parent_search_id,
            }
        )
    )

    tenant = str(tenant_id or current_tenant())
    with tenant_tx(conn, tenant) as tx:
        row = tx.execute(
            f"""
            INSERT INTO configuration_searches
                (public_id, tenant_id, capability_id, parent_search_id,
                 dataset_version_id, split_id, split_digest, annotation_digest, backend,
                 fingerprint_digest, space, space_digest, strategy, seed, read_partitions,
                 selection_metric, selection_partition, selection_folds, selection_rule,
                 trials_planned, budget, code_commit, image_digest, search_digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (
                new_ulid("cs"), _uuid.UUID(tenant), capability_id,
                _uuid.UUID(str(parent_search_id)) if parent_search_id else None,
                _uuid.UUID(str(dataset_version_id)), _uuid.UUID(str(split_id)),
                split_digest, annotation_digest, Jsonb(dict(backend)), fingerprint_digest,
                Jsonb(dict(space)), space_digest(space), strategy, int(seed),
                list(read_partitions), selection_metric, selection_partition,
                list(selection_folds) if selection_folds else None, selection_rule,
                int(trials_planned), Jsonb(dict(budget)), code_commit, image_digest,
                digest,
            ),
        ).fetchone()
    return dict(row)


def nominate(
    conn: psycopg.Connection[Any],
    *,
    search_id: str,
    training_run_id: str,
    runner_up_training_run_id: str,
    selection_margin: float,
    cost: Mapping[str, Any],
    trials_completed: int,
    trials_failed: int = 0,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Nominate EXACTLY ONE trial and close the search. `MOS-TRAIN-216`.

    "A `ConfigurationSearch` MUST nominate **exactly one** trial. Only the nominated trial
    MAY be registered as a `ModelVersion` under `MOS-TRAIN-138` and only the nominated
    trial MAY be the subject of a candidate `EvaluationRun` on `test`. Evaluating all N
    trials on `test` and keeping the best is the same defect with more GPU hours and a
    cleaner-looking audit trail."

    The nomination is the CALLER's, never this function's: there is no scoring here and no
    `best_of()`. `MOS-TRAIN-233` forbids a ranking view from carrying a promotion control,
    and a repository function that could pick the winner would be that control with a
    different name.

    A second nomination on the same search is refused by `training_runs_one_nomination`
    (0013, and `MOS-TRAIN-219` requires that index by name). `MOS-TRAIN-217`'s remedy is a
    NEW search row with `parent_search_id` set, a rationale, "and a fresh increment of
    `test_exposure_count`".
    """
    if not cost.get("stop_reason"):
        _refuse(
            [
                _one("MOS-TRAIN-234", "stop_reason_not_recorded",
                     f"cost.stop_reason MUST be one of {STOP_REASONS}",
                     observed=dict(cost), bound=list(STOP_REASONS))
            ]
        )
    if str(cost["stop_reason"]) not in STOP_REASONS:
        _refuse(
            [
                _one("MOS-TRAIN-234", "unknown_stop_reason",
                     f"cost.stop_reason MUST be one of {STOP_REASONS}",
                     observed=cost["stop_reason"], bound=list(STOP_REASONS))
            ]
        )
    if training_run_id == runner_up_training_run_id:
        _refuse(
            [
                _one("MOS-TRAIN-216", "nomination_equals_runner_up",
                     "the nominated trial and the runner-up are different trials; "
                     "selection_margin is nominated minus runner-up and has no meaning "
                     "otherwise",
                     observed=training_run_id)
            ]
        )

    with tenant_tx(conn, tenant_id) as tx:
        search = tx.execute(
            f"SELECT {_COLUMNS} FROM configuration_searches WHERE public_id = %s "
            "OR id::text = %s",
            (search_id, search_id),
        ).fetchone()
        if search is None:
            raise TrainingError(f"no configuration search {search_id}")
        s = dict(search)
        if s["state"] == "SUCCEEDED":
            _refuse(
                [
                    _one(
                        "MOS-TRAIN-217",
                        "search_already_nominated",
                        "a trial that was not nominated MAY be nominated later, and doing "
                        "so MUST be an explicit recorded act: a new ConfigurationSearch "
                        "row referencing the original by parent_search_id, naming the new "
                        "nomination, carrying a rationale, and incurring a fresh "
                        "increment of test_exposure_count. A silent second nomination off "
                        "the back of a first test run is selection on test performed one "
                        "candidate at a time",
                        observed=s["nominated_training_run_id"],
                    )
                ]
            )

        # The nomination flag on the run is what `training_runs_one_nomination` enforces.
        tx.execute(
            "UPDATE training_runs SET nominated = true WHERE id = %s AND search_id = %s",
            (_uuid.UUID(str(training_run_id)), s["id"]),
        )
        row = tx.execute(
            f"""
            UPDATE configuration_searches
               SET state = 'SUCCEEDED', finished_at = %s,
                   nominated_training_run_id = %s, runner_up_training_run_id = %s,
                   selection_margin = %s, cost = %s, trials_completed = %s,
                   trials_failed = %s
             WHERE id = %s
            RETURNING {_COLUMNS}
            """,
            (
                now or datetime.now(UTC), _uuid.UUID(str(training_run_id)),
                _uuid.UUID(str(runner_up_training_run_id)), float(selection_margin),
                Jsonb(dict(cost)), int(trials_completed), int(trials_failed), s["id"],
            ),
        ).fetchone()
    return dict(row)


def get(
    conn: psycopg.Connection[Any], ident: str, *, tenant_id: str | None = None
) -> dict[str, Any]:
    """One search by `public_id` or uuid."""
    column = "public_id" if ident.startswith("cs_") else "id::text"
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            f"SELECT {_COLUMNS} FROM configuration_searches WHERE {column} = %s", (ident,)
        ).fetchone()
    if row is None:
        raise TrainingError(f"no configuration search {ident}")
    return dict(row)


def selection_is_noise(selection_margin: float | None, seed_sd: float | None) -> bool:
    """`MOS-TRAIN-176` item 15's test, as one function so two surfaces cannot disagree.

    "`selection_margin` MUST be rendered on the same line as `seed_variance.sd`
    (`MOS-TRAIN-127`), and when `selection_margin <= seed_variance.sd` the item MUST carry
    the literal marker `selection not distinguishable from run-to-run noise`."

    `MOS-TRAIN-236` is the reason it is a single comparison and not a test: "A search that
    spent 81 GPU-hours to produce a winner 0.006 Dice ahead of the runner-up, against a
    measured seed standard deviation of 0.011, selected noise -- and the dossier must make
    that arithmetic visible without requiring the approver to perform it."
    """
    if selection_margin is None or seed_sd is None:
        return False
    return float(selection_margin) <= float(seed_sd)
