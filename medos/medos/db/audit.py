# SPDX-License-Identifier: Apache-2.0
"""`audit_events` -- the one writer, and the one verifier.

`docs/spec/15-delivery.md` section 15.2.4: "`audit_events` append-only at the database
role level (`REVOKE UPDATE, DELETE` from the application role -- one line in a migration
now, an ugly backfill later)". The migration is `0003_audit_provenance.up.sql`; this
module is what may write through it, and it exists as a chokepoint for the same reason
`medos.db.tenancy.tenant_tx` does: a control with several entry points has as many
postures as it has entry points.

WHAT THIS MODULE REFUSES TO LET A CALLER DO
-------------------------------------------
`MOS-SEC-147` is the requirement that shapes the whole file:

    `actor` + `on_behalf_of` MUST be able to express "`pulmo.effusion@2.1.0` acting for
    `u_7d2f4a` in `job_01JB8N...`". A flat `actor: "user_123"` string MUST NOT be used;
    it cannot answer an access question about a delegated execution, which is the only
    kind of access question this platform generates.

So `record()` takes an `Actor` and, optionally, an `OnBehalfOf` -- never a string. The
delegated case is the normal case here: everything a capability does inside a job is
`Actor(kind="service_version", id="pulmo.lung_seg@0.1.0")` acting for the principal that
created the job, in that job. An audit table that can only say "the worker did it"
answers "who caused this patient's images to be read?" with the name of a process.

WHAT IS NOT HERE, AND WHY
-------------------------
No hash computation. `seq`, `prev_hash` and `hash` are assigned by the
`audit_events_chain_trg` BEFORE INSERT trigger (`MOS-SEC-151`), because a chain the
writer computes is a chain the writer can decline to compute -- and because the Go
control plane replaces this HTTP surface at `MOS-REL-084` and must land on byte-identical
hashes without reimplementing RFC 8785. `verify_chain()` calls the same SQL function the
trigger does, so a verifier that disagrees with a writer is not expressible
(`MOS-SEC-153`).

No PDP. `policy_decision_id` stays null throughout 0.1.0 (chapter 8 section 8.10 puts the
Cedar PDP at 0.2.0); the column exists so `MOS-SEC-155`'s join needs no later ALTER.

Spec: MOS-SEC-146, MOS-SEC-147, MOS-SEC-148, MOS-SEC-149, MOS-SEC-150, MOS-SEC-153,
MOS-SEC-155, MOS-STORE-228.
No PHI (CONTRACT.md section 11): UIDs, codes, counts and digests only.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import current_tenant, tenant_tx
from medos.sdk.canonical import new_ulid, study_ref

__all__ = [
    "ACTOR_KINDS",
    "ACTION_CLASSES",
    "MANDATORY_CLASSES",
    "OUTCOMES",
    "PEP_IDS",
    "Actor",
    "OnBehalfOf",
    "Resource",
    "new_request_id",
    "record",
    "trail_for_job",
    "trail_for_request",
    "verify_chain",
]


# MOS-SEC-146's closed `actor.kind` set. `service_version` is the kind that makes
# MOS-SEC-147's example expressible at all.
ACTOR_KINDS: frozenset[str] = frozenset(
    {"user", "service_account", "workload", "service_version", "platform_admin"}
)

# MOS-SEC-146 section 8.4.2's permission classes. "Class drives audit and caching, not
# grants."
ACTION_CLASSES: frozenset[str] = frozenset(
    {"read", "write", "phi", "clinical", "admin", "governance"}
)

# MOS-SEC-148: "Every action whose permission class is `phi`, `clinical`, `admin` or
# `governance` MUST produce an `AuditEvent` on both `allow` and `deny`. Class `read` MAY
# be sampled at a tenant-configured rate, default 1.0."
#
# Exported so a call site can assert its own obligation rather than rely on a reviewer
# noticing. Nothing in this module samples: the default rate is 1.0 and there is no
# tenant configuration to read in this slice, so every call written is recorded.
MANDATORY_CLASSES: frozenset[str] = frozenset({"phi", "clinical", "admin", "governance"})

OUTCOMES: frozenset[str] = frozenset({"allow", "deny", "error"})

# MOS-SEC-047: "There MUST be exactly eight Policy Enforcement Points." `llm.request` is
# 0.4.0 and is listed because the set is closed, not because anything reaches it.
PEP_IDS: frozenset[str] = frozenset(
    {
        "api.request",
        "gateway.query",
        "gateway.retrieve",
        "gateway.store",
        "dispatch.job",
        "runner.invoke",
        "result.publish",
        "llm.request",
    }
)


@dataclass(frozen=True)
class Actor:
    """`MOS-SEC-146`'s `actor` jsonb -- `{kind, id, auth, key_id}` -- as four fields.

    `id` is deliberately `str` and not `uuid`: chapter 8's own worked example is
    `pulmo.effusion@2.1.0` for a `service_version` and `u_7d2f4a` for a user, and
    `jobs.created_by_id` is `text` in schema.sql. A uuid column here would make the
    requirement's example unrepresentable, which is a strange way to satisfy it.
    """

    kind: str
    id: str
    auth: str | None = None       # api_key | oidc | job_token | internal | none
    key_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            raise ValueError(f"actor.kind {self.kind!r} is not one of {sorted(ACTOR_KINDS)}")
        if not self.id:
            raise ValueError("actor.id MUST be present (MOS-SEC-146)")

    @classmethod
    def for_capability(cls, capability_id: str, version: str) -> Actor:
        """The `service_version` actor of `MOS-SEC-147`, spelled `<id>@<version>` once.

        One helper rather than an f-string at each call site, because the audit trail is
        only queryable if every row that means "this capability" spells it identically.
        """
        return cls(kind="service_version", id=f"{capability_id}@{version}", auth="internal")

    @classmethod
    def from_principal(cls, principal: Any) -> Actor:
        """Build from anything with the `medos.gateway.auth.Principal` shape.

        Duck-typed on purpose: `medos.db` MUST NOT import `medos.gateway`, which sits
        above it. The five attribute names are the contract and they are the same five
        MOS-SEC-146 names.
        """
        return cls(
            kind=str(principal.actor_kind),
            id=str(principal.principal_id),
            auth=str(getattr(principal, "auth_method", None) or "api_key"),
            key_id=str(getattr(principal, "key_id", "") or "") or None,
        )


@dataclass(frozen=True)
class OnBehalfOf:
    """`MOS-SEC-146`'s `on_behalf_of` jsonb -- `{kind, id, role}` -- "populated from the
    Job Token `obo` claim".

    The half of `MOS-SEC-147` that a flat actor string cannot express. Present on every
    row a capability or a worker writes inside a job: the worker is the actor, the
    principal who asked for the job is who it acts for, and without the second field the
    access question "which user caused these images to be read" has no answer at all.
    """

    kind: str                     # user | service_account
    id: str
    role: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("user", "service_account"):
            raise ValueError(
                f"on_behalf_of.kind {self.kind!r} MUST be 'user' or 'service_account' "
                "(MOS-SEC-146)"
            )
        if not self.id:
            raise ValueError("on_behalf_of.id MUST be present when a delegation exists")

    @classmethod
    def from_job(cls, job: dict[str, Any]) -> OnBehalfOf | None:
        """The delegation implied by a `jobs` row's `created_by_kind` / `created_by_id`.

        `triage` is a `created_by_kind` in schema.sql and is NOT an `on_behalf_of.kind`:
        a study-arrival trigger acts for no one, so it produces no delegation and the
        worker's own `workload` actor is the whole answer. Returning `None` is that fact,
        not a missing value.
        """
        kind = str(job.get("created_by_kind") or "")
        if kind not in ("user", "service_account"):
            return None
        return cls(kind=kind, id=str(job["created_by_id"]))


@dataclass(frozen=True)
class Resource:
    """`MOS-SEC-146`'s `resource` jsonb -- `{kind, id}` "plus `study_ref` where
    applicable; never a raw UID for a P3 subject"."""

    kind: str
    id: str
    version: str | None = None

    @classmethod
    def study(cls, tenant_id: str, study_instance_uid: str) -> Resource:
        """A study as `MOS-SEC-106` requires it on an audited surface: the P1 `study_ref`.

        The raw StudyInstanceUID is P2 (`MOS-SEC-105`) and travels in the row's own
        `study_instance_uid` column, which chapter 12 section 12.13 declares and which the
        `audit_events_study_idx` partial index serves. What is NOT done is putting the raw
        UID in `resource_id`, because `resource` is the member that appears in an export.
        """
        return cls(kind="study", id=study_ref(tenant_id, study_instance_uid))


def new_request_id() -> str:
    """`MOS-SEC-150`'s pair key.

    "two events MUST be written, joined by `request_id`: the authorization event
    committed BEFORE the upstream call, and the effect event after it". One value,
    generated once by the caller and passed to both `record()` calls -- which is why this
    is a function here and not a default inside `record()`, where each call would mint its
    own and the pair would never join.
    """
    return new_ulid("req")


# =====================================================================================
# THE WRITER
# =====================================================================================
def record(
    conn: psycopg.Connection[Any],
    *,
    action: str,
    action_class: str,
    actor: Actor,
    resource: Resource,
    outcome: str,
    pep: str,
    trace_id: str,
    request_id: str,
    on_behalf_of: OnBehalfOf | None = None,
    job_id: str | None = None,
    span_id: str | None = None,
    study_instance_uid: str | None = None,
    source_ip: str | None = None,
    user_agent: str | None = None,
    detail: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
    policy_decision_id: str | None = None,
    break_glass_id: str | None = None,
) -> str:
    """Append one `AuditEvent`. Returns its `public_id` (`aud_` + ULID).

    `MOS-SEC-149`: "Where the audited action occurs inside a database transaction, the
    `AuditEvent` MUST be inserted in that same transaction. A committed state change with
    no audit row MUST NOT be possible." This function opens no transaction of its own
    beyond `tenant_tx`'s savepoint, so a caller already inside one gets exactly that, and
    a caller that is not gets a transaction of one statement -- which is the right answer
    for an authorization event that must commit BEFORE the upstream call (`MOS-SEC-150`).

    `job_id` is the PUBLIC job id (`job_...`, `MOS-STORE-357`). The uuid and `root_job_id`
    are resolved here from `jobs`, inside the tenant scope, so a caller cannot record an
    event against a job of another tenant: the lookup simply finds nothing and the row is
    refused rather than silently written with a null job.

    `seq`, `prev_hash` and `hash` are NOT parameters. They are assigned by the BEFORE
    INSERT trigger (`MOS-SEC-151`).
    """
    if action_class not in ACTION_CLASSES:
        raise ValueError(
            f"action_class {action_class!r} is not one of {sorted(ACTION_CLASSES)}"
        )
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome {outcome!r} is not one of {sorted(OUTCOMES)}")
    if pep not in PEP_IDS:
        raise ValueError(
            f"pep {pep!r} is not one of the eight PEP ids (MOS-SEC-047): {sorted(PEP_IDS)}"
        )
    if source_ip is not None:
        # A malformed address would be caught by the `inet` column, but the error there
        # names the column and not the caller. MOS-SEC-146 also forbids inventing one.
        ipaddress.ip_address(source_ip)

    tenant_id = current_tenant()
    public_id = new_ulid("aud")

    with tenant_tx(conn):
        job_uuid: Any = None
        root_uuid: Any = None
        if job_id is not None:
            row = conn.execute(
                "SELECT id, root_job_id FROM jobs WHERE public_id = %s", (job_id,)
            ).fetchone()
            if row is None:
                raise LookupError(
                    f"no job {job_id} in this tenant; an audit row MUST NOT name a job "
                    "it cannot join to (MOS-SEC-155)"
                )
            job_uuid = row["id"]
            root_uuid = row["root_job_id"]

        conn.execute(
            """
            INSERT INTO audit_events (
                public_id, tenant_id, occurred_at,
                seq, prev_hash, hash,
                actor_kind, actor_id, actor_auth, actor_key_id,
                on_behalf_of_kind, on_behalf_of_id, on_behalf_of_role,
                action, action_class, resource_kind, resource_id, resource_version,
                outcome, policy_decision_id, break_glass_id, pep,
                job_id, job_public_id, root_job_id,
                trace_id, span_id, request_id,
                source_ip, user_agent, study_instance_uid, detail)
            VALUES (
                %(public_id)s, %(tenant)s, coalesce(%(occurred)s, now()),
                -- Placeholders. audit_events_chain_trg overwrites all three before the
                -- row lands (MOS-SEC-151); they are NOT NULL, so something must be sent.
                1, repeat('0', 64), repeat('0', 64),
                %(akind)s, %(aid)s, %(aauth)s, %(akey)s,
                %(okind)s, %(oid)s, %(orole)s,
                %(action)s, %(aclass)s, %(rkind)s, %(rid)s, %(rver)s,
                %(outcome)s, %(pdid)s, %(bgid)s, %(pep)s,
                %(job)s, %(jobpub)s, %(root)s,
                %(trace)s, %(span)s, %(req)s,
                %(ip)s, %(ua)s, %(study)s, %(detail)s)
            """,
            {
                "public_id": public_id,
                "tenant": tenant_id,
                "occurred": occurred_at,
                "akind": actor.kind,
                "aid": actor.id,
                "aauth": actor.auth,
                "akey": actor.key_id,
                "okind": on_behalf_of.kind if on_behalf_of else None,
                "oid": on_behalf_of.id if on_behalf_of else None,
                "orole": on_behalf_of.role if on_behalf_of else None,
                "action": action,
                "aclass": action_class,
                "rkind": resource.kind,
                "rid": resource.id,
                "rver": resource.version,
                "outcome": outcome,
                "pdid": policy_decision_id,
                "bgid": break_glass_id,
                "pep": pep,
                "job": job_uuid,
                "jobpub": job_id,
                "root": root_uuid,
                "trace": trace_id,
                "span": span_id,
                "req": request_id,
                "ip": source_ip,
                "ua": (user_agent or None) and user_agent[:200],
                "study": study_instance_uid,
                "detail": Jsonb(detail or {}),
            },
        )
    return public_id


# =====================================================================================
# READS -- MOS-SEC-155's joins, as queries rather than as a table in a document
# =====================================================================================
_TRAIL_COLUMNS = """
    public_id, seq, occurred_at, recorded_at,
    actor_kind, actor_id, actor_auth, actor_key_id,
    on_behalf_of_kind, on_behalf_of_id, on_behalf_of_role,
    action, action_class, resource_kind, resource_id, outcome, pep,
    job_public_id, trace_id, span_id, request_id, study_instance_uid, detail,
    prev_hash, hash
"""


def trail_for_job(conn: psycopg.Connection[Any], job_id: str) -> list[dict[str, Any]]:
    """`MOS-SEC-155`: `AuditEvent` -> `Job` by `job_id`, in chain order."""
    with tenant_tx(conn):
        return list(
            conn.execute(
                f"SELECT {_TRAIL_COLUMNS} FROM audit_events "
                "WHERE job_public_id = %s ORDER BY seq",
                (job_id,),
            ).fetchall()
        )


def trail_for_request(conn: psycopg.Connection[Any], request_id: str) -> list[dict[str, Any]]:
    """`MOS-SEC-150`: the authorization event and its effect event, as a pair."""
    with tenant_tx(conn):
        return list(
            conn.execute(
                f"SELECT {_TRAIL_COLUMNS} FROM audit_events "
                "WHERE request_id = %s ORDER BY seq",
                (request_id,),
            ).fetchall()
        )


def verify_chain(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str | None = None,
    from_seq: int = 1,
) -> dict[str, Any] | None:
    """`MOS-SEC-153`: recompute the chain and report the FIRST divergent `seq`, or None.

    Delegates to `audit_verify_chain()`, which recomputes each row's hash with
    `audit_row_hash()` -- the same function the insert trigger used. That identity is the
    point: a verifier with its own canonicaliser verifies that two implementations agree,
    not that the data is intact.

    Returns `None` when the range is intact, which makes this usable as
    `assert verify_chain(conn) is None` rather than as a report a human has to read.
    """
    tid = tenant_id or current_tenant()
    with tenant_tx(conn, tid):
        row = conn.execute(
            "SELECT seq, reason FROM audit_verify_chain(%s, %s)", (tid, from_seq)
        ).fetchone()
    return dict(row) if row is not None else None


def next_seq_preview(conn: psycopg.Connection[Any]) -> int:
    """The `seq` the next row of this tenant will get. Diagnostics and tests only."""
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT coalesce(max(seq), 0) + 1 AS n FROM audit_events WHERE tenant_id = %s",
            (current_tenant(),),
        ).fetchone()
    assert row is not None
    return int(row["n"])
