# SPDX-License-Identifier: Apache-2.0
"""Readers for the two normative contract files, and for the spec tables they mirror.

    MOS-SEC-032 -- the permission list MUST be generated from one source file,
    `medos/contracts/permissions.yaml`. Every `permission:` value in the route registries
    MUST be a key present in that file.
    MOS-API-085 -- the route table MUST be declared in `medos/api/v1/routes.{core,train}.yaml`.

WHY THERE IS A YAML PARSER IN THIS FILE AND NOT A DEPENDENCY
------------------------------------------------------------
PyYAML is not a dependency of this project and `pyproject.toml` is not this change's to
edit. `tests/_support/stack.py` already makes the same call for `docker-compose.yml` and
reads service names with a regex -- which is fine for a list of names and would be
reckless here, because these two files are the SOURCE OF TRUTH for what a permission is
and which route enforces it. A regex that silently mis-reads one of them produces a green
check over a wrong answer, which is the exact failure mode the whole contract exists to
prevent.

So: a parser for a deliberately small YAML subset that REFUSES everything outside it.
Block mappings, block sequences, flow sequences of scalars, plain / single-quoted /
double-quoted scalars, `null`, booleans, integers, comments, blank lines. An anchor, an
alias, a flow mapping, a block scalar, a tab, a second document or a duplicate key is a
`ContractSyntaxError` naming the line -- never a shrug and a partial parse. The two files
are written inside that subset on purpose, and `tests/unit/test_permission_contract.py`
exercises the refusals as well as the acceptances.

When PyYAML becomes a dev dependency, `load_yaml` below is the only thing to delete.

THE SPEC TABLE PARSERS ARE HERE FOR THE SAME REASON THE CONTRACT FILES EXIST
-----------------------------------------------------------------------------
`catalogue_8_3_2()` and `route_table_10_2_b()` read the markdown tables of
docs/spec/08-security.md and docs/spec/10-api.md. They live beside the YAML reader, in
ONE copy, because this project has seven recorded instances of one rule living in two
copies that drifted (docs/spec/99-known-inconsistencies.md) and a checker that carried
its own second parser would be the eighth.

Spec: MOS-SEC-031, MOS-SEC-032, MOS-SEC-033, MOS-API-005, MOS-API-085, MOS-API-112.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CATALOGUE_HEADING",
    "ContractSyntaxError",
    "PERMISSION_CLASSES",
    "PERMISSION_RE",
    "Permission",
    "ROOT",
    "RouteRow",
    "catalogue_8_3_2",
    "load_permissions",
    "load_routes",
    "load_plane_routes",
    "PLANE_REGISTRIES",
    "load_yaml",
    "non_permission_paragraph",
    "permission_table_10_2_a",
    "route_table_10_2_b",
]

#: THE PLATFORM DIRECTORY, not the repository. `medos/` holds this package's siblings
#: -- `contracts/`, `api/`, `schemas/` -- and the platform package itself at
#: `medos/medos/`.
ROOT: Final[Path] = Path(__file__).resolve().parents[1]
#: THE REPOSITORY. `docs/` and `deploy/` stayed at the root when `medos/` became a
#: product directory, and they stayed for a reason: the specification binds the
#: viewer and the trainer as well, so it cannot live inside one product. Reading
#: them off ROOT looks for `medos/docs/`, which does not exist and never should.
REPO: Final[Path] = Path(__file__).resolve().parents[2]

#: MOS-SEC-031, verbatim. A third copy of this regex would be the drift this module was
#: written to prevent, so `tests/unit/test_permission_contract.py` asserts it is
#: character-identical to `medos.security.scopes.PERMISSION_RE` and to the regex inside
#: `scope_is_wellformed()` in `medos/medos/db/migrations/0004_auth.up.sql`.
PERMISSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$"
)

CATALOGUE_HEADING: Final[str] = "#### 8.3.2 The complete permission list"

#: The six values of the `class` enum (ch. 8 §8.3.1). Mirrored spelling-for-spelling; the
#: checker refuses a seventh rather than widening.
PERMISSION_CLASSES: Final[tuple[str, ...]] = (
    "read", "write", "phi", "clinical", "admin", "governance",
)


class ContractSyntaxError(ValueError):
    """A contract file this reader refuses to guess at."""


# =====================================================================================
# The YAML subset
# =====================================================================================
_KEY_RE = re.compile(r'^(?:"((?:[^"\\]|\\.)*)"|([^\s:#][^:]*?))\s*:(?:\s+(.*))?$')
_INT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_REFUSED = {
    "&": "an anchor",
    "*": "an alias",
    "!": "a tag",
    "{": "a flow mapping",
    "|": "a block scalar",
    ">": "a folded scalar",
}


@dataclass(frozen=True)
class _Line:
    number: int
    indent: int
    text: str


#: A quote opens a quoted scalar only where a scalar may begin. Without this, the
#: apostrophe in `summary: Record the tenant's TrainingDataPolicy` opens a single-quoted
#: scalar that never closes, and the file is refused for a reason that is not true of it.
_SCALAR_MAY_BEGIN_AFTER = ":,-[ "


def _strip_comment(raw: str, number: int) -> str:
    """Drop a `#` comment, respecting quotes. A `#` inside a scalar is not a comment.

    A `#` inside a folded (`>-`) continuation line is NOT supported and would be taken
    for a comment. Neither contract file contains one; if one ever does, the parser must
    learn block scalars properly rather than be argued with.
    """
    out: list[str] = []
    quote: str | None = None
    previous = ""
    i = 0
    while i < len(raw):
        ch = raw[i]
        if quote is None and ch == "#" and (not out or out[-1] in " \t"):
            break
        if quote is None and ch in "\"'" and previous in _SCALAR_MAY_BEGIN_AFTER:
            quote = ch
        elif quote == '"' and ch == "\\":
            out.append(ch)
            i += 1
            if i < len(raw):
                out.append(raw[i])
                i += 1
            previous = "\\"
            continue
        elif quote is not None and ch == quote:
            quote = None
        out.append(ch)
        if ch not in " \t":
            previous = ch
        i += 1
    if quote is not None:
        raise ContractSyntaxError(f"line {number}: unterminated {quote} quoted scalar")
    return "".join(out).rstrip()


def _lines(text: str) -> list[_Line]:
    out: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw:
            raise ContractSyntaxError(f"line {number}: a tab. This subset indents with spaces.")
        if raw.strip() in {"---", "..."}:
            raise ContractSyntaxError(f"line {number}: multi-document YAML is not read here")
        body = _strip_comment(raw, number)
        if not body.strip():
            continue
        out.append(_Line(number, len(body) - len(body.lstrip(" ")), body.strip()))
    return out


def _scalar(token: str, number: int) -> Any:
    token = token.strip()
    if token == "":
        return None
    if token[0] == '"':
        if len(token) < 2 or not token.endswith('"'):
            raise ContractSyntaxError(f"line {number}: unterminated double-quoted scalar")
        return _unescape(token[1:-1], number)
    if token[0] == "'":
        if len(token) < 2 or not token.endswith("'"):
            raise ContractSyntaxError(f"line {number}: unterminated single-quoted scalar")
        return token[1:-1].replace("''", "'")
    if token[0] == "[":
        if not token.endswith("]"):
            raise ContractSyntaxError(f"line {number}: unterminated flow sequence")
        inner = token[1:-1].strip()
        if not inner:
            return []
        return [_scalar(part, number) for part in _split_flow(inner, number)]
    if token[0] in _REFUSED:
        raise ContractSyntaxError(f"line {number}: {_REFUSED[token[0]]} is not read here")
    if token in {"null", "~"}:
        return None
    if token == "true":
        return True
    if token == "false":
        return False
    if _INT_RE.match(token):
        return int(token)
    return token


def _split_flow(inner: str, number: int) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in inner:
        if quote is None and ch in "\"'":
            quote = ch
        elif quote is not None and ch == quote:
            quote = None
        if quote is None and ch == "[":
            depth += 1
        elif quote is None and ch == "]":
            depth -= 1
        if quote is None and depth == 0 and ch == ",":
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    if quote is not None or depth:
        raise ContractSyntaxError(f"line {number}: malformed flow sequence")
    parts.append("".join(current))
    return [p for p in (p.strip() for p in parts) if p != ""]


_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "/": "/"}


def _unescape(body: str, number: int) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise ContractSyntaxError(f"line {number}: trailing backslash")
        nxt = body[i]
        if nxt not in _ESCAPES:
            raise ContractSyntaxError(f"line {number}: unsupported escape \\{nxt}")
        out.append(_ESCAPES[nxt])
        i += 1
    return "".join(out)


class _Parser:
    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.i = 0

    def at_end(self) -> bool:
        return self.i >= len(self.lines)

    def peek(self) -> _Line:
        return self.lines[self.i]

    def node(self, indent: int) -> Any:
        line = self.peek()
        if line.indent != indent:
            raise ContractSyntaxError(
                f"line {line.number}: indented {line.indent}, expected {indent}"
            )
        if line.text == "-" or line.text.startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while not self.at_end() and self.peek().indent == indent:
            line = self.peek()
            if not (line.text == "-" or line.text.startswith("- ")):
                break
            self.i += 1
            rest = line.text[1:].strip()
            child = indent + 2
            if not rest:
                if self.at_end() or self.peek().indent <= indent:
                    items.append(None)
                    continue
                items.append(self.node(self.peek().indent))
                continue
            match = _KEY_RE.match(rest)
            if match is None:
                items.append(_scalar(rest, line.number))
                continue
            # `- key: value` opens a mapping whose first key sits at indent + 2.
            self.lines.insert(self.i, _Line(line.number, child, rest))
            items.append(self.mapping(child))
        return items

    def folded(self, indent: int, number: int) -> str:
        """`>-`: the more-indented lines below, folded onto one line, trailing newline
        stripped. The one block-scalar form these two files use, because a paragraph of
        rationale beside the thing it explains is the whole point of them."""
        parts: list[str] = []
        while not self.at_end() and self.peek().indent > indent:
            parts.append(self.lines[self.i].text)
            self.i += 1
        if not parts:
            raise ContractSyntaxError(f"line {number}: `>-` with no folded lines below")
        return " ".join(parts)

    def mapping(self, indent: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while not self.at_end() and self.peek().indent == indent:
            line = self.peek()
            if line.text == "-" or line.text.startswith("- "):
                break
            match = _KEY_RE.match(line.text)
            if match is None:
                raise ContractSyntaxError(f"line {line.number}: not a `key: value` entry")
            key = match.group(1) if match.group(1) is not None else match.group(2)
            if match.group(1) is not None:
                key = _unescape(key, line.number)
            key = key.strip()
            if key in out:
                raise ContractSyntaxError(f"line {line.number}: duplicate key {key!r}")
            raw_value = match.group(3)
            self.i += 1
            if raw_value is not None and raw_value.strip() == ">-":
                out[key] = self.folded(indent, line.number)
                continue
            if raw_value is not None and raw_value.strip():
                out[key] = _scalar(raw_value, line.number)
                continue
            if self.at_end() or self.peek().indent <= indent:
                out[key] = None
                continue
            out[key] = self.node(self.peek().indent)
        return out


def load_yaml(text: str) -> Any:
    """Parse the subset described in this module's docstring, or raise."""
    lines = _lines(text)
    if not lines:
        return None
    parser = _Parser(lines)
    value = parser.node(lines[0].indent)
    if not parser.at_end():
        bad = parser.peek()
        raise ContractSyntaxError(f"line {bad.number}: dedent to an unopened level")
    return value


# =====================================================================================
# The two contract files
# =====================================================================================
@dataclass(frozen=True)
class Permission:
    """One row of the catalogue: the identifier, its grants prose, its class."""

    id: str
    grants: str
    cls: str


def load_permissions(path: Path | None = None) -> dict[str, Any]:
    """`medos/contracts/permissions.yaml`, parsed. Shape is checked by `medos/tools/permcheck.py`."""
    path = path or ROOT / "contracts" / "permissions.yaml"
    data = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractSyntaxError(f"{path}: top level is not a mapping")
    return data


#: The route registry, one file per PRODUCT. `medos/api/v1/routes.yaml` was a single file
#: describing both surfaces, which meant `medos/tools/permcheck.py` could check it against
#: only one app and was therefore right about only one of them.
PLANE_REGISTRIES: dict[str, str] = {
    "core": "routes.core.yaml",
    "train": "routes.train.yaml",
}


def load_plane_routes(plane: str, path: Path | None = None) -> dict[str, Any]:
    """One product's registry: `core` or `train`."""
    if plane not in PLANE_REGISTRIES:
        raise ValueError(f"unknown plane {plane!r}; expected one of {sorted(PLANE_REGISTRIES)}")
    path = path or ROOT / "api" / "v1" / PLANE_REGISTRIES[plane]
    data = load_yaml(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractSyntaxError(f"{path}: top level is not a mapping")
    return data


def load_routes(path: Path | None = None) -> dict[str, Any]:
    """BOTH registries, merged -- the whole documented `/api/v1` surface.

    Most checks are about the surface as a whole: that every `permission:` is a
    catalogue key, that every schema resolves, that the registry agrees with table
    10.2-B. Those questions do not care which deployable serves a row, and asking them
    per file would just mean asking them twice.

    The two that DO care are the ones that compare the registry against a running app,
    and they call `load_plane_routes` instead. Before the split they could not: one
    registry checked against `create_app` reported 33 declared-but-unserved rows on a
    correct deployment, and checked against the Train app said nothing about Core.

    `routes` concatenates. Every other key belongs to exactly one file today -- all 34
    schemas and all 15 problem types are the training surface's -- and a key appearing
    in both with different values is a contradiction rather than something to merge, so
    it raises.
    """
    if path is not None:  # an explicit file: the caller means that one
        data = load_yaml(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ContractSyntaxError(f"{path}: top level is not a mapping")
        return data

    merged: dict[str, Any] = {}
    seen_routes: list[Any] = []
    for plane in PLANE_REGISTRIES:
        doc = load_plane_routes(plane)
        for key, value in doc.items():
            if key == "routes":
                seen_routes.extend(value)
            elif key == "coverage":
                # Counts ADD, narrative keys take the last writer. `served_entries` is
                # 28 in one file and 33 in the other and 61 across the surface; taking
                # either verbatim would make the merged view disagree with its own
                # `routes` list, which is the kind of quietly-wrong number this
                # registry exists to prevent.
                current = merged.setdefault("coverage", {})
                for ckey, cval in value.items():
                    if ckey.endswith("_entries") and isinstance(cval, int):
                        current[ckey] = current.get(ckey, 0) + cval
                    else:
                        current[ckey] = cval
            elif key in merged and merged[key] != value:
                raise ContractSyntaxError(
                    f"{key!r} differs between the core and train registries. A key in "
                    f"both files with two values is a contradiction, not a merge."
                )
            else:
                merged[key] = value
    merged["routes"] = seen_routes
    return merged


# =====================================================================================
# The spec tables
# =====================================================================================
_CELL = re.compile(r"`([^`]+)`")


_ESCAPED_PIPE = "\x00"


def _rows(block: str) -> list[list[str]]:
    """Markdown table rows, split on UNESCAPED pipes.

    Table 10.2-B row 68b writes `` `IN_REVIEW`→`ACCEPTED` \\| `MODIFIED` `` inside its
    Purpose cell. Splitting on every `|` turns that one row into nine cells, the row is
    silently dropped, and the parser then reports a route table one row short of the
    one MOS-API-012 counts -- which is the class of quiet miscount this whole file
    exists to make impossible.
    """
    out: list[list[str]] = []
    for line in block.splitlines():
        if not line.startswith("|"):
            continue
        masked = line.replace("\\|", _ESCAPED_PIPE)
        cells = [
            c.strip().replace(_ESCAPED_PIPE, "\\|")
            for c in masked.strip().strip("|").split("|")
        ]
        if all(set(c) <= {"-", ":"} and c for c in cells):
            continue  # the header underline
        out.append(cells)
    return out


def catalogue_8_3_2(path: Path | None = None) -> list[Permission]:
    """Section 8.3.2's table, expanded one `Permission` per identifier.

    Four rows carry a slash list -- `model.read` / `model.create` / ... and the
    `annotation_set.read` / `.create` / ... shorthand, whose later entries elide the
    resource segment. Both are expanded here, so "one key per row of the catalogue and
    no others" (ch. 8 acceptance check 5) is checkable against identifiers rather than
    against table lines.
    """
    path = path or REPO / "docs" / "spec" / "08-security.md"
    text = path.read_text(encoding="utf-8")
    try:
        start = text.index(CATALOGUE_HEADING)
        end = text.index("Spellings that appear in other", start)
    except ValueError as exc:  # pragma: no cover - a renamed heading is a spec edit
        raise ContractSyntaxError(f"{path}: section 8.3.2 not found ({exc})") from None

    out: list[Permission] = []
    for cells in _rows(text[start:end]):
        if len(cells) != 3 or not cells[0].startswith("`"):
            continue
        names = _CELL.findall(cells[0])
        classes = _CELL.findall(cells[2])
        expanded: list[str] = []
        base = ""
        for name in names:
            if name.startswith("."):
                expanded.append(base + name)
            else:
                expanded.append(name)
                base = name.split(".")[0]
        if len(classes) == 1:
            classes = classes * len(expanded)
        if len(classes) != len(expanded):
            raise ContractSyntaxError(
                f"{path}: row {cells[0]!r} has {len(expanded)} permissions "
                f"and {len(classes)} classes"
            )
        out.extend(
            Permission(ident, cells[1], cls) for ident, cls in zip(expanded, classes)
        )
    return out


def non_permission_paragraph(path: Path | None = None) -> str:
    """The paragraph below 8.3.2 that registers spellings as **not** permissions.

    Returned as raw text and not as a parsed set, deliberately: the paragraph names both
    the refused spellings and the registered forms to use instead, in one sentence, and a
    parser that told them apart would be guessing. `medos/contracts/permissions.yaml` carries
    the pairing by hand; the checker asserts each of its `not_permissions` keys appears
    here and is absent from the catalogue, which is the direction that can be verified.
    """
    path = path or REPO / "docs" / "spec" / "08-security.md"
    text = path.read_text(encoding="utf-8")
    start = text.index("Spellings that appear in other")
    end = text.index("The three-segment rows of the table above", start)
    return text[start:end]


def permission_table_10_2_a(path: Path | None = None) -> set[str]:
    """Every permission identifier named in the first column of table 10.2-A.

    The table is chapter 10's statement of "the spellings the API layer binds to", and
    §10.11 acceptance item 12 requires every one of them to resolve in chapter 8's
    catalogue. Nothing read it until this function existed, so a route could bind a
    permission chapter 10 had never heard of and no check noticed -- which is how a
    route table and its own permission table drift, the failure this module was written
    for. Returned as a SET of identifiers and not as rows: a row groups several
    spellings behind one prose cell, the grouping is editorial, and a parser that
    treated it as meaningful would be inventing structure the table does not have.
    """
    path = path or REPO / "docs" / "spec" / "10-api.md"
    text = path.read_text(encoding="utf-8")
    start = text.index("**Table 10.2-A")
    end = text.index("#### 10.2.2", start)
    out: set[str] = set()
    for cells in _rows(text[start:end]):
        if len(cells) != 2 or not cells[0].startswith("`"):
            continue
        out.update(_CELL.findall(cells[0]))
    return out


@dataclass(frozen=True)
class RouteRow:
    """One row of table 10.2-B."""

    number: str
    method: str
    path: str
    purpose: str
    permission: str | None


def route_table_10_2_b(path: Path | None = None) -> list[RouteRow]:
    """Table 10.2-B, including the reserved `R*` rows of `MOS-API-112`.

    `permission` is `None` for the three rows the table marks `— (authn only)`.
    """
    path = path or REPO / "docs" / "spec" / "10-api.md"
    text = path.read_text(encoding="utf-8")
    start = text.index("**Table 10.2-B — the complete `/api/v1` surface**")
    end = text.index("**MOS-API-012**", start)
    out: list[RouteRow] = []
    for cells in _rows(text[start:end]):
        if len(cells) != 7 or cells[0] in {"#", ""}:
            continue
        permissions = _CELL.findall(cells[4])
        out.append(
            RouteRow(
                number=cells[0],
                method=cells[1],
                path=cells[2].strip("`"),
                purpose=cells[3],
                permission=permissions[0] if permissions else None,
            )
        )
    return out
