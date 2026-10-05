# SPDX-License-Identifier: Apache-2.0
"""The five artifacts of the permission contract, held to each other.

`medos/contracts/permissions.yaml`, `medos/api/v1/routes.{core,train}.yaml`, `medos/schemas/`,
the running ASGI app and the three spec tables (docs/spec/08-security.md section 8.3.2,
docs/spec/10-api.md tables 10.2-A and 10.2-B) each state part of one fact. Nothing in
this repository stopped them drifting apart until this file, and drift is not
hypothetical here: docs/spec/99-known-inconsistencies.md carries SEVEN recorded
instances of one rule living in two copies that diverged, one of which (entry 5) is
twenty-three route rows bound to identifiers chapter 8 says are not permissions. These
tests are the thing that makes the eighth instance a red run.

WHAT THE TRAINING-SURFACE CHANGE ADDED TO THIS FILE, AND WHY EACH IS HERE
-------------------------------------------------------------------------
Register entry 79 is that chapter 19's Train, Evaluate and Promote screens had no API
path in any chapter. Twelve reserved rows now carry them, and four of chapter 19's rules
are the kind that a reviewer can only take on trust unless something asserts them:

  MOS-UI-149  six quantities the console MUST NOT reveal -- asserted as the ABSENCE of a
              member from a closed request schema, which is the difference between a
              screen that offers no control and an API on which none exists.
  MOS-UI-148 / MOS-TRAIN-211  a no-code client MUST NOT reach `monai_supervised` without
              a 20-character rationale -- asserted as the schema's own biconditional,
              beside 0013's matching CHECK.
  MOS-UI-167 / MOS-SEC-158  no promotion from this surface -- asserted against chapter
              17's own FORBIDDEN tuples, imported rather than restated.
  MOS-TRAIN-216 / MOS-UI-163  the test-exposure counter only goes up -- asserted as a
              `const` on the wire and as two triggers in the migration.

`status:` is also checked in both directions against the app for the first time, so a
reserved row that gets quietly mounted, and a `served` row nothing serves, are both red.

Everything below is a file read, a markdown table parse, an import of a constant, or a
construction of the ASGI app. No container, no database, no network.

Spec: MOS-SEC-031, MOS-SEC-032, MOS-SEC-033, MOS-SEC-034, MOS-SEC-158, MOS-API-005,
MOS-API-008, MOS-API-012, MOS-API-035, MOS-API-036, MOS-API-038, MOS-API-084,
MOS-API-085, MOS-API-089, MOS-API-093, MOS-API-112, MOS-SAFE-069, MOS-REG-021,
MOS-TRAIN-124, MOS-TRAIN-141, MOS-TRAIN-174, MOS-TRAIN-211, MOS-TRAIN-216,
MOS-TRAIN-222, MOS-TRAIN-234, MOS-UI-102, MOS-UI-147 to MOS-UI-149, MOS-UI-157,
MOS-UI-162, MOS-UI-163, MOS-UI-167, MOS-UI-168, chapter 8 acceptance check 5 and
chapter 10 §10.11 acceptance item 12.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
#: The PLATFORM directory. Paths inside `medos/api/v1/routes.*.yaml` are relative to
#: it, not to the repository, because `medos/tools/permcheck.py` -- the shipped tool
#: that reads the same index -- resolves them that way. One canonical form, and this
#: is the constant that keeps the test using it instead of a second one.
PLATFORM = ROOT / "medos"
# `medos/tools/` is a directory of scripts, not a package, and giving it an `__init__.py` to
# please one test would make five unrelated scripts importable as `tools.*`. The path
# insertion is what `medos/tools/permcheck.py` itself does, for the same reason.
if str(ROOT / "medos" / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "medos" / "tools"))

# Separated from `permcheck` because ruff's isort sees the repository-root `medos/contracts/`
# directory and classifies `medos/tools/contracts.py` as first-party. Joining the two blocks
# is an I001; this is the sorted form, not an accident.
import contracts  # noqa: E402
import permcheck  # noqa: E402

PERMISSIONS = contracts.load_permissions()
ROUTES = contracts.load_routes()
CATALOGUE: dict[str, Any] = PERMISSIONS["permissions"]
NOT_PERMISSIONS: dict[str, Any] = PERMISSIONS["not_permissions"]
UNREGISTERED: dict[str, Any] = PERMISSIONS["unregistered_in_use"]
ENTRIES: list[dict[str, Any]] = ROUTES["routes"]

#: MOS-API-112's four paths, five rows -- the `decision` path carries two verbs. Rows
#: `R1`-`R5`, the corpus side: chapter 19 sections 19.3.2 and 19.3.4. The name kept its
#: `RESERVED_` prefix through two changes and is now wrong on both halves -- these five
#: are served -- but it is the key the whole file joins on, so it is corrected here rather
#: than renamed in forty places: the SET is what the assertions are about and the set has
#: not changed.
RESERVED_CORPUS = {
    ("POST", "/harvest-batches"),
    ("GET", "/harvest-candidates/{harvest_candidate_id}/decision"),
    ("POST", "/harvest-candidates/{harvest_candidate_id}/decision"),
    ("PUT", "/tenants/{tenant_id}/training-policy"),
    ("GET", "/harvest-batches/{harvest_batch_id}/stratification"),
}

#: Rows `R6`-`R17`, the run side: chapter 19 sections 19.3.5, 19.3.6 and 19.3.7, plus
#: the `ConfigurationSearch` and `ConversionRun` entities register entry 79 names.
#: There is deliberately NO promotion, approval, deployment or `clinical_use_mode` path
#: among them; `test_no_training_route_can_reach_a_promotion` is what holds that.
RESERVED_RUNS = {
    ("POST", "/training-runs"),
    ("GET", "/training-runs"),
    ("GET", "/training-runs/{training_run_id}"),
    ("POST", "/training-runs/{training_run_id}/cancel"),
    ("GET", "/capabilities/{capability_id}/seed-variance"),
    ("GET", "/training-runs/{training_run_id}/test-exposure"),
    ("GET", "/training-runs/{training_run_id}/candidate"),
    ("POST", "/configuration-searches"),
    ("GET", "/configuration-searches/{configuration_search_id}"),
    ("POST", "/configuration-searches/{configuration_search_id}/nomination"),
    ("POST", "/conversion-runs"),
    ("GET", "/conversion-runs/{conversion_run_id}"),
}

#: Rows `R18`-`R31`, the corpus-ASSEMBLY side: everything a browser needs upstream of
#: `POST /api/v1/training-runs`, whose three ids had no producer on `/api/v1` at all.
#: There is deliberately NO row that freezes a `DatasetSplit` and none that writes a
#: waiver; `test_no_row_lets_a_caller_name_its_own_partition` and
#: `test_no_row_anywhere_writes_a_waiver` are what hold those two absences.
RESERVED_UPSTREAM = {
    ("GET", "/tenants/{tenant_id}/training-policy"),
    ("POST", "/sampling-plans"),
    ("GET", "/sampling-plans"),
    ("GET", "/sampling-plans/{sampling_plan_id}"),
    ("GET", "/harvest-batches"),
    ("GET", "/harvest-batches/{harvest_batch_id}"),
    ("GET", "/harvest-batches/{harvest_batch_id}/candidates"),
    ("GET", "/harvest-batches/{harvest_batch_id}/split-preview"),
    ("POST", "/harvest-batches/{harvest_batch_id}/seal"),
    ("GET", "/seal-runs/{seal_run_id}"),
    ("GET", "/dataset-splits/{dataset_split_id}"),
    ("POST", "/dataset-versions/{dataset_version_id}/annotation-sets"),
    ("GET", "/dataset-versions/{dataset_version_id}/annotation-sets"),
    ("GET", "/annotation-sets/{annotation_set_id}"),
}

RESERVED = RESERVED_CORPUS | RESERVED_RUNS | RESERVED_UPSTREAM

#: The five permissions MOS-API-112's change registered, one per reserved corpus row.
CORPUS_PERMISSIONS = {
    "harvest_batch.open",
    "harvest_candidate.read",
    "curation_decision.record",
    "training_data_policy.record",
    "corpus_stratification_report.read",
}

#: The eight this change registers for the run side. `model_version.read`, which row R12
#: binds, is NOT here: chapter 8 already registered it and this change mints nothing it
#: can reuse.
RUN_PERMISSIONS = {
    "training_run.read",
    "training_run.submit",
    "training_run.cancel",
    "configuration_search.read",
    "configuration_search.declare",
    "configuration_search.nominate",
    "conversion_run.read",
    "conversion_run.submit",
}

#: The four the corpus-assembly change registers. Everything else rows R18-R31 bind was
#: already in section 8.3.2 and is reused rather than re-minted: `dataset.read`,
#: `dataset_version.read`, `dataset_version.create`, `dataset_split.read`,
#: `dataset_split.freeze`, `annotation_set.read` and `annotation_set.create`. That reuse
#: is the point of MOS-UI-102's reference principal -- the seeded `evidence_scientist`
#: role already holds every one of them, so the surface needed four new keys and not
#: eleven.
UPSTREAM_PERMISSIONS = {
    "training_data_policy.read",
    "sampling_plan.read",
    "sampling_plan.declare",
    "harvest_batch.read",
}

#: The seven chapter 8 keys rows R18-R31 REUSE. Named so that a later change that mints
#: a parallel spelling for one of them fails here rather than in review.
REUSED_EVIDENCE_PERMISSIONS = {
    "dataset.read",
    "dataset_version.read",
    "dataset_version.create",
    "dataset_split.read",
    "dataset_split.freeze",
    "annotation_set.read",
    "annotation_set.create",
}

NEW_PERMISSIONS = CORPUS_PERMISSIONS | RUN_PERMISSIONS | UPSTREAM_PERMISSIONS


# =====================================================================================
# The checker itself -- and proof that it checks
# =====================================================================================
def test_the_permission_contract_holds() -> None:
    """`python medos/tools/permcheck.py`, as a test. This is the CI clause of MOS-SEC-032."""
    findings = permcheck.check()
    assert findings == [], "\n".join(findings)


def test_the_checker_notices_a_permission_the_chapter_does_not_have() -> None:
    """A checker whose only evidence is that it passes has not been shown to check."""
    mutated = copy.deepcopy(PERMISSIONS)
    mutated["permissions"]["invented.permission"] = {"grants": "nothing", "class": "read"}
    findings = permcheck.check(source=mutated, with_code=False)
    assert any("invented.permission" in f and "§8.3.2 has no row" in f for f in findings)


def test_the_checker_notices_a_chapter_row_the_file_does_not_have() -> None:
    mutated = copy.deepcopy(PERMISSIONS)
    del mutated["permissions"]["harvest_batch.open"]
    findings = permcheck.check(source=mutated, with_code=False)
    assert any(
        "harvest_batch.open" in f and "permissions.yaml has no key" in f for f in findings
    )


def test_the_checker_notices_a_class_that_drifted() -> None:
    mutated = copy.deepcopy(PERMISSIONS)
    mutated["permissions"]["curation_decision.record"]["class"] = "read"
    findings = permcheck.check(source=mutated, with_code=False)
    assert any("curation_decision.record" in f and "class" in f for f in findings)


def test_the_checker_notices_a_route_bound_to_an_unregistered_permission() -> None:
    mutated = copy.deepcopy(ROUTES)
    mutated["routes"][0]["permission"] = "harvest.everything"
    findings = permcheck.check(routes_doc=mutated, with_code=False)
    assert any("harvest.everything" in f for f in findings)


def test_the_checker_notices_a_training_route_reaching_a_promotion() -> None:
    """The check that holds 19.3.7 shut. Written as a mutation because the committed
    files cannot exhibit the failure -- and a guard that has only ever been observed
    passing over a set with no offender in it is a guard nobody has seen work."""
    mutated = copy.deepcopy(ROUTES)
    entry = next(e for e in mutated["routes"] if e.get("surface") == "training_console")
    entry["permission"] = "artifact.approve"
    findings = permcheck.check(routes_doc=mutated)
    assert any("MOS-SEC-158" in f and "artifact.approve" in f for f in findings)
    assert any("MOS-UI-102" in f and "governance" in f for f in findings)


def test_the_checker_notices_a_permission_table_10_2_a_does_not_name() -> None:
    mutated = copy.deepcopy(ROUTES)
    entry = next(e for e in mutated["routes"] if e["table_row"] == "R8")
    entry["permission"] = "phi.erase"
    findings = permcheck.check(routes_doc=mutated)
    assert any("table 10.2-A does not name it" in f for f in findings)


def test_the_checker_notices_a_problem_type_block_that_drifted_from_the_code() -> None:
    """`training_problem_types` records a divergence this change deliberately does not
    resolve, so the one thing it must not do is quietly stop describing the code."""
    mutated = copy.deepcopy(ROUTES)
    mutated["training_problem_types"]["training-run-refused"]["status"] = 418
    findings = permcheck.check(routes_doc=mutated)
    assert any("training-run-refused" in f and "418" in f for f in findings)


def test_the_checker_notices_a_problem_slug_nothing_can_raise() -> None:
    mutated = copy.deepcopy(ROUTES)
    entry = next(e for e in mutated["routes"] if e["table_row"] == "R6")
    entry["problems"] = [*entry["problems"], "gpu-was-sad"]
    findings = permcheck.check(routes_doc=mutated)
    assert any("gpu-was-sad" in f for f in findings)


# =====================================================================================
# MOS-SEC-031 -- the identifier grammar
# =====================================================================================
def test_every_catalogue_identifier_matches_the_grammar() -> None:
    for identifier in CATALOGUE:
        assert contracts.PERMISSION_RE.match(identifier), identifier


def test_nothing_anywhere_carries_a_four_segment_identifier() -> None:
    """MOS-SEC-031: "Four or more segments is a build error." Every surface at once."""
    everywhere: set[str] = set(CATALOGUE) | set(NOT_PERMISSIONS) | set(UNREGISTERED)
    for entry in ENTRIES:
        if entry.get("permission"):
            everywhere.add(entry["permission"])
        everywhere.update((entry.get("permission_by_transition") or {}).values())
    everywhere.update(permcheck.enforced_permissions())
    offenders = sorted(i for i in everywhere if i.count(".") >= 3)
    assert offenders == [], offenders


def test_the_grammar_is_one_string_in_all_three_places() -> None:
    """`medos/tools/contracts.py`, `medos/medos/security/scopes.py` and `0004_auth.up.sql`.

    Three copies of a regex is the drift this whole file is about; the copies exist
    because one is Python, one is Python in a different layer and one is PL/pgSQL, and
    none of the three can import the others. Equality is asserted instead.
    """
    from medos.security.scopes import PERMISSION_RE as SCOPES_RE

    assert contracts.PERMISSION_RE.pattern == SCOPES_RE.pattern
    sql = (ROOT / "medos/medos/db/migrations/0004_auth.up.sql").read_text(encoding="utf-8")
    assert contracts.PERMISSION_RE.pattern in sql


# =====================================================================================
# MOS-SEC-032 -- one key per row of section 8.3.2, and no others
# =====================================================================================
def test_the_catalogue_is_exactly_section_8_3_2() -> None:
    rows = {p.id: p for p in contracts.catalogue_8_3_2()}
    assert set(rows) == set(CATALOGUE)
    for identifier, row in rows.items():
        assert CATALOGUE[identifier]["class"] == row.cls, identifier
        assert CATALOGUE[identifier]["grants"] == row.grants, identifier


def test_every_class_is_one_of_the_six() -> None:
    for identifier, body in CATALOGUE.items():
        assert body["class"] in contracts.PERMISSION_CLASSES, identifier


def test_the_three_blocks_are_disjoint() -> None:
    """A spelling cannot be a grant and a refusal at once, in either direction."""
    assert not set(CATALOGUE) & set(NOT_PERMISSIONS)
    assert not set(CATALOGUE) & set(UNREGISTERED)
    assert not set(NOT_PERMISSIONS) & set(UNREGISTERED)


def test_the_new_training_permissions_are_registered_in_both_places() -> None:
    rows = {p.id for p in contracts.catalogue_8_3_2()}
    assert NEW_PERMISSIONS <= rows
    assert NEW_PERMISSIONS <= set(CATALOGUE)


def test_no_new_permission_uses_a_create_action() -> None:
    """MOS-SEC-034 pairs a `create` permission with a removal disposition, and names
    exactly four never-deleted resources -- AuditEvent, PolicyDecision, Result and
    Deployment. `harvest_batches`, `curation_decisions` and `training_data_policies` are
    append-only under MOS-STORE-359 and MOS-TRAIN-203, so a fifth entry on that list
    would be owed and MOS-SEC-034 is not this change's to edit. The verbs are the ones
    the domain and the columns already use -- `open` (opened_by), `record` (recorded_by,
    decided_by) -- so the create/delete pairing is not engaged at all.
    """
    for identifier in NEW_PERMISSIONS:
        assert not identifier.endswith(".create"), identifier
        assert not identifier.endswith(".delete"), identifier


# =====================================================================================
# What the code enforces
# =====================================================================================
def test_every_enforced_identifier_is_accounted_for() -> None:
    """MOS-SEC-032: every permission identifier appearing anywhere MUST be a key here.

    Accounted for means one of three things, and the difference is the point: a grant
    (`permissions`), a spelling section 8.3.2 refuses (`not_permissions`), or a recorded
    defect that denies under MOS-SEC-033 (`unregistered_in_use`).
    """
    sites = permcheck.enforced_permissions()
    accounted = set(CATALOGUE) | set(NOT_PERMISSIONS) | set(UNREGISTERED)
    unaccounted = {i: sites[i] for i in sorted(set(sites) - accounted)}
    assert unaccounted == {}, unaccounted


def test_unregistered_in_use_is_exactly_the_remainder() -> None:
    """The defect register cannot grow silently, and cannot keep a closed entry."""
    sites = permcheck.enforced_permissions()
    remainder = {
        i for i in sites if i not in CATALOGUE and i not in NOT_PERMISSIONS
    }
    assert remainder == set(UNREGISTERED)


def test_the_recorded_defects_are_the_three_this_change_found() -> None:
    """Pinned by name. A fourth is a finding somebody has to look at, not a number."""
    assert set(UNREGISTERED) == {"evidence.run", "evidence.report.issue", "phi.admin"}


def test_the_gateway_literals_the_checker_claims_are_really_there() -> None:
    """`enforced_permissions()` lists three gateway literals by hand, because they are
    written inline rather than in an importable table. Hand-written means it can rot, so
    the files are read and the strings asserted present."""
    app = (ROOT / "medos/medos/gateway/app.py").read_text(encoding="utf-8")
    auth = (ROOT / "medos/medos/gateway/auth.py").read_text(encoding="utf-8")
    assert '"study.write" if write else "study.read"' in app
    assert 'has("phi.admin")' in app
    assert 'has("study.read")' in auth


def test_every_declared_non_permission_is_or_is_not_named_by_the_chapter() -> None:
    paragraph = contracts.non_permission_paragraph()
    for identifier, body in NOT_PERMISSIONS.items():
        named = body.get("named_in_8_3_2", True)
        assert (f"`{identifier}`" in paragraph) is named, identifier


# =====================================================================================
# medos/api/v1/routes.{core,train}.yaml -- MOS-API-085, MOS-API-005
# =====================================================================================
def test_every_route_declares_exactly_one_permission_or_is_authn_only() -> None:
    for entry in ENTRIES:
        if entry.get("permission") is None:
            assert entry.get("authn_only") is True, entry["operation_id"]
        else:
            assert isinstance(entry["permission"], str), entry["operation_id"]


def test_every_route_permission_is_a_key_of_contracts_permissions_yaml() -> None:
    """MOS-SEC-032's binding clause, with the two recorded exceptions named."""
    offenders = sorted(
        entry["operation_id"]
        for entry in ENTRIES
        if entry.get("permission")
        and entry["permission"] not in CATALOGUE
        and entry.get("permission_is_registered") is not False
    )
    assert offenders == [], offenders


def test_the_routes_bound_to_a_non_permission_are_exactly_the_two_known_ones() -> None:
    """docs/spec/99-known-inconsistencies.md entry 5, as far as it reaches served code.

    Pinned so a third cannot be added quietly. Resolving these two is chapter 10's
    change -- re-pointing the rows at `service_version.publish` and `model.create` --
    and this file records the extent rather than inventing a mapping.
    """
    bound = {
        entry["permission"]
        for entry in ENTRIES
        if entry.get("permission_is_registered") is False
    }
    assert bound == {"service.publish", "model.write"}
    for identifier in bound:
        assert identifier in NOT_PERMISSIONS


def test_every_operation_id_is_unique() -> None:
    ids = [entry["operation_id"] for entry in ENTRIES]
    assert len(ids) == len(set(ids))


def test_routes_yaml_agrees_with_table_10_2_b() -> None:
    rows = {row.number: row for row in contracts.route_table_10_2_b()}
    for entry in ENTRIES:
        number = entry.get("table_row")
        if number is None:
            assert entry.get("defined_by"), entry["operation_id"]
            continue
        row = rows[str(number)]
        assert (row.method, row.path) == (entry["method"], entry["path"])
        assert row.permission == entry.get("permission")


def test_every_route_the_platform_serves_has_a_registry_entry() -> None:
    """A registry that omits a served route is not a registry (MOS-API-012 item 3:
    "a handler that is reachable but absent from the generated route table MUST fail").

    The served set is read off the real ASGI app rather than a list in this file, so
    mounting a new router without registering it here is a red run.
    """
    assert permcheck.unregistered_served_routes(ENTRIES) == []


def test_the_coverage_block_is_recomputed_not_asserted() -> None:
    """The numbers in `coverage:` are the honest part of an incomplete registry, so
    they are recomputed here. They can be made true by adding routes; they cannot be
    made true by editing the block."""
    rows = [row.number for row in contracts.route_table_10_2_b()]
    served_rows = [n for n in rows if not n.startswith("R")]
    with_entry = {str(e["table_row"]) for e in ENTRIES if e.get("table_row") is not None}
    coverage = ROUTES["coverage"]
    assert coverage["served_entries"] == sum(1 for e in ENTRIES if e["status"] == "served")
    assert coverage["reserved_entries"] == sum(
        1 for e in ENTRIES if e["status"] == "reserved"
    )
    assert coverage["table_10_2_b_served_rows"] == len(served_rows)
    assert coverage["table_10_2_b_reserved_rows"] == len(rows) - len(served_rows)
    assert coverage["table_10_2_b_rows_without_an_entry"] == len(
        [n for n in served_rows if n not in with_entry]
    )


def test_mos_safe_069s_eight_rows_are_all_here() -> None:
    """MOS-SAFE-069: "Every row below MUST also appear in Chapter 10 table 10.2-B and in
    the registry of the product that owns it" (MOS-SAFE-069, amended at 0.4.0)."""
    declared = {(e["method"], e["path"]) for e in ENTRIES}
    for method, path in (
        ("GET", "/result-reviews"),
        ("GET", "/result-reviews/{result_review_id}"),
        ("POST", "/result-reviews/{result_review_id}/claim"),
        ("POST", "/result-reviews/{result_review_id}/release"),
        ("POST", "/result-reviews/{result_review_id}/submit"),
        ("POST", "/result-reviews/{result_review_id}/assign"),
        ("POST", "/result-reviews/{result_review_id}/reopen"),
        ("GET", "/results/{result_id}/review"),
    ):
        assert (method, path) in declared, (method, path)


def test_the_status_routes_name_every_transition_permission_of_mos_reg_021() -> None:
    """MOS-REG-021 makes the permission a function of the target status, so the entry
    carries the map rather than one flattened answer. `artifact.recall` MUST NOT be
    reachable through `artifact.status.set` (MOS-REG-109)."""
    from medos.registry.lifecycle import TRANSITION_PERMISSION

    maps = [e["permission_by_transition"] for e in ENTRIES if e.get("permission_by_transition")]
    assert len(maps) == 2
    for declared in maps:
        for status, permission in declared.items():
            assert TRANSITION_PERMISSION[status] == permission, status
        assert declared["RECALLED"] != declared["DEPRECATED"]


# =====================================================================================
# MOS-API-112 -- the three preconditions, one test each
# =====================================================================================
def test_every_reserved_path_has_a_row_in_table_10_2_b() -> None:
    rows = {
        (row.method, row.path)
        for row in contracts.route_table_10_2_b()
        if row.number.startswith("R")
    }
    assert rows == RESERVED


def test_every_reserved_path_has_a_routes_yaml_entry_naming_a_registered_key() -> None:
    """Every `R*` row has an entry, and every one binds a catalogue key.

    Asserted over ALL SEVENTEEN `R*` rows rather than over the entries whose `status:` is
    `reserved`, because the two stopped being the same set when `medos/medos/api/
    routes_training.py` mounted `R6`-`R17`. The property MOS-API-112 asked for -- a row
    declared, permissioned and schema'd before anything serves it -- is about the ROW; a
    test keyed on `status` would quietly stop covering a row the moment it was served,
    which is the moment the binding starts to matter most.
    """
    by_path = {(e["method"], e["path"]): e for e in ENTRIES}
    entries = {key: by_path[key] for key in RESERVED if key in by_path}
    assert set(entries) == RESERVED
    # The keys no change in this sequence had to mint, because chapter 8 already carried
    # them: `model_version.read` on row R12, and the five evidence-plane spellings rows
    # R26-R31 bind. `dataset.read` is absent because the two rows that bind it (32, 37)
    # are served rows of table 10.2-B and not `R*` rows, and `dataset_split.freeze`
    # because R26 carries it under `also_requires` rather than as its one permission.
    assert {e["permission"] for e in entries.values()} == NEW_PERMISSIONS | {
        "model_version.read",
        "dataset_version.create",
        "dataset_version.read",
        "dataset_split.read",
        "annotation_set.create",
        "annotation_set.read",
    }
    for entry in entries.values():
        assert entry["permission"] in CATALOGUE, entry["operation_id"]


def test_every_reserved_path_has_a_schema_for_every_body() -> None:
    """Keyed on the `R*` ROW, for the reason the test above gives."""
    index: dict[str, str] = ROUTES["schemas"]
    by_path = {(e["method"], e["path"]): e for e in ENTRIES}
    for key in sorted(RESERVED):
        entry = by_path[key]
        names = [(entry.get("request") or {}).get("schema")]
        names += [r.get("schema") for r in (entry.get("responses") or {}).values()]
        for name in [n for n in names if n]:
            assert name in index, (entry["operation_id"], name)
            assert (PLATFORM / index[name]).is_file(), index[name]


def test_the_corpus_paths_are_served_and_the_registry_says_so() -> None:
    """MOS-API-112's own five rows, mounted. THE TEST THIS REPLACES ASSERTED THE OPPOSITE.

    It asserted that nothing served `R1`-`R5`, on a clause in `MOS-API-112` that read
    "MUST NOT be served until a handler is registered" -- which forbids serving them until
    they are served, and cited `MOS-API-089`, a requirement carrying no reservation clause
    at all. `docs/spec/99-known-inconsistencies.md` entry 88 records the circularity and
    the measured consequence: a browser could seal a cohort, freeze a split and submit a
    training run, and could not open a batch or decide a candidate. The requirement now
    says what it means -- "these five are unserved because no handler exists, and the
    change that registers one against the `permission:` value of its own row serves them"
    -- and `medos/medos/api/routes_curation.py` registers one for each.

    ENTRY 89's LESSON APPLIED. A test that pins a sentence in place is a lock and not a
    guard, so this asserts the property in BOTH directions -- the app serves the path AND
    the registry says `served` -- rather than asserting a phrase. Either half alone is
    satisfiable by editing one file.
    """
    # The TRAIN deployable's factory: Core does not mount the training or curation
    # routers, so building it here would 404 on every path this test checks.
    from medos.api.training_plane import create_training_app as create_app

    served = {
        (m, getattr(route, "path", ""))
        for route in permcheck.flatten_routes(create_app())
        for m in (getattr(route, "methods", None) or ())
    }
    declared = {(e["method"], e["path"]): e for e in ENTRIES}
    for method, path in RESERVED_CORPUS:
        assert (method, f"/api/v1{path}") in served, (method, path)
        entry = declared[(method, path)]
        assert entry["status"] == "served", entry["operation_id"]
        assert entry["permission"] in CORPUS_PERMISSIONS, entry["operation_id"]


def test_mos_api_112_no_longer_states_the_precondition_it_has_discharged() -> None:
    text = _requirement("MOS-API-112")
    # The original clause, in the requirement's own voice. The amended text still
    # QUOTES chapter 19 saying something like it, which is why the whole original
    # sentence is matched rather than a fragment of it.
    assert (
        "permission is registered there today, and `MOS-API-005` admits no route "
        "without one" not in text
    )
    assert "discharged" in text
    # CORPUS_PERMISSIONS and not NEW_PERMISSIONS: MOS-API-112 named the five it
    # registered and says nothing about the eight the run side adds, which stand on
    # MOS-API-089 instead. A requirement is asserted about its own claims.
    for permission in CORPUS_PERMISSIONS:
        assert f"`{permission}`" in text, permission


def test_mos_api_112_no_longer_forbids_serving_what_it_now_describes_as_served() -> None:
    """The amendment register entry 88 records, asserted on the requirement's own text.

    The circular clause is gone and the correction is stated: the reservation is what was
    discharged, the five were unserved only because no handler existed, and the change
    that writes one serves them. `status: served` replaces `status: reserved` in the
    requirement's own account of the registry, so a reader of chapter 10 is not told
    something the deployment contradicts.
    """
    text = _requirement("MOS-API-112")
    # The circular clause survives ONLY as a quotation of the draft that carried it,
    # immediately followed by why it was wrong. Asserted as the pair rather than as an
    # absence, because deleting the sentence would delete the record of the amendment --
    # which is the thing docs/spec/99-known-inconsistencies.md entry 88 exists to keep.
    assert "MUST NOT be served until a handler is registered**, which is circular" in text
    assert "all five `status: served`" in text
    assert "What is discharged is the reservation, not the implementation" in text
    # The obligation MOS-API-112 put on the serving change, still recorded as it stands
    # rather than quietly dropped: the Idempotency-Key echo is closed, the `required`
    # list is chapter 10's to widen and has not been widened.
    assert "MedicalOS-Idempotency-Key" in text


def test_the_training_use_not_permitted_binding_survived_the_amendment() -> None:
    """MOS-API-112's one fixed binding, and the schema that renders it, in one test."""
    text = _requirement("MOS-API-112")
    assert "https://spec.medicalos.org/problems/training-use-not-permitted" in text
    assert "`class: authz_error`" in text
    assert "`code: TRAINING_USE_NOT_PERMITTED`" in text
    assert "`retryable: false`" in text

    schema = json.loads(
        (ROOT / "medos/schemas/training/training-use-not-permitted-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )
    properties = schema["properties"]
    assert properties["type"]["const"] == (
        "https://spec.medicalos.org/problems/training-use-not-permitted"
    )
    assert properties["class"]["const"] == "authz_error"
    assert properties["code"]["const"] == "TRAINING_USE_NOT_PERMITTED"
    assert properties["retryable"]["const"] is False
    assert properties["status"]["const"] == 403


# =====================================================================================
# medos/schemas/ -- MOS-API-084, MOS-API-008, MOS-API-093
# =====================================================================================
ALL_SCHEMAS = sorted((ROOT / "medos" / "schemas").rglob("*.json"))
TRAINING_SCHEMAS = sorted((ROOT / "medos" / "schemas" / "training").glob("*.json"))
#: Chapter 7's entities, which the training surface READS and does not own. A second
#: directory rather than more files under `training/`, so that a change to chapter 17's
#: surface does not sit in the same folder as chapter 7's wire contract.
EVIDENCE_SCHEMAS = sorted((ROOT / "medos" / "schemas" / "evidence").glob("*.json"))
#: Every schema this sequence of changes authored. The four shape tests below run over
#: BOTH directories: the closure rule of MOS-API-008 and the enum-classification rule of
#: MOS-API-093 are properties of a schema, not of the folder it happens to live in, and
#: a test parametrised on one directory would have silently stopped covering the surface
#: the moment a second one appeared.
AUTHORED_SCHEMAS = TRAINING_SCHEMAS + EVIDENCE_SCHEMAS


@pytest.mark.parametrize("path", ALL_SCHEMAS, ids=lambda p: p.name)
def test_every_schema_parses_and_declares_2020_12(path: Path) -> None:
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.parametrize("path", ALL_SCHEMAS, ids=lambda p: p.name)
def test_every_schema_id_is_under_the_one_authority(path: Path) -> None:
    """MOS-API-084: one authority, `https://spec.medicalos.org`. The retired
    `schemas.medicalos.org` is docs/spec/99-known-inconsistencies.md entry 10, and the
    grep that catches it is this."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    prefix = "https://spec.medicalos.org/schemas/v1/"
    assert schema["$id"].startswith(prefix), schema["$id"]
    assert schema["$id"].endswith("/1.0.0.json"), schema["$id"]
    slug = schema["$id"][len(prefix):-len("/1.0.0.json")]
    assert path.name == f"{slug}-1.0.0.json", (path.name, slug)


@pytest.mark.parametrize("path", ALL_SCHEMAS, ids=lambda p: p.name)
def test_no_schema_references_an_outside_authority(path: Path) -> None:
    for ref in _collect(json.loads(path.read_text(encoding="utf-8")), "$ref"):
        assert ref.startswith("#/") or ref.startswith(
            "https://spec.medicalos.org/"
        ), ref


@pytest.mark.parametrize("path", AUTHORED_SCHEMAS, ids=lambda p: p.name)
def test_every_object_in_a_training_schema_is_closed(path: Path) -> None:
    """MOS-API-008: request bodies MUST be validated with `additionalProperties: false`.
    Applied to the responses too, because a generated response type with an open bag on
    it is a type nobody can round-trip."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    for pointer, node in _objects_with_properties(schema):
        assert node.get("additionalProperties") is False, f"{path.name}{pointer}"


@pytest.mark.parametrize("path", AUTHORED_SCHEMAS, ids=lambda p: p.name)
def test_every_required_name_is_a_declared_property(path: Path) -> None:
    schema = json.loads(path.read_text(encoding="utf-8"))
    for pointer, node in _objects_with_properties(schema):
        for name in node.get("required", []):
            assert name in node["properties"], f"{path.name}{pointer}/{name}"


@pytest.mark.parametrize("path", AUTHORED_SCHEMAS, ids=lambda p: p.name)
def test_every_enum_declares_its_classification(path: Path) -> None:
    """MOS-API-093: enum classification MUST be declared with `x-medicalos-enum`. An
    undeclared enum is one nobody can tell is safe to extend (table 10.9-A)."""
    for pointer, node in _nodes(json.loads(path.read_text(encoding="utf-8"))):
        if "enum" in node:
            assert node.get("x-medicalos-enum") in {"closed", "open"}, f"{path.name}{pointer}"


def test_the_curation_reason_codes_are_chapter_17s_closed_set() -> None:
    """MOS-TRAIN-204: `reason_code` MUST NOT include any value whose meaning is a model
    outcome. The enum is pinned against chapter 17's table and against the migration's
    CHECK, so a `hard_case` cannot be added to one copy alone."""
    expected = [
        "quality_artefact",
        "wrong_anatomy",
        "wrong_phase",
        "prior_treatment",
        "duplicate_patient",
        "geometry_unsupported",
        "annotation_infeasible",
        "out_of_scope",
    ]
    request = json.loads(
        (ROOT / "medos/schemas/training/curation-decision-request-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )
    assert request["properties"]["reason_code"]["enum"] == expected
    migration = (ROOT / "medos/medos/db/migrations/0011_curation.up.sql").read_text(
        encoding="utf-8"
    )
    for value in expected:
        assert f"'{value}'" in migration, value
    # Against the enum VALUES, not the file text: the description quotes MOS-TRAIN-204's
    # own examples of what the set must never contain, and a naive substring search over
    # the document would fire on the sentence forbidding them.
    for forbidden in ("model_performed_poorly", "outlier", "hard_case"):
        assert forbidden not in expected, forbidden
        assert forbidden not in request["properties"]["reason_code"]["enum"], forbidden


def test_the_harvest_batch_states_are_the_migrations_check() -> None:
    schema = json.loads(
        (ROOT / "medos/schemas/training/harvest-batch-1.0.0.json").read_text(encoding="utf-8")
    )
    assert schema["properties"]["state"]["enum"] == [
        "OPEN",
        "SAMPLED",
        "CURATING",
        "SEALED",
        "ABANDONED",
    ]
    migration = (ROOT / "medos/medos/db/migrations/0011_curation.up.sql").read_text(
        encoding="utf-8"
    )
    assert "CHECK (state IN ('OPEN','SAMPLED','CURATING','SEALED','ABANDONED'))" in migration


def test_the_legal_bases_are_chapter_17s_closed_set() -> None:
    schema = json.loads(
        (ROOT / "medos/schemas/training/training-data-policy-put-request-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$defs"]["policy"]["properties"]["legal_basis"]["enum"] == [
        "broad_consent",
        "research_ethics_approval",
        "public_corpus_licence",
        "data_processing_agreement",
        "national_derogation",
    ]


def test_the_stratification_report_carries_all_seven_checks() -> None:
    """MOS-TRAIN-088 requires the FULL result. A missing check is not a pass."""
    schema = json.loads(
        (ROOT / "medos/schemas/training/corpus-stratification-report-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )
    checks = schema["properties"]["checks"]
    assert checks["required"] == ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]
    assert set(checks["properties"]) == set(checks["required"])


def test_the_schema_index_and_the_pending_list_are_both_true() -> None:
    index: dict[str, str] = ROUTES["schemas"]
    for name, relative in index.items():
        assert (PLATFORM / relative).is_file(), (name, relative)
    used: set[str] = set()
    for entry in ENTRIES:
        if (entry.get("request") or {}).get("schema"):
            used.add(entry["request"]["schema"])
        for response in (entry.get("responses") or {}).values():
            if response.get("schema"):
                used.add(response["schema"])
    assert sorted(used - set(index)) == sorted(ROUTES["schemas_pending"])
    assert set(ROUTES["minted_schema_names"]) <= set(ROUTES["schemas_pending"])


# =====================================================================================
# The generated target MOS-SEC-032 can actually have here
# =====================================================================================
def test_the_generated_catalogue_is_a_fresh_generation() -> None:
    """MOS-SEC-032: "CI MUST fail if any generated file differs from a fresh generation."
    One target of the four; `medos/contracts/permissions.yaml` names the other three and why
    this repository cannot produce them."""
    assert permcheck.GENERATED.is_file()
    assert permcheck.GENERATED.read_text(encoding="utf-8") == permcheck.render_generated()


def test_the_generated_catalogue_is_readable_without_a_yaml_parser() -> None:
    """The point of emitting it: `medos/medos/security/scopes.py` states that it cannot do
    MOS-SEC-033's resolution without a generated catalogue, and it may not grow a YAML
    dependency to get one."""
    data = json.loads(permcheck.GENERATED.read_text(encoding="utf-8"))
    assert {p["id"] for p in data["permissions"]} == set(CATALOGUE)
    assert data["classes"] == list(contracts.PERMISSION_CLASSES)


def test_the_file_says_which_generated_targets_it_does_not_produce() -> None:
    """A source of truth that silently generates nothing is one in name only."""
    text = (ROOT / "medos/contracts/permissions.yaml").read_text(encoding="utf-8")
    assert "NOT PRODUCED" in text
    for target in (
        "Go constants",
        "Cedar action schema",
        "OpenAPI security scheme",
        "Seed-role migration",
    ):
        assert target in text, target


# =====================================================================================
# The YAML subset reader -- the contract files are only as trustworthy as it is
# =====================================================================================
def test_the_loader_reads_the_shapes_the_contract_files_use() -> None:
    parsed = contracts.load_yaml(
        'version: 1\n'
        "block:\n"
        "  key: plain value\n"
        '  quoted: "a: colon, and a # hash"\n'
        "  flag: false\n"
        "  empty: null\n"
        "  flow: [a, b, c]\n"
        "seq:\n"
        "  - one: 1\n"
        "    two: [x]\n"
        "  - one: 2\n"
        "    two: []\n"
        "folded: >-\n"
        "  first line\n"
        "  second line\n"
    )
    assert parsed["version"] == 1
    assert parsed["block"]["quoted"] == "a: colon, and a # hash"
    assert parsed["block"]["flag"] is False
    assert parsed["block"]["empty"] is None
    assert parsed["block"]["flow"] == ["a", "b", "c"]
    assert parsed["seq"] == [{"one": 1, "two": ["x"]}, {"one": 2, "two": []}]
    assert parsed["folded"] == "first line second line"


def test_an_apostrophe_in_a_plain_scalar_is_not_a_quote() -> None:
    """`summary: Record the tenant's TrainingDataPolicy` is in
    `medos/api/v1/routes.train.yaml`.
    """
    parsed = contracts.load_yaml("summary: Record the tenant's policy\nnext: ok\n")
    assert parsed == {"summary": "Record the tenant's policy", "next": "ok"}


@pytest.mark.parametrize(
    "text",
    [
        "a: &anchor 1\n",
        "a: *alias\n",
        "a: {inline: mapping}\n",
        "a: |\n  block scalar\n",
        "a:\n\tb: 1\n",
        "a: 1\na: 2\n",
        "---\na: 1\n",
        'a: "unterminated\n',
        "a: >-\n",
    ],
    ids=[
        "anchor", "alias", "flow-mapping", "block-scalar", "tab",
        "duplicate", "multi-doc", "unterminated", "empty-fold",
    ],
)
def test_the_loader_refuses_what_it_cannot_represent(text: str) -> None:
    """It errors rather than guessing. A parser that half-reads the permission catalogue
    produces a green check over a wrong answer."""
    with pytest.raises(contracts.ContractSyntaxError):
        contracts.load_yaml(text)


# =====================================================================================
# Pinning what is known-wrong, so it cannot quietly get worse
# =====================================================================================
def test_table_10_2_b_still_has_the_101_rows_mos_api_012_counts() -> None:
    """The reserved rows are numbered `R*`, outside MOS-API-012's enumeration of `1`-`87`
    and its lettered sub-rows, so adding them does not falsify its count. Thirty-one now:
    MOS-API-112's five, the twelve run-side rows, and the fourteen corpus-assembly rows
    that carry sections 19.3.2, 19.3.3 and 19.3.4."""
    rows = contracts.route_table_10_2_b()
    assert len([r for r in rows if not r.number.startswith("R")]) == 101
    assert len([r for r in rows if r.number.startswith("R")]) == 31
    text = _requirement("MOS-API-012")
    assert "101 routes" in text


def test_the_extent_of_known_inconsistency_5_is_pinned() -> None:
    """Entry 5 names twenty-one rows bound to non-permissions. There are twenty-three:
    rows 26 and 29, both `model.write`, are absent from the register's list and from its
    spelling list. Pinned here rather than corrected -- the register is chapter 99's --
    so that a twenty-fourth is a red run and the undercount has a witness.
    """
    rows = [
        row
        for row in contracts.route_table_10_2_b()
        if row.permission and row.permission not in CATALOGUE
    ]
    assert len(rows) == 23
    assert {"26", "29"} <= {row.number for row in rows}
    assert {row.permission for row in rows} <= set(NOT_PERMISSIONS)


# =====================================================================================
# helpers
# =====================================================================================
def _requirement(identifier: str) -> str:
    """The text of one requirement: from its bold id to the next requirement or heading.

    Both boundaries, because a requirement at the end of a subsection is otherwise read
    as running through the next subsection's prose, and a test that asserts a phrase is
    ABSENT from a requirement would then be asserting it about the wrong text.
    """
    for chapter in ("10-api.md", "08-security.md"):
        text = (ROOT / "docs" / "spec" / chapter).read_text(encoding="utf-8")
        marker = f"**{identifier}**"
        if marker not in text:
            continue
        start = text.index(marker)
        ends = [
            text.index(boundary, start + len(marker))
            for boundary in ("\n**MOS-", "\n#")
            if boundary in text[start + len(marker):]
        ]
        return text[start : min(ends)] if ends else text[start:]
    raise AssertionError(f"{identifier} not found")


def _nodes(node: Any, pointer: str = "") -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        out.append((pointer, node))
        for key, value in node.items():
            out.extend(_nodes(value, f"{pointer}/{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out.extend(_nodes(value, f"{pointer}/{index}"))
    return out


#: Conditional subschema keywords. `additionalProperties: false` inside one of these
#: does NOT mean "this object is closed" -- it means "every property not listed in THIS
#: branch is forbidden", which would forbid the object's own declared members the moment
#: the branch applies. The closure MOS-API-008 asks for belongs on the base schema, and
#: the base schema is what these three are excluded from being confused with.
_CONDITIONAL = ("if", "then", "else")


def _objects_with_properties(schema: Any) -> list[tuple[str, dict[str, Any]]]:
    return [
        (pointer, node)
        for pointer, node in _nodes(schema)
        if isinstance(node.get("properties"), dict)
        and not any(
            pointer.endswith(f"/{keyword}") or f"/{keyword}/" in pointer
            for keyword in _CONDITIONAL
        )
    ]


def _collect(node: Any, key: str) -> list[Any]:
    return [value[key] for _, value in _nodes(node) if key in value]


# =====================================================================================
# `status:` describes the deployment, not the intention
#
# Until this change only one direction was checked -- a served route with no entry --
# so `status: served` was a word anybody could write about a route that did not exist.
# The registry of a platform that has not built something yet is exactly where that
# error is cheap to make and expensive to find, because the next reader takes the file
# at its word.
# =====================================================================================
def test_the_status_of_every_entry_is_what_the_app_actually_does() -> None:
    disagreements = permcheck.status_disagrees_with_the_app(ENTRIES)
    assert disagreements == [], "\n".join(disagreements)


def test_the_checker_notices_a_served_claim_the_app_does_not_honour() -> None:
    """The `served` direction. A registry nobody can falsify is documentation."""
    mutated = copy.deepcopy(ENTRIES)
    # NOTHING IS RESERVED ANY MORE, so the mutation has to invent the disagreement rather
    # than borrow it: a `served` entry whose PATH the app does not mount is exactly the
    # claim this direction exists to catch, and it is the shape a typo in `path:` takes.
    entry = next(e for e in mutated if e["status"] == "served")
    entry["path"] = entry["path"] + "-that-nothing-mounts"
    findings = permcheck.status_disagrees_with_the_app(mutated)
    assert any(entry["operation_id"] in f and "does not serve" in f for f in findings)


def test_the_checker_notices_a_reserved_route_that_is_being_served() -> None:
    """The `reserved` direction, which is the one MOS-API-112 and MOS-API-089 care
    about: a reserved row that gets quietly mounted is the licence both requirements
    withhold, and the app-to-registry check alone would not see it."""
    mutated = copy.deepcopy(ENTRIES)
    entry = next(e for e in mutated if e["status"] == "served")
    entry["status"] = "reserved"
    findings = permcheck.status_disagrees_with_the_app(mutated)
    assert any(entry["operation_id"] in f and "SERVES" in f for f in findings)


def test_the_twelve_run_rows_are_served_and_the_registry_says_so() -> None:
    """The other half of the flip `medos/medos/api/routes_training.py` performed.

    The twelve rows of 19.3.5-19.3.7 were declarations; they are now a deployment. Both
    halves are asserted here because forgetting either one is the failure the `status:`
    key exists to catch, and `permcheck.status_disagrees_with_the_app` catches it from
    the other direction: a `served` entry the app does not serve and a `reserved` entry
    it does are both findings. This test is the positive statement of the same property
    -- these twelve, by name, on both sides.
    """
    from medos.api.training_plane import create_training_app as create_app

    served = {
        (m, getattr(route, "path", ""))
        for route in permcheck.flatten_routes(create_app())
        for m in (getattr(route, "methods", None) or ())
    }
    declared_served = {
        (e["method"], e["path"]) for e in ENTRIES if e["status"] == "served"
    }
    assert RESERVED_RUNS <= declared_served
    for method, path in RESERVED_RUNS:
        assert (method, f"/api/v1{path}") in served, (method, path)
    assert permcheck.status_disagrees_with_the_app(ENTRIES) == []


# =====================================================================================
# Table 10.2-A -- §10.11 acceptance item 12, checked for the first time
# =====================================================================================
def test_table_10_2_a_names_every_permission_a_route_row_binds() -> None:
    """A route bound to a spelling chapter 10's own permission table never heard of is
    the drift this file exists for, one table earlier than usual. Scoped to entries that
    render a row of table 10.2-B, because 10.2-A is by its own title "permissions
    referenced by the route table"; the two chapter 6 §6.11 status routes bind
    `artifact.status.set`, which 10.2-A does not name and which is chapter 10's to add.
    """
    table_a = contracts.permission_table_10_2_a()
    missing = sorted(
        entry["permission"]
        for entry in ENTRIES
        if entry.get("permission")
        and entry.get("table_row") is not None
        and entry["permission"] not in table_a
    )
    assert missing == [], missing


def test_every_permission_table_10_2_a_names_is_accounted_for() -> None:
    """§10.11 item 12: "Every permission named in table 10.2-A resolves in Chapter 8's
    catalogue." Eleven of them resolve into `not_permissions` instead, which is register
    entry 5 and is why the assertion is against the union rather than the catalogue."""
    table_a = contracts.permission_table_10_2_a()
    assert table_a <= set(CATALOGUE) | set(NOT_PERMISSIONS)


# =====================================================================================
# 19.3.7 -- the surface cannot reach a promotion, asserted rather than described
# =====================================================================================
def test_no_training_route_binds_a_permission_chapter_17_forbids() -> None:
    """MOS-SEC-158: the exclusion is "enforced by their absence from the role, never by
    a check in code". A route that BINDS one of those identifiers is a way to reach the
    act whatever the role table later says, and MOS-UI-167 requires the promotion
    control on this surface to be ABSENT rather than merely unauthorised.

    The forbidden set is IMPORTED from chapter 17's own two tuples. A second copy in
    this file would be the copy that stops matching the day MOS-TRAIN-174 grows a sixth
    entry, and this test would then pass while the property it names had lapsed.
    """
    from medos.training.candidate import FORBIDDEN_PIPELINE_PERMISSIONS
    from medos.training.orchestrator import FORBIDDEN_PERMISSIONS

    forbidden = set(FORBIDDEN_PERMISSIONS) | set(FORBIDDEN_PIPELINE_PERMISSIONS)
    assert "artifact.approve" in forbidden, "the check has lost its subject"
    assert forbidden == permcheck.forbidden_on_a_training_route()

    offenders = sorted(
        (entry["operation_id"], entry["permission"])
        for entry in ENTRIES
        if entry.get("surface") in permcheck.TRAINING_SURFACES
        and entry.get("permission") in forbidden
    )
    assert offenders == [], offenders


def test_no_training_route_is_a_governance_or_clinical_action() -> None:
    """MOS-UI-102: every action the console offers MUST be authorised by a permission of
    class `read`, `write` or `phi`, and the console MUST NOT contain a screen, a control
    or a code path requiring `governance` or `clinical`. Applied to the `expert` surface
    too: the ConfigurationSearch and ConversionRun rows are off the console for a
    different reason (MOS-UI-150), and neither is a governance act either.

    R4 (`PUT /tenants/{tenant_id}/training-policy`) binds `training_data_policy.record`,
    which IS `governance`, and is deliberately not marked with a `surface:` -- MOS-UI-110
    forbids the console offering a screen that edits the policy, because the assertion it
    records is the site's legal one and not this operator's.
    """
    offenders = sorted(
        (entry["operation_id"], entry["permission"], CATALOGUE[entry["permission"]]["class"])
        for entry in ENTRIES
        if entry.get("surface") in permcheck.TRAINING_SURFACES
        and CATALOGUE[entry["permission"]]["class"] not in {"read", "write", "phi"}
    )
    assert offenders == [], offenders
    policy = next(e for e in ENTRIES if e["table_row"] == "R4")
    assert policy.get("surface") is None
    assert CATALOGUE[policy["permission"]]["class"] == "governance"


def test_no_route_anywhere_offers_a_promotion_path_from_the_training_surface() -> None:
    """MOS-TRAIN-189 as a property of the declared surface, beside the call-graph form
    `tests/gate/test_no_auto_promote.py` already asserts of the code. No path, method or
    operation id on the model-development surface names an act that changes what serves.
    """
    words = ("promote", "promotion", "deployment", "cutover", "clinical-use", "approve")
    offenders = [
        entry["operation_id"]
        for entry in ENTRIES
        if entry.get("surface") in permcheck.TRAINING_SURFACES
        and any(w in entry["path"].lower() or w in entry["operation_id"].lower() for w in words)
    ]
    assert offenders == [], offenders


def test_the_candidate_projection_names_the_decider_and_offers_no_act() -> None:
    """MOS-UI-168 requires the candidate's terminal state to be rendered as what it is
    and the deciding role to be NAMED. This is that requirement as four `const`s, and the
    constants are what make the answer a property of the platform rather than a per-
    candidate computation a server could one day vary.
    """
    schema = _training_schema("training-run-candidate")
    promotion = schema["properties"]["promotion"]["properties"]
    assert promotion["performed_on_this_surface"]["const"] is False
    assert promotion["permission"]["const"] == "artifact.approve"
    assert promotion["transition"]["const"] == "VALIDATED -> APPROVED"
    assert promotion["requirement"]["const"] == "MOS-TRAIN-008"
    # No link, no href, no action: the object is a statement, not an affordance.
    for name in schema["properties"]:
        assert not any(w in name for w in ("url", "href", "link", "action")), name


def test_the_candidate_lifecycle_enum_is_the_registrys_own_list() -> None:
    """A second copy of a lifecycle vocabulary is a copy that falls behind the first."""
    from medos.registry.lifecycle import LIFECYCLE_STATUSES

    schema = _training_schema("training-run-candidate")
    declared = schema["properties"]["lifecycle_status"]["enum"]
    assert declared == [*LIFECYCLE_STATUSES["model_version"], None]


# =====================================================================================
# 19.3.5 -- what Train may carry, and what it structurally cannot
# =====================================================================================
def test_the_train_request_carries_only_the_four_things_mos_ui_147_shows() -> None:
    """MOS-UI-147: "a read-only summary" of the cohort, the split, the reference standard
    and the target capability, "and one control". The four required members are those
    four; everything else in MOS-TRAIN-124's binding is server-derived, because a value a
    client may supply is a value a client may supply wrongly and MOS-TRAIN-126's claim for
    the record is that these are recorded rather than asserted.
    """
    schema = _training_schema("training-run-submit-request")
    assert schema["required"] == [
        "capability_id",
        "dataset_version_id",
        "split_id",
        "annotation_set_id",
    ]
    assert schema["additionalProperties"] is False


def test_the_train_request_cannot_reach_any_quantity_mos_ui_149_hides() -> None:
    """MOS-UI-149 lists six quantities the console MUST NOT display as editable, MUST NOT
    display as defaults, and MUST NOT reveal behind an "advanced" region. They live in the
    derived plan, in the registered `PreprocessingSpec` and in `TrainingRun.hyperparameters`
    (MOS-TRAIN-224 puts the batch size there). With no such member and a closed object,
    a no-code client cannot reach any of them under any name -- which is the difference
    between a screen that does not offer a control and an API on which none exists.

    The partition members are refused for MOS-TRAIN-141's reason rather than MOS-UI-149's,
    and `medos/medos/training/cohort.py` states it: a caller that can name its own data can name
    the `test` partition. Same test, because the same absence is what enforces both.
    """
    schema = _training_schema("training-run-submit-request")
    declared = set(schema["properties"])
    for forbidden in (
        "hyperparameters",
        "hyperparameters_digest",
        "patch_size",
        "batch_size",
        "target_spacing",
        "normalisation",
        "network_topology",
        "fingerprint",
        "fingerprint_digest",
        "fit_partition",
        "select_partition",
        "partition",
        "read_partitions",
        "path",
        "bucket",
        "prefix",
        "uri",
        "glob",
        "nominated",
        "seeds",
        "hardware",
        "code_commit",
        "image_digest",
    ):
        assert forbidden not in declared, forbidden


def test_a_no_code_client_cannot_submit_monai_supervised_without_a_rationale() -> None:
    """MOS-UI-148 forbids the console offering `monai_supervised`; MOS-TRAIN-211 requires
    a hand-configured run to record a rationale of at least 20 characters naming what the
    auto-configured baseline failed to do. Both hold structurally here:

      * a body with no `training_backend` -- what the console sends, MOS-UI-147 having
        given the operator nothing to choose -- gets the server's auto-configuring
        backend, and the `else` branch forbids a rationale it could not justify;
      * a body naming `monai_supervised` is invalid without a rationale, before a
        connection to the database is opened, let alone a GPU claimed.

    The same biconditional is 0013's `training_runs_rationale_only_when_hand_configured`
    plus its `length(backend_rationale) >= 20`, so the wire and the column cannot drift.
    """
    schema = _training_schema("training-run-submit-request")
    assert "training_backend" not in schema["required"]
    assert schema["properties"]["backend_rationale"]["minLength"] == 20

    rule = next(
        r
        for r in schema["allOf"]
        if r["if"].get("properties", {}).get("training_backend")
    )
    kind = rule["if"]["properties"]["training_backend"]["properties"]["kind"]
    assert kind["const"] == "monai_supervised"
    assert rule["then"]["required"] == ["backend_rationale"]
    assert rule["else"]["not"]["required"] == ["backend_rationale"]

    migration = (ROOT / "medos/medos/db/migrations/0013_training.up.sql").read_text(
        encoding="utf-8"
    )
    assert "length(backend_rationale) >= 20" in migration
    assert (
        "CHECK (training_backend->>'kind' = 'monai_supervised' OR backend_rationale IS NULL)"
        in migration
    )


def test_the_backend_kinds_are_the_migrations_check_and_the_engines_tuple() -> None:
    from medos.training.runs import AUTO_CONFIGURING_BACKENDS, BACKEND_KINDS

    schema = _training_schema("training-run-submit-request")
    assert schema["properties"]["training_backend"]["properties"]["kind"]["enum"] == list(
        BACKEND_KINDS
    )
    # MOS-UI-148's two, named so that a third auto-configuring backend has to be a
    # deliberate edit here rather than an accident of the enum above.
    assert AUTO_CONFIGURING_BACKENDS == {"nnunet", "auto3dseg"}


def test_the_run_states_are_the_migrations_check() -> None:
    schema = _training_schema("training-run")
    assert schema["properties"]["state"]["enum"] == [
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    ]
    migration = (ROOT / "medos/medos/db/migrations/0013_training.up.sql").read_text(
        encoding="utf-8"
    )
    check = "CHECK (state IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED'))"
    assert check in migration


def test_the_run_projection_omits_the_selection_statistic_and_the_hyperparameters() -> None:
    """MOS-TRAIN-222 forbids a selection statistic on any UI surface presenting model
    performance, and 0013's own column comment says `search_trial_score` is "never a
    reported metric". A member on the run resource travels to every client that reads a
    run, including the one MOS-UI-154 forbids it on -- so the statistic lives on the
    ConfigurationSearch, which is the thing it describes, and `selection_margin` is
    projected there for exactly that reason.
    """
    run = _training_schema("training-run")
    assert "search_trial_score" not in run["properties"]
    assert "hyperparameters" not in run["properties"]
    assert "hyperparameters_digest" in run["properties"]
    for location in ("fingerprint_bucket", "fingerprint_object_key", "bundle_bucket",
                     "bundle_object_key"):
        assert location not in run["properties"], location
    search = _training_schema("configuration-search")
    assert "selection_margin" in search["properties"]


# =====================================================================================
# 19.3.6 -- the counter, and the absence of an action
# =====================================================================================
def test_evaluation_gets_no_action_route_on_this_surface() -> None:
    """MOS-UI-162: evaluation "MUST be automatic on `state = SUCCEEDED`" and "MUST NOT be
    an action the operator initiates". MOS-TRAIN-138 and MOS-TRAIN-140 create the bundle
    and the candidate `EvaluationRun` inside the pipeline. So no unsafe method on the
    training surface touches an evaluation, and row 42 (`POST /evaluations`) is not
    declared on it either -- a second create path for the candidate run would be two ways
    to spend the one consumable MOS-TRAIN-216 counts.
    """
    for entry in ENTRIES:
        if entry.get("surface") not in permcheck.TRAINING_SURFACES:
            continue
        if entry["method"] == "GET":
            continue
        assert "evaluation" not in entry["path"], entry["operation_id"]
        assert entry.get("permission") != "evaluation.run", entry["operation_id"]


def test_the_test_exposure_counter_says_on_the_wire_that_it_only_goes_up() -> None:
    """MOS-TRAIN-216 requires the counter never to be resettable and MOS-UI-163 requires
    the console to LABEL it as a count that only goes up. A constant on the wire is a fact
    the server asserts; the same sentence in a console template is a claim one client
    makes and another may not. The database holds the same assertion twice.
    """
    schema = _training_schema("split-test-exposure")
    assert schema["properties"]["monotonic"]["const"] is True
    migration = (ROOT / "medos/medos/db/migrations/0013_training.up.sql").read_text(
        encoding="utf-8"
    )
    assert "split_test_exposure_no_reset" in migration
    assert "split_test_exposure_no_delete" in migration


def test_the_exposure_route_is_keyed_on_a_run_and_not_on_a_digest() -> None:
    """MOS-UI-101: the operator MUST NOT be required to read or write a digest in order to
    complete any task the console offers. MOS-TRAIN-216 keys the counter on
    `(capability_id, split_digest)`, so a path keyed on the pair would be exactly that
    prohibited composition. The run is the handle the operator actually has.
    """
    entry = next(e for e in ENTRIES if e["table_row"] == "R11")
    assert entry["path"] == "/training-runs/{training_run_id}/test-exposure"
    assert "digest" not in entry["path"]


def test_the_seed_variance_response_carries_no_non_inferiority_margin() -> None:
    """MOS-EVID-087 forbids tuning the margin to make a candidate pass; MOS-TRAIN-128
    renders `sd` BESIDE the declared margin and forbids using it to compute, adjust or
    justify one; MOS-UI-157 repeats the prohibition for the console. A member here named
    for the margin would put the two numbers in one document and invite the arithmetic all
    three forbid, so the absence is asserted rather than trusted to the prose above it.
    """
    schema = _training_schema("capability-seed-variance")
    names = {pointer.rsplit("/", 1)[-1] for pointer, _ in _nodes(schema)}
    for forbidden in ("delta", "margin", "non_inferiority_margin", "acceptance_criteria"):
        assert forbidden not in names, forbidden
    assert schema["properties"]["runs_required"]["const"] == 3


# =====================================================================================
# ConfigurationSearch and ConversionRun -- the two rules a schema can carry
# =====================================================================================
def test_the_search_budget_requires_every_bound_mos_train_234_names() -> None:
    """"An absent bound is a registration error, not an unlimited one." A schema that made
    any of the five optional would make *unbounded* expressible, which is the single thing
    that requirement rules out."""
    schema = _training_schema("configuration-search-declare-request")
    assert "budget" in schema["required"]
    assert schema["properties"]["budget"]["required"] == [
        "max_trials",
        "max_gpu_hours",
        "max_wall_clock_hours",
        "max_ensemble_members",
        "max_footprint_bytes",
    ]
    migration = (ROOT / "medos/medos/db/migrations/0013_training.up.sql").read_text(
        encoding="utf-8"
    )
    assert "budget ?& array['max_trials','max_gpu_hours','max_wall_clock_hours'," in migration


def test_the_search_never_reads_or_selects_on_the_test_partition() -> None:
    """MOS-TRAIN-214 walls the search off from `test` and MOS-TRAIN-215 keeps the
    AcceptanceCriteria for a partition no search, no tuning step and no model selection
    ever touched. Neither the request nor the response admits the value anywhere."""
    request = _training_schema("configuration-search-declare-request")
    assert "read_partitions" not in request["properties"]
    assert request["properties"]["selection_partition"]["enum"] == ["tune", "train"]
    response = _training_schema("configuration-search")
    assert response["properties"]["read_partitions"]["items"]["enum"] == ["train", "tune"]
    assert response["properties"]["selection_partition"]["enum"] == ["tune", "train"]


def test_the_conversion_request_cannot_carry_a_tolerance() -> None:
    """MOS-TRAIN-164: the remedy for a failing conversion is a different conversion, never
    a relaxed tolerance on that version, because "a tolerance relaxed to admit one artifact
    silently relaxes it for every future artifact of that family".
    `medos/medos/training/conversion.py` accepts no such argument; a request schema that did
    would reintroduce at the edge what the engine refuses in the middle.
    """
    request = _training_schema("conversion-run-submit-request")
    assert "tolerances" not in request["properties"]
    assert request["properties"]["calibration_partition"]["const"] == "tune"
    # And the response DOES carry them, because a reader has to see the comparison.
    assert "tolerances" in _training_schema("conversion-run")["properties"]["equivalence"][
        "properties"
    ]


# =====================================================================================
# The problem documents these rows will emit -- register entry 80, recorded not resolved
# =====================================================================================
def test_the_declared_problem_types_are_what_the_engine_actually_raises() -> None:
    """The block records what the engine raises, including the places it disagrees with
    the specification. Recording is worth nothing if it can rot, so each entry is checked
    by importing the class its own `raised_by` names and rendering its problem document.

    Resolved through the DOTTED PATH rather than against one fixed module. The seal
    surface raises from two packages -- chapter 17's `medos.sdk.errors`
    for a curation refusal, chapter 7's `medos.sdk.refusal` for a seal or
    a freeze refusal -- and a resolver hard-wired to the first would have made the
    second unrecordable, which is how a block written to stop drift acquires a blind
    spot in the shape of the thing it is not looking at.
    """
    import importlib

    from medos.sdk.refusal import Refusal

    declared = ROUTES["training_problem_types"]
    assert declared, "the block is empty"
    modules = set()
    for slug, body in declared.items():
        module_name, _, name = str(body["raised_by"]).rpartition(".")
        modules.add(module_name)
        klass = getattr(importlib.import_module(module_name), name)
        problem = klass([Refusal(check_id="probe", code="probe", message="probe")])
        rendered = problem.as_problem()
        assert klass.problem_type == body["type_as_raised"], slug
        assert rendered["status"] == body["status"], slug
        assert rendered["class"] == body["class_as_raised"], slug
        assert rendered["type"].endswith("/" + slug), slug
    assert modules == {
        "medos.sdk.errors",
        "medos.sdk.refusal",
    }, modules


def test_the_authority_split_of_register_entry_80_is_still_unresolved_and_named() -> None:
    """DELIBERATELY NOT FIXED HERE, and this test is the reason it can stay that way
    without becoming invisible. MOS-API-036 names `https://spec.medicalos.org/problems/`
    as the only permitted prefix and `medos/medos/api/problems.py` carries it as `PROBLEM_BASE`;
    `medos/medos/training/errors.py` spells every type under `medicalos.dev`. Entry 80 makes the
    decision platform-wide -- MOS-API-035 makes `type` the stable identifier a client
    matches on, so changing twelve is a breaking change and changing one makes the code
    internally inconsistent instead of externally. This change records what the code does.

    When the platform-wide decision lands, THIS TEST FAILS, which is the point: the
    registry block has to be updated in the same change rather than left describing an
    authority nothing emits any more.
    """
    from medos.api.problems import PROBLEM_BASE

    declared = ROUTES["training_problem_types"]
    divergent = [
        slug
        for slug, body in declared.items()
        if not str(body["type_as_raised"]).startswith(PROBLEM_BASE)
    ]
    assert sorted(divergent) == sorted(declared), (
        "some training problem type now matches MOS-API-036's authority and some do not; "
        "register entry 80 is a single decision and a partial migration is worse than "
        "either end of it"
    )
    for body in declared.values():
        assert body["type_as_raised"].startswith("https://medicalos.dev/problems/")


def test_the_class_spelling_of_the_two_403s_is_recorded_as_off_the_closed_enum() -> None:
    """The second half of the same divergence, which entry 80 does not itself name.
    Table 10.4-A is closed at six values (MOS-API-038) and `medos/medos/api/problems.py` carries
    them as `WIRE_CLASSES`; `HarvestRefused` and `PartitionForbidden` render
    `class: authorization`, which is not one of the six, while MOS-API-112 pins
    `authz_error` for `training-use-not-permitted` and the schema holds it as a `const`.
    A handler that renders `as_problem()` straight onto the wire emits a class outside the
    closed enum. Recorded here so the change that serves these rows meets it in a test
    rather than in production.
    """
    from medos.api.problems import WIRE_CLASSES

    declared = ROUTES["training_problem_types"]
    off_enum = {
        slug: body["class_as_raised"]
        for slug, body in declared.items()
        if body["class_as_raised"] not in WIRE_CLASSES
    }
    assert off_enum == {
        "training-use-not-permitted": "authorization",
        "partition-not-readable": "authorization",
    }
    assert declared["training-use-not-permitted"]["class_as_specified"] == "authz_error"
    problem = json.loads(
        (ROOT / "medos/schemas/training/training-use-not-permitted-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )
    assert problem["properties"]["class"]["const"] == "authz_error"


def test_every_problem_a_training_row_names_can_actually_be_raised() -> None:
    """A route that advertises a refusal nothing produces is the declaration-without-a-
    thing this registry refuses elsewhere. R9 is the deliberate opposite case: a cancel of
    an already-terminal run raises `TrainingError`, which carries no `problem_type`, so no
    slug is listed and the entry says the change that serves it owes one."""
    declared = set(ROUTES["training_problem_types"])
    # `api_problem_types` is the second producer, and it is a DIFFERENT kind of thing: no
    # refusal class raises these, `medos/medos/api/` builds them, and
    # `medos/medos/api/problems.py::problem_type_uri` derives the slug from the `code`. That is
    # what makes them raiseable in the sense this test is about, and `medos/tools/permcheck.py`
    # recomputes each slug from its code so the block cannot drift from the handler.
    minted = set(ROUTES.get("api_problem_types") or {})
    assert not (declared & minted), "one slug has one producer or it has none"
    for entry in ENTRIES:
        if entry.get("surface") not in permcheck.TRAINING_SURFACES:
            continue
        for slug in entry.get("problems") or []:
            assert slug in declared or slug in minted or slug in permcheck.SHARED_PROBLEMS, (
                entry["operation_id"],
                slug,
            )
    from medos.api.problems import problem_type_uri

    for slug, body in (ROUTES.get("api_problem_types") or {}).items():
        assert problem_type_uri(str(body["code"])).rsplit("/", 1)[-1] == slug, slug
    cancel = next(e for e in ENTRIES if e["table_row"] == "R9")
    assert "owes one" in cancel["note"]


# =====================================================================================
# The eight keys this change registers, held to the rows that bind them
# =====================================================================================
def test_every_permission_this_change_registers_is_bound_by_a_row() -> None:
    """A permission with no route is a grant nobody can exercise and nobody can audit.
    Chapter 8 carries several for surfaces that do not exist yet; these eight were minted
    for these rows, so each one has to be reachable from one."""
    bound = {e["permission"] for e in ENTRIES if e.get("permission")}
    assert RUN_PERMISSIONS <= bound
    for permission in RUN_PERMISSIONS:
        rows = [e["operation_id"] for e in ENTRIES if e.get("permission") == permission]
        assert rows, permission


def test_the_run_side_permissions_use_the_domains_own_verbs() -> None:
    """The naming discipline MOS-API-112's change established, continued. No `create`:
    MOS-SEC-034 pairs a `create` permission with a removal disposition and names exactly
    four never-deleted resources -- AuditEvent, PolicyDecision, Result and Deployment --
    and `training_runs`, `configuration_searches` and `conversion_runs` are all append-only
    under 0013's `forbid_training_mutation`, so a fifth entry on that list would be owed
    and MOS-SEC-034 is not this change's to edit. No `delete` and no `update` either: the
    binding is sealed at submit (MOS-TRAIN-124) and a re-nomination is a NEW row
    (MOS-TRAIN-217), so neither spelling would name an act the platform can perform. The
    verbs used are the ones chapter 17 and the port already use -- `submit` and `cancel`
    are `Orchestrator.Submit` and `Orchestrator.Cancel` (MOS-TRAIN-122), `nominate` is
    MOS-TRAIN-216's own word, and `declare` is MOS-TRAIN-220's registration.
    """
    for identifier in RUN_PERMISSIONS:
        for verb in (".create", ".delete", ".update", ".write"):
            assert not identifier.endswith(verb), identifier
    assert {i.split(".")[1] for i in RUN_PERMISSIONS} == {
        "read",
        "submit",
        "cancel",
        "declare",
        "nominate",
    }


def test_every_table_row_marks_what_the_registry_says_it_is() -> None:
    """Table 10.2-B and `medos/api/v1/routes.{core,train}.yaml` MUST agree about what is served.

    THE DEFECT THIS REPLACES A TEST FOR. Twenty-six rows of table 10.2-B opened
    "**RESERVED, MUST NOT be served until a handler is registered (`MOS-API-089`)**",
    twenty-one of them describing routes this deployment serves: `POST /api/v1/
    training-runs` answers `401`, which is authentication refusing a request that reached
    a handler, not the `404` an absent route gives. So the normative table asserted a
    prohibition the platform was in breach of, for routes committed in 41bb056.

    The formula also miscited its own requirement. `MOS-API-089` is the rule that
    `routes_gen.go` registers each handler together with the `permission` value from the
    same YAML row, "so documentation cannot drift from enforcement" -- a coupling rule,
    and the one that defect violated rather than the one that licensed it. The test that
    stood here asserted the phrase was present on every row from R6 up, which pinned the
    wrong sentence in place and would have kept it there.

    What is checkable, and is checked instead: the marker on a row and the `status` of the
    matching registry entry say the same thing. `MOS-API-112` reserves exactly five paths
    by name; every other row is served or the registry is lying about the deployment.
    """
    rows = {r.number: r for r in contracts.route_table_10_2_b()}
    by_row = {
        str(e["table_row"]): e for e in ENTRIES if str(e.get("table_row", "")).startswith("R")
    }
    assert by_row, "no registry entry carries a table_row of the R-series"

    for number, entry in sorted(by_row.items()):
        row = rows.get(number)
        assert row is not None, f"{number} has a registry entry and no table 10.2-B row"
        marked_reserved = "RESERVED" in row.purpose
        registry_reserved = entry["status"] == "reserved"
        assert marked_reserved == registry_reserved, (
            f"{number} ({entry['method']} {entry['path']}): table 10.2-B says "
            f"{'RESERVED' if marked_reserved else 'SERVED'} and its product's route "
            f"registry says "
            f"{entry['status']}. One of them is wrong about the deployment."
        )
        if marked_reserved:
            # No row reaches here today. The branch stays because the agreement above is
            # only half a rule: a row may be marked reserved by a requirement that says
            # so, and a row reserved by NOTHING is a row anybody may serve. MOS-API-112 is
            # the only requirement in this chapter that has ever reserved one, and it no
            # longer does.
            assert "MOS-API-112" in row.purpose, number

    # THE WHOLE R-SERIES IS SERVED. Asserted as an equality and not as `not reserved`,
    # because the failure this replaces was a marker nobody recomputed: if a future change
    # reserves a row, this line names it rather than passing quietly.
    assert {n for n, e in by_row.items() if e["status"] == "reserved"} == set()


def _training_schema(slug: str) -> dict[str, Any]:
    return json.loads(
        (ROOT / "medos" / "schemas" / "training" / f"{slug}-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )


# =====================================================================================
# The corpus-assembly surface -- rows R18-R31, and the seal shape they turn on
#
# `POST /api/v1/training-runs` binds a dataset_version_id, a split_id and an
# annotation_set_id, and until this change not one of the three had a producer on
# `/api/v1`. Everything below asserts a property of the DECLARATION, because no handler
# exists yet: a reviewer can read the argument in `medos/api/v1/routes.train.yaml` and in
# MOS-API-112, and these are what stop the argument and the files diverging.
# =====================================================================================
def _evidence_schema(slug: str) -> dict[str, Any]:
    return json.loads(
        (ROOT / "medos" / "schemas" / "evidence" / f"{slug}-1.0.0.json").read_text(
            encoding="utf-8"
        )
    )


def _entry(operation_id: str) -> dict[str, Any]:
    return next(e for e in ENTRIES if e["operation_id"] == operation_id)


#: Every row of the corpus-assembly surface, including the two entries for existing
#: served rows 32 and 37. Used wherever a property has to hold across the whole surface
#: rather than on the one row a reviewer happens to look at.
UPSTREAM_OPERATIONS = (
    "getTenantTrainingPolicy",
    "declareSamplingPlan",
    "listSamplingPlans",
    "getSamplingPlan",
    "listHarvestBatches",
    "getHarvestBatch",
    "listHarvestCandidates",
    "getHarvestBatchSplitPreview",
    "sealHarvestBatch",
    "getSealRun",
    "getDatasetSplit",
    "freezeAnnotationSet",
    "listAnnotationSets",
    "getAnnotationSet",
    "listDatasets",
    "getDatasetVersion",
)


def test_every_permission_the_corpus_assembly_change_registers_is_bound_by_a_row() -> None:
    """A permission with no route is a grant nobody can exercise and nobody can audit."""
    assert UPSTREAM_PERMISSIONS <= set(CATALOGUE)
    assert UPSTREAM_PERMISSIONS <= {p.id for p in contracts.catalogue_8_3_2()}
    bound = {e["permission"] for e in ENTRIES if e.get("permission")}
    for permission in UPSTREAM_PERMISSIONS:
        assert permission in bound, permission


def test_the_upstream_permissions_use_the_domains_own_verbs() -> None:
    """MOS-SEC-034 pairs a `create` permission with a removal disposition and names
    exactly four never-deleted resources. `sampling_plans` is sealed outright by
    MOS-STORE-359 and `harvest_batches` is append-only, so a `create` spelling for either
    would owe a fifth entry on a list chapter 8 owns and this change may not edit. The
    verbs used are the domain's: MOS-TRAIN-083 DECLARES a plan, and reading is `read`.
    """
    for identifier in UPSTREAM_PERMISSIONS:
        for verb in (".create", ".delete", ".update", ".write"):
            assert not identifier.endswith(verb), identifier
    assert {i.split(".")[1] for i in UPSTREAM_PERMISSIONS} == {"read", "declare"}


def test_the_seven_evidence_permissions_are_reused_and_not_re_minted() -> None:
    """MOS-UI-102 names the seeded `evidence_scientist` role as the console's reference
    principal and lists `dataset.*`, `dataset_version.*`, `dataset_split.read/freeze` and
    `annotation_set.*` in it. Every one of the seven spellings rows R26-R31 and rows 32
    and 37 bind was already in section 8.3.2, so this surface minted four keys and not
    eleven. A later change that invents `dataset_version.seal` or `split.freeze` beside
    one of these fails here."""
    assert REUSED_EVIDENCE_PERMISSIONS <= set(CATALOGUE)
    bound = {e["permission"] for e in ENTRIES if e.get("permission")}
    bound |= {p for e in ENTRIES for p in (e.get("also_requires") or [])}
    assert REUSED_EVIDENCE_PERMISSIONS <= bound
    # Chapter 19's own sentence, read rather than paraphrased: if MOS-UI-102 ever stops
    # naming these families in the reference principal, the subset claim this surface
    # rests on has lapsed and this test is where that shows.
    reference_principal = (ROOT / "docs/spec/19-operator-surfaces.md").read_text(
        encoding="utf-8"
    )
    for family in ("`dataset_version.*`", "`dataset_split.read/freeze`", "`annotation_set.*`"):
        assert family in reference_principal, family


def test_the_seal_binds_the_spelling_mos_ui_104_fixes_and_not_row_38s() -> None:
    """MOS-UI-104: chapter 10 row 38 declares `dataset.write`, which section 8.3.2 lists
    among the spellings that are NOT permission identifiers and that a build MUST fail on
    under MOS-SEC-033. MOS-SEC-158 fixes the mapping -- sealing a dataset version is
    `dataset_version.create` -- and the requirement says in terms that the console MUST
    NOT propagate the defective spelling into its route declarations.
    """
    seal = _entry("sealHarvestBatch")
    assert seal["permission"] == "dataset_version.create"
    assert "dataset.write" in NOT_PERMISSIONS
    for operation in UPSTREAM_OPERATIONS:
        entry = _entry(operation)
        names = [entry.get("permission"), *(entry.get("also_requires") or [])]
        for name in names:
            assert name not in NOT_PERMISSIONS, (operation, name)


def test_the_seal_row_records_the_second_permission_mos_api_005_cannot_hold() -> None:
    """MOS-API-005 admits exactly one permission per row and MOS-UI-130 makes the seal ONE
    action spanning two platform operations -- sealing the `DatasetVersion` and freezing
    the `DatasetSplit`. Binding only the first would let a principal who may seal a
    version freeze a split they may not, which is a widening nobody declared. The row
    records the second under `also_requires:` instead of flattening to the weaker of the
    two, both are catalogue keys, and both are named in table 10.2-A."""
    seal = _entry("sealHarvestBatch")
    assert seal["also_requires"] == ["dataset_split.freeze"]
    table_a = contracts.permission_table_10_2_a()
    assert "dataset_version.create" in table_a
    assert "dataset_split.freeze" in table_a
    assert "dataset_split.freeze" in CATALOGUE
    # And it is the ONLY row that needs the escape hatch. A second one is a sign that
    # MOS-API-005 needs widening rather than that this key needs spreading.
    carriers = [e["operation_id"] for e in ENTRIES if e.get("also_requires")]
    assert carriers == ["sealHarvestBatch"], carriers


def test_the_checker_notices_an_also_requires_naming_an_unregistered_key() -> None:
    """The escape hatch is checked exactly as `permission:` is. A conjunct nobody
    validates is a grant that enters the surface without passing section 8.3.2."""
    mutated = copy.deepcopy(ROUTES)
    entry = next(e for e in mutated["routes"] if e["operation_id"] == "sealHarvestBatch")
    entry["also_requires"] = ["dataset.write_everything"]
    findings = permcheck.check(with_code=False, routes_doc=mutated)
    assert any("also_requires" in f and "dataset.write_everything" in f for f in findings)


def test_the_checker_notices_a_governance_permission_reaching_the_console() -> None:
    """MOS-UI-102 admits only `read`, `write` and `phi` on this surface, and the
    `also_requires` conjunct is the one place a `governance` grant could arrive without
    appearing in the row's declared `permission:`."""
    mutated = copy.deepcopy(ROUTES)
    entry = next(e for e in mutated["routes"] if e["operation_id"] == "sealHarvestBatch")
    entry["also_requires"] = ["training_data_policy.record"]
    assert CATALOGUE["training_data_policy.record"]["class"] == "governance"
    findings = permcheck.check(routes_doc=mutated)
    assert any("MOS-UI-102" in f and "sealHarvestBatch" in f for f in findings)


# -------------------------------------------------------------------------------------
# The seal shape: asynchronous, polled, and never a Job
# -------------------------------------------------------------------------------------
def test_the_seal_follows_the_long_running_pattern_and_creates_no_job() -> None:
    """MOS-API-056: every long-running operation MUST be a `POST` returning `202` with a
    `Location` naming a RESOURCE WITH A STATUS FIELD, never a bare operation handle;
    MOS-API-057 polls it on its own resource with `Retry-After`. MOS-API-001 makes
    `POST /api/v1/jobs` the only endpoint that may create a `Job`, and chapter 17 forbids
    the pipeline reaching one independently -- MOS-TRAIN-121 C3 excludes `job.create` from
    the orchestrator account outright. So the seal is its own entity, and `job.create`
    appears on no row of this surface.
    """
    seal = _entry("sealHarvestBatch")
    assert seal["method"] == "POST"
    assert set(seal["responses"]) == {"202"}
    assert seal["responses"]["202"]["schema"] == "SealRun"
    assert seal["responses"]["202"]["headers"] == ["Location", "Retry-After"]

    poll = _entry("getSealRun")
    assert poll["method"] == "GET"
    assert poll["path"] == "/seal-runs/{seal_run_id}"
    assert poll["responses"]["200"]["schema"] == "SealRun"
    assert "state" in _evidence_schema("seal-run")["required"]

    for operation in UPSTREAM_OPERATIONS:
        entry = _entry(operation)
        names = [entry.get("permission"), *(entry.get("also_requires") or [])]
        assert "job.create" not in names, operation
    # And nothing on the surface points at /jobs.
    assert not any(
        _entry(o)["path"].startswith("/jobs") for o in UPSTREAM_OPERATIONS
    )


def test_no_row_freezes_a_split_and_no_body_names_a_partition() -> None:
    """MOS-TRAIN-141: the cohort resolver takes a split id and a partition name and
    nothing else, because a container that can name its own data can name the test
    partition. A standalone freeze route would have to accept `assignments` -- a caller
    stating which patients are in `test` -- so there is none, and MOS-UI-130 is the second
    reason: the freeze is the back half of one operator action. MOS-EVID-028 adds the
    third absence: no seed, anywhere.
    """
    paths = {(e["method"], e["path"]) for e in ENTRIES}
    assert ("POST", "/dataset-splits") not in paths
    seal = "/harvest-batches/{harvest_batch_id}/seal"
    assert not any(
        method == "POST" and "split" in path and path != seal for method, path in paths
    )
    forbidden = (
        "assignments",
        "partition",
        "partitions",
        "fit_partition",
        "select_partition",
        "seed",
        "rng_seed",
        "train_fraction",
        "test_fraction",
        "min_days_between_studies",
        "study_level_split",
        "allow_same_patient",
    )
    for slug in ("seal-run-submit-request", "annotation-set-freeze-request"):
        schema = _evidence_schema(slug)
        for member in forbidden:
            assert member not in schema["properties"], (slug, member)
    plan = _training_schema("sampling-plan-declare-request")
    for member in forbidden:
        assert member not in plan["properties"], member


def test_no_body_on_this_surface_can_write_a_waiver() -> None:
    """MOS-UI-135: the console MUST NOT write a waiver and MUST NOT display a waiver
    control. MOS-TRAIN-115 is why the absence costs the operator nothing -- a waiver on
    L1, L2, L3 or L5 cannot permit a training run at all, so the control would not even
    unblock them. The frozen split still PROJECTS waivers, because MOS-EVID-036 requires
    every one reproduced in full in any report citing the split; reporting one and
    offering one are different acts.
    """
    for slug in ("seal-run-submit-request", "annotation-set-freeze-request"):
        properties = _evidence_schema(slug)["properties"]
        assert "waivers" not in properties, slug
        assert "stratification_waivers" not in properties, slug
    assert "waivers" not in _training_schema("sampling-plan-declare-request")["properties"]
    split = _evidence_schema("dataset-split")
    assert "waivers" in split["properties"]["leakage_report"]["properties"]


def test_the_seal_body_holds_nothing_the_client_could_assert_wrongly() -> None:
    """The one member and the absences, asserted rather than described. Each omission is
    a requirement: MOS-UI-135 the waiver, MOS-UI-137 the PreprocessingSpec, MOS-EVID-021
    and MOS-DATA-021 the de-identification status, MOS-EVID-116 the free-text description,
    MOS-EVID-003 and MOS-EVID-098 the evidence kind and the envelope."""
    schema = _evidence_schema("seal-run-submit-request")
    assert schema["additionalProperties"] is False
    assert list(schema["properties"]) == ["dataset_id"]
    assert schema["required"] == ["dataset_id"]
    for member in (
        "preprocessing_spec",
        "preprocessing_spec_id",
        "deidentification_status",
        "deid_policy_id",
        "uid_mapping_table_id",
        "source_description",
        "evidence_kind",
        "envelope",
    ):
        assert member not in schema["properties"], member


# -------------------------------------------------------------------------------------
# MOS-EVID-037 made unrepresentable to break
# -------------------------------------------------------------------------------------
#: MOS-UI-133's nine rows, in its order.
MOS_UI_133_ROWS = [
    "geometry_admissibility",
    "corpus_composition_blocking",
    "corpus_composition_warning",
    "test_partition_floor",
    "seeded_annotation_ceilings",
    "leakage_key_set_disjointness",
    "leakage_pixel_identity",
    "leakage_near_duplicate",
    "acceptance_binding",
]


def _seal_check() -> dict[str, Any]:
    return _evidence_schema("seal-run")["$defs"]["check"]


def _conditional(schema: dict[str, Any], outcome: str) -> dict[str, Any]:
    """The `then` branch guarded on one `outcome` value."""
    return next(
        clause["then"]
        for clause in schema["allOf"]
        if clause["if"]["properties"].get("outcome", {}).get("const") == outcome
    )


def test_the_check_battery_is_all_nine_rows_of_mos_ui_133_and_cannot_be_short() -> None:
    """MOS-UI-133 tabulates nine checks and MOS-UI-134 requires L3 and L4 to be run as a
    named step rather than a silent wait. A battery that could carry eight entries is a
    battery a client reads as *everything that applied* -- a missing check is not a pass,
    which is the same sentence MOS-TRAIN-088 makes about C1-C7 and MOS-EVID-035 about
    L1-L5."""
    battery = _evidence_schema("seal-run")["properties"]["check_battery"]
    assert battery["minItems"] == 9
    assert battery["maxItems"] == 9
    assert battery["uniqueItems"] is True
    assert _seal_check()["properties"]["check_id"]["enum"] == MOS_UI_133_ROWS
    assert _seal_check()["properties"]["check_id"]["x-medicalos-enum"] == "closed"
    assert _seal_check()["properties"]["order"]["maximum"] == 9


def test_a_check_cannot_report_pass_over_series_it_never_hashed() -> None:
    """THE LOAD-BEARING ASSERTION OF THE WHOLE SEAL SURFACE.

    MOS-EVID-018's `series_pixel_digest` and MOS-EVID-034's `dhash64` are computed by the
    caller that holds the pixels, and `medos/medos/evidence/repo.py::load_records` returns
    `dhash64` as None for every rehydrated record. So on this deployment L3 and L4 will
    be skipped, which MOS-EVID-037 expressly permits -- provided the skip is reported as
    a skip. The risk is not the skip; it is a cohort sealed without near-duplicate
    detection being shown to a no-code operator as SEALED and read by them as CHECKED.

    The schema makes that unrepresentable: `outcome: pass` requires
    `series_without_pixel_evidence` to be exactly 0, so a handler that retrieved no
    pixels cannot emit a valid document claiming L3 or L4 passed. Only the `pass`
    direction is constrained -- a `fail` found among the series that WERE hashed is a
    real finding and forcing it to `skipped` would hide a genuine leak.
    """
    check = _seal_check()
    assert "series_without_pixel_evidence" in check["required"]
    assert check["properties"]["series_without_pixel_evidence"]["type"] == "integer"
    then = _conditional(check, "pass")
    assert then["properties"]["series_without_pixel_evidence"]["const"] == 0
    # The structural skip of MOS-EVID-037 -- a series with fewer than three instances --
    # is counted SEPARATELY and may coexist with `pass`, because that is what
    # `medos/medos/evidence/leakage.py::_l4` actually does: it passes over the eligible
    # remainder. Collapsing the two counters would have made the engine's own output
    # unrepresentable, which is a schema disagreeing with the platform rather than
    # constraining it.
    assert "series_skipped_structural" in check["required"]
    assert "series_skipped_structural" not in then.get("properties", {})


def test_a_skipped_check_must_state_the_reason_and_what_it_costs() -> None:
    """MOS-EVID-037 requires an explicit reason. MOS-UI-105 requires the operator to be
    told, in their own vocabulary, what was refused and what it means -- and a skipped
    L4 is not a refusal, which is exactly why it would otherwise pass unremarked. Both
    members are required together, and both carry a length floor, because an empty string
    satisfies `type: string` and says nothing.
    """
    check = _seal_check()
    then = _conditional(check, "skipped")
    assert set(then["required"]) == {"skipped_reason", "operator_disclosure"}
    assert then["properties"]["skipped_reason"]["type"] == "string"
    assert then["properties"]["operator_disclosure"]["type"] == "string"
    assert check["properties"]["skipped_reason"]["minLength"] >= 20
    assert check["properties"]["operator_disclosure"]["minLength"] >= 40
    assert check["properties"]["outcome"]["enum"] == ["pass", "fail", "warn", "skipped"]


def test_only_a_non_blocking_check_may_warn() -> None:
    """MOS-UI-145 and MOS-TRAIN-092: C4 and C6 describe a corpus that is NARROW, which the
    reader of the eventual report needs to know; C1, C2, C3, C5 and C7 describe a corpus
    that cannot support the claim being made from it. A warning styled like a block
    teaches the operator to ignore blocks -- and a block that returned `warn` would be
    that, one layer lower."""
    then = _conditional(_seal_check(), "warn")
    assert then["properties"]["blocking"]["const"] is False


def test_the_seal_run_reports_the_pixel_boundary_it_read_through() -> None:
    """MOS-TRAIN-068 and MOS-TRAIN-199: the pipeline acquires imaging ONLY as the
    `dataset_export` consumer class, which forces `clean_pixel_data: true`. A const rather
    than a string, because no other value is lawful here and a field that could carry
    `clinical_viewer` is a field somebody will one day set."""
    evidence = _evidence_schema("seal-run")["properties"]["pixel_evidence"]
    assert evidence["properties"]["consumer_class"]["const"] == "dataset_export"
    for member in (
        "series_total",
        "series_with_pixel_digest",
        "series_with_perceptual_hash",
    ):
        assert member in evidence["required"], member


def test_the_seal_is_atomic_from_the_operators_view() -> None:
    """MOS-UI-132: either a sealed `DatasetVersion` and a frozen `DatasetSplit` both
    exist, or nothing was created. MOS-UI-131 is the reason -- a version that seals and
    then fails its split freeze is immutable (MOS-EVID-013), undeletable (MOS-API-011
    answers 409) and useless to an operator who cannot clean it up."""
    schema = _evidence_schema("seal-run")
    succeeded = next(
        clause["then"]
        for clause in schema["allOf"]
        if clause["if"]["properties"].get("state", {}).get("const") == "SUCCEEDED"
    )
    assert succeeded["properties"]["dataset_version_id"]["type"] == "string"
    assert succeeded["properties"]["dataset_split_id"]["type"] == "string"
    refused = next(
        clause["then"]
        for clause in schema["allOf"]
        if clause["if"]["properties"].get("state", {}).get("const") == "REFUSED"
    )
    assert refused["properties"]["refusals"]["minItems"] == 1


def test_the_seal_run_has_no_state_no_route_can_reach() -> None:
    """A value no path can produce is a state no reader can trust. No row cancels a seal,
    so there is no CANCELLED; MOS-UI-158 and MOS-UI-160 are why REFUSED and FAILED are
    separate -- a blocked C-check rendered in the register of a crash teaches the operator
    that the gates are unreliable."""
    states = _evidence_schema("seal-run")["properties"]["state"]["enum"]
    assert "CANCELLED" not in states
    assert {"REFUSED", "FAILED"} <= set(states)
    paths = {(e["method"], e["path"]) for e in ENTRIES}
    assert ("POST", "/seal-runs/{seal_run_id}/cancel") not in paths


# -------------------------------------------------------------------------------------
# The cohort builder: MOS-UI-111 as a schema rather than as a promise
# -------------------------------------------------------------------------------------
def test_the_sampling_plan_body_cannot_express_a_query() -> None:
    """MOS-UI-111 forbids a query language, a free-text search box over metadata, a
    regular-expression field, a boolean expression builder and a saved-query editor, and
    requires every constraint to be expressed by choosing among values the archive
    contains. A wire form with an operator member would leave that rule to the browser.
    The facet selection carries `include` and nothing else."""
    schema = _training_schema("sampling-plan-declare-request")
    facet = schema["$defs"]["facet_selection"]
    assert list(facet["properties"]) == ["include"]
    assert facet["additionalProperties"] is False
    for member in ("exclude", "min", "max", "pattern", "regex", "op", "query", "where"):
        assert member not in facet["properties"], member
    # `null` is a permitted VALUE -- MOS-TRAIN-084's `not recorded`, which MOS-EVID-020
    # forbids serialising as a default and MOS-UI-112 requires rendering as its own facet
    # value.
    assert "null" in facet["properties"]["include"]["items"]["type"]


def test_the_facet_set_is_closed_at_mos_ui_112s_table() -> None:
    """MOS-UI-112: the facet set MUST be exactly the fields the seal-time checks and the
    split generator read, and nothing else -- a facet the checks do not read lets the
    operator build a cohort that fails for a reason they never saw. The four required ones
    are MOS-TRAIN-083's minimum and the same four `0011_curation.up.sql` asserts."""
    strata = _training_schema("sampling-plan-declare-request")["properties"]["strata"]
    assert strata["additionalProperties"] is False
    assert sorted(strata["required"]) == [
        "acquisition_bucket",
        "ran_on_platform",
        "review_outcome",
        "score_band",
    ]
    migration = (ROOT / "medos/medos/db/migrations/0011_curation.up.sql").read_text(
        encoding="utf-8"
    )
    for dimension in strata["required"]:
        assert f"'{dimension}'" in migration, dimension
    for facet in ("modality", "institution_key", "slice_thickness_mm", "study_year"):
        assert facet in strata["properties"], facet


def test_the_plan_body_cannot_lower_either_corpus_floor() -> None:
    """MOS-TRAIN-083 sets the naive floor and MOS-TRAIN-101 the de-novo control floor at
    0.100, which MOS-UI-126 forbids the console offering as an optional extra. A client
    that may supply either may supply zero, so neither is a request member and the
    response reports the server's own values."""
    request = _training_schema("sampling-plan-declare-request")
    assert "min_naive_fraction" not in request["properties"]
    assert "de_novo_control_fraction" not in request["properties"]
    response = _training_schema("sampling-plan")
    assert response["properties"]["de_novo_control_fraction"]["minimum"] == 0.1
    assert "min_naive_fraction" in response["required"]


def test_the_curation_queue_projects_no_field_mos_train_079_forbids() -> None:
    """MOS-TRAIN-079 forbids `PatientName`, `PatientBirthDate`, `AccessionNumber`,
    institution free text and every source-space UID on a candidate; MOS-UI-122 forbids
    the console displaying any of them. This is the row closest to a patient on the whole
    surface, so the property is asserted over the projection rather than trusted to the
    renderer. `institution_key` is MOS-TRAIN-089's HMAC and there is no display-name
    member beside it to tempt a resolver (MOS-UI-113)."""
    schema = _training_schema("harvest-candidate")
    properties = schema["properties"]
    for member in (
        "patient_name",
        "patient_birth_date",
        "accession_number",
        "institution_name",
        "source_study_instance_uid",
        "source_patient_id",
        "institution_display_name",
    ):
        assert member not in properties, member
    assert "patient_key" in properties
    assert "institution_key" in properties
    # MOS-TRAIN-204's closed exclusion vocabulary, and no value whose meaning is a model
    # outcome.
    codes = properties["reason_code"]["enum"]
    for forbidden in ("hard_case", "outlier", "model_performed_poorly", "low_score"):
        assert forbidden not in codes, forbidden
    # MOS-EVID-020: every acquisition field present, every one nullable, none defaulted.
    acquisition = properties["acquisition_profile"]
    assert len(acquisition["required"]) == 13
    for name, body in acquisition["properties"].items():
        assert "null" in body["type"], name


def test_the_split_preview_declares_itself_a_preview_and_creates_nothing() -> None:
    """MOS-UI-116 requires the projected split to come from a server-side evaluation of
    `assign()` and forbids a second implementation in the browser; MOS-UI-117 requires the
    panel to be labelled a preview and the seal-time result to be authoritative. Both are
    consts on the wire rather than words the console is trusted to add. And the preview
    creates nothing: MOS-EVID-028 makes a split a materialised manifest, so a preview that
    minted an id would be a split by another name."""
    schema = _training_schema("split-preview")
    assert schema["properties"]["is_preview"]["const"] is True
    assert schema["properties"]["authoritative"]["const"] is False
    assert schema["properties"]["test_partition_floor"]["const"] == 30
    for member in ("dataset_split_id", "split_id", "public_id", "seed", "split_digest"):
        assert member not in schema["properties"], member
    # MOS-UI-107: the console computes the remedy, not a description of the constraint.
    assert "patients_needed_for_test_floor" in schema["required"]
    assert _entry("getHarvestBatchSplitPreview")["method"] == "GET"


def test_the_annotation_freeze_carries_no_reference_standard_on_the_wire() -> None:
    """A client that supplies the reference standard can supply one no reader produced,
    and MOS-EVID-038's named readers would then be an assertion rather than a record --
    MOS-TRAIN-096 authenticates every annotation session as a `User` precisely so that
    they are not. `tool` is not a member either: MOS-UI-124 has the platform fill it from
    the deployed annotation stack. MOS-UI-123 keeps `reader_id` a User principal, never a
    group, a rota, a shared login, a service account or an email address."""
    schema = _evidence_schema("annotation-set-freeze-request")
    for member in ("entries", "annotations", "masks", "cases", "payload"):
        assert member not in schema["properties"], member
    reader = schema["$defs"]["reader"]
    assert "tool" not in reader["properties"]
    assert reader["properties"]["reader_id"]["pattern"].startswith("^[0-9a-f]{8}-")
    for member in ("group_id", "rota", "email", "service_account", "login"):
        assert member not in reader["properties"], member
    # MOS-TRAIN-110: no edit path exists anywhere on this surface.
    paths = {(e["method"], e["path"]) for e in ENTRIES}
    assert ("PATCH", "/annotation-sets/{annotation_set_id}") not in paths
    assert ("DELETE", "/annotation-sets/{annotation_set_id}") not in paths


def test_the_frozen_split_projects_the_whole_leakage_report() -> None:
    """MOS-EVID-035 stores it verbatim and MOS-EVID-037 makes a `skipped` L4 a permanent
    property of the split. A projection carrying only a verdict would hide, one screen
    after the SealRun made it visible, exactly what the SealRun exists to show."""
    report = _evidence_schema("dataset-split")["properties"]["leakage_report"]
    assert sorted(report["required"]) == ["L1", "L2", "L3", "L4", "L5", "detail", "waivers"]
    outcomes = _evidence_schema("dataset-split")["$defs"]["leakage_outcome"]["enum"]
    assert outcomes == ["pass", "fail", "skipped", "waived"]
    assert _evidence_schema("dataset-split")["properties"]["partition_level"]["const"] == (
        "patient"
    )


# -------------------------------------------------------------------------------------
# What is declared but not buildable yet, said out loud
# -------------------------------------------------------------------------------------
def test_every_row_whose_engine_is_absent_names_the_engine_and_stays_reserved() -> None:
    """`engine_absent:` says the function a handler would call DOES NOT EXIST.

    THE KEY IS NOW UNUSED, AND THAT IS THE ASSERTION. It carried twelve entries while
    `R18`-`R31` were reserved; the change that served them BUILT every engine it named --
    `0015_seal_runs.up.sql` is the `seal_runs` table, `medos/medos/training/split.py` is
    `MOS-TRAIN-112`'s `assign()`, `medos/medos/training/retrieval.py` is the `dataset_export`
    adapter, and `medos/medos/evidence/repo.py`'s read side is the five SELECTs the rest named.
    So the honest state of the registry is that no entry claims an absent engine.

    The check is kept, inverted, for one reason: the key MUST NOT come back on a served
    row. `medos/tools/permcheck.py` refuses that combination and
    `test_the_checker_notices_a_served_row_claiming_its_engine_is_absent` proves the
    refusal fires, so this test asserts only the invariant that survives an empty set --
    every entry carrying the key is reserved and says something substantial.

    WHAT IS STILL ABSENT IS NOT AN ENGINE and is therefore correctly NOT recorded here.
    `R29`'s annotation store (`MOS-TRAIN-095`, chapter 19 section 19.4.3) and the
    Gateway's `dataset_export` de-identification stage (`MOS-DATA-037`) are both unbuilt;
    `freeze_annotation_set` and the retrieval adapter both EXIST. Their absences are
    recorded in the `note:` of the rows that feel them and are rendered on the wire --
    `503` with the variable named, and `check_battery` with `skipped` plus an
    `operator_disclosure`. A key that meant "something upstream of this engine is
    missing" would be a second, vaguer claim in the same field.
    """
    absent = [e for e in ENTRIES if e.get("engine_absent")]
    for entry in absent:
        assert entry["status"] == "reserved", entry["operation_id"]
        assert len(entry["engine_absent"]) > 40, entry["operation_id"]
    # The three engines MOS-API-112 named as owed now exist and are importable.
    from medos.evidence import repo as ev_repo
    from medos.training import retrieval, split

    assert callable(split.assign)
    assert callable(retrieval.DatasetExportRetriever)
    assert callable(ev_repo.get_split)
    seal_sql = (
        ROOT / "medos" / "medos" / "db" / "migrations" / "0015_seal_runs.up.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE seal_runs" in seal_sql


def test_the_checker_notices_a_served_row_claiming_its_engine_is_absent() -> None:
    """The mutation is now the other way round, and it is the one that would happen.

    While `R18`-`R31` were reserved the plausible mistake was flipping a row to `served`
    with its `engine_absent:` still on it. Now that every one of them IS served and the
    key is gone, the plausible mistake is a later change ADDING it back -- a developer
    who finds an engine wanting and records the fact in the registry without noticing
    that a handler is already mounted against the row. Both are the same contradiction:
    a route cannot be serving an engine its own entry says does not exist.
    """
    mutated = copy.deepcopy(ROUTES)
    entry = next(e for e in mutated["routes"] if e["operation_id"] == "getSealRun")
    assert entry["status"] == "served"
    entry["engine_absent"] = (
        "a later change decided the poll driver was not really there after all"
    )
    findings = permcheck.check(with_code=False, routes_doc=mutated)
    assert any("engine_absent" in f and "getSealRun" in f for f in findings)


def test_the_whole_corpus_assembly_surface_is_served() -> None:
    """The registry describes the deployment, not the intention -- and the deployment
    changed. All sixteen upstream entries are `served` by `medos/medos/api/routes_curation.py`,
    and the RUNNING ASGI APP is asked rather than trusted, which is the direction that
    catches a registry flipped to `served` ahead of the handler.

    The path is compared with its parameter NAMES erased, for the reason
    `medos/tools/permcheck.py::_path_shape` gives: `medos/api/v1/routes.{core,train}.yaml`
    carries table 10.2-B's spelling and a handler carries its own, and the difference has no
    effect on the wire.
    """
    from medos.api.training_plane import create_training_app as create_app

    def shape(path: str) -> str:
        return "/".join(
            "{}" if seg.startswith("{") else seg for seg in path.split("/")
        )

    served = {
        (m, shape(getattr(route, "path", "")))
        for route in permcheck.flatten_routes(create_app())
        for m in (getattr(route, "methods", None) or ())
    }
    for operation in UPSTREAM_OPERATIONS:
        entry = _entry(operation)
        assert entry["status"] == "served", operation
        assert (entry["method"], shape(f"/api/v1{entry['path']}")) in served, operation


def test_the_console_surface_stops_at_the_policy_write() -> None:
    """`R4` is served and is NOT a console row, and the difference is enforced, not meant.

    `MOS-UI-110`: the console "MUST NOT offer a screen that edits the policy", because
    "the assertion it records is the site's legal one, not this operator's". `MOS-UI-102`
    says it independently and structurally: no screen, control or code path of that
    surface may require a permission of class `governance`, and
    `training_data_policy.record` is one. So `R4` carries no `surface:` while the other
    four of MOS-API-112's rows do, and `medos/tools/permcheck.py` scopes its class rule to
    entries that carry one -- which is what makes the absence load-bearing instead of an
    omission somebody forgot to fill in.

    Asserted from BOTH sides: `R4` has no surface, and every row that HAS one binds a
    permission whose class `MOS-UI-102` admits.
    """
    catalogue = contracts.load_permissions()["permissions"]
    policy_write = _entry("putTenantTrainingPolicy")
    assert policy_write["status"] == "served"
    assert "surface" not in policy_write, (
        "MOS-UI-110 forbids the console editing the policy; a `surface:` here would say "
        "the opposite and would pull a governance-class key onto the surface MOS-UI-102 "
        "excludes it from"
    )
    assert catalogue[policy_write["permission"]]["class"] == "governance"

    for number in ("R1", "R2", "R3", "R5"):
        entry = next(e for e in ENTRIES if e["table_row"] == number)
        assert entry["status"] == "served", number
        assert entry["surface"] in permcheck.TRAINING_SURFACES, number
        assert catalogue[entry["permission"]]["class"] in ("read", "write", "phi"), number


def test_mos_api_112_settles_the_seal_shape_and_argues_all_three_options() -> None:
    """The requirement had to say something different once the upstream surface existed,
    and what it says is the decision. A reader who disagrees with the outcome can find the
    two rejected options named, with the requirement that rejects each."""
    text = _requirement("MOS-API-112")
    for phrase in (
        "MOS-API-056",
        "MOS-TRAIN-121",
        "MOS-TRAIN-122",
        "MOS-UI-134",
        "MOS-EVID-037",
        "EvaluationRun",
        "seal_runs",
        "assign()",
        "R18",
        "R31",
    ):
        assert phrase in text, phrase
    # The two rejected shapes are named as rejected, not merely unmentioned.
    assert "synchronous" in text.lower()
    assert "job.create" in text
    # And the third option is admitted to be what this deployment will do.
    assert "load_records" in text
    assert "series_not_hashed" in text or "series_without_pixel_evidence" in text


def test_every_declared_engine_exists_or_is_declared_absent() -> None:
    """`engine:` is a note to whoever implements the handler. A note naming a function
    nobody can import is worse than no note -- it reads as a promise that the work is
    already done. Resolved BY IMPORT, for the same reason the permission discovery is:
    a dotted string is not evidence that a symbol exists.

    This check found one. `R18` named `medos.training.policy.current_policy`, which does
    not exist and never did; the function is `live_policy`. The entry had no
    `engine_absent:` beside it, so the registry was quietly asserting that the read half
    of the training policy was already implemented.
    """
    import importlib

    for entry in ENTRIES:
        dotted = entry.get("engine")
        if not dotted or entry.get("engine_absent"):
            continue
        module_name, _, name = str(dotted).rpartition(".")
        module = importlib.import_module(module_name)
        assert hasattr(module, name), (entry["operation_id"], dotted)


def test_the_checker_notices_an_engine_that_does_not_exist() -> None:
    mutated = copy.deepcopy(ROUTES)
    entry = next(e for e in mutated["routes"] if e["operation_id"] == "getHarvestBatch")
    entry["engine"] = "medos.training.curation.get_batch_but_better"
    findings = permcheck.check(routes_doc=mutated)
    assert any("does not exist" in f and "getHarvestBatch" in f for f in findings)
