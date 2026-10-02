# SPDX-License-Identifier: Apache-2.0
"""GATE CHECK `idempotency-three-surface` — docs/spec/15-delivery.md §15.1.2, release 0.1.0.

    "exactly one `results` row per capability, exactly one `SeriesInstanceUID` per
     generated DICOM object kind in the PACS, exactly one completion event, including the
     crash-between-store-and-complete variant"

THREE SURFACES, BECAUSE ANY ONE OF THEM CAN BE RIGHT WHILE THE PLATFORM IS WRONG
--------------------------------------------------------------------------------
A `UNIQUE (job_id, capability_id)` does not stop a second STOW. A deterministic UID does
not stop a second `results` row. Both can be right while the event stream emits two
completions and a downstream consumer books the study twice. The gate names all three
because each is a different mechanism -- a database constraint, a UID derivation and a
transition guard -- and a release can ship with two of the three.

THE TRIGGER MATTERS AS MUCH AS THE SURFACES
-------------------------------------------
Two triggers are exercised, and they fail differently:

  * a REPEAT SUBMISSION -- the second press of the button. Dedup happens at admission, on
    the platform-derived key (`MOS-EXEC-053`), and no client header participates.
  * a CRASH BETWEEN `store_dicom` AND the terminal commit. Dedup cannot happen at
    admission here, because there is nothing left in the database to deduplicate against:
    the PACS holds the objects and the platform's record of them is gone. What must
    happen instead is that the same job derives the same UIDs (`MOS-IMG-062`), finds its
    own objects already in the archive, and RECONCILES rather than re-running the write
    (`MOS-IMG-080`, `MOS-EXEC-059`). If it minted new UIDs instead, the study would end up
    with four derived series and two SEGs a radiologist has to choose between.

The crash is reproduced BY ITS END STATE and not by timing a `docker kill`, and that is a
deliberate choice rather than a shortcut. The whole attempt takes a few seconds on this
stack and the window between the store and the terminal commit is a fraction of that, so a
timed kill is a coin flip: `tests/e2e/test_demo.py` has the timed version and it
legitimately skips when it loses the race. Emptying the job tables while leaving the
archive alone produces exactly the state a crash in that window leaves behind, every time.
A release gate that is a coin flip is not a gate.

THIS MODULE RUNS LAST AND IT IS DESTRUCTIVE. The crash arm truncates the deployment's job
tables. `tests/gate/conftest.py`'s collection hook pins the order so that every other
check has already read what it needs.

Spec: MOS-API-026, MOS-API-029, MOS-EXEC-013, MOS-EXEC-020, MOS-EXEC-053, MOS-EXEC-054,
MOS-EXEC-059, MOS-IMG-062, MOS-IMG-063, MOS-IMG-080, MOS-IMG-081.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from .conftest import (
    CAPABILITIES,
    JOB_TABLES,
    SOP_CLASS_SEG,
    SOP_CLASS_SR_COMPREHENSIVE_3D,
    Api,
    Ingested,
    derived_series,
    qido_instances,
    tag,
    why,
)

pytestmark = pytest.mark.gate_0_1_0


def _job_uuid(db: Any, public_id: str) -> Any:
    row = db.execute("SELECT id FROM jobs WHERE public_id = %s", (public_id,)).fetchone()
    assert row is not None, f"no job row for {public_id}"
    return row["id"]


def _assert_three_surfaces(
    db: Any, dicomweb: Any, job_public_id: str, study_uid: str, label: str
) -> dict[str, str]:
    """The three surfaces of §15.1.2, asserted together. Returns {kind: series uid}.

    Together and not as three tests, in this helper, because the two TRIGGERS are the
    tests: running the same three assertions against a replay and against a reconciled
    crash is the check, and splitting them six ways would hide which trigger failed.
    """
    job_uuid = _job_uuid(db, job_public_id)

    # ---- surface 1: exactly one `results` row per capability ---------------------------
    rows = db.execute(
        "SELECT capability_id, count(*) AS n FROM results WHERE job_id = %s "
        "GROUP BY capability_id ORDER BY capability_id",
        (job_uuid,),
    ).fetchall()
    assert [r["capability_id"] for r in rows] == sorted(CAPABILITIES), (
        f"{label}: results rows cover {[r['capability_id'] for r in rows]}, expected "
        f"{sorted(CAPABILITIES)}"
    )
    duplicated = {r["capability_id"]: r["n"] for r in rows if r["n"] != 1}
    assert not duplicated, f"{label}: more than one results row per capability: {duplicated}"

    # ---- surface 2: exactly one SeriesInstanceUID per object kind, IN THE PACS ----------
    # Read from the archive and not from `result_dicom_objects`: the database's account of
    # what it stored is precisely the thing under test.
    series_rows = derived_series(dicomweb, study_uid)
    by_uid = {str(tag(r, "0020000E")) for r in series_rows}
    assert len(by_uid) == 2, (
        f"{label}: expected exactly 2 MedicalOS-minted series in the study (one SEG, one "
        f"SR), the PACS holds {len(by_uid)}: {sorted(by_uid)}"
    )
    kinds: dict[str, list[str]] = {}
    for series_uid in sorted(by_uid):
        instances = qido_instances(dicomweb, study_uid, series_uid)
        assert len(instances) == 1, (
            f"{label}: generated series {series_uid} holds {len(instances)} instances"
        )
        kinds.setdefault(str(tag(instances[0], "00080016")), []).append(series_uid)
    assert sorted(kinds) == sorted([SOP_CLASS_SEG, SOP_CLASS_SR_COMPREHENSIVE_3D]), (
        f"{label}: generated SOP classes are {sorted(kinds)}"
    )
    for sop_class, uids in kinds.items():
        assert len(uids) == 1, (
            f"{label}: SOP class {sop_class} appears under {len(uids)} SeriesInstanceUIDs "
            f"(MOS-IMG-063)"
        )

    # ---- surface 3: exactly one completion event ---------------------------------------
    events = {
        r["event_type"]: r["n"]
        for r in db.execute(
            "SELECT event_type, count(*) AS n FROM job_events WHERE job_id = %s "
            "GROUP BY event_type",
            (job_uuid,),
        ).fetchall()
    }
    assert events.get("job.completed") == 1, (
        f"{label}: {events.get('job.completed')} job.completed events, expected exactly 1 "
        f"({events})"
    )
    assert "job.rejected" not in events
    assert "job.failed" not in events

    out = {
        SOP_CLASS_SEG: "SEG",
        SOP_CLASS_SR_COMPREHENSIVE_3D: "SR",
    }
    return {out[sop_class]: uids[0] for sop_class, uids in kinds.items()}


# ======================================================================================
# TRIGGER 1 — the second press of the button
# ======================================================================================
def test_idempotency_three_surface__a_repeat_submission_creates_no_second_job(
    api: Api, completed_job: dict[str, Any], primary_study: Ingested, db: Any
) -> None:
    """MOS-API-026: a repeat MUST NOT create a second resource.

    `200` and not `202` is the whole signal, and it comes from the PLATFORM-derived key
    (`MOS-EXEC-053`) rather than from a client header -- this request sends none.
    """
    before = db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]

    r = api.post_job(primary_study.study_instance_uid)
    assert r.status_code == 200, (
        f"expected an idempotent replay 200, got {r.status_code}: {r.text[:400]}"
    )
    assert r.headers.get("MedicalOS-Idempotent-Replay") == "true", (
        "the replay is not advertised in the response headers; a client cannot tell a "
        "replay from a fresh accept"
    )
    assert r.json()["job_id"] == completed_job["job_id"]

    after = db.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"]
    assert after == before, f"the replay created {after - before} extra job row(s)"
    print(f"[I-replay] 200 MedicalOS-Idempotent-Replay=true, {before} job rows unchanged")


def test_idempotency_three_surface__all_three_surfaces_hold_after_a_replay(
    completed_job: dict[str, Any], primary_study: Ingested, db: Any, dicomweb: Any
) -> None:
    """The gate's three surfaces, measured after the repeat submission above."""
    uids = _assert_three_surfaces(
        db,
        dicomweb,
        completed_job["job_id"],
        primary_study.study_instance_uid,
        label="after replay",
    )
    print(
        f"[I-surfaces] 1 results row per capability, "
        f"SEG={uids['SEG']} SR={uids['SR']} (1 instance each), 1 completion event"
    )


def test_idempotency_three_surface__a_client_key_does_not_change_the_derived_identity(
    api: Api, completed_job: dict[str, Any], primary_study: Ingested, db: Any
) -> None:
    """MOS-API-029 / MOS-EXEC-054: the client's `Idempotency-Key` MUST NOT feed UID derivation.

    The failure it prevents is concrete. If a client-settable string entered the seed, two
    callers submitting different studies under the same key would mint the same
    SOPInstanceUID, and a PACS resolves that by overwriting one patient's segmentation
    with another's.

    Submitting the same body under an arbitrary header value must therefore land on the
    SAME job -- proving the header reached neither the dedup key nor the UID seed -- while
    still being echoed and persisted, which MOS-API-029 separately requires.
    """
    client_key = "a-client-chosen-key-that-must-not-matter"
    r = requests.post(
        f"{api.base_url}/api/v1/jobs",
        json={
            "study_instance_uid": primary_study.study_instance_uid,
            "capabilities": CAPABILITIES,
        },
        headers=api.headers(**{"Idempotency-Key": client_key}),
        timeout=60,
    )
    assert r.status_code == 200, f"{r.status_code}: {r.text[:400]}"
    assert r.json()["job_id"] == completed_job["job_id"], (
        "a client-chosen Idempotency-Key produced a different job; the header has reached "
        "the dedup key, and therefore the UID seed (MOS-EXEC-054)"
    )
    assert r.headers.get("MedicalOS-Idempotency-Key") == client_key

    row = db.execute(
        "SELECT idempotency_key, request_idempotency_key FROM jobs WHERE public_id = %s",
        (completed_job["job_id"],),
    ).fetchone()
    assert row["idempotency_key"].startswith("ik_"), (
        f"the derived key is {row['idempotency_key']!r}, which is not a platform-derived "
        f"ik_ value"
    )
    assert row["idempotency_key"] != client_key
    print("[I-clientkey] echoed and persisted, and not in the derived identity")


# ======================================================================================
# TRIGGER 2 — the crash between store_dicom and the terminal commit
#
# DESTRUCTIVE. Runs last. See the module docstring for why the end state and not a kill.
# ======================================================================================
def test_idempotency_three_surface__all_three_hold_after_a_crash_between_store_and_complete(
    api: Api, completed_job: dict[str, Any], primary_study: Ingested, db: Any, dicomweb: Any
) -> None:
    """MOS-EXEC-059 / MOS-IMG-080 / MOS-IMG-081, deterministically and with no race.

    The state a crash between `store_dicom` and the terminal commit leaves behind is
    exactly this: the PACS holds the generated objects and the database knows nothing
    about them. The archive is NOT touched -- chapter 9's default-DENY table forbids
    MedicalOS deleting a study or an instance and `MOS-EXEC-061` makes recovery past
    `store_dicom` forward-only BECAUSE nothing can be un-stored, so there is no delete
    anywhere in `medos/medos/` and none here either.

    `TRUNCATE` and not `DELETE`: `job_events` carries a BEFORE DELETE trigger that raises
    (`MOS-EXEC-013`, append-only), and TRUNCATE does not fire row-level triggers -- so the
    append-only guarantee stays true for application code while the harness can still
    reach the state. If TRUNCATE were available to application code the guarantee would be
    worthless; it is not, and nothing in `medos/medos/` issues one.
    """
    seg_before = next(
        o for r in completed_job["results"] for o in r["dicom_objects"]
        if o["object_kind"] == "SEG"
    )
    sr_before = next(
        o for r in completed_job["results"] for o in r["dicom_objects"]
        if o["object_kind"] == "SR"
    )
    derived_before = {
        str(tag(row, "0020000E"))
        for row in derived_series(dicomweb, primary_study.study_instance_uid)
    }
    assert derived_before == {
        seg_before["series_instance_uid"],
        sr_before["series_instance_uid"],
    }

    # THE CRASH, reproduced by its end state.
    db.execute(f"TRUNCATE {JOB_TABLES} CASCADE")
    assert derived_series(dicomweb, primary_study.study_instance_uid), (
        "the archive lost the generated objects when the job tables were emptied; the "
        "crash state this arm needs is 'objects present, rows absent'"
    )

    r = api.post_job(primary_study.study_instance_uid)
    assert r.status_code == 202, (
        f"expected a FRESH job after the job tables were emptied, got {r.status_code} "
        f"(a 200 replay would mean the dedup key survived the crash, which is not the "
        f"state under test): {r.text[:400]}"
    )
    job = api.wait_for_terminal(r.json()["job_id"])
    assert job["state"] == "COMPLETED", why(job)

    # ---- the same objects, not new ones ------------------------------------------------
    objects = {o["object_kind"]: o for res in job["results"] for o in res["dicom_objects"]}
    for kind, before in (("SEG", seg_before), ("SR", sr_before)):
        assert objects[kind]["sop_instance_uid"] == before["sop_instance_uid"], (
            f"the recovered attempt minted a NEW {kind} SOPInstanceUID; the study now "
            f"holds two {kind}s a radiologist has to choose between (MOS-IMG-062)"
        )
        assert objects[kind]["series_instance_uid"] == before["series_instance_uid"]

    derived_after = {
        str(tag(row, "0020000E"))
        for row in derived_series(dicomweb, primary_study.study_instance_uid)
    }
    assert derived_after == derived_before, (
        f"the archive gained or lost derived series across the recovery: "
        f"{sorted(derived_before)} -> {sorted(derived_after)}"
    )

    # ---- and it RECONCILED rather than re-storing --------------------------------------
    steps = {
        s["step_key"]: s
        for s in db.execute(
            "SELECT step_key, status, skip_reason FROM job_steps WHERE job_id = %s",
            (_job_uuid(db, job["job_id"]),),
        ).fetchall()
    }
    skipped = {k for k, s in steps.items() if s["status"] == "skipped"}
    assert skipped, (
        "nothing was skipped: the worker rewrote objects the archive already held "
        "(MOS-EXEC-059 / MOS-IMG-080, 'the platform MUST reconcile against the archive "
        "instead of re-running inference')"
    )
    assert {"write_dicom", "store_dicom"} & skipped, (
        f"neither write_dicom nor store_dicom was skipped; skipped={sorted(skipped)}"
    )
    for key in sorted(skipped):
        # MOS-EXEC-020: a skip always carries a reason; a reasonless skip is unstorable.
        assert steps[key]["skip_reason"], f"{key} was skipped with no reason"

    # ---- the three surfaces, after the crash and the recovery --------------------------
    _assert_three_surfaces(
        db,
        dicomweb,
        job["job_id"],
        primary_study.study_instance_uid,
        label="after crash-between-store-and-complete",
    )
    print(
        f"[I-crash] recovered on a fresh job; skipped={sorted(skipped)} "
        f"reason={steps[sorted(skipped)[0]]['skip_reason']!r}; "
        f"SEG/SR UIDs unchanged, {len(derived_after)} derived series, 1 completion event"
    )
