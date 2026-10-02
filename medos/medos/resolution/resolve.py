# SPDX-License-Identifier: Apache-2.0
"""`Resolve()` -- capability resolution as a PURE function. `MOS-REG-051`..`MOS-REG-071`.

    resolve(capability, {modality, tenant_id, environment, study_constraints},
            registry_snapshot) -> Outcome        # Outcome.ranked is the ordered list

THE ONE PROPERTY THIS FILE EXISTS TO HAVE
------------------------------------------
There is no database handle, no clock, no randomness and no ambient state in this module.
`MOS-REG-012`: "`Resolve()` MUST be called with a snapshot, never with a live database
handle. Replaying a recorded `(epoch, snapshot_id)` and re-running `Resolve()` MUST yield
a byte-identical `Outcome`." `MOS-REG-053` restates it as a property test over 10 000
random registry states, which `tests/integration/test_resolution.py` runs.

The only time value read anywhere below is `snapshot.as_of`, which `MOS-REG-051` names as
"the only time source the resolver may read"; it is used for one thing, the licence
expiry in `F2`.

WHY PURITY IS THE CLINICAL REQUIREMENT AND NOT AN AESTHETIC ONE
---------------------------------------------------------------
Chapter 16 records the failure this closes: an agent registered in January resolving
differently in March after v4 is approved -- no version bump, no audit event -- makes
`MOS-EXEC`/`MOS-SAFE` reproducibility false by construction. §6.7.7 works the same failure
through in full: one `job_9f21` reporting 642 mL in January and 705 mL in March, a
provenance record whose two halves name different artifacts, and a second overlapping SEG
series in PACS because the derived `SeriesInstanceUID` is a function of the model version.
None of those three raises an exception.

Two mechanisms stop it, and BOTH are needed:

  1. this function is a pure function of `(request, snapshot)`, so the same inputs give
     the same answer forever (`MOS-REG-053`, `MOS-REG-071`); and
  2. the answer is PINNED into the job at creation and read back verbatim on every retry
     (`MOS-REG-066`, `MOS-REG-067`) -- `medos/medos/resolution/pin.py` writes it and
     `medos/medos/resolution/retry.py` reads it, and the retry module cannot re-resolve because
     it has no `Snapshot` in scope.

FILTER ORDER IS NORMATIVE
--------------------------
`MOS-REG-055`'s stages run in order and each records an `Exclusion`. `MOS-REG-056` fixes
one ordering explicitly -- F3 before F5 -- so that "an explicit pin MUST NOT resurrect a
`SUSPENDED` or `RECALLED` version". Reordering them changes clinical behaviour.

INTERPRETATIONS TAKEN HERE, ALL REPORTED RATHER THAN SILENT
------------------------------------------------------------
I1. `MOS-REG-058`'s P1/P2 are family-match RANKING keys, so a pin's `service_family` is
    not also a hard filter at `F5`; the range applies to every surviving candidate and
    the family decides the order. Read the other way -- family as a filter -- P1 would be
    1 for every candidate that can still be ranked, i.e. a dead key. The arrival path of
    `MOS-REG-054` (`JobPin{ServiceFamily, Range: "=<version>"}`) lands on the right
    version under either reading.
I2. `F7` with an EMPTY node inventory for the environment passes rather than excluding
    everything. An empty `Nodes` is "no inventory was loaded", not "this site owns no
    accelerators"; there is no `node_profile` table in this build (chapter 13 owns it),
    and a filter that fails closed on absent inventory would take the whole platform
    offline the day the inventory loader is late. When the inventory IS present, `F7` is
    enforced.
I3. `F6` admits a `MARGINAL` envelope zone and excludes only `OUT`. `MOS-EVID-101` makes
    `MARGINAL` a TENANT decision (`flag` or `reject`) taken at pre-flight with the
    tenant's policy; a resolver that rejected it here would pre-empt a decision that is
    not its own, and would do it without the flag path that puts
    `[OUTSIDE VALIDATED RANGE]` in front of a reader.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Any, Final

from medos.resolution.bucket import canary_bucket, deployment_rank
from medos.resolution.model import (
    LIFECYCLE_BLOCKED,
    Candidate,
    DeploymentRow,
    Exclusion,
    Outcome,
    RankKey,
    Request,
    ServiceVersionRow,
    Snapshot,
    StudyProfile,
    VersionPin,
    study_profile,
    version_pin,
)
from medos.resolution.versions import (
    Range,
    RangeSyntaxError,
    compare_versions,
    parse_range,
    parse_version,
)

__all__ = [
    "RESOLVER_VERSION",
    "ZERO_CANDIDATES",
    "SELECTED",
    "build_request",
    "resolve",
    "resolve_request",
]

RESOLVER_VERSION: Final[str] = "1.0.0"
SELECTED: Final[str] = "SELECTED"
ZERO_CANDIDATES: Final[str] = "ZERO_CANDIDATES"

# The value `MOS-REG-060` requires for an absent acceptance metric: "-Inf, never 0 and
# never 'pass'". A missing metric that ranked 0 would beat a genuine 0.0 and would tie
# with every other missing metric, which is exactly how an unevaluated version wins.
_ABSENT_METRIC: Final[float] = float("-inf")

_SERVING: Final[str] = "SERVING"
_DISPATCHABLE_ROLES: Final[frozenset[str]] = frozenset({"ACTIVE", "CANARY"})


# =====================================================================================
# Entry points
# =====================================================================================
def build_request(capability: str, context: Mapping[str, Any] | Request) -> Request:
    """`(capability, context)` -> `MOS-REG-051`'s `Request`. No defaults for identity.

    `tenant_id` and `environment` have no default: a resolution that silently ran for
    "some tenant" in "some environment" is the bug this platform's row-level security
    exists to make impossible, and a default here would reintroduce it above the database.
    """
    if isinstance(context, Request):
        return context
    data = dict(context)
    missing = [k for k in ("tenant_id", "environment") if not data.get(k)]
    if missing:
        raise ValueError(
            f"resolution context is missing {', '.join(missing)}; "
            "MOS-REG-051's Request has no default tenant or environment"
        )
    profile = study_profile(
        data.get("study_constraints", data.get("study"))
    )
    if not profile.study_id and data.get("study_id"):
        profile = StudyProfile(
            study_id=str(data["study_id"]), constraints=dict(profile.constraints)
        )
    return Request(
        capability_id=capability,
        tenant_id=str(data["tenant_id"]),
        environment=str(data["environment"]),
        modality=str(data.get("modality", "")),
        study=profile,
        job_pin=version_pin(data.get("job_pin", data.get("pin"))),
    )


def resolve(
    capability: str,
    context: Mapping[str, Any] | Request,
    registry_snapshot: Snapshot,
) -> Outcome:
    """The three-argument form: capability, request context, snapshot.

    Returns `MOS-REG-051`'s `Outcome`, whose `ranked` member is the ordered candidate
    list. `MOS-REG-052`: this never raises for a resolution outcome -- a registry with
    nothing in it is `ZERO_CANDIDATES`, not an exception. It DOES raise `ValueError` for
    a context that names no tenant or no environment, which is a caller defect caught
    before resolution rather than a clinical decision.
    """
    return resolve_request(build_request(capability, context), registry_snapshot)


def resolve_request(request: Request, snapshot: Snapshot) -> Outcome:
    """`MOS-REG-051`'s `Resolve(req, snap) Outcome`, verbatim in shape and in order."""
    inputs_hash = request.inputs_hash()
    base = {
        "resolver_version": RESOLVER_VERSION,
        "snapshot_id": snapshot.snapshot_id,
        "epoch": snapshot.epoch,
        "inputs_hash": inputs_hash,
    }

    capability = snapshot.capabilities.get(request.capability_id)
    if capability is None:
        # `MOS-REG-069`'s one non-F-stage reason code. There is nothing to exclude:
        # no candidate was ever eligible to be considered.
        return Outcome(
            decision=ZERO_CANDIDATES,
            selected=None,
            ranked=(),
            excluded=(),
            reason_code="capability_unknown",
            **base,
        )

    effective_pin, pin_range, pin_error = _effective_pin(request, snapshot)
    bucket = canary_bucket(request.tenant_id, request.study.study_id)
    declared_tenant_pin = _tenant_pin(request, snapshot)
    tenant_pin_family = declared_tenant_pin.service_family if declared_tenant_pin else ""

    candidates: list[Candidate] = []
    exclusions: list[Exclusion] = []
    for service in sorted(snapshot.services, key=lambda s: s.id):
        verdict = _filter(
            service,
            request=request,
            snapshot=snapshot,
            pin_range=pin_range,
            pin_error=pin_error,
            bucket=bucket,
        )
        if isinstance(verdict, Exclusion):
            exclusions.append(verdict)
            continue
        deployment, rank = verdict
        candidates.append(
            _candidate(
                service,
                deployment,
                rank,
                request=request,
                snapshot=snapshot,
                tenant_pin_family=tenant_pin_family,
                bucket=bucket,
            )
        )

    ranked = tuple(sorted(candidates, key=functools.cmp_to_key(_compare)))
    if ranked:
        return Outcome(
            decision=SELECTED,
            selected=ranked[0],
            ranked=ranked,
            excluded=tuple(exclusions),
            reason_code="",
            **base,
        )
    # The ids the effective pin names EXACTLY, if it is an exact pin. `MOS-REG-056`'s two
    # reason codes are about those rows and no others.
    exact = pin_range.exact_version if pin_range is not None else None
    pinned_ids = (
        frozenset(
            s.id
            for s in snapshot.services
            if s.version == str(exact)
            and (
                not (effective_pin and effective_pin.service_family)
                or s.family == effective_pin.service_family
            )
        )
        if exact is not None
        else frozenset()
    )
    return Outcome(
        decision=ZERO_CANDIDATES,
        selected=None,
        ranked=(),
        excluded=tuple(exclusions),
        reason_code=_zero_reason(exclusions, exact is not None, pinned_ids),
        **base,
    )


# =====================================================================================
# The effective pin -- job pin, else tenant pin, else unconstrained (MOS-REG-055 F5)
# =====================================================================================
def _effective_pin(
    request: Request, snapshot: Snapshot
) -> tuple[VersionPin | None, Range | None, str]:
    """`(pin, parsed_range, parse_error)`.

    `MOS-REG-052` forbids an error return, so an unparseable stored range becomes an `F5`
    exclusion carrying the parser's message. `MOS-REG-062` puts the real refusal at
    admission time (`medos.resolution.versions.validate_range`), where the value is
    written and a human is present to read the message.
    """
    pin = request.job_pin
    if pin is None or (not pin.range and not pin.service_family):
        pin = _tenant_pin(request, snapshot)
    if pin is None or not pin.range:
        return pin, None, ""
    try:
        return pin, parse_range(pin.range), ""
    except RangeSyntaxError as exc:
        return pin, None, str(exc)


def _tenant_pin(request: Request, snapshot: Snapshot) -> VersionPin | None:
    """The `TenantPin` for this `(tenant, capability[, environment])`, if any.

    An environment-specific pin wins over an environment-agnostic one; ties are broken by
    family so two equally specific pins cannot resolve in registry order.
    """
    matches = [
        p
        for p in snapshot.tenant_pins
        if p.tenant_id == request.tenant_id
        and p.capability_id == request.capability_id
        and p.environment in ("", request.environment)
    ]
    if not matches:
        return None
    best = sorted(matches, key=lambda p: (p.environment == "", p.service_family))[0]
    return VersionPin(service_family=best.service_family, range=best.version_range)


# =====================================================================================
# Stage F -- the hard filters, in order (MOS-REG-055)
# =====================================================================================
def _filter(
    service: ServiceVersionRow,
    *,
    request: Request,
    snapshot: Snapshot,
    pin_range: Range | None,
    pin_error: str,
    bucket: int,
) -> Exclusion | tuple[DeploymentRow, int]:
    """Run F1..F9 for one candidate. Returns its `Exclusion` or `(deployment, rank)`."""
    sid = service.id
    capability = snapshot.capabilities[request.capability_id]

    # -- F1: the capability exists, is supported, and this version claims it ------------
    if request.capability_id not in service.capabilities:
        return Exclusion(
            sid, "F1", "capability_not_claimed",
            f"{sid} claims {sorted(service.capabilities)}",
        )
    if capability.status == "reserved":
        # MOS-REG-046: "`reserved` rows exist to freeze the identifier; they are not
        # resolvable and MUST be excluded by filter F1."
        return Exclusion(
            sid, "F1", "capability_reserved", f"{capability.id} is reserved"
        )
    if capability.status == "deprecated":
        return Exclusion(
            sid, "F1", "capability_deprecated", f"{capability.id} is deprecated"
        )

    # -- F2: tenant visibility and a non-expired licence -------------------------------
    if service.tenant_id is not None and service.tenant_id != request.tenant_id:
        # MOS-REG-107: the row is not visible, which is a 404 on the API and an exclusion
        # here. Confirming that the id names something is itself the disclosure.
        return Exclusion(
            sid, "F2", "not_visible_to_tenant", "artifact belongs to another tenant"
        )
    licensed = [x for x in snapshot.licences if x.service_family == service.family]
    if licensed:
        held = [x for x in licensed if x.tenant_id == request.tenant_id]
        if not held:
            return Exclusion(
                sid, "F2", "not_visible_to_tenant",
                f"no LicenceGrant for {service.family}",
            )
        if snapshot.as_of is not None and all(
            g.expires_at is not None and g.expires_at <= snapshot.as_of for g in held
        ):
            return Exclusion(
                sid, "F2", "licence_expired",
                f"every LicenceGrant for {service.family} expired before snapshot.as_of",
            )

    # -- F3: lifecycle of the version AND its models AND its preprocessing specs -------
    # MOS-REG-056 puts this BEFORE F5 on purpose: an explicit pin must not resurrect a
    # SUSPENDED or RECALLED version.
    blocked = _lifecycle_exclusion(service, snapshot)
    if blocked is not None:
        return blocked

    # -- F4: a SERVING deployment in an ACTIVE or CANARY role --------------------------
    deployments = [
        d
        for d in snapshot.deployments
        if d.tenant_id == request.tenant_id
        and d.environment == request.environment
        and d.capability_id == request.capability_id
        and d.subject_id == sid
    ]
    if not deployments:
        return Exclusion(
            sid, "F4", "no_deployment",
            f"no deployment for ({request.tenant_id}, {request.environment}, "
            f"{request.capability_id}, {sid})",
        )
    serving = [d for d in deployments if d.state == _SERVING]
    if not serving:
        return Exclusion(
            sid, "F4", "deployment_not_serving",
            f"deployment state {sorted({d.state for d in deployments})}",
        )
    dispatchable = [d for d in serving if d.role in _DISPATCHABLE_ROLES]
    if not dispatchable:
        return Exclusion(
            sid, "F4", "deployment_standby",
            f"deployment role {sorted({d.role for d in serving})}",
        )
    deployment = sorted(dispatchable, key=lambda d: (d.role != "ACTIVE", d.id))[0]

    # -- F5: the effective version range -----------------------------------------------
    if pin_error:
        return Exclusion(sid, "F5", "range_unsatisfied", pin_error)
    if pin_range is not None and not pin_range.matches(service.version):
        return Exclusion(
            sid, "F5", "range_unsatisfied",
            f"{service.version} does not satisfy {pin_range.text!r}",
        )

    # -- F6: modality and the applicability envelope -----------------------------------
    if request.modality and service.modalities and request.modality not in service.modalities:
        return Exclusion(
            sid, "F6", "modality_mismatch",
            f"{request.modality} not in {sorted(service.modalities)}",
        )
    envelope = snapshot.envelopes.get(sid)
    if envelope is not None:
        zone = envelope.verdict_zone(request.study.constraints)
        if zone == "OUT":
            # I3 in the module docstring: MARGINAL is the tenant's decision at pre-flight
            # (MOS-EVID-101), so only OUT excludes here.
            return Exclusion(
                sid, "F6", "envelope_mismatch", "study is OUT of the declared envelope"
            )

    # -- F7: an accelerator in this environment can run it -----------------------------
    runtime = _runtime_exclusion(service, snapshot, request.environment)
    if runtime is not None:
        return runtime

    # -- F8: the evidence gate (gateable, MOS-REG-005) ---------------------------------
    if snapshot.gate_config.evidence_gate and not _has_passing_report(
        service, request, snapshot
    ):
        return Exclusion(
            sid, "F8", "evidence_gate_failed",
            f"no active passing ValidationReport for {sid} on "
            f"{request.capability_id} for tenant {request.tenant_id}",
        )

    # -- F9: the regulatory gate (gateable, MOS-REG-005) -------------------------------
    if snapshot.gate_config.regulatory_gate and deployment.clinical_use_mode == "clinical":
        jurisdiction = ""
        profile = snapshot.tenants.get(request.tenant_id)
        if profile is not None:
            jurisdiction = profile.jurisdiction
        if not service.legal_manufacturer_id:
            return Exclusion(
                sid, "F9", "regulatory_gate_failed", "no legal_manufacturer declared"
            )
        if not jurisdiction or jurisdiction not in service.regulatory_jurisdictions:
            return Exclusion(
                sid, "F9", "regulatory_gate_failed",
                f"no regulatory_status declared for jurisdiction {jurisdiction!r}",
            )

    rank = deployment_rank(deployment.role, deployment.traffic_permille, bucket)
    return deployment, rank


def _lifecycle_exclusion(
    service: ServiceVersionRow, snapshot: Snapshot
) -> Exclusion | None:
    """F3 over the transitive closure: the version, its models, its preprocessing specs."""
    subjects: list[tuple[str, str]] = [(service.id, service.lifecycle_status)]
    for ref in service.model_version_refs:
        row = snapshot.models.get(ref)
        subjects.append((ref, row.lifecycle_status if row else "UNKNOWN"))
    for ref in service.preprocessing_spec_refs:
        row = snapshot.preproc.get(ref)
        subjects.append((ref, row.lifecycle_status if row else "UNKNOWN"))
    for subject_id, status in subjects:
        if status not in LIFECYCLE_BLOCKED:
            continue
        if status == "SUSPENDED":
            code = "version_suspended"
        elif status == "RECALLED":
            code = "version_recalled"
        else:
            code = "version_not_validated"
        return Exclusion(service.id, "F3", code, f"{subject_id} is {status}")
    unknown = [s for s, status in subjects if status == "UNKNOWN"]
    if unknown:
        # MOS-REG-025 makes a dangling ref a publish-time refusal, so reaching this is a
        # snapshot that was built from an inconsistent registry. It excludes rather than
        # dispatches: an unresolvable model reference is not a version anyone validated.
        return Exclusion(
            service.id, "F3", "version_not_validated",
            f"unresolved reference(s) {sorted(unknown)}",
        )
    return None


def _runtime_exclusion(
    service: ServiceVersionRow, snapshot: Snapshot, environment: str
) -> Exclusion | None:
    """F7. See interpretation I2: an empty inventory is unknown, not empty."""
    if not service.gpu_required:
        return None
    nodes = [n for n in snapshot.nodes if n.environment == environment]
    if not nodes:
        return None
    for node in nodes:
        arch_ok = not service.gpu_architectures or bool(
            set(node.gpu_architectures) & set(service.gpu_architectures)
        )
        memory_ok = node.gpu_memory_mib >= service.gpu_memory_mib
        if arch_ok and memory_ok:
            return None
    return Exclusion(
        service.id, "F7", "runtime_unsatisfiable",
        f"no node in {environment} offers {sorted(service.gpu_architectures)} "
        f"with >= {service.gpu_memory_mib} MiB",
    )


def _has_passing_report(
    service: ServiceVersionRow, request: Request, snapshot: Snapshot
) -> bool:
    return any(
        r.subject_id == service.id
        and r.capability_id == request.capability_id
        and r.tenant_id in ("", request.tenant_id)
        and r.verdict == "pass"
        and r.status == "active"
        for r in snapshot.validation_reports
    )


# =====================================================================================
# Ranking (MOS-REG-058..MOS-REG-061)
# =====================================================================================
def _candidate(
    service: ServiceVersionRow,
    deployment: DeploymentRow,
    rank: int,
    *,
    request: Request,
    snapshot: Snapshot,
    tenant_pin_family: str,
    bucket: int,
) -> Candidate:
    key = _rank_key(
        service,
        rank,
        request=request,
        snapshot=snapshot,
        tenant_pin_family=tenant_pin_family,
        bucket=bucket,
    )
    return Candidate(
        service_version_id=service.id,
        service_family=service.family,
        version=service.version,
        image_digest=service.image_digest,
        model_version_ids=tuple(service.model_version_refs),
        preprocessing_spec_ids=tuple(service.preprocessing_spec_refs),
        deployment_id=deployment.id,
        deployment_environment=deployment.environment,
        deployment_state=deployment.state,
        deployment_role=deployment.role,
        clinical_use_mode=deployment.clinical_use_mode,
        content_digest=service.content_digest,
        rank_key=key,
    )


def _rank_key(
    service: ServiceVersionRow,
    rank: int,
    *,
    request: Request,
    snapshot: Snapshot,
    tenant_pin_family: str,
    bucket: int,
) -> RankKey:
    """`MOS-REG-058`'s seven keys, computed once per surviving candidate.

    P4 reads `snap.Metrics` at `MOS-REG-051`'s own key,
    `serviceVersionID + "|" + capabilityID + "|" + datasetVersionID`, where the dataset is
    the TENANT's acceptance `DatasetVersion` (`tenant_acceptance_bindings`, ch. 12). A
    tenant with no binding has no acceptance dataset, so the metric is ABSENT and ranks
    `-Inf` (`MOS-REG-060`) -- it does not silently fall back to somebody else's cohort,
    which would be the platform quietly ranking on evidence the tenant never accepted.
    """
    dataset = snapshot.acceptance_datasets.get(
        f"{request.tenant_id}|{request.capability_id}", ""
    )
    point = snapshot.metrics.get(
        f"{service.id}|{request.capability_id}|{dataset}"
    ) if dataset else None
    job_pin = request.job_pin
    return RankKey(
        job_pin_family_match=int(
            bool(job_pin and job_pin.service_family == service.family)
        ),
        tenant_pin_family_match=int(
            bool(tenant_pin_family) and tenant_pin_family == service.family
        ),
        deployment_rank=rank,
        acceptance_metric=point.value if point is not None else _ABSENT_METRIC,
        acceptance_metric_present=point is not None,
        deprecated=int(service.lifecycle_status == "DEPRECATED"),
        version=service.version,
        content_digest=service.content_digest,
        canary_bucket=bucket,
    )


def _compare(a: Candidate, b: Candidate) -> int:
    """`MOS-REG-058`'s tuple, compared left to right. Ascending = better first.

    `MOS-REG-061`: "`Outcome.Ranked` MUST be a total order. A resolver that can return two
    candidates with an identical `RankKey` is non-conformant; P7 exists to make this
    impossible." P7 is `content_digest`, which `0012_artifacts.up.sql` makes UNIQUE -- so
    the last comparison below can only be reached by a snapshot that a caller built with
    two rows sharing a digest, and the id tie-break keeps the order total even then.
    """
    ka, kb = a.rank_key, b.rank_key
    for x, y in (
        (ka.job_pin_family_match, kb.job_pin_family_match),      # P1 desc
        (ka.tenant_pin_family_match, kb.tenant_pin_family_match),  # P2 desc
        (ka.deployment_rank, kb.deployment_rank),                 # P3 desc
    ):
        if x != y:
            return -1 if x > y else 1
    if ka.acceptance_metric != kb.acceptance_metric:              # P4 desc
        return -1 if ka.acceptance_metric > kb.acceptance_metric else 1
    if ka.deprecated != kb.deprecated:                            # P5 asc
        return -1 if ka.deprecated < kb.deprecated else 1
    order = compare_versions(parse_version(a.version), parse_version(b.version))
    if order != 0:                                                # P6 desc
        return -order
    if ka.content_digest != kb.content_digest:                    # P7 asc
        return -1 if ka.content_digest < kb.content_digest else 1
    if a.service_version_id != b.service_version_id:
        return -1 if a.service_version_id < b.service_version_id else 1
    return 0


# =====================================================================================
# Zero candidates (MOS-REG-069)
# =====================================================================================
def _zero_reason(
    exclusions: list[Exclusion], exact_pin: bool, pinned_ids: frozenset[str]
) -> str:
    """The single reason code a `REJECTED` job carries, chosen deterministically.

    `MOS-REG-069`: "The reason code MUST be one of the F-stage codes in `MOS-REG-055` plus
    `capability_unknown`", and the job terminates `REJECTED`, never `FAILED`, and never
    falls back to another version.

    `MOS-REG-056` owns the two special cases: an EXACT pin naming a suspended or recalled
    version yields `pinned_version_suspended` / `pinned_version_recalled`, because "your
    range matched nothing" is a different clinical sentence from "the version you named
    was withdrawn". An exact pin naming a version the registry does not hold at all is
    `pinned_version_not_found` (`MOS-REG-055`, F5).

    Otherwise the code is the one from the candidate that got FURTHEST through the
    filters: it is the most actionable of the set, and "furthest" is a total order over
    the stage number with the candidate order as the tie-break.
    """
    if exact_pin:
        for e in exclusions:
            if e.service_version_id not in pinned_ids or e.stage != "F3":
                continue
            if e.reason_code == "version_suspended":
                return "pinned_version_suspended"
            if e.reason_code == "version_recalled":
                return "pinned_version_recalled"
        if not pinned_ids:
            # The registry holds no version with that number at all. F5's other code.
            return "pinned_version_not_found"
    if not exclusions:
        # An empty registry, or one in which nothing claims the capability. The F1 code is
        # the true one: no ServiceVersion claims it.
        return "capability_not_claimed"
    furthest = max(exclusions, key=lambda e: int(e.stage[1:]))
    return furthest.reason_code
