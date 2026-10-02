# SPDX-License-Identifier: Apache-2.0
"""A path that resolves only after a prefix nobody wrote.

Commit `3ff9aec` -- "deploy/ follows the platform, and three release records stop lying
about what was measured" -- moved the deployment from `deploy/` to `medos/deploy/`. It
updated the documents. It did not update the code comments.

MEASURED, on a tree nobody had touched for the purpose: **28 citations across 15 files still
named a root `deploy/`**, and there is no root `deploy/` -- `git ls-files | awk -F/ '{print
$1}' | sort -u` does not list one. Sixteen of them named a file that EXISTS one directory
down, so a reader following one found nothing and had no way to guess the prefix:
`docker-compose.yml`, `nginx.conf.template`, `viewer-config.js`, `orthanc.json`,
`gateway-entrypoint.sh`, `gateway-principals.json`, `medicalos-config.js`,
`requirements.txt`, `web-entrypoint.sh`.

WHY A SEPARATE CHECK FROM THE DOCUMENT ONES. `tests/unit/test_product_readmes.py` resolves
paths in the four product READMEs and `tests/unit/test_documented_commands_resolve.py`
resolves them in fenced commands. Neither reads a `.js` comment, a `.json` field or an
`.html` note, and that is where all sixteen were: `viewer/viewer-config.js`'s header
explaining the override seam, `training-console/src/refusals/catalogue.js`'s explanation of
where a credential comes from, `standalone/index.html`'s note on what serves the origin.
A comment is the documentation a reader is holding when they are already in the file.

WHAT IT ASSERTS, AND THE ONE THING IT MUST NOT. It asserts that no file cites a bare
`<tree>/…` path whose target exists at `medos/<tree>/…`, which is decidable and was wrong
sixteen times. It does NOT assert that every bare citation resolves: `deploy/compose/
ohif-config.js` and `deploy/compose/ohif.lock` were DELETED with the OHIF withdrawal, and
most sentences naming them say so in the same breath. Prefixing those would turn a correct
statement about a removed file into a wrong statement about a path that still does not
exist. A path is EVIDENCE there, not an address -- the distinction `docs/README.md` draws
for the release records, one directory over.

Register entry 133.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Trees that moved under `medos/`. A bare citation of one is the defect, but only when the
#: target is there to be found.
MOVED_UNDER_MEDOS = ("deploy", "services", "schemas", "contracts", "examples", "web", "api")

#: `docs/spec/` is normative prose and `docs/releases/` is history; a path in either is
#: evidence of what was true when it was written. `docs/README.md` says so for the records.
#:
#: `medos/medos/db/migrations/` is exempt for a harder reason: those files are IMMUTABLE.
#: `tests/unit/test_migration_immutability.py` asserts each one still holds the bytes of the
#: commit that added it, and register entry 116 is a path rewrite that changed sixteen of
#: them. `0004_auth.up.sql` carries `contracts/permissions.yaml` inside a SQL string; it
#: stays, and a check that asked for it to be fixed would be asking for entry 116 again.
#:
#: `medos/deploy/` is NOT exempt, and was in the first draft of this module. That draft hid
#: the worst site in the repository: the compose file's own USAGE block held four
#: `docker compose -f deploy/compose/docker-compose.yml` lines -- the commands a reader
#: copies to bring the stack up, naming a path that does not exist.
SKIP_PREFIX = ("docs/spec/", "docs/releases/", "medos/medos/db/migrations/")

#: Files another session holds. An entry here is a deferral with a reason, not an exemption:
#: `trainer/requirements.txt` carries one citation, in the heading "WHY THIS FILE IS NOT
#: `deploy/compose/requirements.txt`", and a one-line edit inside somebody's open file buys
#: nothing. Register entry 123 is the precedent for not widening onto work in flight.
DEFERRED: dict[str, str] = {
    "trainer/requirements.txt": "held by another session; one citation, in a heading",
}

#: Files whose bare citations are CORRECT, each with what the path is doing there. Measured
#: one by one, because the first run of this check called all four defects and all four are
#: not: a path can be EVIDENCE about a past state, or a name fragment, and neither is an
#: address. This is the same distinction `docs/README.md` draws for the release records, and
#: a check that cannot see it must be told rather than obeyed.
NOT_AN_ADDRESS: dict[str, str] = {
    "medos/api/v1/routes.train.yaml": (
        "the `schemas:` block's 34 entries, which resolve against the registry's own "
        "PRODUCT root rather than the repository's. MEASURED: `medos/tools/permcheck.py` "
        "checks each with `(ROOT / relative).is_file()` where its `ROOT` is `<repo>/medos`, "
        "and all 34 resolve there while 0 of 34 resolve from the repository root. "
        "Prefixing them would break the check that reads them. THIS ROW SAID SOMETHING "
        "ELSE AND BOTH HALVES WERE WRONG: it counted 28, and it gave the reason as "
        "`MOS-API-085` still naming the pre-split file, which was a deferral to register "
        "entry 114 rather than a reason -- and entry 114 is now closed, so the stated "
        "reason would have been stale as well as wrong. The real reason does not depend on "
        "any requirement."
    ),
    "tests/gate/test_zero_core_change.py": (
        "`CAPABILITY_WITNESS = \"services/lung_nodule/service.py\"`, and the comment above "
        "it says why: \"that is where the file was when the second capability landed\". The "
        "gate matches it against `git diff --name-only` output for that range, where the "
        "path was bare. Prefixing it would break the check it is the witness for."
    ),
    "tests/integration/test_lung_nodule.py": (
        "the same shape -- a path recorded as it appeared in a past diff, beside lines that "
        "DO carry the prefix (`medos/examples/lung-nodule/worker_main.py`, "
        "`medos/services/lung_nodule/service.py`), so the file distinguishes the two uses "
        "correctly and this check does not."
    ),
    "tests/unit/test_packaging_surface.py": (
        "`NOT_SHIPPED = (\"tools\", \"services\", \"api/v1\", …)` -- setuptools package-name "
        "fragments in an exclude list, not filesystem paths. `api/v1` is not a directory "
        "reference here at all."
    ),
    "tests/unit/test_moved_tree_citations.py": (
        "THIS FILE. It quotes the six paths above in order to say why each is exempt, so "
        "read as addresses its own reasons are what it forbids -- the third time in this "
        "sweep that a check would have banned the paragraph retracting the thing it checks "
        "for. `tests/unit/test_product_readmes.py` and "
        "`tests/unit/test_documented_test_citations.py` carry the same declared-exception "
        "shape for the same reason."
    ),
}

CITATION = re.compile(
    r"(?<![/\w-])(" + "|".join(MOVED_UNDER_MEDOS) + r")/([\w][\w./-]*[\w])"
)


def _tracked() -> list[str]:
    """Tracked AND untracked-but-present files, and the second half is not a nicety.

    THIS MODULE WENT RED THE MOMENT IT WAS COMMITTED, and the reason is this function. It
    read `git ls-files`, which lists only what the index knows -- so while the file was new
    and untracked, the check could not see ITSELF. It passed, was committed, and the commit
    is what made it visible to its own predicate: the six paths quoted in NOT_AN_ADDRESS's
    reasons are, read as addresses, exactly what it forbids. A green run before the commit
    and a red one after, from one `git add`.

    That blind spot is worse than the embarrassment. Any new file is unchecked until it is
    tracked, which is precisely when a reviewer is looking at it and a contributor most
    wants to know. `--others --exclude-standard` closes it: untracked files that are not
    ignored are read too, so the check applies to a file the moment it exists on disk.
    """
    return subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout.split()


def _candidates() -> list[str]:
    return [
        p for p in _tracked()
        if not any(p.startswith(s) for s in SKIP_PREFIX)
        and Path(p).suffix in {
            ".py", ".js", ".mjs", ".html", ".json", ".md", ".yml", ".yaml", ".css",
            ".sh", ".txt", ".sql", ".template",
        }
    ]


def _findable_only_one_level_down() -> dict[str, list[str]]:
    """file -> the bare citations in it whose target exists at `medos/<citation>`."""
    out: dict[str, list[str]] = {}
    for rel in _candidates():
        if rel in DEFERRED or rel in NOT_AN_ADDRESS:
            continue
        try:
            text = (ROOT / rel).read_bytes().decode("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits: list[str] = []
        for m in CITATION.finditer(text):
            cited = f"{m.group(1)}/{m.group(2)}".rstrip(".,;:)`\"'")
            if (ROOT / cited).exists():
                continue  # resolves as written
            if (ROOT / "medos" / cited).exists():
                hits.append(cited)
        if hits:
            out[rel] = sorted(set(hits))
    return out


def test_there_are_citations_to_check() -> None:
    """A green run must not be reachable by reading no files."""
    assert len(_candidates()) >= 300, (
        f"only {len(_candidates())} candidate file(s); the tracked-file filter is broken"
    )
    blob = "\n".join(
        (ROOT / p).read_bytes().decode("utf-8", "replace") for p in _candidates()[:400]
    )
    assert CITATION.search(blob), "the citation pattern matches nothing at all"


def test_no_file_cites_a_path_that_only_resolves_one_level_down() -> None:
    broken = _findable_only_one_level_down()
    assert not broken, (
        f"{sum(len(v) for v in broken.values())} citation(s) in {len(broken)} file(s) name "
        f"a path that does not exist but IS at `medos/<that path>`:\n  "
        + "\n  ".join(f"{rel}: {hits}" for rel, hits in sorted(broken.items()))
        + "\n\n  Commit 3ff9aec moved these trees under medos/ and updated the documents "
        "but not the code comments. A reader following one of these finds nothing."
    )


def test_the_root_of_each_moved_tree_really_is_gone() -> None:
    """The premise. If a root `deploy/` ever comes back, this whole check is wrong."""
    present = sorted(t for t in MOVED_UNDER_MEDOS if (ROOT / t).is_dir())
    assert not present, (
        f"{present} exist at the repository root again, so a bare citation of one may be "
        "correct and this module's premise no longer holds. Decide which tree is "
        "authoritative before re-pointing anything."
    )


def test_every_deferral_still_has_the_citation_it_defers() -> None:
    """A deferral for a file that no longer cites anything is a stale exemption."""
    stale: list[str] = []
    for rel in DEFERRED:
        if not (ROOT / rel).exists():
            stale.append(f"{rel} (gone)")
            continue
        text = (ROOT / rel).read_bytes().decode("utf-8", "replace")
        if not any(
            (ROOT / "medos" / f"{m.group(1)}/{m.group(2)}").exists()
            and not (ROOT / f"{m.group(1)}/{m.group(2)}").exists()
            for m in CITATION.finditer(text)
        ):
            stale.append(f"{rel} (nothing left to defer)")
    assert not stale, f"DEFERRED carries stale entries: {stale}"


def test_every_not_an_address_entry_still_has_a_bare_citation() -> None:
    """An exemption for a file that no longer cites anything bare is a stale exemption.

    This is the half of a declared list that is easy to leave out: the list stops the check
    firing, so nothing notices when the reason evaporates.
    """
    stale: list[str] = []
    for rel, reason in NOT_AN_ADDRESS.items():
        assert reason.strip(), f"NOT_AN_ADDRESS[{rel!r}] states no reason"
        if not (ROOT / rel).exists():
            stale.append(f"{rel} (gone)")
            continue
        text = (ROOT / rel).read_bytes().decode("utf-8", "replace")
        bare = [
            f"{m.group(1)}/{m.group(2)}"
            for m in CITATION.finditer(text)
            if not (ROOT / f"{m.group(1)}/{m.group(2)}").exists()
            and (ROOT / "medos" / f"{m.group(1)}/{m.group(2)}").exists()
        ]
        if not bare:
            stale.append(f"{rel} (nothing bare left)")
    assert not stale, (
        f"NOT_AN_ADDRESS carries stale entries: {stale}. Remove each one; the check covers "
        "those files again the moment the exemption goes."
    )


def test_the_compose_files_own_usage_block_names_itself_correctly() -> None:
    """The site the first draft of this module excluded, asserted at its own address.

    `medos/deploy/compose/docker-compose.yml`'s USAGE block is the four commands a reader
    copies to bring the stack up. All four named `deploy/compose/docker-compose.yml`, which
    does not exist, and the first draft of this check skipped everything under
    `medos/deploy/` — so the file that most needed it was the one file exempt from it.
    """
    compose = ROOT / "medos" / "deploy" / "compose" / "docker-compose.yml"
    rel = compose.relative_to(ROOT).as_posix()
    text = compose.read_bytes().decode("utf-8")
    usage = [
        line.strip().lstrip("# ")
        for line in text.splitlines()
        if "docker compose -f" in line
    ]
    assert len(usage) >= 4, (
        f"{rel}'s USAGE block no longer holds the four commands this check was written "
        f"against; it holds {len(usage)}"
    )
    wrong = [line for line in usage if f"-f {rel}" not in line]
    assert not wrong, (
        f"{rel} tells a reader to run:\n  " + "\n  ".join(wrong)
        + f"\n\n  The file is at {rel}. Every one of those commands exits "
        '"no such file or directory" when copied from the file that prints them.'
    )


# =======================================================================================
# A FILE THAT WAS SPLIT IN TWO, WHICH IS NOT THE DEFECT THE CHECKS ABOVE LOOK FOR
# =======================================================================================
#
# Commit `b39ea3b` replaced `medos/api/v1/routes.yaml` with `routes.core.yaml` and
# `routes.train.yaml`, one registry per product, because `medos/tools/permcheck.py` checks a
# registry against an app and one file could only ever be right about one of the two.
#
# The check above cannot see a citation of the old name. Its predicate is "resolves one level
# down", and `medos/medos/api/v1/routes.yaml` does not exist either -- a SPLIT file resolves
# NOWHERE, at any prefix. MEASURED rather than argued: with all 70 live citations present,
# `test_no_file_cites_a_path_that_only_resolves_one_level_down` was green, and so were
# `test_documented_test_citations.py` (it reads only `test_*.py` names, and skips `docs/spec/`)
# and `test_register_numbers_re_derive.py`. Nothing in the repository could say so, which is
# why `MOS-API-085` kept requiring the file for five days after it stopped existing.
#
# THE EXEMPTIONS ARE PROPERTIES, NOT COUNTS. A count of surviving citations is a number with
# no owner and it goes stale the first time somebody edits a paragraph -- that is register
# entry 140's defect, and "state the property, date the count" is the rule this suite settled
# on. So each exempt file declares WHAT MAKES its citations legitimate, and the check reads
# that property at every site:
#
#   * a chapter that AMENDED its requirement keeps the old spelling inside `~~...~~`. That is
#     what a strikethrough is. A gate that forbade the substring would forbid its own
#     retraction, which this repository has recorded four separate times.
#   * a module whose docstring says the registry "RECORDED this as absent" is describing a
#     state that is gone: all five such modules were created before the split (`6896ffa`,
#     `b7615dd`, against `b39ea3b`), so `routes.yaml` was the file's real name when the
#     sentence was written, and `engine_absent:` is no longer a key in either registry.
#   * the register and the applied migrations are history by kind.

#: The split, and what replaced it. Both targets are asserted to exist, so this table cannot
#: name a phantom and go on passing.
SPLIT_FILE = "api/v1/routes.yaml"
SPLIT_TARGETS = ("medos/api/v1/routes.core.yaml", "medos/api/v1/routes.train.yaml")

#: A citation of the split file is legitimate in these files, for this property. `WHOLE_FILE`
#: means the file is history or immutable by kind; otherwise every citation's own line must
#: carry the named property.
STRUCK = "struck"
PAST = "past-tense"
WHOLE_FILE = "whole-file"

SPLIT_EXEMPT: dict[str, tuple[str, str]] = {
    "docs/spec/08-security.md": (
        STRUCK, "`MOS-SEC-032` amended at 0.4.0; the old spelling is its strikethrough"),
    "docs/spec/09-clinical-safety.md": (
        STRUCK, "`MOS-SAFE-069` amended at 0.4.0"),
    "docs/spec/10-api.md": (
        STRUCK, "`MOS-API-085` and `MOS-API-086` amended at 0.4.0"),
    "docs/spec/99-known-inconsistencies.md": (
        WHOLE_FILE, "the register: measured history, entries 88, 89, 114 and 130"),
    "MEDICALOS_SPEC.md": (
        WHOLE_FILE,
        "the requirement index carries the 0.3.0 wording for an amended requirement and says "
        "so in the row; nothing derives it from the chapters"),
    "medos/medos/db/migrations/0015_seal_runs.up.sql": (
        WHOLE_FILE,
        "an applied migration. `tests/unit/test_migration_immutability.py` asserts each one "
        "still holds the bytes of the commit that added it"),
    "medos/tools/contracts.py": (
        PAST, "the account OF the split: \"`medos/api/v1/routes.yaml` was a single file\""),
    "medos/medos/evidence/repo.py": (PAST, "\"recorded every one of these as absent\""),
    "medos/medos/training/curation.py": (PAST, "\"recorded this as absent\", twice"),
    "medos/medos/training/retrieval.py": (
        PAST, "\"recorded this as the third of R26's three absences\""),
    "medos/medos/training/split.py": (
        PAST, "the same, and another session holds this file"),
    "tests/unit/test_moved_tree_citations.py": (
        WHOLE_FILE,
        "THIS MODULE. Every occurrence here is the subject under description -- the constant, "
        "the regex built from it, and the prose explaining what was split. The check flagged "
        "its own documentation on the first run, which is the shape four register entries "
        "record: a gate keyed to a substring forbids the paragraph that explains the "
        "substring. The exemption is not blanket, though: "
        "`test_the_split_check_is_keyed_to_the_constant` below asserts that the pattern is "
        "built from SPLIT_FILE, so the check cannot be re-pointed at some other string while "
        "the prose here goes on describing this one"),
    "tests/integration/test_api_curation.py": (
        WHOLE_FILE,
        "DEFERRED, not exempt: another session holds uncommitted changes here, and editing it "
        "would mix this change into their diff. Register entry 123 is the precedent"),
}

#: The words that make a sentence about the past a sentence about the past. Small and
#: declared, because `\bwas\b` alone would also pass "the registry was updated today".
PAST_MARKERS = ("recorded", "was a single file")

SPLIT_RE = re.compile(r"(?<![a-z.])(?:medos/)?" + re.escape(SPLIT_FILE))
STRUCK_RE = re.compile(r"~~[^~]*?api/v1/routes\.yaml[^~]*?~~")


def _split_citations() -> dict[str, list[tuple[int, str]]]:
    """file -> [(lineno, line)] for every citation of the split file, tracked or not."""
    out: dict[str, list[tuple[int, str]]] = {}
    for rel in _tracked():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = [
            (i, line)
            for i, line in enumerate(text.splitlines(), 1)
            if SPLIT_RE.search(line)
        ]
        if hits:
            out[rel] = hits
    return out


def test_the_split_targets_exist() -> None:
    """Otherwise this table names a phantom and the check below is vacuous."""
    for target in SPLIT_TARGETS:
        assert (ROOT / target).is_file(), (
            f"{target} does not exist, so the registry this module says replaced "
            f"{SPLIT_FILE} is not there. Either the split was undone or this table is wrong."
        )
    assert not (ROOT / "medos" / SPLIT_FILE).exists(), (
        f"medos/{SPLIT_FILE} exists again. If the split was reverted, `MOS-API-085` and this "
        f"module both need amending, in that order."
    )


def test_no_file_cites_the_split_registry_as_a_live_address() -> None:
    offenders: list[str] = []
    for rel, hits in _split_citations().items():
        if rel not in SPLIT_EXEMPT:
            offenders.extend(f"{rel}:{n}" for n, _ in hits)
            continue
        kind, _why = SPLIT_EXEMPT[rel]
        if kind == WHOLE_FILE:
            continue
        for n, line in hits:
            if kind == STRUCK and STRUCK_RE.search(line):
                continue
            if kind == PAST and any(m in line for m in PAST_MARKERS):
                continue
            offenders.append(f"{rel}:{n}  (exempt as {kind}, but this line is not)")
    assert not offenders, (
        "these cite `" + SPLIT_FILE + "`, which commit b39ea3b replaced with "
        + " and ".join(SPLIT_TARGETS) + ":\n  "
        + "\n  ".join(offenders)
        + "\nA reader following one finds nothing, at any prefix. Name the registry of the "
        "product that owns the row -- every R-series row is in routes.train.yaml and "
        "routes.core.yaml declares none of them -- or, if the sentence is about the past, "
        "say so in it and add the file to SPLIT_EXEMPT with the property that makes it so."
    )


def test_the_split_pattern_matches_the_old_name_and_neither_new_one() -> None:
    """The teeth behind this module's own whole-file exemption.

    THE FIRST VERSION OF THIS TEST COULD NOT FAIL, and it was proof-by-breaking that said so
    rather than reading it. It asserted `"re.escape(SPLIT_FILE)" in source` against this
    module's own text -- and that string occurs twice here, once in the derivation being
    checked and once inside the assertion doing the checking. The assertion found itself.
    Deleting `SPLIT_RE`'s derivation outright would have left it green. It is removed rather
    than tuned until it passes, and this paragraph stands where it was.

    What is checked instead is BEHAVIOUR, which a source-text search cannot fake:

      * the pattern matches both spellings of the file that was split, bare and prefixed. A
        pattern that matched neither would make the whole check pass by not looking, and the
        module's whole-file exemption would then cover occurrences nobody is looking for.
      * the pattern matches NEITHER replacement. This is the half that would do damage rather
        than nothing: a pattern loose enough to match `routes.core.yaml` would flag all 59
        citations the repair just corrected, and the obvious response to that failure is to
        exempt the files, which would put the defect back under an exemption.
    """
    assert SPLIT_RE.search(SPLIT_FILE), (
        f"the pattern does not match the bare spelling {SPLIT_FILE!r} of the file it names, "
        "so this check passes by not looking"
    )
    assert SPLIT_RE.search("medos/" + SPLIT_FILE), (
        "the pattern does not match the prefixed spelling, which is how 79 of the 82 citations "
        "were written"
    )
    for target in SPLIT_TARGETS:
        assert not SPLIT_RE.search(target), (
            f"the pattern matches {target}, one of the files that REPLACED the split one. A "
            "check that flags the correct name reports every repaired citation as a defect, "
            "and the natural response to that is an exemption that hides the real ones."
        )


def test_every_split_exemption_still_has_the_citation_it_exempts() -> None:
    """The other half: a frozen table whose subject has gone is the shape entry 140 records."""
    citations = _split_citations()
    stale = sorted(rel for rel in SPLIT_EXEMPT if rel not in citations)
    assert not stale, (
        f"{stale} are exempted from the split-registry check and no longer cite it at all. "
        f"Drop the rows: an exemption nobody needs reads as a defect somebody tolerated."
    )


#: (chapter, requirement) amended at 0.4.0 because it named the split file. PER REQUIREMENT,
#: not per file: the first version of the check below searched `docs/spec/10-api.md` for any
#: struck citation, and that file carries two -- `MOS-API-085`'s and `MOS-API-086`'s -- so
#: opening one of them back into a live address left the other and the check stayed green.
AMENDED_FOR_THE_SPLIT: tuple[tuple[str, str], ...] = (
    ("docs/spec/10-api.md", "MOS-API-085"),
    ("docs/spec/10-api.md", "MOS-API-086"),
    ("docs/spec/09-clinical-safety.md", "MOS-SAFE-069"),
    ("docs/spec/08-security.md", "MOS-SEC-032"),
)


@pytest.mark.parametrize(("rel", "req"), AMENDED_FOR_THE_SPLIT)
def test_an_amended_requirement_still_carries_its_own_strikethrough(rel: str, req: str) -> None:
    """Stated positively, because the negative form is a trap this suite has fallen into.

    A check that banned the old spelling would ban the retraction of the old spelling, and four
    register entries record that shape. The obligation is the opposite one: a requirement
    amended because it named the split file MUST still show what it struck, or a reader holding
    a 0.3.0 copy cannot tell what changed and the amendment is indistinguishable from a quiet
    correction.
    """
    text = (ROOT / rel).read_text(encoding="utf-8")
    m = re.search(rf"\*\*{re.escape(req)}\*\*.*?(?=\n\n\*\*MOS-|\Z)", text, re.S)
    assert m, f"{rel} no longer states {req}"
    assert STRUCK_RE.search(m.group(0)), (
        f"{req} in {rel} was amended because it named the split registry, and its own text no "
        f"longer shows the struck original. Searching the whole FILE for a strikethrough is "
        f"not enough -- 10-api.md carries two, and losing one leaves the other."
    )
