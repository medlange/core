<!-- MedicalOS Specification v0.4.0 — chapter 18 of 19. Normative.
     212 requirements. Do not edit without a requirement-ID review. -->

[← 17. Model Development Pipeline](17-training-pipeline.md) · [Index](../../MEDICALOS_SPEC.md) · [19. Operator Surfaces →](19-operator-surfaces.md)

---

## 18. Standards and Regulatory Conformance

Chapters 1 through 17 state what MedicalOS does. This chapter states, clause by clause, which of
those statements a notified body, an FDA reviewer or a hospital clinical-safety officer can be
pointed at when they ask "show me how this satisfies *<standard>*" — and, with equal weight, where
the honest answer is "it does not yet".

The chapter is a **mapping**, not a design. It introduces almost no new engineering requirements:
its normative content is the rules for how a conformance claim may be made, the assignment of every
obligation to exactly one owner, and the small number of things the platform must publish about
*itself* in order to be cheap for a publisher to certify around. Everything else in it is a pointer
into chapters 1–17 or an admission that no pointer exists.

The gap list this chapter produces is not an embarrassment to be minimised. It is the project plan
for becoming certifiable, and it is the most valuable output here. The specification is unusually
strong on provenance, evidence, traceability and supply-chain integrity — and genuinely weak on
lifecycle process, risk management, usability engineering and post-market surveillance. Both
sentences are stated in the mapping tables with requirement IDs behind the first and blank cells
behind the second.

### 18.1. Scope, and how to use this chapter

#### 18.1.1. What this chapter is

- **MOS-CONF-001** — This chapter maps clauses of named external standards and regulations onto the
  requirement IDs of chapters 1–17 that satisfy them, names the party that owns each obligation, and
  records the ones that nothing in this document satisfies. It is a navigational and evidentiary
  document. It MUST NOT be read as creating, widening or relaxing any requirement defined in another
  chapter.
- **MOS-CONF-002** — Where this chapter and the owning chapter disagree about what a requirement
  says, the owning chapter wins and the disagreement is a defect in this chapter (`MOS-CORE-006`,
  ch 1). A mapping row is a cross-reference under `MOS-CORE-007`, never a restatement.
- **MOS-CONF-003** — This chapter MUST NOT change the regulatory status of any component. In
  particular it does not make MedicalOS core a medical device: `MOS-CORE-005` (ch 1) and
  `MOS-SAFE-001`–`MOS-SAFE-003` (ch 9) are unaffected by anything written here. A reader who
  finishes this chapter believing the core has become a device has read it wrong, and
  `MOS-CONF-010` exists to make that reading impossible to support in print.

#### 18.1.2. What this chapter is not

- **MOS-CONF-004** — This chapter is not legal or regulatory advice, and MUST carry that statement
  in `docs/REGULATORY.md` alongside the sentence `MOS-SAFE-007` already requires there. Jurisdictional
  determinations — whether a given service is a device, which class it falls in, whether an in-house
  exemption applies — are made by the party named as owner, not by this document and not by the
  platform (`MOS-SAFE-006`, `MOS-SAFE-029`).
- **MOS-CONF-005** — This chapter is not a certificate, an audit result, or evidence of an audit. A
  row in any table here MUST NOT be presented as a finding made by a third party. Every `SATISFIED`
  in this chapter is a self-assessment by the authors of this specification against requirement text
  in this specification, and every table MUST carry that qualification where it is rendered outside
  the document.
- **MOS-CONF-006** — Clause numbers, clause titles and clause summaries in this chapter are the
  authors' reading and MUST be verified against a controlled copy of the standard before any external
  use. The standards themselves are copyrighted and are not reproduced here; the "What it requires"
  column is a paraphrase written to be checkable, not a quotation. Every mapping row MUST carry a
  `clause_verified` boolean alongside its status — distinct from the status vocabulary of §18.1.5,
  which grades the platform, not the citation. A row with `clause_verified: false` MUST NOT be shown
  to an external reviewer, and a rendering that omits the field MUST be treated as `false`.

#### 18.1.3. Conformance is claimed per clause, with an owner — never as a blanket

- **MOS-CONF-007** — A conformance statement about MedicalOS MUST name (a) the standard and its
  edition, (b) the specific clause, (c) the owner from §18.1.4, (d) the requirement IDs that satisfy
  it, and (e) a status from §18.1.5. A statement missing any of the five is not a conformance
  statement and MUST NOT be published.
- **MOS-CONF-008** — Blanket claims of the form "MedicalOS is ISO 13485 compliant", "MedicalOS is
  IEC 62304 certified", "MedicalOS is HIPAA compliant" or "MedicalOS is MDR compliant" MUST NOT
  appear in this specification, the repository, the UI, the API, release notes, or marketing. For an
  infrastructure component that is not a device and has no quality management system, such a
  statement is not merely imprecise: it is false, and it is the kind of false that ends an audit.
  This extends `MOS-SEC-007` (ch 8), which already forbids representing the platform as *providing*
  HIPAA, GDPR or MDR compliance, from those three regimes to every standard named in this chapter,
  and it extends `MOS-SAFE-008`'s banned-vocabulary rule from the word "validation" to the word
  "compliant". CI MUST grep for the banned forms exactly as `MOS-SAFE-008` and `MOS-REL-097` already
  require for their own lists.
- **MOS-CONF-009** — The permitted form is per clause and per owner. The canonical phrasing is:

  > *IEC 62304:2006+A1:2015 clause 5.3.3, owner PUBLISHER. MedicalOS supplies the evidence a
  > publisher needs to satisfy this clause for MedicalOS as a SOUP item: `MOS-REL-001`,
  > `MOS-REL-002`, `MOS-TEST-002`. Status: PARTIAL — no single exported platform data sheet.*

  A claim in this form is falsifiable by reading the cited requirement, which is the property that
  makes it worth making.
- **MOS-CONF-010** — Every rendering of this chapter — printed, exported, or shown in the UI — MUST
  carry the `MOS-SAFE-001` positioning statement verbatim at its head, and MUST carry the sentence
  `MedicalOS core is not a medical device; the obligations mapped here attach to the owner named in
  each row.` Both sentences MUST be reproduced verbatim and MUST NOT be paraphrased, shortened or
  moved below the first table.
- **MOS-CONF-011** — The three conformance profiles of `MOS-CORE-010` — `platform-core`,
  `service-publisher`, `deploying-site` — are the only profiles, and this chapter MUST NOT introduce
  a fourth. A per-clause claim under `MOS-CONF-007` is made *inside* a profile claim under
  `MOS-CORE-011`, which already requires the claimant to name the profile, the document version and
  the `sha256` digest of `spec/requirements.yaml`. A per-clause claim that does not sit inside a
  valid `MOS-CORE-011` profile claim is unanchored and MUST be refused.

#### 18.1.4. The three owners

Every obligation in this chapter is assigned to exactly one of three owners. The owners are not new;
they are the three conformance profiles of `MOS-CORE-010` and the three columns of `MOS-SAFE-004`,
renamed for compactness.

| Owner | Who | Conformance profile (`MOS-CORE-010`) | `MOS-SAFE-004` column | What the platform does for them |
|---|---|---|---|---|
| **PLATFORM** | The MedicalOS project and the operator of an installation | `platform-core` | *MedicalOS project (upstream)* | — it is the obligated party |
| **PUBLISHER** | The `ServiceVersion` publisher named in `clinical.legal_manufacturer` — the legal manufacturer of a clinical claim | `service-publisher` | *ServiceVersion publisher (manufacturer)* | supplies evidence, hooks and machine-checkable gates |
| **SITE** | The deploying hospital, imaging network or research group | `deploying-site` | *Deploying site (operator)* | supplies controls, configuration and audit |

- **MOS-CONF-012** — Every obligation mapped in this chapter MUST carry exactly one owner. An
  obligation shared between two parties MUST be split into two rows with one owner each, never
  recorded as "PLATFORM and SITE". Shared ownership of a regulatory obligation is how obligations go
  unperformed.
- **MOS-CONF-013** — A clause with **no** owner is a **gap**, and MUST be recorded as a `GAP` row in
  the mapping table, entered in the chapter's gap list, and — where it is a decision rather than an
  omission — carried to chapter 16 as an open question under `MOS-CORE-058`. A clause MUST NOT be
  silently omitted from a mapping table to avoid recording a gap. Silence in this document is never
  permission (`MOS-CORE-008`).
- **MOS-CONF-014** — Assigning an owner is not the same as that owner having performed the
  obligation. `Owner: PUBLISHER` means the obligation is the publisher's to discharge; it says
  nothing about whether any particular publisher has. The platform MUST NOT infer, assert or default
  a publisher's or a site's conformance from the fact that an owner is named — the same posture
  `MOS-SAFE-009` and `MOS-SAFE-029` take toward `intended_use` and `regulatory_status`, for the same
  reason.

#### 18.1.5. Status vocabulary

- **MOS-CONF-015** — Every mapping row MUST carry exactly one status from this closed set. Anything
  other than `SATISFIED` MUST carry a one-line justification in the same row.

| Status | Meaning | Evidence required in the row |
|---|---|---|
| `SATISFIED` | Requirement IDs in this document fully discharge the clause for the named owner | the requirement IDs |
| `PARTIAL` | The clause is addressed but not fully; a named part is missing | the requirement IDs **and** the named missing part |
| `GAP` | Nothing in this document addresses the clause for the named owner | what would have to exist |
| `NOT-APPLICABLE` | The clause does not reach the named owner, for a stated structural reason | the reason, not merely the assertion |

- **MOS-CONF-016** — `NOT-APPLICABLE` MUST be justified structurally, by a requirement ID that makes
  the clause unreachable — for example, DICOM object writing does not reach a PUBLISHER because the
  ownership boundary of chapter 2 assigns all DICOM writing to the platform. "We do not do that" is
  not a justification; "no code path exists by which we could, because `MOS-XXX-NNN`" is.
- **MOS-CONF-017** — `PARTIAL` MUST NOT be used where `GAP` is correct. A clause with one adjacent
  requirement and no coverage of its substance is a `GAP`. The temptation to grade generously is
  exactly what makes a conformance document worthless, because a reviewer who finds one inflated row
  re-reads every other row with suspicion.
- **MOS-CONF-018** — Every requirement ID cited anywhere in this chapter MUST exist as a definition
  site in `spec/requirements.yaml` (`MOS-CORE-021`, `MOS-CORE-024`). CI MUST fail on a citation in
  this chapter that resolves to no definition, and MUST additionally fail on a citation to a
  requirement whose `status` is `withdrawn` (`MOS-CORE-023`). A conformance document that cites a
  requirement that does not exist fails an audit on the first spot-check, and every other row is then
  assumed to be equally invented.
- **MOS-CONF-019** — `CONF` MUST be added to the area-code regular expression of `MOS-CORE-020` and
  to the allocation table of `MOS-CORE-022` as `CONF | Standards and regulatory conformance | 18 |
  001–299`, and chapter 18 MUST be added to the chapter map of §1.7.1, the reading paths of §1.7.2 —
  where the *Regulatory reviewer* path becomes `1 → 9 → 18 → 2 → 7` — and the document's chapter
  count. Until that amendment lands, every ID in this chapter fails `MOS-CORE-020` and the generated
  index of `MOS-CORE-024` is incomplete. This is a documentation-consistency obligation, not a
  conformance gap, and it MUST be made in the same change as this chapter.

### 18.2. The manufacturer boundary

Everything downstream in this chapter depends on one question, and the question is not "is MedicalOS
a medical device". It is: **for this particular deployment, who is the legal manufacturer of the
clinical claim?** The previous specification never asked it. Chapter 9 answers it for the general
case — `MOS-SAFE-003` makes the `ServiceVersion` publisher named in `clinical.legal_manufacturer` the
manufacturer of every clinical output, and `MOS-SAFE-004` divides the responsibilities in a table
that "MUST NOT be silently reassigned by configuration". This section applies that answer to the four
deployment shapes the platform actually produces, because the answer differs in each, and because
three of the four are shapes in which the site or the operator is closer to being a manufacturer than
they expect.

- **MOS-CONF-020** — Four deployment shapes are recognised. Every `Deployment` MUST be classifiable
  into exactly one. A deployment that fits none is **undefined scope** under `MOS-CORE-008`, MUST be
  refused rather than resolved by assumption, and MUST be raised as an open question.

#### 18.2.1. The four shapes

| # | Shape | Legal manufacturer of the clinical claim | Platform's role | Site's role | Anchors |
|---|---|---|---|---|---|
| **(a)** | **Third-party sealed service.** A vendor publishes a signed `ServiceVersion`; the site deploys it. | The vendor, named in `clinical.legal_manufacturer` | Integrator and record-keeper; makes no claim | Verifies the claim applies in its jurisdiction; owns the decision to run `clinical` | `MOS-SAFE-003`, `MOS-SAFE-004`, `MOS-SVC-012`, `MOS-SVC-034`, `MOS-REG-027`, `MOS-REG-028` |
| **(b)** | **First-party service published by the platform operator.** The same organisation maintains MedicalOS and publishes a service onto it. | The operator, *acting as publisher* — a distinct legal role from *acting as platform maintainer* | Unchanged: the core still makes no claim | As (a), and MUST NOT be told the service is "part of the platform" | `MOS-SAFE-002`, `MOS-SAFE-010`, `MOS-CORE-014`, `MOS-CORE-004` |
| **(c)** | **Site-trained model.** The site harvests, curates, trains, evaluates and promotes its own model through chapter 17. | The site | Supplies the pipeline, the evidence plane and the promotion gate; asserts nothing | Becomes the manufacturer; declares its own `legal_manufacturer` and its own `regulatory_status` | `MOS-SAFE-005`, `MOS-SAFE-006`, `MOS-TRAIN-173`, `MOS-TRAIN-174`, `MOS-TRAIN-189` |
| **(d)** | **Research use, no clinical claim.** The platform runs with every `Deployment` at `clinical_use_mode: research_only`. | None — no clinical claim is made by anyone | Enforces the research boundary in code, at eight named points | Owns the research governance, ethics approval and data legal basis | `MOS-SAFE-033`, `MOS-SAFE-035`, `MOS-SAFE-039`–`MOS-SAFE-043`, `MOS-SAFE-050`, `MOS-SAFE-051` |

#### 18.2.2. Shape (a) — third-party sealed service

- **MOS-CONF-021** — In shape (a) the PLATFORM owner holds no device obligation and the PUBLISHER
  owner holds all of them. The platform's obligations are those of an integrator: carry the claim
  faithfully (`MOS-SAFE-021`, `MOS-SAFE-022`), write it into every generated object
  (`MOS-IMG-075`, `MOS-IMG-076`, `MOS-SAFE-020`), refuse to author or adjust it (`MOS-SAFE-009`),
  and gate clinical use on evidence rather than on assertion (`MOS-SAFE-036`).
- **MOS-CONF-022** — The platform MUST NOT be described as verifying the publisher's regulatory
  status. `MOS-SAFE-029` is explicit that the platform stores, digests, displays and reproduces
  `identifier`, `authorising_body` and `evidence_uri` without interpreting them, and that verification
  is the SITE's act. A conformance claim that implies otherwise misassigns an obligation that
  currently has an owner, which is worse than a gap.

#### 18.2.3. Shape (b) — a first-party service, and the conflict of interest in it

This is the shape that looks safest and is not. When the organisation that maintains MedicalOS also
publishes a service onto it, the two roles collapse in every direction except the legal one, and
every mechanism chapter 9 relies on — a distinct signing key, a distinct `legal_manufacturer` block,
a gate the publisher cannot reach — is being operated by the same people on both sides.

- **MOS-CONF-023** — The platform project and the publisher of a first-party service MUST be treated
  as distinct parties for every purpose in this chapter, even where they are the same legal entity.
  `MOS-SAFE-002` already forbids the project from making a clinical claim, and `MOS-SAFE-010` already
  states that a packager who publishes a `ServiceVersion` under their own key is a manufacturer while
  a contributor to core is not. `MOS-CONF-023` adds the consequence: a first-party service MUST be
  published as an ordinary `ServiceVersion` through the ordinary registry path, signed with a key
  bound to the publishing entity (`MOS-REG-028`), integrated exclusively through the public surface
  (`MOS-CORE-014`), and subject to the same clinical gate as a third party's (`MOS-SAFE-036`).
- **MOS-CONF-024** — A first-party service MUST NOT be bundled into a platform release artifact, MUST
  NOT be installed by default, and MUST NOT be presented in any UI, document or release note as
  "included", "built in" or "part of MedicalOS". `MOS-REL-001` defines a release as first-party
  containers plus signatures; a service is not one of them. The reason is not branding: if the
  service ships inside the release, the release becomes a device, and `MOS-CORE-005` stops being
  true.
- **MOS-CONF-025** — The release plan's 0.1.0 contents include a first-party native pleural-effusion
  service and a vendored lung segmentation (`MOS-REL-003`'s Release Decision Record records what
  actually shipped; ch 15 §15.1.2 fixes the contents). Both are shape (b) artifacts, not platform
  components: the pleural-effusion service is first-party, and repackaging a third-party lung
  segmentation under the operator's own signing key is itself a manufacturing act under
  `MOS-SAFE-010`. The Release Decision Record MUST record both as `ServiceVersion`s with their own
  `legal_manufacturer`, separately from the platform images it lists by digest.
- **MOS-CONF-026** — Whether the approver of a first-party service's clinical gate
  (`deployment.approve_clinical`, `MOS-SAFE-036`) may be an employee of the publishing entity is a
  governance question this chapter does not settle. `MOS-TRAIN-175` already carries the analogous
  question — whether one person may hold all three of `evidence.report.issue`, `artifact.approve` and
  `deployment.approve_clinical` in a `clinical` tenant — to chapter 16. The first-party variant is
  sharper, because there the publisher and the platform operator may be the same team, and it MUST be
  carried alongside it.

#### 18.2.4. Shape (c) — a model the site trained itself

- **MOS-CONF-027** — A site that trains a model through chapter 17 and promotes it into clinical use
  is the manufacturer of that model's clinical claim. This is not a new ruling: `MOS-SAFE-005`
  already makes a site that substitutes weights or edits a `PreprocessingSpec` the manufacturer of
  the result, and makes it *mechanically* true by failing the `MOS-SAFE-036` gate with
  `signature_not_from_declared_manufacturer` until the site supplies and signs its own
  `legal_manufacturer` block. Chapter 17's output lands in exactly that position.
- **MOS-CONF-028** — The platform MUST NOT assert that a health-institution in-house exemption
  applies to a site-trained model. `MOS-SAFE-006` offers `regulatory_status[].status =
  in_house_exemption` as an explicitly declared, auditable claim the site makes, carrying the site's
  own identity — and `MOS-SAFE-025` places `in_house_exemption` in the CLEARED set, meaning a site
  that declares it passes the clinical gate on its own assertion. That is the correct design and it
  is also the single highest-leverage self-assertion in the document: the SITE owner carries the full
  weight of it, and this chapter MUST say so in every place the exemption is mapped.
- **MOS-CONF-029** — Chapter 17's structural guarantees are the platform's contribution to shape (c)
  and MUST be cited wherever a site-trained model's lifecycle is mapped: no non-human principal holds
  `evidence.report.issue`, `artifact.approve`, `deployment.approve_clinical`, `deployment.promote` or
  `deployment.gate.override` (`MOS-TRAIN-174`); three distinct human acts with three distinct
  permissions and three audit records (`MOS-TRAIN-173`); and no call path from a `TrainingRun`,
  `ConversionRun`, `EvaluationRun` or `ValidationReport` to a `SERVING` deployment
  (`MOS-TRAIN-189`). Together these are a strong lifecycle control over *promotion*. They are not a
  lifecycle control over *development*, and §18.3 and the mapping tables MUST NOT let the first be
  read as the second.
- **MOS-CONF-030** — A site operating in shape (c) is simultaneously the SITE owner and the PUBLISHER
  owner. Every mapping table MUST be readable that way: a site-trained deployment inherits the union
  of the two columns, not the intersection. This is the shape in which a site most often discovers it
  has acquired obligations it did not price, and the union reading is what surfaces that before
  promotion rather than after.

#### 18.2.5. Shape (d) — no clinical claim at all

- **MOS-CONF-031** — In shape (d) no party is a manufacturer, because no clinical claim exists. The
  research boundary is not a policy statement: it is the eight enforcement points of `MOS-SAFE-035`,
  each with a named failure — the promotion gate, mode pinning onto `Job` and `Result`
  (`MOS-SAFE-034`), the marking gate before STOW (`MOS-SAFE-039`), the destination gate
  (`MOS-SAFE-040`), the read-path filter (`MOS-SAFE-041`), the SR verification gate
  (`MOS-SAFE-042`), the export gate (`MOS-SAFE-043`) and the one-way demotion path. The default is
  `research_only` with no tenant-wide, service-wide or environment-wide override (`MOS-SAFE-033`).
- **MOS-CONF-032** — Absence of a device obligation in shape (d) MUST NOT be presented as absence of
  obligation. Data-protection obligations, research-ethics approval and the lawful basis for using
  patient data reach the SITE owner in full, and the platform's contribution is controls, not
  discharge: de-identification under DICOM PS3.15 Annex E (`MOS-DATA-027`), retention and erasure
  (`MOS-SEC-126`–`MOS-SEC-132`), and, for training data, the site-declared `legal_basis` and
  `basis_reference` that `MOS-TRAIN-074` requires the platform to store, digest, display and refuse
  the harvest without — while explicitly forbidding the platform from assessing their sufficiency.
  No field anywhere in the document records a research-ethics approval reference, and §18.4's
  mapping MUST record that as a `GAP` against the SITE owner.

### 18.3. MedicalOS as SOUP

#### 18.3.1. Why this section exists

A publisher shipping a device that runs on MedicalOS does not get to treat the platform as
furniture. Under IEC 62304, software the manufacturer did not develop under a compliant process and
cannot fully characterise is **SOUP** — Software of Unknown Provenance — and SOUP carries specific,
enumerable obligations in that manufacturer's software file. MedicalOS is SOUP in every publisher's
62304 file, in all four shapes of §18.2 where a clinical claim exists.

This is the inversion that makes the section worth writing. `MOS-CORE-005` says the core is not a
device, and that is a statement about the platform's *own* obligations. It says nothing about the
platform's *customers'* obligations, and the customer's obligation is where the platform's product
argument lives: a platform that is expensive to characterise is a platform that raises every
publisher's certification cost, and a platform that is cheap to characterise is a reason to build on
MedicalOS rather than on a competitor. The spec's existing strengths — signed digest-pinned releases,
a generated requirement index, a requirement-to-test traceability file, SBOMs on every artifact — are
most of the raw material. What is missing is packaging and a formal anomaly list.

- **MOS-CONF-033** — MedicalOS MUST be documented, in `docs/REGULATORY.md` and in
  `docs/services/AUTHORING.md` (`MOS-REL-095`), as SOUP for the purposes of a publisher's IEC 62304
  software file, and MUST NOT be described to publishers in any way that suggests the platform's
  presence in their file is optional or that the platform discharges their SOUP obligations.
- **MOS-CONF-034** — The platform MUST NOT state or imply a software safety classification (62304
  class A/B/C) for itself. Classification is performed by the manufacturer, against that
  manufacturer's hazard analysis and risk controls, for the system that manufacturer is placing on
  the market. `MOS-SAFE-019`'s IMDRF categorisation is a structural consistency check on a
  publisher's own declaration and is explicitly "not a regulatory determination"; it MUST NOT be
  reused as a platform self-classification.

#### 18.3.2. What a publisher must document about MedicalOS, and what the platform supplies

The obligations below are the ones a publisher must discharge about MedicalOS in their own file.
Owner is **PUBLISHER** for every row — this is the publisher's file, not the platform's. The
platform's job, and the subject of §18.3.3, is to make each row cheap.

| IEC 62304 obligation (2006+A1:2015) | What the publisher must record about the platform | What the platform supplies today | Status |
|---|---|---|---|
| **8.1.2** — Identify SOUP: title, manufacturer, unique designator | Name `MedicalOS`, the project as supplier, and an exact version | `MOS-CORE-001` fixes the name; `MOS-REL-001` makes a release a signed tag plus OCI images referenced **by digest** with SPDX 2.3 SBOMs and cosign signatures; `MOS-REL-002` states pre-1.0 version semantics explicitly; `MEDICALOS_BUILD_COMMIT` and `MEDICALOS_IMAGE_DIGEST` are baked at build time and surfaced by `/version` (ch 13 §13.2) | **SATISFIED** |
| **5.3.3** — Specify functional and performance requirements for the SOUP item | State what the publisher relies on the platform to do, and how well | Chapters 1–17 are that specification; `MOS-CORE-024` generates `spec/requirements.yaml` from the text and CI fails on drift; `MOS-TEST-002` maps every requirement to at least one executable check in `tests/traceability.yaml`; `MOS-REL-098` requires every ID to be greppable from the repository | **PARTIAL** — the material exists but is not exported as a single platform data sheet a publisher can paste into a file; no stated performance envelope (throughput, latency, capacity) for the platform as a whole |
| **5.3.4** — Specify the system hardware and software required by the SOUP item | State what MedicalOS itself needs to run | `MOS-OPS-009`/`MOS-OPS-010` fix the configuration contract to environment variables validated at startup; ch 13 §13.9 gives the production workload table and the three agnosticism constraints (`MOS-OPS-061`–`MOS-OPS-063`); `MOS-OPS-070` pins per-artifact `built_for` CUDA compute capability and TensorRT version | **PARTIAL** — no single stated *intended operating environment*: no supported PostgreSQL, Kubernetes, container-runtime, host-OS, GPU-driver or browser matrix, and no statement of which combinations are tested |
| **7.1.3** — Evaluate the SOUP supplier's published anomaly list | Obtain the platform's anomaly list, assess each entry against their own hazards, and record the assessment | `docs/spec/99-known-inconsistencies.md` exists and has the right shape: twenty-two entries, each with cited chapter and line numbers, a *Consequence* and a *Resolution* | **PARTIAL** — it is a specification-defect register, explicitly marked non-normative, with no stable identifiers, no severity, no affected-version range, no workaround field, no fixed-in version, no machine-readable form, and no coverage of runtime software anomalies. §18.3.4 specifies what it must become |
| **7.1.2** — Identify potential causes of a hazardous situation, including failure of SOUP | Include "MedicalOS fails or behaves unexpectedly" in their hazard analysis | Failure semantics are unusually well specified — `REJECTED` as a clinical outcome distinct from `FAILED` (spine §4, `MOS-EXEC-001`), RFC 9457 `class` separating `clinical_rejection` from transport and system failure (`MOS-API-037` table 10.4-A, `MOS-API-039`), the marking gate that fails the job rather than emit an unmarked object (`MOS-SAFE-039`) | **PARTIAL** — failure *behaviour* is specified; there is no hazard analysis, no risk file and no enumeration of the platform's own failure modes as hazards. The document contains no FMEA, no ISO 14971 artifact and no risk-control table anywhere |
| **§6 maintenance / §9 problem resolution** | Know how defects are reported, triaged and fixed, and for how long a version is supported | `SECURITY.md` is mandatory and must carry reporting channel, response commitment and supported versions (`MOS-REL-095`); `MOS-SEC-143` sets a CVSS-banded vulnerability budget with deadlines (7/30/90 days); `MOS-SEC-144` requires daily rescanning of published digests; `MOS-API-091`/`MOS-API-095` give API deprecation a 180-day `Deprecation`/`Sunset` notice and a 12-month `/api/v1` floor after `/api/v2` | **PARTIAL** — security defects have a budget and the HTTP API has a deprecation policy; non-security defects have neither a triage process nor a published response commitment, and no platform-release support window or end-of-life policy exists |

#### 18.3.3. The product argument: make MedicalOS cheap SOUP

- **MOS-CONF-035** — MedicalOS MUST make itself cheap to characterise as SOUP. This is a product
  requirement, not a courtesy: every hour a publisher spends reconstructing what the platform is, what
  it needs, and what is known to be wrong with it is an hour that argues for a competitor. The five
  obligations below are the whole of it.
- **MOS-CONF-036** — **Versioned, digest-pinned releases.** Already satisfied by `MOS-REL-001`,
  `MOS-REL-002` and `MOS-REL-003`. The platform MUST NOT weaken it: a release that exists only as a
  branch or a mutable tag is unusable as a SOUP designator, and `MOS-SEC-140` already forbids a
  mutable tag in any deployable configuration.
- **MOS-CONF-037** — **A published anomaly list.** The platform MUST publish, per release, a
  machine-readable anomaly register meeting §18.3.4, at a stable URL and inside the release artifact
  so that an air-gapped site has it without network access (`MOS-SEC-141` establishes that on-prem
  with no internet is the primary shape).
- **MOS-CONF-038** — **A documented support and deprecation policy.** The platform MUST publish, for
  each release line: the date support begins, the date it ends, what "supported" entails (security
  patches only, or defect fixes too), and the minimum notice before a supported version's end of
  life. `MOS-API-095` already does this for the HTTP surface at 180 days; there is no equivalent for
  the platform itself, and `MOS-CONF-038` is a `GAP` until one is written.
- **MOS-CONF-039** — **A stated intended operating environment.** The platform MUST publish a single
  table naming the supported host OS, container runtime, orchestrator, PostgreSQL version, object
  store interface, GPU compute capabilities and viewer versions, distinguishing *tested*, *supported*
  and *known to work*. `MOS-OPS-070` shows the pattern at artifact level — an exact `built_for` with a
  hard refusal on mismatch, and an explicit rejection of a vague `min_triton: "2.x"` as a
  compatibility statement. The platform MUST NOT state its own compatibility any more vaguely than it
  requires of a model artifact.
- **MOS-CONF-040** — **A platform SOUP data sheet.** The platform MUST generate, per release, a
  single document containing the four preceding items plus the `MOS-CORE-011` conformance-claim triple
  (profile, document version, `spec/requirements.yaml` digest), the SBOM references of
  `MOS-SEC-142`, and the `tests/traceability.yaml` summary of `MOS-TEST-002`. It MUST be generated
  under the drift-check regime of `MOS-REL-095`, never hand-maintained, and it MUST be the single
  artifact a publisher cites in their 62304 file. A publisher who has to assemble this from fifteen
  chapters will assemble it wrong, and the platform will be blamed for the error.

#### 18.3.4. What the Known Inconsistencies register must become

`docs/spec/99-known-inconsistencies.md` is already the right shape — a per-defect register with
precise citations, a stated consequence and a stated resolution, re-verified against the current text
rather than carried forward on trust. It is not yet an anomaly list in the 62304 sense, and the eight
requirements below are what it is missing — none of which changes its character.

- **MOS-CONF-041** — Every entry MUST carry a stable identifier `MOS-ANOM-NNNN`, immutable and never
  reused, under the same rule `MOS-CORE-023` applies to requirement IDs. Ordinal numbering ("defect
  4") cannot be cited from an external file, because the number changes when an entry above it
  closes.
- **MOS-CONF-042** — Every entry MUST carry these fields, and an entry missing any of them MUST fail
  CI:

| Field | Meaning |
|---|---|
| `id` | `MOS-ANOM-NNNN` |
| `title` | one line |
| `category` | `specification_defect` \| `implementation_defect` \| `documentation_defect` |
| `affects` | requirement IDs and, for an implementation defect, the component |
| `introduced_in` | release version, or `pre-0.1.0` |
| `affected_versions` | a version range |
| `status` | `open` \| `fixed` \| `wont_fix` \| `superseded` |
| `fixed_in` | release version, required when `status = fixed` |
| `severity` | `critical` \| `major` \| `minor`, per `MOS-CONF-043` |
| `clinical_impact` | prose: what a clinician or patient could experience, or `none` |
| `workaround` | prose, or the literal `none known` |
| `consequence` | the existing field, retained |
| `resolution` | the existing field, retained |

- **MOS-CONF-043** — `severity` MUST be assigned against clinical effect, not engineering effort:
  `critical` where the anomaly could cause a wrong or missing clinical output to be presented as
  correct, or could cause PHI to escape a boundary; `major` where it could cause a job to fail, a
  result to be unavailable, or a stated guarantee to be unmet without a wrong output; `minor`
  otherwise. The platform MUST NOT assign a severity by consensus about how hard the fix is, which is
  the failure mode every such register eventually develops.
- **MOS-CONF-044** — The register MUST cover **runtime software anomalies**, not only specification
  defects. Today it covers only the latter. A publisher's 7.1.3 evaluation is about the software they
  are shipping; a register of contradictions between chapters is necessary and is not sufficient.
- **MOS-CONF-045** — The register MUST be published in a machine-readable form
  (`docs/spec/anomalies.yaml`) generated from the same source as the prose, under the generated-file
  regime of `MOS-CORE-024` and `MOS-REL-095`: hand-editing forbidden, CI failing on drift.
- **MOS-CONF-046** — A snapshot of the register MUST be included in every release artifact and
  referenced from the Release Decision Record of `MOS-REL-003`, so that "the anomaly list as of
  version X" is a fixed, retrievable object. A living document with no per-version snapshot cannot be
  cited by a publisher whose file was frozen at version X.
- **MOS-CONF-047** — The register MUST be removed from `non-normative` status to the extent of its
  *existence and completeness* — the obligation to maintain it becomes normative — while the entries
  themselves remain descriptions of defects rather than requirements. An anomaly list a supplier is
  not obliged to maintain is not an anomaly list a manufacturer can rely on.
- **MOS-CONF-048** — Closing an entry MUST set `status = fixed` and `fixed_in`, and MUST NOT delete
  the entry. The current register already does this correctly for the two entries retained "only for
  the record"; `MOS-CONF-048` makes that practice a rule, because a publisher who assessed
  `MOS-ANOM-0007` needs to find it after it closes in order to re-assess it.

### 18.4. The ownership matrix

One table, every major obligation family, three owners. A cell names the requirement IDs that carry
the obligation for that owner, or states `none — gap`. Per-clause mappings against named standards
follow in §18.5 onward; this table is the index over them.

- **MOS-CONF-049** — The matrix below is the authoritative assignment of obligation families to
  owners for this document. A per-clause row in any later section MUST be consistent with it, and a
  clause that cannot be placed under any family in this table MUST be raised under `MOS-CONF-013`
  rather than assigned to a family it does not belong to.
- **MOS-CONF-050** — `none — gap` in a cell is a claim about *this document*, not about the world. It
  means no requirement in chapters 1–17 places the obligation on that owner. It does not mean the
  obligation does not exist; for a PUBLISHER or a SITE the obligation usually exists in law
  regardless, and the gap is that the platform provides no hook, evidence or control for it.
- **MOS-CONF-051** — `n/a` in a cell MUST meet the structural-justification bar of `MOS-CONF-016`.

| # | Obligation family | PLATFORM | PUBLISHER | SITE |
|---|---|---|---|---|
| 1 | **Clinical claim and intended use** | `MOS-CORE-004`, `MOS-SAFE-002`, `MOS-SAFE-009`, `MOS-SAFE-021`, `MOS-SAFE-022` — carry, digest, display; never author | `MOS-SAFE-013`, `MOS-SAFE-014`, `MOS-SAFE-017`, `MOS-SAFE-020` — declare the `clinical` block | `MOS-SAFE-028` (jurisdiction per `Deployment`), `MOS-SAFE-036` (named approver) |
| 2 | **Regulatory conformity / marketing authorisation** | `MOS-SAFE-025`, `MOS-SAFE-026`, `MOS-SAFE-027`, `MOS-SAFE-030` — enumerate, expire, demote, query | `MOS-SAFE-023`, `MOS-SAFE-024` — declare per jurisdiction | `MOS-SAFE-029` (verification is the site's act), `MOS-SAFE-006` (in-house exemption is the site's claim) |
| 3 | **Clinical evidence** | `MOS-EVID-127`, `MOS-EVID-129`, `MOS-SEC-139` — one report type, a site acceptance suite, signing the claim not only the bytes | `MOS-EVID-117`, `MOS-EVID-122`, `MOS-EVID-128`, `MOS-REG-032` | `MOS-EVID-129`, `MOS-EVID-131`, `MOS-EVID-134` — run SAT-1…SAT-5, report it as what it is |
| 4 | **Quality management system (ISO 13485)** | none — gap | none — gap | none — gap |
| 5 | **Risk management (ISO 14971)** | none — gap; `MOS-OPEN-025` records that a risk file is undecided | `MOS-SAFE-019` (IMDRF category, structural check only), `MOS-REG-032` (`known_failure_modes` must be non-empty) — no risk file, no hazard analysis, no risk controls | none — gap |
| 6 | **Software lifecycle process (IEC 62304)** | `MOS-REL-116`, `MOS-REL-117`, `MOS-REL-123`, `MOS-REL-003`, `MOS-TEST-002`, `MOS-REL-098` — change classes, gate matrix, DoD, decision records, requirement→test traceability | none — gap; the platform imposes no lifecycle obligation on a publisher beyond manifest completeness | n/a — a site that only deploys is not developing software; a site in shape (c) reads the PUBLISHER cell |
| 7 | **SOUP declaration and anomaly list** | `MOS-REL-001`, `MOS-REL-002`, `MOS-SEC-142`, `MOS-CONF-035`–`MOS-CONF-048` | owns the 62304 obligation; consumes what the PLATFORM cell supplies | n/a — the site is not a 62304 manufacturer except in shape (c) |
| 8 | **Usability engineering (IEC 62366-1)** | `MOS-SAFE-012`, `MOS-SAFE-041`, `MOS-SAFE-047`, `MOS-TRAIN-011` — specific UI obligations, not a usability engineering process | none — gap | none — gap |
| 9 | **Post-market surveillance** | `MOS-EVID-135`, `MOS-EVID-136`, `MOS-EVID-137`, `MOS-EVID-139`, `MOS-EVID-142`, `MOS-SAFE-071`, `MOS-SAFE-072` — unattended drift monitoring with an honest no-ground-truth caveat | none — gap; no route by which a site's `monitoring_period` report or review statistics reach the publisher | `MOS-EVID-135`, `MOS-EVID-140` — receive, act, and require a human to recover from `SUSPENDED` |
| 10 | **Complaint handling and CAPA** | none — gap | none — gap | none — gap |
| 11 | **Field safety, recall and traceability of affected records** | `MOS-SVC-103`, `MOS-REG-040`, `MOS-REG-092`, `MOS-SAFE-031`, `MOS-SAFE-032` — impact query, no deletion, revocation is forward-only | `MOS-SAFE-025` (`status: withdrawn`), `MOS-REG-021`/`MOS-REG-022` (`RECALLED`, irreversible, all deployments forced to `DRAINING`) | none — gap; no field-safety-notice delivery, acknowledgement or closure record |
| 12 | **Labelling and AI-derived marking** | `MOS-SAFE-047`, `MOS-SAFE-039`, `MOS-SAFE-042`, `MOS-SAFE-050`, `MOS-SAFE-051`, `MOS-SAFE-053`, `MOS-IMG-075` | `MOS-SAFE-020`, `MOS-REG-027` — supply the equipment identity the writer needs | `MOS-SAFE-040` — `accepts_research` on every DICOM destination |
| 13 | **DICOM interoperability conformance** | `MOS-IMG-093`, `MOS-IMG-148`, `MOS-REL-095` (`docs/dicom/CONFORMANCE.md`, partly generated, drift-checked) | n/a — `MOS-SVC-006` gives DICOM writing to exactly one component, the platform's writer, and requires the Gateway to reject a STOW-RS presented with a service-scoped token; a service has no code path to write an object | `MOS-EVID-129` SAT-3 — verify against the site's own PACS and viewer |
| 14 | **Health software product safety (IEC 82304-1)** | none — gap for the product-level obligations (accompanying documents, product validation, post-market plan); the constituent parts exist across chs 9, 13 and 15 | none — gap | none — gap |
| 15 | **Information security and secure development (IEC 81001-5-1)** | `MOS-SEC-138`, `MOS-SEC-140`, `MOS-SEC-141`, `MOS-SEC-142`, `MOS-SEC-143`, `MOS-SEC-144`, `MOS-SEC-145` — signing, digest pinning, offline verification, SBOM, CVSS budget, daily rescan, suspend on failed verification | `MOS-REG-028` — signing identity bound to the declared `legal_manufacturer` | `MOS-OPS-011` (secrets as file paths), `MOS-SEC-127` (KMS root → tenant KEK → per-scope DEK; who holds the root is `OQ-08`, open) |
| 16 | **Data protection (GDPR / HIPAA and equivalents)** | `MOS-DATA-027`, `MOS-SEC-126`, `MOS-SEC-127`–`MOS-SEC-132`, `MOS-SEC-007` — controls, never compliance | n/a — `MOS-SVC-003` (no component but the Gateway holds a PACS credential), `MOS-SVC-025` (egress ⊆ `["dicom-gateway"]`), `MOS-REL-045` (no route to the database, broker or object store) | `MOS-SEC-007`, `MOS-SEC-129`, `MOS-SEC-132`, `MOS-TRAIN-074` — the covered entity's own programme, plus the site-declared training legal basis |
| 17 | **Audit trail and record retention** | `MOS-SEC-146`, `MOS-SEC-148`, `MOS-SEC-149`, `MOS-SEC-150`, `MOS-SEC-151`, `MOS-SEC-152`, `MOS-SEC-153`, `MOS-SEC-154` — hash-chained, append-only, checkpointed, offline-verifiable | n/a — audit is written by the platform about the service, not by the service | `MOS-SEC-126`, `MOS-SEC-154` — retention configuration and signed archive before partition detach |
| 18 | **Change and configuration control** | `MOS-REL-001`, `MOS-REL-002`, `MOS-REL-003`, `MOS-REL-116`, `MOS-REL-117`, `MOS-API-091`–`MOS-API-095` | `MOS-SAFE-021`, `MOS-SAFE-031`, `MOS-REG-021` — any `clinical`-block change mints a new `ServiceVersion`; no in-place edit | `MOS-SAFE-005`, `MOS-EVID-129` — modifying a version makes the site the manufacturer; upgrade re-runs SAT |
| 19 | **Clinical governance and authorisation to use** | `MOS-SAFE-035`, `MOS-SAFE-036`, `MOS-SAFE-037`, `MOS-SAFE-045` — the gate, its audit record, and a live pass/fail view | `MOS-SAFE-036` — supply the evidence the gate checks | `MOS-SAFE-033`, `MOS-SAFE-036`, `MOS-SAFE-038` — own the decision, name the approver, demote freely |
| 20 | **Human oversight and autonomy limits** | `MOS-SAFE-011`, `MOS-SAFE-057`, `MOS-SAFE-063`, `MOS-SAFE-073`, `MOS-TRAIN-174`, `MOS-TRAIN-189` — hard refusal of `autonomous`, default-DENY table, no automated path to serving | `MOS-SAFE-014` — declare `intended_use.autonomy` | `MOS-SAFE-061`, `MOS-SAFE-067`, `MOS-SAFE-068` — choose `review_mode`, grant review permissions, define clinically qualified roles |
| 21 | **Training-data governance and provenance** | `MOS-TRAIN-074`, `MOS-TRAIN-084`, `MOS-TRAIN-088`, `MOS-EVID-122` — store, digest, stratify, never assess | `MOS-EVID-117`, `MOS-EVID-128` — the evidence bundle and its named approver | `MOS-TRAIN-074` — declare `legal_basis` and `basis_reference`; the platform refuses the harvest without them |
| 22 | **Distributor / economic-operator obligations** | none — gap by deliberate deferral: `MOS-OPEN-042` (OQ-21) defaults to an evidence service with no storefront, keeping the platform out of the distribution chain | n/a while the platform is not in the distribution chain | n/a |

- **MOS-CONF-052** — Families 4, 5, 10 and 14 are `none — gap` in every column, and family 8 is a gap
  in two of three. These five are the certification backlog. A publisher can satisfy families 1, 2,
  3, 11, 12, 15, 16, 17, 18, 19 and 21 largely by pointing at this document; they cannot satisfy 4,
  5, 8, 10 or 14 by pointing at anything. The platform MUST NOT present its strength in the first
  group as coverage of the second, and any summary of this chapter MUST reproduce this paragraph.
- **MOS-CONF-053** — Families 5 and 10 are the two the platform can close most cheaply relative to
  their value, because both have existing scaffolding: family 5 has `MOS-REG-032`'s mandatory
  `known_failure_modes` and `not_validated_for` lists, `MOS-SAFE-019`'s IMDRF axes, and
  `MOS-EVID-137`'s enumerated monitoring signals to hang a hazard analysis on; family 10 has
  `MOS-SAFE-071`'s review-outcome aggregates, `MOS-EVID-135`'s signed monitoring reports and
  `MOS-REG-040`'s impact query, and needs a complaint record, a route to the publisher, and a closure
  state. Neither is a research problem. Both are unstarted, and this chapter MUST NOT imply otherwise.

### 18.4. IEC 62304 and IEC 82304-1

#### 18.4.1. How IEC 62304 reaches MedicalOS at all

IEC 62304:2006+A1:2015 is a **process** standard for the lifecycle of medical device software. It does not decide whether something is a device; it tells whoever *is* the manufacturer of device software how to plan, specify, design, verify, configure, release, maintain and fix it. MedicalOS core is not a medical device (`MOS-CORE-005`, `MOS-SAFE-001`, `MOS-SAFE-002`), so 62304 does not bind the MedicalOS project as a manufacturer. It reaches MedicalOS along three distinct paths, and conflating them is the most common failure of this kind of mapping.

**MOS-CONF-100** — This specification MUST distinguish three roles in which IEC 62304 touches MedicalOS, and every clause mapping in §18.4 MUST state which role it is written against.

| Role | What it means | Who the manufacturer is | Binding? |
|---|---|---|---|
| **R1 — MedicalOS as SOUP** | A publisher's `ServiceVersion` is device software. It runs on MedicalOS, and MedicalOS performs series selection, de-identification, geometry conversion, UID derivation and **all DICOM writing** (spine §2, `MOS-SAFE-004`). MedicalOS is therefore Software Of Unknown Provenance inside that publisher's 62304 file. | the `ServiceVersion` publisher | **Yes**, on the publisher. The platform owes evidence. |
| **R2 — MedicalOS as the publisher's verification infrastructure** | The conformance kit (`MOS-TEST-047`), the evidence plane (ch 7), the fixture corpus (ch 14) and the provenance record (`MOS-SAFE-083`) are things a publisher cites as their own 5.5/5.7/7.3 evidence. 62304 §5.1.4 makes the manufacturer responsible for the tools they rely on. | the publisher | **Yes**, on the publisher. Tool-qualification burden. |
| **R3 — MedicalOS applying 62304 to itself** | Voluntary adoption, with no certification claim. `MOS-OPEN-025` records this as an open decision with a default of "adopt requirement-ID→test traceability and a SOUP inventory from 0.2.0; make no certification claim of any kind". | the MedicalOS project | **No.** Voluntary, and currently undecided. |

**MOS-CONF-101** — The clause numbers and titles reproduced in §18.4 are working references. Before this chapter is issued to a notified body, an FDA reviewer or a customer's clinical safety officer, every clause number, clause title and class-applicability statement in §18.4 MUST be verified line-by-line against the purchased, controlled copies of IEC 62304:2006+A1:2015 and IEC 82304-1:2016, and the verification MUST be recorded with a date and a named reviewer. A conformance table whose clause numbers were never checked against the standard is worse than no table.

**MOS-CONF-102** — MedicalOS MUST NOT state, imply or permit a third party to imply that the platform is "IEC 62304 compliant", "62304 certified", "developed under IEC 62304", or "62304-ready". `MOS-SAFE-008` already bans the adjacent vocabulary and requires CI to enforce it; that ban MUST be extended to the strings `62304`, `82304`, `13485` and `14971` wherever they appear adjacent to a compliance verb in the repository, the UI, the API or marketing material. The permitted form of words is factual: "MedicalOS publishes the SOUP evidence set of §18.4.4."

**MOS-CONF-103** — The platform's obligation under R1 is **not** to conform to 62304. It is to be *documentable* by someone who must. Concretely: a publisher must be able to complete 62304 §5.3.3, §5.3.4, §7.1.2 c), §7.1.3 and §8.1.2 for MedicalOS from published artifacts alone, without a support contract, without reading the source and without negotiating an NDA. §18.4.4 makes that an enumerated deliverable.

**MOS-CONF-104** — R2 carries a burden the specification has not yet acknowledged. A publisher who cites a MedicalOS-produced `ValidationReport`, conformance report or provenance record as verification evidence for their own device is relying on a tool, and 62304 §5.1.4 requires them to justify that reliance. The platform MUST therefore publish, per release, the version and digest of every evidence-producing component (`medicalos-conformance`, the evidence runner, `medicalos-reproduce`), the checks each performs, and their known limitations, so a publisher can write a tool-qualification rationale instead of an assertion. `MOS-TEST-048` already signs the conformance report and binds it to an image digest; what is missing is a statement of what the report does *not* establish.

#### 18.4.2. Clause 4 — general requirements

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 4.1 Quality management system | Device software is developed under a QMS (ISO 13485 in practice). | PUBLISHER | — the platform supplies nothing and claims nothing | NOT-APPLICABLE to platform; GAP for any publisher without a QMS. The platform MUST NOT be read as substituting for one. |
| 4.2 Risk management (ISO 14971) | A risk management process covering the software's contribution to hazards. | PUBLISHER (device) / PLATFORM (its own, if R3) | `MOS-SAFE-014` `known_failure_modes[]` with `detection`/`mitigation`; `MOS-SAFE-019` IMDRF risk classification; `MOS-SAFE-073` DENY table; `MOS-EVID-137` monitoring signals | PARTIAL. The platform carries publisher-declared hazard *metadata* and enforces a default-deny posture, but has **no risk file of its own** — no hazard list, no severity/probability estimates, no risk control traceability. `MOS-OPEN-025` records this as undecided. |
| 4.3 Software safety classification (A/B/C) | The manufacturer assigns a class to the software system and to each software item, and documents the rationale. | PUBLISHER | §18.4.2.1 below | GAP for the platform: **no MedicalOS component carries a software safety class anywhere in this specification**, and `MOS-TEST-013`'s `safety_critical` list is the only criticality partition that exists. |
| 4.4 Legacy software | Software already in use before the lifecycle process was applied MAY be brought under 62304 through a documented, risk-based justification plus a gap-closure plan, instead of reconstructing a design history. | PLATFORM | nothing today | GAP — and see `MOS-CONF-113`, which argues this is the *correct* route for MedicalOS rather than an embarrassment. |

##### 18.4.2.1. Software safety classification, concretely

62304 §4.3 classifies on **severity of possible harm**, assuming no risk control measures external to the software system:

- **Class A** — no injury or damage to health is possible.
- **Class B** — non-serious injury is possible.
- **Class C** — death or serious injury is possible.

**MOS-CONF-105** — The platform MUST NOT compute, infer, default or validate a software safety class for a `ServiceVersion`. It is a manufacturer determination, exactly as `intended_use` and `regulatory_status` are (`MOS-SAFE-009`). `MOS-SAFE-019`'s IMDRF category check is a *structural consistency* check between two declared axes and is explicitly "not a regulatory determination"; it MUST NOT be presented, in a UI string, an export or a sales conversation, as a 62304 class.

Three worked determinations, stated as the platform's *expectation* of what a competent publisher will conclude. They are not determinations by the platform.

| Capability | Hazardous situation | Severity with no external control | Plausible class | Why |
|---|---|---|---|---|
| `lung_nodule` (spine §14, 0.3.0, LIDC-IDRI) | A malignant nodule is not marked; the reader anchors on the absent mark; diagnosis is delayed by one or more surveillance intervals. | Death or serious injury | **C**, arguable down to **B** | A missed early-stage lung cancer is a death pathway. The only thing that gets a publisher to B is an *external* risk control: `intended_use.reading_paradigm = second_reader`, where the radiologist completes an unaided read first, so the AI can only add marks and the miss pathway is the unaided baseline. At `concurrent_read` automation bias is live and the B argument needs human-factors evidence. At `triage` the AI reorders the worklist and a false negative directly delays the read — that is C with no argument. |
| `pleural_effusion` volume (spine §14, 0.1.0) | An over-reported effusion volume contributes to an unnecessary thoracentesis (pneumothorax, haemorrhage); an under-reported volume contributes to a delayed drainage in a decompensating patient. | Serious injury | **B**, conservatively **C** | Under `MOS-SAFE-019` this is IMDRF `drive` × `serious` = category III. B is defensible only because the clinician always sees the images the measurement was computed from, which is an external control the publisher must state and the site must actually operate. |
| `emphysema_laa` (spine §14, 0.1.0) | A wrong %LAA-950 contributes to a wrong COPD phenotype, or to a wrong lung-volume-reduction candidacy assessment. | Non-serious injury, arguably serious | **B** | A is only available if *no* injury is possible, which is hard to sustain once the number enters a candidacy discussion. |

**MOS-CONF-106** — Determinism MUST NOT be presented as lowering a software safety class. `emphysema_laa` is a deterministic measurement and not a learned model (spine §14, `MOS-REL-041`'s weight-free native worker notwithstanding), and `MOS-SVC-020` requires `operating_points` to be empty when `deterministic: true`. Determinism reduces output *variance*; 62304 classifies on *severity*. A deterministic function of the wrong input volume is wrong deterministically. The specification's own evidence for this is `MOS-IMG-054`'s golden-fixture self-test, which exists precisely because a byte-identical container can compute a byte-different tensor.

**MOS-CONF-107** — The external risk control that the whole Class-B argument rests on — the reading paradigm — is **declarative in MedicalOS and not enforced**. `intended_use.reading_paradigm` is a required, signed, digest-covered manifest field (`MOS-SAFE-014`, `MOS-SAFE-021`) and is displayed (`MOS-SAFE-012`), but no platform control makes a `second_reader` service's output unavailable until an unaided read is recorded: `MOS-SAFE-057` fixes Interpretation A ("results are stored and visible; nothing is auto-actioned"), `MOS-SAFE-061` **refuses** `review_mode: mandatory_pre_publication` in 0.1–0.4, and `MOS-SAFE-062` makes `Result.review_status` a read-only derived projection. A publisher classifying at B on a reading-paradigm argument MUST therefore record that control as a **use-related assumption placed on the deploying site**, not as a software risk control, and the site MUST accept it. This is a real, load-bearing seam and the platform MUST say so in `docs/services/AUTHORING.md` (`MOS-REL-095`).

**MOS-CONF-108** — Because MedicalOS writes every DICOM object (`MOS-SAFE-073` row 4; services MUST NOT write DICOM, spine §2), a publisher **MUST NOT** assign MedicalOS a lower software safety class than the software item that uses it, and MUST NOT treat it as a non-contributing component. The platform can corrupt a correct model output: wrong series selected, inconsistent UID remapping, an inverse geometry transform applied to the wrong grid, model-space `PixelMeasures` written into a SEG. `MOS-TEST-013`'s `safety_critical` list names exactly these modules — `imaging/geometry`, `imaging/uid`, `imaging/dicom_writer`, `dataplane/deidentify`, `dataplane/triage`, `registry/resolve` — and the platform has built its highest-rigour verification around them. That list is the honest starting point for a publisher's 7.1.1 item identification, and §18.4.4 turns it into a deliverable.

**MOS-CONF-109** — 62304 §4.3 permits a lower class for an item only where segregation from higher-class items is documented and effective. MedicalOS supplies unusually strong segregation evidence and the platform MUST keep supplying it: `MOS-REL-107`/`MOS-REL-108` make a third-party extension an OCI image executed **out-of-process** with no in-process plugin API; `MOS-SVC-031`/`MOS-SVC-029` digest-pin and signature-verify the image; `MOS-TEST-047` step K9 asserts the service completes under a `deny-all` network policy with any attempt to reach the PACS, database, broker or object store recorded and failing the step; `MOS-SEC-159` denies the dataset-export consumer class every network route to the PACS. This is a segregation argument a reviewer can execute rather than read.

##### 18.4.2.2. Class implications for required documentation

**MOS-CONF-110** — The documentation a publisher owes scales with the class, and the platform MUST NOT let a publisher believe the platform reduces it.

| If the service is… | The publisher additionally owes, beyond Class A | What the platform supplies |
|---|---|---|
| **Class A** | Development plan, software requirements, configuration management, problem resolution, maintenance, release records — and **SOUP identification (§8.1.2) even at Class A**. | `MOS-REL-001` release identity; §18.4.4 SOUP data sheet |
| **Class B** | + software architectural design incl. the SOUP clauses §5.3.3/§5.3.4, unit implementation and verification, integration and integration testing, system testing, documented and evaluated known residual anomalies at release | the interface contracts of ch 2/3/4; `MOS-TEST-047` conformance kit; the evidence plane of ch 7 |
| **Class C** | + detailed design down to the software unit and its interfaces, unit-level acceptance criteria, and the strictest configuration control and defect-avoidance planning | nothing — this is entirely inside the publisher's code base |

**MOS-CONF-111** — The per-sub-clause class applicability table (which clauses are B-and-C-only, which are C-only) MUST be transcribed from the controlled copy of the standard under `MOS-CONF-101` before §18.4 is used in a submission. The three-row summary above is a planning aid, not a conformance claim.

##### 18.4.2.3. Clause 4.4 and why it is the platform's best route

**MOS-CONF-112** — MedicalOS has **no defined software development lifecycle**. What it has instead is: a change-class matrix with per-class merge gates (`MOS-REL-116`, `MOS-REL-117`, enforced by CI and a PR template under `MOS-REL-118`, un-relabellable under `MOS-REL-119`, un-silently-disableable under `MOS-REL-120`); a requirement-ID scheme (`MOS-CORE-020`) with total requirement→check traceability (`MOS-TEST-002`) and an orphan-ID job (`MOS-REL-098`); a seven-level test pyramid (`MOS-TEST-008`) with named things that may not be faked (`MOS-TEST-009`); a release definition that is a single-commit property (`MOS-REL-123`, `MOS-REL-124`) with a Release Decision Record naming a human approver (`MOS-REL-003`). That is a great deal of 62304's *output* with none of its *process*: no software development plan, no design reviews, no recorded verification of the architecture, no detailed design, no software maintenance plan, no problem resolution process.

**MOS-CONF-113** — The project SHOULD therefore treat **clause 4.4 (legacy software) as its intended route into 62304**, not as an admission of failure, and SHOULD say so when the R3 question in `MOS-OPEN-025` is decided. The alternative — reconstructing a design history for work already done — is named in `MOS-OPEN-025` itself as "the most common reason small manufacturers stall". Clause 4.4 asks for a risk-based justification of continued use plus a gap-closure plan, and the artifacts this specification already produces (2,008 identified requirements, total traceability to executable checks, immutable signed artifacts, per-result provenance) are an unusually strong evidence base for exactly that justification. What is missing is the risk file and the plan, not the evidence.

**MOS-CONF-114** — `MOS-REL-050`'s spike rule is a live 4.4 hazard and MUST be treated as one. Weeks 0 and 1–2 are built as time-boxed spikes with "deliberately wrong architecture", required to be "deleted or re-implemented behind a named interface at the end of its time box". Any spike code that survives into 0.1.0 without that re-implementation is legacy software in the 62304 sense from the day the product ships. CI MUST be able to answer, per release, which components trace to a spike and which were re-implemented; the Release Decision Record (`MOS-REL-003`) is the right place to carry the answer.

#### 18.4.3. Clause 5 — software development process

Owner column reads: PUBLISHER owns the obligation for their device software; PLATFORM rows describe what MedicalOS would owe under R3 and, more importantly, what it supplies as evidence under R1/R2.

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 5.1 Software development planning | A plan covering process, deliverables, traceability, verification, configuration management, tools, standards and methods; kept current. | PUBLISHER; PLATFORM under R3 | `MOS-REL-116`/`MOS-REL-117` change classes and gate matrix; `MOS-REL-003` Release Decision Record; `MOS-TEST-002` traceability; `MOS-REL-027` build-vs-adopt register; `MOS-REL-095` documentation set | **PARTIAL** — the gates, the traceability discipline and the documentation set exist and are CI-enforced, but no document is a software development plan, no lifecycle model is named, and tool/method planning (5.1.4) is absent. |
| 5.2 Software requirements analysis | Requirements derived from system requirements, including functional, interface, security and risk-control requirements; verified; traceable. | PUBLISHER; PLATFORM under R3 | this specification: `MOS-CORE-020` ID grammar, `MOS-CORE-006`/`MOS-CORE-007` single-definition-site and cross-reference rules, `MOS-TEST-002` every ID mapped to ≥1 executable check, `MOS-REL-081` restatement at the point of enforcement, `MOS-REL-098` no orphan IDs | **SATISFIED** for the platform, and materially stronger than most certified products. This is the spec's best clause. |
| 5.3 Software architectural design | Architecture that realises the requirements; interfaces between items; interfaces to SOUP; segregation for risk control; verified. | PUBLISHER; PLATFORM under R3 | ch 2 (planes, ownership boundary, service contract), ch 12 (data model), ch 13 (process inventory, deployment); `MOS-REL-039` one deployable per trust boundary; `MOS-REL-048` explicit interfaces with ≥2 implementations; `MOS-REL-107`/`MOS-REL-108` out-of-process extension boundary | **PARTIAL** — architecture is documented to an unusual depth, but 5.3.6 (verify the architecture against the requirements, as a recorded activity with an output) has no counterpart. `MOS-REL-117` requires an ADR "if it changes a register row or a port" — that is change control, not architecture verification. |
| 5.3.3 / 5.3.4 SOUP | Specify the functional and performance requirements of each SOUP item, and the hardware/software it needs to operate correctly. | PUBLISHER (owes it); PLATFORM (must make it possible) | §18.4.4 | **PARTIAL** — see §18.4.4. |
| 5.3.5 Segregation | Segregation necessary for risk control is specified and its effectiveness shown. | PUBLISHER | `MOS-CONF-109` evidence set | **SATISFIED** as evidence; the *claim* is the publisher's. |
| 5.4 Software detailed design | Refine items into units; detailed design for each unit and each interface; verified. Class C only for 5.4.2–5.4.4. | PUBLISHER; PLATFORM under R3 | nothing | **GAP** for the platform. No unit-level design exists or is required anywhere in chapters 1–17. |
| 5.5 Unit implementation and verification | Implement each unit; establish a unit verification process with acceptance criteria; verify. | PUBLISHER; PLATFORM under R3 | `MOS-TEST-008` L0/L1 with a ≥3000-case floor; `MOS-REL-049` tests land with the code at the change class's depth; `MOS-TEST-013` mutation score ≥ 0.80 on the `safety_critical` list; `MOS-TEST-014` property-based geometry invariants; `MOS-TEST-011` seeded determinism | **PARTIAL** — the *verification* is strong and quantified; there is no declared set of unit acceptance criteria, and no record that a unit was verified against a design it does not have. |
| 5.6 Software integration and integration testing | Integrate per plan; test integration; test content and records; regression testing on change; anomalies into problem resolution. | PUBLISHER; PLATFORM under R3 | `MOS-TEST-008` L2/L4; `MOS-TEST-009` no-fakes list; `MOS-TEST-051` named committed fault points (exhaustive registry) with `MOS-TEST-052` proving they are absent from release images; `MOS-TEST-053` mandatory L4 failure matrix; `MOS-REL-073` every clinical-path change adds a fixture-corpus case | **SATISFIED** except 5.6.8 — anomalies have nowhere to go, because there is no problem resolution process (§18.4.8). |
| 5.7 Software system testing | Test against the software requirements; re-test after change; evaluate; record. | PUBLISHER; PLATFORM under R3 | `MOS-TEST-008` L5 (AT-01…AT-24 on the full stack); `MOS-REL-012` a requirement a release claims MUST have its acceptance criterion executed in that release's CI run; `MOS-REL-004` no tag on a red gate | **SATISFIED**, with the same 5.7.2 caveat: anomalies have no process. |
| 5.8 Software release | Verification complete and evaluated; **known residual anomalies documented and evaluated for acceptability**; released version documented; how it was created documented; archived; release repeatable. | PUBLISHER; PLATFORM under R3 | `MOS-REL-001` signed tag + digest-referenced images + SBOM + cosign signature; `MOS-REL-003` Release Decision Record with per-check gate results and a named approver; `MOS-REL-123`/`MOS-REL-124` Done is a single-commit property; `MOS-SEC-140` digest pinning everywhere; `MOS-REL-060` air-gapped installability | **PARTIAL — and the anomaly half is a hard GAP.** See `MOS-CONF-115`–`MOS-CONF-117`. |

**MOS-CONF-115** — **Known residual anomalies.** 62304's release clause requires the manufacturer to document the anomalies remaining in the released software and to evaluate each one to confirm it does not produce unacceptable risk. MedicalOS publishes **no anomaly list**. `CHANGELOG.md` (`MOS-REL-095`) records what changed, not what is still wrong. `MIGRATIONS.md` records breaking changes. The Release Decision Record (`MOS-REL-003`) records gate results and cut Tier B/C items — the closest artifact, and still not an anomaly list. `docs/spec/99-known-inconsistencies.md` is explicitly non-normative and is a register of defects **in this specification**, not in the software. The platform MUST publish `docs/KNOWN_ANOMALIES.md` per release: one row per open defect with an id, the affected component, observable symptom, whether a clinical path is reachable, the workaround, and the release in which a fix is planned. Without it, **every** publisher is blocked on both the release clause and §7.1.3 (below), and the honest sentence they must write in their risk file is "the supplier publishes no anomaly list" — which a notified body will pursue.

**MOS-CONF-116** — **How the release was created, and archiving.** The platform can reproduce a *clinical run* — `MOS-SAFE-092`'s `medicalos-reproduce` reconstructs an inference from the provenance record alone and asserts against `execution.reproducibility_class`, and `MOS-SAFE-093` refuses the clinical gate to a `not_reproducible` version. It cannot reproduce a *build*. Nothing in chapters 1–17 records the build environment (toolchain digests, base image digests, build flags), requires a reproducible build, or requires source, tools and build environment to be archived for the supported life of a release. `MOS-REL-095`'s `DEVELOPMENT.md` names toolchain versions for humans, which is not an archival record. The platform MUST record the build environment on every release and MUST archive source, generated artifacts and the pinned toolchain alongside the signed tag.

**MOS-CONF-117** — **Verification of the SBOM's role.** `MOS-SEC-142` requires a CycloneDX or SPDX SBOM on every image and every model artifact, retrievable through the API for any deployed version; `MOS-REG-087` attaches it via the OCI Referrers API; `MOS-REG-094` makes SBOM verification a mandatory install step; `MOS-REL-038` requires a recorded licence per dependency with CI failing on an absent or deny-listed one; `MOS-REL-059` defines "documented dependency" as a register row plus a licence entry plus an SBOM entry. This is a genuine third-party-component inventory and it satisfies the *mechanical* half of §8.1.2 for MedicalOS's own SOUP. It does **not** satisfy the analytical half: an SBOM enumerates components, it does not record the functional and performance requirements placed on them (§5.3.3) or the evaluation of their anomaly lists (§7.1.3). The platform MUST NOT present its SBOM as a SOUP analysis.

#### 18.4.4. The SOUP clauses, in depth — 5.3.3, 5.3.4, 7.1.2, 7.1.3, 8.1.2

These five clauses are the ones MedicalOS is on the *receiving* end of. A publisher cannot close them by inspecting the platform; they can only close them from what the platform publishes. This subsection is the one place in chapter 18 that creates a real engineering deliverable, and it is deliberately small.

**MOS-CONF-118** — The platform MUST publish, per release, a **SOUP data sheet** at `docs/SOUP.md`, generated where possible and versioned with the release. Its required contents are the table below. It MUST be retrievable without authentication and MUST be included in the release artifacts (`MOS-REL-001`), because a publisher assembling a technical file eighteen months after deployment cannot depend on a running installation.

| 62304 clause | What the publisher must record | What the SOUP data sheet MUST carry | Backing requirement today | Status |
|---|---|---|---|---|
| **8.1.2 a) title** | The SOUP item's title | `MedicalOS`, the spelling fixed by `MOS-CORE-001`, with the expansion ban restated | `MOS-CORE-001` | **SATISFIED** |
| **8.1.2 b) manufacturer** | The SOUP item's supplier | A named legal or project entity, its contact address, and the signing identity under which releases are published | **nothing** — `legal_manufacturer` (`MOS-SAFE-020`) exists only on a `ServiceVersion`; the platform signs with "the platform key" (`MOS-SEC-138` table, `MOS-SEC-152`) but declares no supplier identity | **GAP** — narrow and cheap. A publisher literally cannot fill field (b). |
| **8.1.2 c) unique designator** | Version, release date, patch level — enough to identify the exact bytes | `v<MAJOR>.<MINOR>.<PATCH>` signed annotated tag, the OCI image digest of every first-party container, SBOM digest, tag date | `MOS-REL-001`; `MOS-REL-002` pre-1.0 version semantics; `MOS-SEC-140` digest pinning everywhere; and — uniquely strong — `MOS-SAFE-083` section D writes `execution.runtime = {platform_version, worker_version, sdk_version, container_image_digests[]}` into the provenance record of **every single result** | **SATISFIED**, and better than the clause asks: the publisher can name the exact SOUP version that produced any individual patient's object, offline, from `MOS-SAFE-089`'s signed export. |
| **5.3.3 functional and performance requirements of the SOUP item** | What the manufacturer *needs* MedicalOS to do, and how well, for their intended use | The platform's declared functional contract and its numeric tolerances, assembled in one place addressed to a publisher | `MOS-SVC-*` service contract (ch 2); `MOS-IMG-*` geometry contract (ch 4) with `MOS-IMG-054`'s golden-fixture tensor-hash self-test; `MOS-REL-095`'s `docs/dicom/CONFORMANCE.md` and `docs/services/AUTHORING.md`; the 0.1.0 `dicom-battery` gate numbers (`MOS-REL-003` row): dciodvfy zero errors, SEG read-back Dice 1.0, origin/spacing/direction within 1e-4 of source, `FrameOfReferenceUID` equality, QIDO-RS instance count exact | **PARTIAL** — the numbers exist and are CI-enforced, but they live in a release gate cell and across four chapters. No single document states "these are the guarantees MedicalOS gives a publisher, and these are the tolerances". |
| **5.3.4 system hardware and software the SOUP item needs** | The IT environment MedicalOS itself requires to operate correctly | Minimum and tested versions: OS, container runtime, orchestrator, PostgreSQL, object store, GPU driver/CUDA, the DICOMweb server, and the `(backend, gpu_architecture, runtime_version)` triple | `MOS-REL-095`'s `DEPLOYMENT.md` with a generated configuration reference; ch 13 process inventory and GPU residency contract; `MOS-SVC-016`/`compute_capability_min`; `MOS-REL-037` compatibility against a port version or observable capability, never a product version; `MOS-REL-060` air-gapped installability | **PARTIAL** — `MOS-REL-037` deliberately refuses to express compatibility as third-party product versions, which is right for the resolver and wrong for this clause: a publisher's 5.3.4 record needs exactly the concrete platform matrix that `MOS-REL-037` declines to state. The two MUST be reconciled by publishing a **tested-configuration matrix** in the SOUP data sheet, distinct from and not consulted by the resolver. |
| **7.1.2 c) failure or unexpected results from SOUP** | Every way MedicalOS failing could contribute to a hazardous situation in the publisher's device | The platform's own list of hazard contributions, with the detection mechanism for each | Closed enums a publisher can enumerate against: `MOS-SVC-097` transport/system failure codes (closed at document level), `MOS-SVC-095` clinical rejection reason codes, ch 5 §5.3.2 closed `failure_class`; `MOS-SAFE-073` DENY table with the enforcing mechanism per row; `MOS-TEST-051` exhaustive fault-point registry; `MOS-TEST-053` mandatory L4 failure matrix; `MOS-SAFE-084` rejected series recorded with reasons | **PARTIAL, and the most valuable gap to close.** The raw material is exceptional — closed failure vocabularies are rare and are precisely what a hazard analysis needs. What does not exist is a platform-authored statement of *what the platform can get wrong in a way that reaches a patient*: wrong series analysed, inconsistent UID remapping silently invalidating result references (spine §8), an inverse geometry transform applied to the wrong grid, model-space `PixelMeasures` written into a SEG, a stale `clinical_use_mode`. These hazards are discussed as prose across chapters 3, 4 and 9; they are not assembled. |
| **7.1.3 evaluate published SOUP anomaly lists** | Read the supplier's known-anomaly list and determine whether any anomaly opens a hazard sequence | A per-release anomaly list the publisher can actually read | **nothing** | **GAP.** See `MOS-CONF-115`. This clause is unclosable by any publisher today. |

**MOS-CONF-119** — The SOUP data sheet MUST carry the **hazard-contribution list** demanded by 7.1.2 c), and its first draft MUST be derived from `MOS-TEST-013`'s `safety_critical` module list — `imaging/geometry`, `imaging/uid`, `imaging/dicom_writer`, `dataplane/deidentify`, `dataplane/triage`, `registry/resolve`, `security/rls`, `safety/content_postconditions` — because that list already encodes the project's own judgement about where a defect reaches a patient. Each entry MUST state: the failure mode, the clinical consequence, whether it is detectable by the platform, which requirement detects it, and what a publisher or site must do about it. Where the platform *does* detect it, the citation is often already there: `MOS-IMG-054` golden-fixture self-test with refusal to serve on mismatch; `MOS-SVC-099` `input_pixel_digest` divergence failing the job in `clinical` mode; `MOS-SAFE-091` daily provenance hash-chain verification; `MOS-SEC-153` audit chain verification reporting the first divergent sequence number; `MOS-EVID-110`/`MOS-EVID-111` output plausibility as a service defect rather than a clinical rejection.

**MOS-CONF-120** — The SOUP data sheet MUST state plainly that MedicalOS is on the clinical output path, not beside it, and MUST reproduce the `MOS-SAFE-004` responsibility table. A publisher who treats the platform as inert transport has mis-scoped their risk file.

**MOS-CONF-121** — `MOS-SAFE-014`'s `known_limitations[]`, `not_validated_for[]` and `known_failure_modes[]` are the **publisher's** anomaly and limitation declarations, and the platform makes them fail-closed: each MUST carry at least one entry, an empty list is a registration error and not an empty set (`MOS-SAFE-015`), ids MUST be unique and stable across versions because provenance references them (`MOS-SAFE-017`), and `GET /api/v1/service-versions/{id}/clinical` returns the block verbatim with its digest (`MOS-SAFE-022`). This is a genuinely good mechanism and it is the pattern the platform MUST copy for itself under `MOS-CONF-115`: the platform demands of every publisher exactly the artifact it does not produce.

**MOS-CONF-122** — `ResultReview.known_failure_mode_id` (`MOS-SAFE-058`) lets a reviewer attribute a rejection to a declared failure mode, and `MOS-EVID-137`'s `review_disagreement_rate` aggregates the outcome over 30-day windows. Together these are the closest thing in the specification to a post-market anomaly feedback loop, and they run **site-side, about the service**. The platform MUST NOT present them as its own post-market surveillance (§18.4.9).

#### 18.4.5. Clause 6 — software maintenance process

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 6.1 Establish a maintenance plan | A documented plan for receiving, evaluating and acting on feedback; how modifications are implemented and released. | PUBLISHER; PLATFORM under R3 | **nothing** | **GAP.** No maintenance plan, no support-period statement, no LTS or backport policy anywhere in chapters 1–17. `MOS-REL-095`'s `SECURITY.md` is required to carry "supported versions", which is the only trace of the concept and covers security only. |
| 6.2.1 Monitor, document and evaluate feedback | Feedback from users and from post-production is collected and evaluated for safety. | SITE → PUBLISHER → PLATFORM | `MOS-EVID-135`–`MOS-EVID-142` continuous site monitoring with signed 30-day period reports; `MOS-OPS-054` alerts routed by owner, with `REJECTED` rises going to the deployment owner and `FAILED` rises to on-call; `MOS-OPS-055` every alert carries a runbook or a named remedy | **PARTIAL** — the *signals* are excellent and quantified. There is no channel by which a site's or publisher's observation reaches the MedicalOS project, and no obligation to evaluate one when it arrives. |
| 6.2.1.3 Evaluate a problem report's effect on safety | Every problem report is assessed for safety impact. | PLATFORM (for platform defects) | **nothing** | **GAP** — depends on §18.4.8, which does not exist. |
| 6.2.2 Use the problem resolution process | Problem reports go into clause 9's process. | PLATFORM | **nothing** | **GAP** |
| 6.2.3–6.2.4 Analyse and approve change requests | Change requests are analysed for effect on safety and approved. | PLATFORM | `MOS-REL-116`/`MOS-REL-117` — C3 is defined as "anything that can alter a stored `Result`, a generated DICOM object, a measurement, a triage or selection decision, a preprocessing step, a policy decision, or a `clinical_use_mode` behaviour", gated on evidence re-run, DICOM battery, fixture case and a **named human approver**; `MOS-REL-119` forbids relabelling to a lighter class | **SATISFIED** — this is a real change-control process and the C3 definition is, in substance, a safety-impact classification. It is arriving from the wrong direction (merge gate rather than change request) but it does the work. |
| 6.2.5 Communicate to users and regulators | Users, and where required regulators, are informed of problems and of changes affecting safety. | PLATFORM → SITE/PUBLISHER | `MOS-REL-095` `CHANGELOG.md` and `MIGRATIONS.md`; `MOS-SAFE-074` requires a release-notes entry for any change to a hard DENY row; `MOS-REL-009(c)` requires a pre-release's notes to name every failing check | **PARTIAL** — release communication exists; there is no advisory or field-notice channel for a safety-relevant defect discovered between releases, and no obligation to notify sites running an affected version even though `MOS-REG-040`'s impact query proves the platform can enumerate exactly who is affected. |
| 6.3 Modification implementation | Modifications are implemented through the same development process and the software is re-released. | PLATFORM | `MOS-REL-117` gate matrix; `MOS-REL-055` numbered, reversible-or-declared-irreversible migrations; `MOS-REL-123` release Done definition | **PARTIAL** — "the same development process" is undefined because there is no defined process (`MOS-CONF-112`). |

**MOS-CONF-123** — The platform MUST publish a maintenance plan at `docs/MAINTENANCE.md` stating: the supported-version window and its length, what a patch release may and may not contain (`MOS-REL-002` already fixes that a PATCH MUST NOT contain a breaking change), the backport policy for a safety-relevant defect, the notification channel by which a site or publisher reports a suspected platform defect, and the platform's committed response time by severity. `MOS-SEC-143` already publishes exactly such a budget for security vulnerabilities — 7 days for a reachable critical, 30 for a reachable high, 90 for medium, with a recorded reachability analysis where not reachable — and `MOS-SEC-144` requires daily rescanning of already-published digests. That table is the template; the gap is that **safety defects have no equivalent**, which is the wrong way round for medical software.

**MOS-CONF-124** — The platform MUST define a **safety advisory** artifact and channel, analogous to a security advisory: an identifier, the affected version range, the observable symptom, whether a clinical path is reachable, the required site action, and the fixed version. It MUST be emitted as a webhook on the existing event envelope (`MOS-API-068` requires exactly one `event_type` registry across SSE, webhooks and the bus; `MOS-API-069` fixes one envelope with no transport-specific variant), so the mechanism costs a registry entry rather than a subsystem.

**MOS-CONF-125** — When a platform defect is found to have affected produced results, the platform MUST be able to enumerate them. It already can: `MOS-REG-040` requires `GET /api/v1/service-versions/{id}/impact` and `.../model-versions/{id}/impact` to return every `job_id`, `result_id`, generated `SeriesInstanceUID` and `tenant_id`, computed from the pinned `job.resolution` records and the provenance record, and states that "a recall state with no impact query is not a recall". The gap is that this is scoped to an *artifact* recall, not to a *platform version*. Because `MOS-SAFE-083` section D already pins `execution.runtime.platform_version` and `container_image_digests[]` on every result, the platform-version impact query is a query away and MUST be provided.

#### 18.4.6. Clause 7 — software risk management process

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 7.1.1 Identify software items that could contribute to a hazardous situation | Name the items. | PUBLISHER (their items); PLATFORM (supplies its own) | `MOS-TEST-013` `safety_critical` list; `MOS-SAFE-073` DENY table naming subject, default and enforcing mechanism per row | **PARTIAL** — the list exists as a *test-rigour* partition, not as a hazard-contribution partition. `MOS-CONF-119` converts it. |
| 7.1.2 Identify potential causes, incl. **c) failure or unexpected results from SOUP** | Enumerate causes: incomplete specification, defects, SOUP failure, hardware failure, foreseeable misuse. | PUBLISHER | §18.4.4; plus `MOS-SAFE-014` `known_failure_modes[]` with a closed `detection` vocabulary (`applicability_envelope`, `output_plausibility_gate`, `human_review`, `none`) | **PARTIAL** — for the publisher's own code it is their work; for cause (c) they depend on a platform hazard list that does not yet exist. |
| 7.1.3 Evaluate published SOUP anomaly lists | Read the supplier's anomaly list; determine whether any anomaly opens a hazard sequence. | PUBLISHER | **nothing to read** | **GAP** — `MOS-CONF-115` is the fix and this is its strongest justification. |
| 7.1.4–7.1.5 Document causes and sequences of events | Record them. | PUBLISHER | — | NOT-APPLICABLE to platform |
| 7.2 Risk control measures | Define measures; where implemented in software, they become software requirements. | PUBLISHER; PLATFORM supplies platform-side controls | `MOS-SAFE-036` (E1) nine-condition clinical gate enumerating every failure; `MOS-SAFE-039` (E3) RUO marking check as one shared function before STOW with no disabling flag; `MOS-SAFE-040` (E4) destination gate; `MOS-SAFE-041` (E5) read-path filter; `MOS-SAFE-042` (E6) `VerificationFlag` forced UNVERIFIED; `MOS-DATA-067` applicability envelope; `MOS-EVID-110`/`MOS-EVID-111` output plausibility; `MOS-REL-071` policy path fail-closed with no circuit breaker, cache or allow-on-timeout | **SATISFIED** as a set of enforced controls. They are excellent and they are not *labelled* as risk control measures, so no traceability from a hazard to them exists. |
| 7.3 Verification of risk control measures | Verify each measure; document traceability from hazard → cause → measure → verification. | PUBLISHER; PLATFORM under R3 | `MOS-TEST-002` maps every requirement ID to ≥1 executable check; `MOS-SAFE-045` exposes the E1 gate state live per condition; ch 14 §14.6.5 fail-closed tests | **PARTIAL** — the requirement→verification half of the chain is complete and CI-enforced. The hazard→requirement half does not exist, because there is no hazard register. This is the single highest-leverage gap in the whole of §18.4: the expensive half of 7.3 traceability is already built. |
| 7.4 Risk management of software changes | Analyse each change for safety impact and for impact on existing risk control measures. | PLATFORM | `MOS-REL-116`–`MOS-REL-119` change classes, with C3 defined by what the change can break and gated on evidence re-run, DICOM battery, a new fixture case, security review and a named approver; `MOS-REL-073` regression memory | **SATISFIED in substance.** The C3 definition is a safety-impact analysis in all but name. What is missing is the output artifact: a recorded analysis, rather than a passed gate. |

**MOS-CONF-126** — The platform SHOULD establish a hazard register under ISO 14971 covering the platform's own contribution to hazardous situations, and SHOULD bind each entry to the requirement IDs that control it. The cost is low and the return is disproportionate, because `MOS-TEST-002` already guarantees every requirement ID is mapped to an executable check: adding a hazard→requirement edge completes a hazard→requirement→check→CI-result chain end to end. `MOS-OPEN-025` names "a risk file under ISO 14971" as part of the undecided R3 package; this requirement records that the marginal cost of the risk file is far lower here than the open question assumes.

**MOS-CONF-127** — The platform MUST NOT describe the E1–E8 controls of `MOS-SAFE-035`–`MOS-SAFE-046` as "risk control measures" in a 62304 or 14971 sense until `MOS-CONF-126` is done. Until a hazard register names what they control, they are engineering gates, and calling them risk controls is the kind of borrowed vocabulary `MOS-SAFE-008` exists to prevent.

#### 18.4.7. Clause 8 — software configuration management process

This is the platform's strongest clause, and it is strong for reasons that are structural rather than procedural.

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 8.1.1 Configuration identification | Every configuration item is uniquely identifiable, including at the level the manufacturer chooses to control. | PLATFORM / PUBLISHER | `MOS-REL-001` signed tag + digest-referenced images; `MOS-REG-017` `content_digest` computed by the registry over RFC 8785 canonicalisation and never accepted from the publisher; `MOS-REG-018` `manifest`, `content_digest`, `version` and `oci_ref` immutable after insert, enforced by a database `REVOKE UPDATE` plus a trigger; `MOS-SVC-010` a `(service_id, version)` pair registrable exactly once; `MOS-SVC-027` `manifest_digest` computed by the platform, not supplied by the vendor | **SATISFIED**, and enforced at the database role level rather than by convention. |
| 8.1.2 Identification of SOUP | Title, manufacturer, unique designator, for every SOUP configuration item. | PUBLISHER (records it); PLATFORM (must be identifiable) | See the §18.4.4 table. Title `MOS-CORE-001` SATISFIED; designator `MOS-REL-001` + `MOS-SAFE-083` SATISFIED and exceptional; **manufacturer field GAP**. For MedicalOS's own SOUP: `MOS-SEC-142` SBOM, `MOS-REL-038` licence per dependency, `MOS-REL-059` register row + licence + SBOM entry | **PARTIAL** — one missing field (supplier identity) blocks an otherwise complete clause. |
| 8.1.3 Identification of system configuration documentation | The configuration of the released system is documented. | PLATFORM | `MOS-REL-003` Release Decision Record; `MOS-REL-095`'s `DEPLOYMENT.md` with a generated configuration reference and a drift check; `MOS-SEC-145` the `Deployment` row records the digests verified at creation | **SATISFIED** |
| 8.2.1–8.2.4 Change control: approve, implement, verify, trace | Changes are approved before implementation, verified, and traceable. | PLATFORM | `MOS-REL-116` mandatory change-class label with C3 as the default for an undeclared class; `MOS-REL-117` gate matrix; `MOS-REL-118` CI + PR-template enforcement, never reviewer memory; `MOS-REL-120` a failing gate is fixed or waived with an issue ID and a named owner; `MOS-REL-081` every constraint restated at its enforcement point as a comment and a test name containing the requirement ID | **SATISFIED in substance**, with one honest qualification: 62304 expects approval *before* implementation, and this is approval before *merge*. For a project of this size that is a defensible tailoring, and it MUST be stated as a tailoring rather than left as a silent difference. |
| 8.3 Configuration status accounting | Records show the status and history of each configuration item. | PLATFORM | `MOS-REG-020`/`MOS-REG-021` the `lifecycle_status` graph (`DRAFT`→`REGISTERED`→`VALIDATING`→`VALIDATED`→`APPROVED`→`DEPRECATED`/`SUSPENDED`/`RECALLED`) with permissioned transitions; `MOS-REG-022` `RECALLED` irreversible, a fixed defect becomes a new version; `MOS-REG-106` a `DEPRECATED` version stays resolvable ≥180 days so pinned jobs can be replayed; `MOS-REG-040` the impact query; `MOS-SEC-151`–`MOS-SEC-153` hash-chained, append-only, checkpointed, offline-verifiable audit | **SATISFIED**, and well beyond the clause. |

**MOS-CONF-128** — The platform MUST declare a supplier identity for MedicalOS itself — name, legal form where one exists, contact address, and the signing key identity under which releases are published — in `docs/SOUP.md` and in the Release Decision Record. The shape MUST reuse `MOS-SAFE-020`'s `legal_manufacturer` schema minus the regulatory fields, so a publisher can paste it into their configuration record. This MUST NOT be read as a device-manufacturer declaration: `MOS-SAFE-002` and `MOS-SAFE-010` remain binding, and the data sheet MUST say so in the same block.

**MOS-CONF-129** — Where the platform's configuration management exceeds what 62304 asks, §18.4 MUST say so rather than under-claim. Three items in particular are stronger than the clause and are worth naming to a reviewer: immutability enforced by a database grant and trigger rather than by policy (`MOS-REG-018`); a content digest the publisher is structurally unable to forge (`MOS-REG-017`, `MOS-SVC-027`); and per-result configuration recording, in which `MOS-SAFE-083` writes the full configuration of the producing system — service version, image digest, model artifact digests, `PreprocessingSpec` versions and digests, accelerator, platform and worker versions, container image digests — into an append-only, hash-chained, offline-verifiable record (`MOS-SAFE-089`, `MOS-SAFE-090`). Most certified products cannot answer "which exact configuration produced this patient's object" at all.

#### 18.4.8. Clause 9 — software problem resolution process

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 9.1 Prepare problem reports | Every problem is recorded as a problem report with type, scope and criticality. | PLATFORM | **nothing** | **GAP** |
| 9.2 Investigate the problem | Investigate, evaluate for safety impact, record the result or record why no action. | PLATFORM | `MOS-SEC-143` for security only; `MOS-OPS-041` names a non-zero log-redaction violation as "a defect, not an operating condition" requiring a ticket-severity alert and a fix at source | **GAP** for safety defects. |
| 9.3 Advise relevant parties | Inform users and, where required, regulators of problems that could affect safety. | PLATFORM → SITE/PUBLISHER | `MOS-SAFE-074` release-notes entry for a DENY-row change; `MOS-REL-009(c)` pre-release notes naming failing checks | **GAP** — `MOS-CONF-124` is the fix. |
| 9.4 Use the change control process | Approved fixes go through clause 8.2. | PLATFORM | `MOS-REL-116`–`MOS-REL-120` | **SATISFIED** — the fix half of the loop works; the report half does not exist. |
| 9.5 Maintain records | Problem reports and their resolutions are retained. | PLATFORM | `MOS-REL-095` `CHANGELOG.md`; `MOS-SEC-154` audit partition export with a permanently retained archive digest (applies to `AuditEvent`, not to defects) | **GAP** |
| 9.6 Analyse problems for trends | Look across problem reports for patterns. | PLATFORM | `MOS-EVID-137` computes trends over *clinical outputs* per 30-day window, not over defects | **GAP** |
| 9.7 Verify software problem resolution | Verify that the fix resolves the problem and introduces no new one. | PLATFORM | `MOS-REL-073` every clinical-path change adds a fixture-corpus case — "the corpus is the regression memory of every defect class the project has met"; `MOS-TEST-012` flake quarantine with an owner and ≤14-day expiry, and no quarantine at all for a `safety_critical` module | **PARTIAL** — the regression mechanism is genuinely good and is the right half to already have; it is triggered by a change, not by a problem report. |
| 9.8 Test documentation contents | Records state what was tested, by whom, with what result. | PLATFORM | `MOS-TEST-002`, `MOS-REL-012`, `MOS-REL-003` | **SATISFIED** |

**MOS-CONF-130** — MedicalOS has **no problem resolution process**. Every clause-9 obligation that begins with receiving a report is unmet, while every clause-9 obligation that begins with a change already being in flight is met and CI-enforced. The asymmetry is exact and it is worth stating plainly to a reviewer: the project has built the second half of the loop with unusual rigour and has not built the first half at all.

**MOS-CONF-131** — The platform MUST establish a problem resolution process before any site runs a `clinical_use_mode: clinical` Deployment, and it MUST include at minimum: an intake channel (`docs/MAINTENANCE.md` per `MOS-CONF-123`), a problem report record carrying type, affected version range, clinical-path reachability and criticality, a mandatory safety-impact evaluation with a recorded outcome including "no action, because…", the link into the existing change-class gates, a trend review at each release block boundary alongside the scope review that `MOS-REL-007` already requires, and the safety advisory of `MOS-CONF-124`. The process SHOULD be modelled directly on `MOS-SEC-143`'s vulnerability budget, which already demonstrates the shape the project is willing to commit to.

**MOS-CONF-132** — A problem report whose safety-impact evaluation finds a clinical path reachable MUST be assigned change class C3 by construction under `MOS-REL-116`, and MUST NOT be downgraded — `MOS-REL-119` already forbids relabelling to bypass a gate, and this requirement makes the intake side of that rule explicit.

**MOS-CONF-133** — Problem reports and their resolutions MUST be retained for at least the retention period of the results they could have affected. `MOS-SEC-154` retains `AuditEvent` for ≥6 years via signed archive with a permanently retained archive digest; the defect record MUST NOT be shorter-lived than the evidence that a defect mattered.

#### 18.4.9. IEC 82304-1 — health software products

IEC 82304-1:2016 governs **health software products** — software intended for health purposes that is placed on the market as a product in its own right — and, importantly, its scope is not limited to medical devices. It is therefore the standard MedicalOS core could most credibly conform to, and the one a hospital procurement officer is most likely to ask about. It invokes IEC 62304 for the development half rather than restating it, and adds three things 62304 does not cover: **product-level use and system requirements**, **product validation against those requirements** including usability, and **accompanying documents plus post-market activities** at the product level.

**MOS-CONF-134** — §18.4 MUST treat IEC 82304-1 as the platform-level frame and IEC 62304 as the development-process frame invoked by it. The two overlap at development and diverge at the product boundary; mapping the same evidence twice without saying which frame it is answering is what makes conformance tables unreadable.

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 4 — Use requirements: intended use, intended users, patient population, clinical/operational environment, contraindications | A stated intended use for the *product*. | PLATFORM (for MedicalOS) / PUBLISHER (for the service) | For the service: `MOS-SAFE-013`–`MOS-SAFE-022`, an exceptionally complete block — `intended_use.statement`, `intended_user`, `intended_setting`, `reading_paradigm`, `autonomy`, `output_kinds`, coded `indications[]`, `contraindications[]` (empty list is a positive assertion, not an omission), `target_population`, digest-sealed under `MOS-SAFE-021` and served verbatim by `MOS-SAFE-022`. For the platform: `MOS-SAFE-001`'s positioning statement | **PARTIAL for the platform.** `MOS-SAFE-001` is a *negative* statement ("does not diagnose, does not replace a PACS, is not a device"). It names no intended user, no operational environment, no patient population and no contraindication. `MOS-SAFE-002` makes the negative form mandatory; nothing supplies the positive form 82304-1 asks for. **SATISFIED for the service**, and the service-side block is a ready-made template for the platform's own. |
| 4 — System requirements: the IT environment, performance, security and privacy requirements placed on the operator | What the product needs from the host environment, and what the operator must provide. | PLATFORM states; SITE provides | ch 13 process inventory, configuration contract, health/readiness endpoints, deployment topologies; `MOS-REL-095`'s `DEPLOYMENT.md`; ch 8 in full — zone model, RLS, DICOM Gateway as sole PACS credential holder, `MOS-SEC-140` digest pinning, `MOS-SEC-141` keyed verification working with no internet access; `MOS-REL-060` air-gapped installability | **PARTIAL** — the same gap as 62304 §5.3.4: the material exists and is not assembled into a stated minimum/tested environment matrix. See `MOS-CONF-118`. |
| 5 — Design and development | Develop the health software per IEC 62304. | PLATFORM | §18.4.3 | **PARTIAL**, limited by `MOS-CONF-112`: no defined lifecycle. |
| 6 — Validation | Validate the product against the *use* requirements, not only verify against the software requirements; includes usability validation (IEC 62366-1). | PLATFORM | `MOS-TEST-008` L5 acceptance against the full stack (AT-01…AT-24); `MOS-REL-012`; `MOS-REL-008` gates stated in observable terms naming no product | **PARTIAL.** The verification is thorough. Validation against *use* requirements cannot be complete while the platform has no stated use requirements (row 1). **Usability engineering is a hard GAP:** the strings `62366` and `usability` do not occur anywhere in chapters 1–17. This matters clinically, not just formally — `MOS-SAFE-012` mandates what must be displayed adjacent to every AI-derived finding, and `MOS-EXEC-001`/spine §4 require `REJECTED` to be visually distinct from `FAILED` in every UI. Those are use-related safety requirements with no usability validation behind them. |
| 7.1 — Product identification | The product and its version are unambiguously identifiable, including in the field. | PLATFORM | `MOS-REL-001`; `MOS-SAFE-083` section D `execution.runtime`; `MOS-REG-018` immutability | **SATISFIED** |
| 7.2 — Accompanying documents | Instructions for use covering intended use, required IT environment, installation and configuration, the security measures the operator must apply, known limitations and residual risks, version identification, manufacturer contact, and decommissioning/data disposal. | PLATFORM writes; SITE reads and acts | `MOS-REL-095` mandates `README.md`, `ARCHITECTURE.md`, `DEPLOYMENT.md`, `SECURITY.md`, `docs/dicom/CONFORMANCE.md`, `docs/evidence/VALIDATION_REPORT.md`, `docs/services/AUTHORING.md`, `CHANGELOG.md`, `MIGRATIONS.md`, with CI failing on an absent or stale document; `MOS-SAFE-007` mandates `docs/REGULATORY.md` carrying the positioning statement, the responsibility table, the `regulatory_status` enumeration, the `clinical_use_mode` gate and a not-legal-advice disclaimer; `MOS-REL-097` forbids any document describing the platform as validating clinically, with CI grepping for the forbidden phrases | **PARTIAL** — a strong, CI-enforced documentation set that is missing three of the clause's items: **operator security measures** (`SECURITY.md` is a vulnerability-reporting and supported-versions document, not a hardening guide), **known limitations and residual risks of the platform** (`MOS-CONF-115`), and **decommissioning and data disposal** (`MOS-API-011` allows deletion of exactly two resources — `Study` and an unsealed `DatasetVersion` — and `MOS-SEC-154` forbids dropping an audit partition; there is no product-level decommissioning procedure). |
| 8 — Post-market activities | A post-market plan: collect and evaluate field experience, communicate updates and known issues, state the period of support, and handle end-of-support. | PLATFORM (product) / PUBLISHER (device) / SITE (its own deployment) | `MOS-EVID-135`–`MOS-EVID-142` signed 30-day monitoring reports per `(tenant, capability, service_version)` with PSI drift, plausibility, envelope and review-disagreement signals, hard breaches driving `SUSPENDED` and no auto-recovery; `MOS-REG-040` impact query; `MOS-SEC-143`/`MOS-SEC-144` vulnerability budget and daily rescan of published digests; `MOS-SAFE-026`/`MOS-SAFE-027` daily certificate-expiry check demoting clinical deployments to `research_only` with an audit event and a webhook | **PARTIAL, and the weakest area of the spec after lifecycle.** Everything listed is surveillance of a **service** by a **site**, or security surveillance. There is no post-market plan for the MedicalOS product: no period of support, no end-of-support procedure, no field-experience intake, no known-issues communication. |

**MOS-CONF-135** — The platform MUST author a positive intended-use statement for MedicalOS as a health software product — intended purpose, intended users (site IT operator, clinical safety officer, radiologist as a consumer of displayed provenance, service publisher as an integrator), intended operational environment, patient population, and what the product is explicitly not for — and MUST place it in `docs/REGULATORY.md` alongside the `MOS-SAFE-001` positioning statement, not in place of it. `MOS-SAFE-002` and `MOS-CORE-005` remain binding: an intended-use statement is not a clinical claim, and the statement MUST NOT contain one.

**MOS-CONF-136** — The platform MUST publish an operator security guide as a required document under `MOS-REL-095`, distinct from `SECURITY.md`. It MUST state the security measures the deploying site MUST apply for the platform's own controls to hold — network zoning per ch 8, key custody, the tenant KEK model, the DICOM Gateway as the sole PACS credential holder, break-glass being read-only (`MOS-SEC-157`), audit export and offline verification (`MOS-SEC-152`), and the `external_llm_allowed` default-false switch (spine §8). Chapter 8 specifies all of these as engineering; none of them is currently addressed to the person who must operate them.

**MOS-CONF-137** — The platform MUST publish a decommissioning and data-disposal procedure covering: tenant offboarding, PHI and imaging-projection disposal through the Gateway, object-store purge, the retention floor on `AuditEvent` (`MOS-SEC-154`) and on provenance records (`MOS-SAFE-090` append-only at the database role level), and what a site may and may not delete. The procedure MUST be consistent with `MOS-SAFE-073` rows 5–7, which make deletion of a `Result`, `ValidationReport`, `AuditEvent`, `PolicyDecision` or provenance record a hard DENY with supersession as the only path — meaning decommissioning is an **export-and-detach** problem, not a delete problem, and the document MUST say so.

**MOS-CONF-138** — The platform MUST publish a post-market plan covering the product: the supported-version window, how field experience is collected and evaluated (the intake of `MOS-CONF-131`), how known issues are communicated (`MOS-CONF-115`, `MOS-CONF-124`), and the end-of-support procedure including what a site must do when its running version leaves support. The plan MUST NOT cite `MOS-EVID-135`–`MOS-EVID-142` as the platform's post-market surveillance: `MOS-EVID-142` is explicit that a `monitoring_period` report carries a verdict of `INDETERMINATE` with reason `no_reference_standard` and can never carry `PASS`, and `MOS-EVID-136` requires every report to state its review coverage and forbids presenting a disagreement rate as an accuracy figure. Those constraints exist because the monitoring plane measures a service against no ground truth; reusing it as evidence about the platform would be a category error the spec has already taken care to avoid.

**MOS-CONF-139** — Usability engineering under IEC 62366-1 is out of scope for this chapter and is owned elsewhere in chapter 18. §18.4 records only its consequence for 82304-1 clause 6: product validation is not complete without it, and the platform's use-related safety requirements (`MOS-SAFE-012` mandatory adjacent display; the `REJECTED`/`FAILED` visual distinction of spine §4; `MOS-SAFE-016`'s "Training population not declared by the publisher" string; the RUO badge of `MOS-SAFE-041`) are exactly the items a usability validation would have to cover.

#### 18.4.10. What §18.4 concludes

**MOS-CONF-140** — The honest summary, which MUST be reproduced wherever §18.4 is excerpted:

> MedicalOS is, today, an excellent **configuration management and traceability** system with **no software lifecycle process**. Under IEC 62304 it scores well on clauses 5.2, 5.6, 5.7, 8 and the change-control half of 6 and 7, and scores nothing on 5.1, 5.4, 6.1, 6.2.1–6.2.2 and the whole of 9. It publishes no known-anomaly list, which alone blocks every publisher on clauses 7.1.3 and 5.8. As SOUP it is identifiable to a degree most suppliers cannot match — per-result, offline, signed — and documentable only up to a missing supplier-identity field and a missing hazard list. Under IEC 82304-1 it has strong system-level engineering and weak product-level packaging: no positive intended-use statement, no operator security guide, no decommissioning procedure, no post-market plan, no usability engineering.

**MOS-CONF-141** — The certification backlog that follows from §18.4 is short, ordered by cost-to-value, and is a project plan rather than a lament:

| # | Action | Closes | Cost |
|---|---|---|---|
| 1 | Publish `docs/KNOWN_ANOMALIES.md` per release | 62304 §5.8 residual anomalies; §7.1.3 for every publisher | days |
| 2 | Declare a supplier identity for MedicalOS | 62304 §8.1.2 b) | hours |
| 3 | Publish `docs/SOUP.md` assembling contracts, tolerances, tested-configuration matrix and hazard-contribution list | 62304 §5.3.3, §5.3.4, §7.1.2 c) | weeks |
| 4 | Establish a problem resolution process on the `MOS-SEC-143` template | 62304 clause 9; §6.2.1–6.2.2 | weeks |
| 5 | Publish `docs/MAINTENANCE.md` with a support window and a safety-advisory channel | 62304 §6.1, §6.2.5; 82304-1 clause 8 | weeks |
| 6 | Build a hazard register and bind it to requirement IDs | 62304 §7.1, §7.3; ISO 14971; resolves half of `MOS-OPEN-025` | weeks — the requirement→check half already exists |
| 7 | Write a positive intended-use statement, an operator security guide and a decommissioning procedure | 82304-1 clause 4, §7.2 | weeks |
| 8 | Write a software development plan and adopt clause 4.4 as the route for existing code | 62304 §5.1, §4.4 | months |
| 9 | Usability engineering per IEC 62366-1 | 82304-1 clause 6 | months; owned elsewhere in ch 18 |

**MOS-CONF-142** — Items 1–5 of `MOS-CONF-141` MUST be complete before the first `clinical_use_mode: clinical` Deployment at a site the project does not itself operate. Items 6–9 SHOULD be scheduled against a named release in chapter 15, because `MOS-CORE-026` makes a deferral without a named release undefined scope, and `MOS-CORE-008` makes silence in this document never permission.


### 18.5 ISO 14971:2019, ISO/TR 24971:2020 and IEC 62366-1:2015+AMD1:2020

ISO 14971 is the standard a notified body reaches for first, and it is the standard this specification is least
prepared for. The gap is not where a reader might expect. MedicalOS has an unusually good *substrate* for a risk
file: every hazard this section enumerates already has a named, numbered, tested control, and the controls are
predominantly inherent-safety-by-design rather than warnings. What it does not have is the *process* that turns a
set of good controls into a risk management file: no severity scale, no probability estimation, no risk
acceptability criteria, no residual risk evaluation, no benefit-risk analysis, no risk management report, no
review. The word "hazard" does not appear in a normative sense anywhere in Chapters 1–17, and the only mention of
ISO 14971 in the entire specification is `MOS-OPEN-025`, which defers the decision on whether to adopt it at all.

IEC 62366-1 is worse, and for a more interesting reason. The specification is meticulous about what a result *is*
— its geometry, its identity, its provenance, its coded concepts — and almost silent about how a result is
*shown to a human*. That is precisely where use error occurs. A SEG written correctly into the source Frame of
Reference and rendered as a confident red overlay in a PACS viewer that strips every AI-derived marker is a
correct object in an unsafe presentation. This section says so explicitly, names the missing artifacts, and
assigns them.

#### 18.5.1 Applicability and the three-owner split of the risk process

**MOS-CONF-200** MedicalOS core is infrastructure, not a medical device (`MOS-CORE-005`, `MOS-SAFE-001`), and
therefore has no ISO 14971 obligation *of its own as a manufacturer*. The obligation attaches to the
`ServiceVersion` publisher named in `clinical.legal_manufacturer` (`MOS-SAFE-003`, `MOS-SAFE-020`). This section
MUST NOT be read, cited or exported as a statement that MedicalOS holds a risk management file for a medical
device. It does not, and `MOS-SAFE-002` forbids implying otherwise.

**MOS-CONF-201** When a publisher's device runs on MedicalOS, MedicalOS is SOUP in that publisher's IEC 62304
file (§18.4). ISO 14971 clause 5.4 then requires the publisher to identify the hazards arising from that SOUP,
and ISO/TR 24971 §5.4 guidance on software makes the point concretely: hazards arising from a software component
the manufacturer did not write must be identified from what the component's supplier documents about it. A SOUP
supplier that publishes nothing forces the publisher to guess. Therefore the platform MUST publish a **hazard
register** (§18.5.3) — the set of hazards the platform's own behaviour can contribute to, each with its
implemented control and its residual — as a maintained, versioned, machine-readable artifact intended for
incorporation by reference into a publisher's risk management file.

**MOS-CONF-202** The platform hazard register MUST carry, as its first line and in any export of it, the
statement: *"This register enumerates hazards contributed by the MedicalOS platform and the controls implemented
against them. It is SOUP documentation. It is not a risk management file, contains no risk estimation, no risk
acceptability criteria and no benefit-risk analysis, and does not discharge any ISO 14971 obligation of the
`ServiceVersion` publisher."* This is the same discipline `MOS-EVID-004` and `MOS-SAFE-008` impose on the word
"validation", applied to the word "risk".

**MOS-CONF-203** Obligations under ISO 14971 are assigned as follows and MUST NOT be silently reassigned by
configuration, mirroring `MOS-SAFE-004`:

| Owner | Holds | Basis |
|---|---|---|
| PUBLISHER | The risk management plan, file, analysis, evaluation, residual risk evaluation, benefit-risk analysis, risk management report and review, for the device that is the `ServiceVersion` | `MOS-SAFE-003`, `MOS-SAFE-005` |
| PLATFORM | The hazard register of §18.5.3, the implemented controls it cites, the evidence that each control is verified, and the post-production signals of `MOS-EVID-135`–`MOS-EVID-141` | this section |
| SITE | Risk management for the *clinical process* into which results are introduced — workflow placement, reader training, display conformance, escalation, and the risks introduced by the site's own configuration choices | `MOS-SAFE-004`, `MOS-SAFE-036` |

**MOS-CONF-204** A site that rebuilds a `ServiceVersion`, substitutes weights, edits a `PreprocessingSpec` or
alters a declared operating threshold becomes the manufacturer and inherits the whole of the PUBLISHER column
(`MOS-SAFE-005`; enforced mechanically because the signature no longer matches and the E1 gate fails with
`signature_not_from_declared_manufacturer`, `MOS-SAFE-036`). The platform MUST surface this consequence in the
gate response rather than only the violation code.

**MOS-CONF-205** The platform MUST NOT compute, infer or default any risk-related field of the `clinical` block
— this extends `MOS-SAFE-009`, which already forbids it for `intended_use`, `regulatory_status` and
`legal_manufacturer` — to `known_failure_modes[]`, `known_limitations[]`, `not_validated_for[]` and
`risk_classification.imdrf`. The single permitted platform computation over these fields is the structural
consistency check of `MOS-SAFE-019` (IMDRF category derived from the two declared axes) and the envelope
consistency check of `MOS-EVID-099`. Neither is a regulatory determination and both MUST be labelled as
structural checks.

#### 18.5.2 ISO 14971:2019 clause-by-clause mapping

**MOS-CONF-206** The table below is the normative mapping. `Satisfied by` cites only requirements that exist in
Chapters 1–17 of this specification; a cell reading GAP means no requirement in this specification addresses the
clause, not that the clause is inapplicable.

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 4.1 | A risk management process established, documented and maintained across the lifecycle | PUBLISHER | — (publisher's own QMS) | NOT-APPLICABLE to platform |
| 4.1 (SOUP view) | The SOUP supplier documents what the publisher needs to analyse | PLATFORM | `MOS-CONF-201`, §18.5.3 register | GAP — the register is created here; nothing in Ch. 1–17 obliges its maintenance |
| 4.2 | Top-management commitment, a documented **policy for establishing criteria for risk acceptability**, periodic review of process suitability | PUBLISHER | — | GAP — no acceptability policy exists anywhere in this specification; `MOS-OPEN-025` defers the decision to adopt 14971 at all |
| 4.3 | Competence of personnel performing risk management | PUBLISHER / SITE | `MOS-EVID-117` (named approver with role, organisation, written statement and `identity_assurance`), `MOS-SAFE-036` (named `approved_by` holding `deployment.approve_clinical`, plus `approval_rationale` ≥ 20 chars), `MOS-SAFE-068` + `MOS-SAFE-104` (`reviewer_class` derived from `Role.clinically_qualified`, never from a job title string) | PARTIAL — the platform records *who* approved and *under what role*, which is more than most systems do, but records no competence, qualification or training evidence for that person |
| 4.4 | A risk management plan, scoped to a specific device, with acceptability criteria and a verification plan | PUBLISHER | — | GAP |
| 4.5 | A risk management file, traceable per hazard from analysis through control to residual risk | PUBLISHER | Platform supplies file *content*: `MOS-SAFE-082`–`MOS-SAFE-090` (one signed, hash-chained, append-only provenance record per `Result`, offline-verifiable, reproducible via `medicalos-reproduce`), `MOS-EVID-118`–`MOS-EVID-120` (DSSE-signed `ValidationReport`) | PARTIAL — the evidence chain is unusually strong; the file that would organise it by hazard does not exist |
| 5.1 | Risk analysis performed and recorded for the specific device | PUBLISHER | `MOS-SAFE-014` `known_failure_modes[]` is a per-service, per-failure-mode record carrying `{id, text, detection, mitigation}` with `detection ∈ {applicability_envelope, output_plausibility_gate, human_review, none}`; `MOS-SAFE-015` forbids an empty list; `MOS-SAFE-017` makes the ids stable and provenance-referenced | PARTIAL — this is a genuine, machine-enforced, publisher-authored hazard/control record, but it has no severity, no probability, no sequence of events and no residual risk field |
| 5.2 | Intended use **and reasonably foreseeable misuse** documented | PUBLISHER | Intended use: `MOS-SAFE-013` (a manifest without a `clinical:` block is rejected — no default, no inheritance, no partial acceptance), `MOS-SAFE-014` (`intended_use.statement`, `intended_user`, `intended_setting[]`, `reading_paradigm`, `autonomy`, `output_kinds`, `indications[]` with coded terms, `contraindications[]`, `target_population`, `input_constraints`), `MOS-SAFE-021` (`intended_use_digest` over RFC 8785 canonical JSON, pinned in provenance), `MOS-SAFE-031` (immutable once any `Job` has referenced it) | **Intended use: SATISFIED** and better structured than most manufacturers' own IFUs. **Reasonably foreseeable misuse: GAP** — the string "misuse" does not appear in this specification; there is no `foreseeable_misuse[]` field and no place to record one |
| 5.3 | Identification of characteristics related to safety (24971 Annex A question list) | PUBLISHER | `MOS-SAFE-014` (`target_population` incl. `pregnancy` state, `operating_point` with `threshold_definition` and `source_evaluation_run_id`, `training_population` or an explicit null, `risk_classification.imdrf`), `MOS-SAFE-015`/`MOS-SAFE-016` (fail-closed on empty declarations; "Training population not declared by the publisher" surfaced, never hidden) | PARTIAL — the fields cover a good fraction of Annex A for imaging software; there is no requirement to work the Annex A list and no record of having done so |
| 5.4 | Identification of hazards and hazardous situations, including those arising from software and from SOUP | PLATFORM (its contribution) + PUBLISHER (the device) | §18.5.3 register, `MOS-CONF-215` | PARTIAL — the register below is complete for the platform contribution as specified in Ch. 1–17; the publisher half is `known_failure_modes[]` and is unstructured by comparison |
| 5.5 | Risk estimation: severity and probability of occurrence for each hazardous situation | PUBLISHER | — | GAP — no severity scale, no probability scale and no estimation method exists in this specification. `MOS-CONF-226` states why the platform MUST NOT supply the probability half |
| 6 | Risk evaluation against the acceptability criteria of the plan | PUBLISHER | — | GAP — depends on 4.2 and 5.5, both GAP |
| 7.1 | Risk control **option analysis in priority order**: inherent safety by design → protective measures → information for safety | PLATFORM (for its controls) | Overwhelmingly inherent-safety-by-design: deterministic UID derivation with random generation forbidden (`MOS-IMG-062`), resolution as a pure function over a pinned snapshot (`MOS-REG-051`–`MOS-REG-053`), measurement in source geometry (`MOS-IMG-041`, `MOS-IMG-042`), de-identification that fails closed (`MOS-DATA-037`), a default-DENY clinical action table with rows that MUST NOT be made overridable (`MOS-SAFE-073`, `MOS-SAFE-074`), and no code path for an autonomous service (`MOS-SAFE-011`) | PARTIAL — the *practice* is right and the priority order is implicitly honoured throughout; the *analysis* is never declared, so an auditor cannot see that information-for-safety was rejected in favour of design in any given case |
| 7.2 | Implementation and verification of each control | PLATFORM | Every control in §18.5.3 is a numbered requirement with a test. Verification: `MOS-TEST-013` (mutation score ≥ 0.80, release-blocking, over an explicit `safety_critical` list naming `imaging/geometry`, `imaging/uid`, `imaging/dicom_writer`, `dataplane/deidentify`, `dataplane/triage`, `registry/resolve`, `security/rls`, `safety/content_postconditions`), `MOS-TEST-014` (property-based `inverse(forward(v)) == v` and < 1e-6 mm round-trip on 256 random patient-space points per case), `MOS-SAFE-056` (CI parses every golden-path object with pydicom and asserts every marker row) | SATISFIED for the platform-owned controls — this is the strongest clause in the mapping |
| 7.3 | Residual risk evaluated per hazardous situation | PUBLISHER | — | GAP |
| 7.4 | **Benefit-risk analysis** where residual risk is not acceptable | PUBLISHER | — | GAP — the terms "benefit-risk" and "risk-benefit" do not occur in this specification |
| 7.5 | Risks arising from risk control measures themselves | PLATFORM | Performed informally and well in four places: `MOS-DATA-074` (`select_best` is default because rejecting a study over two equivalent thin recons would be user-hostile — with the compensating rule that the ambiguity flag is mandatory), `MOS-DATA-076` (an `applicability` failure under ambient routing must *not* create a `REJECTED` job, or `REJECTED` becomes noise and stops being read), `MOS-EVID-092` (the gate MUST NOT take a capability out of service in order to block a candidate; the incumbent stays live), `MOS-EVID-139` (an alert notifies and MUST NOT change deployment state) | PARTIAL — the reasoning is present and sound; it is prose in four chapters rather than a clause-7.5 record |
| 7.6 | Completeness of risk control | PUBLISHER | — | GAP |
| 8 | Overall residual risk evaluated and **disclosed** in the accompanying information | PUBLISHER | Disclosure half only: `MOS-SAFE-014`/`MOS-SAFE-015` (`known_limitations[]`, `not_validated_for[]`, `known_failure_modes[]`, each ≥ 1 entry, fail-closed), `MOS-SAFE-022` (`GET /service-versions/{id}/clinical` returns the block verbatim with its digest), `MOS-SAFE-012` (manufacturer, version, mode and review status displayed adjacent to every finding without interaction) | PARTIAL — the platform is an excellent *transport* for the disclosure; the evaluation that would produce it does not exist, and nothing requires the disclosed limitations to be shown at the point of reading rather than at an API endpoint |
| 9 | Risk management review before release; a risk management report | PUBLISHER | — | GAP |
| 10.1 | Production and post-production information: collection from production | PLATFORM | Software analogue only: `MOS-IMG-054`/`MOS-IMG-055` (golden-fixture tensor-hash self-test at worker startup; on mismatch the worker MUST NOT become ready and MUST NOT claim any job), `MOS-SVC-026` (`POST /v1/selftest` bundle-digest check at readiness; refuse to serve on mismatch), `MOS-REG-036` (self-test failure moves the `Deployment` to `SUSPENDED`), `MOS-REL-118` (the Definition-of-Done matrix is CI-enforced, never reviewer memory) | PARTIAL — build/release integrity is well controlled; there is no "production process" in the 10.1 sense and the mapping should say so rather than claim conformance |
| 10.2 | Post-production information **collection system**: user feedback, complaints, incidents, publicly available information | PLATFORM (signals) + PUBLISHER (system) + SITE (reporting) | `MOS-EVID-135`–`MOS-EVID-138` (unattended monitoring over production traffic; a signed `monitoring_period` report per 30-day window per `(tenant, capability, service_version)`; ten named signals including `envelope_out_rate`, `input_psi`, `output_volume_psi`, `plausibility_fail_rate`, `review_disagreement_rate` and `review_coverage`, with fixed PSI binning so windows are comparable across sites), `MOS-EVID-104` (envelope decisions counted per `reason_code`), `MOS-SAFE-071` (`ResultReview` outcomes are the only production ground-truth signal obtained for free), `MOS-EVID-141` (signals carry no PHI) | PARTIAL and the most valuable PARTIAL in this chapter — the *telemetry* half is genuinely strong and is more than many cleared devices have. The *complaint* half is entirely absent: no complaint intake, no user feedback channel, no adverse-event record, no literature or public-information review |
| 10.3 | Review of post-production information; reassessment of risk; **action** | PLATFORM (technical) + PUBLISHER (regulatory) | `MOS-EVID-139` (a hard breach drives the `Deployment` to `SUSPENDED`, stops new dispatch, does not kill in-flight work), `MOS-EVID-140` (recovery requires a human action with a rationale and re-runs SAT-1..SAT-3; auto-recovery on a metric drifting back under threshold MUST NOT be implemented), `MOS-REG-021`/`MOS-REG-022` (`RECALLED` is a human decision and is irreversible; a fix is a new version, never a status reversal), `MOS-REG-040` + `MOS-SVC-103` + `MOS-SAFE-032` (recall has an impact query enumerating every affected `job_id`, `result_id`, generated `SeriesInstanceUID` and `tenant_id`, computed from pinned resolution records; affected results are marked, never deleted), `MOS-REG-092` (key revocation invalidates future verification only; the response to a compromised publisher identity is `RECALLED` plus the impact query), `MOS-REG-109` (`artifact.recall` is separately grantable because it is irreversible and patient-facing) | PARTIAL — the *technical* field action is complete and enforced. The *regulatory* act is absent: no requirement to notify anyone. There is no vigilance obligation, no field safety corrective action, no field safety notice, no serious-incident report and no timeline anywhere in this specification |

**MOS-CONF-207** The three clauses that gate certification are 4.2 (risk acceptability policy), 5.5 (risk
estimation) and 7.3/8 (residual risk). They are GAP, they are sequential, and no amount of additional control
implementation closes them. A publisher who reads this chapter as evidence that MedicalOS "does ISO 14971" has
misread it; the platform implements controls, and controls are clause 7.2, which is the one clause that does not
help a manufacturer who has not done clauses 4 through 6.

#### 18.5.3 The engineered hazard register

This is the part of the section with real content. Every hazard below is one the specification already engineers
against; the register makes explicit what was implicit, in the form clause 5.4 asks for.

**MOS-CONF-215** The platform MUST maintain the hazard register below as a versioned artifact at
`docs/RISK-REGISTER.yaml`, machine-readable, with one entry per `HZ-` id carrying `{id, hazard,
hazardous_situation, sequence_of_events[], harm, owner, controls[] (requirement ids), verification[] (test ids),
residual_status, residual_note}`. It MUST be published alongside the SOUP declaration of §18.4 and MUST be
included in the release artifact set.

**MOS-CONF-216** CI MUST assert that every requirement id cited in `controls[]` exists in `docs/spec/` and that
every `HZ-` id referenced from `docs/SOUP-*.yaml` and from this chapter resolves in the register. A dangling
citation in a risk document is the failure mode this whole chapter exists to prevent.

**MOS-CONF-217** The register MUST be reviewed and, where necessary, extended whenever any module in the
`safety_critical` list of `MOS-TEST-013` changes, and the review MUST be a named gate in the Definition-of-Done
matrix of `MOS-REL-118`. A hazard register that is not re-opened when the geometry module changes is decoration.

##### 18.5.3.1 Register — hazard, situation and harm

| ID | Hazard / hazardous situation | Harm | Owner |
|---|---|---|---|
| HZ-01 | Wrong-series selection: a confident result is produced on a reconstruction the model was not validated for | Quantification error read as clinical fact — e.g. %LAA reported off a sharp kernel — leading to mis-staging or an inappropriate intervention | PLATFORM (mechanism), PUBLISHER (selector and envelope correctness) |
| HZ-02 | Geometry or inverse-transform error misregisters a SEG relative to its source series | Segmentation attributed to the wrong anatomy or the wrong slices; lesion mislocalised; in the worst case a wrong-site decision | PLATFORM |
| HZ-03 | A measurement is computed in a grid other than the source grid | Systematically wrong numeric value presented with full precision and no indication of error | PLATFORM |
| HZ-04 | Duplicate or colliding DICOM identity overwrites, or is confused with, a prior result | The object a clinician reads is not the object the report references; a prior finding silently disappears; two clinically distinct runs collapse into one series identity | PLATFORM |
| HZ-05 | A model is applied outside its applicability envelope | Silently degraded performance on a population or acquisition the evidence does not cover; a false negative in a patient group never tested | PUBLISHER (declares), PLATFORM (enforces), SITE (checks coverage) |
| HZ-06 | Stale or re-run capability resolution silently changes which model produced a result | Provenance attributes a result to a version that did not produce it; a suspended or recalled version is resurrected on retry | PLATFORM |
| HZ-07 | Inconsistent or irreversible de-identification UID remapping breaks the link from a result back to its source instances | Every result ever produced under the broken mapping becomes unattributable; an overlay cannot be traced to the images it was computed from | PLATFORM |
| HZ-08 | PHI disclosure via logs, traces, metric labels, a sealed service's stdout, or an external LLM | Privacy harm; secondary safety harm through loss of clinician and patient trust in the record | PLATFORM (controls), SITE (configuration) |
| HZ-09 | An AI result is mistaken for a confirmed diagnosis | Automation bias: a false positive acted on, or a false negative not independently sought | PLATFORM (marking), PUBLISHER (reading paradigm and IFU), SITE (viewer and reader training) |
| HZ-10 | Automated promotion of an unvalidated or regressed model into the clinical read path | An unevaluated decision boundary reads patients | PLATFORM (gate), PUBLISHER (evidence), SITE (approver) |
| HZ-11 | A deployed service's preprocessing diverges from the preprocessing used at training | Silent distribution shift: the model sees inputs it was never trained on, with no error and no signal | PLATFORM |
| HZ-12 | An implausible output reaches DICOM — a mask outside the body contour, an anatomically impossible volume | A gross error rendered as an authoritative overlay | PLATFORM |
| HZ-13 | Silent non-analysis: a study inside the service's envelope is never analysed and nobody is told | A finding that would have been flagged is not; absence is indistinguishable from negative | PLATFORM (mechanism), SITE (must actually look) |
| HZ-14 | Performance drifts after deployment without detection | Progressive, unnoticed degradation across a whole site's traffic | PLATFORM (signals), SITE (response), PUBLISHER (thresholds) |
| HZ-15 | A defective version continues to serve, or its already-produced results are not identifiable | Patients read against a known-defective model; no ability to scope the affected population | PLATFORM (enumeration), PUBLISHER (regulatory action) |
| HZ-16 | Self-reinforcing training loop: a model's confident errors become the labels its successor is trained on | Systematic misses become invisible and are measured as improvement | PLATFORM (blocks the loop), PUBLISHER (cohort design) |
| HZ-17 | Generated narrative asserts a finding the source `Result` does not contain, or asserts it diagnostically | A hallucinated or over-asserted statement in a clinical text | PLATFORM (post-conditions), PUBLISHER (0.4+) |

##### 18.5.3.2 Register — sequence of events, control and residual

| ID | Reasonably foreseeable sequence of events | Existing control | Residual |
|---|---|---|---|
| HZ-01 | A chest CT carries two thin axial recons (soft and sharp kernel) → both satisfy the `SeriesSelector` → their `rank_tuple`s tie → one is chosen → the result is reported without the reader knowing which recon it came from | Selection is a pure function of `(triage records, selector, triage_spec_version)` (`MOS-DATA-081`); services never self-select (`MOS-DATA-004`); every `rank` list MUST end with `series_instance_uid asc`, rejected at registration otherwise, so the tie-break is deterministic and recorded (`MOS-DATA-072`, `MOS-DATA-073`); a tie sets `ambiguous: true` with `ambiguity_peers[]` and the flag MUST be surfaced in the provenance panel **and in the generated SR** (`MOS-DATA-074`); the selected series list is pinned into the `Job` row at creation (`MOS-DATA-082`); per-series rejections are first-class typed data, never prose (`MOS-DATA-079`); the full selection report is retrievable through the API (`MOS-DATA-064`); the provenance panel renders the consumed series list with the rejected ones collapsed but present (`MOS-SAFE-088`); a %LAA measurement is reported with the source `ConvolutionKernel` and slice spacing alongside it (`MOS-IMG-041`); `dataplane/triage` is a `safety_critical` module at mutation score ≥ 0.80 (`MOS-TEST-013`) | **PARTIAL.** The default `ambiguity.policy` is `select_best` (`MOS-DATA-074`), so one of two equivalent recons is chosen and the clinician is informed by a flag. No requirement specifies the *prominence* of that flag in any viewer, and a third-party viewer renders none of it. The residual is a presentation residual, and it belongs to HZ-09 |
| HZ-02 | Model output is produced in model space → the inverse transform recomputes the affine instead of using the recorded one, or interpolates labels linearly → the SEG is written offset by one or more slices, or carrying a `FrameOfReferenceUID` that does not match the source | The inverse MUST be the exact inverse of the forward chain step by step (`MOS-IMG-032`); it MUST target the **recorded** canonical grid, with shape equality and element-wise affine agreement within 1e-4 asserted, and a mismatch is a `FAILED` job with `geometry_mismatch`, never a silently resized array (`MOS-IMG-033`); discrete labels MUST use nearest-neighbour or `onehot_linear_argmax`, and linear/spline interpolation of integer labels is forbidden because it invents labels (`MOS-IMG-034`); a further nearest-neighbour resample onto source slice positions is mandatory when the canonical grid was resampled, recorded as `source_grid_resample_applied` in provenance (`MOS-IMG-035`); slice spacing is derived from `ImagePositionPatient`, never from tags (`MOS-IMG-016`); orientation consistency is checked per instance (`MOS-IMG-015`); gantry tilt is detected by two independent mandatory tests and rejected by default, and MUST NOT be "corrected" by shearing the affine while leaving voxels in place (`MOS-IMG-020`–`MOS-IMG-022`); every geometry failure terminates the job `REJECTED` (`MOS-IMG-010`); `FrameOfReferenceUID` on a SEG MUST be copied from the source (`MOS-IMG-068`); a derived grid requires a Spatial Registration object written in the same commit or the job FAILS `derived_geometry_incomplete` (`MOS-IMG-043`); one tolerance table governs all epsilons (`MOS-IMG-044`). Verification: `imaging/geometry` is `safety_critical` (`MOS-TEST-013`) and carries property-based round-trip tests over randomly generated physically valid acquisitions (`MOS-TEST-014`) | **SATISFIED.** This is the best-controlled hazard in the system. Residual: the 22-fixture geometry corpus is finite, and multi-frame Enhanced CT/MR per-frame geometry (`MOS-IMG-008`) is a less-exercised path than single-frame |
| HZ-03 | A volume or %LAA is computed on the resampled model-space grid, or a diameter by multiplying an index distance by one spacing scalar → the number is wrong by several percent and is displayed to full precision | Intensity-threshold measurements MUST be computed on source HU after the Modality LUT, without any resampling, and reported with the threshold, source kernel and Δs — computing %LAA on a resampled grid is explicitly forbidden (`MOS-IMG-041`); linear measurements MUST be transformed through the affine, never by a single spacing scalar (`MOS-IMG-042`); `PixelMeasuresSequence` carries the **source** `PixelSpacing` and slice thickness (`MOS-IMG-106`); the platform independently recomputes every volume measurement from the shipped mask on the source grid and FAILS with `result_measurement_inconsistent` beyond `ε_meas` — a service may not report a volume that disagrees with the mask it shipped (`MOS-IMG-144`); plausibility geometry is likewise evaluated in source geometry (`MOS-EVID-107`) with a `measurement_consistency` rule using the platform's own volume code (`MOS-EVID-109`); measurements delivered outside DICOM carry unit, coded concept, `derivation: "ai_derived"` and `score_threshold` (`MOS-SAFE-054`) | **SATISFIED.** Residual: `ε_meas` is a single global tolerance; a capability whose measurement is more sensitive than that tolerance would pass the consistency check while being clinically wrong. `MOS-OPEN-024` already flags that the %LAA reference standard is undecided |
| HZ-04 | A job is retried, or two workers race, or a caller supplies its own idempotency key → a UID is regenerated randomly or derived from a client-controlled value → an existing object is overwritten, or two distinct runs land in one series | Every minted UID MUST come from the derivation function over the complete tuple `(tenant_id, idempotency_key, service_id, service_version, model_id, model_version, uid_space, uid_kind, output_index)`; random generation is forbidden anywhere in the DICOM writing path (`MOS-IMG-062`); the same UID therefore implies the same declared inputs, and any change to pixels, segments or measurements requires a version change which produces a different UID (`MOS-IMG-085`); `jobs.idempotency_key` is platform-derived, never random and never client-supplied (`MOS-EXEC-053`), and the HTTP `Idempotency-Key` header is stored separately and MUST NOT participate in UID derivation precisely because a client-chosen key "would let one tenant's caller collide two clinically distinct runs into one DICOM series identity" (`MOS-EXEC-054`); `UNIQUE (tenant_id, idempotency_key)` is the single enforcement point (`MOS-EXEC-055`); a retry MUST NOT recompute the key and MUST NOT re-run resolution (`MOS-EXEC-012`); before the first write of a derived `SeriesInstanceUID` the platform issues a cross-study QIDO-RS query and FAILS with `dicom_uid_collision` if it exists elsewhere (`MOS-IMG-070`); completeness is determined by comparing the **set** of `SOPInstanceUID`s (`MOS-IMG-082`); reconciliation is concurrency-safe because both workers compute the same UIDs (`MOS-IMG-086`); `StudyInstanceUID` MUST NEVER be minted (`MOS-IMG-069`); altering an existing instance, or STOWing a UID that exists with different content, is **DENY (hard)** with no override path in 0.1–0.4, enforced by a Gateway STOW guard on content digest (`MOS-SAFE-073` row 5, `MOS-SAFE-074`); `SeriesNumber` comes from a reserved band ≥ 9000 so generated series never take a modality number (`MOS-IMG-071`, `MOS-IMG-072`); the write is the commit point and recovery is forward-only via `superseded_by`, with no compensating deletion (`MOS-IMG-146`); DICOM writing exists in exactly one package (`MOS-IMG-145`); `imaging/uid` and `imaging/dicom_writer` are `safety_critical` (`MOS-TEST-013`) | **SATISFIED** |
| HZ-05 | A 6 mm recon, a paediatric patient, or a scanner vendor absent from the validation cohort arrives → the job runs → a confident output is produced on data the evidence does not cover | Every `ServiceVersion` declaring a capability MUST carry an `ApplicabilityEnvelope` version, and one without it MUST NOT be deployable in `clinical` mode (`MOS-EVID-095`); a study attribute evaluates to exactly one of `IN`/`MARGINAL`/`OUT`, the study takes the worst zone, and a **missing attribute evaluates to `MARGINAL`, never to `IN`** (`MOS-EVID-096`); numeric bounds MUST be derivable from the cohort's `acquisition_profile` (`MOS-EVID-097`) and a publisher MUST NOT widen a bound without a covering `EvaluationRun` — the rule that stops "validated on 2.5 mm archival data, declared for 0.6 mm" from being a manifest edit (`MOS-EVID-098`); `not_validated_for[]` MUST be consistent with the envelope, CI-enforced at publish (`MOS-EVID-099`); evaluation happens once at triage, after selector match and before dispatch, and the service cannot influence it (`MOS-EVID-100`, `MOS-SVC-023`); a breach is `REJECTED`, a clinical outcome, visually distinct from `FAILED`, with a structured per-constraint reason carrying `observed` and `bound` (`MOS-EVID-102`, `MOS-SVC-096`) from a closed `reason_code` vocabulary (`MOS-EVID-103`); explicit requests always produce an answerable job id (`MOS-DATA-077`) while ambient routing writes a `NOT_APPLICABLE` triage decision instead, so `REJECTED` does not become noise (`MOS-DATA-076`); envelope decisions are counted per `reason_code` (`MOS-EVID-104`) and site acceptance reports envelope coverage against the site's own traffic (`MOS-EVID-132`) | **PARTIAL.** The closed `reason_code` set of `MOS-EVID-103` covers ten attributes: slice thickness, pixel spacing, z-coverage, instance count, kernel class, manufacturer, contrast phase, body part, patient age, and attribute-absent. `target_population.sex` and `target_population.pregnancy` are **required declarations** in `MOS-SAFE-014` with **no enforcement path** — there is no `patient_sex` envelope attribute and no `reason_code` for one, even though triage already extracts `patient_sex` (`MOS-DATA-061`). A service declaring `sex: female` or `pregnancy: excluded` will run on any patient. Likewise there is no envelope attribute for prior surgery, implants, motion artefact or image quality. See gap G-14971-06 |
| HZ-06 | A job is retried after a registry change, or a capability is resolved twice → a different `ModelVersion` serves the second run → provenance and object disagree, or a suspended version keeps serving | `Resolve()` MUST be called with a snapshot at an `epoch`, never a live database handle (`MOS-REG-011`, `MOS-REG-012`); it is deterministic — same request, same snapshot, byte-identical outcome (`MOS-REG-053`); the resolved set, as the transitive closure of `ServiceVersion`, its `ModelVersion`s, its `PreprocessingSpec`s and the `Deployment`, is written into the `Job` row in the same transaction (`MOS-REG-066`); a retry MUST NOT re-run resolution (`MOS-EXEC-012`); if a pinned version became `SUSPENDED` or `RECALLED` between attempts the retry MUST NOT run and the job goes `REJECTED` with `pinned_version_recalled`/`pinned_version_suspended`, re-analysis requiring a **new** Job carrying `supersedes_job_id` so it is visible as a distinct clinical event (`MOS-REG-068`); an explicit pin cannot resurrect a suspended or recalled version (`MOS-REG-056`); zero candidates is `REJECTED` and never a silent fallback (`MOS-REG-069`); resolution is reproducible offline from `(epoch, snapshot_id, inputs_hash)` plus the changelog (`MOS-REG-071`); `deployment_environment`, `deployment_state_at_execution` and role are snapshotted onto the `Result` and carried in provenance (`MOS-SAFE-046`); there MUST NOT be a dispatch path that bypasses `Resolve` (`MOS-REG-054`); `registry/resolve` is `safety_critical` (`MOS-TEST-013`) | **SATISFIED** |
| HZ-07 | The de-identification UID mapping is regenerated per invocation, or the map store is lost → a `ResultBundle` references source `SOPInstanceUID`s that no longer resolve → results cannot be tied to images | The de-identified UID generator MUST be a single shared library used by the Gateway and every consumer (`MOS-DATA-032`) with `deid_uid_map` as the authoritative record and the generator as its rebuild path (`MOS-DATA-033`); the platform MUST translate every source SOP Instance UID, Series Instance UID and Frame of Reference UID in a returned bundle back from the de-identified space (`MOS-DATA-036`); a referenced UID with no reverse mapping MUST FAIL the job (`MOS-IMG-088`); de-identification MUST fail closed, and an unavailable `deid_uid_map` store MUST cause the Gateway to refuse rather than proceed (`MOS-DATA-037`, `MOS-DATA-010`); the mapping table has its own protections in Ch. 8 (`MOS-SEC-104`); PS3.15 Annex E profiles are applied under a versioned per-tenant policy with a monotonic, never-reused `deid_policy_version` (`MOS-DATA-027`, `MOS-DATA-028`) and every object carries `PatientIdentityRemoved` and `DeidentificationMethod` (`MOS-DATA-029`); `dataplane/deidentify` is `safety_critical` (`MOS-TEST-013`) | **SATISFIED.** Residual: loss or corruption of `deid_uid_map` is an availability hazard with no stated recovery objective; backup and restore of that table is not specified as a safety-relevant control anywhere |
| HZ-08 | A DICOM UID or patient identifier reaches a structured log, an OTel span attribute, a metric label, a sealed vendor container's stdout, or an external LLM prompt | Logging goes only through a closed-attribute-type constructor in Go and Python (`MOS-SEC-107`, `MOS-SEC-109`) with error chains sanitized to their class so a driver error cannot quote a parameter value (`MOS-SEC-108`); a second independent redaction filter runs in the shipper (`MOS-SEC-110`); CI runs a PHI scanner over the repository, a fixture log corpus, and exported traces and metrics, seeded with known identifiers (`MOS-SEC-111`); metric label names come from a closed allowlist with declared value enumerations and bounded cardinality (`MOS-SEC-112`–`MOS-SEC-114`), and `patient`, `study`, `series`, `instance`, `accession`, `sop`, `uid` MUST NOT appear as substrings of a label name (`MOS-SEC-115`); span attributes use the same allowlist and span names are route templates (`MOS-SEC-116`, `MOS-SEC-117`); a sealed service's stdout is treated as PHI-bearing and reading it requires a permission (`MOS-SEC-119`, `MOS-SEC-120`); sending DICOM-sourced text or pixels to an external endpoint is **DENY** by default under a per-tenant `external_llm_allowed` switch (`MOS-SAFE-073` row 16, `MOS-SEC-122`); exporting PHI outside the tenant boundary is **DENY**, de-identified evidence export only (`MOS-SAFE-073` row 15); provenance records MUST contain no `PatientName`, no `PatientBirthDate` and no free-text clinical history (`MOS-SAFE-086`); monitoring signals carry only `tenant_id`, `capability_id`, `service_version`, `reason_code` (`MOS-EVID-141`); burned-in pixel PHI is screened by tenant policy (`MOS-DATA-038`); RLS is forced on every tenant table or the migration fails CI (`MOS-SEC-077`) and `security/rls` is `safety_critical` (`MOS-TEST-013`) | **SATISFIED** as a security control set. Note for the risk file: under ISO 14971 the *harm* here is not physical, and privacy risk is properly managed under a security risk process (AAMI TIR57 / IEC 81001-5-1) **interfaced to** the 14971 file, not inside it. The 14971-relevant consequence is indirect and is captured as HZ-07. The publisher MUST make that interface explicit rather than filing PHI controls as 14971 risk controls |
| HZ-09 | A SEG and SR land in the reading worklist beside the modality series → the reader opens them in the hospital's own PACS viewer, which renders the overlay but none of the AI-derived markers → a confident red overlay is read as a confirmed finding, or an absent overlay is read as a negative study | Every generated object MUST be identifiable as AI-derived and its mode determinable **from metadata alone**, from a QIDO-RS response without retrieving pixels (`MOS-SAFE-047`), via a base marker set on every object (`MOS-SAFE-048`), per-object-type markers (`MOS-SAFE-049`), an RUO marker set in research mode (`MOS-SAFE-050`) and a burned-in `RESEARCH USE ONLY` banner on research-mode SC frames (`MOS-SAFE-051`); marking is decided at write time from the Job's pinned mode and is never retroactively rewritten (`MOS-SAFE-055`); CI parses every golden-path object and asserts every marker row (`MOS-SAFE-056`, `MOS-TEST-036`); MedicalOS-controlled surfaces MUST display service id, version, `legal_manufacturer.name`, `clinical_use_mode` and `review_status` adjacent to the finding without interaction (`MOS-SAFE-012`); an SR with `VerificationFlag = VERIFIED` without a human `ResultReview` is **DENY (hard)** (`MOS-SAFE-073` row 13) and research-mode SRs are written `UNVERIFIED` with no `VerifyingObserverSequence` (`MOS-SAFE-042`); results are stored and visible but **nothing is auto-actioned** (`MOS-SAFE-057`), no transition may notify a clinician or write to an external clinical system (`MOS-SAFE-063`), and asserting a finding to a clinician by page/SMS/escalation is **DENY** with no transport wired (`MOS-SAFE-073` row 12); a `REJECTED` review hides nothing and is rendered everywhere (`MOS-SAFE-064`); an `autonomy: autonomous` service is refused at deployment (`MOS-SAFE-011`, `MOS-SAFE-073` row 19); generated narrative is rejected for assertive-diagnosis patterns without an attribution token (`MOS-SAFE-080`); platform language is policed (`MOS-CORE-004`, `MOS-SAFE-008`, `MOS-EVID-004`); **AMENDED at specification 0.4.0** — ~~a headless-browser rendering check on the pinned OHIF asserts the overlay is present at the expected slices and the SR panel hydrates~~ a headless-browser rendering check on a pinned viewer asserts the overlay is present at the expected slices and the SR panel hydrates (`MOS-IMG-157a`, `MOS-TEST-062`, `MOS-TEST-068`), and from 0.4.0 that viewer is an independent renderer drawn from the `MOS-REL-027` incumbent list rather than the platform's own — the check exists to fail on a defect in the **object**, and a check the platform runs against its own renderer cannot do that. `MOS-IMG-157`'s own text is struck and `MOS-IMG-157a` carries the obligation; that amendment is Chapter 4's, and this cell records which reading of it this hazard's control set depends on. The control is weaker than the citation looks: register entry 107 records that the check has never been executed, and that `MOS-TEST-068`'s `viewer-pin` job asserts a running container's digest against a lock file that does not exist and a viewer the stack no longer runs | **PARTIAL, and this is the most important residual in the register.** `MOS-SAFE-012` binds "web UI result panel, OHIF provenance panel, exported PDF, SR rendering" — that is, the surfaces MedicalOS controls. It does not and cannot bind the hospital's PACS viewer, which is where most reading actually happens and which will render a SEG overlay with none of it. `MOS-IMG-158` defers third-party viewer verification to release 0.4.0 and only as a documented manual procedure with screenshots. Nothing requires the SITE to verify that its own viewer surfaces the markers, and nothing requires reader training. The residual is owned jointly by SITE and PUBLISHER, and the specification currently assigns it to neither. See gaps G-62366-05 and G-62366-06. **Amended at specification 0.4.0:** the binding list quoted above is `MOS-SAFE-012`'s wording as it stood at 0.3.0, and its second item named a surface release 0.4.0 no longer deploys. Chapter 9 owns that text and has amended it in this same version — `OHIF provenance panel` is struck and the item now reads `the clinician surface's provenance panel`, with no field leaving the set and none becoming optional. The quotation is left standing as it was so that a reader holding a conformance report written against 0.3.0 can find the wording it was checked against. The residual does not turn on it, because the residual is about the surfaces MedicalOS does **not** control and that set did not change when the controlled one became first-party. One thing did change and it cuts the other way: the clinician surface is now a surface the platform can fix rather than configure, so a defect in how a marker is presented there is no longer a third party's release to wait for — which removes an excuse and supplies no evidence |
| HZ-10 | An automated evaluation produces a better metric → automation promotes the candidate → a model with no signed report and no named approver serves clinical reads | Setting `clinical_use_mode = clinical` is refused unless all nine E1 conditions hold, the response enumerating every failure: manifest signature verifies against the declared manufacturer key, clinical block digest matches, `autonomy == assistive`, a non-expired CLEARED `regulatory_status` exists for the deployment's jurisdiction, the cited `ValidationReport` resolves and its signature and digest verify, every `AcceptanceCriteria` criterion is met, the deployment is production-serving, `operating_point` is declared where any finding is probabilistic, and the request carries a named `approved_by` holding `deployment.approve_clinical` plus a rationale of ≥ 20 characters (`MOS-SAFE-036`); the passing condition set, approver and rationale are written to an `AuditEvent` carried in the provenance of every subsequent result (`MOS-SAFE-037`); the gate is queryable live whether or not the deployment is clinical (`MOS-SAFE-045`); the evidence gate refuses on `FAIL` or `INDETERMINATE` after checking signature, criteria version, subject binding, report expiry, cohort, partition and reference-of-record (`MOS-EVID-090`), the incumbent stays live rather than being taken out of service to block a candidate (`MOS-EVID-092`), and the gate re-opens on a change to the version, the `PreprocessingSpec`, any operating threshold or the criteria version (`MOS-EVID-094`); regression criteria are one-sided paired non-inferiority tests over shared cases with a declared, justified margin (`MOS-EVID-085`–`MOS-EVID-087`); the report names a real identifiable human with an `identity_assurance` value (`MOS-EVID-117`) and is DSSE-signed with a publisher- or tenant-held key (`MOS-EVID-118`, `MOS-EVID-119`); the training pipeline MUST NOT hold a signing key and MUST NOT populate `approver` (`MOS-TRAIN-151`); **no automated path MUST exist from production data to a serving model** (`MOS-TRAIN-005`) and an end-to-end negative test drives a complete pipeline run to `PASS` and asserts zero `deployments` rows changed (`MOS-TRAIN-018`, `MOS-TEST-087`); changing a threshold, `PreprocessingSpec` or weights in place is **DENY (hard)** because the signature covers the artifact (`MOS-SAFE-073` row 17); promotion is **DENY** until E1 passes (row 18); the reverse transition to `research_only` is always permitted and never gated (`MOS-SAFE-038`) | **SATISFIED.** This is the second-strongest control set in the system and it is exactly the clause-7.1 "inherent safety by design" pattern: the unsafe operation has no code path, rather than a warning |
| HZ-11 | A `PreprocessingSpec` is edited, or a library version drifts → the serving preprocessing no longer matches training → inputs shift silently with no error | The worker runs the pinned `PreprocessingSpec` on the shipped golden fixture at startup and compares the model-space tensor hash to the training-time value (`MOS-IMG-054`); on mismatch it MUST NOT become ready and MUST NOT claim any job, emitting `preprocessing_selftest_failed` with expected and observed digests (`MOS-IMG-055`, `MOS-EXEC-016` failure code table); the self-test result is recorded (`MOS-IMG-057`) and carried in provenance; the affected `Deployment` moves to `SUSPENDED` (`MOS-REG-036`); a `sealed`-mode service ships no golden fixture and is gated instead by `POST /v1/selftest` against `expected_result_bundle_digest`, with the pod drained on mismatch (`MOS-SVC-026`); the `PreprocessingSpec` is a versioned artifact under the same signature as the weights, with one implementation imported by serving and training alike; preprocessing MUST be deterministic on CPU with a single code path (`MOS-IMG-048`) | **SATISFIED** |
| HZ-12 | A model produces a mask outside the body contour or a physiologically impossible volume → the object is written and rendered as authoritative | Plausibility rules are declared per capability as a versioned `PlausibilityRuleSet`, evaluated **by the platform** after the service returns and before any DICOM object is written, and a service MUST NOT evaluate its own rules or set their outcome (`MOS-EVID-105`); rule types are a closed set (`MOS-EVID-106`); geometry is evaluated in source geometry (`MOS-EVID-107`) with a deterministic `body_contour` construction (`MOS-EVID-108`) and a `measurement_consistency` rule recomputing from the shipped mask (`MOS-EVID-109`); a `fail` is a defect of the service's output and is therefore `FAILED`, not `REJECTED` (`MOS-EVID-111`); a rule MUST be calibrated against the acceptance run before promotion, with a false-fire ceiling of 1 % (`MOS-EVID-112`); `plausibility_warn_rate` and `plausibility_fail_rate` are monitored signals with alert and hard-breach thresholds (`MOS-EVID-137`) | **SATISFIED** |
| HZ-13 | A study inside the service's envelope yields no eligible series → nothing is produced → the absence is read as a negative study, or is never noticed at all | A study that passes `applicability` but yields zero eligible candidates for a required `SeriesRequirement` MUST create a `Job` and terminate it `REJECTED` with `no_eligible_series` **even under ambient auto-routing**, because "this study was inside the service's clinical envelope and was not analysed" is clinically actionable information a radiologist must be able to see (`MOS-DATA-075`); a declared capability that produces no output on a qualifying input MUST be reported as a per-capability rejection, never as silence (`MOS-SVC-011`); `REJECTED` is a clinical terminal outcome visually distinct from `FAILED` in every UI, carrying a machine-readable reason (`MOS-EXEC-001`, `MOS-EVID-102`); job-level reason codes are a closed set (`MOS-DATA-080`); rejection reasons carry `observed` and `allowed` so they are legible without reading the manifest (`MOS-SVC-096`) | **PARTIAL.** The platform makes the non-analysis visible and typed. Nothing in this specification requires anyone to look. There is no worklist obligation, no SITE requirement to route `REJECTED` jobs to a human, and no alert on a rising rejection rate other than the envelope counters of `MOS-EVID-104`. "Visible in an API" is not a control against a hazard whose harm is that nobody noticed |
| HZ-14 | Case mix, scanner fleet or protocol changes after go-live → performance degrades gradually → no single case looks wrong | Unattended monitoring over production traffic with a signed 30-day `monitoring_period` report per `(tenant, capability, service_version)` (`MOS-EVID-135`); ten named signals with default alert and hard-breach thresholds (`MOS-EVID-137`); PSI with fixed binning so numbers are comparable across windows and sites (`MOS-EVID-138`); an alert notifies and MUST NOT change deployment state, while a hard breach drives the `Deployment` to `SUSPENDED`, stops new dispatch and leaves in-flight jobs alone (`MOS-EVID-139`); recovery requires a human action with a rationale and re-runs SAT-1..SAT-3, and auto-recovery MUST NOT be implemented (`MOS-EVID-140`); a `monitoring_period` report MUST NOT carry a `PASS` verdict (`MOS-EVID-142`); site acceptance establishes the envelope-coverage baseline against the site's own traffic (`MOS-EVID-132`) | **PARTIAL, honestly acknowledged by the specification itself.** `MOS-EVID-136` states that monitoring has **no ground truth**. Every signal is a proxy. The one signal closest to truth, `review_disagreement_rate`, is gated by `review_coverage`, whose own default alert fires below 0.05 because the signal becomes uninformative — and nothing obliges a site to review any results at all. A site that reviews nothing has monitoring that cannot detect a performance change |
| HZ-15 | A defect is found in a deployed version → it must stop serving and every result it produced must be identifiable | `RECALLED` is a human decision, separately permissioned because it is irreversible and patient-facing (`MOS-REG-021`, `MOS-REG-109`), irreversible with a fix expressed as a new version rather than a status reversal (`MOS-REG-022`); it forces all deployments to `DRAINING` and makes the version unreachable from both the capability path and the arrival path (`MOS-REG-054`, `MOS-REG-056`); in-flight results from a recalled version are flagged `produced_by_recalled_version` (`MOS-REG-057`); the impact query returns every `job_id`, `result_id`, generated `SeriesInstanceUID` and `tenant_id`, computed from pinned resolution records and provenance, and MUST ship in the same release as `RECALLED` because "a recall state with no impact query is not a recall" (`MOS-REG-040`, `MOS-SVC-103`, `MOS-SAFE-032`); affected results are marked, never deleted (`MOS-SVC-103`, `MOS-SAFE-073` row 7); a `ServiceVersion` referenced by any `Job` cannot be deleted or have its `clinical` block mutated (`MOS-SAFE-031`, `MOS-SVC-116`); a daily rescan finding a new critical vulnerability auto-`SUSPENDS` but MUST NOT auto-`RECALL` (`MOS-REG-091`); key revocation invalidates future verification only, the response to a compromised identity being recall plus impact (`MOS-REG-092`); a deployment row is never deleted because it is permanent provenance (`MOS-SEC-037a`, `MOS-API-054b`) | **PARTIAL.** Technical field action is complete. The regulatory act is absent: nothing requires anyone to be **told**. No field safety notice, no field safety corrective action, no serious-incident report, no competent-authority or FDA notification, no timeline, no record of having notified. The impact query produces the list; the specification stops there. See gap G-14971-09 |
| HZ-16 | A deployed model's masks seed the next annotation round → readers correct what is visibly wrong and leave what looks right → the successor is trained on its predecessor's decision boundary and measured as better while its systematic misses stay invisible | No automated path from production data to a serving model (`MOS-TRAIN-005`); the corpus stratification check at seal (`MOS-TRAIN-088`) and the C5 harvest check applying the no-widening rule at the producing end (`MOS-TRAIN-090`, `MOS-EVID-098`); MONAI Label's own training loop MUST be unreachable and any weights it produces MUST NOT be registrable as a `ModelVersion`, because a labelling tool that trains on the labels it collects is the closed loop implemented inside a tool with no dataset, split, evaluation or report (`MOS-TRAIN-106`); patient-level splits with leakage checks (`MOS-EVID-029`); `AnnotationSet` names readers and the consensus rule; the three human acts of the promotion gate (`MOS-TRAIN-151`) | **PARTIAL.** The mechanical loop is broken. The *epistemic* loop — a reference standard partly derived from the model being evaluated — is described in Ch. 17 as a known trap and is mitigated by cohort discipline rather than prevented. It is a PUBLISHER risk and belongs in the publisher's file with a stated mitigation |
| HZ-17 | An agentic report step generates narrative → it states a finding, laterality or number that the source `Result` does not contain, or states it diagnostically | Content-safety post-conditions extract every numeric, laterality term and finding label from generated text and assert set-membership in the source `Result`, failing closed on unmatched tokens, with `hallucination_rate` defined as that unmatched-token rate (spine §13, Ch. 11); the assertive-diagnosis lexicon check rejects narrative matching a closed pattern set without an attribution token (`MOS-SAFE-080`); `safety/content_postconditions` is `safety_critical` at mutation score ≥ 0.80 (`MOS-TEST-013`); safety rules are published in two explicitly labelled classes and a GUIDANCE-class rule MUST NOT be listed under any heading containing "control", "safeguard", "mitigation" or "requirement" (`MOS-SAFE-078`, `MOS-SAFE-081`) | **PARTIAL by design** — the agentic layer is 0.4+ and not in the MVP (spine §13). The controls are specified ahead of the feature, which is the right order. The residual is that token set-membership catches fabricated *tokens* and not fabricated *relations* between real tokens |

**MOS-CONF-218** Every entry in a publisher's `known_failure_modes[]` (`MOS-SAFE-014`) whose `detection` is
`applicability_envelope`, `output_plausibility_gate` or `human_review` MUST cite the corresponding `HZ-` id and
the platform requirement that implements that detection. A publisher claiming `detection:
output_plausibility_gate` without a `PlausibilityRuleSet` rule covering the failure mode is claiming a control
that does not exist, and CI SHOULD warn on it at `ServiceVersion` registration.

**MOS-CONF-219** A `known_failure_modes[]` entry with `detection: none` MUST be treated as an undetected failure
mode and MUST be carried through to every surface named in `MOS-SAFE-012`. The platform MUST NOT allow
`detection: none` to be the least visible value merely because it is the least work to declare.

**MOS-CONF-220** The register MUST record, per hazard, the **severity** of the potential harm on a scale the
platform declares once and does not vary per publisher. It MUST NOT record a probability of occurrence, and MUST
NOT compute a risk index. Probability depends on the clinical context, prevalence, workflow placement and reader
behaviour at a specific site with a specific device — none of which the platform knows. A platform-supplied
probability would be a fabricated number in a document whose entire value is that its numbers are real.

**MOS-CONF-221** The register MUST record, per hazard, the **detectability** of the failure by the control cited
— specifically whether the control fails the job, rejects the job, flags the output, or only records a fact for
later inspection. HZ-13 exists in the register precisely because "records a fact for later inspection" is a
materially weaker control than the others and must not be presented as equivalent to them.

#### 18.5.4 ISO/TR 24971 guidance applied to this system

ISO/TR 24971 is guidance, not a requirement, and no clause of it can be SATISFIED. Three of its sections change
what this specification should do, and are recorded as obligations here.

**MOS-CONF-225** 24971 §4.2 expects the risk acceptability policy to be stated in terms a reviewer can apply.
For a SOUP supplier the analogue is a declared position on what the platform will and will not treat as an
acceptable platform behaviour. The platform MUST declare exactly one such position and MUST declare it as an
engineering position, not a clinical one: **a platform behaviour that can produce a clinically-consumable output
which is wrong without being detectably wrong is not acceptable and MUST be controlled by design.** Every
SATISFIED row in §18.5.3.2 is an instance of that rule; HZ-13's PARTIAL is the one place the rule is currently
bent, and it is bent knowingly.

**MOS-CONF-226** 24971 §5.4 and Annex C treat "probability of occurrence" as often unknowable for software, and
recommend that where probability cannot be reasonably estimated, the manufacturer assumes the hazardous situation
occurs and controls on severity alone. The platform MUST adopt that convention for its own register
(`MOS-CONF-220`), and the register MUST say so explicitly so that a publisher does not mistake the absence of a
probability column for an oversight.

**MOS-CONF-227** 24971 guidance on SOUP (aligned with IEC 62304 §7) requires the publisher to track the SOUP's
**known anomalies**. The platform MUST therefore publish a machine-readable known-anomaly list per release —
open defects with a safety-relevant consequence, each mapped to the `HZ-` ids it bears on and to the release in
which it was found — and MUST include it in the release artifact set alongside the register. A SOUP supplier
that ships a register of controls but no list of its own open defects has published the flattering half.
This obligation is a new engineering requirement, and it is deliberate: it is the one thing this chapter asks
the project to build that it does not already have.

**MOS-CONF-228** 24971 §7 discusses risks arising from risk control measures. The four instances already
reasoned through in this specification (`MOS-DATA-074`, `MOS-DATA-076`, `MOS-EVID-092`, `MOS-EVID-139`) MUST be
carried into the register as explicit clause-7.5 entries rather than remaining as rationale prose, so that a
reviewer can see that the question was asked.

**MOS-CONF-229** 24971 Annex E treats security risk as a separate process that interfaces with, rather than
lives inside, the 14971 file. The platform MUST present its PHI and tenancy controls (HZ-08) under that
framing, and MUST NOT present them as ISO 14971 risk controls. Misfiling security controls as safety controls
inflates an apparent risk file and is a recognisable audit finding.

#### 18.5.5 Production and post-production information (clause 10) — what exists and what does not

**MOS-CONF-233** The platform's post-production information system consists of, and is limited to: the ten
monitoring signals of `MOS-EVID-137`, the signed 30-day `monitoring_period` reports of `MOS-EVID-135`, the
envelope decision counters of `MOS-EVID-104`, the `ResultReview` outcome record of `MOS-SAFE-071`, and the
audit and provenance records of `MOS-SAFE-070` and `MOS-SAFE-082`. It MUST be described in exactly those terms
and MUST NOT be described as post-market surveillance.

**MOS-CONF-234** The platform MUST NOT describe the `monitoring_period` report as a post-market surveillance
report or a PMS report, and MUST NOT allow the term "PMCF" to appear in any generated document. This extends
`MOS-SAFE-008` and `MOS-EVID-004` to the post-market vocabulary, for the same reason: a term of art used loosely
in a compliance document is a false claim.

**MOS-CONF-235** The platform MUST provide a complaint intake surface, because the publisher cannot build one
into a container that never sees a user. Minimum contract: a `Complaint` record carrying `{complaint_id,
tenant_id, service_version_id, result_id (nullable), reported_by, reported_at, category, narrative,
patient_harm_alleged (bool), site_contact}`, an API endpoint under `POST /api/v1/complaints` with a permission,
an `AuditEvent` on every state change, immutability of the narrative after submission, and per-publisher
retrieval scoped to that publisher's own `ServiceVersion`s. The platform MUST NOT triage, classify, assess or
close a complaint: that is the publisher's regulatory act. It provides the record and the routing, and nothing
else. **This is a GAP in Chapters 1–17 and is new work.**

**MOS-CONF-236** The platform MUST provide a machine-readable notification channel by which a publisher can
publish a field safety notice against a `ServiceVersion`, such that every tenant with a `Deployment` of that
version receives it and the receipt is recorded. The platform MUST NOT author, approve or transmit the notice's
content on the publisher's behalf. **GAP, new work.**

**MOS-CONF-237** The SITE MUST be told, at site acceptance, which of the ten monitoring signals it is expected
to respond to and by whom — the alert routing of `MOS-OPS-054` routes by owner, but no owner is defined for a
clinical-performance signal as opposed to an infrastructure one. A `review_disagreement_rate` alert routed to an
SRE on-call rotation is an alert nobody can act on. **GAP: `MOS-EVID-137`'s signals have thresholds and no named
clinical owner.**

**MOS-CONF-238** The PUBLISHER MUST define the `review_disagreement_rate` capability-declared bound that
`MOS-EVID-137` compares against. The specification requires the comparison and does not require the bound to be
declared anywhere in the `clinical` block or the `AcceptanceCriteria`. **GAP.**

**MOS-CONF-239** Where a monitoring hard breach suspends a `Deployment` (`MOS-EVID-139`), the platform MUST
record the suspension as a candidate post-production input to the publisher's risk file and MUST expose it
through the same retrieval path as complaints (`MOS-CONF-235`). A suspension that only exists as a deployment
state change is invisible to the process that is supposed to learn from it.

#### 18.5.6 IEC 62366-1:2015+AMD1:2020 — usability engineering

This is where the specification is weakest, and the weakness is structural rather than accidental. Chapters 1–17
specify, with great care, what a result *is*. They specify almost nothing about how it is *presented*. IEC
62366-1 is entirely about presentation, and it is the standard under which most AI-imaging use error is
analysed. There is no usability engineering file, no use specification in the 62366 sense, no user interface
specification, no use-related hazard analysis, no formative evaluation and no summative evaluation anywhere in
this specification. The words "use error", "usability engineering", "human factors", "formative", "summative"
and "instructions for use" do not occur in Chapters 1–17.

**MOS-CONF-241** The usability engineering process of IEC 62366-1 applies to the **device**, which is the
`ServiceVersion`, and is therefore PUBLISHER-owned. The platform is not exempt from contributing: it renders the
provenance panel, it writes the markers, and it defines the states (`REJECTED`, `UNREVIEWED`, `research_only`)
that a user must correctly interpret. Those are user interface elements of the device even though the platform
authors them, and §18.5.6.2 assigns them.

**MOS-CONF-242** — **AMENDED at specification 0.4.0.** Under IEC 62366-1 **clause 6**, a user interface the
manufacturer did not develop is a **User Interface of Unknown Provenance (UOUP)**, and the manufacturer must
evaluate it against the hazard-related use scenarios of the device. ~~MedicalOS's viewer surfaces, and OHIF
beneath them, are UOUP to the publisher in exactly the way MedicalOS is SOUP under 62304.~~ MedicalOS's
surfaces are UOUP **to the publisher** in exactly the way MedicalOS is SOUP under 62304, and who wrote them
does not change that: the publisher did not develop them either way. The platform MUST therefore publish,
alongside the SOUP declaration and the hazard register, a **UI contribution statement** enumerating every
user-visible element the platform renders or writes that bears on safe interpretation of a result: the
`MOS-SAFE-048`/`049`/`050` marker sets, the `MOS-SAFE-051` burned-in RUO banner, the `MOS-SAFE-012` adjacency
set, the `MOS-SAFE-088` provenance panel contents, the `REJECTED`-versus-`FAILED` visual distinction, the
`MOS-DATA-074` ambiguity flag, the `MOS-SAFE-062` `review_status` projection, and the `MOS-SAFE-016` "Training
population not declared by the publisher" string. **GAP, new work.** Without it, a publisher performing UOUP
evaluation has to reverse-engineer the platform's UI contract from source.

The struck sentence named OHIF as the layer beneath the platform's surfaces, and it was doing more work than
it looked like it was doing. Release 0.4.0 withdrew the OHIF deployment and the clinician surface is
first-party — `viewer/`, served at `/mos-viewer/`, held to the four guarantees of `MOS-UI-009a` — so the
sentence is false in its facts. It was also carrying an argument, and the argument is the part that changes:
clause 6 exists because a manufacturer cannot produce usability-engineering records for software it did not
write, and it stops being available to the platform for the surface the platform now writes. The obligation
gets larger, not smaller, and pretending otherwise would be the defect this chapter was written to find in
other people's conformance tables. That is `MOS-CONF-242a`. Register entry 106 in
`docs/spec/99-known-inconsistencies.md` records the amendment wave this belongs to.

**MOS-CONF-242a** A user interface the manufacturer **did** develop is not UOUP to that manufacturer, and
clause 6 does not apply to it. Two consequences follow and they point in opposite directions, so the
specification states both:

- **For the PUBLISHER, nothing is relieved.** MedicalOS remains a user interface the publisher did not develop
  and for which the publisher holds no usability-engineering records, which is what makes it UOUP under either
  limb of the definition. The publisher's clause-6 evaluation remains the publisher's obligation, and
  `MOS-CONF-242`'s UI contribution statement remains the thing that makes it performable without
  reverse-engineering. The platform MUST NOT read the first-party build as having discharged any part of it.
- **For the PLATFORM, the clinician surface leaves clause 6 and enters the clause 5 process.** The platform is
  the manufacturer of `viewer/` and MUST NOT describe it, file it, or evaluate it as UOUP.
  Four obligations follow, each checkable rather than asserted:
  1. The platform MUST record a **use specification** (clause 5.1) for the clinician surface in its own right —
     intended user, use environment, operating principle — and MUST NOT discharge it by citing `MOS-SAFE-013`
     or `MOS-SAFE-014`, which are the *publisher's* declarations about the *device*, not the platform's about
     its own surface.
  2. The platform MUST produce a **user interface specification** (clause 5.6) for the elements it renders.
     Row 5.6 of §18.5.6.1 already records that what exists is six scattered clauses and not a specification;
     that finding was written when those clauses described somebody else's viewer, and it now describes the
     platform's own code.
  3. **Formative and summative evaluation** (clauses 5.8 and 5.9) of the hazard-related use scenarios that run
     through the clinician surface are PLATFORM obligations for that surface. Both are a **GAP**. Nothing in
     this specification puts the first-party viewer in front of a clinician before release. `MOS-IMG-157a`
     asserts that an overlay renders at the expected slice indices; it is a rendering check, it was never a
     usability evaluation, it is pointed at a viewer the platform did not write and so does not observe the
     first-party surface at all, and the distinction matters more now that the platform wrote the renderer.
  4. The platform MUST NOT claim a clause-6 position for any surface it serves. As of release 0.4.0 it serves
     none it did not author: the clinician surface at `/mos-viewer/` is `viewer/`, and the extension
     surface still served at `/medicalos/` is `medos/web/ohif-extension/`, which is first-party code that now runs
     without the viewer it was written as a contribution to. The checkable form is a grep: a conformance
     statement naming a UOUP element the platform serves MUST name the third party that wrote it, and after
     0.4.0 there is no such party to name.

This is a net increase in the platform's obligation and is recorded as one. Under the withdrawn arrangement the
clinician surface carried IEC 62304 §8.1.2 SOUP obligations and a clause-6 evaluation; it now carries the §5
software lifecycle and the clause-5 usability process. That trade is not a discovery made after the fact — it
is written down as the counter-argument to the build decision in `docs/adr/BUILD_VS_ADOPT.md`, which states
that a first-party viewer is "a MedicalOS software item at the clinician surface's safety class, carrying the
full §5 lifecycle … where OHIF carried only §8.1.2 SOUP obligations." What this amendment adds is that the
usability half of that trade had not been named anywhere, and an unnamed obligation is not a scheduled one.

One thing this amendment has not verified, said here rather than left in the wording. "A user interface the
manufacturer did not develop" is this chapter's paraphrase of clause 6 and not the standard's definition, which
has two limbs and reaches an interface whose usability-engineering records are merely unavailable as well as
one somebody else wrote. Nothing above turns on which limb applies — the first-party surface satisfies the
second limb for the publisher today, which is why the publisher's obligation is unchanged — but the paraphrase
has never been checked against a controlled copy, and no requirement in this specification obliges anyone to
check it. `MOS-CONF-101` is the requirement that would be expected to, and it does not reach here: it binds
"the clause numbers and titles reproduced in §18.4" against controlled copies of IEC 62304 and IEC 82304-1,
and the 62366-1 material is §18.5.6 under neither standard. `MOS-CONF-111` inherits the same §18.4 scope. So
the clause numbers, titles and applicability statements of §18.5.6 — including the clause-5-versus-clause-6
boundary this amendment turns on — rest on an unchecked paraphrase. **GAP, new work:** `MOS-CONF-101`'s
obligation MUST be extended to §18.5.6 and to a controlled copy of IEC 62366-1:2015+AMD1:2020 before this
chapter is issued to a notified body, an FDA reviewer or a customer's clinical safety officer. This amendment
MUST NOT be read as having performed that check or as having created the requirement to.

##### 18.5.6.1 Clause mapping

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 5.1 | **Use specification**: intended medical indication, patient population, part of the body or type of tissue, user profile, use environment, operating principle | PUBLISHER | `MOS-SAFE-013`/`MOS-SAFE-014` supply, as machine-checked required fields: `intended_use.statement`, `intended_user` (closed enum), `intended_setting[]` (closed enum), `reading_paradigm` (`triage`/`first_reader`/`concurrent_read`/`second_reader`/`post_read_qa`), `indications[]` with SNOMED/RadLex/DCM/LOINC/ICD-10 codes, `contraindications[]` where an empty list is an explicit positive assertion, `target_population` with age bounds, sex, body part and pregnancy state, `input_constraints` as the human twin of the `SeriesSelector`, and `output_kinds[]` | **PARTIAL, and unusually strong for a platform.** `reading_paradigm` in particular is the single most safety-relevant use-specification field for AI imaging and most manifests in this market do not carry it. Missing: **use environment** beyond a care-setting enum (no display class, no ambient lighting, no reading-room versus mobile versus tele-reading distinction), **user profile** beyond a role enum (no training, no experience, no frequency of use, no language), and **operating principle** (no statement of what the model does in terms a user could form a mental model from) |
| 5.2 | Identification of user interface characteristics related to safety and potential use errors | PUBLISHER + PLATFORM (its contribution) | — | **GAP.** No requirement in Chapters 1–17 identifies a UI characteristic as safety-related. `MOS-SAFE-012` mandates *content adjacency* without analysing why those five fields and not others |
| 5.3 | Identification of known and foreseeable hazards and hazardous situations **related to use** | PUBLISHER + PLATFORM | §18.5.3 HZ-09 and HZ-13 are use-related; the register is otherwise a technical-hazard register | **PARTIAL.** Two of seventeen entries are use-related, and both are PARTIAL. A real use-related hazard analysis would produce more: misreading `UNREVIEWED` as reviewed, misreading a research-mode object as clinical, misreading `REJECTED` as a system error and re-submitting, misreading an absent overlay as a negative finding, misreading an ambiguity flag as unimportant, misreading a threshold-dependent measurement as threshold-independent |
| 5.4 | **Hazard-related use scenarios** derived from 5.3 | PUBLISHER | — | **GAP** |
| 5.5 | Selection of the hazard-related use scenarios to be taken into summative evaluation | PUBLISHER | — | **GAP** |
| 5.6 | **User interface specification** — the normative description of the UI that implements the use specification | PUBLISHER (device) + PLATFORM (its own surfaces) | Fragments only: `MOS-SAFE-012` (five fields, adjacent, no interaction required), `MOS-SAFE-088` (provenance panel contents), `MOS-SAFE-051` (a burned-in banner occupying the top 40 rows of a research-mode SC frame, opaque background, fixed text), `MOS-SAFE-016` (an exact string for an undeclared training population), `MOS-EVID-102` + spine §4 (`REJECTED` visually distinct from `FAILED` in every UI), `MOS-DATA-074` (the ambiguity flag is mandatory in the provenance panel and the SR) | **PARTIAL.** These are genuine UI requirements, and they are the right ones — but they are six scattered clauses, not a specification. None states layout, prominence, contrast, ordering, persistence, what happens when the panel is collapsed, or what a viewer that cannot render them must do |
| 5.7 | User interface evaluation plan | PUBLISHER | — | **GAP** |
| 5.8 | **Formative evaluation** during development | PUBLISHER | — | **GAP.** No requirement in this specification puts any interface in front of a clinician before release. `MOS-IMG-157a`'s headless-browser check asserts that the overlay renders at the expected slices and the SR panel hydrates; that is a rendering test, not a formative evaluation, and `MOS-IMG-157a` itself says "a human looked at it and it displayed" is not a check. (Cited as `MOS-IMG-157` before specification 0.4.0, which struck that text and moved the obligation) — which is correct about rendering and says nothing about comprehension |
| 5.9 | **Summative evaluation** of the hazard-related use scenarios with representative users | PUBLISHER | — | **GAP, and the single largest gap in this chapter.** A summative evaluation is the artifact a notified body asks for by name, it requires representative users and a validated protocol, it cannot be retrofitted from telemetry, and nothing in this specification produces or requires one |
| 6 — **AMENDED at specification 0.4.0** | **UOUP**: evaluation of a user interface the manufacturer did not develop | PUBLISHER (performs) + PLATFORM (enables) — and, for the platform's own clinician surface, **no longer this clause at all**: see `MOS-CONF-242a` | `MOS-CONF-242` as amended (UI contribution statement, new work) and `MOS-CONF-242a` (the first-party clinician surface is not UOUP to the platform and moves to clause 5); ~~`MOS-IMG-157` (pinned-OHIF rendering check, release-blocking at 0.1.0)~~ `MOS-IMG-157a` (pinned-viewer rendering check, release-blocking at 0.1.0, drawing its renderer from the `MOS-REL-027` incumbent list from 0.4.0; `MOS-IMG-157`'s text is struck and this is the id that carries it); `MOS-IMG-158` (independent third-party viewer verification deferred to 0.4.0, manual, screenshots into the release record); `MOS-TEST-068` (the viewer under test MUST be pinned by digest) | **PARTIAL, and at 0.4.0 weaker in one respect than it was at 0.3.0.** Pinning the viewer by digest is exactly the right primitive for UOUP and most integrators do not do it — against a third-party viewer. Against the platform's own build it is not provenance evidence and proves nothing a commit does not already prove, so the primitive now applies only to the renderers of `MOS-IMG-157` and `MOS-IMG-158`. The evaluation that pinning enables is still not specified, and third-party viewer behaviour — the common case in a hospital — is still deferred and manual. What was one obligation in one clause is now two in two: clause 6 for the publisher, with a mechanism; clause 5 for the platform's own surface, with none yet |
| 7 | Accompanying documentation / instructions for use | PUBLISHER | `MOS-SAFE-022` (`GET /service-versions/{id}/clinical` returns the block verbatim with its digest, tenant-scoped, cacheable) is the machine-readable IFU carrier; `MOS-SAFE-014` supplies its content; `MOS-SAFE-021` makes it tamper-evident | **PARTIAL.** A structured, signed, digest-pinned intended-use block is a better IFU substrate than a PDF. It is not an IFU: nothing requires it to be rendered to a user in readable form at the point of use, and `MOS-SAFE-012` requires only five of its fields to be shown adjacent to a finding — `contraindications`, `known_limitations`, `not_validated_for` and `known_failure_modes` are not among them |

##### 18.5.6.2 Platform obligations that follow

**MOS-CONF-250** The platform MUST publish the UI contribution statement of `MOS-CONF-242` as a versioned
artifact and MUST version it independently of the code, so that a publisher's UOUP evaluation can cite a
specific revision.

**MOS-CONF-251** The platform MUST specify, for each element in the UI contribution statement, the **degradation
behaviour** when a consuming viewer cannot render it: whether the element is present in DICOM metadata and
therefore survives into any conformant viewer (`MOS-SAFE-047`, `MOS-SAFE-048`), present only in the MedicalOS
surface, or present only as burned-in pixels (`MOS-SAFE-051`). A publisher performing a use-related hazard
analysis needs to know which of its safety messages survive the hospital's own viewer, and today that requires
reading three chapters.

**MOS-CONF-252** The platform SHOULD extend `MOS-SAFE-012`'s adjacency set to include a count and an
affordance for `known_limitations[]`, `not_validated_for[]` and `contraindications[]` — not the full text, which
would be unreadable beside a finding, but the fact that they exist and a one-interaction path to them. Declaring
limitations into an API endpoint no clinician will ever call satisfies the letter of disclosure and none of its
purpose. This is SHOULD rather than MUST because the right affordance is a usability question and MUST-ing a
design before a formative evaluation is precisely the error this section is written to prevent.

**MOS-CONF-253** The platform MUST NOT add, remove or restyle any element of the UI contribution statement in a
patch or minor release without recording the change against the statement's version, because a publisher's
completed UOUP evaluation is invalidated by it. This is the UI analogue of `MOS-REG-096`'s version-bump rule.

**MOS-CONF-254** The `RESEARCH USE ONLY` banner of `MOS-SAFE-051` occupies the top 40 rows of an SC frame on an
opaque background. That is a usability decision — a specific size, position and contrast — taken with no
formative evaluation behind it, and it is defeatable by cropping, by a viewer that scales to fit, and by any
workflow that exports a sub-region. The platform MUST record it in the UI contribution statement as an element
whose effectiveness is **unevaluated**, and MUST NOT present it as a verified risk control. It is a good default;
it is not evidence.

**MOS-CONF-255** The SITE MUST verify, during site acceptance, that its own clinical viewer renders the
AI-derived marker set and the `clinical_use_mode` of a generated object, and MUST record the result. The
platform MUST add this as a named site-acceptance step alongside SAT-1..SAT-5 (`MOS-EVID-132`,
`MOS-EVID-134`), and a failure MUST block clinical promotion in the same way any other SAT failure does.
**GAP, new work — and it is the control that closes HZ-09's residual.**

**MOS-CONF-256** The SITE MUST define, and record at acceptance, who is responsible for reviewing `REJECTED`
jobs carrying `no_eligible_series` (`MOS-DATA-075`) and at what cadence. **GAP — this is the control that
closes HZ-13's residual, and it is a site process control, not a platform feature.**

**MOS-CONF-257** The PUBLISHER MUST declare `foreseeable_misuse[]` in the `clinical` block, with the same shape
and the same fail-closed discipline as `not_validated_for[]` (`MOS-SAFE-015`): at least one entry, each
`{id, text, mitigation}`, ids stable and provenance-referenced (`MOS-SAFE-017`). Candidate entries this system
makes foreseeable and which a publisher should be expected to have considered: running a `second_reader` service
as a de facto first read because the overlay arrives before the radiologist opens the study; treating an absent
overlay as a negative finding; reading a `research_only` object in the clinical path because it appeared in the
worklist; accepting a `ResultReview` without opening the images; using a measurement across reconstruction
kernels for which it is not comparable. **GAP — the field does not exist.**

**MOS-CONF-258** The PUBLISHER MUST, for each hazard-related use scenario selected under 62366-1 §5.5, name the
platform control it relies on by requirement id. Where the publisher's mitigation for a use-related hazard is
"the platform displays X", that reliance MUST be explicit, because `MOS-SAFE-012` binds only MedicalOS-controlled
surfaces and a publisher relying on it in a third-party viewer is relying on something that is not true.

**MOS-CONF-259** The platform MUST NOT claim, in any document, that MedicalOS supports, provides or contributes
to a usability engineering file, until a use specification, a user interface specification and at least a
formative evaluation exist for the surfaces it owns. Until then the accurate statement is: *"MedicalOS provides
specified, tested and versioned user interface elements, and publishes their contract. It has performed no
usability engineering process under IEC 62366-1."*

#### 18.5.7 Certification backlog arising from this section

The ordering below is the project plan, not a list of complaints. It is ordered by what unblocks what.

| # | Item | Standard | Owner | Unblocks |
|---|---|---|---|---|
| 1 | Adopt ISO 14971 for the platform-as-SOUP scope; resolve `MOS-OPEN-025` | 14971 cl. 4.1 | PLATFORM | everything below |
| 2 | Severity scale, and the 24971 §C convention of assuming occurrence where probability is unknowable | 14971 cl. 5.5 | PLATFORM + PUBLISHER | 3, 4 |
| 3 | Risk acceptability policy and criteria | 14971 cl. 4.2, 6 | PUBLISHER | 4, 5 |
| 4 | Residual risk evaluation per hazard; overall residual risk; benefit-risk where needed | 14971 cl. 7.3, 7.4, 8 | PUBLISHER | 5 |
| 5 | Risk management plan, file, report and review | 14971 cl. 4.4, 4.5, 9 | PUBLISHER | certification |
| 6 | `docs/RISK-REGISTER.yaml` + CI citation check | 14971 cl. 5.4 | PLATFORM | publisher's 5.4, SOUP file |
| 7 | Known-anomaly list per release | 24971 / 62304 cl. 7 | PLATFORM | publisher's SOUP analysis |
| 8 | `foreseeable_misuse[]` in the `clinical` block | 14971 cl. 5.2 | PLATFORM (field) + PUBLISHER (content) | 62366 §5.3, §5.4 |
| 9 | Envelope attributes for `patient_sex` and pregnancy state, closing `MOS-SAFE-014` → `MOS-EVID-103` | 14971 cl. 7.1 | PLATFORM | HZ-05 residual |
| 10 | Complaint record and intake endpoint | 14971 cl. 10.2 | PLATFORM | publisher's PMS |
| 11 | Field safety notice channel with recorded receipt | 14971 cl. 10.3 | PLATFORM | publisher's vigilance |
| 12 | Named clinical owner and response path per monitoring signal | 14971 cl. 10.3 | SITE + PLATFORM | HZ-14 residual |
| 13 | UI contribution statement with degradation behaviour | 62366-1 cl. 6 | PLATFORM | publisher's UOUP evaluation |
| 14 | Use specification completion: use environment, user profile, operating principle | 62366-1 cl. 5.1 | PUBLISHER | 62366 §5.2–5.6 |
| 15 | Use-related hazard analysis and hazard-related use scenarios | 62366-1 cl. 5.2–5.5 | PUBLISHER | 16, 17 |
| 16 | User interface specification for the publisher's own surfaces | 62366-1 cl. 5.6 | PUBLISHER | 17 |
| 17 | Formative evaluation, then summative evaluation with representative users | 62366-1 cl. 5.8, 5.9 | PUBLISHER | certification |
| 18 | Site-acceptance step: viewer renders the AI-derived marker set | 62366-1 cl. 6 | SITE | HZ-09 residual |
| 19 | Site-acceptance step: named owner and cadence for `REJECTED` review | 14971 cl. 7.1 | SITE | HZ-13 residual |

Items 6, 7, 8, 9, 10, 11, 13, 18 and 19 are platform work and are small. Items 3, 4, 5, 15, 16 and 17 are
publisher work and are not small. Item 17 is the one that cannot be started late.

### 18.6 ISO 13485, ISO/IEC 27001:2022 and ISO/IEC 42001:2023

#### 18.6.1 What a management-system standard can and cannot say about software

All three standards in this section certify an **organisation**, not a program. ISO 13485 certifies a quality management system, ISO/IEC 27001 an information security management system, ISO/IEC 42001 an AI management system. None of the three has a conformity assessment route for a source tree, a container image or a specification document. A vendor whose datasheet says its software "is ISO 27001 certified" is either describing its hosting provider's certificate or misrepresenting one.

What software *can* do is two things: **supply records** into somebody else's management system, and **implement** the subset of controls that are technical rather than procedural. That is the whole claim of §18.6, and it is the claim the specification already makes for three other regimes in `MOS-SEC-007` — MedicalOS "MUST NOT be represented as providing HIPAA, GDPR or MDR *compliance*. It provides controls a covered entity can use inside its own compliance programme."

**MOS-CONF-300** MedicalOS MUST NOT be represented as certified, certifiable, or conformant to ISO 13485, ISO/IEC 27001 or ISO/IEC 42001. It MAY be represented as supplying named records into, and implementing named technical controls of, a certified organisation's management system. The CI string ban of `MOS-SAFE-008` MUST be extended to the strings `ISO 13485 certified`, `ISO 27001 certified`, `ISO 42001 certified`, `QMS certified`, `ISMS certified` and `AIMS certified`, in every documentation file, UI string, API field and marketing asset.

**MOS-CONF-301** Every clause and control in §18.6 MUST carry exactly one owner drawn from {PLATFORM, PUBLISHER, SITE}, assigned consistently with the responsibility table of `MOS-SAFE-004`. A clause for which no owner can be named is a gap and MUST appear in §18.6.6. An owner of SITE or PUBLISHER is not a gap; it is a correctly located obligation.

**MOS-CONF-302** When a publisher's `ServiceVersion` runs on MedicalOS, MedicalOS is SOUP in that publisher's IEC 62304 software file. The platform MUST therefore publish, per release, a machine-readable SOUP identity block containing `product`, `version`, `image_digests[]`, `sbom_ref` (`MOS-SEC-142`), `intended_platform_function`, `functional_and_performance_requirements_ref` and `published_anomalies_ref`. The last member cannot be populated today and is a gap (§18.6.6); it MUST NOT be filled by pointing at `99-known-inconsistencies.md`, which is a non-normative register of defects in *this document* and not a list of anomalies in *released software*. Publishing a specification defect register as a SOUP anomaly list would be the kind of false mapping that fails an audit.

---

#### 18.6.2 ISO 13485:2016

##### 18.6.2.1 What the platform actually contributes to a publisher's QMS

A QMS is a set of documented processes, records and responsibilities held by a legal entity. Nothing in chapters 1–17 creates one, and nothing should. What the platform contributes is a set of **records** a publisher's QMS can cite without re-deriving them, and a set of **gates** that make certain process failures mechanically impossible rather than procedurally discouraged. That distinction is the platform's real value to a 13485 auditor: a gate that cannot be bypassed is stronger evidence than a signed-off procedure that says the same thing.

| QMS input the platform supplies | Mechanism | Requirement IDs | Clauses served |
|---|---|---|---|
| Design history of an artifact | `EvaluationRun` binds every input that can change a number; per-case metrics persisted, aggregates recomputable | `MOS-EVID-061`, `MOS-EVID-062`, `MOS-EVID-065`, `MOS-EVID-066` | 7.3.6, 7.3.10 |
| Design review record | Three human acts, three permissions, three audit rows; no machine principal may hold them | `MOS-TRAIN-173`, `MOS-TRAIN-174`, `MOS-TRAIN-175` | 7.3.5 |
| Design change control | Artifact immutability, enforced semver bump rules, gate re-opening on any material change | `MOS-REG-018`, `MOS-REG-019`, `MOS-REG-095`, `MOS-REG-096`, `MOS-EVID-094` | 7.3.9 |
| Device description and intended use | Mandatory `clinical:` block, digested at registration | `MOS-SAFE-013`, `MOS-SAFE-014`, `MOS-SAFE-021` | 4.2.3, 7.3.3 |
| Traceability from record to patient-visible object | Seven joins that MUST resolve, walked by CI for one completed job | `MOS-SEC-155`, `MOS-SAFE-082`, `MOS-SAFE-083` | 7.5.9 |
| Records protection | Hash-chained audit with signed Merkle checkpoints; purge exclusions | `MOS-SEC-151`, `MOS-SEC-152`, `MOS-SEC-153`, `MOS-STORE-337` | 4.2.5 |
| Field-action population query | Recall impact endpoints; one-statement recall query | `MOS-REG-040`, `MOS-SAFE-032`, `MOS-STORE-280` | 8.3 |
| Transfer-to-production record | Fixed install flow; five-step site acceptance test, signed | `MOS-REG-094`, `MOS-EVID-129`, `MOS-EVID-133` | 7.3.8 |

**MOS-CONF-303** The platform MUST be able to export, for one `(ServiceVersion, tenant)` pair, a single **QMS evidence pack** containing: the `clinical` block and its `intended_use_digest`; every `EvaluationRun` binding and per-case metric set behind the version's claims; every `ValidationReport` of all three kinds of `MOS-EVID-127`; the approval dossier of `MOS-TRAIN-176`; the registry lifecycle history; the deployment-gate decisions of `MOS-EVID-091`; and the `AuditEvent` range covering them with its checkpoint signatures. Each member MUST already be signed or hash-chained by its owning chapter; the pack MUST introduce no new trust root and MUST NOT re-sign anything.

**MOS-CONF-304** The QMS evidence pack MUST verify offline under `medicalos-verify` (`MOS-EVID-122`, `MOS-EVID-123`, `MOS-EVID-124`) with no network access and no running MedicalOS instance. A notified-body reviewer or an FDA reviewer does not get a login, and evidence that requires one is not evidence.

**MOS-CONF-305** The platform MUST generate a **traceability matrix** from requirement ID → test name → source location over its own requirement IDs, as the mechanical form of `MOS-REL-098`. It MUST be regenerated in CI, MUST fail the build on an orphan ID, and MUST be a member of every release's published artifact set. This is the single cheapest 62304-shaped artifact available to this project, because the ID scheme already exists — the point `MOS-OPEN-025` makes.

##### 18.6.2.2 Clause map

| Clause | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| **4.1.6** Validation of software used in the QMS | Documented validation of computer software used in the QMS, proportionate to risk, revalidated on change | PUBLISHER, SITE | `MOS-EVID-122`, `MOS-EVID-123`, `MOS-EVID-124` (self-contained, offline, network-refusing verification); `MOS-SAFE-092` (`medicalos-reproduce` rebuilds a run from the provenance record alone); `MOS-SAFE-093`; the AT-01…AT-24 battery of ch 14; `MOS-REL-123` | PARTIAL — the objective evidence a tool validation would cite exists, but the platform ships no installation/operational qualification script a publisher can run at its own site and retain. `MOS-EVID-129`'s SAT suite qualifies a `ServiceVersion`, not the platform install. |
| **4.2.1–4.2.2** Documentation, quality manual | Quality manual, documented procedures, scope and exclusions | PUBLISHER | — | NOT-APPLICABLE — a quality manual is a property of an organisation. The platform neither holds one nor should. |
| **4.2.3** Medical device file | One file per device type: description, specification, labelling and IFU, manufacturing, measuring, installation, servicing | PUBLISHER | `MOS-SAFE-013`, `MOS-SAFE-014` (mandatory `clinical:` block, rejected at registration if absent), `MOS-SAFE-015`, `MOS-SAFE-020` (`legal_manufacturer` identity), `MOS-SAFE-023`, `MOS-SAFE-024` (`regulatory_status`, one entry per jurisdiction), `MOS-SAFE-021` (`intended_use_digest`), `MOS-TRAIN-176` (approval dossier), `MOS-EVID-122` (report bundle), `MOS-REL-095` (`docs/dicom/CONFORMANCE.md`) | PARTIAL — device description, specification and performance evidence are held, digest-bound and exportable. There is no risk-management file, no clinical evaluation report, no labelling or IFU artifact, and no servicing record anywhere in chapters 1–17. |
| **4.2.4** Control of documents | Review, approve and re-approve documents; identify changes and current revision status; prevent unintended use of obsolete documents | PLATFORM (artifact-borne), PUBLISHER (device) | `MOS-REG-017` (the registry computes `content_digest` and MUST NOT accept one), `MOS-REG-018` (manifest, digest, version and `oci_ref` immutable after insert, enforced by `REVOKE UPDATE`), `MOS-REG-019` (`409` on republish with different content), `MOS-REG-021` (`DEPRECATED`, `SUSPENDED`, `RECALLED` as revision status), `MOS-REL-095` (document set with per-document CI staleness checks) | PARTIAL — every document that is an artifact is controlled well above the clause's bar. Prose documents are controlled only by Git history; no requirement states who approves a revision or how an obsolete revision is withdrawn. |
| **4.2.5** Control of records | Legible, identifiable, retrievable; defined retention; protected from loss and unauthorised alteration | PLATFORM (mechanism), SITE (retention decision) | `MOS-SEC-146`–`MOS-SEC-150` (AuditEvent schema, same-transaction insert, paired events across boundaries), `MOS-SEC-151`–`MOS-SEC-154` (RFC 8785 hash chain, signed Merkle checkpoints, `audit.verify` with a CI tamper test, partition export rather than drop), `MOS-SEC-126` (retention per data class), `MOS-EVID-013` (sealing enforced by `REVOKE UPDATE`, not application code), `MOS-SAFE-090`, `MOS-SAFE-091` (append-only provenance with daily chain verification), `MOS-STORE-337` (purge MUST NOT delete audit, provenance, validation or evaluation rows) | SATISFIED |
| **7.3.2** Design and development planning | Plan and control design; define stages, review/verification/validation activities, responsibilities and how the plan is updated | PUBLISHER (device), PLATFORM (its own lifecycle) | `MOS-REL-116`, `MOS-REL-117` (change classes and the gate matrix) constitute a per-change plan; `MOS-TRAIN-190` places pipeline stages into releases | GAP — for the platform itself there is no design and development plan, no defined stages and no planned review points. `MOS-OPEN-025` records adoption of 62304-shaped lifecycle artifacts as undecided, gated at `G-PILOT`. |
| **7.3.3** Design and development inputs | Functional, performance, usability and safety requirements; applicable regulatory requirements; risk-management outputs | PUBLISHER | `MOS-SAFE-014` (intended use, indications, contraindications, target population, known limitations, input constraints), `MOS-SAFE-015` (`not_validated_for[]` and `known_failure_modes[]` MUST each carry at least one entry), `MOS-SAFE-019` (IMDRF category derived from two axes; a disagreeing manifest is rejected), `MOS-EVID-076`–`MOS-EVID-079` (AcceptanceCriteria as a declarative document validated at write time), `MOS-EVID-095`–`MOS-EVID-099` (ApplicabilityEnvelope; bounds derivable from the cohort and narrowable only) | PARTIAL — functional, performance and safety inputs are captured and machine-checked, and regulatory inputs are captured per jurisdiction. **Usability requirements are absent from chapters 1–17 entirely**: there is no IEC 62366 input, no user-interface specification and no use-error analysis. Risk-management outputs have no home either. |
| **7.3.4** Design and development outputs | Meet the inputs; provide information for purchasing, production and service; contain or reference acceptance criteria; identify characteristics essential for safe use | PUBLISHER | `MOS-REG-084` (the signed unit is an OCI manifest whose config blob is the artifact manifest), `MOS-REG-086` (the PreprocessingSpec blob's digest equals the registry row's `content_digest`), `MOS-REG-087` (SBOM, attestations and the ValidationReport attached via the OCI Referrers API), `MOS-TRAIN-129` (MONAI Bundle is the source form, the signed OCI artifact the registered form), `MOS-EVID-076` (criteria are a document, never an expression string) | SATISFIED |
| **7.3.5** Design and development review | Systematic review at suitable stages, with representatives of the functions concerned; records | PUBLISHER | `MOS-TRAIN-173` (three distinct human decisions, three permissions, three audit records, never collapsed), `MOS-TRAIN-174` (no service account, API key, CI identity or scheduled task may hold `evidence.report.issue`, `artifact.approve`, `deployment.approve_clinical`, `deployment.promote` or `deployment.gate.override` — asserted as a SQL query in CI), `MOS-TRAIN-175` (three timestamps even when one person exercises all three), `MOS-TRAIN-176`–`MOS-TRAIN-178` (generated, offline-renderable dossier in which every number links to its `evaluation_run_id`) | SATISFIED for the promotion review. No earlier-stage review exists, because the platform has no visibility into a publisher's earlier stages and claims none. |
| **7.3.6** Design and development verification | Verify that outputs meet inputs; record methods, acceptance criteria and, where appropriate, statistical technique with rationale for sample size | PLATFORM (its own contracts), PUBLISHER (the model) | PLATFORM: `MOS-IMG-054` (worker startup self-test against the golden fixture; refuse to serve on mismatch), `MOS-TRAIN-060`, `MOS-TRAIN-062` (record the golden fixture last before signing; re-run on a clean checkout in CI), `MOS-OPS-078` (bit-identical output asserted across every allowed `patch_batch_size`), `MOS-REL-117` (C3 gates: DICOM battery plus an evidence re-run reporting per-case deltas). PUBLISHER: `MOS-EVID-061` (the run binds every input that can change a number), `MOS-EVID-062` (`code_dirty = true` blocks `SUCCEEDED`), `MOS-EVID-056`–`MOS-EVID-060` (mandatory CI companion, patient-level cluster bootstrap, declared method), `MOS-EVID-077` (gate on `ci_lower_95`, not a point estimate) | SATISFIED — including the statistical-rationale limb, which most software specifications omit. |
| **7.3.7** Design and development validation | Validate on representative product under defined operating conditions; clinical evaluation where required | PUBLISHER, exclusively | `MOS-EVID-001` (the platform MUST NOT perform or confer clinical validation), `MOS-SAFE-008` (the vocabulary is banned and CI enforces the ban), `MOS-EVID-117` (a named, identifiable human approver with a role, an organisation, a written statement and an identity assurance level), `MOS-EVID-118`, `MOS-EVID-121` (DSSE-signed; an unsigned report MUST NOT be accepted) | NOT-APPLICABLE to PLATFORM by construction. The platform's role is confined to recording, binding and signing the publisher's act — which is the correct scope for infrastructure. |
| **7.3.8** Design and development transfer | Verify that design outputs are suitable for production before becoming final specifications; records | PUBLISHER → SITE | `MOS-REG-094` (fixed install flow: fetch → verify signature → verify SBOM and attestations → validate manifest against its JSON Schema → compatibility check), `MOS-EVID-127` (all three evidence kinds are one row type with one verification procedure), `MOS-EVID-128` (a site report MUST cite the vendor report by digest and fails to issue without it), `MOS-EVID-129` (the five-step SAT), `MOS-EVID-130` (SAT-2 compares against the publisher's recorded fixture values, not against "does it run"), `MOS-EVID-133` (one operator action; no code, no YAML, no vendor data), `MOS-EVID-134` (a failed step leaves the deployment blocked) | SATISFIED |
| **7.3.9** Control of design and development changes | Identify, review, verify, validate and approve changes before implementation; evaluate the effect on product already delivered | PUBLISHER (decision), PLATFORM (enforcement) | `MOS-REG-018` (immutability after insert), `MOS-REG-019` (`409` on divergent republish), `MOS-REG-095`, `MOS-REG-096` (normative semver bump rules; a publish declaring a smaller bump than the manifest diff requires is rejected), `MOS-EVID-094` (a change to the version, the PreprocessingSpec, any operating threshold, the criteria or the cohort re-opens the gate), `MOS-EVID-064` (a backend conversion is a different numerical artifact and requires its own run), `MOS-REG-102`, `MOS-REG-103` (converted versions carry their own `evaluation_run_id` and re-pass the criteria), `MOS-SAFE-031` (the `clinical` block is immutable once any Job has referenced the version), `MOS-REL-116`–`MOS-REL-120` (change classes, gate matrix, no relabelling to a lighter class, no silently disabled gate) | SATISFIED — the strongest ISO 13485 clause in the specification, and the one to show a reviewer first. "Evaluate the effect on product already delivered" is discharged by `MOS-REG-040` and `MOS-SAFE-032`. |
| **7.3.10** Design and development files | Maintain a file per device type or family demonstrating conformity to 7.3 | PUBLISHER | `MOS-EVID-061` (complete binding), `MOS-EVID-065`, `MOS-EVID-066` (per-case metrics persisted; aggregates recomputable to 1e-9 relative), `MOS-TRAIN-176` (dossier), `MOS-SAFE-082`, `MOS-SAFE-083` (one provenance record per Result, in the same transaction as the Result and the terminal Job transition), `MOS-SEC-155` (the join table a CI test walks end to end), `MOS-CONF-303` | PARTIAL — a design history for the *artifact* is complete and machine-reconstructable. A design history for the *platform* is not assembled into a file; `MOS-CONF-305` creates its traceability limb and `MOS-OPEN-025` owns the rest. |
| **7.5.6** Validation of processes for production and service provision | Validate processes whose resulting output cannot be verified by subsequent monitoring or measurement; revalidate on change | PLATFORM (DICOM production), PUBLISHER (inference) | `MOS-IMG-054` (startup self-test; refuse to serve on golden-fixture mismatch), `MOS-IMG-062` (every minted UID derived by one function; random UIDs forbidden), `MOS-IMG-070` (QIDO-RS pre-check before re-inference, so retries resume rather than re-run), `MOS-TRAIN-042` (CI asserts transform identity on a synthetic volume with a known affine), `MOS-TRAIN-062` (clean-checkout reproduction before signing), `MOS-EVID-130` (SAT-2 revalidation at the site), `MOS-OPS-078` (the batching knob MUST NOT change outputs) | SATISFIED |
| **7.5.8** Identification | Identify product throughout realisation; identify status with respect to monitoring and measurement | PLATFORM | `MOS-REG-017`, `MOS-REG-018` (content-addressed, immutable artifact identity), `MOS-IMG-062`–`MOS-IMG-067` (deterministic UID derivation under a declared org root), `MOS-SAFE-047` (every generated object identifiable as AI-derived, and its `clinical_use_mode` determinable, from a QIDO-RS response alone), `MOS-SAFE-048`, `MOS-SAFE-049`, `MOS-SAFE-050` (base, per-object-type and RUO marker sets), `MOS-SAFE-046` (deployment environment, deployment id and `clinical_use_mode` snapshotted onto the Result), `MOS-REG-020`, `MOS-REG-021` (lifecycle status *is* the monitoring-and-measurement status) | SATISFIED |
| **7.5.9** Traceability | Establish and document traceability; records permitting identification of components and of the distribution of each device | PLATFORM | `MOS-SEC-155` (seven joins that MUST all resolve, walked by CI for one completed job), `MOS-SAFE-082`–`MOS-SAFE-089` (provenance record, rejected series with reasons, signed offline-verifiable export), `MOS-SAFE-090` (append-only at the database role level, hash-chained), `MOS-REG-040` (`/impact` endpoints returning affected `job_id` and `result_id`), `MOS-SAFE-032` (`results-produced` per version), `MOS-STORE-280` (the recall query answerable in one statement), `MOS-EVID-072` (every displayed performance number renders from a claim row linked to its `EvaluationRun`) | SATISFIED — the strongest clause mapped anywhere in this chapter. |
| **8.2.1** Feedback | Gather and monitor information from production and post-production activity as an input to risk management and to the QMS | PUBLISHER (obligation), PLATFORM (signals) | `MOS-SAFE-071` (aggregate `ResultReview` outcome rates per service and version), `MOS-EVID-135` (signed `monitoring_period` report per 30-day window per tenant/capability/version), `MOS-EVID-137` (the mandatory signal set), `MOS-EVID-104` (envelope decisions counted per reason code) | PARTIAL — the platform emits automatic telemetry, which is not the same thing as feedback. No channel exists by which a human's report of a problem enters the system, no feedback record exists, and nothing routes a signal to the publisher. `MOS-SAFE-072` forbids presenting the review aggregate as a performance metric and `MOS-EVID-142` forbids a monitoring report carrying a `PASS` verdict, so neither may be offered as discharging 8.2.1 on its own. |
| **8.2.2** Complaint handling | Documented procedures for timely receipt, review, evaluation, investigation and closure of complaints; records; justification where no investigation is performed | PUBLISHER | — | **GAP** — there is no `Complaint` entity, no intake endpoint, no investigation state machine, no closure record, and no link from a complaint to the `Result`, `Job` or `ServiceVersion` that occasioned it. |
| **8.2.3** Reporting to regulatory authorities | Notify authorities of complaints meeting reportability criteria; retain records | PUBLISHER | `MOS-REG-040` and `MOS-SAFE-032` supply the affected-population query such a report would cite; `MOS-SAFE-028` already puts an immutable `jurisdiction` on every Deployment | **GAP** — nothing consumes them. There is no vigilance record, no reportability decision, no clock and no jurisdiction-specific timeline. |
| **8.3** Control of nonconforming product | Identify and control nonconforming product; define responsibility for review and disposition; record the nonconformity and the action taken; act on product detected after delivery | PLATFORM (artifact level), PUBLISHER (disposition) | `MOS-REG-021` (`SUSPENDED` reversible, `RECALLED` irreversible), `MOS-REG-022` (`409` on any transition out of `RECALLED`), `MOS-REG-091` (a daily rescan auto-suspends and MUST NOT auto-recall), `MOS-REG-109` (`artifact.recall` separately grantable from routine status setting), `MOS-SEC-145` (a digest that later fails verification suspends the deployment and emits an audit event and a webhook), `MOS-EVID-014` (a sealed object may be marked `DEFECTIVE`, never edited), `MOS-EVID-139`, `MOS-EVID-140` (a hard monitoring breach suspends; recovery needs a human rationale and re-running SAT-1…SAT-3), `MOS-SEC-035` (a `Result` is never deletable; `superseded_by`), `MOS-SAFE-031` | PARTIAL — identification, containment and enumeration are strong. **Disposition is not recorded**: `MOS-REG-040` lists the results a recall affects, and nothing anywhere records what was decided about each one. |
| **8.5.1–8.5.3** Improvement, corrective action, preventive action | Review nonconformities, determine causes, evaluate the need for action, verify the action does not adversely affect conformity, record results, review effectiveness | PUBLISHER | `MOS-SEC-143` (published vulnerability budget with CVSS-banded deadlines), `MOS-SEC-144` (daily rescan of already-published digests), `MOS-AGENT-108` (a successful production injection becomes a regression fixture before the fix is released), `MOS-REL-073` (a clinical-path change adds at least one named-corpus case) | PARTIAL — three narrow, well-specified corrective loops with no common record. There is no CAPA entity, no root-cause field, no effectiveness review and no closure approval. `MOS-SEC-143` MUST NOT be described as a CAPA process; it is a patch SLA. |

##### 18.6.2.3 The post-market half is missing, and it is the expensive half

The pattern above is unambiguous and worth stating plainly rather than burying in a Status column. Everything ISO 13485 asks of the pre-market design controls — inputs, outputs, review, verification, transfer, change control, design file, traceability — is either SATISFIED or PARTIAL with a named, small deficit. Everything it asks *after* the product ships — feedback, complaints, vigilance reporting, disposition of nonconforming product already delivered, CAPA — is a gap or a partial with no record structure behind it. A publisher cannot certify a QMS on this platform without building that half themselves, and they should be told so before they buy, not during their audit.

**MOS-CONF-306** The platform MUST NOT describe `MOS-SAFE-071`'s review-outcome aggregate or `MOS-EVID-135`'s `monitoring_period` report as "post-market surveillance", "PMS", "feedback" in the ISO 13485 8.2.1 sense, or "vigilance". They are monitoring telemetry, and `MOS-SAFE-072` and `MOS-EVID-142` already constrain what may be claimed of them.

**MOS-CONF-307** The platform SHOULD provide a minimal feedback intake: a `Feedback` record carrying `{feedback_id, tenant_id, reported_by, reported_at, free_text, severity_asserted, linked_result_id?, linked_job_id?, linked_service_version_id?, routed_to_publisher_at?}`, append-only, audited as class `clinical`, with no platform-side triage logic. This is the smallest structure that turns 8.2.1 from GAP into a publisher-dischargeable obligation, and it deliberately makes no reportability decision.

**MOS-CONF-308** The platform MUST NOT implement complaint handling, complaint investigation or regulatory reporting. These are `PUBLISHER` obligations under `MOS-SAFE-003` and `MOS-SAFE-004`, and a platform that performed them would be assuming a manufacturer function it has spent chapter 9 disclaiming. `MOS-CONF-307`'s record exists to be *exported*, not adjudicated.

**MOS-CONF-309** When a recall or suspension occurs, the platform MUST record a **disposition** per affected `Result`: one of `{no_action, notified, superseded, withdrawn_from_read_path}`, with actor, timestamp and rationale, referencing the `Result` rows enumerated by `MOS-REG-040`. Without it, `MOS-REG-040` answers "which results are affected" and nothing answers "what was done about them", which is the question ISO 13485 8.3 actually asks.

**MOS-CONF-310** The platform MUST NOT present `MOS-SEC-143` as a corrective and preventive action process. Where a vulnerability handling record is exported into a publisher's QMS it MUST be labelled `vulnerability_remediation`, not `capa`.

---

#### 18.6.3 ISO/IEC 27001:2022

##### 18.6.3.1 Posture

An ISMS certificate belongs to an organisation and covers a declared scope. The deployable object here is not the certificate but the **Statement of Applicability**: the SITE, when it certifies, must justify each of the 93 Annex A controls as applicable-and-implemented, applicable-and-not-implemented, or excluded. The platform's contribution is to implement a defined subset of the technological controls so that the SITE's SoA can cite a mechanism rather than a policy.

**MOS-CONF-320** The platform MUST publish and maintain an Annex A control-mapping document in the documentation set of `MOS-REL-095`, with one row per Annex A control, an owner from `MOS-CONF-301`'s enum and, for PLATFORM rows, the requirement IDs that implement it. CI MUST fail when a cited requirement ID does not exist, which is the same orphan check as `MOS-REL-098` applied in the opposite direction.

**MOS-CONF-321** Every control this section marks SITE MUST be enumerated in `DEPLOYMENT.md` as an explicit operator obligation. A control the platform does not implement and does not tell the operator about is a control nobody holds.

##### 18.6.3.2 A.5 Organizational controls

| Control | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| A.5.1–A.5.8, A.5.10, A.5.11 Policies, roles, segregation of duties, management responsibility, authority contact, threat intelligence, security in project management, asset return, acceptable use | Organisational policy and assignment | SITE | Platform contributes segregation of duties mechanically: `MOS-SEC-042` (no seeded role holds both halves of the evidence/approval split), `MOS-TRAIN-174` (no machine principal holds an approval permission), `MOS-SEC-080` (break-glass SHOULD require a second approver) | SITE — correctly located; not a gap |
| A.5.9 Inventory of information and other associated assets | Inventory maintained, owners assigned | PLATFORM (software assets), SITE (everything else) | `MOS-REG-001` (the registry is the only source of truth for which artifact versions exist), `MOS-SEC-142` (SBOM per image and per model artifact, retrievable through the API for any deployed version), `MOS-SEC-140` (digest pinning everywhere) | PARTIAL — the software and model inventory is exemplary; there is no inventory of information assets (databases, object-store prefixes, telemetry stores) with named owners. |
| A.5.12–A.5.13 Classification and labelling of information | Classification scheme applied, labels attached | PLATFORM | `MOS-SEC-102` (six classes P0/P1/P2/P3/P4/SEC; the class, not the field name, determines where a field may travel), `MOS-SEC-105` (a normative forbidden-surface matrix), `MOS-SEC-106` (`study_ref` substitution) | SATISFIED |
| A.5.14 Information transfer | Rules and agreements for transfer | PLATFORM (internal), SITE (external) | `MOS-SEC-020` (mTLS between platform components with SPIFFE workload identity), `MOS-SEC-088` (everything addresses the Gateway; the PACS is not routable from other zones), `MOS-DATA-021` (Gateway egress by consumer class), `MOS-DATA-037` (de-identification fails closed), `MOS-EVID-011` (`patient_key` MUST NOT cross a tenancy boundary in a vendor bundle) | SATISFIED for platform-internal transfer |
| A.5.15 Access control | Rules established based on business and security requirements | PLATFORM | `MOS-SEC-001` (deny-by-default on every axis, including unavailability of the decision point), `MOS-SEC-031`–`MOS-SEC-033` (one permission namespace), `MOS-SEC-038`–`MOS-SEC-043` (tenant-definable roles, seeded roles, `platform_admin` holds no `phi`-class permission) | SATISFIED |
| A.5.16 Identity management | Full lifecycle of identities | PLATFORM (platform identities), SITE (people) | `MOS-SEC-008` (every request resolves to exactly one principal of exactly one kind; no anonymous access outside `/healthz`, `/readyz`, OpenAPI), `MOS-SEC-009` (one `Authenticator` port), `MOS-SEC-018`, `MOS-SEC-019` (per-tenant IdP claim mapping as data; a tenant resolvable from the issuer alone) | SATISFIED |
| A.5.17 Authentication information | Allocation and management of secrets | PLATFORM | `MOS-SEC-010` (argon2id, plaintext returned once), `MOS-SEC-011`, `MOS-SEC-012` (mandatory expiry ≤ 365 days), `MOS-SEC-014` (`mos_` registered as a secret-scanning pattern), `MOS-SEC-015` (revocation effective within 5 s), `MOS-SEC-133`–`MOS-SEC-137` (no secrets in the repo, declared holder/store/rotation/compromise procedure per secret, file mounts not env vars, never logged, two-key overlap for rotation) | SATISFIED |
| A.5.18 Access rights | Provision, review and revoke | PLATFORM (mechanism), SITE (review) | `MOS-SEC-013` (a key's scope is a subset of the principal's permissions, re-checked at every use), `MOS-SEC-044` (permissions evaluated per call, never snapshotted into a token as a grant), `MOS-SEC-027` (a Job Token scope is a ceiling, not a grant) | PARTIAL — provisioning and revocation are strong; no requirement mandates periodic access review, which is a SITE process the platform gives no report to support. |
| A.5.19–A.5.22 Supplier relationships | Security in supplier agreements, ICT supply chain, monitoring and change management of supplier services | PLATFORM (artifact supply chain), SITE (commercial) | `MOS-SEC-138` (every artifact signed; verification failure blocks, never warns), `MOS-SEC-141` (keyed verification MUST work with no internet access), `MOS-SEC-142` (SBOM), `MOS-SEC-144` (build-time and daily rescan), `MOS-REG-087`, `MOS-REG-089`, `MOS-REG-090` (attestations, named verifier and enforcement point per requirement, fail closed), `MOS-REG-094` (fixed install flow), `MOS-CORE-014` (publishers integrate only through the public API and SDK) | PARTIAL — the technical supply chain is among the strongest parts of the specification. There is no supplier assessment, onboarding or agreement process, and none should live in software. |
| A.5.23 Information security for use of cloud services | Acquisition, use, management and exit of cloud services | SITE | Spine §15 and `MOS-SEC-141` establish on-prem-first with no mandatory proprietary cloud dependency, which reduces this control's surface but does not discharge it | SITE |
| A.5.24–A.5.28 Incident management planning, assessment, response, learning, evidence collection | Plan, classify, respond, learn, collect evidence | SITE (process), PLATFORM (evidence) | `MOS-REL-095` (`SECURITY.md` carries a reporting channel, a response commitment and supported versions), `MOS-SEC-152`, `MOS-SEC-153` (signed checkpoints and `audit.verify` make forensic evidence admissible), `MOS-OPS-052` (tail sampling retains 100 % of non-`COMPLETED` and error traces) | **GAP (PLATFORM side)** — the evidence-collection limb (A.5.28) is well served. There is no incident classification scheme, no severity ladder, no response-time commitment beyond a prose line in `SECURITY.md`, and no security-incident record joined to `AuditEvent`. |
| A.5.29–A.5.30 Information security during disruption; ICT readiness for business continuity | Maintain security during disruption; plan and test ICT continuity against RTO/RPO | SITE (plan), PLATFORM (capability) | `MOS-STORE-320` mentions point-in-time restore in passing, as the recovery route from a bad migration | **GAP** — no backup requirement, no RTO, no RPO, no restore test, no redundancy requirement anywhere in chapters 1–17. See `MOS-CONF-322`. |
| A.5.31–A.5.32 Legal, statutory, regulatory and contractual requirements; intellectual property | Identify and meet applicable requirements | PUBLISHER, SITE | `MOS-SAFE-023`, `MOS-SAFE-024` (`regulatory_status` per jurisdiction), `MOS-SAFE-029` (the platform stores and displays, never validates, the assertion), `MOS-TRAIN-072`–`MOS-TRAIN-077` (`TrainingDataPolicy` with a declared legal basis and reference, harvest refused without it), `MOS-EVID-027` (licence non-null and carried into the report) | SATISFIED as a capture mechanism; the determination itself is never the platform's. |
| A.5.33 Protection of records | Records protected from loss, destruction, falsification and unauthorised access | PLATFORM | `MOS-SEC-149`–`MOS-SEC-154`, `MOS-SAFE-090`, `MOS-EVID-013`, `MOS-STORE-337` | SATISFIED |
| A.5.34 Privacy and protection of PII | Identify and meet PII requirements | PLATFORM (mechanism), SITE (determination) | `MOS-SEC-102`–`MOS-SEC-106`, `MOS-SEC-126`–`MOS-SEC-132` (retention per class, `ErasureRequest`, crypto-shredding, the honest report of what could not be erased), `MOS-SEC-121`–`MOS-SEC-125` (`external_llm_allowed` default false), ch 3 de-identification | SATISFIED |
| A.5.35–A.5.36 Independent review; compliance with policies and standards | Independent review at planned intervals; regular compliance verification | SITE | `MOS-SEC-111`, `MOS-TEST-007` (PHI scanner in CI), `MOS-SEC-153` (CI tamper test), `MOS-SEC-156` (a release MUST NOT ship with a security requirement unimplemented) | PARTIAL — automated self-verification is strong; there is no independent review and no penetration test requirement. |
| A.5.37 Documented operating procedures | Procedures documented and available | PLATFORM (partial), SITE | `MOS-OPS-055` (every alert carries a `runbook` annotation or a `summary` naming the remedy, enforced by an alert-rule lint), `MOS-REL-095` (`DEPLOYMENT.md`, `DEVELOPMENT.md`), `MOS-OPS-054` (alerts routed by owner) | PARTIAL — alert-level runbooks are mandated; no operating-procedure set exists for backup, restore, key compromise, tenant offboarding or incident response. |

##### 18.6.3.3 A.6 People controls and A.7 Physical controls

**All 22 controls in these two themes are SITE.** A.6.1–A.6.8 (screening, terms of employment, awareness, disciplinary process, post-employment responsibilities, confidentiality agreements, remote working, reporting events) and A.7.1–A.7.14 (physical perimeter and entry, securing offices, monitoring, protection against physical and environmental threats, working in secure areas, clear desk, equipment siting, off-premises assets, storage media, supporting utilities, cabling, maintenance, secure disposal) describe obligations of an employing organisation operating premises. `MOS-SEC-006` already disclaims the adjacent threat model explicitly: MedicalOS MUST NOT claim to protect against a hostile administrator with root on the deployment host, physical access to GPU nodes, or a compromised hypervisor.

Two platform mechanisms touch this theme without discharging any of its controls: `MOS-SAFE-104` (`Role.clinically_qualified`, tenant-set, defaulting to false) gives the SITE a place to record a competence determination it makes elsewhere, and `MOS-SEC-136` prevents a secret reaching a log where a person could read it. Neither is an A.6 or A.7 control.

##### 18.6.3.4 A.8 Technological controls

| Control | Owner | Satisfied by | Status |
|---|---|---|---|
| A.8.1 User endpoint devices | SITE | — | SITE |
| A.8.2 Privileged access rights | PLATFORM | `MOS-SEC-043` (`platform_admin` exists only in the system tenant and holds no `phi`-class permission), `MOS-SEC-080`–`MOS-SEC-082` (break-glass: time-boxed, second approver SHOULD, always audited, never grants write), `MOS-SEC-157` (break-glass RLS policy declared `FOR SELECT` so it cannot leak into `USING` for writes), `MOS-TRAIN-174` | SATISFIED |
| A.8.3 Information access restriction | PLATFORM | `MOS-SEC-072`–`MOS-SEC-079` (forced RLS, application role without `BYPASSRLS`, the repository chokepoint, migrations and background jobs covered), `MOS-SEC-083`–`MOS-SEC-086` (tenant-first object keys, no PHI in keys), `MOS-SEC-100` (cache key prefixing) | SATISFIED |
| A.8.4 Access to source code | SITE | — | **GAP** — the core is Apache-2.0 and public by design (spine §15, ch 15 §15.8), so read restriction is inapplicable; write restriction (branch protection, signed commits, release-tag signing per `MOS-REL-123`) is partially covered but never stated as a control. |
| A.8.5 Secure authentication | PLATFORM | `MOS-SEC-010`, `MOS-SEC-016` (OIDC Authorization Code with PKCE; implicit flow MUST NOT be supported), `MOS-SEC-017` (issuer, audience, expiry with ≤ 60 s skew, JWKS signature), `MOS-SEC-020`, `MOS-SEC-022` (certificate lifetime ≤ 24 h, fail readiness rather than serve expired) | SATISFIED |
| A.8.6 Capacity management | PLATFORM (compute), SITE (storage) | `MOS-OPS-079`, `MOS-OPS-080` (GPU reserved bytes measured not estimated; headroom ratio bounded), ch 13 §13.11 backpressure and §13.12 quotas including the GPU-hours ledger, `MOS-SEC-114` (metric cardinality bounded) | PARTIAL — compute capacity is governed; there is no storage capacity, retention-pressure or object-store growth control. |
| A.8.7 Protection against malware | SITE | — | SITE — no requirement exists, and none is claimed. |
| A.8.8 Management of technical vulnerabilities | PLATFORM | `MOS-SEC-143` (CVSS-banded budget with deadlines and a reachability exemption), `MOS-SEC-144` (build-time plus daily rescan of published digests), `MOS-SEC-145` (a newly failing digest suspends the deployment), `MOS-REG-091` (a new critical finding auto-suspends but never auto-recalls) | SATISFIED |
| A.8.9 Configuration management | PLATFORM | `MOS-SEC-140` (digest pinning everywhere; CI fails on `:latest` or a tag without a digest), `MOS-OPS-065` (the running process's reported digest must match the manifest), `MOS-REL-095` (generated configuration reference with a drift check), ch 13 §13.2 configuration contract | SATISFIED |
| A.8.10 Information deletion | PLATFORM | `MOS-SEC-126` (retention per class), `MOS-SEC-129`, `MOS-SEC-130` (`ErasureRequest` record; destroy DEKs, delete prefixes, null columns, never touch audit rows), `MOS-SEC-131` (tenant erasure destroys the pepper, making every emitted `study_ref` unlinkable), `MOS-SEC-132` (report what could not be erased) | SATISFIED |
| A.8.11 Data masking | PLATFORM | `MOS-SEC-103` (per-tenant pepper, never exported, never rotated), `MOS-SEC-106` (`study_ref` substitution wherever the matrix forbids P2), `MOS-EVID-010`, `MOS-EVID-035` (`patient_key`, HMAC accession hash), ch 3 PS3.15 de-identification profiles | SATISFIED |
| A.8.12 Data leakage prevention | PLATFORM | `MOS-SEC-105` (normative forbidden-surface matrix), `MOS-SEC-107`–`MOS-SEC-110` (typed log API with a closed attribute type; third-party loggers not importable; error-chain sanitisation; independent shipper-side redaction), `MOS-SEC-111` (PHI scanner over repo, fixture log corpus, exported traces and metrics), `MOS-SEC-112`–`MOS-SEC-118` (metric and span allowlists, `http.url`/`db.statement` dropped), `MOS-SEC-119`, `MOS-SEC-120` (sealed-service stdout treated as PHI-bearing), `MOS-SEC-124` (pre-flight prompt scan, fail closed) | SATISFIED — the most thoroughly specified control in this table. |
| A.8.13 Information backup | PLATFORM | — | **GAP** — no backup requirement exists. `MOS-STORE-320` assumes point-in-time restore without requiring it. |
| A.8.14 Redundancy of information processing facilities | PLATFORM, SITE | — | **GAP** — no availability, replication or failover requirement. `MOS-OPS-056` declares SLO targets but no redundancy mechanism behind them. |
| A.8.15 Logging | PLATFORM | `MOS-SEC-146`–`MOS-SEC-150` (schema, actor plus on-behalf-of, mandatory events for `phi`/`clinical`/`admin`/`governance` classes on both allow and deny, same-transaction insert, paired cross-boundary events), `MOS-SEC-151`–`MOS-SEC-154` | SATISFIED |
| A.8.16 Monitoring activities | PLATFORM (mechanism), SITE (watching) | ch 13 §13.4 metric catalogue, `MOS-OPS-054`, `MOS-OPS-055`, `MOS-OPS-056`, `MOS-OPS-041` (a redaction violation raises a ticket-severity alert), `MOS-AGENT-147` (a non-zero PHI block count pages), `MOS-EVID-137` | SATISFIED |
| A.8.17 Clock synchronisation | PLATFORM | — | **GAP** — no clock requirement exists, although `MOS-SEC-017` assumes ≤ 60 s token skew, `MOS-SEC-024` ties `exp` to `deadline_at`, and the audit chain of `MOS-SEC-151` carries timestamps whose forensic value depends on synchronised clocks. See `MOS-CONF-324`. |
| A.8.18 Use of privileged utility programs | SITE | `MOS-SEC-073` (the application database role MUST NOT hold `BYPASSRLS`) constrains one such utility path | PARTIAL |
| A.8.19 Installation of software on operational systems | PLATFORM | `MOS-REG-094` (fixed install flow), `MOS-SEC-138`, `MOS-SEC-140`, `MOS-REG-090` (verification fails closed and MUST NOT be bypassable in production) | SATISFIED |
| A.8.20–A.8.22 Networks security, security of network services, segregation of networks | PLATFORM (topology), SITE (fabric) | `MOS-SEC-004` (no cross-zone connection outside the declared list, enforced by NetworkPolicy or per-service networks, not by code), `MOS-SEC-005` (no component outside `Z-GATEWAY` holds a PACS credential), `MOS-SEC-029` (sealed containers run egress deny-all except the Gateway and callback), `MOS-SEC-088`, `MOS-DATA-006`, `MOS-OPS-066` | SATISFIED |
| A.8.23 Web filtering | SITE | `MOS-SEC-029`, `MOS-AGENT-045` (`external_index_allowed` default false) constrain platform-originated egress only | PARTIAL |
| A.8.24 Use of cryptography | PLATFORM | `MOS-SEC-127` (KMS root → tenant KEK → per-scope DEK), `MOS-SEC-128` (named envelope-encrypted columns), `MOS-SEC-023` (Ed25519 JWS Job Tokens), `MOS-EVID-007`, `MOS-EVID-008` (SHA-256 and RFC 8785 everywhere, no algorithm choice), `MOS-EVID-118` (Ed25519 in a DSSE envelope), `MOS-SEC-137` (two-key overlap for rotation) | SATISFIED |
| A.8.25 Secure development life cycle | PLATFORM | `MOS-REL-116`, `MOS-REL-117` (change classes and gate matrix), `MOS-REL-118` (enforced by CI and a PR template, never reviewer memory), `MOS-REL-121` (observability is a gate, not a phase) | PARTIAL — the change-level lifecycle is well specified; there is no lifecycle above it (see 7.3.2 above and `MOS-OPEN-025`). |
| A.8.26 Application security requirements | PLATFORM | Chapter 8 in its entirety; `MOS-SEC-156` (a release MUST NOT ship with a security requirement in its row unimplemented) | SATISFIED |
| A.8.27 Secure system architecture and engineering principles | PLATFORM | `MOS-SEC-001` (deny by default), `MOS-SEC-002` (isolation enforced at the layer that stores or moves the data, not only where it is exposed), `MOS-SEC-003` (every security-relevant decision joinable to the execution that caused it), `MOS-SEC-004` | SATISFIED |
| A.8.28 Secure coding | PLATFORM | `MOS-SEC-107`, `MOS-SEC-109` (closed-constructor logging in Go and Python, import bans enforced), `MOS-SEC-108` (error-chain sanitisation), ch 15 §15.5 language conventions, `MOS-REL-117` (lint and type checks on every class) | SATISFIED |
| A.8.29 Security testing in development and acceptance | PLATFORM | `MOS-SEC-111`, `MOS-TEST-007`, `MOS-TEST-056` (PHI scanning over repo, images, fixture logs, traces and metrics), AT-08 (tenant isolation on API, imaging plane, database and network) and AT-09 (no PHI on six surfaces), `MOS-TEST-067` (cross-tenant responses byte-identical to not-found), `MOS-SEC-153` (audit tamper test), `MOS-AGENT-107` (a ≥ 40-fixture prompt-injection canary corpus in CI) | PARTIAL — the automated suite is unusually strong for a specification at this stage; there is no requirement for independent penetration testing or a security assessment before a clinical deployment. |
| A.8.30 Outsourced development | SITE, PUBLISHER | `MOS-CORE-014` (publishers integrate only through the public API and SDK; service-specific code MUST NOT be merged into core), `MOS-REG-093` (the boundary is the OCI artifact plus manifest, out of process) | PARTIAL — the technical boundary is exact; contractual control is a SITE obligation. |
| A.8.31 Separation of development, test and production environments | PLATFORM | `MOS-SAFE-046` (`deployment_environment` ∈ `dev`, `staging`, `production` snapshotted onto every Result), `MOS-REG-074` (one active serving deployment per tenant/environment/capability), `MOS-STORE-320` (down-migrations for local development only), `MOS-SEC-020` (workload identity carries the environment) | PARTIAL — the environment is a first-class, carried value, and no requirement forbids a production credential, dataset or PACS endpoint reaching a non-production environment. |
| A.8.32 Change management | PLATFORM | `MOS-REL-116`–`MOS-REL-120`, `MOS-REL-123`, `MOS-REL-124` (a release is not Done on a gate run assembled from several commits), `MOS-REG-095`, `MOS-REG-096`, `MOS-EVID-094` | SATISFIED |
| A.8.33 Test information | PLATFORM | `MOS-TEST-007` (no real PHI in any fixture, golden file, recorded interaction or CI log), `MOS-TEST-056` (the PHI token set), `MOS-IMG-155` (named versioned de-identified fixture corpus), `MOS-TRAIN-072`, `MOS-TRAIN-073` (harvest refused without a `TrainingDataPolicy`) | SATISFIED |
| A.8.34 Protection of information systems during audit testing | SITE | — | SITE |

##### 18.6.3.5 The four technological gaps

Four A.8 controls have no owner that can discharge them today, and all four are PLATFORM: A.8.13 backup, A.8.14 redundancy, A.8.17 clock synchronisation and — jointly with A.5.24–A.5.27 — security incident management. Backup and redundancy are the more serious pair, because a hospital procurement questionnaire asks about RPO and RTO on the first page, and the honest answer at 0.2.0 is that the specification has never mentioned either.

**MOS-CONF-322** The platform MUST declare, per data class of `MOS-SEC-126`, a backup mechanism, a recovery point objective, a recovery time objective, and a restore verification cadence; and a restore MUST be exercised in CI or in a scheduled job against a non-production copy, because a backup that has never been restored is a belief, not a control. Until this exists, `DEPLOYMENT.md` MUST state that backup and continuity are wholly SITE obligations for which the platform provides no mechanism.

**MOS-CONF-323** The platform MUST define a security incident record joined to `AuditEvent` by `request_id` or `trace_id`, carrying a severity from a declared closed enum, a detection source, a containment action and a closure rationale, and MUST declare a response-time commitment per severity in `SECURITY.md`. The evidence-collection limb of A.5.28 is already satisfied by `MOS-SEC-152` and `MOS-SEC-153`; what is missing is the record that cites it.

**MOS-CONF-324** Every component MUST synchronise its clock to a configured time source, MUST expose its measured offset as a metric, and MUST fail readiness when the offset exceeds a declared bound. `MOS-SEC-017`'s 60 s token skew tolerance, `MOS-SEC-024`'s token expiry rule and the forensic value of `MOS-SEC-151`'s timestamps all assume this and none of them requires it.

**MOS-CONF-325** The platform MUST NOT claim any A.6 (people) or A.7 (physical) control, and MUST NOT claim A.8.1, A.8.7 or A.8.34. `MOS-SEC-006` already fixes the threat-model boundary these sit outside.

**MOS-CONF-326** Before the first `clinical`-mode deployment at any site, an independent security assessment of the platform SHOULD be performed and its report SHOULD be retained by the SITE. The specification's automated suite (`MOS-SEC-111`, AT-08, AT-09, `MOS-AGENT-107`) tests the threats the authors thought of, which is precisely the population an independent assessment exists to widen.

---

#### 18.6.4 ISO/IEC 42001:2023

##### 18.6.4.1 Why 42001 is the right AI target for this project

ISO/IEC 42001 is a certifiable management-system standard with the same clause skeleton as 27001 (context, leadership, planning, support, operation, performance evaluation, improvement) plus an Annex A of AI-specific controls. It is the AI counterpart a hospital procurement officer or a notified body can actually ask for a certificate against. That makes it more useful to this project than NIST AI RMF 1.0, which is a voluntary framework with no conformity assessment scheme and therefore nothing to show a reviewer. §18.6.5 maps NIST as a cross-reference only.

**MOS-CONF-340** The AI management system is an obligation of the `PUBLISHER` (for the AI system it manufactures) and of the `SITE` (for the AI systems it deploys and uses). MedicalOS core is neither; under `MOS-CORE-005` and `MOS-SAFE-002` it makes no clinical claim and manufactures no AI system. The platform's 42001 role is to supply the records of Annex A.6 (life cycle), A.7 (data) and A.6.2.8 (event logs), and to make A.9 (use) mechanically enforceable.

##### 18.6.4.2 Annex A control map

| Control | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| A.2.2–A.2.4 AI policy, alignment, review | A documented AI policy, aligned with other policies, reviewed | PUBLISHER, SITE | — | SITE/PUBLISHER — correctly located, not a platform gap |
| A.3.2 AI roles and responsibilities | Responsibilities defined and allocated | PUBLISHER, SITE | `MOS-SAFE-003` (the `ServiceVersion` publisher is the manufacturer), `MOS-SAFE-004` (the three-column responsibility table, not silently reassignable by configuration), `MOS-SAFE-005` (a site that modifies a version becomes the manufacturer, and the signature makes it mechanically true), `MOS-TRAIN-173`, `MOS-TRAIN-174` | SATISFIED — this is the specification's single best 42001 mapping. |
| A.3.3 Reporting of concerns | A mechanism for reporting concerns about the AI system | SITE | — | **GAP** — no concern-reporting channel exists for clinical users. `MOS-CONF-307`'s feedback record would be the natural home. |
| A.4.2–A.4.6 Resource documentation, data, tooling, system and computing, human resources | Document the resources an AI system consumes | PUBLISHER | `MOS-EVID-061` (the binding), `MOS-TRAIN-129`–`MOS-TRAIN-131` (the bundle and its declared backend), `MOS-REG-097`–`MOS-REG-100` (exact compatibility pins, `NodeProfile` inventory), `MOS-SEC-142` (SBOM), `MOS-SAFE-104` (`clinically_qualified` roles) | SATISFIED for data, tooling and computing resources; PARTIAL for human resources, which is a SITE competence determination. |
| A.5.2–A.5.5 AI system impact assessment: process, documentation, impact on individuals and groups, societal impact | A documented process for assessing the impacts of the AI system on individuals, groups and society, performed and recorded | PUBLISHER | Adjacent inputs only: `MOS-SAFE-019` (IMDRF risk category derived from significance-of-information and healthcare-situation axes), `MOS-SAFE-014` (`known_limitations`, `not_validated_for`, `target_population`), `MOS-SAFE-016` (`training_population`, nullable with an explicit reason), `MOS-EVID-067` (mandatory strata including patient-level covariates) | **GAP** — the phrase "impact assessment" appears nowhere in chapters 1–17, and neither does "risk assessment". The platform holds the *inputs* an impact assessment would consume — declared population, declared limitations, per-stratum performance — and provides no place to record the assessment, no requirement that one exists, and no gate that consults one. See `MOS-CONF-341`. |
| A.6.1.2–A.6.1.3 Objectives for responsible development; processes for responsible design and development | Define and apply responsible development objectives and processes | PUBLISHER, PLATFORM | `MOS-TRAIN-001` (the pipeline runs entirely off the production inference path), `MOS-TRAIN-005` (no automated path from production data to a serving model), `MOS-TRAIN-086`, `MOS-TRAIN-100`–`MOS-TRAIN-103` (model-seeded fraction bounded, de-novo control arm, paired anchoring sub-study, reader exclusion), `MOS-TRAIN-194` (no monitoring-triggered retraining before 0.5) | SATISFIED — the closed-loop prohibition is a stronger statement of responsible development than the control asks for. |
| A.6.2.2 AI system requirements and specification | Requirements specified, including performance | PUBLISHER | `MOS-SAFE-013`, `MOS-SAFE-014`, `MOS-EVID-076`–`MOS-EVID-079`, `MOS-EVID-075` (clinical bar on the Capability, engineering bar on the ModelVersion) | SATISFIED |
| A.6.2.3 Documentation of design and development | Design and development documented | PUBLISHER | `MOS-EVID-061`, `MOS-TRAIN-176`–`MOS-TRAIN-178`, `MOS-TRAIN-235` (a search reproducible from its seed and configuration space) | SATISFIED |
| A.6.2.4 AI system verification and validation | Verification and validation performed against defined criteria, with records | PUBLISHER | `MOS-EVID-061`–`MOS-EVID-070`, `MOS-EVID-083`–`MOS-EVID-089` (verdict derivation, advisory vs blocking criteria, paired non-inferiority with a declared and justified margin), `MOS-EVID-090`–`MOS-EVID-094` (the gate; `FAIL` not overridable in clinical mode), `MOS-EVID-129` | SATISFIED |
| A.6.2.5 AI system deployment | Deployment plan, requirements met before deployment | PLATFORM | `MOS-REG-072`–`MOS-REG-081` (deployment as the sole owner of liveness; role and state orthogonal; `verification_ref` required before `SERVING`; no auto-promotion in clinical mode), `MOS-SAFE-036` (the E1 clinical gate), `MOS-SAFE-037` | SATISFIED |
| A.6.2.6 AI system operation and monitoring | Operation and monitoring defined and performed | PLATFORM | `MOS-EVID-135`–`MOS-EVID-142` (unattended monitoring, signed period reports, PSI drift with fixed binning, hard breach suspends, no `PASS` verdict without a reference standard), `MOS-EVID-100`–`MOS-EVID-104` (applicability envelope evaluated at triage), `MOS-EVID-105`–`MOS-EVID-112` (output plausibility rules, calibrated before promotion), `MOS-OPS-054`–`MOS-OPS-056` | SATISFIED |
| A.6.2.7 AI system technical documentation | Technical documentation determined and maintained for relevant parties | PUBLISHER | `MOS-TRAIN-176` (offline-renderable dossier), `MOS-EVID-122` (self-contained bundle), `MOS-REL-095`, `MOS-SAFE-022` (`/clinical` returns the block verbatim with its digest) | SATISFIED |
| A.6.2.8 AI system recording of event logs | Event logs recorded, with retention | PLATFORM | `MOS-SEC-146`–`MOS-SEC-155`, `MOS-SAFE-082`–`MOS-SAFE-091` (one provenance record per Result, hash-chained, daily verified, signed export), `MOS-SEC-126` (audit retention ≥ 6 years) | SATISFIED |
| A.7.2–A.7.3 Data for development; acquisition of data | Data for development defined; acquisition documented and lawful | PUBLISHER, SITE | `MOS-TRAIN-072` (`training_use_allowed` default false, no override and no admin bypass), `MOS-TRAIN-073` (`TrainingDataPolicy` with declared legal basis, reference, digest, scope, expiry and named human), `MOS-TRAIN-074` (the platform stores and refuses, never assesses sufficiency), `MOS-TRAIN-075`–`MOS-TRAIN-077`, `MOS-EVID-016` (only a materialised list; never a query or a view), `MOS-EVID-027` (licence) | SATISFIED |
| A.7.4 Quality of data | Data quality defined and assessed | PUBLISHER | `MOS-EVID-024`, `MOS-EVID-025` (computed `acquisition_profile` with a fixed percentile method), `MOS-EVID-026` (n ≥ 30 patients for an acceptance cohort), `MOS-EVID-019` (lossy compression declared), `MOS-EVID-020` (no imputation of acquisition values), `MOS-EVID-034`–`MOS-EVID-037` (five blocking leakage checks with an auditable waiver object), `MOS-TRAIN-081` (exclusion reasons aggregated and reproduced in the report) | SATISFIED |
| A.7.5 Data provenance | Provenance of data recorded and maintained | PUBLISHER, PLATFORM | `MOS-EVID-010` (`patient_key`), `MOS-EVID-015`–`MOS-EVID-018` (single-transaction sealing, content-addressed manifest, per-series pixel digest), `MOS-EVID-021`, `MOS-EVID-022` (de-identification policy version, parent version on any derivation), `MOS-TRAIN-085`, `MOS-TRAIN-098`, `MOS-TRAIN-099` (per-case annotation provenance with a closed three-value enum and seed-model binding), `MOS-TRAIN-087` (corpus generation counter) | SATISFIED — better than the control requires. |
| A.7.6 Data preparation | Preparation methods documented | PUBLISHER | `PreprocessingSpec` as a versioned artifact co-located with the weights and covered by the same signature (`MOS-REG-086`), `MOS-IMG-048` (deterministic CPU preprocessing, single code path), `MOS-TRAIN-042` (CI asserts transform identity), `MOS-IMG-054` | SATISFIED |
| A.8.2 System documentation and information for users | Users given information needed to use the system | PUBLISHER | `MOS-SAFE-013`, `MOS-SAFE-014`, `MOS-SAFE-018` (input constraints are prose for humans; the `SeriesSelector` is the machine contract and the platform MUST NOT evaluate the prose), `MOS-SAFE-022`, `MOS-SAFE-088` (**AMENDED at specification 0.4.0:** ~~the OHIF provenance panel~~ the provenance panel of whichever operator surface renders the result renders manufacturer, mode, and the rest without a second request; the requirement's own wording is Chapter 9's, and it binds the rendering surface rather than a named product, so narrowing this gloss to the clinician surface would drop the actor the requirement actually has) | SATISFIED — the information reaching the user is unchanged; only the surface rendering it is first-party |
| A.8.3 External reporting | A mechanism for external parties to report adverse impacts | PUBLISHER | — | **GAP** — same gap as ISO 13485 8.2.2 and 42001 A.3.3, reached from a third direction. |
| A.8.4 Communication of incidents | Incidents communicated to relevant interested parties | PUBLISHER, SITE | `MOS-SEC-145` (a failed digest verification emits a webhook), `MOS-EVID-139` (a hard breach suspends and alerts), `MOS-SAFE-027` (certificate expiry demotes clinical deployments) | PARTIAL — the platform can emit machine events about its own state changes. There is no AI-incident notion, no recipient list and no communication record. |
| A.8.5 Information for interested parties | Interested parties informed of their role and of the AI system's characteristics | PLATFORM | `MOS-SAFE-012` (every user-facing surface displaying an AI-derived finding shows the disclosure adjacent to it), `MOS-SAFE-047`–`MOS-SAFE-054` (AI-derived determinable from metadata alone; per-type and RUO markers; `derivation: "ai_derived"` on every API measurement), `MOS-SAFE-051` (burned-in RUO banner on research SC frames), `MOS-AGENT-091` (persistent, non-dismissible chat disclosure that output is AI-generated and not a diagnosis) | SATISFIED — transparency to affected parties is one of the strongest areas of the specification, because it is enforced in the DICOM bytes and not only in the UI. |
| A.9.2 Processes for responsible use | Defined processes for responsible use | PLATFORM, SITE | `MOS-SAFE-033`–`MOS-SAFE-044` (the eight enforcement points of the research/clinical boundary, each code with a named failure), `MOS-SAFE-073`, `MOS-SAFE-074` (the default-deny clinical-action table, whose hard-DENY rows MUST NOT be made overridable by configuration, role, tenant setting, feature flag or environment variable), `MOS-SAFE-076` | SATISFIED |
| A.9.3 Objectives for responsible use | Objectives defined | SITE | `MOS-SAFE-011` (the platform refuses to run a version declaring `intended_use.autonomy: autonomous`), `MOS-SAFE-057` (results stored and visible; review recorded; nothing auto-actioned) | SATISFIED as a floor the SITE cannot lower |
| A.9.4 Intended use of the AI system | Use consistent with intended use | PLATFORM | `MOS-SAFE-018`, `MOS-EVID-095`–`MOS-EVID-103` (every clinical-mode version carries an `ApplicabilityEnvelope`; out-of-envelope studies are `REJECTED` with a closed reason code, visually distinct from `FAILED`), `MOS-EVID-099` (`not_validated_for` MUST be consistent with the envelope) | SATISFIED |
| A.10.2 Allocating responsibilities in the AI supply chain | Responsibilities allocated between parties | PLATFORM | `MOS-SAFE-003`, `MOS-SAFE-004`, `MOS-SAFE-005`, `MOS-SAFE-045` (a modified version fails the gate with `signature_not_from_declared_manufacturer` until the site signs as manufacturer) | SATISFIED |
| A.10.3 Suppliers | Requirements placed on suppliers of AI components | PLATFORM, PUBLISHER | `MOS-REG-094`, `MOS-SEC-138`, `MOS-SEC-142`, `MOS-REG-085` (weights MUST NOT be baked into a service image), `MOS-TRAIN-025` (a Model Zoo bundle must be re-registered under the registering party's own signing identity), `MOS-AGENT-109`–`MOS-AGENT-114` (all model access through one port; per-tenant `LlmBinding`; no platform-wide default; `binding_digest` pinned into every draft; external bindings marked `evidence_perishable`) | SATISFIED for third-party AI components, including the LLM case, which most AI governance mappings omit. |
| A.10.4 Customers | Obligations toward customers of the AI system | PUBLISHER | `MOS-EVID-127`, `MOS-EVID-128` (site acceptance reports cite the vendor report by digest) | PARTIAL |

**MOS-CONF-341** A `ServiceVersion` intended for `clinical` mode SHOULD carry an `impact_assessment_ref` pointing at a publisher-held assessment document, digested at registration in the manner of `MOS-SAFE-021`, and the platform MUST NOT interpret, validate or assess its contents — the posture `MOS-SAFE-029` takes toward `regulatory_status` and `MOS-TRAIN-074` takes toward `legal_basis`. Until such a field exists, ISO/IEC 42001 A.5.2–A.5.5 is a GAP that no party can discharge through the platform.

**MOS-CONF-342** The third-party AI component register required by A.10.3 MUST be answerable as a query rather than maintained as a document: the set of `ServiceVersion` rows with their `legal_manufacturer` and signing identity, plus the set of `LlmBinding` rows with their `binding_digest`, `locality` and `evidence_perishable` flag. The platform MUST expose it per tenant.

**MOS-CONF-343** The platform MUST NOT describe its transparency mechanisms (`MOS-SAFE-012`, `MOS-SAFE-047`–`MOS-SAFE-054`, `MOS-AGENT-091`) as explainability or interpretability. They disclose *that* an output is AI-derived, by whom and under what mode; they say nothing about *why* a model produced it, and no requirement in chapters 1–17 produces a model explanation.

**MOS-CONF-344** Where a signal required by A.6.2.6 is emitted, it MUST carry the monitoring caveats already fixed by `MOS-EVID-136` (monitoring has no ground truth; the reviewed subset is not a random sample), `MOS-EVID-141` (aggregate, no PHI) and `MOS-EVID-142` (verdict `INDETERMINATE`, reason `no_reference_standard`). An AI management system fed uncaveated drift numbers will draw performance conclusions the data does not support.

**MOS-CONF-345** The data-governance controls A.7.2–A.7.6 are `PUBLISHER`-owned but MUST be mechanically enforced by the platform at the points chapters 7 and 17 already name; no configuration, role or flag MUST permit a harvest without a `TrainingDataPolicy` (`MOS-TRAIN-072`), a seal from a live query (`MOS-EVID-016`), or a frozen split with an unwaived leakage finding (`MOS-EVID-034`).

**MOS-CONF-346** The platform MUST NOT claim conformance to A.2 (AI policy), A.3.3 (reporting of concerns) or A.8.3 (external reporting). The first is organisational; the second and third are the same gap as ISO 13485 8.2.2 and MUST be reported once, in §18.6.6, rather than three times in three vocabularies.

---

#### 18.6.5 NIST AI RMF 1.0 — cross-reference only

**MOS-CONF-350** NIST AI RMF 1.0 is a voluntary framework with no conformity assessment scheme, no certificate and no auditor. MedicalOS MUST NOT be described as conformant to, aligned with, or assessed against it. It is mapped below at function level only, as a navigational aid for readers who arrive with NIST vocabulary, and every substantive obligation resolves to the ISO/IEC 42001 row it duplicates.

| Function | Where it lands in this specification | Owner |
|---|---|---|
| **GOVERN** — policies, accountability, workforce, third-party risk | `MOS-SAFE-003`, `MOS-SAFE-004`, `MOS-TRAIN-173`, `MOS-TRAIN-174`, `MOS-SAFE-073`; AI policy itself is absent (42001 A.2) | PUBLISHER, SITE |
| **MAP** — context, categorisation, capabilities, impacts | `MOS-SAFE-013`, `MOS-SAFE-014`, `MOS-SAFE-019`, `MOS-EVID-095`–`MOS-EVID-099`; impact characterisation is absent (42001 A.5) | PUBLISHER |
| **MEASURE** — metrics, evaluation, TEVV, tracking | `MOS-EVID-047`–`MOS-EVID-070`, `MOS-EVID-083`–`MOS-EVID-089`, `MOS-EVID-135`–`MOS-EVID-142` | PUBLISHER, PLATFORM |
| **MANAGE** — risk treatment, third parties, incident response, recovery | `MOS-REG-021`, `MOS-REG-040`, `MOS-EVID-139`, `MOS-EVID-140`, `MOS-SEC-145`; incident response and recovery are gaps (`MOS-CONF-322`, `MOS-CONF-323`) | PLATFORM, SITE |

---

#### 18.6.6 Consolidated gap register for §18.6

Every row below is a clause or control for which no party can currently discharge the obligation through this platform. Each is a line item in the project plan for becoming certifiable; none should be presented to a reviewer as anything else.

| # | Standard and clause | Gap | Owner of the fix | Remedy in this chapter |
|---|---|---|---|---|
| D-1 | ISO 13485 8.2.2; ISO/IEC 42001 A.3.3, A.8.3 | No complaint or concern intake exists in any form | PLATFORM (record), PUBLISHER (process) | `MOS-CONF-307`, `MOS-CONF-308` |
| D-2 | ISO 13485 8.2.3 | No vigilance record, reportability decision, clock or jurisdiction timeline | PUBLISHER | `MOS-CONF-308` (explicitly out of platform scope; the query inputs exist) |
| D-3 | ISO 13485 8.2.1 | Monitoring telemetry is not feedback and MUST NOT be labelled as such | PLATFORM | `MOS-CONF-306`, `MOS-CONF-307` |
| D-4 | ISO 13485 8.3 | Nonconforming results are enumerable but their disposition is never recorded | PLATFORM | `MOS-CONF-309` |
| D-5 | ISO 13485 8.5.2–8.5.3 | No CAPA record, root cause, effectiveness review or closure | PUBLISHER | `MOS-CONF-310` (ban on mislabelling the vulnerability budget as CAPA) |
| D-6 | ISO 13485 7.3.2, 7.3.10; ISO/IEC 27001 A.8.25 | No design and development plan and no assembled design history for the platform itself; `MOS-OPEN-025` records the decision as open at `G-PILOT` | PLATFORM | `MOS-CONF-305` (traceability limb only); the rest remains open |
| D-7 | ISO 13485 7.3.3 | Usability engineering is absent from chapters 1–17: no IEC 62366 input, no UI specification, no use-error analysis | PLATFORM, PUBLISHER | none in §18.6 — belongs to the usability part of this chapter |
| D-8 | ISO/IEC 27001 A.8.13, A.8.14, A.5.29, A.5.30 | No backup, no RPO, no RTO, no restore test, no redundancy requirement | PLATFORM | `MOS-CONF-322` |
| D-9 | ISO/IEC 27001 A.5.24–A.5.27 | No security incident classification, severity ladder, response commitment or incident record | PLATFORM | `MOS-CONF-323` |
| D-10 | ISO/IEC 27001 A.8.17 | No clock synchronisation requirement, though three existing requirements assume one | PLATFORM | `MOS-CONF-324` |
| D-11 | ISO/IEC 27001 A.5.35, A.8.29 | No independent review and no penetration test before clinical deployment | SITE | `MOS-CONF-326` |
| D-12 | ISO/IEC 27001 A.5.18 | No periodic access review and no report to support one | SITE | `MOS-CONF-321` (name it as a SITE obligation in `DEPLOYMENT.md`) |
| D-13 | ISO/IEC 27001 A.8.31 | Environment is a carried value, but no requirement forbids production credentials, datasets or PACS endpoints in non-production | PLATFORM | none in §18.6 — a one-line requirement belongs in ch 8 |
| D-14 | ISO/IEC 42001 A.5.2–A.5.5 | No AI system impact assessment: the phrase appears nowhere; the inputs exist, the assessment and its gate do not | PUBLISHER | `MOS-CONF-341` |
| D-15 | ISO/IEC 42001 A.8.4 | No AI-incident notion, recipient list or communication record | PUBLISHER, SITE | `MOS-CONF-323` (security half only) |
| D-16 | IEC 62304 SOUP (via ISO 13485 7.3.3 and `MOS-CONF-302`) | No published anomaly list for released software; the specification defect register is not one and MUST NOT be offered as one | PLATFORM | `MOS-CONF-302` |

### 18.7 IMDRF SaMD, GMLP and the FDA Predetermined Change Control Plan

#### 18.7.1 IMDRF is a regulators' forum, not a scheme one certifies against

The International Medical Device Regulators Forum publishes **guidance documents** agreed among its member
regulators. It issues no certificates, accredits no bodies, and has no conformity-assessment procedure. Its
documents matter because national regulators adopt the vocabulary and the reasoning: the FDA's digital-health
guidance, MDCG 2019-11, and Health Canada's SaMD guidance all speak IMDRF. Conformance is therefore always
*indirect* — an IMDRF-shaped argument presented to a regulator that accepts it.

**MOS-CONF-400** No MedicalOS artifact — UI string, API field, `ValidationReport`, marketing page, repository
document — MUST state or imply "IMDRF compliant", "IMDRF certified", "IMDRF conformant" or any equivalent. This
is the same class of prohibited claim as the strings banned by `MOS-EVID-005` and `MOS-SAFE-008`, and CI SHOULD
extend the `MOS-EVID-005` grep to cover the literal `IMDRF` followed within 40 characters by `compliant`,
`certified`, `conformant` or `approved`.

**MOS-CONF-401** The specification MAY and does import IMDRF **vocabulary** as a structural convenience. It
already does so: `risk_classification.imdrf` on the `ServiceVersion` manifest (`MOS-SAFE-014`) carries the two
IMDRF/SaMD WG/N12 axes, and the derivation table of `MOS-SAFE-019` is the N12 category matrix. Importing a
vocabulary is not a conformance claim, and `MOS-SAFE-019` already says so verbatim ("This is a structural
consistency check, not a regulatory determination").

**MOS-CONF-402** Every IMDRF obligation in §18.7 attaches to the **PUBLISHER**. There is no IMDRF obligation on
the PLATFORM, because IMDRF's subject is a SaMD and the platform is not one (`MOS-CORE-005`, `MOS-SAFE-001`).
The platform's role is confined to carrying, verifying and displaying the publisher's declarations.

**MOS-CONF-403** IMDRF document numbers and revision years cited in this chapter MUST be re-verified against the
current IMDRF publication list before any submission that relies on them. The numbering of the AI/ML working
group's outputs in particular has changed between proposed and final issues, and a stale citation in a
submission is a finding.

#### 18.7.2 N10 — key definitions, and where each one lands here

IMDRF/SaMD WG/N10 defines SaMD as software intended for one or more medical purposes that performs those
purposes *without being part of* a hardware medical device, and notes explicitly that SaMD may run on
general-purpose computing platforms and may be used in combination with other products, including other medical
devices. MedicalOS is precisely that general-purpose combination environment.

| N10 term | What it means | Where it lands in this specification | Owner | Status |
|---|---|---|---|---|
| **Software as a Medical Device (SaMD)** | software performing a medical purpose, not part of a hardware device | the `ServiceVersion`, not MedicalOS | PUBLISHER | SATISFIED — `MOS-CORE-005`, `MOS-SAFE-003`, `MOS-SAFE-004` assign the device role to the publisher and nothing else |
| **SaMD Definition Statement** | a statement of significance of information + healthcare situation, plus intended use | `clinical.intended_use` + `risk_classification.imdrf` | PUBLISHER | SATISFIED — `MOS-SAFE-013`, `MOS-SAFE-014`, `MOS-SAFE-019`; fail-closed at registration |
| **"without being part of"** | software not necessary for a hardware device to achieve its purpose | services consume DICOM and return a `ResultBundle`; no hardware control path exists | PLATFORM | SATISFIED — `MOS-SAFE-073` default-DENY posture; `MOS-SAFE-011` refuses `autonomy: autonomous` |
| **general-purpose computing platform** | the non-device substrate SaMD runs on | MedicalOS core, Triton, the host OS | PLATFORM | SATISFIED — this is the structural ruling of `MOS-CORE-005`, restated in `MOS-CONF-404` |
| **used in combination** | SaMD as a module inside a larger product | `Deployment` of a `ServiceVersion` into a tenant/environment | SITE | PARTIAL — the combination is recorded (`MOS-SAFE-046`) but no requirement obliges the site to assess the *combination* as a system |
| **changes to SaMD** | modification of the software after release | a new `ServiceVersion`/`ModelVersion`, never an edit | PUBLISHER | SATISFIED — `MOS-TRAIN-013`, `MOS-SAFE-021`, `MOS-SAFE-031`, `MOS-EVID-064` |

**MOS-CONF-404** MedicalOS MUST be described, in any document a publisher submits that names it, as the
**general-purpose computing platform** on which the SaMD runs, in the N10 sense — and, in the publisher's IEC
62304 file, as **SOUP**. These two descriptions are consistent and the publisher MUST use both: N10 positions the
platform relative to the device, 62304 positions it relative to the device's software file.

**MOS-CONF-405** The platform MUST NOT generate, infer or default any member of the SaMD Definition Statement.
`MOS-SAFE-009` already forbids this for `intended_use`, `regulatory_status` and `legal_manufacturer`; this
requirement extends the same posture to `risk_classification.imdrf.healthcare_situation` and
`risk_classification.imdrf.information_significance`, which are publisher judgements. The platform's only act on
them is the consistency check of `MOS-SAFE-019`.

**MOS-CONF-406** A `Deployment`'s combination context — which other services run on the same studies, which
viewer renders the output, which reading paradigm the site actually operates — is a **SITE** obligation and is
currently unrecorded beyond `MOS-SAFE-046`. This is logged as a gap in `MOS-CONF-407`.

**MOS-CONF-407** GAP (SITE). No requirement obliges a site to record or assess the *combination* of services
operating on the same study. Two capabilities producing contradictory findings on one study — an effusion
quantifier reporting 800 mL beside a nodule detector that segmented the same opacity as a mass — is a system-level
hazard with no owner in this specification. A future revision SHOULD require a per-`(tenant, capability set)`
combination record; 0.2.0 does not have one.

#### 18.7.3 N12 — risk categorisation, worked for two real capabilities

N12 categorises a SaMD on two axes and nothing else:

1. **Significance of the information provided to the healthcare decision** — `treat_or_diagnose` >
   `drive clinical management` > `inform clinical management`.
2. **State of the healthcare situation or condition** — `critical` > `serious` > `non_serious`.

The resulting I–IV matrix is exactly the table `MOS-SAFE-019` already carries. Category increases with both axes;
IV is the highest.

| significance \ situation | `non_serious` | `serious` | `critical` |
|---|---|---|---|
| `inform` | I | I | II |
| `drive` | I | II | III |
| `treat_or_diagnose` | II | III | IV |

##### Worked derivation — `pleural_effusion` (volumetric quantifier)

The specification's worked example service (`pulmo.pleural-effusion` 1.4.0, Chapter 9) declares
`intended_setting: [inpatient, emergency_department]`, `reading_paradigm: concurrent_read`,
`autonomy: assistive`, and outputs a SEG plus a TID 1500 SR carrying left/right/total volume in millilitres.

- **Situation.** An effusion in an inpatient or ED population is a condition where timely intervention matters
  and where a major therapeutic intervention (thoracentesis, chest drain) may be indicated, but which is not of
  itself immediately life-threatening in the general case. That is `serious`. A publisher who scoped the product
  to haemodynamically unstable patients or to massive effusion triage would have to argue `critical`.
- **Significance.** The volume is one input to a drainage decision that a radiologist and a treating clinician
  make; the product does not state a diagnosis and does not define the intervention. Under a concurrent-read
  paradigm with an assistive autonomy declaration, that is `drive clinical management`, not
  `treat_or_diagnose`. A publisher who marketed the number as *the* drainage criterion would be claiming
  `treat_or_diagnose`.
- **Derivation.** `drive` × `serious` → **Category II**. Under the aggressive scoping, `drive` × `critical` →
  III; under the aggressive claim, `treat_or_diagnose` × `serious` → III.

##### Worked derivation — `lung_nodule` (detector)

- **Situation.** The target condition is a potentially malignant pulmonary nodule. Lung cancer is not an
  immediately life-threatening state at the point of detection and is frequently curable when found early;
  accurate detection is nonetheless what makes that curability real. `serious` is the defensible axis value.
  `critical` is arguable only for a product scoped to a staging or acutely symptomatic population.
- **Significance.** A detector operating as a concurrent read aids a radiologist who independently reads the
  study and assigns the Lung-RADS or Fleischner follow-up. The output drives the follow-up interval; it does not
  by itself diagnose. `drive clinical management`. A product positioned as an **autonomous or stand-alone second
  reader** whose output determines the follow-up without an independent human read would be `treat_or_diagnose` —
  and MedicalOS refuses to run it anyway (`MOS-SAFE-011`).
- **Derivation.** `drive` × `serious` → **Category II**; `treat_or_diagnose` × `serious` → III under the
  stand-alone framing.

**MOS-CONF-408** Both worked derivations land at **Category II** under this specification's constraints, and
neither can reach IV while `MOS-SAFE-011` refuses `autonomy: autonomous`. This is a property of the platform's
posture, not of the capabilities: Category IV requires `treat_or_diagnose` × `critical`, and a
`treat_or_diagnose` claim from an assistive concurrent-read product is not coherent.

**MOS-CONF-409** The platform MUST NOT use `risk_classification.imdrf.category` to relax any gate, widen any
envelope, reduce any acceptance criterion, or alter any default. The category is declared, digested under
`intended_use_digest` (`MOS-SAFE-021`), displayed, and carried in provenance. It MUST NOT become an input to
`evaluate_gate()` (`MOS-EVID-090`) or to the E1 conditions of `MOS-SAFE-036`. A category is a regulator's
navigation aid, not a permission.

**MOS-CONF-410** GAP (PUBLISHER, unenforced). `MOS-SAFE-019` checks that the declared category is *arithmetically
consistent* with the two declared axes. Nothing checks that the two axes are consistent with
`intended_use.statement`, `intended_use.reading_paradigm` or `indications[]`. A publisher may declare
`inform` × `non_serious` → I on a product whose own intended-use statement describes driving a drainage decision,
and the platform will accept it. A future revision SHOULD add a registration-time warning event
(`service.clinical.imdrf_axis_divergence`) on the model of `MOS-SAFE-018`.

#### 18.7.4 N23 — the three legs of clinical evaluation, against Chapter 7

IMDRF/SaMD WG/N23 decomposes clinical evaluation of a SaMD into three questions:

1. **Valid clinical association** — is there a valid clinical association between the SaMD's output and the
   targeted clinical condition?
2. **Analytical validation** — does the SaMD correctly process input data to generate accurate, reliable and
   precise output data?
3. **Clinical validation** — does the use of that output achieve the intended purpose in the target population in
   the context of clinical care?

This is the closest existing fit between an external framework and this specification. Chapter 7 is, almost
exactly, an implementation of leg 2 with hooks for leg 3 and nothing at all for leg 1.

**A warning before the table.** Chapter 7's three-way split — `vendor_evidence`, `site_acceptance`,
`monitoring_period` (`MOS-EVID-003`) — is **not** N23's three legs. The coincidence of the number three has
already produced this error in other documents. `MOS-EVID-003`'s axis is *who produced the evidence and when*;
N23's axis is *what question the evidence answers*. A `vendor_evidence` report carries analytical validation and
may carry clinical validation; a `site_acceptance` report carries neither and says so (`MOS-EVID-131`); a
`monitoring_period` report is structurally forbidden from carrying a verdict at all (`MOS-EVID-142`).

| N23 leg | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| **1. Valid clinical association** | literature, guidelines, prior evidence that the measured quantity relates to the condition; the output's clinical meaning | PUBLISHER | `intended_use.statement` and `indications[].codes[]` (`MOS-SAFE-014`); coded concepts resolve through `capability_concepts` (`MOS-EVID-143`) | **GAP** — there is no entity, field or artifact anywhere in the specification that carries a literature basis, a guideline citation or a prior-evidence argument. `indications[]` is a claim, not its justification |
| **2a. Analytical — correct input processing** | the device consumes the input it was designed for, deterministically | PUBLISHER (declares), PLATFORM (enforces) | `SeriesSelector` triage (spine §6); `ApplicabilityEnvelope` `MOS-EVID-095`–`MOS-EVID-104`; geometry contract `MOS-IMG-039`, `MOS-IMG-040`; `PreprocessingSpec` one-implementation rule and golden-fixture self-test `MOS-IMG-054`, `MOS-REG-036` | SATISFIED |
| **2b. Analytical — accurate, reliable output** | measured performance against a reference standard, with reproducibility | PUBLISHER | `EvaluationRun` full binding `MOS-EVID-061`; per-case persistence `MOS-EVID-065`; recomputability `MOS-EVID-066`; metric registry `MOS-EVID-054`; operating point mandatory `MOS-EVID-055`; `n`/CI/convention companions `MOS-EVID-056`; patient-cluster bootstrap `MOS-EVID-057` | SATISFIED — this is the strongest correspondence in the document |
| **2c. Analytical — precision / repeatability** | same input, same output; backend and version stability | PUBLISHER + PLATFORM | `MOS-EVID-062` (no dirty tree); `MOS-EVID-064` (a backend conversion is a new artifact and a new run); `MOS-TRAIN-153`, `MOS-TRAIN-165` (converted version evaluated through the serving stack, and an E1/E2/E3 pass is explicitly *not* clinical equivalence); `MOS-TRAIN-014` (no runtime mutation) | SATISFIED |
| **2d. Analytical — reference standard quality** | the ground truth is defensible | PUBLISHER | `AnnotationSet` reader identity and consensus rule `MOS-EVID-038`, `MOS-EVID-039`; independence from the artifact under test `MOS-EVID-042`; inter-reader agreement mandatory and reported `MOS-EVID-043` | SATISFIED |
| **3. Clinical validation** | the output, used as intended, achieves the intended purpose in the target population in real clinical care | PUBLISHER | the platform explicitly refuses to perform or confer it (`MOS-EVID-001`, `MOS-EVID-002`); it **carries** the publisher's own report via `clinical.clinical_evidence` (`MOS-SAFE-014`) and gates on its verification (`MOS-SAFE-036`) | **PARTIAL** — carried and verified, never produced. Correct division of labour, but a publisher reading Chapter 7 as its clinical-evaluation plan will submit an analytical file and call it clinical evidence |

**MOS-CONF-411** A `ValidationReport` of `kind: vendor_evidence` MUST NOT be represented, by the platform or in
any generated document, as a clinical evaluation under N23. It is evidence for leg 2. `MOS-EVID-002`'s verbatim
bounding sentence already carries this; this requirement names the framework it bounds against.

**MOS-CONF-412** GAP (PUBLISHER, no platform hook). The specification carries no representation of N23 leg 1.
A future revision SHOULD add an optional `clinical_association` member to the `clinical` block —
`{statement, references[], evidence_class}` where `evidence_class` ∈ `guideline`, `systematic_review`,
`primary_literature`, `mechanistic`, `none_declared` — digested under `intended_use_digest` and surfaced by
`GET /api/v1/service-versions/{id}/clinical` (`MOS-SAFE-022`). Until then, leg 1 lives entirely outside the
platform and a reviewer will ask where it is.

**MOS-CONF-413** GAP (PUBLISHER). Nothing in the specification requires, records or verifies a **clinical
investigation or reader study**. `MOS-SAFE-071` and `MOS-SAFE-072` are explicit that `ResultReview` aggregates
are "reviewer agreement, not a validated performance estimate" and MUST NOT enter a `ValidationReport`; that is
the right answer for a monitoring signal and it means the platform produces no clinical-validation evidence of
any kind. This is by design (`MOS-EVID-001`) and is nonetheless the largest single hole between this
specification and a marketing submission.

#### 18.7.5 Machine-learning-enabled MDSW: the locked-algorithm property

The IMDRF AI/ML working group's key-terms document distinguishes a **locked** algorithm — one that produces the
same output for the same input every time — from an **adaptive** or **continuously learning** one that changes
its behaviour in the field. Regulators treat the two entirely differently, and the adaptive case is exactly what
a PCCP exists to make submittable.

| Term | Definition in the guidance | Owner | Satisfied by | Status |
|---|---|---|---|---|
| **Locked algorithm** | fixed function of input; changes only through a controlled release | PLATFORM (enforces) + PUBLISHER (declares) | `MOS-TRAIN-014` forbids online learning, continual learning, federated averaging into a live artifact, runtime calibration fitting and adapter hot-swap in 0.1.0–0.4.0; `MOS-TRAIN-015` and `MOS-SVC-058` forbid a container mutating its weights; `MOS-IMG-054` refuses to serve on a preprocessing hash mismatch | SATISFIED — mechanically, not by policy |
| **Adaptive / continuously learning algorithm** | updates itself from field data | PLATFORM | **not supported — refused by construction** | NOT-APPLICABLE — `MOS-TRAIN-005` forbids the edge outright; `MOS-EVID-139` forbids a drift alert changing deployment state; `MOS-SAFE-063` forbids a `ResultReview` retraining anything |
| **Training / tuning / test data** | the three disjoint roles | PUBLISHER | `MOS-EVID-030` fixes the partition vocabulary to `train`/`tune`/`test`; `MOS-EVID-029` makes the unit of assignment `patient_key`; `MOS-EVID-033` bars every form of selection on `test` | SATISFIED |
| **Model / model version** | the versioned learned artifact | PUBLISHER | `ModelVersion` (`MOS-REG-030`), immutable, digest-addressed; `MOS-TRAIN-013` requires a new version for any change | SATISFIED |
| **Real-world performance monitoring** | observing the deployed model | SITE (runs) + PUBLISHER (acts) | `MOS-EVID-135`–`MOS-EVID-142`: unattended monitoring, signed 30-day period reports, the signal table of `MOS-EVID-137` | PARTIAL — signals exist; no obligation on anyone to *act* on them beyond suspension (`MOS-EVID-139`) |

**MOS-CONF-414** Every `ModelVersion` served by MedicalOS is a **locked algorithm** in the IMDRF sense, and a
publisher MUST be able to state this in a submission with the platform's mechanism as its evidence:
`MOS-TRAIN-014`, `MOS-TRAIN-015`, `MOS-SVC-058` and `MOS-IMG-054`, plus the CI assertions of `MOS-TRAIN-017`.
This is a genuinely strong position and it MUST NOT be weakened by a configuration flag in any release.

**MOS-CONF-415** The `medicalos-verify` offline verifier (`MOS-EVID-122`, `MOS-EVID-123`) is the platform's
answer to "prove the artifact that produced this number is the artifact you deployed". A publisher SHOULD cite it
by name in the analytical-validation section of a submission, and a site SHOULD run it as part of incoming
inspection.

#### 18.7.6 GMLP — the ten guiding principles

The **Good Machine Learning Practice for Medical Device Development: Guiding Principles** were published jointly
by the FDA, Health Canada and the MHRA. They are principles, not requirements: nothing is certified against them
and no clause numbers exist. They are nonetheless the checklist a reviewer will read alongside a submission, and
four of the ten map onto this specification unusually well.

| # | Principle | Owner | Satisfied by | Status |
|---|---|---|---|---|
| 1 | Multi-disciplinary expertise is leveraged throughout the total product life cycle | PUBLISHER | `MOS-EVID-038`/`MOS-EVID-039` name readers and the consensus rule; `MOS-EVID-117` requires an identifiable human approver with a role and organisation; `MOS-TRAIN-173` splits promotion into three distinct human acts with three permissions; `MOS-SAFE-104` `clinically_qualified` is the only input by which a review counts as a qualified human read | **PARTIAL** — the *decision points* are staffed and audited; nothing records who was involved in design, risk analysis or intended-use definition, and there is no design-history entity |
| 2 | Good software engineering and security practices are implemented | PLATFORM (core) + PUBLISHER (service) | `MOS-REL-001` signed tag + digest-pinned images + SPDX SBOM + cosign; `MOS-SEC-138`/`MOS-SEC-140`/`MOS-SEC-141` signature verification blocking and offline-capable; `MOS-SEC-142` SBOM per image and per model artifact; `MOS-TEST-002` every requirement id mapped to at least one executable check | **PARTIAL** — engineering and supply-chain practice is strong and falsifiable; there is no IEC 62304-shaped lifecycle, no SOUP inventory and no risk file, and `MOS-OPEN-025` records that as an undecided default rather than a commitment |
| 3 | Clinical study participants and data sets are representative of the intended patient population | PUBLISHER | `MOS-EVID-024` mandatory `acquisition_profile` on every sealed `DatasetVersion`; `MOS-EVID-025` percentile conventions; `MOS-EVID-067` mandatory strata including `patient_age_years` and `patient_sex`; `MOS-TRAIN-088` corpus stratification check as a blocking gate at seal time with C1–C7 and no silent waiver (`MOS-TRAIN-091`); `MOS-TRAIN-089` institution-key hashing | **PARTIAL** — representativeness is *measured, persisted and gated at the corpus level*, which is more than most specifications do. But no requirement obliges a **demographic subgroup floor** to exist in an `AcceptanceCriteria`; a capability may gate on `reference_volume_ml` decile and never on sex or age and still pass |
| 4 | Training data sets are independent of test sets | PUBLISHER | **the single best correspondence in the document.** `MOS-EVID-028` split is a materialised manifest, never a seed; `MOS-EVID-029` patient-level assignment enforced at write time; `MOS-EVID-031` every patient accounted for, silent omission is a write error; `MOS-EVID-034` five leakage checks blocking at freeze time; `MOS-EVID-036` no silent waiver; `MOS-EVID-033` `test` barred from threshold, checkpoint, architecture and search selection; `MOS-TRAIN-214` confines a `ConfigurationSearch` to `train`/`tune`; `MOS-TRAIN-216` one nominated trial; `MOS-TRAIN-207` the trainer is not granted read on acceptance cohorts | **SATISFIED** |
| 5 | Selected reference datasets are based upon best available methods | PUBLISHER | `MOS-EVID-038` ≥1 named reader; `MOS-EVID-039` six enumerated consensus rules with their parameters (STAPLE records iterations, tolerance, priors and seed); `MOS-EVID-041` masks in source geometry; `MOS-EVID-042` the reference MUST NOT derive from the artifact under test, and an algorithmic reference cannot be `reference_of_record`; `MOS-EVID-043` inter-reader agreement mandatory and reported | **SATISFIED** |
| 6 | Model design is tailored to the available data and reflects the intended use of the device | PUBLISHER | `MOS-TRAIN-211` dataset-fingerprint auto-configuring backend as the default for label outputs; `MOS-TRAIN-223` the derived configuration transcribed into the registered `PreprocessingSpec`; `MOS-EVID-095`–`MOS-EVID-098` the envelope is *derived from the evaluation cohort* and cannot be widened without a run covering the widening; `MOS-EVID-099` `not_validated_for` must be consistent with the envelope, asserted in CI | **SATISFIED** — `MOS-EVID-098` is the clause that makes "validated on 2.5 mm archival data, declared for 0.6 mm" fail mechanically |
| 7 | Focus is placed on the performance of the human-AI team | PUBLISHER | `MOS-SAFE-014` `reading_paradigm` is a required declared enum; `MOS-SAFE-011` assistive only; `MOS-SAFE-057` results are stored and visible, nothing auto-actioned; `MOS-SAFE-012` manufacturer, version, mode and review status shown adjacent to every finding | **GAP** — every one of those is about *positioning*, not performance. Nothing in the specification measures a reader with the tool against a reader without it. No MRMC design, no reader-study entity, no place to put the result. An `EvaluationRun` measures the model alone, by construction (`MOS-EVID-061`) |
| 8 | Testing demonstrates device performance during clinically relevant conditions | PUBLISHER + SITE | `MOS-EVID-067` mandatory strata incl. `slice_thickness_mm`, `convolution_kernel_class`, `manufacturer`, `contrast_phase`; subgroup floors and catastrophic-case counts as part of the gate (`MOS-EVID-088`); `MOS-EVID-051`/`MOS-EVID-052` empty-ground-truth cases excluded from Dice but separately gated; SAT-4 site cohort check with its mandatory power disclaimer (`MOS-EVID-131`) and envelope coverage against real site traffic (`MOS-EVID-132`) | **PARTIAL** — technically clinically-relevant *conditions* are covered well; clinically relevant *use* is not, for the reason in row 7 |
| 9 | Users are provided clear, essential information | PUBLISHER (declares) + PLATFORM (surfaces) | `MOS-SAFE-014` requires ≥1 entry each in `known_limitations`, `not_validated_for` and `known_failure_modes`, fail-closed (`MOS-SAFE-015`); `MOS-SAFE-016` renders an undeclared training population as an explicit string, never a hidden null; `MOS-SAFE-012` five facts adjacent to every finding without interaction; `MOS-SAFE-022` the whole block as a one-page data sheet over the API; `MOS-EVID-072` every displayed performance number renders from a `capability_claims` row and links to its `EvaluationRun` | **SATISFIED** — and `MOS-SAFE-015`'s refusal of an empty `not_validated_for` list is the mechanism that makes it real |
| 10 | Deployed models are monitored for performance and re-training risks are managed | SITE (monitors) + PUBLISHER (acts) + PLATFORM (mechanism) | `MOS-EVID-135` unattended monitoring with a signed 30-day report; `MOS-EVID-137` the signal table (envelope-out rate, input and output PSI, plausibility fail rate, review disagreement rate, review coverage, p95 latency); `MOS-EVID-136` monitoring has no ground truth and MUST NOT present disagreement as accuracy; `MOS-EVID-139` alert notifies, hard breach suspends; `MOS-EVID-140` recovery requires a human and re-runs SAT-1–SAT-3; **re-training risk**: `MOS-TRAIN-005` forbids the automated loop, `MOS-TRAIN-085`–`MOS-TRAIN-087` measure model-seeded fraction and corpus generation and bar generation ≥ 2 | **SATISFIED** — the feedback-loop trap (`MOS-TRAIN-087`) is handled better here than the principle asks |

**MOS-CONF-416** Principles 4, 5, 6, 9 and 10 are **SATISFIED** and a publisher MAY cite the named requirement
ids as evidence in a submission. Principles 1, 2, 3 and 8 are **PARTIAL** and principle 7 is a **GAP**; a
publisher MUST NOT cite them as satisfied and MUST supply its own evidence.

**MOS-CONF-417** GAP (PUBLISHER, highest-value). GMLP principle 7 has no representation in this specification.
Closing it requires a `ReaderStudy` entity — design (fully-crossed MRMC or sequential), readers with their
experience levels, cases with and without AI assistance, the reading order and washout, and the endpoint
(typically ΔAUC or Δsensitivity at matched specificity). It is the single most expensive missing item for a US
submission of a CADe product, and nothing in the evidence plane is shaped to hold it. It is out of scope for
0.2.0 and MUST be recorded as such rather than approximated with `ResultReview` statistics, which `MOS-SAFE-072`
explicitly forbids.

**MOS-CONF-418** GAP (PUBLISHER + PLATFORM). GMLP principle 3 is measured but not gated on demographics. A
future revision SHOULD require that an `AcceptanceCriteria` set for a `clinical_use_mode: clinical` capability
declare at least one subgroup criterion over `patient_sex` and one over an `patient_age_years` banding, or
declare in writing why neither is applicable — on the model of `MOS-EVID-052`, which already refuses a
`dice_mean_per_case` criterion that lacks a companion empty-GT criterion. The strata are already persisted
(`MOS-EVID-067`); only the obligation is missing.

#### 18.7.7 FDA Predetermined Change Control Plan (PCCP)

**This is the most commercially important item in this chapter.** A PCCP is a section of a marketing submission
in which a manufacturer describes, *in advance*, changes it intends to make to an already-authorised device and
the methods by which it will make and verify them. If the FDA authorises the PCCP, changes made within it do not
require a new submission. For an ML-enabled device this converts "every retrain is a new 510(k)" into "retrains
inside the envelope are a documented internal act" — which is the difference between a product that can be
improved and one that is frozen at clearance.

A PCCP has exactly three parts.

| Part | What it must contain | What this specification already provides | Owner | Status |
|---|---|---|---|---|
| **1. Description of Modifications** | the specific, enumerated, bounded set of changes to be made; what is *not* covered | `MOS-EVID-094` enumerates exactly what re-opens the gate: ServiceVersion/ModelVersion, `PreprocessingSpec` version, any operating threshold, `AcceptanceCriteria` version, tenant acceptance binding, inference backend, accelerator class. `MOS-TRAIN-013` states the general rule that each such change is a new `ModelVersion`. `MOS-EVID-064` fixes that a backend conversion is a distinct numerical artifact | PUBLISHER | **PARTIAL** — the specification enumerates the *change taxonomy* precisely, which is the hard half. It has no artifact in which a publisher declares, ex ante, which subset of that taxonomy it intends to exercise |
| **2a. Modification Protocol — data management** | how data are collected, curated, split, and kept independent across updates | `MOS-TRAIN-072`–`MOS-TRAIN-077` `TrainingDataPolicy`: legal basis, scope, expiry, revocation, `permits_redistribution`, with harvest refused without it and no administrator bypass. `MOS-TRAIN-202`–`MOS-TRAIN-205` curation as a materialised list with every exclusion retained and a closed reason vocabulary that MUST NOT contain a model outcome. `MOS-EVID-015`/`MOS-EVID-016` sealed, content-addressed `DatasetVersion`s that cannot be a query. `MOS-EVID-022`/`MOS-EVID-023` derivation recorded; adding cases produces a new version | PUBLISHER | **SATISFIED** |
| **2b. Modification Protocol — re-training practices** | how the model is retrained, with what confinement | `MOS-TRAIN-124` `TrainingRun` binds every input that can change the artifact. `MOS-TRAIN-214`–`MOS-TRAIN-216` a `ConfigurationSearch` is confined to `train`/`tune` and nominates exactly one trial. `MOS-TRAIN-220` the search space is a declarative digested document, never a callable. `MOS-TRAIN-234`/`MOS-TRAIN-235` bounded, recorded, reproducible budget. `MOS-TRAIN-005` the closed-loop prohibition | PUBLISHER | **SATISFIED** |
| **2c. Modification Protocol — performance evaluation** | the acceptance criteria the updated model must meet, and the comparison against the incumbent | `MOS-EVID-076`–`MOS-EVID-079` `AcceptanceCriteria` as a declarative grammar with no dynamic code path, `ci_lower_95` as the default bound, thresholds pinned. `MOS-EVID-085`–`MOS-EVID-089` **paired non-inferiority regression** with a declared margin δ, a written `margin_rationale`, `INDETERMINATE` on unpaired runs, and a catastrophic-case count. `MOS-TRAIN-177` the incumbent is re-run on the *same* cohort. `MOS-EVID-091` every gate invocation persisted append-only | PUBLISHER | **SATISFIED** — this is the part most PCCPs are weakest on and this specification is strongest on |
| **2d. Modification Protocol — update procedures** | how the update reaches users, how it is verified at the site, how it is rolled back, how users are told | `MOS-TRAIN-181` the five-step promotion with steps 4 and 5 each requiring a distinct human act. `MOS-TRAIN-184` `auto_promote` refused for clinical. `MOS-EVID-128`–`MOS-EVID-134` site acceptance testing against the vendor report, including fixture equality (`MOS-EVID-130`) and envelope coverage on real traffic (`MOS-EVID-132`). `MOS-TRAIN-185`–`MOS-TRAIN-188` rollback without re-approval, with the incumbent kept warm for a declared window. `MOS-SAFE-012` version and manufacturer adjacent to every finding | PUBLISHER + SITE | **PARTIAL** — deployment, verification and rollback are complete. **Nothing obliges anyone to tell a clinical user that the model behind their reads changed.** There is a `deployment.updated` webhook and the version is rendered beside each finding; there is no change notice, no release note obligation and no acknowledgement |
| **3. Impact Assessment** | benefits and risks of each modification; effect on other parts of the device; effect on existing risk controls; how the protocol keeps the device safe and effective | `MOS-TRAIN-176` the 15-item approval dossier — incumbent comparison, leakage with waivers, per-criterion verdict, regression with margin rationale, strata with underpowered ones marked, seed variance beside selection margin, conversion equivalence, **envelope diff with widened bounds flagged** (item 11), publisher declarations verbatim, plausibility firing rates, and the explicit scope of the decision (item 14). `MOS-EVID-036`/`MOS-TRAIN-091` reproduce every waiver in full | PUBLISHER | **PARTIAL** — the dossier is a genuine, rigorous impact assessment of the *artifact swap*. It is not an assessment of the *modification class* against a hazard analysis, because there is no hazard analysis: this specification contains no ISO 14971 risk file, no hazard identifiers, and no traceability from a change to the risk controls it touches |

##### What a PCCP still needs that this specification does not have

**MOS-CONF-419** GAP (PUBLISHER). There is no `ChangeControlPlan` artifact. A publisher can demonstrate every
individual mechanism a PCCP's Modification Protocol requires and still have no document that binds them into a
plan, declares its boundary, and is versioned with the cleared device.

**MOS-CONF-420** GAP (PUBLISHER). There is no ISO 14971 risk file and therefore no impact assessment in the
regulatory sense. `MOS-SAFE-014`'s `known_failure_modes[]` carries `{id, text, detection, mitigation}` and is the
nearest thing to a hazard register the specification has; it is a publisher declaration surfaced to users, not a
risk-management file, and nothing traces a modification to the failure modes it could affect.

**MOS-CONF-421** GAP (PUBLISHER + PLATFORM). No requirement notifies a site or a clinical user that the model
behind a clinical deployment changed. `MOS-SAFE-012` makes the version *visible*; visibility is not notification.
For a PCCP this matters directly: transparency to users about updates made under the plan is part of what the
FDA assesses.

##### The minimum additions that would make a PCCP expressible

These are the only genuinely new engineering requirements this chapter creates, and they are declarative
wrappers over machinery that already exists.

**MOS-CONF-422** A publisher MAY supply a `ChangeControlPlan` alongside a `ServiceVersion`. When supplied it
MUST be a signed, JCS-canonicalised (`MOS-EVID-008`) document carrying exactly three top-level members —
`modifications`, `protocol`, `impact_assessment` — digested under `intended_use_digest` (`MOS-SAFE-021`) so that
editing it requires a new `ServiceVersion`. The platform MUST store, digest, display and reproduce it in
provenance, and MUST NOT interpret or assess it. This is the same posture `MOS-SAFE-029` takes toward
`regulatory_status` and `MOS-TRAIN-074` takes toward `legal_basis`, for the same reason.

**MOS-CONF-423** `modifications` MUST be an array drawn from a closed vocabulary that is exactly the change set
`MOS-EVID-094` already enumerates: `weights_retrain`, `preprocessing_spec_version`, `operating_threshold`,
`acceptance_criteria_version`, `inference_backend`, `accelerator_class`, `envelope_bounds`. A value outside that
set MUST be rejected at registration. A plan that does not name a change type does not cover it.

**MOS-CONF-424** `protocol` MUST cite, by requirement id, the mechanism it relies on for each of the four
Modification Protocol parts, and MUST name: the `AcceptanceCriteria` version that updates will be gated against,
the regression `margin` (δ) and its `margin_rationale` (`MOS-EVID-087`), and the `DatasetVersion`
`Dataset.purpose = acceptance` lineage the updates will be evaluated on. A protocol that does not pin δ in
advance is a protocol that can be tuned to pass a specific candidate, which `MOS-EVID-087` already forbids at the
criteria level and which MUST be forbidden here at the plan level.

**MOS-CONF-425** When a `ChangeControlPlan` is present, `evaluate_gate()` (`MOS-EVID-090`) MUST record on the
`deployment_gate_decisions` row (`MOS-EVID-091`) which `modifications[]` entry the candidate exercises, and MUST
return `INDETERMINATE` with reason `change_outside_declared_plan` when the diff against the incumbent exercises a
change type the plan does not name. It MUST NOT return `FAIL`: a change outside the plan is not a defective
candidate, it is a candidate that needs a different regulatory route, and conflating the two would push
publishers to widen the plan rather than file.

**MOS-CONF-426** A `ChangeControlPlan` MUST NOT be treated by the platform as evidence of anything. Its presence
MUST NOT relax `MOS-SAFE-036`'s E1 conditions, MUST NOT substitute for a `ValidationReport`, and MUST NOT permit
`promotion_policy.auto_promote` where `MOS-TRAIN-184` forbids it. An FDA-authorised PCCP removes a *submission*
obligation from the publisher; it removes nothing from the platform's gate.

**MOS-CONF-427** The platform SHOULD emit `deployment.service_version.changed` to the site's configured webhook
on every cutover (step 5 of `MOS-TRAIN-181`), carrying `deployment_id`, the outgoing and incoming
`service_version`, the `validation_report_digest` of the incoming version, the `CriteriaVerdict`, and the
`modifications[]` entry exercised when a `ChangeControlPlan` is present. This is the smallest change that closes
`MOS-CONF-421`.

---

### 18.8 EU MDR, the EU AI Act and jurisdictional routes

#### 18.8.1 MDR qualification under MDCG 2019-11

MDCG 2019-11 is the Medical Device Coordination Group's guidance on qualification and classification of software
under Regulation (EU) 2017/745 (MDR) and Regulation (EU) 2017/746 (IVDR). Its qualification test, in substance:
software is Medical Device Software (MDSW) when it has a **medical purpose of its own** and performs an action on
data **beyond storage, archival, communication, simple search, or lossless compression**.

| Component | Action on data | Medical purpose of its own? | Qualification | Owner | Status |
|---|---|---|---|---|---|
| A `ServiceVersion` producing findings, masks and measurements | segmentation, detection, quantification | yes — declared in `intended_use.statement` | **MDSW** | PUBLISHER | SATISFIED — `MOS-SAFE-013`, `MOS-SAFE-014` make the medical purpose an explicit, signed, digest-covered declaration; `MOS-SAFE-003` names the manufacturer |
| DICOM Gateway: DICOMweb proxy, tenancy filtering, de-identification, audit | communication, storage, de-identification | no | not MDSW | PLATFORM | NOT-APPLICABLE — communication and storage are named exclusions in MDCG 2019-11; de-identification removes information rather than creating it |
| Study triage and `SeriesSelector` routing | search and selection against declared constraints | no — routes, does not interpret | not MDSW | PLATFORM | NOT-APPLICABLE — simple search against publisher-declared constraints is a named exclusion; `MOS-SVC-053` forbids a service self-selecting, which keeps the act mechanical |
| Job lifecycle, queue, audit, policy engine | orchestration | no | not MDSW | PLATFORM | NOT-APPLICABLE — no action on patient data |
| Evidence plane: datasets, evaluation, reports | measurement about an artifact, not about a patient | no | not MDSW | PLATFORM | NOT-APPLICABLE — the subject of every evidence-plane measurement is a model, not a patient (`MOS-EVID-001`) |
| **Platform-side DICOM writing and measurement computation** | writes SEG/SR; computes volumes on the source grid | **contested** | **contested** | PLATFORM | **PARTIAL** — the position is arguable in both directions and untested; see `MOS-CONF-429` |

**MOS-CONF-428** The platform's qualification position is that MedicalOS core is not MDSW because no component
of it has a medical purpose of its own: it transports, governs, records and formats a publisher's clinical claim
without creating one. `MOS-CORE-005`, `MOS-SAFE-001`, `MOS-SAFE-002` and `MOS-CORE-004` state this and CI
enforces the absence of a platform performance claim (`MOS-EVID-005`, `MOS-TEST-001`).

**MOS-CONF-429** GAP (PLATFORM) — **the one soft edge in the structural ruling, stated plainly.** The platform
does not only transport the publisher's numbers. `MOS-IMG-039` and `MOS-IMG-040` place the measurement rule and
the volume formula in the platform; `MOS-EVID-044` requires one implementation of volume computation shared by
the evidence plane and the runtime; `MOS-EVID-109`'s `measurement_consistency` rule has the platform **recompute
the measurement from the shipped mask with its own code** and fail the job on disagreement (`MOS-EVID-111`); and
the platform, not the service, writes every SEG and SR (spine §2, §7). A notified body may reasonably ask whether
computing and emitting a millilitre figure that a clinician reads is "an action on data beyond storage,
communication and simple search" performed for a medical purpose. The counter-argument is the stronger one and it
is already written into the ownership boundary: the mask and the findings — the clinical claim — are entirely the
service's, `MOS-SVC-059` forbids the platform from reinterpreting, re-thresholding or re-labelling them, and the
platform applies a fixed, non-clinical geometric formula to a mask it did not produce, in the way a DICOM toolkit
does. **The argument is nonetheless untested with any regulator and MUST NOT be presented as settled.** It MUST be
put to a regulatory consultant before any submission that relies on the platform being out of scope, and it is
the highest-priority item on this chapter's backlog.

**MOS-CONF-430** Where a site or publisher cannot sustain `MOS-CONF-429`'s argument in its jurisdiction, the
fallback MUST be to treat the platform as a component within the publisher's device — that is, as **SOUP** in the
publisher's IEC 62304 file, documented, risk-assessed and anomaly-tracked by the publisher — rather than to
re-characterise MedicalOS as a device. The evidence for that treatment already exists: digest-pinned images
(`MOS-SEC-140`), SBOMs per image and model artifact (`MOS-SEC-142`), signed releases (`MOS-REL-001`), and
requirement-to-test traceability (`MOS-TEST-002`).

**MOS-CONF-431** GAP (PLATFORM). MedicalOS publishes no **known-anomaly list**. IEC 62304 clause 8.1.2 requires
a SOUP user to evaluate the SOUP's published anomaly list against its own device; a SOUP with no anomaly list
forces the publisher to evaluate nothing, which a competent auditor will notice. Publishing a release-scoped
known-anomalies document is cheap and SHOULD be added; `99-known-inconsistencies.md` is a specification-level
analogue and is not it.

#### 18.8.2 Rule 11 and why these products land IIa or IIb

MDR Annex VIII **Rule 11**, in substance: software intended to provide information used to take decisions with
diagnosis or therapeutic purposes is **class IIa**, except where such decisions have an impact that may cause
death or an irreversible deterioration of health, in which case it is **class III**, or a serious deterioration
of health or a surgical intervention, in which case it is **class IIb**. Software intended to monitor
physiological processes is IIa, except where it monitors vital physiological parameters whose variation could
result in immediate danger, which is IIb. All other software is class I.

Rule 11's structure means an MDSW that informs a diagnostic or therapeutic decision essentially never lands in
class I, and the whole argument is about which exception applies.

| Capability | Rule 11 limb | Reasoning | Plausible class | Owner | Status |
|---|---|---|---|---|---|
| `pleural_effusion` volumetric quantifier | information for a therapeutic decision | The volume feeds a drainage decision. Thoracentesis and chest-drain insertion are interventional procedures; under the "serious deterioration of health or a surgical intervention" limb this points to **IIb**. The counter-argument — that the radiologist independently confirms the effusion and its extent, so the software's contribution is quantitative refinement rather than the decision — supports **IIa** | **IIa or IIb**, with IIb the conservative assumption | PUBLISHER | PARTIAL — the declared class is transported and gated on (`MOS-SAFE-024`, `MOS-SAFE-025`, `MOS-SAFE-036`); the classification itself is asserted by the publisher and verified by nobody in this system (`MOS-SAFE-029`) |
| `lung_nodule` detector | information for a diagnostic decision | A missed nodule delays a lung-cancer diagnosis. The class III limb requires an impact that "may cause death or irreversible deterioration"; manufacturers of concurrent-read CADe products generally rebut III by pointing to the independent radiologist read and the assistive paradigm, landing **IIa**, while notified bodies frequently take the cancer-delay argument to **IIb** | **IIa or IIb**; III is arguable and is rebutted by the concurrent-read paradigm, not by the technology | PUBLISHER | PARTIAL — as above; note that the rebuttal depends on `intended_use.reading_paradigm` (`MOS-SAFE-014`), which is a declared enum the platform does not enforce against actual site practice |
| `emphysema_laa` deterministic measurement | information for a diagnostic decision | Chapter 15 places this as a deterministic measurement rather than a learned model; it is still MDSW and still Rule 11 — determinism changes the evidence, not the classification | **IIa** | PUBLISHER | PARTIAL — as above; Chapter 16 `MOS-OPEN-024` separately records that the ground truth for this capability is unsettled |
| MedicalOS core | none — not MDSW under `MOS-CONF-428` | Rule 11 applies to MDSW; if the qualification position holds, no rule applies at all | **none** | PLATFORM | **NOT-APPLICABLE** — conditional on `MOS-CONF-428` and therefore on the untested argument of `MOS-CONF-429` |

**MOS-CONF-432** The class declared in `regulatory_status[].device_class` (`MOS-SAFE-024`) is the publisher's,
asserted, never computed. `MOS-SAFE-029` already forbids the platform from interpreting or verifying it and this
chapter adds no exception. The platform MUST NOT derive a Rule 11 class from
`risk_classification.imdrf.category` or from any other field; the IMDRF matrix and Rule 11 are different
instruments with different axes and a mapping between them does not exist.

**MOS-CONF-433** A class IIa or above device requires notified-body involvement, which requires an ISO 13485 QMS
and an MDR Annex IX (or X/XI) conformity assessment. None of that is a platform obligation and none of it is
represented in this specification. A publisher reading this chapter MUST NOT infer that deploying on MedicalOS
contributes to conformity assessment; `MOS-CORE-005` explicitly forbids the platform from implying it.

#### 18.8.3 Annex I GSPRs that bear on software

Annex I is the full General Safety and Performance Requirements list and the publisher's conformity assessment
covers all of it. The subset below is the part where a platform mechanism is genuinely relevant evidence; every
row's obligation remains the PUBLISHER's.

| GSPR | What it requires (in substance) | Owner | Platform evidence available | Status |
|---|---|---|---|---|
| **1** | achieve intended performance; safe and effective; risks acceptable against benefits | PUBLISHER | `ValidationReport` with acceptance verdict (`MOS-EVID-113`, `MOS-EVID-114`), signed and offline-verifiable (`MOS-EVID-118`, `MOS-EVID-123`) | PARTIAL — performance evidenced; benefit-risk weighing is the publisher's and has no representation here |
| **3** | a risk management system across the whole lifecycle | PUBLISHER | none | **GAP** — no ISO 14971 file exists anywhere in this specification (`MOS-CONF-420`) |
| **4** | risk control measures; safety by design; state of the art | PUBLISHER + PLATFORM | applicability envelope refusing out-of-envelope studies as a clinical `REJECTED` (`MOS-EVID-100`–`MOS-EVID-102`); output plausibility gate run by the platform on every result (`MOS-EVID-105`, `MOS-EVID-109`, `MOS-EVID-111`); golden-fixture refusal to serve (`MOS-IMG-054`) | PARTIAL — real, enforced risk controls exist; they are not traceable to hazards because there are none |
| **5** | reduce risks related to use error | PUBLISHER | `MOS-SAFE-012` mandatory adjacent disclosure; `MOS-EVID-102` `REJECTED` visually distinct from `FAILED`; `MOS-SAFE-041` research results excluded from clinical feeds by default | **PARTIAL** — no usability engineering process, no IEC 62366-1 usability file, no formative or summative evaluation. This is a named weakness of the specification |
| **14.1 / 14.2** | devices designed to eliminate or reduce risk from the intended use environment | SITE | `clinical_use_mode` gate (`MOS-SAFE-033`–`MOS-SAFE-043`); `accepts_research` destination control (`MOS-SAFE-040`) | **PARTIAL** — the research/clinical boundary is enforced by code rather than policy, but nothing characterises the intended use *environment* itself: no reading-room, display-calibration, network-degradation or concurrent-load assumptions are stated anywhere |
| **17.1** | repeatability, reliability and performance in line with intended use; single-fault safety | PUBLISHER + PLATFORM | determinism of UID derivation (`MOS-IMG-062`); locked algorithm (`MOS-CONF-414`); fail-closed de-identification (`MOS-DATA-037`); fail-closed Gateway (`MOS-DATA-025`); `MOS-EVID-092` incumbent stays live when a gate fails | **PARTIAL** — the platform's contribution is complete and enforced; the GSPR itself is discharged by the publisher's conformity assessment, never by a platform mechanism (`MOS-CONF-434`) |
| **17.2** | developed per the state of the art: life cycle, risk management, information security, verification and validation | PUBLISHER | `MOS-TEST-002` traceability; `MOS-REL-001` signed releases; Chapter 8 in full | **PARTIAL** — security and verification are strong; "life cycle" and "risk management" are absent (`MOS-OPEN-025` leaves 62304 adoption undecided) |
| **17.4** | state the minimum hardware, IT network and IT security requirements to run the software as intended | SITE + PUBLISHER | Chapter 13 deployment topology and GPU budget arithmetic; Chapter 8 zones `Z-EDGE`/`Z-PLATFORM`/`Z-GATEWAY`; `MOS-SEC-141` offline keyed verification for on-prem | PARTIAL — the facts exist across Chapters 8 and 13; no single document states them as a deployment requirement statement a hospital can sign off |
| **23.1 / 23.4** | label and instructions for use, including residual risks, contraindications, warnings, and performance characteristics | PUBLISHER (declares) + PLATFORM (surfaces) | `MOS-SAFE-014` complete block; `MOS-SAFE-015` fail-closed on empty `not_validated_for`/`known_failure_modes`; `MOS-SAFE-022` one-page data sheet over the API | **PARTIAL** — content transport and mandatory disclosure are complete; the IFU document itself is the publisher's and has no platform representation |

**MOS-CONF-434** The platform MUST NOT present any of the evidence in the table above as satisfying a GSPR. A
GSPR is satisfied by a manufacturer's conformity assessment, not by a platform mechanism — which is why no row of
the GSPR table reads `SATISFIED`, and why a reader who wants one is reading the table wrong. The correct sentence
for a publisher's technical documentation is "the following platform-enforced control contributes to our
conformity argument for GSPR *n*", and the platform MUST NOT generate any document that says otherwise
(`MOS-SAFE-008`, `MOS-EVID-005`). The same reading applies to the AI Act table of §18.8.4, where `SATISFIED`
means "the mechanism an assessor would look for exists, is enforced, and is falsifiable" — not "the obligation is
discharged". Obligations are discharged by providers, deployers and manufacturers; this chapter maps mechanisms.

**MOS-CONF-435** GAP (PLATFORM). GSPR 5 and the usability engineering process behind it (IEC 62366-1) have no
representation in this specification at all. There is no use-specification, no user-interface-of-interest
identification, no use-error analysis, and no summative evaluation. `MOS-SAFE-012`'s adjacency rule and
`MOS-EVID-102`'s visual-distinction rule are two good use-error controls that arrived by engineering judgement
rather than by a usability process. A hospital clinical-safety officer operating under DCB 0129/0160 or an
equivalent will ask for the process, not the controls.

**MOS-CONF-436** GAP (PLATFORM). GSPR 17.4 requires a statable minimum-requirements document. The facts are
distributed across Chapters 8 and 13; the document does not exist. Producing `docs/DEPLOYMENT_REQUIREMENTS.md`
from those chapters is a documentation task, not an engineering one, and SHOULD be done for 0.2.0.

#### 18.8.4 The EU AI Act

Regulation (EU) 2024/1689. Its interaction with MDR is specific and frequently misunderstood: a medical device
is **high-risk AI** under Article 6(1) when the AI system is a product, or a safety component of a product,
covered by the Union harmonisation legislation listed in **Annex I** — which includes MDR 2017/745 — **and** that
product is required to undergo third-party conformity assessment. A class IIa or IIb device needs a notified
body; therefore an ML-enabled MDSW at IIa or above is high-risk AI automatically, with no separate risk
assessment. The Article 6(3) derogation does not apply to the Annex I route.

The conformity assessment is not doubled: Article 43(3) routes the AI Act assessment through the notified body
already performing the MDR assessment.

| AI Act obligation | Overlaps MDR? | Owner | Satisfied by | Status |
|---|---|---|---|---|
| **Art. 9** risk management system | yes — largely the ISO 14971 file | PUBLISHER | none | **GAP** — no risk management system, hazard register or risk file exists anywhere in this specification (`MOS-CONF-420`) |
| **Art. 10** data and data governance: relevance, representativeness, error-freeness, completeness; examination for bias; gap-filling measures | **partly additive** — MDR has no equivalent clause | PUBLISHER | `MOS-TRAIN-072`–`MOS-TRAIN-077` legal basis for training data; `MOS-EVID-016` no dataset from a query; `MOS-EVID-024` `acquisition_profile`; `MOS-EVID-034` leakage checks; `MOS-TRAIN-088` corpus stratification C1–C7 blocking at seal; `MOS-TRAIN-203`/`MOS-TRAIN-204` every exclusion retained with a reason that MUST NOT be a model outcome | **PARTIAL** — provenance, independence and cohort characterisation are strong; **bias examination is not obligatory** (`MOS-CONF-418`) |
| **Art. 10(5)** processing special-category data where strictly necessary for bias detection and correction | additive | PUBLISHER + SITE | `MOS-TRAIN-073` `TrainingDataPolicy.legal_basis`; `MOS-EVID-067` persists `patient_sex` and `patient_age_years` | PARTIAL — the data are held and the legal basis is recorded; no bias-detection activity is defined |
| **Art. 11 + Annex IV** technical documentation | yes — MDR Annex II/III | PUBLISHER | `ValidationReport` self-contained export bundle (`MOS-EVID-122`); `MOS-TRAIN-176` approval dossier; `MOS-SAFE-022` data sheet; `MOS-TEST-002` traceability | **PARTIAL** — Annex IV wants a description of the system's architecture, the training methodology, and the design choices; the platform produces run-level evidence, not a design description |
| **Art. 12** record-keeping / automatic logging over the lifetime | **additive** — MDR has no logging clause | PLATFORM (mechanism) + SITE (retention) | `AuditEvent` append-only with a per-tenant hash chain (`MOS-SEC-151`), monthly partitions never dropped but exported as signed archives (`MOS-SEC-154`), ≥ 6 years default retention (`MOS-SEC-126`); provenance records append-only and hash-chained (`MOS-SAFE-090`); one `AuditEvent` per PHI-bearing Gateway response (`MOS-DATA-022`) | **SATISFIED** — comfortably exceeds the Art. 26(6) deployer minimum of six months |
| **Art. 13** transparency; instructions for use naming capabilities, limitations, accuracy levels, and the intended purpose | yes — MDR GSPR 23 | PUBLISHER (declares) + PLATFORM (surfaces) | `MOS-SAFE-014` including `known_limitations`, `not_validated_for`, `known_failure_modes`; `MOS-SAFE-015` fail-closed; `MOS-SAFE-016` undeclared training population rendered explicitly; `MOS-EVID-072`/`MOS-EVID-073` every displayed number links to its `EvaluationRun` | **SATISFIED** |
| **Art. 14** human oversight: the system is designed so a natural person can understand, monitor, override, disregard and interrupt it | **partly additive** | PLATFORM + SITE | `MOS-SAFE-011` refuses `autonomy: autonomous`; `MOS-SAFE-073` default-DENY clinical-action table; `MOS-SAFE-057` nothing is auto-actioned; `ResultReview` with `REJECTED` preserved as data (`MOS-SAFE-064`); `MOS-SAFE-038` demotion to `research_only` always permitted and never gated; `MOS-EVID-139` hard breach suspends; `MOS-TRAIN-182` promotion steps 4 and 5 are irreducibly human | **SATISFIED** — this is the specification's strongest AI Act correspondence and the "interrupt" property is mechanical, not procedural |
| **Art. 15** accuracy, robustness, cybersecurity; declared accuracy metrics in the IFU | yes — GSPR 1, 17 | PUBLISHER + PLATFORM | accuracy: Chapter 7 in full, `MOS-EVID-056` bare scalars structurally impossible; robustness: `MOS-EVID-095`–`MOS-EVID-104` envelope, `MOS-EVID-105`–`MOS-EVID-112` plausibility gate, `MOS-IMG-054` fixture self-test; cybersecurity: Chapter 8 in full, `MOS-SEC-138`–`MOS-SEC-142` | **SATISFIED** |
| **Art. 17** quality management system | yes — ISO 13485 | PUBLISHER | none | **GAP** — no QMS is described anywhere |
| **Art. 26** deployer obligations: use per instructions, assign competent human oversight, monitor, keep logs ≥ 6 months | additive | SITE | `MOS-SAFE-104` `clinically_qualified` as the only input deciding a qualified read; `MOS-SAFE-028` per-deployment jurisdiction; `MOS-EVID-135` monitoring; `MOS-SEC-126` retention | PARTIAL — the controls exist; no requirement obliges a site to use them |
| **Art. 72** post-market monitoring plan | yes — MDR Art. 83–86 | PUBLISHER | `MOS-EVID-135`–`MOS-EVID-142` produce the signals | **PARTIAL** — signals, not a plan. No PMS plan artifact, no PMS report, no PSUR |
| **Art. 73** serious incident reporting | yes — MDR Art. 87 vigilance | PUBLISHER + SITE | none | **GAP** — nothing in this specification represents an incident, a report, a timeline or a competent-authority notification |
| **Art. 4** AI literacy of staff | additive | SITE + PUBLISHER | none | **GAP** — no training-record concept |

**MOS-CONF-437** A publisher whose device is class IIa or above and incorporates a machine-learned component
MUST assume it is high-risk AI under Article 6(1) via Annex I. The platform MUST NOT offer a field, flag or
control that could be read as an assessment of AI Act applicability, and `regulatory_status[].status`
(`MOS-SAFE-025`) MUST NOT be extended with an AI Act value in 0.2.0 — an AI Act obligation is not a marketing
authorisation and putting it in the CLEARED-set enum would make it gate `MOS-SAFE-036` E1, which is wrong.

**MOS-CONF-438** The Article 12 logging obligation is the one AI Act requirement this specification satisfies
outright, and it does so because of decisions taken for other reasons — tenancy, audit and provenance. A
publisher SHOULD cite `MOS-SEC-151`, `MOS-SEC-154`, `MOS-SEC-126`, `MOS-SAFE-090` and `MOS-DATA-022` as its
logging evidence, and a site SHOULD note that the platform default (≥ 6 years) exceeds the Art. 26(6) deployer
minimum by an order of magnitude, which is a retention *cost* the site must plan for, not only a compliance win.

**MOS-CONF-439** GAP (PUBLISHER + SITE). Articles 72 and 73 together — post-market monitoring plan and serious
incident reporting — are the largest structured absence in this specification after ISO 14971. Chapter 7's
monitoring plane produces exactly the inputs a PMS plan would consume and stops there. A future revision SHOULD
add an `Incident` entity linked to `Result`, `ResultReview.rejection_reason` and
`clinical.known_failure_modes[].id`, because `MOS-SAFE-058` already lets a reviewer attribute an error to a
declared failure mode and that attribution is the natural seed of a vigilance record.

**MOS-CONF-440** AI Act application dates as adopted place the Annex I high-risk obligations later than the
Regulation's other tranches, and amendment proposals affecting those dates have been in circulation. Dates MUST
NOT be relied upon from this document; they MUST be confirmed with counsel at the time of planning
(`MOS-CONF-449`).

#### 18.8.5 Jurisdictions: route, expected standards, and what this specification gives you

| Jurisdiction | Route for the `ServiceVersion` | Standards expected | Where this specification helps | Owner | Status |
|---|---|---|---|---|---|
| **EU** | MDR 2017/745; Rule 11 → IIa/IIb → notified body under Annex IX; plus AI Act Art. 6(1) via Annex I, assessed through the same notified body (Art. 43(3)); UDI and EUDAMED registration | ISO 13485, ISO 14971, IEC 62304, IEC 62366-1, ISO/IEC 27001 or equivalent for security, ISO 15223-1 labelling | `regulatory_status[].status = ce_mdr` / `ce_mdd_legacy` with mandatory `device_class`, `identifier`, `authorising_body`, `certificate_valid_until` (`MOS-SAFE-025`); automatic demotion to `research_only` on certificate expiry (`MOS-SAFE-026`, `MOS-SAFE-027`) | PUBLISHER | PARTIAL — status transport is complete; 13485/14971/62304/62366 are all absent |
| **US** | FDA: 510(k) with a predicate, De Novo where none exists, or PMA; **PCCP** in the submission for planned ML updates; QMSR (harmonised with ISO 13485) | ISO 13485 via QMSR, ISO 14971, IEC 62304, IEC 62366-1, AAMI/ANSI guidance for ML-enabled devices, GMLP as the reviewer's checklist | `fda_510k` / `fda_de_novo` / `fda_pma` statuses (`MOS-SAFE-025`); §18.7.6 GMLP mapping; §18.7.7 PCCP mapping with `MOS-CONF-422`–`MOS-CONF-427` | PUBLISHER | PARTIAL — the PCCP Modification Protocol is strongly supported; the reader study (`MOS-CONF-417`) is absent and is usually decisive for CADe |
| **UK** | UKCA under the UK MDR 2002 as amended, with an approved body; EU CE marks accepted under the recognition arrangements in force; MHRA software and AI guidance | ISO 13485, ISO 14971, IEC 62304, IEC 62366-1; **DCB 0129** (manufacturer) and **DCB 0160** (deploying organisation) clinical risk management for NHS deployment | `ukca` status (`MOS-SAFE-025`); DCB 0160 is a SITE obligation the platform supports with `MOS-SAFE-012` disclosure, `MOS-SAFE-036` E1 with a named approver, and `MOS-EVID-129` SAT evidence | PUBLISHER + SITE | PARTIAL — DCB 0129 requires a clinical safety case report and a hazard log, neither of which this specification produces (`MOS-CONF-420`) |
| **Russia** | State registration with **Roszdravnadzor** (Росздравнадзор) under the national registration rules (ПП РФ № 1416), or the EAEU common procedure; risk class 1 / 2а / 2б / 3 per the nomenclature classification (Приказ Минздрава № 4н). **Instrument numbers and the national-vs-EAEU transition MUST be verified — both have been amended repeatedly** | **ГОСТ Р ИСО 13485** (QMS); ГОСТ Р ИСО 14971 (risk); ГОСТ Р МЭК 62304 (software lifecycle); the ГОСТ Р 59921 series on artificial-intelligence systems in clinical medicine | `national_registration` status with mandatory `authorising_body`, `identifier` and `evidence_uri` (`MOS-SAFE-025`) — the enum already accommodates this route without a code change | PUBLISHER | **PARTIAL** — `national_registration` transports the registration without a schema change, and the 152-ФЗ/323-ФЗ controls below are real; the ГОСТ Р ИСО 13485 QMS, the ГОСТ Р ИСО 14971 risk file and the ГОСТ Р МЭК 62304 lifecycle are all absent, and the ГОСТ Р 59921 AI-in-clinical-medicine series has no representation at all |

##### Russia: personal-data and confidentiality obligations that bind the SITE regardless of device status

These bind the deploying hospital whether or not anything is a device, and they are the ones a Russian
procurement will test first.

| Provision | What it requires | Owner | Satisfied by | Status |
|---|---|---|---|---|
| **152-ФЗ Art. 10** — special categories | Data on state of health are a special category; processing is prohibited except on enumerated grounds, including medical-prophylactic purposes by a person bound by professional confidentiality | SITE | PHI class matrix and the prohibition on PHI in logs, spans, event payloads, metric labels and LLM prompts (`MOS-SEC-105`); envelope encryption of identifying columns under a per-patient DEK (`MOS-SEC-127`, `MOS-SEC-128`); `external_llm_allowed` defaults false (spine §8) | PARTIAL — the platform supplies controls; the lawful ground is the site's and the platform explicitly does not assess it (`MOS-SEC-007`) |
| **152-ФЗ Art. 3(9)** — обезличивание (depersonalisation) | actions after which it is impossible to determine the data subject **without additional information** | SITE | DICOM PS3.15 Annex E profiles per tenant (`MOS-DATA-027`), versioned policy (`MOS-DATA-028`), `PatientIdentityRemoved = YES` with method coding (`MOS-DATA-029`), HMAC-derived UID remapping (`MOS-DATA-032`) | **PARTIAL, and the honest reading is unfavourable.** `MOS-DATA-031` *requires* the UID mapping to be **reversible within the tenant**, and `MOS-DATA-035` provides an audited re-identification path under `phi.reidentify`. That is pseudonymisation, not depersonalisation: the additional information is held by the same operator, so the data remain personal data under Art. 3(9). The platform MUST NOT be described as depersonalising. The one place the standard is met is `MOS-EVID-011`, which re-keys `patient_key` on export across a tenancy boundary |
| **152-ФЗ Art. 18(5)** — localisation | when collecting personal data of citizens of the Russian Federation, the operator must ensure recording, systematisation, accumulation, storage, amendment and retrieval using databases located in the territory of the Russian Federation | SITE | `Tenant.data_region` exists as a column with default `on-prem` (Chapter 12) and is surfaced to the policy engine as a Cedar attribute; the architecture is on-prem-first with no mandatory cloud dependency (spine §15) | **GAP** — `data_region` is *stored and surfaced*, and **no requirement anywhere enforces physical storage placement against it**. There is no admission control on object-store or database placement, no CI assertion, and no deployment gate. An on-prem Russian installation satisfies Art. 18(5) by deployment topology, not by any platform control, and the platform MUST NOT claim otherwise |
| **323-ФЗ Art. 13** — врачебная тайна (medical confidentiality) | the fact of seeking care, health status, diagnosis and other information obtained during examination and treatment constitute medical confidentiality; disclosure requires the patient's written consent outside the enumerated exceptions | SITE | tenant isolation with forced RLS (Chapter 8); the Gateway as sole PACS credential holder with one `AuditEvent` per PHI-bearing response (`MOS-DATA-022`); audited, permissioned re-identification (`MOS-DATA-035`); append-only hash-chained audit (`MOS-SEC-151`, `MOS-SEC-154`); quarantine invisible to all but `phi.admin` (`MOS-DATA-024`); `MOS-SEC-105`'s PHI-exclusion matrix | **SATISFIED** for the technical access-control and traceability layer. Consent capture and the lawfulness of each disclosure are the site's and have no platform representation |

**MOS-CONF-441** `regulatory_status[].jurisdiction` MUST be an ISO 3166-1 alpha-2 code or the literal `EU`
(`MOS-SAFE-024`), and every `Deployment` MUST carry an immutable `jurisdiction` (`MOS-SAFE-028`). The jurisdiction
table above is descriptive; the enforcement point is `MOS-SAFE-036` E1, which resolves `regulatory_status`
against `deployment.jurisdiction` and refuses `clinical_use_mode = clinical` without a CLEARED, unexpired entry.
No row of this section adds a gate.

**MOS-CONF-442** The platform MUST NOT represent itself as providing compliance with HIPAA, GDPR, MDR, the AI
Act, 152-ФЗ or 323-ФЗ. `MOS-SEC-007` already states this for the first three; this requirement extends the same
sentence to the AI Act and to the Russian instruments. What the platform provides is controls a covered entity or
an operator may use inside its own compliance programme.

**MOS-CONF-443** GAP (PLATFORM). Art. 18(5) localisation has a stored attribute and no enforcement
(`Tenant.data_region`). A future revision SHOULD make `data_region` a checked precondition on object-store bucket
selection and on any cross-region replication, and SHOULD add a CI assertion that no deployable configuration
places a PHI-bearing store outside the tenant's declared region. Until then, localisation is a deployment fact
the site asserts, and `MOS-CONF-442` forbids the platform from asserting it.

**MOS-CONF-444** GAP (PLATFORM, terminology). The specification uses "de-identification" throughout for an
operation that is, by `MOS-DATA-031`'s own blocking invariant, reversible pseudonymisation. The engineering is
correct and the reversibility is *required* — `MOS-DATA-031` explains at length why per-invocation randomisation
would silently orphan every result. The terminology is nonetheless wrong in a 152-ФЗ context and in a GDPR
context, and a regulator or a DPO reading "de-identified" will read "anonymised". Documentation SHOULD state, in
`docs/REGULATORY.md` (`MOS-SAFE-007`), that the platform performs **pseudonymisation with a tenant-held
reversible mapping**, and that anonymisation is achieved only by the export re-keying of `MOS-EVID-011` combined
with the PHI exclusions of `MOS-EVID-116`.

#### 18.8.6 This chapter is not legal advice

**MOS-CONF-445** This chapter is an engineering mapping document written by the specification's authors. It is
**not legal advice, not regulatory advice, and not a conformity assessment**. Every classification in it — the
IMDRF categories of `MOS-CONF-408`, the Rule 11 classes of §18.8.2, the qualification position of
`MOS-CONF-428` — is a reasoned engineering opinion that a competent authority, a notified body or a court may
reject.

**MOS-CONF-446** A **qualified regulatory consultant** MUST be engaged before any regulatory submission, any CE
marking, any claim of conformity, and any contractual representation to a hospital about regulatory status. In
particular, `MOS-CONF-429` — whether platform-side measurement computation and DICOM authoring qualifies
MedicalOS as MDSW — MUST be put to a consultant and to the intended notified body before it is relied upon.

**MOS-CONF-447** The statements of `MOS-CONF-445` and `MOS-CONF-446` MUST be reproduced verbatim in
`docs/REGULATORY.md`, which `MOS-SAFE-007` already requires to exist and already requires to carry a
not-legal-advice disclaimer. This chapter supplies the specific text.

**MOS-CONF-448** No requirement in this chapter creates, implies or transports a regulatory status for any
`ServiceVersion` or for MedicalOS core. Chapter 18 is a map of obligations onto existing mechanisms and a list of
the obligations that have no mechanism. It grants nothing.

**MOS-CONF-449** Every external instrument cited in this chapter — regulation numbers, guidance document
numbers, standard designations, ГОСТ numbers, application dates — MUST be re-verified against the current
official source before use in a submission. Regulatory instruments are amended; this document is a snapshot and
carries no currency guarantee.

---

### Acceptance criteria

Numbered, falsifiable checks for the whole of Chapter 18. Each is executable as a CI job or a scripted document
check unless marked as a review gate.

1. **Every requirement id in this chapter is in range and unique.** A script extracts every `MOS-CONF-NNN`
   defined in Chapter 18 and asserts: (a) no id is defined twice; (b) every id falls inside the numeric block
   allocated to the section that defines it, where §18.7–§18.8 own `MOS-CONF-400`–`MOS-CONF-499` and currently
   define `400`–`449` contiguously; (c) every `MOS-CONF-*` token referenced anywhere in the chapter resolves to a
   definition in the chapter. Renumbering any requirement, or citing one that was never defined, makes it fail.

2. **Every cited requirement id resolves.** A script extracts every `MOS-<AREA>-<NNN>` token appearing in
   Chapter 18 outside its own `MOS-CONF-*` definitions, and asserts that each one is **defined** — not merely
   mentioned — in `docs/spec/*.md`. Definition means the id appears either in bold at the start of a
   normative statement **or** as the first cell of a normative table row in its owning chapter — Chapter 2
   defines the `MOS-SVC` ownership boundary in table form and the check MUST accept that shape. A
   cross-reference is not a definition. Introducing a citation to a non-existent id makes it exit non-zero.

3. **No cited requirement is misquoted.** For every row of every mapping table in Chapter 18 whose
   `Satisfied by` cell names a requirement id, a reviewer check confirms that the cited requirement's text
   supports the claim made in the `What it requires` cell. This is a review gate, executed once per revision of
   Chapter 18 and recorded with a named reviewer, on the model of `MOS-EVID-117`. A row that cites a requirement
   which does not support its claim is a defect of the same class as a false performance number.

4. **Every clause row names an owner.** A **mapping table** is any table in Chapter 18 that maps an external
   clause, principle, definition or obligation onto this specification. Every mapping table MUST carry an
   `Owner` column and a `Status` column, and every data row's `Owner` cell MUST contain one or more of
   `PLATFORM`, `PUBLISHER`, `SITE` — joined by `+` where an obligation is genuinely shared — and nothing else:
   no empty cell, no `—`, no `TBD`, no hedge. Tables that map nothing onto the specification (the N12 category
   matrix of §18.7.3, which reproduces an external matrix verbatim) are not mapping tables and are exempt; the
   script MUST identify them by an explicit allowlist of table captions, not by the absence of the columns it is
   checking for. A clause with no owner is a gap and MUST appear in the gap list of check 7.

5. **No row claims SATISFIED without naming a requirement.** A script asserts that every table row whose
   `Status` cell begins with `SATISFIED` has a non-empty `Satisfied by` cell containing at least one
   `MOS-<AREA>-<NNN>` token, and that no `SATISFIED` row cites only prose, a chapter number, or a spine section.
   `NOT-APPLICABLE` rows MUST carry a justification but need not cite an id.

6. **Every non-SATISFIED row carries a one-line justification.** A script asserts that every row whose `Status`
   is `PARTIAL`, `GAP` or `NOT-APPLICABLE` has status-cell text beyond the bare keyword. A bare `GAP` with no
   reason fails.

7. **The gap list is complete and consistent.** A script collects every row whose `Status` is `GAP` or `PARTIAL`
   and asserts that each one is either (a) named in a `MOS-CONF-*` requirement that states the gap explicitly, or
   (b) listed in the chapter's consolidated backlog. Adding a `GAP` row without a corresponding statement fails.

8. **No conformance claim appears anywhere.** Extending the `MOS-EVID-005` CI grep, a repository-wide
   case-insensitive search asserts that none of `IMDRF compliant`, `IMDRF certified`, `GMLP compliant`,
   `AI Act compliant`, `MDR compliant`, `Rule 11 compliant`, `152-ФЗ compliant`, `GDPR compliant` or
   `HIPAA compliant` appears in any source file, template, locale file or generated artifact, excluding this
   specification. The check MUST NOT flag a `device_class` **value** such as `IIa` or `IIb`, which is a
   publisher assertion the platform is required to store and display (`MOS-SAFE-024`, `MOS-SAFE-029`); banning a
   legitimate stored value is the failure mode that gets a grep like this switched off.
   (`MOS-CONF-400`, `MOS-CONF-442`)

9. **The chapter creates no gate.** A script asserts that no `MOS-CONF-*` requirement is referenced from the E1
   condition table of `MOS-SAFE-036`, from `evaluate_gate()` (`MOS-EVID-090`), or from any code path that can
   change a `Deployment`'s `clinical_use_mode`, with the single declared exception of `MOS-CONF-425`'s
   `change_outside_declared_plan` `INDETERMINATE` reason. (`MOS-CONF-409`, `MOS-CONF-426`, `MOS-CONF-448`)

10. **`risk_classification.imdrf` remains publisher-owned.** A test registers a `ServiceVersion` whose declared
    `category` disagrees with the `MOS-SAFE-019` derivation and asserts a `422` naming `/clinical/risk_classification/imdrf/category`;
    a second test asserts that no platform code path writes `healthcare_situation` or `information_significance`.
    (`MOS-CONF-405`)

11. **The `ChangeControlPlan` is inert.** A test registers a `ServiceVersion` carrying a `ChangeControlPlan`,
    asserts its digest is covered by `intended_use_digest`, asserts that editing it is impossible without a new
    `ServiceVersion`, and asserts that its presence changes no E1 condition outcome and does not permit
    `promotion_policy.auto_promote` where `MOS-TRAIN-184` forbids it. (`MOS-CONF-422`, `MOS-CONF-426`)

12. **A change outside the plan is INDETERMINATE, not FAIL.** A test promotes a candidate whose diff against the
    incumbent exercises `inference_backend` while the plan declares only `weights_retrain`, and asserts
    `evaluate_gate()` returns `INDETERMINATE` with reason `change_outside_declared_plan`, that the
    `deployment_gate_decisions` row records it, and that the incumbent remains serving (`MOS-EVID-092`).
    (`MOS-CONF-425`)

13. **Declared regulatory instruments are re-verified.** The chapter carries a `last_verified` date per external
    instrument cited in §18.8.5 and the jurisdiction table. A CI job warns when any is older than 12 months and
    the release gate refuses a `G-PILOT` or later release with one older than 24 months. This is the executable
    form of `MOS-CONF-449`.

14. **`docs/REGULATORY.md` carries the disclaimers verbatim.** A script asserts that the text of `MOS-CONF-445`
    and `MOS-CONF-446` appears byte-for-byte in `docs/REGULATORY.md`, alongside the items `MOS-SAFE-007` already
    requires. (`MOS-CONF-447`)

15. **The pseudonymisation statement is present.** A script asserts that `docs/REGULATORY.md` contains the phrase
    "pseudonymisation with a tenant-held reversible mapping" and does not contain "anonymised" or
    "anonymized" applied to platform-held data. (`MOS-CONF-444`)

16. **No localisation claim is made.** A script asserts that no source file, template, locale file or generated
    document asserts that the platform enforces data residency or localisation, given that `Tenant.data_region`
    has no enforcement. (`MOS-CONF-443`)

17. **Traceability covers this chapter.** `tests/traceability.yaml` maps every `MOS-CONF-<NNN>` defined in
    Chapter 18 to at least one check id, and the `traceability` job exits 0. Deleting any one mapping makes it
    exit non-zero. This is `MOS-TEST-002` applied to Chapter 18 and is the check that makes the other sixteen
    discoverable.

---

[← 17. Model Development Pipeline](17-training-pipeline.md) · [Index](../../MEDICALOS_SPEC.md) · [19. Operator Surfaces →](19-operator-surfaces.md)
