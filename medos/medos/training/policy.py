# SPDX-License-Identifier: Apache-2.0
"""`TrainingDataPolicy` and `tenants.training_use_allowed`: permission to train.

`MOS-TRAIN-072`: "Each `Tenant` MUST carry `training_use_allowed boolean NOT NULL DEFAULT
false`. When it is false, the harvest MUST refuse, with `403` and problem type
`training-use-not-permitted`. There is no per-study, per-user or per-environment override,
and no platform-administrator bypass."

This module is the flag that makes "the clinic gave me this data for my work" expressible
as a constraint the platform enforces rather than a thing someone remembers.

WHAT THE PLATFORM DOES AND DOES NOT DO WITH A LEGAL BASIS
---------------------------------------------------------
`MOS-TRAIN-074` is unusually precise about the posture, and it is the requirement most
easily violated by a helpful implementation: "The platform MUST NOT interpret, validate
against a registry, or assess the sufficiency of `legal_basis` or `basis_reference`. It
MUST store them, digest them, display them, refuse the harvest without them, and reproduce
them in every `DatasetVersion`'s provenance. This is the same posture `MOS-SAFE-029` takes
toward `regulatory_status`, for the same reason: the assertion is the site's, and a
platform that validates it has quietly assumed it."

So `record_policy` checks that `legal_basis` is a member of the closed enum and that
`basis_reference` is non-empty -- both of which are questions about the FORM of the
record -- and asks nothing about whether an ethics approval number is real, current, or
covers what the operator thinks it covers. `basis_document_digest` is a digest of an
instrument the platform holds; it is never verified against an issuer.

WHERE THE ENFORCEMENT ACTUALLY LIVES
------------------------------------
In the database, on the `MOS-EVID-013` pattern. Migration 0011 carries:

  * `tenants_training_policy_required` -- a DEFERRED constraint trigger, so the flag and
    its instrument can be written in one transaction and the flag cannot be true at
    commit without a live instrument (`MOS-TRAIN-073`);
  * `training_data_policies_revocation` -- an AFTER UPDATE trigger that writes
    `training_use_allowed = false` in the revoking transaction (`MOS-TRAIN-076`);
  * `training_data_policies_live_uk` -- one unrevoked instrument per tenant
    (`MOS-STORE-358`).

`assert_harvest_permitted` below is the earlier, better-worded failure. It is NOT the
guarantee, and it does not repeat the checks the triggers make; it adds the two the
triggers cannot make, because they are questions about a STUDY rather than about a row:
scope membership (`MOS-TRAIN-075`) and redistribution (`MOS-TRAIN-077`).

THE EXPIRY SWEEP
----------------
`MOS-TRAIN-075` requires expiry to "flip `training_use_allowed` to false automatically and
MUST emit `tenant.training_use.expired`". `MOS-STORE-358` says the mechanism is "the
ordinary tenant-iterating sweep of `MOS-STORE-333`", not a trigger: nothing writes a row
at the moment a timestamp passes. `sweep_expired_policies` is that sweep, written to be
called by a scheduler and to be idempotent. The event is returned rather than emitted --
chapter 8's event bus is not this module's to write to, and a sweep that emitted directly
would be a second producer of an audit-visible fact.

Spec: MOS-TRAIN-072, MOS-TRAIN-073, MOS-TRAIN-074, MOS-TRAIN-075, MOS-TRAIN-076,
MOS-TRAIN-077, MOS-STORE-333, MOS-STORE-358, MOS-SAFE-029.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from medos.db.tenancy import tenant_tx
from medos.sdk.canonical import canonical_bytes, sha256_hex
from medos.sdk.errors import HarvestRefused, Refusal

__all__ = [
    "LEGAL_BASES",
    "SCOPE_KEYS",
    "PolicyRow",
    "record_policy",
    "live_policy",
    "revoke_policy",
    "set_training_use_allowed",
    "training_use_allowed",
    "assert_harvest_permitted",
    "sweep_expired_policies",
    "basis_document_digest",
]

# `MOS-TRAIN-073`'s closed set, in its order. The platform has no opinion about which one
# a site asserts; it has an opinion about the set being closed, because an open one makes
# `MOS-TRAIN-081`'s reproduction of the basis in a report unreadable.
LEGAL_BASES: tuple[str, ...] = (
    "broad_consent",
    "research_ethics_approval",
    "public_corpus_licence",
    "data_processing_agreement",
    "national_derogation",
)

# Every key of `MOS-TRAIN-073`'s `scope` object, all five required. An absent key is not
# an open bound: it is a question nobody answered, and `MOS-TRAIN-075` refuses a harvest
# "for any study outside `scope`", which is unanswerable against a missing key. `date_to:
# null` is how open-ended is written, and the CHECK in migration 0011 enforces presence.
SCOPE_KEYS: tuple[str, ...] = (
    "modalities",
    "body_parts",
    "capabilities",
    "date_from",
    "date_to",
)


def _row(r: Any) -> dict[str, Any]:
    if r is None:
        raise LookupError("expected a row, got none")
    return dict(r) if isinstance(r, Mapping) else r


def basis_document_digest(document: bytes) -> str:
    """`sha256:<hex>` of a held instrument. `MOS-TRAIN-074`: store it, digest it.

    Deliberately over BYTES and not over a parsed structure: the instrument is a PDF or a
    scan and the platform is not entitled to an opinion about its contents.
    """
    return "sha256:" + sha256_hex(document)


@dataclass(frozen=True)
class PolicyRow:
    """A `TrainingDataPolicy` as stored. `MOS-TRAIN-073`'s field set, verbatim."""

    id: str
    tenant_id: str
    legal_basis: str
    basis_reference: str
    basis_document_digest: str | None
    scope: dict[str, Any]
    permits_redistribution: bool
    recorded_by: str
    recorded_at: _dt.datetime
    expires_at: _dt.datetime | None
    revoked_at: _dt.datetime | None
    revocation_reason: str | None

    @property
    def is_live(self) -> bool:
        """Unrevoked, and either open-ended or unexpired. `MOS-STORE-358`'s wording.

        Evaluated against the CALLER's clock, which is why the database trigger evaluates
        it again against `now()`. Two clocks disagreeing is not a hazard here: the trigger
        is the one that decides whether the transaction commits.
        """
        if self.revoked_at is not None:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > _dt.datetime.now(_dt.UTC)

    def provenance(self) -> dict[str, Any]:
        """What `MOS-TRAIN-074` requires reproduced "in every DatasetVersion's provenance".

        The digest of this object is stable under key order
        (`medos.sdk.canonical`), so a `ValidationReport` citing a cohort can
        cite the instrument by digest rather than by transcribing an approval number into a
        second place.
        """
        body = {
            "legal_basis": self.legal_basis,
            "basis_reference": self.basis_reference,
            "basis_document_digest": self.basis_document_digest,
            "scope": self.scope,
            "permits_redistribution": self.permits_redistribution,
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }
        return {
            "training_data_policy_id": self.id,
            **body,
            "digest": "sha256:" + sha256_hex(canonical_bytes(body)),
        }


def _to_policy(r: Mapping[str, Any]) -> PolicyRow:
    return PolicyRow(
        id=str(r["id"]),
        tenant_id=str(r["tenant_id"]),
        legal_basis=r["legal_basis"],
        basis_reference=r["basis_reference"],
        basis_document_digest=r["basis_document_digest"],
        scope=dict(r["scope"]),
        permits_redistribution=bool(r["permits_redistribution"]),
        recorded_by=str(r["recorded_by"]),
        recorded_at=r["recorded_at"],
        expires_at=r["expires_at"],
        revoked_at=r["revoked_at"],
        revocation_reason=r["revocation_reason"],
    )


def record_policy(
    conn: psycopg.Connection[Any],
    *,
    legal_basis: str,
    basis_reference: str,
    scope: Mapping[str, Any],
    recorded_by: str,
    permits_redistribution: bool = False,
    basis_document_digest: str | None = None,
    expires_at: _dt.datetime | None = None,
) -> PolicyRow:
    """Record the instrument. `MOS-TRAIN-073`.

    Does NOT set `training_use_allowed`: that is `set_training_use_allowed`, and the two
    are separate calls because they are separate decisions by (usually) separate people.
    Both may run in one transaction, which is what the DEFERRED constraint trigger of
    migration 0011 exists to permit.

    `permits_redistribution` defaults to false, the restrictive side of `MOS-TRAIN-077`. A
    default of true would make a `vendor_evidence` export possible for every tenant whose
    operator did not think about redistribution, which is the population the requirement
    is written for.
    """
    if legal_basis not in LEGAL_BASES:
        raise HarvestRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-073",
                    code="unknown_legal_basis",
                    message=f"legal_basis must be one of {LEGAL_BASES}",
                    observed=legal_basis,
                    bound=list(LEGAL_BASES),
                ),
            )
        )
    if not basis_reference.strip():
        raise HarvestRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-073",
                    code="missing_basis_reference",
                    message="basis_reference is the instrument -- approval number, "
                            "agreement id, licence SPDX id -- and MUST NOT be empty. "
                            "MOS-TRAIN-074: the platform stores it and never assesses it, "
                            "which only works if there is something to store.",
                    observed=basis_reference,
                ),
            )
        )
    missing = [k for k in SCOPE_KEYS if k not in scope]
    if missing:
        raise HarvestRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-073",
                    code="incomplete_scope",
                    message=(
                        f"scope is missing {missing}. An absent key is not an open "
                        "bound: MOS-TRAIN-075 refuses a harvest for any study outside "
                        "scope, and that question has no answer against a missing key. "
                        "Write `null` for an open bound."
                    ),
                    observed=sorted(scope),
                    bound=list(SCOPE_KEYS),
                ),
            )
        )
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO training_data_policies
              (tenant_id, legal_basis, basis_reference, basis_document_digest, scope,
               permits_redistribution, recorded_by, expires_at)
            VALUES (current_tenant_id(), %s, %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id, tenant_id, legal_basis, basis_reference, basis_document_digest,
                      scope, permits_redistribution, recorded_by, recorded_at,
                      expires_at, revoked_at, revocation_reason
            """,
            (
                legal_basis,
                basis_reference,
                basis_document_digest,
                canonical_bytes(dict(scope)).decode("utf-8"),
                permits_redistribution,
                recorded_by,
                expires_at,
            ),
        ).fetchone()
    return _to_policy(_row(row))


def live_policy(conn: psycopg.Connection[Any]) -> PolicyRow | None:
    """The tenant's live instrument, or None. `MOS-STORE-358`'s definition of live.

    `expires_at > now()` is evaluated in the database rather than in Python so that this
    function and the constraint trigger agree about the boundary case to the microsecond.
    """
    with tenant_tx(conn):
        row = conn.execute(
            """
            SELECT id, tenant_id, legal_basis, basis_reference, basis_document_digest,
                   scope, permits_redistribution, recorded_by, recorded_at,
                   expires_at, revoked_at, revocation_reason
              FROM training_data_policies
             WHERE tenant_id = current_tenant_id()
               AND revoked_at IS NULL
               AND (expires_at IS NULL OR expires_at > now())
            """
        ).fetchone()
    return _to_policy(_row(row)) if row is not None else None


def training_use_allowed(conn: psycopg.Connection[Any]) -> bool:
    """`tenants.training_use_allowed` for the bound tenant. `MOS-TRAIN-072`."""
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT training_use_allowed FROM tenants WHERE id = current_tenant_id()"
        ).fetchone()
    if row is None:
        return False
    r = _row(row)
    return bool(r["training_use_allowed"])


def set_training_use_allowed(conn: psycopg.Connection[Any], allowed: bool) -> None:
    """Write the flag. `MOS-TRAIN-072`, `MOS-TRAIN-073`.

    Setting it true with no live instrument does not fail here -- it fails at COMMIT, in
    `tenants_training_policy_required`, because that trigger is DEFERRABLE INITIALLY
    DEFERRED so that the flag and the instrument can be written in either order inside one
    transaction. The consequence for a caller is that the exception surfaces from the
    commit rather than from this call, which is the price of letting the two writes be one
    transaction, and it is worth naming here so the traceback is not a surprise.

    There is no `force`, no `reason_override` and no platform-administrator path.
    `MOS-TRAIN-072` forbids all three by name.
    """
    with tenant_tx(conn):
        conn.execute(
            "UPDATE tenants SET training_use_allowed = %s, updated_at = now() "
            "WHERE id = current_tenant_id()",
            (allowed,),
        )


def revoke_policy(
    conn: psycopg.Connection[Any], *, policy_id: str, reason: str
) -> PolicyRow:
    """Withdraw the instrument. `MOS-TRAIN-076`.

    "Revocation MUST NOT retroactively alter a sealed `DatasetVersion` -- `MOS-EVID-013`
    forbids editing sealed objects -- but it MUST block every new harvest and MUST be
    recorded on the tenant so that a subsequent `DatasetVersion` cannot be sealed from
    candidates acquired after it."

    Nothing here touches a sealed cohort, and nothing here deletes a candidate: what
    erasure means for imaging already committed to a sealed cohort is `OQ-16` and is not
    resolved. The flag is written back to false by the database trigger in the same
    transaction, so there is no window in which the tenant says yes and the instrument
    says no.
    """
    if not reason.strip():
        raise HarvestRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-076",
                    code="revocation_without_reason",
                    message="a withdrawal with no recorded reason is indistinguishable "
                            "from an accident",
                ),
            )
        )
    with tenant_tx(conn):
        row = conn.execute(
            """
            UPDATE training_data_policies
               SET revoked_at = now(), revocation_reason = %s
             WHERE tenant_id = current_tenant_id() AND id = %s AND revoked_at IS NULL
            RETURNING id, tenant_id, legal_basis, basis_reference, basis_document_digest,
                      scope, permits_redistribution, recorded_by, recorded_at,
                      expires_at, revoked_at, revocation_reason
            """,
            (reason, policy_id),
        ).fetchone()
    if row is None:
        raise LookupError(f"no live training_data_policies row {policy_id!r}")
    return _to_policy(_row(row))


def _in_scope(scope: Mapping[str, Any], study: Mapping[str, Any]) -> list[Refusal]:
    """`MOS-TRAIN-075`'s scope test, per dimension, so a refusal names the one that failed.

    A scope member that is `null` is an open bound and matches everything. A study field
    that is absent is NOT treated as matching: an unknown modality against a declared
    `["CT"]` is a study nobody established was in scope, and `MOS-TRAIN-075` refuses "any
    study outside `scope`" rather than any study known to be outside it.
    """
    out: list[Refusal] = []
    for key, study_key in (
        ("modalities", "modality"),
        ("body_parts", "body_part_examined"),
        ("capabilities", "capability_id"),
    ):
        declared = scope.get(key)
        if declared is None:
            continue
        value = study.get(study_key)
        if value is None or value not in declared:
            out.append(
                Refusal(
                    check_id="MOS-TRAIN-075",
                    code="study_outside_scope",
                    message=f"{study_key}={value!r} is outside the declared {key} of the "
                            "TrainingDataPolicy",
                    observed=value,
                    bound=list(declared),
                    detail={"dimension": key},
                )
            )
    study_date = study.get("study_date")
    for key, comparison in (("date_from", "<"), ("date_to", ">")):
        bound = scope.get(key)
        if bound is None:
            continue
        if study_date is None or (
            (str(study_date) < str(bound)) if comparison == "<" else
            (str(study_date) > str(bound))
        ):
            out.append(
                Refusal(
                    check_id="MOS-TRAIN-075",
                    code="study_outside_scope",
                    message=f"study_date={study_date!r} is outside the declared {key}",
                    observed=study_date,
                    bound=bound,
                    detail={"dimension": key},
                )
            )
    return out


def assert_harvest_permitted(
    conn: psycopg.Connection[Any],
    *,
    study: Mapping[str, Any] | None = None,
    for_vendor_evidence: bool = False,
) -> PolicyRow:
    """The gate every harvest passes. Raises `HarvestRefused` (403). `MOS-TRAIN-072/075/077`.

    Four questions, in the order that produces the most useful refusal first:

      1. is `training_use_allowed` true?                    `MOS-TRAIN-072`
      2. is there a live instrument behind it?              `MOS-TRAIN-073`
      3. is this study inside the declared scope?           `MOS-TRAIN-075`
      4. may the result be redistributed, if it must be?    `MOS-TRAIN-077`

    (1) and (2) cannot disagree at commit -- the constraint trigger sees to that -- but
    they CAN disagree in between, if this function is called inside a transaction that has
    already flipped the flag, so both are asked.

    `study` is optional because a harvest opens before any study is drawn; the batch-level
    call passes none and the per-candidate call passes one. That is not a loophole: a
    candidate drawn without the per-study check is a candidate `MOS-TRAIN-075` refuses, and
    `medos.training.curation.add_candidate` always passes one.
    """
    refusals: list[Refusal] = []
    if not training_use_allowed(conn):
        raise HarvestRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-072",
                    code="training_use_not_permitted",
                    message=(
                        "tenants.training_use_allowed is false. There is no per-study, "
                        "per-user or per-environment override and no "
                        "platform-administrator bypass: record a TrainingDataPolicy and "
                        "set the flag."
                    ),
                    observed=False,
                    bound=True,
                ),
            )
        )
    policy = live_policy(conn)
    if policy is None:
        raise HarvestRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-073",
                    code="no_live_training_data_policy",
                    message="training_use_allowed is true with no live "
                            "TrainingDataPolicy -- unrevoked and either open-ended or "
                            "unexpired. Setting the flag MUST require the instrument.",
                ),
            )
        )
    if study is not None:
        refusals.extend(_in_scope(policy.scope, study))
    if for_vendor_evidence and not policy.permits_redistribution:
        refusals.append(
            Refusal(
                check_id="MOS-TRAIN-077",
                code="redistribution_not_permitted",
                message=(
                    "permits_redistribution is false, which blocks this tenant's data "
                    "from any vendor_evidence export (MOS-EVID-011) and from use as a "
                    "golden fixture (MOS-TRAIN-061)."
                ),
                observed=False,
                bound=True,
            )
        )
    if refusals:
        raise HarvestRefused(tuple(refusals))
    return policy


def sweep_expired_policies(
    conn: psycopg.Connection[Any], *, tenant_ids: Sequence[str]
) -> list[dict[str, Any]]:
    """`MOS-TRAIN-075`'s expiry half, as the tenant-iterating sweep of `MOS-STORE-333`.

    Returns one `tenant.training_use.expired` event body per tenant whose flag this call
    turned off. Returns them rather than emitting them: chapter 8 owns the event bus, and
    a sweep that wrote to it directly would be a second producer of an audit-visible fact.
    The caller emits.

    Idempotent by construction -- the UPDATE is a no-op once the flag is already false --
    so a scheduler that double-fires produces one event and then none.

    Deliberately NOT a database trigger. Nothing writes a row at the moment a timestamp
    passes, so a trigger cannot fire; a view that computed the effective flag would be
    read by some call sites and not others. `MOS-STORE-358` names the sweep explicitly.
    """
    events: list[dict[str, Any]] = []
    for tenant_id in tenant_ids:
        with tenant_tx(conn, tenant_id):
            rows = conn.execute(
                """
                SELECT p.id, p.expires_at
                  FROM training_data_policies p
                 WHERE p.tenant_id = current_tenant_id()
                   AND p.revoked_at IS NULL
                   AND p.expires_at IS NOT NULL
                   AND p.expires_at <= now()
                """
            ).fetchall()
            if not rows:
                continue
            changed = conn.execute(
                """
                UPDATE tenants SET training_use_allowed = false, updated_at = now()
                 WHERE id = current_tenant_id() AND training_use_allowed
                RETURNING id
                """
            ).fetchone()
            if changed is None:
                continue
            expired = _row(rows[0])
            events.append(
                {
                    "event": "tenant.training_use.expired",
                    "tenant_id": tenant_id,
                    "training_data_policy_id": str(expired["id"]),
                    "expires_at": expired["expires_at"].isoformat(),
                    "requirement": "MOS-TRAIN-075",
                }
            )
    return events
