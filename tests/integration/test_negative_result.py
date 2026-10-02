# SPDX-License-Identifier: Apache-2.0
"""A detector that ran and found nothing is a RESULT, not a failure.

THE DISTINCTION UNDER TEST, and it is clinical rather than technical
--------------------------------------------------------------------
Three things a radiologist must be able to tell apart from the outside:

    "we analysed this study and found nothing"   -> COMPLETED, Finding.present == False
    "we could not analyse this study"            -> REJECTED,  class clinical_rejection
    "something in the platform broke"            -> FAILED,    class system_failure

Until the fix this module pins, the first collapsed into the third. `plan_outputs` raised
`no_label_map_in_bundle` when no capability returned a label map and `all_segments_empty`
when every segment it did return was empty, and `step_write_dicom` built a SEG
unconditionally while ignoring `jobs.requested_outputs`. So the MODAL outcome of any
detection capability -- a chest CT with no nodules on it -- terminated the job `FAILED`,
which `MOS-EXEC-014` defines as "this study should have been analysed and the platform
could not do it". That is the same category error as reporting an absent study as `FAILED`
rather than `REJECTED`, which is already a Tier A release gate, and it has the same
consequence: a radiologist either chases an IT ticket or, worse, assumes the study was
cleared (`MOS-UI-030b`: "the absence of a result is not a negative finding").

WHY THESE TESTS RUN THE WHOLE PIPELINE
---------------------------------------
Every claim here is about a job's TERMINAL STATE and about the rows a client reads, so
none of them can be made against `plan_outputs` alone. Each test enqueues a real job,
claims it with a real `WorkerRunner` against a real Postgres, and reads back `jobs`,
`results` and `result_dicom_objects`. The PACS is a fake, for the reason
`tests/integration/test_worker.py` gives for its own: the tests are about what the WORKER
does, and the fake really receives files and really answers "does this series exist".

The capabilities in section 0 are test-local and injected through `WorkerDeps.registry`,
which is the supported composition seam (`MOS-REL-020`, the same one
`medos/services/lung_nodule/registry.py` uses). They are not mocks: `_Detector` really reads the
Hounsfield array it is handed and really thresholds it, so "found nothing" is a fact about
pixels and not a hard-coded return value. A capability that returned a canned outcome
would let every assertion below pass while nothing was examined.

Spec: MOS-EXEC-001, MOS-EXEC-014, MOS-EXEC-016a, MOS-EXEC-023, MOS-IMG-098, MOS-IMG-109,
MOS-IMG-121, MOS-IMG-142, MOS-IMG-143, MOS-SVC-011, MOS-SVC-089, MOS-SVC-090,
MOS-SAFE-014, MOS-SAFE-083, MOS-UI-030b.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pydicom
import pytest
from medos.capabilities import REGISTRY
from medos.capabilities.base import (
    CapabilityContext,
    CapabilityMetadata,
    FailureMode,
    require_source_grid,
)
from medos.core.bundle import CapabilityOutcome, CodedConcept, Finding, LabelMap, Measurement
from medos.core.geometry import CanonicalVolume
from medos.db import repo
from medos.db.queue import PostgresJobQueue, make_worker_id
from medos.db.repo import JobSpec
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    bind_current_tenant,
    reset_current_tenant,
)
from medos.dicomweb.gateway import FetchedSeries, SeriesSummary
from medos.worker.runner import RunnerConfig, RunOutcome, WorkerRunner
from medos.worker.steps import WorkerDeps
from psycopg.rows import dict_row
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

STUDY_UID = "1.2.826.0.1.3680043.8.498.20000000000000000000000000000001"

# The concept every test capability below segments and measures. Resolved out of the
# dictionary rather than spelled here as a literal would be -- `MOS-IMG-112` forbids the
# writer inventing a code, and a test that invented one would be testing a code path the
# platform refuses.
LUNG = CodedConcept(scheme="SCT", code="39607008", meaning="Lung structure")
VOLUME_CONCEPT = CodedConcept(scheme="SCT", code="118565006", meaning="Volume")


# =====================================================================================
# 0. Test-local capabilities
# =====================================================================================
_FAILURE_MODES = (
    FailureMode(
        id="TEST-FM-001",
        text="A fixture. Not validated for anything and never to be deployed.",
        detection="Its capability_id is not in medos.capabilities.REGISTRY.",
        mitigation="None. This capability exists only inside tests/integration.",
    ),
)


def _metadata(capability_id: str, output_kinds: tuple[str, ...]) -> CapabilityMetadata:
    return CapabilityMetadata(
        capability_id=capability_id,
        version="1.0.0",
        method_class="deterministic_algorithm",
        method_summary="Counts connected voxels above a Hounsfield threshold.",
        output_kinds=output_kinds,
        computation_geometry="source",
        input_constraints="None; the fixture volume is synthetic.",
        not_validated_for=("Everything. This is a test fixture, not a clinical method.",),
        known_failure_modes=_FAILURE_MODES,
        parameters={"threshold_hu": "per instance"},
        operating_point=None,
    )


@dataclass(frozen=True)
class _Detector:
    """A detection capability that really looks, and honestly reports finding nothing.

    `threshold_hu` is the whole fixture: the synthetic series carries one bright cube, so
    a threshold below it finds a candidate and a threshold above it finds none, from the
    SAME pixels and the SAME code path. That is what makes "found nothing" a measured
    fact here rather than a branch the test selected.

    `present` follows `MOS-SVC-089`'s rule for a deterministic capability: no `score`, no
    `operating_point`, and `present` set EXPLICITLY. `MOS-SVC-090` then makes the negative
    reportable -- "absent findings MAY be reported with `present: false`" carrying the
    same coded concept and the same evidence fields as a present one -- which is exactly
    what a viewer needs to tell "analysed, negative" from "not analysed".

    `declares_seg` exists so one class covers both halves of `MOS-SVC-011`: a capability
    that declares SEG in `output_kinds` (`MOS-SAFE-014`) has promised a label map, and a
    capability that declares none has promised nothing.
    """

    capability_id: str = "test_detector"
    version: str = "1.0.0"
    threshold_hu: float = 0.0
    declares_seg: bool = True
    return_label_map: bool = True

    @property
    def metadata(self) -> CapabilityMetadata:
        kinds = ("SEG", "SR", "MEASUREMENT") if self.declares_seg else ("SR", "MEASUREMENT")
        return _metadata(self.capability_id, kinds)

    def applicable(self, vol: CanonicalVolume) -> str | None:
        return None

    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome:
        require_source_grid(vol, ctx)
        hu = ctx.source.hu_array
        assert hu is not None
        mask = (hu >= self.threshold_hu).astype(np.uint8)
        n_voxels = int(mask.sum())
        label_map = (
            LabelMap(array=mask, segments=(LUNG,)) if self.return_label_map else None
        )
        delta_r, delta_c = ctx.source.pixel_spacing_mm
        # MOS-IMG-016's projected slice spacing, not SliceThickness: the volume of a voxel
        # is a property of the grid the measurement is computed on (MOS-IMG-039).
        voxel_ml = float(delta_r * delta_c * ctx.source.delta_s_mm) / 1000.0
        return CapabilityOutcome(
            capability_id=self.capability_id,
            findings=(
                Finding(
                    kind="lung",
                    # MOS-SVC-089: deterministic, so `present` is stated and not derived
                    # from a score this method does not compute.
                    present=n_voxels > 0,
                    score=None,
                    measurements=(
                        Measurement(
                            name=VOLUME_CONCEPT,
                            value=n_voxels * voxel_ml,
                            unit="ml",
                        ),
                    ),
                ),
            ),
            label_map=label_map,
            source_sop_instance_uids=tuple(ctx.source.sop_instance_uids),
        )


# =====================================================================================
# Fixtures: a real Postgres, a fake PACS, a synthetic CT with one bright cube
# =====================================================================================
@pytest.fixture()
def wconn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
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


class FakeGateway:
    """The five methods the executor calls, and a record of what was STOWed.

    `stored` is the assertion surface for "no SEG was written": a test that only read the
    database would pass against a worker that wrote a SEG into the PACS and forgot to row
    it, which is the more dangerous of the two failures.
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

    def assert_stored(self, *args: Any, **kwargs: Any) -> set[str]:
        return set()

    def kinds_stored(self) -> set[str]:
        """`{"SEG", "SR"}` read off the Modality of the bytes that were actually sent."""
        return {
            str(pydicom.dcmread(str(p), stop_before_pixels=True).Modality)
            for p in self.stored
        }


def _synthetic_ct(dest: Path, n: int = 6, *, bright_hu: int = 500) -> tuple[list[Path], str]:
    """A valid single-frame CT series with one bright cube in the middle.

    Valid enough for `build_canonical_volume`: a supported SOP class, monotonic
    `ImagePositionPatient` along the slice normal, one orientation, one grid, HU after the
    Modality LUT. The background is -1024 HU (air) and a 4x4x2 cube is `bright_hu`, so a
    detector thresholding above `bright_hu` measures zero from real pixels.
    """
    dest.mkdir(parents=True, exist_ok=True)
    series_uid, frame_uid = generate_uid(), generate_uid()
    paths: list[Path] = []
    for k in range(n):
        pixels = np.zeros((16, 16), dtype=np.int16)
        if k in (2, 3):
            pixels[6:10, 6:10] = bright_hu + 1024  # + intercept -1024 -> bright_hu
        ds = Dataset()
        ds.file_meta = FileMetaDataset()
        ds.file_meta.MediaStorageSOPClassUID = CTImageStorage
        ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = STUDY_UID
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        ds.PatientName = "TEST^SYNTHETIC"  # synthetic, not PHI; never logged
        ds.PatientID = "SYNTHETIC"
        # highdicom's SR constructor reads these off the first evidence instance, and
        # MOS-IMG-090 lists them "copy-or-empty" -- a zero-length Type 2 is the correct
        # value when the source has none, but the source has to HAVE the element.
        ds.PatientBirthDate = ""
        ds.PatientSex = ""
        ds.StudyDate = "20240101"
        ds.StudyTime = "120000"
        ds.StudyID = ""
        ds.AccessionNumber = ""
        ds.ReferringPhysicianName = ""
        ds.ImagePositionPatient = [0.0, 0.0, float(k) * 2.0]
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.PixelSpacing = [1.0, 1.0]
        ds.SliceThickness = 2.0
        ds.Rows, ds.Columns = 16, 16
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
        ds.PixelRepresentation = 1
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.RescaleIntercept = -1024.0
        ds.RescaleSlope = 1.0
        ds.RescaleType = "HU"
        ds.PixelData = pixels.tobytes()
        path = dest / f"{ds.SOPInstanceUID}.dcm"
        ds.save_as(str(path), enforce_file_format=True)
        paths.append(path)
    return paths, series_uid


def _run(
    wconn: psycopg.Connection[Any],
    pg_dsn: str,
    tmp_path: Path,
    *,
    capability: Any,
    requested_outputs: tuple[str, ...] = ("SEG", "SR"),
    files: Sequence[Path] | None = None,
    series_uid: str | None = None,
) -> tuple[RunOutcome | None, str, FakeGateway]:
    """Enqueue one job for `capability` and run it to a terminal state."""
    if files is None or series_uid is None:
        files, series_uid = _synthetic_ct(tmp_path / "src")
    cid = capability.capability_id
    created = repo.create_job_queued(
        wconn,
        PostgresJobQueue(wconn),
        JobSpec(
            study_instance_uid=STUDY_UID,
            capability_ids=(cid,),
            requested_outputs=requested_outputs,
            max_attempts=1,
        ),
    )
    gateway = FakeGateway(
        series=[SeriesSummary(STUDY_UID, series_uid, "CT", len(files))], files=files
    )
    deps = WorkerDeps(
        gateway=gateway,
        work_root=tmp_path / "work",
        registry={**REGISTRY, cid: capability},
    )
    cfg = RunnerConfig(
        dsn=pg_dsn,
        work_root=tmp_path / "work",
        worker_id=make_worker_id("negres"),
        lease_seconds=300,
        heartbeat_seconds=60,
        reclaim_on_poll=False,
    )
    runner = WorkerRunner(cfg, conn=wconn, deps=deps)
    return runner.run_once(), created.job_id, gateway


def _job_row(wconn: psycopg.Connection[Any], job_id: str) -> dict[str, Any]:
    uid = repo.get_job_uuid(wconn, job_id)
    row = wconn.execute(
        "SELECT state, reject_reason_code, failure_class, failure_code "
        "FROM jobs WHERE id = %s",
        (uid,),
    ).fetchone()
    assert row is not None
    return dict(row)


# =====================================================================================
# 1. "We analysed this study and found nothing" is COMPLETED
# =====================================================================================
def test_a_detector_that_found_nothing_completes_with_a_negative_result(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The modal outcome of a detector. It must be an ANSWER, not an incident.

    The detector thresholds at 2000 HU on a volume whose brightest voxel is 500 HU, so it
    examines every voxel and admits none. Before the fix this job ended `FAILED` with
    `failure_class = 'invalid_result_bundle'`, `failure_code = 'all_segments_empty'` --
    `MOS-EXEC-016a` renders that as `class: "system_failure"`, `MOS-EXEC-015` lets it
    consume retry budget and page someone, and `MOS-UI-033` puts a trace id on the screen
    for a support ticket. None of that is true of a normal chest CT.
    """
    outcome, job_id, gateway = _run(
        wconn, pg_dsn, tmp_path, capability=_Detector(threshold_hu=2000.0)
    )

    assert outcome is not None
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: {outcome.failure_code or outcome.reject_reason_code}"
    )
    row = _job_row(wconn, job_id)
    assert row["state"] == "COMPLETED"
    # Not a rejection and not a failure. Both columns null is the machine-readable form of
    # "nothing declined this study and nothing broke".
    assert row["reject_reason_code"] is None
    assert row["failure_code"] is None and row["failure_class"] is None

    # The clinical content: an explicit negative finding, MOS-SVC-090's shape.
    results = repo.list_results(wconn, job_id)
    assert len(results) == 1
    findings = results[0]["findings"]
    assert [f["present"] for f in findings] == [False]
    assert findings[0]["kind"] == "lung"

    # ... carrying the SAME coded concept and the same evidence fields as a positive one
    # would (MOS-SVC-090), so a consumer switches on `present` and not on a shape change.
    uid = repo.get_job_uuid(wconn, job_id)
    measurements = wconn.execute(
        "SELECT concept_scheme, concept_code, value FROM result_measurements m "
        "JOIN results r ON r.id = m.result_id WHERE r.job_id = %s",
        (uid,),
    ).fetchall()
    assert [(m["concept_scheme"], m["concept_code"]) for m in measurements] == [
        ("SCT", "118565006")
    ]
    assert float(measurements[0]["value"]) == 0.0

    # The DICOM half. No SEG -- there is no region to outline -- and an SR, which is the
    # object that says "these images were analysed" in the PACS, where the reading
    # happens (chapter 18 HZ-13: "absence is indistinguishable from negative").
    assert gateway.kinds_stored() == {"SR"}
    assert {o["object_kind"] for o in results[0]["dicom_objects"]} == {"SR"}
    assert outcome.seg_sop_instance_uid is None, (
        "a SOPInstanceUID was reported for a SEG that was never written; it resolves to "
        "nothing in the PACS"
    )
    assert outcome.sr_sop_instance_uid is not None

    # NEVER AS SILENCE (MOS-SVC-011). The SEG's absence is named, with a reason code.
    assert [(o["kind"], o["reason_code"]) for o in outcome.outputs_omitted] == [
        ("SEG", "all_segments_empty")
    ]


def test_a_capability_with_nothing_to_draw_at_all_completes(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The other shape of the same outcome: a capability that returns NO label map.

    `pleural_effusion` is the platform's own instance of it -- CONTRACT.md section 7 makes
    it return `present=False` with a `not_implemented` note, no label map and no
    measurement -- so this runs shipped code and not a fixture. A job requesting only that
    capability used to die in `plan_outputs` with `no_label_map_in_bundle`, which means
    the platform could not run its own third capability on its own as a job.

    It now completes, and it writes NO DICOM object at all: there is no region to outline
    and no number to report, so there is neither a SEG nor a TID 1500 measurement group.
    Both absences are named with a reason code rather than left as silence
    (`MOS-SVC-011`), and the `Result` carries the finding.

    Note what `present=False` means HERE and what it does not: `METADATA.not_validated_for`
    says in so many words that it MUST NOT be read as the absence of a pleural effusion.
    The job being COMPLETED is a claim about the platform, not about the patient.
    """
    outcome, job_id, gateway = _run(
        wconn, pg_dsn, tmp_path, capability=REGISTRY["pleural_effusion"]
    )

    assert outcome is not None
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: {outcome.failure_code or outcome.reject_reason_code}"
    )
    assert gateway.stored == []
    assert outcome.seg_sop_instance_uid is None and outcome.sr_sop_instance_uid is None
    assert [(o["kind"], o["reason_code"]) for o in outcome.outputs_omitted] == [
        ("SEG", "no_label_map"),
        ("SR", "no_measurements"),
    ]
    results = repo.list_results(wconn, job_id)
    assert [f["present"] for f in results[0]["findings"]] == [False]
    assert results[0]["dicom_objects"] == []


def test_a_negative_with_a_number_in_it_still_reaches_the_pacs(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The line between "no DICOM object" and "an SR that says zero", and why it is there.

    A capability whose negative carries a NUMBER -- "candidate count: 0", "burden: 0 ml",
    which is what `medos/services/lung_nodule` reports -- has one TID 1501 measurement group, so
    it gets an SR, and that SR is the only artefact of a negative read that reaches the
    hospital's own viewer. Chapter 18's HZ-13 is exactly this: "absence is
    indistinguishable from negative", and the PACS is where the reading happens.

    A capability that computed no number at all has no measurement group and therefore no
    TID 1500 document. That half is FORCED, not chosen, and it is REPORTED in
    `medos/medos/writer/identity.py`: TID 1500 permits a Measurement Report with no Imaging
    Measurements container and MOS-IMG-109 marks both group templates conditional, but
    highdicom refuses to build one and MOS-IMG-094 forbids assembling it by hand.
    """
    outcome, _job_id, gateway = _run(
        wconn, pg_dsn, tmp_path, capability=_Detector(threshold_hu=2000.0)
    )
    assert outcome is not None and outcome.terminal_state == "COMPLETED"

    sr = pydicom.dcmread(str(gateway.stored[0]))
    assert str(sr.Modality) == "SR"
    assert str(sr.SOPClassUID) == "1.2.840.10008.5.1.4.1.1.88.34"  # MOS-IMG-060
    # Root concept: DCM 126000 "Imaging Measurement Report" (MOS-IMG-109).
    assert str(sr.ConceptNameCodeSequence[0].CodeValue) == "126000"

    codes = {str(item.ConceptNameCodeSequence[0].CodeValue) for item in sr.ContentSequence}
    assert "121049" in codes, "TID 1204 language item is a MUST in MOS-IMG-109"
    assert "121058" in codes, "procedure reported is a MUST in MOS-IMG-109"
    assert "111028" in codes, "the TID 1600 Image Library is a MUST in MOS-IMG-109"

    # The number, and it is zero. MOS-IMG-110: a value a clinician could act on is a NUM
    # content item with a coded name and a UCUM unit, never free text. Scoped to the
    # Imaging Measurements container (DCM 126010) because the TID 1600 Image Library
    # carries NUM items of its own for acquisition context.
    nums = _measurement_nums(sr)
    assert len(nums) == 1
    measured = nums[0].MeasuredValueSequence[0]
    assert float(measured.NumericValue) == 0.0
    assert str(measured.MeasurementUnitsCodeSequence[0].CodingSchemeDesignator) == "UCUM"

    # MOS-IMG-121/122: evidence is the source instances and nothing else -- no SEG was
    # written, so referencing one would point at an instance absent from the destination.
    assert not _references_seg(sr)


# =====================================================================================
# 2. "Analysed, negative" versus "not analysed" -- what a client actually sees
# =====================================================================================
def test_a_negative_result_is_distinguishable_from_a_rejection(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The whole point, asserted on the rows a client reads and on nothing else.

    Two jobs on the SAME study with the SAME capability. The first analyses it and finds
    nothing; the second is handed a study whose series list is empty, so nothing was
    analysed at all. The discriminators a viewer switches on are the terminal state and
    the presence of a `Result`, and they differ in every position:

        analysed, negative -> COMPLETED, reject_reason_code null, one results row whose
                              findings carry `present: false`
        not analysed       -> REJECTED,  reject_reason_code = 'no_eligible_series',
                              zero results rows

    `MOS-EXEC-016` maps the second onto RFC 9457 `class: "clinical_rejection"` and
    `MOS-UI-030b` makes a surface print "the absence of a result is not a negative
    finding" beside it -- a sentence that would be a lie printed beside the first.
    """
    analysed, negative_job, _gw = _run(
        wconn, pg_dsn, tmp_path, capability=_Detector(threshold_hu=2000.0)
    )
    assert analysed is not None and analysed.terminal_state == "COMPLETED"

    # The same capability on a study the platform cannot read: no eligible series.
    # A DIFFERENT StudyInstanceUID, because MOS-EXEC-053 derives the idempotency key from
    # (tenant, service, study, series, requested_outputs, parameters) and re-enqueueing
    # the identical tuple would deduplicate onto the job above rather than create a
    # second one.
    detector = _Detector(threshold_hu=2000.0)
    created = repo.create_job_queued(
        wconn,
        PostgresJobQueue(wconn),
        JobSpec(
            study_instance_uid=STUDY_UID[:-1] + "2",
            capability_ids=(detector.capability_id,),
            max_attempts=1,
        ),
    )
    empty_gateway = FakeGateway(series=[], files=[])
    runner = WorkerRunner(
        RunnerConfig(
            dsn=pg_dsn,
            work_root=tmp_path / "work2",
            worker_id=make_worker_id("negres"),
            lease_seconds=300,
            heartbeat_seconds=60,
            reclaim_on_poll=False,
        ),
        conn=wconn,
        deps=WorkerDeps(
            gateway=empty_gateway,
            work_root=tmp_path / "work2",
            registry={**REGISTRY, detector.capability_id: detector},
        ),
    )
    not_analysed = runner.run_once()
    assert not_analysed is not None

    negative_row = _job_row(wconn, negative_job)
    rejected_row = _job_row(wconn, created.job_id)

    assert (negative_row["state"], rejected_row["state"]) == ("COMPLETED", "REJECTED")
    assert negative_row["reject_reason_code"] is None
    assert rejected_row["reject_reason_code"] == "no_eligible_series"
    # Neither is a FAILURE. The rejection is an answer too (MOS-EXEC-014).
    assert negative_row["failure_code"] is None
    assert rejected_row["failure_code"] is None

    assert len(repo.list_results(wconn, negative_job)) == 1
    assert repo.list_results(wconn, created.job_id) == []
    # And the negative result SAYS it is negative, rather than being an empty result that
    # a consumer would have to infer a polarity from.
    assert [f["present"] for f in repo.list_results(wconn, negative_job)[0]["findings"]] == [
        False
    ]


# =====================================================================================
# 3. `requested_outputs` is honoured: SR only, and no failure for want of a SEG
# =====================================================================================
def test_an_sr_only_request_succeeds_without_a_seg(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """`requested_outputs = {SR}` on a capability that DID find something.

    This is the half that has nothing to do with negatives: chapter 5 section 5.6.2 makes
    `jobs.requested_outputs` the column that "drives the step plan's write/store steps",
    and `step_write_dicom` ignored it entirely -- it built a SEG for every job and the SR
    referenced it. A caller who asked for a report and not a drawing got both, and a
    caller whose capability had no drawing to give got a failed job.

    The detector thresholds at 0 HU, so the bright cube IS found: the measurement is
    non-zero and `present` is true. What must not appear is a SEG.
    """
    outcome, job_id, gateway = _run(
        wconn,
        pg_dsn,
        tmp_path,
        capability=_Detector(threshold_hu=0.0),
        requested_outputs=("SR",),
    )

    assert outcome is not None
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: {outcome.failure_code or outcome.reject_reason_code}"
    )
    results = repo.list_results(wconn, job_id)
    assert [f["present"] for f in results[0]["findings"]] == [True], (
        "the fixture proves nothing if the detector found nothing here"
    )

    assert gateway.kinds_stored() == {"SR"}
    assert {o["object_kind"] for o in results[0]["dicom_objects"]} == {"SR"}
    assert [(o["kind"], o["reason_code"]) for o in outcome.outputs_omitted] == [
        ("SEG", "not_requested")
    ]

    # The measurement survives the SEG's absence. MOS-IMG-120 binds its value to the
    # Result, and losing the drawing must not lose the number.
    sr = pydicom.dcmread(str(gateway.stored[0]))
    nums = _measurement_nums(sr)
    assert len(nums) == 1, "the SR carries no measurement for a region it measured"
    assert str(nums[0].ConceptNameCodeSequence[0].CodeValue) == "118565006"

    # MOS-IMG-122: an SR that "references instances absent from the destination is a
    # battery failure". No SEG was STOWed, so nothing in this document may point at one.
    assert not _references_seg(sr), (
        "the SR references a SEG SOPInstanceUID that this job never wrote; every viewer "
        "that resolves it finds nothing"
    )


def _walk(dataset: Dataset) -> Iterator[Dataset]:
    for item in getattr(dataset, "ContentSequence", []) or []:
        yield item
        yield from _walk(item)


def _measurement_nums(sr: Dataset) -> list[Dataset]:
    """Every NUM item under the Imaging Measurements container (DCM 126010).

    Scoped deliberately: the TID 1600 Image Library carries NUM items of its own for
    acquisition context, and counting those would make "the SR reports one measurement"
    pass on a document that reports none.
    """
    return [
        item
        for group in getattr(sr, "ContentSequence", []) or []
        if str(group.ConceptNameCodeSequence[0].CodeValue) == "126010"
        for item in _walk(group)
        if str(item.ValueType) == "NUM"
    ]


def _references_seg(sr: Dataset) -> bool:
    """True if any content item or evidence entry names the SEG SOP Class."""
    seg_class = "1.2.840.10008.5.1.4.1.1.66.4"
    for item in _walk(sr):
        for ref in getattr(item, "ReferencedSOPSequence", []) or []:
            if str(getattr(ref, "ReferencedSOPClassUID", "")) == seg_class:
                return True
    for study in getattr(sr, "CurrentRequestedProcedureEvidenceSequence", []) or []:
        for series in getattr(study, "ReferencedSeriesSequence", []) or []:
            for ref in getattr(series, "ReferencedSOPSequence", []) or []:
                if str(getattr(ref, "ReferencedSOPClassUID", "")) == seg_class:
                    return True
    return False


# =====================================================================================
# 4. "Something in the platform broke" is still FAILED
# =====================================================================================
def test_a_capability_that_declares_a_seg_and_produces_none_still_fails(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The real defect the old `no_label_map_in_bundle` was catching, still caught.

    Making the writer total could easily have made this disappear, and it is the one case
    that must not: a capability declaring `SEG` in its `output_kinds` (`MOS-SAFE-014`) has
    promised a label map, and returning none means it did not produce the output it
    declared. `MOS-SVC-011` requires that be "reported as a per-capability rejection,
    never as silence"; `MOS-IMG-142` classes a service returning something other than what
    it declared as "a service defect, not a clinical outcome".

    The check moved from the writer to `validate_bundle`, which is where the capability's
    DECLARATION is reachable -- the writer sees a bundle and cannot tell a detector's
    honest negative from a segmenter's failure without it.
    """
    broken = _Detector(
        capability_id="test_broken_segmenter", declares_seg=True, return_label_map=False
    )
    outcome, job_id, gateway = _run(wconn, pg_dsn, tmp_path, capability=broken)

    assert outcome is not None
    assert outcome.terminal_state == "FAILED"
    # MOS-EXEC-016a maps `invalid_result_bundle` onto `class: "system_failure"`, which is
    # the correct thing to say about software that did not do what it declared.
    assert outcome.failure_class == "invalid_result_bundle"
    assert outcome.failure_code == "declared_output_not_produced"
    row = _job_row(wconn, job_id)
    assert row["state"] == "FAILED"
    assert row["reject_reason_code"] is None

    # MOS-IMG-142: "no DICOM object may be written".
    assert gateway.stored == []
    assert repo.list_results(wconn, job_id) == []


def test_the_writer_still_refuses_a_segment_it_cannot_code(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """A platform-side fault on the write path, which must stay `FAILED`.

    Making `plan_outputs` total is a change to what it refuses, and the risk of such a
    change is that it stops refusing something it should. `MOS-IMG-112` is the sharpest
    case: "a concept with no `capability_concepts` row MUST NOT be written. The writer
    MUST raise rather than invent a code." A guessed SNOMED code in a SEG is a clinical
    assertion nobody made.

    The capability here returns a NON-empty segment under a code the dictionary does not
    carry, so the writer has something to draw and no legal way to name it -- which is a
    platform-side refusal and not a statement about the study. `MOS-EXEC-016a` maps
    `dicom_write_failed` onto `class: "transport_failure"` for the operator; what matters
    here is that the job is `FAILED` and not `COMPLETED`, and that nothing reached the PACS.
    """

    @dataclass(frozen=True)
    class _UncodedDetector(_Detector):
        capability_id: str = "test_uncoded"

        def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome:
            base = _Detector.run(self, vol, ctx)
            assert base.label_map is not None
            uncoded = CodedConcept(scheme="SCT", code="999999999", meaning="Not a code")
            return CapabilityOutcome(
                capability_id=self.capability_id,
                findings=base.findings,
                label_map=LabelMap(array=base.label_map.array, segments=(uncoded,)),
                source_sop_instance_uids=base.source_sop_instance_uids,
            )

    outcome, job_id, gateway = _run(
        wconn, pg_dsn, tmp_path, capability=_UncodedDetector(threshold_hu=0.0)
    )

    assert outcome is not None
    assert outcome.terminal_state == "FAILED"
    assert outcome.failure_code == "unmapped_segment_concept"
    assert gateway.stored == []
    assert repo.list_results(wconn, job_id) == []


def test_a_capability_that_declares_no_seg_and_produces_none_is_not_a_defect(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The control for the test above: the SAME missing label map, declared honestly.

    Without this pair the check is untestable as a check -- a test that only asserted the
    failure would pass against a rule that failed every job with no label map, which is
    the behaviour being removed.
    """
    honest = _Detector(
        capability_id="test_honest_detector",
        threshold_hu=2000.0,
        declares_seg=False,
        return_label_map=False,
    )
    outcome, job_id, gateway = _run(wconn, pg_dsn, tmp_path, capability=honest)

    assert outcome is not None
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: {outcome.failure_code}"
    )
    assert gateway.kinds_stored() == {"SR"}
    assert len(repo.list_results(wconn, job_id)) == 1
