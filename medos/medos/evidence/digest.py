# SPDX-License-Identifier: Apache-2.0
"""Content addressing and the tenant-scoped surrogate keys. Chapter 7 section 7.2.

Requirements implemented here
-----------------------------
MOS-EVID-007  every digest is SHA-256, serialised `"sha256:" + lowercase_hex`. No other
              algorithm is permitted in 0.2.0, and the prefix is what makes a second one
              introducible later without re-digesting the world.
MOS-EVID-008  everything digested or signed is canonicalised with RFC 8785 (JCS) first,
              and non-finite numbers are rejected at WRITE time, not at digest time.
MOS-EVID-009  the manifest is JSONL -- one JCS-canonical object per line, LF-terminated,
              UTF-8, no trailing blank line -- and `manifest_digest()` below is the one
              function that computes its digest.
MOS-EVID-010  `patient_key`, the evidence plane's patient identity and the unit of every
              split and every bootstrap resample.
MOS-EVID-035  `accession_number_hash`. The raw accession number is never stored.
MOS-TRAIN-089 `institution_key`, "by the same construction `MOS-EVID-035` uses".

ONE CANONICALISER, NOT TWO
--------------------------
`MOS-EVID-009` prints a reference `canonical_json` that calls `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=False, allow_nan=False)` and describes it as JCS.
`medos.sdk.canonical.canonical_bytes` already exists, is used by the audit chain
(`MOS-SEC-151`) and the provenance record (`MOS-SAFE-083`), and is the one this module
uses -- MOS-REL-032 forbids re-implementing an available dependency to avoid it, and two
canonicalisers in one repository is how two digests of the same object appear.

THEY ARE NOT IDENTICAL, AND THE DIFFERENCE IS REPORTED RATHER THAN PAPERED OVER:
`json.dumps` renders an integral float `3.0` as `3.0`; RFC 8785 requires ECMAScript
`Number::toString`, which renders it `3`. `canonical_bytes` implements the RFC.
Chapter 7's snippet does not, so a reader who re-digests a manifest with the chapter's
own code gets a different digest for any line carrying an integral float -- and
`acquisition.kvp` is exactly that (`120.0`). This module follows the RFC that
`MOS-EVID-008` names, not the snippet that `MOS-EVID-009` prints.

WHY THE KEYS ARE HMACs AND NOT DIGESTS
--------------------------------------
`patient_key` is derived from `IssuerOfPatientID|PatientID`, which an attacker holding a
patient list can enumerate. A plain SHA-256 of a guessable input is reversible by
enumeration, so `MOS-EVID-010` specifies HMAC under a per-tenant secret. That is what
makes `MOS-EVID-011`'s claim true -- "MUST NOT be reversible without the tenant salt" --
and what makes the key comparable across every DatasetVersion in one tenant and
meaningless outside it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Sequence
from typing import Any

from medos.sdk.canonical import canonical_bytes, new_ulid

__all__ = [
    "canonical_line",
    "manifest_digest",
    "manifest_bytes",
    "sha256_of",
    "patient_key",
    "accession_number_hash",
    "institution_key",
    "new_dataset_id",
    "new_dataset_version_id",
    "new_split_id",
    "new_annotation_set_id",
    "new_evaluation_run_id",
]


# =====================================================================================
# Digests
# =====================================================================================
def canonical_line(obj: Any) -> bytes:
    """One JCS-canonical manifest line, WITHOUT its terminator. MOS-EVID-008."""
    return canonical_bytes(obj)


def manifest_bytes(lines: Sequence[dict[str, Any]]) -> bytes:
    """The manifest object exactly as it is written to the object store. MOS-EVID-009.

    "A manifest is a JSONL file: one JCS-canonical JSON object per line, LF-terminated,
    UTF-8, no trailing blank line."

    LF-terminated means every line including the last carries its `\\n`, which is not the
    same as "no trailing blank line" being a missing final newline -- a blank line is two
    consecutive LFs. Written this way the byte stream and the digest agree by
    construction, because `manifest_digest()` below hashes `line + b"\\n"` per line.
    """
    return b"".join(canonical_line(line) + b"\n" for line in lines)


def manifest_digest(lines: Sequence[dict[str, Any]]) -> str:
    """`sha256:<hex>` over the JSONL manifest. MOS-EVID-009, MOS-EVID-007.

    `lines` MUST already be in the canonical sort order declared for the manifest kind
    (`MOS-EVID-017` for a DatasetVersion, `MOS-EVID-030` for a split, `MOS-EVID-040` for
    an AnnotationSet). This function does NOT sort: sorting here would silently repair a
    caller that built the lines in the wrong order, and the whole point of a content
    address is that two callers who disagree about order produce two different digests
    and find out.
    """
    h = hashlib.sha256()
    for line in lines:
        h.update(canonical_line(line))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()


def sha256_of(data: bytes) -> str:
    """`sha256:<hex>` over raw bytes. MOS-EVID-007's serialisation, one spelling."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


# =====================================================================================
# Tenant-scoped surrogates
# =====================================================================================
def _b32_10(mac: bytes) -> str:
    """The shared tail of MOS-EVID-010, MOS-EVID-035 and MOS-TRAIN-089.

    Ten bytes is eighty bits, which is exactly sixteen base32 characters, so the
    `rstrip("=")` in the chapter's snippet removes nothing -- it is there because the
    construction was written against a variable truncation. Kept for byte-equality with
    the published code path; the result is lower-cased so the three keys share one
    grammar (`^[a-z2-7]{16}$`) and one CHECK constraint in `0006_evidence.up.sql`.
    """
    return base64.b32encode(mac[:10]).decode("ascii").rstrip("=").lower()


def patient_key(tenant_salt: bytes, issuer: str, patient_id: str) -> str:
    """`pk_` + HMAC-SHA256(tenant_salt, "issuer|patient_id")[:10], base32. MOS-EVID-010.

    `issuer` is `IssuerOfPatientID` (0010,0021) when present, otherwise the `source_id`
    of the `Dataset` (`datasets.custodian`) -- for example `TCIA/LIDC-IDRI`. Passing an
    empty issuer is refused rather than defaulted: an empty issuer silently merges the
    patient spaces of two collections, which is a leakage defect that L1 cannot see
    because it produces ONE key for two people rather than two keys for one.

    `tenant_salt` is a per-tenant secret from the tenant keyring (chapter 8), stable for
    the life of the tenant. It is never logged and never leaves the process.
    """
    if not issuer:
        raise ValueError(
            "issuer MUST be IssuerOfPatientID or the Dataset source_id, never empty "
            "(MOS-EVID-010)"
        )
    if not patient_id:
        raise ValueError("patient_id MUST NOT be empty (MOS-EVID-010)")
    if not tenant_salt:
        raise ValueError("tenant_salt MUST be a per-tenant secret (MOS-EVID-010)")
    msg = f"{issuer}|{patient_id}".encode()
    mac = hmac.new(tenant_salt, msg, hashlib.sha256).digest()
    return "pk_" + _b32_10(mac)


def accession_number_hash(tenant_salt: bytes, accession_number: str) -> str:
    """HMAC-SHA256(tenant_salt, AccessionNumber)[:10], base32. MOS-EVID-035.

    "The raw accession number MUST NOT be stored in the split manifest." It is not stored
    anywhere by this package: `dataset_cases.accession_number_hash` holds this value and
    the column has no plaintext sibling.

    No prefix, because `MOS-EVID-035` specifies none. Leakage check L5 compares these
    across partitions to catch one study ingested twice through different routes.
    """
    if not accession_number:
        raise ValueError("accession number MUST NOT be empty (MOS-EVID-035)")
    if not tenant_salt:
        raise ValueError("tenant_salt MUST be a per-tenant secret (MOS-EVID-035)")
    mac = hmac.new(tenant_salt, accession_number.encode("utf-8"), hashlib.sha256).digest()
    return _b32_10(mac)


def institution_key(tenant_salt: bytes, institution_name: str) -> str:
    """HMAC-SHA256(tenant_salt, InstitutionName)[:10], base32. MOS-TRAIN-089.

    "by the same construction `MOS-EVID-035` uses for accession numbers. The raw
    institution name MUST NOT be stored on a candidate, in a split manifest or in a
    report." It is the input to corpus stratification checks C1 (site concentration) and
    C7 (single-site declaration), which are the two that decide whether a cohort can
    support a `vendor_evidence` claim at all.
    """
    if not institution_name:
        raise ValueError("institution name MUST NOT be empty (MOS-TRAIN-089)")
    if not tenant_salt:
        raise ValueError("tenant_salt MUST be a per-tenant secret (MOS-TRAIN-089)")
    mac = hmac.new(tenant_salt, institution_name.encode("utf-8"), hashlib.sha256).digest()
    return _b32_10(mac)


# =====================================================================================
# Identities.  Chapter 7 section 7.2's `<prefix>_<ULID>` forms.
# =====================================================================================
# `new_ulid` is `medos.sdk.canonical`'s -- the same encoder behind `job_`,
# `aud_` and `prv_`. Chapter 7 section 7.2 fixes the four prefixes below; MOS-STORE-357 fixes
# where the value lives (a `public_id` column beside the internal uuid).
def new_dataset_id() -> str:
    """`ds_<ULID>`. Chapter 7 section 7.2."""
    return new_ulid("ds")


def new_dataset_version_id() -> str:
    """`dsv_<ULID>`. Chapter 7 section 7.2."""
    return new_ulid("dsv")


def new_split_id() -> str:
    """`spl_<ULID>`. Chapter 7 section 7.2."""
    return new_ulid("spl")


def new_annotation_set_id() -> str:
    """`ann_<ULID>`. Chapter 7 section 7.2."""
    return new_ulid("ann")


def new_evaluation_run_id() -> str:
    """`evr_<ULID>`. Chapter 7 section 7.2, the fifth prefix of the same table."""
    return new_ulid("evr")
