# SPDX-License-Identifier: Apache-2.0
"""Sealed mode: the ABI at the boundary, and the boundary itself.

Chapter 2 defines two execution modes on one manifest and `MOS-SVC-118` places the second
in this release: "`native` mode MUST be implemented in 0.1.0. `sealed` mode MUST NOT be
claimed as available before 0.3.0." What makes it sealed is not the HTTP shape -- it is
that the vendor's container holds weights the platform never sees and reaches nothing but
the platform-mediated input it is handed.

The file has two halves and they prove different kinds of thing.

PART A drives the real reference service over real sockets and checks the contract of
section 2.5.2: the closed endpoint table, the two digests, idempotency, the step-9
verifications. Where the thing under test is a MALICIOUS vendor -- a service that lies
about an artifact digest, cites an instance it was never given, or returns a label map on
a grid of its own choosing -- there is no such service to run, so the wire is scripted
instead. Being the adversary is the only way to test the defence.

PART B is the one that cannot be faked. Chapter 8's acceptance check 12 is a sentence
about a running container:

    "From a running `sealed` service container, TCP connections to the PACS port, the
    PostgreSQL port, the Triton port and the object-store port all fail to connect."

so this file opens those connections, from inside `medos-sealed-service`, to the IP
ADDRESSES of the containers concerned -- not their DNS names. A name that does not resolve
is weak evidence: it proves the container is not in that network's DNS domain, not that
the route is absent, and a deployment that put the database on a shared network while
leaving the alias off would pass a name-only check while being wide open. The probe
pattern, the `_UNPROBEABLE` taxonomy and the insistence on a positive CONTROL are lifted
from `tests/integration/test_gateway.py`, which does the same job for `MOS-DATA-006`.

WHY THE CONTROLS ARE NOT DECORATION. Every assertion in part B is a NEGATIVE -- "no
connection was opened" -- and a negative from a probe that cannot work is indistinguishable
from isolation. Two controls therefore run alongside: the same probe from the same
container to the Gateway MUST succeed, and the same probe from `medos-worker` to Postgres
MUST succeed. If either fails, the isolation above is unproven rather than proven, and the
test says so instead of passing.

Spec: chapter 2 sections 2.5, 2.5.2, 2.6.2, 2.7, 2.8 and acceptance checks 3, 10, 15-20,
22, 36; chapter 8 sections 8.1.1, 8.2.5, 8.6 and acceptance check 12. Requirement ids are
cited per test.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from medos.sealed import isolation as iso
from medos.sealed.abi import (
    ENDPOINTS,
    EXECUTION_STATES,
    ExecutionRequest,
    ExecutionStatus,
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
    SelftestMismatch,
    TransportRefused,
)
from medos.sealed.invoker import Response, SealedInvoker, SealedServiceConfig
from medos.sealed.nifti import affine_from_geometry, read_nifti1_header
from medos.sealed.reference import ReferenceService, Weights, build_server
from medos.sealed.reference import digest_of_manifest as vendor_manifest_digest
from medos.sealed.reference import digest_of_selftest_bundle as vendor_selftest_digest

from tests._support.skips import skip_environment, skip_infra

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = REPO_ROOT / "medos" / "deploy" / "sealed-reference"
MANIFEST_PATH = VENDOR_DIR / "service.manifest.json"
WEIGHTS_PATH = VENDOR_DIR / "weights.bin"
REFERENCE_SOURCE = REPO_ROOT / "medos" / "medos" / "sealed" / "reference.py"

SEALED_CONTAINER = iso.SEALED_SERVICE_CONTAINER
GATEWAY_CONTAINER = "medos-gateway"

#: How many destination/port pairs a probe run must cover before its silence means
#: anything. Six is four destinations' worth: a run that only reached the database and
#: the PACS has not been told about the object store or the inference server.
_PROBE_FLOOR = 6
INVOKER_CONTAINER = "medos-worker"

STUDY = "1.2.826.0.1.3680043.8.498.20000000000000000000000000000001"
SERIES = "1.2.826.0.1.3680043.8.498.20000000000000000000000000000002"
SOPS = tuple(
    f"1.2.826.0.1.3680043.8.498.2000000000000000000000000000000{i}" for i in range(3, 9)
)

# `MOS-SEC-030` wants mTLS with a workload certificate and the compose stack has no mesh.
# Every config below names the deviation rather than defaulting past it; see
# `medos/medos/sealed/isolation.py`'s module docstring for why the gap is recorded and
# not closed.
PLAINTEXT_DEVIATION = (
    "the compose stack has no service mesh and no workload CA, so MOS-SEC-030's mTLS "
    "cannot be satisfied; Z-SERVICE network isolation is the only control in force"
)


# =====================================================================================
# Fixtures: a real vendor service, and a real Gateway for it to read through
# =====================================================================================
@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class _GatewayStub(BaseHTTPRequestHandler):
    """A DICOMweb origin that answers exactly what a sealed service is allowed to ask for.

    It records the method and the Authorization header of every request, which is how
    `MOS-SVC-047` ("MUST NOT issue any HTTP method other than those in `allowed_methods`")
    becomes observable from the platform side without trusting the service's own report.
    """

    calls: list[tuple[str, str, str]] = []

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        type(self).calls.append(
            (self.command, self.path, self.headers.get("Authorization", ""))
        )
        body = json.dumps([{"00080018": {"vr": "UI", "Value": [uid]}} for uid in SOPS])
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/dicom+json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        type(self).calls.append((self.command, self.path, ""))
        # `MOS-SVC-006` / `MOS-SEC-028`: "The Gateway MUST reject a STOW-RS request
        # presented with a service-scoped token." A service cannot STOW, and the origin
        # says so rather than accepting and discarding.
        self.send_response(403)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture(scope="module")
def gateway_stub() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/dicomweb"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def vendor(manifest: dict[str, Any]) -> Iterator[tuple[str, ReferenceService]]:
    """The reference sealed service, on a real socket, with the real weights blob."""
    server, service = build_server(manifest, weights_path=str(WEIGHTS_PATH), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", service
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def config(
    manifest: dict[str, Any], vendor: tuple[str, ReferenceService]
) -> SealedServiceConfig:
    base_url, _service = vendor
    timeouts = manifest["timeouts"]
    return SealedServiceConfig(
        base_url=base_url,
        # From the REGISTRY, computed by the platform at registration (`MOS-SVC-027`).
        registered_manifest_digest=manifest_digest(manifest),
        expected_selftest_digest=manifest["selftest"]["expected_result_bundle_digest"],
        accept_ms=timeouts["accept_ms"],
        analyze_ms=timeouts["analyze_ms"],
        artifact_fetch_ms=timeouts["artifact_fetch_ms"],
        heartbeat_interval_ms=timeouts["heartbeat_interval_ms"],
        max_result_bundle_bytes=manifest["outputs"]["max_result_bundle_bytes"],
        max_artifact_bytes=manifest["outputs"]["max_artifact_bytes"],
        plaintext_deviation=PLAINTEXT_DEVIATION,
    )


def _request(gateway_base: str, **overrides: Any) -> ExecutionRequest:
    kwargs: dict[str, Any] = {
        "execution_id": "exe_01J9ZM7B4K2N6P8R0T3V5X7ZAC",
        "job_id": "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V",
        "deadline_at": "2026-09-16T12:27:11Z",
        "clinical_use_mode": "research_only",
        "capabilities_requested": ("pleural_effusion",),
        "operating_points": {"pleural_effusion": {"id": "balanced",
                                                  "score_threshold": 0.42}},
        "dicomweb_base_url": gateway_base,
        "access_token": "jt_reference_execution_token",
        "token_expires_at": "2026-09-16T12:27:11Z",
        "study_instance_uid": STUDY,
        "roles": ({"name": "primary_axial", "series_instance_uid": SERIES,
                   "frame_of_reference_uid": SERIES + ".1",
                   "instance_count": len(SOPS),
                   "sop_instance_uids": list(SOPS)},),
        "max_result_bundle_bytes": 4_194_304,
        "max_artifact_bytes": 268_435_456,
    }
    kwargs.update(overrides)
    return ExecutionRequest(**kwargs)


# =====================================================================================
# PART A.1 -- the endpoint table is CLOSED, and closure is enforced in code
# =====================================================================================
def test_the_abi_table_is_exactly_the_nine_rows_of_section_2_5_2() -> None:
    """Section 2.5.2: "A sealed service exposes exactly these HTTP endpoints ... There are
    no others."

    Pinned as a set, so that adding a tenth row is a deliberate edit to this list and not a
    side effect of adding a convenience endpoint to the reference service.
    """
    rows = {(e.method, e.path) for e in ENDPOINTS}
    assert rows == {
        ("GET", "/healthz"),
        ("GET", "/readyz"),
        ("GET", "/v1/manifest"),
        ("POST", "/v1/selftest"),
        ("POST", "/v1/executions"),
        ("GET", "/v1/executions/{id}"),
        ("GET", "/v1/executions/{id}/artifacts/{artifact_id}"),
        ("DELETE", "/v1/executions/{id}"),
        ("POST", "/v1/executions/{id}/cancel"),
    }
    # `MOS-SVC-048`: "All label maps and key images MUST be served from GET
    # /v1/executions/{id}/artifacts/{artifact_id}. A service MUST NOT push artifacts
    # anywhere." There is no method by which it could: no PUT or POST row touches an
    # artifact path anywhere in the table.
    writes = {
        (m, p) for m, p in rows if m in ("PUT", "PATCH") or "artifact" in p and m != "GET"
    }
    assert not writes, f"the ABI table contains a write path for artifacts: {writes}"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/executions/exe_1/logs"),
        ("GET", "/v1/admin"),
        ("POST", "/v1/executions/exe_1/artifacts/a1"),
        ("PUT", "/v1/executions/exe_1"),
        ("GET", "/metrics"),
        ("GET", "/v1/manifest/raw"),
    ],
)
def test_no_path_outside_the_table_resolves(method: str, path: str) -> None:
    """A prefix of a table row is not a table row.

    `/v1/executions/{id}` is a prefix of `/v1/executions/{id}/logs`, and a matcher that
    accepted prefixes would re-open the closed table through the back door. Matching is
    segment-by-segment for exactly this reason.
    """
    assert endpoint_for(method, path) is None


def test_the_invoker_refuses_to_send_a_request_outside_the_table(
    config: SealedServiceConfig,
) -> None:
    """Section 2.5.2: "the platform MUST NOT call any path outside this table."

    Enforced where requests are CONSTRUCTED rather than reviewed where they are written,
    so a future caller reaching for an off-table path gets an exception and not a 404.
    """
    invoker = SealedInvoker(config)
    with pytest.raises(ProtocolViolation, match="not a row of the"):
        invoker._request("GET", "/v1/executions/exe_1/logs")
    with pytest.raises(ProtocolViolation, match="not a row of the"):
        invoker._request("POST", "/v1/admin/shutdown", body={})


def test_the_reference_service_answers_404_off_the_table(
    vendor: tuple[str, ReferenceService],
) -> None:
    """The other half of the closure: "a service MUST NOT expose an administrative surface".

    Driven with a raw socket rather than the invoker, because the invoker will not send
    these -- and "the platform cannot ask" is not the same claim as "the service does not
    answer".
    """
    base_url, _ = vendor
    host, port = base_url.rsplit("/", 1)[-1].split(":")
    for method, path in (("GET", "/metrics"), ("GET", "/v1/admin"),
                         ("PUT", "/v1/executions/exe_1"), ("GET", "/v1/manifest/raw")):
        with socket.create_connection((host, int(port)), 5) as sock:
            sock.sendall(
                f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
                f"Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )
            # Drained to EOF rather than `recv(64)`: closing mid-response resets the
            # connection and the server logs a traceback, which would leave real failures
            # buried in noise this test manufactured.
            chunks = []
            while chunk := sock.recv(4096):
                chunks.append(chunk)
        head = b"".join(chunks).decode("latin-1")
        assert " 404 " in head, f"{method} {path} answered {head.splitlines()[0]!r}, not 404"


# =====================================================================================
# PART A.2 -- the two digests, computed twice by two parties
# =====================================================================================
def test_vendor_and_platform_compute_the_same_manifest_digest(
    manifest: dict[str, Any],
) -> None:
    """`MOS-SVC-027`, and the reason it is worth a test of its own.

    The digest is computed independently by two implementations that never share a line:
    `medos/medos/core/canonical.py` for the platform and `_jcs` inside the vendor's own image.
    That is not duplication to be removed -- it is the contract. RFC 8785's third rule is
    ECMAScript number formatting, under which `1.0` prints as `1`; an implementation that
    skips it disagrees with every other one on any manifest containing an integral float,
    and `execution.sealed.port: 8080` and `score_threshold: 0.42` put such numbers in every
    real manifest. This test is what caught exactly that during development.
    """
    assert vendor_manifest_digest(manifest) == manifest_digest(manifest)


def test_the_declared_selftest_digest_is_the_one_the_image_produces(
    manifest: dict[str, Any], vendor: tuple[str, ReferenceService]
) -> None:
    """`MOS-SVC-026`, from both sides, against the real weights blob.

    `expected_result_bundle_digest` is the vendor's claim about its own image. The platform
    recomputes it from the bundle the image actually returns, with its own canonicaliser.
    """
    _base, service = vendor
    bundle = service.selftest_bundle()
    declared = manifest["selftest"]["expected_result_bundle_digest"]
    assert vendor_selftest_digest(bundle) == declared
    assert selftest_digest(bundle) == declared


def test_the_selftest_digest_does_not_depend_on_the_manifest_digest(
    manifest: dict[str, Any], vendor: tuple[str, ReferenceService]
) -> None:
    """The resolved cycle, asserted so the resolution cannot be undone by accident.

    `MOS-SVC-026` puts the self-test digest inside the manifest; `MOS-SVC-027` digests the
    manifest; `MOS-SVC-077` puts THAT digest inside every `ResultBundle`, including the
    self-test one. Under SHA-256 there is no fixed point, so `service.manifest_digest` is
    removed from the self-test digest's input (see `medos/medos/sealed/abi.py:selftest_digest`).

    The property that makes the resolution safe is this one: changing the manifest digest
    MUST NOT change the self-test digest. If a later edit restores the member, this test
    fails and the manifest becomes uncomputable again.
    """
    _base, service = vendor
    bundle = dict(service.selftest_bundle())
    before = selftest_digest(bundle)
    bundle["service"] = {**bundle["service"], "manifest_digest": "sha256:" + "ab" * 32}
    assert selftest_digest(bundle) == before
    # And the removal is narrow: the service's IDENTITY still binds the digest.
    bundle["service"] = {**bundle["service"], "id": "com.example.someone-else"}
    assert selftest_digest(bundle) != before


# =====================================================================================
# PART A.3 -- section 2.6.2 steps 3 and 4, which MOS-SVC-052 forbids turning off
# =====================================================================================
def test_step_3_accepts_the_registered_manifest_and_refuses_any_other(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """Section 2.6.2 step 3. "Mismatch => job FAILED with `service_manifest_mismatch`; the
    `ServiceVersion` transitions to `SUSPENDED`."

    The consequence is carried on the exception type rather than left to the caller to
    remember, because "suspend the version for every tenant" and "fail this job" are not
    interchangeable outcomes.
    """
    invoker = SealedInvoker(config)
    assert invoker.verify_manifest() == manifest_digest(manifest)

    wrong = SealedInvoker(
        SealedServiceConfig(
            **{**_as_kwargs(config), "registered_manifest_digest": "sha256:" + "00" * 32}
        )
    )
    with pytest.raises(ManifestMismatch) as excinfo:
        wrong.verify_manifest()
    assert excinfo.value.code == "service_manifest_mismatch"
    assert "SUSPENDED" in excinfo.value.consequence


def test_the_manifest_endpoint_serves_yaml_by_default_and_json_on_request(
    vendor: tuple[str, ReferenceService], config: SealedServiceConfig
) -> None:
    """Section 2.5.2's table says `application/yaml`; `MOS-SVC-027` digests the JSON form.

    Both are true of one document on one path. The default is the table's media type; the
    platform asks for JSON because it must canonicalise, and adding a YAML READER to the
    platform to bridge the gap would be a parser on the untrusted-input path bought for one
    digest.
    """
    base_url, _ = vendor
    invoker = SealedInvoker(config)
    default, _endpoint = invoker._request("GET", "/v1/manifest")
    assert default.headers["content-type"].startswith("application/yaml")
    assert b"schema_version:" in default.body and b'"1.0.0"' in default.body

    negotiated, _ = invoker._request("GET", "/v1/manifest", accept="application/json")
    assert negotiated.headers["content-type"].startswith("application/json")
    assert json.loads(negotiated.body)["metadata"]["id"] == "com.example.sealed-reference"
    assert base_url  # the fixture really did serve these


def test_a_yaml_only_service_fails_closed_rather_than_skipping_step_3(
    config: SealedServiceConfig,
) -> None:
    """`MOS-SVC-052`: step 3 "MUST NOT be configurable off".

    A vendor that implements only the table's `application/yaml` is conformant to the
    table and leaves this platform unable to compute `manifest_digest`. The answer is a
    typed refusal, because "we could not check" is the shape every disabled check takes on
    the way out.
    """
    transport = _ScriptedTransport({
        ("GET", "/v1/manifest"): Response(200, {"content-type": "application/yaml"},
                                          b"schema_version: 1.0.0\n"),
    })
    invoker = SealedInvoker(config, transport=transport)
    with pytest.raises(ManifestNotCanonicalisable):
        invoker.verify_manifest()


def test_step_4_runs_the_shipped_fixture_and_refuses_a_drifted_image(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """`MOS-SVC-026`: "A runner MUST run the self-test once per pod after readiness and
    MUST refuse to serve on mismatch."

    The consequence here is the POD, not the version -- a self-test failure can be a
    node-local fault, and suspending a vendor for every tenant over one bad GPU would be a
    worse outage than the one prevented.
    """
    bundle = SealedInvoker(config).run_selftest()
    assert selftest_digest(bundle) == manifest["selftest"]["expected_result_bundle_digest"]
    assert bundle["status"] == "completed"

    drifted = SealedInvoker(
        SealedServiceConfig(
            **{**_as_kwargs(config), "expected_selftest_digest": "sha256:" + "11" * 32}
        )
    )
    with pytest.raises(SelftestMismatch) as excinfo:
        drifted.run_selftest()
    assert "POD is drained" in excinfo.value.consequence


# =====================================================================================
# PART A.4 -- one whole execution, steps 5 to 10
# =====================================================================================
def test_a_complete_sealed_execution_is_verified_end_to_end(
    config: SealedServiceConfig, gateway_stub: str, manifest: dict[str, Any]
) -> None:
    """Section 2.6.2 steps 5-10 against the real service, over real sockets.

    Everything the invoker returns has been through step 9: the bundle echoes the right
    ids, attributes itself to the registered manifest, cites only dispatched instances, and
    every artifact was pulled, size-checked, digest-checked and opened to confirm it is on
    the declared grid.
    """
    _GatewayStub.calls.clear()
    invoker = SealedInvoker(config)
    request = _request(gateway_stub)
    platform_geometry = {
        "shape": [len(SOPS), 16, 16],
        "spacing_mm": [1.0, 0.7, 0.7],
        "origin_lps_mm": [-120.0, -160.0, -80.0],
        "direction_lps": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
    }
    result = invoker.execute(request, platform_geometry=platform_geometry)

    bundle = result.result_bundle
    assert bundle["status"] == "completed"
    assert bundle["execution_id"] == request.execution_id
    assert bundle["service"]["manifest_digest"] == manifest_digest(manifest)
    assert bundle["narrative"] is None                      # MOS-SVC-073
    assert bundle["diagnostics"]["deterministic"] is True   # MOS-SVC-075
    assert [o["outcome"] for o in bundle["capability_outcomes"]] == ["produced"]

    # `MOS-SVC-044`: phases are open snake_case strings and steps are integers. No float
    # progress was sent, and none was synthesised.
    assert all(p.replace("_", "").isalnum() and p.islower() for p in result.phases)
    assert result.phases[-1] == "done"

    # The artifact was pulled (`MOS-SVC-048`) and is a real NIfTI on the declared grid.
    (artifact_id, payload), = result.artifacts.items()
    assert artifact_id == bundle["label_maps"][0]["artifact_id"]
    header = read_nifti1_header(payload)
    assert list(header.shape) == [16, 16, len(SOPS)]
    assert header.dtype_name == "uint8"                     # MOS-SVC-085

    # `MOS-SVC-047`: the service used the Job Token only against the Gateway, and used
    # only `GET`. Read off the Gateway's own record, not the service's report.
    methods = {method for method, _path, _auth in _GatewayStub.calls}
    assert methods == {"GET"}, f"the service issued {methods}; allowed_methods was ['GET']"
    assert all(auth == "Bearer jt_reference_execution_token"
               for _m, _p, auth in _GatewayStub.calls)

    # Step 10 released it: a second DELETE finds nothing.
    with pytest.raises(ProtocolViolation):
        invoker.release(request.execution_id)
    assert manifest["execution"]["mode"] == "sealed"


def test_the_execution_is_idempotent_on_execution_id(
    config: SealedServiceConfig, gateway_stub: str
) -> None:
    """`MOS-SVC-045`, both halves, and chapter 5's retry path depends on the first.

    "A repeat with the same `execution_id` and an identical body MUST return `202`
    referring to the same execution. A repeat with the same `execution_id` and a different
    body MUST return `409`."
    """
    invoker = SealedInvoker(config)
    request = _request(gateway_stub, execution_id="exe_01J9ZM7B4K2N6P8R0T3V5X7ZAD")
    assert invoker.start(request) == request.execution_id
    assert invoker.start(request) == request.execution_id       # identical body -> 202

    # A "retry" that changed the study is not a retry.
    different = _request(
        gateway_stub,
        execution_id=request.execution_id,
        study_instance_uid=STUDY[:-1] + "9",
    )
    with pytest.raises(ExecutionConflict):
        invoker.start(different)
    invoker.release(request.execution_id)


def test_a_reissued_job_token_is_not_a_different_body(
    config: SealedServiceConfig, gateway_stub: str
) -> None:
    """`MOS-SEC-024` reissues the token on deadline extension; `MOS-SVC-045` must not care.

    If the gateway block counted toward the idempotency fingerprint, every token refresh
    would turn a legitimate retry into a `409` -- a platform that cannot retry a job whose
    deadline was extended, which is precisely the job most likely to need retrying.
    """
    invoker = SealedInvoker(config)
    first = _request(gateway_stub, execution_id="exe_01J9ZM7B4K2N6P8R0T3V5X7ZAE")
    refreshed = _request(
        gateway_stub,
        execution_id=first.execution_id,
        access_token="jt_reference_execution_token_v2",
        token_expires_at="2026-09-16T13:27:11Z",
    )
    assert first.idempotency_fingerprint() == refreshed.idempotency_fingerprint()
    assert invoker.start(first) == first.execution_id
    assert invoker.start(refreshed) == first.execution_id
    invoker.release(first.execution_id)


def test_the_post_handler_accepts_without_analysing(
    config: SealedServiceConfig, gateway_stub: str
) -> None:
    """`MOS-SVC-046`: "MUST accept within `timeouts.accept_ms` and MUST NOT perform
    inference inside the `POST` handler."

    A `200` on `POST /v1/executions` is the prohibition broken -- it means the answer was
    ready when the call returned. The first poll after acceptance must therefore find a
    non-terminal state, which is what "did not analyse in the handler" looks like from
    outside.
    """
    invoker = SealedInvoker(config)
    request = _request(gateway_stub, execution_id="exe_01J9ZM7B4K2N6P8R0T3V5X7ZAF")
    response, endpoint = invoker._request("POST", "/v1/executions", body=request.to_wire())
    assert endpoint.success == 202
    assert response.status == 202, "a 200 here means the service analysed in the handler"
    status = invoker.poll(request.execution_id)
    assert status.state in EXECUTION_STATES
    invoker.wait(request.execution_id)
    invoker.release(request.execution_id)


def test_cancel_is_implemented_in_this_release(
    config: SealedServiceConfig, gateway_stub: str
) -> None:
    """`MOS-SVC-117`: `501` in 0.1.0-0.2.0, "implementable from 0.3". This is 0.3.0.

    The service-side half only. Mapping a cancelled execution onto the reserved `Job` state
    `CANCELLED` is chapter 5's and is deliberately not touched here.
    """
    invoker = SealedInvoker(config)
    request = _request(gateway_stub, execution_id="exe_01J9ZM7B4K2N6P8R0T3V5X7ZAG")
    invoker.start(request)
    invoker.cancel(request.execution_id)
    status = invoker.poll(request.execution_id)
    assert status.state == "failed" and status.error["code"] == "cancelled"
    invoker.release(request.execution_id)


# =====================================================================================
# PART A.5 -- being the adversary: what step 9 catches
# =====================================================================================
class _ScriptedTransport:
    """A malicious or broken vendor, spoken directly onto the wire.

    There is no such service to run, and building one that is selectively dishonest would
    mean putting the lies into `reference.py`, where they would be one edit away from
    shipping. Scripting the responses keeps the adversary in the test file.
    """

    def __init__(self, responses: Mapping[tuple[str, str], Response]) -> None:
        self.responses = dict(responses)
        self.sent: list[tuple[str, str]] = []

    def request(
        self, method: str, url: str, *, body: bytes | None = None,
        content_type: str | None = None, accept: str | None = None,
        timeout_s: float = 30.0, max_bytes: int | None = None,
    ) -> Response:
        path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        self.sent.append((method, path))
        for (want_method, want_path), response in self.responses.items():
            if want_method == method and want_path == path:
                if max_bytes is not None and len(response.body) > max_bytes:
                    raise ArtifactRefused(
                        f"{url} exceeded the declared ceiling of {max_bytes} bytes"
                    )
                return response
        return Response(404, {"content-type": "application/json"}, b'{"code":"nope"}')


def _scripted(
    config: SealedServiceConfig,
    bundle: Mapping[str, Any],
    artifacts: Mapping[str, bytes],
    execution_id: str,
) -> SealedInvoker:
    responses: dict[tuple[str, str], Response] = {
        ("POST", "/v1/executions"): Response(
            202, {"content-type": "application/json"},
            json.dumps({"execution_id": execution_id}).encode()),
        ("GET", f"/v1/executions/{execution_id}"): Response(
            200, {"content-type": "application/json"},
            json.dumps({
                "schema_version": "1.0.0", "execution_id": execution_id,
                "state": "succeeded", "phase": "done", "steps_completed": 5,
                "steps_total": 5, "result_bundle": bundle, "error": None,
            }).encode()),
        ("DELETE", f"/v1/executions/{execution_id}"): Response(204, {}, b""),
    }
    for artifact_id, payload in artifacts.items():
        responses[("GET", f"/v1/executions/{execution_id}/artifacts/{artifact_id}")] = (
            Response(200, {"content-type": "application/octet-stream"}, payload)
        )
    return SealedInvoker(config, transport=_ScriptedTransport(responses))


def _honest_output(
    manifest: dict[str, Any], execution_id: str, job_id: str
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """A bundle the reference service would really produce, as a base to corrupt."""
    service = ReferenceService(manifest, Weights(str(WEIGHTS_PATH)))
    artifacts: dict[str, bytes] = {}
    bundle = service._bundle(
        execution_id=execution_id, job_id=job_id, study=STUDY, series=SERIES,
        sop_uids=SOPS, capabilities=("pleural_effusion",),
        operating_points={"pleural_effusion": {"id": "balanced", "score_threshold": 0.42}},
        gateway_outcome="reached", artifacts=artifacts,
    )
    return bundle, artifacts


def test_an_artifact_whose_bytes_do_not_match_its_digest_is_refused(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """Chapter 2 acceptance check 17: "the job ends FAILED with `artifact_digest_mismatch`".

    Nothing reaches the object store, which is the point: the invoker holds the bytes in
    memory until they have been verified, precisely so a failure has nothing to clean up.
    """
    execution_id = "exe_01J9ZM7B4K2N6P8R0T3V5X7ZB1"
    bundle, artifacts = _honest_output(manifest, execution_id, "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V")
    artifact_id = bundle["label_maps"][0]["artifact_id"]
    # A byte FLIPPED rather than appended, and the distinction is the test. Appending
    # would trip the length ceiling first and the digest would never be computed; flipping
    # leaves `size_bytes` honest, so the only thing left that can catch it is the hash.
    payload = bytearray(artifacts[artifact_id])
    payload[-1] ^= 0xFF
    artifacts[artifact_id] = bytes(payload)
    invoker = _scripted(config, bundle, artifacts, execution_id)
    with pytest.raises(ArtifactRefused, match="digest"):
        invoker.execute(_request("http://127.0.0.1:1/dicomweb",
                                 execution_id=execution_id))


def test_an_artifact_over_the_declared_ceiling_is_refused_while_streaming(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """`outputs.max_artifact_bytes` is a ceiling held DURING the read, not after it.

    A service answering `200` with forty gigabytes is a denial of service against the
    invoker, and finding the overrun after buffering it is finding it too late. The bundle
    declares a small `size_bytes` and the body is larger, which is the cheap version of the
    same lie.
    """
    execution_id = "exe_01J9ZM7B4K2N6P8R0T3V5X7ZB2"
    bundle, artifacts = _honest_output(manifest, execution_id, "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V")
    artifact_id = bundle["label_maps"][0]["artifact_id"]
    artifacts[artifact_id] = artifacts[artifact_id] + b"\x00" * 4096
    invoker = _scripted(config, bundle, artifacts, execution_id)
    with pytest.raises(ArtifactRefused):
        invoker.execute(_request("http://127.0.0.1:1/dicomweb",
                                 execution_id=execution_id))


def test_a_label_map_on_the_wrong_grid_is_refused(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """`MOS-SVC-082` and chapter 2 acceptance check 15: a `[311,512,512]` array against a
    `[312,512,512]` grid fails with `geometry_mismatch`, and no DICOM object is written.

    The declaration is not the check. The invoker OPENS the artifact, because a label map
    becomes a SEG burned onto a patient's images and a plausible-looking array on the wrong
    grid is a segmentation drawn on the wrong anatomy.
    """
    execution_id = "exe_01J9ZM7B4K2N6P8R0T3V5X7ZB3"
    bundle, artifacts = _honest_output(manifest, execution_id, "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V")
    # The artifact is untouched and its digest still matches; only the bundle's declared
    # grid moves. That is the interesting direction: the bytes are honest and the claim is
    # not, so only opening them catches it.
    bundle["geometry"]["shape"] = [len(SOPS) - 1, 16, 16]
    invoker = _scripted(config, bundle, artifacts, execution_id)
    with pytest.raises(ArtifactRefused, match="geometry_mismatch"):
        invoker.execute(_request("http://127.0.0.1:1/dicomweb",
                                 execution_id=execution_id))


def test_a_label_map_affine_off_by_two_microns_per_mm_is_refused(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """Chapter 2 acceptance check 16: an affine differing by `2e-3` mm in origin fails.

    The tolerance of section 2.6.2 step 9 is `1e-4`, so `2e-3` is twenty times over. A
    check written with a loose tolerance would pass this and shift every segment by two
    microns per millimetre of the volume, which is invisible until it is measured.
    """
    execution_id = "exe_01J9ZM7B4K2N6P8R0T3V5X7ZB4"
    bundle, artifacts = _honest_output(manifest, execution_id, "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V")
    bundle["geometry"]["origin_lps_mm"][0] += 2e-3
    invoker = _scripted(config, bundle, artifacts, execution_id)
    with pytest.raises(ArtifactRefused, match="affine differs"):
        invoker.execute(_request("http://127.0.0.1:1/dicomweb",
                                 execution_id=execution_id))
    # THE OTHER HALF, and without it this test would pass on a checker that rejected
    # everything: an offset of 4e-5 mm is inside the 1e-4 tolerance and MUST be accepted.
    # A geometry check with no upper side is a geometry check nobody can ship against.
    bundle["geometry"]["origin_lps_mm"][0] = -120.0 + 4e-5
    accepted = _scripted(config, bundle, artifacts, execution_id).execute(
        _request("http://127.0.0.1:1/dicomweb", execution_id=execution_id)
    )
    assert accepted.result_bundle["status"] == "completed"
    affine = affine_from_geometry(bundle["geometry"])
    assert affine[0][3] == pytest.approx(120.0, abs=1e-3), (
        "row 0 is negated on the way to RAS; a comparison that forgot the LPS->RAS "
        "conversion would reject every correct label map and accept a mirrored one"
    )


def test_a_finding_citing_an_instance_that_was_not_dispatched_is_refused(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """`MOS-SVC-088` and chapter 2 acceptance check 18.

    "Every listed UID MUST be a member of the dispatched selection." It is what makes the
    SR evidence sequence resolvable, and it is also what catches a service reaching past
    its Job Token scope: a UID it was not given was either leaked to it or invented by it,
    and both are refusals.
    """
    execution_id = "exe_01J9ZM7B4K2N6P8R0T3V5X7ZB5"
    bundle, artifacts = _honest_output(manifest, execution_id, "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V")
    bundle["findings"][0]["source_sop_instance_uids"].append(SERIES + ".999")
    invoker = _scripted(config, bundle, artifacts, execution_id)
    with pytest.raises(EvidenceOutsideSelection) as excinfo:
        invoker.execute(_request("http://127.0.0.1:1/dicomweb",
                                 execution_id=execution_id))
    assert excinfo.value.code == "schema_invalid"


@pytest.mark.parametrize(
    "mutate,expected",
    [
        pytest.param(
            lambda b: b["findings"][0].update({"score": 0.30,
                                               "operating_point": {"id": "balanced",
                                                                   "score_threshold": 0.42},
                                               "present": True}),
            "MOS-SVC-089",
            id="threshold-inconsistent",
        ),
        pytest.param(
            lambda b: b.update({"narrative": "No acute cardiopulmonary process."}),
            "MOS-SVC-073",
            id="narrative-not-null",
        ),
        pytest.param(
            lambda b: b["label_maps"][0].pop("role"),
            "MOS-SVC-121",
            id="label-map-without-role",
        ),
        pytest.param(
            lambda b: b.update({"status": "rejected",
                                "rejection": {"reason_code": "vendor_said_no"}}),
            "MOS-SVC-095",
            id="rejection-outside-the-closed-enum",
        ),
        pytest.param(
            lambda b: b.pop("measurements"),
            "section 2.8.1",
            id="required-array-absent",
        ),
    ],
)
def test_a_malformed_bundle_is_rejected_before_any_dicom_write(
    config: SealedServiceConfig, manifest: dict[str, Any], mutate: Any, expected: str
) -> None:
    """Chapter 2 acceptance checks 19, 20 and 22, and the members section 2.8.1 requires.

    Every one of these is a bundle that would otherwise be written into a DICOM SR or SEG:
    a `present: true` that contradicts its own threshold reads to a clinician as a positive
    finding, and a rejection outside `MOS-SVC-095`'s closed enum cannot be mapped to a
    `clinical_rejection` problem at all.
    """
    execution_id = "exe_01J9ZM7B4K2N6P8R0T3V5X7ZB6"
    bundle, artifacts = _honest_output(manifest, execution_id, "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V")
    mutate(bundle)
    invoker = _scripted(config, bundle, artifacts, execution_id)
    with pytest.raises(BundleInvalid) as excinfo:
        invoker.execute(_request("http://127.0.0.1:1/dicomweb",
                                 execution_id=execution_id))
    assert excinfo.value.code == "schema_invalid", expected


def test_a_float_progress_value_is_a_protocol_violation() -> None:
    """`MOS-SVC-044` and chapter 5 `MOS-EXEC-001`: no float progress, from either side.

    Rejected rather than ignored. A service that sends one believes the platform is
    displaying it, and a platform that silently drops it has agreed to a contract it is not
    honouring.
    """
    body = {
        "schema_version": "1.0.0", "execution_id": "exe_x", "state": "running",
        "phase": "inference", "steps_completed": 3, "steps_total": 6,
        "progress": 0.5, "result_bundle": None, "error": None,
    }
    assert any("progress" in p for p in ExecutionStatus.problems(body))
    with pytest.raises(ValueError, match="progress"):
        ExecutionStatus.parse(body)


def test_the_bundle_must_attribute_itself_to_the_registered_manifest(
    config: SealedServiceConfig, manifest: dict[str, Any]
) -> None:
    """`MOS-SVC-077`: "`service.manifest_digest` MUST equal the registered digest."

    Step 3 asks the image what it is; this asks the ANSWER what produced it. They are not
    the same check -- a bundle attributed to an unregistered manifest is what ends up in
    the SEG's equipment tags and in provenance.
    """
    execution_id = "exe_01J9ZM7B4K2N6P8R0T3V5X7ZB7"
    bundle, artifacts = _honest_output(manifest, execution_id, "job_01J9ZM7A9C1E3G5J7H9N1Q3S5V")
    bundle["service"]["manifest_digest"] = "sha256:" + "cd" * 32
    invoker = _scripted(config, bundle, artifacts, execution_id)
    with pytest.raises(BundleInvalid, match="attributes itself"):
        invoker.execute(_request("http://127.0.0.1:1/dicomweb",
                                 execution_id=execution_id))


# =====================================================================================
# PART A.6 -- the credential rules, which are the reason sealed mode exists
# =====================================================================================
def test_an_execution_request_may_only_grant_GET(gateway_stub: str) -> None:
    """`MOS-SEC-028`: "A service principal's `scope` MUST NOT contain `study.write` or any
    other write verb: a service cannot STOW."

    Refused at CONSTRUCTION. A platform that can express a writeable execution request has
    already lost the argument, whatever the Gateway then does about it -- and `MOS-SVC-060`
    says the same thing from the other end: "`requested_dicom_kinds` is a request, not a
    grant."
    """
    with pytest.raises(ValueError, match="allowed_methods"):
        _request(gateway_stub, allowed_methods=("GET", "POST"))
    with pytest.raises(ValueError, match="access_token is empty"):
        _request(gateway_stub, access_token="")


def test_the_job_token_travels_in_the_body_and_nowhere_else(gateway_stub: str) -> None:
    """`MOS-SEC-025`: the token is a mounted file, or the `gateway.access_token` member of
    the execution request body -- "it MUST NOT be delivered any other way".

    The environment-variable prohibition has a stated reason worth keeping in view: env
    vars and argv "appear in `/proc`, crash dumps and container inspection output". Part B
    checks the container's environment for exactly that.
    """
    wire = _request(gateway_stub).to_wire()
    assert wire["gateway"]["access_token"]
    flattened = json.dumps({k: v for k, v in wire.items() if k != "gateway"})
    assert "jt_reference_execution_token" not in flattened


def test_a_plaintext_service_url_is_refused_unless_the_deviation_is_recorded() -> None:
    """`MOS-SEC-030`: "The platform MUST call a `sealed` service over mTLS presenting its
    own workload certificate; the service MUST validate the SAN."

    The compose stack has no mesh and no workload CA, so this requirement is NOT met here.
    It is recorded as a refusal-unless-named rather than as a comment, so that the gap
    appears in the configuration of every deployment that has it -- where an auditor reads
    configurations -- instead of in a Python docstring.
    """
    with pytest.raises(TransportRefused, match="MOS-SEC-030"):
        SealedServiceConfig(
            base_url="http://sealed.invalid:8080",
            registered_manifest_digest="sha256:" + "00" * 32,
            expected_selftest_digest="sha256:" + "00" * 32,
        )
    named = SealedServiceConfig(
        base_url="http://sealed.invalid:8080",
        registered_manifest_digest="sha256:" + "00" * 32,
        expected_selftest_digest="sha256:" + "00" * 32,
        plaintext_deviation=PLAINTEXT_DEVIATION,
    )
    assert "mTLS" in named.plaintext_deviation


def test_the_weights_are_never_surrendered_by_any_row_of_the_abi(
    config: SealedServiceConfig, gateway_stub: str
) -> None:
    """Section 2.5's mode table: "inside the vendor image; never surrendered".

    Every reachable row of the endpoint table is called and every response body is searched
    for the weight bytes. This is the property that makes sealed mode worth building: a
    vendor can be made to run inside a hospital without handing the hospital its model.
    """
    weights = WEIGHTS_PATH.read_bytes()
    needle = weights[:64]
    invoker = SealedInvoker(config)
    request = _request(gateway_stub, execution_id="exe_01J9ZM7B4K2N6P8R0T3V5X7ZB8")
    result = invoker.execute(request)

    bodies: list[bytes] = []
    for method, path in (("GET", "/healthz"), ("GET", "/readyz"),
                         ("GET", "/v1/manifest"), ("POST", "/v1/selftest")):
        response, _ = invoker._request(
            method, path, body={} if method == "POST" else None,
            timeout_ms=config.analyze_ms,
        )
        bodies.append(response.body)
    bodies.extend(result.artifacts.values())
    bodies.append(json.dumps(result.result_bundle).encode())

    for body in bodies:
        assert needle not in body, "a sealed service returned its own weights"
    # The DIGEST is disclosed, and deliberately: `diagnostics` records which weights
    # produced the answer (provenance, MOS-SVC-068) and a digest is not the weights.
    assert result.result_bundle["diagnostics"]["weights_digest"].startswith("sha256:")


def test_the_reference_service_imports_nothing_from_the_platform() -> None:
    """A sealed service that needed `medos` would not be sealed.

    Parsed rather than grepped, so that a string mentioning the package does not fail and a
    lazily imported one does not pass. This is what lets
    `medos/deploy/compose/Dockerfile.sealed` copy ONE file into a stock Python image: if this
    test fails, that Dockerfile stops building and the boundary has quietly moved.
    """
    tree = ast.parse(REFERENCE_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "medos" not in imported, (
        f"medos/medos/sealed/reference.py imports {sorted(imported)}; a sealed service ships "
        "in its own image with its own runtime (chapter 2 section 2.5)"
    )


# =====================================================================================
# PART B -- the deployment. `MOS-SEC-004`: enforced by per-service networks,
#           "not only by configuration of the client".
# =====================================================================================
def _docker() -> str:
    path = shutil.which("docker")
    if path is None:
        skip_infra(
            "the docker CLI is not on PATH, so Z-SERVICE's network isolation cannot be "
            "established. Refusing to assume it holds.",
            dependency="docker",
        )
    return path


def _docker_out(*args: str, timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(
        [_docker(), *args], capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# Output that means "this container could not be asked", as opposed to "this container was
# asked and could not connect". The difference is the whole test: a container that is
# absent or restarting is not evidence of isolation. Lifted from
# tests/integration/test_gateway.py, where the same distinction guards MOS-DATA-006.
_UNPROBEABLE = (
    "no such container",
    "is not running",
    "is restarting",
    "executable file not found",
    "oci runtime exec failed",
    "cannot exec",
)


def _tcp_probe_from(container: str, address: str, port: int) -> str | None:
    """`reached`, `refused`, or None when the container cannot be asked at all.

    The target is an IP ADDRESS and never a DNS name. A container that cannot RESOLVE
    `postgres` may still route to it, and `MOS-SEC-004` is about the connection.

    `socket.create_connection` and not a database driver: the sealed image ships no
    psycopg, no boto3 and no Kafka client, and reading that absence as the boundary is the
    mistake this probe exists to refuse. A raw socket is what an attacker inside the
    container has, and it is what gets used here.
    """
    python = (
        "import socket\n"
        f"socket.create_connection(('{address}', {port}), 4).close()\n"
        "print('MEDOS_REACHED')\n"
    )
    attempts = (
        ["exec", container, "python", "-c", python],
        ["exec", container, "python3", "-c", python],
        ["exec", container, "sh", "-c",
         f"nc -w 4 -z {address} {port} && echo MEDOS_REACHED || echo MEDOS_REFUSED"],
    )
    for args in attempts:
        code, out = _docker_out(*args, timeout=90)
        if "MEDOS_REACHED" in out:
            return "reached"
        if any(marker in out.lower() for marker in _UNPROBEABLE):
            continue  # the tool or the container is missing; this says nothing
        if "MEDOS_REFUSED" in out or code != 0:
            return "refused"
    return None


def _observe(container: str) -> iso.ContainerObservation:
    """One `docker inspect`, reduced to the fields `medos/medos/sealed/isolation.py` reads."""
    # `{{json .}}` and not a `dict` template: docker's Go templates carry no `dict`
    # function, and a template that fails to parse returns a non-zero exit that reads
    # exactly like "the container is not running" -- which would report a MISSING
    # observation as an isolated one. Parsing the whole document has no such failure mode.
    code, out = _docker_out("inspect", container, "--format", "{{json .}}")
    if code != 0:
        return iso.ContainerObservation(name=container, running=False)
    raw = json.loads(out.strip())
    settings = raw.get("NetworkSettings") or {}
    host = raw.get("HostConfig") or {}
    config = raw.get("Config") or {}
    return iso.ContainerObservation(
        name=container,
        networks={
            key: (value or {}).get("IPAddress", "")
            for key, value in (settings.get("Networks") or {}).items()
        },
        published_ports=tuple(
            port for port, bindings in (settings.get("Ports") or {}).items() if bindings
        ),
        user=config.get("User") or "",
        read_only_root_filesystem=bool(host.get("ReadonlyRootfs")),
        privileged=bool(host.get("Privileged")),
        cap_add=tuple(host.get("CapAdd") or ()),
        cap_drop=tuple(host.get("CapDrop") or ()),
        env_names=tuple(line.split("=", 1)[0] for line in (config.get("Env") or ())),
        running=(raw.get("State") or {}).get("Status") == "running",
    )


@pytest.fixture(scope="module")
def sealed_container() -> iso.ContainerObservation:
    observation = _observe(SEALED_CONTAINER)
    if not observation.running:
        skip_infra(
            f"{SEALED_CONTAINER} is not running, so nothing below would be evidence of "
            "isolation. `docker compose up -d medos-sealed-service` in medos/deploy/compose.",
            dependency="medos-sealed-service",
        )
    return observation


@pytest.fixture(scope="module")
def deployment() -> dict[str, iso.ContainerObservation]:
    names = {SEALED_CONTAINER, GATEWAY_CONTAINER, INVOKER_CONTAINER}
    names.update(c for dest in iso.FORBIDDEN_DESTINATIONS for c in dest.containers)
    return {name: _observe(name) for name in sorted(names)}


def test_the_sealed_container_is_hardened_as_MOS_SVC_050_requires(
    sealed_container: iso.ContainerObservation,
) -> None:
    """`MOS-SVC-050`: non-root, read-only root filesystem, no `CAP_NET_RAW` or
    `CAP_SYS_ADMIN`, and no ingress but the invoker's.

    Read off the RUNNING container and not off docker-compose.yml, because the running
    container is what an attacker meets. `CAP_NET_RAW` is in that sentence for a concrete
    reason: it is what lets a process craft raw packets, and raw sockets on a shared bridge
    are how a probe becomes a spoof.
    """
    problems = iso.hardening_violations(sealed_container)
    assert not problems, "MOS-SVC-050 violated:\n  " + "\n  ".join(problems)
    assert sealed_container.user.startswith("65534"), sealed_container.user


def test_the_sealed_container_holds_no_credential_and_no_forbidden_address(
    sealed_container: iso.ContainerObservation,
) -> None:
    """`MOS-SEC-087`, `MOS-SEC-005`, `MOS-SEC-025`, read from `docker inspect`.

    `MOS-SEC-025` names this exact surface as the reason the Job Token may not be an
    environment variable: env vars "appear in `/proc`, crash dumps and container inspection
    output". So the check is performed where the requirement says the leak would be.

    Stated as a whole-environment assertion rather than a denylist walk, because the
    interesting failure is a variable nobody thought to forbid.
    """
    assert set(sealed_container.env_names) <= {
        "PATH", "LANG", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
        "PYTHON_PIP_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256",
        "PYTHON_SETUPTOOLS_VERSION",
        "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "PYTHONFAULTHANDLER",
    }, (
        "the sealed container's environment has grown beyond the base image plus three "
        f"Python switches: {sorted(sealed_container.env_names)}. Every other service in "
        "docker-compose.yml carries a DSN, a bucket or a token; Z-SERVICE carries none."
    )


def test_the_sealed_container_is_on_the_sealed_network_and_nothing_else(
    sealed_container: iso.ContainerObservation,
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """The STRUCTURAL half of the proof, and it is what makes the probes below durable.

    A TCP probe says "this container cannot reach Postgres now". Membership says "and
    nothing added to the `medos` network tomorrow will be reachable either" -- which is the
    live question in this release, because Kafka arrives as `JobQueue` driver 2 and
    `MOS-SVC-064` forbids a service the broker. The route must already be absent when it
    lands, not removed afterwards.
    """
    problems = iso.isolation_violations(sealed_container, deployment)
    assert not problems, "Z-SERVICE membership is wrong:\n  " + "\n  ".join(problems)
    assert sealed_container.network_keys() == {"sealed"}


def test_no_broker_is_reachable_from_z_service(
    deployment: dict[str, iso.ContainerObservation],
    sealed_container: iso.ContainerObservation,
) -> None:
    """`MOS-SVC-064` and `MOS-SEC-096`, for a component that may not be deployed yet.

    "A service MUST NOT produce to or consume from the event bus." The broker is driver 2
    of `MOS-REL-023` and lands in this same release under someone else's migration. This
    test passes today by absence and keeps passing tomorrow by membership: whatever name
    the broker arrives under, it joins `medos` like the rest of `Z-DATA`, and `Z-SERVICE`
    is not on `medos`.
    """
    broker = next(d for d in iso.FORBIDDEN_DESTINATIONS if d.what == "the event bus")
    running = [
        name for name in broker.containers if deployment.get(name, _absent(name)).running
    ]
    for name in running:
        shared = deployment[name].network_keys() & sealed_container.network_keys()
        assert not shared, f"{name} shares {sorted(shared)} with the sealed container"
    assert "medos" not in sealed_container.network_keys(), (
        "the sealed container is on the `medos` network, where every Z-DATA component "
        "lives; a broker added there would be immediately reachable"
    )


def _absent(name: str) -> iso.ContainerObservation:
    return iso.ContainerObservation(name=name, running=False)


def test_the_membership_checker_reports_a_broken_deployment() -> None:
    """The checker's negative control, and it is the reason to trust the test above.

    `test_the_sealed_container_is_on_the_sealed_network_and_nothing_else` asserts an EMPTY
    list of violations, and an empty list is also what a checker that never finds anything
    returns. The honest way to demonstrate the difference is to break the deployment and
    watch the suite go red -- and deliberately attaching a live sealed container to the
    database network is not something a test run should do to a machine other people are
    working on.

    So the SAME checker is handed a synthetic observation of the broken deployment. It must
    name Postgres, MinIO, Triton and the PACS, by requirement, and it must also flag a
    container that is in neither table: `MOS-SEC-004` forbids a zone crossing that is not
    in the table, so "unclassified" is a violation rather than a default-allow.
    """
    broken = iso.ContainerObservation(
        name=SEALED_CONTAINER,
        networks={"medicalos-slice_sealed": "172.23.0.2",
                  "medicalos-slice_medos": "172.22.0.99"},
        user="65534:65534",
        read_only_root_filesystem=True,
        cap_drop=("ALL",),
    )
    # SYNTHETIC PEERS, not the live stack's. This used to start from the `deployment`
    # fixture, and the fixture reports a container that is not running as
    # `running=False` -- which the checker correctly ignores. So on a machine where
    # MinIO and Triton happened to be down, this negative control asserted `MOS-SVC-065`
    # against a checker that had never been shown an object store, and failed with a
    # message about the sealed service. A control that depends on what is up measures
    # the machine, not the checker.
    peers = {
        dest.containers[0]: iso.ContainerObservation(
            name=dest.containers[0],
            networks={"medicalos-slice_medos": f"172.22.0.{index + 10}"},
        )
        for index, dest in enumerate(iso.FORBIDDEN_DESTINATIONS)
    }
    peers["medos-web"] = iso.ContainerObservation(
        name="medos-web", networks={"medicalos-slice_medos": "172.22.0.50"}
    )
    problems = iso.isolation_violations(broken, peers)
    joined = "\n".join(problems)
    assert "MOS-SVC-063" in joined, joined      # Postgres
    assert "MOS-SVC-065" in joined, joined      # the object store
    assert "MOS-SEC-094" in joined, joined      # Triton
    assert "sealed" in joined and "medos" in joined
    assert "medos-web" in joined and "neither" in joined, (
        "a container in neither the allowed nor the forbidden table must be reported; "
        "MOS-SEC-004 closes the zone table and an unclassified crossing is not a pass"
    )


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("user", "root", "non-root"),
        ("user", "0:0", "non-root"),
        ("read_only_root_filesystem", False, "writable root filesystem"),
        ("privileged", True, "privileged"),
        ("cap_drop", (), "cap_drop"),
        ("cap_add", ("NET_RAW",), "CAP_NET_RAW"),
        ("published_ports", ("8080/tcp",), "publishes"),
        ("env_names", ("MEDOS_DATABASE_URL",), "MOS-SVC-063"),
        ("env_names", ("MEDOS_OBJECT_STORE_SECRET_KEY",), "MOS-SEC-087"),
        ("env_names", ("MEDOS_JOB_TOKEN",), "MOS-SEC-025"),
    ],
)
def test_the_hardening_checker_reports_each_MOS_SVC_050_failure(
    field: str, value: Any, expected: str
) -> None:
    """One parameter per clause of `MOS-SVC-050` and `MOS-SEC-025`, each broken alone.

    `test_the_sealed_container_is_hardened_as_MOS_SVC_050_requires` also asserts an empty
    list. This is what makes that assertion mean something: every clause, individually,
    produces a finding naming the requirement it broke.
    """
    healthy = {
        "name": SEALED_CONTAINER,
        "networks": {"medicalos-slice_sealed": "172.23.0.2"},
        "user": "65534:65534",
        "read_only_root_filesystem": True,
        "cap_drop": ("ALL",),
        "env_names": ("PATH", "PYTHONUNBUFFERED"),
    }
    assert not iso.hardening_violations(iso.ContainerObservation(**healthy))
    broken = iso.ContainerObservation(**{**healthy, field: value})
    problems = iso.hardening_violations(broken)
    assert any(expected in p for p in problems), (field, value, problems)


def test_a_tcp_connection_from_the_sealed_container_to_z_data_is_refused(
    sealed_container: iso.ContainerObservation,
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """CHAPTER 8 ACCEPTANCE CHECK 12, executed rather than described.

        "From a running `sealed` service container, TCP connections to the PACS port, the
         PostgreSQL port, the Triton port and the object-store port all fail to connect."

    Every forbidden destination in `medos/medos/sealed/isolation.py`, every IP address it holds,
    every port it listens on. The connection is opened with a raw socket from inside the
    vendor's container -- which is what `MOS-SEC-004` means by enforcement that is not
    "only ... configuration of the client", and what makes the four ownership rules
    `MOS-SVC-062` to `MOS-SVC-065` true rather than agreed.
    """
    probed: dict[str, str] = {}
    skipped: list[str] = []
    for destination in iso.FORBIDDEN_DESTINATIONS:
        for container in destination.containers:
            observation = deployment.get(container) or _absent(container)
            if not observation.running:
                skipped.append(container)
                continue
            for address in sorted(set(observation.networks.values())):
                if not address:
                    continue
                for port in destination.ports:
                    verdict = _tcp_probe_from(SEALED_CONTAINER, address, port)
                    if verdict is None:
                        continue
                    probed[f"{destination.zone} {container} {address}:{port}"] = verdict

    if not probed:
        skip_environment(
            "no forbidden destination could be probed from the sealed container, so "
            "MOS-SEC-004 cannot be established from inside the deployment",
            detail="no-probeable-destination",
        )

    reached = sorted(key for key, verdict in probed.items() if verdict == "reached")
    assert not reached, (
        "chapter 8 acceptance check 12 violated -- the sealed container opened a TCP "
        f"connection to {reached}. Z-SERVICE has egress to Z-GATEWAY and the platform "
        "callback only (MOS-SEC-029). Check the `networks:` membership of "
        "medos-sealed-service in medos/deploy/compose/docker-compose.yml."
    )
    # A THIN RESULT IS NOT THE SAME FAILURE AS A CROSSED BOUNDARY, and this assertion
    # used to report both with the same words. On a machine where MinIO, Triton and the
    # broker are simply not up, three pairs get probed, none is reached, and the suite
    # said "too thin a result to call the zone boundary established" -- which reads as a
    # verdict on the deployment rather than on the machine. The floor keeps its teeth
    # where it has them: a shortfall the absent containers do not account for is still a
    # failure, because that is a stack that IS up and still cannot be probed.
    if len(probed) < _PROBE_FLOOR and skipped:
        skip_environment(
            f"only {len(probed)} of the {_PROBE_FLOOR} destination/port pairs this check "
            f"wants could be probed, and {sorted(set(skipped))} are not running on this "
            "machine. Nothing that WAS probed was reached",
            detail="forbidden-destination-not-deployed",
        )
    assert len(probed) >= _PROBE_FLOOR, (
        f"only {len(probed)} destination/port pairs were probed ({sorted(probed)}); that is "
        f"too thin a result to call the zone boundary established. Containers that could "
        f"not be asked: {sorted(set(skipped))}"
    )
    assert sealed_container.running


def test_the_control_probes_prove_the_negatives_above_mean_something(
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """THE CONTROL, and it is not decoration.

    Every verdict in the test above is a NEGATIVE, and a negative from a probe that cannot
    work is indistinguishable from isolation. Two positives are therefore required:

      1. the sealed container CAN open a connection to the Gateway -- its one permitted
         egress (`MOS-SVC-025`, `MOS-SEC-029`). If this fails, the deployment is not
         isolated, it is broken, and no sealed job could run at all.
      2. `medos-worker` CAN open a connection to Postgres with the SAME probe. If this
         fails, the probe technique itself does not work on this machine and the negatives
         above established nothing.
    """
    gateway = deployment.get(GATEWAY_CONTAINER) or _absent(GATEWAY_CONTAINER)
    if not gateway.running:
        skip_infra(f"{GATEWAY_CONTAINER} is not running", dependency="medos-gateway")
    gateway_verdicts = {
        address: _tcp_probe_from(SEALED_CONTAINER, address, 8043)
        for address in sorted(set(gateway.networks.values())) if address
    }
    if "reached" not in gateway_verdicts.values():
        skip_infra(
            f"the sealed container could not reach the Gateway on any of its addresses "
            f"({gateway_verdicts}). Either medos-gateway is restarting or the deployment's "
            "one permitted egress is broken; in both cases the isolation asserted "
            "elsewhere in this file is unproven rather than proven.",
            dependency="medos-gateway",
        )
    # And only on the SEALED interface. The Gateway is on three networks and the sealed
    # container may reach it on exactly one of them -- which is a sharper statement of
    # MOS-SEC-029 than "the Gateway is reachable".
    assert sum(1 for v in gateway_verdicts.values() if v == "reached") == 1, gateway_verdicts

    postgres = deployment.get("medos-postgres") or _absent("medos-postgres")
    if not postgres.running:
        skip_infra("medos-postgres is not running", dependency="postgres")
    worker_verdicts = {
        address: _tcp_probe_from(INVOKER_CONTAINER, address, 5432)
        for address in sorted(set(postgres.networks.values())) if address
    }
    if "reached" not in worker_verdicts.values():
        skip_infra(
            f"medos-worker could not open a connection to Postgres ({worker_verdicts}) "
            "with the same probe that reported the sealed container isolated. The probe "
            "technique is not working on this machine, so nothing above is established.",
            dependency="postgres",
        )


def test_the_sealed_container_has_no_route_to_the_internet(
    sealed_container: iso.ContainerObservation,
) -> None:
    """Chapter 2 acceptance check 10, first half: "From the service pod, `curl
    https://example.com` fails".

    `MOS-SVC-025`: "`network.egress` MUST be a subset of `["dicom-gateway"]`. There is no
    manifest syntax for an arbitrary host." `MOS-SEC-029` says it as a container property:
    "egress deny-all except the Gateway address and the platform callback address." This is
    threat T3 -- "a compromised or malicious `sealed` service exfiltrates a study" -- and
    the vendor's container is the one place in this deployment where the code is not ours.

    THE CONTROL IS LOAD-BEARING HERE MORE THAN ANYWHERE. A laptop behind a captive portal
    or an air-gapped CI runner would pass this test for a reason that has nothing to do
    with the deployment, and would go on passing it if `internal: true` were deleted
    tomorrow. So the same probe runs from `medos-api`, which is on the ordinary `medos`
    network: if THAT cannot reach the internet either, this machine cannot distinguish
    isolation from having no uplink, and the test says so instead of passing.
    """
    # A well-known anycast resolver, by IP: no DNS, because a sealed container cannot
    # resolve anything anyway and a name lookup would make this a test of DNS.
    verdict = _tcp_probe_from(SEALED_CONTAINER, "1.1.1.1", 443)
    control = _tcp_probe_from("medos-api", "1.1.1.1", 443)
    if control != "reached":
        skip_environment(
            "medos-api cannot open an outbound connection either, so this machine cannot "
            "tell egress deny-all from having no uplink",
            detail="no-outbound-route-from-this-host",
        )
    assert verdict != "reached", (
        "the sealed container opened an outbound internet connection. MOS-SEC-029 requires "
        "egress deny-all except the Gateway and the platform callback; check that the "
        "`sealed` network in medos/deploy/compose/docker-compose.yml is still `internal: true`."
    )
    assert sealed_container.network_keys() == {"sealed"}


def test_the_host_has_no_route_to_the_sealed_service(
    sealed_container: iso.ContainerObservation,
) -> None:
    """The host is `Z-EDGE`, and the zone table grants `Z-SERVICE` ingress from
    `Z-PLATFORM` only.

    Two layers: the container publishes no port, and nothing on the host answers as the
    sealed service. The second matters because a `docker run -p` by hand, or a future
    `ports:` block added for debugging, would reopen the ingress without touching the
    compose service's own definition.
    """
    assert not sealed_container.published_ports, (
        f"{SEALED_CONTAINER} publishes {sorted(sealed_container.published_ports)}; a "
        "published port is an ingress from every browser on the machine"
    )
    port = int(os.environ.get("MEDOS_SEALED_PORT", "8080"))
    with contextlib.suppress(OSError):
        with socket.create_connection(("127.0.0.1", port), 2):
            pass
        # Something answers. Only a failure if it identifies itself as this service.
        import urllib.request

        with contextlib.suppress(Exception):
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/manifest",
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                served = json.loads(response.read())
            if (served.get("metadata") or {}).get("id") == "com.example.sealed-reference":
                pytest.fail(
                    f"the sealed service answered on 127.0.0.1:{port}. Z-SERVICE takes "
                    "ingress from Z-PLATFORM only (chapter 8 section 8.1.1)."
                )


def test_the_invoker_can_drive_the_sealed_container_over_the_sealed_network(
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """The positive half of the boundary: the platform CAN do its job through it.

    Run from inside `medos-worker` -- the sealed-service invoker of section 2.6.2 step 2 --
    because that is the only place the call is allowed to come from, and because a test
    that could make it from the host would be evidence the isolation had failed.

    Three rows of the ABI table, with stdlib only: liveness, readiness, and the manifest
    whose digest step 3 compares. If this passes while the probes above report every
    Z-DATA destination unreachable, the zone boundary is doing exactly what chapter 8
    describes: a permitted call through, everything else denied.
    """
    worker = deployment.get(INVOKER_CONTAINER) or _absent(INVOKER_CONTAINER)
    if not worker.running:
        skip_infra(f"{INVOKER_CONTAINER} is not running", dependency="medos-worker")
    script = (
        "import json, urllib.request\n"
        "base = 'http://medos-sealed-service:8080'\n"
        "out = {}\n"
        "for p in ('/healthz', '/readyz'):\n"
        "    out[p] = urllib.request.urlopen(base + p, timeout=5).status\n"
        "r = urllib.request.urlopen(urllib.request.Request(\n"
        "    base + '/v1/manifest', headers={'Accept': 'application/json'}), timeout=5)\n"
        "out['manifest'] = json.loads(r.read())['metadata']\n"
        "print('MEDOS_ABI ' + json.dumps(out))\n"
    )
    code, out = _docker_out("exec", INVOKER_CONTAINER, "python", "-c", script, timeout=90)
    if "MEDOS_ABI" not in out:
        if any(marker in out.lower() for marker in _UNPROBEABLE):
            skip_infra(f"{INVOKER_CONTAINER} could not be asked: {out.strip()[:160]}",
                       dependency="medos-worker")
        pytest.fail(
            f"the invoker could not drive the sealed service (exit {code}): "
            f"{out.strip()[:300]}"
        )
    payload = json.loads(out.split("MEDOS_ABI ", 1)[1].splitlines()[0])
    assert payload["/healthz"] == 200 and payload["/readyz"] == 200
    assert payload["manifest"]["id"] == "com.example.sealed-reference"
    assert payload["manifest"]["version"] == "0.3.0"


def test_the_running_image_serves_the_manifest_this_repository_registered(
    deployment: dict[str, iso.ContainerObservation], manifest: dict[str, Any]
) -> None:
    """Section 2.6.2 step 3 against the DEPLOYED container, not a fixture.

    The invoker computes `manifest_digest` from what the image serves and compares it with
    the registered value. Running it here closes the loop that the rest of part A opens on
    loopback: the bytes in `medos/deploy/sealed-reference/service.manifest.json` are the bytes
    inside `medicalos/sealed-reference:0.3.0`, and a rebuild that drifted would show up as
    a mismatch rather than as a surprise at dispatch.
    """
    worker = deployment.get(INVOKER_CONTAINER) or _absent(INVOKER_CONTAINER)
    if not worker.running:
        skip_infra(f"{INVOKER_CONTAINER} is not running", dependency="medos-worker")
    script = (
        "import json, urllib.request\n"
        "r = urllib.request.urlopen(urllib.request.Request(\n"
        "    'http://medos-sealed-service:8080/v1/manifest',\n"
        "    headers={'Accept': 'application/json'}), timeout=5)\n"
        "print('MEDOS_MANIFEST ' + json.dumps(json.loads(r.read())))\n"
    )
    code, out = _docker_out("exec", INVOKER_CONTAINER, "python", "-c", script, timeout=90)
    if "MEDOS_MANIFEST" not in out:
        skip_infra(f"could not read the manifest from the container: {out.strip()[:160]}",
                   dependency="medos-sealed-service")
    served = json.loads(out.split("MEDOS_MANIFEST ", 1)[1].splitlines()[0])
    assert manifest_digest(served) == manifest_digest(manifest), (
        "the running sealed image serves a manifest whose digest differs from the one in "
        "medos/deploy/sealed-reference/service.manifest.json. Rebuild the image "
        "(`docker compose build medos-sealed-service`) or explain the drift."
    )
    # `MOS-SVC-029`: digest-pinned, never a tag. Chapter 2's acceptance check 3.
    image_ref = served["execution"]["sealed"]["image"]
    assert "@sha256:" in image_ref and ":latest" not in image_ref, image_ref
    assert served["execution"]["mode"] == "sealed"
    assert served["network"]["egress"] == ["dicom-gateway"], (
        "MOS-SVC-025: `network.egress` MUST be a subset of ['dicom-gateway']. There is no "
        "manifest syntax for an arbitrary host."
    )


def _as_kwargs(config: SealedServiceConfig) -> dict[str, Any]:
    return {
        "base_url": config.base_url,
        "registered_manifest_digest": config.registered_manifest_digest,
        "expected_selftest_digest": config.expected_selftest_digest,
        "accept_ms": config.accept_ms,
        "analyze_ms": config.analyze_ms,
        "artifact_fetch_ms": config.artifact_fetch_ms,
        "heartbeat_interval_ms": config.heartbeat_interval_ms,
        "max_result_bundle_bytes": config.max_result_bundle_bytes,
        "max_artifact_bytes": config.max_artifact_bytes,
        "plaintext_deviation": config.plaintext_deviation,
    }
