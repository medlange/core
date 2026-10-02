# SPDX-License-Identifier: Apache-2.0
"""The served OpenAPI document describes the surface this deployment actually serves.

WHY
---
`/openapi.json` and `/docs` are two of the six paths in `medos.api.auth.PUBLIC_PATHS` --
served to anyone who can reach the port, with no credential. They are therefore the
first and sometimes only description of the platform a stranger reads, and for a long
time that description said:

    "The thin Python HTTP surface of the weeks 1-2 vertical slice. Replaced by the Go
     control plane at weeks 3-5 (MOS-REL-084). No auth, no tenancy, no registries."

All three denials were false. Authentication is `AuthenticationMiddleware` plus
`ApiKeyAuthenticator`; tenancy is bound per request off the principal; the registry
router is included. The sentence was true of the weeks 1-2 slice and was never revised
as the surface grew around it -- which is how documentation fails, not by being written
wrongly but by staying still.

THE SECOND DEFECT WAS WORSE, because it cost time rather than trust: the document
declared no `securitySchemes` and no top-level `security`. A client generated from it
sends no `Authorization` header and fails on every authenticated route, and `/docs`
offered no way to enter a key -- so the honest reading of the old document was "this API
needs no credential", and everything a reader built on that reading was wrong.

WHAT THIS ASSERTS
-----------------
That the document names a bearer scheme, applies it, and does not carry any of the four
stale claims back. The stale-claim list is explicit rather than a general "is it
accurate?" check, because the specific sentences are what came back twice.

This runs against `create_app()` directly. It needs no database: the factory takes its
connection opener as an argument and nothing here opens one.

Spec: MOS-API-001, MOS-API-086 (the checked-in artefact, which does NOT exist yet and is
recorded as debt in `tests/unit/test_required_documents.py`), MOS-SEC-008.
"""

from __future__ import annotations

from typing import Any

import pytest
from medos.api.app import API_KEY_SCHEME, create_app
from medos.api.auth import PUBLIC_PATHS


def _never_connects(**_: Any) -> Any:
    raise AssertionError("building the OpenAPI document must not open a connection")


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return create_app(connect=_never_connects).openapi()


#: The sentences that were served, publicly, while being false. Kept verbatim: a
#: paraphrase would not catch the original coming back in a revert.
STALE_CLAIMS: tuple[str, ...] = (
    "No auth, no tenancy, no registries",
    "Replaced by the Go control plane",
    "weeks 1-2 vertical slice",
    "weeks 1-2 slice",
)


@pytest.mark.parametrize("claim", STALE_CLAIMS)
def test_the_document_does_not_carry_a_stale_claim(
    document: dict[str, Any], claim: str
) -> None:
    info = document["info"]
    blob = " ".join(str(info.get(k, "")) for k in ("title", "summary", "description"))
    assert claim not in blob, (
        f"the served OpenAPI document says {claim!r}. This text is public -- it is what "
        f"`/docs` shows a stranger before they have a credential -- and it was false on "
        f"every count when it was last checked."
    )


def test_the_document_declares_the_bearer_scheme(document: dict[str, Any]) -> None:
    schemes = document.get("components", {}).get("securitySchemes", {})
    assert API_KEY_SCHEME in schemes, (
        f"no {API_KEY_SCHEME!r} security scheme. Without one, a generated client sends "
        f"no Authorization header and fails on every authenticated route, and `/docs` "
        f"has no Authorize button."
    )
    scheme = schemes[API_KEY_SCHEME]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer", scheme
    assert "mos_" in scheme.get("bearerFormat", ""), (
        "bearerFormat should name the real wire format from chapter 8 section 8.2.2 "
        "rather than being left blank or guessed at as JWT."
    )


def test_the_scheme_is_actually_applied(document: dict[str, Any]) -> None:
    """A declared scheme that no operation requires documents nothing."""
    assert document.get("security") == [{API_KEY_SCHEME: []}], (
        f"top-level security is {document.get('security')!r}. Declaring the scheme "
        f"without applying it leaves the document saying the API is open."
    )


def test_the_description_says_how_to_get_a_key(document: dict[str, Any]) -> None:
    """Telling a reader they need a credential without telling them how to mint one just
    moves the dead end."""
    description = document["info"]["description"]
    assert "medos.security.cli" in description, description[:200]
    assert "Bearer" in description, description[:200]


def test_no_public_path_appears_as_a_secured_operation(document: dict[str, Any]) -> None:
    """Global security is only correct while nothing in `PUBLIC_PATHS` is in the schema.
    `/healthz` and `/readyz` are `include_in_schema=False` today; if that changes, this
    fails and the security must move from global to per-operation rather than quietly
    mislabelling two probes as authenticated."""
    served = set(document.get("paths", {}))
    overlap = served & set(PUBLIC_PATHS)
    assert not overlap, (
        f"{sorted(overlap)} are in PUBLIC_PATHS but appear in the OpenAPI document, "
        f"which applies security globally. Either exempt them per operation or take "
        f"them out of the schema."
    )


def test_the_document_still_describes_the_real_routes(document: dict[str, Any]) -> None:
    """Guard against an override that builds a valid but empty document -- every
    assertion above would pass over `{}`.

    THE FLOOR WAS 40 AND IS NOW 20, because this fixture builds CORE. `create_app` is
    the PACS-and-models service and serves 25 paths; the model-preparation service adds
    27 more on top. Lowering a threshold is usually how a check stops checking, so the
    companion assertion below pins the split itself rather than trusting this number.
    """
    paths = document.get("paths", {})
    assert len(paths) > 20, f"only {len(paths)} paths; the override may have dropped routes"
    assert any(p.startswith("/api/v1/jobs") for p in paths), sorted(paths)[:10]


def test_the_core_document_describes_core_and_the_train_document_describes_both() -> None:
    """The two deployables publish two different contracts, and each must be honest.

    A single served document that described the training surface on a Core deployment
    would tell a client to call routes that answer 404 there -- the same class of defect
    as the `securitySchemes` omission above, where the public description of the API did
    not match the API.
    """
    from medos.api.training_plane import create_training_app

    core = create_app(connect=_never_connects).openapi()["paths"]
    train = create_training_app(connect=_never_connects).openapi()["paths"]

    assert set(core) < set(train), "Train must publish a strict superset of Core"
    assert not [p for p in core if "training-runs" in p or "harvest" in p], (
        "the Core document advertises model-preparation routes it does not serve"
    )
    assert [p for p in train if "training-runs" in p], (
        "the Train document does not advertise the training surface"
    )


def test_the_document_is_built_without_a_database(document: dict[str, Any]) -> None:
    """The fixture's opener raises. Reaching here at all is the assertion: `/openapi.json`
    is public, so a version of it that touched Postgres would be an unauthenticated
    database round trip."""
    assert document["info"]["title"]
