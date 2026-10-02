# SPDX-License-Identifier: Apache-2.0
"""The line between a development stack and a deployment that holds patients.

THE PROBLEM THIS SOLVES
-----------------------
MedicalOS refuses when a declaration is missing, and those refusals are the product. But a
developer meeting the platform for the first time hits them within five minutes, and from
outside there is no way to tell a refusal that means "you have asked me to assert something
I cannot back" from one that means "nobody has configured me yet". The first is the
platform working. The second is an empty form. Both answer `503`.

The fix is to let a development stack carry answers to the second kind -- and the whole
difficulty is making sure those answers can never be mistaken for real ones. A default that
is convenient on a laptop and silently correct-looking in a hospital is worse than no
default, because it converts a loud absence into a quiet falsehood recorded in a row that
is never updated.

WHY `MEDOS_ENV` UNSET IS NOT DEV
---------------------------------
`is_dev_deployment()` requires `MEDOS_ENV == "dev"` EXACTLY. Unset is not dev, `DEV` is not
dev, `development` is not dev.

That reads backwards at first -- surely the friendly default is the one that helps the
newcomer? It is the opposite, and the reason is which mistake actually happens. Nobody
deploys to a hospital having deliberately typed `MEDOS_ENV=dev`. What happens is that
somebody copies `docker-compose.yml`, changes the database URL and the image tag, and sets
nothing else. If unset meant dev, that deployment would accept every dev default in this
file and look healthy. Because unset means NOT dev, it refuses on the first one and names
it. The newcomer's cost is one line in a `.env`; the operator's cost of the other choice is
a false provenance claim on real patients.

WHAT THIS CANNOT DO, STATED PLAINLY
------------------------------------
Nothing here stops an operator who sets `MEDOS_ENV=dev` on a deployment holding patients.
No software can, and a module that implied otherwise would be making the same category of
promise as the defaults it replaces. What it stops is the OMISSION path -- and omission is
the failure that actually occurs.

THE MARKER IS A SUBSTRING, AND THAT IS DELIBERATELY NOT THE WHOLE GUARD
------------------------------------------------------------------------
`refuse_dev_value_outside_dev` catches any value CONTAINING `DEV_MARKER`. That is a shape
check, and register entry 97 is the standing lesson about shape checks: `2.25.` was
accepted as a covered UID root because the shape looked right, and it turned out to belong
to no authority, so it covered nothing and admitted everything. A guard that only asks
"does this value look like a dev value" has the same defect -- an unmarked stand-in sails
through.

So the enumeration half lives in `tests/gate/test_dev_mode_cannot_reach_production.py`:
every variable in `GUARDED_DECLARATIONS` must either carry the marker or appear in
`MARKER_EXEMPT` with a written reason. A variable in neither class fails the gate. The
substring check is what refuses at runtime; the enumeration is what stops somebody adding
a seventh declaration with a friendly unmarked default.

Spec: MOS-REL-032. Register entries 82, 97.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Final

__all__ = [
    "DEV_MARKER",
    "ENV_VAR",
    "GUARDED_DECLARATIONS",
    "MARKER_EXEMPT",
    "is_dev_deployment",
    "refuse_dev_value_outside_dev",
    "offending_declarations",
]

#: The variable that says what kind of deployment this is. Read in exactly one place.
ENV_VAR: Final[str] = "MEDOS_ENV"

#: The substring every development default carries.
#:
#: Chosen to be unmistakable in a log, a screenshot, a bug report or a copied `.env`. It
#: is a sentence and not a token because the person who needs to read it may be meeting
#: this platform for the first time, in a hurry, looking at a value they did not set.
DEV_MARKER: Final[str] = "dev-stack-not-for-patients"


#: Every declaration a development stack may answer on the operator's behalf.
#:
#: CLOSED, and closed on purpose. The gate asserts that each of these either carries
#: `DEV_MARKER` in its shipped default or appears in `MARKER_EXEMPT` below -- so adding a
#: seventh declaration with a convenient unmarked default turns CI red rather than
#: quietly widening what a dev stack is allowed to assert.
GUARDED_DECLARATIONS: Final[tuple[str, ...]] = (
    "MEDOS_TENANT_SALT",
    "MEDOS_DEID_PROVENANCE",
    "MEDOS_API_VIEWER_AUTHORIZATION",
    "MEDOS_TRAINING_ENVIRONMENT",
    "MEDOS_ANNOTATION_CAMPAIGN_DIR",
    "MEDOS_SEAL_STORE",
    "MEDOS_EVIDENCE_BUCKET",
)

#: Declarations that must NOT carry the marker, each with the reason.
#:
#: An exemption is a claim that a value is equally true in production, and it is written
#: down so that it can be argued with. Two kinds qualify and no others:
#:
#:   CONFIGURATION -- a choice about where bytes go. `s3` is as correct in a hospital as
#:   on a laptop, and marking it would make a production deployment refuse its own object
#:   store for no reason.
#:
#:   A CLAIM ABOUT DATA -- bounded by what the archive actually holds rather than by which
#:   environment is running. Marking it would be the wrong guard on the wrong axis:
#:   `MEDOS_DEID_PROVENANCE` is false the moment an undeclared study is ingested, whether
#:   the deployment calls itself dev or not, and
#:   `tests/integration/test_deid_provenance_declaration.py` is what holds it.
MARKER_EXEMPT: Final[Mapping[str, str]] = {
    "MEDOS_SEAL_STORE": (
        "configuration, not an assertion: where sealed manifest objects are written is a "
        "deployment choice that is equally true in production. A marker here would make a "
        "real deployment refuse its own object store."
    ),
    "MEDOS_EVIDENCE_BUCKET": (
        "configuration, not an assertion: the bucket evidence objects are written to is a "
        "deployment choice, and the same name is as correct in a hospital as on a laptop. "
        "What must be true of evidence is guarded by the digests over its contents, not by "
        "where the bytes happen to live."
    ),
    "MEDOS_DEID_PROVENANCE": (
        "a claim about DATA, not about the environment. It becomes false when a study "
        "outside its declared roots is ingested, which can happen on any deployment. It is "
        "guarded by coverage of the actual holdings -- "
        "tests/integration/test_deid_provenance_declaration.py -- and an environment marker "
        "would be a guard on the wrong axis that made the real one look redundant."
    ),
}


class DevValueOutsideDevDeployment(RuntimeError):
    """A development default reached a deployment that has not declared itself dev.

    Deliberately NOT a subclass of anything that a request handler already catches. It is
    raised at startup and at the first read, and it should stop the process rather than
    become a 503 on one endpoint while the rest of the deployment carries on.
    """


def is_dev_deployment(env: Mapping[str, str] | None = None) -> bool:
    """True only when `MEDOS_ENV` is exactly `dev`.

    Exact, case-sensitive, and with no synonyms. Accepting `development` or `DEV` would
    mean guessing at intent, and the whole value of this predicate is that it never
    guesses: either somebody wrote `dev`, or this is not a dev deployment.
    """
    source = os.environ if env is None else env
    return source.get(ENV_VAR) == "dev"


def refuse_dev_value_outside_dev(
    variable: str,
    value: str | None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Raise if `value` is a development default and this is not a development deployment.

    Called as the FIRST statement after each loader reads its variable. Placing it there
    rather than at the end matters: every one of those loaders documents "Raises
    DeploymentNotDeclared; never returns a default", and a check that runs before any
    interpretation makes each loader strictly stricter without touching that promise.

    Silent when the value is absent -- an absent declaration is the loader's own business
    and it already refuses it with a better message than this function could write.
    """
    if not value or DEV_MARKER not in str(value):
        return
    if is_dev_deployment(env):
        return
    source = os.environ if env is None else env
    declared = source.get(ENV_VAR)
    shown = "unset" if declared is None else repr(declared)
    raise DevValueOutsideDevDeployment(
        f"{variable} carries the development marker {DEV_MARKER!r}, and this deployment "
        f"has not declared itself a development stack ({ENV_VAR}={shown}).\n"
        f"\n"
        f"  That value exists so a laptop can run the whole platform without an operator "
        f"answering questions only a real deployment can answer. It is not true of a "
        f"deployment that holds patients, and this refusal is what stops it being "
        f"recorded as though it were.\n"
        f"\n"
        f"  If this IS a development stack, set {ENV_VAR}=dev.\n"
        f"  If it is not, replace {variable} with the value that is true here. "
        f"`medos doctor` lists every declaration and who owns it."
    )


def offending_declarations(env: Mapping[str, str] | None = None) -> list[str]:
    """Every guarded variable currently holding a dev value on a non-dev deployment.

    WHY A STARTUP SWEEP AND NOT ONLY A PER-READ REFUSAL. A per-read check is lazy by
    construction: a production deployment carrying a development salt would start
    healthily, serve for weeks, and discover it at the first seal -- by which time
    `patient_key` has been computed under a published secret on every row written since.
    This enumerates them all at boot so the answer arrives once, in front of whoever
    started the process, naming every variable at the same time.
    """
    source = dict(os.environ if env is None else env)
    if is_dev_deployment(source):
        return []
    return [
        variable
        for variable in GUARDED_DECLARATIONS
        if DEV_MARKER in str(source.get(variable, ""))
    ]
