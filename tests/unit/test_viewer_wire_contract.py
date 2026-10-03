# SPDX-License-Identifier: Apache-2.0
"""The viewer surface and `POST /api/v1/jobs` must agree on one wire shape.

WHAT THIS MODULE IS FOR
-----------------------
Chapter 19 found the client and the API describing the same request two different ways:

    `MOS-UI-021a` -- "The wire shapes are Chapter 10's. A surface MUST send `target` +
    `input` per `MOS-API-043` and MUST read `Job.status` per `MOS-API-049`. The current
    extension client sends `{study_instance_uid, capabilities}` and reads `body.state`,
    which is the weeks-1-2 slice's own contract and is **not** the Chapter 10 shape; the
    divergence MUST be closed in the surface, not in Chapter 10."

It is closed: `medos/web/ohif-extension/src/core/client.js` now sends the `MOS-API-043` envelope
and reads `Job.status`, and `medos/medos/api/routes_jobs.py` accepts both envelopes so that the
CONTRACT.md section 9 callers already in this tree keep working.

Two halves of one contract, written in two languages, in two directories, is exactly the
arrangement that drifts. `tests/integration/test_api.py` pins the SERVER half over real
HTTP. This module pins the CLIENT half and, more usefully, checks the two against each
other: every member the JavaScript puts on the wire is read back out of the JavaScript and
required to be a member of the pydantic model that will receive it.

WHY THE SOURCE IS PARSED RATHER THAN THE REQUEST OBSERVED
----------------------------------------------------------
Observing the request needs a browser, a running stack and a study -- `MOS-SAFE-089a`'s
acceptance check 24, which is manual and is recorded as manual in
`medos/web/ohif-extension/README.md`. This runs in the unit suite with no containers, so the
divergence cannot reappear between two of those manual passes. It is a weaker check and it
is the one that runs on every commit; neither substitutes for the other.

WHAT IT DELIBERATELY DOES NOT DO
---------------------------------
It does not execute the JavaScript. There is no `node` in this project's toolchain
(`requirements-dev.txt` pins a Python venv and nothing else), and adding one to assert a
literal object shape would be a second language runtime in CI for a check that regular
expressions answer exactly. The cost is that this module sees TEXT: it can prove the
members are named and that the old ones are gone, and it cannot prove the request is built
correctly at runtime. That is what the browser pass and `tests/e2e/test_viewer_extension.py`
are for.

Spec: MOS-API-008, MOS-API-043, MOS-API-044, MOS-API-045, MOS-API-049, MOS-UI-021,
      MOS-UI-021a, MOS-UI-029, MOS-SAFE-089a.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

#: `import ... from '<spec>'` / `export ... from '<spec>'`, capturing the specifier.
_FROM_SPECIFIER = (
    r"""(?:^|\s)(?:import|export)[^'"\n]*?from\s+['"]([^'"]+)['"]"""
)

#: `import('<spec>')` -- the dynamic form.
_DYNAMIC_SPECIFIER = r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)"""

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_ROOT = REPO_ROOT / "medos" / "web" / "ohif-extension"
CLIENT_JS = EXTENSION_ROOT / "src" / "core" / "client.js"


def _strip_js_comments(source: str) -> str:
    """Drop `/* ... */` and whole-line `//`.

    Load-bearing, not cosmetic: every module in this package carries long prose headers
    that QUOTE the old shape in order to explain why it changed. A scan that read the
    comments would find `{study_instance_uid, capabilities}` in a file that no longer
    sends it and report a divergence that is not there -- the same false reading that
    `tests/_support/viewer.py` had to exclude the app config for.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", " ", source)


@pytest.fixture(scope="module")
def client_source() -> str:
    assert CLIENT_JS.exists(), f"{CLIENT_JS} is missing; the surface has no client"
    return _strip_js_comments(CLIENT_JS.read_text(encoding="utf-8"))


# =====================================================================================
# The request: MOS-API-043
# =====================================================================================
def test_the_client_sends_the_chapter_10_envelope(client_source: str) -> None:
    """`MOS-API-043`: `{target, input: {study_instance_uid, ...}}`.

    `MOS-API-045` puts `prior_study_instance_uids` and `series_instance_uids` in the
    schema "from 0.1.0 so that prior-comparison work never requires a breaking widening",
    and the client sends them EMPTY rather than omitting them -- which is how a client
    proves it is on the widened schema rather than the pre-0.1.0 one.
    """
    body = re.search(
        r"const body = \{(.*?)\n    \};", client_source, re.DOTALL
    )
    assert body, "no request body literal in submit(); the wire shape cannot be read"
    literal = body.group(1)
    for member in (
        "target",
        "input",
        "study_instance_uid",
        "prior_study_instance_uids",
        "series_instance_uids",
    ):
        assert member in literal, f"the submitted body does not name `{member}`"


def test_the_client_no_longer_sends_the_contract_section_9_envelope(
    client_source: str,
) -> None:
    """The divergence `MOS-UI-021a` names, asserted gone rather than assumed gone.

    `capabilities` survives in this file as CONFIGURATION -- the list of capability ids a
    deployment offers as `target.kind: "capability"` -- so its mere presence proves
    nothing. What must be absent is a REQUEST BODY that carries it, which is what the
    `submit()` body literal is.
    """
    body = re.search(r"const body = \{(.*?)\n    \};", client_source, re.DOTALL)
    assert body
    literal = body.group(1)
    assert "capabilities" not in literal, (
        "submit() still puts `capabilities` on the wire; MOS-API-043 has no such member "
        "and MOS-API-008 makes it a 400"
    )


def test_every_member_the_client_sends_is_accepted_by_the_api_model() -> None:
    """The two halves, checked against each other rather than each against the spec.

    A member the client sends that the server's model does not declare is a `400` under
    `MOS-API-008`'s `additionalProperties: false` -- at runtime, in a browser, with an
    RFC 9457 document the reader has to interpret. It is a spelling mistake and it should
    be caught here.
    """
    from medos.api.routes_jobs import JobCreateRequest, JobInput, JobTarget

    source = _strip_js_comments(CLIENT_JS.read_text(encoding="utf-8"))
    body = re.search(r"const body = \{(.*?)\n    \};", source, re.DOTALL)
    assert body
    literal = body.group(1)

    top_level = set(re.findall(r"(?m)^      ([a-z_]+)[,:]", literal))
    inner = set(re.findall(r"(?m)^        ([a-z_]+):", literal))
    # `requested_outputs` is added conditionally, after the literal.
    if "body.requested_outputs" in source:
        top_level.add("requested_outputs")

    assert top_level, "no top-level members parsed out of the body literal"
    unknown_top = top_level - set(JobCreateRequest.model_fields)
    assert not unknown_top, (
        f"the client sends {sorted(unknown_top)}, which JobCreateRequest does not accept"
    )
    unknown_inner = inner - set(JobInput.model_fields)
    assert not unknown_inner, (
        f"the client sends input.{sorted(unknown_inner)}, which JobInput does not accept"
    )

    # And the target the client can build: MOS-API-044's enum is CLOSED, so a kind the
    # client invents is a 400 no matter how reasonable it looks. Chapter 14's own FT-07
    # fixture gets this wrong with `kind: "service"` (entry 20 of
    # docs/spec/99-known-inconsistencies.md), which is why it is asserted here.
    kinds = set(re.findall(r"kind: '([a-z_]+)'", source))
    allowed = set(JobTarget.model_fields["kind"].annotation.__args__)
    assert kinds, "submit() builds no target"
    assert kinds <= allowed, f"the client builds target.kind {sorted(kinds - allowed)}"


# =====================================================================================
# The response: MOS-API-049
# =====================================================================================
def test_the_status_field_is_read_in_exactly_one_place() -> None:
    """`MOS-UI-029`: "The state's classification MUST be decided in exactly one place in
    the surface's code, from `Job.status` and `problem.class`, and every renderer MUST ask
    that one function. A surface that string-matches a state in more than one component
    will eventually disagree with itself, and the disagreement will be invisible until it
    is a rejection painted red."

    That one place is `jobStatus()` in `core/client.js`. Every other module in the package
    must route through it, so `job.state` and `body.state` may appear in exactly one
    file -- the one that implements the fallback.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(EXTENSION_ROOT.rglob("*.js")):
        source = _strip_js_comments(path.read_text(encoding="utf-8"))
        hits = re.findall(r"\b(?:job|body|doc|view)\.state\b", source)
        if hits and path != CLIENT_JS:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        f"these modules read `.state` directly instead of asking jobStatus(): {offenders}"
    )


def test_jobstatus_prefers_the_chapter_10_field(client_source: str) -> None:
    """`MOS-API-049` names the field `status`; `MOS-TEST-064` spells out why `state` still
    exists ("the DB column is `state`, MOS-EXEC-001; `status` is the JSON field name").
    The fallback is permitted and its ORDER is not: a client that preferred `state` would
    keep reading the deprecated member forever."""
    body = re.search(
        r"export function jobStatus\(job\) \{(.*?)\n\}", client_source, re.DOTALL
    )
    assert body, "jobStatus() is gone; MOS-UI-029's single decision point with it"
    literal = body.group(1)
    assert literal.index("job.status") < literal.index("job.state"), (
        "jobStatus() reads `state` before `status`; MOS-API-049's field must win"
    )


# =====================================================================================
# What makes runtime loading possible at all
# =====================================================================================
def test_the_package_is_loadable_as_native_browser_modules() -> None:
    """The constraint the whole delivery route rests on.

    `medos/deploy/compose/medicalos-config.js` hands `/medicalos/src/index.js` to OHIF, which
    passes it to a native dynamic `import()`. There is no bundler in that path, so a bare
    specifier (`import React from 'react'`), an extensionless relative specifier or a
    `.jsx` file is not a style preference -- it is a module the browser refuses to load,
    and the failure is a console error in a viewer nobody is watching.

    `tests/e2e/test_viewer_extension.py` walks the SERVED graph and catches a missing
    file; this catches the import that could never have resolved, without a stack.
    """
    src = EXTENSION_ROOT / "src"
    assert not list(src.rglob("*.jsx")), (
        "a .jsx file cannot be imported by a browser; the runtime load path is broken"
    )

    bad: dict[str, list[str]] = {}
    for path in sorted(src.rglob("*.js")):
        source = _strip_js_comments(path.read_text(encoding="utf-8"))
        specifiers = re.findall(_FROM_SPECIFIER, source)
        specifiers += re.findall(_DYNAMIC_SPECIFIER, source)
        offending = [
            spec
            for spec in specifiers
            if not (spec.startswith("./") or spec.startswith("../") or spec.startswith("/"))
            or not spec.endswith((".js", ".css", ".json"))
        ]
        if offending:
            bad[str(path.relative_to(REPO_ROOT))] = offending
    assert not bad, f"non-browser-resolvable import specifiers: {bad}"
