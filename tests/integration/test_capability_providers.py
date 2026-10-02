# SPDX-License-Identifier: Apache-2.0
"""The deployment-time capability seam: one resolver, read by the API and by the worker.

THE DEFECT THESE TESTS PIN
----------------------------
0.3.0 added `lung_nodule` under `medos/services/` without touching a core file. It was also
undeployable: nothing in `deploy/` or `medos/medos/` ever called
`services.lung_nodule.registry.worker_registry`, `known_capability_ids()` read the module
singleton `medos.capabilities.REGISTRY` and refused a `lung_nodule` job at submit, and the
worker only ever saw the capability through a `WorkerDeps` argument that only tests passed.
The capability ran end to end in the suite and was unreachable in production.

Fixing `known_capability_ids()` alone would have MOVED the defect: the API would accept a
job the worker cannot run, the submit would return `202`, and the study would FAIL with an
internal error that has nothing to do with the study. So what is asserted here is not "the
API knows about lung_nodule" but "the API and the worker cannot disagree", which is a
property of there being exactly one resolver.

THE SECOND DEFECT, AND WHY THIS FILE'S FIXTURES CHANGED SHAPE
---------------------------------------------------------------
The first mechanism read a dotted module path out of `MEDOS_CAPABILITY_PROVIDERS` and called
`importlib.import_module()` on it, in both platform processes. `MOS-REL-108` is unqualified
-- "no shared-library loading, no dynamic module import, no user-supplied code executed
inside a platform process" -- and `MOS-CONF-109` then cites that clause as the IEC 62304
section 4.3 segregation argument that lets a publisher classify platform items at a lower
safety class, calling it "a segregation argument a reviewer can execute rather than read".
A reviewer executing it found the mechanism it denies. Entry 68 of
`docs/spec/99-known-inconsistencies.md`.

The variable now SELECTS among names `medos/services/catalogue.py` holds, and that file reaches
every capability with an ordinary top-level import. So these tests no longer write modules
onto `sys.path` and no longer assert that a provider "did not import": there is no import of
a configured name left to exercise, which is the whole claim. What replaces those two tests
is an unknown selector refused with the known ones listed, and a scan of the seam's own
source for the constructs `MOS-REL-108` names -- `MOS-CONF-109`'s "execute rather than
read", executed.

WHAT IS ASSERTED, IN ORDER
    1. ONE RESOLVER      the two sides answer from the same call, under three different
                         configurations, including one this repository does not ship --
                         and the API keeps no capability list of its own to fall back to.
    2. NO PLUGIN API     the seam performs no dynamic import, reaches its catalogue by a
                         literal name, serves exactly what that catalogue statically
                         imports, and refuses an unknown selector by listing the known ones.
    3. FAIL CLOSED       nine ways a configured provider or a catalogue can be a deployment
                         defect -- the name is not one this image ships, it is the old
                         dotted path, the catalogue does not import, the catalogue holds a
                         key no configuration could select, the provider exposes no
                         `worker_registry`, it returns a non-Mapping, it returns a value
                         that is not a `Capability`, it adds nothing, it drops or mutates
                         what it was given -- each refusing to START, in BOTH processes,
                         with the same message naming the provider. And the worker refuses
                         BEFORE it opens a database connection.
    4. NO REPLACEMENT    a provider may ADD. Colliding with a platform capability id, or
                         with another provider's, is refused by name.
    5. READ-ONLY CORE    `medos.capabilities.REGISTRY` still holds exactly three keys
                         after every one of the above, and so does the shipped catalogue.
    6. END TO END        a `lung_nodule` job POSTed to a real uvicorn-hosted API over a
                         loopback socket, claimed by a real `WorkerRunner` whose registry
                         came from the CONFIGURATION and not from an injected argument,
                         COMPLETING with a stored SEG and SR on real LIDC-IDRI pixels.

THE SPEC DEFECT THIS SITS ON, NAMED RATHER THAN INVENTED
----------------------------------------------------------
There is NO requirement id for deployment-time capability registration in this build.
Chapter 2 section 2.9.3 defines registration as `POST /api/v1/service-versions` with an OCI
reference (`MOS-SVC-107`) and chapter 6 section 6.8 makes the `deployment` table the only
thing that decides whether a version receives work (`MOS-REG-072`); CONTRACT.md section 0
removes both from this slice, and neither chapter says what a deployment does in the
meantime. `MOS-REG-093` points the other way for the proprietary case ("no in-process
plugin loading"). The gap is recorded in `docs/spec/99-known-inconsistencies.md`. What the
specification DOES fix is how the stand-in may be BUILT, and that half is cited here rather
than treated as absent: `MOS-REL-108` and `MOS-CONF-109` bound the mechanism even where no
requirement names it.

Needs Postgres for the end-to-end test only; everything above it is pure process state.

Spec: MOS-REL-108, MOS-CONF-109, MOS-REL-107, MOS-REL-020, MOS-SVC-002, MOS-REG-042,
MOS-REG-051, MOS-API-051, MOS-EXEC-034, CONTRACT.md sections 6, 9 and 11.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import psycopg
import pytest
import uvicorn
from medos.api.app import create_app
from medos.api.routes_jobs import known_capability_ids
from medos.capabilities import providers
from medos.capabilities.providers import CapabilityProviderError
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    bind_current_tenant,
    reset_current_tenant,
)
from medos.dicomweb.gateway import FetchedSeries, SeriesSummary
from medos.worker.runner import RunnerConfig, WorkerRunner
from medos.worker.steps import WorkerDeps
from psycopg.rows import dict_row

from medos import capabilities
from medos.api import routes_jobs
from tests._support.capability_source import serve_one_extra_capability
from tests._support.skips import skip_no_data

PLATFORM_THREE = frozenset({"lung_segmentation", "emphysema_laa", "pleural_effusion"})

#: The selector this repository actually ships and that `medos/deploy/compose` configures. A
#: NAME and not a module path: `medos/services/catalogue.py` is keyed by it.
LUNG_NODULE_PROVIDER = "lung_nodule"

#: The value this variable held before `MOS-REL-108` was applied to it. Kept as a named
#: constant because an operator upgrading a running deployment still has it in a compose
#: file or a secret store, and what that value does now is a property worth pinning.
OLD_DOTTED_FORM = "services.lung_nodule.registry"

REPO_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_MODULE = REPO_ROOT / "medos" / "medos" / "capabilities" / "providers.py"
CATALOGUE_MODULE = REPO_ROOT / "medos" / "services" / "catalogue.py"

CORPUS = Path(os.environ.get("MEDOS_E2E_LCTSC_ROOT", "F:/WorkSpace/PulmoAI/TCIA"))
CASE_WITH_CANDIDATES = "LIDC-IDRI-0001"


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture(autouse=True)
def isolated_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Every test starts from an UNCONFIGURED deployment and leaves no memo behind.

    The resolver memoises on `(providers, registry_dir)` so that `known_capability_ids()`
    does not recompose a JSON dictionary on every `POST /api/v1/jobs`. That memo is
    process state, so it is cleared on both sides of every test here -- otherwise a test
    that configured a provider would leak its answer into the next one and the whole file
    would assert the first configuration it happened to run.
    """
    monkeypatch.delenv(providers.ENV_PROVIDERS, raising=False)
    monkeypatch.setenv(providers.ENV_REGISTRY_DIR, str(tmp_path / "registry"))
    providers.clear_cache()
    yield
    providers.clear_cache()


@pytest.fixture()
def stand_in_catalogue(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Put a catalogue in front of the resolver that this repository does not ship.

    `providers._shipped_catalogue` is the seam, and NOT `service_catalogue()`. What varies
    between deployments is which modules the IMAGE holds; everything downstream of that --
    the catalogue key check, the selector lookup, the provider contract, the concept
    chaining, the memo, and both process entry points -- stays the real code, so these
    tests still measure the mechanism rather than their own stubs.

    The entries are real `ModuleType` objects built here instead of source files written
    onto `sys.path`. That is not a weakening of the fixture it replaces, it IS the change:
    a provider now reaches a platform process because a top-level import in
    `medos/services/catalogue.py` put it there, and nothing a deployment configures can cause an
    import. The failure the old fixture existed to reproduce -- a module that explodes
    while the resolver is importing it -- has no resolver-side counterpart any more; what
    remains of it is `test_a_catalogue_that_does_not_import_refuses_startup`, which breaks
    the ONE import the platform still performs.
    """
    shipped = providers._shipped_catalogue

    def install(entries: Mapping[str, Any]) -> None:
        catalogue = {**shipped(), **entries}
        monkeypatch.setattr(providers, "_shipped_catalogue", lambda: catalogue)
        providers.clear_cache()

    yield install


def module(name: str, **members: Any) -> ModuleType:
    """A catalogue value: a module object exposing the provider contract's two names."""
    built = ModuleType(name)
    for attribute, value in members.items():
        setattr(built, attribute, value)
    return built


@dataclass(frozen=True)
class Stub:
    """A `Capability` satisfying CONTRACT.md section 6 structurally, and doing nothing."""

    capability_id: str = "vendor_stub"
    version: str = "1.0.0"

    def applicable(self, vol: Any) -> None:
        return None

    def run(self, vol: Any, ctx: Any) -> Any:
        raise NotImplementedError


def adds_vendor_stub(concepts: Any, base: Any = None) -> dict[str, Any]:
    """A provider that behaves. Used as the "second vendor" in the collision tests."""
    from medos.capabilities import REGISTRY

    registry = dict(REGISTRY if base is None else base)
    registry[Stub.capability_id] = Stub()
    return registry


def good_provider(name: str) -> ModuleType:
    return module(name, worker_registry=adds_vendor_stub)


def configure(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    monkeypatch.setenv(providers.ENV_PROVIDERS, " ".join(names))
    providers.clear_cache()


def worker_side_ids(tmp_path: Path) -> frozenset[str]:
    """What `medos-worker` would serve: the DEFAULT `WorkerDeps`, resolved right now.

    `gateway=object()` because `WorkerDeps` only stores it -- the point of this call is the
    two `default_factory`s, which are the worker's half of the seam.
    """
    deps = WorkerDeps(gateway=object(), work_root=tmp_path / "work")  # type: ignore[arg-type]
    return frozenset(deps.registry)


# =====================================================================================
# 1. ONE RESOLVER -- the API and the worker cannot disagree
# =====================================================================================
def test_an_unconfigured_deployment_serves_exactly_the_platform_three(
    tmp_path: Path,
) -> None:
    """The stock answer did not move. This is the control for everything below."""
    assert known_capability_ids() == PLATFORM_THREE
    assert worker_side_ids(tmp_path) == PLATFORM_THREE


def test_the_api_refuses_the_capability_when_it_is_not_configured(
    tmp_path: Path,
) -> None:
    """`lung_nodule` is NOT admitted merely because the code is in the tree.

    The seam is configuration, not discovery. Appearing in `medos/services/catalogue.py` makes a
    capability SELECTABLE and nothing more; a resolver that served every catalogued name
    would make every image serve every capability it could find, and an operator could no
    longer state which ones this deployment runs.
    """
    assert "lung_nodule" not in known_capability_ids()
    assert "lung_nodule" not in worker_side_ids(tmp_path)


def test_the_api_admits_the_capability_when_it_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, LUNG_NODULE_PROVIDER)

    assert known_capability_ids() == PLATFORM_THREE | {"lung_nodule"}
    assert worker_side_ids(tmp_path) == PLATFORM_THREE | {"lung_nodule"}


def test_the_api_and_the_worker_answer_from_one_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """The invariant, asserted across three configurations rather than one.

    Equality here is the whole mechanism. Two independent compositions would agree on the
    configuration that shipped and diverge on the one nobody tried, so the third case uses
    a provider this repository does not contain.
    """
    stand_in_catalogue({"vendor_alpha": good_provider("vendor_alpha")})

    for names, expected in (
        ((), PLATFORM_THREE),
        ((LUNG_NODULE_PROVIDER,), PLATFORM_THREE | {"lung_nodule"}),
        (
            (LUNG_NODULE_PROVIDER, "vendor_alpha"),
            PLATFORM_THREE | {"lung_nodule", "vendor_stub"},
        ),
    ):
        configure(monkeypatch, *names)
        api = known_capability_ids()
        worker = worker_side_ids(tmp_path)
        assert api == worker == expected, (names, sorted(api), sorted(worker))


def test_the_api_holds_no_capability_list_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """`known_capability_ids()` answers from the resolver and has nowhere else to look.

    THE DEFECT THIS CLOSES. The function used to end:

        except ModuleNotFoundError:
            return SLICE_CAPABILITY_IDS

    -- a frozenset of the original three, exported in `medos.api.routes_jobs.__all__`.
    That guard dated from when `medos.capabilities` was a sibling deliverable that might
    not have landed; the resolver now lives inside that package, so the branch could only
    fire on a broken install, and what it did there was serve a NARROWER set than the
    deployment configured. A deployment whose resolver is unreachable would have admitted
    three capabilities and refused the fourth -- the API and the worker disagreeing, which
    is the one outcome this whole seam exists to prevent, reached through the error path.

    Asserted two ways, because neither alone holds. Behaviourally: a capability only the
    deployment resolver knows about is admitted, so the answer really does come from
    there. Structurally: no module-level name in `routes_jobs.py` binds a capability id at
    all, so there is no second list for a future edit to fall back to. A constant naming
    three capability ids, exported as public surface, reads as authoritative to the next
    person who needs one -- that is the hazard, not the unreachable branch.
    """
    served = serve_one_extra_capability(monkeypatch)
    assert known_capability_ids() == served

    source = Path(routes_jobs.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=routes_jobs.__file__)
    platform_ids = set(PLATFORM_THREE)
    offenders: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        for literal in ast.walk(node):
            if isinstance(literal, ast.Constant) and literal.value in platform_ids:
                offenders.append(f"line {node.lineno}: {literal.value!r}")
    assert not offenders, (
        f"medos/medos/api/routes_jobs.py binds capability ids at module level: {offenders}. "
        f"The admitted set has exactly one source, and a copy here is the one a fallback "
        f"reaches for."
    )


def test_two_providers_compose_onto_one_coded_concept_dictionary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """MOS-REG-042: "a second code table anywhere in the platform is forbidden".

    Each provider is handed the PREVIOUS composition as its base, so the dictionary the
    last one returns contains every earlier provider's rows. Composing each overlay onto
    the platform base independently would leave whichever dictionary the worker happened
    to keep missing the other capability's codes -- and a capability resolving codes out of
    a table that does not contain them raises at `coded()`, mid-job, on a study.
    """
    stand_in_catalogue({"vendor_beta": good_provider("vendor_beta")})
    configure(monkeypatch, LUNG_NODULE_PROVIDER, "vendor_beta")

    resolved = providers.resolve()
    assert resolved.concepts is not None
    blob = json.loads(resolved.concepts.path.read_text(encoding="utf-8"))

    # The platform's own rows and the overlay's, in ONE file.
    assert "anatomy.lung" in blob["concepts"]
    assert any(key.startswith("finding.") for key in blob["concepts"])
    assert resolved.supplied_by["lung_nodule"] == LUNG_NODULE_PROVIDER
    assert resolved.supplied_by["vendor_stub"] == "vendor_beta"


# =====================================================================================
# 2. NO IN-PROCESS PLUGIN API -- MOS-REL-108, executed rather than read
# =====================================================================================
#: Every construct that turns a name computed at run time into running code. `getattr` is
#: NOT here: `getattr(module, REGISTRY_FUNCTION)` reads an attribute, under a module-level
#: constant name, of a module the catalogue already holds -- and reading an attribute is
#: not loading code. `re.compile` is not here either while the bare builtin `compile` is,
#: which is why the scan builds the FULL dotted callee instead of matching a last segment.
FORBIDDEN_CALLS = frozenset(
    {
        "__import__",
        "exec",
        "eval",
        "compile",
        "import_module",
        "reload",
        "importlib.import_module",
        "importlib.reload",
        "importlib.__import__",
        "importlib.util.spec_from_file_location",
        "importlib.util.module_from_spec",
        "spec_from_file_location",
        "module_from_spec",
        "SourceFileLoader",
        "exec_module",
        "load_module",
    }
)

#: Machinery whose only purpose is reaching a module named at run time.
FORBIDDEN_IMPORTS = frozenset({"importlib", "imp", "pkgutil", "runpy", "pkg_resources"})


def _callee(node: ast.expr) -> str:
    """The full dotted name of a call target, so `re.compile` and `compile` differ."""
    parts: list[str] = []
    current: ast.expr | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _imported_names(path: Path) -> set[str]:
    """Every module name imported anywhere in a file, function bodies included.

    The whole tree and not only module scope: the platform's one import of the Service
    Plane is deliberately function-local (`providers._shipped_catalogue`), and a scan that
    skipped function bodies would report that file as importing nothing at all.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("path", [PROVIDERS_MODULE, CATALOGUE_MODULE], ids=lambda p: p.name)
def test_the_seam_performs_no_dynamic_import(path: Path) -> None:
    """`MOS-REL-108`, as a check rather than a docstring claim.

    "MedicalOS MUST NOT offer an in-process plugin API -- no shared-library loading, no
    dynamic module import, no user-supplied code executed inside a platform process." This
    is `MOS-CONF-109`'s "segregation argument a reviewer can execute rather than read",
    executed: the two modules where a value from OUTSIDE the image arrives are parsed, and
    the constructs that turn a run-time name into running code are absent from both.

    PARSED, NOT GREPPED, and that distinction is load-bearing here: both modules discuss
    `importlib.import_module()` in prose, because the mechanism they replaced used it. A
    text search would report the explanation as the defect. The AST sees calls and imports
    and never sees a docstring.

    BOUNDED TO THE SEAM, said plainly rather than left to be discovered: `medos/medos/core`
    and `medos/medos/resolution` both call `importlib.import_module()` in their lazy re-export
    shims. Those compose a name from a fixed table inside the same module and can only ever
    reach `medos.*`, so no configuration steers them -- a distinction this scan cannot make,
    which is why it is pointed at the modules a deployment's configuration actually reaches
    rather than at the tree.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node.func)
        assert callee not in FORBIDDEN_CALLS, (
            f"{path.name}:{node.lineno} calls {callee}(). MOS-REL-108 forbids a dynamic "
            f"module import inside a platform process without qualification, and "
            f"MOS-CONF-109 cites it as the IEC 62304 section 4.3 segregation evidence a "
            f"reviewer is invited to execute."
        )

    for name in _imported_names(path):
        assert name.split(".")[0] not in FORBIDDEN_IMPORTS, (
            f"{path.name} imports {name!r}, which exists to reach a module named at run "
            f"time. The capability seam selects among names this image already ships."
        )


def test_the_catalogue_is_reached_by_a_literal_name() -> None:
    """The one import the platform performs is spelled in the source, not read from it.

    The positive half of the scan above. `services.catalogue` being a literal is what makes
    "the set of code a configured deployment can execute is the set of import statements in
    the image" a true sentence rather than an aspiration.
    """
    assert "services.catalogue" in _imported_names(PROVIDERS_MODULE)


def test_what_the_resolver_can_serve_is_what_the_catalogue_statically_imports() -> None:
    """Every catalogued module got there through an `import` statement in that file.

    The runtime mapping tied back to the static source. A catalogue that assembled its
    values some other way -- a scan of `medos/services/`, a name built from a setting -- would
    pass every other test in this file and would be the plugin API again.
    """
    catalogue = providers.service_catalogue()
    assert catalogue, "this image ships no capability package at all"

    imported = _imported_names(CATALOGUE_MODULE)
    for name, supplier in catalogue.items():
        assert isinstance(supplier, ModuleType), (name, type(supplier).__name__)
        assert supplier.__name__ in imported, (
            f"the catalogue serves {name!r} from {supplier.__name__!r}, which "
            f"`medos/services/catalogue.py` does not import. Whatever put it there is not an "
            f"import statement, and MOS-REL-108 is a statement about what can be."
        )

    # And the shipped answer is this repository's, not a stand-in leaked from another test.
    assert providers.known_providers() == (LUNG_NODULE_PROVIDER,)


# =====================================================================================
# 3. FAIL CLOSED AND LOUD -- nine deployment defects, nine refusals to start
# =====================================================================================
def _refusal(monkeypatch: pytest.MonkeyPatch, *names: str) -> str:
    """Configure `names`, assert BOTH processes refuse to start, return the message.

    Both, and not one: "refuse to start" has to be true of the API process and of the
    worker process independently, because a deployment in which only one of them refuses
    is a deployment that accepts jobs nothing will ever run.
    """
    configure(monkeypatch, *names)

    with pytest.raises(CapabilityProviderError) as api_refused:
        create_app(connect=lambda **_: None, configure_logs=False)  # type: ignore[arg-type,return-value]

    providers.clear_cache()
    with pytest.raises(CapabilityProviderError) as worker_refused:
        WorkerDeps(gateway=object(), work_root=Path("."))  # type: ignore[arg-type]

    assert str(api_refused.value) == str(worker_refused.value)
    return str(api_refused.value)


def test_a_selector_this_image_does_not_ship_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R1. The commonest deployment defect there is: a typo, or a capability not in this
    image. It replaces "the provider does not import", which was the same defect reached
    through the mechanism `MOS-REL-108` forbids.

    The refusal LISTS WHAT IS KNOWN, which the import-based version structurally could not:
    a failed `import_module()` knows nothing about what would have succeeded, so an operator
    with a typo got a `ModuleNotFoundError` and no way to find the right spelling short of
    reading the tree.

    The alternative -- falling back to the platform three -- is the failure this whole
    mechanism exists to prevent. A deployment that believes it runs a fourth capability and
    silently does not produces a worklist with findings missing and nothing anywhere saying
    so.
    """
    message = _refusal(monkeypatch, "lung_nodul")  # sic

    assert "lung_nodul" in message
    assert "not one this image ships" in message
    assert "lung_nodule" in message  # and it names what IS selectable
    # It must NOT read as a recoverable condition.
    assert "lung_segmentation" in message  # names what it refused to fall back to


def test_the_old_dotted_module_path_is_refused_and_names_its_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration, named. An operator upgrading has the old value in a compose file.

    `services.lung_nodule.registry` was a working configuration one commit ago. Refused
    with nothing but "not one this image ships", it reads as "the capability was removed"
    -- the wrong conclusion, and a wrong remedy after it. So the refusal says the form is
    gone, says what went with it, and names the selector that replaces this exact value.
    """
    message = _refusal(monkeypatch, OLD_DOTTED_FORM)

    assert OLD_DOTTED_FORM in message
    assert "dotted module path this variable used to take" in message
    assert f"the selector for it is {LUNG_NODULE_PROVIDER!r}" in message
    assert "MOS-REL-108" in message


def test_a_catalogue_that_does_not_import_refuses_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ONE import the platform performs, broken: `medos/services/` absent from the image.

    This is what remains of "a provider that raises on import" once no CONFIGURED name is
    ever imported. The catalogue is first-party code the image is supposed to contain, and
    an image that does not contain it cannot serve the capability its configuration names.
    Refused in both processes, naming the PYTHONPATH the Dockerfile sets, because "did not
    import" with no pointer is where an operator loses an afternoon.
    """
    monkeypatch.setitem(sys.modules, "services.catalogue", None)
    message = _refusal(monkeypatch, LUNG_NODULE_PROVIDER)

    assert "medos/services/catalogue.py" in message
    assert "did not import" in message
    assert "PYTHONPATH" in message
    assert "lung_segmentation" in message  # and it did not fall back


def test_a_catalogue_key_no_configuration_could_select_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """The catalogue's own defect, and the quietest one in this file.

    `configured_providers()` splits `MEDOS_CAPABILITY_PROVIDERS` on commas and whitespace,
    so a key holding either could be shipped, catalogued, and selected by no value an
    operator can write. The capability would be in the image and unreachable -- the 0.3.0
    defect this seam closes, arrived at from inside -- and nothing would report it, because
    the configuration naming it simply matches nothing.
    """
    stand_in_catalogue({"vendor stub": good_provider("vendor_spaced")})
    message = _refusal(monkeypatch, LUNG_NODULE_PROVIDER)

    assert "vendor stub" in message
    assert "never be selected" in message
    assert "medos/services/catalogue.py" in message


def test_a_provider_with_no_registry_function_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """R2. A catalogued module that is not a provider -- a package root, say."""
    stand_in_catalogue({"vendor_empty": module("vendor_empty", VERSION="1.0.0")})
    message = _refusal(monkeypatch, "vendor_empty")

    assert "vendor_empty" in message
    assert "worker_registry" in message


def test_a_provider_that_returns_a_non_mapping_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """R2. `worker_registry` returning a list of capabilities is the obvious near-miss."""
    stand_in_catalogue(
        {
            "vendor_listy": module(
                "vendor_listy",
                worker_registry=lambda concepts, base=None: ["vendor_stub"],
            )
        }
    )
    message = _refusal(monkeypatch, "vendor_listy")

    assert "vendor_listy" in message
    assert "not a Mapping" in message


def test_a_provider_whose_value_is_not_a_capability_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """R2, on the values. CONTRACT.md section 6 is the bar and it is structural.

    A module or a class object in the mapping is what a provider that returned
    `{"x": SomeCapability}` instead of `{"x": SomeCapability()}` produces, and the worker
    would only discover it at `capability.applicable(vol)` -- inside a job, on a study.
    """

    class NotQuite:
        capability_id = "vendor_stub"
        version = "1.0.0"
        # no applicable(), no run()

    def worker_registry(concepts: Any, base: Any = None) -> dict[str, Any]:
        from medos.capabilities import REGISTRY

        registry = dict(REGISTRY if base is None else base)
        registry["vendor_stub"] = NotQuite()
        return registry

    stand_in_catalogue(
        {"vendor_halfbaked": module("vendor_halfbaked", worker_registry=worker_registry)}
    )
    message = _refusal(monkeypatch, "vendor_halfbaked")

    assert "vendor_halfbaked" in message
    assert "vendor_stub" in message
    assert "Capability" in message
    assert "applicable" in message and "run" in message


def test_a_provider_that_adds_nothing_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """R4. The silent fallback, reached from the other direction.

    A provider that returns its base unchanged leaves a deployment serving the platform
    three while its configuration says otherwise -- which is exactly the state an operator
    would be in if the resolver had swallowed an import error.
    """

    def worker_registry(concepts: Any, base: Any = None) -> dict[str, Any]:
        from medos.capabilities import REGISTRY

        return dict(REGISTRY if base is None else base)

    stand_in_catalogue(
        {"vendor_inert": module("vendor_inert", worker_registry=worker_registry)}
    )
    message = _refusal(monkeypatch, "vendor_inert")

    assert "vendor_inert" in message
    assert "added no capability" in message


def test_a_provider_that_mutates_its_base_in_place_is_still_checked(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """The check is a comparison against a SNAPSHOT, not against the object handed over.

    `base[cid] = Mine(); return base` is the shortest way to write a provider, and it makes
    the "before" and the "after" the same object. Compared naively, a shadowed
    `lung_segmentation` would read as no change at all -- R3 would be a tautology and this
    provider would be admitted. The resolver copies before it calls, so it is not.
    """

    @dataclass(frozen=True)
    class Shadow:
        capability_id: str = "lung_segmentation"
        version: str = "9.9.9"

        def applicable(self, vol: Any) -> None:
            return None

        def run(self, vol: Any, ctx: Any) -> Any:
            raise NotImplementedError

    def worker_registry(concepts: Any, base: Any = None) -> Any:
        base["lung_segmentation"] = Shadow()  # in place, and returns the same object
        return base

    stand_in_catalogue(
        {"vendor_inplace": module("vendor_inplace", worker_registry=worker_registry)}
    )
    message = _refusal(monkeypatch, "vendor_inplace")

    assert "vendor_inplace" in message
    assert "lung_segmentation" in message
    assert "REPLACE" in message

    from medos.capabilities import REGISTRY

    assert REGISTRY["lung_segmentation"].version != "9.9.9"


def test_a_provider_that_drops_a_capability_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """R3, the quiet half. Composing `{own}` instead of `base | {own}` disables three."""
    stand_in_catalogue(
        {
            "vendor_forgetful": module(
                "vendor_forgetful",
                worker_registry=lambda concepts, base=None: {"vendor_stub": Stub()},
            )
        }
    )
    message = _refusal(monkeypatch, "vendor_forgetful")

    assert "vendor_forgetful" in message
    assert "DROPPED" in message
    assert "lung_segmentation" in message


def test_the_worker_refuses_before_it_opens_a_database_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordering inside `WorkerRunner.__init__`, which was MEASURED and not assumed.

    Before `capability_providers.resolve()` moved ahead of `connect()`, a broken provider
    in the container surfaced as a `psycopg.OperationalError` naming the DSN -- the wrong
    thing entirely, and a diagnosis an operator follows for an hour. A worker must also not
    be holding a connection, a claim or a lease while it discovers that it cannot run
    anything at all.

    The DSN here points at a port nothing listens on, so a regression in that ordering
    raises `OperationalError` and fails this test rather than passing for free.
    """
    configure(monkeypatch, "not_in_this_image")
    config = RunnerConfig(
        dsn="postgresql://medos:medos@127.0.0.1:1/medos_nonexistent",
        work_root=tmp_path / "work",
    )

    with pytest.raises(CapabilityProviderError) as refused:
        WorkerRunner(config)

    assert "not_in_this_image" in str(refused.value)


# =====================================================================================
# 4. A PROVIDER MAY ADD AND NOTHING ELSE
# =====================================================================================
def test_a_provider_that_shadows_a_platform_capability_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """The supply-chain hole, closed. MOS-REG-051: two implementations, one id.

    A third-party `lung_segmentation` that the platform accepted would be undetectable
    afterwards: every SEG's `model_version`, every `results` provenance block and every SR
    would name the platform capability while the vendor's code produced the voxels. There
    is no precedence rule to appeal to here -- the refusal names the provider and the id
    and stops the process.
    """

    @dataclass(frozen=True)
    class Shadow:
        capability_id: str = "lung_segmentation"
        version: str = "9.9.9"

        def applicable(self, vol: Any) -> None:
            return None

        def run(self, vol: Any, ctx: Any) -> Any:
            raise NotImplementedError

    def worker_registry(concepts: Any, base: Any = None) -> dict[str, Any]:
        from medos.capabilities import REGISTRY

        registry = dict(REGISTRY if base is None else base)
        registry["lung_segmentation"] = Shadow()  # the whole defect, in one line
        return registry

    stand_in_catalogue(
        {"vendor_shadow": module("vendor_shadow", worker_registry=worker_registry)}
    )
    message = _refusal(monkeypatch, "vendor_shadow")

    assert "vendor_shadow" in message
    assert "lung_segmentation" in message
    assert "REPLACE" in message
    assert "platform" in message

    # And the platform capability is still the platform's.
    from medos.capabilities import REGISTRY

    assert REGISTRY["lung_segmentation"].version != "9.9.9"


def test_a_provider_that_collides_with_another_provider_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """Two vendors, one capability id. Refused, and the message names the second one.

    Which of the two is "the" implementation is not a question with an answer, so this is a
    collision and not an override. It is also the case the platform-name check alone would
    miss, because neither id is the platform's.
    """
    stand_in_catalogue(
        {
            "vendor_one": good_provider("vendor_one"),
            "vendor_two": good_provider("vendor_two"),  # same `vendor_stub` id
        }
    )
    message = _refusal(monkeypatch, "vendor_one", "vendor_two")

    assert "vendor_two" in message
    assert "vendor_stub" in message
    assert "REPLACE" in message
    # The FIRST provider is not at fault and must not be the one named as the offender.
    assert message.index("vendor_two") < message.index("vendor_stub")


def test_a_provider_whose_capability_disagrees_with_its_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """The worker dispatches by KEY; every `results` row records the DECLARED id.

    A mapping whose key and whose `capability_id` differ produces evidence attributed to a
    capability the job did not ask for, and nothing downstream compares the two.
    """

    @dataclass(frozen=True)
    class Mislabelled:
        capability_id: str = "what_it_says_it_is"
        version: str = "1.0.0"

        def applicable(self, vol: Any) -> None:
            return None

        def run(self, vol: Any, ctx: Any) -> Any:
            raise NotImplementedError

    def worker_registry(concepts: Any, base: Any = None) -> dict[str, Any]:
        from medos.capabilities import REGISTRY

        registry = dict(REGISTRY if base is None else base)
        registry["what_it_is_filed_under"] = Mislabelled()
        return registry

    stand_in_catalogue(
        {
            "vendor_mislabelled": module(
                "vendor_mislabelled", worker_registry=worker_registry
            )
        }
    )
    message = _refusal(monkeypatch, "vendor_mislabelled")

    assert "what_it_is_filed_under" in message
    assert "what_it_says_it_is" in message


# =====================================================================================
# 5. THE PLATFORM SINGLETON STAYS READ-ONLY
# =====================================================================================
def test_the_platform_singleton_is_never_mutated(
    monkeypatch: pytest.MonkeyPatch, stand_in_catalogue: Any
) -> None:
    """`medos.capabilities.REGISTRY` holds exactly three keys, before and after.

    The obvious implementation of this seam -- have the resolver write the provider's
    entries into the module global -- would make `zero-core-change` true of the diff and
    false of the runtime, and would break `tests/unit/test_capabilities.py`,
    `test_lung_nodule.py::test_the_platform_registry_is_untouched` and every other consumer
    in the process including `medos.training.runs`. CONTRACT.md section 11.
    """
    from medos.capabilities import REGISTRY

    before = dict(REGISTRY)
    stand_in_catalogue({"vendor_gamma": good_provider("vendor_gamma")})
    configure(monkeypatch, LUNG_NODULE_PROVIDER, "vendor_gamma")

    resolved = providers.resolve()

    assert set(resolved.registry) == PLATFORM_THREE | {"lung_nodule", "vendor_stub"}
    assert set(REGISTRY) == PLATFORM_THREE
    assert REGISTRY == before
    # Identity, not equality: the composed mapping holds the platform's own objects.
    for cid in PLATFORM_THREE:
        assert resolved.registry[cid] is REGISTRY[cid]


def test_the_shipped_catalogue_is_read_only_and_survives_a_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalogue is the singleton problem one level up, so it is closed the same way.

    It is imported once per process and read on every resolution. A resolver that wrote
    into it -- or a caller that could -- would be editing what every later resolution in
    that process serves, which is the global mutable state CONTRACT.md section 11 forbids
    and exactly why `resolve()` returns a `MappingProxyType` of its own.
    """
    from services.catalogue import CATALOGUE

    before = dict(CATALOGUE)
    configure(monkeypatch, LUNG_NODULE_PROVIDER)
    providers.resolve()

    assert dict(CATALOGUE) == before
    with pytest.raises(TypeError):
        CATALOGUE["smuggled"] = object()  # type: ignore[index]


def test_the_resolved_mapping_cannot_be_mutated_by_a_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composed answer is read-only too, or the memo becomes global mutable state.

    `resolve()` is memoised, so a caller that wrote into the mapping it returned would be
    editing every later caller's answer -- the singleton problem, reconstructed one level
    up.
    """
    configure(monkeypatch, LUNG_NODULE_PROVIDER)
    registry = providers.resolve().registry

    with pytest.raises(TypeError):
        registry["smuggled"] = object()  # type: ignore[index]


def test_changing_the_configuration_changes_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The memo is keyed on the configuration, not on "what this process started with".

    `known_capability_ids()` has always been documented as resolving at call time. Adding a
    cache under it would quietly break that promise, so the cache key contains the provider
    list and the registry directory.
    """
    assert known_capability_ids() == PLATFORM_THREE

    monkeypatch.setenv(providers.ENV_PROVIDERS, LUNG_NODULE_PROVIDER)
    assert "lung_nodule" in known_capability_ids()  # no clear_cache() call

    monkeypatch.delenv(providers.ENV_PROVIDERS)
    assert known_capability_ids() == PLATFORM_THREE


# =====================================================================================
# 6. END TO END -- the real API, the real worker, real pixels
# =====================================================================================
def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def running_server(app: Any) -> Iterator[str]:
    """One `uvicorn` server in a thread. Mirrors `test_api.py::running_server`."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


class FakeGateway:
    """A `DicomWebGateway` stand-in serving real files off disk.

    The same fake as `test_lung_nodule.py`'s, and for the same reason: the pipeline must
    really decode LIDC-IDRI pixels and really threshold Hounsfield units, or every
    assertion below passes while nothing was measured. It is also the ONLY thing injected
    into the worker here -- the registry and the concept dictionary come from the
    configuration, which is the claim this test exists to make.
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

    Invented UIDs are not an option: `require_source_grid` compares
    `ctx.series_instance_uid` to the volume's own. LIDC-IDRI is public, de-identified,
    CC BY 3.0 data, so its UIDs carry no PHI.
    """
    import pydicom

    ds = pydicom.dcmread(str(files[0]), stop_before_pixels=True)
    return str(ds.StudyInstanceUID), str(ds.SeriesInstanceUID)


@pytest.fixture()
def wconn(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
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


def test_a_configured_capability_submits_through_the_api_and_runs_on_the_worker(
    wconn: psycopg.Connection[Any],
    pg_dsn: str,
    api_key: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole seam, end to end, with nothing injected but a PACS.

    WHAT MAKES THIS DIFFERENT FROM `test_lung_nodule.py`'s end-to-end test: that one builds
    `WorkerDeps(registry=worker_registry(...), concepts=...)` by hand, which proves the
    capability WORKS and proves nothing about whether a deployment can reach it. Here the
    only argument this test supplies is the gateway. The registry and the concept dictionary
    on the runner are the ones `MEDOS_CAPABILITY_PROVIDERS` produced, and the API that
    accepted the job read the same variable through the same function.

    Four claims, in order:
      1. a `lung_nodule` POST is refused `400 UNKNOWN_CAPABILITY` with the provider absent;
      2. the same POST is `202 QUEUED` with it configured;
      3. the worker's DEFAULT deps contain the capability, from the configuration alone;
      4. the job COMPLETES, with a `lung_nodule` result row and stored DICOM objects.
    """
    files = _case_files(CASE_WITH_CANDIDATES)
    study_uid, series_uid = _case_uids(files)
    body = {"study_instance_uid": study_uid, "capabilities": ["lung_nodule"]}
    auth = {"Authorization": f"Bearer {api_key}"}

    def opener(**kwargs: Any) -> psycopg.Connection[Any]:
        return psycopg.connect(pg_dsn, row_factory=dict_row, **kwargs)

    # -- 1. unconfigured: the API refuses, and says what it does accept -----------------
    with running_server(create_app(connect=opener, configure_logs=False)) as url:
        with httpx.Client(base_url=url, timeout=30.0, headers=auth) as client:
            refused = client.post("/api/v1/jobs", json=body)
    assert refused.status_code == 400, refused.text
    doc = refused.json()
    assert doc["code"] == "UNKNOWN_CAPABILITY"
    assert "lung_nodule" not in doc["supported_capabilities"]

    # -- 2. configured: the same request is accepted ------------------------------------
    configure(monkeypatch, LUNG_NODULE_PROVIDER)
    with running_server(create_app(connect=opener, configure_logs=False)) as url:
        with httpx.Client(base_url=url, timeout=30.0, headers=auth) as client:
            accepted = client.post("/api/v1/jobs", json=body)
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    assert accepted.json()["state"] == "QUEUED"

    # -- 3. the worker resolves the SAME capability, from the SAME variable --------------
    cfg = RunnerConfig(
        dsn=pg_dsn,
        work_root=tmp_path / "work",
        worker_id="wrk_provider_seam",
        lease_seconds=1800,
        heartbeat_seconds=60,
        reclaim_on_poll=False,
    )
    runner = WorkerRunner(cfg, conn=wconn)
    assert "lung_nodule" in runner.deps.registry, (
        "the worker built its default WorkerDeps and did NOT get the configured "
        "capability; the API would have accepted a job this process cannot run"
    )
    gateway = FakeGateway(
        series=[SeriesSummary(study_uid, series_uid, "CT", len(files))], files=files
    )
    # The ONLY substitution. `replace()` on the runner's own deps keeps the registry and
    # the concept dictionary the resolver produced.
    runner.deps = replace(runner.deps, gateway=gateway)

    outcome = runner.run_once()

    # -- 4. it completed, and the evidence names this capability -------------------------
    assert outcome is not None, "the worker claimed nothing; the job never reached it"
    assert outcome.job_id == job_id
    assert outcome.terminal_state == "COMPLETED", outcome

    rows = wconn.execute(
        "SELECT capability_id FROM results WHERE job_id = "
        "(SELECT id FROM jobs WHERE public_id = %s)",
        (job_id,),
    ).fetchall()
    assert [r["capability_id"] for r in rows] == ["lung_nodule"]
    assert gateway.stored, "no DICOM object was STOWed"


# =====================================================================================
# Chapter 9 metadata, and the ordering, for a capability the PLATFORM does not ship
# =====================================================================================
def test_the_clinical_block_of_a_configured_capability_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`metadata_for` answers for what the DEPLOYMENT serves, not what this module ships.

    It read `medos.capabilities.REGISTRY` directly, so with `lung_nodule` configured it
    raised `KeyError`; `medos.training.runs._output_kind_of` caught that and returned
    `None`, and a training run against the capability release 0.3.0 exists to prove lost
    its output kind with nothing logged. A soft failure on a `MOS-SAFE-013` block that
    "MUST be present and has no default" is the `MOS-SVC-011` silence this project forbids.

    This uses the REAL resolution path -- a catalogue selector in the environment variable
    -- rather than a patched `capability_ids`, because the defect was in which mapping was
    consulted, and a patch of the answer cannot see that.
    """
    monkeypatch.setenv(providers.ENV_PROVIDERS, LUNG_NODULE_PROVIDER)
    providers.clear_cache()
    try:
        metadata = capabilities.metadata_for("lung_nodule")
        order = capabilities.execution_order(["lung_nodule"])
    finally:
        providers.clear_cache()

    assert metadata is not None
    assert metadata.output_kinds, (
        "an empty clinical block is how the defect presented downstream: "
        "`_output_kind_of` returns None and the training run records no output kind"
    )
    assert order == ("lung_nodule",)


def test_the_ordering_still_refuses_a_capability_nothing_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative control for the test above.

    Resolving against the deployment must WIDEN the answer, never make it permissive: an
    id no provider contributed is still an error, and the message names what this
    deployment serves rather than what the platform ships, because those are now different
    sets and the operator needs the one they configured.
    """
    monkeypatch.setenv(providers.ENV_PROVIDERS, LUNG_NODULE_PROVIDER)
    providers.clear_cache()
    try:
        with pytest.raises(KeyError) as excinfo:
            capabilities.execution_order(["no_such_capability"])
    finally:
        providers.clear_cache()

    message = str(excinfo.value)
    assert "no_such_capability" in message
    assert "lung_nodule" in message, (
        "the refusal must list the DEPLOYMENT's set; listing the platform three would send "
        "an operator looking for a configuration defect that is not there"
    )
