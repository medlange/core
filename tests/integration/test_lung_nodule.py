# SPDX-License-Identifier: Apache-2.0
"""`lung_nodule` as a SECOND capability -- and the `zero-core-change` question it answers.

WHAT IS ACTUALLY BEING TESTED HERE
-----------------------------------
`MOS-REL-020`: "The block MUST end by adding lung nodule detection on LIDC-IDRI as a second
capability, and the resulting diff MUST touch no core code. This is the platform's real
acceptance test and it is more informative than any end-to-end demo: if the second
capability requires a core edit, that is learned in week 13 rather than in year two."

So the interesting assertions are not "the detector works". They are:

  * the capability reaches the worker through an INJECTED registry, with
    `medos.capabilities.REGISTRY` unmutated (`test_the_platform_registry_is_untouched`);
  * its coded concepts, UCUM units, envelope and clinical block are REGISTRY DATA that the
    code reads, not a Python copy of the same facts (`test_the_registry_row_is_the_envelope`);
  * the Service Plane imports nothing chapter 2 section 2.7 forbids it
    (`test_service_plane_imports_nothing_it_must_not` -- `MOS-SVC-038`'s static scan);
  * a job runs end to end on a real LIDC-IDRI study and a SEG and an SR come out
    (`test_end_to_end_on_a_real_lidc_study`);
  * and the places where the platform did NOT accommodate it are pinned as tests, so they
    are defects on record rather than prose in a report:
    `test_two_label_maps_in_one_job_is_refused`,
    `test_a_detection_with_nothing_to_draw_fails_the_job`, and
    `test_the_service_version_cannot_be_published_without_fabricating_evidence`.

    The sharpest of them WAS
    `test_only_this_capability_deployed_fails_the_job_at_write_dicom`, which ran a real job
    for a deployment shipping ONLY this capability and watched the platform's own write
    step die on a hard-coded capability name. That defect is now FIXED in
    `medos.worker.steps.producing_model_version`, and the test is inverted rather than
    deleted: `test_only_this_capability_deployed_completes_and_declares_its_own_version`
    runs the same job, on the same registry of one, and asserts both halves of the old
    defect are gone -- it completes, and the SEG and SR declare THIS capability's version
    rather than the one a co-installed capability happened to have. The capability's
    measurement is unaffected either way: the fix is a core edit, made after 0.3.0 banked
    `zero-core-change`, and nothing in `medos/services/` moved for it.

WHY NO `tests/gate/test_zero_core_change.py`
---------------------------------------------
`tests/unit/test_gate_contract.py` declares 0.3.0 in `NOT_YET` and asserts that no module
for a 0.3.0 check exists under `tests/gate/`: "a module for `zero-core-change` sitting there
before 0.3.0 is claimed is a check nobody agreed to gate a release on". Claiming 0.3.0 means
moving it to `IMPLEMENTED`, which that test then requires ALL EIGHT of the row's checks for
-- `zero-core-change`, `resolution-purity`, `queue-driver-parity`, `sealed-mode-isolation`
and chapter 17's four. That is the release's decision, not this component's, so the
measurement lives here as an ordinary integration test and the gate module is left for
whoever closes 0.3.0.

THE CORPUS
-----------
`MEDOS_E2E_LCTSC_ROOT` (default `F:/WorkSpace/PulmoAI/TCIA`) is opened READ-ONLY and never
written. The two studies used are chosen for a measured property -- one yields candidates
and one does not -- and both are named, so a failure is reproducible.

NOTE ON LIDC-IDRI'S ANNOTATIONS -- THE REASON NOTHING HERE MEASURES SENSITIVITY.
The published collection carries four independent radiologists' nodule contours per case as
XML. Those files are NOT in this corpus: 25 `LIDC-IDRI-*` case directories were walked and
every single file under them is a `.dcm`. There is therefore no reference standard on this
machine to score against.

So nothing below measures sensitivity, specificity or a false-positive rate, and no test
here may be read as evidence that this detector finds nodules -- only that the platform ran
it and produced well-formed, correctly coded output. `acceptance_criteria.status` is `unmet`
for exactly this reason, and `MOS-SVC-021` independently forbids displaying such a number
for a capability with no operating point.

Spec: MOS-REL-020, MOS-SVC-002, MOS-SVC-020, MOS-SVC-021, MOS-SVC-038, MOS-SVC-053..061,
MOS-IMG-039, MOS-IMG-098, MOS-IMG-112, MOS-IMG-121, MOS-REG-016, MOS-REG-025, MOS-REG-042,
MOS-REG-044, MOS-REG-049, MOS-SAFE-014, MOS-SAFE-015.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest
from medos.core.errors import SystemFailure
from medos.core.geometry import build_canonical_volume
from medos.db import repo
from medos.db.queue import PostgresJobQueue
from medos.db.repo import JobSpec
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    bind_current_tenant,
    reset_current_tenant,
)
from medos.dicomweb.gateway import FetchedSeries, SeriesSummary
from medos.registry.jsonschema import check_schema, iter_errors
from medos.registry.schemas import schema_for_kind
from medos.worker.runner import RunnerConfig, WorkerRunner
from medos.worker.steps import WorkerDeps
from psycopg.rows import dict_row
from services.lung_nodule import concepts as overlay
from services.lung_nodule import detector
from services.lung_nodule import registry as svc_registry
from services.lung_nodule.service import CAPABILITY_ID, VERSION, LungNodule

from tests._support.skips import skip_infra, skip_no_data

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "medos" / "examples" / "lung-nodule"
SCHEMAS = REPO_ROOT / "medos" / "schemas" / "lung-nodule"
SERVICE_PKG = REPO_ROOT / "medos" / "services" / "lung_nodule"

CORPUS = Path(os.environ.get("MEDOS_E2E_LCTSC_ROOT", "F:/WorkSpace/PulmoAI/TCIA"))

#: MEASURED on this corpus, at the parameters in `detector.PARAMETERS`. Named rather than
#: discovered at run time so that a change in behaviour is a test failure with a case id in
#: it, not a silently different study.
CASE_WITH_CANDIDATES = "LIDC-IDRI-0001"  # 133 slices, dS 2.5 mm -> 2 candidates
CASE_WITHOUT_CANDIDATES = "LIDC-IDRI-0019"  # 305 slices, dS 1.25 mm -> 0 candidates


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture()
def wconn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """Autocommit connection with the job tables emptied. Mirrors `test_worker.py::wconn`."""
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=True)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(
        "TRUNCATE jobs, job_queue, job_events, job_steps, job_series, "
        "results, result_measurements, result_dicom_objects CASCADE"
    )
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        reset_current_tenant(token)
        conn.close()


@pytest.fixture(scope="session")
def registration() -> dict[str, Any]:
    return json.loads((EXAMPLES / "capability.json").read_text(encoding="utf-8"))


class FakeGateway:
    """A `DicomWebGateway` stand-in serving real files off disk.

    A fake and not a mock: the pipeline below must really decode LIDC-IDRI pixels, really
    build a canonical volume from `ImagePositionPatient`, and really threshold Hounsfield
    units. A mock that recorded calls would let every assertion pass while nothing was
    measured.

    It COPIES out of the corpus into the worker's scratch directory and never writes back;
    `MEDOS_E2E_LCTSC_ROOT` is read-only.
    """

    def __init__(self, series: Sequence[SeriesSummary], files: Sequence[Path]) -> None:
        self.series = list(series)
        self.files = list(files)
        self.stored: list[Path] = []

    def list_series(self, study_instance_uid: str) -> list[SeriesSummary]:
        return list(self.series)

    def fetch_series(
        self,
        study_instance_uid: str,
        series_instance_uid: str,
        dest_dir: Path,
        *,
        verify_against_qido: bool = True,
    ) -> FetchedSeries:
        dest_dir.mkdir(parents=True, exist_ok=True)
        out: list[Path] = []
        for src in self.files:
            dst = dest_dir / src.name
            dst.write_bytes(src.read_bytes())
            out.append(dst)
        return FetchedSeries(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            directory=dest_dir,
            paths=tuple(out),
            sop_instance_uids=tuple(p.stem for p in out),
            bytes_written=sum(p.stat().st_size for p in out),
        )

    def series_exists(self, study_instance_uid: str, series_instance_uid: str) -> bool:
        return False

    def store_files(self, paths: Sequence[Path], **_: Any) -> list[Any]:
        self.stored.extend(paths)
        return []

    def assert_stored(self, *args: Any, **kwargs: Any) -> set[str]:
        return set()


def _case_files(case: str) -> list[Path]:
    """Every instance of the largest series in `case`, or a `skip_no_data`."""
    if not CORPUS.is_dir():
        skip_no_data(
            f"no TCIA corpus at {CORPUS} (set MEDOS_E2E_LCTSC_ROOT)", corpus="tcia-corpus"
        )
    root = CORPUS / case
    if not root.is_dir():
        skip_no_data(f"{case} is not a directory under {CORPUS}", corpus="lidc-idri")
    by_dir: dict[Path, list[Path]] = {}
    for p in root.rglob("*.dcm"):
        by_dir.setdefault(p.parent, []).append(p)
    if not by_dir:
        skip_no_data(f"no .dcm instances under {root}", corpus="lidc-idri")
    return sorted(max(by_dir.values(), key=len))


def _case_uids(files: Sequence[Path]) -> tuple[str, str]:
    """The REAL `(StudyInstanceUID, SeriesInstanceUID)` off the instances on disk.

    Invented UIDs are not an option here, and the platform is right to refuse them:
    `require_source_grid` compares `ctx.series_instance_uid` to the volume's own, because
    "the provenance of any result would name the wrong series (MOS-IMG-121)". A fake
    `SeriesSummary` UID over real pixels is exactly that mismatch, and the first version of
    this test hit it.

    LIDC-IDRI is public, de-identified, CC BY 3.0 data, so its UIDs carry no PHI.
    """
    import pydicom

    ds = pydicom.dcmread(str(files[0]), stop_before_pixels=True)
    return str(ds.StudyInstanceUID), str(ds.SeriesInstanceUID)


def _deps(tmp_path: Path, gateway: FakeGateway, *, base: Any = None) -> WorkerDeps:
    """`WorkerDeps` wired the way a deployment shipping this capability would wire it.

    THIS IS THE SEAM `MOS-REL-020` TURNS ON. No core module is imported for its contents,
    nothing global is mutated, and the only thing that changed relative to a stock worker is
    two constructor arguments.
    """
    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    return WorkerDeps(
        gateway=gateway,
        work_root=tmp_path / "work",
        registry=svc_registry.worker_registry(concepts, base=base),
        concepts=concepts,
    )


def _run_job(
    wconn: psycopg.Connection[Any],
    pg_dsn: str,
    tmp_path: Path,
    case: str,
    *,
    caps: tuple[str, ...] = (CAPABILITY_ID,),
    base: Any = None,
) -> tuple[Any, str, FakeGateway]:
    files = _case_files(case)
    study_uid, series_uid = _case_uids(files)
    created = repo.create_job_queued(
        wconn,
        PostgresJobQueue(wconn),
        JobSpec(
            study_instance_uid=study_uid,
            capability_ids=caps,
            max_attempts=1,
            parameters={},
        ),
    )
    gateway = FakeGateway(
        series=[SeriesSummary(study_uid, series_uid, "CT", len(files))], files=files
    )
    cfg = RunnerConfig(
        dsn=pg_dsn,
        work_root=tmp_path / "work",
        worker_id="wrk_lungnodule_test",
        lease_seconds=1800,
        heartbeat_seconds=60,
        reclaim_on_poll=False,
    )
    runner = WorkerRunner(cfg, conn=wconn, deps=_deps(tmp_path, gateway, base=base))
    return runner.run_once(), created.job_id, gateway


# =====================================================================================
# 1. The capability is DATA the platform resolves, not code the platform imports
# =====================================================================================
def test_the_registry_row_validates_against_its_published_schema(
    registration: dict[str, Any],
) -> None:
    """The Capability row is schema-checked by the PLATFORM's validator, not by hand.

    `medos.registry.jsonschema` is the one this repository ships; using it here means the
    row is held to the same keyword set and the same closed-object discipline as a
    `service_version` manifest. `MOS-REG-008` keeps `Capability` out of the artifact kinds
    ("a curated vocabulary, not a shipped unit"), which is why it has its own schema under
    `medos/schemas/` rather than a row in `artifact_manifest_schemas`.
    """
    schema = json.loads(
        (SCHEMAS / "capability-registration-1.0.0.json").read_text(encoding="utf-8")
    )
    check_schema(schema, registry={})
    assert iter_errors(registration, schema, registry={}) == []

    # MOS-API-084 / chapter 10's check 20: `schemas.medicalos.org` is the RETIRED authority
    # and MUST NOT appear in any `$id` or `$ref`. docs/spec/99-known-inconsistencies.md
    # entry 10 records that chapter 6 has not moved; this schema is new, so it starts on the
    # authority chapters 2, 5 and 10 fixed.
    assert schema["$id"].startswith("https://spec.medicalos.org/schemas/v1/")

    def _uris(node: Any) -> Iterator[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("$id", "$ref") and isinstance(value, str):
                    yield value
                else:
                    yield from _uris(value)
        elif isinstance(node, list):
            for item in node:
                yield from _uris(item)

    # `$id` and `$ref` VALUES only. The prose in a `description` may name the retired
    # authority -- this schema's does, to say why it is not used -- and a substring search
    # over the whole document would make documenting the rule a violation of it.
    assert [u for u in _uris(schema) if "schemas.medicalos.org" in u] == []


def test_every_code_the_capability_emits_resolves_in_the_dictionary(
    tmp_path: Path, registration: dict[str, Any]
) -> None:
    """MOS-IMG-112 / MOS-REG-042 / MOS-REG-044, asserted on the composed dictionary.

    Three things at once: every concept key the row names has a row (a missing one RAISES
    rather than being invented); every MEASUREMENT concept carries a non-empty UCUM unit;
    and the unit the row declares is the unit the dictionary carries -- two places that
    could disagree about millilitres, checked against each other.
    """
    concepts = svc_registry.worker_concepts(tmp_path / "registry")

    # `segment_profile` names a PROFILE slug and the other two name CONCEPT keys; the two
    # namespaces are separate in `capability_concepts.json` and conflating them is how a
    # profile slug ends up being looked up as a code.
    for member in ("finding_type", "segmented_property_type"):
        assert concepts[registration["concepts"][member]]["code_value"], member
    assert registration["concepts"]["segment_profile"] in concepts.segment_profiles

    for measurement in registration["measurements"]:
        row = concepts[measurement["concept_key"]]
        assert row["ucum_unit"], f"{measurement['concept_key']} has no UCUM unit"
        assert row["ucum_unit"] == measurement["ucum_unit"], measurement["measurement_id"]
        assert row.get("computation_geometry") == "source"

    # The segment profile resolves end to end: slug -> category/type/region/finding.
    profile = concepts.profile(registration["concepts"]["segment_profile"])
    for key in ("category", "type", "anatomic_region", "finding_type"):
        assert concepts[profile[key]]["code_value"], key
    assert len(profile["label"]) <= 64  # MOS-IMG: SegmentLabel is 64 characters


def test_a_private_code_carries_its_rationale(tmp_path: Path) -> None:
    """MOS-IMG-114: a `99MEDOS` code is a claim that no standard term exists, and the claim
    has to be written down and reviewed. A private code with no rationale is an invented one
    with extra steps.
    """
    composed = overlay.compose()
    private = {
        k: v for k, v in composed["concepts"].items() if v.get("coding_scheme") == "99MEDOS"
    }
    assert private, "the overlay contributes no private codes; this test is checking nothing"
    for key, row in private.items():
        assert row.get("private_code_rationale"), f"{key} has no private_code_rationale"


def test_the_overlay_refuses_to_redefine_a_code_the_base_owns(tmp_path: Path) -> None:
    """MOS-REG-042 forbids a second definition of a coded concept.

    The merge is STRICT, so a capability that tried to silently redefine `anatomy.lung` --
    making two capabilities draw different things under one code, indistinguishable in the
    SEG afterwards -- fails at composition rather than at read time.
    """
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps(
            {
                "concepts": {
                    "morphology.nodule": {
                        "coding_scheme": "SCT",
                        "code_value": "99999999",
                        "code_meaning": "Something else entirely",
                    }
                },
                "segment_profiles": {},
                "rtstruct_roi_map": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="redefines concepts.'morphology.nodule'"):
        overlay.compose(base=base)


def test_the_registry_row_is_the_envelope(tmp_path: Path) -> None:
    """The applicability envelope is EVALUATED from the row, not restated in Python.

    Proven by changing the row and observing the capability's answer change. If the envelope
    were a Python copy, this test would pass the edited row and reject nothing -- which is
    exactly the drift `MOS-REG-016` forbids ("a hand-maintained duplicate of any manifest
    shape is forbidden").
    """
    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    files = _case_files(CASE_WITH_CANDIDATES)
    vol, _source, _diag = build_canonical_volume(files)

    assert LungNodule(concepts=concepts).applicable(vol) is None

    row = json.loads((EXAMPLES / "capability.json").read_text(encoding="utf-8"))
    for constraint in row["applicability"]["constraints"]:
        if constraint["attribute"] == "delta_s_mm":
            constraint["max"] = 0.5  # the study is 2.5 mm; this must now refuse it
    tightened = LungNodule(concepts=concepts, registration=row)

    verdict = tightened.applicability_report(vol)
    assert verdict is not None
    reason, detail = verdict
    assert reason == "outside_applicability_envelope"
    assert detail["observed"]["delta_s_mm"] == pytest.approx(2.5, abs=0.01)
    assert detail["rationale"]  # MOS-SVC-023: the refusal says why, in the row's words


def test_an_envelope_attribute_the_build_cannot_observe_raises(tmp_path: Path) -> None:
    """A constraint nothing evaluates is a claim of safety nobody checks.

    The failure this guards is silent: add `contrast_phase` to the row, no code observes it,
    and the envelope keeps passing every study while the registry advertises a limit.
    """
    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    files = _case_files(CASE_WITH_CANDIDATES)
    vol, _source, _diag = build_canonical_volume(files)

    row = json.loads((EXAMPLES / "capability.json").read_text(encoding="utf-8"))
    row["applicability"]["constraints"].append(
        {
            "attribute": "contrast_phase",
            "kind": "enum",
            "values": ["non_contrast"],
            "reason_code": "input_constraint_unmet",
            "rationale": "an attribute this build does not observe",
        }
    )
    with pytest.raises(KeyError, match="contrast_phase"):
        LungNodule(concepts=concepts, registration=row).applicable(vol)


# =====================================================================================
# 2. The ownership boundary (chapter 2 section 2.7)
# =====================================================================================
#: `MOS-SVC-038`'s list, verbatim. A native service's import closure may contain none of
#: these at module scope. `psycopg` and `kafka` are the database and the bus; the rest are
#: the network, the object store and process escape hatches.
FORBIDDEN_IMPORTS = frozenset(
    {
        "socket", "http", "urllib", "requests", "httpx", "aiohttp", "psycopg", "psycopg2",
        "sqlalchemy", "asyncpg", "boto3", "botocore", "minio", "kafka", "confluent_kafka",
        "redis", "subprocess", "ctypes", "multiprocessing.connection", "tritonclient",
    }
)

#: Planes the Service Plane may not reach into. `medos.core` and `medos.capabilities` are
#: the native ABI a service is MEANT to import (section 2.5.1: "the vendor imports the
#: platform's ABI wheel"); everything else is another plane's business.
FORBIDDEN_MEDOS = ("medos.db", "medos.api", "medos.bus", "medos.dicomweb", "medos.gateway",
                   "medos.objectstore", "medos.writer", "medos.worker", "medos.evidence",
                   "medos.security", "medos.sealed", "medos.promotion", "medos.training")


def _module_scope_imports(path: Path) -> set[str]:
    """Every module imported at MODULE SCOPE. Function-local imports are not scanned.

    Matching `MOS-SVC-038`, which says "imports, at module scope". An import inside a
    function is a runtime decision the static scan is explicitly not the control for --
    `MOS-SVC-039`'s network namespace is.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        """Descend through everything EXCEPT function bodies."""
        for node in body:
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # a function-local import is not module scope
            elif isinstance(node, (ast.ClassDef, ast.If, ast.Try, ast.With)):
                # A class body, a `if TYPE_CHECKING:` guard and a `try: import x` fallback
                # all execute at import time, so their imports ARE module scope.
                visit(node.body)
                visit(getattr(node, "orelse", []))
                visit(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)

    visit(tree.body)
    return names


def test_service_plane_imports_nothing_it_must_not() -> None:
    """MOS-SVC-038's static import scan, run against this package.

    Chapter 2's ownership table is otherwise a docstring claim. This makes it a check: the
    Service Plane "MUST NOT know about PostgreSQL, the event bus, the object store, PACS
    credentials, other tenants, other services, or its own Deployment state".

    `medos.writer` is on the forbidden list too, which is `MOS-SVC-060` -- "a service MUST
    NOT construct, mint or STOW a DICOM object" -- as an import rule.
    """
    modules = sorted(SERVICE_PKG.glob("*.py"))
    assert len(modules) >= 4, f"expected the whole package, found {modules}"

    for module in modules:
        imported = _module_scope_imports(module)
        for name in imported:
            root = name.split(".")[0]
            assert root not in FORBIDDEN_IMPORTS, (
                f"{module.name} imports {name!r} at module scope; MOS-SVC-038 forbids it"
            )
            assert not any(
                name == f or name.startswith(f + ".") for f in FORBIDDEN_MEDOS
            ), f"{module.name} imports {name!r}; chapter 2 section 2.7 puts it in another plane"

    # MOS-SVC-060 at its strongest: this package cannot construct a DICOM object because it
    # cannot NAME one. No `pydicom`, no `highdicom`, anywhere in the import closure.
    #
    # Worth asserting separately from `medos.writer` above: a service could bypass the
    # platform writer entirely and build a Dataset by hand, and "a service MUST NOT
    # construct, mint or STOW a DICOM object" would be just as violated. A native service
    # receives a `CanonicalVolume` -- already decoded, already on a grid -- so it has no
    # legitimate reason to hold a DICOM library at all.
    every_import = {n for m in modules for n in _module_scope_imports(m)}
    dicom_libraries = {n for n in every_import if n.split(".")[0] in {"pydicom", "highdicom"}}
    assert not dicom_libraries, (
        f"the Service Plane imports {sorted(dicom_libraries)}; a native service is handed a "
        "decoded CanonicalVolume and MUST NOT construct a DICOM object (MOS-SVC-060)"
    )

    # And the platform ABI it DOES use is only the two packages section 2.5.1 sanctions
    # ("the vendor imports the platform's ABI wheel"), so the surface it depends on is
    # small enough to state in one line.
    medos_imports = {n for n in every_import if n.split(".")[0] == "medos"}
    assert all(
        n.startswith("medos.core") or n.startswith("medos.capabilities")
        for n in medos_imports
    ), f"unexpected platform imports: {sorted(medos_imports)}"


def test_the_capability_writes_no_dicom_and_returns_source_grid_geometry(
    tmp_path: Path,
) -> None:
    """MOS-SVC-056 / MOS-SVC-060 / MOS-IMG-039, on a real study.

    The label map comes back on the SOURCE grid -- not a resampled one and not model space
    -- and every measurement declares source geometry. The capability returns arrays and
    numbers; it mints no UID and builds no dataset.
    """
    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    files = _case_files(CASE_WITH_CANDIDATES)
    vol, source, _diag = build_canonical_volume(files)
    from medos.capabilities.base import CapabilityContext

    ctx = CapabilityContext(
        job_id="job_TEST",
        series_instance_uid=vol.series_instance_uid,
        source=source,
        clinical_use_mode="research_only",
    )
    outcome = LungNodule(concepts=concepts).run(vol, ctx)

    assert outcome.label_map is not None
    assert outcome.label_map.array.shape == source.hu_array.shape  # SOURCE grid
    assert outcome.label_map.array.dtype.name == "uint8"
    # MOS-IMG-121: the instances actually consumed, echoed verbatim.
    assert outcome.source_sop_instance_uids == tuple(source.sop_instance_uids)
    # MOS-SVC-020/021: no score anywhere. A threshold is not a classifier.
    assert all(f.score is None for f in outcome.findings)
    assert all(m.unit for f in outcome.findings for m in f.measurements)


def test_the_detector_is_deterministic_and_does_not_mutate_its_input() -> None:
    """Two properties a `deterministic_algorithm` has to actually have.

    DETERMINISM, because `method_class` claims it and because the whole provenance story
    depends on it: `MOS-REG-047` puts the parameters in the registry row so a stored number
    can be re-derived, which is meaningless if the same array gives two answers.

    IMMUTABILITY, because `MOS-SVC-042` says "`volumes[role].array` MUST be treated as
    read-only. A service that mutates it MUST be considered non-conformant". The worker
    hands the same `hu_array` to every capability in the job, so a service that wrote into
    it would corrupt the next one's input -- silently, and only when two capabilities run
    together.

    Synthetic, not a real study: the properties are about the code, and a 36x72x72 phantom
    makes them checkable in a second. The phantom is built the way a chest actually is --
    room air OUTSIDE a soft-tissue body wall, lungs INSIDE it -- because the detector's
    step 2 discards any air component reaching an in-plane face. A bare air block floating
    in a volume, which is the obvious fixture to reach for, yields a lung field of zero and
    a test that asserts nothing.
    """
    import numpy as np

    rng = np.random.default_rng(20260916)
    hu = np.full((36, 72, 72), -1000.0, dtype=np.float32)  # room air
    hu[3:33, 6:66, 6:66] = 40.0  # body wall: soft tissue, reaching no face
    hu[5:31, 12:60, 12:34] = -850.0  # right lung
    hu[5:31, 12:60, 38:60] = -850.0  # left lung
    hu[14:20, 30:36, 20:26] = 30.0  # compact blob: a candidate
    hu[10:16, 20:26, 44:50] = 60.0  # compact blob: a candidate
    hu[8:28, 40:43, 46:49] = 40.0  # a long tube: a vessel, and it must be REJECTED
    hu += rng.normal(0.0, 2.0, hu.shape).astype(np.float32)
    before = hu.copy()

    spacing = (2.0, 1.0, 1.0)
    first = detector.detect(hu, spacing)
    second = detector.detect(hu, spacing)

    # Not vacuous: the phantom really is detected, and the vessel really is filtered out.
    # Without this the determinism assertions below would pass on two empty results.
    assert first.lung_field_ml > 50.0
    assert first.n_enclosed_components == 3
    assert len(first.candidates) == 2, first.rejected
    assert first.rejected["aspect_ratio"] == 1, (
        "the tube was not rejected by the aspect-ratio filter; it is the only thing "
        "separating a vessel from a nodule in this method (LN-FM-001)"
    )

    assert np.array_equal(hu, before), (
        "detect() wrote into the HU array it was given (MOS-SVC-042)"
    )
    assert len(first.candidates) == len(second.candidates)
    assert [c.volume_ml for c in first.candidates] == [c.volume_ml for c in second.candidates]
    assert [c.sphericity for c in first.candidates] == [
        c.sphericity for c in second.candidates
    ]
    assert first.rejected == second.rejected
    assert np.array_equal(first.mask, second.mask)

    # The candidates come back largest first, which is what makes the SR's group ordering
    # and `MAX_REPORTED_CANDIDATES` meaningful rather than arbitrary.
    volumes = [c.volume_ml for c in first.candidates]
    assert volumes == sorted(volumes, reverse=True)
    assert [c.index for c in first.candidates] == list(range(len(first.candidates)))


def test_an_implausible_candidate_count_rejects_instead_of_reporting(
    tmp_path: Path,
) -> None:
    """MOS-SAFE-014's `output_plausibility_gate`, exercised rather than declared.

    `LN-FM-003` names this gate as its mitigation: the candidate count is a function of the
    threshold, not of the patient, and a three-figure count means the threshold is wrong for
    this series. The declared response is to REFUSE, because reporting the number would be
    reporting the bug as a measurement -- the same rule that made `pleural_effusion` return
    `present=False` rather than a plausible figure.

    A mitigation nothing exercises is a sentence in a manifest, so this builds a phantom
    that really trips it: 128 compact blobs in one lung field, against a ceiling of 60.

    The refusal is a `CapabilityRejection` with `service_declined`, which chapter 5's T7
    turns into a job-level REJECTED -- NOT a FAILED. Nothing malfunctioned; the service
    declined to answer, and every UI has to render those two differently (MOS-EXEC-001).
    """
    import numpy as np
    from medos.capabilities.base import CapabilityContext, CapabilityRejection
    from medos.core.geometry import SourceGeometry

    concepts = svc_registry.worker_concepts(tmp_path / "registry")

    hu = np.full((44, 110, 110), -1000.0, dtype=np.float32)
    hu[3:41, 6:104, 6:104] = 40.0  # body wall
    hu[5:39, 12:98, 12:98] = -850.0  # lung field
    placed = 0
    for k in range(8, 36, 8):
        for r in range(18, 94, 10):
            for c in range(18, 94, 22):
                hu[k : k + 3, r : r + 5, c : c + 5] = 40.0
                placed += 1
    hu += np.random.default_rng(7).normal(0.0, 1.5, hu.shape).astype(np.float32)
    assert placed > detector.MAX_PLAUSIBLE_CANDIDATES, (
        "the phantom does not exceed the ceiling; this test would prove nothing"
    )

    result = detector.detect(hu, (2.0, 1.0, 1.0))
    assert result.implausible
    assert len(result.candidates) == placed

    # And the capability turns that into a refusal rather than an outcome.
    source = SourceGeometry(
        paths=(),
        sop_instance_uids=tuple(f"2.25.{i}" for i in range(hu.shape[0])),
        pixel_spacing_mm=(1.0, 1.0),
        delta_s_mm=2.0,
        slice_thickness_mm=2.0,
        rows=hu.shape[1],
        columns=hu.shape[2],
        image_position_patient=tuple(
            (0.0, 0.0, 2.0 * i) for i in range(hu.shape[0])
        ),
        image_orientation_patient=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0),
        convolution_kernel=None,
        hu_array=hu,
        first_dataset_path=None,
        dropped_duplicate_sop_instance_uids=(),
        study_instance_uid="2.25.800",
        series_instance_uid="2.25.801",
        frame_of_reference_uid="2.25.802",
        max_jitter_mm=0.0,
    )

    class _Vol:
        """The `CanonicalVolume` members the envelope and the guard actually read.

        A stand-in rather than a real `build_canonical_volume` result: constructing 44
        synthetic DICOM instances on disk to reach one `raise` would test pydicom, not this.
        """

        modality = "CT"
        value_units = "HU"
        shape = hu.shape
        spacing_mm = (2.0, 1.0, 1.0)
        tilt_deg = 0.0
        resampled_from_source = False
        series_instance_uid = "2.25.801"

    ctx = CapabilityContext(
        job_id="job_T",
        series_instance_uid="2.25.801",
        source=source,
        clinical_use_mode="research_only",
    )
    with pytest.raises(CapabilityRejection) as raised:
        LungNodule(concepts=concepts).run(_Vol(), ctx)

    # `service_declined`, and it is in chapter 5's closed job-level vocabulary -- a
    # capability cannot invent a rejection code no consumer switches on.
    assert raised.value.reason_code == "service_declined"
    assert raised.value.reason_code in {
        "outside_applicability_envelope",
        "unsupported_geometry",
        "input_constraint_unmet",
        "service_declined",
    }
    assert raised.value.detail["observed"]["n_candidates"] == placed
    assert raised.value.detail["rationale"]


def test_the_platform_registry_is_untouched(tmp_path: Path) -> None:
    """`medos.capabilities.REGISTRY` still has exactly its three keys.

    This is the heart of `zero-core-change` expressed as a runtime property rather than as a
    diff. `tests/unit/test_capabilities.py` asserts the same three keys; if this capability
    had registered itself by mutating the global -- the obvious shortcut -- that test would
    break, and so would every other consumer in the process, including
    `medos.training.runs`.
    """
    from medos.capabilities import REGISTRY

    before = dict(REGISTRY)
    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    injected = svc_registry.worker_registry(concepts)

    assert set(REGISTRY) == {"lung_segmentation", "emphysema_laa", "pleural_effusion"}
    assert REGISTRY == before
    assert CAPABILITY_ID not in REGISTRY
    assert CAPABILITY_ID in injected
    assert injected[CAPABILITY_ID].version == VERSION


# =====================================================================================
# 3. Clinical honesty (chapter 9)
# =====================================================================================
def test_nothing_about_this_capability_claims_to_be_a_model(
    tmp_path: Path, registration: dict[str, Any]
) -> None:
    """The `pleural_effusion` rule, applied to a capability that DOES compute something.

    A threshold rendered as "AI" is a claim about provenance and validation that is untrue,
    and a clinician reading "AI detection" applies a different prior than one reading "HU
    threshold". `method_class` is the field that stops it, and it has to agree everywhere:
    the registry row, the metadata block, and the `method_detail` that reaches storage.
    """
    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    capability = LungNodule(concepts=concepts)
    metadata = capability.metadata

    assert registration["method_class"] == "deterministic_algorithm"
    assert metadata.method_class == "deterministic_algorithm"
    assert metadata.operating_point is None  # MOS-SVC-020
    assert metadata.computation_geometry == "source"
    assert "no weights" in metadata.method_summary.lower() or "THERE IS NO MODEL" in (
        metadata.method_summary
    )

    # MOS-SAFE-015: both lists non-empty, and each failure mode declares how it is detected.
    assert metadata.not_validated_for
    assert metadata.known_failure_modes
    detections = {f.detection for f in metadata.known_failure_modes}
    assert detections <= {
        "applicability_envelope", "output_plausibility_gate", "human_review", "none"
    }
    # At least one mode is declared UNDETECTABLE. A capability whose every failure is caught
    # by a gate is a capability whose failure modes have not been thought about.
    assert "none" in detections

    # MOS-REG-047: every number the output depends on travels with it.
    assert set(detector.PARAMETERS) <= set(metadata.parameters)
    assert metadata.parameters["learned_model"] == "no"


def test_the_clinical_bar_is_not_in_the_service_manifest(
    registration: dict[str, Any],
) -> None:
    """MOS-REG-049: clinical acceptance belongs to the Capability, engineering to the
    ServiceVersion, and "the registry MUST reject a `service_version` that declares clinical
    acceptance thresholds in its own manifest".

    Checked by value and not by field name: every clinical threshold number is looked for in
    the manifest's text, so moving one into `engineering_acceptance` under a different key
    still fails.
    """
    manifest = json.loads(
        (EXAMPLES / "service-version.manifest.json").read_text(encoding="utf-8")
    )
    spec_text = json.dumps(manifest["spec"])

    criteria = registration["acceptance_criteria"]["criteria"]
    assert criteria, "no acceptance criteria declared; this test is checking nothing"
    for criterion in criteria:
        assert criterion["metric"] not in spec_text, criterion["id"]

    # And the engineering block carries only engineering bars.
    assert set(manifest["spec"]["engineering_acceptance"]) <= {
        "p95_wall_clock_seconds", "max_gpu_memory_mib", "result_bundle_schema_validity"
    }
    # MOS-SVC-020: deterministic means no operating point. See the manifest's note for the
    # spec contradiction that makes OMISSION rather than `[]` the representable form.
    assert "operating_points" not in manifest["spec"]["capabilities"][0]


def test_the_acceptance_criteria_is_declared_unmet_with_a_subgroup_floor(
    registration: dict[str, Any],
) -> None:
    """The bar exists, it is UNMET, and it has the stratum
    `medos/medos/evidence/gate.py` demands.

    That module is explicit that an aggregate "passes a model that lost every sub-6 mm
    nodule", so a nodule capability whose criteria carry no small-nodule stratum has written
    a bar it can clear while failing the cases that matter.
    """
    acceptance = registration["acceptance_criteria"]
    assert acceptance["status"] == "unmet"
    assert acceptance["evidence_reference"] is None

    strata = {c["stratum"] for c in acceptance["criteria"]}
    assert len(strata) > 1, f"no subgroup floor; every criterion is cohort-wide: {strata}"
    assert any("under" in s or "small" in s for s in strata), strata

    for criterion in acceptance["criteria"]:
        assert criterion["margin_rationale"], criterion["id"]
        # MOS-SVC-021: a sensitivity without a threshold must not be displayed. There is no
        # threshold here, so both members are explicitly null rather than absent.
        assert criterion["operating_point_id"] is None
        assert criterion["score_threshold"] is None


# =====================================================================================
# 4. End to end on a real LIDC-IDRI study
# =====================================================================================
@pytest.mark.slow
def test_end_to_end_on_a_real_lidc_study(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """A job the platform created, ran and completed, with a SEG and an SR out the far end.

    Everything on the path is the platform's: the queue, the lease, the eight reserved
    steps, series selection, `build_canonical_volume` reading slice order from
    `ImagePositionPatient`, the SEG and SR writers, and the provenance record. The ONLY
    thing supplied by this component is two `WorkerDeps` arguments.
    """
    outcome, job_id, gateway = _run_job(
        wconn, pg_dsn, tmp_path, CASE_WITH_CANDIDATES
    )

    assert outcome is not None, "nothing was claimed off the queue"
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: "
        f"{outcome.failure_code or outcome.reject_reason_code} "
        f"{getattr(outcome, 'reject_detail_code', '')}"
    )

    job = repo.get_job(wconn, job_id)
    assert job["state"] == "COMPLETED"
    assert job["steps_completed"] == job["steps_total"] == 8
    assert list(job["capability_ids"]) == [CAPABILITY_ID]

    # BOTH derived objects, stored, in two different series (MOS-IMG-073).
    results = repo.list_results(wconn, job_id)
    assert [r["capability_id"] for r in results] == [CAPABILITY_ID]
    objects = [o for r in results for o in r["dicom_objects"]]
    assert {o["object_kind"] for o in objects} == {"SEG", "SR"}
    assert all(o["stow_state"] == "stored" for o in objects)
    seg = next(o for o in objects if o["object_kind"] == "SEG")
    sr = next(o for o in objects if o["object_kind"] == "SR")
    assert seg["sop_class_uid"] == "1.2.840.10008.5.1.4.1.1.66.4"
    assert sr["sop_class_uid"] == "1.2.840.10008.5.1.4.1.1.88.34"
    assert seg["series_instance_uid"] != sr["series_instance_uid"]
    assert len(gateway.stored) == 2

    # The SEG as an OBJECT, not as a database row. A `result_dicom_objects` row says a
    # write happened; it does not say the segments carry this capability's codes, and
    # MOS-IMG-112's whole point is that a wrong code is invisible downstream.
    import pydicom

    # The worker writes each object as `{sop_instance_uid}.dcm`, so the stored path is
    # matched by the UID the database recorded -- not by a substring of the filename.
    seg_path = next(p for p in gateway.stored if p.stem == seg["sop_instance_uid"])
    seg_ds = pydicom.dcmread(str(seg_path))
    segments = seg_ds.SegmentSequence
    assert len(segments) == 2, "expected the search region and the candidate segment"
    coded_types = {
        (
            s.SegmentedPropertyTypeCodeSequence[0].CodingSchemeDesignator,
            s.SegmentedPropertyTypeCodeSequence[0].CodeValue,
        )
        for s in segments
    }
    # `anatomy.lung` for what was searched, `morphology.nodule` for what was found. Both
    # resolved out of the dictionary; neither invented here.
    assert coded_types == {("SCT", "39607008"), ("SCT", "27925004")}
    nodule_seg = next(
        s for s in segments
        if s.SegmentedPropertyTypeCodeSequence[0].CodeValue == "27925004"
    )
    # MOS-SVC-021 again, at the object level: a SEG segment carries no score, and the
    # algorithm is typed as what it is.
    assert nodule_seg.SegmentAlgorithmType in ("AUTOMATIC", "SEMIAUTOMATIC")
    assert (
        nodule_seg.SegmentedPropertyCategoryCodeSequence[0].CodeValue == "49755003"
    ), "the nodule segment is not categorised as a morphologically abnormal structure"

    # The measurements reached storage with their codes and UCUM units (MOS-STORE-282:
    # scheme, code and unit are NOT NULL, so "a bare number with a free-text label" cannot
    # be stored).
    uid = repo.get_job_uuid(wconn, job_id)
    rows = wconn.execute(
        "SELECT concept_scheme, concept_code, ucum_unit, value, geometry_space, "
        "       method_detail "
        "FROM result_measurements m JOIN results r ON r.id = m.result_id "
        "WHERE r.job_id = %s",
        (uid,),
    ).fetchall()
    assert rows, "no measurements were persisted"
    assert all(r["ucum_unit"] for r in rows)
    assert all(r["geometry_space"] == "source" for r in rows)  # MOS-IMG-039
    assert {r["concept_scheme"] for r in rows} <= {"SCT", "99MEDOS"}

    # MOS-SAFE-012 / MOS-REG-047: what produced the number travels with the number.
    detail = rows[0]["method_detail"]
    assert detail["capability_id"] == CAPABILITY_ID
    assert detail["learned_model"] is False
    assert detail["score_available"] is False
    assert detail["parameters"]["candidate_hu_min"] == detector.CANDIDATE_HU_MIN

    # The candidate count is a real measurement and it is non-negative and integral.
    counts = [r for r in rows if r["concept_code"] == "NODULE_CANDIDATE_COUNT"]
    assert len(counts) == 1
    assert float(counts[0]["value"]).is_integer()
    assert float(counts[0]["value"]) >= 0

    # THE SR CARRIES THE SAME NUMBERS. docs/spec/15-delivery.md section 15.2.3's exit check
    # is "the measurement reported in the SR equals the measurement" the platform recorded,
    # and it is only checkable from the SR side. A `result_measurements` row proves the
    # database agrees with the bundle; it says nothing about what a radiologist opening the
    # report will read, and the SR is the object that outlives this system.
    sr_path = next(p for p in gateway.stored if p.stem == sr["sop_instance_uid"])
    sr_ds = pydicom.dcmread(str(sr_path))

    def code_of(item: Any) -> str:
        """`CodeValue`, or the long/URN form DICOM requires past 16 characters.

        PS3.3 types `CodeValue` as SH (16 characters). A longer value MUST instead be
        carried in `LongCodeValue` (UC) with `CodeValue` absent, and both of this
        capability's private measurement codes -- `NODULE_CANDIDATE_COUNT` (22) and
        `NODULE_EQUIV_DIAMETER` (21) -- are past the limit, so highdicom writes them there.

        Reading only `CodeValue` is therefore how a consumer silently loses exactly the
        measurements that are NOT standard-coded, which is the opposite of what a
        99MEDOS code is for. The platform's own `param.operating_threshold`
        (`OPERATING_THRESHOLD`, 19 characters) has the same property.
        """
        code = item.ConceptNameCodeSequence[0]
        for attribute in ("CodeValue", "LongCodeValue", "URNCodeValue"):
            if attribute in code:
                return str(getattr(code, attribute))
        raise AssertionError(f"content item has no code value: {code}")

    found: list[tuple[str, float, str]] = []

    def collect(items: Any) -> None:
        for item in items:
            if getattr(item, "ValueType", "") == "NUM":
                measured = item.MeasuredValueSequence[0]
                found.append(
                    (
                        code_of(item),
                        float(measured.NumericValue),
                        str(measured.MeasurementUnitsCodeSequence[0].CodeValue),
                    )
                )
            if "ContentSequence" in item:
                collect(item.ContentSequence)

    collect(sr_ds.ContentSequence)

    # Every number this capability produced appears in the SR, by code, value and unit.
    # `approx` because the SR stores a DS (decimal string) and the database a float8.
    for row in rows:
        assert any(
            code == row["concept_code"]
            and unit == row["ucum_unit"]
            and value == pytest.approx(float(row["value"]), rel=1e-6, abs=1e-9)
            for code, value, unit in found
        ), (
            f"{row['concept_scheme']}:{row['concept_code']} = {row['value']} "
            f"{row['ucum_unit']} is in result_measurements but not in the SR"
        )

    # And the private codes really travelled -- including the one whose CODE MEANING is
    # where the "not clinically validated" disclaimer lives for a viewer that renders the
    # SR without reading any of our metadata.
    assert any(code == "NODULE_CANDIDATE_COUNT" for code, _v, _u in found)
    assert any(code == "NODULE_EQUIV_DIAMETER" for code, _v, _u in found)


@pytest.mark.slow
def test_a_study_with_no_candidates_still_completes(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The modal outcome of a detector, and it must not be a FAILURE.

    `LIDC-IDRI-0019` yields zero candidates at these parameters. The job still completes,
    the SEG still carries the SEARCH REGION, the nodule segment is OMITTED and RECORDED
    (`MOS-IMG-098`), and the SR reports a count of zero.

    Without the search-region segment this job USED to die in `plan_outputs` with
    `all_segments_empty` -- a `SystemFailure`, i.e. the platform reporting a MALFUNCTION
    for a legitimate negative result. That is fixed: the writer is now total and a
    capability with nothing to outline gets a plan with no SEG and a recorded reason. See
    `test_a_detection_with_nothing_to_draw_is_a_result_not_a_failure`, which pins the fix
    on the same three inputs that used to pin the defect.

    The search region is kept regardless, and the assertions below are why: it is what
    makes the negative legible to a reader rather than merely non-fatal to the platform.
    """
    outcome, job_id, _gateway = _run_job(
        wconn, pg_dsn, tmp_path, CASE_WITHOUT_CANDIDATES
    )

    assert outcome is not None
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: {outcome.failure_code or outcome.reject_reason_code}"
    )

    uid = repo.get_job_uuid(wconn, job_id)
    rows = wconn.execute(
        "SELECT concept_code, value FROM result_measurements m "
        "JOIN results r ON r.id = m.result_id WHERE r.job_id = %s",
        (uid,),
    ).fetchall()
    count = next(r for r in rows if r["concept_code"] == "NODULE_CANDIDATE_COUNT")
    assert float(count["value"]) == 0.0

    # present=False on the nodule finding means "the filters admitted nothing", and the
    # capability's `not_validated_for` says so in those words. It is NOT a negative finding.
    results = repo.list_results(wconn, job_id)
    assert [r["capability_id"] for r in results] == [CAPABILITY_ID]

    # The search region was still measured and drawn, so a reader can see WHAT was searched
    # -- which is the only way `LN-FM-002` (juxtapleural lesions are unreachable by this
    # method) is visible to anybody looking at the result.
    #
    # There are TWO volume measurements under SCT:118565006 here, and the distinction is the
    # point: the search region, which must be a whole lung, and the nodule burden, which
    # must be exactly zero. Asserting only that "a volume exists" would pass on a run that
    # reported 0 ml of lung.
    volumes = sorted(float(r["value"]) for r in rows if r["concept_code"] == "118565006")
    assert len(volumes) == 2, f"expected a search-region volume and a burden, got {volumes}"
    assert volumes[0] == 0.0, "the nodule burden is not zero on a zero-candidate study"
    assert volumes[1] > 1000.0, (
        f"the search region is only {volumes[1]:.0f} ml; a delineated lung field on a "
        "chest CT is litres, so the SEG is not showing what was actually searched"
    )

    objects = [o for r in results for o in r["dicom_objects"]]
    assert {o["object_kind"] for o in objects} == {"SEG", "SR"}


def _label_map_outcome(capability_id: str) -> Any:
    """A minimal `CapabilityOutcome` that owns a label map. No concepts, no pixels of note.

    The write path's `model_version` question is answered from WHICH outcome carries the
    label map, never from what is in it, so a 2x4x4 array with one voxel set is the whole
    fixture.
    """
    import numpy as np
    from medos.core.bundle import CapabilityOutcome, CodedConcept, LabelMap

    array = np.zeros((2, 4, 4), dtype=np.uint8)
    array[0, 1, 1] = 1
    return CapabilityOutcome(
        capability_id=capability_id,
        findings=(),
        label_map=LabelMap(
            array=array,
            segments=(CodedConcept(scheme="SCT", code="27925004", meaning="Nodule"),),
        ),
        source_sop_instance_uids=(),
    )


# =====================================================================================
# 5. What the platform did NOT accommodate. Pinned as tests, not as prose.
# =====================================================================================
def test_a_deployment_shipping_only_this_capability_can_write_dicom(
    tmp_path: Path,
) -> None:
    """WAS a pinned platform defect. `step_write_dicom` resolved a capability BY NAME:

        model_version=str(ctx.deps.registry["lung_segmentation"].version)

    Any deployment whose registry did not happen to contain `lung_segmentation` -- which
    is every deployment shipping one vendor service (`MOS-REL-020`) -- raised `KeyError`
    at the write step. It was latent in the end-to-end tests above only because
    `worker_registry()` composes onto the platform's three, and that accident was doing
    real work.

    FIXED in `medos.worker.steps.producing_model_version`, which asks the question the
    field is really asking -- "which model produced the label map in THIS object" -- and
    answers it from the bundle. The registry of exactly one is now sufficient, which is
    what this asserts.

    Asserted against the real resolver rather than by grepping `steps.py` for the absent
    string: a source-substring tripwire stops being true the moment the string moves, and
    says nothing either way about behaviour.
    """
    from medos.core.bundle import ResultBundle
    from medos.worker.steps import producing_model_version

    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    only_this = svc_registry.worker_registry(concepts, base={})
    assert set(only_this) == {CAPABILITY_ID}

    bundle = ResultBundle(outcomes=(_label_map_outcome(CAPABILITY_ID),))
    assert producing_model_version(only_this, bundle) == VERSION


@pytest.mark.slow
def test_only_this_capability_deployed_completes_and_declares_its_own_version(
    wconn: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
) -> None:
    """The fix, DEMONSTRATED: a real job, a real registry of ONE, all eight steps green.

    `base={}` is the deployment `medos/examples/lung-nodule/worker_main.py` produces when it
    serves this capability alone -- the ordinary case for a vendor shipping one service.
    There is no `lung_segmentation` anywhere in this job: not in the request, not in the
    registry, not in the bundle. Before the fix, seven of the eight steps passed and
    `write_dicom` raised `KeyError` on a capability name it had no reason to need.

    The second half is what makes the fix a fix rather than a rescue. `model_version` is
    MOS-IMG-062's UID seed and MOS-STORE-286's `derivation_inputs`, so the objects must
    declare the version of the model that actually produced their pixels. This asserts the
    written value is `lung_nodule`'s own -- not `lung_segmentation`'s, which is what the
    composed three-capability runs above were silently stamping onto every `lung_nodule`
    SEG and SR -- and that it agrees with the `execution.models[]` entry `result_rows.py`
    writes for the same result (MOS-SAFE-083 section D).
    """
    from medos.capabilities import REGISTRY as PLATFORM_REGISTRY

    outcome, job_id, _gateway = _run_job(
        wconn, pg_dsn, tmp_path, CASE_WITH_CANDIDATES, base={}
    )

    assert outcome is not None
    assert outcome.terminal_state == "COMPLETED", (
        f"{outcome.terminal_state}: "
        f"{outcome.failure_code or outcome.reject_reason_code}"
    )

    uid = repo.get_job_uuid(wconn, job_id)
    steps = {
        s["step_key"]: s["status"]
        for s in wconn.execute(
            "SELECT step_key,status FROM job_steps WHERE job_id=%s ORDER BY step_index",
            (uid,),
        ).fetchall()
    }
    assert steps["write_dicom"] == "succeeded"
    assert steps["store_dicom"] == "succeeded"
    assert steps["persist_result"] == "succeeded"

    results = repo.list_results(wconn, job_id)
    assert [r["capability_id"] for r in results] == [CAPABILITY_ID]
    objects = [o for r in results for o in r["dicom_objects"]]
    assert {o["object_kind"] for o in objects} == {"SEG", "SR"}
    assert all(o["stow_state"] == "stored" for o in objects)

    # The point of the whole exercise: MOS-IMG-062's tuple, as recorded per object.
    other = str(PLATFORM_REGISTRY["lung_segmentation"].version)
    assert other != VERSION, "the fixture proves nothing if the two versions agree"
    for obj in objects:
        declared = obj["derivation_inputs"]["model_version"]
        assert declared == VERSION, (
            f"{obj['object_kind']} declares model_version={declared!r}; this job ran "
            f"{CAPABILITY_ID}@{VERSION} and nothing else"
        )
        assert declared != other

    # ... and the provenance record, which answered per-outcome all along. Before the fix
    # these two disagreed inside one job.
    record = results[0]["provenance"]["record"]
    assert record["execution"]["models"][0]["model_id"] == CAPABILITY_ID
    assert record["execution"]["models"][0]["version"] == VERSION


def test_two_label_maps_in_one_job_is_refused(tmp_path: Path) -> None:
    """PLATFORM CONSTRAINT, pinned. One SEG-producing capability per job.

    `plan_outputs` refuses a bundle with two label maps: "MOS-IMG-066 needs a total ordering
    across the combined segment set and CONTRACT.md section 5 does not define how two label
    maps compose; refusing to invent one." That refusal is correct and well-reasoned, and it
    also means `lung_nodule` can never share a job with `lung_segmentation`.

    Recorded because it is a real limit on what "add a second capability" can mean: the
    capabilities compose in the registry but NOT in a single job's outputs.

    Exercised against the real writer with a real two-outcome bundle rather than grepped
    out of the source: a constraint asserted by substring search stops being true the
    moment the string moves, and says nothing about behaviour either way.
    """
    import numpy as np
    from medos.core.bundle import CapabilityOutcome, CodedConcept, LabelMap, ResultBundle
    from medos.writer.identity import build_job_identity, plan_outputs

    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    array = np.zeros((4, 8, 8), dtype=np.uint8)
    array[1, 2:5, 2:5] = 1

    def outcome(capability_id: str, concept_key: str) -> CapabilityOutcome:
        row = concepts[concept_key]
        return CapabilityOutcome(
            capability_id=capability_id,
            findings=(),
            label_map=LabelMap(
                array=array,
                segments=(
                    CodedConcept(
                        scheme=str(row["coding_scheme"]),
                        code=str(row["code_value"]),
                        meaning=str(row["code_meaning"]),
                    ),
                ),
            ),
            source_sop_instance_uids=("2.25.1",),
        )

    identity = build_job_identity(
        job_id="job_01JTESTTESTTESTTESTTESTTE",
        tenant_id="00000000-0000-0000-0000-000000000001",
        service_id="medos.slice",
        service_version="0.1.0",
        study_instance_uid="2.25.900",
        capability_ids=(CAPABILITY_ID, "lung_segmentation"),
        requested_outputs=("SEG", "SR"),
        clinical_use_mode="RESEARCH_ONLY",
        model_version=VERSION,
    )
    bundle = ResultBundle(
        outcomes=(
            outcome(CAPABILITY_ID, "morphology.nodule"),
            outcome("lung_segmentation", "anatomy.lung"),
        )
    )
    with pytest.raises(SystemFailure) as raised:
        plan_outputs(identity, bundle, concepts)
    assert raised.value.reason_code == "multiple_label_maps_in_bundle"


@pytest.mark.slow
def test_a_detection_with_nothing_to_draw_is_a_result_not_a_failure(tmp_path: Path) -> None:
    """WAS a pinned platform defect. FIXED: "I ran and there is nothing to draw" is sayable.

    What this test asserted before, and what it asserts now, are the same three inputs:

      * a bundle whose only outcome carries NO label map -- what a detector that found
        nothing returns when the platform lets it;
      * a bundle whose label map has every segment empty -- what a detector returns when
        it draws only its findings;
      * `requested_outputs = ("SR",)`, a job that asked for a report and not a drawing.

    All three used to end in `SystemFailure` (`no_label_map_in_bundle`,
    `all_segments_empty`), so `medos/medos/worker/runner.py` reported them `FAILED` -- and
    `MOS-EXEC-014` defines `FAILED` as "this study should have been analysed and the
    platform could not do it". The modal outcome of a nodule detector is no nodules, so
    the platform was reporting a malfunction for the common case, which is the same
    category error as reporting an absent study as `FAILED` rather than `REJECTED`.

    Now all three produce a PLAN: an SR and no SEG, with the SEG's absence carried as an
    `OmittedOutput` with a machine-readable `reason_code` (`MOS-SVC-011`, "never as
    silence"). `medos/services/lung_nodule/service.py`'s L2 note describes the workaround this
    capability adopted -- always drawing its search region -- which remains a better
    clinical object and is no longer load-bearing.
    """
    from medos.capabilities.base import CapabilityContext
    from medos.core.bundle import CapabilityOutcome, ResultBundle
    from medos.writer.identity import build_job_identity, plan_outputs

    concepts = svc_registry.worker_concepts(tmp_path / "registry")
    files = _case_files(CASE_WITHOUT_CANDIDATES)
    vol, source, _diag = build_canonical_volume(files)
    ctx = CapabilityContext(
        job_id="job_TEST",
        series_instance_uid=vol.series_instance_uid,
        source=source,
        clinical_use_mode="research_only",
    )
    real = LungNodule(concepts=concepts).run(vol, ctx)

    nothing_to_draw = CapabilityOutcome(
        capability_id=real.capability_id,
        findings=real.findings,
        label_map=None,
        source_sop_instance_uids=real.source_sop_instance_uids,
    )
    identity = build_job_identity(
        job_id="job_01JTESTTESTTESTTESTTESTTE",
        tenant_id="00000000-0000-0000-0000-000000000001",
        service_id="medos.slice",
        service_version="0.1.0",
        study_instance_uid=vol.study_instance_uid,
        capability_ids=(CAPABILITY_ID,),
        requested_outputs=("SR",),  # SEG NOT requested
        clinical_use_mode="RESEARCH_ONLY",
        model_version=VERSION,
    )
    plan = plan_outputs(identity, ResultBundle(outcomes=(nothing_to_draw,)), concepts)
    assert plan.written_kinds == ("SR",)
    assert plan.segments == ()
    assert not plan.has_findings_to_draw
    # The absence is NAMED, and both reasons are present: the job did not ask for a SEG,
    # and there would have been nothing to put in one.
    omitted = {o.kind: o.reason_code for o in plan.omitted_outputs}
    assert omitted == {"SEG": "not_requested"}, omitted

    # Same bundle, SEG requested: still a plan, and now the reason is the honest one.
    seg_wanted = plan_outputs(
        _replace_identity(identity, requested_outputs=("SEG", "SR")),
        ResultBundle(outcomes=(nothing_to_draw,)),
        concepts,
    )
    assert seg_wanted.written_kinds == ("SR",)
    assert [(o.kind, o.reason_code) for o in seg_wanted.omitted_outputs] == [
        ("SEG", "no_label_map")
    ]

    # The other half: a label map whose every segment is empty. MOS-IMG-098 omits each
    # empty segment and RECORDS it; omitting all of them leaves no SEG to write, and the
    # record of which segments were empty survives on the plan.
    from dataclasses import replace as _replace

    import numpy as np

    assert real.label_map is not None
    empty_map = _replace(real.label_map, array=np.zeros_like(real.label_map.array))
    drew_nothing = plan_outputs(
        _replace_identity(identity, requested_outputs=("SEG", "SR")),
        ResultBundle(outcomes=(_replace(real, label_map=empty_map),)),
        concepts,
    )
    assert drew_nothing.written_kinds == ("SR",)
    assert [(o.kind, o.reason_code) for o in drew_nothing.omitted_outputs] == [
        ("SEG", "all_segments_empty")
    ]
    assert set(drew_nothing.empty_segments) == {"lung", "lung_nodule"}, (
        f"MOS-IMG-098 requires every empty segment to be recorded, got "
        f"{drew_nothing.empty_segments}"
    )

    # And the manifest MOS-IMG-079 persists before the first write promises only what
    # will be written -- a manifest naming an absent SEG would make MOS-IMG-080's
    # reconciliation permanently unsatisfiable on every retry.
    manifest = drew_nothing.manifest(frame_count=len(source.sop_instance_uids))
    assert [o["kind"] for o in manifest["objects"]] == ["sr"]
    assert manifest["omitted"][0]["reason_code"] == "all_segments_empty"


def _replace_identity(identity: Any, **changes: Any) -> Any:
    """`dataclasses.replace` on a frozen `JobIdentity`, without the import at module top."""
    from dataclasses import replace

    return replace(identity, **changes)


def test_the_service_version_cannot_be_published_without_fabricating_evidence() -> None:
    """PLATFORM / SPEC DEFECT, pinned. Registration demands evidence that cannot exist yet.

    The manifest's `spec` block validates against the platform's OWN schema -- asserted
    below, so the shape is right and the blocker is not a malformed document.

    What blocks publication is `MOS-REG-025` (native mode: `models[]` non-empty, every ref
    resolving to a `model_version` with `lifecycle_status` in VALIDATED/APPROVED) combined
    with the `model_version` schema making `evaluation_run_id` REQUIRED. This capability has
    no EvaluationRun; its AcceptanceCriteria is `unmet`. Publishing it means minting an
    `er_...` id for a run that never happened, in the one field the deployment gate reads.

    So the artifact row is NOT written. `MOS-REG-020`'s own lifecycle has DRAFT and
    REGISTERED states BEFORE VALIDATED, which a mandatory `evaluation_run_id` makes
    unreachable -- a general defect, not a property of this capability.
    """
    manifest = json.loads(
        (EXAMPLES / "service-version.manifest.json").read_text(encoding="utf-8")
    )
    assert iter_errors(manifest["spec"], schema_for_kind("service_version"), registry={}) == []
    assert manifest["lifecycle_status"] == "DRAFT"

    model_schema = schema_for_kind("model_version")
    assert "evaluation_run_id" in model_schema["required"]
    # And there is no value of `weights_availability` that describes a parameter set.
    assert set(model_schema["properties"]["weights_availability"]["enum"]) == {
        "platform_managed", "vendor_sealed"
    }


# =====================================================================================
# 6. The measurement MOS-REL-020 actually asks for
# =====================================================================================
#: The allow-list `MOS-REL-020` states: "the diff that introduces the second capability
#: touches only `medos/services/`, `medos/schemas/`, `medos/examples/` and registry
#: rows". `tests/` is here because a capability shipped without the test that proves
#: it ran is not shipped -- `MOS-REL-012`: "an unexecuted acceptance criterion means
#: the requirement is not satisfied, whatever the code does". It is called out rather
#: than smuggled in: the spec sentence does not name it, and this component's report
#: says so.
#: THE PATHS AS THEY WERE IN THAT RANGE, and they are not rewritten when the tree
#: moves. `BASELINE..ENDPOINT` is a CLOSED historical range; `git diff` over it emits
#: `services/lung_nodule/detector.py`, because that is where the file was when the
#: second capability landed. A pass that rewrote this list to `medos/services/` --
#: today's spelling -- made every one of those eleven paths read as a violation of
#: `MOS-REL-020`'s allow-list, which is the opposite of what the diff shows.
#:
#: This list is the SPECIFICATION's, not the tree's. The docstring below says so in
#: terms: it does not get edited to fit the diff. It gets edited when the range moves.
ALLOWED_PREFIXES = ("services/", "schemas/", "examples/", "tests/")

BASELINE = "04e9400"
#: Pinned endpoint -- see tests/gate/test_zero_core_change.py for the full reasoning.
ENDPOINT = "7c1b408"


def test_zero_core_change_the_diff_touches_only_the_allowed_paths() -> None:
    """THE measurement. `git diff --name-only 04e9400`, classified.

    Run against the real repository, not a fixture. If a core file had to change, this test
    names it -- which is the whole point of `MOS-REL-020`: "if the second capability requires
    a core edit, that is learned in week 13 rather than in year two".

    This test does NOT edit the allow-list to fit the diff. `ALLOWED_PREFIXES` is the spec
    sentence plus `tests/`, declared above with its justification.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{BASELINE}..{ENDPOINT}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        skip_infra(f"git is not runnable here: {exc}", dependency="git")
        return
    if proc.returncode != 0:
        skip_infra(
            f"`git diff {BASELINE}` failed: {proc.stderr.strip()[:200]}", dependency="git"
        )
        return

    changed = [line for line in proc.stdout.splitlines() if line.strip()]
    assert changed, (
        f"the diff against {BASELINE} is EMPTY. Untracked files do not appear in "
        "`git diff`; `git add -N` them, or this check passes while proving nothing."
    )

    # ONE implementation of "is this path allowed", not two. This file and
    # `tests/gate/test_zero_core_change.py` carry the SAME measurement, and while they each
    # owned a copy of the rule they disagreed: the gate partitions into
    # allowed / gate-own / violation, while this file's four-prefix list had never heard of
    # the gate-own bucket, so it reported `pyproject.toml` and the §15.1.2 cell itself as
    # core changes. Two copies of one rule drifting apart is the defect `derive_uid` and
    # `SourceGeometry` each cost this project once already. The gate owns the rule; this
    # file asks it.
    from tests.gate.test_zero_core_change import _bucket

    offenders = [p for p in changed if _bucket(p) == "violation"]
    assert not offenders, (
        "zero-core-change: the following paths are outside the allow-list "
        f"{list(ALLOWED_PREFIXES)}:\n  " + "\n  ".join(offenders)
    )

    # `tests/` was added to the allow-list above, so it must not become the route by which
    # a core-adjacent edit slips through. The ONLY test file this capability may add or
    # change is its own: an edit to `tests/unit/test_capabilities.py` or to anything under
    # `tests/gate/` would mean the capability changed the platform's existing checks to
    # accommodate itself, which is the failure `MOS-REL-020` is written against.
    # Gate-own paths are excluded here for the same reason `_bucket` excludes them: the
    # release-0.3.0 gate's own modules landed in this range, and a gate cannot be evidence
    # about its own introduction. Without this the assertion below reports `tests/gate/**`
    # as the capability editing the platform's checks, which is the opposite of what
    # happened -- those files ARE the checks, added alongside.
    test_paths = [
        p for p in changed
        if p.startswith("tests/") and _bucket(p) != "gate-own"
    ]
    assert test_paths == ["tests/integration/test_lung_nodule.py"], (
        "zero-core-change: `tests/` may carry only this capability's own test; "
        f"the diff also touches {[p for p in test_paths if 'lung_nodule' not in p]}"
    )

    # And the capability really is present in the diff -- an empty `services/` would pass
    # the check above while shipping nothing.
    #
    # AS-OF-THE-RANGE PATHS, like everything else in this test. These three read
    # `medos/services/`, `medos/examples/` and `medos/schemas/` for a while, which is where
    # those directories are TODAY and not where they were when this diff was taken. All
    # three then matched nothing, and the positive control that exists to stop the check
    # passing over an empty change set became the thing reporting the empty change set.
    assert any(p.startswith("services/lung_nodule/") for p in changed)
    assert any(p.startswith("examples/lung-nodule/") for p in changed)
    assert any(p.startswith("schemas/lung-nodule/") for p in changed)
