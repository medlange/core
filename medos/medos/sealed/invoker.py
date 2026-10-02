# SPDX-License-Identifier: Apache-2.0
"""The platform side of a sealed execution: chapter 2 section 2.6.2, steps 3 to 10.

A sealed service is untrusted code holding a vendor's weights, and `MOS-SVC-052` states
what the platform has instead of trust:

    "Steps 3, 4 and 9 of section 2.6.2 are mandatory and MUST NOT be configurable off in
    `clinical` mode. A sealed service is untrusted code; the platform's only leverage is
    verification at the boundary."

So the three are not options on this class. `verify_manifest`, `run_selftest` and the
step-9 checks inside `execute` have no `skip=` parameter, no environment variable and no
`clinical_use_mode` branch that weakens them -- research mode gets the same checks, because
a check that is on in production and off in research is a check nobody has ever seen fail.

WHAT THE PLATFORM GIVES, AND WHAT IT TAKES BACK

Given: one execution request carrying a Job Token scoped to `GET` on one study, and the
address of the Gateway. That is the entire input surface. No database DSN, no bucket, no
broker, no PACS credential -- chapter 2's ownership table spends six rows (`MOS-SVC-062`
to `MOS-SVC-067`) saying so and `medos/medos/sealed/isolation.py` is where they are enforced
rather than promised.

Taken back: a `ResultBundle` and a set of artifacts the platform PULLS. `MOS-SVC-048`:
"A service MUST NOT push artifacts anywhere. Presigned upload URLs are not part of this
contract and MUST NOT be issued by the platform." The closed endpoint table in
`medos/medos/sealed/abi.py` is how that is true structurally: `_request` refuses to send
anything the table does not contain, so there is no path on which a push could be
accepted even if a future caller wanted one.

`MOS-SEC-030` AND THE ONE DEVIATION THIS CLASS REFUSES TO MAKE QUIETLY

"The platform MUST call a `sealed` service over mTLS presenting its own workload
certificate; the service MUST validate the SAN." The compose stack has no mesh and no
workload CA, so `SealedServiceConfig` REFUSES a plaintext base URL unless the caller names
the deviation in `plaintext_deviation`. The gap is then in the config of every deployment
that has it, where an auditor reads configs, instead of in a comment in a Python file.

Spec: chapter 2 sections 2.5.2, 2.6.2, 2.8; `MOS-SVC-026`, `MOS-SVC-027`, `MOS-SVC-041`,
`MOS-SVC-043`..`MOS-SVC-052`, `MOS-SVC-072`, `MOS-SVC-088`; chapter 8 `MOS-SEC-025`,
`MOS-SEC-030`, `MOS-SEC-119`.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from medos.sealed.abi import (
    ExecutionRequest,
    ExecutionStatus,
    bundle_problems,
    endpoint_for,
    manifest_digest,
    selftest_digest,
)
from medos.sealed.errors import (
    ArtifactRefused,
    BundleInvalid,
    EvidenceOutsideSelection,
    ExecutionConflict,
    ManifestMismatch,
    ManifestNotCanonicalisable,
    ProtocolViolation,
    SealedError,
    SelftestMismatch,
    ServiceFailed,
    ServiceTimeout,
    TransportRefused,
)
from medos.sealed.nifti import geometry_problems, read_nifti1_header, sequence_close

__all__ = [
    "Response",
    "SealedExecutionResult",
    "SealedInvoker",
    "SealedServiceConfig",
    "Transport",
    "UrllibTransport",
]


# =====================================================================================
# 1. Transport
# =====================================================================================
@dataclass(frozen=True, slots=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolViolation(f"body is not JSON: {exc}") from exc


class Transport(Protocol):
    """One request. A protocol so the invoker can be driven without a socket."""

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str | None = None,
        timeout_s: float = 30.0,
        max_bytes: int | None = None,
    ) -> Response: ...


class UrllibTransport:
    """stdlib only. `requests` is a dependency of the platform image, not of this seam.

    `max_bytes` is enforced while READING and not after. `outputs.max_artifact_bytes` is a
    ceiling the invoker is supposed to hold a vendor to (section 2.6.2 step 9), and a
    service answering `200` with forty gigabytes is a denial of service against the
    invoker; discovering the overrun after buffering it is discovering it too late.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        accept: str | None = None,
        timeout_s: float = 30.0,
        max_bytes: int | None = None,
    ) -> Response:
        request = urllib.request.Request(url, data=body, method=method)
        if content_type:
            request.add_header("Content-Type", content_type)
        if accept:
            request.add_header("Accept", accept)
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = _read_capped(response, max_bytes, url)
                return Response(
                    status=response.status,
                    headers={k.lower(): v for k, v in response.headers.items()},
                    body=payload,
                )
        except urllib.error.HTTPError as exc:  # a status is an answer, not a failure
            payload = _read_capped(exc, max_bytes, url)
            return Response(
                status=exc.code,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                body=payload,
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServiceTimeout(f"{method} {url}: {exc}") from exc


def _read_capped(stream: Any, max_bytes: int | None, url: str) -> bytes:
    if max_bytes is None:
        return stream.read()
    payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ArtifactRefused(
            f"{url} exceeded the declared ceiling of {max_bytes} bytes while streaming",
            detail={"max_bytes": max_bytes},
        )
    return payload


# =====================================================================================
# 2. Configuration
# =====================================================================================
@dataclass(frozen=True, slots=True)
class SealedServiceConfig:
    """Everything the invoker needs, and nothing it must not have.

    `registered_manifest_digest` and `expected_selftest_digest` come from the REGISTRY row
    (`MOS-SVC-032`: the verification result is persisted on the `ServiceVersion` and must
    be reproducible offline from the artifact alone), never from the service. Comparing a
    service's manifest with the manifest that same service just served would verify
    nothing at all.

    The `timeouts:` block is the manifest's (section 2.3.1); the invoker does not invent
    its own, because a vendor who declared `analyze_ms: 900000` has stated the deadline the
    evidence was produced under.
    """

    base_url: str
    registered_manifest_digest: str
    expected_selftest_digest: str
    accept_ms: int = 5_000
    analyze_ms: int = 900_000
    artifact_fetch_ms: int = 120_000
    heartbeat_interval_ms: int = 15_000
    max_result_bundle_bytes: int = 4_194_304
    max_artifact_bytes: int = 268_435_456
    #: `MOS-SEC-030`. Non-empty means: this deployment cannot do mTLS and here is why.
    plaintext_deviation: str = ""

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise TransportRefused(f"base_url {self.base_url!r} is not an HTTP URL")
        if self.base_url.startswith("http://") and not self.plaintext_deviation:
            raise TransportRefused(
                "MOS-SEC-030 requires the platform to call a sealed service over mTLS "
                "with its own workload certificate, and this base_url is plaintext. Pass "
                "`plaintext_deviation=<why>` to record the deviation; there is no flag "
                "that turns the requirement off."
            )
        if self.heartbeat_interval_ms <= 0 or self.analyze_ms <= 0:
            raise ValueError("timeouts must be positive")

    def url(self, path: str) -> str:
        return self.base_url.rstrip("/") + path


@dataclass(frozen=True, slots=True)
class SealedExecutionResult:
    """What `execute` hands back: a verified bundle plus the bytes it pulled.

    `artifacts` is `artifact_id -> bytes`. They are held in memory here because the
    invoker's caller streams them to the object store (`Z-DATA`, which this package has no
    credential for -- `MOS-SEC-087`), and because `max_artifact_bytes` bounds them.
    """

    execution_id: str
    result_bundle: Mapping[str, Any]
    artifacts: Mapping[str, bytes]
    polls: int
    phases: tuple[str, ...]


# =====================================================================================
# 3. The invoker
# =====================================================================================
class SealedInvoker:
    """One sealed `ServiceVersion` at one address. Not thread-safe; one job at a time.

    `MOS-SVC-072` is why that is not a limitation to fix later: "A service MUST be given
    data from exactly one tenant per execution and MUST NOT retain state across
    executions." An invoker that multiplexed executions over one object would be the
    platform-side half of the state this forbids.
    """

    def __init__(
        self,
        config: SealedServiceConfig,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.transport = transport or UrllibTransport()
        self._sleep = sleep
        self._clock = clock

    # -- the closed table, enforced ---------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        accept: str | None = None,
        timeout_ms: int | None = None,
        max_bytes: int | None = None,
    ) -> tuple[Response, Any]:
        """Every byte this class sends goes through here, and here consults the table.

        Section 2.5.2: "the platform MUST NOT call any path outside this table". A guard
        in the one place requests are constructed is the difference between that sentence
        being enforced and being reviewed.
        """
        endpoint = endpoint_for(method, path)
        if endpoint is None:
            raise ProtocolViolation(
                f"{method} {path} is not a row of the medicalos-service-abi/1.0 table; "
                "section 2.5.2 closes it and the platform MUST NOT call anything else"
            )
        payload = None if body is None else json.dumps(body).encode("utf-8")
        response = self.transport.request(
            method,
            self.config.url(path),
            body=payload,
            content_type="application/json" if payload is not None else None,
            accept=accept,
            timeout_s=(timeout_ms or self.config.accept_ms) / 1000.0,
            max_bytes=max_bytes,
        )
        return response, endpoint

    # -- step 3 -----------------------------------------------------------------------
    def verify_manifest(self) -> str:
        """Section 2.6.2 step 3. Mismatch => job FAILED and the version is SUSPENDED.

        `GET /v1/manifest` is `application/yaml` in the section 2.5.2 table, and
        `MOS-SVC-027` defines `manifest_digest` over the manifest CONVERTED TO JSON. This
        platform carries no YAML reader (see the report accompanying this component), so
        the invoker asks for `application/json` on the SAME path -- content negotiation,
        not a tenth endpoint -- and fails closed when the service cannot produce it.
        `MOS-SVC-052` forbids configuring this step off, and "the service only speaks YAML
        so we skipped it" is what configuring it off looks like from the inside.
        """
        response, _ = self._request(
            "GET", "/v1/manifest", accept="application/json", timeout_ms=self.config.accept_ms
        )
        if response.status != 200:
            raise ManifestMismatch(
                f"GET /v1/manifest answered {response.status}",
                detail={"status": response.status},
            )
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise ManifestNotCanonicalisable(
                f"GET /v1/manifest answered {content_type!r}; MOS-SVC-027's digest is "
                "defined over the JSON form and this platform has no YAML reader",
                detail={"content_type": content_type},
            )
        served = response.json()
        if not isinstance(served, Mapping):
            raise ManifestNotCanonicalisable("the manifest is not a JSON object")
        digest = manifest_digest(served)
        if digest != self.config.registered_manifest_digest:
            raise ManifestMismatch(
                "the image is running a manifest other than the registered one",
                detail={
                    "served": digest,
                    "registered": self.config.registered_manifest_digest,
                },
            )
        return digest

    # -- step 4 -----------------------------------------------------------------------
    def run_selftest(self) -> Mapping[str, Any]:
        """Section 2.6.2 step 4 / `MOS-SVC-026`. Mismatch => the POD is drained.

        Run "once per pod after readiness". The consequence is deliberately narrower than
        step 3's: a self-test failure can be a node-local fault (a missing GPU, a driver
        mismatch), and suspending the `ServiceVersion` for every tenant over one bad node
        would be a worse outage than the one being prevented.
        """
        response, endpoint = self._request(
            "POST", "/v1/selftest", body={}, timeout_ms=self.config.analyze_ms
        )
        if response.status != endpoint.success:
            raise SelftestMismatch(
                f"POST /v1/selftest answered {response.status}",
                detail={"status": response.status},
            )
        bundle = response.json()
        if not isinstance(bundle, Mapping):
            raise SelftestMismatch("the self-test did not return a ResultBundle object")
        digest = selftest_digest(bundle)
        if digest != self.config.expected_selftest_digest:
            raise SelftestMismatch(
                "the self-test bundle does not reproduce the manifest's declared digest",
                detail={
                    "computed": digest,
                    "declared": self.config.expected_selftest_digest,
                },
            )
        return bundle

    # -- step 5 -----------------------------------------------------------------------
    def start(self, request: ExecutionRequest) -> str:
        """`POST /v1/executions`. `202` and an `execution_id`, or a typed refusal.

        `MOS-SVC-045` makes this idempotent on `execution_id`: "A repeat with the same
        `execution_id` and an identical body MUST return `202` referring to the same
        execution. A repeat with the same `execution_id` and a different body MUST return
        `409`." The retry path of chapter 5 depends on the first half; the second half is
        the service telling the platform its idempotency key is being reused, which is a
        platform defect and must surface as one.
        """
        response, endpoint = self._request(
            "POST", "/v1/executions", body=request.to_wire(),
            timeout_ms=self.config.accept_ms,
        )
        if response.status == 409:
            raise ExecutionConflict(
                f"{request.execution_id} was already started with a different body",
                detail=_safe_problem(response),
            )
        if response.status != endpoint.success:
            # `MOS-SVC-046`: "The service MUST accept within `timeouts.accept_ms` and MUST
            # NOT perform inference inside the `POST` handler." A 200 here is that
            # prohibition broken, and it is worth saying so rather than reporting "not 202".
            extra = (
                " -- a 200 means the service analysed inside the POST handler, which "
                "MOS-SVC-046 forbids" if response.status == 200 else ""
            )
            raise ProtocolViolation(
                f"POST /v1/executions answered {response.status}, expected 202{extra}",
                detail=_safe_problem(response),
            )
        body = response.json()
        execution_id = (body or {}).get("execution_id") if isinstance(body, Mapping) else None
        if execution_id != request.execution_id:
            raise ProtocolViolation(
                f"the service renamed the execution to {execution_id!r}; the id is the "
                "platform's idempotency key (MOS-SVC-045) and is not the service's to pick"
            )
        return str(execution_id)

    # -- step 7 -----------------------------------------------------------------------
    def poll(self, execution_id: str) -> ExecutionStatus:
        response, endpoint = self._request(
            "GET", f"/v1/executions/{execution_id}",
            timeout_ms=self.config.heartbeat_interval_ms,
        )
        if response.status != endpoint.success:
            raise ProtocolViolation(
                f"GET /v1/executions/{{id}} answered {response.status}",
                detail=_safe_problem(response),
            )
        body = response.json()
        if not isinstance(body, Mapping):
            raise ProtocolViolation("the poll response is not an object")
        try:
            return ExecutionStatus.parse(body)
        except ValueError as exc:
            raise ProtocolViolation(f"malformed poll response: {exc}") from exc

    def wait(self, execution_id: str) -> tuple[ExecutionStatus, int, tuple[str, ...]]:
        """Poll every `heartbeat_interval_ms` until terminal or `analyze_ms` expires.

        The interval is the manifest's `timeouts.heartbeat_interval_ms` because section
        2.6.2 step 7 ties the two together: the invoker "renews the queue lease on each
        successful poll". Polling faster would renew a lease on no new evidence; polling
        slower would let a live execution's lease expire under it.
        """
        deadline = self._clock() + self.config.analyze_ms / 1000.0
        interval = self.config.heartbeat_interval_ms / 1000.0
        polls = 0
        phases: list[str] = []
        while True:
            status = self.poll(execution_id)
            polls += 1
            if not phases or phases[-1] != status.phase:
                phases.append(status.phase)
            if status.terminal:
                return status, polls, tuple(phases)
            if self._clock() >= deadline:
                raise ServiceTimeout(
                    f"{execution_id} did not reach a terminal state within "
                    f"{self.config.analyze_ms} ms (last phase {status.phase!r})",
                    detail={"phase": status.phase, "polls": polls},
                )
            self._sleep(interval)

    # -- step 8 -----------------------------------------------------------------------
    def fetch_artifact(
        self, execution_id: str, artifact_id: str, *, expected_sha256: str, declared_bytes: int
    ) -> bytes:
        """Pull one artifact and verify it before anybody downstream sees it.

        Three checks, and the order matters: the ceiling is enforced during the read, the
        declared length is compared next (a mismatch is a bundle that lies about its own
        artifact), and the digest last. Verifying the digest of bytes that were never
        bounded would mean buffering whatever the vendor sent in order to find out it was
        too big.
        """
        # The tighter of the two ceilings. `max_artifact_bytes` is what the MANIFEST
        # permits and `size_bytes` is what THIS bundle claims; a service whose body runs
        # past its own declaration is stopped at the declaration rather than at the
        # manifest's much larger limit. `size_bytes` is required by `MOS-SVC-087` and
        # `_verify_bundle` has already refused a bundle without it, so the fallback below
        # is unreachable in practice and is kept so that a future caller of
        # `fetch_artifact` on its own still gets a bound.
        ceiling = min(self.config.max_artifact_bytes, declared_bytes or
                      self.config.max_artifact_bytes)
        response, endpoint = self._request(
            "GET", f"/v1/executions/{execution_id}/artifacts/{artifact_id}",
            timeout_ms=self.config.artifact_fetch_ms,
            max_bytes=ceiling,
        )
        if response.status != endpoint.success:
            raise ArtifactRefused(
                f"artifact {artifact_id} answered {response.status}",
                detail=_safe_problem(response),
            )
        payload = response.body
        if declared_bytes and len(payload) != declared_bytes:
            raise ArtifactRefused(
                f"artifact {artifact_id} is {len(payload)} bytes; the bundle declared "
                f"{declared_bytes}",
                detail={"artifact_id": artifact_id},
            )
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if digest != expected_sha256:
            raise ArtifactRefused(
                f"artifact {artifact_id} digest {digest} != declared {expected_sha256}",
                detail={"artifact_id": artifact_id},
            )
        return payload

    # -- step 10 ----------------------------------------------------------------------
    def release(self, execution_id: str) -> None:
        """`DELETE /v1/executions/{id}` -- `MOS-SVC-049`, after every artifact is verified.

        Never before. The service "MUST retain the execution for at least
        `timeouts.artifact_fetch_ms` after reaching `succeeded` if no `DELETE` arrives",
        so a DELETE sent early is the platform destroying the only copy of an artifact it
        has not yet checked.
        """
        response, endpoint = self._request(
            "DELETE", f"/v1/executions/{execution_id}",
            timeout_ms=self.config.accept_ms,
        )
        if response.status != endpoint.success:
            raise ProtocolViolation(
                f"DELETE /v1/executions/{{id}} answered {response.status}, expected "
                f"{endpoint.success}",
                detail=_safe_problem(response),
            )

    def cancel(self, execution_id: str) -> None:
        """`MOS-SVC-117`: reserved and `501` before 0.3.0; live from this release.

        The `Job` state `CANCELLED` and the `job.cancel` permission are chapter 5's and
        are not touched here. This is the service-side half only: the execution stops and
        the resources are released.
        """
        response, endpoint = self._request(
            "POST", f"/v1/executions/{execution_id}/cancel",
            body={}, timeout_ms=self.config.accept_ms,
        )
        if response.status != endpoint.success:
            raise ProtocolViolation(
                f"cancel answered {response.status}, expected {endpoint.success}",
                detail=_safe_problem(response),
            )

    # -- steps 5 to 10, in order ------------------------------------------------------
    def execute(
        self,
        request: ExecutionRequest,
        *,
        platform_geometry: Mapping[str, Any] | None = None,
    ) -> SealedExecutionResult:
        """One analysis, verified. Releases the execution on every path that reached it.

        The `finally` is not tidiness. Section 2.6.2 step 10 has the platform DELETE the
        execution, and a service holds scratch state -- pixel-derived, tenant-scoped state
        (`MOS-SVC-072`) -- until it arrives. Leaving it behind on the failure path is
        leaving one tenant's derived data inside a vendor container while the next tenant's
        job is dispatched to it.
        """
        execution_id = self.start(request)
        released = False
        try:
            status, polls, phases = self.wait(execution_id)
            if status.state == "failed":
                raise ServiceFailed(
                    f"the service failed the execution: {_redact(status.error)}",
                    detail={"phase": status.phase},
                )
            bundle = status.result_bundle or {}
            self._verify_bundle(bundle, request, platform_geometry)
            artifacts = self._pull_artifacts(execution_id, bundle)
            self.release(execution_id)
            released = True
            return SealedExecutionResult(
                execution_id=execution_id,
                result_bundle=bundle,
                artifacts=artifacts,
                polls=polls,
                phases=phases,
            )
        finally:
            if not released:
                try:
                    self.release(execution_id)
                except SealedError:
                    pass  # the original failure is the one worth reporting

    # -- step 9 -----------------------------------------------------------------------
    def _verify_bundle(
        self,
        bundle: Mapping[str, Any],
        request: ExecutionRequest,
        platform_geometry: Mapping[str, Any] | None = None,
    ) -> None:
        raw = json.dumps(bundle).encode("utf-8")
        if len(raw) > self.config.max_result_bundle_bytes:
            raise BundleInvalid(
                f"the bundle is {len(raw)} bytes, over the declared "
                f"{self.config.max_result_bundle_bytes}"
            )
        if bundle.get("execution_id") != request.execution_id:
            raise BundleInvalid("the bundle echoes a different execution_id")
        if bundle.get("job_id") != request.job_id:
            raise BundleInvalid("the bundle echoes a different job_id")
        service = bundle.get("service")
        if not isinstance(service, Mapping):
            raise BundleInvalid("the bundle names no service")
        if service.get("manifest_digest") != self.config.registered_manifest_digest:
            # The same comparison as step 3, one layer in. Step 3 asks the image what it
            # is; this asks the ANSWER what produced it, and a bundle attributed to an
            # unregistered manifest would be written into DICOM equipment tags.
            raise BundleInvalid(
                "the bundle attributes itself to a manifest other than the registered one",
                detail={"claimed": service.get("manifest_digest")},
            )

        if platform_geometry is not None:
            # Section 2.6.2 step 9's third check, and the one that distinguishes sealed
            # mode from native. In native mode the platform OWNS the grid, so there is
            # nothing to compare. Here the service built its own canonical volume from
            # DICOM it fetched itself, and the platform recomputes the grid from the
            # selected series' HEADERS -- "no pixel data needed" -- and compares. Without
            # it, a service could return a correct-looking label map on a grid of its own
            # choosing and every measurement derived from it would be wrong by the ratio
            # of the voxel volumes while looking entirely plausible.
            declared = bundle.get("geometry")
            if not isinstance(declared, Mapping):
                raise BundleInvalid("the bundle declares no geometry (MOS-SVC-081)")
            if declared.get("voxel_order") != "LPS":
                raise BundleInvalid(
                    f"geometry.voxel_order is {declared.get('voxel_order')!r}; "
                    "MOS-SVC-081 fixes it as 'LPS'"
                )
            for member in ("shape", "spacing_mm", "origin_lps_mm", "direction_lps"):
                expected, got = platform_geometry.get(member), declared.get(member)
                if expected is None:
                    continue
                if not isinstance(got, (list, tuple)) or not sequence_close(got, expected):
                    raise BundleInvalid(
                        f"geometry_mismatch: the service declares {member}={got!r}; the "
                        f"platform computed {list(expected)!r} from the selected series' "
                        "DICOM headers (section 2.6.2 step 9, tolerance 1e-4)"
                    )

        dispatched = request.dispatched_sop_instance_uids()
        problems = bundle_problems(
            bundle,
            dispatched_sop_instance_uids=dispatched,
            requested_capabilities=request.capabilities_requested,
        )
        evidence = [p for p in problems if "was not dispatched" in p]
        if evidence:
            raise EvidenceOutsideSelection("; ".join(evidence))
        if problems:
            raise BundleInvalid("; ".join(problems))

    def _pull_artifacts(
        self, execution_id: str, bundle: Mapping[str, Any]
    ) -> dict[str, bytes]:
        """Steps 8 and 9 for every declared artifact: pull, verify bytes, verify grid.

        The geometry check happens HERE, on the bytes, and not against the bundle's
        declaration of them. `MOS-SVC-082` is explicit that the platform verifies the array
        ("The platform MUST verify this and MUST fail the job with `geometry_mismatch`"),
        and a label map is the object that becomes a DICOM SEG burned onto a patient's
        images: a mirrored or off-by-one-slice array that nobody opened is a segmentation
        drawn on the wrong anatomy.
        """
        geometry = bundle.get("geometry") or {}
        out: dict[str, bytes] = {}
        for label_map in bundle.get("label_maps") or ():
            artifact_id = str(label_map.get("artifact_id"))
            payload = self.fetch_artifact(
                execution_id,
                artifact_id,
                expected_sha256=str(label_map.get("sha256")),
                declared_bytes=int(label_map.get("size_bytes") or 0),
            )
            self._verify_label_map_grid(artifact_id, payload, geometry)
            out[artifact_id] = payload
        return out

    def _verify_label_map_grid(
        self, artifact_id: str, payload: bytes, geometry: Mapping[str, Any]
    ) -> None:
        try:
            header = read_nifti1_header(payload)
        except ValueError as exc:
            raise ArtifactRefused(
                f"artifact {artifact_id} is not a readable nifti-gzip label map "
                f"(MOS-SVC-084): {exc}",
                detail={"artifact_id": artifact_id},
            ) from exc
        problems = geometry_problems(header, geometry)
        if problems:
            raise ArtifactRefused(
                f"geometry_mismatch on {artifact_id}: " + "; ".join(problems),
                detail={"artifact_id": artifact_id},
            )


# =====================================================================================
# 4. Helpers
# =====================================================================================
def _safe_problem(response: Response) -> dict[str, Any]:
    """A bounded, non-PHI-bearing summary of an error body.

    `MOS-SEC-119` treats a sealed service's output as potentially PHI-bearing, and an
    error body is output. So the body is NOT copied into the exception: only its length
    and content type, which is enough to debug a protocol failure and carries nothing.
    """
    return {
        "status": response.status,
        "content_type": response.headers.get("content-type", ""),
        "body_bytes": len(response.body),
    }


def _redact(error: Mapping[str, Any] | None) -> str:
    """A vendor-supplied error object, reduced to its code.

    Same reason as `_safe_problem`: `MOS-SEC-119` makes vendor-controlled text potentially
    PHI-bearing, and a job failure reason is read in a UI and shipped to a log index. The
    full body belongs in the per-job stdout capture that requirement mandates, not here.
    """
    if not isinstance(error, Mapping):
        return "no error object"
    code = error.get("code")
    return f"code={code!r}" if code else "an error object with no code"
