# SPDX-License-Identifier: Apache-2.0
"""Development-stack demo helpers.

A package only so that `tests/unit/test_demo_corpus.py` can import `seed_corpus` by name
rather than by path. Nothing here is part of the platform: `medos/tools/` sits outside `medos/medos/`
because `MOS-IMG-069` forbids the platform to mint a `StudyInstanceUID`, and a seeder
must.
"""
