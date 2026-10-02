# SPDX-License-Identifier: Apache-2.0
"""`ResultReview` endpoints. `MOS-SAFE-069`'s eight rows.

| Method + path                                  | Success |
|------------------------------------------------|---------|
| `GET  /api/v1/result-reviews`                  | 200 list |
| `GET  /api/v1/result-reviews/{id}`             | 200, `rounds[]` |
| `POST /api/v1/result-reviews/{id}/claim`       | 200, 409 `already_claimed` |
| `POST /api/v1/result-reviews/{id}/release`     | 200, claimant only |
| `POST /api/v1/result-reviews/{id}/submit`      | 200, 422 on missing conditional fields |
| `POST /api/v1/result-reviews/{id}/assign`      | 200 |
| `POST /api/v1/result-reviews/{id}/reopen`      | 201 + new round |
| `GET  /api/v1/results/{result_id}/review`      | 200, convenience projection |

"Chapter 10 owns the route registry, this chapter owns the state machine behind it."
Accordingly: the state machine is `medos/medos/safety/review.py`, the rows are
`medos/medos/safety/repo.py`, and this file is a thin HTTP surface -- CONTRACT.md section 1's
"no business logic in handlers", which matters more here than elsewhere because
`MOS-REL-084` replaces this surface with Go.

AUTHORIZATION, AND WHAT IT CAN HONESTLY BE TODAY
------------------------------------------------
`MOS-SAFE-067` names six permissions. `medos/medos/security/scopes.py` explains at length why a
scope entry is a CEILING and not a grant (`MOS-SEC-027`, `MOS-SEC-045`): the effective
permission set is the intersection of the credential's scope, the principal's role grants
and the manifest's declared permissions, and this platform has no `roles` table, so the
second term is unavailable.

What this module does is enforce the term it HAS: a request whose credential does not carry
the permission is refused 403. That is sound -- the ceiling is a real bound and exceeding it
is a real violation -- and it is stated rather than dressed up: holding the scope entry is
NECESSARY here and is not yet SUFFICIENT anywhere, because the role check that would make it
sufficient does not exist. The gateway takes the identical posture
(`medos.gateway.auth.Principal.has`), so there is one answer in the codebase rather than two.

`reviewer_class` is NOT derived from the scope, and could not be: `MOS-SAFE-104` makes
`roles.clinically_qualified` "the only input by which the platform may decide that a review
counts as a qualified human read". `medos.safety.review.reviewer_class_for` asks the role
directory, which in this deployment answers False for everyone, so every submitted review is
recorded `non_clinical`. See that module's docstring for why that is the requirement's own
answer and not a stub.

Spec: MOS-SAFE-058..070, MOS-SAFE-104, MOS-API-035, MOS-API-036, MOS-API-037, MOS-API-047,
MOS-SEC-008, MOS-SEC-027, MOS-SEC-045, CONTRACT.md sections 1 and 11.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from medos.api import db_connection
from medos.api.auth import principal_of
from medos.api.problems import build_problem, json_safe, problem_response, trace_id_of
from medos.safety import repo as safety_repo
from medos.safety.policy import default_policy_source
from medos.safety.review import (
    PERMISSIONS,
    REJECTION_REASONS,
    SUBMIT_OUTCOMES,
    SubmissionInvalid,
    reviewer_class_for,
)
from medos.security.scopes import scope_permits

__all__ = ["router", "REVIEW_ID_RE"]

router = APIRouter(prefix="/api/v1", tags=["result-reviews"])

# `MOS-SAFE-058` gives `ResultReview` the prefix `rrv_`; the ULID grammar is
# `MOS-STORE-357`'s, identical to `jobs.public_id`'s and to the CHECK in
# `0010_safety.up.sql`. Case-sensitive: Crockford base32 upper case only.
REVIEW_ID_RE = re.compile(r"^rrv_[0-9A-HJKMNP-TV-Z]{26}$")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# =====================================================================================
# Request models
# =====================================================================================
class SubmitRequest(BaseModel):
    """`MOS-SAFE-069`'s submit body, field for field.

    `extra="forbid"`: a caller that spells `rationale` instead of `action_rationale` must
    get a 422 naming the field, not a review recorded with no rationale. The two
    conditionally-required combinations are policed by
    `medos.safety.review.validate_submission`, not here, because the database CHECKs are
    written against that same function's rules and one statement of a rule is the point.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["ACCEPTED", "MODIFIED", "REJECTED"]
    reviewer_role: Annotated[str, Field(min_length=1, max_length=128)]
    action_rationale: Annotated[str, Field(max_length=4000)] | None = None
    modifications: list[dict[str, Any]] | None = None
    rejection_reason: str | None = None
    known_failure_mode_id: Annotated[str, Field(max_length=64)] | None = None


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_user_id: Annotated[str, Field(min_length=1, max_length=64)]


class ReopenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `MOS-SAFE-066`: "`POST .../reopen` opens a new round and MUST require
    # `action_rationale`." The 20-character floor is `MOS-SAFE-058`'s and is enforced in
    # `medos.safety.repo.reopen_review` so that a direct repository caller meets it too.
    action_rationale: Annotated[str, Field(min_length=1, max_length=4000)]


# =====================================================================================
# Helpers
# =====================================================================================
def _problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    problem_class: str,
    **extensions: Any,
) -> Response:
    return problem_response(
        build_problem(
            status=status,
            code=code,
            title=title,
            detail=detail,
            problem_class=problem_class,
            instance=request.url.path,
            trace_id=trace_id_of(request),
            **extensions,
        )
    )


def _require(request: Request, permission: str) -> Response | None:
    """403 unless the credential's scope carries `permission`. See the module docstring.

    `MOS-SEC-033`: "a permission not present in `medos/contracts/permissions.yaml` MUST be
    treated as unknown and MUST deny." The catalogue is not generated yet, so the local
    guard is that the permission is one `MOS-SAFE-067` names -- a typo in a handler
    becomes a 500 at import review rather than a route that denies nobody.
    """
    if permission not in PERMISSIONS:
        raise RuntimeError(
            f"{permission!r} is not one of MOS-SAFE-067's permissions: "
            f"{sorted(PERMISSIONS)}"
        )
    principal = principal_of(request)
    if principal is None:
        # Unreachable behind `AuthenticationMiddleware`, which refuses an unauthenticated
        # request before any handler runs (`MOS-SEC-008`). Present because a handler that
        # assumes a principal is a handler that grants access if the middleware is ever
        # mounted in the wrong order.
        return _problem(
            request,
            status=401,
            code="UNAUTHENTICATED",
            title="Unauthenticated",
            detail="every request reaching this endpoint resolves to one principal",
            problem_class="authz_error",
        )
    if not scope_permits(principal.scope, permission):
        return _problem(
            request,
            status=403,
            code="PERMISSION_DENIED",
            title="Permission denied",
            detail=(
                f"this credential does not carry {permission!r} "
                f"({PERMISSIONS[permission]})"
            ),
            problem_class="authz_error",
            required_permission=permission,
        )
    return None


def _bad_review_id(request: Request, review_id: str) -> Response | None:
    if REVIEW_ID_RE.match(review_id):
        return None
    return _problem(
        request,
        status=400,
        code="INVALID_RESULT_REVIEW_ID",
        title="Malformed result review id",
        detail=(
            "a ResultReview id is 'rrv_' followed by a 26-character upper-case "
            "Crockford base32 ULID (MOS-SAFE-058)"
        ),
        problem_class="client_error",
    )


def _not_found(request: Request, review_id: str) -> Response:
    return _problem(
        request,
        status=404,
        code="RESULT_REVIEW_NOT_FOUND",
        title="No such result review",
        detail=f"no ResultReview {review_id} is visible to this tenant",
        problem_class="client_error",
    )


def _conflict(request: Request, exc: safety_repo.ReviewConflict) -> Response:
    """`MOS-SAFE-069`'s 409. `code` carries `already_claimed` when that is what it is."""
    return _problem(
        request,
        status=409,
        code=exc.code.upper(),
        title="Review transition refused",
        detail=str(exc),
        problem_class="client_error",
        current_state=exc.state,
    )


def _unprocessable(request: Request, exc: SubmissionInvalid) -> Response:
    return _problem(
        request,
        status=422,
        code="REVIEW_SUBMISSION_INVALID",
        title="Missing conditional field",
        detail=str(exc),
        problem_class="client_error",
        violations=exc.pointers,
    )


def _principal_id(request: Request) -> str:
    principal = principal_of(request)
    assert principal is not None
    return str(principal.principal_id)


def _shape(rows: Sequence[safety_repo.ReviewRow]) -> list[dict[str, Any]]:
    return [json_safe(r.as_api()) for r in rows]


# =====================================================================================
# GET /api/v1/result-reviews
# =====================================================================================
@router.get("/result-reviews", summary="List result reviews")
def list_result_reviews(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_id: Annotated[str | None, Query(max_length=64)] = None,
    state: Annotated[str | None, Query(max_length=16)] = None,
    assignee_user_id: Annotated[str | None, Query(max_length=64)] = None,
    overdue: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """Tenant-scoped by row-level security, never by a WHERE clause in this handler."""
    denied = _require(request, "result.review.read")
    if denied is not None:
        return denied
    if result_id is not None and not _UUID_RE.match(result_id):
        return _problem(
            request,
            status=400,
            code="INVALID_RESULT_ID",
            title="Malformed result id",
            detail="result_id is the uuid of a results row",
            problem_class="client_error",
        )
    rows = safety_repo.list_reviews(
        conn,
        result_id=result_id,
        state=state,
        assignee_user_id=assignee_user_id,
        overdue=overdue,
        limit=limit,
    )
    return {"items": _shape(rows), "count": len(rows)}


# =====================================================================================
# GET /api/v1/result-reviews/{id}
# =====================================================================================
@router.get("/result-reviews/{result_review_id}", summary="Read one result review")
def get_result_review(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_review_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """`MOS-SAFE-069`: "includes all rounds via `rounds[]`".

    The named round is the top-level document and every round is in `rounds[]`, oldest
    first. A review is corrected by a new round and never by an edit (`MOS-SAFE-059`), so
    the history IS the record: a response that showed only the latest round would render a
    reopened review as though the first conclusion had never been reached.
    """
    denied = _require(request, "result.review.read")
    if denied is not None:
        return denied
    bad = _bad_review_id(request, result_review_id)
    if bad is not None:
        return bad
    try:
        rounds = safety_repo.get_review_rounds(conn, result_review_id)
    except safety_repo.ReviewNotFound:
        return _not_found(request, result_review_id)
    current = next(r for r in rounds if r.public_id == result_review_id)
    return {**json_safe(current.as_api()), "rounds": _shape(rounds)}


# =====================================================================================
# POST /api/v1/result-reviews/{id}/claim
# =====================================================================================
@router.post("/result-reviews/{result_review_id}/claim", summary="Claim a review")
def claim(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_review_id: Annotated[str, Path(max_length=64)],
) -> Any:
    denied = _require(request, "result.review.submit")
    if denied is not None:
        return denied
    bad = _bad_review_id(request, result_review_id)
    if bad is not None:
        return bad
    try:
        row = safety_repo.claim_review(
            conn, result_review_id, reviewer_user_id=_principal_id(request)
        )
    except safety_repo.ReviewNotFound:
        return _not_found(request, result_review_id)
    except safety_repo.ReviewConflict as exc:
        return _conflict(request, exc)
    conn.commit()
    return json_safe(row.as_api())


# =====================================================================================
# POST /api/v1/result-reviews/{id}/release
# =====================================================================================
@router.post("/result-reviews/{result_review_id}/release", summary="Release a claimed review")
def release(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_review_id: Annotated[str, Path(max_length=64)],
) -> Any:
    denied = _require(request, "result.review.submit")
    if denied is not None:
        return denied
    bad = _bad_review_id(request, result_review_id)
    if bad is not None:
        return bad
    try:
        row = safety_repo.release_review(
            conn, result_review_id, reviewer_user_id=_principal_id(request)
        )
    except safety_repo.ReviewNotFound:
        return _not_found(request, result_review_id)
    except safety_repo.ReviewConflict as exc:
        return _conflict(request, exc)
    conn.commit()
    return json_safe(row.as_api())


# =====================================================================================
# POST /api/v1/result-reviews/{id}/submit
# =====================================================================================
@router.post("/result-reviews/{result_review_id}/submit", summary="Submit a review")
def submit(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_review_id: Annotated[str, Path(max_length=64)],
    body: SubmitRequest,
) -> Any:
    """Terminal for this row. `reviewer_class` is derived here and stored immutably.

    `MOS-SAFE-068` derives it from the PRINCIPAL and the role directory -- two
    request-scoped facts -- so the derivation happens at the PEP and the answer is handed
    down. `medos.safety.repo.submit_review` takes it as an argument and does not compute
    it: a repository that derived it would need to know what kind of credential made the
    request, which is knowledge `MOS-SEC-008` concentrates here.
    """
    denied = _require(request, "result.review.submit")
    if denied is not None:
        return denied
    bad = _bad_review_id(request, result_review_id)
    if bad is not None:
        return bad
    if body.rejection_reason is not None and body.rejection_reason not in REJECTION_REASONS:
        return _problem(
            request,
            status=422,
            code="REVIEW_SUBMISSION_INVALID",
            title="Unknown rejection reason",
            detail=f"rejection_reason MUST be one of {sorted(REJECTION_REASONS)}",
            problem_class="client_error",
            violations=[{"pointer": "/rejection_reason", "detail": "closed enum"}],
        )
    if body.action not in SUBMIT_OUTCOMES:  # pragma: no cover - the Literal covers it
        return _problem(
            request,
            status=422,
            code="REVIEW_SUBMISSION_INVALID",
            title="Unknown action",
            detail=f"action MUST be one of {sorted(SUBMIT_OUTCOMES)}",
            problem_class="client_error",
        )

    principal = principal_of(request)
    assert principal is not None
    reviewer_class = reviewer_class_for(
        principal_kind=principal.kind,
        principal_id=str(principal.principal_id),
        reviewer_role=body.reviewer_role,
    )
    try:
        row = safety_repo.submit_review(
            conn,
            result_review_id,
            outcome=body.action,
            reviewer_user_id=str(principal.principal_id),
            reviewer_role=body.reviewer_role,
            reviewer_class=reviewer_class,
            action_rationale=body.action_rationale,
            modifications=body.modifications,
            rejection_reason=body.rejection_reason,
            known_failure_mode_id=body.known_failure_mode_id,
        )
    except safety_repo.ReviewNotFound:
        return _not_found(request, result_review_id)
    except safety_repo.ReviewConflict as exc:
        return _conflict(request, exc)
    except SubmissionInvalid as exc:
        return _unprocessable(request, exc)
    conn.commit()
    return json_safe(row.as_api())


# =====================================================================================
# POST /api/v1/result-reviews/{id}/assign
# =====================================================================================
@router.post("/result-reviews/{result_review_id}/assign", summary="Assign a review")
def assign(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_review_id: Annotated[str, Path(max_length=64)],
    body: AssignRequest,
) -> Any:
    denied = _require(request, "result.review.assign")
    if denied is not None:
        return denied
    bad = _bad_review_id(request, result_review_id)
    if bad is not None:
        return bad
    try:
        row = safety_repo.assign_review(
            conn, result_review_id, assignee_user_id=body.assignee_user_id
        )
    except safety_repo.ReviewNotFound:
        return _not_found(request, result_review_id)
    except safety_repo.ReviewConflict as exc:
        return _conflict(request, exc)
    conn.commit()
    return json_safe(row.as_api())


# =====================================================================================
# POST /api/v1/result-reviews/{id}/reopen
# =====================================================================================
@router.post(
    "/result-reviews/{result_review_id}/reopen", status_code=201, summary="Open a new round"
)
def reopen(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_review_id: Annotated[str, Path(max_length=64)],
    body: ReopenRequest,
    response: Response,
) -> Any:
    """`201 + new round` (`MOS-SAFE-069`). The previous round is not touched."""
    denied = _require(request, "result.review.reopen")
    if denied is not None:
        return denied
    bad = _bad_review_id(request, result_review_id)
    if bad is not None:
        return bad
    policy = default_policy_source().policy_for(
        service_id="", service_version=""
    )
    try:
        row = safety_repo.reopen_review(
            conn, result_review_id, action_rationale=body.action_rationale, policy=policy
        )
    except safety_repo.ReviewNotFound:
        return _not_found(request, result_review_id)
    except safety_repo.ReviewConflict as exc:
        return _conflict(request, exc)
    except SubmissionInvalid as exc:
        return _unprocessable(request, exc)
    conn.commit()
    response.headers["Location"] = f"/api/v1/result-reviews/{row.public_id}"
    return json_safe(row.as_api())


# =====================================================================================
# GET /api/v1/results/{result_id}/review
# =====================================================================================
@router.get("/results/{result_id}/review", summary="The review of one result")
def review_of_result(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    result_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """`MOS-SAFE-069`'s "convenience projection": the highest round, plus the history.

    Highest round and not "the one that is open", because `MOS-SAFE-062` defines
    `Result.review_status` as the projection of the highest-round row and this endpoint
    must agree with that column. A 200 with `review: null` when no row exists, rather than
    a 404: `MOS-SAFE-062` makes `UNREVIEWED` a real state of a real result, and a 404 would
    say the result does not exist.
    """
    denied = _require(request, "result.review.read")
    if denied is not None:
        return denied
    if not _UUID_RE.match(result_id):
        return _problem(
            request,
            status=400,
            code="INVALID_RESULT_ID",
            title="Malformed result id",
            detail="result_id is the uuid of a results row",
            problem_class="client_error",
        )
    rounds = safety_repo.list_reviews(conn, result_id=result_id, limit=200)
    if not rounds:
        return {"result_id": result_id, "review_status": "UNREVIEWED", "review": None,
                "rounds": []}
    ordered = sorted(rounds, key=lambda r: r.round)
    latest = ordered[-1]
    return {
        "result_id": result_id,
        "review_status": latest.state,
        "review": json_safe(latest.as_api()),
        "rounds": _shape(ordered),
    }
