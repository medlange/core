# SPDX-License-Identifier: Apache-2.0
"""A documented command that does not run is worse than an undocumented one.

REGISTER ENTRY 121 IS THIS DEFECT, AND IT SHIPPED. When `medos/` became a product
directory the editable install broke, and with it `python -m medos.cli doctor`,
`python -m medos.security.cli issue` and `python -m medos.gateway.reconcile` -- all three
documented in README.md, all three answering `ModuleNotFoundError` from the repository
root. Nothing caught it: the image built, because under `/app` the layout is different, and
the suite passed, because `pythonpath` puts the package on the path without the install
being correct. That entry ends "**Found by running a documented command as a user would.**"
This module is that reading, done mechanically and on every run.

IT CHECKS ADDRESSES, NOT OUTCOMES, and the distinction is the whole design. That
`python -m medos.cli doctor` resolves says the module imports; it says nothing about what
doctor reports, and it cannot, because most of these commands need a deployment. An address
is the half that is checkable without a running stack, and it is the half entry 121 was
about.

WHAT IT DELIBERATELY DOES NOT READ. `docs/spec/` and `docs/releases/`. A command in the
specification is a normative example and a command in a release record is EVIDENCE of what
was run at a tag -- `docs/README.md` says a path in those files is not an address. Checking
them would demand that history resolve against today's tree, which is the failure that
rewrote two release records twice in one day. The specification is reached only by
`test_every_cli_the_specification_says_must_ship_is_reachable` below, which reads its
MUST-ship claims rather than its examples.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"

#: Documentation trees whose commands are addresses a reader types.
SKIP = ("docs/spec", "docs/releases", "node_modules", ".git", ".venv")

#: A fence that opens a block of commands. An untagged fence is prose, a diagram or a
#: response body in this repository, and treating it as commands is how the first draft of
#: this module tried to resolve the word "The".
FENCE = re.compile(r"^```(bash|sh|console|shell)\s*$")

#: Console commands the documentation names as though a reader could type them. A name here
#: must be declared in `[project.scripts]`, or be listed in NOT_SHIPPED with its reason.
CONSOLE_NAMES = ("medicalos-verify", "medicalos-conformance", "medos")

#: CLIs a `MUST ship` requirement names that the platform does NOT ship. FROZEN, in the
#: shape `tests/unit/test_gate_contract.py` uses for a check it may not implement: an
#: unmet MUST recorded here is visible and cannot grow without a test failure, where an
#: unmet MUST recorded nowhere is indistinguishable from a satisfied one.
NOT_SHIPPED: dict[str, str] = {
    "medicalos-conformance": (
        "MOS-TEST-047 makes it a MUST -- 'a self-contained CLI a third-party vendor runs "
        "against their own image without access to the platform' -- and §14.8 acceptance "
        "criterion 17 asserts its behaviour ('medicalos-conformance run on the reference "
        "service image produces a report with K1-K10 all pass'). Measured: the string "
        "appears in no Python file in the repository. Register entry 126."
    ),
}


def _docs() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.md")
        if not any(s in p.as_posix() for s in SKIP)
    )


DOCS = _docs()


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")


def _command_lines(path: Path) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    inside = False
    for n, line in enumerate(_text(path).split("\n"), start=1):
        if line.startswith("```"):
            inside = bool(FENCE.match(line)) if not inside else False
            continue
        if not inside:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        out.append((n, stripped))
    return out


def _all_commands() -> list[tuple[Path, int, str]]:
    return [(d, n, line) for d in DOCS for n, line in _command_lines(d)]


COMMANDS = _all_commands()


def _scripts() -> dict[str, str]:
    data = tomllib.loads(_text(PYPROJECT))
    return data.get("project", {}).get("scripts", {})


def test_the_documentation_holds_commands_to_check() -> None:
    """The corpus is non-empty, so a green run cannot mean the extraction broke.

    Every check below is a loop over `COMMANDS`. A fence-matching bug that emptied it would
    turn every one of them green, which is the shape of the skip-heavy pass this project
    has been caught by four times.
    """
    assert len(COMMANDS) >= 40, (
        f"only {len(COMMANDS)} command line(s) extracted from {len(DOCS)} documents; the "
        "fence matcher is broken, not the documentation"
    )
    verbs = {line.split()[0] for _, _, line in COMMANDS}
    assert {"python", "pytest", "docker"} <= verbs, (
        f"the extracted verbs are {sorted(verbs)[:12]}; the three the documentation "
        "actually uses are missing, so the extraction is reading the wrong lines"
    )


def test_every_documented_python_module_imports() -> None:
    """`python -m <module>`. This is the exact form register entry 121 broke."""
    bad: list[str] = []
    checked = 0
    for doc, n, line in COMMANDS:
        for module in re.findall(r"python(?:3|\.exe)?\s+-m\s+([\w.]+)", line):
            checked += 1
            try:
                found = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                bad.append(f"{doc.relative_to(ROOT).as_posix()}:{n}  python -m {module}")
    assert checked, "no `python -m` command was extracted; the matcher is broken"
    assert not bad, (
        f"{len(bad)} of {checked} documented `python -m` command(s) name a module that "
        f"does not import:\n  " + "\n  ".join(bad) + "\n\nThis is register entry 121: "
        "`pip install -e .` broke and every documented command went with it, and the "
        "defect was found by a person typing one."
    )


def test_every_documented_file_path_exists() -> None:
    """Script paths, compose files and pytest targets a command names."""
    patterns = (
        r"python(?:3|\.exe)?\s+((?:\./)?[\w./-]+\.py)\b",
        r"-f\s+((?:\./)?[\w./-]+\.ya?ml)\b",
        r"pytest\s+((?:[\w./-]+/)+[\w./-]*)",
    )
    bad: list[str] = []
    checked = 0
    for doc, n, line in COMMANDS:
        for pattern in patterns:
            for raw in re.findall(pattern, line):
                target = raw.replace("\\", "/").lstrip("./")
                if not target or target.startswith("$") or "*" in target:
                    continue
                # Sibling product trees (viewer/, trainer/) are legitimate
                # documentation targets in the monorepo and absent from a
                # split checkout; existence of THOSE paths is their own
                # repos' business, not this gate's.
                head = target.split("/", 1)[0]
                if head in ("viewer", "trainer") and not (ROOT / head).is_dir():
                    continue
                checked += 1
                if not (ROOT / target).exists():
                    bad.append(f"{doc.relative_to(ROOT).as_posix()}:{n}  {raw}")
    assert checked, "no file-bearing command was extracted; the matcher is broken"
    assert not bad, (
        f"{len(bad)} of {checked} documented path(s) do not exist:\n  " + "\n  ".join(bad)
    )


def test_no_document_types_a_console_command_that_pip_does_not_install() -> None:
    """A bare command name in a fenced block promises something `pip install` provides.

    `pyproject.toml` declares no `[project.scripts]` at all, so nothing lands on PATH.
    Measured today, no document types one either: `medos` is documented as
    `python -m medos.cli`, and `medicalos-verify` as
    `python medos/tools/medicalos_verify.py` -- a directory pyproject explicitly excludes
    from the platform, which is its own question and not this one's.

    The empty case is ASSERTED rather than skipped. Three parametrised cases that all skip
    is a test that looks like coverage and is not, which is the shape this project has lost
    coverage to four times.
    """
    scripts = _scripts()
    typed = {
        name: [
            f"{d.relative_to(ROOT).as_posix()}:{n}"
            for d, n, line in COMMANDS if line.split()[0] == name
        ]
        for name in CONSOLE_NAMES
    }
    offenders = {
        name: sites for name, sites in typed.items()
        if sites and name not in scripts and name not in NOT_SHIPPED
    }
    assert not offenders, (
        f"{sorted(offenders)} are typed as commands and `[project.scripts]` declares "
        f"none of them, so `pip install -e .` puts nothing on PATH: {offenders}. Declare "
        "them, rewrite the documentation to the form that works, or record them in "
        "NOT_SHIPPED with a register entry."
    )
    if not any(typed.values()):
        assert not scripts, (
            "no document types a console command, yet `[project.scripts]` now declares "
            f"{sorted(scripts)}. Either the documentation has stopped telling readers "
            "about an installed command, or this check is watching the wrong names."
        )


def test_every_cli_the_specification_says_must_ship_is_reachable() -> None:
    """`MOS-TEST-047`: "The platform MUST ship `medicalos-conformance`".

    Measured: that string appears in no Python file in the repository. §14.8 acceptance
    criterion 17 nonetheless asserts what it produces. An unmet MUST recorded nowhere is
    indistinguishable from a satisfied one, so it is recorded in NOT_SHIPPED and here.
    """
    spec = ROOT / "docs" / "spec"
    claimed: dict[str, str] = {}
    for chapter in sorted(spec.glob("[0-9][0-9]-*.md")):
        head = _text(chapter).split("\n", 1)[0]
        if "Non-normative" in head:
            # THE APPENDIX IS IN THIS GLOB AND MUST NOT BE IN THIS SCAN.
            # `99-known-inconsistencies.md` matches `[0-9][0-9]-*.md`, its first line reads
            # "appendix. Non-normative.", and register entry 126 QUOTES this requirement
            # verbatim -- "The platform MUST ship `medicalos-conformance`". So the phrase was
            # found in two files, and if chapter 14 ever softens the requirement, the check
            # would go on demanding the CLI on the authority of a non-normative entry
            # DESCRIBING the requirement that had just been withdrawn. That is the shape four
            # register entries record: a gate keyed to a substring forbids the paragraph that
            # retracts the substring. The filter reads the file's own marker rather than its
            # number, because a second appendix would have the same problem and not the same
            # name.
            continue
        for m in re.finditer(r"MUST ship `([a-z][a-z0-9-]+)`", _text(chapter)):
            claimed[m.group(1)] = chapter.name
    assert claimed, "no `MUST ship` requirement was found; the matcher is broken"
    assert not any(n.startswith("99-") for n in claimed.values()), (
        "a `MUST ship` claim was taken from the non-normative appendix"
    )

    scripts = _scripts()
    unreachable = {
        name: chapter for name, chapter in claimed.items()
        if name not in scripts
        and importlib.util.find_spec(name.replace("-", "_")) is None
    }
    undeclared = sorted(set(unreachable) - set(NOT_SHIPPED))
    assert not undeclared, (
        f"the specification says the platform MUST ship {undeclared}, nothing ships them, "
        f"and NOT_SHIPPED does not record them: "
        f"{ {n: unreachable[n] for n in undeclared} }"
    )

    stale = sorted(set(NOT_SHIPPED) - set(unreachable))
    assert not stale, (
        f"NOT_SHIPPED records {stale} as unshipped, and they are now reachable. Delete "
        "the entry and close its register entry in the same change."
    )


def test_not_shipped_names_a_register_entry_for_each_unmet_must() -> None:
    """An unmet MUST is a defect of the platform, and this repository records those."""
    register = _text(ROOT / "docs" / "spec" / "99-known-inconsistencies.md")
    for name, reason in NOT_SHIPPED.items():
        m = re.search(r"[Rr]egister entry (\d+)", reason)
        assert m, f"NOT_SHIPPED[{name!r}] names no register entry"
        assert re.search(rf"^{m.group(1)}\. \S", register, re.M), (
            f"NOT_SHIPPED[{name!r}] cites register entry {m.group(1)}, which the register "
            "does not hold"
        )
        assert name in register, (
            f"register entry {m.group(1)} does not name {name!r}, so a reader following "
            "the citation lands on an entry about something else"
        )


# ======================================================================================
# Compose services named in a command a reader is told to type
# ======================================================================================
#: THE FIRST SWEEP OF THIS SESSION HAD THIS RIGHT AND I OVERRODE IT WITH A GREP. A scratch
#: survey reported `ohif` as a compose service named in documentation and absent from the
#: compose file. I then grepped for `up ohif` / `restart ohif` / `logs ohif`, found only
#: prose about the withdrawal, and concluded it was a false positive. The real site was
#: `docker compose ... up -d --force-recreate ohif`, the last line of
#: `medos/web/ohif-extension/README.md` §5.1 -- a LIVE credential-granting procedure whose
#: final command exits "no such service", because the service that serves that origin has
#: been `web` running nginx since OHIF was withdrawn. My pattern decided the answer and the
#: enumeration had been right.
COMPOSE_FILE = ROOT / "medos" / "deploy" / "compose" / "docker-compose.yml"

#: Words that follow `docker compose <verb>` without being a service name.
_NOT_A_SERVICE = frozenset({
    "-d", "-f", "-v", "-t", "--build", "--wait", "--detach", "--force-recreate",
    "--remove-orphans", "--profile", "--tail", "--follow", "--no-deps", "--rm",
    "--wait-timeout", "--", "up", "down", "run", "exec", "logs", "restart", "stop",
    "start", "ps", "build", "config", "pull", "kill",
})


def _compose_services() -> set[str]:
    import yaml

    doc = yaml.safe_load(_text(COMPOSE_FILE))
    return set((doc or {}).get("services", {}))


def _services_named_in_commands() -> dict[str, list[str]]:
    """document -> compose service names a fenced command applies a verb to."""
    verbs = ("up", "run", "exec", "logs", "restart", "stop", "start", "kill", "build")
    out: dict[str, list[str]] = {}
    for doc, _, line in COMMANDS:
        if "compose" not in line:
            continue
        tokens = line.split()
        for i, token in enumerate(tokens):
            if token not in verbs:
                continue
            for word in tokens[i + 1:]:
                if word in _NOT_A_SERVICE or word.startswith("-"):
                    continue
                if not re.fullmatch(r"[a-z][a-z0-9-]*", word):
                    break
                out.setdefault(doc.relative_to(ROOT).as_posix(), []).append(word)
                break
    return out


def test_every_compose_service_a_command_names_exists() -> None:
    declared = _compose_services()
    assert declared, "the compose file declares no services; the reader is broken"
    named = _services_named_in_commands()
    assert named, (
        "no compose service was extracted from any documented command; the matcher is "
        "broken, not the documentation"
    )
    ghosts = {
        doc: sorted({s for s in services if s not in declared})
        for doc, services in named.items()
    }
    ghosts = {k: v for k, v in ghosts.items() if v}
    assert not ghosts, (
        f"documented commands name compose service(s) the file does not declare: {ghosts}. "
        f"Declared: {sorted(declared)}. This is how `docker compose ... ohif` survived the "
        "OHIF withdrawal inside a live procedure."
    )


# ---------------------------------------------------------------------------------------
# THE ONE DOCUMENT THE PLATFORM GENERATES, AND THE SHIM THAT SERVES IT
# ---------------------------------------------------------------------------------------
#
# This module reads the markdown a reader can browse. It does not read the README the
# platform WRITES INTO EVERY EXPORTED EVIDENCE BUNDLE, and that README is the only
# instruction its reader has: `MOS-CONF-304` makes them a third party who "does not get a
# login", and `MOS-EVID-122` requires the archive to be verifiable with no network and no
# MedicalOS instance. Register entry 126 measured the documentation and missed this file.
#
# Two things were wrong there and one was not.
#
# NOT WRONG: the README prints `medicalos-verify report ...`. `MOS-EVID-122` requires
# README.txt to carry the "verification command" and `MOS-EVID-123` prints exactly that form,
# so the README is quoting the specification. That the command ships nowhere is entry 126's
# defect, not this file's, and editing the README to match the implementation would be
# amending a normative example from the wrong end.
#
# WRONG: it said "Everything needed to check it is inside this archive". REQUIRED_MEMBERS
# lists fourteen files and not one of them is a program, so the sentence was false about the
# one thing its reader has to go and find.
#
# ALSO WRONG, in code: `medos/tools/medicalos_verify.py`'s shim, whose comment reads "run from
# a checkout without installation", inserted `medos/` and not the repository root. MEASURED:
# `python -E -S medos/tools/medicalos_verify.py --help` died with `ModuleNotFoundError: No
# module named 'medos.sdk'`, which `medos/medos/evidence/__init__.py` imports.
# The one path the shim exists to serve was the one path it did not serve.

VERIFIER = ROOT / "medos" / "tools" / "medicalos_verify.py"

#: Packages this repository ships. The shim's job is to make every one of them importable
#: without an install, so an import error naming one of these is the shim's failure. An import
#: error naming a THIRD-PARTY package is not: `numpy` is a declared dependency and a site-less
#: interpreter has none, which is what makes this check arrangeable at all.
OWN_PACKAGES = ("medos", "medos.sdk", "services", "tools")


def test_the_verifier_shim_reaches_every_package_this_repository_ships() -> None:
    """Run it the way its own comment promises, on an interpreter with no site packages.

    `-S` is what makes this a real test rather than a tautology: this checkout carries an
    editable install whose finder maps `medos` and `medos.sdk` to the tree, so
    on any ordinary interpreter the shim appears to work whatever it puts on `sys.path`. With
    no site directory there is no finder, and the only roots are the ones the shim adds.
    """
    result = subprocess.run(
        [sys.executable, "-E", "-S", str(VERIFIER), "--help"],
        cwd=ROOT, capture_output=True, text=True, errors="replace",
    )
    blame = [
        pkg for pkg in OWN_PACKAGES
        if f"No module named '{pkg}'" in result.stderr
    ]
    assert not blame, (
        f"{VERIFIER.relative_to(ROOT)} says it can be run from a checkout without "
        f"installation, and it cannot reach {blame} -- packages this repository ships. Its "
        f"`sys.path` roots must match `tests/_support/roots.py`'s IMPORT_ROOTS, which is the "
        f"authority on what a child needs.\n  stderr tail: "
        + " ".join(result.stderr.strip().splitlines()[-2:])
    )


def test_the_exported_bundles_readme_does_not_claim_to_carry_the_verifier() -> None:
    """The pair: what the archive holds, and what its README says it holds.

    Asserted in both directions, because either half alone is satisfiable by a lie. If a
    future change puts the verifier INTO the archive, the members list gains a program and
    this test fails, which is the moment the README's sentence should change back.
    """
    source = (ROOT / "medos" / "medos" / "evidence" / "bundle.py").read_text(encoding="utf-8")
    sys.path.insert(0, str(ROOT / "medos"))
    from medos.evidence.bundle import REQUIRED_MEMBERS  # noqa: PLC0415

    programs = [m for m in REQUIRED_MEMBERS if m.endswith((".py", ".sh", ".exe"))]
    assert not programs, (
        f"the bundle now carries {programs}, so the README's statement that the verifier is "
        "not a member has gone false. Change the README in the same commit."
    )
    assert "The verifier itself is not a member of the archive" in source, (
        "the generated README no longer tells its reader that the verifier is not in the "
        "archive. REQUIRED_MEMBERS holds fourteen files and no program, and that reader has "
        "no other instruction -- MOS-CONF-304 says they do not get a login."
    )
    assert "Everything needed to check it is" not in source, (
        "the generated README has gone back to claiming everything needed is inside the "
        "archive. The verifier is not, and it is the one thing the reader has to obtain."
    )
