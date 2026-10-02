# SPDX-License-Identifier: Apache-2.0
"""Verbatim texts the specification requires to appear unaltered, and their one home.

WHY A MODULE FOR STRINGS
------------------------
Some statements in this platform are quoted requirements, not prose the code is free to
phrase. `MOS-TRAIN-165` is one: chapter 17's acceptance check 10 asserts its text
"appears verbatim in the rendered block", so a paraphrase anywhere is a defect and two
copies are a defect waiting to happen.

WHY HERE AND NOT ON EITHER SIDE THAT USES IT
--------------------------------------------
`EQUIVALENCE_DISCLAIMER` is produced by the conversion pipeline
(`medos/medos/training/conversion.py`) and rendered by the promotion dossier
(`medos/medos/promotion/dossier.py`). Neither can own it:

  * `medos/medos/promotion/` cannot import from `medos/medos/training/` once the
    training plane is a separate deployable -- that is the whole point of the split,
    and a core process that imports the training engine to read one string has not
    been split at all.
  * `medos/medos/training/` cannot import from `medos/medos/promotion/` either, and
    that direction is forbidden for a much older and better reason: `MOS-TRAIN-189`
    requires the training pipeline to be structurally incapable of promoting, and
    `tests/gate/test_no_auto_promote.py` check 1 asserts that the import closure of
    `medos.training` reaches neither `medos.promotion` nor `medos.evidence.deployment`.
    A convenience import of a disclaimer would open exactly the edge that check exists
    to keep closed.

So it lives in `medos/medos/core/`, which both planes already depend on and which depends on
neither. The dependency direction this establishes is the one the split needs: training
imports core, promotion imports core, and they do not import each other.

Spec: MOS-TRAIN-165, MOS-TRAIN-189.
"""

from __future__ import annotations

from typing import Final

__all__ = ["EQUIVALENCE_DISCLAIMER"]

#: `MOS-TRAIN-165`, verbatim. Chapter 17 acceptance check 10 asserts this exact text
#: appears in the rendered dossier block, so it is quoted and never paraphrased.
#:
#: What it is for: an E1/E2/E3 conversion pass compares a converted graph against its
#: source. It says the conversion preserved the computation. It says nothing whatever
#: about whether the model is clinically equivalent to anything, and the distance
#: between those two claims is the distance between an engineering check and a
#: regulatory one.
EQUIVALENCE_DISCLAIMER: Final[str] = (
    "An E1/E2/E3 pass MUST NOT be reported, summarised, or displayed as evidence of "
    "clinical equivalence. It is evidence that the conversion did not break the graph."
)
