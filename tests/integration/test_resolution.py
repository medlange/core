# SPDX-License-Identifier: Apache-2.0
"""Capability resolution: purity, precedence, the range grammar, and THE PIN.

    MOS-REG-051  the signature, and "exactly one implementation"
    MOS-REG-052  Resolve never returns an error
    MOS-REG-053  determinism: same Request + same Snapshot => byte-identical Outcome,
                 "asserted by a property test running 10 000 random registry states"
    MOS-REG-055  the F1..F9 filters, in order
    MOS-REG-056  F3 before F5: a pin MUST NOT resurrect a SUSPENDED or RECALLED version
    MOS-REG-058  the P1..P7 precedence order
    MOS-REG-059  the CRC-32C canary bucket, and study stickiness
    MOS-REG-060  a missing acceptance metric ranks -Inf and is RECORDED as absent
    MOS-REG-061  Ranked is a total order
    MOS-REG-062..064  the closed range grammar
    MOS-REG-066  the resolved set is pinned into the job at creation
    MOS-REG-067  every retry uses the pin verbatim; re-resolution impossible BY
                 CONSTRUCTION -- the retry path has no Snapshot in scope
    MOS-REG-068  a pinned version withdrawn between attempts stops the retry
    MOS-REG-069  ZERO_CANDIDATES is REJECTED with a clinical reason, never a fallback
    MOS-REG-071  offline replay reproduces the Outcome byte-for-byte

Chapter 6's acceptance criteria exercised here: 3 (resolver purity), 5 (precedence),
8 (replay), 9 (no re-resolution on retry, with the static check), 13 (canary stickiness).

The file is one file because these are one component's proofs, and the pin proof at the
end is meaningless without the pure-function proofs above it: a resolver that is not
deterministic makes the pin a workaround rather than a guarantee.
"""

from __future__ import annotations

import ast
import os
import random
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.db import audit
from medos.db import repo as jobs_repo
from medos.db.queue import PostgresJobQueue
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.registry import repo as registry_repo
from medos.registry.digest import content_digest_of
from medos.resolution import pin as pin_module
from medos.resolution import retry as retry_module
from medos.resolution import snapshot as snapshot_module
from medos.resolution.bucket import crc32c, deployment_rank
from medos.resolution.model import (
    Capability,
    DeploymentRow,
    GateConfig,
    LicenceGrant,
    MetricPoint,
    ModelVersionRow,
    NodeProfile,
    PreprocRow,
    ServiceVersionRow,
    Snapshot,
    TenantPin,
    TenantProfile,
    ValidationReportRow,
    VersionPin,
)
from medos.resolution.resolve import resolve
from medos.resolution.versions import RangeSyntaxError, parse_range, parse_version
from psycopg.rows import dict_row

from tests._support.capability_source import (
    VENDOR_CAPABILITY_ID,
    serve_one_extra_capability,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: BOTH roots on the child's path. The package is `medos/medos/` now, so a subprocess
#: given only the repository root imports nothing, `check=True` raises, and this test
#: reports a CalledProcessError where it means to report a list of loaded drivers.
_CHILD_PYTHONPATH = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "medos")])
RESOLUTION_DIR = REPO_ROOT / "medos" / "medos" / "resolution"

TENANT = DEFAULT_TENANT_ID
CAPABILITY = "pleural_effusion"
FAMILY = "pulmo.pleural-effusion"
STUDY = "stu_4410"
AS_OF = datetime(2026, 1, 14, 8, 22, 19, tzinfo=UTC)

ACTOR = audit.Actor(
    kind="service_account", id="11111111-1111-1111-1111-111111111111", auth="api_key"
)
TRACE = "0af7651916cd43dd8448eb211c80319c"
SIGNER = (
    "https://github.com/pulmoai/pleural-effusion/.github/workflows/"
    "release.yml@refs/tags/v3.2.1"
)


def _context(**overrides: Any) -> dict[str, Any]:
    """The `{modality, tenant_id, environment, study_constraints}` argument."""
    ctx: dict[str, Any] = {
        "tenant_id": TENANT,
        "environment": "production",
        "modality": "CT",
        "study_constraints": {"study_id": STUDY, "slice_thickness_mm": 1.0},
    }
    ctx.update(overrides)
    return ctx


# =====================================================================================
# A frozen snapshot -- §6.7.7's registry, plus the two versions acceptance check 5 adds
# =====================================================================================
def _service(
    sid: str,
    version: str,
    *,
    status: str = "APPROVED",
    family: str = FAMILY,
    models: tuple[str, ...] = ("mv_a",),
    modalities: tuple[str, ...] = ("CT",),
    capabilities: tuple[str, ...] = (CAPABILITY,),
    tenant_id: str | None = None,
    gpu_required: bool = False,
    jurisdictions: tuple[str, ...] = ("EU",),
    manufacturer: str = "lm_pulmoai_gmbh",
) -> ServiceVersionRow:
    return ServiceVersionRow(
        id=sid,
        family=family,
        version=version,
        lifecycle_status=status,
        content_digest="sha256:" + sid.encode("utf-8").hex().ljust(64, "0")[:64],
        tenant_id=tenant_id,
        capabilities=capabilities,
        modalities=modalities,
        model_version_refs=models,
        preprocessing_spec_refs=("ps_a",),
        gpu_architectures=("sm_80",),
        gpu_required=gpu_required,
        gpu_memory_mib=9216,
        legal_manufacturer_id=manufacturer,
        regulatory_jurisdictions=jurisdictions,
        image_digest="sha256:" + "a1" * 32,
    )


def _deployment(
    did: str,
    subject: str,
    *,
    role: str = "ACTIVE",
    state: str = "SERVING",
    permille: int = 0,
    clinical_use_mode: str = "research_only",
) -> DeploymentRow:
    return DeploymentRow(
        id=did,
        tenant_id=TENANT,
        environment="production",
        capability_id=CAPABILITY,
        subject_id=subject,
        role=role,
        state=state,
        traffic_permille=permille,
        clinical_use_mode=clinical_use_mode,
    )


def frozen_snapshot(
    *,
    tenant_pin: bool = True,
    metric_for_330: float | None = 0.881,
    gate_config: GateConfig | None = None,
) -> Snapshot:
    """Acceptance check 5's fixture registry, verbatim.

    "A fixture registry with four versions (a tenant-pinned `3.1.4`, a higher-metric
    `3.3.0`, a newer `3.4.0`, and a `3.5.0` whose model is `SUSPENDED`) resolves to
    `3.1.4`; removing the tenant pin resolves to `3.3.0`; removing its acceptance metric
    resolves to `3.4.0`. The `SUSPENDED` candidate never appears in `Ranked` and always
    appears in `Excluded` with stage `F3`."

    `3.1.4` is in a DIFFERENT family, because a tenant pin names a FAMILY (`MOS-REG-058`
    P2) -- a pin that named a version would be tested by F5, not by P2.
    """
    metrics: dict[str, MetricPoint] = {}
    if metric_for_330 is not None:
        metrics["sv_330|pleural_effusion|dv_acceptance"] = MetricPoint(
            value=metric_for_330, metric="dice_coefficient", evaluation_run_id="er_330"
        )
    return Snapshot(
        epoch=171402,
        as_of=AS_OF,
        capabilities={CAPABILITY: Capability(id=CAPABILITY, primary_metric="dice_coefficient")},
        services=(
            _service("sv_314", "3.1.4", family="pulmo.effusion-classic"),
            _service("sv_330", "3.3.0"),
            _service("sv_340", "3.4.0"),
            _service("sv_350", "3.5.0", models=("mv_suspended",)),
        ),
        models={
            "mv_a": ModelVersionRow("mv_a", "pulmo.effusion-unet", "3.2.1", "APPROVED"),
            "mv_suspended": ModelVersionRow(
                "mv_suspended", "pulmo.effusion-unet", "3.5.0", "SUSPENDED"
            ),
        },
        preproc={"ps_a": PreprocRow("ps_a", "pulmo.effusion-prep", "2.0.0", "APPROVED")},
        deployments=(
            _deployment("dep_314", "sv_314"),
            _deployment("dep_330", "sv_330"),
            _deployment("dep_340", "sv_340"),
            _deployment("dep_350", "sv_350"),
        ),
        tenant_pins=(
            (
                TenantPin(
                    tenant_id=TENANT,
                    capability_id=CAPABILITY,
                    service_family="pulmo.effusion-classic",
                ),
            )
            if tenant_pin
            else ()
        ),
        metrics=metrics,
        tenants={TENANT: TenantProfile(TENANT, "EU")},
        acceptance_datasets={f"{TENANT}|{CAPABILITY}": "dv_acceptance"},
        validation_reports=(
            ValidationReportRow(
                id="vr_0091",
                tenant_id=TENANT,
                subject_id="sv_330",
                subject_version="3.3.0",
                capability_id=CAPABILITY,
            ),
        ),
        gate_config=gate_config or GateConfig(),
    ).with_identity()


# =====================================================================================
# A. Purity -- chapter 6 acceptance check 3
# =====================================================================================
PURE_MODULES = ("resolve.py", "model.py", "versions.py", "bucket.py")

# The Go build constraint of MOS-REG-051 -- "MUST NOT import net/http, database/sql, os,
# math/rand, or call time.Now" -- in this platform's vocabulary.
FORBIDDEN_IMPORTS = {
    "psycopg", "psycopg_pool", "sqlalchemy",      # database/sql
    "requests", "httpx", "http", "urllib", "socket", "fastapi",  # net/http
    "random", "secrets", "uuid",                  # math/rand
    "os", "pathlib", "subprocess",                # os
    "time", "datetime.datetime.now",
}
FORBIDDEN_CALLS = {
    "now", "utcnow", "today", "time", "monotonic", "perf_counter",
    "random", "randint", "choice", "shuffle", "urandom", "uuid4", "open",
}


def test_the_pure_modules_import_nothing_that_can_do_io() -> None:
    """`MOS-REG-051`'s build constraint, as a static check over the source."""
    for name in PURE_MODULES:
        tree = ast.parse((RESOLUTION_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            offending = sorted(set(names) & FORBIDDEN_IMPORTS)
            assert not offending, f"{name} imports {offending} (MOS-REG-051)"


def test_the_pure_modules_read_no_clock_and_no_randomness() -> None:
    """"contains no `time.Now` call" -- and no `random`, which is the same defect."""
    for name in PURE_MODULES:
        tree = ast.parse((RESOLUTION_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else ""
            )
            assert attr not in FORBIDDEN_CALLS, (
                f"{name} calls {attr}() -- the resolver reads snapshot.as_of and "
                "nothing else (MOS-REG-051)"
            )


def test_importing_the_resolver_loads_no_database_driver() -> None:
    """The import check as CI would run it: a fresh interpreter, nothing but the resolver.

    In-process this would pass trivially -- pytest has already imported psycopg. A
    subprocess is the only honest form of "the resolve package imports none of ...".

    `random` and `secrets` are NOT in the forbidden set here, and the reason is worth
    stating rather than hiding: `medos.sdk.canonical` -- the platform's
    single RFC 8785 canonicaliser, which `MOS-SEC-151` forbids duplicating -- also carries
    `new_ulid`, and that pulls `secrets` (and `random` beneath it) into the process.
    Importing a module that offers a random function is not reading randomness. The
    two tests above are the ones that close that gap: no module in this package imports
    `random`/`secrets`, and none calls a random or clock function. Re-implementing
    canonical JSON locally to make a module list shorter would trade a real invariant
    (one canonicaliser) for a cosmetic one, and two canonicalisers is exactly the defect
    `MOS-SEC-151` names.
    """
    probe = (
        "import sys; import medos.resolution.resolve as r; "
        "bad=[m for m in sys.modules if m.split('.')[0] in "
        "('psycopg','psycopg_pool','sqlalchemy','requests','httpx','urllib3',"
        "'socket','ssl','fastapi','starlette')]; "
        "print(sorted(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, cwd=str(REPO_ROOT), check=True,
        env={"PYTHONPATH": _CHILD_PYTHONPATH, "PATH": "", "SYSTEMROOT": "C:\\Windows"},
    )
    assert out.stdout.strip() == "[]", out.stdout


def test_the_retry_path_has_no_snapshot_in_scope() -> None:
    """Chapter 6 acceptance check 9's static check, `MOS-REG-067`.

    "Re-resolution on retry MUST be impossible by construction: the retry path MUST read
    the pin and MUST NOT have the snapshot in scope."
    """
    source = (RESOLUTION_DIR / "retry.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    assert not [m for m in imported if m.startswith("medos.resolution.")], (
        f"retry.py imports {imported}; it must not be able to reach a Snapshot"
    )
    assert "Snapshot" not in dir(retry_module)
    assert "resolve" not in dir(retry_module)


# =====================================================================================
# B. The range grammar -- MOS-REG-062, MOS-REG-063, MOS-REG-064
# =====================================================================================
@pytest.mark.parametrize(
    ("spec_range", "matches", "rejects"),
    [
        ("=3.2.1", ["3.2.1"], ["3.2.2"]),
        ("3.2.1", ["3.2.1"], ["3.2.2"]),                       # bare is EXACT, not caret
        (">=3.2 <4", ["3.2.0", "3.9.7"], ["3.1.9", "4.0.0"]),
        ("^3.2.0", ["3.4.0", "3.2.0"], ["4.0.0", "3.1.9"]),
        ("~3.2.0", ["3.2.9", "3.2.0"], ["3.3.0"]),
        ("^0.4.1", ["0.4.9", "0.4.1"], ["0.5.0"]),             # 0.x caret is minor-locked
    ],
)
def test_the_range_table_of_mos_reg_062(
    spec_range: str, matches: list[str], rejects: list[str]
) -> None:
    parsed = parse_range(spec_range)
    for version in matches:
        assert parsed.matches(version), f"{spec_range} should match {version}"
    for version in rejects:
        assert not parsed.matches(version), f"{spec_range} must not match {version}"


@pytest.mark.parametrize("bad", ["*", "x", "latest", "", "   ", ">=3.2 || <2", "3.2.1.4"])
def test_the_grammar_is_closed(bad: str) -> None:
    """`MOS-REG-063`: no disjunction, no `*`, no `latest`, no `x`, no empty range."""
    with pytest.raises(RangeSyntaxError):
        parse_range(bad)


def test_a_prerelease_satisfies_only_a_range_that_names_it() -> None:
    """`MOS-REG-064`, with its own example: `>=3.2 <4` MUST NOT match `3.5.0-rc.1`."""
    assert not parse_range(">=3.2 <4").matches("3.5.0-rc.1")
    assert parse_range(">=3.5.0-rc.1 <4").matches("3.5.0-rc.1")
    assert parse_range("=3.5.0-rc.1").matches("3.5.0-rc.1")


def test_a_prerelease_sorts_below_its_release() -> None:
    """`MOS-REG-058` P6's parenthesis, which decides which of two versions wins."""
    from medos.resolution.versions import compare_versions

    assert compare_versions(parse_version("3.2.1-rc.1"), parse_version("3.2.1")) < 0
    assert compare_versions(parse_version("3.2.1-rc.2"), parse_version("3.2.1-rc.10")) < 0


# =====================================================================================
# C. The canary bucket -- MOS-REG-059, MOS-REG-079, acceptance check 13
# =====================================================================================
def test_crc32c_is_castagnoli_and_not_zlib() -> None:
    """The standard vectors. `zlib.crc32(b"123456789")` is 0xCBF43926 -- a different hash."""
    assert crc32c(b"") == 0
    assert crc32c(b"123456789") == 0xE3069283


def test_deployment_rank_is_mos_reg_059s_three_lines() -> None:
    assert deployment_rank("CANARY", 100, 99) == 2
    assert deployment_rank("CANARY", 100, 100) == 0
    assert deployment_rank("ACTIVE", 0, 613) == 1
    assert deployment_rank("STANDBY", 0, 0) == 0


def test_canary_is_study_sticky_and_lands_within_the_tolerance() -> None:
    """Acceptance check 13: 10 000 studies, share within +-1.5 pp of 10 %, sticky."""
    snap = _canary_snapshot()
    canary = 0
    for i in range(10_000):
        study = f"stu_{i:06d}"
        first = resolve(CAPABILITY, _context(study_constraints={"study_id": study}), snap)
        assert first.is_selected
        # "for every study every attempt of every job resolves to the same side": the
        # bucket is a function of (tenant, study) only, so a second call -- which is what
        # a second job for the same study does -- cannot cross the boundary.
        again = resolve(CAPABILITY, _context(study_constraints={"study_id": study}), snap)
        assert again.selected == first.selected
        if first.selected.deployment_role == "CANARY":
            canary += 1
    share = canary / 10_000 * 100
    assert abs(share - 10.0) <= 1.5, f"canary share {share:.2f}% is outside 10% +- 1.5pp"


def _canary_snapshot() -> Snapshot:
    return Snapshot(
        epoch=1,
        as_of=AS_OF,
        capabilities={CAPABILITY: Capability(id=CAPABILITY)},
        services=(_service("sv_active", "3.2.1"), _service("sv_canary", "3.4.0")),
        models={"mv_a": ModelVersionRow("mv_a", "f", "1.0.0", "APPROVED")},
        preproc={"ps_a": PreprocRow("ps_a", "f", "2.0.0", "APPROVED")},
        deployments=(
            _deployment("dep_active", "sv_active"),
            _deployment("dep_canary", "sv_canary", role="CANARY", permille=100),
        ),
        tenants={TENANT: TenantProfile(TENANT, "EU")},
    ).with_identity()


# =====================================================================================
# D. Precedence -- MOS-REG-058, chapter 6 acceptance check 5
# =====================================================================================
def test_precedence_is_the_written_order() -> None:
    """Acceptance check 5, all three steps, on one fixture registry."""
    pinned = resolve(CAPABILITY, _context(), frozen_snapshot())
    assert pinned.selected.version == "3.1.4", "the tenant pin (P2) wins"

    no_pin = resolve(CAPABILITY, _context(), frozen_snapshot(tenant_pin=False))
    assert no_pin.selected.version == "3.3.0", "the higher acceptance metric (P4) wins"

    no_metric = resolve(
        CAPABILITY, _context(), frozen_snapshot(tenant_pin=False, metric_for_330=None)
    )
    assert no_metric.selected.version == "3.4.0", "the newer version (P6) wins"


def test_the_suspended_model_is_never_ranked_and_always_excluded_at_f3() -> None:
    """Acceptance check 5's last sentence. F3 covers the transitive closure, not just the
    ServiceVersion: `sv_350` is APPROVED and its MODEL is SUSPENDED."""
    for snap in (
        frozen_snapshot(),
        frozen_snapshot(tenant_pin=False),
        frozen_snapshot(tenant_pin=False, metric_for_330=None),
    ):
        outcome = resolve(CAPABILITY, _context(), snap)
        assert "sv_350" not in [c.service_version_id for c in outcome.ranked]
        excluded = {e.service_version_id: e for e in outcome.excluded}
        assert excluded["sv_350"].stage == "F3"
        assert excluded["sv_350"].reason_code == "version_suspended"
        assert "mv_suspended" in excluded["sv_350"].detail


def test_a_job_pin_family_outranks_a_tenant_pin_family() -> None:
    """P1 before P2. The arrival path of `MOS-REG-054` depends on exactly this."""
    outcome = resolve(
        CAPABILITY,
        _context(job_pin=VersionPin(service_family=FAMILY, range=">=3.2 <4")),
        frozen_snapshot(),
    )
    assert outcome.selected.service_family == FAMILY
    assert outcome.selected.rank_key.job_pin_family_match == 1


def test_a_missing_metric_ranks_minus_inf_and_is_recorded_as_absent() -> None:
    """`MOS-REG-060`: never 0, never "pass", and the ABSENCE is on the record."""
    outcome = resolve(CAPABILITY, _context(), frozen_snapshot(tenant_pin=False))
    keys = {c.service_version_id: c.rank_key for c in outcome.ranked}
    assert keys["sv_330"].acceptance_metric_present is True
    assert keys["sv_340"].acceptance_metric_present is False
    assert keys["sv_340"].acceptance_metric == float("-inf")
    assert "-Inf" in keys["sv_340"].as_string()
    assert keys["sv_340"].as_document()["acceptance_metric"] is None


def test_a_deprecated_version_still_resolves_but_ranks_last() -> None:
    """`MOS-REG-106`: DEPRECATED stays resolvable so pinned jobs can be replayed; P5
    puts it behind everything that is not deprecated."""
    base = frozen_snapshot(tenant_pin=False, metric_for_330=None)
    from dataclasses import replace

    services = tuple(
        replace(s, lifecycle_status="DEPRECATED") if s.id == "sv_340" else s
        for s in base.services
    )
    snap = replace(base, services=services).with_identity()
    outcome = resolve(CAPABILITY, _context(), snap)
    order = [c.service_version_id for c in outcome.ranked]
    assert order[-1] == "sv_340"
    assert outcome.selected.version == "3.3.0"


def test_the_two_gateable_filters_exist_and_are_configured_off() -> None:
    """`MOS-REG-005`: F8 and F9 "MUST NOT be absent from the code"; they are OFF by
    default and their state travels in the pinned record."""
    off = resolve(CAPABILITY, _context(), frozen_snapshot(tenant_pin=False))
    assert off.is_selected

    on = resolve(
        CAPABILITY,
        _context(),
        frozen_snapshot(tenant_pin=False, gate_config=GateConfig(evidence_gate=True)),
    )
    # Only sv_330 has an active passing ValidationReport in the fixture.
    assert on.selected.service_version_id == "sv_330"
    assert {e.reason_code for e in on.excluded if e.stage == "F8"} == {
        "evidence_gate_failed"
    }

    record = pin_module.resolution_record(
        on, capability_id=CAPABILITY, resolved_at=AS_OF,
        gate_config=GateConfig(evidence_gate=True),
    )
    assert record["gate_config"] == {"evidence_gate": True, "regulatory_gate": False}


def test_the_regulatory_gate_reads_the_tenants_jurisdiction() -> None:
    """F9, and only for a `clinical` deployment (`MOS-REG-083`)."""
    from dataclasses import replace

    base = frozen_snapshot(tenant_pin=False, metric_for_330=None)
    snap = replace(
        base,
        deployments=tuple(
            replace(d, clinical_use_mode="clinical") for d in base.deployments
        ),
        tenants={TENANT: TenantProfile(TENANT, "US")},
        gate_config=GateConfig(regulatory_gate=True),
    ).with_identity()
    outcome = resolve(CAPABILITY, _context(), snap)
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code == "regulatory_gate_failed"


def test_an_unlicensed_or_expired_tenant_is_excluded_at_f2() -> None:
    """F2, with `snap.as_of` as the only clock the resolver may read."""
    from dataclasses import replace

    base = frozen_snapshot(tenant_pin=False, metric_for_330=None)
    expired = replace(
        base,
        licences=(
            LicenceGrant(TENANT, FAMILY, expires_at=AS_OF - timedelta(days=1)),
            LicenceGrant(TENANT, "pulmo.effusion-classic", expires_at=None),
        ),
    ).with_identity()
    outcome = resolve(CAPABILITY, _context(), expired)
    codes = {e.service_version_id: e.reason_code for e in outcome.excluded}
    assert codes["sv_340"] == "licence_expired"
    assert outcome.selected.service_version_id == "sv_314"  # its grant has no expiry


def test_f7_enforces_the_accelerator_inventory_when_one_is_present() -> None:
    from dataclasses import replace

    base = frozen_snapshot(tenant_pin=False, metric_for_330=None)
    services = tuple(replace(s, gpu_required=True) for s in base.services)
    snap = replace(
        base,
        services=services,
        nodes=(
            NodeProfile(
                id="node_1", environment="production",
                gpu_architectures=("sm_70",), gpu_memory_mib=4096,
            ),
        ),
    ).with_identity()
    outcome = resolve(CAPABILITY, _context(), snap)
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code == "runtime_unsatisfiable"


def test_the_envelope_is_consulted_through_the_snapshot_not_reimplemented() -> None:
    """F6's `envelope_mismatch`, via the `EnvelopeLike` protocol."""
    from dataclasses import replace

    class _Out:
        def verdict_zone(self, attributes: Any) -> str:
            return "OUT"

    class _Marginal:
        def verdict_zone(self, attributes: Any) -> str:
            return "MARGINAL"

    base = frozen_snapshot(tenant_pin=False, metric_for_330=None)
    snap = replace(
        base, envelopes={"sv_340": _Out(), "sv_330": _Marginal()}
    ).with_identity()
    outcome = resolve(CAPABILITY, _context(), snap)
    excluded = {e.service_version_id: e for e in outcome.excluded}
    assert excluded["sv_340"].reason_code == "envelope_mismatch"
    # MARGINAL is the tenant's decision at pre-flight (MOS-EVID-101), not the resolver's.
    assert "sv_330" in [c.service_version_id for c in outcome.ranked]


# =====================================================================================
# E. Zero candidates -- MOS-REG-069, MOS-REG-056, MOS-REG-070
# =====================================================================================
def test_an_unknown_capability_is_zero_candidates_not_an_exception() -> None:
    """`MOS-REG-052` + `MOS-REG-069`. Never a 500, never a fallback."""
    outcome = resolve("lung_nodule", _context(), frozen_snapshot())
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code == "capability_unknown"
    assert outcome.selected is None
    assert outcome.ranked == ()


def test_an_exact_pin_on_a_suspended_version_does_not_resurrect_it() -> None:
    """`MOS-REG-056`: F3 runs BEFORE F5, and the reason names the withdrawal."""
    from dataclasses import replace

    base = frozen_snapshot(tenant_pin=False)
    services = tuple(
        replace(s, lifecycle_status="SUSPENDED") if s.id == "sv_340" else s
        for s in base.services
    )
    snap = replace(base, services=services).with_identity()
    outcome = resolve(
        CAPABILITY, _context(job_pin=VersionPin(range="=3.4.0")), snap
    )
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code == "pinned_version_suspended"


def test_an_exact_pin_on_a_recalled_version_is_unreachable_from_the_arrival_path() -> None:
    """`MOS-REG-054`: the study-arrival path pins `=<version>` and MUST NOT reach a
    RECALLED version any more than the capability path can."""
    from dataclasses import replace

    base = frozen_snapshot(tenant_pin=False)
    services = tuple(
        replace(s, lifecycle_status="RECALLED") if s.id == "sv_340" else s
        for s in base.services
    )
    snap = replace(base, services=services).with_identity()
    outcome = resolve(
        CAPABILITY,
        _context(job_pin=VersionPin(service_family=FAMILY, range="=3.4.0")),
        snap,
    )
    assert outcome.reason_code == "pinned_version_recalled"


def test_every_exclusion_carries_a_stage_and_a_reason() -> None:
    """`MOS-REG-070`: the per-candidate reason is what makes a rejection clinical."""
    outcome = resolve(CAPABILITY, _context(modality="MR"), frozen_snapshot())
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code == "modality_mismatch"
    assert outcome.excluded
    for exclusion in outcome.excluded:
        assert exclusion.stage.startswith("F")
        assert exclusion.reason_code
        assert exclusion.detail


def test_a_malformed_stored_range_is_an_exclusion_not_a_raise() -> None:
    """`MOS-REG-052`. The refusal belongs at admission (`validate_range`), and by the time
    a study arrives the only useful answer is a legible rejection."""
    outcome = resolve(
        CAPABILITY, _context(job_pin=VersionPin(range="*")), frozen_snapshot()
    )
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code in ("range_unsatisfied", "pinned_version_not_found")


# =====================================================================================
# F. Determinism -- MOS-REG-053, acceptance check 3
# =====================================================================================
def _random_snapshot(rng: random.Random) -> Snapshot:
    """A random registry state: statuses, roles, versions, metrics, pins, gates."""
    n = rng.randint(0, 5)
    services: list[ServiceVersionRow] = []
    deployments: list[DeploymentRow] = []
    metrics: dict[str, MetricPoint] = {}
    families = ("pulmo.pleural-effusion", "pulmo.effusion-classic")
    for i in range(n):
        sid = f"sv_{i}"
        version = f"{rng.randint(0, 4)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}"
        services.append(
            _service(
                sid,
                version,
                status=rng.choice(
                    ["APPROVED", "VALIDATED", "DEPRECATED", "SUSPENDED", "RECALLED",
                     "DRAFT", "REGISTERED", "VALIDATING"]
                ),
                family=rng.choice(families),
                models=rng.choice([("mv_a",), ("mv_suspended",), ("mv_a", "mv_b")]),
                modalities=rng.choice([("CT",), ("MR",), ("CT", "MR")]),
                capabilities=rng.choice([(CAPABILITY,), ("lung_segmentation",)]),
            )
        )
        if rng.random() < 0.8:
            deployments.append(
                _deployment(
                    f"dep_{i}",
                    sid,
                    role=rng.choice(["ACTIVE", "CANARY", "SHADOW", "STANDBY"]),
                    state=rng.choice(["SERVING", "PENDING", "DRAINING", "RETIRED"]),
                    permille=rng.choice([0, 100, 1000]),
                )
            )
        if rng.random() < 0.5:
            metrics[f"{sid}|{CAPABILITY}|dv_acceptance"] = MetricPoint(
                value=round(rng.random(), 4), metric="dice_coefficient"
            )
    pins: tuple[TenantPin, ...] = ()
    if rng.random() < 0.4:
        pins = (
            TenantPin(
                tenant_id=TENANT,
                capability_id=CAPABILITY,
                service_family=rng.choice(families),
                version_range=rng.choice(["", ">=2 <5", "^3.0.0", "=3.2.1"]),
            ),
        )
    return Snapshot(
        epoch=rng.randint(1, 10**6),
        as_of=AS_OF,
        capabilities={CAPABILITY: Capability(id=CAPABILITY, primary_metric="dice_coefficient")},
        services=tuple(services),
        models={
            "mv_a": ModelVersionRow("mv_a", "f", "1.0.0", "APPROVED"),
            "mv_b": ModelVersionRow("mv_b", "f", "1.1.0", "VALIDATED"),
            "mv_suspended": ModelVersionRow("mv_suspended", "f", "2.0.0", "SUSPENDED"),
        },
        preproc={"ps_a": PreprocRow("ps_a", "f", "2.0.0", "APPROVED")},
        deployments=tuple(deployments),
        tenant_pins=pins,
        metrics=metrics,
        tenants={TENANT: TenantProfile(TENANT, "EU")},
        acceptance_datasets={f"{TENANT}|{CAPABILITY}": "dv_acceptance"},
        gate_config=GateConfig(
            evidence_gate=rng.random() < 0.2, regulatory_gate=rng.random() < 0.2
        ),
    ).with_identity()


def test_ten_thousand_random_registry_states_resolve_identically_twice() -> None:
    """`MOS-REG-053`, at its own stated scale.

    "This MUST be asserted by a property test running 10 000 random registry states twice
    each." The generator is seeded, so a failure is reproducible from the case index.
    """
    rng = random.Random(0xC0FFEE)
    for case in range(10_000):
        snap = _random_snapshot(rng)
        study = f"stu_{rng.randrange(10**6):06d}"
        pin = rng.choice([None, VersionPin(range=">=3 <4"), VersionPin(FAMILY, "^3.0.0")])
        ctx = _context(study_constraints={"study_id": study}, job_pin=pin)
        first = resolve(CAPABILITY, ctx, snap)
        second = resolve(CAPABILITY, ctx, snap)
        assert first.canonical_bytes() == second.canonical_bytes(), f"case {case}"


def test_the_order_does_not_depend_on_the_order_rows_arrive_in() -> None:
    """`MOS-REG-053`'s "Map iteration MUST be sorted before it can affect output".

    A snapshot is a set of rows; two loaders that read the same registry in a different
    order must resolve identically. This is the property a `dict`/`set` walk breaks.
    """
    from dataclasses import replace

    rng = random.Random(7)
    for case in range(200):
        snap = _random_snapshot(rng)
        shuffled_services = list(snap.services)
        shuffled_deployments = list(snap.deployments)
        rng.shuffle(shuffled_services)
        rng.shuffle(shuffled_deployments)
        other = replace(
            snap,
            services=tuple(shuffled_services),
            deployments=tuple(shuffled_deployments),
        )
        a = resolve(CAPABILITY, _context(), snap)
        b = resolve(CAPABILITY, _context(), replace(other, snapshot_id=snap.snapshot_id))
        assert [c.service_version_id for c in a.ranked] == [
            c.service_version_id for c in b.ranked
        ], f"case {case}"
        assert a.selected == b.selected


def test_ranked_is_a_total_order() -> None:
    """`MOS-REG-061`: no two candidates may share a rank position."""
    rng = random.Random(11)
    for _ in range(500):
        snap = _random_snapshot(rng)
        outcome = resolve(CAPABILITY, _context(), snap)
        keys = [c.rank_key.as_string() + "|" + c.content_digest for c in outcome.ranked]
        assert len(keys) == len(set(keys))


def test_resolve_never_raises_for_any_generated_state() -> None:
    """`MOS-REG-052`, as a property rather than as a promise."""
    rng = random.Random(0x5EED)
    for _ in range(2_000):
        snap = _random_snapshot(rng)
        outcome = resolve(
            rng.choice([CAPABILITY, "lung_nodule"]),
            _context(job_pin=rng.choice([None, VersionPin(range="=9.9.9")])),
            snap,
        )
        assert outcome.decision in ("SELECTED", "ZERO_CANDIDATES")
        assert (outcome.selected is None) == (outcome.decision == "ZERO_CANDIDATES")
        assert bool(outcome.reason_code) == (outcome.decision == "ZERO_CANDIDATES")


# =====================================================================================
# G. THE PIN -- MOS-REG-066, MOS-REG-067, MOS-REG-068, against a real database
# =====================================================================================
APP_PASSWORD = "medos_app"


@pytest.fixture()
def db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """A connection with the registry, the deployments and the job tables emptied."""
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(
        "TRUNCATE artifacts, registry_changelog, deployments, jobs, job_queue, "
        "job_events, job_steps, job_series CASCADE"
    )
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant

        reset_current_tenant(token)
        conn.close()


SUPPLY_CHAIN: dict[str, Any] = {
    "oci_image_digest": "sha256:" + "a1" * 32,
    "signature": b"cosign-bundle-bytes",
    "signature_alg": "cosign-sigstore",
    "signer_identity": SIGNER,
    "sbom_object_key": "sbom/pulmo-effusion.cdx.json",
}


def _model_manifest(version: str, run_id: str) -> dict[str, Any]:
    manifest = {
        "schema_version": "1.0.0",
        "kind": "model_version",
        "family": "pulmo.effusion-unet",
        "version": version,
        "publisher": {"org_id": "org_pulmoai", "signing_identity": SIGNER},
        "created_at": "2026-01-09T11:02:41Z",
        "spec": {
            "capabilities": [CAPABILITY],
            "weights_availability": "platform_managed",
            "weights": {
                "oci_ref": "ghcr.io/pulmoai/effusion-unet",
                "digest": "sha256:" + "5e" * 32,
                "format": "onnx",
                "size_bytes": 184236032,
            },
            "preprocessing_spec_ref": "ps_pulmo_effusion_prep_2_0_0",
            "golden_fixture": {
                "sha256": "sha256:" + "0c" * 32,
                "output_tensor_sha256": "sha256:" + "b9" * 32,
                "output_shape": [1, 2, 128, 192, 192],
            },
            "io": {
                "input": {"name": "input", "shape": [1, 1, 128, 192, 192],
                          "dtype": "float32", "layout": "NCZYX", "orientation": "LPS",
                          "value_range": [-1.0, 1.0]},
                "output": {"name": "logits", "shape": [1, 2, 128, 192, 192],
                           "dtype": "float32", "layout": "NCZYX",
                           "kind": "segmentation_logits",
                           "label_map": {"0": "background", "1": "pleural_effusion"}},
            },
            "operating_point": {
                "kind": "probability_threshold", "score_threshold": 0.45,
                "selected_on_evaluation_run": run_id,
                "selection_rule": "max F1 on the sealed validation split",
            },
            "runtime": {"engine": "triton", "engine_version": ">=24.08 <25.00",
                        "backend": "onnxruntime",
                        "gpu_architectures": ["sm_80", "sm_86"], "cuda": ">=12.1 <13",
                        "driver_min": "535.104.05", "gpu_memory_mib": 9216},
            "derived_from": None,
            "evaluation_run_id": run_id,
            "applicability_envelope_ref": "ae_chest_ct_v2",
            "not_validated_for": ["studies with slice thickness > 3.0 mm"],
            "known_failure_modes": ["loculated effusions are under-segmented"],
        },
    }
    manifest["content_digest"] = content_digest_of(manifest)
    return manifest


def _service_manifest(version: str, model_ref: str) -> dict[str, Any]:
    manifest = {
        "schema_version": "1.0.0",
        "kind": "service_version",
        "family": FAMILY,
        "version": version,
        "publisher": {"org_id": "org_pulmoai", "signing_identity": SIGNER},
        "created_at": "2026-01-09T11:02:41Z",
        "spec": {
            "mode": "native",
            "image": {"ref": "ghcr.io/pulmoai/pleural-effusion",
                      "digest": "sha256:" + "a1" * 32},
            "capabilities": [{"id": CAPABILITY,
                              "outputs": ["segmentation", "measurement"]}],
            "modalities": ["CT"],
            "series_selector_ref": "sel_chest_ct_thin_axial_v3",
            "models": [{"ref": model_ref, "role": "primary"}],
            "preprocessing_specs": [],
            "resources": {"gpu_required": True, "gpu_memory_mib": 9216,
                          "cpu_millicores": 4000, "memory_mib": 24576,
                          "max_concurrent_jobs": 2},
            "engineering_acceptance": {"p95_wall_clock_seconds": 180,
                                       "max_gpu_memory_mib": 11264,
                                       "result_bundle_schema_validity": 1.0},
            "legal_manufacturer": {"id": "lm_pulmoai_gmbh", "name": "PulmoAI GmbH",
                                   "device_serial_number": "PULMO-EFF-0003",
                                   "software_versions": version},
            "regulatory_status": [{"jurisdiction": "EU",
                                   "status": "not_a_medical_device",
                                   "evidence_ref": None}],
            "compatibility": {
                "medicalos_api": ">=1.0 <2", "service_contract": "1.2",
                "runtime": {"engine": "triton", "engine_version": ">=24.08 <25.00",
                            "backend": "onnxruntime",
                            "gpu_architectures": ["sm_80", "sm_86"],
                            "cuda": ">=12.1 <13", "driver_min": "535.104.05",
                            "gpu_memory_mib": 9216},
            },
        },
    }
    manifest["content_digest"] = content_digest_of(manifest)
    return manifest


def _publish(conn: psycopg.Connection[Any], manifest: dict[str, Any], public_id: str,
             *, status: str = "APPROVED") -> dict[str, Any]:
    row, _created = registry_repo.publish(
        conn, manifest=manifest, public_id=public_id, actor=ACTOR, trace_id=TRACE,
        **SUPPLY_CHAIN,
    )
    conn.commit()
    for step in ("VALIDATING", "VALIDATED", "APPROVED"):
        row = registry_repo.set_status(
            conn, public_id, to_status=step, reason=None, actor=ACTOR, trace_id=TRACE
        )
        if step == status:
            break
    conn.commit()
    return row


def _deploy(conn: psycopg.Connection[Any], public_id: str, subject: str, version: str,
            *, role: str = "ACTIVE", state: str = "SERVING", commit: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO deployments (public_id, tenant_id, environment, capability_id,
            subject_kind, subject_id, subject_version, role, state, clinical_use_mode,
            pin_range, verification_ref, created_by)
        VALUES (%s, %s, 'production', %s, 'service_version', %s, %s, %s, %s,
                'research_only', %s, 'ver_fixture', %s)
        """,
        (public_id, TENANT, CAPABILITY, subject, version, role, state, "^3.0.0",
         ACTOR.id),
    )
    if commit:
        conn.commit()


DEPLOYMENT_IDS = {
    # `deployments.public_id` is `dep_` + a 26-character Crockford base32 ULID in
    # upper case (ch. 10's grammar, CHECKed by 0008). Fixed values, so the assertions
    # below can name the row they mean.
    "3_2_1": "dep_01JQ90A4TT0000000000000000",
    "3_4_0": "dep_01JT5R2Q8N0000000000000000",
}


def _publish_release(conn: psycopg.Connection[Any], version: str, *, suffix: str,
                     run_id: str, deploy_role: str | None = "ACTIVE") -> str:
    """One model + one service at `version`, optionally deployed. Returns the sv id."""
    mv = f"mv_pulmo_effusion_unet_{suffix}"
    sv = f"sv_pulmo_effusion_{suffix}"
    _publish(conn, _model_manifest(version, run_id), mv)
    _publish(conn, _service_manifest(version, mv), sv)
    if deploy_role:
        _deploy(conn, DEPLOYMENT_IDS[suffix], sv, version, role=deploy_role)
    return sv


def test_the_pin_survives_a_newer_approved_version(
    db: psycopg.Connection[Any],
) -> None:
    """§6.7.7 end to end, against the real registry. `MOS-REG-066`/`MOS-REG-067`.

    State A: `3.2.1` is APPROVED and ACTIVE. A job is created and the resolved set is
    pinned into it in the SAME transaction.
    State B: `3.4.0` is published, approved and deployed ACTIVE; the epoch moves.

    A fresh resolution now selects `3.4.0` -- the drift is real and this test proves the
    registry moved. The EXISTING job still runs `3.2.1`, because the retry path reads the
    pin and has no snapshot to resolve against. That is the difference between "the same
    job id reported 642 mL and then 705 mL" and a legible clinical record.
    """
    sv_321 = _publish_release(db, "3.2.1", suffix="3_2_1", run_id="er_01JP4T9X7B")

    snap_a = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF
    )
    outcome_a = resolve(CAPABILITY, _context(), snap_a)
    assert outcome_a.is_selected
    assert outcome_a.selected.service_version_id == sv_321
    assert outcome_a.selected.version == "3.2.1"
    assert outcome_a.selected.model_version_ids == ("mv_pulmo_effusion_unet_3_2_1",)
    # MOS-REG-115: environment and state travel with the pin.
    assert outcome_a.selected.deployment_environment == "production"
    assert outcome_a.selected.deployment_state == "SERVING"

    record = pin_module.resolution_record(
        outcome_a, capability_id=CAPABILITY, resolved_at=snap_a.as_of
    )
    queue = PostgresJobQueue(db)
    spec = jobs_repo.JobSpec(
        study_instance_uid="1.2.826.0.1.3680043.8.498.44100",
        capability_ids=(CAPABILITY,),
        service_id="pulmo.pleural-effusion",
        service_version=outcome_a.selected.version,
    )
    # MOS-REG-066: "written into the Job row in the same transaction that creates the
    # job, before any enqueue". `tenant_tx` joins the open transaction as a SAVEPOINT, so
    # this really is one transaction and not two.
    from medos.db.tenancy import tenant_tx

    with tenant_tx(db):
        created = jobs_repo.create_job_queued(db, queue, spec)
        pin_module.write_pin(db, job_public_id=created.job_id, record=record)
    db.commit()

    # ---- State B: a newer version is published, approved and deployed ----------------
    # The cutover is ONE transaction, as `MOS-REG-077` requires and as
    # `deployments_one_active_per_slot` (`MOS-REG-074`) enforces: two ACTIVE/SERVING rows
    # in one `(tenant, environment, capability)` slot is a configuration error, not a
    # load-balancing strategy, so the old role is demoted in the same breath.
    sv_340 = _publish_release(db, "3.4.0", suffix="3_4_0", run_id="er_01JT5R2Q8N",
                              deploy_role=None)
    with db.transaction():
        db.execute(
            "UPDATE deployments SET role = 'STANDBY' WHERE public_id = %s",
            (DEPLOYMENT_IDS["3_2_1"],),
        )
        _deploy(db, DEPLOYMENT_IDS["3_4_0"], sv_340, "3.4.0", commit=False)
    db.commit()

    snap_b = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF + timedelta(days=48)
    )
    assert snap_b.epoch > snap_a.epoch, "the registry epoch must have moved (MOS-REG-010)"
    assert snap_b.snapshot_id != snap_a.snapshot_id

    fresh = resolve(CAPABILITY, _context(), snap_b)
    assert fresh.selected.service_version_id == sv_340, (
        "a NEW job must see the new version -- otherwise this test proves nothing"
    )

    # ---- and the existing job still resolves to the pinned set -----------------------
    pinned = retry_module.pinned_selection(db, created.job_id)
    assert pinned.service_version_id == sv_321
    assert pinned.version == "3.2.1"
    assert pinned.model_version_ids == ("mv_pulmo_effusion_unet_3_2_1",)
    assert pinned.epoch == snap_a.epoch
    assert pinned.snapshot_id == snap_a.snapshot_id
    assert pinned.deployment_environment == "production"
    # The pinned deployment state is the one at resolution (MOS-REG-115): the row has
    # since moved to STANDBY, and the pin is NOT rewritten to match it.
    assert pinned.deployment_state == "SERVING"
    assert pinned.deployment_role == "ACTIVE"
    row = db.execute(
        "SELECT role FROM deployments WHERE public_id = %s",
        (DEPLOYMENT_IDS["3_2_1"],),
    ).fetchone()
    assert row["role"] == "STANDBY"


def test_the_pinned_record_is_immutable_in_the_database(
    db: psycopg.Connection[Any],
) -> None:
    """The pin is not merely "not overwritten by our code": the database refuses.

    `job_events_no_update` / `job_events_no_delete` (schema.sql, `MOS-EXEC-013`) are the
    structural half of `MOS-STORE-267`'s `jobs_resolution_pinned` trigger, which cannot be
    used here because the columns chapter 12 §12.10 declares do not exist in this build --
    see `medos/medos/resolution/pin.py` for the defect report.
    """
    _publish_release(db, "3.2.1", suffix="3_2_1", run_id="er_01JP4T9X7B")
    snap = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF
    )
    outcome = resolve(CAPABILITY, _context(), snap)
    record = pin_module.resolution_record(
        outcome, capability_id=CAPABILITY, resolved_at=snap.as_of
    )
    queue = PostgresJobQueue(db)
    from medos.db.tenancy import tenant_tx

    with tenant_tx(db):
        created = jobs_repo.create_job_queued(
            db,
            queue,
            jobs_repo.JobSpec(
                study_instance_uid="1.2.826.0.1.3680043.8.498.44101",
                capability_ids=(CAPABILITY,),
                service_id="pulmo.pleural-effusion",
                service_version="3.2.1",
            ),
        )
        pin_module.write_pin(db, job_public_id=created.job_id, record=record)
    db.commit()

    with pytest.raises(psycopg.Error, match="append-only"):
        db.execute(
            "UPDATE job_events SET payload = '{\"resolution\": {}}'::jsonb "
            "WHERE event_type = 'job.requested'"
        )
    db.rollback()
    with pytest.raises(psycopg.Error, match="append-only"):
        db.execute("DELETE FROM job_events WHERE event_type = 'job.requested'")
    db.rollback()

    # And it still reads back verbatim.
    assert retry_module.pinned_selection(db, created.job_id).version == "3.2.1"


def test_a_withdrawn_pinned_version_stops_the_retry(
    db: psycopg.Connection[Any],
) -> None:
    """`MOS-REG-068`: the retry MUST NOT run; the job is REJECTED, never re-resolved."""
    _publish_release(db, "3.2.1", suffix="3_2_1", run_id="er_01JP4T9X7B")
    snap = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF
    )
    outcome = resolve(CAPABILITY, _context(), snap)
    record = pin_module.resolution_record(
        outcome, capability_id=CAPABILITY, resolved_at=snap.as_of
    )
    queue = PostgresJobQueue(db)
    from medos.db.tenancy import tenant_tx

    with tenant_tx(db):
        created = jobs_repo.create_job_queued(
            db,
            queue,
            jobs_repo.JobSpec(
                study_instance_uid="1.2.826.0.1.3680043.8.498.44102",
                capability_ids=(CAPABILITY,),
                service_id="pulmo.pleural-effusion",
                service_version="3.2.1",
            ),
        )
        pin_module.write_pin(db, job_public_id=created.job_id, record=record)
    db.commit()

    pinned = retry_module.pinned_selection(db, created.job_id)
    assert retry_module.retry_refusal(
        retry_module.pinned_version_status(db, pinned)
    ) is None

    registry_repo.set_status(
        db, "mv_pulmo_effusion_unet_3_2_1", to_status="RECALLED",
        reason="weights defect found in post-market surveillance",
        actor=ACTOR, trace_id=TRACE,
    )
    db.commit()

    refusal = retry_module.retry_refusal(
        retry_module.pinned_version_status(db, pinned)
    )
    assert refusal is not None
    assert refusal[0] == "pinned_version_recalled"
    assert "mv_pulmo_effusion_unet_3_2_1" in refusal[1]


def test_replay_reproduces_the_outcome_byte_for_byte(
    db: psycopg.Connection[Any],
) -> None:
    """Chapter 6 acceptance check 8, `MOS-REG-071`.

    The snapshot is rebuilt from the same registry state and the same `inputs_hash`; the
    `Outcome` must be byte-identical. What this build can prove is the pure half: given
    the recorded `(epoch, snapshot_id, inputs_hash)`, the resolver reproduces the answer.
    Rebuilding a HISTORICAL snapshot from `registry_changelog` needs the changelog replay
    that `MOS-REG-011` provides and no component has yet written -- reported, not faked.
    """
    _publish_release(db, "3.2.1", suffix="3_2_1", run_id="er_01JP4T9X7B")
    first = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF
    )
    second = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF
    )
    assert first.snapshot_id == second.snapshot_id
    assert first.epoch == second.epoch

    a = resolve(CAPABILITY, _context(), first)
    b = resolve(CAPABILITY, _context(), second)
    assert a.canonical_bytes() == b.canonical_bytes()
    assert a.inputs_hash == b.inputs_hash

    record = pin_module.resolution_record(
        a, capability_id=CAPABILITY, resolved_at=first.as_of
    )
    assert record["snapshot_id"] == first.snapshot_id
    assert record["inputs_hash"] == a.inputs_hash
    assert record["epoch"] == first.epoch


def test_the_vocabulary_holds_what_this_deployment_serves(
    db: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.6's capability vocabulary is the DEPLOYMENT's set, not the platform singleton.

    THE DEFECT, AND WHY IT WAS NARROW ENOUGH TO SURVIVE. `_capability_vocabulary` UNIONS
    `acceptance_criteria.capability_id` with a second source, and that second source was
    `medos.capabilities.REGISTRY`. So a deployment-configured capability that happened to
    have an acceptance-criteria row was already present, and one without a row -- the
    ordinary case for a capability a tenant has just enabled -- was not. `F1` then answered
    `capability_unknown` for a capability the API had admitted and the worker had run.

    WHY THAT IS WORSE THAN A WRONG ANSWER. `MOS-REG-066` writes the resolved set into the
    `Job` row in the transaction that creates the job, and `MOS-REG-067` makes every retry
    read that pin rather than re-resolve. A vocabulary missing a capability is therefore
    not a transient rejection that a later publish fixes; it is pinned into the job.

    WHAT THIS DOES NOT DISTURB. The read happens in the LOADER, and the loader is the
    impure half `MOS-REG-051` requires so that `resolve()` can be a pure function of
    `(capability, context, registry_snapshot)`. The vocabulary is frozen into the
    `Snapshot` before any resolution runs, so `tests/gate/test_resolution_purity.py`'s
    property -- two resolutions of one frozen snapshot are byte-identical -- is untouched.
    """
    served = serve_one_extra_capability(monkeypatch)

    snap = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF
    )

    assert set(snap.capabilities) >= served
    assert snap.capabilities[VENDOR_CAPABILITY_ID].status == "supported"
    # No `acceptance_criteria` row for it, which is the case the union used to miss.
    assert snap.capabilities[VENDOR_CAPABILITY_ID].acceptance_criteria_ref == ""

    # And the resolver now answers about it. ZERO_CANDIDATES because this fixture registry
    # holds no service claiming it -- but NOT `capability_unknown`, which is the answer a
    # vocabulary read out of the platform singleton produced for work already in flight.
    outcome = resolve(VENDOR_CAPABILITY_ID, _context(), snap)
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code != "capability_unknown"


def test_the_loader_projects_mos_reg_023s_index_columns(
    db: psycopg.Connection[Any],
) -> None:
    """The snapshot's hard-filter columns come from the stored manifest, once."""
    sv = _publish_release(db, "3.2.1", suffix="3_2_1", run_id="er_01JP4T9X7B",
                          deploy_role=None)
    snap = snapshot_module.load_snapshot(
        db, tenant_id=TENANT, environment="production", as_of=AS_OF
    )
    row = next(s for s in snap.services if s.id == sv)
    assert row.capabilities == (CAPABILITY,)
    assert row.mode == "native"
    assert row.modalities == ("CT",)
    assert row.model_version_refs == ("mv_pulmo_effusion_unet_3_2_1",)
    assert row.gpu_required is True
    assert row.gpu_architectures == ("sm_80", "sm_86")
    assert row.legal_manufacturer_id == "lm_pulmoai_gmbh"
    assert row.regulatory_jurisdictions == ("EU",)
    assert row.lifecycle_status == "APPROVED"

    # No deployment -> F4 excludes it, and the job is REJECTED with a clinical reason.
    outcome = resolve(CAPABILITY, _context(), snap)
    assert outcome.decision == "ZERO_CANDIDATES"
    assert outcome.reason_code == "no_deployment"
