# SPDX-License-Identifier: Apache-2.0
"""The viewer's capability list and the deployment's served set are ONE set.

WHY THIS FILE EXISTS
--------------------
`medos/deploy/compose/medicalos-config.js` carries `window.MEDICALOS.capabilities`, the list the
OHIF toolbar button offers a reader (`MOS-SAFE-089a`). `medos/deploy/compose/docker-compose.yml`
carries `MEDOS_CAPABILITY_PROVIDERS`, which decides what the API admits and the worker
executes. They are the same set written twice, and they drifted: the variable defaulted to
`lung_nodule` while the viewer list stayed at CONTRACT.md section 7's three, so the shipped
stack admitted four capabilities and offered three. A reader could not submit the one
capability release 0.3.0 exists to prove.

Nothing caught it. `zero-core-change` measures a diff's path set and a path set cannot see
a list inside a config file; the 0.3.0 gate's 56 checks never open the viewer. This is the
sixth instance on this project of one rule living in two copies that drift -- after
`derive_uid`, `SourceGeometry`, `zero-core-change`, `known_capability_ids`, and the three
served-set assemblies of register entry 68.

WHY IT IS A COPY AT ALL, AND WHY THE COPY STAYS
-----------------------------------------------
A browser cannot read an environment variable, and `MOS-UI-017` says what a surface does
before the `Artifact` registry lands: render "a fixed list, configured per deployment, of
the single ServiceVersion pinned for each capability". Deployment configuration in a file
the browser can load IS the specified mechanism at this release, so the copy is correct and
the drift is the defect. The durable fix -- the viewer asking the API what it admits --
needs a route that does not exist and belongs with Chapter 19's operator surfaces, not
here. Until then this test is what makes the two copies one answer.

Spec: MOS-UI-017, MOS-UI-018, MOS-SAFE-089a, MOS-API-043. Register entry 68.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from medos.capabilities import REGISTRY as PLATFORM_REGISTRY
from medos.capabilities import providers

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "medos" / "deploy" / "compose"
VIEWER_CONFIG = COMPOSE_DIR / "medicalos-config.js"
COMPOSE_FILE = COMPOSE_DIR / "docker-compose.yml"


def _viewer_capabilities() -> list[str]:
    """`window.MEDICALOS.capabilities` out of the config file.

    Parsed rather than imported because the file is JavaScript the browser loads, and a
    test that re-stated the list would be a seventh copy of it.
    """
    text = VIEWER_CONFIG.read_text(encoding="utf-8")
    match = re.search(r"^\s*capabilities:\s*(\[[^\]]*\])", text, re.MULTILINE | re.DOTALL)
    assert match, (
        f"{VIEWER_CONFIG} has no `capabilities:` array. If the key was renamed, this test "
        f"is the thing that stops the viewer and the API drifting apart -- update it "
        f"rather than deleting it."
    )
    # Single-quoted JS string literals, and a trailing comma JSON does not allow.
    array = match.group(1).replace("'", '"')
    array = re.sub(r",\s*\]", "]", array)
    return list(json.loads(array))


def _compose_provider_default() -> str:
    """The `MEDOS_CAPABILITY_PROVIDERS` value the stack runs with when nobody sets one.

    `"${MEDOS_CAPABILITY_PROVIDERS-lung_nodule}"` -- the compose default-substitution
    form. The default is what a `docker compose up` with no `.env` actually serves, which
    is the configuration every reviewer and every new contributor meets first.
    """
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"MEDOS_CAPABILITY_PROVIDERS:\s*\"\$\{MEDOS_CAPABILITY_PROVIDERS[-:]?-?([^}]*)\}\"",
        text,
    )
    assert match, (
        f"{COMPOSE_FILE} does not set MEDOS_CAPABILITY_PROVIDERS with a default. The "
        f"viewer list cannot be checked against a set nothing declares."
    )
    return match.group(1).strip()


def test_the_viewer_offers_exactly_what_the_deployment_admits(monkeypatch) -> None:
    """The two copies of the served set agree, for the compose default configuration.

    This is the assertion the shipped stack failed. `MOS-SAFE-089a` makes the toolbar
    button the only job-creation path a reader has, so a capability missing from this list
    is a capability no reader can run, whatever the API would have accepted.
    """
    monkeypatch.setenv(providers.ENV_PROVIDERS, _compose_provider_default())
    providers.clear_cache()
    try:
        served = set(providers.capability_ids())
    finally:
        providers.clear_cache()

    offered = set(_viewer_capabilities())

    assert offered == served, (
        "the viewer and the API disagree about what this deployment runs.\n"
        f"  offered by medos/deploy/compose/medicalos-config.js : {sorted(offered)}\n"
        f"  admitted by MEDOS_CAPABILITY_PROVIDERS   : {sorted(served)}\n"
        f"  offered but not admitted: {sorted(offered - served)} "
        "(a reader submits it and the API refuses with capability_not_supported)\n"
        f"  admitted but not offered: {sorted(served - offered)} "
        "(the deployment runs it and no reader can reach it -- the 0.3.0 defect)"
    )


def test_the_viewer_list_has_no_duplicates_and_no_blanks() -> None:
    """A duplicate renders the same capability twice in the picker; a blank renders one
    the reader cannot identify. Both are cheap to introduce by hand-editing an array and
    neither is visible in the equality above, which compares sets."""
    offered = _viewer_capabilities()
    assert offered, "the picker would render nothing and the toolbar button would be inert"
    assert len(offered) == len(set(offered)), f"duplicate entries: {offered}"
    assert all(isinstance(c, str) and c.strip() for c in offered), offered


def test_the_extension_fallback_does_not_contradict_the_configured_list() -> None:
    """`readConfig()` in the extension carries its own default for the unconfigured case.

    It is a THIRD copy, and it is deliberately allowed to be a SUBSET: it answers "what
    should the button offer when nobody configured anything", and the honest answer there
    is the platform capabilities that ship in every image, not one deployment's extras.
    What it MUST NOT do is offer something no deployment serves, which would put a
    capability in front of a reader that the API will always refuse.
    """
    client_js = (
        Path(__file__).resolve().parents[2]
        / "medos" / "web" / "ohif-extension" / "src" / "core" / "client.js"
    ).read_text(encoding="utf-8")
    match = re.search(r"capabilities:\s*cfg\.capabilities\s*\|\|\s*(\[[^\]]*\])", client_js)
    if match is None:
        pytest.skip("readConfig() no longer carries a literal capability fallback")
    array = re.sub(r",\s*\]", "]", match.group(1).replace("'", '"'))
    fallback = set(json.loads(array))

    platform = set(PLATFORM_REGISTRY)
    assert fallback <= platform, (
        "the extension's unconfigured fallback offers a capability that is not a platform "
        f"capability: {sorted(fallback - platform)}. No deployment is guaranteed to serve "
        "it, so the button would offer a reader something the API refuses."
    )
