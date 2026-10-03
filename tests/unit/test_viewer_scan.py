# SPDX-License-Identifier: Apache-2.0
"""The extension-load scan, driven in BOTH directions against synthetic bytes.

WHY THIS EXISTS SEPARATELY FROM THE e2e CHECK
----------------------------------------------
`tests/e2e/test_viewer_extension.py` asks the RUNNING viewer whether the MedicalOS
extension is in the app it serves. That module can only ever exercise whatever branch this
deployment happens to be in -- today the runtime-url one, and never the bundle one, and
never any of the ways a graph fails to resolve. A check whose other branches have never
been observed is not a check; it is a hope with an assertion attached, and the requirement
it stands for (`MOS-SAFE-089a`) has already reached a green release gate once by not being
looked at.

`scan_scripts`, `import_specifiers` and `bare_specifiers` are the decisions as pure
functions, so every branch can be driven here with no network, no container and no viewer.
Unit tests declare no infrastructure (`tests/_support/stack.py`), and these keep that:
they pass strings to a function.

THE THIRD CASE IS THE ONE THAT ACTUALLY HAPPENED
-------------------------------------------------
The first run of this probe reported the extension as LOADED. It was not. The served
document pulls `app-config.js` in as a `<script src>`, and that file's header comment
explains at length why naming '@medicalos/extension-medicalos' in `extensions` "would be a
false claim in a config file" -- so the scan found the id inside a sentence saying the id
should not be there. A false pass on the exact requirement the whole exercise is about.
`test_an_id_that_appears_only_in_a_comment_is_not_a_load` pins the fix.

Spec: MOS-SAFE-089a, MOS-REL-012.
"""

from __future__ import annotations

from tests._support.viewer import (
    EXTENSION_ID,
    SCAN_CONTROL_IDS,
    bare_specifiers,
    import_specifiers,
    scan_scripts,
)

#: Stands in for the pre-built OHIF bundle: minified, megabyte-scale, and carrying OHIF's
#: own extension ids as plain strings. Padded past the comment-stripping threshold so it
#: is treated the way the real bundle is.
_OHIF_BUNDLE = (
    "!function(e){var t={};"
    + ",".join(f'"{i}"' for i in SCAN_CONTROL_IDS)
    + ";"
    + "x" * 300_000
)


def test_an_extension_id_present_in_the_bundle_is_found() -> None:
    """The bundle branch, which this deployment does not currently produce.

    The live viewer loads the extension by runtime URL, so the e2e check never exercises
    this path. It is what a viewer rebuilt from the OHIF monorepo with this package in its
    workspace would look like, and it has to keep working for `load_path` to mean anything.
    """
    bundle = _OHIF_BUNDLE + f';r.registerExtension("{EXTENSION_ID}");'
    scan = scan_scripts([("app.bundle.js", bundle)])
    assert scan.extension_found
    assert scan.carrier == "app.bundle.js"
    assert set(scan.control_ids_found) == set(SCAN_CONTROL_IDS)


def test_an_extension_id_absent_from_the_bundle_is_not_found() -> None:
    """The negative branch, and the state a stock OHIF image is in.

    The controls are still found, which is what makes the negative a finding rather than a
    shrug: the scan can see extension ids and this one is not there.
    """
    scan = scan_scripts([("app.bundle.js", _OHIF_BUNDLE)])
    assert not scan.extension_found
    assert scan.carrier is None
    assert set(scan.control_ids_found) == set(SCAN_CONTROL_IDS)


def test_a_bundle_with_no_control_id_leaves_the_scan_unable_to_conclude() -> None:
    """"Cannot tell" must be distinguishable from "not loaded".

    `probe_viewer` turns this state into `ScanNotDiscriminating` rather than reporting an
    absence, for the reason `tests/_support/stack.py` settled for the worker probe: a
    probe that cannot see its subject must not report on it.
    """
    scan = scan_scripts([("something.js", "console.log('not a viewer bundle');")])
    assert not scan.extension_found
    assert scan.control_ids_found == ()


def test_an_id_that_appears_only_in_a_comment_is_not_a_load() -> None:
    """The false pass this probe produced on its first run, pinned.

    A hand-written served file discussing the extension id -- which
    `medos/deploy/compose/medicalos-config.js` does, at length, to explain why it must
    NOT list it -- must not read as the extension being loaded.
    """
    config = (
        "/* naming '" + EXTENSION_ID + "' here does nothing unless the viewer has been\n"
        " * rebuilt with the extension in its workspace. */\n"
        "// see also " + EXTENSION_ID + "\n"
        "window.config = { extensions: [], modes: [] };\n"
    )
    scan = scan_scripts([("app-config.js", config)])
    assert not scan.extension_found, (
        "an extension id inside a comment was read as the extension being loaded; that is "
        "the false pass that made the first run of this probe report MOS-SAFE-089a as "
        "satisfied while the button did not exist"
    )


def test_executable_code_in_the_same_small_file_is_still_scanned() -> None:
    """Comment stripping must not be an escape hatch that hides a real load.

    The failure direction that matters for the check's credibility is the opposite one: if
    stripping comments also removed code, a genuinely loaded extension in a small served
    file would read as absent.
    """
    config = (
        "/* a comment that mentions nothing */\n"
        f'window.config = {{ extensions: ["{EXTENSION_ID}"] }};\n'
    )
    scan = scan_scripts([("small.js", config)])
    assert scan.extension_found


def test_bytes_scanned_is_reported_so_an_empty_scan_cannot_look_thorough() -> None:
    """A zero-byte scan finding nothing must be visibly a zero-byte scan."""
    assert scan_scripts([]).bytes_scanned == 0
    assert scan_scripts([("a.js", "xyz")]).bytes_scanned == 3


# ======================================================================================
# The runtime-url load path's weakest link: the module graph a browser must import
# ======================================================================================
def test_relative_specifiers_are_collected_in_source_order() -> None:
    """Static, named, default and dynamic imports all reach the walk.

    A specifier the walk does not see is a module it never fetches, which is a 404 or a
    wrong MIME type it never reports -- a silent hole in the one check standing behind the
    runtime load path.
    """
    source = (
        "import { id } from './id.js';\n"
        "import getPanelModule, { PANEL_ID } from './getPanelModule.js';\n"
        "import './side-effect.js';\n"
        "const late = await import('./panels/provenance-panel.js');\n"
    )
    assert import_specifiers(source) == (
        "./id.js",
        "./getPanelModule.js",
        "./side-effect.js",
        "./panels/provenance-panel.js",
    )


def test_a_bare_specifier_is_reported_and_a_relative_one_is_not() -> None:
    """`import React from 'react'` is the failure that leaves the toolbar silently empty.

    There is no import map on the OHIF page, so a bare specifier throws a TypeError before
    any of the extension's code runs: no button, no console error anyone is watching, and
    an `app-config.js` that still lists the extension. `medos/web/ohif-extension/src/index.js`
    records "no bare imports" and "this package MUST NOT import React" as load-bearing for
    exactly this reason.
    """
    assert bare_specifiers("import { id } from './id.js';\n") == ()
    assert bare_specifiers("import '/medicalos/src/id.js';\n") == ()
    assert bare_specifiers("import React from 'react';\n") == ("react",)
    assert bare_specifiers("import { x } from '@ohif/core';\n") == ("@ohif/core",)


def test_a_specifier_without_a_file_extension_is_left_to_the_fetch() -> None:
    """`'./id'` is relative, so it is not bare -- but a browser will 404 it.

    The walk does not second-guess the specifier: it resolves it and lets the FETCH decide,
    which is what the browser does too. Encoding the extension rule here as well would give
    two places to disagree about what a browser accepts.
    """
    assert bare_specifiers("import { id } from './id';\n") == ()
    assert import_specifiers("import { id } from './id';\n") == ("./id",)


# ======================================================================================
# What the extension's README counts about itself
# ======================================================================================
#: MEASURED: `medos/web/ohif-extension/README.md` §0 said "eight files here cite them as the
#: authority for what they do and do not contribute", of `MOS-UI-012` and `MOS-UI-012a`.
#: FOUR source files cite them -- `src/index.js`, `src/getToolbarModule.js`,
#: `src/getPanelModule.js`, `src/panels/provenance-panel.js` -- and five counting the README
#: itself. Checked at the commit that first wrote the sentence and at every commit since: it
#: was four every time. Not drift; wrong from the start.
#:
#: WHY THIS NUMBER IS WORTH A CHECK. It is the count of files whose stated authority has been
#: WITHDRAWN, so it is the size of the package's orphaned surface -- register entry 109's
#: subject, and the thing a reader is deciding about when they ask what to do with this
#: package. A reader who trusts "eight" and finds four has to work out which four the sentence
#: meant, and there is no answer.
import re  # noqa: E402 -- the module's own imports are above; this section was appended
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

EXTENSION = Path(__file__).resolve().parents[2] / "medos" / "web" / "ohif-extension"
WITHDRAWN_AUTHORITY = "MOS-UI-012"


def _files_citing_the_withdrawn_requirements() -> list[str]:
    out = subprocess.run(
        ["git", "grep", "-l", "--cached", "--others", "--exclude-standard",
         WITHDRAWN_AUTHORITY, "--", "medos/web/ohif-extension"],
        cwd=EXTENSION.parents[2], capture_output=True, text=True,
    ).stdout.split()
    if not out:  # older git: --others is not accepted by grep
        out = subprocess.run(
            ["git", "grep", "-l", WITHDRAWN_AUTHORITY, "--",
             "medos/web/ohif-extension"],
            cwd=EXTENSION.parents[2], capture_output=True, text=True,
        ).stdout.split()
    return sorted(p for p in out if not p.endswith("README.md"))


def test_the_extension_readme_counts_the_orphaned_files_correctly() -> None:
    cited = _files_citing_the_withdrawn_requirements()
    assert cited, (
        f"no file under the extension cites {WITHDRAWN_AUTHORITY}; either the citations were "
        "removed -- in which case §0's whole paragraph is obsolete -- or this search is broken"
    )
    prose = " ".join((EXTENSION / "README.md").read_text(encoding="utf-8").split())
    words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
    match = re.search(
        r"\*\*(\w+)\*\* files here cite them|(\w+) files here cite them", prose
    )
    assert match, (
        "the extension README no longer states how many of its files cite the withdrawn "
        "requirements. That count is the size of the package's orphaned surface and register "
        "entry 109's subject."
    )
    token = (match.group(1) or match.group(2)).lower()
    claimed = words.get(token, int(token) if token.isdigit() else None)
    assert claimed == len(cited), (
        f"the README says {token!r} files cite {WITHDRAWN_AUTHORITY}; {len(cited)} do: "
        f"{cited}"
    )


def test_the_readme_names_every_file_it_counts() -> None:
    """A count with no names leaves a reader unable to check it, which is how 'eight' survived.

    The sentence stated a number for four years' worth of commits and named none of the files,
    so nobody could see it was wrong without running a grep.
    """
    cited = _files_citing_the_withdrawn_requirements()
    prose = (EXTENSION / "README.md").read_text(encoding="utf-8")
    missing = [
        path for path in cited
        if path.removeprefix("medos/web/ohif-extension/") not in prose
    ]
    assert not missing, (
        f"the README counts the files citing the withdrawn requirements without naming "
        f"{missing}. A reader cannot check a bare number."
    )


# ======================================================================================
# The README's `Runs?` column, against the import graph rather than against itself
# ======================================================================================
#
# §0 of `medos/web/ohif-extension/README.md` carries a blanket -- "Everything **below §1** is
# preserved as written against the OHIF deployment" -- so §1 is the one section claimed to be
# true today, and §1's table is a claim about WIRING: which modules the served entry point
# actually reaches. A grep cannot check that, which is the whole reason it drifted.
#
# MEASURED before this check existed: the table said `src/getCommandsModule.js` and
# `src/core/events.js` run "**yes**, through `standalone/`", and `standalone/app.js` imports
# neither, directly or transitively. It put the orphaned count at four where seven is right, and
# it had no row at all for `src/id.js`. Its opening sentence said the extension "loads
# into the
# viewer the compose stack runs", after the stack stopped running any OHIF build.
#
# Four wrong cells and one missing row, in a section whose whole subject is what still runs.
# Register entry 109 is the decision they feed, so a reader deciding this package's fate was
# reading a table that over-counted what works by two and under-counted the dead surface by
# three.

#: The page the origin actually serves for this package.
SERVED_ENTRY = EXTENSION / "standalone" / "app.js"

#: A row of §1's table naming a file under `src/`, with the verdict in its third cell.
README_ROW = re.compile(
    r"^\| `(src/[\w./-]+)` \| [^|]* \| \*\*(yes|no)\*\*", re.M
)

#: A relative ES-module import. The package ships no bundler and no bare specifiers, so a
#: relative path is the whole of the graph.
#:
#: COMMENTS ARE STRIPPED FIRST, and that is not tidiness. Proof-by-breaking commented out
#: `standalone/app.js`'s import of `render.js` and this gate stayed GREEN: the pattern matched
#: the text inside the `//`, so a disabled import still counted as wiring. A gate written
#: because a grep cannot see wiring was itself reading a grep of dead text.
JS_IMPORT = re.compile(r"""(?:from|import)\s+['"](\.[^'"]+)['"]""")


def _without_comments(source: str) -> str:
    """`//` and `/* */` removed, so a disabled import is not mistaken for a live one."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;)\s])//.*$", "", source)


def _reachable_from_served_entry() -> set[str]:
    """Every `src/` module the served page reaches, transitively, as a posix path."""
    seen: set[Path] = set()
    stack = [SERVED_ENTRY.resolve()]
    while stack:
        current = stack.pop()
        if current in seen or not current.is_file():
            continue
        seen.add(current)
        for rel in JS_IMPORT.findall(_without_comments(current.read_text(encoding="utf-8"))):
            stack.append((current.parent / rel).resolve())
    root = EXTENSION.resolve()
    return {
        p.relative_to(root).as_posix() for p in seen
        if p.is_relative_to(root) and p.relative_to(root).as_posix().startswith("src/")
    }


def test_the_served_page_exists_and_imports_something() -> None:
    """Otherwise the comparison below passes by finding an empty graph."""
    assert SERVED_ENTRY.is_file(), (
        f"{SERVED_ENTRY.relative_to(EXTENSION.parents[2])} is gone. §1's whole table is about "
        "what that page reaches; if the page moved, the table and this check move with it."
    )
    assert _reachable_from_served_entry(), (
        "the served page reaches no module under `src/`, which would make every **yes** "
        "in §1's "
        "table wrong and every **no** right by accident. Either the imports changed shape -- "
        "teach JS_IMPORT the new one -- or the package really is fully orphaned, which is "
        "register entry 109's subject and not a thing to discover from a green test."
    )


def test_every_src_module_has_a_row_in_the_readme_table() -> None:
    """A file with no row is the shape that hid `src/id.js`."""
    rows = {m.group(1) for m in README_ROW.finditer(
        (EXTENSION / "README.md").read_text(encoding="utf-8"))}
    on_disk = {
        p.relative_to(EXTENSION).as_posix()
        for p in sorted((EXTENSION / "src").rglob("*.js"))
    }
    missing = sorted(on_disk - rows)
    assert not missing, (
        f"§1's table has no row for {missing}. A module with no row is neither claimed to run "
        "nor claimed to be orphaned, so it is missing from both counts a reader uses to decide "
        "this package's fate."
    )
    stale = sorted(rows - on_disk)
    assert not stale, (
        f"§1's table has rows for {stale}, which are not on disk. Either they were deleted and "
        "the rows must go, or they moved and the rows must follow."
    )


def test_the_readme_runs_column_matches_the_import_graph() -> None:
    reachable = _reachable_from_served_entry()
    readme = (EXTENSION / "README.md").read_text(encoding="utf-8")
    wrong: list[str] = []
    for m in README_ROW.finditer(readme):
        path, verdict = m.group(1), m.group(2)
        claimed = verdict == "yes"
        if claimed != (path in reachable):
            wrong.append(
                f"{path}: table says **{verdict}**, the import graph from "
                f"{SERVED_ENTRY.name} says "
                f"{'reachable' if path in reachable else 'unreachable'}"
            )
    assert not wrong, (
        "§1's `Runs?` column disagrees with what the served page imports:\n  "
        + "\n  ".join(wrong)
        + "\nThis column is a claim about wiring, and a grep over the source cannot check one. "
        "Walk the graph from the entry point the origin serves and write down what it reaches."
    )
