# SPDX-License-Identifier: Apache-2.0
"""Every channel is measurable in every fold, or the split is refused.

WHAT WENT WRONG WITHOUT THIS
-----------------------------
nnU-Net writes `splits_final.json` with `sklearn.model_selection.KFold(shuffle=True)`.
Plain KFold, which knows nothing about labels. On a single-label dataset that is right,
because every case carries every class. On a PARTIALLY LABELLED one it is not, and the
ten-channel 90-case build shows what it costs:

    coronary_calcification    15 cases    14 train    1 val
    vertebral_body            15 cases    13 train    2 val
    lung_neoplasm              4 cases     2 train    2 val

ONE validation case for `coronary_calcification`. Its per-channel Dice was computed from
a single patient, and in most epochs the 100 validation patches drew nothing from it at
all -- so the channel read `nan`, the checkpoint metric could not see it, and 0.670
looked like a result while being an artefact of who landed in the fold.

Stratified on the supervision SIGNATURE -- the exact set of channels a case is annotated
for -- the same 90 cases give every 15-case channel exactly 3 validation cases in every
one of the five folds.

WHY SIGNATURE AND NOT CORPUS
-----------------------------
One corpus does not imply one signature. In the lung-nodule corpus some cases carry
`benign_nodule`, some `lung_neoplasm`, some both, and treating them as interchangeable is
how `lung_neoplasm` ended up with two. Grouping by the signature itself is what makes
the guarantee hold per channel rather than per folder.

Spec: MOS-EVID-034 (the split is an input to a run), MOS-TRAIN-141.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.ingest.stratified_split import (  # noqa: E402
    check,
    main,
    per_channel_counts,
    stratify,
)

#: The real shape of the 90-case build: five corpora of 15 with a single signature each,
#: and one corpus whose cases carry three different signatures. That last group is the
#: one plain KFold mishandles.
CHANNELS = [
    "aorta_arch", "aorta_ascending", "aorta_descending", "benign_nodule",
    "coronary_calcification", "lung_neoplasm", "pleural_effusion", "pneumonia",
    "pulmonary_trunk", "vertebral_body",
]


def _dataset() -> dict[str, list[str]]:
    cases: dict[str, list[str]] = {}
    aorta = ["aorta_arch", "aorta_ascending", "aorta_descending", "pulmonary_trunk"]
    for i in range(15):
        cases[f"aorta_{i}"] = list(aorta)
        cases[f"spine_{i}"] = ["vertebral_body"]
        cases[f"coronary_{i}"] = ["coronary_calcification"]
        cases[f"hydro_{i}"] = ["pleural_effusion"]
        cases[f"pneu_{i}"] = ["pneumonia"]
    for i in range(11):
        cases[f"nods_b_{i}"] = ["benign_nodule"]
    for i in range(3):
        cases[f"nods_both_{i}"] = ["benign_nodule", "lung_neoplasm"]
    cases["nods_n_0"] = ["lung_neoplasm"]
    return cases


def _write(tmp_path: Path, cases: dict[str, list[str]]) -> Path:
    raw = tmp_path / "Dataset999_Probe"
    raw.mkdir(parents=True)
    (raw / "supervision.json").write_text(
        json.dumps({"channels": CHANNELS, "cases": cases}), encoding="utf-8"
    )
    return raw


# --------------------------------------------------------------------------------------
# the guarantee
# --------------------------------------------------------------------------------------


def test_every_case_appears_in_exactly_one_validation_fold() -> None:
    cases = _dataset()
    splits = stratify(cases, folds=5)
    seen: list[str] = []
    for split in splits:
        seen += split["val"]
    assert sorted(seen) == sorted(cases), "a case is missing from, or in two, val folds"


def test_train_and_val_never_overlap() -> None:
    """The leakage this file would otherwise introduce is the worst kind: silent, and
    it inflates every number."""
    for split in stratify(_dataset(), folds=5):
        assert not (set(split["train"]) & set(split["val"]))
        assert len(split["train"]) + len(split["val"]) == len(_dataset())


def test_a_fifteen_case_channel_gets_three_validation_cases_in_every_fold() -> None:
    """THE DEFECT, fixed. Under plain KFold `coronary_calcification` had one."""
    cases = _dataset()
    splits = stratify(cases, folds=5)
    for channel in ("coronary_calcification", "vertebral_body", "pleural_effusion",
                    "pneumonia", "aorta_arch", "pulmonary_trunk"):
        per_fold = [per_channel_counts(s, CHANNELS, cases)[channel][1] for s in splits]
        assert per_fold == [3, 3, 3, 3, 3], f"{channel}: {per_fold}"


def test_a_channel_spread_across_signatures_is_still_balanced() -> None:
    """`benign_nodule` lives in two signatures -- alone, and with `lung_neoplasm`. A
    split that stratified by CORPUS would treat them as one pool and could put every
    both-channel case in one fold."""
    cases = _dataset()
    splits = stratify(cases, folds=5)
    per_fold = [per_channel_counts(s, CHANNELS, cases)["benign_nodule"][1] for s in splits]
    assert sum(per_fold) == 14
    assert max(per_fold) - min(per_fold) <= 1, f"unbalanced: {per_fold}"


def test_round_robin_beats_a_random_partition_on_the_smallest_channel() -> None:
    """With 4 cases and 5 folds a random partition can put all four in one fold, leaving
    four folds unable to measure the channel at all. Round-robin cannot: the worst fold
    gets 0 and no fold gets more than 1."""
    cases = _dataset()
    splits = stratify(cases, folds=5)
    per_fold = [per_channel_counts(s, CHANNELS, cases)["lung_neoplasm"][1] for s in splits]
    assert sorted(per_fold) == [0, 1, 1, 1, 1], per_fold


def test_the_split_is_deterministic() -> None:
    """A split is an input to a run (`MOS-TRAIN-141`). Two builds of the same dataset
    must produce the same folds, or a rerun is not a rerun."""
    cases = _dataset()
    assert stratify(cases, 5, seed=7) == stratify(cases, 5, seed=7)


def test_a_different_seed_gives_a_different_split() -> None:
    """Otherwise the seed is decoration and `test_the_split_is_deterministic` passes on
    a constant."""
    cases = _dataset()
    assert stratify(cases, 5, seed=7) != stratify(cases, 5, seed=8)


def test_the_split_does_not_depend_on_filename_order() -> None:
    """Without the shuffle inside each signature group, the folds would be a property of
    how the filesystem happened to list the corpus."""
    cases = _dataset()
    reversed_order = dict(reversed(list(cases.items())))
    assert stratify(cases, 5) == stratify(reversed_order, 5)


# --------------------------------------------------------------------------------------
# the refusal
# --------------------------------------------------------------------------------------


def test_check_names_a_channel_that_cannot_meet_the_floor() -> None:
    cases = _dataset()
    problems = check(stratify(cases, 5), CHANNELS, cases, minimum=3)
    joined = " ".join(problems)
    assert "lung_neoplasm" in joined and "4 case(s)" in joined
    assert "benign_nodule" in joined
    assert "coronary_calcification" not in joined, "a channel with 3 per fold is fine"


def test_the_tool_refuses_rather_than_writing_an_unmeasurable_split(tmp_path) -> None:
    raw = _write(tmp_path, _dataset())
    out = tmp_path / "pre"
    assert main(["--raw", str(raw), "--preprocessed", str(out), "--min-val-cases", "3"]) == 2
    assert not (out / "splits_final.json").exists(), (
        "it refused and wrote the file anyway; a refusal after the side effect is not "
        "a refusal"
    )


def test_a_dataset_that_can_meet_the_floor_is_written(tmp_path) -> None:
    """The other direction: if it refused everything, the check would be a wall rather
    than a guard."""
    cases = {f"c_{i}": ["only_channel"] for i in range(50)}
    raw = tmp_path / "Dataset998_Probe"
    raw.mkdir()
    (raw / "supervision.json").write_text(
        json.dumps({"channels": ["only_channel"], "cases": cases}), encoding="utf-8"
    )
    out = tmp_path / "pre"
    assert main(["--raw", str(raw), "--preprocessed", str(out), "--min-val-cases", "3"]) == 0
    written = json.loads((out / "splits_final.json").read_text(encoding="utf-8"))
    assert len(written) == 5
    assert all(len(s["val"]) == 10 for s in written)


def test_allow_unmeasurable_writes_and_says_so(tmp_path) -> None:
    """A dataset can genuinely be too small. The escape exists so the other nine
    channels are usable -- and it is a flag somebody has to type, not a default."""
    raw = _write(tmp_path, _dataset())
    out = tmp_path / "pre"
    code = main([
        "--raw", str(raw), "--preprocessed", str(out),
        "--min-val-cases", "3", "--allow-unmeasurable",
    ])
    assert code == 0
    assert (out / "splits_final.json").exists()


def test_a_missing_supervision_file_is_refused(tmp_path) -> None:
    """It stratifies on which channels each case carries. Without that file it has
    nothing to stratify on, and falling back to a random split would silently reinstate
    the defect."""
    empty = tmp_path / "Dataset997_Empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="supervision.json"):
        main(["--raw", str(empty)])


def test_nothing_is_written_without_an_output_directory(tmp_path) -> None:
    """A report-only run, so the counts can be inspected before committing to them."""
    cases = {f"c_{i}": ["only_channel"] for i in range(50)}
    raw = tmp_path / "Dataset996_Probe"
    raw.mkdir()
    (raw / "supervision.json").write_text(
        json.dumps({"channels": ["only_channel"], "cases": cases}), encoding="utf-8"
    )
    assert main(["--raw", str(raw)]) == 0
    assert not list(tmp_path.rglob("splits_final.json"))
