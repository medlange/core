# SPDX-License-Identifier: Apache-2.0
"""`medicalos-service-abi/1.0`, sealed half: chapter 2 section 2.5.2, as enforceable code.

The section opens with a closure rule and the rule is the whole security argument:

    "A sealed service exposes exactly these HTTP endpoints on `execution.sealed.port`.
    There are no others; the platform MUST NOT call any path outside this table, and a
    service MUST NOT expose an administrative surface."

`ENDPOINTS` is that table, and `endpoint_for` is how `medos/medos/sealed/invoker.py` refuses to
issue a request the table does not contain. A closed table written as a comment is a
convention; written as a lookup that the only request method consults, it is a boundary.
Two consequences fall out of it for free and both are named requirements: there is no
path by which a service can push anything (`MOS-SVC-048` -- artifacts are PULLED, and
"presigned upload URLs are not part of this contract"), and there is no administrative
path a compromised invoker could call.

THE TWO DIGESTS, WHICH ARE NOT THE SAME DIGEST

`manifest_digest` (`MOS-SVC-027`) identifies the DOCUMENT the image was built from, and
section 2.6.2 step 3 compares it with the registered value to catch an image running a
manifest other than the one whose evidence, intended use and signatures were verified.

`selftest_digest` (`MOS-SVC-026`) identifies the OUTPUT the image produces on its shipped
golden fixture, with `execution_id`, `job_id` and `diagnostics` removed because those are
volatile by construction. Section 2.6.2 step 4 compares it with the manifest's declared
value to catch an image whose weights, kernels or CUDA runtime have drifted.

A single "is this the right service" check would catch neither reliably. The first cannot
detect a swapped weight file; the second cannot detect a widened intended-use statement.

NO FLOAT PROGRESS, ANYWHERE. `MOS-SVC-044` and chapter 5 `MOS-EXEC-001` both forbid it,
and `ExecutionStatus.parse` rejects a `progress` member outright rather than ignoring it:
a service that sends one believes the platform is displaying it.

Pure module -- no sockets, no clock, no `medos.db`. The only import from the rest of the
platform is the RFC 8785 canonicaliser, deliberately: `medos/medos/registry/digest.py` already
records why a second implementation of that would give the registry and the wire two
opinions about one manifest.

Spec: chapter 2 sections 2.5.2, 2.8; `MOS-SVC-021`, `MOS-SVC-026`, `MOS-SVC-027`,
`MOS-SVC-043`, `MOS-SVC-044`, `MOS-SVC-045`, `MOS-SVC-046`, `MOS-SVC-047`, `MOS-SVC-048`,
`MOS-SVC-049`, `MOS-SVC-073`, `MOS-SVC-089`, `MOS-SVC-117`, `MOS-SEC-025`, `MOS-SEC-028`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from medos.sdk.canonical import ULID_ALPHABET, canonical_bytes, sha256_hex

__all__ = [
    "ABI",
    "ENDPOINTS",
    "EXECUTION_STATES",
    "REASON_CODES",
    "SCHEMA_VERSION",
    "TERMINAL_STATES",
    "VOLATILE_BUNDLE_MEMBERS",
    "Endpoint",
    "ExecutionRequest",
    "ExecutionStatus",
    "bundle_without_volatile_members",
    "endpoint_for",
    "manifest_digest",
    "selftest_digest",
]

ABI: Final[str] = "medicalos-service-abi/1.0"
SCHEMA_VERSION: Final[str] = "1.0.0"

#: `MOS-SVC-043`. `succeeded` means a well-formed bundle exists; a bundle with
#: `status: "rejected"` is still `succeeded`, "because a clinical rejection is an outcome,
#: not an error". Collapsing the two is how a triage refusal becomes an incident report.
EXECUTION_STATES: Final[tuple[str, ...]] = ("accepted", "running", "succeeded", "failed")
TERMINAL_STATES: Final[tuple[str, ...]] = ("succeeded", "failed")

#: `MOS-SVC-026`: removed before the self-test digest is taken. Volatile by construction --
#: `diagnostics` carries wall-clock timings and GPU ids.
VOLATILE_BUNDLE_MEMBERS: Final[tuple[str, ...]] = ("execution_id", "job_id", "diagnostics")

#: `MOS-SVC-095`: the CLOSED enum a clinical rejection may draw on, quoted from section
#: 2.8.6. `MOS-SVC-097` is its other half -- a transport or system failure is NOT a
#: rejection and MUST NOT be signalled as one, "by returning an HTTP error, raising an
#: exception, or producing an empty bundle". The two lists never overlap, and a vendor that
#: reports `gateway_error` as a clinical rejection is telling a technologist the study was
#: unsuitable when in fact the platform's own plumbing failed.
REASON_CODES: Final[frozenset[str]] = frozenset({
    "no_eligible_series",
    "geometry_unsupported",
    "gantry_tilt_uncorrectable",
    "spacing_non_uniform",
    "insufficient_coverage",
    "slice_thickness_out_of_envelope",
    "pixel_spacing_out_of_envelope",
    "kernel_unsupported",
    "contrast_phase_unsupported",
    "orientation_unsupported",
    "instance_count_below_minimum",
    "burned_in_annotation_present",
    "patient_attribute_out_of_envelope",
    "capability_not_supported_for_input",
})

_PHASE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
# Crockford base32, taken from `medos.sdk.canonical.ULID_ALPHABET` rather
# than retyped: it omits I, L, O and U so that a transcribed id cannot be confused
# with 1, 0 or V.
#
# NOTE, and it is a specification nit rather than a defect: the example ids in chapter 2
# section 2.5.2 (`job_01J9ZM7A9C1E3G5J7L9N1Q3S5U`, `exe_01J9ZM7B4K2N6P8R0T3V5X7ZAC`)
# contain `U` and `L`, which are not in that alphabet, so they are illustrative rather
# than well-formed. Reported; the platform's own minter is the authority and this pattern
# follows it.
_ULID_BODY = f"[{ULID_ALPHABET}]{{26}}"
_EXECUTION_ID = re.compile(f"^exe_{_ULID_BODY}$")
_JOB_ID = re.compile(f"^job_{_ULID_BODY}$")
_UID = re.compile(r"^[0-9]+(\.[0-9]+)*$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


# =====================================================================================
# 1. The closed endpoint table
# =====================================================================================
@dataclass(frozen=True, slots=True)
class Endpoint:
    """One row of section 2.5.2's table.

    `success` is the single status the row names. It is compared rather than ranged
    (`2xx`), because the difference between the `202` of `POST /v1/executions` and a `200`
    is the difference between "accepted, come back later" and "done" -- and `MOS-SVC-046`
    forbids the second: a service MUST NOT perform inference inside the POST handler, so a
    `200` there means it did.
    """

    method: str
    path: str
    purpose: str
    success: int
    requirement: str


ENDPOINTS: Final[tuple[Endpoint, ...]] = (
    Endpoint("GET", "/healthz", "liveness", 200, "MOS-SVC-050"),
    Endpoint("GET", "/readyz", "readiness; weights loaded", 200, "MOS-SVC-026"),
    Endpoint("GET", "/v1/manifest", "the manifest the image was built with", 200,
             "MOS-SVC-027"),
    Endpoint("POST", "/v1/selftest", "run the shipped golden fixture", 200, "MOS-SVC-026"),
    Endpoint("POST", "/v1/executions", "start one analysis", 202, "MOS-SVC-045"),
    Endpoint("GET", "/v1/executions/{id}", "poll state, phase, result", 200,
             "MOS-SVC-043"),
    Endpoint("GET", "/v1/executions/{id}/artifacts/{artifact_id}",
             "fetch one label map or key image", 200, "MOS-SVC-048"),
    Endpoint("DELETE", "/v1/executions/{id}", "release execution resources", 204,
             "MOS-SVC-049"),
    # `MOS-SVC-117`: "MUST return 501 Not Implemented in 0.1.0-0.2.0 and MUST NOT be relied
    # upon. Cancellation of a running inference is not implemented before 0.3.0". This IS
    # 0.3.0, so the row is live and 202 is its success; the reference service implements
    # it. Mapping a cancelled execution onto the reserved `Job` state CANCELLED is chapter
    # 5's, not this package's -- the ABI stops at the service boundary.
    Endpoint("POST", "/v1/executions/{id}/cancel", "cancel a running execution", 202,
             "MOS-SVC-117"),
)

_SEGMENT = re.compile(r"^\{[a-z_]+\}$")


def endpoint_for(method: str, path: str) -> Endpoint | None:
    """The table row this request is, or `None` -- which means: do not send it.

    Matching is segment-by-segment against the templates, so `/v1/executions/x/logs` finds
    no row even though `/v1/executions/{id}` is a prefix of it. A prefix match here would
    re-open the closed table through the back door, which is the whole failure mode
    section 2.5.2 is written against.
    """
    want = [s for s in path.split("?", 1)[0].split("/") if s != ""]
    for endpoint in ENDPOINTS:
        if endpoint.method != method.upper():
            continue
        have = [s for s in endpoint.path.split("/") if s != ""]
        if len(have) != len(want):
            continue
        if all(
            _SEGMENT.match(h) is not None or h == w
            for h, w in zip(have, want, strict=True)
        ):
            return endpoint
    return None


# =====================================================================================
# 2. The two digests
# =====================================================================================
def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """`MOS-SVC-027`: sha256 over the RFC 8785 canonicalisation of the manifest as JSON.

    Delegates to the registry's `content_digest_of`, which is the same computation under
    chapter 6's name for it (`MOS-REG-017`) and which excludes a `content_digest` member
    when one is present. A chapter 2 `service.yaml` carries no such member, so the two
    definitions coincide -- and routing through one function is what stops them drifting
    the day someone adds one.

    "MUST be computed by the platform at registration and MUST NOT be supplied by the
    vendor": nothing here reads a digest out of the document.
    """
    from medos.registry.digest import content_digest_of  # local: keeps this module pure

    return content_digest_of(manifest)


def bundle_without_volatile_members(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """The bundle with `execution_id`, `job_id` and `diagnostics` removed."""
    return {k: v for k, v in bundle.items() if k not in VOLATILE_BUNDLE_MEMBERS}


def selftest_digest(bundle: Mapping[str, Any]) -> str:
    """`MOS-SVC-026`, plus the one removal that makes the requirement computable at all.

    A CYCLE IN THE SPECIFICATION, RESOLVED HERE AND REPORTED RATHER THAN PATCHED SILENTLY.

      * `MOS-SVC-026`: `selftest.expected_result_bundle_digest` is a member of the manifest
        and is the digest of the self-test `ResultBundle`.
      * `MOS-SVC-027`: `manifest_digest` is the digest of the whole manifest -- which
        therefore covers `selftest.expected_result_bundle_digest`.
      * `MOS-SVC-077` and section 2.8.1: every `ResultBundle` carries
        `service.manifest_digest`, and "MUST equal the registered digest".

    So the self-test digest depends on the manifest digest, which depends on the self-test
    digest. Under SHA-256 that has no fixed point a vendor can compute, and no ordering of
    the three requirements produces one: the manifest cannot be signed before the digest it
    must contain exists.

    `service.manifest_digest` is therefore removed alongside the three members `MOS-SVC-026`
    already removes. It is the narrowest cut available and it costs the self-test nothing:
    the self-test exists to catch "an image whose weights, kernels or CUDA runtime have
    drifted" (which is what the rest of the bundle records), while the identity of the
    manifest is checked directly and independently by section 2.6.2 step 3 against the
    registry. Removing it from THIS digest removes no check from the platform.

    The alternative -- excluding the field from `MOS-SVC-027`'s input instead -- was
    rejected: it would weaken the digest that the artifact signature binds, and it would
    make the registry's `manifest_digest` and the wire's disagree, which
    `medos/medos/registry/digest.py` exists to prevent.
    """
    reduced = bundle_without_volatile_members(bundle)
    service = reduced.get("service")
    if isinstance(service, Mapping):
        reduced["service"] = {k: v for k, v in service.items() if k != "manifest_digest"}
    return "sha256:" + sha256_hex(canonical_bytes(reduced))


# =====================================================================================
# 3. `POST /v1/executions` -- the request
# =====================================================================================
@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """The body of section 2.5.2, validated on the way out and on the way in.

    `gateway.access_token` is the Job Token of chapter 8 section 8.2.5. `MOS-SEC-025`
    permits exactly two delivery mechanisms and this is the second: "Where a `sealed`
    service is invoked over HTTP rather than launched by the platform, the same token MAY
    instead be delivered as the `gateway.access_token` member of the execution request
    body; it MUST NOT be delivered any other way." Hence no `token_path`, no env var, and
    `validate` refuses a request whose token is empty -- an unauthenticated sealed service
    is `MOS-SEC-030`'s prohibition in the other direction.

    `allowed_methods` is pinned to `["GET"]` rather than merely defaulted to it.
    `MOS-SEC-028`: "A service principal's `scope` MUST NOT contain `study.write` or any
    other write verb: a service cannot STOW." A platform that can express a writeable
    execution request has already lost the argument, whatever the Gateway then does.
    """

    execution_id: str
    job_id: str
    deadline_at: str
    clinical_use_mode: str
    capabilities_requested: tuple[str, ...]
    operating_points: Mapping[str, Mapping[str, Any]]
    dicomweb_base_url: str
    access_token: str
    token_expires_at: str
    study_instance_uid: str
    roles: tuple[Mapping[str, Any], ...]
    max_result_bundle_bytes: int
    max_artifact_bytes: int
    schema_version: str = SCHEMA_VERSION
    allowed_methods: tuple[str, ...] = ("GET",)

    def __post_init__(self) -> None:
        for problem in self.validate():
            raise ValueError(f"malformed sealed execution request: {problem}")

    # -- validation ------------------------------------------------------------------
    def validate(self) -> list[str]:
        out: list[str] = []
        if self.schema_version != SCHEMA_VERSION:
            out.append(f"schema_version {self.schema_version!r} != {SCHEMA_VERSION!r}")
        if not _EXECUTION_ID.match(self.execution_id):
            out.append(f"execution_id {self.execution_id!r} is not exe_<ULID>")
        if not _JOB_ID.match(self.job_id):
            out.append(f"job_id {self.job_id!r} is not job_<ULID>")
        if self.clinical_use_mode not in ("clinical", "research_only", "shadow"):
            out.append(f"clinical_use_mode {self.clinical_use_mode!r} is not a mode")
        if not self.capabilities_requested:
            out.append("capabilities_requested is empty; MOS-SVC-011 requires at least one")
        for cap in self.capabilities_requested:
            if not _CAPABILITY.match(cap):
                out.append(f"capability {cap!r} is not a capability id")
        for cap, point in self.operating_points.items():
            if cap not in self.capabilities_requested:
                out.append(f"operating point for {cap!r}, which was not requested")
            # `MOS-SVC-021`: the member is named `score_threshold` everywhere it travels,
            # and this is one of the five places the requirement enumerates by name.
            if "score_threshold" not in point or "id" not in point:
                out.append(f"operating point {cap!r} lacks id/score_threshold (MOS-SVC-021)")
        if tuple(self.allowed_methods) != ("GET",):
            out.append(
                f"allowed_methods {list(self.allowed_methods)} is not exactly ['GET']; "
                "MOS-SEC-028 forbids a service principal any write verb"
            )
        if not self.access_token:
            out.append("gateway.access_token is empty (MOS-SEC-025, MOS-SEC-030)")
        if not self.dicomweb_base_url.startswith(("http://", "https://")):
            out.append(f"gateway.dicomweb_base_url {self.dicomweb_base_url!r} is not a URL")
        if not _UID.match(self.study_instance_uid):
            out.append("input.study_instance_uid is not a DICOM UID")
        if not self.roles:
            out.append("input.roles is empty; the service has nothing to read")
        for role in self.roles:
            for key in ("name", "series_instance_uid", "sop_instance_uids"):
                if key not in role:
                    out.append(f"input role lacks {key!r}")
            uids = role.get("sop_instance_uids") or ()
            if not uids:
                out.append(f"role {role.get('name')!r} dispatches no instance")
        if self.max_result_bundle_bytes <= 0 or self.max_artifact_bytes <= 0:
            out.append("limits must be positive")
        return out

    # -- wire form -------------------------------------------------------------------
    def to_wire(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "job_id": self.job_id,
            "deadline_at": self.deadline_at,
            "clinical_use_mode": self.clinical_use_mode,
            "capabilities_requested": list(self.capabilities_requested),
            "operating_points": {k: dict(v) for k, v in self.operating_points.items()},
            "gateway": {
                "dicomweb_base_url": self.dicomweb_base_url,
                "access_token": self.access_token,
                "token_expires_at": self.token_expires_at,
                "allowed_methods": list(self.allowed_methods),
            },
            "input": {
                "study_instance_uid": self.study_instance_uid,
                "roles": [dict(r) for r in self.roles],
            },
            "limits": {
                "max_result_bundle_bytes": self.max_result_bundle_bytes,
                "max_artifact_bytes": self.max_artifact_bytes,
            },
        }

    @classmethod
    def from_wire(cls, body: Mapping[str, Any]) -> ExecutionRequest:
        gateway = body.get("gateway") or {}
        inputs = body.get("input") or {}
        limits = body.get("limits") or {}
        return cls(
            schema_version=str(body.get("schema_version", "")),
            execution_id=str(body.get("execution_id", "")),
            job_id=str(body.get("job_id", "")),
            deadline_at=str(body.get("deadline_at", "")),
            clinical_use_mode=str(body.get("clinical_use_mode", "")),
            capabilities_requested=tuple(body.get("capabilities_requested") or ()),
            operating_points=dict(body.get("operating_points") or {}),
            dicomweb_base_url=str(gateway.get("dicomweb_base_url", "")),
            access_token=str(gateway.get("access_token", "")),
            token_expires_at=str(gateway.get("token_expires_at", "")),
            allowed_methods=tuple(gateway.get("allowed_methods") or ()),
            study_instance_uid=str(inputs.get("study_instance_uid", "")),
            roles=tuple(inputs.get("roles") or ()),
            max_result_bundle_bytes=int(limits.get("max_result_bundle_bytes", 0)),
            max_artifact_bytes=int(limits.get("max_artifact_bytes", 0)),
        )

    def dispatched_sop_instance_uids(self) -> frozenset[str]:
        """Every instance the platform handed over. `MOS-SVC-088`'s membership set."""
        return frozenset(
            uid for role in self.roles for uid in (role.get("sop_instance_uids") or ())
        )

    def idempotency_fingerprint(self) -> str:
        """`MOS-SVC-045`: what "an identical body" means, decided once.

        The JCS digest of the wire body minus `gateway`. The gateway block is excluded
        because the Job Token is reissued on deadline extension (`MOS-SEC-024`) and a
        retry that carries a fresh token is still the same execution -- treating it as a
        different body would turn every token refresh into a `409`.
        """
        wire = {k: v for k, v in self.to_wire().items() if k != "gateway"}
        return "sha256:" + sha256_hex(canonical_bytes(wire))


# =====================================================================================
# 4. `GET /v1/executions/{id}` -- the poll response
# =====================================================================================
@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    """`MOS-SVC-043`/`MOS-SVC-044`, parsed strictly because a poll drives a lease.

    The invoker renews the queue lease on each successful poll (section 2.6.2 step 7). A
    malformed poll response that is tolerated therefore does not merely display wrong: it
    keeps a job's lease alive on evidence the platform did not actually understand.
    """

    execution_id: str
    state: str
    phase: str
    steps_completed: int
    steps_total: int
    result_bundle: Mapping[str, Any] | None
    error: Mapping[str, Any] | None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @classmethod
    def parse(cls, body: Mapping[str, Any]) -> ExecutionStatus:
        problems = cls.problems(body)
        if problems:
            raise ValueError("; ".join(problems))
        return cls(
            execution_id=str(body["execution_id"]),
            state=str(body["state"]),
            phase=str(body["phase"]),
            steps_completed=int(body["steps_completed"]),
            steps_total=int(body["steps_total"]),
            result_bundle=body.get("result_bundle"),
            error=body.get("error"),
        )

    @staticmethod
    def problems(body: Mapping[str, Any]) -> list[str]:
        out: list[str] = []
        if body.get("schema_version") != SCHEMA_VERSION:
            out.append(f"schema_version {body.get('schema_version')!r}")
        state = body.get("state")
        if state not in EXECUTION_STATES:
            out.append(f"state {state!r} is not one of {list(EXECUTION_STATES)}")
        phase = body.get("phase")
        if not isinstance(phase, str) or not _PHASE.match(phase):
            out.append(f"phase {phase!r} is not open lowercase snake_case (MOS-SVC-044)")
        done, total = body.get("steps_completed"), body.get("steps_total")
        # `isinstance(True, int)` is True in Python, and a bool here is a service that
        # thinks the platform wants a flag. Rejected explicitly.
        for name, value in (("steps_completed", done), ("steps_total", total)):
            if isinstance(value, bool) or not isinstance(value, int):
                out.append(f"{name} {value!r} is not an integer (MOS-SVC-044)")
        if isinstance(done, int) and isinstance(total, int) and not isinstance(done, bool):
            if not 0 <= done <= total:
                out.append(f"0 <= steps_completed <= steps_total violated: {done}/{total}")
        if "progress" in body:
            out.append(
                "a `progress` member is present; MOS-SVC-044 forbids a float progress "
                "value and MOS-EXEC-001 forbids the platform synthesising one"
            )
        if state == "succeeded" and not isinstance(body.get("result_bundle"), Mapping):
            out.append("state is succeeded with no result_bundle (MOS-SVC-043)")
        if state != "succeeded" and body.get("result_bundle") is not None:
            out.append(f"state is {state!r} but a result_bundle is present")
        if state == "failed" and body.get("error") is None:
            out.append("state is failed with no error")
        return out


# =====================================================================================
# 5. The `ResultBundle` checks the invoker performs (section 2.6.2 step 9)
# =====================================================================================
def bundle_problems(
    bundle: Mapping[str, Any],
    *,
    dispatched_sop_instance_uids: frozenset[str],
    requested_capabilities: Sequence[str],
) -> list[str]:
    """Section 2.8's floor, plus the three membership rules `MOS-SVC-052` makes mandatory.

    Deliberately NOT a full JSON Schema run: the bundle schema is
    `result-bundle/1.0.0.json` and belongs to whoever owns section 2.8. What is here is
    the part a sealed invoker must do itself because it depends on values only the
    DISPATCH knows -- which instances were handed over, which capabilities were asked for
    -- and which therefore cannot live in a schema at all.
    """
    out: list[str] = []
    if bundle.get("schema_version") != SCHEMA_VERSION:
        out.append(f"bundle schema_version {bundle.get('schema_version')!r}")
    status = bundle.get("status")
    if status not in ("completed", "rejected"):
        out.append(f"status {status!r} is not completed|rejected")
    rejection = bundle.get("rejection")
    if (status == "rejected") != (rejection is not None):
        out.append("`rejection` MUST be non-null iff status == 'rejected'")
    if isinstance(rejection, Mapping) and rejection.get("reason_code") not in REASON_CODES:
        out.append(
            f"rejection.reason_code {rejection.get('reason_code')!r} is outside the "
            "closed enum of MOS-SVC-095"
        )
    # `MOS-SVC-073`: "A service MUST NOT emit free-text clinical narrative in 0.1.0-0.3.0.
    # `ResultBundle.narrative` MUST be `null`."
    if bundle.get("narrative") is not None:
        out.append("narrative is not null; MOS-SVC-073 defers narrative to chapter 11")
    # Section 2.8.1 marks these required-but-possibly-empty. Absent is not empty: a missing
    # array is a service that did not consider the question.
    for member in ("label_maps", "findings", "measurements", "key_images"):
        if not isinstance(bundle.get(member), list):
            out.append(f"{member} is absent or not an array (section 2.8.1 marks it required)")

    outcomes = bundle.get("capability_outcomes")
    if not isinstance(outcomes, list):
        out.append("capability_outcomes is not an array")
        outcomes = []
    # `MOS-SVC-011`: a capability a service declares but does not produce an output for
    # "MUST be reported as a per-capability rejection, never as silence".
    reported = {o.get("capability") for o in outcomes if isinstance(o, Mapping)}
    for cap in requested_capabilities:
        if cap not in reported:
            out.append(f"capability {cap!r} was requested and is silent (MOS-SVC-011)")
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            out.append("a capability outcome is not an object")
            continue
        if outcome.get("outcome") not in ("produced", "rejected"):
            out.append(
                f"capability_outcomes[{outcome.get('capability')!r}].outcome is "
                f"{outcome.get('outcome')!r}, not produced|rejected (MOS-SVC-076)"
            )
        if outcome.get("outcome") == "rejected":
            if outcome.get("reason_code") not in REASON_CODES:
                out.append(
                    f"capability_outcomes[{outcome.get('capability')!r}].reason_code "
                    f"{outcome.get('reason_code')!r} is outside MOS-SVC-095's closed enum"
                )
    # `MOS-SVC-076`: rejected IF AND ONLY IF every outcome is rejected. Both directions,
    # because the interesting failure is the other one -- a bundle marked `completed` whose
    # every capability was actually refused reads to a clinician as a clean negative study.
    if outcomes and all(isinstance(o, Mapping) for o in outcomes):
        all_rejected = all(o.get("outcome") == "rejected" for o in outcomes)
        if all_rejected != (status == "rejected"):
            out.append(
                f"status is {status!r} but "
                f"{'every' if all_rejected else 'not every'} capability outcome is "
                "rejected (MOS-SVC-076)"
            )

    for finding in bundle.get("findings") or ():
        if not isinstance(finding, Mapping):
            out.append("a finding is not an object")
            continue
        uids = finding.get("source_sop_instance_uids") or ()
        if not uids:
            out.append("a finding cites no instance (MOS-SVC-088)")
        for uid in uids:
            if uid not in dispatched_sop_instance_uids:
                out.append(
                    f"finding cites {uid} which was not dispatched (MOS-SVC-088)"
                )
        if not finding.get("finding_sites"):
            out.append("a finding carries no finding_sites (MOS-SVC-088a)")
        # `MOS-SVC-089`: present MUST equal score >= score_threshold, and a deterministic
        # capability MUST carry neither member. Chapter 2's acceptance check 20 is exactly
        # this arithmetic.
        score, point = finding.get("score"), finding.get("operating_point")
        if score is None and point is None:
            if "present" not in finding:
                out.append("a deterministic finding does not state `present`")
        elif score is None or not isinstance(point, Mapping):
            out.append("a scored finding carries score xor operating_point")
        else:
            threshold = point.get("score_threshold")
            if not isinstance(threshold, (int, float)):
                out.append("operating_point has no numeric score_threshold (MOS-SVC-021)")
            elif bool(finding.get("present")) != (float(score) >= float(threshold)):
                out.append(
                    f"present={finding.get('present')} but score={score} and "
                    f"score_threshold={threshold} (MOS-SVC-089)"
                )

    roles: list[str] = []
    for label_map in bundle.get("label_maps") or ():
        if not isinstance(label_map, Mapping):
            out.append("a label_map is not an object")
            continue
        # `MOS-SVC-087`: "`sha256` and `size_bytes` MUST be declared for every artifact and
        # MUST be verified by the platform on fetch." `role` is `MOS-SVC-121`, and it is
        # not decoration: chapter 4 `MOS-IMG-066` sorts on `(role, artifact_id)` to derive
        # `series_index`, so a missing role changes every derived SeriesInstanceUID.
        for key in ("artifact_id", "sha256", "size_bytes", "role", "segments"):
            if key not in label_map:
                out.append(f"label_map lacks {key!r} (MOS-SVC-087, MOS-SVC-121)")
        digest = str(label_map.get("sha256", ""))
        if digest and not _SHA256.match(digest):
            out.append(f"label_map sha256 {digest!r} is not sha256:<64 hex>")
        role = label_map.get("role")
        if isinstance(role, str):
            if not _PHASE.match(role):
                out.append(f"label_map role {role!r} is not lowercase snake_case")
            if role in roles:
                out.append(f"label_map role {role!r} is not unique in the bundle")
            roles.append(role)
        for segment in label_map.get("segments") or ():
            if not isinstance(segment, Mapping):
                out.append("a label_map segment is not an object")
                continue
            # `MOS-SVC-085`: label_value, category (CID 7150), type (CID 7151/7166) and
            # algorithm_type. The writer cannot construct a conformant SEG without all four.
            for key in ("label_value", "category", "type", "algorithm_type"):
                if key not in segment:
                    out.append(f"label_map segment lacks {key!r} (MOS-SVC-085)")
            if segment.get("algorithm_type") not in (None, "AUTOMATIC", "SEMIAUTOMATIC",
                                                     "MANUAL"):
                out.append(
                    f"segment algorithm_type {segment.get('algorithm_type')!r} is not one "
                    "of AUTOMATIC|SEMIAUTOMATIC|MANUAL (MOS-SVC-085)"
                )
            if isinstance(segment.get("label_value"), int) and segment["label_value"] < 1:
                out.append("segment label_value must be positive; 0 means background")
    return out
