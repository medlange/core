# SPDX-License-Identifier: Apache-2.0
"""The `medos` command line.

Deliberately thin. Everything here either REPORTS on a deployment or drives a tool that
already exists; no command in this package makes a clinical decision, writes an evidence
row, or changes a declaration. The platform's surface is its API and its Python package,
and a CLI that grew a second way to do those things would be a second implementation to
keep honest.
"""
