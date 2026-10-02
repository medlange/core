# SPDX-License-Identifier: Apache-2.0
"""Scope validation. The grammar half of chapter 8 section 8.3, and nothing more.

    MOS-SEC-031 -- a permission identifier MUST match
    `^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*){1,2}$`. Four or more segments is a build error.
    MOS-SEC-033 -- a permission not present in `medos/contracts/permissions.yaml` MUST be
    treated as unknown and MUST deny. Wildcards MUST NOT be expressible in a `Role`, in a
    `JobToken.scope`, or in an `ApiKey.scope`.

WHAT THIS MODULE DOES AND DOES NOT DO, AND WHY THE LINE IS HERE
---------------------------------------------------------------
It enforces the GRAMMAR (MOS-SEC-031) and the NO-WILDCARD rule (the expressibility half
of MOS-SEC-033) at issuance, in Python, mirroring the CHECK constraint
`api_keys_scope_grammar` in `0004_auth.up.sql`. Two layers, deliberately: the constraint
is the guarantee, this is the good error message.

It does NOT resolve an identifier against `medos/contracts/permissions.yaml` and it does NOT
check MOS-SEC-013's "scope MUST be a subset of the permissions held by the principal".
Both need things this block does not have -- the generated permission catalogue
(MOS-SEC-032) and the `users` / `roles` / `role_permissions` tables (chapter 12) -- and a
subset check against an empty permission set would have exactly one honest outcome:
refuse every scope. A check that is satisfied by holding nothing is worse than an absent
check, because it reads as enforcement.

So the posture is: a scope entry is a well-formed identifier or it is refused, and
NOTHING in this block treats a scope entry as a grant. `Principal.scope` is a ceiling
(MOS-SEC-027, MOS-SEC-045) and there is as yet no floor for it to bound. `require()`
below is the seam the authorization layer fills: when `role_permissions` exists it gains
the intersection with the principal's current role grants, and every caller of it is
already in the right place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

__all__ = [
    "PERMISSION_RE",
    "ScopeError",
    "validate_scope",
    "normalise_scope",
    "scope_permits",
]

# MOS-SEC-031, verbatim. Identical to the regex inside `scope_is_wellformed()` in
# medos/medos/db/migrations/0004_auth.up.sql; a test asserts the two agree on a shared table of
# cases, because two copies of a regex is exactly the drift CONTRACT.md section 2 was
# written about.
PERMISSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$"
)


class ScopeError(ValueError):
    """A scope entry that MOS-SEC-031 or MOS-SEC-033 refuses.

    The offending entry IS echoed in the message, unlike everything in
    `medos.security.authn`: a scope string is an operator's typo at issuance time, not a
    credential, and hiding it would make the CLI unusable.
    """


def normalise_scope(scope: Iterable[str] | None) -> tuple[str, ...]:
    """Strip, drop empties, de-duplicate, sort. Validation is `validate_scope`.

    Sorted and de-duplicated because `scope text[]` is a SET in every way that matters
    and storing it as an ordered bag makes two identical credentials compare unequal --
    which is how "did this rotation change anything?" becomes unanswerable.
    """
    if scope is None:
        return ()
    return tuple(sorted({s.strip() for s in scope if s and s.strip()}))


def validate_scope(scope: Iterable[str] | None) -> tuple[str, ...]:
    """Normalise and refuse anything MOS-SEC-031/033 forbids. Returns the clean tuple."""
    entries = normalise_scope(scope)
    for entry in entries:
        if "*" in entry:
            raise ScopeError(
                f"{entry!r}: wildcards MUST NOT be expressible in an ApiKey.scope "
                "(MOS-SEC-033). Name every permission."
            )
        if not PERMISSION_RE.match(entry):
            segments = entry.count(".") + 1
            if segments >= 4:
                raise ScopeError(
                    f"{entry!r}: {segments} segments. A permission identifier is "
                    "resource.action or resource.sub_resource.action; four or more is a "
                    "build error (MOS-SEC-031)."
                )
            raise ScopeError(
                f"{entry!r} does not match {PERMISSION_RE.pattern} (MOS-SEC-031). "
                "The dicom.* and fhir.* spellings are deleted."
            )
    return entries


def scope_permits(scope: Iterable[str], permission: str) -> bool:
    """Is `permission` inside this ceiling? Exact match only -- no prefix, no wildcard.

    NOT an authorization decision on its own. MOS-SEC-045: the effective permission set
    is the INTERSECTION of the principal's current role grants, the credential's scope,
    and the executing manifest's declared permissions. This answers the middle term.
    A caller that treats a `True` from here as a grant has implemented the union
    MOS-SEC-045 forbids.

    An EMPTY scope permits nothing, not everything. Chapter 12 defaults the column to
    `'{}'`, and "no scope means full access" is the single most common way a credential
    system ships a privilege escalation.
    """
    return permission in set(scope)
