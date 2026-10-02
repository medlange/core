# SPDX-License-Identifier: Apache-2.0
"""`GET /users/me` -- who is calling, and what that credential may do.

WHY THIS ROUTE EXISTS AND WHY IT IS SEPARATE
----------------------------------------------
Chapter 10 table 10.2-B row 7: `GET /users/me`, "Identity and effective permissions of the
caller", permission "— (authn only)", response `200 Principal`. It was in the table and
absent from `medos/api/v1/routes.core.yaml`, so no surface had a server answer for "signed in
as", and `MOS-UI-006`'s one permitted client-side courtesy -- "a surface MAY hide or
disable a control the operator's permissions do not allow" -- had nothing to read.

It is its own module rather than a row in `routes_jobs.py` because rows 5, 6, 8 and 9 of
that same table are the user-management surface (`/users`, `/users/{id}`), and they need a
`users` table that does not exist. Putting `me` beside them from the start means the module
grows the rest of its rows in one place instead of being moved later.

AUTHN ONLY, AND THAT IS A DECISION RATHER THAN AN OMISSION
------------------------------------------------------------
Every other route in the registry declares a permission. This one deliberately does not,
because requiring a permission to discover your own permissions is a loop: a credential
with an empty scope could not learn that its scope is empty, and the surface would render
an authenticated-but-blank screen with no way to explain itself. The table says "authn
only" and that is what is implemented. `AuthenticationMiddleware` still applies -- there is
no anonymous access here -- so the route answers 401 before it answers anything else.

WHAT IT MUST NOT RETURN
------------------------
`Principal.log_fields()` already fixes the safe subset and its docstring says why: "No
secret, and no display name or email even when a later block adds them -- those are P2 data
under section 8.6.1. Ids and the public key id only (`MOS-SEC-105`, `MOS-SEC-136`)." This
route returns that subset plus the scope, and nothing else. In particular it does NOT
return the credential's secret, and `credential_key_id` is included only because
`MOS-SEC-136` says in as many words that a key id MAY be logged -- it is the public half,
and it is what makes "revoke the key that did this" a sentence somebody can act on.

THE SCOPE IS A CEILING, AND THE RESPONSE SAYS SO
--------------------------------------------------
`medos/medos/security/scopes.py` (`MOS-SEC-027`, `MOS-SEC-045`): the effective permission set is
the INTERSECTION of the credential's scope, the principal's role grants and the executing
manifest's declared permissions. This platform has no roles table, so the scope is the only
term that exists. Returning it under a field called `permissions` would assert an effective
set that has not been computed; it is returned as `scope` with `scope_is_a_ceiling: true`,
so a surface that hides a control on the strength of it is making a hint and knows it.
`MOS-UI-006` requires the surface to render whatever the API answers when the control is
exercised anyway, which is the enforcement point.

Spec: MOS-API-003, MOS-SEC-008, MOS-SEC-027, MOS-SEC-045, MOS-SEC-105, MOS-SEC-136,
MOS-UI-003, MOS-UI-006, chapter 10 table 10.2-B row 7.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from starlette.requests import Request

from medos.api.auth import principal_of
from medos.api.problems import build_problem, problem_response, trace_id_of

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["identity"])


@router.get("/users/me", summary="Identity and effective permissions of the caller")
def get_current_principal(request: Request) -> Any:
    """The caller's own identity. Authn only, by design -- see the module docstring."""
    principal = principal_of(request)
    if principal is None:
        # Unreachable behind `AuthenticationMiddleware`. Present for the same reason
        # `medos/medos/api/authz.py::require` carries the branch: a handler that assumes a
        # principal is a handler that serves an answer when the middleware is mounted out
        # of order, and this one's answer would be an identity.
        return problem_response(
            build_problem(
                status=401,
                code="UNAUTHENTICATED",
                title="Unauthenticated",
                detail="every request reaching this endpoint resolves to one principal",
                problem_class="authz_error",
                instance=str(request.url.path),
                trace_id=trace_id_of(request),
            )
        )

    return {
        "principal_kind": principal.kind,
        "principal_id": principal.principal_id,
        "tenant_id": principal.tenant_id,
        # The PUBLIC half of the credential. MOS-SEC-136: "A secret's `key_id` MAY be
        # logged." It is what makes "revoke the key that did this" actionable.
        "api_key_id": principal.credential_key_id,
        "expires_at": principal.expires_at.isoformat() if principal.expires_at else None,
        # NOT `permissions`. See the module docstring: this is a ceiling, not an effective
        # set, and naming it otherwise would invite a surface to treat it as authorisation.
        "scope": list(principal.scope),
        "scope_is_a_ceiling": True,
    }
