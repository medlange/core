# SPDX-License-Identifier: Apache-2.0
"""The registry on the EXISTING API paths. Chapter 10 table 10.2-B rows 14-18, 28-31.

§15.2.6: "One `artifacts` table with per-kind JSON-Schema'd manifests **served on the
existing API paths**." So there is no `/api/v1/artifacts` surface here, and there MUST NOT
be one: a second path space for the same rows is how two clients end up disagreeing about
what a service version is. `/service-versions` and `/model-versions` are chapter 10's
paths, and they read and write the one `artifacts` table.

WHICH PERMISSION GUARDS WHICH ROUTE, AND WHY THE TWO CHAPTERS DIFFER
--------------------------------------------------------------------
§6.11 settles it in terms: "where a path, a verb or a permission differs between the two
tables, Chapter 10 is normative". So the routes chapter 10 lists take chapter 10's
permissions -- `service.read`, `service.publish`, `model.read`, `model.write` -- even
though `MOS-REG-108` names `artifact.publish` for the same act. The status route is NOT in
chapter 10's table; chapter 6 adds it, so it takes chapter 6's permissions, which is also
what makes `MOS-REG-109` implementable: `artifact.recall` is a different permission from
`artifact.status.set`, because a recall is irreversible and has a patient-facing
consequence. The tension between `MOS-REG-108` and table 10.2-B is in this component's
report, not resolved by inventing a third answer.

THINNESS. CONTRACT.md §0: "the HTTP layer MUST stay thin -- no business logic in
handlers." Each handler validates its input, calls `medos.registry.repo`, and shapes the
response. It does not compute a digest, decide a lifecycle edge, or open a transaction:
`repo.publish` owns the one-transaction guarantee that the artifact row, its changelog row
and its `AuditEvent` commit together.

Spec: MOS-REG-018 (PATCH is 405), MOS-REG-019, MOS-REG-021, MOS-REG-040, MOS-REG-084,
MOS-REG-087, MOS-REG-107, MOS-REG-108, MOS-REG-109, MOS-API-035, MOS-API-036,
MOS-SEC-008, MOS-SEC-033, MOS-SEC-045, CONTRACT.md §0 and §9.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Depends, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from medos.api import db_connection
from medos.api.auth import principal_of
from medos.api.problems import build_problem, json_safe, problem_response, trace_id_of
from medos.db import audit
from medos.registry import repo
from medos.registry.errors import RegistryError
from medos.registry.lifecycle import LIFECYCLE_STATUSES, permission_for
from medos.security.scopes import scope_permits

__all__ = ["router", "PERMISSIONS"]

router = APIRouter(prefix="/api/v1", tags=["registry"])

# The permissions this surface can require, with the text a denial quotes back. A typo in
# a handler becomes a 500 at import review rather than a route that denies nobody --
# `MOS-SEC-033`: "a permission not present in `medos/contracts/permissions.yaml` MUST be treated
# as unknown and MUST deny", and that catalogue does not exist yet (see
# `medos/medos/security/scopes.py`'s docstring for why a subset check against an empty catalogue
# would be worse than none).
PERMISSIONS: dict[str, str] = {
    "service.read": "read service versions (table 10.2-B rows 14, 16-18)",
    "service.publish": "publish an immutable signed service release (row 15)",
    "model.read": "read model versions (rows 28, 30, 31)",
    "model.write": "register a model version (row 29)",
    "artifact.approve": "move a VALIDATED version to APPROVED (MOS-REG-021)",
    "artifact.suspend": "suspend or un-suspend a version (MOS-REG-021)",
    "artifact.recall": "recall a version, irreversibly (MOS-REG-109)",
    "artifact.status.set": "routine lifecycle status management (MOS-REG-021)",
    "evidence.run": "record that an EvaluationRun started or finished (MOS-REG-021)",
}

_KIND_READ = {"service_version": "service.read", "model_version": "model.read"}
_KIND_WRITE = {"service_version": "service.publish", "model_version": "model.write"}


# =====================================================================================
# Bodies
# =====================================================================================
class PublishRequest(BaseModel):
    """`ServiceVersionCreateRequest` / `ModelVersionCreateRequest`.

    `extra="forbid"`: `MOS-REG-015` makes an unknown member of a MANIFEST a reject, and a
    publish request that silently dropped `sbom_object_key` because it was spelled
    `sbom_key` would register an artifact whose supply-chain material the publisher
    believes they supplied.
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=4, max_length=70)]
    manifest: dict[str, Any]
    # MOS-REG-084: the signed unit is the OCI image manifest whose config blob is the
    # MedicalOS artifact manifest. These five describe that unit; MOS-REG-090 makes them
    # mandatory for anything entering REGISTERED.
    oci_image_digest: str | None = None
    signature: str | None = None            # base64 of the cosign/DSSE bundle
    signature_alg: Literal["cosign-sigstore", "ed25519"] | None = None
    signer_identity: Annotated[str, Field(max_length=512)] | None = None
    sbom_object_key: Annotated[str, Field(max_length=512)] | None = None
    lifecycle_status: Literal["DRAFT", "REGISTERED"] = "REGISTERED"


class StatusRequest(BaseModel):
    """§6.11: `{ "status": "SUSPENDED", "reason": "self-test regression on sm_89" }`."""

    model_config = ConfigDict(extra="forbid")

    status: str
    reason: Annotated[str, Field(max_length=2000)] | None = None


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
    problem_class: str = "client_error",
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


def _json(payload: Any, status: int) -> Response:
    """A plain JSON response. Named so nothing here is mistaken for a problem document."""
    return JSONResponse(content=payload, status_code=status)


def _require(request: Request, permission: str) -> Response | None:
    """403 unless the credential's scope carries `permission`."""
    if permission not in PERMISSIONS:  # pragma: no cover - a typo guard, not a path
        raise RuntimeError(
            f"{permission!r} is not a registry permission: {sorted(PERMISSIONS)}"
        )
    principal = principal_of(request)
    if principal is None:  # pragma: no cover - AuthenticationMiddleware refuses first
        return _problem(
            request, status=401, code="UNAUTHENTICATED", title="Unauthenticated",
            detail="every request reaching this endpoint resolves to one principal",
            problem_class="authz_error",
        )
    if not scope_permits(principal.scope, permission):
        return _problem(
            request, status=403, code="PERMISSION_DENIED", title="Permission denied",
            detail=f"this credential does not carry {permission!r} ({PERMISSIONS[permission]})",
            problem_class="authz_error", required_permission=permission,
        )
    return None


def _actor(request: Request) -> audit.Actor:
    principal = principal_of(request)
    # `Principal.kind` is `user` or `service_account`, both of which are `ACTOR_KINDS`
    # (MOS-SEC-146); `credential_key_id` is the public half of the credential and is the
    # one part MOS-SEC-136 says MAY be logged, which is what makes "revoke the key that
    # published this" a sentence an operator can act on.
    return audit.Actor(
        kind=getattr(principal, "kind", None) or "service_account",
        id=str(getattr(principal, "principal_id", "unknown")),
        auth="api_key",
        key_id=getattr(principal, "credential_key_id", None),
    )


def _registry_problem(request: Request, exc: RegistryError) -> Response:
    """One mapping from a registry refusal to its problem document.

    The status and the code are on the exception class (see `medos/medos/registry/errors.py`),
    so this function cannot classify a refusal differently from a second caller of the
    same repository function.
    """
    return _problem(
        request,
        status=exc.status,
        code=exc.code,
        title=exc.code.replace("_", " ").title(),
        detail=exc.detail,
        problem_class=exc.problem_class,
        **exc.extensions,
    )


def _render(row: dict[str, Any], version_kind: str) -> dict[str, Any]:
    """The wire shape. Chapter 6's names on the outside, chapter 12's in the column.

    `content_digest` is `MOS-REG-017`'s name for the column §12.9 calls `manifest_digest`,
    and `published_at` is chapter 6's name for §12.9's `sealed_at`. The divergence is
    chapter 12's to resolve; what this function will not do is emit BOTH names, which
    would leave a client to guess which is authoritative.
    """
    return json_safe(
        {
            "id": row["public_id"],
            "kind": version_kind,
            "family": row["family"],
            "version": row["version"],
            "status": row["lifecycle_status"],
            "status_reason": row["status_reason"],
            "content_digest": row["manifest_digest"],
            "manifest_schema": f"{version_kind}/{row['manifest_schema_version']}",
            "oci_image_digest": row["oci_image_digest"],
            "published_by": row["published_by"],
            "published_at": row["sealed_at"],
            "tenant_id": row["tenant_id"],
        }
    )


def _attestation(row: dict[str, Any]) -> dict[str, Any]:
    """Row 18: "Image digest, signature, SBOM reference, `legal_manufacturer`"."""
    spec = (row["manifest"] or {}).get("spec") or {}
    return json_safe(
        {
            "subject_digest": row["oci_image_digest"],
            "content_digest": row["manifest_digest"],
            "signature_alg": row["signature_alg"],
            "signer_identity": row["signer_identity"],
            "sbom_object_key": row["sbom_object_key"],
            # MOS-REG-087's referrer artifactTypes, named so a client knows what to fetch
            # from the OCI registry rather than guessing.
            "referrers": {
                "signature": "application/vnd.dev.cosign.simplesigning.v1+json",
                "sbom": "application/vnd.cyclonedx+json",
                "provenance": "application/vnd.in-toto+json",
            },
            "legal_manufacturer": spec.get("legal_manufacturer"),
        }
    )


# =====================================================================================
# Routes -- the two kinds share every handler; only the path and the permission differ.
# =====================================================================================
def _list(
    request: Request,
    conn: psycopg.Connection[Any],
    version_kind: str,
    *,
    family: str | None,
    status_in: str | None,
    capability_id: str | None,
    limit: int,
) -> Response | list[dict[str, Any]]:
    denied = _require(request, _KIND_READ[version_kind])
    if denied is not None:
        return denied
    rows = repo.list_artifacts(
        conn,
        kind=version_kind,
        family=family,
        status_in=[s for s in (status_in or "").split(",") if s] or None,
        capability_id=capability_id,
        limit=limit,
    )
    return [_render(r, version_kind) for r in rows]


def _publish(
    request: Request, conn: psycopg.Connection[Any], version_kind: str, body: PublishRequest
) -> Response:
    denied = _require(request, _KIND_WRITE[version_kind])
    if denied is not None:
        return denied
    if body.manifest.get("kind") != version_kind:
        return _problem(
            request, status=422, code="ARTIFACT_KIND_MISMATCH",
            title="Artifact kind mismatch",
            detail=f"this path publishes {version_kind}; the manifest declares "
                   f"{body.manifest.get('kind')!r}",
        )
    try:
        row, created = repo.publish(
            conn,
            manifest=body.manifest,
            public_id=body.id,
            actor=_actor(request),
            trace_id=trace_id_of(request),
            lifecycle_status=body.lifecycle_status,
            oci_image_digest=body.oci_image_digest,
            signature=body.signature.encode("ascii") if body.signature else None,
            signature_alg=body.signature_alg,
            signer_identity=body.signer_identity,
            sbom_object_key=body.sbom_object_key,
        )
    except RegistryError as exc:
        return _registry_problem(request, exc)
    # MOS-REG-019: 201 for a new version, 200 for a byte-identical republish. The
    # difference is the whole of the idempotency contract and a client depends on it.
    return _json(_render(row, version_kind), 201 if created else 200)


def _set_status(
    request: Request, conn: psycopg.Connection[Any], version_kind: str, ident: str,
    body: StatusRequest,
) -> Response:
    permitted = LIFECYCLE_STATUSES[version_kind]
    if body.status not in permitted:
        return _problem(
            request, status=422, code="UNKNOWN_LIFECYCLE_STATUS",
            title="Unknown lifecycle status",
            detail=f"{body.status!r} is not a {version_kind} status (MOS-REG-020)",
            permitted_statuses=list(permitted),
        )
    try:
        current = repo.get(conn, ident, kind=version_kind)
    except RegistryError as exc:
        return _registry_problem(request, exc)
    # MOS-REG-021's permission column, and MOS-REG-109's separation: a recall is NOT
    # reachable with the routine status-management permission.
    permission = permission_for(body.status, from_status=current["lifecycle_status"])
    denied = _require(request, permission)
    if denied is not None:
        return denied
    try:
        row = repo.set_status(
            conn, ident, to_status=body.status, reason=body.reason,
            actor=_actor(request), trace_id=trace_id_of(request), kind=version_kind,
        )
    except RegistryError as exc:
        return _registry_problem(request, exc)
    return _json(_render(row, version_kind), 200)


def _get_one(
    request: Request, conn: psycopg.Connection[Any], version_kind: str, ident: str,
    projection: str,
) -> Response:
    denied = _require(request, _KIND_READ[version_kind])
    if denied is not None:
        return denied
    try:
        row = repo.get(conn, ident, kind=version_kind)
    except RegistryError as exc:
        return _registry_problem(request, exc)
    if projection == "manifest":
        # Row 17 calls this "Raw `service.yaml` as published" and types it
        # `application/yaml`. What was published here is the JSON manifest that was
        # digested (MOS-REG-017), and re-serialising it as YAML would change the bytes
        # the signature covers. It is returned as published. Reported.
        return _json(json_safe(row["manifest"]), 200)
    if projection == "attestation":
        return _json(_attestation(row), 200)
    if projection == "impact":
        # MOS-REG-040: "a recall state with no impact query is not a recall."
        return _json(
            {"artifact_id": row["public_id"], "status": row["lifecycle_status"],
             "affected": json_safe(repo.impact(conn, ident))}, 200
        )
    return _json(_render(row, version_kind), 200)


# ------------------------------------------------------------------ service-versions ---
@router.get("/service-versions")
def list_service_versions(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    family: str | None = Query(default=None),
    status__in: str | None = Query(default=None),
    capability_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> Any:
    # MOS-API-005: medos/api/v1/routes.core.yaml declares 'service.read' on
    # GET /service-versions, and this handler did not check it.
    denied = _require(request, "service.read")
    if denied is not None:
        return denied

    return _list(request, conn, "service_version", family=family, status_in=status__in,
                 capability_id=capability_id, limit=limit)


@router.post("/service-versions")
def publish_service_version(
    request: Request,
    body: PublishRequest,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
) -> Response:
    # MOS-API-005: medos/api/v1/routes.core.yaml declares 'service.publish' on
    # POST /service-versions, and this handler did not check it.
    denied = _require(request, "service.publish")
    if denied is not None:
        return denied

    return _publish(request, conn, "service_version", body)


@router.get("/service-versions/{service_version_id}")
def get_service_version(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    service_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: medos/api/v1/routes.core.yaml declares 'service.read' on
    # GET /service-versions/{service_version_id}, and this handler did not check it.
    denied = _require(request, "service.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "service_version", service_version_id, "row")


@router.get("/service-versions/{service_version_id}/manifest")
def get_service_manifest(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    service_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'service.read' on
    # GET /service-versions/{service_version_id}/manifest; this handler did not check it.
    denied = _require(request, "service.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "service_version", service_version_id, "manifest")


@router.get("/service-versions/{service_version_id}/attestation")
def get_service_attestation(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    service_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'service.read' on
    # GET /service-versions/{service_version_id}/attestation; this handler did not check it.
    denied = _require(request, "service.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "service_version", service_version_id, "attestation")


@router.get("/service-versions/{service_version_id}/impact")
def get_service_impact(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    service_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'service.read' on
    # GET /service-versions/{service_version_id}/impact; this handler did not check it.
    denied = _require(request, "service.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "service_version", service_version_id, "impact")


@router.post("/service-versions/{service_version_id}/status")
def set_service_status(
    request: Request,
    body: StatusRequest,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    service_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'artifact.status.set' on
    # POST /service-versions/{service_version_id}/status; this handler did not check it.
    denied = _require(request, "artifact.status.set")
    if denied is not None:
        return denied

    return _set_status(request, conn, "service_version", service_version_id, body)


@router.patch("/service-versions/{service_version_id}")
def patch_service_version(
    request: Request, service_version_id: Annotated[str, Path(max_length=70)]
) -> Response:
    return _immutable(request)


# -------------------------------------------------------------------- model-versions ---
@router.get("/model-versions")
def list_model_versions(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    family: str | None = Query(default=None),
    status__in: str | None = Query(default=None),
    capability_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> Any:
    # MOS-API-005: medos/api/v1/routes.core.yaml declares 'model.read' on
    # GET /model-versions, and this handler did not check it.
    denied = _require(request, "model.read")
    if denied is not None:
        return denied

    return _list(request, conn, "model_version", family=family, status_in=status__in,
                 capability_id=capability_id, limit=limit)


@router.post("/model-versions")
def publish_model_version(
    request: Request,
    body: PublishRequest,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
) -> Response:
    # MOS-API-005: medos/api/v1/routes.core.yaml declares 'model.write' on
    # POST /model-versions, and this handler did not check it.
    denied = _require(request, "model.write")
    if denied is not None:
        return denied

    return _publish(request, conn, "model_version", body)


@router.get("/model-versions/{model_version_id}")
def get_model_version(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    model_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: medos/api/v1/routes.core.yaml declares 'model.read' on
    # GET /model-versions/{model_version_id}, and this handler did not check it.
    denied = _require(request, "model.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "model_version", model_version_id, "row")


@router.get("/model-versions/{model_version_id}/manifest")
def get_model_manifest(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    model_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'model.read' on
    # GET /model-versions/{model_version_id}/manifest; this handler did not check it.
    denied = _require(request, "model.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "model_version", model_version_id, "manifest")


@router.get("/model-versions/{model_version_id}/attestation")
def get_model_attestation(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    model_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'model.read' on
    # GET /model-versions/{model_version_id}/attestation; this handler did not check it.
    denied = _require(request, "model.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "model_version", model_version_id, "attestation")


@router.get("/model-versions/{model_version_id}/impact")
def get_model_impact(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    model_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'model.read' on
    # GET /model-versions/{model_version_id}/impact; this handler did not check it.
    denied = _require(request, "model.read")
    if denied is not None:
        return denied

    return _get_one(request, conn, "model_version", model_version_id, "impact")


@router.post("/model-versions/{model_version_id}/status")
def set_model_status(
    request: Request,
    body: StatusRequest,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    model_version_id: Annotated[str, Path(max_length=70)],
) -> Response:
    # MOS-API-005: the registry declares 'artifact.status.set' on
    # POST /model-versions/{model_version_id}/status; this handler did not check it.
    denied = _require(request, "artifact.status.set")
    if denied is not None:
        return denied

    return _set_status(request, conn, "model_version", model_version_id, body)


@router.patch("/model-versions/{model_version_id}")
def patch_model_version(
    request: Request, model_version_id: Annotated[str, Path(max_length=70)]
) -> Response:
    return _immutable(request)


def _immutable(request: Request) -> Response:
    """§6.11: `PATCH /api/v1/service-versions/{id}` is 405 -- manifests are immutable.

    A route that exists and refuses, rather than a 404 from the router: a client that
    PATCHes a manifest has a wrong model of the registry, and "405, manifests are
    immutable, publish a new version" corrects it where "404" does not.
    """
    return _problem(
        request,
        status=405,
        code="ARTIFACT_IMMUTABLE",
        title="Artifact Immutable",
        detail="an artifact manifest is immutable after publish (MOS-REG-018); publish a "
               "new version, or change the lifecycle status via POST .../status",
        allowed_methods=["GET", "POST"],
    )
