# SPDX-License-Identifier: Apache-2.0
"""The retry path. `MOS-REG-067`, `MOS-REG-068`, `MOS-EXEC-008`, `MOS-EXEC-012`.

    MOS-REG-067 -- "On **every** retry and every requeue, the dispatcher MUST use
    `job.resolution.selected` verbatim and MUST NOT call `Resolve` again. Re-resolution on
    retry MUST be impossible BY CONSTRUCTION: the retry path MUST read the pin and MUST
    NOT have the snapshot in scope."

WHY THIS IS A SEPARATE MODULE AND NOT A FUNCTION NEXT TO `resolve()`
---------------------------------------------------------------------
"By construction" is a statement about what is IN SCOPE, and in Python scope is a module.
This module imports `psycopg` and `medos.db.tenancy` and nothing else from this package:
there is no `Snapshot` here, no `resolve`, and no import that could reach one. A future
edit that wanted to "just re-resolve if the pin looks stale" would have to add the import
first, and `tests/integration/test_resolution.py::test_the_retry_path_has_no_snapshot_in_scope`
fails on that import -- which is the static check chapter 6's acceptance criterion 9 asks
for, written so that it fails in CI rather than in a hospital.

WHAT A RETRY IS ALLOWED TO DO WITH A WITHDRAWN VERSION
-------------------------------------------------------
`MOS-REG-068`: if the pinned version became `SUSPENDED` or `RECALLED` between attempts the
retry MUST NOT run -- the job goes to `REJECTED` (a clinical outcome, not a transport
failure) with `pinned_version_suspended` / `pinned_version_recalled`, and re-analysis is a
NEW job carrying `supersedes_job_id`. That check is a lifecycle lookup on the pinned ids
(`pinned_version_status`), NOT a re-resolution: it can only ever stop the job, never
change which version it runs. `MOS-REG-057` is the other half -- a job already RUNNING
against a version that is suspended mid-flight is allowed to finish.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import psycopg

from medos.db.tenancy import tenant_tx

__all__ = [
    "PinMissing",
    "PinnedSelection",
    "pinned_resolution",
    "pinned_selection",
    "pinned_version_status",
    "retry_refusal",
]

# Read back by the same two constants the writer uses; duplicated as literals here on
# purpose -- importing them from `pin` would pull `medos.resolution.model` and with it the
# `Snapshot` type into this module's namespace, which is the one thing it must not have.
_PIN_EVENT_TYPE = "job.requested"
_PIN_PAYLOAD_KEY = "resolution"

# MOS-REG-068's two reason codes, and the statuses that produce them.
_WITHDRAWN = {
    "SUSPENDED": "pinned_version_suspended",
    "RECALLED": "pinned_version_recalled",
}


class PinMissing(LookupError):
    """No resolution record for this job.

    `MOS-REG-004`: "A release MUST NOT ship a code path that dispatches a job without a
    `resolution` object." So this is raised rather than defaulted: a job with no pin is
    not dispatchable, and guessing one would be the re-resolution `MOS-REG-067` forbids.
    """


@dataclass(frozen=True, slots=True)
class PinnedSelection:
    """`job.resolution.selected`, verbatim. What every attempt after the first runs."""

    service_version_id: str
    service_family: str
    version: str
    image_digest: str
    model_version_ids: tuple[str, ...]
    preprocessing_spec_ids: tuple[str, ...]
    deployment_id: str
    deployment_environment: str
    deployment_state: str
    deployment_role: str
    clinical_use_mode: str
    canary_bucket: int
    epoch: int
    snapshot_id: str
    resolved_at: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> PinnedSelection:
        selected = record.get("selected") or {}
        if not selected:
            raise PinMissing(
                "the pinned resolution record has no `selected`; the job was REJECTED "
                "with ZERO_CANDIDATES and MUST NOT be dispatched (MOS-REG-069)"
            )
        return cls(
            service_version_id=str(selected["service_version_id"]),
            service_family=str(selected.get("service_family", "")),
            version=str(selected["version"]),
            image_digest=str(selected.get("image_digest", "")),
            model_version_ids=tuple(selected.get("model_version_ids") or ()),
            preprocessing_spec_ids=tuple(selected.get("preprocessing_spec_ids") or ()),
            deployment_id=str(selected.get("deployment_id", "")),
            deployment_environment=str(selected.get("deployment_environment", "")),
            deployment_state=str(selected.get("deployment_state", "")),
            deployment_role=str(selected.get("deployment_role", "")),
            clinical_use_mode=str(selected.get("clinical_use_mode", "")),
            canary_bucket=int(selected.get("canary_bucket", 0)),
            epoch=int(record.get("epoch", 0)),
            snapshot_id=str(record.get("snapshot_id", "")),
            resolved_at=str(record.get("resolved_at", "")),
        )

    @property
    def pinned_artifact_ids(self) -> tuple[str, ...]:
        """Everything whose withdrawal stops the retry (`MOS-REG-068`)."""
        return (
            self.service_version_id,
            *self.model_version_ids,
            *self.preprocessing_spec_ids,
        )


def pinned_resolution(
    conn: psycopg.Connection[Any], job_public_id: str
) -> dict[str, Any]:
    """The whole §6.7.5 record for this job, as written at creation."""
    with tenant_tx(conn):
        row = conn.execute(
            """
            SELECT e.payload -> %s AS record
              FROM job_events e
              JOIN jobs j ON j.id = e.job_id
             WHERE j.public_id = %s
               AND e.event_type = %s
               AND e.payload ? %s
             ORDER BY e.seq
             LIMIT 1
            """,
            (_PIN_PAYLOAD_KEY, job_public_id, _PIN_EVENT_TYPE, _PIN_PAYLOAD_KEY),
        ).fetchone()
    if row is None:
        raise PinMissing(f"job {job_public_id} carries no resolution record")
    record = row["record"] if isinstance(row, Mapping) else row[0]
    if isinstance(record, str):  # a connection without a jsonb loader
        record = json.loads(record)
    return dict(record)


def pinned_selection(
    conn: psycopg.Connection[Any], job_public_id: str
) -> PinnedSelection:
    """THE retry path. Reads the pin; cannot re-resolve, because there is no snapshot."""
    return PinnedSelection.from_record(pinned_resolution(conn, job_public_id))


def pinned_version_status(
    conn: psycopg.Connection[Any], selection: PinnedSelection
) -> dict[str, str]:
    """`{artifact public_id: lifecycle_status}` for the pinned closure.

    A lifecycle lookup by id, and nothing else: it cannot return a different version, so
    it cannot become re-resolution by accident.
    """
    ids = list(selection.pinned_artifact_ids)
    if not ids:  # pragma: no cover - a selection always names a service version
        return {}
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT public_id, lifecycle_status FROM artifacts WHERE public_id = ANY(%s)",
            (ids,),
        ).fetchall()
    out: dict[str, str] = {}
    for row in rows:
        if isinstance(row, Mapping):
            out[str(row["public_id"])] = str(row["lifecycle_status"])
        else:  # pragma: no cover - tuple row factory
            out[str(row[0])] = str(row[1])
    return out


def retry_refusal(statuses: Mapping[str, str]) -> tuple[str, str] | None:
    """`MOS-REG-068`: `(reason_code, detail)` when this retry MUST NOT run, else `None`.

    RECALLED outranks SUSPENDED when both appear: a recall is irreversible
    (`MOS-REG-022`) and is the more serious sentence to put in front of a reader.
    """
    for status in ("RECALLED", "SUSPENDED"):
        named = sorted(k for k, v in statuses.items() if v == status)
        if named:
            return _WITHDRAWN[status], f"{', '.join(named)} is {status}"
    return None
