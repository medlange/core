# SPDX-License-Identifier: Apache-2.0
"""`POST /api/v1/jobs` and `GET /api/v1/jobs/{job_id}`. CONTRACT.md section 1 and 9.

CONTRACT.md section 9, verbatim:

    - `POST /api/v1/jobs` -> `202` `{"job_id": "...", "state": "QUEUED"}`
      body: `{"study_instance_uid": "...", "capabilities": [...]}`
      Accepts an `Idempotency-Key` header. **That header MUST NOT feed UID derivation** --
      the UID seed is derived server-side from job identity (chapter 4). This was a
      register defect.
    - `GET /api/v1/jobs/{job_id}` -> job with `state`, `phase`, `steps_completed`,
      `steps_total`, `rejection` (when `REJECTED`), `results[]`, `provenance`

TWO ENVELOPES ON `POST /api/v1/jobs`, AND WHY
----------------------------------------------
The body quoted above is CONTRACT.md section 9's and is what this service shipped.
Chapter 10 `MOS-API-043` fixes a DIFFERENT body -- `{target, input: {...}}` -- and
`MOS-API-049` fixes the status field as `status`, not `state`. Chapter 19 `MOS-UI-021a`
names the divergence and rules that it "MUST be closed in the surface, not in Chapter
10". It cannot be closed in the surface alone: a viewer that starts sending Chapter 10's
body to a server that forbids unknown members gets a `400`. So this module accepts BOTH
envelopes -- each closed, mixing them refused -- and answers with `status` beside `state`.
The OHIF extension now sends Chapter 10's; the old envelope stays accepted because every
other caller in the tree sends it and removing it is a separate breaking change.

One reconciliation is NOT clean and is recorded rather than hidden: `MOS-API-043`'s
`target` names exactly one capability (or one service version), while this slice's job
runs a capability SET (`jobs.capability_ids`, `UNIQUE (job_id, capability_id)`).
`target.kind: "capability"` therefore maps to a one-element set and
`target.kind: "service_version"` to every capability that service version implements.
Chapter 10 has no member for a per-job capability subset; see
`docs/spec/99-known-inconsistencies.md` and this component's report.

THE REGISTER DEFECT, AND WHY THIS FILE IS WHERE IT IS PREVENTED
---------------------------------------------------------------
`MOS-API-029`: the client's `Idempotency-Key` "MUST be persisted as
`jobs.request_idempotency_key`, MUST NOT be written to `jobs.idempotency_key`, and MUST
NOT participate in DICOM UID derivation."

The failure mode is concrete. `derive_uid` (MOS-IMG-062) seeds every generated
SOPInstanceUID from the job's identity tuple. If a client-settable string entered that
tuple, two callers submitting different studies under the same `Idempotency-Key` would
mint the SAME SOPInstanceUID -- a cross-study DICOM identity collision, which a PACS
resolves by overwriting one patient's segmentation with another's. Nothing downstream
detects it, because both objects are internally consistent.

The control here is structural, not a code review rule. The header reaches exactly one
field, `JobSpec.request_idempotency_key`, and `JobSpec.idempotency_key()` builds
`derive_idempotency_key`'s material from a keyword-only argument list that has no such
parameter (MOS-EXEC-054). This handler therefore CANNOT leak it even by mistake; the
test `test_client_idempotency_key_never_reaches_uid_derivation` pins that by submitting
the same body under two different header values and asserting one identical derived key.

Thinness
--------
CONTRACT.md section 0: "the HTTP layer MUST stay thin -- no business logic in handlers."
Each handler here does three things and nothing else -- validate the input, call
`medos.db.repo` / `medos.db.queue`, shape the response. Specifically it does NOT:
select series, derive a UID, choose a job state, open a transaction, or decide a retry.
`create_job_queued()` owns the one-transaction guarantee (MOS-EXEC-034) and
`job_transition()` owns the state machine (MOS-EXEC-011).

Spec: MOS-API-001, MOS-API-008, MOS-API-023, MOS-API-029, MOS-API-036, MOS-API-043,
MOS-API-045, MOS-API-046, MOS-API-047, MOS-API-048, MOS-API-049, MOS-API-051,
MOS-API-053, MOS-API-054, MOS-API-111, MOS-EXEC-034, MOS-EXEC-053, MOS-EXEC-054,
MOS-REL-051, CONTRACT.md sections 7 and 9.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Depends, Header, Path, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request

from medos.api import db_connection
from medos.api.authz import actor_of, require
from medos.api.problems import (
    TRACE_HEADER,
    build_problem,
    json_safe,
    normalize_problem,
    problem_response,
    trace_id_of,
)

# Imported as the MODULE and at module level, both on purpose. At module level because
# this handler cannot admit a job without knowing what this deployment serves, so an
# import that fails must stop the process rather than leave the question to be answered
# some other way at request time; as the module so the call below is late-bound and there
# is no second copy of the answer for a caller to reach.
from medos.capabilities import providers as capability_providers
from medos.db import repo
from medos.db.queue import DEFAULT_SERVICE_ID, PostgresJobQueue

__all__ = [
    "router",
    "JobCreateRequest",
    "JobCreateResponse",
    "JobTarget",
    "JobInput",
    "SLICE_SERVICE_VERSION_ID",
    "JOB_ID_RE",
    "reject_bad_job_id",
    "job_not_found_problem",
]

router = APIRouter(prefix="/api/v1", tags=["jobs"])

# MOS-API-111: `job_` then a 26-character Crockford base32 ULID in UPPER case (Crockford
# omits I, L, O and U). Case-sensitive: a lowercase form MUST NOT be accepted in a path
# segment. Same expression as the `public_id` CHECK in schema.sql.
JOB_ID_RE = re.compile(r"^job_[0-9A-HJKMNP-TV-Z]{26}$")

# MOS-API-023. Also the CHECK on `jobs.request_idempotency_key`; validated here so a bad
# header is a `400` with a pointer rather than a constraint violation surfacing as `500`.
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._~-]{1,255}$")

# The `study_instance_uid` CHECK in schema.sql, restated at the boundary for the same
# reason. A DICOM UID is digits and dots, at most 64 characters (PS3.5 section 9.1).
DICOM_UID_RE = re.compile(r"^[0-9.]{1,64}$")

# There is deliberately NO list of capability ids in this module. See
# `known_capability_ids()` below for why the one that used to be here was a hazard rather
# than a safety net.

# MOS-EXEC-023's plan is eight steps; `requested_outputs` is bounded by the CHECK on
# `jobs.requested_outputs`.
ALLOWED_OUTPUTS: frozenset[str] = frozenset({"SEG", "SR", "SC"})

# `MOS-API-044`: when `target.kind` is `service_version`, `target.id` is a `ServiceVersion`
# id. Chapter 6's registry -- which mints `sv_01J8ZK...` ids -- is 0.3.0 and CONTRACT.md
# section 0 removes it from this slice, so the deployment's ONE service version is named
# here by the `service_id/service_version` pair the `jobs` row already carries
# (`repo.JobSpec.service_id` / `.service_version`). Stated rather than hidden: this is a
# stand-in for a registry id, it is the only value this deployment accepts, and an
# unknown one is a `400` naming what is accepted rather than a silent substitution.
SLICE_SERVICE_VERSION_ID = "medos.slice/0.1.0"

# MOS-API-046: the polling hint returned with the `202`.
RETRY_AFTER_SECONDS = 2


def known_capability_ids() -> frozenset[str]:
    """The capability ids this deployment will accept.

    Resolved at call time, not import time, so the registry is read as it is when the
    request arrives rather than as it was when the process started.

    THE SAME RESOLVER THE WORKER USES, and that is the whole substance of this function.
    It used to read the module singleton `medos.capabilities.REGISTRY` directly, so a
    deployment that had configured a fourth capability -- which the worker WOULD serve --
    was refused here at submit with `capability_not_supported`. Reading a different answer
    than the process that has to execute the job is the defect; whether this particular
    list was too short or too long is a symptom.

    `medos.capabilities.providers.capability_ids()` is that one answer, and it is also
    what `medos.worker.steps.WorkerDeps.registry` defaults to. A configured provider that
    is broken raises `CapabilityProviderError` rather than degrading to the platform three
    -- but it never gets this far in a healthy process, because `create_app()` resolves
    once at startup and refuses to serve at all (see `medos.api.app`).

    AND THERE IS NO FALLBACK LIST, WHICH IS THE SECOND HALF OF THE SAME RULE.
    This function used to end `except ModuleNotFoundError: return SLICE_CAPABILITY_IDS` --
    a frozenset of the original three, exported in `__all__`. That guard was written when
    `medos.capabilities` was a sibling deliverable that might not have landed yet; the
    resolver now lives INSIDE that package, so the branch could only fire on a broken
    install, and what it did there was serve a narrower set than the deployment configured
    -- silently admitting three capabilities and refusing the fourth, which is precisely
    the divergence between the admitting process and the executing one that this whole
    seam exists to make impossible. `MOS-REL-051`'s rule, quoted in the paragraph the
    guard carried: a module that exists but raises on import is a real defect and MUST NOT
    be masked into "use the defaults". A `ModuleNotFoundError` on `medos.capabilities` is
    that defect too, not an earlier release.

    So the import is at module level and the list is gone. A constant naming three
    capability ids, exported as part of this module's public surface, reads as
    authoritative to the next person who needs one; the only authoritative answer is the
    resolver's, and there is now no second place to find a different one.
    """
    return capability_providers.capability_ids()


# =====================================================================================
# Request / response models
# =====================================================================================
class JobTarget(BaseModel):
    """`MOS-API-043`/`MOS-API-044`'s `target`. What to run, never which model.

    `kind` is a CLOSED enum of exactly two values. `version_range` MAY carry a semver
    range when `kind` is `capability` and MUST be absent when it is `service_version`
    (`MOS-API-044`); the second half is enforced in `JobCreateRequest` below, where the
    violation can be reported with a JSON Pointer at `/target/version_range`.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["capability", "service_version"]
    id: Annotated[str, Field(min_length=1, max_length=128)]
    version_range: Annotated[str, Field(max_length=64)] | None = None


class JobInput(BaseModel):
    """`MOS-API-045`'s `input`. One primary study, plus the two arrays that exist from
    0.1.0 so that enabling them later is additive rather than a schema break."""

    model_config = ConfigDict(extra="forbid")

    study_instance_uid: Annotated[str, Field(pattern=DICOM_UID_RE.pattern, max_length=64)]
    prior_study_instance_uids: list[str] = Field(default_factory=list, max_length=8)
    series_instance_uids: list[str] = Field(default_factory=list, max_length=64)


class JobCreateRequest(BaseModel):
    """Two envelopes, each closed, for one endpoint.

    `MOS-API-043` fixes `JobCreateRequest` as `{target, input, requested_outputs, labels}`.
    CONTRACT.md section 9 -- binding on the weeks 1-2 slice, and what this service shipped
    -- fixes it as `{study_instance_uid, capabilities}`. Chapter 19 `MOS-UI-021a` names
    that divergence and rules that it "MUST be closed in the surface, not in Chapter 10".
    Closing it in the surface alone is not possible against a server that rejects the
    Chapter 10 body, so this model accepts BOTH and the surface now sends Chapter 10's.
    The old envelope stays accepted because every non-viewer caller in the tree still
    sends it; removing it is a separate, breaking change with its own migration.

    `extra="forbid"` implements `MOS-API-008`: "Request bodies MUST be validated ... with
    `additionalProperties: false`; an unknown request member MUST produce `400` with class
    `client_error` and a `violations[]` member." Note what "both envelopes" does NOT mean:
    every member below is named, `JobTarget` and `JobInput` are closed too, and a body
    that MIXES the two envelopes is refused by `_exactly_one_envelope` rather than
    silently half-read. Two closed schemas is not one open one.

    There is deliberately no `idempotency_key` member and no `job_id` member. Both are
    server-derived (MOS-EXEC-053, MOS-STORE-357); accepting either from a client is the
    defect this file exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    # ---- MOS-API-043 (chapter 10) ----------------------------------------------------
    target: JobTarget | None = None
    input: JobInput | None = None
    labels: dict[str, str] | None = None

    # ---- CONTRACT.md section 9 (weeks 1-2) -------------------------------------------
    study_instance_uid: (
        Annotated[str, Field(pattern=DICOM_UID_RE.pattern, max_length=64)] | None
    ) = None
    capabilities: Annotated[list[str], Field(min_length=1, max_length=16)] | None = None

    # MOS-API-045: "present **from 0.1.0** so that prior-comparison work never requires a
    # breaking widening". A non-empty value is refused with `422` / `PRIORS_NOT_SUPPORTED`
    # below, because inter-study registration (chapter 4) does not exist yet. The field is
    # here so that enabling it later is additive rather than a schema break.
    prior_study_instance_uids: list[str] = Field(default_factory=list, max_length=8)

    # ---- common to both --------------------------------------------------------------
    # The `requested_outputs` column exists and drives the step plan's write/store steps.
    # Defaulted rather than required so CONTRACT.md section 9's two-member body is
    # accepted exactly as written.
    requested_outputs: list[str] = Field(default_factory=lambda: ["SEG", "SR"], max_length=3)

    @model_validator(mode="after")
    def _exactly_one_envelope(self) -> JobCreateRequest:
        """One envelope per request, whole. A half-filled body is a `400`, not a guess."""
        chapter10 = self.target is not None or self.input is not None
        contract = self.study_instance_uid is not None or self.capabilities is not None
        if chapter10 and contract:
            raise ValueError(
                "mixes the MOS-API-043 envelope (target/input) with CONTRACT.md section "
                "9's (study_instance_uid/capabilities); send one or the other"
            )
        if not chapter10 and not contract:
            raise ValueError(
                "missing `target` and `input` (MOS-API-043). A job names what to run and "
                "the study to run it on"
            )
        if chapter10:
            if self.target is None:
                raise ValueError("`input` was sent without `target` (MOS-API-043)")
            if self.input is None:
                raise ValueError("`target` was sent without `input` (MOS-API-043)")
            if self.target.kind == "service_version" and self.target.version_range is not None:
                # MOS-API-044, verbatim: "When `kind` is `service_version`, `id` is a
                # `ServiceVersion` id, `version_range` MUST be absent".
                raise ValueError(
                    "target.version_range MUST be absent when target.kind is "
                    "'service_version' (MOS-API-044)"
                )
            if self.prior_study_instance_uids:
                raise ValueError(
                    "priors belong in `input.prior_study_instance_uids` under "
                    "MOS-API-043, not at the top level"
                )
        else:
            if self.study_instance_uid is None:
                raise ValueError("`capabilities` was sent without `study_instance_uid`")
            if self.capabilities is None:
                raise ValueError("`study_instance_uid` was sent without `capabilities`")
            if self.labels is not None:
                raise ValueError("`labels` belongs to the MOS-API-043 envelope")
        return self

    # ---- the one shape the handler reads ----------------------------------------------
    def resolved_study_instance_uid(self) -> str:
        return (
            self.input.study_instance_uid if self.input is not None else self.study_instance_uid
        )  # type: ignore[return-value]

    def resolved_priors(self) -> list[str]:
        return (
            list(self.input.prior_study_instance_uids)
            if self.input is not None
            else list(self.prior_study_instance_uids)
        )

    def envelope(self) -> str:
        """Which of the two the caller sent. Reported in logs and in violations pointers,
        never guessed at twice."""
        return "MOS-API-043" if self.target is not None else "CONTRACT.md-9"


class JobCreateResponse(BaseModel):
    """`{"job_id": "...", "status": "QUEUED", "state": "QUEUED"}`.

    `MOS-API-049` fixes the JSON field name as `status`; `MOS-TEST-064` says it in so
    many words -- "the DB column is `state` (`MOS-EXEC-001`); `status` is the JSON field
    name of `MOS-API-049`". CONTRACT.md section 9 wrote `state` onto the wire, which made
    the column name the field name, and chapter 19 `MOS-UI-021a` names the consequence in
    the viewer. `status` is therefore the field of record here and `state` is kept beside
    it, carrying the identical value, because every other caller in this repository reads
    it and `MOS-API-093` makes ADDING a response member the safe direction. The two can
    never disagree: both are set from one argument in `_with_status()`.

    Chapter 10 `MOS-API-046` returns the complete `Job` representation here; CONTRACT.md
    is binding for this slice and fixes the two-member body, so the rest of chapter 10's
    `202` contract is carried in HEADERS -- `Location`, `Retry-After`,
    `MedicalOS-Trace-Id`, `MedicalOS-Idempotency-Key` -- where it costs the body nothing
    and a weeks 3-5 Go handler can widen the body additively.
    """

    job_id: str
    #: MOS-API-049's closed enum. The field of record.
    status: str
    #: CONTRACT.md section 9's name for the same value. Deprecated, never divergent.
    state: str


# =====================================================================================
# POST /api/v1/jobs
# =====================================================================================
@router.post(
    "/jobs",
    response_model=JobCreateResponse,
    status_code=http_status.HTTP_202_ACCEPTED,
    summary="Submit a job",
)
def create_job(
    request: Request,
    body: JobCreateRequest,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """Create one job and enqueue it in a single transaction (MOS-EXEC-034).

    `MOS-API-047` is the rule that shapes the whole handler: this endpoint MUST NOT
    return a `clinical_rejection` synchronously. Zero eligible series, an out-of-envelope
    study or an unsupported geometry all produce a persisted `REJECTED` job that
    `GET /api/v1/jobs/{id}` answers `200` for. The only non-2xx answers here are
    `client_error` -- a malformed body, an unknown capability, an unsupported prior --
    which are facts about the REQUEST and not about the patient's imaging.
    """
    trace_id = trace_id_of(request)
    instance = request.url.path

    # ---- authorise -------------------------------------------------------------------
    # MOS-API-005: this route declares `job.create` in medos/api/v1/routes.core.yaml and, until
    # now, declared it and enforced nothing -- the registry says so itself above the row.
    # This is the ONLY job-creation path in the platform (MOS-API-001), which made it the
    # one route where the credential was never asked for anything.
    denied = require(request, "job.create")
    if denied is not None:
        return denied

    # ---- validate ------------------------------------------------------------------
    if idempotency_key is not None and not IDEMPOTENCY_KEY_RE.match(idempotency_key):
        return problem_response(
            build_problem(
                status=400,
                code="IDEMPOTENCY_KEY_INVALID",
                title="Malformed Idempotency-Key header",
                detail="Idempotency-Key MUST match ^[A-Za-z0-9._~-]{1,255}$ (MOS-API-023).",
                problem_class="client_error",
                instance=instance,
                trace_id=trace_id,
            )
        )

    # ---- resolve the target onto this slice's capability set -------------------------
    # MOS-API-043's `target` names ONE capability or ONE service version; CONTRACT.md
    # section 9's `capabilities` names a SET that one job runs. The two are reconciled
    # here and nowhere else, and the reconciliation is reported rather than assumed:
    # see `SLICE_SERVICE_VERSION_ID` and the note in `JobCreateRequest`.
    known = known_capability_ids()
    if body.target is not None:
        if body.target.kind == "service_version":
            if body.target.id != SLICE_SERVICE_VERSION_ID:
                return problem_response(
                    build_problem(
                        status=400,
                        code="UNKNOWN_SERVICE_VERSION",
                        title="Unknown service version",
                        detail=(
                            "target.id does not name a service version this deployment "
                            "runs (MOS-API-044)."
                        ),
                        problem_class="client_error",
                        instance=instance,
                        trace_id=trace_id,
                        violations=[
                            {
                                "pointer": "/target/id",
                                "code": "UNKNOWN_SERVICE_VERSION",
                                "detail": "no such service version is deployed here",
                            }
                        ],
                        supported_service_versions=[SLICE_SERVICE_VERSION_ID],
                    )
                )
            # Every capability that service version implements. NOT a resolution the
            # client computed (MOS-UI-011 forbids a surface doing that) and not a
            # substitution: this deployment runs exactly one service version and its
            # capability set is the registry.
            capability_ids = sorted(known)
        else:
            capability_ids = [body.target.id]
        if body.input is not None and body.input.series_instance_uids:
            # MOS-API-045 lets a client pin the series set; this slice cannot honour a pin
            # because selection happens inside `fetch_series` at execution time
            # (CONTRACT.md section 0). Refusing is `client_error` and NOT
            # `clinical_rejection`: the platform cannot do this yet, which is a fact about
            # the platform, and calling it clinical would tell a radiologist the SERIES
            # was unsuitable. Same reasoning as PRIORS_NOT_SUPPORTED below.
            return problem_response(
                build_problem(
                    status=422,
                    code="SERIES_PINNING_NOT_SUPPORTED",
                    title="Pinning the series set is not supported in this release",
                    detail=(
                        "input.series_instance_uids MUST be empty: this release selects "
                        "series at execution time (MOS-API-045, CONTRACT.md section 0)."
                    ),
                    problem_class="client_error",
                    instance=instance,
                    trace_id=trace_id,
                )
            )
    else:
        capability_ids = list(body.capabilities or ())

    if body.resolved_priors():
        # MOS-API-045: `422`, class `client_error`, code `PRIORS_NOT_SUPPORTED` until the
        # prior-selection rule (chapter 3) and inter-study registration (chapter 4) exist.
        # `client_error` and NOT `clinical_rejection`: the platform cannot do this yet,
        # which is a fact about the platform. Calling it a clinical rejection would tell a
        # radiologist the STUDY was unsuitable, which is false and is precisely the
        # conflation MOS-API-039 forbids.
        return problem_response(
            build_problem(
                status=422,
                code="PRIORS_NOT_SUPPORTED",
                title="Prior studies are not supported in this release",
                detail=(
                    "input.prior_study_instance_uids MUST be empty: inter-study "
                    "registration is deferred to 0.4.0 (MOS-API-045)."
                ),
                problem_class="client_error",
                instance=instance,
                trace_id=trace_id,
            )
        )

    unknown = [c for c in capability_ids if c not in known]
    if unknown:
        # A capability id that does not exist is a malformed request, not a clinical
        # answer: there is no resolution to fail (CONTRACT.md section 0 removes the
        # registry), so MOS-API-047's "zero resolution candidates -> REJECTED job" does
        # not apply. Naming the accepted ids makes this self-correcting for a client.
        return problem_response(
            build_problem(
                status=400,
                code="UNKNOWN_CAPABILITY",
                title="Unknown capability id",
                detail=f"{len(unknown)} unknown capability id(s) in the request.",
                problem_class="client_error",
                instance=instance,
                trace_id=trace_id,
                violations=[
                    {
                        "pointer": (
                            "/target/id"
                            if body.target is not None
                            else f"/capabilities/{capability_ids.index(c)}"
                        ),
                        "code": "UNKNOWN_CAPABILITY",
                        "detail": f"no capability {c!r} is registered",
                    }
                    for c in unknown
                ],
                supported_capabilities=sorted(known),
            )
        )

    bad_outputs = [o for o in body.requested_outputs if o not in ALLOWED_OUTPUTS]
    if bad_outputs or not body.requested_outputs:
        return problem_response(
            build_problem(
                status=400,
                code="UNSUPPORTED_OUTPUT",
                title="Unsupported requested_outputs",
                detail="requested_outputs MUST be a non-empty subset of SEG, SR, SC.",
                problem_class="client_error",
                instance=instance,
                trace_id=trace_id,
                supported_outputs=sorted(ALLOWED_OUTPUTS),
            )
        )

    bad_uids = [u for u in body.resolved_priors() if not DICOM_UID_RE.match(u)]
    if bad_uids:  # pragma: no cover - unreachable while priors are refused above
        return problem_response(
            build_problem(
                status=400,
                code="SCHEMA_VIOLATION",
                title="Request body failed schema validation",
                detail="prior_study_instance_uids contains a value that is not a DICOM UID.",
                problem_class="client_error",
                instance=instance,
                trace_id=trace_id,
            )
        )

    # ---- call medos.db --------------------------------------------------------------
    # MOS-UI-003: the action is exercised with the OPERATOR's credential, so the row must
    # record the operator.
    created_by_kind, created_by_id = actor_of(request)
    spec = repo.JobSpec(
        study_instance_uid=body.resolved_study_instance_uid(),
        capability_ids=tuple(dict.fromkeys(capability_ids)),
        service_id=DEFAULT_SERVICE_ID,
        requested_outputs=tuple(dict.fromkeys(body.requested_outputs)),
        prior_study_instance_uids=tuple(body.resolved_priors()),
        # THE ONE FIELD THE CLIENT'S HEADER REACHES. `JobSpec.idempotency_key()` does not
        # read it (MOS-API-029, MOS-EXEC-054); it is stored for echo and audit only.
        request_idempotency_key=idempotency_key,
        # These were the literals "service_account"/"medos-api". medos/medos/db/audit.py
        # derives the AuditEvent actor from these columns, so the audit trail named the
        # API for every job ever created and could not say who pressed the button.
        created_by_kind=created_by_kind,
        created_by_id=created_by_id,
        trace_id=trace_id or None,
    )
    queue = PostgresJobQueue(conn)
    created = repo.create_job_queued(conn, queue, spec)

    # ---- shape the response ---------------------------------------------------------
    response.headers["Location"] = f"/api/v1/jobs/{created.job_id}"
    response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    response.headers[TRACE_HEADER] = trace_id
    if idempotency_key is not None:
        # MOS-API-022: the server echoes the key it used for request deduplication.
        response.headers["MedicalOS-Idempotency-Key"] = idempotency_key
    if not created.created:
        # MOS-API-026: a repeat MUST NOT create a second resource. The dedup here is on
        # the PLATFORM-derived key (MOS-EXEC-053) -- same study, same capabilities, same
        # service version -- which is a strictly stronger guarantee than the client's
        # header provides and holds even when the client sends no header at all.
        response.status_code = http_status.HTTP_200_OK
        response.headers["MedicalOS-Idempotent-Replay"] = "true"
    # MOS-API-049: `status` is the field of record; `state` carries the identical value
    # for CONTRACT.md section 9's callers. One argument, two members -- they cannot drift.
    return JobCreateResponse(
        job_id=created.job_id, status=created.state, state=created.state
    )


# =====================================================================================
# GET /api/v1/jobs/{job_id}
# =====================================================================================
@router.get("/jobs/{job_id}", summary="Read one job")
def get_job(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    job_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """The job, its progress, its rejection or error, its results and its provenance.

    A `REJECTED` job is answered `200` with a `rejection` member, NOT an HTTP error. This
    is the distinction chapter 10 calls out (`MOS-API-039`, `MOS-API-040`,
    `MOS-API-047`) and it is the reason `medos.core.errors` makes the classification a
    class attribute: a clinical non-answer is a clinical finding. A client that receives
    a `4xx` here learns "your request was wrong"; a client that receives a `200` with a
    `rejection` learns "the study was not analysed and here is why", which is what a
    radiologist has to act on.

    `steps_completed` / `steps_total` and no float `progress` (MOS-API-051, MOS-EXEC-021).
    """
    denied = require(request, "job.read")
    if denied is not None:
        return denied

    trace_id = trace_id_of(request)
    instance = request.url.path

    bad = reject_bad_job_id(job_id, instance=instance, trace_id=trace_id)
    if bad is not None:
        return bad

    view = repo.job_view(conn, job_id)
    if view is None:
        return problem_response(
            job_not_found_problem(job_id, instance=instance, trace_id=trace_id)
        )

    return _shape_job_view(view, instance=instance, trace_id=trace_id)


@router.get("/jobs/{job_id}/series-selection", summary="Series selection for one job")
def get_series_selection(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    job_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """`MOS-API-054`: "Selection is first-class data, not a log line."

    Present because `medos.db.repo.job_view()` puts a `series_selection_href` on every
    stored rejection, and an href that 404s is a defect rather than a nicety. Pure
    projection of the `job_series` rows -- no selection happens here.
    """
    denied = require(request, "job.read")
    if denied is not None:
        return denied

    trace_id = trace_id_of(request)
    instance = request.url.path

    bad = reject_bad_job_id(job_id, instance=instance, trace_id=trace_id)
    if bad is not None:
        return bad

    job = repo.get_job(conn, job_id)
    if job is None:
        return problem_response(
            job_not_found_problem(job_id, instance=instance, trace_id=trace_id)
        )

    rows = json_safe(repo.selected_series(conn, job_id))
    view = repo.job_view(conn, job_id)
    assert view is not None
    evaluated = json_safe(view["series_selection"])
    return {
        "job_id": job_id,
        "service_id": job["service_id"],
        "service_version": job["service_version"],
        "selected": [r for r in evaluated if r["decision"] == "selected"],
        "rejected": [r for r in evaluated if r["decision"] != "selected"],
        "selected_series_uids": [r["series_instance_uid"] for r in rows],
    }


# =====================================================================================
# Shaping helpers -- serialisation only, no decisions
# =====================================================================================
def reject_bad_job_id(job_id: str, *, instance: str, trace_id: str) -> Any:
    """`MOS-API-111`: matching MUST be case-sensitive and a lowercase form MUST NOT be
    accepted in a path segment. Answered `400` and not `404`, because a value that cannot
    be a job id is a malformed request rather than a resource that might exist."""
    if JOB_ID_RE.match(job_id):
        return None
    return problem_response(
        build_problem(
            status=400,
            code="INVALID_JOB_ID",
            title="Malformed job id",
            detail="A job id MUST match ^job_[0-9A-HJKMNP-TV-Z]{26}$ (MOS-API-111).",
            problem_class="client_error",
            instance=instance,
            trace_id=trace_id,
        )
    )


def job_not_found_problem(job_id: str, *, instance: str, trace_id: str) -> dict[str, Any]:
    return build_problem(
        status=404,
        code="JOB_NOT_FOUND",
        title="Job not found",
        detail=f"No job {job_id}.",
        problem_class="client_error",
        instance=instance,
        trace_id=trace_id,
    )


def _shape_job_view(view: dict[str, Any], *, instance: str, trace_id: str) -> dict[str, Any]:
    """`repo.job_view()` rows -> the wire body.

    Serialisation only: timestamps to RFC 3339 (MOS-API-002), `Decimal` to `float`,
    `uuid` to `str`, and the two problem members onto chapter 10's `type` authority. No
    field is computed here and none is dropped -- what the database recorded is what the
    OHIF provenance panel renders (CONTRACT.md section 10).
    """
    out = json_safe(view)
    if "state" in out:
        # MOS-API-049 / MOS-TEST-064: "the DB column is `state` (MOS-EXEC-001); `status`
        # is the JSON field name of MOS-API-049". `repo.job_view` projects the column and
        # this is the one place its wire name is fixed. `state` stays beside it because
        # CONTRACT.md section 9 named it and every other reader in the tree still uses it;
        # adding a member is the safe direction (MOS-API-093) and the two are set from
        # one value, so they cannot disagree.
        out["status"] = out["state"]
    if "rejection" in out and out["rejection"] is not None:
        # MOS-API-040: a REJECTED job carries `rejection` of class `clinical_rejection`
        # and `error` null. Never both.
        out["rejection"] = normalize_problem(
            out["rejection"],
            instance=instance,
            trace_id=trace_id,
            occurred_at=view.get("finished_at"),
            default_class="clinical_rejection",
        )
        out["error"] = None
    if "error" in out and out["error"] is not None:
        out["error"] = normalize_problem(
            out["error"],
            instance=instance,
            trace_id=trace_id,
            occurred_at=view.get("finished_at"),
            default_class="system_failure",
        )
        out["rejection"] = None
    out.setdefault("rejection", None)
    out.setdefault("error", None)
    out["trace_id"] = trace_id
    return out
