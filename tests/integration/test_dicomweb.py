# SPDX-License-Identifier: Apache-2.0
"""Live DICOMweb tests against the PACS, reached through the DICOM Gateway.

CONTRACT.md §11: "Integration tests may assume Postgres and Orthanc from
`medos/deploy/compose/docker-compose.yml`." This module does not start a container -- it
connects to whatever `MEDOS_DICOMWEB_URL` points at and skips when nothing answers.

THE DEFAULT IT CONNECTS TO MOVED, AND THAT IS REGISTER ENTRY 70
---------------------------------------------------------------
It used to be `http://127.0.0.1:8042/dicom-web`, Orthanc's own root on a published host
port. That port does not exist and cannot: `orthanc` joins only the `internal: true`
`pacs` network, and Docker binds no host port for such a container however loudly the
compose file asks. So every test in this module was structurally unreachable and skipped
on `orthanc` forever -- ten of the twenty-nine skips register entry 70 counts. MOS-REL-012:
"An unexecuted acceptance criterion means the requirement is not satisfied, whatever the
code does."

The default is now the Gateway's DICOMweb root, which is where `medos-worker` and
`tests/gate/` have pointed all along (MOS-DATA-002 makes the Gateway the only route to the
PACS; MOS-DATA-015 and MOS-DATA-017 say so for the viewer and the worker). Two shape
differences follow and both are asserted by being used: the path carries a tenant segment
(MOS-DATA-007) and every request presents a scoped bearer token, never a PACS credential
(MOS-DATA-005, MOS-DATA-017). `tests/_support/stack.py` owns both values so this module
cannot drift from the probe that guards it.

Each test creates its OWN study from a synthetic phantom and removes it afterwards.
Depending on data that happens to already be in the archive makes a test that passes on
one machine and fails on the next, and MOS-IMG-152's set equality is meaningless against
an archive whose contents you did not write.

The assertions are the imaging contract's, not HTTP's:
  * MOS-IMG-084 -- a STOW-RS succeeded only when the status is 200 AND
    `FailedSOPSequence` is empty.
  * MOS-IMG-152 -- completeness is SET equality of `SOPInstanceUID`s, not count equality.
  * MOS-DATA-023 -- a series retrieve streams; it is decoded incrementally.
  * MOS-DATA-058 -- metadata retrieval returns no inline pixel data.
  * MOS-IMG-085 -- re-storing an object that is already present is safe.

Spec: MOS-DATA-007, MOS-DATA-023, MOS-DATA-058, MOS-IMG-082, MOS-IMG-084, MOS-IMG-085,
MOS-IMG-152.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from medos.core.errors import SeriesSelectionError, TransportFailure
from medos.dicomweb import DicomWebGateway, GatewayConfig, PacsCredentials
from medos.dicomweb.client import DicomWebClient

from tests._support import stack
from tests._support.skips import skip_infra

DICOMWEB_URL = stack.dicomweb_url()
N_INSTANCES = 6
ROWS = COLS = 48


def _credentials() -> PacsCredentials:
    """What this harness is allowed to present, and in which order.

    The bearer is the scoped Gateway token of MOS-DATA-017 and is the normal case. The
    user/password pair is kept because `MEDOS_DICOMWEB_URL` may still be pointed straight
    at an Orthanc with `ORTHANC_AUTH_ENABLED=true` -- that is what the override is for --
    and because `medos.dicomweb` is the ONLY module in the tree permitted to hold either
    (CONTRACT.md §1), so the harness must not grow a second way to authenticate.
    """
    user = os.environ.get("MEDOS_DICOMWEB_USER") or None
    if user:
        return PacsCredentials(user=user, password=os.environ.get("MEDOS_DICOMWEB_PASSWORD"))
    return PacsCredentials(bearer_token=stack.dicomweb_token())


@pytest.fixture(scope="module")
def gateway() -> Iterator[DicomWebGateway]:
    config = GatewayConfig(
        base_url=DICOMWEB_URL,
        credentials=_credentials(),
        timeout_s=30.0,
    )
    gw = DicomWebGateway(config)
    if not gw.available():
        gw.close()
        skip_infra(
            f"no DICOMweb origin at {DICOMWEB_URL} (set MEDOS_DICOMWEB_URL)",
            dependency="orthanc",
        )
    try:
        yield gw
    finally:
        gw.close()


def _write_phantom(directory: Path) -> tuple[str, str, list[Path]]:
    """A tiny CT series on disk. Returns (study_uid, series_uid, paths)."""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

    directory.mkdir(parents=True, exist_ok=True)
    study_uid, series_uid, frame_uid = (generate_uid() for _ in range(3))
    paths: list[Path] = []
    rng = np.random.default_rng(20260914)
    for k in range(N_INSTANCES):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = CTImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        meta.ImplementationClassUID = generate_uid()
        ds = Dataset()
        ds.file_meta = meta
        ds.SOPClassUID = CTImageStorage
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.FrameOfReferenceUID = frame_uid
        ds.Modality = "CT"
        ds.PatientID = "MEDOS-DICOMWEB-TEST"
        ds.PatientName = "Phantom^DicomWeb"
        ds.SeriesNumber = 1
        ds.InstanceNumber = k + 1
        ds.Rows = ROWS
        ds.Columns = COLS
        ds.PixelSpacing = [1.0, 1.0]
        ds.SliceThickness = 2.0
        ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        ds.ImagePositionPatient = [0.0, 0.0, float(k) * 2.0]
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleSlope = 1.0
        ds.RescaleIntercept = -1024.0
        ds.RescaleType = "HU"
        # Random pixels so the parts differ: a multipart decoder that mixes parts up is
        # invisible when every instance carries the same bytes.
        ds.PixelData = rng.integers(0, 2000, (ROWS, COLS), dtype=np.uint16).tobytes()
        path = directory / f"{ds.SOPInstanceUID}.dcm"
        pydicom.dcmwrite(path, ds, enforce_file_format=True)
        paths.append(path)
    return str(study_uid), str(series_uid), paths


@pytest.fixture
def stored_series(gateway: DicomWebGateway, tmp_path: Path):
    """Store a phantom series in the archive and remove it afterwards."""
    study_uid, series_uid, paths = _write_phantom(tmp_path / "src")
    expected = {p.stem for p in paths}
    results = gateway.store_files(paths, study_instance_uid=study_uid)
    try:
        yield study_uid, series_uid, paths, expected, results
    finally:
        _delete_study(study_uid)


def _delete_study(study_uid: str) -> None:
    """Remove the test study through Orthanc's NATIVE REST API, from inside the container.

    There is no DELETE verb in DICOMweb's QIDO/WADO/STOW triad, so cleanup cannot go
    through `DicomWebGateway` -- and adding a delete to the gateway to make a test tidy
    would put a destructive PACS operation into production code that nothing else needs.
    Chapter 9's default-DENY table forbids MedicalOS deleting a study or an instance, and
    MOS-EXEC-061 makes recovery past `store_dicom` forward-only precisely because nothing
    can be un-stored; so there is no delete anywhere in `medos/medos/` and this capability stays
    outside the platform, where it belongs.

    The native API is not exposed to the host and MOS-DATA-006 is why: "A deployment in
    which any other container can open a TCP connection to the PACS port is non-conformant,
    whether or not it has a credential." `stack.orthanc_native_rest` runs the request
    INSIDE `medos-orthanc`, over that container's own loopback -- the PACS talking to
    itself, which that sentence does not reach.

    THIS USED TO BE `_best_effort_delete` AND IT SWALLOWED EVERY EXCEPTION. That is why
    it could keep "working" against an address that had not existed for two releases. A
    teardown that silently does nothing leaves the archive holding this run's phantoms,
    and `test_qido_returns_the_instance_set` then asserts set equality against a series it
    did not write. The helper classifies its failures and routes them to `skip_infra`, so
    an archive whose state nobody established stops the run instead of colouring it green.
    """
    lookup = stack.orthanc_native_rest(
        [stack.NativeCall("POST", "/tools/lookup", study_uid)]
    )[0]
    if not lookup.ok:
        skip_infra(
            f"Orthanc answered /tools/lookup with HTTP {lookup.status}; the archive's "
            "state after this test cannot be established",
            dependency="orthanc-rest",
        )
    rows = lookup.json() or []
    deletes = [
        stack.NativeCall("DELETE", f"/studies/{row['ID']}")
        for row in rows
        if row.get("Type") == "Study"
    ]
    if deletes:
        stack.orthanc_native_rest(deletes)


# =====================================================================================
def test_preflight_and_config_summary(gateway: DicomWebGateway):
    gateway.preflight()
    summary = gateway.config_summary()
    assert summary["base_url"] == DICOMWEB_URL.rstrip("/")
    assert summary["tenancy"] == "absent_in_this_slice"  # CONTRACT.md §0


def test_stow_is_judged_by_mos_img_084_not_by_the_http_status(stored_series):
    _study, _series, paths, expected, results = stored_series
    assert results, "store_files returned no batch result"
    for result in results:
        assert result.http_status == 200
        assert result.failed == ()
        assert result.ok
        assert result.bytes_sent > 0
    referenced = {u for r in results for u in r.referenced_sop_instance_uids}
    assert referenced == expected


def test_qido_returns_the_instance_set(gateway: DicomWebGateway, stored_series):
    study_uid, series_uid, _paths, expected, _results = stored_series
    assert gateway.instance_uids(study_uid, series_uid) == expected
    assert gateway.series_exists(study_uid, series_uid)
    summaries = {s.series_instance_uid: s for s in gateway.list_series(study_uid)}
    assert series_uid in summaries
    assert summaries[series_uid].modality == "CT"
    assert summaries[series_uid].n_instances == N_INSTANCES


def test_series_summary_carries_no_phi(gateway: DicomWebGateway, stored_series):
    """CONTRACT.md §11: the selection record is UIDs, a modality and a count."""
    study_uid, _series, _paths, _expected, _results = stored_series
    for summary in gateway.list_series(study_uid):
        blob = repr(summary)
        assert "Phantom" not in blob
        assert "MEDOS-DICOMWEB-TEST" not in blob


def test_series_metadata_carries_no_inline_pixel_data(
    gateway: DicomWebGateway, stored_series
):
    """MOS-DATA-058: triage reads metadata only, and metadata is not pixels."""
    study_uid, series_uid, _paths, _expected, _results = stored_series
    rows = gateway.series_metadata(study_uid, series_uid)
    assert len(rows) == N_INSTANCES
    for row in rows:
        pixel_data = row.get("7FE00010", {})
        assert "Value" not in pixel_data  # a BulkDataURI reference is fine


def test_retrieve_series_matches_the_bytes_that_were_stored(
    gateway: DicomWebGateway, stored_series, tmp_path: Path
):
    """MOS-IMG-152 set equality, and byte equality of every instance.

    Byte equality matters beyond tidiness: chapter 4 derives output UIDs from a digest of
    the input pixels (MOS-IMG-063), so an origin that transcodes on retrieve silently
    changes every derived UID.
    """
    study_uid, series_uid, paths, expected, _results = stored_series
    fetched = gateway.fetch_series(study_uid, series_uid, tmp_path / "dst")
    assert set(fetched.sop_instance_uids) == expected
    assert fetched.n_instances == N_INSTANCES
    original = {p.stem: p.read_bytes() for p in paths}
    for path in fetched.paths:
        assert path.read_bytes() == original[path.stem]


def test_retrieve_streams_rather_than_buffering(
    gateway: DicomWebGateway, stored_series
):
    """MOS-DATA-023: the parts arrive one at a time, not as one body.

    Asserted by consuming exactly one part and stopping: if the iterator only yielded
    after the whole body was read, the first `next()` would still have to consume every
    instance, and a partial consumption could not close the response mid-stream.
    """
    study_uid, series_uid, _paths, expected, _results = stored_series
    creds = _credentials()
    client = DicomWebClient(
        DICOMWEB_URL,
        user=creds.user,
        password=creds.password,
        bearer_token=creds.bearer_token,
        timeout_s=30.0,
    )
    try:
        stream = client.iter_series_instances(study_uid, series_uid, chunk_bytes=4096)
        first = next(stream)
        assert first.content[128:132] == b"DICM"
        assert first.content_type.startswith("application/dicom")
        stream.close()  # the generator's finally closes the HTTP response
        # ... and the archive is unchanged by a half-read retrieve.
        assert client.instance_uids(study_uid, series_uid) == expected
    finally:
        client.close()


def test_assert_stored_is_the_mos_img_084_plus_152_gate(
    gateway: DicomWebGateway, stored_series
):
    study_uid, series_uid, _paths, expected, results = stored_series
    present = gateway.assert_stored(results, study_uid, series_uid, sorted(expected))
    assert present == expected
    # MOS-IMG-083: an instance in the derived series that this job did not write is a
    # failure, not a curiosity.
    with pytest.raises(TransportFailure) as excinfo:
        gateway.assert_stored(
            results, study_uid, series_uid, sorted(expected)[: N_INSTANCES - 1]
        )
    assert excinfo.value.reason_code == "dicomweb_series_incomplete"
    assert excinfo.value.detail["unexpected_sop_instance_uids"]


def test_restoring_an_existing_object_is_safe(gateway: DicomWebGateway, stored_series):
    """MOS-IMG-085: an object already present MUST NOT be re-stored destructively."""
    study_uid, series_uid, paths, expected, _results = stored_series
    again = gateway.store_files(paths, study_instance_uid=study_uid)
    assert all(r.ok for r in again)
    assert gateway.instance_uids(study_uid, series_uid) == expected


def test_fetch_series_refuses_an_empty_series(gateway: DicomWebGateway, tmp_path: Path):
    """A series that is not there must be a classified refusal, never an empty success.

    WHICH classification depends on who is answering, and the difference is a
    requirement rather than an accident. Asked of a bare Orthanc, "this study does not
    exist" and "this is not a DICOMweb root" are the same 404 and the only honest answer
    is a `TransportFailure`. Asked of the Gateway -- which is the route since register
    entry 70 -- MOS-DATA-013 makes a missing study `STUDY_NOT_FOUND`, and
    `medos.dicomweb.client` deliberately turns that into a `SeriesSelectionError`: a
    CLINICAL rejection (`no_eligible_series`), not a transport fault.

    That separation is MOS-EXEC-014 and the `rejection-distinct` release gate. A study the
    archive does not hold is a job that must terminate REJECTED with a machine-readable
    reason, not FAILED with a retry budget burned against a PACS that is perfectly well.
    So this test asserts the refusal AND that it carries one of the two correct
    classifications -- widening it to "raises something" would throw away the very
    distinction the new route earns.
    """
    with pytest.raises((TransportFailure, SeriesSelectionError)) as excinfo:
        gateway.fetch_series("1.2.826.0.1.3680043.2.1125.999", "1.2.3.4.5", tmp_path)
    assert excinfo.value.reason_code in {
        # A bare PACS cannot tell the two apart, so both stay admissible.
        "dicomweb_series_incomplete",
        "dicomweb_root_not_found",
        # The Gateway can, and says so. MOS-DATA-013.
        "no_eligible_series",
    }
    if isinstance(excinfo.value, SeriesSelectionError):
        assert excinfo.value.detail["upstream_code"] == "STUDY_NOT_FOUND"


def test_transport_failures_are_classified_not_crashes():
    """CONTRACT.md §9: a PACS that is down is `transport_failure`, retryable, 502."""
    dead = DicomWebGateway(
        GatewayConfig(base_url="http://127.0.0.1:9/dicom-web", timeout_s=2.0)
    )
    try:
        assert dead.available() is False
        with pytest.raises(TransportFailure) as excinfo:
            dead.preflight()
        problem = excinfo.value.to_problem()
        assert excinfo.value.reason_code == "dicomweb_unreachable"
        assert problem["class"] == "transport_failure"
        assert problem["status"] == 502
        assert problem["retryable"] is True
    finally:
        dead.close()


def test_a_wrong_dicomweb_root_is_reported_as_such():
    """A root that is not a DICOMweb root must fail preflight, however it answers.

    THIS TEST USED TO BE ABLE TO SKIP ITSELF, AND DID.
    It asked `gw.available()` first and, if the bogus root answered at all, took a
    `skip_environment` -- "this machine cannot produce the condition". On any deployment
    following MOS-OPS-005 that is EVERY machine: the one origin the viewer, the API and
    the PACS share is an nginx whose SPA fallback returns `200 text/html` for every
    unrecognised path. Pointed at that origin the test skipped under both switches,
    permanently and green, and `available()` really did return True for an HTML page.

    A test whose subject is "a wrong root is reported" cannot be allowed to opt out when
    the root answers: that IS the interesting case, and it is the exact shape of both
    incidents in this project's history -- a URL that answers while not being the thing.
    `DicomWebClient.preflight` now checks what came back rather than only the status, so
    both an unreachable root and a chatty one land here and the test always asserts.

    THE BOGUS ROOT IS NOW APPENDED, NOT SUBSTITUTED, AND THE CREDENTIAL IS REAL.
    It used to be `DICOMWEB_URL.rsplit("/", 1)[0] + "/definitely-not-dicom-web"`, which
    replaced the last path segment. Against the Gateway that segment is the TENANT
    (MOS-DATA-007), so the old construction asked "what happens to a request for someone
    else's tenant" and got MOS-DATA-009's 403 -- a real and separately tested rule, but
    not this test's subject. Appending keeps the tenant correct and makes the ROOT wrong,
    which is the thing being measured; and presenting the same credential every other test
    presents keeps the answer from being the trivial 401 that any path would earn.
    """
    root = DICOMWEB_URL + "/definitely-not-dicom-web"
    gw = DicomWebGateway(
        GatewayConfig(base_url=root, credentials=_credentials(), timeout_s=10.0)
    )
    try:
        assert gw.available() is False, (
            f"{root} is not a DICOMweb root and available() must say so. If this origin "
            "answers, it is answering with something that is not QIDO-RS."
        )
        with pytest.raises(TransportFailure) as excinfo:
            gw.preflight()
        assert excinfo.value.reason_code in {
            "dicomweb_root_not_found",
            "dicomweb_http_error",
            "dicomweb_unreachable",
            # An origin with a catch-all: a 2xx that is not a QIDO-RS array.
            "dicomweb_bad_response",
        }
    finally:
        gw.close()
