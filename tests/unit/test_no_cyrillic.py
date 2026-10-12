# SPDX-License-Identifier: Apache-2.0
"""The language gate: no Cyrillic in the repository outside localization.

THE OWNER'S RULE (AGENTS.md, 2026-10-11): the three public products are
international-facing; every code comment, doc, example and config ships in
English. The only permitted Cyrillic is product locale data — the i18n/*.json
translation files, and the language-picker self-labels in locale registries
(each language labels itself in its own language: the Russian entry in
`viewer/src/core/i18n.js` sits beside `Deutsch` and `Español`).

This test scans the tree the way a contributor's edit would touch it and
fails with a file:line list, so the rule is enforced by CI rather than by
memory. Scope discipline: binary formats and machine-generated caches are
not text-scanned; .deid/.evidence hold local research notes that never ship.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CYRILLIC = re.compile(r"[\u0400-\u04FF]")

#: Text-bearing extensions a source scan cares about. Binaries (images,
#: fonts, weights, TorchScript .ts blobs) cannot carry reviewable prose;
#: caches and VCS internals are generated, not authored.
TEXT_EXTENSIONS = {
    ".py", ".js", ".mjs", ".md", ".json", ".yaml", ".yml", ".toml",
    ".html", ".css", ".sh", ".txt", ".cfg", ".ini", ".rst",
}

#: Directories that are generated, vendored, local-only research state, or
#: run outputs (artifacts/ holds trained models and benchmark reports —
#: binary .ts blobs can carry arbitrary byte sequences) — none of it ships
#: in a public split, and scanning it would fail on machine output, not on
#: authored prose.
SKIP_DIRS = {
    ".git", "__pycache__", ".ruff_cache", ".pytest_cache", ".venv",
    "node_modules", "dist", "build", ".deid", ".evidence", "medos.egg-info",
    "artifacts",
}

#: Translation files: Cyrillic is the product here.
_LOCALE_FILE = re.compile(r"(^|/)i18n/[a-z]{2,3}(-[a-zA-Z]+)?\.json$")

#: In a locale registry (the LANGUAGES table) a language labels ITSELF in
#: its own language — the Russian entry sits beside `Deutsch` and `日本語`.
#: Only `label:` lines carry that data; anything else Cyrillic in the file
#: is a violation like anywhere else.
_LOCALE_LABEL_LINE = re.compile(r"\blabel\s*:")


def _is_permitted(path: Path, line: str) -> bool:
    """The locale allowlist, applied per-line so a stray Russian comment in
    a registry file still fails."""
    rel = path.relative_to(ROOT).as_posix()
    if _LOCALE_FILE.search(rel):
        return True
    if rel.endswith("src/core/i18n.js") and _LOCALE_LABEL_LINE.search(line):
        return True
    return False


def test_no_cyrillic_outside_localization() -> None:
    violations: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if CYRILLIC.search(line) and not _is_permitted(path, line):
                violations.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not violations, (
        "Cyrillic outside localization (the owner's English-only rule, "
        f"AGENTS.md): {violations}"
    )
