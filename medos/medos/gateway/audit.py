# SPDX-License-Identifier: Apache-2.0
"""The PHI-access record. MOS-DATA-022 -- the reason the Gateway exists at all.

    "The Gateway MUST emit one `AuditEvent` per PHI-bearing response. This is the only
    point in the architecture where a per-patient image access can be recorded, because
    every image read in the system passes through it."

    -- docs/spec/03-medical-data-plane.md, MOS-DATA-022

and `docs/spec/15-delivery.md` section 15.2.4 gives that sentence as one of the two
reasons the Gateway is scheduled in this block rather than later.

This module writes no SQL. `medos.db.audit.record()` owns the `audit_events` insert, the
per-tenant gapless `seq` and the MOS-SEC-151 hash chain; what is here is the MOS-DATA-022
field mapping and, more importantly, the rules about what must NOT be in the row.

WHAT MAY NOT APPEAR IN THIS RECORD
----------------------------------
MOS-DATA-022: "`patient_key` is a tenant-scoped surrogate, never a `PatientID`. No
attribute value from the study, including `PatientName`, `StudyDescription` or
`SeriesDescription`, MAY appear in this record (spine section 8)." MOS-STORE-307 says the
same one layer down: audit rows carry "only the surrogate `patient_id` and, for imaging
access, `study_instance_uid`". So:

  * `patient_key` comes from `medos.gateway.projection.patient_key`, a digest of the
    tenant-scoped `patients.id`, and the PACS's `PatientID` never reaches this module.
  * `resource_id` is the P1 `study_ref` (`MOS-SEC-106`), not the raw UID; the raw UID
    travels in the row's own `study_instance_uid` column, which is what
    `audit_events_study_idx` serves.
  * `_assert_phi_free` rejects a `detail` dict carrying any of the attribute-shaped keys,
    so "someone adds `study_description` to the detail for debugging" fails a test rather
    than shipping a PHI column to wherever audit is exported.

WHEN THE ROW IS WRITTEN, AND WHY IT IS AFTER THE BYTES AND NOT BEFORE
---------------------------------------------------------------------
MOS-DATA-022 asks for one event per PHI-bearing RESPONSE. A row written at decision time
records an intention; a row written when the response body finishes records what actually
left the building, including a retrieval the client abandoned halfway -- which is the one
an access review most wants to see accurately. `medos.gateway.app` writes it from the
streaming generator's `finally`, so a disconnect still produces the record with the byte
and instance counts actually transferred.

A DENIAL is the opposite case and is written immediately: there is no body to wait for,
and MOS-SEC-148 requires class `phi` to "produce an `AuditEvent` on both `allow` and
`deny`."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from medos.db import audit as dbaudit
from medos.db.tenancy import tenant_context

__all__ = [
    "AccessRecord",
    "OPERATIONS",
    "PEP_FOR_OPERATION",
    "record_phi_access",
    "record_denial",
]

log = logging.getLogger("medos.gateway.audit")

# MOS-DATA-022's `operation` member. One string per route of MOS-DATA-007, spelled once.
OPERATIONS: frozenset[str] = frozenset(
    {
        "QIDO-RS.studies",
        "QIDO-RS.series",
        "QIDO-RS.instances",
        "WADO-RS.study",
        "WADO-RS.series",
        "WADO-RS.instance",
        "WADO-RS.frames",
        "WADO-RS.study_metadata",
        "WADO-RS.series_metadata",
        "WADO-RS.rendered",
        "STOW-RS.study",
    }
)

# MOS-SEC-047: "There MUST be exactly eight Policy Enforcement Points." Three of them are
# the Gateway's, and which one applies is a property of the operation, not of the handler
# that happens to run.
PEP_FOR_OPERATION: dict[str, str] = {
    "QIDO-RS.studies": "gateway.query",
    "QIDO-RS.series": "gateway.query",
    "QIDO-RS.instances": "gateway.query",
    "WADO-RS.study": "gateway.retrieve",
    "WADO-RS.series": "gateway.retrieve",
    "WADO-RS.instance": "gateway.retrieve",
    "WADO-RS.frames": "gateway.retrieve",
    "WADO-RS.study_metadata": "gateway.retrieve",
    "WADO-RS.series_metadata": "gateway.retrieve",
    "WADO-RS.rendered": "gateway.retrieve",
    "STOW-RS.study": "gateway.store",
}

# Keys that would put a DICOM attribute value into `detail`. Not a blocklist of every
# possible attribute -- that is unwinnable -- but of the ones a developer would plausibly
# reach for while debugging, which is the realistic leak path (MOS-DATA-022, spine 8).
_FORBIDDEN_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "patient_name", "patientname", "patient_id", "patientid", "mrn",
        "patient_birth_date", "patientbirthdate", "birth_date", "dob",
        "study_description", "studydescription", "series_description",
        "seriesdescription", "accession_number", "accessionnumber",
        "referring_physician", "referringphysician", "institution_name",
        "patient_address", "patient_telephone",
    }
)


@dataclass
class AccessRecord:
    """One PHI-bearing exchange, as MOS-DATA-022 shapes it.

    Mutable on purpose and by exactly one caller: `medos.gateway.app` creates it at
    decision time and fills `instance_count`, `bytes_out` and `outcome` as the response
    streams, then writes once. A frozen record would force the route to carry four loose
    counters through a generator instead.
    """

    tenant_id: str
    principal: Any                       # medos.gateway.auth.Principal
    operation: str
    consumer_class: str
    trace_id: str
    request_id: str
    study_instance_uid: str | None = None
    series_instance_uid: str | None = None
    sop_instance_uid: str | None = None
    patient_key: str | None = None
    job_id: str | None = None
    source_ip: str | None = None
    user_agent: str | None = None
    instance_count: int = 0
    bytes_out: int = 0
    outcome: str = "allow"
    reason_code: str | None = None
    deid_profile_version: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def detail(self) -> dict[str, Any]:
        """MOS-DATA-022's non-column members, as `audit_events.detail`.

        `series_instance_uid`, `instance_count`, `operation`, `consumer_class`,
        `deid_profile_version` and `bytes` are in the requirement's JSON but are not
        columns of Chapter 12's `audit_events`; `detail` is where MOS-SEC-146 puts
        "counts, reason codes, old/new non-PHI values", and every one of these is a count,
        a UID or a closed-enum word.
        """
        doc: dict[str, Any] = {
            "operation": self.operation,
            "consumer_class": self.consumer_class,
            "instance_count": self.instance_count,
            "bytes": self.bytes_out,
        }
        if self.patient_key:
            doc["patient_key"] = self.patient_key
        if self.series_instance_uid:
            doc["series_instance_uid"] = self.series_instance_uid
        if self.sop_instance_uid:
            doc["sop_instance_uid"] = self.sop_instance_uid
        if self.reason_code:
            doc["reason_code"] = self.reason_code
        if self.deid_profile_version is not None:
            doc["deid_profile_version"] = self.deid_profile_version
        doc.update(self.extra)
        _assert_phi_free(doc)
        return doc


def _assert_phi_free(detail: dict[str, Any]) -> None:
    """MOS-DATA-022 / MOS-STORE-307, enforced rather than reviewed.

    Raises rather than dropping the offending key: silently stripping it would make the
    record incomplete in a way nobody notices, and the correct response to "an attribute
    value is about to enter the audit trail" is to stop, not to tidy.
    """
    bad = sorted(k for k in detail if str(k).lower() in _FORBIDDEN_DETAIL_KEYS)
    if bad:
        raise ValueError(
            f"audit detail must carry no attribute value from the study "
            f"(MOS-DATA-022, MOS-STORE-307); refusing keys: {bad}"
        )


def record_phi_access(
    conn: psycopg.Connection[Any], access: AccessRecord
) -> str:
    """Write the MOS-DATA-022 row. Returns its `aud_...` public id.

    `action` is `phi.access` and `action_class` is `phi`, which puts it in MOS-SEC-148's
    mandatory set: "Every action whose permission class is `phi`, `clinical`, `admin` or
    `governance` MUST produce an `AuditEvent` on both `allow` and `deny`." It is
    deliberately NOT class `read`, the one class MOS-SEC-148 permits a tenant to sample: a
    sampled per-patient image-access trail is not a trail.
    """
    if access.operation not in OPERATIONS:
        raise ValueError(f"{access.operation!r} is not a MOS-DATA-007 operation")
    pep = PEP_FOR_OPERATION[access.operation]
    resource = (
        dbaudit.Resource.study(access.tenant_id, access.study_instance_uid)
        if access.study_instance_uid
        else dbaudit.Resource(kind="study_collection", id=f"tenant:{access.tenant_id}")
    )
    # The tenant is bound here rather than assumed: this is called from a streaming
    # generator's `finally`, which may run on a different task than the handler that
    # bound the request's tenant, and `record()` reads `current_tenant()`.
    with tenant_context(access.tenant_id):
        return dbaudit.record(
            conn,
            action="phi.access",
            action_class="phi",
            actor=dbaudit.Actor.from_principal(access.principal),
            resource=resource,
            outcome=access.outcome,
            pep=pep,
            trace_id=access.trace_id,
            request_id=access.request_id,
            on_behalf_of=_obo(access.principal),
            job_id=access.job_id,
            study_instance_uid=access.study_instance_uid,
            source_ip=access.source_ip,
            user_agent=access.user_agent,
            detail=access.detail(),
        )


def record_denial(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    principal: Any,
    operation: str,
    consumer_class: str,
    trace_id: str,
    request_id: str,
    reason_code: str,
    action: str = "phi.access",
    study_instance_uid: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """A refusal. MOS-SEC-148 requires the `deny` row as firmly as the `allow` row.

    `action` is a parameter because MOS-DATA-009 names a second event type by hand:
    "on mismatch the Gateway MUST return `403` ... and MUST emit an `AuditEvent` of type
    `gateway.tenant_mismatch`."

    NOTE what is recorded on a MOS-DATA-013 denial: `reason_code` is `study_not_in_tenant`
    and `study_instance_uid` IS recorded. That is not an inconsistency with the 404 the
    caller receives -- the point of MOS-DATA-013 is that the CALLER learns nothing, while
    the deployment's own audit trail is exactly where "someone probed for this UID" must
    be visible.
    """
    if operation not in OPERATIONS:
        raise ValueError(f"{operation!r} is not a MOS-DATA-007 operation")
    resource = (
        dbaudit.Resource.study(tenant_id, study_instance_uid)
        if study_instance_uid
        else dbaudit.Resource(kind="study_collection", id=f"tenant:{tenant_id}")
    )
    detail = {
        "operation": operation,
        "consumer_class": consumer_class,
        "reason_code": reason_code,
        "instance_count": 0,
        "bytes": 0,
    }
    _assert_phi_free(detail)
    with tenant_context(tenant_id):
        return dbaudit.record(
            conn,
            action=action,
            action_class="phi",
            actor=dbaudit.Actor.from_principal(principal),
            resource=resource,
            outcome="deny",
            pep=PEP_FOR_OPERATION[operation],
            trace_id=trace_id,
            request_id=request_id,
            on_behalf_of=_obo(principal),
            study_instance_uid=study_instance_uid,
            source_ip=source_ip,
            user_agent=user_agent,
            detail=detail,
        )


def _obo(principal: Any) -> dbaudit.OnBehalfOf | None:
    """MOS-SEC-147's delegation, when the principal carries one.

    Without it the access question "which user caused these images to be read" has no
    answer for anything a worker retrieves, because the worker is the actor on every row.
    """
    kind = getattr(principal, "on_behalf_of_kind", None)
    pid = getattr(principal, "on_behalf_of_id", None)
    if not kind or not pid:
        return None
    return dbaudit.OnBehalfOf(
        kind=str(kind), id=str(pid), role=getattr(principal, "on_behalf_of_role", None)
    )
