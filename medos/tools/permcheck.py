# SPDX-License-Identifier: Apache-2.0
"""The permission contract checker, and the one generator MOS-SEC-032 can have here.

    python medos/tools/permcheck.py           # check; exit 1 and print every finding
    python medos/tools/permcheck.py --emit    # regenerate medos/contracts/permissions.generated.json

WHAT IT CHECKS
--------------
Chapter 8 acceptance check 5, as far as this repository can state it:

  * every identifier in `medos/contracts/permissions.yaml` matches MOS-SEC-031's grammar and
    carries one of the six classes of §8.3.1;
  * `permissions:` is EXACTLY section 8.3.2's table -- same identifiers, same grants
    prose, same class, in both directions ("one key per row of the catalogue and no
    others");
  * every `permission:` in the route registries is a key of the catalogue (MOS-SEC-032),
    unless the entry declares `permission_is_registered: false`, which is only accepted
    for a spelling §8.3.2 itself registers as a non-permission;
  * every permission identifier this repository ENFORCES is accounted for -- a catalogue
    key, a `not_permissions` key, or an `unregistered_in_use` key -- and
    `unregistered_in_use` is exactly the remainder, so it cannot silently grow or rot;
  * the three blocks are disjoint;
  * every schema the registries name either resolves to a file that exists or is
    listed in `schemas_pending`, and `schemas_pending` is exactly the unresolved set;
  * every reserved route of MOS-API-112 resolves every schema it names, because
    "a schema in `medos/schemas/`" is one of the three preconditions that requirement sets;
  * the registries agree with table 10.2-B on method, path and permission for
    every entry that names a row, and every reserved row `R*` has an entry;
  * `status:` agrees with the running ASGI app IN BOTH DIRECTIONS -- a `served` entry
    the app does not serve and a `reserved` entry it does are both findings, so the
    registry describes the deployment rather than an intention (MOS-API-089);
  * table 10.2-A names every permission a table 10.2-B row binds, and every identifier
    it names resolves in chapter 8's catalogue (§10.11 acceptance item 12);
  * no route on a `surface:` of the model-development stack binds a permission chapter
    17 forbids that identity holding, or one whose class is not `read`, `write` or
    `phi` (MOS-UI-102, MOS-SEC-158, MOS-TRAIN-174) -- read off chapter 17's own tuples;
  * `training_problem_types` matches what `medos/medos/training/errors.py` raises, type,
    status and `class`, and `api_problem_types` matches what `medos/medos/api/problems.py`
    derives from each declared `code` -- so both blocks are recomputed rather than
    reviewed -- and every problem slug a training row names is shared, declared in one of
    the two, or a finding;
  * the `coverage:` counts are recomputed from the spec and from `medos/medos/api/`, so a
    number in that block can only be made true, never adjusted.

WHAT IT GENERATES, AND WHAT IT CANNOT
--------------------------------------
`medos/contracts/permissions.generated.json`, and nothing else. MOS-SEC-032 names four
targets -- Go constants, the Cedar action schema, the OpenAPI security scheme
descriptions and the seed-role migration -- and this repository has no Go control plane,
no Cedar policies, no generated OpenAPI document and no `roles` table. The header of
`medos/contracts/permissions.yaml` records each absence by name. Emitting a stub for any of
them would be worse than emitting nothing: a generated file nobody consumes still passes
a drift check, which is how a build acquires a green light over a boundary that does not
exist.

The JSON is real, though, and not a mirror for its own sake: `medos/medos/security/scopes.py`
says in its own docstring that it cannot perform MOS-SEC-033's resolution because "the
generated permission catalogue (MOS-SEC-032)" does not exist, and this file is that
input, readable with stdlib `json` and no new dependency. Wiring `scopes.py` to it is a
later change.

Spec: MOS-SEC-031, MOS-SEC-032, MOS-SEC-033, MOS-API-005, MOS-API-085, MOS-API-112.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import contracts
from contracts import (  # noqa: E402
    PERMISSION_CLASSES,
    PERMISSION_RE,
    ROOT,
    catalogue_8_3_2,
    load_permissions,
    load_routes,
    non_permission_paragraph,
    permission_table_10_2_a,
    route_table_10_2_b,
)

__all__ = [
    "GENERATED",
    "SHARED_PROBLEMS",
    "TRAINING_SURFACES",
    "check",
    "enforced_permissions",
    "forbidden_on_a_training_route",
    "generated_catalogue",
    "render_generated",
    "status_disagrees_with_the_app",
    "unregistered_served_routes",
]

GENERATED: Final[Path] = ROOT / "contracts" / "permissions.generated.json"

#: The `surface:` values that mark an entry as part of chapter 19's model-development
#: surface. `training_console` is the no-code surface of §19.3 -- CUT at specification
#: 0.4.0 and retained as the class label for the routes that surface would have driven;
#: `expert` is the `ConfigurationSearch` / `ConversionRun` side that `MOS-UI-150` keeps
#: off it. Both are subject to `forbidden_on_a_training_route()`; the difference is which
#: SCREENS may exist, not which permissions may be reached from a pipeline surface.
TRAINING_SURFACES: Final[frozenset[str]] = frozenset({"training_console", "expert"})


# =====================================================================================
# Where this repository actually enforces a permission
# =====================================================================================
def enforced_permissions() -> dict[str, list[str]]:
    """Identifier -> the sites that pass it to a permission check.

    Enumerated by IMPORTING the tables rather than by grepping for dotted strings. A
    grep cannot tell `job.create` the permission from `job.create` the audit action or
    `result.review.created` the event type -- all three exist in this tree -- and a
    discovery pass that confuses them produces a catalogue with event names in it.

    The gateway's three literals are the exception: they are written inline rather than
    in a table, so they are listed here by hand with their file and are asserted present
    by `tests/unit/test_permission_contract.py`, which reads the files.
    """
    from medos.api.routes_registry import PERMISSIONS as REGISTRY_PERMISSIONS
    from medos.evidence.report import SIGNING_PERMISSION
    from medos.registry.lifecycle import TRANSITION_PERMISSION
    from medos.safety.review import PERMISSIONS as REVIEW_PERMISSIONS
    from medos.training.candidate import (
        FORBIDDEN_PIPELINE_PERMISSIONS,
        PIPELINE_PERMISSIONS,
    )
    from medos.training.orchestrator import FORBIDDEN_PERMISSIONS

    sites: dict[str, list[str]] = {}

    def add(identifiers: Any, where: str) -> None:
        for identifier in identifiers:
            sites.setdefault(identifier, []).append(where)

    add(REGISTRY_PERMISSIONS, "medos/medos/api/routes_registry.py PERMISSIONS")
    add(TRANSITION_PERMISSION.values(), "medos/medos/registry/lifecycle.py TRANSITION_PERMISSION")
    add(REVIEW_PERMISSIONS, "medos/medos/safety/review.py PERMISSIONS")
    add(PIPELINE_PERMISSIONS, "medos/medos/training/candidate.py PIPELINE_PERMISSIONS")
    add(
        FORBIDDEN_PIPELINE_PERMISSIONS,
        "medos/medos/training/candidate.py FORBIDDEN_PIPELINE_PERMISSIONS",
    )
    add(FORBIDDEN_PERMISSIONS, "medos/medos/training/orchestrator.py FORBIDDEN_PERMISSIONS")
    add([SIGNING_PERMISSION], "medos/medos/evidence/report.py SIGNING_PERMISSION")
    add(["study.read", "study.write"], "medos/medos/gateway/app.py _needed")
    add(["phi.admin"], "medos/medos/gateway/app.py quarantine visibility")
    add(["study.read"], "medos/medos/gateway/auth.py consumer-class resolution")

    principals = json.loads(
        # BACK ON `ROOT`, one commit after it moved to `REPO`. `deploy/` was at the
        # repository root when that edit was made and is `medos/deploy/` now: it is the
        # platform's deployment, and it followed the platform. `docs/` did not and is
        # still read off `REPO` -- the specification binds three products.
        (ROOT / "deploy" / "compose" / "gateway-principals.json").read_text(encoding="utf-8")
    )
    for principal in principals["principals"]:
        add(principal["scopes"], "medos/deploy/compose/gateway-principals.json scopes")

    return {k: sorted(set(v)) for k, v in sorted(sites.items())}


def _path_shape(path: str) -> str:
    """A path with its parameter NAMES erased, so `{artifact_id}` and
    `{service_version_id}` compare equal. `medos/api/v1/routes.{core,train}.yaml` carries table 10.2-B's
    spellings and the handlers carry their own; the difference has no effect on the
    wire, and comparing on it would report thirteen phantom mismatches."""
    return "/".join(
        "{}" if segment.startswith("{") else segment for segment in path.split("/")
    )


def _app_factory(plane: str):  # noqa: ANN202 - a factory, typed by its caller
    """The app factory for one product. Static, per `MOS-REL-108`: no import by name
    from a string the caller composed, just two branches a reader can see."""
    if plane == "core":
        from medos.api.app import create_app

        return create_app
    if plane == "train":
        from medos.api.training_plane import create_training_app

        return create_training_app
    raise ValueError(f"unknown plane {plane!r}")


def unregistered_served_routes(
    entries: list[dict[str, Any]], plane: str = "train"
) -> list[tuple[str, str]]:
    """`/api/v1` routes the running app serves that this plane's registry does not carry.

    Read off the real ASGI app, not a list: MOS-API-012's acceptance item 3 is that "a
    handler that is reachable but absent from the generated route table MUST fail", and
    a hand-kept list of served routes is the second copy that drifts.
    """
    # THE APP FOR THIS PLANE, and the registry for this plane. There are two products
    # and two registries; checking one against the other's app is how a both-directions
    # check becomes one nobody trusts -- Core against the full registry would report 33
    # declared-but-unserved rows on a correct deployment.
    create_app = _app_factory(plane)

    declared = {(e["method"], _path_shape(e["path"])) for e in entries}
    missing: list[tuple[str, str]] = []
    for route in create_app().routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if methods is None or not path.startswith("/api/v1"):
            continue
        for method in sorted(methods):
            if method in {"HEAD", "OPTIONS"}:
                continue
            key = (method, _path_shape(path[len("/api/v1"):]))
            if key not in declared:
                missing.append(key)
    return sorted(missing)


def status_disagrees_with_the_app(
    entries: list[dict[str, Any]], plane: str = "train"
) -> list[str]:
    """Entries whose `status:` is not what the running app does. BOTH DIRECTIONS.

    `unregistered_served_routes()` above answers "is every served route declared?".
    This answers the other half -- "is every declaration true?" -- and until it existed
    `served` was a word anybody could write. A registry describes a deployment or it
    describes an intention, and the difference only shows up when somebody declares a
    route they are about to build and then does not build it.

    The `reserved` direction is the one that matters most here: `MOS-API-112` and
    `MOS-API-089` both say a reserved row MUST NOT be served, and a check that only
    looked for undeclared handlers would let a reserved row be quietly mounted.
    """
    # THE APP FOR THIS PLANE, and the registry for this plane. There are two products
    # and two registries; checking one against the other's app is how a both-directions
    # check becomes one nobody trusts -- Core against the full registry would report 33
    # declared-but-unserved rows on a correct deployment.
    create_app = _app_factory(plane)

    served: set[tuple[str, str]] = set()
    for route in create_app().routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", "")
        if methods is None or not path.startswith("/api/v1"):
            continue
        for method in sorted(methods):
            if method in {"HEAD", "OPTIONS"}:
                continue
            served.add((method, _path_shape(path[len("/api/v1"):])))

    out: list[str] = []
    for entry in entries:
        key = (entry["method"], _path_shape(entry["path"]))
        operation = entry.get("operation_id", "<unnamed>")
        if entry.get("status") == "served" and key not in served:
            out.append(
                f"{operation} is declared `status: served` and the app does not serve "
                f"{key[0]} /api/v1{entry['path']}"
            )
        if entry.get("status") == "reserved" and key in served:
            out.append(
                f"{operation} is declared `status: reserved` and the app SERVES "
                f"{key[0]} /api/v1{entry['path']} (MOS-API-089, MOS-API-112)"
            )
    return out


#: Problem slugs that belong to the API layer rather than to one domain, and therefore
#: need no entry in `training_problem_types`. Every other slug a training row names must
#: be one chapter 17's error module can actually raise -- a route that advertises a
#: refusal nothing produces is the same declaration-without-a-thing this registry refuses
#: elsewhere.
SHARED_PROBLEMS: Final[frozenset[str]] = frozenset(
    {
        "schema-violation",
        "not-found",
        "cross-tenant-denied",
        "rate-limited",
        "idempotency-key-conflict",
        "method-not-allowed",
    }
)


def _probe_refusal() -> Any:
    """One `Refusal`, only so that a refusal class can be instantiated to read its
    rendered problem document. `RefusalError` refuses to be constructed with none."""
    from medos.sdk.refusal import Refusal

    return Refusal(check_id="probe", code="probe", message="probe")


def forbidden_on_a_training_route() -> frozenset[str]:
    """Permissions no route of the model-development surface may bind.

    IMPORTED from chapter 17's own two tables rather than restated, because a second
    copy of a forbidden list is the copy that stops matching:

      `medos.training.orchestrator.FORBIDDEN_PERMISSIONS` -- `MOS-TRAIN-121` C3, the
      orchestrator account's exclusions.
      `medos.training.candidate.FORBIDDEN_PIPELINE_PERMISSIONS` -- `MOS-TRAIN-139` and
      `MOS-TRAIN-174`, the pipeline identity's.

    `MOS-SEC-158` makes the exclusion structural -- "enforced by their absence from the
    role, never by a check in code" -- and this function is the API-surface reading of
    the same sentence: a route that BINDS one of these is a way to reach the act
    whatever the role table says, and `MOS-UI-167` requires the promotion control on
    this surface to be absent rather than merely unauthorised.
    """
    from medos.training.candidate import FORBIDDEN_PIPELINE_PERMISSIONS
    from medos.training.orchestrator import FORBIDDEN_PERMISSIONS

    return frozenset(FORBIDDEN_PERMISSIONS) | frozenset(FORBIDDEN_PIPELINE_PERMISSIONS)


# =====================================================================================
# The one generated target
# =====================================================================================
def generated_catalogue(source: dict[str, Any] | None = None) -> dict[str, Any]:
    source = source or load_permissions()
    return {
        "_generated_by": "medos/tools/permcheck.py --emit. DO NOT EDIT.",
        "_source": "medos/contracts/permissions.yaml",
        "_spec": "MOS-SEC-031, MOS-SEC-032, MOS-SEC-033",
        "version": source["version"],
        "classes": list(PERMISSION_CLASSES),
        "permissions": [
            {"id": ident, "class": body["class"], "grants": body["grants"]}
            for ident, body in sorted(source["permissions"].items())
        ],
        "not_permissions": sorted(source["not_permissions"]),
        "unregistered_in_use": sorted(source["unregistered_in_use"]),
    }


def render_generated(source: dict[str, Any] | None = None) -> str:
    """The exact bytes of `medos/contracts/permissions.generated.json`. Deterministic."""
    return json.dumps(generated_catalogue(source), indent=2, ensure_ascii=False) + "\n"


# =====================================================================================
# The checks
# =====================================================================================
def check(
    *,
    with_code: bool = True,
    source: dict[str, Any] | None = None,
    routes_doc: dict[str, Any] | None = None,
) -> list[str]:
    """Every finding, as a list of one-line strings. Empty means the contract holds.

    `source` and `routes_doc` override what is read from disk. They exist so that
    `tests/unit/test_permission_contract.py` can mutate one key and assert this function
    NOTICES: a checker whose only test is that it passes on the committed files is a
    checker nobody has shown to check anything.
    """
    findings: list[str] = []

    def bad(message: str) -> None:
        findings.append(message)

    source = source if source is not None else load_permissions()
    catalogue: dict[str, Any] = source["permissions"]
    not_permissions: dict[str, Any] = source["not_permissions"]
    unregistered: dict[str, Any] = source["unregistered_in_use"]

    # --- MOS-SEC-031, and the six-value class enum -----------------------------------
    for ident, body in catalogue.items():
        if not PERMISSION_RE.match(ident):
            bad(f"MOS-SEC-031: {ident!r} does not match {PERMISSION_RE.pattern}")
        if body.get("class") not in PERMISSION_CLASSES:
            bad(f"§8.3.1: {ident!r} has class {body.get('class')!r}, not one of the six")
        if not (body.get("grants") or "").strip():
            bad(f"{ident!r} has no grants prose")

    # --- one key per row of section 8.3.2, and no others -----------------------------
    spec_rows = {p.id: p for p in catalogue_8_3_2()}
    for ident in sorted(set(spec_rows) - set(catalogue)):
        bad(f"MOS-SEC-032: §8.3.2 has a row for {ident!r} and permissions.yaml has no key")
    for ident in sorted(set(catalogue) - set(spec_rows)):
        bad(f"MOS-SEC-032: permissions.yaml has {ident!r} and §8.3.2 has no row")
    for ident in sorted(set(catalogue) & set(spec_rows)):
        row, entry = spec_rows[ident], catalogue[ident]
        if row.grants != entry["grants"]:
            bad(
                f"{ident!r}: grants differ from §8.3.2 -- "
                f"{entry['grants']!r} vs {row.grants!r}"
            )
        if row.cls != entry["class"]:
            bad(f"{ident!r}: class {entry['class']!r} but §8.3.2 says {row.cls!r}")

    # --- the three blocks are disjoint ------------------------------------------------
    blocks = (("not_permissions", not_permissions), ("unregistered_in_use", unregistered))
    for name, block in blocks:
        for ident in sorted(set(block) & set(catalogue)):
            bad(f"{ident!r} is in both `permissions` and `{name}`; it cannot be both")
    for ident in sorted(set(not_permissions) & set(unregistered)):
        bad(f"{ident!r} is in both `not_permissions` and `unregistered_in_use`")

    # --- `named_in_8_3_2`, both ways --------------------------------------------------
    paragraph = non_permission_paragraph()
    for ident, body in not_permissions.items():
        named = body.get("named_in_8_3_2", True)
        present = f"`{ident}`" in paragraph
        if named and not present:
            bad(f"{ident!r}: claims §8.3.2's paragraph names it; it does not")
        if not named and present:
            bad(f"{ident!r}: claims §8.3.2's paragraph does not name it; it does")

    # --- what the code enforces --------------------------------------------------------
    if with_code:
        sites = enforced_permissions()
        accounted = set(catalogue) | set(not_permissions) | set(unregistered)
        for ident in sorted(set(sites) - accounted):
            bad(
                f"MOS-SEC-032: {ident!r} is enforced at {sites[ident]} and is in no block "
                "of medos/contracts/permissions.yaml"
            )
        remainder = {i for i in sites if i not in catalogue and i not in not_permissions}
        for ident in sorted(set(unregistered) - remainder):
            bad(
                f"`unregistered_in_use` carries {ident!r}, which nothing enforces any more. "
                "Delete the entry, or register the permission."
            )

    # --- the route registries, MERGED: routes.core.yaml + routes.train.yaml ------------------
    routes_doc = routes_doc if routes_doc is not None else load_routes()
    entries: list[dict[str, Any]] = routes_doc["routes"]
    schemas: dict[str, str] = routes_doc["schemas"]
    pending: list[str] = routes_doc["schemas_pending"]

    seen_ids: set[str] = set()
    for entry in entries:
        operation = entry.get("operation_id", "<unnamed>")
        if operation in seen_ids:
            bad(f"MOS-API-024: duplicate operation_id {operation!r}")
        seen_ids.add(operation)
        if entry.get("status") not in {"served", "reserved"}:
            bad(f"{operation}: status must be served or reserved")
        permission = entry.get("permission")
        if permission is None:
            if not entry.get("authn_only"):
                bad(f"MOS-API-005: {operation} declares no permission and is not authn_only")
        elif permission in catalogue:
            if entry.get("permission_is_registered") is False:
                bad(f"{operation}: marked unregistered but {permission!r} is a catalogue key")
        elif permission in not_permissions:
            if entry.get("permission_is_registered") is not False:
                bad(
                    f"MOS-SEC-032: {operation} binds {permission!r}, which §8.3.2 registers "
                    "as a NON-permission, without declaring permission_is_registered: false"
                )
        else:
            bad(
                f"MOS-SEC-032: {operation} binds {permission!r}, which is not a key of "
                "medos/contracts/permissions.yaml"
            )
        for status, target in (entry.get("permission_by_transition") or {}).items():
            if target not in catalogue and target not in unregistered:
                bad(f"{operation}: transition {status} names unknown permission {target!r}")
        # `also_requires` -- MOS-API-005 admits exactly one permission per row, and row
        # R26 cannot honestly satisfy it: MOS-UI-130 makes the seal ONE action spanning
        # `dataset_version.create` and `dataset_split.freeze`. The registry records the
        # second rather than flattening to the weaker of the two, which is how a
        # principal who may seal a version acquires the power to freeze a split. Checked
        # exactly as `permission:` is, so a conjunct cannot be an unregistered spelling.
        for target in entry.get("also_requires") or []:
            if target == permission:
                bad(f"{operation}: also_requires repeats the declared permission {target!r}")
            elif target not in catalogue:
                bad(
                    f"MOS-SEC-032: {operation} also_requires {target!r}, which is not a "
                    "key of medos/contracts/permissions.yaml"
                )
        # An `engine_absent:` note says the function a handler would call DOES NOT EXIST.
        # On a reserved row that is the honest thing to record; on a served row it is a
        # contradiction, and the contradiction is the interesting direction -- a route
        # that went live while its own registry entry still said its engine was missing.
        if entry.get("engine_absent") and entry.get("status") == "served":
            bad(
                f"{operation}: status is served and engine_absent is declared; a route "
                "cannot be serving an engine its own entry says does not exist"
            )

    # --- schemas -------------------------------------------------------------------------
    for name, relative in schemas.items():
        if not (ROOT / relative).is_file():
            bad(f"schemas index: {name!r} -> {relative} does not exist")
    used: set[str] = set()
    for entry in entries:
        request = entry.get("request") or {}
        if request.get("schema"):
            used.add(request["schema"])
        for response in (entry.get("responses") or {}).values():
            if response.get("schema"):
                used.add(response["schema"])
    unresolved = sorted(used - set(schemas))
    if unresolved != sorted(pending):
        bad(
            "schemas_pending is not the unresolved set: "
            f"missing {sorted(set(unresolved) - set(pending))}, "
            f"stale {sorted(set(pending) - set(unresolved))}"
        )
    for entry in entries:
        if entry.get("status") != "reserved":
            continue
        names = [(entry.get("request") or {}).get("schema")] + [
            r.get("schema") for r in (entry.get("responses") or {}).values()
        ]
        for name in [n for n in names if n]:
            if name not in schemas:
                bad(
                    f"MOS-API-112: reserved route {entry['operation_id']} names schema "
                    f"{name!r}, which `medos/schemas/` does not carry"
                )

    # --- routes.yaml against table 10.2-B --------------------------------------------------
    rows = {row.number: row for row in route_table_10_2_b()}
    for entry in entries:
        number = entry.get("table_row")
        if number is None:
            if not entry.get("defined_by"):
                bad(f"{entry['operation_id']}: no table_row and no defined_by")
            continue
        row = rows.get(str(number))
        if row is None:
            bad(f"{entry['operation_id']}: table_row {number!r} is not a row of table 10.2-B")
            continue
        if row.method != entry["method"] or row.path != entry["path"]:
            bad(
                f"{entry['operation_id']}: row {number} is {row.method} {row.path}, "
                f"this entry is {entry['method']} {entry['path']}"
            )
        if row.permission != entry.get("permission"):
            bad(
                f"{entry['operation_id']}: row {number} binds {row.permission!r}, "
                f"this entry binds {entry.get('permission')!r}"
            )
    declared_rows = {str(e.get("table_row")) for e in entries}
    for number, row in rows.items():
        if number.startswith("R") and number not in declared_rows:
            bad(f"MOS-API-112: reserved row {number} ({row.path}) has no routes.yaml entry")

    # --- `engine:` names something that exists, or `engine_absent:` says it does not ----
    # The key is a note to whoever implements the handler, and a note that names a
    # function nobody can import is worse than no note: it reads as a promise that the
    # work is already done. Resolved by IMPORT rather than by grep, for the reason the
    # permission discovery uses -- a dotted string is not evidence that a symbol exists.
    # An entry may be wrong in exactly one direction without failing here: it may declare
    # `engine_absent:` and name a function that does exist, because a function whose
    # signature cannot yet serve the row is a judgement this checker cannot make.
    if with_code:
        import importlib

        for entry in entries:
            dotted = entry.get("engine")
            if not dotted or entry.get("engine_absent"):
                continue
            module_name, _, name = str(dotted).rpartition(".")
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                bad(
                    f"{entry['operation_id']}: engine {dotted!r} names module "
                    f"{module_name!r}, which cannot be imported, and the entry declares "
                    "no engine_absent"
                )
                continue
            if not hasattr(module, name):
                bad(
                    f"{entry['operation_id']}: engine {dotted!r} does not exist, and the "
                    "entry declares no engine_absent"
                )

    if with_code:
        # ONCE PER PRODUCT, each registry against its own app. `entries` is the merged
        # view and is right for every check above -- a permission is a catalogue key
        # whichever deployable serves it -- but these two compare a declaration against
        # a running application, and there are two applications. Run against the merged
        # registry, Core would report 33 rows it does not serve and Train would report
        # none of Core's omissions.
        # THE REGISTRY EACH APP IS ACCOUNTABLE FOR, which is not one file each. Train
        # SERVES Core's routes as well as its own -- it is Core plus routers -- so the
        # registry describing what the Train app serves is both files. Core's is one.
        # Checking Train against routes.train.yaml alone reported all 28 Core routes as
        # unregistered, which is true of the file and false of the product.
        accountable = {
            "core": lambda: contracts.load_plane_routes("core")["routes"],
            "train": lambda: (
                contracts.load_plane_routes("core")["routes"]
                + contracts.load_plane_routes("train")["routes"]
            ),
        }
        for plane, entries_for in accountable.items():
            plane_entries = entries_for()
            for method, path in unregistered_served_routes(plane_entries, plane):
                bad(
                    f"MOS-API-012: {method} /api/v1{path} is served by the {plane} "
                    f"deployable and has no medos/api/v1/routes.{plane}.yaml entry"
                )
            findings.extend(
                f"[{plane}] {f}" for f in status_disagrees_with_the_app(plane_entries, plane)
            )

    # --- table 10.2-A ------------------------------------------------------------------
    # §10.11 acceptance item 12: "Every permission named in table 10.2-A resolves in
    # Chapter 8's catalogue." Checked both ways, because the failure that actually
    # happens is the other one -- a route binding a spelling chapter 10's own permission
    # table never heard of. Scoped to entries that render a row of table 10.2-B, since
    # 10.2-A is by its own title "permissions referenced by the route table"; the two
    # chapter 6 §6.11 status routes bind `artifact.status.set`, which 10.2-A does not
    # name and does not have to, and re-pointing that is chapter 10's edit.
    table_a = permission_table_10_2_a()
    for ident in sorted(table_a - set(catalogue) - set(not_permissions)):
        bad(
            f"§10.11 item 12: table 10.2-A names {ident!r}, which is no key of "
            "medos/contracts/permissions.yaml"
        )
    for entry in entries:
        if entry.get("table_row") is None:
            continue
        bound = [entry["permission"]] if entry.get("permission") else []
        bound += list(entry.get("also_requires") or [])
        for permission in bound:
            if permission not in table_a:
                bad(
                    f"{entry['operation_id']}: binds {permission!r} on table 10.2-B row "
                    f"{entry['table_row']}, and table 10.2-A does not name it"
                )

    # --- the training surface's forbidden permissions -----------------------------------
    # MOS-UI-102 (class read/write/phi only) and MOS-SEC-158 / MOS-TRAIN-174 (the acts no
    # non-human principal and no training surface may reach). Both are read off chapter
    # 17's own tuples, so a change to either lands here rather than drifting.
    if with_code:
        forbidden = forbidden_on_a_training_route()
        for entry in entries:
            if entry.get("surface") not in TRAINING_SURFACES:
                continue
            # `also_requires` is held to the same two rules as `permission`. A second
            # permission a route needs is a permission the surface's principal must
            # hold, so exempting it would let a `governance` grant onto the console
            # through the one key MOS-API-005 does not make the row declare.
            for permission in [entry.get("permission"), *(entry.get("also_requires") or [])]:
                if permission is None:
                    continue
                if permission in forbidden:
                    bad(
                        f"MOS-SEC-158: {entry['operation_id']} is on the "
                        f"{entry['surface']} surface and binds {permission!r}, which "
                        "chapter 17 forbids that identity holding"
                    )
                entry_class = (catalogue.get(permission) or {}).get("class")
                if entry_class not in {"read", "write", "phi"}:
                    bad(
                        f"MOS-UI-102: {entry['operation_id']} is on the "
                        f"{entry['surface']} surface and binds {permission!r}, class "
                        f"{entry_class!r}; only read, write and phi are admitted"
                    )

    # --- the problem types the training rows name ----------------------------------------
    # `training_problem_types` records what medos/medos/training/errors.py RAISES, including the
    # two respects in which it disagrees with the specification (register entry 80 and the
    # `class` spelling). Recording is only worth something if it cannot rot, so the block
    # is checked against the classes by import, and every slug a training row names must
    # appear in it.
    if with_code:
        declared_problems: dict[str, Any] = routes_doc.get("training_problem_types") or {}
        # Resolved through the DOTTED PATH the entry gives rather than against one fixed
        # module. The seal surface raises from two packages -- chapter 17's
        # `medos.sdk.errors` for a curation refusal and chapter 7's
        # `medos.sdk.refusal` for a seal or freeze refusal -- and a resolver that
        # assumed the first would have made the second unrecordable, which is how a
        # block that exists to stop drift acquires a blind spot shaped like the thing
        # it is not looking at.
        import importlib

        for slug, body in declared_problems.items():
            dotted = str(body["raised_by"])
            module_name, _, name = dotted.rpartition(".")
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                bad(f"training_problem_types[{slug}]: {module_name} cannot be imported")
                continue
            klass = getattr(module, name, None)
            if klass is None:
                bad(f"training_problem_types[{slug}]: {dotted} does not exist")
                continue
            if klass.problem_type != body["type_as_raised"]:
                bad(
                    f"training_problem_types[{slug}]: raises "
                    f"{klass.problem_type!r}, the block says {body['type_as_raised']!r}"
                )
            problem = klass([_probe_refusal()]).as_problem()
            if problem["status"] != body["status"]:
                bad(
                    f"training_problem_types[{slug}]: raises status "
                    f"{problem['status']!r}, the block says {body['status']!r}"
                )
            if problem["class"] != body["class_as_raised"]:
                bad(
                    f"training_problem_types[{slug}]: raises class "
                    f"{problem['class']!r}, the block says {body['class_as_raised']!r}"
                )
        # `api_problem_types` is the second half, and it is checked the same way by a
        # different route. These slugs have no refusal class to import: they are
        # `build_problem(code=...)` calls in `medos/medos/api/`, and
        # `medos/medos/api/problems.py::problem_type_uri` makes the slug a pure function of the
        # code -- "one mapping rather than a hand-maintained table, because a table is how
        # `type` and `code` drift apart". So the check recomputes the slug from the code
        # rather than trusting the block, which is the same property `training_problem_types`
        # gets from importing the class.
        from medos.api.problems import problem_type_uri

        api_problems: dict[str, Any] = routes_doc.get("api_problem_types") or {}
        for slug, body in api_problems.items():
            derived = problem_type_uri(str(body["code"])).rsplit("/", 1)[-1]
            if derived != slug:
                bad(
                    f"api_problem_types[{slug}]: code {body['code']!r} derives "
                    f"{derived!r}; MOS-API-037 makes `code` stable per `type` only "
                    "because one is a function of the other"
                )
            if not str(body.get("minted_by", "")).startswith("medos/medos/api/"):
                bad(
                    f"api_problem_types[{slug}]: `minted_by` must name the medos/medos/api "
                    "site that builds it, so a reader can go and check"
                )
        overlap = sorted(set(api_problems) & set(declared_problems))
        if overlap:
            bad(
                f"{overlap} are declared in BOTH training_problem_types and "
                "api_problem_types; one slug has one producer or it has none"
            )

        for entry in entries:
            if entry.get("surface") not in TRAINING_SURFACES:
                continue
            for slug in entry.get("problems") or []:
                if slug in SHARED_PROBLEMS or slug in declared_problems:
                    continue
                if slug in api_problems:
                    continue
                bad(
                    f"{entry['operation_id']}: names problem {slug!r}, which is neither a "
                    "shared problem nor an entry of training_problem_types or "
                    "api_problem_types"
                )

    # --- coverage ---------------------------------------------------------------------------
    coverage: dict[str, Any] = routes_doc["coverage"]
    served_rows = [n for n in rows if not n.startswith("R")]
    reserved_rows = [n for n in rows if n.startswith("R")]
    with_entry = {str(e["table_row"]) for e in entries if e.get("table_row") is not None}
    actual = {
        "served_entries": sum(1 for e in entries if e["status"] == "served"),
        "reserved_entries": sum(1 for e in entries if e["status"] == "reserved"),
        "table_10_2_b_served_rows": len(served_rows),
        "table_10_2_b_reserved_rows": len(reserved_rows),
        "table_10_2_b_rows_without_an_entry": len(
            [n for n in served_rows if n not in with_entry]
        ),
    }
    if with_code:
        actual["routes_served_by_medos_api_without_an_entry"] = len(
            unregistered_served_routes(entries)
        )
    for key, value in actual.items():
        if coverage.get(key) != value:
            bad(f"coverage.{key} says {coverage.get(key)!r}; the files say {value!r}")

    # --- the generated file -------------------------------------------------------------------
    expected = render_generated(source)
    if not GENERATED.is_file():
        bad(f"MOS-SEC-032: {GENERATED.relative_to(ROOT)} is missing; run --emit")
    elif GENERATED.read_text(encoding="utf-8") != expected:
        bad(
            f"MOS-SEC-032: {GENERATED.relative_to(ROOT)} differs from a fresh generation; "
            "run `python medos/tools/permcheck.py --emit`"
        )

    return findings


def main(argv: list[str]) -> int:
    if "--emit" in argv:
        GENERATED.write_text(render_generated(), encoding="utf-8")
        print(f"wrote {GENERATED.relative_to(ROOT)}")
        return 0
    findings = check()
    for finding in findings:
        print(f"FAIL  {finding}")
    if findings:
        print(f"\n{len(findings)} finding(s).")
        return 1
    print("permission contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
