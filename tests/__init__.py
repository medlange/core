# SPDX-License-Identifier: Apache-2.0
"""The MedicalOS test suite, as a package.

This file holds no code and has one job: to make a test module's import name its PATH.
Without it, pytest names a module after its BASENAME alone, and two files sharing a
basename across two directories become the same module twice. That is not a warning; it
is a collection ERROR that stops the run before one test executes:

    import file mismatch:
      imported module 'test_migration_planes' has this __file__ attribute:
        tests/unit/test_migration_planes.py
      which is not the same as the test file we want to collect:
        tests/integration/test_migration_planes.py
      HINT: ... use a unique basename for your test file modules

`pytest` -- the bare command, the one `tests/README.md` puts on its first line and a
newcomer types first -- could not run here for as long as `test_migration_planes.py` and
`test_deployment_gate.py` each existed in two directories. The suite was green the whole
time, because CI and every contributor ran the directories one at a time. What was red was
the entry point. The hint pytest offers is the wrong half of the choice: renaming a file
so a tool can tell two of them apart puts the tool's limitation in the test's name, and
`tests/integration/test_migration_planes.py` and `tests/unit/test_migration_planes.py` are
correctly named -- the same subject examined two ways, which is exactly what a directory
split is for.

AND A SECOND EFFECT, QUIETER. `tests/gate/test_capability_reachable.py` does

    from tests.integration import test_viewer_capability_list as viewer_list
    from tests.gate.conftest import JOB_TABLES, Api, Ingested, why

Those dotted names resolve to the modules pytest collected only if pytest spells them the
same way. It did not: with `__init__.py` in `tests/gate/` but not here, the walk stopped
one directory short and pytest called them `gate.conftest` and
`integration.test_viewer_capability_list`, while the imports asked for `tests.*`. Measured
on a collect-only run of that one file, `tests/gate/conftest.py` stood in `sys.modules`
TWICE, under both names, with two copies of its module-level state -- fixtures defined in
one copy, constants read from the other. It is one module now.

`tests/unit/test_suite_layout.py` keeps this true for directories added later.
"""
