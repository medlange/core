# SPDX-License-Identifier: Apache-2.0
"""Publish, read, list and transition. The whole of the registry's row access.

One function per verb chapter 6 names, each doing the schema validation, the digest, the
cross-row checks and the write in ONE transaction, so an artifact row, its changelog row
(`MOS-REG-010`, the trigger) and its `AuditEvent` (`MOS-REG-003`) commit together or not
at all. A published row with no audit event is exactly what `MOS-SEC-149` forbids.

WHAT IS CHECKED HERE RATHER THAN IN THE SCHEMA, AND WHY
-------------------------------------------------------
The JSON Schema (`medos/medos/registry/schemas.py`) can only see one manifest. Everything that
needs a second row is here:

  `MOS-REG-017`  the content digest is COMPUTED, never taken from the publisher, and a
                 supplied value is compared.
  `MOS-REG-019`  republish: byte-identical is a 200 no-op, different content is a 409
                 quoting the digest already on record.
  `MOS-REG-025`  every `spec.models[].ref` resolves to a real `model_version` row whose
                 `lifecycle_status` is `VALIDATED` or `APPROVED` -- "at publish, not at
                 dispatch", because a dangling ref discovered at dispatch is a clinical
                 rejection in front of a waiting radiologist.
  `MOS-REG-026`  a sealed service still declares exactly one `primary` ModelVersion, so
                 its evidence has a subject to bind to.
  `MOS-REG-029`  a claimed capability exists. Chapter 6 wants `capabilities.status =
                 supported`; §12.9.1's `capabilities` table is not in this schema yet, so
                 the check is against the set THIS DEPLOYMENT serves --
                 `medos.capabilities.providers.capability_ids()`. Narrower than the
                 requirement (no `status` column exists anywhere, so every served id reads
                 as `supported`) and reported as such.

                 It used to read the module singleton `medos.capabilities.REGISTRY`, and
                 that was a third copy of a rule that is supposed to have exactly one.
                 `MEDOS_CAPABILITY_PROVIDERS` is what the API admits at submit and what
                 the worker dispatches on; a registry that answered from the platform
                 singleton instead refused a `ServiceVersion` for a capability the
                 deployment was already running -- so the capability executed, produced
                 Results, and could never be given the registry row those Results are
                 supposed to name. Recorded as entry 68 of
                 docs/spec/99-known-inconsistencies.md.
  `MOS-REG-103`  an `evaluation_run_id` already cited by another ModelVersion is refused:
                 a converted model that reuses its source's metrics is claiming evidence
                 it does not have.
  `MOS-REG-087`  signature, SBOM and the signed OCI digest are present before a row may be
  `MOS-REG-090`  `REGISTERED`, and the check fails CLOSED -- no flag turns it off.

Spec: MOS-REG-003, MOS-REG-010, MOS-REG-013, MOS-REG-014, MOS-REG-017, MOS-REG-018,
MOS-REG-019, MOS-REG-021, MOS-REG-022, MOS-REG-025, MOS-REG-026, MOS-REG-028,
MOS-REG-029, MOS-REG-040, MOS-REG-087, MOS-REG-090, MOS-REG-103, MOS-REG-107,
MOS-SEC-149, MOS-STORE-253.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db import audit
from medos.db.tenancy import current_tenant, tenant_tx
from medos.registry.digest import content_digest_of
from medos.registry.errors import (
    ArtifactNotFound,
    InvalidManifest,
    LifecycleViolation,
    SupplyChainRefusal,
    UnknownKind,
    UnresolvedReference,
    VersionConflict,
)
from medos.registry.jsonschema import SchemaError
from medos.registry.lifecycle import (
    FAMILY_KIND,
    VERSION_KIND,
    permission_for,
    transition_allowed,
)
from medos.registry.schemas import SCHEMA_VERSION, manifest_errors, schema_for_kind

__all__ = [
    "PUBLIC_ID_RE",
    "get",
    "impact",
    "list_artifacts",
    "publish",
    "set_status",
]

PUBLIC_ID_RE = re.compile(r"^(sv|mv|ps|dv|as|wv|pv)_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\.(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?$"
)
# The public-id prefix per family-level kind; the same mapping the CHECK constraint
# `artifacts_public_id_prefix_matches_kind` carries, stated here so the error arrives
# before the database has to raise one.
_PREFIX = {
    "service": "sv", "model": "mv", "preprocessing": "ps", "dataset": "dv",
    "annotation": "as", "workflow": "wv", "policy_set": "pv",
}
# `MOS-REG-025`: a referenced ModelVersion must be past evaluation. Filter F3 (§6.7.2)
# excludes the same statuses at dispatch; the point of checking here as well is that a
# publish-time refusal names the publisher, and a dispatch-time refusal names a patient.
_REFERENCEABLE = ("VALIDATED", "APPROVED")

_COLUMNS = """
  id::text AS id, public_id, tenant_id::text AS tenant_id, kind, family, version,
  version_major, version_minor, version_patch, version_pre, lifecycle_status,
  status_reason, published_by, manifest_schema_version, manifest, manifest_digest,
  oci_image_digest, bundle_bucket, bundle_object_key, bundle_digest, bundle_size_bytes,
  signature_alg, signer_identity, sbom_object_key, sealed_at, created_at, updated_at
"""


# =====================================================================================
# Publish
# =====================================================================================
def publish(
    conn: psycopg.Connection[Any],
    *,
    manifest: Mapping[str, Any],
    public_id: str,
    actor: audit.Actor,
    trace_id: str,
    request_id: str | None = None,
    lifecycle_status: str = "REGISTERED",
    oci_image_digest: str | None = None,
    signature: bytes | None = None,
    signature_alg: str | None = None,
    signer_identity: str | None = None,
    sbom_object_key: str | None = None,
    bundle: Mapping[str, Any] | None = None,
    published_by: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Register one artifact. Returns `(row, created)`.

    `created is False` is `MOS-REG-019`'s idempotent republish: the same
    `(kind, family, version)` with byte-identical content is a 200 and a no-op, and
    different content is a `VersionConflict` carrying the digest already on record.

    `lifecycle_status` is `REGISTERED` by default, which is `MOS-REG-021`'s
    `DRAFT -> REGISTERED` edge collapsed into the publish call -- "manifest frozen, digest
    assigned, signature verified". Passing `DRAFT` registers an unsigned draft and is the
    only way to get a row without the supply-chain material, exactly as that table says.
    """
    problems = manifest_errors(manifest)
    if problems:
        # MOS-REG-014: a hard reject. Nothing is written, and every violation is listed
        # rather than the first, because a publisher fixing a manifest wants the list.
        raise InvalidManifest(
            f"manifest fails its JSON Schema ({len(problems)} violation(s))",
            violations=problems[:32],
        )

    version_kind = str(manifest["kind"])
    family_kind = FAMILY_KIND.get(version_kind)
    if family_kind is None:  # pragma: no cover - the envelope enum already closed this
        raise UnknownKind(f"{version_kind!r} is not an artifact kind (MOS-REG-013)")
    try:
        schema_for_kind(family_kind)
    except SchemaError as exc:
        # The kind is legal (the envelope enum admitted it) but this release seeds no
        # schema for it, so `MOS-REG-014` has nothing to validate against and the schema
        # foreign key would refuse the row anyway. Answered as 422 with the reason, not as
        # a database error a publisher cannot act on.
        raise UnknownKind(
            f"no manifest schema is registered for kind {version_kind!r}: {exc}"
        ) from exc

    if not PUBLIC_ID_RE.match(public_id) or public_id.split("_", 1)[0] != _PREFIX[family_kind]:
        raise InvalidManifest(
            f"public_id {public_id!r} must be {_PREFIX[family_kind]}_<id> for a "
            f"{version_kind}"
        )

    computed = content_digest_of(manifest)
    supplied = manifest.get("content_digest")
    if supplied is not None and str(supplied) != computed:
        # MOS-REG-017: "A publisher-supplied value MUST be compared and a mismatch
        # rejected." The registry computes; it never takes.
        raise InvalidManifest(
            "content_digest does not match the canonicalised manifest",
            computed_digest=computed,
            supplied_digest=str(supplied),
        )

    match = _SEMVER_RE.match(str(manifest["version"]))
    if match is None:  # pragma: no cover - the envelope pattern already closed this
        raise InvalidManifest(f"version {manifest['version']!r} is not semver")

    _verify_supply_chain(
        version_kind,
        lifecycle_status=lifecycle_status,
        manifest=manifest,
        oci_image_digest=oci_image_digest,
        signature=signature,
        signature_alg=signature_alg,
        signer_identity=signer_identity,
        sbom_object_key=sbom_object_key,
    )

    # MOS-REG-029's vocabulary, read HERE and not inside the publish scope below.
    #
    # `capability_ids()` resolves `MEDOS_CAPABILITY_PROVIDERS`, which on the first call of
    # a configured process composes this deployment's registry and writes a coded-concept
    # dictionary to disk. Two things follow. It can raise `CapabilityProviderError` --
    # a deployment defect, not a `RegistryError`, and not something a publisher can act
    # on -- and every other refusal reachable from inside `tenant_tx` is a `RegistryError`
    # with a status; keeping the odd one out in front of the transaction is what keeps
    # that true. And it can do file I/O, which has no business running with an open
    # transaction holding a read on `artifacts`.
    #
    # Nothing is written before this point either way, so a raise here leaves no
    # half-published row -- but it leaves no open transaction to reason about either.
    # Resolved only for the kind that consults it, because a `model_version` publish has
    # no `spec.capabilities[].id` for `MOS-REG-029` to check.
    served_capabilities = (
        _served_capability_ids() if version_kind == "service_version" else frozenset()
    )

    tenant_id = current_tenant()
    bundle = dict(bundle or {})

    with tenant_tx(conn):
        existing = conn.execute(
            f"SELECT {_COLUMNS} FROM artifacts WHERE kind = %s AND family = %s "
            "AND version = %s",
            (family_kind, manifest["family"], manifest["version"]),
        ).fetchone()
        if existing is not None:
            if existing["manifest_digest"] == computed:
                return dict(existing), False        # MOS-REG-019: identical -> 200 no-op
            raise VersionConflict(
                f"{manifest['family']} {manifest['version']} is already published with "
                "different content",
                existing_content_digest=existing["manifest_digest"],
                submitted_content_digest=computed,
            )

        _check_cross_row_rules(conn, version_kind, manifest, served_capabilities)

        row = conn.execute(
            """
            INSERT INTO artifacts (
              public_id, tenant_id, kind, family, version, version_major, version_minor,
              version_patch, version_pre, lifecycle_status, published_by,
              manifest_schema_version, manifest, manifest_digest, oci_image_digest,
              bundle_bucket, bundle_object_key, bundle_digest, bundle_size_bytes,
              signature_alg, signature, signer_identity, sbom_object_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                public_id, tenant_id, family_kind, manifest["family"], manifest["version"],
                int(match["major"]), int(match["minor"]), int(match["patch"]),
                match["pre"] or "", lifecycle_status,
                published_by or manifest["publisher"]["org_id"], SCHEMA_VERSION,
                Jsonb(dict(manifest)), computed, oci_image_digest,
                bundle.get("bucket"), bundle.get("object_key"), bundle.get("digest"),
                bundle.get("size_bytes"), signature_alg, signature, signer_identity,
                sbom_object_key,
            ),
        ).fetchone()

        # MOS-REG-003: every mutation of an Artifact row emits an AuditEvent carrying the
        # actor, the permission exercised and the new row digest. Class `governance`,
        # which MOS-SEC-148 makes mandatory rather than sampled.
        audit.record(
            conn,
            action="artifact.publish",
            action_class="governance",
            actor=actor,
            resource=audit.Resource(
                kind=version_kind, id=public_id, version=str(manifest["version"])
            ),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "family": manifest["family"],
                "content_digest": computed,
                "lifecycle_status": lifecycle_status,
                "permission": "artifact.publish",
            },
        )
        written = conn.execute(
            f"SELECT {_COLUMNS} FROM artifacts WHERE id = %s", (row["id"],)
        ).fetchone()
    return dict(written), True


def _verify_supply_chain(
    version_kind: str,
    *,
    lifecycle_status: str,
    manifest: Mapping[str, Any],
    oci_image_digest: str | None,
    signature: bytes | None,
    signature_alg: str | None,
    signer_identity: str | None,
    sbom_object_key: str | None,
) -> None:
    """`MOS-REG-087`/`MOS-REG-089`/`MOS-REG-090`: registry admission, failing closed.

    From 0.3.0 the SBOM referrer is enforced and the signature has been enforced since
    0.2.0, so a `service_version` or `model_version` entering `REGISTERED` without a
    signature, a signer identity, a CycloneDX referrer and the signed OCI digest is
    refused. There is no parameter that turns this off: `MOS-REG-090` says a verification
    error "MUST NOT degrade to a warning, and MUST NOT be bypassable by configuration".

    WHAT THIS CANNOT DO YET, STATED RATHER THAN FAKED. `MOS-REG-028` requires the registry
    to verify that `publisher.signing_identity` is one of the identities registered FOR
    `legal_manufacturer.id`. No table in this schema holds that binding -- chapter 6 names
    no table for it and chapter 12 declares none -- so what is checked is the half that is
    checkable: the identity the signature was made with is the identity the manifest
    claims. The binding to the legal manufacturer is owed, and is in this component's
    report. The same goes for `MOS-REG-085` (no weights blob inside a service image),
    which needs an OCI client this tree does not have.
    """
    if version_kind not in ("service_version", "model_version"):
        return
    if lifecycle_status == "DRAFT":
        return  # MOS-REG-021: the signature is verified on DRAFT -> REGISTERED.

    missing = [
        name
        for name, value in (
            ("oci_image_digest", oci_image_digest),
            ("signature", signature),
            ("signature_alg", signature_alg),
            ("signer_identity", signer_identity),
            ("sbom_object_key", sbom_object_key),
        )
        if not value
    ]
    if missing:
        raise SupplyChainRefusal(
            f"registry admission requires {', '.join(missing)} for a {version_kind} "
            "(MOS-REG-087, MOS-REG-089); verification fails closed (MOS-REG-090)",
            missing=missing,
        )
    claimed = str(manifest["publisher"]["signing_identity"])
    if signer_identity != claimed:
        raise SupplyChainRefusal(
            "the signing identity does not match the identity the manifest declares "
            "(MOS-REG-028)",
            manifest_signing_identity=claimed,
            signer_identity=signer_identity,
        )


def _served_capability_ids() -> frozenset[str]:
    """What this DEPLOYMENT serves, for `MOS-REG-029`. The same answer as the API's.

    `medos.capabilities.providers` is the one composition of the platform registry and
    whatever `MEDOS_CAPABILITY_PROVIDERS` names -- `medos.api.routes_jobs.
    known_capability_ids()` and `medos.worker.steps.WorkerDeps.registry` are the other two
    readers of it, and the point of there being one function is that the three cannot
    disagree about which capabilities exist.

    Imported inside the call rather than at module top so that importing
    `medos.registry.repo` -- which the CLI, the migration tooling and half the test suite
    do -- does not pull in the capability implementations and their numerical stack.
    """
    from medos.capabilities.providers import capability_ids

    return capability_ids()


def _check_cross_row_rules(
    conn: psycopg.Connection[Any],
    version_kind: str,
    manifest: Mapping[str, Any],
    served_capabilities: frozenset[str],
) -> None:
    """The checks that need the rest of the registry. Called inside the publish scope.

    `served_capabilities` is passed in rather than resolved here: see `publish()` for why
    the one check that reads something outside the database reads it before the scope
    opens.
    """
    spec = manifest.get("spec") or {}

    if version_kind == "service_version":
        # MOS-REG-029: a claimed capability MUST exist. "Exists" means this deployment
        # serves it -- see the module docstring for the narrowing, and for why reading the
        # platform singleton here made a configured capability unpublishable.
        claimed = [c["id"] for c in spec.get("capabilities", [])]
        unknown = [c for c in claimed if c not in served_capabilities]
        if unknown:
            raise UnresolvedReference(
                f"capability {unknown} is not a supported capability (MOS-REG-029)",
                unknown_capabilities=unknown,
                supported_capabilities=sorted(served_capabilities),
            )

        models = list(spec.get("models") or ())
        primaries = [m for m in models if m.get("role") == "primary"]
        if spec.get("mode") == "sealed" and len(primaries) != 1:
            # MOS-REG-026: "A sealed service without a declared ModelVersion MUST be
            # rejected; otherwise its evidence has nothing to bind to."
            raise UnresolvedReference(
                f"a sealed service declares exactly one primary ModelVersion, found "
                f"{len(primaries)} (MOS-REG-026)"
            )
        if spec.get("mode") == "native" and not models:  # pragma: no cover - minItems 1
            raise UnresolvedReference("mode: native requires spec.models[] (MOS-REG-025)")

        for entry in models:
            _require_referenceable_model(conn, str(entry["ref"]))

    if version_kind == "model_version":
        # MOS-REG-103: "the registry MUST refuse a publish whose `evaluation_run_id` is
        # already referenced by another ModelVersion". Copying the source version's
        # metrics into a converted model is claiming evidence that was never produced for
        # the thing being served.
        run_id = spec.get("evaluation_run_id")
        clash = conn.execute(
            "SELECT public_id FROM artifacts WHERE kind = 'model' "
            "AND manifest -> 'spec' ->> 'evaluation_run_id' = %s LIMIT 1",
            (run_id,),
        ).fetchone()
        if clash is not None:
            raise UnresolvedReference(
                f"evaluation_run_id {run_id} is already cited by {clash['public_id']} "
                "(MOS-REG-103); a converted or re-published model needs its own run",
                evaluation_run_id=run_id,
                cited_by=clash["public_id"],
            )


def _require_referenceable_model(conn: psycopg.Connection[Any], ref: str) -> None:
    row = conn.execute(
        "SELECT public_id, lifecycle_status FROM artifacts "
        "WHERE kind = 'model' AND public_id = %s",
        (ref,),
    ).fetchone()
    if row is None:
        raise UnresolvedReference(
            f"spec.models[].ref {ref!r} resolves to no model_version. Dangling refs are "
            "rejected at publish, not at dispatch (MOS-REG-025).",
            unresolved_ref=ref,
        )
    if row["lifecycle_status"] not in _REFERENCEABLE:
        raise UnresolvedReference(
            f"{ref} is {row['lifecycle_status']}; a referenced ModelVersion MUST be "
            f"{' or '.join(_REFERENCEABLE)} at publish time (MOS-REG-025)",
            unresolved_ref=ref,
            lifecycle_status=row["lifecycle_status"],
        )


# =====================================================================================
# Read
# =====================================================================================
def get(
    conn: psycopg.Connection[Any], ident: str, *, kind: str | None = None
) -> dict[str, Any]:
    """One artifact by `public_id` or uuid, within the tenant's visibility.

    `MOS-REG-107`: the row simply is not visible to a non-entitled tenant, so this raises
    `ArtifactNotFound` -- a 404, never a 403. A 403 would confirm that the id names
    something, which is the disclosure the requirement closes.
    """
    column = "id::text" if _UUID_RE.match(ident) else "public_id"
    sql = f"SELECT {_COLUMNS} FROM artifacts WHERE {column} = %s"
    params: list[Any] = [ident]
    if kind is not None:
        sql += " AND kind = %s"
        params.append(FAMILY_KIND.get(kind, kind))
    with tenant_tx(conn):
        row = conn.execute(sql, tuple(params)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no artifact {ident}")
    return dict(row)


def list_artifacts(
    conn: psycopg.Connection[Any],
    *,
    kind: str | None = None,
    family: str | None = None,
    status_in: Sequence[str] | None = None,
    capability_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """The list surface behind rows 14 and 28 of table 10.2-B, tenant-scoped by RLS."""
    sql = f"SELECT {_COLUMNS} FROM artifacts WHERE true"
    params: list[Any] = []
    if kind is not None:
        sql += " AND kind = %s"
        params.append(FAMILY_KIND.get(kind, kind))
    if family is not None:
        sql += " AND family = %s"
        params.append(family)
    if status_in:
        sql += " AND lifecycle_status = ANY(%s)"
        params.append(list(status_in))
    if capability_id is not None:
        # Both kinds declare their capabilities, in two shapes: a ServiceVersion carries
        # objects (`[{id: ...}]`), a ModelVersion carries strings.
        sql += (
            " AND (manifest -> 'spec' -> 'capabilities' @> %s::jsonb"
            "   OR manifest -> 'spec' -> 'capabilities' @> %s::jsonb)"
        )
        params += [f'[{{"id": "{capability_id}"}}]', f'["{capability_id}"]']
    sql += " ORDER BY family, version_major DESC, version_minor DESC, version_patch DESC"
    sql += " LIMIT %s"
    params.append(max(1, min(int(limit), 500)))
    with tenant_tx(conn):
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


# =====================================================================================
# Transition
# =====================================================================================
def set_status(
    conn: psycopg.Connection[Any],
    ident: str,
    *,
    to_status: str,
    reason: str | None,
    actor: audit.Actor,
    trace_id: str,
    request_id: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """`POST /{...}/status` (§6.11). The Python half of `MOS-REG-021`.

    The database trigger is the guarantee and this is the good error message; both refuse
    the same edges, and `test_python_and_sql_agree_on_the_lifecycle_graph` pins that.
    `permission_for()` is exported so the HTTP layer can check the permission BEFORE the
    write rather than inferring it from a failure.
    """
    row = get(conn, ident, kind=kind)
    version_kind = VERSION_KIND.get(row["kind"], row["kind"])
    previous = _previous_status(conn, row["id"]) if row["lifecycle_status"] == "SUSPENDED" \
        else None
    allowed, reason_code = transition_allowed(
        row["kind"], row["lifecycle_status"], to_status, previous_status=previous
    )
    if not allowed:
        raise LifecycleViolation(
            f"{row['lifecycle_status']} -> {to_status} is refused: {reason_code} "
            "(MOS-REG-021, MOS-REG-022)",
            reason_code=reason_code,
            from_status=row["lifecycle_status"],
            to_status=to_status,
            previous_status=previous,
        )
    if to_status == "SUSPENDED" and not (reason or "").strip():
        raise LifecycleViolation(
            "a suspension names its reason (MOS-REG-021)", reason_code="reason_required"
        )

    with tenant_tx(conn):
        updated = conn.execute(
            "UPDATE artifacts SET lifecycle_status = %s, status_reason = %s "
            f"WHERE id = %s RETURNING {_COLUMNS}",
            (to_status, reason, row["id"]),
        ).fetchone()
        audit.record(
            conn,
            action=f"artifact.status.{to_status.lower()}",
            action_class="governance",
            actor=actor,
            resource=audit.Resource(
                kind=version_kind, id=row["public_id"], version=row["version"]
            ),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "from_status": row["lifecycle_status"],
                "to_status": to_status,
                "status_reason": reason,
                "content_digest": row["manifest_digest"],
                "permission": permission_for(to_status, from_status=row["lifecycle_status"]),
            },
        )
    return dict(updated)


def _previous_status(conn: psycopg.Connection[Any], artifact_id: str) -> str | None:
    """The status held before the suspension, from `registry_changelog` (`MOS-REG-010`).

    The same query the lifecycle trigger runs. No column remembers this -- §12.9 provides
    nowhere to put it -- so the append-only log is the record, which is the better answer
    anyway: it is also the audit trail of who suspended what and when.
    """
    row = conn.execute(
        "SELECT row_json ->> 'lifecycle_status' AS status FROM registry_changelog "
        "WHERE table_name = 'artifact' AND row_id = %s "
        "AND row_json ->> 'lifecycle_status' <> 'SUSPENDED' "
        "ORDER BY epoch DESC LIMIT 1",
        (artifact_id,),
    ).fetchone()
    return None if row is None else row["status"]


# =====================================================================================
# Impact -- MOS-REG-040
# =====================================================================================
def impact(
    conn: psycopg.Connection[Any], ident: str, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Every result this version produced. `MOS-REG-040`: "a recall state with no impact
    query is not a recall."

    NARROWER THAN THE REQUIREMENT, AND SAYING SO. `MOS-REG-040` computes this from the
    pinned `job.resolution` record of §6.7.5, which the capability-resolution component of
    this release introduces; until that column exists the join is on the denormalised
    `jobs.service_id` / `jobs.service_version` that `MOS-EXEC-086` already puts on every
    job. For a `model_version` the impact is the union over every `service_version` whose
    manifest references it, which is the same set as long as a job runs exactly the
    service version it resolved -- the very thing the pin exists to guarantee. Reported.
    """
    row = get(conn, ident)
    with tenant_tx(conn):
        if row["kind"] == "model":
            families = conn.execute(
                "SELECT family, version FROM artifacts WHERE kind = 'service' "
                "AND manifest -> 'spec' -> 'models' @> %s::jsonb",
                (f'[{{"ref": "{row["public_id"]}"}}]',),
            ).fetchall()
            pairs = [(r["family"], r["version"]) for r in families]
        else:
            pairs = [(row["family"], row["version"])]
        if not pairs:
            return []
        out: list[dict[str, Any]] = []
        for family, version in pairs:
            found = conn.execute(
                """
                SELECT j.public_id AS job_id, r.id::text AS result_id,
                       rdo.series_instance_uid, j.tenant_id::text AS tenant_id
                  FROM jobs j
                  LEFT JOIN results r ON r.job_id = j.id
                  LEFT JOIN result_dicom_objects rdo ON rdo.result_id = r.id
                 WHERE j.service_id = %s AND j.service_version = %s
                 ORDER BY j.created_at DESC
                 LIMIT %s
                """,
                (family, version, max(1, min(int(limit), 1000))),
            ).fetchall()
            out += [dict(f) for f in found]
    return out[:limit]
