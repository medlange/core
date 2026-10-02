# SPDX-License-Identifier: Apache-2.0
"""GATE CHECK `rejection-distinct` — docs/spec/15-delivery.md §15.1.2, release 0.1.0.

    "a study with no eligible series terminates `REJECTED` with a machine-readable reason,
     not `FAILED`"

WHY THIS IS RELEASE-GATED AND NOT A NICETY
------------------------------------------
`REJECTED` is Tier A in §15.1.3 -- it MUST NOT be cut under any circumstance -- and
§15.2.4 says why in a sentence: to a clinician a transport failure and a clinical
non-answer "are otherwise the same red X and one of them is information a radiologist must
act on". A radiologist who sees a generic error where the truth is "this study contains no
thin axial reconstruction" will chase an IT ticket, or worse, assume the study was
cleared.

So the check is four claims, not one:
  * the terminal state is `REJECTED` and not `FAILED`;
  * the reason is a machine-readable CODE from a closed enum, not a sentence;
  * the refusal is carried on a `200` job document with a `rejection` member and a null
    `error`, because `MOS-API-047` forbids `POST /api/v1/jobs` from returning a clinical
    rejection synchronously at all;
  * the rejected job wrote nothing into the archive.

Spec: MOS-EXEC-001, MOS-EXEC-014, MOS-API-036, MOS-API-039, MOS-API-040, MOS-API-047,
MOS-API-054, MOS-REL-005 (Tier A).
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from tests._support.skips import skip_no_data

from .conftest import Api, derived_series_allowing_absent, why

pytestmark = pytest.mark.gate_0_1_0

#: A syntactically valid StudyInstanceUID with nothing behind it. The DEGENERATE form of
#: "no eligible series", used by the second arm below, and the cleanest proof that
#: `POST /api/v1/jobs` does not pre-validate against the PACS: a synchronous 4xx here
#: would be MOS-API-047's exact prohibition.
ABSENT_STUDY = "1.2.826.0.1.3680043.8.498.99999999999999999999999999999999"

#: Chapter 5's `jobs.reject_reason_code` CHECK, RESTATED rather than imported. A test that
#: read the enum it is checking would assert nothing; if this list and schema.sql's CHECK
#: disagree, one of them is a defect and the disagreement is the finding.
CLOSED_REASON_ENUM = frozenset(
    {
        "no_eligible_series",
        "no_candidate_service_version",
        "outside_applicability_envelope",
        "unsupported_geometry",
        "input_constraint_unmet",
        "policy_denied",
        "service_declined",
    }
)


@pytest.fixture(scope="module")
def ineligible_study(dicomweb: Any) -> str:
    """A study the tenant DOES hold, containing no series any capability can use.

    This is §14.3.4's `fx-localizer-only` / `fx-dose-report` shape, built from the corpus:
    STOW only an LCTSC case's RTSTRUCT series and leave its CT behind. The study is then
    present, owned, retrievable and triageable -- and the selector has nothing to select.

    It is the scenario §15.1.2 actually names. A study the archive has never heard of (the
    second arm below) is the DEGENERATE case: it also has no eligible series, but the
    platform learns that from a retrieval failure rather than from triage, and the two
    paths can disagree. Gating on only one of them would leave the other free to regress.
    """
    import pydicom

    from .conftest import LCTSC_ROOT, _cases

    # SEARCH for a usable study; do not assert on the first candidate. The archive is
    # SHARED and PERSISTENT: any run that STOWs an LCTSC case's CT into one of these
    # studies -- the e2e suite, an integration test, a past session -- makes that study
    # permanently unusable here. Asserting on the FIRST candidate turned one such run
    # into a 0.1.0 gate that was red forever, for a reason with nothing to do with
    # rejection handling, while four other candidate cases sat unused. That is the
    # sticky-state defect `clean_slate` argues at length for the job tables, one layer
    # out: a release gate whose verdict is stuck is worse than no gate.
    contaminated: list[str] = []
    for case in _cases()[3:8]:
        for study_dir in sorted(p for p in case.iterdir() if p.is_dir()):
            for series_dir in sorted(p for p in study_dir.iterdir() if p.is_dir()):
                files = sorted(series_dir.glob("*.dcm"))
                if not files:
                    continue
                ds = pydicom.dcmread(str(files[0]), stop_before_pixels=True)
                if str(ds.Modality) != "RTSTRUCT":
                    continue
                results = dicomweb.store_files(files, study_instance_uid=study_dir.name)
                failed = sum(len(r.failed) for r in results)
                assert failed == 0, f"STOW of the RTSTRUCT reported {failed} failed SOPs"
                series = dicomweb.list_series(study_dir.name)
                modalities = sorted({s.modality for s in series})
                if modalities != ["RTSTRUCT"]:
                    contaminated.append(f"{study_dir.name} holds {modalities}")
                    continue
                print(
                    f"[ineligible] study={study_dir.name} modalities={modalities} "
                    f"series={len(series)}"
                )
                return str(study_dir.name)

    # TWO DIFFERENT OUTCOMES, AND ONLY ONE OF THEM IS A SKIP.
    if contaminated:
        # The scenario IS buildable from this corpus; the ARCHIVE is dirty. That is a
        # deployment fact, not a missing-data fact, so it MUST fail rather than skip.
        # `skip_no_data` is a skip even under `--require-stack`, and a 0.1.0 gate that
        # went green because its rejection check quietly stood down is the exact shape
        # of the MOS-SAFE-089a defect this package was rebuilt after.
        raise AssertionError(
            "every candidate study already holds series beyond its RTSTRUCT, so the "
            "'present but no eligible series' scenario cannot be built against THIS "
            "archive: "
            + "; ".join(contaminated)
            + ". The corpus is fine -- the archive is dirty. Something STOWed a CT into "
            "these studies (the e2e suite and several integration tests ingest whole "
            "cases). Remove them from the PACS, or move the `_cases()[3:8]` window onto "
            "cases nothing else touches. This is deliberately NOT a skip: the study "
            "this fixture needs is buildable, and a rejection check that stood down "
            "inside a green 0.1.0 gate is how MOS-REL-012 gets violated silently."
        )
    skip_no_data(
        f"no LCTSC case under {LCTSC_ROOT} has an RTSTRUCT series that can be STOWed "
        "without its CT, so the 'study present, no eligible series' scenario cannot be "
        "built",
        corpus="lctsc-corpus",
    )


@pytest.fixture(scope="module")
def rejected_job(api: Api, ineligible_study: str) -> dict[str, Any]:
    """Submit the study that holds no eligible series and wait for it to terminate."""
    r = api.post_job(ineligible_study)
    assert r.status_code in (200, 202), (
        f"a study with no eligible series must still be ACCEPTED (MOS-API-047: this "
        f"endpoint MUST NOT return a clinical_rejection synchronously); got "
        f"{r.status_code}: {r.text[:400]}"
    )
    return api.wait_for_terminal(r.json()["job_id"], timeout_s=300)


def test_rejection_distinct__terminates_rejected_and_not_failed(
    rejected_job: dict[str, Any]
) -> None:
    """MOS-EXEC-014: `REJECTED` is a terminal state of its own, not a flavour of `FAILED`.

    `FAILED` here would be the defect the whole check exists to catch, so the assertion
    message says which of the two the platform chose and quotes its own account of why.
    """
    assert rejected_job["state"] == "REJECTED", why(rejected_job)
    # MOS-API-040: a REJECTED job carries `rejection` and `error` is null. Never both, and
    # never neither -- a terminal state with no explanation is the same red X again.
    assert rejected_job["error"] is None, (
        "a REJECTED job also carries an `error`; MOS-API-040 makes the two exclusive so a "
        "consumer can branch on one field"
    )
    assert rejected_job["rejection"] is not None
    print(f"[R-state] {rejected_job['state']}, error=None, rejection present")


def test_rejection_distinct__the_reason_is_machine_readable_and_in_the_closed_enum(
    rejected_job: dict[str, Any], db: Any
) -> None:
    """MOS-API-036 / MOS-EXEC-001: a code from a closed set, classed `clinical_rejection`.

    The `class` enum is the half that matters to a consumer building a worklist:
    `clinical_rejection` says the study was examined and found unsuitable, and
    `transport_failure` says the platform could not look. Conflating them is exactly what
    §15.2.4 scheduled the RFC 9457 error model to prevent.

    `retryable is False` is asserted because a rejection that advertises itself as
    retryable produces a client that resubmits the same unsuitable study forever.
    """
    rejection = rejected_job["rejection"]
    assert rejection["class"] == "clinical_rejection", (
        f"the rejection is classed {rejection['class']!r}; a study with no eligible series "
        f"is a clinical non-answer, not a transport failure (MOS-API-036)"
    )
    assert rejection["reason_code"] == "no_eligible_series", (
        f"reason_code={rejection['reason_code']!r}, expected 'no_eligible_series'"
    )
    assert rejection["reason_code"] in CLOSED_REASON_ENUM
    assert rejection["retryable"] is False

    observed = {
        row["reject_reason_code"]
        for row in db.execute(
            "SELECT DISTINCT reject_reason_code FROM jobs "
            "WHERE reject_reason_code IS NOT NULL"
        ).fetchall()
    }
    outside = observed - CLOSED_REASON_ENUM
    assert not outside, (
        f"the deployment holds jobs whose reject_reason_code is outside chapter 5's "
        f"closed enum: {sorted(outside)}"
    )
    print(
        f"[R-reason] class={rejection['class']} code={rejection['reason_code']} "
        f"retryable={rejection['retryable']}; enum observed across the deployment: "
        f"{sorted(observed)}"
    )


def test_rejection_distinct__the_rejection_is_a_200_document_with_first_class_selection(
    api: Api, rejected_job: dict[str, Any]
) -> None:
    """MOS-API-047 and MOS-API-054, over the wire.

    A rejected job is read with `200`, not a `4xx`: "a client that receives a `4xx` here
    learns 'your request was wrong'; a client that receives a `200` with a `rejection`
    learns 'the study was not analysed and here is why', which is what a radiologist has
    to act on."

    And the selection href is followed. MOS-API-054 makes selection "first-class data, not
    a log line", and an href that 404s is a defect rather than a nicety -- it is the only
    place a clinician can see WHICH series were looked at and why each was passed over.
    """
    again = api.get_job(rejected_job["job_id"])
    assert again.status_code == 200, (
        f"GET on a REJECTED job answered {again.status_code}; MOS-API-047 requires 200 "
        f"with a rejection member"
    )
    assert again.json()["state"] == "REJECTED"

    href = rejected_job["rejection"]["series_selection_href"]
    assert href, "the rejection carries no series_selection_href (MOS-API-054)"
    selection = requests.get(f"{api.base_url}{href}", headers=api.headers(), timeout=60)
    assert selection.status_code == 200, f"{href} -> {selection.status_code}"
    body = selection.json()
    assert body["selected_series_uids"] == [], (
        f"the job was rejected for having no eligible series but the selection endpoint "
        f"reports {len(body['selected_series_uids'])} selected"
    )
    assert body["selected"] == []
    print(
        f"[R-api] 200 + rejection member; {href} -> 200 with 0 selected, "
        f"{len(body['rejected'])} evaluated-and-rejected series"
    )


def test_rejection_distinct__a_study_the_archive_does_not_hold_is_also_rejected(
    api: Api
) -> None:
    """The degenerate case of "no eligible series": a study with nothing behind it.

    A study the deployment does not hold has, trivially, no eligible series, and the
    clinician-facing answer is the same one: this study was not analysed, and here is the
    machine-readable reason. `tests/e2e/test_demo.py` states the same reading in terms --
    "a syntactically valid UID with no instances behind it ... is the cleanest form of no
    eligible series" -- and that is the behaviour the platform had before the Gateway
    became the only route to the PACS.

    It is a SEPARATE arm from the one above because the platform reaches the two by
    different paths. A study that is present but ineligible is decided by triage. An
    absent one is decided by what the retrieval answers, and `MOS-DATA-013` now makes that
    answer `404` -- so this arm is the one that fails if a retrieval status gets
    re-interpreted as a transport fault and handed to a radiologist as a system error.
    That is exactly the conflation §15.2.4 built the RFC 9457 `class` enum to prevent, and
    it is why `REJECTED` is Tier A.
    """
    r = api.post_job(ABSENT_STUDY)
    assert r.status_code in (200, 202), f"{r.status_code}: {r.text[:400]}"
    job = api.wait_for_terminal(r.json()["job_id"], timeout_s=300)

    assert job["state"] == "REJECTED", why(job)
    assert job["rejection"]["class"] == "clinical_rejection"
    assert job["rejection"]["reason_code"] in CLOSED_REASON_ENUM
    print(
        f"[R-absent] {job['state']} "
        f"code={job['rejection']['reason_code']}"
    )


def test_rejection_distinct__a_rejected_job_writes_nothing_into_the_archive(
    rejected_job: dict[str, Any], ineligible_study: str, dicomweb: Any, db: Any
) -> None:
    """A non-answer must leave no artefact a viewer could render as an answer.

    Two surfaces, because a platform can be clean on one: nothing derived in the PACS
    under that study, and no `results` or `result_dicom_objects` row for the job. The
    second is the one that would let a provenance panel display a finding for a study the
    platform declined to analyse.
    """
    assert derived_series_allowing_absent(dicomweb, ineligible_study) == [], (
        "a REJECTED job left MedicalOS-minted series in the archive"
    )
    counts = db.execute(
        """
        SELECT (SELECT count(*) FROM results
                 WHERE job_id = (SELECT id FROM jobs WHERE public_id = %(p)s)) AS results,
               (SELECT count(*) FROM result_dicom_objects
                 WHERE result_id IN (SELECT id FROM results
                        WHERE job_id = (SELECT id FROM jobs WHERE public_id = %(p)s)))
                 AS objects,
               (SELECT count(*) FROM job_events
                 WHERE job_id = (SELECT id FROM jobs WHERE public_id = %(p)s)
                   AND event_type = 'job.rejected') AS rejected_events,
               (SELECT count(*) FROM job_events
                 WHERE job_id = (SELECT id FROM jobs WHERE public_id = %(p)s)
                   AND event_type IN ('job.completed', 'job.failed')) AS other_terminal
        """,
        {"p": rejected_job["job_id"]},
    ).fetchone()
    assert counts["results"] == 0, f"a REJECTED job produced {counts['results']} results row(s)"
    assert counts["objects"] == 0
    # MOS-EXEC-013: the terminal transition is in the append-only event log, exactly once,
    # and it is the REJECTED one.
    assert counts["rejected_events"] == 1, (
        f"{counts['rejected_events']} job.rejected events, expected exactly 1"
    )
    assert counts["other_terminal"] == 0, (
        "the job emitted a completion or failure event as well as a rejection"
    )
    print("[R-archive] 0 derived series, 0 results, 1 job.rejected event")
