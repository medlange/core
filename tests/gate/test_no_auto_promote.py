# SPDX-License-Identifier: Apache-2.0
"""`no-auto-promote` -- §15.1.2's 0.3.0 gate row via chapter 17's `MOS-TRAIN-193`.

    "no automated path exists from a training run to a SERVING deployment"

A NEGATIVE ABOUT A WHOLE CODEBASE, WHICH IS WHY IT IS ASSERTED FOUR WAYS
-------------------------------------------------------------------------
"No path exists" cannot be established by running something and watching it not happen.
`MOS-TRAIN-233` says so and says what to do instead: "CI MUST assert this as a call-graph
property". This module does that and three more, because each closes a different route and
any one alone is bypassable:

  1. STRUCTURE   the import closure of `medos.training` reaches neither `medos.promotion`
                 nor `medos.evidence.deployment`. A module that cannot reach the
                 deployment mutator cannot call it, however it is invoked. With its
                 control: `medos.promotion` DOES reach the mutator, so the assertion is
                 not passing because both halves are empty.
  2. IDENTITY    every promotion act requires `actor.kind == 'user'`. `MOS-TRAIN-152`:
                 "an automated approver is exactly the shape a well-intentioned automation
                 of the last mile takes." A service account holding the approval grant is
                 the automated path, assembled without a single forbidden import.
  3. DATABASE    the deployments trigger refuses `auto_promote` (`MOS-TRAIN-184`). This is
                 the half that binds a `psql` session and any future service written in
                 another language, and it is the reason (1) is not merely a convention.
  4. ABSENCE     no scheduler, watcher or monitoring hook in the tree starts a training
                 run or a promotion. `MOS-TRAIN-194` forbids exactly the four-component
                 assembly -- "a drift alert that starts a training run, which produces a
                 candidate, which passes a gate, which promotes itself" -- in which no
                 individual component looks wrong.

WHAT THIS CHECK DOES NOT CLAIM
-------------------------------
It does not claim a human cannot promote a bad model. Chapter 17's three acts, the
deployment gate of 0.2.0 and `MOS-EVID-064` are what stand between a candidate and a
serving slot. This check claims only that the last mile is walked by a person, every time.

Needs a schema for (3). (1), (2) and (4) read source.

Spec: MOS-TRAIN-152, MOS-TRAIN-174, MOS-TRAIN-181, MOS-TRAIN-184, MOS-TRAIN-189,
MOS-TRAIN-193, MOS-TRAIN-194, MOS-TRAIN-233, MOS-REG-080, MOS-REL-004.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import pkgutil
import re
import uuid as _uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db import audit
from medos.db.tenancy import DEFAULT_TENANT_ID
from medos.promotion import acts as promo

pytestmark = pytest.mark.gate_0_3_0

REPO_ROOT = Path(__file__).resolve().parents[2]

#: What the training/evaluation/conversion side must not be able to reach.
#: `medos.promotion` owns the three acts; `medos.evidence.deployment` owns the row that
#: says what is serving.
FORBIDDEN_FROM_TRAINING: tuple[str, ...] = ("medos.promotion", "medos.evidence.deployment")

#: The packages `MOS-TRAIN-189` scopes the ban to: "any path from a TrainingRun, a
#: ConversionRun, an EvaluationRun or a ValidationReport to a SERVING Deployment".
PIPELINE_PACKAGES: tuple[str, ...] = ("medos.training",)

OPERATOR = "11111111-1111-1111-1111-111111111111"


def _closure(package: str) -> set[str]:
    """Every first-party module transitively imported by `package`, by STATIC read.

    Static and not `sys.modules`: an import inside a function body is still a path, and a
    deferred import is the most likely shape of an accidental one. `MOS-TRAIN-189` is
    about paths, not about what one process happened to load.
    """
    root = Path(importlib.import_module(package).__file__).parent
    seen: set[str] = set()
    frontier = [f"{package}.{m.name}" for m in pkgutil.iter_modules([str(root)])] + [package]
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, AttributeError, ValueError):
            # `from medos.x import Y` where Y is a class. Not a module edge.
            continue
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            continue
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("medos."):
                        frontier.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("medos."):
                    frontier.append(node.module)
                    frontier.extend(f"{node.module}.{a.name}" for a in node.names)
    return seen


# =====================================================================================
# 1. STRUCTURE
# =====================================================================================
def test_no_auto_promote_the_pipeline_cannot_reach_a_deployment_mutation() -> None:
    """`MOS-TRAIN-189` / `MOS-TRAIN-233`, as the call-graph property they ask for."""
    for package in PIPELINE_PACKAGES:
        closure = _closure(package)
        offenders = sorted(
            m
            for m in closure
            if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_FROM_TRAINING)
        )
        assert offenders == [], (
            f"{package} reaches the deployment side: {offenders}. MOS-TRAIN-189 forbids "
            f"any path from a TrainingRun, a ConversionRun, an EvaluationRun or a "
            f"ValidationReport to a SERVING Deployment that does not pass through steps 4 "
            f"and 5 of MOS-TRAIN-181."
        )


def test_no_auto_promote_the_structural_assertion_has_a_positive_control() -> None:
    """The mutator is REACHABLE from somewhere, or the test above proves nothing.

    If `medos.evidence.deployment` had been deleted or renamed, the closure walk above
    would find no offender for a reason that has nothing to do with isolation. So the
    package that is SUPPOSED to reach it is asserted to.
    """
    promotion_closure = _closure("medos.promotion")
    assert any(m.startswith("medos.evidence.deployment") for m in promotion_closure), (
        "medos.promotion does not reach medos.evidence.deployment, so the mutator either "
        "moved or no longer exists -- and the isolation asserted above is an artifact of "
        "that, not a property of the pipeline."
    )


# =====================================================================================
# 2. IDENTITY
# =====================================================================================
def test_no_auto_promote_every_promotion_act_requires_a_named_human() -> None:
    """`MOS-TRAIN-152` / `MOS-TRAIN-174`. Every non-human actor kind, not a sample.

    `platform_admin` is in the refused set although a platform administrator is a person:
    it is a ROLE a process can hold too, and an audit row that says `platform_admin` does
    not name the human who performed the act (`MOS-EVID-117`).
    """
    non_human = sorted(audit.ACTOR_KINDS - {"user"})
    assert non_human, "audit.ACTOR_KINDS has no non-human kind; the check has no subject"
    assert set(non_human) == set(promo.NON_HUMAN_ACTOR_KINDS), (
        f"medos.promotion.acts.NON_HUMAN_ACTOR_KINDS is {sorted(promo.NON_HUMAN_ACTOR_KINDS)} "
        f"and audit.ACTOR_KINDS minus 'user' is {non_human}. A kind in one and not the "
        f"other is a principal that can approve without being refused or a principal that "
        f"cannot exist."
    )
    for kind in non_human:
        actor = audit.Actor(kind=kind, id=OPERATOR, auth="api_key")
        with pytest.raises(promo.ApprovalRefused):
            promo.assert_human(actor, act="approve_clinical_use")
    promo.assert_human(audit.Actor(kind="user", id=OPERATOR, auth="api_key"), act="x")


def test_no_auto_promote_the_acts_that_change_what_serves_all_call_the_human_check() -> None:
    """The guard above is worth nothing if an act forgets to call it.

    Asserted by reading each act's body for a call to `assert_human`, rather than by
    driving every act against a database: the acts have different preconditions (an
    approved artifact, a verified candidate, a rollback reservation) and a check that only
    reaches the guard after satisfying all of them is a check that stops reaching it the
    day a precondition changes.
    """
    source = (Path(promo.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # The acts that put something in front of a patient, or move what is serving.
    acts = {
        "approve_artifact",
        "approve_clinical_use",
        "cutover",
    }
    found = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in acts
    }
    assert set(found) == acts, (
        f"medos/medos/promotion/acts.py no longer defines {sorted(acts - set(found))}. The "
        f"three-act promotion is what `no-auto-promote` is a property of; if the acts were "
        f"renamed this check is looking at the wrong functions."
    )
    for name, node in sorted(found.items()):
        calls = [
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]
        assert "assert_human" in calls, (
            f"{name}() does not call assert_human(). MOS-TRAIN-152 puts the check at the "
            f"write path of every act, because a grant table is edited by people who are "
            f"not reading this file."
        )


# =====================================================================================
# 3. DATABASE
# =====================================================================================
def test_no_auto_promote_the_database_refuses_an_auto_promoting_deployment(
    platform_db: Any,
) -> None:
    """`MOS-TRAIN-184` in the schema, where it binds a `psql` session too.

    A raw INSERT, so the DATABASE's answer is what is asserted rather than the library's.
    `MOS-REG-080` already forbids the narrow form -- clinical and `auto_promote` on one
    row -- and 0013's `BEFORE INSERT` trigger fires ahead of the table CHECK, so this
    branch is reached without the row having to satisfy 0008's clinical gate first.
    """
    from medos.sdk.canonical import new_ulid

    def insert(*, clinical: bool, auto: bool) -> None:
        platform_db.execute(
            """
            INSERT INTO deployments (public_id, tenant_id, environment, capability_id,
                subject_kind, subject_id, subject_version, role, state, clinical_use_mode,
                promotion_policy, created_by)
            VALUES (%s, %s, 'production', 'lung_nodule', 'model_version', 'mv_gate',
                    '1.0.0', 'STANDBY', 'PENDING', %s, %s, %s)
            """,
            (
                new_ulid("dep"),
                _uuid.UUID(DEFAULT_TENANT_ID),
                "clinical" if clinical else "research_only",
                json.dumps({"auto_promote": auto}),
                _uuid.UUID(OPERATOR),
            ),
        )

    with pytest.raises(psycopg.errors.CheckViolation) as exc:
        insert(clinical=True, auto=True)
    assert "auto_promote MUST be false for a clinical deployment" in str(exc.value)
    platform_db.rollback()

    # The negative control: the same row without `auto_promote` is refused for OTHER
    # reasons or accepted, but never for this one. Without it, a trigger that raised on
    # every insert would satisfy the assertion above.
    try:
        insert(clinical=False, auto=False)
    except psycopg.errors.CheckViolation as other:  # pragma: no cover - schema-dependent
        assert "auto_promote" not in str(other), (
            "a deployment with auto_promote=false was still refused for auto_promote; the "
            "trigger fires unconditionally and the assertion above establishes nothing"
        )
    platform_db.rollback()

    assert promo.assert_no_auto_promote(
        platform_db,
        tenant_id=DEFAULT_TENANT_ID,
        environment="production",
        capability_id="lung_nodule",
        promotion_policy={"auto_promote": False},
    ) is None
    platform_db.rollback()


def test_no_auto_promote_the_trigger_is_shipped_by_a_migration_not_by_a_fixture(
) -> None:
    """The rule is in the migration set, so every database the release creates has it.

    A trigger created by a test fixture protects the test. `MOS-TRAIN-184` is a property
    of the deployment.
    """
    ddl = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted((REPO_ROOT / "medos" / "medos" / "db" / "migrations").glob("*.up.sql"))
    )
    assert "deployments_no_auto_promote" in ddl, (
        "no migration creates the `deployments_no_auto_promote` trigger; MOS-TRAIN-184 "
        "would then hold only where a fixture put it"
    )
    assert re.search(
        r"BEFORE\s+INSERT\s+OR\s+UPDATE[^;]*ON\s+deployments", ddl, re.IGNORECASE
    ), "the auto-promote trigger is not a BEFORE INSERT OR UPDATE on deployments"


# =====================================================================================
# 4. ABSENCE
# =====================================================================================
def test_no_auto_promote_nothing_in_the_tree_schedules_a_run_or_a_promotion() -> None:
    """`MOS-TRAIN-194`: no drift alert, cron entry or watcher starts the chain.

    The forbidden construction is four individually reasonable components wired together,
    so the check is for the WIRING: a scheduler or a monitoring callback in the same
    module as a call into the training or promotion API. Searched over `medos/medos/` and
    `deploy/`, which is where a scheduler would have to live to run unattended.
    """
    schedulers = re.compile(
        r"\b(crontab|APScheduler|BackgroundScheduler|schedule\.every|celery|"
        r"add_periodic_task|on_drift|on_alert|autoretrain|auto_retrain)\b",
        re.IGNORECASE,
    )
    entrypoints = re.compile(
        r"\b(training\.runs\.submit|runs\.submit|promo\.cutover|acts\.cutover|"
        r"approve_clinical_use|create_candidate_deployment)\b"
    )
    offenders: list[str] = []
    # `medos/medos` and `medos/deploy`, not `medos`. The word changed meaning when
    # `medos/` became a product directory: it now also holds `api/`, `schemas/`,
    # `web/` and `tools/`. Scanning all of it flagged `medos/api/v1/routes.train.yaml`
    # -- a CONTRACT DOCUMENT that names `runs.submit` because that is what it
    # documents -- as a scheduler beside a training entrypoint. A false positive from
    # a widened root is how a gate gets relaxed to make it quiet.
    #
    # `deploy` was a separate entry until it became `medos/deploy/`; it is still
    # scanned, by its new name, rather than skipped by a `continue` that said nothing.
    roots = ("medos/medos", "medos/deploy")
    missing = [r for r in roots if not (REPO_ROOT / r).exists()]
    assert not missing, (
        f"this scan is addressed by path and {missing} does not exist, so it would "
        "loop over nothing and report a pass"
    )
    for root in roots:
        base = REPO_ROOT / root
        for path in sorted(base.rglob("*")):
            if path.suffix not in (".py", ".yml", ".yaml", ".sh") or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if schedulers.search(text) and entrypoints.search(text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "a scheduler or alert hook sits in the same module as a training/promotion "
        f"entrypoint: {offenders}. MOS-TRAIN-194 forbids the assembly, not the parts."
    )
