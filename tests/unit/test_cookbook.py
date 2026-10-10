# SPDX-License-Identifier: Apache-2.0
"""The deployment-profile cookbook cannot rot quietly either.

`medos/examples/profiles/README.md` is an operator document: every recipe in it was
proven against the live stack, and its code blocks and recorded outputs are the
contract a reader follows. These checks pin the parts that drift silently: the
profiles the recipes name exist, the CLI the recipes type is the CLI that exists,
and the one rule that bit during the live runs — `fields` maps the EXTERNAL key to
the CANONICAL field — is stated in the cookbook's own words.

Spec: roadmap C4.
"""

from __future__ import annotations

import re
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[2] / "medos" / "examples" / "profiles"
COOKBOOK = (EXAMPLES / "README.md").read_text(encoding="utf-8")


def test_the_cookbook_names_the_profiles_that_exist() -> None:
    for name in ("local.yaml", "mosmed.yaml"):
        assert (EXAMPLES / name).is_file(), f"the cookbook's recipe names {name}, which is gone"
        assert name in COOKBOOK, f"the cookbook lost its {name} recipe"


def test_the_cookbook_types_the_cli_that_exists() -> None:
    assert "python -m medos.sdk run --profile" in COOKBOOK, (
        "the cookbook's command must be the real one: `python -m medos.sdk run`"
    )


def test_the_cookbook_states_the_fields_direction_the_codec_enforces() -> None:
    assert re.search(r"external payload key\s*→\s*the event's canonical field", COOKBOOK), (
        "decode_request reads mapping.fields as external-key -> canonical-field; the "
        "cookbook must say so in words, because the CodecError is where operators "
        "meet the rule otherwise"
    )
    assert "{study_uid}" in COOKBOOK and "{ai_result}" in COOKBOOK, (
        "the outbound template placeholders are the event document's field names; a "
        "cookbook that omits them produces identity-less outbound messages"
    )


def test_the_cookbook_carries_real_recorded_outputs() -> None:
    assert '"segments": 1' in COOKBOOK, "recipe 1 must show what a real run printed"
    assert '"type": "DICOMREPORTNOTIFY"' in COOKBOOK, (
        "recipe 2 must show the real outbound shape, placeholders resolved"
    )
    assert "deploy_model.py --modelcard" in COOKBOOK, (
        "recipe 3 is the G-C2 command; the cookbook is where an operator meets it"
    )
