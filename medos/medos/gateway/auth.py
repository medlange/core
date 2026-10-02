# SPDX-License-Identifier: Apache-2.0
"""The Gateway's principal: who is calling, which tenant they are, what egress they get.

This module does NOT define a second authentication system. `medos.security.authn` owns
the `Authenticator` port, the refusal taxonomy and the control-plane `Principal`
(15.2.4 item 2, `MOS-SEC-009`); everything here either imports from it or adapts it.

What this module adds, and why the Gateway needs it added
---------------------------------------------------------
`medos.security.authn.Principal` answers "who and which tenant". The Gateway has to
answer three more questions before it may return a single byte of pixel data, and none of
them are authentication questions:

  * MOS-DATA-021  which **consumer class** this principal is, because that is what selects
                  de-identification, UID space and pixel-PHI screening on egress.
  * MOS-DATA-018  whether the principal is **scoped to named studies** -- the `study`
                  claim of a Job Token. A viewer is not; a job's service token must be.
  * MOS-SEC-147   the five-field audit actor. `medos.db.audit.Actor.from_principal`
                  duck-types on `actor_kind` / `principal_id` / `auth_method` / `key_id`,
                  which are this class's attribute names and are the same five names
                  MOS-SEC-146 uses.

Requirements implemented here
-----------------------------
MOS-DATA-009  "The effective tenant of a request is derived from the authenticated
              principal, never from the URL." `Principal.tenant_id` is the only tenant the
              Gateway binds. `require_tenant_match()` is the `{t}`-segment comparison.
MOS-DATA-017  "A `Service` container MUST receive a scoped Gateway token, never a
              credential", presented as `Authorization: Bearer <token>`.
MOS-DATA-021  consumer class resolution, including its conservative default.

WHY THE START-UP DRIVER IS A FILE OF DIGESTS
--------------------------------------------
`StaticApiKeyAuthenticator` is the driver the Gateway boots with, because MOS-DATA-005
delivers the Gateway's own configuration "from a secret store at start-up" and because the
Gateway's principals are deployment topology -- the viewer, the worker, the platform
writer -- not tenant-managed API keys. The file holds `key_sha256` and never a key: it is
mounted into a container, and a mounted file holding a live credential is the posture
MOS-DATA-005 forbids one layer down for the PACS credential. A leaked principals file
grants nothing.

When the database-backed `api_keys` driver of 15.2.4 item 2 lands, it is a second
`Authenticator` and `create_app(authenticator=...)` is the only line that changes:
`Principal.from_security_principal()` already converts its output.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from medos.security.authn import (
    AuthenticationError,
    Credential,
    MalformedCredential,
    MissingCredential,
    UnknownCredential,
)
from medos.security.authn import Principal as SecurityPrincipal
from medos.security.scopes import normalise_scope

__all__ = [
    "Principal",
    "StaticApiKeyAuthenticator",
    "CONSUMER_CLASSES",
    "ACTOR_KINDS",
    "ENV_PRINCIPALS",
    "ENV_PRINCIPALS_FILE",
    "ENV_CLINICAL_USE_MODE",
    "resolve_consumer_class",
    "TenantMismatch",
]

log = logging.getLogger("medos.gateway.auth")

ENV_PRINCIPALS = "MEDOS_GATEWAY_PRINCIPALS"            # inline JSON
ENV_PRINCIPALS_FILE = "MEDOS_GATEWAY_PRINCIPALS_FILE"  # a path to the same JSON
ENV_CLINICAL_USE_MODE = "MEDOS_CLINICAL_USE_MODE"

# MOS-DATA-021, table 3.2.4. Closed.
CONSUMER_CLASSES: frozenset[str] = frozenset(
    {"clinical_viewer", "research_viewer", "service", "platform_writer", "dataset_export"}
)

# MOS-SEC-147 / the `actor_kind` CHECK of `audit_events`. Same set as
# `medos.db.audit.ACTOR_KINDS`; imported rather than restated would be better, but
# `medos.gateway` importing `medos.db.audit` for a constant would make an import cycle out
# of a five-element tuple. A test asserts the two agree.
ACTOR_KINDS: frozenset[str] = frozenset(
    {"user", "service_account", "workload", "service_version", "platform_admin"}
)


class TenantMismatch(AuthenticationError):
    """MOS-DATA-009: the `{t}` path segment disagrees with the principal's tenant.

    403 and not 404, and MOS-DATA-013 says why the distinction is deliberate: "`403` is
    reserved for MOS-DATA-009 (tenant segment spoofing) and for a caller whose own tenant
    owns the study but who lacks `study.read`." Spoofing the segment reveals nothing about
    what the deployment holds, so there is no existence oracle to protect here.
    """

    status = 403
    code = "TENANT_MISMATCH"


@dataclass(frozen=True)
class Principal:
    """The authenticated caller as the Gateway needs it. Nothing else decides anything.

    Attribute names are load-bearing in two directions: `medos.db.audit.Actor
    .from_principal` reads `actor_kind`, `principal_id`, `auth_method` and `key_id`, and
    MOS-SEC-146 names those same four as the audit `actor` members.
    """

    principal_id: str
    actor_kind: str
    tenant_id: str
    scopes: frozenset[str] = frozenset()
    key_id: str = ""
    on_behalf_of_kind: str | None = None
    on_behalf_of_id: str | None = None
    on_behalf_of_role: str | None = None
    job_id: str | None = None
    # MOS-DATA-018's `study` claim. EMPTY means "every study this tenant owns", which is
    # what a viewer principal gets. A job-scoped service token MUST NOT be empty, and
    # `may_see_study` is where that is enforced rather than reviewed.
    study_scope: frozenset[str] = frozenset()
    consumer_class_override: str | None = None
    auth_method: str = "api_key"

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def may_see_study(self, study_instance_uid: str) -> bool:
        return not self.study_scope or study_instance_uid in self.study_scope

    @classmethod
    def from_security_principal(
        cls,
        principal: SecurityPrincipal,
        *,
        consumer_class_override: str | None = None,
        job_id: str | None = None,
        study_scope: frozenset[str] = frozenset(),
    ) -> Principal:
        """Adapt `medos.security.authn.Principal`. The seam item 2's driver plugs into.

        `kind` there is `user | service_account` (chapter 8 table 8.2.1's two issuable
        kinds); `actor_kind` here is MOS-SEC-146's wider five-value audit set, of which
        those two are members. The widening is safe and the narrowing never happens.
        """
        return cls(
            principal_id=principal.principal_id,
            actor_kind=principal.kind,
            tenant_id=principal.tenant_id,
            scopes=frozenset(principal.scope),
            key_id=principal.credential_key_id or "",
            job_id=job_id,
            study_scope=study_scope,
            consumer_class_override=consumer_class_override,
        )

    def __repr__(self) -> str:
        return (
            f"Principal(id={self.principal_id!r}, kind={self.actor_kind!r}, "
            f"tenant={self.tenant_id!r}, scopes={sorted(self.scopes)!r})"
        )

    def log_fields(self) -> dict[str, str]:
        """The subset that may appear in a log line. No secret, no display name.

        Mirrors `medos.security.authn.Principal.log_fields` on purpose: two log shapes for
        one concept makes the trail unqueryable.
        """
        out = {
            "principal_kind": self.actor_kind,
            "principal_id": self.principal_id,
            "tenant_id": self.tenant_id,
        }
        if self.key_id:
            out["api_key_id"] = self.key_id
        return out


def resolve_consumer_class(principal: Principal, *, clinical_use_mode: str) -> str:
    """MOS-DATA-021, in the table's own order.

    | Consumer class    | Principal                                            |
    |-------------------|------------------------------------------------------|
    | `clinical_viewer` | `User` with `study.read`, Deployment mode `clinical`  |
    | `research_viewer` | `User` with `study.read`, mode `research_only`        |
    | `service`         | `ServiceAccount` with a scoped token                  |
    | `platform_writer` | control-plane DICOM writer                            |
    | `dataset_export`  | evidence-plane exporter (Chapter 7)                   |

    "A consumer class that cannot be determined MUST be treated as `service`. This is the
    conservative default: it de-identifies." That is why the fallback is not
    `clinical_viewer` even though this deployment's only human principal is a clinician:
    the wrong default here ships identified pixels to something that should not hold them.
    """
    if principal.consumer_class_override:
        return principal.consumer_class_override
    if principal.actor_kind == "user" and principal.has("study.read"):
        return "clinical_viewer" if clinical_use_mode == "clinical" else "research_viewer"
    return "service"


def require_tenant_match(principal: Principal, path_tenant: str) -> None:
    """MOS-DATA-009's equality comparison. Raises `TenantMismatch`.

    "The `{t}` path segment is a routing convenience and MUST be compared for equality
    with the principal's tenant; on mismatch the Gateway MUST return `403` ... and MUST
    emit an `AuditEvent` of type `gateway.tenant_mismatch`." The audit row is the route's
    job -- it needs the trace id -- so this function raises and does not write.
    """
    if str(path_tenant).strip().lower() != str(principal.tenant_id).strip().lower():
        raise TenantMismatch(
            "the tenant in the path is not the authenticated principal's tenant"
        )


@dataclass(frozen=True)
class StaticApiKeyAuthenticator:
    """Gateway principals from a JSON document, matched by SHA-256 digest of the key.

    DEVIATION FROM MOS-DATA-017/018/019, STATED RATHER THAN HIDDEN
    --------------------------------------------------------------
    A `Service` is supposed to present a per-job JWT whose lifetime is
    `min(job_deadline_at, iat + 1800s)` and whose `job_id` the Gateway re-checks against
    the job table, "because a cancelled or failed job must lose PHI access immediately"
    (MOS-DATA-019). This driver mints nothing and expires nothing: it matches a long-lived
    key. The consequence is concrete -- a leaked worker key keeps PHI access after its job
    is cancelled -- and it is written here because the control-plane token minting is
    Chapter 5's dispatcher, which this block does not build.

    What survives the swap is everything downstream: `Principal.job_id` and
    `Principal.study_scope` exist and are already enforced by the routes, so the JWT driver
    fills them from claims instead of from a file and no route changes.

    Document shape::

        {"clinical_use_mode": "clinical",
         "principals": [
           {"principal_id": "user:ohif", "actor_kind": "user",
            "tenant_id": "0000...", "scopes": ["study.read"],
            "key_id": "ohif-v1", "key_sha256": "<64 hex>"}]}
    """

    principals: tuple[Principal, ...] = ()
    digests: tuple[str, ...] = ()
    clinical_use_mode: str = "clinical"

    def __post_init__(self) -> None:
        if len(self.principals) != len(self.digests):
            raise ValueError("principals and digests must be the same length")

    # -- construction ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StaticApiKeyAuthenticator:
        src = dict(os.environ) if env is None else env
        raw = src.get(ENV_PRINCIPALS)
        if not raw:
            path = src.get(ENV_PRINCIPALS_FILE)
            if not path:
                raise ValueError(
                    f"the Gateway needs a principal set: set {ENV_PRINCIPALS} (inline "
                    f"JSON) or {ENV_PRINCIPALS_FILE} (a path). An unauthenticated Gateway "
                    "is a PACS with extra hops (MOS-DATA-009)."
                )
            raw = Path(path).read_text(encoding="utf-8")
        return cls.from_document(json.loads(raw), default_mode=src.get(ENV_CLINICAL_USE_MODE))

    @classmethod
    def from_document(
        cls, doc: dict[str, Any], *, default_mode: str | None = None
    ) -> StaticApiKeyAuthenticator:
        mode = doc.get("clinical_use_mode") or default_mode or "clinical"
        if mode not in ("clinical", "research_only"):
            raise ValueError(
                f"clinical_use_mode {mode!r} is not one of clinical, research_only "
                "(MOS-DATA-021)"
            )
        principals: list[Principal] = []
        digests: list[str] = []
        for entry in doc.get("principals", ()):
            for forbidden in ("key", "secret", "password", "token"):
                if forbidden in entry:
                    raise ValueError(
                        f"principal {entry.get('principal_id')!r} carries a plaintext "
                        f"{forbidden!r}; only key_sha256 is permitted in this document."
                    )
            digest = str(entry.get("key_sha256", "")).strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(
                    f"principal {entry.get('principal_id')!r}: key_sha256 must be 64 hex "
                    "characters."
                )
            kind = str(entry.get("actor_kind", "service_account"))
            if kind not in ACTOR_KINDS:
                raise ValueError(f"actor_kind {kind!r} is not one of {sorted(ACTOR_KINDS)}")
            override = entry.get("consumer_class")
            if override is not None and override not in CONSUMER_CLASSES:
                raise ValueError(f"consumer_class {override!r} is not a MOS-DATA-021 class")
            principals.append(
                Principal(
                    principal_id=str(entry["principal_id"]),
                    actor_kind=kind,
                    tenant_id=str(entry["tenant_id"]),
                    # MOS-SEC-031's grammar, validated by the platform's own validator so
                    # a Gateway principal cannot hold a scope an api_keys row could not.
                    scopes=frozenset(normalise_scope(entry.get("scopes", ()))),
                    key_id=str(entry.get("key_id", "")),
                    on_behalf_of_kind=entry.get("on_behalf_of_kind"),
                    on_behalf_of_id=entry.get("on_behalf_of_id"),
                    on_behalf_of_role=entry.get("on_behalf_of_role"),
                    job_id=entry.get("job_id"),
                    study_scope=frozenset(entry.get("study_scope", ())),
                    consumer_class_override=override,
                )
            )
            digests.append(digest)
        if not principals:
            raise ValueError("the principal set is empty (MOS-DATA-009)")
        return cls(tuple(principals), tuple(digests), mode)

    # -- the `medos.security.authn.Authenticator` port ---------------------------------
    def authenticate(self, credential: Credential) -> Principal:
        """Resolve a `Credential` to a Gateway `Principal`, or raise.

        Signature is `medos.security.authn.Authenticator`'s. The return type is this
        module's richer `Principal`, which is a widening: every field of the port's
        `Principal` is present under the same meaning.
        """
        if credential.scheme.lower() != "bearer":
            raise MalformedCredential(
                f"the Gateway accepts `Authorization: Bearer <token>` "
                f"(MOS-DATA-017); got scheme {credential.scheme!r}"
            )
        if not credential.secret:
            raise MissingCredential("no bearer token was presented")
        presented = hashlib.sha256(credential.secret.encode("utf-8")).hexdigest()
        found: Principal | None = None
        for principal, digest in zip(self.principals, self.digests, strict=True):
            # Not a dict lookup and no early break: both leak a match through timing, and
            # the loop is over a handful of deployment principals.
            if hmac.compare_digest(presented, digest):
                found = principal
        if found is None:
            raise UnknownCredential("the presented credential matches no principal")
        return found

    def consumer_class(self, principal: Principal) -> str:
        return resolve_consumer_class(principal, clinical_use_mode=self.clinical_use_mode)


def credential_from_headers(headers: Any, *, source_ip: str | None) -> Credential:
    """Normalise `Authorization: Bearer <token>` off the transport. MOS-DATA-017.

    Raises `MissingCredential` rather than returning an empty credential: a falsy
    credential that a caller forgets to check is the same defect class as an RLS predicate
    that returns an empty set instead of erroring.
    """
    raw = ""
    try:
        raw = str(headers.get("authorization") or "")
    except AttributeError:  # pragma: no cover - defensive
        raw = ""
    if not raw:
        raise MissingCredential("no Authorization header (MOS-DATA-017)")
    scheme, _, value = raw.partition(" ")
    if not value.strip():
        raise MalformedCredential("Authorization must be `Bearer <token>`")
    return Credential(scheme=scheme, secret=value.strip(), source_ip=source_ip)
