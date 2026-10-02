# SPDX-License-Identifier: Apache-2.0
"""The capability registry.

CONTRACT.md §1: "`capabilities/__init__.py`  # REGISTRY: dict[str, Capability]".
CONTRACT.md §6: "A capability MUST NOT touch the database, the network, or the PACS."

WHAT IS IN THE REGISTRY AND WHAT IT IS NOT
    This is NOT chapter 6's registry. There is no `Capability` row, no `ServiceVersion`,
    no resolution and no deployment in this slice (CONTRACT.md §0) -- resolution here is
    `REGISTRY[capability_id]` and nothing more. Weeks 3-5 replaces the dictionary with a
    real lookup; the shape of what it returns is what has to survive, which is why the
    values are instances satisfying the §6 protocol rather than modules or factories.

    Every value is a FROZEN dataclass with no per-job state, so one registry instance is
    safe to share across concurrent jobs (CONTRACT.md §11: no global mutable state).
    `emphysema_laa` carries its per-job dependency through `bind()`, which returns a new
    instance -- the registry entry is never mutated.

EXECUTION ORDER
    CONTRACT.md §7 makes `emphysema_laa`'s dependency on `lung_segmentation` an explicit
    step ordering in `worker/steps.py`. `execution_order()` computes that order from the
    `depends_on` declarations so the worker does not hard-code it, and raises on a cycle
    or an undeclared dependency rather than picking an order that happens to work.

    THE LOOP THE WORKER IS EXPECTED TO WRITE, exactly:

        produced: dict[str, CapabilityOutcome] = {}
        attempted: list[str] = []
        for capability_id in execution_order(job.capability_ids):
            try:
                capability = bind_dependencies(
                    REGISTRY[capability_id], produced, attempted=attempted
                )
                attempted.append(capability_id)
                reason = capability.applicable(vol)
                if reason is not None:
                    ...            # per-capability REJECTED, chapter 5 T7
                    continue
                produced[capability_id] = capability.run(vol, ctx)
            except CapabilityRejection as exc:
                attempted.append(capability_id)
                ...                # per-capability REJECTED, carries exc.reason_code

    `attempted` is what lets `bind_dependencies` distinguish "the dependency ran and
    rejected" (a clinical outcome, which rejects this capability too) from "the worker
    called me out of order" (a bug). Without it, a study whose `lung_segmentation`
    rejects would take down the step with a `MissingDependency` RuntimeError instead of
    recording a rejection. Measured: 5 of the 24 LCTSC-Test cases reach that state.
    `pleural_effusion` has no dependency and still produces its outcome on those cases.

Spec: MOS-SVC-011, MOS-SAFE-012, MOS-EXEC-001, CONTRACT.md §1, §6, §7.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from medos.capabilities.base import (
    APPLICABILITY_REASON_CODES,
    Capability,
    CapabilityContext,
    CapabilityMetadata,
    CapabilityRejection,
    DependentCapability,
    DetectionMethod,
    FailureMode,
    MissingDependency,
    coded,
    default_concepts_path,
    load_concepts,
    mask_for_segment,
    require_source_grid,
    segment_index,
)
from medos.capabilities.emphysema_laa import EmphysemaLaa
from medos.capabilities.lung_segmentation import LungSegmentation
from medos.capabilities.pleural_effusion import PleuralEffusion

__all__ = [
    "REGISTRY",
    "Capability",
    "CapabilityContext",
    "CapabilityMetadata",
    "CapabilityRejection",
    "DependentCapability",
    "DetectionMethod",
    "FailureMode",
    "MissingDependency",
    "APPLICABILITY_REASON_CODES",
    "LungSegmentation",
    "EmphysemaLaa",
    "PleuralEffusion",
    "execution_order",
    "bind_dependencies",
    "metadata_for",
    "coded",
    "load_concepts",
    "default_concepts_path",
    "segment_index",
    "mask_for_segment",
    "require_source_grid",
]


REGISTRY: dict[str, Capability] = {
    "lung_segmentation": LungSegmentation(),
    "emphysema_laa": EmphysemaLaa(),
    "pleural_effusion": PleuralEffusion(),
}


def _served(registry: Mapping[str, Capability] | None) -> Mapping[str, Capability]:
    """The capabilities THIS DEPLOYMENT serves, not the ones this module ships.

    `REGISTRY` is the platform base. A deployment composes onto it (`MOS-REG-072` owns
    "does this receive work"; `medos.capabilities.providers` is the pre-registry stand-in),
    so a function that answers a question ABOUT A RUNNING DEPLOYMENT and reads `REGISTRY`
    answers it for a deployment that may not exist.

    Measured, not predicted: with `MEDOS_CAPABILITY_PROVIDERS=lung_nodule`,
    `metadata_for("lung_nodule")` raised `KeyError`, `medos.training.runs._output_kind_of`
    caught it and returned `None`, and a training run against the one capability release
    0.3.0 exists to prove silently lost its output kind. That is the fourth and fifth site
    of register entry 68's defect, and the loss was SILENT, which `MOS-SVC-011` forbids.

    The import is function-local because `providers` imports `REGISTRY` from this module;
    top-level would be a cycle. Callers that hold their own mapping -- the worker holds
    `ctx.deps.registry` -- SHOULD pass it, so nothing resolves twice.
    """
    if registry is not None:
        return registry
    from medos.capabilities.providers import resolve

    return resolve().registry


def metadata_for(
    capability_id: str, registry: Mapping[str, Capability] | None = None
) -> CapabilityMetadata:
    """The chapter 9 honesty block for one capability (MOS-SAFE-012).

    Every surface that shows a finding must show what produced it. `method_class` is the
    field that keeps a Hounsfield threshold from being rendered as "AI".

    Resolved against what the DEPLOYMENT serves (`_served`), so a capability a deployment
    composed in has a clinical block like any other. `MOS-SAFE-013` makes that block
    mandatory and admits no default, so the alternative to finding it is raising, never
    substituting.
    """
    capability = _served(registry)[capability_id]
    metadata = getattr(capability, "metadata", None)
    if metadata is None:
        raise KeyError(
            f"capability {capability_id!r} declares no metadata; MOS-SAFE-013 makes the "
            "clinical block mandatory and there is no default"
        )
    return metadata


def execution_order(
    capability_ids: Iterable[str], registry: Mapping[str, Capability] | None = None
) -> tuple[str, ...]:
    """Order the requested capabilities so every dependency runs before its dependent.

    This is the ordering CONTRACT.md §7 requires `worker/steps.py` to apply. Deriving it
    from `depends_on` rather than writing the sequence down means adding a third
    capability with a dependency does not need the worker edited, and a cycle is an error
    rather than an arbitrary order.

    A dependency that was NOT requested is an error, not something to silently add: the
    job asked for a specific set of capabilities and quietly running another one produces
    a `Result` nobody asked for, with provenance claiming the caller requested it.
    """
    served = _served(registry)
    requested = list(dict.fromkeys(capability_ids))  # de-duplicate, keep order
    unknown = [cid for cid in requested if cid not in served]
    if unknown:
        raise KeyError(
            f"unknown capability_id(s): {unknown}; this deployment serves "
            f"{sorted(served)}"
        )

    deps: dict[str, tuple[str, ...]] = {
        cid: tuple(getattr(served[cid], "depends_on", ())) for cid in requested
    }
    for cid, required in deps.items():
        missing = [dep for dep in required if dep not in requested]
        if missing:
            raise ValueError(
                f"capability {cid!r} depends on {missing}, which the job did not "
                f"request. Add it to the request explicitly -- running an unrequested "
                f"capability would produce a Result the caller never asked for."
            )

    ordered: list[str] = []
    visiting: set[str] = set()

    def visit(cid: str, chain: tuple[str, ...]) -> None:
        if cid in ordered:
            return
        if cid in visiting:
            raise ValueError(f"dependency cycle among capabilities: {[*chain, cid]}")
        visiting.add(cid)
        for dep in deps[cid]:
            visit(dep, (*chain, cid))
        visiting.discard(cid)
        ordered.append(cid)

    for cid in requested:
        visit(cid, ())
    return tuple(ordered)


def bind_dependencies(
    capability: Capability,
    upstream: Mapping[str, object],
    *,
    attempted: Iterable[str] = (),
) -> Capability:
    """Bind a dependent capability to the outcomes produced so far; pass others through.

    The worker calls this once per capability, in `execution_order`. Keeping the
    `isinstance` check here rather than in the worker means the worker never has to know
    which capabilities have dependencies.

    `attempted` is every capability the worker has already RUN in this job, whether or not
    it produced an outcome, and it is what separates the two ways a dependency can be
    absent from `upstream`:

      * attempted and absent -> it ran and REJECTED. `emphysema_laa` has no mask to
        measure inside, which is a clinical fact about this study, so this raises
        `CapabilityRejection("input_constraint_unmet")` and the dependent capability
        terminates `REJECTED` too (chapter 5 T7, `MOS-EXEC-001`). Measured: LCTSC-Test
        S1-204, S2-201, S3-102, S3-203 and S3-204 all reach exactly this state.
      * never attempted -> the worker called this out of `execution_order`. That is a
        wiring bug, and `MissingDependency` says so rather than reporting a clinical
        rejection for a study that was never the problem.

    `attempted` defaults to empty, which keeps the strict reading: with no evidence that
    the dependency was ever run, an absent outcome is a programming error.
    """
    if not isinstance(capability, DependentCapability):
        return capability

    seen = set(attempted)
    rejected_upstream = [
        dep for dep in capability.depends_on if dep not in upstream and dep in seen
    ]
    if rejected_upstream:
        raise CapabilityRejection(
            "input_constraint_unmet",
            {
                "capability_id": getattr(capability, "capability_id", "?"),
                "depends_on": list(capability.depends_on),
                "unsatisfied": rejected_upstream,
                "produced": sorted(upstream),
            },
            f"{getattr(capability, 'capability_id', '?')} requires the output of "
            f"{rejected_upstream}, which ran and produced no outcome for this study; "
            "there is no input to compute on and a value computed without it would be "
            "measured over the wrong voxels",
        )
    return capability.bind(upstream)  # type: ignore[arg-type]
