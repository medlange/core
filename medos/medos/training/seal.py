# SPDX-License-Identifier: Apache-2.0
"""`SealRun` -- the act of sealing, submitted and polled. Table 10.2-B rows R26 and R27.

WHAT THIS MODULE IS, IN ONE SENTENCE. It runs `MOS-UI-133`'s nine checks over a candidate
set BEFORE anything is created, and creates the `DatasetVersion` and the `DatasetSplit`
together or creates neither.

IT IS NOT A `Job`, AND `MOS-API-001` IS THE REASON THE ENTITY EXISTS RATHER THAN AN
OBSTACLE TO IT
--------------------------------------------------------------------------------------
"POST /api/v1/jobs is the only endpoint that may create a Job. No other path in the
system may create a Job." Chapter 17 forbids it independently and more strongly:
`MOS-TRAIN-121` C3 excludes `job.create` from the orchestrator account outright, C2
forbids the pipeline appearing between a `Job` and its `Result`, C4 requires a total
orchestrator outage to change the outcome of no `Job` at all. A seal that created a Job
would need a grant the pipeline identity MUST NOT hold and would put a training-plane
operation onto the lifecycle the clinical dispatcher watches. `MOS-API-056` already names
a long-running operation that is NOT a Job -- `EvaluationRun`, table 10.2-B row 42 -- and
this entity takes the same shape: `202` with a `Location`, polled with `Retry-After: 30`
(`MOS-API-057`).

WHY IT IS NOT SYNCHRONOUS EITHER
---------------------------------
`MOS-EVID-018`'s `series_pixel_digest` is a digest over stored pixel values "computed by
the caller that holds the pixels", and `MOS-EVID-034` L4's `dhash64` hashes the
normalised mid-axial slice. Both oblige the seal to retrieve every instance of every
series through the Gateway as `dataset_export` (`MOS-TRAIN-068`, `MOS-TRAIN-199`), and
`MOS-TRAIN-114`'s floor of thirty `test` patients puts the smallest sealable cohort near
a hundred and fifty. Minutes. The arithmetic is not the binding reason: `MOS-UI-134`
requires L3 and L4 to run "as a named, progress-reported step with the cohort size shown,
never as a silent wait", and a synchronous POST has nowhere to report progress from. A
socket that is either open or closed also cannot tell the operator whether a
`DatasetVersion` was created -- the state `MOS-UI-132` forbids leaving them in, and worse
here because `MOS-EVID-013` makes the object immutable and `MOS-API-011` answers a delete
with `409`.

THE ORDER IS `MOS-UI-131`'s AND IS STRICTER THAN THE PLATFORM REQUIRES
------------------------------------------------------------------------
`MOS-TRAIN-088` runs C1-C7 DURING the seal and `MOS-EVID-034` runs L1-L5 at freeze, both
after the point of no return. This driver runs all nine BEFORE it writes anything,
because "a `DatasetVersion` that seals and then fails its split freeze is immutable,
undeletable and useless, and the operator this surface exists for cannot clean it up".
The cost is that C1-C7 are evaluated twice -- once here as a gate and once inside
`seal_from_batch`, which records the report (`MOS-TRAIN-088` requires the full result
recorded whether it passes or blocks). They are the same function over the same records,
so the two cannot disagree; the alternative is the immutable orphan.

WHAT `check_battery` CANNOT DO, AND WHY THAT IS THE POINT
----------------------------------------------------------
It cannot report `pass` for a check that examined no pixels.
`medos/schemas/evidence/seal-run-1.0.0.json` requires `series_without_pixel_evidence: 0` on a
`pass`, requires a `skipped_reason` and an `operator_disclosure` on a `skipped`, and
requires all nine rows present. `_battery()` below is written so those follow from the
MEASUREMENT rather than from the author's intent: it reads `PixelEvidence`'s counters --
which `medos/medos/training/retrieval.py` increments only when a retrieval actually returned
pixels -- and the outcome falls out of them. There is no branch in which an author
decides that L3 passed.

On THIS deployment the answer is `skipped` for rows 7 and 8, because the Gateway refuses
the `dataset_export` consumer class with `503 DEID_NOT_IMPLEMENTED` and `MOS-DATA-037`
requires it to. `medos/medos/training/retrieval.py`'s docstring is the full argument. Rows 5
and 9 also skip, each for a stated structural reason -- there is no `AnnotationSet` at
seal time for `MOS-TRAIN-086`'s ceilings to be computed over, and an acceptance binding
may simply not have been recorded yet (`MOS-UI-146` requires the operator to be told,
which is a disclosure and not a block).

Spec: MOS-API-001, MOS-API-011, MOS-API-042, MOS-API-056, MOS-API-057, MOS-API-112,
MOS-DATA-021, MOS-DATA-037, MOS-EVID-013, MOS-EVID-015, MOS-EVID-026, MOS-EVID-028,
MOS-EVID-031, MOS-EVID-034, MOS-EVID-037, MOS-EVID-080, MOS-EVID-082, MOS-IMG-045,
MOS-SEC-105, MOS-TRAIN-080, MOS-TRAIN-081, MOS-TRAIN-086, MOS-TRAIN-088, MOS-TRAIN-092,
MOS-TRAIN-111, MOS-TRAIN-112, MOS-TRAIN-114, MOS-TRAIN-121, MOS-TRAIN-122, MOS-TRAIN-208,
MOS-TRAIN-209, MOS-UI-130, MOS-UI-131, MOS-UI-132, MOS-UI-133, MOS-UI-134, MOS-UI-144,
MOS-UI-145, MOS-UI-146, MOS-UI-158, MOS-UI-160.
"""

from __future__ import annotations

import logging
import tempfile
import threading
import uuid as _uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol

import psycopg
from psycopg.types.json import Jsonb

from medos.config.devmode import refuse_dev_value_outside_dev
from medos.db.tenancy import bind_current_tenant, reset_current_tenant, tenant_tx
from medos.evidence import acceptance as _acceptance
from medos.evidence import leakage as _leak
from medos.evidence import repo as _evrepo
from medos.evidence import stratification as _strat
from medos.evidence.manifest import SeriesRecord
from medos.evidence.profile import acquisition_profile
from medos.evidence.store import ManifestStore
from medos.sdk.canonical import new_ulid
from medos.sdk.errors import CurationRefused, Refusal
from medos.sdk.refusal import FreezeRefused, SealRefused
from medos.training import curation as _cur
from medos.training import retrieval as _retrieval
from medos.training import split as _split
from medos.training.retrieval import DatasetExportRetriever, PixelEvidence

__all__ = [
    "BATTERY_ROWS",
    "DEID_PROVENANCE_VAR",
    "TERMINAL_STATES",
    "DeidProvenance",
    "DeploymentNotDeclared",
    "SealDriver",
    "SealRunRow",
    "ThreadSealDriver",
    "execute",
    "get_seal_run",
    "load_deid_provenance",
    "new_seal_run_id",
    "submit",
]

log = logging.getLogger("medos.training.seal")

TERMINAL_STATES: Final[frozenset[str]] = frozenset({"SUCCEEDED", "REFUSED", "FAILED"})

#: `MOS-UI-133`'s table, transcribed: order, the row's check id, the individual ids it
#: covers, the owning requirement, whether it blocks, and whether it is cheap to preview.
#: A CONSTANT and not nine literals scattered through `_battery()`, because `MOS-API-112`
#: requires all nine present in every response and an array assembled by appending is an
#: array that can come up short.
BATTERY_ROWS: Final[tuple[dict[str, Any], ...]] = (
    {"order": 1, "check_id": "geometry_admissibility",
     "covers": ["geometry"], "owner": "MOS-TRAIN-111",
     "blocking": True, "cheap_to_preview": True},
    {"order": 2, "check_id": "corpus_composition_blocking",
     "covers": ["C1", "C2", "C3", "C5", "C7"], "owner": "MOS-TRAIN-088",
     "blocking": True, "cheap_to_preview": True},
    {"order": 3, "check_id": "corpus_composition_warning",
     "covers": ["C4", "C6"], "owner": "MOS-TRAIN-088",
     "blocking": False, "cheap_to_preview": True},
    {"order": 4, "check_id": "test_partition_floor",
     "covers": ["MOS-TRAIN-114"], "owner": "MOS-TRAIN-114",
     "blocking": True, "cheap_to_preview": True},
    {"order": 5, "check_id": "seeded_annotation_ceilings",
     "covers": ["MOS-TRAIN-086"], "owner": "MOS-TRAIN-086",
     "blocking": True, "cheap_to_preview": True},
    {"order": 6, "check_id": "leakage_key_set_disjointness",
     "covers": ["L1", "L2", "L5"], "owner": "MOS-EVID-034",
     "blocking": True, "cheap_to_preview": True},
    {"order": 7, "check_id": "leakage_pixel_identity",
     "covers": ["L3"], "owner": "MOS-EVID-034",
     "blocking": True, "cheap_to_preview": False},
    {"order": 8, "check_id": "leakage_near_duplicate",
     "covers": ["L4"], "owner": "MOS-EVID-037",
     "blocking": True, "cheap_to_preview": False},
    {"order": 9, "check_id": "acceptance_binding",
     "covers": ["MOS-EVID-080", "MOS-EVID-082"], "owner": "MOS-EVID-080",
     "blocking": True, "cheap_to_preview": True},
)

#: `MOS-UI-105` fixes the register of an `operator_disclosure`: no requirement id, no
#: check id, no field name, no digest, no UID. `MOS-UI-109` requires the copy to be a
#: PINNED STRING SELECTED BY IDENTIFIER and forbids generating it at render time, so
#: these are constants keyed on the reason and not f-strings built from the failure.
#: They state WHAT THE SKIP COSTS, which is the one thing a reader with no terminal
#: cannot work out for themselves.
_DISCLOSURE: Final[dict[str, str]] = {
    "leakage_pixel_identity": (
        "The platform could not read the images themselves for this collection, so it "
        "could not check whether the same pictures appear twice under different "
        "identifiers. If duplicates are present, the model will be tested on images it "
        "was trained on and its score will look better than its real performance."
    ),
    "leakage_near_duplicate": (
        "The platform could not read the images themselves for this collection, so it "
        "could not look for near-identical scans -- the same acquisition reconstructed "
        "twice, or a repeat scan taken minutes later. If any are present and land on "
        "both sides of the split, the score you get at the end will be higher than the "
        "model deserves and nothing later in the process will flag it."
    ),
    "seeded_annotation_ceilings": (
        "This check limits how much of the reference standard may have started as a "
        "machine suggestion that a person edited. It cannot run yet, because the "
        "reference standard is attached after the collection is sealed. It will be "
        "checked before the model is trained."
    ),
    "acceptance_binding": (
        "Nobody has yet recorded the bar this capability will be measured against. The "
        "collection can be sealed without it, but the evaluation at the end will have "
        "nothing to compare its result to, and recording the bar is not something this "
        "screen can do."
    ),
    "geometry_admissibility": (
        "This collection was not checked against the image geometry the deployed "
        "capability accepts, because no such setting is registered for it here. Scans "
        "the capability cannot process may be inside the collection."
    ),
    "corpus_composition_blocking": (
        "One of the composition checks could not be run on this collection. The usual "
        "reason is that nobody has yet declared the range of scans the model will be "
        "offered in clinical use, so the platform cannot tell whether your collection "
        "covers it. A model declared for a range it was barely trained on will be sent "
        "those scans in practice and nothing will flag it."
    ),
    "corpus_composition_warning": (
        "One of the two advisory composition checks could not be run on this "
        "collection. These describe how narrow the collection is rather than whether it "
        "can be used, so nothing is blocked -- but the reader of the eventual report "
        "will not be told how narrow it was."
    ),
}


class DeploymentNotDeclared(RuntimeError):
    """The deployment has not said something only it can say. Rendered as `503`.

    A platform fault under `MOS-UI-160` and never a client error: nothing the caller can
    change in the request body makes a deployment that never declared its
    de-identification provenance able to record one.
    """

    def __init__(self, variable: str, detail: str) -> None:
        self.variable = variable
        self.detail = detail
        super().__init__(f"{variable}: {detail}")


#: `MOS-EVID-021`'s three facts, declared by the deployment, on the argument
#: `medos/medos/api/routes_training.py` already makes for `MEDOS_TRAINING_ENVIRONMENT`.
DEID_PROVENANCE_VAR: Final[str] = "MEDOS_DEID_PROVENANCE"


@dataclass(frozen=True)
class DeidProvenance:
    """What `MOS-EVID-021` requires a sealed cohort to name about its UID space.

    WHY THIS IS A DEPLOYMENT DECLARATION AND NOT A REQUEST MEMBER, AND NOT A DEFAULT.

    `medos/schemas/evidence/seal-run-submit-request-1.0.0.json` refuses all three on the wire:
    "A client-supplied de-identification status is a client asserting that images are
    de-identified." It is right, and the alternative it names -- "the tenant's profile
    version is what the platform knows" -- IS NOT TRUE OF THIS PLATFORM. There is no
    `deid_policies` table (0005_gateway.up.sql creates `studies.deid_policy_id` NULLable
    "with its FK deferred to the migration that creates `deid_policies`", and no
    migration does) and no tenant UID mapping table anywhere in `medos/medos/db/migrations/`.
    The de-identification subsystem is unbuilt, which is the same fact that makes
    `medos/medos/gateway/app.py` refuse every `dataset_export` retrieval.

    So there are three possible behaviours and only one is honest:

      * DEFAULT IT to `pseudonymised` and invent the two ids. That writes a claim about
        a UID space nobody established into an immutable, undeletable row, and
        `MOS-EVID-021` exists precisely because "a DatasetVersion sealed under one UID
        mapping is not interchangeable with the same images under another".
      * REFUSE THE SEAL outright until the subsystem lands. That is defensible and it
        stops the surface working for a reason the operator cannot act on.
      * MAKE THE DEPLOYMENT SAY IT, once, where a reviewer can read it, and refuse `503`
        naming the variable when it has not. `MOS-TRAIN-124`'s environment declaration
        took exactly this shape for exactly this reason, and `routes_training.py`'s
        docstring states the honest gap it leaves: "a deployment-level declaration is an
        assertion by the operator, not an observation by the runner. What the
        declaration buys is that the assertion is made ONCE, by the deployment, in a
        place a reviewer can read -- rather than per-request by whichever client
        happened to POST."

    The third is taken. The same honest gap applies and is repeated here rather than
    left implicit: this is the site asserting what its export pipeline does, and the
    platform recording the assertion. When `deid_policies` and the UID mapping table
    land, this loader is replaced by a read of those rows and the variable goes away.
    """

    deidentification_status: str
    deid_policy_id: str | None
    uid_mapping_table_id: str | None

    _STATUSES: ClassVar[tuple[str, ...]] = (
        "identified",
        "pseudonymised",
        "public_deidentified",
    )


def load_deid_provenance(env: Mapping[str, str] | None = None) -> DeidProvenance:
    """Read the declaration. Raises `DeploymentNotDeclared`; never returns a default."""
    import json as _json
    import os as _os

    source = dict(env if env is not None else _os.environ)
    raw = source.get(DEID_PROVENANCE_VAR, "").strip()
    # A development default must not reach a deployment that has not declared itself
    # one. First statement after the read, before any interpretation: this makes the
    # loader strictly stricter and leaves its "never returns a default" promise intact.
    refuse_dev_value_outside_dev(DEID_PROVENANCE_VAR, raw, source)

    if not raw:
        raise DeploymentNotDeclared(
            DEID_PROVENANCE_VAR,
            "MOS-EVID-021 requires a pseudonymised or public_deidentified DatasetVersion "
            "to name its de-identification policy version and its tenant UID mapping "
            "table, and this platform has no table for either -- 0005_gateway.up.sql "
            "defers `deid_policies` to a migration that was never written. The seal will "
            "not invent them: a UID space nobody established, recorded in an immutable "
            'row, is what that requirement forbids. Set it to {"deidentification_status": '
            '"identified"|"pseudonymised"|"public_deidentified", "deid_policy_id": "...", '
            '"uid_mapping_table_id": "..."}',
        )
    if raw.startswith("@"):
        try:
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise DeploymentNotDeclared(
                DEID_PROVENANCE_VAR, f"names a file this process cannot read: {exc}"
            ) from exc
    try:
        document = _json.loads(raw)
    except ValueError as exc:
        raise DeploymentNotDeclared(DEID_PROVENANCE_VAR, f"is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DeploymentNotDeclared(DEID_PROVENANCE_VAR, "is not a JSON object")
    status = str(document.get("deidentification_status", ""))
    if status not in DeidProvenance._STATUSES:
        raise DeploymentNotDeclared(
            DEID_PROVENANCE_VAR,
            f"deidentification_status must be one of {list(DeidProvenance._STATUSES)}; "
            f"got {status!r}. MOS-EVID-012 and MOS-EVID-021 are normative for the set",
        )
    policy = document.get("deid_policy_id") or None
    mapping = document.get("uid_mapping_table_id") or None
    if status != "identified" and not (policy and mapping):
        # The same condition `medos/medos/evidence/repo.py` refuses at seal time and
        # `dataset_versions_deid_policy` refuses in the database. Checked HERE too so
        # the operator learns it at submit, in one request, rather than from a poll two
        # minutes later on a run that retrieved a whole cohort first.
        raise DeploymentNotDeclared(
            DEID_PROVENANCE_VAR,
            f"declares deidentification_status {status!r}, which MOS-EVID-021 requires "
            "to name BOTH a deid_policy_id and a uid_mapping_table_id; one or both are "
            "missing. A cohort whose UID space nobody can name cannot be compared with "
            "another sealed under a different one",
        )
    return DeidProvenance(
        deidentification_status=status,
        deid_policy_id=str(policy) if policy else None,
        uid_mapping_table_id=str(mapping) if mapping else None,
    )


def new_seal_run_id() -> str:
    """`slr_<ULID>`, on the pattern 0013 uses for `tr_`, `cs_` and `cv_`."""
    return new_ulid("slr")


def _row(r: Any) -> dict[str, Any]:
    if r is None:
        raise LookupError("expected a row, got none")
    return dict(r)


@dataclass(frozen=True)
class SealRunRow:
    """One `seal_runs` row. The projection of rows R26 and R27 is built from this."""

    id: str
    public_id: str
    harvest_batch_id: str
    dataset_id: str
    state: str
    phase: str
    patients_total: int
    series_total: int
    series_retrieved: int
    instances_total: int
    instances_retrieved: int
    consumer_class: str
    series_with_pixel_digest: int
    series_with_perceptual_hash: int
    check_battery: list[dict[str, Any]]
    refusals: list[dict[str, Any]]
    dataset_version_id: str | None
    dataset_split_id: str | None
    manifest_digest: str | None
    split_digest: str | None
    corpus_stratification_report_id: str | None
    reused: bool
    submitted_by: str
    submitted_at: Any
    started_at: Any
    finished_at: Any
    failure_reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


_SELECT: Final[str] = """
SELECT id, public_id, harvest_batch_id, dataset_id, state, phase, patients_total,
       series_total, series_retrieved, instances_total, instances_retrieved,
       consumer_class, series_with_pixel_digest, series_with_perceptual_hash,
       check_battery, refusals, dataset_version_id, dataset_split_id, manifest_digest,
       split_digest, corpus_stratification_report_id, reused, submitted_by,
       submitted_at, started_at, finished_at, failure_reason
  FROM seal_runs
"""


def _to_row(r: Mapping[str, Any]) -> SealRunRow:
    return SealRunRow(
        id=str(r["id"]),
        public_id=r["public_id"],
        harvest_batch_id=str(r["harvest_batch_id"]),
        dataset_id=str(r["dataset_id"]),
        state=r["state"],
        phase=r["phase"],
        patients_total=int(r["patients_total"]),
        series_total=int(r["series_total"]),
        series_retrieved=int(r["series_retrieved"]),
        instances_total=int(r["instances_total"]),
        instances_retrieved=int(r["instances_retrieved"]),
        consumer_class=r["consumer_class"],
        series_with_pixel_digest=int(r["series_with_pixel_digest"]),
        series_with_perceptual_hash=int(r["series_with_perceptual_hash"]),
        check_battery=list(r["check_battery"] or []),
        refusals=list(r["refusals"] or []),
        dataset_version_id=str(r["dataset_version_id"]) if r["dataset_version_id"] else None,
        dataset_split_id=str(r["dataset_split_id"]) if r["dataset_split_id"] else None,
        manifest_digest=r["manifest_digest"],
        split_digest=r["split_digest"],
        corpus_stratification_report_id=(
            str(r["corpus_stratification_report_id"])
            if r["corpus_stratification_report_id"]
            else None
        ),
        reused=bool(r["reused"]),
        submitted_by=str(r["submitted_by"]),
        submitted_at=r["submitted_at"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        failure_reason=r["failure_reason"],
    )


# =====================================================================================
# Submit and poll -- `MOS-TRAIN-122`'s first two methods
# =====================================================================================
def submit(
    conn: psycopg.Connection[Any],
    *,
    batch_id: str,
    dataset_id: str,
    submitted_by: str,
) -> SealRunRow:
    """Record the act. Writes one `seal_runs` row in `PENDING` and returns it.

    The cheap refusals happen HERE, inside the request, because `MOS-UI-160` separates a
    refusal the operator caused from a platform fault and a `202` for a batch that is
    already sealed is neither: it is a `409` the caller can act on immediately. What is
    NOT checked here is anything that needs the cohort -- that is the driver's, and the
    whole reason the operation is polled.
    """
    batch = _cur.get_batch(conn, batch_id)
    if batch.state == "SEALED":
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-209",
                    code="batch_already_sealed",
                    message=(
                        f"batch {batch_id} is already sealed against "
                        f"{batch.dataset_version_id}; sealing is idempotent on CONTENT, "
                        "not a second identity for the same batch"
                    ),
                    observed=batch.dataset_version_id,
                ),
            )
        )
    public_id = new_seal_run_id()
    with tenant_tx(conn):
        conn.execute(
            """
            INSERT INTO seal_runs
              (public_id, tenant_id, harvest_batch_id, dataset_id, submitted_by)
            VALUES (%s, current_tenant_id(), %s, %s, %s)
            """,
            (public_id, _uuid.UUID(batch_id), _uuid.UUID(dataset_id),
             _uuid.UUID(str(submitted_by))),
        )
    got = get_seal_run(conn, public_id)
    assert got is not None  # just inserted, under this tenant
    return got


def get_seal_run(conn: psycopg.Connection[Any], seal_run_id: str) -> SealRunRow | None:
    """`MOS-TRAIN-122`'s `Poll`, as one SELECT under the tenant GUC.

    Returns `None` rather than raising for an id this tenant cannot see: row-level
    security makes "no such row" and "another tenant's row" indistinguishable here by
    construction, which is what `MOS-SEC-072` is for, and the handler renders one `404`
    for both.
    """
    column = "public_id" if seal_run_id.startswith("slr_") else "id"
    param: Any = seal_run_id if column == "public_id" else _uuid.UUID(seal_run_id)
    with tenant_tx(conn) as tx:
        row = tx.execute(f"{_SELECT} WHERE {column} = %s", (param,)).fetchone()
    return _to_row(_row(row)) if row is not None else None


def _update(conn: psycopg.Connection[Any], seal_run_id: str, **columns: Any) -> None:
    """Write progress. One statement, one transaction, under the tenant GUC.

    Committed as it goes rather than at the end: the whole point of a polled resource is
    that a reader on another connection sees the phase change while the work is running
    (`MOS-UI-134`). A driver that wrote its progress inside the seal's own transaction
    would report nothing until it had already finished.
    """
    if not columns:
        return
    assignments = ", ".join(f"{name} = %s" for name in columns)
    with tenant_tx(conn):
        conn.execute(
            f"UPDATE seal_runs SET {assignments} "
            "WHERE tenant_id = current_tenant_id() AND id = %s",
            (*columns.values(), _uuid.UUID(seal_run_id)),
        )


# =====================================================================================
# The nine checks
# =====================================================================================
@dataclass
class _Check:
    """One battery entry under construction."""

    row: Mapping[str, Any]
    outcome: str = "pass"
    skipped_reason: str | None = None
    series_evaluated: int = 0
    series_skipped_structural: int = 0
    series_without_pixel_evidence: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        disclosure = (
            _DISCLOSURE.get(str(self.row["check_id"]))
            if self.outcome == "skipped"
            else None
        )
        return {
            "order": self.row["order"],
            "check_id": self.row["check_id"],
            "covers": list(self.row["covers"]),
            "owner": self.row["owner"],
            "blocking": bool(self.row["blocking"]),
            "cheap_to_preview": bool(self.row["cheap_to_preview"]),
            "outcome": self.outcome,
            "skipped_reason": self.skipped_reason if self.outcome == "skipped" else None,
            "operator_disclosure": disclosure,
            "series_evaluated": self.series_evaluated,
            "series_skipped_structural": self.series_skipped_structural,
            "series_without_pixel_evidence": self.series_without_pixel_evidence,
            "detail": dict(self.detail),
        }


def _by_id() -> dict[str, dict[str, Any]]:
    return {str(r["check_id"]): dict(r) for r in BATTERY_ROWS}


#: What the battery says about a run that has not run yet.
#:
#: `medos/schemas/evidence/seal-run-1.0.0.json` fixes `check_battery` at exactly nine entries
#: with no condition on `state`, because `MOS-API-112` is emphatic that "a missing check
#: is not a pass" and "an array that could be short is an array a client will read as
#: 'everything that applied'". That is right for a terminal run and leaves the `202` at
#: submit with nothing lawful to send: no check has run, and the closed four-value
#: `outcome` enum has no value meaning *not yet*.
#:
#: REPORTED: `outcome` wants a fifth value, or the schema wants `minItems: 9` made
#: conditional on a terminal `state`. Either is chapter 10's edit and neither is taken
#: here. Until then the projection renders `skipped` with a reason that says the battery
#: has not run, which is true and -- the part that matters -- cannot be misread as
#: `pass`: the one direction the whole design protects.
_NOT_YET_RUN_REASON: Final[str] = (
    "this seal run has not reached the check battery yet; the nine rows of MOS-UI-133 "
    "are reported here because the wire contract admits no short array, and none of "
    "them has been evaluated"
)
_NOT_YET_RUN_DISCLOSURE: Final[str] = (
    "The platform has not finished checking this collection yet. Nothing below has been "
    "decided, and nothing has been created. Keep this page open, or come back to it: "
    "the result appears here when the checks have run."
)


def pending_battery() -> list[dict[str, Any]]:
    """`MOS-UI-133`'s nine rows for a run that has not started. See `_NOT_YET_RUN_REASON`."""
    return [
        {
            **dict(row),
            "outcome": "skipped",
            "skipped_reason": _NOT_YET_RUN_REASON,
            "operator_disclosure": _NOT_YET_RUN_DISCLOSURE,
            "series_evaluated": 0,
            "series_skipped_structural": 0,
            "series_without_pixel_evidence": 0,
            "detail": {},
        }
        for row in BATTERY_ROWS
    ]


def _leakage_outcome(report: Any, ids: Sequence[str]) -> tuple[str, dict[str, Any]]:
    """Fold several L-checks into one `MOS-UI-133` row. `fail` dominates, then `skipped`.

    `fail` first because a row of the table that contained one failing check and one
    passing one is a blocking row, and `MOS-UI-145` makes the register of the display a
    function of this value. `skipped` before `pass` because `MOS-EVID-037`'s whole point
    is that the two are not the same answer.
    """
    outcomes = {c.id: c.outcome for c in report.checks if c.id in ids}
    detail = {
        c.id: c.as_dict() for c in report.checks if c.id in ids
    }
    if "fail" in outcomes.values():
        return "fail", detail
    if "waived" in outcomes.values():
        return "waived", detail
    if "skipped" in outcomes.values():
        return "skipped", detail
    return "pass", detail


def _battery(
    *,
    strat_report: Any,
    leak_report: Any,
    assignment: _split.Assignment,
    evidence: PixelEvidence,
    geometry: tuple[str, str, dict[str, Any]],
    acceptance: tuple[str, str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """`MOS-UI-133`'s nine rows, in order, all nine present, computed not asserted."""
    rows = _by_id()
    series_total = evidence.series_total
    no_pixels = evidence.series_without_pixel_digest
    no_hash = evidence.series_without_perceptual_hash
    pixel_reason = evidence.stated_reason()

    checks: list[_Check] = []

    # 1 -- geometry admissibility (MOS-TRAIN-111)
    g_outcome, g_reason, g_detail = geometry
    checks.append(
        _Check(rows["geometry_admissibility"], outcome=g_outcome,
               skipped_reason=g_reason or None, series_evaluated=series_total,
               detail=g_detail)
    )

    # 2 and 3 -- C1-C7, split the way MOS-TRAIN-092 splits them.
    #
    # AN `n/a` IN THE GROUP MAKES THE WHOLE ROW `skipped`, NOT `pass`, AND C5 IS WHY.
    # `medos/medos/evidence/stratification.py::_c5` returns `n/a` when no ApplicabilityEnvelope
    # was declared, and its own comment says "NOT `pass`. MOS-TRAIN-090 names C5 as the
    # one most easily rationalised away." A row that reported `pass` because C1, C2, C3
    # and C7 passed would rationalise it away in exactly the place `MOS-UI-143` says the
    # refusal has to be concrete -- the operator would read "composition: passed" and
    # never learn that the check which asks whether the declared range is represented did
    # not run. So the row skips, names which sub-check was not applicable, and the
    # `operator_disclosure` says what that costs.
    def _strat_row(check_id: str, ids: Sequence[str]) -> _Check:
        selected = [c for c in strat_report.checks if c.id in ids]
        outcomes = {c.outcome for c in selected}
        not_applicable = sorted(c.id for c in selected if c.outcome == "n/a")
        if "fail" in outcomes:
            outcome = "fail"
        elif "warn" in outcomes and not not_applicable:
            outcome = "warn"
        elif not_applicable:
            outcome = "skipped"
        elif "warn" in outcomes:
            outcome = "warn"
        else:
            outcome = "pass"
        reason = ""
        if outcome == "skipped":
            reasons = {
                c.id: str((c.detail or {}).get("reason", "not applicable"))
                for c in selected
                if c.outcome == "n/a"
            }
            reason = (
                f"MOS-TRAIN-088 reports n/a rather than pass for {not_applicable} on "
                f"this corpus, so this row did not discharge every check it covers: "
                + "; ".join(f"{k}: {v}" for k, v in reasons.items())
            )
        return _Check(
            rows[check_id],
            outcome=outcome,
            skipped_reason=reason or None,
            series_evaluated=series_total,
            detail={c.id: c.as_dict() for c in selected},
        )

    checks.append(_strat_row("corpus_composition_blocking", ("C1", "C2", "C3", "C5", "C7")))
    checks.append(_strat_row("corpus_composition_warning", ("C4", "C6")))

    # 4 -- the test partition floor (MOS-TRAIN-114). MOS-UI-142 does the arithmetic on
    # the screen, so the numbers it needs are in `detail` rather than in prose.
    counts = assignment.partition_patients
    test_patients = counts.get("test", 0)
    total_patients = sum(counts.values())
    meets = test_patients >= _split.TEST_PARTITION_FLOOR
    needed_total = -(-_split.TEST_PARTITION_FLOOR * 100 // 20)  # ceil(30 / 0.20)
    checks.append(
        _Check(
            rows["test_partition_floor"],
            outcome="pass" if meets else "fail",
            series_evaluated=series_total,
            detail={
                "test_patients": test_patients,
                "floor": _split.TEST_PARTITION_FLOOR,
                "total_patients": total_patients,
                "patients_needed_in_total": max(0, needed_total - total_patients),
                "partition_patients": counts,
            },
        )
    )

    # 5 -- seeded-annotation ceilings (MOS-TRAIN-086). SKIPPED, structurally: the
    # ceilings are computed per partition over `annotation_provenance`, and no
    # AnnotationSet exists at seal time -- row R29 freezes one AGAINST the sealed
    # DatasetVersion this run is about to create. Reported as `skipped` with the reason
    # rather than `pass`, because a ceiling nobody could evaluate is not a ceiling met.
    checks.append(
        _Check(
            rows["seeded_annotation_ceilings"],
            outcome="skipped",
            skipped_reason=(
                "MOS-TRAIN-086's ceilings are computed over the AnnotationSet's "
                "annotation_provenance per partition, and no AnnotationSet is bound to "
                "this DatasetVersion at seal time -- row R29 freezes one against the "
                "version this run creates. MOS-TRAIN-124 binds the set to the run, where "
                "the ceiling is evaluable"
            ),
            series_evaluated=0,
            detail={"evaluated_at": "training_run_submission", "ceilings": {
                "train": {"model_seeded_corrected": 0.50, "model_output_unreviewed": 0.0},
                "tune": {"model_seeded_corrected": 0.25, "model_output_unreviewed": 0.0},
                "test": {"model_seeded_corrected": 0.0, "model_output_unreviewed": 0.0},
            }},
        )
    )

    # 6 -- L1, L2, L5. No pixels needed: they compare key sets.
    outcome, detail = _leakage_outcome(leak_report, ("L1", "L2", "L5"))
    checks.append(
        _Check(rows["leakage_key_set_disjointness"], outcome=outcome,
               series_evaluated=series_total, detail=detail)
    )

    # 7 -- L3, pixel identity. THE SCHEMA WILL NOT LET THIS PASS WITHOUT PIXELS.
    #
    # `medos/medos/evidence/leakage.py::_l3` is told which series carry no pixel-derived
    # digest and reports `skipped` for them, so the FROZEN split and this battery agree.
    # The override below is belt and braces and is kept deliberately: this projection
    # must not be able to render `pass` while `series_without_pixel_evidence` is
    # non-zero whatever the engine says, because the schema forbids exactly that pairing
    # and a response that violated it would be rejected by a client rather than by us.
    l3_outcome, l3_detail = _leakage_outcome(leak_report, ("L3",))
    if no_pixels and l3_outcome == "pass":
        l3_outcome = "skipped"
    checks.append(
        _Check(
            rows["leakage_pixel_identity"],
            outcome=l3_outcome,
            skipped_reason=(
                (
                    f"{no_pixels} of {series_total} series carry no pixel-derived digest, "
                    f"so MOS-EVID-018's series_pixel_digest could not be computed and the "
                    f"comparison examined nothing for them: {pixel_reason}"
                )
                if l3_outcome == "skipped"
                else None
            ),
            series_evaluated=evidence.series_with_pixel_digest,
            series_without_pixel_evidence=no_pixels,
            detail=l3_detail,
        )
    )

    # 8 -- L4, near duplicates. `_l4` ALREADY reports `skipped` for a missing dhash64
    # (MOS-EVID-037), and separates the structural fewer-than-three-instances skip, which
    # may coexist with a pass over the eligible remainder (MOS-UI-144).
    l4_outcome, l4_detail = _leakage_outcome(leak_report, ("L4",))
    l4_raw = next((c for c in leak_report.checks if c.id == "L4"), None)
    structural = 0
    if l4_raw is not None and l4_raw.detail:
        structural = sum(
            1
            for s in (l4_raw.detail.get("skipped") or [])
            if "fewer than 3 instances" in str(s.get("skipped_reason", ""))
        )
    if no_hash and l4_outcome == "pass":
        l4_outcome = "skipped"
    checks.append(
        _Check(
            rows["leakage_near_duplicate"],
            outcome=l4_outcome,
            skipped_reason=(
                (
                    f"{no_hash} of {series_total} series carry no 64-bit perceptual hash, "
                    f"so MOS-EVID-034 L4 had nothing to compare for them and "
                    f"MOS-EVID-037 requires this to be reported as a skip rather than a "
                    f"pass: {pixel_reason or 'no dhash64 was supplied with the record'}"
                )
                if l4_outcome == "skipped"
                else None
            ),
            series_evaluated=evidence.series_with_perceptual_hash,
            series_skipped_structural=structural,
            series_without_pixel_evidence=no_hash,
            detail=l4_detail,
        )
    )

    # 9 -- the acceptance binding (MOS-EVID-080, MOS-EVID-082; MOS-UI-146).
    a_outcome, a_reason, a_detail = acceptance
    checks.append(
        _Check(rows["acceptance_binding"], outcome=a_outcome,
               skipped_reason=a_reason or None, detail=a_detail)
    )

    checks.sort(key=lambda c: int(c.row["order"]))
    return [c.as_dict() for c in checks]


def _blocking_failures(battery: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The entries that stop the seal. `MOS-EVID-034` and `MOS-TRAIN-088` block on `fail`.

    A `skipped` BLOCKING check does not block -- that is `MOS-EVID-037`'s ruling and it
    is exactly why the skip has to be visible instead. A `warn` never blocks
    (`MOS-UI-145`), and the schema refuses a `warn` on a blocking row in any case.
    """
    return [dict(c) for c in battery if c["blocking"] and c["outcome"] == "fail"]


def _refusals_from(battery: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """`SealRun.refusals`: one entry per blocking failure, in the engine's own shape."""
    out: list[dict[str, Any]] = []
    for check in _blocking_failures(battery):
        detail = dict(check.get("detail") or {})
        # A grouped row (C1-C7, L1/L2/L5) carries one sub-check per failing id, so the
        # refusal names the ACTUAL check that failed rather than the row that contains
        # it: register entry 83 records a console that rendered the wrong one.
        failing = [
            sub
            for sub in detail.values()
            if isinstance(sub, Mapping) and sub.get("outcome") == "fail"
        ]
        if failing:
            for sub in failing:
                out.append(
                    {
                        "code": f"SEAL_{str(check['check_id']).upper()}",
                        "check_id": str(sub.get("id") or check["check_id"]),
                        "message": str(
                            sub.get("statistic")
                            or f"{check['check_id']} reported fail"
                        ),
                        "observed": sub.get("observed"),
                        "bound": sub.get("bound"),
                        "detail": {k: v for k, v in sub.items()
                                   if k not in ("observed", "bound")},
                    }
                )
        else:
            out.append(
                {
                    "code": f"SEAL_{str(check['check_id']).upper()}",
                    "check_id": str(check["check_id"]),
                    "message": f"{check['check_id']} reported fail; see detail",
                    "observed": None,
                    "bound": None,
                    "detail": detail,
                }
            )
    return out


# =====================================================================================
# The driver -- `MOS-TRAIN-122`'s port, for one operation that is not a TrainingRun
# =====================================================================================
class SealDriver(Protocol):
    """One method. `MOS-TRAIN-122`'s `Submit`, for the seal.

    `Poll` is `get_seal_run` and needs no driver: the state lives in the row, so a poll
    is a SELECT and survives a restart of whatever ran the work. `Cancel` and `Logs` are
    deliberately absent -- there is no `CANCELLED` state on this entity (chapter 19 asks
    for a cancel control on a `TrainingRun`, which runs for days, and for none here), and
    a `Logs` method would need a log store this operation does not have. A port method
    no path can reach is a method no reader can trust.
    """

    def start(self, seal_run_id: str, tenant_id: str) -> None: ...


class ThreadSealDriver:
    """The shipped driver: one seal run, one daemon thread, one database connection.

    A THREAD AND NOT A SUBPROCESS, and not the request's own connection. The work is
    minutes of network I/O and digest arithmetic, so a thread is enough; the connection
    is its own because the request's is closed when the response is written, and writing
    progress onto a connection somebody else owns is how a poll starts returning a
    transaction that was rolled back.

    WHAT THIS DRIVER DOES NOT SURVIVE, STATED RATHER THAN HIDDEN. A process restart
    between `submit` and the terminal write leaves a row in `RETRIEVING`, `CHECKING` or
    `SEALING` for ever, and `GET /api/v1/seal-runs/{id}` reports that state truthfully
    because nothing may invent a terminal outcome for work whose result nobody observed.
    `MOS-UI-132` is not breached by it -- the constraint `seal_runs_atomic_outcome`
    means such a row names neither object, so nothing was created -- but the operator is
    left polling. A sweeper that ages a non-terminal run out to `FAILED` with a stated
    reason belongs with a lease and a heartbeat, which is `MOS-EXEC`'s machinery and not
    this module's; it is named in the change report as the honest remaining gap.
    """

    def __init__(
        self,
        connect: Callable[..., psycopg.Connection[Any]],
        *,
        store: ManifestStore,
        bucket: str,
        gateway_factory: Callable[[], Any] | None = None,
        deid: DeidProvenance | None = None,
    ) -> None:
        self._connect = connect
        self._store = store
        self._bucket = bucket
        self._gateway_factory = gateway_factory
        self._deid = deid
        self._threads: list[threading.Thread] = []

    def start(self, seal_run_id: str, tenant_id: str) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(seal_run_id, tenant_id),
            name=f"seal-{seal_run_id[:8]}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def join(self, timeout: float | None = None) -> None:
        """For tests and for a graceful shutdown. Never called on the request path."""
        for thread in self._threads:
            thread.join(timeout)

    def _run(self, seal_run_id: str, tenant_id: str) -> None:
        # `threading.Thread` does not copy ContextVars, so the tenant binding the
        # request established is NOT here -- `medos.db.tenancy.bind_current_tenant`
        # documents exactly this case. Binding explicitly is what makes every query in
        # the driver run under the GUC.
        token = bind_current_tenant(tenant_id)
        conn = self._connect()
        try:
            execute(
                conn,
                seal_run_id,
                store=self._store,
                bucket=self._bucket,
                gateway=self._gateway_factory() if self._gateway_factory else None,
                deid=self._deid,
            )
        except Exception as exc:  # noqa: BLE001 - the driver's last resort
            log.exception("seal_run_driver_failed", extra={"reason": type(exc).__name__})
            try:
                _fail(conn, seal_run_id, type(exc).__name__)
            except Exception:  # noqa: BLE001 - nothing left to do but not lose the thread
                log.exception("seal_run_failure_not_recorded")
        finally:
            conn.close()
            reset_current_tenant(token)


def _fail(conn: psycopg.Connection[Any], seal_run_id: str, reason: str) -> None:
    """`MOS-UI-160`: a platform fault, named as one. `MOS-API-042`: the TYPE, not the
    message -- a psycopg message carries the DSN host and the database name."""
    _update(
        conn,
        seal_run_id,
        state="FAILED",
        phase="finished",
        failure_reason=reason,
        finished_at=_now(conn),
    )


def _now(conn: psycopg.Connection[Any]) -> Any:
    with tenant_tx(conn) as tx:
        return tx.execute("SELECT now() AS t").fetchone()["t"]  # type: ignore[index]


# =====================================================================================
# The work
# =====================================================================================
def execute(
    conn: psycopg.Connection[Any],
    seal_run_id: str,
    *,
    store: ManifestStore,
    bucket: str,
    gateway: Any | None = None,
    spec: Any | None = None,
    deid: DeidProvenance | None = None,
) -> SealRunRow:
    """Run one seal to a terminal state. `MOS-TRAIN-208`, in `MOS-UI-131`'s order.

    Synchronous from this function's point of view and asynchronous from the caller's:
    the driver above runs it on a thread, and `tests/integration/test_api_curation.py`
    calls it directly so that the check battery is testable without a race.
    """
    run = get_seal_run(conn, seal_run_id)
    if run is None:  # pragma: no cover - the driver was handed an id it just wrote
        raise LookupError(f"no seal_runs row {seal_run_id} is visible to this tenant")
    if run.terminal:
        return run
    # Read HERE as well as at submit, so the driver cannot run against a declaration
    # that was correct when the request arrived and is not correct now.
    provenance = deid if deid is not None else load_deid_provenance()

    _update(conn, run.id, state="RETRIEVING", phase="freezing_batch",
            started_at=_now(conn))

    batch = _cur.get_batch(conn, run.harvest_batch_id)
    rows = _cur.candidates(conn, run.harvest_batch_id)
    unsettled = [c.id for c in rows if c.decision not in ("include", "exclude")]
    if unsettled:
        # MOS-TRAIN-081: the exclusion aggregate's denominator is the DRAWN set, so an
        # undecided candidate makes it a fraction of something nobody chose. Refused
        # before a single instance is retrieved.
        return _refuse(
            conn,
            run,
            battery=None,
            refusals=[
                {
                    "code": "BATCH_NOT_SETTLED",
                    "check_id": "MOS-TRAIN-081",
                    "message": (
                        f"{len(unsettled)} of {len(rows)} candidates have no settled "
                        "decision; decide them, or remove them from the batch before "
                        "any decision exists"
                    ),
                    "observed": len(unsettled),
                    "bound": 0,
                    "detail": {"candidate_ids": unsettled[:20]},
                }
            ],
        )

    included = [c for c in rows if c.decision == "include"]
    if not included:
        return _refuse(
            conn,
            run,
            battery=None,
            refusals=[
                {
                    "code": "EMPTY_COHORT",
                    "check_id": "MOS-EVID-016",
                    "message": (
                        "the batch has no included candidates; a DatasetVersion is an "
                        "explicit, materialised list of instances"
                    ),
                    "observed": 0,
                    "bound": 1,
                    "detail": {},
                }
            ],
        )

    patients = sorted({c.patient_key for c in included})
    _update(
        conn,
        run.id,
        phase="retrieving_pixels",
        patients_total=len(patients),
        series_total=sum(len(c.series_instance_uids) for c in included),
        instances_total=sum(len(c.instance_uids) for c in included),
    )

    # ---- step 3: retrieve through `dataset_export` ----------------------------------
    with tempfile.TemporaryDirectory(prefix="medos-seal-") as work_dir:
        # The Gateway is resolved from the PIPELINE's own credential when the caller
        # did not inject one. `gateway_from_env` refuses to borrow the worker's token;
        # see `medos/medos/training/retrieval.py` for why that one line is what keeps
        # MOS-TRAIN-068 true at runtime.
        reason = ""
        if gateway is None:
            gateway, reason = _retrieval.gateway_from_env()
        retriever = DatasetExportRetriever(
            gateway=gateway, work_dir=Path(work_dir), unavailable_reason=reason
        )
        records_by_candidate: dict[str, list[SeriesRecord]] = {}
        records: list[SeriesRecord] = []
        for candidate in included:
            got = retriever(candidate)
            records_by_candidate[candidate.id] = got
            records.extend(got)
            _update(
                conn,
                run.id,
                series_retrieved=retriever.evidence.series_with_pixel_digest,
                instances_retrieved=retriever.evidence.instances_retrieved,
                series_with_pixel_digest=retriever.evidence.series_with_pixel_digest,
                series_with_perceptual_hash=(
                    retriever.evidence.series_with_perceptual_hash
                ),
            )
        evidence = retriever.evidence

        # ---- the battery, BEFORE anything is created (MOS-UI-131) --------------------
        _update(conn, run.id, state="CHECKING", phase="computing_profile")
        profile = acquisition_profile(records)
        strat = _strat.stratification_report(profile, evidence_kind="vendor_evidence")

        _update(conn, run.id, phase="running_checks")
        strata = {
            c.patient_key: _split.stratum_of(c.acquisition_profile or {})
            for c in included
        }
        assignment = _split.assign(
            patients,
            strata,
            _split.seed_label_for(
                capability_id=batch.capability_id, harvest_batch_id=run.harvest_batch_id
            ),
        )
        leak = _leak.leakage_report(records, assignment.pairs)
        battery = _battery(
            strat_report=strat,
            leak_report=leak,
            assignment=assignment,
            evidence=evidence,
            geometry=_geometry_check(spec, included),
            acceptance=_acceptance_check(conn, batch.capability_id),
        )

        blocking = _blocking_failures(battery)
        if blocking:
            # MOS-TRAIN-088: the FULL C1-C7 result is recorded whether it passes or
            # blocks -- "a fail that blocked without leaving a record would make the most
            # informative outcome the only one with no evidence".
            report_id = _cur.record_stratification_report(
                conn, batch_id=run.harvest_batch_id, report=strat, dataset_version_id=None
            )
            return _refuse(
                conn,
                run,
                battery=battery,
                refusals=_refusals_from(battery),
                corpus_stratification_report_id=report_id,
            )

        # ---- both writes, or neither (MOS-UI-130, MOS-UI-132) -----------------------
        _update(conn, run.id, state="SEALING", phase="sealing_version",
                check_battery=_json(battery))
        try:
            sealed = _cur.seal_from_batch(
                conn,
                batch_id=run.harvest_batch_id,
                dataset_id=run.dataset_id,
                retrieve=lambda c: records_by_candidate[c.id],
                store=store,
                bucket=bucket,
                sealed_by=run.submitted_by,
                # MOS-EVID-021: the status and its two ids come from the DEPLOYMENT's
                # declaration and never from the request body -- see `DeidProvenance`
                # for why a default here would be a claim about a UID space nobody
                # established, written into an immutable row.
                deidentification_status=provenance.deidentification_status,
                deid_policy_id=provenance.deid_policy_id,
                uid_mapping_table_id=provenance.uid_mapping_table_id,
                spec=spec,
                evidence_kind="vendor_evidence",
            )
        except (SealRefused, CurationRefused) as exc:
            return _refuse(
                conn, run, battery=battery,
                refusals=[_refusal_dict(r) for r in exc.refusals],
            )

        # The two ids are NOT written here. `seal_runs_orphan_version_is_a_fault`
        # admits a version without a split only in `SEALING` and `FAILED`, and the
        # honest reading of MOS-UI-132 is that the pair is what the operator was
        # promised -- so the pointer to the sealed version appears either beside its
        # split (SUCCEEDED) or beside the fault that stranded it (FAILED), and never as
        # a half-outcome a poll could catch and render as done.
        _update(conn, run.id, phase="freezing_split",
                reused=sealed.reused,
                corpus_stratification_report_id=_uuid.UUID(
                    sealed.stratification_report_id
                ))
        try:
            split = _evrepo.freeze_split(
                conn,
                dataset_version_id=sealed.dataset_version_id,
                # MOS-UI-130 forbids the console exposing that the action spans two
                # operations, so the split has no name the operator chose: it is named
                # after the batch it came from.
                name=f"seal/{run.public_id}",
                assignments=list(assignment.pairs),
                store=store,
                bucket=bucket,
                frozen_by=run.submitted_by,
                assignment_method=assignment.method,
                stratified_by=_split.STRATIFIED_BY,
                strata=assignment.strata,
                records=records,
                # The frozen split MUST NOT record `L3: pass` over series nobody
                # hashed. The SealRun says `skipped` and MOS-EVID-035 stores this
                # report verbatim in an immutable row, so the two would otherwise
                # disagree about the same cohort for ever.
                series_without_pixel_evidence=[
                    o.series_instance_uid for o in retriever.outcomes
                    if not o.held_pixels
                ],
            )
        except FreezeRefused as exc:
            # MOS-UI-132: a sealed version with no split is the partial outcome that
            # requirement forbids leaving the operator in, and MOS-EVID-013 makes it
            # undeletable. It cannot be undone, so it is reported as a PLATFORM FAULT
            # (MOS-UI-160) naming the version that exists -- not as something the
            # operator did, because the battery above passed L1-L5 over these exact
            # records and a disagreement between the two is a defect in this platform.
            _update(
                conn, run.id,
                state="FAILED", phase="finished",
                # NAMED, not hidden: the object exists and the operator cannot delete
                # it, so the one record of the act says which one it is.
                dataset_version_id=_uuid.UUID(sealed.dataset_version_id),
                manifest_digest=sealed.manifest_digest,
                failure_reason="FreezeRefused",
                refusals=_json([_refusal_dict(r) for r in exc.refusals]),
                finished_at=_now(conn),
            )
            got = get_seal_run(conn, run.id)
            assert got is not None
            return got

    _update(
        conn,
        run.id,
        state="SUCCEEDED",
        phase="finished",
        dataset_version_id=_uuid.UUID(sealed.dataset_version_id),
        manifest_digest=sealed.manifest_digest,
        dataset_split_id=_uuid.UUID(split.id),
        split_digest=split.split_digest,
        check_battery=_json(battery),
        finished_at=_now(conn),
    )
    got = get_seal_run(conn, run.id)
    assert got is not None
    return got


def _refuse(
    conn: psycopg.Connection[Any],
    run: SealRunRow,
    *,
    battery: list[dict[str, Any]] | None,
    refusals: list[dict[str, Any]],
    corpus_stratification_report_id: str | None = None,
) -> SealRunRow:
    """REFUSED, with the battery as far as it got and the refusals that stopped it.

    `seal_runs_battery_complete` requires nine entries on a terminal run, so a refusal
    that happened BEFORE the battery ran carries nine `skipped` entries rather than an
    empty array -- `MOS-API-112`'s "a missing check is not a pass", applied to the case
    where the checks genuinely did not run.
    """
    if battery is None:
        battery = [
            {
                **row,
                "outcome": "skipped",
                "skipped_reason": (
                    "the seal was refused before the check battery ran; see refusals"
                ),
                "operator_disclosure": _DISCLOSURE.get(
                    str(row["check_id"]),
                    "This check did not run, because the collection was turned back "
                    "before the platform got to it. Fix the problem described above and "
                    "seal again.",
                ),
                "series_evaluated": 0,
                "series_skipped_structural": 0,
                "series_without_pixel_evidence": 0,
                "detail": {},
            }
            for row in (dict(r) for r in BATTERY_ROWS)
        ]
    columns: dict[str, Any] = {
        "state": "REFUSED",
        "phase": "finished",
        "check_battery": _json(battery),
        "refusals": _json(refusals),
        "finished_at": _now(conn),
    }
    if corpus_stratification_report_id:
        columns["corpus_stratification_report_id"] = _uuid.UUID(
            corpus_stratification_report_id
        )
    _update(conn, run.id, **columns)
    got = get_seal_run(conn, run.id)
    assert got is not None
    return got


def _refusal_dict(refusal: Any) -> dict[str, Any]:
    """One engine `Refusal` in the `SealRun.refusals` shape."""
    return {
        "code": refusal.code,
        "check_id": refusal.check_id,
        "message": refusal.message,
        "observed": refusal.observed,
        "bound": refusal.bound,
        "detail": dict(getattr(refusal, "detail", None) or {}),
    }


def _json(value: Any) -> Any:
    """A `jsonb` parameter, with UUIDs, datetimes and Decimals made JSON-safe.

    `medos.api.problems.json_safe` does the same job and is NOT imported here: it lives
    in `medos/medos/api/`, and importing it would put the HTTP layer inside `medos.training`'s
    import closure -- the line `tests/gate/test_no_auto_promote.py` check 1 draws and
    `medos/medos/api/routes_training.py`'s own docstring explains. Twelve lines of coercion is
    the cheaper side of that trade.
    """
    return Jsonb(_plain(value))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


# =====================================================================================
# The two checks that are not a call into an existing engine function
# =====================================================================================
def _geometry_check(
    spec: Any | None, included: Sequence[Any]
) -> tuple[str, str, dict[str, Any]]:
    """Row 1. `MOS-TRAIN-111`'s geometry gate under the SERVING `PreprocessingSpec`.

    `spec is None` is `skipped` and never `pass`: `MOS-IMG-045` makes the spec the
    registered artifact a run is fitted against, and a cohort nobody checked against one
    may contain series the deployed capability cannot process. `MOS-UI-137` forbids the
    console resolving a geometry refusal by EDITING the spec, which is why the spec
    arrives from the capability's registered version and is never a request member.
    """
    if spec is None:
        return (
            "skipped",
            "no PreprocessingSpec is registered for this capability in this deployment, "
            "so MOS-TRAIN-111's geometry gate had nothing to evaluate the cohort "
            "against; MOS-IMG-045 makes the spec the registered artifact a run is fitted "
            "to and MOS-UI-137 forbids this surface supplying one",
            {"spec": None},
        )
    offending: list[dict[str, Any]] = []
    for candidate in included:
        try:
            _cur.assert_geometry_admissible(spec, candidate)
        except CurationRefused as exc:
            offending.extend(_refusal_dict(r) for r in exc.refusals)
    return (
        "fail" if offending else "pass",
        "",
        {"refused": offending[:50], "n_refused": len(offending)},
    )


def _acceptance_check(
    conn: psycopg.Connection[Any], capability_id: str
) -> tuple[str, str, dict[str, Any]]:
    """Row 9. `MOS-EVID-080` and `MOS-EVID-082`, as `MOS-UI-146` asks for them.

    An ABSENT binding is `skipped`, not `fail`. `MOS-UI-146` says the operator "needs to
    know before they start" and names the remedy as one taken by somebody holding
    `capability.update` -- so blocking the seal would strand them behind a control this
    surface does not have, and passing silently would waste the evaluation run.
    A binding that names a `training` or `tuning` cohort IS a `fail`: `MOS-EVID-082`
    forbids it and the remedy is to re-bind, not to seal.
    """
    with tenant_tx(conn) as tx:
        tenant = str(tx.execute("SELECT current_tenant_id() AS t").fetchone()["t"])  # type: ignore[index]
    binding = _acceptance.load_binding(
        conn, tenant_id=tenant, capability_id=capability_id
    )
    if binding is None:
        return (
            "skipped",
            "no tenant_acceptance_bindings row exists for this capability, so "
            "MOS-EVID-080's criteria are not bound to a concrete cohort and the "
            "evaluation this corpus exists to feed has no bar to be measured against",
            {"capability_id": capability_id, "binding": None},
        )
    with tenant_tx(conn) as tx:
        purpose = tx.execute(
            "SELECT d.purpose FROM dataset_versions v JOIN datasets d ON d.id = v.dataset_id"
            " WHERE v.id = %s",
            (binding.dataset_version_id,),
        ).fetchone()
    named = str(purpose["purpose"]) if purpose else None  # type: ignore[index]
    if named in ("training", "tuning"):
        return (
            "fail",
            "",
            {
                "capability_id": capability_id,
                "acceptance_dataset_purpose": named,
                "requirement": "MOS-EVID-082",
                "message": (
                    "the acceptance binding names a dataset whose purpose is "
                    f"{named!r}; MOS-EVID-082 forbids evaluating against a cohort the "
                    "model was fitted on"
                ),
            },
        )
    return ("pass", "", {"capability_id": capability_id,
                         "acceptance_dataset_purpose": named})
