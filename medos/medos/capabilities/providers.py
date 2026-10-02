# SPDX-License-Identifier: Apache-2.0
"""The deployment-time capability seam: ONE resolver, read by the API and the worker.

THE DEFECT THIS CLOSES
-----------------------
0.3.0 shipped `lung_nodule` under `medos/services/` without touching a core file, and that
measurement is real. What it did NOT ship was any way to DEPLOY it:

  * `medos/services/lung_nodule/registry.py` exposes `worker_registry(concepts, base=...)`, a
    composition function, and nothing in `deploy/` or `medos/medos/` ever called it.
  * `medos.api.routes_jobs.known_capability_ids()` read the module singleton
    `medos.capabilities.REGISTRY`, so the API refused a `lung_nodule` job at submit.
  * `medos.worker.steps.WorkerDeps.registry` is injectable, but only a test ever injected
    it; `medos.worker.runner` builds its own `WorkerDeps` and took the default.

So the capability ran end to end in tests and was unreachable in production. This module
is the missing mechanism: a deployment NAMES its providers in the environment, and the API
and the worker both resolve that same name through this one function.

THE SECOND DEFECT, AND WHY THE SHAPE OF THIS MODULE CHANGED
-------------------------------------------------------------
The first version of this module read a dotted module path out of `MEDOS_CAPABILITY_PROVIDERS`
and called `importlib.import_module()` on it, in the API process and in the worker process.
`MOS-REL-108` is unqualified:

    "MedicalOS MUST NOT offer an in-process plugin API -- no shared-library loading, no
     dynamic module import, no user-supplied code executed inside a platform process."

That is not an engineering-style rule here. `MOS-CONF-109` cites `MOS-REL-107`/`MOS-REL-108`
as the IEC 62304 section 4.3 SEGREGATION ARGUMENT that lets a publisher classify platform
items at a lower safety class, and calls it "a segregation argument a reviewer can execute
rather than read". A reviewer executing it against the previous version found `importlib`
over an environment variable inside a platform process -- the mechanism the argument denies
exists. Recorded as entry 68 of `docs/spec/99-known-inconsistencies.md`.

So the variable no longer names a module. It SELECTS among names this image already ships,
and the list of them is `medos/services/catalogue.py`: one ordinary top-level import of
first-party code per capability, fixed when the image was built. There is no import machinery
left in this file -- `_select_provider` is a dictionary lookup, and an unknown name is a
refusal that lists the known ones rather than an import attempt. The set of modules a
configured deployment can execute is the set a reviewer can read out of the source, which is
the property `MOS-REL-108` is actually asking for. What the seam still buys is unchanged: a
deployment decides which shipped capabilities it serves, without a rebuild and without a code
change.

THE CLAUSE'S THIRD LIMB, ANSWERED RATHER THAN STEPPED AROUND. `MOS-REL-108` also forbids
"user-supplied code executed inside a platform process", and a capability under
`medos/services/` does execute in the worker. What makes that lawful is that it is not
user-supplied: it is first-party code the image owner compiled in, and no operator,
configuration value or run-time input can add to the set. An operator turns a shipped
capability ON and OFF; introducing one is an edit to `medos/services/catalogue.py` and a
rebuild, which is a code review and a supply chain. Anything a THIRD PARTY supplies is
`MOS-REL-107`'s OCI image executed out-of-process over chapter 2's versioned service contract,
and nothing in this file is a route to it.

WHY THE CATALOGUE IS NOT IN THIS PACKAGE. Section 15.1.2's `zero-core-change` gate row
requires the diff that introduces a capability to touch only `medos/services/`,
`medos/schemas/`, `medos/examples/` and registry rows. A catalogue under `medos/medos/` would
make capability number three a core edit and falsify 0.3.0's platform claim at the first use.
So the catalogue is `medos/services/catalogue.py`, and the import of it in
`_shipped_catalogue()` is FUNCTION-LOCAL for two reasons that are not style: the platform must
stay importable in an installation that ships no Service Plane at all (`pyproject.toml`
packages `medos*` and not `services*`), and an unconfigured process must not acquire a second
way to fail at startup over code it was never going to compose. The name imported is a literal
either way.

ONE RESOLVER, AND WHY THAT IS THE WHOLE POINT
-----------------------------------------------
The failure mode of two composition paths is not "duplicated code". It is that the API
accepts a job the worker cannot run: the submit succeeds, the job queues, the worker
claims it, `resolve_capability_order` raises `unknown capability_id`, and the job FAILS
with an internal error for a study that was never the problem. A job that is going to be
refused must be refused at submit, in the same breath and from the same answer.

So `resolve()` is the only composition in the tree. `known_capability_ids()` is
`frozenset(resolve().registry)` and `WorkerDeps`'s defaults are `resolve().registry` and
`resolve().concepts`. Neither side owns a fallback list, and neither side can drift.

FAIL CLOSED AND LOUD -- THE FOUR REFUSALS
-------------------------------------------
A configured provider is an operator's statement that this deployment runs that
capability. Every way that statement can be false is a deployment defect and is refused by
name, because the alternative -- quietly serving the platform three -- means a deployment
believes it is running a capability it is not, and a clinician reads a worklist that does
not say so.

  R1. The configured name is not one this image ships. (Previously "the provider does not
      import", which is the SAME deployment defect -- a typo, or a capability not in this
      image -- reached through a mechanism `MOS-REL-108` forbids. The refusal now lists
      what the catalogue holds, which the old one could not do, because a failed import
      knows nothing about what would have succeeded.)
  R2. The provider exposes no `worker_registry` (or it is not callable, or it raises, or
      it returns something that is not a `Mapping` of `Capability`).
  R3. The provider REPLACES a capability it did not supply -- a platform one, or one an
      earlier provider added. A provider may ADD and may do nothing else. Silently
      shadowing `lung_segmentation` with a third-party implementation is a supply-chain
      hole: every downstream `Result` would carry a provenance block naming a capability
      whose code never ran.
  R4. The provider adds NOTHING. A configured provider that contributes no capability is
      the same lie as a silent fallback, arrived at from the other direction.

Each refusal is a `CapabilityProviderError` naming the provider and what it did. None of
them degrade to the defaults.

And one refusal on the CATALOGUE rather than on a provider: a key that is not a selector
token. `configured_providers()` splits the variable on commas and whitespace, so a key
holding either could be shipped, catalogued, and never selectable -- a capability that is in
the image and unreachable, which is the 0.3.0 defect above in miniature and is exactly the
class of defect nobody notices, because the configuration that would have exposed it simply
does nothing.

THE PLATFORM SINGLETON IS READ-ONLY
-------------------------------------
`medos.capabilities.REGISTRY` is READ and never written. `resolve()` returns a NEW mapping
(a `MappingProxyType` over a fresh dict, so a consumer cannot mutate it either).
`tests/unit/test_capabilities.py` and `test_lung_nodule.py::
test_the_platform_registry_is_untouched` both assert the singleton still holds exactly its
three keys at runtime, and this module is written so that stays true no matter how many
providers are configured. CONTRACT.md section 11: "No global mutable state."

WHAT THE SPECIFICATION SAYS ABOUT THIS -- REPORTED, NOT INVENTED
------------------------------------------------------------------
Nothing. Chapter 2 section 2.9.3 defines registration as `POST /api/v1/service-versions`
with an OCI reference (`MOS-SVC-107`), and chapter 6 section 6.8 makes the `deployment`
table "the only entity that determines whether a version receives work" (`MOS-REG-072`).
Neither exists in this build -- CONTRACT.md section 0 removes chapter 6's registry from the
slice -- and NEITHER CHAPTER SAYS WHAT A DEPLOYMENT DOES IN THE MEANTIME. There is no
requirement id for "how a running API and a running worker agree on the set of capability
ids this deployment serves", and `MOS-REG-093` points the other way ("no in-process plugin
loading") for the PROPRIETARY case this module does not serve.

That gap is reported in `docs/spec/99-known-inconsistencies.md` rather than papered over
with an invented id. This module is the stand-in and it is scoped to say so: it composes
first-party Python that the deployment already trusts and already ships in its image; it is
not an artifact registry, it verifies no signature, and it must be REPLACED by chapter 6's
`deployment` rows, not extended, when those land.

WHAT THE SPECIFICATION DOES SAY IS HOW THE STAND-IN MAY BE BUILT, and that half is not a
gap at all: `MOS-REL-107` makes a third-party extension an OCI image executed
out-of-process, `MOS-REL-108` forbids an in-process plugin API without qualification, and
`MOS-REG-093` forbids in-process loading for a proprietary sealed service specifically. A
missing requirement for deployment-time registration is not a licence to build the one
mechanism three requirements rule out, which is how the previous version of this file came
to exist. This one is a lookup in a statically composed table, and it MUST NOT become the
installation path for a sealed third-party service.

Spec: MOS-REL-108, MOS-REL-107, MOS-CONF-109 (the bound on HOW this may work),
MOS-SVC-107, MOS-REG-072, MOS-REG-093 (what this is NOT), MOS-REL-020, MOS-SVC-002,
MOS-IMG-111, CONTRACT.md sections 6 and 11.
"""

from __future__ import annotations

import inspect
import os
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, ModuleType

from medos.capabilities import REGISTRY as PLATFORM_REGISTRY
from medos.capabilities import Capability, load_concepts
from medos.core.concepts import ConceptDictionary

__all__ = [
    "ENV_PROVIDERS",
    "ENV_REGISTRY_DIR",
    "REGISTRY_FUNCTION",
    "CONCEPTS_FUNCTION",
    "CapabilityProviderError",
    "ResolvedCapabilities",
    "ServiceCatalogue",
    "configured_providers",
    "default_registry_dir",
    "service_catalogue",
    "known_providers",
    "resolve",
    "capability_ids",
    "clear_cache",
]

#: The ONE configuration value. A whitespace- or comma-separated list of SELECTOR NAMES --
#: keys of `medos/services/catalogue.py`'s `CATALOGUE` -- in the order they are composed. Set
#: identically on every process of a deployment: `medos/deploy/compose/docker-compose.yml`
#: sets it on `medos-api` and `medos-worker` from a single `${MEDOS_CAPABILITY_PROVIDERS}` so
#: the two CANNOT be given different values by a hand edit to one block.
#:
#: A NAME, NEVER A MODULE PATH. The previous form was a dotted path handed to
#: `importlib.import_module()`, which `MOS-REL-108` forbids without qualification and which
#: `MOS-CONF-109` then cites as executable segregation evidence. An unknown name here is a
#: refusal listing the known ones; nothing in this module will try to import it.
ENV_PROVIDERS = "MEDOS_CAPABILITY_PROVIDERS"

#: Where a provider's composed coded-concept dictionary is written. A BUILD ARTEFACT
#: directory, not a data store: it is rewritten from the overlay on every resolution and
#: nothing reads it that did not just write it. Defaults under the system temp directory so
#: an unconfigured process needs no volume.
ENV_REGISTRY_DIR = "MEDOS_CAPABILITY_REGISTRY_DIR"

#: The provider contract. Two names, both at module level.
#:
#:     def worker_registry(
#:         concepts: ConceptDictionary,
#:         base: Mapping[str, Capability] | None = None,
#:     ) -> Mapping[str, Capability]
#:
#:         `base` plus this provider's own entries, as a NEW mapping. REQUIRED.
#:
#:     def worker_concepts(
#:         work_dir: Path, base: Path | None = None
#:     ) -> ConceptDictionary
#:
#:         the dictionary at `base` with this provider's coded-concept rows merged in,
#:         composed into `work_dir`. OPTIONAL -- a provider whose capability emits only
#:         codes the platform dictionary already owns does not need one.
#:
#: `worker_registry(base)` is not a new shape invented here: it is exactly the signature
#: `medos/services/lung_nodule/registry.py` already exposed and that nothing called.
REGISTRY_FUNCTION = "worker_registry"
CONCEPTS_FUNCTION = "worker_concepts"

#: What `medos/services/catalogue.py` hands back: selector name -> the module that supplies it.
#: A `Mapping` and not a `dict`, because the catalogue is a `MappingProxyType` and nothing
#: here has any business writing to it.
ServiceCatalogue = Mapping[str, ModuleType]

_SPLIT = re.compile(r"[,\s]+")

#: The shape of a catalogue key, and therefore of a value an operator can write in
#: `ENV_PROVIDERS`. Enforced on the catalogue rather than on the variable: the variable is
#: checked against the catalogue anyway, so the only thing a loose key can produce is an
#: entry that is impossible to select.
_SELECTOR = re.compile(r"[a-z][a-z0-9_]*")


class CapabilityProviderError(RuntimeError):
    """A configured capability provider is a deployment defect. Refuse to start.

    NEVER caught and turned into "use the platform defaults". That is the one outcome this
    whole module exists to prevent: an operator who configured a capability and got a
    silently smaller deployment has no signal at all, and the first evidence is a clinical
    worklist missing findings nobody knows to look for.
    """


@dataclass(frozen=True)
class ResolvedCapabilities:
    """What this deployment serves. The same object on both sides of the wire.

    `concepts` is `None` when no provider contributed coded-concept rows, which means "the
    platform dictionary is unchanged -- load it the ordinary way". It is NOT an error and
    it is NOT an empty dictionary: making this module load the platform's concepts for an
    unconfigured process would put a second concept-loading decision in the tree, and
    `medos.worker.steps.resolve_concepts()` already owns that one.
    """

    registry: Mapping[str, Capability]
    concepts: ConceptDictionary | None
    providers: tuple[str, ...]
    #: capability_id -> the SELECTOR NAME that supplied it. Empty for a stock deployment.
    #: Recorded rather than assumed equal to the selector, because one catalogue entry may
    #: register more than one capability.
    supplied_by: Mapping[str, str]

    @property
    def capability_ids(self) -> frozenset[str]:
        return frozenset(self.registry)


# =====================================================================================
# Configuration
# =====================================================================================
def configured_providers(spec: str | None = None) -> tuple[str, ...]:
    """The provider names this deployment declares, in composition order.

    Names, not module paths: each one must be a key of `service_catalogue()` and is checked
    against it by `resolve()`. This function does no validation of its own -- an unknown
    name is a refusal that lists the known ones, and only the catalogue knows those.

    Read from the environment at CALL time, not at import time, for the same reason
    `known_capability_ids()` always resolved at call time: a process must answer with the
    configuration it has, not the one it started with. Duplicates are collapsed keeping
    first position -- naming a provider twice is a typo, not a request to compose it
    twice, and composing it twice would trip R3 against its own first pass.
    """
    raw = os.environ.get(ENV_PROVIDERS, "") if spec is None else spec
    return tuple(dict.fromkeys(p for p in _SPLIT.split(raw.strip()) if p))


def default_registry_dir() -> Path:
    override = os.environ.get(ENV_REGISTRY_DIR)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "medos-capability-registry"


# =====================================================================================
# The catalogue -- where `MOS-REL-108` is satisfied, by one literal import and a lookup
# =====================================================================================
def service_catalogue() -> ServiceCatalogue:
    """The names this image can be asked to serve, and the module behind each one.

    Two steps, split so that each can be exercised on its own: what this image SHIPS, and
    whether that is a catalogue an operator could actually select from.

    NOT MEMOISED. `sys.modules` already is; what repeats per call is one regex per catalogue
    entry, and a memo here would be process state holding a mapping that `resolve()`'s memo
    already keys on the configuration.
    """
    return _checked_catalogue(_shipped_catalogue())


def _shipped_catalogue() -> ServiceCatalogue:
    """THE ONLY IMPORT OF THE SERVICE PLANE THE PLATFORM PERFORMS, AND IT IS A LITERAL.

    `services.catalogue` is spelled in the source, not read from anywhere, so the set of code
    a configured deployment can execute inside a platform process is decided when the image
    is built and is visible to `grep`. `MOS-CONF-109` invites a reviewer to EXECUTE the
    `MOS-REL-108` segregation argument rather than read it; this is the line they land on,
    and what it does is one import of a first-party module followed by a dictionary lookup.

    FUNCTION-LOCAL, for two reasons that are not style. `pyproject.toml` packages `medos*`
    and not `services*` -- the Service Plane is beside the platform, not inside it -- so a
    top-level import here would make `medos` un-importable in an installation that ships no
    services at all. And an unconfigured process must not acquire a second way to fail at
    startup over a Service Plane it was never going to compose, which is why `_compose`
    calls this only once at least one provider is configured.

    This is also the seam `tests/integration/test_capability_providers.py` substitutes to
    stand in a catalogue this repository does not ship. Deliberately THIS function and not
    `service_catalogue()`: what a test has any business replacing is which modules the image
    happens to hold, and everything downstream of that -- the key check, the lookup, the
    provider contract, the concept chaining, the memo -- stays real.
    """
    try:
        from services.catalogue import CATALOGUE
    except Exception as exc:  # noqa: BLE001 -- every failure here is the same defect
        raise CapabilityProviderError(
            f"the capability catalogue `medos/services/catalogue.py` did not import: "
            f"{type(exc).__name__}: {exc}\n"
            f"  {ENV_PROVIDERS} names at least one provider, so this deployment declares "
            f"that it serves Service Plane capabilities, and the catalogue is the only "
            f"statement of which ones this image holds. Refusing to start is the only "
            f"honest outcome -- falling back to {sorted(PLATFORM_REGISTRY)} would leave a "
            f"deployment believing it runs a capability it does not.\n"
            f"  Check that `medos/services/` is on this process's import path "
            f"(the Service Plane "
            f"is not part of the `medos` distribution; medos/deploy/compose/Dockerfile puts it "
            f"there with PYTHONPATH=/app) and that every package the catalogue imports "
            f"imports cleanly. The catalogue imports them all at module scope on purpose: "
            f"a service package that is in the image and broken is a broken image, and an "
            f"image says so at startup rather than on the first job that selects it."
        ) from exc
    return CATALOGUE


def known_providers() -> tuple[str, ...]:
    """Every name `ENV_PROVIDERS` may hold, sorted. What a refusal lists."""
    return tuple(sorted(service_catalogue()))


def _checked_catalogue(catalogue: ServiceCatalogue) -> ServiceCatalogue:
    """Every key must be a name an operator can actually write in the environment.

    `configured_providers()` splits `ENV_PROVIDERS` on commas and whitespace, so a catalogue
    key holding either could be listed, shipped, and never selected: the capability would be
    in the image, in the catalogue, and unreachable -- which is the defect this module was
    written to close, arrived at from inside. It is also the quietest possible version of
    it, because the configuration that would expose it just silently does nothing.

    The token shape carries one more thing: `_provider_concepts` composes into
    `work_dir / name`, and a key restricted to `[a-z][a-z0-9_]*` is a directory name rather
    than a path expression.
    """
    bad = sorted(
        repr(key)
        for key in catalogue
        if not (isinstance(key, str) and _SELECTOR.fullmatch(key))
    )
    if not bad:
        return catalogue
    raise CapabilityProviderError(
        f"the capability catalogue `medos/services/catalogue.py` is keyed by {', '.join(bad)}, "
        f"which does not match the selector shape `{_SELECTOR.pattern}`.\n"
        f"  {ENV_PROVIDERS} is split on commas and whitespace, so a key holding either can "
        f"never be selected: the capability would ship in this image, appear in the "
        f"catalogue, and be unreachable by any configuration -- with nothing anywhere "
        f"saying so, because the value that would have named it simply matches nothing.\n"
        f"  The name is also the directory the provider's composed coded-concept dictionary "
        f"is written to under {ENV_REGISTRY_DIR}."
    )


# =====================================================================================
# The resolver
# =====================================================================================
_CACHE: dict[tuple[tuple[str, ...], str], ResolvedCapabilities] = {}
_CACHE_LOCK = threading.Lock()


def clear_cache() -> None:
    """Drop the memoised resolutions. For tests that move the configuration under a process."""
    with _CACHE_LOCK:
        _CACHE.clear()


def resolve(
    *,
    providers: Sequence[str] | None = None,
    registry_dir: Path | None = None,
    base: Mapping[str, Capability] | None = None,
    base_concepts: ConceptDictionary | None = None,
) -> ResolvedCapabilities:
    """Compose the platform registry with every configured provider. The ONE answer.

    Memoised on `(providers, registry_dir)` when called with no explicit `base` -- which is
    every production call. The memo is not a cache of "what the process started with": the
    key CONTAINS the configuration, so changing `MEDOS_CAPABILITY_PROVIDERS` produces a
    different key and a fresh resolution, and a failure is never memoised at all. What it
    buys is that `known_capability_ids()` does not re-read and re-compose a JSON
    dictionary on every `POST /api/v1/jobs`.
    """
    names = tuple(providers) if providers is not None else configured_providers()
    explicit = base is not None or base_concepts is not None
    work_dir = registry_dir if registry_dir is not None else default_registry_dir()

    if not explicit:
        key = (names, str(work_dir))
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
        if hit is not None:
            return hit

    resolved = _compose(
        names,
        work_dir,
        base if base is not None else PLATFORM_REGISTRY,
        base_concepts,
    )

    if not explicit:
        with _CACHE_LOCK:
            _CACHE[(names, str(work_dir))] = resolved
    return resolved


def capability_ids(
    *, providers: Sequence[str] | None = None, registry_dir: Path | None = None
) -> frozenset[str]:
    """The capability ids this deployment serves. What the API admits at submit."""
    return resolve(providers=providers, registry_dir=registry_dir).capability_ids


def _compose(
    names: tuple[str, ...],
    work_dir: Path,
    base: Mapping[str, Capability],
    base_concepts: ConceptDictionary | None,
) -> ResolvedCapabilities:
    # `dict(base)` and never `base` itself. Every subsequent step works on this copy, so
    # `medos.capabilities.REGISTRY` is read once and is never the object a provider is
    # handed a reference to mutate.
    registry: dict[str, Capability] = dict(base)
    concepts = base_concepts
    supplied_by: dict[str, str] = {}

    if not names:
        return ResolvedCapabilities(
            registry=MappingProxyType(registry),
            concepts=concepts,
            providers=(),
            supplied_by=MappingProxyType(supplied_by),
        )

    # THE CATALOGUE FIRST, and every configured name resolved against it BEFORE any
    # provider runs. Two orderings, both deliberate:
    #
    #   * ahead of `load_concepts()`, for the reason `WorkerRunner` resolves ahead of
    #     `connect()`: a configuration defect must not be reported as a problem with
    #     something else. A misspelled selector surfacing as "code dictionary not found"
    #     names the wrong thing entirely, which is the failure that ordering was measured
    #     to fix in the worker and is the same failure here.
    #   * ahead of the composition loop, so a deployment whose SECOND selector is a typo
    #     does not first compose the first provider's concept dictionary onto disk and then
    #     refuse. Work done on the way to a refusal leaves a build artefact behind for a
    #     process that is not going to start.
    catalogue = service_catalogue()
    selected = tuple((name, _select_provider(name, catalogue)) for name in names)

    # Loaded ONLY when a provider exists, and only once. An unconfigured process must not
    # pay a file read here, and -- more importantly -- must not acquire a second reason to
    # fail at startup over a concept dictionary it was never going to compose.
    if concepts is None:
        concepts = load_concepts()

    for name, module in selected:
        concepts = _provider_concepts(name, module, work_dir, concepts)
        registry, added = _provider_registry(name, module, registry, concepts)
        for cid in added:
            supplied_by[cid] = name

    return ResolvedCapabilities(
        registry=MappingProxyType(registry),
        concepts=concepts,
        providers=names,
        supplied_by=MappingProxyType(supplied_by),
    )


# =====================================================================================
# R1 -- the name is one this image ships
# =====================================================================================
def _select_provider(name: str, catalogue: ServiceCatalogue) -> ModuleType:
    """A LOOKUP, not an import. That sentence is the whole of `MOS-REL-108` here.

    The refusal below can list what this image holds, which the import-based version could
    not: a failed `import_module()` knows nothing about what would have succeeded, so an
    operator with a typo got a `ModuleNotFoundError` and no way to see the right spelling
    short of reading the tree.
    """
    module = catalogue.get(name)
    if module is not None:
        return module

    known = sorted(catalogue)
    raise CapabilityProviderError(
        f"capability provider {name!r} is not one this image ships. It ships {known}.\n"
        f"  {ENV_PROVIDERS} SELECTS among the names in `medos/services/catalogue.py`; it does "
        f"not name a module to import. `MOS-REL-108` forbids a dynamic module import "
        f"inside a platform process without qualification, and `MOS-CONF-109` cites that "
        f"clause as IEC 62304 section 4.3 segregation evidence a reviewer is invited to "
        f"execute, so an importlib call over this variable would be the very mechanism "
        f"that argument denies exists.\n"
        f"{_dotted_path_hint(name, known)}"
        f"  {ENV_PROVIDERS} names it, so this deployment declares that it serves that "
        f"provider's capabilities. It does not, and refusing to start is the only honest "
        f"outcome -- falling back to {sorted(PLATFORM_REGISTRY)} would leave a deployment "
        f"believing it runs a capability it does not.\n"
        f"  A capability that is in the source tree but not in the catalogue is not "
        f"deployable by configuration at all. Adding it is a line in "
        f"`medos/services/catalogue.py` and a rebuild -- a code review and a supply chain, "
        f"which is the difference between configuration and a plugin API."
    )


def _dotted_path_hint(name: str, known: Sequence[str]) -> str:
    """Name the migration, because this variable used to hold dotted module paths.

    An operator upgrading a running deployment has the old value sitting in a compose file
    or a secret store, and "not one this image ships" on its own reads as "the capability
    was removed" -- the wrong conclusion, and the wrong remedy after it.
    """
    if "." not in name:
        return ""
    segments = set(name.split("."))
    suggestion = next((candidate for candidate in known if candidate in segments), None)
    return (
        f"  {name!r} is the dotted module path this variable used to take. That form went "
        f"with the importlib call that consumed it"
        + (f"; the selector for it is {suggestion!r}.\n" if suggestion else ".\n")
    )


# =====================================================================================
# R2/R3/R4 -- the provider composes
# =====================================================================================
def _provider_registry(
    name: str,
    module: ModuleType,
    incoming: Mapping[str, Capability],
    concepts: ConceptDictionary,
) -> tuple[dict[str, Capability], tuple[str, ...]]:
    """Call `name.worker_registry(concepts, base=incoming)` and hold it to the contract."""
    fn = getattr(module, REGISTRY_FUNCTION, None)
    if not callable(fn):
        raise CapabilityProviderError(
            f"capability provider {name!r} exposes no callable {REGISTRY_FUNCTION!r}.\n"
            f"  The provider contract is one module-level function:\n"
            f"      def {REGISTRY_FUNCTION}(concepts, base=None) -> Mapping[str, Capability]\n"
            f"  returning `base` plus this provider's own entries, as a NEW mapping."
        )

    # SNAPSHOT BEFORE THE CALL, and compare everything below against the snapshot.
    # `incoming` is handed to the provider, and a provider that writes into it and returns
    # it -- `base[cid] = Mine(); return base` -- would otherwise make every check vacuous:
    # the "before" and the "after" would be the same object, so a shadowed
    # `lung_segmentation` would read as no change at all. The snapshot is what makes R3 a
    # comparison rather than a tautology.
    before: dict[str, Capability] = dict(incoming)

    try:
        produced = fn(concepts, base=incoming)
    except CapabilityProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CapabilityProviderError(
            f"capability provider {name!r}.{REGISTRY_FUNCTION}() raised: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(produced, Mapping):
        raise CapabilityProviderError(
            f"capability provider {name!r}.{REGISTRY_FUNCTION}() returned "
            f"{type(produced).__name__}, not a Mapping[str, Capability]. The worker "
            f"dispatches with `registry[capability_id]` and the API admits "
            f"`frozenset(registry)`; neither is meaningful over that object."
        )

    _reject_replacement(name, before, produced)
    # Insertion order, NOT sorted: the keys have not been validated as strings yet, and
    # sorting a mapping that mixed a str key with an int one would raise a TypeError here
    # instead of the message below naming the provider that did it.
    added = tuple(cid for cid in produced if cid not in before)
    _reject_a_provider_that_adds_nothing(name, added)
    for cid in added:
        _reject_a_value_that_is_not_a_capability(name, cid, produced[cid])

    return dict(produced), added


def _reject_replacement(
    name: str, incoming: Mapping[str, Capability], produced: Mapping[str, object]
) -> None:
    """R3. A provider may ADD. It may not replace, and it may not drop.

    Identity (`is`), not equality: two `Capability` instances of the same frozen dataclass
    compare equal, so `==` would wave through a provider that reconstructed
    `LungSegmentation()` with different parameters. What has to be true is that the object
    the platform put in the mapping is the object still in it.
    """
    dropped = sorted(cid for cid in incoming if cid not in produced)
    if dropped:
        raise CapabilityProviderError(
            f"capability provider {name!r}.{REGISTRY_FUNCTION}() DROPPED {dropped} from "
            f"the mapping it was given. A provider composes `base` plus its own entries; "
            f"removing one silently disables a capability this deployment still advertises."
        )

    replaced = sorted(cid for cid in incoming if produced[cid] is not incoming[cid])
    if replaced:
        platform = [cid for cid in replaced if cid in PLATFORM_REGISTRY]
        raise CapabilityProviderError(
            f"capability provider {name!r} tried to REPLACE {replaced}"
            + (f" (platform capabilit{'y' if len(platform) == 1 else 'ies'}: {platform})"
               if platform else "")
            + ".\n"
            "  A provider may ADD a capability id and nothing else. An id that already "
            "resolves is already producing Results, and shadowing it means every "
            "provenance block, every SEG `model_version` and every SR written afterwards "
            "names an implementation whose code did not run. Rename the capability.\n"
            "  This is a name collision, not an override: there is no precedence rule to "
            "appeal to, because MOS-REG-051 makes two implementations behind one "
            "capability id a resolution ambiguity."
        )


def _reject_a_provider_that_adds_nothing(name: str, added: tuple[str, ...]) -> None:
    """R4. A configured provider that contributes nothing is the silent fallback again."""
    if not added:
        raise CapabilityProviderError(
            f"capability provider {name!r}.{REGISTRY_FUNCTION}() added no capability. "
            f"{ENV_PROVIDERS} names it, which is this deployment stating that it serves "
            f"something that provider supplies; it supplies nothing, so the statement is "
            f"false. Remove it from {ENV_PROVIDERS}, or fix the provider."
        )


def _reject_a_value_that_is_not_a_capability(name: str, cid: str, value: object) -> None:
    """R2, on the values. CONTRACT.md section 6 is the whole bar and it is structural."""
    if not isinstance(cid, str) or not cid:
        raise CapabilityProviderError(
            f"capability provider {name!r} added the key {cid!r}, which is not a "
            f"non-empty capability id string."
        )
    if not isinstance(value, Capability):
        missing = [
            attr
            for attr in ("capability_id", "version", "applicable", "run")
            if not hasattr(value, attr)
        ]
        raise CapabilityProviderError(
            f"capability provider {name!r} registered {cid!r} as "
            f"{type(value).__name__}, which does not satisfy "
            f"`medos.capabilities.Capability` (CONTRACT.md section 6); it is missing "
            f"{missing}. The worker calls `applicable()` then `run()` on whatever this "
            f"mapping holds."
        )
    declared = getattr(value, "capability_id", None)
    if declared != cid:
        raise CapabilityProviderError(
            f"capability provider {name!r} registered {cid!r} under a capability that "
            f"declares `capability_id = {declared!r}`. The worker dispatches by the KEY "
            f"and every Result row records the DECLARED id, so the two disagreeing means "
            f"a job for {cid!r} produces evidence attributed to {declared!r}."
        )


# =====================================================================================
# The coded-concept half
# =====================================================================================
def _provider_concepts(
    name: str, module: ModuleType, work_dir: Path, base: ConceptDictionary
) -> ConceptDictionary:
    """Chain this provider's concept overlay onto the dictionary composed so far.

    CHAINED and not independent: `medos/services/lung_nodule/concepts.py` records that "a
    deployment that wants two independently-shipped capabilities must compose both overlays
    itself", and two providers each composing base+own would leave the second dictionary
    missing the first's rows -- the second capability would then resolve codes out of a
    table that does not contain the first's, which is `MOS-REG-042`'s forbidden second code
    table arrived at by accident. So each provider is handed the PREVIOUS composition as
    its base and the result is one dictionary containing everything.

    Each provider composes into its own subdirectory, so two providers cannot overwrite one
    another's build artefact.
    """
    fn = getattr(module, CONCEPTS_FUNCTION, None)
    if fn is None:
        return base
    if not callable(fn):
        raise CapabilityProviderError(
            f"capability provider {name!r} exposes {CONCEPTS_FUNCTION!r} as "
            f"{type(fn).__name__}, which is not callable. Omit it entirely if the "
            f"capability needs no coded-concept rows of its own."
        )

    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError) as exc:  # pragma: no cover - exotic callables
        raise CapabilityProviderError(
            f"capability provider {name!r}.{CONCEPTS_FUNCTION} is not introspectable "
            f"({exc}); the contract is `{CONCEPTS_FUNCTION}(work_dir, base=None)`."
        ) from exc
    if "base" not in parameters:
        raise CapabilityProviderError(
            f"capability provider {name!r}.{CONCEPTS_FUNCTION}() takes "
            f"{sorted(parameters)} and no `base`.\n"
            f"  The contract is `{CONCEPTS_FUNCTION}(work_dir, base=None)` where `base` is "
            f"the path of the dictionary composed so far. Without it two providers cannot "
            f"be chained, and the second would resolve its codes out of a table missing "
            f"the first's rows (MOS-REG-042)."
        )

    # The selector is a single lower-case token (`_checked_catalogue`), so this is a
    # directory NAME rather than a path expression that happens to be read from a mapping.
    destination = work_dir / name
    try:
        composed = fn(destination, base=base.path)
    except CapabilityProviderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CapabilityProviderError(
            f"capability provider {name!r}.{CONCEPTS_FUNCTION}() raised: "
            f"{type(exc).__name__}: {exc}\n"
            f"  It was asked to compose its coded-concept rows onto {base.path} into "
            f"{destination}."
        ) from exc

    if not isinstance(composed, ConceptDictionary):
        raise CapabilityProviderError(
            f"capability provider {name!r}.{CONCEPTS_FUNCTION}() returned "
            f"{type(composed).__name__}, not a "
            f"medos.core.concepts.ConceptDictionary."
        )
    return composed
