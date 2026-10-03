# SPDX-License-Identifier: Apache-2.0
"""What THIS DEPLOYMENT adds to the viewer it serves.

`viewer/` is a DICOMweb viewer and knows nothing about MedicalOS. Everything that makes
the tree at `/mos-viewer/` this platform's clinician surface is applied from outside it:
a configuration file mounted over its defaults, and a text substitution into the response
body. Both are asserted here.

  * `MOS-SAFE-001` requires its sentence verbatim in the web UI footer and `MOS-UI-008`
    binds it to every operator surface. The viewer carries a MARKER; `medos/deploy/compose/`
    substitutes the sentence into the body with `sub_filter`. The training console used to
    carry the sentence itself, because it was MedicalOS's engineering surface and had no
    host but this one; the two halves were pinned together because a text substitution
    that matches nothing is exactly how a footer ends up empty with every test still
    green. The console is withdrawn at specification 0.4.0 (`MOS-UI-100` CUT, register
    entry 150), and the half that remains -- the viewer and its substitution -- is
    asserted here.
  * The viewer ships `viewer-config.js` holding a plain DICOMweb client. This deployment
    ships its own and mounts it over that one, which is where the tenant, the consumer
    class and the product name come from.

WHY THE DIRECTION MATTERS, and why these tests are not in `viewer/tests/`. The criterion
for that directory is that a test there may read `viewer/` and NOTHING ELSE. The platform
is a superstructure over the viewer, so the platform's suite may read both; the viewer's
may not read the platform. A viewer whose own tests reach into `medos/deploy/compose/` is a
viewer that knows its host -- the exact coupling the separation exists to remove. The
mirror halves of these two live at `viewer/tests/test_independence.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "viewer"
DEPLOY = ROOT / "medos" / "deploy" / "compose"


def test_the_deployment_supplies_a_configuration_and_mounts_it_over_the_viewers() -> None:
    """A seam with nothing on the other side is a viewer that quietly stopped being this
    platform's clinician surface.

    The viewer's half -- that it ships a default at all, that the file is loaded as a
    plain classic script, and that it is loaded BEFORE the module that reads it -- is
    asserted in `viewer/tests/test_independence.py`. This is the half that is about a
    deployment: that one exists, and that it actually lands on top of the default.

    MEASURED, and the reason the viewer ships a file rather than leaving a gap: a bind
    mount cannot create a mountpoint inside a read-only directory. Mounting
    `viewer-config.js` into the viewer's tree, which this compose file mounts `:ro`,
    failed the container at `openat viewer-config.js: read-only file system`.
    """
    theirs = DEPLOY / "viewer-config.js"
    assert theirs.is_file(), "this deployment supplies no viewer configuration"

    # COMMENTS STRIPPED, AND THE KEY ASKED FOR AT ITS SITE. Measured: the first version of
    # this check tested `"clinical_viewer" in supplied`, and setting `surface: null` LEFT
    # IT GREEN -- the words survive in the comment block above the value explaining what
    # the value is for. A gate that reads prose about a setting is not reading the setting.
    supplied = re.sub(r"//.*$", "", theirs.read_text(encoding="utf-8"), flags=re.M)
    supplied = re.sub(r"/\*.*?\*/", "", supplied, flags=re.S)
    settings = dict(re.findall(r"^\s*(\w+)\s*:\s*'([^']*)'\s*,", supplied, re.M))
    expected = {
        "productName": None,                       # any name, but there must be one
        "dicomWebRoot": None,
        "tenant": None,
        "surfaceHeader": "X-MedicalOS-Surface",
        "surface": "clinical_viewer",              # MOS-UI-005's closed value space
    }
    for key, value in expected.items():
        assert key in settings and settings[key], (
            f"the deployment's configuration does not set {key} to a string value. "
            f"What it does set: {sorted(settings) or 'nothing this check could parse'}"
        )
        assert value is None or settings[key] == value, (
            f"the deployment sets {key} to {settings[key]!r} and this platform's value "
            f"is {value!r}"
        )

    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert (
        "./viewer-config.js:/usr/share/nginx/html/mos-viewer/viewer-config.js:ro" in compose
    ), (
        "the deployment's configuration is not mounted over the viewer's default, so the "
        "surface serves a plain DICOMweb viewer and the Gateway is sent no consumer class"
    )

    shipped = (VIEWER / "viewer-config.js")
    assert shipped.is_file(), (
        "the viewer ships no default configuration, so there is no mountpoint for this "
        "deployment's file to replace and the mount above fails the container"
    )


def test_the_deployment_mounts_the_presentation_config_over_the_viewers() -> None:
    """V2's second seam, the same mechanism and the same measured constraint as the
    first: `viewer.config.json` (branding, theme, panel enablement, deep-link routing)
    ships a default inside the viewer's read-only tree, and this deployment replaces it
    by mounting over it. Without the mount the viewer runs its shipped defaults; without
    the shipped file the mount itself fails the container."""
    theirs = DEPLOY / "viewer.config.json"
    assert theirs.is_file(), "this deployment supplies no presentation configuration"
    data = json.loads(theirs.read_text(encoding="utf-8"))
    for key in ("version", "branding", "theme", "panels", "routing"):
        assert key in data, f"the deployment's presentation config lacks the {key!r} section"

    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    assert (
        "./viewer.config.json:/usr/share/nginx/html/mos-viewer/viewer.config.json:ro" in compose
    ), "the deployment's presentation config is not mounted over the viewer's default"

    shipped = VIEWER / "viewer.config.json"
    assert shipped.is_file(), (
        "the viewer ships no default presentation config, so there is no mountpoint for "
        "this deployment's file to replace and the mount above fails the container"
    )


def test_the_clinician_surface_carries_the_positioning_statement_verbatim() -> None:
    """`MOS-SAFE-001` names the web UI footer, and for four releases it was not there.

    The requirement names FOUR places its sentence must appear verbatim: the README, every
    release artifact, the API landing document, and the web UI footer. `MOS-UI-008` binds
    it to every operator surface, and this deployment has one -- the clinician surface;
    the engineering surface that used to be the second one is withdrawn at specification
    0.4.0 (`MOS-UI-100` CUT, register entry 150) and carried the statement in its own
    footer while it existed. A gate checked the README. Nothing checked either footer.

    The viewer's footer read "RESEARCH USE ONLY - NOT FOR DIAGNOSTIC USE. NOT FOR CLINICAL
    DECISION MAKING." directly beneath a comment reading "MOS-UI-008: both surfaces MUST
    carry the MOS-SAFE-001 statement verbatim in a persistently reachable footer. Verbatim
    means verbatim." The comment named the requirement, defined the word, and sat on top of
    a different sentence. The training console, then unbuilt, carried neither the
    statement nor the claim.

    THE SENTENCE IS READ OUT OF THE SPECIFICATION, not spelled here. A gate holding its own
    copy of a verbatim requirement drifts from the requirement silently, which is the exact
    failure mode the word "verbatim" exists to prevent.
    """
    repo = ROOT
    chapter = (repo / "docs" / "spec" / "09-clinical-safety.md").read_text(encoding="utf-8")
    # The statement is the blockquote directly under MOS-SAFE-001.
    anchor = chapter.index("**MOS-SAFE-001**")
    quote = re.search(r"^> (.+)$", chapter[anchor:anchor + 1200], re.M)
    assert quote, "MOS-SAFE-001 has no blockquoted statement; this gate cannot find its subject"
    statement = quote.group(1).strip()
    assert "does not diagnose" in statement, (
        f"the sentence read out of the specification is not the positioning statement: "
        f"{statement!r}"
    )

    # THE VIEWER DOES NOT CARRY IT, AND THE ORIGIN PUTS IT BACK. The viewer is a DICOMweb
    # viewer;
    # a hospital pointing it at their own archive would be asserting something about
    # MedicalOS that is not true of their deployment. So `viewer/index.html` carries a
    # MARKER and `medos/deploy/compose/nginx.conf.template` substitutes the sentence into the
    # response body.
    #
    # IN THE BYTES, NOT IN A SCRIPT. Passing the statement through `viewer-config.js` and
    # writing it with JavaScript would make a required safety marking depend on a module
    # loading -- strictly weaker than what the requirement asks for, and it would look like
    # a refactor. `sub_filter` runs on the body, so the statement is in the HTML the
    # browser parses. Verified on the running stack: the delivered document carries it
    # inside `<span class="foot-statement">`.
    #
    # THE TWO HALVES ARE PINNED TOGETHER, because a text substitution that matches nothing
    # is exactly how a footer ends up empty with every test still green.
    MARKER = "<!--HOST_STATEMENT-->"
    viewer_markup = (repo / "viewer/index.html").read_text(encoding="utf-8")
    assert MARKER in viewer_markup, (
        f"the viewer's footer has no {MARKER} for a host to substitute into, so the "
        "MOS-SAFE-001 statement has nowhere to land in the delivered document"
    )
    assert statement not in re.sub(r"<!--.*?-->", "", viewer_markup, flags=re.S), (
        "the viewer carries this platform's positioning statement in its own markup "
        "again, which is a claim about MedicalOS made by a viewer that may be pointed at "
        "any archive"
    )

    nginx = (repo / "medos/deploy/compose/nginx.conf.template").read_text(encoding="utf-8")
    filt = re.search(r"sub_filter\s+'([^']*)'\s+'([^']*)'", nginx)
    assert filt, (
        "no `sub_filter` puts a statement into the viewer's footer, so the marker is "
        "delivered to the browser as an empty comment and the footer says nothing"
    )
    assert filt.group(1) == MARKER, (
        f"the substitution matches {filt.group(1)!r} and the viewer's marker is {MARKER!r};"
        " a filter that matches nothing changes nothing and reports success"
    )
    assert filt.group(2) == statement, (
        "the sentence this deployment substitutes is not the one the specification states."
        f"\n    substituted: {filt.group(2)!r}\n    MOS-SAFE-001: {statement!r}"
    )

    # AND THE RESEARCH-USE MARKING SURVIVES BESIDE IT. The statement is what the
    # specification names; the research-use line is what every measurement record mirrors
    # as `clinical_use: research_only`, and trading one for the other is not a fix.
    viewer = (VIEWER / "index.html").read_text(encoding="utf-8")
    assert "RESEARCH USE ONLY" in re.sub(r"<!--.*?-->", "", viewer, flags=re.S), (
        "the research-use marking was removed to make room for the statement, so a reader "
        "is told what the platform is and not what this build may be used for"
    )
