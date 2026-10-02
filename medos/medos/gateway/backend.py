# SPDX-License-Identifier: Apache-2.0
"""`PacsBackend` and `BackendResolver` -- MOS-DATA-014, rendered in Python.

Chapter 3 states the port in Go::

    type PacsBackend interface {
        BaseURL() string
        Credential() Credential
        SupportsRendered() bool
    }
    type BackendResolver interface {
        Resolve(ctx context.Context, tenantID string) (PacsBackend, error)
    }

and then states the requirement the shape exists for: "A single shared backend and one
backend per tenant MUST both be expressible without a Gateway code change. The choice
between them is an open deployment question (Chapter 16) and this chapter deliberately
does not settle it." So this module ships both resolvers and the routes take the port.

MOS-DATA-005 is the other half, and it is why the credential is read from the environment
here and from nowhere else in the process: "The credential MUST be delivered to the
Gateway process from a secret store at start-up and MUST NOT appear in any image layer,
any Helm values file committed to the repository, or any environment variable of any other
deployment unit." `pacs_backends` (migration 0004) carries the base URL and deliberately
has no credential column, with a migration-time assertion that keeps it that way.

MOS-DATA-002 is what all of this is in service of: "No component other than the DICOM
Gateway MAY hold, mount, receive or derive a PACS credential. This includes the control
plane, workers, service containers (both `native` and `sealed`), the OHIF viewer, the
evidence-plane exporter, developer tooling, and Airflow-style batch jobs."
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from medos.dicomweb.gateway import PacsCredentials

__all__ = [
    "PacsBackend",
    "BackendResolver",
    "SharedBackendResolver",
    "PerTenantBackendResolver",
    "UnknownBackend",
    "ENV_PACS_URL",
    "ENV_PACS_USER",
    "ENV_PACS_PASSWORD",
    "ENV_PACS_BACKEND_ID",
    "backend_from_env",
]

ENV_PACS_URL = "MEDOS_GATEWAY_PACS_URL"
ENV_PACS_USER = "MEDOS_GATEWAY_PACS_USER"
ENV_PACS_PASSWORD = "MEDOS_GATEWAY_PACS_PASSWORD"  # a variable NAME, not a secret
ENV_PACS_TOKEN = "MEDOS_GATEWAY_PACS_TOKEN"  # a variable NAME, not a secret
ENV_PACS_BACKEND_ID = "MEDOS_GATEWAY_PACS_BACKEND_ID"
ENV_PACS_RENDERED = "MEDOS_GATEWAY_PACS_SUPPORTS_RENDERED"
ENV_PACS_TIMEOUT = "MEDOS_GATEWAY_PACS_TIMEOUT_S"


class UnknownBackend(LookupError):
    """No backend is configured for this tenant. MOS-DATA-025: the Gateway fails closed."""


@dataclass(frozen=True)
class PacsBackend:
    """One PACS, its credential, and what it can do. The Go interface's four members.

    `credential` is a `PacsCredentials`, which never prints its value -- see its `__repr__`.
    A `PacsBackend` therefore cannot leak the credential into a traceback or a log line by
    being included in one, which matters because the routes log the backend id.
    """

    backend_id: str
    base_url: str
    credential: PacsCredentials
    supports_rendered: bool = True
    timeout_s: float = 300.0

    def summary(self) -> dict[str, object]:
        """Loggable. Never the credential value -- only its scheme."""
        return {
            "backend_id": self.backend_id,
            "base_url": self.base_url,
            "auth_scheme": self.credential.scheme,
            "supports_rendered": self.supports_rendered,
        }


class BackendResolver(Protocol):
    def resolve(self, tenant_id: str) -> PacsBackend: ...


@dataclass(frozen=True)
class SharedBackendResolver:
    """One PACS for every tenant; the Gateway is what keeps them apart.

    `pacs_backends.partitioning = 'shared_gateway_filtered'`. This is the deployment the
    compose stack runs and the one MOS-DATA-011 is written for: "a PACS that has no
    tenancy model will silently ignore an unknown matching key and return everything", so
    the tenancy predicate has to be applied by the Gateway after the backend answers.
    """

    backend: PacsBackend

    def resolve(self, tenant_id: str) -> PacsBackend:
        return self.backend


@dataclass(frozen=True)
class PerTenantBackendResolver:
    """One PACS per tenant. `pacs_backends.partitioning = 'per_tenant'`.

    Present so that MOS-DATA-014's "MUST both be expressible without a Gateway code
    change" is a demonstrated property rather than a claim. It fails closed on an unmapped
    tenant (MOS-DATA-025) rather than falling back to a default backend, because a
    fallback here sends tenant A's retrieval to tenant B's PACS.
    """

    backends: dict[str, PacsBackend]

    def resolve(self, tenant_id: str) -> PacsBackend:
        try:
            return self.backends[tenant_id]
        except KeyError:
            raise UnknownBackend(
                f"no PACS backend is configured for tenant {tenant_id}"
            ) from None


def backend_from_env(env: dict[str, str] | None = None) -> PacsBackend:
    """The one place in the Gateway process that reads a PACS credential. MOS-DATA-005.

    A second reader is how "the Gateway is the only credential holder" silently stops
    being true, so the variable names are module constants and this function is their only
    consumer. `medos.dicomweb.gateway.GatewayConfig.from_env` reads a DIFFERENT set of
    names (`MEDOS_DICOMWEB_*`) on purpose: that object is the CLIENT the worker uses, it
    now points at this Gateway rather than at the PACS, and giving the two the same
    variable names would make a mis-set worker env hand the worker a PACS credential.
    """
    src = dict(os.environ) if env is None else env
    url = src.get(ENV_PACS_URL)
    if not url:
        raise ValueError(
            f"{ENV_PACS_URL} is required: the Gateway is the only component that may hold "
            "a PACS address and credential (MOS-DATA-002, MOS-DATA-005)."
        )
    rendered = str(src.get(ENV_PACS_RENDERED, "true")).strip().lower()
    return PacsBackend(
        backend_id=src.get(ENV_PACS_BACKEND_ID) or "orthanc-local",
        base_url=url.rstrip("/"),
        credential=PacsCredentials(
            user=src.get(ENV_PACS_USER) or None,
            password=src.get(ENV_PACS_PASSWORD) or None,
            bearer_token=src.get(ENV_PACS_TOKEN) or None,
        ),
        supports_rendered=rendered not in ("0", "false", "no", "off"),
        timeout_s=float(src.get(ENV_PACS_TIMEOUT) or 300.0),
    )
