# SPDX-License-Identifier: Apache-2.0
"""`served-plan-equivalence` -- §15.1.2's 0.3.0 row via chapter 17's `MOS-TRAIN-193`.

    "the artifact actually served is the artifact that was evaluated -- a backend
     conversion is a NEW `ModelVersion` with its OWN `EvaluationRun`"

THE SENTENCE THE WHOLE CHECK IS ABOUT
---------------------------------------
`MOS-TRAIN-153`, and the requirement says of itself that it "is the whole content of the
requirement and is the single thing most often got wrong":

    "An `EvaluationRun` executed against the checkpoint inside a training container MUST
     NOT be attached to a converted `ModelVersion`, whatever its numbers are. **The run
     must exercise the plan actually served, not the checkpoint that was trained.**"

The defect it forbids is not exotic. A team trains a model, evaluates the PyTorch
checkpoint, converts to a serialized engine for the GPU it will serve on, and attaches the
evaluation it already has -- because converting is "just a format change". It is not: a
fused kernel, a different reduction order and a lower precision class all move the
numbers, and the artifact in front of a patient is then one nobody measured.

FOUR SURFACES, AND THE DATABASE IS THE ONE THAT CANNOT BE ARGUED WITH
-----------------------------------------------------------------------
  IDENTITY     `conversion_runs` carries `CHECK (target_model_version_id <>
               source_model_version_id)`. A conversion that registered back onto its own
               source WOULD be the checkpoint's evaluation serving the converted plan, and
               the schema refuses it -- for a `psql` session as much as for this code.
  ATTACHMENT   `run_exercises_the_served_plan` refuses a run whose backend is not the
               version's backend, and a run executed on a compute capability the plan was
               not built for. A serialized engine is pinned to a GPU architecture and a
               runtime build; an `sm_80` measurement of an `sm_86` plan is a measurement
               of something else.
  PER CASE     `MOS-TRAIN-163`: every case holds the tolerance, and a cohort mean does not
               rescue one that does not. Chapter 17 acceptance check 8 states the fixture
               exactly -- one case at `post_threshold_dice = 0.980` in an otherwise perfect
               `fp32` cohort whose mean is 0.999 -- because the aggregate is precisely the
               statistic that reports nothing happened.
  LINEAGE      `MOS-TRAIN-166`: a conversion changes numerics and never preprocessing. If
               preprocessing changed, it is a new lineage needing a fresh training-side
               evaluation, and MUST NOT be registered with `derived_from` set as though it
               were a conversion.

Needs a schema for the identity surface. The other three are pure.

Spec: MOS-EVID-064, MOS-TRAIN-153, MOS-TRAIN-156, MOS-TRAIN-159, MOS-TRAIN-162,
MOS-TRAIN-163, MOS-TRAIN-164, MOS-TRAIN-166, MOS-TRAIN-167, MOS-TRAIN-193, MOS-REL-004;
chapter 17 acceptance criteria 8 and 9; docs/spec/06-registries.md §6.10.2.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import psycopg
import pytest
from medos.registry.digest import content_digest_of
from medos.sdk.errors import ConversionRefused
from medos.training import conversion as conv

from tests.gate import _platform as P

pytestmark = pytest.mark.gate_0_3_0

SOURCE_RUN = "er_01JP4T9X7B"
TARGET_RUN = "er_01JT5R2Q8NA"


def _cases(n: int = 21, *, bad_index: int | None = None) -> list[conv.CaseEquivalence]:
    """`MOS-TRAIN-159`'s two corpora: the golden fixture plus >= 20 patients."""
    out = [
        conv.CaseEquivalence(
            case_key="golden",
            patient_key="golden",
            e1_max_abs_probability_diff=1e-6,
            e2_post_threshold_dice=1.0,
            e3_volume_rel_diff=0.0,
            max_abs_logit_diff=1e-5,
            is_golden_fixture=True,
        )
    ]
    for i in range(n):
        out.append(
            conv.CaseEquivalence(
                case_key=f"case-{i}",
                patient_key=f"pk-{i}",
                e1_max_abs_probability_diff=2e-5,
                e2_post_threshold_dice=0.980 if i == bad_index else 0.99995,
                e3_volume_rel_diff=2e-4,
                max_abs_logit_diff=3e-4,
            )
        )
    return out


def _model(family: str, version: str, *, run_id: str, **spec: Any) -> dict[str, Any]:
    manifest = P.model_manifest(family, version, **spec)
    manifest["spec"]["evaluation_run_id"] = run_id
    manifest["spec"]["operating_point"]["selected_on_evaluation_run"] = run_id
    manifest["content_digest"] = content_digest_of(
        {k: v for k, v in manifest.items() if k != "content_digest"}
    )
    return manifest


# =====================================================================================
# IDENTITY -- a conversion is a NEW ModelVersion
# =====================================================================================
def test_served_plan_equivalence_the_conversion_registers_a_new_version_with_its_own_run(
    platform_db: Any,
) -> None:
    """`MOS-EVID-064` / §6.10.2, end to end against the registry and 0013's table.

    The converted artifact is a separate `artifacts` row, its `derived_from` names the
    source, and its `evaluation_run_id` is NOT the source's. The last of those three is
    the check's subject: the first two can both be true while the converted plan is still
    serving on the checkpoint's numbers.
    """
    source = P.publish(
        platform_db, _model("pulmo.effusion-unet", "1.0.0", run_id=SOURCE_RUN), "mv_src_1_0_0"
    )
    target = P.publish(
        platform_db,
        _model(
            "pulmo.effusion-unet",
            # `1.0.1` and not `1.0.0+trt`: chapter 10's version grammar admits a
            # prerelease suffix and no build metadata, so a conversion is a new VERSION of
            # the family rather than a decoration on the source's.
            "1.0.1",
            run_id=TARGET_RUN,
            derived_from="mv_src_1_0_0",
        ),
        "mv_src_1_0_0_trt",
    )
    assert source["id"] != target["id"], "the conversion did not create a new artifact row"

    run = conv.create(
        platform_db,
        source_model_version_id=source["id"],
        target_format="tensorrt_plan",
        precision="fp32",
        toolchain={"tensorrt": "10.3.0"},
        equivalence_cohort_digest=conv.equivalence_cohort_digest(
            [f"pk-{i}" for i in range(20)]
        ),
        code_commit=P.COMMIT,
        image_digest=P.IMAGE,
    )
    conv.start(
        platform_db,
        conversion_id=run["public_id"],
        # MOS-TRAIN-167's three OBSERVED values. A tensorrt_plan conversion that declares
        # none is refused: MOS-OPS-070's load-time refusal is the last defence and it
        # needs something to compare against.
        built_for={
            "cuda_compute_capability": "8.6",
            "tensorrt_version": "10.3.0",
            "cuda_version": "12.4",
        },
    )
    done = conv.succeed(
        platform_db,
        conversion_id=run["public_id"],
        target_model_version_id=target["id"],
        equivalence=conv.evaluate_equivalence(_cases(), precision="fp32"),
    )
    platform_db.commit()

    assert done["state"] == "SUCCEEDED"
    assert str(done["target_model_version_id"]) == str(target["id"])
    assert str(done["source_model_version_id"]) == str(source["id"])

    rows = {
        str(r["id"]): r
        for r in platform_db.execute(
            "SELECT id, manifest FROM artifacts WHERE id IN (%s, %s)",
            (source["id"], target["id"]),
        ).fetchall()
    }
    src_spec = rows[str(source["id"])]["manifest"]["spec"]
    tgt_spec = rows[str(target["id"])]["manifest"]["spec"]
    assert tgt_spec["derived_from"] == "mv_src_1_0_0", (
        "the converted version does not name its source; §6.10.2 makes `derived_from` how "
        "a recall of the source reaches its conversions (MOS-TRAIN-169)"
    )
    assert tgt_spec["evaluation_run_id"] != src_spec["evaluation_run_id"], (
        "the converted ModelVersion carries the SOURCE's EvaluationRun. This is the defect "
        "MOS-TRAIN-153 names as the one most often got wrong: the artifact in front of a "
        "patient was never measured, and its numbers belong to a checkpoint."
    )
    # And the preprocessing did NOT change: a conversion changes numerics only.
    assert not conv.preprocessing_carries_forward(src_spec, tgt_spec)


def test_served_plan_equivalence_the_database_refuses_a_conversion_onto_its_own_source(
    platform_db: Any,
) -> None:
    """0013's `CHECK (target_model_version_id <> source_model_version_id)`.

    The structural form of "a NEW ModelVersion". Asserted against the database rather than
    the library because the library is one refactor from stopping, and the table is not.
    """
    source = P.publish(
        platform_db, _model("pulmo.effusion-unet", "2.0.0", run_id=SOURCE_RUN), "mv_self_2_0_0"
    )
    run = conv.create(
        platform_db,
        source_model_version_id=source["id"],
        target_format="tensorrt_plan",
        precision="fp16",
        toolchain={"tensorrt": "10.3.0"},
        equivalence_cohort_digest=conv.equivalence_cohort_digest(
            [f"pk-{i}" for i in range(20)]
        ),
        code_commit=P.COMMIT,
        image_digest=P.IMAGE,
    )
    conv.start(
        platform_db,
        conversion_id=run["public_id"],
        built_for={
            "cuda_compute_capability": "8.6",
            "tensorrt_version": "10.3.0",
            "cuda_version": "12.4",
        },
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        conv.succeed(
            platform_db,
            conversion_id=run["public_id"],
            target_model_version_id=source["id"],
            equivalence=conv.evaluate_equivalence(_cases(), precision="fp16"),
        )
    platform_db.rollback()


# =====================================================================================
# ATTACHMENT -- MOS-TRAIN-153, chapter 17 acceptance criterion 9
# =====================================================================================
def test_served_plan_equivalence_a_checkpoint_run_cannot_be_attached_to_a_plan() -> None:
    """The requirement's own sentence, executed. Two ways of getting it wrong.

    A PyTorch-runtime run attached to a `tensorrt_plan` version; and an `sm_80` run
    attached to a plan built for 8.6. Both are "the artifact served is not the artifact
    that was evaluated", and the second is the subtler one -- the backend name matches and
    the engine still will not load, or loads and computes differently.
    """
    wrong_backend = conv.run_exercises_the_served_plan(
        model_backend="tensorrt",
        model_built_for={"cuda_compute_capability": "8.6"},
        run_inference_backend={"kind": "pytorch"},
        run_accelerator={"cuda_compute_capability": "8.6"},
    )
    assert wrong_backend, "a PyTorch run was accepted for a TensorRT plan"
    assert wrong_backend[0].check_id == "MOS-TRAIN-153"
    assert wrong_backend[0].code == "run_backend_is_not_the_served_backend"

    wrong_arch = conv.run_exercises_the_served_plan(
        model_backend="tensorrt",
        model_built_for={"cuda_compute_capability": "8.6"},
        run_inference_backend={"kind": "tensorrt"},
        run_accelerator={"cuda_compute_capability": "8.0"},
    )
    assert wrong_arch, "a run on sm_80 was accepted for a plan built for 8.6"
    assert wrong_arch[0].code == "run_accelerator_is_not_the_built_for_target"

    # THE CONTROL. Without it the function could refuse everything.
    assert (
        conv.run_exercises_the_served_plan(
            model_backend="tensorrt",
            model_built_for={"cuda_compute_capability": "8.6"},
            run_inference_backend={"kind": "tensorrt"},
            run_accelerator={"cuda_compute_capability": "8.6"},
        )
        == ()
    )


# =====================================================================================
# PER CASE -- MOS-TRAIN-163, chapter 17 acceptance criterion 8
# =====================================================================================
def test_served_plan_equivalence_one_case_below_the_floor_refuses_the_registration() -> None:
    """Criterion 8 verbatim: a mean of 0.999 does not rescue a case at 0.980.

    The arithmetic is asserted, not assumed: the spoiled cohort's mean really is 0.999 to
    three places and really is above the 0.9995 per-case floor being missed by one case.
    Otherwise this test would be "a bad cohort is refused", which is a different and much
    weaker claim.
    """
    clean = conv.evaluate_equivalence(_cases(), precision="fp32")
    assert clean["verdict"] == "pass"
    assert clean["E2"]["bound"] == 0.9995 and clean["E1"]["bound"] == 1e-4
    assert clean["n_patients"] >= conv.MIN_EQUIVALENCE_PATIENTS
    # No aggregate anywhere in the block. An equivalence report that carries a mean is one
    # somebody will read instead of the per-case floor.
    assert "mean" not in json.dumps(clean).lower().replace("max_abs", "")

    spoiled = _cases(bad_index=7)
    mean_dice = sum(c.e2_post_threshold_dice for c in spoiled[1:]) / (len(spoiled) - 1)
    assert round(mean_dice, 3) == 0.999, mean_dice
    # The aggregate reports that nothing happened. It sits within 0.0005 of the floor it
    # is missing, and nineteen thousandths ABOVE the case that is actually failing -- so
    # any gate a reasonable person would write over the mean (">= 0.99", ">= 0.995") lets
    # this cohort through. That gap is the whole reason MOS-TRAIN-163 is per-case.
    worst = min(c.e2_post_threshold_dice for c in spoiled[1:])
    assert worst == 0.980 and mean_dice - worst > 0.018, (worst, mean_dice)
    assert mean_dice >= 0.995, mean_dice
    with pytest.raises(ConversionRefused) as exc:
        conv.evaluate_equivalence(spoiled, precision="fp32")
    refusal = exc.value.refusals[0]
    assert refusal.check_id == "MOS-TRAIN-163"
    assert refusal.detail["failures"][0]["case_key"] == "case-7", (
        "the refusal does not name the offending case; an operator cannot act on 'a case "
        "failed'"
    )


def test_served_plan_equivalence_the_bound_is_a_property_of_the_precision_class() -> None:
    """`MOS-TRAIN-162`: one row per class, no default row, and no way to loosen one.

    A tolerance relaxed to admit one artifact relaxes it for every future artifact of that
    family, which is why `evaluate_equivalence` has no override parameter at all -- checked
    here by signature, because an override added later would make every assertion above
    conditional on nobody using it.
    """
    marginal = _cases()
    marginal[8] = conv.CaseEquivalence(
        case_key="case-7",
        patient_key="pk-7",
        e1_max_abs_probability_diff=2e-5,
        e2_post_threshold_dice=0.9990,
        e3_volume_rel_diff=2e-4,
        max_abs_logit_diff=3e-4,
    )
    assert conv.evaluate_equivalence(marginal, precision="fp16")["verdict"] == "pass"
    with pytest.raises(ConversionRefused):
        conv.evaluate_equivalence(marginal, precision="fp32")

    assert conv.tolerances_for("fp32", declared={"E2_post_threshold_dice": 0.9999}).e2_min
    with pytest.raises(ConversionRefused) as exc:
        conv.tolerances_for("fp32", declared={"E2_post_threshold_dice": 0.99})
    assert exc.value.refusals[0].code == "declared_tolerance_is_looser"

    params = inspect.signature(conv.evaluate_equivalence).parameters
    assert not any("override" in p or "force" in p or "waive" in p for p in params), (
        f"evaluate_equivalence takes {sorted(params)}; a per-call override is how a "
        f"per-case floor becomes advisory"
    )


# =====================================================================================
# LINEAGE -- MOS-TRAIN-166
# =====================================================================================
def test_served_plan_equivalence_a_conversion_may_not_change_preprocessing() -> None:
    """`MOS-TRAIN-166`. A changed preprocessing is a new lineage, not a conversion.

    If preprocessing changed, the source's evaluation does not describe the target even in
    principle -- the model is being fed different tensors -- so registering it with
    `derived_from` set would attach an evaluation of something else. That is the same
    defect as the checkpoint attachment, arriving through the front door.
    """
    source = P.model_manifest("pulmo.effusion-unet", "1.0.0")["spec"]
    same = dict(source)
    assert conv.preprocessing_carries_forward(source, same) == ()

    moved_spec = {**source, "preprocessing_spec_ref": "ps_pulmo_effusion_prep_3_0_0"}
    problems = conv.preprocessing_carries_forward(source, moved_spec)
    assert problems and problems[0].code == "conversion_changed_preprocessing"

    moved_golden = {
        **source,
        "golden_fixture": {
            **source["golden_fixture"],
            "output_tensor_sha256": "sha256:" + "11" * 32,
        },
    }
    problems = conv.preprocessing_carries_forward(source, moved_golden)
    assert problems and problems[0].code == "golden_tensor_hash_differs", (
        "the golden output tensor may differ between a source and its conversion. "
        "MOS-TRAIN-134 makes medicalos-preprocessing the ONE recorder of that value, so a "
        "difference means the preprocessing changed and this is not a conversion."
    )
