# SPDX-License-Identifier: Apache-2.0
"""Pure core: geometry, DICOM reading, identity, masks, measurements, the bundle.

CONTRACT.md §1: "pure, no I/O to DB or HTTP". Reading source DICOM files is permitted
(MOS-IMG-096); reaching a database, a PACS or an HTTP endpoint is not — that belongs to
`medos.dicomweb`, `medos.db` and `medos.api`.

Import order, bottom to top, and there are no cycles:

    errors  ->  dicomio  ->  geometry  ->  masks
                                       ->  measure  ->  bundle
    concepts (standalone)                  uids     (standalone)
    canonical (standalone)

Everything here was lifted from `spikes/week0/` per CONTRACT.md §2 — a move and a
deduplication, not a rewrite. The two known duplicate definitions are resolved:
`derive_uid` exists once, in `medos.core.uids`; `SourceGeometry` exists once, in
`medos.core.geometry`. Each of those modules' docstrings records which behaviour was kept
and why.

WHY THE RE-EXPORTS ARE LAZY (PEP 562)
-------------------------------------
This file used to import every submodule eagerly. That made
`import medos.sdk.canonical` — a module whose entire content is `json.dumps`
with sorted keys — pull in `geometry`, which imports `pydicom`, which imports
`pydicom.data`, which imports `urllib.request`.

It was found by the offline-verification test for chapter 7 §7.12.3, which asserts that
nothing in `medicalos-verify`'s import closure can open a socket (`MOS-EVID-124`). The
verifier needs one function from `medos.sdk.canonical` (`MOS-EVID-008`, and
MOS-REL-032's one-canonicaliser rule says it must be THAT function and not a copy), and it
was getting the whole DICOM stack and an HTTP client with it.

So the names below are resolved on first attribute access instead of at import time. Every
existing spelling keeps working — `from medos.core import SourceGeometry`,
`import medos.core; medos.core.ResultBundle`, and `import medos.core.geometry` — because
`__getattr__` imports the owning submodule on demand and caches the result in the module
globals. What changes is that importing a leaf module no longer imports its siblings.
"""

from __future__ import annotations

import importlib
from typing import Any

# name -> the submodule that defines it. One line per public export, which is the same
# information the old `from ... import ...` block carried, in the form a lazy resolver can
# use. A name added to `__all__` without a line here raises on access rather than silently
# resolving to nothing.
_OWNER: dict[str, str] = {
    # errors
    "MedosError": "errors",
    "ClinicalRejection": "errors",
    "TransportFailure": "errors",
    "SystemFailure": "errors",
    "GeometryRejection": "errors",
    "SeriesSelectionError": "errors",
    # geometry
    "CanonicalVolume": "geometry",
    "SourceGeometry": "geometry",
    "build_canonical_volume": "geometry",
    "scan_series": "geometry",
    "select_series": "geometry",
    # identity
    "derive_uid": "uids",
    "derive_idempotency_key": "uids",
    "deid_uid": "uids",
    "check_uid_length": "uids",
    "JobIdentity": "uids",
    # masks / measurements
    "mask_from_hu_threshold": "masks",
    "mask_from_rtstruct": "masks",
    "find_rtstruct": "masks",
    "measure_volume_ml": "measure",
    "laa_percent": "measure",
    # concepts and the return contract
    "ConceptDictionary": "concepts",
    "CodedConcept": "bundle",
    "Finding": "bundle",
    "LabelMap": "bundle",
    "CapabilityOutcome": "bundle",
    "ResultBundle": "bundle",
}

# Submodules reachable as attributes of the package, for callers that write
# `import medos.core` and then `medos.core.geometry`. Python populates these itself after
# an explicit `import medos.core.geometry`; this keeps the attribute form working without
# one, which the eager version supported by accident.
# `canonical` LEFT THIS PACKAGE. It is the one canonicalisation `MOS-REL-032` permits and
# both the platform and the trainer digest against it, so it moved to
# `medos.sdk` -- the package `MOS-IMG-003` requires and this repository had
# built inside `medos/medos/`. Listing it here after the move would make
# `medos.core.canonical` resolve to an ImportError raised from an attribute lookup, which
# reads like a broken checkout rather than a rename.
_SUBMODULES = frozenset(
    {
        "bundle", "concepts", "dicomio", "errors", "geometry", "masks",
        "measure", "uids",
    }
)

__all__ = [
    # errors
    "MedosError",
    "ClinicalRejection",
    "TransportFailure",
    "SystemFailure",
    "GeometryRejection",
    "SeriesSelectionError",
    # geometry
    "CanonicalVolume",
    "SourceGeometry",
    "build_canonical_volume",
    "scan_series",
    "select_series",
    # identity
    "derive_uid",
    "derive_idempotency_key",
    "deid_uid",
    "check_uid_length",
    "JobIdentity",
    # masks / measurements
    "mask_from_hu_threshold",
    "mask_from_rtstruct",
    "find_rtstruct",
    "measure_volume_ml",
    "laa_percent",
    # concepts and the return contract
    "ConceptDictionary",
    "CodedConcept",
    "Finding",
    "LabelMap",
    "CapabilityOutcome",
    "ResultBundle",
]


def __getattr__(name: str) -> Any:
    """PEP 562. Resolve a public name, or a submodule, on first access."""
    owner = _OWNER.get(name)
    if owner is not None:
        value = getattr(importlib.import_module(f"medos.core.{owner}"), name)
        globals()[name] = value  # cache: the resolver runs once per name per process
        return value
    if name in _SUBMODULES:
        module = importlib.import_module(f"medos.core.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module 'medos.core' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted({*__all__, *_SUBMODULES})
