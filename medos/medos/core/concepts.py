# SPDX-License-Identifier: Apache-2.0
"""The coded-concept dictionary (MOS-IMG-111/112/113).

CONTRACT.md §1: "ConceptDictionary". Lifted verbatim from
`spikes/week0/write_dicom_results.py`.

MOS-IMG-113 forbids materialising the clinical code list "in code as a table, a constant
map or an enum", so the codes are loaded from data and a missing key RAISES. MOS-IMG-112:
"The writer MUST raise rather than invent a code" -- a guessed SNOMED code in a SEG is a
clinical assertion nobody made.

Spec: MOS-IMG-111, MOS-IMG-112, MOS-IMG-113.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["ConceptDictionary"]


class ConceptDictionary:
    """Stand-in for `capability_concepts` (MOS-IMG-111).

    MOS-IMG-113: "This list is documentation, not a dictionary ... it MUST NOT be
    materialised in code as a table, a constant map or an enum". So the clinical codes
    live in capability_concepts.json and a missing key raises (MOS-IMG-112: "The writer
    MUST raise rather than invent a code").
    """

    def __init__(self, path: Path) -> None:
        if not path.exists():
            raise SystemExit(
                f"code dictionary not found: {path}\n"
                "MOS-IMG-112 forbids inventing a code, so the writer cannot proceed."
            )
        blob = json.loads(path.read_text(encoding="utf-8"))
        self.path = path
        self._concepts: dict[str, dict[str, Any]] = blob["concepts"]
        self.segment_profiles: dict[str, Any] = blob["segment_profiles"]
        self.rtstruct_roi_map: dict[str, str] = {
            k: v for k, v in blob["rtstruct_roi_map"].items() if not k.startswith("_")
        }

    def __getitem__(self, concept_key: str) -> dict[str, Any]:
        try:
            return self._concepts[concept_key]
        except KeyError:
            raise KeyError(
                f"concept {concept_key!r} has no capability_concepts row in "
                f"{self.path}. MOS-IMG-112: the writer MUST raise rather than invent a "
                "code."
            ) from None

    def profile(self, structure: str) -> dict[str, Any]:
        if structure not in self.segment_profiles:
            raise KeyError(
                f"no segment profile for structure {structure!r} in {self.path}"
            )
        return self.segment_profiles[structure]
