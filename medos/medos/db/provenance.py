# SPDX-License-Identifier: Apache-2.0
"""`result_provenance` -- write one record per `Result`, read it back whole.

`MOS-SAFE-082`: "Exactly one provenance record MUST be written per `Result`, in the SAME
database transaction as the `Result` row and the terminal `Job` transition. A `Result`
without a provenance record MUST be impossible by foreign-key constraint, not by
convention. If the record cannot be written, the Job MUST fail."

That sentence is why `save()` opens no transaction of its own beyond `tenant_tx`'s
savepoint and why `medos.db.repo.save_result` calls it inline rather than afterwards: the
`results` INSERT and this INSERT are two statements of one transaction, which already
also carries `queue.complete()` and the T8 transition (CONTRACT.md section 8). There is no
window in which a result exists without its provenance, and no code path that writes one
without the other.

`sequence_no`, `prev_record_hash` and `record_hash` are NOT written here. The
`result_provenance_chain_trg` BEFORE INSERT trigger assigns them and writes them back into
the stored `record` document (`MOS-SAFE-090`), so the exported record is self-verifying
and no writer -- in any language -- can skip the chain.

Spec: MOS-SAFE-082, MOS-SAFE-083, MOS-SAFE-085, MOS-SAFE-087, MOS-SAFE-090,
MOS-STORE-278, MOS-STORE-279, MOS-STORE-228.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import current_tenant, tenant_tx

__all__ = ["save", "get_for_result", "get_for_job", "verify_chain", "find_by_object_uid"]


def _ts(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def save(
    conn: psycopg.Connection[Any],
    *,
    result_id: str,
    job_uuid: Any,
    root_job_uuid: Any,
    record: dict[str, Any],
    capability_id: str,
    capability_version: str,
    service_id: str,
    service_version: str,
    worker_version: str,
    runtime_version: str,
    clinical_use_mode: str,
    input_series_uids: tuple[str, ...],
    input_instance_uids: tuple[str, ...],
    input_uid_digest: str,
    input_pixel_digest: str,
    parent_job_uuid: Any = None,
    platform_commit: str | None = None,
) -> str | None:
    """Insert the record for one result. Returns its `provenance_id`, or None on replay.

    `ON CONFLICT (result_id) DO NOTHING`, for the same reason `save_result` does:
    `MOS-STORE-275` makes a retry that reaches the result stage again treat the
    uniqueness violation as SUCCESS rather than as an error, because deterministic UIDs
    (`MOS-IMG-062`) mean the second attempt would have written the same objects. Returning
    None says "the record was already there", which is a different fact from "a record was
    written now" and the caller may need it; neither is an error.

    Every scalar column is PROJECTED OUT of `record` by the caller passing the same values
    that went into it. The duplication is chapter 12 section 12.11's design
    (`MOS-STORE-279`: "Provenance records both internal ids and DICOM UIDs for its inputs.
    The redundancy is deliberate") and it is what makes "which result produced
    SOPInstanceUID X" an index lookup rather than a jsonb walk over every row.
    """
    tenant_id = current_tenant()
    inp = record["input"]
    ex = record["execution"]
    outputs = record.get("outputs") or []
    generated = [uid for o in outputs for uid in o["sop_instance_uids"]]

    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO result_provenance (
                result_id, tenant_id, provenance_id,
                job_id, job_public_id, parent_job_id, root_job_id, job_depth,
                idempotency_key, record_schema_version,
                sequence_no, prev_record_hash, record_hash,
                started_at, finished_at, duration_ms,
                patient_internal_id, study_internal_id, study_instance_uid,
                series_considered, input_series_uids, input_instance_uids,
                input_instance_count, input_uid_digest, input_pixel_digest,
                geometry, acquisition, gateway,
                capability_id, capability_version, resolution, governance,
                clinical_use_mode,
                service_id, service_version, service_image_digest, execution_mode,
                models, preprocessing_specs, preprocessing_selftest,
                operating_threshold, threshold_source, inference, accelerator,
                worker_version, runtime_version, platform_commit,
                applicability, plausibility, reproducibility_class,
                evidence, outputs, generated_object_uids, record)
            VALUES (
                %(result)s, %(tenant)s, %(prv)s,
                %(job)s, %(jobpub)s, %(parent)s, %(root)s, %(depth)s,
                %(ik)s, %(schema)s,
                -- Placeholders: result_provenance_chain_trg overwrites all three and
                -- writes them back into `record` before the row lands (MOS-SAFE-090).
                1, repeat('0', 64), repeat('0', 64),
                %(started)s, %(finished)s, %(duration)s,
                %(patient)s, %(studyint)s, %(study)s,
                %(considered)s, %(series)s, %(instances)s,
                %(icount)s, %(idigest)s, %(pdigest)s,
                %(geometry)s, %(acquisition)s, %(gateway)s,
                %(cap)s, %(capv)s, %(resolution)s, %(governance)s,
                %(mode)s,
                %(svc)s, %(svcv)s, %(image)s, %(exmode)s,
                %(models)s, %(prep)s, %(selftest)s,
                %(threshold)s, %(tsource)s, %(inference)s, %(accel)s,
                %(worker)s, %(runtime)s, %(commit)s,
                %(applicability)s, %(plausibility)s, %(repro)s,
                %(evidence)s, %(outputs)s, %(generated)s, %(record)s)
            ON CONFLICT (result_id) DO NOTHING
            RETURNING provenance_id
            """,
            {
                "result": result_id,
                "tenant": tenant_id,
                "prv": record["provenance_id"],
                "job": job_uuid,
                "jobpub": record["job_id"],
                "parent": parent_job_uuid,
                "root": root_job_uuid,
                "depth": record.get("job_depth", 0),
                "ik": record["idempotency_key"],
                "schema": record["record_schema_version"],
                "started": _ts(record["started_at"]),
                "finished": _ts(record["finished_at"]),
                "duration": record["duration_ms"],
                "patient": inp.get("patient_internal_id"),
                "studyint": inp.get("study_internal_id"),
                "study": inp["study_instance_uid"],
                "considered": Jsonb(inp["series_considered"]),
                "series": list(input_series_uids),
                "instances": list(input_instance_uids),
                "icount": len(input_instance_uids),
                "idigest": input_uid_digest,
                "pdigest": input_pixel_digest,
                "geometry": Jsonb(inp["canonical_geometry"]),
                "acquisition": Jsonb(inp.get("acquisition") or {}),
                "gateway": Jsonb(inp.get("gateway") or {}),
                "cap": capability_id,
                "capv": capability_version,
                "resolution": Jsonb(record.get("resolution") or {}),
                "governance": Jsonb(record.get("governance") or {}),
                "mode": clinical_use_mode,
                "svc": service_id,
                "svcv": service_version,
                "image": ex.get("service_image_digest"),
                "exmode": ex.get("execution_mode", "native"),
                "models": Jsonb(ex.get("models") or []),
                "prep": Jsonb(ex.get("preprocessing_specs") or []),
                "selftest": Jsonb(ex.get("preprocessing_selftest") or {}),
                "threshold": ex.get("operating_threshold"),
                "tsource": ex.get("threshold_source"),
                "inference": Jsonb(ex.get("inference") or {}),
                "accel": Jsonb(ex.get("accelerator") or {}),
                "worker": worker_version,
                "runtime": runtime_version,
                "commit": platform_commit,
                "applicability": Jsonb(ex.get("applicability") or {}),
                "plausibility": Jsonb(ex.get("plausibility") or {}),
                "repro": ex.get("reproducibility_class", "numeric_tolerance"),
                "evidence": Jsonb(record.get("evidence") or {}),
                "outputs": Jsonb(outputs),
                "generated": generated,
                "record": Jsonb(record),
            },
        ).fetchone()
    return None if row is None else str(row["provenance_id"])


def get_for_result(conn: psycopg.Connection[Any], result_id: str) -> dict[str, Any] | None:
    """`MOS-SAFE-087`'s body: the record as JSON, exactly as it was hashed.

    Returns the stored `record` document with the three chain members the trigger wrote
    back into it, so what a caller gets is what `provenance_verify_chain` verifies.
    """
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT record FROM result_provenance WHERE result_id = %s", (result_id,)
        ).fetchone()
    return None if row is None else dict(row["record"])


def get_for_job(conn: psycopg.Connection[Any], job_id: str) -> list[dict[str, Any]]:
    """Every record of one job, in write order. One per result (`MOS-STORE-278`)."""
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT result_id, record FROM result_provenance "
            "WHERE job_public_id = %s ORDER BY sequence_no",
            (job_id,),
        ).fetchall()
    return [{"result_id": str(r["result_id"]), **dict(r["record"])} for r in rows]


def find_by_object_uid(
    conn: psycopg.Connection[Any], sop_instance_uid: str
) -> dict[str, Any] | None:
    """"Which job produced this SOPInstanceUID?" -- the reverse of `MOS-SEC-155`'s last
    row, answered from the provenance record alone.

    This is the question a radiologist's screenshot generates: a reviewer holds one
    generated object and needs the run behind it. `generated_object_uids` with its GIN
    index is what makes it a lookup (`MOS-STORE-279`).
    """
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT result_id, job_public_id, record FROM result_provenance "
            "WHERE generated_object_uids @> ARRAY[%s]::dicom_uid[] LIMIT 1",
            (sop_instance_uid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "result_id": str(row["result_id"]),
        "job_id": row["job_public_id"],
        "record": dict(row["record"]),
    }


def verify_chain(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str | None = None,
    from_seq: int = 1,
) -> dict[str, Any] | None:
    """`MOS-SAFE-090`/`MOS-SAFE-091`: the daily chain check, as a function.

    Returns None when intact, else `{sequence_no, reason}` for the FIRST divergence.
    `MOS-SAFE-091` requires a `provenance.chain.broken` event and an `AuditEvent` on any
    mismatch; emitting those is the scheduler's job and not this function's, because a
    verifier that writes is a verifier that can be made to write.
    """
    tid = tenant_id or current_tenant()
    with tenant_tx(conn, tid):
        row = conn.execute(
            "SELECT sequence_no, reason FROM provenance_verify_chain(%s, %s)",
            (tid, from_seq),
        ).fetchone()
    return dict(row) if row is not None else None
