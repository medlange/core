# SPDX-License-Identifier: Apache-2.0
"""The Artifact registry. Chapter 6 sections 6.3, 6.4, 6.5, 6.9; chapter 12 section 12.9.

`docs/spec/15-delivery.md` section 15.2.6 fixes the shape of this package in one sentence:

    One `artifacts` table with per-kind JSON-Schema'd manifests served on the existing
    API paths.

So there is ONE table (`artifacts`, migration `0012_artifacts.up.sql`) with a `kind`
discriminator (`MOS-REG-013`, `MOS-STORE-253`), one schema per kind held in
`artifact_manifest_schemas`, and no parallel `/api/v1/artifacts` surface: publishing and
reading happen on chapter 10 table 10.2-B's existing `/service-versions` and
`/model-versions` paths (`medos/medos/api/routes_registry.py`).

Modules
-------
`schemas.py`      the SINGLE in-repo schema source (`MOS-REG-016`). The envelope and one
                  spec schema per kind, the family-level/version-level kind mapping, and
                  the generator that seeds `artifact_manifest_schemas`.
`jsonschema.py`   a strict JSON Schema 2020-12 subset validator with a CLOSED keyword
                  set, so a schema keyword this platform does not implement is a build
                  error rather than a silently ignored constraint.
`digest.py`       `MOS-REG-017`'s content digest: SHA-256 over the RFC 8785
                  canonicalisation of the manifest with `content_digest` removed.
`lifecycle.py`    `MOS-REG-020`/`MOS-REG-021`/`MOS-REG-022`: the per-kind status sets, the
                  transition graph and the permission each transition requires.
`repo.py`         publish, read, list and the status transition, against the one table.
`errors.py`       the refusals, each carrying the HTTP status and problem code chapter 6
                  names for it.

What this package deliberately does NOT own: capability resolution (`Resolve()`, section
6.7) and `Deployment` (section 6.8). Both are separate components of this release, both
read this registry, and neither is imported here.
"""

from __future__ import annotations

from medos.registry.digest import content_digest_of, manifest_without_digest
from medos.registry.errors import (
    ArtifactNotFound,
    ImmutableArtifact,
    InvalidManifest,
    RegistryError,
    UnknownKind,
    VersionConflict,
)
from medos.registry.lifecycle import (
    FAMILY_KIND,
    LIFECYCLE_STATUSES,
    TRANSITION_PERMISSION,
    VERSION_KIND,
    permission_for,
    transition_allowed,
)
from medos.registry.schemas import ENVELOPE_SCHEMA, SCHEMAS, schema_for_kind, schema_rows

__all__ = [
    "ENVELOPE_SCHEMA",
    "FAMILY_KIND",
    "LIFECYCLE_STATUSES",
    "SCHEMAS",
    "TRANSITION_PERMISSION",
    "VERSION_KIND",
    "ArtifactNotFound",
    "ImmutableArtifact",
    "InvalidManifest",
    "RegistryError",
    "UnknownKind",
    "VersionConflict",
    "content_digest_of",
    "manifest_without_digest",
    "permission_for",
    "schema_for_kind",
    "schema_rows",
    "transition_allowed",
]
