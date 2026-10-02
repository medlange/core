# SPDX-License-Identifier: Apache-2.0
"""The model-development surface. Table 10.2-B rows `R6`-`R17`, chapter 19 section 19.3.

| Method + path                                            | Row | Surface |
|----------------------------------------------------------|-----|---------|
| `POST /api/v1/training-runs`                               | R6  | console |
| `GET  /api/v1/training-runs`                               | R7  | console |
| `GET  /api/v1/training-runs/{id}`                          | R8  | console |
| `POST /api/v1/training-runs/{id}/cancel`                   | R9  | console |
| `GET  /api/v1/capabilities/{capability_id}/seed-variance`  | R10 | console |
| `GET  /api/v1/training-runs/{id}/test-exposure`            | R11 | console |
| `GET  /api/v1/training-runs/{id}/candidate`                | R12 | console |
| `POST /api/v1/configuration-searches`                      | R13 | expert  |
| `GET  /api/v1/configuration-searches/{id}`                 | R14 | expert  |
| `POST /api/v1/configuration-searches/{id}/nomination`      | R15 | expert  |
| `POST /api/v1/conversion-runs`                             | R16 | expert  |
| `GET  /api/v1/conversion-runs/{id}`                        | R17 | expert  |

`medos/api/v1/routes.train.yaml` is normative over this file. Every row above carries its
argument there and the arguments are NOT repeated here; what this module owes is the four things
CONTRACT.md section 1 admits in a handler -- parse, authorise, bind the tenant, call the
engine, render -- and nothing else. `MOS-REL-084` replaces this surface with Go, and that
is why the rule is worth keeping: a rule implemented here has to be implemented twice.

WHY THIS FILE IS IN `medos/medos/api/` AND NOT IN `medos/medos/training/`
--------------------------------------------------------------
`tests/gate/test_no_auto_promote.py` check 1 asserts the import closure of
`medos.training` reaches neither `medos.promotion` nor `medos.evidence.deployment`. A
router that imports the engine AND the registry belongs on the API side of that line, and
putting it inside the engine package would put the registry inside the pipeline's closure
-- which is `MOS-TRAIN-189`'s call-graph property failing for a reason that has nothing to
do with anybody intending a promotion. Check 4 of the same gate forbids the ASSEMBLY of a
periodic trigger and a training entrypoint in one module (`MOS-TRAIN-194`), so this module
contains no timer, no watcher, no drift callback and no retry loop: a run starts because a
person pressed one control, every time.

THE REFUSALS ARE THE PRODUCT
-----------------------------
Section 19.3.1 says what this surface refuses to be and section 19.3.4 is titled "Seal --
the refusal surface". A no-code operator cannot audit a silent success, so every gate the
engine enforces is rendered as an RFC 9457 document with the machine-readable `code`, the
`refusals[]` array the engine produced verbatim, and enough detail to act on:

  403 `TRAINING_USE_NOT_PERMITTED`   `MOS-TRAIN-072`, and `MOS-API-112` pins the wire form.
  422 `TRAINING_RUN_REFUSED`         leakage (`MOS-TRAIN-115`), an incomplete binding
                                     (`MOS-TRAIN-124`), a hand-configured backend with no
                                     rationale (`MOS-TRAIN-211`).
  400 `SCHEMA_VIOLATION`             the two biconditionals the request schemas carry, so
                                     that `monai_supervised` without a rationale is
                                     refused before a connection is opened.
  422 `CONFIGURATION_SEARCH_REFUSED` `MOS-TRAIN-216` / `MOS-TRAIN-220` / `MOS-TRAIN-234`.
  422 `CONVERSION_REFUSED`           `MOS-TRAIN-157` / `MOS-TRAIN-164` / `MOS-TRAIN-169`.
  403 `PARTITION_NOT_READABLE`       `MOS-TRAIN-141`. No declared route takes a partition
                                     name, so this is defence in depth and not a path.
  503 `TRAINING_ENVIRONMENT_NOT_RECORDED`  see the next section.

TWO PLATFORM-WIDE DIVERGENCES THIS MODULE RECORDS AND DOES NOT RESOLVE
-----------------------------------------------------------------------
`docs/spec/99-known-inconsistencies.md` entry 80. `medos/medos/training/errors.py` spells its
`type` under `https://medicalos.dev/problems/` while `MOS-API-036` and
`medos/medos/api/problems.py PROBLEM_BASE` name `https://spec.medicalos.org/problems/` as the
only permitted authority. THIS MODULE EMITS WHAT THE ENGINE RAISES, unrewritten, because
what a client sees today is the engine's spelling and because changing one of the thirteen
types here would make the code internally inconsistent instead of externally. Entry 80
owns the one change that can move all of them at once.

The `class` is the other half and it CANNOT be carried through unchanged: `HarvestRefused`
and `PartitionForbidden` render `class: "authorization"`, which is not one of table
10.4-A's six values, and `medos.api.problems.build_problem` refuses to emit a word outside
that closed enum (`MOS-API-038`). `_CLASS_AT_THE_BOUNDARY` below maps it to `authz_error`,
which is the registered spelling and the one `MOS-API-112` pins for
`training-use-not-permitted`. The mapping is at the boundary, is one line, and is the
minimum needed to emit a renderable document.

WHERE THE SERVER-DERIVED HALF OF A `TrainingRun` COMES FROM
------------------------------------------------------------
`medos/schemas/training/training-run-submit-request-1.0.0.json` carries four ids and one
optional backend, and `MOS-TRAIN-124` requires the run to bind some twenty further inputs
-- every one of them server-derived, on one argument: a value a client may supply is a
value a client may supply wrongly, and a binding that records a client's claim about the
GPU it ran on is a binding that says something false in a `ValidationReport`.

Three of those come off the rows the four ids name: `dataset_version_digest`,
`split_digest` and `annotation_digest` are read here from the sealed and frozen rows, and
the split's `leakage_report` is read with them, because `MOS-TRAIN-115` puts the leakage
check before the first batch is loaded and `medos.training.runs.submit` refuses a caller
that did not look.

The rest -- `code_commit`, `code_dirty`, `image_digest`, the backend version, the four
reproducibility blocks and the bound `PreprocessingSpec` -- describe THE DEPLOYMENT, and
this repository has no table for them: `0013_training.up.sql` records what a run bound and
nothing records what the training image IS. So this module reads one declaration,
`MEDOS_TRAINING_ENVIRONMENT`, and refuses `503` when it is absent or incomplete rather
than inventing a value. An operator seeing that refusal is being told the truth: the
deployment has not said what it would train with.

HONEST GAP, STATED HERE BECAUSE IT IS THE WEAKEST JOINT IN THIS FILE. `MOS-TRAIN-126`'s
claim is that the determinism settings and the hardware are "recorded rather than
asserted". A deployment-level declaration is an assertion by the operator, not an
observation by the runner. The observation point is `medos.training.runs.start()`, inside
the process that actually holds the GPU, and this surface does not own it. What the
declaration buys is that the assertion is made ONCE, by the deployment, in a place a
reviewer can read -- rather than per-request by whichever client happened to POST.

Spec: MOS-API-004, MOS-API-005, MOS-API-015, MOS-API-035, MOS-API-036, MOS-API-037,
MOS-API-038, MOS-API-046, MOS-API-089, MOS-API-112, MOS-SEC-008, MOS-SEC-027, MOS-SEC-032,
MOS-SEC-033, MOS-SEC-045, MOS-SEC-074, MOS-SEC-075, MOS-SEC-158, MOS-TRAIN-003,
MOS-TRAIN-008, MOS-TRAIN-072, MOS-TRAIN-115, MOS-TRAIN-122, MOS-TRAIN-124, MOS-TRAIN-125,
MOS-TRAIN-127, MOS-TRAIN-135, MOS-TRAIN-140, MOS-TRAIN-141, MOS-TRAIN-156, MOS-TRAIN-189,
MOS-TRAIN-194, MOS-TRAIN-211, MOS-TRAIN-213, MOS-TRAIN-216, MOS-TRAIN-218, MOS-TRAIN-220,
MOS-TRAIN-234, MOS-UI-101, MOS-UI-102, MOS-UI-147, MOS-UI-148, MOS-UI-149, MOS-UI-156,
MOS-UI-157, MOS-UI-162, MOS-UI-163, MOS-UI-167, MOS-UI-168, MOS-EVID-083,
CONTRACT.md sections 1, 9 and 11.
"""

from __future__ import annotations

import json
import os
import re
import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final, Literal

import psycopg
from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from medos.api import db_connection
from medos.api.auth import principal_of
from medos.api.problems import (
    RETRYABLE_CLASSES,
    build_problem,
    json_safe,
    now_rfc3339,
    problem_response,
    trace_id_of,
)
from medos.config.devmode import refuse_dev_value_outside_dev
from medos.db import audit
from medos.db.tenancy import tenant_tx
from medos.sdk import autoconfig
from medos.sdk.errors import (
    ConversionRefused,
    HarvestRefused,
    PartitionForbidden,
    RunRefused,
    SearchRefused,
    TrainingError,
)
from medos.sdk.refusal import RefusalError
from medos.security.scopes import scope_permits
from medos.training import conversion as conv
from medos.training import policy as train_policy
from medos.training import runs as tr
from medos.training import search as search_mod
from medos.training.candidate import FORBIDDEN_PIPELINE_PERMISSIONS
from medos.training.orchestrator import FORBIDDEN_PERMISSIONS

__all__ = [
    "ENVIRONMENT_VAR",
    "PERMISSIONS",
    "PROMOTION_NOTICE",
    "TrainingEnvironment",
    "load_training_environment",
    "router",
]

router = APIRouter(prefix="/api/v1", tags=["training"])


# =====================================================================================
# Permissions
# =====================================================================================
#: The nine keys these twelve rows bind, with the sentence `medos/contracts/permissions.yaml`
#: registers each under. Eight were minted for this surface; `model_version.read` is
#: chapter 8's existing spelling and row `R12` reuses it (`MOS-UI-102` names it, not
#: `model.read`, in the `evidence_scientist` reference principal).
PERMISSIONS: Final[dict[str, str]] = {
    "training_run.read": "read a TrainingRun, its state and its reproducibility binding",
    "training_run.submit": "submit a TrainingRun against a sealed cohort and frozen split",
    "training_run.cancel": "cancel a TrainingRun in flight as the named human",
    "configuration_search.read": "read a ConfigurationSearch, its space and its nomination",
    "configuration_search.declare": "register a ConfigurationSearch with a bounded budget",
    "configuration_search.nominate": "nominate the one trial a search may evaluate on test",
    "conversion_run.read": "read a ConversionRun and its equivalence result",
    "conversion_run.submit": "submit a ConversionRun against a registered ModelVersion",
    "model_version.read": "read a ModelVersion",
}

#: `MOS-SEC-158`: the exclusion is "enforced by their absence from the role, never by a
#: check in code". The API-surface reading of the same sentence is that no route of this
#: module may BIND one of chapter 17's forbidden permissions -- a route that did would be
#: the grant written down, reachable whatever the role table later says. Asserted at
#: import against the two tuples chapter 17 keeps, never against a second copy of them, so
#: that an addition to either lands here as an ImportError-time failure rather than as a
#: review someone has to remember to do.
_FORBIDDEN: Final[frozenset[str]] = frozenset(FORBIDDEN_PERMISSIONS) | frozenset(
    FORBIDDEN_PIPELINE_PERMISSIONS
)
if not _FORBIDDEN:  # pragma: no cover - the tuples are non-empty in every release
    raise RuntimeError(
        "medos.training.orchestrator.FORBIDDEN_PERMISSIONS and "
        "medos.training.candidate.FORBIDDEN_PIPELINE_PERMISSIONS are both empty; the "
        "MOS-SEC-158 guard below has lost its subject and would pass vacuously"
    )
_OVERLAP = sorted(set(PERMISSIONS) & _FORBIDDEN)
if _OVERLAP:  # pragma: no cover - a defect, caught before the app can be built
    raise RuntimeError(
        f"medos/medos/api/routes_training.py binds {_OVERLAP}, which MOS-SEC-158 and "
        "MOS-TRAIN-174 exclude from the training identity. MOS-UI-167 requires the "
        "promotion control to be ABSENT, not merely unauthorised."
    )


# =====================================================================================
# Section 19.3.7, as four constants
# =====================================================================================
#: `MOS-UI-168` requires the console to render the candidate's terminal state as what it
#: is -- built, measured, waiting for a decision by a named person -- and to NAME the role
#: that makes it. `medos/schemas/training/training-run-candidate-1.0.0.json` pins all four as
#: `const`, because this states a fixed property of the platform and a server that could
#: vary it would be a server where promotion is sometimes available here. No link, no
#: href, no action, no deployment reference: `MOS-TRAIN-189`'s call-graph property,
#: extended to the console by `MOS-UI-170`, stays true of a client that reads this.
PROMOTION_NOTICE: Final[dict[str, Any]] = {
    "performed_on_this_surface": False,
    "permission": "artifact.approve",
    "transition": "VALIDATED -> APPROVED",
    "requirement": "MOS-TRAIN-008",
}


# =====================================================================================
# The deployment's training environment
# =====================================================================================
ENVIRONMENT_VAR: Final[str] = "MEDOS_TRAINING_ENVIRONMENT"

_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"^tr_[0-9A-HJKMNP-TV-Z]{26}$")
_SEARCH_ID_RE: Final[re.Pattern[str]] = re.compile(r"^cs_[0-9A-HJKMNP-TV-Z]{26}$")
_CONVERSION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^cv_[0-9A-HJKMNP-TV-Z]{26}$")
_CAPABILITY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")

#: What the declaration MUST carry. Every one is a field of `MOS-TRAIN-124`'s binding that
#: describes the deployment rather than the cohort, and an absent one is refused rather
#: than defaulted -- `medos.training.runs.RunBinding` has no defaults on the four
#: reproducibility blocks for the same reason, and its docstring says why: "a default is an
#: assertion wearing a record's clothes".
_ENVIRONMENT_KEYS: Final[tuple[str, ...]] = (
    "code_commit",
    "code_dirty",
    "image_digest",
    "backend_versions",
    "seeds",
    "determinism",
    "hardware",
    "framework_versions",
    "preprocessing",
)

#: What one entry of `preprocessing` MUST carry. `MOS-IMG-045` makes the
#: `PreprocessingSpec` the registered artifact a run is fitted against; `MOS-IMG-049`'s
#: `io.output_kind` is what `MOS-TRAIN-211` keys its default-backend rule on, so it is
#: declared beside the spec rather than guessed from a capability's output list.
_SPEC_KEYS: Final[tuple[str, ...]] = ("id", "version", "digest", "output_kind")


class EnvironmentNotRecorded(RuntimeError):
    """The deployment has not declared what it would train with. Rendered as `503`."""

    def __init__(self, missing: Sequence[str], *, detail: str = "") -> None:
        self.missing: tuple[str, ...] = tuple(missing)
        self.extra = detail
        super().__init__(f"{ENVIRONMENT_VAR} is missing {list(self.missing)}. {detail}")


@dataclass(frozen=True)
class TrainingEnvironment:
    """The half of `MOS-TRAIN-124`'s binding that is a property of the DEPLOYMENT.

    Frozen, and constructed per request from the declaration rather than cached in a
    module-level variable: CONTRACT.md section 11 forbids global mutable state, and an
    environment cached at import is one an operator cannot correct without a restart.
    """

    code_commit: str
    code_dirty: bool
    image_digest: str
    backend_versions: Mapping[str, str]
    seeds: Mapping[str, Any]
    determinism: Mapping[str, Any]
    hardware: Mapping[str, Any]
    framework_versions: Mapping[str, Any]
    preprocessing: Mapping[str, Mapping[str, Any]]

    def spec_for(self, capability_id: str) -> Mapping[str, Any]:
        """The bound `PreprocessingSpec` for a capability, or `EnvironmentNotRecorded`."""
        bound = self.preprocessing.get(capability_id)
        if bound is None:
            raise EnvironmentNotRecorded(
                [f"preprocessing.{capability_id}"],
                detail=(
                    "this deployment has bound a PreprocessingSpec for "
                    f"{sorted(self.preprocessing)} and not for {capability_id!r}. "
                    "MOS-IMG-045 makes the spec the registered thing a run is fitted "
                    "against; a run whose spec nobody declared has no MOS-TRAIN-124 "
                    "binding to record"
                ),
            )
        absent = [k for k in _SPEC_KEYS if k not in bound]
        if absent:
            raise EnvironmentNotRecorded(
                [f"preprocessing.{capability_id}.{k}" for k in absent]
            )
        return bound

    def version_of(self, backend_kind: str) -> str:
        """`MOS-TRAIN-124` pins `training_backend.version`; `MOS-REL-037` forbids a range."""
        version = self.backend_versions.get(backend_kind)
        if not version:
            raise EnvironmentNotRecorded([f"backend_versions.{backend_kind}"])
        return str(version)


def load_training_environment(
    env: Mapping[str, str] | None = None,
) -> TrainingEnvironment:
    """Read the declaration. Raises `EnvironmentNotRecorded`; never returns a default.

    The value is a JSON object, or `@<path>` naming a file holding one. Both forms exist
    because a container hands configuration in an environment variable and an operator
    writes a document; neither is a code path that guesses.
    """
    source = dict(env if env is not None else os.environ)
    raw = source.get(ENVIRONMENT_VAR, "").strip()
    # A development default must not reach a deployment that has not declared itself
    # one. First statement after the read, before any interpretation: this makes the
    # loader strictly stricter and leaves its "never returns a default" promise intact.
    refuse_dev_value_outside_dev(ENVIRONMENT_VAR, raw, source)

    if not raw:
        raise EnvironmentNotRecorded(
            list(_ENVIRONMENT_KEYS),
            detail=f"{ENVIRONMENT_VAR} is unset in this deployment",
        )
    if raw.startswith("@"):
        try:
            raw = open(raw[1:], encoding="utf-8").read()
        except OSError as exc:
            raise EnvironmentNotRecorded(
                list(_ENVIRONMENT_KEYS),
                detail=f"{ENVIRONMENT_VAR} names a file this process cannot read: {exc}",
            ) from exc
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise EnvironmentNotRecorded(
            list(_ENVIRONMENT_KEYS), detail=f"{ENVIRONMENT_VAR} is not JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise EnvironmentNotRecorded(
            list(_ENVIRONMENT_KEYS), detail=f"{ENVIRONMENT_VAR} is not a JSON object"
        )
    missing = [k for k in _ENVIRONMENT_KEYS if k not in document]
    if missing:
        raise EnvironmentNotRecorded(missing)
    return TrainingEnvironment(
        code_commit=str(document["code_commit"]),
        code_dirty=bool(document["code_dirty"]),
        image_digest=str(document["image_digest"]),
        backend_versions=dict(document["backend_versions"]),
        seeds=dict(document["seeds"]),
        determinism=dict(document["determinism"]),
        hardware=dict(document["hardware"]),
        framework_versions=dict(document["framework_versions"]),
        preprocessing={
            str(k): dict(v) for k, v in dict(document["preprocessing"]).items()
        },
    )


# =====================================================================================
# Request models. The biconditionals are the request schemas', restated in pydantic.
# =====================================================================================
class _Backend(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["monai_supervised", "nnunet", "auto3dseg"]


class TrainingRunSubmitRequest(BaseModel):
    """`MOS-UI-147`'s "single action with no configuration", as four ids.

    `hyperparameters` is NOT a member and `extra="forbid"` is what makes that structural:
    `MOS-TRAIN-224` puts the training batch size in it and `MOS-UI-149` forbids revealing
    that quantity as editable, as a default, or behind an "advanced" region. With no such
    member a no-code client cannot reach any of `MOS-UI-149`'s six quantities under any
    name -- the difference between a screen that does not offer a control and an API on
    which the control does not exist.

    `fit_partition` and `select_partition` are absent for `MOS-TRAIN-141`'s reason: the
    cohort resolver takes a split id and a partition name and nothing else, and a request
    body that named a partition would be a container that can name its own data.
    """

    model_config = ConfigDict(extra="forbid")

    capability_id: Annotated[str, Field(pattern=_CAPABILITY_RE.pattern)]
    dataset_version_id: Annotated[str, Field(pattern=_UUID_RE.pattern)]
    split_id: Annotated[str, Field(pattern=_UUID_RE.pattern)]
    annotation_set_id: Annotated[str, Field(pattern=_UUID_RE.pattern)]
    training_backend: _Backend | None = None
    backend_rationale: Annotated[str, Field(min_length=20, max_length=2000)] | None = None
    search_id: Annotated[str, Field(pattern=_UUID_RE.pattern)] | None = None
    trial_index: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def _biconditionals(self) -> TrainingRunSubmitRequest:
        """`MOS-TRAIN-211` and `MOS-TRAIN-219`, as the schema's two `allOf` branches.

        This is the rule that keeps `MOS-UI-148` true on the wire and not only on the
        screen: a body naming `monai_supervised` with no rationale is refused before a
        connection is opened, and a body supplying a rationale for an auto-configured run
        is refused rather than silently dropped. 0013 carries the same pair as CHECKs, so
        a direct database writer meets it too.
        """
        hand_configured = (
            self.training_backend is not None
            and self.training_backend.kind == "monai_supervised"
        )
        if hand_configured and self.backend_rationale is None:
            raise ValueError(
                "MOS-TRAIN-211: a hand-configured monai_supervised run MUST record a "
                "backend_rationale of at least 20 characters naming what the "
                "auto-configured baseline failed to do. MOS-UI-148 fixes the no-code "
                "console to an auto-configuring backend, so omit training_backend"
            )
        if not hand_configured and self.backend_rationale is not None:
            raise ValueError(
                "backend_rationale names what the auto-configured baseline failed to "
                "do; an auto-configuring run has no baseline to have deviated from"
            )
        if (self.search_id is None) != (self.trial_index is None):
            raise ValueError(
                "MOS-TRAIN-219: a trial carries search_id AND trial_index; a second, "
                "lighter run record for trials is forbidden"
            )
        return self


class _Space(BaseModel):
    model_config = ConfigDict(extra="forbid")

    axes: list[dict[str, Any]]
    derived_from_fingerprint: list[str] | None = None
    constraints: list[dict[str, Any]] | None = None


class _Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_trials: Annotated[int, Field(ge=1)]
    max_gpu_hours: Annotated[float, Field(gt=0)]
    max_wall_clock_hours: Annotated[float, Field(gt=0)]
    max_ensemble_members: Annotated[int, Field(ge=1)]
    max_footprint_bytes: Annotated[int, Field(ge=1)]


class ConfigurationSearchDeclareRequest(BaseModel):
    """`MOS-TRAIN-218`'s record. The verb is `declare`: `MOS-TRAIN-220` refuses
    registration of a space that violates its grammar, so the domain's own word is
    registration and no verb had to be invented to avoid `create` (`MOS-SEC-034`)."""

    model_config = ConfigDict(extra="forbid")

    capability_id: Annotated[str, Field(pattern=_CAPABILITY_RE.pattern)]
    dataset_version_id: Annotated[str, Field(pattern=_UUID_RE.pattern)]
    split_id: Annotated[str, Field(pattern=_UUID_RE.pattern)]
    annotation_set_id: Annotated[str, Field(pattern=_UUID_RE.pattern)]
    parent_search_id: Annotated[str, Field(pattern=_UUID_RE.pattern)] | None = None
    space: _Space
    strategy: Literal["grid", "random", "adaptive"]
    seed: int
    selection_metric: Annotated[str, Field(min_length=1, max_length=128)]
    selection_partition: Literal["tune", "train"]
    selection_folds: list[int] | None = None
    selection_rule: Annotated[str, Field(min_length=1, max_length=512)]
    trials_planned: Annotated[int, Field(ge=1)]
    budget: _Budget

    @model_validator(mode="after")
    def _folds_bind_to_the_partition(self) -> ConfigurationSearchDeclareRequest:
        """`MOS-TRAIN-215`: the selection metric is computed on `tune`, or on
        cross-validation folds within `train`. `test` is in neither branch."""
        if (self.selection_partition == "train") != (self.selection_folds is not None):
            raise ValueError(
                "MOS-TRAIN-215: selection_folds is set exactly when the selection "
                "partition is train"
            )
        return self


class _Cost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_reason: Literal[
        "space_exhausted",
        "max_trials",
        "max_gpu_hours",
        "max_wall_clock_hours",
        "cancelled",
        "failed",
    ]
    gpu_hours: float | None = None
    wall_clock_hours: float | None = None
    footprint_bytes: int | None = None


class ConfigurationSearchNominationRequest(BaseModel):
    """`MOS-TRAIN-216`: exactly one trial, and the act spends the test partition."""

    model_config = ConfigDict(extra="forbid")

    training_run_id: Annotated[str, Field(pattern=_RUN_ID_RE.pattern)]
    runner_up_training_run_id: Annotated[str, Field(pattern=_RUN_ID_RE.pattern)]
    selection_margin: float
    trials_completed: Annotated[int, Field(ge=0)]
    trials_failed: Annotated[int, Field(ge=0)] = 0
    cost: _Cost


class ConversionRunSubmitRequest(BaseModel):
    """`MOS-TRAIN-156`. There is no `tolerances` member and no per-case skip, matching
    `medos.training.conversion`, which accepts neither: `MOS-TRAIN-164` says the remedy
    for a failed tolerance is a different conversion and never a relaxed bound, because
    "a tolerance relaxed to admit one artifact silently relaxes it for every future
    artifact of that family"."""

    model_config = ConfigDict(extra="forbid")

    source_model_version_id: Annotated[str, Field(pattern=_UUID_RE.pattern)]
    target_format: Literal["torchscript", "onnx", "tensorrt_plan"]
    precision: Literal["fp32", "tf32", "fp16", "int8"]
    toolchain: dict[str, Any]
    equivalence_cohort_digest: Annotated[str, Field(pattern=_DIGEST_RE.pattern)]
    calibration_dataset_version_id: (
        Annotated[str, Field(pattern=_UUID_RE.pattern)] | None
    ) = None
    calibration_partition: Literal["tune"] | None = None

    @model_validator(mode="after")
    def _int8_binds_a_tune_calibration(self) -> ConversionRunSubmitRequest:
        """`MOS-TRAIN-157`: "Calibrating on `test` is operating-point selection on the
        test set under another name". The member is a `tune` literal, so the only way to
        name another partition is to fail validation."""
        named = (
            self.calibration_dataset_version_id is not None
            or self.calibration_partition is not None
        )
        if (self.precision == "int8") != named:
            raise ValueError(
                "MOS-TRAIN-157: an int8 conversion MUST draw its calibration cohort from "
                "the tune partition and MUST record it; no other precision class has one"
            )
        return self


# =====================================================================================
# Problem rendering
# =====================================================================================
#: `medos/medos/training/errors.py` renders `class: "authorization"` on the two refusals that
#: are permission answers rather than clinical ones. That word is not one of table
#: 10.4-A's six and `MOS-API-038` closes the enum, so the boundary maps it to the
#: registered spelling. `MOS-API-112` pins `authz_error` for `training-use-not-permitted`,
#: which is the same answer arrived at from the other direction.
_CLASS_AT_THE_BOUNDARY: Final[dict[str, str]] = {"authorization": "authz_error"}


def _problem(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    problem_class: str,
    **extensions: Any,
) -> Response:
    return problem_response(
        build_problem(
            status=status,
            code=code,
            title=title,
            detail=detail,
            problem_class=problem_class,
            instance=request.url.path,
            trace_id=trace_id_of(request),
            **extensions,
        )
    )


def _refusal(request: Request, exc: RefusalError) -> Response:
    """Render an engine refusal AS THE ENGINE RAISED IT. See the module docstring.

    `type` is carried through unrewritten (register entry 80 owns the authority
    question), `code` is derived from the slug so that `MOS-API-037`'s "code is stable per
    type" is a function rather than a table anybody can desynchronise, and `refusals[]` is
    the engine's own array -- which is what makes the document actionable: `check_id`,
    `code`, `message`, `observed` and `bound` per failed check, the same vocabulary a
    `ValidationReport` reproduces (`MOS-EVID-036` keys waivers on `check_id`).
    """
    raw = exc.as_problem()
    slug = str(raw.get("type", "")).rsplit("/", 1)[-1] or "unspecified"
    code = slug.upper().replace("-", "_")
    wire_class = _CLASS_AT_THE_BOUNDARY.get(str(raw.get("class")), str(raw.get("class")))
    doc: dict[str, Any] = {
        "type": raw["type"],
        "title": raw.get("title", "refused"),
        "status": int(raw.get("status", 422)),
        "detail": str(raw.get("detail", "")),
        "instance": request.url.path,
        "class": wire_class,
        "code": code,
        "retryable": wire_class in RETRYABLE_CLASSES,
        "trace_id": trace_id_of(request),
        "occurred_at": now_rfc3339(),
        "refusals": json_safe(raw.get("refusals", [])),
        "check_ids": list(exc.check_ids),
    }
    return problem_response(doc)


def _environment_problem(request: Request, exc: EnvironmentNotRecorded) -> Response:
    """`503`, class `system_failure`, `retryable: false`. A platform fault, not a mistake.

    `MOS-UI-160` separates a refusal the operator caused from one the deployment caused,
    and this is squarely the second: nothing the caller can change to the request body
    will make a deployment that never declared its training image able to record a
    `MOS-TRAIN-124` binding. The default class for `503` is `transport_failure`, which
    carries `retryable: true`; that would be a lie, so the class is passed explicitly.
    """
    return _problem(
        request,
        status=503,
        code="TRAINING_ENVIRONMENT_NOT_RECORDED",
        title="This deployment has not declared its training environment",
        detail=(
            f"MOS-TRAIN-124 binds inputs that describe the deployment rather than the "
            f"cohort, and this platform has no table for them. Set {ENVIRONMENT_VAR} to "
            f"a JSON object (or @path to one) carrying {list(_ENVIRONMENT_KEYS)}. "
            + exc.extra
        ),
        problem_class="system_failure",
        retryable=False,
        missing=list(exc.missing),
        environment_variable=ENVIRONMENT_VAR,
    )


def _not_found(request: Request, kind: str, ident: str) -> Response:
    """404, and the same 404 for "no such row" and "another tenant's row".

    Row-level security makes the two indistinguishable to this handler by construction,
    which is the point: `MOS-SEC-072`'s isolation would be worth nothing if the API told
    a caller that a row it may not read exists.
    """
    return _problem(
        request,
        status=404,
        code=f"{kind.upper()}_NOT_FOUND",
        title=f"No such {kind.replace('_', ' ')}",
        detail=f"no {kind} {ident} is visible to this tenant",
        problem_class="client_error",
    )


def _malformed(request: Request, code: str, detail: str) -> Response:
    return _problem(
        request,
        status=400,
        code=code,
        title="Malformed identifier",
        detail=detail,
        problem_class="client_error",
    )


def _require(request: Request, permission: str) -> Response | None:
    """403 unless the credential's scope carries `permission`.

    `medos/medos/security/scopes.py` explains at length why a scope entry is a CEILING and not a
    grant (`MOS-SEC-027`, `MOS-SEC-045`): the effective set is the intersection of the
    credential's scope, the principal's role grants and the manifest's declared
    permissions, and this platform has no roles table. What is enforced here is the term
    that exists, stated rather than dressed up -- exactly the posture
    `medos/medos/api/routes_reviews.py` takes, so the codebase has one answer and not two.
    """
    if permission not in PERMISSIONS:  # pragma: no cover - a typo, caught at review
        raise RuntimeError(f"{permission!r} is not a permission this module declares")
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
    if not scope_permits(principal.scope, permission):
        return _problem(
            request,
            status=403,
            code="PERMISSION_DENIED",
            title="Permission denied",
            detail=f"this credential does not carry {permission!r} "
            f"({PERMISSIONS[permission]})",
            problem_class="authz_error",
            required_permission=permission,
        )
    return None


def _actor(request: Request) -> audit.Actor:
    """The principal as `MOS-SEC-146`'s actor. `MOS-UI-156` requires the console to name
    the person who cancelled a run; the name comes from the credential and the audit
    event, never from a request body a client could fill in with somebody else's."""
    principal = principal_of(request)
    assert principal is not None
    return audit.Actor(kind=principal.kind, id=str(principal.principal_id), auth="api_key")


# =====================================================================================
# Projections. One function per response schema, so the wire shape is greppable.
# =====================================================================================
#: Columns of `training_runs` that `medos/schemas/training/training-run-1.0.0.json` deliberately
#: does NOT project, with the requirement that keeps each off the wire. Named rather than
#: filtered by a whitelist so that a new column arrives as a decision instead of silently
#: appearing in every client's response.
_RUN_NOT_PROJECTED: Final[tuple[str, ...]] = (
    "id",                      # the public id is the identity on the wire (MOS-CORE-028)
    "tenant_id",               # never a response member (MOS-API-003)
    "hyperparameters",         # MOS-UI-149: the batch size is not readable off the wire
    "search_trial_score",      # MOS-TRAIN-222: no selection statistic on a UI surface
    "fingerprint_bucket",      # MOS-TRAIN-223 / MOS-TRAIN-141: addresses, not digests
    "fingerprint_object_key",
    "bundle_bucket",
    "bundle_object_key",
    "orchestrator_run_id",     # the driver's handle; no client acts on it
    "updated_at",
)


def _run_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `training_runs` row as `TrainingRun`. Rows R6, R7, R8 and R9."""
    out = {k: v for k, v in row.items() if k not in _RUN_NOT_PROJECTED}
    out["training_run_id"] = out.pop("public_id")
    return json_safe(out)  # type: ignore[no-any-return]


def _search_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `configuration_searches` row as `ConfigurationSearch`. Rows R13, R14, R15."""
    out = {k: v for k, v in row.items() if k not in ("id", "tenant_id", "updated_at")}
    out["configuration_search_id"] = out.pop("public_id")
    return json_safe(out)  # type: ignore[no-any-return]


def _conversion_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """One `conversion_runs` row as `ConversionRun`. Rows R16 and R17."""
    out = {k: v for k, v in row.items() if k not in ("id", "tenant_id", "updated_at")}
    out["conversion_run_id"] = out.pop("public_id")
    return json_safe(out)  # type: ignore[no-any-return]


# =====================================================================================
# R6  POST /api/v1/training-runs
# =====================================================================================
@router.post("/training-runs", status_code=202, summary="Train")
def submit_training_run(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    body: TrainingRunSubmitRequest,
) -> Any:
    """`MOS-UI-147`'s one control. Refuses before anything expensive happens.

    The order of the checks is the order that produces the most useful refusal first, and
    it is the engine's order, not a second one: the tenant's training-use flag
    (`MOS-TRAIN-072`, a permission answer), then what the deployment declared, then the
    rows the four ids name, then `medos.training.runs.submit`, which performs the leakage
    re-execution of `MOS-TRAIN-115`, the binding completeness of `MOS-TRAIN-124` and the
    backend rule of `MOS-TRAIN-211`. This handler adds no second copy of any of them --
    `MOS-UI-116`'s argument is that a second implementation disagrees with the
    authoritative one on exactly the cases where it matters.
    """
    denied = _require(request, "training_run.submit")
    if denied is not None:
        return denied

    # MOS-TRAIN-072, first, because it is the answer that no change to the request body
    # can alter: "There is no per-study, per-user or per-environment override, and no
    # platform-administrator bypass."
    try:
        train_policy.assert_harvest_permitted(conn)
    except HarvestRefused as exc:
        return _refusal(request, exc)

    try:
        environment = load_training_environment()
        spec = environment.spec_for(body.capability_id)
    except EnvironmentNotRecorded as exc:
        return _environment_problem(request, exc)

    with tenant_tx(conn) as tx:
        dataset = tx.execute(
            "SELECT manifest_digest FROM dataset_versions WHERE id = %s",
            (_uuid.UUID(body.dataset_version_id),),
        ).fetchone()
        split = tx.execute(
            "SELECT manifest_digest AS split_digest, leakage_report "
            "FROM dataset_splits WHERE id = %s",
            (_uuid.UUID(body.split_id),),
        ).fetchone()
        annotations = tx.execute(
            "SELECT manifest_digest AS annotation_digest FROM annotation_sets "
            "WHERE id = %s",
            (_uuid.UUID(body.annotation_set_id),),
        ).fetchone()
    if dataset is None:
        return _not_found(request, "dataset_version", body.dataset_version_id)
    if split is None:
        return _not_found(request, "dataset_split", body.split_id)
    if annotations is None:
        return _not_found(request, "annotation_set", body.annotation_set_id)

    output_kind = str(spec["output_kind"])
    kind = (
        body.training_backend.kind
        if body.training_backend is not None
        else autoconfig.default_backend_for(output_kind)
    )
    if kind is None:
        # MOS-TRAIN-211 fixes the default for a `label` capability and for no other, so
        # there is no auto-configuring choice this surface may make for one. Refusing is
        # the honest answer; picking `nnunet` anyway would be the API deciding something
        # no requirement authorises and recording it as though a rule had.
        return _problem(
            request,
            status=422,
            code="TRAINING_BACKEND_NOT_DERIVABLE",
            title="No auto-configured backend for this output kind",
            detail=(
                f"MOS-TRAIN-211 fixes a dataset-fingerprint auto-configuring default only "
                f"for a capability whose io.output_kind is 'label'; this one is "
                f"{output_kind!r}. MOS-UI-148 forbids the no-code console offering a "
                f"backend chooser, so this capability is not trainable from that surface"
            ),
            problem_class="client_error",
            output_kind=output_kind,
        )

    try:
        version = environment.version_of(kind)
    except EnvironmentNotRecorded as exc:
        return _environment_problem(request, exc)

    binding = tr.RunBinding(
        capability_id=body.capability_id,
        dataset_version_id=body.dataset_version_id,
        dataset_version_digest=str(dict(dataset)["manifest_digest"]),
        split_id=body.split_id,
        split_digest=str(dict(split)["split_digest"]),
        annotation_set_id=body.annotation_set_id,
        annotation_digest=str(dict(annotations)["annotation_digest"]),
        preprocessing_spec_id=str(spec["id"]),
        preprocessing_spec_version=int(spec["version"]),
        preprocessing_spec_digest=str(spec["digest"]),
        code_commit=environment.code_commit,
        code_dirty=environment.code_dirty,
        image_digest=environment.image_digest,
        # `plan_digest` is None here and MUST be: MOS-TRAIN-135 freezes the derived plan
        # at RUN START, in `medos.training.runs.start`, and a plan digest supplied at
        # submit would be a digest of a plan nothing had derived yet.
        training_backend={"kind": kind, "version": version, "plan_digest": None},
        # MOS-TRAIN-223: the plan is derived from the cohort fingerprint by the backend.
        # An empty set here is the honest record of "this run configured nothing by hand",
        # and it is what makes `hyperparameters_digest` distinguish the auto-configured
        # runs of one cohort from a hand-tuned one.
        hyperparameters={},
        seeds=dict(environment.seeds),
        determinism=dict(environment.determinism),
        hardware=dict(environment.hardware),
        framework_versions=dict(environment.framework_versions),
        backend_rationale=body.backend_rationale,
        search_id=body.search_id,
        trial_index=body.trial_index,
    )

    try:
        run = tr.submit(
            conn,
            binding=binding,
            actor=_actor(request),
            trace_id=trace_id_of(request),
            leakage=dict(split)["leakage_report"],
            output_kind=output_kind,
        )
    except RunRefused as exc:
        return _refusal(request, exc)
    except HarvestRefused as exc:  # pragma: no cover - the gate above catches it first
        return _refusal(request, exc)
    except PartitionForbidden as exc:  # pragma: no cover - no declared route names one
        return _refusal(request, exc)
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        # 0013's `training_runs_run_digest_uk UNIQUE (tenant_id, run_digest)`. The same
        # binding is the same experiment (MOS-TRAIN-124), so a re-submission is a
        # conflict and not a second run. `medos/api/v1/routes.train.yaml` does not list a slug
        # for this and the change that serves R6 owes chapter 10 one; a 500 here would be
        # the wrong answer to a correct database rule.
        return _problem(
            request,
            status=409,
            code="TRAINING_RUN_DIGEST_CONFLICT",
            title="A run with this binding already exists",
            detail=(
                "MOS-TRAIN-124's run_digest is over the full binding, so a resubmission "
                "of the same cohort, split, reference standard, image and backend is the "
                "same experiment. Change something about the binding, or read the "
                "existing run"
            ),
            problem_class="client_error",
        )
    conn.commit()

    location = f"/api/v1/training-runs/{run.public_id}"
    response.headers["Location"] = location  # MOS-API-046
    return _run_view(run.as_dict())


# =====================================================================================
# R7  GET /api/v1/training-runs
# =====================================================================================
@router.get("/training-runs", summary="List training runs")
def list_training_runs(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    capability_id: Annotated[str | None, Query(max_length=64)] = None,
    split_digest: Annotated[str | None, Query(max_length=71)] = None,
    search_id: Annotated[str | None, Query(max_length=64)] = None,
    state: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """`MOS-API-015`'s envelope over `TrainingRun`, `MOS-API-018`'s ordering.

    `next_cursor` IS ALWAYS NULL AND `has_more` IS HONEST. `MOS-API-017` makes the cursor
    an opaque keyset over the sort key, the tie-break id and a fingerprint of the filter
    set, and `medos.training.runs.list_runs` takes `limit` and four bound columns and no
    cursor. Implementing the continuation HERE would mean a second query over
    `training_runs` written in the HTTP layer -- the thing CONTRACT.md section 1 forbids
    and the thing that goes stale when chapter 17 changes the ordering. So this page is
    bounded and says so: a client seeing `has_more: true` narrows its filters. The engine
    owes `cursor=`; that is an edit to `medos/medos/training/runs.py` and not to this file.
    """
    denied = _require(request, "training_run.read")
    if denied is not None:
        return denied
    if search_id is not None and not _UUID_RE.match(search_id):
        return _malformed(
            request,
            "INVALID_SEARCH_ID",
            "search_id is the uuid of a configuration_searches row",
        )
    if split_digest is not None and not _DIGEST_RE.match(split_digest):
        return _malformed(
            request,
            "INVALID_SPLIT_DIGEST",
            "split_digest is 'sha256:' followed by 64 lower-case hex characters",
        )
    rows = tr.list_runs(
        conn,
        capability_id=capability_id,
        split_digest=split_digest,
        search_id=search_id,
        state_in=[state] if state else None,
        limit=limit + 1,
    )
    return {
        "items": [_run_view(r.as_dict()) for r in rows[:limit]],
        "next_cursor": None,
        "has_more": len(rows) > limit,
    }


# =====================================================================================
# R8  GET /api/v1/training-runs/{training_run_id}
# =====================================================================================
@router.get("/training-runs/{training_run_id}", summary="Read one training run")
def get_training_run(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    training_run_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """The `MOS-TRAIN-124` binding, as recorded. `MOS-UI-152` reads the run from here."""
    denied = _require(request, "training_run.read")
    if denied is not None:
        return denied
    bad = _bad_run_id(request, training_run_id)
    if bad is not None:
        return bad
    try:
        run = tr.get(conn, training_run_id)
    except TrainingError:
        return _not_found(request, "training_run", training_run_id)
    return _run_view(run.as_dict())


def _bad_run_id(request: Request, training_run_id: str) -> Response | None:
    if _RUN_ID_RE.match(training_run_id):
        return None
    return _malformed(
        request,
        "INVALID_TRAINING_RUN_ID",
        "a TrainingRun id is 'tr_' followed by a 26-character upper-case Crockford "
        "base32 ULID (MOS-CORE-028, 0013_training.up.sql)",
    )


# =====================================================================================
# R9  POST /api/v1/training-runs/{training_run_id}/cancel
# =====================================================================================
@router.post("/training-runs/{training_run_id}/cancel", summary="Cancel a training run")
def cancel_training_run(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    training_run_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """`Orchestrator.Cancel` as a deliberate act attributed to a person (`MOS-UI-156`).

    The body is empty and that is a decision: 0013 admits a `failure_reason` only on
    `FAILED`, so a cancellation reason has nowhere to be stored, and a field the server
    discards is a field a client will believe was recorded.

    The 404 and the 409 are told apart by asking the engine twice rather than by copying
    its rule: `get` answers "does this run exist for this tenant", `cancel` answers "is it
    still cancellable". A terminal-state check written here would be a second copy of
    `medos.training.runs._terminate`'s precondition and would drift from it.
    """
    denied = _require(request, "training_run.cancel")
    if denied is not None:
        return denied
    bad = _bad_run_id(request, training_run_id)
    if bad is not None:
        return bad
    try:
        tr.get(conn, training_run_id)
    except TrainingError:
        return _not_found(request, "training_run", training_run_id)
    try:
        run = tr.cancel(conn, run_id=training_run_id)
    except psycopg.errors.CheckViolation as exc:
        conn.rollback()
        # A PLATFORM DEFECT, FOUND BY SERVING THIS ROW, AND NOT PAPERED OVER.
        #
        # 0013's `training_runs_fingerprint_frozen` reads "state = 'PENDING' OR kind =
        # 'monai_supervised' OR fingerprint_digest IS NOT NULL", on the argument that an
        # auto-configuring backend freezes its plan at run start (MOS-TRAIN-135 /
        # MOS-TRAIN-223) and so from RUNNING onward the digest exists. CANCELLED from
        # PENDING was not considered: the run leaves PENDING without ever passing through
        # `start()`, so there is no digest and the CHECK refuses the transition. The
        # effect is that a queued nnunet or auto3dseg run -- which is EVERY run the
        # no-code console submits, because MOS-UI-148 fixes it to those two -- cannot be
        # cancelled until it is running.
        #
        # This is rendered as a `system_failure` rather than a 409, because a 409 would
        # say the platform refused on purpose and it did not: MOS-UI-156 requires the
        # control. It is not rendered as a bare 500 either -- an operator who is told
        # "internal error, quote the trace id" cannot tell this apart from a dead
        # database, and the two need different people. Resolving it is an edit to 0013
        # and to `medos/medos/training/runs.py`, neither of which this change owns.
        return _problem(
            request,
            status=500,
            code="TRAINING_RUN_CANCEL_BLOCKED",
            title="This deployment cannot cancel a queued auto-configured run",
            detail=(
                "MOS-UI-156 requires this control and the training_runs schema refuses "
                "the PENDING -> CANCELLED transition for a run whose backend freezes a "
                "derived plan at run start (MOS-TRAIN-135, MOS-TRAIN-223): the run never "
                "started, so it has no fingerprint_digest. Cancelling the run once it is "
                "RUNNING succeeds. This is a platform defect, not a property of this run"
            ),
            problem_class="system_failure",
            retryable=False,
            blocking_constraint=str(exc.diag.constraint_name or "unknown"),
        )
    except TrainingError as exc:
        # `medos/api/v1/routes.train.yaml` records that chapter 10 has no slug for this conflict
        # and that the change serving R9 owes one. It is emitted rather than listed, because
        # listing a slug in the registry is that file's edit and rendering a 500 for a
        # correct refusal is this file's defect.
        return _problem(
            request,
            status=409,
            code="TRAINING_RUN_NOT_CANCELLABLE",
            title="Training run is already terminal",
            detail=str(exc),
            problem_class="client_error",
        )
    conn.commit()
    return _run_view(run.as_dict())


# =====================================================================================
# R10 GET /api/v1/capabilities/{capability_id}/seed-variance
# =====================================================================================
@router.get(
    "/capabilities/{capability_id}/seed-variance",
    summary="Seed-variance characterisation for a capability",
)
def get_capability_seed_variance(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    capability_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """`MOS-TRAIN-127`, and `200` with `characterised: false` rather than `404`.

    A 404 cannot tell "this capability has no characterisation" apart from "there is no
    such capability", and `MOS-UI-157` branches the screen on exactly that difference:
    when none exists the console offers to queue the two further runs that would produce
    one, because an operator who does not know the obligation is owed will produce one
    candidate and discover it at a gate they cannot reach.

    Bound to `training_run.read` and not to `capability.read` although the path sits under
    `/capabilities`: the row names its `training_run_ids`, and a principal who may not
    read a run may not read a list of them either.

    GAP, RECORDED: `medos.training.runs.seed_variance()` returns `MOS-TRAIN-127`'s own
    shape -- metric, runs, sd, backend, gpu count, timestamp -- and the response schema
    also requires `training_run_ids` (why the permission is what it is) and
    `hyperparameters_digest` (half of "is this characterisation still about the thing
    being promoted?"). The engine is asked whether a live characterisation exists and what
    it says; the two projection-only columns are read beside it. The engine owes them on
    its return value, which is chapter 17's edit.
    """
    denied = _require(request, "training_run.read")
    if denied is not None:
        return denied
    if not _CAPABILITY_RE.match(capability_id):
        return _malformed(
            request,
            "INVALID_CAPABILITY_ID",
            "a capability id is lower snake_case, 3-64 characters (MOS-REG-041)",
        )
    live = tr.seed_variance(conn, capability_id=capability_id)
    if live is None:
        return {
            "capability_id": capability_id,
            "characterised": False,
            "runs_required": 3,
            "characterisation": None,
        }
    with tenant_tx(conn) as tx:
        extra = tx.execute(
            "SELECT training_run_ids, hyperparameters_digest "
            "FROM capability_seed_variance "
            "WHERE capability_id = %s AND superseded_at IS NULL",
            (capability_id,),
        ).fetchone()
    detail = dict(extra or {})
    return json_safe(
        {
            "capability_id": capability_id,
            "characterised": True,
            "runs_required": 3,
            "characterisation": {
                **live,
                "training_run_ids": detail.get("training_run_ids") or [],
                "hyperparameters_digest": detail.get("hyperparameters_digest"),
            },
        }
    )


# =====================================================================================
# R11 GET /api/v1/training-runs/{training_run_id}/test-exposure
# =====================================================================================
@router.get(
    "/training-runs/{training_run_id}/test-exposure",
    summary="The test-exposure counter for this run's capability and split",
)
def get_training_run_test_exposure(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    training_run_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """Section 19.3.6's only row, and a read. `MOS-TRAIN-216`, `MOS-UI-163`.

    Keyed on a run rather than on the `(capability_id, split_digest)` pair the counter is
    defined over, because a path segment holding a digest would require the operator to
    compose one and `MOS-UI-101` forbids that in any task the console offers. The pair is
    read off the run.

    `monotonic` is a server-asserted constant and not console copy: `MOS-UI-163` requires
    the figure to be labelled as a count that only goes up, and the same sentence written
    into a template is a claim one client makes and another may not. The database holds
    the assertion twice over (a decrement trigger and a delete trigger), so it cannot
    become a lie without a migration.
    """
    denied = _require(request, "training_run.read")
    if denied is not None:
        return denied
    bad = _bad_run_id(request, training_run_id)
    if bad is not None:
        return bad
    try:
        run = tr.get(conn, training_run_id)
    except TrainingError:
        return _not_found(request, "training_run", training_run_id)

    capability_id = run.capability_id
    split_digest = str(run["split_digest"])
    count = tr.test_exposure_count(
        conn, capability_id=capability_id, split_digest=split_digest
    )
    # The timestamps are projection-only and `medos.training.runs.test_exposure_count`
    # returns the scalar the requirement is about. Read here rather than recomputed: the
    # COUNT stays the engine's answer, which is the number with a rule attached.
    with tenant_tx(conn) as tx:
        stamps = tx.execute(
            "SELECT first_exposed_at, last_exposed_at FROM split_test_exposure "
            "WHERE capability_id = %s AND split_digest = %s",
            (capability_id, split_digest),
        ).fetchone()
    window = dict(stamps or {})
    return json_safe(
        {
            "capability_id": capability_id,
            "split_digest": split_digest,
            "exposure_count": count,
            "monotonic": True,
            "first_exposed_at": window.get("first_exposed_at"),
            "last_exposed_at": window.get("last_exposed_at"),
        }
    )


# =====================================================================================
# R12 GET /api/v1/training-runs/{training_run_id}/candidate
# =====================================================================================
#: The projection of section 19.3.7. Written as ONE statement over four tables because it
#: is one question -- "what became of this run's candidate, and who decides?" -- and
#: because `MOS-UI-116`'s argument one screen earlier is that a client which joins the
#: rows itself renders a state no server asserted.
#:
#: `criteria_verdict` is taken from the ACTIVE `ValidationReport` that CITES the candidate
#: evaluation run (`evaluation_run_ids @> [e.id]`) rather than from a subject-id match:
#: the citation is unambiguous, and `MOS-EVID-083`'s three states are the report's own
#: column. `INDETERMINATE` is carried through as itself -- `MOS-UI-164` forbids folding it
#: into `FAIL`, because one needs more data and the other needs a different model.
_CANDIDATE_SQL: Final[str] = """
    SELECT r.public_id                       AS training_run_id,
           r.candidate_model_version_id      AS candidate_model_version_id,
           a.lifecycle_status                AS lifecycle_status,
           e.public_id                       AS evaluation_run_id,
           v.verdict                         AS criteria_verdict,
           coalesce(x.exposure_count, 0)     AS test_exposure_count
      FROM training_runs r
      LEFT JOIN artifacts a
             ON a.id = r.candidate_model_version_id
      LEFT JOIN LATERAL (
            SELECT id, public_id
              FROM evaluation_runs
             WHERE model_version_id = r.candidate_model_version_id
               AND partition = 'test'
               AND split_digest = r.split_digest
             ORDER BY created_at DESC
             LIMIT 1) e ON true
      LEFT JOIN LATERAL (
            SELECT verdict
              FROM validation_reports
             WHERE e.id = ANY(evaluation_run_ids)
               AND status = 'ACTIVE'
             ORDER BY report_version DESC
             LIMIT 1) v ON true
      LEFT JOIN split_test_exposure x
             ON x.capability_id = r.capability_id
            AND x.split_digest = r.split_digest
     WHERE r.public_id = %s
"""


@router.get(
    "/training-runs/{training_run_id}/candidate",
    summary="What became of the candidate, and who decides -- read-only",
)
def get_training_run_candidate(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    training_run_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """Section 19.3.7 gets ONE READ AND NOTHING THAT ACTS.

    `MOS-UI-167` requires the promotion control to be ABSENT -- not disabled, not behind a
    tooltip. `MOS-TRAIN-003` makes the pipeline's terminal state a candidate at
    `VALIDATED` and says the pipeline MUST NOT BE CAPABLE of producing any state beyond
    it. `MOS-TRAIN-008` puts `VALIDATED -> APPROVED` in a named human's hands and
    `MOS-SEC-158` enforces that by the ABSENCE of the grant rather than by a check in
    code -- so an approval route here would be that grant, written down and reachable
    whatever the role table later says.

    A DOSSIER ROUTE WAS REJECTED FOR A DIFFERENT REASON. `MOS-TRAIN-011` requires the
    promotion decision to be made against the full evidence set -- the ValidationReport,
    the diff against the incumbent, per-stratum results, the regression comparison and the
    corpus stratification report -- and `MOS-UI-170` puts that in section 19.5, on a
    different surface, under the separation of duties that keeps it there, saying in terms
    that 19.3.7 "adds no path to it". `MOS-TRAIN-011` also says a promotion UI showing one
    aggregate number and a button is non-conformant precisely because it reproduces the
    automated path with a human-shaped delay in it. Hence: no metric here, no link, no
    action, no deployment reference. `MOS-UI-165` and `MOS-EVID-056` are the same rule for
    the number specifically.

    This route binds `model_version.read` -- class `read`, `MOS-UI-102`'s reference
    principal names it -- and reads three rows. It is the only member of this module that
    names no engine function, because there is no act to name.
    """
    denied = _require(request, "model_version.read")
    if denied is not None:
        return denied
    bad = _bad_run_id(request, training_run_id)
    if bad is not None:
        return bad
    with tenant_tx(conn) as tx:
        row = tx.execute(_CANDIDATE_SQL, (training_run_id,)).fetchone()
    if row is None:
        return _not_found(request, "training_run", training_run_id)
    projected = dict(row)
    projected["promotion"] = dict(PROMOTION_NOTICE)
    return json_safe(projected)


# =====================================================================================
# R13 POST /api/v1/configuration-searches
# =====================================================================================
@router.post("/configuration-searches", status_code=201, summary="Declare a search")
def declare_configuration_search(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    body: ConfigurationSearchDeclareRequest,
) -> Any:
    """`MOS-TRAIN-218`. NOT A CONSOLE SURFACE -- `MOS-UI-150` keeps it off 19.3.

    A bounded budget, a declarative space and a nomination are three decisions a no-code
    operator has no basis for making, spending a resource -- the `test` partition -- they
    cannot see being spent. The route exists at all because `MOS-TRAIN-213` catches ANY
    procedure producing more than one trained artifact from one cohort and keeping a
    subset by a score computed on data, and `MOS-TRAIN-218` requires every one to produce
    a record: a platform that serves `TrainingRun` and not this invites the search to be
    run outside the API and the record to be written afterwards, which is the tidier-
    looking and worse audit trail `MOS-TRAIN-216` describes.

    `backend` and `fingerprint_digest` are server-derived for the same reason the run's
    are, and `space` is NOT: `MOS-TRAIN-220` makes the space the caller's declaration and
    digests it, and a space this surface composed would be a space nobody declared.
    """
    denied = _require(request, "configuration_search.declare")
    if denied is not None:
        return denied
    try:
        environment = load_training_environment()
        spec = environment.spec_for(body.capability_id)
    except EnvironmentNotRecorded as exc:
        return _environment_problem(request, exc)

    with tenant_tx(conn) as tx:
        split = tx.execute(
            "SELECT manifest_digest AS split_digest FROM dataset_splits WHERE id = %s",
            (_uuid.UUID(body.split_id),),
        ).fetchone()
        annotations = tx.execute(
            "SELECT manifest_digest AS annotation_digest FROM annotation_sets "
            "WHERE id = %s",
            (_uuid.UUID(body.annotation_set_id),),
        ).fetchone()
    if split is None:
        return _not_found(request, "dataset_split", body.split_id)
    if annotations is None:
        return _not_found(request, "annotation_set", body.annotation_set_id)

    kind = autoconfig.default_backend_for(str(spec["output_kind"]))
    if kind is None:
        return _problem(
            request,
            status=422,
            code="TRAINING_BACKEND_NOT_DERIVABLE",
            title="No auto-configured backend for this output kind",
            detail=(
                "MOS-TRAIN-211 fixes an auto-configuring default only for a capability "
                f"whose io.output_kind is 'label'; this one is {spec['output_kind']!r}"
            ),
            problem_class="client_error",
        )
    try:
        version = environment.version_of(kind)
    except EnvironmentNotRecorded as exc:
        return _environment_problem(request, exc)

    try:
        row = search_mod.create(
            conn,
            capability_id=body.capability_id,
            dataset_version_id=body.dataset_version_id,
            split_id=body.split_id,
            split_digest=str(dict(split)["split_digest"]),
            annotation_digest=str(dict(annotations)["annotation_digest"]),
            backend={"kind": kind, "version": version},
            fingerprint_digest=str(spec["digest"]),
            space=body.space.model_dump(exclude_none=True),
            strategy=body.strategy,
            seed=body.seed,
            selection_metric=body.selection_metric,
            selection_partition=body.selection_partition,
            selection_rule=body.selection_rule,
            trials_planned=body.trials_planned,
            budget=body.budget.model_dump(),
            code_commit=environment.code_commit,
            image_digest=environment.image_digest,
            selection_folds=body.selection_folds,
            parent_search_id=body.parent_search_id,
        )
    except SearchRefused as exc:
        return _refusal(request, exc)
    conn.commit()
    public_id = str(dict(row)["public_id"])
    response.headers["Location"] = f"/api/v1/configuration-searches/{public_id}"
    return _search_view(dict(row))


# =====================================================================================
# R14 GET /api/v1/configuration-searches/{configuration_search_id}
# =====================================================================================
@router.get(
    "/configuration-searches/{configuration_search_id}",
    summary="Read a configuration search",
)
def get_configuration_search(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    configuration_search_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """The space, the budget, the cost and the nomination. `MOS-TRAIN-218`."""
    denied = _require(request, "configuration_search.read")
    if denied is not None:
        return denied
    if not _SEARCH_ID_RE.match(configuration_search_id):
        return _malformed(
            request,
            "INVALID_CONFIGURATION_SEARCH_ID",
            "a ConfigurationSearch id is 'cs_' followed by a 26-character upper-case "
            "Crockford base32 ULID",
        )
    try:
        row = search_mod.get(conn, configuration_search_id)
    except TrainingError:
        return _not_found(request, "configuration_search", configuration_search_id)
    return _search_view(row)


# =====================================================================================
# R15 POST /api/v1/configuration-searches/{configuration_search_id}/nomination
# =====================================================================================
@router.post(
    "/configuration-searches/{configuration_search_id}/nomination",
    summary="Nominate the one trial MOS-TRAIN-216 admits",
)
def nominate_configuration_search_trial(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    configuration_search_id: Annotated[str, Path(max_length=64)],
    body: ConfigurationSearchNominationRequest,
) -> Any:
    """A separate route and a separate permission from `declare`, because this act spends
    the test partition.

    `MOS-TRAIN-217` makes a later nomination an explicit recorded act with its own row and
    a fresh increment of `test_exposure_count`, on the grounds that a silent second
    nomination off the back of a first test run is selection on test performed one
    candidate at a time. A grant that bundled `declare` and `nominate` would let whoever
    may start a search also spend the consumable, which is the separation `MOS-TRAIN-216`
    is about. The path segment is singular because there is exactly one, and 0013's
    `training_runs_one_nomination` partial unique index refuses a second.

    The body names runs by their `tr_` public ids -- `MOS-UI-101`'s rule against making a
    caller compose an internal identifier -- and `medos.training.search.nominate` takes
    uuids, so the two ids are resolved through `medos.training.runs.get`, which also makes
    a run in another tenant a 404 rather than a foreign-key error.
    """
    denied = _require(request, "configuration_search.nominate")
    if denied is not None:
        return denied
    if not _SEARCH_ID_RE.match(configuration_search_id):
        return _malformed(
            request,
            "INVALID_CONFIGURATION_SEARCH_ID",
            "a ConfigurationSearch id is 'cs_' followed by a 26-character ULID",
        )
    resolved: dict[str, str] = {}
    for field_name, public_id in (
        ("training_run_id", body.training_run_id),
        ("runner_up_training_run_id", body.runner_up_training_run_id),
    ):
        try:
            resolved[field_name] = tr.get(conn, public_id).id
        except TrainingError:
            return _not_found(request, "training_run", public_id)
    try:
        row = search_mod.nominate(
            conn,
            search_id=configuration_search_id,
            training_run_id=resolved["training_run_id"],
            runner_up_training_run_id=resolved["runner_up_training_run_id"],
            selection_margin=body.selection_margin,
            cost=body.cost.model_dump(exclude_none=True),
            trials_completed=body.trials_completed,
            trials_failed=body.trials_failed,
        )
    except SearchRefused as exc:
        return _refusal(request, exc)
    except TrainingError:
        return _not_found(request, "configuration_search", configuration_search_id)
    conn.commit()
    return _search_view(row)


# =====================================================================================
# R16 POST /api/v1/conversion-runs
# =====================================================================================
@router.post("/conversion-runs", status_code=202, summary="Submit a conversion run")
def submit_conversion_run(
    request: Request,
    response: Response,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    body: ConversionRunSubmitRequest,
) -> Any:
    """`MOS-TRAIN-156`. Chapter 19 gives conversion no screen at all, so `MOS-UI-101`'s
    no-digest rule does not reach it and `equivalence_cohort_digest` may be a member.

    Declared and served because a conversion performed outside the API and recorded
    afterwards is a `ModelVersion` whose equivalence evidence nobody watched being
    produced. The refusals are `medos.training.conversion`'s: an `int8` conversion that
    names no `tune` calibration cohort, a calibration on anything but `tune`, and a source
    that is `SUSPENDED` or `RECALLED` -- the last because a recall that stops at the
    source while its conversion keeps serving is the recall failing at the only point
    where it mattered (`MOS-TRAIN-169`).
    """
    denied = _require(request, "conversion_run.submit")
    if denied is not None:
        return denied
    try:
        environment = load_training_environment()
    except EnvironmentNotRecorded as exc:
        return _environment_problem(request, exc)
    try:
        row = conv.create(
            conn,
            source_model_version_id=body.source_model_version_id,
            target_format=body.target_format,
            precision=body.precision,
            toolchain=body.toolchain,
            equivalence_cohort_digest=body.equivalence_cohort_digest,
            code_commit=environment.code_commit,
            image_digest=environment.image_digest,
            calibration_dataset_version_id=body.calibration_dataset_version_id,
            calibration_partition=body.calibration_partition,
        )
    except ConversionRefused as exc:
        return _refusal(request, exc)
    except TrainingError:
        return _not_found(request, "model_version", body.source_model_version_id)
    conn.commit()
    public_id = str(dict(row)["public_id"])
    response.headers["Location"] = f"/api/v1/conversion-runs/{public_id}"
    return _conversion_view(dict(row))


# =====================================================================================
# R17 GET /api/v1/conversion-runs/{conversion_run_id}
# =====================================================================================
@router.get("/conversion-runs/{conversion_run_id}", summary="Read a conversion run")
def get_conversion_run(
    request: Request,
    conn: Annotated[psycopg.Connection[Any], Depends(db_connection)],
    conversion_run_id: Annotated[str, Path(max_length=64)],
) -> Any:
    """The run AND its full equivalence result, not only the verdict. `MOS-TRAIN-161`."""
    denied = _require(request, "conversion_run.read")
    if denied is not None:
        return denied
    if not _CONVERSION_ID_RE.match(conversion_run_id):
        return _malformed(
            request,
            "INVALID_CONVERSION_RUN_ID",
            "a ConversionRun id is 'cv_' followed by a 26-character upper-case Crockford "
            "base32 ULID",
        )
    try:
        row = conv.get(conn, conversion_run_id)
    except TrainingError:
        return _not_found(request, "conversion_run", conversion_run_id)
    return _conversion_view(row)
