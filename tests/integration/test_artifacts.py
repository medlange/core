# SPDX-License-Identifier: Apache-2.0
"""The Artifact registry, proved against a real database. Chapter 6 §§6.3-6.5, 6.9, 6.11.

`docs/spec/15-delivery.md` §15.2.6 is one sentence long and every test here is a clause of
it: ONE `artifacts` table (`test_there_is_one_table_and_no_parallel_surface`), with
per-kind JSON-Schema'd manifests (`test_a_manifest_that_fails_its_schema_is_refused`),
served on the existing API paths (`test_the_api_paths_are_chapter_tens`).

Four invariants are asserted TWICE, once through the repository and once in raw SQL as a
role that is not the owner, because the two catch different failures: the Python check is
the good error message and the database check is the guarantee. An operator with a psql
prompt is inside the second and outside the first.

Spec: MOS-REG-010, MOS-REG-013, MOS-REG-014, MOS-REG-015, MOS-REG-016, MOS-REG-017,
MOS-REG-018, MOS-REG-019, MOS-REG-020, MOS-REG-021, MOS-REG-022, MOS-REG-025,
MOS-REG-026, MOS-REG-029, MOS-REG-039, MOS-REG-087, MOS-REG-090, MOS-REG-103,
MOS-REG-107, MOS-REG-109, MOS-STORE-228, MOS-STORE-253, MOS-STORE-254.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from medos.api.app import create_app
from medos.db import audit
from medos.db.migrate import MIGRATIONS_DIR, apply_migrations, discover
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.registry import lifecycle, repo
from medos.registry.digest import content_digest_of
from medos.registry.errors import (
    ArtifactNotFound,
    InvalidManifest,
    LifecycleViolation,
    SupplyChainRefusal,
    UnresolvedReference,
    VersionConflict,
)
from medos.registry.schemas import SCHEMA_VERSION, manifest_errors, schema_rows
from psycopg.rows import dict_row

from tests._support.capability_source import (
    VENDOR_CAPABILITY_ID,
    serve_one_extra_capability,
)
from tests._support.skips import skip_infra
from tests.integration.conftest import ADMIN_URL, APP_PASSWORD

pytestmark = pytest.mark.slow

ACTOR = audit.Actor(kind="service_account", id="11111111-1111-1111-1111-111111111111",
                    auth="api_key")
TRACE = "0af7651916cd43dd8448eb211c80319c"
SIGNER = "https://github.com/pulmoai/pleural-effusion/.github/workflows/release.yml@refs/tags/v3.2.1"

# The supply-chain material `MOS-REG-087` requires from 0.3.0. Every publish in this file
# passes it, because `MOS-REG-090` makes its absence a refusal and there is no flag that
# turns the refusal off -- which is itself asserted, in `test_supply_chain_fails_closed`.
SUPPLY_CHAIN: dict[str, Any] = {
    "oci_image_digest": "sha256:" + "a1" * 32,
    "signature": b"cosign-bundle-bytes",
    "signature_alg": "cosign-sigstore",
    "signer_identity": SIGNER,
    "sbom_object_key": "sbom/pulmo-effusion-3.2.1.cdx.json",
}


# =====================================================================================
# Fixtures
# =====================================================================================
def _manifest(kind: str, family: str, version: str, **spec_overrides: Any) -> dict[str, Any]:
    """A valid manifest for either kind, with the digest `MOS-REG-017` computes."""
    if kind == "model_version":
        spec: dict[str, Any] = {
            "capabilities": ["pleural_effusion"],
            "weights_availability": "platform_managed",
            "weights": {
                "oci_ref": "ghcr.io/pulmoai/effusion-unet",
                "digest": "sha256:" + "5e" * 32,
                "format": "onnx",
                "size_bytes": 184236032,
            },
            "preprocessing_spec_ref": "ps_pulmo_effusion_prep_2_0_0",
            # MOS-IMG-049 and known-inconsistency 2: the `sha256:` prefix. See
            # medos/medos/registry/schemas.py for why chapter 4 wins over MOS-REG-036 here.
            "golden_fixture": {
                "sha256": "sha256:" + "0c" * 32,
                "output_tensor_sha256": "sha256:" + "b9" * 32,
                "output_shape": [1, 2, 128, 192, 192],
            },
            "io": {
                "input": {"name": "input", "shape": [1, 1, 128, 192, 192],
                          "dtype": "float32", "layout": "NCZYX", "orientation": "LPS",
                          "value_range": [-1.0, 1.0]},
                "output": {"name": "logits", "shape": [1, 2, 128, 192, 192],
                           "dtype": "float32", "layout": "NCZYX",
                           "kind": "segmentation_logits",
                           "label_map": {"0": "background", "1": "pleural_effusion"}},
            },
            "operating_point": {
                "kind": "probability_threshold", "score_threshold": 0.45,
                "selected_on_evaluation_run": "er_01JP4T9X7B",
                "selection_rule": "max F1 on the sealed validation split",
            },
            "runtime": {"engine": "triton", "engine_version": ">=24.08 <25.00",
                        "backend": "onnxruntime",
                        "gpu_architectures": ["sm_80", "sm_86"], "cuda": ">=12.1 <13",
                        "driver_min": "535.104.05", "gpu_memory_mib": 9216},
            "derived_from": None,
            "evaluation_run_id": "er_01JP4T9X7B",
            "applicability_envelope_ref": "ae_chest_ct_v2",
            "not_validated_for": ["studies with slice thickness > 3.0 mm"],
            "known_failure_modes": ["loculated effusions are under-segmented"],
        }
    else:
        spec = {
            "mode": "native",
            "image": {"ref": "ghcr.io/pulmoai/pleural-effusion",
                      "digest": "sha256:" + "a1" * 32},
            "capabilities": [{"id": "pleural_effusion",
                              "outputs": ["segmentation", "measurement"]}],
            "modalities": ["CT"],
            "series_selector_ref": "sel_chest_ct_thin_axial_v3",
            "models": [{"ref": "mv_pulmo_effusion_unet_3_2_1", "role": "primary"}],
            "preprocessing_specs": [{"ref": "ps_pulmo_effusion_prep_2_0_0"}],
            "resources": {"gpu_required": True, "gpu_memory_mib": 11264,
                          "cpu_millicores": 4000, "memory_mib": 24576,
                          "max_concurrent_jobs": 2},
            "engineering_acceptance": {"p95_wall_clock_seconds": 180,
                                       "max_gpu_memory_mib": 11264,
                                       "result_bundle_schema_validity": 1.0},
            "legal_manufacturer": {"id": "lm_pulmoai_gmbh", "name": "PulmoAI GmbH",
                                   "device_serial_number": "PULMO-EFF-0003",
                                   "software_versions": version},
            "regulatory_status": [{"jurisdiction": "EU",
                                   "status": "not_a_medical_device",
                                   "evidence_ref": None}],
            "compatibility": {"medicalos_api": ">=1.0 <2", "service_contract": "1.2"},
        }
    spec.update(spec_overrides)
    manifest = {
        "schema_version": "1.0.0",
        "kind": kind,
        "family": family,
        "version": version,
        "publisher": {"org_id": "org_pulmoai", "signing_identity": SIGNER},
        "created_at": "2026-01-09T11:02:41Z",
        "spec": spec,
    }
    manifest["content_digest"] = content_digest_of(manifest)
    return manifest


@pytest.fixture()
def db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection with the default tenant bound and the registry emptied.

    Deliberately NOT the shared `db` fixture of conftest: that one truncates the job
    tables, and these tests want the registry empty and the job tables untouched.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    # CASCADE from 0.3.0: `training_runs.candidate_model_version_id` and
    # `conversion_runs.source_model_version_id` reference `artifacts(id)` (migration
    # 0013, MOS-TRAIN-138 and MOS-TRAIN-156), and Postgres refuses to truncate a table a
    # foreign key points at without it. Nothing in this file reads either table, so the
    # cascade empties two tables these tests never populate.
    conn.execute("TRUNCATE artifacts, registry_changelog CASCADE")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant

        reset_current_tenant(token)
        conn.close()


def _dsn_as(dsn: str, role: str, password: str) -> str:
    tail = dsn.split("://", 1)[-1].split("@", 1)[-1]
    return f"postgresql://{role}:{password}@{tail}"


@pytest.fixture()
def app_conn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection AS `medicalos_app` -- NOBYPASSRLS, holding only grants.

    Every proof about immutability and row security has to be made as this role: the
    fixture connection above is the bootstrap superuser, which bypasses row-level security
    entirely, so an assertion made on it would prove nothing.
    """
    # `autocommit=True` so the session-level tenant binding below is not inside a
    # transaction: every refusal these tests provoke ends in a ROLLBACK, and a binding set
    # inside the rolled-back transaction would go with it.
    conn = psycopg.connect(
        _dsn_as(pg_dsn, "medicalos_app", APP_PASSWORD), row_factory=dict_row,
        autocommit=True,
    )
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    try:
        yield conn
    finally:
        conn.close()


def _publish_model(conn: psycopg.Connection[Any], *, version: str = "3.2.1",
                   public_id: str = "mv_pulmo_effusion_unet_3_2_1",
                   status: str = "APPROVED", run_id: str = "er_01JP4T9X7B",
                   **overrides: Any) -> dict[str, Any]:
    manifest = _manifest("model_version", "pulmo.effusion-unet", version,
                         evaluation_run_id=run_id, **overrides)
    row, _created = repo.publish(conn, manifest=manifest, public_id=public_id,
                                 actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
    conn.commit()
    if status != "REGISTERED":
        for step in ("VALIDATING", "VALIDATED", "APPROVED"):
            row = repo.set_status(conn, public_id, to_status=step, reason=None,
                                  actor=ACTOR, trace_id=TRACE)
            if step == status:
                break
        conn.commit()
    return row


# =====================================================================================
# 1. The migration, from empty
# =====================================================================================
@pytest.fixture()
def empty_dsn() -> Iterator[str]:
    """A brand-new database of this module's own, for the from-empty proof.

    NOT conftest's `pristine_dsn`: that fixture is session-scoped and
    `test_queue.py::test_schema_applies_cleanly_from_empty` asserts the database it hands
    over is EMPTY. Applying the schema into the shared one makes that test fail in a later
    module with an error that names neither test -- which is exactly the cross-test
    coupling a throwaway database exists to prevent.
    """
    admin_url = ADMIN_URL
    name = f"medos_reg_{secrets.token_hex(6)}"
    # GUARDED, because an unguarded connect here ESCAPES THE SKIP TAXONOMY. Without this
    # the fixture raises `psycopg.OperationalError` on a machine with no Postgres, pytest
    # reports it as an ERROR rather than a classified skip, and the run's own summary --
    # "INFRA SKIPS: 455 (postgres)" -- does not account for it. One unexplained error
    # beside four hundred explained skips is exactly the state this directory's taxonomy
    # exists to make impossible, and it reads as a real defect to whoever sees it next.
    # Under --require-stack this still FAILS, which is the point: it is not a way out.
    try:
        admin = psycopg.connect(admin_url, autocommit=True)
    except psycopg.OperationalError as exc:
        skip_infra(f"{admin_url.rsplit('@', 1)[-1]}: {exc}", dependency="postgres")
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    head, _, _tail = admin_url.rpartition("/")
    try:
        yield f"{head}/{name}"
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def test_0012_applies_from_empty_and_is_recorded(empty_dsn: str) -> None:
    """Baseline plus every migration in order, onto a database that had nothing.

    `MOS-STORE-214`: "every migration set begins at the bootstrap DDL". The ledger is
    asserted rather than the tables, because a migration that ran without recording itself
    is the failure that makes the next deployment apply it twice.
    """
    from medos.db.conn import apply_schema

    with psycopg.connect(empty_dsn, autocommit=True, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone()["n"] == 0, "the fixture database is not empty"
        apply_schema(conn)
        ledger = [r["version"] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        assert "0012_artifacts" in ledger
        assert ledger == sorted(ledger)
        # The property this assertion is about is that 0012 is allocated to exactly ONE
        # component, not that it is the last number in the tree. Release 0.3.0's brief
        # pre-allocates 0012_artifacts, 0013_training and 0014_outbox to three different
        # components, so a later number is expected rather than a collision; what would
        # be a collision is two files claiming 0012, and that is what is checked.
        assert ledger.count("0012_artifacts") == 1
        artifacts_files = sorted(
            p.name for p in MIGRATIONS_DIR.glob("0012_*.up.sql")
        )
        assert artifacts_files == ["0012_artifacts.up.sql"], (
            f"two agents allocated migration 0012: {artifacts_files}"
        )
        # Applying twice is a no-op: `apply_migrations` skips what the ledger holds.
        assert apply_migrations(conn) == []
        tables = {r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'").fetchall()}
        assert {"artifacts", "artifact_manifest_schemas", "registry_changelog"} <= tables


def test_there_is_one_table_and_no_parallel_surface(db: psycopg.Connection[Any]) -> None:
    """§15.2.6: ONE `artifacts` table, and no per-kind registry beside it.

    `MOS-STORE-253`: "There is no per-kind artifact table." The check is a query over the
    catalogue rather than a reviewer's memory, because the way this invariant dies is a
    later migration adding `model_versions` for one convenient join.
    """
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_name IN ('service_versions','model_versions','preprocessing_specs',"
        "'workflow_versions','tool_versions','agent_versions','artifact_family')"
    ).fetchall()
    assert rows == [], f"per-kind artifact tables exist: {[r['table_name'] for r in rows]}"

    app = create_app(connect=lambda **kw: None, configure_logs=False)
    paths = {getattr(r, "path", "") for r in app.routes}
    assert not [p for p in paths if p.startswith("/api/v1/artifacts")], (
        "§15.2.6 serves the registry on the EXISTING API paths; a /api/v1/artifacts "
        "surface is the parallel one it forbids"
    )


def test_the_api_paths_are_chapter_tens() -> None:
    """Rows 14-18 and 28-31 of table 10.2-B, plus §6.11's status and impact routes."""
    app = create_app(connect=lambda **kw: None, configure_logs=False)
    seen = {(getattr(r, "path", ""), m)
            for r in app.routes for m in getattr(r, "methods", ())}
    for path in ("/api/v1/service-versions", "/api/v1/model-versions"):
        assert (path, "GET") in seen and (path, "POST") in seen
    # THE PARAMETER IS NAMED PER KIND, and this loop shared one spelling across both.
    # `{artifact_id}` was renamed to `{service_version_id}` and `{model_version_id}` in the
    # handlers and in medos/api/v1/routes.core.yaml; the test kept the old spelling and asserted
    # a path that has not existed since. It never failed, because this module could not run
    # until the interpreter was corrected -- see f178c18.
    for path in ("/api/v1/service-versions/{service_version_id}",
                 "/api/v1/model-versions/{model_version_id}"):
        assert (path, "GET") in seen
        assert (path, "PATCH") in seen            # §6.11: exists, and answers 405
        assert (f"{path}/manifest", "GET") in seen
        assert (f"{path}/attestation", "GET") in seen
        assert (f"{path}/impact", "GET") in seen  # MOS-REG-040
        assert (f"{path}/status", "POST") in seen


def test_seeded_schemas_are_the_in_repo_source(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-016`: one source, and the database carries exactly what it generates.

    The digest is compared, not the text: a seed that has been hand-edited in the
    migration -- the way two statements of one schema always drift -- changes the digest
    and fails here.
    """
    expected = {r["kind"]: r for r in schema_rows()}
    rows = db.execute(
        "SELECT kind, schema_version, json_schema, schema_digest, introduced_in "
        "FROM artifact_manifest_schemas ORDER BY kind").fetchall()
    assert {r["kind"] for r in rows} == set(expected)
    for row in rows:
        want = expected[row["kind"]]
        assert row["schema_digest"] == want["schema_digest"], row["kind"]
        assert row["json_schema"] == json.loads(json.dumps(want["json_schema"]))
        assert row["schema_version"] == SCHEMA_VERSION


def test_a_kind_with_no_seeded_schema_cannot_be_registered(
    db: psycopg.Connection[Any]
) -> None:
    """§15.2.6: no `WorkflowVersion` is backed by a table before 0.4.0.

    Enforced by the schema foreign key rather than by a code review: with no row in
    `artifact_manifest_schemas` for the kind, the insert cannot resolve
    `(kind, manifest_schema_version)` and is refused by the database.
    """
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO artifacts (public_id, tenant_id, kind, family, version, "
            "version_major, version_minor, version_patch, lifecycle_status, "
            "published_by, manifest_schema_version, manifest, manifest_digest) VALUES "
            "('wv_triage_flow', %s, 'workflow', 'x.y', '1.0.0', 1, 0, 0, 'REGISTERED', 'me', "
            "'1.0.0', '{}'::jsonb, %s)",
            (DEFAULT_TENANT_ID, "sha256:" + "ff" * 32),
        )
    db.rollback()


# =====================================================================================
# 2. Manifest validation -- MOS-REG-013, MOS-REG-014, MOS-REG-015, MOS-REG-017
# =====================================================================================
@pytest.mark.parametrize(
    ("mutation", "expect"),
    [
        ({"not_validated_for": []}, "minItems"),                      # MOS-REG-032
        ({"known_failure_modes": []}, "minItems"),                    # MOS-REG-032
        ({"unknown_member": 1}, "additionalProperties"),              # MOS-REG-015
        ({"weights_availability": "vendor_sealed"}, "type"),          # MOS-REG-039
        ({"golden_fixture": {"sha256": "0c" * 32,
                             "output_tensor_sha256": "b9" * 32}}, "pattern"),  # MOS-IMG-049
    ],
)
def test_a_manifest_that_fails_its_schema_is_refused(
    db: psycopg.Connection[Any], mutation: dict[str, Any], expect: str
) -> None:
    """`MOS-REG-014`: validation failure is a HARD reject, and nothing is written.

    The row count is asserted afterwards, because "store and validate later" is the exact
    path the requirement closes and a refusal that still wrote a row would pass a test
    that only looked at the exception.
    """
    manifest = _manifest("model_version", "pulmo.effusion-unet", "3.2.1", **mutation)
    assert any(expect in v for v in manifest_errors(manifest))
    with pytest.raises(InvalidManifest) as exc:
        repo.publish(db, manifest=manifest, public_id="mv_x", actor=ACTOR,
                     trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert exc.value.status == 422
    assert db.execute("SELECT count(*) AS c FROM artifacts").fetchone()["c"] == 0


def test_an_unknown_kind_is_refused(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-013`: the discriminator's enum is closed."""
    manifest = _manifest("model_version", "pulmo.effusion-unet", "3.2.1")
    manifest["kind"] = "tool_version"
    with pytest.raises(InvalidManifest):
        repo.publish(db, manifest=manifest, public_id="mv_x", actor=ACTOR,
                     trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()


def test_content_digest_is_computed_and_a_supplied_mismatch_is_refused(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-017`: computed over RFC 8785 without the digest field; never accepted."""
    manifest = _manifest("model_version", "pulmo.effusion-unet", "3.2.1")
    assert manifest["content_digest"] == content_digest_of(manifest)
    # The digest is over the manifest WITHOUT its own digest member, so re-computing on
    # the stored manifest (which carries it) reproduces the same value.
    row = _publish_model(db, status="REGISTERED")
    assert row["manifest_digest"] == manifest["content_digest"]
    assert content_digest_of(row["manifest"]) == row["manifest_digest"]

    lying = _manifest("model_version", "pulmo.other-unet", "1.0.0",
                      evaluation_run_id="er_01JP4T9X7C")
    lying["content_digest"] = "sha256:" + "00" * 32
    with pytest.raises(InvalidManifest) as exc:
        repo.publish(db, manifest=lying, public_id="mv_other", actor=ACTOR,
                     trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert exc.value.extensions["computed_digest"] != exc.value.extensions["supplied_digest"]


def test_republish_identical_is_a_noop_and_different_is_409(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-019`, both halves, and the digest in the conflict document."""
    first = _publish_model(db, status="REGISTERED")
    manifest = _manifest("model_version", "pulmo.effusion-unet", "3.2.1")

    again, created = repo.publish(db, manifest=manifest,
                                  public_id="mv_pulmo_effusion_unet_3_2_1", actor=ACTOR,
                                  trace_id=TRACE, **SUPPLY_CHAIN)
    assert created is False and again["id"] == first["id"]

    changed = _manifest("model_version", "pulmo.effusion-unet", "3.2.1",
                        known_failure_modes=["one changed byte"])
    with pytest.raises(VersionConflict) as exc:
        repo.publish(db, manifest=changed, public_id="mv_pulmo_effusion_unet_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert exc.value.status == 409
    assert exc.value.extensions["existing_content_digest"] == first["manifest_digest"]


# =====================================================================================
# 3. Immutability -- MOS-REG-018, MOS-STORE-228, MOS-STORE-254
# =====================================================================================
def test_an_immutable_column_cannot_be_updated_by_the_app_role(
    db: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-018`, at the GRANT level. `medicalos_app` is what the API connects as."""
    _publish_model(db, status="REGISTERED")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE artifacts SET manifest = '{}'::jsonb")
    app_conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("UPDATE artifacts SET version = '9.9.9'")
    app_conn.rollback()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app_conn.execute("DELETE FROM artifacts")
    app_conn.rollback()
    # The two mutable columns ARE writable, which is what makes the revocation a
    # column-level one rather than a table-level one (MOS-STORE-228). Rolled back
    # explicitly so the proof costs the next test nothing.
    with app_conn.transaction():
        app_conn.execute(
            "UPDATE artifacts SET lifecycle_status = 'SUSPENDED', status_reason = 'x'")
        raise psycopg.Rollback()


def test_an_immutable_column_cannot_be_updated_by_the_owner_either(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-STORE-254`: the trigger binds every role, including a superuser session.

    This is the case a GRANT cannot reach and the one that actually rewrites a published
    manifest -- the connection here IS the bootstrap superuser.
    """
    _publish_model(db, status="REGISTERED")
    # `psycopg.DatabaseError` and not a named subclass: the SQLSTATEs these triggers raise
    # are the platform's own `MOS05` / `MOS07` (schema.sql's convention), which psycopg has
    # no mapping for. The message is asserted instead, so a DIFFERENT refusal would fail.
    for statement in (
        "UPDATE artifacts SET manifest = '{\"x\": 1}'::jsonb",
        "UPDATE artifacts SET manifest_digest = 'sha256:" + "00" * 32 + "'",
        "UPDATE artifacts SET version = '9.9.9'",
        "UPDATE artifacts SET oci_image_digest = NULL",
        "UPDATE artifacts SET tenant_id = NULL",
        "DELETE FROM artifacts",
    ):
        with pytest.raises(psycopg.DatabaseError) as exc:
            db.execute(statement)
        db.rollback()
        assert "immutable" in str(exc.value)


def test_updating_only_the_status_is_permitted_and_touches_updated_at(
    db: psycopg.Connection[Any]
) -> None:
    """Only `lifecycle_status` and `status_reason` are mutable (`MOS-REG-018`)."""
    row = _publish_model(db, status="REGISTERED")
    before = db.execute("SELECT updated_at FROM artifacts WHERE id = %s",
                        (row["id"],)).fetchone()["updated_at"]
    db.execute("UPDATE artifacts SET lifecycle_status = 'VALIDATING' WHERE id = %s",
               (row["id"],))
    after = db.execute("SELECT lifecycle_status, updated_at FROM artifacts WHERE id = %s",
                       (row["id"],)).fetchone()
    db.commit()
    assert after["lifecycle_status"] == "VALIDATING"
    assert after["updated_at"] >= before


# =====================================================================================
# 4. The lifecycle graph -- MOS-REG-020, MOS-REG-021, MOS-REG-022
# =====================================================================================
def test_python_and_sql_agree_on_the_lifecycle_graph(db: psycopg.Connection[Any]) -> None:
    """Two statements of one graph, compared edge for edge.

    `medos/medos/registry/lifecycle.py` is the error message and the trigger is the guarantee.
    The duplication is deliberate and this test is what stops it becoming drift: the SQL
    edges are read back out of the function body rather than re-typed here.
    """
    body = db.execute(
        "SELECT pg_get_functiondef(oid) AS src FROM pg_proc "
        "WHERE proname = 'artifacts_lifecycle_guard'").fetchone()["src"]
    sql_edges = set(re.findall(r"'([A-Z]+)>([A-Z]+)'", body))
    python_edges = {
        (frm, to)
        for frm, tos in lifecycle.TRANSITIONS.items()
        for to in tos
        # The SUSPENDED exits other than RECALLED are not fixed-head edges: the head is
        # whatever status the row held before, which the trigger reads from the changelog.
        if not (frm == "SUSPENDED" and to != "RECALLED")
    }
    assert sql_edges == python_edges


def test_recall_is_irreversible(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-022`, in the repository AND in the database."""
    _publish_model(db, status="APPROVED")
    repo.set_status(db, "mv_pulmo_effusion_unet_3_2_1", to_status="RECALLED",
                    reason="weights defect", actor=ACTOR, trace_id=TRACE)
    db.commit()

    with pytest.raises(LifecycleViolation) as exc:
        repo.set_status(db, "mv_pulmo_effusion_unet_3_2_1", to_status="APPROVED",
                        reason="we fixed it", actor=ACTOR, trace_id=TRACE)
    db.rollback()
    assert exc.value.extensions["reason_code"] == "recall_is_irreversible"
    assert exc.value.status == 409

    with pytest.raises(psycopg.DatabaseError) as raised:
        db.execute("UPDATE artifacts SET lifecycle_status = 'APPROVED' "
                   "WHERE public_id = 'mv_pulmo_effusion_unet_3_2_1'")
    db.rollback()
    assert "irreversible" in str(raised.value)


def test_suspension_is_reversible_and_restores_the_previous_status(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-021`: "SUSPENDED -> previous status ... requires `status_reason`".

    The previous status is read from `registry_changelog`, which is the only record of it
    -- §12.9 provides no column. Restoring the WRONG status is refused, which is the point:
    a suspended REGISTERED version must not come back APPROVED.
    """
    _publish_model(db, status="APPROVED")
    repo.set_status(db, "mv_pulmo_effusion_unet_3_2_1", to_status="SUSPENDED",
                    reason="self-test regression on sm_89", actor=ACTOR, trace_id=TRACE)
    db.commit()

    with pytest.raises(LifecycleViolation) as exc:
        repo.set_status(db, "mv_pulmo_effusion_unet_3_2_1", to_status="VALIDATED",
                        reason="partially fixed", actor=ACTOR, trace_id=TRACE)
    db.rollback()
    assert exc.value.extensions["reason_code"] == "suspension_restores_previous_status_only"

    restored = repo.set_status(db, "mv_pulmo_effusion_unet_3_2_1", to_status="APPROVED",
                               reason="driver 550 rolled back on the sm_89 nodes",
                               actor=ACTOR, trace_id=TRACE)
    db.commit()
    assert restored["lifecycle_status"] == "APPROVED"
    assert "sm_89" in restored["status_reason"]


def test_a_status_outside_the_kinds_set_is_refused(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-020`, and `MOS-REG-038`: no `STAGING`, `DEPLOYED` or `PRODUCTION`."""
    row = _publish_model(db, status="REGISTERED")
    for bad in ("STAGING", "DEPLOYED", "PRODUCTION", "SEALED"):
        with pytest.raises(LifecycleViolation):
            repo.set_status(db, row["public_id"], to_status=bad, reason=None,
                            actor=ACTOR, trace_id=TRACE)
        db.rollback()
    # The lifecycle trigger fires before the CHECK does, so the raw UPDATE is refused by
    # the graph rather than by the constraint. Both are refusals; what acceptance check 11
    # actually asks is that the three Deployment words appear in NO artifact status set,
    # which is asserted against the constraint's own definition.
    with pytest.raises(psycopg.DatabaseError):
        db.execute("UPDATE artifacts SET lifecycle_status = 'PRODUCTION' WHERE id = %s",
                   (row["id"],))
    db.rollback()
    definition = db.execute(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE conname = 'artifacts_lifecycle_status_per_kind'").fetchone()["def"]
    for forbidden in ("STAGING", "DEPLOYED", "PRODUCTION"):
        assert forbidden not in definition, (
            f"MOS-REG-038: {forbidden} is a Deployment concept, never a lifecycle status"
        )


def test_a_suspension_without_a_reason_is_refused(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-021`: a suspended version with no reason is an outage nobody can lift."""
    row = _publish_model(db, status="APPROVED")
    with pytest.raises(LifecycleViolation):
        repo.set_status(db, row["public_id"], to_status="SUSPENDED", reason="  ",
                        actor=ACTOR, trace_id=TRACE)
    db.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE artifacts SET lifecycle_status = 'SUSPENDED' WHERE id = %s",
                   (row["id"],))
    db.rollback()


# =====================================================================================
# 5. Cross-row rules -- MOS-REG-025, MOS-REG-026, MOS-REG-029, MOS-REG-103
# =====================================================================================
def test_a_dangling_model_ref_is_refused_at_publish(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-025`: "rejected at publish, not at dispatch"."""
    manifest = _manifest("service_version", "pulmo.pleural-effusion", "3.2.1")
    with pytest.raises(UnresolvedReference) as exc:
        repo.publish(db, manifest=manifest, public_id="sv_pulmo_effusion_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert exc.value.extensions["unresolved_ref"] == "mv_pulmo_effusion_unet_3_2_1"


def test_a_referenced_model_must_be_validated_or_approved(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-025`: the referenced version is past evaluation, or the publish fails."""
    _publish_model(db, status="REGISTERED")
    manifest = _manifest("service_version", "pulmo.pleural-effusion", "3.2.1")
    with pytest.raises(UnresolvedReference) as exc:
        repo.publish(db, manifest=manifest, public_id="sv_pulmo_effusion_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert exc.value.extensions["lifecycle_status"] == "REGISTERED"

    _publish_model(db, status="APPROVED")  # walks REGISTERED -> VALIDATING -> ... -> APPROVED
    row, created = repo.publish(db, manifest=manifest,
                                public_id="sv_pulmo_effusion_3_2_1", actor=ACTOR,
                                trace_id=TRACE, **SUPPLY_CHAIN)
    db.commit()
    assert created and row["kind"] == "service"


def test_a_sealed_service_still_declares_one_primary_model(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-026`: without it, the sealed service's evidence has nothing to bind to."""
    _publish_model(db, status="APPROVED")
    manifest = _manifest("service_version", "pulmo.sealed-effusion", "1.0.0",
                         mode="sealed",
                         models=[{"ref": "mv_pulmo_effusion_unet_3_2_1",
                                  "role": "dependency"}])
    with pytest.raises(UnresolvedReference) as exc:
        repo.publish(db, manifest=manifest, public_id="sv_sealed", actor=ACTOR,
                     trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert "MOS-REG-026" in exc.value.detail


def test_an_unknown_capability_is_refused(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-029`: a claimed capability MUST exist (see repo.py on the narrowing)."""
    _publish_model(db, status="APPROVED")
    manifest = _manifest("service_version", "pulmo.pleural-effusion", "3.2.1",
                         capabilities=[{"id": "invented_capability",
                                        "outputs": ["segmentation"]}])
    with pytest.raises(UnresolvedReference) as exc:
        repo.publish(db, manifest=manifest, public_id="sv_pulmo_effusion_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert exc.value.extensions["unknown_capabilities"] == ["invented_capability"]


def test_a_capability_this_deployment_serves_can_be_published_against(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`MOS-REG-029`'s "exists" is THIS DEPLOYMENT's capability set, not the platform's.

    THE DEFECT. This check used to read `medos.capabilities.REGISTRY`, the module
    singleton holding the three capabilities compiled into the platform. A deployment that
    configured a fourth through `MEDOS_CAPABILITY_PROVIDERS` -- one the API admits at
    submit and the worker executes -- could therefore never publish a `ServiceVersion` for
    it. Not a cosmetic gap: the capability produces `Result` rows whose provenance names a
    service version, and the registry refused to hold the row those Results point at.

    The test above (`test_an_unknown_capability_is_refused`) is the other half of this
    one. Together they say that the boundary is the deployment's set and that it is still
    a boundary -- an id nothing serves is refused, an id this deployment serves is not.

    See `tests/_support/capability_source.py` for why the resolver is substituted rather
    than configured here, and `tests/integration/test_capability_providers.py` for the
    proof that the configuration reaches it.
    """
    served = serve_one_extra_capability(monkeypatch)
    _publish_model(db, status="APPROVED")
    manifest = _manifest("service_version", "pulmo.pleural-effusion", "3.2.1",
                         capabilities=[{"id": VENDOR_CAPABILITY_ID,
                                        "outputs": ["detection"]}])

    row, created = repo.publish(db, manifest=manifest,
                                public_id="sv_pulmo_effusion_3_2_1", actor=ACTOR,
                                trace_id=TRACE, **SUPPLY_CHAIN)
    db.commit()

    assert created
    assert row["manifest"]["spec"]["capabilities"][0]["id"] == VENDOR_CAPABILITY_ID
    # The platform singleton is not what admitted it, and still does not contain it.
    from medos.capabilities import REGISTRY

    assert VENDOR_CAPABILITY_ID not in REGISTRY
    assert VENDOR_CAPABILITY_ID in served


def test_the_capability_refusal_is_raised_before_anything_is_written(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-029` refuses inside the publish scope, so the scope writes nothing.

    The capability vocabulary is resolved BEFORE the transaction opens (see `publish()`),
    which is what keeps a `CapabilityProviderError` -- a deployment defect carrying no
    HTTP status, raised by a resolver that may touch the filesystem -- out of a scope
    whose every other refusal is a `RegistryError`. This asserts the consequence that
    matters to a reader of the `artifacts` table: a refused publish leaves no row, no
    changelog entry and no audit event, whichever check did the refusing.
    """

    def counts() -> tuple[int, ...]:
        # Deltas rather than absolutes: `audit_events` is append-only (MOS-SEC-149) and
        # the `db` fixture does not truncate it, so an absolute count here would depend on
        # which other tests ran first in this session.
        return tuple(
            db.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            for table in ("artifacts", "registry_changelog", "audit_events")
        )

    _publish_model(db, status="APPROVED")
    before = counts()
    manifest = _manifest("service_version", "pulmo.pleural-effusion", "3.2.1",
                         capabilities=[{"id": "invented_capability",
                                        "outputs": ["segmentation"]}])
    with pytest.raises(UnresolvedReference):
        repo.publish(db, manifest=manifest, public_id="sv_pulmo_effusion_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()

    # The row, its changelog entry (MOS-REG-010's trigger) and its AuditEvent commit
    # together or not at all. Not at all.
    assert counts() == before


def test_an_evaluation_run_cannot_be_cited_by_two_model_versions(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-103`: a converted model reusing its source's metrics is refused."""
    _publish_model(db, status="REGISTERED")
    converted = _manifest("model_version", "pulmo.effusion-unet-trt", "3.2.1",
                          derived_from="mv_pulmo_effusion_unet_3_2_1")
    with pytest.raises(UnresolvedReference) as exc:
        repo.publish(db, manifest=converted, public_id="mv_pulmo_effusion_trt_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
    db.rollback()
    assert exc.value.extensions["cited_by"] == "mv_pulmo_effusion_unet_3_2_1"


def test_supply_chain_fails_closed(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-087`/`MOS-REG-090`: registry admission, with no way to turn it off."""
    manifest = _manifest("model_version", "pulmo.effusion-unet", "3.2.1")
    with pytest.raises(SupplyChainRefusal) as exc:
        repo.publish(db, manifest=manifest, public_id="mv_pulmo_effusion_unet_3_2_1",
                     actor=ACTOR, trace_id=TRACE)
    db.rollback()
    assert set(exc.value.extensions["missing"]) == {
        "oci_image_digest", "signature", "signature_alg", "signer_identity",
        "sbom_object_key",
    }

    # MOS-REG-028's checkable half: the identity that signed is the identity the manifest
    # claims. (The binding of that identity to `legal_manufacturer.id` has no storage in
    # this schema; see repo.py and this component's report.)
    wrong = dict(SUPPLY_CHAIN, signer_identity="https://example.invalid/someone-else")
    with pytest.raises(SupplyChainRefusal):
        repo.publish(db, manifest=manifest, public_id="mv_pulmo_effusion_unet_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **wrong)
    db.rollback()

    # MOS-REG-021: a DRAFT is the one row that may exist before the signature, because
    # the signature is verified on DRAFT -> REGISTERED.
    row, _created = repo.publish(db, manifest=manifest,
                                 public_id="mv_pulmo_effusion_unet_3_2_1",
                                 actor=ACTOR, trace_id=TRACE, lifecycle_status="DRAFT")
    db.commit()
    assert row["lifecycle_status"] == "DRAFT"


# =====================================================================================
# 6. The changelog and the epoch -- MOS-REG-010, MOS-REG-011
# =====================================================================================
def test_every_artifact_write_appends_to_the_changelog(
    db: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-010`: in the SAME transaction, with a monotonic epoch."""
    row = _publish_model(db, status="REGISTERED")
    entries = db.execute(
        "SELECT epoch, op, row_json ->> 'lifecycle_status' AS status FROM "
        "registry_changelog WHERE row_id = %s ORDER BY epoch", (row["id"],)).fetchall()
    assert [e["op"] for e in entries] == ["INSERT"]
    assert [e["epoch"] for e in entries] == sorted(e["epoch"] for e in entries)

    repo.set_status(db, row["public_id"], to_status="VALIDATING", reason=None,
                    actor=ACTOR, trace_id=TRACE)
    db.commit()
    entries = db.execute(
        "SELECT op, row_json ->> 'lifecycle_status' AS status FROM registry_changelog "
        "WHERE row_id = %s ORDER BY epoch", (row["id"],)).fetchall()
    assert [(e["op"], e["status"]) for e in entries] == [
        ("INSERT", "REGISTERED"), ("UPDATE", "VALIDATING")]


def test_a_rolled_back_publish_leaves_no_changelog_row(
    db: psycopg.Connection[Any]
) -> None:
    """The other half of "in the same transaction": no phantom epochs.

    A changelog row for a write that never committed would make `MOS-REG-011`'s snapshot
    describe a registry that never existed.
    """
    manifest = _manifest("model_version", "pulmo.effusion-unet", "3.2.1")
    # An OUTER transaction, because `tenant_tx` commits when it is the outermost scope
    # (that is what makes `repo.publish` a complete unit of work on its own). Inside one,
    # it is a savepoint -- so this is the caller-composes case, and the artifact row, its
    # changelog row and its audit row must vanish together.
    with db.transaction():
        repo.publish(db, manifest=manifest, public_id="mv_pulmo_effusion_unet_3_2_1",
                     actor=ACTOR, trace_id=TRACE, **SUPPLY_CHAIN)
        assert db.execute(
            "SELECT count(*) AS c FROM registry_changelog").fetchone()["c"] == 1
        raise psycopg.Rollback()
    assert db.execute("SELECT count(*) AS c FROM registry_changelog").fetchone()["c"] == 0
    assert db.execute("SELECT count(*) AS c FROM artifacts").fetchone()["c"] == 0


def test_the_changelog_is_append_only(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-010`: `REVOKE UPDATE, DELETE`, plus a trigger that binds the owner too."""
    row = _publish_model(db, status="REGISTERED")
    for statement in (
        "UPDATE registry_changelog SET actor = 'someone else'",
        "DELETE FROM registry_changelog",
    ):
        with pytest.raises(psycopg.DatabaseError):
            db.execute(statement)
        db.rollback()
    assert db.execute("SELECT count(*) AS c FROM registry_changelog WHERE row_id = %s",
                      (row["id"],)).fetchone()["c"] == 1


def test_a_publish_writes_an_audit_event(db: psycopg.Connection[Any]) -> None:
    """`MOS-REG-003`: every mutation of an Artifact row emits an `AuditEvent`."""
    row = _publish_model(db, status="REGISTERED")
    # The newest event for this artifact: `audit_events` is append-only for the life of
    # the test database and earlier tests in this module published the same id.
    events = db.execute(
        "SELECT action, action_class, resource_kind, detail FROM audit_events "
        "WHERE resource_id = %s ORDER BY seq DESC LIMIT 1", (row["public_id"],)).fetchall()
    assert [e["action"] for e in events] == ["artifact.publish"]
    assert events[0]["action_class"] == "governance"   # MOS-SEC-148: never sampled
    assert events[0]["resource_kind"] == "model_version"
    # MOS-REG-003 wants the permission exercised and the row digest on the event.
    assert events[0]["detail"]["permission"] == "artifact.publish"
    assert events[0]["detail"]["content_digest"] == row["manifest_digest"]


# =====================================================================================
# 7. Tenancy -- MOS-REG-107
# =====================================================================================
def test_another_tenants_artifact_is_not_visible(
    db: psycopg.Connection[Any], app_conn: psycopg.Connection[Any]
) -> None:
    """`MOS-REG-107`: private artifacts are not enumerable by a non-entitled tenant.

    Asserted as `medicalos_app`, which holds NOBYPASSRLS: the superuser connection above
    would see every row whatever the policy said.
    """
    row = _publish_model(db, status="REGISTERED")
    mine = app_conn.execute("SELECT count(*) AS c FROM artifacts").fetchone()["c"]
    assert mine == 1

    other = str(uuid.uuid4())
    app_conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, other))
    assert app_conn.execute("SELECT count(*) AS c FROM artifacts").fetchone()["c"] == 0
    assert app_conn.execute(
        "SELECT count(*) AS c FROM registry_changelog").fetchone()["c"] == 0

    # And through the repository, where the answer is a 404 rather than a 403: a 403 would
    # confirm that the id names something. The ContextVar and the session GUC name the
    # same tenant here; `tenant_tx` refuses to re-bind a transaction that already carries
    # a different one (MOS-SEC-072), which is itself the containment working.
    from medos.db.tenancy import reset_current_tenant

    token = bind_current_tenant(other)
    try:
        with pytest.raises(ArtifactNotFound):
            repo.get(app_conn, row["public_id"])
    finally:
        reset_current_tenant(token)
        app_conn.execute(
            "SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))


def test_a_connection_with_no_tenant_gets_no_access(pg_dsn: str) -> None:
    """`MOS-STORE-224`: not full access -- NO access, loudly (SQLSTATE 42704)."""
    with psycopg.connect(_dsn_as(pg_dsn, "medicalos_app", APP_PASSWORD),
                         row_factory=dict_row) as conn:
        with pytest.raises(psycopg.errors.UndefinedObject):
            conn.execute("SELECT count(*) FROM artifacts").fetchone()


# =====================================================================================
# 8. The HTTP surface -- chapter 10's paths, chapter 6's semantics
# =====================================================================================
def _key(conn: psycopg.Connection[Any], scope: list[str]) -> str:
    from medos.security import store

    minted, _record = store.issue(
        conn,
        tenant_id=DEFAULT_TENANT_ID,
        principal_kind="service_account",
        principal_id="11111111-1111-1111-1111-111111111111",
        created_by=store.BOOTSTRAP_OPERATOR_ID,
        expires_at=datetime.now(UTC) + timedelta(days=1),
        scope=scope,
        label="registry integration test",
        env="dev",
    )
    conn.commit()
    return minted.plaintext


@pytest.fixture()
def http(pg_dsn: str) -> Iterator[TestClient]:
    def opener(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(pg_dsn, row_factory=dict_row, **kwargs)

    app = create_app(connect=opener, configure_logs=False)
    with TestClient(app) as client:
        yield client


def test_publish_read_and_patch_over_http(
    db: psycopg.Connection[Any], http: TestClient
) -> None:
    """Rows 15, 16, 17, 18 and §6.11's 405. The registry as an integrator meets it."""
    publisher = _key(db, ["model.write", "model.read"])
    manifest = _manifest("model_version", "pulmo.effusion-unet", "3.2.1")
    body = {
        "id": "mv_pulmo_effusion_unet_3_2_1",
        "manifest": manifest,
        "oci_image_digest": SUPPLY_CHAIN["oci_image_digest"],
        "signature": "Y29zaWduLWJ1bmRsZQ==",
        "signature_alg": "cosign-sigstore",
        "signer_identity": SIGNER,
        "sbom_object_key": SUPPLY_CHAIN["sbom_object_key"],
    }
    auth = {"Authorization": f"Bearer {publisher}"}

    created = http.post("/api/v1/model-versions", json=body, headers=auth)
    assert created.status_code == 201, created.text
    assert created.json()["content_digest"] == manifest["content_digest"]

    # MOS-REG-019: the identical republish is a 200, not a second row.
    again = http.post("/api/v1/model-versions", json=body, headers=auth)
    assert again.status_code == 200

    one = http.get("/api/v1/model-versions/mv_pulmo_effusion_unet_3_2_1", headers=auth)
    assert one.status_code == 200 and one.json()["status"] == "REGISTERED"

    listed = http.get("/api/v1/model-versions?capability_id=pleural_effusion", headers=auth)
    assert [a["id"] for a in listed.json()] == ["mv_pulmo_effusion_unet_3_2_1"]

    raw = http.get("/api/v1/model-versions/mv_pulmo_effusion_unet_3_2_1/manifest",
                   headers=auth)
    assert raw.json()["spec"]["io"]["input"]["orientation"] == "LPS"

    att = http.get("/api/v1/model-versions/mv_pulmo_effusion_unet_3_2_1/attestation",
                   headers=auth)
    assert att.json()["subject_digest"] == SUPPLY_CHAIN["oci_image_digest"]
    assert att.json()["sbom_object_key"] == SUPPLY_CHAIN["sbom_object_key"]

    patched = http.patch("/api/v1/model-versions/mv_pulmo_effusion_unet_3_2_1",
                         json={"version": "9.9.9"}, headers=auth)
    assert patched.status_code == 405                      # §6.11, MOS-REG-018
    assert patched.json()["code"] == "ARTIFACT_IMMUTABLE"
    assert patched.headers["content-type"].startswith("application/problem+json")

    missing = http.get("/api/v1/model-versions/mv_does_not_exist", headers=auth)
    assert missing.status_code == 404                      # MOS-REG-107: never 403


def test_recall_needs_its_own_permission(
    db: psycopg.Connection[Any], http: TestClient
) -> None:
    """`MOS-REG-109`: recall MUST NOT be reachable by the routine status permission.

    THIS TEST IS RED, AND THE DEFECT IS RECORDED AS ENTRY 104 of
    docs/spec/99-known-inconsistencies.md. It is not marked `xfail`, by the house rule
    `tests/integration/test_gateway.py` states: an xfail reports green and arrives in the
    skip taxonomy as an unclassified line, so a defect that is real would look like a
    passing suite.

    What it catches: `set_model_status` opens with an unconditional
    `_require(request, "artifact.status.set")` before delegating to `_set_status`, which
    then requires `permission_for("RECALLED", ...)` -- `artifact.recall`. The two are an
    AND, so a credential holding `artifact.recall` and not the routine permission cannot
    recall, and the separately-grantable permission is not separately grantable. It is NOT
    an escalation in the other direction: the routine permission alone still cannot recall.

    Deleting the blanket check would fix it and turn
    `tests/gate/test_declared_permissions_are_enforced.py` red, because that gate scans
    each handler's own source for the declared permission and the real enforcement is a
    computed argument in a shared helper. Entry 104 states the two ways out; both are
    specification decisions rather than code ones.
    """
    _publish_model(db, status="APPROVED")
    routine = {"Authorization": f"Bearer {_key(db, ['model.read', 'artifact.status.set'])}"}
    # `artifact.approve` as well, so the refusal the last assertion sees is MOS-REG-022's
    # 409 and not a 403: the permission check runs BEFORE the lifecycle check, which is
    # the right order -- an unauthorised caller learns nothing about the row's state.
    recall_key = _key(db, ["model.read", "artifact.recall", "artifact.approve"])
    recaller = {"Authorization": f"Bearer {recall_key}"}
    path = "/api/v1/model-versions/mv_pulmo_effusion_unet_3_2_1/status"

    refused = http.post(path, json={"status": "RECALLED", "reason": "defect"},
                        headers=routine)
    assert refused.status_code == 403
    assert refused.json()["required_permission"] == "artifact.recall"

    # The same credential CAN deprecate: the routine permission is not useless, it is
    # narrower -- which is the whole of MOS-REG-109.
    deprecated = http.post(path, json={"status": "DEPRECATED", "reason": "superseded"},
                           headers=routine)
    assert deprecated.status_code == 200 and deprecated.json()["status"] == "DEPRECATED"

    recalled = http.post(path, json={"status": "RECALLED", "reason": "weights defect"},
                         headers=recaller)
    assert recalled.status_code == 200 and recalled.json()["status"] == "RECALLED"

    again = http.post(path, json={"status": "APPROVED", "reason": "fixed"},
                      headers=recaller)
    assert again.status_code == 409                        # MOS-REG-022
    assert again.json()["reason_code"] == "recall_is_irreversible"


def test_impact_is_available_in_the_release_that_has_recall(
    db: psycopg.Connection[Any], http: TestClient
) -> None:
    """`MOS-REG-040`: "a recall state with no impact query is not a recall"."""
    _publish_model(db, status="REGISTERED")
    auth = {"Authorization": f"Bearer {_key(db, ['model.read'])}"}
    answer = http.get("/api/v1/model-versions/mv_pulmo_effusion_unet_3_2_1/impact",
                      headers=auth)
    assert answer.status_code == 200
    assert answer.json()["artifact_id"] == "mv_pulmo_effusion_unet_3_2_1"
    assert isinstance(answer.json()["affected"], list)


def test_a_read_without_the_permission_is_403(
    db: psycopg.Connection[Any], http: TestClient
) -> None:
    """`MOS-SEC-045`: the credential's scope is a ceiling, and an empty one permits
    nothing."""
    _publish_model(db, status="REGISTERED")
    auth = {"Authorization": f"Bearer {_key(db, ['job.read'])}"}
    assert http.get("/api/v1/model-versions", headers=auth).status_code == 403


# =====================================================================================
# 9. The migration's own guarantees, asserted rather than assumed
# =====================================================================================
def test_the_migration_file_is_immutable_once_applied(pg_dsn: str) -> None:
    """`medos/medos/db/migrate.py` refuses a migration whose bytes changed after it ran.

    Asserted here because the generated schema seed lives in that file: an "innocent"
    hand-edit of the JSON would otherwise reach a deployed database as a silent
    divergence from `medos/medos/registry/schemas.py`.
    """
    migration = next(m for m in discover() if m.version == "0012_artifacts")
    with psycopg.connect(pg_dsn, autocommit=True, row_factory=dict_row) as conn:
        recorded = conn.execute(
            "SELECT file_digest FROM schema_migrations WHERE version = %s",
            (migration.version,)).fetchone()
    assert recorded is not None and recorded["file_digest"] == migration.digest


def test_grants_are_column_level_on_artifacts(pg_dsn: str) -> None:
    """`MOS-STORE-228`: `artifacts` takes a COLUMN-level revocation, not a table-level one."""
    with psycopg.connect(pg_dsn, autocommit=True, row_factory=dict_row) as conn:
        updatable = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.column_privileges "
            "WHERE table_name = 'artifacts' AND privilege_type = 'UPDATE' "
            "AND grantee = 'medicalos_app'").fetchall()}
        assert updatable == {"lifecycle_status", "status_reason", "updated_at"}
        deletable = conn.execute(
            "SELECT count(*) AS c FROM information_schema.role_table_grants "
            "WHERE table_name IN ('artifacts','registry_changelog') "
            "AND privilege_type = 'DELETE' AND grantee <> 'medicalos_owner'").fetchone()
        assert deletable["c"] == 0
