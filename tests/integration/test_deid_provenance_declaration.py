# SPDX-License-Identifier: Apache-2.0
"""`MEDOS_DEID_PROVENANCE` is a claim about data, and this is what keeps it true.

WHY THIS FILE EXISTS
--------------------
`medos/deploy/compose/docker-compose.yml` declares that every image this installation seals is
`public_deidentified`, under TCIA's published policy and TCIA's published UID space. That
is true of what the archive holds today and it is not a property of the software: it is a
property of which studies happen to be in it, and anybody can change that with one STOW.

`MOS-EVID-021` makes the declaration part of an immutable, content-addressed
`DatasetVersion`, and `MOS-EVID-018` ties dataset identity to the UIDs. So a study the
declaration does not cover, sealed under it, records a false statement about provenance in
a row that is never updated and that every downstream `ValidationReport` cites. There is no
later moment at which that gets noticed.

So the declaration is not left to vigilance. This module fails the moment the archive holds
a study that no declaration covers, which turns "somebody ingested clinic images into the
dev stack" from a silent widening of a compliance claim into a red suite.

TWO WAYS A STUDY CAN BE COVERED, AND WHY THERE HAD TO BE A SECOND ONE
----------------------------------------------------------------------
The first way is its UID root: TCIA's registered arc, or the synthetic arc this suite's
own fixtures generate under. A root is enough on its own because it names the authority
that issued the UID.

The second way is a CORPUS MANIFEST -- a file, emitted by `medos/tools/ingest/nrrd_to_dicom.py`
when it converts a corpus, that names every study it produced and states the provenance
facts for them. That mechanism exists because of a hole this module used to have, and the
hole is worth writing down because it is the exact defect this repository keeps finding:

    `2.25.` USED TO BE A COVERED ROOT HERE, justified as "this platform's own derived
    objects (MOS-IMG-062), not sources". That justification does not survive contact with
    `MOS-IMG-069`. The platform is forbidden to mint a `StudyInstanceUID` at all --
    `derive_uid` has no such parameter and `medos/medos/writer/identity.py` copies the study UID
    and never mints one -- so a derived SEG or SR lands in an EXISTING study and creates a
    series, never a study row. The covered root was therefore protecting a case that
    cannot occur, while leaving a wide door open: `2.25.` is the ISO arc for UUID-derived
    OIDs, which is exactly what an ingest tool with no registered root will mint under. A
    hundred clinic studies would have entered the archive, matched the `2.25.` prefix,
    inherited a `public_deidentified` / `tcia:*` declaration that is false about every one
    of them, and left this suite green. Measured before removing it: 0 of the 176 studies
    in the archive were under `2.25.`, so the root was covering nothing and risking
    everything.

So coverage by prefix was replaced, for that case, by coverage as a RECORDED FACT about a
named corpus. A manifest has to say the same three things `MOS-EVID-021` asks of a seal,
and it has to say who holds the legal basis -- because a corpus that arrives outside a
public archive has one, and `MOS-TRAIN-073` will ask.

WHAT IT DOES NOT DO
-------------------
It does not verify that the images ARE de-identified. Nothing here can: the platform
performs no de-identification (`medos/medos/gateway/app.py` answers 503 `DEID_NOT_IMPLEMENTED`,
`studies.deid_policy_version` is null on every row, and `deid_policies` is a table
0005_gateway deferred and nobody wrote). It verifies the narrower thing that is checkable
-- that every study came from a source some declaration names -- and each declaration
states plainly what its two ids point at.

Spec: MOS-EVID-018, MOS-EVID-021, MOS-TRAIN-073, MOS-IMG-069. Register entries 93, 97.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from tests._support import stack
from tests._support.skips import skip_infra

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "medos" / "deploy" / "compose" / "docker-compose.yml"

#: Where corpus manifests are read from.
#:
#: The default is in-repo and, in this open-source repository, empty but for a README: a
#: manifest names a deployment's own data holdings, and committing one would publish which
#: corpora an installation holds and who supplied them. A deployment points this at its own
#: directory. An installation that ingests a corpus and declares nothing gets a red suite,
#: which is the intended outcome and not an inconvenience to route around.
PROVENANCE_DIR_VAR = "MEDOS_PROVENANCE_DIR"
DEFAULT_PROVENANCE_DIR = REPO / "medos" / "deploy" / "provenance"

#: UID roots covered by the declaration in docker-compose.yml, and why each is covered.
#:
#: Read the module docstring before adding one. A root is the right mechanism only when the
#: prefix itself names the authority that issued the UID; when it does not -- `2.25.` being
#: the case that taught this -- the corpus manifest is the mechanism, because it records a
#: fact instead of inferring one.
COVERED_ROOTS: tuple[tuple[str, str], ...] = (
    ("1.3.6.1.4.1.14519.", "TCIA's registered root: the public corpus the declaration names"),
    ("1.2.826.0.1.3680043.8.498.", "this suite's synthetic fixtures; no patient behind them"),
)

_DEID_STATUSES = frozenset({"identified", "pseudonymised", "public_deidentified"})


def _declared() -> dict[str, Any]:
    """The declaration as docker-compose.yml writes it, defaults resolved."""
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(
        r"MEDOS_DEID_PROVENANCE:\s*>-\s*\n\s*\$\{MEDOS_DEID_PROVENANCE-(.*?)\}\}\s*\n",
        text,
        re.DOTALL,
    )
    assert match, (
        "docker-compose.yml no longer declares MEDOS_DEID_PROVENANCE with a default. If "
        "the declaration moved, move this check with it: it is the only thing standing "
        "between an ingested clinic study and a false provenance claim in an immutable row."
    )
    raw = re.sub(r"\s+", " ", match.group(1)).strip() + "}"
    return json.loads(raw)


def _provenance_dir() -> Path:
    return Path(os.environ.get(PROVENANCE_DIR_VAR) or DEFAULT_PROVENANCE_DIR)


def _manifests() -> list[tuple[Path, dict[str, Any]]]:
    """Every corpus manifest, parsed. A manifest that will not parse is a failure, not an
    absence: silently ignoring it would un-cover its studies and the suite would then fail
    somewhere else, naming the studies rather than the file that broke."""
    directory = _provenance_dir()
    if not directory.is_dir():
        return []
    out: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append((path, json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError as exc:
            pytest.fail(f"corpus manifest {path} is not valid JSON: {exc}")
    return out


def _covered_by_manifest() -> dict[str, str]:
    """study_instance_uid -> corpus_id, across every manifest."""
    out: dict[str, str] = {}
    for path, doc in _manifests():
        corpus_id = str(doc.get("corpus", {}).get("corpus_id") or path.stem)
        for uid in doc.get("study_instance_uids", []):
            out[str(uid)] = corpus_id
    return out


def test_the_declaration_is_well_formed_and_says_what_the_seal_requires() -> None:
    """It must parse, and carry the three members `MOS-EVID-021` needs for a non-identified
    status. A malformed declaration fails closed at the seal (503), but it fails there in
    front of an operator; failing here is cheaper and names the file."""
    doc = _declared()
    assert doc["deidentification_status"] in _DEID_STATUSES, doc
    if doc["deidentification_status"] != "identified":
        assert doc.get("deid_policy_id"), "MOS-EVID-021 requires a policy id"
        assert doc.get("uid_mapping_table_id"), "MOS-EVID-021 requires a UID mapping id"


def test_the_two_ids_name_an_external_authority_and_do_not_pretend_to_be_tenant_rows() -> None:
    """The honesty property, asserted rather than left to the comment above them.

    This installation holds no tenant policy row and no tenant UID mapping table. The ids
    therefore carry an authority prefix, so that nothing downstream reads them as pointing
    into a table here. If someone later gives this deployment a real `deid_policies` row
    and a real mapping table, this test is what they must come and change -- which is the
    moment to re-read `MOS-EVID-021` rather than inherit a reading of it.
    """
    doc = _declared()
    for field in ("deid_policy_id", "uid_mapping_table_id"):
        value = str(doc.get(field, ""))
        assert ":" in value and not value.startswith(("dp_", "um_")), (
            f"{field}={value!r} looks like a tenant row id. This deployment has no "
            f"deid_policies table and no UID mapping table; an id shaped like a local row "
            f"claims one exists."
        )


def test_the_2_25_arc_is_not_a_covered_root() -> None:
    """A regression guard on the hole described in the module docstring.

    `2.25.` is where any tool without a registered root will mint, so re-adding it as a
    covered root silently re-opens the door for every future ingest. It reads as harmless
    -- the platform does write `2.25.` UIDs, just never for studies -- which is precisely
    why it needs an assertion rather than a comment.
    """
    roots = [root for root, _why in COVERED_ROOTS]
    assert "2.25." not in roots, (
        "`2.25.` is back in COVERED_ROOTS. It is the ISO arc for UUID-derived OIDs and it "
        "belongs to no authority, so it cannot vouch for the provenance of anything. The "
        "platform's own 2.25. objects are SEG and SR series inside existing studies -- "
        "MOS-IMG-069 forbids minting a study UID -- so covering it protects nothing and "
        "admits any ingested corpus. Cover corpora with a manifest instead; see "
        "medos/tools/ingest/nrrd_to_dicom.py."
    )
    assert not any(r.startswith("2.25") for r in roots), roots


def test_every_corpus_manifest_says_what_a_seal_will_ask_it() -> None:
    """A manifest that covers studies must answer the same questions the compose
    declaration answers, plus the one a public archive does not raise: who holds the legal
    basis. `MOS-TRAIN-073` will ask for it at the seal, and the answer is a fact about an
    agreement between people that no amount of inspecting the pixels can recover.
    """
    manifests = _manifests()
    if not manifests:
        pytest.skip(
            f"no corpus manifests in {_provenance_dir()} -- nothing to check. This is the "
            f"normal state for a checkout that has ingested no corpus of its own."
        )
    for path, doc in manifests:
        corpus = doc.get("corpus")
        assert isinstance(corpus, dict), f"{path}: no `corpus` object"
        status = corpus.get("deidentification_status")
        assert status in _DEID_STATUSES, f"{path}: deidentification_status={status!r}"
        if status != "identified":
            assert corpus.get("deid_policy_id"), f"{path}: MOS-EVID-021 requires a policy id"
            assert corpus.get("uid_mapping_table_id"), (
                f"{path}: MOS-EVID-021 requires a mapping id"
            )
        for field in ("corpus_id", "legal_basis", "legal_basis_holder"):
            assert str(corpus.get(field) or "").strip(), (
                f"{path}: `{field}` is empty. MOS-TRAIN-073 asks a corpus that did not come "
                f"from a public archive on whose authority it is being used, and an empty "
                f"string is not an answer -- it is the absence of one, recorded as though "
                f"it were."
            )
        assert (
            isinstance(doc.get("study_instance_uids"), list) and doc["study_instance_uids"]
        ), (
            f"{path}: a manifest that lists no studies covers nothing. If the corpus is "
            f"not ingested yet, the manifest should not be here yet."
        )
        assert corpus.get("known_limitations"), (
            f"{path}: `known_limitations` is empty. Every corpus reconstructed from files "
            f"has lost something -- at minimum its original UIDs -- and a manifest that "
            f"claims none is the one thing a reader cannot check."
        )


def test_no_study_is_claimed_by_two_corpora() -> None:
    """Two manifests claiming one study means two provenance stories for the same immutable
    row, and nothing downstream picks between them."""
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for path, doc in _manifests():
        corpus_id = str(doc.get("corpus", {}).get("corpus_id") or path.stem)
        for uid in doc.get("study_instance_uids", []):
            previous = seen.setdefault(str(uid), corpus_id)
            if previous != corpus_id:
                clashes.append(f"{uid}: {previous} and {corpus_id}")
    assert not clashes, "studies claimed by two corpora:\n  " + "\n  ".join(clashes[:10])


@pytest.mark.slow
def test_every_study_in_the_archive_is_covered_by_a_declaration() -> None:
    """THE CHECK THIS MODULE EXISTS FOR.

    One STOW of an undeclared study makes the compose declaration false, and nothing else
    in the repository would notice. `MOS-EVID-021` puts the claim in an immutable row and
    `MOS-EVID-018` ties dataset identity to these UIDs, so the falsehood is permanent and
    is cited by every `ValidationReport` over the cohort.
    """
    try:
        conn = psycopg.connect(stack.database_url(), row_factory=dict_row)
    except psycopg.OperationalError as exc:
        skip_infra(f"{stack.database_url().rsplit('@', 1)[-1]}: {exc}", dependency="postgres")
    with conn:
        rows = conn.execute("SELECT study_instance_uid FROM studies").fetchall()

    prefixes = tuple(p for p, _why in COVERED_ROOTS)
    by_manifest = _covered_by_manifest()
    uncovered = sorted(
        str(r["study_instance_uid"])
        for r in rows
        if not str(r["study_instance_uid"]).startswith(prefixes)
        and str(r["study_instance_uid"]) not in by_manifest
    )
    assert not uncovered, (
        f"{len(uncovered)} study(ies) in this archive are covered by no provenance "
        f"declaration, so the declaration in docker-compose.yml is now FALSE about them "
        f"and anything sealed under it records a false provenance claim in an immutable "
        f"row.\n"
        f"  first few: {uncovered[:5]}\n"
        f"  covered roots:\n             "
        + "\n             ".join(f"{p}* -- {why}" for p, why in COVERED_ROOTS)
        + f"\n  corpus manifests: {len(_manifests())} in {_provenance_dir()}, "
        f"covering {len(by_manifest)} study(ies)\n\n"
        "Either those studies do not belong in this stack, or they need a declaration. "
        "For a corpus converted from files, `medos/tools/ingest/nrrd_to_dicom.py --manifest` "
        "writes one; point "
        + PROVENANCE_DIR_VAR
        + " at the directory holding it. For clinic images that is not a formality: it "
        "means answering the question MOS-EVID-021 asks and this platform refuses to "
        "answer for you."
    )
