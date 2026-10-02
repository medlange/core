# SPDX-License-Identifier: Apache-2.0
"""What the release-0.1.0 gate actually covers, item by item, and what it does not.

WHY THIS FILE EXISTS
--------------------
`pytest -m gate_0_1_0 --require-stack` reported `24 passed`, exit 0, while `MOS-SAFE-089a`
was unmet: the OHIF toolbar button is a MUST for 0.1 whenever it ships, and the extension
cannot load into the pinned `ohif/app:v3.9.2` image at all. The gate could not see it
because the only check that would have caught it is Chapter 9's acceptance check 24,
whose text begins "With a study open in the viewer" -- a human with a browser.

The defect is not the button. It is that a release gate went green while a Tier B MUST was
unmet and said nothing, and nothing in the repository knew the difference between
"checked and passing" and "not checked". That will happen again on a different requirement
unless the gate is made to say which of the release's contents it actually covers.

So this module is the register: every item of `docs/spec/15-delivery.md` 15.1.2's contents
column for 0.1.0, every row of 15.1.3's tier table for 0.1.0, and for each one the honest
answer to a single question --

    IF THIS ITEM WERE ABSENT OR BROKEN, WOULD THAT RELEASE'S GATE GO RED?

It is the same device `tests/_support/stack.py` already settled on for compose services:
`UNCOVERED` there names every service with no probe and the reason, and a unit test fails
when a service appears in neither table, so "nobody classified it" stops being possible.
Here, `tests/unit/test_release_criteria.py` parses 15.1.2 and 15.1.3 out of the
specification and fails when an item appears that this register does not classify. The
register cannot drift behind the spec, and a new release item cannot arrive without
someone writing down whether a check covers it.

FOUR RELEASES NOW, AND ONLY ONE OF THEM IS REGISTERED IN FULL
--------------------------------------------------------------
The module was written for 0.1.0 and 0.1.0 is still the only release whose contents and
tier rows are classified item by item. What changed is that 15.1.2 and 15.1.3 now carry
chapter 19's operator-surface elements in all four rows, ingested from `MOS-UI-365` and
tiered from `MOS-UI-371`, and every one of THOSE is registered here. So the coverage is:

    0.1.0   every contents item and every tier row, as before, plus the six
            chapter-19 elements the ingestion added to each table
    0.2.0   the chapter-19 elements only. The evidence-plane and training-pipeline
            items of that row remain unclassified and are counted, not named, in
            `UNREGISTERED_CONTENTS` / `UNREGISTERED_TIERS` so the debt cannot grow
    0.3.0   the same
    0.4.0   the same, and nothing in that row is built at all

`tests/unit/test_release_criteria.py` holds the ingested half to the specification by
matching on the requirement id: EVERY entry of 15.1.2 or 15.1.3 that cites a `MOS-UI-`
requirement MUST have a criterion here, in table order. That is the property that matters,
because the failure this whole ingestion repairs was chapter 19 placing work in a release
row that never listed it. An element cannot now be added to a row and left unclassified.

THE FIVE VERDICTS
-----------------
``GATE``    A check in THAT ITEM'S OWN release gate row fails if the item is absent or
            wrong. This is the only verdict that makes a green gate mean anything about
            the item.
``SUITE``   An automated test exists, but OUTSIDE that release's gate selection -- either
            in the ordinary suite, or in a LATER release's gate row, which gates a later
            tag and not this one. The full suite covers it; this release gate does not.
            `MOS-REL-004` gates the TAG on the gate row, so this is real coverage of the
            product and no coverage of the tag.
``MANUAL``  Verified only by a human procedure -- a browser, an eye, a network capture.
``NONE``    Nothing verifies it, anywhere.
``CUT``     Declared cut for this release under `MOS-REL-009`, so the criterion is
            inapplicable rather than failed. Permitted for Tier B and Tier C only;
            `MOS-REL-005` forbids it absolutely for Tier A.

`CUT` was vacuous until this revision and is now the commonest verdict in the register,
which is the whole point of the ingestion. `MOS-UI-365` assigns eighteen operator-surface
elements to 0.2.0 and 0.3.0; both releases were declared complete without them. Before the
ingestion that was an omission -- the items were in no contents cell, so no response under
`MOS-REL-009` was available and none was recorded. After it, each is a cut with a tier, a
Release Decision Record entry and, for Tier B, a named release it MUST reappear in
(`MOS-REL-005`). Nothing shipped that had not shipped before. What changed is that the
repository now says so, and `MOS-UI-013a`'s precedent applies: a cut element's acceptance
criterion is inapplicable rather than failed, and the cut MUST NOT be recorded as a pass.

THE RETURN CHAIN ENDS IN A WITHDRAWAL (2026-10-02)
---------------------------------------------------
Every Tier B cut of 0.2.0 and 0.3.0 that names reappearance at the next release was an
element of the no-code engineering surface of chapter 19 §19.3. At specification 0.4.0 the
surface itself was withdrawn (`MOS-UI-100` CUT, §19.3 CUT; register entries 83 and 150), and
`MOS-REL-005`'s return is discharged by that withdrawal, recorded in the 0.4.0 rows of
15.1.2 and 15.1.3 and in criterion `ui040-returning-set-from-030` below. The `reappears_in`
fields throughout name the release where the return question lands, which is 0.4.0, and the
decision recorded there is the withdrawal. They are left in place because the cuts they
annotate were taken at their own releases, and history is not rewritten.

`GATE` is not a synonym for "well tested". Several items below are far better covered by
`tests/integration/` than by the gate, and are still `SUITE`: the question this register
answers is what the TAG is gated on, not what the repository knows.

WHY THE GAPS ARE FROZEN RATHER THAN FAILED
-------------------------------------------
Three Tier A items were `SUITE` rather than `GATE` before the chapter-19 ingestion, and
thirteen are now. Turning that into a red gate would be the wrong move twice over: it
would fail a gate over test placement rather than over product behaviour, and a gate that
is red for a reason `MOS-REL-009` has no response for is a gate people learn to bypass.
Instead the set is FROZEN: `tests/unit/test_release_criteria.py` asserts the Tier A items
lacking gate coverage are exactly the ones named here, so the existing debt is visible on
every run and a NEW Tier A gap is a test failure. Debt that cannot grow is debt that gets
paid.

THREE OF THE THIRTEEN ARE WORSE THAN UNGATED: THEY ARE UNBUILT
----------------------------------------------------------------
`MOS-REL-005` says Tier A "MUST NOT be cut under any circumstance", and `MOS-REL-009`
offers no response that cuts one, so an uncuttable item that was not built is a RELEASE
DEFECT and has no verdict that makes it go away. Three are recorded, each measured rather
than inferred, and none of them is retiered to make the arithmetic close:

  `ui010-safe012-adjacency-set`          0.1.0. `MOS-UI-345` requires all five members of
                                         the `MOS-SAFE-012` set adjacent and without
                                         interaction. `legal_manufacturer` and
                                         `review_status` occur nowhere under `medos/web/`.
  `ui020-ruo-badge-and-research-gating`  0.2.0. The badge renders; the `include_research`
                                         gating half has no implementation on either side
                                         -- the string occurs nowhere in `medos/medos/`,
                                         `medos/web/` or `tests/`.
  `ui030-honest-metric-rule`             0.3.0. In force and vacuously true, because no
                                         surface renders a figure at all, and executed by
                                         nothing: `headline-partition` has no module.

Reporting them is the point. Retiering one to B so that `MOS-REL-009` would admit a cut is
the single move this register exists to make impossible, and it is what the 0.1.0 record
already refused once over the six untiered items (register entry 61).

WHAT THIS MODULE DOES NOT DO
-----------------------------
It does not assign tiers. 15.1.3's table for 0.1.0 tiers twelve items while 15.1.2's
contents column lists thirteen, and six contents items -- de-identification, study triage,
the queue driver, the vendored segmentation, API-key auth and "Single Triton" -- appear in
no tier row at all. `MOS-REL-005` requires every contents item to carry exactly one tier
and 15.3's conformance item 1 says a parser "fails if any contents item has no tier". That
is a defect in the SPECIFICATION and it is reported as one: `tier` is `None` for those
items and `UNTIERED_CONTENTS` names them, rather than this file inventing an answer the
spec does not state.

Spec: MOS-REL-003, MOS-REL-004, MOS-REL-005, MOS-REL-008, MOS-REL-009, MOS-REL-012,
MOS-SAFE-088, MOS-SAFE-089a.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "RELEASE",
    "RELEASES",
    "SPEC",
    "Criterion",
    "CRITERIA",
    "UNREGISTERED_CONTENTS",
    "UNREGISTERED_TIERS",
    "UNTIERED_CONTENTS",
    "TIER_A_WITHOUT_GATE_COVERAGE",
    "Verdict",
    "by_slug",
    "cut_criteria",
    "for_release",
    "ingested_marker",
    "not_machine_verified",
    "parse_contents_items",
    "parse_tier_items",
    "report_lines",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "docs" / "spec" / "15-delivery.md"

#: The one release this register classifies in full.
RELEASE = "0.1.0"

#: Every release 15.1.2 gives a row to, in table order. The chapter-19 ingestion put
#: elements in all four, so the register is no longer single-release.
RELEASES: tuple[str, ...] = ("0.1.0", "0.2.0", "0.3.0", "0.4.0")

#: The substring that marks a 15.1.2 or 15.1.3 entry as one chapter 19 placed there. The
#: register is held to the specification by this and not by a hand-kept list: an element
#: added to a release row citing a `MOS-UI-` requirement and left unclassified is a test
#: failure, which is exactly the shape of the defect this ingestion repairs.
def ingested_marker() -> str:
    return "MOS-UI-"


Verdict = Literal["GATE", "SUITE", "MANUAL", "NONE", "CUT"]

#: Ordered worst-first, for the report.
VERDICT_ORDER: tuple[Verdict, ...] = ("NONE", "MANUAL", "SUITE", "CUT", "GATE")

VERDICT_MEANING: dict[Verdict, str] = {
    "GATE": "a check in this item's OWN release gate row fails if the item is absent",
    "SUITE": "automated, but outside that row: the tag for that release is not gated on it",
    "MANUAL": "verified only by a human procedure",
    "NONE": "nothing verifies it, anywhere",
    "CUT": "declared cut under MOS-REL-009; inapplicable rather than failed",
}


# ======================================================================================
# Parsing the specification. The register is checked against THIS, not against prose.
# ======================================================================================
def _row(prefix: str) -> list[str]:
    rows = [
        line
        for line in SPEC.read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]
    if len(rows) != 1:
        raise AssertionError(
            f"docs/spec/15-delivery.md has {len(rows)} rows starting {prefix!r}; the "
            f"release contents cannot be derived from an ambiguous table"
        )
    return [c.strip() for c in rows[0].split("|")]


def _clean(fragment: str) -> str:
    """One item's text, normalised for comparison.

    The em dash cut matters for exactly one row: 15.1.3's Tier B cell attaches a
    paragraph of rationale to `OHIF toolbar button` explaining why it is Tier B and not
    Tier C. The ITEM is the words before the dash; the rest is argument, and keying a
    register on an argument makes every copy-edit a test failure.
    """
    text = fragment.split("—")[0]
    return re.sub(r"\s+", " ", text).strip(" .")


def parse_contents_items(release: str = RELEASE) -> list[str]:
    """15.1.2's contents column for `release`, split into its items.

    Split on `;` and on a sentence boundary before a capital: the 0.1.0 cell ends
    "...audit. Single Triton, docker compose.", which is two items and one semicolon
    short of being three.
    """
    cells = _row(f"| **{release}**")
    if len(cells) != 5:
        raise AssertionError(
            f"15.1.2's row for {release} has {len(cells) - 2} columns, not 3"
        )
    parts = re.split(r";|\.\s+(?=[A-Z])", cells[2])
    return [_clean(p) for p in parts if _clean(p)]


def parse_tier_items(release: str = RELEASE) -> list[tuple[str, str]]:
    """15.1.3's tier row for `release`, as `(tier, item)` pairs in table order."""
    cells = _row(f"| {release} |")
    if len(cells) != 6:
        raise AssertionError(
            f"15.1.3's row for {release} has {len(cells) - 2} columns, not 4"
        )
    out: list[tuple[str, str]] = []
    for tier, cell in zip(("A", "B", "C"), cells[2:5]):
        for part in cell.split(";"):
            item = _clean(part)
            if item:
                out.append((tier, item))
    return out


# ======================================================================================
# The register
# ======================================================================================
@dataclass(frozen=True)
class Criterion:
    """One item of 15.1.2 or 15.1.3, and what actually stands behind it."""

    #: Stable identifier. Never parsed from the spec, so a copy-edit does not rename it.
    slug: str
    #: The spec's own words, normalised. Compared against the parsed table.
    text: str
    #: "15.1.2" (contents) or "15.1.3" (tier table).
    origin: Literal["15.1.2", "15.1.3"]
    #: A/B/C, or None where 15.1.3 assigns none -- see UNTIERED_CONTENTS.
    tier: str | None
    verdict: Verdict
    #: The 15.1.2 / 15.1.3 row this item belongs to. Defaults to the one release the
    #: register classifies in full, so the original entries read as they always did.
    release: str = RELEASE
    #: Gate check names, or test node ids, that stand behind the verdict.
    covered_by: tuple[str, ...] = ()
    #: What is NOT verified. Empty only when the item is fully covered by `covered_by`.
    gap: str = ""
    #: For CUT: the MOS-REL-009 response and where it must be recorded.
    cut: str = ""
    #: For a Tier B cut, MOS-REL-005's "MUST reappear as Tier A or B of the next release".
    reappears_in: str = ""
    _notes: tuple[str, ...] = field(default=(), repr=False)

    @property
    def machine_verified_by_the_gate(self) -> bool:
        return self.verdict == "GATE"

    @property
    def label(self) -> str:
        return f"[{self.release} {self.tier or '-'}] {self.text}"


#: 15.1.2's contents column, in table order. One entry per parsed item; the parser above
#: and `tests/unit/test_release_criteria.py` hold this list to the spec's own wording.
_CONTENTS: tuple[Criterion, ...] = (
    Criterion(
        slug="gateway-and-deid",
        text="DICOM Gateway + de-identification",
        origin="15.1.2",
        tier=None,
        verdict="GATE",
        covered_by=(
            "tests/gate/conftest.py::stack (unauthenticated QIDO-RS is 401)",
            "--require-stack probe `medos-gateway`",
            "tenant-isolation (403 on a direct QIDO-RS for another tenant)",
        ),
        gap=(
            "the DE-IDENTIFICATION half of this item is not delivered and nothing checks "
            "that it is not: medos/medos/gateway/app.py refuses egress for any consumer class "
            "that would need it with 503 DEID_NOT_IMPLEMENTED (MOS-DATA-037's fail-closed "
            "rule applied to an unimplemented stage), and NO test anywhere asserts that "
            "refusal. Widening _NO_DEID_CLASSES would serve identified pixels to a class "
            "that must not have them, and the gate, the integration suite and the e2e "
            "suite would all stay green."
        ),
    ),
    Criterion(
        slug="triage-and-series-selector",
        text="study triage + `SeriesSelector`",
        origin="15.1.2",
        tier=None,
        verdict="GATE",
        covered_by=(
            "provenance-replay (series_consumed is exactly the ingested CT series and "
            "instances_consumed equals its instance count)",
            "rejection-distinct (a study with no eligible series terminates REJECTED)",
        ),
    ),
    Criterion(
        slug="job-and-queue-driver",
        text="`Job` + Postgres queue driver",
        origin="15.1.2",
        tier=None,
        verdict="GATE",
        covered_by=(
            "idempotency-three-surface (exactly one job.completed event per job)",
            "every gate check runs through a real QUEUED -> COMPLETED job",
        ),
        gap=(
            "DURABILITY is not gated. MOS-REL-008's own example of a real gate is 'the "
            "job is durably queued and survives a SIGKILL of the claiming process', and "
            "no gate check kills anything: idempotency-three-surface's crash arm "
            "reproduces the post-crash STATE by truncating the job tables, deliberately "
            "and for good reasons, but it never exercises lease expiry or reclaim. "
            "tests/integration/test_queue.py and test_worker.py do, outside the gate."
        ),
    ),
    Criterion(
        slug="geometry-contract",
        text="geometry contract",
        origin="15.1.2",
        tier="A",
        verdict="GATE",
        covered_by=(
            "dicom-battery (origin/spacing/direction within 1e-4 of source)",
            "dicom-battery (FrameOfReferenceUID string equality)",
        ),
    ),
    Criterion(
        slug="seg-sr-writing-deterministic-uids",
        text="platform-side SEG/SR writing with deterministic UIDs",
        origin="15.1.2",
        tier="A",
        verdict="GATE",
        covered_by=(
            "dicom-battery (dciodvfy zero errors; SEG read-back Dice 1.0; SR is TID 1500)",
            "idempotency-three-surface (one SeriesInstanceUID per generated object kind, "
            "read from the PACS)",
        ),
    ),
    Criterion(
        slug="native-service-pleural-effusion",
        text="one native service (pleural effusion, own data)",
        origin="15.1.2",
        tier="B",
        verdict="GATE",
        covered_by=(
            "idempotency-three-surface (exactly one `results` row for pleural_effusion)",
        ),
        gap=(
            "THE GATE IS SATISFIED BY A DECLARED PLACEHOLDER. "
            "medos/medos/capabilities/pleural_effusion.py registers method_class "
            "'not_implemented', parameters['implemented'] is False and output_kinds is "
            "empty; it returns present=False with an unresolvable finding kind, honestly "
            "and on purpose (CONTRACT.md 7). The gate's surface-1 assertion is 'one "
            "results row per capability', which the placeholder produces. So this "
            "contents item -- a native service trained on own data -- is NOT delivered, "
            "and no gate check would notice. tests/unit/test_capabilities.py asserts the "
            "placeholder is LABELLED honestly, which is a different claim."
        ),
    ),
    Criterion(
        slug="vendored-lung-segmentation",
        text="vendored lung segmentation",
        origin="15.1.2",
        tier=None,
        verdict="GATE",
        covered_by=(
            "idempotency-three-surface (exactly one `results` row for lung_segmentation)",
            "dicom-battery (the SEG it produced reads back at Dice 1.0 on the source grid)",
        ),
    ),
    Criterion(
        slug="emphysema-laa-deterministic",
        text="`emphysema_laa` as a deterministic measurement, not a learned model",
        origin="15.1.2",
        tier="B",
        verdict="GATE",
        covered_by=(
            "idempotency-three-surface (exactly one `results` row for emphysema_laa)",
        ),
        gap=(
            "the qualifier is not gated. 'deterministic' is never measured -- no gate "
            "check runs the capability twice on the same volume and compares -- and 'not "
            "a learned model' is asserted only by "
            "tests/unit/test_capabilities.py::test_emphysema_laa_is_not_labelled_as_a_"
            "model, which reads the declared metadata rather than the code. A learned "
            "model wired in behind the same capability id would pass the gate."
        ),
    ),
    Criterion(
        slug="ohif-button-and-provenance-panel",
        text="OHIF toolbar button + provenance panel",
        origin="15.1.2",
        tier="B",
        verdict="SUITE",
        covered_by=(
            "tests/e2e/test_viewer_extension.py (the extension is loaded in the served "
            "viewer, by bundle or by runtime import, and no substitute is passed off as "
            "it)",
            "tests/e2e/test_demo.py (the panel's required-field list matches the API; "
            "REJECTED renders distinct from FAILED)",
        ),
        gap=(
            "THIS IS THE ITEM THAT WENT GREEN WHILE UNMET, and it is still not gated. No "
            "gate check touches the viewer at all -- `ohif` is deliberately not in "
            "tests/gate's declared dependency set, and adding it would make the gate red "
            "for a Tier B dependency none of the five checks uses. The load question is "
            "now machine-checkable (it was not when the gate last went green with this "
            "unmet), but it is checked in the e2e suite, so `-m gate_0_1_0` still cannot "
            "see this item. The rest of acceptance check 24 -- one activation, one POST, "
            "the job rendering inline from the SSE stream, a network capture -- needs a "
            "browser and is verified by nobody between releases."
        ),
    ),
    Criterion(
        slug="api-key-auth",
        text="API-key auth",
        origin="15.1.2",
        tier=None,
        verdict="SUITE",
        covered_by=(
            "tests/integration/test_auth.py (no key is 401; a bad, expired or revoked key "
            "is 401; argon2id at MOS-SEC-010's parameters; the public paths are exactly "
            "three)",
        ),
        gap=(
            "the gate NEVER presents a bad credential. Every gate request carries the one "
            "valid key the `api` fixture mints, so no refusal path is exercised: an "
            "authenticator that accepted an expired key, a revoked key, or a key minted "
            "for another environment would leave all 24 gate checks green. The only "
            "no-credential assertion in the gate is against the GATEWAY "
            "(tests/gate/conftest.py::stack), not against the control plane."
        ),
    ),
    Criterion(
        slug="tenant-id-and-rls",
        text="`tenant_id` + Postgres RLS",
        origin="15.1.2",
        tier="A",
        verdict="GATE",
        covered_by=("tenant-isolation (403 on the REST API and 403 on a direct QIDO-RS)",),
        gap=(
            "the HTTP boundary is gated; ROW-LEVEL SECURITY is not. Both 403s are produced "
            "by explicit code -- medos/medos/security/authn.py's CROSS_TENANT_DENIED and the "
            "Gateway's MOS-DATA-009 segment comparison -- so `ALTER TABLE ... DISABLE ROW "
            "LEVEL SECURITY` across the schema would leave every gate check green. The "
            "gate opens one connection as the BOOTSTRAP role, which bypasses RLS by "
            "design, and uses `medicalos_app` only to ARRANGE the other tenant's job. "
            "tests/integration/test_tenancy.py asserts FORCE ROW LEVEL SECURITY on every "
            "tenant-owned table, outside the gate."
        ),
    ),
    Criterion(
        slug="audit",
        text="audit",
        origin="15.1.2",
        tier="A",
        verdict="SUITE",
        covered_by=(
            "tests/integration/test_audit_provenance.py (INSERT/SELECT only for the "
            "application role; UPDATE and DELETE both fail; every partition hardened; the "
            "chain verifies and a forged row is reported at its sequence)",
            "tests/integration/test_gateway.py (one PHI-access row per retrieval)",
        ),
        gap=(
            "A TIER A ITEM WITH NO GATE CHECK AT ALL. The string `audit` does not appear "
            "in any assertion in tests/gate/ -- only in one comment. Dropping the audit "
            "table, removing the append-only grant, or silencing the Gateway's PHI-access "
            "write would not turn a single one of the 24 gate checks red. MOS-REL-005 "
            "makes this item uncuttable, and the gate cannot tell whether it is there."
        ),
    ),
    Criterion(
        slug="single-triton-docker-compose",
        text="Single Triton, docker compose",
        origin="15.1.2",
        tier=None,
        verdict="GATE",
        covered_by=(
            "--require-stack probes `triton` and `medos-tritond` before any gate test is "
            "allowed to pass",
            "tests/unit/test_stack_contract.py (every compose service is probed or "
            "explicitly excused)",
        ),
        gap=(
            "reachability is gated; the inference PATH is not. No gate check asserts that "
            "a capability's result came from a versioned model reference resolved at "
            "dispatch, so a worker that silently fell back to a local computation with "
            "Triton still running would pass."
        ),
    ),
    # ----------------------------------------------------------------------------------
    # The chapter-19 ingestion. `MOS-UI-365` placed these in the 0.1.0 row and 15.1.2 had
    # never listed them; `MOS-UI-371` tiers the first three A and the last three B.
    # ----------------------------------------------------------------------------------
    Criterion(
        slug="ui010-rejected-failed-four-channels",
        text=(
            "and the operator-surface elements Chapter 19 places in this release "
            "(`MOS-UI-365`): the `REJECTED`/`FAILED` distinction carried on four "
            "independent channels (`MOS-UI-337`)"
        ),
        origin="15.1.2",
        release="0.1.0",
        tier="A",
        verdict="SUITE",
        covered_by=(
            "tests/e2e/test_demo.py::test_rejected_renders_visually_distinct_from_failed "
            "(all four channels: hue, box treatment, heading weight, wording, plus the "
            "reason code verbatim in a <code> element and no transform applied to it)",
        ),
        gap=(
            "OUTSIDE THE GATE, and STRUCTURAL. `rejection-distinct` is in the 0.1.0 row "
            "and covers the platform half -- the job terminates REJECTED and not FAILED "
            "with a machine-readable reason -- and no gate check touches the viewer at "
            "all. The four-channel claim is asserted by reading the served render.js and "
            "styles.css, not by rendering: chapter 19's acceptance check 2 asks for both "
            "states rendered with the stylesheet DISABLED and again in a greyscale-forced "
            "profile, and neither arm exists anywhere in this repository. A stylesheet "
            "that failed to load would leave the CSS assertions green and the reader "
            "with two identical grey boxes."
        ),
    ),
    Criterion(
        slug="ui010-safe012-adjacency-set",
        text=(
            "the `MOS-SAFE-012` adjacency set rendered without interaction on every "
            "result-bearing surface (`MOS-UI-345`)"
        ),
        origin="15.1.2",
        release="0.1.0",
        tier="A",
        verdict="NONE",
        covered_by=(),
        gap=(
            "A TIER A ITEM THAT IS NOT BUILT, AND MOS-REL-005 ADMITS NO CUT FOR IT. "
            "MOS-UI-345 requires all five members -- service_id, the ServiceVersion, "
            "legal_manufacturer.name, the clinical_use_mode of the producing deployment "
            "and the review_status of the Result -- adjacent to the finding, in the "
            "initial render, with zero interaction and zero extra requests. MEASURED: "
            "`legal_manufacturer` and `review_status` occur NOWHERE under medos/web/. "
            "provenance.js's REQUIRED_JOB_FIELDS carries service_id and service_version "
            "and render.js carries clinical_use_mode; the other two are absent from the "
            "field lists, from the renderer and from the panel-contract test that parses "
            "those lists, so the one check that would notice is checking a list that does "
            "not name them. Three of five is not the MOS-SAFE-012 set. Reported as a "
            "release defect rather than retiered: MOS-SAFE-053 puts both fields at the "
            "top level of the Result precisely so a client cannot omit them by accident."
        ),
    ),
    Criterion(
        slug="ui010-refusal-rendering",
        text=(
            "the clinician surface's three-part refusal rendering with the "
            "machine-readable code secondary (`MOS-UI-330`, `MOS-UI-331`)"
        ),
        origin="15.1.2",
        release="0.1.0",
        tier="A",
        verdict="SUITE",
        covered_by=(
            "tests/e2e/test_demo.py::test_rejected_renders_visually_distinct_from_failed "
            "(REJECTED_COPY and FAILED_COPY each carry title, body and tail, and the "
            "reason code reaches a <code> element from the API field with no transform)",
        ),
        gap=(
            "only the two TERMINAL OUTCOMES, and outside the gate. MOS-UI-330 requires a "
            "three-part record for every blocking gate reachable from the surface; what "
            "exists is two frozen copy objects in render.js for REJECTED and FAILED. "
            "Nothing enumerates the other reachable gates -- that is the catalogue item, "
            "`ui010-refusal-catalogue`, and it is cut -- and nothing asserts MOS-UI-331's "
            "ORDER or that `code`, `type`, `class` and `trace_id` render visually "
            "secondary. The tail string 're-running will not change the outcome' is doing "
            "the work of `what_would_resolve_it` for one gate and no other."
        ),
    ),
    Criterion(
        slug="ui010-configured-capability-list",
        text="submission against a fixed configured capability list (`MOS-UI-366`)",
        origin="15.1.2",
        release="0.1.0",
        tier="B",
        verdict="SUITE",
        covered_by=(
            "capability-reachable property 5 (every capability the deployment serves is "
            "offered to a reader, read from the bytes the medos-web container serves)",
            "tests/e2e/test_viewer_extension.py (the client module the list drives is "
            "served and resolvable)",
        ),
        gap=(
            "the check that covers it is in the 0.3.0 ROW, not this one. `-m gate_0_1_0` "
            "collects five modules and none of them reads medicalos-config.js, so the 0.1.0 "
            "TAG is not gated on the reader having anything to submit -- which is the "
            "same shape as the MOS-SAFE-089a failure, one level down. MOS-REL-004 gates "
            "the 0.3.0 tag on this row too, so the item is covered from 0.3.0 onward and "
            "was uncovered for the two releases before it. Tier B and not A by "
            "MOS-REL-005's own test: MOS-UI-366 leaves this list standing until "
            "capability resolution exists, so removing it costs no rewrite."
        ),
    ),
    Criterion(
        slug="ui010-refusal-catalogue",
        text=(
            "the refusal catalogue seeded for this release's gates with its CI "
            "completeness check (`MOS-UI-335`, `MOS-UI-336`)"
        ),
        origin="15.1.2",
        release="0.1.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "nothing verifies it because it does not exist. MEASURED 2026-09-16: no file "
            "in this repository contained `what_is_wrong`, `why_it_blocks` or "
            "`what_would_resolve_it`, so there was no versioned catalogue, no record per "
            "gate id, and no CI job enumerating the gates a surface can reach. Refusal "
            "text lived as two frozen objects inside render.js, which MOS-UI-335 "
            "forbids as a permanent arrangement and which cannot be reviewed as prose by "
            "someone who is not a programmer. THE FIRST CLAUSE WENT FALSE ONCE AND IS "
            "TRUE AGAIN: medos/web/training-console/src/refusals/catalogue.js carried 75 "
            "three-part records between its construction and its deletion on 2026-10-02, "
            "when the engineering surface was withdrawn with §19.3 (register entry 150). "
            "The date is what makes the sentence a correct statement "
            "about 2026-09-16 rather than a wrong one about now, and register entry 83 "
            "holds what it means for this cut."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.1.0.md",
        reappears_in="0.2.0",
    ),
    Criterion(
        slug="ui010-rebuilt-ohif-bundle",
        text="the two surfaces hosted inside a rebuilt OHIF bundle (`MOS-UI-369`)",
        origin="15.1.2",
        release="0.1.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "THE ORIGINAL DEFECT, NOW NAMED AS A CUT INSTEAD OF AS AN OMISSION. "
            "MOS-UI-369 records it as a fact rather than an assumption: "
            "medos/deploy/compose/docker-compose.yml pins the pre-built ohif/app:v3.9.2, OHIF "
            "v3 resolves extensions from its own build-time bundle, medicalos-config.js "
            "therefore ships `extensions: []`, and the rebuild from the OHIF monorepo was "
            "not performed. What runs is the standalone host on the viewer's origin, "
            "importing the identical src/core/*.js modules -- the same job-creation path "
            "and the same renderer, not a mock. MOS-UI-013a is the precedent this "
            "register now applies everywhere: the standalone page satisfies the "
            "BEHAVIOURAL half of MOS-SAFE-089a and not 'for the study currently open in "
            "the viewer', and the cut MUST NOT be recorded as a pass."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.1.0.md",
        reappears_in="0.2.0",
    ),
)


#: 15.1.3's tier table, in table order. Items whose substance is already a contents row
#: carry the same verdict and point at it, so the two tables cannot disagree.
_TIERS: tuple[Criterion, ...] = (
    Criterion(
        slug="tier-a-tenant-id-rls",
        text="`tenant_id` + RLS",
        origin="15.1.3",
        tier="A",
        verdict="GATE",
        covered_by=("tenant-isolation",),
        gap="see criterion `tenant-id-and-rls`: the RLS mechanism itself is SUITE-only",
    ),
    Criterion(
        slug="tier-a-gateway-sole-credential-holder",
        text="DICOM Gateway as the sole PACS credential holder",
        origin="15.1.3",
        tier="A",
        verdict="SUITE",
        covered_by=(
            "tests/integration/test_gateway.py::test_the_pacs_credential_reaches_exactly_"
            "one_deployment_unit",
            "tests/integration/test_gateway.py::test_the_pacs_publishes_no_port_to_this_host",
            "tests/integration/test_gateway.py::test_no_container_but_the_gateway_can_open_"
            "a_tcp_connection_to_the_pacs",
        ),
        gap=(
            "the gate proves the Gateway IS a route and refuses an anonymous one; it "
            "proves nothing about SOLE. A second credential holder -- a PACS password "
            "handed to the worker or baked into the viewer's config -- would not turn any "
            "of the 24 gate checks red. The word that carries the Tier A weight is the "
            "one the gate does not measure."
        ),
    ),
    Criterion(
        slug="tier-a-deterministic-uids-unique",
        text="deterministic UIDs + `UNIQUE (job_id, capability_id)`",
        origin="15.1.3",
        tier="A",
        verdict="GATE",
        covered_by=(
            "idempotency-three-surface (all three surfaces, after a replay AND after the "
            "crash-between-store-and-complete variant)",
        ),
    ),
    Criterion(
        slug="tier-a-append-only-audit",
        text="append-only audit",
        origin="15.1.3",
        tier="A",
        verdict="SUITE",
        covered_by=(
            "tests/integration/test_audit_provenance.py::test_update_and_delete_as_the_"
            "application_role_both_fail",
            "tests/integration/test_audit_provenance.py::test_the_owner_cannot_mutate_the_"
            "audit_trail_either",
        ),
        gap="see criterion `audit`: no gate check references the audit trail at all",
    ),
    Criterion(
        slug="tier-a-geometry-contract",
        text="the geometry contract",
        origin="15.1.3",
        tier="A",
        verdict="GATE",
        covered_by=("dicom-battery",),
    ),
    Criterion(
        slug="tier-a-rejected-terminal-state",
        text="`REJECTED` as a distinct terminal state",
        origin="15.1.3",
        tier="A",
        verdict="GATE",
        covered_by=(
            "rejection-distinct (terminates REJECTED and not FAILED; a rejected job writes "
            "nothing into the archive)",
        ),
    ),
    Criterion(
        slug="tier-a-rfc9457-class-enum",
        text=(
            "RFC 9457 `class` enum separating `clinical_rejection` from transport failure"
        ),
        origin="15.1.3",
        tier="A",
        verdict="GATE",
        covered_by=(
            "rejection-distinct (the rejection is classed `clinical_rejection`, from a "
            "closed enum, in an RFC 9457 document)",
        ),
        gap=(
            "only the clinical_rejection side is gated. No gate check produces a TRANSPORT "
            "failure and asserts it is classed differently, so the SEPARATION -- which is "
            "the whole point of the enum -- rests on one of its two arms. "
            "tests/integration/test_api.py exercises the transport_failure side, outside "
            "the gate."
        ),
    ),
    Criterion(
        slug="tier-ui010-rejected-failed-four-channels",
        text=(
            "the `REJECTED`/`FAILED` distinction on four independent channels "
            "(`MOS-UI-337`)"
        ),
        origin="15.1.3",
        release="0.1.0",
        tier="A",
        verdict="SUITE",
        covered_by=(
            "tests/e2e/test_demo.py::test_rejected_renders_visually_distinct_from_failed",
        ),
        gap=(
            "see criterion `ui010-rejected-failed-four-channels`: structural, on the "
            "served CSS and renderer, and outside every gate row"
        ),
    ),
    Criterion(
        slug="tier-ui010-safe012-adjacency-set",
        text=(
            "the `MOS-SAFE-012` adjacency set on every result-bearing surface "
            "(`MOS-UI-345`)"
        ),
        origin="15.1.3",
        release="0.1.0",
        tier="A",
        verdict="NONE",
        covered_by=(),
        gap=(
            "see criterion `ui010-safe012-adjacency-set`: two of the five members occur "
            "nowhere under medos/web/, and this item is Tier A, so MOS-REL-005 admits no cut"
        ),
    ),
    Criterion(
        slug="tier-ui010-refusal-rendering",
        text="the clinician surface's three-part refusal rendering (`MOS-UI-330`)",
        origin="15.1.3",
        release="0.1.0",
        tier="A",
        verdict="SUITE",
        covered_by=(
            "tests/e2e/test_demo.py::test_rejected_renders_visually_distinct_from_failed",
        ),
        gap=(
            "see criterion `ui010-refusal-rendering`: two terminal outcomes, no "
            "enumeration of the other reachable gates, and outside every gate row"
        ),
    ),
    Criterion(
        slug="tier-b-pleural-effusion",
        text="pleural-effusion native service",
        origin="15.1.3",
        tier="B",
        verdict="GATE",
        covered_by=("idempotency-three-surface",),
        gap="see criterion `native-service-pleural-effusion`: a declared placeholder passes",
    ),
    Criterion(
        slug="tier-b-emphysema-laa",
        text="`emphysema_laa`",
        origin="15.1.3",
        tier="B",
        verdict="GATE",
        covered_by=("idempotency-three-surface",),
        gap="see criterion `emphysema-laa-deterministic`: determinism is not measured",
    ),
    Criterion(
        slug="tier-b-provenance-panel",
        text="provenance panel",
        origin="15.1.3",
        tier="B",
        verdict="SUITE",
        covered_by=(
            "tests/e2e/test_demo.py::test_the_panels_required_field_list_matches_what_the_"
            "api_returns",
            "tests/e2e/test_demo.py::test_rejected_renders_visually_distinct_from_failed",
        ),
        gap=(
            "structural, on the SERVED JavaScript and CSS, and outside the gate. Nothing "
            "renders the panel: MOS-SAFE-088's list of what it must show 'without a second "
            "request' is checked as a field list parsed out of provenance.js, not as "
            "anything a reviewer would see. The panel MODULE is in the extension's "
            "resolvable module graph (tests/e2e/test_viewer_extension.py), which is a "
            "different and much weaker claim than the panel rendering that list."
        ),
    ),
    Criterion(
        slug="tier-b-ohif-toolbar-button",
        text="OHIF toolbar button",
        origin="15.1.3",
        tier="B",
        verdict="SUITE",
        covered_by=(
            "tests/e2e/test_viewer_extension.py::test_the_medicalos_extension_is_loaded_"
            "in_the_served_viewer",
            "tests/e2e/test_viewer_extension.py::test_every_module_the_viewer_imports_for_"
            "the_extension_is_actually_served",
            "tests/unit/test_viewer_scan.py (both branches of the bundle scan, against "
            "synthetic bytes)",
        ),
        gap=(
            "OUTSIDE THE GATE, and only the load question. `-m gate_0_1_0` does not run "
            "these: the tag is not gated on the button, which is how MOS-SAFE-089a reached "
            "a green gate unmet. What no automated check covers at all is the rest of "
            "Chapter 9 acceptance check 24 -- one activation issuing exactly one POST, the "
            "job_id and Job.status rendering inline and updating from the SSE stream, a "
            "principal without job.create being refused, and a network capture showing "
            "zero viewer-to-PACS traffic outside the Gateway. Those need a browser. The "
            "probe also cannot prove the import SUCCEEDED: a throw inside preRegistration "
            "leaves every HTTP-observable fact true and the toolbar empty."
        ),
    ),
    Criterion(
        slug="tier-ui010-configured-capability-list",
        text="submission against a fixed configured capability list (`MOS-UI-366`)",
        origin="15.1.3",
        release="0.1.0",
        tier="B",
        verdict="SUITE",
        covered_by=("capability-reachable (0.3.0 row, property 5)",),
        gap=(
            "see criterion `ui010-configured-capability-list`: the check that covers it "
            "belongs to the 0.3.0 row, so the 0.1.0 and 0.2.0 tags were not gated on it"
        ),
    ),
    Criterion(
        slug="tier-ui010-refusal-catalogue",
        text="the refusal catalogue and its CI completeness check (`MOS-UI-335`)",
        origin="15.1.3",
        release="0.1.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui010-refusal-catalogue`: no catalogue and no CI check exist",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.1.0.md",
        reappears_in="0.2.0",
    ),
    Criterion(
        slug="tier-ui010-rebuilt-ohif-bundle",
        text="the two surfaces inside a rebuilt OHIF bundle (`MOS-UI-369`)",
        origin="15.1.3",
        release="0.1.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "see criterion `ui010-rebuilt-ohif-bundle`: the rebuild was not performed and "
            "the standalone host stands in, which MOS-UI-013a permits as a cut and not as "
            "a pass"
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.1.0.md",
        reappears_in="0.2.0",
    ),
    Criterion(
        slug="tier-c-docker-compose-ergonomics",
        text="docker-compose ergonomics",
        origin="15.1.3",
        tier="C",
        verdict="NONE",
        covered_by=(),
        gap=(
            "nothing measures ergonomics and nothing should try to. Tier C: MOS-REL-005 "
            "permits it to be cut and dropped permanently, so an uncovered Tier C item is "
            "the one shape of gap in this table that costs nothing."
        ),
    ),
)

#: The chapter-19 ingestion in 15.1.2's and 15.1.3's 0.2.0, 0.3.0 and 0.4.0 rows. The
#: 0.1.0 half is interleaved into `_CONTENTS` and `_TIERS` above, because that release is
#: registered in full and the tests hold those two tuples to the whole table in order.
#: Here only the `MOS-UI-` entries are registered; the rest of each row is counted in
#: `UNREGISTERED_CONTENTS` / `UNREGISTERED_TIERS` and named nowhere, which is honest debt
#: rather than a claim.
#:
#: EVERY CUT BELOW IS A CUT THAT ALREADY HAPPENED. 0.2.0 and 0.3.0 were declared complete
#: without these elements. What this tuple does is give each one the tier `MOS-REL-009`
#: needs to select a response, the Release Decision Record entry `MOS-REL-003` requires,
#: and -- for Tier B -- the `MOS-REL-005` release it MUST reappear in. The verdicts are
#: `CUT` and not `NONE` for a reason that matters: `NONE` says nobody checked, `CUT` says
#: the item is not in this release and the criterion is inapplicable rather than failed
#: (`MOS-UI-013a`). Recording the second as the first would lose the reappearance
#: obligation, which is the only machinery that gets any of them built.
_INGESTED: tuple[Criterion, ...] = (
    # ============================== 0.2.0 contents ====================================
    Criterion(
        slug="ui020-ruo-badge-and-research-gating",
        text=(
            "and the operator-surface elements Chapter 19 places in this release "
            "(`MOS-UI-365`): the RUO badge with the `include_research` gating of every "
            "list, feed, worklist and count (`MOS-UI-346`)"
        ),
        origin="15.1.2",
        release="0.2.0",
        tier="A",
        verdict="NONE",
        covered_by=(),
        gap=(
            "A TIER A ITEM HALF BUILT, AND MOS-REL-005 ADMITS NO CUT FOR IT. The BADGE "
            "exists: render.js emits `medos-ruo` with the clinical_use_mode whenever the "
            "view carries one. The GATING does not, on either side -- MEASURED: the "
            "string `include_research` occurs nowhere in medos/medos/, medos/web/ or "
            "tests/, so the "
            "query parameter MOS-UI-346 requires alongside `result.read.research` is not "
            "implemented, not served and not asserted. Today the omission is masked "
            "rather than harmless: no surface renders a result LIST at all, so there is "
            "nothing for a research result to leak into. The first list shipped is the "
            "first list that defaults research results into a clinician's view, which is "
            "the exact failure MOS-SAFE-041 exists for. The 0.2.0 row's `ruo-marking` "
            "check covers the DICOM writer refusing to emit an unmarked object and says "
            "nothing about either half of this item."
        ),
    ),
    Criterion(
        slug="ui020-resultreview-surface",
        text="the `ResultReview` surface with its `REJECTED` review rendering (`MOS-UI-350`)",
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no review surface exists. medos/web/ holds the OHIF extension and nothing else, "
            "and that extension renders a job and its results with no review control. "
            "The ENTITY shipped in 0.2.0 and medos/medos/api/routes_reviews.py serves it; what "
            "is cut is the screen. MOS-SAFE-064's obligation that a REJECTED review hide "
            "nothing is therefore unexercised by any surface rather than violated by one."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="ui020-curation-queue-surface",
        text=(
            "the curation queue surface over `HarvestCandidate` and `CurationBatch` with "
            "its exclusion vocabulary (`MOS-UI-357`)"
        ),
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no surface exists, and MOS-UI-368 records that it could not have been built "
            "conformantly: POST /api/v1/harvest-batches and the curation-decision path "
            "are RESERVED and MUST NOT be served until each has a table 10.2-B row, a "
            "schema in medos/schemas/ and a registered permission key, and MOS-API-112 records "
            "that no harvest or curation permission exists in chapter 8's catalogue. So "
            "this element had no API to be a client of. That is a reason for the cut, not "
            "an excuse for leaving it unrecorded: MOS-API-112 scopes the rows as 0.2.0 "
            "work and they were not done either."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="ui020-training-policy-surface",
        text=(
            "the training-policy surface over `training_use_allowed` and "
            "`TrainingDataPolicy` (`MOS-UI-365`, with `MOS-UI-368` reserving the API "
            "paths it would drive)"
        ),
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no surface exists, and PUT /api/v1/tenants/{tenant_id}/training-policy is "
            "reserved under MOS-API-112 for the same reason as the curation paths. The "
            "per-Tenant policy itself is 0.2.0 platform work and shipped; what is cut is "
            "the screen that would edit it."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="ui020-training-refusal-rendering",
        text=(
            "the seal, split-freeze, leakage and stratification refusal rendering "
            "(`MOS-UI-332`)"
        ),
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no engineering surface exists, so rows L1-C7 of MOS-UI-332's catalogue have "
            "nowhere to render. The GATES themselves block: leakage-check is green in the "
            "0.2.0 row and leakage-blocks-training in the 0.3.0 row, both against the "
            "platform rather than a screen. Tier B and not A for exactly that reason -- "
            "the refusal rendering is how an operator learns WHY a build stopped, and its "
            "absence costs one person an investigation rather than letting a wrong "
            "artifact exist."
            " WENT STALE IN PART AND IS TRUE AGAIN -- medos/web/training-console existed "
            "between its construction and its deletion on 2026-10-02, when the engineering "
            "surface was withdrawn with section 19.3; see "
            "docs/spec/99-known-inconsistencies.md entries 83 and 150 for the history and "
            "the resolution. The sentence above the marker is the justification, and it "
            "describes the repository again."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="ui020-annotation-campaign-surface",
        text="the annotation campaign surface over the adopted annotation tool (`MOS-UI-358`)",
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no surface exists and no authentication shim exists with it. MOS-UI-358's "
            "obligation is the one that decays quietly: every annotation session must "
            "authenticate as a MedicalOS User so that annotation_readers.reader_id "
            "derives from that identity, and a deployment that adopts the annotation tool "
            "WITHOUT the shim gets a shared workstation login and an AnnotationSet that "
            "MOS-EVID-038 cannot name the readers of, after the fact rather than at the "
            "time. Cutting the surface MUST NOT be read as licensing that deployment."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="ui020-refusal-catalogue",
        text=(
            "the refusal catalogue at this release's full set with its CI completeness "
            "check (`MOS-UI-335`, `MOS-UI-336`)"
        ),
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "the 0.1.0 seed set was cut and returned here as MOS-REL-005 requires, and "
            "was cut again: still no catalogue file, still no record per gate id, still "
            "no CI enumeration. §15.1.2's 0.2.0 gate row now names `refusal-completeness` "
            "for this item, and that check is declared INAPPLICABLE rather than failed in "
            "tests/unit/test_gate_contract.py's `INAPPLICABLE`, keyed to this criterion "
            "being CUT. Build the catalogue and that declaration must be deleted and the "
            "module written; the two cannot both be true."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="ui020-contribution-statement",
        text="the first issue of the UI contribution statement (`MOS-UI-359`)",
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "MEASURED: the phrase occurs in the specification index and in no artifact "
            "this repository publishes. MOS-CONF-242 records the obligation AND records "
            "it as a gap -- under IEC 62366-1 clause 6 a user interface the manufacturer "
            "did not develop is a User Interface of Unknown Provenance -- and MOS-UI-359 "
            "is explicit that enumerating the platform's contribution does not close the "
            "usability-engineering gap of MOS-CONF-139, it only makes the publisher's "
            "half closable. Tier B and not C because dropping it permanently would leave "
            "MOS-CONF-242 unsatisfiable by anyone."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="ui020-returning-set-from-010",
        text=(
            "and the Tier B operator-surface elements cut from 0.1.0 under "
            "`MOS-REL-009`, returning as `MOS-REL-005` requires and enumerated in that "
            "release's Release Decision Record (`MOS-UI-369`)"
        ),
        origin="15.1.2",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "the two elements cut from 0.1.0 -- the rebuilt OHIF bundle and the refusal "
            "catalogue -- returned here and were cut again, so the set is empty of "
            "delivered members. The rebuilt bundle is the one MOS-UI-365 marks **ships** "
            "at 0.2.0: the OHIF monorepo rebuild was still not performed, "
            "medos/deploy/compose/docker-compose.yml still pins ohif/app:v3.9.2 and "
            "medicalos-config.js still ships `extensions: []`."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    # ================================ 0.2.0 tiers =====================================
    Criterion(
        slug="tier-ui020-ruo-badge-and-research-gating",
        text="the RUO badge and its `include_research` gating (`MOS-UI-346`)",
        origin="15.1.3",
        release="0.2.0",
        tier="A",
        verdict="NONE",
        covered_by=(),
        gap=(
            "see criterion `ui020-ruo-badge-and-research-gating`: the badge renders, the "
            "gating is unimplemented on both sides, and Tier A admits no cut"
        ),
    ),
    Criterion(
        slug="tier-ui020-resultreview-surface",
        text="the `ResultReview` surface (`MOS-UI-350`)",
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "see criterion `ui020-resultreview-surface`: the entity shipped, the screen "
            "did not"
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="tier-ui020-curation-queue-surface",
        text="the curation queue surface (`MOS-UI-357`)",
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui020-curation-queue-surface`: its API paths are reserved",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="tier-ui020-training-policy-surface",
        text="the training-policy surface (`MOS-UI-368`)",
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui020-training-policy-surface`: its API path is reserved",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="tier-ui020-training-refusal-rendering",
        text=(
            "the seal, split-freeze, leakage and stratification refusal rendering "
            "(`MOS-UI-332`)"
        ),
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "see criterion `ui020-training-refusal-rendering`: the gates block, nothing "
            "renders"
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="tier-ui020-annotation-campaign-surface",
        text="the annotation campaign surface (`MOS-UI-358`)",
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui020-annotation-campaign-surface`: no surface and no auth shim",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="tier-ui020-refusal-catalogue",
        text="the refusal catalogue at the 0.2.0 set (`MOS-UI-335`)",
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "see criterion `ui020-refusal-catalogue`: this cut is what makes "
            "`refusal-completeness` inapplicable rather than failed"
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="tier-ui020-contribution-statement",
        text="the first issue of the UI contribution statement (`MOS-UI-359`)",
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui020-contribution-statement`: no statement is published",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    Criterion(
        slug="tier-ui020-returning-set-from-010",
        text="the operator-surface elements returning from the 0.1.0 cut (`MOS-UI-369`)",
        origin="15.1.3",
        release="0.2.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui020-returning-set-from-010`: both members were cut again",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.2.0.md",
        reappears_in="0.3.0",
    ),
    # ============================== 0.3.0 contents ====================================
    Criterion(
        slug="ui030-model-picker",
        text=(
            "and the operator-surface elements Chapter 19 places in this release "
            "(`MOS-UI-365`): the clinician model picker driven by capability resolution "
            "(`MOS-UI-366`)"
        ),
        origin="15.1.2",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no picker exists. The substrate it needed DID arrive: `resolution-purity` is "
            "green in this row and the 0.3.0 contents carry capability resolution as a "
            "pure function, which is the condition MOS-UI-366 says the picker was waiting "
            "on. So this is a cut of the surface and not of its dependency, and the "
            "0.1.0 configured-list submit is what a reader still uses. MOS-UI-366's "
            "constraint survives the cut: whatever ships MUST NOT render a rank, a score "
            "or a recommendation beside an entry unless resolution actually produced it."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="ui030-training-submit-surface",
        text="the training submit surface with run monitoring (`MOS-UI-367`)",
        origin="15.1.2",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "THIS IS THE ELEMENT THE QUESTION 'WHEN CAN TRAINING BE DRIVEN FROM THE "
            "FRONT END' IS ABOUT, AND IT WAS CUT WITHOUT BEING RECORDED. MOS-UI-367 "
            "places it here and forbids any part of it shipping earlier, because a "
            "no-code surface over a partial substrate is a surface whose refusals are not "
            "yet enforced and there is no operator on it who would notice. The substrate "
            "it named arrived: TrainingRun with the Orchestrator port and one driver, "
            "ConversionRun, the three-act promotion and ConfigurationSearch are all in "
            "this release's contents, and leakage-blocks-training and no-auto-promote are "
            "green in this row. The surface is what is missing. MOS-REL-005 puts it in "
            "0.4.0 as Tier B, and 0.4.0 is the last row 15.1.2 defines."
            " WENT STALE IN PART AND IS RESOLVED BY WITHDRAWAL -- medos/web/training-console "
            "existed between its construction and its deletion on 2026-10-02, and the 0.4.0 "
            "row now records that the return MOS-REL-005 required is discharged by the "
            "withdrawal of the engineering surface (MOS-UI-100 CUT, section 19.3 CUT, "
            "register entries 83 and 150). The reappears_in field below names the release "
            "where the return question lands, 0.4.0, and the decision recorded there is the "
            "withdrawal, not a build."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="ui030-configuration-search-surface",
        text="the configuration-search surface (`MOS-UI-303`)",
        origin="15.1.2",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no surface exists. Split from the diagnostics block deliberately: "
            "MOS-UI-371 tiers the DIAGNOSTICS BLOCK Tier C and says nothing about the "
            "surface that would carry it, and MOS-REL-005 requires exactly one tier per "
            "contents item, so one row carrying two tiers had to become two items."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="ui030-search-diagnostics-block",
        text="the search diagnostics block (`MOS-UI-304`)",
        origin="15.1.2",
        release="0.3.0",
        tier="C",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no surface exists to carry it. Tier C by MOS-UI-371, so MOS-REL-005 permits "
            "it to be dropped permanently -- and MOS-UI-365 does not drop it, so it is "
            "cut and still placed. Recorded rather than deleted for that reason."
        ),
        cut="MOS-REL-009 Tier C cut, recorded in docs/releases/0.3.0.md",
    ),
    Criterion(
        slug="ui030-promotion-screen",
        text=(
            "the promotion screen carrying the dossier, the three acts and "
            "`test_exposure_count` (`MOS-UI-318`, `MOS-UI-320`)"
        ),
        origin="15.1.2",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no screen exists. The three acts and the refusal of an automated promotion "
            "are enforced platform-side and gated: `no-auto-promote` asserts the import "
            "closure, the named-human requirement and the database trigger, all in this "
            "row and all green. What is cut is the screen that would show an approver the "
            "fifteen items of MOS-TRAIN-176 before they act, and with it the "
            "`duty-separation` check §15.1.2's 0.3.0 row now names, which has no subject "
            "without it and is declared inapplicable rather than failed."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="ui030-conversion-surface",
        text="the conversion surface rendering E1, E2 and E3 (`MOS-UI-365`)",
        origin="15.1.2",
        release="0.3.0",
        tier="C",
        verdict="CUT",
        covered_by=(),
        gap=(
            "no surface exists. ConversionRun itself is in this release's contents and "
            "`chain-equivalence` and `served-plan-equivalence` are green in this row, so "
            "the platform half is delivered and gated. Tier C by MOS-UI-371; cut and "
            "still placed, because MOS-UI-365 does not drop it."
        ),
        cut="MOS-REL-009 Tier C cut, recorded in docs/releases/0.3.0.md",
    ),
    Criterion(
        slug="ui030-honest-metric-rule",
        text="the honest-metric display rule in force end to end (`MOS-UI-300`)",
        origin="15.1.2",
        release="0.3.0",
        tier="A",
        verdict="NONE",
        covered_by=(),
        gap=(
            "A TIER A ITEM THAT IS IN FORCE, VACUOUSLY TRUE, AND EXECUTED BY NOTHING. No "
            "surface in this repository renders a figure drawn from capability_claims or "
            "evaluation_runs, so 'no selection-partition metric appears as headline "
            "performance' is satisfied the way an empty set satisfies a universal. "
            "MOS-REL-012 is the requirement that makes that insufficient: an unexecuted "
            "acceptance criterion means the requirement is not satisfied, whatever the "
            "code does. §15.1.2's 0.3.0 row now names `headline-partition` for this item "
            "and there is no module for it; the check is declared inapplicable in "
            "tests/unit/test_gate_contract.py because the SURFACES it would measure were "
            "cut, and the rule itself is NOT cut and cannot be -- MOS-REL-005 forbids it. "
            "The first surface that renders a figure is the moment this stops being "
            "vacuous, and the declaration must be deleted before that surface ships."
            " WENT STALE IN PART AND IS TRUE AGAIN -- medos/web/training-console existed "
            "between its construction and its deletion on 2026-10-02, when the engineering "
            "surface was withdrawn with section 19.3; see "
            "docs/spec/99-known-inconsistencies.md entries 83 and 150 for the history and "
            "the resolution. No surface in this repository renders a figure again, which is "
            "the sentence above the marker, and it describes the repository again."
        ),
    ),
    Criterion(
        slug="ui030-refusal-catalogue",
        text=(
            "the refusal catalogue extended to this release's full set including the "
            "run-time re-execution refusals (`MOS-UI-332`, `MOS-UI-335`)"
        ),
        origin="15.1.2",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "cut for the third consecutive release. MOS-UI-370 extends "
            "`refusal-completeness`'s enumeration here rather than adding a second check, "
            "so the inapplicability declared for the 0.2.0 row covers this scope too, and "
            "one deletion re-arms both."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="ui030-returning-set-from-020",
        text=(
            "and the Tier B operator-surface elements cut from 0.2.0 under "
            "`MOS-REL-009`, returning as `MOS-REL-005` requires and enumerated in that "
            "release's Release Decision Record (`MOS-UI-365`)"
        ),
        origin="15.1.2",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "eight Tier B elements returned from 0.2.0 and all eight were cut again: the "
            "rebuilt OHIF bundle, the ResultReview surface, the curation queue surface, "
            "the training-policy surface, the training refusal rendering, the annotation "
            "campaign surface, the refusal catalogue and the UI contribution statement. "
            "The refusal catalogue is registered separately at this release because "
            "MOS-UI-365 extends its scope here; the other seven are this set."
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    # ================================ 0.3.0 tiers =====================================
    Criterion(
        slug="tier-ui030-honest-metric-rule",
        text="the honest-metric display rule in force end to end (`MOS-UI-300`)",
        origin="15.1.3",
        release="0.3.0",
        tier="A",
        verdict="NONE",
        covered_by=(),
        gap=(
            "see criterion `ui030-honest-metric-rule`: vacuously true, executed by "
            "nothing, and uncuttable"
        ),
    ),
    Criterion(
        slug="tier-ui030-model-picker",
        text="the clinician model picker (`MOS-UI-366`)",
        origin="15.1.3",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui030-model-picker`: resolution shipped, the picker did not",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="tier-ui030-training-submit-surface",
        text="the training submit surface with run monitoring (`MOS-UI-367`)",
        origin="15.1.3",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "see criterion `ui030-training-submit-surface`: the whole substrate "
            "MOS-UI-367 named is present and the surface is not"
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="tier-ui030-configuration-search-surface",
        text="the configuration-search surface (`MOS-UI-303`)",
        origin="15.1.3",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui030-configuration-search-surface`",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="tier-ui030-promotion-screen",
        text="the promotion screen (`MOS-UI-318`)",
        origin="15.1.3",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap=(
            "see criterion `ui030-promotion-screen`: this cut is what makes "
            "`duty-separation` inapplicable rather than failed"
        ),
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="tier-ui030-refusal-catalogue",
        text="the refusal catalogue extended to the 0.3.0 set (`MOS-UI-335`)",
        origin="15.1.3",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui030-refusal-catalogue`: a third consecutive cut",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="tier-ui030-returning-set-from-020",
        text="the operator-surface elements returning from the 0.2.0 cut (`MOS-UI-365`)",
        origin="15.1.3",
        release="0.3.0",
        tier="B",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui030-returning-set-from-020`: seven members, none delivered",
        cut="MOS-REL-009 Tier B cut, recorded in docs/releases/0.3.0.md",
        reappears_in="0.4.0",
    ),
    Criterion(
        slug="tier-ui030-search-diagnostics-block",
        text="the search diagnostics block (`MOS-UI-304`)",
        origin="15.1.3",
        release="0.3.0",
        tier="C",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui030-search-diagnostics-block`: Tier C by MOS-UI-371",
        cut="MOS-REL-009 Tier C cut, recorded in docs/releases/0.3.0.md",
    ),
    Criterion(
        slug="tier-ui030-conversion-surface",
        text="the conversion surface (`MOS-UI-365`)",
        origin="15.1.3",
        release="0.3.0",
        tier="C",
        verdict="CUT",
        covered_by=(),
        gap="see criterion `ui030-conversion-surface`: Tier C by MOS-UI-371",
        cut="MOS-REL-009 Tier C cut, recorded in docs/releases/0.3.0.md",
    ),
    # ============================== 0.4.0 contents ====================================
    Criterion(
        slug="ui040-unattended-worklist-surface",
        text=(
            "and the operator-surface elements Chapter 19 places in this release "
            "(`MOS-UI-365`): the unattended-arrival worklist surface"
        ),
        origin="15.1.2",
        release="0.4.0",
        tier="C",
        verdict="NONE",
        covered_by=(),
        gap=(
            "0.4.0 is not built. tests/unit/test_gate_contract.py declares it NOT_YET and "
            "asserts its checks have no modules, so nothing verifies any item in this "
            "row and nothing should yet. Recorded so that the row is not empty of "
            "chapter 19 when the block opens. NOT a cut: a release that has not happened "
            "has cut nothing, and writing CUT here would manufacture a MOS-REL-005 "
            "reappearance obligation against a release 15.1.2 does not define."
        ),
    ),
    Criterion(
        slug="ui040-returning-set-from-030",
        text=(
            "and the Tier B operator-surface elements cut from 0.3.0 under "
            "`MOS-REL-009`, whose return `MOS-REL-005` required is discharged at "
            "specification 0.4.0 by the withdrawal of the engineering surface "
            "(`MOS-UI-367`, amended `MOS-UI-100`)"
        ),
        origin="15.1.2",
        release="0.4.0",
        tier="B",
        verdict="NONE",
        covered_by=(),
        gap=(
            "THE RETURN CHAIN ENDS IN A WITHDRAWAL. Seven Tier B elements cut from 0.3.0 "
            "carried a MOS-REL-005 obligation to reappear at 0.4.0, together with the "
            "training submit surface, the model picker, the configuration-search surface "
            "and the promotion screen. On 2026-10-02 the engineering surface itself was "
            "withdrawn before the release (MOS-UI-100 CUT, section 19.3 CUT, register "
            "entries 83 and 150), and the 0.4.0 row now discharges the return by that "
            "withdrawal rather than promising a build. Nothing verifies any of it because "
            "there is nothing to verify: what is recorded is the discharge of the "
            "obligation, not a claim about code. A future return of the surface is a new "
            "minor version of chapter 19 under MOS-CORE-036, and a new row here."
        ),
    ),
    # ================================ 0.4.0 tiers =====================================
    Criterion(
        slug="tier-ui040-returning-set-from-030",
        text=(
            "the operator-surface elements cut at 0.3.0 whose return is discharged by the "
            "withdrawal of the engineering surface at specification 0.4.0 (`MOS-UI-367`)"
        ),
        origin="15.1.3",
        release="0.4.0",
        tier="B",
        verdict="NONE",
        covered_by=(),
        gap=(
            "see criterion `ui040-returning-set-from-030`: the return is discharged by "
            "withdrawal, not promised as a build"
        ),
    ),
    Criterion(
        slug="tier-ui040-unattended-worklist-surface",
        text="the unattended-arrival worklist surface (`MOS-UI-365`)",
        origin="15.1.3",
        release="0.4.0",
        tier="C",
        verdict="NONE",
        covered_by=(),
        gap="see criterion `ui040-unattended-worklist-surface`: the release is not built",
    ),
)

CRITERIA: tuple[Criterion, ...] = _CONTENTS + _TIERS + _INGESTED


#: 15.1.2 contents items that 15.1.3's 0.1.0 row assigns no tier. REPORTED, not fixed:
#: MOS-REL-005 requires exactly one tier per contents item and 15.3's conformance item 1
#: says a parser "fails if any contents item has no tier", so this is a spec defect.
UNTIERED_CONTENTS: tuple[str, ...] = (
    "gateway-and-deid",
    "triage-and-series-selector",
    "job-and-queue-driver",
    "vendored-lung-segmentation",
    "api-key-auth",
    "single-triton-docker-compose",
)

#: Tier A items the gate does not cover. FROZEN: `tests/unit/test_release_criteria.py`
#: asserts this set exactly, so the existing debt stays visible and a new Tier A gap is a
#: test failure rather than a discovery. MOS-REL-005 makes every one of these uncuttable.
#:
#: Ten of the thirteen arrived with the chapter-19 ingestion, and they are NOT ten new
#: gaps: every one of them was already uncovered, in a chapter that placed the element in
#: a release row this table had never read. Making them visible is the change. Three of
#: them -- the two `safe012` entries' subject, the RUO gating and the honest-metric rule
#: -- are additionally NOT BUILT, which the module docstring names as release defects and
#: which no verdict in this file can discharge.
TIER_A_WITHOUT_GATE_COVERAGE: tuple[str, ...] = (
    "audit",
    "tier-a-append-only-audit",
    "tier-a-gateway-sole-credential-holder",
    "ui010-rejected-failed-four-channels",
    "ui010-refusal-rendering",
    "ui010-safe012-adjacency-set",
    "ui020-ruo-badge-and-research-gating",
    "ui030-honest-metric-rule",
    "tier-ui010-rejected-failed-four-channels",
    "tier-ui010-refusal-rendering",
    "tier-ui010-safe012-adjacency-set",
    "tier-ui020-ruo-badge-and-research-gating",
    "tier-ui030-honest-metric-rule",
)

#: release -> how many of 15.1.2's contents items for that row this register does NOT
#: classify. FROZEN for the same reason `UNTIERED_CONTENTS` is: 0.1.0 is registered in
#: full, and for the other three rows only the chapter-19 elements are. A count and not a
#: list, because naming them would read as a classification and there is none; what the
#: count buys is that the unclassified half cannot GROW without a test failure, which is
#: the property the 0.2.0 Release Decision Record asked for in terms when it recorded
#: "there is no coverage register for this release".
UNREGISTERED_CONTENTS: dict[str, int] = {"0.1.0": 0, "0.2.0": 6, "0.3.0": 6, "0.4.0": 4}

#: The same, for 15.1.3's tier rows.
UNREGISTERED_TIERS: dict[str, int] = {"0.1.0": 0, "0.2.0": 9, "0.3.0": 5, "0.4.0": 5}


def by_slug(slug: str) -> Criterion:
    for criterion in CRITERIA:
        if criterion.slug == slug:
            return criterion
    raise KeyError(f"no release criterion {slug!r}")


def for_release(release: str, origin: str | None = None) -> tuple[Criterion, ...]:
    """Every criterion of one release row, in table order."""
    return tuple(
        c
        for c in CRITERIA
        if c.release == release and (origin is None or c.origin == origin)
    )


def cut_criteria(release: str | None = None) -> tuple[Criterion, ...]:
    """Every item declared cut under `MOS-REL-009`, optionally for one release.

    `tests/unit/test_gate_contract.py` reads this: a gate check may be declared
    inapplicable rather than failed only where the element it measures is recorded here,
    so the two files cannot disagree about what was cut.
    """
    return tuple(
        c
        for c in CRITERIA
        if c.verdict == "CUT" and (release is None or c.release == release)
    )


def not_machine_verified() -> tuple[Criterion, ...]:
    """Every criterion no release gate run stands behind."""
    return tuple(c for c in CRITERIA if c.verdict != "GATE")


# ======================================================================================
# The report. Deliberately loud, and printed on every gate run.
# ======================================================================================
def _wrap(text: str, width: int, indent: str) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width=width, initial_indent=indent, subsequent_indent=indent)


def report_lines(width: int = 96) -> list[str]:
    """The coverage report, as lines for a pytest terminal summary.

    Every criterion that is not `GATE` is named, with its tier, its verdict and what is
    missing. A green gate that prints this cannot be quoted as "0.1.0 is verified"; it can
    only be quoted as "these thirteen items are verified and these twelve are not".
    """
    counts: dict[Verdict, int] = dict.fromkeys(VERDICT_ORDER, 0)
    for criterion in CRITERIA:
        counts[criterion.verdict] += 1

    lines: list[str] = [
        f"{len(CRITERIA)} criteria parsed from docs/spec/15-delivery.md 15.1.2 (contents) "
        f"and 15.1.3 (tiers):",
        "  "
        + ", ".join(f"{r} {len(for_release(r))}" for r in RELEASES)
        + f". Only {RELEASE} is registered in full; for the other rows this is the",
        "chapter-19 ingestion of MOS-UI-365 and MOS-UI-371 only.",
        "",
    ]
    for verdict in reversed(VERDICT_ORDER):
        lines.append(f"  {verdict:<7}{counts[verdict]:>3}   {VERDICT_MEANING[verdict]}")

    uncovered = not_machine_verified()
    lines += [
        "",
        f"NOT MACHINE-VERIFIED BY THIS GATE: {len(uncovered)} of {len(CRITERIA)} criteria. "
        "A green gate does NOT",
        "cover the following, and MOS-REL-012 is explicit that an unexecuted acceptance "
        "criterion",
        'means the requirement is not satisfied, "whatever the code does".',
        "",
    ]
    for criterion in sorted(
        uncovered, key=lambda c: (VERDICT_ORDER.index(c.verdict), c.tier or "Z", c.slug)
    ):
        lines.append(f"  {criterion.verdict:<7}{criterion.label}")
        if criterion.gap:
            lines += _wrap(criterion.gap, width, "          ")
        if criterion.cut:
            lines += _wrap(f"CUT: {criterion.cut}", width, "          ")

    # The sneakiest category, and the reason this section exists: a criterion whose
    # verdict is GATE but whose `gap` is non-empty LOOKS covered on every summary line
    # anyone will ever read. `one native service (pleural effusion, own data)` is gated by
    # an assertion a declared placeholder satisfies. Listing these separately is what stops
    # "17 GATE" from being quoted as "17 items verified".
    partial = [c for c in CRITERIA if c.verdict == "GATE" and c.gap]
    lines += [
        "",
        f"GATED, BUT ONLY IN PART: {len(partial)}. A check covers the item; it does not "
        "cover all of it.",
        "",
    ]
    for criterion in partial:
        lines.append(f"  GATE   {criterion.label}")
        lines += _wrap(criterion.gap, width, "          ")

    tier_a_gaps = [c for c in CRITERIA if c.tier == "A" and c.verdict != "GATE"]
    lines += [
        "",
        f"TIER A WITHOUT A GATE CHECK: {len(tier_a_gaps)}. MOS-REL-005 forbids cutting a "
        "Tier A item under",
        "any circumstance, and the gate cannot tell whether these are present:",
    ]
    for criterion in tier_a_gaps:
        lines.append(f"  {criterion.verdict:<7}{criterion.text}")
    lines += [
        "",
        f"UNTIERED CONTENTS ITEMS: {len(UNTIERED_CONTENTS)}. MOS-REL-005 requires every "
        "contents item to carry",
        "exactly one tier and 15.3 item 1 fails a parser that finds one without. "
        "15.1.3's 0.1.0 row",
        "assigns none to:",
    ]
    lines += _wrap(", ".join(UNTIERED_CONTENTS) + ".", width, "  ")
    lines.append("This is a SPECIFICATION defect and is reported, not patched here.")

    cuts = cut_criteria()
    lines += [
        "",
        f"CUT UNDER MOS-REL-009: {len(cuts)}. Inapplicable rather than failed "
        "(MOS-UI-013a), recorded in the",
        "Release Decision Record, and for Tier B carrying the release MOS-REL-005 makes "
        "it reappear in:",
    ]
    for criterion in cuts:
        where = f" -> returns in {criterion.reappears_in}" if criterion.reappears_in else ""
        lines.append(f"  {criterion.release} [{criterion.tier}] {criterion.slug}{where}")

    lines += [
        "",
        "NOT CLASSIFIED AT ALL, by row: "
        + ", ".join(
            f"{r} {UNREGISTERED_CONTENTS[r]} contents / {UNREGISTERED_TIERS[r]} tier"
            for r in RELEASES
        )
        + ".",
        "Those are the pre-chapter-19 items of the 0.2.0, 0.3.0 and 0.4.0 rows. The "
        "counts are frozen, so",
        "the unclassified half cannot grow unnoticed; it is still unclassified, and a "
        "green gate on those",
        "rows stands behind no statement about them.",
    ]
    return lines
