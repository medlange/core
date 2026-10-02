# SPDX-License-Identifier: Apache-2.0
"""One authorisation check and one actor, shared by every route module.

THE DEFECT THIS CLOSES, AND WHY IT WAS INVISIBLE
-------------------------------------------------
`medos/api/v1/routes.core.yaml` declares `job.create` and `job.read` on the job routes and then
says so itself, in a comment above the rows:

    "`job.create` and `job.read` are declared below and enforced nowhere."

Four routes carry `enforced_in_code: false`. Measured on 2026-09-21: `routes_jobs.py`
contained ZERO permission checks while `routes_curation.py` had 27, `routes_registry.py`
10 and `routes_reviews.py` 15. So the one route that starts clinical work -- the only
job-creation path in the platform (`MOS-API-001`) -- was the one route that asked nothing
of the credential presenting it.

Worse, and separately: the job row recorded `created_by_kind="service_account"`,
`created_by_id="medos-api"` as *literals*. `medos/medos/db/audit.py` derives the AuditEvent
actor from those columns, so every job in the audit trail was attributed to the API's own
service account no matter who authenticated. `MOS-UI-003` requires each action be
"exercised with the operator's own credential and the route's declared permission", and
neither half was true.

WHY A SHARED MODULE AND NOT A FIFTH COPY
-----------------------------------------
`_require` existed in FOUR route modules -- `routes_curation`, `routes_registry`,
`routes_reviews`, `routes_training` -- as four separate definitions of the same function,
and the module that lacked a copy is the module that enforced nothing. That is the failure
mode duplication produces: the check is not a thing the codebase HAS, it is a thing each
module remembered to write, and one forgot. Adding a fifth copy would leave the next
module to remember too.

THIS MODULE IS CORE-SAFE, WHICH CONSTRAINS ITS IMPORTS
-------------------------------------------------------
`routes_curation.py` gets its `_problem` from `routes_training.py`. Both are Train-plane
modules and the core image deletes them (`medos/deploy/compose/Dockerfile`, target `core`), so
that import is fine there and would be fatal here: `routes_jobs.py` is Core. This module
therefore builds its documents from `medos.api.problems` directly, and
`tests/gate/test_core_train_boundary.py` is what keeps that true.

Spec: MOS-API-005 (one declared permission per route), MOS-SEC-008, MOS-SEC-027 and
MOS-SEC-045 (a scope is a CEILING, never a grant), MOS-UI-003, MOS-UI-006 (enforcement is
server-side and only server-side), MOS-SEC-146 (the closed actor.kind set).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from medos.api.auth import principal_of
from medos.api.problems import build_problem, problem_response, trace_id_of
from medos.security.scopes import scope_permits

if TYPE_CHECKING:  # pragma: no cover
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = ["actor_of", "require"]


def require(request: Request, *permissions: str) -> Response | None:
    """`None` when the credential carries EVERY permission named; a problem document else.

    Variadic because `MOS-API-005` admits one permission per route and a small number of
    rows span two (`MOS-UI-130`'s `R26`); the registry records the second under
    `also_requires:` and `medos/tools/permcheck.py` holds it to the same rules.

    WHAT IS ACTUALLY ENFORCED, STATED RATHER THAN DRESSED UP. `medos/medos/security/scopes.py`
    explains that a scope entry is a CEILING and not a grant (`MOS-SEC-027`,
    `MOS-SEC-045`): the effective set is the intersection of the credential's scope, the
    principal's role grants and the manifest's declared permissions. This platform has no
    roles table, so what is checked here is the one term that exists. That is the posture
    `routes_training.py` and `routes_reviews.py` already take, and this module keeps the
    codebase to one answer rather than five.

    A 403 names the missing permission. `MOS-UI-130` forbids the CONSOLE revealing that an
    action spans two operations; it does not forbid the API telling an integrator which
    grant it lacks, and a 403 naming one of two sends them round the loop twice.
    """
    principal = principal_of(request)
    instance = str(request.url.path)
    trace_id = trace_id_of(request)

    if principal is None:
        # Unreachable behind `AuthenticationMiddleware`, which resolves exactly one
        # principal or refuses. Present because a handler that ASSUMES a principal grants
        # access when the middleware is mounted out of order, and a failure that opens a
        # route is not one to leave to ordering.
        return problem_response(
            build_problem(
                status=401,
                code="UNAUTHENTICATED",
                title="Unauthenticated",
                detail="every request reaching this endpoint resolves to one principal",
                problem_class="authz_error",
                instance=instance,
                trace_id=trace_id,
            )
        )

    for permission in permissions:
        if not scope_permits(principal.scope, permission):
            return problem_response(
                build_problem(
                    status=403,
                    code="PERMISSION_DENIED",
                    title="Permission denied",
                    detail=f"this credential does not carry {permission!r}",
                    problem_class="authz_error",
                    instance=instance,
                    trace_id=trace_id,
                    required_permission=permission,
                    required_permissions=list(permissions),
                )
            )
    return None


def actor_of(request: Request) -> tuple[str, str]:
    """`(kind, id)` for the principal that made this request, for an audit row.

    `MOS-SEC-146` closes the `actor.kind` set and `Principal.kind` is already drawn from
    it, so this is a projection and not a translation.

    THERE IS NO FALLBACK, DELIBERATELY. An earlier version of the job route passed the
    literals `("service_account", "medos-api")`, which meant the audit trail named the API
    rather than the operator and did so for every job ever created. A default here would
    reintroduce exactly that: the caller would get a plausible actor when the principal is
    missing, and nothing downstream could tell it from a real one. Raising is correct --
    `AuthenticationMiddleware` guarantees a principal, so no principal is a wiring defect,
    and a wiring defect that silently mislabels an audit row is worse than a 500.
    """
    principal = principal_of(request)
    if principal is None:  # pragma: no cover - see the docstring
        raise RuntimeError(
            "no principal on the request: AuthenticationMiddleware resolves one or "
            "refuses, so this means the middleware is not mounted ahead of this route. "
            "Refusing to attribute the action to a default actor."
        )
    return principal.kind, principal.principal_id
