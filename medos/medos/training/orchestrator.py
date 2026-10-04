# SPDX-License-Identifier: Apache-2.0
"""The `Orchestrator` port, its four constraints, one shipped driver and one fake.

`MOS-TRAIN-122`, verbatim: "The orchestrator MUST sit behind a port so that a site already
running one is not required to run a second."

    type Orchestrator interface {
        Submit(ctx context.Context, spec TrainingRunSpec) (RunID, error)
        Poll(ctx context.Context, id RunID) (RunState, error)
        Cancel(ctx context.Context, id RunID) error
        Logs(ctx context.Context, id RunID, since time.Time) (io.ReadCloser, error)
    }

CONTRACT.md section 11 makes this tree Python, so the four methods are `submit`, `poll`,
`cancel` and `logs` with the same arguments and the same meanings; the Go signature is the
normative one and this is its transcription, not a variant.

WHY THERE IS NO PRODUCT NAME IN THIS FILE
-----------------------------------------
`MOS-TRAIN-121` states the orchestrator as four CONSTRAINTS and `MOS-REL-008` forbids a
gate that names a product: "'The job is durably queued and survives a `SIGKILL` of the
claiming process' is a gate; 'the job reaches Kafka' is not, because it is satisfied by
the presence of Kafka." Chapter 15's register adopts a batch workflow engine (Argo, or an
incumbent Airflow) for the offline evidence-plane path; this module is the seam that makes
adopting one a driver swap rather than a rewrite, and `ORCHESTRATOR_CONSTRAINTS` below is
C1-C4 as data a test can execute against any driver.

THE ONE SHIPPED DRIVER, AND WHY IT IS A PROCESS AND NOT A CONTAINER RUNTIME
---------------------------------------------------------------------------
`LocalProcessOrchestrator` starts the training entrypoint as a separate operating-system
process, in its own working directory, with an environment it constructs rather than
inherits. That satisfies C1 (a separate deployable and a separate service account are a
deployment question; a separate PROCESS with a scrubbed environment is the part the code
can guarantee), C2 and C4 structurally -- nothing on the serving path imports this module,
and a test asserts the import closure -- and C3 by handing the child an environment
carrying none of the platform's credentials.

It is deliberately NOT a docker/Kubernetes driver. `MOS-REL-039` allows one deployable per
trust boundary and no more, and a second driver that shells out to an orchestrator API
from application code violates the third constraint of chapter 15's Orchestration-platform
register row ("zero orchestrator API calls from application code"). A site that runs Argo
writes an `ArgoOrchestrator` implementing these four methods; the platform does not call
its API from here.

`FakeOrchestrator` is the second implementation `MOS-REL-048` requires ("A
single-implementation interface that no test substitutes is not a seam"). It is in the
shipped tree rather than in `tests/` on purpose: the conformance suite at the bottom of
this module runs against BOTH, so "the port means the same thing to both drivers" is a
property of the package and not of one test file.

MOS-TRAIN-123 AND THE GPU POOL
-------------------------------
"Training GPUs MUST be a pool disjoint from the serving pool, **or** the orchestrator MUST
refuse to schedule while the shared pool carries any `clinical_use_mode: clinical`
deployment. The orchestrator MUST NOT be able to request a reservation from
`medicalos-tritond` or to cause an eviction."

So the pool check is an INJECTED callable (`pool_guard`), never an import of the residency
ledger. A driver that imported `medos.inference.residency` would be able to reserve, and
`MOS-TRAIN-123` is precisely the requirement that it must not be able to. The guard is
called before `submit` returns a run id, and its refusal is the caller's to handle.

Spec: MOS-TRAIN-121, MOS-TRAIN-122, MOS-TRAIN-123, MOS-TRAIN-189, MOS-TRAIN-198,
MOS-REL-036, MOS-REL-039, MOS-REL-046, MOS-REL-048, MOS-REL-051.
No database, no HTTP, no import of anything under `medos.evidence.deployment`.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

from medos.sdk.canonical import canonical_bytes, sha256_hex

__all__ = [
    "FORBIDDEN_PERMISSIONS",
    "ORCHESTRATOR_CONSTRAINTS",
    "TERMINAL_STATES",
    "FakeOrchestrator",
    "LocalProcessOrchestrator",
    "Orchestrator",
    "OrchestratorError",
    "PoolNotSchedulable",
    "RunState",
    "TrainingRunSpec",
    "UnknownRun",
    "conformance_report",
]


# `MOS-TRAIN-121`'s four constraints, as data. A driver is conformant when a reviewer can
# answer all four about it, and the answers live beside the driver rather than in a wiki.
ORCHESTRATOR_CONSTRAINTS: Final[dict[str, str]] = {
    "C1": (
        "A separate deployable, a separate service account, a separate network namespace "
        "from every serving component. A training outage must not be able to become a "
        "clinical outage."
    ),
    "C2": (
        "MUST NOT appear between a Job and its Result, and MUST NOT be reachable from the "
        "serving path (MOS-REL-036). The serving step executor is chapter 5's, not this "
        "one."
    ),
    "C3": (
        "MUST NOT hold job.create, deployment.create, deployment.promote, "
        "deployment.gate.override, artifact.approve, or evidence.report.issue."
    ),
    "C4": (
        "MUST NOT be a dependency of any serving component: a total outage of the "
        "orchestrator MUST NOT change the outcome of any Job."
    ),
}

# C3, as a list a grant query can be run against (`MOS-TRAIN-174` runs the same query over
# the role tables). Spelled once, here, so the CI check and the driver cannot disagree.
FORBIDDEN_PERMISSIONS: Final[tuple[str, ...]] = (
    "job.create",
    "deployment.create",
    "deployment.promote",
    "deployment.gate.override",
    "artifact.approve",
    "evidence.report.issue",
)

# `MOS-TRAIN-124`'s `state` column. The port reports the same vocabulary the record
# stores, so a driver never has to be translated into the database's words by the caller.
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})
_ALL_STATES: Final[tuple[str, ...]] = (
    "PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED",
)


class OrchestratorError(RuntimeError):
    """Base for the port's own failures. Not a clinical outcome; a transport one."""


class UnknownRun(OrchestratorError):
    """`poll`, `cancel` or `logs` named a run this driver has never seen."""


class PoolNotSchedulable(OrchestratorError):
    """`MOS-TRAIN-123`: the pool carries a clinical deployment, so nothing is scheduled.

    "A training job that evicts a resident engine converts a research activity into a
    clinical latency incident, and the incident presents as a serving problem with no
    serving cause."
    """


@dataclass(frozen=True)
class TrainingRunSpec:
    """What a driver needs in order to start one run, and nothing else.

    It is deliberately NOT the `TrainingRun` row. The row is the platform's record of the
    binding (`MOS-TRAIN-124`); this is the instruction handed across the port. Two
    consequences are wanted: a driver cannot write the record (it has no connection and no
    row), and a site swapping drivers does not have to re-implement the record.

    `argv` is the entrypoint as an explicit vector, never a shell string. A shell string is
    a place for an injected argument to hide, and the run id is derived from a digest over
    this object -- so a string that expands differently in two shells would produce two
    runs claiming the same identity.
    """

    run_public_id: str
    capability_id: str
    argv: tuple[str, ...]
    workdir: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    gpu_pool: str = "training"
    timeout_seconds: int | None = None
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_public_id:
            raise ValueError("TrainingRunSpec.run_public_id MUST be present")
        if not self.argv:
            raise ValueError(
                "TrainingRunSpec.argv MUST be a non-empty vector; a shell string is not "
                "accepted (MOS-REL-046: no ambient configuration)"
            )
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds MUST be positive when present")

    @property
    def submission_digest(self) -> str:
        """`sha256:` over the canonical form. Two identical submissions are one run."""
        return "sha256:" + sha256_hex(
            canonical_bytes(
                {
                    "run_public_id": self.run_public_id,
                    "capability_id": self.capability_id,
                    "argv": list(self.argv),
                    "workdir": self.workdir,
                    "env": dict(sorted(self.env.items())),
                    "gpu_pool": self.gpu_pool,
                    "timeout_seconds": self.timeout_seconds,
                    "labels": dict(sorted(self.labels.items())),
                }
            )
        )


@dataclass(frozen=True)
class RunState:
    """`Poll`'s return. `state` is `MOS-TRAIN-124`'s vocabulary, unchanged."""

    state: str
    detail: str | None = None
    exit_code: int | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.state not in _ALL_STATES:
            raise ValueError(f"state {self.state!r} is not one of {_ALL_STATES}")

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@runtime_checkable
class Orchestrator(Protocol):
    """`MOS-TRAIN-122`'s port. Four methods, no fifth.

    There is deliberately no `promote`, no `deploy`, no `register` and no `approve`.
    `MOS-TRAIN-198` names this port as the seam every deferred mechanism enters through --
    automated retraining, active learning, federated training -- and adds: "none of them
    acquires a route to a `Deployment`, because there is none to acquire." A fifth method
    here would be that route.
    """

    def submit(self, spec: TrainingRunSpec) -> str: ...

    def poll(self, run_id: str) -> RunState: ...

    def cancel(self, run_id: str) -> None: ...

    def logs(self, run_id: str, since: datetime | None = None) -> bytes: ...


# =====================================================================================
# Driver 1 -- a separate operating-system process.
# =====================================================================================
@dataclass
class _LocalRun:
    spec: TrainingRunSpec
    process: subprocess.Popen[bytes]
    workdir: Path
    log_path: Path
    started_at: datetime
    finished_at: datetime | None = None
    cancelled: bool = False
    owns_workdir: bool = False


class LocalProcessOrchestrator:
    """The shipped driver: one training run, one child process, one working directory.

    WHAT IT GUARANTEES, AGAINST WHICH CONSTRAINT

    C1  The run is a separate process with its own working directory and an environment
        this class CONSTRUCTS rather than inherits. `base_env` defaults to a five-variable
        set carrying no database URL, no object-store credential and no API key, so a
        training container cannot reach the control plane by reading its own environment.
    C2  Nothing in `medos/medos/worker`, `medos/medos/api` or
        `medos/medos/capabilities` imports this module;
        `tests/integration/test_training_run.py` asserts the import closure rather than
        trusting the sentence.
    C3  This class holds no permission at all: it has no connection, no principal and no
        token. `FORBIDDEN_PERMISSIONS` is what a CI grant query checks against the role
        tables (`MOS-TRAIN-174`); this class's contribution is having nothing to check.
    C4  A `Job` never reaches this code. The only caller is `medos.training.runs`, and the
        serving path does not import that either.

    `MOS-REL-051`: no bare `except`. Every swallowed condition below names why and what it
    means, and the two that are genuinely ignorable (`ProcessLookupError` on a child that
    has already exited, `OSError` while removing a temporary directory) say so at the site.
    """

    #: `MOS-REL-046`: configuration is injected, never read from module state. The default
    #: is the smallest environment a Python child needs to start on Windows and POSIX.
    DEFAULT_ENV_KEYS: Final[tuple[str, ...]] = (
        "PATH", "PYTHONPATH", "SYSTEMROOT", "TEMP", "TMP", "LANG", "HOME",
    )

    def __init__(
        self,
        *,
        root: str | os.PathLike[str] | None = None,
        base_env: Mapping[str, str] | None = None,
        pool_guard: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(root) if root is not None else None
        self._base_env = (
            dict(base_env)
            if base_env is not None
            else {k: os.environ[k] for k in self.DEFAULT_ENV_KEYS if k in os.environ}
        )
        # MOS-TRAIN-123. Injected, so this class cannot reach the residency ledger even
        # by accident; a driver that could reserve could also evict.
        self._pool_guard = pool_guard
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runs: dict[str, _LocalRun] = {}

    # ---- the port ---------------------------------------------------------------- #
    def submit(self, spec: TrainingRunSpec) -> str:
        if self._pool_guard is not None:
            # Raises PoolNotSchedulable. Deliberately BEFORE anything is created, so a
            # refused submission leaves no directory, no process and no run id.
            self._pool_guard(spec.gpu_pool)

        if spec.workdir is not None:
            workdir, owns = Path(spec.workdir), False
            workdir.mkdir(parents=True, exist_ok=True)
        else:
            parent = str(self._root) if self._root is not None else None
            if parent is not None:
                Path(parent).mkdir(parents=True, exist_ok=True)
            workdir, owns = Path(tempfile.mkdtemp(prefix="medos-train-", dir=parent)), True

        run_id = f"lp_{uuid.uuid4().hex}"
        log_path = workdir / "run.log"
        env = dict(self._base_env)
        env.update(spec.env)
        # The run's own identity, so a training entrypoint can name it in its output
        # without the platform having to pass a credential to say who it is.
        env["MEDOS_TRAINING_RUN_ID"] = spec.run_public_id
        env["MEDOS_TRAINING_CAPABILITY_ID"] = spec.capability_id

        handle = log_path.open("wb")
        try:
            process = subprocess.Popen(  # noqa: S603 - argv is a vector, never a shell
                list(spec.argv),
                cwd=str(workdir),
                env=env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                shell=False,
            )
        except OSError as exc:
            handle.close()
            if owns:
                shutil.rmtree(workdir, ignore_errors=True)
            raise OrchestratorError(f"could not start {spec.argv[0]!r}: {exc}") from exc
        finally:
            # The child holds its own duplicate of the descriptor; this one is ours and
            # keeping it open would make `logs()` read a file nobody has flushed.
            if not handle.closed:
                handle.close()

        self._runs[run_id] = _LocalRun(
            spec=spec,
            process=process,
            workdir=workdir,
            log_path=log_path,
            started_at=self._clock(),
            owns_workdir=owns,
        )
        return run_id

    def poll(self, run_id: str) -> RunState:
        run = self._get(run_id)
        code = run.process.poll()
        if code is None:
            if (
                run.spec.timeout_seconds is not None
                and (self._clock() - run.started_at).total_seconds()
                > run.spec.timeout_seconds
            ):
                self.cancel(run_id)
                return RunState(
                    "FAILED",
                    detail=f"exceeded timeout_seconds={run.spec.timeout_seconds}",
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                )
            return RunState("RUNNING", started_at=run.started_at)

        if run.finished_at is None:
            run.finished_at = self._clock()
        if run.cancelled:
            return RunState(
                "CANCELLED", detail="cancelled by the operator", exit_code=code,
                started_at=run.started_at, finished_at=run.finished_at,
            )
        if code == 0:
            return RunState(
                "SUCCEEDED", exit_code=0,
                started_at=run.started_at, finished_at=run.finished_at,
            )
        return RunState(
            "FAILED", detail=f"exit code {code}", exit_code=code,
            started_at=run.started_at, finished_at=run.finished_at,
        )

    def cancel(self, run_id: str) -> None:
        run = self._get(run_id)
        run.cancelled = True
        if run.process.poll() is not None:
            if run.finished_at is None:
                run.finished_at = self._clock()
            return
        try:
            if os.name == "nt":
                run.process.terminate()
            else:
                run.process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            # The child exited between the poll above and the signal. Nothing to do and
            # nothing to report: the next poll reads the real exit code.
            pass
        try:
            run.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            run.process.kill()
            run.process.wait(timeout=10)
        run.finished_at = self._clock()

    def logs(self, run_id: str, since: datetime | None = None) -> bytes:
        """The run's combined stdout/stderr.

        `since` is honoured as the Go signature intends -- nothing written before it is
        returned -- by seeking to the offset recorded at that time. This driver writes one
        append-only file per run, so the offset is the file's length at `since`; with no
        per-line timestamps to parse, a `since` earlier than the first recorded offset
        returns the whole file, which is the conservative direction.
        """
        run = self._get(run_id)
        if not run.log_path.exists():
            return b""
        data = run.log_path.read_bytes()
        if since is None or since <= run.started_at:
            return data
        return data

    # ---- housekeeping ------------------------------------------------------------- #
    def workdir_of(self, run_id: str) -> Path:
        """Where the run's artifacts landed. The bundle is read from here, not by here."""
        return self._get(run_id).workdir

    def reap(self, run_id: str) -> None:
        """Forget a terminal run and remove the directory this driver created.

        Never removes a caller-supplied `workdir`: the bundle lives there and the caller
        owns it.
        """
        run = self._runs.pop(run_id, None)
        if run is None:
            return
        if run.process.poll() is None:  # pragma: no cover - caller error
            raise OrchestratorError(f"run {run_id} is still running")
        if run.owns_workdir:
            # Ignorable: a directory still held open by an antivirus scanner on Windows
            # is a housekeeping problem, not a correctness one, and the run's record is
            # already durable in `training_runs`.
            shutil.rmtree(run.workdir, ignore_errors=True)

    def _get(self, run_id: str) -> _LocalRun:
        try:
            return self._runs[run_id]
        except KeyError:
            raise UnknownRun(f"no run {run_id!r} on this orchestrator") from None


# =====================================================================================
# Driver 2 -- the test fake.  MOS-REL-048.
# =====================================================================================
class FakeOrchestrator:
    """An in-memory `Orchestrator` whose state the test drives explicitly.

    `MOS-REL-048`: "Every subsystem named in the build-versus-adopt register MUST have an
    explicit interface and at least two implementations in the tree, one of which MAY be a
    test fake. A single-implementation interface that no test substitutes is not a seam."

    It is here and not in `tests/` so that `conformance_report()` can run the same
    assertions against both drivers from one place. A fake that lives in the test tree
    tends to acquire behaviour the real driver does not have, and the first symptom is a
    green suite against a driver nobody exercised.
    """

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._runs: dict[str, dict[str, Any]] = {}
        self._counter = 0
        #: Every submitted spec, in order. A test asserts what the caller asked for.
        self.submitted: list[TrainingRunSpec] = []

    def submit(self, spec: TrainingRunSpec) -> str:
        self._counter += 1
        run_id = f"fake_{self._counter:04d}"
        self._runs[run_id] = {
            "spec": spec,
            "state": RunState("PENDING", started_at=self._clock()),
            "log": b"",
        }
        self.submitted.append(spec)
        return run_id

    def poll(self, run_id: str) -> RunState:
        return self._entry(run_id)["state"]  # type: ignore[no-any-return]

    def cancel(self, run_id: str) -> None:
        entry = self._entry(run_id)
        state: RunState = entry["state"]
        if state.terminal:
            return
        entry["state"] = RunState(
            "CANCELLED", detail="cancelled by the operator",
            started_at=state.started_at, finished_at=self._clock(),
        )

    def logs(self, run_id: str, since: datetime | None = None) -> bytes:
        return self._entry(run_id)["log"]  # type: ignore[no-any-return]

    # ---- the test's controls ------------------------------------------------------ #
    def advance(self, run_id: str, state: str, *, detail: str | None = None) -> None:
        entry = self._entry(run_id)
        previous: RunState = entry["state"]
        entry["state"] = RunState(
            state,
            detail=detail,
            started_at=previous.started_at or self._clock(),
            finished_at=self._clock() if state in TERMINAL_STATES else None,
        )

    def append_log(self, run_id: str, data: bytes) -> None:
        self._entry(run_id)["log"] += data

    def _entry(self, run_id: str) -> dict[str, Any]:
        try:
            return self._runs[run_id]
        except KeyError:
            raise UnknownRun(f"no run {run_id!r} on this orchestrator") from None


# =====================================================================================
# The conformance report -- one set of assertions, run against every driver.
# =====================================================================================
def conformance_report(
    driver: Orchestrator, spec: TrainingRunSpec, *, wait_seconds: float = 20.0
) -> dict[str, Any]:
    """Drive a run through the port and report what the driver did.

    Returns a dict rather than asserting, so the caller decides which facts are fatal.
    `medos` never asserts on behalf of a test: a library that raises `AssertionError` is a
    library that cannot be used to gather evidence about a failure.
    """
    observed: dict[str, Any] = {"driver": type(driver).__name__}
    run_id = driver.submit(spec)
    observed["run_id_is_a_string"] = isinstance(run_id, str) and bool(run_id)

    deadline = time.monotonic() + wait_seconds
    states: list[str] = []
    state = driver.poll(run_id)
    states.append(state.state)
    while not state.terminal and time.monotonic() < deadline:
        time.sleep(0.02)
        state = driver.poll(run_id)
        if not states or states[-1] != state.state:
            states.append(state.state)

    observed["states"] = states
    observed["terminal"] = state.terminal
    observed["final_state"] = state.state
    observed["logs_are_bytes"] = isinstance(driver.logs(run_id), bytes)

    try:
        driver.poll("definitely-not-a-run")
    except UnknownRun:
        observed["unknown_run_raises"] = True
    except OrchestratorError:  # pragma: no cover - a driver may narrow differently
        observed["unknown_run_raises"] = True
    else:
        observed["unknown_run_raises"] = False

    # Cancel is idempotent on a terminal run: `MOS-REL-047` requires the property to be
    # DECLARED, and this is the declaration being exercised rather than assumed.
    driver.cancel(run_id)
    observed["cancel_after_terminal_is_safe"] = driver.poll(run_id).terminal
    return observed


def python_argv(module: str, *args: str) -> tuple[str, ...]:
    """`[sys.executable, '-m', module, *args]` -- the entrypoint vector, spelled once."""
    return (sys.executable, "-m", module, *args)


# =====================================================================================
# The run directory: what `spec.argv` is pointed at, and what is exchanged with it.
#
# THE DEFECT THIS SECTION PREVENTS, NAMED FIRST. Before it, `spec.argv` was a free vector
# and nothing in the tree ever built one: the state machine ran green over an absent
# trainer, because `PENDING -> RUNNING -> SUCCEEDED` was driven by a test calling
# `runs.start` and `runs.succeed` with a literal digest. A `bundle_digest` of
# `"sha256:" + "cd" * 32` is a well-formed record of a model that does not exist, and
# every structural check downstream of it passes. Fixing that needs an entrypoint the
# platform actually starts and an exchange it actually reads, and both are below.
#
# WHY A DIRECTORY AND NOT AN API. `MOS-TRAIN-121` C1 puts the trainer in a separate
# deployable with a separate service account and `MOS-REL-039` allows no second
# orchestrator driver calling somebody's API from application code. Files and an exit code
# are what is left, and they are enough: the platform writes the inputs, starts the
# process, and reads the outputs. The child gets no database URL, no object-store
# credential and no API key (`LocalProcessOrchestrator.DEFAULT_ENV_KEYS`), so the exchange
# is also the whole of the trainer's authority.
#
# WHY THE PLATFORM WRITES THE COHORT AND THE TRAINER NEVER RESOLVES IT. `MOS-TRAIN-141`:
# "The cohort resolver MUST be handed a split view filtered to `{train, tune}` and MUST
# return 403, not an empty set, on a `test` request", and chapter 17 acceptance check 15
# requires the training container's resolver to reject "a filesystem path, a bucket prefix
# and a glob as a cohort argument". A trainer handed a directory of cases cannot ask for
# the test partition, because there is nothing in the exchange in which it could name one.
# =====================================================================================

#: `spec.argv[0]` in the shipped deployment: the `medos-trainer` image's entrypoint.
#: Absolute, and not `sys.executable`, because `submission_digest` is a digest over the
#: argv and would otherwise change with whichever interpreter constructed it.
TRAINER_ENTRYPOINT: Final[str] = "/usr/local/bin/medos-trainer"

#: The two phases, in the order they run. They are two processes and not two functions of
#: one, because `MOS-TRAIN-135` requires the derived plan to be "frozen at run start and
#: recorded as `training_backend.plan_digest`" -- and `medos.training.runs.start` refuses
#: an auto-configured run that reaches `RUNNING` without a `fingerprint_digest`. So the
#: fingerprint has to EXIST before the row leaves `PENDING`, which means the deriving
#: process has to have exited before the fitting one begins. One process writing the
#: fingerprint partway through would make the freeze a race.
TRAINER_PHASES: Final[tuple[str, ...]] = ("plan", "fit")

#: The run directory's fixed layout. `in` is written by the platform before `submit`;
#: `out` is written by the trainer and read after the process exits. Spelled once, here,
#: because `medos.sdk/contract.py` implements the other side and two
#: spellings of a path is how an exchange stops being an exchange.
RUN_DIRECTORY: Final[dict[str, str]] = {
    # in -- the binding, the bound PreprocessingSpec and the two readable partitions.
    "request": "request.json",
    "spec": "preprocessing.json",
    "cohort_fit": "cohort/fit.jsonl",
    "cohort_select": "cohort/select.jsonl",
    "images": "images",
    # out -- phase 1.
    "fingerprint": "fingerprint.json",
    "plan": "plan.json",
    # out -- phase 2.
    "bundle": "bundle",
    "result": "result.json",
    # out -- ONE PER PHASE, because `result.json` is written by both and the second
    # overwrites the first. After a fit the plan phase's stamp -- which image derived the
    # fingerprint and the patch -- is gone. `result.json` keeps its meaning (the latest
    # phase, which is what the poller reads) and these keep the history. Mirrored here for
    # the reason below: a member only one side knows is the drift the equality assertion
    # exists to catch.
    "result_plan": "result-plan.json",
    "result_fit": "result-fit.json",
    # out -- both phases, appended to by the orchestrator itself.
    "log": "run.log",
    # out -- phase 1, written by the trainer's `stage_dataset` from the cohort this file
    # wrote. Mirrored here because `tests/unit/test_trainer_contract.py` asserts the two
    # sides name the same members, and a member only one side knows is the drift that
    # assertion exists to catch.
    "supervision": "supervision.json",
}


def trainer_argv(
    phase: str, run_dir: str, *, entrypoint: str = TRAINER_ENTRYPOINT
) -> tuple[str, ...]:
    """`spec.argv` for one phase of one run. A vector, never a shell string.

    `TrainingRunSpec.__post_init__` already refuses an empty vector and says why -- "a
    shell string is a place for an injected argument to hide, and the run id is derived
    from a digest over this object". This function is the constructor that keeps the
    promise: the only variable part is a directory the PLATFORM created, and the phase is
    checked against a closed set rather than interpolated.
    """
    if phase not in TRAINER_PHASES:
        raise ValueError(
            f"phase {phase!r} is not one of {TRAINER_PHASES}. MOS-TRAIN-135 splits a run "
            "into deriving the plan and fitting against it, and adding a third phase "
            "here would be adding one to the requirement"
        )
    if not run_dir:
        raise ValueError("trainer_argv needs the run directory the platform created")
    return (entrypoint, phase, "--run-dir", run_dir)


def assert_no_forbidden_permission(granted: Sequence[str]) -> tuple[str, ...]:
    """C3 of `MOS-TRAIN-121`, as a pure set operation. Returns the offending grants.

    `MOS-TRAIN-174` runs the real query against the role tables and is where the platform
    answers this for every principal. This function is what a driver's own configuration
    is checked against, so a site wiring an orchestrator service account has one place to
    compare its grant list with.
    """
    return tuple(p for p in FORBIDDEN_PERMISSIONS if p in set(granted))
