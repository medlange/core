# SPDX-License-Identifier: Apache-2.0
"""The corpus-assembly surface. Table 10.2-B rows `R1`-`R5` and `R18`-`R31`, plus `32`, `37`.

| Method + path                                        | Row | Permission              |
|------------------------------------------------------|-----|-------------------------|
| `POST /harvest-batches`                                | R1  | `harvest_batch.open`    |
| `GET  /harvest-candidates/{id}/decision`               | R2  | `harvest_candidate.read` |
| `POST /harvest-candidates/{id}/decision`               | R3  | `curation_decision.record` |
| `PUT  /tenants/{tenant_id}/training-policy`            | R4  | `training_data_policy.record` |
| `GET  /harvest-batches/{id}/stratification`      | R5  | `corpus_stratification_report.read` |
| `GET  /tenants/{tenant_id}/training-policy`            | R18 | `training_data_policy.read` |
| `POST /sampling-plans`                                 | R19 | `sampling_plan.declare` |
| `GET  /sampling-plans`                                 | R20 | `sampling_plan.read`    |
| `GET  /sampling-plans/{sampling_plan_id}`              | R21 | `sampling_plan.read`    |
| `GET  /harvest-batches`                                | R22 | `harvest_batch.read`    |
| `GET  /harvest-batches/{id}`                           | R23 | `harvest_batch.read`    |
| `GET  /harvest-batches/{id}/candidates`                | R24 | `harvest_candidate.read` |
| `GET  /harvest-batches/{id}/split-preview`             | R25 | `harvest_batch.read`    |
| `POST /harvest-batches/{id}/seal`                      | R26 | `dataset_version.create`|
|                                                      |     |  + `dataset_split.freeze` |
| `GET  /seal-runs/{seal_run_id}`                        | R27 | `dataset_version.read`  |
| `GET  /dataset-splits/{dataset_split_id}`              | R28 | `dataset_split.read`    |
| `POST /dataset-versions/{id}/annotation-sets`          | R29 | `annotation_set.create` |
| `GET  /dataset-versions/{id}/annotation-sets`          | R30 | `annotation_set.read`   |
| `GET  /annotation-sets/{annotation_set_id}`            | R31 | `annotation_set.read`   |
| `GET  /datasets`                                       | 32  | `dataset.read`          |
| `GET  /dataset-versions/{dataset_version_id}`          | 37  | `dataset.read`          |

Every path above is under `/api/v1`, which the router's own `prefix=` carries.

`medos/api/v1/routes.train.yaml` is normative over this file. Every row above carries its
argument there and the arguments are NOT repeated here; what this module owes is the five things
CONTRACT.md section 1 admits in a handler -- parse, authorise, bind the tenant, call the
engine, render -- and nothing else.

WHY THIS IS A SECOND MODULE AND NOT MORE OF `routes_training.py`
------------------------------------------------------------------
Three reasons, and the third is the one that would still hold if the file were short.

  1. `routes_training.py` serves chapter 19 sections 19.3.5-19.3.7 -- what happens AFTER
     a cohort exists. These rows serve 19.3.2-19.3.4 -- how one comes to exist. The two
     halves meet at exactly three ids on `POST /api/v1/training-runs`, and the seam is
     where the surface was broken until now.
  2. MOST OF THESE ROWS READ CHAPTER 7 ENTITIES THIS SURFACE DOES NOT OWN.
     `DatasetVersion`, `DatasetSplit`, `AnnotationSet` and `Dataset` belong to chapter 7;
     rows 32 and 37 are EXISTING table 10.2-B rows that predate the training console.
     `medos/schemas/` already draws this line -- `medos/schemas/evidence/` is a second directory
     rather than more files under `medos/schemas/training/`, "because filing them under
     `training/` would put chapter 7's wire contract in a directory chapter 17's changes
     edit". A module boundary that matches the schema boundary is one fewer place for
     the two to drift.
  3. `PERMISSIONS` IS PER-MODULE AND SO IS THE `MOS-SEC-158` GUARD. `routes_training.py`
     asserts at import that no permission it binds is one chapter 17 excludes from the
     training identity. That assertion is only as good as its subject: folding eleven
     more keys into one dict makes one failure message name eleven routes, and makes the
     two surfaces' forbidden-permission posture a single fact when `MOS-UI-102` scopes it
     per surface.

THE FIVE ROWS THIS MODULE GAINED, AND WHY THEY ARE HERE RATHER THAN RESERVED
------------------------------------------------------------------------------
`R1`-`R5` were `status: reserved` until this change on a sentence that read "MUST NOT be
served until a handler is registered", which forbids serving them until they are served.
`docs/spec/99-known-inconsistencies.md` entry 88 records the circularity and `MOS-API-112`
as amended states the correction in terms: "these five are unserved because no handler
exists, and the change that registers one against the `permission:` value of its own row
serves them." No safety property depended on the reservation -- `MOS-TRAIN-073` already
binds the policy to a named human and `MOS-SEC-032` already registers all five keys -- and
the measured consequence was that a browser could seal a cohort, freeze a split and submit
a training run and could not perform the operator's FIRST TWO STEPS.

Four of the five are thin over an engine function that already existed. `R1` is not, and
the difference is `THE DRAW` below.

WHAT IS NOT HERE, AND WHY EACH ABSENCE IS A RULE
--------------------------------------------------
  NO BULK DECISION ROUTE, AND NO `decisions` ARRAY ON `R3`. `MOS-TRAIN-080` makes a
    decision a named human deciding one candidate, `MOS-TRAIN-204` forbids auto-inclusion
    and `MOS-UI-119` forbids a *select all and include* control "that writes decisions
    without the cases having been shown". A body that carried a list of candidate ids
    would be that control with an HTTP client in front of it, and no amount of console
    discipline could take it away again. The candidate is the PATH and the body names one
    decision; the structural form of the rule is that there is no member in which a second
    candidate could be written.
  NO `decided_by` AND NO `recorded_by` ON ANY BODY. `MOS-TRAIN-080`'s named human and
    `MOS-TRAIN-073`'s named human are the authenticated principal, read from the
    credential by `_actor`. A caller that may name someone else has not named anybody.
  NO `DELETE` AND NO `PATCH` ON A DECISION. `MOS-TRAIN-203` retains every exclusion and
    `curation_decisions` is append-only; a settled candidate answers `409` on a second
    decision rather than overwriting the first.
  NO `force`, NO `reason_override` AND NO ADMINISTRATOR PATH ON `R4`. `MOS-TRAIN-072`
    forbids all three by name, and `medos.training.policy.set_training_use_allowed`
    already says so; this route adds no parameter that could become one.
  NO ROUTE FREEZES A SPLIT. `MOS-UI-130` makes the seal one operator action spanning the
    `DatasetVersion` seal and the `DatasetSplit` freeze and `MOS-UI-132` makes it atomic.
    Independently: a freeze route would have to accept `assignments`, and a caller that
    names which patients are in `test` is `MOS-TRAIN-141`'s "container that can name its
    own data" with an HTTP client in front of it.
  NO ROUTE WRITES A WAIVER. `MOS-UI-135`, and `MOS-TRAIN-115`'s reason that a waiver on
    L1, L2, L3 or L5 cannot permit a training run anyway.
  NO ROUTE CREATES A `Dataset`. Row 33 binds `dataset.write`, which section 8.3.2
    registers as a spelling a build MUST fail on.
  NO PATCH AND NO DELETE ANYWHERE. `MOS-STORE-359` seals `sampling_plans`,
    `MOS-EVID-013` seals the version, `MOS-TRAIN-110` seals the annotation set, and
    `MOS-API-011` answers a delete on any of them with `409`.

THE DRAW: WHERE `R1`'s CANDIDATES COME FROM, AND THE ONE PLACE THEY COULD COME FROM
--------------------------------------------------------------------------------------
Table 10.2-B row `R1` is "open a `HarvestBatch` against a sealed `SamplingPlan` **and draw
its candidates**", and `HarvestBatchCreateRequest` carries three ids and no candidate
payload -- so the draw is the SERVER's, over data the platform already holds.
`MOS-TRAIN-079` fixes what a candidate must carry and `MOS-TRAIN-084` requires
`acquisition_profile` "copied from the source header without imputation". Exactly one
table in this platform holds that: `result_provenance`, written by the worker at the
moment the capability ran, carrying `acquisition`, `geometry`, `input_series_uids`,
`input_instance_uids` and the `gateway` block with `deid_policy_version`. `studies` is a
thin projection with no header fields and no series UIDs; `harvest_candidates` itself
requires `cardinality(series_instance_uids) >= 1`, which that projection cannot supply.
So `_draw_candidates` reads the platform's own result record, and `medos.training.
curation.add_candidate` -- which re-runs `MOS-TRAIN-075`'s per-study scope test on every
row -- remains the only writer.

TWO CONSEQUENCES, NAMED HERE BECAUSE NEITHER IS FIXABLE FROM THIS FILE.

  1. EVERY CANDIDATE THIS DRAW PRODUCES HAS `ran_on_platform = true`, AND THE SAMPLING
     PLAN IS REQUIRED TO STRATIFY ON THAT COLUMN. `declare_sampling_plan` refuses a plan
     without it because "the corpus can only ever describe the inside of the current
     envelope" otherwise -- and a draw that can only produce one value of it is that
     failure with the stratum still declared. Closing it needs a source of
     `MOS-TRAIN-084`'s profile for a study the capability never ran on, which on this
     platform means a Gateway QIDO under `MOS-TRAIN-068`'s `dataset_export` class and a
     column on `harvest_candidates` for the modality (`medos/medos/training/retrieval.py`
     already reports the second). `not_drawn` counts what was skipped and why, so the
     shortfall is visible per draw rather than inferred from a cohort.
  2. `score_band` COLLAPSES TO ONE VALUE ON THIS DEPLOYMENT. `MOS-TRAIN-083` bands "below
     the operating point, or the bottom decile" and requires the lowest band's sampling
     fraction to be at least three times the top band's. The operating point is
     `result_provenance.operating_threshold`, which is NULL on every row this platform has
     written, and a decile is a property of a cohort that does not exist until the draw
     finishes. `SCORE_BANDS` therefore carries `unscored` and `unbanded_no_operating_point`
     as first-class labels rather than folding either into a band it is not: a case with no
     score is not a low-scoring case, and `MOS-EVID-020`'s rule against imputation is the
     same rule one level up.

`acquisition_bucket` is `medos.training.split.stratum_of`'s `slice_thickness_band` and NOT
a second banding: the bands the split stratifies on and the bands the operator selects on
have to be the same bands, or `MOS-UI-112`'s "exactly the fields that the seal-time checks
and the split generator read" is false the moment the two drift.

THE ROW CLOSEST TO A PATIENT IS `R24`, AND ITS PROJECTION IS SUBTRACTIVE
-------------------------------------------------------------------------
`_candidate_view` names what it drops and why. `instance_uids` becomes a COUNT because a
page whose size is the cohort's instance count is not a page; `note` is not projected at
all because `MOS-UI-120` admits it as free text on one decision and free text written by
an operator has no business on a list screen; there is no display-name member anywhere,
because `MOS-UI-113` forbids a control that resolves `institution_key` back to a name and
the surest way to forbid one is to ship no value it could resolve.

THREE DEPLOYMENT DECLARATIONS, ON `routes_training.py`'s PRECEDENT
------------------------------------------------------------------
`MEDOS_SEAL_STORE`, `MEDOS_ANNOTATION_STACK` and `MEDOS_TENANT_SALT`. All follow the
argument that module
already makes for `MEDOS_TRAINING_ENVIRONMENT`: a value a client may supply is a value a
client may supply wrongly, and what a deployment declaration buys is that the assertion
is made ONCE, by the deployment, in a place a reviewer can read. All three refuse `503`
when absent rather than defaulting, and the refusal says what the deployment has not said.
`MEDOS_TENANT_SALT` is the newest and is the chapter 8 tenant keyring's stand-in:
`medos/medos/evidence/repo.py` already REPORTS that keyring as an absent dependency -- "a salt
in the repository is not a salt" -- and `MOS-EVID-010` and `MOS-TRAIN-089` both need one
before a candidate can carry a `patient_key` or an `institution_key` at all. A draw
without it is refused rather than run with a constant, because an `institution_key` every
deployment can compute is not an HMAC.

WHAT `MOS-API-022` GETS ON `R1` AND `R3`, AND WHAT IT DOES NOT
----------------------------------------------------------------
`MOS-API-022` marks `Idempotency-Key` **required** on rows 15, 29, 36, 38, 42, 46, 51, 55,
62, 68b and 71a and reaches neither of these two, so both are `optional` -- and that
requirement's second sentence is still a MUST for an optional row: "when absent the server
MUST generate one and MUST echo it in `MedicalOS-Idempotency-Key`." That half is closed
here, for `R1`, `R3` and the three existing creating rows `R19`, `R26` and `R29`, which
were serving without it. What is NOT closed is making the key REQUIRED: that is an edit to
`MOS-API-022`'s enumeration in chapter 10, which owns it, and a key this surface merely
echoes is not a replay guard. The exposure is bounded and is stated rather than implied:
`R3` cannot double-decide a candidate (`curation_decisions_settled_uk` answers `409`) and
`R4` is a `PUT` whose second identical call is a no-op by construction, but `R1` CAN open
two batches from two deliveries of one request, and the operator sees two batches rather
than a wrong cohort.

Spec: MOS-API-004, MOS-API-005, MOS-API-011, MOS-API-014, MOS-API-015, MOS-API-016,
MOS-API-017, MOS-API-018, MOS-API-022, MOS-API-023, MOS-API-035, MOS-API-037,
MOS-API-038, MOS-API-042, MOS-API-046, MOS-API-056, MOS-API-057, MOS-API-089,
MOS-API-112, MOS-EVID-010, MOS-EVID-013, MOS-EVID-020,
MOS-EVID-027, MOS-EVID-034, MOS-EVID-035, MOS-EVID-036, MOS-EVID-037, MOS-EVID-038,
MOS-EVID-039, MOS-EVID-042, MOS-EVID-043, MOS-EVID-116, MOS-SEC-008, MOS-SEC-027,
MOS-SEC-032, MOS-SEC-045, MOS-SEC-072, MOS-SEC-158, MOS-STORE-359, MOS-TRAIN-072,
MOS-TRAIN-073, MOS-TRAIN-074, MOS-TRAIN-075, MOS-TRAIN-076, MOS-TRAIN-077,
MOS-TRAIN-078, MOS-TRAIN-079,
MOS-TRAIN-080, MOS-TRAIN-083, MOS-TRAIN-084, MOS-TRAIN-088, MOS-TRAIN-089,
MOS-TRAIN-091, MOS-TRAIN-095, MOS-TRAIN-097,
MOS-TRAIN-101, MOS-TRAIN-110, MOS-TRAIN-112, MOS-TRAIN-114, MOS-TRAIN-122,
MOS-TRAIN-141, MOS-TRAIN-202, MOS-TRAIN-203, MOS-TRAIN-204, MOS-TRAIN-208,
MOS-TRAIN-209, MOS-UI-101, MOS-UI-102, MOS-UI-104,
MOS-UI-110, MOS-UI-111, MOS-UI-112, MOS-UI-113, MOS-UI-116, MOS-UI-117, MOS-UI-119,
MOS-UI-120,
MOS-UI-122, MOS-UI-129, MOS-UI-130, MOS-UI-132, MOS-UI-135, MOS-UI-136, MOS-UI-147,
CONTRACT.md sections 1, 9 and 11.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import psycopg
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field, model_validator

from medos.api import db_connection
from medos.api.auth import principal_of
from medos.api.problems import json_safe

# The five renderers below are `medos/medos/api/routes_training.py`'s and are REUSED rather
# than copied. They are underscore-named because they are internal to `medos/medos/api/`, not
# because they are private to that module: a second `_not_found` here would be a second
# answer to "does a row this tenant may not read exist", and `MOS-SEC-072`'s isolation is
# worth nothing if two surfaces answer it differently. The alternative -- promoting them
# into `medos/medos/api/problems.py` -- is a wider edit than this change should make to a file
# four other routers import.
from medos.api.routes_training import _actor, _malformed, _not_found, _problem, _refusal
from medos.config.devmode import refuse_dev_value_outside_dev
from medos.db.tenancy import tenant_tx
from medos.evidence import digest as ev_digest
from medos.evidence import repo as ev_repo
from medos.evidence.store import InMemoryManifestStore, ManifestStore, S3ManifestStore
from medos.sdk.errors import CurationRefused, HarvestRefused
from medos.sdk.refusal import FreezeRefused, Refusal, RefusalError
from medos.security.scopes import scope_permits
from medos.training import curation as cur
from medos.training import policy as train_policy
from medos.training import retrieval
from medos.training import seal as seal_mod
from medos.training import split as split_mod
from medos.training.candidate import FORBIDDEN_PIPELINE_PERMISSIONS
from medos.training.orchestrator import FORBIDDEN_PERMISSIONS

__all__ = [
    "ANNOTATION_STACK_VAR",
    "PERMISSIONS",
    "SCORE_BANDS",
    "SEAL_BUCKET_VAR",
    "SEAL_CAMPAIGN_DIR_VAR",
    "SEAL_STORE_VAR",
    "TENANT_SALT_VAR",
    "router",
    "seal_driver_for",
]

log = logging.getLogger("medos.api.curation")

router = APIRouter(prefix="/api/v1", tags=["curation"])


# =====================================================================================
# Permissions
# =====================================================================================
#: The sixteen keys these twenty-one rows bind, with the sentence
#: `medos/contracts/permissions.yaml` registers each under. Every one is an EXISTING key of
#: chapter 8 section 8.3.2 -- this change mints none, which is why `MOS-API-005` and
#: `MOS-SEC-032` are satisfied for every row the moment a handler is registered.
#:
#: `training_data_policy.record` is the one key here of class `governance`, and that is
#: why `medos/api/v1/routes.train.yaml` carries NO `surface:` on row `R4`. `MOS-UI-102` forbids
#the : console holding a `governance` permission and `MOS-UI-110` forbids it editing the
#: policy at all -- "the assertion it records is the site's legal one, not this
#: operator's" -- so `R4` is served for an integrator and for the role that records the
#: instrument, and the console reads `R18` and renders the refusal. `medos/tools/permcheck.py`
#: scopes its class rule to entries carrying a `surface:`, which is what makes the
#: absence of that key load-bearing rather than an omission.
PERMISSIONS: Final[dict[str, str]] = {
    "harvest_batch.open": (
        "open a HarvestBatch against a sealed SamplingPlan and draw its candidates"
    ),
    "curation_decision.record": (
        "record a CurationDecision on a candidate as the named human"
    ),
    "training_data_policy.record": (
        "record the tenant's TrainingDataPolicy as the named human and set "
        "training_use_allowed with it"
    ),
    "corpus_stratification_report.read": "read a batch's CorpusStratificationReport",
    "training_data_policy.read": (
        "read whether the tenant's TrainingDataPolicy permits training use, and the "
        "instrument recorded"
    ),
    "sampling_plan.declare": (
        "declare a versioned SamplingPlan before any candidate is drawn"
    ),
    "sampling_plan.read": (
        "read a versioned SamplingPlan and the spec_digest it was sealed under"
    ),
    "harvest_batch.read": (
        "read a HarvestBatch, its state and the composition of the candidate set drawn "
        "into it"
    ),
    "harvest_candidate.read": (
        "read HarvestCandidate rows and the settled decision on one"
    ),
    "dataset_version.create": "seal a dataset version",
    "dataset_version.read": "read a sealed dataset version manifest",
    "dataset_split.freeze": "freeze a patient-level DatasetSplit manifest",
    "dataset_split.read": "read a frozen DatasetSplit manifest",
    "annotation_set.create": "annotations",
    "annotation_set.read": "annotations",
    "dataset.read": "dataset catalogue",
}

#: `MOS-SEC-158`: the exclusion is "enforced by their absence from the role, never by a
#: check in code". The API-surface reading is that no route of this module may BIND one
#: of chapter 17's forbidden permissions -- a route that did would be the grant written
#: down, reachable whatever the role table later says. Asserted at import against the two
#: tuples chapter 17 keeps, never against a second copy, so an addition to either lands
#: here as an import-time failure rather than as a review someone has to remember.
_FORBIDDEN: Final[frozenset[str]] = frozenset(FORBIDDEN_PERMISSIONS) | frozenset(
    FORBIDDEN_PIPELINE_PERMISSIONS
)
_OVERLAP = sorted(set(PERMISSIONS) & _FORBIDDEN)
if _OVERLAP:  # pragma: no cover - a defect, caught before the app can be built
    raise RuntimeError(
        f"medos/medos/api/routes_curation.py binds {_OVERLAP}, which MOS-SEC-158 and "
        "MOS-TRAIN-174 exclude from the training identity."
    )

#: `MOS-UI-130` makes the seal ONE operator action spanning two platform operations, and
#: `MOS-API-005` admits exactly one `permission:` per row. `medos/api/v1/routes.train.yaml`
#records : the second under `also_requires:`; this is the same pair, enforced. Binding only the
#: first would let a principal who may seal a version freeze a split they may not.
SEAL_ALSO_REQUIRES: Final[tuple[str, ...]] = ("dataset_split.freeze",)


# =====================================================================================
# Deployment declarations
# =====================================================================================
#: Where manifests are written. `s3` uses `medos.objectstore.S3Client` (MinIO on the
#: development stack); `memory` is the in-process driver, which is a REAL driver of
#: `ManifestStore` and not a mock -- the seal path exercises put/get/delete against it
#: unchanged -- and is correct only for a deployment that has no object store yet.
SEAL_STORE_VAR: Final[str] = "MEDOS_SEAL_STORE"
SEAL_BUCKET_VAR: Final[str] = "MEDOS_EVIDENCE_BUCKET"

#: `MOS-TRAIN-097`: the production tool, "for the MONAI Label stack the literal form
#: `MONAI Label 0.8.4 + 3D Slicer 5.6.2`. Never empty: a reference standard whose
#: production tool is unrecorded is not reproducible." `MOS-UI-124` has the PLATFORM fill
#: it from the deployed annotation stack and keeps it off the request body, so it is a
#: deployment declaration and a `503` when absent.
ANNOTATION_STACK_VAR: Final[str] = "MEDOS_ANNOTATION_STACK"

#: Where a closed campaign's `MOS-EVID-040` lines are read from, one NDJSON file per
#: `dataset_version_id`. See `_campaign_entries` for the argument; the short form is that
#: the annotation server of `MOS-TRAIN-095` is unbuilt (chapter 19 section 19.4.3), the
#: reference standard MUST NOT arrive on the wire, and a deployment file is the same
#: shape `MEDOS_TRAINING_ENVIRONMENT` already uses for a fact about the deployment.
SEAL_CAMPAIGN_DIR_VAR: Final[str] = "MEDOS_ANNOTATION_CAMPAIGN_DIR"

#: The per-tenant secret `MOS-EVID-010`'s `patient_key` and `MOS-TRAIN-089`'s
#: `institution_key` are HMAC'd under. Chapter 8's tenant keyring does not exist --
#: `medos/medos/evidence/repo.py` reports it as a dependency and refuses to work around it
#: with a constant -- so the deployment declares it, exactly as it declares where
#: manifests are written. There is no default and there is no fallback: a salt every
#: deployment can compute is not a salt, and two tenants sharing one would make
#: `MOS-EVID-010`'s "tenant-scoped surrogate" a cross-tenant identifier.
TENANT_SALT_VAR: Final[str] = "MEDOS_TENANT_SALT"

#: `MOS-TRAIN-083` bands the score "below the operating point, or the bottom decile", and
#: this platform can answer neither on most rows. The vocabulary is therefore four labels
#: and not two, because `MOS-EVID-020`'s rule against imputation applies to a stratum the
#: same way it applies to a slice thickness: a case whose capability emitted no score is
#: NOT a low-scoring case, and a deployment that never recorded an operating point has not
#: placed a case above or below it. Folding either into `below_operating_point` would put
#: the whole cohort in the band `MOS-TRAIN-083` oversamples, which is the one band where
#: being wrong changes what the model learns.
SCORE_BANDS: Final[tuple[str, ...]] = (
    "below_operating_point",
    "at_or_above_operating_point",
    "unbanded_no_operating_point",
    "unscored",
)

#: `MOS-STORE-266`'s terminal `jobs.state` values, which are also the closed set
#: `harvest_candidates.platform_outcome` admits. A case whose job has not finished has no
#: platform outcome and is not drawn: `harvest_candidates_ran_ck` makes `ran_on_platform`
#: and `platform_outcome` one fact, so a running job would have to be recorded as a case
#: the platform never saw.
_TERMINAL_JOB_STATES: Final[frozenset[str]] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "REJECTED", "NOT_APPLICABLE"}
)

#: `MOS-API-023`'s grammar for `Idempotency-Key`.
_IDEMPOTENCY_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._~-]{1,255}$")

_UUID_RE: Final[str] = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_CAPABILITY_RE: Final[str] = r"^[a-z][a-z0-9_]{2,63}$"
_SEAL_RUN_RE: Final[str] = r"^slr_[0-9A-HJKMNP-TV-Z]{26}$"


#: Raised by anything this surface asks the deployment for and does not get. Defined in
#: `medos/medos/training/seal.py` and imported rather than redefined: `MEDOS_DEID_PROVENANCE`
#: is read by the seal driver on its own thread, where no HTTP layer exists, and two
#: exception types meaning the same thing is how one of them stops being caught.
DeploymentNotDeclared = seal_mod.DeploymentNotDeclared


def _manifest_store(request: Request) -> tuple[ManifestStore, str]:
    """The `(store, bucket)` pair the seal writes its three manifests to.

    Bound to THIS APP INSTANCE and not to the module: CONTRACT.md section 11 forbids
    global mutable state and `MOS-REL-046` requires two independent instances in one
    process. `app.state` is where `create_app` already keeps its injected collaborators,
    so a test constructs a second app with a second store and the two share nothing.

    It is cached per instance rather than rebuilt per call for one reason that matters:
    `InMemoryManifestStore` is a REAL driver of the port, and a fresh one per request
    would write the manifest and then drop it -- producing exactly the `MOS-EVID-015`
    failure ("a row whose manifest location does not resolve to an object whose SHA-256
    equals `manifest_digest` MUST be treated as DEFECTIVE") that the store exists to
    avoid. An operator correcting the configuration restarts the process, which is the
    same cost as correcting a DSN.
    """
    cached = getattr(request.app.state, "medos_manifest_store", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    bucket = os.environ.get(SEAL_BUCKET_VAR, "").strip()
    kind = os.environ.get(SEAL_STORE_VAR, "").strip().lower()
    if not kind:
        raise DeploymentNotDeclared(
            SEAL_STORE_VAR,
            "this deployment has not said where sealed manifests are written. "
            "MOS-EVID-015 requires the dataset_versions row and its manifest object to "
            "be written together, and a row whose manifest location does not resolve is "
            "DEFECTIVE by that requirement. Set it to 's3' or, for a deployment with no "
            "object store, to 'memory'",
        )
    if kind == "memory":
        pair: tuple[ManifestStore, str] = (
            InMemoryManifestStore(),
            bucket or "medos-evidence",
        )
    elif kind == "s3":
        from medos.objectstore.s3 import S3Client, S3Config

        client = S3Client(S3Config.from_env())
        pair = (S3ManifestStore(client), bucket or client.config.bucket)
    else:
        raise DeploymentNotDeclared(
            SEAL_STORE_VAR, f"{kind!r} is not a manifest store this platform ships"
        )
    request.app.state.medos_manifest_store = pair
    return pair


def _annotation_stack() -> dict[str, str]:
    """`MOS-TRAIN-097`'s `tool` and the campaign's `instructions_uri`, from the
    deployment. Raises `DeploymentNotDeclared`; never returns a default."""
    raw = os.environ.get(ANNOTATION_STACK_VAR, "").strip()
    # A development default must not reach a deployment that has not declared itself
    # one. First statement after the read, before any interpretation: this makes the
    # loader strictly stricter and leaves its "never returns a default" promise intact.
    refuse_dev_value_outside_dev(ANNOTATION_STACK_VAR, raw)

    if not raw:
        raise DeploymentNotDeclared(
            ANNOTATION_STACK_VAR,
            "MOS-TRAIN-097 requires every reader row to record the tool that produced "
            "the annotations -- 'MONAI Label 0.8.4 + 3D Slicer 5.6.2' is the literal "
            "form -- and MOS-UI-124 has the platform fill it rather than the operator "
            'type it. Set it to {"tool": "...", "instructions_uri": "..."}',
        )
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise DeploymentNotDeclared(
            ANNOTATION_STACK_VAR, f"is not JSON: {exc}"
        ) from exc
    missing = [k for k in ("tool", "instructions_uri") if not str(document.get(k, ""))]
    if missing:
        raise DeploymentNotDeclared(ANNOTATION_STACK_VAR, f"is missing {missing}")
    return {"tool": str(document["tool"]),
            "instructions_uri": str(document["instructions_uri"])}


def _campaign_entries(dataset_version_id: str) -> list[dict[str, Any]]:
    """The closed campaign's `MOS-EVID-040` lines, from the deployment's own store.

    WHY THIS IS NOT A REQUEST MEMBER, AND WHY IT IS NOT INVENTED EITHER.
    `medos/schemas/evidence/annotation-set-freeze-request-1.0.0.json` carries no per-case
    payload, on the ground that "a client that supplies the reference standard on the
    wire can supply one no reader produced, and MOS-EVID-038's named readers would be an
    assertion rather than a record". `medos/medos/evidence/repo.py::freeze_annotation_set`
    takes `entries` from its caller in-process, and the annotation server `MOS-TRAIN-095`
    names is unbuilt -- chapter 19 section 19.4.3 says so in an inventory read out of the
    repository: no MONAI Label deployment, no annotation code in the OHIF extension, no
    writer for the `annotations` table (which cannot hold a campaign's work in any case,
    because its foreign key points at the set the campaign has not yet produced).

    So this reads a file the DEPLOYMENT placed, exactly as `MEDOS_TRAINING_ENVIRONMENT`
    is a declaration the deployment makes about itself, and refuses `503` when there is
    none. What it will NOT do is freeze an empty reference standard: an `AnnotationSet`
    whose `annotation_digest` is a digest over zero lines is a set every `EvaluationRun`
    must fail against (`MOS-EVID-046` -- "MUST cause the run to fail, not to silently
    shrink `n`"), and minting one because the store is missing is the quiet downgrade
    this whole surface is written against.
    """
    directory = os.environ.get(SEAL_CAMPAIGN_DIR_VAR, "").strip()
    if not directory:
        raise DeploymentNotDeclared(
            SEAL_CAMPAIGN_DIR_VAR,
            "the annotation server of MOS-TRAIN-095 is not built in this platform "
            "(chapter 19 section 19.4.3), so there is no store this campaign can be "
            "frozen FROM. A reference standard MUST NOT arrive on the wire "
            "(MOS-EVID-038: the named readers would be an assertion rather than a "
            "record), and an AnnotationSet frozen over zero cases fails every "
            "EvaluationRun that cites it (MOS-EVID-046). Point this at a directory "
            "holding one <dataset_version_id>.ndjson of MOS-EVID-040 lines per closed "
            "campaign, or build the annotation surface",
        )
    path = Path(directory) / f"{dataset_version_id}.ndjson"
    if not path.is_file():
        raise DeploymentNotDeclared(
            SEAL_CAMPAIGN_DIR_VAR,
            "holds no closed campaign for this DatasetVersion. MOS-EVID-046 requires "
            "the set to cover every patient in the partition it is evaluated on, so a "
            "campaign that produced nothing is not a set to freeze",
        )
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    if not entries:
        raise DeploymentNotDeclared(
            SEAL_CAMPAIGN_DIR_VAR,
            "the campaign file for this DatasetVersion is empty; see MOS-EVID-046",
        )
    return entries


def _tenant_salt() -> bytes:
    """The per-tenant HMAC secret, from the deployment. Raises `DeploymentNotDeclared`.

    Read per call and never cached at import, for the reason every other loader in this
    module gives: a declaration cached at import is one an operator cannot correct without
    a restart, and CONTRACT.md section 11 forbids the module-level state that would make
    the cache possible. The value itself is never logged, never returned and never put in
    a problem detail -- only the variable's NAME travels, which is the remedy.
    """
    raw = os.environ.get(TENANT_SALT_VAR, "").strip()
    # A development default must not reach a deployment that has not declared itself
    # one. First statement after the read, before any interpretation: this makes the
    # loader strictly stricter and leaves its "never returns a default" promise intact.
    refuse_dev_value_outside_dev(TENANT_SALT_VAR, raw)

    if not raw:
        raise DeploymentNotDeclared(
            TENANT_SALT_VAR,
            "this deployment has not declared the per-tenant secret that MOS-EVID-010's "
            "patient_key and MOS-TRAIN-089's institution_key are HMAC'd under. Chapter "
            "8's tenant keyring is not built (medos/medos/evidence/repo.py reports it), and a "
            "draw that invented a salt would mint surrogates no later draw could "
            "reproduce and an institution_key every deployment could compute. Set it to "
            "a secret this tenant holds and does not rotate",
        )
    return raw.encode("utf-8")


def _idempotency_key(
    request: Request, response: Response, supplied: str | None
) -> Response | None:
    """`MOS-API-022`'s optional-row half, honoured rather than ignored.

    "Other unsafe methods MAY accept the header; when absent the server MUST generate one
    and MUST echo it in `MedicalOS-Idempotency-Key`." Neither `R1` nor `R3` is in that
    requirement's **required** list (rows 15, 29, 36, 38, 42, 46, 51, 55, 62, 68b, 71a),
    so neither may answer `400 IDEMPOTENCY_KEY_REQUIRED` -- doing so would enforce a rule
    chapter 10 has not written and break a conforming client. What is owed is the echo,
    and it was owed by `R19`, `R26` and `R29` too, which have been serving without it.

    Returns a `Response` only when the SUPPLIED key is malformed: `MOS-API-023` fixes the
    grammar and a key outside it cannot be matched against a stored one later, so
    accepting it would be worse than refusing it. Otherwise it sets the echo header and
    returns `None`. It does NOT make the operation replay-safe: there is no column on
    `harvest_batches` or `curation_decisions` to store a key against, that column is
    chapter 12's, and a header echoed is not a header remembered. The module docstring
    states the exposure per row.
    """
    key = (supplied or "").strip()
    if supplied is not None and not _IDEMPOTENCY_KEY_RE.match(key):
        return _malformed(
            request,
            "IDEMPOTENCY_KEY_MALFORMED",
            "Idempotency-Key MUST match ^[A-Za-z0-9._~-]{1,255}$ (MOS-API-023).",
        )
    response.headers["MedicalOS-Idempotency-Key"] = key or f"ik_{secrets.token_hex(16)}"
    return None


def _deployment_problem(request: Request, exc: DeploymentNotDeclared) -> Response:
    """`503`, class `system_failure`, `retryable: false`. A platform fault, not a mistake.

    `MOS-UI-160` separates a refusal the operator caused from one the deployment caused,
    and this is squarely the second: nothing the caller can change in the request body
    will make a deployment that never said where it writes manifests able to write one.
    The default class for `503` is `transport_failure`, which carries `retryable: true`;
    that would be a lie, so the class is passed explicitly. The same shape
    `routes_training.py::_environment_problem` uses, for the same reason.
    """
    return _problem(
        request,
        status=503,
        code="DEPLOYMENT_NOT_DECLARED",
        title="This deployment has not declared something only it can declare",
        detail=f"{exc.variable} {exc.detail}",
        problem_class="system_failure",
        retryable=False,
        environment_variable=exc.variable,
    )


# =====================================================================================
# Authorisation
# =====================================================================================
def _require(request: Request, *permissions: str) -> Response | None:
    """`403` unless the credential's scope carries EVERY permission named.

    Variadic because of exactly one row: `MOS-API-005` admits one permission per route
    and `MOS-UI-130` makes `R26` one action spanning two.
    `medos/api/v1/routes.train.yaml` records the second under `also_requires:` and
    `medos/tools/permcheck.py` holds it to the same two
    rules as `permission:` -- it must be a catalogue key and its class must be one
    `MOS-UI-102` admits on this surface. Here it is CHECKED, which is the half a registry
    entry cannot do.

    `medos/medos/security/scopes.py` explains why a scope entry is a CEILING and not a grant
    (`MOS-SEC-027`, `MOS-SEC-045`): the effective set is the intersection of the
    credential's scope, the principal's role grants and the manifest's declared
    permissions, and this platform has no roles table. What is enforced is the term that
    exists, stated rather than dressed up -- the posture `routes_training.py` and
    `routes_reviews.py` both take, so the codebase has one answer and not three.
    """
    principal = principal_of(request)
    if principal is None:
        # Unreachable behind `AuthenticationMiddleware`. Present because a handler that
        # assumes a principal grants access if the middleware is mounted out of order.
        return _problem(
            request,
            status=401,
            code="UNAUTHENTICATED",
            title="Unauthenticated",
            detail="every request reaching this endpoint resolves to one principal",
            problem_class="authz_error",
        )
    for permission in permissions:
        if permission not in PERMISSIONS:  # pragma: no cover - a typo, caught at review
            raise RuntimeError(f"{permission!r} is not a permission this module declares")
        if not scope_permits(principal.scope, permission):
            return _problem(
                request,
                status=403,
                code="PERMISSION_DENIED",
                title="Permission denied",
                detail=(
                    f"this credential does not carry {permission!r} "
                    f"({PERMISSIONS[permission]})"
                ),
                problem_class="authz_error",
                required_permission=permission,
                # MOS-UI-130 forbids the CONSOLE exposing that the seal spans two
                # operations; it does not forbid the API telling a caller which grant it
                # is missing, and a 403 that named only one of two would send an
                # integrator round the loop twice.
                required_permissions=list(permissions),
            )
    return None


def _tenant_of(request: Request) -> str:
    principal = principal_of(request)
    assert principal is not None
    return str(principal.tenant_id)


# =====================================================================================
# Request models. The absences are the contract; see each schema for the requirement.
# =====================================================================================
class _FacetSelection(BaseModel):
    """`MOS-UI-111` rendered structurally: ONE member, and it holds chosen values.

    There is no `exclude`, no `min`, no `max`, no `pattern` and no operator, because that
    requirement names a query language, a free-text search, a regular expression, a
    boolean expression builder and a saved-query editor and forbids each one. A wire form
    that accepted an operator would leave the rule to the browser; this one has no member
    in which one could be written.
    """

    model_config = ConfigDict(extra="forbid")

    include: list[str | int | float | bool] = Field(min_length=1)


class _Strata(BaseModel):
    """`MOS-UI-112`'s facet table, CLOSED: "exactly the fields that the seal-time checks
    and the split generator read, and nothing else". The four required members are
    `MOS-TRAIN-083`'s minimum and the same four `0011_curation.up.sql` asserts with
    `strata ?& array[...]`; that CHECK admits any further key and this model does not,
    because the database is guarding the floor and `MOS-UI-112` is fixing the ceiling."""

    model_config = ConfigDict(extra="forbid")

    score_band: _FacetSelection
    review_outcome: _FacetSelection
    ran_on_platform: _FacetSelection
    acquisition_bucket: _FacetSelection
    modality: _FacetSelection | None = None
    body_part_examined: _FacetSelection | None = None
    institution_key: _FacetSelection | None = None
    manufacturer: _FacetSelection | None = None
    manufacturer_model_name: _FacetSelection | None = None
    convolution_kernel_class: _FacetSelection | None = None
    slice_thickness_mm: _FacetSelection | None = None
    pixel_spacing_mm: _FacetSelection | None = None
    contrast_phase: _FacetSelection | None = None
    study_year: _FacetSelection | None = None
    patient_age_years: _FacetSelection | None = None
    patient_sex: _FacetSelection | None = None
    z_coverage_mm: _FacetSelection | None = None
    instance_count: _FacetSelection | None = None


class SamplingPlanDeclareRequest(BaseModel):
    """Row `R19`. `MOS-TRAIN-083`'s plan, declared before any candidate is drawn.

    `min_naive_fraction` and `de_novo_control_fraction` are NOT members:
    `MOS-TRAIN-083` sets the first floor and `MOS-TRAIN-101` the second at 0.100, and a
    client that may supply either may supply zero. The columns carry those defaults and
    `0011_curation.up.sql` CHECKs the second of them.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: Annotated[str, Field(pattern=_CAPABILITY_RE)]
    plan_version: Annotated[int, Field(ge=1)]
    strata: _Strata


class HarvestBatchCreateRequest(BaseModel):
    """Row `R1`. Three ids, and the two absences are the specification.

    `tenant_id` is NOT a member -- it is bound from the credential, and a `tenant_id` in a
    request body is the shape `MOS-API-041`'s cross-tenant refusal exists to prevent.
    `opened_by` is NOT a member either: `MOS-TRAIN-080`'s named human is worth nothing if
    the caller may name someone else.

    There is no candidate payload and no `patients`, `studies` or `series` array, because
    `MOS-TRAIN-078` makes the harvest produce the rows and `MOS-TRAIN-084` requires the
    `acquisition_profile` "copied from the source header without imputation" -- a client
    that supplied a profile would be supplying an assertion where the requirement demands
    a record. See `THE DRAW` in the module docstring for where they come from instead.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: Annotated[str, Field(pattern=_CAPABILITY_RE)]
    sampling_plan_id: Annotated[str, Field(pattern=_UUID_RE)]
    training_data_policy_id: Annotated[str, Field(pattern=_UUID_RE)]


class CurationDecisionRequest(BaseModel):
    """Row `R3`. ONE decision, on the candidate the PATH names.

    `candidate_id` is not a member -- two places to name the candidate is one place to
    disagree -- and `decided_by` is not a member, because `MOS-TRAIN-080`'s named human is
    the authenticated principal. There is no array member of any kind: `MOS-UI-119`
    forbids a control that writes decisions "without the cases having been shown", and the
    strongest form of that rule is a wire shape in which a second candidate cannot be
    written.
    """

    model_config = ConfigDict(extra="forbid")

    decision: Literal["include", "exclude", "defer"]
    reason_code: (
        Literal[
            "quality_artefact",
            "wrong_anatomy",
            "wrong_phase",
            "prior_treatment",
            "duplicate_patient",
            "geometry_unsupported",
            "annotation_infeasible",
            "out_of_scope",
        ]
        | None
    ) = None
    note: Annotated[str, Field(max_length=4096)] | None = None
    review_seconds: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def _reason_pairs_with_exclude(self) -> CurationDecisionRequest:
        """`curation_decisions_reason_ck` as a request rule, in BOTH directions.

        The database CHECK is `(decision = 'exclude') = (reason_code IS NOT NULL)` and it
        is the binding constraint. This exists so the caller gets `MOS-TRAIN-203`'s
        sentence and a per-pointer violation (`MOS-API-008`) rather than a 23514 with a
        constraint name in it -- and so that the *other* direction is refused too: a
        `reason_code` on an `include` is an exclusion reason attached to a case that was
        not excluded, and `MOS-TRAIN-081` reproduces those reasons in every
        `ValidationReport` citing the cohort.
        """
        if self.decision == "exclude" and self.reason_code is None:
            raise ValueError(
                "reason_code is required when decision is exclude (MOS-TRAIN-203): "
                "every excluded candidate is retained WITH a reason, because an "
                "exclusion with no reason is an absence"
            )
        if self.decision != "exclude" and self.reason_code is not None:
            raise ValueError(
                "reason_code is admitted only on an exclusion (MOS-TRAIN-080, "
                "curation_decisions_reason_ck)"
            )
        return self


class _PolicyScope(BaseModel):
    """`MOS-TRAIN-073`'s `scope`, all five keys REQUIRED.

    An absent key is not an open bound: `MOS-TRAIN-075` refuses "a harvest for any study
    outside `scope`", which is unanswerable against a missing key, and `date_to: null` is
    how open-ended is written. The column's CHECK requires all five and so does this.
    """

    model_config = ConfigDict(extra="forbid")

    modalities: Annotated[list[Annotated[str, Field(pattern=r"^[A-Z]{2,16}$")]],
                          Field(min_length=1)]
    body_parts: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1)]
    capabilities: Annotated[
        list[Annotated[str, Field(pattern=_CAPABILITY_RE)]], Field(min_length=1)
    ]
    date_from: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None
    date_to: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")] | None


class _PolicyTerms(BaseModel):
    """`MOS-TRAIN-073`'s field table, minus the two the platform fills.

    `recorded_by` is absent because the named human is the credential. `revoked_at` and
    `revocation_reason` are absent because a withdrawal is `training_use_allowed: false`
    plus a reason, not an edit to the instrument -- the row's sealing trigger forbids
    changing its terms after the fact, so a correction is a new row.

    `legal_basis` and `basis_reference` are carried and NOT assessed. `MOS-TRAIN-074`:
    the platform "MUST store, digest, display and refuse-without" and MUST NOT interpret
    them, validate them against a registry or assess their sufficiency. A platform that
    validated an approval number would have quietly assumed an assertion that is the
    site's to make.
    """

    model_config = ConfigDict(extra="forbid")

    legal_basis: Literal[
        "broad_consent",
        "research_ethics_approval",
        "public_corpus_licence",
        "data_processing_agreement",
        "national_derogation",
    ]
    basis_reference: Annotated[str, Field(min_length=1, max_length=512)]
    basis_document_digest: (
        Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")] | None
    ) = None
    scope: _PolicyScope
    permits_redistribution: bool
    expires_at: datetime | None = None


class TrainingDataPolicyPutRequest(BaseModel):
    """Row `R4`. The flag and the instrument in ONE body, because they are one fact.

    `MOS-TRAIN-072` puts `training_use_allowed` on the tenant and `MOS-TRAIN-073` makes
    the instrument its precondition; `0011_curation.up.sql` binds them with a DEFERRABLE
    INITIALLY DEFERRED constraint trigger so both can be written in one transaction. Two
    routes -- one for the flag and one for the instrument -- would be two calls that can
    half-succeed, and the half that succeeded would be the flag.
    """

    model_config = ConfigDict(extra="forbid")

    training_use_allowed: bool
    policy: _PolicyTerms | None = None
    revocation_reason: Annotated[str, Field(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def _the_flag_decides_which_half_is_required(self) -> TrainingDataPolicyPutRequest:
        """`MOS-TRAIN-073`: setting the flag true MUST require the instrument; setting it
        false is a withdrawal and `MOS-TRAIN-076` wants the reason recorded, because "a
        withdrawal with no recorded reason is indistinguishable from an accident"."""
        if self.training_use_allowed:
            if self.policy is None:
                raise ValueError(
                    "policy is required when training_use_allowed is true "
                    "(MOS-TRAIN-073): the flag MUST NOT be set without the instrument, "
                    "and there is no administrator bypass"
                )
            if self.revocation_reason is not None:
                raise ValueError(
                    "revocation_reason is a withdrawal field and is admitted only when "
                    "training_use_allowed is false (MOS-TRAIN-076)"
                )
        else:
            if self.revocation_reason is None:
                raise ValueError(
                    "revocation_reason is required when training_use_allowed is false "
                    "(MOS-TRAIN-076)"
                )
            if self.policy is not None:
                raise ValueError(
                    "policy is admitted only when training_use_allowed is true; a "
                    "withdrawal does not re-state the terms it withdraws"
                )
        return self


class SealRunSubmitRequest(BaseModel):
    """Row `R26`. ONE MEMBER, and the absences are the specification.

    `assignments`, `partitions`, `fractions`, `seed`, `stratification_waivers`,
    `preprocessing_spec`, `deidentification_status`, `deid_policy_id`,
    `uid_mapping_table_id`, `source_description`, `evidence_kind` and `envelope` are all
    absent; `medos/schemas/evidence/seal-run-submit-request-1.0.0.json` names the requirement
    that removes each one. `extra="forbid"` is what makes the list structural rather than
    a comment.
    """

    model_config = ConfigDict(extra="forbid")

    dataset_id: Annotated[str, Field(pattern=_UUID_RE)]


class _Reader(BaseModel):
    """`MOS-UI-124`'s structured inputs. `tool` is NOT a member: that requirement has the
    platform fill it from the deployed annotation stack. `reader_id` is a User principal
    and never a group, a rota, a shared login, a service account or an email address
    (`MOS-UI-123`) -- a shared account makes `MOS-EVID-038` unsatisfiable after the
    fact, and the uuid pattern is what stops one being typed."""

    model_config = ConfigDict(extra="forbid")

    reader_id: Annotated[str, Field(pattern=_UUID_RE)]
    role: Literal["radiologist", "resident", "algorithm", "registry_extract"]
    years_experience: Annotated[int, Field(ge=0)] | None = None
    board_certified: bool | None = None
    specialty: str | None = None
    instructions_uri: str | None = None
    blinded_to: list[str] | None = None


class AnnotationSetFreezeRequest(BaseModel):
    """Row `R29`. `MOS-UI-129`'s close-the-campaign, with no per-case payload."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=200)]
    capability_id: Annotated[str, Field(pattern=_CAPABILITY_RE)]
    label_definition_id: Annotated[str, Field(pattern=_UUID_RE)]
    annotation_type: Literal["mask", "bounding_box", "point", "case_label", "measurement"]
    consensus_rule: Literal[
        "single_reader", "majority_at_least_2", "union", "intersection", "staple",
        "arbitrated",
    ]
    consensus_params: dict[str, Any] | None = None
    readers: Annotated[list[_Reader], Field(min_length=1)]
    reference_of_record: bool


# =====================================================================================
# Projections. One function per response schema, so the wire shape is greppable.
# =====================================================================================
#: Columns of `harvest_candidates` that `medos/schemas/training/harvest-candidate-1.0.0.json`
#: deliberately does NOT project, with the requirement that keeps each off the wire.
#: Named rather than filtered by a whitelist so that a new column arrives as a decision
#: instead of silently appearing in every client's response.
_CANDIDATE_NOT_PROJECTED: Final[tuple[str, ...]] = (
    "instance_uids",  # projected as a COUNT: a page the size of the cohort is not a page
    "note",           # MOS-UI-120: free text on one decision, not a list-screen member
    "decided_by",     # MOS-TRAIN-080's named human belongs on R2, the single decision
)


def _plan_view(plan: cur.SamplingPlanRow) -> dict[str, Any]:
    """One `sampling_plans` row as `SamplingPlan`. Rows R19, R20 and R21."""
    return json_safe(  # type: ignore[no-any-return]
        {
            "sampling_plan_id": plan.id,
            "capability_id": plan.capability_id,
            "plan_version": plan.plan_version,
            "strata": plan.strata,
            "min_naive_fraction": plan.min_naive_fraction,
            "de_novo_control_fraction": plan.de_novo_control_fraction,
            "spec_digest": plan.spec_digest,
            "sealed_at": plan.sealed_at,
            "created_at": plan.created_at,
        }
    )


def _policy_view(
    policy: train_policy.PolicyRow | None, *, training_use_allowed: bool
) -> dict[str, Any]:
    """The tenant's flag and its live instrument as `TrainingDataPolicy`. Rows R4 and R18.

    ONE function for both rows. The read and the write MUST render the same document or a
    console that painted the first screen from `R18` and then re-painted it from `R4`'s
    response would show two different states for one fact -- which is `MOS-UI-116`'s
    argument about the split preview applied to the policy.

    `policy: null` is the answer for a tenant with no instrument, never a `404`:
    `MOS-UI-110` requires the console's first screen to render `training_use_allowed =
    false` under the refusal contract, and the absence of an instrument is the state that
    screen has to paint.
    """
    if policy is None:
        return {"training_use_allowed": training_use_allowed, "policy": None}
    return json_safe(  # type: ignore[no-any-return]
        {
            "training_use_allowed": training_use_allowed,
            "policy": {
                "training_data_policy_id": policy.id,
                "legal_basis": policy.legal_basis,
                "basis_reference": policy.basis_reference,
                "basis_document_digest": policy.basis_document_digest,
                "scope": policy.scope,
                "permits_redistribution": policy.permits_redistribution,
                "recorded_by": policy.recorded_by,
                "recorded_at": policy.recorded_at,
                "expires_at": policy.expires_at,
                "revoked_at": policy.revoked_at,
                "revocation_reason": policy.revocation_reason,
            },
        }
    )


def _decision_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `curation_decisions` row as `CurationDecision`. Rows R2 and R3.

    `auto_excluded_predicate_id` and its digest live on the CANDIDATE and are projected
    here, together, or not at all: `medos/schemas/training/curation-decision-1.0.0.json` makes
    each `dependentRequired` on the other, and `MOS-TRAIN-080` requires a mechanical
    exclusion to be "visible in the queue as an exclusion rather than an absence" -- a
    predicate id with no digest is an exclusion nobody can reproduce.
    """
    out: dict[str, Any] = {
        "curation_decision_id": str(row["id"]),
        "harvest_candidate_id": str(row["candidate_id"]),
        "decision": row["decision"],
        "reason_code": row["reason_code"],
        "note": row["note"],
        "review_seconds": int(row["review_seconds"]),
        "decided_by": str(row["decided_by"]),
        "decided_at": row["decided_at"],
    }
    predicate = row.get("auto_excluded_predicate_id")
    digest = row.get("auto_excluded_predicate_digest")
    if predicate and digest:
        out["auto_excluded_predicate_id"] = str(predicate)
        out["auto_excluded_predicate_digest"] = str(digest)
    return json_safe(out)  # type: ignore[no-any-return]


#: The two keys `_record_stratification` folds into the `checks` column alongside `C1`-`C7`
#: and that `CorpusStratificationReport` does not carry. Named rather than filtered by a
#: `C\d` match, so a third key arrives as a decision instead of being silently dropped.
_CHECKS_COLUMN_EXTRAS: Final[tuple[str, ...]] = ("evidence_kind", "waivers")


def _stratification_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `corpus_stratification_reports` row as `CorpusStratificationReport`. Row R5.

    TWO DIVERGENCES BETWEEN THE ENGINE AND THIS ROW'S SCHEMA, RESOLVED IN THE ONLY
    DIRECTION THAT LOSES NOTHING, AND BOTH REPORTED RATHER THAN PAPERED OVER.

      1. `medos/medos/evidence/stratification.py::CheckResult.as_dict()` emits `id` and
         `detail`; `medos/schemas/training/corpus-stratification-report-1.0.0.json` closes the
         check object at `{statistic, observed, bound, outcome, waiver?}`. `id` is dropped
         because the KEY carries it -- `checks.C1.id = "C1"` is the same fact twice --
         and `detail` is dropped because the schema has no member for it. That is a real
         loss against `MOS-TRAIN-088`'s "full result and not the verdict", and it is the
         schema's to widen, not this file's.
      2. `outcome` is `pass | warn | fail | n/a` in the schema and the engine also emits
         `waived`. A waived check is projected as `warn` WITH its `waiver` object
         attached, because `MOS-TRAIN-091` makes a waiver something that "MUST NOT block"
         and "MUST be reproduced in full" -- which is exactly what `warn` plus the object
         says. Projecting it as `pass` would erase the waiver's whole point.

    `observed` and `bound` are carried through EXACTLY as the engine computed them. The
    schema types both as `number` and the engine emits an object for `C4`
    (`{distinct, p90_over_p10}` against `{min_distinct, max_p90_over_p10}`) and `null` for
    `C5` when no thin-slice subset applies, so those rows do not satisfy their own schema.
    Coercing them would be the worse defect: a reader cannot see the margin
    `MOS-TRAIN-088` asks for in a number that was invented to fit a type. The integration
    test asserts the divergence by name so that fixing the schema turns it red.
    """
    stored = dict(row["checks"])
    checks: dict[str, Any] = {}
    for check_id, value in stored.items():
        if check_id in _CHECKS_COLUMN_EXTRAS:
            continue
        entry = dict(value)
        outcome = str(entry.get("outcome", ""))
        projected: dict[str, Any] = {
            "statistic": entry.get("statistic", ""),
            "observed": entry.get("observed"),
            "bound": entry.get("bound"),
            "outcome": "warn" if outcome == "waived" else outcome,
        }
        waiver = _waiver_for(check_id, stored.get("waivers") or [])
        if waiver is not None:
            projected["waiver"] = waiver
        checks[check_id] = projected
    return json_safe(  # type: ignore[no-any-return]
        {
            "corpus_stratification_report_id": str(row["id"]),
            "harvest_batch_id": str(row["harvest_batch_id"]),
            "dataset_version_id": (
                str(row["dataset_version_id"]) if row["dataset_version_id"] else None
            ),
            "verdict": row["verdict"],
            "checks": checks,
            "computed_at": row["computed_at"],
        }
    )


def _waiver_for(
    check_id: str, waivers: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """`MOS-TRAIN-091`'s waiver object for one check, in the shape the schema closes on.

    Projected key by key rather than passed through, because the schema forbids an extra
    member and `MOS-EVID-036`'s waiver discipline is what the six required keys ARE.

    THE ATTRIBUTION IS WHAT MAKES IT A WAIVER, AND `observed`/`bound` ARE NOT PART OF
    THAT. `medos/medos/evidence/stratification.py::_waived` refuses a waiver missing
    `check_id`, `waived_by`, `waived_at` or `rationale` -- "a waiver missing any of those
    is not a waiver, it is an unattributed decision" -- and those four are checked here
    too. `observed` and `bound` are carried through WHATEVER they are, including `null`,
    because dropping the whole object when a statistic is null would hide the waiver, and
    `MOS-TRAIN-091` says in terms that there is no silent waiver. A null there fails the
    schema exactly as `C5`'s own null does, which is the divergence
    `medos/api/v1/routes.train.yaml` records under this row's `schema_divergence:`
    -- the same defect, not a second one,
    and visibly wrong beats invisibly absent.
    """
    attribution = ("check_id", "waived_by", "waived_at", "rationale")
    for waiver in waivers:
        if waiver.get("check_id") != check_id:
            continue
        if any(waiver.get(key) is None for key in attribution):
            return None
        return {
            **{key: waiver[key] for key in attribution},
            "observed": waiver.get("observed"),
            "bound": waiver.get("bound"),
        }
    return None


def _batch_view(batch: cur.BatchRow) -> dict[str, Any]:
    """One `harvest_batches` row as `HarvestBatch`. Rows R22 and R23."""
    return json_safe(  # type: ignore[no-any-return]
        {
            "harvest_batch_id": batch.id,
            "capability_id": batch.capability_id,
            "sampling_plan_id": batch.sampling_plan_id,
            "training_data_policy_id": batch.training_data_policy_id,
            "state": batch.state,
            "candidate_count": batch.candidate_count,
            "dataset_version_id": batch.dataset_version_id,
            "opened_by": batch.opened_by,
            "opened_at": batch.opened_at,
            "sealed_at": batch.sealed_at,
        }
    )


def _candidate_view(candidate: cur.CandidateRow, decided_at: Any) -> dict[str, Any]:
    """One candidate as `HarvestCandidate`. Row R24 -- THE ROW CLOSEST TO A PATIENT.

    Subtractive, and the subtractions are the specification. `MOS-TRAIN-079` forbids
    `PatientName`, `PatientBirthDate`, `AccessionNumber`, institution free text and every
    source-space UID on a candidate. There is no MEMBER here for the first four, and an
    earlier draft of this docstring claimed the same of the fifth -- which was a claim about
    member NAMES where the requirement is about VALUES. `study_instance_uid` and
    `series_instance_uids` are members, and they carry whatever the row holds.

    WHAT ACTUALLY PROTECTS THE VALUES, because it is not this function. `_DRAW_SELECT`
    reads `result_provenance.study_instance_uid`, and on this deployment that column
    holds `1.3.6.*` -- the TCIA source space the worker saw, because the Gateway's
    de-identification stage is not implemented for this consumer class and the record
    it wrote carries the UIDs it was given. What stops such a row becoming a candidate
    is `deid_policy_version`: a record naming no policy has been through none, and
    `add_candidate` refuses it. So `MOS-TRAIN-079` holds here through the Gateway and
    the policy version that records it, NOT through anything in this function, and
    nothing asserts the link between the two. Register entry 90.

    A namespace test was written here and removed rather than shipped. `medos.core.uids`
    mints ours as `2.25.` + an integer, so it is tempting to call anything else
    source-space -- but a UID de-identified by an external pipeline may legitimately
    carry another root, and the check refused five conforming fixtures on its first run.
    An invariant that is merely strict is not the same as the one the requirement states.

    `institution_key` is `MOS-TRAIN-089`'s HMAC and there is no display-name member to
    tempt a resolver (`MOS-UI-113`, `MOS-EVID-116`). `_CANDIDATE_NOT_PROJECTED` names the
    three columns that exist and do not travel.
    """
    assert _CANDIDATE_NOT_PROJECTED  # the tuple above is the documentation of this shape
    return json_safe(  # type: ignore[no-any-return]
        {
            "harvest_candidate_id": candidate.id,
            "harvest_batch_id": candidate.harvest_batch_id,
            "patient_key": candidate.patient_key,
            "study_instance_uid": candidate.study_instance_uid,
            "series_instance_uids": list(candidate.series_instance_uids),
            "instance_count": len(candidate.instance_uids),
            "geometry": candidate.geometry,
            "acquisition_profile": candidate.acquisition_profile,
            "institution_key": candidate.institution_key,
            "deid_policy_version": candidate.deid_policy_version,
            "ran_on_platform": candidate.ran_on_platform,
            "platform_outcome": candidate.platform_outcome,
            "review_outcome": candidate.review_outcome,
            "score_band": candidate.score_band,
            "acquisition_bucket": candidate.acquisition_bucket,
            "sampling_weight": candidate.sampling_weight,
            "corpus_generation": candidate.corpus_generation,
            "auto_excluded_predicate_id": candidate.auto_excluded_predicate_id,
            "decision": candidate.decision,
            "reason_code": candidate.reason_code,
            "decided_at": decided_at,
        }
    )


def _seal_run_view(run: seal_mod.SealRunRow) -> dict[str, Any]:
    """One `seal_runs` row as `SealRun`. Rows R26 and R27.

    `check_battery` and `pixel_evidence` travel in full. That is the whole point of the
    entity: `MOS-API-112` makes the nine rows and the pixel counters the mechanism by
    which a cohort sealed without near-duplicate detection cannot be rendered as one
    that was checked.
    """
    return json_safe(  # type: ignore[no-any-return]
        {
            "seal_run_id": run.public_id,
            "harvest_batch_id": run.harvest_batch_id,
            "dataset_id": run.dataset_id,
            "state": run.state,
            "progress": {
                "phase": run.phase,
                "patients_total": run.patients_total,
                "series_total": run.series_total,
                "series_retrieved": run.series_retrieved,
                "instances_total": run.instances_total,
                "instances_retrieved": run.instances_retrieved,
            },
            "pixel_evidence": {
                "consumer_class": run.consumer_class,
                "series_total": run.series_total,
                "series_with_pixel_digest": run.series_with_pixel_digest,
                "series_with_perceptual_hash": run.series_with_perceptual_hash,
            },
            # Nine rows always, even on the `202` where nothing has run:
            # `medos/schemas/evidence/seal-run-1.0.0.json` admits no short array, and
            # `medos.training.seal.pending_battery` explains what the rows say and what
            # is reported about the `outcome` enum having no value for *not yet*.
            "check_battery": run.check_battery or seal_mod.pending_battery(),
            "refusals": run.refusals,
            "dataset_version_id": run.dataset_version_id,
            "dataset_split_id": run.dataset_split_id,
            "manifest_digest": run.manifest_digest,
            "split_digest": run.split_digest,
            "corpus_stratification_report_id": run.corpus_stratification_report_id,
            "reused": run.reused,
            "submitted_by": run.submitted_by,
            "submitted_at": run.submitted_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
        }
    )


def _split_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `dataset_splits` row as `DatasetSplit`. Row R28, with the WHOLE report."""
    return json_safe(  # type: ignore[no-any-return]
        {
            "dataset_split_id": str(row["id"]),
            "public_id": row["public_id"],
            "dataset_version_id": str(row["dataset_version_id"]),
            "name": row["name"],
            "partition_level": row["partition_level"],
            "partitions": list(row["partitions"]),
            # PRESENT AT ZERO, NEVER OMITTED. `freeze_split` stores one key per
            # partition the assignment actually used, so a cohort with no excluded
            # patients stores three keys; `medos/schemas/evidence/dataset-split-1.0.0.json`
            # requires all four of chapter 7's vocabulary (`MOS-EVID-031`,
            # `MOS-EVID-033`) and closes the object. Filled here rather than in the
            # column, because the column is the record of what the split contains and
            # the schema is the shape a reader may rely on -- and `MOS-UI-114`'s
            # argument applies: the difference between "this split excluded nobody" and
            # "the projection dropped the key" is the difference between two remedies.
            "partition_patients": {
                name: int(dict(row["partition_patients"]).get(name, 0))
                for name in ("train", "tune", "test", "excluded")
            },
            "assignment_method": row["assignment_method"],
            "stratified_by": list(row["stratified_by"]),
            "leakage_report": _leakage_view(row["leakage_report"]),
            # 0006 spells chapter 7's `split_digest` as `manifest_digest`, and chapter 7
            # is normative on the wire. Renamed here, once, rather than in the column.
            "split_digest": row["manifest_digest"],
            "frozen_at": row["sealed_at"],
            "frozen_by": str(row["sealed_by"]),
        }
    )


def _leakage_view(report: Mapping[str, Any]) -> dict[str, Any]:
    """`LeakageReport.as_dict()` in the shape `medos/schemas/evidence/dataset-split` fixes.

    The engine writes `{L1: 'pass', ..., 'waivers': [...], 'detail': {...}}` when it has
    detail and omits `detail` when it has none; the schema REQUIRES all seven members.
    Filled with an empty object rather than left out, because `additionalProperties:
    false` plus a required member means a reader can rely on the key existing -- and a
    reader who has to handle both shapes will handle one of them wrongly.
    """
    out = {k: v for k, v in dict(report).items() if k in ("L1", "L2", "L3", "L4", "L5")}
    out["waivers"] = list(report.get("waivers") or [])
    out["detail"] = dict(report.get("detail") or {})
    return out


def _reader_id_to_user(reader_id: str) -> str:
    """`rdr_<32 hex>` back to the `User` uuid `MOS-UI-123` requires on the wire.

    THE OTHER HALF OF THE DIVERGENCE `freeze_annotation_set` records.
    `0006_evidence.up.sql` CHECKs `annotation_readers.reader_id ~ '^rdr_[a-z0-9_]{1,60}$'`
    and chapter 7 section 7.5 writes `rdr_a1`;
    `medos/schemas/evidence/annotation-set-1.0.0.json` types the same member as a uuid,
    because `MOS-UI-123` and `MOS-TRAIN-096` make a reader a MedicalOS `User` principal and
    never a group, a rota or a shared login. The column keeps its grammar, the wire keeps its
    type, and the translation is lossless in both directions -- all 32 hex digits, no
    truncation -- so `MOS-EVID-038`'s named reader is recoverable from the row and from the
    response. A value this platform did not write (chapter 7's own `rdr_a1`) is returned
    UNCHANGED rather than mangled into a uuid it is not: a reader whose id does not round-trip
    is a reader nobody can resolve, and that is worse than a schema violation a client can
    see.
    """
    body = reader_id[4:] if reader_id.startswith("rdr_") else reader_id
    if len(body) == 32:
        try:
            return str(_uuid.UUID(body))
        except ValueError:
            return reader_id
    return reader_id


def _annotation_set_view(
    row: Mapping[str, Any], readers: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """One `annotation_sets` row as `AnnotationSet`. Rows R29, R30 and R31."""
    projected = [
        {**dict(r), "reader_id": _reader_id_to_user(str(r["reader_id"]))}
        for r in readers
    ]
    readers = projected
    return json_safe(  # type: ignore[no-any-return]
        {
            "annotation_set_id": str(row["id"]),
            "public_id": row["public_id"],
            "dataset_version_id": str(row["dataset_version_id"]),
            "name": row["name"],
            "capability_id": row["capability_id"],
            "label_definition_id": str(row["label_definition_id"]),
            "annotation_type": row["annotation_type"],
            "consensus_rule": row["consensus_rule"],
            "consensus_params": row["consensus_params"],
            "reader_count": row["reader_count"],
            "readers": [dict(r) for r in readers],
            "reference_of_record": row["reference_of_record"],
            "annotation_digest": row["manifest_digest"],
            "frozen_at": row["sealed_at"],
            "frozen_by": str(row["sealed_by"]),
        }
    )


def _dataset_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `datasets` row as `Dataset`. Row 32."""
    out = dict(row)
    out["dataset_id"] = str(out.pop("id"))
    return json_safe(out)  # type: ignore[no-any-return]


def _dataset_version_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `dataset_versions` row as `DatasetVersion`. Row 37."""
    out = dict(row)
    out["dataset_version_id"] = str(out.pop("id"))
    out["dataset_id"] = str(out["dataset_id"])
    out["parent_version_id"] = (
        str(out["parent_version_id"]) if out["parent_version_id"] else None
    )
    out["sealed_by"] = str(out["sealed_by"])
    return json_safe(out)  # type: ignore[no-any-return]


def _page(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    """`MOS-API-015`'s envelope, exactly: three members and no fourth.

    `next_cursor` IS ALWAYS NULL AND `has_more` IS HONEST, for the reason
    `routes_training.py::list_training_runs` already states: `MOS-API-017` makes the
    cursor an opaque keyset over the sort key, the tie-break id and a fingerprint of the
    filter set, and implementing the continuation in the HTTP layer would be a second
    query over the same table written where CONTRACT.md section 1 forbids one. A client
    seeing `has_more: true` narrows its filters. No `total`: `MOS-API-016`.
    """
    return {
        "items": items[:limit],
        "next_cursor": None,
        "has_more": len(items) > limit,
    }


# =====================================================================================
# The draw. Row R1's second half.
# =====================================================================================
#: The platform's own record of a case the capability ran on, joined to the two rows that
#: say how it ended. One query for the whole draw, because the alternative is N+1 over a
#: cohort `MOS-TRAIN-114` sizes at a hundred and fifty patients minimum.
#:
#: `::text[]` on both UID arrays for the reason `medos/medos/training/curation.py` gives at
#: `_CANDIDATE_SELECT`: `dicom_uid` is a DOMAIN, psycopg has no loader for its array type,
#: and the column otherwise comes back as the raw Postgres array LITERAL -- a `str` whose
#: `len()` is a character count.
#:
#: EVERY TERM IS UNDER `current_tenant_id()`, ON EVERY TABLE IN THE JOIN, and not only on
#: the driving one. Row-level security is the predicate that matters, and the explicit
#: terms are the second lock: a cross-tenant cohort read is a disclosure of patient data,
#: not a bug, and a join that reached `patients` without one would resolve an MRN from
#: another tenant's archive.
_DRAW_SELECT: Final[str] = """
    SELECT p.study_instance_uid,
           p.input_series_uids::text[]   AS series_instance_uids,
           p.input_instance_uids::text[] AS instance_uids,
           p.geometry                    AS geometry,
           p.acquisition                 AS acquisition,
           p.gateway                     AS gateway,
           p.operating_threshold         AS operating_threshold,
           r.review_status               AS review_status,
           r.findings                    AS findings,
           j.state                       AS job_state,
           s.study_date                  AS study_date,
           s.deid_policy_version         AS study_deid_policy_version,
           s.pacs_backend                AS pacs_backend,
           pt.issuer_of_patient_id       AS issuer_of_patient_id,
           pt.patient_id_value           AS patient_id_value
      FROM result_provenance p
      JOIN results r
        ON r.tenant_id = current_tenant_id() AND r.id = p.result_id
      JOIN jobs j
        ON j.tenant_id = current_tenant_id() AND j.id = p.job_id
      LEFT JOIN studies s
        ON s.tenant_id = current_tenant_id()
       AND s.study_instance_uid = p.study_instance_uid
       AND s.erased_at IS NULL
      LEFT JOIN patients pt
        ON pt.tenant_id = current_tenant_id() AND pt.id = s.patient_id
     WHERE p.tenant_id = current_tenant_id()
       AND p.capability_id = %s
       AND p.provenance_redacted_at IS NULL
       AND r.superseded_by IS NULL
     ORDER BY p.study_instance_uid
"""


class _NothingWasDrawn(Exception):
    """The plan's strata selected no case. Raised to ROLL THE BATCH BACK.

    It is an exception and not a return value for one reason that is about transactions
    and not about style. `medos.db.tenancy.tenant_tx` inherits
    `psycopg.Connection.transaction()`'s semantics: "on a connection with no open
    transaction it BEGINs and COMMITs on exit". `open_batch` opens one of its own, so a
    handler that discovered the empty draw afterwards and called `conn.rollback()` would
    be rolling back nothing -- the batch row would already be committed, and the operator
    would be left with an empty `harvest_batches` row they did not ask for and cannot use.
    Raising out of an OUTER `tenant_tx` makes the inner ones savepoints and rolls the
    whole draw back as one act, which is what `MOS-TRAIN-202`'s materialised list means:
    the batch and its candidates exist together or neither does.
    """

    def __init__(self, outcome: _DrawOutcome) -> None:
        super().__init__("the draw selected no candidates")
        self.outcome = outcome


@dataclass
class _DrawOutcome:
    """What one draw did, in counts and nothing else.

    NO UIDs, NO KEYS AND NO PATIENT IDENTIFIERS ON THIS OBJECT. It is what the refusal
    detail and the log line are built from when a draw selects nothing, and the curation
    surface is the closest a patient identifier gets to a screen in this platform
    (`MOS-TRAIN-079`, `MOS-EVID-116`). A count is enough to act on: `MOS-UI-107` wants the
    remedy computed, and "142 of 160 cases were skipped because the job has not finished"
    is a remedy. "Study 1.2.826... was skipped" is a disclosure.
    """

    considered: int = 0
    drawn: int = 0
    not_drawn: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.not_drawn[reason] = self.not_drawn.get(reason, 0) + 1


def _score_band(findings: Any, operating_threshold: Any) -> str:
    """One case's `score_band`. `MOS-TRAIN-083`, and `SCORE_BANDS` is the whole vocabulary.

    The score is the largest score any finding carries, because the band is a property of
    what the capability asserted about the case and a case with one confident finding is
    not a low-scoring case. A finding with `score: null` contributes nothing rather than
    zero -- `MOS-EVID-020` again: a null is not a low score.
    """
    scores = [
        float(f["score"])
        for f in (findings or [])
        if isinstance(f, Mapping) and isinstance(f.get("score"), (int, float))
    ]
    if not scores:
        return "unscored"
    if operating_threshold is None:
        return "unbanded_no_operating_point"
    return (
        "below_operating_point"
        if max(scores) < float(operating_threshold)
        else "at_or_above_operating_point"
    )


def _acquisition_of(
    row: Mapping[str, Any], *, institution_key: str, study_year: int | None
) -> cur.CandidateAcquisition:
    """`MOS-TRAIN-084`'s thirteen fields from `result_provenance.acquisition`.

    ABSENT IS `None` AND NEVER A DEFAULT (`MOS-EVID-020`). Four of the thirteen have no
    source in this platform's provenance record and are therefore `None` on every
    candidate this draw produces -- `convolution_kernel_class`, `exposure_mas`,
    `iterative_recon_strength` and `station_key`. Each is a REPORTED gap and not a
    rounding: `convolution_kernel_class` is read by the `C3` reconstruction-diversity
    check of `MOS-TRAIN-088`, so a cohort drawn here cannot clear `C3` on the kernel
    class, and `medos/medos/worker` recording it is chapter 5's change, not chapter 10's.

    `pixel_spacing_mm` comes from `geometry.source_pixel_spacing_mm`, which chapter 4's
    builder copies off the header. That is a recorded fact and not a derivation: the
    resampled spacing lives under `spacing_mm` and is deliberately NOT read here, because
    a stratification computed over the grid the platform chose describes the platform and
    not the archive.
    """
    acquisition = dict(row["acquisition"] or {})
    geometry = dict(row["geometry"] or {})
    spacing = geometry.get("source_pixel_spacing_mm")
    pixel_spacing: tuple[float, float] | None = None
    if isinstance(spacing, (list, tuple)) and len(spacing) == 2:
        pixel_spacing = (float(spacing[0]), float(spacing[1]))
    thickness = acquisition.get("slice_thickness_mm")
    return cur.CandidateAcquisition(
        manufacturer=_or_none(acquisition.get("manufacturer")),
        manufacturer_model_name=_or_none(acquisition.get("model")),
        convolution_kernel=_or_none(acquisition.get("kernel")),
        convolution_kernel_class=None,
        slice_thickness_mm=float(thickness) if thickness is not None else None,
        pixel_spacing_mm=pixel_spacing,
        kvp=_or_float(acquisition.get("kvp")),
        exposure_mas=None,
        contrast_phase=_or_none(acquisition.get("contrast_phase")),
        iterative_recon_strength=None,
        station_key=None,
        institution_key=institution_key,
        study_year=study_year,
    )


def _or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _or_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _facet_admits(strata: Mapping[str, Any], facet: str, value: Any) -> bool:
    """Does the plan's facet admit this realised value?

    A facet the plan does not declare admits everything -- `MOS-UI-111` makes a constraint
    something the operator EXPRESSED by choosing values, and a facet nobody chose is not a
    constraint. A facet that IS declared admits only what its `include` list carries,
    which is the whole of `MOS-UI-111`: "every constraint MUST be expressed by choosing
    among values the archive actually contains", and there is no operator, no negation and
    no pattern in which anything else could be said.
    """
    selection = strata.get(facet)
    if not isinstance(selection, Mapping):
        return True
    include = selection.get("include")
    if not isinstance(include, list):
        return True
    return value in include


def _draw_candidates(
    conn: psycopg.Connection[Any],
    *,
    batch_id: str,
    plan: cur.SamplingPlanRow,
    salt: bytes,
) -> _DrawOutcome:
    """Materialise the batch's candidates. `MOS-TRAIN-078`, `MOS-TRAIN-202`, `MOS-UI-118`.

    "The harvest MUST produce `HarvestCandidate` rows, never a `DatasetVersion` directly"
    and "an explicit, materialised list of candidate triples with a per-candidate
    disposition". `MOS-EVID-016` forbids a cohort defined by a live query, so this runs
    ONCE, at open, and writes rows -- it is not a saved filter and re-opening a batch
    against the same plan draws a second, independent set.

    `medos.training.curation.add_candidate` is the only writer, and it re-runs
    `MOS-TRAIN-075`'s per-study scope test on every row: a study outside the policy's
    modalities, body parts, capabilities or date range raises `HarvestRefused` and is
    counted under `out_of_scope` rather than drawn. The scope test is NOT reimplemented in
    the SQL above, for `MOS-UI-116`'s reason one layer down -- two implementations of one
    bound disagree exactly where the bound matters.

    `sampling_weight` IS `1.0` AND THAT IS A STATEMENT, NOT A PLACEHOLDER.
    `MOS-TRAIN-083` requires "the realised per-case sampling weight" on the candidate, and
    the draw is exhaustive within the stratum the plan declared: every case whose realised
    facet values are in the plan's `include` lists is drawn, so the inclusion probability
    is one and the weight is its reciprocal. The requirement's own worry -- that sampling
    is "everything the service ran on" -- is answered by the `include` lists being a
    proper subset, not by discarding cases at random inside them. The consequence to be
    honest about is that `MOS-TRAIN-083`'s "lowest `score_band` sampling fraction >= 3x
    the top band" is then a RATIO OF ONE, satisfiable only by a plan that declares the
    low band and not the top one; see `SCORE_BANDS` for why this deployment cannot tell
    the two apart at all.
    """
    outcome = _DrawOutcome()
    with tenant_tx(conn) as tx:
        rows = tx.execute(_DRAW_SELECT, (plan.capability_id,)).fetchall()

    strata = dict(plan.strata)
    for raw in rows:
        row = dict(raw)
        outcome.considered += 1

        job_state = str(row["job_state"])
        if job_state not in _TERMINAL_JOB_STATES:
            outcome.skip("the job has not reached a terminal state")
            continue
        if row["patient_id_value"] is None:
            # MOS-EVID-010's surrogate is an HMAC over the tenant-scoped natural key, and
            # `MOS-TRAIN-112` groups the split BY PATIENT. A candidate with no resolvable
            # patient cannot be assigned to a partition without risking the same person in
            # two of them, which is the leak L1 exists to catch.
            outcome.skip("no projection row resolves the study to a patient")
            continue
        deid_version = (dict(row["gateway"] or {}).get("deid_policy_version")
                        or row["study_deid_policy_version"])
        if deid_version is None:
            # MOS-TRAIN-069 and MOS-TRAIN-079: the candidate records the policy version it
            # was de-identified under. A cohort that cannot say which profile produced it
            # cannot be re-derived, and a default would be a claim about somebody's PHI.
            outcome.skip("the record does not name a de-identification policy version")
            continue

        acquisition_source = dict(row["acquisition"] or {})
        study_date = row["study_date"]
        study_year = int(study_date.year) if study_date is not None else None
        institution_key = ev_digest.institution_key(salt, str(row["pacs_backend"] or ""))
        acquisition = _acquisition_of(
            row, institution_key=institution_key, study_year=study_year
        )
        profile = acquisition.as_json()

        realised = {
            "score_band": _score_band(row["findings"], row["operating_threshold"]),
            "review_outcome": str(row["review_status"]),
            "ran_on_platform": True,
            "acquisition_bucket": split_mod.stratum_of(profile)["slice_thickness_band"],
            "modality": _or_none(acquisition_source.get("modality")),
            "body_part_examined": _or_none(acquisition_source.get("body_part")),
            "institution_key": institution_key,
            "manufacturer": acquisition.manufacturer,
            "manufacturer_model_name": acquisition.manufacturer_model_name,
            "convolution_kernel_class": acquisition.convolution_kernel_class,
            "slice_thickness_mm": acquisition.slice_thickness_mm,
            "contrast_phase": acquisition.contrast_phase,
            "study_year": study_year,
        }
        rejected = [f for f, v in realised.items() if not _facet_admits(strata, f, v)]
        if rejected:
            outcome.skip(f"outside the plan's {sorted(rejected)[0]} selection")
            continue

        try:
            cur.add_candidate(
                conn,
                batch_id=batch_id,
                # The MRN is read from `patients` to be HMAC'd and is never bound to a
                # name here, never logged and never returned: MOS-EVID-010's construction
                # needs the natural key and MOS-TRAIN-079 forbids it on the row.
                patient_key=ev_digest.patient_key(
                    salt,
                    str(row["issuer_of_patient_id"] or ""),
                    str(row["patient_id_value"]),
                ),
                study_instance_uid=str(row["study_instance_uid"]),
                series_instance_uids=list(row["series_instance_uids"] or []),
                instance_uids=list(row["instance_uids"] or []),
                geometry=dict(row["geometry"] or {}),
                acquisition=acquisition,
                institution_key=institution_key,
                deid_policy_version=int(deid_version),
                sampling_weight=1.0,
                ran_on_platform=True,
                platform_outcome=job_state,
                review_outcome=realised["review_outcome"],
                score_band=realised["score_band"],
                acquisition_bucket=realised["acquisition_bucket"],
                # MOS-TRAIN-087: a case whose annotation was not seeded by a model is
                # generation 0. This platform seeds none -- there is no annotation server
                # (chapter 19 section 19.4.3) -- so 0 is a fact and not an optimistic
                # default. When one is built, this value comes from the seeding model's
                # own corpus and not from here.
                corpus_generation=0,
                study={
                    "modality": realised["modality"],
                    "body_part_examined": realised["body_part_examined"],
                    "capability_id": plan.capability_id,
                    "study_date": study_date.isoformat() if study_date else None,
                },
            )
        except HarvestRefused as exc:
            # MOS-TRAIN-075's per-study scope test, answered by the engine. The candidate
            # is NOT drawn and the refusal is counted; it is not an error, it is the
            # policy's bound doing its job one case at a time.
            outcome.skip(
                "outside the TrainingDataPolicy scope "
                f"({','.join(sorted({r.code for r in exc.refusals}))})"
            )
            continue
        except psycopg.errors.UniqueViolation:
            # `harvest_candidates_uk UNIQUE (harvest_batch_id, study_instance_uid)`. Two
            # capabilities ran on one study, so the join produced it twice; the batch is
            # a list of STUDIES and the second row is the same study.
            #
            # NO `conn.rollback()` HERE, and that is the whole point of the outer
            # transaction the caller opens. `add_candidate`'s own `tenant_tx` is a
            # SAVEPOINT inside it, and the savepoint is already rolled back by the time
            # this line runs; a connection-level rollback would discard the batch and
            # every candidate drawn before this one, turning one duplicated study into a
            # silently truncated cohort.
            outcome.skip("the study is already in this batch under another result")
            continue
        outcome.drawn += 1
    return outcome


# =====================================================================================
# R1  POST /api/v1/harvest-batches
# =====================================================================================
@router.post("/harvest-batches", status_code=201, summary="Open a harvest batch")
def open_harvest_batch(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    body: HarvestBatchCreateRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """The operator's first step on the cohort, and the one this surface was missing.

    `MOS-TRAIN-072` IS ASKED BEFORE THE BODY IS READ, and the refusal it produces is the
    one `MOS-UI-110` puts on the console's first screen. `assert_harvest_permitted` is
    called here AND again inside `open_batch` AND once per candidate inside
    `add_candidate`; that is not redundancy, it is three different questions --
    may this tenant contribute at all, is there a live instrument behind the flag, and is
    THIS study inside the declared scope.

    THE BODY NAMES THE INSTRUMENT AND THE PLATFORM CHOOSES IT. `open_batch` records the
    tenant's LIVE policy on the batch, so a `training_data_policy_id` that is not the live
    one is refused rather than honoured: `MOS-TRAIN-076` needs the batch to name the
    instrument it was actually drawn under, and a caller that could name a revoked one
    could produce a batch whose provenance says it predates its own revocation.

    A DRAW THAT SELECTED NOTHING IS A REFUSAL AND NOT AN EMPTY `201`. A batch with no
    candidates is not a cohort under construction, and `HarvestBatch` has no member in
    which "and here is why nothing matched" could be said (`additionalProperties: false`).
    `MOS-UI-107` requires the remedy computed rather than the constraint described, so the
    refusal carries the per-reason counts the draw accumulated -- and no UID, no key and
    no patient identifier, because those counts are read on a screen.
    """
    denied = _require(request, "harvest_batch.open")
    if denied is not None:
        return denied
    malformed = _idempotency_key(request, response, idempotency_key)
    if malformed is not None:
        return malformed

    try:
        policy = train_policy.assert_harvest_permitted(conn)
    except HarvestRefused as exc:
        return _refusal(request, exc)

    plan = cur.get_sampling_plan(conn, body.sampling_plan_id)
    if plan is None:
        return _not_found(request, "sampling_plan", body.sampling_plan_id)
    if plan.capability_id != body.capability_id:
        return _problem(
            request,
            status=409,
            code="SAMPLING_PLAN_CAPABILITY_MISMATCH",
            title="That plan was declared for another capability",
            detail=(
                f"sampling plan {body.sampling_plan_id} was declared for "
                f"{plan.capability_id!r} and this batch names {body.capability_id!r}. "
                "MOS-TRAIN-083 makes the plan the description of the draw, and a plan "
                "that describes a different capability's cohort describes nothing"
            ),
            problem_class="client_error",
        )
    if body.training_data_policy_id != policy.id:
        return _problem(
            request,
            status=409,
            code="TRAINING_POLICY_NOT_LIVE",
            title="That is not the instrument this tenant is harvesting under",
            detail=(
                "the batch records the LIVE TrainingDataPolicy, so MOS-TRAIN-076 can say "
                "which candidates predate a revocation without reconstructing the answer "
                "from timestamps. Read the live one from "
                "GET /api/v1/tenants/{tenant_id}/training-policy and name that"
            ),
            problem_class="client_error",
        )

    try:
        salt = _tenant_salt()
    except DeploymentNotDeclared as exc:
        return _deployment_problem(request, exc)

    # ONE TRANSACTION FOR THE BATCH AND ITS CANDIDATES. `tenant_tx` BEGINs here because
    # nothing is open, which makes every `tenant_tx` inside `open_batch`,
    # `add_candidate` and `set_batch_state` a SAVEPOINT rather than a commit of its own.
    # That is what makes `_NothingWasDrawn` able to undo the batch, and what makes a
    # failure half-way through the draw leave no half-drawn cohort behind.
    try:
        with tenant_tx(conn):
            batch = cur.open_batch(
                conn,
                capability_id=body.capability_id,
                sampling_plan_id=body.sampling_plan_id,
                opened_by=_actor(request).id,
            )
            outcome = _draw_candidates(conn, batch_id=batch.id, plan=plan, salt=salt)
            if outcome.drawn == 0:
                raise _NothingWasDrawn(outcome)
            # MOS-TRAIN-202: the batch is a materialised list. `SAMPLED` is the state
            # that says the draw happened, and it is written here rather than inside
            # `open_batch` because that function opens a batch and this route draws.
            cur.set_batch_state(conn, batch_id=batch.id, state="SAMPLED")
    except _NothingWasDrawn as nothing:
        log.info(
            "harvest_draw_selected_nothing",
            extra={
                "capability_id": plan.capability_id,
                "considered": nothing.outcome.considered,
                # COUNTS ONLY. MOS-TRAIN-079 and MOS-SEC-105: a log line is exactly where
                # a StudyInstanceUID or a patient surrogate leaks, and a reason string
                # with a count in it is everything an operator can act on anyway.
                "not_drawn": dict(nothing.outcome.not_drawn),
            },
        )
        return _refusal(
            request,
            CurationRefused(
                (
                    Refusal(
                        check_id="MOS-TRAIN-083",
                        code="draw_selected_no_candidates",
                        message=(
                            "the plan's strata selected none of the cases this platform "
                            "holds for this capability, so no batch was opened. Widen "
                            "the selection, or run the capability on more studies: "
                            "MOS-TRAIN-084 requires the acquisition profile copied from "
                            "the source header, and the platform holds one only for a "
                            "study it has processed"
                        ),
                        observed=dict(nothing.outcome.not_drawn),
                        bound=nothing.outcome.considered,
                    ),
                )
            ),
        )

    drawn = cur.find_batch(conn, batch.id)
    assert drawn is not None  # just written, under this tenant
    response.headers["Location"] = f"/api/v1/harvest-batches/{batch.id}"  # MOS-API-046
    return _batch_view(drawn)


# =====================================================================================
# R18  GET /api/v1/tenants/{tenant_id}/training-policy
# =====================================================================================
@router.get(
    "/tenants/{tenant_id}/training-policy",
    summary="Whether this tenant's policy permits training use",
)
def get_training_policy(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    tenant_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """The read half of `R4`, and the row `R4` could not be.

    `MOS-UI-110` requires the console's FIRST screen to render
    `training_use_allowed = false` under the refusal contract and forbids it rendering an
    HTTP status, a problem document or the string `403`. Without this row the only way to
    learn the flag is to be refused by `R1`, so the console would have to render a
    refusal it is forbidden to render or infer one it cannot distinguish from a network
    fault. So a tenant with no policy is `200` with `training_use_allowed: false` and
    `policy: null` -- NOT a `404`: the absence of an instrument is the answer, and it is
    the answer the first screen has to paint.

    The path carries a `tenant_id` and the tenant comes from the CREDENTIAL
    (`MOS-API-003`). A mismatch is `404` and never `403`: telling a caller that another
    tenant exists is the leak `MOS-SEC-072` exists to prevent, and this is the one route
    in the block whose path could be used to probe for one.
    """
    denied = _require(request, "training_data_policy.read")
    if denied is not None:
        return denied
    if tenant_id != _tenant_of(request):
        return _not_found(request, "tenant", tenant_id)

    policy = train_policy.live_policy(conn)
    if policy is None:
        return _policy_view(None, training_use_allowed=False)
    return _policy_view(
        policy, training_use_allowed=train_policy.training_use_allowed(conn)
    )


# =====================================================================================
# R4  PUT /api/v1/tenants/{tenant_id}/training-policy
# =====================================================================================
@router.put(
    "/tenants/{tenant_id}/training-policy",
    summary="Record the tenant's TrainingDataPolicy and set training_use_allowed",
)
def put_training_policy(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    tenant_id: Annotated[str, PathParam(pattern=_UUID_RE)],
    body: TrainingDataPolicyPutRequest,
) -> Any:
    """The site's legal assertion, recorded as the named human. `MOS-TRAIN-073`.

    THE ACTOR COMES FROM THE CREDENTIAL AND THE BODY HAS NO MEMBER FOR IT.
    `MOS-TRAIN-073` binds the instrument to "the named human who asserted it", and a
    `recorded_by` a caller could fill in would make that name a claim rather than a
    record. The column is sealed after insert, so the name cannot be corrected later
    either -- which is why it has to be right at insert and why it is read from the
    credential.

    ONE TRANSACTION FOR BOTH WRITES, AND THAT IS THE WHOLE REASON THE ROW EXISTS.
    `training_data_policies` and `tenants.training_use_allowed` are one fact in two
    tables; `0011_curation.up.sql` binds them with a DEFERRABLE INITIALLY DEFERRED
    constraint trigger precisely so both can be written in either order inside one
    transaction. The trigger fires at COMMIT, so a flag set true with no instrument
    surfaces from `conn.commit()` and not from the call that set it -- caught here and
    rendered as the refusal `MOS-TRAIN-073` describes, rather than as a 500.

    `PUT` AND NOT `POST`, AND THE SECOND IDENTICAL CALL IS A NO-OP. `MOS-API-022` marks
    this row `not_applicable` for `Idempotency-Key` because the METHOD is idempotent, and
    that is only true if the handler makes it so: a repeat of the same terms sets the flag
    and returns the live row rather than minting a second instrument. Terms that DIFFER
    are a `409` and never a silent replacement -- the row's sealing trigger forbids
    editing `legal_basis`, `basis_reference`, `scope`, `permits_redistribution` or
    `recorded_by` after the fact, so a correction is a withdrawal followed by a new
    instrument, by two people who each meant it.
    """
    denied = _require(request, "training_data_policy.record")
    if denied is not None:
        return denied
    if tenant_id != _tenant_of(request):
        # 404 and never 403: MOS-SEC-072. Telling a caller that another tenant exists is
        # the leak, and this path is the one that could be used to probe for one.
        return _not_found(request, "tenant", tenant_id)

    live = train_policy.live_policy(conn)
    if not body.training_use_allowed:
        assert body.revocation_reason is not None  # the model validator holds this
        try:
            with tenant_tx(conn):
                if live is not None:
                    train_policy.revoke_policy(
                        conn, policy_id=live.id, reason=body.revocation_reason
                    )
                # MOS-TRAIN-072: the flag is written even when there was no instrument to
                # withdraw. A tenant whose flag is true with no live policy cannot exist
                # at commit, but a tenant whose flag is false and has never had one can,
                # and `MOS-UI-110`'s first screen renders exactly that state. One
                # transaction again: a revocation that withdrew the instrument and left
                # the flag true is the state MOS-TRAIN-076 exists to make impossible.
                train_policy.set_training_use_allowed(conn, False)
        except HarvestRefused as exc:
            return _refusal(request, exc)
        return _policy_view(
            train_policy.live_policy(conn),
            training_use_allowed=train_policy.training_use_allowed(conn),
        )

    terms = body.policy
    assert terms is not None  # the model validator holds this
    if live is not None:
        if not _same_terms(live, terms):
            return _problem(
                request,
                status=409,
                code="TRAINING_POLICY_ALREADY_LIVE",
                title="A different instrument is already live for this tenant",
                detail=(
                    f"training_data_policy {live.id} is live under "
                    f"{live.legal_basis!r}/{live.basis_reference!r}. Its terms are "
                    "sealed after insert (MOS-TRAIN-073), so a change is a withdrawal "
                    "and a new instrument: PUT training_use_allowed false with a "
                    "revocation_reason first, then record the new one"
                ),
                problem_class="client_error",
            )
        train_policy.set_training_use_allowed(conn, True)
        conn.commit()
        return _policy_view(live, training_use_allowed=True)

    try:
        # ONE TRANSACTION, WHICH IS THE WHOLE REASON THIS IS ONE ROUTE. Without the outer
        # `tenant_tx` each engine call would COMMIT on its own -- `tenant_tx` inherits
        # `psycopg.Connection.transaction()`'s "BEGINs and COMMITs on exit" -- and the
        # instrument and the flag would be two writes that can half-succeed. The half
        # that succeeded would be the instrument, or worse, the FLAG. The deferred
        # constraint trigger of 0011_curation.up.sql exists precisely so both can be
        # written in either order inside one transaction, and it fires at the commit this
        # block performs on exit.
        with tenant_tx(conn):
            recorded = train_policy.record_policy(
                conn,
                legal_basis=terms.legal_basis,
                basis_reference=terms.basis_reference,
                scope=terms.scope.model_dump(),
                recorded_by=_actor(request).id,
                permits_redistribution=terms.permits_redistribution,
                basis_document_digest=terms.basis_document_digest,
                expires_at=terms.expires_at,
            )
            train_policy.set_training_use_allowed(conn, True)
    except HarvestRefused as exc:
        conn.rollback()
        return _refusal(request, exc)
    except psycopg.errors.IntegrityError as exc:
        # `tenants_training_policy_required` is DEFERRABLE INITIALLY DEFERRED, so this is
        # where a flag without an instrument lands -- at COMMIT, on a connection that has
        # already run both statements. Rendered as MOS-TRAIN-073's refusal rather than as
        # a 500 with a trigger name in it.
        conn.rollback()
        return _refusal(
            request,
            HarvestRefused(
                (
                    Refusal(
                        check_id="MOS-TRAIN-073",
                        code="training_policy_required",
                        message=(
                            "the database refused the instrument and the flag as a "
                            "pair. Either setting training_use_allowed true found no "
                            "live TrainingDataPolicy behind it -- MOS-TRAIN-073 requires "
                            "the instrument and there is no administrator bypass -- or "
                            "another request recorded one between this handler's read "
                            "and its write, which the live-policy partial unique index "
                            "refuses. Read the live one and try again"
                        ),
                        observed=type(exc).__name__,
                    ),
                )
            ),
        )
    return _policy_view(recorded, training_use_allowed=True)


def _same_terms(live: train_policy.PolicyRow, terms: _PolicyTerms) -> bool:
    """Is this `PUT` a repeat of the instrument already recorded?

    Compared over exactly the members the row's sealing trigger freezes, because those are
    the members a second `PUT` could not change even if it wanted to. `expires_at` is
    included although the trigger does not freeze it: `MOS-TRAIN-075` makes expiry flip
    the flag automatically, so a `PUT` that moved the expiry would be re-licensing a
    harvest, which is not a no-op whatever the other members say.
    """
    return (
        live.legal_basis == terms.legal_basis
        and live.basis_reference == terms.basis_reference
        and live.basis_document_digest == terms.basis_document_digest
        and live.scope == terms.scope.model_dump()
        and live.permits_redistribution == terms.permits_redistribution
        and live.expires_at == terms.expires_at
    )


# =====================================================================================
# R19  POST /api/v1/sampling-plans
# =====================================================================================
@router.post("/sampling-plans", status_code=201, summary="Declare a sampling plan")
def declare_sampling_plan(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    body: SamplingPlanDeclareRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """The cohort builder's only write. `MOS-TRAIN-083`, before any candidate is drawn.

    The verb is `declare` and not `create`: `MOS-SEC-034` requires a disposition for a
    `create` verb, the domain's own word for registering a plan is *declare*
    (`MOS-TRAIN-083`: "MUST declare a versioned `SamplingPlan`"), and `sampling_plan.declare`
    is the key chapter 8 registers -- so no verb had to be invented to avoid one.
    """
    denied = _require(request, "sampling_plan.declare")
    if denied is not None:
        return denied
    # MOS-API-022: this row is not in that requirement's `required` list, and the
    # sentence that DOES reach it is still a MUST -- "when absent the server MUST
    # generate one and MUST echo it in `MedicalOS-Idempotency-Key`". It was serving
    # without the echo until the change that served R1-R5.
    malformed = _idempotency_key(request, response, idempotency_key)
    if malformed is not None:
        return malformed

    # A facet the operator did not choose is ABSENT and never an empty `include`:
    # `MOS-UI-112` fixes the ceiling of the facet set and `MOS-TRAIN-083` the floor, and
    # a stratum declared with no values is a dimension the draw would divide by zero on.
    strata: dict[str, Any] = {
        name: {"include": list(facet.include)}
        for name, facet in (
            (n, getattr(body.strata, n)) for n in type(body.strata).model_fields
        )
        if facet is not None
    }
    try:
        plan_id = cur.declare_sampling_plan(
            conn,
            capability_id=body.capability_id,
            plan_version=body.plan_version,
            strata=strata,
        )
    except CurationRefused as exc:
        return _refusal(request, exc)
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        # `UNIQUE (tenant_id, capability_id, plan_version)`. MOS-STORE-359 seals the row,
        # so a second declaration at the same version is a conflict and never an update:
        # "a plan edited after the draw cannot describe the draw".
        return _problem(
            request,
            status=409,
            code="SAMPLING_PLAN_VERSION_EXISTS",
            title="A plan already exists at this version",
            detail=(
                f"capability {body.capability_id!r} already has a sampling plan at "
                f"version {body.plan_version}. MOS-STORE-359 seals the row outright, so "
                "a changed plan is a new plan_version and never an edit"
            ),
            problem_class="client_error",
        )
    conn.commit()

    plan = cur.get_sampling_plan(conn, plan_id)
    assert plan is not None  # just written, under this tenant
    response.headers["Location"] = f"/api/v1/sampling-plans/{plan_id}"  # MOS-API-046
    return _plan_view(plan)


# =====================================================================================
# R20  GET /api/v1/sampling-plans
# =====================================================================================
@router.get("/sampling-plans", summary="List sampling plans")
def list_sampling_plans(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    capability_id: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    denied = _require(request, "sampling_plan.read")
    if denied is not None:
        return denied
    rows = cur.list_sampling_plans(
        conn, capability_id=capability_id, limit=limit + 1
    )
    return _page([_plan_view(p) for p in rows], limit)


# =====================================================================================
# R21  GET /api/v1/sampling-plans/{sampling_plan_id}
# =====================================================================================
@router.get("/sampling-plans/{sampling_plan_id}", summary="Read one sampling plan")
def get_sampling_plan(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    sampling_plan_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """No PATCH and no DELETE on this resource, and both absences are `MOS-STORE-359`."""
    denied = _require(request, "sampling_plan.read")
    if denied is not None:
        return denied
    plan = cur.get_sampling_plan(conn, sampling_plan_id)
    if plan is None:
        return _not_found(request, "sampling_plan", sampling_plan_id)
    return _plan_view(plan)


# =====================================================================================
# R22  GET /api/v1/harvest-batches
# =====================================================================================
@router.get("/harvest-batches", summary="List harvest batches")
def list_harvest_batches(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    capability_id: Annotated[str | None, Query(max_length=64)] = None,
    state: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """`MOS-UI-138`'s argument, one entity earlier: a surface whose operator closes the
    tab mid-curation and cannot find the batch again has taught them to avoid the check
    just as surely as a refusal that discards work."""
    denied = _require(request, "harvest_batch.read")
    if denied is not None:
        return denied
    if state is not None and state not in cur.BATCH_STATES:
        return _malformed(
            request,
            "UNKNOWN_FILTER",
            f"state is one of {list(cur.BATCH_STATES)}",
        )
    rows = cur.list_batches(
        conn, capability_id=capability_id, state=state, limit=limit + 1
    )
    return _page([_batch_view(b) for b in rows], limit)


# =====================================================================================
# R23  GET /api/v1/harvest-batches/{harvest_batch_id}
# =====================================================================================
@router.get("/harvest-batches/{harvest_batch_id}", summary="Read one harvest batch")
def get_harvest_batch(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    harvest_batch_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    denied = _require(request, "harvest_batch.read")
    if denied is not None:
        return denied
    batch = cur.find_batch(conn, harvest_batch_id)
    if batch is None:
        return _not_found(request, "harvest_batch", harvest_batch_id)
    return _batch_view(batch)


# =====================================================================================
# R24  GET /api/v1/harvest-batches/{harvest_batch_id}/candidates
# =====================================================================================
@router.get(
    "/harvest-batches/{harvest_batch_id}/candidates",
    summary="The curation queue",
)
def list_harvest_candidates(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    harvest_batch_id: Annotated[str, PathParam(pattern=_UUID_RE)],
    decision: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """An excluded candidate is HERE, with its reason, and not absent.

    `MOS-TRAIN-080`'s queue means the drawn set and not the surviving set: the running
    exclusion tally `MOS-UI-121` requires beside the cohort size is a property of the
    batch and of the stratification report, and a console that derived it from the pages
    it had fetched would compute a different number from the one the seal computes
    (`MOS-UI-116`). Every query behind this route runs under the tenant GUC.
    """
    denied = _require(request, "harvest_candidate.read")
    if denied is not None:
        return denied
    if decision is not None and decision not in ("include", "exclude", "undecided"):
        return _malformed(
            request, "UNKNOWN_FILTER", "decision is include, exclude or undecided"
        )
    batch = cur.find_batch(conn, harvest_batch_id)
    if batch is None:
        return _not_found(request, "harvest_batch", harvest_batch_id)

    rows = cur.candidates(conn, harvest_batch_id)
    if decision == "undecided":
        rows = [c for c in rows if c.decision not in ("include", "exclude")]
    elif decision is not None:
        rows = [c for c in rows if c.decision == decision]

    decided = _decided_at(conn, [c.id for c in rows[: limit + 1]])
    return _page(
        [_candidate_view(c, decided.get(c.id)) for c in rows[: limit + 1]], limit
    )


def _decided_at(
    conn: psycopg.Connection[Any], candidate_ids: Sequence[str]
) -> dict[str, Any]:
    """`decided_at` per candidate, in ONE query.

    `medos.training.curation.CandidateRow` does not carry it and this schema requires it.
    Read here rather than added to the engine row, because the timestamp is a wire
    requirement of `medos/schemas/training/harvest-candidate-1.0.0.json` and not something any
    engine caller needs -- and one query for the page is the alternative to N+1.
    """
    if not candidate_ids:
        return {}
    with tenant_tx(conn) as tx:
        rows = tx.execute(
            "SELECT candidate_id, max(decided_at) AS decided_at FROM curation_decisions "
            "WHERE tenant_id = current_tenant_id() AND candidate_id = ANY(%s) "
            "AND decision <> 'defer' GROUP BY candidate_id",
            ([_uuid.UUID(c) for c in candidate_ids],),
        ).fetchall()
    return {str(dict(r)["candidate_id"]): dict(r)["decided_at"] for r in rows}


#: One candidate's settled decision, joined to the two predicate columns that live on the
#: candidate. `decision <> 'defer'` is `curation_decisions_settled_uk`'s own predicate, so
#: this reads the row that index makes unique and never one of a candidate's deferrals.
_DECISION_SELECT: Final[str] = """
    SELECT d.id, d.candidate_id, d.decision, d.reason_code, d.note, d.review_seconds,
           d.decided_by, d.decided_at,
           c.auto_excluded_predicate_id, c.auto_excluded_predicate_digest
      FROM curation_decisions d
      JOIN harvest_candidates c
        ON c.tenant_id = current_tenant_id() AND c.id = d.candidate_id
     WHERE d.tenant_id = current_tenant_id()
"""


def _settled_decision(
    conn: psycopg.Connection[Any], candidate_id: str
) -> dict[str, Any] | None:
    """The candidate's SETTLED decision, or `None` while it is still in the queue.

    `decision <> 'defer'` is `curation_decisions_settled_uk`'s own predicate, so this
    reads the row that index makes unique and never one of a candidate's deferrals.
    """
    with tenant_tx(conn) as tx:
        row = tx.execute(
            _DECISION_SELECT + " AND d.candidate_id = %s AND d.decision <> 'defer'",
            (_uuid.UUID(candidate_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def _decision_by_id(
    conn: psycopg.Connection[Any], decision_id: str
) -> dict[str, Any] | None:
    """One `curation_decisions` row by its own id, settled or deferred.

    `R3` renders what it just wrote and a deferral is a `CurationDecision` -- the schema's
    `decision` enum carries `defer`, and `MOS-TRAIN-080` makes a deferral a recorded act
    by a named human that simply is not terminal. Reading it back by CANDIDATE would find
    nothing, because the settled index excludes it; reading it back by its own id returns
    the row that was written, which is what a `201` is supposed to mean.
    """
    with tenant_tx(conn) as tx:
        row = tx.execute(
            _DECISION_SELECT + " AND d.id = %s", (_uuid.UUID(decision_id),)
        ).fetchone()
    return dict(row) if row is not None else None


def _candidate_exists(conn: psycopg.Connection[Any], candidate_id: str) -> bool:
    """Is there a candidate this tenant can see? Row-level security is the predicate."""
    with tenant_tx(conn) as tx:
        row = tx.execute(
            "SELECT 1 FROM harvest_candidates "
            " WHERE tenant_id = current_tenant_id() AND id = %s",
            (_uuid.UUID(candidate_id),),
        ).fetchone()
    return row is not None


# =====================================================================================
# R2  GET /api/v1/harvest-candidates/{harvest_candidate_id}/decision
# =====================================================================================
@router.get(
    "/harvest-candidates/{harvest_candidate_id}/decision",
    summary="The settled curation decision on one candidate",
)
def get_curation_decision(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    harvest_candidate_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """Bound to `harvest_candidate.read` and not to a permission of its own.

    Reading the decision IS reading the candidate's curation state: a principal who may
    see the candidate may see whether it was included, and a second key would be a second
    place for the two to disagree. `medos/api/v1/routes.train.yaml` and this handler bind the
    same one, which is what `MOS-API-089` is for.

    A CANDIDATE WITH NO SETTLED DECISION IS A `404` ON THE DECISION AND NOT AN EMPTY `200`.
    `MOS-TRAIN-080` forbids auto-inclusion, so "undecided" is backlog and never an
    implicit anything; a body with `decision: null` would be a shape a client could read
    as a value. A candidate carrying only deferrals is undecided by
    `curation_decisions_settled_uk`'s own definition and answers the same way. The two
    404s are told apart by their `code`, because the remedies differ: one is a wrong id
    and the other is a queue item nobody has reached.
    """
    denied = _require(request, "harvest_candidate.read")
    if denied is not None:
        return denied
    if not _candidate_exists(conn, harvest_candidate_id):
        return _not_found(request, "harvest_candidate", harvest_candidate_id)
    row = _settled_decision(conn, harvest_candidate_id)
    if row is None:
        return _problem(
            request,
            status=404,
            code="CURATION_DECISION_NOT_FOUND",
            title="This candidate has no settled decision",
            detail=(
                f"harvest candidate {harvest_candidate_id} is still in the queue. "
                "MOS-TRAIN-080 forbids auto-inclusion, so an undecided candidate is "
                "backlog and not an implicit include; a deferral is not a settled "
                "decision either"
            ),
            problem_class="client_error",
        )
    return _decision_view(row)


# =====================================================================================
# R3  POST /api/v1/harvest-candidates/{harvest_candidate_id}/decision
# =====================================================================================
@router.post(
    "/harvest-candidates/{harvest_candidate_id}/decision",
    status_code=201,
    summary="Record a curation decision as the named human",
)
def record_curation_decision(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    harvest_candidate_id: Annotated[str, PathParam(pattern=_UUID_RE)],
    body: CurationDecisionRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """ONE candidate, ONE decision, ONE named human. `MOS-TRAIN-080`, `MOS-TRAIN-204`.

    THE THREE THINGS THIS ROUTE CANNOT DO, AND THEY ARE THE SPECIFICATION.
      It cannot decide in bulk. The candidate is the path and the body carries no array,
        so `MOS-UI-119`'s forbidden *select all and include* has no wire form here. A
        console that wanted it would have to issue one request per case, which is what
        "without the cases having been shown" was written to prevent.
      It cannot name the decider. `_actor` reads the credential; `MOS-TRAIN-080`'s named
        human is not a body member on any route of this surface.
      It cannot auto-include. `medos.training.curation.decide` is the only writer of
        `decision='include'` and it demands a principal id; `auto_exclude` is the machine
        path and can write only `exclude`. There is no route to `auto_exclude` at all,
        because `MOS-TRAIN-205` requires the predicate's serialised SOURCE with it and a
        predicate a client supplies is a judgement wearing a predicate's clothes.

    `review_seconds` IS REQUIRED AND HAS NO DEFAULT, here and in the engine. It is "time
    on task, for the queue's own quality signal", and a default of zero would make a
    curator who spent four seconds on two hundred cases indistinguishable from one who
    spent forty minutes.

    `MOS-TRAIN-072` IS ASKED AGAIN. A decision is part of the harvest, and a tenant whose
    flag went false between the draw and the queue is a tenant whose curation stops --
    `MOS-TRAIN-076` blocks every new harvest on revocation and says in terms that a
    subsequent `DatasetVersion` MUST NOT be sealed from candidates acquired after it.
    """
    denied = _require(request, "curation_decision.record")
    if denied is not None:
        return denied
    malformed = _idempotency_key(request, response, idempotency_key)
    if malformed is not None:
        return malformed
    try:
        train_policy.assert_harvest_permitted(conn)
    except HarvestRefused as exc:
        return _refusal(request, exc)
    if not _candidate_exists(conn, harvest_candidate_id):
        return _not_found(request, "harvest_candidate", harvest_candidate_id)

    try:
        decision_id = cur.decide(
            conn,
            candidate_id=harvest_candidate_id,
            decision=body.decision,
            decided_by=_actor(request).id,
            review_seconds=body.review_seconds,
            reason_code=body.reason_code,
            note=body.note,
        )
    except CurationRefused as exc:
        conn.rollback()
        return _refusal(request, exc)
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        # `curation_decisions_settled_uk` is UNIQUE (candidate_id) WHERE decision <>
        # 'defer'. MOS-TRAIN-203 keeps the row: a settled candidate is not re-decided and
        # the first decision is not overwritten, so this is a 409 and never a 200.
        return _problem(
            request,
            status=409,
            code="CURATION_DECISION_ALREADY_SETTLED",
            title="This candidate has already been decided",
            detail=(
                f"harvest candidate {harvest_candidate_id} carries a settled "
                "CurationDecision. curation_decisions is append-only (MOS-TRAIN-203) "
                "and a correction is a new batch drawn against a new plan, never an "
                "edit: the decision is part of the evidence a ValidationReport "
                "reproduces (MOS-TRAIN-081)"
            ),
            problem_class="client_error",
        )
    conn.commit()

    row = _decision_by_id(conn, decision_id)
    assert row is not None  # just written, under this tenant
    if body.decision != "defer":
        # `Location` names the SETTLED decision, and only a settled one has that address:
        # `R2` reads `curation_decisions_settled_uk`'s row, so pointing a deferral at it
        # would be a `201` whose `Location` answers `404`. A deferral is still returned in
        # full -- it is a recorded act by a named human that simply is not terminal.
        response.headers["Location"] = (  # MOS-API-046
            f"/api/v1/harvest-candidates/{harvest_candidate_id}/decision"
        )
    return _decision_view(row)


# =====================================================================================
# R5  GET /api/v1/harvest-batches/{harvest_batch_id}/stratification
# =====================================================================================
@router.get(
    "/harvest-batches/{harvest_batch_id}/stratification",
    summary="The corpus stratification report recorded on a batch",
)
def get_harvest_batch_stratification(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    harvest_batch_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """`MOS-TRAIN-088`'s FULL result, which is the half a verdict cannot carry.

    "MUST record its full result as a `CorpusStratificationReport` on the batch. A `fail`
    MUST block sealing." `MOS-UI-116` makes this route the source of C1-C7 for the
    composition panel and forbids a second implementation in the browser, and
    `MOS-TRAIN-011` makes the same report part of the evidence set a promotion decision
    has to show. So the whole `checks` object travels, every check including the ones that
    passed, with its observed statistic beside its bound.

    THE LATEST REPORT, AND THERE MAY BE SEVERAL. `MOS-UI-131` moves the battery BEFORE the
    point of no return and `medos/medos/training/seal.py` records a report when it REFUSES as
    well as when it seals -- "a fail that blocked without leaving a record would make the
    most informative outcome the only one with no evidence". A batch can therefore carry a
    failed dry run and a later passing seal, and the row a reader wants is the most
    recent. The earlier ones are not deleted: `MOS-STORE-359` keeps them.

    A BATCH WITH NO REPORT IS A `404` ON THE REPORT AND NOT AN EMPTY `200`. The battery
    runs at the seal; a batch still being curated has not been checked, and a body that
    said `verdict: null` would read to a no-code operator as a cohort that passed nothing
    in particular rather than one nobody has looked at -- which is the exact confusion
    `MOS-API-112` fixes structurally for `SealRun.check_battery`.
    """
    denied = _require(request, "corpus_stratification_report.read")
    if denied is not None:
        return denied
    if cur.find_batch(conn, harvest_batch_id) is None:
        return _not_found(request, "harvest_batch", harvest_batch_id)
    with tenant_tx(conn) as tx:
        row = tx.execute(
            "SELECT id, harvest_batch_id, dataset_version_id, verdict, checks, "
            "       computed_at "
            "  FROM corpus_stratification_reports "
            " WHERE tenant_id = current_tenant_id() AND harvest_batch_id = %s "
            " ORDER BY computed_at DESC, created_at DESC LIMIT 1",
            (_uuid.UUID(harvest_batch_id),),
        ).fetchone()
    if row is None:
        return _problem(
            request,
            status=404,
            code="CORPUS_STRATIFICATION_REPORT_NOT_FOUND",
            title="No stratification report has been recorded on this batch",
            detail=(
                f"harvest batch {harvest_batch_id} has not been through the C1-C7 "
                "battery. MOS-TRAIN-088 runs it when a training DatasetVersion is "
                "sealed from the batch and when a vendor_evidence report is issued "
                "against one; seal the batch to produce it"
            ),
            problem_class="client_error",
        )
    return _stratification_view(dict(row))


# =====================================================================================
# R25  GET /api/v1/harvest-batches/{harvest_batch_id}/split-preview
# =====================================================================================
@router.get(
    "/harvest-batches/{harvest_batch_id}/split-preview",
    summary="The projected split, computed server-side",
)
def get_split_preview(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    harvest_batch_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """`MOS-UI-116`: this comes from the SERVER, and a second implementation in the
    browser is forbidden -- the two disagree on exactly the cohorts that sit near a
    bound, which is every cohort where the preview matters. `MOS-UI-117` makes it a
    preview and the seal-time result authoritative, so `is_preview` and `authoritative`
    are members of the response rather than words the console is trusted to add.

    IT CREATES NOTHING. No `DatasetSplit` row, no persisted assignment, no id: it calls
    `medos.training.split.assign`, the same pure function the seal calls, over a set that
    is still changing -- which is a different object from `MOS-TRAIN-093`'s report over a
    sealed manifest, and `MOS-EVID-028` makes a split a materialised manifest and never a
    projection.
    """
    denied = _require(request, "harvest_batch.read")
    if denied is not None:
        return denied
    batch = cur.find_batch(conn, harvest_batch_id)
    if batch is None:
        return _not_found(request, "harvest_batch", harvest_batch_id)

    rows = cur.candidates(conn, harvest_batch_id)
    included = [c for c in rows if c.decision == "include"]
    undecided = sum(1 for c in rows if c.decision not in ("include", "exclude"))
    patients = sorted({c.patient_key for c in included})
    strata = {
        c.patient_key: split_mod.stratum_of(c.acquisition_profile or {})
        for c in included
    }
    assignment = split_mod.assign(
        patients,
        strata,
        split_mod.seed_label_for(
            capability_id=batch.capability_id, harvest_batch_id=harvest_batch_id
        ),
    )
    counts = assignment.partition_patients
    test = counts.get("test", 0)
    # MOS-UI-142 does this arithmetic on the screen and MUST carry it in the remedy, so
    # the server computes it once rather than leaving two implementations of a ceiling.
    needed_total = -(-split_mod.TEST_PARTITION_FLOOR * 100 // 20)
    return json_safe(
        {
            "harvest_batch_id": harvest_batch_id,
            "is_preview": True,
            "authoritative": False,
            "computed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "included_patient_count": len(patients),
            "included_candidate_count": len(included),
            "undecided_candidate_count": undecided,
            "partition_patients": {
                "train": counts.get("train", 0),
                "tune": counts.get("tune", 0),
                "test": test,
                "excluded": counts.get("excluded", 0),
            },
            "assignment_method": assignment.method,
            "stratified_by": list(split_mod.STRATIFIED_BY),
            "test_partition_floor": split_mod.TEST_PARTITION_FLOOR,
            "meets_test_partition_floor": test >= split_mod.TEST_PARTITION_FLOOR,
            "patients_needed_for_test_floor": max(0, needed_total - len(patients)),
        }
    )


# =====================================================================================
# R26  POST /api/v1/harvest-batches/{harvest_batch_id}/seal
# =====================================================================================
def seal_driver_for(request: Request) -> seal_mod.SealDriver:
    """The `SealDriver` this app was built with, or one bound from the deployment.

    On `app.state` so that a test can inject a synchronous driver and assert the battery
    without racing a thread, and so that `create_app` decides the concurrency model in
    one place -- the same posture `create_app` takes toward `connect` and `authenticator`
    (`MOS-REL-046`: "a test MUST be able to construct two independent instances of any
    component in one process").
    """
    driver = getattr(request.app.state, "medos_seal_driver", None)
    if driver is not None:
        return driver  # type: ignore[no-any-return]
    store, bucket = _manifest_store(request)
    return seal_mod.ThreadSealDriver(
        request.app.state.medos_connect,
        store=store,
        bucket=bucket,
        gateway_factory=_gateway_factory(),
        deid=seal_mod.load_deid_provenance(),
    )


def _gateway_factory() -> Any:
    """A `dataset_export` Gateway opener, or `None` with the reason recorded downstream.

    `medos.training.retrieval.gateway_from_env` is the ONE place the pipeline's Gateway
    credential is read, and it refuses to fall back to `MEDOS_DICOMWEB_TOKEN` -- the
    worker's key, which the Gateway resolves as `platform_writer`, whose egress carries
    no de-identification (`MOS-DATA-021`). Building the config here instead would put a
    second reader of that variable in the tree, which is how "the pipeline only ever
    retrieves as dataset_export" silently stops being true.

    `None` is not a silent skip: the reason travels with it into the `SealRun`, and
    "no dataset_export credential is configured" is a different finding from "the
    Gateway refused" with a different remedy.
    """

    def factory() -> Any:
        gateway, _reason = retrieval.gateway_from_env()
        return gateway

    return factory


@router.post(
    "/harvest-batches/{harvest_batch_id}/seal",
    status_code=202,
    summary="Seal the cohort",
)
def seal_harvest_batch(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    harvest_batch_id: Annotated[str, PathParam(pattern=_UUID_RE)],
    body: SealRunSubmitRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """ONE operator action, `202`, and never a `Job`.

    `MOS-UI-130` makes this one action spanning the `DatasetVersion` seal and the
    `DatasetSplit` freeze; `MOS-API-005` admits one permission and this row honestly
    needs two, so `dataset_split.freeze` is checked here as well and recorded in the
    registry under `also_requires:`.

    `202` and not `201`: `MOS-API-056`'s long-running-operation pattern. The response
    carries a `Location` naming the `SealRun` and `Retry-After: 30`, the hint
    `MOS-API-057` fixes, and a client MUST NOT poll faster than it. The alternative
    shapes -- a synchronous seal and a `Job` -- are refused in `medos/medos/training/seal.py`'s
    docstring and in `medos/api/v1/routes.train.yaml`'s block header, each by name.

    The refusals that happen HERE are the ones no cohort work is needed for: the tenant's
    training-use flag (`MOS-TRAIN-072`, an answer no change to the body can alter), a
    batch that does not exist, a batch already sealed. Everything the candidate set
    decides happens in the driver and is read back from the `SealRun` -- which is the
    whole reason the operation is polled.
    """
    denied = _require(request, "dataset_version.create", *SEAL_ALSO_REQUIRES)
    if denied is not None:
        return denied
    malformed = _idempotency_key(request, response, idempotency_key)  # MOS-API-022
    if malformed is not None:
        return malformed

    try:
        train_policy.assert_harvest_permitted(conn)
    except HarvestRefused as exc:
        return _refusal(request, exc)

    batch = cur.find_batch(conn, harvest_batch_id)
    if batch is None:
        return _not_found(request, "harvest_batch", harvest_batch_id)
    try:
        ev_repo.get_dataset(conn, body.dataset_id)
    except LookupError:
        # MOS-UI-101: the dataset is SELECTED from row 32 and never composed, so an id
        # that names nothing this tenant can see is a 404 on the dataset and not a 422
        # on the body -- the operator picked from a list that has since changed.
        return _not_found(request, "dataset", body.dataset_id)

    try:
        # Read HERE, in the handler, and not inside `seal_driver_for`: an injected
        # driver bypasses that function's body, and a declaration checked only on the
        # path the shipped driver takes is a declaration a test harness can skip past.
        # `MOS-EVID-021`'s refusal has to reach the operator as a `503` on the POST
        # rather than as a REFUSED run two minutes later, after the driver has
        # retrieved a whole cohort to discover the deployment never said what UID space
        # it seals under.
        seal_mod.load_deid_provenance()
        driver = seal_driver_for(request)
    except DeploymentNotDeclared as exc:
        return _deployment_problem(request, exc)

    try:
        run = seal_mod.submit(
            conn,
            batch_id=harvest_batch_id,
            dataset_id=body.dataset_id,
            submitted_by=_actor(request).id,
        )
    except CurationRefused as exc:
        return _refusal(request, exc)
    conn.commit()

    driver.start(run.id, _tenant_of(request))

    location = f"/api/v1/seal-runs/{run.public_id}"
    response.headers["Location"] = location  # MOS-API-046
    response.headers["Retry-After"] = "30"  # MOS-API-057
    return _seal_run_view(run)


# =====================================================================================
# R27  GET /api/v1/seal-runs/{seal_run_id}
# =====================================================================================
@router.get("/seal-runs/{seal_run_id}", summary="Poll the seal")
def get_seal_run(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    seal_run_id: Annotated[str, PathParam(pattern=_SEAL_RUN_RE)],
) -> Any:
    """`MOS-API-057`'s poll. The load-bearing member is `check_battery`.

    Bound to `dataset_version.read` rather than to `harvest_batch.read` because the
    document names the sealed version, its digest and its per-partition counts:
    `MOS-API-005` binds a permission to what a route READS, not to what its first path
    segment is called.

    `Retry-After` is returned on every non-terminal poll and not only at creation, so a
    client that lost the `202` still learns the hint rather than choosing its own.
    """
    denied = _require(request, "dataset_version.read")
    if denied is not None:
        return denied
    run = seal_mod.get_seal_run(conn, seal_run_id)
    if run is None:
        return _not_found(request, "seal_run", seal_run_id)
    if not run.terminal:
        response.headers["Retry-After"] = "30"
    return _seal_run_view(run)


# =====================================================================================
# R28  GET /api/v1/dataset-splits/{dataset_split_id}
# =====================================================================================
@router.get("/dataset-splits/{dataset_split_id}", summary="Read a frozen split")
def get_dataset_split(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    dataset_split_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """The WHOLE `leakage_report` and not a verdict. There is no POST on this
    collection: see `R26`."""
    denied = _require(request, "dataset_split.read")
    if denied is not None:
        return denied
    row = ev_repo.get_split(conn, dataset_split_id)
    if row is None:
        return _not_found(request, "dataset_split", dataset_split_id)
    return _split_view(row)


# =====================================================================================
# R29  POST /api/v1/dataset-versions/{dataset_version_id}/annotation-sets
# =====================================================================================
@router.post(
    "/dataset-versions/{dataset_version_id}/annotation-sets",
    status_code=201,
    summary="Close the campaign and freeze the reference standard",
)
def freeze_annotation_set(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    dataset_version_id: Annotated[str, PathParam(pattern=_UUID_RE)],
    body: AnnotationSetFreezeRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """`MOS-UI-129`'s explicit close-the-campaign. No per-case payload on the wire.

    There is no PATCH and no DELETE here, for `MOS-TRAIN-110`'s reason -- the pipeline
    MUST NOT append to a frozen set, and adding a reader or a case produces a NEW set --
    and `annotation_sets_frozen` refuses the other reading in the database.
    """
    denied = _require(request, "annotation_set.create")
    if denied is not None:
        return denied
    malformed = _idempotency_key(request, response, idempotency_key)  # MOS-API-022
    if malformed is not None:
        return malformed

    version = ev_repo.get_dataset_version(conn, dataset_version_id)
    if version is None:
        return _not_found(request, "dataset_version", dataset_version_id)

    try:
        stack = _annotation_stack()
        entries = _campaign_entries(dataset_version_id)
        store, bucket = _manifest_store(request)
    except DeploymentNotDeclared as exc:
        return _deployment_problem(request, exc)

    # REPORTED DIVERGENCE, resolved here in the one direction that loses nothing.
    # `medos/schemas/evidence/annotation-set-freeze-request-1.0.0.json` types `reader_id` as a
    # UUID -- "a MedicalOS User principal (MOS-UI-123, MOS-TRAIN-096)" -- and
    # `0006_evidence.up.sql` CHECKs `annotation_readers.reader_id ~ '^rdr_[a-z0-9_]{1,60}$'`,
    # which chapter 7 section 7.5's own example writes as `rdr_a1`. The two cannot both
    # be satisfied by one literal, so the uuid is carried into the column's grammar
    # WITHOUT LOSS -- all 32 hex digits, no truncation -- and the User id is recoverable
    # from the row. Truncating would make `MOS-EVID-038`'s named reader unresolvable
    # after the fact, which is the one thing that requirement exists to prevent. Which
    # of the two spellings is normative is chapter 7's and chapter 19's to settle.
    readers = [
        {
            "reader_id": f"rdr_{r.reader_id.replace('-', '')}",
            "role": r.role,
            "years_experience": r.years_experience,
            "board_certified": r.board_certified,
            "specialty": r.specialty,
            # MOS-UI-124: the platform fills `tool` from the deployed annotation stack.
            "tool": stack["tool"],
            "instructions_uri": r.instructions_uri or stack["instructions_uri"],
            "blinded_to": list(r.blinded_to or []),
        }
        for r in body.readers
    ]
    try:
        frozen = ev_repo.freeze_annotation_set(
            conn,
            dataset_version_id=dataset_version_id,
            name=body.name,
            capability_id=body.capability_id,
            label_definition_id=body.label_definition_id,
            annotation_type=body.annotation_type,
            consensus_rule=body.consensus_rule,
            readers=readers,
            entries=entries,
            store=store,
            bucket=bucket,
            frozen_by=_actor(request).id,
            consensus_params=body.consensus_params or {},
            reference_of_record=body.reference_of_record,
        )
    except FreezeRefused as exc:
        return _refusal(request, exc)
    except DeploymentNotDeclared as exc:
        return _deployment_problem(request, exc)
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        return _problem(
            request,
            status=409,
            code="ANNOTATION_SET_NAME_EXISTS",
            title="A set of that name is already frozen against this cohort",
            detail=(
                "annotation_sets_name_uk is UNIQUE (dataset_version_id, name). "
                "MOS-TRAIN-110 makes a frozen set un-appendable, so adding a reader "
                "or a case produces a NEW set -- under a new name"
            ),
            problem_class="client_error",
        )
    conn.commit()

    row = ev_repo.get_annotation_set(conn, frozen.id)
    assert row is not None  # just written, under this tenant
    location = f"/api/v1/annotation-sets/{frozen.id}"
    response.headers["Location"] = location  # MOS-API-046
    return _annotation_set_view(row, row.pop("readers"))


# =====================================================================================
# R30  GET /api/v1/dataset-versions/{dataset_version_id}/annotation-sets
# =====================================================================================
@router.get(
    "/dataset-versions/{dataset_version_id}/annotation-sets",
    summary="The reference standards frozen against one cohort",
)
def list_annotation_sets(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    dataset_version_id: Annotated[str, PathParam(pattern=_UUID_RE)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """So that `R6`'s `annotation_set_id` is chosen from a list rather than composed."""
    denied = _require(request, "annotation_set.read")
    if denied is not None:
        return denied
    if ev_repo.get_dataset_version(conn, dataset_version_id) is None:
        return _not_found(request, "dataset_version", dataset_version_id)
    rows = ev_repo.list_annotation_sets(
        conn, dataset_version_id=dataset_version_id, limit=limit + 1
    )
    readers = ev_repo.readers_of(conn, [str(r["id"]) for r in rows])
    return _page(
        [_annotation_set_view(r, readers.get(str(r["id"]), [])) for r in rows], limit
    )


# =====================================================================================
# R31  GET /api/v1/annotation-sets/{annotation_set_id}
# =====================================================================================
@router.get("/annotation-sets/{annotation_set_id}", summary="Read one frozen set")
def get_annotation_set(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    annotation_set_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """The readers are projected rather than reduced to a count: `MOS-EVID-043` puts
    inter-reader agreement in every report citing a multi-reader set."""
    denied = _require(request, "annotation_set.read")
    if denied is not None:
        return denied
    row = ev_repo.get_annotation_set(conn, annotation_set_id)
    if row is None:
        return _not_found(request, "annotation_set", annotation_set_id)
    return _annotation_set_view(row, row.pop("readers"))


# =====================================================================================
# Row 32  GET /api/v1/datasets
# =====================================================================================
@router.get("/datasets", summary="List datasets")
def list_datasets(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    purpose: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """An EXISTING row of table 10.2-B. Declared and served because `R26` requires a
    `dataset_id` and `MOS-UI-101` forbids the operator composing one. Its sibling row 33
    (`POST /datasets`) is NOT served: it binds `dataset.write`, which section 8.3.2
    registers as a spelling a build MUST fail on."""
    denied = _require(request, "dataset.read")
    if denied is not None:
        return denied
    rows = ev_repo.list_datasets(conn, purpose=purpose, limit=limit + 1)
    return _page([_dataset_view(r) for r in rows], limit)


# =====================================================================================
# Row 37  GET /api/v1/dataset-versions/{dataset_version_id}
# =====================================================================================
@router.get(
    "/dataset-versions/{dataset_version_id}", summary="Read a sealed dataset version"
)
def get_dataset_version(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    dataset_version_id: Annotated[str, PathParam(pattern=_UUID_RE)],
) -> Any:
    """`MOS-UI-147`'s read-only cohort summary. Rows 36, 38 and 40 are NOT served: they
    describe a two-step create-then-seal model this platform does not implement."""
    denied = _require(request, "dataset.read")
    if denied is not None:
        return denied
    row = ev_repo.get_dataset_version(conn, dataset_version_id)
    if row is None:
        return _not_found(request, "dataset_version", dataset_version_id)
    return _dataset_version_view(row)


# Checked at import rather than reviewed: `RefusalError` is the base `_refusal()`
# renders an engine refusal from, and a change that narrowed either class would silently
# stop these routes emitting RFC 9457 documents for the refusals that are this surface's
# whole product (chapter 19 section 19.3.4 is titled "Seal -- the refusal surface").
if not issubclass(CurationRefused, RefusalError):  # pragma: no cover - a defect
    raise RuntimeError(
        "medos.sdk.errors.CurationRefused is no longer a RefusalError; "
        "medos/medos/api/routes_curation.py renders it through routes_training._refusal, "
        "which reads `as_problem()` and `check_ids` off that base (MOS-API-035)"
    )
if not issubclass(FreezeRefused, RefusalError):  # pragma: no cover - a defect
    raise RuntimeError(
        "medos.sdk.refusal.FreezeRefused is no longer a RefusalError; see above"
    )
