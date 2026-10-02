# SPDX-License-Identifier: Apache-2.0
"""Where a CHILD interpreter has to look to import this repository.

`medos` is not at the repository root any more. It is `medos/medos/` -- the package
nested inside the product directory that carries its own README, deploy files and
Dockerfile -- and the SDK lives inside that package at `medos/medos/sdk/` (importable as
`medos.sdk`). `pip install -e .` resolves the package through
`[tool.setuptools.packages.find] where = [".", "medos"]`, so an IN-PROCESS import works
and nothing in a normal test run notices.

A subprocess does notice. Nine tests in this repository spawn a fresh interpreter to prove
something an in-process check cannot -- that an import closure carries no socket module,
that a verifier still refuses without the `cryptography` wheel, that a worker claims a job
and dies -- and each of them wrote `PYTHONPATH=<repo root>` by hand. After the move every
one of those children raised `ModuleNotFoundError: No module named 'medos.training'`, and
the failure arrived wearing the test's own clothes: `assert 1 == 0` from a closure probe,
`'SignatureBackendMissing' not in ...` from a verifier that never loaded, and "the child
never claimed the job" from a worker that never started. Three tests, three different
symptoms, one cause.

So the path is computed HERE, once. A site that needs an extra entry (a sandbox that must
shadow the installed tree) passes it and keeps the precedence it wants.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The repository. `tests/_support/roots.py` -> `tests/_support` -> `tests` -> here.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every directory a child must have on `sys.path` to import what this repository ships.
#: The repository root reaches `services`, `tests` and `tools`; `medos/` reaches the
#: `medos` package itself -- and with it `medos.sdk`, the SDK inside the package.
IMPORT_ROOTS: tuple[Path, ...] = (REPO_ROOT, REPO_ROOT / "medos")


def child_pythonpath(*prepend: Path | str, inherit: str | None = None) -> str:
    """`PYTHONPATH` for a child, with `prepend` ahead of this repository's roots.

    `inherit` appends an existing value (usually `os.environ.get("PYTHONPATH")`). Empty
    entries are dropped, because a stray `os.pathsep` puts the CURRENT DIRECTORY on the
    child's path and makes the result depend on where pytest was invoked from.
    """
    parts = [str(p) for p in prepend]
    parts += [str(p) for p in IMPORT_ROOTS]
    if inherit:
        parts += inherit.split(os.pathsep)
    seen: set[str] = set()
    ordered = [p for p in parts if p and not (p in seen or seen.add(p))]
    return os.pathsep.join(ordered)
