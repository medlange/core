# SPDX-License-Identifier: Apache-2.0
"""Tests that drive the deployed stack over HTTP, QIDO and SQL, and nothing else.

A module here talks to `deploy/docker-compose.yml` the way a client would: no fixture
reaches inside a container, no assertion reads a file the stack owns. They carry the `e2e`
marker so CI can select or deselect them, and they skip -- never fail -- when the stack is
down, which is precisely the behaviour `--require-stack` exists to override.

`tests/__init__.py` says why these package markers exist.
"""
