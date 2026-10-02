# SPDX-License-Identifier: Apache-2.0
"""Building a `Snapshot` from the registry. The IMPURE half, deliberately in its own file.

`MOS-REG-011` / `MOS-REG-012`: the resolver is called with a snapshot, never with a live
database handle, and a recorded `(epoch, snapshot_id)` must be replayable. Every database
read, every clock read and every adapter to another package lives here; `resolve.py`
imports none of it. That split is what the purity test asserts, and it is why "the
resolver does not touch the database" is a structural fact rather than a convention.

WHERE EACH MEMBER COMES FROM IN THIS BUILD, AND THE THREE THAT HAVE NO TABLE
-----------------------------------------------------------------------------
    services / models / preproc   `artifacts` (0012, `MOS-REG-013`), manifest `spec`
                                  projected into `MOS-REG-023`'s index columns
    deployments                   `deployments` (0008, §6.8)
    validation_reports            `validation_reports` (0009, ch. 7)
    metrics                       `evaluation_runs.aggregate_metrics` (0007)
    acceptance_datasets           `tenant_acceptance_bindings` (0008)
    envelopes                     `applicability_envelopes` (0010), evaluated by
                                  `medos.safety.envelope` through the adapter below
    epoch                         `registry_changelog` / the `registry_epoch` sequence
                                  (0012, `MOS-REG-010`)

Three members have NO table in this deployment and are therefore parameters with honest
defaults, each reported rather than faked:

  * `capabilities` -- §6.6's `capability` table (ch. 12 §12.9.1's `capabilities` +
    `capability_concepts`) does not exist. The vocabulary is taken from
    `acceptance_criteria.capability_id` and from the set THIS DEPLOYMENT serves
    (`medos.capabilities.providers.capability_ids()`), and a caller may pass its own.
    Consequence, stated: `MOS-REG-046`'s `reserved`/`deprecated` statuses cannot be read
    from anywhere, so every known capability loads as `supported`, and `F1` can only
    produce `capability_not_claimed` / `capability_unknown` until the table exists.
    The deployment resolver and not `medos.capabilities.REGISTRY`: a capability a
    deployment configures is one the API admits and the worker runs, and a vocabulary
    that omitted it made `F1` answer `capability_unknown` for work already in flight.
  * `tenant_pins` -- ch. 12's `capability_pins` does not exist either, so rank key P2 and
    the tenant half of the effective pin are only exercised by an explicit argument.
  * `nodes` -- ch. 13's accelerator inventory does not exist; `F7` therefore sees an empty
    inventory and passes (interpretation I2 in `resolve.py`).

`AcceptanceCriteria.primary_metric` (`MOS-REG-058` P4) has no member in chapter 7's
`acceptance_criteria.spec` either -- the spec stores `absolute[]` clauses, each naming its
own metric. The first `absolute` clause's metric is read as the primary bar, which is the
only reading that does not invent a field, and the divergence is reported.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg

from medos.db.tenancy import tenant_tx
from medos.resolution.model import (
    Capability,
    DeploymentRow,
    GateConfig,
    LicenceGrant,
    MetricPoint,
    ModelVersionRow,
    NodeProfile,
    PreprocRow,
    ServiceVersionRow,
    Snapshot,
    TenantPin,
    TenantProfile,
    ValidationReportRow,
)

__all__ = [
    "EnvelopeAdapter",
    "load_snapshot",
    "registry_epoch",
    "service_row_from_artifact",
]


class EnvelopeAdapter:
    """`medos.safety.envelope` behind `model.EnvelopeLike`. The ONE envelope evaluator.

    `MOS-EVID-096`'s three zones are computed by `medos.safety.envelope.evaluate`, which
    `MOS-EVID-100` puts in the platform and beyond the service's reach. This class exists
    so that the pure resolver can consult that evaluation without importing the package
    (which reaches `requests` through `medos/medos/safety/__init__.py`) and without a second
    implementation of a clinically load-bearing rule.
    """

    __slots__ = ("_envelope",)

    def __init__(self, envelope: Any) -> None:
        self._envelope = envelope

    def verdict_zone(self, attributes: Mapping[str, Any]) -> str:
        from medos.safety.envelope import evaluate

        return str(evaluate(self._envelope, dict(attributes)).zone)


def registry_epoch(conn: psycopg.Connection[Any]) -> int:
    """`MOS-REG-010`'s monotonic epoch, as of this read."""
    with tenant_tx(conn):
        row = conn.execute(
            "SELECT coalesce(max(epoch), 0) AS epoch FROM registry_changelog"
        ).fetchone()
    return int(_value(row, "epoch", 0))


def service_row_from_artifact(row: Mapping[str, Any]) -> ServiceVersionRow:
    """`MOS-REG-023`'s derived index columns, projected from the stored manifest.

    The requirement calls them "derived index columns on every `service_version` row,
    computed from the manifest at publish time and used as hard filters by the resolver".
    `0012_artifacts.up.sql` stores the manifest and does not materialise them, so they are
    derived here, once, from the one stored document -- rather than copied into a second
    place that can disagree with the manifest it came from.
    """
    manifest = _as_dict(row.get("manifest"))
    spec = _as_dict(manifest.get("spec"))
    runtime = _as_dict(_as_dict(spec.get("compatibility")).get("runtime"))
    resources = _as_dict(spec.get("resources"))
    return ServiceVersionRow(
        id=str(row["public_id"]),
        family=str(row["family"]),
        version=str(row["version"]),
        lifecycle_status=str(row["lifecycle_status"]),
        content_digest=str(row["manifest_digest"]),
        tenant_id=str(row["tenant_id"]) if row.get("tenant_id") else None,
        capabilities=tuple(
            str(c.get("id", "")) for c in _as_list(spec.get("capabilities"))
        ),
        mode=str(spec.get("mode", "native")),
        modalities=tuple(str(m) for m in _as_list(spec.get("modalities"))),
        model_version_refs=tuple(
            str(m.get("ref", "")) for m in _as_list(spec.get("models"))
        ),
        preprocessing_spec_refs=tuple(
            str(p.get("ref", "")) for p in _as_list(spec.get("preprocessing_specs"))
        ),
        gpu_architectures=tuple(
            str(a) for a in _as_list(runtime.get("gpu_architectures"))
        ),
        gpu_required=bool(resources.get("gpu_required", False)),
        gpu_memory_mib=int(resources.get("gpu_memory_mib") or 0),
        legal_manufacturer_id=str(_as_dict(spec.get("legal_manufacturer")).get("id", "")),
        regulatory_jurisdictions=tuple(
            str(r.get("jurisdiction", ""))
            for r in _as_list(spec.get("regulatory_status"))
        ),
        image_digest=str(_as_dict(spec.get("image")).get("digest", "")),
    )


def load_snapshot(
    conn: psycopg.Connection[Any],
    *,
    tenant_id: str,
    environment: str,
    as_of: datetime | None = None,
    capabilities: Mapping[str, Capability] | None = None,
    tenant_pins: Sequence[TenantPin] = (),
    nodes: Sequence[NodeProfile] = (),
    licences: Sequence[LicenceGrant] = (),
    jurisdiction: str = "",
    gate_config: GateConfig | None = None,
) -> Snapshot:
    """One consistent read of the registry, content-addressed. `MOS-REG-011`.

    The whole read runs inside ONE `tenant_tx` transaction so that a publish landing
    mid-load cannot produce a snapshot that is half of one epoch and half of the next --
    a snapshot whose `snapshot_id` names a state that never existed is not replayable,
    and `MOS-REG-071` requires replay.

    `as_of` defaults to the wall clock HERE, which is the one place a clock belongs: the
    resolver reads `snapshot.as_of` and never a clock of its own (`MOS-REG-051`).
    """
    moment = as_of or datetime.now(UTC)
    with tenant_tx(conn):
        artifacts = conn.execute(
            """
            SELECT public_id, id::text AS uuid, kind, family, version,
                   lifecycle_status, tenant_id::text AS tenant_id,
                   manifest, manifest_digest
              FROM artifacts
             WHERE kind IN ('service','model','preprocessing')
            """
        ).fetchall()
        deployment_rows = conn.execute(
            """
            SELECT public_id, tenant_id::text AS tenant_id, environment, capability_id,
                   subject_id, subject_version, role, state, traffic_permille,
                   clinical_use_mode, pin_range
              FROM deployments
             WHERE tenant_id = %s AND environment = %s AND subject_kind = 'service_version'
            """,
            (tenant_id, environment),
        ).fetchall()
        report_rows = conn.execute(
            """
            SELECT public_id, tenant_id::text AS tenant_id, subject_id::text AS subject_id,
                   subject_version, capability_id, verdict, status
              FROM validation_reports
             WHERE subject_kind = 'service_version' AND tenant_id = %s
            """,
            (tenant_id,),
        ).fetchall()
        binding_rows = conn.execute(
            """
            SELECT capability_id, dataset_version_id::text AS dataset_version_id
              FROM tenant_acceptance_bindings WHERE tenant_id = %s
            """,
            (tenant_id,),
        ).fetchall()
        criteria_rows = conn.execute(
            "SELECT capability_id, version, spec FROM acceptance_criteria WHERE tenant_id = %s",
            (tenant_id,),
        ).fetchall()
        run_rows = conn.execute(
            """
            SELECT service_version_id::text AS service_version_id, capability_id,
                   dataset_version_id::text AS dataset_version_id,
                   aggregate_metrics, public_id
              FROM evaluation_runs
             WHERE tenant_id = %s AND state = 'SUCCEEDED' AND service_version_id IS NOT NULL
            """,
            (tenant_id,),
        ).fetchall()
        envelope_rows = conn.execute(
            """
            SELECT subject_id, subject_version, version, constraints,
                   marginal_policy_default, derivation
              FROM applicability_envelopes
             WHERE subject_kind = 'service_version'
            """
        ).fetchall()
        epoch_row = conn.execute(
            "SELECT coalesce(max(epoch), 0) AS epoch FROM registry_changelog"
        ).fetchone()

    services: list[ServiceVersionRow] = []
    models: dict[str, ModelVersionRow] = {}
    preproc: dict[str, PreprocRow] = {}
    public_by_uuid: dict[str, str] = {}
    for row in artifacts:
        data = _as_dict(row)
        public_by_uuid[str(data["uuid"])] = str(data["public_id"])
        kind = str(data["kind"])
        if kind == "service":
            services.append(service_row_from_artifact(data))
        elif kind == "model":
            spec = _as_dict(_as_dict(data.get("manifest")).get("spec"))
            models[str(data["public_id"])] = ModelVersionRow(
                id=str(data["public_id"]),
                family=str(data["family"]),
                version=str(data["version"]),
                lifecycle_status=str(data["lifecycle_status"]),
                preprocessing_spec_ref=str(spec.get("preprocessing_spec_ref", "")),
            )
        else:
            preproc[str(data["public_id"])] = PreprocRow(
                id=str(data["public_id"]),
                family=str(data["family"]),
                version=str(data["version"]),
                lifecycle_status=str(data["lifecycle_status"]),
            )

    deployments = tuple(
        DeploymentRow(
            id=str(_value(r, "public_id", "")),
            tenant_id=str(_value(r, "tenant_id", "")),
            environment=str(_value(r, "environment", "")),
            capability_id=str(_value(r, "capability_id", "")),
            subject_id=str(_value(r, "subject_id", "")),
            subject_version=str(_value(r, "subject_version", "")),
            role=str(_value(r, "role", "ACTIVE")),
            state=str(_value(r, "state", "PENDING")),
            traffic_permille=int(_value(r, "traffic_permille", 0) or 0),
            clinical_use_mode=str(_value(r, "clinical_use_mode", "research_only")),
            pin_range=str(_value(r, "pin_range", "") or ""),
        )
        for r in deployment_rows
    )

    reports = tuple(
        ValidationReportRow(
            id=str(_value(r, "public_id", "")),
            tenant_id=str(_value(r, "tenant_id", "")),
            subject_id=public_by_uuid.get(
                str(_value(r, "subject_id", "")), str(_value(r, "subject_id", ""))
            ),
            subject_version=str(_value(r, "subject_version", "")),
            capability_id=str(_value(r, "capability_id", "")),
            # Chapter 7 spells the verdict PASS/FAIL/INDETERMINATE; the resolver compares
            # one lowercase spelling. Folded here, at the boundary, not in the filter.
            verdict=str(_value(r, "verdict", "")).lower(),
            status=str(_value(r, "status", "")).lower(),
        )
        for r in report_rows
    )

    acceptance_datasets = {
        f"{tenant_id}|{_value(r, 'capability_id', '')}": str(
            _value(r, "dataset_version_id", "")
        )
        for r in binding_rows
    }

    primary_metrics: dict[str, str] = {}
    criteria_refs: dict[str, str] = {}
    for r in criteria_rows:
        cid = str(_value(r, "capability_id", ""))
        spec = _as_dict(_value(r, "spec", {}))
        absolute = _as_list(spec.get("absolute"))
        if absolute:
            primary_metrics[cid] = str(_as_dict(absolute[0]).get("metric", ""))
        criteria_refs[cid] = str(_value(r, "version", ""))

    metrics: dict[str, MetricPoint] = {}
    for r in run_rows:
        sv = public_by_uuid.get(str(_value(r, "service_version_id", "")))
        cid = str(_value(r, "capability_id", ""))
        dataset = str(_value(r, "dataset_version_id", ""))
        if not sv or not cid:
            continue
        wanted = primary_metrics.get(cid, "")
        for entry in _as_list(_value(r, "aggregate_metrics", [])):
            item = _as_dict(entry)
            if item.get("value") is None:
                continue
            if wanted and str(item.get("metric")) != wanted:
                continue
            if str(item.get("stratum", "overall")) not in ("overall", "all", ""):
                continue
            metrics[f"{sv}|{cid}|{dataset}"] = MetricPoint(
                value=float(item["value"]),
                metric=str(item.get("metric", "")),
                evaluation_run_id=str(_value(r, "public_id", "")),
            )
            break

    envelopes: dict[str, Any] = {}
    if envelope_rows:
        from medos.safety.envelope import ApplicabilityEnvelope

        for r in envelope_rows:
            data = _as_dict(r)
            declaration = ApplicabilityEnvelope.from_manifest(
                {
                    "subject_kind": "service_version",
                    "subject_id": str(data.get("subject_id", "")),
                    "subject_version": str(data.get("subject_version", "")),
                    "version": int(data.get("version") or 1),
                    "constraints": _as_list(data.get("constraints")),
                    "marginal_policy_default": str(
                        data.get("marginal_policy_default") or "flag"
                    ),
                    "derivation": str(data.get("derivation") or "declared"),
                }
            )
            envelopes[str(data.get("subject_id", ""))] = EnvelopeAdapter(declaration)

    vocabulary = dict(capabilities) if capabilities is not None else _capability_vocabulary(
        criteria_refs, primary_metrics
    )

    return Snapshot(
        epoch=int(_value(epoch_row, "epoch", 0)),
        as_of=moment,
        capabilities=vocabulary,
        services=tuple(sorted(services, key=lambda s: s.id)),
        models=models,
        preproc=preproc,
        deployments=deployments,
        tenant_pins=tuple(tenant_pins),
        nodes=tuple(nodes),
        metrics=metrics,
        licences=tuple(licences),
        validation_reports=reports,
        tenants={tenant_id: TenantProfile(tenant_id, jurisdiction)},
        acceptance_datasets=acceptance_datasets,
        envelopes=envelopes,
        gate_config=gate_config or GateConfig(),
    ).with_identity()


def _capability_vocabulary(
    criteria_refs: Mapping[str, str], primary_metrics: Mapping[str, str]
) -> dict[str, Capability]:
    """§6.6's vocabulary, assembled from what this build actually has. See the header.

    The UNION is what makes the second source matter, and it is also what made the defect
    narrow enough to survive review. A deployment-configured capability that happened to
    have an `acceptance_criteria` row was already in the vocabulary through
    `criteria_refs`; one without a row -- the ordinary case, since chapter 7's criteria are
    tenant configuration and a freshly enabled capability has none -- was not, because the
    other half of the union read the platform singleton `medos.capabilities.REGISTRY`.
    That capability could be submitted and executed and then resolved as
    `capability_unknown`, and §6.6 resolution is PINNED INTO THE JOB ROW at creation and
    replayed on every retry, so the wrong vocabulary is not a transient wrong answer.

    Read at LOAD time and frozen into the `Snapshot`, which is the only place a deployment
    read belongs: `MOS-REG-051` makes `resolve()` a pure function of
    `(capability, context, registry_snapshot)`, and this module is the impure half that
    exists so that it can be. The resolver never sees this call.
    """
    from medos.capabilities.providers import capability_ids

    ids = set(criteria_refs) | set(capability_ids())
    return {
        cid: Capability(
            id=cid,
            status="supported",
            primary_metric=primary_metrics.get(cid, ""),
            acceptance_criteria_ref=criteria_refs.get(cid, ""),
        )
        for cid in sorted(ids)
    }


# =====================================================================================
# Row helpers -- psycopg may hand back a dict_row or a tuple depending on the connection
# =====================================================================================
def _value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    return default  # pragma: no cover - the callers all use dict_row connections


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []
