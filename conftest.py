# SPDX-License-Identifier: Apache-2.0
"""Repository-root conftest.

Its only job is to register the skip taxonomy (`tests/_support/skips.py`) as a pytest
plugin, so that `--require-stack` and `--require-corpus` exist no matter which
subdirectory of `tests/` is being run:

    pytest                                  # everything
    pytest tests/integration --require-stack
    pytest tests/e2e/test_demo.py --require-stack

`pytest_plugins` is only honoured in the ROOTDIR conftest, which is why this file is at
the top level rather than under `tests/`. The taxonomy itself lives in one place and is
not duplicated here.

The `sys.path` insertion is belt and braces: `[tool.pytest.ini_options] pythonpath = [".", "medos"]`
already puts the repository root on the path before conftests load, but this file is also
imported by tooling that does not read that setting.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
# `medos/` is a product directory; the package is `medos/medos/`, and `tools` and
# `services` sit beside it. Both roots go on the path, in the same order as
# `[tool.pytest.ini_options] pythonpath`, so tooling that does not read that setting sees
# the same thing pytest does.
for _entry in (str(_ROOT), str(_ROOT / "medos")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

pytest_plugins = ("tests._support.skips",)
