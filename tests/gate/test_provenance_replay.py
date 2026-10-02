# SPDX-License-Identifier: Apache-2.0
"""GATE CHECK `provenance-replay` — docs/spec/15-delivery.md §15.1.2, release 0.1.0.

    "the provenance record names the consumed series, the pinned versions and every stored
     object"

Three clauses, three tests, plus one that is the reason the check is called *replay*
rather than *provenance*: the record must be enough to IDENTIFY the inputs again, which
means the series it names must still be the series the archive holds and the objects it
names must still be the objects in the PACS. A provenance record that is internally
consistent and points at nothing is a log line.

`MOS-STORE-278` is what makes this structural rather than cosmetic: "a result without
provenance MUST NOT be observable through any API". So the record is read where a reviewer
would read it -- off `GET /api/v1/jobs/{id}` -- and then cross-checked against the archive
through the Gateway and against the deployment's own `result_provenance` rows.

Spec: MOS-STORE-278, MOS-SAFE-086, MOS-EXEC-053, MOS-IMG-062, CONTRACT.md §§5, 10.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from .conftest import Ingested, derived_series, qido_instances, tag

pytestmark = pytest.mark.gate_0_1_0

#: CONTRACT.md §10's field set. `generated_objects` is deliberately absent: it is required
#: PER OBJECT, not per capability, and in this release `lung_segmentation` owns both
#: generated objects while `emphysema_laa` and `pleural_effusion` contribute measurements
#: into the SAME SR. Demanding an object from every capability would make the platform's
#: correct shape -- one SEG and one SR per job, not per capability -- look like missing
#: provenance, and would push an implementer to mint a needless second SR.
REQUIRED_FIELDS = (
    "job_id",
    "study_instance_uid",
    "series_consumed",
    "capability_id",
    "capability_version",
    "preprocessing_version",
    "worker_version",
    "runtime_version",
    "started_at",
    "finished_at",
)


def test_provenance_replay__the_record_names_the_consumed_series(
    completed_job: dict[str, Any], primary_study: Ingested, dicomweb: Any
) -> None:
    """"The series ACTUALLY consumed" -- the phrase CONTRACT.md §10 sets in bold.

    Named is not enough. The named series is re-queried through the Gateway and its
    instance count compared with the count the record claims, so that a record naming a
    series that no longer exists, or that holds a different number of instances than the
    run consumed, fails here rather than being discovered by whoever tries to reproduce
    the result.
    """
    assert completed_job["results"], "a COMPLETED job with no results"
    for result in completed_job["results"]:
        prov = result["provenance"]
        assert prov, f"{result['capability_id']}: no provenance (MOS-STORE-278)"
        assert list(prov["series_consumed"]) == [primary_study.series_instance_uid], (
            f"{result['capability_id']}: provenance names "
            f"{list(prov['series_consumed'])}, the job consumed "
            f"{[primary_study.series_instance_uid]}"
        )
        assert prov["study_instance_uid"] == primary_study.study_instance_uid
        assert prov["job_id"] == completed_job["job_id"]
        assert prov["instances_consumed"] == primary_study.n_instances

    # The archive's own answer, so that "names the consumed series" means something that
    # can still be fetched.
    consumed = completed_job["results"][0]["provenance"]["series_consumed"][0]
    rows = qido_instances(dicomweb, primary_study.study_instance_uid, consumed)
    assert len(rows) == primary_study.n_instances, (
        f"the provenance names series {consumed}, which the PACS now reports as holding "
        f"{len(rows)} instances against the {primary_study.n_instances} the record claims "
        f"were consumed; the run is not replayable from this record"
    )
    print(
        f"[P-series] {len(completed_job['results'])} result(s), each naming 1 consumed "
        f"series with {primary_study.n_instances} instances, re-queried from the archive"
    )


def test_provenance_replay__the_record_names_the_pinned_versions(
    completed_job: dict[str, Any], db: Any
) -> None:
    """Every field of CONTRACT.md §10's set is present and non-empty, per result.

    Non-empty and not merely present: a `null` `worker_version` is a record that cannot be
    replayed, and an absent key and an empty one fail for the same reason.

    The API's projection is then compared with `result_provenance` in the deployment's own
    database. The API is a view; the row is the record `MOS-STORE-278` is about. If the
    two disagree about the pinned version of the code that produced a clinical artefact,
    one of them is lying to a reviewer.
    """
    for result in completed_job["results"]:
        prov = result["provenance"]
        missing = [f for f in REQUIRED_FIELDS if not prov.get(f)]
        assert not missing, (
            f"{result['capability_id']}: provenance missing or empty: {missing} "
            f"(CONTRACT.md §10)"
        )
        assert prov["capability_id"] == result["capability_id"]

    rows = db.execute(
        """
        SELECT capability_id, capability_version, worker_version, runtime_version,
               service_id, service_version, record_hash, sequence_no
        FROM result_provenance
        WHERE job_public_id = %s
        ORDER BY capability_id
        """,
        (completed_job["job_id"],),
    ).fetchall()
    assert len(rows) == len(completed_job["results"]), (
        f"{len(rows)} result_provenance row(s) for {len(completed_job['results'])} "
        f"results; a result without a provenance row is unobservable by MOS-STORE-278 and "
        f"this one is observable"
    )
    stored = {r["capability_id"]: r for r in rows}
    for result in completed_job["results"]:
        row = stored[result["capability_id"]]
        prov = result["provenance"]
        for field in ("capability_version", "worker_version", "runtime_version"):
            assert row[field] == prov[field], (
                f"{result['capability_id']}: the API reports {field}={prov[field]!r} and "
                f"the stored record says {row[field]!r}"
            )
        assert row["record_hash"], "a provenance row with no record_hash"
    print(
        "[P-versions] "
        + "; ".join(
            f"{r['capability_id']}@{r['capability_version']} "
            f"worker={r['worker_version']} runtime={r['runtime_version']}"
            for r in rows
        )
    )


def test_provenance_replay__the_record_names_every_stored_object(
    completed_job: dict[str, Any], primary_study: Ingested, dicomweb: Any
) -> None:
    """Exactly the objects that were stored -- no more and no less -- and they are there.

    Two directions, because each catches a different defect:
      * per result, the objects its provenance names equal the objects it wrote. A record
        that claims an object it did not write points a reviewer at someone else's data.
      * across the job, the union of every provenance's objects equals every stored
        object. This is the one that catches an object written with NO provenance at all,
        which `MOS-STORE-278` forbids from being observable.

    Then the PACS is asked. A provenance record that names a SOPInstanceUID the archive
    does not hold is not a record of what happened.
    """
    stored = {
        (o["object_kind"], o["series_instance_uid"], o["sop_instance_uid"])
        for r in completed_job["results"]
        for o in r["dicom_objects"]
    }
    assert stored, "the completed job stored no DICOM objects"

    for result in completed_job["results"]:
        named = {
            (o["object_kind"], o["series_instance_uid"], o["sop_instance_uid"])
            for o in result["provenance"]["generated_objects"]
        }
        own = {
            (o["object_kind"], o["series_instance_uid"], o["sop_instance_uid"])
            for o in result["dicom_objects"]
        }
        assert named == own, (
            f"{result['capability_id']}: provenance names {sorted(named)} but the result "
            f"wrote {sorted(own)}"
        )

    all_named = {
        (o["object_kind"], o["series_instance_uid"], o["sop_instance_uid"])
        for r in completed_job["results"]
        for o in r["provenance"]["generated_objects"]
    }
    assert all_named == stored, (
        f"objects stored with no provenance naming them: {sorted(stored - all_named)} "
        f"(MOS-STORE-278)"
    )

    in_pacs = {
        str(tag(row, "0020000E"))
        for row in derived_series(dicomweb, primary_study.study_instance_uid)
    }
    for kind, series_uid, sop_uid in sorted(all_named):
        assert series_uid in in_pacs, (
            f"provenance names {kind} series {series_uid}, which the PACS does not hold"
        )
        rows = qido_instances(dicomweb, primary_study.study_instance_uid, series_uid)
        assert {str(tag(r, "00080018")) for r in rows} == {sop_uid}, (
            f"provenance names {kind} instance {sop_uid}; the archive's series holds "
            f"{len(rows)} instance(s) and not that one alone"
        )
    print(f"[P-objects] {len(all_named)} named object(s), all present in the PACS")


def test_provenance_replay__the_record_is_safe_to_hand_a_reviewer(
    completed_job: dict[str, Any]
) -> None:
    """MOS-SAFE-086: the provenance record is exported and "must be safe to hand a reviewer".

    Asserted over the WHOLE job document rather than field by field, because the failure
    mode is a key nobody thought to check. Measurement geometry is asserted in the same
    test because it is the other thing a reviewer has to be able to trust about the
    numbers: CONTRACT.md §5 requires them "computed in SOURCE geometry. Never in model
    space", and a value measured in model space is wrong by however much the resampling
    moved it.
    """
    blob = json.dumps(completed_job).lower()
    for forbidden in (
        "patientname",
        "patientid",
        "patientbirthdate",
        "accessionnumber",
        "studydescription",
        "patientage",
        "patientsex",
    ):
        assert forbidden not in blob, (
            f"PHI-bearing key {forbidden!r} appears in the job document (MOS-SAFE-086)"
        )

    n = 0
    for result in completed_job["results"]:
        for m in result["measurements"]:
            assert m["geometry_space"] == "source", (
                f"{result['capability_id']}/{m['concept_code']} was measured in "
                f"{m['geometry_space']}, not source geometry (CONTRACT.md §5)"
            )
            assert m["ucum_unit"], f"{m['concept_code']} carries no UCUM unit"
            n += 1
    assert n, "no measurements to check"
    print(f"[P-reviewer] no PHI keys; {n} measurement(s), all in source geometry")
