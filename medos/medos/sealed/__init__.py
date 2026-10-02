# SPDX-License-Identifier: Apache-2.0
"""Sealed-mode services: the second execution mode of chapter 2, and its isolation.

Chapter 2 defines two execution modes on ONE manifest (`MOS-SVC-035`: a `ServiceVersion`
declares exactly one). 0.1.0 built `native`: the vendor ships a wheel and weights, the
platform builds the canonical volume and runs inference on its own Triton. This package
is `sealed`, which `MOS-SVC-118` places in 0.3.0 and not before:

    the service brings its own runtime, the platform exchanges only the canonical volume
    and the `ResultBundle`, and the vendor never surrenders weights.

Four modules, and the split is the point:

  `abi.py`        the wire contract of chapter 2 section 2.5.2 -- the CLOSED endpoint
                  table, the execution request, the poll response, and the two digests
                  (`MOS-SVC-026`, `MOS-SVC-027`). No I/O.
  `invoker.py`    the PLATFORM side of section 2.6.2 steps 3-10, including the three
                  verifications `MOS-SVC-052` forbids configuring off.
  `reference.py`  the VENDOR side: a stdlib-only reference sealed service. It is what
                  `medos/deploy/compose/Dockerfile.sealed` builds into its own image, and it
                  imports nothing from `medos` -- a sealed service that needed the
                  platform's package would not be sealed.
  `isolation.py`  the isolation contract as data, plus the checker that evaluates a
                  RUNNING deployment against it.

WHY ISOLATION IS IN THIS PACKAGE AND NOT IN A DOCUMENT

`MOS-SEC-004`: "This MUST be enforced by NetworkPolicy (Kubernetes) or by per-service
networks (compose), not only by configuration of the client." The zone table of section
8.1.1 gives `Z-SERVICE` exactly one egress -- `Z-GATEWAY` and the platform callback -- and
threat T3 is a malicious sealed service exfiltrating a study. A sealed container that
holds no database credential but can still open a TCP connection to Postgres has not been
isolated; it has been asked politely. `isolation.py` states the forbidden destinations and
`tests/integration/test_sealed_mode.py` opens the connections.

Spec: chapter 2 sections 2.3.1, 2.5, 2.5.2, 2.6.2, 2.7; chapter 8 sections 8.1.1, 8.2.5,
8.6; `MOS-SVC-025`, `MOS-SVC-029`, `MOS-SVC-043`..`MOS-SVC-052`, `MOS-SVC-062`..
`MOS-SVC-065`, `MOS-SVC-118`, `MOS-SEC-004`, `MOS-SEC-029`, `MOS-SEC-087`, `MOS-SEC-088`,
`MOS-SEC-094`, `MOS-SEC-096`, `MOS-SEC-119`.
"""

from __future__ import annotations

from medos.sealed.abi import (
    ABI,
    ENDPOINTS,
    EXECUTION_STATES,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    VOLATILE_BUNDLE_MEMBERS,
    Endpoint,
    ExecutionRequest,
    ExecutionStatus,
    endpoint_for,
    manifest_digest,
    selftest_digest,
)
from medos.sealed.errors import (
    ArtifactRefused,
    BundleInvalid,
    EvidenceOutsideSelection,
    ExecutionConflict,
    ManifestMismatch,
    ManifestNotCanonicalisable,
    ProtocolViolation,
    SealedError,
    SelftestMismatch,
    ServiceFailed,
    ServiceTimeout,
    TransportRefused,
)
from medos.sealed.invoker import (
    SealedExecutionResult,
    SealedInvoker,
    SealedServiceConfig,
    UrllibTransport,
)
from medos.sealed.isolation import (
    ALLOWED_EGRESS,
    FORBIDDEN_DESTINATIONS,
    FORBIDDEN_ENV_SUBSTRINGS,
    SEALED_NETWORK_SUFFIX,
    SEALED_SERVICE_CONTAINER,
    ContainerObservation,
    Destination,
    hardening_violations,
    isolation_violations,
)
from medos.sealed.nifti import (
    Nifti1Header,
    affine_from_geometry,
    geometry_problems,
    read_nifti1_header,
)

__all__ = [
    "ABI",
    "ALLOWED_EGRESS",
    "ArtifactRefused",
    "BundleInvalid",
    "ContainerObservation",
    "Destination",
    "ENDPOINTS",
    "EXECUTION_STATES",
    "Endpoint",
    "EvidenceOutsideSelection",
    "ExecutionConflict",
    "ExecutionRequest",
    "ExecutionStatus",
    "FORBIDDEN_DESTINATIONS",
    "FORBIDDEN_ENV_SUBSTRINGS",
    "ManifestMismatch",
    "ManifestNotCanonicalisable",
    "Nifti1Header",
    "ProtocolViolation",
    "SCHEMA_VERSION",
    "SEALED_NETWORK_SUFFIX",
    "SEALED_SERVICE_CONTAINER",
    "SealedError",
    "SealedExecutionResult",
    "SealedInvoker",
    "SealedServiceConfig",
    "SelftestMismatch",
    "ServiceFailed",
    "ServiceTimeout",
    "TERMINAL_STATES",
    "TransportRefused",
    "UrllibTransport",
    "VOLATILE_BUNDLE_MEMBERS",
    "affine_from_geometry",
    "endpoint_for",
    "geometry_problems",
    "hardening_violations",
    "isolation_violations",
    "manifest_digest",
    "read_nifti1_header",
    "selftest_digest",
]
