# SPDX-License-Identifier: Apache-2.0
"""`ConversionRun`, E1/E2/E3 and the tolerances. 17.9.

THE TRAP, IN 17.9.1'S OWN WORDS
-------------------------------
"What was evaluated in 17.8 is a PyTorch module running fp32 under a Python inferer inside
a training container. What is served is a serialised graph running under a Triton backend
on a specific GPU architecture, possibly at reduced precision, with a different kernel
selection, a different reduction order and a different memory layout. These are different
numerical artifacts that happen to share a weights file."

That is why a conversion is a **new** `ModelVersion` requiring its **own** `EvaluationRun`
(`MOS-REG-102`, `MOS-REG-103`, `MOS-EVID-064`, `MOS-SVC-017`, `MOS-OPS-071`) -- the artifact
you validated is not the artifact you serve.

WHAT E1/E2/E3 ARE AND WHAT THEY ARE NOT
----------------------------------------
`MOS-TRAIN-161` fixes three quantities at three points in the pipeline, and the reason each
is measured where it is:

    E1  max_abs_probability_diff  post-activation probability map, model space
        -- bounded in [0,1], so a tolerance is interpretable across models; the raw logit
           scale is model-specific and a fixed logit tolerance means different things for
           different heads.
    E2  post_threshold_dice       binarised label map at the operating point, model space
        -- the quantity the clinical output is actually derived from; a probability
           difference below the threshold everywhere is harmless and a smaller one
           straddling it is not.
    E3  volume_rel_diff           the reported measurement in SOURCE geometry
        -- the number that reaches the SR. "E1 and E2 can both pass while the inverse
           transform and the source-grid resampling amplify a boundary-voxel disagreement
           into a reportable difference."

`max_abs_logit_diff` is recorded because `MOS-REG-104` names it and is explicitly NOT the
gating quantity.

`MOS-TRAIN-165` is the sentence this module exists to keep true: "An E1/E2/E3 pass MUST NOT
be reported, summarised, or displayed as evidence of clinical equivalence. It is evidence
that the conversion did not break the graph." So `EQUIVALENCE_DISCLAIMER` is the verbatim
statement the dossier must render, and nothing here produces a number that looks like a
metric.

THE ARITHMETIC BEHIND "EVERY CASE", NOT THE MEAN
-------------------------------------------------
`MOS-TRAIN-163`: "A mean `post_threshold_dice` of 0.999 over 20 cases is consistent with
nineteen cases at 1.000 and one at 0.980, and on a 40 mL effusion a Dice of 0.980 is a
symmetric difference of roughly 1.6 mL -- small in isolation, and exactly the size of
difference that moves a single case across a reporting threshold while the aggregate
reports that nothing happened."

So `evaluate_equivalence()` takes per-case rows and returns the worst case per quantity
together with the offending `case_key`. There is no mean anywhere in this module, and there
is no argument that relaxes a bound: `MOS-TRAIN-164`'s remedy "is a different conversion
... never a relaxed tolerance on that version", because "a tolerance relaxed to admit one
artifact silently relaxes it for every future artifact of that family."

Spec: MOS-TRAIN-153 to MOS-TRAIN-172, MOS-REG-102 to MOS-REG-105, MOS-EVID-064,
MOS-OPS-070, MOS-OPS-071, MOS-OPS-078.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from medos.core.statements import EQUIVALENCE_DISCLAIMER as _EQUIVALENCE_DISCLAIMER
from medos.db.tenancy import current_tenant, tenant_tx
from medos.sdk.canonical import canonical_bytes, new_ulid, sha256_hex
from medos.sdk.errors import ConversionRefused, Refusal, TrainingError

__all__ = [
    "EQUIVALENCE_DISCLAIMER",
    "MIN_EQUIVALENCE_PATIENTS",
    "PRECISION_CLASSES",
    "TARGET_FORMATS",
    "TOLERANCES",
    "CaseEquivalence",
    "Tolerances",
    "create",
    "equivalence_cohort_digest",
    "evaluate_equivalence",
    "fail",
    "get",
    "start",
    "succeed",
    "tolerances_for",
]

TARGET_FORMATS: Final[tuple[str, ...]] = ("torchscript", "onnx", "tensorrt_plan")
PRECISION_CLASSES: Final[tuple[str, ...]] = ("fp32", "tf32", "fp16", "int8")

#: `MOS-TRAIN-159`: "at least 20 distinct `patient_key`s drawn from the `tune` partition".
MIN_EQUIVALENCE_PATIENTS: Final[int] = 20

#: `MOS-TRAIN-165`, verbatim, and DEFINED IN `medos/medos/core/statements.py` rather than here.
#: The dossier renders the same string, and once the training plane is a separate
#: deployable `medos/medos/promotion/` can no longer import this module to get it -- while the
#: reverse edge is forbidden outright by `MOS-TRAIN-189`. One definition, in the package
#: both sides already depend on. Re-exported under its historical name because chapter
#: 17's acceptance checks and `tests/integration/test_training_run.py` read
#: `conversion.EQUIVALENCE_DISCLAIMER`.
EQUIVALENCE_DISCLAIMER: Final[str] = _EQUIVALENCE_DISCLAIMER


@dataclass(frozen=True)
class Tolerances:
    """One row of `MOS-TRAIN-162`'s table. `e1`/`e3` are ceilings, `e2` is a floor."""

    e1_max: float
    e2_min: float
    e3_max: float

    def as_dict(self) -> dict[str, float]:
        return {
            "E1_max_abs_probability_diff": self.e1_max,
            "E2_post_threshold_dice": self.e2_min,
            "E3_volume_rel_diff": self.e3_max,
        }

    def is_tighter_than(self, other: Tolerances) -> bool:
        """`MOS-TRAIN-162`: "A `ModelVersion` MAY declare tighter values and MUST NOT
        declare looser ones." Tighter is: smaller ceilings, larger floor."""
        return (
            self.e1_max <= other.e1_max
            and self.e2_min >= other.e2_min
            and self.e3_max <= other.e3_max
        )


#: `MOS-TRAIN-162`'s table, transcribed. `tf32` and `fp16` share one row in the chapter.
TOLERANCES: Final[dict[str, Tolerances]] = {
    "fp32": Tolerances(e1_max=1e-4, e2_min=0.9995, e3_max=1e-3),
    "tf32": Tolerances(e1_max=5e-3, e2_min=0.998, e3_max=5e-3),
    "fp16": Tolerances(e1_max=5e-3, e2_min=0.998, e3_max=5e-3),
    "int8": Tolerances(e1_max=5e-2, e2_min=0.99, e3_max=2e-2),
}


def _refuse(refusals: Sequence[Refusal]) -> None:
    raise ConversionRefused(tuple(refusals))


def _one(check_id: str, code: str, message: str, **kw: Any) -> Refusal:
    return Refusal(check_id=check_id, code=code, message=message, **kw)


def tolerances_for(
    precision: str, *, declared: Mapping[str, float] | None = None
) -> Tolerances:
    """The bounds this conversion is held to. Tighter is accepted; looser is refused.

    `MOS-TRAIN-162`'s one-sentence rule is the whole of this function, and the refusal
    quotes `MOS-TRAIN-164`'s reason for why there is no `override=`.
    """
    if precision not in TOLERANCES:
        raise ValueError(
            f"precision {precision!r} is not one of {PRECISION_CLASSES}; "
            "MOS-TRAIN-162's table has one row per precision class and no default row"
        )
    default = TOLERANCES[precision]
    if declared is None:
        return default

    candidate = Tolerances(
        e1_max=float(declared.get("E1_max_abs_probability_diff", default.e1_max)),
        e2_min=float(declared.get("E2_post_threshold_dice", default.e2_min)),
        e3_max=float(declared.get("E3_volume_rel_diff", default.e3_max)),
    )
    if not candidate.is_tighter_than(default):
        _refuse(
            [
                _one(
                    "MOS-TRAIN-162",
                    "declared_tolerance_is_looser",
                    "a ModelVersion MAY declare tighter values and MUST NOT declare "
                    "looser ones. MOS-TRAIN-164: the remedy for a failing conversion is a "
                    "different conversion -- another precision class, another opset, a "
                    "different plugin or layer-fusion configuration -- never a relaxed "
                    "tolerance on that version, because a tolerance relaxed to admit one "
                    "artifact silently relaxes it for every future artifact of that family",
                    observed=candidate.as_dict(),
                    bound=default.as_dict(),
                    detail={"precision": precision},
                )
            ]
        )
    return candidate


@dataclass(frozen=True)
class CaseEquivalence:
    """One case of the equivalence corpus, measured at `MOS-TRAIN-161`'s three points."""

    case_key: str
    patient_key: str
    e1_max_abs_probability_diff: float
    e2_post_threshold_dice: float
    e3_volume_rel_diff: float
    max_abs_logit_diff: float | None = None
    is_golden_fixture: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_key": self.case_key,
            "patient_key": self.patient_key,
            "E1": self.e1_max_abs_probability_diff,
            "E2": self.e2_post_threshold_dice,
            "E3": self.e3_volume_rel_diff,
            "max_abs_logit_diff": self.max_abs_logit_diff,
            "is_golden_fixture": self.is_golden_fixture,
        }


def equivalence_cohort_digest(patient_keys: Sequence[str]) -> str:
    """`sha256:` over the sorted, de-duplicated patient keys. `MOS-TRAIN-159`.

    "frozen as a named subset with its own digest, and reused **unchanged** for every
    conversion of that model family, so that two conversions are comparable to each other
    and a slow degradation across a series of conversions is visible."
    """
    return "sha256:" + sha256_hex(
        canonical_bytes({"equivalence_cohort": sorted(set(patient_keys))})
    )


def evaluate_equivalence(
    cases: Sequence[CaseEquivalence],
    *,
    precision: str,
    declared_tolerances: Mapping[str, float] | None = None,
    require_golden_fixture: bool = True,
) -> dict[str, Any]:
    """The `conversion_equivalence` block, per case, with no mean anywhere.

    Returns the block `conversion_runs.equivalence` stores and `MOS-REG-104` requires on
    the converted version. Raises `ConversionRefused` naming every offending case when any
    bound is missed -- `MOS-TRAIN-163`: "E2 and E3 MUST be satisfied by **every case** in
    the equivalence cohort, not by the cohort mean."

    Two corpora, both mandatory (`MOS-TRAIN-159`): the golden fixture, which "catches a
    gross wiring error -- transposed axes, a lost normalisation, an output head bound to
    the wrong tensor -- in seconds, before a cohort run is scheduled"; and an
    `equivalence_cohort` of at least twenty distinct `patient_key`s from `tune`.
    """
    bounds = tolerances_for(precision, declared=declared_tolerances)
    problems: list[Refusal] = []

    golden = [c for c in cases if c.is_golden_fixture]
    cohort = [c for c in cases if not c.is_golden_fixture]
    if require_golden_fixture and not golden:
        problems.append(
            _one(
                "MOS-TRAIN-159",
                "golden_fixture_case_absent",
                "the golden fixture is one of two MANDATORY corpora: one volume, which "
                "catches a gross wiring error in seconds, before a cohort run is "
                "scheduled",
                observed=0,
                bound=1,
            )
        )
    patients = {c.patient_key for c in cohort}
    if len(patients) < MIN_EQUIVALENCE_PATIENTS:
        problems.append(
            _one(
                "MOS-TRAIN-159",
                "equivalence_cohort_too_small",
                f"the equivalence cohort MUST hold at least {MIN_EQUIVALENCE_PATIENTS} "
                "distinct patient_keys drawn from the tune partition of the split the "
                "source version was trained on; the test partition MUST NOT be used",
                observed=len(patients),
                bound=MIN_EQUIVALENCE_PATIENTS,
            )
        )

    failures: list[dict[str, Any]] = []
    for case in cases:
        breached: list[str] = []
        if case.e1_max_abs_probability_diff > bounds.e1_max:
            breached.append("E1")
        if case.e2_post_threshold_dice < bounds.e2_min:
            breached.append("E2")
        if case.e3_volume_rel_diff > bounds.e3_max:
            breached.append("E3")
        if breached:
            failures.append({**case.as_dict(), "breached": breached})

    if failures:
        problems.append(
            _one(
                "MOS-TRAIN-163",
                "per_case_tolerance_breached",
                f"{len(failures)} case(s) missed a per-case bound. The tolerances are "
                "per-case floors and ceilings, not a cohort mean: a mean "
                "post_threshold_dice of 0.999 over 20 cases is consistent with nineteen "
                "cases at 1.000 and one at 0.980, and on a 40 mL effusion a Dice of 0.980 "
                "is a symmetric difference of roughly 1.6 mL -- exactly the size of "
                "difference that moves a single case across a reporting threshold while "
                "the aggregate reports that nothing happened",
                observed=len(failures),
                bound=0,
                detail={"precision": precision, "tolerances": bounds.as_dict(),
                        "failures": failures[:20]},
            )
        )

    if problems:
        _refuse(problems)

    worst_e1 = max((c.e1_max_abs_probability_diff for c in cases), default=0.0)
    worst_e2 = min((c.e2_post_threshold_dice for c in cases), default=1.0)
    worst_e3 = max((c.e3_volume_rel_diff for c in cases), default=0.0)
    logit = [c.max_abs_logit_diff for c in cases if c.max_abs_logit_diff is not None]
    return {
        # The three quantities, reported as their WORST case. There is no mean: a mean
        # would be the aggregate MOS-TRAIN-163 is about.
        "E1": {"quantity": "max_abs_probability_diff", "worst_case": worst_e1,
               "bound": bounds.e1_max, "direction": "<="},
        "E2": {"quantity": "post_threshold_dice", "worst_case": worst_e2,
               "bound": bounds.e2_min, "direction": ">="},
        "E3": {"quantity": "volume_rel_diff", "worst_case": worst_e3,
               "bound": bounds.e3_max, "direction": "<="},
        # MOS-REG-104 names it; MOS-TRAIN-161 says it MUST NOT be the gating quantity, so
        # it carries no bound.
        "max_abs_logit_diff": max(logit) if logit else None,
        "tolerances": bounds.as_dict(),
        "precision": precision,
        "n_cases": len(cases),
        "n_patients": len(patients),
        "golden_fixture_included": bool(golden),
        "per_case": [c.as_dict() for c in cases],
        "verdict": "pass",
        "disclaimer": EQUIVALENCE_DISCLAIMER,
    }


# =====================================================================================
# The record
# =====================================================================================
_COLUMNS = """
    id, public_id, tenant_id, source_model_version_id, target_model_version_id,
    target_format, precision, toolchain, built_for, calibration_dataset_version_id,
    calibration_partition, equivalence_cohort_digest, equivalence, code_commit,
    image_digest, state, failure_reason, started_at, finished_at, created_at, updated_at
"""


def create(
    conn: psycopg.Connection[Any],
    *,
    source_model_version_id: str,
    target_format: str,
    precision: str,
    toolchain: Mapping[str, Any],
    equivalence_cohort_digest: str,
    code_commit: str,
    image_digest: str,
    calibration_dataset_version_id: str | None = None,
    calibration_partition: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Record a `ConversionRun` at `PENDING`. `MOS-TRAIN-156`.

    Refuses before the row exists when the source is `SUSPENDED` or `RECALLED`
    (`MOS-TRAIN-169`) and when an `int8` conversion names a calibration partition other
    than `tune` (`MOS-TRAIN-157`: "Calibrating on `test` is operating-point selection on
    the test set under another name").
    """
    problems: list[Refusal] = []
    if target_format not in TARGET_FORMATS:
        problems.append(
            _one("MOS-TRAIN-156", "unknown_target_format",
                 f"target_format MUST be one of {TARGET_FORMATS}",
                 observed=target_format, bound=list(TARGET_FORMATS))
        )
    if precision not in PRECISION_CLASSES:
        problems.append(
            _one("MOS-TRAIN-156", "unknown_precision_class",
                 f"precision MUST be one of {PRECISION_CLASSES}",
                 observed=precision, bound=list(PRECISION_CLASSES))
        )
    if (precision == "int8") != (calibration_dataset_version_id is not None):
        problems.append(
            _one(
                "MOS-TRAIN-157",
                "int8_calibration_binding",
                "int8 calibration MUST draw its calibration cohort from the tune "
                "partition and MUST record it; no other precision class has one",
                observed={"precision": precision,
                          "calibration": calibration_dataset_version_id},
            )
        )
    if calibration_partition is not None and calibration_partition != "tune":
        problems.append(
            _one(
                "MOS-TRAIN-157",
                "calibration_partition_not_tune",
                "calibrating on test is operating-point selection on the test set under "
                "another name, and is refused by MOS-EVID-033",
                observed=calibration_partition, bound="tune",
            )
        )
    if problems:
        _refuse(problems)

    tenant = str(tenant_id or current_tenant())
    with tenant_tx(conn, tenant) as tx:
        source = tx.execute(
            "SELECT public_id, lifecycle_status, kind FROM artifacts WHERE id = %s",
            (_uuid.UUID(str(source_model_version_id)),),
        ).fetchone()
        if source is None:
            raise TrainingError(f"no artifact {source_model_version_id} in this tenant")
        status = dict(source)["lifecycle_status"]
        if status in ("SUSPENDED", "RECALLED"):
            _refuse(
                [
                    _one(
                        "MOS-TRAIN-169",
                        "source_is_not_convertible",
                        f"the source ModelVersion is {status}. A conversion MUST NOT be "
                        "performed against a suspended or recalled source: a recall that "
                        "stops at the source while its conversion keeps serving is the "
                        "recall failing at the only point where it mattered",
                        observed=status,
                        bound=["DRAFT", "REGISTERED", "VALIDATING", "VALIDATED",
                               "APPROVED", "DEPRECATED"],
                        detail={"source": dict(source)["public_id"]},
                    )
                ]
            )

        row = tx.execute(
            f"""
            INSERT INTO conversion_runs
                (public_id, tenant_id, source_model_version_id, target_format, precision,
                 toolchain, calibration_dataset_version_id, calibration_partition,
                 equivalence_cohort_digest, code_commit, image_digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (
                new_ulid("cv"), _uuid.UUID(tenant),
                _uuid.UUID(str(source_model_version_id)), target_format, precision,
                Jsonb(dict(toolchain)),
                _uuid.UUID(str(calibration_dataset_version_id))
                if calibration_dataset_version_id else None,
                calibration_partition, equivalence_cohort_digest, code_commit,
                image_digest,
            ),
        ).fetchone()
    return dict(row)


def start(
    conn: psycopg.Connection[Any],
    *,
    conversion_id: str,
    built_for: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """`PENDING -> RUNNING`, recording the OBSERVED `built_for`. `MOS-TRAIN-167`.

    "`built_for` MUST be read from the driver and runtime on the machine that performed
    the conversion, never taken from configuration or from a template. A `built_for` that
    is configured rather than observed is a declaration that a plan will load correctly on
    hardware it was not built for, and Triton's refusal to load is the last defence."

    This function cannot tell an observed value from a configured one and does not pretend
    to. What it CAN do is refuse a `tensorrt_plan` conversion that declares none, which is
    the state in which `MOS-OPS-070`'s load-time refusal has nothing to compare against.
    """
    row = get(conn, conversion_id, tenant_id=tenant_id)
    if row["state"] != "PENDING":
        raise TrainingError(f"conversion {row['public_id']} is {row['state']}, not PENDING")
    if row["target_format"] == "tensorrt_plan":
        keys = {"cuda_compute_capability", "tensorrt_version", "cuda_version"}
        if not built_for or not keys <= set(built_for):
            _refuse(
                [
                    _one(
                        "MOS-TRAIN-167",
                        "built_for_not_observed",
                        "a tensorrt_plan conversion MUST record the compute capability, "
                        "the TensorRT version and the CUDA version read from the machine "
                        "that performed it; MOS-OPS-070's load-time refusal is the last "
                        "defence and it needs something to compare against",
                        observed=sorted(built_for or {}),
                        bound=sorted(keys),
                    )
                ]
            )
    with tenant_tx(conn, tenant_id) as tx:
        out = tx.execute(
            f"""
            UPDATE conversion_runs
               SET state = 'RUNNING', started_at = %s, built_for = %s
             WHERE id = %s AND state = 'PENDING'
            RETURNING {_COLUMNS}
            """,
            (now or datetime.now(UTC), Jsonb(dict(built_for)) if built_for else None,
             _uuid.UUID(str(row["id"]))),
        ).fetchone()
    if out is None:  # pragma: no cover
        raise TrainingError(f"conversion {conversion_id} was not PENDING")
    return dict(out)


def succeed(
    conn: psycopg.Connection[Any],
    *,
    conversion_id: str,
    target_model_version_id: str,
    equivalence: Mapping[str, Any],
    patch_batch_size_digests: Mapping[int, str] | None = None,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """`RUNNING -> SUCCEEDED`, binding the converted `ModelVersion`. `MOS-TRAIN-164`.

    `equivalence` is what `evaluate_equivalence()` returned; it already refused if any
    per-case bound was missed, so a block reaching here carries `verdict: pass` and the
    0013 CHECK requires exactly that.

    `patch_batch_size_digests` is `MOS-TRAIN-168`: "The conversion job MUST assert
    bit-identical output on the golden fixture across every value in
    `allowed_patch_batch_sizes` before publish. This is where that assertion runs. It is
    what allows Chapter 13 to treat 'reduce `patch_batch_size` to fit another model on the
    GPU' as a capacity decision rather than a revalidation event."
    """
    row = get(conn, conversion_id, tenant_id=tenant_id)
    if row["state"] != "RUNNING":
        raise TrainingError(f"conversion {row['public_id']} is {row['state']}, not RUNNING")
    if equivalence.get("verdict") != "pass":
        _refuse(
            [
                _one("MOS-TRAIN-164", "conversion_did_not_pass",
                     "a conversion failing any tolerance MUST NOT be registered",
                     observed=equivalence.get("verdict"), bound="pass")
            ]
        )

    block = dict(equivalence)
    if patch_batch_size_digests is not None:
        digests = {int(k): str(v) for k, v in patch_batch_size_digests.items()}
        distinct = set(digests.values())
        if len(distinct) > 1:
            _refuse(
                [
                    _one(
                        "MOS-TRAIN-168",
                        "patch_batch_size_changes_the_output",
                        "the golden fixture's output is not bit-identical across every "
                        "value in allowed_patch_batch_sizes, so reducing the batch to fit "
                        "another model on the GPU is a revalidation event rather than a "
                        "capacity decision (MOS-OPS-078, MOS-OPS-088)",
                        observed=digests,
                        bound=1,
                    )
                ]
            )
        block["patch_batch_size_invariance"] = {
            "allowed_patch_batch_sizes": sorted(digests),
            "golden_fixture_output_digest": next(iter(distinct), None),
        }

    with tenant_tx(conn, tenant_id) as tx:
        out = tx.execute(
            f"""
            UPDATE conversion_runs
               SET state = 'SUCCEEDED', finished_at = %s, equivalence = %s,
                   target_model_version_id = %s
             WHERE id = %s AND state = 'RUNNING'
            RETURNING {_COLUMNS}
            """,
            (now or datetime.now(UTC), Jsonb(block),
             _uuid.UUID(str(target_model_version_id)), _uuid.UUID(str(row["id"]))),
        ).fetchone()
    if out is None:  # pragma: no cover
        raise TrainingError(f"conversion {conversion_id} was not RUNNING")
    return dict(out)


def fail(
    conn: psycopg.Connection[Any],
    *,
    conversion_id: str,
    reason: str,
    equivalence: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Any non-terminal state -> `FAILED`, retaining what was measured.

    The measurements are kept on a failure because `MOS-TRAIN-159`'s equivalence cohort is
    "reused **unchanged** for every conversion of that model family, so that ... a slow
    degradation across a series of conversions is visible" -- which is only visible if the
    failures are still there to look at.
    """
    if not reason:
        raise ValueError("a FAILED conversion MUST name a reason (MOS-REL-051)")
    row = get(conn, conversion_id, tenant_id=tenant_id)
    if row["state"] in ("SUCCEEDED", "FAILED"):
        raise TrainingError(f"conversion {row['public_id']} is already terminal")
    with tenant_tx(conn, tenant_id) as tx:
        out = tx.execute(
            f"""
            UPDATE conversion_runs
               SET state = 'FAILED', failure_reason = %s, finished_at = %s,
                   equivalence = coalesce(%s, equivalence)
             WHERE id = %s AND state IN ('PENDING','RUNNING')
            RETURNING {_COLUMNS}
            """,
            (reason, now or datetime.now(UTC),
             Jsonb(dict(equivalence)) if equivalence else None,
             _uuid.UUID(str(row["id"]))),
        ).fetchone()
    if out is None:  # pragma: no cover
        raise TrainingError(f"conversion {conversion_id} was already terminal")
    return dict(out)


def get(
    conn: psycopg.Connection[Any], ident: str, *, tenant_id: str | None = None
) -> dict[str, Any]:
    """One conversion by `public_id` or uuid."""
    column = "public_id" if ident.startswith("cv_") else "id::text"
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            f"SELECT {_COLUMNS} FROM conversion_runs WHERE {column} = %s", (ident,)
        ).fetchone()
    if row is None:
        raise TrainingError(f"no conversion run {ident}")
    return dict(row)


# =====================================================================================
# The two rules about the converted version that are NOT about numerics
# =====================================================================================
def preprocessing_carries_forward(
    source_spec: Mapping[str, Any], target_spec: Mapping[str, Any]
) -> tuple[Refusal, ...]:
    """`MOS-TRAIN-166`: a conversion changes numerics, never preprocessing.

    "A converted `ModelVersion` MUST reference the **same** `PreprocessingSpec` version as
    its `derived_from` source, and its `spec.golden_fixture.output_tensor_sha256` MUST be
    byte-identical to the source's -- preprocessing is not what changed. If the conversion
    required a preprocessing change ... that is a new `PreprocessingSpec` version by
    `MOS-IMG-046`, which is a new `ModelVersion` lineage requiring a fresh training-side
    evaluation -- not a conversion, and it MUST NOT be registered with `derived_from` set
    as though it were."

    Returns the differences rather than raising, so a publisher can be shown all of them.
    """
    out: list[Refusal] = []
    for field in ("preprocessing_spec_ref",):
        if source_spec.get(field) != target_spec.get(field):
            out.append(
                _one(
                    "MOS-TRAIN-166",
                    "conversion_changed_preprocessing",
                    f"{field} differs between the source and the conversion. "
                    "Preprocessing is not what changed; if it did, this is a new lineage "
                    "requiring a fresh training-side evaluation, not a conversion",
                    observed=target_spec.get(field),
                    bound=source_spec.get(field),
                )
            )
    src_golden = dict(source_spec.get("golden_fixture") or {})
    tgt_golden = dict(target_spec.get("golden_fixture") or {})
    if src_golden.get("output_tensor_sha256") != tgt_golden.get("output_tensor_sha256"):
        out.append(
            _one(
                "MOS-TRAIN-166",
                "golden_tensor_hash_differs",
                "spec.golden_fixture.output_tensor_sha256 MUST be byte-identical to the "
                "source's. MOS-TRAIN-134 makes medicalos-preprocessing the ONE recorder "
                "of that value, so a difference means the preprocessing changed",
                observed=tgt_golden.get("output_tensor_sha256"),
                bound=src_golden.get("output_tensor_sha256"),
            )
        )
    return tuple(out)


def run_exercises_the_served_plan(
    *,
    model_backend: str,
    model_built_for: Mapping[str, Any] | None,
    run_inference_backend: Mapping[str, Any],
    run_accelerator: Mapping[str, Any],
) -> tuple[Refusal, ...]:
    """`MOS-TRAIN-153`: the run must exercise the plan actually served, not the checkpoint.

    "An `EvaluationRun` executed against the checkpoint inside a training container MUST
    NOT be attached to a converted `ModelVersion`, whatever its numbers are. **The run must
    exercise the plan actually served, not the checkpoint that was trained** -- this
    sentence is the whole content of the requirement and is the single thing most often got
    wrong."

    Chapter 17 acceptance check 9 is the executable form and this function is what answers
    it: attaching a PyTorch-runtime run to a `tensorrt_plan` version, or an `sm_80` run to
    a version built for `8.6`, is refused.
    """
    out: list[Refusal] = []
    run_backend = str(
        run_inference_backend.get("kind") or run_inference_backend.get("backend") or ""
    )
    if model_backend and run_backend and run_backend != model_backend:
        out.append(
            _one(
                "MOS-TRAIN-153",
                "run_backend_is_not_the_served_backend",
                f"the version is served by {model_backend!r} and the run was executed "
                f"under {run_backend!r}. The run must exercise the plan actually served, "
                "not the checkpoint that was trained",
                observed=run_backend,
                bound=model_backend,
            )
        )
    if model_built_for:
        want = str(model_built_for.get("cuda_compute_capability") or "")
        got = str(run_accelerator.get("cuda_compute_capability")
                  or run_accelerator.get("gpu_architecture") or "")
        got_norm = got.replace("sm_", "")
        want_norm = want.replace("sm_", "")
        if want_norm and got_norm and want_norm.replace(".", "") != got_norm.replace(".", ""):
            out.append(
                _one(
                    "MOS-TRAIN-153",
                    "run_accelerator_is_not_the_built_for_target",
                    f"the plan was built for compute capability {want!r} and the run ran "
                    f"on {got!r}. A serialized engine is pinned to a GPU architecture and "
                    "a runtime build (MOS-REL-037, MOS-OPS-070)",
                    observed=got,
                    bound=want,
                )
            )
    return tuple(out)
