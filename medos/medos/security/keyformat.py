# SPDX-License-Identifier: Apache-2.0
"""The API-key wire format. Chapter 8 section 8.2.2.

    mos_<env>_<key_id>_<secret>
         |      |        \\- 32 random bytes, Crockford base32, 52 chars
         |      \\- 12 chars Crockford base32, the lookup column, not secret
         \\- prod | stg | dev
    example: mos_prod_7Q2XK4M9AB0C_K7QW4M2X9A0BD1PCR8ZT3VYH6JNE5SFG2M4QXB0D

This module is pure: it mints, formats and parses. It never touches the database, never
hashes and never decides whether a key is valid, so it is unit-testable with no fixtures
and it is the same code the CLI and the authenticator both use. One definition of the
format, as CONTRACT.md section 2 requires of anything that was ever defined twice.

Requirements implemented
------------------------
MOS-SEC-011
    `key_id` is the public half and the only lookup key. `PresentedKey.key_id` is safe to
    log (MOS-SEC-136 says so explicitly: "A secret's `key_id` MAY be logged"); `.secret`
    is not, and this module never puts it in a `repr`, a `str` or an exception message.

MOS-SEC-014
    The `mos_` prefix is a secret-scanning pattern. `SECRET_SCAN_PATTERN` below is the
    regex to register with the CI scanner and with the repository's push protection, kept
    next to the code that produces the thing it matches so the two cannot drift.

Why Crockford base32 and not base64url or hex
---------------------------------------------
Chapter 8 names it, and the reason survives restating: the alphabet excludes I, L, O and
U, so a key read over a phone or copied out of a screenshot cannot become a different
valid key through a 1/l or 0/O confusion. Decoding is deliberately NOT implemented --
nothing in the platform ever needs the bytes back. The key is compared by hashing the
presented characters, so the encoding is the value.
"""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "ALPHABET",
    "ENVIRONMENTS",
    "KEY_ID_CHARS",
    "SECRET_CHARS",
    "SECRET_BITS",
    "KEY_RE",
    "KEY_ID_RE",
    "SECRET_SCAN_PATTERN",
    "MalformedKeyError",
    "PresentedKey",
    "MintedKey",
    "api_key_env",
    "mint",
    "parse",
    "format_key",
    "new_key_id",
]

# Crockford base32: 0-9 then A-Z without I, L, O, U.
ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# The same alphabet as a character class, for the regexes and for the CHECK constraint in
# medos/medos/db/migrations/0004_auth.up.sql. Kept identical to that constraint on purpose.
_CLASS: Final[str] = "[0-9A-HJKMNP-TV-Z]"

ENVIRONMENTS: Final[tuple[str, ...]] = ("prod", "stg", "dev")

KEY_ID_CHARS: Final[int] = 12          # 60 bits. Public; collision-resistant, not secret.
SECRET_CHARS: Final[int] = 52          # ceil(256 / 5) -- 32 random bytes, section 8.2.2.
SECRET_BITS: Final[int] = 256

KEY_ID_RE: Final[re.Pattern[str]] = re.compile(rf"^{_CLASS}{{{KEY_ID_CHARS}}}$")

KEY_RE: Final[re.Pattern[str]] = re.compile(
    rf"^mos_(?P<env>{'|'.join(ENVIRONMENTS)})"
    rf"_(?P<key_id>{_CLASS}{{{KEY_ID_CHARS}}})"
    rf"_(?P<secret>{_CLASS}{{{SECRET_CHARS}}})$"
)

# MOS-SEC-014. Register this with the CI secret scanner and with push protection. It is
# deliberately looser than KEY_RE -- a scanner must catch a TRUNCATED or MANGLED key in a
# log line too, because the thing it is protecting against is the key being written down
# at all, not the key being written down correctly.
SECRET_SCAN_PATTERN: Final[str] = (
    r"mos_(?:prod|stg|dev)_[0-9A-HJKMNP-TV-Z]{12}_[0-9A-HJKMNP-TV-Z]{8,}"
)


class MalformedKeyError(ValueError):
    """The presented string is not an API key.

    Raised by `parse()` only. It carries NO part of the input: a malformed credential is
    still a credential, and the most common malformation in practice is "the client sent
    the right key with a stray newline", where echoing the input into a log or a problem
    document would publish it (MOS-SEC-136).
    """


def api_key_env(env: dict[str, str] | None = None) -> str:
    """The environment this deployment mints and accepts keys for. `MEDOS_ENV`, else dev.

    Deployment CONFIGURATION, read once at the edge, exactly like
    `medos.db.tenancy.default_tenant_id()`. It is `dev` by default and NOT `prod`:
    getting this wrong in the safe direction means a production deployment that refuses
    its own keys until an operator sets `MEDOS_ENV=prod`, which is a loud five-minute
    problem. The other default is a development deployment that silently accepts
    production credentials.

    An unrecognised value is a refusal, not a fallback: `MEDOS_ENV=production` (a very
    easy thing to type) would otherwise be accepted as if it meant something.
    """
    import os

    env = dict(os.environ) if env is None else env
    value = (env.get("MEDOS_ENV") or "dev").strip()
    if value not in ENVIRONMENTS:
        raise MalformedKeyError(
            f"MEDOS_ENV={value!r} is not one of {ENVIRONMENTS} (chapter 8 section 8.2.2)"
        )
    return value


def _random_chars(n: int) -> str:
    """`n` characters drawn uniformly from the Crockford alphabet.

    Rejection-free because 32 is a power of two: `secrets.choice` over a 32-symbol
    alphabet consumes exactly 5 unbiased bits per character, so `n` characters carry
    exactly `5n` bits of entropy with no modulo bias to reason about.
    """
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def new_key_id() -> str:
    """A fresh public key id. Uniqueness is enforced by the database, not by hope."""
    return _random_chars(KEY_ID_CHARS)


@dataclass(frozen=True)
class PresentedKey:
    """A syntactically valid key as presented by a caller. Says nothing about validity.

    `secret` is excluded from `repr` and from equality. Equality on a secret is a timing
    oracle and `repr` is how a secret reaches a traceback, a `pytest` assertion diff and
    a structured log line -- three places MOS-SEC-136 forbids it to be.
    """

    env: str
    key_id: str
    secret: str = field(repr=False, compare=False)

    def __str__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return f"<PresentedKey env={self.env} key_id={self.key_id}>"

    def matches_env(self, env: str) -> bool:
        """Constant-time-ish environment check. `hmac.compare_digest` because the
        environment is short and public, and using the same primitive everywhere is
        cheaper to review than deciding case by case which comparisons may be `==`."""
        return hmac.compare_digest(self.env, env)


@dataclass(frozen=True)
class MintedKey:
    """A newly minted key: the parts to store, plus the plaintext to show ONCE.

    MOS-SEC-010: "The plaintext MUST be returned exactly once, at creation, and MUST NOT
    be recoverable." `plaintext` exists on this object for the length of one CLI call and
    is never written anywhere. The caller that persists the key persists `key_id` and the
    hash of `secret`; nothing persists `plaintext`.
    """

    env: str
    key_id: str
    secret: str = field(repr=False, compare=False)

    @property
    def plaintext(self) -> str:
        return format_key(self.env, self.key_id, self.secret)

    def __str__(self) -> str:  # pragma: no cover
        return f"<MintedKey env={self.env} key_id={self.key_id}>"


def format_key(env: str, key_id: str, secret: str) -> str:
    """Assemble the wire form. Validates its parts, because a key that is stored with a
    key_id the regex would reject is a key that can never be presented again."""
    if env not in ENVIRONMENTS:
        raise MalformedKeyError(f"environment must be one of {ENVIRONMENTS}")
    if not KEY_ID_RE.match(key_id):
        raise MalformedKeyError(f"key_id must be {KEY_ID_CHARS} Crockford base32 chars")
    if len(secret) != SECRET_CHARS or any(c not in ALPHABET for c in secret):
        raise MalformedKeyError(f"secret must be {SECRET_CHARS} Crockford base32 chars")
    return f"mos_{env}_{key_id}_{secret}"


def mint(env: str) -> MintedKey:
    """Mint one key. The only source of API-key material in the platform."""
    if env not in ENVIRONMENTS:
        raise MalformedKeyError(f"environment must be one of {ENVIRONMENTS}")
    return MintedKey(env=env, key_id=new_key_id(), secret=_random_chars(SECRET_CHARS))


def parse(raw: str) -> PresentedKey:
    """Parse a presented credential, or raise `MalformedKeyError`.

    Whitespace is stripped first: `Authorization: Bearer <key>\\r\\n` is the single most
    common way a correct key arrives looking wrong, and refusing it teaches integrators
    that the platform is flaky rather than that their client is.

    This function does NOT lowercase, uppercase or otherwise canonicalise the body.
    Crockford base32 defines a decode-time mapping (i -> 1, o -> 0, lowercase to upper)
    and adopting it here would mean two distinct strings hash to two distinct values but
    name one key -- so the stored hash would match one spelling and not the other. The
    key is the characters. One spelling, one hash.
    """
    if not isinstance(raw, str):  # pragma: no cover - defensive at the ASGI boundary
        raise MalformedKeyError("credential is not a string")
    match = KEY_RE.match(raw.strip())
    if match is None:
        raise MalformedKeyError(
            "credential is not a MedicalOS API key "
            "(expected mos_<env>_<12>_<52>, chapter 8 section 8.2.2)"
        )
    return PresentedKey(
        env=match.group("env"),
        key_id=match.group("key_id"),
        secret=match.group("secret"),
    )
