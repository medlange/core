# SPDX-License-Identifier: Apache-2.0
"""The corpus-assembly surface over real HTTP. Table 10.2-B rows `R18`-`R31`, 32 and 37.

WHAT THIS FILE IS TRYING TO CATCH, and why each one is a test rather than a review

  1. A ROUTE THAT ANSWERS 200 WITH THE WRONG BODY. `medos/api/v1/routes.yaml` now says
     `status: served` for sixteen more rows and `tests/unit/test_permission_contract.py`
     asserts the app serves exactly those paths. What that check CANNOT see is the body.
     So every response here is validated against the `medos/schemas/` file the registry names
     for it, member for member, with the validator this repository already ships
     (`medos/medos/registry/jsonschema.py`). A path that answers 200 with a body its own
     schema rejects is a served row in the same sense that a stub is an implementation.

  2. A SEAL PRESENTED AS CHECKED THAT WAS NOT. This is the defect the whole design turns
     on. `MOS-API-112`: "The risk is not that L4 is skipped; it is that a cohort sealed
     without near-duplicate detection is presented to a no-code operator as *sealed* and
     reads to them as *checked*." So the battery is asserted to carry all nine rows of
     `MOS-UI-133`, and L3 and L4 are asserted to be `skipped` -- with a `skipped_reason`
     and an `operator_disclosure` -- on a deployment whose Gateway refuses the
     `dataset_export` consumer class. A `pass` there would be the whole failure.

  3. THE SEAL BECOMING A `Job`. `MOS-API-001` and `MOS-TRAIN-121` C3 both forbid it. The
     test drives a seal and asserts `jobs` is empty afterwards, because an import-graph
     check cannot see a `Job` created through a generic repository call.

  4. A PARTIAL OUTCOME. `MOS-UI-132`: both objects or neither. A `SUCCEEDED` run is
     asserted to name both, and the database constraint that admits an orphan version
     only on `SEALING` and `FAILED` is exercised directly, because that is the case the
     first draft of the migration made unstorable -- and an unstorable fault is a fault
     nobody reports.

  5. A PREVIEW THAT CREATED SOMETHING. `R25` is asserted to leave `dataset_splits`
     untouched and to carry `is_preview: true` / `authoritative: false` as consts.

  6. A SPLIT TOOL THAT ACQUIRED A WAY TO DEFEAT L1. `MOS-TRAIN-117` names four parameter
     spellings; `tests/integration/test_curation.py` already greps `seal_from_batch` for
     them and this file greps `medos.training.split.assign`, because the grep that
     protects the caller does not protect the generator the caller acquired.

  7. A CROSS-TENANT READ, and a credential without the grant. Every route is driven with
     a ceiling that does not carry its permission and asserted to answer `403` naming
     it; `R26` is asserted to need BOTH of its permissions, which is the half a registry
     `also_requires:` entry cannot enforce.

WHAT THIS FILE DOES NOT COVER, AND WHERE IT MOVED TO. This paragraph used to read "there
is no HTTP path to open a harvest batch or to decide a candidate: `R1`-`R5` are `status:
reserved`". That stopped being true when `medos/medos/api/routes_curation.py` served all five;
`docs/spec/99-known-inconsistencies.md` entry 88 records why the reservation was never a
constraint. The fixtures below STILL build the batch and its decisions through
`medos.training.curation`, deliberately: this file asserts one property per row and a
fixture that drove `R1` would make every test here depend on the draw's own inputs --
`result_provenance` rows, a tenant salt, an archive. `tests/integration/
test_api_curation_walk.py` is where the chain is driven end to end over HTTP with nothing
in between, and that is the file to read for "can a person do this from a browser".
Everything from the sampling plan and the batch READS onwards is real HTTP here too.

SKIP DISCIPLINE: `tests/_support/skips.py` only, through the `tests/integration` prefix's
`postgres` declaration in `tests/_support/stack.py`.

Spec: MOS-API-001, MOS-API-005, MOS-API-011, MOS-API-015, MOS-API-035, MOS-API-046,
MOS-API-056, MOS-API-057, MOS-API-084, MOS-API-089, MOS-API-112, MOS-DATA-037,
MOS-EVID-013, MOS-EVID-021, MOS-EVID-028, MOS-EVID-031, MOS-EVID-034, MOS-EVID-037,
MOS-EVID-046, MOS-SEC-008, MOS-SEC-072, MOS-STORE-359, MOS-TRAIN-072, MOS-TRAIN-079,
MOS-TRAIN-081, MOS-TRAIN-083, MOS-TRAIN-110, MOS-TRAIN-112, MOS-TRAIN-114,
MOS-TRAIN-117, MOS-TRAIN-121, MOS-TRAIN-208, MOS-UI-101, MOS-UI-111, MOS-UI-116,
MOS-UI-117, MOS-UI-122, MOS-UI-130, MOS-UI-131, MOS-UI-132, MOS-UI-133, MOS-UI-144.
"""

from __future__ import annotations

import inspect
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from medos.api.routes_curation import PERMISSIONS

# THE TRAIN DEPLOYABLE'S FACTORY, not Core's. `medos.api.app:create_app` builds the
# PACS-and-models service, which does not mount the training or curation routers --
# the routes this module exercises are served by `medos-train-api`. Building Core
# here would 404 on every one of them.
from medos.api.training_plane import create_training_app as create_app  # noqa: E402
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import digest as ev_digest
from medos.evidence import repo as ev_repo
from medos.registry.jsonschema import iter_errors
from medos.training import curation as cur
from medos.training import policy as pol
from medos.training import seal as seal_mod
from medos.training import split as split_mod
from psycopg.rows import dict_row

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = "11111111-1111-1111-1111-111111111111"
READER = "33333333-3333-3333-3333-333333333333"
CAPABILITY = "pleural_effusion"
SALT = b"curation-suite-salt-not-a-real-secret"

#: `MOS-TRAIN-114` sets the floor at 30 `test` patients and the split is 70/10/20, so the
#: smallest cohort that can clear it is 150 -- `MOS-UI-142` does exactly this arithmetic
#: on the screen. 160 drawn with one in twenty excluded leaves 152 included.
PATIENTS = 160

CURATION_TABLES = (
    "seal_runs", "corpus_stratification_reports", "curation_decisions",
    "harvest_candidates", "harvest_batches", "sampling_plans",
    "training_data_policies",
)
EVIDENCE_TABLES = (
    "datasets", "dataset_versions", "dataset_cases", "dataset_splits",
    "dataset_split_members", "annotation_sets", "annotation_readers", "annotations",
)

DEID = {
    "deidentification_status": "pseudonymised",
    "deid_policy_id": "test/deid-profile/v4",
    "uid_mapping_table_id": f"uidmap/{DEFAULT_TENANT_ID}/v1",
}
ANNOTATION_STACK = {
    "tool": "MONAI Label 0.8.4 + 3D Slicer 5.6.2",
    "instructions_uri": "https://example.invalid/reading-instructions",
}


# =====================================================================================
# Schema registry. `MOS-API-084` makes `medos/schemas/` the single source of truth for every
# response body, so the assertion is against the FILE and never against a copy.
# =====================================================================================
def _registry() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in (ROOT / "medos" / "schemas").rglob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        out[str(document["$id"])] = document
    return out


REGISTRY = _registry()


def _inline_local_refs(node, root):
    """Resolve `#/$defs/...` pointers before validating.

    REPORTED DIVERGENCE, worked around here rather than papered over.
    `medos/medos/registry/jsonschema.py::_resolve` REFUSES a local pointer -- "name the
    schema by its $id so one manifest schema is one addressable document
    (MOS-REG-015)" -- and the `medos/schemas/` tree that `MOS-API-084` makes authoritative
    uses `$defs` freely (`seal-run`, `dataset-split`, `annotation-set-freeze-request`,
    `sampling-plan-declare-request`). So this repository holds two JSON Schema
    dialects: chapter 6's registry dialect, which forbids local pointers, and chapter
    10's wire dialect, which uses them. Neither is wrong; nothing reconciles them, and
    the consequence is that the platform's own validator cannot read the platform's own
    wire schemas without this pre-pass. Which dialect `medos/schemas/` is written in is
    chapter 6's and chapter 10's to settle. This is a TEST FIXTURE and MUST NOT become
    a runtime component.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target = root
            for segment in ref[2:].split("/"):
                target = target[segment.replace("~1", "/").replace("~0", "~")]
            merged = {k: v for k, v in node.items() if k != "$ref"}
            return {**_inline_local_refs(target, root), **merged}
        return {k: _inline_local_refs(v, root) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_local_refs(v, root) for v in node]
    return node


def _schema(name: str) -> Any:
    document = REGISTRY[f"https://spec.medicalos.org/schemas/v1/{name}/1.0.0.json"]
    return _inline_local_refs(document, document)


def conforms(instance: Any, name: str) -> None:
    """Assert a response body against the schema `medos/api/v1/routes.yaml` names for its row.

    The validator is `medos/medos/registry/jsonschema.py` -- the one this repository ships and
    already uses for capability manifests -- rather than a new dependency: `MOS-REG-014`
    made it the platform's validator and a second one would disagree with it on exactly
    the edge cases a schema is written for.
    """
    problems = [str(p) for p in iter_errors(instance, _schema(name), registry=REGISTRY)]
    assert not problems, f"{name}: {problems}"


# =====================================================================================
# Server, credentials and database fixtures
# =====================================================================================
def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def _running_server(app: Any) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def curation_url(pg_dsn: str) -> Iterator[str]:
    """One app over the throwaway database, with an IN-PROCESS seal driver.

    `app.state.medos_seal_driver` is the injection point `seal_driver_for` reads, and it
    is used here rather than the shipped `ThreadSealDriver` for one reason: a test that
    polls a thread is a test that is sometimes green. The driver below runs
    `medos.training.seal.execute` synchronously on its own connection, so the `202` is
    still a `202`, the `SealRun` is still polled, and the battery is asserted without a
    race. `test_the_shipped_driver_runs_the_seal_on_its_own_thread` covers the real one.
    """

    def opener(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(pg_dsn, row_factory=dict_row, **kwargs)

    app = create_app(connect=opener, configure_logs=False)
    app.state.medos_manifest_store = (_MemoryStore(), "medos-evidence")

    class _Inline:
        def start(self, seal_run_id: str, tenant_id: str) -> None:
            token = bind_current_tenant(tenant_id)
            conn = opener()
            try:
                seal_mod.execute(
                    conn,
                    seal_run_id,
                    store=app.state.medos_manifest_store[0],
                    bucket=app.state.medos_manifest_store[1],
                    gateway=None,
                    deid=seal_mod.load_deid_provenance(),
                )
            finally:
                conn.close()
                from medos.db.tenancy import reset_current_tenant

                reset_current_tenant(token)

    app.state.medos_seal_driver = _Inline()
    with _running_server(app) as url:
        yield url


class _MemoryStore:
    """`medos.evidence.store.InMemoryManifestStore`, session-scoped for this app.

    Not a mock: it is a real driver of the port, and the seal path exercises put/get/
    delete against it unchanged. One per app instance, because a fresh one per request
    would write the manifest and drop it -- the `MOS-EVID-015` failure the store exists
    to avoid.
    """

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self._objects[(bucket, key)] = bytes(data)

    def get(self, bucket: str, key: str) -> bytes:
        return self._objects[(bucket, key)]

    def delete(self, bucket: str, key: str) -> None:
        self._objects.pop((bucket, key), None)

    def __len__(self) -> int:
        return len(self._objects)


def _issue(pg_dsn: str, scope: list[str], label: str, principal: str = OPERATOR) -> str:
    from medos.security import store

    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        minted, _record = store.issue(
            conn,
            tenant_id=DEFAULT_TENANT_ID,
            principal_kind="service_account",
            principal_id=principal,
            created_by=store.BOOTSTRAP_OPERATOR_ID,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scope=scope,
            label=label,
            env="dev",
        )
        conn.commit()
    return str(minted.plaintext)


@pytest.fixture(scope="session")
def curator_key(pg_dsn: str) -> str:
    """A credential carrying all twelve permissions these sixteen rows bind."""
    return _issue(pg_dsn, sorted(PERMISSIONS), "corpus assembly, full ceiling")


@pytest.fixture()
def curator(curation_url: str, curator_key: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=curation_url,
        timeout=120.0,
        headers={"Authorization": f"Bearer {curator_key}"},
    ) as c:
        yield c


@pytest.fixture()
def declared(tmp_path: Path) -> Iterator[Path]:
    """The three deployment declarations this surface reads, for one test.

    Set on `os.environ` and not on the app, because each loader reads it PER REQUEST on
    purpose: a declaration cached at import is one an operator cannot correct without a
    restart, and CONTRACT.md section 11 forbids the module-level cache that would make
    the caching possible.
    """
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    previous = {
        k: os.environ.get(k)
        for k in (
            seal_mod.DEID_PROVENANCE_VAR,
            "MEDOS_ANNOTATION_STACK",
            "MEDOS_ANNOTATION_CAMPAIGN_DIR",
            "MEDOS_SEAL_RETRIEVAL",
        )
    }
    os.environ[seal_mod.DEID_PROVENANCE_VAR] = json.dumps(DEID)
    os.environ["MEDOS_ANNOTATION_STACK"] = json.dumps(ANNOTATION_STACK)
    os.environ["MEDOS_ANNOTATION_CAMPAIGN_DIR"] = str(campaigns)
    # No Gateway is injected into the inline driver, so this only makes the intent
    # explicit: this deployment seals from candidate metadata and retrieves no pixels.
    os.environ["MEDOS_SEAL_RETRIEVAL"] = "metadata_only"
    try:
        yield campaigns
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:  # pragma: no cover - only when a developer exports one
                os.environ[key] = value


@pytest.fixture()
def cdb(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute("TRUNCATE " + ", ".join(CURATION_TABLES + EVIDENCE_TABLES) + " CASCADE")
    conn.execute("TRUNCATE jobs, job_queue, job_events, job_steps, job_series CASCADE")
    conn.execute("UPDATE tenants SET training_use_allowed = false")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant

        reset_current_tenant(token)
        conn.close()


# =====================================================================================
# The cohort. Built through the engine, because MOS-API-112 forbids serving R1-R5.
# =====================================================================================
def _uid(*parts: int) -> str:
    return "1.2.826.0.1.3680043.10.9." + ".".join(str(p) for p in parts)


def _site(patient: int) -> dict[str, Any]:
    """Two sites, two scanners, two kernel classes, two years: the cohort that clears
    C1 (<= 0.60 site share), C2 (<= 0.70 scanner), C3 (two reconstruction classes) and
    C7 (two sites for vendor_evidence)."""
    half = patient % 2
    return {
        "institution": f"SITE-{half}",
        "manufacturer": "SIEMENS" if half else "GE MEDICAL SYSTEMS",
        "model": "Sensation 16" if half else "Discovery CT750 HD",
        "kernel": "B30f" if half else "STANDARD",
        "kernel_class": "soft" if half else "standard",
        "thickness": 1.25 if half else 2.5,
        "year": 2022 + half,
    }


@pytest.fixture()
def cohort(cdb: psycopg.Connection[Any]) -> dict[str, str]:
    """A permitted tenant, a Dataset, a plan, a batch and 160 settled candidates."""
    pol.record_policy(
        cdb,
        legal_basis="research_ethics_approval",
        basis_reference="REC-2026-118",
        scope={
            "modalities": ["CT"],
            "body_parts": ["CHEST"],
            "capabilities": [CAPABILITY],
            "date_from": "2020-01-01",
            "date_to": None,
        },
        recorded_by=OPERATOR,
        permits_redistribution=True,
    )
    pol.set_training_use_allowed(cdb, True)
    dataset = ev_repo.create_dataset(
        cdb,
        slug=f"corpus-{secrets.token_hex(4)}",
        display_name="curated training corpus",
        purpose="training",
        custodian="TCIA/TEST",
        created_by=OPERATOR,
    )
    plan = cur.declare_sampling_plan(
        cdb,
        capability_id=CAPABILITY,
        plan_version=1,
        strata={
            "score_band": {"include": ["bottom", "top"]},
            "review_outcome": {"include": ["REJECTED", "MODIFIED"]},
            "ran_on_platform": {"include": [True, False]},
            "acquisition_bucket": {"include": ["thin", "thick"]},
        },
    )
    batch = cur.open_batch(
        cdb, capability_id=CAPABILITY, sampling_plan_id=plan, opened_by=OPERATOR
    )
    for patient in range(PATIENTS):
        s = _site(patient)
        ran = patient % 3 == 0
        candidate = cur.add_candidate(
            cdb,
            batch_id=batch.id,
            patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", f"P{patient:04d}"),
            study_instance_uid=_uid(patient, 1),
            series_instance_uids=[_uid(patient, 1, 1)],
            instance_uids=[_uid(patient, 1, 1, i) for i in range(40)],
            geometry={"tilt_deg": 0.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
            acquisition=cur.CandidateAcquisition(
                manufacturer=s["manufacturer"],
                manufacturer_model_name=s["model"],
                convolution_kernel=s["kernel"],
                convolution_kernel_class=s["kernel_class"],
                slice_thickness_mm=s["thickness"],
                pixel_spacing_mm=(0.703125, 0.703125),
                kvp=120.0,
                exposure_mas=110.0,
                contrast_phase="non_contrast",
                institution_key=ev_digest.institution_key(SALT, s["institution"]),
                study_year=s["year"],
            ),
            institution_key=ev_digest.institution_key(SALT, s["institution"]),
            deid_policy_version=4,
            sampling_weight=1.0,
            # harvest_candidates_ran_ck: ran_on_platform = (platform_outcome IS NOT NULL)
            ran_on_platform=ran,
            platform_outcome="COMPLETED" if ran else None,
            review_outcome="ACCEPTED" if ran else None,
            score_band="top" if patient % 2 else "bottom",
            acquisition_bucket="thin" if patient % 2 else "thick",
            study={
                "modality": "CT",
                "body_part_examined": "CHEST",
                "capability_id": CAPABILITY,
                "study_date": f"{s['year']}-05-0{1 + patient % 9}",
            },
        )
        # MOS-TRAIN-080: a NAMED HUMAN decides, one candidate at a time. `decide` is the
        # only writer of `decision='include'` and there is no auto-inclusion.
        cur.decide(
            cdb,
            candidate_id=candidate,
            decision="include" if patient % 20 else "exclude",
            decided_by=OPERATOR,
            review_seconds=45,
            reason_code=None if patient % 20 else "quality_artefact",
        )
    cdb.commit()
    return {"dataset_id": dataset.id, "batch_id": batch.id, "plan_id": plan}


def _seal(curator: httpx.Client, cohort: dict[str, str]) -> dict[str, Any]:
    """Drive `R26` and poll `R27` to a terminal state. Returns the final `SealRun`."""
    r = curator.post(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/seal",
        json={"dataset_id": cohort["dataset_id"]},
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["seal_run_id"]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        body = curator.get(f"/api/v1/seal-runs/{run_id}").json()
        if body["state"] in seal_mod.TERMINAL_STATES:
            return body
        time.sleep(0.2)
    raise AssertionError("the seal run never reached a terminal state")  # pragma: no cover


# =====================================================================================
# 1. The registry's claim about the wire, checked against the wire
# =====================================================================================
def test_the_policy_read_answers_false_rather_than_refusing(
    curator: httpx.Client, cdb: psycopg.Connection[Any]
) -> None:
    """`MOS-UI-110`: the console's FIRST screen renders `training_use_allowed = false`
    under the refusal contract and MUST NOT render an HTTP status, a problem document or
    the string `403`. A tenant with no policy is therefore `200` with `policy: null` --
    the absence of an instrument is the answer, and it is the answer the screen paints.
    """
    r = curator.get(f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"training_use_allowed": False, "policy": None}
    conforms(body, "training-data-policy")


def test_another_tenants_policy_is_a_404_and_not_a_403(curator: httpx.Client) -> None:
    """The one route in the block whose PATH could be used to probe for another tenant.

    `MOS-SEC-072`: telling a caller that a row it may not read exists is the leak. The
    tenant comes from the credential (`MOS-API-003`) and a mismatch is `404`.
    """
    other = "99999999-9999-9999-9999-999999999999"
    r = curator.get(f"/api/v1/tenants/{other}/training-policy")
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "TENANT_NOT_FOUND"


def test_the_plan_is_declared_read_back_and_sealed(
    curator: httpx.Client, cdb: psycopg.Connection[Any]
) -> None:
    """`R19`, `R20`, `R21`. `MOS-STORE-359` seals the row: no PATCH, no DELETE, and a
    second declaration at the same version is a conflict rather than an edit."""
    body = {
        "capability_id": CAPABILITY,
        "plan_version": 7,
        "strata": {
            "score_band": {"include": ["bottom", "top"]},
            "review_outcome": {"include": ["REJECTED"]},
            "ran_on_platform": {"include": [False]},
            "acquisition_bucket": {"include": ["thin"]},
        },
    }
    r = curator.post("/api/v1/sampling-plans", json=body)
    assert r.status_code == 201, r.text
    plan = r.json()
    conforms(plan, "sampling-plan")
    assert r.headers["Location"] == f"/api/v1/sampling-plans/{plan['sampling_plan_id']}"
    # MOS-TRAIN-083's floor and MOS-TRAIN-101's are SERVER-SET; no request member
    # reaches either, and a client that may supply one may supply zero.
    assert plan["min_naive_fraction"] == 0.2
    assert plan["de_novo_control_fraction"] == 0.1

    listed = curator.get("/api/v1/sampling-plans").json()
    conforms(listed, "page-sampling-plan")
    assert listed["next_cursor"] is None  # MOS-API-017 is owed, and is not faked
    assert "total" not in listed  # MOS-API-016

    one = curator.get(f"/api/v1/sampling-plans/{plan['sampling_plan_id']}")
    assert one.status_code == 200
    conforms(one.json(), "sampling-plan")

    again = curator.post("/api/v1/sampling-plans", json=body)
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "SAMPLING_PLAN_VERSION_EXISTS"


def test_a_plan_cannot_carry_an_operator_a_range_or_a_pattern(
    curator: httpx.Client, cdb: psycopg.Connection[Any]
) -> None:
    """`MOS-UI-111` names a query language, a free-text search, a regular expression, a
    boolean expression builder and a saved-query editor and forbids each one. The wire
    form has no member in which any of them could be written, so the refusal is `400`
    and not a server that quietly ignores the extra key."""
    for facet in ({"exclude": ["x"]}, {"include": ["a"], "min": 1}, {"pattern": ".*"}):
        r = curator.post(
            "/api/v1/sampling-plans",
            json={
                "capability_id": CAPABILITY,
                "plan_version": 99,
                "strata": {
                    "score_band": facet,
                    "review_outcome": {"include": ["REJECTED"]},
                    "ran_on_platform": {"include": [False]},
                    "acquisition_bucket": {"include": ["thin"]},
                },
            },
        )
        assert r.status_code == 400, (facet, r.text)


def test_the_batch_and_its_queue_read_back(
    curator: httpx.Client, cohort: dict[str, str]
) -> None:
    """`R22`, `R23`, `R24`. The queue is the DRAWN set: an excluded candidate is here,
    with its reason, and not absent (`MOS-TRAIN-080`, `MOS-TRAIN-081`)."""
    listed = curator.get("/api/v1/harvest-batches").json()
    conforms(listed, "page-harvest-batch")
    assert any(b["harvest_batch_id"] == cohort["batch_id"] for b in listed["items"])

    one = curator.get(f"/api/v1/harvest-batches/{cohort['batch_id']}")
    assert one.status_code == 200
    conforms(one.json(), "harvest-batch")
    assert one.json()["candidate_count"] == PATIENTS

    page = curator.get(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/candidates", params={"limit": 25}
    ).json()
    conforms(page, "page-harvest-candidate")
    assert len(page["items"]) == 25
    assert page["has_more"] is True
    excluded = curator.get(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/candidates",
        params={"decision": "exclude", "limit": 200},
    ).json()
    assert len(excluded["items"]) == PATIENTS // 20
    assert all(c["reason_code"] == "quality_artefact" for c in excluded["items"])


def test_the_candidate_projection_carries_no_phi_and_no_instance_list(
    curator: httpx.Client, cohort: dict[str, str]
) -> None:
    """THE ROW CLOSEST TO A PATIENT. `MOS-TRAIN-079` forbids `PatientName`,
    `PatientBirthDate`, `AccessionNumber`, institution free text and every source-space
    UID; `MOS-UI-122` forbids the console displaying any of them; `MOS-UI-113` forbids a
    control that resolves `institution_key` back to a name, so there is no display-name
    member to tempt a resolver. `instance_uids` is a COUNT: a page whose size is the
    cohort's instance count is not a page."""
    page = curator.get(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/candidates", params={"limit": 5}
    ).json()
    forbidden = (
        "patient_name", "patientname", "birth", "accession", "mrn",
        "institution_name", "display_name", "note", "decided_by",
    )
    for item in page["items"]:
        for member in item:
            assert not any(f in member.lower() for f in forbidden), member
        # `series_instance_uids` IS projected and `instance_uids` is NOT: the first is
        # one line per candidate, the second is the cohort's whole instance count, and a
        # page the size of that is not a page. Both are de-identified UIDs, which
        # MOS-API-006 permits in a response.
        assert "instance_uids" not in item
        assert isinstance(item["instance_count"], int)
        # MOS-TRAIN-089's HMAC, never a name.
        assert item["institution_key"] and " " not in item["institution_key"]


def test_the_split_preview_is_server_side_and_creates_nothing(
    curator: httpx.Client, cohort: dict[str, str], cdb: psycopg.Connection[Any]
) -> None:
    """`R25`. `MOS-UI-116` forbids a second implementation in the browser and
    `MOS-UI-117` makes this a preview: `is_preview` and `authoritative` are consts on
    the wire rather than words the console is trusted to add. `MOS-EVID-028` makes a
    split a materialised manifest and never a projection, so nothing is written."""
    before = cdb.execute("SELECT count(*) AS n FROM dataset_splits").fetchone()["n"]
    r = curator.get(f"/api/v1/harvest-batches/{cohort['batch_id']}/split-preview")
    assert r.status_code == 200, r.text
    body = r.json()
    conforms(body, "split-preview")
    assert body["is_preview"] is True and body["authoritative"] is False
    assert "dataset_split_id" not in body and "split_id" not in body
    assert body["included_patient_count"] == PATIENTS - PATIENTS // 20
    assert body["undecided_candidate_count"] == 0
    assert body["test_partition_floor"] == 30
    assert body["meets_test_partition_floor"] is True
    cdb.rollback()
    after = cdb.execute("SELECT count(*) AS n FROM dataset_splits").fetchone()["n"]
    assert after == before


def test_the_preview_and_the_seal_agree_because_they_call_one_function(
    curator: httpx.Client, cohort: dict[str, str], declared: Path,
    cdb: psycopg.Connection[Any],
) -> None:
    """`MOS-UI-116`'s actual content: "a second implementation disagrees with the
    authoritative one on exactly the cohorts that sit near a bound". The preview and the
    frozen split are asserted to carry the SAME per-partition counts, which is only true
    because both evaluate `medos.training.split.assign`."""
    preview = curator.get(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/split-preview"
    ).json()
    run = _seal(curator, cohort)
    assert run["state"] == "SUCCEEDED", run["refusals"]
    split = curator.get(f"/api/v1/dataset-splits/{run['dataset_split_id']}").json()
    assert split["partition_patients"] == preview["partition_patients"]
    assert split["assignment_method"] == preview["assignment_method"]


# =====================================================================================
# 2. The seal, and the disclosure that is the point of it
# =====================================================================================
def test_the_seal_is_accepted_polled_and_names_both_objects(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """`R26` and `R27`, end to end. `MOS-API-056`'s `202` + `Location`, `MOS-API-057`'s
    `Retry-After: 30`, and `MOS-UI-132`'s both-or-neither."""
    r = curator.post(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/seal",
        json={"dataset_id": cohort["dataset_id"]},
    )
    assert r.status_code == 202, r.text
    submitted = r.json()
    conforms(submitted, "seal-run")
    assert r.headers["Retry-After"] == "30"
    assert r.headers["Location"] == f"/api/v1/seal-runs/{submitted['seal_run_id']}"
    assert submitted["dataset_version_id"] is None
    assert submitted["dataset_split_id"] is None

    final = curator.get(f"/api/v1/seal-runs/{submitted['seal_run_id']}").json()
    conforms(final, "seal-run")
    assert final["state"] == "SUCCEEDED", final["refusals"]
    assert final["dataset_version_id"] and final["dataset_split_id"]
    assert final["manifest_digest"] and final["split_digest"]
    # MOS-TRAIN-088 records the FULL C1-C7 result whether it passes or blocks.
    assert final["corpus_stratification_report_id"]
    assert final["progress"]["patients_total"] == PATIENTS - PATIENTS // 20


def test_the_battery_has_all_nine_rows_and_cannot_pass_a_check_it_did_not_run(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """THE TEST THIS WHOLE DESIGN EXISTS FOR.

    `MOS-API-112`: "The risk is not that L4 is skipped; it is that a cohort sealed
    without near-duplicate detection is presented to a no-code operator as *sealed* and
    reads to them as *checked*." On this deployment no pixels are retrieved -- the
    Gateway answers every `dataset_export` retrieval `503 DEID_NOT_IMPLEMENTED` because
    `MOS-DATA-037` requires it to -- so L3 and L4 MUST report `skipped`, with
    `MOS-EVID-037`'s explicit reason and `MOS-UI-105`'s operator disclosure beside it.
    """
    final = _seal(curator, cohort)
    battery = final["check_battery"]
    assert [c["order"] for c in battery] == list(range(1, 10))
    assert [c["check_id"] for c in battery] == [
        r["check_id"] for r in seal_mod.BATTERY_ROWS
    ]

    by_id = {c["check_id"]: c for c in battery}
    for name in ("leakage_pixel_identity", "leakage_near_duplicate"):
        check = by_id[name]
        assert check["outcome"] == "skipped", check
        assert check["series_without_pixel_evidence"] == final["pixel_evidence"][
            "series_total"
        ]
        assert len(check["skipped_reason"]) >= 20
        assert len(check["operator_disclosure"]) >= 40
        # MOS-UI-105 fixes the register of the operator-facing half: no requirement id,
        # no check id, no field name, no digest, no UID.
        disclosure = check["operator_disclosure"]
        assert "MOS-" not in disclosure
        assert "sha256" not in disclosure
        assert "dhash" not in disclosure.lower()

    # And the structural half: no entry may claim `pass` over series it never saw.
    for check in battery:
        if check["outcome"] == "pass":
            assert check["series_without_pixel_evidence"] == 0, check
        if check["outcome"] == "warn":
            assert check["blocking"] is False, check
        if check["outcome"] == "skipped":
            assert check["skipped_reason"] and check["operator_disclosure"], check


def test_pixel_evidence_is_reported_at_the_top_level_as_well_as_per_check(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """`MOS-TRAIN-068` and `MOS-TRAIN-199`: the pipeline acquires imaging ONLY as
    `dataset_export`. The const is what stops the field ever carrying `clinical_viewer`,
    and the counters are what make "this cohort's images were never fetched" a queryable
    property of the seal rather than a comment."""
    final = _seal(curator, cohort)
    evidence = final["pixel_evidence"]
    assert evidence["consumer_class"] == "dataset_export"
    assert evidence["series_total"] == PATIENTS - PATIENTS // 20
    assert evidence["series_with_pixel_digest"] == 0
    assert evidence["series_with_perceptual_hash"] == 0


def test_the_seal_creates_no_job(
    curator: httpx.Client, cohort: dict[str, str], declared: Path,
    cdb: psycopg.Connection[Any],
) -> None:
    """`MOS-API-001`: "POST /api/v1/jobs is the only endpoint that may create a Job. No
    other path in the system may create a Job." `MOS-TRAIN-121` C3 forbids it from the
    other direction by excluding `job.create` from the orchestrator account.

    Asserted against the TABLE and not against the import graph: a `Job` created through
    a generic repository call is invisible to a call-graph check, and this is the one
    long-running operation on the platform that a later change would be most tempted to
    put on the job lifecycle.
    """
    assert _seal(curator, cohort)["state"] == "SUCCEEDED"
    cdb.rollback()
    assert cdb.execute("SELECT count(*) AS n FROM jobs").fetchone()["n"] == 0
    assert cdb.execute("SELECT count(*) AS n FROM job_queue").fetchone()["n"] == 0


def test_a_second_seal_of_the_same_batch_is_refused(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """`MOS-TRAIN-209`: sealing is idempotent on CONTENT, not a second identity for the
    same batch. The refusal is at submit, inside the request, because nothing about the
    cohort has to be read to answer it."""
    assert _seal(curator, cohort)["state"] == "SUCCEEDED"
    again = curator.post(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/seal",
        json={"dataset_id": cohort["dataset_id"]},
    )
    assert again.status_code == 422, again.text
    assert any(
        r["code"] == "batch_already_sealed" for r in again.json().get("refusals", [])
    )


def test_a_seal_with_no_deployment_declaration_is_a_503_at_submit(
    curator: httpx.Client, cohort: dict[str, str], tmp_path: Path
) -> None:
    """`MOS-EVID-021` requires a pseudonymised cohort to name its de-identification
    policy and its UID mapping table, and this platform has no table for either. The
    refusal is a `503` under `MOS-UI-160` -- a platform fault, not something the
    operator did -- and it arrives AT SUBMIT rather than from a poll two minutes later,
    after a whole cohort has been retrieved to discover it.
    """
    previous = os.environ.pop(seal_mod.DEID_PROVENANCE_VAR, None)
    try:
        r = curator.post(
            f"/api/v1/harvest-batches/{cohort['batch_id']}/seal",
            json={"dataset_id": cohort["dataset_id"]},
        )
    finally:
        if previous is not None:  # pragma: no cover - only when exported
            os.environ[seal_mod.DEID_PROVENANCE_VAR] = previous
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["code"] == "DEPLOYMENT_NOT_DECLARED"
    assert body["retryable"] is False
    assert body["class"] == "system_failure"
    assert body["environment_variable"] == seal_mod.DEID_PROVENANCE_VAR


def test_the_body_cannot_name_a_partition_a_seed_or_a_waiver(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """`MOS-TRAIN-141`: a container that can name its own data can name the test
    partition. `MOS-EVID-028` forbids a seed. `MOS-UI-135` forbids this surface writing
    a waiver at all. The wire form has one member, and `extra="forbid"` is what makes
    the list of absences structural rather than a comment in a schema."""
    for extra in (
        {"assignments": [["pk_aaaaaaaaaaaaaaaa", "test"]]},
        {"seed": 7},
        {"partitions": {"test": 0.2}},
        {"stratification_waivers": [{"check_id": "C1"}]},
        {"deidentification_status": "public_deidentified"},
        {"source_description": "typed by the operator"},
        {"evidence_kind": "portable_evidence"},
    ):
        r = curator.post(
            f"/api/v1/harvest-batches/{cohort['batch_id']}/seal",
            json={"dataset_id": cohort["dataset_id"], **extra},
        )
        assert r.status_code == 400, (extra, r.status_code, r.text)


# =====================================================================================
# 3. What the seal produced, read back
# =====================================================================================
def test_the_frozen_split_carries_the_whole_leakage_report(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """`R28`. `MOS-EVID-035` stores it verbatim and `MOS-EVID-037` makes a `skipped` L4 a
    PERMANENT property of the split -- so a projection carrying only `frozen: true`
    would hide, one screen after the `SealRun` made it visible, the thing the `SealRun`
    exists to show. There is no POST on this collection: see `R26`."""
    final = _seal(curator, cohort)
    r = curator.get(f"/api/v1/dataset-splits/{final['dataset_split_id']}")
    assert r.status_code == 200, r.text
    split = r.json()
    conforms(split, "dataset-split")
    assert split["partition_level"] == "patient"
    assert split["leakage_report"]["L4"] == "skipped"
    # L3 too: see test_the_frozen_split_agrees_with_the_seal_run_about_l3.
    assert split["leakage_report"]["L3"] == "skipped"
    assert split["leakage_report"]["L1"] == "pass"
    assert split["partition_patients"]["test"] >= split_mod.TEST_PARTITION_FLOOR
    # MOS-EVID-031: every partition present, `excluded` at zero rather than omitted.
    assert set(split["partition_patients"]) == {"train", "tune", "test", "excluded"}
    # MOS-EVID-028: a materialised manifest, never a seed.
    assert "seed" not in json.dumps(split).lower().replace("seed_label", "")


def test_the_sealed_version_reads_back_for_the_train_summary(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """Row 37. `MOS-UI-147` renders the cohort as a read-only summary, and a console
    that could see its cohort only through the `SealRun` that made it would lose the
    summary the next day."""
    final = _seal(curator, cohort)
    r = curator.get(f"/api/v1/dataset-versions/{final['dataset_version_id']}")
    assert r.status_code == 200, r.text
    version = r.json()
    conforms(version, "dataset-version")
    assert version["status"] == "SEALED"
    assert version["manifest_digest"] == final["manifest_digest"]
    assert version["patient_count"] == PATIENTS - PATIENTS // 20


def test_the_dataset_catalogue_is_readable_and_has_no_writer(
    curator: httpx.Client, cohort: dict[str, str]
) -> None:
    """Row 32 is served so that `R26`'s `dataset_id` is SELECTED (`MOS-UI-101`). Row 33
    is NOT: it binds `dataset.write`, which section 8.3.2 registers as a spelling a
    build MUST fail on and `MOS-UI-104` forbids this surface propagating."""
    r = curator.get("/api/v1/datasets")
    assert r.status_code == 200, r.text
    conforms(r.json(), "page-dataset")
    assert any(d["dataset_id"] == cohort["dataset_id"] for d in r.json()["items"])
    assert curator.post("/api/v1/datasets", json={}).status_code == 405


def test_sealing_against_an_unknown_dataset_is_a_404(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    r = curator.post(
        f"/api/v1/harvest-batches/{cohort['batch_id']}/seal",
        json={"dataset_id": "44444444-4444-4444-4444-444444444444"},
    )
    assert r.status_code == 404, r.text
    assert r.json()["code"] == "DATASET_NOT_FOUND"


# =====================================================================================
# 4. The reference standard
# =====================================================================================
def _campaign(campaigns: Path, version_id: str, cdb: psycopg.Connection[Any]) -> int:
    """Write the closed campaign the deployment declares. `MOS-EVID-040`'s lines.

    Written from `dataset_cases` -- the sealed manifest -- so the set covers every
    patient in the cohort, which is what `MOS-EVID-046` requires of a set an
    `EvaluationRun` will cite.
    """
    import hashlib

    cdb.rollback()
    rows = cdb.execute(
        "SELECT patient_key, study_instance_uid, series_instance_uid FROM dataset_cases "
        "WHERE dataset_version_id = %s ORDER BY patient_key",
        (version_id,),
    ).fetchall()
    reader = f"rdr_{OPERATOR.replace('-', '')}"
    lines = []
    for row in rows:
        r = dict(row)
        digest = "sha256:" + hashlib.sha256(r["series_instance_uid"].encode()).hexdigest()
        lines.append(json.dumps({
            "patient_key": r["patient_key"],
            "study_instance_uid": r["study_instance_uid"],
            "series_instance_uid": r["series_instance_uid"],
            "reference": {"kind": "mask", "uri": f"s3://b/{r['patient_key']}.seg.nrrd",
                          "digest": digest, "geometry": "source",
                          "reference_volume_ml": 286.4},
            "per_reader": [{"reader_id": reader, "uri": f"s3://b/{r['patient_key']}.nrrd",
                            "digest": digest, "volume_ml": 286.4}],
        }))
    (campaigns / f"{version_id}.ndjson").write_text("\n".join(lines), encoding="utf-8")
    return len(lines)


_FREEZE_BODY = {
    "name": "reference standard v1",
    "capability_id": CAPABILITY,
    "label_definition_id": "22222222-2222-2222-2222-222222222222",
    "annotation_type": "mask",
    "consensus_rule": "single_reader",
    "readers": [{"reader_id": OPERATOR, "role": "radiologist"}],
    "reference_of_record": True,
}


def test_closing_a_campaign_that_produced_nothing_is_refused(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """THE REFUSAL THAT MATTERS MOST ON `R29`. An `AnnotationSet` whose
    `annotation_digest` is a digest over zero lines is a set every `EvaluationRun`
    citing it must FAIL against (`MOS-EVID-046`: "MUST cause the run to fail, not to
    silently shrink `n`"). Minting one because the annotation store is unbuilt would be
    the quiet downgrade this surface exists against, so the route answers `503` and
    names the gap instead."""
    final = _seal(curator, cohort)
    r = curator.post(
        f"/api/v1/dataset-versions/{final['dataset_version_id']}/annotation-sets",
        json=_FREEZE_BODY,
    )
    assert r.status_code == 503, r.text
    assert r.json()["environment_variable"] == "MEDOS_ANNOTATION_CAMPAIGN_DIR"
    assert r.json()["retryable"] is False


def test_the_reference_standard_freezes_and_reads_back(
    curator: httpx.Client, cohort: dict[str, str], declared: Path,
    cdb: psycopg.Connection[Any],
) -> None:
    """`R29`, `R30`, `R31`. `MOS-UI-124` has the PLATFORM fill `tool` from the deployed
    annotation stack, so it is not a member of the request and is asserted to arrive on
    the row from the declaration. `MOS-TRAIN-110` makes the set un-appendable: a second
    freeze under the same name is a conflict."""
    final = _seal(curator, cohort)
    version_id = final["dataset_version_id"]
    n = _campaign(declared, version_id, cdb)
    assert n == PATIENTS - PATIENTS // 20

    r = curator.post(
        f"/api/v1/dataset-versions/{version_id}/annotation-sets", json=_FREEZE_BODY
    )
    assert r.status_code == 201, r.text
    created = r.json()
    conforms(created, "annotation-set")
    assert r.headers["Location"].endswith(created["annotation_set_id"])
    assert created["reader_count"] == 1
    assert created["readers"][0]["tool"] == ANNOTATION_STACK["tool"]
    # MOS-EVID-038: the named reader is recoverable. The uuid is carried into the
    # column's `rdr_` grammar without truncation and translated back on the wire, where
    # MOS-UI-123 requires a User principal -- so the round trip is the identity.
    assert created["readers"][0]["reader_id"] == OPERATOR
    stored = cdb.execute(
        "SELECT reader_id FROM annotation_readers WHERE annotation_set_id = %s",
        (created["annotation_set_id"],),
    ).fetchone()
    assert dict(stored)["reader_id"] == f"rdr_{OPERATOR.replace('-', '')}"

    listed = curator.get(f"/api/v1/dataset-versions/{version_id}/annotation-sets").json()
    conforms(listed, "page-annotation-set")
    assert [i["annotation_set_id"] for i in listed["items"]] == [
        created["annotation_set_id"]
    ]
    one = curator.get(f"/api/v1/annotation-sets/{created['annotation_set_id']}")
    assert one.status_code == 200
    conforms(one.json(), "annotation-set")

    again = curator.post(
        f"/api/v1/dataset-versions/{version_id}/annotation-sets", json=_FREEZE_BODY
    )
    assert again.status_code == 409, again.text
    assert again.json()["code"] == "ANNOTATION_SET_NAME_EXISTS"


def test_the_request_body_carries_no_per_case_payload_and_no_tool(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """`MOS-EVID-038`: "a client that supplies the reference standard on the wire can
    supply one no reader produced, and the named readers would be an assertion rather
    than a record". `MOS-UI-124` keeps `tool` off the body."""
    final = _seal(curator, cohort)
    version_id = final["dataset_version_id"]
    for extra in (
        {"entries": [{"patient_key": "pk_aaaaaaaaaaaaaaaa"}]},
        {"annotations": []},
        {"annotation_digest": "sha256:" + "0" * 64},
    ):
        r = curator.post(
            f"/api/v1/dataset-versions/{version_id}/annotation-sets",
            json={**_FREEZE_BODY, **extra},
        )
        assert r.status_code == 400, (extra, r.text)
    r = curator.post(
        f"/api/v1/dataset-versions/{version_id}/annotation-sets",
        json={
            **_FREEZE_BODY,
            "readers": [{"reader_id": OPERATOR, "role": "radiologist", "tool": "vim"}],
        },
    )
    assert r.status_code == 400, r.text


# =====================================================================================
# 5. Authorisation, and the conjunction a registry cannot enforce
# =====================================================================================
@pytest.mark.parametrize(
    ("method", "path", "permission"),
    [
        ("GET", f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy",
         "training_data_policy.read"),
        ("POST", "/api/v1/sampling-plans", "sampling_plan.declare"),
        ("GET", "/api/v1/sampling-plans", "sampling_plan.read"),
        ("GET", "/api/v1/harvest-batches", "harvest_batch.read"),
        ("GET", "/api/v1/datasets", "dataset.read"),
    ],
)
def test_every_route_refuses_a_credential_without_its_permission(
    pg_dsn: str, curation_url: str, method: str, path: str, permission: str
) -> None:
    """`MOS-SEC-027`/`MOS-SEC-045`: the scope entry is a CEILING. What is asserted here
    is the NECESSARY half -- the route refuses without it -- which is the half this
    platform can state, since it has no roles table."""
    scope = sorted(set(PERMISSIONS) - {permission})
    key = _issue(pg_dsn, scope, f"without {permission}", principal=READER)
    # A VALID body, because FastAPI validates before the handler runs and a 400 from
    # the request schema would hide the 403 this test is about -- which is exactly the
    # shape in which an authorisation sweep passes while authorising nothing.
    body = {
        "capability_id": CAPABILITY,
        "plan_version": 31,
        "strata": {
            "score_band": {"include": ["top"]},
            "review_outcome": {"include": ["REJECTED"]},
            "ran_on_platform": {"include": [False]},
            "acquisition_bucket": {"include": ["thin"]},
        },
    }
    with httpx.Client(base_url=curation_url, timeout=30.0,
                      headers={"Authorization": f"Bearer {key}"}) as c:
        r = c.request(method, path, json=body if method == "POST" else None)
    assert r.status_code == 403, r.text
    assert r.json()["required_permission"] == permission


def test_the_seal_needs_both_of_its_permissions(
    pg_dsn: str, curation_url: str, cohort: dict[str, str], declared: Path
) -> None:
    """`MOS-API-005` admits exactly one permission per row and `MOS-UI-130` makes `R26`
    one action spanning two. `medos/api/v1/routes.yaml` records the second under
    `also_requires:`; THIS is where it is enforced, which is the half a registry entry
    cannot do. Binding only the first would let a principal who may seal a version
    freeze a split they may not."""
    for missing in ("dataset_version.create", "dataset_split.freeze"):
        scope = sorted(set(PERMISSIONS) - {missing})
        key = _issue(pg_dsn, scope, f"seal without {missing}", principal=READER)
        with httpx.Client(base_url=curation_url, timeout=30.0,
                          headers={"Authorization": f"Bearer {key}"}) as c:
            r = c.post(
                f"/api/v1/harvest-batches/{cohort['batch_id']}/seal",
                json={"dataset_id": cohort["dataset_id"]},
            )
        assert r.status_code == 403, (missing, r.text)
        assert r.json()["required_permission"] == missing
        assert set(r.json()["required_permissions"]) == {
            "dataset_version.create", "dataset_split.freeze"
        }


def test_an_anonymous_request_is_refused(curation_url: str) -> None:
    """`MOS-SEC-008` admits anonymous access on `/healthz`, `/readyz` and the OpenAPI
    document and nowhere else."""
    with httpx.Client(base_url=curation_url, timeout=30.0) as c:
        assert c.get("/api/v1/datasets").status_code == 401


# =====================================================================================
# 6. The split generator, and the four parameters it MUST NOT have
# =====================================================================================
def _patient_keys(n: int) -> list[str]:
    """`MOS-EVID-010`'s shape: `pk_` plus 16 base32 characters from `[a-z2-7]`."""
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"

    def encode(value: int) -> str:
        out = []
        for _ in range(16):
            out.append(alphabet[value % 32])
            value //= 32
        return "".join(reversed(out))

    keys = ["pk_" + encode(i * 2_654_435_761) for i in range(n)]
    # Asserted, because a generator that collides would make `assign` look as though it
    # dropped patients -- which is the defect this fixture is used to rule out.
    assert len(set(keys)) == n
    return keys


def test_assign_never_acquires_a_way_to_defeat_l1() -> None:
    """`MOS-TRAIN-117`: "a split tool that offers a 'minimum days between studies' option
    is offering a way to defeat this check". `MOS-EVID-028` forbids a seed.

    `tests/integration/test_curation.py` already greps `seal_from_batch` for the same
    four. This grep is over the GENERATOR the caller acquired, which that one cannot see.
    """
    signature = inspect.signature(split_mod.assign)
    for forbidden in ("seed", "min_days_between_studies", "study_level_split",
                      "allow_same_patient"):
        assert forbidden not in signature.parameters, forbidden
    source = inspect.getsource(split_mod)
    for forbidden in ("min_days_between_studies", "study_level_split",
                      "allow_same_patient"):
        # Named in the prose that forbids them and nowhere else, so the grep is exact.
        assert source.count(forbidden) <= 2, forbidden


def test_assign_is_a_pure_function_evaluated_once() -> None:
    """`MOS-TRAIN-112`: "Assignment MUST be a pure function evaluated once, whose output
    is the manifest." Two calls with the same three arguments produce the same manifest,
    in this process and in any other -- which is what makes `MOS-EVID-028`'s "the
    manifest is the split, never a seed" true rather than aspirational."""
    keys = _patient_keys(60)
    strata = {k: {"manufacturer": "SIEMENS", "slice_thickness_band": "<=2.0mm"}
              for k in keys}
    first = split_mod.assign(keys, strata, "cap/batch")
    second = split_mod.assign(list(reversed(keys)), strata, "cap/batch")
    assert first.pairs == second.pairs
    # MOS-EVID-031: every patient appears EXACTLY once. The reference implementation's
    # integer cuts leave a remainder unplaced; this one places it and says so.
    assert sorted(pk for pk, _ in first.pairs) == sorted(keys)
    assert len({pk for pk, _ in first.pairs}) == len(keys)


def test_a_different_seed_label_produces_a_different_split() -> None:
    """`MOS-EVID-028` permits the seed label to be RECORDED and forbids it being the
    split. This is the other half of the previous test: the function is deterministic
    and is not constant."""
    keys = _patient_keys(60)
    strata = {k: {"manufacturer": "SIEMENS", "slice_thickness_band": "<=2.0mm"}
              for k in keys}
    assert (
        split_mod.assign(keys, strata, "cap/batch-a").pairs
        != split_mod.assign(keys, strata, "cap/batch-b").pairs
    )


def test_the_recorded_method_names_the_cuts_that_were_actually_made() -> None:
    """ONE FIELD OVER FROM A LIE THIS MODULE ALREADY FIXED.

    `assignment_method`'s own docstring explains why `stratified_by` became a parameter:
    the string named the module constant while `assign()` stratified on whatever its
    caller handed it, so "the first caller to pass anything else got a method string
    naming a stratum nobody stratified on". The percentages were left hard-coded at
    `70/10/20` in that same string, and they had exactly the same property.

    The first caller to pass anything else was a technical run that opens no held-out
    `test` partition and therefore splits 80/20. Its recorded method -- the account of
    how these patients were placed, stored on the frozen row -- described a three-way
    split that never happened.
    """
    keys = _patient_keys(60)
    strata = {k: {"manufacturer": "SIEMENS", "slice_thickness_band": "<=2.0mm"}
              for k in keys}
    two_way = split_mod.assign(keys, strata, "cap/batch",
                               partitions=(("train", 80), ("tune", 20)))

    assert "80/20" in two_way.method, two_way.method
    assert "70/10/20" not in two_way.method, (
        "the recorded method describes a three-way split; this assignment made two cuts"
    )
    # The partition nobody asked for is not reported as one that exists and is empty.
    assert "test" not in two_way.partition_patients, two_way.partition_patients
    assert two_way.partition_patients["train"] == 48
    assert two_way.partition_patients["tune"] == 12

    # And the default is untouched: this is a new capability, not a changed one.
    default = split_mod.assign(keys, strata, "cap/batch")
    assert "70/10/20" in default.method, default.method
    assert default.partition_patients["test"] == 12


# =====================================================================================
# 6a. The retrieval boundary, and the credential it is not allowed to borrow
# =====================================================================================
def test_the_retriever_refuses_to_borrow_the_workers_gateway_credential() -> None:
    """THE DEFECT THIS TEST EXISTS FOR WAS REAL, AND IT WAS IN THE FIRST DRAFT.

    The consumer class is resolved SERVER-SIDE from the principal
    (`medos/medos/gateway/auth.py::resolve_consumer_class`), so a constant in the client says
    what the pipeline is ENTITLED to and not what the Gateway will grant it. The first
    version of `medos/medos/training/retrieval.py` was wired through
    `GatewayConfig.from_env()`, which reads `MEDOS_DICOMWEB_URL` and
    `MEDOS_DICOMWEB_TOKEN` -- the worker's pair. The Gateway resolves that key as
    `platform_writer`, whose egress row reads "De-identification on egress: none"
    (`MOS-DATA-021`), and `MOS-TRAIN-068` forbids the pipeline touching the identified
    side at all. On a deployment with real studies the seal would have retrieved
    IDENTIFIED pixels, computed perfectly correct digests over them, and reported L3 and
    L4 as `pass` -- a seal that succeeds and is wrong, which is worse than one that
    refuses.

    So the token variable is the pipeline's own and there is no fallback.
    """
    from medos.training import retrieval

    gateway, reason = retrieval.gateway_from_env(
        {"MEDOS_DICOMWEB_URL": "http://gateway:8043/dicomweb/t",
         "MEDOS_DICOMWEB_TOKEN": "the-workers-platform-writer-key"}
    )
    assert gateway is None
    assert retrieval.DATASET_EXPORT_TOKEN_VAR in reason
    assert "platform_writer" in reason

    # A URL is not a credential, so the worker's URL may be shared.
    gateway, reason = retrieval.gateway_from_env(
        {"MEDOS_DICOMWEB_URL": "http://gateway:8043/dicomweb/t",
         retrieval.DATASET_EXPORT_TOKEN_VAR: "the-pipelines-own-key"}
    )
    assert gateway is not None and reason == ""

    # And the one place that reads it is this function: a second reader is how "the
    # pipeline only ever retrieves as dataset_export" silently stops being true.
    source = (
        ROOT / "medos" / "medos" / "training" / "retrieval.py"
    ).read_text(encoding="utf-8") + (
        ROOT / "medos" / "medos" / "api" / "routes_curation.py"
    ).read_text(encoding="utf-8") + (
        ROOT / "medos" / "medos" / "training" / "seal.py"
    ).read_text(encoding="utf-8")
    assert source.count('"MEDOS_DICOMWEB_TOKEN"') == 0


def test_an_unfetchable_cohort_is_reported_rather_than_silently_skipped(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """`MOS-EVID-037` requires an EXPLICIT reason, and "not available" is not one.

    The `SealRun` must be able to tell an operator WHICH of the two happened -- the
    Gateway refused, or nobody configured a credential -- because the remedies are
    different people. Both land in `skipped_reason`; neither is silence.
    """
    final = _seal(curator, cohort)
    by_id = {c["check_id"]: c for c in final["check_battery"]}
    reason = by_id["leakage_pixel_identity"]["skipped_reason"]
    assert "MOS-EVID-018" in reason
    assert str(final["pixel_evidence"]["series_total"]) in reason
    # The engineer-facing half names the requirement; the operator-facing half does not.
    assert "MOS-" not in by_id["leakage_pixel_identity"]["operator_disclosure"]


def test_the_frozen_split_agrees_with_the_seal_run_about_l3(
    curator: httpx.Client, cohort: dict[str, str], declared: Path
) -> None:
    """THE ONE PLACE THE DISCLOSURE COULD HAVE LEAKED AWAY.

    The `SealRun` says L3 `skipped`. `MOS-EVID-035` stores the leakage report VERBATIM
    on the frozen split and `MOS-EVID-013` makes that row immutable, so if the engine
    wrote `pass` there the permanent record would contradict the disclosure for ever --
    and the permanent record is the one a `ValidationReport` cites. `_l3` is told which
    series carry no pixel-derived digest and reports `skipped` for them.
    """
    final = _seal(curator, cohort)
    battery = {c["check_id"]: c["outcome"] for c in final["check_battery"]}
    split = curator.get(f"/api/v1/dataset-splits/{final['dataset_split_id']}").json()
    assert battery["leakage_pixel_identity"] == "skipped"
    assert split["leakage_report"]["L3"] == "skipped"
    assert battery["leakage_near_duplicate"] == "skipped"
    assert split["leakage_report"]["L4"] == "skipped"
    # And the stored detail carries MOS-EVID-037's explicit reason, so the split is
    # self-describing once the SealRun is history. `LeakageCheck.as_dict()` nests the
    # check's own `detail` one level down, which is the shape the ValidationReport
    # cohort block of section 7.12.1 already prints.
    l3 = split["leakage_report"]["detail"]["L3"]["detail"]
    assert "skipped_reason" in l3
    assert l3["n_skipped"] == split["partition_patients"]["train"] + (
        split["partition_patients"]["tune"] + split["partition_patients"]["test"]
    )


# =====================================================================================
# 7. The database refuses what MOS-UI-132 forbids, and admits what MOS-UI-160 requires
# =====================================================================================
def test_the_seal_run_row_refuses_a_split_without_a_version(
    cdb: psycopg.Connection[Any], cohort: dict[str, str]
) -> None:
    """`seal_runs_split_implies_version`. A split with no version is a record of
    something that cannot have occurred -- `dataset_splits.dataset_version_id` is NOT
    NULL -- so the row is refused rather than stored and puzzled over later."""
    run = seal_mod.submit(
        cdb, batch_id=cohort["batch_id"], dataset_id=cohort["dataset_id"],
        submitted_by=OPERATOR,
    )
    cdb.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        cdb.execute(
            "UPDATE seal_runs SET dataset_split_id = %s WHERE id = %s",
            ("55555555-5555-5555-5555-555555555555", run.id),
        )
    cdb.rollback()


def test_the_seal_run_row_admits_an_orphan_version_only_as_a_fault(
    curator: httpx.Client, cdb: psycopg.Connection[Any], cohort: dict[str, str],
    declared: Path,
) -> None:
    """`seal_runs_orphan_version_is_a_fault`, and the reason it is not an equality.

    `MOS-UI-132` says a partial outcome "MUST be reported as a platform fault under
    MOS-UI-160". The first draft of this table wrote the two ids as an equality -- both
    or neither -- which reads like that requirement and makes the one row that could
    report the fault unstorable. The `DatasetVersion` is immutable (`MOS-EVID-013`) and
    undeletable (`MOS-API-011` answers 409), so the orphan EXISTS whether or not the
    platform is willing to name it.
    """
    done = _seal(curator, cohort)
    version_id = done["dataset_version_id"]
    assert version_id
    cdb.rollback()

    # A SECOND batch: MOS-TRAIN-209 refuses a second seal of the first one, which is
    # the behaviour `test_a_second_seal_of_the_same_batch_is_refused` asserts.
    second = cur.open_batch(
        cdb, capability_id=CAPABILITY, sampling_plan_id=cohort["plan_id"],
        opened_by=OPERATOR,
    )
    cdb.commit()
    run = seal_mod.submit(
        cdb, batch_id=second.id, dataset_id=cohort["dataset_id"],
        submitted_by=OPERATOR,
    )
    cdb.commit()
    # PENDING with a version and no split: refused. The version is REAL, so the foreign
    # key is satisfied and the CHECK is the only thing that can refuse the row --
    # otherwise this test would pass against a constraint that does not exist.
    with pytest.raises(psycopg.errors.CheckViolation):
        cdb.execute(
            "UPDATE seal_runs SET dataset_version_id = %s WHERE id = %s",
            (version_id, run.id),
        )
    cdb.rollback()
    # FAILED with the same orphan: ACCEPTED. MOS-UI-160 requires the partial outcome to
    # be reported and MOS-EVID-013 makes the object undeletable, so refusing to record
    # which version was stranded would leave the operator with an orphan nobody names.
    cdb.execute(
        "UPDATE seal_runs SET state = 'FAILED', phase = 'finished', "
        "failure_reason = 'FreezeRefused', finished_at = now(), "
        "check_battery = %s::jsonb, dataset_version_id = %s WHERE id = %s",
        (json.dumps([dict(r) for r in seal_mod.BATTERY_ROWS]), version_id, run.id),
    )
    cdb.commit()
    stranded = seal_mod.get_seal_run(cdb, run.id)
    assert stranded is not None
    assert stranded.state == "FAILED"
    assert stranded.dataset_version_id == version_id
    assert stranded.dataset_split_id is None


def test_a_terminal_seal_run_carries_all_nine_checks_in_the_database(
    cdb: psycopg.Connection[Any], cohort: dict[str, str]
) -> None:
    """`seal_runs_battery_complete`. `MOS-API-112`: "a missing check is not a pass", and
    an array that could be short is an array a client reads as "everything that
    applied"."""
    run = seal_mod.submit(
        cdb, batch_id=cohort["batch_id"], dataset_id=cohort["dataset_id"],
        submitted_by=OPERATOR,
    )
    cdb.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        cdb.execute(
            "UPDATE seal_runs SET state = 'REFUSED', finished_at = now(), "
            "refusals = '[{\"code\":\"x\",\"check_id\":\"y\",\"message\":\"z\"}]'::jsonb, "
            "check_battery = '[]'::jsonb WHERE id = %s",
            (run.id,),
        )
    cdb.rollback()


def test_a_refused_seal_run_must_say_why(
    cdb: psycopg.Connection[Any], cohort: dict[str, str]
) -> None:
    """`seal_runs_refused_says_why`. `MOS-UI-105` requires the `why` part to be the
    actual observed value and the actual bound, and a refusal document with no refusals
    in it cannot produce one."""
    run = seal_mod.submit(
        cdb, batch_id=cohort["batch_id"], dataset_id=cohort["dataset_id"],
        submitted_by=OPERATOR,
    )
    cdb.commit()
    with pytest.raises(psycopg.errors.CheckViolation):
        cdb.execute(
            "UPDATE seal_runs SET state = 'REFUSED', finished_at = now() WHERE id = %s",
            (run.id,),
        )
    cdb.rollback()


def test_a_seal_run_cannot_be_deleted(
    cdb: psycopg.Connection[Any], cohort: dict[str, str]
) -> None:
    """`MOS-API-011` puts the sealed objects on the append-only set and the record of
    the act that made them belongs there with them: a `REFUSED` seal an operator can
    make disappear is a refusal that never happened."""
    seal_mod.submit(
        cdb, batch_id=cohort["batch_id"], dataset_id=cohort["dataset_id"],
        submitted_by=OPERATOR,
    )
    cdb.commit()
    tail = os.environ.get("MEDOS_TEST_DATABASE_URL") or ""
    if not tail:  # pragma: no cover - the grant is asserted below instead
        pass
    granted = cdb.execute(
        "SELECT count(*) AS n FROM information_schema.table_privileges "
        "WHERE table_name = 'seal_runs' AND grantee = 'medicalos_app' "
        "AND privilege_type = 'DELETE'"
    ).fetchone()
    assert granted["n"] == 0


# =====================================================================================
# 8. An unsettled batch stops before anything is created
# =====================================================================================
def test_an_unsettled_candidate_blocks_the_seal_before_retrieval(
    curator: httpx.Client, cohort: dict[str, str], declared: Path,
    cdb: psycopg.Connection[Any],
) -> None:
    """`MOS-TRAIN-081`: the exclusion aggregate's denominator is the DRAWN set, so an
    undecided candidate makes it a fraction of something nobody chose. `MOS-UI-131`
    requires the refusal BEFORE anything is created, and the `SealRun` proves it: no
    `DatasetVersion`, no `DatasetSplit`, and nine battery rows that all say `skipped`
    rather than an empty array."""
    cur.add_candidate(
        cdb,
        batch_id=cohort["batch_id"],
        patient_key=ev_digest.patient_key(SALT, "TCIA/TEST", "P9999"),
        study_instance_uid=_uid(9999, 1),
        series_instance_uids=[_uid(9999, 1, 1)],
        instance_uids=[_uid(9999, 1, 1, i) for i in range(40)],
        geometry={"tilt_deg": 0.0, "spacing_class": "uniform", "max_jitter_mm": 0.0},
        acquisition=cur.CandidateAcquisition(manufacturer="SIEMENS"),
        institution_key=ev_digest.institution_key(SALT, "SITE-1"),
        deid_policy_version=4,
        sampling_weight=1.0,
        ran_on_platform=False,
        study={"modality": "CT", "body_part_examined": "CHEST",
               "capability_id": CAPABILITY, "study_date": "2023-05-01"},
    )
    cdb.commit()

    final = _seal(curator, cohort)
    assert final["state"] == "REFUSED"
    conforms(final, "seal-run")
    assert final["dataset_version_id"] is None
    assert final["dataset_split_id"] is None
    assert any(r["check_id"] == "MOS-TRAIN-081" for r in final["refusals"])
    assert len(final["check_battery"]) == 9
    assert all(c["outcome"] == "skipped" for c in final["check_battery"])
    assert final["progress"]["instances_retrieved"] == 0


# =====================================================================================
# 9. The shipped driver, once, so the inline one is not the only thing tested
# =====================================================================================
def test_the_shipped_driver_runs_the_seal_on_its_own_thread(
    pg_dsn: str, cohort: dict[str, str], declared: Path,
    cdb: psycopg.Connection[Any],
) -> None:
    """`ThreadSealDriver` is what a deployment gets. The inline driver above makes the
    battery assertable without a race; this asserts the real one reaches the same
    terminal state, binds the tenant on a thread that starts with no ContextVar
    (`medos.db.tenancy.bind_current_tenant` documents exactly this case), and writes its
    progress on its OWN connection -- the request's is closed when the response is."""

    def opener(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(pg_dsn, row_factory=dict_row, **kwargs)

    store = _MemoryStore()
    driver = seal_mod.ThreadSealDriver(
        opener, store=store, bucket="medos-evidence",
        deid=seal_mod.load_deid_provenance(),
    )
    run = seal_mod.submit(
        cdb, batch_id=cohort["batch_id"], dataset_id=cohort["dataset_id"],
        submitted_by=OPERATOR,
    )
    cdb.commit()
    driver.start(run.id, DEFAULT_TENANT_ID)
    driver.join(timeout=180)

    cdb.rollback()
    final = seal_mod.get_seal_run(cdb, run.id)
    assert final is not None and final.state == "SUCCEEDED", final
    assert final.dataset_version_id and final.dataset_split_id
    assert len(final.check_battery) == 9
    # Three manifests: the dataset, the split, and nothing else until R29 runs.
    assert len(store) == 2
