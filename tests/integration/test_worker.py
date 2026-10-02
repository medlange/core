# SPDX-License-Identifier: Apache-2.0
"""Integration proof for `medos.worker`: the claim loop and the step executor.

Every test below RUNS the thing. The claims this component makes are the kind that are
true in a design document and false in a process:

  * a selection or geometry failure terminates the job `REJECTED` with a machine-readable
    reason, and NEVER `FAILED` (CONTRACT.md section 3, MOS-EXEC-014 -- release-gated);
  * a transport fault terminates `FAILED`, retries with backoff, and stops at
    `max_attempts` (MOS-EXEC-030, MOS-EXEC-062, MOS-EXEC-063);
  * a load shed touches no queue row and therefore consumes no retry budget
    (MOS-EXEC-064 mechanism 1);
  * a lost lease aborts with NO transition and NO STOW (MOS-EXEC-028);
  * the results rows and the terminal transition commit together (CONTRACT.md section 8);
  * a worker killed mid-job has its lease reclaimed and the job resumes on attempt N+1
    (CONTRACT.md section 4) -- proved by actually killing a process;
  * against a real Orthanc holding a real LCTSC series, a job reaches `COMPLETED` with
    BOTH a SEG and a TID 1500 SR stored, and the SR's measurements equal the stored
    `result_measurements` rows within MOS-IMG-044's tolerance.

Run:
    pytest tests/integration/test_worker.py -v
      MEDOS_TEST_DATABASE_URL  -> a Postgres (see conftest.py)
      MEDOS_DICOMWEB_URL       -> a DICOMweb root; the end-to-end tests skip without one.
                                  It DEFAULTS to the Gateway (`tests/_support/stack.py`),
                                  not to Orthanc: MOS-DATA-006 leaves the PACS no host
                                  port, and MOS-DATA-002 makes `medos-gateway` the only
                                  route in. Register entry 70.

The Orthanc-backed tests are idempotent by construction: a second run finds the derived
series already stored and takes the MOS-EXEC-059 skip path, which is itself a behaviour
worth exercising.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pydicom
import pytest
from medos.core.errors import TransportFailure
from medos.db import repo
from medos.db.queue import LeaseLost, PostgresJobQueue, make_worker_id
from medos.db.repo import JobSpec
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    bind_current_tenant,
    reset_current_tenant,
)
from medos.dicomweb.gateway import (
    DicomWebGateway,
    FetchedSeries,
    GatewayConfig,
    PacsCredentials,
    SeriesSummary,
)
from medos.worker.runner import RunnerConfig, WorkerRunner
from medos.worker.steps import (
    MIN_INSTANCES,
    STEP_KEYS,
    WorkerDeps,
    resolve_capability_order,
    select_ct_series,
)
from psycopg.rows import dict_row
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from tests._support import stack
from tests._support.roots import child_pythonpath
from tests._support.skips import skip_infra

CAPS = ("lung_segmentation", "emphysema_laa", "pleural_effusion")

# Chapter 5 section 5.4.1's eight reserved step keys, in plan order. Duplicated here on
# purpose: a test that imported the list it is checking would assert nothing.
RESERVED_STEP_KEYS = (
    "fetch_series",
    "build_volume",
    "envelope_check",
    "service_invoke",
    "validate_bundle",
    "write_dicom",
    "store_dicom",
    "persist_result",
)

# chapter 5's closed `jobs.reject_reason_code` enum, from schema.sql's CHECK.
REJECT_ENUM = {
    "no_eligible_series",
    "no_candidate_service_version",
    "outside_applicability_envelope",
    "unsupported_geometry",
    "input_constraint_unmet",
    "policy_denied",
    "service_declined",
}


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture()
def wconn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """An autocommit connection with every table emptied.

    autocommit, because that is how the runner opens its own: each `medos.db` helper
    wraps itself in `conn.transaction()`, which on an autocommit connection is a real
    BEGIN/COMMIT. A test on a manual-commit connection would not be testing the runner's
    transaction shape.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    # See tests/integration/conftest.py::db for why a session-level binding is correct in
    # a test fixture and forbidden in production (MOS-STORE-226). The ContextVar is what
    # `tenant_tx()` reads; the GUC is what a raw SQL statement in a test needs.
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(
        "TRUNCATE jobs, job_queue, job_events, job_steps, job_series, "
        "results, result_measurements, result_dicom_objects CASCADE"
    )
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        reset_current_tenant(token)
        conn.close()


def _enqueue(
    conn: psycopg.Connection[Any],
    study: str = "1.2.826.0.1.3680043.8.498.10000000000000000000000000000001",
    caps: tuple[str, ...] = CAPS,
    *,
    max_attempts: int = 5,
) -> str:
    queue = PostgresJobQueue(conn)
    created = repo.create_job_queued(
        conn,
        queue,
        JobSpec(study_instance_uid=study, capability_ids=caps, max_attempts=max_attempts),
    )
    return created.job_id


def _runner(
    conn: psycopg.Connection[Any],
    pg_dsn: str,
    gateway: Any,
    tmp_path: Path,
    **config: Any,
) -> WorkerRunner:
    cfg = RunnerConfig(
        dsn=pg_dsn,
        work_root=tmp_path / "work",
        worker_id=make_worker_id("test"),
        lease_seconds=config.pop("lease_seconds", 120),
        heartbeat_seconds=config.pop("heartbeat_seconds", 30),
        reclaim_on_poll=False,
        **config,
    )
    deps = WorkerDeps(gateway=gateway, work_root=cfg.work_root)
    return WorkerRunner(cfg, conn=conn, deps=deps)


# =====================================================================================
# Fake PACS
# =====================================================================================
class FakeGateway:
    """A `DicomWebGateway` stand-in. Only the five methods the executor calls.

    A fake and not a mock: the tests below are about the WORKER's behaviour when the PACS
    answers in a particular way, and a mock that records calls would let the worker pass
    while doing nothing with the answer.
    """

    def __init__(
        self,
        series: Sequence[SeriesSummary] = (),
        *,
        files: Sequence[Path] = (),
        raise_on_list: Exception | None = None,
        raise_on_store: Exception | None = None,
        existing: set[str] | None = None,
    ) -> None:
        self.series = list(series)
        self.files = list(files)
        self.raise_on_list = raise_on_list
        self.raise_on_store = raise_on_store
        self.existing = existing or set()
        self.stored: list[Path] = []

    def list_series(self, study_instance_uid: str) -> list[SeriesSummary]:
        if self.raise_on_list is not None:
            raise self.raise_on_list
        return list(self.series)

    def fetch_series(
        self,
        study_instance_uid: str,
        series_instance_uid: str,
        dest_dir: Path,
        *,
        verify_against_qido: bool = True,
    ) -> FetchedSeries:
        dest_dir.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for src in self.files:
            dst = dest_dir / src.name
            dst.write_bytes(src.read_bytes())
            out.append(dst)
        return FetchedSeries(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            directory=dest_dir,
            paths=tuple(out),
            sop_instance_uids=tuple(p.stem for p in out),
            bytes_written=sum(p.stat().st_size for p in out),
        )

    def series_exists(self, study_instance_uid: str, series_instance_uid: str) -> bool:
        return series_instance_uid in self.existing

    def store_files(self, paths: Sequence[Path], **_: Any) -> list[Any]:
        if self.raise_on_store is not None:
            raise self.raise_on_store
        self.stored.extend(paths)
        return []

    def assert_stored(self, *args: Any, **kwargs: Any) -> set[str]:
        return set()


def _synthetic_ct(
    dest: Path, n: int = 4, *, rows_of_last: int | None = None
) -> list[Path]:
    """A minimal but VALID single-frame CT series on disk.

    Valid enough for `build_canonical_volume` to accept: a supported SOP class, monotonic
    ImagePositionPatient along the slice normal, one orientation, one grid, HU after the
    Modality LUT. `rows_of_last` mutates the final instance's `Rows` to trip
    `geometry_inconsistent_grid` -- which is what the rejection test needs, and which has
    to come from the DATA rather than from a patched function, or the test proves nothing
    about the real builder.
    """
    dest.mkdir(parents=True, exist_ok=True)
    study_uid, series_uid, frame_uid = generate_uid(), generate_uid(), generate_uid()
    paths: list[Path] = []
    for k in range(n):
        rows = rows_of_last if (rows_of_last and k == n - 1) else 16
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
        ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        ds.PatientName = "TEST^SYNTHETIC"  # synthetic, not PHI; never logged
        ds.PatientID = "SYNTHETIC"
        ds.StudyDate = "20240101"
        ds.StudyTime = "120000"
        ds.ImagePositionPatient = [0.0, 0.0, float(k) * 2.0]
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.PixelSpacing = [1.0, 1.0]
        ds.SliceThickness = 2.0
        ds.Rows, ds.Columns = rows, 16
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
        ds.PixelRepresentation = 1
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.RescaleIntercept = -1024.0
        ds.RescaleSlope = 1.0
        ds.RescaleType = "HU"
        ds.PixelData = np.zeros((rows, 16), dtype=np.int16).tobytes()
        path = dest / f"{ds.SOPInstanceUID}.dcm"
        ds.save_as(str(path), enforce_file_format=True)
        paths.append(path)
    return paths


# =====================================================================================
# 1. The step plan
# =====================================================================================
def test_step_keys_are_chapter_5s_eight_reserved_keys() -> None:
    """MOS-EXEC-023. The names are reserved and "MUST NOT be reused with a different
    meaning", so the executor's plan is exactly that list, in that order."""
    assert STEP_KEYS == RESERVED_STEP_KEYS


def test_step_plan_is_written_once_per_attempt(wconn: psycopg.Connection[Any]) -> None:
    """MOS-EXEC-022: a fresh plan per attempt, same `step_key` set, `attempt` on each row."""
    job_id = _enqueue(wconn)
    assert repo.plan_steps(wconn, job_id, attempt=1) == 8
    assert repo.plan_steps(wconn, job_id, attempt=2) == 8  # replaced, not appended
    uid = repo.get_job_uuid(wconn, job_id)
    rows = wconn.execute(
        "SELECT step_key, attempt FROM job_steps WHERE job_id = %s ORDER BY step_index",
        (uid,),
    ).fetchall()
    assert [r["step_key"] for r in rows] == list(RESERVED_STEP_KEYS)
    assert {r["attempt"] for r in rows} == {2}


# =====================================================================================
# 2. Capability ordering (CONTRACT.md section 7)
# =====================================================================================
def test_capability_order_puts_the_dependency_first() -> None:
    from medos.capabilities import REGISTRY

    order = resolve_capability_order(REGISTRY, CAPS)
    assert order.index("lung_segmentation") < order.index("emphysema_laa")
    assert set(order) == set(CAPS)


def test_capability_order_refuses_an_unrequested_dependency() -> None:
    """Silently adding `lung_segmentation` would write a `results` row the caller never
    asked for, and `jobs.capability_ids` would disagree with the `results` table."""
    from medos.capabilities import REGISTRY

    with pytest.raises(ValueError, match="depends on"):
        resolve_capability_order(REGISTRY, ("emphysema_laa",))


# =====================================================================================
# 2b. `model_version` on the write path is a property of the RESULT
#
# REGRESSION. `step_write_dicom` used to fill the field with
#
#     model_version=str(ctx.deps.registry["lung_segmentation"].version)
#
# -- one capability's name, hard-coded on the path that writes every derived object. The
# first three tests below FAIL against that line: with `KeyError: 'lung_segmentation'`,
# then with the wrong version string, then with a bare `KeyError` where a `SystemFailure`
# belongs. Three, because the defect had two halves and fixing only the crash would have
# left the silent half in place. The last one PASSES against it, and must: the fix is not
# allowed to move a UID either of the platform's own job shapes already wrote.
#
# What the field feeds: MOS-IMG-062's UID seed (so every SeriesInstanceUID and
# SOPInstanceUID this job derives), and MOS-STORE-286's `dicom_objects.derivation_inputs`.
# MOS-IMG-085 makes "same UID implies same declared inputs" the invariant the retry path
# depends on, which is what a version that does not track the producing method breaks.
# =====================================================================================
class _StubCapability:
    """`Capability` as far as this code path is concerned: an id and a version.

    Deliberately NOT one of the four real capabilities. The point of the regression is a
    registry that contains a capability the platform has never heard of, which is what any
    deployment shipping a vendor's service through `WorkerDeps.registry` looks like.
    """

    def __init__(self, capability_id: str, version: str) -> None:
        self.capability_id = capability_id
        self.version = version

    def applicable(self, vol: Any) -> str | None:  # pragma: no cover - never called
        return None

    def run(self, vol: Any, ctx: Any) -> Any:  # pragma: no cover - never called
        raise NotImplementedError


def _outcome(capability_id: str, *, with_label_map: bool) -> Any:
    from medos.core.bundle import CapabilityOutcome, CodedConcept, LabelMap

    label_map = None
    if with_label_map:
        array = np.zeros((2, 4, 4), dtype=np.uint8)
        array[0, 1:3, 1:3] = 1
        label_map = LabelMap(
            array=array,
            segments=(CodedConcept(scheme="SCT", code="39607008", meaning="Lung"),),
        )
    return CapabilityOutcome(
        capability_id=capability_id,
        findings=(),
        label_map=label_map,
        source_sop_instance_uids=(),
    )


def test_model_version_comes_from_the_capability_that_produced_the_label_map() -> None:
    """A deployment shipping ONE capability, which is not `lung_segmentation`.

    This is `medos/examples/lung-nodule/worker_main.py` composed with `base={}` -- the ordinary
    shape for a vendor shipping a single service (MOS-REL-020). The old line raised
    `KeyError` here and the job was reported as an INTERNAL FAILURE naming a capability the
    operator never requested.
    """
    from medos.core.bundle import ResultBundle
    from medos.worker.steps import producing_model_version

    registry = {"widget_detect": _StubCapability("widget_detect", "3.1.4")}
    bundle = ResultBundle(outcomes=(_outcome("widget_detect", with_label_map=True),))

    assert producing_model_version(registry, bundle) == "3.1.4"


def test_model_version_follows_the_producer_not_whoever_else_is_installed() -> None:
    """The silent half: the old line was wrong even where it did not raise.

    `lung_segmentation` IS in this registry and declares a DIFFERENT version, so the old
    line returned `0.2.0` for a SEG whose every voxel came from `widget_detect@3.1.4`.
    Two versions of the producing method would then derive identical UIDs (MOS-IMG-062) and
    record identical `derivation_inputs` (MOS-STORE-286), while `result_provenance`'s
    `execution.models[]` -- built per-outcome in `worker/result_rows.py` -- said `3.1.4`.
    One job, two provenance records, contradicting each other.
    """
    from medos.capabilities import REGISTRY
    from medos.core.bundle import ResultBundle
    from medos.worker.steps import producing_model_version

    registry: dict[str, Any] = dict(REGISTRY)
    registry["widget_detect"] = _StubCapability("widget_detect", "3.1.4")
    assert registry["lung_segmentation"].version != "3.1.4", "the fixture proves nothing"
    bundle = ResultBundle(
        outcomes=(
            _outcome("pleural_effusion", with_label_map=False),
            _outcome("widget_detect", with_label_map=True),
        )
    )

    assert producing_model_version(registry, bundle) == "3.1.4"


def test_model_version_is_absent_loudly_when_the_producer_is_not_registered() -> None:
    """No `.get()` and no fallback: a version that cannot be stated stops the job.

    Substituting some other capability's version would put a false statement into every
    derived UID and into `derivation_inputs`, where nothing downstream can detect it. A
    `SystemFailure` names the producer and the registry that lacks it, so the message is
    about the operator's actual composition rather than about a capability nobody asked for.
    """
    from medos.core.bundle import ResultBundle
    from medos.core.errors import SystemFailure
    from medos.worker.steps import producing_model_version

    bundle = ResultBundle(outcomes=(_outcome("widget_detect", with_label_map=True),))

    with pytest.raises(SystemFailure) as excinfo:
        producing_model_version({}, bundle)
    assert excinfo.value.reason_code == "producing_capability_not_in_registry"
    assert excinfo.value.detail["capability_id"] == "widget_detect"
    assert "widget_detect" in excinfo.value.message


def test_model_version_still_resolves_when_no_capability_drew_anything() -> None:
    """The seam with the SR-only write path: no label map is not "no answer".

    `writer.identity.producing_outcome` owns the rule for which outcome a job with nothing
    to draw is attributable to, and reports why that rule is under-determined. What is
    asserted here is only the property this resolver needs from it: a version comes back,
    it belongs to a capability that actually ran, and MOS-IMG-062's UID seed is therefore
    fillable. A bundle of findings with no pixels must not reach `KeyError` or `None`.
    """
    from medos.core.bundle import ResultBundle
    from medos.worker.steps import producing_model_version

    registry = {"widget_detect": _StubCapability("widget_detect", "3.1.4")}
    bundle = ResultBundle(outcomes=(_outcome("widget_detect", with_label_map=False),))

    assert producing_model_version(registry, bundle) == "3.1.4"


def test_model_version_for_the_platform_capabilities_did_not_move() -> None:
    """The fix must not re-identify anything already written (MOS-IMG-085).

    `emphysema_laa` returns no label map of its own -- it measures inside
    `lung_segmentation`'s mask -- so for both of the job shapes this platform shipped
    before the fix, the producing capability IS `lung_segmentation` and the answer is the
    string the old line returned. If that were not so, every SEG and SR in an existing
    deployment would derive a new UID on its next retry.
    """
    from medos.capabilities import REGISTRY
    from medos.core.bundle import ResultBundle
    from medos.worker.steps import producing_model_version

    solo = ResultBundle(outcomes=(_outcome("lung_segmentation", with_label_map=True),))
    paired = ResultBundle(
        outcomes=(
            _outcome("lung_segmentation", with_label_map=True),
            _outcome("emphysema_laa", with_label_map=False),
        )
    )
    expected = str(REGISTRY["lung_segmentation"].version)
    assert producing_model_version(REGISTRY, solo) == expected
    assert producing_model_version(REGISTRY, paired) == expected


# =====================================================================================
# 3. REJECTED, not FAILED -- the release-gated distinction
# =====================================================================================
def test_no_eligible_series_rejects_with_a_machine_readable_reason(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """CONTRACT.md section 3 + MOS-STORE-270.

    A study holding only a SEG is not an error: it is an answer about the study. The job
    must be REJECTED, the reason must be in chapter 5's enum, every evaluated series must
    have a `job_series` row saying why it lost, and the queue row must be gone.
    """
    job_id = _enqueue(wconn)
    study = repo.get_job(wconn, job_id)["study_instance_uid"]
    gateway = FakeGateway(
        series=[
            SeriesSummary(study, "2.25.1", "SEG", 1),
            SeriesSummary(study, "2.25.2", "PR", 1),
        ]
    )
    runner = _runner(wconn, pg_dsn, gateway, tmp_path)
    outcome = runner.run_once()

    assert outcome is not None
    assert outcome.terminal_state == "REJECTED"
    assert outcome.reject_reason_code == "no_eligible_series"

    job = repo.get_job(wconn, job_id)
    assert job["state"] == "REJECTED"
    assert job["state"] != "FAILED"
    assert job["reject_reason_code"] in REJECT_ENUM
    assert job["failure_class"] is None  # a rejection is not a failure
    assert job["finished_at"] is not None

    uid = repo.get_job_uuid(wconn, job_id)
    verdicts = wconn.execute(
        "SELECT series_instance_uid, decision, reason_code FROM job_series WHERE job_id = %s",
        (uid,),
    ).fetchall()
    assert {v["series_instance_uid"] for v in verdicts} == {"2.25.1", "2.25.2"}
    assert all(v["decision"] == "rejected" and v["reason_code"] for v in verdicts)

    # MOS-EXEC-015: the queue row is deleted in the same transaction; the job is not
    # retried and consumes no further budget.
    assert PostgresJobQueue(wconn).lease_state(job_id) is None
    assert repo.list_results(wconn, job_id) == []


def test_unsupported_geometry_rejects_and_never_fails(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """MOS-IMG-011: a geometry rejection is a clinical outcome, not a crash.

    The malformed instance is malformed in the FILE -- one slice with a different `Rows`
    -- so the rejection comes out of the real `build_canonical_volume`, not out of a
    patched function.
    """
    job_id = _enqueue(wconn)
    study = repo.get_job(wconn, job_id)["study_instance_uid"]
    files = _synthetic_ct(tmp_path / "bad", n=4, rows_of_last=32)
    gateway = FakeGateway(
        series=[SeriesSummary(study, "2.25.9", "CT", len(files))], files=files
    )
    outcome = _runner(wconn, pg_dsn, gateway, tmp_path).run_once()

    assert outcome is not None
    assert outcome.terminal_state == "REJECTED"
    assert outcome.reject_reason_code == "unsupported_geometry"
    # The FINE code survives at full resolution on the step row and in the event stream,
    # while the job-level column stays inside the schema's closed enum.
    assert outcome.reject_detail_code == "geometry_inconsistent_grid"

    job = repo.get_job(wconn, job_id)
    assert job["state"] == "REJECTED"
    assert job["failure_class"] is None

    uid = repo.get_job_uuid(wconn, job_id)
    step = wconn.execute(
        "SELECT step_key, status, error_code FROM job_steps "
        "WHERE job_id = %s AND status = 'failed'",
        (uid,),
    ).fetchone()
    assert step["step_key"] == "build_volume"
    assert step["error_code"] == "geometry_inconsistent_grid"

    events = repo.list_events(wconn, job_id)
    assert any(e["event_type"] == "job.rejected" for e in events)
    assert not any(e["event_type"] == "job.failed" for e in events)


# =====================================================================================
# 4. FAILED is for transport and system faults
# =====================================================================================
def test_transport_failure_retries_with_backoff_then_fails(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """MOS-EXEC-030 + MOS-EXEC-062 + MOS-EXEC-063.

    A PACS that will not answer is not a statement about the patient, so the job must
    FAIL and not REJECT; it must retry with backoff while budget remains; and it must
    stop at `max_attempts`. The retry decision is `fail_ex`'s, never the runner's.
    """
    job_id = _enqueue(wconn, max_attempts=2)
    gateway = FakeGateway(
        raise_on_list=TransportFailure(
            "dicomweb_unreachable", {"url": "http://nowhere"}, "connection refused"
        )
    )
    runner = _runner(wconn, pg_dsn, gateway, tmp_path)

    first = runner.run_once()
    assert first is not None
    assert first.terminal_state == "QUEUED"  # T9: retry scheduled
    assert first.failure_class == "gateway_unavailable"
    assert first.retry_at is not None
    assert repo.get_job(wconn, job_id)["state"] == "QUEUED"
    lease = PostgresJobQueue(wconn).lease_state(job_id)
    assert lease["claim_count"] == 1
    assert lease["lease_owner"] is None

    # The backoff is real: the row is not claimable yet. Skip past it rather than sleep.
    wconn.execute(
        "UPDATE job_queue SET available_at = clock_timestamp() "
        "WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)",
        (job_id,),
    )

    second = runner.run_once()
    assert second is not None
    assert second.terminal_state == "FAILED"  # T11: budget exhausted
    job = repo.get_job(wconn, job_id)
    assert job["state"] == "FAILED"
    assert job["reject_reason_code"] is None  # never a clinical rejection
    assert job["failure_class"] == "gateway_unavailable"
    assert job["attempt"] == 2 == job["max_attempts"]
    assert PostgresJobQueue(wconn).lease_state(job_id) is None


# =====================================================================================
# 5. Load shedding (MOS-EXEC-064 mechanism 1)
# =====================================================================================
def test_load_shed_does_not_consume_retry_budget(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """"The queue is pull-based, so load-shedding is *not calling `Claim`*."

    With no free slot the runner must not touch the queue row at all: `claim_count` stays
    where it was, the job stays QUEUED and claimable, and `jobs.attempt` does not move.
    MOS-EXEC-035 is why this is the only correct implementation -- `claim_count` is
    incremented in exactly one statement in the system, so a shed cannot be compensated
    for after the fact.
    """
    job_id = _enqueue(wconn)
    runner = _runner(wconn, pg_dsn, FakeGateway(), tmp_path)

    before = PostgresJobQueue(wconn).lease_state(job_id)["claim_count"]
    assert runner._slots.acquire(blocking=False)  # occupy the only execution slot
    try:
        assert runner.run_once() is None
    finally:
        runner._slots.release()

    assert runner.shed_count == 1
    after = PostgresJobQueue(wconn).lease_state(job_id)
    assert after["claim_count"] == before == 0
    assert after["lease_owner"] is None
    job = repo.get_job(wconn, job_id)
    assert job["state"] == "QUEUED"
    assert job["attempt"] == 0
    assert PostgresJobQueue(wconn).depth() == 1  # still claimable by anyone


# =====================================================================================
# 6. Lease loss (MOS-EXEC-028)
# =====================================================================================
def test_lease_loss_aborts_without_a_transition_and_without_storing(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """"MUST abort immediately, MUST NOT call Complete or Fail, MUST NOT STOW."

    The job is left RUNNING on purpose: by the time a lease is lost another worker may
    already own it, and writing ANY terminal transition from here would race that worker.
    The reclaimer is what resolves it.
    """
    job_id = _enqueue(wconn)
    study = repo.get_job(wconn, job_id)["study_instance_uid"]
    files = _synthetic_ct(tmp_path / "ok", n=4)
    gateway = FakeGateway(
        series=[SeriesSummary(study, "2.25.9", "CT", len(files))], files=files
    )
    runner = _runner(wconn, pg_dsn, gateway, tmp_path)

    class _Lost:
        def check(self) -> None:
            raise LeaseLost(job_id, runner.config.worker_id)

    import medos.worker.runner as runner_mod

    original = runner_mod._Heartbeat
    runner_mod._Heartbeat = lambda *a, **k: _CtxWrap(_Lost())  # type: ignore[assignment]
    try:
        outcome = runner.run_once()
    finally:
        runner_mod._Heartbeat = original  # type: ignore[assignment]

    assert outcome is not None
    assert outcome.terminal_state == "ABORTED"
    job = repo.get_job(wconn, job_id)
    assert job["state"] == "RUNNING"  # NOT COMPLETED, NOT FAILED, NOT REJECTED
    assert job["finished_at"] is None
    assert repo.list_results(wconn, job_id) == []
    assert gateway.stored == []  # nothing reached the PACS


class _CtxWrap:
    def __init__(self, guard: Any) -> None:
        self._guard = guard

    def __enter__(self) -> Any:
        return self._guard

    def __exit__(self, *exc: object) -> None:
        return None


# =====================================================================================
# 7. Selection is deterministic
# =====================================================================================
def test_selection_is_total_and_stable() -> None:
    """MOS-IMG-066's principle applied to selection: largest instance count, then the
    lexicographically smallest UID. "Iteration order of a hash map is not a rule."""
    study = "1.2.3"
    rows = [
        SeriesSummary(study, "2.25.b", "CT", 100),
        SeriesSummary(study, "2.25.a", "CT", 100),
        SeriesSummary(study, "2.25.c", "CT", 400),
        SeriesSummary(study, "2.25.d", "SEG", 1),
        SeriesSummary(study, "2.25.e", "CT", MIN_INSTANCES - 1),
    ]
    winner, losers = select_ct_series(rows)
    assert winner.series_instance_uid == "2.25.c"
    assert select_ct_series(list(reversed(rows)))[0] == winner
    codes = {r.series_instance_uid: c for r, c, _d in losers}
    assert codes["2.25.d"] == "modality_not_applicable"
    assert codes["2.25.e"] == "insufficient_instances"
    assert codes["2.25.a"] == codes["2.25.b"] == "not_highest_ranked"


# =====================================================================================
# 8. Crash and reclaim -- by killing a real process
# =====================================================================================
_KILL_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path

    from medos.db.conn import connect
    from medos.dicomweb.gateway import SeriesSummary  # noqa: F401 - import-graph check
    from medos.worker.runner import RunnerConfig, WorkerRunner
    from medos.worker.steps import WorkerDeps

    dsn, marker, worker_id, work_root = sys.argv[1:5]


    class Hanging:
        '''Claims the job, then stops answering. The lease is held by a live process.'''

        def list_series(self, study_instance_uid):
            Path(marker).write_text("claimed")
            time.sleep(600)
            return []


    conn = connect(dsn, autocommit=True, application_name="medos-kill-test")
    cfg = RunnerConfig(
        dsn=dsn,
        worker_id=worker_id,
        work_root=Path(work_root),
        lease_seconds=4,
        heartbeat_seconds=1,
        reclaim_on_poll=False,
    )
    runner = WorkerRunner(
        cfg, conn=conn, deps=WorkerDeps(gateway=Hanging(), work_root=Path(work_root))
    )
    runner.run_once()
    """
)


def test_killed_worker_is_reclaimed_and_the_job_resumes_on_the_next_attempt(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """CONTRACT.md section 4: "Expired leases MUST be reclaimable. A reclaim increments
    `attempt`."

    A real `kill()`, not a simulated one, because the whole point is that the dying
    process gets NO chance to clean up: no `finally`, no `fail()`, no state write. What
    makes the job recoverable is the lease expiring, and that has to be true of a process
    that was shot, not of one that cooperated.

    `attempt` increments on the next CLAIM and not inside the reclaimer -- MOS-EXEC-035
    puts the increment in exactly one statement in the system. The observable property
    CONTRACT.md section 4 states is asserted here: the job resumes on attempt N+1.
    """
    job_id = _enqueue(wconn)
    marker = tmp_path / "claimed.marker"
    script = tmp_path / "kill_worker.py"
    script.write_text(_KILL_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath(inherit=env.get("PYTHONPATH"))
    proc = subprocess.Popen(
        [
            sys.executable,
            str(script),
            pg_dsn,
            str(marker),
            "doomed-worker",
            str(tmp_path / "w"),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        assert marker.exists(), "the child never claimed the job"

        job = repo.get_job(wconn, job_id)
        assert job["state"] == "RUNNING"
        assert job["attempt"] == 1
        lease = PostgresJobQueue(wconn).lease_state(job_id)
        assert lease["lease_owner"] == "doomed-worker"

        proc.kill()  # SIGKILL / TerminateProcess. No handler runs.
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover
            proc.kill()

    # The job is still RUNNING and still leased to a process that no longer exists.
    assert repo.get_job(wconn, job_id)["state"] == "RUNNING"
    assert PostgresJobQueue(wconn).lease_state(job_id)["lease_owner"] == "doomed-worker"

    runner = _runner(
        wconn, pg_dsn, FakeGateway(), tmp_path, lease_seconds=8, heartbeat_seconds=2
    )
    # Nothing to reclaim until the 4 s lease actually expires.
    assert runner.reclaim_expired() == []

    deadline = time.monotonic() + 30
    reclaimed: list[Any] = []
    while time.monotonic() < deadline and not reclaimed:
        time.sleep(0.5)
        reclaimed = runner.reclaim_expired()
    assert reclaimed, "the expired lease was never reclaimed"
    assert reclaimed[0].job_id == job_id
    assert reclaimed[0].outcome == "requeued"
    assert reclaimed[0].previous_owner == "doomed-worker"

    job = repo.get_job(wconn, job_id)
    assert job["state"] == "QUEUED"
    events = repo.list_events(wconn, job_id)
    assert any(e["event_type"] == "queue.lease_expired" for e in events)

    # T10 set a backoff; skip it and prove the next claim resumes on attempt 2.
    wconn.execute(
        "UPDATE job_queue SET available_at = clock_timestamp() "
        "WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)",
        (job_id,),
    )
    outcome = runner.run_once()
    assert outcome is not None
    assert outcome.job_id == job_id
    assert outcome.attempt == 2  # CONTRACT.md section 4's property, end to end
    assert repo.get_job(wconn, job_id)["attempt"] == 2


# =====================================================================================
# 9. End to end against a real Orthanc holding a real LCTSC series
# =====================================================================================
def _harness_gateway(base: str, *, timeout_s: float) -> DicomWebGateway:
    """The ONE way this module builds a DICOMweb client, and why it carries a bearer.

    Since register entry 70 the harness reaches DICOM the way `medos-worker` does --
    through `medos-gateway` -- because MOS-DATA-006 leaves the PACS no host port and
    MOS-DATA-002 makes the Gateway the only route in. MOS-SEC-008 admits anonymous access
    on /healthz, /readyz and the OpenAPI document and nowhere else, so a client built
    without the token measures a 401 rather than the archive.

    MOS-DATA-017: a scoped Gateway TOKEN, never a PACS credential -- MOS-DATA-005 keeps
    the credential itself on `medos-gateway` alone.

    One function and not two literals: the second construction in this module (the
    end-to-end test re-reading the SR it just stored) was written without the bearer and
    failed 401 on a job that had COMPLETED, which is the two-copies-that-drift failure
    this project has six recorded instances of.
    """
    return DicomWebGateway(
        GatewayConfig(
            base_url=base,
            credentials=PacsCredentials(bearer_token=stack.dicomweb_token()),
            timeout_s=timeout_s,
        )
    )


def _orthanc_study() -> tuple[str, str, str] | None:
    """`(base_url, study_uid, series_uid)` of a real CT series, or None to skip.

    `MEDOS_E2E_STUDY_UID` pins the study. It exists because WHICH study is in the
    development Orthanc is a property of the machine, not of this test, and because the
    capability's applicability envelope is narrower than "is a CT": a study the
    `lung_segmentation` capability declines is a correct REJECTED outcome and not an
    end-to-end failure, but it is also not what this test is measuring. Without the
    variable the first CT study with enough instances is used, which is right on a
    single-study development stack.
    """
    base = stack.dicomweb_url()
    gateway = _harness_gateway(base, timeout_s=30.0)
    try:
        gateway.preflight()
    except Exception:  # noqa: BLE001 - no PACS route is a skip, not a failure
        return None

    pinned = os.environ.get("MEDOS_E2E_STUDY_UID")
    if pinned:
        for series in gateway.list_series(pinned):
            if series.modality == "CT" and series.n_instances >= MIN_INSTANCES:
                return base, pinned, series.series_instance_uid
        return None

    try:
        import requests

        studies = requests.get(
            f"{base}/studies",
            headers={
                "Accept": "application/dicom+json",
                "Authorization": f"Bearer {stack.dicomweb_token()}",
            },
            timeout=30,
        ).json()
    except Exception:  # noqa: BLE001
        return None
    for row in studies or []:
        study = (row.get("0020000D") or {}).get("Value", [None])[0]
        if not study:
            continue
        for series in gateway.list_series(study):
            if series.modality == "CT" and series.n_instances >= 50:
                return base, study, series.series_instance_uid
    return None


def _purge_derived_series(study_instance_uid: str) -> None:
    """Remove this study's DERIVED series from the archive. Test housekeeping only.

    Chapter 9's default-DENY table forbids MedicalOS deleting a study or an instance, and
    MOS-EXEC-061 builds on that: recovery past `store_dicom` is forward-only precisely
    because nothing can be un-stored. So there is no delete anywhere in `medos/medos/`, and this
    helper talks to Orthanc's NATIVE REST API rather than through `DicomWebGateway` --
    the capability stays outside the platform, where it belongs.

    It exists so the two slow tests are independent of each other and of the previous run:
    one of them asserts that objects are stored for the first time and the other asserts
    that a second attempt finds them already there, and neither claim is checkable if the
    archive's contents depend on what ran an hour ago.

    Only `2.25.*` series are removed -- MOS-IMG-064's derived form when no `org_root` is
    configured -- so a source acquisition series can never be deleted by accident.

    THE ROUTE, AND WHY IT IS NOT A HOST PORT. MOS-DATA-006: "A deployment in which any
    other container can open a TCP connection to the PACS port is non-conformant, whether
    or not it has a credential." `orthanc` therefore publishes nothing to the host, and
    this used to dial `127.0.0.1:8042` and fail forever (register entry 70).
    `stack.orthanc_native_rest` executes inside `medos-orthanc` over its own loopback --
    the PACS talking to itself -- and classifies every failure into the skip taxonomy, so
    this returns None rather than a bool: "could not clean the archive" now stops the run
    by name instead of being a False the caller has to remember to check.
    """
    listing = stack.orthanc_native_rest([stack.NativeCall("GET", "/series")])[0]
    if not listing.ok:
        skip_infra(
            f"Orthanc answered /series with HTTP {listing.status}; refusing to assert on "
            "an archive whose state is unknown",
            dependency="orthanc-rest",
        )
    series_ids = listing.json() or []
    # One exec for the whole fan-out rather than one per series: a `docker exec` costs
    # seconds, and this loop visits every series in the archive.
    tag_rows = stack.orthanc_native_rest(
        [stack.NativeCall("GET", f"/series/{sid}") for sid in series_ids]
    )
    derived: dict[str, str] = {}
    for series_id, row in zip(series_ids, tag_rows, strict=True):
        if not row.ok:
            continue
        tags = row.json() or {}
        uid = tags.get("MainDicomTags", {}).get("SeriesInstanceUID", "")
        if uid.startswith("2.25.") and tags.get("ParentStudy"):
            derived[series_id] = tags["ParentStudy"]
    if not derived:
        return
    parents = stack.orthanc_native_rest(
        [stack.NativeCall("GET", f"/studies/{p}") for p in derived.values()]
    )
    doomed = [
        series_id
        for (series_id, _parent), row in zip(derived.items(), parents, strict=True)
        if row.ok
        and (row.json() or {}).get("MainDicomTags", {}).get("StudyInstanceUID")
        == study_instance_uid
    ]
    if doomed:
        stack.orthanc_native_rest(
            [stack.NativeCall("DELETE", f"/series/{sid}") for sid in doomed]
        )


@pytest.fixture()
def worker_gateway_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the scoped Gateway token where `WorkerRunner` will look for it.

    The two tests below run a REAL `WorkerRunner` in this process, and the runner builds
    its client from `GatewayConfig.from_env()` -- by that classmethod's own docstring "the
    ONLY place in `medos` that reads a PACS credential variable", which is the property
    CONTRACT.md section 1 exists to make auditable. So the credential has to arrive the way
    it arrives in the container: through the environment.

    `MEDOS_DICOMWEB_TOKEN` and not a user/password pair, because MOS-DATA-017 gives a
    Service container a scoped Gateway TOKEN and never a credential; MOS-DATA-005 keeps the
    PACS credential on `medos-gateway` alone. docker-compose.yml sets exactly this variable
    on `medos-worker`. Without it the Gateway correctly answers 401 and every job dies at
    `fetch_series` with `transport_failure` -- the regression `medos/medos/worker/runner.py`
    already carries a long comment about.

    monkeypatch, so the variable is gone again at teardown and no later test inherits it.
    """
    monkeypatch.setenv("MEDOS_DICOMWEB_TOKEN", stack.dicomweb_token())


@pytest.mark.slow
def test_end_to_end_completes_with_a_seg_and_an_sr_stored(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path, worker_gateway_token: None
) -> None:
    """The weeks 1-2 vertical slice, from the queue to two stored DICOM objects.

    docs/spec/15-delivery.md section 15.2.3's exit check has two halves and BOTH are
    asserted: the SEG renders, and "the measurement reported in the SR equals the
    measurement" the platform recorded -- which is only checkable because the SR exists.
    The round-trip path never wrote one, so the check had no left-hand side.

    Idempotent: a second run finds the derived series already in the archive and takes the
    MOS-EXEC-059 skip, which is asserted rather than worked around.
    """
    found = _orthanc_study()
    if found is None:
        skip_infra(
            f"no CT series with >= {MIN_INSTANCES} instances reachable at "
            f"{stack.dicomweb_url()} (set MEDOS_DICOMWEB_URL, or MEDOS_E2E_STUDY_UID to "
            "pin one)",
            dependency="orthanc",
        )
    base, study, series_uid = found
    _purge_derived_series(study)

    job_id = _enqueue(wconn, study=study)
    cfg = RunnerConfig(
        dsn=pg_dsn,
        dicomweb_url=base,
        work_root=tmp_path / "work",
        worker_id=make_worker_id("e2e"),
        lease_seconds=600,
        heartbeat_seconds=30,
        reclaim_on_poll=False,
    )
    with WorkerRunner(cfg, conn=wconn) as runner:
        outcome = runner.run_once()

    assert outcome is not None, "nothing was claimed"
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: {outcome.failure_code or outcome.reject_reason_code}"
    )

    job = repo.get_job(wconn, job_id)
    assert job["state"] == "COMPLETED"
    assert job["steps_completed"] == job["steps_total"] == 8
    assert list(job["selected_series_uids"]) == [series_uid]
    assert job["finished_at"] is not None

    # Every step ran, in order, and left a row and an event (MOS-EXEC-020, MOS-EXEC-013).
    uid = repo.get_job_uuid(wconn, job_id)
    steps = wconn.execute(
        "SELECT step_key, phase, status FROM job_steps WHERE job_id = %s ORDER BY step_index",
        (uid,),
    ).fetchall()
    assert [s["step_key"] for s in steps] == list(RESERVED_STEP_KEYS)
    assert all(s["status"] in ("succeeded", "skipped") for s in steps)
    events = repo.list_events(wconn, job_id)
    changed = [e for e in events if e["event_type"] == "job.step_changed"]
    assert {e["payload"]["step_key"] for e in changed} == set(RESERVED_STEP_KEYS)
    assert any(e["event_type"] == "job.completed" for e in events)

    # BOTH objects. This is the gap this component exists to close.
    results = repo.list_results(wconn, job_id)
    objects = [o for r in results for o in r["dicom_objects"]]
    kinds = {o["object_kind"] for o in objects}
    assert kinds == {"SEG", "SR"}, f"expected a SEG and an SR, got {kinds}"
    assert all(o["stow_state"] == "stored" for o in objects)

    seg = next(o for o in objects if o["object_kind"] == "SEG")
    sr = next(o for o in objects if o["object_kind"] == "SR")
    assert seg["sop_class_uid"] == "1.2.840.10008.5.1.4.1.1.66.4"
    assert sr["sop_class_uid"] == "1.2.840.10008.5.1.4.1.1.88.34"
    assert seg["series_instance_uid"] != sr["series_instance_uid"]

    # Provenance (CONTRACT.md section 10, MOS-STORE-278).
    for result in results:
        prov = result["provenance"]
        assert prov["study_instance_uid"] == study
        assert list(prov["series_consumed"]) == [series_uid]
        assert prov["instances_consumed"] >= 50
        assert prov["worker_version"] and prov["runtime_version"]
        assert prov["preprocessing_version"]
        assert prov["geometry"]["computation_geometry"] == "source"

    # The measurements really are in the archived SR, and they are the recorded ones
    # (MOS-IMG-120, with MOS-IMG-044's 1e-6 relative tolerance -- VR DS cannot carry a
    # float64 exactly and `result_measurements.value` is numeric(18,6)).
    gateway = _harness_gateway(base, timeout_s=120.0)
    fetched = gateway.fetch_series(study, sr["series_instance_uid"], tmp_path / "sr")
    sr_ds = pydicom.dcmread(str(fetched.paths[0]))
    assert str(sr_ds.SOPInstanceUID) == sr["sop_instance_uid"]
    assert sr_ds.CompletionFlag == "COMPLETE"
    assert sr_ds.VerificationFlag == "UNVERIFIED"  # MOS-IMG-091

    db_rows = sorted(
        (m["concept_code"], float(m["value"]))
        for r in results
        for m in r["measurements"]
    )
    assert db_rows, "no measurements were recorded"
    wanted = {code for code, _ in db_rows}
    in_sr: list[tuple[str, float]] = []

    def _walk(items: Any) -> None:
        for item in items:
            if getattr(item, "ValueType", "") == "NUM":
                name = item.ConceptNameCodeSequence[0]
                if name.CodeValue in wanted:
                    in_sr.append(
                        (name.CodeValue, float(item.MeasuredValueSequence[0].NumericValue))
                    )
            _walk(getattr(item, "ContentSequence", []) or [])

    _walk(sr_ds.ContentSequence)
    in_sr.sort()
    assert len(in_sr) == len(db_rows), f"SR carries {len(in_sr)} of {len(db_rows)} measurements"
    for (code_sr, value_sr), (code_db, value_db) in zip(in_sr, db_rows, strict=True):
        assert code_sr == code_db
        assert abs(value_sr - value_db) / abs(value_db) < 1e-6

    # The SEG is renderable and its segments are coded (MOS-IMG-112: never invented).
    seg_fetched = gateway.fetch_series(study, seg["series_instance_uid"], tmp_path / "seg")
    seg_ds = pydicom.dcmread(str(seg_fetched.paths[0]))
    assert int(seg_ds.NumberOfFrames) >= 1
    assert len(seg_ds.SegmentSequence) >= 1
    assert np.count_nonzero(seg_ds.pixel_array) > 0
    for segment in seg_ds.SegmentSequence:
        code = segment.SegmentedPropertyTypeCodeSequence[0]
        assert code.CodeValue and code.CodingSchemeDesignator

    # MOS-IMG-117: the SR's tracking identifiers are the SEG's.
    seg_tracking = {str(s.TrackingUID) for s in seg_ds.SegmentSequence}
    sr_tracking: set[str] = set()

    def _tracking(items: Any) -> None:
        for item in items:
            if getattr(item, "ValueType", "") == "UIDREF":
                sr_tracking.add(str(item.UID))
            _tracking(getattr(item, "ContentSequence", []) or [])

    _tracking(sr_ds.ContentSequence)
    assert seg_tracking <= sr_tracking


class _AbortOnceStored:
    """A `LeaseGuard` that fails the lease the instant `store_dicom` has committed.

    This reproduces the ONE crash window MOS-EXEC-059 exists for: the objects are in the
    PACS and the `results` rows are not written yet. It is driven off the `job_steps` row
    rather than off a call count, so it stays correct if the number of `lease.check()`
    calls changes.
    """

    def __init__(self, conn: Any, job_id: str, worker_id: str) -> None:
        self._conn, self._job_id, self._worker_id = conn, job_id, worker_id

    def check(self) -> None:
        row = self._conn.execute(
            "SELECT status FROM job_steps "
            " WHERE job_id = (SELECT id FROM jobs WHERE public_id = %s)"
            "   AND step_key = 'store_dicom'",
            (self._job_id,),
        ).fetchone()
        if row is not None and row["status"] == "succeeded":
            raise LeaseLost(self._job_id, self._worker_id)


@pytest.mark.slow
def test_attempt_2_reconciles_with_the_objects_attempt_1_already_stored(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path, worker_gateway_token: None
) -> None:
    """MOS-EXEC-059 + MOS-IMG-080/081: reconcile, do not re-store.

    The scenario is the real one and it is driven end to end:

      attempt 1  STOWs the SEG and the SR, then loses its lease before `persist_result`
                 commits -- the exact window in which the PACS has the objects and the
                 database has nothing;
      reclaimer  releases the expired lease (T10);
      attempt 2  finds both derived series already present, SKIPS `write_dicom` and
                 `store_dicom`, and COMPLETES with the SAME SOPInstanceUIDs.

    Deterministic UIDs (MOS-IMG-062) are the whole mechanism: attempt 2 can only ask "are
    my objects already there?" because it derives the same identity attempt 1 did.
    MOS-EXEC-061 is why it must: recovery past the store is forward-only, because platform
    policy denies deleting a study, so re-storing would leave a duplicate series forever.
    """
    found = _orthanc_study()
    if found is None:
        skip_infra(
            f"no CT series with >= {MIN_INSTANCES} instances reachable at "
            f"{stack.dicomweb_url()} (set MEDOS_DICOMWEB_URL, or MEDOS_E2E_STUDY_UID to "
            "pin one)",
            dependency="orthanc",
        )
    base, study, _series = found
    _purge_derived_series(study)

    job_id = _enqueue(wconn, study=study)
    cfg = RunnerConfig(
        dsn=pg_dsn,
        dicomweb_url=base,
        work_root=tmp_path / "work",
        worker_id=make_worker_id("e2e-retry"),
        lease_seconds=600,
        heartbeat_seconds=30,
        reclaim_on_poll=False,
    )
    runner = WorkerRunner(cfg, conn=wconn)

    import medos.worker.runner as runner_mod

    original = runner_mod._Heartbeat
    runner_mod._Heartbeat = lambda *a, **k: _CtxWrap(  # type: ignore[assignment]
        _AbortOnceStored(wconn, job_id, cfg.worker_id)
    )
    try:
        aborted = runner.run_once()
    finally:
        runner_mod._Heartbeat = original  # type: ignore[assignment]

    assert aborted is not None
    assert aborted.terminal_state == "ABORTED"
    job = repo.get_job(wconn, job_id)
    assert job["state"] == "RUNNING"          # MOS-EXEC-028: no transition was written
    assert repo.list_results(wconn, job_id) == []   # and no results were committed

    uid = repo.get_job_uuid(wconn, job_id)
    stored_step = wconn.execute(
        "SELECT status, detail FROM job_steps WHERE job_id = %s AND step_key = 'store_dicom'",
        (uid,),
    ).fetchone()
    assert stored_step["status"] == "succeeded"
    written = {o["sop_instance_uid"] for o in stored_step["detail"]["objects"]}
    assert len(written) == 2  # the SEG and the SR are in the PACS with nothing recorded

    # The lease expires and the janitor requeues it (T10).
    wconn.execute(
        "UPDATE job_queue SET lease_expires_at = clock_timestamp() - interval '1 second' "
        " WHERE job_id = %s",
        (uid,),
    )
    reclaimed = runner.reclaim_expired()
    assert [r.job_id for r in reclaimed] == [job_id]
    assert repo.get_job(wconn, job_id)["state"] == "QUEUED"
    wconn.execute(
        "UPDATE job_queue SET available_at = clock_timestamp() WHERE job_id = %s", (uid,)
    )

    second = runner.run_once()
    assert second is not None
    assert second.job_id == job_id
    assert second.attempt == 2                      # CONTRACT.md section 4
    assert second.terminal_state == "COMPLETED"

    skipped = wconn.execute(
        "SELECT step_key, skip_reason FROM job_steps WHERE job_id = %s AND status = 'skipped'"
        " ORDER BY step_index",
        (uid,),
    ).fetchall()
    assert [s["step_key"] for s in skipped] == ["write_dicom", "store_dicom"]
    assert all("MOS-EXEC-059" in s["skip_reason"] for s in skipped)

    # Same objects, not new ones: nothing was written to the PACS twice.
    objects = {
        o["sop_instance_uid"]
        for r in repo.list_results(wconn, job_id)
        for o in r["dicom_objects"]
    }
    assert objects == written
    assert {second.seg_sop_instance_uid, second.sr_sop_instance_uid} == written
    runner.close()
