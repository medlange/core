# SPDX-License-Identifier: Apache-2.0
"""The schema source, the validator and the lifecycle graph, with no database.

These are the checks that have to be fast enough to run on every save: a manifest schema
with a mistyped keyword, an object schema that forgot its closure, a digest that changes
when a publisher reorders two members. `tests/integration/test_artifacts.py` proves the
same rules against Postgres; this file proves the parts that are pure.

Spec: MOS-REG-014, MOS-REG-015, MOS-REG-016, MOS-REG-017, MOS-REG-020, MOS-REG-021,
MOS-REG-022, MOS-REG-095, MOS-REG-109, MOS-REG-113, MOS-STORE-253.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from medos.registry import lifecycle, schemas
from medos.registry.digest import content_digest_of, manifest_without_digest
from medos.registry.jsonschema import (
    SchemaError,
    assert_closed_objects,
    check_schema,
    iter_errors,
)

MIGRATION = (
    Path(__file__).resolve().parents[2] / "medos/medos/db/migrations/0012_artifacts.up.sql"
)


# =====================================================================================
# The validator's closed keyword set -- MOS-REG-015
# =====================================================================================
def test_an_unknown_schema_keyword_is_a_build_error() -> None:
    """A permissive validator IGNORES `minumum`; this one refuses the schema.

    That is the whole reason this validator is in the tree rather than a dependency: the
    constraint that silently does nothing is the one that lets a bad manifest through.
    """
    with pytest.raises(SchemaError) as exc:
        check_schema({"type": "integer", "minumum": 0}, registry={})
    assert "minumum" in str(exc.value)


def test_an_unresolvable_ref_is_a_build_error() -> None:
    """`MOS-REG-016`: every schema is in the in-repo source; nothing is fetched."""
    with pytest.raises(SchemaError):
        check_schema({"$ref": "https://schemas.medicalos.org/artifact/tool/1.0.0.json"},
                     registry=schemas.SCHEMAS)


def test_an_object_schema_must_be_closed() -> None:
    """`MOS-REG-015`: `additionalProperties: false` at every object level."""
    with pytest.raises(SchemaError):
        assert_closed_objects({"type": "object", "properties": {"a": {"type": "string"}}})
    # A constrained MAP is closed in the sense the requirement is after: the key is
    # constrained by `propertyNames` and the value by a schema. Chapter 6's own
    # `io.output.label_map` is such a map and cannot set the literal `false`.
    assert_closed_objects(
        {"type": "object", "propertyNames": {"pattern": "^[0-9]+$"},
         "additionalProperties": {"type": "string"}}
    )


def test_every_seeded_schema_is_closed_and_checkable() -> None:
    """The import-time gate, asserted rather than assumed."""
    for ident, schema in schemas.SCHEMAS.items():
        check_schema(schema, registry=schemas.SCHEMAS)
        assert_closed_objects(schema, path=ident)
    assert set(schemas.SEEDED_KINDS) == {"service", "model"}
    assert {r["kind"] for r in schemas.schema_rows()} == set(schemas.SEEDED_KINDS)


def test_a_kind_without_a_schema_is_refused_by_name() -> None:
    """The five kinds this release does not seed say so, rather than failing obscurely."""
    for kind in ("preprocessing", "dataset", "annotation", "workflow", "policy_set"):
        with pytest.raises(SchemaError) as exc:
            schemas.schema_for_kind(kind)
        assert kind in str(exc.value)
    with pytest.raises(SchemaError):
        schemas.schema_for_kind("tool")          # not an artifact kind at all


# =====================================================================================
# The envelope -- MOS-REG-013, MOS-REG-015
# =====================================================================================
def _envelope(**overrides: Any) -> dict[str, Any]:
    base = {
        "schema_version": "1.0.0",
        "kind": "model_version",
        "family": "pulmo.effusion-unet",
        "version": "3.2.1",
        "content_digest": "sha256:" + "ab" * 32,
        "publisher": {"org_id": "org_pulmoai", "signing_identity": "https://x/y@refs/tags/v1"},
        "created_at": "2026-01-09T10:41:07Z",
        "spec": {},
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("mutation", "keyword"),
    [
        ({"kind": "tool_version"}, "enum"),              # MOS-REG-013: closed enum
        ({"version": "3.2"}, "pattern"),                 # MOS-REG-095: semver
        ({"version": "03.2.1"}, "pattern"),
        ({"family": "Pulmo.Effusion"}, "pattern"),
        ({"content_digest": "ab" * 32}, "pattern"),      # the `sha256:` prefix is required
        ({"created_at": "9 January 2026"}, "format"),
        ({"surprise": True}, "additionalProperties"),    # MOS-REG-015
    ],
)
def test_the_envelope_refuses(mutation: dict[str, Any], keyword: str) -> None:
    errors = [v.keyword for v in iter_errors(_envelope(**mutation), schemas.ENVELOPE_SCHEMA,
                                             registry=schemas.SCHEMAS)]
    assert keyword in errors


def test_a_missing_required_member_is_named() -> None:
    manifest = _envelope()
    del manifest["publisher"]
    assert any("publisher" in str(v) for v in iter_errors(
        manifest, schemas.ENVELOPE_SCHEMA, registry=schemas.SCHEMAS))


# =====================================================================================
# The content digest -- MOS-REG-017
# =====================================================================================
def test_the_digest_excludes_itself_and_ignores_member_order() -> None:
    """RFC 8785 canonicalisation: two orderings of one manifest are one digest.

    If the digest depended on member order, a publisher's YAML formatter would turn a
    byte-identical republish (`MOS-REG-019`: 200, a no-op) into a conflict (409).
    """
    a = {"kind": "model_version", "version": "1.0.0", "spec": {"b": 1, "a": 2}}
    b = {"version": "1.0.0", "spec": {"a": 2, "b": 1}, "kind": "model_version"}
    assert content_digest_of(a) == content_digest_of(b)

    with_digest = dict(a, content_digest="sha256:" + "00" * 32)
    assert content_digest_of(with_digest) == content_digest_of(a)
    assert "content_digest" not in manifest_without_digest(with_digest)


# =====================================================================================
# The lifecycle graph -- MOS-REG-020, MOS-REG-021, MOS-REG-022, MOS-REG-109
# =====================================================================================
def test_recall_has_no_exit() -> None:
    """`MOS-REG-022`: not "discouraged" -- there is no edge out of RECALLED."""
    assert lifecycle.TRANSITIONS["RECALLED"] == frozenset()
    ok, why = lifecycle.transition_allowed("model", "RECALLED", "APPROVED")
    assert not ok and why == "recall_is_irreversible"


def test_unsuspension_restores_only_the_previous_status() -> None:
    assert lifecycle.transition_allowed(
        "model", "SUSPENDED", "APPROVED", previous_status="APPROVED") == (True, "")
    ok, why = lifecycle.transition_allowed(
        "model", "SUSPENDED", "APPROVED", previous_status="REGISTERED")
    assert not ok and why == "suspension_restores_previous_status_only"
    ok, why = lifecycle.transition_allowed("model", "SUSPENDED", "APPROVED")
    assert not ok and why == "no_previous_status_recorded"


def test_the_status_set_is_per_kind() -> None:
    """`MOS-REG-020`'s three rows, and `MOS-REG-038`'s exclusion."""
    assert lifecycle.transition_allowed("preprocessing", "REGISTERED", "VALIDATING")[1] == (
        "status_not_permitted_for_kind")
    assert lifecycle.transition_allowed("dataset", "SEALED", "DEFECTIVE") == (True, "")
    for kind_statuses in lifecycle.LIFECYCLE_STATUSES.values():
        assert not {"STAGING", "DEPLOYED", "PRODUCTION"} & set(kind_statuses)


def test_recall_takes_a_permission_of_its_own() -> None:
    """`MOS-REG-109`: not reachable with the routine status-management permission."""
    assert lifecycle.permission_for("RECALLED") == "artifact.recall"
    assert lifecycle.permission_for("DEPRECATED") == "artifact.status.set"
    assert lifecycle.permission_for("APPROVED") == "artifact.approve"
    # Un-suspension is the suspender's decision reversed, so it takes their permission.
    assert lifecycle.permission_for("APPROVED", from_status="SUSPENDED") == "artifact.suspend"


def test_the_two_kind_vocabularies_are_one_mapping() -> None:
    """`MOS-STORE-253`: identity plus the `_version`/`_spec`/`_set` suffix, and nothing
    else."""
    assert lifecycle.VERSION_KIND["policy_set"] == "policy_set_version"
    assert lifecycle.FAMILY_KIND["annotation_set"] == "annotation"
    assert set(lifecycle.FAMILY_KIND.values()) == set(lifecycle.VERSION_KIND)
    # 0.4.0 kinds are absent on purpose (§15.2.6).
    assert not {"tool", "agent", "workflow"} & set(lifecycle.VERSION_KIND)


# =====================================================================================
# The two statements of one grammar -- the JSON Schema and the SQL domain
# =====================================================================================
def test_the_semver_grammar_is_the_same_in_the_schema_and_in_the_migration() -> None:
    """The envelope's `version` pattern and 0012's `semver` domain are one grammar.

    Two statements of a grammar is a duplication that has to be checked, not trusted: a
    manifest the API accepts and the column refuses would be a 500 at insert time.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    domain = re.search(r"CREATE DOMAIN semver AS text CHECK \(VALUE ~\s*\n?\s*'([^']+)'", sql)
    assert domain is not None, "0012 no longer declares the semver domain"
    schema_pattern = schemas.ENVELOPE_SCHEMA["properties"]["version"]["pattern"]
    assert domain.group(1) == schema_pattern


def test_the_generated_seed_is_what_the_migration_carries() -> None:
    """`MOS-REG-016`: the SQL seed is generated from this source, not maintained beside it.

    The integration suite compares the APPLIED database with the source; this catches the
    same drift one step earlier, without a database, which is where a developer who has
    just hand-edited the migration will see it.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    assert schemas.sql_seed() in sql
