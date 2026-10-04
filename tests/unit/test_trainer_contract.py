# SPDX-License-Identifier: Apache-2.0
"""The exchange between the platform and `trainer/`, asserted from both sides.

WHAT THIS MODULE IS FOR
------------------------
`medos/medos/training/orchestrator.py` and `medos.sdk/contract.py` each
spell the run directory. They have to: the platform writes it and the trainer reads it,
and neither image imports the other's half at run time -- that separation is the whole
reason `medicalos/medos` carries no torch. A constant written down twice with no test
between the two copies is not one constant, and the failure mode is the quiet one: the
platform stages `cohort/fit.jsonl` and the trainer looks for `cohort/train.jsonl`, finds
nothing, and refuses a cohort that was staged correctly.

So this module imports BOTH and compares them. It needs no torch, no GPU and no
container: `medos.sdk.contract` is pure Python over one directory, which is a
deliberate property of that module and is what makes this a unit test.

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
from medos.sdk.fixtures import MOS_TRAIN_058_DOCUMENT as _FULL_SPEC  # noqa: E402
from medos.training import orchestrator as orch  # noqa: E402


# =====================================================================================
# 1. The two halves of the exchange are one exchange
# =====================================================================================
def test_the_platform_and_the_trainer_name_the_same_run_directory() -> None:
    assert orch.RUN_DIRECTORY == contract.RUN_DIRECTORY, (
        "medos/medos/training/orchestrator.py and medos.sdk/contract.py "
        "disagree about the run directory. The platform writes it and the trainer reads "
        "it; a path spelled two ways is a cohort that was staged and not found"
    )


def test_every_role_the_trainer_writes_is_a_role_the_platform_reads() -> None:
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

    The platform's resolver already answers 403 on `test`. This is the second lock: a
    `request.json` that named `test` would mean the platform had a defect, and a trainer
    that trained on it anyway would make the 403 decorative. `MOS-TRAIN-214` adds no
    fourth partition and `val` is not a value.
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

    "a container that can name its own data can name the test partition". Note the third
    case: `images/../../etc/passwd` spells nothing forbidden and resolves somewhere it
    must not, which is why the check is on the RESOLVED path.
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
# The fit path must be the MASKED path. Read from the source, because it cannot be run.
# ======================================================================================


def _fit_function_source() -> str:
    """`backend.fit`'s source, without importing it.

    `backend.py` imports torch and nnunetv2 at call time and binds nnU-Net's three roots
    from the environment at import time (`_bind_roots`). A unit test declares no
    infrastructure, so this reads the file.
    """
    import ast

    source = (TRAINER_ROOT / "medos_trainer" / "backend.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "fit":
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("backend.py declares no `fit`")


def test_the_fit_constructs_the_masked_trainer_and_not_the_stock_one() -> None:
    """THE DEFECT THIS EXISTS FOR, and it shipped.

    `fit` imported `nnunetv2...nnUNetTrainer` and constructed it. The subclass that holds
    the per-(case, channel) mask -- `nnUNetTrainerMaskedChannels`, 345 lines -- was
    reachable from one test and from nothing on the fitting path, and so was `masked.py`
    beneath it, because only the subclass calls it.

    Nothing raised. A fit built on the stock trainer runs to completion, writes a
    checkpoint and reports a falling loss; what it learns is that every channel a case
    does not annotate is background. On the cohort this was found with, 762 of 900
    (case, channel) pairs would have been taught as negatives.

    A test that instantiates the trainer cannot be a unit test -- it needs torch, CUDA and
    a preprocessed dataset -- so this asserts the WIRING, which is the part that was wrong.
    """
    import ast

    body = _fit_function_source()
    tree = ast.parse(body)

    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "nnUNetTrainerMaskedChannels" in constructed, (
        "backend.fit does not construct nnUNetTrainerMaskedChannels. The masked loss is "
        f"then unreachable and the fit trains every unannotated channel as background. "
        f"It constructs: {sorted(constructed)}"
    )
    assert "nnUNetTrainer" not in constructed, (
        "backend.fit constructs nnU-Net's stock trainer. It has no mask, and a fit built "
        "on it converges, writes a checkpoint and reports a falling loss while learning "
        "to suppress every finding nobody annotated."
    )


def test_the_fit_does_not_import_the_stock_trainer_at_all() -> None:
    """The import is the other half. Kept separate so the failure names which half broke.

    A `from nnunetv2...nnUNetTrainer import nnUNetTrainer` inside `fit` is either dead or
    a second construction site waiting to be used; both are the thing above, one edit away.
    """
    import ast

    tree = ast.parse(_fit_function_source())
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("nnunetv2")
        for alias in node.names
    }
    assert "nnUNetTrainer" not in imported, (
        "backend.fit imports nnU-Net's stock trainer from "
        f"nnunetv2; it imports {sorted(imported)}"
    )


# ======================================================================================
# The dataset.json the backend BUILDS must be the one the masked trainer can start on.
# ======================================================================================


def _spec_with(*names: str) -> dict:
    """A spec document carrying nothing but the label set this test needs."""
    label_set = [{"name": "background", "value": 0}]
    label_set += [{"name": n, "value": i} for i, n in enumerate(names, start=1)]
    return {"io": {"label_set": label_set, "output_kind": "label"}}


def test_the_built_label_set_is_regions_and_not_mutually_exclusive_classes() -> None:
    """A partially labelled cohort cannot be a softmax, and the shape is what says so.

    `{"neo": 1}` is a class in a mutually exclusive set. `{"neo": [1]}` is a region of one
    label and gets its own sigmoid head. This cohort annotates one finding per case and
    says NOTHING about the others, so mutual exclusivity is a claim the data does not
    make. `masked_trainer.force_region_mode` holds nnU-Net to the second reading and can
    only do so if the label set arrives shaped as regions.
    """
    from medos_trainer import backend

    labels = backend._labels_from_spec(_spec_with("neo", "effusion"))
    assert labels["background"] == 0, labels
    assert labels["neo"] == [1], f"neo must be a singleton region, got {labels['neo']!r}"
    assert labels["effusion"] == [2], labels


def test_the_built_dataset_json_declares_regions_class_order() -> None:
    """THE DEFECT THIS EXISTS FOR. It stopped the first real fit at construction.

    `force_region_mode` sets `_has_regions` and calls `_get_regions()`, which asserts
    `regions_class_order is not None`. The backend built a dataset.json with neither
    regions nor an order, and the masked trainer died inside nnU-Net's LabelManager. It
    had never been seen because `fit` constructed the stock trainer, which forces nothing
    and needs neither.
    """
    from medos_trainer import backend

    document = backend._dataset_json(
        [], labels=backend._labels_from_spec(_spec_with("neo", "effusion", "pneumonia"))
    )
    assert "regions_class_order" in document, sorted(document)
    assert document["regions_class_order"] == [1, 2, 3], document["regions_class_order"]


def test_regions_class_order_is_the_label_values_ascending() -> None:
    """The order decides which channel wins a voxel when several sigmoid heads fire.

    It must come from the registered spec's own values, not from dict insertion order and
    not from nnU-Net: a model whose channel meanings came from a document nobody
    registered is a model whose outputs are not described by its signed artifact
    (`MOS-TRAIN-136`).
    """
    from medos_trainer import backend

    spec = {"io": {"label_set": [
        {"name": "third", "value": 3},
        {"name": "background", "value": 0},
        {"name": "first", "value": 1},
        {"name": "second", "value": 2},
    ]}}
    document = backend._dataset_json([], labels=backend._labels_from_spec(spec))
    assert document["regions_class_order"] == [1, 2, 3], document["regions_class_order"]


# ======================================================================================
# The supervision map travels WITH the cohort, or it does not travel.
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
    """Before this member existed the map could only arrive out of band.

    `nnUNetTrainerMaskedChannels` reads `supervision.json` from `$nnUNet_raw/<dataset>/`
    and refuses without it. That directory is built by `stage_dataset` FROM THE COHORT,
    and the only writer of the map was `medos/tools/ingest/nnunet_dataset.py`, which drops it
    beside a dataset it built itself, elsewhere. The two directories never met, so the
    masked path could not run at all.
    """
    entry = contract.CohortEntry.from_document(
        _supervision_line("c1", ["neo", "effusion"]), where="cohort/fit.jsonl:1")
    assert entry.supervises == ("neo", "effusion"), entry.supervises


def test_a_cohort_that_says_nothing_about_supervision_is_not_the_same_as_none() -> None:
    """`None` and `()` are different answers and the difference decides the run.

    `None` is "this cohort does not record supervision", and `stage_dataset` then writes
    no map so the trainer refuses outright. `()` is "this case annotates nothing", which
    a cohort may legally contain: it contributes to no channel's loss. Collapsing the two
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


def _entries(*pairs) -> list:
    return [
        contract.CohortEntry.from_document(_supervision_line(k, s), where="w")
        for k, s in pairs
    ]


def test_stage_dataset_writes_the_map_from_the_cohort(tmp_path) -> None:
    """The map is a projection of the sealed cohort, not a file a side tool dropped."""
    import json as _json

    from medos_trainer import backend

    labels = {"background": 0, "neo": [1], "effusion": [2]}
    backend._write_supervision(
        tmp_path, _entries(("c1", ["neo"]), ("c2", ["effusion", "neo"])), labels=labels)

    document = _json.loads((tmp_path / "supervision.json").read_text(encoding="utf-8"))
    assert document["channels"] == ["neo", "effusion"], document["channels"]
    assert document["cases"] == {"c1": ["neo"], "c2": ["effusion", "neo"]}, document["cases"]
    assert document["empty_segment_is_negative"] is False, document


def test_a_cohort_that_declares_supervision_for_only_some_cases_is_refused(tmp_path) -> None:
    """THE HALF-MAP, refused before a GPU is reserved.

    A case with no entry reaches a batch and is either rejected by the trainer mid-epoch
    or -- if somebody 'fixes' that with a default -- supervised on every channel, which is
    the false-negative signal the subsystem exists to remove. Both counts go in the
    message so the reader knows how far off the cohort is.
    """
    from medos_trainer import backend

    with pytest.raises(backend.ContractViolation, match="1 of 2"):
        backend._write_supervision(
            tmp_path, _entries(("c1", ["neo"]), ("c2", ...)),
            labels={"background": 0, "neo": [1]})


def test_a_cohort_supervising_a_channel_the_spec_does_not_declare_is_refused(tmp_path) -> None:
    """Supervision applied to an output head that does not exist.

    The label set comes from the registered PreprocessingSpec (`MOS-TRAIN-136`). A channel
    named only in the cohort would mask against a head the network was never built with.
    """
    from medos_trainer import backend

    with pytest.raises(backend.ContractViolation, match="does not declare"):
        backend._write_supervision(
            tmp_path, _entries(("c1", ["ghost"])),
            labels={"background": 0, "neo": [1]})


def test_a_cohort_that_declares_nothing_writes_no_map(tmp_path) -> None:
    """Deliberately silent here, so the trainer's own refusal is the message read.

    It names the file, the directory it looked in, and what falling back would have cost.
    A refusal invented here would be a worse version of one already written.
    """
    from medos_trainer import backend

    backend._write_supervision(
        tmp_path, _entries(("c1", ...), ("c2", ...)), labels={"background": 0, "neo": [1]})
    assert not (tmp_path / "supervision.json").exists()


# =====================================================================================
# 6. The bound spec is parsed at the START of a phase
#
# `preprocessing.json` is the deployment's registered `PreprocessingSpec`. Until the
# parse moved, the FIRST thing to read it as a spec was
# `packaging.derive_spec_document`, which runs AFTER the fit -- so a template missing
# `backend`, `inverse` or `golden_fixture` was refused at the end of a run that had
# already spent its preprocessing and every epoch. That happened: a 2-epoch run reached
# "Training done." and died on `KeyError: 'backend'`.
#
# These two assert the ORDER, not the parser. `medos/medos/training/spec.py` has its own
# tests; what is new here is WHEN it runs, and the observable for that is whether the
# workspace was built before the refusal.
# =====================================================================================
def _run_directory(root: Path, spec: dict) -> Path:
    """The smallest run directory `_phase` will read: a request and a spec."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "request.json").write_text(json.dumps({
        "contract_version": contract.CONTRACT_VERSION,
        "training_run_id": "t-0001",
        "capability_id": "medos.unit",
        "training_backend": {"kind": "nnunet", "version": "2"},
        "partitions": {"fit": "train", "select": "tune"},
        "seeds": {"python": 1, "numpy": 1, "torch": 1, "dataloader_worker_base": 1},
        "determinism": {"torch_use_deterministic_algorithms": False,
                        "cudnn_benchmark": True, "tf32_allowed": True,
                        "cublas_workspace_config": ""},
        "preprocessing": {"id": "prep.unit", "version": "1.0.0"},
    }), encoding="utf-8")
    (root / "preprocessing.json").write_text(json.dumps(spec), encoding="utf-8")
    return root


def test_a_spec_that_cannot_parse_is_refused_before_the_workspace_is_built(tmp_path) -> None:
    """The half-hour defect, asserted as an ordering fact.

    `work/` is created by `backend.prepare_workspace`, the first thing after the spec
    check and the step every later one depends on. Its ABSENCE after a failed phase is
    the evidence that the refusal came first -- and it needs no monkeypatch, no torch and
    no GPU, which is what keeps this a unit test.
    """
    from medos_trainer import __main__ as entry

    incomplete = dict(_FULL_SPEC)
    incomplete.pop("backend")
    root = _run_directory(tmp_path / "run", incomplete)

    assert entry._phase("plan", str(root)) == 1
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert "backend" in result["reason"], result["reason"]
    assert not (root / "work").exists(), (
        "the workspace was built before the spec was checked, so the refusal that was "
        "available at second zero would still have arrived after the preprocessing"
    )


def test_the_registered_spec_of_a_real_deployment_passes_that_check(tmp_path) -> None:
    """The other direction, so the gate above is not merely refusing everything.

    `MOS_TRAIN_058_DOCUMENT` is chapter 17's own worked example, and it is what a
    registered spec looks like. If this ever fails, the check has become stricter than
    registration -- which would make the trainer refuse specs the platform accepts.
    """
    from medos_trainer import __main__ as entry

    root = _run_directory(tmp_path / "run", _FULL_SPEC)
    entry._parse_bound_spec(contract.RunDirectory(str(root)))


# =====================================================================================
# 7. The dataset's manifest describes the DATASET, not the call that last wrote it
#
# `derive_plan` stages twice -- fit, then select, with the fingerprint taken in between
# so `MOS-TRAIN-135`'s "derived from the FIT partition" is a fact about what was on disk
# and not a promise. Both `dataset.json` and `supervision.json` were being rewritten from
# the second call's own cases, so a 90-case dataset ended up with a manifest describing
# 20 and a supervision map missing every fit case.
# =====================================================================================
def _staging_run(root: Path, cases) -> contract.RunDirectory:
    """A run directory with real files behind every cohort line."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "cohort").mkdir(exist_ok=True)
    for case, _channels in cases:
        d = root / "images" / case
        d.mkdir(parents=True, exist_ok=True)
        (d / "image.nii.gz").write_bytes(b"\x1f\x8b0")
        (d / "label.nii.gz").write_bytes(b"\x1f\x8b0")
    return contract.RunDirectory(str(root))


def _cohort(root: Path, *pairs):
    out = []
    for case, channels in pairs:
        line = _supervision_line(case, channels)
        line["image"] = f"images/{case}/image.nii.gz"
        line["label"] = f"images/{case}/label.nii.gz"
        out.append(contract.CohortEntry.from_document(line, where="w"))
    return out


_SPEC_FOR_STAGING = {
    "io": {"label_set": [{"value": 0, "name": "background"},
                         {"value": 1, "name": "neo"},
                         {"value": 2, "name": "effusion"}]}
}


def test_the_second_staging_describes_the_whole_dataset_and_not_its_own_cases(tmp_path) -> None:
    """THE FATAL HALF. The masked trainer reads `supervision.json` for every case in a
    batch; a map holding only the select partition would have had no entry for 70 of 90.
    """
    import json as _json

    from medos_trainer import backend

    fit = _cohort(tmp_path, ("f1", ["neo"]), ("f2", ["effusion"]))
    select = _cohort(tmp_path, ("s1", ["neo"]))
    run = _staging_run(tmp_path, [("f1", 0), ("f2", 0), ("s1", 0)])
    raw = tmp_path / "raw"

    backend.stage_dataset(run, fit, raw_root=raw, spec_document=_SPEC_FOR_STAGING)
    backend.stage_dataset(run, select, raw_root=raw, spec_document=_SPEC_FOR_STAGING,
                          manifest=(*fit, *select))

    dataset = raw / backend.DATASET_NAME
    supervision = _json.loads((dataset / "supervision.json").read_text(encoding="utf-8"))
    assert sorted(supervision["cases"]) == ["f1", "f2", "s1"], supervision["cases"]
    manifest = _json.loads((dataset / "dataset.json").read_text(encoding="utf-8"))
    assert manifest["numTraining"] == 3, manifest["numTraining"]


def test_the_fit_partition_is_staged_into_a_directory_holding_nothing_else(tmp_path) -> None:
    """PHASE 1 OWNS THE DATASET DIRECTORY, because the ordering is the only control.

    A `work/` that survived a failed attempt still holds the select cases the previous
    attempt staged last. Stage the fit partition on top and the fingerprint is extracted
    over both -- `MOS-TRAIN-135` violated with nothing on disk to show it. nnU-Net's
    count check happens to catch the case where the numbers disagree; this asserts the
    property the check is a proxy for.
    """
    from medos_trainer import backend

    run = _staging_run(tmp_path, [("f1", 0), ("stale", 0)])
    raw = tmp_path / "raw"
    backend.stage_dataset(run, _cohort(tmp_path, ("stale", ["neo"])),
                          raw_root=raw, spec_document=_SPEC_FOR_STAGING)
    assert (raw / backend.DATASET_NAME / "imagesTr" / "stale_0000.nii.gz").exists()

    # What `derive_plan` does before it stages the fit partition.
    import shutil
    shutil.rmtree(raw / backend.DATASET_NAME, ignore_errors=True)
    backend.stage_dataset(run, _cohort(tmp_path, ("f1", ["neo"])),
                          raw_root=raw, spec_document=_SPEC_FOR_STAGING)

    staged = sorted(p.name for p in (raw / backend.DATASET_NAME / "imagesTr").iterdir())
    assert staged == ["f1_0000.nii.gz"], staged


def test_derive_plan_rebuilds_the_dataset_directory_rather_than_adding_to_it() -> None:
    """The wiring for the test above: `derive_plan` must actually perform that removal,
    and it must do so BEFORE it stages the fit partition."""
    import inspect

    from medos_trainer import backend

    source = inspect.getsource(backend.derive_plan)
    removal = source.index("shutil.rmtree")
    first_staging = source.index("stage_dataset(run, fit_cases")
    assert removal < first_staging, (
        "derive_plan stages the fit partition before clearing the dataset directory, so "
        "a leftover select partition joins the fingerprint"
    )


# =====================================================================================
# 8. The mask has a switch, and the switch is load-bearing
#
# `empty_segment_is_negative` existed in `supervision.json` from the start and reached
# exactly one place: a log line. `_mask_for` built the mask from `cases` regardless, so
# writing `true` produced a run whose own log announced the unmasked semantics while the
# loss went on masking. That is worse than having no field at all -- the record
# contradicted the computation.
#
# It is now the switch it claimed to be, which is what makes an unmasked CONTROL arm
# possible: the comparison the whole subsystem's claim rests on.
# =====================================================================================
def _request(**extra) -> dict:
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
    a run that trains unannotated findings as background converges, writes a checkpoint
    and scores well on each corpus's own split, because that split carries the same blind
    spot as its training data."""
    assert contract.RunRequest.from_document(_request()).empty_segment_is_negative is False


def test_the_control_arm_is_selected_by_the_request_and_only_by_it() -> None:
    request = contract.RunRequest.from_document(
        _request(supervision={"empty_segment_is_negative": True}))
    assert request.empty_segment_is_negative is True


def test_a_string_that_merely_looks_false_is_refused_rather_than_coerced(tmp_path) -> None:
    """`bool("false")` is True. A request carrying the JSON string "false" would select
    the UNMASKED arm while reading, to anyone scanning the file, as the masked one."""
    with pytest.raises(contract.ContractViolation, match="JSON boolean"):
        contract.RunRequest.from_document(
            _request(supervision={"empty_segment_is_negative": "false"}))


def test_a_supervision_member_this_image_does_not_implement_is_refused() -> None:
    """A member the trainer ignores is a setting the platform believes it applied."""
    with pytest.raises(contract.ContractViolation, match="does not implement"):
        contract.RunRequest.from_document(
            _request(supervision={"empty_segment_is_negative": False, "weight_by": "density"}))


def test_the_written_map_carries_the_mode_it_was_asked_for(tmp_path) -> None:
    """`stage_dataset` writes the map; the mode has to survive into it, because the fit
    phase reads the map and never sees the request."""
    import json as _json

    from medos_trainer import backend

    labels = {"background": 0, "neo": [1]}
    for asked in (False, True):
        target = tmp_path / str(asked)
        target.mkdir()
        backend._write_supervision(
            target, _entries(("c1", ["neo"])), labels=labels,
            empty_segment_is_negative=asked)
        written = _json.loads((target / "supervision.json").read_text(encoding="utf-8"))
        assert written["empty_segment_is_negative"] is asked, written


# =====================================================================================
# 9. A phase's record survives the next phase
#
# Both phases wrote `result.json`, and the fit overwrote the plan's. After a completed run
# the question "which image derived this plan" had no answer in the run directory -- the
# same question `produced_by` was added to stop inferring from a directory name. It came
# up for real: the plans had to be re-derived after an nnU-Net bump, and by then the only
# record of which image had made the previous ones was gone.
# =====================================================================================
def test_each_phase_has_a_result_file_only_it_writes() -> None:
    for role in ("result_plan", "result_fit"):
        assert role in contract.RUN_DIRECTORY, role
        assert role in orch.RUN_DIRECTORY, role
    # And they are distinct paths, or the second phase overwrites the first again.
    paths = {contract.RUN_DIRECTORY[r] for r in ("result", "result_plan", "result_fit")}
    assert len(paths) == 3, paths


def test_a_phase_writes_both_the_latest_and_its_own_record(tmp_path) -> None:
    """`result.json` keeps its meaning -- the latest phase, which the executor polls --
    and the per-phase file keeps the history. Both, from the one writer."""
    from medos_trainer import __main__ as entry

    root = _run_directory(tmp_path / "run", _FULL_SPEC)
    entry._write_result(contract.RunDirectory(str(root)),
                        contract.success_document(
                            "plan", fingerprint_digest="sha256:" + "a" * 64
                        ))

    latest = json.loads((root / "result.json").read_text(encoding="utf-8"))
    mine = json.loads((root / "result-plan.json").read_text(encoding="utf-8"))
    assert latest == mine, "the two files disagree about the same phase"
    assert latest["phase"] == "plan"
    assert "produced_by" in latest, "the stamp must be in both"


def test_the_fit_does_not_erase_the_plans_record(tmp_path) -> None:
    """THE DEFECT, AS A SEQUENCE. Write a plan result, then a fit result, then ask what
    image derived the plan. Before this change the answer was gone."""
    from medos_trainer import __main__ as entry

    root = _run_directory(tmp_path / "run", _FULL_SPEC)
    run = contract.RunDirectory(str(root))
    entry._write_result(
        run, contract.success_document("plan", plan_digest="sha256:" + "b" * 64)
    )
    entry._write_result(run, contract.success_document("fit", bundle={"path": "bundle"}))

    latest = json.loads((root / "result.json").read_text(encoding="utf-8"))
    assert latest["phase"] == "fit", "result.json must still be the latest phase"
    survived = json.loads((root / "result-plan.json").read_text(encoding="utf-8"))
    assert survived["phase"] == "plan", "the plan's own record was overwritten"
    assert survived["plan_digest"] == "sha256:" + "b" * 64


def test_a_failing_phase_records_itself_too(tmp_path) -> None:
    """A run that failed is when the record is read. `MOS-REL-051` makes the reason
    mandatory; this makes the reason SURVIVE a later phase's write."""
    from medos_trainer import __main__ as entry

    root = _run_directory(tmp_path / "run", _FULL_SPEC)
    run = contract.RunDirectory(str(root))
    entry._write_result(run, contract.failure_document("plan", reason="ContractViolation: x"))
    entry._write_result(run, contract.success_document("fit", bundle={"path": "bundle"}))

    failed = json.loads((root / "result-plan.json").read_text(encoding="utf-8"))
    assert failed["status"] == "FAILED" and "ContractViolation" in failed["reason"]
