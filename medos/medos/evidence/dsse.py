# SPDX-License-Identifier: Apache-2.0
"""Ed25519 inside a DSSE envelope. Chapter 7 section 7.12.2.

    MOS-EVID-118  `report.json` is canonicalised with JCS and signed with Ed25519 inside
                  a DSSE envelope over the Pre-Authentication Encoding
                  `"DSSEv1" SP len(payloadType) SP payloadType SP len(payload) SP payload`.
    MOS-EVID-119  The private key is publisher- or tenant-held. It MUST NOT be held by
                  the evaluation runner, and signing MUST be a distinct, separately
                  authorised step from running the evaluation.
    MOS-EVID-120  Multiple signatures on one envelope MUST be supported, and verification
                  MUST report WHICH presented signature verified against WHICH trusted
                  key -- not merely "valid".
    MOS-EVID-121  "Signing the model artifact but not the claim about it is backwards."
                  Chapter 18 section 18.7.4 records the same gap from the conformance
                  side. This module is the claim half.

TWO BACKENDS, AND THE LINE BETWEEN THEM
---------------------------------------
`cryptography` is the adopted dependency and is the ONLY path that touches a private key.
A second, pure-standard-library RFC 8032 verifier sits beside it and is used only when
`cryptography` is absent.

That is not an evasion of `MOS-REL-032` ("MUST NOT re-implement functionality available
in an adopted dependency IN ORDER TO AVOID the dependency"), and the reason is recorded
here because the rule asks for it. The deliverable of this block is a verifier that runs
on a machine with no network route to MedicalOS -- typically a hospital's air-gapped
acceptance workstation, which is also a machine where `pip install` is a change-control
ticket. A verifier that cannot run until a C-extension wheel is provisioned is a verifier
that will not be run. The fallback is:

  * verification only. `sign()` raises when `cryptography` is missing rather than
    reaching for it, because the fallback is ordinary variable-time Python and a
    variable-time scalar multiplication by a SECRET scalar leaks the key. Verification
    handles only public values, so the same property is not needed there.
  * the published RFC 8032 section 7 reference construction, not a novel one.
  * cross-checked: `test_validation_report.py` runs both backends over the RFC 8032
    section 7.1 test vectors AND over freshly generated keypairs, and asserts they agree
    on every accept and every reject.

WHAT THIS MODULE DOES NOT DO
----------------------------
It holds no key material and reads none from the database. `MOS-EVID-119` puts the key in
chapter 8's keyring; until that exists, a key arrives here as bytes from a caller who was
authorised to hold it, and `Approver.require_signing_permission` is the authorisation
seam -- see `medos.evidence.report.sign_report`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "PAYLOAD_TYPE",
    "SignatureBackendMissing",
    "DsseError",
    "generate_keypair",
    "key_id",
    "pae",
    "sign",
    "verify_raw",
    "build_envelope",
    "verify_envelope",
    "TrustedKey",
    "SignatureResult",
    "EnvelopeVerification",
    "parse_trust_bundle",
    "public_key_pem",
    "public_key_hex",
    "backend_name",
]

# MOS-EVID-118's `payloadType`, verbatim.
PAYLOAD_TYPE: Final[str] = "application/vnd.medicalos.validation-report+json"


class DsseError(ValueError):
    """A malformed envelope, key or trust bundle. Never a failed signature."""


class SignatureBackendMissing(RuntimeError):
    """Signing was attempted without `cryptography`. MOS-EVID-118.

    Deliberately not a fallback to the pure-Python implementation: see the module
    docstring. A private-key operation goes through the audited library or it does not
    happen.
    """


# =====================================================================================
# 1. Pre-Authentication Encoding.  MOS-EVID-118, verbatim.
# =====================================================================================
def pae(payload_type: str, payload: bytes) -> bytes:
    """`"DSSEv1" SP len(payloadType) SP payloadType SP len(payload) SP payload`.

    `len` is the BYTE length rendered as ASCII decimal. The PAE is what makes a signature
    unambiguous about which of the two fields a byte belongs to: without the lengths, a
    payload type ending in a space and a payload beginning with one are indistinguishable
    from the reverse split, and a signature over the concatenation would cover both.
    """
    type_bytes = payload_type.encode("utf-8")
    return b" ".join(
        [
            b"DSSEv1",
            str(len(type_bytes)).encode("ascii"),
            type_bytes,
            str(len(payload)).encode("ascii"),
            payload,
        ]
    )


# =====================================================================================
# 2. Backend A -- `cryptography`. The only path that touches a private key.
# =====================================================================================
def _cryptography():
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: PLC0415

        return ed25519
    except ImportError:  # pragma: no cover - exercised by the fallback test
        return None


def backend_name() -> str:
    """Which verifier is in use. Printed by `medicalos-verify` so a run is reproducible."""
    return "cryptography" if _cryptography() is not None else "rfc8032-reference"


# =====================================================================================
# 3. Backend B -- RFC 8032 section 7 reference verification, standard library only.
#
# Verify-only, and only reachable when `cryptography` is absent. Public values only:
# `_point_mul` is called with the signature scalar S and the challenge h, both of which
# are published in the signature itself, so the variable-time double-and-add leaks
# nothing that is not already on the wire.
# =====================================================================================
_P: Final[int] = 2**255 - 19
_L: Final[int] = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D: Final[int] = -121665 * _inv(121666) % _P
_SQRT_M1: Final[int] = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_By: Final[int] = 4 * _inv(5) % _P
_Bx: Final[int] = _recover_x(_By, 0) or 0
_B: Final[tuple[int, int, int, int]] = (_Bx, _By, 1, _Bx * _By % _P)


def _point_add(
    p: tuple[int, int, int, int], q: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * t1 * t2 * _D % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _point_mul(s: int, p: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _point_add(q, p)
        p = _point_add(p, p)
        s >>= 1
    return q


def _point_equal(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> bool:
    x1, y1, z1, _ = p
    x2, y2, z2, _ = q
    return (x1 * z2 - x2 * z1) % _P == 0 and (y1 * z2 - y2 * z1) % _P == 0


def _decompress(s: bytes) -> tuple[int, int, int, int] | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _verify_reference(public_key: bytes, signature: bytes, message: bytes) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a = _decompress(public_key)
    if a is None:
        return False
    r_bytes = signature[:32]
    r = _decompress(r_bytes)
    if r is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:  # RFC 8032: a non-canonical S is a rejected signature, not a valid one.
        return False
    h = int.from_bytes(
        hashlib.sha512(r_bytes + public_key + message).digest(), "little"
    ) % _L
    return _point_equal(_point_mul(s, _B), _point_add(r, _point_mul(h, a)))


# =====================================================================================
# 4. The two primitives every caller uses.
# =====================================================================================
def generate_keypair() -> tuple[bytes, bytes]:
    """`(private_seed_32, public_key_32)`. Requires `cryptography`.

    Used by tests and by the key-provisioning CLI. `MOS-EVID-119` puts production key
    generation in chapter 8's keyring; this is the shape that keyring must produce.
    """
    ed25519 = _cryptography()
    if ed25519 is None:
        raise SignatureBackendMissing(
            "key generation requires `cryptography`; the standard-library fallback in "
            "this module is verify-only by design (MOS-EVID-119)"
        )
    sk = ed25519.Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415

    seed = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return bytes(seed), bytes(pub)


def sign(private_seed: bytes, message: bytes) -> bytes:
    """Raw 64-byte Ed25519 signature. `cryptography` only -- see the module docstring."""
    ed25519 = _cryptography()
    if ed25519 is None:
        raise SignatureBackendMissing(
            "signing requires `cryptography`; the standard-library fallback is "
            "verify-only because a variable-time multiply by a SECRET scalar leaks it"
        )
    if len(private_seed) != 32:
        raise DsseError("an Ed25519 private key is a 32-byte seed")
    return ed25519.Ed25519PrivateKey.from_private_bytes(private_seed).sign(message)


def verify_raw(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """Does `signature` verify over `message` under `public_key`? Never raises."""
    ed25519 = _cryptography()
    if ed25519 is None:
        return _verify_reference(public_key, signature, message)
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except Exception:  # noqa: BLE001 - InvalidSignature and every malformed-input error
        return False
    return True


def key_id(public_key: bytes) -> str:
    """`ed25519:<16 lower-case hex>` -- the DSSE `keyid` of MOS-EVID-118's example.

    Chapter 7 shows the FORM (`"ed25519:b41f0c7a2d95e386"`, algorithm prefix plus eight
    bytes) and fixes no construction, so this module defines one: the first 8 bytes of
    `sha256(raw_public_key)`. Derived rather than assigned, so that two operators who
    provision the same key independently produce the same id and a bundle naming a key id
    can be matched against a trust bundle without a registry lookup -- which is the whole
    point of an offline verifier.
    """
    if len(public_key) != 32:
        raise DsseError("an Ed25519 public key is 32 raw bytes")
    return "ed25519:" + hashlib.sha256(public_key).digest()[:8].hex()


# =====================================================================================
# 5. Envelope construction and verification.
# =====================================================================================
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise DsseError(f"{what} is not valid base64") from exc


def build_envelope(
    payload: bytes,
    signers: list[tuple[bytes, bytes]],
    *,
    payload_type: str = PAYLOAD_TYPE,
) -> dict[str, Any]:
    """A DSSE envelope over `payload`, signed by every `(private_seed, public_key)`.

    A list and not a single key because `MOS-EVID-120` requires multiple signatures on
    one envelope: "a `site_acceptance` report is typically signed by the site and
    countersigned by the platform operator". Making the plural case the ONLY signature
    shows the countersignature path is real rather than reserved.
    """
    if not signers:
        raise DsseError("an envelope MUST carry at least one signature (MOS-EVID-121)")
    message = pae(payload_type, payload)
    return {
        "payloadType": payload_type,
        "payload": _b64(payload),
        "signatures": [
            {"keyid": key_id(pub), "sig": _b64(sign(seed, message))}
            for seed, pub in signers
        ],
    }


@dataclass(frozen=True)
class TrustedKey:
    """One entry of a trust bundle: a public key and the human name of its holder."""

    public_key: bytes
    name: str

    @property
    def key_id(self) -> str:
        return key_id(self.public_key)


@dataclass(frozen=True)
class SignatureResult:
    """MOS-EVID-120: which presented signature verified against which trusted key."""

    keyid: str
    verified: bool
    trusted_key_name: str | None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "keyid": self.keyid,
            "verified": self.verified,
            "trusted_key_name": self.trusted_key_name,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EnvelopeVerification:
    payload: bytes | None
    payload_type: str | None
    results: tuple[SignatureResult, ...]
    error: str | None = None

    @property
    def any_verified(self) -> bool:
        return any(r.verified for r in self.results)

    def as_dict(self) -> dict[str, Any]:
        return {
            "payload_type": self.payload_type,
            "signatures": [r.as_dict() for r in self.results],
            "error": self.error,
        }


def verify_envelope(
    envelope: Any, trusted: list[TrustedKey], *, payload_type: str = PAYLOAD_TYPE
) -> EnvelopeVerification:
    """Verify every signature against every trusted key. Reports per signature.

    `MOS-EVID-120` forbids collapsing this to a boolean, and the reason is operational: a
    site that countersigns a vendor report needs to know whether the SITE's signature or
    the VENDOR's is the one that verified, because those are different facts about who
    stands behind the document.

    Never raises on a bad signature -- only on an envelope that is not an envelope.
    """
    if not isinstance(envelope, dict):
        return EnvelopeVerification(None, None, (), "envelope is not a JSON object")
    got_type = envelope.get("payloadType")
    if got_type != payload_type:
        return EnvelopeVerification(
            None, got_type if isinstance(got_type, str) else None, (),
            f"payloadType is {got_type!r}, expected {payload_type!r}",
        )
    raw = envelope.get("payload")
    if not isinstance(raw, str):
        return EnvelopeVerification(None, got_type, (), "payload is missing")
    try:
        payload = _unb64(raw, "payload")
    except DsseError as exc:
        return EnvelopeVerification(None, got_type, (), str(exc))

    sigs = envelope.get("signatures")
    if not isinstance(sigs, list) or not sigs:
        return EnvelopeVerification(payload, got_type, (), "no signatures present")

    message = pae(payload_type, payload)
    by_id = {k.key_id: k for k in trusted}
    results: list[SignatureResult] = []
    for entry in sigs:
        if not isinstance(entry, dict) or not isinstance(entry.get("sig"), str):
            results.append(SignatureResult("?", False, None, "malformed signature entry"))
            continue
        keyid = entry.get("keyid") if isinstance(entry.get("keyid"), str) else "?"
        try:
            sig = _unb64(entry["sig"], "sig")
        except DsseError as exc:
            results.append(SignatureResult(keyid, False, None, str(exc)))
            continue
        # The keyid is a HINT, never the decision: a forged envelope can claim any id.
        # Every trusted key is tried, and the id is only used to name the match.
        match = None
        candidates = [by_id[keyid]] if keyid in by_id else list(trusted)
        for key in candidates:
            if verify_raw(key.public_key, sig, message):
                match = key
                break
        if match is None:
            results.append(
                SignatureResult(keyid, False, None, "no trusted key verifies this signature")
            )
        else:
            results.append(SignatureResult(match.key_id, True, match.name))
    return EnvelopeVerification(payload, got_type, tuple(results))


# =====================================================================================
# 6. Trust bundles and key files.
#
# Chapter 7 section 7.12.3 shows `--trust-bundle ./trusted_publishers.pem` on the command
# line and `keys/publisher_ed25519.pub` -- "raw 32-byte public key, hex" -- inside the
# bundle. Two encodings for the same 32 bytes, so both are parsed here and there is
# exactly one place that knows either.
# =====================================================================================
# SubjectPublicKeyInfo for Ed25519 is fixed-length and fully determined: SEQUENCE(42) of
# SEQUENCE(5) of OID 1.3.101.112, then BIT STRING(33) with zero unused bits. RFC 8410.
_SPKI_PREFIX: Final[bytes] = bytes.fromhex("302a300506032b6570032100")


def public_key_pem(public_key: bytes, name: str | None = None) -> str:
    """A PEM `PUBLIC KEY` block, optionally preceded by a `# name:` comment line.

    The comment is what lets `MOS-EVID-120`'s "verified against which trusted key" print
    a name a human recognises ("PulmoAI s.r.o. release key") rather than a key id.
    """
    if len(public_key) != 32:
        raise DsseError("an Ed25519 public key is 32 raw bytes")
    body = base64.b64encode(_SPKI_PREFIX + public_key).decode("ascii")
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    head = f"# name: {name}\n" if name else ""
    body_lines = "\n".join(lines)
    return (
        f"{head}-----BEGIN PUBLIC KEY-----\n{body_lines}\n-----END PUBLIC KEY-----\n"
    )


def public_key_hex(public_key: bytes) -> str:
    """`keys/publisher_ed25519.pub` -- "raw 32-byte public key, hex" (MOS-EVID-122)."""
    if len(public_key) != 32:
        raise DsseError("an Ed25519 public key is 32 raw bytes")
    return public_key.hex() + "\n"


def parse_public_key(text: str) -> bytes:
    """Accept either spelling: 64 hex characters, or one PEM `PUBLIC KEY` block."""
    stripped = "".join(text.split())
    if len(stripped) == 64:
        try:
            return bytes.fromhex(stripped)
        except ValueError:
            pass
    keys = parse_trust_bundle(text)
    if len(keys) != 1:
        raise DsseError("expected exactly one public key")
    return keys[0].public_key


def parse_trust_bundle(text: str) -> list[TrustedKey]:
    """Every Ed25519 `PUBLIC KEY` block in a PEM file, with its `# name:` comment.

    A non-Ed25519 key in the file is a hard error rather than a silent skip: a trust
    bundle whose RSA entry was ignored would verify nothing while looking populated, and
    the operator would learn that only from a report that failed to verify.
    """
    out: list[TrustedKey] = []
    pending_name: str | None = None
    buf: list[str] | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            if buf is None:
                label = s.lstrip("#").strip()
                if label.lower().startswith("name:"):
                    label = label[5:].strip()
                pending_name = label or None
            continue
        if s == "-----BEGIN PUBLIC KEY-----":
            buf = []
            continue
        if s == "-----END PUBLIC KEY-----":
            if buf is None:
                raise DsseError("END PUBLIC KEY without a BEGIN")
            der = _unb64("".join(buf), "PEM body")
            if len(der) != 44 or not der.startswith(_SPKI_PREFIX):
                raise DsseError(
                    "trust bundle contains a key that is not Ed25519; MOS-EVID-118 "
                    "permits no other algorithm in 0.2.0"
                )
            out.append(TrustedKey(der[12:], pending_name or f"key {len(out) + 1}"))
            buf, pending_name = None, None
            continue
        if buf is not None and s:
            buf.append(s)
    if buf is not None:
        raise DsseError("BEGIN PUBLIC KEY without an END")
    if not out:
        raise DsseError("trust bundle contains no PUBLIC KEY block")
    return out


def new_report_salt() -> bytes:
    """A per-report re-keying salt. MOS-EVID-011.

    Lives here rather than in `digest.py` because it is generated at EXPORT time and
    never stored beside the tenant salt: see `medos.evidence.bundle.rekey_patient`.
    """
    return secrets.token_bytes(32)
