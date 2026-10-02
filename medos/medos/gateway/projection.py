# SPDX-License-Identifier: Apache-2.0
"""The `studies` projection, read as the Gateway's tenancy predicate.

MOS-DATA-010: "Tenancy of imaging data is held in the `studies` projection ... There is no
separate `study_tenancy` table: the projection row *is* the tenancy record, and it, not
the PACS, is authoritative for who may see a study."

Every function here goes through `medos.db.tenancy.tenant_tx()` -- the single repository
chokepoint of 15.2.4 item 1 -- with exactly one deliberate exception, `study_owner()`,
which calls the SECURITY DEFINER function that migration 0004 creates for MOS-DATA-012 and
explains at length there.

Requirements implemented here
-----------------------------
MOS-DATA-011  the QIDO filter is an INTERSECTION applied AFTER the backend answers, never
              a matching key pushed into the backend query.
MOS-DATA-012  STOW resolves ownership across tenants on `studies_uid_global_idx`, rejects
              the whole request with 409 if another tenant owns the study, and creates the
              row for the caller's tenant in the same transaction that records the attempt.
MOS-DATA-013  a study that exists in the backend but not in the caller's tenant set is
              indistinguishable from one that does not exist. This module answers with a
              bool and never with a reason, so a route cannot accidentally branch on the
              difference.
MOS-DATA-022  `patient_key` is "a tenant-scoped surrogate, never a `PatientID`".
MOS-STORE-239 erasure is a tombstone: every read here carries `erased_at IS NULL`.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from medos.db.tenancy import tenant_tx

__all__ = [
    "StudyRow",
    "patient_key",
    "study_owner",
    "owned_subset",
    "is_owned",
    "tenant_study_uids",
    "study_row",
    "admit_study",
    "ProjectionUnavailable",
]

log = logging.getLogger("medos.gateway.projection")


class ProjectionUnavailable(RuntimeError):
    """MOS-DATA-025: "If the `studies` projection is unreachable ... MUST return 503".

    A distinct type because MOS-DATA-025 also says the backend circuit breaker "MUST NOT
    be applied to the tenancy or de-identification path" -- the two failures have to stay
    separable at the point they are caught, or a tripped breaker degrades into unfiltered
    passthrough.
    """


@dataclass(frozen=True)
class StudyRow:
    """The projection fields the Gateway acts on. Deliberately not the whole row.

    No `study_description`, no `accession_number`, no `referring_physician`: CONTRACT.md
    section 11 and MOS-DATA-022 both forbid an attribute value from the study reaching a
    log or an audit record, and the cheapest way to keep that true is for the object the
    audit writer is handed not to contain one.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID
    patient_id: uuid.UUID
    study_instance_uid: str
    pacs_backend: str
    instance_count: int
    series_count: int

    @property
    def patient_key(self) -> str:
        return patient_key(self.patient_id)


def patient_key(patient_id: uuid.UUID | str) -> str:
    """MOS-DATA-022's `patient_key`: "a tenant-scoped surrogate, never a `PatientID`".

    Derived from `patients.id`, which is already tenant-scoped by
    `patients_natural_uk (tenant_id, issuer_of_patient_id, patient_id_value)`: the same
    hospital MRN held by two tenants is two rows and therefore two surrogates, so a key
    seen in one tenant's audit export says nothing about another tenant's holdings.

    A digest rather than the raw uuid so that the key is stable under a projection rebuild
    that re-mints `patients.id`... it is NOT, and that is worth stating plainly: a rebuild
    (MOS-STORE-245) that re-mints the row id re-mints the surrogate, and historical audit
    rows then point at a surrogate no live row resolves to. MOS-STORE-244 is the fix --
    "a rebuild preserves platform state by matching on the tenant-scoped natural key" --
    and it is the reconciler's obligation, not this function's.
    """
    raw = str(patient_id)
    return "pk_" + hashlib.sha256(
        f"medicalos/patient_key/v1\x1f{raw}".encode()
    ).hexdigest()[:16]


# =====================================================================================
# Reads
# =====================================================================================
def study_owner(
    conn: psycopg.Connection[Any], study_instance_uid: str
) -> uuid.UUID | None:
    """MOS-DATA-012's cross-tenant ownership verdict. The ONLY non-tenant-scoped read.

    Delegates to `study_owner_tenant(dicom_uid)`, the SECURITY DEFINER function migration
    0004 creates. What crosses the tenancy boundary is one uuid or NULL; no row, no
    attribute, no count.

    MOS-DATA-012 is explicit that a tenant-scoped read will not do: "a query that cannot
    see another tenant's row reports the study as unknown and silently splits ownership."

    Called only from the STOW admission path. It is NOT a retrieval check -- using it as
    one would build the existence oracle MOS-DATA-013 forbids.
    """
    try:
        row = conn.execute(
            "SELECT study_owner_tenant(%s) AS owner", (study_instance_uid,)
        ).fetchone()
    except psycopg.Error as exc:
        raise ProjectionUnavailable(str(exc)) from exc
    if row is None:
        return None
    owner = row["owner"] if isinstance(row, dict) else row[0]
    return owner


def owned_subset(
    conn: psycopg.Connection[Any], study_instance_uids: Iterable[str]
) -> set[str]:
    """MOS-DATA-011: the intersection of a candidate set with the tenant's owned set.

    "QIDO-RS responses MUST be filtered by the Gateway against the `studies` projection
    **after** the backend responds. The Gateway MUST NOT delegate the tenancy predicate to
    the backend query, because a PACS that has no tenancy model will silently ignore an
    unknown matching key and return everything."

    The predicate that does the work is row-level security, not the WHERE clause: the
    query below names no tenant. `tenant_tx()` binds `medicalos.tenant_id` and
    `studies_tenant_isolation` does the rest, which is what makes a forgotten filter a
    42704 rather than a leak.
    """
    uids = [u for u in dict.fromkeys(study_instance_uids) if u]
    if not uids:
        return set()
    try:
        with tenant_tx(conn) as tx:
            rows = tx.execute(
                "SELECT study_instance_uid FROM studies "
                " WHERE study_instance_uid = ANY(%s) AND erased_at IS NULL",
                (uids,),
            ).fetchall()
    except psycopg.Error as exc:
        raise ProjectionUnavailable(str(exc)) from exc
    return {
        (r["study_instance_uid"] if isinstance(r, dict) else r[0]) for r in rows
    }


def is_owned(conn: psycopg.Connection[Any], study_instance_uid: str) -> bool:
    """MOS-DATA-013. A bool, and never a reason.

    "A QIDO-RS or WADO-RS request naming a study that exists in the backend but is not in
    the caller's tenant set MUST return `404`, not `403`. A `403` here is an existence
    oracle." Returning a bool rather than an enum is the mechanical guard: a route cannot
    branch on "exists elsewhere" because this function will not tell it.
    """
    return bool(owned_subset(conn, [study_instance_uid]))


def study_row(
    conn: psycopg.Connection[Any], study_instance_uid: str
) -> StudyRow | None:
    """The tenant's own projection row, or None. Tenant-scoped; RLS is the predicate."""
    try:
        with tenant_tx(conn) as tx:
            row = tx.execute(
                "SELECT id, tenant_id, patient_id, study_instance_uid, pacs_backend, "
                "       instance_count, series_count "
                "  FROM studies "
                " WHERE study_instance_uid = %s AND erased_at IS NULL",
                (study_instance_uid,),
            ).fetchone()
    except psycopg.Error as exc:
        raise ProjectionUnavailable(str(exc)) from exc
    if row is None:
        return None
    return _to_study_row(row)


def tenant_study_uids(conn: psycopg.Connection[Any], *, limit: int = 5000) -> list[str]:
    """Every StudyInstanceUID this tenant owns. Used only by the QIDO study list.

    Bounded, because MOS-DATA-023's per-response instance cap has a sibling problem here:
    an unbounded list on a tenant with a hundred thousand studies is a request that never
    returns.
    """
    try:
        with tenant_tx(conn) as tx:
            rows = tx.execute(
                "SELECT study_instance_uid FROM studies "
                " WHERE erased_at IS NULL ORDER BY first_seen_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
    except psycopg.Error as exc:
        raise ProjectionUnavailable(str(exc)) from exc
    return [(r["study_instance_uid"] if isinstance(r, dict) else r[0]) for r in rows]


def quarantined_study_uids(conn: psycopg.Connection[Any]) -> set[str]:
    """Every StudyInstanceUID with an OPEN quarantine row. MOS-DATA-024 / MOS-STORE-349.

    "Quarantined instances MUST be invisible through every Gateway route to every
    principal except one holding `phi.admin`, and MUST NOT appear in QIDO-RS results"
    (MOS-DATA-024); MOS-STORE-349 states the mechanism: "the Gateway subtracts every open
    row from every QIDO-RS response."

    NO TENANT CONTEXT, AND THAT IS THE POINT. `quarantine` is one of the two pre-tenancy
    tables of MOS-STORE-345: it carries no `tenant_id` and no row security, because it is
    the table that RECORDS that the tenant could not be resolved. A policy on it would be
    circular. It is therefore read on a bare connection, and the column that names a
    tenant is called `owning_tenant_id` for its role, never `tenant_id`.

    Study-keyed rather than instance-keyed, which is a deliberate over-subtraction: one
    quarantined instance hides its whole study. MOS-STORE-349's own comment gives the
    reason -- "a 1,131-instance chest CT from an unmapped AE is 1,131 rows" -- and the
    conservative direction is the safe one, because the alternative shows a clinician a
    study that is missing slices without saying so.
    """
    try:
        rows = conn.execute(
            "SELECT DISTINCT study_instance_uid FROM quarantine "
            " WHERE released_at IS NULL AND purged_at IS NULL"
        ).fetchall()
    except psycopg.Error as exc:
        raise ProjectionUnavailable(str(exc)) from exc
    return {(r["study_instance_uid"] if isinstance(r, dict) else r[0]) for r in rows}


# =====================================================================================
# The STOW admission path.  MOS-DATA-012.
# =====================================================================================
def admit_study(
    conn: psycopg.Connection[Any],
    *,
    study_instance_uid: str,
    tenant_id: str,
    pacs_backend: str,
    patient_id_value: str = "",
    issuer_of_patient_id: str = "",
    modalities: Sequence[str] = (),
) -> tuple[StudyRow, str]:
    """Resolve or create the `studies` row for a STOW-RS request. MOS-DATA-012.

    Returns `(row, outcome)` where `outcome` is `"existing"` or `"created"`.
    Raises `PermissionError` when another tenant owns the study -- the 409 of MOS-DATA-012,
    "the whole request MUST be rejected with `409` and none of its instances stored".

    THE ORDER MATTERS AND IS THE REQUIREMENT
    ----------------------------------------
    The cross-tenant resolution happens FIRST and outside the tenant-scoped read, because
    MOS-DATA-012 says a tenant-scoped read "reports the study as unknown and silently
    splits ownership". Only after the global verdict comes back "nobody, or you" does the
    tenant-scoped upsert run.

    ONE TRANSACTION
    ---------------
    "the Gateway MUST create the `studies` row for the calling principal's tenant in the
    same transaction that records the STOW attempt". The caller opens the transaction and
    writes its audit row inside it; `tenant_tx()` joins rather than nests when a
    transaction is already open, so the two commit together or not at all.

    `patient_id_value` is a PHI value ((0010,0020)). It is written to `patients` and is
    never returned, never logged and never put in an audit row -- only `patient_key` is
    (MOS-DATA-022, MOS-STORE-307). The default `''` is for the case the STOW payload
    carried no PatientID: an empty natural key is still a stable one per tenant, and
    inventing a synthetic MRN would be worse than recording its absence.
    """
    owner = study_owner(conn, study_instance_uid)
    if owner is not None and str(owner) != str(tenant_id):
        raise PermissionError(
            "study_owned_by_other_tenant"
        )  # deliberately carries no uid and no tenant id

    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            "SELECT id, tenant_id, patient_id, study_instance_uid, pacs_backend, "
            "       instance_count, series_count "
            "  FROM studies WHERE study_instance_uid = %s AND erased_at IS NULL",
            (study_instance_uid,),
        ).fetchone()
        if row is not None:
            return _to_study_row(row), "existing"

        patient = tx.execute(
            "INSERT INTO patients (tenant_id, patient_id_value, issuer_of_patient_id, "
            "                      phi_state) "
            "VALUES (%s, %s, %s, 'identified') "
            "ON CONFLICT (tenant_id, issuer_of_patient_id, patient_id_value) "
            "  DO UPDATE SET last_synced_at = now() "
            "RETURNING id",
            (tenant_id, patient_id_value, issuer_of_patient_id),
        ).fetchone()
        patient_uuid = patient["id"] if isinstance(patient, dict) else patient[0]

        created = tx.execute(
            "INSERT INTO studies (tenant_id, patient_id, study_instance_uid, "
            "                     pacs_backend, phi_state, modalities_in_study) "
            "VALUES (%s, %s, %s, %s, 'identified', %s) "
            "RETURNING id, tenant_id, patient_id, study_instance_uid, pacs_backend, "
            "          instance_count, series_count",
            (tenant_id, patient_uuid, study_instance_uid, pacs_backend, list(modalities)),
        ).fetchone()
        assert created is not None
        return _to_study_row(created), "created"


def _to_study_row(row: Any) -> StudyRow:
    get = (lambda k, i: row[k]) if isinstance(row, dict) else (lambda k, i: row[i])
    return StudyRow(
        id=get("id", 0),
        tenant_id=get("tenant_id", 1),
        patient_id=get("patient_id", 2),
        study_instance_uid=get("study_instance_uid", 3),
        pacs_backend=get("pacs_backend", 4),
        instance_count=get("instance_count", 5),
        series_count=get("series_count", 6),
    )
