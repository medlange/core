# SPDX-License-Identifier: Apache-2.0
"""`AcceptanceCriteria` ownership, persistence and the tenant binding. Chapter 7 §7.8.

    MOS-EVID-075  The CLINICAL bar belongs to the `Capability` and is versioned with it.
                  The ENGINEERING bar -- p95 latency, GPU memory ceiling, ResultBundle
                  schema validity, throughput -- belongs to the ModelVersion/ServiceVersion
                  and is out of scope for the criteria document.
    MOS-REG-049   The same rule from the registry's side: "The registry MUST reject a
                  `service_version` that declares clinical acceptance thresholds in its own
                  manifest."
    MOS-REG-046   A capability MUST NOT leave `reserved` until it has an
                  `acceptance_criteria_ref`.
    MOS-REG-048   A change to any `measurements[].parameters` value MUST increment
                  `capability.revision` and MUST INVALIDATE the `acceptance_criteria_ref`
                  until a new EvaluationRun is recorded: changing an HU threshold changes
                  the clinical output and passes the same gate as a weights change.
    MOS-EVID-079  Authoring is validated against the grammar at WRITE time -- delegated to
                  `medos.evidence.criteria.validate_criteria`, which owns the grammar.
    MOS-EVID-080  A tenant MUST bind the Capability's criteria to a concrete cohort before
                  they can be evaluated; one row per (tenant, capability).
    MOS-EVID-081  `threshold_overrides` MAY ONLY TIGHTEN, compared in the direction of the
                  criterion's `op`.
    MOS-EVID-082  A DatasetVersion whose `Dataset.purpose` is `training` or `tuning` MUST
                  NOT be bound. Enforced in the database by 0008's
                  `forbid_training_cohort_binding()` trigger; this module lets the
                  exception surface rather than pre-checking it in a second place.
    MOS-EVID-087  A change to delta bumps `AcceptanceCriteria.version` and re-opens the
                  gate for every deployment relying on it.

WHY OWNERSHIP IS NOT A FILING PREFERENCE
-----------------------------------------
A bar carried by the artifact is a bar the artifact's publisher sets, so every new version
ships with the bar it happens to pass and the gate becomes a tautology. Carried by the
Capability, the bar outlives the thing being measured, which is the only arrangement in
which "did this version meet the bar" has a possible answer of no. `MOS-EVID-071`'s
refusal of a free-form `metrics` map on a version row is the same rule applied to the
answer rather than to the question.

WHAT "ONLY TIGHTEN" MEANS, AND THE SPELLING TRAP IN IT
------------------------------------------------------
Section 7.8.3 calls the map `threshold_overrides`, which reads as "operating thresholds".
`MOS-EVID-081` describes it as relaxing or tightening "any blocking criterion ... comparing
in the direction of the criterion's `op`", and chapter 7 acceptance check 17 settles it
outright: "Submit a `threshold_overrides` entry lowering `sens_all` from 0.90 to 0.85.
Expect rejection. Submit one raising it to 0.93. Expect acceptance." 0.90 is `sens_all`'s
criterion VALUE, not an operating threshold. The map is therefore keyed by CRITERION ID and
carries `value` and/or `margin`. Recorded here because the field name misleads.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import tenant_tx
from medos.evidence import criteria as criteria_mod

__all__ = [
    "OverrideRelaxes",
    "AcceptanceCriteriaRow",
    "TenantBindingRow",
    "FORBIDDEN_VERSION_FIELDS",
    "apply_threshold_overrides",
    "reject_clinical_acceptance_in_manifest",
    "publish_criteria",
    "load_criteria",
    "bind_tenant_acceptance",
    "load_binding",
    "effective_criteria",
]


class OverrideRelaxes(ValueError):
    """MOS-EVID-081: an override that loosens a blocking criterion. Rejected at write."""


# MOS-EVID-071: writable fields that must never appear on a version resource. A request
# carrying one is rejected with RFC 9457 `class: schema_violation`.
FORBIDDEN_VERSION_FIELDS = ("metrics", "performance", "accuracy", "dice", "sensitivity")

# Keys that would carry a clinical bar onto an artifact manifest. `engineering_acceptance`
# (§6.4) is deliberately absent: the engineering bar is exactly what the version IS
# allowed to declare (MOS-EVID-075).
_FORBIDDEN_MANIFEST_KEYS = (
    "acceptance_criteria",
    "clinical_acceptance",
    "acceptance_thresholds",
)


class AcceptanceCriteriaRow:
    """One `acceptance_criteria` row, with the document parsed out of `spec`."""

    __slots__ = ("id", "tenant_id", "capability_id", "version", "spec",
                 "margin_rationale", "approved_by", "approved_at", "authored_by")

    def __init__(self, **kw: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, kw.get(name))

    @property
    def document(self) -> dict[str, Any]:
        """The document in the shape `medos.evidence.criteria` and the gate consume."""
        return {
            "apiVersion": "medicalos.io/v1",
            "kind": "AcceptanceCriteria",
            "metadata": {
                "capability_id": self.capability_id,
                "version": self.version,
                "authored_by": self.authored_by,
                "margin_rationale": self.margin_rationale,
            },
            "spec": deepcopy(self.spec),
        }

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<AcceptanceCriteria {self.capability_id} v{self.version}>"


class TenantBindingRow:
    """One `tenant_acceptance_bindings` row. MOS-EVID-080."""

    __slots__ = ("tenant_id", "capability_id", "criteria_version", "dataset_version_id",
                 "split_id", "partition", "annotation_set_id", "threshold_overrides",
                 "bound_by", "bound_at")

    def __init__(self, **kw: Any) -> None:
        for name in self.__slots__:
            setattr(self, name, kw.get(name))


# =====================================================================================
# MOS-EVID-081.  Pure; no database.
# =====================================================================================
def _tightens(op: str, old: float, new: float) -> bool:
    """Is `new` at least as strict as `old`, in the direction of `op`?"""
    if op in (">=", ">"):
        return new >= old
    if op in ("<=", "<"):
        return new <= old
    raise OverrideRelaxes(f"cannot compare an override in the direction of op {op!r}")


def apply_threshold_overrides(
    document: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """Return `document` with the tenant's per-criterion overrides applied.

    Raises `OverrideRelaxes` on any override that loosens a BLOCKING criterion. Advisory
    criteria are exempt because they never gate (MOS-EVID-084), so loosening one cannot
    weaken the gate -- and refusing it anyway would be a rule with no safety content.

    A SMALLER `margin` is a tightening for both criterion shapes: a non-inferiority test
    tolerates less loss, and a `catastrophic_count` counts more cases as catastrophic.
    """
    out = deepcopy(dict(document))
    if not overrides:
        return out

    spec = out.setdefault("spec", {})
    by_id: dict[str, dict[str, Any]] = {}
    for kind in ("absolute", "regression"):
        for criterion in spec.get(kind, []):
            if isinstance(criterion, dict) and isinstance(criterion.get("id"), str):
                by_id[criterion["id"]] = criterion

    unknown = sorted(set(overrides) - set(by_id))
    if unknown:
        # Not ignored. A tenant who overrode `sens_all` in a version that renamed the
        # criterion would otherwise believe a tightening is in force that is not.
        raise OverrideRelaxes(
            f"threshold_overrides names criterion id(s) {unknown} that this "
            f"AcceptanceCriteria version does not declare"
        )

    for cid, raw in overrides.items():
        if not isinstance(raw, Mapping):
            raise OverrideRelaxes(f"override for {cid!r} MUST be an object")
        extra = sorted(set(raw) - {"value", "margin"})
        if extra:
            raise OverrideRelaxes(
                f"override for {cid!r} carries {extra}; only `value` and `margin` may be "
                f"overridden, and only in the tightening direction (MOS-EVID-081)"
            )
        criterion = by_id[cid]
        blocking = criterion.get("severity", "blocking") == "blocking"

        if "value" in raw:
            new = float(raw["value"])
            old = criterion.get("value")
            op = criterion.get("op")
            if not isinstance(old, (int, float)) or not isinstance(op, str):
                raise OverrideRelaxes(
                    f"override for {cid!r} sets `value`, but that criterion carries no "
                    f"`op`/`value` pair to compare against"
                )
            if blocking and not _tightens(op, float(old), new):
                raise OverrideRelaxes(
                    f"override for blocking criterion {cid!r} moves `value` {old} -> {new} "
                    f"against op {op!r}, which RELAXES the Capability's bar; a tenant "
                    f"binding may only tighten (MOS-EVID-081)"
                )
            criterion["value"] = new

        if "margin" in raw:
            new = float(raw["margin"])
            old = criterion.get("margin")
            if not isinstance(old, (int, float)):
                raise OverrideRelaxes(
                    f"override for {cid!r} sets `margin`, which is a regression-criterion "
                    f"field and this criterion has none"
                )
            if new <= 0.0:
                raise OverrideRelaxes(f"override for {cid!r}: `margin` MUST be > 0")
            if blocking and new > float(old):
                raise OverrideRelaxes(
                    f"override for blocking criterion {cid!r} widens `margin` {old} -> "
                    f"{new}, which tolerates MORE loss; a tenant binding may only tighten "
                    f"(MOS-EVID-081)"
                )
            criterion["margin"] = new

    return out


def reject_clinical_acceptance_in_manifest(manifest: Mapping[str, Any]) -> None:
    """MOS-REG-049 and MOS-EVID-071, as an executable admission check.

    Raises `criteria.CriteriaError` naming every offending path, so the caller can return
    one RFC 9457 `class: schema_violation` problem listing all of them rather than one per
    round trip. `spec.engineering_acceptance` is explicitly NOT an offence.
    """
    offences: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, child in node.items():
                here = f"{path}.{key}" if path else str(key)
                if key == "engineering_acceptance":
                    continue  # MOS-EVID-075: the engineering bar belongs to the version.
                if key in FORBIDDEN_VERSION_FIELDS or key in _FORBIDDEN_MANIFEST_KEYS:
                    offences.append(here)
                    continue
                walk(child, here)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]")

    walk(manifest, "")
    if offences:
        raise criteria_mod.CriteriaError(
            "a ServiceVersion/ModelVersion manifest MUST NOT declare clinical acceptance "
            "thresholds or a performance figure -- the clinical bar belongs to the "
            "Capability (MOS-REG-049) and a published number exists only as a "
            "`capability_claims` row (MOS-EVID-071). Offending paths: "
            + ", ".join(offences)
        )


# =====================================================================================
# Persistence.
# =====================================================================================
def _row(cursor_row: Any) -> dict[str, Any]:
    return dict(cursor_row) if cursor_row is not None else {}


def publish_criteria(
    conn: psycopg.Connection[Any],
    *,
    document: Mapping[str, Any],
    authored_by: str,
    tenant_id: str | None = None,
    known_strata_fields: Sequence[str] | None = None,
    cohort_dataset_version_id: str | None = None,
    split_id: str | None = None,
    annotation_set_id: str | None = None,
) -> AcceptanceCriteriaRow:
    """Validate and insert one immutable `AcceptanceCriteria` version. MOS-EVID-076/079.

    `tenant_id = None` writes a PLATFORM-GLOBAL row (MOS-STORE-220), which is what a
    Capability's criteria are in 0.1-0.2: `medos.capabilities.REGISTRY` is platform-wide,
    so its bar is too. That path is NOT reachable as `medicalos_app`, by design: 0008's
    dual-scope policy admits `tenant_id IS NULL` on read and never on write, so the
    application can read the platform catalogue and can never create a row in it. Seeding
    one is an owner/migrator act, and 0.2.0 ships no API surface for it -- REPORTED,
    because MOS-REG-046 makes `acceptance_criteria_ref` a precondition for a Capability
    leaving `reserved` and the registry that would own that seeding lands in 0.3.0.

    The document is validated BEFORE insert and stored verbatim. There is no update path:
    MOS-EVID-087 says a change to delta bumps the version, and the 0008 seal trigger makes
    that structural rather than advisory.
    """
    criteria_mod.validate_criteria(dict(document), known_strata_fields=known_strata_fields)

    metadata = document.get("metadata", {})
    spec = document.get("spec", {})
    capability_id = metadata.get("capability_id")
    version = metadata.get("version")
    if not isinstance(capability_id, str) or not capability_id:
        raise criteria_mod.CriteriaError(
            "metadata.capability_id is required: the criteria belong to the Capability "
            "(MOS-EVID-075, MOS-REG-049)"
        )
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise criteria_mod.CriteriaError("metadata.version MUST be an integer >= 1")

    rationale = spec.get("margin_rationale") or metadata.get("margin_rationale")

    sql = """
        INSERT INTO acceptance_criteria
            (tenant_id, capability_id, version, supersedes, spec, margin_rationale,
             cohort_dataset_version_id, split_id, annotation_set_id,
             effective_from, authored_by)
        VALUES (%(tenant_id)s, %(capability_id)s, %(version)s, %(supersedes)s,
                %(spec)s, %(margin_rationale)s, %(dsv)s, %(split)s, %(ann)s,
                %(effective_from)s, %(authored_by)s)
        RETURNING id, tenant_id, capability_id, version, spec, margin_rationale,
                  approved_by, approved_at, authored_by
    """
    params = {
        "tenant_id": tenant_id,
        "capability_id": capability_id,
        "version": version,
        "supersedes": metadata.get("supersedes"),
        "spec": Jsonb(dict(spec)),
        "margin_rationale": rationale,
        "dsv": cohort_dataset_version_id,
        "split": split_id,
        "ann": annotation_set_id,
        "effective_from": metadata.get("effective_from"),
        "authored_by": authored_by,
    }
    if tenant_id is None:
        # A platform-global row is written outside `tenant_tx`: `current_tenant_id()` is
        # irrelevant to it and binding a tenant would make the WITH CHECK policy refuse.
        with conn.transaction():
            row = conn.execute(sql, params).fetchone()
    else:
        with tenant_tx(conn, tenant_id) as tx:
            row = tx.execute(sql, params).fetchone()
    return AcceptanceCriteriaRow(**_row(row))


def load_criteria(
    conn: psycopg.Connection[Any], *, capability_id: str, version: int
) -> AcceptanceCriteriaRow | None:
    """The Capability's criteria at `version`. Tenant-owned rows shadow platform-global.

    `ORDER BY tenant_id IS NULL` puts the tenant's own row first, so a site that has
    published its own stricter version of a capability's criteria gets it. Nothing in
    chapter 7 requires that shadowing and nothing forbids it; it falls out of
    MOS-STORE-220's dual scope, and the alternative -- an ambiguous two-row result -- is
    not a better answer.
    """
    row = conn.execute(
        """
        SELECT id, tenant_id, capability_id, version, spec, margin_rationale,
               approved_by, approved_at, authored_by
          FROM acceptance_criteria
         WHERE capability_id = %s AND version = %s
         ORDER BY tenant_id IS NULL
         LIMIT 1
        """,
        (capability_id, version),
    ).fetchone()
    return AcceptanceCriteriaRow(**_row(row)) if row is not None else None


def bind_tenant_acceptance(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    capability_id: str,
    criteria_version: int,
    dataset_version_id: str,
    split_id: str,
    annotation_set_id: str,
    bound_by: str,
    partition: str = "test",
    threshold_overrides: Mapping[str, Any] | None = None,
) -> TenantBindingRow:
    """Bind, or rebind, the tenant's cohort for a capability. MOS-EVID-080.

    Upserts, because MOS-STORE-297a makes this the one deliberately mutable evidence row:
    "a site rebinds as its cohort grows". `threshold_overrides` is validated against the
    bound criteria version BEFORE the write (MOS-EVID-081, "rejected at write time"), and
    MOS-EVID-082's training-cohort refusal is the database's, not a second copy here.
    """
    overrides = dict(threshold_overrides or {})
    if overrides:
        row = load_criteria(conn, capability_id=capability_id, version=criteria_version)
        if row is None:
            raise criteria_mod.CriteriaError(
                f"no AcceptanceCriteria version {criteria_version} for capability "
                f"{capability_id!r}; a binding cannot override criteria that do not exist"
            )
        apply_threshold_overrides(row.document, overrides)  # raises OverrideRelaxes

    with tenant_tx(conn, tenant_id) as tx:
        result = tx.execute(
            """
            INSERT INTO tenant_acceptance_bindings
                (tenant_id, capability_id, criteria_version, dataset_version_id,
                 split_id, partition, annotation_set_id, threshold_overrides, bound_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, capability_id) DO UPDATE SET
                criteria_version    = EXCLUDED.criteria_version,
                dataset_version_id  = EXCLUDED.dataset_version_id,
                split_id            = EXCLUDED.split_id,
                partition           = EXCLUDED.partition,
                annotation_set_id   = EXCLUDED.annotation_set_id,
                threshold_overrides = EXCLUDED.threshold_overrides,
                bound_by            = EXCLUDED.bound_by,
                bound_at            = now()
            RETURNING tenant_id, capability_id, criteria_version, dataset_version_id,
                      split_id, partition, annotation_set_id, threshold_overrides,
                      bound_by, bound_at
            """,
            (
                _uuid.UUID(str(tenant_id)),
                capability_id,
                criteria_version,
                _uuid.UUID(str(dataset_version_id)),
                _uuid.UUID(str(split_id)),
                partition,
                _uuid.UUID(str(annotation_set_id)),
                Jsonb(overrides),
                _uuid.UUID(str(bound_by)),
            ),
        ).fetchone()
    return TenantBindingRow(**_row(result))


def load_binding(
    conn: psycopg.Connection[Any], *, tenant_id: str, capability_id: str
) -> TenantBindingRow | None:
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            """
            SELECT tenant_id, capability_id, criteria_version, dataset_version_id,
                   split_id, partition, annotation_set_id, threshold_overrides,
                   bound_by, bound_at
              FROM tenant_acceptance_bindings
             WHERE tenant_id = %s AND capability_id = %s
            """,
            (_uuid.UUID(str(tenant_id)), capability_id),
        ).fetchone()
    return TenantBindingRow(**_row(row)) if row is not None else None


def effective_criteria(
    conn: psycopg.Connection[Any], *, tenant_id: str, capability_id: str
) -> tuple[dict[str, Any], TenantBindingRow]:
    """The criteria document the gate evaluates, with the tenant's tightenings applied.

    Raises `criteria.CriteriaError` when the capability has no binding: MOS-EVID-080 says
    a tenant "MUST bind them to a concrete cohort before they can be evaluated", so an
    unbound capability has no gate to run rather than a default one to fall back on.
    """
    binding = load_binding(conn, tenant_id=tenant_id, capability_id=capability_id)
    if binding is None:
        raise criteria_mod.CriteriaError(
            f"capability {capability_id!r} has no tenant_acceptance_bindings row; the "
            f"Capability's criteria MUST be bound to a concrete cohort before they can be "
            f"evaluated (MOS-EVID-080)"
        )
    row = load_criteria(
        conn, capability_id=capability_id, version=int(binding.criteria_version)
    )
    if row is None:
        raise criteria_mod.CriteriaError(
            f"binding for {capability_id!r} names AcceptanceCriteria version "
            f"{binding.criteria_version}, which does not exist"
        )
    return apply_threshold_overrides(row.document, binding.threshold_overrides or {}), binding
