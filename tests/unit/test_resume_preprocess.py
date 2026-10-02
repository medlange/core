# SPDX-License-Identifier: Apache-2.0
"""The resume driver's two safety properties, read from its source.

WHY STRUCTURAL
---------------
`medos/tools/trainer/resume_preprocess.py` imports `nnunetv2`, which is installed in the trainer
image and not in this environment, so importing it here would be an infrastructure skip
that asserts nothing. The two properties worth gating are readable from the source, and
both are properties about what the script MUST NOT do — which is the kind a structural
test can actually hold.

The script exists because `DefaultPreprocessor.run` wipes its own output before it starts,
so a run killed at case 298 of 585 loses all 298. That happened, twice: an out-of-memory
kill at 298/585 cost two hours, and a second kill during the resume attempt cost nothing
at all, because by then this script was doing the skipping. The second kill is the
evidence that the first one did not have to be expensive.

Spec: MOS-TRAIN-190 (the training pipeline is the evidence plane's producer).
"""

from __future__ import annotations

import re
from pathlib import Path

DRIVER = (
    Path(__file__).resolve().parents[2]
    / "medos"
    / "tools"
    / "trainer"
    / "resume_preprocess.py"
)


def _source() -> str:
    return DRIVER.read_text(encoding="utf-8")


def _code() -> str:
    """Source with the module docstring removed, so prose is not mistaken for code."""
    text = _source()
    opened = text.index('"""')
    closed = text.index('"""', opened + 3) + 3
    return text[:opened] + text[closed:]


def test_the_driver_exists_where_the_run_command_points_at_it() -> None:
    assert DRIVER.is_file(), f"{DRIVER} is missing; the documented resume command mounts it"


def test_completion_is_keyed_on_the_pkl_and_never_on_the_npz() -> None:
    """`run_case_save` writes the .npz first and the .pkl second.

        np.savez_compressed(output_filename_truncated + '.npz', data=data, seg=seg)
        write_pickle(properties, output_filename_truncated + '.pkl')

    So a case interrupted between the two writes leaves an .npz with no .pkl. Treating the
    .npz as the marker would accept a truncated array as finished and feed it to training —
    turning a crash that cost time into a crash that costs correctness, silently. The .pkl
    is the only marker whose presence means the pair was completed.
    """
    code = _code()
    skip = re.search(
        r'if isfile\(join\(output, key \+ "(\.\w+)"\)\):\s*\n\s*finished \+= 1', code
    )
    assert skip, "the driver's completion check is not in the expected shape; re-read it"
    assert skip.group(1) == ".pkl", (
        f"completion is keyed on {skip.group(1)!r}. The .npz is written first, so it exists "
        "for cases that were interrupted mid-write and are NOT finished."
    )


def test_the_driver_resumes_preprocessing_and_never_re_plans() -> None:
    """Re-planning would rewrite the plans the finished cases were preprocessed under.

    nnU-Net's planner is deterministic given the same fingerprint, so it would very
    probably write the same plans back — but resuming across two plans would mix two
    preprocessings in one directory with nothing on disk recording it, and "very probably"
    is not a property worth resting several hundred finished cases on.
    """
    code = _code()
    assert "preprocess_entry" in code, "the driver does not call the preprocess-only entrypoint"
    assert "plan_and_preprocess_entry()" not in code, (
        "the driver calls the combined entrypoint, which re-extracts the fingerprint and "
        "re-runs the planner over a directory holding cases built under the old plans"
    )


def test_only_one_deletion_is_skipped_and_the_rest_are_delegated() -> None:
    """A blanket no-op `rmtree` would silently disable every deletion in the process.

    The script suppresses exactly the one wipe `run()` performs on its own output; anything
    else that asks to delete a directory still deletes it. A resume tool that quietly
    stopped all cleanup would leak whatever the library expects to be able to remove.
    """
    code = _code()
    assert "_real_rmtree(path, *args, **kwargs)" in code, (
        "the patched rmtree never delegates to the real one, so it suppresses every "
        "deletion in the process rather than the single wipe it is aimed at"
    )


def test_a_dry_run_exists_and_stops_before_any_case_is_written() -> None:
    """The claim "this does not delete your work" is worth being able to test cheaply.

    Run with the preprocessed volume mounted read-only, the dry run exercises both seams —
    the skipped wipe and the filtered work list — and exits before a worker starts. A
    read-only mount is the only check on the no-delete claim that does not rely on reading
    the code and believing it.
    """
    code = _code()
    assert "MEDOS_RESUME_DRY_RUN" in code, "the driver has no dry run"
    assert "raise SystemExit(0)" in code, "the dry run does not stop before preprocessing"
