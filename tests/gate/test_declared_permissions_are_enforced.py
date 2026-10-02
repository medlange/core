# SPDX-License-Identifier: Apache-2.0
"""A route that DECLARES a permission must CHECK it. Derived, not restated.

WHAT WENT WRONG WITHOUT THIS
-----------------------------
`medos/api/v1/routes.core.yaml` declared `job.create` on `POST /api/v1/jobs` and `job.read` on
the three job-read routes, and not one of them checked anything. Measured on 2026-09-21:
`routes_jobs.py` contained ZERO permission checks while `routes_curation.py` had 27,
`routes_registry.py` 10 and `routes_reviews.py` 15. The registry even carried the
admission in a comment -- "declared below and enforced nowhere" -- and four rows said
`enforced_in_code: false`.

So the gap was known, written down, and still open, because `enforced_in_code` was a
FIELD SOMEBODY TYPED rather than a fact anybody derived. A hand-maintained boolean that
claims code does something is worth exactly as much as the next person's memory.

WHAT THIS CHECKS, AND WHY IT IS STRUCTURAL RATHER THAN A LIST
---------------------------------------------------------------
For every route in both registries that names a `permission:`, this walks the handler's
own source for a call to the shared `require(...)` (or a module-local `_require(...)`,
which four route modules still define privately) naming that exact permission string.

It is deliberately a SOURCE check and not a live request. A live check would need a
server, a database and a credential per route, would be slow, and -- worse -- would pass
for a route nobody remembered to add to the test. Reading the registry means a route added
tomorrow is covered the moment its row exists, which is the property that makes this a
gate rather than a snapshot.

WHAT IT CANNOT CATCH, STATED SO NOBODY OVER-TRUSTS IT
------------------------------------------------------
That the check is REACHED. A `require()` call after an early `return` would satisfy this
file and enforce nothing. Catching that needs the live request, and
`tests/integration/test_auth.py` is where that belongs. This gate catches the failure that
actually happened -- the check being absent entirely -- and says plainly that it is not
the whole of the claim.

Spec: MOS-API-005, MOS-UI-003, MOS-UI-006, MOS-REL-050 (a control that looks like an
enforcement point and enforces nothing is worse than its absence).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
API = REPO / "medos" / "medos" / "api"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.contracts import load_routes  # noqa: E402

pytestmark = pytest.mark.gate_0_3_0


def _all_rows() -> list[dict]:
    """EVERY route row, permission or not.

    Path spelling is independent of authorisation: `patchModelVersion` carries
    `permission: null` and still publishes a path an integrator reads. Scoping the
    path-drift check to permissioned rows left two PATCH routes un-renamed and registered
    the old spelling alongside the new one -- two live routes for one resource, which the
    in-container route dump caught and this file had not.
    """
    return list(load_routes()["routes"])


def _registry_rows() -> list[dict]:
    """Every route row that names a `permission:`, from BOTH registries.

    Read through `tools.contracts.load_routes` -- the canonical loader
    `medos/tools/permcheck.py` and the other contract checks already use -- rather than a
    reader written here.

    THE FIRST VERSION OF THIS FUNCTION HAND-PARSED THE YAML AND RETURNED ZERO ROWS. Every
    assertion below passed, instantly, against nothing: the field is `operation_id` and the
    reader looked for `operationId`. A gate that parses its own input is a gate that can be
    green because it found no work, which is the failure this whole file exists to catch,
    one level up. Hence `test_declared_permissions_are_enforced_check_has_teeth` below,
    which fails if the row count collapses again.
    """
    return [r for r in load_routes()["routes"] if r.get("permission")]


def _shape(path: str) -> str:
    """`/model-versions/{model_version_id}` -> `/model-versions/{}`.

    Path PARAMETER NAMES are not part of the route: `/x/{a}` and `/x/{b}` match the same
    requests. They do diverge here -- the registry writes `{model_version_id}` where the
    handler binds `{artifact_id}` -- and that divergence is a real registry defect, but it
    is a DIFFERENT defect from an unenforced permission. Normalising for the match keeps
    this gate answering one question;
    `test_declared_permissions_are_enforced_registry_and_code_agree_on_path_parameters`
    reports the other.
    """
    return re.sub(r"\{[^}]*\}", "{}", path)


def _handlers() -> dict[tuple[str, str], tuple[Path, str, str, str]]:
    """`(METHOD, shape)` -> `(file, function name, source, literal path)`.

    KEYED ON THE ROUTE BINDING, NOT ON THE NAME. The first version matched the registry's
    `operation_id` against the Python function name, with a camelCase-to-snake_case
    fallback. That is not a rule this codebase follows: `getJobSeriesSelection` is served
    by `get_series_selection`, and eleven rows landed in a "no handler of that name" bucket
    that looked like missing routes and were nothing of the sort.

    The decorator is the actual binding -- `@router.get("/jobs/{job_id}/series-selection")`
    is what FastAPI registers -- so matching on `(method, path)` asks the question the
    registry is really making a claim about. It also means renaming a handler cannot break
    this gate, and adding a route to a registry without binding it genuinely can.
    """
    out: dict[tuple[str, str], tuple[Path, str, str, str]] = {}
    verbs = {"get", "post", "put", "patch", "delete"}
    for path in sorted(API.glob("routes_*.py")):
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines(keepends=True)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = "".join(lines[node.lineno - 1 : node.end_lineno])
            for dec in node.decorator_list:
                call = dec if isinstance(dec, ast.Call) else None
                func = call.func if call else dec
                if not isinstance(func, ast.Attribute) or func.attr not in verbs:
                    continue
                if not call or not call.args or not isinstance(call.args[0], ast.Constant):
                    continue
                literal = str(call.args[0].value)
                # The LITERAL is taken from the AST, not by grepping `body`. `body` starts
                # at `node.lineno`, which for a decorated function is the `def` line -- the
                # decorators sit above it and are not in the slice. A regex over `body`
                # therefore finds no decorator at all, which is exactly how the path-drift
                # test below first shipped green against an empty dict.
                out[(func.attr.upper(), _shape(literal))] = (path, node.name, body, literal)
    return out


def _enforces(source: str, permission: str) -> bool:
    """Does this handler's source pass `permission` to a require call?

    A SPLAT COUNTS. `routes_curation.py::seal_harvest_batch` writes
    `_require(request, "dataset_version.create", *SEAL_ALSO_REQUIRES)`, where the second
    declared permission lives in a module constant. A literal-only matcher reported that
    route as unenforced -- a false positive, and the worst kind: it sends somebody to
    "fix" a route that was already correct, and a gate that cries wolf gets muted.

    Resolving the constant would mean importing the module, which needs psycopg and a
    database. Treating a splat as satisfying the remaining declared permissions is the
    honest trade, and it is recorded here as a KNOWN HOLE: a handler splatting an empty or
    wrong tuple passes this gate. `medos/tools/permcheck.py` checks the registry side of that
    pairing against the running app.
    """
    for call in re.finditer(r"\b_?require\s*\(([^)]*)\)", source, re.S):
        args = call.group(1)
        if f'"{permission}"' in args or f"'{permission}'" in args:
            return True
        if "*" in args:
            return True
    return False


def test_declared_permissions_are_enforced_on_every_route_that_declares_one() -> None:
    """THE DEFECT, closed. Four job routes declared a permission and checked nothing."""
    handlers = _handlers()
    unenforced: list[str] = []
    unmatched: list[str] = []

    for row in _registry_rows():
        op = row["operation_id"]
        key = (str(row.get("method", "")).upper(), _shape(str(row.get("path", ""))))
        found = handlers.get(key)
        if found is None:
            unmatched.append(f"{key[0]} {key[1]} -> {op}")
            continue
        path, fn_name, source, _literal = found
        needed = [row["permission"]]
        if row.get("also_requires"):
            needed.extend(
                row["also_requires"] if isinstance(row["also_requires"], list)
                else [row["also_requires"]]
            )
        for permission in needed:
            if not _enforces(source, permission):
                unenforced.append(
                    f"{row.get('method', '?').upper():6} {row.get('path', '?'):52} "
                    f"declares {permission!r} but {path.name}::{fn_name} never checks it"
                )

    assert not unenforced, (
        "these routes declare a permission the handler does not check:\n  "
        + "\n  ".join(sorted(unenforced))
        + "\n\nAdd `denied = require(request, <permission>)` from medos.api.authz and return "
        "it when it is not None. A declared-and-unenforced permission is MOS-REL-050's "
        "control that looks like an enforcement point and enforces nothing."
    )
    # Reported separately: an operationId with no handler is a registry defect, not an
    # authorisation one, and conflating them hides whichever is rarer.
    assert not unmatched, (
        "these registry rows name an operationId with no handler of that name:\n  "
        + "\n  ".join(sorted(unmatched))
    )


def test_declared_permissions_are_enforced_so_the_registry_flag_is_never_false() -> None:
    """`enforced_in_code: false` is now a contradiction, and this is what says so.

    The field was how the gap was recorded while it was open. With the check above
    deriving the answer, a `false` means either the field is stale or the route regressed;
    either way the registry and the code disagree and somebody has to look.
    """
    stale = [
        f"{name}:{n}"
        for name in ("routes.core.yaml", "routes.train.yaml")
        for n, line in enumerate(
            (REPO / "medos" / "api" / "v1" / name).read_text(encoding="utf-8").splitlines(), 1
        )
        if line.strip() == "enforced_in_code: false"
    ]
    assert not stale, (
        "rows still claim the permission is unenforced:\n  " + "\n  ".join(stale)
        + "\nEither the route enforces it -- in which case set true -- or it does not, in "
        "which case the test above should have failed first."
    )


def test_declared_permissions_are_enforced_on_the_four_job_routes() -> None:
    """Named, because these four are the ones that were open, and a generic gate going
    green tells you nothing about which specific hole closed."""
    handlers = _handlers()
    for method, route, permission in (
        ("POST", "/jobs", "job.create"),
        ("GET", "/jobs/{job_id}", "job.read"),
        ("GET", "/jobs/{job_id}/series-selection", "job.read"),
        ("GET", "/jobs/{job_id}/events", "job.read"),
    ):
        found = handlers.get((method, _shape(route)))
        assert found is not None, f"{method} {route} is gone; restore it or update this test"
        assert _enforces(found[2], permission), (
            # `{method} {route}`, not `{op}`: there is no `op` in this scope, so the
            # f-string raised NameError the moment the assertion failed -- which is the
            # only moment it is ever evaluated. The gate could report a pass or a crash
            # and nothing else. ruff F821 saw it; no run ever could.
            f"{method} {route} does not check {permission!r}. This is the exact defect "
            f"the gate exists for: POST /api/v1/jobs is the only job-creation path in "
            f"the platform "
            f"(MOS-API-001) and was the only route asking nothing of its credential."
        )


def test_declared_permissions_are_enforced_and_the_job_records_the_real_principal() -> None:
    """`created_by` must come from the request, not from a literal.

    `medos/medos/db/audit.py` derives the AuditEvent actor from `jobs.created_by_kind` and
    `created_by_id`. Those were the literals "service_account"/"medos-api", so every job
    in the audit trail named the API instead of the operator -- `MOS-UI-003` requires the
    action be exercised with the operator's own credential, and the row is where that
    becomes checkable afterwards.
    """
    source = (API / "routes_jobs.py").read_text(encoding="utf-8")
    assert 'created_by_id="medos-api"' not in source, (
        "the job route still hardcodes the API's own service account as the actor"
    )
    assert "actor_of(request)" in source, (
        "the job route does not derive its actor from the request principal"
    )


def test_declared_permissions_are_enforced_check_has_teeth() -> None:
    """The gate must actually find routes to check.

    Written because the first version of `_registry_rows` returned ZERO and every test in
    this file passed in 0.56 seconds against an empty list. A floor rather than an exact
    count, so adding a route does not fail it; low enough to be obviously safe, high enough
    that a parser returning nothing cannot slip through.
    """
    rows = _registry_rows()
    assert len(rows) >= 40, (
        f"only {len(rows)} registry rows name a permission. The registries declare ~59; a "
        f"number near zero means the loader stopped matching the file and every assertion "
        f"above is passing against nothing."
    )
    assert len(_handlers()) >= 50, "route handler discovery returned almost nothing"


def test_declared_permissions_are_enforced_registry_and_code_agree_on_path_parameters() -> None:
    """The registry and the handler should spell a path parameter the same way.

    Found while building the gate above: `medos/api/v1/routes.core.yaml` declares
    `/model-versions/{model_version_id}` while `routes_registry.py` binds
    `/model-versions/{artifact_id}`. Routing is unaffected -- the name is local to the
    handler -- but the registry is the document an integrator reads, and the generated
    OpenAPI carries the code's spelling, so the two disagree in public.

    Its own test rather than folded into the enforcement check, because they are different
    defects and one red gate would hide whichever was rarer.
    """
    bound = {
        key: literal for key, (_f, _fn, _src, literal) in _handlers().items()
    }

    drift: list[str] = []
    for row in _all_rows():
        method = str(row.get("method", "")).upper()
        declared = str(row.get("path", ""))
        code_path = bound.get((method, _shape(declared)))
        if code_path and code_path != declared:
            drift.append(f"{method:6} registry {declared!r} != code {code_path!r}")

    assert not drift, (
        "the registry and the handlers spell path parameters differently:\n  "
        + "\n  ".join(sorted(set(drift)))
        + "\nRouting is unaffected; the published contract is not. Rename one side."
    )
