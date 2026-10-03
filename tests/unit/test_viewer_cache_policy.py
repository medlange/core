# SPDX-License-Identifier: Apache-2.0
"""The viewer's freshness is the origin's job, not a number somebody remembers to raise.

WHAT WAS THERE
--------------
Every module specifier, the stylesheet link, the entry `<script>` and two runtime
`fetch`es carried a hand-written query token:

    import { draw } from './src/render/viewport.js?v=0.4.0-32';
    <link rel="stylesheet" href="./styles.css?v=0.4.0-32">
    await fetch(`./i18n/${code}.json?v=0.4.0-32`)

Sixty-four of them across seventeen files, all the same string, raised by hand. The `-32`
is not a version of anything: it is the number of times someone edited a file, reloaded,
saw the old behaviour, and bumped. In one working day it went from `-1` to `-32`.

WHY IT CAN GO
-------------
The origin already answers `Cache-Control: no-store` for everything under `/mos-viewer/`,
and `medos/deploy/compose/nginx.conf.template` says why in full: native ES modules, no build
step, no content hash in any filename, so a browser that reuses yesterday's `volume.js`
against today's `app.js` gets a page that loads and silently lacks the feature. Measured on
the running stack, `nginx/1.27.5`:

    GET /mos-viewer/app.js   -> 200, Cache-Control: no-store
    GET /mos-viewer/         -> 200, Cache-Control: no-store

`no-store` forbids storing the response at all, which is strictly stronger than changing
the URL: a token defeats the cache for the ONE file whose specifier was edited, and a
token that somebody forgot to raise defeats nothing. That is the failure it was raised
thirty-one times to paper over.

WHAT THIS TEST IS FOR
---------------------
Deleting the token is easy; keeping it deleted is the point. Nothing stops the next
debugging session from pasting `?v=` back into one import, which works, ships, and puts
the repository back to hand-maintaining sixty-four copies of a number. So the token is
forbidden HERE, and the header it depends on is asserted in the same test -- the two facts
are one fact, and separating them is how you get a tree with no tokens served by an origin
that lost its `no-store` line.

WHAT THIS TEST IS NOT. It is not a rule against cache busting. A build step that emits
`app.7f3c91.js` is the production answer and needs no token, no header and no test; when
this repository grows one, this test is what should be deleted, deliberately, rather than
worked around with a token that agrees with it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "viewer"
NGINX = ROOT / "medos" / "deploy" / "compose" / "nginx.conf.template"

SERVED = ("*.js", "*.mjs", "*.html", "*.css")


def _served_files() -> list[Path]:
    return sorted(
        p
        for pattern in SERVED
        for p in VIEWER.rglob(pattern)
        if "__pycache__" not in p.parts
    )


def _location_block(conf: str, path: str) -> str:
    """The body of `location <path> { ... }`, braces balanced."""
    start = conf.index(f"location {path} {{")
    depth, i = 0, conf.index("{", start)
    for j in range(i, len(conf)):
        if conf[j] == "{":
            depth += 1
        elif conf[j] == "}":
            depth -= 1
            if depth == 0:
                return conf[i + 1 : j]
    raise AssertionError(f"`location {path}` is not closed in {NGINX.name}")


def test_no_module_specifier_carries_a_hand_bumped_version_token() -> None:
    offenders: dict[str, list[int]] = {}
    for path in _served_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "?v=" in line:
                rel = str(path.relative_to(ROOT)).replace("\\", "/")
                offenders.setdefault(rel, []).append(number)

    assert not offenders, (
        "a hand-maintained cache token is back in the viewer:\n"
        + "\n".join(
            f"    {rel}: line(s) {', '.join(str(n) for n in lines)}"
            for rel, lines in sorted(offenders.items())
        )
        + "\n  The origin answers `Cache-Control: no-store` for everything under "
        "/mos-viewer/ (medos/deploy/compose/nginx.conf.template), which is stronger than a "
        "changed URL and does not have to be remembered. The last one of these reached "
        "`?v=0.4.0-32` in a single day, across 64 sites, because the number is a count of "
        "reloads rather than a version."
    )


def test_the_origin_refuses_to_store_the_viewer_at_all() -> None:
    """The header the deletion above depends on -- asserted with it, not beside it."""
    conf = NGINX.read_text(encoding="utf-8")
    body = _location_block(conf, "/mos-viewer/")

    assert re.search(r'^\s*add_header\s+Cache-Control\s+"no-store";', body, re.M), (
        "`location /mos-viewer/` no longer answers `Cache-Control: no-store`. The viewer "
        "ships native ES modules with no build step and no content hash in any filename, "
        "and nothing else keeps a browser from running yesterday's module against today's "
        "`app.js` -- a page that loads, silently lacks the feature, and matches the file "
        "on disk. The query tokens that used to paper over this were removed BECAUSE of "
        "this header; see test_no_module_specifier_carries_a_hand_bumped_version_token."
    )

    assert "try_files" not in body, (
        "a `try_files` fallback is back in `location /mos-viewer/`. An SPA fallback here "
        "answers 200 text/html for every path this origin does not recognise, which is how "
        "a root-relative fetch from the viewer came back as another product's shell and "
        "read as a stale build."
    )
