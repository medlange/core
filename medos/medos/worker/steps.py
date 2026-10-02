# SPDX-License-Identifier: Apache-2.0
"""The typed in-process step executor (CONTRACT.md section 1: `worker/steps.py`).

Chapter 5 section 5.4.1 (`MOS-EXEC-023`) fixes eight reserved `step_key` values and their
canonical `phase` strings. This module runs exactly those eight, in that order, in this
process, as ordinary Python function calls.

WHAT THIS IS NOT
----------------
It is not a workflow engine, and `MOS-REL-036` is the reason: batch orchestration on the
inference path is forbidden. There is no DAG object, no scheduler, no retry-per-step, no
step queue and no persisted intermediate artifact. The canonical volume is a local
variable. A step that fails ends the attempt, and the ATTEMPT is what retries -- which is
the only unit whose retry semantics chapter 5 defines. Roughly 200 lines of pipeline plus
the reasons for them, and the reasons are the part that would be expensive to rediscover.

THE STEP PLAN, AND HOW IT MAPS ONTO THE PIPELINE
------------------------------------------------
CONTRACT.md section 0's pipeline is "DICOMweb pull -> series selection -> canonical volume
-> in-process inference -> inverse transform -> ResultBundle -> SEG + SR -> STOW". Chapter
5's eight reserved keys are the platform's names for the same path, and the reserved names
win (MOS-EXEC-023: they "MUST NOT be reused with a different meaning"):

    fetch_series    retrieving           QIDO the study, SELECT the series, WADO the instances
    build_volume    building_volume      MOS-IMG-012..038, one CanonicalVolume + SourceGeometry
    envelope_check  checking_applicability  Capability.applicable() for every requested id
    service_invoke  inferring            the capabilities, in dependency order -> ResultBundle
    validate_bundle validating_result    the platform re-validates what the service returned
    write_dicom     writing_dicom        SEG **and** SR, built but not yet stored
    store_dicom     storing_dicom        STOW both objects, read FailedSOPSequence
    persist_result  persisting           results rows + T8, in ONE transaction

Selection is inside `fetch_series` rather than a step of its own because the plan is eight
rows and adding a ninth would break `MOS-EXEC-022`'s immutability for every job already
planned. Writing the SEG and the SR is one step for the same reason, and because they are
one atomic unit: an SR references the SEG's SOPInstanceUID (MOS-IMG-116), so a plan that
could write one without the other could write a dangling reference.

REJECTED versus FAILED -- the distinction this module exists to get right
------------------------------------------------------------------------
CONTRACT.md section 3: "`REJECTED` is a **clinical** outcome, not an error: no eligible
series, outside the applicability envelope, unsupported geometry. It MUST carry a
machine-readable reason." MOS-EXEC-014 says why it is release-gated: "a radiologist who
sees a red error where the truth is 'no thin axial recon exists in this study' will either
chase an IT ticket or, worse, assume the study was cleared."

So every rejection path here raises a `ClinicalRejection` subclass carrying a code, and
`reject_reason_code()` maps the exception TYPE -- never a string match, never a status code
-- onto chapter 5's closed `jobs.reject_reason_code` enum. A transport fault raises
`TransportFailure` and a defect raises `SystemFailure`; the runner turns the first into T7
and the other two into T9/T11. No step decides its own terminal state.

WHAT LIVES NEXT DOOR, AND WHY IT IS NOT IN HERE
-----------------------------------------------
CONTRACT.md section 1 lists two modules under `worker/`. There are four, and the two extra
ones are REPORTED rather than folded in silently, because each is a different SUBJECT and
folding them back would make this file a place where three things are edited for three
unrelated reasons:

    `worker/selection.py`    which series this job runs on -- chapter 3's admission
                             decision, which produces `job_series` rows a radiologist
                             reads, not pipeline control flow.
    `worker/result_rows.py`  how a `ResultBundle` becomes `results` /
                             `result_measurements` / `result_dicom_objects` rows --
                             chapter 12 mapping, pure, and writing nothing.

What is left here is the plan, the eight step functions and the loop that runs them.

PHI (CONTRACT.md section 11)
----------------------------
Every `detail` dict this module writes into `job_steps.detail` and every `job_events`
payload it produces contains UIDs, counts, millimetres and codes. There is no PatientName,
PatientID, AccessionNumber, StudyDate or SeriesDescription anywhere in this file, and
`DicomWebGateway.list_series` returns a `SeriesSummary` that does not carry them in the
first place.

Spec: MOS-EXEC-019..024, MOS-EXEC-057, MOS-EXEC-059, MOS-EXEC-061, MOS-IMG-039,
MOS-IMG-062, MOS-IMG-084, MOS-IMG-085, MOS-IMG-098, MOS-IMG-120, MOS-IMG-141,
MOS-STORE-270, MOS-STORE-275, MOS-STORE-278, MOS-STORE-282, MOS-REL-036,
CONTRACT.md sections 0, 3, 5, 6, 7, 8 and 10.
"""

from __future__ import annotations

import platform
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import psycopg
import pydicom
from pydicom.dataset import Dataset

from medos.capabilities import Capability, CapabilityContext
from medos.capabilities import providers as capability_providers
from medos.core.bundle import CapabilityOutcome, ResultBundle
from medos.core.concepts import ConceptDictionary
from medos.core.errors import (
    ClinicalRejection,
    GeometryRejection,
    MedosError,
    SeriesSelectionError,
    SystemFailure,
    TransportFailure,
)
from medos.core.geometry import CanonicalVolume, SourceGeometry, build_canonical_volume
from medos.db import audit, repo
from medos.db.repo import (
    SLICE_TENANT_ID,
    MeasurementRow,
    ResultRow,
    SeriesVerdict,
)
from medos.db.tenancy import current_tenant, tenant_tx
from medos.dicomweb.gateway import DicomWebGateway
from medos.inference.backend import InferenceBackend
from medos.inference.dispatch import run_capability
from medos.safety.envelope import EnvelopeViolation
from medos.safety.marking import assert_marked
from medos.sdk.canonical import new_ulid
from medos.worker.result_rows import (
    dicom_object_rows,
    finding_json,
    geometry_record,
    provenance_record,
)
from medos.worker.selection import (
    MIN_INSTANCES,
    SELECTABLE_MODALITIES,
    SELECTOR_NAME,
    select_ct_series,
)
from medos.writer.identity import (
    PREPROCESSING_SPEC_VERSION,
    OutputPlan,
    build_job_identity,
    plan_outputs,
    preprocessing_spec_digest,
    producing_outcome,
)
from medos.writer.seg import build_seg
from medos.writer.sr import build_sr

__all__ = [
    "STEP_KEYS",
    "WORKER_VERSION",
    "EnvelopeRejection",
    "EnvelopeViolation",
    "BundleInvalid",
    "LeaseGuard",
    "WorkerDeps",
    "StepContext",
    "PipelineState",
    "Step",
    "STEPS",
    "execute_plan",
    "reject_reason_code",
    "failure_class_for",
    "select_ct_series",
    "deployment_registry",
    "deployment_concepts",
    "resolve_concepts",
    "SELECTOR_NAME",
    "SELECTABLE_MODALITIES",
    "MIN_INSTANCES",
]


# The worker's own version. Recorded on every `results` row (CONTRACT.md section 10) and
# therefore part of the answer to "what produced this number".
WORKER_VERSION = "medos.worker/0.1.0"

# The DICOMweb gateway build that read the pixels. MOS-SAFE-083 section B names
# `input.gateway.gateway_version`, and MOS-STORE-251 pins the de-identification policy
# alongside it: "which code read these images, under which policy" is one question.
GATEWAY_VERSION = "medos.dicomweb.gateway/0.1.0"


def _endpoint_of(gateway: Any) -> str | None:
    """The DICOMweb root a STOW went to, for `outputs[].destination.stow_endpoint`.

    Read defensively because the gateway is INJECTED (`WorkerDeps.gateway`) and a test
    stand-in implements the five methods the executor calls, not the config object. A
    missing endpoint is recorded as null: MOS-SAFE-085's point is that the record must
    not claim more than was verified, and that applies to WHERE as much as to WHETHER.
    """
    config = getattr(gateway, "config", None)
    return getattr(config, "base_url", None)

STEP_KEYS: tuple[str, ...] = tuple(key for key, _phase, _owner, _timeout in repo.STEP_PLAN)


# =====================================================================================
# Errors this module adds (CONTRACT.md defines neither; both are reported)
# =====================================================================================
class EnvelopeRejection(ClinicalRejection):
    """A requested capability declined the study before any inference ran.

    chapter 5's reject enum spells it `outside_applicability_envelope`. It is a
    `ClinicalRejection` and therefore `REJECTED`, because "this service does not apply to
    this data" is an answer about the study, not an incident.
    """


class BundleInvalid(SystemFailure):
    """The platform's re-validation of a `ResultBundle` failed (MOS-IMG-141).

    `SystemFailure`, so `FAILED` and not `REJECTED`, and `failure_class =
    'invalid_result_bundle'`. A service that returns a label map of the wrong shape is
    broken; retrying re-runs the bug, and calling it a clinical rejection would tell a
    radiologist the STUDY was unsuitable when the truth is that the software is.
    """


def reject_reason_code(exc: BaseException, step_key: str | None = None) -> str:
    """Map a clinical rejection onto chapter 5's closed `jobs.reject_reason_code` enum.

    On the exception TYPE and never on a message: a string match is exactly how "no
    eligible series" would one day start arriving as a FAILED job after an innocuous
    reword. `step_key` is the tie-breaker for the one case the type cannot decide -- a
    capability that ran and declined raises whatever `ClinicalRejection` subclass its own
    package defines, and "a clinical rejection raised while the service was executing" is
    chapter 5's `service_declined` no matter what that subclass is called.

    The fine-grained code -- `no_eligible_series`, `geometry_gantry_tilt`, whatever the
    capability itself used -- is NOT discarded: `execute_plan` writes it into
    `job_steps.error_code` and thence into the `job.step_changed` event, so the
    machine-readable reason CONTRACT.md section 3 requires survives at full resolution
    while `jobs.reject_reason_code` stays inside the enum the schema CHECK permits.
    """
    if isinstance(exc, SeriesSelectionError):
        return "no_eligible_series"
    if isinstance(exc, GeometryRejection):
        return "unsupported_geometry"
    # Both envelope refusals map to the same chapter 5 code, and they are two different
    # types on purpose: `EnvelopeRejection` is a capability declining in code
    # (CONTRACT.md section 6), `EnvelopeViolation` is the DECLARED envelope of chapter 7
    # section 7.10 refusing the study (MOS-EVID-102). The job column takes one value --
    # `MOS-DATA-080`'s closed set has exactly one member for this -- while
    # `job_steps.error_code` keeps the fine-grained `MOS-EVID-103` code and the
    # `violations[]` document, so nothing is lost by the collapse.
    if isinstance(exc, (EnvelopeRejection, EnvelopeViolation)):
        return "outside_applicability_envelope"
    if step_key in ("service_invoke", "validate_bundle"):
        return "service_declined"
    return "input_constraint_unmet"


# =====================================================================================
# Capability ordering and concept resolution -- owned HERE, on purpose
# =====================================================================================
def resolve_capability_order(
    registry: Mapping[str, Capability], capability_ids: Sequence[str]
) -> tuple[str, ...]:
    """Total, stable execution order for a requested capability set.

    CONTRACT.md section 7: "`emphysema_laa` depends on `lung_segmentation`'s mask. Express
    that as an explicit step ordering in `worker/steps.py`, not as a hidden import." This
    function is that sentence. It reads each capability's OWN `depends_on` declaration --
    so the dependency is data the capability publishes, and the ordering decision is the
    worker's -- and imports no capability module.

    Dependencies first, then alphabetical among independents, so two runs of the same job
    produce the same `ResultBundle` order and therefore the same SEG segment numbering
    (MOS-IMG-066).

    A requested capability whose dependency was NOT requested raises. Silently pulling
    `lung_segmentation` into a job that asked only for `emphysema_laa` would write a
    `results` row and a SEG the caller never asked for, and `jobs.capability_ids` would
    then disagree with the `results` table.
    """
    unknown = [c for c in capability_ids if c not in registry]
    if unknown:
        raise KeyError(
            f"unknown capability_id(s) {unknown}; the registry holds {sorted(registry)}"
        )
    requested = set(capability_ids)
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(cid: str, stack: tuple[str, ...]) -> None:
        if cid in seen:
            return
        if cid in stack:
            raise ValueError(f"capability dependency cycle: {' -> '.join((*stack, cid))}")
        for dep in tuple(getattr(registry[cid], "depends_on", ())):
            if dep not in requested:
                raise ValueError(
                    f"capability {cid!r} depends on {dep!r}, which this job did not "
                    "request (CONTRACT.md section 7). Request both, or neither."
                )
            visit(dep, (*stack, cid))
        seen.add(cid)
        ordered.append(cid)

    for cid in sorted(requested):
        visit(cid, ())
    return tuple(ordered)


def deployment_registry() -> Mapping[str, Capability]:
    """`WorkerDeps.registry`'s default: what THIS DEPLOYMENT serves, not what core ships.

    The same call `medos.api.routes_jobs.known_capability_ids()` makes. That is the point
    and it is not a tidiness argument: two independent compositions disagree, and the shape
    of the disagreement is "the API accepted a job this worker cannot run", which surfaces
    as `resolve_capability_order` raising `unknown capability_id` and the job FAILING with
    an internal error for a study that was never the problem.

    With no `MEDOS_CAPABILITY_PROVIDERS` configured this returns exactly
    `medos.capabilities.REGISTRY`'s three entries, which is what this default was before.
    A provider that is configured and broken raises `CapabilityProviderError` out of
    `WorkerDeps()` -- so `WorkerRunner(config)` refuses to construct, `main()` exits
    non-zero, and the supervisor restarts into the same loud failure instead of the worker
    quietly serving three capabilities while the operator believes it serves four.
    """
    return capability_providers.resolve().registry


def deployment_concepts() -> ConceptDictionary:
    """`WorkerDeps.concepts`'s default: the dictionary the resolved registry resolves in.

    A provider that contributes coded-concept rows returns the COMPOSED dictionary from
    `resolve()`; every capability in the mapping is then constructed against that one
    object, which is what keeps `MOS-REG-042`'s "a second code table anywhere in the
    platform is forbidden" true of a multi-provider deployment.

    `None` from the resolver means no provider contributed rows, and then the platform's
    own loader answers -- `resolve_concepts()` below, unchanged, which is the one place
    that decides where the platform's concept rows live.
    """
    composed = capability_providers.resolve().concepts
    return composed if composed is not None else resolve_concepts()


def resolve_concepts() -> ConceptDictionary:
    """Load the coded-concept dictionary through whichever loader the registry exposes.

    MOS-IMG-113 forbids materialising the clinical code list in code, so it is loaded from
    data; MOS-IMG-112 makes a missing key raise. WHICH module owns the loader is a
    `medos.capabilities` decision and not this module's, hence the lookup rather than a
    fixed import: the worker must not become a second place that decides where the
    concept rows live.
    """
    import medos.capabilities as caps

    for name in ("load_concepts", "default_concepts"):
        loader = getattr(caps, name, None)
        if callable(loader):
            return loader()
    from medos.capabilities.base import load_concepts

    return load_concepts()


def failure_class_for(exc: BaseException, step_key: str | None) -> tuple[str, str]:
    """`(failure_class, failure_code)` from chapter 5 section 5.3.2's twelve-class table.

    `MOS-EXEC-030` makes the retry DECISION the queue driver's, not the caller's; what the
    caller owes it is an accurate class, because that class is what an operator reads
    first and what `MOS-EXEC-016a` maps onto the wire.
    """
    if isinstance(exc, TransportFailure):
        if step_key == "store_dicom" or exc.reason_code.startswith("dicom_store"):
            return "dicom_store_failed", exc.reason_code
        return "gateway_unavailable", exc.reason_code
    if isinstance(exc, BundleInvalid):
        return "invalid_result_bundle", exc.reason_code
    if isinstance(exc, MedosError):
        if step_key == "write_dicom":
            return "dicom_write_failed", exc.reason_code
        return "internal", exc.reason_code
    return "internal", "unhandled_exception"


# =====================================================================================
# Collaborators
# =====================================================================================
class LeaseGuard(Protocol):
    """MOS-EXEC-028's abort signal, narrowed to what a step needs to know.

    "A runner that receives `Valid = false` MUST abort immediately, MUST NOT call
    `Complete` or `Fail`, MUST NOT STOW any DICOM object, and MUST release GPU memory."
    `check()` raises `medos.db.queue.LeaseLost` when the lease is gone. The executor calls
    it between every step and, critically, immediately before the STOW: that is the last
    moment at which aborting still prevents a job whose lease another worker now holds
    from writing into the PACS.
    """

    def check(self) -> None: ...


class _NullLeaseGuard:
    """For unit tests and for a single-shot run with no heartbeat thread."""

    def check(self) -> None:
        return None


@dataclass(frozen=True)
class WorkerDeps:
    """Everything the executor needs that is not the database.

    Injected rather than imported so that a test can substitute a fake PACS, and so that
    the capability registry stays a parameter -- CONTRACT.md section 11: "No global
    mutable state. Pass the connection; do not import a singleton."

    THE DEFAULT IS NOW A DEPLOYMENT'S ANSWER, NOT CORE'S. `registry` and `concepts` used to
    default to `medos.capabilities.REGISTRY` and the platform concept file, which made the
    injection seam reachable only from a test: the shipped `python -m medos.worker.runner`
    served exactly the three built-in capabilities and no flag or variable could add a
    fourth. They now default through `medos.capabilities.providers.resolve()`, which the
    API's `known_capability_ids()` also calls, so one `MEDOS_CAPABILITY_PROVIDERS` value
    configures both processes and they cannot disagree about what this deployment serves.
    Injection is unchanged and still wins: passing `registry=` skips the resolver entirely.
    """

    # `DicomWebGateway` and never `DicomWebClient`: CONTRACT.md section 1 makes the
    # gateway "the ONLY holder of PACS credentials in later releases", and a worker that
    # held a raw client would be a second place to audit for that when weeks 3-5 arrives.
    gateway: DicomWebGateway
    work_root: Path
    # Defaults resolved from `MEDOS_CAPABILITY_PROVIDERS` through the ONE resolver the
    # API also reads (`medos.capabilities.providers`). Still injected -- a test substitutes
    # a fake registry exactly as before -- but an unconfigured process no longer gets the
    # core singleton by a DIFFERENT route than the API does.
    registry: Mapping[str, Capability] = field(default_factory=deployment_registry)
    concepts: ConceptDictionary = field(default_factory=deployment_concepts)
    worker_version: str = WORKER_VERSION
    runtime_version: str = field(
        default_factory=lambda: f"python/{platform.python_version()}; "
        f"pydicom/{pydicom.__version__}; numpy/{np.__version__}"
    )
    platform_commit: str | None = None
    keep_work_dir: bool = False
    # Weeks 3-5 (docs/spec/15-delivery.md section 15.2.4): native-mode inference runs on
    # the single shared Triton, administered by medicalos-tritond (chapter 13 section
    # 13.10). Injected for the same reason `gateway` is -- CONTRACT.md section 11 forbids
    # importing a singleton -- and OPTIONAL because the three capabilities in this
    # codebase are deterministic and legitimately need no model server. A native-mode
    # capability scheduled on a worker where this is None raises NativeBackendMissing;
    # there is deliberately no in-process fallback (see medos/medos/inference/dispatch.py).
    #
    # The type is the PROVISIONAL InferenceBackend port of section 15.2.9, which that
    # section labels "proposed, NOT settled; see OQ-10 (Chapter 16)".
    inference: InferenceBackend | None = None


@dataclass(frozen=True)
class StepContext:
    conn: psycopg.Connection[Any]
    queue: Any  # medos.db.queue.JobQueue
    job: dict[str, Any]  # the `jobs` row, read once at claim
    worker_id: str
    attempt: int
    deps: WorkerDeps
    lease: LeaseGuard = field(default_factory=_NullLeaseGuard)

    @property
    def job_id(self) -> str:
        return str(self.job["public_id"])

    @property
    def study_instance_uid(self) -> str:
        return str(self.job["study_instance_uid"])

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(self.job["capability_ids"])

    @property
    def work_dir(self) -> Path:
        # One directory per (job, attempt). Per ATTEMPT, so a retry after a partial
        # retrieval cannot mistake half a series for a whole one.
        return self.deps.work_root / self.job_id / f"attempt-{self.attempt}"


@dataclass
class PipelineState:
    """The values that pass between steps. Local, in memory, never persisted.

    chapter 5 section 5.4.2 describes `ExecutionArtifact` rows carrying the canonical
    volume between steps. There are none here: this slice runs the whole plan in one
    process, so the artifact IS this object, and inventing an artifact table would add a
    serialisation boundary with nothing on the other side of it. What chapter 12 wanted
    the artifacts FOR -- "what grid was this measured on" -- is recorded on the `results`
    row instead (`results.geometry`, MOS-STORE-273).
    """

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    series_instance_uid: str | None = None
    # MOS-SAFE-084: every series triaged, selected AND rejected, kept in memory as the
    # SAME objects `fetch_series` wrote to `job_series`. The provenance record needs "what
    # else was on the table" and reading the rows back would be a second source of truth
    # for it -- the two would drift the first time a selector changed.
    series_verdicts: tuple[SeriesVerdict, ...] = ()
    instance_paths: tuple[Path, ...] = ()
    volume: CanonicalVolume | None = None
    source: SourceGeometry | None = None
    order: tuple[str, ...] = ()
    outcomes: dict[str, CapabilityOutcome] = field(default_factory=dict)
    bound: dict[str, Capability] = field(default_factory=dict)
    bundle: ResultBundle | None = None
    plan: OutputPlan | None = None
    seg: Dataset | None = None
    sr: Dataset | None = None
    stored_sop_instance_uids: tuple[str, ...] = ()
    # MOS-SAFE-085: "outputs[].destination.qido_verified_at MUST be set only after a
    # QIDO-RS query against the minted SeriesInstanceUID returned the expected instance
    # count. A STOW-RS 200 is not proof of landing." These three are the evidence of that
    # re-read, captured where it happens (`store_dicom`) and nowhere else, so a record
    # cannot claim a verification the pipeline did not perform.
    qido_verified_at: datetime | None = None
    stow_http_status: int | None = None
    stow_endpoint: str | None = None
    verified_counts: dict[str, int] = field(default_factory=dict)
    # sop_instance_uid -> the Part 10 file this attempt wrote, so the digest and the
    # size recorded in `result_dicom_objects` are of the exact bytes that were STOWed.
    object_files: dict[str, Path] = field(default_factory=dict)
    reused_existing_objects: bool = False
    result_rows: tuple[ResultRow, ...] = ()
    result_ids: tuple[str, ...] = ()
    # MOS-EVID-101 / MOS-SAFE-083: the declared-envelope verdict, kept so that
    # `persist_result` can write the TRUTH into `execution.applicability` instead of the
    # hardcoded `{"in_envelope": true, "violations": []}` this pipeline carried before the
    # envelope existed. `None` means no envelope was declared for this ServiceVersion,
    # which is a third state and is recorded as such -- not as "in envelope".
    envelope_verdict: Any | None = None


class _SkipStep(Exception):
    """Raised by a step that is a no-op on this attempt. Carries the MOS-EXEC-020 reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# =====================================================================================
# 1. fetch_series  --  retrieving
# =====================================================================================
def step_fetch_series(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """QIDO the study, apply the selector, WADO the winning series to local disk.

    MOS-STORE-270: "a job whose selection produced no `selected` row MUST be `REJECTED`
    with `reject_reason_code` set", and the reason every rejected series was rejected is a
    ROW, "not a log line and not a `jsonb` blob". So the verdicts are written for BOTH
    outcomes -- including the losing one, before the rejection is raised -- and a
    clinician asking "why did this not run on my study" gets the table rather than a
    shrug.
    """
    gateway = ctx.deps.gateway

    # MOS-SEC-150: a PACS read crosses a boundary with no shared transaction, so it takes
    # TWO audit rows joined by `request_id` -- the authorization event committed BEFORE
    # the upstream call and the effect event after it. "Append-only storage forbids
    # updating the first, so the pair is the mechanism." A single row written afterwards
    # could only ever record reads that completed, which is the half that does not matter:
    # an interrupted retrieval that touched a patient's pixels is exactly the event an
    # access review is looking for.
    #
    # MOS-SEC-089 is the obligation this discharges: "an AuditEvent of class `phi`
    # recording the instance and series counts actually returned". MOS-SEC-147 is why the
    # actor is the worker and the delegation is the job's creator -- the images were read
    # on someone's authority, and the row says whose.
    request_id = audit.new_request_id()
    obo = audit.OnBehalfOf.from_job(ctx.job)
    actor = audit.Actor(kind="workload", id=ctx.worker_id, auth="internal")
    audit.record(
        ctx.conn,
        action="study.read_pixels",
        action_class="phi",
        actor=actor,
        resource=audit.Resource.study(current_tenant(), ctx.study_instance_uid),
        outcome="allow",
        pep="gateway.retrieve",
        trace_id=str(ctx.job["trace_id"]),
        request_id=request_id,
        job_id=ctx.job_id,
        on_behalf_of=obo,
        study_instance_uid=ctx.study_instance_uid,
        detail={"attempt": ctx.attempt, "reason": "job_execution"},
    )

    # MOS-DATA-058: selection reads METADATA only. `list_series` is QIDO-RS and returns
    # `SeriesSummary`, which carries no PatientName for a log line to leak.
    rows = gateway.list_series(ctx.study_instance_uid)

    try:
        winner, losers = select_ct_series(rows)
    except SeriesSelectionError:
        repo.record_series_verdicts(
            ctx.conn,
            ctx.job_id,
            [
                SeriesVerdict(
                    series_instance_uid=r.series_instance_uid,
                    decision="rejected",
                    reason_code="modality_not_applicable",
                    reason_detail=f"modality {r.modality!r}, {r.n_instances} instance(s)",
                    instance_count=r.n_instances,
                    modality=r.modality,
                )
                for r in rows
            ],
        )
        raise

    verdicts = [
        SeriesVerdict(
            series_instance_uid=winner.series_instance_uid,
            decision="selected",
            selector_name=SELECTOR_NAME,
            rank=0,
            instance_count=winner.n_instances,
            modality=winner.modality,
        )
    ]
    verdicts.extend(
        SeriesVerdict(
            series_instance_uid=row.series_instance_uid,
            decision="rejected",
            reason_code=code,
            reason_detail=detail,
            instance_count=row.n_instances,
            modality=row.modality,
        )
        for row, code, detail in losers
    )
    repo.record_series_verdicts(ctx.conn, ctx.job_id, verdicts)
    # MOS-SAFE-084's half of the same verdicts: the row goes to `job_series`, the value
    # goes into the provenance record, and both come from this one list.
    state.series_verdicts = tuple(verdicts)

    # `verify_against_qido` compares the SET that arrived against the SET QIDO listed
    # (MOS-IMG-152). It is the difference between "the builder was handed 234 of 237
    # slices" failing HERE, as a transport failure, and surfacing two steps later as a
    # short volume with a perfectly plausible affine.
    fetched = gateway.fetch_series(
        ctx.study_instance_uid,
        winner.series_instance_uid,
        ctx.work_dir,
        verify_against_qido=True,
    )
    state.series_instance_uid = winner.series_instance_uid
    state.instance_paths = tuple(sorted(fetched.paths))

    # MOS-SEC-150's effect event, joined to the authorization above by `request_id`, and
    # MOS-SEC-089's counts: "the instance and series counts actually returned".
    audit.record(
        ctx.conn,
        action="study.read_pixels.completed",
        action_class="phi",
        actor=actor,
        resource=audit.Resource.study(current_tenant(), ctx.study_instance_uid),
        outcome="allow",
        pep="gateway.retrieve",
        trace_id=str(ctx.job["trace_id"]),
        request_id=request_id,
        job_id=ctx.job_id,
        on_behalf_of=obo,
        study_instance_uid=ctx.study_instance_uid,
        detail={
            "series_evaluated": len(rows),
            "series_selected": 1,
            "instances_returned": fetched.n_instances,
            "bytes_returned": fetched.bytes_written,
            "series_instance_uid": winner.series_instance_uid,
        },
    )
    return {
        "series_instance_uid": winner.series_instance_uid,
        "selector_name": SELECTOR_NAME,
        "series_evaluated": len(rows),
        "series_rejected": len(losers),
        "instances_retrieved": fetched.n_instances,
        "bytes_retrieved": fetched.bytes_written,
    }


# =====================================================================================
# 2. build_volume  --  building_volume
# =====================================================================================
def step_build_volume(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """MOS-IMG-012..038. Raises `GeometryRejection` -> T7 `REJECTED`, never `FAILED`.

    `allow_tilt_correction` and `allow_resample_non_uniform` stay False: MOS-IMG-021 makes
    *reject* the default and MOS-IMG-022 rates a wrong correction worse than a rejection.
    A study this slice cannot build is a study this slice declines, out loud, with a code.
    """
    volume, source, diagnostics = build_canonical_volume(state.instance_paths)
    state.volume = volume
    state.source = source
    return {
        "shape": list(volume.shape),
        "spacing_mm": [round(float(s), 6) for s in volume.spacing_mm],
        "anatomical_code": volume.anatomical_code,
        "spacing_class": volume.spacing_class,
        "tilt_deg": round(float(volume.tilt_deg), 4),
        "max_jitter_mm": round(float(volume.max_jitter_mm), 6),
        "instances_used": len(volume.sop_instance_uids),
        "instances_dropped": len(volume.dropped_duplicate_sop_instance_uids),
        "builder_version": volume.builder_version,
        "sort_order_evidence": diagnostics.get("sort_order_evidence"),
    }


# =====================================================================================
# 3. envelope_check  --  checking_applicability
# =====================================================================================
def step_envelope_check(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """The declared `ApplicabilityEnvelope`, then `Capability.applicable()`. In that order.

    TWO GATES, TWO OWNERS, AND WHY THE ORDER MATTERS
    ------------------------------------------------
    1. The DECLARED envelope (chapter 7 section 7.10, `MOS-EVID-095`..`104`). Versioned,
       immutable, content-addressed data published against the ServiceVersion, evaluated by
       the PLATFORM. `MOS-EVID-100`: "It MUST NOT be re-evaluated inside the service, and
       the service MUST NOT be able to influence it."
    2. `Capability.applicable()` (CONTRACT.md section 6). A capability's own opinion about
       its method, in code.

    The declared envelope runs FIRST because it is the party with standing. When both would
    refuse, the rejection a reader gets should name the published clinical claim -- "this
    service is validated to 3.0 mm and this study is 6.0 mm" -- and not an implementation
    detail of a threshold algorithm. Running the capability first would make the
    machine-readable reason depend on which gate happened to be stricter.

    The capability gate is NOT removed. `medos/medos/capabilities/emphysema_laa.py` says why in
    its own words: "an envelope that is only enforced upstream is an envelope that
    disappears when the upstream changes." Two gates, and the union of their refusals.

    MARGINAL DOES NOT STOP THE JOB
    -------------------------------
    `MOS-EVID-101` gives a MARGINAL study under the tenant's default `flag` policy the
    outcome "proceeds" -- with an annotation obligation that `state.envelope_verdict`
    carries forward into the provenance record (`MOS-SAFE-083`'s
    `execution.applicability`). Only `OUT`, or `MARGINAL` under a `reject` tenant, raises.

    NO DECLARED ENVELOPE IS REPORTED, NOT SILENTLY PASSED
    ------------------------------------------------------
    `MOS-EVID-095` requires every ServiceVersion that declares a capability to carry one.
    If none is stored, this step records `envelope: {"declared": false}` in
    `job_steps.detail` and proceeds on the capability gate alone. It does NOT reject: a
    site that has not yet loaded its envelopes would have every job fail, and the honest
    statement is "this study was not checked against a declared envelope", which the step
    detail and the provenance record both then say. `MOS-EVID-095`'s consequence for the
    gap is elsewhere and is the right one -- such a ServiceVersion "MUST NOT be deployable
    in `clinical` mode", which is `MOS-SAFE-036`'s E1 gate, not this step's job.
    """
    assert state.volume is not None
    try:
        order = resolve_capability_order(ctx.deps.registry, ctx.capability_ids)
    except (KeyError, ValueError) as exc:
        raise SystemFailure(
            "capability_resolution_failed",
            {"capability_ids": list(ctx.capability_ids)},
            str(exc),
        ) from exc
    state.order = order

    envelope_detail = _evaluate_declared_envelope(ctx, state)

    verdicts: dict[str, str | None] = {}
    for cid in order:
        capability = ctx.deps.registry[cid]
        verdicts[cid] = capability.applicable(state.volume)

    refused = {cid: reason for cid, reason in verdicts.items() if reason is not None}
    if refused:
        raise EnvelopeRejection(
            "outside_applicability_envelope",
            {
                "refused": refused,
                "series_instance_uid": state.series_instance_uid,
                "modality": state.volume.modality,
                "spacing_class": state.volume.spacing_class,
                "declared_envelope": envelope_detail,
            },
            "no capability in this job applies to the selected series: "
            + "; ".join(f"{cid}={reason}" for cid, reason in sorted(refused.items())),
        )
    return {
        "order": list(order),
        "applicable": sorted(verdicts),
        "clinical_use_mode": ctx.job["clinical_use_mode"],
        "envelope": envelope_detail,
    }


def _evaluate_declared_envelope(
    ctx: StepContext, state: PipelineState
) -> dict[str, Any]:
    """Resolve, evaluate and RECORD the declared envelope. Raises on a rejecting verdict.

    Split out of `step_envelope_check` because it is a different SUBJECT -- chapter 7's
    published claim rather than chapter 5's step plan -- and because the recording half
    (`envelope_decisions`, `MOS-EVID-104`) must happen on the rejecting path too. A
    rejection that is not counted is a rejection a site cannot see, and `MOS-EVID-104` is
    explicit that the share of a site's traffic a capability refuses "is a procurement fact
    the site must be able to see on day one, not a silent drop". So the row is written
    BEFORE the raise, in its own transaction, and the raise is what ends the attempt.
    """
    from medos.safety import repo as safety_repo
    from medos.safety.attributes import attributes_from_paths
    from medos.safety.envelope import evaluate

    envelope_row = safety_repo.resolve_envelope(
        ctx.conn,
        subject_id=str(ctx.job["service_id"]),
        subject_version=str(ctx.job["service_version"]),
    )
    if envelope_row is None:
        return {
            "declared": False,
            "consequence": (
                "this study was NOT checked against a declared ApplicabilityEnvelope; "
                "MOS-EVID-095 requires one and MOS-SAFE-036's E1 gate refuses clinical "
                "promotion of a ServiceVersion without it"
            ),
        }

    attributes = attributes_from_paths(state.instance_paths)
    verdict = evaluate(
        envelope_row.envelope,
        attributes.as_mapping(),
        marginal_policy=safety_repo.tenant_marginal_policy(ctx.conn),  # type: ignore[arg-type]
    )
    state.envelope_verdict = verdict

    decision_id = safety_repo.record_decision(
        ctx.conn,
        verdict,
        envelope_row=envelope_row,
        study_instance_uid=ctx.study_instance_uid,
        series_instance_uid=state.series_instance_uid,
        job_uuid=str(ctx.job["id"]),
    )

    # MOS-DATA-077: an explicitly requested job "MUST always be created, and an
    # `applicability` failure terminates it in `REJECTED` with reason
    # `outside_applicability_envelope`". `raise_if_rejected` raises `EnvelopeViolation`,
    # a `ClinicalRejection`, so the runner takes T7 and writes REJECTED -- the path 0.1.0
    # already built, reused rather than duplicated.
    verdict.raise_if_rejected()

    return {
        "declared": True,
        "envelope_public_id": envelope_row.public_id,
        "envelope_version": envelope_row.envelope.version,
        "envelope_digest": envelope_row.envelope_digest,
        "decision_id": decision_id,
        "zone": verdict.zone,
        "outcome": verdict.outcome,
        "marginal_policy": verdict.marginal_policy,
        "violations": verdict.violations(),
    }


# =====================================================================================
# 4. service_invoke  --  inferring
# =====================================================================================
def step_service_invoke(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """Run the capabilities in dependency order and assemble the `ResultBundle`.

    CONTRACT.md section 7: "`emphysema_laa` depends on `lung_segmentation`'s mask. Express
    that as an explicit step ordering in `worker/steps.py`, not as a hidden import." The
    ordering is `state.order` (resolved in `envelope_check` from
    `medos.capabilities.DEPENDENCIES`) and the hand-off is `bind()` -- so the dependency
    is visible in this loop and nowhere else.

    MOS-EXEC-024: sub-progress is written to `job_steps.detail` on THIS row only, and
    never to `steps_completed`. "The job-level counters stay platform-owned so that a
    misbehaving vendor cannot fabricate platform progress."
    """
    assert state.volume is not None and state.source is not None
    for index, cid in enumerate(state.order):
        ctx.lease.check()
        capability = ctx.deps.registry[cid]
        if hasattr(capability, "bind"):
            capability = capability.bind(state.outcomes)
        state.bound[cid] = capability

        cap_ctx = CapabilityContext(
            job_id=ctx.job_id,
            series_instance_uid=str(state.series_instance_uid),
            source=state.source,
            clinical_use_mode=str(ctx.job["clinical_use_mode"]),
        )
        repo.set_step_detail(
            ctx.conn,
            ctx.job_id,
            "service_invoke",
            {
                "service_phase": "running_capability",
                "capability_id": cid,
                "capabilities_done": index,
                "capabilities_total": len(state.order),
            },
        )
        # ONE call site for both execution modes. `run_capability` is
        # `capability.run(vol, ctx)` for a deterministic capability and the
        # preprocess -> Triton -> postprocess sandwich for a native-mode one
        # (section 15.2.4; chapter 13 section 13.10). The branch lives in
        # medos/medos/inference/dispatch.py so that this executor is unchanged if OQ-10
        # resolves against the InferenceBackend port at G-0.3.0.
        outcome = run_capability(capability, state.volume, cap_ctx, ctx.deps.inference)
        if outcome.capability_id != cid:
            raise BundleInvalid(
                "capability_id_mismatch",
                {"requested": cid, "returned": outcome.capability_id},
                f"{cid} returned an outcome labelled {outcome.capability_id!r}",
            )
        state.outcomes[cid] = outcome

    state.bundle = ResultBundle(outcomes=tuple(state.outcomes[c] for c in state.order))
    return {
        "capabilities_run": list(state.order),
        "findings_per_capability": {
            cid: len(state.outcomes[cid].findings) for cid in state.order
        },
        "label_maps": [cid for cid in state.order if state.outcomes[cid].label_map is not None],
    }


# =====================================================================================
# 5. validate_bundle  --  validating_result
# =====================================================================================
def declared_output_kinds(capability: Capability | None) -> frozenset[str]:
    """What this capability SAYS it produces (`CapabilityMetadata.output_kinds`).

    `frozenset()` for a capability with no metadata, which is the honest answer: an
    undeclared capability has promised nothing, so nothing it returns contradicts a
    promise. The platform must not infer a promise from a capability's name, from its
    class, or from what it happened to return on the last study.

    `metadata` is not on the `Capability` protocol (CONTRACT.md section 6 fixes that
    interface at `capability_id`, `version`, `applicable`, `run` and forbids additions),
    so it is read reflectively. REPORTED: chapter 9's `MOS-SAFE-014` makes `output_kinds`
    a REQUIRED member of a published `ServiceVersion`'s clinical block, and chapter 6
    resolves it at dispatch -- so in the real platform this reads the registry row, not
    the Python object, and `getattr` disappears with the registries CONTRACT.md section 0
    removed from this slice.
    """
    metadata = getattr(capability, "metadata", None)
    kinds = getattr(metadata, "output_kinds", ())
    return frozenset(str(k).upper() for k in kinds)


def step_validate_bundle(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """MOS-IMG-141: the platform independently re-validates what the service returned.

    Every check below is one the writer would otherwise discover with a stack trace, or
    -- worse -- not discover at all:

      * a label map that is not uint8, or not on the SOURCE grid, relabels or misplaces
        every segment in the SEG (CONTRACT.md section 5 invariant 2);
      * `segments` shorter than the largest label value writes a SEG segment with no
        coded meaning;
      * a non-finite measurement passes silently through numeric(18,6) as an error;
      * a `source_sop_instance_uids` set that is not a subset of what was consumed makes
        the SR's evidence sequence reference instances this job never read (MOS-IMG-121).

    AND the one that moved here from the writer. `plan_outputs` used to fail the job with
    `no_label_map_in_bundle` whenever no capability returned a label map, which caught a
    real defect -- a segmentation capability returning nothing -- by catching every
    negative detection result as well. The two are distinguishable, but only with a fact
    the writer does not have: the capability's DECLARED `output_kinds` (`MOS-SAFE-014`).
    A capability that declares `SEG` and returns no label map has not produced the output
    it declared, which `MOS-SVC-011` requires be "reported as a per-capability rejection,
    never as silence"; a capability that declares no `SEG` and returns no label map has
    done exactly what it said it would.

    It is `BundleInvalid` -> `FAILED` and not a rejection, because this build's
    `CapabilityOutcome` has no per-capability rejection channel to report it through --
    `MOS-SVC-095`'s `capability_outcomes[].outcome` / `.reason_code` pair is REPORTED as
    missing from CONTRACT.md section 5's return contract. `FAILED` is the correct terminal
    state of the two available: a capability returning something other than what it
    declared is a service defect (`MOS-IMG-142`: "a service defect, not a clinical
    outcome"), not a statement about the study. What it MUST NOT be is silence, and what
    it must not do is swallow the negative result alongside it.
    """
    assert state.bundle is not None and state.source is not None
    source_grid = state.source.hu_array.shape if state.source.hu_array is not None else None
    consumed = set(state.source.sop_instance_uids)
    checks = 0

    if not state.bundle.outcomes:
        raise BundleInvalid("empty_bundle", {}, "the bundle carries no outcomes")

    for outcome in state.bundle.outcomes:
        checks += 1
        declared = declared_output_kinds(ctx.deps.registry.get(outcome.capability_id))
        if "SEG" in declared and outcome.label_map is None:
            raise BundleInvalid(
                "declared_output_not_produced",
                {
                    "capability_id": outcome.capability_id,
                    "declared_output_kinds": sorted(declared),
                    "missing": "SEG",
                },
                f"{outcome.capability_id} declares SEG in its output_kinds "
                "(MOS-SAFE-014) and returned no LabelMap. MOS-SVC-011 forbids reporting "
                "a declared output that was not produced as silence; a capability whose "
                "honest answer is 'nothing to outline' must declare that by not "
                "declaring SEG, not by returning less than it declared.",
            )
        if not set(outcome.source_sop_instance_uids) <= consumed:
            raise BundleInvalid(
                "evidence_not_consumed",
                {
                    "capability_id": outcome.capability_id,
                    "n_claimed": len(outcome.source_sop_instance_uids),
                    "n_consumed": len(consumed),
                },
                f"{outcome.capability_id} claims evidence instances this job did not "
                "consume (MOS-IMG-121)",
            )
        lm = outcome.label_map
        if lm is not None:
            if lm.array.dtype != np.uint8:
                raise BundleInvalid(
                    "label_map_dtype",
                    {"capability_id": outcome.capability_id, "dtype": str(lm.array.dtype)},
                    "LabelMap.array MUST be uint8 (CONTRACT.md section 5)",
                )
            if source_grid is not None and lm.array.shape != source_grid:
                raise BundleInvalid(
                    "label_map_not_source_grid",
                    {
                        "capability_id": outcome.capability_id,
                        "shape": list(lm.array.shape),
                        "source_grid": list(source_grid),
                    },
                    "LabelMap.array is not on the SOURCE grid; MOS-IMG-032/033 exist to "
                    "prevent exactly this",
                )
            top = int(lm.array.max())
            if top > len(lm.segments):
                raise BundleInvalid(
                    "label_map_segment_count",
                    {
                        "capability_id": outcome.capability_id,
                        "max_label": top,
                        "n_segments": len(lm.segments),
                    },
                    f"label value {top} has no entry in LabelMap.segments",
                )
        for finding in outcome.findings:
            for m in finding.measurements:
                checks += 1
                if not np.isfinite(m.value):
                    raise BundleInvalid(
                        "measurement_not_finite",
                        {"capability_id": outcome.capability_id, "code": m.name.code},
                        "a non-finite measurement cannot be stored or rendered",
                    )
                if not m.unit or not m.name.code or not m.name.scheme:
                    raise BundleInvalid(
                        "measurement_uncoded",
                        {"capability_id": outcome.capability_id, "unit": m.unit},
                        "MOS-STORE-282: scheme, code and unit are NOT NULL, so 'a bare "
                        "number with a free-text label' cannot be stored",
                    )
    return {"outcomes": len(state.bundle.outcomes), "checks": checks}


# =====================================================================================
# 6. write_dicom  --  writing_dicom
# =====================================================================================
def producing_model_version(
    registry: Mapping[str, Capability], bundle: ResultBundle
) -> str:
    """The version of the model that produced the objects this job is about to write.

    This used to read `registry["lung_segmentation"].version`, and that line was wrong in
    two independent ways at once.

    It was a `KeyError` in any deployment whose registry does not happen to contain that
    capability -- the ordinary case for a vendor shipping ONE service (MOS-REL-020). Seven
    of the eight steps would succeed and the job would be reported as an internal failure
    naming a capability the operator never requested, so the report was misleading about
    its own cause as well as spurious.

    And where it SUCCEEDED it was still wrong. `model_version` is not decoration: it is in
    MOS-IMG-062's UID seed, so it seeds every SeriesInstanceUID and SOPInstanceUID this job
    writes, and MOS-IMG-085 makes "same UID implies same declared inputs" the invariant the
    retry path depends on. A second capability's SEG stamped with a first capability's
    version means two versions of the producing method derive the same UIDs and are
    indistinguishable in `dicom_objects.derivation_inputs` (MOS-STORE-286) -- while the
    provenance record written by `result_rows.py` for the SAME job says
    `execution.models[] = [{model_id: <capability_id>, version: <capability.version>}]`
    (MOS-SAFE-083 section D, MOS-SVC-098). One job, two provenance records, contradicting
    each other.

    So the question is answered per RESULT, the way `result_rows.py` already answers it:
    `writer.identity.producing_outcome` names the outcome the written objects are
    attributable to -- the label map's owner when there is one, and read that function for
    what it does and reports when there is not -- and that capability's declared `version`
    is the version that produced it. For a `lung_segmentation` or an `emphysema_laa` job
    this returns exactly the string the old line returned -- `emphysema_laa` contributes no
    label map of its own -- so no UID any existing deployment has already written moves.

    NO `.get()` AND NO FALLBACK, deliberately. A missing entry here means the bundle holds
    an outcome for a capability this worker's registry does not contain, which is a defect
    in composition and not a condition to tolerate: substituting some other capability's
    version would make the provenance record silently false rather than loudly absent, and
    a provenance record that is quietly wrong is the one failure mode chapter 9 cannot
    detect after the fact.
    """
    owner = producing_outcome(bundle)
    capability = registry.get(owner.capability_id)
    if capability is None:
        raise SystemFailure(
            "producing_capability_not_in_registry",
            {
                "capability_id": owner.capability_id,
                "registry": sorted(registry),
            },
            f"these objects are attributable to {owner.capability_id!r}, which this "
            "worker's registry does not contain, so the version that produced them "
            "cannot be stated. MOS-IMG-062 seeds every derived UID with it and "
            "MOS-STORE-286 records it in "
            "dicom_objects.derivation_inputs; refusing to write objects that would "
            "misstate their own provenance.",
        )
    return str(capability.version)


def step_write_dicom(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """Allocate the derived identity, then build the SEG **and** the SR.

    MOS-EXEC-059 / MOS-EXEC-023: this step is skippable, and the condition is "the derived
    series already exists". Deterministic UIDs are what make the question askable at all
    -- this attempt would write the same SeriesInstanceUIDs the last one did -- and
    skipping is how a retry after a crash between STOW and commit reconciles instead of
    duplicating (MOS-IMG-080/081, MOS-EXEC-061: recovery past the store is forward-only
    because platform policy denies deleting a study).

    Both objects are built here and neither is stored. The STOW is the next step, and the
    separation is what lets `store_dicom` be the single mutating boundary.

    WHICH objects, though, is `plan.writes_seg` / `plan.writes_sr` and not a constant.
    This step used to build a SEG unconditionally and ignore `jobs.requested_outputs`
    entirely, so a job asking for `{SR}` failed for want of a SEG, and a capability whose
    honest answer was "I ran and there is nothing to draw" could not be written at all --
    `plan_outputs` raised `SystemFailure` and the job reported a platform malfunction for
    the modal outcome of every detector. Chapter 5 section 5.6.2 makes `requested_outputs`
    the column that "drives the step plan's write/store steps"; this is that.
    """
    assert state.bundle is not None and state.source is not None
    identity = build_job_identity(
        job_id=ctx.job_id,
        tenant_id=SLICE_TENANT_ID,
        service_id=str(ctx.job["service_id"]),
        service_version=str(ctx.job["service_version"]),
        study_instance_uid=ctx.study_instance_uid,
        capability_ids=ctx.capability_ids,
        requested_outputs=tuple(ctx.job["requested_outputs"]),
        # MOS-IMG-136's spelling is upper case; chapter 5 stores it lower case.
        clinical_use_mode=str(ctx.job["clinical_use_mode"]).upper(),
        # Per-RESULT, never per-deployment: see `producing_model_version` above.
        model_version=producing_model_version(ctx.deps.registry, state.bundle),
        expected_idempotency_key=str(ctx.job["idempotency_key"]),
    )
    plan = plan_outputs(identity, state.bundle, ctx.deps.concepts)
    state.plan = plan

    # MOS-EXEC-059 reconciles against the series this attempt INTENDS to write. Asking
    # about a series the plan excludes would make the skip unreachable forever -- a
    # negative job writes no SEG, so `series_exists` on its SEG UID is false on every
    # attempt, and a retry after a successful STOW of the SR would re-STOW it.
    gateway = ctx.deps.gateway
    planned_series = [
        (kind, series_uid, sop_uid)
        for kind, series_uid, sop_uid, on in (
            ("SEG", plan.seg_series_instance_uid, plan.seg_sop_instance_uid, plan.writes_seg),
            ("SR", plan.sr_series_instance_uid, plan.sr_sop_instance_uid, plan.writes_sr),
        )
        if on
    ]
    omitted_detail = [o.to_dict() for o in plan.omitted_outputs]
    if not planned_series:
        # Every requested output was omitted for a recorded reason. The Result still
        # exists and still carries the capability's findings; what does not exist is a
        # DICOM object. `_SkipStep` rather than a failure, because nothing malfunctioned
        # -- MOS-EXEC-020 is exactly the "this step is a no-op on this attempt" channel.
        #
        # `reused_existing_objects` is deliberately NOT set: it means "the PACS already
        # holds what this attempt would have written" (MOS-EXEC-059), and saying that of a
        # job that was never going to write anything would put a false reconciliation
        # claim in `store_dicom`'s skip reason. `store_dicom` reaches the same conclusion
        # from the same plan, one step later, and says so in its own words.
        raise _SkipStep(
            "this job writes no DICOM object: "
            + "; ".join(f"{o.kind} {o.reason_code}" for o in plan.omitted_outputs)
        )
    if all(
        gateway.series_exists(ctx.study_instance_uid, series_uid)
        for _kind, series_uid, _sop in planned_series
    ):
        state.reused_existing_objects = True
        state.stored_sop_instance_uids = tuple(sop for _k, _s, sop in planned_series)
        raise _SkipStep(
            "every derived series this job writes already exists in the PACS ("
            + ", ".join(f"{kind} {series}" for kind, series, _sop in planned_series)
            + "); MOS-EXEC-059"
        )

    source_datasets = [pydicom.dcmread(str(p)) for p in state.source.paths]
    seg = (
        build_seg(plan, source_datasets, state.source, ctx.deps.concepts)
        if plan.writes_seg
        else None
    )
    sr = build_sr(plan, seg, source_datasets, ctx.deps.concepts) if plan.writes_sr else None
    state.seg = seg
    state.sr = sr

    return {
        "manifest": plan.manifest(frame_count=len(state.source.sop_instance_uids)),
        "written_kinds": list(plan.written_kinds),
        "omitted_outputs": omitted_detail,
        "seg_sop_instance_uid": plan.seg_sop_instance_uid if plan.writes_seg else None,
        "sr_sop_instance_uid": plan.sr_sop_instance_uid if plan.writes_sr else None,
        "segments": [
            {
                "structure": s.structure,
                "segment_number": s.segment_number,
                "n_voxels": s.n_voxels,
                "measurements": len(s.measurements),
            }
            for s in plan.segments
        ],
        "empty_segments": list(plan.empty_segments),
    }


# =====================================================================================
# 7. store_dicom  --  storing_dicom
# =====================================================================================
def step_store_dicom(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """STOW the SEG and the SR, in one batch, and read `FailedSOPSequence`.

    EVERY object the plan holds, never a subset. This is the gap the round-trip path had:
    it stored a SEG and no SR, so OHIF rendered a shape with no numbers and the weeks 1-2
    exit check -- "the measurement reported in the SR equals the measurement recomputed
    from the stored SEG" (docs/spec/15-delivery.md section 15.2.3) -- had nothing to
    compare against. What the plan holds is `write_dicom`'s decision and is `{SEG, SR}`
    for any job with a region to outline that asked for both; a job with neither is a
    `_SkipStep` here, not a partial store.

    MOS-EXEC-061: this is the commit point of the pipeline and its last mutating step.
    Recovery past it is forward-only; there is no compensating delete, because deleting a
    study is denied to the platform by chapter 9's default-DENY table. Hence the
    `lease.check()` immediately before the POST: it is the last instant at which a worker
    that has lost its lease can still be stopped from writing into the PACS
    (MOS-EXEC-028).

    MOS-IMG-084: a 200 with a non-empty `FailedSOPSequence` is a PARTIAL store and is a
    failure, and `DicomWebGateway.assert_stored` is deliberately the thing that raises --
    `store_files` returns the receipts without judging them, because "is this job FAILED"
    is the worker's decision and not the transport's.

    The acceptance check is run PER DERIVED SERIES, because the SEG and the SR are two
    series (MOS-IMG-073 gives them different SeriesNumber bands), and MOS-IMG-152's set
    equality is a property of a series. It also catches MOS-IMG-083's surplus: an
    unexpected instance in a derived series means a UID collision or a concurrent writer,
    and completing anyway would attach someone else's pixels to this job's provenance.
    """
    if state.reused_existing_objects:
        raise _SkipStep("objects already present in the PACS; nothing to store (MOS-EXEC-059)")
    assert state.plan is not None
    plan = state.plan

    # WHICH objects, from the plan. "BOTH objects, always" above is still the rule for a
    # job that has both to write; it is not a rule about what every job has. The job that
    # legitimately has no SEG -- nothing to outline, or SEG not requested -- stores the
    # objects it does have, and `plan.omitted_outputs` says what is absent and why.
    planned: list[tuple[str, str, str, Dataset]] = []
    for kind, series_uid, sop_uid, dataset in (
        ("SEG", plan.seg_series_instance_uid, plan.seg_sop_instance_uid, state.seg),
        ("SR", plan.sr_series_instance_uid, plan.sr_sop_instance_uid, state.sr),
    ):
        if dataset is not None:
            planned.append((kind, series_uid, sop_uid, dataset))
    if not planned:
        raise _SkipStep(
            "this job writes no DICOM object; nothing to store "
            + "; ".join(f"{o.kind} {o.reason_code}" for o in plan.omitted_outputs)
        )

    # Serialised to the attempt's work directory first. Two reasons: `store_files` reads
    # Part 10 bytes, and a file on disk is what makes `object_digest` and `size_bytes`
    # recordable without re-encoding the dataset a second time and hoping the two encodings
    # agree.
    ctx.work_dir.mkdir(parents=True, exist_ok=True)
    paths: list[tuple[str, str, str, Path]] = []
    for kind, series_uid, sop_uid, dataset in planned:
        path = ctx.work_dir / f"{sop_uid}.dcm"
        dataset.save_as(str(path), enforce_file_format=True)
        paths.append((kind, series_uid, sop_uid, path))
    state.object_files = {sop_uid: path for _k, _s, sop_uid, path in paths}

    # MOS-SAFE-039 (enforcement point E3), literally: "The check MUST run on the assembled
    # dataset immediately before STOW-RS". Not on `state.seg` / `state.sr` -- on the
    # datasets re-read from the files that are about to be POSTed, because the bytes on
    # disk are what the PACS will receive and a serialisation step is a place a marking can
    # be lost. `assert_marked` raises `MarkingAbsent` -> `FAILED`, `error.code =
    # safety_marking_absent`, and nothing is stored.
    #
    # This is the second call site of the same function; `medos.writer.seg` / `.sr` call it
    # too. MOS-SAFE-039 asks for one FUNCTION shared by the writers, not one call, and the
    # two sites answer two different questions: "did the writer build it right" and "is
    # what we are about to send marked".
    mode = str(ctx.job["clinical_use_mode"])
    for kind, _series_uid, _sop_uid, path in paths:
        assert_marked(
            pydicom.dcmread(str(path), stop_before_pixels=True),
            object_kind=kind,  # type: ignore[arg-type]
            mode=mode,
        )

    ctx.lease.check()  # MOS-EXEC-028: the last instant before the PACS is mutated
    gateway = ctx.deps.gateway
    receipts = gateway.store_files(
        [path for _k, _s, _sop, path in paths], study_instance_uid=ctx.study_instance_uid
    )
    # MOS-SAFE-085. `assert_stored` is a QIDO-RS re-read of the derived series
    # (MOS-IMG-152), so THIS is the instant at which landing was verified, and the count
    # it returns is the count that was verified. Recording the timestamp anywhere earlier
    # -- at the STOW 200, say -- would be recording a claim as a verification, which is
    # the distinction MOS-SAFE-085 exists to draw.
    state.verified_counts = {
        series_uid: len(
            gateway.assert_stored(receipts, ctx.study_instance_uid, series_uid, [sop_uid])
        )
        for _kind, series_uid, sop_uid, _path in paths
    }
    state.qido_verified_at = datetime.now(UTC)
    state.stow_http_status = max((r.http_status for r in receipts), default=None)
    state.stow_endpoint = _endpoint_of(gateway)

    state.stored_sop_instance_uids = tuple(sop for _k, _s, sop, _p in paths)

    # MOS-SEC-089 / MOS-SEC-148: a write into a patient's study is class `phi` and is
    # audited. The failure path needs no call of its own -- `assert_stored` raises above
    # this line and the attempt ends -- and a job that touched the PACS and then crashed
    # still leaves the `study.read_pixels` pair, so the trail never goes quiet on the
    # PHI-touching half of the work.
    audit.record(
        ctx.conn,
        action="study.write",
        action_class="phi",
        actor=audit.Actor(kind="workload", id=ctx.worker_id, auth="internal"),
        resource=audit.Resource.study(current_tenant(), ctx.study_instance_uid),
        outcome="allow",
        pep="gateway.store",
        trace_id=str(ctx.job["trace_id"]),
        request_id=audit.new_request_id(),
        job_id=ctx.job_id,
        on_behalf_of=audit.OnBehalfOf.from_job(ctx.job),
        study_instance_uid=ctx.study_instance_uid,
        detail={
            "objects_stored": len(state.stored_sop_instance_uids),
            "sop_instance_uids": list(state.stored_sop_instance_uids),
            "series_instance_uids": [series for _k, series, _sop, _p in paths],
            "qido_verified": True,
            "instance_counts_verified": dict(state.verified_counts),
            "stow_http_status": state.stow_http_status,
        },
    )
    return {
        "http_statuses": [r.http_status for r in receipts],
        "bytes_sent": sum(r.bytes_sent for r in receipts),
        "objects": [
            {
                "kind": kind,
                "series_instance_uid": series_uid,
                "sop_instance_uid": sop_uid,
                "size_bytes": path.stat().st_size,
            }
            for kind, series_uid, sop_uid, path in paths
        ],
        "omitted_outputs": [o.to_dict() for o in plan.omitted_outputs],
    }


# =====================================================================================
# 8. persist_result  --  persisting
# =====================================================================================
def step_persist_result(ctx: StepContext, state: PipelineState) -> dict[str, Any]:
    """Build the `results` rows. The COMMIT is the runner's -- see `execute_plan`.

    CONTRACT.md section 8, NON-NEGOTIABLE: "the results row and the terminal state
    transition MUST be written in ONE transaction." This function therefore writes
    NOTHING; it only assembles the rows. `execute_plan` opens the single transaction that
    marks this step succeeded, inserts the results and performs T8, so there is no window
    in which a result exists for a job that is not COMPLETED, or the reverse.
    """
    assert state.bundle is not None and state.source is not None and state.volume is not None
    rows: list[ResultRow] = []
    finished = datetime.now(UTC)
    series_uid = str(state.series_instance_uid)
    geometry = geometry_record(state.volume, state.source)
    # schema.sql CHECKs `input_pixel_digest ~ '^[0-9a-f]{64}$'`; CanonicalVolume spells it
    # `sha256:<hex>` (MOS-IMG-036). The algorithm is not dropped -- it is the column's
    # documented one -- only the prefix.
    pixel_digest = state.volume.pixel_digest.split(":", 1)[-1]

    for cid in state.order:
        outcome = state.outcomes[cid]
        capability = state.bound.get(cid, ctx.deps.registry[cid])
        method_detail = (
            capability.method_detail(
                CapabilityContext(
                    job_id=ctx.job_id,
                    series_instance_uid=series_uid,
                    source=state.source,
                    clinical_use_mode=str(ctx.job["clinical_use_mode"]),
                )
            )
            if hasattr(capability, "method_detail")
            else {}
        )
        measurements: list[MeasurementRow] = []
        for finding_index, finding in enumerate(outcome.findings):
            for m in finding.measurements:
                measurements.append(
                    MeasurementRow(
                        concept_scheme=m.name.scheme,
                        concept_code=m.name.code,
                        concept_display=m.name.meaning,
                        value=float(m.value),
                        ucum_unit=m.unit,
                        source_series_instance_uid=series_uid,
                        finding_index=finding_index,
                        # CONTRACT.md section 5 / MOS-STORE-283. Always 'source' here,
                        # because `to_bundle_measurement` refuses to convert anything else.
                        geometry_space="source",
                        method_detail=method_detail,
                    )
                )
        # MOS-SAFE-082: exactly one provenance record per Result, written in the SAME
        # transaction as the Result row and the terminal Job transition. It is BUILT here,
        # beside the row it belongs to, and WRITTEN by `repo.save_result` one statement
        # after the `results` INSERT -- `result_id` is the only member that cannot be
        # known before that INSERT returns, and it is the only one `save_result` fills in.
        record = provenance_record(
            state=state,
            outcome=outcome,
            capability=ctx.deps.registry[cid],
            job=ctx.job,
            tenant_id=current_tenant(),
            result_id="",
            provenance_id=new_ulid("prv"),
            worker_version=ctx.deps.worker_version,
            runtime_version=ctx.deps.runtime_version,
            platform_commit=ctx.deps.platform_commit,
            preprocessing_version=PREPROCESSING_SPEC_VERSION,
            preprocessing_digest=preprocessing_spec_digest(),
            finished_at=finished,
            gateway_summary={
                # MOS-STORE-251 pins the de-identification policy version into provenance.
                # This slice pulls from a research PACS with no de-identification stage
                # (CONTRACT.md section 0), so the honest record is that none was applied
                # and that nothing was checked -- NOT a profile name that was never run.
                "deid_profile": None,
                "deid_policy_version": None,
                "uid_map_id": None,
                "uid_remap_applied": False,
                "burned_in_phi_check": "skipped",
                "gateway_version": GATEWAY_VERSION,
            },
        )
        rows.append(
            ResultRow(
                capability_id=cid,
                capability_version=str(ctx.deps.registry[cid].version),
                provenance=record,
                result_kind="segmentation" if outcome.label_map is not None else "measurement",
                findings=[finding_json(f) for f in outcome.findings],
                input_series_uids=(series_uid,),
                input_instance_uids=tuple(outcome.source_sop_instance_uids),
                preprocessing_version=PREPROCESSING_SPEC_VERSION,
                preprocessing_digest=preprocessing_spec_digest(),
                worker_version=ctx.deps.worker_version,
                runtime_version=ctx.deps.runtime_version,
                measurements=tuple(measurements),
                dicom_objects=dicom_object_rows(state, outcome),
                geometry=geometry,
                clinical_use_mode=str(ctx.job["clinical_use_mode"]),
                input_pixel_digest=pixel_digest,
                platform_commit=ctx.deps.platform_commit,
                started_at=state.started_at,
                finished_at=finished,
            )
        )
    state.result_rows = tuple(rows)
    return {
        "results": len(rows),
        "measurements": sum(len(r.measurements) for r in rows),
        "dicom_objects": sum(len(r.dicom_objects) for r in rows),
    }


# =====================================================================================
# The plan
# =====================================================================================
@dataclass(frozen=True)
class Step:
    key: str
    run: Callable[[StepContext, PipelineState], dict[str, Any]]


STEPS: tuple[Step, ...] = (
    Step("fetch_series", step_fetch_series),
    Step("build_volume", step_build_volume),
    Step("envelope_check", step_envelope_check),
    Step("service_invoke", step_service_invoke),
    Step("validate_bundle", step_validate_bundle),
    Step("write_dicom", step_write_dicom),
    Step("store_dicom", step_store_dicom),
    Step("persist_result", step_persist_result),
)


def execute_plan(ctx: StepContext) -> PipelineState:
    """Run the eight steps and commit the terminal transaction. Returns the final state.

    Every step writes a `job_steps` row transition and a `job_events` row: `start_step`,
    then exactly one of `finish_step` / `skip_step` / `fail_step`, each of which emits a
    `job.step_changed` event through `repo._emit_step_event`. `jobs.phase` follows from the
    rollup trigger on `job_steps` (chapter 5 section 5.4.2) and is never written by this
    module -- MOS-EXEC-024 keeps the job-level counters platform-owned, and a runner that
    set `phase` directly would be able to disagree with the step rows.

    The last step is special and that is the point of the whole file: `persist_result`
    ASSEMBLES the rows, and the transaction below writes them together with T8
    (CONTRACT.md section 8, MOS-EXEC-057). A crash anywhere inside it leaves the job
    RUNNING with no results, its lease expires, and the reclaimer requeues it -- which is
    recoverable. The alternative -- results committed, transition not -- is a COMPLETED-ish
    job no state machine can describe.
    """
    state = PipelineState()
    for step in STEPS:
        ctx.lease.check()
        repo.start_step(ctx.conn, ctx.job_id, step.key)
        try:
            detail = step.run(ctx, state)
        except _SkipStep as skip:
            repo.skip_step(ctx.conn, ctx.job_id, step.key, skip.reason)
            continue
        except MedosError as exc:
            repo.fail_step(
                ctx.conn,
                ctx.job_id,
                step.key,
                error_code=exc.reason_code,
                error_detail=exc.message[:2000],
            )
            raise
        except SystemExit as exc:
            # Spike-lifted code signals fatal conditions with SystemExit, which derives
            # from BaseException and would otherwise walk straight past every handler in
            # the runner and kill the worker process mid-lease. Converted here, once, at
            # the only place that can see which step raised it.
            repo.fail_step(
                ctx.conn,
                ctx.job_id,
                step.key,
                error_code="internal",
                error_detail=str(exc)[:2000],
            )
            raise SystemFailure(
                "step_raised_systemexit",
                {"step_key": step.key},
                f"{step.key} raised SystemExit: {exc}",
            ) from exc
        except Exception as exc:
            repo.fail_step(
                ctx.conn,
                ctx.job_id,
                step.key,
                error_code="internal",
                error_detail=f"{type(exc).__name__}: {exc}"[:2000],
            )
            raise SystemFailure(
                "step_failed",
                {"step_key": step.key, "exception": type(exc).__name__},
                f"{step.key} raised {type(exc).__name__}: {exc}",
            ) from exc

        if step.key == "persist_result":
            ctx.lease.check()
            # THE one transaction (CONTRACT.md section 8). `complete_job_with_results`
            # calls `queue.complete()` inside it, which is fenced (MOS-EXEC-027) and
            # deletes the queue row (MOS-STORE-274).
            with tenant_tx(ctx.conn):
                repo.finish_step(ctx.conn, ctx.job_id, step.key, detail=detail)
                state.result_ids = tuple(
                    repo.complete_job_with_results(
                        ctx.conn, ctx.queue, ctx.job_id, ctx.worker_id, state.result_rows
                    )
                )
                # Inside the same transaction: the STOW already succeeded, so a row that
                # says `pending` on a COMPLETED job would be a lie the next reader has no
                # way to resolve.
                for sop_uid in state.stored_sop_instance_uids:
                    repo.mark_dicom_stored(ctx.conn, sop_uid, state="stored")
        else:
            repo.finish_step(ctx.conn, ctx.job_id, step.key, detail=detail)
    return state
