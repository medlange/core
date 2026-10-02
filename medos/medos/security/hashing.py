# SPDX-License-Identifier: Apache-2.0
"""Secret hashing at rest. One port, one driver.

    MOS-SEC-010 -- "An `ApiKey` secret MUST be stored as `argon2id(secret, salt,
    m=64MiB, t=3, p=1)`. The plaintext MUST be returned exactly once, at creation, and
    MUST NOT be recoverable."

The parameters are NOT tunable from configuration and there is deliberately no
`Argon2idHasher(memory_cost=...)` knob that a deployment can turn down to make its test
suite faster. A parameter that can be lowered is a parameter that will be lowered, and
the resulting hashes are indistinguishable in the column from the strong ones. They are
module constants, asserted by `tests/integration/test_auth.py`.

Why a port at all, when there is exactly one driver
---------------------------------------------------
For the same reason chapter 8 puts `Authenticator` behind a port (MOS-SEC-009): the thing
that changes is the algorithm, and when it does -- argon2 parameters are a moving target,
and the encoded PHC string carries its own parameters precisely so they can move -- the
change must be a new driver plus a rehash-on-use path, not an edit to the authenticator.
`needs_rehash()` is that path's hook; it exists now so that the first parameter bump is a
one-line change rather than a data migration over a column nobody can read.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from argon2 import Type
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from argon2.low_level import hash_secret, verify_secret

__all__ = [
    "ARGON2_TIME_COST",
    "ARGON2_MEMORY_COST_KIB",
    "ARGON2_PARALLELISM",
    "ARGON2_HASH_LEN",
    "ARGON2_SALT_LEN",
    "SecretHasher",
    "Argon2idHasher",
]

# MOS-SEC-010, verbatim: m=64MiB, t=3, p=1.
ARGON2_TIME_COST: Final[int] = 3
ARGON2_MEMORY_COST_KIB: Final[int] = 64 * 1024   # 64 MiB, expressed as argon2 does
ARGON2_PARALLELISM: Final[int] = 1
ARGON2_HASH_LEN: Final[int] = 32
ARGON2_SALT_LEN: Final[int] = 16


@runtime_checkable
class SecretHasher(Protocol):
    """The port. Two methods and no way to ask for the plaintext back."""

    def hash(self, secret: str) -> bytes:
        """Return the encoded hash to store. A fresh random salt per call."""

    def verify(self, stored: bytes, secret: str) -> bool:
        """True iff `secret` produced `stored`. MUST NOT raise on a mismatch."""

    def needs_rehash(self, stored: bytes) -> bool:
        """True if `stored` was produced with weaker parameters than today's."""


class Argon2idHasher:
    """The argon2id driver. MOS-SEC-010.

    `argon2.low_level` rather than `argon2.PasswordHasher` for one reason: the high-level
    class raises `VerifyMismatchError` on a wrong secret, and a driver whose normal
    negative answer is an exception invites a caller to write `try: verify() except:
    pass`, which swallows `InvalidHash` -- a CORRUPT STORED HASH -- as if it were a wrong
    password. Here a mismatch is `False` and a corrupt row is an exception, because those
    are two different operational problems and only one of them is the caller's fault.
    """

    __slots__ = ()

    def hash(self, secret: str) -> bytes:
        """The PHC-encoded hash, as bytes, ready for `api_keys.secret_hash bytea`.

        The salt is generated inside `hash_secret` from `os.urandom`; it is embedded in
        the returned PHC string, which is why the schema has no `salt` column.
        """
        import os

        return hash_secret(
            secret.encode("utf-8"),
            os.urandom(ARGON2_SALT_LEN),
            time_cost=ARGON2_TIME_COST,
            memory_cost=ARGON2_MEMORY_COST_KIB,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LEN,
            type=Type.ID,
        )

    def verify(self, stored: bytes, secret: str) -> bool:
        """Constant-time within argon2's own comparison. `False` on mismatch.

        `InvalidHash` is NOT caught: it means the bytea in the row is not an argon2 PHC
        string, which is a corrupted or hand-edited credential row and must be loud.
        """
        try:
            return bool(verify_secret(bytes(stored), secret.encode("utf-8"), Type.ID))
        except VerifyMismatchError:
            return False
        except VerificationError:
            # The library raises the base class for "did not verify" in some versions;
            # `InvalidHash` is a sibling of it and is deliberately not caught here.
            return False

    def needs_rehash(self, stored: bytes) -> bool:
        """True when the row was written with parameters weaker than today's constants.

        Parsed out of the PHC string rather than stored in a column, because the PHC
        string is the authority and a column would be a second one.
        """
        try:
            header = bytes(stored).decode("ascii", "strict").split("$")
        except UnicodeDecodeError:
            raise InvalidHash("stored hash is not ASCII") from None
        # $argon2id$v=19$m=65536,t=3,p=1$<salt>$<hash>
        if len(header) < 4 or header[1] != "argon2id":
            raise InvalidHash("stored hash is not argon2id")
        params = dict(
            part.split("=", 1) for part in header[3].split(",") if "=" in part
        )
        return (
            int(params.get("m", 0)) < ARGON2_MEMORY_COST_KIB
            or int(params.get("t", 0)) < ARGON2_TIME_COST
            or int(params.get("p", 0)) != ARGON2_PARALLELISM
        )
