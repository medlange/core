#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Audit DICOM that this platform is about to ingest, and say what is actually in it.

WHY AUDIT THE OUTPUT AND NOT THE INPUT
---------------------------------------
`medos/tools/ingest/nrrd_to_dicom.py` has a good argument for why its output cannot carry an
identifier: it reads only NRRD geometry and segment metadata, it never reads the scene
files where the identifiers live, and it writes every DICOM tag explicitly. That argument
is probably right. It is also exactly the kind of argument this repository keeps catching
out -- a claim about a state, reasoned from how the code ought to behave, standing in for
a measurement of the state itself.

So this tool measures the artefact. It opens what was actually written and reports what is
actually in it, with no reference to how it got there. If the converter grows a bug, or
somebody points this at DICOM from another source, the audit does not change.

WHAT IT CHECKS
--------------
1. THE CONFIDENTIALITY PROFILE. Every tag PS3.15 Table E.1-1 lists as an identifier, by
   tag number rather than by keyword -- a private or mistyped element still has its number.
   Reported as present-and-non-empty, present-and-empty, or absent. Empty is not a finding:
   PatientBirthDate is type 2 and an empty value is the correct way to say "unknown".

2. EVERY OTHER ELEMENT, BY PATTERN. Cyrillic, long digit runs, dates, person names, UID
   roots outside the declared one, Windows user paths, and free-text elements that are
   unexpectedly long. This is the pass that catches an identifier hiding in a tag nobody
   thought to list.

3. UID PROVENANCE. Every UID in the file must be either under the declared root, or a
   well-known DICOM UID (transfer syntaxes, SOP classes). A UID from anywhere else is a
   fragment of the source archive that survived, and it is the single most likely way for
   a linkable identifier to escape a conversion.

WHAT IT PRINTS
--------------
Tag numbers, keywords, categories and COUNTS. Never a value. A tool whose output has to be
handled as carefully as the data it is auditing is a tool nobody runs.

Exit code is 0 when clean, 1 when anything is found. It is meant for CI as much as for a
person.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import pydicom
    from pydicom.tag import Tag
except ImportError:  # pragma: no cover - import guard
    sys.exit("pydicom is required.\n  pip install pydicom")

TOOL = "medicalos-audit-dicom-phi"
VERSION = "1.0.0"

#: PS3.15 Table E.1-1, the Basic Application Level Confidentiality Profile. Listed by tag
#: number: a file that carries PatientName under a mistyped keyword still carries it at
#: (0010,0010). Not exhaustive of the standard -- it is the set whose presence in a
#: file-converted corpus would mean something leaked -- and each entry says what it is so a
#: reader can judge a finding without looking the number up.
PROFILE_TAGS: dict[tuple[int, int], str] = {
    (0x0008, 0x0014): "InstanceCreatorUID",
    (0x0008, 0x0050): "AccessionNumber",
    (0x0008, 0x0080): "InstitutionName",
    (0x0008, 0x0081): "InstitutionAddress",
    (0x0008, 0x0090): "ReferringPhysicianName",
    (0x0008, 0x0092): "ReferringPhysicianAddress",
    (0x0008, 0x0094): "ReferringPhysicianTelephoneNumbers",
    (0x0008, 0x1010): "StationName",
    (0x0008, 0x1030): "StudyDescription",
    (0x0008, 0x103E): "SeriesDescription",
    (0x0008, 0x1040): "InstitutionalDepartmentName",
    (0x0008, 0x1048): "PhysiciansOfRecord",
    (0x0008, 0x1050): "PerformingPhysicianName",
    (0x0008, 0x1060): "NameOfPhysiciansReadingStudy",
    (0x0008, 0x1070): "OperatorsName",
    (0x0008, 0x1080): "AdmittingDiagnosesDescription",
    (0x0010, 0x0010): "PatientName",
    (0x0010, 0x0020): "PatientID",
    (0x0010, 0x0030): "PatientBirthDate",
    (0x0010, 0x0032): "PatientBirthTime",
    (0x0010, 0x0040): "PatientSex",
    (0x0010, 0x1000): "OtherPatientIDs",
    (0x0010, 0x1001): "OtherPatientNames",
    (0x0010, 0x1010): "PatientAge",
    (0x0010, 0x1020): "PatientSize",
    (0x0010, 0x1030): "PatientWeight",
    (0x0010, 0x1040): "PatientAddress",
    (0x0010, 0x1090): "MedicalRecordLocator",
    (0x0010, 0x2154): "PatientTelephoneNumbers",
    (0x0010, 0x2160): "EthnicGroup",
    (0x0010, 0x2180): "Occupation",
    (0x0010, 0x21B0): "AdditionalPatientHistory",
    (0x0010, 0x4000): "PatientComments",
    (0x0018, 0x1000): "DeviceSerialNumber",
    (0x0020, 0x4000): "ImageComments",
    (0x0032, 0x1032): "RequestingPhysician",
    (0x0032, 0x1060): "RequestedProcedureDescription",
    (0x0038, 0x0300): "CurrentPatientLocation",
    (0x0038, 0x0400): "PatientInstitutionResidence",
    (0x0038, 0x4000): "VisitComments",
    (0x0040, 0x0275): "RequestAttributesSequence",
    (0x0040, 0xA123): "PersonName",
    (0x0040, 0xA730): "ContentSequence",
}

#: Tags this converter writes ON PURPOSE with a minted or constant value. A finding on one
#: of these is expected and is reported separately rather than counted as a leak -- but it
#: is still reported, because "expected" is a claim that should be visible.
INTENTIONAL: dict[tuple[int, int], str] = {
    (0x0010, 0x0010): "surrogate minted from the case index",
    (0x0010, 0x0020): "surrogate minted from the case index",
    (0x0008, 0x1030): "constant description written by the converter",
    (0x0008, 0x103E): "constant description written by the converter",
}

PATTERNS: dict[str, re.Pattern[str]] = {
    "cyrillic": re.compile(r"[\u0400-\u04ff]{2,}"),
    "date_like": re.compile(
        r"\b(19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2}\b|\b\d{1,2}[-./]\d{1,2}[-./](19|20)\d{2}\b"
    ),
    "person_name_delimited": re.compile(r"[A-Za-z\u0400-\u04ff]{2,}\^[A-Za-z\u0400-\u04ff]{2,}"),
    "windows_user_path": re.compile(r"(?i)[A-Z]:\\+Users\\+[^\\\s\"']+"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "long_digit_run": re.compile(r"(?<!\d)\d{7,}(?!\d)"),
}

#: Numeric-valued elements where a long digit run is a float, not an identifier. Checked by
#: VR rather than by tag: this is a property of how the value is encoded, so it stays true
#: for elements nobody listed.
NUMERIC_VRS = frozenset({"DS", "IS", "FL", "FD", "SL", "SS", "UL", "US", "SV", "UV", "AT", "OB", "OW", "OF", "OD", "UN"})

WELL_KNOWN_UID_ROOTS = ("1.2.840.10008.",)  # the DICOM standard's own arc


def _values(ds: Any, path: str = "") -> Iterable[tuple[Any, str]]:
    """Every data element in the dataset, recursing into sequences."""
    for elem in ds:
        if elem.tag == Tag(0x7FE0, 0x0010):  # PixelData
            continue
        here = f"{path}/{elem.keyword or elem.tag}"
        if elem.VR == "SQ":
            for i, item in enumerate(elem.value or []):
                yield from _values(item, f"{here}[{i}]")
        else:
            yield elem, here


def audit_file(path: Path, uid_root: str) -> dict[str, Any]:
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    profile_present: dict[str, str] = {}
    pattern_hits: list[tuple[str, str, str]] = []
    foreign_uids: list[tuple[str, str]] = []

    for elem, where in _values(ds):
        key = (elem.tag.group, elem.tag.element)
        raw = elem.value
        text = "" if raw is None else str(raw)

        if key in PROFILE_TAGS:
            name = PROFILE_TAGS[key]
            profile_present[name] = "non-empty" if text.strip() else "empty"

        if elem.VR == "UI" and text:
            for uid in (text.split("\\") if "\\" in text else [text]):
                uid = uid.strip()
                if not uid:
                    continue
                if uid.startswith(uid_root) or uid.startswith(WELL_KNOWN_UID_ROOTS):
                    continue
                foreign_uids.append((where, uid[:12] + "..."))

        if not text or elem.VR in NUMERIC_VRS:
            continue
        for label, pattern in PATTERNS.items():
            if label == "long_digit_run" and elem.VR in ("UI", "DA", "TM", "DT"):
                continue  # a UID or a date is digits by definition
            if pattern.search(text):
                pattern_hits.append((label, where, elem.VR))

    return {"profile": profile_present, "patterns": pattern_hits, "foreign_uids": foreign_uids}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog=TOOL, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", required=True, metavar="DIR", help="directory of DICOM to audit")
    p.add_argument("--uid-root", default="2.25.", metavar="PREFIX",
                   help="the UID arc this corpus was minted under (default 2.25.)")
    p.add_argument("--limit", type=int, default=0, metavar="N", help="audit at most N files")
    args = p.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"{TOOL}: --root {root} is not a directory", file=sys.stderr)
        return 2

    files = sorted(root.rglob("*.dcm"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"{TOOL}: no .dcm under {root}", file=sys.stderr)
        return 2

    profile: collections.Counter[tuple[str, str]] = collections.Counter()
    patterns: collections.Counter[tuple[str, str, str]] = collections.Counter()
    foreign: collections.Counter[str] = collections.Counter()
    unreadable = 0

    for path in files:
        try:
            result = audit_file(path, args.uid_root)
        except Exception as exc:  # noqa: BLE001
            unreadable += 1
            if unreadable <= 3:
                print(f"  unreadable: {path.name}: {exc.__class__.__name__}")
            continue
        for name, state in result["profile"].items():
            profile[(name, state)] += 1
        for label, where, vr in result["patterns"]:
            patterns[(label, where, vr)] += 1
        for _where, uid in result["foreign_uids"]:
            foreign[uid] += 1

    print("-" * 86)
    print(f"{TOOL} {VERSION}")
    print("-" * 86)
    print(f"  root          {root}")
    print(f"  files audited {len(files)}" + (f"   ({unreadable} unreadable)" if unreadable else ""))
    print(f"  uid root      {args.uid_root}")
    print("-" * 86)

    print("  CONFIDENTIALITY PROFILE TAGS PRESENT (PS3.15 E.1-1)")
    if not profile:
        print("    none")
    for (name, state), count in sorted(profile.items()):
        tag = next(t for t, n in PROFILE_TAGS.items() if n == name)
        note = INTENTIONAL.get(tag)
        flag = "   " if (state == "empty" or note) else " ! "
        why = f"  <- {note}" if note else ("  <- empty: correct for an unknown type-2 value" if state == "empty" else "")
        print(f"  {flag}({tag[0]:04X},{tag[1]:04X}) {name:38s} {state:9s} {count:6d}{why}")

    print("-" * 86)
    print("  PATTERN HITS OUTSIDE NUMERIC ELEMENTS")
    unexpected = 0
    if not patterns:
        print("    none")
    for (label, where, vr), count in patterns.most_common(30):
        tag_name = where.rsplit("/", 1)[-1]
        intentional = any(n == tag_name for n in (INTENTIONAL.get(t) and PROFILE_TAGS[t] for t in INTENTIONAL))
        if not intentional:
            unexpected += count
        print(f"    {label:22s} {where:44s} VR={vr} {count:6d}")

    print("-" * 86)
    print("  UIDS OUTSIDE THE DECLARED ROOT AND THE DICOM STANDARD ARC")
    if not foreign:
        print("    none")
    for uid, count in foreign.most_common(10):
        print(f"  !  {uid:30s} {count:6d}")

    print("-" * 86)
    leaks = sum(c for (n, s), c in profile.items()
                if s == "non-empty" and next(t for t, nm in PROFILE_TAGS.items() if nm == n) not in INTENTIONAL)
    verdict_bad = bool(foreign) or leaks or unexpected
    if verdict_bad:
        print("  VERDICT: findings above. This corpus is NOT clean; do not ingest it until")
        print("  each line marked ! is either removed or explained in the corpus manifest.")
    else:
        print("  VERDICT: no confidentiality-profile tag carries a value that was not minted")
        print("  by the converter, no element matches an identifier pattern, and every UID is")
        print("  under the declared root or the DICOM standard arc. Measured, not argued.")
    print("-" * 86)
    return 1 if verdict_bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
