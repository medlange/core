# SPDX-License-Identifier: Apache-2.0
"""Registry refusals, each carrying the status and problem code chapter 6 names for it.

Chapter 6 is unusually specific about the HTTP answer to each refusal -- 422 for a
manifest that fails its schema (`MOS-REG-013`), 409 for a conflicting republish quoting
the existing digest (`MOS-REG-019`), 409 for a transition out of `RECALLED`
(`MOS-REG-022`), 405 for a PATCH (§6.11). Those statuses are part of the contract, so they
live on the exception rather than in the handler: a second caller of `medos.registry.repo`
(the worker, a CLI, the conformance suite) gets the same classification without copying a
mapping table.

`problem_class` is `medos.api.problems`' table 10.4-A word, not free text; `MOS-API-038`
makes a class minted outside that table a defect.

REPORTED, NOT SILENTLY RESOLVED. Chapter 6 writes `class: validation_error` twice
(`MOS-REG-013`, and §6.3's reject of an unknown `kind`), and `validation_error` is not one
of table 10.4-A's six class words -- `MOS-API-038` calls a class minted outside that table
a defect, and chapter 10 is normative for the wire shape. Every class below is therefore
`client_error`, which is the table's word for a 4xx that is not an authz failure, a rate
limit or a clinical rejection. The contradiction is in this component's report; it is not
resolved by widening the enum, because the enum being closed is the point.

Spec: MOS-REG-013, MOS-REG-014, MOS-REG-018, MOS-REG-019, MOS-REG-021, MOS-REG-022,
MOS-API-036, MOS-API-038.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ArtifactNotFound",
    "ImmutableArtifact",
    "InvalidManifest",
    "LifecycleViolation",
    "RegistryError",
    "SupplyChainRefusal",
    "UnknownKind",
    "UnresolvedReference",
    "VersionConflict",
]


class RegistryError(Exception):
    """Base. `status`, `code` and `problem_class` are class attributes by design.

    The class IS the classification. Deciding it per instance is how one refusal ends up
    reported two ways depending on which call site raised it.
    """

    status: int = 400
    code: str = "REGISTRY_ERROR"
    problem_class: str = "client_error"

    def __init__(self, detail: str, **extensions: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extensions = extensions


class InvalidManifest(RegistryError):
    """`MOS-REG-014`: schema validation failure is a HARD reject. No store-and-validate."""

    status = 422
    code = "MANIFEST_SCHEMA_VIOLATION"
    problem_class = "client_error"


class UnknownKind(RegistryError):
    """`MOS-REG-013`: the `kind` enum is closed; an unknown kind is 422."""

    status = 422
    code = "UNKNOWN_ARTIFACT_KIND"
    problem_class = "client_error"


class VersionConflict(RegistryError):
    """`MOS-REG-019`: republishing `(family, version)` with different content is 409.

    The existing `content_digest` travels in `extensions` and is quoted in the problem
    document, because "409 conflict" without the digest leaves the publisher unable to
    tell a re-tag from a genuine content change.
    """

    status = 409
    code = "ARTIFACT_VERSION_CONFLICT"
    problem_class = "client_error"


class LifecycleViolation(RegistryError):
    """`MOS-REG-021`/`MOS-REG-022`: a transition the graph does not contain."""

    status = 409
    code = "LIFECYCLE_TRANSITION_REFUSED"
    problem_class = "client_error"


class ImmutableArtifact(RegistryError):
    """`MOS-REG-018`: manifest, digest, version and image digest are immutable."""

    status = 405
    code = "ARTIFACT_IMMUTABLE"
    problem_class = "client_error"


class ArtifactNotFound(RegistryError):
    """404. `MOS-REG-107`: a non-entitled tenant gets 404, never 403 -- the existence of
    another tenant's private artifact is itself not disclosable."""

    status = 404
    code = "ARTIFACT_NOT_FOUND"
    problem_class = "client_error"


class UnresolvedReference(RegistryError):
    """`MOS-REG-025`/`MOS-REG-026`: a dangling `models[].ref` is rejected at PUBLISH.

    "Dangling refs MUST be rejected at publish, not at dispatch" -- a ref that resolves
    to nothing at dispatch time is a clinical rejection in front of a waiting radiologist
    instead of a build error in front of a publisher.
    """

    status = 422
    code = "UNRESOLVED_ARTIFACT_REFERENCE"
    problem_class = "client_error"


class SupplyChainRefusal(RegistryError):
    """`MOS-REG-090`: verification fails CLOSED at every enforcement point.

    Not a warning, and not bypassable by configuration. Registry admission is one of the
    two enforcement points `MOS-REG-089` names for signatures and the only one for SBOMs.
    """

    status = 422
    code = "SUPPLY_CHAIN_VERIFICATION_FAILED"
    problem_class = "client_error"
