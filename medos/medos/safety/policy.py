# SPDX-License-Identifier: Apache-2.0
"""The four `Deployment` facts chapter 9 needs, behind a port.

`clinical_use_mode` (`MOS-SAFE-033`), `review_mode` and `review_sla_hours`
(`MOS-SAFE-061`, `MOS-SAFE-058`) and `emit_verified_sr_on_accept` (`MOS-SAFE-065`) are all
fields on `Deployment`. There is no `deployments` table in this deployment: chapter 6
section 6.8 owns it, and `0010_safety.up.sql` says at length why a three-column stub here
would be a second answer to a question this release does not own.

A PORT, AND WHY IT IS NOT A CONSTANT
-------------------------------------
`medos.db.queue`'s `JobQueue` establishes the shape: a `Protocol` naming the questions and
one driver answering them, so that the call sites are written against the interface from
the first day and the driver swap is a constructor argument rather than a refactor. The
same reasoning applies here with one addition that is specific to safety: a MODULE
CONSTANT would be read at import time, and `MOS-SAFE-034` requires the mode to be COPIED
into the Job row at creation and into the Result row at result creation as immutable
columns. A constant invites reading it at display time, which is exactly the defect
`MOS-STORE-277` calls out -- "a deployment that later flips to `clinical` must not
retroactively reclassify results produced while it was research-only". A port that must be
called explicitly keeps the copy at the boundary where it belongs.

WHAT THIS MODULE REFUSES
------------------------
`MOS-SAFE-061`: "`mandatory_pre_publication` MUST be refused at Deployment creation in
0.1-0.4 with `class: not_implemented`, `detail: "pre-publication review gating is reserved;
see Chapter 16"`." `DeploymentSafetyPolicy.__post_init__` is where a Deployment comes into
being in this release, so that is where the refusal lives, with the requirement's own
detail string verbatim.

That refusal is the mechanism by which chapter 16's OQ-01 stays open. See
`medos/medos/safety/review.py`'s module docstring.

`MOS-SAFE-033`: "Its default is `research_only`. There is no tenant-wide, service-wide or
environment-wide override. A Deployment created without the field MUST be created as
`research_only`." The dataclass default is `research_only`, `from_env` never widens it, and
there is no environment variable, no tenant column and no config key anywhere in this
module that sets it to `clinical`. Promoting a Deployment to `clinical` is `MOS-SAFE-036`'s
E1 gate -- nine conditions, a named approver, a verifiable `ValidationReport` -- and that
gate is the 0.2.0 evaluation work landing beside this one. Until it exists, the honest
state of every Deployment here is `research_only`, and this module makes that the only
reachable state rather than leaving a config key that would bypass a gate that has not
been built.

Spec: MOS-SAFE-033, MOS-SAFE-034, MOS-SAFE-036, MOS-SAFE-038, MOS-SAFE-058, MOS-SAFE-061,
MOS-SAFE-063, MOS-SAFE-065, MOS-STORE-277, MOS-EVID-101, CONTRACT.md section 11.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

__all__ = [
    "ClinicalUseMode",
    "ReviewMode",
    "REVIEW_MODES",
    "RESERVED_REVIEW_MODES",
    "ReviewModeReserved",
    "DeploymentSafetyPolicy",
    "DeploymentPolicySource",
    "StaticDeploymentPolicySource",
    "default_policy_source",
]

ClinicalUseMode = Literal["research_only", "clinical"]
ReviewMode = Literal[
    "off", "optional", "mandatory_post_publication", "mandatory_pre_publication"
]

# `MOS-SAFE-061`'s four values, in the requirement's order.
REVIEW_MODES: tuple[str, ...] = (
    "off",
    "optional",
    "mandatory_post_publication",
    "mandatory_pre_publication",
)

# Expressible in the enum, refused by the platform in 0.1-0.4.
RESERVED_REVIEW_MODES: frozenset[str] = frozenset({"mandatory_pre_publication"})


# `MOS-SAFE-061`'s `detail`, verbatim, so the string a caller sees is the string the
# specification writes down.
RESERVED_DETAIL = "pre-publication review gating is reserved; see Chapter 16"

# The RFC 9457 `code` this is rendered under. `SCREAMING_SNAKE_CASE` per `MOS-API-037`,
# and `medos.api.problems.problem_type_uri` derives the `type` from it mechanically.
RESERVED_PROBLEM_CODE = "REVIEW_MODE_RESERVED"


class ReviewModeReserved(ValueError):
    """`MOS-SAFE-061`: `mandatory_pre_publication` MUST be refused in 0.1-0.4.

    A `ValueError` and deliberately NOT a `MedosError`.

    SPEC DEFECT, reported rather than silently reconciled (D7). `MOS-SAFE-061` prescribes
    `class: not_implemented`. `MOS-API-038` makes table 10.4-A's class set CLOSED --
    `clinical_rejection`, `client_error`, `authz_error`, `rate_limit`,
    `transport_failure`, `system_failure` -- and states that "a `class` word minted outside
    this table is a defect"; `medos.api.problems.build_problem` raises on one, and
    CONTRACT.md section 9 closes `ProblemClass` at three members besides. Chapter 9's own
    scope note says it does not define "the API envelope (Chapter 10)", so chapter 10 wins
    and this is rendered `class: client_error`, HTTP 422, `code:
    REVIEW_MODE_RESERVED`, carrying `MOS-SAFE-061`'s `detail` string unchanged. The
    machine-readable discriminator a consumer branches on is `code`, which is exact; only
    the coarse `class` word differs from chapter 9's text.

    Being a `ValueError` rather than a `MedosError` is the second half of the same
    decision: a `MedosError` subclass would have to declare a `problem_class` from
    CONTRACT.md section 9's three-value enum, and every one of those three is a lie here.
    Nothing malfunctioned, nothing was rejected clinically, and no peer failed.
    """

    def __init__(self, review_mode: str) -> None:
        super().__init__(f"review_mode {review_mode!r}: {RESERVED_DETAIL}")
        self.review_mode = review_mode
        self.reason_code = "review_mode_reserved"
        self.detail = RESERVED_DETAIL


@dataclass(frozen=True)
class DeploymentSafetyPolicy:
    """The `Deployment` facts chapter 9 reads. Frozen; a change is a new object.

    `deployment_id` is carried because `MOS-SAFE-048`'s (0018,1000) DeviceSerialNumber row
    and `MOS-IMG-134`'s `ContributingEquipmentSequence` both name it, and because
    `MOS-SAFE-044` requires two Deployments of one ServiceVersion to be distinguishable by
    `deployment_id` alone.
    """

    deployment_id: str
    clinical_use_mode: ClinicalUseMode = "research_only"
    review_mode: ReviewMode = "optional"
    review_sla_hours: int | None = None
    emit_verified_sr_on_accept: bool = False
    # `MOS-EVID-101`'s tenant setting, resolved onto the policy so the worker reads one
    # object. Sourced from `tenants.marginal_policy`, not from the Deployment: chapter 7
    # states it at the tenant and `0010_safety.up.sql` stores it there.
    marginal_policy: Literal["flag", "reject"] = "flag"

    def __post_init__(self) -> None:
        if self.clinical_use_mode not in ("research_only", "clinical"):
            raise ValueError(
                f"clinical_use_mode {self.clinical_use_mode!r} is not one of "
                "research_only, clinical (MOS-SAFE-033)"
            )
        if self.review_mode in RESERVED_REVIEW_MODES:
            raise ReviewModeReserved(self.review_mode)
        if self.review_mode not in REVIEW_MODES:
            raise ValueError(
                f"review_mode {self.review_mode!r} is not one of {list(REVIEW_MODES)} "
                "(MOS-SAFE-061)"
            )
        if self.review_sla_hours is not None and self.review_sla_hours <= 0:
            raise ValueError("review_sla_hours MUST be positive when set")
        if self.marginal_policy not in ("flag", "reject"):
            raise ValueError(
                f"marginal_policy {self.marginal_policy!r} is not flag or reject "
                "(MOS-EVID-101)"
            )
        if self.emit_verified_sr_on_accept and self.clinical_use_mode != "clinical":
            # MOS-SAFE-042/065: a verified SR revision is forbidden in research_only. A
            # Deployment that declares both is not a configuration to be silently ignored
            # at issuance time -- it is a Deployment whose operator believes reviews will
            # produce verified documents and they will not.
            raise ValueError(
                "emit_verified_sr_on_accept is meaningless in research_only mode: "
                "MOS-SAFE-042 forbids a VERIFIED SR there and MOS-SAFE-065 makes the "
                "revision clinical-mode only"
            )

    @property
    def opens_review_on_result(self) -> bool:
        """`MOS-SAFE-059` row 1: a `PENDING` row is created when `review_mode != off`."""
        return self.review_mode != "off"

    @property
    def is_research_only(self) -> bool:
        return self.clinical_use_mode == "research_only"

    def review_due_at(self, *, now: datetime | None = None) -> datetime | None:
        """`MOS-SAFE-058`'s `review_due_at`, "from `Deployment.review_sla_hours`".

        None when no SLA is set, which `MOS-SAFE-061` makes the normal case for
        `optional`: "`mandatory_post_publication` creates the `PENDING` row, sets
        `review_due_at`, and drives the reminder/expiry machinery". An `optional` review
        with no due date never expires, which is the correct behaviour for a review nobody
        is obliged to do.
        """
        if self.review_sla_hours is None:
            return None
        return (now or datetime.now(UTC)) + timedelta(hours=self.review_sla_hours)


class DeploymentPolicySource(Protocol):
    """Resolve the safety policy for the Deployment a job ran under.

    One method, taking the identity a `jobs` row already carries. When chapter 6's
    registry lands this is implemented over the `deployments` table keyed on the job's
    pinned `deployment_id` (`MOS-SAFE-046` reads `environment` and `state` from that row at
    the moment execution starts, and this port is where that read belongs).
    """

    def policy_for(
        self, *, service_id: str, service_version: str, tenant_id: str | None = None
    ) -> DeploymentSafetyPolicy: ...


@dataclass(frozen=True)
class StaticDeploymentPolicySource:
    """The one Deployment this release has, declared once.

    `medos.writer.identity.DEPLOYMENT_ID` is already the platform's single deployment id
    and is reused rather than respelled: two spellings of a deployment id is two answers to
    "which deployment wrote this object", and `MOS-IMG-085`'s invariant is built on the
    first one.
    """

    policy: DeploymentSafetyPolicy

    def policy_for(
        self, *, service_id: str, service_version: str, tenant_id: str | None = None
    ) -> DeploymentSafetyPolicy:
        return self.policy

    def with_marginal_policy(
        self, marginal_policy: str
    ) -> StaticDeploymentPolicySource:
        """A copy carrying the tenant's `MOS-EVID-101` disposition.

        Returns a NEW source rather than mutating, because `DeploymentSafetyPolicy` is
        frozen and because a per-request tenant setting must not be able to reach a
        process-wide object -- a mutated singleton is how tenant A's `reject` policy ends
        up applied to tenant B's next study on the same worker.
        """
        from dataclasses import replace

        return StaticDeploymentPolicySource(
            replace(self.policy, marginal_policy=marginal_policy)  # type: ignore[arg-type]
        )


def default_policy_source(
    env: Mapping[str, str] | None = None,
) -> StaticDeploymentPolicySource:
    """The process default: `research_only`, `review_mode: optional`, no SLA.

    `MEDOS_REVIEW_MODE` and `MEDOS_REVIEW_SLA_HOURS` are readable from the environment
    because `review_mode` is a deployment-configuration knob with no safety consequence in
    either direction -- `MOS-SAFE-061` is explicit that `mandatory_post_publication` "does
    not delay storage or visibility", and `off` merely means no row is opened.

    `clinical_use_mode` is NOT readable from the environment, and that asymmetry is the
    point of this function. `MOS-SAFE-033` forbids an environment-wide override and
    `MOS-SAFE-036` makes promotion an evidence gate with a named approver. An env var
    called `MEDOS_CLINICAL_USE_MODE` would be that gate's bypass, and it would be set in a
    docker-compose file by someone who wanted the RUO banner to go away.
    """
    environ = env if env is not None else os.environ
    from medos.writer.identity import DEPLOYMENT_ID

    review_mode = environ.get("MEDOS_REVIEW_MODE", "optional").strip() or "optional"
    sla_raw = environ.get("MEDOS_REVIEW_SLA_HOURS", "").strip()
    return StaticDeploymentPolicySource(
        DeploymentSafetyPolicy(
            deployment_id=DEPLOYMENT_ID,
            clinical_use_mode="research_only",
            review_mode=review_mode,  # type: ignore[arg-type]
            review_sla_hours=int(sla_raw) if sla_raw else None,
            emit_verified_sr_on_accept=False,
        )
    )
