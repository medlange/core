# SPDX-License-Identifier: Apache-2.0
"""The MedicalOS skip taxonomy: one home for every reason a test does not run.

WHY THIS FILE EXISTS
--------------------
Twice on this project a suite reported green while the system was broken:

  * 74 of 95 integration tests skipped on ``no Postgres at 127.0.0.1:55433`` -- a port
    that no longer existed. pytest printed ``21 passed`` and exited 0.
  * all 17 e2e tests skipped on ``the compose stack is not up (medos-api -> HTTP 503)``.
    The full run printed ``242 passed, 17 skipped`` and exited 0, while the end-to-end
    path was dead.

Both runs were honest about the *count* and silent about the *meaning*. ``17 skipped`` is
not information. ``INFRA SKIPS: 17 (medos-api: 11, orthanc: 4, docker: 2)`` is.

THE TAXONOMY
------------
``skip_infra(reason, *, dependency)``
    A dependency that SHOULD be reachable is not: Postgres, Orthanc's DICOMweb or its
    native REST API, the compose stack, the docker CLI. On a developer laptop this is a
    skip; under ``--require-stack`` it is a FAILURE, because a CI job that cannot reach
    its own stack has not tested anything and must not be green.

``skip_no_data(reason, *, corpus)``
    Optional test data is absent -- the LCTSC/TCIA corpus is large, private and not
    redistributable. A contributor without it must still be able to run the suite, so
    this stays a skip even under ``--require-stack``. The separate ``--require-corpus``
    switch turns it into a failure, for the nightly job that DOES mount the corpus.

``skip_environment(reason, *, detail)``
    Neither of the above: a legitimate caveat about the machine the test is running on
    that no amount of provisioning fixes -- an origin that answers on a URL the test
    needed to be a 404, a race the harness cannot win on a fast host. Always a skip,
    under every switch, but still REPORTED so it cannot hide.

TWO SWITCHES, NOT ONE
---------------------
Conflating "the stack is down" with "the private corpus is not mounted" is what forces
people to turn the whole thing off. ``--require-stack`` / ``MEDOS_REQUIRE_STACK=1`` is
for every CI run. ``--require-corpus`` / ``MEDOS_REQUIRE_CORPUS=1`` is for the nightly
that has F:/WorkSpace/PulmoAI/TCIA mounted. They are independent.

HOW THE REPORT IS BUILT
-----------------------
Each helper tags its reason string with a machine-readable ``[medos-skip kind:key]``
prefix and then calls ``pytest.skip`` (or ``pytest.fail``) as usual. The summary is
assembled in ``pytest_runtest_logreport`` by parsing that tag back out of the report,
NOT by counting calls to the helpers. That distinction is load-bearing: a session- or
module-scoped fixture that skips runs its body ONCE and skips N tests, and it is the N
that the reader needs to see.

THREE THINGS AN ADVERSARIAL PASS FOUND, AND WHAT NOW STOPS THEM
---------------------------------------------------------------
1. ``--require-stack`` only ever reacted to a skip, so a dependency that NO test
   classifies was invisible to it. With medos-gateway, medos-worker, ohif, minio, triton
   and the tritond all stopped -- a platform that cannot fetch one DICOM instance --
   ``pytest ... --require-stack`` printed ``233 passed`` and ``INFRA SKIPS: 0``.
   ``tests/_support/stack.py`` now holds a probe per compose service and a per-module
   declaration of which ones each suite needs; strict mode PROBES them and aborts before
   a test is allowed to pass. A status code is not accepted as proof -- see that file.

2. A skip taken at COLLECTION time (``pytest.skip(allow_module_level=True)``,
   ``pytest.importorskip``) produced no run report, so it never reached ``_STORE``. Two
   whole modules could vanish under ``pytest --require-stack`` while this block printed
   ``OTHER SKIPS: 0``: the summary positively asserted that nothing had gone untested.
   ``pytest_collectreport`` now records those, and strict mode fails on them.

3. A run in which nothing ran at all reported nothing at all. A marker that matches no
   test, a path that holds no pytest-style test: the taxonomy block printed all zeros,
   which reads exactly like a clean run. The block now always states how many tests were
   collected, deselected and actually ran, and under ``--require-stack`` a session that
   ran no tests is a failure rather than a quiet exit.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
from typing import Any, NoReturn

import pytest

from tests._support import stack as _stack

__all__ = [
    "skip_infra",
    "skip_no_data",
    "skip_environment",
    "skipif_no_data",
    "require_stack",
    "require_corpus",
]

# --------------------------------------------------------------------------------------
# Tagging
# --------------------------------------------------------------------------------------
KIND_INFRA = "infra"
KIND_DATA = "data"
KIND_ENV = "env"

#: Not a skip and not tagged like one. pytest reports an `xfail` through the SAME
#: `report.skipped` channel as a skip, so until this existed the first `xfail` marks in
#: this repository -- the witnesses for register entries 111 and 112 -- landed in
#: `OTHER SKIPS: 2 (not yet classified)` beside a reason line reading `@pytest.mark.xfail(`,
#: which is the decorator's source text rather than anything a reader can act on.
#:
#: An xfail here is a DELIBERATE, NAMED RECORD of a defect that is still in the tree, and
#: it is the opposite of an unclassified skip: a skip is a question nobody asked, an
#: `xfail(strict=True)` is a question asked every run whose answer is expected to be "still
#: broken" and which FAILS THE SUITE the day it changes. Printing them together would
#: erode exactly the signal this module exists to protect.
KIND_XFAIL = "xfail"

_SKIP_TAG = "medos-skip"
_STRICT_TAG = "medos-strict"
_TAG_RE = re.compile(r"\[(?:medos-skip|medos-strict) (infra|data|env):([^\]]+)\]")


def _tagged(kind: str, key: str, reason: str, *, strict: bool = False) -> str:
    marker = _STRICT_TAG if strict else _SKIP_TAG
    return f"[{marker} {kind}:{key}] {reason}"


# --------------------------------------------------------------------------------------
# How to start each dependency. The message that names a dependency and does not say how
# to get it is the message people learn to ignore.
# --------------------------------------------------------------------------------------
_COMPOSE = "docker compose -f medos/deploy/compose/docker-compose.yml"

START_HINTS: dict[str, str] = {
    "postgres": f"{_COMPOSE} up -d postgres",
    "orthanc": f"{_COMPOSE} up -d orthanc",
    "orthanc-e2e": f"{_COMPOSE} up -d orthanc",
    "orthanc-rest": f"{_COMPOSE} up -d orthanc",
    "minio": f"{_COMPOSE} up -d minio",
    "triton": f"{_COMPOSE} up -d triton",
    "medos-tritond": f"{_COMPOSE} up -d medos-tritond",
    # The harness reaches DICOM through the Gateway, not through the PACS: MOS-DATA-006
    # leaves `orthanc` no host port and MOS-DATA-002 makes `medos-gateway` the only route
    # in (register entry 70). So the command that fixes an unreachable DICOMweb root is
    # the one that starts the Gateway, and the variable to point elsewhere names the
    # Gateway's tenant-scoped root.
    "dicomweb": (
        f"{_COMPOSE} up -d --build medos-gateway orthanc "
        "(and point MEDOS_DICOMWEB_URL at it if it is not on "
        "127.0.0.1:8043/dicomweb/<tenant>)"
    ),
    "medos-api": f"{_COMPOSE} up -d --build",
    "medos-worker": f"{_COMPOSE} up -d --build",
    "medos-gateway": f"{_COMPOSE} up -d --build",
    "ohif": f"{_COMPOSE} up -d --build",
    "compose-stack": f"{_COMPOSE} up -d --build",
    "docker": "start Docker Desktop / the docker daemon, then " + f"{_COMPOSE} up -d --build",
    # NOT a compose service. `dciodvfy` is dicom3tools, the external structural validator
    # MOS-TEST-026 names, and it is the one arm of the release-0.1.0 `dicom-battery` that
    # a machine can legitimately be missing. The hint is a command because a message that
    # names a dependency and does not say how to get it is the message people ignore.
    "dciodvfy": (
        "install dicom3tools (Debian/Ubuntu: `apt-get install dicom3tools`), or build the "
        "container the battery falls back to:\n"
        "             printf 'FROM python:3.11-slim\\nRUN apt-get update && apt-get "
        "install -y --no-install-recommends dicom3tools\\n' | "
        "docker build -t medos-dicom3tools:local -\n"
        "             (override the image name with MEDOS_DCIODVFY_IMAGE)"
    ),
}

_DEFAULT_HINT = f"{_COMPOSE} up -d --build"

CORPUS_HINTS: dict[str, str] = {
    "recorded-fixtures": (
        "tests/_recorded/ holds recordings produced FROM PATIENT STUDIES, which is why "
        ".gitignore excludes the directory and why it is in no clone. Mount a copy, or "
        "regenerate it against the LCTSC tree. Until then the byte-identity checks have "
        "nothing to compare against."
    ),
    "lctsc-corpus": (
        "mount the LCTSC/TCIA tree and set MEDOS_E2E_LCTSC_ROOT "
        "(default F:/WorkSpace/PulmoAI/TCIA). The corpus is private and is never "
        "redistributed with this repository."
    ),
}

_DEFAULT_CORPUS_HINT = "mount the corpus and point the documented env var at it"


# --------------------------------------------------------------------------------------
# Strict-mode state
#
# Seeded from the environment at import time so the helpers behave correctly even if this
# module is used outside a pytest run, then overwritten in `pytest_configure` from the
# parsed command line. `pytest_configure` runs BEFORE test modules are imported, so a
# module-level `skipif_no_data(...)` decorator already sees the final value.
# --------------------------------------------------------------------------------------
def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


_require_stack: bool = _env_flag("MEDOS_REQUIRE_STACK")
_require_corpus: bool = _env_flag("MEDOS_REQUIRE_CORPUS")


def require_stack() -> bool:
    """True when infrastructure skips must fail instead."""
    return _require_stack


def require_corpus() -> bool:
    """True when absent-corpus skips must fail instead."""
    return _require_corpus


# --------------------------------------------------------------------------------------
# The three helpers
# --------------------------------------------------------------------------------------
def skip_infra(reason: str, *, dependency: str) -> NoReturn:
    """A dependency that should be reachable is not.

    Skips by default. Under ``--require-stack`` / ``MEDOS_REQUIRE_STACK=1`` this FAILS,
    with a message naming the dependency and the command that starts it.
    """
    if _require_stack:
        hint = START_HINTS.get(dependency, _DEFAULT_HINT)
        pytest.fail(
            _tagged(
                KIND_INFRA,
                dependency,
                f"required dependency {dependency!r} is unavailable, and --require-stack "
                f"(MEDOS_REQUIRE_STACK=1) forbids skipping it.\n"
                f"  reason:   {reason}\n"
                f"  start it: {hint}\n"
                f"  Drop --require-stack to run the suite without this dependency.",
                strict=True,
            ),
            pytrace=False,
        )
    pytest.skip(_tagged(KIND_INFRA, dependency, reason))


def skip_no_data(reason: str, *, corpus: str) -> NoReturn:
    """Optional, non-redistributable test data is absent.

    Skips by default AND under ``--require-stack``: absent private data is not a broken
    system. Only ``--require-corpus`` / ``MEDOS_REQUIRE_CORPUS=1`` turns it into a
    failure, for the nightly job that mounts the corpus.
    """
    if _require_corpus:
        hint = CORPUS_HINTS.get(corpus, _DEFAULT_CORPUS_HINT)
        pytest.fail(
            _tagged(
                KIND_DATA,
                corpus,
                f"corpus {corpus!r} is absent, and --require-corpus "
                f"(MEDOS_REQUIRE_CORPUS=1) forbids skipping it.\n"
                f"  reason:   {reason}\n"
                f"  mount it: {hint}",
                strict=True,
            ),
            pytrace=False,
        )
    pytest.skip(_tagged(KIND_DATA, corpus, reason))


def skip_environment(reason: str, *, detail: str) -> NoReturn:
    """A caveat about this machine that no provisioning fixes.

    Always a skip, under every switch -- but it is counted and printed, so that a run
    which quietly stopped asserting anything still says so out loud.
    """
    pytest.skip(_tagged(KIND_ENV, detail, reason))


def skipif_no_data(condition: bool, reason: str, *, corpus: str) -> Any:
    """The marker form of :func:`skip_no_data`, for whole-test decoration.

    ``pytest.mark.skipif`` cannot be made to fail under ``--require-corpus``, so this
    returns a MedicalOS marker that ``pytest_runtest_setup`` below routes through
    :func:`skip_no_data` -- one code path, both switches honoured.
    """
    return pytest.mark.medos_no_data(bool(condition), reason, corpus)


# ======================================================================================
# The pytest plugin: options, marker routing and the session report.
#
# Registered from the repository-root conftest.py via `pytest_plugins`, so the switches
# exist no matter which subdirectory of tests/ is being run.
# ======================================================================================
def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("medos", "MedicalOS skip taxonomy")
    group.addoption(
        "--require-stack",
        action="store_true",
        default=False,
        help=(
            "Turn every infrastructure skip into a failure. Postgres, Orthanc, the "
            "compose stack and docker must all be reachable. Also MEDOS_REQUIRE_STACK=1."
        ),
    )
    group.addoption(
        "--require-corpus",
        action="store_true",
        default=False,
        help=(
            "Turn every absent-test-data skip into a failure. For the nightly job that "
            "mounts the LCTSC/TCIA corpus. Also MEDOS_REQUIRE_CORPUS=1."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    global _require_stack, _require_corpus
    _require_stack = bool(config.getoption("--require-stack")) or _env_flag(
        "MEDOS_REQUIRE_STACK"
    )
    _require_corpus = bool(config.getoption("--require-corpus")) or _env_flag(
        "MEDOS_REQUIRE_CORPUS"
    )
    config.addinivalue_line(
        "markers",
        "medos_no_data(absent, reason, corpus): skip (or, under --require-corpus, fail) "
        "when optional non-redistributable test data is missing",
    )
    _STORE.clear()
    _COUNTS.clear()
    _PREFLIGHT.clear()


def pytest_runtest_setup(item: pytest.Item) -> None:
    for mark in item.iter_markers("medos_no_data"):
        absent, reason, corpus = mark.args
        if absent:
            skip_no_data(reason, corpus=corpus)


# ======================================================================================
# The stack preflight.
#
# `--require-stack` used to mean only "do not skip". That covers a dependency some test
# already reaches for; it covers nothing at all for one that no test touches. The
# adversarial run that motivated this stopped six of the ten compose services -- among
# them medos-gateway, which is the worker's ONLY route to DICOM -- and the strict suite
# reported 233 passed with INFRA SKIPS: 0.
#
# So strict mode now probes. The dependency set is not "everything in docker-compose.yml"
# but what the SELECTED tests declare (tests/_support/stack.py, SUITE_DEPENDENCIES), at
# module granularity: `pytest tests/unit --require-stack` must stay green with no
# containers at all, because a unit test that needs a container is in the wrong
# directory, and a switch that goes red on a correct run is a switch people delete.
# ======================================================================================
_PREFLIGHT: dict[str, tuple[bool, str, list[str]]] = {}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    # `trylast`: `-m`/`-k` deselection happens in this same hook, and probing must see
    # the surviving selection. Probing a dependency of a test that was deselected is a
    # false red, and a false red is how a switch like this one gets deleted.
    declared = _stack.dependencies_for_paths(str(item.path) for item in items)
    if not declared:
        return
    for key in sorted(declared):
        ready, detail = _stack.probe(key)
        _PREFLIGHT[key] = (ready, detail, sorted(set(declared[key])))

    broken = {k: v for k, v in _PREFLIGHT.items() if not v[0]}
    if not broken or not _require_stack:
        return

    lines = [
        "",
        "the stack is not up, and --require-stack (MEDOS_REQUIRE_STACK=1) forbids "
        "running the suite against it.",
        "",
    ]
    for key, (_ready, detail, paths) in sorted(broken.items()):
        dep = _stack.PROBES[key]
        lines += [
            f"  {key}  -- {dep.what}",
            f"      probe:    {detail[:240]}",
            f"      start it: {START_HINTS.get(key, _DEFAULT_HINT)}",
            f"      needed by: {', '.join(paths[:4])}"
            + (f" (+{len(paths) - 4} more)" if len(paths) > 4 else ""),
        ]
    lines += [
        "",
        "  Nothing ran. A green run here would have meant nothing: this is the failure "
        "that two",
        "  previous incidents on this project produced as '242 passed, 17 skipped', "
        "exit 0.",
        "  Drop --require-stack to run whatever does not need these dependencies.",
    ]
    raise pytest.UsageError("\n".join(lines))


# ======================================================================================
# Accounting: what was collected, what was thrown away, what actually ran.
#
# "29 skipped" was never the dangerous number. "0 of 0 ran" is, because it is printed in
# exactly the same green as a clean run.
# ======================================================================================
_COUNTS: dict[str, int] = {}


def _bump(key: str, n: int = 1) -> None:
    _COUNTS[key] = _COUNTS.get(key, 0) + n


def pytest_collection_finish(session: pytest.Session) -> None:
    # `session.items` is the SURVIVING selection: deselection already happened in
    # pytest_collection_modifyitems. So "collected" has to be reconstructed.
    _COUNTS["selected"] = len(session.items)
    _COUNTS["collected"] = len(session.items) + _COUNTS.get("deselected", 0)


def pytest_deselected(items: list[pytest.Item]) -> None:
    _bump("deselected", len(items))


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Record a skip taken during COLLECTION, which produces no run report.

    `pytest.skip(..., allow_module_level=True)` and `pytest.importorskip` remove a whole
    module before any test of it exists. Without this hook such a module was absent from
    `_STORE` entirely and the summary printed `OTHER SKIPS: 0` while pytest printed
    `2 skipped` -- the report contradicting the run, in the direction of "all clear".
    """
    if not report.skipped:
        return
    text = _report_text(report)
    match = _TAG_RE.search(text)
    nodeid = report.nodeid or "(root)"
    key = f"collect:{nodeid}"
    if match is None:
        _STORE.setdefault(key, (None, f"{nodeid}: {_first_line(text)}", "skipped"))
    else:
        _STORE[key] = (match.group(1), match.group(2), "skipped")
    _bump("collect_skipped")


# One row per test node: nodeid -> (kind | None, key-or-raw-reason, outcome). Keyed by
# nodeid so a setup skip plus a teardown report cannot double-count, and so a
# module-scoped fixture that runs its body once but skips eleven tests is reported as
# eleven -- which is the number the reader actually needs.
_STORE: dict[str, tuple[str | None, str, str]] = {}


def _report_text(report: pytest.TestReport) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record one classified row per test node that did not pass."""
    # An `xfail` arrives through `report.skipped` with `wasxfail` carrying the marker's
    # reason. Classified here rather than left to fall through to the unclassified bucket:
    # see KIND_XFAIL. `report.wasxfail` is also set on an XPASS, which pytest reports as
    # `failed` under `strict=True` -- that path is left alone deliberately, because a
    # strict xfail that passed is a REAL failure and belongs in the failure list.
    if report.skipped and hasattr(report, "wasxfail"):
        reason = str(report.wasxfail) or "(no reason given)"
        _STORE[report.nodeid] = (KIND_XFAIL, reason, "skipped")
        return
    if report.skipped:
        text, outcome = _report_text(report), "skipped"
    elif report.failed:
        text, outcome = str(report.longrepr), "failed"
    else:
        return
    match = _TAG_RE.search(text)
    if match is None:
        if outcome == "skipped":
            _STORE.setdefault(report.nodeid, (None, _first_line(text), outcome))
        return
    _STORE[report.nodeid] = (match.group(1), match.group(2), outcome)


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else "(no reason given)"
    if line.startswith("Skipped:"):
        line = line[len("Skipped:") :]
    return line.lstrip(": ").strip() or "(no reason given)"


def _tally(kind: str, outcome: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for k, key, out in _STORE.values():
        if k == kind and out == outcome:
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _render(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if not counts:
        return "0"
    return f"{total} (" + ", ".join(f"{k}: {n}" for k, n in counts.items()) + ")"


def _unsanctioned_collect_skips() -> list[str]:
    """Collection-time skips that ``--require-stack`` must not tolerate.

    An `env` skip is a machine caveat and a `data` skip is the private corpus; both are
    sanctioned by the taxonomy. An `infra` skip or -- much more importantly -- an
    UNTAGGED one is a whole module quietly leaving the run, which is the thing this
    suite has twice been burned by.
    """
    return [
        reason
        for key, (kind, reason, outcome) in _STORE.items()
        if key.startswith("collect:") and outcome == "skipped" and kind in (None, KIND_INFRA)
    ]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refuse to exit 0 on a strict run that proved nothing.

    Two distinct ways a run can be vacuous and still look clean:

      * nothing ran -- an `-m` marker that matches no test, a path that holds no
        pytest-style test -- this repository HAD one, `spikes/week0/test_contracts.py`,
        a script on which `pytest` reported "no tests ran"). pytest's own code for that is
        5, which some CI treats as success and which in any case arrives with a summary
        line that reads like a pass.
      * a whole module skipped during collection, which never produced a run report.

    Under `--require-stack` both are failures, and the exit code says so.
    """
    if not _require_stack:
        return
    ran = _COUNTS.get("selected", 0)
    if ran <= 0:
        _COUNTS["fatal_nothing_ran"] = 1
        session.exitstatus = 1
    if _unsanctioned_collect_skips():
        _COUNTS["fatal_collect_skips"] = 1
        session.exitstatus = 1


def pytest_terminal_summary(
    terminalreporter: Any, exitstatus: int, config: pytest.Config
) -> None:
    """Print the taxonomy summary at the end of EVERY run, including a clean one.

    Printed even when every count is zero: the absence of the block is then itself a
    signal that the plugin did not load, and a zero is a positive statement that nothing
    went untested for want of a dependency.
    """
    write = terminalreporter.write_line
    terminalreporter.write_sep("=", "MedicalOS skip taxonomy", bold=True)

    modes = []
    modes.append("--require-stack ON" if _require_stack else "--require-stack off")
    modes.append("--require-corpus ON" if _require_corpus else "--require-corpus off")
    write("mode: " + ", ".join(modes))

    # Selection accounting FIRST. Every other number below is meaningless without it: a
    # run that collected nothing has the same all-zero taxonomy as a run that tested
    # everything and found nothing wrong.
    collected = _COUNTS.get("collected", 0)
    deselected = _COUNTS.get("deselected", 0)
    ran = _COUNTS.get("selected", 0)
    line = f"SELECTION:   {collected} collected, {deselected} deselected, {ran} ran"
    if ran <= 0:
        line += "   <- NOTHING RAN. This run asserted nothing."
    write(line)

    # WHICH PYTHON RAN THIS, when it is not the project's own.
    #
    # `DEVELOPMENT.md` says to create `.venv` and activate it. A shell that has not -- a
    # fresh non-interactive one, a tool that spawns its own, an IDE pointed at a base
    # install -- resolves `python` to whatever is first on PATH, and the declared
    # dependencies are simply absent there. What comes back is:
    #
    #     ModuleNotFoundError: No module named 'psycopg'
    #
    # which reads exactly like a dependency that lives only in a container, because that is
    # a real and common arrangement in this repository. It is not what happened. Measured
    # here: under a base Anaconda interpreter this suite reported "34 failed, 1059 passed"
    # and 7 collection errors, every one of them that module or `nibabel`; under
    # `.venv/Scripts/python.exe` the same tree reports 1251 passed and nothing skipped.
    # A hundred and fifty-eight tests were not failing. They were not running, and the
    # summary above said so in a way nobody reads as that.
    #
    # The taxonomy exists because "a check that cannot pass and a check that never executes
    # look identical from outside". This is that, one layer down: the runner itself.
    venv = pathlib.Path(__file__).resolve().parents[2] / ".venv"
    if venv.is_dir():
        running_in = pathlib.Path(sys.prefix).resolve()
        if running_in != venv.resolve():
            write(
                f"INTERPRETER: {running_in}   <- NOT {venv}. A declared dependency missing "
                f"here is this, not a container-only module."
            )

    if _PREFLIGHT:
        unready = sorted(k for k, (ok, _d, _p) in _PREFLIGHT.items() if not ok)
        ready = sorted(k for k, (ok, _d, _p) in _PREFLIGHT.items() if ok)
        write(
            f"STACK PROBE: {len(ready)}/{len(_PREFLIGHT)} declared dependencies ready"
            + (f"   <- UNREACHABLE: {', '.join(unready)}" if unready else "")
        )
        for key in unready:
            write(f"  - {key}: {_PREFLIGHT[key][1][:110]}")

    infra = _tally(KIND_INFRA, "skipped")
    data = _tally(KIND_DATA, "skipped")
    env = _tally(KIND_ENV, "skipped")
    infra_failed = _tally(KIND_INFRA, "failed")
    data_failed = _tally(KIND_DATA, "failed")

    suffix = (
        ""
        if _require_stack
        else "   <- strict mode (--require-stack) would FAIL these"
    )
    write(f"INFRA SKIPS: {_render(infra)}{suffix if infra else ''}")

    data_suffix = (
        "" if _require_corpus else "   <- --require-corpus would FAIL these"
    )
    write(f"DATA SKIPS:  {_render(data)}{data_suffix if data else ''}")
    write(f"ENV SKIPS:   {_render(env)}   <- machine caveats; never failed by a switch")

    if infra_failed:
        write(f"INFRA FAILURES (--require-stack): {_render(infra_failed)}")
    if data_failed:
        write(f"DATA FAILURES (--require-corpus): {_render(data_failed)}")

    xfailed = sorted(
        {r for kind, r, out in _STORE.values() if kind == KIND_XFAIL and out == "skipped"}
    )
    n_xfail = sum(1 for k, _r, out in _STORE.values() if k == KIND_XFAIL and out == "skipped")
    if n_xfail:
        write(
            f"RECORDED DEFECTS: {n_xfail}   <- xfail(strict): still broken ON PURPOSE. "
            "Each names a register entry; fixing one turns the suite RED"
        )
        for reason in xfailed[:8]:
            write(f"  - {reason[:110]}")
        if len(xfailed) > 8:
            write(f"  ... and {len(xfailed) - 8} more")

    other = sorted(
        {reason for kind, reason, out in _STORE.values() if kind is None and out == "skipped"}
    )
    n_other = sum(1 for k, _r, out in _STORE.values() if k is None and out == "skipped")
    write(f"OTHER SKIPS: {n_other}" + ("  (not yet classified)" if n_other else ""))
    for reason in other[:8]:
        write(f"  - {reason[:100]}")
    if len(other) > 8:
        write(f"  ... and {len(other) - 8} more distinct reasons")

    n_collect = _COUNTS.get("collect_skipped", 0)
    if n_collect:
        write(
            f"COLLECT-TIME SKIPS: {n_collect}   <- whole modules left the run before any "
            "test of them existed"
        )

    if _COUNTS.get("fatal_nothing_ran"):
        write("")
        write(
            "FAILED (--require-stack): no test ran. A strict run that asserts nothing is "
            "not a pass; the exit code is 1."
        )
    if _COUNTS.get("fatal_collect_skips"):
        write("")
        write(
            "FAILED (--require-stack): a module was skipped at collection time. "
            "Collection-time skips:"
        )
        for reason in _unsanctioned_collect_skips()[:8]:
            write(f"  - {reason[:110]}")

    if (infra or n_collect) and not _require_stack:
        write("")
        write(
            "A dependency above was unreachable. In CI this run would be RED: "
            "pytest --require-stack (or MEDOS_REQUIRE_STACK=1)."
        )
