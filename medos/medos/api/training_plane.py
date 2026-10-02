# SPDX-License-Identifier: Apache-2.0
"""The model-preparation plane's HTTP surface, and the app factory that serves it.

TWO SERVICES, ONE REPOSITORY
----------------------------
MedicalOS ships as two deployables:

  * **Core** -- `medos.api.app:create_app`. The PACS-and-models service: DICOM in and
    out through the credentialed gateway, jobs, capabilities, the service and model
    registries, results and reviews. It cannot train and does not carry the code that
    could. 25 route paths.
  * **Train** -- `create_training_app` below. Core plus the model-preparation surface:
    cohort harvesting, curation, dataset versions, splits, annotation sets, training
    runs, configuration searches and conversion runs. 52 route paths.

Train is Core plus routers. Core is not Train minus anything: it is the whole of what a
site that only SERVES models needs, and the fact that it is also a strict subset is what
makes the split cheap rather than a fork.

WHY THIS MODULE EXISTS AT ALL, RATHER THAN A FLAG
-------------------------------------------------
`MOS-REL-108` forbids dynamic module import in a platform process, on `MOS-CONF-109`'s
IEC 62304 section 4.3 segregation argument, and an earlier version of this platform DID
resolve providers with `importlib.import_module()` on an environment variable before
that was removed. So there is no `MEDOS_ENABLE_TRAINING` and no plugin scan. Which
surfaces a process serves is decided by WHICH ENTRYPOINT WAS STARTED, and the routers
reach `create_app` as ordinary Python objects the caller already imported.

The consequence worth stating: on a Core deployment this module is never imported, so
`medos.training` is never imported either, and that is checked rather than asserted --
`tests/gate/test_core_train_boundary.py` walks the static import closure of
`medos.api.app` and fails if it reaches the training engine.

WHY THE ROUTERS STAY UNDER `medos/medos/api/`
----------------------------------------
Two existing contracts pin them here, and neither is worth breaking for tidiness:

  * `medos/tools/permcheck.py` requires every `api_problem_types[*].minted_by` in
    `medos/api/v1/routes.train.yaml` to name a file under `medos/medos/api/`.
    Fourteen entries do.
  * `tests/gate/test_no_auto_promote.py` check 1 asserts the import closure of
    `medos.training` reaches neither `medos.promotion` nor `medos.evidence.deployment`
    (`MOS-TRAIN-189`: the training pipeline must be structurally incapable of
    promoting). These routers import the registry and the engine, so moving them INTO
    `medos/medos/training/` would open exactly the edge that check keeps closed.

So the boundary is drawn where it can be enforced -- at the import closure and at what
each image contains -- rather than by moving files into a directory whose name suggests
a separation the checks would then contradict.

Spec: MOS-REL-108, MOS-CONF-109, MOS-TRAIN-189, MOS-API-112.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, FastAPI

from medos.api.app import create_app
from medos.api.routes_curation import router as curation_router
from medos.api.routes_training import router as training_router

__all__ = ["TRAINING_ROUTERS", "create_training_app"]

#: Chapter 10 table 10.2-B rows R6-R31, in mount order.
#:
#: ORDER IS PART OF THE CONTRACT. Starlette matches in registration order, and
#: `curation_router` follows `training_router` because
#: `GET /api/v1/dataset-versions/{dataset_version_id}` (row 37) and
#: `GET /api/v1/dataset-versions/{id}/annotation-sets` (row R30) live in the second.
#: Neither collides with a training path today, so this is documentation rather than
#: load-bearing -- but the rule it documents is that a later router MUST NOT be able to
#: shadow an earlier one's route, and a tuple is where that rule is now written down.
TRAINING_ROUTERS: tuple[APIRouter, ...] = (training_router, curation_router)


def create_training_app(
    *,
    extra_routers: Sequence[APIRouter] = (),
    **kwargs: Any,
) -> FastAPI:
    """Build the model-preparation deployable: Core, plus the training surface.

    Every keyword `medos.api.app.create_app` accepts is forwarded unchanged -- this is
    the same application with more routers, not a second application that resembles it.
    `MOS-REL-046` still holds: two calls with different DSNs share nothing.

    Mounted inside the same PEP as every other surface, because `create_app` adds
    `AuthenticationMiddleware` around whatever it was given. That is load-bearing beyond
    the usual here: row R24 is the route closest to a patient on the whole platform, and
    a curation route is authenticated and tenant-bound on exactly the same path as a job
    endpoint.

    Run it with:

        uvicorn medos.api.training_plane:create_training_app --factory --port 8000
    """
    return create_app(extra_routers=(*TRAINING_ROUTERS, *extra_routers), **kwargs)
