# SPDX-License-Identifier: Apache-2.0
"""The run-directory exchange, asserted from the platform side.

WHAT THIS MODULE IS FOR
------------------------
`medos/medos/training/orchestrator.py` and `medos.sdk/contract.py` each
spell the run directory, and this module is the test tying the two
spellings together: the platform writes the directory and reads the result
documents, and a constant written down twice with no test between the two
copies is not one constant.

WHAT CHANGED UNDER THIS GATE
-----------------------------
The trainer no longer participates in this exchange. The vanilla-stack
rewrite made the trainer a standalone framework (cases directory in,
inference bundle out, no run directory at all), so the tests that asserted
the TRAINER's half -- the wired phases, the staged dataset, the supervision
map the backend wrote -- went with the code they read. What remains here is
the PLATFORM's own vocabulary: `RunDirectory`, `RunRequest`, `CohortEntry`
and the result documents are core product surface, still shipped and still
tested from both sides of the platform's own tree.

It needs no torch, no GPU and no container: `medos.sdk.contract` is pure
Python over one directory, which is a deliberate property of that module
and is what keeps these tests unit tests.

Spec: MOS-TRAIN-116, MOS-TRAIN-121, MOS-TRAIN-135, MOS-TRAIN-141, MOS-REL-051,
docs/spec/17-training-pipeline.md acceptance check 15.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TRAINER_ROOT = REPO_ROOT / "trainer"
if not (TRAINER_ROOT / "medos_trainer").is_dir():
    pytest.skip(
        "the trainer tree is not present in this checkout (core split) -- "
        "the same gates run in medlange/trainer's own CI",
        allow_module_level=True,
    )


if str(TRAINER_ROOT) not in sys.path:
    sys.path.insert(0, str(TRAINER_ROOT))

from medos.sdk import contract  # noqa: E402
from medos.training import orchestrator as orch  # noqa: E402


# =====================================================================================
# 1. The two halves of the exchange are one exchange
# =====================================================================================
def test_the_platform_and_the_trainer_name_the_same_run_directory() -> None:
    assert orch.RUN_DIRECTORY == contract.RUN_DIRECTORY, (
        "medos/medos/training/orchestrator.py and medos.sdk/contract.py "
        "disagree about the run directory. The platform writes it and reads "
        "the results back; a path spelled two ways is a cohort that was staged "
        "and not found"
    )


def test_every_role_the_platform_writes_is_a_role_the_platform_reads() -> None:
    """`fingerprint`, `plan`, `bundle` and `result` are the four outputs, by name."""
    for role in ("request", "spec", "cohort_fit", "cohort_select", "images",
                 "fingerprint", "plan", "bundle", "result", "log"):
        assert role in orch.RUN_DIRECTORY, role
        assert role in contract.RUN_DIRECTORY, role


# =====================================================================================
# 2. `spec.argv` -- a vector, a closed phase set, and nowhere to put a cohort argument
# =====================================================================================
def test_trainer_argv_is_a_vector_naming_a_phase_and_a_directory() -> None:
    argv = orch.trainer_argv("plan", "/runs/tr_1")
    assert argv == (orch.TRAINER_ENTRYPOINT, "plan", "--run-dir", "/runs/tr_1")
    assert isinstance(argv, tuple), "a list would let a caller mutate a submitted spec"


@pytest.mark.parametrize("phase", ["train", "all", "", "plan;fit", "PLAN"])
def test_trainer_argv_refuses_a_phase_outside_the_two_mos_train_135_defines(
    phase: str,
) -> None:
    """A third phase here would be a third phase in `MOS-TRAIN-135`, which has two."""
    with pytest.raises(ValueError, match="phase"):
        orch.trainer_argv(phase, "/runs/tr_1")


def test_trainer_argv_refuses_an_empty_run_directory() -> None:
    with pytest.raises(ValueError, match="run directory"):
        orch.trainer_argv("fit", "")


def test_a_training_run_spec_built_from_trainer_argv_is_accepted_by_the_port() -> None:
    """The vector is one `TrainingRunSpec.__post_init__` admits, and it digests."""
    spec = orch.TrainingRunSpec(
        run_public_id="tr_01J0000000000000000000000A:plan",
        capability_id="lung_segmentation",
        argv=orch.trainer_argv("plan", "/runs/tr_1"),
        workdir="/runs/tr_1",
    )
    assert spec.submission_digest.startswith("sha256:")
    # The same instruction twice is the same run: MOS-TRAIN-124's argument, at the port.
    again = orch.TrainingRunSpec(
        run_public_id=spec.run_public_id,
        capability_id=spec.capability_id,
        argv=orch.trainer_argv("plan", "/runs/tr_1"),
        workdir="/runs/tr_1",
    )
    assert spec.submission_digest == again.submission_digest


# =====================================================================================
# 3. `request.json`
# =====================================================================================
def _request(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "contract_version": contract.CONTRACT_VERSION,
        "training_run_id": "tr_01J0000000000000000000000A",
        "capability_id": "lung_segmentation",
        "training_backend": {"kind": "nnunet", "version": "2.5.1"},
        "partitions": {"fit": "train", "select": "tune"},
        "seeds": {"python": 1, "numpy": 1, "torch": 1, "dataloader_worker_base": 1},
        "determinism": {"torch_use_deterministic_algorithms": False,
                        "cudnn_benchmark": True, "cublas_workspace_config": "",
                        "tf32_allowed": True},
        "preprocessing": {"id": "prep.x", "version": 1,
                          "digest": "sha256:" + "aa" * 32, "output_kind": "label"},
    }
    document.update(overrides)
    return document


def test_a_well_formed_request_parses() -> None:
    request = contract.RunRequest.from_document(_request())
    assert request.backend_kind == "nnunet"
    assert (request.fit_partition, request.select_partition) == ("train", "tune")


def test_a_platform_and_a_trainer_that_disagree_about_the_exchange_refuse() -> None:
    """Not negotiate. A member one side ignores is a fit against inputs nobody sent."""
    with pytest.raises(contract.ContractViolation, match="contract_version"):
        contract.RunRequest.from_document(_request(contract_version="0"))


@pytest.mark.parametrize("partitions", [
    {"fit": "test", "select": "tune"},
    {"fit": "train", "select": "test"},
    {"fit": "train", "select": "excluded"},
    {"fit": "train", "select": "val"},
])
def test_the_trainer_refuses_a_request_naming_a_partition_it_may_not_read(
    partitions: dict[str, str],
) -> None:
    """`MOS-TRAIN-141`, checked on the trainer's side too.

    The platform's resolver already answers 403 on `test`. This is the second
    lock: a `request.json` that named `test` would mean the platform had a
    defect, and a trainer that trained on it anyway would make the 403
    decorative. `MOS-TRAIN-214` adds no fourth partition and `val` is not a
    value.
    """
    with pytest.raises(contract.ContractViolation, match="MOS-TRAIN-141"):
        contract.RunRequest.from_document(_request(partitions=partitions))


def test_one_partition_cannot_be_both_the_fit_and_the_selection() -> None:
    with pytest.raises(contract.ContractViolation, match="MOS-TRAIN-116"):
        contract.RunRequest.from_document(
            _request(partitions={"fit": "train", "select": "train"})
        )


# =====================================================================================
# 4. Acceptance check 15: a path, a prefix and a glob are not cohort arguments
# =====================================================================================
@pytest.fixture()
def run_directory(tmp_path: Path) -> contract.RunDirectory:
    (tmp_path / "images" / "case_a").mkdir(parents=True)
    (tmp_path / "images" / "case_a" / "image.nii.gz").write_bytes(b"not really a volume")
    (tmp_path / "cohort").mkdir()
    return contract.RunDirectory(tmp_path)


def test_a_staged_path_inside_images_resolves(run_directory: contract.RunDirectory) -> None:
    resolved = run_directory.staged_path("images/case_a/image.nii.gz", where="t")
    assert resolved.is_file()


@pytest.mark.parametrize("relative", [
    "/etc/passwd",
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "images/../../etc/passwd",
    "../images/case_a/image.nii.gz",
    "work/raw/case_a.nii.gz",
])
def test_the_trainer_refuses_a_volume_path_outside_the_staged_images(
    run_directory: contract.RunDirectory, relative: str
) -> None:
    """Chapter 17 acceptance check 15, and `MOS-TRAIN-141`'s reason for it.

    "a container that can name its own data can name the test partition". Note
    the third case: `images/../../etc/passwd` spells nothing forbidden and
    resolves somewhere it must not, which is why the check is on the RESOLVED
    path.
    """
    with pytest.raises(contract.ContractViolation):
        run_directory.staged_path(relative, where="t")


def test_a_case_the_platform_listed_and_did_not_stage_is_refused(
    run_directory: contract.RunDirectory,
) -> None:
    with pytest.raises(contract.ContractViolation, match="not staged"):
        run_directory.staged_path("images/case_b/image.nii.gz", where="t")


# =====================================================================================
# 5. The cohort files
# =====================================================================================
def _case(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "patient_key": "pk_" + "0" * 16,
        "case_key": "case_a",
        "study_instance_uid": "1.2.826.0.1.3680043.8.498.1",
        "series_instance_uid": "1.2.826.0.1.3680043.8.498.2",
        "partition": "train",
        "image": "images/case_a/image.nii.gz",
        "label": "images/case_a/label.nii.gz",
        "fold": 0,
    }
    document.update(overrides)
    return document


def test_a_cohort_line_in_the_test_partition_is_refused() -> None:
    with pytest.raises(contract.ContractViolation, match="MOS-TRAIN-141"):
        contract.CohortEntry.from_document(_case(partition="test"), where="t")


def test_an_empty_cohort_file_is_refused_rather_than_trained_on(
    run_directory: contract.RunDirectory,
) -> None:
    """A fit over zero cases returns the initialisation and records it as a model."""
    run_directory.path("cohort_fit").write_text("", encoding="utf-8")
    with pytest.raises(contract.ContractViolation, match="empty"):
        run_directory.cohort("cohort_fit")


def test_a_cohort_file_round_trips(run_directory: contract.RunDirectory) -> None:
    run_directory.path("cohort_fit").write_text(
        json.dumps(_case()) + "\n", encoding="utf-8"
    )
    cases = run_directory.cohort("cohort_fit")
    assert len(cases) == 1
    assert cases[0].case_key == "case_a"
    assert cases[0].fold == 0


# =====================================================================================
# 6. Every exit carries a reason (MOS-REL-051)
# =====================================================================================
def test_a_failure_document_must_name_a_reason() -> None:
    with pytest.raises(ValueError, match="MOS-REL-051"):
        contract.failure_document("fit", reason="")


def test_a_failure_document_carries_the_engines_own_refusals() -> None:
    document = contract.failure_document(
        "plan", reason="the exporter refused",
        refusals=[{"check_id": "MOS-TRAIN-223", "code": "fingerprint_field_not_mapped"}],
    )
    assert document["status"] == "FAILED"
    assert document["refusals"][0]["check_id"] == "MOS-TRAIN-223"


def test_a_success_document_names_the_phase_it_describes() -> None:
    document = contract.success_document("plan", fingerprint_digest="sha256:" + "0" * 64)
    assert document["status"] == "SUCCEEDED" and document["phase"] == "plan"


# ======================================================================================
# The supervision map travels WITH the cohort, or it does not travel.
#
# The map's WRITER was the trainer's staging code, and it went with the
# backend the vanilla stack replaced; what remains testable here is the
# platform's own reader: how `CohortEntry` parses the `supervises` member.
# ======================================================================================


def _supervision_line(key: str, supervises=...) -> dict:
    """One cohort line. `supervises=...` means the member is absent, not empty."""
    line = {
        "patient_key": f"p-{key}",
        "case_key": key,
        "study_instance_uid": "1.2.3",
        "series_instance_uid": "1.2.4",
        "partition": "train",
        "image": f"images/{key}/image.nii.gz",
        "label": f"images/{key}/label.nii.gz",
    }
    if supervises is not ...:
        line["supervises"] = supervises
    return line


def test_a_cohort_line_carries_the_channels_the_case_annotates() -> None:
    """The member exists and parses: a case names the channels it annotates."""
    entry = contract.CohortEntry.from_document(
        _supervision_line("c1", ["neo", "effusion"]), where="cohort/fit.jsonl:1")
    assert entry.supervises == ("neo", "effusion"), entry.supervises


def test_a_cohort_that_says_nothing_about_supervision_is_not_the_same_as_none() -> None:
    """`None` and `()` are different answers and the difference decides the run.

    `None` is "this cohort does not record supervision". `()` is "this case
    annotates nothing", which a cohort may legally contain. Collapsing the two
    would turn a missing record into a positive claim about the data.
    """
    absent = contract.CohortEntry.from_document(_supervision_line("c1"), where="w")
    empty = contract.CohortEntry.from_document(_supervision_line("c2", []), where="w")
    assert absent.supervises is None, absent.supervises
    assert empty.supervises == (), empty.supervises


def test_a_scalar_supervises_is_refused() -> None:
    """A string is iterable, so `"neo"` would silently become ('n','e','o')."""
    with pytest.raises(contract.ContractViolation, match="supervises"):
        contract.CohortEntry.from_document(_supervision_line("c1", "neo"), where="w")


# =====================================================================================
# 7. The mask has a switch, and the switch is load-bearing
#
# `empty_segment_is_negative` exists so a run can name its own semantics in
# the request. The request READER is platform surface and stays tested here;
# the map WRITER was the trainer's and went with the backend it targeted.
# =====================================================================================
def _supervision_request(**extra) -> dict:
    document = {
        "contract_version": contract.CONTRACT_VERSION,
        "training_run_id": "t-1", "capability_id": "c",
        "training_backend": {"kind": "nnunet", "version": "2"},
        "partitions": {"fit": "train", "select": "tune"},
        "seeds": {}, "determinism": {}, "preprocessing": {},
    }
    document.update(extra)
    return document


def test_a_request_that_says_nothing_about_supervision_is_masked() -> None:
    """THE DEFAULT IS THE SAFE ONE. The dangerous arm must be asked for in writing:
    a run that trains unannotated findings as background converges, writes a
    checkpoint and scores well on each corpus's own split, because that split
    carries the same blind spot as its training data."""
    request = contract.RunRequest.from_document(_supervision_request())
    assert request.empty_segment_is_negative is False


def test_the_control_arm_is_selected_by_the_request_and_only_by_it() -> None:
    request = contract.RunRequest.from_document(
        _supervision_request(supervision={"empty_segment_is_negative": True}))
    assert request.empty_segment_is_negative is True


def test_a_string_that_merely_looks_false_is_refused_rather_than_coerced() -> None:
    """`bool("false")` is True. A request carrying the JSON string "false" would select
    the UNMASKED arm while reading, to anyone scanning the file, as the masked one."""
    with pytest.raises(contract.ContractViolation, match="JSON boolean"):
        contract.RunRequest.from_document(
            _supervision_request(supervision={"empty_segment_is_negative": "false"}))


def test_a_supervision_member_this_image_does_not_implement_is_refused() -> None:
    """A member the reader ignores is a setting the platform believes it applied."""
    with pytest.raises(contract.ContractViolation, match="does not implement"):
        contract.RunRequest.from_document(
            _supervision_request(
                supervision={"empty_segment_is_negative": False, "weight_by": "density"}
            )
        )


# =====================================================================================
# 8. A phase's record survives the next phase
#
# `result.json` / `result-plan.json` / `result-fit.json` are the platform's
# own vocabulary for the run record; the WRITER went with the trainer's
# phases, and the roles stay asserted here.
# =====================================================================================
def test_each_phase_has_a_result_file_only_it_writes() -> None:
    for role in ("result_plan", "result_fit"):
        assert role in contract.RUN_DIRECTORY, role
        assert role in orch.RUN_DIRECTORY, role
    # And they are distinct paths, or the second phase overwrites the first again.
    paths = {contract.RUN_DIRECTORY[r] for r in ("result", "result_plan", "result_fit")}
    assert len(paths) == 3, paths
