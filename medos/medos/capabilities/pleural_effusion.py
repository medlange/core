# SPDX-License-Identifier: Apache-2.0
"""`pleural_effusion` -- a declared placeholder. It measures nothing and says so.

CONTRACT.md §7: "**placeholder -- returns `present=False` with a `not_implemented`
note**". The table's own note gives the reason: "real model needs the clinic data, which
is PHI-bearing and out of scope here".

WHY A PLACEHOLDER IS THE RIGHT OUTPUT AND A HEURISTIC IS NOT
    A plausible-looking fake number is the worst output this repository can produce. An
    effusion detector built from, say, a dependent fluid-density threshold would return
    values for every case, would be wrong in a way nobody can see from the number, and
    would be indistinguishable in the database, the SR and the OHIF panel from a validated
    result. Every downstream consumer -- the SR writer, the provenance panel, a future
    evaluation run -- would treat it as a measurement. `present=False, score=None` with an
    unresolvable finding kind cannot be mistaken for one.

WHY IT RUNS AT ALL INSTEAD OF REJECTING
    `applicable()` returns None, so the job reaches `RUNNING` and produces a `Result`.
    Rejecting instead would be a CLINICAL statement -- chapter 5 makes `REJECTED` mean
    "this study is outside the applicability envelope" -- and this capability's silence is
    about MedicalOS, not about the study. MOS-SVC-011 points the same way: "a capability a
    service declares but does not produce an output for ... MUST be reported as a
    per-capability rejection, never as silence." So: an output, explicitly empty.

HOW THE "not_implemented" NOTE IS CARRIED
    `CONTRACT.md` §5 owns the `Finding` field list and it has no note field, so the note
    rides in the one free-text member: `kind = "pleural_effusion.not_implemented"`. That
    is deliberate rather than a workaround:

      * A consumer matching `kind == "pleural_effusion"` does NOT match it. A placeholder
        that answered to the real name would be read as a confident negative.
      * The string is not a key of `segment_profiles` and not a concept key, so any writer
        that tries to resolve it through `ConceptDictionary` RAISES (MOS-IMG-112). The
        placeholder is structurally incapable of producing a coded clinical assertion.
      * `NOT_IMPLEMENTED_NOTE` and `METADATA` carry the prose for the surfaces that have
        room for it (MOS-SAFE-012's result panel, the provenance panel).

    `score` is `None`, not `0.0`. MOS-SVC-020 makes `score_threshold` the name of a
    calibrated operating point; `0.0` would assert a calibrated confidence of zero, which
    is a different and false claim from "no score exists".

Spec: MOS-SVC-011, MOS-SVC-020, MOS-IMG-112, MOS-IMG-121, MOS-SAFE-012, MOS-SAFE-014,
MOS-SAFE-015, MOS-EXEC-001, CONTRACT.md §5, §7.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from medos.capabilities.base import (
    CapabilityContext,
    CapabilityMetadata,
    FailureMode,
    require_source_grid,
)
from medos.core.bundle import CapabilityOutcome, Finding
from medos.core.geometry import CanonicalVolume

__all__ = [
    "PleuralEffusion",
    "METADATA",
    "CAPABILITY_ID",
    "VERSION",
    "FINDING_KIND",
    "NOT_IMPLEMENTED_NOTE",
]

CAPABILITY_ID = "pleural_effusion"
VERSION = "0.0.0"  # 0.0.0, not 0.1.0: there is no implementation to version.

# The finding kind. `.not_implemented` is part of the identifier, not a suffix a consumer
# may strip.
FINDING_KIND = "pleural_effusion.not_implemented"

NOT_IMPLEMENTED_NOTE = (
    "pleural_effusion is not implemented in the weeks 1-2 slice. No image data was "
    "examined and no inference was performed. present=False means 'this capability "
    "produced no assertion', NOT 'no pleural effusion is present'. A real detector "
    "requires labelled clinical data, which is PHI-bearing and out of scope here "
    "(CONTRACT.md §7)."
)

KNOWN_FAILURE_MODES: tuple[FailureMode, ...] = (
    FailureMode(
        id="PE-FM-001",
        text=(
            "The output is a placeholder. Reading present=False as a negative finding for "
            "pleural effusion would be a false negative on every case with an effusion, "
            "which is every failure mode this capability has."
        ),
        detection="human_review",
        mitigation=(
            "The finding kind is 'pleural_effusion.not_implemented', which no consumer "
            "matching the real capability name will match, and which no code dictionary "
            "resolves (MOS-IMG-112). METADATA.method_class is 'not_implemented'."
        ),
    ),
)

METADATA = CapabilityMetadata(
    capability_id=CAPABILITY_ID,
    version=VERSION,
    method_class="not_implemented",
    method_summary=(
        "No method. This capability performs no computation on the image data and "
        "returns a single explicitly-empty finding."
    ),
    output_kinds=(),
    computation_geometry="source",
    input_constraints=(
        "None enforced. The capability examines no image data, so there is no input it "
        "could be outside the constraints of."
    ),
    not_validated_for=(
        "Everything. There is no implementation to validate.",
        "In particular: present=False MUST NOT be read as the absence of a pleural "
        "effusion.",
    ),
    known_failure_modes=KNOWN_FAILURE_MODES,
    parameters={"implemented": False, "note": NOT_IMPLEMENTED_NOTE},
    operating_point=None,
)


@dataclass(frozen=True)
class PleuralEffusion:
    """CONTRACT.md §6 `Capability`, implemented as a declared placeholder."""

    capability_id: str = CAPABILITY_ID
    version: str = VERSION
    metadata: CapabilityMetadata = METADATA

    def applicable(self, vol: CanonicalVolume) -> str | None:
        """Always applicable: see the module docstring for why this does not reject."""
        return None

    def applicability_report(
        self, vol: CanonicalVolume
    ) -> tuple[str, dict[str, Any]] | None:
        return None

    def run(self, vol: CanonicalVolume, ctx: CapabilityContext) -> CapabilityOutcome:
        """Return the empty finding. No pixel data is read.

        `require_source_grid` is still called: it costs nothing, and it means the
        placeholder enforces the same context/volume agreement as a real capability, so
        the day someone implements this the guard is already there rather than being
        remembered.
        """
        require_source_grid(vol, ctx)
        return CapabilityOutcome(
            capability_id=CAPABILITY_ID,
            findings=(
                Finding(
                    kind=FINDING_KIND,
                    # CONTRACT.md §7, verbatim: "returns present=False with a
                    # not_implemented note". It means "no assertion was produced".
                    present=False,
                    # Not 0.0. There is no calibrated score, and 0.0 would claim one.
                    score=None,
                    measurements=(),
                ),
            ),
            # No SEG and no SR content: there is nothing to draw and nothing to report.
            label_map=None,
            # MOS-IMG-121: the instances that WOULD have been consumed. Recorded because a
            # provenance record for this result must still say which series it was asked
            # about, even though it read none of the pixels.
            source_sop_instance_uids=tuple(ctx.source.sop_instance_uids),
        )
