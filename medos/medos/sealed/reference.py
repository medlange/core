# SPDX-License-Identifier: Apache-2.0
"""A reference SEALED service. The vendor's side of the boundary, not the platform's.

This is the program `medos/deploy/compose/Dockerfile.sealed` builds into its own image and
`medos-sealed-service` runs. It stands in for `com.pulmoai.chest-ct` of chapter 2 section
2.6.2: a third party's container, holding a third party's weights, which the platform
drives over `medicalos-service-abi/1.0` and otherwise cannot see into.

IT IMPORTS NOTHING FROM `medos`, AND THAT IS THE POINT

A sealed service that needed the platform's package would not be sealed -- chapter 2's
whole distinction between the modes is that in `native` the vendor imports the platform's
ABI wheel and runs inside the platform's process, and in `sealed` "the service brings its
own runtime" and the platform's only leverage is verification at the boundary
(`MOS-SVC-052`). So: stdlib only, its own image, its own JCS implementation.

That last one deserves its sentence. `_jcs` below duplicates, in fifteen lines, what
`medos/medos/core/canonical.py` does for the platform. Everywhere else in this repository that
would be a defect and `MOS-REL-032` would name it. Here it is the contract: `MOS-SVC-027`
requires a vendor to be able to compute the digest of its own manifest independently, and
the agreement between the two implementations is a property worth TESTING rather than
assuming -- `tests/integration/test_sealed_mode.py` asserts it, and a divergence would mean
every sealed vendor's self-declared digest is wrong.

WHAT IT REFUSES TO DO, ON PURPOSE

  * It never serves its weights. `/opt/vendor/weights.bin` is read at start-up, its digest
    goes into `diagnostics`, and no endpoint returns a byte of it. "Weights location:
    inside the vendor image; never surrendered" (section 2.5's mode table).
  * It reads pixels only through `gateway.dicomweb_base_url`, only with the Job Token it
    was handed in the request body (`MOS-SEC-025`), and only with `GET` (`MOS-SVC-047`).
  * It pushes nothing. Artifacts are served from
    `GET /v1/executions/{id}/artifacts/{artifact_id}` and pulled by the platform
    (`MOS-SVC-048`).
  * It logs an execution id and a phase. Never a UID, never a header, never a response
    body (`MOS-SVC-051`, `MOS-SEC-119`).

Spec: chapter 2 sections 2.3.1, 2.5.2, 2.6.2; `MOS-SVC-026`, `MOS-SVC-027`, `MOS-SVC-043`
to `MOS-SVC-051`, `MOS-SVC-072`, `MOS-SVC-073`, `MOS-SVC-117`; `MOS-SEC-025`,
`MOS-SEC-119`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SCHEMA_VERSION = "1.0.0"
SERVER_NAME = "medicalos-sealed-reference"

# The golden fixture of `selftest.fixture`, reduced to what a deterministic service needs:
# the instances it pretends to have read. Baked in, because `MOS-SVC-026` runs the
# self-test on the SHIPPED fixture and a self-test that reached the network would be
# testing the network.
SELFTEST_STUDY = "1.2.826.0.1.3680043.8.498.10000000000000000000000000000001"
SELFTEST_SERIES = "1.2.826.0.1.3680043.8.498.10000000000000000000000000000002"
SELFTEST_SOPS = tuple(
    f"1.2.826.0.1.3680043.8.498.1000000000000000000000000000000{i}" for i in range(3, 9)
)


# =====================================================================================
# 1. The vendor's own RFC 8785 canonicaliser. See the module docstring.
# =====================================================================================
def _jcs(value: Any) -> bytes:
    """RFC 8785. Three rules, and the third is the one that bites.

    Sorted keys and no insignificant whitespace are what
    `json.dumps(sort_keys=True, separators=(",", ":"))` already does. The third rule is
    ECMAScript `Number::toString`, under which an integral number prints WITHOUT a decimal
    point: `1.0` is `1`, and `score_threshold: 0.42` stays `0.42`. Python prints `1.0`.

    Skipping the normalisation is not a rounding detail. A manifest that declares
    `"count": 1` and a service that loads it into a float would hash to two different
    digests for one document, and `MOS-SVC-027` compares those digests across two
    independent implementations by construction -- the vendor's and the platform's. Every
    sealed registration would fail, and the first diagnosis anyone reached for would be
    "the registry is wrong".
    """
    return json.dumps(
        _jcs_numbers(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _jcs_numbers(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() and abs(value) < 2**53 else value
    if isinstance(value, dict):
        return {k: _jcs_numbers(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jcs_numbers(v) for v in value]
    raise TypeError(f"{type(value).__name__} has no canonical JSON form")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_of_manifest(manifest: dict[str, Any]) -> str:
    """`MOS-SVC-027`, computed by the vendor over its own document."""
    return _sha256(_jcs({k: v for k, v in manifest.items() if k != "content_digest"}))


def digest_of_selftest_bundle(bundle: dict[str, Any]) -> str:
    """`MOS-SVC-026`, minus `execution_id`, `job_id`, `diagnostics` -- and one more.

    `service.manifest_digest` is removed as well, and the reason is a cycle in the
    specification rather than a convenience: `MOS-SVC-026` puts this digest INSIDE the
    manifest, `MOS-SVC-027` digests the whole manifest, and `MOS-SVC-077` puts that digest
    inside every `ResultBundle` -- including this one. A vendor cannot compute either value
    first. `medos/medos/sealed/abi.py:selftest_digest` carries the full argument and the
    platform's half of this agreement; the two implementations are independent on purpose
    and `tests/integration/test_sealed_mode.py` asserts they produce the same string.
    """
    volatile = ("execution_id", "job_id", "diagnostics")
    reduced = {k: v for k, v in bundle.items() if k not in volatile}
    service = reduced.get("service")
    if isinstance(service, dict):
        reduced["service"] = {k: v for k, v in service.items() if k != "manifest_digest"}
    return _sha256(_jcs(reduced))


# =====================================================================================
# 2. The analysis. Deterministic, because `MOS-SVC-075` asks for reproducibility and
#    because `MOS-SVC-026`'s self-test digest is meaningless without it.
# =====================================================================================
class Weights:
    """The thing the vendor never surrenders.

    A real service would memory-map a multi-gigabyte checkpoint here. What matters for the
    boundary is identical either way: the bytes are read from inside the image, they
    influence the output, and no endpoint returns them.
    """

    def __init__(self, path: str | None) -> None:
        if path and os.path.exists(path):
            with open(path, "rb") as handle:
                self._blob = handle.read()
            self.source = "file"
        else:
            # A deterministic stand-in so the service runs outside its image (the test
            # suite drives it on the host). Declared as such in `diagnostics` rather than
            # silently pretending a checkpoint was loaded.
            self._blob = b"medicalos-sealed-reference-weights-v1"
            self.source = "builtin"
        self.digest = _sha256(self._blob)

    def score(self, seed: str) -> float:
        """A 'model'. Deterministic in the weights and the input, and in nothing else."""
        raw = hashlib.sha256(self.digest.encode() + b"|" + seed.encode()).digest()
        return round(int.from_bytes(raw[:4], "big") / 0xFFFFFFFF, 6)


def _nifti1_label_map(
    *,
    shape_zyx: tuple[int, int, int],
    spacing_zyx: tuple[float, float, float],
    origin_lps: tuple[float, float, float],
    direction_lps: tuple[float, ...],
    voxels: bytes,
) -> bytes:
    """A real gzip-compressed NIfTI-1, written with `struct`. `MOS-SVC-084`.

    The header is a fixed 348-byte record and this writes eleven of its fields; everything
    else is zero, which is what the format specifies for unused members. `mtime=0` in the
    gzip wrapper because gzip otherwise stamps the clock into the bytes and `MOS-SVC-075`'s
    reproducibility -- and `MOS-SVC-026`'s self-test digest -- would then change every
    second.

    LPS -> RAS on the last step, because `geometry` is LPS (`MOS-SVC-081`) and a NIfTI
    affine is RAS+. The platform reverses this in `medos/medos/sealed/nifti.py` and the two are
    written independently on purpose: a shared helper would make a sign error agree with
    itself.
    """
    nz, ny, nx = shape_zyx
    dz, dy, dx = spacing_zyx
    header = bytearray(348)
    struct.pack_into("<i", header, 0, 348)
    struct.pack_into("<8h", header, 40, 3, nx, ny, nz, 1, 1, 1, 1)
    struct.pack_into("<2h", header, 70, 2, 8)          # datatype uint8, bitpix 8
    struct.pack_into("<8f", header, 76, 1.0, dx, dy, dz, 0.0, 0.0, 0.0, 0.0)
    struct.pack_into("<f", header, 108, 352.0)         # vox_offset for a single-file .nii
    struct.pack_into("<f", header, 112, 1.0)           # scl_slope
    header[123] = 2 | 8                                # xyzt_units: mm + sec
    struct.pack_into("<2h", header, 252, 0, 1)         # qform_code 0, sform_code SCANNER
    cosines = [[direction_lps[r * 3 + c] for c in range(3)] for r in range(3)]
    step = (dx, dy, dz)
    for row in range(3):
        values = [cosines[row][col] * step[col] for col in range(3)] + [origin_lps[row]]
        if row < 2:  # LPS -> RAS
            values = [-v for v in values]
        struct.pack_into("<4f", header, 280 + row * 16, *values)
    header[344:348] = b"n+1\x00"
    return gzip.compress(bytes(header) + b"\x00" * 4 + voxels, compresslevel=9, mtime=0)


def _voxels(shape_zyx: tuple[int, int, int], seed: str) -> bytes:
    """A deterministic uint8 label array: `1` inside a seed-placed box, `0` elsewhere.

    `MOS-SVC-085`: label values are positive integers and `0` is background.
    """
    nz, ny, nx = shape_zyx
    raw = hashlib.sha256(seed.encode()).digest()
    y0, x0 = raw[0] % max(ny - 4, 1), raw[1] % max(nx - 4, 1)
    plane = bytearray(ny * nx)
    for y in range(y0, min(y0 + 4, ny)):
        for x in range(x0, min(x0 + 4, nx)):
            plane[y * nx + x] = 1
    return bytes(plane) * nz


class Execution:
    __slots__ = ("id", "job_id", "fingerprint", "state", "phase", "done", "total",
                 "bundle", "artifacts", "started_at", "cancelled", "error")

    def __init__(self, execution_id: str, job_id: str, fingerprint: str) -> None:
        self.id = execution_id
        self.job_id = job_id
        self.fingerprint = fingerprint
        self.state = "accepted"
        self.phase = "accepted"
        self.done = 0
        self.total = 5
        self.bundle: dict[str, Any] | None = None
        self.artifacts: dict[str, bytes] = {}
        self.started_at = time.time()
        self.cancelled = False
        self.error: dict[str, Any] | None = None


class ReferenceService:
    """The vendor's service. One instance per process; executions are kept in memory."""

    def __init__(self, manifest: dict[str, Any], weights: Weights) -> None:
        self.manifest = manifest
        self.manifest_digest = digest_of_manifest(manifest)
        self.weights = weights
        self.port = int(
            ((manifest.get("execution") or {}).get("sealed") or {}).get("port", 8080)
        )
        self._executions: dict[str, Execution] = {}
        self._lock = threading.Lock()
        self.ready = True

    # -- bundle construction ----------------------------------------------------------
    def _bundle(
        self,
        *,
        execution_id: str,
        job_id: str,
        study: str,
        series: str,
        sop_uids: tuple[str, ...],
        capabilities: tuple[str, ...],
        operating_points: dict[str, Any],
        gateway_outcome: str,
        artifacts: dict[str, bytes],
    ) -> dict[str, Any]:
        seed = "|".join(sorted(sop_uids))
        # `MOS-SVC-095`'s closed enum is the ONLY vocabulary a clinical rejection may use,
        # and `MOS-SVC-097` keeps transport failures out of it: a Gateway that did not
        # answer is a system failure reported as `state: "failed"` by `_analyse`, never as
        # a rejected bundle. What reaches here as a rejection is the one clinical case --
        # the Gateway answered and the selection is not there.
        rejected = gateway_outcome == "absent"
        shape = (max(len(sop_uids), 1), 16, 16)
        spacing = (1.0, 0.7, 0.7)
        origin = (-120.0, -160.0, -80.0)
        direction = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        geometry = {
            "study_instance_uid": study,
            "series_instance_uid": series,
            "voxel_order": "LPS",           # MOS-SVC-081
            "shape": list(shape),           # (nz, ny, nx)
            "spacing_mm": list(spacing),    # (dz, dy, dx)
            "origin_lps_mm": list(origin),
            "direction_lps": list(direction),
            "derived": False,
        }

        label_maps: list[dict[str, Any]] = []
        if not rejected:
            artifact_id = "lm_" + hashlib.sha256(seed.encode()).hexdigest()[:24]
            payload = _nifti1_label_map(
                shape_zyx=shape, spacing_zyx=spacing, origin_lps=origin,
                direction_lps=direction, voxels=_voxels(shape, seed),
            )
            artifacts[artifact_id] = payload
            label_maps.append({
                "artifact_id": artifact_id,
                # `MOS-SVC-121`: lowercase snake_case, unique in the bundle. Chapter 4
                # `MOS-IMG-066` sorts on `(role, artifact_id)` to derive `series_index`.
                "role": "lung_structures",
                "format": "nifti-gzip",
                # `MOS-SVC-087`: both declared, both verified by the platform on fetch.
                "sha256": _sha256(payload),
                "size_bytes": len(payload),
                "segments": [{
                    "label_value": 1,
                    "category": {"scheme": "SCT", "code": "123037004",
                                 "display": "Body structure",
                                 "scheme_uri": "http://snomed.info/sct"},
                    "type": {"scheme": "SCT", "code": "39607008",
                             "display": "Lung structure",
                             "scheme_uri": "http://snomed.info/sct"},
                    "algorithm_type": "AUTOMATIC",
                    "anatomic_region": {"scheme": "SCT", "code": "39607008",
                                        "display": "Lung structure",
                                        "scheme_uri": "http://snomed.info/sct"},
                }],
            })

        findings: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        for capability in capabilities:
            if rejected:
                # `MOS-SVC-011`: a capability that produced nothing is reported as a
                # per-capability rejection, "never as silence".
                outcomes.append({
                    "capability": capability,
                    "outcome": "rejected",
                    "reason_code": "no_eligible_series",
                })
                continue
            outcomes.append({"capability": capability, "outcome": "produced"})
            point = operating_points.get(capability)
            if point is None:
                # A deterministic capability: `MOS-SVC-089` requires it to carry neither
                # `score` nor `operating_point` and to state `present` explicitly.
                findings.append({
                    "capability": capability,
                    "concept": {"scheme": "SCT", "code": "68172002",
                                "display": "Emphysema of lung",
                                "scheme_uri": "http://snomed.info/sct"},
                    "finding_sites": [{"scheme": "SCT", "code": "39607008",
                                       "display": "Lung structure",
                                       "scheme_uri": "http://snomed.info/sct"}],
                    "present": True,
                    "source_series_instance_uid": series,
                    "source_sop_instance_uids": list(sop_uids),
                })
                continue
            score = self.weights.score(capability + "|" + seed)
            threshold = float(point.get("score_threshold", 0.5))
            findings.append({
                "capability": capability,
                "concept": {"scheme": "SCT", "code": "60046008",
                            "display": "Pleural effusion",
                            "scheme_uri": "http://snomed.info/sct"},
                "finding_sites": [{"scheme": "SCT", "code": "181128001",
                                   "display": "Pleural cavity structure",
                                   "scheme_uri": "http://snomed.info/sct"}],
                "score": score,
                # `MOS-SVC-021`: this member is named `score_threshold` on every surface it
                # crosses, and the execution request is one of the five the requirement
                # enumerates.
                "operating_point": {"id": str(point.get("id", "default")),
                                    "score_threshold": threshold},
                # `MOS-SVC-089`: present MUST equal score >= score_threshold. Computed, not
                # asserted, so the two can never disagree in this service's output.
                "present": score >= threshold,
                "source_series_instance_uid": series,
                "source_sop_instance_uids": list(sop_uids),
            })

        return {
            "schema_version": SCHEMA_VERSION,
            "service": {
                "id": (self.manifest.get("metadata") or {}).get("id"),
                "version": (self.manifest.get("metadata") or {}).get("version"),
                # `MOS-SVC-077`: MUST equal the registered digest of the dispatched
                # ServiceVersion. The vendor computes it over its own manifest.
                "manifest_digest": self.manifest_digest,
            },
            "execution_id": execution_id,
            "job_id": job_id,
            # `MOS-SVC-076`: rejected if and only if EVERY capability outcome is rejected.
            "status": "rejected" if rejected else "completed",
            "rejection": ({"reason_code": "no_eligible_series",
                           "observed": {"instances_retrieved": 0},
                           "allowed": {"min_instances": 1}} if rejected else None),
            "capability_outcomes": outcomes,
            "geometry": geometry,
            "label_maps": label_maps,
            "findings": findings,
            # Section 2.8.1 marks both required and possibly empty. Present and empty is a
            # statement; absent is a service that did not consider the question.
            "measurements": [],
            "key_images": [],
            # `MOS-SVC-073`: null in 0.1.0-0.3.0. Narrative is chapter 11's, and a service
            # that filled this in would be making a clinical claim in free text.
            "narrative": None,
            "diagnostics": {
                # `MOS-SVC-075`: declared, and true -- every number above is a function of
                # the weights and the dispatched UIDs and of nothing else.
                "deterministic": True,
                "weights_digest": self.weights.digest,
                "weights_source": self.weights.source,
                # An outcome CLASS, never a body: `MOS-SEC-119` treats everything a vendor
                # writes as potentially PHI-bearing.
                "gateway": gateway_outcome,
                "abi": "medicalos-service-abi/1.0",
            },
        }

    def selftest_bundle(self) -> dict[str, Any]:
        """`MOS-SVC-026`: the shipped golden fixture, never the network."""
        artifacts: dict[str, bytes] = {}
        return self._bundle(
            execution_id="exe_00000000000000000000000000",
            job_id="job_00000000000000000000000000",
            study=SELFTEST_STUDY,
            series=SELFTEST_SERIES,
            sop_uids=SELFTEST_SOPS,
            capabilities=("pleural_effusion",),
            operating_points={"pleural_effusion": {"id": "balanced",
                                                   "score_threshold": 0.42}},
            gateway_outcome="not_used",
            artifacts=artifacts,
        )

    # -- execution lifecycle -----------------------------------------------------------
    def start(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        execution_id = str(body.get("execution_id", ""))
        fingerprint = _sha256(
            _jcs({k: v for k, v in body.items() if k != "gateway"})
        )
        with self._lock:
            existing = self._executions.get(execution_id)
            if existing is not None:
                # `MOS-SVC-045`, both halves.
                if existing.fingerprint != fingerprint:
                    return 409, {"code": "execution_id_reused_with_different_body"}
                return 202, {"execution_id": execution_id}
            execution = Execution(execution_id, str(body.get("job_id", "")), fingerprint)
            self._executions[execution_id] = execution
        # `MOS-SVC-046`: accept inside `accept_ms` and do NOT analyse in the POST handler.
        thread = threading.Thread(
            target=self._analyse, args=(execution, body), daemon=True,
            name=f"analyse-{execution_id[-6:]}",
        )
        thread.start()
        return 202, {"execution_id": execution_id}

    def _analyse(self, execution: Execution, body: dict[str, Any]) -> None:
        try:
            gateway = body.get("gateway") or {}
            inputs = body.get("input") or {}
            roles = inputs.get("roles") or []
            series = str(roles[0].get("series_instance_uid")) if roles else ""
            sop_uids = tuple(
                uid for role in roles for uid in (role.get("sop_instance_uids") or ())
            )
            self._advance(execution, "fetching_metadata", 1)
            outcome = _read_through_gateway(
                base_url=str(gateway.get("dicomweb_base_url", "")),
                token=str(gateway.get("access_token", "")),
                allowed_methods=tuple(gateway.get("allowed_methods") or ()),
                study=str(inputs.get("study_instance_uid", "")),
                series=series,
            )
            if outcome in ("unreachable", "denied", "not_used"):
                # `MOS-SVC-097`: "A service MUST NOT signal a clinical rejection by
                # returning an HTTP error, raising an exception, or producing an empty
                # bundle. Transport and system failures are these, and only these:
                # ... gateway_error ...". A Gateway that refused the token or did not
                # answer is a system failure, and reporting it as "the study was
                # unsuitable" would tell a technologist to re-image a patient over a
                # platform fault.
                with self._lock:
                    execution.state = "failed"
                    execution.phase = "failed"
                    execution.error = {"code": "gateway_error", "detail": outcome}
                return
            for phase, step in (("preprocessing", 2), ("inference", 3),
                                ("postprocessing", 4)):
                if execution.cancelled:
                    return
                self._advance(execution, phase, step)
            artifacts: dict[str, bytes] = {}
            bundle = self._bundle(
                execution_id=execution.id,
                job_id=execution.job_id,
                study=str(inputs.get("study_instance_uid", "")),
                series=series,
                sop_uids=sop_uids,
                capabilities=tuple(body.get("capabilities_requested") or ()),
                operating_points=dict(body.get("operating_points") or {}),
                gateway_outcome=outcome,
                artifacts=artifacts,
            )
            with self._lock:
                if execution.cancelled:
                    return
                execution.artifacts = artifacts
                execution.bundle = bundle
                execution.phase = "done"
                execution.done = 5
                execution.state = "succeeded"
        except Exception as exc:  # noqa: BLE001 - a vendor crash is `failed`, not a 500
            with self._lock:
                execution.state = "failed"
                execution.phase = "failed"
                execution.error = {"code": "internal_error", "class": type(exc).__name__}

    def _advance(self, execution: Execution, phase: str, step: int) -> None:
        with self._lock:
            execution.state = "running"
            execution.phase = phase
            execution.done = step

    def poll(self, execution_id: str) -> dict[str, Any] | None:
        with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                return None
            body = {
                "schema_version": SCHEMA_VERSION,
                "execution_id": execution.id,
                "state": execution.state,
                "phase": execution.phase,
                "steps_completed": execution.done,
                "steps_total": execution.total,
                "started_at": _iso(execution.started_at),
                "result_bundle": execution.bundle if execution.state == "succeeded" else None,
                "error": execution.error,
            }
            if execution.state in ("succeeded", "failed"):
                body["finished_at"] = _iso(time.time())
            return body

    def artifact(self, execution_id: str, artifact_id: str) -> bytes | None:
        with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                return None
            return execution.artifacts.get(artifact_id)

    def release(self, execution_id: str) -> bool:
        with self._lock:
            return self._executions.pop(execution_id, None) is not None

    def cancel(self, execution_id: str) -> bool:
        with self._lock:
            execution = self._executions.get(execution_id)
            if execution is None:
                return False
            execution.cancelled = True
            execution.state = "failed"
            execution.phase = "cancelled"
            execution.error = {"code": "cancelled"}
            return True


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _read_through_gateway(
    *, base_url: str, token: str, allowed_methods: tuple[str, ...], study: str, series: str
) -> str:
    """The ONE outbound call a sealed service makes. Returns an outcome CLASS, not data.

    `MOS-SVC-047`: the token is used only against `gateway.dicomweb_base_url`, is not
    persisted, is not logged, and no method outside `allowed_methods` is issued. The
    refusal below is the service enforcing that on itself -- the Gateway enforces it again
    (`MOS-SEC-026`), and both are supposed to.

    The return value is one of `reached` / `denied` / `absent` / `unreachable` /
    `not_used`. It goes into `diagnostics`, where `MOS-SEC-119` applies to everything a
    vendor writes: a status class carries no PHI, a response body would.
    """
    if not base_url or not token:
        return "not_used"
    if "GET" not in {m.upper() for m in allowed_methods}:
        return "not_used"
    url = f"{base_url.rstrip('/')}/studies/{study}/series/{series}/metadata"
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/dicom+json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read(1_048_576)
            return "reached"
    except urllib.error.HTTPError as exc:
        # A status means a TCP connection was established and the Gateway answered. 403/404
        # is a policy outcome, not an isolation failure, and the distinction is exactly what
        # the deployment test reads.
        return "denied" if exc.code in (401, 403) else "absent"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "unreachable"


# =====================================================================================
# 3. HTTP. Exactly the nine rows of section 2.5.2 and nothing else.
# =====================================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = SERVER_NAME
    sys_version = ""
    service: ReferenceService  # set on the server instance

    # `MOS-SVC-051` / `MOS-SEC-119`: the access log of BaseHTTPRequestHandler prints the
    # request line, which carries `execution_id` and -- for the artifact route -- an
    # artifact id derived from SOP Instance UIDs. Silenced; the service logs phases only.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # -- helpers ---------------------------------------------------------------------
    def _send(self, status: int, payload: bytes = b"", content_type: str | None = None) -> None:
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        # RFC 9110: a 204 carries no body and no Content-Length. The ABI's DELETE row is
        # the only 204 here, and `MOS-SVC-049` has the platform send it after every
        # artifact is verified -- a framing error on that response would strand the
        # invoker with the execution still held.
        if status != 204:
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload and status != 204:
            self.wfile.write(payload)

    def _json(self, status: int, body: Any) -> None:
        self._send(status, json.dumps(body).encode("utf-8"), "application/json")

    def _not_found(self) -> None:
        # Section 2.5.2: "There are no others". A path outside the table is 404 with no
        # hint of what else might exist; a sealed service exposes no administrative
        # surface and no discovery surface either.
        self._json(404, {"code": "no_such_endpoint"})

    def _segments(self) -> list[str]:
        return [s for s in self.path.split("?", 1)[0].split("/") if s]

    # -- routes ----------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        service = self.server.service  # type: ignore[attr-defined]
        segments = self._segments()
        if segments == ["healthz"]:
            return self._send(200)
        if segments == ["readyz"]:
            return self._send(200 if service.ready else 503)
        if segments == ["v1", "manifest"]:
            return self._manifest(service)
        if len(segments) == 3 and segments[:2] == ["v1", "executions"]:
            body = service.poll(segments[2])
            return (
                self._json(200, body)
                if body
                else self._json(404, {"code": "no_such_execution"})
            )
        if (len(segments) == 5 and segments[:2] == ["v1", "executions"]
                and segments[3] == "artifacts"):
            payload = service.artifact(segments[2], segments[4])
            if payload is None:
                return self._json(404, {"code": "no_such_artifact"})
            return self._send(200, payload, "application/octet-stream")
        return self._not_found()

    def _manifest(self, service: ReferenceService) -> None:
        """`200 application/yaml`, or JSON when the platform asks for it.

        The section 2.5.2 table names `application/yaml` and that is the default. The
        platform's digest (`MOS-SVC-027`) is defined over the JSON conversion, so a
        platform with no YAML reader asks for JSON on this same path -- content
        negotiation, not a tenth endpoint. Serving both from one document is what keeps
        them the same document.
        """
        accept = (self.headers.get("Accept") or "").lower()
        if "application/json" in accept:
            payload = json.dumps(service.manifest).encode("utf-8")
            return self._send(200, payload, "application/json")
        return self._send(200, _as_yaml(service.manifest).encode("utf-8"), "application/yaml")

    def do_POST(self) -> None:  # noqa: N802
        service = self.server.service  # type: ignore[attr-defined]
        segments = self._segments()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if segments == ["v1", "selftest"]:
            return self._json(200, service.selftest_bundle())
        if segments == ["v1", "executions"]:
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return self._json(400, {"code": "malformed_body"})
            if not isinstance(body, dict) or not body.get("execution_id"):
                return self._json(400, {"code": "malformed_body"})
            status, payload = service.start(body)
            return self._json(status, payload)
        if (len(segments) == 4 and segments[:2] == ["v1", "executions"]
                and segments[3] == "cancel"):
            # `MOS-SVC-117`: 501 before 0.3.0, implementable from 0.3.0. This is 0.3.0.
            ok = service.cancel(segments[2])
            return self._json(202, {"execution_id": segments[2]}) if ok else self._json(
                404, {"code": "no_such_execution"})
        return self._not_found()

    def do_DELETE(self) -> None:  # noqa: N802
        service = self.server.service  # type: ignore[attr-defined]
        segments = self._segments()
        if len(segments) == 3 and segments[:2] == ["v1", "executions"]:
            if service.release(segments[2]):
                return self._send(204)
            return self._json(404, {"code": "no_such_execution"})
        return self._not_found()

    # Every other method is outside the table.
    def do_PUT(self) -> None:  # noqa: N802
        self._not_found()

    def do_PATCH(self) -> None:  # noqa: N802
        self._not_found()


def _as_yaml(value: Any, indent: int = 0) -> str:
    """A YAML EMITTER, not a parser. Fifteen lines, and nothing reads it back.

    The section 2.5.2 table says `GET /v1/manifest` answers `application/yaml`, so the
    reference service answers `application/yaml`. Emitting the subset this manifest uses
    (maps, lists, strings, numbers, booleans, null) is bounded and checkable; PARSING YAML
    is not, which is why the platform side asks for JSON instead of growing a reader.
    """
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return pad + "{}\n"
        out = ""
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                out += f"{pad}{key}:\n" + _as_yaml(item, indent + 1)
            else:
                out += f"{pad}{key}: {_scalar(item)}\n"
        return out
    if isinstance(value, list):
        out = ""
        for item in value:
            if isinstance(item, (dict, list)) and item:
                nested = _as_yaml(item, indent + 1)
                first, _, rest = nested.partition("\n")
                out += f"{pad}- {first.strip()}\n" + (rest if rest.strip() else "")
            else:
                out += f"{pad}- {_scalar(item)}\n"
        return out
    return pad + _scalar(value) + "\n"


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value))


# =====================================================================================
# 4. Entry point
# =====================================================================================
def build_server(
    manifest: dict[str, Any], *, weights_path: str | None = None, port: int = 0
) -> tuple[ThreadingHTTPServer, ReferenceService]:
    service = ReferenceService(manifest, Weights(weights_path))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - see below
    # 0.0.0.0 and not 127.0.0.1: the container's only reachable interface is the one the
    # invoker connects to over the `sealed` network, and binding loopback would make the
    # service unreachable from its own invoker. What keeps this from being an exposure is
    # the network the container is on (`medos/medos/sealed/isolation.py`), not the bind address:
    # a container published to the host would be reachable on a loopback bind too.
    server.service = service  # type: ignore[attr-defined]
    return server, service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MedicalOS reference sealed service")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--weights", default="/opt/vendor/weights.bin")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--print-digests", action="store_true",
        help="print manifest_digest and the self-test bundle digest, then exit",
    )
    args = parser.parse_args(argv)

    with open(args.manifest, "rb") as handle:
        manifest = json.loads(handle.read().decode("utf-8"))

    if args.print_digests:
        # No socket is opened on this path: it is a BUILD step. The vendor runs it to fill
        # `selftest.expected_result_bundle_digest` into the manifest it then signs.
        service = ReferenceService(manifest, Weights(args.weights))
        print(json.dumps({
            "manifest_digest": service.manifest_digest,
            "selftest_digest": digest_of_selftest_bundle(service.selftest_bundle()),
        }, indent=2))
        return 0

    port = args.port or int(
        ((manifest.get("execution") or {}).get("sealed") or {}).get("port", 8080)
    )
    server, service = build_server(manifest, weights_path=args.weights, port=port)
    print(f"{SERVER_NAME} listening on 0.0.0.0:{port} "
          f"manifest_digest={service.manifest_digest}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
