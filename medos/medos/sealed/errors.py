# SPDX-License-Identifier: Apache-2.0
"""What the platform does when a sealed service misbehaves, as a type per outcome.

Chapter 2 does not leave the consequences to the caller's judgement -- section 2.6.2 names
a different one for each failure, and they are not interchangeable:

  step 3  manifest digest mismatch  -> job `FAILED` AND the `ServiceVersion` -> `SUSPENDED`
  step 4  self-test digest mismatch -> the POD is drained; no job is dispatched to it
  step 9  a verification failure    -> this job fails; the version is untouched

A single `SealedError` with a string message would let a caller collapse those three into
one `except`, and the difference between "suspend the version for every tenant" and "fail
this job" is exactly the difference a registry status transition costs. Each class
therefore carries `consequence`, and `medos/medos/sealed/invoker.py` never raises a bare one.

`requirement` is on the instance rather than in the docstring because these errors travel:
a job failure reason is read by an operator who does not have the chapter open.

Spec: `MOS-SVC-026`, `MOS-SVC-027`, `MOS-SVC-043`..`MOS-SVC-052`, `MOS-SVC-088`,
`MOS-SVC-089`, `MOS-SVC-073`, `MOS-SEC-030`, chapter 2 section 2.6.2.
"""

from __future__ import annotations

__all__ = [
    "ArtifactRefused",
    "BundleInvalid",
    "EvidenceOutsideSelection",
    "ExecutionConflict",
    "IsolationViolation",
    "ManifestMismatch",
    "ManifestNotCanonicalisable",
    "ProtocolViolation",
    "SealedError",
    "SelftestMismatch",
    "ServiceFailed",
    "ServiceTimeout",
    "TransportRefused",
]


class SealedError(Exception):
    """Base. `code` is what a `Job.failure_reason` carries; never raised directly."""

    code: str = "sealed_error"
    requirement: str = ""
    consequence: str = "the job fails"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self.code}] {self.message}"


class ManifestMismatch(SealedError):
    """The image is not running the manifest that was registered (section 2.6.2 step 3).

    Two different documents claiming one identity: the registry verified signatures,
    evidence and intended use against manifest A, and manifest B is what will produce the
    clinical output. `MOS-SVC-052` makes this check mandatory in `clinical` mode.
    """

    code = "service_manifest_mismatch"
    requirement = "MOS-SVC-027, MOS-SVC-052"
    consequence = "the job FAILS and the ServiceVersion transitions to SUSPENDED"


class ManifestNotCanonicalisable(SealedError):
    """`GET /v1/manifest` answered in a form whose `manifest_digest` cannot be computed.

    `MOS-SVC-027` defines the digest over the RFC 8785 canonicalisation of the manifest
    CONVERTED TO JSON. A service that serves only `application/yaml` (which the section
    2.5.2 table permits) hands the platform bytes it cannot canonicalise without a YAML
    reader. That is a refusal, not a skip: `MOS-SVC-052` forbids configuring step 3 off,
    and "we could not check" is the shape every disabled check takes on the way out.
    """

    code = "service_manifest_not_canonicalisable"
    requirement = "MOS-SVC-027, MOS-SVC-052"
    consequence = "the job FAILS; the ServiceVersion is left alone (the image may be fine)"


class SelftestMismatch(SealedError):
    """`POST /v1/selftest` did not reproduce `selftest.expected_result_bundle_digest`.

    `MOS-SVC-026`: "A runner MUST run the self-test once per pod after readiness and MUST
    refuse to serve on mismatch." The pod, not the version -- the same image on another
    node with a working GPU may be fine, and suspending the version would take a vendor
    offline for a node-local fault.
    """

    code = "service_selftest_mismatch"
    requirement = "MOS-SVC-026"
    consequence = "the POD is drained; no job is dispatched to it"


class ProtocolViolation(SealedError):
    """The service answered outside the ABI, or the platform was asked to step outside it.

    Both directions are the same defect. Section 2.5.2: "There are no others; the platform
    MUST NOT call any path outside this table, and a service MUST NOT expose an
    administrative surface."
    """

    code = "service_protocol_violation"
    requirement = "MOS-SVC-043, MOS-SVC-044, MOS-SVC-048"
    consequence = "the job FAILS"


class ExecutionConflict(SealedError):
    """`MOS-SVC-045`: the same `execution_id` was re-posted with a different body.

    A retry that changed the study, the capability set or the operating point is not a
    retry. `409` is the service telling the platform its idempotency key is being reused,
    which is a platform defect and must not be papered over with a fresh id.
    """

    code = "service_execution_conflict"
    requirement = "MOS-SVC-045"
    consequence = "the job FAILS; the retry is not re-attempted under a new id"


class ServiceTimeout(SealedError):
    """A deadline in `timeouts:` expired -- accept, analyze, or artifact fetch."""

    code = "service_timeout"
    requirement = "MOS-SVC-041, MOS-SVC-046, MOS-SVC-049"
    consequence = "the job FAILS and the execution is released with DELETE"


class ArtifactRefused(SealedError):
    """A fetched artifact failed a step-9 check: digest, declared size, or the cap.

    `outputs.max_artifact_bytes` is a ceiling the platform enforces while streaming, not
    after: a service that answers a 200 with 40 GB is a denial of service against the
    invoker, and discovering the overrun after buffering it is discovering it too late.
    """

    code = "service_artifact_refused"
    requirement = "MOS-SVC-048, section 2.6.2 step 9"
    consequence = "the job FAILS; nothing is written to the object store"


class EvidenceOutsideSelection(SealedError):
    """`MOS-SVC-088`: a finding cites an instance the platform did not dispatch.

    This is the check that makes "which images did it look at" answerable, and it is also
    the one that catches a service reaching past its Job Token scope -- if it names an
    instance it was not given, either the Gateway leaked it or the service invented it,
    and both are refusals.
    """

    code = "schema_invalid"
    requirement = "MOS-SVC-088, MOS-SEC-028"
    consequence = "the bundle is REJECTED before any DICOM write"


class BundleInvalid(SealedError):
    """The `ResultBundle` is not well formed under section 2.8.

    Includes `MOS-SVC-089`'s threshold consistency (`present == score >= score_threshold`)
    and `MOS-SVC-073`'s `narrative: null` floor for 0.1.0-0.3.0.
    """

    code = "schema_invalid"
    requirement = "MOS-SVC-073, MOS-SVC-080, MOS-SVC-089"
    consequence = "the bundle is REJECTED before any DICOM write"


class TransportRefused(SealedError):
    """The invoker refused to open the connection at all.

    Raised by `SealedServiceConfig` when a plaintext base URL is used without a recorded
    deviation. `MOS-SEC-030` requires mTLS with the platform's workload certificate and
    SAN validation by the service; a deployment that cannot do that has a gap, and the gap
    is louder as a refusal-unless-waived than as a comment.
    """

    code = "service_transport_refused"
    requirement = "MOS-SEC-030"
    consequence = "no connection is opened"


class ServiceFailed(SealedError):
    """The service reported `state: "failed"`. A SYSTEM failure, never a clinical one.

    `MOS-SVC-097` draws that line and closes both lists: "A service MUST NOT signal a
    clinical rejection by returning an HTTP error, raising an exception, or producing an
    empty bundle." A `failed` execution is `service_unavailable`, `gateway_error`,
    `internal_error` and their siblings -- never `no_eligible_series`. A `ResultBundle`
    with `status: "rejected"` arrives under `state: "succeeded"` and does not come here,
    "because a clinical rejection is an outcome, not an error" (`MOS-SVC-043`).

    Collapsing the two would tell a technologist the study was unsuitable when the
    platform's own plumbing failed, which is a re-imaged patient.
    """

    code = "service_execution_failed"
    requirement = "MOS-SVC-043, MOS-SVC-097"
    consequence = "the job FAILS with the service's own failure code; it is not a rejection"


class IsolationViolation(SealedError):
    """A sealed container can reach something `Z-SERVICE` has no egress to.

    Not raised on the execution path -- `medos/medos/sealed/isolation.py` returns findings
    rather than raising, because an operator wants every violation at once and not the first
    one. The type exists so that a deployment checker can raise a typed failure.
    """

    code = "sealed_isolation_violation"
    requirement = "MOS-SEC-004, MOS-SEC-029, MOS-SVC-050"
    consequence = "the deployment is non-conformant; no sealed job may be dispatched"
