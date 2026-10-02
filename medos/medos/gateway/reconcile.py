# SPDX-License-Identifier: Apache-2.0
"""The projection reconciler: rebuild `studies` from what a PACS already holds.

MOS-STORE-234 calls the projection "a queryable, joinable, indexable copy of DICOM
metadata keyed by UID, **fully rebuildable**"; MOS-STORE-245 is the rebuild. This module is
the minimum of that rebuild the Gateway needs: the study level, because MOS-DATA-010 puts
tenancy on the study and the Gateway reads nothing below it.

WHY A GATEWAY IN FRONT OF AN EXISTING PACS NEEDS THIS AT ALL
-------------------------------------------------------------
MOS-DATA-012 creates a `studies` row when an instance arrives through STOW-RS. Every study
that was already in the PACS before the Gateway existed has no row, and MOS-DATA-013 then
makes it invisible -- correctly, because "no row" means "nobody has claimed this data".
Someone has to claim it, and that someone is a human operator naming a tenant.

WHY `--tenant` IS REQUIRED AND HAS NO DEFAULT
----------------------------------------------
MOS-DATA-047: "An instance whose tenant cannot be determined MUST be written to
`quarantine` with reason `tenant_unresolved` ... Assigning unattributed data to a default
tenant is forbidden." A reconciler with a default tenant IS that assignment, dressed as
configuration. So the CLI refuses to run without `--tenant`, and there is no environment
fallback: an operator typing a tenant id is an attribution, and `MEDOS_TENANT_ID` quietly
defaulting is not.

WHAT IT DOES NOT DO
-------------------
No `series` or `instances` rows (nothing reads them yet -- see 0005_gateway's header), no
patient demographics (`patients` gets the natural key and nothing else; CONTRACT.md
section 11), and no triage. It never deletes: a study that has disappeared from the PACS
keeps its row, because MOS-STORE-239 makes removal a tombstone and only an erasure request
may set one.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from medos.db import conn as dbconn
from medos.db.tenancy import tenant_tx
from medos.gateway.backend import PacsBackend, backend_from_env
from medos.gateway.projection import admit_study, study_owner

__all__ = ["ReconcileReport", "reconcile_tenant", "main"]

log = logging.getLogger("medos.gateway.reconcile")

_TAG_STUDY_UID = "0020000D"
_TAG_PATIENT_ID = "00100020"
_TAG_ISSUER = "00100021"
_TAG_MODALITIES = "00080061"


@dataclass(frozen=True)
class ReconcileReport:
    """What one pass did. UID counts only -- never a UID list, never an attribute."""

    tenant_id: str
    backend_id: str
    seen: int
    created: int
    existing: int
    skipped_foreign: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "backend_id": self.backend_id,
            "studies_seen": self.seen,
            "studies_created": self.created,
            "studies_already_owned": self.existing,
            "studies_skipped_owned_by_other_tenant": self.skipped_foreign,
        }


def reconcile_tenant(
    conn: psycopg.Connection[Any],
    backend: PacsBackend,
    tenant_id: str,
    *,
    limit: int = 10000,
) -> ReconcileReport:
    """Claim every study the backend holds that nobody else owns, for `tenant_id`.

    Uses the same `admit_study` the STOW-RS path uses, so the ownership rule is stated
    once: a study another tenant already owns is SKIPPED, never re-assigned. MOS-DATA-048
    is the principle -- instances for a study owned by A that arrive as B "MUST NOT be
    merged into A's study and MUST NOT re-assign ownership".

    The QIDO-RS call here goes DIRECTLY to the backend and not through the Gateway, which
    is correct and is the one place in `medos` outside `medos.gateway` that it would be
    wrong: this function IS part of the Gateway (`medos.gateway.reconcile`), runs in the
    Gateway's own process or beside it with the Gateway's own configuration, and
    MOS-DATA-001 names the reconciler's sibling -- the Ingest Controller -- as a data-plane
    component that may read a PACS.
    """
    from medos.dicomweb.client import DicomWebClient

    client = DicomWebClient(
        backend.base_url,
        user=backend.credential.user,
        password=backend.credential.password,
        bearer_token=backend.credential.bearer_token,
        timeout_s=backend.timeout_s,
    )
    try:
        rows = client.qido_studies(limit=str(limit))
    finally:
        client.close()

    seen = created = existing = foreign = 0
    for row in rows:
        uid = _first(row, _TAG_STUDY_UID)
        if not uid:
            continue
        seen += 1
        owner = study_owner(conn, uid)
        if owner is not None and str(owner) != str(tenant_id):
            foreign += 1
            continue
        _, outcome = admit_study(
            conn,
            study_instance_uid=uid,
            tenant_id=tenant_id,
            pacs_backend=backend.backend_id,
            patient_id_value=_first(row, _TAG_PATIENT_ID),
            issuer_of_patient_id=_first(row, _TAG_ISSUER),
            modalities=_values(row, _TAG_MODALITIES),
        )
        if outcome == "created":
            created += 1
        else:
            existing += 1
        _refresh_counts(conn, tenant_id, uid, client_rows=row)

    report = ReconcileReport(
        tenant_id=str(tenant_id),
        backend_id=backend.backend_id,
        seen=seen,
        created=created,
        existing=existing,
        skipped_foreign=foreign,
    )
    log.info("gateway_reconcile", extra=report.as_dict())
    return report


def _refresh_counts(
    conn: psycopg.Connection[Any], tenant_id: str, uid: str, *, client_rows: dict[str, Any]
) -> None:
    """Copy the QIDO counters onto the row. MOS-STORE-240 says what they are worth.

    "code MUST NOT assume a counter is exact, and a decision needing an exact count counts
    rows or asks the Gateway." So these feed a study list, never an access decision.
    """
    series = _first(client_rows, "00201206")
    instances = _first(client_rows, "00201208")
    with tenant_tx(conn, tenant_id) as tx:
        tx.execute(
            "UPDATE studies SET series_count = COALESCE(%s, series_count), "
            "       instance_count = COALESCE(%s, instance_count), "
            "       last_synced_at = now() "
            " WHERE study_instance_uid = %s",
            (
                int(series) if str(series).isdigit() else None,
                int(instances) if str(instances).isdigit() else None,
                uid,
            ),
        )


def _first(row: dict[str, Any], tag: str) -> str:
    values = _values(row, tag)
    return values[0] if values else ""


def _values(row: dict[str, Any], tag: str) -> list[str]:
    element = row.get(tag)
    if not isinstance(element, dict):
        return []
    raw = element.get("Value") or []
    out: list[str] = []
    for v in raw:
        if isinstance(v, dict):  # PN is {"Alphabetic": "..."} -- never wanted here
            continue
        out.append(str(v))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m medos.gateway.reconcile",
        description=(
            "Claim the studies a PACS already holds for one named tenant "
            "(MOS-STORE-245). The tenant is an operator attribution and has no default "
            "(MOS-DATA-047)."
        ),
    )
    parser.add_argument("--tenant", required=True, help="the owning tenant's uuid")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    backend = backend_from_env()
    # autocommit for the reason `medos.gateway.app.create_app` gives at length: this
    # module's first statement is `study_owner()`'s bare execute, which would otherwise
    # open an implicit transaction and turn every later `tenant_tx()` into an uncommitted
    # savepoint. A reconciler that reports "created 2" and persists nothing is worse than
    # one that fails.
    conn = dbconn.connect(
        args.dsn or dbconn.dsn_from_env(),
        application_name="medos-reconcile",
        autocommit=True,
    )
    try:
        report = reconcile_tenant(conn, backend, args.tenant, limit=args.limit)
    finally:
        conn.close()
    import json

    print(json.dumps(report.as_dict(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
