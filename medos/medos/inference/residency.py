# SPDX-License-Identifier: Apache-2.0
"""The residency contract of chapter 13 §13.10.3/§13.10.4, as executable logic.

WHAT THIS IS
    A memory budget, a set of reservations against it, and the non-starving rotation of
    `MOS-OPS-086`.  All of it is pure bookkeeping over an injected clock and two injected
    callbacks (`load` / `unload`), so the three-capabilities-two-slots case of §13.10.4
    can be executed as a unit test rather than argued about, and so `tritond` can hold the
    only Triton admin client while this module holds none.

THE HONEST STATEMENT ABOUT GPUs
    §13.10.3 is written for a GPU: `MOS-OPS-079` requires
    `MEDICALOS_GPU_RUNTIME_RESERVED_BYTES` to be **measured** at startup, and
    `MOS-OPS-090` requires a 60-second `nvidia-smi` reconciliation.  This deployment has
    no GPU (the compose profile is CPU; `docs/spec/15-delivery.md` §15.2.4 asks for the
    move onto shared Triton, not for a GPU).  So:

      * The arithmetic -- `budget_bytes`, `footprint_bytes`, `committed_bytes`, the
        eviction order, the hold window, the starvation-free promotion -- is implemented
        exactly as specified and is independent of the device.
      * `MEDICALOS_GPU_RUNTIME_RESERVED_BYTES` is measured the CPU-mode way: process RSS
        headroom rather than `nvidia-smi` free memory.  `ResidencyLedger.device` records
        `"cpu"` so nothing downstream can mistake this for a GPU accounting.
      * `MOS-OPS-090`'s `nvidia-smi` reconciliation is NOT implemented and is NOT faked.
        `reconcile_observed()` raises `NotImplementedError` on a CPU node with the reason.
        A stub that returned "no drift" would be worse than an absent one: it would make
        `SealedServiceFootprintDrift` un-fireable while appearing to be implemented.

WHY EVICTION ORDER IS THREE RULES AND NOT ONE
    `MOS-OPS-114`: every `evictable` victim must be exhausted before any `on_demand`
    model is evicted, `resident` is never a victim, and an `on_demand` model inside its
    hold window is not a victim either.  Collapsing that to "least recently used" is the
    thrash `MOS-OPS-087` exists to prevent: on a two-slot node, LRU evicts precisely the
    model the next job needs.

Spec: MOS-OPS-079, MOS-OPS-080, MOS-OPS-081, MOS-OPS-082, MOS-OPS-083, MOS-OPS-085,
MOS-OPS-086, MOS-OPS-087, MOS-OPS-090, MOS-OPS-092, MOS-OPS-114, MOS-REG-076a.
"""

from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from medos.inference.backend import ModelRef

__all__ = [
    "ResidencyClass",
    "RESIDENCY_CLASSES",
    "ResidencySettings",
    "ModelSlot",
    "Reservation",
    "ResidencyLedger",
    "ReservationOutcome",
    "Unsatisfiable",
]

# MOS-OPS-114: "These three values are the whole of the column".  `sealed service` in the
# §13.10.3 table is a footprint CATEGORY, not a value of `residency`, and is represented
# here by `ResidencySettings.sealed_service_bytes` rather than by a fourth class.
ResidencyClass = Literal["resident", "on_demand", "evictable"]
RESIDENCY_CLASSES: frozenset[str] = frozenset({"resident", "on_demand", "evictable"})

_Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Unsatisfiable(RuntimeError):
    """`409 unsatisfiable` of §13.10.3: this model can never run on this node.

    `MOS-OPS-083` is explicit about what the caller must do with it: job `FAILED` with
    `failure.class = 'internal'` and `failure.code = 'capacity_configuration'`, and
    "**never `REJECTED`**, because `REJECTED` is a clinical outcome about the study and
    this is an operator error about the cluster".
    """

    def __init__(self, ref: ModelRef, footprint: int, budget: int, pinned: int) -> None:
        super().__init__(
            f"{ref}: footprint {footprint} B exceeds budget {budget} B "
            f"(resident-pinned {pinned} B)"
        )
        self.ref = ref
        self.footprint_bytes = footprint
        self.budget_bytes = budget
        self.resident_pinned_bytes = pinned

    def to_body(self) -> dict[str, object]:
        return {
            "state": "unsatisfiable",
            "reason": "footprint_exceeds_budget",
            "footprint_bytes": self.footprint_bytes,
            "budget_bytes": self.budget_bytes,
            "resident_pinned_bytes": self.resident_pinned_bytes,
        }


@dataclass(frozen=True)
class ResidencySettings:
    """The environment variables of §13.10.3, with their specified defaults and bounds."""

    # MOS-OPS-079: MEASURED at startup, never estimated.  The 1.2 GiB in the chart is "a
    # starting value only", which is why this field has no default that pretends otherwise
    # -- `ResidencyLedger.measure_budget` fills it.
    runtime_reserved_bytes: int
    device_total_bytes: int
    # MOS-OPS-080: default 0.90, MUST NOT exceed 0.95.
    headroom_ratio: float = 0.90
    # MOS-OPS-087: default 300, clamped upward against the p95 job duration.
    min_hold_seconds: int = 300
    # MOS-OPS-086: the rotation tick.
    tick_seconds: float = 2.0
    # MOS-OPS-081: how often a worker refreshes `admissible_service_ids`.
    poll_interval_seconds: float = 2.0
    # §13.10.3: sealed services hold their declared footprint permanently (MOS-OPS-089).
    sealed_service_bytes: int = 0
    device: str = "cpu"

    def __post_init__(self) -> None:
        if not 0.0 < self.headroom_ratio <= 0.95:
            raise ValueError(
                f"MEDICALOS_GPU_HEADROOM_RATIO={self.headroom_ratio} violates "
                "MOS-OPS-080 (must be > 0 and MUST NOT exceed 0.95); a budget at 1.0 "
                "produces out-of-memory failures mid-study"
            )

    @property
    def budget_bytes(self) -> int:
        """`(total - runtime_reserved) * headroom`, exactly as §13.10.3 writes it."""
        return int(
            (self.device_total_bytes - self.runtime_reserved_bytes) * self.headroom_ratio
        )

    @staticmethod
    def from_env(device_total_bytes: int, runtime_reserved_bytes: int) -> ResidencySettings:
        ratio = float(os.environ.get("MEDICALOS_GPU_HEADROOM_RATIO", "0.90"))
        hold = int(os.environ.get("MEDICALOS_RESIDENCY_MIN_HOLD_SECONDS", "300"))
        return ResidencySettings(
            runtime_reserved_bytes=runtime_reserved_bytes,
            device_total_bytes=device_total_bytes,
            headroom_ratio=ratio,
            min_hold_seconds=hold,
            tick_seconds=float(os.environ.get("MEDICALOS_RESIDENCY_TICK_SECONDS", "2")),
            poll_interval_seconds=float(
                os.environ.get("MEDICALOS_RESIDENCY_POLL_INTERVAL_SECONDS", "2")
            ),
            sealed_service_bytes=int(
                os.environ.get("MEDICALOS_SEALED_SERVICE_BYTES", "0")
            ),
            device=os.environ.get("MEDICALOS_RESIDENCY_DEVICE", "cpu"),
        )


@dataclass
class ModelSlot:
    """One registered ModelVersion and everything residency needs to know about it."""

    ref: ModelRef
    residency: ResidencyClass
    footprint_bytes: int
    service_ids: tuple[str, ...] = ()
    loaded: bool = False
    last_used_at: datetime | None = None
    held_until: datetime | None = None
    active_reservations: int = 0
    queued_jobs: int = 0
    waiting_since: datetime | None = None
    load_duration_seconds: float | None = None

    def to_state(self) -> dict[str, object]:
        return {
            "model_id": self.ref.model_id,
            "model_version": self.ref.model_version,
            "class": self.residency,
            "footprint_bytes": self.footprint_bytes,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "held_until": self.held_until.isoformat() if self.held_until else None,
            "active_reservations": self.active_reservations,
        }


@dataclass
class Reservation:
    reservation_id: str
    ref: ModelRef
    job_id: str
    patch_batch_size: int
    expires_at: datetime
    granted_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class ReservationOutcome:
    """`201 granted` or `202 queued` of §13.10.3. `409` is raised as `Unsatisfiable`."""

    state: Literal["granted", "queued"]
    reservation_id: str | None = None
    triton_model_name: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None
    position: int | None = None
    retry_after_seconds: int | None = None

    def to_body(self, device_uuid: str | None = None) -> dict[str, object]:
        if self.state == "granted":
            return {
                "reservation_id": self.reservation_id,
                "state": "granted",
                "gpu_uuid": device_uuid,
                "triton_model_name": self.triton_model_name,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            }
        return {
            "state": "queued",
            "reason": self.reason,
            "position": self.position,
            "retry_after_seconds": self.retry_after_seconds,
        }


class ResidencyLedger:
    """The budget, the reservations, and the rotation. Thread-safe; holds no HTTP client.

    `load` and `unload` are injected callables taking a `ModelRef`.  `tritond` passes the
    Triton admin client's methods; a unit test passes recorders.  That is what makes
    §13.10.4's three-capabilities-two-slots scenario a test rather than a claim.
    """

    def __init__(
        self,
        settings: ResidencySettings,
        *,
        load: Callable[[ModelRef], float],
        unload: Callable[[ModelRef], None],
        clock: _Clock = _utcnow,
        node_id: str = "node-01",
        device_uuid: str | None = None,
    ) -> None:
        self.settings = settings
        self._load = load
        self._unload = unload
        self._clock = clock
        self.node_id = node_id
        self.device_uuid = device_uuid
        self._slots: dict[tuple[str, str], ModelSlot] = {}
        self._reservations: dict[str, Reservation] = {}
        self._lock = threading.RLock()
        self.evictions = 0
        self.loads = 0

    # -- registration -------------------------------------------------------------
    def register(self, slot: ModelSlot) -> ModelSlot:
        """Add a ModelVersion to the ledger. Idempotent on `(model_id, model_version)`.

        `MOS-OPS-084` -- refuse a `SERVING` transition whose `resident`-class footprint
        would exceed the budget -- is applied here rather than at load time, because "an
        unrunnable capability discovered at 2 a.m." is exactly what deferring it produces.
        """
        if slot.residency not in RESIDENCY_CLASSES:
            raise ValueError(
                f"residency={slot.residency!r} is not one of {sorted(RESIDENCY_CLASSES)} "
                "(MOS-OPS-114 closes the set)"
            )
        with self._lock:
            key = (slot.ref.model_id, slot.ref.model_version)
            self._slots[key] = slot
            pinned = self._pinned_bytes()
            if slot.residency == "resident" and pinned > self.settings.budget_bytes:
                del self._slots[key]
                raise Unsatisfiable(
                    slot.ref, slot.footprint_bytes, self.settings.budget_bytes, pinned
                )
            return slot

    def slot(self, ref: ModelRef) -> ModelSlot | None:
        return self._slots.get((ref.model_id, ref.model_version))

    # -- accounting ---------------------------------------------------------------
    def _pinned_bytes(self) -> int:
        return (
            sum(s.footprint_bytes for s in self._slots.values() if s.residency == "resident")
            + self.settings.sealed_service_bytes
        )

    def committed_bytes(self) -> int:
        """`sum over resident + granted-but-not-yet-loaded` + sealed services (§13.10.3)."""
        with self._lock:
            total = self.settings.sealed_service_bytes
            for slot in self._slots.values():
                if slot.loaded or slot.residency == "resident":
                    total += slot.footprint_bytes
            return total

    # -- reservations -------------------------------------------------------------
    def reserve(
        self, ref: ModelRef, job_id: str, patch_batch_size: int, ttl_seconds: int
    ) -> ReservationOutcome:
        """`POST /internal/v1/residency/reservations`. 201, 202 or `Unsatisfiable`.

        `MOS-OPS-085` is the rule that shapes the 202 branch: "Nothing fails.  Jobs for
        the non-resident capability remain `QUEUED`.  No job is `REJECTED`, no job is
        `FAILED`, no retry budget is consumed."  So contention returns `queued`, never an
        error, and the caller is told when to come back.
        """
        with self._lock:
            self._expire_locked()
            slot = self.slot(ref)
            if slot is None:
                raise KeyError(
                    f"{ref} is not registered on this node; a reservation for an "
                    "unregistered ModelVersion is a deployment error, not contention"
                )
            if (
                slot.footprint_bytes + self._sealed_and_pinned_excluding(ref)
                > self.settings.budget_bytes
            ):
                raise Unsatisfiable(
                    ref,
                    slot.footprint_bytes,
                    self.settings.budget_bytes,
                    self._pinned_bytes(),
                )
            now = self._clock()
            if not slot.loaded:
                if not self._make_room_locked(slot):
                    slot.queued_jobs += 1
                    if slot.waiting_since is None:
                        slot.waiting_since = now
                    return ReservationOutcome(
                        state="queued",
                        reason="gpu_memory_contention",
                        position=self._queue_position_locked(slot),
                        retry_after_seconds=max(1, int(self.settings.tick_seconds * 10)),
                    )
                self._load_locked(slot)
            reservation = Reservation(
                reservation_id=f"res_{secrets.token_hex(8)}",
                ref=ref,
                job_id=job_id,
                patch_batch_size=patch_batch_size,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self._reservations[reservation.reservation_id] = reservation
            slot.active_reservations += 1
            slot.last_used_at = now
            slot.queued_jobs = max(0, slot.queued_jobs - 1)
            if slot.queued_jobs == 0:
                slot.waiting_since = None
            from medos.inference.repository import triton_model_name

            return ReservationOutcome(
                state="granted",
                reservation_id=reservation.reservation_id,
                triton_model_name=triton_model_name(ref.model_id, ref.model_version),
                expires_at=reservation.expires_at,
            )

    def renew(self, reservation_id: str, ttl_seconds: int) -> Reservation | None:
        """`MOS-OPS-082`: renewed at least every `ttl_seconds / 3` while the job runs."""
        with self._lock:
            self._expire_locked()
            reservation = self._reservations.get(reservation_id)
            if reservation is None:
                return None
            reservation.expires_at = self._clock() + timedelta(seconds=ttl_seconds)
            slot = self.slot(reservation.ref)
            if slot is not None:
                slot.last_used_at = self._clock()
            return reservation

    def release(self, reservation_id: str) -> bool:
        """`DELETE`. Idempotent (MOS-OPS-082): a late DELETE from a resurrected worker
        whose reservation already expired by TTL is harmless and returns False."""
        with self._lock:
            reservation = self._reservations.pop(reservation_id, None)
            if reservation is None:
                return False
            slot = self.slot(reservation.ref)
            if slot is not None:
                slot.active_reservations = max(0, slot.active_reservations - 1)
                slot.last_used_at = self._clock()
            return True

    def release_for_job(self, job_id: str) -> int:
        """Release every reservation a job holds. `MOS-OPS-082` keys release by `job_id`."""
        with self._lock:
            ids = [r.reservation_id for r in self._reservations.values() if r.job_id == job_id]
            for reservation_id in ids:
                self.release(reservation_id)
            return len(ids)

    def _expire_locked(self) -> None:
        now = self._clock()
        for reservation_id, reservation in list(self._reservations.items()):
            if reservation.expires_at <= now:
                del self._reservations[reservation_id]
                slot = self.slot(reservation.ref)
                if slot is not None:
                    slot.active_reservations = max(0, slot.active_reservations - 1)

    def _sealed_and_pinned_excluding(self, ref: ModelRef) -> int:
        return sum(
            s.footprint_bytes
            for key, s in self._slots.items()
            if s.residency == "resident" and key != (ref.model_id, ref.model_version)
        ) + self.settings.sealed_service_bytes

    def _queue_position_locked(self, slot: ModelSlot) -> int:
        waiting = [
            s for s in self._slots.values() if s.queued_jobs > 0 and s.waiting_since
        ]
        waiting.sort(key=lambda s: s.waiting_since or self._clock())
        return waiting.index(slot) + 1 if slot in waiting else 1

    # -- loading / eviction -------------------------------------------------------
    def _load_locked(self, slot: ModelSlot) -> None:
        duration = self._load(slot.ref)
        slot.loaded = True
        slot.load_duration_seconds = duration
        self.loads += 1
        now = self._clock()
        slot.last_used_at = now
        # MOS-OPS-114: `evictable` gets NO hold window, and tritond MUST NOT clamp one on.
        hold = 0 if slot.residency == "evictable" else self.settings.min_hold_seconds
        slot.held_until = now + timedelta(seconds=hold)
        # MOS-OPS-092: p95 load > 30 s forces residency = 'resident', or the Deployment is
        # refused.  Measured here, where the number actually exists.
        if duration > 30.0 and slot.residency != "resident":
            raise Unsatisfiable(
                slot.ref, slot.footprint_bytes, self.settings.budget_bytes,
                self._pinned_bytes(),
            )

    def _victims_locked(self) -> list[ModelSlot]:
        """MOS-OPS-086's victim order, and MOS-OPS-114's exhaustion rule.

        Every `evictable` with no active reservation first (least recently used first),
        and only then `on_demand` models that are out of their hold window.  `resident` is
        never in this list.
        """
        now = self._clock()

        def _lru(slots: list[ModelSlot]) -> list[ModelSlot]:
            return sorted(
                slots, key=lambda s: s.last_used_at or datetime.min.replace(tzinfo=UTC)
            )

        evictable = [
            s for s in self._slots.values()
            if s.loaded and s.residency == "evictable" and s.active_reservations == 0
        ]
        on_demand = [
            s for s in self._slots.values()
            if s.loaded
            and s.residency == "on_demand"
            and s.active_reservations == 0
            and (s.held_until is None or now >= s.held_until)
        ]
        return _lru(evictable) + _lru(on_demand)

    def _make_room_locked(self, promote: ModelSlot) -> bool:
        """The `while` loop of MOS-OPS-086. True when `promote` now fits."""
        while self.committed_bytes() + promote.footprint_bytes > self.settings.budget_bytes:
            victims = self._victims_locked()
            if not victims:
                return False  # everything is pinned, in use, or inside its hold window
            victim = victims[0]
            self._unload(victim.ref)
            victim.loaded = False
            victim.held_until = None
            self.evictions += 1
        return True

    def tick(self) -> ModelSlot | None:
        """One iteration of MOS-OPS-086's rotation. Returns the model promoted, if any.

        FIFO by wait time -- `argmax(waiting_since)` in the specification's pseudocode
        means the LONGEST-waiting model, which is `min` over the timestamp.  That is what
        makes the rotation starvation-free: a model that has been waiting longest cannot
        be overtaken indefinitely by a model with more queued jobs.
        """
        with self._lock:
            self._expire_locked()
            waiting = [
                s for s in self._slots.values()
                if not s.loaded and s.queued_jobs > 0 and s.waiting_since is not None
            ]
            if not waiting:
                return None
            promote = min(waiting, key=lambda s: s.waiting_since or self._clock())
            if promote.footprint_bytes > self.settings.budget_bytes:
                raise Unsatisfiable(
                    promote.ref, promote.footprint_bytes,
                    self.settings.budget_bytes, self._pinned_bytes(),
                )
            if not self._make_room_locked(promote):
                return None
            self._load_locked(promote)
            return promote

    # -- introspection ------------------------------------------------------------
    def admissible_service_ids(self) -> list[str]:
        """`MOS-OPS-081`: the inboxes a worker may `Claim` from right now.

        Only services whose model is LOADED. A worker that claims first and waits for a
        slot burns lease time and starves other workers of that job.
        """
        with self._lock:
            ids: set[str] = set()
            for slot in self._slots.values():
                if slot.loaded:
                    ids.update(slot.service_ids)
            return sorted(ids)

    def state(self) -> dict[str, object]:
        """`GET /internal/v1/residency/state`, in §13.10.3's shape.

        `gpu_uuid` is present and `null` on a CPU node rather than omitted: a consumer
        that keys on the field's presence must not silently treat a CPU node as a GPU one,
        and `device` says which this is.
        """
        with self._lock:
            self._expire_locked()
            return {
                "node_id": self.node_id,
                "device": self.settings.device,
                "gpu_uuid": self.device_uuid,
                "budget_bytes": self.settings.budget_bytes,
                "committed_bytes": self.committed_bytes(),
                "resident": [s.to_state() for s in self._slots.values() if s.loaded],
                "waiting": [
                    {
                        "model_id": s.ref.model_id,
                        "model_version": s.ref.model_version,
                        "queued_jobs": s.queued_jobs,
                        "waiting_since": (
                            s.waiting_since.isoformat() if s.waiting_since else None
                        ),
                    }
                    for s in self._slots.values()
                    if not s.loaded and s.queued_jobs > 0
                ],
                "admissible_service_ids": self.admissible_service_ids(),
                "evictions_total": self.evictions,
                "loads_total": self.loads,
            }

    def reconcile_observed(self) -> dict[str, object]:
        """MOS-OPS-090. NOT IMPLEMENTED on a CPU node, and deliberately not stubbed.

        The rule scrapes `nvidia-smi --query-compute-apps` and attributes usage to
        processes so that `SealedServiceFootprintDrift` can fire.  There is no
        `nvidia-smi` here and no sealed service to drift.  Returning a cheerful "no drift"
        would make an unimplemented safety control indistinguishable from a passing one.
        """
        raise NotImplementedError(
            "MOS-OPS-090 requires nvidia-smi attribution; this node is "
            f"device={self.settings.device!r}. Not implemented and not stubbed: a fake "
            "'no drift' result would make SealedServiceFootprintDrift un-fireable while "
            "appearing implemented."
        )
