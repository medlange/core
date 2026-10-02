# SPDX-License-Identifier: Apache-2.0
"""A development default must not reach a deployment that has not declared itself one.

WHAT THIS GATE IS FOR
---------------------
Level 1 makes MedicalOS start and work on a laptop, which means shipping answers to
declarations only a real deployment can truly make. Every one of those answers is a lie
about a production installation, and the entire safety of the arrangement rests on them
being unusable there.

So this is the gate that holds it, and it is written as an ENUMERATION and not only as a
predicate. Register entry 97 is the standing reason: `2.25.` was accepted as a covered UID
root because the shape looked right, and it turned out to belong to no authority -- so it
covered nothing and admitted everything while the suite stayed green. A guard spelled
"refuse any value that looks like a dev value" has precisely that defect: it says nothing
at all about a seventh declaration somebody adds next month with a friendly unmarked
default.

Hence property (2). Every variable in the closed set must EITHER carry the marker in the
shipped compose default OR appear in `MARKER_EXEMPT` with a written reason. A variable in
neither class fails, and the only way to add one is to decide, in writing, which it is.

HOW TO MAKE THIS GATE FAIL, which is the test of whether it is a gate at all:
  * delete the marker from the tenant salt's compose default  -> (2) red
  * remove the `MEDOS_ENV` anchor from docker-compose.yml     -> (1) red
  * make `is_dev_deployment` treat unset as dev               -> (3) red
  * drop `refuse_dev_value_outside_dev` from a loader         -> (4) red
  * add a guarded declaration with an unmarked default        -> (2) red

Spec: MOS-REL-032. Register entries 82, 97.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from medos.config.devmode import (
    DEV_MARKER,
    ENV_VAR,
    GUARDED_DECLARATIONS,
    MARKER_EXEMPT,
    DevValueOutsideDevDeployment,
    is_dev_deployment,
    offending_declarations,
    refuse_dev_value_outside_dev,
)

# NOT marked as a release gate. `tests/unit/test_gate_contract.py` requires every module
# under tests/gate/ to name an implemented section 15.1.2 gate row or to appear in
# LOCAL_EXTRA with an MOS-REL-009 response, and this check is neither -- it guards a
# Level-1 mechanism that no release criterion has yet been written for. Recording it as a
# gate without that accounting is exactly what MOS-UI-013a forbids. It runs in the default
# unit suite instead, which is what makes it enforced; promoting it to a gate is a release
# decision with a human signature on it.

COMPOSE = (
    Path(__file__).resolve().parents[2]
    / "medos" / "deploy" / "compose" / "docker-compose.yml"
)


def _compose_defaults() -> dict[str, str]:
    """Every `VAR: "${VAR-default}"` the compose file ships, as {variable: default}.

    Parsed from the text rather than from `docker compose config`, deliberately: this
    gate must state what the REPOSITORY ships, and `config` resolves the developer's own
    environment into the answer. A developer who exported a dev salt would otherwise see
    a green gate for a file that does not carry one.
    """
    text = COMPOSE.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    for match in re.finditer(r'^\s*(MEDOS_[A-Z_]+):\s*"?\$\{\1[-:]?-?(.*?)\}"?\s*$',
                             text, re.MULTILINE):
        found.setdefault(match.group(1), match.group(2))
    # The provenance declaration is written as a folded multi-line block, so it does not
    # match the single-line form above. Read it separately rather than let it look absent.
    folded = re.search(r'(MEDOS_[A-Z_]+):\s*>-\s*\n\s*\$\{\1-(.*?)\}\}\s*\n', text, re.DOTALL)
    if folded:
        found.setdefault(folded.group(1), re.sub(r"\s+", " ", folded.group(2)))
    return found


# --------------------------------------------------------------------------------------
# (1) the switch exists and the development stack sets it
# --------------------------------------------------------------------------------------


def test_the_compose_stack_declares_itself_a_development_stack() -> None:
    """Without this the whole mechanism is inert, which is what it was until now: MEDOS_ENV
    was read by two Python files and set by zero services, so the credential-environment
    check that was supposed to keep dev keys out of production had nothing to compare."""
    defaults = _compose_defaults()
    assert ENV_VAR in defaults, (
        f"{ENV_VAR} is no longer declared in docker-compose.yml. Every development "
        f"default in that file is then unusable on the development stack itself, and the "
        f"credential environment check has nothing to compare against."
    )
    assert defaults[ENV_VAR] == "dev", defaults[ENV_VAR]


# --------------------------------------------------------------------------------------
# (2) THE ENUMERATION -- the half a predicate cannot give
# --------------------------------------------------------------------------------------


def test_every_guarded_declaration_is_marked_or_exempted_in_writing() -> None:
    """The property that survives somebody adding a new declaration.

    A variable may ship a development default ONLY if that default carries the marker, or
    if it is written down in `MARKER_EXEMPT` with a reason. Neither is not an option, and
    the failure message says which of the two the author has to choose.
    """
    defaults = _compose_defaults()
    unclassified: list[str] = []
    for variable in GUARDED_DECLARATIONS:
        if variable in MARKER_EXEMPT:
            continue
        default = defaults.get(variable, "")
        if not default:
            continue  # ships no default at all: nothing to smuggle
        if DEV_MARKER not in default:
            unclassified.append(variable)
    assert not unclassified, (
        f"these variables ship a default that does NOT carry {DEV_MARKER!r} and are not "
        f"in MARKER_EXEMPT: {unclassified}\n"
        f"Each one is a value a production deployment would silently accept. Either put "
        f"the marker in the default, so the loader refuses it outside a dev stack, or add "
        f"it to MARKER_EXEMPT in medos/medos/config/devmode.py with the reason it is equally "
        f"true in production. Choosing neither is what this assertion exists to prevent."
    )


def test_every_exemption_gives_a_reason() -> None:
    """An exemption without an argument is an exemption nobody can disagree with."""
    for variable, reason in MARKER_EXEMPT.items():
        assert variable in GUARDED_DECLARATIONS, (
            f"{variable} is exempted from a set it is not in; the exemption is dead text."
        )
        assert len(reason.split()) >= 8, f"{variable}: the reason is too short to be one"


def test_the_guarded_set_is_not_empty_and_covers_the_salt() -> None:
    """A closed set that drifted to empty would make every other assertion here vacuous."""
    assert GUARDED_DECLARATIONS
    assert "MEDOS_TENANT_SALT" in GUARDED_DECLARATIONS, (
        "the tenant salt is the declaration with the worst failure mode -- every "
        "patient_key and institution_key ever written under a published secret -- and it "
        "must be in the guarded set."
    )
    assert "MEDOS_TENANT_SALT" not in MARKER_EXEMPT


# --------------------------------------------------------------------------------------
# (3) unset is not dev -- the inversion that closes the omission path
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "DEV", "Dev", "development", "prod", "staging"])
def test_only_the_exact_string_dev_is_a_development_deployment(value) -> None:
    """The failure that actually happens is an operator copying the compose file and
    setting nothing. If unset meant dev, that deployment would accept every development
    default in this repository and look healthy."""
    env = {} if value is None else {ENV_VAR: value}
    assert is_dev_deployment(env) is False, f"{value!r} was treated as a dev deployment"


def test_dev_is_a_development_deployment() -> None:
    assert is_dev_deployment({ENV_VAR: "dev"}) is True


# --------------------------------------------------------------------------------------
# (4) the refusal fires, and fires for each loader
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("env", [{}, {ENV_VAR: "prod"}, {ENV_VAR: "DEV"}])
def test_a_marked_value_is_refused_outside_an_explicit_dev_deployment(env) -> None:
    with pytest.raises(DevValueOutsideDevDeployment) as caught:
        refuse_dev_value_outside_dev("MEDOS_TENANT_SALT", f"x-{DEV_MARKER}-y", env)
    message = str(caught.value)
    assert "MEDOS_TENANT_SALT" in message, "the refusal must name the variable"
    assert ENV_VAR in message, "and say which switch decides"


def test_a_marked_value_is_accepted_on_a_development_deployment() -> None:
    refuse_dev_value_outside_dev("MEDOS_TENANT_SALT", f"x-{DEV_MARKER}-y", {ENV_VAR: "dev"})


def test_an_unmarked_value_is_never_refused() -> None:
    """The guard must be inert for real values, or a production deployment cannot start."""
    for env in ({}, {ENV_VAR: "prod"}, {ENV_VAR: "dev"}):
        refuse_dev_value_outside_dev("MEDOS_TENANT_SALT", "a-real-operator-secret", env)


def test_an_absent_value_is_left_to_its_own_loader() -> None:
    """Absence is the loader's business and it already refuses it with a better message."""
    for value in (None, ""):
        refuse_dev_value_outside_dev("MEDOS_TENANT_SALT", value, {ENV_VAR: "prod"})


@pytest.mark.parametrize(
    "module,function",
    [
        ("medos/medos/training/seal.py", "load_deid_provenance"),
        ("medos/medos/training/channelmap.py", "load_channel_map"),
        ("medos/medos/api/routes_training.py", "load_training_environment"),
        ("medos/medos/api/routes_curation.py", "_tenant_salt"),
    ],
)
def test_each_loader_calls_the_refusal(module: str, function: str) -> None:
    """Asserted PER FUNCTION, via the syntax tree, and the distinction is not pedantry.

    The first version of this test asked whether the FILE contained the call anywhere.
    `routes_curation.py` holds two loaders, so a mutation that deleted the guard from
    `_tenant_salt` left the other one's call in place and the gate stayed green -- a check
    that could not fail, discovered by running the mutation rather than by reading the
    test. Walking the named function's own body is what closes it.

    Still a source check, and still honest about what that means: it catches deletion, not
    a call placed after the value has already been used. The behavioural half is the
    parametrised refusal tests above, which execute the predicate itself.
    """
    import ast

    tree = ast.parse((Path(__file__).resolve().parents[2] / module).read_text(encoding="utf-8"))
    target = next(
        (node for node in ast.walk(tree)
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name == function),
        None,
    )
    assert target is not None, (
        f"{module} no longer defines {function}. If the loader was renamed, this gate must "
        f"be renamed with it -- a gate pointing at a function that does not exist passes "
        f"for the wrong reason."
    )
    called = any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None))
        == "refuse_dev_value_outside_dev"
        for node in ast.walk(target)
    )
    assert called, (
        f"{module}::{function} does not call refuse_dev_value_outside_dev. That "
        f"declaration can now carry a development default into a production deployment "
        f"unchallenged."
    )


# --------------------------------------------------------------------------------------
# (5) the startup sweep names them all at once
# --------------------------------------------------------------------------------------


def test_the_startup_sweep_names_every_offender_together() -> None:
    """A per-read refusal is lazy: a production deployment on a development salt starts
    healthily and discovers it at the first seal, by which time every row written since
    carries a patient_key computed under a published secret."""
    env = {
        ENV_VAR: "prod",
        "MEDOS_TENANT_SALT": f"salt-{DEV_MARKER}",
        "MEDOS_API_VIEWER_AUTHORIZATION": f"Bearer {DEV_MARKER}",
        "MEDOS_SEAL_STORE": "s3",
    }
    offenders = offending_declarations(env)
    assert set(offenders) == {"MEDOS_TENANT_SALT", "MEDOS_API_VIEWER_AUTHORIZATION"}, offenders


def test_the_sweep_is_silent_on_a_development_deployment() -> None:
    env = {ENV_VAR: "dev", "MEDOS_TENANT_SALT": f"salt-{DEV_MARKER}"}
    assert offending_declarations(env) == []


def test_the_sweep_ignores_variables_outside_the_closed_set() -> None:
    """Scope discipline: the sweep reports on declarations, not on every string in the
    environment that happens to mention the marker."""
    env = {ENV_VAR: "prod", "SOMETHING_ELSE": f"x-{DEV_MARKER}"}
    assert offending_declarations(env) == []
