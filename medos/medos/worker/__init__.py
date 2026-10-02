# SPDX-License-Identifier: Apache-2.0
"""The job runner (CONTRACT.md section 1: `worker/`).

`steps.py` is the typed in-process step executor -- chapter 5's eight reserved step keys,
run in order, in ONE process, with no orchestration engine between them
(`MOS-REL-036` forbids batch orchestration on the inference path).
`runner.py` is the claim loop that leases a job, heartbeats it, hands it to the executor
and writes the terminal transition.

Two further modules, split out of `steps.py` and REPORTED as contract additions because
CONTRACT.md section 1 lists only the two above:

`selection.py`    chapter 3's series-selection decision (`job_series` rows).
`result_rows.py`  `ResultBundle` -> database rows. Pure; writes nothing.
"""

from medos.worker.runner import RunnerConfig, RunOutcome, WorkerRunner
from medos.worker.selection import SELECTOR_NAME, select_ct_series
from medos.worker.steps import (
    STEP_KEYS,
    PipelineState,
    StepContext,
    WorkerDeps,
    execute_plan,
)

__all__ = [
    "WorkerRunner",
    "RunnerConfig",
    "RunOutcome",
    "WorkerDeps",
    "StepContext",
    "PipelineState",
    "execute_plan",
    "STEP_KEYS",
    "select_ct_series",
    "SELECTOR_NAME",
]
