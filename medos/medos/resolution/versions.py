# SPDX-License-Identifier: Apache-2.0
"""Semantic versions and the CLOSED range grammar of `MOS-REG-062`..`MOS-REG-064`.

WHY THE GRAMMAR IS CLOSED, AND WHY IT IS PARSED HERE AND NOT AT DISPATCH
-----------------------------------------------------------------------
`MOS-REG-062`: "The range grammar is closed and small. Anything outside it is a parse
error surfaced at manifest/pin validation time, not at dispatch time." A range that only
fails when a study arrives is a range whose defect is discovered by a patient.

So `parse_range` RAISES on anything the grammar does not admit, and the admission path
(`validate_range`) is expected to call it when a `TenantPin`, a `Deployment.pin_range` or
a job pin is written. `Resolve()` itself never raises (`MOS-REG-052`): it treats an
unparseable range as an `F5` exclusion carrying `range_unsatisfied` and the parse message
in `detail`, because by then the malformed value is already stored and the clinically
correct answer is a legible refusal, not a 500.

THE THREE RULES THAT ARE EASY TO GET BACKWARDS
----------------------------------------------
1. A BARE version is EXACT, not caret (`MOS-REG-062`'s table, row 2: "`3.2.1` | exact
   (bare is exact, **not** caret)"). Every other ecosystem reads a bare version as caret;
   this one does not, and the difference is a silent minor-version upgrade in a clinical
   deployment.
2. Disjunction is not supported and `*`, `latest`, `x` and the empty range are rejected
   (`MOS-REG-063`). "A caller wanting 'any' omits the pin entirely, which is a visible,
   auditable choice."
3. A prerelease does NOT satisfy a range unless the range names a prerelease in the same
   `(major, minor, patch)` tuple (`MOS-REG-064`). `>=3.2 <4` MUST NOT match `3.5.0-rc.1`.

Pure: no clock, no randomness, no I/O. Imports stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

__all__ = [
    "OPERATORS",
    "Clause",
    "Range",
    "RangeSyntaxError",
    "Version",
    "compare_versions",
    "parse_range",
    "parse_version",
    "validate_range",
]


class RangeSyntaxError(ValueError):
    """A pin the grammar of `MOS-REG-062` does not admit.

    A PUBLISHING/CONFIGURATION defect, never a clinical outcome: it is raised where a pin
    is written, and `Resolve()` never propagates it (`MOS-REG-052`).
    """


# `MOS-REG-062`'s `op` production, longest first so `>=` is not read as `>`.
OPERATORS: Final[tuple[str, ...]] = (">=", "<=", ">", "<", "=", "^", "~")

_NUM = r"(?:0|[1-9][0-9]*)"
_PRE = r"[0-9A-Za-z.-]+"
# `partial := num ( "." num ( "." num ( "-" prerelease )? )? )?`
_PARTIAL_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?P<major>{_NUM})(?:\.(?P<minor>{_NUM})(?:\.(?P<patch>{_NUM})"
    rf"(?:-(?P<pre>{_PRE}))?)?)?$"
)
_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    rf"^(?P<major>{_NUM})\.(?P<minor>{_NUM})\.(?P<patch>{_NUM})(?:-(?P<pre>{_PRE}))?$"
)


def _prerelease_parts(pre: str) -> tuple[object, ...]:
    """Semver 2.0 §11: dot-separated identifiers, numeric ones compared numerically."""
    parts: list[object] = []
    for ident in pre.split("."):
        if ident.isdigit() and (ident == "0" or not ident.startswith("0")):
            parts.append(int(ident))
        else:
            parts.append(ident)
    return tuple(parts)


@dataclass(frozen=True, slots=True)
class Version:
    """One `x.y.z[-pre]`. The registry stores the parts separately; this is their algebra.

    `artifacts.version_major/minor/patch/version_pre` are the same four values, and
    `0012_artifacts.up.sql`'s `artifacts_version_parts_agree` CHECK is what stops the
    string and the parts from disagreeing about the same number.
    """

    major: int
    minor: int
    patch: int
    prerelease: str = ""

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease != ""

    def sort_tuple(self) -> tuple[int, int, int, int, tuple[object, ...]]:
        """Ascending semver order. A prerelease sorts BELOW its release (`MOS-REG-058` P6)."""
        # 0 for a prerelease, 1 for a release: ascending, the prerelease comes first,
        # which is "a prerelease sorts below its release".
        return (
            self.major,
            self.minor,
            self.patch,
            0 if self.is_prerelease else 1,
            _prerelease_parts(self.prerelease) if self.is_prerelease else (),
        )

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{self.prerelease}" if self.prerelease else base


def parse_version(text: str) -> Version:
    """A FULL `x.y.z[-pre]`. Artifact versions are never partial (`semver` domain, ch. 12)."""
    m = _VERSION_RE.match(text.strip())
    if m is None:
        raise RangeSyntaxError(f"{text!r} is not a semantic version")
    return Version(
        int(m["major"]), int(m["minor"]), int(m["patch"]), m["pre"] or ""
    )


def compare_versions(a: Version, b: Version) -> int:
    """`-1 | 0 | 1`, ascending. Prereleases order per semver 2.0 §11."""
    ta, tb = a.sort_tuple(), b.sort_tuple()
    # The trailing prerelease tuples mix int and str, which Python will not compare
    # across types, so they are compared identifier by identifier (semver 2.0 §11:
    # "Numeric identifiers always have lower precedence than non-numeric identifiers").
    for x, y in zip(ta[:4], tb[:4], strict=True):
        if x != y:
            return -1 if x < y else 1
    pa, pb = ta[4], tb[4]
    for x, y in zip(pa, pb, strict=False):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int):
            return -1
        if isinstance(y, int):
            return 1
        return -1 if str(x) < str(y) else 1
    if len(pa) != len(pb):
        return -1 if len(pa) < len(pb) else 1
    return 0


@dataclass(frozen=True, slots=True)
class Clause:
    """One `op? partial` of `MOS-REG-062`. `precision` is how many parts were written."""

    op: str
    major: int
    minor: int
    patch: int
    prerelease: str
    precision: int  # 1 = "3", 2 = "3.2", 3 = "3.2.1"

    @property
    def floor(self) -> Version:
        """The partial with its unwritten components zero-filled."""
        return Version(self.major, self.minor, self.patch, self.prerelease)

    def _upper_exclusive(self) -> Version | None:
        """The `<` bound `^` and `~` expand to; `None` for the comparison operators."""
        if self.op == "^":
            # Standard caret: the leftmost NON-ZERO component is locked. `MOS-REG-062`
            # states the 0.x case explicitly ("`^0.4.1` | `>=0.4.1 <0.5.0` (0.x caret is
            # minor-locked)"), and `^0.0.3` follows the same rule one place further right.
            if self.major > 0 or self.precision == 1:
                return Version(self.major + 1, 0, 0)
            if self.minor > 0 or self.precision == 2:
                return Version(0, self.minor + 1, 0)
            return Version(0, 0, self.patch + 1)
        if self.op == "~":
            # `~3` is major-locked; `~3.2` and `~3.2.0` are minor-locked.
            if self.precision == 1:
                return Version(self.major + 1, 0, 0)
            return Version(self.major, self.minor + 1, 0)
        return None

    def matches(self, v: Version) -> bool:
        floor = self.floor
        cmp = compare_versions(v, floor)
        if self.op in ("^", "~"):
            upper = self._upper_exclusive()
            assert upper is not None
            return cmp >= 0 and compare_versions(v, upper) < 0
        if self.op == "=":
            # An exact clause written at less than full precision constrains only the
            # components it names: `=3.2` admits every 3.2.x. `=3.2.1` admits one version.
            if self.precision == 3:
                return cmp == 0
            if v.major != self.major:
                return False
            return self.precision == 1 or v.minor == self.minor
        if self.op == ">=":
            return cmp >= 0
        if self.op == ">":
            return cmp > 0
        if self.op == "<=":
            return cmp <= 0
        if self.op == "<":
            return cmp < 0
        raise AssertionError(f"unreachable operator {self.op!r}")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class Range:
    """A conjunction of clauses. `MOS-REG-062`: "clauses are ANDed"."""

    text: str
    clauses: tuple[Clause, ...]

    def matches(self, version: Version | str) -> bool:
        v = parse_version(version) if isinstance(version, str) else version
        if v.is_prerelease and not self._names_prerelease_of(v):
            # `MOS-REG-064`, verbatim: "Prerelease versions MUST NOT satisfy a range
            # unless the range names a prerelease in the same (major, minor, patch)
            # tuple." Without this, `>=3.2 <4` silently ships `3.5.0-rc.1` to a clinic.
            return False
        return all(c.matches(v) for c in self.clauses)

    def _names_prerelease_of(self, v: Version) -> bool:
        return any(
            c.prerelease != ""
            and (c.major, c.minor, c.patch) == (v.major, v.minor, v.patch)
            for c in self.clauses
        )

    @property
    def exact_version(self) -> Version | None:
        """The one version this range admits, when it is a full-precision exact pin.

        `MOS-REG-056` needs this: an exact pin that names a `SUSPENDED` or `RECALLED`
        version must yield `pinned_version_suspended` / `pinned_version_recalled`, which
        is a different sentence from "your range matched nothing".
        """
        if len(self.clauses) != 1:
            return None
        c = self.clauses[0]
        if c.op == "=" and c.precision == 3:
            return c.floor
        return None


_REJECTED: Final[dict[str, str]] = {
    "*": "`*` is not in the grammar (MOS-REG-063); omit the pin instead",
    "x": "`x` is not in the grammar (MOS-REG-063); omit the pin instead",
    "X": "`X` is not in the grammar (MOS-REG-063); omit the pin instead",
    "latest": "`latest` is not in the grammar (MOS-REG-063); omit the pin instead",
}


def parse_range(text: str) -> Range:
    """`MOS-REG-062`'s grammar, and nothing else. Raises `RangeSyntaxError`.

    The rejections of `MOS-REG-063` are separate messages rather than one "bad range",
    because "`*` is not supported, omit the pin" is actionable and "parse error" is not.
    """
    if not isinstance(text, str) or text.strip() == "":
        raise RangeSyntaxError(
            "an empty range is rejected (MOS-REG-063): a caller wanting any version "
            "omits the pin entirely"
        )
    raw = text.strip()
    if "||" in raw:
        raise RangeSyntaxError(
            "disjunction (`||`) MUST NOT be supported (MOS-REG-063)"
        )
    clauses: list[Clause] = []
    for token in raw.split():
        if token in _REJECTED:
            raise RangeSyntaxError(_REJECTED[token])
        op = "="
        rest = token
        for candidate in OPERATORS:
            if token.startswith(candidate):
                op = candidate
                rest = token[len(candidate) :]
                break
        m = _PARTIAL_RE.match(rest)
        if m is None:
            raise RangeSyntaxError(
                f"{token!r} is not `op? partial` (MOS-REG-062); "
                f"the grammar admits {', '.join(OPERATORS)} and a dotted partial"
            )
        precision = 1 + (m["minor"] is not None) + (m["patch"] is not None)
        clauses.append(
            Clause(
                op=op,
                major=int(m["major"]),
                minor=int(m["minor"] or 0),
                patch=int(m["patch"] or 0),
                prerelease=m["pre"] or "",
                precision=precision,
            )
        )
    if not clauses:  # pragma: no cover - `raw` is non-empty, so split() is non-empty
        raise RangeSyntaxError("an empty range is rejected (MOS-REG-063)")
    return Range(text=raw, clauses=tuple(clauses))


def validate_range(text: str) -> Range:
    """The admission-time call of `MOS-REG-062`: parse now, so dispatch cannot fail later.

    `MOS-REG-065` is a separate, stricter rule the deployment surface applies on top of
    this one: a `clinical_use_mode = clinical` deployment MUST express at least a `^` or
    `~` bound, so an unconstrained pin is refused where the deployment is created. That
    check belongs to the Deployment component; this function is the grammar half.
    """
    return parse_range(text)
