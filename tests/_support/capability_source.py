# SPDX-License-Identifier: Apache-2.0
"""One helper: make the DEPLOYMENT serve a capability the platform does not ship.

WHAT THIS IS FOR
-----------------
`medos.capabilities.providers` is the ONE place that answers "which capabilities does this
deployment serve". Four paths read it, and each of them, at some point in this project's
history, read `medos.capabilities.REGISTRY` -- the PLATFORM singleton -- instead:

    medos.api.routes_jobs.known_capability_ids()   admission at POST /api/v1/jobs
    medos.worker.steps.WorkerDeps.registry         execution
    medos.registry.repo.publish()                  MOS-REG-029, at publish
    medos.resolution.snapshot.load_snapshot()      the section 6.6 resolution vocabulary

The first two were fixed at `8734721` and the last two were not, which produced a
deployment whose worker RAN a capability that no `ServiceVersion` could be published
against and that section 6.6 resolved as `capability_unknown`. Entry 68 of
`docs/spec/99-known-inconsistencies.md` records it. The general shape is the one this
project keeps meeting: one rule written down in two places, and the copies drift.

WHY THE RESOLVER IS SUBSTITUTED HERE RATHER THAN CONFIGURED
-------------------------------------------------------------
`tests/integration/test_capability_providers.py` proves the MECHANISM -- that
`MEDOS_CAPABILITY_PROVIDERS` reaches the resolver, that a bad configuration refuses to
start, that the API and the worker cannot disagree. Repeating that here would test the
seam a third time and pin nothing new, and it would couple three unrelated test files to
the shape of the configuration variable.

What the callers of this helper pin is narrower and is the thing that actually broke: that
the path ASKS THE RESOLVER AT ALL. Substituting the resolver's one public answer is the
sharpest instrument for that, because it is the only change made: if the path under test
still reads `medos.capabilities.REGISTRY`, the substitution has no effect on it and the
assertion fails. A capability id no platform build contains is used for the same reason --
a path reading the singleton cannot accidentally pass.

Not a fixture, because two of the three callers need it after other fixture work has
already run and one needs it before.
"""

from __future__ import annotations

import pytest

__all__ = ["VENDOR_CAPABILITY_ID", "serve_one_extra_capability"]

#: A capability id the platform registry does not hold and no provider in this repository
#: supplies. Shaped to `medos/medos/registry/schemas.py`'s `_CAPABILITY` pattern
#: (`^[a-z][a-z0-9_]{2,47}$`, which mirrors `MOS-REG-041`) so that a `ServiceVersion`
#: manifest claiming it passes JSON-Schema validation and reaches the `MOS-REG-029` check
#: this exists to exercise.
VENDOR_CAPABILITY_ID = "vendor_stub_capability"


def serve_one_extra_capability(
    monkeypatch: pytest.MonkeyPatch, capability_id: str = VENDOR_CAPABILITY_ID
) -> frozenset[str]:
    """This deployment serves the platform's capabilities plus `capability_id`.

    Returns the served set, so a caller can assert equality against it rather than
    restating the platform three -- which would put a fourth copy of the list in the tree
    while testing that there is only one.
    """
    from medos.capabilities import REGISTRY, providers

    assert capability_id not in REGISTRY, (
        f"{capability_id!r} is in the platform registry, so a path that reads the "
        f"singleton would pass these tests. Pick an id the platform does not ship."
    )
    served = frozenset(REGISTRY) | {capability_id}

    def _deployment_capability_ids(**_: object) -> frozenset[str]:
        return served

    # The MODULE attribute, not a name already bound in a caller: `repo.py` and
    # `snapshot.py` both do a function-local `from medos.capabilities.providers import
    # capability_ids`, so they read this attribute at call time, and `routes_jobs.py`
    # holds the module and calls through it for the same reason.
    monkeypatch.setattr(providers, "capability_ids", _deployment_capability_ids)
    return served
