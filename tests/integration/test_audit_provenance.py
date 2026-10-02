# SPDX-License-Identifier: Apache-2.0
"""Weeks 3-5: `audit_events` is append-only BY GRANT, and provenance is self-sufficient.

`docs/spec/15-delivery.md` section 15.2.4 asks this block for two things and this file is
what makes each of them a fact rather than a claim:

  1. "`audit_events` append-only at the database role level (`REVOKE UPDATE, DELETE` from
     the application role)". Section 1 below connects AS `medicalos_app` -- not as the
     bootstrap superuser, which would prove nothing -- and shows UPDATE, DELETE and
     TRUNCATE all refused, on the parent and on every partition. Section 2 shows the
     second layer refusing the OWNER, which is the role that actually destroys an audit
     trail.

  2. "the full provenance record". Section 4 runs a job to COMPLETED and then answers
     "what exactly went in and what exactly came out" from the record ALONE -- the record
     is serialised to a file, every database connection is closed, and the answer is
     reconstructed from the file. That is the only honest way to test MOS-SAFE-092's
     claim that a record reconstructs a run.

Section 3 is the requirement that makes an audit table worth having: MOS-SEC-147's
"`pulmo.effusion@2.1.0` acting for `u_7d2f4a` in `job_01JB8N...`". A trail whose actor is
a flat string cannot answer an access question about a delegated execution, and every
execution on this platform is delegated.

Spec: MOS-SEC-146, MOS-SEC-147, MOS-SEC-148, MOS-SEC-149, MOS-SEC-150, MOS-SEC-151,
MOS-SEC-153, MOS-SEC-154, MOS-SEC-155, MOS-STORE-228, MOS-SAFE-082, MOS-SAFE-083,
MOS-SAFE-084, MOS-SAFE-085, MOS-SAFE-086, MOS-SAFE-090, MOS-SAFE-092, MOS-STORE-278.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from medos.core import provenance as prov
from medos.db import audit, repo
from medos.db import provenance as provenance_db
from medos.db.queue import PostgresJobQueue
from medos.db.repo import JobSpec
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    bind_current_tenant,
    reset_current_tenant,
    tenant_context,
)
from medos.dicomweb.gateway import FetchedSeries, SeriesSummary
from medos.worker.runner import RunnerConfig, WorkerRunner, make_worker_id
from medos.worker.steps import WorkerDeps
from psycopg.rows import dict_row
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from tests.integration.conftest import APP_PASSWORD, OWNER_PASSWORD

pytestmark = pytest.mark.slow

CAPS = ("lung_segmentation",)


# =====================================================================================
# Fixtures
# =====================================================================================
def _dsn_as(pg_dsn: str, user: str, password: str) -> str:
    return re.sub(r"^postgresql://[^@]+@", f"postgresql://{user}:{password}@", pg_dsn)


@pytest.fixture()
def root(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """The bootstrap superuser. Builds fixtures and inspects the catalogue; PROVES nothing.

    It bypasses row-level security, so anything it observes about isolation is worthless.
    What it is used for here is legitimate: seeding rows, reading `pg_class`, and -- in
    section 2 -- deliberately disabling a trigger to forge a row, which is the tamper this
    chain is supposed to detect.

    NOTE what is NOT truncated: `audit_events`. It carries a statement-level TRUNCATE
    guard (MOS-SEC-154 / chapter 8 section 8.9.3) and truncating it here would be this
    file quietly exempting itself from the control it is testing. Every assertion below
    therefore filters by job, tenant or request id instead of assuming an empty table --
    which is how a reader of a real audit trail has to work anyway.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
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


@pytest.fixture()
def app_conn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection as `medicalos_app` -- NOBYPASSRLS, owns nothing, holds only grants.

    This is the role the API and the worker run as, and it is the only role whose
    privileges are evidence about what the application can do.
    """
    conn = psycopg.connect(
        _dsn_as(pg_dsn, "medicalos_app", APP_PASSWORD), row_factory=dict_row
    )
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def owner_conn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = psycopg.connect(
        _dsn_as(pg_dsn, "medicalos_owner", OWNER_PASSWORD), row_factory=dict_row
    )
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


class FakeGateway:
    """The five methods the executor calls, answering from local files.

    A fake and not a mock: these tests are about what the platform RECORDS when the PACS
    answers, and a mock that counted calls would let the recording be wrong.
    """

    def __init__(self, series: Sequence[SeriesSummary], files: Sequence[Path]) -> None:
        self.series = list(series)
        self.files = list(files)
        self.stored: list[Path] = []

    def list_series(self, study_instance_uid: str) -> list[SeriesSummary]:
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
        return False

    def store_files(self, paths: Sequence[Path], **_: Any) -> list[Any]:
        self.stored.extend(paths)
        return []

    def assert_stored(
        self,
        results: Sequence[Any],
        study_instance_uid: str,
        series_instance_uid: str,
        expected_sop_instance_uids: Sequence[str],
    ) -> set[str]:
        # The real gateway re-reads the derived series over QIDO-RS and returns the set it
        # found (MOS-IMG-152); the fake returns the same shape so `qido_verified_at` and
        # `instance_count_verified` carry a real count rather than a placeholder.
        return set(expected_sop_instance_uids)


def _synthetic_ct(dest: Path, n: int = 40) -> tuple[list[Path], str, str]:
    """A minimal but VALID single-frame CT series. Returns (paths, study_uid, series_uid).

    Every number here is dictated by a real gate in `medos.capabilities.lung_segmentation`
    and by `medos.core.masks.mask_from_hu_threshold`, because a fixture that the pipeline
    declines is a correct REJECTED and a useless subject for a provenance test:

      * `n = 40`, `SliceThickness = 4 mm`, `PixelSpacing = 1.5 mm` -- inside the
        applicability envelope (`MIN_SLICES = 16`, `DELTA_S_MM_RANGE = (0.5, 5.0)`,
        `MAX_IN_PLANE_MM = 1.5`).
      * The two lung rectangles sit INSIDE a soft-tissue body that reaches every in-plane
        face. `mask_from_hu_threshold` keeps only air components that touch no in-plane
        face -- room air always does, lungs never do -- so a phantom whose "lungs" ran to
        the image edge would be discarded as room air.
      * They are absent from the first two and last two slices. `_scan_extent_filter`
        drops any component spanning the whole craniocaudal extent, which is how the real
        capability removes the scanner couch; a phantom present on every slice is a couch.
      * 70 x 50 px per lung over 36 slices is ~2270 ml total, inside
        `PLAUSIBLE_TOTAL_ML = (1500, 12000)` and well over `MIN_COMPONENT_ML = 50`.
    """
    dest.mkdir(parents=True, exist_ok=True)
    study_uid, series_uid, frame_uid = generate_uid(), generate_uid(), generate_uid()
    rows = cols = 160
    paths: list[Path] = []
    for k in range(n):
        body = np.full((rows, cols), -1000, dtype=np.int16)  # room air, touches the faces
        body[20:150, 20:150] = 40                            # soft tissue
        if 2 <= k < n - 2:
            body[40:110, 25:75] = -850                       # one lung
            body[40:110, 85:135] = -850                      # the other
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
        # Synthetic values, never a real person. They exist so that the PHI scan in
        # `test_the_record_carries_no_phi` has something to look for: a record that leaked
        # PatientName, StudyDescription or AccessionNumber would carry THESE strings.
        ds.PatientName = "TEST^SYNTHETIC"
        ds.PatientID = "SYNTHETIC-0001"
        # `medos.writer.identity._COPY_OR_EMPTY` -- MOS-IMG-090's "copy-or-empty" set.
        # All five are present because the current writer reads them with a plain
        # attribute access and raises `AttributeError` when one is absent, rather than
        # writing the zero-length Type 2 element MOS-IMG-090 specifies. REPORTED as a
        # defect in `medos/medos/writer/identity.py`; setting them here is not a workaround for
        # this test's subject, and it gives the PHI scan five more things to look for.
        ds.PatientBirthDate = "19700101"
        ds.PatientSex = "O"
        ds.StudyID = "1"
        ds.ReferringPhysicianName = "REF^SYNTHETIC"
        ds.StudyDescription = "SYNTHETIC CHEST PHANTOM"
        ds.AccessionNumber = "ACC-SYNTHETIC-1"
        ds.StudyDate = "20240101"
        ds.StudyTime = "120000"
        ds.ImagePositionPatient = [0.0, 0.0, float(k) * 4.0]
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.PixelSpacing = [1.5, 1.5]
        ds.SliceThickness = 4.0
        ds.ConvolutionKernel = "B31f"
        ds.Rows, ds.Columns = rows, cols
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
        ds.PixelRepresentation = 1
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.RescaleIntercept = 0.0
        ds.RescaleSlope = 1.0
        ds.RescaleType = "HU"
        ds.PixelData = body.tobytes()
        path = dest / f"{ds.SOPInstanceUID}.dcm"
        ds.save_as(str(path), enforce_file_format=True)
        paths.append(path)
    return paths, study_uid, series_uid


@pytest.fixture()
def completed_job(
    root: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> dict[str, Any]:
    """One job driven to COMPLETED through the real runner. The subject of sections 3-4.

    Through `WorkerRunner` and not by hand-inserting rows, because the thing under test is
    what the PIPELINE records -- an audit trail and a provenance record assembled by a
    test fixture would prove that the fixture is correct.
    """
    files, study_uid, series_uid = _synthetic_ct(tmp_path / "src")
    gateway = FakeGateway(
        [SeriesSummary(
            study_instance_uid=study_uid,
            series_instance_uid=series_uid,
            modality="CT",
            n_instances=len(files),
        )],
        files,
    )
    queue = PostgresJobQueue(root)
    created = repo.create_job_queued(
        root,
        queue,
        JobSpec(
            study_instance_uid=study_uid,
            capability_ids=CAPS,
            created_by_kind="user",
            created_by_id="u_7d2f4a",
            source_ip="198.51.100.9",
            user_agent="OHIF/3.9 (integration test)",
        ),
    )
    cfg = RunnerConfig(
        dsn=pg_dsn,
        work_root=tmp_path / "work",
        worker_id=make_worker_id("audit-test"),
        lease_seconds=120,
        heartbeat_seconds=30,
        reclaim_on_poll=False,
    )
    runner = WorkerRunner(
        cfg, conn=root, deps=WorkerDeps(gateway=gateway, work_root=cfg.work_root)
    )
    runner.run_once()

    job = repo.get_job(root, created.job_id)
    assert job is not None, "the job vanished"
    assert job["state"] == "COMPLETED", (
        f"the fixture job is {job['state']}, not COMPLETED "
        f"({job.get('reject_reason_code') or job.get('failure_code')}); "
        "sections 3 and 4 have nothing to read"
    )
    return {
        "job_id": created.job_id,
        "job": job,
        "study_instance_uid": study_uid,
        "series_instance_uid": series_uid,
        "instance_uids": sorted(p.stem for p in files),
        "worker_id": cfg.worker_id,
    }


# =====================================================================================
# 1. THE HEADLINE: the revocation bites.  MOS-STORE-228, chapter 8 section 8.9.3.
# =====================================================================================
def test_the_application_role_holds_insert_and_select_and_nothing_else(
    root: psycopg.Connection[Any],
) -> None:
    """Chapter 8 section 8.10's exit check item 3, verbatim:

        `has_table_privilege('medicalos_app','audit_events','UPDATE')` and the same for
        `DELETE` are both `false`.

    The catalogue read is the cheap half and it runs first, because a failure here names
    the defect ("the grant is wrong") while the behavioural test below only reports a
    symptom.
    """
    got = {
        verb: root.execute(
            "SELECT has_table_privilege('medicalos_app','audit_events',%s) AS p", (verb,)
        ).fetchone()["p"]
        for verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES")
    }
    assert got["SELECT"] is True, "the application role cannot read its own audit trail"
    assert got["INSERT"] is True, "the application role cannot append (MOS-SEC-149)"
    assert got["UPDATE"] is False, "MOS-STORE-228: UPDATE is granted on audit_events"
    assert got["DELETE"] is False, "MOS-STORE-228: DELETE is granted on audit_events"
    assert got["TRUNCATE"] is False, "TRUNCATE empties the table without a single DELETE"


def test_every_audit_partition_is_hardened_not_just_the_parent(
    root: psycopg.Connection[Any],
) -> None:
    """MOS-SEC-154 makes `audit_events` partitioned, and a partition reached DIRECTLY
    bypasses the parent's ACL, its policies and its triggers.

    `DELETE FROM audit_events_2026_03` is a perfectly ordinary statement. If the hardening
    stopped at the parent, the table would be append-only only for callers polite enough
    to use it.
    """
    rows = root.execute(
        """
        SELECT c.relname,
               has_table_privilege('medicalos_app', c.oid, 'UPDATE')   AS upd,
               has_table_privilege('medicalos_app', c.oid, 'DELETE')   AS del,
               has_table_privilege('medicalos_app', c.oid, 'TRUNCATE') AS trunc,
               c.relrowsecurity, c.relforcerowsecurity,
               EXISTS (SELECT 1 FROM pg_trigger t
                        WHERE t.tgrelid = c.oid
                          AND t.tgname = c.relname || '_no_mutate') AS guarded
          FROM pg_class c
          JOIN pg_inherits i ON i.inhrelid = c.oid
         WHERE i.inhparent = 'audit_events'::regclass
        """
    ).fetchall()
    assert len(rows) >= 2, "audit_events is not partitioned (MOS-SEC-154)"
    bad = [
        r["relname"]
        for r in rows
        if r["upd"] or r["del"] or r["trunc"]
        or not r["relrowsecurity"] or not r["relforcerowsecurity"] or not r["guarded"]
    ]
    assert not bad, f"unhardened audit partitions: {bad}"


def test_update_and_delete_as_the_application_role_both_fail(
    app_conn: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """The behavioural half. Connected AS `medicalos_app`, with rows it wrote itself.

    A test that used the bootstrap superuser here would prove nothing at all: a superuser
    bypasses grants and row security alike, so it would be measuring the test harness.
    """
    row = app_conn.execute(
        "SELECT public_id, seq FROM audit_events WHERE job_public_id = %s ORDER BY seq LIMIT 1",
        (completed_job["job_id"],),
    ).fetchone()
    app_conn.commit()
    assert row is not None, "the completed job left no audit row at all (MOS-SEC-149)"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "UPDATE audit_events SET action = 'job.create' WHERE public_id = %s",
            (row["public_id"],),
        )
    app_conn.rollback()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "DELETE FROM audit_events WHERE public_id = %s", (row["public_id"],)
        )
    app_conn.rollback()

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("TRUNCATE audit_events")
    app_conn.rollback()

    # Still there, and unchanged. The point of an append-only table is not that the verbs
    # error; it is that the row survives them.
    after = app_conn.execute(
        "SELECT action, hash FROM audit_events WHERE public_id = %s", (row["public_id"],)
    ).fetchone()
    app_conn.commit()
    assert after is not None


def test_the_provenance_record_is_append_only_for_the_application_role_too(
    app_conn: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SAFE-090 and chapter 9's DENY row 7: "Delete a Result, ValidationReport,
    AuditEvent, PolicyDecision or provenance record -> DENY (hard), DB role lacks
    DELETE/UPDATE on those tables"."""
    row = app_conn.execute(
        "SELECT result_id FROM result_provenance WHERE job_public_id = %s",
        (completed_job["job_id"],),
    ).fetchone()
    app_conn.commit()
    assert row is not None, "a COMPLETED job produced no provenance record (MOS-SAFE-082)"

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "UPDATE result_provenance SET worker_version = 'forged' WHERE result_id = %s",
            (row["result_id"],),
        )
    app_conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute(
            "DELETE FROM result_provenance WHERE result_id = %s", (row["result_id"],)
        )
    app_conn.rollback()


# =====================================================================================
# 2. The second layer: the trigger binds the OWNER, which a grant cannot.
# =====================================================================================
def test_the_owner_cannot_mutate_the_audit_trail_either(
    owner_conn: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """Chapter 8 section 8.9.3 ships BOTH a revocation and a trigger, and this is why:

    `REVOKE` binds a grantee. The table owner is not a grantee -- it holds privileges by
    ownership -- so a revocation says nothing about it, and `medicalos_owner` is reachable
    through `medicalos_migrator`'s membership and through any operator who connects with
    owner rights. That is the session that actually destroys an audit trail, and the
    statement-level trigger is the only thing in the database that stops it.
    """
    row = owner_conn.execute(
        "SELECT public_id FROM audit_events WHERE job_public_id = %s LIMIT 1",
        (completed_job["job_id"],),
    ).fetchone()
    owner_conn.commit()
    assert row is not None

    for statement, params in (
        ("UPDATE audit_events SET outcome = 'deny' WHERE public_id = %s", (row["public_id"],)),
        ("DELETE FROM audit_events WHERE public_id = %s", (row["public_id"],)),
        ("TRUNCATE audit_events", ()),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
            owner_conn.execute(statement, params or None)
        assert "append-only" in str(excinfo.value), (
            "the owner was stopped, but by something other than the append-only trigger"
        )
        owner_conn.rollback()


# =====================================================================================
# 3. MOS-SEC-147: the actor can express "capability X acting for user Y in job Z".
# =====================================================================================
def test_the_audit_row_joins_to_the_execution_that_caused_it(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SEC-003 and MOS-SEC-155, together.

        "Every security-relevant decision MUST leave a record that is joinable, by
         `job_id` and `trace_id`, to the execution that caused it."

    So: every row of this job's trail names the job, and every row carries the SAME
    `trace_id` the `jobs` row carries. A trail that agreed with the job about the id but
    not about the trace would join in SQL and not in a distributed trace, which is where
    the question is actually asked.
    """
    job = completed_job["job"]
    with tenant_context(DEFAULT_TENANT_ID):
        trail = audit.trail_for_job(root, completed_job["job_id"])

    assert trail, "no audit rows for a COMPLETED job"
    assert all(r["job_public_id"] == completed_job["job_id"] for r in trail)
    assert {r["trace_id"] for r in trail} == {job["trace_id"]}, (
        "the audit trail and the job disagree about the trace id (MOS-SEC-003)"
    )
    # Gapless and ordered, per tenant (MOS-SEC-146). Gaps matter because to a verifier a
    # gap and a deletion are the same observation.
    seqs = [r["seq"] for r in trail]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_actor_plus_on_behalf_of_expresses_a_capability_acting_for_a_user_in_a_job(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SEC-147, as its own sentence:

        "`actor` + `on_behalf_of` MUST be able to express `pulmo.effusion@2.1.0` acting
         for `u_7d2f4a` in `job_01JB8N...`. A flat `actor: "user_123"` string MUST NOT be
         used; it cannot answer an access question about a delegated execution, which is
         the only kind of access question this platform generates."

    The fixture creates the job as `u_7d2f4a` on purpose, so the assertion below is the
    requirement's own example with this slice's capability substituted for the
    requirement's.
    """
    with tenant_context(DEFAULT_TENANT_ID):
        trail = audit.trail_for_job(root, completed_job["job_id"])

    created = [r for r in trail if r["action"] == "result.create"]
    assert created, "committing a result left no audit row (MOS-SEC-149)"
    row = created[0]

    assert row["actor_kind"] == "service_version", (
        "the actor of a result is the capability that produced it, not the worker process"
    )
    assert row["actor_id"].startswith("lung_segmentation@"), row["actor_id"]
    assert row["on_behalf_of_kind"] == "user"
    assert row["on_behalf_of_id"] == "u_7d2f4a"
    assert row["job_public_id"] == completed_job["job_id"]

    # The whole sentence, reconstructed from the row. If any of the three fields were a
    # single flattened string this would be unrepresentable, which is the defect
    # MOS-SEC-147 names.
    sentence = (
        f"{row['actor_id']} acting for {row['on_behalf_of_id']} in {row['job_public_id']}"
    )
    assert re.fullmatch(
        r"lung_segmentation@\S+ acting for u_7d2f4a in job_[0-9A-HJKMNP-TV-Z]{26}",
        sentence,
    ), sentence


def test_a_phi_read_writes_the_authorization_and_effect_pair(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SEC-150. A PACS read crosses a boundary with no shared transaction, so:

        "two events MUST be written, joined by `request_id`: the authorization event
         (`action = study.read_pixels`, `outcome = allow`) committed BEFORE the upstream
         call, and the effect event (`action = study.read_pixels.completed`) after it,
         carrying counts in `detail`. Append-only storage forbids updating the first, so
         the pair is the mechanism."

    A single row written afterwards would record only reads that completed, which is the
    half that does not matter: an interrupted retrieval that touched a patient's pixels is
    exactly what an access review is looking for.
    """
    with tenant_context(DEFAULT_TENANT_ID):
        trail = audit.trail_for_job(root, completed_job["job_id"])

    auth = [r for r in trail if r["action"] == "study.read_pixels"]
    effect = [r for r in trail if r["action"] == "study.read_pixels.completed"]
    assert len(auth) == 1 and len(effect) == 1
    assert auth[0]["request_id"] == effect[0]["request_id"], "the pair does not join"
    assert auth[0]["seq"] < effect[0]["seq"], "the authorization was recorded after the read"
    assert auth[0]["action_class"] == "phi" == effect[0]["action_class"], (
        "MOS-SEC-148: a PHI read is class `phi` and is therefore never sampled away"
    )
    assert effect[0]["detail"]["instances_returned"] == len(completed_job["instance_uids"])
    assert effect[0]["detail"]["series_evaluated"] >= 1

    with tenant_context(DEFAULT_TENANT_ID):
        pair = audit.trail_for_request(root, auth[0]["request_id"])
    assert [r["action"] for r in pair] == [
        "study.read_pixels",
        "study.read_pixels.completed",
    ]


def test_the_audit_resource_carries_the_pseudonym_and_never_the_raw_uid(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SEC-105 / MOS-SEC-106: a StudyInstanceUID is P2 and "MUST be replaced by
    `study_ref` (P1) on every surface where the matrix forbids P2".

    `resource` is such a surface -- MOS-SEC-146 says "never a raw UID for a P3 subject"
    and an audit export goes to a reviewer. The raw UID still travels in the row's own
    `study_instance_uid` column, which chapter 12 section 12.13 declares and the partial
    index serves, so "who looked at this study" stays answerable inside the platform.
    """
    with tenant_context(DEFAULT_TENANT_ID):
        trail = audit.trail_for_job(root, completed_job["job_id"])
    study = completed_job["study_instance_uid"]
    for row in trail:
        if row["resource_kind"] == "study":
            assert row["resource_id"] != study, "the raw UID is in `resource_id`"
            assert row["resource_id"].startswith("sr_"), row["resource_id"]
            assert row["study_instance_uid"] == study


def test_the_chain_verifies_and_a_forged_row_is_reported_at_its_seq(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SEC-151 and MOS-SEC-153: "recompute the chain over a range and report the first
    `seq` at which it diverges. A tamper test MUST exist in CI."

    The forgery is deliberately done the only way it CAN be done -- as the superuser, with
    the append-only trigger disabled on the partition holding the row. That is the threat
    model: the chain is not there to stop a database administrator, it is there to make
    what they did visible afterwards.
    """
    with tenant_context(DEFAULT_TENANT_ID):
        assert audit.verify_chain(root) is None, "the chain is broken before any tampering"
        trail = audit.trail_for_job(root, completed_job["job_id"])

    victim = trail[len(trail) // 2]
    part = root.execute(
        "SELECT tableoid::regclass AS part FROM audit_events WHERE public_id = %s",
        (victim["public_id"],),
    ).fetchone()["part"]

    root.execute(f'ALTER TABLE {part} DISABLE TRIGGER "{part}_no_mutate"')
    try:
        root.execute(
            f"UPDATE {part} SET outcome = 'deny' WHERE public_id = %s",
            (victim["public_id"],),
        )
        with tenant_context(DEFAULT_TENANT_ID):
            broken = audit.verify_chain(root)
        assert broken is not None, "a forged row verified clean"
        assert broken["seq"] == victim["seq"], (
            f"divergence reported at seq {broken['seq']}, forged at {victim['seq']}"
        )
        assert "hash" in broken["reason"]
    finally:
        # Put it back, both the row and the guard, so a later test in this session is not
        # reading a forged trail.
        root.execute(
            f"UPDATE {part} SET outcome = %s WHERE public_id = %s",
            (victim["outcome"], victim["public_id"]),
        )
        root.execute(f'ALTER TABLE {part} ENABLE TRIGGER "{part}_no_mutate"')

    with tenant_context(DEFAULT_TENANT_ID):
        assert audit.verify_chain(root) is None, "the restore did not restore"


# =====================================================================================
# 4. THE PROVENANCE RECORD: sufficient on its own.  MOS-SAFE-083 / MOS-SAFE-092.
# =====================================================================================
def test_one_provenance_record_exists_per_result_and_only_via_the_result(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-STORE-278: "Exactly one `result_provenance` row MUST exist per `results` row,
    written in the same transaction, and a result without provenance MUST NOT be
    observable through any API."

    The cardinality is asserted here; the FK that makes the orphan impossible is asserted
    by the catalogue read below it. Both, because the FK alone permits a `results` row
    with no child and the count alone permits the FK to have been dropped.
    """
    counts = root.execute(
        """
        SELECT (SELECT count(*) FROM results WHERE job_id = j.id)            AS results,
               (SELECT count(*) FROM result_provenance WHERE job_id = j.id)  AS records
          FROM jobs j WHERE j.public_id = %s
        """,
        (completed_job["job_id"],),
    ).fetchone()
    assert counts["results"] >= 1
    assert counts["records"] == counts["results"], "MOS-SAFE-082: one record per result"

    fk = root.execute(
        """
        SELECT confrelid::regclass::text AS parent, confdeltype
          FROM pg_constraint
         WHERE conrelid = 'result_provenance'::regclass AND contype = 'f'
        """
    ).fetchall()
    assert any(f["parent"] == "results" for f in fk), (
        "MOS-SAFE-082: a Result without a provenance record must be impossible BY "
        "FOREIGN KEY, not by convention"
    )
    assert all(f["confdeltype"] == "r" for f in fk), (
        "MOS-STORE-337: result_provenance is ON DELETE RESTRICT, so a purge cannot take "
        "the evidence with the data"
    )


def test_the_record_identifies_the_exact_inputs_and_outputs_with_no_other_table(
    root: psycopg.Connection[Any], completed_job: dict[str, Any], tmp_path: Path
) -> None:
    """The block's acceptance check, and the only honest way to run it.

    The record is read once, written to a JSON file, and every assertion afterwards is
    made against THAT FILE -- the database is not consulted again for any input or output
    fact. `MOS-SAFE-092` promises a tool that "reconstructs the run from the record alone";
    a test that kept a cursor open would be testing the cursor.

    The expected values come from the FIXTURE (the synthetic files on disk and the UIDs
    they carry), not from another query, so the record is checked against the world rather
    than against the platform's opinion of the world.
    """
    with tenant_context(DEFAULT_TENANT_ID):
        records = provenance_db.get_for_job(root, completed_job["job_id"])
    assert len(records) >= 1

    exported = tmp_path / "provenance.json"
    exported.write_text(json.dumps(records[0], indent=2, default=str), encoding="utf-8")

    # --- from here down, the file is the only source -------------------------------
    record = json.loads(exported.read_text(encoding="utf-8"))

    assert record["record_schema_version"] == prov.RECORD_SCHEMA_VERSION
    assert not prov.validate(record), prov.validate(record)

    inputs = prov.consumed_inputs(record)
    assert inputs["study_instance_uid"] == completed_job["study_instance_uid"]
    assert inputs["series"] == [completed_job["series_instance_uid"]]
    assert sorted(inputs["instance_uids"]) == completed_job["instance_uids"], (
        "the record does not name the exact instances that were read (MOS-STORE-279)"
    )
    assert inputs["instance_count"] == len(completed_job["instance_uids"])
    assert re.fullmatch(r"[0-9a-f]{64}", inputs["uid_digest"])
    assert re.fullmatch(r"[0-9a-f]{64}", inputs["pixel_digest"])
    # MOS-SAFE-083 section B: the geometry the measurements were computed on.
    for key in ("shape", "spacing_mm", "origin_lps_mm", "direction_lps",
                "frame_of_reference_uid"):
        assert record["input"]["canonical_geometry"][key], f"geometry.{key} missing"

    outputs = prov.produced_outputs(record)
    assert {o["kind"] for o in outputs} == {"SEG", "SR"}, (
        "MOS-SAFE-083 section F: every generated object, with where it landed"
    )
    for o in outputs:
        assert re.fullmatch(r"2\.25\.\d+", o["series_instance_uid"]), o
        assert re.fullmatch(r"2\.25\.\d+", o["sop_instance_uid"]), o
        assert o["sha256"] and re.fullmatch(r"[0-9a-f]{64}", o["sha256"])
        assert o["size_bytes"] and o["size_bytes"] > 0
        # MOS-SAFE-085: set only after a QIDO-RS re-read returned the expected count.
        assert o["qido_verified_at"], "outputs[].destination.qido_verified_at is unset"

    # MOS-SAFE-083 section D: the versions that make the run repeatable.
    ex = record["execution"]
    assert ex["preprocessing_specs"] and ex["preprocessing_specs"][0]["version"]
    assert ex["models"] and ex["models"][0]["version"]
    assert ex["runtime"]["worker_version"].startswith("medos.worker/")
    assert ex["runtime"]["runtime_version"]
    assert ex["reproducibility_class"] in ("bitwise", "numeric_tolerance", "not_reproducible")

    # MOS-SAFE-090: the record carries its own chain position and hash, so the exported
    # file is verifiable rather than merely readable.
    assert re.fullmatch(r"[0-9a-f]{64}", record["record_hash"])
    assert re.fullmatch(r"[0-9a-f]{64}", record["prev_record_hash"])
    assert record["sequence_no"] >= 1


def test_the_record_shows_what_else_was_on_the_table(
    root: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """MOS-SAFE-084: "`input.series_considered[]` MUST include rejected series with their
    reasons. The most common silent clinical failure in radiology AI is analysing the
    wrong reconstruction; the record must show what else was on the table."

    So the study here has TWO series -- a CT that is selected and an SR that is not -- and
    the record must carry both, with a reason on the loser.
    """
    files, study_uid, series_uid = _synthetic_ct(tmp_path / "src2")
    decoy = "1.2.826.0.1.3680043.8.498.99999999999999999999999999999999"
    gateway = FakeGateway(
        [
            SeriesSummary(
                study_instance_uid=study_uid,
                series_instance_uid=series_uid,
                modality="CT",
                n_instances=len(files),
            ),
            SeriesSummary(
                study_instance_uid=study_uid,
                series_instance_uid=decoy,
                modality="SR",
                n_instances=1,
            ),
        ],
        files,
    )
    queue = PostgresJobQueue(root)
    created = repo.create_job_queued(
        root, queue, JobSpec(study_instance_uid=study_uid, capability_ids=CAPS)
    )
    cfg = RunnerConfig(
        dsn=pg_dsn,
        work_root=tmp_path / "work2",
        worker_id=make_worker_id("audit-test-2"),
        reclaim_on_poll=False,
    )
    WorkerRunner(
        cfg, conn=root, deps=WorkerDeps(gateway=gateway, work_root=cfg.work_root)
    ).run_once()

    job = repo.get_job(root, created.job_id)
    assert job is not None and job["state"] == "COMPLETED", job

    with tenant_context(DEFAULT_TENANT_ID):
        record = provenance_db.get_for_job(root, created.job_id)[0]

    considered = {s["series_instance_uid"]: s for s in record["input"]["series_considered"]}
    assert series_uid in considered and considered[series_uid]["selected"] is True
    assert decoy in considered, "the rejected series is absent from the record"
    assert considered[decoy]["selected"] is False
    assert considered[decoy]["rejection_reason"], (
        "MOS-SAFE-084: a rejected series with no reason is exactly the silence this "
        "field exists to abolish"
    )
    # MOS-SAFE-083 section B: consumed is a subset of the SELECTED considered entries.
    assert set(record["input"]["series_consumed"]) <= {
        uid for uid, s in considered.items() if s["selected"]
    }


def test_the_record_carries_no_phi(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SAFE-086 and chapter 9's acceptance criterion 19.

    Both halves: a recursive KEY scan for the named PHI attributes, and a VALUE scan for
    the fixture's own synthetic patient name, study description and accession number --
    which `_synthetic_ct` writes into every instance precisely so that a leak has
    something recognisable to leak.
    """
    with tenant_context(DEFAULT_TENANT_ID):
        record = provenance_db.get_for_job(root, completed_job["job_id"])[0]

    prov.assert_no_phi(
        record,
        forbidden_values=frozenset(
            {
                "TEST^SYNTHETIC",
                "SYNTHETIC-0001",
                "SYNTHETIC CHEST PHANTOM",
                "ACC-SYNTHETIC-1",
                "19700101",
                "REF^SYNTHETIC",
            }
        ),
    )
    # And the blunt instrument, because a nested structure the walker did not expect
    # would pass the walk: the serialised document must not contain the strings at all.
    blob = json.dumps(record)
    for value in ("TEST^SYNTHETIC", "SYNTHETIC-0001", "SYNTHETIC CHEST PHANTOM"):
        assert value not in blob, f"the provenance record leaked {value!r}"


def test_the_provenance_chain_verifies_and_a_forged_record_is_caught(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SAFE-090 / MOS-SAFE-091: the per-tenant record chain, and the daily check that
    "MUST emit `provenance.chain.broken` plus an `AuditEvent` on any mismatch".

    Emitting those is the scheduler's job; what is tested here is that the mismatch is
    DETECTED, and that the detection names the record.

    BOTH directions are asserted, because either one alone is satisfied by a broken
    verifier: a verifier that always returns None passes "a genuine record verifies", and
    one that always returns a row passes "a forgery is caught".

    THE COPY IS `deepcopy`, AND THAT IS LOAD-BEARING.
    This test previously did `forged = dict(row["record"])` and then assigned through
    THREE levels of nesting -- `forged["execution"]["runtime"]["worker_version"]`.
    `dict()` is a shallow copy, so `forged["execution"] is row["record"]["execution"]`,
    and the "forgery" mutated the record the `finally` block then wrote back as the
    pristine original. The restore was a no-op that re-applied the forgery; the third
    check reported `record does not hash to the recorded record_hash` and the failure
    read exactly like a canonicalisation bug in `provenance_verify_chain`.

    It was not one. Write (`result_provenance_chain` trigger) and verify
    (`provenance_verify_chain`) both hash `jcs_canonical(record - 'record_hash')`, the
    same expression in the same migration, and the FIRST check below passed on every run
    that reported this failure -- which on its own rules out a write/verify divergence.
    The damage was not confined to this test either: the `finally` block re-enabled the
    immutability trigger over a row it had left forged, so every later reader of this
    tenant's chain inherited a broken one.

    So the restore is now asserted rather than assumed (`_stored` below). A future
    regression fails AT the restore, naming it, instead of surfacing three statements
    later as a phantom canonicalisation bug.
    """

    def _stored() -> dict[str, Any]:
        return dict(
            root.execute(
                "SELECT record FROM result_provenance WHERE result_id = %s",
                (row["result_id"],),
            ).fetchone()["record"]
        )

    with tenant_context(DEFAULT_TENANT_ID):
        assert provenance_db.verify_chain(root) is None, (
            "a genuine, untouched chain did not verify"
        )

    row = root.execute(
        "SELECT result_id, sequence_no, record FROM result_provenance "
        "WHERE job_public_id = %s ORDER BY sequence_no LIMIT 1",
        (completed_job["job_id"],),
    ).fetchone()
    pristine = copy.deepcopy(dict(row["record"]))

    # Forged as the superuser with the immutability trigger off -- the same threat model
    # as the audit tamper test: the chain exists to make an administrator's edit visible,
    # not to prevent it.
    root.execute("ALTER TABLE result_provenance DISABLE TRIGGER result_provenance_immutable")
    forged = copy.deepcopy(pristine)
    # ONE BYTE. `0.2.0` -> `0.2.1` in the nested runtime block: the smallest edit an
    # administrator could make, deep enough that a shallow copy would alias it, and a
    # change a reviewer reading the record would never notice by eye.
    version = forged["execution"]["runtime"]["worker_version"]
    forged["execution"]["runtime"]["worker_version"] = version[:-1] + (
        "1" if version[-1] != "1" else "2"
    )
    assert forged != pristine, "the forgery changed nothing"
    assert pristine["execution"]["runtime"]["worker_version"] == version, (
        "forging mutated the pristine copy: the deepcopy above is not deep"
    )
    assert (
        len(json.dumps(forged, sort_keys=True))
        == len(json.dumps(pristine, sort_keys=True))
    ), "the forgery was meant to be one byte, not a resize"

    try:
        root.execute(
            "UPDATE result_provenance SET record = %s WHERE result_id = %s",
            (psycopg.types.json.Jsonb(forged), row["result_id"]),
        )
        with tenant_context(DEFAULT_TENANT_ID):
            broken = provenance_db.verify_chain(root)
        assert broken is not None, "a forged provenance record verified clean"
        assert broken["sequence_no"] == row["sequence_no"]
        assert broken["reason"] == "record does not hash to the recorded record_hash"
    finally:
        root.execute(
            "UPDATE result_provenance SET record = %s WHERE result_id = %s",
            (psycopg.types.json.Jsonb(pristine), row["result_id"]),
        )
        root.execute(
            "ALTER TABLE result_provenance ENABLE TRIGGER result_provenance_immutable"
        )

    # The restore is asserted, not assumed. This is the statement the old shallow copy
    # would have failed, and it names the cause instead of blaming the verifier.
    assert _stored() == pristine, (
        "the restore did not put the original record back: the forged document is still "
        "in the table, and the chain is now genuinely broken for every later reader"
    )
    with tenant_context(DEFAULT_TENANT_ID):
        assert provenance_db.verify_chain(root) is None, (
            "the restored, genuine record was still reported as forged"
        )


def test_one_generated_object_resolves_back_to_its_whole_run(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SEC-155's last row, read backwards: `Job -> Result -> generated SOPInstanceUIDs`
    is a join, and a reviewer holding ONE object needs it to run in the other direction.

    This is the question a radiologist's screenshot generates. `generated_object_uids`
    with its GIN index is what makes it a lookup instead of a jsonb scan over every row
    (MOS-STORE-279).
    """
    with tenant_context(DEFAULT_TENANT_ID):
        record = provenance_db.get_for_job(root, completed_job["job_id"])[0]
        sop_uid = record["outputs"][0]["sop_instance_uids"][0]
        found = provenance_db.find_by_object_uid(root, sop_uid)

    assert found is not None, f"no record claims {sop_uid}"
    assert found["job_id"] == completed_job["job_id"]
    assert found["record"]["input"]["series_consumed"] == [
        completed_job["series_instance_uid"]
    ]


def test_the_api_surfaces_the_full_record_and_the_trail(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """MOS-SAFE-087 / MOS-STORE-278: the record is reachable on the job resource, and the
    weeks 1-2 flat field set is still there beside it.

    Both, deliberately. `medos/web/ohif-extension/src/core/provenance.js` and
    tests/e2e/test_demo.py read the flat keys; widening a record by breaking its readers
    is not widening it.
    """
    view = repo.job_view(root, completed_job["job_id"])
    assert view is not None
    result = view["results"][0]

    # The weeks 1-2 projection, unchanged.
    for key in ("job_id", "study_instance_uid", "series_consumed", "capability_id",
                "capability_version", "preprocessing_version", "worker_version",
                "runtime_version", "generated_objects"):
        assert key in result["provenance"], f"the narrow field set lost {key}"

    # The MOS-SAFE-083 document, nested under it.
    record = result["provenance"]["record"]
    assert record is not None, "MOS-STORE-278: a result is observable with no provenance"
    assert record["input"]["series_considered"]
    assert record["outputs"]
    assert record["execution"]["preprocessing_specs"]

    # MOS-SEC-155 on the same surface: the trail that says who caused it.
    trail = view["audit_trail"]
    assert any(r["action"] == "job.create" for r in trail)
    assert any(r["action"] == "result.create" for r in trail)
    assert all(r["job_public_id"] == completed_job["job_id"] for r in trail)


def test_the_record_and_the_results_row_agree_about_the_input_digest(
    root: psycopg.Connection[Any], completed_job: dict[str, Any]
) -> None:
    """The redundancy of MOS-STORE-279 is deliberate, which makes agreement testable.

    `results.input_uid_digest` and the record's `sha256_of_sorted_sop_uid_list` describe
    the same set through two code paths. A reader comparing them is checking the WRITER,
    and that only works while the two are computed identically -- which is why
    `medos.worker.result_rows.provenance_uid_digest` delegates to `repo._uid_digest`
    rather than reimplementing it.
    """
    row = root.execute(
        """
        SELECT r.input_uid_digest, r.input_instance_count, p.record
          FROM results r
          JOIN result_provenance p ON p.result_id = r.id
          JOIN jobs j ON j.id = r.job_id
         WHERE j.public_id = %s
        """,
        (completed_job["job_id"],),
    ).fetchone()
    assert row is not None
    instances = row["record"]["input"]["instances"]
    assert instances["sha256_of_sorted_sop_uid_list"] == row["input_uid_digest"]
    assert instances["count"] == row["input_instance_count"]
