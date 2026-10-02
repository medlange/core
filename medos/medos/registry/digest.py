# SPDX-License-Identifier: Apache-2.0
"""`MOS-REG-017`: the content digest of a manifest, and the one way to compute it.

    "`content_digest` MUST be computed by the registry over the RFC 8785
    canonicalisation of the manifest *excluding* the `content_digest` field itself, and
    MUST NOT be accepted from the publisher. A publisher-supplied value MUST be compared
    and a mismatch rejected."

Both halves are here: `content_digest_of` computes, `reconcile_supplied_digest` compares.
The canonicaliser is `medos.sdk.canonical`, which already serves the audit chain
(`MOS-SEC-151`) and the provenance chain (`MOS-SAFE-090`) -- a second RFC 8785
implementation would give the registry and the audit trail two opinions about the same
bytes, which is the failure that makes a hash chain worthless.

Chapter 12 calls this column `manifest_digest` and chapter 6 calls it `content_digest`.
They are the same value; `medos/medos/registry/repo.py` writes the column and speaks chapter 6's
name on the wire, and the divergence is recorded in this component's report rather than
resolved by inventing a third name.

Spec: MOS-REG-017, MOS-REG-019, MOS-STORE-254. Pure: no I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from medos.sdk.canonical import canonical_bytes, sha256_hex

__all__ = ["content_digest_of", "manifest_without_digest", "reconcile_supplied_digest"]


def manifest_without_digest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest with `content_digest` removed. The input to the digest."""
    return {k: v for k, v in manifest.items() if k != "content_digest"}


def content_digest_of(manifest: Mapping[str, Any]) -> str:
    """`sha256:<hex>` over the RFC 8785 canonicalisation of the manifest sans digest."""
    return "sha256:" + sha256_hex(canonical_bytes(manifest_without_digest(manifest)))


def reconcile_supplied_digest(manifest: Mapping[str, Any]) -> tuple[str, str | None]:
    """`(computed, supplied_or_None)`. The caller decides what a mismatch costs.

    Returning rather than raising, because the two callers want different things: the
    publish path refuses a mismatch with HTTP 422, and an offline verifier wants to report
    both values side by side.
    """
    supplied = manifest.get("content_digest")
    return content_digest_of(manifest), (str(supplied) if supplied is not None else None)
