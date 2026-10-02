# SPDX-License-Identifier: Apache-2.0
"""The release gates' contract with the specification, asserted by the ordinary suite.

WHY THIS IS A UNIT TEST AND NOT PART OF ANY GATE
-------------------------------------------------
`tests/gate/conftest.py` refuses a gate run that collects only some of a release row's
checks. That guard cannot fire if `tests/gate/` is deleted, renamed, or never reached --
its own conftest would not be loaded. `pytest -m gate_0_1_0` would then collect nothing and
exit 5, which is non-zero but arrives with a summary line that reads like a clean run, and
which some CI treats as success. The two failures this repository has already had were both
of that shape.

So gate completeness is also a property of the DEFAULT suite, asserted here against the
specification text rather than against a list this repository maintains. The check names are
PARSED out of `docs/spec/15-delivery.md` §15.1.2's release rows: if a future spec revision
renames a check or adds one, this test fails and names it, which is the behaviour
`MOS-REL-012` asks for -- "an unexecuted acceptance criterion means the requirement is not
satisfied, whatever the code does".

THREE RELEASES NOW, AND THE LIST OF THEM IS THE ONE THING DECLARED BY HAND
---------------------------------------------------------------------------
§15.1.2 names four release rows and this repository has built three of them. Which three
cannot be parsed out of the specification -- the spec states what each release's gate IS,
not which release the repository has reached -- so `IMPLEMENTED` below is a deliberate
declaration and `NOT_YET` is its complement.
`test_every_release_row_is_declared_implemented_or_not_yet` makes the pair exhaustive, so a
fifth release row appearing in the spec breaks this test until somebody decides which
bucket it belongs in, rather than being silently ungated.

Everything else is still derived: the check names, the module names, the markers and the
one-module-per-check mapping all come from the parsed table.

§15.1.2 WRITES ITS CHECK NAMES IN TWO GRAMMARS, AND 0.3.0 IS THE FIRST ROW TO USE BOTH
----------------------------------------------------------------------------------------
Rows 0.1.0, 0.2.0 and 0.4.0 write every check as ``name`` (definition) -- a backticked
hyphenated token immediately followed by the parenthesis that opens its definition. The
0.3.0 cell defines four that way and then DELEGATES four:

    "... plus the four checks Chapter 17 adds to this row under `MOS-TRAIN-193`, each
     stated in observable terms and naming no product: `leakage-blocks-training`,
     `chain-equivalence`, `served-plan-equivalence`, `no-auto-promote`."

A parser that recognised only the first grammar would have silently yielded a four-check
0.3.0 gate -- and `tests/gate/conftest.py`'s completeness guard would then have been
satisfied by half a row, which is the exact failure `MOS-REL-012` is written against and
the exact failure this module exists to make impossible. So `spec_checks` recognises both
grammars AND asserts that every backticked hyphenated lowercase token in the gate cell was
claimed by one of them: a THIRD grammar in a future revision fails here by name rather than
disappearing.

ONE CHECK IN `tests/gate/` IS NOT IN §15.1.2, AND IT IS DECLARED RATHER THAN TOLERATED
----------------------------------------------------------------------------------------
`LOCAL_EXTRA` below. The spec's gate row is a floor -- `MOS-REL-004` forbids tagging while
a named check is red, and says nothing about a site adding one -- but an UNdeclared extra
module is indistinguishable from a check nobody agreed to, so the extras are enumerated
with their `MOS-REL-009` response written down. Everything else about them is derived
exactly as for a spec check: module name, marker, test-identifier prefix.

THREE CHECKS IN §15.1.2 HAVE NO MODULE, AND THAT IS A RECORDED CUT RATHER THAN A HOLE
----------------------------------------------------------------------------------------
`INAPPLICABLE` below. `MOS-UI-370` adds `refusal-completeness` to the 0.2.0 row and
`headline-partition` and `duty-separation` to the 0.3.0 row, and this repository has a
module for none of them. The three obvious responses are all wrong:

  * WRITE THE MODULES. Each would assert a property of a surface that does not exist. A
    check with no subject cannot fail honestly and cannot pass honestly either; it would
    pass vacuously on an empty DOM, which is the `zero-core-change` failure of register
    entry 68 in a new costume -- a green check measuring the wrong thing.
  * LEAVE THEM MISSING. Then `test_tests_gate_implements_exactly_the_checks_of_the_
    implemented_releases` is red with no `MOS-REL-009` response available, which the
    `LOCAL_EXTRA` argument below already identifies as the shape of red people learn to
    bypass.
  * DROP THE NAMES FROM §15.1.2. That is the original defect. `MOS-UI-370` names these
    checks; a release table that omits an element because the repository lacks it is the
    silent scope reduction `MOS-REL-006` exists to prevent.

The fourth response is the one the specification already contains. `MOS-UI-013a` settles
it for `MOS-SAFE-089a`: the OHIF toolbar button MAY be cut under `MOS-REL-009`, and where
it is cut the requirement is "inapplicable rather than failed" -- and, in the same
sentence, the cut "MUST NOT be recorded as a pass". `MOS-REL-009` response (b) uses the
identical words from the other end: a cut applies when removing the item "makes the check
INAPPLICABLE rather than merely unexecuted". So a check whose subject was cut is
inapplicable; a check whose subject is present and unverified is not.

WHAT KEEPS THAT FROM BECOMING AN EXCUSE
-----------------------------------------
The word "inapplicable" is one assertion away from "we did not write it", so an entry here
is not a sentence -- it is a claim about three other files, each of which is checked:

  1. the check MUST be one §15.1.2's row for that release actually names, so an entry
     cannot invent a check or survive the spec renaming one;
  2. every element it names as its subject MUST be recorded `CUT` in
     `tests/_support/release_criteria.py`, at tier B or C. `MOS-REL-005` forbids cutting
     a Tier A item under any circumstance, so a Tier A subject makes the entry illegal and
     the check simply unmet;
  3. that cut MUST appear in the release's Release Decision Record under "Items cut under
     `MOS-REL-009`", which is where `MOS-REL-003` requires it and where a human approver
     signs next to it;
  4. `tests/gate/` MUST NOT hold a module for the check. The day somebody builds the
     surface and writes the module, this entry stops being true and the test says so,
     rather than the module sitting unmarked and uncollected.

So declaring a check inapplicable costs a tier, a register verdict and a line in a record
an approver puts their name under. That is more expensive than writing the module would
be if the surface existed, which is the correct price.

WHAT IT DOES NOT MEAN
-----------------------
It does not mean the requirement is waived. `MOS-REL-006`'s invariant holds on every
branch: "an acceptance criterion is never cut; only scope is." `headline-partition` in
particular guards a Tier A rule (`ui030-honest-metric-rule`) that is NOT cut and cannot
be -- what was cut is every surface that renders a figure, so the rule is vacuously true
today and the check has nothing to measure. The first surface that renders a figure makes
it applicable again, and this declaration must be deleted in the same change.

TWO ACCEPTANCE CRITERIA SAY THE OPPOSITE OF THIS, AND THEY ARE NAMED RATHER THAN IGNORED
------------------------------------------------------------------------------------------
Chapter 15's own conformance item 1 says "Every gate check named resolves to a test
identifier that exists in the repository ... Fails if any contents item has no tier or any
gate name has no test." Chapter 19's acceptance check 35 says the same thing about these
three by name: "each of `refusal-completeness`, `headline-partition` and `duty-separation`
resolves to a test identifier that exists in the repository". Under both, this dict is a
failure and not a resolution.

Against them stand `MOS-UI-013a` and `MOS-REL-009`'s response (b), which make a cut item's
criterion inapplicable rather than failed, and `MOS-REL-012`, which is the reason a
vacuous pass would be worse than an absent check: a module asserting "no figure in a
headline position comes from a selection partition" against a repository that renders no
figures would go green, and a green check is quotable. The two positions cannot both be
satisfied while the surfaces are cut, and which gives way is the decision of the chapters'
owners rather than of this file -- so this file takes the reading that refuses to
manufacture evidence, and register entry 77 records the conflict so the other reading
stays available. If it is chosen, the consequence is concrete and this paragraph is where
it is written down: the three modules get written, they assert over an empty surface set,
and `INAPPLICABLE` is deleted.

Needs no container: it reads five files.

Spec: MOS-REL-003, MOS-REL-004, MOS-REL-005, MOS-REL-006, MOS-REL-008, MOS-REL-009,
MOS-REL-012, MOS-TRAIN-193, MOS-UI-013a, MOS-UI-370.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests._support.release_criteria import by_slug

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "docs" / "spec" / "15-delivery.md"
GATE_DIR = REPO_ROOT / "tests" / "gate"
RELEASES_DIR = REPO_ROOT / "docs" / "releases"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: The heading `MOS-REL-003` requires in every Release Decision Record.
CUT_SECTION = "## Items cut under MOS-REL-009"

#: release -> the pytest marker that selects its gate. THE ONE HAND-MAINTAINED FACT in this
#: module, because "which release has this repository built" is not a question the
#: specification answers.
IMPLEMENTED: dict[str, str] = {
    "0.1.0": "gate_0_1_0",
    "0.2.0": "gate_0_2_0",
    "0.3.0": "gate_0_3_0",
}

#: The release rows §15.1.2 names that this repository has NOT built yet. Their checks MUST
#: NOT appear in `tests/gate/`: a module for `unattended-arrival` sitting there before 0.4.0
#: is claimed is a check nobody agreed to gate a release on, and it would make a gate run
#: red for a reason `MOS-REL-009` has no response for.
NOT_YET: tuple[str, ...] = ("0.4.0",)

#: release -> checks this repository added to that release's gate row ON TOP of §15.1.2,
#: each with the reason and the `MOS-REL-009` response that makes it answerable when red.
#:
#: This dict is the ONLY route by which `tests/gate/` may hold a module the specification
#: does not name. It is deliberately hostile to growth: every entry is argued here, in the
#: same file that refuses every unargued one.
LOCAL_EXTRA: dict[str, dict[str, str]] = {
    "0.3.0": {
        "declared-permissions-are-enforced": (
            "`medos/api/v1/routes.core.yaml` declared `job.create` on POST /api/v1/jobs and "
            "`job.read` on the job-read routes, and not one of them checked anything. "
            "Eighteen routes across three modules were in that state. The registry even "
            "carried the admission in a comment -- 'declared below and enforced nowhere' "
            "-- and four rows said `enforced_in_code: false`, so the gap was known, "
            "written down, and still open. It stayed open because `enforced_in_code` was "
            "A FIELD SOMEBODY TYPED rather than a fact anybody derived, and because "
            "`_require` existed as four separate definitions in four route modules: the "
            "check was not something the codebase HAD, it was something each module "
            "remembered to write, and the module that forgot is the one serving the only "
            "job-creation path in the platform (MOS-API-001). This check derives the "
            "claim instead -- it reads both registries through `tools.contracts."
            "load_routes` and asserts the bound handler names the declared permission -- "
            "so a route added tomorrow is covered the moment its row exists. It is a "
            "local extra rather than a 15.1.2 row because MOS-API-005 and MOS-UI-006 "
            "state the obligation per route and no release table names a check for it; "
            "the spec's floor is silent here and this is a site adding one. MOS-REL-009 "
            "response when red: NOT a cut. A declared-and-unenforced permission is "
            "MOS-REL-050's control that looks like an enforcement point and enforces "
            "nothing, which that requirement calls worse than its absence -- the "
            "response is to add the check to the named handler, or to delete the "
            "`permission:` from the row and accept in writing that the route is open. "
            "Cutting the gate would restore the exact condition it was written to end."
        ),
        "core-train-boundary": (
            "MedicalOS ships as two deployables -- Core, the PACS-and-models service, "
            "and Train, which adds the model-preparation surface -- and the whole value "
            "of Core is a claim about what it CANNOT do. A claim like that decays "
            "silently: one convenience import of `medos.training` from a core module "
            "changes nothing at run time, breaks no test, and quietly puts the cohort, "
            "seal and conversion machinery back into the address space of a service "
            "whose entire pitch is that it does not carry them. Route counts cannot see "
            "it, because the import happens whether or not a router is mounted. So this "
            "check walks the STATIC import closure of `medos.api.app` and fails if it "
            "reaches the training plane, reading source rather than `sys.modules` -- an "
            "import inside a function body is still a path, and a deferred import is "
            "the most likely shape of an accidental one. It also asserts the cheap "
            "direction holds: Train is Core PLUS routers, never a fork, so Core's "
            "served set must remain a strict subset. MOS-REL-009 response when red: "
            "this is a Tier A structural failure of the SPLIT rather than of any "
            "release's contents -- the response is to move the shared thing into a "
            "package both planes depend on, as `medos/medos/core/statements.py` was created "
            "to do for one string, and never to widen the check."
        ),
        "migration-drift": (
            "Learned the hard way on this project. The 0.2.0 gate was fully green while "
            "the LIVE deployment had none of 0.2.0's tables: every gate check that needs "
            "a schema builds a throwaway database and applies the migrations to it from "
            "empty, which proves the migration SET is applicable and proves nothing "
            "whatever about the database the release actually runs against. "
            "MOS-STORE-214's ledger exists precisely so that question has an answer, and "
            "nothing was asking it. MOS-REL-009 response when red: this is a Tier A "
            "failure of the deployment rather than of the contents -- the response is to "
            "run the migrations, not to cut scope, and the check names the exact versions "
            "that are missing, extra or digest-drifted so that response is one command."
        ),
        "capability-reachable": (
            "Register entry 68 names this check in terms and ends by recording that it "
            "does not exist. Two defects shipped behind a green gate. 0.3.0 added "
            "`lung_nodule` under medos/services/ touching no core file -- `zero-core-change` "
            "passed and was right -- while four places derived the served set from the "
            "platform singleton `medos.capabilities.REGISTRY` instead of from this "
            "deployment's configuration, so the capability was admitted nowhere, "
            "resolved as `capability_unknown`, and unpublishable against MOS-REG-029. "
            "Then the compose file defaulted MEDOS_CAPABILITY_PROVIDERS to `lung_nodule` "
            "while medicalos-config.js still offered three capabilities, and MOS-SAFE-089a "
            "makes that toolbar button the only job-creation path a reader has. Neither "
            "was visible to any check in the row: `zero-core-change` compares a diff's "
            "PATH SET, and a path set can see neither reachability nor a list inside a "
            "config file. So this check asks the one question no other one does -- is a "
            "capability this deployment serves admitted, resolved, published against, "
            "executed and offered to a reader, in one deployment, from one configuration. "
            "Its INPUT is read out of the running containers with `docker inspect` rather "
            "than out of docker-compose.yml, because both defects were states in which "
            "every in-process answer agreed and the deployment did not. Its outputs are "
            "read in two places and the module docstring names which is which rather than "
            "claiming one: admission comes from the deployed API over HTTP, execution "
            "from the deployed worker via the deployed database, and the offered list "
            "from the bytes the medos-web container serves -- while the section 6.6 "
            "vocabulary and the MOS-REG-029 publish rule have no deployed surface to "
            "drive at all and run this checkout's code in-process, configured from the "
            "containers. Property 4 refuses to pass on a MOS-EXEC-053 replay, because a "
            "replay re-reads an execution by whatever worker image was running then, "
            "which is exactly the state the original defect was invisible in. "
            "MOS-REL-009 response when red: make the deployment serve what it says it "
            "serves; do NOT cut the capability. This is a Tier A failure of the "
            "deployment rather than of the release's contents, so the slip rule's cut "
            "list does not apply -- a capability that is configured, executed and "
            "unreachable is worse than one nobody enabled, because the operator believes "
            "it runs. The failure names which of the five properties broke and for which "
            "capability id, and each has one response: extend MEDOS_CAPABILITY_PROVIDERS "
            "or the image's medos/services/catalogue.py so the API admits it; fix the caller "
            "that assembled a served set from `medos.capabilities.REGISTRY` rather than "
            "from `medos.capabilities.providers`; add the id to medicalos-config.js's "
            "`capabilities` array so a reader can submit it."
        ),
    }
}

@dataclass(frozen=True)
class Inapplicable:
    """One §15.1.2 check whose subject was cut, so it is inapplicable, not failed."""

    #: Criterion slugs in `tests/_support/release_criteria.py`. Every one MUST be `CUT`
    #: at tier B or C, and MUST be named in that release's Release Decision Record.
    subjects: tuple[str, ...]
    #: Why the check has no subject, and what makes it applicable again.
    reason: str


#: release -> checks §15.1.2's row names whose subject this release CUT under
#: `MOS-REL-009`. See the module docstring for the whole argument; the four conditions it
#: lists are asserted by `test_every_inapplicable_check_is_a_recorded_cut_and_not_a_hole`.
#:
#: This dict is the ONLY route by which a check §15.1.2 names for an implemented release
#: may have no module in `tests/gate/`. It is as hostile to growth as `LOCAL_EXTRA`, and
#: for the mirror-image reason: that dict lets a check exist that the spec does not name,
#: this one lets a check the spec names not exist, and both are ways for the gate and the
#: specification to stop meaning the same thing.
INAPPLICABLE: dict[str, dict[str, Inapplicable]] = {
    "0.2.0": {
        "refusal-completeness": Inapplicable(
            subjects=("ui020-refusal-catalogue", "ui030-refusal-catalogue"),
            reason=(
                "The check enumerates every gate a surface can reach and fails when one "
                "has no three-part refusal record. MEASURED 2026-09-16, WHEN THIS WAS "
                "WRITTEN AND BEFORE THE TRAINING CONSOLE WAS BUILT: no file in this "
                "repository contained `what_is_wrong`, `why_it_blocks` or "
                "`what_would_resolve_it`, and the only refusal text was two frozen "
                "objects in medos/web/ohif-extension/src/core/render.js covering the "
                "REJECTED and FAILED terminal states. THAT WENT FALSE ONCE AND IS TRUE "
                "AGAIN: medos/web/training-console/src/refusals/catalogue.js carried 75 "
                "three-part records between the console's construction and its deletion "
                "on 2026-10-02, when the engineering surface was withdrawn with chapter "
                "19 section 19.3 (register entries 83 and 150). The question entry 83 "
                "posed -- whether a catalogue on that surface discharges this cut -- is "
                "answered by the withdrawal: there is no surface, so there is nothing to "
                "enumerate, and the cut describes the repository again. The date is why "
                "the measurement stays: it is a correct statement about 2026-09-16. The "
                "catalogue was placed at 0.1.0 as a seed set, cut, returned at 0.2.0 by "
                "MOS-REL-005, cut again, and extended and cut a third time at 0.3.0 -- "
                "MOS-UI-370 extends this one check's enumeration at 0.3.0 rather than "
                "adding a second, so both cuts are subjects of this single entry. "
                "MOS-REL-009 response when the day comes: none, because the response was "
                "already taken -- this is a Tier B cut recorded in docs/releases/0.2.0.md "
                "and docs/releases/0.3.0.md, not a red check awaiting a decision. Writing "
                "the catalogue is what deletes this entry, and the module MUST be written "
                "in the same change; a catalogue with no completeness check is how "
                "SPLIT_L1_PATIENT_IN_TWO_PARTITIONS ends up in a sentence a clinician "
                "reads."
            ),
        ),
    },
    "0.3.0": {
        "headline-partition": Inapplicable(
            subjects=(
                "ui030-configuration-search-surface",
                "ui030-search-diagnostics-block",
                "ui030-promotion-screen",
                "ui030-model-picker",
            ),
            reason=(
                "The check asserts that no figure in a headline position comes from a run "
                "whose partition a selection touched. Nothing in this repository renders "
                "a figure: medos/web/ holds the OHIF extension and nothing else, and its "
                "renderer emits job state, series selection, provenance fields and stored "
                "objects -- no value read from capability_claims or evaluation_runs, no "
                "summary card, no list column named for a capability's primary metric, no "
                "picker tooltip, no export header. Every surface that would carry one was "
                "cut from this release. READ THIS ONE CAREFULLY: the RULE is Tier A "
                "(`ui030-honest-metric-rule`) and is NOT cut, because MOS-REL-005 forbids "
                "cutting a Tier A item; it is in force and vacuously true. What is cut is "
                "the four surfaces, and a check with no surface to walk is inapplicable "
                "under the MOS-UI-013a precedent rather than passing on an empty DOM -- "
                "which is the outcome that would actually be dangerous, because a vacuous "
                "green here would be quoted as evidence that the honest-metric rule is "
                "enforced. MOS-REL-009 response: the cuts are Tier B and Tier C and are "
                "recorded in docs/releases/0.3.0.md; the first surface that renders a "
                "figure deletes this entry and needs the module in the same change. "
                "WENT STALE ONCE AND IS TRUE AGAIN: medos/web/training-console existed "
                "between its construction and its deletion on 2026-10-02, when the "
                "engineering surface was withdrawn with section 19.3 (register entries 83 "
                "and 150). Its first sentence -- no figure read from capability_claims or "
                "evaluation_runs -- held throughout the console's life, because the "
                "candidate projection carries no aggregate metric, and the clause saying "
                "medos/web/ holds the OHIF extension and nothing else is true again, "
                "because the console is gone. See "
                "docs/spec/99-known-inconsistencies.md entry 83."
            ),
        ),
        "duty-separation": Inapplicable(
            subjects=("ui030-training-submit-surface", "ui030-promotion-screen"),
            reason=(
                "The check asserts that no single authenticated FLOW both submits a "
                "TrainingRun and performs an approval act. There is no flow: neither the "
                "training submit surface nor the promotion screen exists, and no surface "
                "in this repository can reach either act. The platform half of the same "
                "separation IS gated and green in this row -- `no-auto-promote` asserts "
                "that medos.training's import closure reaches neither medos.promotion nor "
                "the deployment mutator, that every promotion act requires actor.kind == "
                "'user', and that the deployments trigger refuses auto_promote -- so what "
                "is unverified is specifically the SURFACE property MOS-UI-315 and "
                "MOS-UI-321 add on top: that one screen never offers both controls and "
                "that a person performing more than one act sees a persistent statement "
                "naming both roles. MOS-REL-009 response: both subjects are Tier B cuts "
                "recorded in docs/releases/0.3.0.md and both return in 0.4.0 under "
                "MOS-REL-005, which is the release this entry must be deleted in. "
                "WENT STALE ONCE AND IS TRUE AGAIN: medos/web/training-console shipped "
                "the training "
                "submit surface between its construction and its deletion on "
                "2026-10-02, when the engineering surface was withdrawn with section 19.3 "
                "and the 0.4.0 row discharged the MOS-REL-005 return by that withdrawal "
                "rather than by a build (register entries 83 and 150). 'There is no flow' "
                "is true again: no surface in this repository can reach either a "
                "TrainingRun submission or an approval act. The promotion screen of "
                "MOS-UI-318 is still absent and MOS-UI-170 keeps it on another "
                "surface. See docs/spec/99-known-inconsistencies.md entry 83; the change "
                "that deletes this entry is still the surface's return, now under a new "
                "minor version of chapter 19 rather than the 0.4.0 build."
            ),
        ),
    },
}


#: A gate check name as §15.1.2 writes it in its FIRST grammar: a backticked
#: lowercase-hyphenated token immediately followed by the parenthesis that opens its
#: definition. The same cells also contain `results`, `SeriesInstanceUID`, `REJECTED`,
#: `ModelVersion` and `FAILED` in backticks, which this pattern excludes by requiring both
#: the hyphen and the following `(`.
CHECK_RE = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`\s*\(")

#: The SECOND grammar, which only the 0.3.0 row uses: a trailing colon followed by a
#: comma-separated run of backticked names that another chapter defines. Anchored on the
#: colon-then-backtick so that a name in ordinary prose cannot be swept up by it.
DELEGATED_RE = re.compile(
    r":\s*((?:`[a-z0-9]+(?:-[a-z0-9]+)+`(?:\s*(?:,|and)\s*)?)+)\s*\."
)

#: Every backticked lowercase-hyphenated token, whatever follows it. Used ONLY to assert
#: that the two grammars above between them claimed all of them -- see the module
#: docstring. `MOS-TRAIN-193` and the other requirement ids are upper case and so are not
#: matched; `clinical_use_mode` and `tenant_id` carry underscores, not hyphens.
ANY_HYPHENATED_RE = re.compile(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`")


def spec_releases() -> list[str]:
    """Every release §15.1.2 gives a row to, in table order."""
    rows = re.findall(
        r"^\| \*\*(\d+\.\d+\.\d+)\*\*",
        SPEC.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert rows, "no release rows parsed out of docs/spec/15-delivery.md §15.1.2"
    return rows


def gate_cell(release: str) -> str:
    """The gate column of §15.1.2's row for `release`, verbatim."""
    rows = [
        line
        for line in SPEC.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"| **{release}**")
    ]
    assert len(rows) == 1, (
        f"docs/spec/15-delivery.md has {len(rows)} rows for release {release} in §15.1.2; "
        f"the gate cannot be derived from an ambiguous table"
    )
    cells = [c.strip() for c in rows[0].split("|")]
    # `| release | contents | gate |` -> a leading and a trailing empty cell.
    assert len(cells) == 5, f"§15.1.2's row for {release} has {len(cells) - 2} columns, not 3"
    return cells[-2]


def spec_checks(release: str = "0.1.0") -> list[str]:
    """The checks §15.1.2's gate column names for `release`, in table order.

    BOTH grammars, and nothing left over. See the module docstring: the 0.3.0 row defines
    four checks inline and delegates four to chapter 17, and a parser that saw only the
    first grammar would have reported a four-check 0.3.0 gate that looked complete.
    """
    cell = gate_cell(release)
    defined = CHECK_RE.findall(cell)
    delegated = [
        name
        for run in DELEGATED_RE.findall(cell)
        for name in re.findall(r"`([a-z0-9]+(?:-[a-z0-9]+)+)`", run)
    ]
    names = defined + [n for n in delegated if n not in defined]
    assert names, f"no check names parsed out of §15.1.2's gate column for {release}"

    unclaimed = [n for n in ANY_HYPHENATED_RE.findall(cell) if n not in names]
    assert not unclaimed, (
        f"§15.1.2's gate column for {release} backticks {sorted(set(unclaimed))}, which "
        f"neither grammar this module knows claimed. Either they are checks written in a "
        f"third form -- in which case this parser must learn it, because an unparsed "
        f"check is an ungated one (MOS-REL-012) -- or they are prose, in which case say "
        f"so here explicitly."
    )
    return names


def local_extra(release: str) -> list[str]:
    """Checks this repository added to `release`'s gate row on top of §15.1.2."""
    return sorted(LOCAL_EXTRA.get(release, {}))


def inapplicable(release: str) -> list[str]:
    """Checks §15.1.2 names for `release` whose subject that release cut."""
    return sorted(INAPPLICABLE.get(release, {}))


def all_checks(release: str) -> list[str]:
    """Everything `pytest -m <release marker>` must collect.

    §15.1.2's row, MINUS the checks whose subject was cut under `MOS-REL-009`, PLUS the
    local extras. The subtraction is the `MOS-UI-013a` precedent applied to a gate row:
    a cut element's criterion is inapplicable rather than failed, so its check is not
    part of what the run must collect -- and it is subtracted HERE, in the one function
    every assertion in this module derives from, rather than special-cased per test.
    """
    dropped = set(inapplicable(release))
    return [c for c in spec_checks(release) if c not in dropped] + local_extra(release)


def module_for(check: str) -> str:
    return "test_" + check.replace("-", "_") + ".py"


def implemented_pairs() -> list[tuple[str, str]]:
    """`(release, check)` for every check this repository claims to have built."""
    return [(r, c) for r in IMPLEMENTED for c in all_checks(r)]


def test_every_release_row_is_declared_implemented_or_not_yet() -> None:
    """The two buckets above are exhaustive over §15.1.2, and disjoint.

    This is what stops the module from quietly ignoring a release. A 0.5.0 row added to the
    specification would otherwise be a gate nobody had noticed was missing -- and "nobody
    noticed" is precisely the failure `MOS-REL-012` is written against.
    """
    declared = set(IMPLEMENTED) | set(NOT_YET)
    rows = set(spec_releases())
    assert not (set(IMPLEMENTED) & set(NOT_YET)), (
        "a release is declared both implemented and not yet implemented"
    )
    assert declared == rows, (
        f"§15.1.2 names {sorted(rows)} and this module declares {sorted(declared)}. "
        f"Add the new release to IMPLEMENTED (with its modules) or to NOT_YET."
    )


def test_tests_gate_implements_exactly_the_checks_of_the_implemented_releases() -> None:
    """One module per named check, named after it, and nothing extra.

    "Nothing extra" is asserted as well as "nothing missing". A `tests/gate/test_foo.py`
    that no spec row names is a check nobody agreed to gate a release on, and it would make
    a gate run red for a reason `MOS-REL-009` has no response for.
    """
    expected = {c: module_for(c) for _release, c in implemented_pairs()}
    assert len(expected) == sum(len(all_checks(r)) for r in IMPLEMENTED), (
        "two release rows name the same check; the module mapping would be ambiguous"
    )
    present = {p.name for p in GATE_DIR.glob("test_*.py")}

    missing = {c: f for c, f in expected.items() if f not in present}
    assert not missing, (
        "the specification names checks that tests/gate does not implement: "
        + ", ".join(f"{c} (expected tests/gate/{f})" for c, f in sorted(missing.items()))
        + ". Write the module, or -- only if the element the check measures was CUT "
        "under MOS-REL-009 -- declare it in INAPPLICABLE, which costs a tier, a CUT "
        "verdict in tests/_support/release_criteria.py and a line in the Release "
        "Decision Record."
    )
    extra = present - set(expected.values())
    assert not extra, (
        f"tests/gate holds modules no implemented §15.1.2 gate row names and no "
        f"LOCAL_EXTRA entry declares: {sorted(extra)}. Delete the module, or add the "
        f"check to LOCAL_EXTRA with the reason it is gated and the MOS-REL-009 response "
        f"for the day it goes red."
    )


def test_the_gate_packages_own_row_table_is_the_specifications() -> None:
    """`tests/gate/conftest.py`'s `RELEASES` == the spec rows plus the declared extras.

    THE LOOP THIS CLOSES. The gate package resolves a check name to a module and a marker
    from its own `RELEASES` table, and its completeness guard counts against that table.
    So a check present in §15.1.2 but missing from `RELEASES` produces the exact failure
    the guard exists to prevent, one level up: the module is collected (it carries the
    marker), the guard does not recognise it, and `-m gate_0_X_0` reports "N of N checks"
    for a row that is short by one. Every other assertion in this file would pass.

    Parsed with `ast` and not imported, for the reason the module docstring gives: the
    gate's conftest pulls in `requests` and the deployment's constants, and a unit test
    that needs those to check a table is in the wrong directory.
    """
    conftest = GATE_DIR / "conftest.py"
    tree = ast.parse(conftest.read_text(encoding="utf-8"), filename=str(conftest))

    #: module-level `NAME = <literal>` bindings, so `RELEASES`' `Name` nodes resolve.
    constants: dict[str, object] = {}
    releases_node: ast.Dict | None = None
    for node in tree.body:
        # Both spellings: `NAME = ...` and the annotated `NAME: T = ...` the gate's
        # conftest actually uses. A parser that knew only the first found no RELEASES and
        # said so -- which is the right failure, but for the wrong reason.
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if value is None:
            continue
        if name == "RELEASES" and isinstance(value, ast.Dict):
            releases_node = value
            continue
        try:
            constants[name] = ast.literal_eval(value)
        except (ValueError, TypeError):
            continue

    assert releases_node is not None, (
        "tests/gate/conftest.py no longer defines a module-level `RELEASES` dict, so the "
        "gate package's row table cannot be compared with §15.1.2"
    )

    def resolve(node: ast.expr) -> object:
        """Literals, module-level names, and tuples that mix the two.

        `RELEASES`' values are `(GATE_CHECKS, "0.1.0")` -- a tuple whose first element is
        a Name, which `ast.literal_eval` refuses outright. So tuples are walked rather
        than evaluated whole.
        """
        if isinstance(node, ast.Name):
            assert node.id in constants, f"RELEASES references unknown name {node.id!r}"
            return constants[node.id]
        if isinstance(node, ast.Tuple):
            return tuple(resolve(element) for element in node.elts)
        return ast.literal_eval(node)

    declared: dict[str, tuple[str, tuple[str, ...]]] = {}
    for key, value in zip(releases_node.keys, releases_node.values):
        assert key is not None
        marker = resolve(key)
        checks, release = resolve(value)  # type: ignore[misc]
        declared[str(release)] = (str(marker), tuple(checks))  # type: ignore[arg-type]

    assert set(declared) == set(IMPLEMENTED), (
        f"tests/gate/conftest.py gates releases {sorted(declared)} and this module "
        f"declares {sorted(IMPLEMENTED)} implemented"
    )
    for release, (marker, checks) in sorted(declared.items()):
        assert marker == IMPLEMENTED[release], (
            f"the gate package selects release {release} with {marker!r}; this module "
            f"declares {IMPLEMENTED[release]!r}"
        )
        assert sorted(checks) == sorted(all_checks(release)), (
            f"release {release}: tests/gate/conftest.py gates {sorted(checks)} and "
            f"§15.1.2 plus LOCAL_EXTRA name {sorted(all_checks(release))}. The gate's "
            f"completeness guard counts against its own table, so a check missing from it "
            f"is a check the guard will report as present."
        )


def test_every_locally_added_check_is_declared_argued_and_not_already_in_the_spec() -> None:
    """`LOCAL_EXTRA` is the one permitted route past the "nothing extra" rule above.

    A gate check that no requirement names has to answer two questions before it may make
    a release red: why does it exist, and what does a reviewer DO when it fails
    (`MOS-REL-009`). An entry that cannot answer both is a check that gets bypassed the
    first time it fires, which is worse than not having it.

    Also asserts the extras do not shadow a spec check. A `LOCAL_EXTRA` entry duplicating
    a §15.1.2 name would give one module two owners and `all_checks` would yield it twice,
    so the count assertion above would fail confusingly rather than here, by name.
    """
    for release, entries in LOCAL_EXTRA.items():
        assert release in IMPLEMENTED, (
            f"LOCAL_EXTRA adds checks to release {release}, which this module does not "
            f"declare IMPLEMENTED. A gate nobody runs is not a gate."
        )
        named_by_spec = set(spec_checks(release))
        for check, reason in entries.items():
            assert check not in named_by_spec, (
                f"{check!r} is already named by §15.1.2's {release} row; it is not an "
                f"addition and must not be declared as one"
            )
            assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)+", check), (
                f"{check!r} does not match the check-name grammar §15.1.2 uses, so it "
                f"would not resolve to a module or a test-identifier prefix"
            )
            assert check not in INAPPLICABLE.get(release, {}), (
                f"{check!r} is declared both a local addition and inapplicable for "
                f"{release}; a check cannot be one this repository added AND one the "
                f"specification names whose subject was cut"
            )
            assert "MOS-REL-009" in reason and len(reason) > 200, (
                f"the justification for {check!r} does not name the MOS-REL-009 response "
                f"for the day it goes red, or is too short to be one"
            )


def _cut_section(release: str) -> str:
    """The "Items cut under MOS-REL-009" section of a Release Decision Record."""
    record = RELEASES_DIR / f"{release}.md"
    assert record.exists(), (
        f"{record} does not exist; MOS-REL-003 requires a Release Decision Record per "
        f"release and a cut has nowhere to be recorded without one"
    )
    text = record.read_text(encoding="utf-8")
    start = text.find(CUT_SECTION)
    assert start != -1, f"{record} has no {CUT_SECTION!r} section"
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_every_inapplicable_check_is_a_recorded_cut_and_not_a_hole() -> None:
    """The four conditions the module docstring sets, asserted one by one.

    This is the test that makes `INAPPLICABLE` cost something. Without it the dict is a
    place to write "we did not build it" in a word that sounds like a decision, and the
    gate row and the repository would drift apart exactly as §15.1.2 and chapter 19 did
    for three releases -- which is the defect the ingestion of `MOS-UI-365` repairs and
    which this dict would otherwise reintroduce at one remove.
    """
    for release, entries in INAPPLICABLE.items():
        assert release in IMPLEMENTED, (
            f"INAPPLICABLE declares checks for {release}, which this module does not "
            f"declare IMPLEMENTED. An unbuilt release cuts nothing; NOT_YET already says "
            f"its checks have no modules."
        )
        named_by_spec = set(spec_checks(release))
        recorded_cuts = _cut_section(release)
        for check, entry in entries.items():
            # 1. the spec names it.
            assert check in named_by_spec, (
                f"{check!r} is declared inapplicable for {release} and §15.1.2's row for "
                f"{release} does not name it. Delete the entry: a check the spec no "
                f"longer names needs no excuse."
            )
            assert entry.subjects, f"{check!r} names no cut element as its subject"
            # 4. no module pretends otherwise.
            module = GATE_DIR / module_for(check)
            assert not module.exists(), (
                f"{module} exists while {check!r} is declared inapplicable for "
                f"{release}. Both cannot be true: either the subject was built, in which "
                f"case delete the INAPPLICABLE entry and the release-criteria CUT with "
                f"it, or the module asserts a property of something that is not there."
            )
            for slug in entry.subjects:
                criterion = by_slug(slug)  # 2. the register knows it...
                assert criterion.verdict == "CUT", (
                    f"{check!r} is declared inapplicable because {slug!r} was cut, but "
                    f"the register records {slug!r} as {criterion.verdict}. A check is "
                    f"inapplicable only where its subject is CUT (MOS-UI-013a); "
                    f"otherwise it is unverified, which is a different word."
                )
                assert criterion.tier in ("B", "C"), (
                    f"{slug!r} is tier {criterion.tier} and is marked CUT. MOS-REL-005 "
                    f"forbids cutting a Tier A item under any circumstance, so "
                    f"{check!r} cannot be inapplicable on its account -- it is unmet, and "
                    f"retiering the item to make this pass is the one move forbidden here."
                )
                # 3. ...and so does the record a human signs.
                assert slug.removeprefix("tier-") in _cut_section(criterion.release), (
                    f"{slug!r} is CUT and does not appear in "
                    f"docs/releases/{criterion.release}.md's {CUT_SECTION!r} section. "
                    f"MOS-REL-003 requires every Tier B or Tier C item cut under "
                    f"MOS-REL-009 to be named in the Release Decision Record, and "
                    f"MOS-UI-013a forbids recording the cut as a pass."
                )
            assert "MOS-REL-009" in entry.reason and len(entry.reason) > 400, (
                f"the justification for {check!r} does not name the MOS-REL-009 position "
                f"for this check, or is too short to be one"
            )
        assert recorded_cuts.strip(), f"{release}'s cut section is empty"


def test_every_cut_in_the_register_is_named_in_its_release_decision_record() -> None:
    """The other direction: `MOS-REL-003` is not satisfied by the cuts that had a check.

    `INAPPLICABLE` only reaches the three elements a gate check happens to name. Most cut
    elements have no check at all -- the curation queue surface, the annotation campaign
    surface, the UI contribution statement -- and `MOS-REL-003` requires every one of them
    in the record regardless. Asserting only the checked ones would leave the commonest
    case unasserted, which is how "Items cut under MOS-REL-009: None" survived three
    releases of cuts in the first place.

    The record names each cut ELEMENT once. The register carries two rows for most of
    them -- one from 15.1.2's contents column and one from 15.1.3's tier table -- so the
    `tier-` prefix is stripped before the lookup rather than making a human write the
    same element twice in a document an approver reads.
    """
    from tests._support.release_criteria import cut_criteria

    missing: list[str] = []
    for criterion in cut_criteria():
        element = criterion.slug.removeprefix("tier-")
        if element not in _cut_section(criterion.release):
            missing.append(f"{criterion.release}: {element}")
    assert not missing, (
        "cut elements absent from their Release Decision Record: "
        + ", ".join(missing)
        + ". MOS-REL-003 requires the record to name any Tier B or Tier C item cut under "
        "MOS-REL-009, and an unrecorded cut is scope that shrank silently (MOS-REL-006)."
    )


@pytest.mark.parametrize("release", NOT_YET)
def test_an_unbuilt_releases_checks_have_no_modules_yet(release: str) -> None:
    """A check for a release this repository has not reached MUST NOT be sitting there.

    Not tidiness. `tests/gate/conftest.py` resolves a module name to a check and a marker;
    a `test_zero_core_change.py` present but unmarked would be collected by the DEFAULT
    suite and by neither gate, which is the worst of the three places it could be.
    """
    present = {p.name for p in GATE_DIR.glob("test_*.py")}
    premature = [c for c in spec_checks(release) if module_for(c) in present]
    assert not premature, (
        f"tests/gate implements {premature} for release {release}, which this module "
        f"declares NOT_YET. Move {release} into IMPLEMENTED once its whole row is built."
    )


@pytest.mark.parametrize(("release", "check"), implemented_pairs())
def test_each_check_carries_its_own_releases_marker(release: str, check: str) -> None:
    """The module exists, it is not empty, it is marked for the RIGHT release, and its
    tests are named after the check.

    The marker is asserted per release and not merely "some gate marker", because a 0.2.0
    module marked `gate_0_1_0` would be collected by the wrong gate: the 0.1.0 run would go
    red for a check its row does not name, and the 0.2.0 run would refuse to start for a
    check it could not find. Both failures point at the wrong place.

    Parsed with `ast` rather than by importing: importing the module would pull in
    `psycopg`, `highdicom` and the conftest's module-level constants, and a unit test that
    needs the deployment's libraries to check a naming convention is in the wrong directory.
    Comments and docstrings are dropped by the parser, so a `pytestmark` named in prose
    cannot satisfy this.
    """
    marker = IMPLEMENTED[release]
    module = GATE_DIR / module_for(check)
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))

    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "pytestmark" for t in node.targets)
    ]
    assert assignments, (
        f"tests/gate/{module.name} does not set `pytestmark`, so `pytest -m {marker}` "
        f"would silently leave the {check!r} check out of the release-{release} gate"
    )
    dumped = " ".join(ast.dump(node.value) for node in assignments)
    assert marker in dumped, (
        f"tests/gate/{module.name} does not carry `pytest.mark.{marker}`. §15.1.2 names "
        f"{check!r} in the release-{release} row and nowhere else."
    )
    wrong = [m for m in IMPLEMENTED.values() if m != marker and m in dumped]
    assert not wrong, (
        f"tests/gate/{module.name} also carries {wrong}, so it would be collected by a "
        f"gate whose row does not name {check!r}"
    )

    prefix = "test_" + check.replace("-", "_")
    tests = [
        n.name
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")
    ]
    assert tests, f"tests/gate/{module.name} defines no tests"
    off_name = [t for t in tests if not t.startswith(prefix)]
    assert not off_name, (
        f"tests/gate/{module.name} defines {off_name}, whose identifiers do not begin "
        f"with {prefix!r}. The check name IS the identifier: a gate report that does not "
        f"say which of §15.1.2's checks a failure belongs to cannot be acted on under "
        f"MOS-REL-009."
    )


@pytest.mark.parametrize("marker", sorted(IMPLEMENTED.values()))
def test_the_gate_markers_are_registered_so_a_typo_cannot_select_nothing(
    marker: str,
) -> None:
    """An unregistered marker is a warning, and `-m <typo>` then quietly selects nothing.

    `pytest -m gate_0_1_O` (capital O) would collect zero tests and, without
    `--require-stack`, print a summary that reads like a clean run. Registering the marker
    does not stop that typo, but it does stop the OPPOSITE failure: a module whose
    `pytest.mark.gate_0_X_0` was silently accepted as an unknown mark and would vanish the
    day `--strict-markers` is switched on.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    assert re.search(rf'^\s*"{marker}:', text, re.MULTILINE), (
        f"the {marker!r} marker is not registered in pyproject.toml's "
        f"[tool.pytest.ini_options] markers list"
    )


def test_a_gate_marker_is_used_only_inside_tests_gate() -> None:
    """A gate check lives in `tests/gate/` or it is not part of the gate.

    Both markers started life on a test in `tests/integration/`, written in anticipation of
    this package. Left there, they would be collected by `-m gate_0_2_0` while
    `tests/gate/conftest.py` could not resolve them to a check name -- so the completeness
    guard would count five of six, the run would print seven passes, and the arithmetic
    would never be done by anyone.
    """
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.joinpath("tests").rglob("test_*.py")):
        if GATE_DIR in path.parents:
            continue
        text = path.read_text(encoding="utf-8")
        for marker in IMPLEMENTED.values():
            if f"mark.{marker}" in text:
                offenders.append(f"{path.relative_to(REPO_ROOT)} -> {marker}")
    assert not offenders, (
        "gate markers outside tests/gate: "
        + ", ".join(offenders)
        + ". A marked test there is selected by the gate and invisible to the "
        "completeness guard in tests/gate/conftest.py."
    )


def test_no_gate_module_calls_pytest_skip_directly() -> None:
    """The skip discipline of `tests/_support/skips.py`, enforced on the gate too.

    A raw `pytest.skip` inside a release gate is the worst place in the repository for one:
    it produces an untagged skip, which the taxonomy reports as `OTHER SKIPS` and which
    `--require-stack` does not turn into a failure. An arm that did that would be
    indistinguishable from an arm that passed.
    """
    offenders: list[str] = []
    for module in sorted(GATE_DIR.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr in ("skip", "importorskip")
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "pytest"
            ):
                offenders.append(f"{module.name}:{node.lineno} pytest.{fn.attr}()")
    assert not offenders, (
        "raw skips in the release gate: "
        + ", ".join(offenders)
        + ". Use tests._support.skips.skip_infra / skip_no_data / skip_environment, so "
        "the arm is counted by name and --require-stack can fail it."
    )


def test_the_summary_names_the_interpreter_when_it_is_not_the_project_venv() -> None:
    """A declared dependency missing from a base interpreter reads like a container-only one.

    `DEVELOPMENT.md` says to create `.venv` and activate it. A shell that has not — a fresh
    non-interactive one, a tool that spawns its own — resolves `python` to whatever is first
    on PATH, where the declared dependencies simply are not. What comes back is

        ModuleNotFoundError: No module named 'psycopg'

    which reads exactly like a module that lives only in a container, because this
    repository really does arrange some that way.

    MEASURED: under a base Anaconda interpreter this tree reported "34 failed, 1059 passed"
    with 7 collection errors, every one of them `psycopg` or `nibabel`. Under
    `.venv/Scripts/python.exe` the same tree reports 1251 passed and nothing skipped. A
    hundred and fifty-eight tests were not failing — they were not running, and every
    summary line above said so in a way nobody reads as that.

    This module already exists because "a check that cannot pass and a check that never
    executes look identical from outside". This is the same thing one layer down, in the
    runner rather than in the checks, so the summary states which Python ran.
    """
    source = (REPO_ROOT / "tests" / "_support" / "skips.py").read_text(encoding="utf-8")
    assert "INTERPRETER:" in source, (
        "the summary never names the interpreter, so a run under the wrong one is "
        "indistinguishable from a run whose dependencies live in containers"
    )
    assert 'venv = pathlib.Path(__file__).resolve().parents[2] / ".venv"' in source, (
        "the check does not locate the project venv from the repository, so it cannot tell "
        "whether the interpreter running is the declared one"
    )
    assert "running_in != venv.resolve()" in source, (
        "the interpreter is named unconditionally or never compared, so the line is either "
        "noise on every correct run or absent on every wrong one"
    )


# ---------------------------------------------------------------------------------------
# A GATE CELL ABBREVIATES A REQUIREMENT, AND AN ABBREVIATION CAN COME APART FROM ITS SUBJECT
# ---------------------------------------------------------------------------------------
#
# Register entry 125. The 0.4.0 row's `second-viewer` cell read "a second, independent
# viewer -- one not named in the 0.1.0 contents and sharing no rendering implementation with
# it". Both clauses picked out a third party only while the 0.1.0 contents named OHIF.
# 15.3.1's Viewer row was amended at the SAME specification version to record BUILD, and
# from that moment the platform's own `viewer/` satisfied both clauses literally -- it is not
# named in the 0.1.0 contents, and it shares no code with OHIF. The cell admitted the one
# candidate the check exists to exclude.
#
# `MOS-IMG-158` ("independent third-party viewer") and `MOS-IMG-157a` ("independent means not
# ours") never admitted it. So the requirements were right and the ABBREVIATION had drifted --
# and nothing else in the repository defined the check: measured, no module and no entry
# in the release criteria named it, so the cell was its only definition. That is what
# made the drift load-bearing instead of cosmetic.
#
# The term is therefore required in BOTH. Watching only the cell would let the requirement be
# watered down underneath it; watching only the requirement is exactly what the specification
# was already doing when it drifted.

#: (release, check, requirement id, chapter file, the term both must carry). A row here is a
#: cell that ABBREVIATES a requirement -- not one that merely cites one.
ABBREVIATED: tuple[tuple[str, str, str, str, str], ...] = (
    ("0.4.0", "second-viewer", "MOS-IMG-158", "04-imaging-contracts.md", "third-party"),
)

#: Requirement ids a gate cell names for some reason OTHER than abbreviating them. Each
#: reason was read at its site rather than assumed, because the test below derives its
#: population FROM THE CELLS and would otherwise have the blind spot of a hand-written list.
CITED_NOT_ABBREVIATED: dict[str, str] = {
    "MOS-UI-370": (
        "the delegation authority for the checks chapter 19 ADDS to the 0.2.0 and 0.3.0 rows "
        "(`refusal-completeness`, `headline-partition`), not a requirement any cell abbreviates"
    ),
    "MOS-TRAIN-193": (
        "the same, for the four checks chapter 17 adds to the 0.3.0 row"
    ),
    "MOS-REL-012": (
        "cited in the 0.3.0 cell as the REASON a capability must ship a test before the "
        "zero-core-change allow-list widens; it states an equivalence, and no check "
        "abbreviates it"
    ),
    "MOS-IMG-157a": (
        "cited by the amended 0.4.0 cell for its 'independent means not ours' clause, which "
        "the cell carries in operative form; `MOS-IMG-158` is the requirement the check "
        "abbreviates and is the row above"
    ),
}

#: Every requirement id appearing in any gate cell, whatever the grammar.
CELL_REQUIREMENT_RE = re.compile(r"`(MOS-[A-Z]+-\d+[a-z]?)`")


def _check_definition(release: str, check: str) -> str:
    """The parenthesised definition `release`'s gate cell gives `check`, parentheses balanced.

    Asking for the term anywhere in the CELL would be nearly vacuous -- the 0.4.0 cell defines
    four checks and any of their words would satisfy it. The construct is asked for at its own
    site, which is the lesson three sibling modules in this suite carry.
    """
    cell = gate_cell(release)
    opener = f"`{check}` ("
    start = cell.index(opener) + len(opener)
    depth = 1
    for i in range(start, len(cell)):
        if cell[i] == "(":
            depth += 1
        elif cell[i] == ")":
            depth -= 1
            if depth == 0:
                return cell[start:i]
    raise AssertionError(
        f"15.1.2's {release} cell opens a definition for {check!r} and never closes it"
    )


def _requirement_text(chapter: str, req: str) -> str:
    """The paragraph of `chapter` that states `req`, up to the next requirement."""
    text = (REPO_ROOT / "docs" / "spec" / chapter).read_text(encoding="utf-8")
    m = re.search(rf"\*\*{re.escape(req)}\*\*.*?(?=\n\n\*\*MOS-|\Z)", text, re.S)
    assert m, f"{chapter} does not state {req}"
    return m.group(0)


@pytest.mark.parametrize(
    ("release", "check", "req", "chapter", "term"),
    ABBREVIATED,
    ids=[f"{r}:{c}" for r, c, _, _, _ in ABBREVIATED],
)
def test_an_abbreviating_cell_and_its_requirement_carry_the_same_term(
    release: str, check: str, req: str, chapter: str, term: str
) -> None:
    definition = _check_definition(release, check)
    assert term in definition, (
        f"15.1.2's {release} row defines {check!r} without the word {term!r}, which is the "
        f"property {req} turns on. This is register entry 125's defect exactly: the cell "
        f"defined the second viewer by two negative tests that stopped picking out a third "
        f"party the moment 15.3.1's Viewer row was amended, and the platform's own viewer "
        f"then satisfied them both."
    )
    text = _requirement_text(chapter, req)
    assert term in text, (
        f"{req} no longer says {term!r}, and 15.1.2's {release} gate cell defers to it for "
        f"exactly that. A cell that points at a requirement which has been watered down is "
        f"the same drift in the other direction."
    )


def test_the_second_viewer_cell_excludes_the_platforms_own_viewer_in_operative_form() -> None:
    """Deference alone is not enough; the exclusion has to be IN the cell.

    Measured before this module existed, nothing in the test tree named this check at all,
    so the cell is where a reader meets it. `MOS-IMG-157a` says the viewer under test
    "MUST NOT be `viewer/`, MUST NOT be built from this repository, and MUST share no source
    with it", and a reader holding only the gate row has to be able to see that.
    """
    definition = _check_definition("0.4.0", "second-viewer")
    assert "`viewer/`" in definition and "MUST NOT" in definition, (
        "15.1.2's `second-viewer` definition no longer excludes the first-party viewer in "
        "operative terms. A cell that only points at MOS-IMG-158 leaves a record quoting it "
        "unable to show what was excluded -- which is how the cell came apart in the first "
        "place."
    )


def test_every_requirement_a_gate_cell_names_is_classified() -> None:
    """The population is DERIVED from the cells, not listed here.

    A hand-written list of sites has the same blind spot as the drift it guards: the day a
    revision points a cell at a new requirement, a list would go on passing. So the ids are
    read out of the cells and each must be either a row of ABBREVIATED or an entry of
    CITED_NOT_ABBREVIATED with its reason.
    """
    abbreviated = {req for _, _, req, _, _ in ABBREVIATED}
    unclassified: list[str] = []
    for release in spec_releases():
        for req in CELL_REQUIREMENT_RE.findall(gate_cell(release)):
            if req not in abbreviated and req not in CITED_NOT_ABBREVIATED:
                unclassified.append(f"{release}:{req}")
    assert not unclassified, (
        f"15.1.2's gate cells name {sorted(set(unclassified))} and this module says nothing "
        f"about them. Either the cell abbreviates that requirement -- add a row to "
        f"ABBREVIATED with the term both must carry -- or it cites it for some other reason, "
        f"which goes in CITED_NOT_ABBREVIATED with the reason read at its site."
    )
    stale = sorted(
        set(CITED_NOT_ABBREVIATED)
        - {r for rel in spec_releases() for r in CELL_REQUIREMENT_RE.findall(gate_cell(rel))}
    )
    assert not stale, (
        f"{stale} are declared as cited-not-abbreviated and no gate cell names them any "
        f"more. A frozen table whose subject has quietly gone is the shape three sibling "
        f"modules in this suite record."
    )


#: A gate cell that defines a check by pointing at another release row's contents. The cell's
#: meaning is then a function of a cell describing a SHIPPED release, which may not be edited to
#: keep this one true, so the definition can be re-pointed by a correction made somewhere else
#: for some other reason.
CROSS_ROW_RE = re.compile(r"\d+\.\d+\.\d+ contents")

#: Struck text is the record of what an amendment removed, and it is stripped before matching.
#: The amended `second-viewer` cell still contains "0.1.0 contents" inside `~~...~~`; a check
#: that read the raw cell would be red on the repair that closed the defect.
STRUCK_SPAN_RE = re.compile(r"~~.*?~~")


def test_no_gate_cell_takes_its_definition_from_another_rows_contents() -> None:
    offenders: list[str] = []
    for release in spec_releases():
        live = STRUCK_SPAN_RE.sub("", gate_cell(release))
        for phrase in CROSS_ROW_RE.findall(live):
            offenders.append(f"{release} -> {phrase!r}")
    assert not offenders, (
        "these 15.1.2 gate cells define a check by reference to another row's contents:\n  "
        + "\n  ".join(offenders)
        + "\nThat makes the check's meaning a function of a cell that describes a SHIPPED "
        "release and therefore MUST NOT be edited to keep this one true. It is how the 0.4.0 "
        "`second-viewer` cell came apart: the clause 'one not named in the 0.1.0 contents' "
        "picked out a third party only while that list named OHIF, and after 15.3.1's Viewer "
        "row recorded BUILD the platform's own viewer satisfied it. Define the check against "
        "the requirement it abbreviates, or against a property of the thing under test."
    )


# ---------------------------------------------------------------------------------------
# "MEASURED: NO FILE IN THIS REPOSITORY CONTAINS X" IS A CLAIM, AND NOTHING RE-DERIVED IT
# ---------------------------------------------------------------------------------------
#
# Two declarations said it -- this module's `refusal-completeness` INAPPLICABLE reason and
# `tests/_support/release_criteria.py`'s matching `gap` -- about `what_is_wrong`,
# `why_it_blocks`
# and `what_would_resolve_it`. Both had gone false. `medos/web/training-console/src/refusals/
# catalogue.js` carries 75 three-part records, and the tokens occur in eleven files.
#
# Register entry 83 already holds what that means for the CUT those declarations record,
# and that
# is a release-accounting decision rather than a sweep. What was not held is the shape: a
# negative existence claim that says MEASURED and gives no date is a PRESENT-TENSE claim, and
# nothing in the repository re-derived one. Both are now dated, which makes each a correct
# statement about 2026-09-16 instead of a wrong one about today.
#
# So the rule is the one this suite settled on for counts, applied to absences: state the
# property, date the measurement. A claim may be TRUE, or it may be DATED. It may not be both
# undated and false.

#: The construct: a sentence asserting that nothing in the tree holds some backticked
#: identifiers. The first version of this comment SPELLED AN EXAMPLE of it, and the check
#: matched its own comment and reported that 592 files contain `a`. Third self-reference of
#: the session. Adjacent string literals are joined
#: before matching, because these sentences are written across several of them.
NEGATIVE_CLAIM_RE = re.compile(
    r"no file in this repository contain(?:s|ed)\s+"
    r"((?:`[A-Za-z_][A-Za-z0-9_]*`[,\s]*(?:or|and)?[,\s]*)+)",
    re.I,
)

#: A date anywhere in the sentence that carries the claim.
CLAIM_DATE_RE = re.compile(r"MEASURED[^.]{0,40}?(\d{4}-\d{2}-\d{2})", re.I)

TOKEN_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")


def _joined(source: str) -> str:
    """Python adjacent-literal concatenation, flattened, so a sentence can be read."""
    return re.sub(r'"\s*\n\s*"', "", source)


def _modules_making_negative_claims() -> dict[str, list[tuple[str, ...]]]:
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "tests"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    ).stdout.split()
    out: dict[str, list[tuple[str, ...]]] = {}
    for rel in tracked:
        if not rel.endswith(".py"):
            continue
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        flat = _joined(path.read_text(encoding="utf-8", errors="replace"))
        claims = []
        for m in NEGATIVE_CLAIM_RE.finditer(flat):
            sentence = flat[max(0, m.start() - 320): m.end()]
            tokens = tuple(TOKEN_RE.findall(m.group(1)))
            dated = bool(CLAIM_DATE_RE.search(sentence))
            claims.append((rel, tokens, dated))
        if claims:
            out[rel] = claims
    return out


def test_there_are_negative_existence_claims_to_check() -> None:
    """Otherwise the check below passes by finding nothing, which is how a gate dies quietly."""
    found = _modules_making_negative_claims()
    assert found, (
        "no module says \"no file in this repository contains ...\" any more. If the phrasing "
        "changed, teach NEGATIVE_CLAIM_RE the new one -- an unparsed claim is an unchecked one."
    )


def test_every_undated_negative_existence_claim_is_still_true() -> None:
    offenders: list[str] = []
    for rel, claims in _modules_making_negative_claims().items():
        for _rel, tokens, dated in claims:
            if dated or not tokens:
                continue
            for token in tokens:
                hits = subprocess.run(
                    ["git", "grep", "-l", "--cached", "-w", "--", token],
                    cwd=REPO_ROOT, capture_output=True, text=True,
                ).stdout.split()
                elsewhere = [h for h in hits if h != rel]
                if elsewhere:
                    offenders.append(
                        f"{rel}: claims no file contains `{token}`; "
                        f"{len(elsewhere)} do, including {elsewhere[0]}"
                    )
    assert not offenders, (
        "these say MEASURED and give no date, and they are false:\n  "
        + "\n  ".join(offenders)
        + "\nEither the claim is true and stays, or it has gone false and takes a date -- "
        "'MEASURED 2026-09-16: no file contained X' is a correct statement about that day, and "
        "the undated form is a present-tense claim about today. Two of these described a "
        "repository that had grown a 75-record refusal catalogue since they were written."
    )
