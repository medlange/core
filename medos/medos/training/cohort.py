# SPDX-License-Identifier: Apache-2.0
"""The cohort resolver the training and search jobs are handed. 403, never an empty set.

`MOS-TRAIN-141`: "The training and selection jobs MUST NOT be able to read the `test`
partition at all. The cohort resolver MUST be handed a split view filtered to
`{train, tune}` and MUST return **403, not an empty set**, on a `test` request. An empty
set is indistinguishable from 'no such patient', and a retry loop around an empty set is
how a silent read becomes a routine one."

`MOS-TRAIN-214` extends the same rule to every process a `ConfigurationSearch` spawns:
"the search driver, each trial process, the ranking step, the ensemble builder and any
interactive session attached to the search's workspace."

WHY A RESOLVER AND NOT A QUERY HELPER
--------------------------------------
`MOS-TRAIN-141`'s second sentence is the load-bearing one, and it is about a FILESYSTEM:
chapter 17 acceptance check 15 requires that "the training container's resolver rejects a
filesystem path, a bucket prefix and a glob as a cohort argument". A helper that took a
partition name and appended it to a `WHERE` clause would satisfy the first sentence and
none of the second, because the argument it accepts is still a string the caller composed.

So `CohortRequest` is a typed object with a closed `partition` vocabulary, and
`resolve()` is the only way to turn one into case rows. There is no `path=`, no `prefix=`,
no `glob=` and no `sql=` anywhere in this module, and `reject_location_argument()` exists
so that a caller which was handed one from a configuration file finds out at the boundary
rather than reading whatever happens to be on disk.

WHAT THIS MODULE IS NOT
-----------------------
It is not a second implementation of the split. `dataset_split_members` (0006) is the
frozen manifest and `medos.evidence.repo.freeze_split` is what writes it; this reads it
through a view that cannot express `test`. `MOS-REL-061`'s rule about one implementation
of preprocessing has the same shape here: a resolver that could reconstruct a partition
would be a second split.

Spec: MOS-TRAIN-120, MOS-TRAIN-141, MOS-TRAIN-207, MOS-TRAIN-214, MOS-EVID-030,
MOS-EVID-031. Reads the database; writes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import psycopg

from medos.db.tenancy import tenant_tx
from medos.sdk.errors import PartitionForbidden, Refusal

__all__ = [
    "PIPELINE_READABLE_PARTITIONS",
    "CohortCase",
    "CohortRequest",
    "assert_readable",
    "reject_location_argument",
    "resolve",
]

#: `MOS-TRAIN-214`: "Chapter 7's partition vocabulary already supplies the partition a
#: search may never read: it is `test` ... and this specification adds **no fourth
#: partition** -- `val` remains not a permitted value and a 'search validation' partition
#: MUST NOT be introduced."
PIPELINE_READABLE_PARTITIONS: Final[frozenset[str]] = frozenset({"train", "tune"})

#: Every partition the frozen split can hold (`MOS-EVID-031`). `excluded` is readable by
#: the pipeline in the sense that it can be counted, and is never a source of cases.
_ALL_PARTITIONS: Final[frozenset[str]] = frozenset({"train", "tune", "test", "excluded"})

# What a cohort argument MUST NOT be. Chapter 17 acceptance check 15 names three shapes;
# the fourth (a URI) is here because a bucket prefix written as `s3://...` is the same
# argument with a scheme in front of it.
_LOCATION_SHAPES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("glob", re.compile(r"[*?\[\]]")),
    ("uri", re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")),
    ("absolute_path", re.compile(r"^(/|[A-Za-z]:[\\/])")),
    ("path_separator", re.compile(r"[\\/]")),
)


@dataclass(frozen=True)
class CohortRequest:
    """A cohort request the resolver can answer. A typed object, never a location.

    `split_id` and `partition` are the whole of it. There is no `limit`, no `order_by` and
    no `where`: a run reads the partition it was bound to, in the manifest's order, or it
    reads nothing. `MOS-EVID-028` makes the split "a frozen manifest of rows, never a
    random seed", and a resolver that could subset it would reintroduce the seed.
    """

    split_id: str
    partition: str

    def __post_init__(self) -> None:
        if self.partition not in _ALL_PARTITIONS:
            raise ValueError(
                f"partition {self.partition!r} is not one of {sorted(_ALL_PARTITIONS)}; "
                "MOS-TRAIN-214 adds no fourth partition and `val` is not a value"
            )
        reject_location_argument(self.split_id, field="split_id")


@dataclass(frozen=True)
class CohortCase:
    """One row of the resolved cohort: the case keys, never a path.

    The resolver hands back UIDs and keys. Where the pixels are is the object store's
    question and `MOS-TRAIN-141`'s point is that a training container never gets to answer
    it for itself.
    """

    patient_key: str
    case_key: str
    study_instance_uid: str
    series_instance_uid: str
    partition: str
    fold: int | None
    stratum: Mapping[str, Any]


def reject_location_argument(value: str, *, field: str) -> None:
    """Refuse a filesystem path, a bucket prefix, a URI or a glob. Acceptance check 15.

    "Assert the training container's resolver rejects a filesystem path, a bucket prefix
    and a glob as a cohort argument."

    Raised as `ValueError` and not as a `Refusal`: a caller that passed a glob has a
    programming defect, not a cohort that failed a check, and conflating the two would put
    a developer's mistake into a `ValidationReport`'s refusal list.
    """
    for shape, pattern in _LOCATION_SHAPES:
        if pattern.search(value):
            raise ValueError(
                f"{field}={value!r} looks like a {shape}. The cohort resolver takes a "
                "split id and a partition name and nothing else (MOS-TRAIN-141); a path, "
                "a bucket prefix and a glob are all refused, because a container that can "
                "name its own data can name the test partition."
            )


def assert_readable(partition: str, *, principal: str) -> None:
    """`MOS-TRAIN-141`'s 403. Raises `PartitionForbidden`; never returns an empty set.

    `principal` is named in the refusal because `MOS-TRAIN-214` makes the rule about WHO
    is asking -- the search driver, a trial process, the ranking step, an interactive
    session -- and a refusal that does not say which of them tried is a refusal nobody can
    act on.
    """
    if partition in PIPELINE_READABLE_PARTITIONS:
        return
    raise PartitionForbidden(
        (
            Refusal(
                check_id="MOS-TRAIN-141",
                code="partition_not_readable_by_pipeline",
                message=(
                    f"principal {principal!r} requested partition {partition!r}; the "
                    "training and selection jobs are handed a split view filtered to "
                    "{train, tune} and this is a 403, not an empty set. An empty set is "
                    "indistinguishable from 'no such patient', and a retry loop around "
                    "an empty set is how a silent read becomes a routine one"
                ),
                observed=partition,
                bound=sorted(PIPELINE_READABLE_PARTITIONS),
                detail={"principal": principal, "requested_partition": partition},
            ),
        )
    )


def resolve(
    conn: psycopg.Connection[Any],
    request: CohortRequest,
    *,
    principal: str,
    tenant_id: str | None = None,
) -> tuple[CohortCase, ...]:
    """The cases of one partition of one frozen split, in manifest order.

    Refuses `test` and `excluded` with a 403 BEFORE touching the database, so there is no
    query to get wrong and no result set to filter. The order is
    `(patient_key, case_key, series_instance_uid)`, which is `MOS-EVID-017`'s manifest
    order and is deterministic without a seed.
    """
    assert_readable(request.partition, principal=principal)

    sql = """
        SELECT m.patient_key,
               c.case_key,
               c.study_instance_uid,
               c.series_instance_uid,
               m.partition,
               m.stratum
          FROM dataset_split_members m
          JOIN dataset_splits s ON s.id = m.split_id
          JOIN dataset_cases c
            ON c.dataset_version_id = s.dataset_version_id
           AND c.patient_key = m.patient_key
         WHERE m.split_id = %s AND m.partition = %s
         ORDER BY m.patient_key, c.case_key, c.series_instance_uid
    """
    with tenant_tx(conn, tenant_id) as tx:
        rows = tx.execute(sql, (request.split_id, request.partition)).fetchall()

    out: list[CohortCase] = []
    for r in rows:
        row = dict(r)
        stratum = row.get("stratum") or {}
        # `MOS-EVID-030`'s `fold` member lives inside the split manifest line, which 0006
        # stores in `stratum`. `MOS-TRAIN-137`: folds are expressed WITHIN `train` and
        # MUST NOT be implemented as a re-partition that touches `tune` or `test`.
        fold = stratum.get("fold") if isinstance(stratum, dict) else None
        out.append(
            CohortCase(
                patient_key=row["patient_key"],
                case_key=row["case_key"],
                study_instance_uid=row["study_instance_uid"],
                series_instance_uid=row["series_instance_uid"],
                partition=row["partition"],
                fold=int(fold) if isinstance(fold, int) else None,
                stratum=dict(stratum) if isinstance(stratum, dict) else {},
            )
        )
    return tuple(out)


def folds_of(cases: Sequence[CohortCase]) -> dict[int, tuple[str, ...]]:
    """`{fold: (patient_key, ...)}` over cases that declare one.

    `MOS-TRAIN-032`: "nnU-Net's built-in cross-validation split MUST NOT be used as a
    `DatasetSplit` ... The pipeline MUST hand nnU-Net a fold assignment derived from the
    frozen `DatasetSplit`." This is that derivation, and it can only see `train` cases
    because `resolve()` is the only way to obtain them.
    """
    out: dict[int, set[str]] = {}
    for case in cases:
        if case.fold is None:
            continue
        out.setdefault(case.fold, set()).add(case.patient_key)
    return {k: tuple(sorted(v)) for k, v in sorted(out.items())}
