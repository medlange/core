# SPDX-License-Identifier: Apache-2.0
"""The injection seam: how a deployment gets `lung_nodule` in front of the worker.

THIS FILE IS THE WHOLE ANSWER TO `MOS-REL-020`
------------------------------------------------
`medos.worker.steps.WorkerDeps` declares

    registry: Mapping[str, Capability] = field(default_factory=deployment_registry)
    concepts: ConceptDictionary        = field(default_factory=deployment_concepts)

and says why: "Injected rather than imported so that a test can substitute a fake PACS, and
so that the capability registry stays a parameter -- CONTRACT.md section 11: 'No global
mutable state. Pass the connection; do not import a singleton.'"

That is the seam. (Those two defaults used to read `medos.capabilities.REGISTRY` and the
platform concept file directly, which made the seam reachable only from a test; they now
route through `medos.capabilities.providers.resolve()`, so a deployment reaches it by
configuration. See below.) A deployment that ships this capability gets its `WorkerDeps`
through `worker_registry()` and `worker_concepts()` below instead of the platform default.
`medos.capabilities.REGISTRY` is READ and never written -- mutating it would be a global
side effect on every other consumer in the process, including `medos.training.runs`, and
`tests/unit/test_capabilities.py` asserts its three keys.

THIS MODULE IS NOW A PROVIDER, AND THE PLATFORM CALLS IT
---------------------------------------------------------
The two functions below are exactly `medos.capabilities.providers`' provider contract, and
they always were -- the contract was written to the shape this file already had. A
deployment names this module's SELECTOR -- `lung_nodule`, the key `medos/services/catalogue.py`
binds to this module -- in `MEDOS_CAPABILITY_PROVIDERS`, and both `medos-api` and
`medos-worker` resolve it through that one function. The variable holds a catalogue key and
not this module's dotted path, because `MOS-REL-108` forbids dynamic module import inside a
platform process and `MOS-CONF-109` rests the IEC 62304 segregation argument on that rule:
`known_capability_ids()` admits `lung_nodule` at submit and `WorkerDeps.registry` contains
it at execute, from the same value, so the two cannot disagree.

WHAT CHANGED, AND WHAT DID NOT. What changed is entirely on the platform side: an
entry-point loader that turns a config value into a `Mapping[str, Capability]`. This file
gained one optional `base` parameter on `worker_concepts` so that two providers can be
chained onto one dictionary. Nothing else here moved, and nothing here registers itself:
this module is IMPORTED and CALLED by the platform, it does not reach into the platform.

WHAT IT STILL DOES NOT REACH, STATED PLAINLY. There is no discovery, no manifest
verification and no signature check: a provider is trusted Python that the deployment
already ships in its image. Chapter 2 section 2.9.3's OCI registration (`MOS-SVC-107`) and
chapter 6 section 6.8's `deployment` rows (`MOS-REG-072`) are what a real third-party
service goes through, and `MOS-REG-093` explicitly rules out in-process plugin loading for
the PROPRIETARY case. Neither of those exists in this build. The resolver is a stand-in for
them and is scoped to say so.

Two places on the execution path did NOT read the injected mapping. Both were recorded in
this component's report; the first has since been fixed in core:
  * `medos.worker.steps.step_write_dicom` resolved `ctx.deps.registry["lung_segmentation"]`
    by NAME to fill the SEG/SR `model_version`. FIXED: `steps.producing_model_version` now
    reads the version off the capability that produced the label map being written, so a
    registry of exactly one is sufficient and the version in the object is the version that
    made it. (A core edit, made after 0.3.0 banked its `zero-core-change` measurement;
    nothing in this package moved for it.)
  * `medos.capabilities.metadata_for` and `execution_order` read the module global, so
    `medos.training.runs` could not see an injected capability's metadata -- and it failed
    SOFT, returning None, so a training run lost this capability's output kind and said
    nothing. CLOSED: both now resolve against what the deployment serves, with the mapping
    injectable so a caller holding one (the worker holds `ctx.deps.registry`) passes it
    rather than resolving twice.

Spec: MOS-REL-020, MOS-SVC-002, CONTRACT.md section 11.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from medos.capabilities import REGISTRY as PLATFORM_REGISTRY
from medos.capabilities import Capability
from medos.core.concepts import ConceptDictionary
from services.lung_nodule import concepts as overlay
from services.lung_nodule.service import CAPABILITY_ID, LungNodule

__all__ = ["CAPABILITY_ID", "worker_concepts", "worker_registry"]


def worker_concepts(work_dir: Path, base: Path | None = None) -> ConceptDictionary:
    """The coded-concept dictionary with this capability's rows registered.

    `work_dir` is where the composed dictionary is written. A deployment would point this
    at a directory in its image; a test points it at `tmp_path`.

    `base` is the dictionary to merge ONTO, and it is how the platform resolver chains
    providers: provider N is handed provider N-1's composition, so a deployment running two
    independently-shipped capabilities ends with ONE dictionary containing both overlays
    rather than two dictionaries each missing the other's rows -- `MOS-REG-042`'s forbidden
    second code table, arrived at by accident. `None` means the platform's own dictionary
    (`default_concepts_path()`), which is what a single-provider deployment and every test
    that calls this directly get.
    """
    return ConceptDictionary(
        overlay.compose_to(work_dir / "capability_concepts.json", base=base)
    )


def worker_registry(
    concepts: ConceptDictionary,
    base: Mapping[str, Capability] | None = None,
) -> dict[str, Capability]:
    """`base` plus `lung_nodule`. A NEW mapping; nothing is mutated.

    The capability is constructed with the composed dictionary rather than its own default,
    so the whole registry resolves codes out of one object. Two capabilities holding
    different dictionaries is the "second code table" `MOS-REG-042` forbids, arrived at by
    accident.
    """
    registry = dict(PLATFORM_REGISTRY if base is None else base)
    if CAPABILITY_ID in registry:
        raise ValueError(
            f"{CAPABILITY_ID!r} is already registered. Two implementations behind one "
            "capability id is a resolution ambiguity, not an override (MOS-REG-051)."
        )
    registry[CAPABILITY_ID] = LungNodule(concepts=concepts)
    return registry
