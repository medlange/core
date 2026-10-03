#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Resume nnU-Net preprocessing instead of starting it over.

WHY THIS EXISTS
----------------
`DefaultPreprocessor.run` wipes its own output before it begins:

    output_directory = join(nnUNet_preprocessed, dataset_name, data_identifier)
    if isdir(output_directory):
        shutil.rmtree(output_directory)

so preprocessing is all-or-nothing. A run killed at case 298 of 585 leaves 298 finished
cases on disk that the next run deletes on its first line. That turned an out-of-memory
kill -- which is recoverable, the workers simply died -- into two hours of lost work, and
it will do so again: the cases are not the same size, the large ones cluster, and the peak
is what decides whether the cgroup holds.

The root cause is not the memory ceiling. It is that the expensive part of the pipeline
has no resume, so every failure costs everything already done.

WHAT IT CHANGES, AND WHAT IT DOES NOT
--------------------------------------
Two seams, both in this process only, neither touching the installed package:

  1. The wipe is skipped ONCE -- the single `shutil.rmtree` that `run()` performs on its
     own output. Every later call delegates to the real one, so nothing else that wants to
     delete a directory is affected.
  2. The work list drops cases already finished.

Nothing about how a case is preprocessed changes. A case this script skips was written by
nnU-Net itself, by the same code path, from the same plans file.

THE COMPLETION MARKER IS THE .pkl, NOT THE .npz
-------------------------------------------------
`run_case_save` writes them in that order:

    np.savez_compressed(output_filename_truncated + '.npz', data=data, seg=seg)
    write_pickle(properties, output_filename_truncated + '.pkl')

so a case interrupted mid-write leaves an `.npz` with no `.pkl`. Keying the resume on the
`.pkl` therefore redoes exactly those cases and no others. Keying it on the `.npz` would
silently accept a truncated array as finished, which is the failure this whole script is
supposed to make less likely rather than more.

USE
----
Same arguments as `nnUNetv2_preprocess`. Lower `-np` if the previous run was killed for
memory; the peak scales with the number of workers holding a resampled volume at once.

    python resume_preprocess.py -d 512 -plans_name nnUNetResEncUNetMPlans -c 3d_fullres -np 3

The plans file must already exist: this resumes preprocessing, it does not plan. Run
`nnUNetv2_plan_experiment` first for a dataset that has never been planned.

Set `MEDOS_RESUME_DRY_RUN=1` to exercise both seams and stop before any worker starts. It
reports what it would keep and what it would redo and exits 0. Run it that way first with
the preprocessed volume mounted READ-ONLY: this script's whole job is to not delete work,
and a read-only mount is the only check on that claim that does not rely on reading it.
"""

from __future__ import annotations

import os
import shutil
import sys
from os.path import isdir, isfile, join

import nnunetv2.preprocessing.preprocessors.default_preprocessor as dp
# `preprocess_entry`, NOT `plan_and_preprocess_entry`. The combined entrypoint re-extracts
# the fingerprint and re-runs the planner, which rewrites the plans file the finished cases
# were preprocessed under. nnU-Net's planner is deterministic given the same fingerprint, so
# it would very probably write the same plans back -- but "very probably" is not a property
# worth resting 298 cases on, and resuming across two plans would mix two preprocessings in
# one directory with nothing on disk recording that it happened. The preprocess-only
# entrypoint takes `-plans_name` and reads the plans already there.
from nnunetv2.experiment_planning.plan_and_preprocess_entrypoints import preprocess_entry

#: Set by the skipped wipe, read by the work-list filter. `run()` computes the output
#: directory and immediately tries to delete it, so intercepting the delete is how this
#: script learns the path without re-deriving it from the plans file and risking a
#: disagreement with the library about where the output lives.
_state: dict[str, str] = {}

_real_rmtree = shutil.rmtree


def _rmtree_keeping_the_first(path, *args, **kwargs):
    """Skip exactly one deletion -- `run()`'s wipe of its own output -- and no others."""
    if "output" not in _state:
        _state["output"] = str(path)
        print(f"[resume] keeping {path} instead of wiping it", flush=True)
        return None
    return _real_rmtree(path, *args, **kwargs)


_real_dataset_of = dp.get_filenames_of_train_images_and_targets


def _dataset_without_finished_cases(raw_folder, dataset_json):
    dataset = _real_dataset_of(raw_folder, dataset_json)

    output = _state.get("output")
    if not output or not isdir(output):
        print("[resume] nothing preprocessed yet; running the full set", flush=True)
        return dataset

    remaining = {}
    finished = 0
    partial = []
    for key, value in dataset.items():
        if isfile(join(output, key + ".pkl")):
            finished += 1
            continue
        # An .npz without its .pkl was interrupted between the two writes. It is redone,
        # and named, because a silent redo looks identical to a case that was never
        # started and the difference matters when a run keeps dying in the same place.
        if isfile(join(output, key + ".npz")):
            partial.append(key)
        remaining[key] = value

    print(
        f"[resume] {finished} case(s) already finished, {len(remaining)} to do",
        flush=True,
    )
    if partial:
        shown = ", ".join(partial[:5])
        more = f" (+{len(partial) - 5} more)" if len(partial) > 5 else ""
        print(
            f"[resume] {len(partial)} case(s) have an .npz with no .pkl and are being "
            f"redone: {shown}{more}",
            flush=True,
        )
    if os.environ.get("MEDOS_RESUME_DRY_RUN"):
        print("[resume] dry run: stopping before any case is preprocessed", flush=True)
        raise SystemExit(0)

    return remaining


def main() -> int:
    shutil.rmtree = _rmtree_keeping_the_first
    dp.shutil.rmtree = _rmtree_keeping_the_first
    dp.get_filenames_of_train_images_and_targets = _dataset_without_finished_cases
    try:
        preprocess_entry()
    finally:
        shutil.rmtree = _real_rmtree
        dp.shutil.rmtree = _real_rmtree
        dp.get_filenames_of_train_images_and_targets = _real_dataset_of
    return 0


if __name__ == "__main__":
    sys.exit(main())
