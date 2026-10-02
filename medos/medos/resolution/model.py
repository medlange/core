# SPDX-License-Identifier: Apache-2.0
"""The three arguments of `Resolve()` and the `Outcome` it returns. `MOS-REG-051`.

WHAT THIS MODULE IS
-------------------
`MOS-REG-051` fixes the signature as `Resolve(req Request, snap Snapshot) Outcome` and
fixes the members of all three. This is that shape in Python, as frozen dataclasses: the
resolver reads them and nothing else, so "pure function of its arguments" is enforceable
by inspection rather than by promise.

WHY EVERYTHING IS FROZEN AND EVERY SEQUENCE IS A TUPLE
-------------------------------------------------------
`MOS-REG-012`: "`Resolve()` MUST be called with a snapshot, never with a live database
handle. Replaying a recorded `(epoch, snapshot_id)` and re-running `Resolve()` MUST yield
a byte-identical `Outcome`." A snapshot that a caller can mutate between two calls is not
a snapshot, and a list member is exactly that hole. `MOS-REG-053` then requires byte
identity across two runs of the same input, which `sorted()` over tuples gives and
`dict`/`set` iteration order does not promise across processes.

WHY THE ENVELOPE ARRIVES AS A PROTOCOL AND NOT AS A SECOND EVALUATOR
---------------------------------------------------------------------
Filter `F6` (`MOS-REG-055`) needs the `applicability_envelope` verdict for a study. That
evaluation already exists, once, in `medos/medos/safety/envelope.py` -- a pure function of
`(envelope, attributes)` that `MOS-EVID-100` puts in the platform and beyond the
service's reach. Re-implementing the three-zone logic here would be a second source of a
clinically load-bearing decision, which `MOS-EVID-096`..`101` and this platform's own
"one definition" rule both forbid.

It is also not importable from a module that must import nothing impure:
`import medos.safety.envelope` runs `medos/medos/safety/__init__.py`, which drags in
`medos.safety.marking` and with it `requests` -- so the resolver's purity check would
fail on a transitive HTTP client it never calls. The seam is therefore a STRUCTURAL
protocol: the snapshot carries objects exposing `verdict_zone(attributes) -> str`, and
`medos/medos/resolution/snapshot.py` -- the impure half -- is where `medos.safety.envelope` is
adapted to it. The resolver imports nothing and duplicates nothing.

Pure: stdlib plus `medos.sdk.canonical` (RFC 8785, `MOS-SEC-151`'s one
canonicaliser).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal, Protocol, runtime_checkable

from medos.sdk.canonical import canonical_json, digest_of

__all__ = [
    "ENVIRONMENTS",
    "LIFECYCLE_BLOCKED",
    "Candidate",
    "Capability",
    "DeploymentRow",
    "EnvelopeLike",
    "Exclusion",
    "GateConfig",
    "LicenceGrant",
    "MetricPoint",
    "ModelVersionRow",
    "NodeProfile",
    "Outcome",
    "PreprocRow",
    "RankKey",
    "Request",
    "ServiceVersionRow",
    "Snapshot",
    "StudyProfile",
    "TenantPin",
    "TenantProfile",
    "ValidationReportRow",
    "VersionPin",
    "iso8601",
]

Environment = Literal["dev", "staging", "production"]
ENVIRONMENTS: Final[tuple[str, ...]] = ("dev", "staging", "production")

# `MOS-REG-055` filter F3, verbatim: the `lifecycle_status` values that MUST NOT dispatch.
# Note what is NOT here: `DEPRECATED` still resolves (ranked last by P5, `MOS-REG-106`
# keeps it resolvable for 180 days so pinned jobs can be replayed), and no deployment
# state appears at all -- liveness is `Deployment`'s answer (`MOS-REG-038`/`MOS-REG-072`).
LIFECYCLE_BLOCKED: Final[frozenset[str]] = frozenset(
    {"SUSPENDED", "RECALLED", "DRAFT", "REGISTERED", "VALIDATING"}
)


def iso8601(value: datetime) -> str:
    """`2026-01-14T08:22:19Z`. The one spelling in the pinned record of `MOS-REG-066`."""
    text = value.isoformat()
    return text[:-6] + "Z" if text.endswith("+00:00") else text


# =====================================================================================
# The Request half
# =====================================================================================
@dataclass(frozen=True, slots=True)
class VersionPin:
    """`MOS-REG-051`'s `VersionPin`. An empty `service_family` means "any family"."""

    service_family: str = ""
    range: str = ""

    def as_document(self) -> dict[str, Any]:
        return {"service_family": self.service_family, "range": self.range}


@dataclass(frozen=True, slots=True)
class StudyProfile:
    """Chapter 3's triage output, reduced to what resolution reads.

    `study_id` is "the platform's internal study identifier, never the
    `StudyInstanceUID`" (`MOS-REG-059`) -- no PHI enters the canary hash, and none enters
    `inputs_hash` either. `constraints` are the nine study attributes an
    `ApplicabilityEnvelope` is evaluated against (`medos/medos/safety/attributes.py`).
    """

    study_id: str = ""
    constraints: Mapping[str, Any] = field(default_factory=dict)

    def as_document(self) -> dict[str, Any]:
        return {"study_id": self.study_id, "constraints": dict(self.constraints)}


@dataclass(frozen=True, slots=True)
class Request:
    """`MOS-REG-051`'s `Request`. `inputs_hash` is computed over `as_document()`."""

    capability_id: str
    tenant_id: str
    environment: str
    modality: str
    study: StudyProfile = field(default_factory=StudyProfile)
    job_pin: VersionPin | None = None

    def as_document(self) -> dict[str, Any]:
        """The RFC 8785 subject of `Outcome.inputs_hash` (`MOS-REG-051`, `MOS-REG-071`)."""
        return {
            "capability_id": self.capability_id,
            "environment": self.environment,
            "job_pin": self.job_pin.as_document() if self.job_pin else None,
            "modality": self.modality,
            "study": self.study.as_document(),
            "tenant_id": self.tenant_id,
        }

    def inputs_hash(self) -> str:
        return "sha256:" + digest_of(self.as_document())


# =====================================================================================
# The Snapshot half -- one frozen row type per registry table the filters read
# =====================================================================================
@dataclass(frozen=True, slots=True)
class Capability:
    """§6.6's row, reduced to the members `F1`, `F8` and `P4` read.

    `status` is `MOS-REG-046`'s gate: a `reserved` capability "is not resolvable and MUST
    be excluded by filter `F1`".
    """

    id: str
    status: str = "supported"  # supported | reserved | deprecated
    output_kind: str = "measurement"
    primary_metric: str = ""
    acceptance_criteria_ref: str = ""
    revision: int = 1


@dataclass(frozen=True, slots=True)
class ServiceVersionRow:
    """`MOS-REG-023`'s derived index columns -- "used as hard filters by the resolver"."""

    id: str
    family: str
    version: str
    lifecycle_status: str
    content_digest: str
    tenant_id: str | None = None  # None = platform catalogue, visible to every tenant
    capabilities: tuple[str, ...] = ()
    mode: str = "native"  # native | sealed (ch. 2)
    modalities: tuple[str, ...] = ()
    model_version_refs: tuple[str, ...] = ()
    preprocessing_spec_refs: tuple[str, ...] = ()
    gpu_architectures: tuple[str, ...] = ()
    gpu_required: bool = False
    gpu_memory_mib: int = 0
    legal_manufacturer_id: str = ""
    regulatory_jurisdictions: tuple[str, ...] = ()
    image_digest: str = ""


@dataclass(frozen=True, slots=True)
class ModelVersionRow:
    id: str
    family: str
    version: str
    lifecycle_status: str
    preprocessing_spec_ref: str = ""


@dataclass(frozen=True, slots=True)
class PreprocRow:
    id: str
    family: str
    version: str
    lifecycle_status: str


@dataclass(frozen=True, slots=True)
class DeploymentRow:
    """§6.8's row. `role` and `state` are orthogonal (`MOS-REG-073`) and both are read."""

    id: str
    tenant_id: str
    environment: str
    capability_id: str
    subject_id: str  # the `service_version` public id
    subject_version: str = ""
    role: str = "ACTIVE"  # ACTIVE | CANARY | SHADOW | STANDBY
    state: str = "SERVING"  # PENDING | VERIFYING | SERVING | SUSPENDED | DRAINING | RETIRED
    traffic_permille: int = 0
    clinical_use_mode: str = "research_only"
    pin_range: str = ""


@dataclass(frozen=True, slots=True)
class TenantPin:
    """Chapter 12's `capability_pins` row -- "Chapter 6's `TenantPin`, read by rank key P2"."""

    tenant_id: str
    capability_id: str
    service_family: str
    version_range: str = ""
    environment: str = ""  # "" = every environment


@dataclass(frozen=True, slots=True)
class NodeProfile:
    """Chapter 13's accelerator inventory, per environment. Read by `F7`."""

    id: str
    environment: str
    gpu_architectures: tuple[str, ...] = ()
    gpu_memory_mib: int = 0
    driver_version: str = ""
    cuda_version: str = ""


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One `(service_version, capability, dataset_version)` metric value. Read by `P4`."""

    value: float
    metric: str = ""
    evaluation_run_id: str = ""


@dataclass(frozen=True, slots=True)
class LicenceGrant:
    """`F2`'s grant. `expires_at` is compared against `snap.as_of`, never against a clock."""

    tenant_id: str
    service_family: str
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ValidationReportRow:
    """Chapter 7's report, reduced to what the (gateable) `F8` reads."""

    id: str
    tenant_id: str
    subject_id: str
    subject_version: str
    capability_id: str
    verdict: str = "pass"
    status: str = "active"


@dataclass(frozen=True, slots=True)
class TenantProfile:
    """The tenant facts resolution reads: its regulatory jurisdiction (`F9`)."""

    tenant_id: str
    jurisdiction: str = ""


@dataclass(frozen=True, slots=True)
class GateConfig:
    """`MOS-REG-005`: the gateable filters are code that is CONFIGURED off, never absent.

    "the gates that a release has not yet built (evidence gate `F8`, regulatory gate
    `F9`) MUST be implemented as explicit `Exclusion` stages that are configured **off**
    per environment, and their configuration state MUST appear in the pinned resolution
    record as `gate_config`. They MUST NOT be absent from the code."
    """

    evidence_gate: bool = False
    regulatory_gate: bool = False

    def as_document(self) -> dict[str, bool]:
        return {
            "evidence_gate": self.evidence_gate,
            "regulatory_gate": self.regulatory_gate,
        }


@runtime_checkable
class EnvelopeLike(Protocol):
    """The `F6` seam. Implemented by `medos.safety.envelope` through a two-line adapter."""

    def verdict_zone(self, attributes: Mapping[str, Any]) -> str:
        """`"IN" | "MARGINAL" | "OUT"` -- `MOS-EVID-096`'s three zones."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """`MOS-REG-051`'s `Snapshot`, plus the four members this build's tables require.

    ADDED MEMBERS, AND WHY EACH IS NOT AN INVENTION
    ------------------------------------------------
    `validation_reports`  `F8` is defined as "a `ValidationReport` exists meeting the
                          Capability's `AcceptanceCriteria`"; chapter 7 stores the
                          verdict on the report row, so the row is what the filter reads.
    `tenants`             `F9` is defined against "the tenant's jurisdiction"; the
                          jurisdiction has to arrive in the snapshot because the resolver
                          may not look it up.
    `acceptance_datasets` `P4` is the metric "on the **tenant's** acceptance
                          `DatasetVersion`" (`tenant_acceptance_bindings`, ch. 12); the
                          binding is the third component of the `Metrics` key
                          `MOS-REG-051` already specifies.
    `envelopes`           `F6`'s evaluator, per the module docstring.

    `as_of` is "the only time source the resolver may read" (`MOS-REG-051`).
    """

    snapshot_id: str = ""
    epoch: int = 0
    as_of: datetime | None = None
    capabilities: Mapping[str, Capability] = field(default_factory=dict)
    services: tuple[ServiceVersionRow, ...] = ()
    models: Mapping[str, ModelVersionRow] = field(default_factory=dict)
    preproc: Mapping[str, PreprocRow] = field(default_factory=dict)
    deployments: tuple[DeploymentRow, ...] = ()
    tenant_pins: tuple[TenantPin, ...] = ()
    nodes: tuple[NodeProfile, ...] = ()
    metrics: Mapping[str, MetricPoint] = field(default_factory=dict)
    licences: tuple[LicenceGrant, ...] = ()
    validation_reports: tuple[ValidationReportRow, ...] = ()
    tenants: Mapping[str, TenantProfile] = field(default_factory=dict)
    acceptance_datasets: Mapping[str, str] = field(default_factory=dict)
    envelopes: Mapping[str, EnvelopeLike] = field(default_factory=dict)
    gate_config: GateConfig = field(default_factory=GateConfig)

    # -- identity ---------------------------------------------------------------------
    def content_document(self) -> dict[str, Any]:
        """Everything the resolver can read, in a canonicalisable form.

        `MOS-REG-011`/`MOS-REG-012`: the snapshot id is a content address, so two
        processes that rebuilt the same registry state from `registry_changelog` agree on
        the id without coordinating. `envelopes` contributes its KEYS only -- the
        evaluator is code reached through a protocol, and the declaration it wraps is
        already content-addressed by chapter 7's `envelope_digest`.
        """
        return {
            "acceptance_datasets": dict(sorted(self.acceptance_datasets.items())),
            "as_of": iso8601(self.as_of) if self.as_of else None,
            "capabilities": [
                _as_document(self.capabilities[k]) for k in sorted(self.capabilities)
            ],
            "deployments": [_as_document(d) for d in self.deployments],
            "envelope_subjects": sorted(self.envelopes),
            "epoch": self.epoch,
            "gate_config": self.gate_config.as_document(),
            "licences": [_as_document(x) for x in self.licences],
            "metrics": {k: _as_document(self.metrics[k]) for k in sorted(self.metrics)},
            "models": [_as_document(self.models[k]) for k in sorted(self.models)],
            "nodes": [_as_document(n) for n in self.nodes],
            "preproc": [_as_document(self.preproc[k]) for k in sorted(self.preproc)],
            "services": [_as_document(s) for s in self.services],
            "tenant_pins": [_as_document(p) for p in self.tenant_pins],
            "tenants": [_as_document(self.tenants[k]) for k in sorted(self.tenants)],
            "validation_reports": [_as_document(v) for v in self.validation_reports],
        }

    def computed_id(self) -> str:
        return "sha256:" + digest_of(self.content_document())

    def with_identity(self) -> Snapshot:
        """The same snapshot carrying its content address. Used by the loader and tests."""
        from dataclasses import replace

        return replace(self, snapshot_id=self.computed_id())


def _as_document(row: Any) -> dict[str, Any]:
    """A frozen row as a JSON document, with datetimes in the one spelling."""
    out: dict[str, Any] = {}
    for name in row.__dataclass_fields__:
        value = getattr(row, name)
        if isinstance(value, datetime):
            value = iso8601(value)
        elif isinstance(value, tuple):
            value = list(value)
        out[name] = value
    return out


# =====================================================================================
# The Outcome half
# =====================================================================================
@dataclass(frozen=True, slots=True)
class RankKey:
    """`MOS-REG-058`'s tuple, recorded per candidate so a ranking can be audited.

    `acceptance_metric_present` is `MOS-REG-060`, which is not decoration: "A missing
    acceptance metric (P4) MUST rank `-Inf`, never `0` and never 'pass' ... and
    `Outcome.Ranked[i].RankKey` MUST record `acceptance_metric_present: false` so the
    pinned record shows the metric was absent."
    """

    job_pin_family_match: int
    tenant_pin_family_match: int
    deployment_rank: int
    acceptance_metric: float
    acceptance_metric_present: bool
    deprecated: int
    version: str
    content_digest: str
    canary_bucket: int

    def as_string(self) -> str:
        """§6.7.5's `"0|0|1|0.881|0|3.1.4"` -- P1..P6, in order.

        P7 (`content_digest`) is the final tie-break and is carried structurally rather
        than in this string, because §6.7.5's own example does not print it. The string
        is a display of the ranking, never its definition.
        """
        metric = (
            format(self.acceptance_metric, ".6g")
            if self.acceptance_metric_present
            else "-Inf"
        )
        return (
            f"{self.job_pin_family_match}|{self.tenant_pin_family_match}|"
            f"{self.deployment_rank}|{metric}|{self.deprecated}|{self.version}"
        )

    def as_document(self) -> dict[str, Any]:
        doc = _as_document(self)
        # `-inf` is not a JSON number. The canonicaliser refuses a non-finite float
        # (MOS-SEC-151 has one serialisation), so the absent metric is recorded as the
        # null it is, with `acceptance_metric_present` carrying the claim.
        if not self.acceptance_metric_present:
            doc["acceptance_metric"] = None
        doc["rank_key"] = self.as_string()
        return doc


@dataclass(frozen=True, slots=True)
class Candidate:
    """`MOS-REG-051`'s `Candidate`, plus `MOS-REG-115`'s deployment members."""

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
    content_digest: str
    rank_key: RankKey

    def as_document(self) -> dict[str, Any]:
        return {
            "clinical_use_mode": self.clinical_use_mode,
            "content_digest": self.content_digest,
            "deployment_environment": self.deployment_environment,
            "deployment_id": self.deployment_id,
            "deployment_role": self.deployment_role,
            "deployment_state": self.deployment_state,
            "image_digest": self.image_digest,
            "model_version_ids": list(self.model_version_ids),
            "preprocessing_spec_ids": list(self.preprocessing_spec_ids),
            "rank_key": self.rank_key.as_document(),
            "service_family": self.service_family,
            "service_version_id": self.service_version_id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class Exclusion:
    """`MOS-REG-070`: every filtered row, with the stage and the reason it was filtered.

    "'No service was available' without the per-candidate reason is not an acceptable
    clinical rejection message."
    """

    service_version_id: str
    stage: str  # "F1".."F9"
    reason_code: str
    detail: str = ""

    def as_document(self) -> dict[str, Any]:
        return {
            "detail": self.detail,
            "reason_code": self.reason_code,
            "service_version_id": self.service_version_id,
            "stage": self.stage,
        }


@dataclass(frozen=True, slots=True)
class Outcome:
    """`MOS-REG-051`'s `Outcome`. `ranked` is the ordered candidate list.

    `MOS-REG-052`: there is no error return. Every call ends either `SELECTED` or
    `ZERO_CANDIDATES` with a reason code.
    """

    decision: str  # "SELECTED" | "ZERO_CANDIDATES"
    selected: Candidate | None
    ranked: tuple[Candidate, ...]
    excluded: tuple[Exclusion, ...]
    reason_code: str
    resolver_version: str
    snapshot_id: str
    epoch: int
    inputs_hash: str

    @property
    def is_selected(self) -> bool:
        return self.decision == "SELECTED"

    def as_document(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "epoch": self.epoch,
            "excluded": [e.as_document() for e in self.excluded],
            "inputs_hash": self.inputs_hash,
            "ranked": [c.as_document() for c in self.ranked],
            "reason_code": self.reason_code,
            "resolver_version": self.resolver_version,
            "selected": self.selected.as_document() if self.selected else None,
            "snapshot_id": self.snapshot_id,
        }

    def canonical_bytes(self) -> bytes:
        """What `MOS-REG-053`'s "byte-identical `Outcome`" is asserted over."""
        return canonical_json(self.as_document()).encode("utf-8")


def as_document(row: Any) -> dict[str, Any]:
    """Public alias of the row-to-document helper, for the snapshot loader and tests."""
    return _as_document(row)


def study_profile(value: Any) -> StudyProfile:
    """Accept a `StudyProfile`, a mapping of constraints, or `None`. Never partial data."""
    if value is None:
        return StudyProfile()
    if isinstance(value, StudyProfile):
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        constraints = data.get("constraints")
        if constraints is None:
            constraints = {
                k: v for k, v in data.items() if k not in ("study_id", "modality")
            }
        return StudyProfile(
            study_id=str(data.get("study_id", "")), constraints=dict(constraints)
        )
    raise TypeError(f"{type(value).__name__} is not a study profile")


def version_pin(value: Any) -> VersionPin | None:
    """Accept a `VersionPin`, a mapping, a bare range string, or `None`."""
    if value is None:
        return None
    if isinstance(value, VersionPin):
        return value
    if isinstance(value, str):
        return VersionPin(range=value)
    if isinstance(value, Mapping):
        return VersionPin(
            service_family=str(value.get("service_family", "")),
            range=str(value.get("range", "")),
        )
    raise TypeError(f"{type(value).__name__} is not a version pin")


def as_tuple(value: Any) -> tuple[Any, ...]:
    """A defensive tuple for a snapshot member a caller handed us as a list."""
    if value is None:
        return ()
    if isinstance(value, tuple):
        return value
    if isinstance(value, Sequence) and not isinstance(value, str):
        return tuple(value)
    return (value,)
