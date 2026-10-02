# SPDX-License-Identifier: Apache-2.0
"""`MOS-SAFE-089a`: is the MedicalOS extension LOADED in the running viewer?

WHAT THIS MODULE IS FOR
-----------------------
The 0.1.0 gate returned `24 passed`, exit 0, while `MOS-SAFE-089a` was unmet: the OHIF
toolbar button is a MUST for release 0.1 whenever it ships, and the extension could not
load into the pinned viewer image at all. Chapter 9's acceptance check 24 is the only
thing that would have caught it, and it begins "With a study open in the viewer" -- a
human with a browser. Between releases, a MUST that is only checkable by a human is a MUST
nobody checks.

`tests/_support/viewer.py` makes the load question answerable over HTTP, from outside any
browser. This module is the assertion over that probe.

THE CONFLATION IT REFUSES
-------------------------
`test_demo.py::test_the_viewer_origin_serves_the_medicalos_extension` asserts that
`http://<viewer>/medicalos/standalone/` returns 200 and that `src/core/*.js` are served
with a JavaScript MIME type. Both are true. Neither says anything about the viewer: nginx
is serving files out of a bind mount, and would keep serving them if every OHIF module in
the package were deleted. That test is not weakened or replaced -- it checks real delivery
preconditions, including the credential scan -- but it is not evidence for
`MOS-SAFE-089a`, and this module exists so the difference is asserted rather than assumed.

AND THE CLAIM THIS MODULE MADE ABOUT THAT TEST WAS ITSELF ASSUMED RATHER THAN ASSERTED
---------------------------------------------------------------------------------------
Both this module and `tests/_support/viewer.py` used to say that test "is green", and one
of them said it "has been for the life of this repository". Neither was established.
`test_demo.py` declared an `orthanc` dependency, `tests/_support/stack.py` resolved that
dependency to `http://127.0.0.1:8042`, and Orthanc has no host port because it sits alone
with the Gateway on an `internal: true` network -- so the whole module was skipped on every
run there has ever been, and the skip taxonomy reported it as infrastructure being down.
Seventeen of its tests, that one included, had never executed. Register entry 70.

The irony is the point and is why it is written down here rather than quietly deleted: this
module exists to refuse a claim that was assumed instead of asserted, and it carried one.
The rule generalises past viewers -- a sentence about what some other test proves is itself
a claim, and it decays exactly like the code it describes.

`test_the_standalone_page_is_not_evidence_for_the_toolbar_button` fails if anyone ever
tries to close the gap by pointing at the standalone page again.

TWO LOAD PATHS, AND WHY BOTH ARE PROBED
----------------------------------------
An extension reaches the running viewer either compiled into the SPA's bundle, or fetched
at runtime by the dynamic `import()` that OHIF's own `pluginImports.js` falls back to for
any `extensions` entry that is not one of its package names. This deployment uses the
second: `medos/deploy/compose/medicalos-config.js` lists `/medicalos/src/index.js`.

The first version of this module knew only the bundle path, and would have reported "not
loaded" against a viewer that does load it -- a false negative on the requirement it
exists to measure, and the mirror image of the false pass it had already caught (see
`tests/unit/test_viewer_scan.py`). `probe_viewer` now settles both, and `load_path` says
which one carried it.

WHAT IS STILL NOT PROVED HERE
------------------------------
That the import SUCCEEDED in a browser, and the rest of acceptance check 24: one
activation issuing exactly one POST, the job_id and status rendering inline and updating
from the SSE stream, a network capture showing zero viewer-to-PACS traffic outside the
Gateway. Those need a browser and a human. `tests/_support/release_criteria.py` says so on
every gate run, so a green check here can never stand in for them.

NO `xfail` IN THIS MODULE, BY HOUSE RULE
-----------------------------------------
`tests/integration/test_gateway.py` states the rule and the reason: an `xfail` reports
green and lands in the skip taxonomy's `OTHER SKIPS` bucket as an unclassified line, so
"a defect that is not red is a defect nobody is holding". These are plain assertions. If
the extension stops loading, this module goes red.

Spec: MOS-SAFE-089a, MOS-SAFE-088, MOS-REL-004, MOS-REL-009, MOS-REL-012, MOS-REL-027.
"""

from __future__ import annotations

import pytest

from tests._support import stack as _stack
from tests._support.release_criteria import by_slug
from tests._support.skips import skip_infra
from tests._support.viewer import (
    EXTENSION_ID,
    SCAN_CONTROL_IDS,
    ScanNotDiscriminating,
    ViewerProbe,
    ViewerUnreachable,
    probe_viewer,
    web_url,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def viewer() -> ViewerProbe:
    """The served viewer, interrogated once.

    A `ScanNotDiscriminating` is NOT a skip: it means the bundle scan cannot see extension
    ids in this deployment at all, so the control this module's negative answers rest on is
    gone. That is a failure, loudly, with the byte counts in the message -- "cannot tell"
    must never reach a report as "not loaded", and it must never reach one as "fine".
    """
    try:
        return probe_viewer()
    except ViewerUnreachable as exc:
        skip_infra(
            f"the viewer origin is not serving a single-page app ({exc})",
            dependency="web",
        )
    except ScanNotDiscriminating as exc:  # pragma: no cover - defensive
        raise AssertionError(str(exc)) from exc


def test_the_scan_can_see_extension_ids_at_all(viewer: ViewerProbe) -> None:
    """The positive control, asserted as its own test so a failure names itself.

    The bundle half of this module is about a string being present or absent in megabytes
    of minified JavaScript. That is only evidence while the same scan, over the same bytes,
    finds ids it is supposed to find. OHIF's own extension ids are in every v3 build.
    """
    assert viewer.control_ids_found, (
        f"none of {list(SCAN_CONTROL_IDS)} was found in {viewer.bytes_scanned} bytes at "
        f"{viewer.origin}; the scan proves nothing in either direction"
    )
    assert viewer.bytes_scanned > 1_000_000, (
        f"only {viewer.bytes_scanned} bytes of script scanned at {viewer.origin}: an OHIF "
        f"v3 build is megabytes, so this is not the viewer's real bundle"
    )
    print(f"[viewer-scan] {viewer.summary()}")


def test_the_medicalos_extension_is_loaded_in_the_served_viewer(
    viewer: ViewerProbe,
) -> None:
    """THE check `MOS-SAFE-089a` needs and the 0.1.0 gate did not have.

    "MUST live in an OHIF extension package outside the OHIF tree" is satisfied by
    `medos/web/ohif-extension/` existing. "Whenever the button ships ... it MUST, for the study
    currently open in the viewer, issue exactly one POST /api/v1/jobs" is satisfied by
    nothing at all unless the extension is in the app the browser runs. This asserts the
    second, against the bytes the viewer actually serves and the config it actually reads.
    """
    assert viewer.loaded, (
        f"{EXTENSION_ID} is not loaded in the viewer at {viewer.origin}.\n"
        f"  bundle path:      id in {viewer.bytes_scanned} bytes of served script = "
        f"{viewer.extension_in_bundle} (scan found {list(viewer.control_ids_found)}, so it "
        f"can see extension ids)\n"
        f"  runtime-url path: loader present = {viewer.runtime_loader_present}, "
        f"`extensions` = {list(viewer.config_extension_entries)}, entry = "
        f"{viewer.runtime_entry}, module graph ok = {viewer.runtime_graph_ok} "
        f"({viewer.runtime_graph_problem or 'no problem'}), id in graph = "
        f"{viewer.extension_in_runtime_graph}\n"
        f"MOS-SAFE-089a's toolbar button does not exist in this viewer. "
        f"{viewer.origin}/medicalos/standalone/ being reachable is a DIFFERENT fact, and "
        f"MOS-SAFE-089a says the substitute inherits no part of the requirement. Until it "
        f"loads, MOS-REL-009 requires one of (a) hold, (b) cut and re-run or (c) "
        f"pre-release to be chosen and recorded in docs/releases/0.1.0.md."
    )
    print(
        f"[viewer-089a] {EXTENSION_ID} is loaded via {viewer.load_path}; "
        f"module graph = {len(viewer.runtime_module_graph)} module(s)"
    )


def test_every_module_the_viewer_imports_for_the_extension_is_actually_served(
    viewer: ViewerProbe,
) -> None:
    """The runtime path's weakest link, checked rather than assumed.

    A dynamic `import()` fails on the first module that 404s, is served with a MIME type a
    browser refuses, or names a BARE specifier -- and every one of those fails SILENTLY as
    far as the toolbar is concerned: no button, no error anyone sees, and a config file
    that still says the extension is enabled. `src/index.js` documents "no bare imports"
    and "every module MUST be a browser-native ES module" as load-bearing constraints;
    this is what holds the package to them between releases.
    """
    assert viewer.runtime_entry is not None, (
        "the served app-config names no runtime extension entry, so there is no module "
        "graph to check; if the extension is meant to load from the bundle instead, that "
        "is asserted by test_the_medicalos_extension_is_loaded_in_the_served_viewer"
    )
    assert viewer.runtime_graph_ok, (
        f"the extension's module graph does not resolve: {viewer.runtime_graph_problem}. "
        f"The browser's import of {viewer.runtime_entry} throws here and the extension "
        f"never registers, while app-config.js still lists it."
    )
    assert viewer.extension_in_runtime_graph, (
        f"{len(viewer.runtime_module_graph)} module(s) reachable from "
        f"{viewer.runtime_entry} and none of them names {EXTENSION_ID}. Something is "
        f"served at that path, but it is not this extension."
    )


def test_a_config_entry_alone_is_never_accepted_as_the_extension_being_loaded(
    viewer: ViewerProbe,
) -> None:
    """The false claim this check must refuse.

    `medos/deploy/compose/medicalos-config.js` once shipped `extensions: []` with a
    comment saying that listing the extension "would be a false claim in a config
    file", because the viewer had no way to load it. Naming it there without a viewer
    that imports it is exactly that false claim: OHIF ignores an entry it cannot
    resolve, so the deployment asserts a toolbar button while the viewer shows none.

    `config_claim_unsupported` is the conjunction that refuses it -- an `extensions` entry
    is evidence only alongside a runtime loader, a resolvable module graph and the
    extension id inside it.
    """
    assert not viewer.config_claim_unsupported, (
        f"{viewer.origin}/app-config.js lists "
        f"{list(viewer.config_extension_entries)} in `extensions`, but neither load path "
        f"delivers the extension (bundle={viewer.extension_in_bundle}, "
        f"runtime loader={viewer.runtime_loader_present}, "
        f"graph ok={viewer.runtime_graph_ok}, id in graph="
        f"{viewer.extension_in_runtime_graph}). The configuration claims a toolbar button "
        f"the running viewer does not have (MOS-SAFE-089a)."
    )


def test_the_standalone_page_is_not_evidence_for_the_toolbar_button(
    viewer: ViewerProbe,
) -> None:
    """The two facts, asserted as two facts.

    The standalone page's reachability and the extension's presence in the viewer are
    measured by different fields and are never allowed to substitute for one another. That
    substitution is what made the original defect invisible: a green test named "the viewer
    origin serves the MedicalOS extension" measured the bind mount.

    `MOS-SAFE-089a` settles the substitution question itself -- where the button is cut, the
    named substitute "inherits no part of" the requirement.
    """
    assert viewer.standalone_reachable, (
        f"{viewer.origin}/medicalos/standalone/ does not answer 200 text/html; the "
        f"substitute surface is not there either"
    )
    assert viewer.standalone_advertises_the_button, (
        "the standalone page no longer carries the button's label"
    )
    # The load verdict must not be derivable from the standalone facts. Stated as an
    # assertion because it is the invariant, not an observation: `loaded` is computed from
    # the bundle scan and the runtime graph, and `ViewerProbe` gives it no other input.
    assert viewer.loaded == (
        viewer.extension_in_bundle or viewer.loaded_at_runtime
    ), "the load verdict is being derived from something other than the two load paths"


def test_the_register_and_this_module_describe_the_same_thing() -> None:
    """The check and the report must not be able to drift apart.

    `tests/_support/release_criteria.py` is what the gate prints. If someone changes what
    this module verifies and forgets the register, the gate's coverage report starts
    describing a check that no longer exists -- which is the original defect at one remove,
    a release gate making a claim nobody is holding it to.
    """
    criterion = by_slug("tier-b-ohif-toolbar-button")
    assert criterion.tier == "B"
    assert any("test_viewer_extension" in c for c in criterion.covered_by), (
        "the register does not point at this module, so the gate report and the machine "
        "check no longer describe the same thing"
    )
    assert criterion.gap, (
        "the register records no residual gap for the toolbar button, but acceptance "
        "check 24's browser half is not verified by anything here"
    )


def test_this_module_declares_its_stack_dependencies() -> None:
    """`tests/_support/stack.py` must know this module needs the viewer origin and only it.

    Both halves are load-bearing. Without `ohif`, `--require-stack` probes nothing this
    module uses and an unreachable viewer degrades to an infra skip discovered mid-run
    instead of a preflight abort. With the directory's full e2e set inherited instead, this
    module declares `orthanc-e2e` and `orthanc-rest` -- host routes to the PACS that
    MOS-DATA-006 deliberately removed -- and a check that needs nothing but static assets
    becomes unrunnable on a correctly isolated deployment.
    """
    declared = _stack.SUITE_DEPENDENCIES.get("tests/e2e/test_viewer_extension.py")
    assert declared is not None, (
        "add tests/e2e/test_viewer_extension.py to tests._support.stack.SUITE_DEPENDENCIES"
    )
    assert "web" in declared, (
        f"this module interrogates the viewer origin at {web_url()} and would degrade to "
        f"an infra skip with nothing probing it"
    )
    assert not any(d.startswith("orthanc") for d in declared), (
        "the host has no route to the PACS any more (MOS-DATA-006); declaring one would "
        "make this check unrunnable on a correctly deployed stack"
    )
