# SPDX-License-Identifier: Apache-2.0
"""NOTHING TO A TRAINED MODEL, OVER HTTP, WITH NO PYTHON IN THE WALK.

WHAT THIS FILE IS FOR, AND WHY IT IS A SECOND FILE
-----------------------------------------------------
`tests/integration/test_api_curation.py` asserts properties of the corpus-assembly rows
one at a time and says in its own header what it could not cover: "There is no HTTP path
to open a harvest batch or to decide a candidate: `R1`-`R5` are `status: reserved` ... The
batch and its decisions are therefore built through `medos.training.curation`." That is no
longer true, and the claim that replaces it is not a property of one route. It is a
property of the CHAIN: a person who has never opened a Python file can record the legal
basis, declare a sampling plan, draw a cohort, decide it case by case, read the
stratification battery, seal, freeze the split and the reference standard, and submit a
training run -- every step an HTTP request with a body and a response.

So this file contains one long test and the fixtures it needs, and the long test is the
deliverable. Splitting it into twelve would lose the only thing it measures, which is that
the twelve compose.

WHAT COUNTS AS "NO PYTHON", STATED BEFORE THE FIRST ASSERTION
----------------------------------------------------------------
Two things in this file are not the operator's and are not counted as steps.

  THE ARCHIVE. `_seed_archive` writes `patients`, `studies`, `jobs`, `results` and
    `result_provenance` -- the record of a capability having run on a study. That is the
    CLINICAL plane, upstream of every screen chapter 19 describes; on a real deployment
    the Gateway's projection and the worker write those rows, and no operator driving the
    training pipeline from a screen has ever created one -- the screen that would have
    been theirs, the training console, is withdrawn at specification 0.4.0 (MOS-UI-100
    CUT, register entry 150), and this walk drives the API it would have driven. A test
    that refused to seed them would not be measuring the surface, it would be measuring
    whether a hospital exists. It is written through
    `medos.core.provenance.build_record`, the platform's OWN builder, rather than as a
    hand-made dict, so a record this fixture writes is a record the worker could have
    written.
  THE ORCHESTRATOR. `medos.training.runs.start` and `.succeed` are called directly at the
    very end. `MOS-TRAIN-122` puts both in the orchestrator's hands and chapter 19 gives
    the console NO control that starts or completes a run -- `tests/integration/
    test_api_training.py` makes the same point in the same way. What the operator does is
    POST once and GET until the state is terminal, and that is what the walk does.

Everything between those two is HTTP: fifteen requests, each asserted for status, for the
`medos/schemas/` document its registry row names, and for the one fact the next step needs.

WHAT THIS FILE IS TRYING TO CATCH
------------------------------------
  1. A CHAIN THAT PASSES STEP BY STEP AND DOES NOT COMPOSE. Every id the walk uses comes
     out of the previous response and is never reconstructed from the fixture -- so a
     route that returns a well-formed document naming something the next route cannot
     resolve fails here and nowhere else.
  2. A DRAW THAT INVENTED SOMETHING. `test_the_draw_records_what_the_platform_holds_and_
     imputes_nothing` reads the drawn candidates back and asserts that the four
     `MOS-TRAIN-084` fields this platform has no source for are `null` and NOT a default
     (`MOS-EVID-020`), and that `patient_key` and `institution_key` are the HMAC
     constructions of `MOS-EVID-010` and `MOS-TRAIN-089` rather than anything readable.
  3. A BULK DECISION ROUTE ARRIVING BY THE BACK DOOR. `MOS-UI-119` and `MOS-TRAIN-204`:
     the walk decides 160 candidates with 160 requests, and a test asserts the request
     model has no member in which a second candidate could be named.
  4. PHI ON THE WIRE. Every response body of the whole walk is searched for the MRNs, the
     patient names, the institution names and the accession numbers the fixture wrote into
     the archive. `MOS-TRAIN-089`, `MOS-TRAIN-079`, `MOS-EVID-116`.
  5. THE STRATIFICATION SCHEMA DIVERGENCE, PINNED. `R5`'s response is validated against
     its own schema and the failures are asserted to be EXACTLY the two `medos/api/v1/
     routes.yaml` records under `schema_divergence:`. Fixing the schema turns this red,
     which is the point of writing it down instead of skipping the validation.

SKIP DISCIPLINE: `tests/_support/skips.py` only, through the `tests/integration` prefix's
`postgres` declaration in `tests/_support/stack.py`.

Spec: MOS-API-022, MOS-API-046, MOS-API-056, MOS-API-057, MOS-API-084, MOS-API-112,
MOS-EVID-010, MOS-EVID-016, MOS-EVID-020, MOS-EVID-116, MOS-SAFE-083, MOS-TRAIN-072,
MOS-TRAIN-073, MOS-TRAIN-078, MOS-TRAIN-079, MOS-TRAIN-080, MOS-TRAIN-083, MOS-TRAIN-084,
MOS-TRAIN-088, MOS-TRAIN-089, MOS-TRAIN-112, MOS-TRAIN-114, MOS-TRAIN-122, MOS-TRAIN-202,
MOS-TRAIN-203, MOS-TRAIN-204, MOS-UI-101, MOS-UI-110, MOS-UI-116, MOS-UI-119, MOS-UI-130,
MOS-UI-147.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from medos.api.routes_curation import PERMISSIONS as CURATION_PERMISSIONS
from medos.api.routes_curation import SCORE_BANDS, TENANT_SALT_VAR
from medos.api.routes_training import PERMISSIONS as TRAINING_PERMISSIONS

# THE TRAIN DEPLOYABLE'S FACTORY, not Core's. `medos.api.app:create_app` builds the
# PACS-and-models service, which does not mount the training or curation routers --
# the routes this module exercises are served by `medos-train-api`. Building Core
# here would 404 on every one of them.
from medos.api.training_plane import create_training_app as create_app  # noqa: E402
from medos.core import provenance as prov
from medos.db import provenance as prov_db
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import digest as ev_digest
from medos.registry.jsonschema import iter_errors
from medos.training import runs as tr
from medos.training import seal as seal_mod
from psycopg.rows import dict_row

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
OPERATOR = "11111111-1111-1111-1111-111111111111"
CAPABILITY = "pleural_effusion"

#: `MOS-TRAIN-114` sets the floor at 30 `test` patients and `MOS-TRAIN-112` splits
#: 70/10/20, so the smallest cohort that can clear it is 150 -- `MOS-UI-142` does exactly
#: this arithmetic on the screen. 160 drawn with one in twenty excluded leaves 152.
PATIENTS = 160

#: The deployment's per-tenant secret. A REAL value for the run and not a constant in the
#: repository: `medos/medos/evidence/repo.py` refuses to work around the absent chapter 8
#: keyring with a hardcoded salt -- "a salt in the repository is not a salt" -- and a test
#: that pinned one would make every digest it asserts reproducible by anybody who can
#: read it.
TENANT_SALT = secrets.token_hex(32)

#: PHI the fixture writes into the archive and that MUST NOT appear in any response body,
#: header or problem document of the walk. MOS-TRAIN-079 forbids `PatientName`,
#: `PatientBirthDate`, `AccessionNumber` and institution free text on a candidate;
#: MOS-EVID-116 keeps the raw institution name out of every manifest and report.
SITES = ("ST ELSEWHERE GENERAL", "COUNTY MEMORIAL")
SCANNERS = (
    ("SIEMENS", "Sensation 16", "B30f", "soft", 1.25),
    ("GE MEDICAL SYSTEMS", "Discovery CT750 HD", "STANDARD", "standard", 2.5),
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
SPEC_DIGEST = "sha256:" + "11" * 32
ENVIRONMENT: dict[str, Any] = {
    "code_commit": "0" * 40,
    "code_dirty": False,
    "image_digest": "sha256:" + "22" * 32,
    "backend_versions": {"nnunet": "2.5.1", "auto3dseg": "1.4.0",
                         "monai_supervised": "1.4.0"},
    "seeds": {"python": 20260311, "numpy": 20260311, "torch": 20260311,
              "dataloader_worker_base": 900},
    "determinism": {"torch_use_deterministic_algorithms": True, "cudnn_benchmark": False,
                    "cublas_workspace_config": ":4096:8", "tf32_allowed": False},
    "hardware": {"gpu_model": "NVIDIA A100-SXM4-80GB", "gpu_count": 2,
                 "driver": "550.54.15", "cuda": "12.4", "cudnn": "9.1.0",
                 "nccl": "2.21.5"},
    "framework_versions": {"torch": "2.4.1", "monai": "1.4.0", "numpy": "1.26.4",
                           "simpleitk": "2.3.1"},
    "preprocessing": {
        CAPABILITY: {"id": "prep.pulmo.effusion", "version": 2, "digest": SPEC_DIGEST,
                     "output_kind": "label"},
    },
}

CURATION_TABLES = (
    "seal_runs", "corpus_stratification_reports", "curation_decisions",
    "harvest_candidates", "harvest_batches", "sampling_plans",
    "training_data_policies",
)
EVIDENCE_TABLES = (
    "datasets", "dataset_versions", "dataset_cases", "dataset_splits",
    "dataset_split_members", "annotation_sets", "annotation_readers", "annotations",
)
ARCHIVE_TABLES = ("result_provenance", "results", "studies", "patients")


# =====================================================================================
# Schema registry. `MOS-API-084` makes `medos/schemas/` the single source of truth, so every
# assertion about a body is against the FILE and never against a copy.
# =====================================================================================
def _registry() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in (ROOT / "medos" / "schemas").rglob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        out[str(document["$id"])] = document
    return out


REGISTRY = _registry()


def _inline_local_refs(node: Any, root: Any) -> Any:
    """Resolve `#/$defs/...` before validating. See `test_api_curation.py` for the whole
    argument: this repository holds two JSON Schema dialects and the platform's own
    validator refuses chapter 10's. A TEST FIXTURE, never a runtime component."""
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


def problems_against(instance: Any, name: str) -> list[str]:
    return [str(p) for p in iter_errors(instance, _schema(name), registry=REGISTRY)]


def conforms(instance: Any, name: str) -> None:
    problems = problems_against(instance, name)
    assert not problems, f"{name}: {problems}"


# =====================================================================================
# Server, credentials, database
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


class _MemoryStore:
    """`InMemoryManifestStore`'s shape, one per app. A REAL driver of the port and not a
    mock: the seal path exercises put/get/delete against it unchanged."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, str], bytes] = {}

    def put(self, bucket: str, key: str, data: bytes) -> None:
        self._objects[(bucket, key)] = bytes(data)

    def get(self, bucket: str, key: str) -> bytes:
        return self._objects[(bucket, key)]

    def delete(self, bucket: str, key: str) -> None:
        self._objects.pop((bucket, key), None)


@pytest.fixture(scope="session")
def walk_url(pg_dsn: str) -> Iterator[str]:
    """One app over the throwaway database, with an in-process seal driver.

    The driver runs `medos.training.seal.execute` synchronously on its own connection, so
    the `202` is still a `202` and the `SealRun` is still polled -- but the poll is not a
    race. `test_api_curation.py` covers the shipped `ThreadSealDriver`.
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


@pytest.fixture(scope="session")
def operator_key(pg_dsn: str) -> str:
    """ONE credential for the whole walk, carrying the ceiling of both routers.

    One rather than two because the claim being measured is about a PERSON: MOS-UI-102's
    reference principal is `evidence_scientist` plus the harvest and curation permissions
    of MOS-UI-103, and a walk that swapped credentials halfway would be measuring two
    people doing half a job each. `medos/medos/security/scopes.py` is emphatic that a scope
    entry is a CEILING and not a grant (MOS-SEC-027, MOS-SEC-045) -- this platform has no
    roles table -- so what the fixture buys is the necessary half.
    """
    from medos.security import store

    scope = sorted(set(CURATION_PERMISSIONS) | set(TRAINING_PERMISSIONS))
    with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
        minted, _record = store.issue(
            conn,
            tenant_id=DEFAULT_TENANT_ID,
            principal_kind="service_account",
            principal_id=OPERATOR,
            created_by=store.BOOTSTRAP_OPERATOR_ID,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            scope=scope,
            label="the whole walk, full ceiling",
            env="dev",
        )
        conn.commit()
    return str(minted.plaintext)


@pytest.fixture()
def operator(walk_url: str, operator_key: str) -> Iterator[httpx.Client]:
    with httpx.Client(
        base_url=walk_url,
        timeout=180.0,
        headers={"Authorization": f"Bearer {operator_key}"},
    ) as c:
        yield c


@pytest.fixture()
def declared(tmp_path: Path) -> Iterator[Path]:
    """The five deployment declarations this walk reads, for the duration of one test.

    On `os.environ` and not on the app, because every loader reads it PER REQUEST on
    purpose: a declaration cached at import is one an operator cannot correct without a
    restart, and CONTRACT.md section 11 forbids the module-level cache that would make the
    caching possible.
    """
    campaigns = tmp_path / "campaigns"
    campaigns.mkdir()
    keys = (
        seal_mod.DEID_PROVENANCE_VAR,
        "MEDOS_ANNOTATION_STACK",
        "MEDOS_ANNOTATION_CAMPAIGN_DIR",
        "MEDOS_SEAL_RETRIEVAL",
        "MEDOS_TRAINING_ENVIRONMENT",
        TENANT_SALT_VAR,
    )
    previous = {k: os.environ.get(k) for k in keys}
    os.environ[seal_mod.DEID_PROVENANCE_VAR] = json.dumps(DEID)
    os.environ["MEDOS_ANNOTATION_STACK"] = json.dumps(ANNOTATION_STACK)
    os.environ["MEDOS_ANNOTATION_CAMPAIGN_DIR"] = str(campaigns)
    # No Gateway is injected into the inline driver, so this only makes the intent
    # explicit: this deployment seals from candidate metadata and retrieves no pixels.
    os.environ["MEDOS_SEAL_RETRIEVAL"] = "metadata_only"
    os.environ["MEDOS_TRAINING_ENVIRONMENT"] = json.dumps(ENVIRONMENT)
    os.environ[TENANT_SALT_VAR] = TENANT_SALT
    try:
        yield campaigns
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:  # pragma: no cover - only when a developer exports one
                os.environ[key] = value


@pytest.fixture()
def wdb(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute("TRUNCATE " + ", ".join(CURATION_TABLES + EVIDENCE_TABLES) + " CASCADE")
    conn.execute("TRUNCATE jobs, job_queue, job_events, job_steps, job_series CASCADE")
    conn.execute("TRUNCATE " + ", ".join(ARCHIVE_TABLES) + " CASCADE")
    conn.execute("TRUNCATE training_runs CASCADE")
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
# The archive. THE CLINICAL PLANE, NOT THE OPERATOR'S -- see the module docstring.
# =====================================================================================
def _uid(*parts: int) -> str:
    return "1.2.826.0.1.3680043.10.9." + ".".join(str(p) for p in parts)


def _site(patient: int) -> dict[str, Any]:
    """Two sites, two scanners, two kernel classes, two years.

    Not decoration: this is the smallest archive that can clear `MOS-TRAIN-088`'s blocking
    checks -- C1 (<= 0.60 patients from one `institution_key`), C2 (<= 0.70 series from one
    scanner), C3 (>= 2 reconstruction classes) and C7 (>= 2 sites for `vendor_evidence`).
    A single-PACS deployment draws a single-site cohort and C1 refuses it, which is what
    that check is for and is stated here rather than discovered at the seal.
    """
    half = patient % 2
    manufacturer, model, kernel, kernel_class, thickness = SCANNERS[half]
    return {
        "institution": SITES[half],
        "manufacturer": manufacturer,
        "model": model,
        "kernel": kernel,
        "kernel_class": kernel_class,
        "thickness": thickness,
        "year": 2022 + half,
        "pacs_backend": f"site-{half}",
    }


def _backends(conn: psycopg.Connection[Any]) -> None:
    """Two `pacs_backends` rows, because `studies.pacs_backend` is a foreign key and
    because `institution_key` is HMAC'd over it: one backend is one site, and one site
    fails C1."""
    for half in (0, 1):
        conn.execute(
            """
            INSERT INTO pacs_backends (id, kind, dicomweb_base_url, partitioning)
            VALUES (%s, 'dicomweb', %s, 'shared_gateway_filtered')
            ON CONFLICT (id) DO NOTHING
            """,
            (f"site-{half}", f"http://pacs-{half}.invalid/dicomweb"),
        )


def _seed_archive(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """`PATIENTS` studies the capability ran on, as the worker would have recorded them.

    WHY `result_provenance` AND NOT `studies`. `MOS-TRAIN-084` requires the candidate's
    `acquisition_profile` "copied from the source header without imputation", and
    `result_provenance` is the only table in this platform that holds one -- `studies` is a
    thin projection with no header fields and no series UIDs, and `harvest_candidates`
    requires `cardinality(series_instance_uids) >= 1`. `medos/medos/api/routes_curation.py`'s
    `THE DRAW` section argues it in full.

    THE RECORD IS BUILT BY `medos.core.provenance.build_record`, the platform's own
    builder, so that what this fixture writes is a record the worker could have written.
    One respect in which it is RICHER than the shipped worker's, and it is the finding
    `test_the_worker_writes_four_of_MOS_TRAIN_084s_fields_as_null` pins:
    `medos/medos/worker/result_rows.py` sets `manufacturer`, `model`, `kvp` and `contrast_phase`
    to `None` in every record it writes and says so in its own comment. A cohort drawn
    from real platform runs today therefore has no manufacturer, and `MOS-TRAIN-088`'s C2
    and C3 read exactly that field.
    """
    _backends(conn)
    salt = TENANT_SALT.encode("utf-8")
    patient_keys: list[str] = []
    for index in range(PATIENTS):
        s = _site(index)
        mrn = f"MRN-{index:05d}"
        patient_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO patients (id, tenant_id, patient_id_value, issuer_of_patient_id,
                                  patient_name, patient_birth_date, phi_state)
            VALUES (%s, current_tenant_id(), %s, %s, %s, %s, 'pseudonymised')
            """,
            (patient_id, mrn, "ISSUER-A", f"DOE^PATIENT{index:04d}", "1970-01-01"),
        )
        study_uid = _uid(index, 1)
        series_uid = _uid(index, 1, 1)
        instance_uids = tuple(_uid(index, 1, 1, i) for i in range(40))
        conn.execute(
            """
            INSERT INTO studies (tenant_id, patient_id, study_instance_uid,
                                 accession_number, study_date, study_description,
                                 modalities_in_study, series_count, instance_count,
                                 pacs_backend, phi_state)
            VALUES (current_tenant_id(), %s, %s, %s, %s, %s, ARRAY['CT'], 1, %s, %s,
                    'pseudonymised')
            """,
            (
                patient_id,
                study_uid,
                f"ACC{index:07d}",
                f"{s['year']}-05-0{1 + index % 9}",
                # The institution name lives HERE and nowhere the draw reads. It is
                # deliberately real free text in a column MOS-TRAIN-079 keeps off a
                # candidate, so `_no_phi` has a subject: a draw that started projecting
                # study_description, or that HMAC'd the wrong thing, would put this on a
                # screen.
                f"CT CHEST W CONTRAST -- {s['institution']}",
                len(instance_uids),
                s["pacs_backend"],
            ),
        )

        public_id = "job_" + "".join(
            secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(26)
        )
        job = conn.execute(
            """
            INSERT INTO jobs (tenant_id, public_id, service_id, service_version,
                              capability_ids, study_instance_uid, selected_series_uids,
                              idempotency_key,
                              created_by_kind, created_by_id, state, trace_id,
                              correlation_id, root_job_id, deadline_at, started_at,
                              finished_at)
            VALUES (current_tenant_id(), %s, 'medos.slice', '0.1.0', ARRAY[%s], %s,
                    ARRAY[%s], %s,
                    'service_account', 'medos-api', 'COMPLETED', %s, %s,
                    gen_random_uuid(), now() + interval '6 hours', now(), now())
            RETURNING id
            """,
            (
                public_id,
                CAPABILITY,
                study_uid,
                series_uid,
                "ik_" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567")
                                for _ in range(26)),
                secrets.token_hex(16),
                secrets.token_hex(16),
            ),
        ).fetchone()
        assert job is not None
        conn.execute("UPDATE jobs SET root_job_id = id WHERE id = %s", (job["id"],))

        # MOS-TRAIN-083 stratifies on `review_outcome`, which is `results.review_status`:
        # a cohort that is all ACCEPTED teaches the model what it already knows.
        review_status = ("ACCEPTED", "MODIFIED", "REJECTED")[index % 3]
        result = conn.execute(
            """
            INSERT INTO results (tenant_id, job_id, capability_id, capability_version,
                                 result_kind, clinical_use_mode, review_status, findings,
                                 study_instance_uid, input_series_uids,
                                 input_instance_uids, input_instance_count,
                                 input_uid_digest, input_pixel_digest,
                                 preprocessing_version, worker_version, runtime_version,
                                 geometry)
            VALUES (current_tenant_id(), %s, %s, '0.1.0', 'segmentation', 'research_only',
                    %s, %s::jsonb, %s, %s, %s, %s, %s, %s, '2', 'w', 'r', '{}'::jsonb)
            RETURNING id
            """,
            (
                job["id"],
                CAPABILITY,
                review_status,
                json.dumps([{"kind": "effusion", "score": None, "present": True,
                             "measurements": []}]),
                study_uid,
                list(instance_uids[:1] and [series_uid]),
                list(instance_uids),
                len(instance_uids),
                secrets.token_hex(32),
                secrets.token_hex(32),
            ),
        ).fetchone()
        assert result is not None

        now = datetime.now(UTC)
        record = prov.build_record(
            provenance_id="prv_" + "".join(
                secrets.choice("0123456789ABCDEFGHJKMNPQRSTVWXYZ") for _ in range(26)
            ),
            tenant_id=DEFAULT_TENANT_ID,
            result_id=str(result["id"]),
            job_id=public_id,
            idempotency_key="ik_" + "".join(
                secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(26)
            ),
            root_job_id=public_id,
            started_at=now,
            finished_at=now,
            study_instance_uid=study_uid,
            series_considered=(
                prov.SeriesVerdict(
                    series_instance_uid=series_uid,
                    selected=True,
                    instance_count=len(instance_uids),
                    modality="CT",
                ),
            ),
            series_consumed=(series_uid,),
            instance_uids=instance_uids,
            instance_uid_digest="0" * 64,
            pixel_digest=secrets.token_hex(32),
            canonical_geometry={
                "tilt_deg": 0.0,
                "spacing_class": "uniform",
                "max_jitter_mm": 0.0,
                "source_pixel_spacing_mm": [0.703125, 0.703125],
                "series_instance_uid": series_uid,
            },
            # MOS-SAFE-083's acquisition envelope, filled. The shipped worker leaves four
            # of these null and reports it; see the docstring above and the test that
            # pins it.
            acquisition={
                "modality": "CT",
                "body_part": "CHEST",
                "manufacturer": s["manufacturer"],
                "model": s["model"],
                "kernel": s["kernel"],
                "slice_thickness_mm": s["thickness"],
                "kvp": 120.0,
                "contrast_phase": "non_contrast",
                "spacing_class": "uniform",
                "value_units": "HU",
            },
            gateway={"deid_policy_version": 4, "deid_profile": "test/deid-profile/v4"},
            patient_internal_id=str(patient_id),
            capability_id=CAPABILITY,
            capability_version="0.1.0",
            service_id="medos.slice",
            service_version="0.1.0",
            preprocessing_specs=(
                prov.PreprocessingSpecRef(
                    id="prep.pulmo.effusion", version="2", digest=SPEC_DIGEST
                ),
            ),
            worker_version="w",
            runtime_version="r",
        )
        prov_db.save(
            conn,
            result_id=str(result["id"]),
            job_uuid=job["id"],
            root_job_uuid=job["id"],
            record=record,
            capability_id=CAPABILITY,
            capability_version="0.1.0",
            service_id="medos.slice",
            service_version="0.1.0",
            worker_version="w",
            runtime_version="r",
            clinical_use_mode="research_only",
            input_series_uids=(series_uid,),
            input_instance_uids=instance_uids,
            input_uid_digest="0" * 64,
            input_pixel_digest="1" * 64,
        )
        patient_keys.append(ev_digest.patient_key(salt, "ISSUER-A", mrn))
    conn.commit()
    return {
        "patient_keys": patient_keys,
        "institution_keys": [
            ev_digest.institution_key(salt, f"site-{half}") for half in (0, 1)
        ],
        "mrns": [f"MRN-{i:05d}" for i in range(PATIENTS)],
    }


@pytest.fixture()
def archive(wdb: psycopg.Connection[Any]) -> dict[str, Any]:
    return _seed_archive(wdb)


@pytest.fixture()
def dataset(wdb: psycopg.Connection[Any]) -> str:
    """One `Dataset` for the seal to bind to.

    Through the engine because row 33 (`POST /datasets`) is NOT served and never will be
    at that spelling: it binds `dataset.write`, which section 8.3.2 registers as a
    spelling a build MUST fail on (`MOS-UI-104`). The operator SELECTS a dataset from row
    32 and does not compose one, which is the step the walk actually performs.
    """
    from medos.evidence import repo as ev_repo

    row = ev_repo.create_dataset(
        wdb,
        slug=f"corpus-{secrets.token_hex(4)}",
        display_name="curated training corpus",
        purpose="training",
        custodian="TEST/SITE",
        created_by=OPERATOR,
    )
    wdb.commit()
    return str(row.id)


# =====================================================================================
# Helpers the walk uses. Each one is ONE HTTP call plus its schema check.
# =====================================================================================
#: Where a human-readable transcript of the walk is written, when the deployment asks for
#: one. Off by default and never asserted on: a test that needed a file to pass would be a
#: test with a second dependency. `MEDOS_WALK_TRANSCRIPT=<path> pytest ...` produces the
#: artifact a reviewer reads -- fifteen requests with their real bodies -- which is the
#: only honest way to answer "how far does it get" without quoting a summary of itself.
WALK_TRANSCRIPT_VAR = "MEDOS_WALK_TRANSCRIPT"


def _transcribe(response: httpx.Response) -> None:
    path = os.environ.get(WALK_TRANSCRIPT_VAR, "").strip()
    if not path:
        return
    request = response.request
    body = request.content.decode("utf-8", "replace") if request.content else ""
    lines = [f"=== {request.method} {request.url.path} -> {response.status_code}"]
    if body:
        lines.append(f"--> {body[:4000]}")
    lines.append(f"<-- {response.text[:4000]}")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n\n")


def _ok(response: httpx.Response, status: int) -> dict[str, Any]:
    _transcribe(response)
    assert response.status_code == status, (
        f"{response.request.method} {response.request.url.path} -> "
        f"{response.status_code}: {response.text[:2000]}"
    )
    return dict(response.json())


def _no_phi(blob: str, archive: dict[str, Any]) -> None:
    """Nothing in this text may be a patient identifier or an institution name.

    `MOS-TRAIN-079` forbids `PatientName`, `PatientBirthDate`, `AccessionNumber` and
    institution free text on a candidate; `MOS-TRAIN-089` makes `institution_key` an HMAC
    and `MOS-EVID-116` keeps the raw name out of every candidate, manifest and report.
    Checked over the WHOLE response text rather than over named members, because the
    failure this is written against is a value arriving somewhere nobody thought to look.
    """
    for mrn in archive["mrns"]:
        assert mrn not in blob, f"an MRN reached the wire: {mrn}"
    for site in SITES:
        assert site not in blob, f"an institution name reached the wire: {site}"
    assert "DOE^" not in blob, "a PatientName reached the wire"
    assert "ACC0000" not in blob, "an AccessionNumber reached the wire"
    assert "CT CHEST W CONTRAST" not in blob, "a StudyDescription reached the wire"


# =====================================================================================
# THE WALK
# =====================================================================================
def test_a_person_goes_from_nothing_to_a_trained_model_over_http(
    operator: httpx.Client,
    wdb: psycopg.Connection[Any],
    archive: dict[str, Any],
    dataset: str,
    declared: Path,
) -> None:
    """Fifteen HTTP requests, in order, each one feeding the next. No Python in between.

    Read the numbered steps as the screens of chapter 19 section 19.3. Every id comes out
    of the previous response; nothing is reconstructed from the fixture, which is what
    makes this a test of the CHAIN rather than of fifteen routes that each work alone.
    """
    seen: list[str] = []

    # -- 1. R18: the first screen, before anything has been recorded ------------------
    # MOS-UI-110: a tenant with no instrument is 200 with `training_use_allowed: false`
    # and `policy: null`, NEVER a 403 -- the absence of an instrument is the answer the
    # first screen has to paint, and that screen may not render an HTTP status.
    before = _ok(operator.get(f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy"), 200)
    conforms(before, "training-data-policy")
    assert before == {"training_use_allowed": False, "policy": None}

    # -- 2. R4: record the legal basis and set the flag, in one request ---------------
    recorded = operator.put(
        f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy",
        json={
            "training_use_allowed": True,
            "policy": {
                "legal_basis": "research_ethics_approval",
                "basis_reference": "REC-2026-118",
                "scope": {
                    "modalities": ["CT"],
                    "body_parts": ["CHEST"],
                    "capabilities": [CAPABILITY],
                    "date_from": "2020-01-01",
                    "date_to": None,
                },
                "permits_redistribution": True,
            },
        },
    )
    policy = _ok(recorded, 200)
    conforms(policy, "training-data-policy")
    seen.append(recorded.text)
    assert policy["training_use_allowed"] is True
    # MOS-TRAIN-073's named human comes from the CREDENTIAL. There is no `recorded_by`
    # member on the request and the response carries the principal that authenticated.
    assert policy["policy"]["recorded_by"] == OPERATOR
    policy_id = policy["policy"]["training_data_policy_id"]

    # A PUT is idempotent by METHOD, and the handler makes that true rather than assuming
    # it: the same terms again is a no-op that returns the same instrument.
    again = _ok(
        operator.put(
            f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy",
            json={
                "training_use_allowed": True,
                "policy": {
                    "legal_basis": "research_ethics_approval",
                    "basis_reference": "REC-2026-118",
                    "scope": {
                        "modalities": ["CT"], "body_parts": ["CHEST"],
                        "capabilities": [CAPABILITY],
                        "date_from": "2020-01-01", "date_to": None,
                    },
                    "permits_redistribution": True,
                },
            },
        ),
        200,
    )
    assert again["policy"]["training_data_policy_id"] == policy_id

    # -- 3. R19: declare the sampling plan before any candidate is drawn --------------
    # MOS-TRAIN-083. The four required facets ARE the operator's selection (MOS-UI-111):
    # every constraint is expressed by choosing among values, and there is no operator, no
    # negation and no pattern anywhere in this body.
    declared_plan = operator.post(
        "/api/v1/sampling-plans",
        json={
            "capability_id": CAPABILITY,
            "plan_version": 1,
            "strata": {
                "score_band": {"include": list(SCORE_BANDS)},
                "review_outcome": {"include": ["ACCEPTED", "MODIFIED", "REJECTED"]},
                "ran_on_platform": {"include": [True]},
                "acquisition_bucket": {"include": ["<=2.0mm", "<=3.0mm"]},
            },
        },
    )
    plan = _ok(declared_plan, 201)
    conforms(plan, "sampling-plan")
    seen.append(declared_plan.text)
    assert declared_plan.headers["Location"].endswith(plan["sampling_plan_id"])
    # MOS-API-022's optional-row half, closed by the change that served R1-R5.
    assert declared_plan.headers["MedicalOS-Idempotency-Key"]

    # -- 4. R1: open the batch and draw its candidates --------------------------------
    opened = operator.post(
        "/api/v1/harvest-batches",
        json={
            "capability_id": CAPABILITY,
            "sampling_plan_id": plan["sampling_plan_id"],
            "training_data_policy_id": policy_id,
        },
        headers={"Idempotency-Key": "walk-open-batch-1"},
    )
    batch = _ok(opened, 201)
    conforms(batch, "harvest-batch")
    seen.append(opened.text)
    assert opened.headers["Location"].endswith(batch["harvest_batch_id"])
    assert opened.headers["MedicalOS-Idempotency-Key"] == "walk-open-batch-1"
    # MOS-TRAIN-202: the batch is a MATERIALISED list and not a stored query, and
    # `SAMPLED` is the state that says the draw happened.
    assert batch["state"] == "SAMPLED"
    assert batch["candidate_count"] == PATIENTS, (
        "the draw is exhaustive within the plan's strata; every seeded case matches"
    )
    assert batch["training_data_policy_id"] == policy_id
    batch_id = batch["harvest_batch_id"]

    # -- 5. R24: read the queue -------------------------------------------------------
    candidates: list[dict[str, Any]] = []
    cursor = 0
    while cursor < PATIENTS:
        page_response = operator.get(
            f"/api/v1/harvest-batches/{batch_id}/candidates?limit=200"
        )
        page = _ok(page_response, 200)
        conforms(page, "page-harvest-candidate")
        seen.append(page_response.text)
        undecided = [c for c in page["items"] if c["decision"] is None]
        if not undecided:
            break
        for candidate in undecided:
            candidates.append(candidate)
            cursor += 1
        break
    assert len(candidates) == PATIENTS

    # -- 6. R3: decide them, ONE AT A TIME, as the named human ------------------------
    # MOS-UI-119 and MOS-TRAIN-204. 160 requests, because there is no member in which a
    # second candidate could be named and no route that takes a list.
    included = 0
    for index, candidate in enumerate(candidates):
        exclude = index % 20 == 0
        body: dict[str, Any] = {
            "decision": "exclude" if exclude else "include",
            "review_seconds": 45,
        }
        if exclude:
            body["reason_code"] = "quality_artefact"
        decided = operator.post(
            f"/api/v1/harvest-candidates/{candidate['harvest_candidate_id']}/decision",
            json=body,
        )
        document = _ok(decided, 201)
        if index < 3:  # every one is schema-checked below; three are checked in full here
            conforms(document, "curation-decision")
            seen.append(decided.text)
        assert document["decided_by"] == OPERATOR
        included += 0 if exclude else 1
    assert included == PATIENTS - PATIENTS // 20

    # A settled candidate is not re-decided. MOS-TRAIN-203 keeps the row.
    conflict = operator.post(
        f"/api/v1/harvest-candidates/{candidates[0]['harvest_candidate_id']}/decision",
        json={"decision": "include", "review_seconds": 1},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "CURATION_DECISION_ALREADY_SETTLED"

    # -- 7. R2: read one settled decision back ----------------------------------------
    read_back = operator.get(
        f"/api/v1/harvest-candidates/{candidates[0]['harvest_candidate_id']}/decision"
    )
    decision = _ok(read_back, 200)
    conforms(decision, "curation-decision")
    seen.append(read_back.text)
    assert decision["decision"] == "exclude"
    assert decision["reason_code"] == "quality_artefact"

    # -- 8. R25: the projected split, computed by the SERVER ---------------------------
    # MOS-UI-116 forbids a second implementation in the browser; MOS-UI-117 makes this a
    # preview and the seal-time result authoritative, and both are members of the body.
    preview_response = operator.get(
        f"/api/v1/harvest-batches/{batch_id}/split-preview"
    )
    preview = _ok(preview_response, 200)
    conforms(preview, "split-preview")
    seen.append(preview_response.text)
    assert preview["is_preview"] is True and preview["authoritative"] is False
    assert preview["included_patient_count"] == included
    assert preview["meets_test_partition_floor"] is True, (
        f"MOS-TRAIN-114 needs 30 test patients; the preview says {preview}"
    )

    # -- 9. R26: seal. ONE operator action, 202, and never a Job -----------------------
    sealed = operator.post(
        f"/api/v1/harvest-batches/{batch_id}/seal", json={"dataset_id": dataset}
    )
    seal_run = _ok(sealed, 202)
    conforms(seal_run, "seal-run")
    seen.append(sealed.text)
    assert sealed.headers["Retry-After"] == "30"  # MOS-API-057
    run_id = seal_run["seal_run_id"]

    # -- 10. R27: poll it to a terminal state ------------------------------------------
    deadline = time.monotonic() + 180
    final: dict[str, Any] = {}
    while time.monotonic() < deadline:
        poll = operator.get(f"/api/v1/seal-runs/{run_id}")
        final = _ok(poll, 200)
        if final["state"] in seal_mod.TERMINAL_STATES:
            seen.append(poll.text)
            break
        time.sleep(0.2)
    conforms(final, "seal-run")
    assert final["state"] == "SUCCEEDED", json.dumps(final["refusals"], indent=2)
    # MOS-UI-132: both objects or neither.
    assert final["dataset_version_id"] and final["dataset_split_id"]
    # MOS-API-112's structural disclosure: nine rows always, and a deployment that
    # retrieved no pixels CANNOT claim L3 or L4 passed.
    battery = {c["check_id"]: c for c in final["check_battery"]}
    assert set(battery) == {r["check_id"] for r in seal_mod.BATTERY_ROWS}
    assert len(battery) == 9
    # The two pixel-dependent rows. This deployment retrieved no pixels, so neither may
    # say `pass`: MOS-EVID-037 rules that L4 reports `skipped` with an EXPLICIT reason,
    # and MOS-API-112 makes the disclosure structural -- a skipped entry MUST carry a
    # skipped_reason of at least 20 characters and an operator_disclosure of at least 40,
    # written in MOS-UI-105's register. "The risk is not that L4 is skipped; it is that a
    # cohort sealed without near-duplicate detection is presented to a no-code operator as
    # sealed and reads to them as checked."
    for check in ("leakage_pixel_identity", "leakage_near_duplicate"):
        assert battery[check]["outcome"] == "skipped", battery[check]
        assert len(battery[check]["skipped_reason"]) >= 20
        assert len(battery[check]["operator_disclosure"]) >= 40

    # MOS-API-001 and MOS-TRAIN-121 C3: the seal is not a Job and created none.
    assert wdb.execute("SELECT count(*) AS n FROM jobs WHERE service_id <> 'medos.slice'")\
        .fetchone()["n"] == 0

    # -- 11. R5: the stratification battery recorded on the batch ----------------------
    strat_response = operator.get(f"/api/v1/harvest-batches/{batch_id}/stratification")
    report = _ok(strat_response, 200)
    seen.append(strat_response.text)
    assert report["verdict"] in ("pass", "warn")
    assert set(report["checks"]) == {"C1", "C2", "C3", "C4", "C5", "C6", "C7"}
    assert report["harvest_batch_id"] == batch_id
    # MOS-TRAIN-088: the FULL result and not the verdict. Every check carries what was
    # measured beside the bound it was measured against.
    for check_id, check in report["checks"].items():
        assert check["statistic"], check_id
        assert check["outcome"] in ("pass", "warn", "fail", "n/a"), check_id

    # -- 12. R28: the frozen split, with the WHOLE leakage report -----------------------
    split_response = operator.get(f"/api/v1/dataset-splits/{final['dataset_split_id']}")
    split = _ok(split_response, 200)
    conforms(split, "dataset-split")
    seen.append(split_response.text)
    assert split["partition_patients"]["test"] >= 30  # MOS-TRAIN-114
    assert set(split["leakage_report"]) >= {"L1", "L2", "L3", "L4", "L5"}

    # -- 13. R29: close the campaign and freeze the reference standard -----------------
    entries = [
        {
            "patient_key": key,
            "study_instance_uid": _uid(index, 1),
            "series_instance_uid": _uid(index, 1, 1),
            "reader_id": "rdr_" + "a" * 8,
            "label": {"effusion": bool(index % 2)},
        }
        for index, key in enumerate(archive["patient_keys"])
    ]
    (declared / f"{final['dataset_version_id']}.ndjson").write_text(
        "\n".join(json.dumps(e) for e in entries), encoding="utf-8"
    )
    frozen_response = operator.post(
        f"/api/v1/dataset-versions/{final['dataset_version_id']}/annotation-sets",
        json={
            "name": "reader consensus v1",
            "capability_id": CAPABILITY,
            "label_definition_id": str(uuid.uuid4()),
            "annotation_type": "mask",
            "consensus_rule": "single_reader",
            "readers": [{"reader_id": str(uuid.uuid4()), "role": "radiologist"}],
            "reference_of_record": True,
        },
    )
    annotation_set = _ok(frozen_response, 201)
    conforms(annotation_set, "annotation-set")
    seen.append(frozen_response.text)

    # -- 14. R6: Train. MOS-UI-147's "single action with no configuration" -------------
    submitted = operator.post(
        "/api/v1/training-runs",
        json={
            "capability_id": CAPABILITY,
            "dataset_version_id": final["dataset_version_id"],
            "split_id": final["dataset_split_id"],
            "annotation_set_id": annotation_set["annotation_set_id"],
        },
    )
    run = _ok(submitted, 202)
    conforms(run, "training-run")
    seen.append(submitted.text)
    assert run["state"] == "PENDING"
    training_run_id = run["training_run_id"]

    # -- 15. R8: watch it terminate -----------------------------------------------------
    # The two engine calls below are THE ORCHESTRATOR's, not the operator's:
    # `MOS-TRAIN-122` puts start and completion in its hands and chapter 19 gives the
    # console no control that does either. What the operator does is the GET.
    assert _ok(operator.get(f"/api/v1/training-runs/{training_run_id}"), 200)[
        "state"
    ] == "PENDING"
    tr.start(
        wdb, run_id=training_run_id, runner="orchestrator/local",
        fingerprint_digest="sha256:" + "ab" * 32,
    )
    wdb.commit()
    running = _ok(operator.get(f"/api/v1/training-runs/{training_run_id}"), 200)
    assert running["state"] == "RUNNING"
    tr.succeed(wdb, run_id=training_run_id, bundle_digest="sha256:" + "cd" * 32)
    wdb.commit()
    done_response = operator.get(f"/api/v1/training-runs/{training_run_id}")
    done = _ok(done_response, 200)
    conforms(done, "training-run")
    seen.append(done_response.text)
    assert done["state"] == "SUCCEEDED"
    assert done["bundle_digest"] == "sha256:" + "cd" * 32

    # -- and nothing the operator read was a patient identifier ------------------------
    _no_phi("\n".join(seen), archive)


# =====================================================================================
# What the walk cannot say by walking
# =====================================================================================
def test_the_draw_records_what_the_platform_holds_and_imputes_nothing(
    operator: httpx.Client,
    wdb: psycopg.Connection[Any],
    archive: dict[str, Any],
    declared: Path,
) -> None:
    """`MOS-EVID-020`: a missing value is `null` and never a default.

    Four of `MOS-TRAIN-084`'s thirteen fields have NO source in this platform's provenance
    record -- `convolution_kernel_class`, `exposure_mas`, `iterative_recon_strength` and
    `station_key` -- and the draw writes `null` for each rather than a plausible value.
    The consequence is real and is named on the handler: `MOS-TRAIN-088`'s C3 reads
    `convolution_kernel_class`, so a cohort drawn here cannot demonstrate reconstruction
    diversity on that field, and filling it in from `convolution_kernel` would have hidden
    that behind an inference nobody asked for.

    `patient_key` and `institution_key` are asserted to be the HMAC constructions of
    `MOS-EVID-010` and `MOS-TRAIN-089` -- recomputed here from the salt and the natural
    key, so a change that swapped in a readable value fails rather than merely looking
    different.
    """
    _ = archive
    policy_id = _ok(
        operator.put(
            f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy",
            json={
                "training_use_allowed": True,
                "policy": {
                    "legal_basis": "broad_consent",
                    "basis_reference": "CONSENT-2026-01",
                    "scope": {
                        "modalities": ["CT"], "body_parts": ["CHEST"],
                        "capabilities": [CAPABILITY],
                        "date_from": None, "date_to": None,
                    },
                    "permits_redistribution": False,
                },
            },
        ),
        200,
    )["policy"]["training_data_policy_id"]
    plan = _ok(
        operator.post(
            "/api/v1/sampling-plans",
            json={
                "capability_id": CAPABILITY,
                "plan_version": 1,
                "strata": {
                    "score_band": {"include": list(SCORE_BANDS)},
                    "review_outcome": {
                        "include": ["ACCEPTED", "MODIFIED", "REJECTED"]
                    },
                    "ran_on_platform": {"include": [True]},
                    "acquisition_bucket": {"include": ["<=2.0mm", "<=3.0mm"]},
                },
            },
        ),
        201,
    )
    batch = _ok(
        operator.post(
            "/api/v1/harvest-batches",
            json={
                "capability_id": CAPABILITY,
                "sampling_plan_id": plan["sampling_plan_id"],
                "training_data_policy_id": policy_id,
            },
        ),
        201,
    )
    page = _ok(
        operator.get(
            f"/api/v1/harvest-batches/{batch['harvest_batch_id']}/candidates?limit=200"
        ),
        200,
    )
    assert len(page["items"]) == PATIENTS

    salt = TENANT_SALT.encode("utf-8")
    expected_patient_keys = {
        ev_digest.patient_key(salt, "ISSUER-A", f"MRN-{i:05d}") for i in range(PATIENTS)
    }
    expected_institution_keys = {
        ev_digest.institution_key(salt, f"site-{half}") for half in (0, 1)
    }
    for candidate in page["items"]:
        profile = candidate["acquisition_profile"]
        for absent in (
            "convolution_kernel_class",
            "exposure_mas",
            "iterative_recon_strength",
            "station_key",
        ):
            assert profile[absent] is None, (
                f"{absent} has no source in result_provenance and MOS-EVID-020 forbids "
                f"a default; the draw wrote {profile[absent]!r}"
            )
        # What the platform DOES hold travels, unmodified.
        assert profile["manufacturer"] in {s[0] for s in SCANNERS}
        assert profile["slice_thickness_mm"] in {s[4] for s in SCANNERS}
        assert profile["institution_key"] in expected_institution_keys
        assert candidate["patient_key"] in expected_patient_keys
        assert candidate["ran_on_platform"] is True
        assert candidate["platform_outcome"] == "COMPLETED"
        assert candidate["review_outcome"] in ("ACCEPTED", "MODIFIED", "REJECTED")
        # No score and no operating point, so the band says so rather than guessing.
        assert candidate["score_band"] == "unscored"
        assert candidate["decision"] is None  # MOS-TRAIN-080: no auto-inclusion


def test_the_worker_writes_four_of_mos_train_084s_fields_as_null(
) -> None:
    """A REPORTED PLATFORM GAP, PINNED WHERE THE DRAW DEPENDS ON IT.

    `medos/medos/worker/result_rows.py::provenance_record` sets `kvp`, `contrast_phase`,
    `manufacturer` and `model` to `None` in EVERY record it writes, with its own comment
    saying so: "MOS-SAFE-083 members this slice does not carry". The draw reads exactly
    those fields for `MOS-TRAIN-084`'s profile, and `MOS-TRAIN-088`'s C2 (scanner share)
    and C3 (reconstruction classes) read the result.

    So a cohort drawn from what this platform's worker ACTUALLY writes today has
    `manufacturer: null` on every candidate, one scanner value, one kernel class, and
    cannot clear C2 or C3. The walk's fixture writes the fields because they are members
    `MOS-SAFE-083` specifies and a conformant worker would fill; this test is the record
    that the shipped one does not, so the gap is a failing statement in the suite rather
    than a sentence in a report nobody reads.

    Asserted against the SOURCE rather than by running the worker, because reaching the
    worker needs a Gateway, a PACS and a GPU, and the claim is about what the code writes.
    """
    source = (ROOT / "medos" / "medos" / "worker" / "result_rows.py").read_text(
        encoding="utf-8"
    )
    block = source[source.index("        acquisition={"):]
    block = block[: block.index(chr(10) + "        },")]
    for field in ("kvp", "contrast_phase", "manufacturer", "model"):
        assert f'"{field}": None' in block, (
            f"{field} is no longer written as None by the worker -- if it is now filled "
            "from the source header, this test has done its job and should be deleted, "
            "and medos/medos/api/routes_curation.py's THE DRAW section should lose the "
            "corresponding paragraph"
        )


def test_there_is_no_wire_shape_in_which_a_second_candidate_could_be_named() -> None:
    """`MOS-UI-119` and `MOS-TRAIN-204`, as a property of the request model.

    "The console MUST NOT offer a *select all and include* control that writes decisions
    without the cases having been shown." A console can be reviewed for that; an API
    cannot, so the rule is structural instead: the candidate is the path, the body has
    `extra="forbid"`, and no member of it is a list. A future change that added one would
    fail here before it reached a screen.
    """
    from medos.api.routes_curation import CurationDecisionRequest

    fields = CurationDecisionRequest.model_fields
    assert set(fields) == {"decision", "reason_code", "note", "review_seconds"}
    assert CurationDecisionRequest.model_config["extra"] == "forbid"
    for name, field in fields.items():
        rendered = str(field.annotation)
        assert "list" not in rendered.lower(), (name, rendered)
    # And `decided_by` is not among them: MOS-TRAIN-080's named human is the credential.
    assert "decided_by" not in fields


def test_the_stratification_row_cannot_satisfy_its_own_schema_and_here_is_where(
    operator: httpx.Client,
    wdb: psycopg.Connection[Any],
    archive: dict[str, Any],
    dataset: str,
    declared: Path,
) -> None:
    """THE DIVERGENCE `R5`'s `schema_divergence:` RECORDS IN
    `medos/api/v1/routes.train.yaml`.

    `medos/schemas/training/corpus-stratification-report-1.0.0.json` types every check's
    `observed` and `bound` as `number`; `medos/medos/evidence/stratification.py` emits an OBJECT
    for C4 and `null` for C5 where no thin-slice subset applies. The handler carries both
    through exactly as computed, because `MOS-TRAIN-088` asks for "the full result and not
    the verdict" and a reader cannot see a margin in a number invented to fit a type.

    This test asserts the response is schema-valid EXCEPT on those pointers. It is
    deliberately not a skip and deliberately not a coercion: widening the schema is
    chapter 10's and chapter 17's edit, and when someone makes it this turns red and they
    come here and delete it.
    """
    report = _sealed_report(operator, wdb, dataset, declared)
    problems = problems_against(report, "corpus-stratification-report")
    assert problems, (
        "R5's body now satisfies its own schema. If the schema was widened to admit an "
        "object `observed`/`bound`, delete this test and the `schema_divergence:` key on "
        "row R5 in medos/api/v1/routes.train.yaml"
    )
    offending = {p.split()[0] if p.split() else p for p in problems}
    assert all("C4" in p or "C5" in p for p in problems), (
        f"the divergence has moved beyond C4 and C5: {sorted(offending)}"
    )


def _sealed_report(
    operator: httpx.Client,
    wdb: psycopg.Connection[Any],
    dataset: str,
    declared: Path,
) -> dict[str, Any]:
    """Drive the walk as far as `R5`. Shared by one test; extracted so the walk above
    reads as one sequence rather than as a sequence with a helper in the middle."""
    policy_id = _ok(
        operator.put(
            f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy",
            json={
                "training_use_allowed": True,
                "policy": {
                    "legal_basis": "research_ethics_approval",
                    "basis_reference": "REC-2026-118",
                    "scope": {
                        "modalities": ["CT"], "body_parts": ["CHEST"],
                        "capabilities": [CAPABILITY],
                        "date_from": "2020-01-01", "date_to": None,
                    },
                    "permits_redistribution": True,
                },
            },
        ),
        200,
    )["policy"]["training_data_policy_id"]
    plan = _ok(
        operator.post(
            "/api/v1/sampling-plans",
            json={
                "capability_id": CAPABILITY,
                "plan_version": 1,
                "strata": {
                    "score_band": {"include": list(SCORE_BANDS)},
                    "review_outcome": {"include": ["ACCEPTED", "MODIFIED", "REJECTED"]},
                    "ran_on_platform": {"include": [True]},
                    "acquisition_bucket": {"include": ["<=2.0mm", "<=3.0mm"]},
                },
            },
        ),
        201,
    )
    batch = _ok(
        operator.post(
            "/api/v1/harvest-batches",
            json={
                "capability_id": CAPABILITY,
                "sampling_plan_id": plan["sampling_plan_id"],
                "training_data_policy_id": policy_id,
            },
        ),
        201,
    )
    batch_id = batch["harvest_batch_id"]
    page = _ok(
        operator.get(f"/api/v1/harvest-batches/{batch_id}/candidates?limit=200"), 200
    )
    for candidate in page["items"]:
        operator.post(
            f"/api/v1/harvest-candidates/{candidate['harvest_candidate_id']}/decision",
            json={"decision": "include", "review_seconds": 30},
        )
    run_id = _ok(
        operator.post(
            f"/api/v1/harvest-batches/{batch_id}/seal", json={"dataset_id": dataset}
        ),
        202,
    )["seal_run_id"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = _ok(operator.get(f"/api/v1/seal-runs/{run_id}"), 200)
        if state["state"] in seal_mod.TERMINAL_STATES:
            break
        time.sleep(0.2)
    return _ok(operator.get(f"/api/v1/harvest-batches/{batch_id}/stratification"), 200)


def test_a_draw_that_selects_nothing_leaves_no_batch_behind(
    operator: httpx.Client,
    wdb: psycopg.Connection[Any],
    archive: dict[str, Any],
    declared: Path,
) -> None:
    """`MOS-TRAIN-202`: the batch and its candidates exist together or neither does.

    THE DEFECT THIS WAS WRITTEN AGAINST, FOUND BY READING THE TRANSACTION BOUNDARY RATHER
    THAN BY A FAILING RUN. `medos.db.tenancy.tenant_tx` inherits
    `psycopg.Connection.transaction()`: "on a connection with no open transaction it
    BEGINs and COMMITs on exit". `open_batch` opens one of its own, so the first draft of
    `R1` -- which called `conn.rollback()` after discovering an empty draw -- was rolling
    back nothing at all. The operator would have been left with an empty `harvest_batches`
    row, `R22` would have listed it, and the remedy the refusal offers (widen the
    selection) would have produced a SECOND batch beside the first.

    The plan below asks for a `review_outcome` no case in the archive carries, so the
    strata select nothing and the refusal carries the per-reason counts. What is asserted
    is the absence: no batch, no candidate, and a refusal that names neither a UID nor a
    patient key.
    """
    policy_id = _ok(
        operator.put(
            f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy",
            json={
                "training_use_allowed": True,
                "policy": {
                    "legal_basis": "public_corpus_licence",
                    "basis_reference": "CC-BY-4.0",
                    "scope": {
                        "modalities": ["CT"], "body_parts": ["CHEST"],
                        "capabilities": [CAPABILITY],
                        "date_from": None, "date_to": None,
                    },
                    "permits_redistribution": True,
                },
            },
        ),
        200,
    )["policy"]["training_data_policy_id"]
    plan = _ok(
        operator.post(
            "/api/v1/sampling-plans",
            json={
                "capability_id": CAPABILITY,
                "plan_version": 1,
                "strata": {
                    "score_band": {"include": list(SCORE_BANDS)},
                    # Every seeded result is ACCEPTED, MODIFIED or REJECTED. Nothing is
                    # UNREVIEWED, so this facet admits no case the platform holds.
                    "review_outcome": {"include": ["UNREVIEWED"]},
                    "ran_on_platform": {"include": [True]},
                    "acquisition_bucket": {"include": ["<=2.0mm", "<=3.0mm"]},
                },
            },
        ),
        201,
    )
    before = wdb.execute("SELECT count(*) AS n FROM harvest_batches").fetchone()["n"]

    refused = operator.post(
        "/api/v1/harvest-batches",
        json={
            "capability_id": CAPABILITY,
            "sampling_plan_id": plan["sampling_plan_id"],
            "training_data_policy_id": policy_id,
        },
    )
    _transcribe(refused)
    assert refused.status_code == 422, refused.text
    document = refused.json()
    assert document["code"] == "CURATION_REFUSED"
    assert document["check_ids"] == ["MOS-TRAIN-083"]
    refusal = document["refusals"][0]
    assert refusal["code"] == "draw_selected_no_candidates"
    # The remedy is computed, not described (MOS-UI-107): counts per reason, and the
    # number of cases that were considered.
    assert refusal["bound"] == PATIENTS
    assert sum(refusal["observed"].values()) == PATIENTS

    # NO UID, NO PATIENT KEY, NO INSTITUTION NAME in the refusal an operator reads.
    _no_phi(refused.text, archive)
    assert "1.2.826" not in refused.text
    assert "pk_" not in refused.text

    # And nothing was created. This is the assertion the defect would have failed.
    wdb.rollback()
    assert wdb.execute("SELECT count(*) AS n FROM harvest_batches").fetchone()["n"] == before
    assert wdb.execute("SELECT count(*) AS n FROM harvest_candidates").fetchone()["n"] == 0


def test_the_five_new_rows_refuse_a_credential_without_their_permission(
    walk_url: str, pg_dsn: str, archive: dict[str, Any], declared: Path
) -> None:
    """A ceiling that does not carry the key answers `403` and NAMES the key.

    Driven with a credential holding every permission of both routers EXCEPT the one the
    row binds, so the refusal cannot come from anything else. `MOS-API-005` binds one
    permission per row and this is the half a registry entry cannot enforce.
    """
    from medos.security import store

    # Every body below is VALID. FastAPI validates the request model before the handler
    # runs, so a malformed body answers 400 `schema-violation` and the 403 this test is
    # about is never reached -- which is a real ordering property of this surface and not
    # a quirk of the test: a caller with no grant can learn the SHAPE of a body. It is the
    # same on every route of `medos/medos/api`, it is the framework's, and it is recorded here
    # rather than worked around, because the fix is a dependency that authorises before
    # the model binds and that is a change to all four routers.
    rows = [
        (
            "POST",
            "/api/v1/harvest-batches",
            "harvest_batch.open",
            {
                "capability_id": CAPABILITY,
                "sampling_plan_id": str(uuid.uuid4()),
                "training_data_policy_id": str(uuid.uuid4()),
            },
        ),
        (
            "GET",
            f"/api/v1/harvest-candidates/{uuid.uuid4()}/decision",
            "harvest_candidate.read",
            None,
        ),
        (
            "POST",
            f"/api/v1/harvest-candidates/{uuid.uuid4()}/decision",
            "curation_decision.record",
            {"decision": "include", "review_seconds": 10},
        ),
        (
            "PUT",
            f"/api/v1/tenants/{DEFAULT_TENANT_ID}/training-policy",
            "training_data_policy.record",
            {"training_use_allowed": False, "revocation_reason": "not this credential"},
        ),
        (
            "GET",
            f"/api/v1/harvest-batches/{uuid.uuid4()}/stratification",
            "corpus_stratification_report.read",
            None,
        ),
    ]
    full = set(CURATION_PERMISSIONS) | set(TRAINING_PERMISSIONS)
    for method, path, permission, body in rows:
        with psycopg.connect(pg_dsn, row_factory=dict_row) as conn:
            minted, _record = store.issue(
                conn,
                tenant_id=DEFAULT_TENANT_ID,
                principal_kind="service_account",
                principal_id=OPERATOR,
                created_by=store.BOOTSTRAP_OPERATOR_ID,
                expires_at=datetime.now(UTC) + timedelta(days=1),
                scope=sorted(full - {permission}),
                label=f"everything but {permission}",
                env="dev",
            )
            conn.commit()
        with httpx.Client(
            base_url=walk_url,
            timeout=60.0,
            headers={"Authorization": f"Bearer {minted.plaintext}"},
        ) as client:
            response = client.request(method, path, json=body)
        assert response.status_code == 403, (path, response.status_code, response.text)
        document = response.json()
        assert document["code"] == "PERMISSION_DENIED"
        assert document["required_permission"] == permission, path
