# SPDX-License-Identifier: Apache-2.0
"""DICOM output writing (CONTRACT.md section 1: `writer/`).

`identity.py` allocates every generated UID and SeriesNumber deterministically and holds
the MOS-IMG-090/095 attribute inheritance; `seg.py` writes the SEG; `sr.py` writes the
TID 1500 Comprehensive 3D SR.

The split is MOS-IMG-076/077's: identity is validated and allocated BEFORE any object is
built, so a deployment missing a legal manufacturer name fails with nothing written rather
than with a SEG in the PACS carrying a placeholder.
"""

from medos.writer.identity import (
    OutputPlan,
    SegmentPlan,
    build_job_identity,
    plan_outputs,
    preprocessing_spec_digest,
)
from medos.writer.seg import build_seg
from medos.writer.sr import build_sr

__all__ = [
    "OutputPlan",
    "SegmentPlan",
    "build_job_identity",
    "plan_outputs",
    "preprocessing_spec_digest",
    "build_seg",
    "build_sr",
]
