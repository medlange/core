# SPDX-License-Identifier: Apache-2.0
"""Tests that drive more than one component together.

The API against a real Postgres, the queue against a real driver, a migration against a
real schema. What is not reachable is SKIPPED through `tests/_support/skips.py` rather than
passed over in silence, and `--require-stack` turns those skips into failures -- see
`tests/README.md` for why the suite has two switches instead of a habit.

`tests/__init__.py` says why these package markers exist.
"""
