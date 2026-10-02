# SPDX-License-Identifier: Apache-2.0
"""The release gates' world: the deployment for 0.1.0, a throwaway schema for 0.2.0 and
0.3.0, and for three of 0.3.0's checks no database at all.

THREE RELEASES LIVE IN THIS PACKAGE, AND THEY HAVE DIFFERENT SUBJECTS
----------------------------------------------------------------------
§15.1.2 now names three gate rows this repository implements: five checks for 0.1.0, six
for 0.2.0 and TEN for 0.3.0 -- the eight the spec names, plus `migration-drift` and
`capability-reachable`, both declared and argued in `tests/unit/test_gate_contract.py`'s
`LOCAL_EXTRA`. All three are run as
`pytest -m gate_0_X_0 --require-stack`, all three refuse a partial selection, and all three
resolve a check name to `tests/gate/test_<name with underscores>.py`. What differs is the
subject.

0.3.0's row has FOUR subjects, and that is a property of what the release contains rather
than untidiness. `zero-core-change` and `no-auto-promote` are claims about the SOURCE TREE
(a git diff; an import closure), and no deployment can answer them. `chain-equivalence` is
a claim about a pure function. `sealed-mode-isolation` and `migration-drift` are claims
about the RUNNING deployment, and only it will do -- `migration-drift` exists precisely
because every other schema-shaped check builds its own database and therefore cannot see
the deployed one. The remaining four need a schema and get a throwaway (`platform_dsn`).

0.1.0's subject is the running `medos/deploy/compose` deployment -- see the section below. The
0.2.0 checks cannot have that subject, and the reason is in the specification rather than
in convenience: `MOS-EVID-006` puts the whole evidence plane outside the serving path, so
there is no HTTP surface to drive, no queue to enqueue on and no worker that runs an
`EvaluationRun`. `tests/gate/_evidence.py` documents what they run against instead (a
brand-new database, the shipped migrations applied from empty, on the deployment's own
Postgres server) and why that is a stronger claim rather than a weaker one.

The consequence for this file is one rule: NOTHING BELONGING TO THE 0.1.0 WORLD MAY RUN
WHEN ONLY 0.2.0 WAS ASKED FOR. `clean_slate` in particular truncates the deployment's job
tables, and a `-m gate_0_2_0` run that did that would destroy another agent's work to
prove something about a table it never touches.

WHAT THE 0.1.0 GATE RUNS AGAINST, AND WHY IT IS NOT THE INTEGRATION SUITE
--------------------------------------------------------------------
`docs/spec/15-delivery.md` §15.1.2 gates the TAG, and `MOS-REL-001` says a release is
container images referenced by digest. So the only honest subject for these five checks is
the running `medos/deploy/compose` deployment: the images, the schema bootstrap, the DSNs, the
compose network, the Gateway's principals file. Nothing here imports `WorkerRunner` or
builds a throwaway database. The ways in are the ways a user has:

    POST/GET http://127.0.0.1:8000/api/v1/...     the control plane, with an API key
    QIDO/WADO/STOW through the DICOM Gateway      with a Gateway token
    SELECT against the deployment's Postgres      what the platform actually recorded

THE PACS IS NOT REACHABLE AND THAT IS THE POINT
-----------------------------------------------
`MOS-DATA-002` makes the Gateway the sole holder of a PACS credential and `MOS-DATA-006`
puts Orthanc on a network that only the Gateway joins. In this deployment Orthanc no
longer publishes a host port at all, so every DICOM byte this gate reads or writes goes
through `http://127.0.0.1:8043/dicomweb/<tenant>/...` with `Authorization: Bearer`.
`tests/e2e/test_demo.py` used to predate that change and still name `127.0.0.1:8042`,
which is why this file was written not to import it. Register entry 70 has since moved
that suite, and the whole integration harness, onto the same Gateway root; the assertions
this gate shares with it stay re-pointed rather than re-used, because a helper that
silently falls back to a direct PACS route is exactly the thing `MOS-DATA-002` exists to
make impossible.

ORDER IS PART OF THE GATE
-------------------------
`idempotency-three-surface`'s crash arm reproduces "the PACS holds the objects and the
database knows nothing about them" by TRUNCATING the job tables and leaving the archive
alone (see that module for why a timed `docker kill` is a coin flip and this is not). That
is destructive to every job row the other four checks read, so `pytest_collection_modifyitems`
below pins the execution order and puts it last. The same hook refuses a PARTIAL gate: a
run that collects four of the five checks is a release gate that has quietly stopped
covering something, and it must not be allowed to look like a pass.

PHI (CONTRACT.md §11)
---------------------
The LCTSC collection is public, de-identified TCIA data. Even so, nothing in this package
prints a patient attribute: every message is UIDs, counts, codes and tag numbers. The
corpus at `MEDOS_E2E_LCTSC_ROOT` is opened read-only and never written.

Spec: MOS-REL-001, MOS-REL-003, MOS-REL-004, MOS-REL-012, MOS-DATA-002, MOS-DATA-006,
MOS-DATA-007, MOS-DATA-009, MOS-DATA-012, MOS-DATA-013, MOS-SEC-008, MOS-SEC-010.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from tests._support.skips import skip_infra, skip_no_data

# ======================================================================================
# The five checks of §15.1.2, in EXECUTION order.
#
# The spec's table lists them dicom-battery, idempotency-three-surface, tenant-isolation,
# provenance-replay, rejection-distinct. That is a reading order, not a running order:
# the idempotency check's crash arm truncates the job tables, so it runs last. The
# CONTENTS of the gate are the spec's; only the sequence is this file's.
# ======================================================================================
GATE_MARKER = "gate_0_1_0"

GATE_CHECKS: tuple[str, ...] = (
    "dicom-battery",
    "tenant-isolation",
    "provenance-replay",
    "rejection-distinct",
    "idempotency-three-surface",
)

#: The release-0.2.0 row of the same table, in EXECUTION order.
#:
#: The spec lists them deployment-gate, non-inferiority, per-case-metrics, leakage-check,
#: report-offline-verify, ruo-marking. Again a reading order rather than a running order:
#: the two checks that need no database at all run first, so that a Postgres that is down
#: cannot make `ruo-marking` -- a pure assertion about the DICOM writer -- look unverified.
#: The CONTENTS are the spec's; only the sequence is this file's.
GATE_020_MARKER = "gate_0_2_0"

GATE_020_CHECKS: tuple[str, ...] = (
    "ruo-marking",
    "report-offline-verify",
    "leakage-check",
    "per-case-metrics",
    "non-inferiority",
    "deployment-gate",
)

#: The release-0.3.0 row, in EXECUTION order. TEN checks: the four §15.1.2's own gate cell
#: defines, the four chapter 17 delegates to it under `MOS-TRAIN-193`, and two --
#: `migration-drift`, `capability-reachable` and `core-train-boundary` -- that this
#: repository added and argued in
#: `tests/unit/test_gate_contract.py`'s `LOCAL_EXTRA`.
#:
#: The order is cheapest-and-most-structural first. `no-auto-promote` and
#: `chain-equivalence` read source and compute; `zero-core-change` reads git;
#: `migration-drift` reads one table; `sealed-mode-isolation` drives docker; the four that
#: need a schema come next; `queue-driver-parity` is last because it forks a second pytest
#: that runs the whole Q01-Q14 suite twice and takes longer than the other eight together.
#: A Postgres that is down must not make a pure assertion about the transform generator
#: look unverified -- the same reasoning the 0.2.0 order above is written from.
#:
#: `capability-reachable` is SECOND TO LAST, ahead of `queue-driver-parity` alone, and the
#: reason is the same rule read from the other end: it is the only check in this row that
#: submits real work to the deployment and waits for the worker to finish it, so it is the
#: only one whose cost is a job. Putting it after the eight cheap ones means a deployment
#: whose worker is wedged still yields eight verdicts instead of one timeout; putting it
#: before `queue-driver-parity` keeps that check's "longest thing in the row, always last"
#: property true. It is NOT structural and must not be read as such -- it is the only
#: check here whose subject is the API, the worker and the viewer configuration at once.
#:
#: NOTHING IN THIS ROW TOUCHES THE DEPLOYMENT'S OWN DATA. `platform_dsn` builds a
#: throwaway database (see below) and `queue-driver-parity` points its child pytest at
#: that one, because the conformance suite TRUNCATEs the job tables on every test and the
#: deployment's job tables are the 0.1.0 row's subject.
GATE_030_MARKER = "gate_0_3_0"

GATE_030_CHECKS: tuple[str, ...] = (
    "no-auto-promote",
    # Beside `no-auto-promote` because they are the same KIND of check read in two
    # directions: that one asserts the training plane's import closure cannot reach
    # promotion, this one asserts the Core deployable's cannot reach the training plane.
    # Both are pure static reads of source -- no database, no docker, no deployment --
    # so they cost milliseconds and belong at the front under this row's
    # cheapest-and-most-structural-first rule.
    "core-train-boundary",
    # Same kind again, and cheaper still: a pure read of the two route registries and the
    # handler source, no database and no app construction. It answers "does the route that
    # DECLARES a permission CHECK it", which eighteen routes did not.
    "declared-permissions-are-enforced",
    "chain-equivalence",
    "zero-core-change",
    "migration-drift",
    "sealed-mode-isolation",
    "resolution-purity",
    "leakage-blocks-training",
    "served-plan-equivalence",
    "capability-reachable",
    "queue-driver-parity",
)

#: marker -> (checks, human name). One entry per release row of §15.1.2 that this
#: repository implements. Adding 0.3.0 was adding a row here and the modules its row
#: names, not editing the hook below -- which is the shape the 0.1.0-only version of this
#: file did not have, and the reason adding 0.2.0 touched it at all. Adding
#: `capability-reachable` to that row later was one line in `GATE_030_CHECKS` and one
#: module, for the same reason.
RELEASES: dict[str, tuple[tuple[str, ...], str]] = {
    GATE_MARKER: (GATE_CHECKS, "0.1.0"),
    GATE_020_MARKER: (GATE_020_CHECKS, "0.2.0"),
    GATE_030_MARKER: (GATE_030_CHECKS, "0.3.0"),
}

#: check name -> the module that implements it. The module basename IS the check name with
#: hyphens turned into underscores, and `tests/unit/test_gate_contract.py` asserts that
#: against the spec table rather than against this dict.
CHECK_MODULES: dict[str, str] = {
    check: "test_" + check.replace("-", "_")
    for checks, _release in RELEASES.values()
    for check in checks
}

#: check name -> the marker that selects it. A module is in exactly one release row.
CHECK_MARKER: dict[str, str] = {
    check: marker for marker, (checks, _r) in RELEASES.items() for check in checks
}


# ======================================================================================
# Where the deployment is. Every default matches medos/deploy/compose/docker-compose.yml.
# ======================================================================================
API_URL = os.environ.get("MEDOS_E2E_API_URL", "http://127.0.0.1:8000").rstrip("/")
GATEWAY_URL = os.environ.get("MEDOS_GATEWAY_URL", "http://127.0.0.1:8043").rstrip("/")
DATABASE_URL = (
    os.environ.get("MEDOS_TEST_DATABASE_URL")
    or os.environ.get("MEDOS_E2E_DATABASE_URL")
    or "postgresql://medos:medos@127.0.0.1:5432/medos"
)
# The Gateway token docker-compose.yml hands medos-worker (MOS-DATA-017: "a Service
# container MUST receive a scoped Gateway token, never a credential"). It is NOT a PACS
# credential and it grants nothing outside this laptop stack.
GATEWAY_TOKEN = os.environ.get("MEDOS_GATEWAY_WORKER_KEY", "medos-dev-worker-key")

TENANT_A = os.environ.get("MEDOS_TENANT_ID", "00000000-0000-0000-0000-000000000000")
#: A second tenant, for `tenant-isolation`. Created if the deployment does not have it.
TENANT_B = os.environ.get("MEDOS_GATE_TENANT_B", "11111111-1111-1111-1111-111111111111")

#: The service account every gate run authenticates as. A uuid because MOS-SEC-072 makes
#: every principal id one.
GATE_PRINCIPAL_ID = "33333333-3333-3333-3333-333333333333"

LCTSC_ROOT = Path(os.environ.get("MEDOS_E2E_LCTSC_ROOT", "F:/WorkSpace/PulmoAI/TCIA"))

CAPABILITIES = ["lung_segmentation", "emphysema_laa", "pleural_effusion"]

JOB_TIMEOUT_S = float(os.environ.get("MEDOS_E2E_JOB_TIMEOUT_S", "900"))

SOP_CLASS_SEG = "1.2.840.10008.5.1.4.1.1.66.4"
SOP_CLASS_SR_COMPREHENSIVE_3D = "1.2.840.10008.5.1.4.1.1.88.34"

#: MOS-IMG-064's derived UID space. Every UID the platform mints starts with this and
#: nothing in a source acquisition series does.
DERIVED_UID_PREFIX = "2.25."

JOB_TABLES = (
    "jobs, job_queue, job_events, job_steps, job_series, "
    "results, result_measurements, result_dicom_objects"
)

pytestmark = pytest.mark.gate_0_1_0


# ======================================================================================
# Collection: the gate runs in gate order, and a partial gate is an error
# ======================================================================================
#: Set when gate tests ran without the gate having been ASKED for as a whole, so the
#: terminal summary can say that what ran was not a gate.
_PARTIAL: pytest.StashKey[list[str]] = pytest.StashKey()

#: Set when any gate item survived selection, so the coverage report below knows a gate
#: verdict is about to be quoted and prints what that verdict does NOT cover.
_RAN: pytest.StashKey[bool] = pytest.StashKey()

#: Set when a check that needs an EMPTY job table survived selection. Distinct from `_RAN`,
#: which means "a 0.1.0 verdict is about to exist" and drives the coverage report instead:
#: `capability-reachable` is a 0.3.0 check that needs the truncation and must NOT make a
#: 0.3.0-only run print release 0.1.0's criteria coverage.
_NEEDS_EMPTY_JOBS: pytest.StashKey[bool] = pytest.StashKey()

#: The checks that cannot be evaluated against a job table an earlier run left behind.
#: `MOS-EXEC-053` derives the idempotency key from the study, the capability set and the
#: service version -- all three fixed for a given check -- so a second run of any of these
#: reads back the FIRST run's outcome and reports it as this run's. `clean_slate` documents
#: at length why that makes a gate verdict sticky, and why sticky is worse than absent.
_NEED_CLEAN_SLATE: frozenset[str] = frozenset({"capability-reachable"})


def _check_of(item: pytest.Item) -> str | None:
    """The §15.1.2 check name this item implements, or None if it implements none.

    Resolved from the MODULE, not from the marker: the marker says which release row an
    item belongs to and the filename says which check, and both have to agree for the item
    to count. An item marked `gate_0_2_0` sitting in `test_dicom_battery.py` is a mistake
    and is counted for neither.
    """
    stem = Path(str(item.path)).stem
    for check, module in CHECK_MODULES.items():
        if stem == module and item.get_closest_marker(CHECK_MARKER[check]):
            return check
    return None


def _refusal(release: str, checks: tuple[str, ...], present: dict[str, int],
             missing: list[str]) -> str:
    return "\n".join(
        [
            "",
            f"the {release} release gate is INCOMPLETE: "
            f"{len(present)} of {len(checks)} checks collected.",
            "",
            *(f"  MISSING  {c}  (expected tests/gate/{CHECK_MODULES[c]}.py)"
              for c in missing),
            *(f"  present  {c}  ({present[c]} test(s))" for c in checks if c in present),
            "",
            f"  docs/spec/15-delivery.md section 15.1.2 names all {len(checks)} for "
            f"{release}, and MOS-REL-004",
            "  leaves no reviewer discretion over them. A run that exercises a subset is "
            "not a gate:",
            "  MOS-REL-012 -- 'An unexecuted acceptance criterion means the requirement "
            "is",
            "  not satisfied, whatever the code does.' Nothing ran.",
        ]
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Pin each gate's order, and refuse a gate that is missing a check.

    `trylast` for the same reason `tests/_support/skips.py` gives: `-m` / `-k` deselection
    happens in this hook, and both the completeness check and the reordering have to see
    the surviving selection.

    THE COMPLETENESS CHECK IS THE POINT OF THIS HOOK. `pytest -m gate_0_1_0` that matches
    nothing already exits non-zero (pytest's own `no tests ran`, and under
    `--require-stack` the skip plugin's `fatal_nothing_ran`). What NEITHER of those
    catches is the far more likely regression: four checks collect, one does not -- it was
    renamed, its module raised on import, someone `-k`'d it out -- and the run prints a
    green `N passed` for a gate that no longer covers what §15.1.2 says it covers.
    `MOS-REL-012` is explicit that an unexecuted acceptance criterion means the
    requirement is not satisfied, "whatever the code does".

    TWO ROWS, ONE RULE, APPLIED INDEPENDENTLY. Each release row is judged on its own:
    `-m gate_0_2_0` must not be refused for the absence of the 0.1.0 checks, because it
    did not ask for them, and `-m "gate_0_1_0 or gate_0_2_0"` is refused if EITHER row is
    short. The two orders are concatenated 0.1.0-first so that a combined run still ends
    on the job-table truncation rather than in the middle of it.
    """
    per_release: dict[str, dict[str, int]] = {m: {} for m in RELEASES}
    gate: list[pytest.Item] = []
    other: list[pytest.Item] = []
    for item in items:
        check = _check_of(item)
        if check is None:
            other.append(item)
            continue
        counts = per_release[CHECK_MARKER[check]]
        counts[check] = counts.get(check, 0) + 1
        gate.append(item)

    if not gate:
        # No gate item survived selection. That is either a run that never asked for a
        # gate (the common case -- say nothing) or a gate run that matched nothing, which
        # the skip plugin and pytest's exit code 5 already refuse to call a pass.
        return

    markexpr = config.getoption("markexpr") or ""
    partial: list[str] = []
    for marker, (checks, release) in RELEASES.items():
        present = per_release[marker]
        if not present:
            continue  # this release row was not selected at all; it is not "incomplete"

        # A gate item of this row survived selection, so a gate VERDICT for it is about to
        # exist. The 0.1.0 terminal summary below prints what that verdict does not cover.
        if marker == GATE_MARKER:
            config.stash[_RAN] = True

        # Any row may carry a check that needs the truncation; 0.1.0 needs it wholesale
        # (four of its five checks assert "created rather than replayed", "exactly one
        # completion event", "this run's rejection"), and 0.3.0 needs it for
        # `capability-reachable` alone, whose property 4 refuses a replay outright rather
        # than reading an execution some earlier worker image performed.
        if marker == GATE_MARKER or (present.keys() & _NEED_CLEAN_SLATE):
            config.stash[_NEEDS_EMPTY_JOBS] = True

        # WHO IS REFUSED, AND WHO IS ONLY WARNED. The completeness rule binds a run that
        # asked for THE GATE -- `-m gate_0_X_0`. A developer running one module by path,
        # or bisecting with `-k`, is debugging a check and must still be able to; refusing
        # them is how a guard like this one gets deleted. They are told, in the summary,
        # that what they ran was not a gate. Adding `-k` on top of the marker IS refused:
        # that is asking for the gate and then removing part of it.
        missing = [c for c in checks if c not in present]
        if not missing:
            continue
        if marker not in markexpr:
            partial.append(
                f"release {release}: {len(present)} of {len(checks)} checks selected "
                f"({', '.join(sorted(present))})"
            )
            continue
        raise pytest.UsageError(_refusal(release, checks, present, missing))

    if partial:
        config.stash[_PARTIAL] = partial

    order = {
        check: n
        for n, check in enumerate(
            [c for checks, _r in RELEASES.values() for c in checks]
        )
    }
    gate.sort(key=lambda it: order[_check_of(it)])  # type: ignore[index]
    # Gate items last: 0.1.0's crash arm truncates the deployment's job tables.
    items[:] = other + gate


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: int, config: pytest.Config
) -> None:
    """Say out loud when gate tests ran but the GATE did not, and what the gate misses.

    TWO REPORTS, AND THE SECOND ONE IS THE NEW ONE
    ----------------------------------------------
    The first is the older guard: a `6 passed` from `pytest tests/gate/
    test_tenant_isolation.py` must never be quotable as "the gate is green".

    The second is the answer to the defect that produced this code. `pytest -m gate_0_1_0
    --require-stack` returned `24 passed`, exit 0, while `MOS-SAFE-089a` -- a MUST for
    release 0.1 -- was unmet, because the only check that would have caught it is a manual
    browser step and nothing anywhere recorded that such a step existed. The five checks
    of 15.1.2 are not the release's contents; they are the part of the contents someone
    wrote a check for. `MOS-REL-012` is explicit that an unexecuted acceptance criterion
    means the requirement is not satisfied "whatever the code does", so a gate that prints
    only its own passes is printing half a sentence.

    Printed, not raised, and deliberately so. Three Tier A items have no gate check today
    (see `tests/_support/release_criteria.py`), and failing the gate over that would be
    failing it for test placement rather than for product behaviour -- a red with no
    `MOS-REL-009` response, which is the shape of red that gets a gate bypassed. The set
    is instead FROZEN by `tests/unit/test_release_criteria.py`, so the debt is visible on
    every run and cannot grow, and it is printed here so that a green gate can never be
    quoted as "0.1.0 is verified" by anyone who read the output.
    """
    partial = config.stash.get(_PARTIAL, None)
    if partial:
        terminalreporter.write_sep("=", "release gate", bold=True)
        terminalreporter.write_line("NOT A GATE RUN.")
        for line in partial:
            terminalreporter.write_line(f"  {line}")
        terminalreporter.write_line(
            "  A gate is `pytest -m gate_0_1_0 --require-stack`, `pytest -m gate_0_2_0 "
            "--require-stack` or `pytest -m gate_0_3_0 --require-stack`,"
        )
        terminalreporter.write_line(
            "  and each refuses to run a subset of its own row. This result covers only "
            "the checks named"
        )
        terminalreporter.write_line(
            "  above and MUST NOT be recorded as a gate outcome (MOS-REL-004)."
        )

    if not config.stash.get(_RAN, False):
        return

    from tests._support.release_criteria import report_lines

    terminalreporter.write_sep(
        "=", "release 0.1.0 criteria coverage (MOS-REL-012)", bold=True
    )
    for line in report_lines():
        terminalreporter.write_line(line)


# ======================================================================================
# Small helpers -- UIDs, counts and codes only (CONTRACT.md §11)
# ======================================================================================
def tag(row: dict[str, Any], keyword: str) -> Any:
    """One DICOM-JSON tag value, or None. `keyword` is the eight-hex-digit tag."""
    return ((row.get(keyword) or {}).get("Value") or [None])[0]


@dataclass(frozen=True)
class Ingested:
    """One source CT series, as the PACS now holds it."""

    study_instance_uid: str
    series_instance_uid: str
    n_instances: int
    sop_instance_uids: tuple[str, ...]
    source_dir: Path


@dataclass(frozen=True)
class Api:
    """The control plane, with one tenant's credential bound to it."""

    base_url: str
    key: str
    tenant_id: str

    def headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", **extra}

    def post_job(
        self,
        study_instance_uid: str,
        capabilities: list[str] | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """The button's request and nothing else (MOS-SAFE-089a, CONTRACT.md §9).

        No `Idempotency-Key` header: the PLATFORM's derived key (MOS-EXEC-053) is what
        deduplicates, and the idempotency check has to measure that rather than a header
        the client chose.
        """
        return requests.post(
            f"{self.base_url}/api/v1/jobs",
            json={
                "study_instance_uid": study_instance_uid,
                "capabilities": CAPABILITIES if capabilities is None else capabilities,
            },
            headers=self.headers(Accept="application/json, application/problem+json"),
            timeout=60,
            **kwargs,
        )

    def get_job(
        self, job_id: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> requests.Response:
        """`headers` REPLACES the credential headers rather than adding to them.

        An explicit parameter and not `**kwargs`: the tenant-isolation check calls this
        with an extra `MedicalOS-Tenant-Id`, and passing that through `**kwargs` gave
        `requests.get()` two `headers` arguments and a TypeError -- a harness crash that
        looks exactly like the platform refusing the request.
        """
        return requests.get(
            f"{self.base_url}/api/v1/jobs/{job_id}",
            headers=self.headers() if headers is None else headers,
            timeout=60,
            **kwargs,
        )

    def wait_for_terminal(
        self, job_id: str, timeout_s: float = JOB_TIMEOUT_S
    ) -> dict[str, Any]:
        """Poll the job document until it reaches a terminal state.

        The job document and not the SSE stream: an event says THAT something changed, the
        job says WHAT IT NOW IS, and a gate that waited on a stream would fail differently
        when the stream stalled than when the platform stalled.
        """
        deadline = time.monotonic() + timeout_s
        last = ""
        while time.monotonic() < deadline:
            r = self.get_job(job_id)
            assert r.status_code == 200, f"GET job -> {r.status_code}: {r.text[:400]}"
            job = r.json()
            marker = f"{job['state']}/{job.get('phase')}/{job.get('steps_completed')}"
            if marker != last:
                print(f"[job {job_id}] {marker}")
                last = marker
            if job["state"] in ("COMPLETED", "FAILED", "REJECTED", "CANCELLED"):
                return job
            time.sleep(1.0)
        raise AssertionError(
            f"job {job_id} did not terminate within {timeout_s}s (last={last})"
        )


def why(job: dict[str, Any]) -> str:
    """A one-line, PHI-free account of a non-COMPLETED job, for an assertion message."""
    import json

    detail = job.get("rejection") or job.get("error") or {}
    return f"{job['state']}: " + json.dumps(detail, default=str)[:500]


# ======================================================================================
# Fixtures
# ======================================================================================
@pytest.fixture(scope="session")
def stack() -> None:
    """Refuse to run the gate against a deployment that is not there.

    A skip by default and, under `--require-stack`, a failure -- `tests/_support/stack.py`
    additionally PROBES `tests/gate`'s declared dependency set before a single test is
    allowed to pass, which is what stops a green gate on a stack with the worker stopped.

    The Gateway is probed twice on purpose: once for readiness, and once for a 401 with no
    credential. A Gateway that answers DICOMweb anonymously is not the chokepoint
    MOS-DATA-002 requires, and every other assertion in this package would still pass.
    """
    problems: list[str] = []
    ready_endpoints = (
        ("medos-api", f"{API_URL}/readyz"),
        ("medos-gateway", f"{GATEWAY_URL}/readyz"),
    )
    for name, url in ready_endpoints:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code >= 400:
                problems.append(f"{name} -> HTTP {r.status_code}")
        except requests.RequestException as exc:
            problems.append(f"{name} -> {type(exc).__name__}")
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL, connect_timeout=10) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any failure here is "no stack"
        problems.append(f"postgres -> {type(exc).__name__}")

    if problems:
        skip_infra(
            "the compose stack is not up (" + "; ".join(problems) + ")",
            dependency=problems[0].split(" -> ")[0],
        )

    anon = requests.get(
        f"{GATEWAY_URL}/dicomweb/{TENANT_A}/studies",
        headers={"Accept": "application/dicom+json"},
        timeout=20,
    )
    assert anon.status_code == 401, (
        f"the Gateway answered an unauthenticated QIDO-RS with {anon.status_code}, not "
        f"401. MOS-SEC-008 admits anonymous access on /healthz, /readyz and the OpenAPI "
        f"document and nowhere else; a PACS route that needs no credential makes "
        f"MOS-DATA-002 decorative and every tenant assertion below meaningless."
    )


@pytest.fixture(scope="session")
def db(stack: None) -> Iterator[Any]:
    """One autocommit connection to the deployment's Postgres, as the operator.

    The BOOTSTRAP role, which bypasses row-level security. That is correct for a harness
    that has to arrange and observe two tenants at once, and it is deliberately NOT how
    `tenant-isolation` proves anything: every isolation assertion in this package is made
    at an HTTP edge against a credential, never on this connection.
    """
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture(scope="session", autouse=True)
def clean_slate(request: pytest.FixtureRequest) -> None:
    """Empty the job tables once, at the start of a run that holds a check needing it.
    The ARCHIVE is not touched, and a 0.2.0-only run does not reach this at all.

    TWO ROWS NEED IT, FOR ONE REASON. Release 0.1.0 needs it wholesale. Release 0.3.0 needs
    it for `capability-reachable` alone, which was added later and which refuses a
    `MOS-EXEC-053` replay outright: its property 4 asserts that THIS run drove the deployed
    worker, and a replay re-reads an execution performed by whatever worker image was
    running then -- the exact state the historic defect hid in, where the check passed and
    printed "registry-confirmed" for four capabilities while the worker was stopped. The
    check is right to refuse; without the truncation it would refuse on every run after the
    first, which is the sticky verdict this fixture's last paragraph exists to prevent.

    THE GUARD AT THE TOP IS LOAD-BEARING, NOT DEFENSIVE
    ---------------------------------------------------
    This fixture is autouse and session-scoped, so before the 0.2.0 checks moved into this
    package it ran for every item in `tests/gate/` -- which was correct when every item in
    `tests/gate/` was a 0.1.0 check. It is not correct now. `pytest -m gate_0_2_0` would
    otherwise TRUNCATE the deployment's eight job tables in order to run six checks that
    never read one of them, and this project has already lost a day to two agents
    truncating each other's job rows and producing a spurious red gate.

    `_NEEDS_EMPTY_JOBS` is set by `pytest_collection_modifyitems` above, and only when an
    item belonging to the 0.1.0 row, or one of `_NEED_CLEAN_SLATE`, survived selection. It
    is a SEPARATE key from `_RAN` on purpose: `_RAN` means "a 0.1.0 verdict is about to
    exist" and drives the coverage report, and a 0.3.0-only run must truncate without
    printing release 0.1.0's criteria coverage. Requesting `db` lazily rather than as a
    parameter
    is the other half: `db` depends on `stack`, so a declared dependency would also make a
    0.2.0-only run demand a live API, Gateway and worker to prove something about the
    DICOM writer.

    WHY TRUNCATING IS NECESSARY AND WHY IT IS NOT CHEATING
    ------------------------------------------------
    Four of the five checks assert things that are only meaningful on a first run: that
    the job was created rather than replayed, that there is exactly one completion event,
    that a rejection is this run's rejection. Every one of those claims is unfalsifiable
    against an hour-old job table. Resetting is what gives the assertions a left-hand
    side; `idempotency-three-surface` then deliberately runs against the dirty state the
    earlier checks created.

    IT IS ALSO WHAT MAKES THE GATE RE-RUNNABLE, and that is not a convenience. The
    platform's derived idempotency key (`MOS-EXEC-053`) is a function of the study and the
    capability set, and `FAILED` is terminal -- so once a gate run leaves a failed job for
    the primary study behind, every later submission of that study is a `200` replay of
    the failure and the gate reports the OLD run's outcome forever. That was observed on
    this stack: a gate run that failed for an unrelated reason wedged the next one, which
    then "found" a defect that had already been fixed. A release gate whose verdict is
    sticky is worse than no gate.

    `TRUNCATE` and not `DELETE`, for the reason the crash arm gives at length:
    `job_events` carries a BEFORE DELETE trigger that raises (`MOS-EXEC-013`), and
    TRUNCATE does not fire row-level triggers -- so the append-only guarantee stays true
    for application code while the harness can still start clean.

    Autouse, so that no arrangement fixture can slip in ahead of it and have its rows
    truncated out from under it.
    """
    if not request.config.stash.get(_NEEDS_EMPTY_JOBS, False):
        return
    db = request.getfixturevalue("db")
    db.execute(f"TRUNCATE {JOB_TABLES} CASCADE")
    print("[gate] job tables truncated; the archive is untouched")


@pytest.fixture(scope="session")
def api(db: Any, clean_slate: None) -> Api:
    """A live API key for tenant A, minted into the deployment's own `api_keys`.

    Session-scoped because MOS-SEC-010's argon2id parameters cost ~240 ms per hash. Issued
    with a one-day expiry: long enough for any plausible gate run, short enough that a copy
    leaking out of a CI log is worthless.
    """
    from datetime import UTC, datetime, timedelta

    from medos.security import store

    minted, record = store.issue(
        db,
        tenant_id=TENANT_A,
        principal_kind="service_account",
        # `api_keys.service_account_id` is a uuid (MOS-SEC-072). A stable literal rather
        # than a fresh one per run, so repeated gate runs do not accumulate distinct
        # principals in the deployment's audit trail.
        principal_id=GATE_PRINCIPAL_ID,
        created_by=store.BOOTSTRAP_OPERATOR_ID,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        scope=["job.create", "job.read"],
        label="release 0.1.0 gate",
        env="dev",
    )
    print(f"[gate] API key {record.key_id} minted for tenant A")
    return Api(base_url=API_URL, key=minted.plaintext, tenant_id=TENANT_A)


@pytest.fixture(scope="session")
def dicomweb(stack: None) -> Iterator[Any]:
    """The DICOMweb client, pointed at the Gateway with the worker's token.

    `medos.dicomweb.DicomWebGateway` and not a bare `requests` session: CONTRACT.md §1
    makes that module the only object in `medos` permitted to reach a PACS, so an ingest
    written any other way would exercise a transport the platform does not use.
    """
    from medos.dicomweb.gateway import DicomWebGateway, GatewayConfig, PacsCredentials

    gw = DicomWebGateway(
        GatewayConfig(
            base_url=f"{GATEWAY_URL}/dicomweb/{TENANT_A}",
            credentials=PacsCredentials(bearer_token=GATEWAY_TOKEN),
            timeout_s=300.0,
        )
    )
    try:
        yield gw
    finally:
        gw.close()


def qido_series(dicomweb: Any, study_instance_uid: str) -> list[dict[str, Any]]:
    return dicomweb._client.qido_series(study_instance_uid)


def qido_instances(
    dicomweb: Any, study_instance_uid: str, series_instance_uid: str
) -> list[dict[str, Any]]:
    return dicomweb._client.qido_instances(study_instance_uid, series_instance_uid)


def derived_series(dicomweb: Any, study_instance_uid: str) -> list[dict[str, Any]]:
    """Every MedicalOS-minted series in the study, as QIDO-RS rows."""
    return [
        row
        for row in qido_series(dicomweb, study_instance_uid)
        if str(tag(row, "0020000E") or "").startswith(DERIVED_UID_PREFIX)
    ]


def derived_series_allowing_absent(
    dicomweb: Any, study_instance_uid: str
) -> list[dict[str, Any]]:
    """As `derived_series`, but a study the caller's tenant does not hold answers `[]`.

    A study with no `studies` projection row is `404` at the Gateway (MOS-DATA-013), and
    the DICOMweb client correctly raises on that rather than returning an empty list --
    "reachable, but nothing is served there" is a different fact from "served, and empty",
    and a client that conflated them would hide a misconfigured root.

    For the question "did this job write anything into the archive?", though, both answers
    are the same answer, and `404` is the stronger of the two. So this helper narrows the
    refusal to exactly that case and lets every other transport failure propagate.
    """
    from medos.core.errors import TransportFailure

    try:
        return derived_series(dicomweb, study_instance_uid)
    except TransportFailure as exc:
        if "404" not in str(exc):
            raise
        return []


# --------------------------------------------------------------------------------------
# The corpus
# --------------------------------------------------------------------------------------
def _cases() -> list[Path]:
    if not LCTSC_ROOT.is_dir():
        skip_no_data(
            f"no LCTSC source tree at {LCTSC_ROOT} (set MEDOS_E2E_LCTSC_ROOT)",
            corpus="lctsc-corpus",
        )
    cases = sorted(p for p in LCTSC_ROOT.glob("LCTSC-*") if p.is_dir())
    if not cases:
        skip_no_data(f"no LCTSC-* case directories under {LCTSC_ROOT}", corpus="lctsc-corpus")
    return cases


def _largest_ct_series(case_dir: Path) -> tuple[Path, Path, int]:
    """`(study_dir, series_dir, n_files)` of the largest CT series in one case.

    One header per series with `stop_before_pixels=True`: a directory name is not evidence
    of a modality, and picking the RTSTRUCT because it sorted first is the kind of silent
    wrong answer this gate exists to catch.
    """
    import pydicom

    best: tuple[Path, Path, int] | None = None
    for study_dir in sorted(p for p in case_dir.iterdir() if p.is_dir()):
        for series_dir in sorted(p for p in study_dir.iterdir() if p.is_dir()):
            files = sorted(series_dir.glob("*.dcm"))
            if not files:
                continue
            ds = pydicom.dcmread(str(files[0]), stop_before_pixels=True)
            if str(ds.Modality) != "CT":
                continue
            if best is None or len(files) > best[2]:
                best = (study_dir, series_dir, len(files))
    if best is None:
        skip_no_data(f"no CT series under {case_dir.name}", corpus="lctsc-corpus")
    return best


def _ingest(dicomweb: Any, case_dir: Path) -> Ingested:
    """STOW one real CT series through the Gateway.

    `assert_stored` is MOS-IMG-084 (HTTP 200 with an empty `FailedSOPSequence`) and
    MOS-IMG-152 (the QIDO-RS re-read contains the expected UID SET, not merely the right
    count) in one call -- two of `dicom-battery`'s arms, on the ingest side.

    Idempotent: Orthanc runs with `OverwriteInstances: true`, so re-STOWing the same
    SOPInstanceUIDs onto themselves does not grow the instance count.
    """
    import pydicom

    study_dir, series_dir, n = _largest_ct_series(case_dir)
    files = sorted(series_dir.glob("*.dcm"))
    sop_uids = tuple(
        str(pydicom.dcmread(str(p), stop_before_pixels=True).SOPInstanceUID) for p in files
    )
    t0 = time.monotonic()
    results = dicomweb.store_files(files, study_instance_uid=study_dir.name)
    present = dicomweb.assert_stored(results, study_dir.name, series_dir.name, list(sop_uids))
    failed = sum(len(r.failed) for r in results)
    print(
        f"[ingest] study={study_dir.name} series={series_dir.name} n={len(present)} "
        f"batches={len(results)} failed_sops={failed} "
        f"elapsed={time.monotonic() - t0:.1f}s"
    )
    assert failed == 0
    assert len(present) == n
    return Ingested(study_dir.name, series_dir.name, n, sop_uids, series_dir)


@pytest.fixture(scope="session")
def primary_study(dicomweb: Any) -> Ingested:
    """The study the battery, the provenance check and the idempotency check all use."""
    return _ingest(dicomweb, _cases()[0])


@pytest.fixture(scope="session")
def reachability_study(dicomweb: Any) -> Ingested:
    """A study of its own for the 0.3.0 row's `capability-reachable`, and why it needs one.

    `MOS-EXEC-053` derives a job's identity from the study, the capability set and the
    service version. `capability-reachable` submits one job per runnable capability group,
    and on this deployment one of those groups IS `CAPABILITIES` above -- the 0.1.0 row's
    own set. Pointed at `primary_study`, its submission would therefore carry the SAME
    derived key as `completed_job`'s, and a combined `-m "gate_0_1_0 or gate_0_3_0"` run
    would hand the 0.3.0 check a `200` replay of the 0.1.0 row's freshly completed job.

    That check refuses to pass on a replay -- a replay proves that some earlier worker
    image once ran the capability, which is the exact state the defect it exists to catch
    was invisible in -- so sharing a study would make the combined run red for something
    that is not a reachability defect, and the printed remedy would be to clear the job
    rows the 0.1.0 row is still reading. A case of its own makes the two rows' job rows
    disjoint, and costs one re-STOW onto itself.

    Index 1: `primary_study` takes 0 and `tenant_b_study` takes 2, so the three fixtures
    that need a study of their own take three different cases and none of them disturbs
    another. Two cases is the floor, and a corpus with fewer says so by name rather than
    failing later on an IndexError.
    """
    cases = _cases()
    if len(cases) < 2:
        skip_no_data(
            f"only {len(cases)} LCTSC case(s) under {LCTSC_ROOT}; capability-reachable "
            "needs a second study of its own, so that its jobs and the release-0.1.0 "
            "row's jobs cannot collide on one MOS-EXEC-053 idempotency key",
            corpus="lctsc-corpus",
        )
    return _ingest(dicomweb, cases[1])


@pytest.fixture(scope="session")
def completed_job(api: Api, primary_study: Ingested) -> dict[str, Any]:
    """Submit the study once, wait, and require COMPLETED.

    Three of the five checks are assertions ABOUT a completed job, so a job that did not
    complete is not a skip and not a soft failure -- it is the gate being red, and the
    message names the platform's own reason so the Release Decision Record (MOS-REL-003)
    can quote it.
    """
    r = api.post_job(primary_study.study_instance_uid)
    assert r.status_code in (200, 202), (
        f"POST /api/v1/jobs -> {r.status_code}: {r.text[:600]}"
    )
    body = r.json()
    print(
        f"[submit] {r.status_code} job_id={body['job_id']} "
        f"replay={r.headers.get('MedicalOS-Idempotent-Replay')} "
        f"trace={r.headers.get('MedicalOS-Trace-Id')}"
    )
    job = api.wait_for_terminal(body["job_id"])
    assert job["state"] == "COMPLETED", why(job)
    return job


@pytest.fixture(scope="session")
def stored_objects(completed_job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{"SEG": {...}, "SR": {...}}` -- the platform's account of what it wrote."""
    objects = {
        o["object_kind"]: o
        for r in completed_job["results"]
        for o in r["dicom_objects"]
    }
    assert set(objects) == {"SEG", "SR"}, sorted(objects)
    return objects


@pytest.fixture(scope="session")
def fetched(
    dicomweb: Any,
    primary_study: Ingested,
    stored_objects: dict[str, dict[str, Any]],
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Path]:
    """The generated SEG and SR, pulled back OUT of the archive, plus one source instance.

    Out of the archive and not off the writer's own Dataset: the writer's object cannot
    prove the archive stored it intact, and every arm of `dicom-battery` is a claim about
    what a third party would read back.
    """
    root = tmp_path_factory.mktemp("gate-objects")
    out: dict[str, Path] = {}
    for kind, obj in stored_objects.items():
        series = dicomweb.fetch_series(
            primary_study.study_instance_uid, obj["series_instance_uid"], root / kind
        )
        assert len(series.paths) == 1, (
            f"{kind} series {obj['series_instance_uid']} yielded {len(series.paths)} "
            f"instances, expected exactly 1"
        )
        out[kind] = series.paths[0]

    blob = dicomweb.fetch_instance(
        primary_study.study_instance_uid,
        primary_study.series_instance_uid,
        primary_study.sop_instance_uids[0],
    )
    source = root / "source.dcm"
    source.write_bytes(blob)
    out["SOURCE"] = source
    return out


# --------------------------------------------------------------------------------------
# The second tenant, for `tenant-isolation`
# --------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def tenant_b(db: Any) -> str:
    """A second tenant row, created if the deployment does not already have one."""
    db.execute(
        """
        INSERT INTO tenants (id, slug, display_name, status)
        VALUES (%s, %s, %s, 'active')
        ON CONFLICT (id) DO NOTHING
        """,
        (TENANT_B, f"gate-b-{str(TENANT_B)[:8]}", "release 0.1.0 gate: the other tenant"),
    )
    return TENANT_B


@pytest.fixture(scope="session")
def tenant_b_study(db: Any, dicomweb: Any, tenant_b: str) -> str:
    """A study that EXISTS in the PACS and whose tenancy record names tenant B.

    MOS-DATA-013 is about a study "that exists in the backend but is not in the caller's
    tenant set". A made-up UID would satisfy the 404 trivially and prove nothing: the
    requirement exists precisely because a `403` there would confirm the study is present.
    So the study has to be real.

    HOW OWNERSHIP IS ARRANGED, AND WHY IT IS NOT A BACK DOOR. The instances are STOWed
    through the Gateway exactly like any other, which creates tenant A's `studies` row
    (MOS-DATA-012). The harness then moves that row -- `studies` IS the tenancy record
    (MOS-DATA-010), so assigning a study to a tenant is a row, and MOS-DATA-047 says a
    human naming a tenant is the only legitimate way it happens. DELETE + INSERT rather
    than UPDATE because `studies_tenant_immutable` correctly refuses to move a row between
    tenants, and this is a new claim rather than an edit. Nothing in `medos/medos/` can do this;
    the operator can, which is the point.
    """
    cases = _cases()
    if len(cases) < 3:
        skip_no_data(
            f"only {len(cases)} LCTSC case(s) under {LCTSC_ROOT}; tenant-isolation needs a "
            "third study it can hand to the other tenant without disturbing the two the "
            "other checks use",
            corpus="lctsc-corpus",
        )
    study_dir, _series_dir, _n = _largest_ct_series(cases[2])
    uid = study_dir.name

    # RE-ENTRANCY, and it is the platform being right rather than the harness being
    # clever. A previous gate run leaves this study owned by tenant B, and MOS-DATA-012
    # then makes a STOW by tenant A into it a 409 for the WHOLE request -- correctly, since
    # one study has one owner. So the arrangement is checked before it is made: if the
    # projection already names tenant B, the study is already in the state this check
    # needs and re-STOWing it would fail for the right reason at the wrong moment.
    owners = {
        str(r["tenant_id"]): r
        for r in db.execute(
            "SELECT tenant_id, id, patient_id, pacs_backend, phi_state, series_count, "
            "       instance_count "
            "FROM studies WHERE study_instance_uid = %s",
            (uid,),
        ).fetchall()
    }
    if set(owners) == {TENANT_B}:
        print(f"[gate] study {uid} is already owned by tenant B from an earlier run")
        return uid

    if TENANT_A not in owners:
        _ingest(dicomweb, cases[2])
        owners = {
            str(r["tenant_id"]): r
            for r in db.execute(
                "SELECT tenant_id, id, patient_id, pacs_backend, phi_state, series_count, "
                "       instance_count "
                "FROM studies WHERE study_instance_uid = %s",
                (uid,),
            ).fetchall()
        }
    row = owners.get(TENANT_A)
    assert row is not None, (
        f"STOW through the Gateway created no `studies` row for {uid}. MOS-DATA-012 "
        f"requires the projection row to be written in the same transaction that records "
        f"the store; without it the study is invisible (MOS-DATA-013) and this check "
        f"cannot distinguish 'isolated' from 'never arrived'."
    )

    patient_b = uuid.uuid4()
    db.execute(
        """
        INSERT INTO patients (id, tenant_id, patient_id_value, issuer_of_patient_id,
                              phi_state)
        VALUES (%s, %s, %s, '', 'pseudonymised')
        ON CONFLICT (tenant_id, issuer_of_patient_id, patient_id_value) DO NOTHING
        """,
        (patient_b, TENANT_B, f"gate-b-{uid[-12:]}"),
    )
    db.execute(
        "DELETE FROM studies WHERE study_instance_uid = %s AND tenant_id = %s",
        (uid, TENANT_A),
    )
    db.execute(
        """
        INSERT INTO studies (tenant_id, patient_id, study_instance_uid, pacs_backend,
                             phi_state, series_count, instance_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, study_instance_uid) DO NOTHING
        """,
        (
            TENANT_B,
            patient_b,
            uid,
            row["pacs_backend"],
            row["phi_state"],
            row["series_count"],
            row["instance_count"],
        ),
    )
    owners = [
        str(r["tenant_id"])
        for r in db.execute(
            "SELECT tenant_id FROM studies WHERE study_instance_uid = %s", (uid,)
        ).fetchall()
    ]
    assert owners == [TENANT_B], (
        f"the study's tenancy record names {owners}, not exactly [{TENANT_B}]; the "
        f"arrangement this check depends on did not take"
    )
    print(f"[gate] study {uid} re-owned to tenant B and left in the PACS")
    return uid


# ======================================================================================
# THE RELEASE-0.2.0 WORLD. A throwaway database, the shipped migrations applied to it.
#
# Nothing below touches the deployment's own `medos` database, and nothing below depends
# on `stack`, `db`, `api` or `dicomweb`. `tests/gate/_evidence.py` states the argument for
# that at length; the short version is MOS-EVID-006 -- the evidence plane has no request
# path, so a deployment is not its subject -- plus the operational fact that a gate which
# another agent's pytest run can turn red is not a gate.
# ======================================================================================
def _swap_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"


@pytest.fixture(scope="session")
def evidence_dsn() -> Iterator[str]:
    """A brand-new database with `schema.sql` and every migration applied, FROM EMPTY.

    `medos.db.conn.apply_schema` runs the weeks 1-2 baseline and then 0001 through 0011 in
    order, so this fixture also proves the five evidence-plane migrations bootstrap a
    database rather than merely having once been applied to a long-lived one. Created on
    the deployment's own Postgres server, because that is the server the release runs on;
    dropped afterwards whatever the outcome.
    """
    import psycopg
    from medos.db.conn import apply_schema
    from psycopg.rows import dict_row

    name = f"medos_gate_020_{secrets.token_hex(6)}"
    try:
        admin = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        skip_infra(
            f"no Postgres at {DATABASE_URL.rsplit('@', 1)[-1]}: {exc}",
            dependency="postgres",
        )
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _swap_dbname(DATABASE_URL, name)
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            apply_schema(conn)
        print(f"[gate] 0.2.0 evidence schema bootstrapped into {name}")
        yield dsn
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as adm:
            adm.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            adm.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture()
def evidence_db(evidence_dsn: str) -> Iterator[Any]:
    """One connection with the default tenant bound and migration 0008's tables emptied.

    TRUNCATE and not DELETE: `deployment_gate_decisions` and `acceptance_criteria` both
    carry a BEFORE DELETE guard (MOS-EVID-091), and TRUNCATE fires no row-level triggers --
    so the seal holds for application code while the harness can still start clean. Only
    0008's four tables are emptied; the cohort tables of 0006 and the run tables of 0007
    accumulate across the checks, which is what lets each one seal its own cohort under a
    fresh nonce instead of racing the others.

    The session GUC is set HERE and nowhere else in this package. `medos.db.tenancy` is
    the only production writer of it (MOS-SEC-074/075, and the integration suite asserts
    there is exactly one); this connection is owned by one test, is never pooled, and
    serves one tenant for its whole life, which is the case that does not generalise. The
    repository layer opens its own `tenant_tx`, which reads the ContextVar bound below.
    """
    import psycopg
    from medos.db.tenancy import (
        DEFAULT_TENANT_ID,
        TENANT_GUC,
        bind_current_tenant,
        reset_current_tenant,
    )
    from psycopg.rows import dict_row

    from tests.gate._evidence import GATE_TABLES

    conn = psycopg.connect(evidence_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(f"TRUNCATE {', '.join(GATE_TABLES)} CASCADE")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        reset_current_tenant(token)
        conn.rollback()
        conn.close()


@pytest.fixture()
def manifest_store() -> Any:
    """Where a sealed manifest goes. In memory: `MOS-EVID-015` fixes the KEY, not the store.

    The object store is not part of any 0.2.0 gate claim -- `minio` is already probed for
    the 0.1.0 row -- and a gate check that went red because a bucket policy changed would
    be reporting the wrong defect.
    """
    from medos.evidence.store import InMemoryManifestStore

    return InMemoryManifestStore()


# ======================================================================================
# The release-0.3.0 world. A SECOND throwaway database, and why it is not the 0.2.0 one.
#
# `evidence_dsn` above builds exactly the database these four checks need. Sharing it
# would still be wrong, for one reason that has already cost this project a spurious red
# gate: the 0.3.0 checks TRUNCATE the registry, deployment and training tables to start
# clean, and `tests/gate/_evidence.py` deliberately lets the 0.2.0 cohort tables ACCUMULATE
# across its six checks so each can seal its own cohort under a fresh nonce. A combined
# `-m "gate_0_2_0 or gate_0_3_0"` run on one database would have 0.3.0 deleting cohorts
# 0.2.0 had sealed and was still reading. Two rows, two databases, no ordering assumption
# between them.
#
# Both are dropped in a `finally`, so a red gate leaves no database behind either.
# ======================================================================================
def _throwaway_database(prefix: str) -> Iterator[str]:
    """CREATE DATABASE, apply schema.sql + every migration FROM EMPTY, DROP afterwards.

    On the deployment's own Postgres server, because that is the server the release runs
    on. `medos.db.conn.apply_schema` runs the weeks 1-2 baseline and then 0002 through
    0014 in order, so the fixture also proves the shipped set bootstraps a database rather
    than merely having once been applied to a long-lived one.
    """
    import psycopg
    from medos.db.conn import apply_schema
    from psycopg.rows import dict_row

    name = f"{prefix}_{secrets.token_hex(6)}"
    try:
        admin = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        skip_infra(
            f"no Postgres at {DATABASE_URL.rsplit('@', 1)[-1]}: {exc}",
            dependency="postgres",
        )
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = _swap_dbname(DATABASE_URL, name)
    try:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            apply_schema(conn)
        print(f"[gate] schema bootstrapped into {name}")
        yield dsn
    finally:
        with psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as adm:
            adm.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            adm.execute(f'DROP DATABASE IF EXISTS "{name}"')


#: The tables the 0.3.0 checks own. Emptied per check, and ONLY in the database
#: `platform_dsn` created itself -- the rule `tests/gate/_evidence.py` states and the one
#: that keeps a colleague's pytest run from turning this gate red.
PLATFORM_TABLES = (
    "training_runs, conversion_runs, artifacts, registry_changelog, deployments, "
    "deployment_gate_decisions, jobs, job_queue, job_events, job_steps, job_series, "
    "dataset_versions, dataset_splits, annotation_sets, datasets"
)


@pytest.fixture(scope="session")
def platform_dsn() -> Iterator[str]:
    """The release-0.3.0 row's schema. See `_throwaway_database`."""
    yield from _throwaway_database("medos_gate_030")


@pytest.fixture()
def platform_db(platform_dsn: str) -> Iterator[Any]:
    """One connection with the default tenant bound and the 0.3.0 tables emptied.

    TRUNCATE and not DELETE, for the reason every suite over these tables gives: 0006,
    0011, 0012 and 0013 all carry BEFORE DELETE guards that raise, and TRUNCATE fires no
    row-level trigger -- so the append-only guarantee holds for application code while the
    harness can still start from empty.
    """
    import psycopg
    from medos.db.tenancy import (
        DEFAULT_TENANT_ID,
        TENANT_GUC,
        bind_current_tenant,
        reset_current_tenant,
    )
    from psycopg.rows import dict_row

    conn = psycopg.connect(platform_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(f"TRUNCATE {PLATFORM_TABLES} CASCADE")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        reset_current_tenant(token)
        conn.rollback()
        conn.close()
