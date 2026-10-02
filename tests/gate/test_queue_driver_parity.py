# SPDX-License-Identifier: Apache-2.0
"""`queue-driver-parity` -- §15.1.2's release-0.3.0 gate row, third check.

    "the `JobQueue` conformance suite passes IDENTICALLY against every registered driver,
     including the second driver this release introduces"

`MOS-TEST-045` makes `C-QUEUE` "a single executable suite parameterised by driver. Every
driver MUST pass every check, unmodified", `MOS-TEST-046` adds the rule that gives the
check its teeth --

    "The 0.3 Kafka driver MUST pass Q01-Q14 with zero changes to the suite. Any check that
     needs a driver-specific variant indicates a leaky port and MUST be resolved by
     changing the port, not the test."

-- and chapter 14 acceptance check 16 fixes the observable: the suite passes against both
drivers "with a byte-identical suite file. `git diff` of the suite between the two runs is
empty."

WHY THIS MODULE FORKS A PYTEST INSTEAD OF RE-ASSERTING Q01-Q14
----------------------------------------------------------------
The obvious implementation -- copy the fourteen checks into `tests/gate/` and parameterise
them -- would be the exact thing `MOS-TEST-045` forbids: a SECOND suite, which can drift
from the first, and whose passing says nothing about whether the first still passes. So
this check's subject is `tests/integration/test_queue_parity.py` itself. It runs that file
in a child pytest, collects the per-test outcomes from a JUnit report, and asserts the
OUTCOME TABLE is identical across drivers.

"Identical" is asserted as a table and not as a count, because the failure this guards is
not "the Kafka driver is broken" -- that shows up anywhere. It is Q07 passing under driver
1 and skipping under driver 2, which two green summary lines hide perfectly.

AGAINST A THROWAWAY DATABASE
-------------------------------
The child is pointed at `platform_dsn` and not at the deployment. The conformance suite
TRUNCATEs the job tables on every test, and the deployment's job tables are the 0.1.0 gate
row's subject: a 0.3.0 check that destroyed them would turn another release's gate red for
a reason that has nothing to do with the queue. The same rule `tests/gate/_evidence.py`
states -- nothing truncates a table it did not create.

WHAT "EVERY REGISTERED DRIVER" MEANS HERE
--------------------------------------------
Discovered, not listed: every class under `medos.db` that implements all six methods of
the `JobQueue` port. A third driver added without being added to the suite's
parameterisation fails `test_..._every_registered_driver_is_exercised`, which is the
failure mode the phrase "every registered driver" exists to catch.

Slowest check in the row, and it is last in `tests/gate/conftest.py`'s order for that
reason: it runs Q01-Q14 twice plus the outbox checks, about a minute and a half.

Spec: MOS-EXEC-025, MOS-EXEC-032, MOS-EXEC-037, MOS-EXEC-040, MOS-EXEC-041, MOS-TEST-045,
MOS-TEST-046, MOS-REL-004, MOS-REL-023; chapter 14 §14.5.5 and acceptance checks 16, AT-20.
"""

from __future__ import annotations

import hashlib
import os
import pkgutil
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from medos.db.queue import JobQueue

from tests._support.roots import REPO_ROOT, child_pythonpath
from tests._support.skips import skip_infra

pytestmark = pytest.mark.gate_0_3_0

#: BOTH import roots, computed in one place. `medos` is `medos/medos/` now, so a
#: child given only the repository root raises ModuleNotFoundError -- and reports it
#: as whatever this test was measuring. See `tests/_support/roots.py`.
SUITE = REPO_ROOT / "tests" / "integration" / "test_queue_parity.py"

#: The six methods of the port. Read off the Protocol rather than written out, so a method
#: added to `JobQueue` widens the definition of "a driver" automatically.
PORT_METHODS = frozenset(n for n in vars(JobQueue) if not n.startswith("_"))

#: `MOS-TEST-045`'s check list. A driver that passes thirteen of them passes no gate.
Q_CHECKS = tuple(f"q{n:02d}" for n in range(1, 15))

#: Outcomes the report can carry. `passed` is the absence of a child element.
_OUTCOME_TAGS = {"failure": "failed", "error": "error", "skipped": "skipped"}

_PARAM_RE = re.compile(r"\[([^\]]+)\]$")


def registered_drivers() -> dict[str, type]:
    """Every class under `medos.db` that implements the whole `JobQueue` port."""
    import importlib

    import medos.db as package

    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(list(package.__path__)):
        module = importlib.import_module(f"medos.db.{info.name}")
        for name, obj in vars(module).items():
            if not isinstance(obj, type) or name.startswith("_"):
                continue
            if obj is JobQueue or getattr(obj, "_is_protocol", False):
                continue
            if PORT_METHODS <= set(dir(obj)):
                found[name] = obj
    return found


def _run_suite(dsn: str, report: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["MEDOS_TEST_DATABASE_URL"] = dsn
    env["PYTHONPATH"] = child_pythonpath(inherit=env.get("PYTHONPATH"))
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(SUITE),
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
            f"--junit-xml={report}",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )


def suite_driver_ids() -> tuple[str, ...]:
    """The `params` of the suite's own `driver` fixture, read out of its source.

    Read from the suite rather than guessed from the report, because a pytest id is a
    JOIN of every parameterisation a test carries: Q10 is also parameterised on
    `explode_after_dispatch`, so its ids read `postgres-True` and `kafka-False`. Splitting
    those on the driver names the suite declares is exact; splitting them on `-` and
    hoping is not.
    """
    import ast

    tree = ast.parse(SUITE.read_text(encoding="utf-8"), filename=str(SUITE))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "driver":
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            for keyword in decorator.keywords:
                if keyword.arg != "params":
                    continue
                return tuple(
                    element.value
                    for element in getattr(keyword.value, "elts", [])
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
    return ()


def _outcomes(report: Path, driver_ids: tuple[str, ...]) -> dict[str, dict[str, str]]:
    """`{check: {driver: outcome}}` from the JUnit report.

    A test id's bracket may carry several parameterisations joined by `-`; the driver is
    whichever component the suite declares as one. Tests with no driver component -- the
    outbox group, which `MOS-EXEC-040` scopes to driver 2 only and which therefore takes a
    `kafka` fixture directly -- land under the key `-`, so they are visible in the table
    without being read as a parity asymmetry.
    """
    table: dict[str, dict[str, str]] = {}
    root = ET.parse(report).getroot()
    for case in root.iter("testcase"):
        name = case.get("name") or ""
        match = _PARAM_RE.search(name)
        parts = match.group(1).split("-") if match else []
        found = [p for p in parts if p in driver_ids]
        driver = found[0] if found else "-"
        # Keep the OTHER parameterisations in the check name, so Q10's two arms are two
        # rows rather than one row that silently overwrites itself.
        rest = [p for p in parts if p not in driver_ids]
        check = _PARAM_RE.sub("", name) + (f"[{'-'.join(rest)}]" if rest else "")
        outcome = "passed"
        for child in case:
            if child.tag in _OUTCOME_TAGS:
                outcome = _OUTCOME_TAGS[child.tag]
                break
        table.setdefault(check, {})[driver] = outcome
    return table


@pytest.fixture(scope="module")
def conformance(platform_dsn: str, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One child pytest over the whole conformance suite. Returns the outcome table.

    Module-scoped: the suite takes over a minute and every assertion below reads the same
    run, which is also what makes "identically" a claim about ONE execution rather than
    about two that happened to agree.
    """
    if not SUITE.exists():
        skip_infra(
            f"{SUITE.relative_to(REPO_ROOT)} does not exist, so there is no conformance "
            f"suite to run against the drivers. MOS-TEST-045 makes C-QUEUE a single "
            f"executable suite; this check cannot substitute for it.",
            dependency="conformance-suite",
        )
    report = tmp_path_factory.mktemp("parity") / "report.xml"
    before = hashlib.sha256(SUITE.read_bytes()).hexdigest()
    proc = _run_suite(platform_dsn, report)
    after = hashlib.sha256(SUITE.read_bytes()).hexdigest()
    if not report.exists():
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        skip_infra(
            f"the conformance suite produced no report (exit {proc.returncode}). "
            f"Tail:\n{tail}",
            dependency="postgres",
        )
    driver_ids = suite_driver_ids()
    return {
        "table": _outcomes(report, driver_ids),
        "driver_ids": driver_ids,
        "returncode": proc.returncode,
        "digest_before": before,
        "digest_after": after,
        "tail": "\n".join((proc.stdout + proc.stderr).splitlines()[-25:]),
    }


# =====================================================================================
# THE CHECK
# =====================================================================================
def test_queue_driver_parity_every_check_has_the_same_outcome_under_every_driver(
    conformance: dict[str, Any],
) -> None:
    """The outcome TABLE is identical across drivers. `MOS-TEST-045` / `MOS-TEST-046`.

    Reported per check and not as a count. Q07 passing under driver 1 and skipping under
    driver 2 is a real and likely regression, and two green summary lines hide it
    completely.
    """
    table = conformance["table"]
    parameterised = {
        check: by_driver
        for check, by_driver in table.items()
        if set(by_driver) != {"-"}
    }
    assert parameterised, (
        "no test in the conformance suite is parameterised by driver. MOS-TEST-045 makes "
        "C-QUEUE one suite run once per driver; a suite that is not parameterised is not "
        "running against every driver."
    )

    divergent = {
        check: by_driver
        for check, by_driver in sorted(parameterised.items())
        if len(set(by_driver.values())) > 1
    }
    assert not divergent, (
        "the conformance suite does not behave identically across drivers:\n  "
        + "\n  ".join(f"{check}: {by_driver}" for check, by_driver in divergent.items())
        + "\nMOS-TEST-046: a check that needs a driver-specific variant indicates a LEAKY "
        "PORT and MUST be resolved by changing the port, not the test."
    )

    not_passing = {
        check: by_driver
        for check, by_driver in sorted(table.items())
        if any(outcome != "passed" for outcome in by_driver.values())
    }
    assert not not_passing, (
        "the conformance suite is not green:\n  "
        + "\n  ".join(f"{check}: {by_driver}" for check, by_driver in not_passing.items())
        + f"\n\nchild pytest tail:\n{conformance['tail']}"
    )
    assert conformance["returncode"] == 0, conformance["tail"]


def test_queue_driver_parity_all_fourteen_checks_ran_under_every_driver(
    conformance: dict[str, Any],
) -> None:
    """`MOS-TEST-045`'s Q01-Q14, each present for each driver.

    "Identical outcomes" is satisfied by a suite from which six checks have been deleted.
    This is the completeness half, and it is the same argument `tests/gate/conftest.py`
    makes about a partial gate: `MOS-REL-012`, an unexecuted acceptance criterion means
    the requirement is not satisfied whatever the code does.
    """
    table = conformance["table"]
    drivers = sorted(conformance["driver_ids"])
    assert drivers, "the suite declares no `driver` fixture parameterisation"

    missing: list[str] = []
    for q in Q_CHECKS:
        matched = [check for check in table if check.lower().startswith(f"test_{q}_")]
        if not matched:
            missing.append(q.upper())
            continue
        for check in matched:
            for driver in drivers:
                if driver not in table[check]:
                    missing.append(f"{q.upper()} under {driver}")
    assert not missing, (
        f"the conformance suite does not cover {sorted(set(missing))} for every driver. "
        f"MOS-TEST-045 requires every driver to pass EVERY check."
    )


def test_queue_driver_parity_every_registered_driver_is_exercised(
    conformance: dict[str, Any],
) -> None:
    """"Every registered driver", read off the code rather than off a list.

    A third `JobQueue` implementation added to `medos/medos/db/` without being added to the
    suite's parameterisation is a driver nothing gates. That is the failure this phrase
    exists to catch, and a hand-maintained list in this file could not catch it.
    """
    drivers = registered_drivers()
    assert len(drivers) >= 2, (
        f"only {sorted(drivers)} implement the JobQueue port. MOS-REL-023 puts a SECOND "
        f"driver in this release, and a parity check over one driver is not one."
    )

    params = sorted(conformance["driver_ids"])
    assert len(params) == len(drivers), (
        f"the suite is parameterised over {params} ({len(params)}) and medos.db "
        f"registers {sorted(drivers)} ({len(drivers)}). Every registered driver must be "
        f"exercised by the one suite."
    )

    source = SUITE.read_text(encoding="utf-8")
    unexercised = sorted(name for name in drivers if name not in source)
    assert not unexercised, (
        f"{unexercised} implement the JobQueue port and are never constructed by the "
        f"conformance suite, so nothing establishes that they satisfy it."
    )


def test_queue_driver_parity_the_suite_file_is_byte_identical_across_the_runs(
    conformance: dict[str, Any],
) -> None:
    """Chapter 14 acceptance check 16: "`git diff` of the suite between the two runs is
    empty".

    Both drivers run from ONE file in ONE process here, so the digest cannot differ -- and
    that is the point: the check is that the suite is a single artifact, and it is asserted
    rather than asserted-about. A future implementation that ran two child processes with a
    conditional edit between them would fail here.
    """
    assert conformance["digest_before"] == conformance["digest_after"], (
        "the conformance suite file changed while it was running. Whatever the outcomes "
        "say, the two drivers were not measured by the same suite."
    )


def test_queue_driver_parity_no_check_branches_on_the_driver_name(
    conformance: dict[str, Any],
) -> None:
    """`MOS-TEST-046`, enforced on the suite's source. The leaky-port rule.

    A conformance suite that says `if driver.name == "kafka":` has stopped being one
    suite: the Kafka arm and the Postgres arm assert different things and the parity
    claim is empty. `.name` is documented in the suite as being for test ids only, and
    this is what keeps that true.

    The outbox group is exempt by requirement, not by convenience: `MOS-EXEC-040` gives
    driver 2 an outbox and driver 1 none, and `MOS-TEST-046` itself scopes the Q13
    addendum to Kafka. Those tests take a `kafka` fixture directly instead of the `driver`
    fixture, so they are not parameterised and are excluded from the parity table above --
    which is why this check reads BRANCHES inside the shared suite rather than mentions of
    the word.
    """
    import ast

    tree = ast.parse(SUITE.read_text(encoding="utf-8"), filename=str(SUITE))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        takes_driver = any(a.arg == "driver" for a in node.args.args)
        if not takes_driver:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, (ast.If, ast.IfExp)):
                continue
            test_src = ast.dump(inner.test)
            if "attr='name'" in test_src and "id='driver'" in test_src:
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, (
        f"the conformance suite branches on the driver name: {offenders}. MOS-TEST-046 -- "
        f"'any check that needs a driver-specific variant indicates a leaky port and MUST "
        f"be resolved by changing the port, not the test'."
    )
