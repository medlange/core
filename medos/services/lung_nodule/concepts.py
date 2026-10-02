# SPDX-License-Identifier: Apache-2.0
"""Deploy-time composition of the coded-concept dictionary.

THE GAP THIS CLOSES, AND WHY IT IS A GAP AND NOT A DESIGN
-----------------------------------------------------------
`MOS-IMG-111` names a `capability_concepts` TABLE and `MOS-REG-042` makes the Capability row
"the single source of coded concepts for that clinical function ... a second code table
anywhere in the platform is forbidden". In this build the table's stand-in is ONE JSON file
loaded by `medos.core.concepts.ConceptDictionary`, and `MEDOS_CAPABILITY_CONCEPTS` REPLACES
its path wholesale. There is no `INSERT`. So registering a fourth capability's concepts has
exactly three shapes:

  a. edit `medos/medos/capabilities/capability_concepts.json` -- the base dictionary
     itself, which is core-adjacent data the zero-core-change gate would count, and which
     a capability shipped by somebody else cannot reach in any case;
  b. commit a full copy of the base dictionary with four rows added -- the second
     definition CONTRACT.md section 2 exists to eliminate, guaranteed to drift on the first
     code added to either copy, and the copy is always the one nobody updates;
  c. keep ONLY the new rows as an overlay and merge them onto the base at deploy time,
     refusing any key the base already owns.

(c) is what this module does. It is not a workaround dressed as a design: the merge is
strict, so a collision is an error rather than a silent override, and the composed file is
a BUILD ARTEFACT written to a caller-supplied directory -- nothing is committed twice.

REPORTED, NOT PATCHED. The platform still has no additive concept-registration path -- no
`INSERT`, no `capability_concepts` table. What it now has is a CHAINER:
`medos.capabilities.providers` hands provider N the composition provider N-1 produced, as
`worker_concepts(work_dir, base=...)`, so a deployment running two independently-shipped
capabilities ends with ONE dictionary holding both overlays rather than two dictionaries
each missing the other's rows. That is enough to keep `MOS-REG-042`'s "a second code table
anywhere in the platform is forbidden" true at run time, and it is NOT the fix: the
composition is still a file rebuilt at every startup from overlays that must all be present
in the image, a capability cannot register a code without shipping a file, and nothing
reconciles an overlay against what a previous deployment composed. The honest fix is
`MOS-IMG-111`'s table; it is a core change and it is out of scope for `MOS-REL-020`, whose
whole question is what can be added WITHOUT one.

Spec: MOS-IMG-111, MOS-IMG-112, MOS-IMG-113, MOS-REG-042, MOS-REG-044.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from medos.capabilities.base import default_concepts_path

__all__ = ["overlay_path", "compose", "compose_to", "ENV_VAR"]

#: The variable `medos.capabilities.base.default_concepts_path` reads. Named here so a
#: caller wiring a deployment does not have to know the platform's spelling of it.
ENV_VAR = "MEDOS_CAPABILITY_CONCEPTS"

#: Sections of the dictionary this overlay may contribute to. `ConceptDictionary.__init__`
#: reads exactly these three keys; a fourth in an overlay would be silently ignored, which
#: is the failure this tuple turns into an error.
_SECTIONS = ("concepts", "segment_profiles", "rtstruct_roi_map")


def overlay_path() -> Path:
    """`medos/examples/lung-nodule/concepts.overlay.json` -- the rows, as registry data."""
    return (
        Path(__file__).resolve().parents[2]
        / "examples"
        / "lung-nodule"
        / "concepts.overlay.json"
    )


def compose(
    base: Path | None = None, overlay: Path | None = None
) -> dict[str, Any]:
    """Base dictionary + overlay rows, as a dict. Strict about collisions.

    A key present in BOTH raises. `MOS-REG-042` makes the Capability row the single source
    for its function's codes; an overlay that quietly redefined `anatomy.lung` would make
    two capabilities draw different things under one code, and the SEGs would be
    indistinguishable afterwards.

    Keys beginning with `_` are prose (`_comment`, `_note`) and are merged without the
    collision check, because two files may each carry their own commentary.
    """
    base_blob: dict[str, Any] = json.loads(
        (base or default_concepts_path()).read_text(encoding="utf-8")
    )
    overlay_blob: dict[str, Any] = json.loads(
        (overlay or overlay_path()).read_text(encoding="utf-8")
    )

    unknown = sorted(set(overlay_blob) - set(_SECTIONS) - {"_comment"})
    if unknown:
        raise ValueError(
            f"overlay contributes section(s) {unknown}, which ConceptDictionary does not "
            f"read; it understands {list(_SECTIONS)}. A section nothing loads is a "
            "registration that silently did not happen."
        )

    composed = dict(base_blob)
    for section in _SECTIONS:
        rows = dict(base_blob.get(section, {}))
        for key, value in overlay_blob.get(section, {}).items():
            if key.startswith("_"):
                continue
            if key in rows:
                raise ValueError(
                    f"overlay redefines {section}.{key!r}, which the base dictionary "
                    f"already owns ({base or default_concepts_path()}). MOS-REG-042 "
                    "forbids a second definition of a coded concept; rename the overlay "
                    "row or extend the base."
                )
            rows[key] = value
        composed[section] = rows

    composed["_composed_from"] = {
        "base": str(base or default_concepts_path()),
        "overlay": str(overlay or overlay_path()),
        "note": (
            "Build artefact. Do not commit. Regenerate with "
            "services.lung_nodule.concepts.compose_to()."
        ),
    }
    return composed


def compose_to(
    destination: Path, base: Path | None = None, overlay: Path | None = None
) -> Path:
    """Write the composed dictionary and return its path.

    The caller sets `MEDOS_CAPABILITY_CONCEPTS` to the result, or passes
    `ConceptDictionary(path)` explicitly. This function sets no environment variable
    itself: a library that mutates `os.environ` changes the behaviour of every other
    capability in the process, and CONTRACT.md section 11 forbids exactly that kind of
    ambient coupling.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(compose(base, overlay), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination
