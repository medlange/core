# SPDX-License-Identifier: Apache-2.0
"""Is the MedicalOS extension actually LOADED in the running viewer?

WHY THIS FILE EXISTS
--------------------
The release-0.1.0 gate returned `24 passed`, exit 0, while `MOS-SAFE-089a` -- a MUST for
0.1 whenever the OHIF toolbar button ships -- was unmet: the extension did not load into
the pinned `ohif/app:v3.9.2` image at all. Nothing in the gate could see it, because the
only check that would have caught it is acceptance check 24's manual browser step.

The near-miss that made it survive is worth naming, because it is the mistake this module
exists to refuse. `tests/e2e/test_demo.py` has a test called
`test_the_viewer_origin_serves_the_medicalos_extension`. What it actually
asserts is that `http://<viewer>/medicalos/standalone/` returns 200 and that
`src/core/*.js` are served with a JavaScript MIME type -- which is true, and which would
stay true if every OHIF module in the package were deleted. nginx serving files out of a
bind mount is not a viewer loading an extension. Those are two different facts and this
module keeps them apart by construction:

    loaded / load_path           the viewer registers the extension, by one of two routes
    standalone_reachable         a static page next to the SPA answers 200

Only the first can satisfy `MOS-SAFE-089a`. The second is what `medos/web/ohif-extension/
README.md` calls the substitute, and `MOS-SAFE-089a` is explicit that a substitute
"inherits no part of" the requirement.

THERE ARE TWO WAYS AN EXTENSION CAN BE IN THE RUNNING VIEWER, AND BOTH ARE CHECKED
-----------------------------------------------------------------------------------
``bundle``       OHIF v3 resolves `appConfig.extensions` entries that name one of its OWN
                 packages from its webpack bundle. A viewer rebuilt from the monorepo with
                 this package in its workspace carries the id in the bytes the browser
                 downloads, and a grep over those bytes settles it.

``runtime-url``  `platform/app/src/pluginImports.js` ends its generated if-chain with
                 `return (await window.browserImportFunction(module)).default;`, and
                 `index.html` defines `browserImportFunction` as a native dynamic
                 `import()`. So any `extensions` entry that is NOT an OHIF package name is
                 passed to `import()` and its default export is registered. A URL is such
                 an entry, and `medos/deploy/compose/medicalos-config.js` uses one.

An earlier version of this module knew only the first, and would have reported the second
as "not loaded" -- a false negative on the very requirement it exists to measure. Both are
now probed, and `load_path` says which one carried it.

WHAT MAKES THE runtime-url ANSWER EVIDENCE RATHER THAN A CONFIG READING
------------------------------------------------------------------------
A URL sitting in `extensions` proves nothing on its own: before the viewer had a loader
for it, that same line would have been inert, and `medicalos-config.js` shipped `extensions:
[]` with a comment saying that listing an unloadable extension "would be a false claim in
a config file". So the runtime path is accepted only when ALL of the following hold, each
observable over HTTP:

  1. the served bundle contains `browserImportFunction` -- the viewer HAS a runtime loader,
     so a non-package entry is imported rather than ignored;
  2. the entry named in `extensions` is served 200 with a JavaScript MIME type (an nginx
     SPA fallback answers 200 `text/html` for every unknown path, which fails here);
  3. every module reachable from it by a relative import is served the same way, and NO
     specifier is bare -- a bare `import React from 'react'` throws in a browser with no
     import map, and the module graph would not register;
  4. the extension id from `medos/web/ohif-extension/src/id.js` appears in that graph.

WHAT EVEN THAT DOES NOT PROVE
------------------------------
That the import SUCCEEDED in a browser. A syntax error, a runtime throw in
`preRegistration`, or a registration OHIF rejected would leave every one of the four facts
above true and the toolbar empty. Chapter 9's acceptance check 24 -- one activation, one
POST, the job rendering inline, a network capture -- still needs a browser and a human,
and `tests/_support/release_criteria.py` says so on every gate run rather than letting a
green check here stand in for it. What this module removes is the failure mode that
actually occurred: shipping with the extension not loadable AT ALL and nothing noticing.

WHY THERE IS NO "VERSION ENDPOINT" INSTEAD
-------------------------------------------
The obvious alternative -- have the extension serve `GET /medicalos/version.json` -- is
the same conflation in a new coat. nginx would serve that file out of the bind mount
whether or not the viewer ever loaded a line of the extension, so a green check would
again mean "the files are on disk". A version endpoint is only evidence if the EXTENSION
publishes it at registration time, which needs a browser executing the SPA. The surfaces
probed below are the ones the VIEWER itself reads -- its bundle, its config, and the
module graph its own loader will import -- and `SCAN_CONTROL_IDS` is what stops the bundle
scan from lying in the other direction.

THE SCAN MUST NOT BE ABLE TO REPORT A FALSE ABSENCE
----------------------------------------------------
A grep that finds nothing is ambiguous: the extension may be missing, or the scan may be
broken -- wrong origin, a bundle served compressed, an OHIF build that mangles package
ids. `probe_viewer` therefore requires a POSITIVE CONTROL: at least one known OHIF
extension id must be found by the same scan, over the same bytes. If none is, the result
is `ScanNotDiscriminating` and the caller must report "cannot tell", never "absent". That
is the rule `tests/_support/stack.py` already settled for the worker probe -- "cannot
tell" is not "healthy" -- applied here.

A CONFIG THAT CLAIMS MORE THAN THE VIEWER DELIVERS IS A FAILURE, NOT A PASS
---------------------------------------------------------------------------
`medos/deploy/compose/medicalos-config.js` once shipped `extensions: []` with a comment
explaining that naming the extension there "would be a false claim in a config file", because
the viewer had no way to load it. OHIF silently ignores an entry it cannot resolve, so a
config naming an unloadable extension asserts a toolbar button that does not exist.
`config_extension_entries` therefore records the CLAIM, and `config_claim_unsupported`
fails the combination of a claim with neither load path behind it -- rather than reading
the config as evidence for itself.

PHI (CONTRACT.md section 11)
----------------------------
Nothing here reads DICOM, sends a credential or prints a patient attribute. It fetches
static assets from the viewer origin and reports counts, ids and booleans.

Spec: MOS-SAFE-089a, MOS-SAFE-088, MOS-REL-004, MOS-REL-012, MOS-REL-027.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "EXTENSION_ID",
    "SCAN_CONTROL_IDS",
    "ViewerProbe",
    "ViewerUnreachable",
    "ScanNotDiscriminating",
    "ScanResult",
    "bare_specifiers",
    "extension_id_from_source",
    "import_specifiers",
    # `web_url`, not `ohif_url`. The function was renamed when the deployment stopped
    # naming its vendor in a first-party surface (MOS-SAFE-001) and `__all__` was not:
    # ruff reported F822 "Undefined name `ohif_url` in `__all__`", and a
    # `from tests._support.viewer import *` would have raised AttributeError.
    "web_url",
    "probe_viewer",
    "scan_scripts",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTENSION_SRC = REPO_ROOT / "medos" / "web" / "ohif-extension" / "src" / "id.js"

#: Ids that MUST be findable by the same scan, over the same bytes, for a negative result
#: about the MedicalOS extension to mean anything. These are OHIF's own, shipped in every
#: v3 build; finding none of them means the scan -- not the extension -- is the problem.
SCAN_CONTROL_IDS: tuple[str, ...] = (
    "@ohif/extension-default",
    "@ohif/extension-cornerstone",
)

_TIMEOUT_S = float(os.environ.get("MEDOS_VIEWER_PROBE_TIMEOUT_S", "60"))

#: `<script ... src="/app.bundle.<hash>.js">`, in source order.
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]*\bsrc=["']([^"']+)["']""", re.IGNORECASE)

#: `extensions: [ ... ]` out of the served app config, non-greedy to the first `]`.
_CONFIG_EXTENSIONS_RE = re.compile(r"\bextensions\s*:\s*\[(.*?)\]", re.DOTALL)

#: One quoted entry inside that array.
_CONFIG_ENTRY_RE = re.compile(r"""['"]([^'"]+)['"]""")

#: The runtime import hook OHIF's `pluginImports.js` falls back to, and which `index.html`
#: defines. Its presence is what turns a non-package `extensions` entry from inert text
#: into a module the viewer imports.
_RUNTIME_LOADER_TOKEN = "browserImportFunction"

#: `import ... from '<specifier>'`, plus bare side-effect imports and dynamic `import(...)`.
_IMPORT_RE = re.compile(
    r"""(?:^|\s)import\s*(?:[\w${},*\s\n]*?\sfrom\s*)?['"]([^'"]+)['"]"""
    r"""|\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""",
    re.MULTILINE,
)

#: A ceiling on the module walk. The package is a dozen files; anything approaching this
#: is a cycle the walk already handles or a mount that is not this package.
_MAX_GRAPH_MODULES = 64

#: THE APP CONFIG IS NOT THE APP, and treating it as part of the bundle produced a FALSE
#: PASS on the first run of this probe: `medos/deploy/compose/medicalos-config.js` is loaded by
#: the document as `<script src="/app-config.js">`, and its header comment explains at length
#: why naming '@medicalos/extension-medicalos' in `extensions` "would be a false claim in
#: a config file". The scan found that sentence and reported the extension as loaded. So
#: the config is excluded from the bundle scan by name and interrogated separately, where
#: naming the extension is a CLAIM to be checked against the bundle rather than evidence.
_CONFIG_BASENAME = "app-config.js"

#: Files below this size are hand-written and are comment-stripped before scanning; the
#: webpack bundle is 15 MB of minified output and is scanned as-is. Stripping `/* ... */`
#: across a minified bundle risks eating real code that lives inside a string or a regex
#: literal, and the failure direction there is a false ABSENCE -- the one this module must
#: never produce. Machine-generated output does not carry prose about our package id.
_STRIP_COMMENTS_BELOW_BYTES = 256 * 1024


class ViewerUnreachable(Exception):
    """The viewer origin did not answer, or did not answer as a viewer."""


class ScanNotDiscriminating(Exception):
    """The scan found no control id, so it cannot be trusted to report an absence."""


def web_url() -> str:
    """The viewer origin, from the same variable every other suite reads."""
    return (os.environ.get("MEDOS_E2E_WEB_URL") or "http://127.0.0.1:3000").rstrip("/")


def extension_id_from_source() -> str:
    """The extension id, PARSED out of `medos/web/ohif-extension/src/id.js`.

    Parsed rather than repeated, so that renaming the package makes this check look for
    the new id instead of going quietly green on a string nothing uses any more.
    """
    text = EXTENSION_SRC.read_text(encoding="utf-8")
    match = re.search(r"""export\s+const\s+id\s*=\s*['"]([^'"]+)['"]""", text)
    if match is None:
        raise ValueError(
            f"no `export const id = '...'` in {EXTENSION_SRC}; the extension id cannot be "
            f"derived from the package, so a scan for it would be a scan for a literal "
            f"this file invented"
        )
    return match.group(1)


#: Resolved once. A module-level constant so callers can name it in an assertion message.
EXTENSION_ID = extension_id_from_source()


@dataclass(frozen=True)
class ViewerProbe:
    """What the viewer origin actually serves, on the two questions that differ."""

    origin: str
    #: Absolute URLs of every `<script src>` in the SPA document, in source order.
    entry_scripts: tuple[str, ...]
    #: The subset actually scanned for ids: `entry_scripts` minus the deployment's app
    #: config, which is a claim about the app and not part of it.
    bundle_scripts: tuple[str, ...]
    #: Total bytes of script scanned for ids.
    bytes_scanned: int
    #: Which of `SCAN_CONTROL_IDS` the scan found. Empty means the scan proves nothing.
    control_ids_found: tuple[str, ...]
    #: Build-time registration: the extension id is inside the SPA's own bundle.
    extension_in_bundle: bool
    #: The script that carried it, when it is there.
    carrier: str | None
    #: Does the served bundle contain OHIF's runtime dynamic-import hook? Without it, a
    #: non-package `extensions` entry is inert and the config claims something untrue.
    runtime_loader_present: bool
    #: The `extensions` array of the served app config, verbatim.
    config_extension_entries: tuple[str, ...]
    #: The entry that is not an OHIF package name, i.e. the one the viewer will `import()`.
    runtime_entry: str | None
    #: Every module URL reached from `runtime_entry` by a relative import.
    runtime_module_graph: tuple[str, ...]
    #: True when every module in that graph is served as JavaScript and no specifier is
    #: bare -- i.e. nothing in the graph would throw before registration.
    runtime_graph_ok: bool
    #: Why not, when `runtime_graph_ok` is False. Empty otherwise.
    runtime_graph_problem: str
    #: The extension id was found in the runtime module graph.
    extension_in_runtime_graph: bool
    #: The separate, weaker fact: a static page served next to the SPA.
    standalone_reachable: bool
    #: Does that page carry the button's label? (It does; it is still not the button.)
    standalone_advertises_the_button: bool

    @property
    def scan_is_discriminating(self) -> bool:
        return bool(self.control_ids_found)

    @property
    def loaded_at_runtime(self) -> bool:
        """The four conditions of the `runtime-url` path, together.

        Together and not severally: each one alone is satisfied by a deployment where the
        button does not exist. A config entry with no loader is the state this repository
        shipped in; a loader with no entry is stock OHIF; a served module that never names
        the extension is any other file under the mount.
        """
        return (
            self.runtime_loader_present
            and self.runtime_entry is not None
            and self.runtime_graph_ok
            and self.extension_in_runtime_graph
        )

    @property
    def loaded(self) -> bool:
        """Is the MedicalOS extension in the app the browser runs, by either route?"""
        return self.extension_in_bundle or self.loaded_at_runtime

    @property
    def load_path(self) -> str | None:
        if self.extension_in_bundle:
            return "bundle"
        if self.loaded_at_runtime:
            return "runtime-url"
        return None

    @property
    def config_claim_unsupported(self) -> bool:
        """The config names an extension that neither route actually delivers.

        This is the false claim the check has to refuse: `extensions` naming a package the
        bundle does not contain, or a URL the viewer has no loader for, or one whose module
        graph does not resolve. In every case the viewer shows no button while the
        deployment's own configuration says it has one.
        """
        return bool(self.config_extension_entries) and not self.loaded

    def summary(self) -> str:
        """One PHI-free line for a terminal report or an assertion message."""
        return (
            f"origin={self.origin} bundles={len(self.bundle_scripts)}/"
            f"{len(self.entry_scripts)} bytes={self.bytes_scanned} "
            f"controls={len(self.control_ids_found)}/{len(SCAN_CONTROL_IDS)} "
            f"load_path={self.load_path} in_bundle={self.extension_in_bundle} "
            f"runtime_loader={self.runtime_loader_present} "
            f"runtime_entry={self.runtime_entry} "
            f"graph={len(self.runtime_module_graph)} ok={self.runtime_graph_ok} "
            f"standalone={self.standalone_reachable}"
        )


def _strip_js_comments(source: str) -> str:
    """Drop `/* ... */` and whole-line `//` from a hand-written script.

    Line comments are matched only at the start of a line, which is what makes this safe
    to run over JavaScript containing `https://` in a string literal.
    """
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", " ", source)


def _scannable(url: str, body: str) -> str:
    """The text of one script, with prose removed where removing it is safe."""
    if len(body) < _STRIP_COMMENTS_BELOW_BYTES:
        return _strip_js_comments(body)
    return body


@dataclass(frozen=True)
class ScanResult:
    """What a scan of some script bodies found. Pure: no I/O, no origin, no network."""

    extension_found: bool
    carrier: str | None
    control_ids_found: tuple[str, ...]
    bytes_scanned: int


def scan_scripts(bodies: list[tuple[str, str]]) -> ScanResult:
    """The whole decision, as a pure function of `[(name, body), ...]`.

    Separated from `probe_viewer` so that its behaviour can be proved against synthetic
    bytes rather than only against whatever the stack happens to be serving. A check whose
    green direction has never been observed is a check nobody has tested: on this
    deployment the extension is absent, so without this seam every run would exercise one
    branch and the other would be a hope. `tests/unit/test_viewer_scan.py` drives both, plus
    the comment-only false positive this scan actually produced on its first run.
    """
    found_controls: list[str] = []
    extension_found = False
    carrier: str | None = None
    scanned = 0
    for name, raw in bodies:
        body = _scannable(name, raw)
        scanned += len(body)
        for control in SCAN_CONTROL_IDS:
            if control in body and control not in found_controls:
                found_controls.append(control)
        if not extension_found and EXTENSION_ID in body:
            extension_found = True
            carrier = name
    return ScanResult(
        extension_found=extension_found,
        carrier=carrier,
        control_ids_found=tuple(found_controls),
        bytes_scanned=scanned,
    )


def import_specifiers(source: str) -> tuple[str, ...]:
    """Every module specifier `source` imports, static and dynamic, in source order.

    Pure, and exported, so the rule that decides whether the extension's module graph will
    load in a browser can be proved against synthetic modules rather than only against
    whatever the mount happens to hold. `tests/unit/test_viewer_scan.py` drives it.
    """
    out: list[str] = []
    for relative, dynamic in _IMPORT_RE.findall(source):
        specifier = relative or dynamic
        if specifier:
            out.append(specifier)
    return tuple(out)


def bare_specifiers(source: str) -> tuple[str, ...]:
    """The specifiers a browser cannot resolve on a page with no import map.

    `import React from 'react'` throws a TypeError before any of the module's code runs,
    so ONE of these anywhere in the graph means the extension does not register and the
    toolbar is empty -- with a config file that still lists it. `medos/web/ohif-extension/
    src/index.js` records "no bare imports" as a load-bearing constraint of the runtime
    load path; this is the function that holds it to that.
    """
    return tuple(s for s in import_specifiers(source) if not s.startswith((".", "/")))


def _resolve(base_url: str, specifier: str) -> str:
    """Resolve a relative module specifier against the URL of the module importing it."""
    from urllib.parse import urljoin

    return urljoin(base_url, specifier)


def _walk_module_graph(entry_url: str) -> tuple[tuple[str, ...], bool, str, bool]:
    """Follow relative imports from `entry_url`. Returns (urls, ok, problem, id_found).

    Breadth-first with a visited set, so an import cycle terminates. Three things make a
    graph NOT ok, and each is a way the browser's import would throw before the extension
    ever registered:

      * a module that is not served, or is served as something other than JavaScript --
        including the nginx SPA fallback's `200 text/html`, which is the shape a typo in
        the mount path takes;
      * a BARE specifier (`react`, `@ohif/core`). There is no import map on this page, so
        a bare specifier is an immediate TypeError. `medos/web/ohif-extension/src/index.js`
        documents "no bare imports" as load-bearing for exactly this reason, and this is
        what holds it to that;
      * an absolute-origin specifier pointing somewhere other than this viewer, which
        would be a second origin the page has no CORS agreement with.
    """
    seen: list[str] = []
    queue: list[str] = [entry_url]
    id_found = False
    while queue and len(seen) < _MAX_GRAPH_MODULES:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.append(url)
        resp = _get(url, what="a module of the extension's graph")
        if resp.status_code != 200:
            return tuple(seen), False, f"{url} -> HTTP {resp.status_code}", id_found
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if "javascript" not in ctype and "ecmascript" not in ctype:
            return (
                tuple(seen),
                False,
                (
                    f"{url} is served as {ctype or '(no content-type)'}, which a browser "
                    f"refuses as an ES module (an nginx SPA fallback answers 200 text/html "
                    f"for every unknown path)"
                ),
                id_found,
            )
        body = resp.text
        if EXTENSION_ID in _scannable(url, body):
            id_found = True
        bare = bare_specifiers(body)
        if bare:
            return (
                tuple(seen),
                False,
                (
                    f"{url} imports the BARE specifier(s) {list(bare)}. This page has no "
                    f"import map, so the browser throws a TypeError and the whole "
                    f"extension fails to register"
                ),
                id_found,
            )
        for specifier in import_specifiers(body):
            queue.append(_resolve(url, specifier))
    return tuple(seen), True, "", id_found


def _get(url: str, *, what: str):
    import requests

    try:
        return requests.get(url, timeout=_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "not reachable"
        raise ViewerUnreachable(f"GET {url} ({what}) -> {type(exc).__name__}: {exc}") from exc


def probe_viewer(origin: str | None = None) -> ViewerProbe:
    """Fetch the SPA document, its scripts and its config, and answer both questions.

    Raises `ViewerUnreachable` when the origin is not there or does not answer as a
    viewer, and `ScanNotDiscriminating` when no control id was found -- the two failure
    modes that must never be reported as "the extension is absent".
    """
    base = (origin or web_url()).rstrip("/")

    index = _get(f"{base}/", what="the SPA document")
    if index.status_code != 200:
        raise ViewerUnreachable(f"GET {base}/ -> HTTP {index.status_code}")
    ctype = (index.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "text/html":
        raise ViewerUnreachable(
            f"GET {base}/ -> {ctype or '(no content-type)'}, not text/html: this origin is "
            f"not serving a single-page app"
        )

    srcs = [
        src if src.startswith("http") else f"{base}/{src.lstrip('/')}"
        for src in _SCRIPT_SRC_RE.findall(index.text)
    ]
    # De-duplicate, preserving source order.
    entry_scripts = tuple(dict.fromkeys(srcs))
    if not entry_scripts:
        raise ViewerUnreachable(
            f"GET {base}/ returned HTML with no <script src>: nothing to interrogate. An "
            f"nginx SPA fallback answers 200 text/html for every unknown path, so this is "
            f"most likely the wrong origin."
        )

    bundle_scripts = tuple(
        url for url in entry_scripts if not url.rsplit("/", 1)[-1] == _CONFIG_BASENAME
    )

    bodies: list[tuple[str, str]] = []
    for url in bundle_scripts:
        resp = _get(url, what="a viewer script")
        if resp.status_code != 200:
            # A 404 on one script is not fatal to the scan; the control check below is
            # what decides whether what we DID read was enough to conclude anything.
            continue
        bodies.append((url, resp.text))

    scan = scan_scripts(bodies)
    found_controls = list(scan.control_ids_found)
    in_bundle = scan.extension_found
    carrier = scan.carrier
    scanned = scan.bytes_scanned

    if not found_controls:
        raise ScanNotDiscriminating(
            f"scanned {scanned} bytes across {len(bundle_scripts)} script(s) at {base} and "
            f"found none of {list(SCAN_CONTROL_IDS)}. OHIF's own extension ids are in "
            f"every v3 build, so their absence means this scan cannot see extension ids at "
            f"all -- wrong origin, a compressed or split bundle, or a build that mangles "
            f"package ids. Reporting the MedicalOS extension as 'absent' on this evidence "
            f"would be a false negative; 'cannot tell' is not 'not loaded'."
        )

    # The runtime loader. Checked over the DOCUMENT as well as the bundles: `index.html`
    # is where `browserImportFunction` is defined and `pluginImports` is where it is
    # called, and either half missing makes a non-package `extensions` entry inert.
    runtime_loader_present = _RUNTIME_LOADER_TOKEN in index.text and any(
        _RUNTIME_LOADER_TOKEN in raw for _name, raw in bodies
    )

    config = _get(f"{base}/{_CONFIG_BASENAME}", what="the app config")
    entries: tuple[str, ...] = ()
    if config.status_code == 200:
        # Comment-stripped: this file's header DISCUSSES the extension id at length,
        # which is the same prose that made the bundle scan report a false pass.
        block = _CONFIG_EXTENSIONS_RE.search(_strip_js_comments(config.text))
        if block:
            entries = tuple(_CONFIG_ENTRY_RE.findall(block.group(1)))

    # The entry OHIF will hand to `import()`: anything that is not one of its own package
    # names. `@ohif/...` entries are resolved from the bundle by the generated if-chain and
    # never reach the dynamic import.
    runtime_entry = next(
        (e for e in entries if not e.startswith("@ohif/")),
        None,
    )
    graph: tuple[str, ...] = ()
    graph_ok = False
    graph_problem = "no non-OHIF entry in `extensions`, so nothing is imported at runtime"
    id_in_graph = False
    if runtime_entry is not None:
        graph, graph_ok, graph_problem, id_in_graph = _walk_module_graph(
            _resolve(f"{base}/", runtime_entry)
        )

    standalone = _get(f"{base}/medicalos/standalone/", what="the standalone page")
    reachable = standalone.status_code == 200 and (
        (standalone.headers.get("Content-Type") or "").startswith("text/html")
    )

    return ViewerProbe(
        origin=base,
        entry_scripts=entry_scripts,
        bundle_scripts=bundle_scripts,
        bytes_scanned=scanned,
        control_ids_found=tuple(found_controls),
        extension_in_bundle=in_bundle,
        carrier=carrier,
        runtime_loader_present=runtime_loader_present,
        config_extension_entries=entries,
        runtime_entry=runtime_entry,
        runtime_module_graph=graph,
        runtime_graph_ok=graph_ok,
        runtime_graph_problem=graph_problem,
        extension_in_runtime_graph=id_in_graph,
        standalone_reachable=reachable,
        standalone_advertises_the_button=reachable
        and "Analyze with MedicalOS" in standalone.text,
    )
