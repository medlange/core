# Release Decision Records

`MOS-REL-003` requires one record per release at `docs/releases/<version>.md`, carrying the
tag, the image digests, the gate results per named check, any Tier B or Tier C item cut
under `MOS-REL-009`, the requirement IDs newly satisfied, and **the named human who approved
the tag** — committed before the tag is pushed.

## Status: three records for three releases, approver named, no tags

<!-- This heading read "no tags, no approver" until 2026-09-26. It was written before the
     approver line was filled and was never revisited, while the body of this same file
     says below that it "now names **John Doe**, supplied by the repository owner on
     2026-09-17". A heading contradicting its own section is the shape a present-tense
     status page acquires when only its paragraphs are maintained.

     "approver named" rather than "one approver named" on purpose:
     tests/unit/test_release_records.py reads EVERY number word in this heading as a
     count of records, on the stated reasoning that the heading is where the repository
     counts its releases. That is the right assumption and the wording gives way to it
     rather than the check acquiring an exception. -->

`git tag` is empty. 0.1.0, 0.2.0 and 0.3.0 are complete in the tree and green on their
gates, and none of them is tagged, because `MOS-REL-004` gates the tag on the gate row and
`MOS-REL-003` requires one record per release, committed first. These three are those
records.

### There is no 0.4.0 record, and this file used to say one was owed

**That sentence was an inference this file made, and it was wrong.** It read: the
specification says "Document version: **0.4.0**", `MOS-REL-003` requires one record per
release, therefore a fourth record is OWED. Every step is checkable and the middle one does
not hold — `MOS-REL-003`'s subject is a *release*, and release 0.4.0 has not happened.

What the documents say, measured rather than inferred:

- **`MOS-REL-003`'s six fields presuppose a tag.** The tag, the image digests at that tag,
  the gate results at that tag, the items cut at that tag, the ids newly satisfied in that
  range, and **the human who approved the tag**. `git tag` is empty.
- **`MOS-REL-004` does not merely leave 0.4.0 untagged; it forbids the tag.** The row's four
  checks are `unattended-arrival`, `second-viewer`, `mcp-tenancy` and
  `narrative-postcondition`. None has a module, and none may have one:
  `tests/unit/test_gate_contract.py` declares 0.4.0 `NOT_YET` and asserts that no module for
  its checks exists in `tests/gate/`. The gate row is red by construction.
- **The "Items cut under `MOS-REL-009`" section has no legitimate content.** `MOS-REL-009`
  is reachable "on a red gate **at the tag date**". There is no tag date, so no response is
  selectable, and writing "None" would imply the question was reached. The repository
  already says this in its own words, in `tests/_support/release_criteria.py`: *"a release
  that has not happened has cut nothing, and writing CUT here would manufacture a
  `MOS-REL-005` reappearance obligation against a release 15.1.2 does not define."*
- **The gate-results table has no truthful cell either.** `MOS-REL-003` asks for pass/fail.
  0.3.0 established `PASS` and `INAPPLICABLE`; `INAPPLICABLE` is forbidden for a `NOT_YET`
  release by the same module. The honest value is NOT RUN, which is not a gate result.
- **The decisions taken at document version 0.4.0 are already recorded, by the requirement
  that governs them.** `MOS-CORE-036` asks for "a minor version increment of this document
  **and** a recorded decision naming the requirement ID being withdrawn". Both hold: the
  document reads 0.4.0, and `docs/spec/01-overview.md` names `MOS-CORE-038` and points at
  `docs/adr/BUILD_VS_ADOPT.md`, which `docs/spec/15-delivery.md`'s amended Viewer row cites
  as well. `MOS-CORE-036` does not ask for a Release Decision Record and no other
  requirement does.

**And the file's location would be the claim.** A reader opening `docs/releases/` and
finding four files, three of them ending in an Approval table with a name and a date, reads
four releases — whatever any individual cell inside the fourth says. Under IEC 62304 §5.8
that reader is an assessor reconstructing the design history.

**The engineering surface will not be part of 0.4.0.** On 2026-10-02 the no-code training
console (`medos/web/training-console/`) was withdrawn before the release: `MOS-UI-100` is
CUT, §19.3 of chapter 19 carries a section-wide CUT mark, and chapter 15's 0.4.0 row records
that the return `MOS-REL-005` required of the 0.3.0 Tier B cuts is discharged by withdrawal
rather than by a build. The console was never in a shipped release — every row that named it
already recorded it as cut — so no record in this directory is rewritten; the decision lives
in the specification's 0.4.0 amendments and in register entry 150.

**What a future 0.4.0 record owes, beyond `MOS-REL-003`'s six fields.** Chapter 4 adds three
obligations that name this document specifically and that nothing else can discharge:
`MOS-IMG-157a` requires the record to state **which viewer and which version** the AT-11
check ran against ("'a pinned viewer' with no name is not a pin") and **which surface each
step ran against**; `MOS-IMG-158` requires the third-party-viewer verification's screenshots
to be **attached to the release record**. Today all three read "never executed", which
`docs/spec/19-operator-surfaces.md` states in the specification's own words. Those fields
are the argument for a 0.4.0 record when the checks run, and the argument against one now.

`tests/unit/test_release_records.py` holds this as an executable rule: a record for a
release the repository declares `NOT_YET` fails the suite, which is the mirror of the check
that already forbids a premature gate module.

`MOS-REL-003` gates it on a record naming a human. These three records exist so that the
only remaining step is the signature.

**Everything machine-derivable is filled in and dated.** The approver line is the one field
this document exists for. It now names **John Doe**, supplied by the repository owner on 2026-09-17
and entered on their instruction, with that provenance stated in each record.

An attribution is not an attestation. Nothing here verified the name, and no release is
tagged. The stronger forms — a signed tag, a commit signed by the approver's key, a
countersigned record — are not in place, and each record says so rather than letting the
filled cell imply more than it carries.

## What these records are not

They are not release notes. `MOS-REL-003`'s list is a decision record: what was true, what
was not verified, and who accepted the gap. Each record's **"What a green gate does not
cover"** section is therefore load-bearing and is written from
`tests/_support/release_criteria.py`, which classifies every item of §15.1.2's contents and
§15.1.3's tiers as `GATE` / `SUITE` / `MANUAL` / `NONE` / `CUT`.

**This paragraph used to end "that register exists for 0.1.0 only", and it no longer does.**
Measured from the register itself: 0.1.0 is classified in full — 37 criteria, 17 `GATE`, 13
`SUITE`, 4 `CUT`, 3 `NONE`, and nothing unregistered. The other three rows are classified in
part, and only where Chapter 19's operator-surface ingestion reached them: 18 criteria for
0.2.0, 18 for 0.3.0, 4 for 0.4.0. What is NOT classified is frozen as a count rather than a
list — `UNREGISTERED_CONTENTS` and `UNREGISTERED_TIERS` — so the unclassified remainder can
shrink and cannot grow without a test failure. The 0.2.0 record's complaint that "there is
no coverage register for this release" is therefore half answered and the half that is not
is a number, which is what it asked for. Each record's own open conditions still say what
was true when that record was written, and are not edited to match this paragraph.

## On the image digests

`MOS-REL-003` asks for image **digests** so that a tagged release can be verified later by
someone who was not present. The third-party images carry real registry manifest digests,
recorded in each record.

The MedicalOS images do not. `docker image inspect` reports a `RepoDigests` entry for each,
but it is byte-identical to the image `Id` — the images were built locally and never pushed,
so no registry manifest digest exists for them. The records print the local image ID and say
what it is. Copying it into the digest field would give the record a provenance it does not
have: nobody else can resolve it, and it does not survive a rebuild on another machine.
**Publishing the MedicalOS images to a registry is a precondition for any release anyone
outside this machine can verify**, and it is listed as an open condition on all three.

**This section used to count them: "five of the seven images … the two MedicalOS images."**
Measured against the compose file today, that is five third-party and **four** first-party —
`medicalos/medos`, `medicalos/medos-train`, `medicalos/sealed-reference` and
`medicalos/trainer` — out of nine distinct images across fifteen `image:` declarations. The
counts are gone from this paragraph rather than corrected to today's, because they will be
wrong again the next time a service is added and nothing recomputes a number written in
prose. `tests/unit/test_release_records.py` checks the split instead.

**The records' own image tables are not stale, and must not be "corrected".** The 0.3.0
table lists `ohif/app:v3.9.2` with a real manifest digest; that image is no longer in the
compose file, which now runs `nginx:1.27-alpine` in its place, because OHIF was withdrawn as
the clinician surface **after** that record was written. The row is a true statement about
0.3.0. The same goes for every record's `| Specification | v0.2.0 |` cell against a tree
whose chapters now read v0.4.0. A path or a version in these files is EVIDENCE, not an
ADDRESS — see `docs/README.md`, "Release records are not to be edited".
