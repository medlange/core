# SPDX-License-Identifier: Apache-2.0
"""The resolution PIN: §6.7.5's record, written in the job-creation transaction.

    MOS-REG-066  the resolved set is written into the Job "in the same transaction that
                 creates the job, before any enqueue"
    MOS-REG-115  `selected` carries `deployment_environment` and `deployment_state`
    MOS-REG-004  every Job row carries a non-null `resolution` object, from 0.1.0
    MOS-EXEC-008 the outcome is "reused verbatim on every retry ... A retry MUST NOT
                 re-run resolution"

WHY THE PIN IS THE POINT OF THE WHOLE COMPONENT
------------------------------------------------
§6.7.7 works it through: without the pin, attempt 2 of `job_9f21` resolves against a newer
epoch, the same job id reports 705 mL instead of 642 mL, the provenance record's two
halves name different artifacts, and PACS accumulates a second overlapping SEG series
because the derived `SeriesInstanceUID` is a function of the model version. Nothing raises
an exception in any of the three. A pure resolver alone does NOT prevent this -- purity
makes the answer stable for a fixed snapshot, and the snapshot is exactly what moved.

WHERE THE RECORD IS STORED IN THIS BUILD, AND THE DEFECT THAT FORCED IT -- READ THIS
-------------------------------------------------------------------------------------
Chapter 12 §12.10 defines the columns this record belongs in:
`jobs.target_kind`, `jobs.requested_capability_id`, `jobs.requested_version_range`,
`jobs.resolved_service_version_id`, `jobs.resolution_snapshot jsonb NOT NULL DEFAULT
'{}'`, `jobs.deployment_id`, and the `jobs_resolution_pinned` BEFORE UPDATE trigger that
makes them immutable after creation (`MOS-STORE-267`).

**None of those columns exists in this deployment.** `medos/medos/db/schema.sql` (migration
`0001_baseline`) created `jobs` without them and no later migration added them, so
`MOS-REG-004` -- binding since 0.1.0 -- has never been satisfied, and the 0.2.0 build's
only resolution record is `result_provenance.resolution`, written after execution rather
than at creation. That is a defect in the execution-plane schema, it is REPORTED and not
silently patched, and it needs a migration this component was explicitly forbidden to
create (0012 is the registry's, 0013 training's, 0014 the outbox's).

So the record is written where this build already has an immutable, same-transaction,
per-job home for it: a `job_events` row of type `job.requested` whose payload carries
`resolution`. That is not a free choice of convenience --

  * `job_events` is append-only at the DATABASE level: `job_events_no_update` and
    `job_events_no_delete` (schema.sql, `MOS-EXEC-013`) raise on UPDATE and on DELETE, so
    the pinned record is at least as immutable as `jobs_resolution_pinned` would make the
    column, and immutable against `psql` as well as against application code;
  * `job_append_event` allocates `seq` under the job's own row lock, so the write is in
    the creating transaction with the job row and the queue row (`MOS-EXEC-034`); and
  * the retry path reads it back through `medos/medos/resolution/retry.py`, which has no
    `Snapshot` in scope and therefore cannot re-resolve (`MOS-REG-067`).

What this arrangement does NOT give, and what the migration must restore:
`jobs.resolved_service_version_id` as a queryable, foreign-keyed column; the `CHECK (state
IN ('CREATED','REJECTED') OR resolved_service_version_id IS NOT NULL)` that makes an
unpinned RUNNING job unrepresentable; and `MOS-REG-040`'s recall-impact query as an index
scan rather than a jsonb walk. `medos.resolution.retry.pinned_selection` is the seam: when
the column lands, that one function reads it and nothing else in the platform changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import tenant_tx
from medos.resolution.model import GateConfig, Outcome, iso8601

__all__ = [
    "PIN_EVENT_TYPE",
    "PIN_PAYLOAD_KEY",
    "resolution_record",
    "write_pin",
]

# The event type and payload key `medos.resolution.retry` reads back. One spelling, here.
PIN_EVENT_TYPE = "job.requested"
PIN_PAYLOAD_KEY = "resolution"


def resolution_record(
    outcome: Outcome,
    *,
    capability_id: str,
    resolved_at: datetime,
    gate_config: GateConfig | None = None,
) -> dict[str, Any]:
    """§6.7.5's document, member for member. PURE -- no clock, no I/O.

    `resolved_at` is passed in rather than read from a clock, and the caller passes
    `snapshot.as_of`: the record must describe the snapshot the decision was made
    against, and a second, later `now()` would put a timestamp on the record that no
    replay of `(epoch, snapshot_id)` can reproduce (`MOS-REG-071`).

    `alternatives` is the rest of `Ranked` -- "the ordered candidate list and the reason
    the winner won" (`MOS-EXEC-008`) -- and `excluded` is `MOS-REG-070`'s per-candidate
    refusal, which is what makes a `ZERO_CANDIDATES` rejection legible to a clinician
    instead of "no service was available".
    """
    gates = gate_config or GateConfig()
    record: dict[str, Any] = {
        "resolver_version": outcome.resolver_version,
        "snapshot_id": outcome.snapshot_id,
        "epoch": outcome.epoch,
        "resolved_at": iso8601(resolved_at),
        "capability_id": capability_id,
        "inputs_hash": outcome.inputs_hash,
        "gate_config": gates.as_document(),
        "decision": outcome.decision,
        "selected": None,
        "alternatives": [],
        "excluded": [e.as_document() for e in outcome.excluded],
    }
    if outcome.reason_code:
        record["reason_code"] = outcome.reason_code
    if outcome.selected is not None:
        s = outcome.selected
        record["selected"] = {
            "service_version_id": s.service_version_id,
            "service_family": s.service_family,
            "version": s.version,
            "image_digest": s.image_digest,
            "model_version_ids": list(s.model_version_ids),
            "preprocessing_spec_ids": list(s.preprocessing_spec_ids),
            "deployment_id": s.deployment_id,
            # MOS-REG-115: environment and state travel with the pin. `environment` is
            # immutable for the life of a Deployment row, so the pinned value IS the
            # environment the job executed in; `state` and `role` are the values AT
            # RESOLUTION, and chapter 9's `*_at_execution` pair is read separately at the
            # start of execution. A divergence between the two pairs is the record that
            # the deployment changed, and it MUST NOT be reconciled by overwriting either.
            "deployment_environment": s.deployment_environment,
            "deployment_state": s.deployment_state,
            "deployment_role": s.deployment_role,
            "clinical_use_mode": s.clinical_use_mode,
            "canary_bucket": s.rank_key.canary_bucket,
            "content_digest": s.content_digest,
            "rank_key": s.rank_key.as_string(),
        }
        record["alternatives"] = [
            {
                "service_version_id": c.service_version_id,
                "version": c.version,
                "rank_key": c.rank_key.as_string(),
            }
            for c in outcome.ranked[1:]
        ]
    return record


def write_pin(
    conn: psycopg.Connection[Any],
    *,
    job_public_id: str,
    record: Mapping[str, Any],
) -> int:
    """Write the pin for `job_public_id`. Returns the event `seq`.

    MUST be called inside the transaction that creates the job (`MOS-REG-066`: "in the
    same transaction that creates the job, before any enqueue"). `tenant_tx` joins the
    caller's open transaction as a SAVEPOINT rather than opening one of its own, so
    calling this straight after `medos.db.repo.create_job_queued` inside one `with`
    block does the right thing.
    """
    with tenant_tx(conn):
        row = conn.execute(
            """
            SELECT job_append_event(j.id, %s, 'control-plane', %s) AS seq
              FROM jobs j
             WHERE j.public_id = %s
            """,
            (PIN_EVENT_TYPE, Jsonb({PIN_PAYLOAD_KEY: dict(record)}), job_public_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"no job {job_public_id}; the pin has nothing to attach to")
    return int(row["seq"] if isinstance(row, Mapping) else row[0])
