# SPDX-License-Identifier: Apache-2.0
"""Authentication: the four responses, and the tenant a caller cannot choose.

docs/spec/15-delivery.md section 15.2.4 item 2. Chapter 8 sections 8.2.1/8.2.2 are the
credential; chapter 10 `MOS-API-003`/`MOS-API-041` are the wire; `medos/medos/security/` is the
implementation.

The file is in three parts.

1. THE FOUR RESPONSES -- no key, bad key, expired key, valid key -- over a real uvicorn
   socket, because every claim here is a wire claim: a status code, a media type, a
   problem `code` and a header. An in-process ASGI shim would let a `TestClient` quirk
   pass for conformance.

2. THE TENANT A CALLER CANNOT CHOOSE. `MOS-API-003`: "The credential determines the
   `Tenant`; there is no tenant path segment and no tenant query parameter." Proved four
   ways -- header, query parameter, request body, and the database itself -- because
   three of those are refusals in code and only the fourth is a guarantee.

3. THE CREDENTIAL AT REST -- argon2id parameters, the 365-day ceiling, the scope grammar,
   immutability, rotation and revocation. These are `MOS-SEC-010` .. `MOS-SEC-015` and
   they are asserted against the row, not against the Python that wrote it.

Never printed anywhere in this file: a secret. Assertions are on `key_id` (`MOS-SEC-136`
permits logging it) and on booleans.
"""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from medos.api.app import create_app
from medos.api.auth import PUBLIC_PATHS
from medos.db.tenancy import DEFAULT_TENANT_ID, NoTenantContextError, tenant_tx
from medos.security import store
from medos.security.apikeys import CREDENTIAL_CACHE_TTL_S, ApiKeyAuthenticator
from medos.security.authn import (
    Credential,
    ExpiredCredential,
    MalformedCredential,
    Principal,
    RevokedCredential,
    UnknownCredential,
)
from medos.security.hashing import (
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    Argon2idHasher,
)
from medos.security.keyformat import ALPHABET, MalformedKeyError, mint, parse
from medos.security.scopes import PERMISSION_RE, ScopeError, validate_scope
from medos.security.store import MAX_LIFETIME_DAYS
from psycopg.rows import dict_row

pytestmark = pytest.mark.slow

# Tenant A is the tenant every weeks 1-2 row was backfilled onto; tenant B is created by
# the fixture below. Two real tenants, because "tenant A cannot reach tenant B" is not
# assertable against a deployment that has one.
TENANT_A = DEFAULT_TENANT_ID
TENANT_B = "22222222-2222-2222-2222-222222222222"

PRINCIPAL_A = "11111111-1111-1111-1111-111111111111"
PRINCIPAL_B = "33333333-3333-3333-3333-333333333333"

# The LCTSC study root, trimmed so a per-test suffix still fits DICOM's 64-character UI.
STUDY_ROOT = "1.3.6.1.4.1.14519.5.2.1.7014.4598.1069438908500116665"
PROBLEM_MEDIA_TYPE = "application/problem+json"

_STUDY_SEQ = itertools.count(1)


def fresh_study() -> str:
    """A study UID no previous job in this run used.

    `POST /api/v1/jobs` is idempotent on the derived job identity -- a repeat is a `200`
    with `MedicalOS-Idempotent-Replay: true`, not a second `202`. That is correct
    behaviour and `test_api.py` asserts it; here it would silently turn "the credential
    was accepted and a job was admitted" into "the credential was accepted and a previous
    job was handed back", so every submission gets its own study.
    """
    # <= 64 characters: `study_instance_uid` is a DICOM UI, and the schema's
    # `dicom_uid` domain is varchar(64). A counter on the last component keeps the
    # root and the length both legal.
    return f"{STUDY_ROOT}.{next(_STUDY_SEQ)}"


# =====================================================================================
# Harness
# =====================================================================================
def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def running_server(app: Any) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if time.monotonic() > deadline:  # pragma: no cover
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _opener(dsn: str):
    def open_conn(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(dsn, row_factory=dict_row, **kwargs)

    return open_conn


def app_role_dsn(dsn: str) -> str:
    """The same database as `medicalos_app`, the NOBYPASSRLS application role.

    Every app, authenticator and direct query in this file connects this way, and that is
    the whole point: the session fixture's DSN is the bootstrap superuser, and a superuser
    bypasses row-level security entirely, FORCE or no FORCE (MOS-SEC-073). A cross-tenant
    test written on the superuser connection would pass while proving nothing.
    """
    from tests.integration.conftest import APP_PASSWORD

    return dsn.replace("//medos:medos@", f"//medicalos_app:{APP_PASSWORD}@")


@pytest.fixture(scope="module")
def two_tenants(pg_dsn: str) -> str:
    """Ensure tenant B exists, and hand back the APPLICATION-ROLE dsn.

    The insert runs on the bootstrap connection, which bypasses RLS.

    That bypass is correct HERE and nowhere else: creating a tenant is by definition the
    one operation that cannot be performed inside a tenant context, and `tenants` carries
    `tenants_self` (`USING (id = current_tenant_id())`) under FORCE row security. In a
    real deployment this is `tenant.create`, a class-`admin` permission held only in the
    system tenant (chapter 8 section 8.3.2). There is no such surface in this block, so
    the fixture writes the row directly and says so.
    """
    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        conn.execute(
            "INSERT INTO tenants (id, slug, display_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (TENANT_B, "tenant-b", "Second tenant (test_auth.py)"),
        )
        conn.commit()
    return app_role_dsn(pg_dsn)


def issue_key(
    dsn: str,
    *,
    tenant_id: str = TENANT_A,
    principal_id: str = PRINCIPAL_A,
    kind: str = "service_account",
    lifetime: timedelta = timedelta(days=1),
    scope: tuple[str, ...] = ("job.create", "job.read"),
    label: str | None = None,
    allowlist: tuple[str, ...] = (),
    now: datetime | None = None,
) -> tuple[str, store.ApiKeyRecord]:
    """Mint a key straight through `store.issue`. Returns `(plaintext, record)`."""
    now = now or datetime.now(UTC)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        minted, record = store.issue(
            conn,
            tenant_id=tenant_id,
            principal_kind=kind,  # type: ignore[arg-type]
            principal_id=principal_id,
            created_by=store.BOOTSTRAP_OPERATOR_ID,
            expires_at=now + lifetime,
            scope=scope,
            source_ip_allowlist=allowlist,
            label=label,
            env="dev",
            now=now,
        )
        conn.commit()
    return minted.plaintext, record


@pytest.fixture(scope="module")
def api_url(two_tenants: str) -> Iterator[str]:
    """One app serving BOTH tenants, with the credential cache disabled.

    `cache_ttl_s=0` so that a revocation asserted three lines later is visible
    immediately. The cache is `MOS-SEC-015`'s <= 5 s allowance and is asserted
    separately, by its constant, rather than by sleeping for five seconds in a test.

    The tenant list is explicit rather than `serving_tenants()` because this process's
    `MEDOS_TENANTS` is not the test's business -- and because it makes the loop of
    `ApiKeyAuthenticator._find` the thing under test.
    """
    opener = _opener(two_tenants)
    authenticator = ApiKeyAuthenticator(
        opener, tenants=lambda: (TENANT_A, TENANT_B), env="dev", cache_ttl_s=0
    )
    app = create_app(connect=opener, configure_logs=False, authenticator=authenticator)
    with running_server(app) as url:
        yield url


@pytest.fixture(scope="module")
def key_a(two_tenants: str) -> tuple[str, store.ApiKeyRecord]:
    return issue_key(two_tenants, tenant_id=TENANT_A, principal_id=PRINCIPAL_A,
                     label="tenant A")


@pytest.fixture(scope="module")
def key_b(two_tenants: str) -> tuple[str, store.ApiKeyRecord]:
    return issue_key(two_tenants, tenant_id=TENANT_B, principal_id=PRINCIPAL_B,
                     label="tenant B")


def auth(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def assert_problem(response: httpx.Response, *, status: int, code: str) -> dict[str, Any]:
    """RFC 9457 shape (`MOS-API-035` .. `MOS-API-038`) plus the members this block adds."""
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    doc = response.json()
    assert doc["code"] == code, doc
    assert doc["status"] == status
    # MOS-API-041: authentication and authorisation refusals are `authz_error`.
    assert doc["class"] in ("authz_error", "transport_failure"), doc["class"]
    for member in ("type", "title", "detail", "instance", "retryable", "trace_id",
                   "occurred_at"):
        assert member in doc, member
    # MOS-API-004: the trace id is on the header of every response, error included.
    assert response.headers["MedicalOS-Trace-Id"] == doc["trace_id"]
    return doc


def submit(client: httpx.Client, **headers: str) -> httpx.Response:
    return client.post(
        "/api/v1/jobs",
        json={"study_instance_uid": fresh_study(),
              "capabilities": ["lung_segmentation"]},
        headers=headers or None,
    )


# =====================================================================================
# 1. THE FOUR RESPONSES
# =====================================================================================
def test_no_key_is_401_authentication_required(api_url: str) -> None:
    """MOS-SEC-008: "Anonymous access MUST NOT exist on any surface except `/healthz`,
    `/readyz` and the OpenAPI document." MOS-API-041: authentication failure is `401`.

    RFC 9110 section 11.6.1 also requires `WWW-Authenticate` on a `401`, and a client
    library that follows the RFC will not retry with a credential without it.
    """
    with httpx.Client(base_url=api_url, timeout=20) as c:
        response = submit(c)
    doc = assert_problem(response, status=401, code="AUTHENTICATION_REQUIRED")
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert doc["retryable"] is False  # table 10.4-A: re-authenticate or stop


def test_a_bad_key_is_401_invalid_credential_and_says_nothing_more(
    api_url: str, key_a: tuple[str, store.ApiKeyRecord]
) -> None:
    """A wrong secret under a REAL key_id and a wholly unknown key_id give the SAME
    answer. Distinguishing them turns the `401` into an oracle for "which half of the
    credential I guessed was right".

    MOS-SEC-136 is asserted here too: no part of the presented credential appears in the
    problem document.
    """
    _plaintext, record = key_a
    wrong_secret = "mos_dev_" + record.key_id + "_" + ("A" * 52)
    unknown_key = "mos_dev_" + "Z" * 12 + "_" + ("B" * 52)
    not_a_key = "hunter2"

    with httpx.Client(base_url=api_url, timeout=20) as c:
        bad = assert_problem(submit(c, **auth(wrong_secret)), status=401,
                             code="INVALID_CREDENTIAL")
        unknown = assert_problem(submit(c, **auth(unknown_key)), status=401,
                                 code="INVALID_CREDENTIAL")
        garbage = assert_problem(submit(c, **auth(not_a_key)), status=401,
                                 code="INVALID_CREDENTIAL")

    assert bad["detail"] == unknown["detail"] == garbage["detail"]
    assert bad["title"] == unknown["title"]
    for doc in (bad, unknown, garbage):
        body = json.dumps(doc)
        assert record.key_id not in body
        assert "A" * 52 not in body and "hunter2" not in body


def test_an_expired_key_is_401_credential_expired(two_tenants: str, api_url: str) -> None:
    """MOS-SEC-012. A DISTINCT code from `INVALID_CREDENTIAL`, and that is safe: it is
    reachable only after the secret has verified, so only the legitimate holder ever
    sees it -- and to them "your key expired, rotate it" is the whole answer.

    The key is minted AS OF two days ago with a one-day lifetime -- a key issued on
    Monday that died on Tuesday, which is the real shape of the thing. It is deliberately
    NOT aged by UPDATEing `expires_at` into the past: `api_keys_has_lifetime`
    (`expires_at > created_at`) refuses that, and that refusal is worth knowing about --
    a row cannot be edited into a state it could never have been issued in.
    """
    two_days_ago = datetime.now(UTC) - timedelta(days=2)
    plaintext, _record = issue_key(
        two_tenants, lifetime=timedelta(days=1), now=two_days_ago,
        label="issued Monday, died Tuesday",
    )

    with httpx.Client(base_url=api_url, timeout=20) as c:
        response = submit(c, **auth(plaintext))
    doc = assert_problem(response, status=401, code="CREDENTIAL_EXPIRED")
    assert doc["retryable"] is False


def test_a_valid_key_is_accepted_and_the_job_belongs_to_its_tenant(
    api_url: str, key_a: tuple[str, store.ApiKeyRecord], two_tenants: str
) -> None:
    """The fourth response. `202` and a job row owned by the CREDENTIAL's tenant.

    The second half is the point: nothing in the request named a tenant, and the row
    landed in tenant A because `api_keys.tenant_id` said so (`MOS-API-003`).
    """
    plaintext, record = key_a
    with httpx.Client(base_url=api_url, timeout=30) as c:
        response = submit(c, **auth(plaintext))
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        with tenant_tx(conn, tenant_id=TENANT_A) as tx:
            row = tx.execute(
                "SELECT tenant_id FROM jobs WHERE public_id = %s", (job_id,)
            ).fetchone()
    assert row is not None and str(row["tenant_id"]) == TENANT_A
    assert record.tenant_id == TENANT_A


def test_a_revoked_key_is_401_credential_revoked(
    two_tenants: str, api_url: str
) -> None:
    """MOS-SEC-015, and the ordering rule: a key that is BOTH revoked and expired reads
    as revoked, because "somebody killed this" and "mint another the same way" are
    different instructions."""
    plaintext, record = issue_key(two_tenants, label="to be revoked")
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        store.revoke(conn, TENANT_A, record.key_id, reason="test")
        conn.commit()

    with httpx.Client(base_url=api_url, timeout=20) as c:
        response = submit(c, **auth(plaintext))
    assert_problem(response, status=401, code="CREDENTIAL_REVOKED")


def test_the_public_paths_are_exactly_three_and_need_no_credential(
    api_url: str
) -> None:
    """MOS-SEC-008's closed list. `/docs` and `/redoc` are the Swagger/ReDoc shells for
    the same document -- they carry no API data and fetch `/openapi.json` in the
    browser -- so the served anonymous surface is the OpenAPI document and the two
    probes, and nothing else."""
    with httpx.Client(base_url=api_url, timeout=20) as c:
        assert c.get("/healthz").status_code == 200
        assert c.get("/readyz").status_code == 200
        assert c.get("/openapi.json").status_code == 200
        # Anything else, with no credential, is a 401.
        assert c.get("/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ").status_code == 401
    assert PUBLIC_PATHS == frozenset(
        {"/healthz", "/readyz", "/openapi.json", "/docs", "/docs/oauth2-redirect",
         "/redoc"}
    )


def test_a_credential_store_outage_is_503_transport_failure_not_401(
    two_tenants: str
) -> None:
    """A `401` during a database outage would have every integration in the hospital
    rotate a working key. `AuthenticatorUnavailable` is `503` / `transport_failure` /
    retryable -- the credential is not the problem, so table 10.4-A's "re-authenticate
    or stop" is the wrong instruction."""

    def refuse(**kwargs: Any) -> psycopg.Connection[Any]:
        raise psycopg.OperationalError("connection refused")

    authenticator = ApiKeyAuthenticator(
        refuse, tenants=lambda: (TENANT_A,), env="dev", cache_ttl_s=0
    )
    app = create_app(connect=refuse, configure_logs=False, authenticator=authenticator)
    with running_server(app) as url, httpx.Client(base_url=url, timeout=20) as c:
        response = submit(c, **auth("mos_dev_" + "Z" * 12 + "_" + "B" * 52))
    doc = assert_problem(response, status=503, code="AUTHENTICATOR_UNAVAILABLE")
    assert doc["class"] == "transport_failure"
    assert doc["retryable"] is True


# =====================================================================================
# 2. THE TENANT A CALLER CANNOT CHOOSE
# =====================================================================================
def test_tenant_a_cannot_read_tenant_b_by_any_request_controlled_field(
    api_url: str,
    key_a: tuple[str, store.ApiKeyRecord],
    key_b: tuple[str, store.ApiKeyRecord],
) -> None:
    """The load-bearing test. `MOS-API-003` / `MOS-API-041`.

    B creates a job. A then tries to reach it FOUR ways, each of which is the place a
    tenant would be smuggled in if the platform let it be:

      header      `MedicalOS-Tenant-Id: <B>`  -> 403 CROSS_TENANT_DENIED
      query       `?tenant_id=<B>`            -> 403 CROSS_TENANT_DENIED
      body        `{"tenant_id": "<B>"}`      -> 422, the field does not exist
      plain GET   the job id, no tenant named -> 404, the row is not visible

    The `404` is the one that matters, and it is not this middleware's doing: A's request
    is bound to tenant A, the row is tenant B's, and `jobs_tenant_isolation` under FORCE
    row security means the SELECT returns nothing. The three refusals above it are belt;
    the `404` is braces.

    `MOS-API-041` also requires that a cross-tenant reference "MUST NOT leak whether the
    referenced id exists", which is why A gets the same `404` it would get for a job id
    that was never issued.
    """
    plaintext_a, _ = key_a
    plaintext_b, _ = key_b

    with httpx.Client(base_url=api_url, timeout=30) as c:
        created = submit(c, **auth(plaintext_b))
        assert created.status_code == 202, created.text
        b_job = created.json()["job_id"]

        # B can read its own job.
        mine = c.get(f"/api/v1/jobs/{b_job}", headers=auth(plaintext_b))
        assert mine.status_code == 200, mine.text

        # 1. The header MOS-API-003 reserves for a platform administrator.
        via_header = c.get(
            f"/api/v1/jobs/{b_job}",
            headers={**auth(plaintext_a), "MedicalOS-Tenant-Id": TENANT_B},
        )
        doc = assert_problem(via_header, status=403, code="CROSS_TENANT_DENIED")
        assert TENANT_B not in json.dumps(doc), "the refusal must not echo the tenant"

        # 2. A query parameter. "there is no tenant query parameter".
        via_query = c.get(
            f"/api/v1/jobs/{b_job}?tenant_id={TENANT_B}", headers=auth(plaintext_a)
        )
        assert_problem(via_query, status=403, code="CROSS_TENANT_DENIED")

        # 3. A body field. MOS-API-008's additionalProperties:false -- the field is not
        #    part of the schema, so it is a 400 SCHEMA_VIOLATION with a JSON pointer at
        #    it. "Silently ignored" would be the dangerous outcome: the caller would
        #    believe it had selected a tenant and be handed tenant A's answer.
        via_body = c.post(
            "/api/v1/jobs",
            json={"study_instance_uid": fresh_study(),
                  "capabilities": ["lung_segmentation"], "tenant_id": TENANT_B},
            headers=auth(plaintext_a),
        )
        assert via_body.status_code == 400, via_body.text
        assert via_body.json()["violations"] == [
            {"pointer": "/tenant_id", "code": "EXTRA_FORBIDDEN",
             "detail": "Extra inputs are not permitted"}
        ]

        # 4. No tenant named at all. The database answers.
        blind = c.get(f"/api/v1/jobs/{b_job}", headers=auth(plaintext_a))
        assert blind.status_code == 404, blind.text
        never_existed = c.get(
            "/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ", headers=auth(plaintext_a)
        )
        assert never_existed.status_code == 404
        assert blind.json()["code"] == never_existed.json()["code"]


def test_the_tenant_selecting_header_is_403_even_for_the_owning_tenant(
    api_url: str, key_b: tuple[str, store.ApiKeyRecord]
) -> None:
    """B naming its OWN tenant is still `403`. The rule is not "do not cross a boundary",
    it is "the credential determines the tenant" -- a request that names the right tenant
    is a request that believes naming one works, and the next one names a different
    one."""
    plaintext_b, _ = key_b
    with httpx.Client(base_url=api_url, timeout=20) as c:
        response = c.get(
            "/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ",
            headers={**auth(plaintext_b), "MedicalOS-Tenant-Id": TENANT_B},
        )
    assert_problem(response, status=403, code="CROSS_TENANT_DENIED")


def test_an_unauthenticated_request_naming_a_tenant_is_401_not_403(
    api_url: str
) -> None:
    """Ordering. `MOS-API-003` refuses the header "for every other principal", so the
    principal must exist first. A `403` here would confirm to an anonymous caller that
    the header is a real feature."""
    with httpx.Client(base_url=api_url, timeout=20) as c:
        response = c.get(
            "/api/v1/jobs/job_01J9F4N7T3R5V7X9Z1B3D5FGHJ",
            headers={"MedicalOS-Tenant-Id": TENANT_B},
        )
    assert_problem(response, status=401, code="AUTHENTICATION_REQUIRED")


def test_a_key_resolves_to_exactly_one_tenant_at_the_database(
    two_tenants: str, key_a: tuple[str, store.ApiKeyRecord]
) -> None:
    """Below the HTTP layer: A's `key_id` is not visible from tenant B's context, and is
    not visible at all without one.

    This is `MOS-SEC-072` / `MOS-STORE-224` applied to the credential directory, run on a
    connection that is NOT the bootstrap superuser -- a superuser bypasses row security
    entirely, so a proof written on the fixture connection would prove nothing.
    """
    _plaintext, record = key_a
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        with tenant_tx(conn, tenant_id=TENANT_A) as tx:
            found = tx.execute(
                "SELECT key_id FROM api_keys WHERE key_id = %s", (record.key_id,)
            ).fetchall()
        assert len(found) == 1

        with tenant_tx(conn, tenant_id=TENANT_B) as tx:
            hidden = tx.execute(
                "SELECT key_id FROM api_keys WHERE key_id = %s", (record.key_id,)
            ).fetchall()
        assert hidden == [], "tenant B saw tenant A's credential row"

        # And with no tenant bound at all: not "everything", not "nothing" -- an error.
        with pytest.raises(psycopg.errors.UndefinedObject):
            with conn.transaction():
                conn.execute("SELECT key_id FROM api_keys").fetchall()


def test_the_chokepoint_refuses_a_second_tenant_inside_one_transaction(
    two_tenants: str
) -> None:
    """The third layer of the tenant guard, for completeness: even if a request-controlled
    tenant reached repository code, `tenant_tx()` refuses to re-bind mid-transaction
    (`TenantContextConflict`) and `NoTenantContextError` covers the unbound case. Both
    are `medos/medos/db/tenancy.py`'s and both are what makes the `404` above structural."""
    from medos.db.tenancy import TenantContextConflict

    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        with pytest.raises(TenantContextConflict):
            with tenant_tx(conn, tenant_id=TENANT_A):
                with tenant_tx(conn, tenant_id=TENANT_B):
                    pass
        conn.rollback()
    with pytest.raises(NoTenantContextError):
        validate_tenant = store.lookup  # noqa: F841 - named for the traceback
        with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
            with tenant_tx(conn):
                pass


def test_a_key_for_an_unserved_tenant_does_not_authenticate(
    two_tenants: str, key_b: tuple[str, store.ApiKeyRecord]
) -> None:
    """The honest cost of the resolution loop, asserted rather than left implicit.

    `ApiKeyAuthenticator` probes the tenants the deployment SERVES. A key belonging to a
    tenant this process does not serve is indistinguishable from an unknown key -- which
    is correct (the process has no business authenticating for a tenant it does not
    serve) and is the behaviour an operator must know about when adding a tenant to a
    running fleet: `MEDOS_TENANTS` is part of the deployment, not decoration.
    """
    plaintext_b, _ = key_b
    only_a = ApiKeyAuthenticator(
        _opener(two_tenants), tenants=lambda: (TENANT_A,), env="dev", cache_ttl_s=0
    )
    with pytest.raises(UnknownCredential):
        only_a.authenticate(Credential(scheme="Bearer", secret=plaintext_b))


# =====================================================================================
# 3. THE CREDENTIAL AT REST
# =====================================================================================
def test_the_secret_is_argon2id_at_the_required_parameters_and_nothing_else(
    two_tenants: str
) -> None:
    """MOS-SEC-010: `argon2id(secret, salt, m=64MiB, t=3, p=1)`. MOS-STORE-232: the
    plaintext "MUST NOT be recoverable, and the schema provides no column that could hold
    it".

    Asserted against the stored bytes and against the whole row -- the plaintext, its
    secret half and its key_id-prefixed form must appear in NO column of the row that
    describes it.
    """
    plaintext, record = issue_key(two_tenants, label="hash shape")
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        with tenant_tx(conn, tenant_id=TENANT_A) as tx:
            row = tx.execute(
                "SELECT * FROM api_keys WHERE key_id = %s", (record.key_id,)
            ).fetchone()
    assert row is not None
    phc = bytes(row["secret_hash"]).decode("ascii")
    assert phc.startswith("$argon2id$")
    assert f"m={ARGON2_MEMORY_COST_KIB}" in phc
    assert f"t={ARGON2_TIME_COST}" in phc
    assert f"p={ARGON2_PARALLELISM}" in phc
    assert ARGON2_MEMORY_COST_KIB == 64 * 1024  # 64 MiB, verbatim

    secret_half = plaintext.rsplit("_", 1)[1]
    serialised = json.dumps({k: str(v) for k, v in row.items()})
    assert plaintext not in serialised
    assert secret_half not in serialised

    # And the hash is not a lookup key: nothing indexes it (MOS-SEC-011).
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        indexed = conn.execute(
            "SELECT count(*) AS n FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
            "WHERE i.indrelid = 'api_keys'::regclass AND a.attname = 'secret_hash'"
        ).fetchone()
    assert indexed is not None and indexed["n"] == 0


def test_the_hasher_verifies_only_the_secret_it_hashed() -> None:
    """The driver, in isolation: a mismatch is `False`, never an exception, so no caller
    is tempted to wrap it in `except: pass` and swallow a corrupt stored hash."""
    hasher = Argon2idHasher()
    stored = hasher.hash("correct horse")
    assert hasher.verify(stored, "correct horse") is True
    assert hasher.verify(stored, "correct horsE") is False
    assert hasher.verify(stored, "") is False
    assert hasher.needs_rehash(stored) is False


def test_a_key_without_an_expiry_or_beyond_365_days_is_refused(
    two_tenants: str
) -> None:
    """MOS-SEC-012, at both layers.

    Python refuses with a sentence an operator can act on; the schema refuses even when
    Python is bypassed entirely, which is the version that survives the next refactor.
    A key with NO expiry is not refused at all -- it is unrepresentable, because the
    column is `NOT NULL`.
    """
    now = datetime.now(UTC)
    with pytest.raises(store.KeyLifetimeError):
        issue_key(two_tenants, lifetime=timedelta(days=MAX_LIFETIME_DAYS + 1))
    with pytest.raises(store.KeyLifetimeError):
        issue_key(two_tenants, lifetime=timedelta(seconds=-1))

    # Bypass the Python check: write the row directly and let the constraints answer.
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        for expires_at, constraint in (
            (now + timedelta(days=400), "api_keys_max_lifetime"),
            (now - timedelta(days=1), "api_keys_has_lifetime"),
        ):
            with pytest.raises(psycopg.errors.CheckViolation) as excinfo:
                with tenant_tx(conn, tenant_id=TENANT_A) as tx:
                    tx.execute(
                        "INSERT INTO api_keys (tenant_id, principal_kind, "
                        "service_account_id, key_id, secret_hash, expires_at, "
                        "created_by, created_at) VALUES (%s,'service_account',%s,%s,"
                        "%s,%s,%s,%s)",
                        (TENANT_A, PRINCIPAL_A, "ABCDEFGHJKMN", b"x" * 64,
                         expires_at, store.BOOTSTRAP_OPERATOR_ID, now),
                    )
            assert excinfo.value.diag.constraint_name == constraint
            conn.rollback()

        # NOT NULL, so "no expiry" cannot be expressed.
        with pytest.raises(psycopg.errors.NotNullViolation):
            with tenant_tx(conn, tenant_id=TENANT_A) as tx:
                tx.execute(
                    "INSERT INTO api_keys (tenant_id, principal_kind, "
                    "service_account_id, key_id, secret_hash, created_by) "
                    "VALUES (%s,'service_account',%s,%s,%s,%s)",
                    (TENANT_A, PRINCIPAL_A, "ABCDEFGHJKMP", b"x" * 64,
                     store.BOOTSTRAP_OPERATOR_ID),
                )
        conn.rollback()


def test_a_wildcard_or_four_segment_scope_is_unrepresentable(two_tenants: str) -> None:
    """MOS-SEC-031 and MOS-SEC-033, in Python AND in the schema -- and the two agree.

    The regex exists twice: `medos.security.scopes.PERMISSION_RE` and the one inside
    `scope_is_wellformed()` in `0004_auth.up.sql`. Two copies of a regex is exactly the
    drift CONTRACT.md section 2 was written about, so the table below is evaluated by
    both and the answers are compared.
    """
    cases = {
        "study.read": True,
        "result.review.read": True,
        "api_key.create": True,
        "*": False,
        "study.*": False,
        "study.read.*": False,
        "a.b.c.d": False,
        "Study.read": False,
        "study": False,
        "study.": False,
    }
    for entry, ok in cases.items():
        assert bool(PERMISSION_RE.match(entry)) is ok, entry

    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        for entry, ok in cases.items():
            row = conn.execute(
                "SELECT scope_is_wellformed(ARRAY[%s]) AS ok", (entry,)
            ).fetchone()
            assert row is not None and row["ok"] is ok, (
                f"{entry!r}: the SQL grammar and PERMISSION_RE disagree"
            )

    with pytest.raises(ScopeError):
        validate_scope(["study.*"])
    with pytest.raises(ScopeError):
        validate_scope(["a.b.c.d"])
    assert validate_scope(["job.read", "job.create", "job.read"]) == (
        "job.create", "job.read"
    )


def test_a_stored_key_is_immutable_except_for_four_columns(two_tenants: str) -> None:
    """A credential whose identity, owner, secret or scope can be UPDATEd in place is a
    mutable grant with an audit trail that lies.

    And `expires_at` may only move EARLIER (`api_keys_expiry_only_shortens`), because
    MOS-SEC-012's ceiling is measured from `created_at`: without that trigger, pushing
    the expiry out by 364 days a year keeps a key alive forever while every constraint
    on the row stays satisfied.
    """
    # `forbid_column_change()` raises SQLSTATE MOS06 -- a project-defined class, so
    # psycopg has no named exception for it and `errors.lookup` does not know it. The
    # sqlstate IS the contract; the exception class is whatever psycopg synthesises.
    sealed = psycopg.Error
    _plaintext, record = issue_key(two_tenants, label="immutable")
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        for column, value in (
            ("secret_hash", b"y" * 64),
            ("key_id", "ZZZZZZZZZZZZ"),
            ("tenant_id", TENANT_B),
            ("scope", ["study.read"]),
            ("service_account_id", PRINCIPAL_B),
        ):
            with pytest.raises(sealed) as excinfo:
                with tenant_tx(conn, tenant_id=TENANT_A) as tx:
                    tx.execute(
                        f"UPDATE api_keys SET {column} = %s WHERE key_id = %s",
                        (value, record.key_id),
                    )
            assert excinfo.value.sqlstate == "MOS06", column
            assert "sealed after insert" in str(excinfo.value)
            conn.rollback()

        with pytest.raises(psycopg.errors.CheckViolation):
            with tenant_tx(conn, tenant_id=TENANT_A) as tx:
                tx.execute(
                    "UPDATE api_keys SET expires_at = expires_at + interval '10 days' "
                    "WHERE key_id = %s",
                    (record.key_id,),
                )
        conn.rollback()

        # Shortening is allowed -- that is how expiry and the rotation grace work.
        with tenant_tx(conn, tenant_id=TENANT_A) as tx:
            tx.execute(
                "UPDATE api_keys SET expires_at = expires_at - interval '1 hour' "
                "WHERE key_id = %s",
                (record.key_id,),
            )
        conn.commit()


def test_a_key_is_revoked_and_never_deleted(two_tenants: str) -> None:
    """MOS-SEC-015 revokes; chapter 12 MOS-STORE-343 says erasing a user revokes its
    api_keys rather than dropping the rows. "Which credential did this" must stay
    answerable after the credential is dead, so `medicalos_app` holds no DELETE."""
    _plaintext, record = issue_key(two_tenants, label="revoke not delete")
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with tenant_tx(conn, tenant_id=TENANT_A) as tx:
                tx.execute("DELETE FROM api_keys WHERE key_id = %s", (record.key_id,))
        conn.rollback()

        revoked = store.revoke(conn, TENANT_A, record.key_id, reason="lost laptop")
        conn.commit()
    assert revoked is not None and revoked.is_revoked
    assert revoked.revoked_reason == "lost laptop"

    # Idempotent: the first timestamp and reason survive a second revoke.
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        again = store.revoke(conn, TENANT_A, record.key_id, reason="second thoughts")
        conn.commit()
    assert again is not None and again.revoked_at == revoked.revoked_at
    assert again.revoked_reason == "lost laptop"


def test_rotation_mints_a_successor_and_retires_the_original(
    two_tenants: str, api_url: str
) -> None:
    """Rotation is a NEW ROW, not an UPDATE. With no grace the predecessor dies at once;
    both writes are in one transaction, so "two live credentials where the operator asked
    for one" is not a reachable state."""
    old_plaintext, old = issue_key(two_tenants, label="rotate me",
                                   scope=("job.create", "job.read"))
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        minted, successor, retired = store.rotate(
            conn, TENANT_A, old.key_id, reason="scheduled"
        )
        conn.commit()

    assert successor.key_id != old.key_id
    assert successor.rotated_from == old.id
    assert successor.scope == old.scope            # the ceiling is inherited
    assert successor.principal_id == old.principal_id
    assert retired.revoked_at is not None
    assert retired.revoked_reason == "scheduled"

    with httpx.Client(base_url=api_url, timeout=20) as c:
        assert_problem(submit(c, **auth(old_plaintext)), status=401,
                       code="CREDENTIAL_REVOKED")
        assert submit(c, **auth(minted.plaintext)).status_code == 202


def test_rotation_with_a_grace_window_keeps_the_old_key_working(
    two_tenants: str, api_url: str
) -> None:
    """The overlap MOS-SEC-137 gives the Job Token signing key, applied to API keys: a
    fleet is re-pointed without a coordinated restart. The grace may never push the old
    key past its own `expires_at` -- `api_keys_expiry_only_shortens` enforces that, and
    `store.rotate` does not try."""
    old_plaintext, old = issue_key(two_tenants, label="rotate with grace",
                                   lifetime=timedelta(days=2))
    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        minted, _successor, retired = store.rotate(
            conn, TENANT_A, old.key_id, grace=timedelta(hours=6)
        )
        conn.commit()

    assert retired.revoked_at is None
    assert retired.expires_at < old.expires_at
    with httpx.Client(base_url=api_url, timeout=30) as c:
        assert submit(c, **auth(old_plaintext)).status_code == 202
        assert submit(c, **auth(minted.plaintext)).status_code == 202


def test_last_used_at_is_stamped_on_use(two_tenants: str, api_url: str) -> None:
    """MOS-SEC-011's `last_used_at`. Not per request -- once per credential-cache miss,
    which is the resolution the column is actually read at ("has this integration gone
    quiet?"). The module fixture disables the cache, so one request is one stamp."""
    plaintext, record = issue_key(two_tenants, label="last used")
    assert record.last_used_at is None
    with httpx.Client(base_url=api_url, timeout=30) as c:
        assert submit(c, **auth(plaintext)).status_code == 202

    with psycopg.connect(two_tenants, row_factory=dict_row) as conn:
        rows = store.list_keys(conn, TENANT_A, include_dead=True)
    stamped = [r for r in rows if r.key_id == record.key_id]
    assert stamped and stamped[0].last_used_at is not None


def test_the_credential_cache_cannot_outlive_five_seconds(two_tenants: str) -> None:
    """MOS-SEC-015: "Implementations MUST NOT cache key validity longer than 5 s."

    Asserted by the constant and by the CLAMP, not by sleeping: a test that sleeps for
    five seconds to prove a five-second bound is a test that will be deleted the first
    time someone looks at the suite's runtime.
    """
    assert CREDENTIAL_CACHE_TTL_S == 5.0
    greedy = ApiKeyAuthenticator(
        _opener(two_tenants), tenants=lambda: (TENANT_A,), cache_ttl_s=3600
    )
    assert greedy._ttl == CREDENTIAL_CACHE_TTL_S  # clamped, not honoured

    # A cached principal is NOT handed to a caller who presents the right key_id with the
    # wrong secret: the cache is keyed on the public half, so it re-checks the secret.
    plaintext, record = issue_key(two_tenants, label="cache")
    cached = ApiKeyAuthenticator(
        _opener(two_tenants), tenants=lambda: (TENANT_A,), env="dev", cache_ttl_s=5
    )
    first = cached.authenticate(Credential(scheme="Bearer", secret=plaintext))
    second = cached.authenticate(Credential(scheme="Bearer", secret=plaintext))
    assert first.credential_key_id == second.credential_key_id == record.key_id
    with pytest.raises(UnknownCredential):
        cached.authenticate(
            Credential(scheme="Bearer",
                       secret="mos_dev_" + record.key_id + "_" + "A" * 52)
        )


def test_a_source_ip_allowlist_is_403_and_fails_closed(two_tenants: str) -> None:
    """MOS-SEC-011's `source_ip_allowlist`. A `403`, not a `401`: the credential IS
    valid and re-authenticating will not help (`MOS-API-041`'s distinction).

    Fails closed when the peer address is unknown -- "we could not tell where this came
    from" is not "it came from an allowed place".
    """
    from medos.security.authn import SourceNotAllowed

    plaintext, _record = issue_key(two_tenants, label="allowlisted",
                                   allowlist=("10.0.0.0/8",))
    authenticator = ApiKeyAuthenticator(
        _opener(two_tenants), tenants=lambda: (TENANT_A,), env="dev", cache_ttl_s=0
    )
    ok = authenticator.authenticate(
        Credential(scheme="Bearer", secret=plaintext, source_ip="10.1.2.3")
    )
    assert ok.tenant_id == TENANT_A
    for bad_source in ("127.0.0.1", None, "not-an-ip"):
        with pytest.raises(SourceNotAllowed):
            authenticator.authenticate(
                Credential(scheme="Bearer", secret=plaintext, source_ip=bad_source)
            )


# =====================================================================================
# The port, the format, and the things that must never be logged
# =====================================================================================
def test_a_second_driver_needs_no_change_to_any_pep(two_tenants: str) -> None:
    """MOS-SEC-009: "Adding OIDC in 0.3 MUST NOT require a change to any PEP."

    The testable form: an `Authenticator` that is not the API-key driver -- no database,
    no argon2, no `mos_` prefix -- drives the same middleware, the same routes and the
    same tenant binding, injected through the one constructor argument. If adding a
    driver ever needs a route, a handler or `medos/medos/api/auth.py` to change, this test is
    where it is noticed.
    """

    class TokenAuthenticator:
        def authenticate(self, credential: Credential) -> Principal:
            if credential.secret != "opaque-token-from-an-idp":
                raise MalformedCredential()
            return Principal(
                kind="user",
                principal_id=PRINCIPAL_A,
                tenant_id=TENANT_A,
                scope=("job.create", "job.read"),
                credential_key_id="OIDC00000000",
            )

    opener = _opener(two_tenants)
    app = create_app(connect=opener, configure_logs=False,
                     authenticator=TokenAuthenticator())
    with running_server(app) as url, httpx.Client(base_url=url, timeout=30) as c:
        assert submit(c, **auth("opaque-token-from-an-idp")).status_code == 202
        assert_problem(submit(c, **auth("wrong")), status=401,
                       code="INVALID_CREDENTIAL")
        assert_problem(submit(c), status=401, code="AUTHENTICATION_REQUIRED")


def test_the_key_format_is_chapter_8_section_8_2_2() -> None:
    """`mos_<env>_<12>_<52>`, Crockford base32.

    The alphabet excludes I, L, O and U so that a key read over a phone or copied from a
    screenshot cannot become a different VALID key through a 1/l or 0/O confusion -- and
    `parse()` deliberately does NOT apply Crockford's decode-time folding, because two
    spellings hashing to two values while naming one key is a credential that works on
    Tuesday and not on Wednesday.
    """
    assert set("ILOU").isdisjoint(ALPHABET)
    assert len(ALPHABET) == 32

    minted = mint("dev")
    assert minted.plaintext.startswith("mos_dev_")
    parsed = parse(minted.plaintext)
    assert parsed.key_id == minted.key_id and parsed.env == "dev"
    assert len(minted.key_id) == 12
    assert len(minted.plaintext.rsplit("_", 1)[1]) == 52

    # Whitespace from a header is tolerated; folding is not.
    assert parse(f"  {minted.plaintext}\r\n").key_id == minted.key_id
    with pytest.raises(MalformedKeyError):
        parse(minted.plaintext.lower())
    for junk in ("", "mos_prod_short_x", "bearer " + minted.plaintext,
                 minted.plaintext + "X"):
        with pytest.raises(MalformedKeyError):
            parse(junk)

    # MOS-SEC-136: the secret is not in a repr, a str or an equality comparison.
    assert minted.secret not in repr(minted)
    assert minted.secret not in str(minted)
    assert minted.secret not in repr(parsed)
    assert minted.secret not in repr(
        Credential(scheme="Bearer", secret=minted.plaintext)
    )


def test_a_refusal_never_carries_a_secret_and_the_principal_log_fields_are_ids_only(
    api_url: str, two_tenants: str
) -> None:
    """MOS-SEC-136 and CONTRACT.md section 11, on the two surfaces that leak: the problem
    document, and the structured log line.

    `Principal.log_fields()` is the allowlist -- ids and the PUBLIC key id, which
    MOS-SEC-136 explicitly permits ("A secret's `key_id` MAY be logged"). No display
    name, no email, no patient attribute, and nothing derived from the secret.
    """
    plaintext, record = issue_key(two_tenants, label="log fields")
    with httpx.Client(base_url=api_url, timeout=20) as c:
        refused = submit(c, **auth(plaintext[:-1] + ("Z" if plaintext[-1] != "Z" else "Y")))
    body = refused.text
    assert plaintext not in body
    assert plaintext.rsplit("_", 1)[1] not in body

    principal = Principal(
        kind="service_account", principal_id=PRINCIPAL_A, tenant_id=TENANT_A,
        scope=("job.read",), credential_key_id=record.key_id,
    )
    fields = principal.log_fields()
    assert set(fields) == {"principal_kind", "principal_id", "tenant_id", "api_key_id"}
    assert fields["api_key_id"] == record.key_id


def test_the_authenticator_refuses_a_key_minted_for_another_environment(
    two_tenants: str
) -> None:
    """A staging key presented to production. Refused before the database is touched,
    with a message that names the problem -- finding out from a generic "invalid
    credential" that the environment prefix was wrong costs an afternoon."""
    plaintext, _record = issue_key(two_tenants, label="env check")
    prod_only = ApiKeyAuthenticator(
        _opener(two_tenants), tenants=lambda: (TENANT_A,), env="prod", cache_ttl_s=0
    )
    with pytest.raises(MalformedCredential) as excinfo:
        prod_only.authenticate(Credential(scheme="Bearer", secret=plaintext))
    assert "environment" in str(excinfo.value)


def test_a_non_bearer_scheme_is_refused(two_tenants: str) -> None:
    """MOS-API-003: "Every request MUST carry `Authorization: Bearer <credential>`."
    `Basic` is refused rather than sniffed, because a platform that accepts a credential
    under the wrong scheme teaches integrators that the scheme is decoration."""
    plaintext, _record = issue_key(two_tenants, label="scheme")
    authenticator = ApiKeyAuthenticator(
        _opener(two_tenants), tenants=lambda: (TENANT_A,), env="dev", cache_ttl_s=0
    )
    with pytest.raises(MalformedCredential):
        authenticator.authenticate(Credential(scheme="Basic", secret=plaintext))
    # ...and the same credential under Bearer still works, so the refusal is the scheme.
    assert authenticator.authenticate(
        Credential(scheme="bearer", secret=plaintext)
    ).tenant_id == TENANT_A


def test_the_refusal_hierarchy_maps_to_the_documented_statuses() -> None:
    """The taxonomy, as a table, so a new refusal class cannot be added with a status
    that contradicts MOS-API-041 without this failing."""
    from medos.security.authn import (
        AuthenticatorUnavailable,
        CrossTenantDenied,
        MissingCredential,
        SourceNotAllowed,
    )

    assert MissingCredential().status == 401
    assert MalformedCredential().status == 401
    assert UnknownCredential().status == 401
    assert ExpiredCredential().status == 401
    assert RevokedCredential().status == 401
    assert SourceNotAllowed().status == 403
    assert CrossTenantDenied().status == 403
    assert AuthenticatorUnavailable().status == 503
    # An unknown key and a wrong secret share one code, on purpose.
    assert UnknownCredential().code == MalformedCredential().code == "INVALID_CREDENTIAL"
    assert CrossTenantDenied().code == "CROSS_TENANT_DENIED"  # MOS-API-041, by name
