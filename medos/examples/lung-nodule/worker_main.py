# SPDX-License-Identifier: Apache-2.0
"""A worker process that serves `lung_nodule`. The whole deployment-side change.

    python medos/examples/lung-nodule/worker_main.py --once

THIS IS NO LONGER HOW A DEPLOYMENT SHIPS THIS CAPABILITY. READ THIS FIRST.
----------------------------------------------------------------------------
When this file was written, `medos.worker.runner.main()` -- what `medos/deploy/compose`'s
`medos-worker` runs as `python -m medos.worker.runner` -- constructed `WorkerRunner(config)`
with no `deps`, and no flag and no environment variable could add a capability to it. So the
shipped binary served exactly the three capabilities in `medos.capabilities.REGISTRY`, and a
deployment that wanted a fourth had to run its own entry point. This was it.

That gap is CLOSED. `medos/medos/capabilities/providers.py` is the entry-point loader this file's
original text asked for: a deployment sets

    MEDOS_CAPABILITY_PROVIDERS=lung_nodule

and BOTH `medos-api` and `medos-worker` resolve it through one function -- the API admits
`lung_nodule` at `POST /api/v1/jobs` and the stock worker binary executes it, from the same
value, so the two cannot disagree. `medos/deploy/compose/docker-compose.yml` sets it from a single
YAML anchor merged into both services.

THE VALUE IS A SELECTOR NAME, NOT A MODULE PATH, and the difference is load-bearing. It was
a dotted path handed to `importlib.import_module()` until `MOS-REL-108` -- "no dynamic module
import" -- was read against it; `MOS-CONF-109` cites that rule as the IEC 62304 segregation
evidence, so the mechanism had to become a lookup. `medos/services/catalogue.py` maps the name to a
module reached by an ordinary top-level import, and a name that is not a key is refused with
the keys listed. An old dotted value is detected by name and told what replaces it.

WHAT THIS FILE IS NOW: the same wiring done by hand, kept as a worked example of the
injection seam and as the escape hatch for a deployment that wants a registry it does not
want to name in its environment. It is not what `medos/deploy/compose` runs and it is not the
supported path. Prefer the variable.

WHY THE SEAM WAS ALREADY RIGHT: `WorkerDeps.registry` and `WorkerDeps.concepts` are public,
documented as parameters ("Injected rather than imported so that ... the capability registry
stays a parameter"), and every consumer on the execution path reads `ctx.deps.registry`
rather than the module global. The loader turns a config value into that mapping; it did not
have to move the seam to do it.

WHAT IT DELIBERATELY DOES NOT DO
---------------------------------
It does not mutate `medos.capabilities.REGISTRY`. That would be a global side effect on
every other consumer in the process -- `medos.training.runs` reads it, and
`tests/unit/test_capabilities.py` asserts its three keys -- and it would make the
"zero core change" claim true of the diff and false of the runtime.

It sets no environment variable. `services.lung_nodule.concepts.compose_to()` returns a
path and this module hands it to `ConceptDictionary` directly; a library that wrote
`MEDOS_CAPABILITY_CONCEPTS` into `os.environ` would change the behaviour of anything else
in the process that later constructed its own dictionary.

Spec: MOS-REL-020, MOS-SVC-002, CONTRACT.md section 11.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

# The repository root, so `medos/services/` and `medos/medos/` are both importable. A packaged
# deployment would install both as wheels and drop this; `pyproject.toml`'s
# `[tool.setuptools.packages.find] include = ["medos*"]` does not cover `services*`, which
# is REPORTED as a packaging gap rather than fixed here -- fixing it edits a core file.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from medos.worker.runner import RunnerConfig, WorkerRunner, dsn_from_env  # noqa: E402
from services.lung_nodule import registry as lung_nodule  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--dicomweb-url", default=None)
    parser.add_argument("--work-root", default=None)
    parser.add_argument(
        "--registry-dir",
        default=None,
        help="where the composed coded-concept dictionary is written (default: work-root)",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--max-jobs", type=int, default=None)
    args = parser.parse_args(argv)

    defaults = RunnerConfig()
    config = RunnerConfig(
        dsn=args.dsn or dsn_from_env(),
        dicomweb_url=args.dicomweb_url or defaults.dicomweb_url,
        work_root=Path(args.work_root) if args.work_root else defaults.work_root,
    )

    # THE TWO LINES. `worker_concepts` composes the base dictionary with this capability's
    # overlay rows; `worker_registry` returns the platform's mapping plus one entry, as a
    # NEW dict. Neither mutates anything the platform owns.
    concepts = lung_nodule.worker_concepts(
        Path(args.registry_dir) if args.registry_dir else config.work_root / "registry"
    )
    registry = lung_nodule.worker_registry(concepts)

    with WorkerRunner(config) as runner:
        # `replace()` on the runner's OWN deps, so the gateway it built is reused rather
        # than rebuilt here. That matters: `medos.worker.runner` constructs the gateway
        # through `GatewayConfig.from_env()`, which its docstring calls "the ONLY place in
        # `medos` that reads a PACS credential variable". Constructing a second one in the
        # Service Plane would put credential handling somewhere nobody audits for it, and
        # `MOS-SVC-003` makes the Gateway the single holder.
        #
        # STILL REPORTED: `WorkerRunner` takes `deps` all-or-nothing and builds the gateway
        # internally, so there is no supported way to say "the default deps, but with this
        # registry". Assigning the attribute back is reaching past the API. The entry-point
        # loader half of that report has since landed (`MEDOS_CAPABILITY_PROVIDERS`, read by
        # `medos.capabilities.providers`) and is what a deployment should use; a
        # `WorkerRunner(config, gateway=...)` parameter for the remaining case -- keep the
        # resolved registry, substitute the PACS -- has not.
        runner.deps = replace(runner.deps, registry=registry, concepts=concepts)
        if args.once:
            runner.run_once()
        else:
            runner.run_forever(max_jobs=args.max_jobs)
    return 0


if __name__ == "__main__":  # pragma: no cover - a process entry point
    raise SystemExit(main())
