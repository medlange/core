# SPDX-License-Identifier: Apache-2.0
"""Tests that read the repository and need nothing running.

A module here opens files, parses documents and calls functions in `medos/medos/`. It does not
reach a port, a database or a container: whatever it asserts, it asserts on a laptop with
nothing started. That is why this is the directory the other two are compared against --
`tests/integration/` needs the stack, `tests/gate/` needs a deployment, and this one needs
a checkout.

`tests/__init__.py` says why these package markers exist.
"""
