# SPDX-License-Identifier: Apache-2.0
"""GATE CHECK `tenant-isolation` — docs/spec/15-delivery.md §15.1.2, release 0.1.0.

    "403 on the REST API **and** 403 on a direct QIDO-RS for another tenant's
     `StudyInstanceUID`"

A SPEC TENSION, RESOLVED RATHER THAN PAPERED OVER
-------------------------------------------------
Read literally against the imaging plane, §15.1.2's second clause contradicts
`MOS-DATA-013`, which says in terms that a QIDO-RS or WADO-RS request naming a study
outside the caller's tenant set "MUST return `404`, not `403`", because "a `403` here is
an existence oracle". Both are normative and they cannot both be right about the same
request.

They are not about the same request. `MOS-DATA-013` reserves `403` for `MOS-DATA-009` --
spoofing the `{t}` path segment -- and Chapter 3's own acceptance check 3 spells the pair
out: with tenant A's key, `GET /dicomweb/t_A/studies/{B_study}/metadata` is `404` and
`GET /dicomweb/t_B/studies/{B_study}/metadata` is `403` with
`type: .../tenant-mismatch`. §15.1.2's "direct QIDO-RS for another tenant's study" is the
second of those: addressing the other tenant's data means addressing the other tenant's
segment. So this check asserts BOTH, and the `404` arm is the one that would be quietly
lost if only the gate row were implemented.

The REST half has the same shape and the same reason. `MOS-API-041` requires `403`
`CROSS_TENANT_DENIED` for a cross-tenant REFERENCE -- a request that names a tenant -- and
in the same sentence forbids leaking "whether the referenced id exists", which is a `404`
for a job the caller cannot see. Four arms, two of them the `403` the gate row names and
two of them the non-leaking `404` that makes the `403` safe.

NOTHING HERE IS PROVED ON THE HARNESS CONNECTION. The `db` fixture is the bootstrap
superuser, which bypasses row-level security entirely; it arranges the other tenant and is
never the subject of an assertion. Every claim below is made at an HTTP edge, against a
credential, by a caller who holds tenant A's key and nothing else.

Spec: MOS-DATA-009, MOS-DATA-010, MOS-DATA-012, MOS-DATA-013, MOS-API-003, MOS-API-041,
MOS-SEC-008, MOS-SEC-072.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
import requests

from tests._support.skips import skip_infra

from .conftest import (
    DATABASE_URL,
    GATEWAY_TOKEN,
    GATEWAY_URL,
    TENANT_A,
    Api,
)

pytestmark = pytest.mark.gate_0_1_0

DICOM_JSON = "application/dicom+json"

#: Attribute keywords that MUST NOT appear in any refusal. A body that leaks one has
#: answered the question the refusal exists to refuse.
DICOM_TAG_MARKERS = ("0020000D", "0008103E", "00200010", "00080060", "00080018", "0020000E")

#: Two StudyInstanceUIDs that no archive holds, one per tenant. Numeric-only and under the
#: 2.25/`1.2.826.0.1.3680043.8.498` space: `jobs.study_instance_uid` is a DICOM UID and the
#: API validates it, so a mnemonic like "...gate.tenant.a" is a 400 SCHEMA_VIOLATION and
#: not a job. They exist to be REFERENCED, never analysed.
GATE_STUDY_UID_A = "1.2.826.0.1.3680043.8.498.10000000000000000000000000000001"
GATE_STUDY_UID_B = "1.2.826.0.1.3680043.8.498.10000000000000000000000000000002"


def _app_dsn() -> str:
    """The DSN for `medicalos_app` -- the role the deployment's services connect as.

    NOT the bootstrap superuser. The other tenant's job row is written through
    `medos.db.repo`'s chokepoint under a tenant binding, as the application role, so it is
    a row that exists under exactly the same forced row-level security as every other job
    in the deployment. Arranging it as a superuser would create a row that RLS had never
    been asked about.
    """
    password = os.environ.get("MEDOS_APP_DB_PASSWORD", "medos_app")
    tail = DATABASE_URL.split("://", 1)[-1].split("@", 1)[-1]
    return f"postgresql://medicalos_app:{password}@{tail}"


@pytest.fixture(scope="module")
def tenant_b_job(tenant_b: str) -> Iterator[str]:
    """One `QUEUED` job owned by tenant B, created through the repository chokepoint.

    It is never claimed: `docker-compose.yml` sets `MEDOS_TENANTS` to tenant A only, and
    `MOS-SEC-078` makes the worker iterate exactly that set -- so a job in a tenant the
    worker does not serve is invisible to it, which is the intended consequence of forced
    row security on `job_queue`. The row exists to be asked for, not to be run.
    """
    import psycopg
    from medos.db.queue import PostgresJobQueue
    from medos.db.repo import JobSpec, create_job_queued
    from medos.db.tenancy import tenant_context
    from psycopg.rows import dict_row

    try:
        conn = psycopg.connect(_app_dsn(), row_factory=dict_row, autocommit=True)
    except psycopg.OperationalError as exc:
        # The taxonomy and not a bare failure, so that this arm is COUNTED by name in the
        # session report -- and so that `--require-stack`, which is how a release gate is
        # run, turns it into the failure it has to be. A gate arm that cannot establish
        # its precondition has not passed.
        skip_infra(
            f"cannot connect as `medicalos_app` to arrange the other tenant's job "
            f"({getattr(exc, 'sqlstate', None) or '?'}). Set MEDOS_APP_DB_PASSWORD to the "
            f"password the containers use: docker exec medos-api python -c "
            f"\"import os;from urllib.parse import urlsplit;"
            f"print(urlsplit(os.environ['MEDOS_DATABASE_URL']).password)\"",
            dependency="postgres",
        )
    try:
        with tenant_context(tenant_b):
            created = create_job_queued(
                conn,
                PostgresJobQueue(conn),
                JobSpec(
                    study_instance_uid=GATE_STUDY_UID_B,
                    capability_ids=("lung_segmentation",),
                    created_by_kind="service_account",
                    created_by_id="0.1.0-gate-runner",
                ),
            )
        print(f"[gate] tenant B owns job {created.job_id}")
        yield created.job_id
    finally:
        conn.close()


@pytest.fixture(scope="module")
def own_job(api: Api) -> str:
    """A job tenant A owns, so the header/query arms refuse a request that would work."""
    r = api.post_job(
        GATE_STUDY_UID_A, capabilities=["lung_segmentation"]
    )
    assert r.status_code in (200, 202), f"{r.status_code}: {r.text[:400]}"
    return str(r.json()["job_id"])


def _problem(response: requests.Response, *, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, (
        f"expected {status}, got {response.status_code}: {response.text[:400]}"
    )
    ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip()
    assert ctype == "application/problem+json", (
        f"a refusal must be RFC 9457 problem+json, got {ctype!r}"
    )
    doc = response.json()
    assert doc.get("code") == code, f"code={doc.get('code')!r}, expected {code!r}"
    return doc


# ======================================================================================
# ARM 1 — 403 on the REST API
# ======================================================================================
def test_tenant_isolation__the_rest_api_refuses_a_cross_tenant_reference_with_403(
    api: Api, own_job: str, tenant_b: str
) -> None:
    """MOS-API-003 / MOS-API-041: naming a tenant is `403 CROSS_TENANT_DENIED`.

    Two ways of naming one, because they are two different smuggling routes and a platform
    can close one and leave the other open: the `MedicalOS-Tenant-Id` header that
    MOS-API-003 reserves for a platform administrator, and a `tenant_id` query parameter
    ("there is no tenant path segment and no tenant query parameter").

    The job asked for is tenant A's OWN, which is the sharper form of the test: the
    request would have succeeded without the tenant field, so a `403` can only come from
    the field itself. `MOS-API-041` also forbids the refusal from echoing the tenant back.
    """
    via_header = api.get_job(
        own_job, headers={**api.headers(), "MedicalOS-Tenant-Id": tenant_b}
    )
    doc = _problem(via_header, status=403, code="CROSS_TENANT_DENIED")
    assert tenant_b not in json.dumps(doc), "the refusal echoes the tenant it refused"

    via_query = requests.get(
        f"{api.base_url}/api/v1/jobs/{own_job}?tenant_id={tenant_b}",
        headers=api.headers(),
        timeout=60,
    )
    _problem(via_query, status=403, code="CROSS_TENANT_DENIED")

    # And the control: without the field, the same request is a 200.
    assert api.get_job(own_job).status_code == 200, (
        "tenant A cannot read its own job, so the 403s above prove nothing"
    )
    print("[T-rest] 403 CROSS_TENANT_DENIED on the header and on the query parameter")


def test_tenant_isolation__the_rest_api_does_not_leak_whether_the_other_job_exists(
    api: Api, tenant_b_job: str
) -> None:
    """The other half of MOS-API-041: "MUST NOT leak whether the referenced id exists".

    Tenant A asks for a job id that really is tenant B's, and for one that was never
    issued. The two answers must be indistinguishable -- same status, same code. A `403`
    here (or any answer that differed from the never-issued one) would turn
    `GET /api/v1/jobs/{id}` into an oracle for which job ids the deployment holds.

    That this is a `404` and not the `403` §15.1.2's gate row names is deliberate and is
    the stricter behaviour; the `403` the gate row asks for is the arm above.
    """
    real = api.get_job(tenant_b_job)
    never = api.get_job("job_01J9F4N7T3R5V7X9Z1B3D5FGHJ")

    assert real.status_code == 404, (
        f"tenant A got {real.status_code} for tenant B's job; forced row-level security "
        f"on `jobs` should make the row simply not exist for this caller"
    )
    assert never.status_code == 404
    assert real.json().get("code") == never.json().get("code"), (
        f"a job that exists in another tenant answers {real.json().get('code')!r} while "
        f"one that never existed answers {never.json().get('code')!r}; the difference is "
        f"an existence oracle (MOS-API-041)"
    )
    # NOT `assert tenant_b_job not in real.text`. The refusal echoes the id the CALLER
    # supplied, in `detail` and in `instance`, and so does the never-issued one -- which is
    # the opposite of a leak: echoing back what was asked is what makes the two answers
    # identical in shape. The leak MOS-API-041 forbids would be a DIFFERENCE between them,
    # and that is what the assertion above measures.
    assert real.json().get("status") == never.json().get("status") == 404
    print("[T-rest] tenant B's job and a never-issued id are both 404, same code")


# ======================================================================================
# ARM 2 — 403 on a direct QIDO-RS for another tenant's study
# ======================================================================================
def _qido(tenant_segment: str, study_uid: str, suffix: str = "") -> requests.Response:
    return requests.get(
        f"{GATEWAY_URL}/dicomweb/{tenant_segment}/studies/{study_uid}{suffix}",
        headers={"Authorization": f"Bearer {GATEWAY_TOKEN}", "Accept": DICOM_JSON},
        timeout=60,
    )


def test_tenant_isolation__a_direct_qido_rs_on_another_tenants_segment_is_403(
    tenant_b_study: str, tenant_b: str
) -> None:
    """MOS-DATA-009: the `{t}` segment is compared with the PRINCIPAL's tenant.

    The credential used is the deployment's own worker token, whose principal is bound to
    tenant A in `gateway-principals.json`. Pointing it at tenant B's segment is the
    "direct QIDO-RS for another tenant's StudyInstanceUID" §15.1.2 names, and it must be
    `403` with `type: .../tenant-mismatch` -- spoofing a segment reveals nothing about
    what the deployment holds, so there is no existence oracle to protect here.

    Asserted on `/series` and on `/metadata`: the first is QIDO-RS and the second is the
    WADO-RS route that actually returns attributes, and a deployment that refused the
    query while serving the metadata would pass a one-route check.
    """
    for suffix in ("/series", "/metadata"):
        r = _qido(tenant_b, tenant_b_study, suffix)
        assert r.status_code == 403, (
            f"GET /dicomweb/{{tenant_B}}/studies/{{...}}{suffix} with tenant A's token -> "
            f"{r.status_code}, expected 403 (MOS-DATA-009): {r.text[:300]}"
        )
        body = r.text
        assert "tenant-mismatch" in body, (
            f"the refusal does not carry MOS-DATA-009's "
            f"type: .../tenant-mismatch: {body[:300]}"
        )
        for marker in DICOM_TAG_MARKERS:
            assert marker not in body, (
                f"the refusal body contains DICOM attribute {marker}; chapter 3's "
                f"acceptance check 3 requires that neither response contain any DICOM "
                f"attribute"
            )
    print("[T-qido] 403 tenant-mismatch on tenant B's segment, no DICOM attribute returned")


def test_tenant_isolation__a_direct_qido_rs_under_the_callers_own_segment_is_404(
    tenant_b_study: str
) -> None:
    """MOS-DATA-013: not `403`, because `403` would confirm the study is present.

    The study named here EXISTS in the PACS -- the fixture STOWed it through the Gateway
    and then moved its tenancy record -- which is the only version of this test that means
    anything. A made-up UID returns `404` from any implementation, including one with no
    tenancy at all.

    "never `2xx`, never any instance bytes" is MOS-DATA-013's own acceptance wording, so
    the instance list is checked as well as the status.
    """
    meta = _qido(TENANT_A, tenant_b_study, "/metadata")
    assert meta.status_code == 404, (
        f"tenant A's own segment returned {meta.status_code} for a study owned by tenant "
        f"B; MOS-DATA-013 requires 404 and forbids 403 here: {meta.text[:300]}"
    )
    for marker in DICOM_TAG_MARKERS:
        assert marker not in meta.text, (
            f"the 404 body contains DICOM attribute {marker}"
        )

    series = _qido(TENANT_A, tenant_b_study, "/series")
    assert series.status_code in (204, 404), (
        f"QIDO-RS for another tenant's study answered {series.status_code}: "
        f"{series.text[:300]}"
    )
    for marker in DICOM_TAG_MARKERS:
        assert marker not in series.text, (
            f"the series query's refusal body contains DICOM attribute {marker}"
        )
    # A DICOM-JSON result is an ARRAY; a refusal is a problem+json OBJECT. Both are
    # acceptable answers here -- MOS-DATA-013 asks for "never 2xx, never any instance
    # bytes" and 404 satisfies it -- but an array, if one comes back, must be empty. This
    # distinction is not pedantry: reading `len()` of the refusal object counts its KEYS,
    # which is how an earlier version of this assertion reported "10 series" for a
    # correctly refused request.
    payload: Any = None
    if series.status_code != 204 and series.content:
        payload = series.json()
    if isinstance(payload, list):
        assert payload == [], (
            f"the query returned {len(payload)} series for a study of another tenant"
        )

    # And the control: with the SAME credential and the SAME route shape, a study tenant A
    # really owns answers 200. Without this, a Gateway that 404s everything passes.
    own = requests.get(
        f"{GATEWAY_URL}/dicomweb/{TENANT_A}/studies",
        headers={"Authorization": f"Bearer {GATEWAY_TOKEN}", "Accept": DICOM_JSON},
        timeout=60,
    )
    assert own.status_code in (200, 204), (
        f"tenant A cannot list its own studies ({own.status_code}); the 404 above proves "
        f"nothing"
    )
    print("[T-qido] 404 + empty series list under tenant A's own segment; A still sees its own")
