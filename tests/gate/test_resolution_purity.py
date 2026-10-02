# SPDX-License-Identifier: Apache-2.0
"""`resolution-purity` -- §15.1.2's release-0.3.0 gate row, second check.

    "the resolver is a pure function of `(capability, context, registry_snapshot)`,
     property-tested over a frozen snapshot, AND the pinned set on an existing `Job` is
     unchanged by a later registry write"

BOTH HALVES, BECAUSE NEITHER ALONE PREVENTS THE FAILURE
---------------------------------------------------------
§6.7.7 works the failure through. Without the pin, attempt 2 of `job_9f21` resolves against
a newer epoch: the same job id reports 705 mL where it reported 642 mL, the provenance
record's two halves name different artifacts, and PACS accumulates a second overlapping SEG
series because the derived `SeriesInstanceUID` is a function of the model version. Nothing
raises an exception in any of the three.

A PURE RESOLVER DOES NOT PREVENT THIS. Purity makes the answer stable for a FIXED snapshot,
and the snapshot is exactly what moved. A pin without purity does not prevent it either:
the pinned record would name a version whose resolution could still be replayed
differently, so `MOS-REG-012`'s "replaying a recorded `(epoch, snapshot_id)` MUST yield a
byte-identical `Outcome`" would be false. The check is an `AND` in the specification and it
is an `AND` here.

HALF ONE: PURITY, ASSERTED THREE WAYS
---------------------------------------
  * STATICALLY. `MOS-REG-051` is a Go build constraint -- "MUST NOT import net/http,
    database/sql, os, math/rand, or call time.Now" -- and the equivalent here is that the
    four pure modules import no driver, no clock and no randomness, read by `ast` rather
    than by importing (an import that only happens inside a function body is still a path).
  * BY PROPERTY. `MOS-REG-053` names the scale: 10 000 random registry states, twice each,
    byte-identical `Outcome`. The generator varies every axis a filter stage reads, so the
    property covers branches rather than one path taken 10 000 times.
  * BY INDEPENDENCE FROM ARRIVAL ORDER. A snapshot is a SET of rows. Two loaders reading
    one registry in different orders must resolve identically -- the property a `dict` or
    `set` walk silently breaks, and the one a same-process repeat cannot detect.

HALF TWO: THE PIN, AGAINST A REAL REGISTRY
--------------------------------------------
State A: `3.2.1` is APPROVED and ACTIVE; a job is created and the resolved set is written
into it in the SAME transaction (`MOS-REG-066`). State B: `3.4.0` is published, approved
and deployed; the epoch moves. A FRESH resolution then selects `3.4.0` -- asserted, because
without it this half proves only that nothing happened -- and the EXISTING job still
resolves to `3.2.1`, because `medos.resolution.retry` reads the pin and has no `Snapshot`
in scope with which to re-resolve.

Needs a schema for half two. Half one needs nothing.

Spec: MOS-REG-004, MOS-REG-010, MOS-REG-012, MOS-REG-051, MOS-REG-052, MOS-REG-053,
MOS-REG-061, MOS-REG-066, MOS-REG-067, MOS-REG-071, MOS-REG-115, MOS-EXEC-008,
MOS-REL-004; docs/spec/06-registries.md §6.7.5, §6.7.7.
"""

from __future__ import annotations

import ast
import random
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from medos.db import repo as jobs_repo
from medos.db.queue import PostgresJobQueue
from medos.resolution import pin as pin_module
from medos.resolution import retry as retry_module
from medos.resolution import snapshot as snapshot_module
from medos.resolution.model import VersionPin
from medos.resolution.resolve import resolve

from tests.gate import _platform as P

pytestmark = pytest.mark.gate_0_3_0

#: The modules `MOS-REG-051` calls pure. `snapshot.py` and `pin.py` are NOT here: loading a
#: snapshot and writing a pin are I/O by definition, and the boundary between them and the
#: four below is the thing being asserted.
PURE_MODULES = ("resolve.py", "model.py", "versions.py", "bucket.py")

#: `MOS-REG-051`'s Go build constraint in this platform's vocabulary.
FORBIDDEN_IMPORTS = {
    "psycopg", "psycopg_pool", "sqlalchemy",                        # database/sql
    "requests", "httpx", "http", "urllib", "socket", "fastapi",     # net/http
    "random", "secrets", "uuid",                                    # math/rand
    "os", "pathlib", "subprocess",                                  # os
    "time",
}
FORBIDDEN_CALLS = {
    "now", "utcnow", "today", "time", "monotonic", "perf_counter",
    "random", "randint", "choice", "shuffle", "urandom", "uuid4", "open",
}

#: `deployments.public_id` is `dep_` + a 26-character Crockford base32 ULID in upper case
#: (chapter 10's grammar, CHECKed by migration 0008). Fixed, so assertions can name a row.
DEPLOYMENT_IDS = {
    "3_2_1": "dep_01JQ90A4TT0000000000000000",
    "3_4_0": "dep_01JT5R2Q8N0000000000000000",
}


def _pure_module_paths() -> list[Path]:
    """`medos/medos/resolution/<name>` for each of `PURE_MODULES`.

    Located through `medos.resolution.__path__` rather than through `resolve.__file__`:
    `medos.resolution.__init__` re-exports the `resolve` FUNCTION under that name, so the
    obvious spelling reads the attribute of a function object.
    """
    import medos.resolution as package

    root = Path(next(iter(package.__path__)))
    return [root / name for name in PURE_MODULES]


# =====================================================================================
# HALF ONE -- purity
# =====================================================================================
def test_resolution_purity_the_pure_modules_reach_no_clock_driver_or_randomness() -> None:
    """`MOS-REG-051`, read with `ast` rather than by importing.

    Read statically because an import inside a function body is still a path: a resolver
    that did `import psycopg` lazily on one branch would satisfy any check that only
    looked at what a process happened to load.
    """
    import_offenders: list[str] = []
    call_offenders: list[str] = []
    for path in _pure_module_paths():
        assert path.exists(), f"{path.name} is not where MOS-REG-051's pure modules live"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                        import_offenders.append(f"{path.name}:{node.lineno} {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in FORBIDDEN_IMPORTS:
                    import_offenders.append(f"{path.name}:{node.lineno} {node.module}")
            elif isinstance(node, ast.Call):
                fn = node.func
                name = (
                    fn.attr
                    if isinstance(fn, ast.Attribute)
                    else fn.id
                    if isinstance(fn, ast.Name)
                    else ""
                )
                if name in FORBIDDEN_CALLS:
                    call_offenders.append(f"{path.name}:{node.lineno} {name}()")
    assert not import_offenders, (
        "the resolver imports something that can do I/O or produce a different answer "
        f"tomorrow: {import_offenders}. MOS-REG-051 states it as a build constraint."
    )
    assert not call_offenders, (
        f"the resolver reads a clock or a random source: {call_offenders}. The only time "
        f"value it may read is `snapshot.as_of` (MOS-REG-051)."
    )


def test_resolution_purity_ten_thousand_random_states_resolve_identically_twice() -> None:
    """`MOS-REG-053`, at its own stated scale. `MOS-REG-012`'s byte-identical `Outcome`.

    The generator is seeded, so a failure is reproducible from the case index printed in
    the message. Comparison is `canonical_bytes()` and not field equality: the requirement
    says byte-identical, and two `Outcome`s that compare equal while serialising
    differently would still make a replayed provenance record disagree with itself.
    """
    rng = random.Random(0xC0FFEE)
    for case in range(10_000):
        snapshot = P.random_snapshot(rng)
        study = f"stu_{rng.randrange(10**6):06d}"
        pin = rng.choice([None, VersionPin(range=">=3 <4"), VersionPin(P.FAMILY, "^3.0.0")])
        ctx = P.context(study_constraints={"study_id": study}, job_pin=pin)
        first = resolve(P.CAPABILITY, ctx, snapshot)
        second = resolve(P.CAPABILITY, ctx, snapshot)
        assert first.canonical_bytes() == second.canonical_bytes(), (
            f"case {case}: two resolutions of one frozen snapshot differ. MOS-REG-012 "
            f"makes replaying a recorded (epoch, snapshot_id) byte-identical."
        )


def test_resolution_purity_the_answer_does_not_depend_on_the_order_rows_arrive_in() -> None:
    """`MOS-REG-053`'s "map iteration MUST be sorted before it can affect output".

    A snapshot is a SET of rows, and two loaders reading one registry in different orders
    must resolve identically. This is the property a `dict`/`set` walk breaks, and running
    the same snapshot twice in one process cannot see it.
    """
    rng = random.Random(7)
    for case in range(200):
        snapshot = P.random_snapshot(rng)
        services = list(snapshot.services)
        deployments = list(snapshot.deployments)
        rng.shuffle(services)
        rng.shuffle(deployments)
        shuffled = replace(
            snapshot,
            services=tuple(services),
            deployments=tuple(deployments),
            snapshot_id=snapshot.snapshot_id,
        )
        a = resolve(P.CAPABILITY, P.context(), snapshot)
        b = resolve(P.CAPABILITY, P.context(), shuffled)
        assert [c.service_version_id for c in a.ranked] == [
            c.service_version_id for c in b.ranked
        ], f"case {case}: the ranking depends on row order"
        assert a.selected == b.selected, f"case {case}: the selection depends on row order"


def test_resolution_purity_resolve_is_total_and_its_ranking_is_a_total_order() -> None:
    """`MOS-REG-052` and `MOS-REG-061`, as properties rather than promises.

    Totality matters for purity because the alternative to an answer is an exception, and
    an exception is a behaviour that depends on how the caller is wrapped. `ZERO_CANDIDATES
    + reason_code` is an answer; a traceback is not.
    """
    rng = random.Random(0x5EED)
    for _ in range(2_000):
        snapshot = P.random_snapshot(rng)
        outcome = resolve(
            rng.choice([P.CAPABILITY, "lung_nodule"]),
            P.context(job_pin=rng.choice([None, VersionPin(range="=9.9.9")])),
            snapshot,
        )
        assert outcome.decision in ("SELECTED", "ZERO_CANDIDATES")
        assert (outcome.selected is None) == (outcome.decision == "ZERO_CANDIDATES")
        assert bool(outcome.reason_code) == (outcome.decision == "ZERO_CANDIDATES")
        keys = [c.rank_key.as_string() + "|" + c.content_digest for c in outcome.ranked]
        assert len(keys) == len(set(keys)), "two candidates share a rank position"


def test_resolution_purity_the_retry_path_has_no_snapshot_in_scope() -> None:
    """`MOS-REG-067`: a retry MUST NOT re-run resolution, and cannot.

    Structural rather than behavioural. A retry path that merely "does not call resolve
    today" is one refactor from calling it; a retry path with no `Snapshot` in scope
    cannot, whatever it is edited into, without that edit being visible here.
    """
    source = Path(retry_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = sorted(
        name
        for name in imported
        if name.endswith(("Snapshot", "resolve", ".snapshot", "load_snapshot"))
    )
    assert not forbidden, (
        f"medos/medos/resolution/retry.py imports {forbidden}. MOS-REG-067 makes a retry read "
        f"the pin; a module that can build a snapshot can re-resolve against a moved "
        f"registry, which is exactly §6.7.7's 642 mL / 705 mL failure."
    )


# =====================================================================================
# HALF TWO -- the pin, against a real registry
# =====================================================================================
def test_resolution_purity_the_pin_is_unchanged_by_a_later_registry_write(
    platform_db: Any,
) -> None:
    """§6.7.7 end to end. `MOS-REG-066` / `MOS-REG-067` / `MOS-REG-115`.

    The middle assertion is the one that makes the last one mean something: a FRESH
    resolution must select the new version. Without it, "the job still runs 3.2.1" is
    equally consistent with a registry that never moved.
    """
    from medos.db.tenancy import tenant_tx

    sv_321 = _publish_release(platform_db, "3.2.1", suffix="3_2_1", run_id="er_01JP4T9X7B")

    snap_a = snapshot_module.load_snapshot(
        platform_db, tenant_id=P.TENANT, environment="production", as_of=P.AS_OF
    )
    outcome_a = resolve(P.CAPABILITY, P.context(), snap_a)
    assert outcome_a.is_selected, "the fixture registry resolves to nothing at state A"
    assert outcome_a.selected.service_version_id == sv_321
    assert outcome_a.selected.version == "3.2.1"
    assert outcome_a.selected.deployment_environment == "production"
    assert outcome_a.selected.deployment_state == "SERVING"

    record = pin_module.resolution_record(
        outcome_a, capability_id=P.CAPABILITY, resolved_at=snap_a.as_of
    )
    spec = jobs_repo.JobSpec(
        study_instance_uid="1.2.826.0.1.3680043.8.498.44100",
        capability_ids=(P.CAPABILITY,),
        service_id=P.FAMILY,
        service_version=outcome_a.selected.version,
    )
    # MOS-REG-066: "written into the Job in the same transaction that creates the job,
    # before any enqueue". `tenant_tx` joins the open transaction as a SAVEPOINT, so this
    # really is one transaction and not two.
    with tenant_tx(platform_db):
        created = jobs_repo.create_job_queued(platform_db, PostgresJobQueue(platform_db), spec)
        pin_module.write_pin(platform_db, job_public_id=created.job_id, record=record)
    platform_db.commit()

    # ---- State B: a newer version is published, approved and deployed ----------------
    sv_340 = _publish_release(
        platform_db, "3.4.0", suffix="3_4_0", run_id="er_01JT5R2Q8N", deploy_role=None
    )
    with platform_db.transaction():
        platform_db.execute(
            "UPDATE deployments SET role = 'STANDBY' WHERE public_id = %s",
            (DEPLOYMENT_IDS["3_2_1"],),
        )
        P.deploy(platform_db, DEPLOYMENT_IDS["3_4_0"], sv_340, "3.4.0", commit=False)
    platform_db.commit()

    snap_b = snapshot_module.load_snapshot(
        platform_db,
        tenant_id=P.TENANT,
        environment="production",
        as_of=P.AS_OF + timedelta(days=48),
    )
    assert snap_b.epoch > snap_a.epoch, "the registry epoch did not move (MOS-REG-010)"
    assert snap_b.snapshot_id != snap_a.snapshot_id

    fresh = resolve(P.CAPABILITY, P.context(), snap_b)
    assert fresh.selected.service_version_id == sv_340, (
        "a NEW job does not see the new version, so the registry did not move and the "
        "assertion below proves nothing"
    )

    pinned = retry_module.pinned_selection(platform_db, created.job_id)
    assert pinned.service_version_id == sv_321, (
        "the pinned set on an existing Job changed after a registry write. This is "
        "§6.7.7: the same job id reporting 642 mL and then 705 mL, with a provenance "
        "record whose two halves name different artifacts."
    )
    assert pinned.version == "3.2.1"
    assert pinned.epoch == snap_a.epoch
    assert pinned.snapshot_id == snap_a.snapshot_id
    # MOS-REG-115: the pinned deployment STATE is the one at resolution. The row has since
    # moved to STANDBY and the pin is not rewritten to match it.
    assert pinned.deployment_state == "SERVING"
    assert pinned.deployment_role == "ACTIVE"
    row = platform_db.execute(
        "SELECT role FROM deployments WHERE public_id = %s", (DEPLOYMENT_IDS["3_2_1"],)
    ).fetchone()
    assert row["role"] == "STANDBY", "the fixture did not actually demote the old row"


def _publish_release(
    conn: Any, version: str, *, suffix: str, run_id: str, deploy_role: str | None = "ACTIVE"
) -> str:
    """One model + one service at `version`, optionally deployed. Returns the sv id."""
    mv = f"mv_pulmo_effusion_unet_{suffix}"
    sv = f"sv_pulmo_effusion_{suffix}"
    manifest = P.model_manifest("pulmo.effusion-unet", version)
    manifest["spec"]["operating_point"]["selected_on_evaluation_run"] = run_id
    manifest["spec"]["evaluation_run_id"] = run_id
    from medos.registry.digest import content_digest_of

    manifest["content_digest"] = content_digest_of(
        {k: v for k, v in manifest.items() if k != "content_digest"}
    )
    P.publish(conn, manifest, mv)
    P.publish(conn, P.service_manifest(version, mv), sv)
    if deploy_role:
        P.deploy(conn, DEPLOYMENT_IDS[suffix], sv, version, role=deploy_role)
    return sv
