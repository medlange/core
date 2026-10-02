# SPDX-License-Identifier: Apache-2.0
"""Read the JSON document a container printed, out of a stream that also carries chatter.

WHY THIS IS NOT `json.loads(stdout)`
--------------------------------------
The trainer's subcommands print one JSON document, and everything else in the image
prints too: `numexpr` announces its thread count, nnU-Net prints its whole plan, torch
warns about pinned memory. So the document is at the END of a stream with prose in front
of it.

WHY IT IS NOT `json.loads(stdout[stdout.rindex("{"):])` EITHER, WHICH IS WHAT IT WAS.
MEASURED: `rindex("{")` finds the last OPENING brace in the text, which for any nested
document is the start of its LAST INNER OBJECT -- `"seeds": {` -- and decoding from there
gets a complete object followed by the enclosing document's closing braces, so
`json.loads` raises `Extra data` on a perfectly well-formed document. Five assertions in
`tests/integration/test_trainer_image.py` failed that way, none of them for a reason that
had anything to do with what they were asserting, and a helper that fails for its own
reasons is a helper that teaches people to distrust the test.

`raw_decode` from each candidate line start, last first, is the fix: it stops at the end
of the first complete value and does not care what follows.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["last_json_object"]


def last_json_object(text: str) -> dict[str, Any]:
    """The last top-level JSON object in `text`. Raises `ValueError` when there is none."""
    decoder = json.JSONDecoder()
    lines = text.splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if not lines[index].startswith("{"):
            continue
        try:
            value, _ = decoder.raw_decode("\n".join(lines[index:]))
        except ValueError:
            continue
        if isinstance(value, dict):
            return dict(value)
    raise ValueError(f"no top-level JSON object in:\n{text[-4000:]}")
