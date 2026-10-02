# SPDX-License-Identifier: Apache-2.0
"""RFC 8785 canonical JSON, the digests built on it, and the P1 pseudonyms.

Two chapters require the same primitive and neither may get its own copy of it:

  * `MOS-SEC-151` -- an audit row's `hash` is "SHA-256 over the RFC 8785 canonical JSON
    of the row with `hash` omitted and `prev_hash` included";
  * `MOS-SAFE-090` -- a provenance record's `record_hash` is
    "sha256(canonical_json(record without record_hash))";

and `MOS-SAFE-021` digests the `clinical` block with the same function. A second
canonicaliser would make two hash chains that disagree about the same bytes, which is
the failure that makes a chain worthless: the verifier and the writer must serialise
identically or every row looks tampered.

Also here because it is the same "derive a stable string from a value" concern and
because it MUST NOT be reinvented per call site: `study_ref`, the P1 pseudonym
`MOS-SEC-106` requires wherever the P2/P3 matrix (`MOS-SEC-105`) forbids a raw
StudyInstanceUID -- the audit `resource` member being the case this block has.

Spec: MOS-SEC-151, MOS-SEC-105, MOS-SEC-106, MOS-SAFE-021, MOS-SAFE-090.
Pure. No I/O, no database, no clock (CONTRACT.md section 1: `medos/medos/core` is pure).
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import time
from typing import Any

__all__ = [
    "canonical_json",
    "canonical_bytes",
    "sha256_hex",
    "digest_of",
    "study_ref",
    "new_ulid",
    "ULID_ALPHABET",
]

ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U


def _canonicalise(value: Any) -> Any:
    """Recursively put a value into the shape `json.dumps` can render canonically.

    RFC 8785 (JCS) is three rules: object members sorted by key, no insignificant
    whitespace, and ECMAScript `Number::toString` for numbers. Python's
    `json.dumps(sort_keys=True, separators=(",", ":"))` gives the first two exactly.

    The third is where a naive implementation drifts, so it is handled here rather than
    left to `json`:

      * a `float` that is integral (`3.0`) MUST serialise as `3`, because ECMAScript has
        one number type and prints it that way. Python prints `3.0`, and a record whose
        `duration_ms` arrived as an int on one attempt and a float on the next would hash
        differently while describing the same run.
      * `NaN` and `±Infinity` are not JSON at all; they are refused rather than emitted
        as the bare words Python's `json` would happily write and no other parser reads.
      * a `Decimal`, a `datetime` or a `UUID` is refused. A hash input is a wire value,
        not an object graph: callers convert to `str`/`float` at the boundary where they
        know the intended precision, because `str(Decimal("1.10"))` and `float(...)`
        disagree and the hash must be over the one the reader will see.
    """
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN and Infinity are not representable in canonical JSON")
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"canonical JSON object keys MUST be strings, got {type(k)}")
            out[k] = _canonicalise(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonicalise(v) for v in value]
    raise TypeError(
        f"{type(value).__name__} has no canonical JSON form; convert it at the boundary "
        "that knows the intended precision (MOS-SEC-151)"
    )


def canonical_json(value: Any) -> str:
    """The RFC 8785 serialisation of `value`.

    Key ordering is Python's, i.e. by Unicode code point. JCS specifies UTF-16 code
    units, and the two orders differ only when a key contains a character above the BMP.
    Every key in every record this platform hashes is drawn from a fixed, ASCII,
    lower-snake-case field set declared in chapter 8 (`MOS-SEC-146`) and chapter 9
    (`MOS-SAFE-083`), so the orders coincide. `_reject_astral_keys` makes that an
    enforced precondition instead of a hopeful comment.
    """
    prepared = _canonicalise(value)
    _reject_astral_keys(prepared)
    return json.dumps(prepared, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _reject_astral_keys(value: Any) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if any(ord(ch) > 0xFFFF for ch in k):
                raise ValueError(
                    f"key {k!r} contains a non-BMP character; code-point and UTF-16 "
                    "ordering diverge there and this canonicaliser sorts by code point"
                )
            _reject_astral_keys(v)
    elif isinstance(value, list):
        for v in value:
            _reject_astral_keys(v)


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_of(value: Any) -> str:
    """`sha256(canonical_json(value))` as lowercase hex. The one spelling of that."""
    return sha256_hex(canonical_bytes(value))


def study_ref(tenant_id: str, study_instance_uid: str) -> str:
    """The P1 pseudonym for a study, per `MOS-SEC-106`.

    `MOS-SEC-105` classifies a StudyInstanceUID as P2 and `MOS-SEC-106` requires it to be
    replaced by `study_ref` "on every surface where the matrix forbids P2", with
    resolution back to the UID gated on `study.read` and audited. The audit `resource`
    member is such a surface (`MOS-SEC-146`: "never a raw UID for a P3 subject", and the
    permission class of the surrounding action is `phi`).

    Tenant-scoped by construction: the same study seen by two tenants gets two refs, so a
    ref leaked from one tenant's export says nothing about another's holdings. It is a
    plain digest and not an HMAC in this block because there is no key management to hang
    a per-tenant secret on yet -- REPORTED as a limitation: a digest over a guessable UID
    is reversible by an attacker who already holds the UID list, which makes `study_ref`
    an unlinkability control between tenants and NOT a confidentiality control.
    """
    material = f"medicalos/study_ref/v1\x1f{tenant_id}\x1f{study_instance_uid}".encode()
    raw = hashlib.sha256(material).digest()
    n = int.from_bytes(raw[:16], "big")
    chars = [ULID_ALPHABET[(n >> (5 * (25 - i))) & 0x1F] for i in range(26)]
    return "sr_" + "".join(chars)


def new_ulid(prefix: str, *, now_ms: int | None = None) -> str:
    """`<prefix>_` + a 26-character Crockford base32 ULID, upper case.

    The same construction `medos.db.repo.new_public_job_id` uses for `job_`; lifted here
    because chapter 8 gives `audit_id` the prefix `aud_` (`MOS-SEC-146`) and chapter 9
    gives `provenance_id` the prefix `prv_` (`MOS-SAFE-083`), and three copies of a ULID
    encoder is how the three drift.
    """
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    n = (ts << 80) | secrets.randbits(80)
    chars = [ULID_ALPHABET[(n >> (5 * (25 - i))) & 0x1F] for i in range(26)]
    return f"{prefix}_" + "".join(chars)
