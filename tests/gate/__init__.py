# SPDX-License-Identifier: Apache-2.0
"""The MedicalOS release gates.

`docs/spec/15-delivery.md` §15.1.2 gives each release a row naming the checks that MUST be
green before its tag, and `MOS-REL-004` removes reviewer discretion over them. This package
is those checks and nothing else: one module per check, named after it, carrying its own
release's marker, so a gate is a command rather than a convention.

    pytest -m gate_0_1_0 -v --require-stack     five checks, against the running deployment
    pytest -m gate_0_2_0 -v --require-stack     six checks, against the evidence plane

Each command refuses a subset of its own row (`tests/gate/conftest.py`), and
`tests/unit/test_gate_contract.py` asserts the same completeness from the default suite by
parsing §15.1.2 rather than by keeping a list. The two rows have different subjects and
different declared dependencies -- `tests/gate/conftest.py` and `tests/gate/_evidence.py`
give the argument in full.

Releases 0.3.0 and 0.4.0 have rows in §15.1.2 and no modules here yet; that is declared, and
asserted, in `tests/unit/test_gate_contract.py`.
"""
