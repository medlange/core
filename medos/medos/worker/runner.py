# SPDX-License-Identifier: Apache-2.0
"""The claim loop (CONTRACT.md section 1: `worker/runner.py`).

One process. It claims a job through the `JobQueue` port, heartbeats the lease while the
step executor runs, and writes exactly one terminal transition. Nothing else.

THE FOUR THINGS THIS FILE IS RESPONSIBLE FOR
--------------------------------------------
1. **Claiming under a capacity gate.** MOS-EXEC-064 mechanism 1: "The queue is pull-based,
   so load-shedding is *not calling `Claim`*. No row is touched, `claim_count` is not
   incremented, and the property holds by construction. A runner MUST call `Claim` only
   when it has that many free execution slots." `run_once()` therefore acquires a slot
   BEFORE it calls `claim_lease()` and returns `None` if it cannot. That ordering IS the
   load-shed implementation, and `test_worker.py` asserts the consequence -- a shed run
   leaves `job_queue.claim_count` unchanged, so it consumes no retry budget.

2. **Heartbeating on a separate connection.** MOS-EXEC-028/029. The heartbeat CANNOT share
   the worker's connection: the worker holds a long transaction around the terminal
   commit, and a second statement on the same psycopg connection from another thread is
   both unsafe and, inside that transaction, semantically wrong -- it would extend the
   lease as part of a transaction that may still roll back. So the heartbeat thread opens
   its own connection and its own `PostgresJobQueue`.

3. **Aborting when the lease is lost.** MOS-EXEC-028: "A runner that receives
   `Valid = false` MUST abort immediately, MUST NOT call `Complete` or `Fail`, MUST NOT
   STOW any DICOM object." `_Heartbeat` is the `LeaseGuard` the executor calls between
   steps and immediately before the STOW; `LeaseLost` propagates out of `execute_plan` and
   this module deliberately performs NO transition on it. The job stays RUNNING until its
   lease expires and the reclaimer requeues it -- which is exactly right, because by then
   another worker may already own it.

4. **Turning an exception into the right terminal state.** CONTRACT.md section 3 and
   MOS-EXEC-014. The mapping is on the exception TYPE:

       ClinicalRejection  -> T7  REJECTED  (queue.reject, machine-readable reason)
       TransportFailure   -> T9/T11 via fail_ex(retryable=True)
       SystemFailure/else -> T11 via fail_ex(retryable=False)

   `REJECTED` is never reached from a transport or system fault and `FAILED` is never
   reached from a clinical one. That is the release gate: selection and geometry failures
   are answers about the study, not incidents.

WHAT IT DOES NOT DO
-------------------
It does not decide whether a retryable failure retries -- MOS-EXEC-030 puts that in
`fail_ex`, and "the caller MUST NOT make that decision itself". It does not touch
`job_queue` (MOS-EXEC-025). It does not set `jobs.phase` (the `job_steps` rollup trigger
does). It runs no thread pool: `max_concurrent_jobs` is the slot COUNT the capacity gate
enforces, and in this slice it is 1, because the capabilities are in-process and CPU-bound
and a second concurrent job would just make both slower while doubling peak memory.

CRASH BEHAVIOUR (the property the test kills a process to prove)
---------------------------------------------------------------
There is no crash handler, on purpose. A worker that dies mid-job leaves the `jobs` row
RUNNING and the `job_queue` row leased. The lease expires; `reclaim_expired()` releases it
with backoff (T10); the next `claim()` increments `claim_count` and copies it to
`jobs.attempt`, so the job resumes on attempt N+1 (`medos.db.queue`, reading 1). Nothing
the dying process could have written would have made that safer, and a `finally:` that
called `fail()` would be actively worse -- it would consume retry budget for a crash the
runner cannot diagnose, and it cannot run at all for a `SIGKILL`.

Spec: MOS-EXEC-014, MOS-EXEC-015, MOS-EXEC-025..030, MOS-EXEC-035, MOS-EXEC-057,
MOS-EXEC-062..064, MOS-EXEC-069, CONTRACT.md sections 3, 4, 8 and 11.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg

from medos.capabilities import providers as capability_providers
from medos.core.errors import ClinicalRejection, MedosError, SystemFailure
from medos.db import repo
from medos.db.conn import connect, dsn_from_env
from medos.db.queue import (
    DEFAULT_SERVICE_ID,
    Lease,
    LeaseLost,
    PostgresJobQueue,
    Reclaimed,
    make_worker_id,
)
from medos.db.tenancy import (
    bind_current_tenant,
    reset_current_tenant,
    serving_tenants,
    tenant_context,
    tenant_tx,
)
from medos.dicomweb.gateway import DicomWebGateway, GatewayConfig
from medos.inference.dispatch import build_resolver
from medos.worker.steps import (
    PipelineState,
    StepContext,
    WorkerDeps,
    execute_plan,
    failure_class_for,
    reject_reason_code,
)

__all__ = [
    "RunnerConfig",
    "RunOutcome",
    "WorkerRunner",
    "main",
    "DEFAULT_ORTHANC_URL",
]

DEFAULT_ORTHANC_URL = os.environ.get(
    "MEDOS_DICOMWEB_URL", "http://127.0.0.1:8042/dicom-web"
)

TerminalState = Literal["COMPLETED", "REJECTED", "FAILED", "QUEUED", "ABORTED"]


# =====================================================================================
# Structured logging (CONTRACT.md section 11)
# =====================================================================================
def log_json(event: str, **fields: Any) -> None:
    """One JSON object per line on stderr. UIDs only -- never a name, an MRN or a date.

    stderr and not stdout so that a caller can parse the runner's stdout (the `--json`
    run summary) without the log stream in it.
    """
    record = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)


# =====================================================================================
# Configuration
# =====================================================================================
@dataclass(frozen=True)
class RunnerConfig:
    dsn: str = field(default_factory=dsn_from_env)
    dicomweb_url: str = DEFAULT_ORTHANC_URL
    work_root: Path = field(
        default_factory=lambda: Path(os.environ["MEDOS_WORK_ROOT"])
        if os.environ.get("MEDOS_WORK_ROOT")
        else Path.cwd() / ".work"
    )
    service_id: str = DEFAULT_SERVICE_ID
    worker_id: str = field(default_factory=lambda: make_worker_id("runner"))
    # MOS-EXEC-029: LeaseDuration >= 4 * heartbeat interval. Enforced in __post_init__,
    # because the guidance is unenforceable inside `heartbeat()` -- that method cannot see
    # the interval its caller uses.
    lease_seconds: int = 120
    heartbeat_seconds: int = 20
    # MOS-EXEC-070: the attempt deadline is absolute, never a duration.
    attempt_deadline_s: int = 3600
    poll_interval_s: float = 1.0
    max_concurrent_jobs: int = 1
    reclaim_on_poll: bool = True
    dicomweb_timeout_s: float = 300.0
    keep_work_dir: bool = False
    # MOS-SEC-078 / MOS-STORE-225: "Background workers, the reconciler and the queue relay
    # MUST acquire a tenant context per unit of work. A process-wide 'admin' connection
    # that reads across tenants MUST NOT exist outside the break-glass path." So the
    # runner has no cross-tenant view at all: it iterates this set, binding one tenant at
    # a time, and every claim, heartbeat, step and transition runs inside that binding.
    # "The loop is the price of the guarantee."
    #
    # It is configuration (`MEDOS_TENANTS`) rather than `SELECT id FROM tenants` -- see
    # medos.db.tenancy.serving_tenants for why reading the table would require exactly the
    # escape hatch MOS-STORE-225 forbids.
    tenant_ids: tuple[str, ...] = field(default_factory=serving_tenants)

    def __post_init__(self) -> None:
        if self.lease_seconds < 4 * self.heartbeat_seconds:
            raise ValueError(
                f"lease_seconds={self.lease_seconds} < 4 * heartbeat_seconds="
                f"{self.heartbeat_seconds}; MOS-EXEC-029 requires LeaseDuration >= 4 * "
                "the heartbeat interval, or a single missed beat expires a healthy lease"
            )
        if self.max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be >= 1")
        if not self.tenant_ids:
            raise ValueError(
                "tenant_ids is empty: a worker with no tenant context serves nothing "
                "(MOS-SEC-078). Set MEDOS_TENANTS or pass tenant_ids explicitly."
            )


@dataclass(frozen=True)
class RunOutcome:
    """What one claimed job did. Returned rather than logged so a test can assert on it."""

    job_id: str
    attempt: int
    terminal_state: TerminalState
    duration_ms: int
    phase: str | None = None
    reject_reason_code: str | None = None
    reject_detail_code: str | None = None  # the fine-grained MOS-IMG-010 / selector code
    failure_class: str | None = None
    failure_code: str | None = None
    retry_at: datetime | None = None
    # `None` means NOT WRITTEN, not "unknown". A job whose capability had nothing to
    # outline writes no SEG, and the caller has to be able to tell that from a SEG that
    # went missing -- `outputs_omitted` carries the reason for every object the job asked
    # for and did not get (`medos.writer.identity.OmittedOutput`, MOS-SVC-011).
    seg_sop_instance_uid: str | None = None
    sr_sop_instance_uid: str | None = None
    result_ids: tuple[str, ...] = ()
    outputs_omitted: tuple[dict[str, str], ...] = ()


# =====================================================================================
# Heartbeat
# =====================================================================================
class _Heartbeat:
    """MOS-EXEC-028's lease extension, on its own connection and its own thread.

    Also the executor's `LeaseGuard`: `check()` raises `LeaseLost` the moment a beat has
    come back `False`, so the abort happens at the next step boundary rather than after
    the STOW.

    A DB error during a beat is NOT treated as a lost lease. Losing the lease is a fact
    the database asserted; failing to ask is not, and treating a transient connection
    blip as "another worker owns this job" would abandon a perfectly good in-flight
    inference. The error is logged and the next beat retries; if the outage outlasts the
    lease, the reclaimer does the right thing anyway.
    """

    def __init__(
        self,
        dsn: str,
        job_id: str,
        worker_id: str,
        *,
        lease_seconds: int,
        interval_s: float,
        service_id: str = DEFAULT_SERVICE_ID,
        tenant_id: str,
    ) -> None:
        self._dsn = dsn
        self._job_id = job_id
        self._worker_id = worker_id
        # Carried explicitly, not inherited. `threading.Thread` starts with an EMPTY
        # contextvars context -- it does not copy the parent's -- so the ambient tenant
        # binding of `run_once()` is invisible inside `_run()`. Without this the first
        # heartbeat would raise NoTenantContextError, and the lease would silently expire
        # while a perfectly healthy job kept working (MOS-SEC-078: a background thread
        # acquires a tenant context per unit of work).
        self._tenant_id = tenant_id
        self._lease_seconds = lease_seconds
        self._interval_s = interval_s
        self._service_id = service_id
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None
        self._beats = 0
        self._conn: psycopg.Connection[Any] | None = None

    @property
    def beats(self) -> int:
        return self._beats

    def check(self) -> None:
        if self._lost.is_set():
            raise LeaseLost(self._job_id, self._worker_id)

    def _run(self) -> None:
        bind_current_tenant(self._tenant_id)   # first statement in this thread
        conn = connect(self._dsn, autocommit=True, application_name="medos-heartbeat")
        self._conn = conn
        queue = PostgresJobQueue(conn, service_id=self._service_id)
        try:
            while not self._stop.wait(self._interval_s):
                try:
                    alive = queue.heartbeat(self._job_id, self._worker_id, self._lease_seconds)
                except Exception as exc:  # noqa: BLE001 - see the class docstring
                    log_json(
                        "heartbeat.error",
                        job_id=self._job_id,
                        worker_id=self._worker_id,
                        error=type(exc).__name__,
                    )
                    continue
                self._beats += 1
                if not alive:
                    self._lost.set()
                    log_json(
                        "heartbeat.lease_lost",
                        job_id=self._job_id,
                        worker_id=self._worker_id,
                        beats=self._beats,
                    )
                    return
        finally:
            conn.close()

    def __enter__(self) -> _Heartbeat:
        self._thread = threading.Thread(
            target=self._run, name=f"hb-{self._job_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 5.0)


# =====================================================================================
# The runner
# =====================================================================================
class WorkerRunner:
    """Claim -> plan -> execute -> one terminal transition. Repeat."""

    def __init__(
        self,
        config: RunnerConfig | None = None,
        *,
        conn: psycopg.Connection[Any] | None = None,
        deps: WorkerDeps | None = None,
    ) -> None:
        self.config = config or RunnerConfig()

        # FAIL CLOSED BEFORE ANYTHING IS OPENED.
        #
        # `MEDOS_CAPABILITY_PROVIDERS` is this deployment's statement of which capabilities
        # it serves, and it is resolved through the same function `medos-api`'s
        # `known_capability_ids()` calls. A provider that does not import, exposes no
        # `worker_registry`, or collides with a platform capability id raises here and the
        # process exits non-zero -- it does NOT quietly serve the platform three while the
        # API, reading the same variable, accepts jobs for a fourth.
        #
        # FIRST, ahead of `connect()`, and that ordering is the point: a configuration
        # defect must not be reported as a database problem, and a worker must not hold a
        # connection, a claim or a lease while it is discovering that it cannot run
        # anything. Measured: before this line moved here, a broken provider in the
        # container surfaced as `psycopg.OperationalError` from the DSN, which names the
        # wrong thing entirely. Memoised, so the `WorkerDeps` defaults below reuse it.
        if deps is None:
            capability_providers.resolve()

        self._owns_conn = conn is None
        # autocommit: every `medos.db` helper wraps itself in `conn.transaction()`, which
        # on an autocommit connection is a real BEGIN/COMMIT. So each step's rows commit
        # as they are written -- a crash leaves the progress that actually happened -- and
        # the one place that needs several writes to be atomic opens the transaction
        # explicitly (`execute_plan`'s terminal block). With autocommit=False a bare
        # `SELECT` in a repo helper would silently open a transaction that never closed,
        # and every subsequent `transaction()` would degrade to a savepoint inside it.
        self.conn = conn or connect(
            self.config.dsn, autocommit=True, application_name="medos-worker"
        )
        self.queue = PostgresJobQueue(
            self.conn,
            service_id=self.config.service_id,
            attempt_deadline_s=self.config.attempt_deadline_s,
        )
        self.deps = deps or WorkerDeps(
            # The gateway is the only object in `medos` permitted to reach a PACS
            # (CONTRACT.md section 1). Constructed here, once, and passed explicitly.
            # `GatewayConfig.from_env()` and NOT a bare `GatewayConfig(...)`. That
            # classmethod is, by its own docstring, "the ONLY place in `medos` that reads
            # a PACS credential variable"; constructing the config directly therefore
            # produced a client with `PacsCredentials()` -- no user, no bearer token -- and
            # sent every request to the Gateway anonymously.
            #
            # That was not cosmetic while it lasted. MOS-DATA-002 makes the Gateway the
            # sole route to the PACS and MOS-DATA-017 gives this container a scoped Gateway
            # TOKEN for it; docker-compose.yml sets `MEDOS_DICOMWEB_TOKEN` on medos-worker
            # and says in a comment that this module reads it. Nothing did, so the Gateway
            # correctly answered 401 and every job died at `fetch_series` with
            # `transport_failure` / `DICOMWEB_UNAUTHORIZED` -- including studies whose
            # honest outcome was a clinical REJECTED, which is the distinction
            # MOS-EXEC-014 and the `rejection-distinct` release gate exist to protect.
            #
            # `replace()` keeps the existing precedence: `--dicomweb-url` and the runner's
            # timeout still win over the environment, and only the credential and TLS
            # settings come from `from_env()`.
            gateway=DicomWebGateway(
                replace(
                    GatewayConfig.from_env(),
                    base_url=self.config.dicomweb_url,
                    timeout_s=self.config.dicomweb_timeout_s,
                )
            ),
            work_root=self.config.work_root,
            keep_work_dir=self.config.keep_work_dir,
            # Weeks 3-5, docs/spec/15-delivery.md section 15.2.4. `build_backend()`
            # returns a TritonBackend when MEDOS_TRITON_URL is set and None otherwise;
            # it CANNOT return the in-process fixture, which is not a shipped driver
            # (section 15.2.9). A worker with no Triton runs the three deterministic
            # capabilities exactly as it did in weeks 1-2.
            # A RESOLVER, not a single backend. OQ-10 is decided: the inference runtime
            # is replaceable and a deployment routes per model, so the worker carries the
            # routing and `run_capability` picks the adapter once it knows which model the
            # capability needs. `build_backend()` survives for callers that want the
            # default route only.
            inference=build_resolver(),
        )
        # MOS-EXEC-064 mechanism 1. A counting semaphore and not a lock, so the gate has
        # the same shape when `max_concurrent_jobs` grows past 1.
        self._slots = threading.BoundedSemaphore(self.config.max_concurrent_jobs)
        self.shed_count = 0
        # The tenant whose work this runner is executing right now. Set by `run_once()`
        # before every claim and read by `_run_lease()` when it hands the binding to the
        # heartbeat thread, which does not inherit it.
        self.current_tenant_id: str = self.config.tenant_ids[0]

    # -- lifecycle ----------------------------------------------------------------
    def close(self) -> None:
        if self._owns_conn:
            self.conn.close()

    def __enter__(self) -> WorkerRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- capacity gate ------------------------------------------------------------
    @contextmanager
    def _slot(self) -> Iterator[bool]:
        """MOS-EXEC-064 mechanism 1, in three lines.

        Acquired BEFORE `claim()`. When it cannot be acquired the runner does not call
        `claim()` at all: no `job_queue` row is read, no `claim_count` is incremented, and
        the job stays exactly as available as it was. That is what makes "a load-shed must
        not consume retry budget" a structural property rather than a compensating
        subtraction after the fact -- and MOS-EXEC-035 forbids the compensation anyway
        ("`claim_count` MUST be incremented in exactly one statement in the entire
        system").
        """
        acquired = self._slots.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._slots.release()

    # -- one job ------------------------------------------------------------------
    def run_once(self) -> RunOutcome | None:
        """Claim at most one job and run it to a terminal state. `None` if nothing ran."""
        with self._slot() as slot:
            if not slot:
                self.shed_count += 1
                log_json(
                    "runner.load_shed",
                    worker_id=self.config.worker_id,
                    reason="no_free_slot",
                    max_concurrent_jobs=self.config.max_concurrent_jobs,
                    # MOS-EXEC-064: no row touched, so no budget consumed.
                    consumed_retry_budget=False,
                )
                return None
            # One tenant at a time, in order. A claim under a binding sees only that
            # tenant's `job_queue` rows, because `job_queue_tenant_isolation` is applied
            # to the `SELECT ... FOR UPDATE SKIP LOCKED` like any other statement -- the
            # claim query itself contains no tenant predicate and MUST NOT (MOS-SEC-076).
            for tenant_id in self.config.tenant_ids:
                token = bind_current_tenant(tenant_id)
                try:
                    self.current_tenant_id = tenant_id
                    lease = self.queue.claim_lease(
                        self.config.worker_id, self.config.lease_seconds
                    )
                    if lease is None:
                        continue
                    return self._run_lease(lease)
                finally:
                    reset_current_tenant(token)
            return None

    def _run_lease(self, lease: Lease) -> RunOutcome:
        started = time.monotonic()
        job = repo.get_job(self.conn, lease.job_id)
        if job is None:  # pragma: no cover - the claim joined `jobs`, so this cannot happen
            raise SystemFailure(
                "claimed_job_vanished", {"job_id": lease.job_id}, "claimed a job that is gone"
            )

        # MOS-EXEC-022: a fresh plan per attempt, same `step_key` set, `attempt` set to
        # the job's current attempt. Written before the first step so `steps_total` is
        # non-zero for the whole run and a client never divides by it at 0.
        steps_total = repo.plan_steps(self.conn, lease.job_id, attempt=lease.claim_count)
        log_json(
            "job.claimed",
            job_id=lease.job_id,
            worker_id=self.config.worker_id,
            attempt=lease.claim_count,
            max_attempts=lease.max_attempts,
            steps_total=steps_total,
            study_instance_uid=job["study_instance_uid"],
            capability_ids=list(job["capability_ids"]),
            trace_id=job["trace_id"],
        )

        state: PipelineState | None = None
        with _Heartbeat(
            self.config.dsn,
            lease.job_id,
            self.config.worker_id,
            lease_seconds=self.config.lease_seconds,
            interval_s=self.config.heartbeat_seconds,
            service_id=self.config.service_id,
            tenant_id=self.current_tenant_id,
        ) as heartbeat:
            ctx = StepContext(
                conn=self.conn,
                queue=self.queue,
                job=job,
                worker_id=self.config.worker_id,
                attempt=lease.claim_count,
                deps=self.deps,
                lease=heartbeat,
            )
            try:
                state = execute_plan(ctx)
            except LeaseLost:
                # MOS-EXEC-028: no Complete, no Fail, no STOW. The job stays RUNNING and
                # the reclaimer owns it from here.
                return self._outcome(lease, "ABORTED", started, phase=self._phase(lease.job_id))
            except ClinicalRejection as exc:
                return self._reject(lease, exc, started)
            except MedosError as exc:
                return self._fail(lease, exc, started)
            except Exception as exc:  # noqa: BLE001 - last resort, classified as internal
                return self._fail(
                    lease,
                    SystemFailure(
                        "unhandled_exception",
                        {"exception": type(exc).__name__},
                        f"{type(exc).__name__}: {exc}",
                    ),
                    started,
                )

        plan = state.plan
        # The UID of an object this job WROTE, never of one it merely derived a UID for.
        # `plan_outputs` derives both UIDs whatever the bundle contained, because they are
        # a pure function of the identity tuple (MOS-IMG-062); reporting the SEG's here
        # for a job that wrote no SEG would hand a caller a SOPInstanceUID that resolves
        # to nothing in the PACS, which is the failure MOS-SAFE-085 exists to keep
        # visible.
        outcome = self._outcome(
            lease,
            "COMPLETED",
            started,
            phase="persisting",
            seg_sop_instance_uid=(
                plan.seg_sop_instance_uid if plan and plan.writes_seg else None
            ),
            sr_sop_instance_uid=(
                plan.sr_sop_instance_uid if plan and plan.writes_sr else None
            ),
            result_ids=state.result_ids,
            outputs_omitted=(
                () if plan is None else tuple(o.to_dict() for o in plan.omitted_outputs)
            ),
        )
        log_json(
            "job.completed",
            job_id=outcome.job_id,
            attempt=outcome.attempt,
            duration_ms=outcome.duration_ms,
            results=len(state.result_ids),
            seg_sop_instance_uid=outcome.seg_sop_instance_uid,
            sr_sop_instance_uid=outcome.sr_sop_instance_uid,
            outputs_omitted=[o["reason_code"] for o in outcome.outputs_omitted],
            reused_existing_objects=state.reused_existing_objects,
        )
        return outcome

    # -- terminal transitions ------------------------------------------------------
    def _reject(self, lease: Lease, exc: ClinicalRejection, started: float) -> RunOutcome:
        """T7. CONTRACT.md section 3: a clinical outcome, with a machine-readable reason.

        MOS-EXEC-015: a rejected job "MUST NOT be retried, MUST NOT consume retry budget,
        MUST NOT be dead-lettered, and MUST NOT raise an on-call alert". `reject()` is a
        single fenced transaction that sets the reason and deletes the queue row, so none
        of those four can happen afterwards by accident.

        `jobs.reject_reason_code` takes chapter 5's closed enum; the FINE code
        (`geometry_gantry_tilt`, `no_eligible_series`, ...) is already on the failing
        `job_steps` row and in its `job.step_changed` event, written by `execute_plan`.
        Both resolutions survive.
        """
        step_key = self._failed_step(lease.job_id)
        code = reject_reason_code(exc, step_key)
        phase = self._phase(lease.job_id)
        self.queue.reject(
            lease.job_id,
            self.config.worker_id,
            reason_code=code,
            reason_detail=exc.message[:2000],
        )
        log_json(
            "job.rejected",
            job_id=lease.job_id,
            attempt=lease.claim_count,
            reject_reason_code=code,
            detail_code=exc.reason_code,
            detail=exc.detail,
            step_key=step_key,
            phase=phase,
            # Stated in the log line because it is the single most misread fact about this
            # state (MOS-EXEC-014): this is an answer, not an incident.
            clinical=True,
        )
        return self._outcome(
            lease,
            "REJECTED",
            started,
            phase=phase,
            reject_reason_code=code,
            reject_detail_code=exc.reason_code,
        )

    def _fail(self, lease: Lease, exc: MedosError, started: float) -> RunOutcome:
        """T9 (retry with backoff) or T11 (terminal FAILED). `fail_ex` decides which.

        MOS-EXEC-030: "`Fail` decides the next state ... and returns the decision to the
        caller. The caller MUST NOT make that decision itself." So this method supplies
        the class and the retryability and reads the answer back; it never branches on
        `attempt < max_attempts` itself.
        """
        phase = self._phase(lease.job_id)
        step_key = self._failed_step(lease.job_id)
        failure_class, failure_code = failure_class_for(exc, step_key)
        decision = self.queue.fail_ex(
            lease.job_id,
            self.config.worker_id,
            retryable=exc.retryable,
            failure_class=failure_class,
            failure_code=failure_code,
            failure_detail=exc.message[:2000],
        )
        log_json(
            "job.failed" if decision.next_state == "FAILED" else "job.retry_scheduled",
            job_id=lease.job_id,
            attempt=lease.claim_count,
            max_attempts=lease.max_attempts,
            next_state=decision.next_state,
            failure_class=failure_class,
            failure_code=failure_code,
            step_key=step_key,
            phase=phase,
            retry_at=decision.retry_at,
        )
        return self._outcome(
            lease,
            "FAILED" if decision.next_state == "FAILED" else "QUEUED",
            started,
            phase=phase,
            failure_class=failure_class,
            failure_code=failure_code,
            retry_at=decision.retry_at,
        )

    # -- loops --------------------------------------------------------------------
    def run_forever(
        self,
        *,
        max_jobs: int | None = None,
        stop: threading.Event | None = None,
        idle_limit: int | None = None,
    ) -> list[RunOutcome]:
        """Poll, claim, run. Returns every outcome, so a caller can assert on a batch.

        MOS-EXEC-037 makes polling the correctness path: `LISTEN`/`NOTIFY` is a latency
        optimisation and the timer is what guarantees a job is eventually picked up. There
        is no NOTIFY consumer in this slice, so the timer is all there is, and that is
        sufficient rather than a shortcut.
        """
        stop = stop or threading.Event()
        outcomes: list[RunOutcome] = []
        idle = 0
        while not stop.is_set():
            if self.config.reclaim_on_poll:
                self.reclaim_expired()
            outcome = self.run_once()
            if outcome is None:
                idle += 1
                if idle_limit is not None and idle >= idle_limit:
                    return outcomes
                stop.wait(self.config.poll_interval_s)
                continue
            idle = 0
            outcomes.append(outcome)
            if max_jobs is not None and len(outcomes) >= max_jobs:
                return outcomes
        return outcomes

    def reclaim_expired(self, *, limit: int = 100) -> list[Reclaimed]:
        """Run the lease janitor (chapter 5 section 5.6.4).

        Co-located with the runner rather than deployed as its own process because in this
        slice there is one process; it is a separate METHOD so that weeks 3-5 can lift it
        into a janitor without touching the claim loop. Every worker running it is safe:
        `FOR UPDATE ... SKIP LOCKED` inside `reclaim_expired` means N reclaimers never
        fight over one row.
        """
        # MOS-SEC-078 again: the reclaimer is a background sweep, so it iterates tenants
        # rather than holding a cross-tenant connection. An expired lease in tenant B is
        # invisible while bound to tenant A, which is the correct and the intended
        # consequence of forced row security on `job_queue`.
        reclaimed: list[Reclaimed] = []
        for tenant_id in self.config.tenant_ids:
            with tenant_context(tenant_id):
                reclaimed.extend(self.queue.reclaim_expired(limit=limit))
        for row in reclaimed:
            log_json(
                "queue.reclaimed",
                job_id=row.job_id,
                outcome=row.outcome,
                claim_count=row.claim_count,
                previous_owner=row.previous_owner,
                available_at=row.available_at,
            )
        return reclaimed

    # -- internals ----------------------------------------------------------------
    def _phase(self, job_id: str) -> str | None:
        row = repo.get_job(self.conn, job_id)
        return None if row is None else str(row["phase"])

    def _failed_step(self, job_id: str) -> str | None:
        """The one query in the runner that reads a table directly rather than via `repo`.

        It goes through `tenant_tx` like everything else. It did not, for one commit, and
        the consequence is worth recording because it is the failure mode this whole
        design is built to produce: `_reject()` calls this FIRST, so a rejection -- the
        clinical outcome, the one MOS-EXEC-014 insists must not look like an error --
        raised 42704 on an unbound autocommit connection, the job stayed RUNNING until its
        600-second lease expired, and the reclaimer requeued it to fail the same way.

        Nothing leaked. That is the point: an access path that forgets the chokepoint
        fails loudly and immediately instead of quietly returning another tenant's rows,
        or this tenant's rows to another tenant (MOS-STORE-224).
        """
        uid = repo.get_job_uuid(self.conn, job_id)
        if uid is None:  # pragma: no cover
            return None
        with tenant_tx(self.conn):
            row = self.conn.execute(
                "SELECT step_key FROM job_steps WHERE job_id = %s AND status = 'failed'"
                " ORDER BY step_index LIMIT 1",
                (uid,),
            ).fetchone()
        return None if row is None else str(row["step_key"])

    def _outcome(
        self,
        lease: Lease,
        terminal_state: TerminalState,
        started: float,
        **fields: Any,
    ) -> RunOutcome:
        return RunOutcome(
            job_id=lease.job_id,
            attempt=lease.claim_count,
            terminal_state=terminal_state,
            duration_ms=int((time.monotonic() - started) * 1000),
            **fields,
        )


# =====================================================================================
# CLI
# =====================================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m medos.worker.runner",
        description="MedicalOS weeks 1-2 job runner: claim, execute, complete.",
    )
    p.add_argument("--dsn", default=None, help="Postgres DSN (default: MEDOS_DATABASE_URL)")
    p.add_argument("--dicomweb-url", default=DEFAULT_ORTHANC_URL)
    p.add_argument("--work-root", default=None)
    p.add_argument("--service-id", default=DEFAULT_SERVICE_ID)
    p.add_argument("--worker-id", default=None)
    p.add_argument("--lease-seconds", type=int, default=120)
    p.add_argument("--heartbeat-seconds", type=int, default=20)
    p.add_argument("--poll-interval", type=float, default=1.0)
    p.add_argument("--max-jobs", type=int, default=None)
    p.add_argument("--idle-limit", type=int, default=None,
                   help="exit after N consecutive empty polls")
    p.add_argument("--once", action="store_true", help="claim at most one job and exit")
    p.add_argument("--reclaim-only", action="store_true",
                   help="run the lease janitor once and exit")
    p.add_argument("--json", action="store_true", help="print run outcomes as JSON on stdout")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = RunnerConfig(
        dsn=args.dsn or dsn_from_env(),
        dicomweb_url=args.dicomweb_url,
        work_root=Path(args.work_root) if args.work_root else RunnerConfig().work_root,
        service_id=args.service_id,
        worker_id=args.worker_id or make_worker_id("runner"),
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        poll_interval_s=args.poll_interval,
    )
    with WorkerRunner(config) as runner:
        if args.reclaim_only:
            reclaimed = runner.reclaim_expired()
            if args.json:
                print(json.dumps([r.__dict__ for r in reclaimed], default=str))
            return 0
        if args.once:
            outcomes = [o for o in (runner.run_once(),) if o is not None]
        else:
            outcomes = runner.run_forever(max_jobs=args.max_jobs, idle_limit=args.idle_limit)
        if args.json:
            print(json.dumps([o.__dict__ for o in outcomes], default=str))
        # Exit 0 whenever the runner itself behaved. A REJECTED or FAILED job is a
        # recorded outcome, not a runner error: exiting non-zero would make a supervisor
        # restart a healthy worker every time a study turned out to be unsuitable.
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
