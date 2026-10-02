# SPDX-License-Identifier: Apache-2.0
"""Authentication. The `Authenticator` port (MOS-SEC-009) and its 0.1 API-key driver.

docs/spec/15-delivery.md section 15.2.4, item 2 of the weeks 3-5 block:

    "API-key authentication behind an `Authenticator` interface, so OIDC is a driver
     rather than a rewrite."

Layout
------
    authn.py      the PORT: Authenticator, Principal, Credential, the refusal hierarchy
    apikeys.py    the DRIVER: ApiKeyAuthenticator (chapter 8 section 8.2.2)
    keyformat.py  the wire format: mos_<env>_<key_id>_<secret>, mint/parse
    hashing.py    SecretHasher port + Argon2idHasher (MOS-SEC-010)
    store.py      api_keys row access, every statement through tenant_tx()
    scopes.py     MOS-SEC-031/033 scope grammar
    cli.py        issuance, rotation, revocation, listing

CONTRACT.md section 1 does not define `medos/medos/security/`; it says "If you need something
this file does not define, define it ONLY inside your own module and report it." This is
that module, and it is reported.

The one rule to keep
--------------------
`Principal.tenant_id` comes from the stored credential row and from nowhere else. No
header, query parameter, path segment or body field may select a tenant (MOS-API-003).
`medos/medos/api/auth.py` is the only place a `Principal` becomes a tenant binding, and it
binds `principal.tenant_id`.
"""

from medos.security.apikeys import ApiKeyAuthenticator
from medos.security.authn import (
    AuthenticationError,
    Authenticator,
    AuthenticatorUnavailable,
    Credential,
    CrossTenantDenied,
    ExpiredCredential,
    MalformedCredential,
    MissingCredential,
    Principal,
    RevokedCredential,
    SourceNotAllowed,
    UnknownCredential,
)
from medos.security.hashing import Argon2idHasher, SecretHasher
from medos.security.keyformat import MalformedKeyError, MintedKey, PresentedKey, mint, parse
from medos.security.scopes import ScopeError, validate_scope

__all__ = [
    "ApiKeyAuthenticator",
    "Argon2idHasher",
    "AuthenticationError",
    "Authenticator",
    "AuthenticatorUnavailable",
    "Credential",
    "CrossTenantDenied",
    "ExpiredCredential",
    "MalformedCredential",
    "MalformedKeyError",
    "MintedKey",
    "MissingCredential",
    "PresentedKey",
    "Principal",
    "RevokedCredential",
    "ScopeError",
    "SecretHasher",
    "SourceNotAllowed",
    "UnknownCredential",
    "mint",
    "parse",
    "validate_scope",
]
