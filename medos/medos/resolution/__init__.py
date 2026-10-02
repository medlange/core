# SPDX-License-Identifier: Apache-2.0
"""Capability resolution. Chapter 6 §6.7 (`MOS-REG-051`..`MOS-REG-071`), §15.2.6.

    resolve(capability, {modality, tenant_id, environment, study_constraints},
            registry_snapshot) -> Outcome        # Outcome.ranked is the ordered list

`docs/spec/15-delivery.md` §15.2.6 fixes this component in one sentence: "Capability
resolution as a pure function with the written precedence order of spine section 9, pinned
into the `Job` row at creation and reused on every retry."

Modules
-------
`resolve.py`    the pure function: `F1`..`F9`, `P1`..`P7`, `ZERO_CANDIDATES`. Imports no
                database, no clock, no randomness. `resolve.py`'s docstring records the
                three interpretations taken where §6.7 admits two readings.
`model.py`      `Request`, `Snapshot`, `Candidate`, `Exclusion`, `RankKey`, `Outcome` --
                `MOS-REG-051`'s structs, frozen, with the canonical-JSON projection that
                `MOS-REG-053`'s byte-identity property is asserted over.
`versions.py`   `MOS-REG-062`'s closed range grammar. A bare version is EXACT, `||` and
                `*` are refused, and a prerelease satisfies nothing that does not name it.
`bucket.py`     `MOS-REG-059`'s CRC-32C study bucket, so canary traffic is study-sticky
                and a retry cannot cross the canary boundary.
`snapshot.py`   the IMPURE half: one consistent, content-addressed read of the registry.
`pin.py`        §6.7.5's record, written in the job-creation transaction.
`retry.py`      the retry path. Reads the pin; has no `Snapshot` in scope, which is what
                makes `MOS-REG-067`'s "MUST be impossible by construction" structural.

WHY `snapshot`, `pin` AND `retry` ARE LAZY ATTRIBUTES AND NOT EAGER IMPORTS
----------------------------------------------------------------------------
They import `psycopg`. If this `__init__` imported them eagerly, then
`import medos.resolution.resolve` would load a database driver -- and chapter 6's
acceptance check 3 ("a CI import check proves the `resolve` package imports none of
`net/http`, `database/sql`, `os`, `math/rand`") would be false of a package that never
touches a database at run time. `medos/medos/core/__init__.py` uses PEP 562 for the same reason;
this follows it. `medos/medos/safety/__init__.py` is the counter-example that motivated the
choice: importing its pure envelope evaluator pulls `requests` in through the package.

SPEC DEVIATIONS, REPORTED HERE AND NOT SILENTLY RESOLVED
---------------------------------------------------------
D1. `MOS-REG-051` says "There MUST be exactly one implementation, **in Go**, in the
    control plane." This platform is Python (CONTRACT.md §0; the Go control plane of
    `MOS-REL-084` was never built, and every chapter's code is Python). The requirement's
    force is "exactly one implementation" and that is honoured: this is the only one, and
    `MOS-REG-110`'s dry-run endpoint is required to call it rather than mirror it. The
    language clause is a spec/build divergence that predates this component.
D2. `MOS-REG-066`'s record has no home in the `jobs` table in this build: chapter 12
    §12.10's `resolved_service_version_id`, `resolution_snapshot`, `deployment_id` and
    `target_kind` columns were never created, so `MOS-REG-004` has been unsatisfied since
    0.1.0. See `pin.py`'s docstring for what was done instead and for the migration that
    must close it.
D3. `MOS-REG-058` P4 names `AcceptanceCriteria.primary_metric`; chapter 7's
    `acceptance_criteria.spec` has no such member. See `snapshot.py`.
"""

from __future__ import annotations

import importlib
from typing import Any

from medos.resolution.bucket import BUCKETS, canary_bucket, crc32c, deployment_rank
from medos.resolution.model import (
    Candidate,
    Capability,
    DeploymentRow,
    Exclusion,
    GateConfig,
    LicenceGrant,
    MetricPoint,
    ModelVersionRow,
    NodeProfile,
    Outcome,
    PreprocRow,
    RankKey,
    Request,
    ServiceVersionRow,
    Snapshot,
    StudyProfile,
    TenantPin,
    TenantProfile,
    ValidationReportRow,
    VersionPin,
)
from medos.resolution.resolve import (
    RESOLVER_VERSION,
    SELECTED,
    ZERO_CANDIDATES,
    build_request,
    resolve,
    resolve_request,
)
from medos.resolution.versions import (
    Range,
    RangeSyntaxError,
    Version,
    compare_versions,
    parse_range,
    parse_version,
    validate_range,
)

__all__ = [
    "BUCKETS",
    "RESOLVER_VERSION",
    "SELECTED",
    "ZERO_CANDIDATES",
    "Candidate",
    "Capability",
    "DeploymentRow",
    "EnvelopeAdapter",
    "Exclusion",
    "GateConfig",
    "LicenceGrant",
    "MetricPoint",
    "ModelVersionRow",
    "NodeProfile",
    "Outcome",
    "PinMissing",
    "PinnedSelection",
    "PreprocRow",
    "Range",
    "RangeSyntaxError",
    "RankKey",
    "Request",
    "ServiceVersionRow",
    "Snapshot",
    "StudyProfile",
    "TenantPin",
    "TenantProfile",
    "ValidationReportRow",
    "Version",
    "VersionPin",
    "build_request",
    "canary_bucket",
    "compare_versions",
    "crc32c",
    "deployment_rank",
    "load_snapshot",
    "parse_range",
    "parse_version",
    "pinned_resolution",
    "pinned_selection",
    "pinned_version_status",
    "resolution_record",
    "resolve",
    "resolve_request",
    "retry_refusal",
    "validate_range",
    "write_pin",
]

# PEP 562: the database-touching modules load on first use, never on `import
# medos.resolution.resolve`.
_LAZY: dict[str, str] = {
    "EnvelopeAdapter": "snapshot",
    "load_snapshot": "snapshot",
    "registry_epoch": "snapshot",
    "resolution_record": "pin",
    "write_pin": "pin",
    "PinMissing": "retry",
    "PinnedSelection": "retry",
    "pinned_resolution": "retry",
    "pinned_selection": "retry",
    "pinned_version_status": "retry",
    "retry_refusal": "retry",
}


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"medos.resolution.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
