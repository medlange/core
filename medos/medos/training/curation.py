# SPDX-License-Identifier: Apache-2.0
"""The curation queue: `SamplingPlan`, `HarvestBatch`, `HarvestCandidate`, `CurationDecision`.

`MOS-TRAIN-078`: "The harvest MUST produce `HarvestCandidate` rows, never a
`DatasetVersion` directly. `MOS-EVID-016` forbids a DatasetVersion defined by a live
query; the curation queue is where the materialised list is decided."

`MOS-TRAIN-202` names the same object `CurationBatch` and fixes what it is: "an explicit,
materialised list of candidate `(patient_key, study_instance_uid, series_instance_uid)`
triples with a per-candidate disposition."

THE THREE RULES THIS MODULE EXISTS TO MAKE STRUCTURAL
-----------------------------------------------------
1. A HUMAN DECIDES WHAT ENTERS A COHORT. `MOS-TRAIN-080`: "Every candidate MUST receive an
   explicit `CurationDecision` by a named human before it can enter a `DatasetVersion`.
   Auto-inclusion MUST NOT be implemented." There is therefore no `include_all`, no
   `auto_include`, no `decide(..., decision='include')` that a predicate can call, and
   `auto_exclude` is the only machine-driven writer of a decision -- exclusion only, with
   the predicate id and its digest recorded. A test greps this module for the absent
   verbs, because the cheapest way for auto-inclusion to reappear is as a convenience
   helper nobody read.

2. EVERY EXCLUSION IS KEPT, WITH A REASON. `MOS-TRAIN-203`: "Deleting an excluded row MUST
   be refused." Enforced in the database (migration 0011: no DELETE grant, and
   `curation_decisions_append_only` fires on the cascade), so this module has no delete
   function for a decided candidate and could not usefully have one.

3. THE EXCLUSIONS ARE PART OF THE COHORT'S DESCRIPTION. `MOS-TRAIN-081`: "Exclusion
   reasons MUST be aggregated per cohort and reproduced in any `ValidationReport` citing
   the resulting `DatasetVersion`. A cohort assembled by excluding a third of the
   candidates as `quality_artefact` describes a different population from the one the
   model will meet, and the reader of the report must be able to see that."
   `exclusion_summary` computes that aggregate and `seal_from_batch` writes it into
   `MOS-EVID-022`'s `derivation` object per `MOS-TRAIN-205`.

WHERE THE PIXELS COME FROM, AND WHY THIS MODULE NEVER FETCHES THEM
-------------------------------------------------------------------
`MOS-TRAIN-199`: every component of this pipeline "MUST obtain imaging exclusively through
the `dataset_export` consumer class of Chapter 3", and `MOS-TRAIN-200` puts the
`patient_key` and the de-identified UIDs "already computed, inside the manifest the
exporter produced". So `seal_from_batch` takes a `retrieve` callable -- step 3 of
`MOS-TRAIN-208` -- and this module holds no client, no credential and no route to a PACS.
The seam is an argument rather than an import for the reason `MOS-TRAIN-200` gives: the
absence of a re-identification path should be a property of what this code is HANDED, not
of what it remembers not to call.

Spec: MOS-TRAIN-078, MOS-TRAIN-079, MOS-TRAIN-080, MOS-TRAIN-081, MOS-TRAIN-082,
MOS-TRAIN-083, MOS-TRAIN-084, MOS-TRAIN-088, MOS-TRAIN-111, MOS-TRAIN-199, MOS-TRAIN-202,
MOS-TRAIN-203, MOS-TRAIN-205, MOS-TRAIN-208, MOS-STORE-358, MOS-STORE-359, MOS-UI-131.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg

from medos.db.tenancy import tenant_tx
from medos.evidence import repo as _evrepo
from medos.evidence import stratification as _strat
from medos.evidence.manifest import SeriesRecord
from medos.evidence.profile import acquisition_profile
from medos.evidence.store import ManifestStore
from medos.sdk.canonical import canonical_bytes, sha256_hex
from medos.sdk.errors import CurationRefused, Refusal
from medos.sdk.refusal import SealRefused
from medos.training.policy import assert_harvest_permitted
from medos.training.vocabulary import assert_reason_code

__all__ = [
    "ACQUISITION_PROFILE_FIELDS",
    "BATCH_STATES",
    "CandidateAcquisition",
    "CandidateRow",
    "BatchRow",
    "declare_sampling_plan",
    "open_batch",
    "add_candidate",
    "decide",
    "auto_exclude",
    "candidates",
    "included_candidates",
    "exclusion_summary",
    "derivation_object",
    "assert_geometry_admissible",
    "seal_from_batch",
    "SealFromBatchResult",
]

# `MOS-TRAIN-084`'s thirteen fields, in its order. The database CHECK on
# `harvest_candidates.acquisition_profile` names exactly these keys, so this tuple and
# that CHECK are one fact; `CandidateAcquisition.as_json()` is what keeps them one.
ACQUISITION_PROFILE_FIELDS: tuple[str, ...] = (
    "manufacturer",
    "manufacturer_model_name",
    "convolution_kernel",
    "convolution_kernel_class",
    "slice_thickness_mm",
    "pixel_spacing_mm",
    "kvp",
    "exposure_mas",
    "contrast_phase",
    "iterative_recon_strength",
    "station_key",
    "institution_key",
    "study_year",
)

BATCH_STATES: tuple[str, ...] = ("OPEN", "SAMPLED", "CURATING", "SEALED", "ABANDONED")


def _row(r: Any) -> dict[str, Any]:
    if r is None:
        raise LookupError("expected a row, got none")
    return dict(r) if isinstance(r, Mapping) else r


# =====================================================================================
# The candidate's acquisition profile
# =====================================================================================
@dataclass(frozen=True)
class CandidateAcquisition:
    """`MOS-TRAIN-084`'s `acquisition_profile`, recorded AT CURATION TIME.

    "Every candidate MUST record `acquisition_profile` at curation time, copied from the
    source header without imputation, with a missing value serialised as `null` and never
    as a default (`MOS-EVID-020`) ... Recording it at sealing time from the manifest is too
    late: stratification has to be computable *before* the cohort is chosen."

    Every member therefore defaults to `None` and NONE of them defaults to a value. A
    default slice thickness of 1.0 mm on a header that did not carry one is the defect
    `MOS-EVID-020` names: it is indistinguishable from a measurement in every histogram
    that follows, including the C4 spread the seal will compute.

    This is NOT `medos.evidence.manifest.Acquisition`. The two field sets overlap and are
    not the same: chapter 7's carries `z_coverage_mm`, `image_type`, `patient_age_years`,
    `patient_sex` and `body_part_examined` for the sealed manifest line, and
    `MOS-TRAIN-084`'s carries `exposure_mas`, `iterative_recon_strength`, `station_key`,
    `institution_key` and `study_year` for the pre-seal stratification. Collapsing them
    into one class would put a column on one side or the other that its owning chapter
    does not declare, and chapter 12's two CHECKs would then disagree with the code.
    """

    manufacturer: str | None = None
    manufacturer_model_name: str | None = None
    convolution_kernel: str | None = None
    convolution_kernel_class: str | None = None
    slice_thickness_mm: float | None = None
    pixel_spacing_mm: tuple[float, float] | None = None
    kvp: float | None = None
    exposure_mas: float | None = None
    contrast_phase: str | None = None
    iterative_recon_strength: str | None = None
    station_key: str | None = None
    institution_key: str | None = None
    study_year: int | None = None

    def as_json(self) -> dict[str, Any]:
        """Exactly `ACQUISITION_PROFILE_FIELDS`, every key present, missing values null."""
        return {
            "manufacturer": self.manufacturer,
            "manufacturer_model_name": self.manufacturer_model_name,
            "convolution_kernel": self.convolution_kernel,
            "convolution_kernel_class": self.convolution_kernel_class,
            "slice_thickness_mm": self.slice_thickness_mm,
            "pixel_spacing_mm": (
                list(self.pixel_spacing_mm) if self.pixel_spacing_mm is not None else None
            ),
            "kvp": self.kvp,
            "exposure_mas": self.exposure_mas,
            "contrast_phase": self.contrast_phase,
            "iterative_recon_strength": self.iterative_recon_strength,
            "station_key": self.station_key,
            "institution_key": self.institution_key,
            "study_year": self.study_year,
        }


@dataclass(frozen=True)
class CandidateRow:
    """A `HarvestCandidate` as stored, plus its settled decision when it has one."""

    id: str
    harvest_batch_id: str
    patient_key: str
    study_instance_uid: str
    series_instance_uids: tuple[str, ...]
    instance_uids: tuple[str, ...]
    geometry: dict[str, Any]
    acquisition_profile: dict[str, Any]
    institution_key: str
    deid_policy_version: int
    ran_on_platform: bool
    platform_outcome: str | None
    review_outcome: str | None
    score_band: str | None
    acquisition_bucket: str | None
    sampling_weight: float
    corpus_generation: int
    auto_excluded_predicate_id: str | None
    decision: str | None = None
    reason_code: str | None = None
    note: str | None = None
    decided_by: str | None = None


@dataclass(frozen=True)
class BatchRow:
    """A `HarvestBatch` / `CurationBatch` as stored.

    The last three members arrived with the change that served table 10.2-B rows R22 and
    R23. They carry defaults so that every existing construction site is unchanged, and
    they exist because `medos/schemas/training/harvest-batch-1.0.0.json` REQUIRES `opened_by`,
    `opened_at` and `sealed_at`: a projection that filled them from anywhere but the row
    would be the second source of a fact that can disagree with the first.
    """

    id: str
    capability_id: str
    sampling_plan_id: str
    training_data_policy_id: str
    state: str
    candidate_count: int
    dataset_version_id: str | None
    opened_by: str | None = None
    opened_at: Any | None = None
    sealed_at: Any | None = None


@dataclass(frozen=True)
class SamplingPlanRow:
    """A `SamplingPlan` as stored. `MOS-TRAIN-083`, `MOS-STORE-359`.

    Read-only by construction: the table is sealed outright by a BEFORE UPDATE trigger
    on that requirement's own ground -- "a plan edited after the draw cannot describe the
    draw" -- so there is no setter here and rows R20 and R21 have no PATCH and no DELETE.
    """

    id: str
    capability_id: str
    plan_version: int
    strata: dict[str, Any]
    min_naive_fraction: float
    de_novo_control_fraction: float
    spec_digest: str
    sealed_at: Any
    created_at: Any


# =====================================================================================
# 1. The plan, declared before the draw
# =====================================================================================
def declare_sampling_plan(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str,
    plan_version: int,
    strata: Mapping[str, Any],
    min_naive_fraction: float = 0.200,
    de_novo_control_fraction: float = 0.100,
) -> str:
    """`MOS-TRAIN-083`. Returns the plan id.

    "A `HarvestBatch` MUST declare a versioned `SamplingPlan` before any candidate is
    drawn, and the realised per-case sampling weight MUST be persisted on the candidate.
    Sampling MUST NOT be 'everything the service ran on', and MUST NOT be a random sample
    of it either: the base rate of the cases that carry information is too low."

    The four mandatory dimensions are checked here as well as by the database CHECK,
    because the refusal a CHECK produces names a constraint and not the dimension. Their
    RULES are stored under their keys verbatim -- chapter 17 owns each rule and this
    function does not interpret them, on the same posture `MOS-TRAIN-074` takes toward a
    legal basis.

    `spec_digest` is computed over the canonical form of the plan body, so a plan that
    claims to be `plan_version = 2` of something and is byte-identical to version 1 is
    visible as such. `MOS-STORE-359` seals the row outright: "a plan edited after the draw
    cannot describe the draw".
    """
    required = ("score_band", "review_outcome", "ran_on_platform", "acquisition_bucket")
    missing = [k for k in required if k not in strata]
    if missing:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-083",
                    code="sampling_plan_incomplete",
                    message=(
                        f"the plan must stratify on at least {list(required)}; missing "
                        f"{missing}. Without `ran_on_platform` the corpus can only ever "
                        "describe the inside of the current envelope, and the model can "
                        "never be shown to work outside it."
                    ),
                    observed=sorted(strata),
                    bound=list(required),
                ),
            )
        )
    if min_naive_fraction < 0.0 or min_naive_fraction > 1.0:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-083",
                    code="min_naive_fraction_out_of_range",
                    message="min_naive_fraction is a fraction of patients",
                    observed=min_naive_fraction,
                    bound=[0.0, 1.0],
                ),
            )
        )
    body = {
        "capability_id": capability_id,
        "plan_version": plan_version,
        "strata": dict(strata),
        "min_naive_fraction": round(float(min_naive_fraction), 3),
        "de_novo_control_fraction": round(float(de_novo_control_fraction), 3),
    }
    spec_digest = "sha256:" + sha256_hex(canonical_bytes(body))
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO sampling_plans
              (tenant_id, capability_id, plan_version, strata, min_naive_fraction,
               de_novo_control_fraction, spec_digest)
            VALUES (current_tenant_id(), %s, %s, %s::jsonb, %s, %s, %s)
            RETURNING id
            """,
            (
                capability_id,
                plan_version,
                canonical_bytes(dict(strata)).decode("utf-8"),
                min_naive_fraction,
                de_novo_control_fraction,
                spec_digest,
            ),
        ).fetchone()
    return str(_row(row)["id"])


# =====================================================================================
# 2. The batch
# =====================================================================================
def open_batch(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str,
    sampling_plan_id: str,
    opened_by: str,
) -> BatchRow:
    """Open a `CurationBatch`. `MOS-TRAIN-202`, gated by `MOS-TRAIN-072`.

    The permission check runs HERE, before a single candidate exists, and again per
    candidate in `add_candidate`. Two checks because they answer different questions: this
    one asks whether the tenant may contribute to training at all, and the per-candidate
    one asks whether a particular study is inside the declared scope (`MOS-TRAIN-075`).

    The batch records `training_data_policy_id`, so `MOS-TRAIN-076` can say which
    candidates predate a revocation without reconstructing the answer from timestamps.
    """
    policy = assert_harvest_permitted(conn)
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO harvest_batches
              (tenant_id, capability_id, sampling_plan_id, training_data_policy_id,
               opened_by)
            VALUES (current_tenant_id(), %s, %s, %s, %s)
            RETURNING id, capability_id, sampling_plan_id, training_data_policy_id,
                      state, candidate_count, dataset_version_id
            """,
            (capability_id, sampling_plan_id, policy.id, opened_by),
        ).fetchone()
    r = _row(row)
    return BatchRow(
        id=str(r["id"]),
        capability_id=r["capability_id"],
        sampling_plan_id=str(r["sampling_plan_id"]),
        training_data_policy_id=str(r["training_data_policy_id"]),
        state=r["state"],
        candidate_count=r["candidate_count"],
        dataset_version_id=None,
    )


_BATCH_SELECT = (
    "SELECT id, capability_id, sampling_plan_id, training_data_policy_id, state, "
    "candidate_count, dataset_version_id, opened_by, opened_at, sealed_at "
    "FROM harvest_batches WHERE tenant_id = current_tenant_id()"
)


def _to_batch(r: Mapping[str, Any]) -> BatchRow:
    return BatchRow(
        id=str(r["id"]),
        capability_id=r["capability_id"],
        sampling_plan_id=str(r["sampling_plan_id"]),
        training_data_policy_id=str(r["training_data_policy_id"]),
        state=r["state"],
        candidate_count=r["candidate_count"],
        dataset_version_id=(
            str(r["dataset_version_id"]) if r["dataset_version_id"] else None
        ),
        opened_by=str(r["opened_by"]) if r.get("opened_by") else None,
        opened_at=r.get("opened_at"),
        sealed_at=r.get("sealed_at"),
    )


def get_batch(conn: psycopg.Connection[Any], batch_id: str) -> BatchRow:
    with tenant_tx(conn):
        row = conn.execute(f"{_BATCH_SELECT} AND id = %s", (batch_id,)).fetchone()
    return _to_batch(_row(row))


def find_batch(conn: psycopg.Connection[Any], batch_id: str) -> BatchRow | None:
    """`get_batch` without the `LookupError`. Table 10.2-B row R23.

    A separate function rather than a flag: `get_batch` is called from the seal path,
    where a missing batch is a defect and an exception is the right answer, and from a
    handler, where it is a `404`. One function with a `raise_missing=` parameter would
    let a caller choose to ignore the defect.
    """
    with tenant_tx(conn):
        row = conn.execute(f"{_BATCH_SELECT} AND id = %s", (batch_id,)).fetchone()
    return _to_batch(dict(row)) if row is not None else None


def list_batches(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> list[BatchRow]:
    """Table 10.2-B row R22. `MOS-API-018`'s ordering: `created_at DESC, id DESC`.

    `medos/api/v1/routes.yaml` recorded this as absent: "`get_batch` reads one by id; nothing
    lists them." The reason the row exists is `MOS-UI-138`'s, one entity earlier -- a
    surface whose operator closes the tab mid-curation and cannot find the batch again
    has taught them to avoid the check just as surely as a refusal that discards work.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if capability_id:
        clauses.append("capability_id = %s")
        params.append(capability_id)
    if state:
        clauses.append("state = %s")
        params.append(state)
    where = ("".join(f" AND {c}" for c in clauses)) if clauses else ""
    params.append(limit)
    with tenant_tx(conn):
        rows = conn.execute(
            f"{_BATCH_SELECT}{where} ORDER BY created_at DESC, id DESC LIMIT %s",
            tuple(params),
        ).fetchall()
    return [_to_batch(dict(r)) for r in rows]


# =====================================================================================
# Reading the plan back. Table 10.2-B rows R20 and R21.
#
# `medos/api/v1/routes.yaml` recorded both as absent: "medos/medos/training/curation.py
# declares a plan and reads none back." Two SELECTs under the tenant GUC, and nothing else --
# the row is sealed by trigger (`MOS-STORE-359`), so there is no update path to write and no
# cache to invalidate.
# =====================================================================================
_PLAN_SELECT = (
    "SELECT id, capability_id, plan_version, strata, min_naive_fraction, "
    "de_novo_control_fraction, spec_digest, sealed_at, created_at "
    "FROM sampling_plans WHERE tenant_id = current_tenant_id()"
)


def _to_plan(r: Mapping[str, Any]) -> SamplingPlanRow:
    return SamplingPlanRow(
        id=str(r["id"]),
        capability_id=r["capability_id"],
        plan_version=int(r["plan_version"]),
        strata=dict(r["strata"]),
        min_naive_fraction=float(r["min_naive_fraction"]),
        de_novo_control_fraction=float(r["de_novo_control_fraction"]),
        spec_digest=r["spec_digest"],
        sealed_at=r["sealed_at"],
        created_at=r["created_at"],
    )


def get_sampling_plan(
    conn: psycopg.Connection[Any], plan_id: str
) -> SamplingPlanRow | None:
    with tenant_tx(conn):
        row = conn.execute(f"{_PLAN_SELECT} AND id = %s", (plan_id,)).fetchone()
    return _to_plan(dict(row)) if row is not None else None


def list_sampling_plans(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str | None = None,
    limit: int = 50,
) -> list[SamplingPlanRow]:
    where = " AND capability_id = %s" if capability_id else ""
    params: tuple[Any, ...] = (
        (capability_id, limit) if capability_id else (limit,)
    )
    with tenant_tx(conn):
        rows = conn.execute(
            f"{_PLAN_SELECT}{where} ORDER BY created_at DESC, id DESC LIMIT %s", params
        ).fetchall()
    return [_to_plan(dict(r)) for r in rows]


def set_batch_state(conn: psycopg.Connection[Any], *, batch_id: str, state: str) -> None:
    """Advance the batch. `SEALED` is written only by `seal_from_batch`.

    `SEALED` is refused here rather than permitted-and-ignored: the CHECK in migration
    0011 requires `dataset_version_id` alongside it, and a caller who set the state by
    hand would either hit a 23514 or -- worse, if they also supplied an id -- record a
    batch as sealed against a version that was never sealed from it.
    """
    if state not in BATCH_STATES:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-STORE-358",
                    code="unknown_batch_state",
                    message=f"state must be one of {BATCH_STATES}",
                    observed=state,
                    bound=list(BATCH_STATES),
                ),
            )
        )
    if state == "SEALED":
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-082",
                    code="seal_is_not_a_state_write",
                    message="SEALED is written by seal_from_batch, together with the "
                            "dataset_version_id it sealed. MOS-TRAIN-082: the pipeline "
                            "MUST call chapter 7's seal and MUST NOT write the manifest "
                            "itself.",
                    observed=state,
                ),
            )
        )
    with tenant_tx(conn):
        conn.execute(
            "UPDATE harvest_batches SET state = %s WHERE tenant_id = current_tenant_id() "
            "AND id = %s",
            (state, batch_id),
        )


# =====================================================================================
# 3. The candidates
# =====================================================================================
def add_candidate(
    conn: psycopg.Connection[Any],
    *,
    batch_id: str,
    patient_key: str,
    study_instance_uid: str,
    series_instance_uids: Sequence[str],
    instance_uids: Sequence[str],
    geometry: Mapping[str, Any],
    acquisition: CandidateAcquisition,
    institution_key: str,
    deid_policy_version: int,
    sampling_weight: float,
    ran_on_platform: bool,
    platform_outcome: str | None = None,
    review_outcome: str | None = None,
    score_band: str | None = None,
    acquisition_bucket: str | None = None,
    corpus_generation: int = 0,
    study: Mapping[str, Any] | None = None,
) -> str:
    """Draw one candidate into the queue. `MOS-TRAIN-079`, `MOS-TRAIN-083`, `MOS-TRAIN-084`.

    `study` carries the scope-test fields of `MOS-TRAIN-075` -- `modality`,
    `body_part_examined`, `capability_id`, `study_date` -- and is REQUIRED in practice
    even though it is typed optional: passing none skips the per-study scope test, and
    `MOS-TRAIN-075` refuses "any study outside `scope`". It is typed optional only so that
    a caller reconstructing a historical batch is not forced to invent the fields; the
    refusal a missing scope test would have raised then surfaces at the seal instead,
    where it is more expensive. Callers in this codebase always pass it.

    NO PHI. `MOS-TRAIN-079` forbids `PatientName`, `PatientBirthDate`, `AccessionNumber`,
    institution free text and every source-space UID on this row, and there is no
    parameter here for any of them. `institution_key` is `MOS-TRAIN-089`'s HMAC --
    `medos.evidence.digest.institution_key` computes it -- and this function does not
    check that, because it cannot distinguish an HMAC from a name that happens to look
    like one. What it CAN do is refuse a value that is obviously a name, and it does not:
    a half-check here would read as a guarantee. The guarantee is the caller's, the CI
    grep's (`MOS-STORE-272`) and the schema review's.
    """
    if sampling_weight <= 0:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-083",
                    code="missing_sampling_weight",
                    message="the realised per-case sampling weight MUST be persisted on "
                            "the candidate; a weight of zero is a case that was not drawn",
                    observed=sampling_weight,
                ),
            )
        )
    if not 0 <= corpus_generation <= 2:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-087",
                    code="corpus_generation_out_of_range",
                    message="corpus_generation is 0, 1 or 2 on a CANDIDATE -- a candidate "
                            "may be generation 2 and be excluded FOR it; a sealed case "
                            "may not be generation 2 at all",
                    observed=corpus_generation,
                    bound=[0, 2],
                ),
            )
        )
    assert_harvest_permitted(conn, study=study)
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO harvest_candidates
              (tenant_id, harvest_batch_id, patient_key, study_instance_uid,
               series_instance_uids, instance_uids, geometry, acquisition_profile,
               institution_key, deid_policy_version, ran_on_platform, platform_outcome,
               review_outcome, score_band, acquisition_bucket, sampling_weight,
               corpus_generation)
            VALUES (current_tenant_id(), %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                batch_id,
                patient_key,
                study_instance_uid,
                list(series_instance_uids),
                list(instance_uids),
                canonical_bytes(dict(geometry)).decode("utf-8"),
                canonical_bytes(acquisition.as_json()).decode("utf-8"),
                institution_key,
                deid_policy_version,
                ran_on_platform,
                platform_outcome,
                review_outcome,
                score_band,
                acquisition_bucket,
                sampling_weight,
                corpus_generation,
            ),
        ).fetchone()
        conn.execute(
            "UPDATE harvest_batches SET candidate_count = candidate_count + 1 "
            "WHERE tenant_id = current_tenant_id() AND id = %s",
            (batch_id,),
        )
    return str(_row(row)["id"])


def decide(
    conn: psycopg.Connection[Any],
    *,
    candidate_id: str,
    decision: str,
    decided_by: str,
    review_seconds: int,
    reason_code: str | None = None,
    note: str | None = None,
) -> str:
    """A NAMED HUMAN decides. `MOS-TRAIN-080`.

    `decided_by` and `review_seconds` are both required and neither has a default.
    `review_seconds` is "time on task, for the queue's own quality signal", and a default
    of zero would make a curator who spent four seconds on two hundred cases
    indistinguishable from one who spent forty minutes -- which is the signal the field
    exists to carry.

    THERE IS NO AUTO-INCLUSION. This function is the only writer of `decision='include'`
    and it demands a principal id. `auto_exclude` below is the machine path and it can
    write only `exclude`.
    """
    assert_reason_code(decision, reason_code)
    if review_seconds < 0:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-080",
                    code="negative_review_seconds",
                    message="review_seconds is time on task",
                    observed=review_seconds,
                ),
            )
        )
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO curation_decisions
              (tenant_id, candidate_id, decision, reason_code, note, review_seconds,
               decided_by)
            VALUES (current_tenant_id(), %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (candidate_id, decision, reason_code, note, review_seconds, decided_by),
        ).fetchone()
    return str(_row(row)["id"])


def auto_exclude(
    conn: psycopg.Connection[Any],
    *,
    candidate_id: str,
    reason_code: str,
    predicate_id: str,
    predicate_source: bytes,
    decided_by: str,
    note: str | None = None,
) -> str:
    """Mechanical exclusion. `MOS-TRAIN-080`, and ONLY exclusion.

    "Auto-inclusion MUST NOT be implemented. Auto-*exclusion* on a mechanical predicate --
    gantry tilt rejected, non-uniform spacing, missing series, duplicate
    `series_pixel_digest` -- IS permitted, MUST record the predicate id and its digest,
    and MUST be visible in the queue as an exclusion rather than an absence."

    So this writes BOTH halves: the predicate columns on the candidate, and an ordinary
    `curation_decisions` row. The decision row is what makes it "visible in the queue as
    an exclusion"; a candidate annotated only on its own columns would be an absence with
    a footnote.

    `decided_by` is still required. `MOS-TRAIN-080` puts an auto-exclusion inside the same
    `CurationDecision` record as a human one, and that record's `decided_by` is `NOT NULL`;
    the principal here is the service account that ran the predicate, and naming it is how
    a queue reviewer can tell the two apart at all.

    `predicate_source` is the predicate's serialised form, digested per `MOS-TRAIN-205`, so
    the exclusion is reproducible. A predicate whose source is not available is not a
    mechanical predicate -- it is a judgement, and `MOS-TRAIN-205` says a human judgement
    "MUST NOT be represented as if it had" a predicate.
    """
    if not predicate_source:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-205",
                    code="predicate_without_source",
                    message="an auto-exclusion records the digest of the predicate's "
                            "serialised form; a human judgement has no predicate and "
                            "MUST NOT be represented as if it had one",
                    observed=None,
                ),
            )
        )
    digest = "sha256:" + sha256_hex(predicate_source)
    with tenant_tx(conn):
        conn.execute(
            """
            UPDATE harvest_candidates
               SET auto_excluded_predicate_id = %s, auto_excluded_predicate_digest = %s
             WHERE tenant_id = current_tenant_id() AND id = %s
            """,
            (predicate_id, digest, candidate_id),
        )
        return decide(
            conn,
            candidate_id=candidate_id,
            decision="exclude",
            decided_by=decided_by,
            review_seconds=0,
            reason_code=reason_code,
            note=note or f"auto-excluded by predicate {predicate_id}",
        )


# `series_instance_uids` and `instance_uids` are `dicom_uid[]`, and `dicom_uid` is a DOMAIN
# (0002 section 90). psycopg has no loader registered for the domain's ARRAY type, so it
# hands the column back as the raw Postgres array LITERAL -- a `str`, not a list. Silently:
# `tuple("{1.2.840...}")` is a tuple of thirty-two characters, `len(...)` is a character
# count, and `series_instance_uids[0]` is `"{"`. Both of those are load-bearing here --
# `MOS-TRAIN-205`'s `removed_series` is a series COUNT, and `MOS-TRAIN-111` requires the
# refusal to NAME the offending `series_instance_uid` -- so the cast is in the query rather
# than in a defensive coercion at the call site. `::text[]` has a well-known OID and comes
# back as a list; the domain's own CHECK still governs what may be stored.
_CANDIDATE_SELECT = """
    SELECT c.id, c.harvest_batch_id, c.patient_key, c.study_instance_uid,
           c.series_instance_uids::text[] AS series_instance_uids,
           c.instance_uids::text[]        AS instance_uids,
           c.geometry, c.acquisition_profile,
           c.institution_key, c.deid_policy_version, c.ran_on_platform,
           c.platform_outcome, c.review_outcome, c.score_band, c.acquisition_bucket,
           c.sampling_weight, c.corpus_generation, c.auto_excluded_predicate_id,
           d.decision, d.reason_code, d.note, d.decided_by
      FROM harvest_candidates c
      LEFT JOIN curation_decisions d
        ON d.tenant_id = c.tenant_id AND d.candidate_id = c.id AND d.decision <> 'defer'
     WHERE c.tenant_id = current_tenant_id() AND c.harvest_batch_id = %s
     ORDER BY c.patient_key, c.study_instance_uid
"""


def _to_candidate(r: Mapping[str, Any]) -> CandidateRow:
    return CandidateRow(
        id=str(r["id"]),
        harvest_batch_id=str(r["harvest_batch_id"]),
        patient_key=r["patient_key"],
        study_instance_uid=r["study_instance_uid"],
        series_instance_uids=tuple(r["series_instance_uids"]),
        instance_uids=tuple(r["instance_uids"]),
        geometry=dict(r["geometry"]),
        acquisition_profile=dict(r["acquisition_profile"]),
        institution_key=r["institution_key"],
        deid_policy_version=int(r["deid_policy_version"]),
        ran_on_platform=bool(r["ran_on_platform"]),
        platform_outcome=r["platform_outcome"],
        review_outcome=r["review_outcome"],
        score_band=r["score_band"],
        acquisition_bucket=r["acquisition_bucket"],
        sampling_weight=float(r["sampling_weight"]),
        corpus_generation=int(r["corpus_generation"]),
        auto_excluded_predicate_id=r["auto_excluded_predicate_id"],
        decision=r["decision"],
        reason_code=r["reason_code"],
        note=r["note"],
        decided_by=str(r["decided_by"]) if r["decided_by"] else None,
    )


def candidates(conn: psycopg.Connection[Any], batch_id: str) -> list[CandidateRow]:
    """Every candidate in the batch with its settled decision, included or not.

    The queue, in the sense `MOS-TRAIN-080` means: an excluded candidate is HERE, with its
    reason, and not absent. A caller rendering the operator surface reads this; a caller
    building a cohort reads `included_candidates`.
    """
    with tenant_tx(conn):
        rows = conn.execute(_CANDIDATE_SELECT, (batch_id,)).fetchall()
    return [_to_candidate(_row(r)) for r in rows]


def included_candidates(
    conn: psycopg.Connection[Any], batch_id: str
) -> list[CandidateRow]:
    """Step 1 of `MOS-TRAIN-208`: "take the `disposition = 'include'` rows"."""
    return [c for c in candidates(conn, batch_id) if c.decision == "include"]


def exclusion_summary(conn: psycopg.Connection[Any], batch_id: str) -> dict[str, Any]:
    """`MOS-TRAIN-081`'s per-cohort aggregate, in the form a report reproduces.

    "A cohort assembled by excluding a third of the candidates as `quality_artefact`
    describes a different population from the one the model will meet, and the reader of
    the report must be able to see that." So the aggregate carries the DENOMINATOR --
    `drawn` -- and not only the counts: eleven `quality_artefact` exclusions mean one thing
    out of two thousand candidates and another out of thirty.
    """
    rows = candidates(conn, batch_id)
    by_reason: dict[str, int] = {}
    excluded_patients: set[str] = set()
    auto = 0
    for c in rows:
        if c.decision != "exclude":
            continue
        code = c.reason_code or "unspecified"
        by_reason[code] = by_reason.get(code, 0) + 1
        excluded_patients.add(c.patient_key)
        if c.auto_excluded_predicate_id:
            auto += 1
    settled = sum(1 for c in rows if c.decision in ("include", "exclude"))
    included = sum(1 for c in rows if c.decision == "include")
    excluded = sum(1 for c in rows if c.decision == "exclude")
    return {
        "drawn": len(rows),
        "settled": settled,
        "unsettled": len(rows) - settled,
        "included": included,
        "excluded": excluded,
        "excluded_fraction": (round(excluded / len(rows), 6) if rows else 0.0),
        "by_reason_code": dict(sorted(by_reason.items())),
        "auto_excluded": auto,
        "excluded_patient_keys": sorted(excluded_patients),
        "requirement": "MOS-TRAIN-081",
    }


def derivation_object(conn: psycopg.Connection[Any], batch_id: str) -> dict[str, Any]:
    """`MOS-TRAIN-205`'s `derivation`, for `MOS-EVID-022` on the sealed version.

    "When exclusion was decided by a machine-checkable predicate, `predicate_digest` MUST
    be the digest of that predicate's serialised form. When exclusion was decided case by
    case by a human, `derivation` MUST carry
    `{"op":"exclude","reason":"reader_judgement","removed_series":N}` and MUST enumerate
    the excluded `patient_key`s; a human judgement has no predicate and MUST NOT be
    represented as if it had one."

    Both halves can be true of one batch, so the object carries a list of operations --
    one per predicate, plus at most one `reader_judgement` entry -- rather than choosing a
    shape. The `reader_judgement` entry never gains a `predicate_digest`, which is the
    sentence's actual content.
    """
    rows = candidates(conn, batch_id)
    per_predicate: dict[str, dict[str, Any]] = {}
    human_patients: set[str] = set()
    human_series = 0
    with tenant_tx(conn):
        digests = {
            str(_row(r)["id"]): _row(r)["auto_excluded_predicate_digest"]
            for r in conn.execute(
                "SELECT id, auto_excluded_predicate_digest FROM harvest_candidates "
                "WHERE tenant_id = current_tenant_id() AND harvest_batch_id = %s",
                (batch_id,),
            ).fetchall()
        }
    for c in rows:
        if c.decision != "exclude":
            continue
        if c.auto_excluded_predicate_id:
            entry = per_predicate.setdefault(
                c.auto_excluded_predicate_id,
                {
                    "op": "exclude",
                    "reason": "predicate",
                    "predicate_id": c.auto_excluded_predicate_id,
                    "predicate_digest": digests.get(c.id),
                    "removed_series": 0,
                    "reason_codes": {},
                },
            )
            entry["removed_series"] += len(c.series_instance_uids)
            code = c.reason_code or "unspecified"
            entry["reason_codes"][code] = entry["reason_codes"].get(code, 0) + 1
        else:
            human_patients.add(c.patient_key)
            human_series += len(c.series_instance_uids)
    ops: list[dict[str, Any]] = [per_predicate[k] for k in sorted(per_predicate)]
    if human_series:
        ops.append(
            {
                "op": "exclude",
                "reason": "reader_judgement",
                "removed_series": human_series,
                "excluded_patient_keys": sorted(human_patients),
            }
        )
    return {
        "source": "curation_batch",
        "harvest_batch_id": batch_id,
        "operations": ops,
        "exclusion_summary": exclusion_summary(conn, batch_id),
    }


# =====================================================================================
# 4. The geometry gate of MOS-TRAIN-111
# =====================================================================================
def assert_geometry_admissible(spec: Any, candidate: CandidateRow) -> None:
    """`MOS-TRAIN-111`. Raises `CurationRefused` naming the offending series.

    "A seal MUST be refused when a retrieved series is rejected by the geometry contract
    of Chapter 4 under the `PreprocessingSpec` the capability will serve -- gantry tilt
    beyond the declared `canonical_geometry.gantry_tilt.max_deg`, or non-uniform spacing
    under `canonical_geometry.non_uniform_spacing: reject`. The refusal MUST name the
    offending `series_instance_uid` and MUST be resolvable only by excluding that series
    with a recorded `reason_code`. It MUST NOT be resolvable by editing the spec."

    That last sentence is why the remedy in the message says `exclude` and never
    "widen the spec": widening it "changes what is served to every patient, and it is the
    cheapest-looking fix on the screen at that moment".

    Reads the `MOS-IMG-036` descriptor already on the candidate (`tilt_deg`,
    `spacing_class`, `max_jitter_mm`) -- the one `medos.core.geometry.CanonicalVolume`
    emits -- so no pixels are touched and the check runs before retrieval as well as after.
    """
    geo = candidate.geometry
    cg = spec.canonical_geometry
    refusals: list[Refusal] = []
    tilt = geo.get("tilt_deg")
    if tilt is not None and float(tilt) > float(cg.gantry_tilt_max_deg):
        refusals.append(
            Refusal(
                check_id="MOS-TRAIN-111",
                code="gantry_tilt_beyond_declared_max",
                message=(
                    f"series {candidate.series_instance_uids[0]} has tilt_deg={tilt} "
                    f"against canonical_geometry.gantry_tilt.max_deg="
                    f"{cg.gantry_tilt_max_deg}. Exclude the series with a recorded "
                    "reason_code (geometry_unsupported). Do NOT widen the spec: that "
                    "changes what is served to every patient."
                ),
                observed=tilt,
                bound=cg.gantry_tilt_max_deg,
                detail={
                    "series_instance_uid": candidate.series_instance_uids[0],
                    "remedy": "exclude with reason_code=geometry_unsupported",
                    "refused_remedy": "widening canonical_geometry.gantry_tilt.max_deg",
                },
            )
        )
    spacing_class = geo.get("spacing_class")
    if cg.non_uniform_spacing == "reject" and spacing_class not in (None, "uniform"):
        refusals.append(
            Refusal(
                check_id="MOS-TRAIN-111",
                code="non_uniform_spacing_rejected",
                message=(
                    f"series {candidate.series_instance_uids[0]} has "
                    f"spacing_class={spacing_class!r} under "
                    "canonical_geometry.non_uniform_spacing: reject. Exclude the series "
                    "with a recorded reason_code (geometry_unsupported). Do NOT change "
                    "the spec to resample_to_uniform to admit the training data."
                ),
                observed=spacing_class,
                bound="uniform",
                detail={
                    "series_instance_uid": candidate.series_instance_uids[0],
                    "remedy": "exclude with reason_code=geometry_unsupported",
                    "refused_remedy": "canonical_geometry.non_uniform_spacing = "
                                      "resample_to_uniform",
                },
            )
        )
    if refusals:
        raise CurationRefused(tuple(refusals))


# =====================================================================================
# 5. The seal
# =====================================================================================
@dataclass(frozen=True)
class SealFromBatchResult:
    """What `seal_from_batch` returns: the chapter-7 seal plus this chapter's record."""

    dataset_version_id: str
    dataset_version_public_id: str
    manifest_digest: str
    reused: bool
    stratification_report_id: str
    verdict: str
    exclusion_summary: dict[str, Any] = field(default_factory=dict)


def _verdict(report: _strat.StratificationReport) -> str:
    outcomes = {c.outcome for c in report.checks}
    if "fail" in outcomes:
        return "fail"
    if "warn" in outcomes:
        return "warn"
    return "pass"


def _record_stratification(
    conn: psycopg.Connection[Any],
    *,
    batch_id: str,
    report: _strat.StratificationReport,
    dataset_version_id: str | None,
) -> str:
    """Write the `CorpusStratificationReport`. `MOS-TRAIN-088`: "on the batch"."""
    body = report.as_dict()
    checks = {c.id: c.as_dict() for c in report.checks}
    body["checks"] = checks
    with tenant_tx(conn):
        row = conn.execute(
            """
            INSERT INTO corpus_stratification_reports
              (tenant_id, harvest_batch_id, dataset_version_id, verdict, checks)
            VALUES (current_tenant_id(), %s, %s, %s, %s::jsonb)
            RETURNING id
            """,
            (
                batch_id,
                dataset_version_id,
                _verdict(report),
                canonical_bytes(
                    {
                        "evidence_kind": report.evidence_kind,
                        "waivers": [dict(w) for w in report.waivers],
                        **checks,
                    }
                ).decode("utf-8"),
            ),
        ).fetchone()
    return str(_row(row)["id"])


def record_stratification_report(
    conn: psycopg.Connection[Any],
    *,
    batch_id: str,
    report: _strat.StratificationReport,
    dataset_version_id: str | None,
) -> str:
    """`_record_stratification`, for a caller outside this module. Returns the row id.

    It exists because `MOS-UI-131` moves the check battery BEFORE the point of no return
    and `MOS-TRAIN-088` still requires the full C1-C7 result recorded when it blocks. The
    seal driver (`medos/medos/training/seal.py`) therefore refuses without ever reaching
    `seal_from_batch`, and the record of the refusal has to be written by something --
    "a fail that blocked without leaving a record would make the most informative outcome
    the only one with no evidence". A public name rather than a reach into the private
    one, so the obligation is visible from outside this file.
    """
    return _record_stratification(
        conn, batch_id=batch_id, report=report, dataset_version_id=dataset_version_id
    )


def seal_from_batch(
    conn: psycopg.Connection[Any],
    *,
    batch_id: str,
    dataset_id: str,
    retrieve: Callable[[CandidateRow], Sequence[SeriesRecord]],
    store: ManifestStore,
    bucket: str,
    sealed_by: str,
    deidentification_status: str,
    spec: Any | None = None,
    evidence_kind: str = "vendor_evidence",
    envelope: Mapping[str, Any] | None = None,
    stratification_waivers: Sequence[Mapping[str, Any]] = (),
    deid_policy_id: str | None = None,
    uid_mapping_table_id: str | None = None,
    source_description: str = "",
) -> SealFromBatchResult:
    """`MOS-TRAIN-208` steps 1-8, with `MOS-TRAIN-088` recorded on the batch either way.

    THE ORDER IS THE POINT, and it is `MOS-UI-131`'s rather than the minimum the platform
    requires. The console "MUST run the complete check battery BEFORE it creates anything,
    and MUST perform the seal only if every blocking check passes", because "a
    `DatasetVersion` that seals and then fails its split freeze is immutable
    (`MOS-EVID-013`), undeletable ... and useless, and the operator this surface exists for
    cannot clean it up". The same reasoning applies one step earlier, so:

      1. freeze the batch and take the `include` rows                    (step 1)
      2. refuse while any candidate is unsettled                         (below)
      3. retrieve through `dataset_export`                               (step 3)
      4. run `MOS-TRAIN-111`'s geometry gate                             (MOS-TRAIN-111)
      5. compute `acquisition_profile` and C1-C7                         (steps 7, 088)
      6. RECORD the stratification report on the batch, pass or fail     (MOS-TRAIN-088)
      7. raise if it blocks -- nothing has been created                  (MOS-TRAIN-088)
      8. call chapter 7's seal                                           (steps 5-8, 082)
      9. record the verdict again against the version it sealed

    Step 6 before step 7 is the requirement's actual content: `MOS-TRAIN-088` says the
    check MUST be run and its FULL result MUST be recorded, and a `fail` MUST block. A
    `fail` that blocked without leaving a record would make the most informative outcome
    the only one with no evidence.

    WHY AN UNSETTLED CANDIDATE BLOCKS THE SEAL. `MOS-TRAIN-081` requires the exclusion
    reasons aggregated per cohort and reproduced in the report, and an aggregate whose
    denominator includes candidates nobody decided about is not the fraction the reader
    thinks it is. A candidate that genuinely should not be decided should be excluded
    (`out_of_scope`) or removed from the batch before any decision exists -- an undecided
    candidate is still deletable, an excluded one never is (`MOS-TRAIN-203`).

    `spec` is a `medos.sdk.spec.PreprocessingSpec` or None. None skips step 4,
    which is correct for a cohort being sealed for `acceptance` rather than for training
    against a particular served spec, and is stated rather than defaulted so it cannot be the
    reason a tilted series entered a training corpus.
    """
    batch = get_batch(conn, batch_id)
    if batch.state == "SEALED":
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-209",
                    code="batch_already_sealed",
                    message=f"batch {batch_id} is already sealed against "
                            f"{batch.dataset_version_id}; sealing is idempotent on "
                            "CONTENT, not a second identity for the same batch",
                    observed=batch.dataset_version_id,
                ),
            )
        )
    rows = candidates(conn, batch_id)
    unsettled = [c.id for c in rows if c.decision not in ("include", "exclude")]
    if unsettled:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-081",
                    code="batch_not_settled",
                    message=(
                        f"{len(unsettled)} of {len(rows)} candidates have no settled "
                        "decision. MOS-TRAIN-081 reproduces the exclusion aggregate in "
                        "every ValidationReport citing this cohort, and its denominator "
                        "is the drawn set: decide them, or remove them from the batch "
                        "before any decision exists."
                    ),
                    observed=len(unsettled),
                    bound=0,
                    detail={"candidate_ids": unsettled[:20]},
                ),
            )
        )
    included = [c for c in rows if c.decision == "include"]
    if not included:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-EVID-016",
                    code="empty_cohort",
                    message="the batch has no included candidates; a DatasetVersion is "
                            "an explicit, materialised list of instances",
                    observed=0,
                ),
            )
        )

    if spec is not None:
        for c in included:
            assert_geometry_admissible(spec, c)

    records: list[SeriesRecord] = []
    for c in included:
        records.extend(retrieve(c))
    if not records:
        raise CurationRefused(
            (
                Refusal(
                    check_id="MOS-TRAIN-208",
                    code="retrieval_returned_nothing",
                    message="step 3 retrieved no series for an included candidate set; "
                            "the dataset_export boundary returned an empty manifest",
                    observed=0,
                ),
            )
        )

    profile = acquisition_profile(records)
    report = _strat.stratification_report(
        profile,
        evidence_kind=evidence_kind,
        envelope=envelope,
        waivers=stratification_waivers,
    )
    blocking = _strat.blocking_refusals(report)
    if blocking:
        report_id = _record_stratification(
            conn, batch_id=batch_id, report=report, dataset_version_id=None
        )
        raise SealRefused(
            tuple(blocking)
            + (
                Refusal(
                    check_id="MOS-TRAIN-088",
                    code="corpus_stratification_recorded",
                    message=f"the full C1-C7 result is recorded as "
                            f"corpus_stratification_reports.{report_id} on batch "
                            f"{batch_id}; a fail blocks sealing and the record of the "
                            "fail is not optional",
                    detail={"corpus_stratification_report_id": report_id},
                ),
            )
        )

    sealed = _evrepo.seal_dataset_version(
        conn,
        dataset_id=dataset_id,
        records=records,
        store=store,
        bucket=bucket,
        sealed_by=sealed_by,
        deidentification_status=deidentification_status,
        deid_policy_id=deid_policy_id,
        uid_mapping_table_id=uid_mapping_table_id,
        source_description=source_description,
        derivation=derivation_object(conn, batch_id),
        evidence_kind=evidence_kind,
        envelope=envelope,
        stratification_waivers=stratification_waivers,
    )
    report_id = _record_stratification(
        conn, batch_id=batch_id, report=report, dataset_version_id=sealed.id
    )
    with tenant_tx(conn):
        conn.execute(
            """
            UPDATE harvest_batches
               SET state = 'SEALED', sealed_at = now(), dataset_version_id = %s
             WHERE tenant_id = current_tenant_id() AND id = %s
            """,
            (sealed.id, batch_id),
        )
    return SealFromBatchResult(
        dataset_version_id=sealed.id,
        dataset_version_public_id=sealed.public_id,
        manifest_digest=sealed.manifest_digest,
        reused=sealed.reused,
        stratification_report_id=report_id,
        verdict=_verdict(report),
        exclusion_summary=exclusion_summary(conn, batch_id),
    )
