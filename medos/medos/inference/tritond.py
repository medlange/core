# SPDX-License-Identifier: Apache-2.0
"""`medicalos-tritond` -- the administrator of the single shared Triton.

WHAT CHAPTER 13 §13.10 ASKS OF THIS PROCESS, AND WHERE EACH PIECE LIVES
    "In `native` mode the platform loads the declared model onto shared Triton; the
    service container is Triton's *client* and the platform is Triton's *administrator*.
    `medicalos-tritond` is the only holder of Triton's model-control credential."

      * model repository layout, `config.pbtxt`, `medicalos.json`  -> `repository.py`
      * the budget, reservations, eviction rotation                -> `residency.py`
      * fetching weights, registration, pinning, readiness, health -> THIS FILE
      * inference                                                  -> `triton.py`

    This is the only module in the codebase that constructs a `TritonAdminClient`.  A
    worker gets a `TritonBackend`, which has no load/unload method at all, so "tritond is
    the only holder" is a property of the type system rather than of a code review.

WEIGHTS COME FROM THE OBJECT STORE. ALWAYS.
    Chapter 2 `MOS-SVC-058`: "Neither mode may bake model weights into a runner image
    (native) nor mutate them at runtime (both)."  So the model repository Triton reads is
    an EMPTY volume at boot, and `stage()` fills it from
    `models/{model_id}/{version}/...` in the object store, verifying the sha256 recorded
    in `medicalos.json` against the bytes actually retrieved (`MOS-OPS-070`: the manifest
    "MUST be covered by the same signature as the weights").  A digest that is only
    computed at publishing time proves nothing about what was served; this one is computed
    on the bytes that land on the disk Triton reads.

    There is no code path that writes into the model repository from anywhere else, and
    none that mutates a staged artifact.  `docker compose` mounts the repository volume
    read-write to `tritond` and READ-ONLY to Triton.

VERSION PINNING IS RESOLUTION BY ID **AND** VERSION
    `MEDICALOS_MODEL_PINS` is a JSON array of explicit `(model_id, model_version,
    residency)` triples.  There is no "latest", no floating minor and no default.
    §15.2.9: "Loading by id and version means the registry contract is genuinely exercised
    from the first slice instead of being asserted."  Two versions of one model pin
    simultaneously and are resident simultaneously -- `MOS-OPS-068` derives a distinct
    Triton model name for each -- which is what canary and blue/green need from chapter 6
    and what makes the pin observable rather than notional.

    This is NOT chapter 6's registry: there is no `ModelVersion` table, no `Deployment`
    row and no resolution function here.  The pins are deployment configuration, which is
    the honest weeks 3-5 shape (`CONTRACT.md` §0 removes registries from the slice, and
    `docs/spec/15-delivery.md` §15.2.6 schedules capability resolution at 0.3.0).  When
    chapter 6 lands, `load_pins` is the function that reads a table instead of an
    environment variable and nothing else in this file changes.

THE STARTUP SELF-TEST (MOS-OPS-015 / MOS-OPS-075)
    `MOS-OPS-075` is explicit that Triton's `model_warmup` "is a latency measure only; it
    does not replace the MOS-IMG preprocessing self-test, which compares an output tensor
    hash against the training-time value and is run by the serving component at startup".
    `selftest()` is that check: the golden input shipped in the artifact is sent through
    the real server and the SHA-256 of the returned tensor is compared against the value
    recorded in `medicalos.json`.  A mismatch fails readiness -- it means the served model
    is not the model that was evaluated, and every result it produces would carry a
    provenance claim that is false.

Spec: MOS-OPS-008 (Triton replaceability is NOT settled -- OQ-10), MOS-OPS-015,
MOS-OPS-068, MOS-OPS-069, MOS-OPS-070, MOS-OPS-071, MOS-OPS-074, MOS-OPS-075,
MOS-OPS-079, MOS-OPS-081, MOS-OPS-082, MOS-OPS-083, MOS-OPS-085, MOS-OPS-086,
MOS-OPS-091, MOS-OPS-113 (the `/internal/v1` boundary), MOS-SVC-058, MOS-REL-023.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from medos.inference.backend import ModelRef
from medos.inference.repository import (
    ModelManifest,
    ModelRepositoryError,
    artifact_key,
    check_built_for,
    manifest_key,
    triton_model_name,
    warmup_key,
    write_model_directory,
)
from medos.inference.residency import (
    ModelSlot,
    ResidencyLedger,
    ResidencySettings,
    Unsatisfiable,
)
from medos.inference.triton import TritonAdminClient, TritonBackend, TritonConfig
from medos.objectstore import ObjectNotFound, S3Client, S3Config

__all__ = [
    "ModelPin",
    "TritondConfig",
    "Tritond",
    "load_pins",
    "measure_device_memory",
    "create_app",
    "main",
]

LOGGER = logging.getLogger("medos.tritond")

# MOS-OPS-092: a model whose p95 load duration exceeds this MUST be `resident` or the
# Deployment is refused.  Enforced in `residency.ResidencyLedger._load_locked`.
SLOW_LOAD_SECONDS = 30.0


@dataclass(frozen=True)
class ModelPin:
    """One `(model_id, model_version)` this node is configured to serve, and how.

    `residency` is chapter 6's `Deployment.residency` column (`MOS-REG-076a`); its three
    values and their semantics are `MOS-OPS-114`.  `service_ids` is what `MOS-OPS-081`'s
    `admissible_service_ids` is computed from -- the inboxes a worker may claim from once
    this model is loaded.
    """

    ref: ModelRef
    residency: str = "on_demand"
    patch_batch_size: int = 1
    service_ids: tuple[str, ...] = ()

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> ModelPin:
        return ModelPin(
            ref=ModelRef(
                model_id=str(raw["model_id"]), model_version=str(raw["model_version"])
            ),
            residency=str(raw.get("residency", "on_demand")),
            patch_batch_size=int(raw.get("patch_batch_size", 1)),
            service_ids=tuple(str(s) for s in raw.get("service_ids", ())),
        )


def load_pins(raw: str | None = None) -> tuple[ModelPin, ...]:
    """Parse `MEDICALOS_MODEL_PINS`. Empty is legal; a malformed pin is not.

    An unparseable pin raises at startup rather than being skipped: a tritond that comes
    up healthy while silently serving one fewer model than the operator configured is the
    failure mode where a job waits forever for a slot that was never going to exist.
    """
    text = raw if raw is not None else os.environ.get("MEDICALOS_MODEL_PINS", "[]")
    try:
        parsed = json.loads(text or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"MEDICALOS_MODEL_PINS is not valid JSON: {exc}. Expected a list of "
            '{"model_id": ..., "model_version": ..., "residency": ...} objects'
        ) from exc
    if not isinstance(parsed, list):
        raise ValueError("MEDICALOS_MODEL_PINS must be a JSON array")
    return tuple(ModelPin.from_dict(item) for item in parsed)


def measure_device_memory() -> tuple[int, int, str]:
    """Return `(total_bytes, runtime_reserved_bytes, device)`. MEASURED, per MOS-OPS-079.

    `MOS-OPS-079`: the reserved figure "MUST be **measured** at `tritond` startup (free
    memory before any model load, subtracted from total), not estimated".  On a GPU node
    that is `nvidia-smi`.  There is no GPU here, so the same measurement is taken against
    host memory and the returned `device` is `"cpu"` so that nothing downstream can read
    this as a GPU accounting.  The measurement, not the constant, is the part of
    MOS-OPS-079 that matters: the chart's 1.2 GiB is explicitly "a starting value only".

    Falls back to `MEDICALOS_DEVICE_TOTAL_BYTES` / `MEDICALOS_GPU_RUNTIME_RESERVED_BYTES`
    where `/proc/meminfo` does not exist (Windows developer machines), because a refusal
    to start on a developer laptop buys nothing.
    """
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        values: dict[str, int] = {}
        for line in meminfo.read_text().splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if parts:
                values[key.strip()] = int(parts[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", values.get("MemFree", 0))
        if total > 0 and available > 0:
            return total, max(0, total - available), "cpu"
    total = int(os.environ.get("MEDICALOS_DEVICE_TOTAL_BYTES", str(8 * 1024**3)))
    reserved = int(
        os.environ.get("MEDICALOS_GPU_RUNTIME_RESERVED_BYTES", str(1_288_490_188))
    )
    return total, reserved, "cpu"


@dataclass(frozen=True)
class TritondConfig:
    model_root: Path
    http_host: str = "0.0.0.0"
    http_port: int = 8500
    node_id: str = "node-01"
    load_timeout_s: float = 180.0

    @staticmethod
    def from_env() -> TritondConfig:
        addr = os.environ.get("MEDICALOS_HTTP_ADDR", "0.0.0.0:8500")
        host, _, port = addr.rpartition(":")
        return TritondConfig(
            model_root=Path(os.environ.get("MEDICALOS_MODEL_REPOSITORY", "/models")),
            http_host=host or "0.0.0.0",
            http_port=int(port or "8500"),
            node_id=os.environ.get("MEDICALOS_NODE_ID", "node-01"),
            load_timeout_s=float(os.environ.get("MEDICALOS_MODEL_LOAD_TIMEOUT_S", "180")),
        )


class Tritond:
    """Stage, register, pin, load, self-test, and answer for readiness.

    Constructed with its collaborators rather than importing them (CONTRACT.md §11: "no
    global mutable state"), so a test can drive the whole reconcile path against a real
    Triton and a real MinIO, or against fakes, without touching this class.
    """

    def __init__(
        self,
        *,
        config: TritondConfig,
        s3: S3Client,
        admin: TritonAdminClient,
        backend: TritonBackend,
        pins: tuple[ModelPin, ...],
        settings: ResidencySettings | None = None,
    ) -> None:
        self.config = config
        self.s3 = s3
        self.admin = admin
        self.backend = backend
        self.pins = pins
        total, reserved, device = measure_device_memory()
        self.settings = settings or ResidencySettings(
            runtime_reserved_bytes=reserved,
            device_total_bytes=total,
            headroom_ratio=float(os.environ.get("MEDICALOS_GPU_HEADROOM_RATIO", "0.90")),
            min_hold_seconds=int(
                os.environ.get("MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS", "300")
            ),
            tick_seconds=float(os.environ.get("MEDICALOS_RESIDENCY_TICK_SECONDS", "2")),
            device=device,
        )
        self.ledger = ResidencyLedger(
            self.settings,
            load=self._load_model,
            unload=self._unload_model,
            node_id=config.node_id,
            device_uuid=None,  # CPU node: present and null, never omitted. See state().
        )
        self.manifests: dict[tuple[str, str], ModelManifest] = {}
        self.selftests: dict[tuple[str, str], dict[str, Any]] = {}
        self.load_durations: dict[tuple[str, str], float] = {}
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._ticker: threading.Thread | None = None

    # -- staging ------------------------------------------------------------------
    def stage(self, ref: ModelRef) -> ModelManifest:
        """Fetch one ModelVersion from the object store into the model repository.

        Four steps, in this order, because each one makes the next meaningful:
          1. read `medicalos.json` and parse it (`MOS-OPS-070` -- an incomplete manifest
             is a packaging defect, refused here rather than at inference time);
          2. read the artifact and VERIFY its sha256 against the manifest;
          3. apply the backend-compatibility gate (`check_built_for`);
          4. write §13.10.1's directory, which generates and then re-validates a
             `config.pbtxt` against `MOS-OPS-071/072/073/074`.
        """
        try:
            manifest = ModelManifest.from_json(self.s3.get_object(manifest_key(ref)))
        except ObjectNotFound as exc:
            raise ModelRepositoryError(
                f"{ref} is pinned but not published: no {manifest_key(ref)} in the "
                "artifact store. Weights are never baked into an image (MOS-SVC-058), "
                "so an unpublished pin is an unrunnable deployment."
            ) from exc
        if (manifest.model_id, manifest.model_version) != (ref.model_id, ref.model_version):
            raise ModelRepositoryError(
                f"{manifest_key(ref)} declares "
                f"{manifest.model_id}@{manifest.model_version}; the key says {ref}. "
                "A manifest that disagrees with its own location means a published "
                "artifact could serve under another version's identity."
            )
        blob = self.s3.get_object(artifact_key(ref, manifest.artifact_filename))
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        if digest != manifest.artifact_digest:
            raise ModelRepositoryError(
                f"MOS-OPS-070: {ref} artifact digest mismatch. manifest="
                f"{manifest.artifact_digest} retrieved={digest}. Refusing to serve "
                "weights that are not the weights that were evaluated."
            )
        warmup_bytes: bytes | None = None
        warmup_name = str(manifest.selftest.get("input_filename", "")) or None
        if warmup_name:
            try:
                warmup_bytes = self.s3.get_object(warmup_key(ref, warmup_name))
            except ObjectNotFound:
                warmup_bytes = None
        try:
            server = self.admin.server_metadata()
        except Exception:  # noqa: BLE001 - staging must work before Triton is reachable
            server = {}
        check_built_for(manifest, server)
        write_model_directory(
            self.config.model_root,
            manifest,
            blob,
            warmup_bytes=warmup_bytes,
            warmup_filename=warmup_name or "golden_input.fp32.bin",
            warmup_batch=int(manifest.selftest.get("batch", 1)) if warmup_bytes else None,
        )
        with self._lock:
            self.manifests[(ref.model_id, ref.model_version)] = manifest
        LOGGER.info(
            json.dumps(
                {
                    "event": "model_staged",
                    "model_id": ref.model_id,
                    "model_version": ref.model_version,
                    "triton_model_name": manifest.triton_model_name,
                    "artifact_digest": manifest.artifact_digest,
                    "weights_bytes": manifest.weights_bytes,
                }
            )
        )
        return manifest

    # -- load / unload, injected into the ledger ----------------------------------
    def _load_model(self, ref: ModelRef) -> float:
        name = triton_model_name(ref.model_id, ref.model_version)
        duration = self.admin.load(name, ready_timeout_s=self.config.load_timeout_s)
        self.load_durations[(ref.model_id, ref.model_version)] = duration
        LOGGER.info(
            json.dumps(
                {
                    "event": "model_load",
                    "model_id": ref.model_id,
                    "model_version": ref.model_version,
                    # MOS-OPS-091: from the load call to model-ready, including warm-up.
                    "medicalos_model_load_duration_seconds": round(duration, 4),
                    "outcome": "loaded",
                    "slow_load": duration > SLOW_LOAD_SECONDS,
                }
            )
        )
        return duration

    def _unload_model(self, ref: ModelRef) -> None:
        name = triton_model_name(ref.model_id, ref.model_version)
        self.admin.unload(name)
        LOGGER.info(
            json.dumps(
                {
                    "event": "model_load",
                    "model_id": ref.model_id,
                    "model_version": ref.model_version,
                    "outcome": "evicted",
                }
            )
        )

    # -- reconcile ----------------------------------------------------------------
    def reconcile(self) -> dict[str, Any]:
        """Bring the node to its pinned configuration. Idempotent; safe to re-run.

        `resident`-class pins are loaded here -- §13.10.3: "loaded at `tritond` startup".
        `on_demand` and `evictable` pins are staged and registered but NOT loaded: they
        load on first reservation, which is the whole point of the class.
        """
        self.admin.assert_server_flags()
        report: dict[str, Any] = {"staged": [], "loaded": [], "selftests": []}
        for pin in self.pins:
            manifest = self.stage(pin.ref)
            slot = ModelSlot(
                ref=pin.ref,
                residency=pin.residency,  # type: ignore[arg-type]
                footprint_bytes=manifest.footprint_bytes(pin.patch_batch_size),
                service_ids=pin.service_ids,
            )
            self.ledger.register(slot)
            report["staged"].append(str(pin.ref))
            if pin.residency == "resident":
                self.ledger._load_locked(slot)  # noqa: SLF001 - startup load, §13.10.3
                report["loaded"].append(str(pin.ref))
                report["selftests"].append(self.selftest(pin.ref))
        return report

    # -- MOS-OPS-015 self-test ----------------------------------------------------
    def selftest(self, ref: ModelRef) -> dict[str, Any]:
        """Run the golden fixture through the REAL server and compare the output hash.

        `MOS-OPS-075` puts this obligation on "the serving component at startup" and is
        explicit that Triton's own `model_warmup` does not discharge it: warm-up proves
        the model runs, this proves it computes what it computed at training time.  A
        mismatch is a hard readiness failure, because every result such a model produced
        would carry provenance naming an evaluation that does not describe it.
        """
        manifest = self.manifests.get((ref.model_id, ref.model_version))
        if manifest is None or not manifest.selftest:
            return {"model": str(ref), "state": "skipped", "reason": "no_selftest_declared"}
        spec = manifest.selftest
        blob = self.s3.get_object(warmup_key(ref, str(spec["input_filename"])))
        shape = tuple(int(d) for d in spec["input_shape"])
        tensor = np.frombuffer(blob, dtype=np.float32).reshape(shape)
        output = self.backend.infer(
            tensor,
            ref.model_id,
            ref.model_version,
            input_name=manifest.input_name,
            output_name=manifest.output_name,
        )
        digest = "sha256:" + hashlib.sha256(
            np.ascontiguousarray(output, dtype=np.float32).tobytes()
        ).hexdigest()
        expected = str(spec["output_sha256"])
        result = {
            "model": str(ref),
            "state": "passed" if digest == expected else "failed",
            "expected_output_sha256": expected,
            "observed_output_sha256": digest,
            "preprocessing_version": manifest.preprocessing_version,
        }
        with self._lock:
            self.selftests[(ref.model_id, ref.model_version)] = result
        if digest != expected:
            LOGGER.error(json.dumps({"event": "model_selftest_failed", **result}))
        return result

    # -- health / readiness -------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Liveness: this process answers. Deliberately does NOT touch Triton.

        Same reasoning `medos.api.app` records for `/healthz`: a liveness probe that fails
        when a dependency is down asks the orchestrator to restart a process that is
        working.
        """
        return {"status": "ok", "uptime_seconds": round(time.time() - self.started_at, 1)}

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        """Ready when Triton is live, every `resident` pin is loaded, and self-tests pass."""
        triton_live = self.admin.is_live()
        pinned_resident = [p for p in self.pins if p.residency == "resident"]
        not_loaded = [
            str(p.ref)
            for p in pinned_resident
            if not (self.ledger.slot(p.ref) and self.ledger.slot(p.ref).loaded)  # type: ignore[union-attr]
        ]
        failed = [
            key for key, value in self.selftests.items() if value.get("state") == "failed"
        ]
        ready = triton_live and not not_loaded and not failed
        return ready, {
            "ready": ready,
            "triton_live": triton_live,
            "resident_not_loaded": not_loaded,
            "selftests_failed": [f"{a}@{b}" for a, b in failed],
            "device": self.settings.device,
            "budget_bytes": self.settings.budget_bytes,
        }

    # -- resolution ---------------------------------------------------------------
    def resolve(self, model_id: str, model_version: str) -> dict[str, Any]:
        """The registry contract, exercised: id AND version in, one served model out.

        Raises `KeyError` when the pair is not pinned.  Not "falls back to the newest
        version" -- see `ModelRef` and §15.2.9.
        """
        ref = ModelRef(model_id=model_id, model_version=model_version)
        manifest = self.manifests.get((model_id, model_version))
        slot = self.ledger.slot(ref)
        if manifest is None or slot is None:
            raise KeyError(f"{ref} is not registered on node {self.config.node_id}")
        return {
            "model_id": model_id,
            "model_version": model_version,
            "triton_model_name": manifest.triton_model_name,
            "backend": manifest.backend,
            "artifact_digest": manifest.artifact_digest,
            "preprocessing_version": manifest.preprocessing_version,
            "residency": slot.residency,
            "loaded": slot.loaded,
            "ready": self.backend.is_ready(model_id, model_version),
            "footprint_bytes": slot.footprint_bytes,
            "service_ids": list(slot.service_ids),
            "load_duration_seconds": self.load_durations.get((model_id, model_version)),
            "selftest": self.selftests.get((model_id, model_version)),
        }

    # -- rotation ticker ----------------------------------------------------------
    def start_ticker(self) -> None:
        """MOS-OPS-086's `every MEDICALOS_RESIDENCY_TICK_SECONDS` loop, in a daemon thread."""
        if self._ticker is not None:
            return

        def _run() -> None:
            while not self._stop.wait(self.settings.tick_seconds):
                try:
                    self.ledger.tick()
                except Unsatisfiable as exc:
                    LOGGER.error(
                        json.dumps({"event": "gpu_residency_unsatisfiable", **exc.to_body()})
                    )
                except Exception:  # noqa: BLE001 - a ticker that dies stops the rotation
                    LOGGER.exception("residency tick failed")

        self._ticker = threading.Thread(target=_run, name="residency-tick", daemon=True)
        self._ticker.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ticker is not None:
            self._ticker.join(timeout=5)
            self._ticker = None

    def metrics(self) -> str:
        """The §13.10 metric names, in Prometheus text format.

        `MOS-REL-016` ships the helpers and not the dashboards, so this is deliberately a
        handful of counters rather than a metrics framework.
        """
        lines = [
            "# HELP medicalos_model_load_total model loads by outcome",
            "# TYPE medicalos_model_load_total counter",
            f'medicalos_model_load_total{{outcome="loaded"}} {self.ledger.loads}',
            f'medicalos_model_load_total{{outcome="evicted"}} {self.ledger.evictions}',
            "# HELP medicalos_model_load_duration_seconds last load duration per model",
            "# TYPE medicalos_model_load_duration_seconds gauge",
        ]
        for (model_id, version), duration in sorted(self.load_durations.items()):
            lines.append(
                f'medicalos_model_load_duration_seconds{{model_id="{model_id}",'
                f'model_version="{version}"}} {duration:.4f}'
            )
        state = self.ledger.state()
        lines += [
            "# HELP medicalos_residency_budget_bytes residency budget on this node",
            "# TYPE medicalos_residency_budget_bytes gauge",
            f"medicalos_residency_budget_bytes {state['budget_bytes']}",
            "# HELP medicalos_residency_committed_bytes committed against the budget",
            "# TYPE medicalos_residency_committed_bytes gauge",
            f"medicalos_residency_committed_bytes {state['committed_bytes']}",
        ]
        return "\n".join(lines) + "\n"


# =====================================================================================
# Request bodies.  MODULE level, not nested inside `create_app`, for a reason worth
# recording: this file carries `from __future__ import annotations`, so every annotation
# is a STRING that FastAPI resolves against the defining function's module globals.  A
# BaseModel defined inside `create_app` is not in those globals, FastAPI cannot resolve
# the name, and it silently degrades the parameter to a QUERY parameter -- producing
# `{"loc": ["query", "body"], "msg": "Field required"}` on a perfectly good JSON POST.
# Measured, 2026-09-14, before this was moved out.
#
# `protected_namespaces=()` is required because `model_id` and `model_version` collide
# with pydantic's reserved `model_` prefix.  Renaming the fields is not an option: they
# are the wire names chapter 13 §13.10.3 specifies.
# =====================================================================================
from pydantic import BaseModel, Field  # noqa: E402  (after the medos imports, by design)


class ReservationRequest(BaseModel):
    """`POST /internal/v1/residency/reservations`, §13.10.3's body, verbatim."""

    model_config = {"extra": "forbid", "protected_namespaces": ()}

    model_id: str
    model_version: str
    job_id: str
    patch_batch_size: int = Field(default=1, ge=1)
    ttl_seconds: int = Field(default=1800, ge=1, le=86400)


class RenewRequest(BaseModel):
    """`POST /internal/v1/residency/reservations/{id}/renew`."""

    model_config = {"extra": "forbid", "protected_namespaces": ()}

    ttl_seconds: int = Field(default=600, ge=1, le=86400)


# =====================================================================================
# The HTTP surface.  MOS-OPS-113: `/internal/v1/...` is mesh-internal, is NOT public API,
# MUST NOT be reachable from the edge zone, and MUST NOT appear in the generated OpenAPI
# document -- which is why every internal route below carries `include_in_schema=False`.
# =====================================================================================
def create_app(daemon: Tritond):  # type: ignore[no-untyped-def]
    """Build the tritond ASGI app. FastAPI, because `medos.api` already pins it.

    Authentication is NOT implemented on this surface and that is stated rather than
    hidden: `MOS-OPS-113` requires workload identity or a component-scoped key, and this
    compose deployment substitutes network placement (an `internal: true` compose network,
    no published port) for it.  That is the same documented shortcut shape the weeks 1-2
    compose file uses for the PACS, with the same closing date -- it is not a claim that
    the control is present.
    """
    from fastapi import FastAPI, HTTPException, Response
    from fastapi.responses import JSONResponse

    app = FastAPI(
        title="medicalos-tritond",
        version="0.1.0",
        description=(
            "Administrator of the single shared Triton (chapter 13 §13.10). "
            "Mesh-internal; not public API (MOS-OPS-113)."
        ),
    )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return daemon.health()

    @app.get("/readyz")
    def readyz() -> Response:
        ready, body = daemon.readiness()
        return JSONResponse(body, status_code=200 if ready else 503)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(daemon.metrics(), media_type="text/plain; version=0.0.4")

    @app.get("/internal/v1/models", include_in_schema=False)
    def list_models() -> dict[str, Any]:
        return {
            "node_id": daemon.config.node_id,
            "models": [
                daemon.resolve(model_id, version)
                for (model_id, version) in sorted(daemon.manifests)
            ],
        }

    @app.get("/internal/v1/models/{model_id}/{model_version}", include_in_schema=False)
    def get_model(model_id: str, model_version: str) -> dict[str, Any]:
        try:
            return daemon.resolve(model_id, model_version)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/internal/v1/residency/state", include_in_schema=False)
    def residency_state() -> dict[str, Any]:
        return daemon.ledger.state()

    @app.post("/internal/v1/residency/reservations", include_in_schema=False)
    def reserve(body: ReservationRequest) -> Response:
        ref = ModelRef(model_id=body.model_id, model_version=body.model_version)
        try:
            outcome = daemon.ledger.reserve(
                ref, body.job_id, body.patch_batch_size, body.ttl_seconds
            )
        except Unsatisfiable as exc:
            # MOS-OPS-083: 409 means the model can never run here. The caller MUST map it
            # to FAILED/'capacity_configuration' and never to REJECTED.
            return JSONResponse(exc.to_body(), status_code=409)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        status = 201 if outcome.state == "granted" else 202
        return JSONResponse(outcome.to_body(daemon.ledger.device_uuid), status_code=status)

    @app.post(
        "/internal/v1/residency/reservations/{reservation_id}/renew",
        include_in_schema=False,
    )
    def renew(reservation_id: str, body: RenewRequest) -> dict[str, Any]:
        reservation = daemon.ledger.renew(reservation_id, body.ttl_seconds)
        if reservation is None:
            raise HTTPException(status_code=404, detail="unknown or expired reservation")
        return {
            "reservation_id": reservation.reservation_id,
            "state": "granted",
            "expires_at": reservation.expires_at.isoformat(),
        }

    @app.delete(
        "/internal/v1/residency/reservations/{reservation_id}", include_in_schema=False
    )
    def release(reservation_id: str) -> Response:
        # MOS-OPS-082: release is idempotent. A late DELETE after TTL reclamation is
        # "harmless" by specification, so an unknown id is 204 and never 404.
        daemon.ledger.release(reservation_id)
        return Response(status_code=204)

    return app


def build_daemon() -> Tritond:
    """Construct a `Tritond` from the environment. The composition root."""
    config = TritondConfig.from_env()
    config.model_root.mkdir(parents=True, exist_ok=True)
    triton_config = TritonConfig.from_env()
    return Tritond(
        config=config,
        s3=S3Client(S3Config.from_env()),
        admin=TritonAdminClient(triton_config),
        backend=TritonBackend(triton_config),
        pins=load_pins(),
    )


def main(argv: list[str] | None = None) -> int:
    """`python -m medos.inference.tritond [serve|reconcile|state]`."""
    import argparse

    parser = argparse.ArgumentParser(prog="medicalos-tritond")
    parser.add_argument(
        "command", nargs="?", default="serve", choices=["serve", "reconcile", "state"]
    )
    parser.add_argument("--wait-for-triton-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("MEDOS_LOG_LEVEL", "INFO"),
        format="%(message)s",
    )
    daemon = build_daemon()

    deadline = time.time() + args.wait_for_triton_seconds
    while time.time() < deadline and not daemon.admin.is_live():
        time.sleep(1.0)
    if not daemon.admin.is_live():
        LOGGER.error(
            json.dumps({"event": "triton_unreachable", "url": daemon.admin.config.url})
        )
        return 1

    report = daemon.reconcile()
    LOGGER.info(json.dumps({"event": "reconciled", **report}))
    if args.command == "reconcile":
        return 0
    if args.command == "state":
        print(json.dumps(daemon.ledger.state(), indent=2))
        return 0

    import uvicorn

    daemon.start_ticker()
    uvicorn.run(
        create_app(daemon),
        host=daemon.config.http_host,
        port=daemon.config.http_port,
        log_level=os.environ.get("MEDOS_LOG_LEVEL", "info").lower(),
        access_log=False,
    )
    daemon.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
