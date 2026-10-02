# SPDX-License-Identifier: Apache-2.0
"""The DICOM Gateway: the only route to the PACS, and the only place PHI access is seen.

docs/spec/03-medical-data-plane.md section 3.2. `medos/deploy/compose/docker-compose.yml` has
carried this sentence in its SECURITY POSTURE header since the Gateway landed:

    NOTHING IN THE SUITE ASSERTS THAT YET. An earlier version of this comment claimed
    `tests/integration/test_gateway.py` asserts it from inside the worker container; that
    file has never existed, and a comment that names a test which is not there is a
    coverage claim nobody can cash.

This is that file.

TWO HALVES, AND WHY NEITHER ALONE IS ENOUGH
-------------------------------------------
**In-process** (`gateway` fixture): a real `create_app()` over the throwaway Postgres of
`tests/integration/conftest.py`, with a STUB PACS that has no tenancy model and answers
every request it is given. That stub is the point rather than a convenience: MOS-DATA-011
says the tenancy predicate must be applied by the Gateway *after* the backend answers,
"because a PACS that has no tenancy model will silently ignore an unknown matching key and
return everything". A stub that returns both tenants' studies to everybody is the only way
to prove the Gateway -- and not the backend -- did the filtering. Two tenants, two studies,
two principals and a job row are things a live single-tenant deployment cannot give us.

**Live stack** (`live_gateway` fixture): the container on `127.0.0.1:8043` in front of the
real Orthanc. QIDO-RS, WADO-RS and STOW-RS against a real PACS with real DICOM bytes, plus
the two facts that only exist at deployment level: no other container can open a TCP
connection to the PACS (MOS-DATA-006), and the PACS credential is in exactly one
deployment unit (MOS-DATA-002, MOS-DATA-005).

Requirements asserted, by id
----------------------------
    MOS-DATA-002   no component other than the Gateway holds or can derive a PACS route
    MOS-DATA-005   the credential is delivered to exactly one deployment unit
    MOS-DATA-006   no other container can open a TCP connection to the PACS port
    MOS-DATA-007   the exposed surface: QIDO-RS, WADO-RS and STOW-RS all work through it
    MOS-DATA-009   the effective tenant comes from the principal; a spoofed `{t}` is 403
    MOS-DATA-011   QIDO-RS is an intersection applied AFTER the backend responds
    MOS-DATA-012   STOW-RS resolves ownership across tenants; 409 stores nothing
    MOS-DATA-013   a study outside the caller's tenant is 404, never 403, never 2xx
    MOS-DATA-015   the clinician surface is configured against the Gateway and not
                 against the PACS (amended at specification 0.4.0: the requirement
                 named OHIF until the deployment stopped running it)
    MOS-DATA-017   a Service presents `Authorization: Bearer <token>`, never a credential
    MOS-DATA-022   one PHI-access audit row per PHI-bearing response, naming principal+job
    MOS-DATA-026   metric label values carry no UID
    MOS-API-035    every refusal is `application/problem+json` (RFC 9457)
    MOS-API-036    every `type` is under `https://spec.medicalos.org/problems/`
    MOS-SEC-148    class `phi` produces an AuditEvent on both allow and deny
    MOS-STORE-307  the audit row carries the surrogate, never an attribute value

THREE TESTS IN HERE FAIL TODAY, AND THEY ARE NOT PLACEHOLDERS
--------------------------------------------------------------
    test_no_dicom_uid_reaches_the_gateway_log_stream
        Chapter 3 acceptance check 14 requires the structured log stream to contain zero
        StudyInstanceUIDs. `medos.api.app.TraceContextMiddleware` logs the raw request
        path, and the Gateway's entire URL space is UIDs.

    test_the_phi_access_row_the_gateway_wrote_is_chained
        `medos/medos/db/audit.py` sends placeholder `seq`/`prev_hash`/`hash` and relies on
        migration 0005's `audit_events_chain` trigger to overwrite them. This deployment's
        Postgres volume predates the migration, so the trigger is absent, nothing fails,
        and every audit row says `seq = 1` with a hash of sixty-four zeroes.

    test_no_gateway_response_body_names_the_pacs_origin
        The metadata routes scrub the PACS origin out of `BulkDataURI`; the STOW-RS route
        returns the backend's `(0008,1190) RetrieveURL` verbatim.

Each says so in its own docstring, names the line responsible, and cites the requirement.
None is marked `xfail`: an `xfail` reports green, and it arrives in the skip taxonomy's
`OTHER SKIPS` bucket as an unclassified line -- the two outcomes `tests/_support/skips.py`
exists to prevent. A defect that is not red is a defect nobody is holding.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
import requests
from medos.db import repo
from medos.db.queue import PostgresJobQueue
from medos.db.repo import JobSpec
from medos.db.tenancy import DEFAULT_TENANT_ID, tenant_context
from medos.dicomweb.gateway import PacsCredentials
from medos.gateway.app import GatewayConfig, create_app
from medos.gateway.auth import Principal, StaticApiKeyAuthenticator
from medos.gateway.backend import PacsBackend, SharedBackendResolver
from psycopg.rows import dict_row

from tests._support import stack as _stack
from tests._support.skips import skip_environment, skip_infra, skip_no_data
from tests.integration.conftest import APP_PASSWORD

# =====================================================================================
# The in-process fixture's world.
#
# Two tenants that are NOT `DEFAULT_TENANT_ID`: the session `api_key` fixture and the
# weeks 1-2 API tests own rows under the default tenant, and `_clear_other_tenants` below
# is deliberately unable to touch them.
# =====================================================================================
TENANT_A = "018f0a00-0000-7000-8000-0000000000aa"
TENANT_B = "018f0a00-0000-7000-8000-0000000000bb"

# 2.25.<uuid-as-int> is the ISO/IEC 9834-8 derived UID branch: collision-free by
# construction and unmistakably synthetic, so one of these can never be confused with a
# UID from the LCTSC corpus if it ever reached a real archive.
STUDY_A = "2.25.140737488355328000000000000000000001"
STUDY_B = "2.25.140737488355328000000000000000000002"

# A PatientName and a PatientID the STUB PACS puts in its QIDO answers. Not PHI -- these
# are invented here -- but they occupy the exact position a real attribute value would,
# which is what makes the log and audit assertions below meaningful.
FIXTURE_PATIENT_NAME = "ZZTEST^GATEWAY^FIXTURE"
FIXTURE_PATIENT_ID = "MRN-GATEWAY-FIXTURE-0001"
FIXTURE_STUDY_DESCRIPTION = "ZZTEST GATEWAY FIXTURE DESCRIPTION"

VIEWER_KEY = "gw-test-viewer-key"          # tenant A, actor_kind=user  -> clinical_viewer
WORKER_KEY = "gw-test-worker-key"          # tenant A, platform_writer, job-scoped
BEE_KEY = "gw-test-tenant-b-key"           # tenant B, actor_kind=user  -> clinical_viewer

DICOM_JSON = "application/dicom+json"
PROBLEM_JSON = "application/problem+json"
MULTIPART = 'multipart/related; type="application/dicom"'
PROBLEM_AUTHORITY = "https://spec.medicalos.org/problems/"

TAG_STUDY_UID = "0020000D"
TAG_SERIES_UID = "0020000E"
TAG_SOP_UID = "00080018"
TAG_PATIENT_NAME = "00100010"
TAG_PATIENT_ID = "00100020"
TAG_RETRIEVE_URL = "00081190"
TAG_REFERENCED_SOP_SEQ = "00081199"
TAG_FAILED_SOP_SEQ = "00081198"

# The stub backend's DICOMweb root and the origin inside it. `pacs.invalid` is RFC 2606's
# reserved TLD: if a rewrite ever fails to happen, the URL that escapes is provably
# unresolvable rather than pointed at something real.
PACS_BASE_URL = "http://pacs.invalid:8042/dicom-web"
PACS_ORIGIN = "pacs.invalid:8042"
GATEWAY_PUBLIC_BASE = "http://gateway.test"


# =====================================================================================
# The stub PACS.
#
# `medos.gateway.app._forward` calls `session.request(method, url, params=, headers=,
# data=, auth=, stream=, timeout=)` and reads `.status_code`, `.headers`, `.content`,
# `.json()` and `.iter_content()`. A real `requests.Response` with its body pre-loaded
# satisfies all of that, so the double is the transport and nothing else is mocked.
# =====================================================================================
class StubPacs:
    """A PACS with no tenancy model: it answers for every study it is asked about.

    This is MOS-DATA-011's premise made executable. If the Gateway delegated its tenancy
    predicate to the backend query -- the optimisation the requirement forbids by name --
    every assertion in `test_qido_rs_is_an_intersection_applied_after_the_backend_answers`
    and `test_a_study_outside_the_tenant_is_404_and_the_backend_is_never_asked` would
    pass while the system leaked, because this stub would have "helpfully" filtered.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.stow_status = 200
        # Set to raise from the transport, so a route's UNHANDLED path can be driven.
        # `requests.RequestException` is caught by every route; this is deliberately not
        # one, because the log assertions below have to reach `_unhandled`.
        self.explode: Exception | None = None

    # -- the transport -----------------------------------------------------------------
    def request(self, method: str, url: str, **_kw: Any) -> requests.Response:
        self.calls.append((method, url))
        if self.explode is not None:
            raise self.explode
        path = url.split("/dicom-web", 1)[-1]
        if method == "POST":
            # A STOW-RS reply as PS3.18 section 10.5 shapes one, with the backend's own
            # origin in ALL THREE of the places a URL lives in it: the top-level
            # `(0008,1190) RetrieveURL`, one per item of `(0008,1199)
            # ReferencedSOPSequence`, and one per item of `(0008,1198)
            # FailedSOPSequence`. A scrub that walks only the top level, or only the
            # attribute one route happened to need, leaves the other two standing.
            root = url.split("/dicom-web")[0]
            return self._response(
                self.stow_status,
                DICOM_JSON,
                json.dumps(
                    {
                        TAG_RETRIEVE_URL: {
                            "vr": "UR",
                            "Value": [f"{root}/dicom-web/studies/x"],
                        },
                        TAG_REFERENCED_SOP_SEQ: {
                            "vr": "SQ",
                            "Value": [
                                {
                                    TAG_RETRIEVE_URL: {
                                        "vr": "UR",
                                        "Value": [
                                            f"{root}/dicom-web/studies/x/series/y"
                                            "/instances/z"
                                        ],
                                    }
                                }
                            ],
                        },
                        TAG_FAILED_SOP_SEQ: {
                            "vr": "SQ",
                            "Value": [
                                {
                                    TAG_RETRIEVE_URL: {
                                        "vr": "UR",
                                        "Value": [f"{root}/dicom-web/studies/x/series/q"],
                                    }
                                }
                            ],
                        },
                    }
                ).encode(),
                # The other carrier: a header. A DICOMweb server answers a store with a
                # `Location`, and a proxy that forwards response headers verbatim
                # republishes the origin there whatever it did to the body.
                headers={
                    "Location": f"{root}/dicom-web/studies/x",
                    "Content-Location": f"{root}/dicom-web/studies/x",
                    "Server": "StubPacs/9.9 (the PACS product, MOS-SEC-091)",
                },
            )
        if path.endswith("/studies"):
            return self._response(200, DICOM_JSON, json.dumps(self._study_list()).encode())
        if path.endswith("/metadata"):
            uid = path.split("/studies/", 1)[-1].split("/", 1)[0]
            return self._response(
                200, DICOM_JSON, json.dumps([self._study_row(uid)]).encode()
            )
        if path.endswith("/series"):
            return self._response(
                200,
                DICOM_JSON,
                json.dumps([{TAG_SERIES_UID: {"vr": "UI", "Value": ["2.25.9001"]}}]).encode(),
            )
        # Everything else is a retrieve: one multipart part, so `_PartCounter` sees one
        # instance and MOS-DATA-022's `instance_count` is checkable.
        body = (
            b"--bnd\r\nContent-Type: application/dicom\r\n\r\n"
            + b"\x00" * 128
            + b"DICM"
            + FIXTURE_PATIENT_NAME.encode()
            + b"\r\n--bnd--\r\n"
        )
        return self._response(
            200,
            'multipart/related; type="application/dicom"; boundary=bnd',
            body,
            # A retrieval's body is opaque DICOM and is streamed untouched; its HEADERS
            # are the streaming routes' own copy of the origin problem.
            headers={
                "Content-Location": f"{url.split('/dicom-web')[0]}/dicom-web{path}",
                "Server": "StubPacs/9.9 (the PACS product, MOS-SEC-091)",
            },
        )

    # -- what it holds -----------------------------------------------------------------
    def _study_list(self) -> list[dict[str, Any]]:
        """BOTH tenants' studies, to everybody. The PACS knows nothing about tenants."""
        return [self._study_row(STUDY_A), self._study_row(STUDY_B)]

    def _study_row(self, uid: str) -> dict[str, Any]:
        return {
            # `(0008,1190) RetrieveURL` is a QIDO-RS return key, not a metadata-only one
            # (PS3.18 section 8.3.4.3), so the plain study-search route publishes the
            # backend origin too. It is here because the Gateway's scrub was once keyed
            # to the two routes that return `BulkDataURI`, and the three that do not were
            # handing the origin out unchanged.
            TAG_RETRIEVE_URL: {"vr": "UR", "Value": [f"{PACS_BASE_URL}/studies/{uid}"]},
            TAG_STUDY_UID: {"vr": "UI", "Value": [uid]},
            TAG_PATIENT_NAME: {"vr": "PN", "Value": [{"Alphabetic": FIXTURE_PATIENT_NAME}]},
            TAG_PATIENT_ID: {"vr": "LO", "Value": [FIXTURE_PATIENT_ID]},
            "00081030": {"vr": "LO", "Value": [FIXTURE_STUDY_DESCRIPTION]},
        }

    @staticmethod
    def _response(
        status: int,
        content_type: str,
        body: bytes,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        resp = requests.Response()
        resp.status_code = status
        resp.url = "stub://pacs"
        resp.headers["Content-Type"] = content_type
        for key, value in (headers or {}).items():
            resp.headers[key] = value
        resp._content = body            # noqa: SLF001 - the documented way to preload one
        resp._content_consumed = True   # noqa: SLF001
        return resp

    # -- assertions helpers ------------------------------------------------------------
    def asked_about(self, study_instance_uid: str) -> bool:
        return any(study_instance_uid in url for _method, url in self.calls)


class GatewayUnderTest:
    """A `TestClient` plus the world it was built over."""

    def __init__(self, client: Any, pacs: StubPacs, dsn: str, job_id: str) -> None:
        self.client = client
        self.pacs = pacs
        self.dsn = dsn
        self.job_id = job_id
        # `audit_events` is append-only (MOS-STORE-228: the BEFORE DELETE trigger raises),
        # so the per-test cleanup CANNOT empty it and rows accumulate across this module.
        # A test that asserts "exactly one deny row" therefore needs to know which rows
        # are its own. Recording the public ids that already existed is the cheap, exact
        # way, and unlike a `seq` watermark it does not itself depend on MOS-SEC-151's
        # chain trigger being installed -- which is a thing this file elsewhere proves is
        # not always true.
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            self._pre_existing = {
                row["public_id"]
                for row in conn.execute("SELECT public_id FROM audit_events").fetchall()
            }

    def get(self, path: str, key: str | None = VIEWER_KEY, **kw: Any) -> Any:
        headers = dict(kw.pop("headers", {}) or {})
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        headers.setdefault("Accept", DICOM_JSON)
        return self.client.get(path, headers=headers, **kw)

    def post(self, path: str, key: str, body: bytes, content_type: str) -> Any:
        return self.client.post(
            path,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": content_type,
                "Accept": DICOM_JSON,
            },
            content=body,
        )

    def audit_rows(self, **where: Any) -> list[dict[str, Any]]:
        """Every audit row THIS TEST caused, oldest first.

        Read as the superuser: row-level security is not what is under test here, and a
        tenant-bound read would hide a row written to the WRONG tenant -- which is exactly
        the failure these assertions have to be able to see.
        """
        clauses = " AND ".join(f"{k} = %s" for k in where) or "TRUE"
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_events WHERE {clauses} "  # noqa: S608 - keys are literals
                "ORDER BY recorded_at, seq",
                tuple(where.values()),
            ).fetchall()
        return [row for row in rows if row["public_id"] not in self._pre_existing]

    def studies_of(self, tenant_id: str) -> set[str]:
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row) as conn:
            rows = conn.execute(
                "SELECT study_instance_uid FROM studies WHERE tenant_id = %s",
                (tenant_id,),
            ).fetchall()
        return {r["study_instance_uid"] for r in rows}


# =====================================================================================
# In-process fixtures
# =====================================================================================
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dsn_as(dsn: str, role: str, password: str) -> str:
    head, _, tail = dsn.partition("://")
    return f"{head}://{role}:{password}@{tail.split('@', 1)[-1]}"


@pytest.fixture()
def seeded(pg_dsn: str) -> Iterator[dict[str, Any]]:
    """Two tenants, one study and one patient each, and one job owned by tenant A.

    Seeded on the bootstrap superuser connection, which bypasses row-level security --
    that is why it may write for two tenants in one connection, and why nothing it
    observes is ever used as evidence below. Every assertion reads through the Gateway or
    through a separate connection.

    The job exists because `medos.db.audit.record` refuses to write a row naming a job it
    cannot join to (MOS-SEC-155): "an audit row MUST NOT name a job it cannot join to". A
    job-scoped Gateway principal with an invented `job_id` would therefore produce a
    SILENTLY MISSING audit row -- `medos.gateway.app._write_access` logs and swallows the
    failure, because the bytes have already left -- and the MOS-DATA-022 test would be
    asserting against a trail that never existed.
    """
    conn = psycopg.connect(pg_dsn, autocommit=True, row_factory=dict_row)
    try:
        _clear_other_tenants(conn)
        for tenant_id, slug in ((TENANT_A, "gw-tenant-a"), (TENANT_B, "gw-tenant-b")):
            conn.execute(
                "INSERT INTO tenants (id, slug, display_name) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (tenant_id, slug, slug),
            )
        for tenant_id, study_uid in ((TENANT_A, STUDY_A), (TENANT_B, STUDY_B)):
            patient = conn.execute(
                "INSERT INTO patients (tenant_id, patient_id_value, issuer_of_patient_id,"
                "                      phi_state) "
                "VALUES (%s, %s, 'gw-test', 'identified') RETURNING id",
                (tenant_id, f"{FIXTURE_PATIENT_ID}-{tenant_id[-2:]}"),
            ).fetchone()
            conn.execute(
                "INSERT INTO studies (tenant_id, patient_id, study_instance_uid, "
                "                     pacs_backend, phi_state) "
                "VALUES (%s, %s, %s, 'orthanc-local', 'identified')",
                (tenant_id, patient["id"], study_uid),
            )
        with tenant_context(TENANT_A):
            created = repo.create_job_queued(
                conn,
                PostgresJobQueue(conn),
                JobSpec(study_instance_uid=STUDY_A, capability_ids=("lung_segmentation",)),
            )
        yield {"job_id": created.job_id}
    finally:
        # CLEAN UP ON THE WAY OUT AS WELL AS ON THE WAY IN, and this is not belt and
        # braces -- it is a bug this module caused and had to fix.
        #
        # `tests/integration/test_tenancy.py` shares the session database and clears
        # foreign tenants by walking the tables with a foreign key INTO `tenants`. That
        # set does not contain `studies`: `studies` carries `tenant_id` but reaches
        # `tenants` only through the composite `studies_patient_fk` into `patients`. So
        # the `studies` rows this fixture seeds for TENANT_A and TENANT_B survive that
        # cleanup and then BLOCK its `DELETE FROM patients`, and twelve tenancy tests
        # error out in setup -- in a full-suite run only, because `test_gateway` sorts
        # before `test_tenancy`. Leaving no rows behind is this module's job.
        try:
            _clear_other_tenants(conn)
        finally:
            conn.close()


def _clear_other_tenants(conn: psycopg.Connection[Any]) -> None:
    """Delete every row belonging to a tenant other than `DEFAULT_TENANT_ID`.

    Lifted in intent from `tests/integration/test_tenancy.py`: the `pg_dsn` database is
    session-scoped, so a previous module's tenants survive into this one and `INSERT INTO
    tenants` would hit the primary key.

    TWO DIFFERENCES FROM THAT FILE'S VERSION, BOTH FOUND BY RUNNING THIS ONE.

    It enumerates tables by the presence of a `tenant_id` COLUMN rather than by a foreign
    key to `tenants`. MOS-SEC-077 -- "no `tenant_id` column anywhere without forced row
    security" -- makes the column the tenancy marker, and the FK is not reliably there:
    `studies` carries `tenant_id` and references `tenants` only through the composite
    `studies_patient_fk` into `patients`, so an FK-to-`tenants` scan never sees it, never
    deletes it, and then cannot delete `patients` either. That was the first failure here.

    And the deletes repeat until a pass is clean, because `information_schema` gives no
    topological order and `studies` must go before `patients`. The order is discovered
    rather than hardcoded, so a future child table needs no edit.

    `audit_events` (and its partitions) carry `tenant_id` and are APPEND-ONLY: MOS-STORE-228's
    `audit_events_immutable` trigger raises `InsufficientPrivilege` on any DELETE. That is
    the guarantee working, not a problem to route around, so those tables are left alone
    and `GatewayUnderTest.audit_rows` filters by what already existed instead. Nothing in
    this module needs them empty; it needs to know which rows are its own.
    """
    conn.execute("TRUNCATE jobs, job_queue, job_events, job_steps, job_series, "
                 "results, result_measurements, result_dicom_objects CASCADE")
    owned = conn.execute(
        "SELECT table_name FROM information_schema.columns "
        " WHERE table_schema = 'public' AND column_name = 'tenant_id' "
        "   AND table_name <> 'tenants' "
        " ORDER BY table_name"
    ).fetchall()

    pending = [row["table_name"] for row in owned]
    for _pass in range(len(pending) + 1):
        blocked: list[str] = []
        for table in pending:
            try:
                conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id <> %s",  # noqa: S608 - catalog
                    (DEFAULT_TENANT_ID,),
                )
            except psycopg.errors.InsufficientPrivilege:
                continue  # append-only by design (MOS-STORE-228); see the docstring
            except psycopg.errors.ForeignKeyViolation:
                blocked.append(table)
        if not blocked:
            break
        if blocked == pending:
            raise AssertionError(
                f"a cycle or an unreachable child blocks the cleanup: {blocked}"
            )
        pending = blocked
    conn.execute("DELETE FROM tenants WHERE id <> %s", (DEFAULT_TENANT_ID,))


@pytest.fixture()
def gateway(pg_dsn: str, seeded: dict[str, Any]) -> Iterator[GatewayUnderTest]:
    """A real `create_app()`, connecting as `medicalos_app` over a stub PACS.

    `medicalos_app` and NOT the bootstrap superuser. MOS-SEC-073 makes that role
    NOBYPASSRLS, so the tenancy filtering proved below is PostgreSQL's row-level security
    doing the work and not a WHERE clause the Gateway happens to have written -- the same
    distinction `tests/integration/test_tenancy.py` is built on. A superuser connection
    here would make every isolation assertion in this file vacuous.
    """
    from fastapi.testclient import TestClient

    app, pacs = _build_gateway_app(pg_dsn, seeded, public_base=GATEWAY_PUBLIC_BASE)
    with TestClient(app) as client:
        yield GatewayUnderTest(client, pacs, pg_dsn, seeded["job_id"])


def _build_gateway_app(
    pg_dsn: str, seeded: dict[str, Any], *, public_base: str
) -> tuple[Any, StubPacs]:
    """The `gateway` fixture's app, with `public_base` left open.

    Extracted so that `test_the_pacs_origin_is_scrubbed_with_no_public_base_configured`
    can build the SAME Gateway with `MEDOS_GATEWAY_PUBLIC_BASE` unset. That configuration
    used to be the one where the origin scrub silently did nothing, so it needs a second
    app rather than a second assertion against this one.
    """
    app_dsn = _dsn_as(pg_dsn, "medicalos_app", APP_PASSWORD)
    pacs = StubPacs()
    principals = (
        Principal(
            principal_id="user:gw-test-viewer",
            actor_kind="user",
            tenant_id=TENANT_A,
            scopes=frozenset({"study.read"}),
            key_id="viewer-v1",
        ),
        Principal(
            principal_id="svc:gw-test-worker",
            actor_kind="service_account",
            tenant_id=TENANT_A,
            scopes=frozenset({"study.read", "study.write"}),
            key_id="worker-v1",
            # MOS-DATA-018's `job` claim and MOS-SEC-147's delegation, which is what makes
            # "which user caused these images to be read" answerable for a worker.
            job_id=seeded["job_id"],
            on_behalf_of_kind="user",
            on_behalf_of_id="user:gw-test-clinician",
            on_behalf_of_role="radiologist",
            # MOS-DATA-020: the platform DICOM writer, in the source UID space. Without
            # the override this principal resolves to `service`, whose egress row requires
            # de-identification, and `_authorise` refuses it with 503 (MOS-DATA-021/037).
            consumer_class_override="platform_writer",
        ),
        Principal(
            principal_id="user:gw-test-tenant-b",
            actor_kind="user",
            tenant_id=TENANT_B,
            scopes=frozenset({"study.read"}),
            key_id="tenant-b-v1",
        ),
    )
    auth = StaticApiKeyAuthenticator(
        principals=principals,
        digests=(_sha256(VIEWER_KEY), _sha256(WORKER_KEY), _sha256(BEE_KEY)),
        clinical_use_mode="clinical",
    )
    backend = PacsBackend(
        backend_id="orthanc-local",
        base_url=PACS_BASE_URL,
        credential=PacsCredentials(user="pacs-user", password="pacs-password"),
    )

    def connect(**kwargs: Any) -> psycopg.Connection[Any]:
        kwargs.setdefault("autocommit", True)
        return psycopg.connect(app_dsn, row_factory=dict_row, **kwargs)

    app = create_app(
        connect=connect,
        authenticator=auth,
        resolver=SharedBackendResolver(backend),
        config=GatewayConfig(public_base=public_base),
        session=pacs,
        configure_logs=False,   # the suite owns logging; see the log tests below
    )
    return app, pacs


# =====================================================================================
# 1. MOS-DATA-011 -- QIDO-RS is an INTERSECTION, applied after the backend answers
# =====================================================================================
def test_qido_rs_is_an_intersection_applied_after_the_backend_answers(
    gateway: GatewayUnderTest,
) -> None:
    """The backend returns both tenants' studies; the caller receives one.

    MOS-DATA-011: "The filter is an intersection of the returned `StudyInstanceUID` set
    with the tenant's owned set."

    Asserted against the projection and not against a fixture list, which is chapter 3's
    own acceptance check 4: "asserted against the `studies` projection, not against a
    fixture list". A fixture list would still pass if the Gateway returned a hardcoded
    answer; the projection is the thing MOS-DATA-010 makes authoritative.
    """
    response = gateway.get(f"/dicomweb/{TENANT_A}/studies")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(DICOM_JSON)

    returned = {row[TAG_STUDY_UID]["Value"][0] for row in response.json()}
    assert returned == gateway.studies_of(TENANT_A), (
        "the QIDO-RS result set must equal the tenant's set in the `studies` projection"
    )
    assert STUDY_B not in returned

    # And the backend WAS asked for everything: the predicate was not pushed down.
    backend_answered = {
        row[TAG_STUDY_UID]["Value"][0] for row in gateway.pacs._study_list()
    }
    assert STUDY_B in backend_answered, (
        "the stub must offer tenant B's study, or this test proves nothing"
    )
    assert len(gateway.pacs.calls) == 1


def test_tenant_b_sees_only_tenant_b(gateway: GatewayUnderTest) -> None:
    """The other direction, on a second principal, so the result is not one tenant's luck."""
    response = gateway.get(f"/dicomweb/{TENANT_B}/studies", key=BEE_KEY)
    assert response.status_code == 200, response.text
    returned = {row[TAG_STUDY_UID]["Value"][0] for row in response.json()}
    assert returned == {STUDY_B} == gateway.studies_of(TENANT_B)


# =====================================================================================
# 2. MOS-DATA-013 -- a foreign study named DIRECTLY is 404, and never reaches the PACS
#
# "This is half the 0.1.0 tenant-isolation gate": the caller supplies a StudyInstanceUID
# it already knows, which is the case a response filter cannot catch and which a
# published dataset manifest makes trivially available.
# =====================================================================================
@pytest.mark.parametrize(
    "route, operation",
    [
        ("/dicomweb/{t}/studies/{st}/metadata", "WADO-RS.study_metadata"),
        ("/dicomweb/{t}/studies/{st}/series", "QIDO-RS.series"),
        ("/dicomweb/{t}/studies/{st}", "WADO-RS.study"),
        ("/dicomweb/{t}/studies/{st}/series/2.25.9001", "WADO-RS.series"),
        ("/dicomweb/{t}/studies/{st}/series/2.25.9001/instances/2.25.9002", "WADO-RS.instance"),
    ],
)
def test_a_study_outside_the_tenant_is_404_and_the_backend_is_never_asked(
    gateway: GatewayUnderTest, route: str, operation: str
) -> None:
    """Tenant A names tenant B's study on tenant A's OWN path. 404, and no bytes.

    MOS-DATA-013: "A QIDO-RS or WADO-RS request naming a study that exists in the backend
    but is not in the caller's tenant set MUST return `404`, not `403`. A `403` here is an
    existence oracle ... Both outcomes are covered by the same acceptance check: never
    `2xx`, never any instance bytes."

    The second assertion is the stronger one and the reason the stub PACS exists: the
    Gateway must not have FORWARDED the request at all. A design that asks the PACS and
    then discards the answer satisfies "never 2xx" while having already pulled another
    tenant's pixels across the wire and into this process.
    """
    before = len(gateway.pacs.calls)
    response = gateway.get(route.format(t=TENANT_A, st=STUDY_B))

    assert response.status_code == 404, f"{operation}: {response.status_code}"
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert not gateway.pacs.asked_about(STUDY_B), (
        "the Gateway forwarded a foreign study to the PACS before refusing it"
    )
    assert len(gateway.pacs.calls) == before

    # The UID may appear in `instance` and NOWHERE else. RFC 9457 and MOS-API-036 both
    # define `instance` as the request path, and the caller supplied that path -- echoing
    # it back tells them nothing they did not already type. Any OTHER occurrence would be
    # the Gateway confirming the UID resolved to something.
    problem = response.json()
    assert STUDY_B in problem["instance"]
    elsewhere = {k: v for k, v in problem.items() if k != "instance"}
    assert STUDY_B not in json.dumps(elsewhere), (
        "the refusal names the UID outside `instance`, confirming its shape"
    )
    assert FIXTURE_PATIENT_NAME not in response.text
    assert b"DICM" not in response.content


def test_the_404_is_indistinguishable_from_a_study_that_does_not_exist(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-DATA-013's existence oracle, closed: the two 404s are byte-identical.

    A Gateway that answered `STUDY_NOT_FOUND` for an unknown UID and, say,
    `STUDY_NOT_IN_TENANT` for a foreign one would satisfy "returns 404" and still confirm
    that a guessed UID is present in the deployment -- which is the whole thing the
    requirement forbids.
    """
    unknown = "2.25.140737488355328000000000000000009999"
    foreign = gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_B}/metadata").json()
    absent = gateway.get(f"/dicomweb/{TENANT_A}/studies/{unknown}/metadata").json()

    # `instance` echoes the caller's own path and `trace_id` / `occurred_at` are per
    # request; everything that could carry a verdict must be identical.
    volatile = {"trace_id", "instance", "occurred_at"}
    assert {k: v for k, v in foreign.items() if k not in volatile} == {
        k: v for k, v in absent.items() if k not in volatile
    }


def test_the_denial_is_audited_even_though_the_caller_learns_nothing(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-SEC-148: class `phi` produces an AuditEvent on both allow and deny.

    And the deliberate asymmetry `medos/medos/gateway/audit.py` states: the caller gets a 404
    that says nothing, while the deployment's own trail records `study_not_in_tenant`
    together with the UID that was probed. "The point of MOS-DATA-013 is that the CALLER
    learns nothing, while the deployment's own audit trail is exactly where 'someone
    probed for this UID' must be visible."
    """
    gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_B}/metadata")

    denials = [
        row
        for row in gateway.audit_rows(action="phi.access", outcome="deny")
        if row["study_instance_uid"] == STUDY_B
    ]
    assert len(denials) == 1, "exactly one deny row for the probe"
    row = denials[0]
    assert str(row["tenant_id"]) == TENANT_A, "recorded against the PROBING tenant"
    assert row["detail"]["reason_code"] == "study_not_in_tenant"
    assert row["actor_id"] == "user:gw-test-viewer"
    assert row["pep"] == "gateway.retrieve"


# =====================================================================================
# 3. MOS-DATA-009 -- the `{t}` segment is not the effective tenant
# =====================================================================================
def test_a_spoofed_tenant_segment_is_403_and_audited_as_tenant_mismatch(
    gateway: GatewayUnderTest,
) -> None:
    """Tenant A's key on tenant B's path. 403, `tenant-mismatch`, and an audit row.

    MOS-DATA-009: "The `{t}` path segment is a routing convenience and MUST be compared
    for equality with the principal's tenant; on mismatch the Gateway MUST return `403`
    with `problem+json` ... and MUST emit an `AuditEvent` of type
    `gateway.tenant_mismatch`."

    403 here and 404 in the test above is not an inconsistency, and MOS-DATA-013 says so
    in as many words: "`403` is reserved for MOS-DATA-009 (tenant segment spoofing)".
    Spoofing the segment is a fact about the request, not about what the deployment holds,
    so there is no existence oracle to protect.
    """
    response = gateway.get(f"/dicomweb/{TENANT_B}/studies")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    problem = response.json()
    assert problem["type"].endswith("/tenant-mismatch")
    assert problem["status"] == 403

    rows = gateway.audit_rows(action="gateway.tenant_mismatch")
    assert len(rows) == 1, "MOS-DATA-009 requires the event, not just the status code"
    assert rows[0]["outcome"] == "deny"
    assert str(rows[0]["tenant_id"]) == TENANT_A, (
        "the event belongs to the principal's tenant, never to the spoofed segment"
    )


def test_spoofing_the_segment_reveals_nothing_about_the_other_tenants_studies(
    gateway: GatewayUnderTest,
) -> None:
    """The 403 body carries no attribute value, no verdict about the UID, and no bytes.

    As in the 404 case, `instance` is the caller's own request path and is exempt: RFC
    9457 defines that member as the path and the caller wrote it. Every other member is
    checked.
    """
    response = gateway.get(f"/dicomweb/{TENANT_B}/studies/{STUDY_B}/metadata")
    assert response.status_code == 403
    problem = response.json()
    elsewhere = json.dumps({k: v for k, v in problem.items() if k != "instance"})
    for leak in (STUDY_B, FIXTURE_PATIENT_NAME, FIXTURE_PATIENT_ID,
                 FIXTURE_STUDY_DESCRIPTION):
        assert leak not in elsewhere
    for leak in (FIXTURE_PATIENT_NAME, FIXTURE_PATIENT_ID, FIXTURE_STUDY_DESCRIPTION):
        assert leak not in response.text
    assert not gateway.pacs.asked_about(STUDY_B)


# =====================================================================================
# 4. The refusals are RFC 9457 problem+json
# =====================================================================================
def test_an_unauthenticated_request_is_401_problem_json(gateway: GatewayUnderTest) -> None:
    """MOS-DATA-017 / MOS-API-035. No credential, no route, and a challenge.

    `WWW-Authenticate: Bearer` is RFC 9110 section 11.6.1's requirement on any 401 and is
    what tells a client WHICH scheme to retry with; a 401 without it is a dead end.
    """
    response = gateway.get(f"/dicomweb/{TENANT_A}/studies", key=None)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.headers.get("www-authenticate") == "Bearer"
    problem = response.json()
    assert problem["status"] == 401
    assert problem["type"].startswith(PROBLEM_AUTHORITY)
    assert problem["instance"] == f"/dicomweb/{TENANT_A}/studies"


def test_an_unknown_credential_is_also_401_and_not_403(gateway: GatewayUnderTest) -> None:
    """A key that matches no principal is an authentication failure, not an authorisation
    one: 403 would say "this credential is real but insufficient", which is a fact about
    the deployment's principal set that an unauthenticated caller must not be handed."""
    response = gateway.get(f"/dicomweb/{TENANT_A}/studies", key="not-a-real-key")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)


@pytest.mark.parametrize(
    "path, key, expected",
    [
        (f"/dicomweb/{TENANT_A}/studies", None, 401),
        (f"/dicomweb/{TENANT_B}/studies", VIEWER_KEY, 403),
        (f"/dicomweb/{TENANT_A}/studies/{STUDY_B}/metadata", VIEWER_KEY, 404),
    ],
)
def test_every_refusal_carries_a_complete_rfc9457_document(
    gateway: GatewayUnderTest, path: str, key: str | None, expected: int
) -> None:
    """MOS-API-035 and MOS-API-036, on the three refusals chapter 3 names.

    MOS-API-036: "`https://spec.medicalos.org/problems/` is the only permitted prefix. A
    `type` under any other host is a defect."

    REPORTED SPEC DEFECT, NOT A TEST BUG: MOS-DATA-009 writes the tenant-mismatch type as
    `https://medicalos.dev/problems/tenant-mismatch`, under a DIFFERENT authority from the
    one MOS-API-036 declares to be the only permitted one -- and MOS-API-036 requires CI
    to "grep the repository AND THIS SPECIFICATION for `/problems/` URIs and fail on any
    other authority", which makes chapter 3's spelling a defect by chapter 10's own terms.
    The implementation follows MOS-API-036. This test asserts the authority MOS-API-036
    fixes and the SLUG both chapters agree on, which is the part that is not in dispute.
    """
    response = gateway.get(path, key=key)
    assert response.status_code == expected
    assert response.headers["content-type"].startswith(PROBLEM_JSON)

    problem = response.json()
    for member in ("type", "title", "status", "detail", "instance"):
        assert problem.get(member), f"RFC 9457 member {member!r} missing or empty"
    assert problem["status"] == expected
    assert problem["type"].startswith(PROBLEM_AUTHORITY), (
        f"MOS-API-036: {problem['type']!r} is not under the only permitted prefix"
    )
    assert problem["instance"] == path
    # Table 10.4-A's closed `class` enum (MOS-API-038). 401 and 403 are authorisation
    # outcomes; the 404 of MOS-DATA-013 is deliberately a plain client error, because
    # calling it an authz failure would itself be the existence oracle in a JSON member.
    assert problem["class"] == ("authz_error" if expected in (401, 403) else "client_error")


# =====================================================================================
# 5. MOS-DATA-022 -- one PHI-access audit row per PHI-bearing response
# =====================================================================================
def test_a_retrieval_writes_exactly_one_phi_access_row_naming_principal_and_job(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-DATA-022, and the whole reason chapter 3 puts a Gateway in front of the PACS.

    "The Gateway MUST emit one `AuditEvent` per PHI-bearing response. This is the only
    point in the architecture where a per-patient image access can be recorded, because
    every image read in the system passes through it."

    THIS TEST IS LOAD-BEARING IN A WAY THAT IS EASY TO MISS. `_write_access` catches every
    exception from the audit write and logs it, because the bytes have already left the
    building and raising would turn a completed retrieval into a 500 with the trail just
    as empty. That is the right call and it means a broken audit path is INVISIBLE from
    the response: the retrieval still returns 200. The only thing that can notice is an
    assertion on the row itself. (Measured while writing this file: a principal carrying a
    `job_id` with no `jobs` row produced 200 and no audit row at all, silently.)
    """
    response = gateway.get(
        f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/series/2.25.9001",
        key=WORKER_KEY,
        headers={"Accept": MULTIPART},
    )
    assert response.status_code == 200
    assert b"DICM" in response.content, "no pixels means nothing PHI-bearing happened"

    rows = [
        r
        for r in gateway.audit_rows(action="phi.access", outcome="allow")
        if r["detail"].get("operation") == "WADO-RS.series"
    ]
    assert len(rows) == 1, f"one row per PHI-bearing response; got {len(rows)}"
    row = rows[0]

    # -- it names the principal (MOS-SEC-146's five-field actor) ----------------------
    assert row["actor_kind"] == "service_account"
    assert row["actor_id"] == "svc:gw-test-worker"
    assert row["actor_auth"] == "api_key"
    assert row["actor_key_id"] == "worker-v1"

    # -- it names the job (MOS-DATA-022's `job_id`) -----------------------------------
    assert row["job_public_id"] == gateway.job_id, (
        "the audit row must join to the job whose token authorised the retrieval"
    )
    assert row["job_id"] is not None

    # -- and who the job acts for (MOS-SEC-147) ---------------------------------------
    assert row["on_behalf_of_id"] == "user:gw-test-clinician"
    assert row["on_behalf_of_role"] == "radiologist"

    # -- the MOS-DATA-022 payload -----------------------------------------------------
    assert row["study_instance_uid"] == STUDY_A
    assert row["detail"]["series_instance_uid"] == "2.25.9001"
    assert row["detail"]["consumer_class"] == "platform_writer"
    assert row["detail"]["instance_count"] == 1
    assert row["detail"]["bytes"] == len(response.content)
    assert row["action_class"] == "phi", (
        "MOS-SEC-148 puts class `phi` in the mandatory set; class `read` may be sampled, "
        "and a sampled per-patient image-access trail is not a trail"
    )
    assert row["pep"] == "gateway.retrieve"
    assert row["trace_id"]


def test_the_audit_row_carries_a_patient_surrogate_and_never_an_attribute_value(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-DATA-022 / MOS-STORE-307.

    "`patient_key` is a tenant-scoped surrogate, never a `PatientID`. No attribute value
    from the study, including `PatientName`, `StudyDescription` or `SeriesDescription`,
    MAY appear in this record."

    The stub PACS returns all three of those in every answer, so if any of them could
    reach the audit row through the retrieval path, this test sees it.
    """
    gateway.get(
        f"/dicomweb/{TENANT_A}/studies/{STUDY_A}",
        key=WORKER_KEY,
        headers={"Accept": MULTIPART},
    )
    rows = gateway.audit_rows(action="phi.access", outcome="allow")
    assert rows, "nothing was audited; the rest of this test would be vacuous"

    for row in rows:
        blob = json.dumps(row, default=str)
        for forbidden in (FIXTURE_PATIENT_NAME, FIXTURE_PATIENT_ID,
                          FIXTURE_STUDY_DESCRIPTION):
            assert forbidden not in blob, "an attribute value reached the audit trail"

    retrieval = [r for r in rows if r["detail"].get("operation") == "WADO-RS.study"]
    assert len(retrieval) == 1
    key = retrieval[0]["detail"]["patient_key"]
    assert key.startswith("pk_"), "MOS-DATA-022's surrogate, resolved from the projection"
    assert FIXTURE_PATIENT_ID not in key

    # Tenant-scoped: the surrogate is derived from `patients.id`, which is unique per
    # tenant, so the same hospital MRN in two tenants is two keys.
    with psycopg.connect(gateway.dsn, autocommit=True, row_factory=dict_row) as conn:
        ids = conn.execute(
            "SELECT tenant_id, id FROM patients WHERE issuer_of_patient_id = 'gw-test'"
        ).fetchall()
    assert len({str(r["id"]) for r in ids}) == 2


def test_a_qido_query_is_audited_too(gateway: GatewayUnderTest) -> None:
    """A study LIST is PHI-bearing: it carries PatientName for every study it returns.

    MOS-SEC-047 puts the query behind a different Policy Enforcement Point from the
    retrieve, and the row has to say which one -- an access review that cannot separate
    "listed the worklist" from "pulled the pixels" cannot answer either question.
    """
    gateway.get(f"/dicomweb/{TENANT_A}/studies")
    rows = [
        r
        for r in gateway.audit_rows(action="phi.access", outcome="allow")
        if r["detail"].get("operation") == "QIDO-RS.studies"
    ]
    assert len(rows) == 1
    assert rows[0]["pep"] == "gateway.query"
    assert rows[0]["detail"]["instance_count"] == 1
    assert rows[0]["detail"]["filtered_out"] == 1, (
        "the count of rows the tenancy filter removed is the evidence MOS-DATA-011 ran"
    )


# =====================================================================================
# 6. MOS-DATA-012 -- STOW-RS admission
# =====================================================================================
def _multipart_dicom(study_instance_uid: str) -> tuple[bytes, str]:
    """A minimal single-instance `multipart/related` STOW-RS body.

    Hand-built rather than pydicom-generated: `_study_uids_in_payload` reads the
    StudyInstanceUID out of the raw bytes, and a fixture that depends on a DICOM writer
    would be testing the writer. The 128-byte preamble + `DICM` is DICOM PS3.10's file
    meta prologue, which is what makes this a file the parser will look inside.
    """
    # (0020,000D) UI, explicit VR little endian, even-padded value.
    value = study_instance_uid.encode("ascii")
    if len(value) % 2:
        value += b"\x00"
    element = (
        b"\x20\x00\x0d\x00" + b"UI" + len(value).to_bytes(2, "little") + value
    )
    part = b"\x00" * 128 + b"DICM" + element
    boundary = "medostestboundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        + part
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return body, f'multipart/related; type="application/dicom"; boundary={boundary}'


def test_stow_rs_rejects_another_tenants_study_with_409_and_stores_nothing(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-DATA-012: "the whole request MUST be rejected with `409` and none of its
    instances stored".

    Tenant A STOWs an instance whose StudyInstanceUID tenant B already owns. The
    cross-tenant resolution happens on the global index -- a tenant-scoped read "reports
    the study as unknown and silently splits ownership" -- so this must be a 409 and not a
    second `studies` row.
    """
    before_a = gateway.studies_of(TENANT_A)
    before_b = gateway.studies_of(TENANT_B)
    body, content_type = _multipart_dicom(STUDY_B)

    response = gateway.post(f"/dicomweb/{TENANT_A}/studies", WORKER_KEY, body, content_type)

    assert response.status_code == 409, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert gateway.studies_of(TENANT_A) == before_a, "a rejected STOW left a projection row"
    assert gateway.studies_of(TENANT_B) == before_b
    assert not any(m == "POST" for m, _u in gateway.pacs.calls), (
        "the instance reached the PACS despite the 409; MOS-DATA-012 says none are stored"
    )
    denials = [
        r
        for r in gateway.audit_rows(action="phi.access", outcome="deny")
        if r["detail"].get("reason_code") == "study_owned_by_other_tenant"
    ]
    assert len(denials) == 1


def test_stow_rs_creates_the_projection_row_for_the_calling_tenant(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-DATA-012's other half, and MOS-DATA-010: the projection row IS the tenancy
    record, so a study the Gateway has never seen becomes the caller's by being stored.

    The audit row and the `studies` row are written in one transaction ("in the same
    transaction that records the STOW attempt"), which is why both are asserted together:
    either of them alone surviving would be the partial effect the requirement forbids.
    """
    new_study = "2.25.140737488355328000000000000000000777"
    body, content_type = _multipart_dicom(new_study)

    response = gateway.post(f"/dicomweb/{TENANT_A}/studies", WORKER_KEY, body, content_type)

    assert response.status_code == 200, response.text
    assert new_study in gateway.studies_of(TENANT_A)
    assert new_study not in gateway.studies_of(TENANT_B)
    assert any(m == "POST" for m, _u in gateway.pacs.calls), (
        "the instance never reached the PACS"
    )

    rows = [
        r
        for r in gateway.audit_rows(action="phi.access", outcome="allow")
        if r["detail"].get("operation") == "STOW-RS.study"
    ]
    assert len(rows) == 1
    assert rows[0]["study_instance_uid"] == new_study
    assert rows[0]["pep"] == "gateway.store"
    assert rows[0]["detail"]["studies_in_payload"] == 1


def test_a_read_only_principal_cannot_stow(gateway: GatewayUnderTest) -> None:
    """MOS-DATA-020: the platform DICOM writer "is the only principal that may STOW".

    The viewer holds `study.read` and nothing else, so the write scope check refuses it --
    403 and not 404, because MOS-DATA-013 reserves 403 for "a caller whose own tenant owns
    the study but who lacks" the scope.
    """
    body, content_type = _multipart_dicom("2.25.140737488355328000000000000000000888")
    response = gateway.post(f"/dicomweb/{TENANT_A}/studies", VIEWER_KEY, body, content_type)

    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_SCOPE"
    assert "2.25.140737488355328000000000000000000888" not in gateway.studies_of(TENANT_A)


# =====================================================================================
# 7. MOS-DATA-008 -- the surface is the table, and nothing else
# =====================================================================================
@pytest.mark.parametrize(
    "method, path",
    [
        ("DELETE", "/dicomweb/{t}/studies/{st}"),
        ("GET", "/dicomweb/{t}/patients"),
        ("GET", "/dicomweb/{t}/../system"),
        ("GET", "/tools/reset"),
        ("GET", "/dicomweb/{t}/studies/{st}/instances"),
    ],
)
def test_an_unmatched_path_is_404_problem_json_and_is_not_forwarded(
    gateway: GatewayUnderTest, method: str, path: str
) -> None:
    """MOS-DATA-008: "An unmatched path MUST return `404` with an RFC 9457 `problem+json`
    body and MUST NOT be forwarded", and no `DELETE` verb exists at all.

    `/tools/reset` is Orthanc's native administrative API: a Gateway with a catch-all
    proxy would hand a caller the ability to wipe the archive.
    """
    before = len(gateway.pacs.calls)
    response = gateway.client.request(
        method,
        path.format(t=TENANT_A, st=STUDY_A),
        headers={"Authorization": f"Bearer {VIEWER_KEY}"},
    )
    assert response.status_code in (404, 405), response.status_code
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert len(gateway.pacs.calls) == before, "an unmatched path was forwarded to the PACS"


# =====================================================================================
# 8. No PHI in the log stream
# =====================================================================================
class _Capture:
    """Every `LogRecord` the Gateway emits, flattened including its `extra` fields.

    Deliberately NOT the formatted output: a leak that reaches `record.__dict__` is one
    formatter change away from reaching the stream, and the production formatter here is
    a JSON one that serialises extras. Asserting on `vars(record)` is strictly stronger
    than asserting on any particular rendering of it.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __enter__(self) -> _Capture:
        import logging

        capture = self

        class Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                fields = {
                    k: v for k, v in vars(record).items() if k not in {"args", "exc_info"}
                }
                fields["message"] = record.getMessage()
                capture.lines.append(json.dumps(fields, default=str))

        self._handler = Handler()
        self._root = logging.getLogger()
        self._previous = self._root.level
        self._root.addHandler(self._handler)
        self._root.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._root.removeHandler(self._handler)
        self._root.setLevel(self._previous)

    @property
    def blob(self) -> str:
        return "\n".join(self.lines)


def test_no_patient_attribute_value_reaches_the_gateway_log_stream(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-DATA-022 / the spine's PHI rule: PHI "MUST NOT appear in structured logs".

    The stub PACS puts a PatientName, a PatientID and a StudyDescription into every answer
    it gives, and the retrieval below carries the name in the instance bytes as well, so
    this exercises both the metadata path and the streaming path.
    """
    with _Capture() as captured:
        gateway.get(f"/dicomweb/{TENANT_A}/studies")
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/metadata")
        gateway.get(
            f"/dicomweb/{TENANT_A}/studies/{STUDY_A}",
            key=WORKER_KEY,
            headers={"Accept": MULTIPART},
        )
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_B}/metadata")

    assert captured.lines, "nothing was logged; this test would pass vacuously"
    for forbidden, what in (
        (FIXTURE_PATIENT_NAME, "PatientName"),
        (FIXTURE_PATIENT_ID, "PatientID"),
        (FIXTURE_STUDY_DESCRIPTION, "StudyDescription"),
    ):
        assert forbidden not in captured.blob, f"{what} reached the log stream"


def test_no_pacs_credential_reaches_the_gateway_log_stream(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-DATA-005: the Gateway is the sole holder, and a log line is an exfiltration
    route as surely as an environment variable in another deployment unit is.

    `medos.gateway.app._forward` logs `backend_id` on every upstream call and
    `PacsBackend.summary()` deliberately reports only the credential's SCHEME; this is the
    assertion that keeps both true.
    """
    with _Capture() as captured:
        gateway.get(f"/dicomweb/{TENANT_A}/studies")
    assert "pacs-password" not in captured.blob
    assert "pacs-user" not in captured.blob


def test_no_dicom_uid_reaches_the_gateway_log_stream(gateway: GatewayUnderTest) -> None:
    """Chapter 3 acceptance check 14, the UID half. THIS TEST CURRENTLY FAILS.

        14. **PHI-free telemetry.** Over a full ingest -> triage -> job run, the
        structured log stream, the OTLP span attributes and the `/metrics` scrape contain
        zero occurrences of the fixture's `PatientName`, `PatientID`, any
        `SeriesDescription` string, and any `StudyInstanceUID`. (MOS-DATA-022, 026, 065)

        -- docs/spec/03-medical-data-plane.md, section 3.9

    THE DEFECT, NAMED: `medos.api.app.TraceContextMiddleware` emits one `http_request`
    record per request carrying the raw `path`, and every WADO-RS and QIDO-RS path in
    MOS-DATA-007's table has the `StudyInstanceUID` -- and often the `SeriesInstanceUID`
    and `SOPInstanceUID` -- embedded in it. The Gateway is the one service where that is
    unavoidable by construction, because its entire URL space is UIDs.

    Confirmed on the deployed container as well as here: `docker logs medos-gateway`
    carries the LCTSC corpus's study, series and SOP UIDs in the clear.

    NOT MARKED `xfail`, deliberately. An `xfail` reports green, and it lands in the skip
    taxonomy's `OTHER SKIPS` bucket as an unclassified line -- the two things
    `tests/_support/skips.py` exists to stop. The requirement is not met, so the test is
    red. The fix belongs in the middleware: log the route TEMPLATE
    (`/dicomweb/{t}/studies/{st}/metadata`) rather than the resolved path, which is also
    the only form that makes the field usable as a metric label under MOS-DATA-026.
    """
    with _Capture() as captured:
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/metadata")
        gateway.get(
            f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/series/2.25.9001",
            key=WORKER_KEY,
            headers={"Accept": MULTIPART},
        )

    assert captured.lines
    for uid in (STUDY_A, "2.25.9001"):
        assert uid not in captured.blob, (
            f"a DICOM UID reached the structured log stream: {uid[:9]}..."
        )


# -------------------------------------------------------------------------------------
# 8b. The same requirement, reached by routes the test above never touches.
#
# The fix for the UID leak was in `medos.api.app.TraceContextMiddleware`, which is the
# request-completion log line. These drive the FOUR other ways a UID-bearing request ends
# -- unmatched, unauthenticated, tenant-refused, and crashed -- because each of those is a
# different log call site in a different module, and a fix that only satisfied the
# happy-path assertion above would leave three of them leaking.
# -------------------------------------------------------------------------------------
def test_no_dicom_uid_reaches_the_log_stream_from_any_refusal_route(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-SEC-105's `Structured logs` row, via every refusal the Gateway can produce.

    `MOS-SEC-105` is explicit that the prohibition is total -- "'no' means MUST NOT
    appear, in any form, including inside a serialized error, an exception message, a
    stack trace or a URL" -- so the requirement is not discharged by the 200 path. The
    four requests below leave through four different code paths:

      * a path MOS-DATA-008 does not expose         -> Starlette `HTTPException` handler,
                                                        and `route_fields`' no-match branch
      * no credential                               -> `_authorise`'s 401
      * a `{t}` that is not the principal's tenant  -> MOS-DATA-009's 403
      * a study the caller's tenant does not own    -> MOS-DATA-013's 404

    Each is a separate `log` call in `medos/medos/gateway/app.py`, `medos/medos/api/app.py` or
    `medos/medos/api/problems.py`, and the request URL carrying the UID is the one string all
    four had in common.
    """
    other_uid = "2.25.777000111222333444555666777888999"
    with _Capture() as captured:
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/nonesuch")
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{other_uid}/metadata", key=None)
        gateway.get(f"/dicomweb/{TENANT_B}/studies/{STUDY_B}/metadata", key=VIEWER_KEY)
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_B}/metadata", key=VIEWER_KEY)

    assert captured.lines, "nothing was logged; this test would pass vacuously"
    for uid in (STUDY_A, STUDY_B, other_uid):
        assert uid not in captured.blob, (
            f"a DICOM UID reached the structured log stream on a refusal path: "
            f"{uid[:9]}..."
        )


def test_the_gateways_unhandled_500_path_logs_no_dicom_uid(
    pg_dsn: str, seeded: dict[str, Any]
) -> None:
    """The `gateway_unhandled` call site, which is the one that logs when things break.

    `medos/medos/gateway/app.py`'s `Exception` handler used to log `request.url.path`, so the
    single log line an operator most needs -- the 500 -- carried the most P2 of any line
    in the file: MOS-DATA-007's instance route is a study, a series AND a SOP UID. It is
    also the line least likely to be exercised, which is why it gets its own test rather
    than a shared one.

    The transport is made to raise something that is NOT a `requests.RequestException`,
    because every route catches that one and turns it into a clean 502.

    Its own `TestClient` with `raise_server_exceptions=False`, because Starlette's
    `ServerErrorMiddleware` runs the handler, sends the 500 and then RE-RAISES so the
    server can log -- and the module's shared client would surface that re-raise instead
    of the response. What is under test is the log line the handler wrote on its way past.
    """
    from fastapi.testclient import TestClient

    app, pacs = _build_gateway_app(pg_dsn, seeded, public_base=GATEWAY_PUBLIC_BASE)
    pacs.explode = ValueError("synthetic transport fault")
    with TestClient(app, raise_server_exceptions=False) as client, _Capture() as captured:
        response = client.get(
            f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/series/2.25.9001"
            f"/instances/2.25.9002",
            headers={"Authorization": f"Bearer {WORKER_KEY}", "Accept": MULTIPART},
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert any("gateway_unhandled" in line for line in captured.lines), (
        "the 500 did not reach `gateway_unhandled`; this test would pass vacuously"
    )
    for uid in (STUDY_A, "2.25.9001", "2.25.9002"):
        assert uid not in captured.blob, (
            f"a DICOM UID reached the log stream from the 500 path: {uid[:9]}..."
        )
    # MOS-SEC-105 forbids P2 in a `problem+json` body's *detail* too. `instance` is the
    # caller's own request URI echoed back to the caller and is permitted there by
    # MOS-API-006; a stack trace or a PACS URL in `detail` would not be.
    assert PACS_ORIGIN not in response.text


def test_the_log_stream_carries_the_route_template_and_a_study_ref(
    gateway: GatewayUnderTest,
) -> None:
    """MOS-SEC-106: the UID is REPLACED, not deleted. Without this the fix is a regression.

    "A P2 identifier needed for correlation MUST be replaced by `study_ref` (P1) on every
    surface where the matrix forbids P2." Deleting the path from the access log would
    satisfy `test_no_dicom_uid_reaches_the_gateway_log_stream` and leave an operator
    unable to answer "which requests touched this study" at all, so this asserts what
    took the UID's place:

      * `route` is MOS-SEC-117's template, which is also the only form usable as a metric
        label under MOS-DATA-026 and MOS-SEC-113;
      * `tenant_id` survives -- P1, and the one identifier MOS-SEC-105 admits in a metric
        label as well;
      * `study_ref` is present, is `medos.sdk.canonical.study_ref`'s value
        for this study, and is stable across two requests for the same study.
    """
    from medos.sdk.canonical import study_ref

    with _Capture() as captured:
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/metadata")
        gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/series/2.25.9001/instances")

    records = [json.loads(line) for line in captured.lines]
    access = [r for r in records if r.get("message") == "http_request"]
    assert len(access) == 2, [r.get("message") for r in records]

    assert access[0]["route"] == "/dicomweb/{t}/studies/{st}/metadata"
    assert access[1]["route"] == "/dicomweb/{t}/studies/{st}/series/{se}/instances"
    assert all("path" not in r for r in access), (
        "the resolved request path is back in the access log record"
    )
    expected = study_ref(TENANT_A, STUDY_A)
    assert expected.startswith("sr_")
    for record in access:
        assert record["tenant_id"] == TENANT_A
        assert record["study_ref"] == expected


# =====================================================================================
# 8c. No response the Gateway produces names the PACS.
#
# MOS-SEC-088: "OHIF, every worker, every service and every operator tool MUST address the
# DICOM Gateway, never the PACS. The PACS network address MUST NOT be resolvable or
# routable from `Z-EDGE`, `Z-SERVICE` or `Z-PLATFORM`." MOS-SEC-091 adds that the shape of
# the deployment behind the Gateway "MUST NOT be visible to any caller".
#
# `test_no_gateway_response_body_names_the_pacs_origin` in the live section proves the one
# case that was broken. These prove the CLASS: every route, both carriers (body and
# header), all three URL attributes of a STOW-RS reply, and the configuration in which the
# scrub used to silently do nothing.
# =====================================================================================
def _origin_offenders(response: Any) -> list[str]:
    """Every place in a response that names the backend. Headers as well as body."""
    found = [f"header {k}" for k, v in response.headers.items() if PACS_ORIGIN in v]
    if PACS_ORIGIN in response.text:
        found.append("body")
    return found


def test_no_gateway_route_publishes_the_pacs_origin_in_a_body_or_a_header(
    gateway: GatewayUnderTest,
) -> None:
    """Every route of MOS-DATA-007 that returns something a caller can read.

    THE REGRESSION THIS CATCHES, AND IT IS NOT THE ONE THE LIVE TEST CATCHES. The scrub
    used to be selected per route by a `rewrite_bulkdata` flag that two of the five JSON
    routes set. QIDO-RS returns `(0008,1190) RetrieveURL` as a standard return key
    (PS3.18 section 8.3.4.3), so plain study search published the origin with the flag
    working exactly as designed; and the streaming retrieve routes forwarded the
    backend's response HEADERS verbatim, which is a carrier no body rewrite reaches.

    A fix confined to `_stow`'s body would leave all five of the requests below leaking.
    """
    checks = {
        "QIDO-RS.studies": gateway.get(f"/dicomweb/{TENANT_A}/studies"),
        "QIDO-RS.series": gateway.get(f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/series"),
        "WADO-RS.study_metadata": gateway.get(
            f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/metadata"
        ),
        "WADO-RS.instance": gateway.get(
            f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/series/2.25.9001"
            f"/instances/2.25.9002",
            key=WORKER_KEY,
            headers={"Accept": MULTIPART},
        ),
        "STOW-RS.study": gateway.post(
            f"/dicomweb/{TENANT_A}/studies/{STUDY_A}",
            WORKER_KEY,
            *_multipart_dicom(STUDY_A),
        ),
    }
    leaked = {
        operation: _origin_offenders(response)
        for operation, response in checks.items()
        if _origin_offenders(response)
    }
    assert not leaked, f"the PACS origin reached the caller on: {leaked}"

    # Not vacuous: the stub puts the origin in each of those places, so every response
    # above had something to scrub.
    assert checks["QIDO-RS.studies"].status_code == 200
    assert GATEWAY_PUBLIC_BASE in checks["QIDO-RS.studies"].text, (
        "the QIDO-RS RetrieveURL was removed rather than re-pointed at the Gateway"
    )

    # MOS-SEC-091, the product rather than the address: the PACS's own `Server` header
    # names which PACS is behind the Gateway and is not forwarded.
    for operation, response in checks.items():
        assert "StubPacs" not in " ".join(response.headers.values()), operation


def test_a_stow_reply_is_scrubbed_in_every_url_attribute_it_carries(
    gateway: GatewayUnderTest,
) -> None:
    """The three places PS3.18 puts a URL in a store response, not just the top-level one.

    PS3.18 section 10.5's store response has `(0008,1190) RetrieveURL` at the top level,
    one per item of `(0008,1199) ReferencedSOPSequence` and one per item of
    `(0008,1198) FailedSOPSequence`. A scrub that walked the top level -- or that read one
    named attribute -- would pass the live test and still hand out two nested copies of
    the origin, which is why this asserts on the parsed document and not on a substring.
    """
    response = gateway.post(
        f"/dicomweb/{TENANT_A}/studies",
        WORKER_KEY,
        *_multipart_dicom(STUDY_A),
    )
    assert response.status_code in (200, 202), response.status_code
    document = response.json()

    urls = [document[TAG_RETRIEVE_URL]["Value"][0]]
    for sequence in (TAG_REFERENCED_SOP_SEQ, TAG_FAILED_SOP_SEQ):
        for item in document[sequence]["Value"]:
            urls.append(item[TAG_RETRIEVE_URL]["Value"][0])
    assert len(urls) == 3, urls
    for url in urls:
        assert PACS_ORIGIN not in url
        assert url.startswith(f"{GATEWAY_PUBLIC_BASE}/dicomweb/{TENANT_A}"), url


def test_the_pacs_origin_is_scrubbed_with_no_public_base_configured(
    pg_dsn: str, seeded: dict[str, Any]
) -> None:
    """The configuration in which the scrub used to be a no-op. MOS-SEC-088.

    `_rewrite_bulkdata` began `if not base: return body`, so the confidentiality of the
    PACS address depended on `MEDOS_GATEWAY_PUBLIC_BASE` being set -- an operator who
    never set it got a Gateway that published `http://pacs.invalid:8042/...` on every
    metadata response, and nothing in the deployment said so. That is a fail-OPEN default
    for a network-topology control, and MOS-DATA-025 sets the opposite posture for this
    service.

    With no base configured the URL becomes the relative reference `/dicomweb/{t}/...`,
    which RFC 3986 section 4.2 resolves against the document's own retrieval URI. The
    caller is already talking to the Gateway, so the link still works.
    """
    from fastapi.testclient import TestClient

    app, _pacs = _build_gateway_app(pg_dsn, seeded, public_base="")
    with TestClient(app) as client:
        response = client.get(
            f"/dicomweb/{TENANT_A}/studies/{STUDY_A}/metadata",
            headers={"Authorization": f"Bearer {VIEWER_KEY}", "Accept": DICOM_JSON},
        )
    assert response.status_code == 200
    assert not _origin_offenders(response), (
        "with MEDOS_GATEWAY_PUBLIC_BASE unset the Gateway published the PACS origin"
    )
    url = response.json()[0][TAG_RETRIEVE_URL]["Value"][0]
    assert url.startswith(f"/dicomweb/{TENANT_A}/"), url


# =====================================================================================
# =====================================================================================
#  THE LIVE STACK
#
#  Everything below drives the medos-gateway CONTAINER in front of the real Orthanc.
#  The Gateway being down is `skip_infra`; an archive with no study in it is
#  `skip_no_data`, because the only way this deployment's archive is populated is the
#  private LCTSC/TCIA corpus.
# =====================================================================================
# =====================================================================================
LIVE_TENANT = os.environ.get(
    "MEDOS_TENANT_ID", "00000000-0000-0000-0000-000000000000"
)
LIVE_VIEWER_KEY = os.environ.get("MEDOS_GATEWAY_VIEWER_KEY", "medos-dev-viewer-key")
LIVE_WORKER_KEY = os.environ.get("MEDOS_GATEWAY_WORKER_KEY", "medos-dev-worker-key")

PACS_CONTAINER = "medos-orthanc"
GATEWAY_CONTAINER = "medos-gateway"
NON_GATEWAY_CONTAINERS = ("medos-worker", "medos-api", "medos-web")
PACS_PORT = 8042


class LiveGateway:
    """The live Gateway's host surface, plus the keys the compose stack issues."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.root = f"{self.base}/dicomweb/{LIVE_TENANT}"
        self.session = requests.Session()

    def get(self, path: str, *, key: str | None = LIVE_VIEWER_KEY, accept: str = DICOM_JSON,
            timeout: int = 120) -> requests.Response:
        headers = {"Accept": accept}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        return self.session.get(f"{self.base}{path}", headers=headers, timeout=timeout)

    def dicomweb(self, path: str, **kw: Any) -> requests.Response:
        return self.get(f"/dicomweb/{LIVE_TENANT}{path}", **kw)


@pytest.fixture(scope="module")
def live_gateway() -> LiveGateway:
    """The medos-gateway container, proved ready before anything is asserted about it.

    `/readyz` and not `/healthz`: liveness deliberately does not touch the database, so a
    liveness probe stays green through exactly the outage that makes every tenancy
    decision below meaningless. This is the same split `tests/_support/stack.py` documents
    and the same reason.
    """
    base = _stack.gateway_url()
    try:
        ready = requests.get(f"{base}/readyz", timeout=10)
    except requests.RequestException as exc:
        skip_infra(f"{base}/readyz: {type(exc).__name__}: {exc}", dependency="medos-gateway")
    if ready.status_code != 200:
        skip_infra(
            f"{base}/readyz -> HTTP {ready.status_code}: the Gateway is answering but is "
            f"not ready, so it cannot serve a tenancy decision",
            dependency="medos-gateway",
        )
    return LiveGateway(base)


@pytest.fixture(scope="module")
def live_study(live_gateway: LiveGateway) -> dict[str, str]:
    """One study, series and instance that really exist in the live archive.

    Discovered THROUGH the Gateway, because there is no longer any other way to look:
    MOS-DATA-006 removed the host's route to Orthanc. An empty archive is `skip_no_data`
    and not `skip_infra` -- the stack is working perfectly, it just has nothing in it, and
    the only thing that populates it here is the private LCTSC/TCIA corpus, which is never
    redistributed with this repository.
    """
    studies = live_gateway.dicomweb("/studies")
    if studies.status_code == 204 or not studies.content:
        skip_no_data(
            "the live archive holds no study, so there is nothing to retrieve through "
            "the Gateway (ingest the corpus with tests/e2e/test_demo.py first)",
            corpus="lctsc-corpus",
        )
    if studies.status_code != 200:
        skip_infra(
            f"QIDO-RS through the live Gateway answered HTTP {studies.status_code}",
            dependency="medos-gateway",
        )
    study = studies.json()[0][TAG_STUDY_UID]["Value"][0]

    series_response = live_gateway.dicomweb(f"/studies/{study}/series")
    assert series_response.status_code == 200, series_response.status_code
    series = series_response.json()[0][TAG_SERIES_UID]["Value"][0]

    instances = live_gateway.dicomweb(f"/studies/{study}/series/{series}/instances")
    assert instances.status_code == 200, instances.status_code
    sop = instances.json()[0][TAG_SOP_UID]["Value"][0]

    return {"study": study, "series": series, "sop": sop}


@pytest.fixture(scope="module")
def live_db() -> Iterator[psycopg.Connection[Any]]:
    """The deployment's own database, for reading the audit trail the Gateway wrote."""
    dsn = _stack.database_url()
    try:
        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        skip_infra(f"{dsn.rsplit('@', 1)[-1]}: {exc}", dependency="postgres")
    try:
        yield conn
    finally:
        conn.close()


def _docker() -> str:
    path = shutil.which("docker")
    if path is None:
        skip_infra(
            "the docker CLI is not on PATH, so the deployment's network isolation cannot "
            "be established. Refusing to assume it holds.",
            dependency="docker",
        )
    return path


def _docker_out(*args: str, timeout: int = 60) -> tuple[int, str]:
    proc = subprocess.run(
        [_docker(), *args], capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# =====================================================================================
# 9. MOS-DATA-002 / 006 -- the Gateway is the ONLY route to the PACS
# =====================================================================================
def test_the_pacs_publishes_no_port_to_this_host(live_gateway: LiveGateway) -> None:
    """MOS-DATA-002: the Gateway is the only route in. Not even the host has another.

    MOS-DATA-006's own sentence is about containers ("A deployment in which any other
    CONTAINER can open a TCP connection to the PACS port is non-conformant"), and
    `medos/deploy/compose/docker-compose.yml` has carried a host publication as a stated
    deviation so that host-side fixtures could drive Orthanc's native REST API. That
    deviation is what this test refuses: with the port published, `MEDOS_DICOMWEB_URL` on
    the host reaches the PACS directly and every tenancy filter, every de-identification
    decision and every MOS-DATA-022 audit row is one URL away from being skipped.

    Asserted against the RUNNING container rather than the file, because the running
    container is what an attacker meets.
    """
    code, out = _docker_out("inspect", PACS_CONTAINER, "--format",
                            "{{json .NetworkSettings.Ports}}")
    if code != 0:
        skip_infra(f"docker inspect {PACS_CONTAINER}: {out.strip()[:160]}",
                   dependency="orthanc")
    published = {
        port: bindings for port, bindings in json.loads(out.strip()).items() if bindings
    }
    assert not published, (
        f"{PACS_CONTAINER} publishes {sorted(published)} to the host. MOS-DATA-002 makes "
        "the Gateway the only route to the PACS; delete the `ports:` block from the "
        "`orthanc` service in medos/deploy/compose/docker-compose.yml, whose own comment "
        "already calls it 'the ONE remaining MOS-DATA-006 deviation'."
    )


def test_nothing_on_this_host_answers_as_the_pacs(live_gateway: LiveGateway) -> None:
    """And the port really is closed, not merely unmapped.

    A status code is not proof in either direction: something else may legitimately own
    127.0.0.1:8042 on a developer laptop. So an open port is only a failure when whatever
    is behind it identifies itself as Orthanc, which is what `/system` does.
    """
    try:
        socket.create_connection(("127.0.0.1", PACS_PORT), 2).close()
    except OSError:
        return  # closed: exactly right
    try:
        response = requests.get(f"http://127.0.0.1:{PACS_PORT}/system", timeout=5)
        payload = response.json() if response.ok else {}
    except (requests.RequestException, ValueError):
        payload = {}
    if not isinstance(payload, dict) or "Version" not in payload:
        skip_environment(
            f"something answers on 127.0.0.1:{PACS_PORT} but it is not Orthanc's REST "
            "API, so this machine cannot say whether the PACS is host-reachable",
            detail="foreign-listener-on-pacs-port",
        )
    pytest.fail(
        f"Orthanc's native REST API answered on 127.0.0.1:{PACS_PORT}. That API is the "
        "one MOS-DATA-008 forbids the Gateway to expose at all, and reaching it bypasses "
        "every tenancy and audit control in MOS-DATA-009..022."
    )


def test_no_container_but_the_gateway_can_open_a_tcp_connection_to_the_pacs() -> None:
    """MOS-DATA-006, in its own words and by its own test.

    "A deployment in which any other container can open a TCP connection to the PACS port
    is non-conformant, whether or not it has a credential."

    The probe targets the PACS's IP ADDRESS and not its DNS name. Name resolution failing
    is weak evidence -- it proves the container is not on the `pacs` network's DNS domain,
    not that the route is absent -- and a deployment that added the PACS to a shared
    network while leaving the alias off would pass a DNS-only check while being wide open.
    """
    code, out = _docker_out(
        "inspect", PACS_CONTAINER, "--format",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
    )
    if code != 0:
        skip_infra(f"docker inspect {PACS_CONTAINER}: {out.strip()[:160]}",
                   dependency="orthanc")
    addresses = [a for a in out.split() if a]
    assert addresses, f"{PACS_CONTAINER} has no IP address; it is not running"

    probed: dict[str, str] = {}
    for container in NON_GATEWAY_CONTAINERS:
        for address in addresses:
            verdict = _tcp_probe_from(container, address, PACS_PORT)
            if verdict is None:
                continue
            probed[f"{container} -> {address}"] = verdict
    if not probed:
        skip_environment(
            "no non-Gateway container offers a usable TCP probe (python, nc or wget), so "
            "MOS-DATA-006 cannot be established from inside the deployment",
            detail="no-tcp-probe-in-containers",
        )

    reached = sorted(k for k, v in probed.items() if v == "reached")
    assert not reached, (
        f"MOS-DATA-006 violated: {reached} opened a TCP connection to the PACS port. "
        "Only the Gateway may. Check the `networks:` membership in "
        "medos/deploy/compose/docker-compose.yml."
    )

    # THE CONTROL, and it is not decoration. Every verdict above is a NEGATIVE -- "no
    # connection was opened" -- and a negative from a probe that cannot work is
    # indistinguishable from isolation. So the same probe is pointed at the one container
    # that MUST succeed. If it does not, nothing above has been established, and the
    # taxonomy's own rule applies: a probe that cannot see its dependency must not report
    # on it in either direction.
    gateway_verdicts = {
        address: _tcp_probe_from(GATEWAY_CONTAINER, address, PACS_PORT)
        for address in addresses
    }
    if "reached" not in gateway_verdicts.values():
        skip_infra(
            f"the Gateway container could not open a connection to the PACS "
            f"({gateway_verdicts}). Either it is restarting or the deployment's own "
            f"route is broken; in both cases the isolation of the containers above is "
            f"unproven rather than proven.",
            dependency="medos-gateway",
        )


# Output that means "this container could not be asked", as opposed to "this container
# was asked and could not connect". The difference is the whole test: a container that is
# absent or restarting is not evidence of isolation.
_UNPROBEABLE = (
    "no such container",
    "is not running",
    "is restarting",
    "executable file not found",
    "oci runtime exec failed",
    "cannot exec",
)


def _tcp_probe_from(container: str, address: str, port: int) -> str | None:
    """`reached`, `refused`, or None when the container cannot be asked at all.

    Three tools tried in order because the images differ on purpose: the medos image has
    python, and the OHIF image is an nginx alpine with busybox `nc` and no python at all.

    The IP address is the target and not the DNS name: a container that cannot RESOLVE
    `orthanc` may still be able to route to it, and MOS-DATA-006 is about the TCP
    connection, not about the name.
    """
    python = (
        "import socket\n"
        f"socket.create_connection(('{address}', {port}), 4).close()\n"
        "print('MEDOS_REACHED')\n"
    )
    attempts = (
        ["exec", container, "python", "-c", python],
        ["exec", container, "python3", "-c", python],
        ["exec", container, "sh", "-c",
         f"nc -w 4 -z {address} {port} && echo MEDOS_REACHED || echo MEDOS_REFUSED"],
    )
    for args in attempts:
        code, out = _docker_out(*args, timeout=90)
        if "MEDOS_REACHED" in out:
            return "reached"
        if any(marker in out.lower() for marker in _UNPROBEABLE):
            continue  # the tool or the container is missing; this says nothing
        if "MEDOS_REFUSED" in out or code != 0:
            return "refused"
    return None


def test_the_pacs_credential_reaches_exactly_one_deployment_unit() -> None:
    """MOS-DATA-005 and chapter 3's acceptance check 1.

    "`grep -r` over the rendered deployment manifests finds the PACS credential reference
    in exactly one deployment unit (the Gateway). Any second occurrence fails."

    Read off the RUNNING containers rather than the compose file: the file is the
    intention and the container environment is what the process can actually read.
    """
    holders: dict[str, list[str]] = {}
    for container in (GATEWAY_CONTAINER, *NON_GATEWAY_CONTAINERS):
        code, out = _docker_out("inspect", container, "--format",
                                "{{range .Config.Env}}{{println .}}{{end}}")
        if code != 0:
            skip_infra(f"docker inspect {container}: {out.strip()[:160]}",
                       dependency="compose-stack")
        names = [
            line.split("=", 1)[0]
            for line in out.splitlines()
            if line.startswith("MEDOS_GATEWAY_PACS_")
        ]
        if names:
            holders[container] = sorted(names)

    assert set(holders) == {GATEWAY_CONTAINER}, (
        f"MOS-DATA-002: the PACS address and credential are visible to {sorted(holders)}; "
        f"only {GATEWAY_CONTAINER} may hold them"
    )


def test_the_worker_and_the_viewer_are_routed_through_the_gateway() -> None:
    """MOS-DATA-015 and MOS-DATA-017: the two consumers reach DICOM only through here.

    The worker's `MEDOS_DICOMWEB_URL` must name the Gateway, and it must present a bearer
    TOKEN rather than a credential. The viewer's nginx must have no route to the PACS --
    `medos/deploy/compose/nginx.conf.template` used to proxy `/dicomweb/` straight to `orthanc`,
    and that is exactly the configuration MOS-DATA-015 forbids.
    """
    code, out = _docker_out("inspect", "medos-worker", "--format",
                            "{{range .Config.Env}}{{println .}}{{end}}")
    if code != 0:
        skip_infra(f"docker inspect medos-worker: {out.strip()[:160]}",
                   dependency="medos-worker")
    env = dict(
        line.split("=", 1) for line in out.splitlines() if "=" in line and line[0].isalpha()
    )
    dicomweb = env.get("MEDOS_DICOMWEB_URL", "")
    assert dicomweb, "the worker has no DICOMweb route at all"
    assert GATEWAY_CONTAINER in dicomweb, (
        f"the worker's DICOMweb route is {dicomweb.split('://')[-1].split('/')[0]!r}; "
        "MOS-DATA-001 says no component other than the Gateway may read from a PACS"
    )
    assert "orthanc" not in dicomweb.lower()
    assert env.get("MEDOS_DICOMWEB_TOKEN"), (
        "MOS-DATA-017: a Service receives a scoped Gateway TOKEN, never a credential"
    )
    assert not env.get("MEDOS_DICOMWEB_PASSWORD"), (
        "MOS-DATA-002: the worker holds something shaped like a PACS credential"
    )

    template = (
        _stack.REPO_ROOT / "medos" / "deploy" / "compose" / "nginx.conf.template"
    ).read_text(encoding="utf-8")
    upstreams = re.findall(r"proxy_pass\s+http://\$?([A-Za-z0-9_.-]+)", template)
    assert "orthanc" not in {u.lower() for u in upstreams}, (
        f"the viewer's nginx proxies to {sorted(set(upstreams))}; MOS-DATA-015 requires "
        "the clinician surface to be configured against the Gateway and not against "
        "the PACS (the requirement named OHIF until specification 0.4.0)"
    )


# =====================================================================================
# 10. MOS-DATA-007 -- QIDO-RS, WADO-RS and STOW-RS all work through the live Gateway
# =====================================================================================
def test_qido_rs_works_through_the_live_gateway_with_a_valid_key(
    live_gateway: LiveGateway, live_study: dict[str, str]
) -> None:
    """All three QIDO-RS rows of MOS-DATA-007's table, against the real archive."""
    studies = live_gateway.dicomweb("/studies")
    assert studies.status_code == 200
    assert studies.headers["content-type"].startswith(DICOM_JSON)
    assert any(
        row[TAG_STUDY_UID]["Value"][0] == live_study["study"] for row in studies.json()
    )

    series = live_gateway.dicomweb(f"/studies/{live_study['study']}/series")
    assert series.status_code == 200
    assert {r[TAG_SERIES_UID]["Value"][0] for r in series.json()} >= {live_study["series"]}

    instances = live_gateway.dicomweb(
        f"/studies/{live_study['study']}/series/{live_study['series']}/instances"
    )
    assert instances.status_code == 200
    assert instances.json(), "a series with no instance is not a series"


def test_wado_rs_returns_real_dicom_bytes_through_the_live_gateway(
    live_gateway: LiveGateway, live_study: dict[str, str]
) -> None:
    """WADO-RS instance retrieve, `multipart/related; type="application/dicom"`.

    MOS-DATA-023 requires that Accept header to be supported; the `DICM` magic at offset
    128 (PS3.10 section 7.1) is what proves the body is a DICOM file and not an error
    page with a 200 on it.
    """
    response = live_gateway.dicomweb(
        f"/studies/{live_study['study']}/series/{live_study['series']}"
        f"/instances/{live_study['sop']}",
        accept=MULTIPART,
    )
    assert response.status_code == 200, response.status_code
    assert response.headers["content-type"].startswith("multipart/related")
    part = _first_dicom_part(response.content, response.headers["content-type"])
    assert part[128:132] == b"DICM", "the retrieved part is not a DICOM file"
    assert len(part) > 1024


def test_wado_rs_metadata_points_bulkdata_at_the_gateway_and_not_at_the_pacs(
    live_gateway: LiveGateway, live_study: dict[str, str]
) -> None:
    """MOS-DATA-002, one layer up from the credential.

    A metadata document whose `BulkDataURI` names `http://orthanc:8042/...` hands the
    browser the PACS origin -- "the same failure in a different coat", as
    `_rewrite_bulkdata` puts it -- and MOS-DATA-006 has just made that URL unreachable, so
    an un-rewritten document is also simply broken for the viewer.
    """
    response = live_gateway.dicomweb(f"/studies/{live_study['study']}/metadata")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(DICOM_JSON)
    assert "orthanc:8042" not in response.text, (
        "the metadata document names the PACS origin"
    )


def test_stow_rs_works_through_the_live_gateway_with_a_valid_key(
    live_gateway: LiveGateway, live_study: dict[str, str]
) -> None:
    """STOW-RS, round-tripped: retrieve one instance and store the same bytes back.

    Storing an instance the archive ALREADY holds is deliberate. STOW-RS is idempotent by
    SOPInstanceUID, so this exercises the whole admission path of MOS-DATA-012 -- the
    cross-tenant ownership resolution, the `studies` upsert and the audit row -- against a
    real PACS while creating nothing and therefore needing no cleanup. The alternative,
    minting fresh UIDs, would leave a synthetic study in a developer's archive that
    nothing in this suite can delete: MOS-DATA-008 forbids the Gateway a `DELETE` verb,
    and MOS-DATA-006 has removed the host's route to Orthanc's native API.
    """
    retrieved = live_gateway.dicomweb(
        f"/studies/{live_study['study']}/series/{live_study['series']}"
        f"/instances/{live_study['sop']}",
        accept=MULTIPART,
    )
    assert retrieved.status_code == 200
    part = _first_dicom_part(retrieved.content, retrieved.headers["content-type"])
    assert part[128:132] == b"DICM"

    boundary = "medoslivestowboundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        + part
        + f"\r\n--{boundary}--\r\n".encode()
    )
    response = live_gateway.session.post(
        f"{live_gateway.root}/studies",
        headers={
            "Authorization": f"Bearer {LIVE_WORKER_KEY}",
            "Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}',
            "Accept": DICOM_JSON,
        },
        data=body,
        timeout=180,
    )
    assert response.status_code in (200, 202), f"{response.status_code}: {response.text[:200]}"
    assert response.headers["content-type"].startswith(DICOM_JSON)
    assert TAG_RETRIEVE_URL in response.json(), (
        "PS3.18 6.6.1.4: a STOW-RS response carries a RetrieveURL"
    )


def _first_dicom_part(body: bytes, content_type: str) -> bytes:
    """The first `application/dicom` part of a `multipart/related` body."""
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    assert match, f"no boundary in {content_type!r}"
    separator = b"--" + match.group(1).encode("ascii")
    parts = body.split(separator)
    assert len(parts) >= 2, "the multipart body has no part in it"
    _headers, _, payload = parts[1].partition(b"\r\n\r\n")
    return payload[:-2] if payload.endswith(b"\r\n") else payload


# =====================================================================================
# 11. The live refusals
# =====================================================================================
def test_the_live_gateway_refuses_an_unauthenticated_request(
    live_gateway: LiveGateway,
) -> None:
    """401 + `application/problem+json`, on the deployed surface and not in a TestClient."""
    response = live_gateway.dicomweb("/studies", key=None)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    assert response.headers.get("WWW-Authenticate") == "Bearer"
    problem = response.json()
    assert problem["status"] == 401
    assert problem["type"].startswith(PROBLEM_AUTHORITY)


def test_the_live_gateway_refuses_a_spoofed_tenant_segment(
    live_gateway: LiveGateway,
) -> None:
    """403 + `.../tenant-mismatch`, and no DICOM attribute in the body (MOS-DATA-009).

    Chapter 3's acceptance check 3 in full: "`GET /dicomweb/t_B/studies/{tenant_B_study_uid}
    /metadata` with tenant A's key returns `403` with `type: .../tenant-mismatch`. Neither
    response contains any DICOM attribute."
    """
    other_tenant = "11111111-1111-1111-1111-111111111111"
    assert other_tenant != LIVE_TENANT
    response = live_gateway.get(f"/dicomweb/{other_tenant}/studies")

    assert response.status_code == 403
    assert response.headers["content-type"].startswith(PROBLEM_JSON)
    problem = response.json()
    assert problem["type"].startswith(PROBLEM_AUTHORITY)
    assert problem["type"].endswith("/tenant-mismatch")
    # No DICOM attribute: a problem document has no DICOM tag keys in it at all.
    assert not re.search(r'"[0-9A-F]{8}"\s*:', response.text)


def test_the_live_gateway_does_not_proxy_the_pacs_administrative_api(
    live_gateway: LiveGateway,
) -> None:
    """MOS-DATA-008: no native REST API, no unmatched-path proxy, no `DELETE`.

    `/tools/reset` and `/system` are Orthanc's own; `/studies` without the `/dicomweb/{t}`
    prefix is the shape a mis-copied viewer configuration produces.
    """
    for path in ("/system", "/tools/reset", "/studies", f"/dicomweb/{LIVE_TENANT}/patients"):
        response = live_gateway.get(path)
        assert response.status_code in (404, 405), f"{path} -> {response.status_code}"
        assert response.headers["content-type"].startswith(PROBLEM_JSON), path
        assert "Version" not in response.text, f"{path} reached Orthanc's /system"


# =====================================================================================
# 12. MOS-DATA-022 on the live stack, and the container log stream
# =====================================================================================
def test_a_live_retrieval_writes_a_phi_access_audit_row(
    live_gateway: LiveGateway, live_study: dict[str, str], live_db: psycopg.Connection[Any]
) -> None:
    """The deployed Gateway records the per-patient access, naming the principal.

    The row is written from the streaming generator's `finally`, after the response has
    been handed to the client, so there is a real race between this assertion and the
    write. It is polled rather than slept on: a fixed sleep is either flaky or slow, and
    on a fast host the row is usually already there on the first look.

    Correlated on `recorded_at` from the DATABASE's own clock and not on `seq`. `seq` is
    MOS-SEC-151's per-tenant gapless counter and would be the natural watermark, but on a
    deployment whose Postgres volume predates migration `0003_audit_provenance` the
    `audit_events_chain` trigger does not exist and every row lands with the writer's
    placeholder `seq = 1` -- see `test_the_phi_access_row_the_gateway_wrote_is_chained`,
    which is the test that holds that. A watermark that is constant is not a watermark,
    and this test must fail for Gateway reasons only.
    """
    started = live_db.execute("SELECT now() AS t").fetchone()["t"]

    response = live_gateway.dicomweb(
        f"/studies/{live_study['study']}/series/{live_study['series']}"
        f"/instances/{live_study['sop']}",
        accept=MULTIPART,
    )
    assert response.status_code == 200
    transferred = len(response.content)

    rows: list[dict[str, Any]] = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        rows = live_db.execute(
            "SELECT * FROM audit_events WHERE tenant_id = %s AND recorded_at >= %s "
            "AND action = 'phi.access' AND outcome = 'allow' ORDER BY recorded_at",
            (LIVE_TENANT, started),
        ).fetchall()
        if any(r["detail"].get("operation") == "WADO-RS.instance" for r in rows):
            break
        time.sleep(0.25)

    retrievals = [r for r in rows if r["detail"].get("operation") == "WADO-RS.instance"]
    assert len(retrievals) == 1, (
        f"MOS-DATA-022 requires one AuditEvent per PHI-bearing response; got "
        f"{len(retrievals)} in 15s"
    )
    row = retrievals[0]
    assert row["study_instance_uid"] == live_study["study"]
    assert row["detail"]["series_instance_uid"] == live_study["series"]
    assert row["actor_id"], "the row must name the principal"
    assert row["actor_kind"] in {"user", "service_account", "workload", "service_version",
                                 "platform_admin"}
    assert row["action_class"] == "phi"
    assert row["pep"] == "gateway.retrieve"
    assert row["detail"]["bytes"] == transferred
    assert row["detail"]["patient_key"].startswith("pk_")
    assert row["trace_id"]

    # MOS-DATA-022's prohibition, checked against the live row rather than a stub's.
    blob = json.dumps(row, default=str)
    for key in ("PatientName", "patient_name", "StudyDescription", "AccessionNumber"):
        assert key not in blob


def test_the_phi_access_row_the_gateway_wrote_is_chained(
    live_gateway: LiveGateway, live_study: dict[str, str], live_db: psycopg.Connection[Any]
) -> None:
    """The MOS-DATA-022 row must land CHAINED, not carrying the writer's placeholders.

    THIS TEST FAILS ON A STALE DEPLOYMENT, AND THAT IS WHAT IT IS FOR.

    `medos/medos/db/audit.py` inserts literal placeholders -- `1`, `'0' * 64`, `'0' * 64` -- for
    `seq`, `prev_hash` and `hash`, with the comment "audit_events_chain_trg overwrites all
    three before the row lands (MOS-SEC-151); they are NOT NULL, so something must be
    sent." That is a correct design and it has one consequence: if the trigger is absent,
    NOTHING FAILS. Every row is accepted, every row says `seq = 1`, every row's hash is
    sixty-four zeroes, and the per-patient PHI-access trail the Gateway exists to produce
    is an unordered heap that no `audit_verify_chain` can check.

    Measured on this development stack while writing this file: `schema_migrations` holds
    `0003_audit_provenance` -- the name migration `0003_audit_provenance.up.sql` used to
    have -- the `audit_events_chain` trigger is not installed, and all 82 audit rows in
    the deployment carry `seq = 1`. The compose stack applies migrations from
    `docker-entrypoint-initdb.d`, which runs ONLY on the first boot of an empty volume, so
    a stack whose volume predates the rename never gets the trigger and never says so.

    On a freshly created deployment (`docker compose down -v && up -d --build`) and in
    CI this test passes: `medos.db.conn.apply_schema` applies every migration in
    `medos/medos/db/migrations/`, and `tests/integration/test_audit_provenance.py` proves the
    chain itself against that database. What only THIS test can see is the gap between
    the migration set in the repository and the one the running deployment actually has.
    """
    started = live_db.execute("SELECT now() AS t").fetchone()["t"]
    response = live_gateway.dicomweb(
        f"/studies/{live_study['study']}/series/{live_study['series']}"
        f"/instances/{live_study['sop']}",
        accept=MULTIPART,
    )
    assert response.status_code == 200

    row: dict[str, Any] | None = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and row is None:
        found = live_db.execute(
            "SELECT seq, prev_hash, hash FROM audit_events "
            " WHERE tenant_id = %s AND recorded_at >= %s AND action = 'phi.access' "
            " ORDER BY recorded_at DESC LIMIT 1",
            (LIVE_TENANT, started),
        ).fetchone()
        row = found
        if row is None:
            time.sleep(0.25)
    assert row is not None, "no PHI-access row was written at all"

    installed = live_db.execute(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'audit_events'::regclass "
        "AND NOT tgisinternal AND tgname = 'audit_events_chain_trg'"
    ).fetchall()
    applied = {
        r["version"]
        for r in live_db.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert installed, (
        "MOS-SEC-151's `audit_events_chain` trigger is not installed on this deployment, "
        "so every audit row keeps medos/medos/db/audit.py's placeholder seq and hash. "
        f"schema_migrations holds {sorted(applied)}; the repository ships "
        "0003_audit_provenance. Re-apply the migrations (or "
        "`docker compose -f medos/deploy/compose/docker-compose.yml down -v && up -d --build`)."
    )
    assert row["hash"] != "0" * 64, "the row kept the writer's placeholder hash"
    assert row["prev_hash"] != "0" * 64 or row["seq"] == 1


def test_no_patient_attribute_value_reaches_the_gateway_container_log_stream(
    live_gateway: LiveGateway, live_study: dict[str, str]
) -> None:
    """The deployed log stream carries no `PatientName` and no `PatientID`.

    The values are read out of the archive through the Gateway's own metadata route and
    are never printed by this test -- a test that proves PHI is absent from a log by
    echoing it into the test output has moved the leak, not closed it.

    UIDs are a SEPARATE and currently failing matter: chapter 3's acceptance check 14 puts
    `StudyInstanceUID` in the same zero-occurrence list, and this deployment's log stream
    does carry them. That is asserted by `test_no_dicom_uid_reaches_the_gateway_log_stream`
    above, which is an `xfail` naming the middleware responsible. This test is scoped to
    the attribute values so that a real regression in them is not hidden behind the known
    UID failure.
    """
    metadata = live_gateway.dicomweb(f"/studies/{live_study['study']}/metadata")
    assert metadata.status_code == 200
    document = metadata.json()[0]
    values: list[str] = []
    for tag in (TAG_PATIENT_NAME, TAG_PATIENT_ID, "00081030", "0008103E", "00080050"):
        for value in document.get(tag, {}).get("Value", []) or []:
            text = value.get("Alphabetic", "") if isinstance(value, dict) else str(value)
            if len(text) >= 4:
                values.append(text)
    if not values:
        skip_no_data(
            "the live study carries no PatientName, PatientID or description value, so "
            "there is nothing whose absence from the log stream could be proved",
            corpus="lctsc-corpus",
        )

    live_gateway.dicomweb(
        f"/studies/{live_study['study']}/series/{live_study['series']}"
        f"/instances/{live_study['sop']}",
        accept=MULTIPART,
    )
    code, logs = _docker_out("logs", "--tail", "800", GATEWAY_CONTAINER, timeout=90)
    if code != 0:
        skip_infra(f"docker logs {GATEWAY_CONTAINER}: {logs.strip()[:160]}",
                   dependency="medos-gateway")
    assert logs.strip(), "the Gateway logged nothing; this test would pass vacuously"

    leaked = [
        hashlib.sha256(v.encode()).hexdigest()[:8] for v in values if v in logs
    ]
    assert not leaked, (
        f"{len(leaked)} DICOM attribute value(s) reached the Gateway container log "
        f"stream (digests {leaked}; the values themselves are PHI and are not printed)"
    )


def test_the_gateway_metrics_carry_no_uid(live_gateway: LiveGateway) -> None:
    """MOS-DATA-026: "Label values MUST NOT contain a UID, a patient identifier or a
    description string (spine section 8)", and the five required families are present."""
    response = live_gateway.get("/metrics", accept="text/plain")
    assert response.status_code == 200
    body = response.text

    for family in (
        "medicalos_gateway_requests_total",
        "medicalos_gateway_deid_duration_seconds",
        "medicalos_gateway_deid_failures_total",
        "medicalos_gateway_uidmap_lookups_total",
        "medicalos_gateway_tenant_denials_total",
    ):
        assert family in body, f"MOS-DATA-026 family {family} is missing"

    labels = re.findall(r'\{([^}]*)\}', body)
    uid_shaped = re.compile(r"[0-9]+(?:\.[0-9]+){5,}")
    offenders = [chunk for chunk in labels if uid_shaped.search(chunk)]
    assert not offenders, f"a UID reached a metric label: {offenders[:2]}"


def test_no_gateway_response_body_names_the_pacs_origin(
    live_gateway: LiveGateway, live_study: dict[str, str]
) -> None:
    """No response the Gateway produces may name the PACS origin. CURRENTLY FAILS on STOW.

    THE INCONSISTENCY, NAMED: `_rewrite_bulkdata` scrubs the backend's origin out of
    WADO-RS metadata documents -- `test_wado_rs_metadata_points_bulkdata_at_the_gateway_
    and_not_at_the_pacs` above asserts that and it passes -- but `_stow` returns the
    backend's response body verbatim, so the `(0008,1190) RetrieveURL` it hands back reads
    `http://orthanc:8042/dicom-web/studies/...`.

    REPORTED HONESTLY: MOS-DATA-007 and MOS-DATA-012 do not spell out RetrieveURL
    rewriting, so this is NOT a verbatim MUST violation. What it is, is the same Gateway
    scrubbing an origin on one route and publishing it on another -- and the origin it
    publishes is the one MOS-DATA-006 has just made unreachable from every consumer, so
    the URL that comes back is simultaneously a disclosure of the PACS address and a dead
    link. `_rewrite_bulkdata`'s own docstring calls handing out that origin "the same
    failure in a different coat".

    The fix is one call: route the STOW-RS response body through `_rewrite_bulkdata` in
    `medos/medos/gateway/app.py::_stow`, exactly as the two metadata routes already do.
    """
    retrieved = live_gateway.dicomweb(
        f"/studies/{live_study['study']}/series/{live_study['series']}"
        f"/instances/{live_study['sop']}",
        accept=MULTIPART,
    )
    part = _first_dicom_part(retrieved.content, retrieved.headers["content-type"])
    boundary = "medosoriginboundary"
    body = (
        f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        + part
        + f"\r\n--{boundary}--\r\n".encode()
    )
    stow = live_gateway.session.post(
        f"{live_gateway.root}/studies",
        headers={
            "Authorization": f"Bearer {LIVE_WORKER_KEY}",
            "Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}',
            "Accept": DICOM_JSON,
        },
        data=body,
        timeout=180,
    )
    assert stow.status_code in (200, 202)
    assert "orthanc:8042" not in stow.text, (
        "the STOW-RS response hands the caller the PACS origin"
    )


# =====================================================================================
# 13. A guard on this file itself
# =====================================================================================
def test_this_module_declares_its_stack_dependencies() -> None:
    """`tests/_support/stack.py` must know this module needs the Gateway.

    Without the declaration, `pytest --require-stack` probes only Postgres for this file
    and every live test above degrades to an infra skip -- which strict mode then turns
    into a failure, but only after the run has already started. The declaration makes it a
    preflight abort with a message that names the container and the command that starts
    it, which is the whole point of that module.
    """
    declared = _stack.SUITE_DEPENDENCIES.get("tests/integration/test_gateway.py")
    assert declared is not None, (
        "add tests/integration/test_gateway.py to tests._support.stack.SUITE_DEPENDENCIES"
    )
    assert "medos-gateway" in declared
    assert "postgres" in declared
    assert "orthanc" not in declared, (
        "the host has no route to Orthanc any more (MOS-DATA-006); declaring it would "
        "make --require-stack fail on a correctly isolated deployment"
    )
