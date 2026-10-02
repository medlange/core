<!-- MedicalOS Specification v0.4.0 — chapter 15 of 19. Normative.
     123 requirements. Do not edit without a requirement-ID review. -->

[← 14. Testing and Acceptance](14-testing.md) · [Index](../../MEDICALOS_SPEC.md) · [16. Open Questions →](16-open-questions.md)

---

## 15. Delivery Plan and Engineering Rules

This chapter is the operational contract for building MedicalOS: what each release contains, what happens when a release gate is red, the order the work is done in and why that order is not negotiable, which subsystems are adopted rather than built, the rules that bind human and coding-agent contributors alike, and the definition of a finished change.

Two properties are load-bearing throughout. First, **the plan is ordered by residual uncertainty, not by dependency comfort** — the previous version's build order spent its first ten-plus weeks on work whose outcome nobody doubted and reached the four genuinely uncertain questions at step 12 or later, where every discovery propagates backwards through schemas that are already populated. Second, **every release gate is stated in observable terms** — a gate that names a product rather than an observable property cannot be failed honestly, because the product is present by construction.

### 15.1. Release plan

#### 15.1.1. What a release is

**MOS-REL-001** A MedicalOS release MUST be a signed annotated git tag `v<MAJOR>.<MINOR>.<PATCH>` plus, for each first-party container, an OCI image referenced by digest, an SPDX 2.3 SBOM and a cosign signature over the image digest. A release that exists only as a branch, an image tag without a digest reference, or an unsigned artifact is not a release.

**MOS-REL-002** Version semantics before 1.0.0 MUST be stated explicitly rather than assumed: the MINOR component carries feature scope and MAY contain breaking changes to internal schemas, Go/Python package APIs and database shapes; the PATCH component MUST NOT contain a breaking change of any kind. The HTTP API path prefix `/api/v1` is independent of the release version and is governed by Chapter 10.

**MOS-REL-003** Every release MUST carry a Release Decision Record at `docs/releases/<version>.md` containing: the tag, the image digests, the gate results (per named check, pass/fail), any Tier B or Tier C item cut under MOS-REL-009, the requirement IDs newly satisfied, and the named human who approved the tag. The record MUST be committed before the tag is pushed.

#### 15.1.2. Contents and gate per release

Contents are fixed by spine section 14 and reproduced here in full. The **gate** column names the checks that MUST be green before the tag; each named check is defined in Chapter 14 and is executable in CI.

Each row also carries the elements another chapter places in that release. Chapter 17 does this through `MOS-TRAIN-190` and always has. Chapter 19 does it through `MOS-UI-365`, and until this revision it did not: `MOS-UI-365` opened by calling itself consistent with this table while this table named no operator surface at all, so every element chapter 19 assigned to 0.2.0 and 0.3.0 was outside the contents a gate is run against, outside the tier table `MOS-REL-009` selects a response from, and therefore outside the one mechanism that would have made its absence a recorded decision rather than an omission. A chapter that places its own work in somebody else's release and is not read by that release is not deferring; it is disappearing. **The rule that follows is general: a release row MUST list every element any chapter assigns to it, in that chapter's own terms and deferring to it by requirement id, and the owning chapter's placement is authoritative over this table's silence.** The consequence of its absence is already measured twice on this project -- chapter 17's elements were missing from this row until `MOS-TRAIN-190` was ingested, and chapter 19's were missing until now, both times with a green gate above them.

Two of these rows also carry a **returning set** rather than a list. `MOS-REL-005` makes a cut Tier B item reappear in the next release, so 0.2.0's contents include what was cut from 0.1.0 and 0.3.0's include what was cut from 0.2.0. Enumerating each returning item twice -- once where it was cut and once where it returns -- would double the table every time a release slips, and the enumeration already exists where `MOS-REL-003` requires it: in the previous release's Release Decision Record, which names each cut item, its tier and the release it MUST reappear in. So the row names the set and points at that record. The set is one contents item carrying one tier because every member of it is Tier B; a returning set containing a Tier C item would have to be split, and a returning set containing a Tier A item cannot exist, because `MOS-REL-005` does not permit the cut that would have created it.

| Release | Contents (binding) | Gate — all MUST be green |
|---|---|---|
| **0.1.0** | DICOM Gateway + de-identification; study triage + `SeriesSelector`; `Job` + Postgres queue driver; geometry contract; platform-side SEG/SR writing with deterministic UIDs; one native service (pleural effusion, own data); vendored lung segmentation; `emphysema_laa` as a deterministic measurement, not a learned model; OHIF toolbar button + provenance panel; API-key auth; `tenant_id` + Postgres RLS; audit. Single Triton, docker compose; and the operator-surface elements Chapter 19 places in this release (`MOS-UI-365`): the `REJECTED`/`FAILED` distinction carried on four independent channels (`MOS-UI-337`); the `MOS-SAFE-012` adjacency set rendered without interaction on every result-bearing surface (`MOS-UI-345`); the clinician surface's three-part refusal rendering with the machine-readable code secondary (`MOS-UI-330`, `MOS-UI-331`); submission against a fixed configured capability list (`MOS-UI-366`); the refusal catalogue seeded for this release's gates with its CI completeness check (`MOS-UI-335`, `MOS-UI-336`); the two surfaces hosted inside a rebuilt OHIF bundle (`MOS-UI-369`). | `dicom-battery` (dciodvfy zero errors; SEG read-back Dice 1.0; origin/spacing/direction within 1e-4 of source; `FrameOfReferenceUID` equality; SR parses as TID 1500 with coded names and UCUM units; STOW-RS 200 with zero failed SOPs; QIDO-RS instance count exact); `idempotency-three-surface` (exactly one `results` row per capability, exactly one `SeriesInstanceUID` per generated DICOM object kind in the PACS, exactly one completion event, including the crash-between-store-and-complete variant); `tenant-isolation` (403 on the REST API **and** 403 on a direct QIDO-RS for another tenant's `StudyInstanceUID`); `provenance-replay` (the provenance record names the consumed series, the pinned versions and every stored object); `rejection-distinct` (a study with no eligible series terminates `REJECTED` with a machine-readable reason, not `FAILED`). |
| **0.2.0** | Evidence plane: `Dataset`, `DatasetVersion`, `DatasetSplit`, `AnnotationSet`, `EvaluationRun`, `AcceptanceCriteria`, `ValidationReport`; gated deployment; applicability envelopes; RUO marking; `ResultReview`; and the training-pipeline elements Chapter 17 places in this release (`MOS-TRAIN-190`): the curation queue (`HarvestCandidate`, `CurationBatch`) with its exclusion vocabulary, per-`Tenant` `training_use_allowed` with its `TrainingDataPolicy`, the corpus stratification check run at seal (`MOS-TRAIN-088`), and the generated transform chain with its byte-equality check (`MOS-TRAIN-191`); and the operator-surface elements Chapter 19 places in this release (`MOS-UI-365`): the RUO badge with the `include_research` gating of every list, feed, worklist and count (`MOS-UI-346`); the `ResultReview` surface with its `REJECTED` review rendering (`MOS-UI-350`); the curation queue surface over `HarvestCandidate` and `CurationBatch` with its exclusion vocabulary (`MOS-UI-357`); the training-policy surface over `training_use_allowed` and `TrainingDataPolicy` (`MOS-UI-365`, with `MOS-UI-368` reserving the API paths it would drive); the seal, split-freeze, leakage and stratification refusal rendering (`MOS-UI-332`); the annotation campaign surface over the adopted annotation tool (`MOS-UI-358`); the refusal catalogue at this release's full set with its CI completeness check (`MOS-UI-335`, `MOS-UI-336`); the first issue of the UI contribution statement (`MOS-UI-359`); and the Tier B operator-surface elements cut from 0.1.0 under `MOS-REL-009`, returning as `MOS-REL-005` requires and enumerated in that release's Release Decision Record (`MOS-UI-369`). | 0.1.0 gate still green, plus: `deployment-gate` (a deliberately regressed `ModelVersion` is refused promotion against a frozen split and the incumbent stays live); `non-inferiority` (the gate passes a within-noise change on a 40-case split and fails a change that loses a named stratum); `per-case-metrics` (per-case rows persisted, not only aggregates); `leakage-check` (a patient present in two splits fails the build); `report-offline-verify` (a `ValidationReport` verifies on a machine with no network access to MedicalOS); `ruo-marking` (in `clinical_use_mode: research_only` the writer refuses to emit an unmarked object); plus the check Chapter 19 adds to this row under `MOS-UI-370`, stated in observable terms and naming no product: `refusal-completeness` (every blocking gate reachable from a surface carries a refusal record whose three members are all present, all non-empty and all free of a code, an HTTP status, a table name, a field path or a requirement id, and CI fails naming the gate when one is missing; the enumeration extends at 0.3.0 to the run-time re-execution refusals). |
| **0.3.0** | Platform: `Artifact` registry; capability resolution as a pure function; sealed-mode services; Kafka `JobQueue` driver; second capability (lung nodule on LIDC-IDRI) added with **zero core code changes**; and the training-pipeline elements Chapter 17 places in this release (`MOS-TRAIN-190`): `TrainingRun` with the `Orchestrator` port and one driver, MONAI Bundle as the source form of a native-mode model artifact, nnU-Net permitted as a training backend, `ConversionRun`, and the three-act promotion; and the operator-surface elements Chapter 19 places in this release (`MOS-UI-365`): the clinician model picker driven by capability resolution (`MOS-UI-366`); the training submit surface with run monitoring (`MOS-UI-367`); the configuration-search surface (`MOS-UI-303`); the search diagnostics block (`MOS-UI-304`); the promotion screen carrying the dossier, the three acts and `test_exposure_count` (`MOS-UI-318`, `MOS-UI-320`); the conversion surface rendering E1, E2 and E3 (`MOS-UI-365`); the honest-metric display rule in force end to end (`MOS-UI-300`); the refusal catalogue extended to this release's full set including the run-time re-execution refusals (`MOS-UI-332`, `MOS-UI-335`); and the Tier B operator-surface elements cut from 0.2.0 under `MOS-REL-009`, returning as `MOS-REL-005` requires and enumerated in that release's Release Decision Record (`MOS-UI-365`). | 0.1.0 and 0.2.0 gates green, plus: `zero-core-change` (the diff that introduces the second capability touches only `medos/services/`, `medos/schemas/`, `medos/examples/`, registry rows, and the capability's own new test module — asserted by a path allow-list over `git diff --name-only`. The test module was added to this list after the 0.3.0 gate refused to widen it on its own: `MOS-REL-012` makes an unexecuted acceptance criterion equivalent to an unsatisfied requirement, so a capability MUST ship a test, and `testpaths` collects only `tests/`, so the test cannot live under `medos/services/` and still run. The list admits ONE NEW test module whose name contains the capability id — not `tests/` wholesale, which would let any test change ride along); `resolution-purity` (the resolver is a pure function of `(capability, context, registry_snapshot)`, property-tested over a frozen snapshot, and the pinned set on an existing `Job` is unchanged by a later registry write); `queue-driver-parity` (the `JobQueue` conformance suite passes identically against every registered driver, including the second driver this release introduces); `sealed-mode-isolation` (a sealed service container has no route to the database, the broker or the object store — asserted by network policy test); plus the four checks Chapter 17 adds to this row under `MOS-TRAIN-193`, each stated in observable terms and naming no product: `leakage-blocks-training`, `chain-equivalence`, `served-plan-equivalence`, `no-auto-promote`. Chapter 19 adds two more to this row under `MOS-UI-370`, in the same observable terms: `headline-partition` (no figure rendered in a headline position originates from a run whose partition any selection touched) and `duty-separation` (no single authenticated flow performs both a training-run submission and an approval act). |
| **0.4.0** | Unattended study-arrival operation; third-party viewer verification; MCP tool surface; agentic report drafting; and the operator-surface elements Chapter 19 places in this release (`MOS-UI-365`): the unattended-arrival worklist surface; and the Tier B operator-surface elements cut from 0.3.0 under `MOS-REL-009`, whose return `MOS-REL-005` required is discharged at specification 0.4.0 by the withdrawal of the engineering surface (`MOS-UI-367`, amended `MOS-UI-100`). | All prior gates green, plus: `unattended-arrival` (a study arriving at the PACS is triaged, analysed and stored with no human action, end to end); `second-viewer` (**AMENDED at specification 0.4.0** — the generated SEG overlays correctly and the SR hydrates in a second, independent viewer: ~~one not named in the 0.1.0 contents and sharing no rendering implementation with it~~ the independent **third-party** viewer `MOS-IMG-158` requires, drawn from the incumbent list §15.3.1's Viewer row retains for exactly this purpose, and bound by `MOS-IMG-157a`'s "independent means not ours" — it MUST NOT be `viewer/`, MUST NOT be built from this repository, and MUST share no source with it); `mcp-tenancy` (an MCP session without a bound `Tenant` and execution context is refused); `narrative-postcondition` (a generated narrative containing a numeric, laterality term or finding label absent from the source `Result` fails closed and is not emitted). |

**Why the `second-viewer` cell is amended in the release that introduced the defect.** The struck
clause defined the second viewer by two negative tests, and both picked out a third party only
while the 0.1.0 contents named OHIF. §15.3.1's Viewer row was amended at this same specification
version to record BUILD, and from that moment the platform's own `viewer/` satisfied the struck
clause on both counts — it is not named in the 0.1.0 contents, and it shares no code with OHIF.
The cell therefore admitted the one candidate the check exists to exclude. `MOS-IMG-158` and
`MOS-IMG-157a` never admitted it, so the requirements were right and the abbreviation had come
apart from them. What made the drift load-bearing is that nothing else defined the check: measured before this amendment, no module, no test and no entry in the release criteria named `second-viewer` at all, so this cell was its only definition anywhere, which is
what made the drift load-bearing. The amendment carries the cell to where the requirements already
stood and adds nothing to what they demand. The amendment marker opens the definition rather than
separating the name from it, because the gate parser in
`tests/unit/test_gate_contract.py` identifies a check by the parenthesis that follows its name and
an unparsed check is an ungated one (`MOS-REL-012`). Register entry 125 holds the account and quotes
the struck clause as it stood.

**MOS-REL-004** A release MUST NOT be tagged while any check in its gate row is red. There is no reviewer discretion on this; the response to a red gate is MOS-REL-009.

#### 15.1.3. Cut tiers

**MOS-REL-005** Every release's contents MUST be assigned to exactly one of three tiers, declared before the release block begins:

| Tier | Meaning | Rule |
|---|---|---|
| **A** | Safety-load-bearing or irreversible in placement. Removing it later costs a rewrite or lets a wrong clinical artifact exist. | MUST NOT be cut under any circumstance. |
| **B** | Feature scope with a real user. | MAY be cut, and then MUST reappear as Tier A or B of the next release. |
| **C** | Convenience, breadth or polish. | MAY be cut and MAY be dropped permanently. |

| Release | Tier A | Tier B | Tier C |
|---|---|---|---|
| 0.1.0 | `tenant_id` + RLS; DICOM Gateway as the sole PACS credential holder; deterministic UIDs + `UNIQUE (job_id, capability_id)`; append-only audit; the geometry contract; `REJECTED` as a distinct terminal state; RFC 9457 `class` enum separating `clinical_rejection` from transport failure; the `REJECTED`/`FAILED` distinction on four independent channels (`MOS-UI-337`); the `MOS-SAFE-012` adjacency set on every result-bearing surface (`MOS-UI-345`); the clinician surface's three-part refusal rendering (`MOS-UI-330`) | pleural-effusion native service; `emphysema_laa`; provenance panel; OHIF toolbar button — Tier B and not Tier C because Chapter 9 `MOS-SAFE-089a` makes it a MUST for release 0.1 and tests it in that chapter's acceptance check 24, so a `curl` against `POST /api/v1/jobs` is not an acceptable substitute; submission against a fixed configured capability list (`MOS-UI-366`); the refusal catalogue and its CI completeness check (`MOS-UI-335`); the two surfaces inside a rebuilt OHIF bundle (`MOS-UI-369`) | docker-compose ergonomics |
| 0.2.0 | sealed `DatasetVersion`; patient-level `DatasetSplit`; per-case metrics; `clinical_use_mode` + RUO marking; the deployment gate; the RUO badge and its `include_research` gating (`MOS-UI-346`) | `ValidationReport` signing and offline verification; applicability envelopes; `ResultReview`; the `ResultReview` surface (`MOS-UI-350`); the curation queue surface (`MOS-UI-357`); the training-policy surface (`MOS-UI-368`); the seal, split-freeze, leakage and stratification refusal rendering (`MOS-UI-332`); the annotation campaign surface (`MOS-UI-358`); the refusal catalogue at the 0.2.0 set (`MOS-UI-335`); the first issue of the UI contribution statement (`MOS-UI-359`); the operator-surface elements returning from the 0.1.0 cut (`MOS-UI-369`) | report rendering/export format beyond JSON |
| 0.3.0 | capability resolution pinned into the `Job` row; the `JobQueue` port; sealed-mode network isolation; the honest-metric display rule in force end to end (`MOS-UI-300`) | `Artifact` registry breadth; the clinician model picker (`MOS-UI-366`); the training submit surface with run monitoring (`MOS-UI-367`); the configuration-search surface (`MOS-UI-303`); the promotion screen (`MOS-UI-318`); the refusal catalogue extended to the 0.3.0 set (`MOS-UI-335`); the operator-surface elements returning from the 0.2.0 cut (`MOS-UI-365`) | Kafka driver (the Postgres driver is production-adequate at this scale); the search diagnostics block (`MOS-UI-304`); the conversion surface (`MOS-UI-365`) |
| 0.4.0 | MCP session→`Tenant` binding; narrative post-conditions | study-arrival routing rules; second-viewer verification; the operator-surface elements cut at 0.3.0 whose return is discharged by the withdrawal of the engineering surface at specification 0.4.0 (`MOS-UI-367`) | agentic report drafting; the unattended-arrival worklist surface (`MOS-UI-365`) |

`MOS-UI-371` assigns tiers to the operator-surface elements above in three groups and leaves the rest to this table. Where it speaks, it is followed exactly: the clinician surface's marking, refusal and `REJECTED`/`FAILED` obligations are Tier A, the hosting form is Tier B, and the search diagnostics block, the conversion surface and the unattended-arrival worklist are Tier C. Where it does not speak, the tier is argued from `MOS-REL-005`'s own test -- does removing it later cost a rewrite, or let a wrong clinical artifact exist -- and not from how much work the item is. Four assignments are worth stating because they could each have gone the other way.

**The refusal catalogue is Tier B and the refusal rendering is Tier A, and they are separate items for that reason.** `MOS-UI-371` makes the clinician surface's refusal obligations Tier A, and those obligations are a display: a refusal renders in words, its three parts render together, and the machine-readable code renders secondary and verbatim (`MOS-UI-330`, `MOS-UI-331`, `MOS-UI-337`). What `MOS-UI-335` and `MOS-UI-336` add is where the strings live and a CI job that enumerates every reachable gate and fails on a missing record. That is a completeness mechanism, not a safety display: moving strings out of a renderer into a versioned catalogue later costs an afternoon, and its absence lets no wrong clinical artifact exist, because the platform refuses whether or not the refusal reads well. **If a reader takes `MOS-UI-371` to tier the catalogue itself Tier A, then `refusal-completeness` is not an inapplicable check but an unmet Tier A obligation, and the declaration in `tests/unit/test_gate_contract.py` that makes it inapplicable MUST be deleted rather than argued with.** The split is recorded here so that reading is available rather than foreclosed.

**The `ResultReview` surface is Tier B and the `review_status` it renders is Tier A.** The safety-display half is already carried by the `MOS-SAFE-012` adjacency set, of which `review_status` is one of five members, and that item is Tier A in every row it appears in. What the surface adds is the act of reviewing, which is feature scope with a real user and which `MOS-SAFE-063` already forbids from having any side effect. Tiering the whole surface A would make the absence of a review screen a bar to tagging while the field it exists to write is rendered correctly without it.

**The clinician model picker and the promotion screen are Tier B.** Neither is a marking, a refusal or a terminal-state distinction. `MOS-UI-366` constrains what the picker may claim once it exists and explicitly leaves the 0.1.0 configured-list submit standing until capability resolution does; the promotion screen's safety weight is carried platform-side by `no-auto-promote` and the deployment gate, which refuse an automated promotion whether or not a screen exists. A surface that has not been built cannot mislead anyone.

**The engineering surface's refusal rendering is Tier B although refusals are Tier A elsewhere in this table.** `MOS-UI-371` scopes its Tier A clause to the *clinician* surface, and the reason survives inspection: the seal, split-freeze and leakage gates of `MOS-TRAIN-115` block in the platform, are gated by `leakage-blocks-training`, and block identically with no surface present. The rendering is how an operator learns why; its absence is a usability cost paid by one person, not a wrong artifact reaching a reader.

#### 15.1.4. The slip rule

**MOS-REL-006** When a release gate is red at the planned tag date, **the contents MUST NOT silently shrink**: the response is exactly one of the three of MOS-REL-009, chosen against the tier table of 15.1.3 — which is the pre-declared, ordered cut list such a response requires — and recorded in the Release Decision Record. Silent reduction is the specific failure this rule exists to prevent: the previous version named three release numbers and stated the contents of none, so a slip had no defined consequence and would have been resolved by quietly dropping whatever was hardest — which, on this project, is always the safety-load-bearing half. One invariant holds on every branch: an acceptance criterion is never cut; only scope is. **Whether the standing policy is a fixed date with variable scope or a fixed scope with a floating date is `OQ-20`, which is open**: Chapter 16 `MOS-OPEN-041` records the recommended default — fixed date, variable scope, drawn from a pre-declared cut list — and the decision gate `G-0.1.0`. Until that decision, this requirement states only what both branches share, MOS-REL-009 selects the response per tier, and this chapter MUST NOT be read as deciding `OQ-20`.

**MOS-REL-007** At each release block boundary a **scope review** MUST be held and recorded in the Release Decision Record. It answers three questions: which Tier B/C items have not been started, which risks the block was supposed to retire were not retired, and whether any item has moved tier. The previous version's build order had no such gate, so an agent handed it would grind all twenty steps regardless of what it learned at step 12.

**MOS-REL-008** Every gate check MUST be stated in observable terms and MUST NOT name a product. "The job is durably queued and survives a `SIGKILL` of the claiming process" is a gate; "the job reaches Kafka" is not, because it is satisfied by the presence of Kafka. "Inference runs behind a versioned model reference resolved at dispatch" is a gate; "the worker calls Triton" is not. A gate that names a product MUST be rewritten before the block that it gates begins.

**MOS-REL-009** On a red gate at the tag date, exactly one of three responses MUST be chosen and recorded:

| Response | When it applies | Consequence |
|---|---|---|
| **(a) Hold** | The failing check covers a Tier A item, or the fix is estimated at ≤ 2 weeks. | No tag. The block continues. The scope review of MOS-REL-007 is held immediately rather than at the block boundary. |
| **(b) Cut and re-run** | The failing check covers a Tier C item only, and removing that item makes the check inapplicable rather than merely unexecuted. | The item is removed from the release contents, the Release Decision Record names it, the full gate is re-run from scratch, and the tag proceeds. |
| **(c) Pre-release** | The failing check covers a Tier B item and the release has downstream consumers who need the rest. | Tag `v<version>-rc.<N>`. The release notes MUST name every failing check. A pre-release MUST NOT be deployed to any `Deployment` with `clinical_use_mode: clinical`, and the control plane MUST refuse such a deployment mechanically, not by policy document. |

**MOS-REL-010** One residual question is deliberately not answered here and is carried to Chapter 16: **what exactly ships when the failing gate is a property of the architecture rather than a feature.** The 0.3.0 `zero-core-change` check is the concrete instance — it cannot be cut, because it is not an item; if the second capability requires a core edit, the honest options are to tag 0.3.0 with a recorded architectural debt, or to withhold the platform claim entirely and re-enter the 0.3.0 block. MOS-REL-009 does not decide between them and MUST NOT be read as deciding.

**MOS-REL-011** Non-goals MUST NOT be re-entered by a slip. Cutting a Tier B item never licenses adopting a non-goal (own PACS, own viewer, own LLM, own vector database, own GPU scheduler, EHR, billing, prescriptions, autonomous clinical decisions, marketplace) as a substitute for the cut item.

**MOS-REL-012** A requirement ID that a release claims to satisfy MUST have its acceptance criterion executed in that release's CI run. An unexecuted acceptance criterion means the requirement is not satisfied, whatever the code does.

### 15.2. The build plan, week by week

The week numbers are nominal engineer-weeks for one engineer with AI assistance, as in the review's estimate. They are a sequencing contract, not a calendar promise: see MOS-REL-026 for the calibration rule.

#### 15.2.1. The ordering argument

**MOS-REL-018** Work MUST be ordered on two axes, in this precedence: **(1) residual uncertainty, descending; (2) among items of equal uncertainty, irreversibility of placement, descending.** Work that is both certain in outcome and reversible in placement goes last.

The first axis is why the output-contract spike is week 0 and registry CRUD is week 10. The second axis is why `tenant_id`/RLS, the DICOM Gateway and deterministic UIDs sit at weeks 3–5 despite being zero-uncertainty work: nobody doubts they can be built, but retrofitting them after a viewer and three workers already point at the PACS directly means re-pointing everything, and retrofitting row-level security after the repository layer exists means auditing every query by hand. Certainty says "later"; irreversibility overrides it.

**MOS-REL-019** A work item whose outcome is uncertain MUST NOT be scheduled behind a work item that its outcome could invalidate. The following ranking is binding for this version:

| Work item | Residual uncertainty | What a late discovery invalidates | Old position | New position |
|---|---|---|---|---|
| Does a generated SEG overlay at correct slice positions in a real viewer? | High | The `ResultBundle` schema, the writer, the geometry contract, and every result already stored | step 14/16 (≈week 12–20) | **week 0** |
| Does a TID 1500 SR hydrate into a viewer's measurement panel? | High | The coded-concept dictionary and the `Result` schema | step 14/16 or nowhere | **week 0** |
| Does a declarative `SeriesSelector` pick the right reconstruction on real multi-recon CT? | High | Triage, job creation, per-series rejection data, and the platform claim in Chapter 6 | nowhere | **week 0** |
| Do the shipped models produce plausible masks on real chest CT rather than notebook fixtures? | High | The entire 0.1.0 content list | nowhere (no step produced a model) | **weeks 1–2** |
| Does the geometry round-trip preserve measurements? | High | Every number the platform has ever published | nowhere | **weeks 1–2** |
| Does DICOMweb retrieval sustain a 400-slice series? | Medium | Gateway design and caching | step 12 | **weeks 1–2** |
| Does TensorRT conversion work for patch-based 3D segmentation? | Medium | The `ModelVersion` backend discriminator only | step 13 | **deferred to 0.3.0** as a per-`ModelVersion` backend discriminator; whether an `InferenceBackend` port is declared at all is OQ-10 (Chapter 16) |
| Go HTTP handlers, migrations, auth, RBAC tables | Zero | Nothing | steps 3–5 | **weeks 3–5** (placed early for irreversibility, not uncertainty) |
| Registry CRUD | Zero | Nothing | step 6 | **weeks 10–13** |
| Broker producer/consumer | Zero, given the port | Nothing | step 8 | **0.3.0, driver 2** |
| Kubernetes manifests | Zero | Nothing | step 20 | **0.4.0 or site-driven** |

#### 15.2.2. Week 0 (days 1–5) — the output-contract spike

**MOS-REL-013** No component whose shape depends on the DICOM output contract MUST be built before the week-0 spike has produced its recorded result. This is the first work item on the project.

Contents — one Python script, two containers (a PACS and OHIF), no Go, no database, no queue, no inference server:

1. Load at least 20 de-identified chest CT studies into a development PACS through a written procedure whose UID remapping is consistent and reversible within the run and whose mapping is persisted (Chapter 3). The procedure is a document from day one because every SEG and SR the platform will ever write references source SOP Instance UIDs, and a per-invocation random remapping silently invalidates all of them.
2. Obtain a mask by any means — an HU threshold, a hand contour. Its clinical provenance is irrelevant to this spike.
3. Write a DICOM SEG and a TID 1500 SR with highdicom from that mask, in source geometry, and STOW-RS them back.
4. Open the study in the viewer. Record two observations: whether the SEG overlays at correct slice positions, and whether the SR hydrates into the measurement panel with named, coded measurements.
5. Implement the DICOM battery (Chapter 14) as CI, against these objects.
6. Write a declarative `SeriesSelector` as a single function over the 20 studies' metadata and count, by hand, how many studies it picks wrong and why.

**Risk retired.** Whether the output contract of Chapter 4 is achievable at all and what metadata it must carry; and how badly one-box series selection fails on real multi-reconstruction CT. Both are questions whose answers change schemas.

**MOS-REL-014** Exit check, recorded in `docs/releases/spike-week0.md`: the two viewer observations with screenshots, the DICOM battery green in CI, and a selector error count with a per-error cause. The spike script itself MUST be deleted or re-implemented behind the Chapter 4 writer interface; only the battery, the fixture corpus and the recorded findings survive.

#### 15.2.3. Weeks 1–2 — one end-to-end slice with deliberately wrong architecture

No auth, no tenancy, no registries, no broker, no inference server. The path is: DICOMweb pull → series selection → canonical volume build → inference in-process → inverse transform → `ResultBundle` → SEG + SR → STOW. Then the button: `POST /api/v1/jobs`, `GET /api/v1/jobs/{id}`, a `jobs` table, a `SELECT ... FOR UPDATE SKIP LOCKED` claim loop, an OHIF toolbar button and a provenance panel. One vertical slice: pleural effusion on own labelled data, a vendored off-the-shelf lung segmentation, and `emphysema_laa` as a deterministic percent-low-attenuation-area measurement.

**MOS-REL-015** This block MUST include an explicit step that **produces and packages the model artifacts** — weights, the `PreprocessingSpec`, and the golden fixture with its recorded training-time tensor hash — in the artifact format of Chapter 6, before any worker consumes them. The previous version's build order contained no such step, which meant its main acceptance test depended on three artifacts that no step produced; that omission is the single largest schedule defect the review found and it is closed here.

**MOS-REL-016** This block MUST also ship the telemetry helper package: the structured-log field set and the metric registry of Chapter 13. Dashboards and distributed tracing come in weeks 3–5, but the helpers exist from the first merge so that the Definition of Done's metrics gate (MOS-REL-121) is satisfiable by every change from that point on. Observability is a property of each change, never a phase.

**Risk retired.** Whether the models work on real chest CT; whether the geometry round-trip preserves measurements; whether DICOMweb sustains 400-slice retrieval.

**Exit check.** The end-to-end case runs from the button to a rendered SEG, and the measurement reported in the SR equals the measurement recomputed from the stored SEG in source geometry to within the tolerance stated in Chapter 4.

#### 15.2.4. Weeks 3–5 — turn the script into a service and install the four irreversible foundations. Tag 0.1.0

These items appear nowhere in the demo path, which is exactly why they are scheduled here rather than discovered as debt:

1. `tenant_id NOT NULL` on every domain table, Postgres row-level security bound to a session variable, and a **single repository chokepoint** that sets it. This converts cross-tenant leakage from a code-review property into a database impossibility for roughly an afternoon's work.
2. API-key authentication behind an `Authenticator` interface, so OIDC is a driver rather than a rewrite.
3. **The DICOM Gateway holding the only PACS credential, with the viewer and every worker re-pointed at it.** Built now because retrofitting a proxy under a working viewer and three workers means re-pointing all of them, and because it is the only place a per-patient PHI-access audit can exist.
4. Deterministic UIDs, `UNIQUE (job_id, capability_id)`, and the results row plus the `COMPLETED` transition committed in one transaction.

Plus: `audit_events` append-only at the database role level (`REVOKE UPDATE, DELETE` from the application role — one line in a migration now, an ugly backfill later); the full provenance record; and an RFC 9457 error model whose `class` enum distinguishes `clinical_rejection` from transport failure, because to a clinician those are otherwise the same red X and one of them is information a radiologist must act on.

Plus the move off the weeks 1–2 in-process loader: native-mode inference runs on the single shared Triton that the 0.1.0 contents name, administered by `medicalos-tritond` (Chapter 13, §13.10). The in-process PyTorch/MONAI loader survives only as a CPU-only test fixture and is not a shipped driver (15.2.9).

**Risk retired.** None — deliberately. This block exists because of the second ordering axis.

**Exit check.** The 0.1.0 gate. Tag 0.1.0.

#### 15.2.5. Weeks 6–9 — build the evidence layer. Tag 0.2.0

This is the differentiator and the previous version omitted it from the MVP entirely while scheduling validation at step 19 of 20. Contents: `datasets`, sealed content-addressed `dataset_versions` with licence and de-identification status per source, patient-level splits, annotation sets naming readers and the consensus rule, and evaluation runs with **per-case metrics persisted, not only aggregates**. One evaluation implementation, not two. `AcceptanceCriteria` on the `Capability`. The deployment gate rewritten as an executable rule with a paired non-inferiority regression — not `if new.dice < old.dice`, which fails on noise roughly half the time on a 40-case split, gets switched off within two weeks, and meanwhile passes a model that lost every sub-6 mm nodule. Applicability envelopes enforced at study validation. A patient-level leakage check. `clinical_use_mode` with RUO marking. `ResultReview`.

**Risk retired.** Whether evidence is portable rather than site-regenerated — proved by verifying a `ValidationReport` on a machine with no network access to the platform. If evidence must be regenerated per site it is a cost centre; if it is portable and re-verifiable it is the thing both hospitals and model vendors route through.

**Exit check.** The 0.2.0 gate. Tag 0.2.0.

#### 15.2.6. Weeks 10–13 — become a platform, then prove it. Tag 0.3.0

One `artifacts` table with per-kind JSON-Schema'd manifests served on the existing API paths. Capability resolution as a pure function with the written precedence order of spine section 9, pinned into the `Job` row at creation and reused on every retry. The public job enum collapsed to the six states with an open `phase` string alongside. Sealed-mode services. Kafka as `JobQueue` driver 2. Per-step rows are not new work in this block: `JobStep`/`job_steps` and the immutable step plan exist from 0.1.0 under Chapter 5 (MOS-EXEC-020 to MOS-EXEC-023). No `Workflow`, `WorkflowVersion`, `Tool`, `ToolVersion`, `Agent` or `AgentVersion` is backed by a table, endpoint or executor before 0.4.0 (Chapter 11, MOS-AGENT-004; spine sections 3 and 13).

**MOS-REL-020** The block MUST end by adding lung nodule detection on LIDC-IDRI as a second capability, and the resulting diff MUST touch no core code. This is the platform's real acceptance test and it is more informative than any end-to-end demo: if the second capability requires a core edit, that is learned in week 13 rather than in year two, while the core is still small enough to change.

**Exit check.** The 0.3.0 gate, including `zero-core-change`. Tag 0.3.0.

#### 15.2.7. Weeks 14–16 — the deployment shape hospitals actually buy. 0.4.0 part A

Study-arrival trigger over the PACS stored-instance callback with routing rules. A second mandatory acceptance test in which an arriving study is triaged and analysed unattended and the results render in a third-party viewer. `dciodvfy` clean in CI across the whole named fixture corpus.

**MOS-REL-021** The unattended-arrival path MUST become a gate check and MUST NOT remain a roadmap item, because a demo whose only entry point is a human pressing a button in a viewer optimises the product against the one deployment mode hospitals do not buy.

#### 15.2.8. Weeks 17–20 — 0.4.0 part B

The MCP tool surface, bound to a `Tenant` and an execution context at session establishment, and agentic report drafting with the narrative post-conditions of spine section 13.

**MOS-REL-022** The MCP surface MUST NOT be built before the governed tool catalogue, enforced provenance and validated models exist. Shipped first, it is a thin protocol wrapper over a PACS that a competent developer reproduces in a weekend, with a foreign agent on the far side and the clinical exposure of the output still on this side.

#### 15.2.9. What comes back later, and behind which seam

**MOS-REL-023** Each deferred subsystem MUST be deferred behind a named seam that exists from the block in which its first driver ships. A subsystem deferred without a seam is not deferred; it is omitted.

| Subsystem | Seam | Driver 1 (ships) | Driver 2 (later) | Why the seam |
|---|---|---|---|---|
| Durable job transport | `JobQueue` port: `Enqueue`, `Claim(lease)`, `Heartbeat`, `Complete`, `Fail`, `Cancel` (Chapter 5) | Postgres `SELECT ... FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`, weeks 1–2 | Kafka + `job_outbox` + relay, 0.3.0 | Driver 1 commits the job row and the queue row in one transaction, so the dual-write problem never exists at all rather than being solved by an outbox. Three durable independent consumers (SSE, webhooks, audit) are what eventually justify driver 2. |
| Inference | `InferenceBackend`: `infer(volume, model_id, model_version) -> tensor` — proposed, NOT settled; see OQ-10 (Chapter 16) | Triton, single instance, in the default compose profile from 0.1.0 (spine section 14; Chapter 13 §13.10). The in-process PyTorch/MONAI loader exists only for the weeks 0–2 spike and for CPU-only test fixtures and is NOT a shipped driver. | Any second backend is deferred behind OQ-10; TensorRT is a per-`ModelVersion` backend discriminator, 0.3.0 | Loading by id and version means the registry contract is genuinely exercised from the first slice instead of being asserted. |
| Orchestration platform | Three constraints: environment-variable configuration only, one process per container, zero orchestrator API calls from application code | docker compose, weeks 1–5 | Kubernetes, 0.4.0 or when a site requires it | The constraints are what make the platform substitutable; the manifests are a day's work once they hold. |
| Agentic layer | The `Tool` contract with input **and** output schemas and a declared idempotency property, MCP-native from the start (Chapter 11) | none in 0.1–0.3 | 0.4.0 | Defined early and built late, so the agentic layer is additive rather than a re-architecture. |
| Identity | `Authenticator` interface | API keys, weeks 3–5 | OIDC, 0.3.0+ | Avoids an auth rewrite at the point where a hospital IT department first appears. |

#### 15.2.10. Calibration

**MOS-REL-026** The week numbers MUST be published with their basis and their multiplier. The basis: the same bottom-up method that produced 42–56 person-weeks for the previous twenty-step order, applied to this plan, yields roughly 16–22 person-weeks — the difference is real deferral, not optimism. AI assistance compresses boilerplate by roughly 30–40% and compresses DICOM SEG correctness, accelerator conversion, viewer internals and model training by approximately zero, and those dominate the uncertain blocks. A planning multiplier of **1.5× on calendar time** MUST be applied when a date is communicated externally, and the multiplier MUST be stated rather than folded silently into the numbers.

**MOS-REL-024** No block MUST be entered while the previous block's exit check is unrecorded. **MOS-REL-025** A block whose risk was not retired MUST NOT be declared complete merely because its calendar allocation expired; the scope review of MOS-REL-007 is the mechanism, and its output is a tier change or a schedule change, never a silent pass.

### 15.3. Build-versus-adopt register

**MOS-REL-027** The register below MUST be maintained in the repository at `docs/adr/BUILD_VS_ADOPT.md` and MUST be normative: no new subsystem MUST be built without a row, and a row MUST NOT record "build" without naming at least one incumbent that was considered and the specific property that disqualified it. The previous version stated "use existing open-source components" and named only the six components that were never in question, which silently resolved every contested decision as "build." The register additionally carries, verbatim and with the same normative force, the six MONAI and nnU-Net rows required by Chapter 17 `MOS-TRAIN-019`; 15.3.1 below is the platform half of one register, not a competing one.

#### 15.3.1. The register

| Subsystem | Incumbents considered | Licence | What is **adopted** | What is **wrapped** | What is **genuinely added** | Justification for the build portion |
|---|---|---|---|---|---|---|
| **Durable execution** | Temporal; Cadence; Restate; Argo Workflows | MIT; MIT; BSL; Apache-2.0 | Nothing in 0.1–0.3 | — | A Postgres-backed `JobQueue` with leases, heartbeats, bounded retries, a `JobStep` table and a stuck-job reconciler | **Weak, and stated as weak — see 15.3.2.** Three reasons hold for 0.1–0.3: the unit of durability is a clinical `Job` that must be queryable under RLS in the same transaction as domain rows; the pipeline is linear per job, not a general DAG; and an on-prem hospital install must not grow a second stateful cluster. All three expire — see the adoption trigger MOS-REL-029. |
| **DICOM object writing** | highdicom; pydicom (raw); dcmjs; dcm4che; DCMTK | MIT; MIT; MIT; MPL-1.1/LGPL/GPL; BSD-style | **highdicom, mandated** (spine section 7) for SEG/SR/SC construction and encoding | A thin `dicomwriter` package mapping `ResultBundle` → highdicom constructors | Deterministic UID derivation; source-attribute inheritance; QIDO skip-if-present; the `legal_manufacturer` → Type 1 equipment-tag binding | Nothing about DICOM encoding is built. The wrapper exists because UID derivation and attribute inheritance are platform policy, not encoding — and highdicom's constructors *require* exactly that metadata, which forces the policy to be written rather than skipped. |
| **Model serving** | Triton; KServe; TorchServe; MONAI Deploy; Ray Serve | BSD-3; Apache-2.0; Apache-2.0; Apache-2.0; Apache-2.0 | Triton as the shipped inference runtime from 0.1.0 (spine section 14); whether it is addressed through an `InferenceBackend` port is OQ-10 (Chapter 16) | `InferenceBackend` port; per-version `backend` discriminator carrying GPU architecture and runtime version | Resolution by `(model_id, model_version)`; the pinned `PreprocessingSpec`; the golden-fixture startup self-test; the rule that a backend conversion is a **new** `ModelVersion` requiring its own `EvaluationRun` | The serving runtime is adopted. What is added is everything the serving runtime deliberately does not know: tenants, jobs, policy, provenance. A serialized engine is pinned to a GPU architecture and runtime version, so a version floor expressed against the product is meaningless — see MOS-REL-037. |
| **PACS** | Orthanc; dcm4chee-arc-light; cloud DICOM stores | **GPLv3**; MPL-1.1/GPL/LGPL; proprietary | Orthanc as the development and reference PACS driver | The DICOM Gateway, which holds the only PACS credential and speaks DICOMweb only | Tenancy filtering; de-identification; per-patient access audit; PACS substitutability | No PACS is built. **Licence consequence, stated because it constrains the code:** Orthanc is GPLv3, so no Orthanc source is linked, vendored or loaded into a first-party binary; interaction is DICOMweb over HTTP across a process boundary. See MOS-REL-034. |
| **Viewer** — **AMENDED at specification 0.4.0** | OHIF; Cornerstone3D; Weasis; 3D Slicer | MIT; MIT; EPL-2.0; BSD-style | ~~OHIF, unmodified and pinned by version~~ The DICOMweb wire format (`MOS-DATA-015`), the DICOM object model, and the standard's own definitions of SEG geometry (C.8.20) and TID 1500 — none of which is anybody's software. The incumbent list is retained and now names the pool `MOS-IMG-158`'s independent viewer and AT-11's reference renderer are drawn from | ~~An OHIF extension package (toolbar button + provenance panel) living outside the OHIF tree~~ Nothing. A row recording a build that also claimed a wrap would be describing a fork, and a fork is still refused. `medos/web/ohif-extension/` survives, is still served at `/medicalos/`, and is still bounded by the four contribution kinds retained in `MOS-UI-009a`; UNCHANGED, what those two surfaces MUST show and do is normative in Chapter 9 (provenance panel, MOS-SAFE-088; toolbar button, MOS-SAFE-089a), not here | ~~Nothing in rendering~~ The first-party clinician viewer at `viewer/`, served at `/mos-viewer/`: a WebGL2 stack renderer for uncompressed Explicit VR Little Endian CT, a SEG overlay resolved through `ReferencedSOPInstanceUID`, a TID 1500 measurement readout, one action and one panel — held to the four guarantees of `MOS-UI-009a` | ~~No viewer is built, and no viewer is forked. The extension is a separate package so that the pinned OHIF version is upgradable.~~ **BUILD, decided at release 0.4.0** and recorded in `docs/adr/BUILD_VS_ADOPT.md` §"Chapter 15 §15.3.1 — the Viewer row". The disqualifying property is characterisability under IEC 62304 §8.1.2 and it is measured there rather than asserted; branding and licence were considered and are recorded there as rejected reasons. RETAINED UNCHANGED: because the output contract is standard DICOM, correctness MUST be verified in a second, independent viewer (0.4.0 `second-viewer` gate) — a single-viewer check verifies the viewer, not the objects. |
| **Policy** | Cedar; OPA/Rego; Casbin; Oso | Apache-2.0; Apache-2.0; Apache-2.0; Apache-2.0/commercial | An existing engine for the attribute-based tier; the driver binding is Chapter 8's | The PDP port `Decide(subject, action, resource, context) -> PolicyDecision` | The attribute set a clinical rule can read (`clinical_use_mode`, deployment state, `regulatory_status`, target population); the default-deny catalogue; the fail-closed rule | RBAC is a table and is built. **A policy language MUST NOT be invented** (MOS-REL-035): a bespoke rule syntax is a parser, an evaluator, a test corpus and a decision-log format that three Apache-2.0 projects already ship. |
| **Workflow** | Argo Workflows; Airflow; Flyte; Prefect; Temporal | Apache-2.0 ×4; MIT | Argo Workflows (or an incumbent Airflow at a site that already runs one) for the **offline** evidence-plane path: dataset ingest, evaluation, packaging | — | A typed in-process step executor for the **serving** path, writing the `JobStep`/`job_steps` rows of Chapter 5 (MOS-EXEC-020 to MOS-EXEC-023) | Two different problems. Offline batch is adopted. The serving path builds a ~200-line typed executor because a general workflow engine on the inference path is the specific boundary violation the previous version got right and must keep: batch orchestration MUST NOT appear between a job and its result (MOS-REL-036). This executor runs the Chapter 5 step plan of a single `Job`; it is not a `Workflow` or `WorkflowVersion`, which are reserved and unbacked before 0.4.0 (Chapter 11, MOS-AGENT-004). |
| **De-identification** | RSNA CTP; `dicognito`; `pydicom`-based deid toolchains; dcm4che | RSNA terms; MIT; MIT; MPL/LGPL/GPL | A library for PS3.15 Annex E tag actions | Per-tenant profile selection | The **tenant-scoped, reversible, consistent UID mapping table**, and burned-in-pixel detection wiring | Tag actions are a solved table lookup. The mapping table is added because results reference source SOP Instance UIDs, so remapping consistency is a platform invariant that no de-identification library can enforce on the platform's behalf. |
| **Object storage** | MinIO; SeaweedFS; Ceph RGW; AWS S3 | **AGPLv3**; Apache-2.0; LGPL; proprietary | The **S3 API**, not any implementation | An `ObjectStore` port | Tenant-prefixed key layout; retention and expiry for intermediate artifacts | Nothing is built. **Licence consequence:** MinIO is AGPLv3, so no MinIO code is linked or vendored; only the S3 wire API is used, and any S3-compatible store satisfies the port. No proprietary cloud service is a mandatory core dependency (MOS-REL-060). |
| **Event bus** | Kafka; Redpanda; NATS JetStream | Apache-2.0; BSL; Apache-2.0 | Kafka as `JobQueue` driver 2 | The `JobQueue` port | Lifecycle topic naming; partition key `<tenant_id>:<study_instance_uid>`; per-topic DLQ | Nothing is built. Deferred to 0.3.0 because driver 1 makes the dual-write problem structurally absent. BSL-licensed alternatives are excluded from the default deployment by MOS-REL-034. |
| **Identity provider** | Keycloak; Ory Hydra/Kratos; Zitadel | Apache-2.0; Apache-2.0; Apache-2.0 | Adopted for OIDC from 0.3.0 | `Authenticator` port | API-key issuance and rotation only | An identity provider MUST NOT be built. API keys in 0.1.0 are a driver, not a competing implementation. |
| **Model weights (vendored)** | TotalSegmentator; nnU-Net-derived models; LCTSC-trained baselines | Code Apache-2.0; **weights licence varies and MUST be verified per artifact** | Off-the-shelf lung segmentation, vendored as a signed artifact | The artifact manifest | The `PreprocessingSpec` pin and the golden fixture | Lung segmentation is commoditised and MUST NOT be a research project inside this plan. **Every vendored weight MUST carry a recorded licence in its artifact manifest** (MOS-REL-038); a weight whose licence is unverified MUST NOT be vendored. |
| **Telemetry** | Prometheus; OpenTelemetry; Grafana | Apache-2.0 ×3 | All three, unmodified | Nothing | The field set and metric naming convention of Chapter 13 | Nothing is built. |

**The Viewer row now records BUILD, and the superseded cells are struck rather than deleted.** That row
answered ADOPT for the whole of specifications 0.1.0 through 0.3.0, and any conformance report written
against either of those versions was checked against that answer. Deleting the cells would leave a reader
holding such a report unable to tell whether the decision changed or was never recorded — the reason Chapter 19
§19.1.2 gives for striking `MOS-UI-009` rather than removing it, and it applies with more force to a register
whose whole function is to make contested decisions visible.

The authority under the old answer went first. `MOS-CORE-038` — the non-goal that made ADOPT the only
permitted answer — was reversed at release 0.4.0 and recorded at specification 0.3.0; `MOS-UI-009`, which
repeated the prohibition in operative form, was withdrawn in Chapter 19 §19.1.2 and replaced by `MOS-UI-009a`.
This row was the third place the same decision was written down, and it was not amended with the other two.
`MOS-REL-027` is unchanged and is what makes the amendment legible: it forbids recording "build"
without naming at least one incumbent that
was considered and the specific property that disqualified it. The incumbents are named in the row; the
disqualifying property is characterisability, argued and measured in `docs/adr/BUILD_VS_ADOPT.md` — 20.29 MB
over 51 requests to render a study list against 35.2 KB over 8 files, and 6.1 MB of WASM image codecs for
transfer syntaxes no object in the archive uses. Under IEC 62304 §8.1.2 a SOUP item must be characterised and
its anomaly list assessed, and that cost scales with the delivered surface rather than the used one. Branding
and licence are recorded there as rejected reasons, because a row that collected every motive would be
unfalsifiable.

**The incumbent list is kept, and it now does a different job.** It was the list the ADOPT decision chose
from. It is now the list `MOS-IMG-158`'s independent third-party viewer is drawn from and the pool AT-11's
reference renderer comes from, and that job is not optional once the platform writes the renderer as well as
the objects: a rendering check the platform runs against its own renderer cannot fail on a defect in the
object, which is the only defect it exists to catch. A row that recorded BUILD and dropped the list would
leave the second-viewer gate with no named candidates and would read as though the alternatives had stopped
existing.

**What the amendment does not do.** It does not relax `MOS-REL-032`: a first-party component MUST NOT
re-implement functionality available in an adopted dependency unless a register row records the reason, and
this row is that record rather than an exemption from the rule. It does not permit a fork: the row records a
build, a row recording a build that also claimed a wrap would be describing one, and `MOS-UI-205a` carries the
prohibition in operative form for any adopted viewer, from any source, including an upstream project's own
fork. And it does not supply the evidence the build now owes: `MOS-IMG-157a` and `MOS-IMG-158` check that
MedicalOS *output* renders in a viewer the platform did not write, the inverse — that the first-party viewer
renders foreign input correctly — has no check in this specification, and `docs/adr/BUILD_VS_ADOPT.md` records
it as unverified in those words. Register entry 106 in `docs/spec/99-known-inconsistencies.md` records the gap this
amendment closes and the one it does not.

#### 15.3.2. The two honest rows

**MOS-REL-028 — Durable execution.** The review's charge is accepted rather than deflected: the feature list this project intends to build for job durability — retries with backoff, leases, heartbeats, crash recovery, per-step state, compensation — is Temporal's feature list, and two of those are Temporal's headline features. The justification above is a real justification for 0.1–0.3 and it is also, honestly, a thin one that will not survive the platform's growth. It is recorded as time-limited rather than permanent.

**MOS-REL-029** MedicalOS MUST adopt a durable-execution engine behind the `JobQueue`/orchestrator seam, rather than extend the in-house engine, as soon as **any** of the following becomes a requirement: child workflows or nested executions; signals or external events delivered to a running execution; timers spanning more than the maximum job deadline; saga compensation across more than two mutating steps; or fan-out/fan-in across more than one service per job. **MOS-REL-030** No feature that duplicates a durable-execution primitive MUST be added to the in-house engine without a recorded ADR that states why MOS-REL-029 has not been triggered.

**MOS-REL-031 — DICOM object writing.** The previous version placed DICOM object generation in the modality worker, on the agent and implicitly in a store tool — three owners across four sections, and no library named. That is deleted. highdicom is adopted, the platform is the sole writer (spine section 2), and the value of adopting it is not that it saves encoding work: it is that its constructors *require* the equipment identity, frame-of-reference and source-image metadata that the previous version omitted, so adoption forces the identity contract to be answered instead of postponed. A hand-rolled encoder would have allowed the omission to persist.

#### 15.3.3. Rules arising from the register

**MOS-REL-032** A first-party component MUST NOT re-implement functionality available in an adopted dependency in order to avoid the dependency, unless a register row records the reason.

**MOS-REL-033** MONAI Deploy's MAP format MUST NOT be adopted as the service packaging boundary. A MAP is an invoked standalone application; the platform's unit is a queue-consuming service with a lease and a heartbeat, and forcing the two together fights the execution contract of Chapter 5. This is recorded as a considered-and-rejected decision so that it is not re-proposed as an unexamined simplification. Chapter 17 `MOS-TRAIN-026` extends the same refusal from the service packaging boundary to the model artifact format and to execution, and cites this requirement rather than restating it.

**MOS-REL-034** No GPL-, LGPL-, AGPL- or BSL-licensed component MUST be statically linked, dynamically linked, vendored into, or loaded in-process by any first-party MedicalOS binary. Interaction with such components MUST be across a process boundary over a documented network protocol. Any first-party code that must be GPL-licensed (for example a PACS in-process callback plugin) MUST live in its own directory with its own `LICENSE` file and a `README.md` stating why, MUST contain no MedicalOS domain logic, and MUST do nothing but forward an identifier over HTTP to the gateway.

**MOS-REL-035** MedicalOS MUST NOT define a policy language. The attribute-based tier MUST use an existing engine bound behind the PDP port of Chapter 8.

**MOS-REL-036** A batch workflow engine MUST NOT appear on the serving path between a `Job` and its `Result`.

**MOS-REL-037** Compatibility MUST be expressed against a port version or an observable capability, never against a third-party product version. A declaration of the form `min_triton: "2.x"` is meaningless for a serialized inference engine pinned to a GPU architecture and a runtime build, and MUST be replaced by the `(backend, gpu_architecture, runtime_version)` triple recorded on the `ModelVersion`.

**MOS-REL-038** Every dependency — library, container base image and model weight — MUST have a recorded licence in the SBOM, and every vendored model weight MUST additionally have its licence recorded in its artifact manifest. CI MUST fail on a dependency whose licence is absent or on the deny-list.

### 15.4. Engineering rules

These bind human contributors and coding agents identically. The previous version's twenty rules were mostly right; the table below records each one's disposition, because silently dropping a rule and silently keeping a contradictory one are the two failure modes an agent cannot detect.

#### 15.4.1. Disposition of the previous rules

| # | Previous rule | Disposition | ID | Rule as it now stands |
|---|---|---|---|---|
| 1 | No unnecessary microservices | Kept, sharpened | **MOS-REL-039** | There MUST be one deployable per trust or runtime boundary and no more. The boundaries are: control plane (Go), DICOM Gateway (Go), native worker (Python), each sealed service (vendor image), viewer, PACS, database, object store. A new deployable requires a register row. |
| 2 | No model-per-service architecture | **Amended — contradicted the sealed mode** | **MOS-REL-040** | Native-mode models MUST NOT each receive their own deployable; they are resolved at runtime by a shared worker. Sealed-mode services ARE one deployable per `ServiceVersion` by design, and that is not a violation of this rule. |
| 3 | No models baked into worker images | **Amended for the same reason** | **MOS-REL-041** | A native-mode worker image MUST NOT contain model weights; weights are fetched by `(model_id, model_version)` from the artifact store at startup. **MOS-REL-042**: a sealed-mode service image MAY contain its weights and MUST declare the image digest that covers them; its weights are never surrendered to the platform. |
| 4 | No DICOM volumes in Kafka | Kept, generalised | **MOS-REL-043** | Pixel data MUST NOT be placed on the event bus, in a job payload, or in an API response body. Large objects travel by reference with a content hash, spatial metadata and an expiry. |
| 5 | No inference in the control plane | Kept | **MOS-REL-044** | The control plane MUST NOT load a model, allocate a GPU, or process pixel data. |
| 6 | Agent has no direct infrastructure access | Kept, widened to all services | **MOS-REL-045** | A `Service` MUST NOT write DICOM, hold PACS credentials, or reach the database, broker or object store directly. Everything it needs arrives through the service contract of Chapter 2. |
| 7 | No global state | Kept, sharpened | **MOS-REL-046** | No package-level mutable state. Configuration, clocks, random sources and clients are injected. A test MUST be able to construct two independent instances of any component in one process. |
| 8 | Make operations idempotent | **Fixed — was a blanket claim, and blanket retry is unsafe** | **MOS-REL-047** | Idempotency MUST be a **declared per-operation property**, not a global assumption. Every operation exposed on a port or a tool contract declares `idempotent: bool` and `retryable_errors[]`. The retry machinery MUST read the declaration. Blanket retry of undeclared operations is forbidden, because a retrieve and a store have opposite retry safety. |
| 9 | Explicit interfaces | Kept, sharpened | **MOS-REL-048** | Every subsystem named in the build-versus-adopt register MUST have an explicit interface and at least two implementations in the tree, one of which MAY be a test fake. A single-implementation interface that no test substitutes is not a seam. |
| 10 | Tests written with the code | Kept, scoped | **MOS-REL-049** | Tests MUST land in the same change as the code, at the depth the change class requires (15.9). |
| 11 | No placeholder implementations on critical paths | **Fixed — collided with deliberately-incomplete early slices** | **MOS-REL-050** | A **spike** — time-boxed, on a branch, never merged to the default branch as a critical-path component, deleted or re-implemented behind a named interface at the end of its box — is permitted and is how weeks 0 and 1–2 are built. A **placeholder** — a stub that returns a plausible value on any path that can produce a `Result`, a DICOM object, a measurement, a triage decision or a policy decision — is forbidden without exception. A path that is not yet implemented MUST fail loudly, not return a default. |
| 12 | Do not hide errors | Kept, sharpened | **MOS-REL-051** | No bare `except:`, no discarded error return, no empty `catch`. An intentionally swallowed error MUST carry a comment stating why and MUST increment a named metric. |
| 13 | Structured logging | Kept, plus the PHI rule | **MOS-REL-052** | All logs are structured with the Chapter 13 field set. **MOS-REL-053**: PHI MUST NOT appear in logs, span attributes, event payloads, metric labels or LLM prompts, and CI MUST run a scanner over log call sites for known PHI-bearing field names. |
| 14 | Use context cancellation | **Amended — the previous rule implied a capability that does not exist** | **MOS-REL-054** | In-process cancellation MUST use `context.Context` in Go and cooperative cancellation in Python. **Cross-process cancellation of a running inference is not implemented before 0.3.0**; `CANCELLED` and the `job.cancel` permission are reserved. Code MUST NOT imply a delivery path that does not exist. |
| 15 | Use migrations | Kept, extended | **MOS-REL-055** | Every schema change is a numbered migration. Each migration MUST be reversible or MUST declare itself irreversible in a header comment with a reason. |
| 16 | Version the API | Kept | **MOS-REL-056** | The HTTP API is versioned in the path and its shape is generated (15.6). |
| 17 | No breaking change without a major version | **Fixed — unworkable pre-1.0 and it read as a prohibition on all change** | **MOS-REL-057** | Before 1.0.0, a MINOR release MAY contain a breaking change to an internal schema or package API, provided the change is named in `CHANGELOG.md` and carries a migration note in `MIGRATIONS.md`. **MOS-REL-058**: the public `Job` state enum is exempt and MUST NOT be widened, reordered or re-spelled — new pipeline stages go in the open `phase` string, so that adding a stage is never a breaking change. |
| 18 | External dependencies documented | Upgraded | **MOS-REL-059** | Documentation of a dependency means a register row, a licence entry and an SBOM entry — not a sentence in a README. |
| 19 | No dependency without necessity | Kept | — | Covered by MOS-REL-027 and MOS-REL-032. |
| 20 | No proprietary cloud service as a mandatory core dependency | **Kept verbatim** | **MOS-REL-060** | No proprietary cloud service MUST be required for any core function. A cloud service MAY be a driver; it MUST NOT be the floor. The full stack MUST be installable and runnable air-gapped. |

#### 15.4.2. Rules added by this version

| ID | Rule |
|---|---|
| **MOS-REL-061** | There MUST be exactly one implementation of preprocessing, in one versioned package, imported by both the serving path and any training or evaluation path. A second implementation is a defect regardless of whether it agrees. |
| **MOS-REL-062** | PostgreSQL is the sole source of truth for job state. No code MUST read the event bus to reconstruct state. A read model derived from the bus MUST NOT back any API response. |
| **MOS-REL-063** | Every change that adds a code path MUST add a metric or a structured-log event on that path, using the Chapter 13 conventions. |
| **MOS-REL-064** | Each enclosing timeout MUST exceed the sum of its children plus a stated margin. The nesting order and the margin MUST be recorded in one place in configuration, not spread across call sites. An outer timeout shorter than an inner one doubles GPU load per retry lap with no error to look at. |
| **MOS-REL-065** | A queue-full or load-shed response MUST NOT consume a job's retry budget. Attempt counts are carried in the message envelope and incremented only on a genuine execution attempt. |
| **MOS-REL-066** | Every mutating clinical path MUST be forward-only past its commit point. A superseded result is marked `superseded_by`; nothing is deleted. Compensation is not implemented and MUST NOT be assumed. |
| **MOS-REL-067** | Deadlines MUST be absolute timestamps, never durations. Only an absolute deadline survives a queue hop. |
| **MOS-REL-068** | `audit_events` MUST be append-only at the database role level; the application role MUST NOT hold `UPDATE` or `DELETE` on it. |
| **MOS-REL-069** | No clinically load-bearing number — a measurement, a volume, a probability, a count, a laterality — MUST be produced, transformed or re-stated by an LLM. Narrative text that restates such a number MUST be checked against the source `Result` and MUST fail closed on an unmatched token. |
| **MOS-REL-070** | Every DICOM object identity MUST be derived deterministically from the documented inputs. Random UID generation on a result path is forbidden. |
| **MOS-REL-071** | The policy decision path MUST be fail-closed and MUST NOT be wrapped in a circuit breaker, a cache with a stale-while-revalidate policy, or a timeout that falls back to allow. "Policy engine unavailable" is a deny. |
| **MOS-REL-072** | Every topic, queue and routing key MUST be lifecycle-scoped or service-scoped. Modality topics and nosology topics are forbidden: modality is an input constraint in `SeriesSelector` and nosology is an output claim in `Capability`, and neither is topology. |
| **MOS-REL-073** | Any change that adds or alters a clinical path MUST add at least one case to the named fixture corpus of Chapter 14 — the corpus is the regression memory of every defect class the project has met. |
| **MOS-REL-074** | A test MUST NOT assert only on a value returned by a mock it configured in the same test. At least one assertion MUST observe a real effect: a database row, a written object, a metric, a status code. |
| **MOS-REL-075** | Skipping or quarantining a test MUST require an issue ID and a named owner in the skip annotation. An unattributed skip fails CI. |

#### 15.4.3. Rules specific to coding agents

These exist because the declared consumer of this document reads it section-locally, decides quickly, and does not notice an absent contract — it invents one.

| ID | Rule |
|---|---|
| **MOS-REL-076** | An agent MUST read the spine and the requirement IDs governing the file it is about to change before changing it, and MUST cite the governing requirement ID in the change description. |
| **MOS-REL-077** | An agent MUST NOT invent a value for a DICOM attribute, a coded concept, a UCUM unit, an OID root, a threshold or a tolerance. If the value has no source in this specification or in a registry row, the agent MUST stop and ask. Values invented here ship into patient records. |
| **MOS-REL-078** | An agent MUST NOT hand-write a type, model or API document that the codegen pipeline produces (15.6), and MUST NOT edit a generated file. Generated files carry a `DO NOT EDIT` header and CI enforces it. |
| **MOS-REL-079** | When the specification and the code disagree, the specification wins. The agent MUST open an issue describing the divergence and MUST NOT change the specification and the code in the same change. |
| **MOS-REL-080** | An agent MUST NOT commit PHI, a real patient identifier, or an unmapped source SOP Instance UID — including in a test fixture, a log sample or a documentation example. |
| **MOS-REL-081** | Every normative constraint MUST be restated at its point of enforcement: a code comment naming the requirement ID, and a test whose name contains that ID. Redundancy across the specification, the comment and the test name is deliberate and MUST NOT be refactored away. |
| **MOS-REL-082** | An agent MUST NOT widen the scope of a change beyond the requirement it cites. Unrelated cleanups go in their own change. |
| **MOS-REL-083** | If a task requires a contract that no chapter defines, the agent MUST stop and ask rather than invent one. An invented contract becomes load-bearing and undocumented within one release. |

### 15.5. Language conventions

#### 15.5.1. Go — control plane, DICOM Gateway, API

**MOS-REL-084** The control plane MUST remain in Go. The language boundary at the control-plane / service-runtime seam is the cheapest available enforcement of the rule that a service has no infrastructure access: in a shared Python process, "the service cannot reach PostgreSQL" is a convention that any `import sqlalchemy` defeats. The duplication cost the review identified is real and is paid down by codegen (15.6), not by merging the runtimes.

**MOS-REL-085** Go conventions, all CI-enforced:

| Concern | Rule |
|---|---|
| Version | Go 1.24 or newer, pinned by a `toolchain` directive in `go.mod`. One module: `github.com/medicalos/medicalos`. |
| Layout | `cmd/<binary>/main.go` for entry points; `internal/` for everything else; no `pkg/`. |
| Context | `context.Context` is the first parameter of every function that performs I/O. Never stored in a struct. |
| Errors | Wrapped with `fmt.Errorf("verb noun: %w", err)`. Sentinels as `var ErrNoEligibleSeries = errors.New("no eligible series")`. Every domain error maps to an RFC 9457 problem with a `class` value; the mapping is a single exhaustive function. |
| Panics | `panic()` MUST NOT appear in a request, job or consumer path. Permitted only in process initialisation, in a `must*` helper called from `main`. |
| Logging | `log/slog` with a JSON handler and the Chapter 13 field set. `fmt.Print*` MUST NOT appear outside `cmd/` CLI output. |
| Database | `pgx/v5` with `sqlc`-generated query code. No ORM. Every query executes through the single repository chokepoint that sets the RLS session variable; a direct `pool.Query` outside that package fails a custom lint rule. |
| Concurrency | Every goroutine has an owner and a lifetime: `errgroup.Group` or an explicit `sync.WaitGroup` with a context. A bare `go func()` in non-test code fails review. |
| Enums | String constants with an exhaustive switch check via the `exhaustive` linter. |
| Tests | `testing` plus `testify/require`; table-driven; `testcontainers-go` for Postgres integration tests; `-race` on in CI. |

```yaml
# .golangci.yml — the enabled set is normative; additions require a PR, removals require an ADR
version: "2"
run:
  timeout: 5m
linters:
  default: none
  enable:
    - errcheck        # MOS-REL-051: no discarded errors
    - govet
    - staticcheck
    - ineffassign
    - unused
    - bodyclose
    - contextcheck    # MOS-REL-085: context threading
    - errorlint       # wrapped-error comparisons
    - exhaustive      # MOS-REL-058: switch over the Job state enum stays exhaustive
    - forbidigo       # MOS-REL-052: no fmt.Print* outside cmd/
    - noctx
    - rowserrcheck
    - sqlclosecheck
    - gosec
  settings:
    exhaustive:
      default-signifies-exhaustive: false
    forbidigo:
      forbid:
        - pattern: '^fmt\.Print.*$'
        - pattern: '^time\.Now$'   # MOS-REL-046: inject the clock
      exclude-godoc-examples: true
```

#### 15.5.2. Python — workers, services, evidence tooling

**MOS-REL-086** Python conventions, all CI-enforced:

| Concern | Rule |
|---|---|
| Version | Python 3.12. One version across workers, services and evidence tooling. |
| Environment | `uv` with `pyproject.toml` and a committed `uv.lock`. A build that resolves dependencies without the lock file fails. |
| Typing | `mypy --strict` on `medicalos_*` packages. Untyped third-party imports are stubbed or explicitly ignored per module with a reason. |
| Lint/format | `ruff check` and `ruff format`. No competing formatter. |
| Wire types | Pydantic v2 models **generated** from JSON Schema (15.6). Hand-written wire models fail CI. |
| Logging | stdlib `logging` with a JSON formatter emitting the same field set as the Go side. `print()` MUST NOT appear outside `__main__` blocks in CLI tools. |
| Numerics | `numpy`, `SimpleITK`, `pydicom`, `highdicom` pinned to exact versions. `torch` pinned with the CUDA wheel index recorded in `pyproject.toml`. |
| Determinism | Seeds set and recorded in the provenance record for any stochastic step. Test-time augmentation axes come from the `PreprocessingSpec`, never from code defaults. |
| Startup | Model weights and the `PreprocessingSpec` load at process start, and the golden-fixture self-test runs before the process reports ready. Lazy loading inside a request handler is forbidden. |
| Tests | `pytest`; `hypothesis` for geometry invariants (round-trip, orientation, spacing); fixtures drawn from the named corpus of Chapter 14. |
| PHI | Exception messages MUST NOT include patient identifiers, accession numbers or source UIDs. Tests assert this on the exception paths of the gateway and the worker. |

```toml
# pyproject.toml (excerpt) — the tool configuration is normative
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "A", "C4", "T20", "RET", "SIM", "ARG", "PTH", "PL", "RUF"]
# T20 bans print(); ARG bans unused arguments that hide a dropped parameter

[tool.mypy]
python_version = "3.12"
strict = true
warn_unreachable = true
disallow_any_explicit = true

[tool.pytest.ini_options]
addopts = "--strict-markers --strict-config -ra"
markers = [
  "integration: requires a live PACS and database",
  "fixture_corpus: exercises the named DICOM fixture corpus",
]
```

**MOS-REL-087** Rust MUST NOT be introduced without a register row demonstrating a measured need. The previous version listed it speculatively across four use cases; a third language triples the codegen surface.

### 15.6. One schema source, and codegen

**MOS-REL-088** There MUST be exactly one authoritative definition of every cross-process data shape, and every language binding MUST be generated from it. The previous version hand-maintained an OpenAPI file, mandated Pydantic in a Python runtime facing a Go control plane, and had no single source anywhere across 25 entities, 25 tables, three SDKs and an event envelope — which is a guaranteed three-way drift.

#### 15.6.1. The sources

| Shape | Authoritative source | Format |
|---|---|---|
| Manifests (`service`, `model`, `preprocessing`, `dataset`, `capability`) | `medos/schemas/<kind>/*.schema.json` | JSON Schema 2020-12 |
| Event envelope and job lifecycle events | `medos/schemas/envelope/*.schema.json` | JSON Schema 2020-12 |
| `ResultBundle`, `Finding`, `Measurement` | `medos/schemas/result/*.schema.json` | JSON Schema 2020-12 |
| RFC 9457 problem document and the `class` enum | `medos/schemas/api/problem.schema.json` | JSON Schema 2020-12 |
| HTTP routes, methods, status codes, auth and pagination | `medos/schemas/api/routes.yaml` | project route manifest |
| Database tables | `control-plane/migrations/*.sql` | SQL DDL |

```yaml
# medos/schemas/api/routes.yaml (excerpt) — the only hand-written half of the API definition
version: 1
base_path: /api/v1
pagination: cursor
routes:
  - id: jobs.create
    method: POST
    path: /jobs
    summary: Create a job for a service version against a study
    auth: [api_key]
    permission: job.create
    request_body: medos/schemas/job/job_create.schema.json
    responses:
      "202":
        body: medos/schemas/job/job_accepted.schema.json
        headers: [Location]
      "400": problem
      "403": problem
      "409": problem
  - id: jobs.get
    method: GET
    path: /jobs/{job_id}
    auth: [api_key]
    permission: job.read
    path_params:
      job_id: {type: string, format: uuid}
    responses:
      "200":
        body: medos/schemas/job/job.schema.json
      "404": problem
  - id: jobs.events
    method: GET
    path: /jobs/{job_id}/events
    auth: [api_key]
    permission: job.read
    produces: text/event-stream
    responses:
      "200":
        body: medos/schemas/envelope/job_event.schema.json
        streaming: true
```

#### 15.6.2. The generators

| Target | Tool | Output path |
|---|---|---|
| Go structs from JSON Schema | `github.com/atombender/go-jsonschema` | `control-plane/internal/schemas/schemas.gen.go` |
| Pydantic v2 models from JSON Schema | `datamodel-code-generator` | `runtime/medicalos_schemas/models.py` |
| TypeScript types from JSON Schema | `json-schema-to-typescript` | `medos/web/src/schemas/index.gen.ts` |
| OpenAPI 3.1 document | `medos/tools/openapigen` (in-repo; reads `routes.yaml` + the schemas) | `docs/api/openapi.yaml` |
| Go SDK client | `oapi-codegen` | `sdk/go/client.gen.go` |
| Python SDK client | `openapi-python-client` | `sdk/python/medicalos/client/` |
| Go query code from SQL | `sqlc` | `control-plane/internal/store/*.sql.go` |

```make
# Makefile (excerpt)
SCHEMAS := $(shell find schemas -name '*.schema.json')

.PHONY: generate
generate: gen-go gen-py gen-ts gen-openapi gen-sdk gen-sql

gen-go:
	go run github.com/atombender/go-jsonschema@v0.17.0 \
	  --package schemas --only-models \
	  --output control-plane/internal/schemas/schemas.gen.go $(SCHEMAS)

gen-py:
	uv run datamodel-codegen \
	  --input schemas --input-file-type jsonschema \
	  --output runtime/medicalos_schemas/models.py \
	  --output-model-type pydantic_v2.BaseModel \
	  --target-python-version 3.12 --use-standard-collections --strict-nullable

gen-ts:
	npx json-schema-to-typescript@15 --input 'medos/schemas/**/*.schema.json' \
	  --output medos/web/src/schemas/index.gen.ts --bannerComment '// Code generated by make generate. DO NOT EDIT.'

gen-openapi:
	go run ./tools/openapigen --routes medos/schemas/api/routes.yaml --schemas schemas --out docs/api/openapi.yaml

gen-sdk: gen-openapi
	go run github.com/oapi-codegen/oapi-codegen/v2/cmd/oapi-codegen@v2.4.1 \
	  -generate client,types -package client -o sdk/go/client.gen.go docs/api/openapi.yaml
	uv run openapi-python-client generate --path docs/api/openapi.yaml --output-path sdk/python/medicalos/client --overwrite

gen-sql:
	sqlc generate

.PHONY: verify-generated
verify-generated: generate
	git diff --exit-code -- \
	  control-plane/internal/schemas runtime/medicalos_schemas medos/web/src/schemas \
	  docs/api/openapi.yaml sdk control-plane/internal/store
```

```yaml
# .github/workflows/ci.yml (excerpt) — the drift gate
  schema-drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make verify-generated
      - name: No hand-edited generated files
        run: |
          set -e
          for f in $(git ls-files '*.gen.go' '*.gen.ts' 'docs/api/openapi.yaml'); do
            head -3 "$f" | grep -q 'DO NOT EDIT' || { echo "missing DO NOT EDIT banner: $f"; exit 1; }
          done
      - name: SPDX header on every first-party source file
        run: |
          set -e
          missing=$(git ls-files '*.go' '*.py' '*.ts' | grep -v '\.gen\.' \
            | xargs grep -L 'SPDX-License-Identifier:' || true)
          test -z "$missing" || { echo "missing SPDX header:"; echo "$missing"; exit 1; }
```

**MOS-REL-089** `make verify-generated` MUST run in CI on every pull request and MUST fail the build on any diff. **MOS-REL-090** Every generated file MUST carry a `DO NOT EDIT` banner and MUST NOT be edited by hand. **MOS-REL-091** `docs/api/openapi.yaml` MUST be generated; a hand-edit to it fails MOS-REL-089 by construction. **MOS-REL-092** A schema change that is breaking MUST bump the schema's `$id` version segment and MUST be recorded in `MIGRATIONS.md`. **MOS-REL-093** SDKs MUST be generated from the generated OpenAPI document; a hand-written SDK method fails review. **MOS-REL-094** A shape that crosses a process boundary MUST NOT be declared in Go or Python source first and reverse-engineered into a schema afterwards.

### 15.7. Documentation set

**MOS-REL-095** The following documents MUST exist, and CI MUST fail if a required document is absent or if a generated document is stale.

| Path | Content | Generated? | CI check |
|---|---|---|---|
| `README.md` | What MedicalOS is, the positioning line verbatim, quickstart, links | No | Link checker; positioning line present verbatim |
| `ARCHITECTURE.md` | The planes, the service contract, the ownership boundary | No | Link checker |
| `DEVELOPMENT.md` | Toolchain versions, `make generate`, how to run the stack, where development data comes from and how it is de-identified | No | Every named command exists as a `make` target |
| `DEPLOYMENT.md` | docker compose and orchestrated install, configuration reference | Partly (configuration table generated from the config struct) | Drift check |
| `SECURITY.md` | Reporting channel, response commitment, supported versions | No | Present |
| `CONTRIBUTING.md` | How to contribute, and the sign-off regime of 15.8 | No | Contains a sign-off section |
| `CODE_OF_CONDUCT.md` | Contributor Covenant 2.1 with a named contact | No | Present |
| `GOVERNANCE.md` | Decision-making, maintainers, how a decision is recorded | No | Present |
| `CHANGELOG.md` | Keep-a-Changelog format, one section per release | No | Contains a section for the tag being released |
| `MIGRATIONS.md` | Every breaking change with its upgrade action | No | Entry exists for each migration marked breaking |
| `docs/api/openapi.yaml` | The HTTP API | **Yes** | `make verify-generated` |
| `docs/dicom/CONFORMANCE.md` | DICOM Conformance Statement: SOP Classes written, transfer syntaxes, DICOMweb services used, character sets, the deterministic UID derivation rule, the source-attribute inheritance table | Partly (the SOP class and attribute tables generated from the writer's configuration) | Drift check; every SOP class the writer can emit appears |
| `docs/evidence/VALIDATION_REPORT.md` | The `ValidationReport` format and the offline verification procedure | Partly (schema section generated) | Drift check |
| `docs/services/AUTHORING.md` | How a third party builds, packages, signs and registers a `ServiceVersion` | No | Every field named exists in the generated manifest schema |
| `docs/adr/NNNN-<slug>.md` | One record per architectural decision, including `BUILD_VS_ADOPT.md` | No | Filename pattern; every ADR has Status/Context/Decision/Consequences |
| `docs/releases/<version>.md` | Release Decision Record (MOS-REL-003) | No | Exists for each tag |

**MOS-REL-096** The documentation set MUST NOT contain a document for a subsystem that is outside the shipped scope. The previous version made a FHIR document mandatory while FHIR appeared in neither the MVP scope nor the non-goals, which is an instruction to an agent to build a client that nothing consumes. A document is added when its subsystem enters a release's contents, not before.

**MOS-REL-097** No document MUST describe the platform as performing clinical validation, conferring clinical validity, or diagnosing. Install and onboarding flows MUST name their steps by what they do — integrity check, compatibility check, functional smoke test — rather than borrowing a regulatory word the platform cannot support. CI MUST grep the documentation set for the forbidden phrases and fail on a match.

**MOS-REL-098** Every requirement ID in this specification MUST be greppable from the repository: either a test name, a code comment, or a documentation anchor cites it. An orphan requirement ID is either unimplemented or misnumbered, and CI MUST report the list.

### 15.8. Open-source governance

#### 15.8.1. Licence

**MOS-REL-099** MedicalOS core MUST be licensed **Apache License 2.0**. The reasons are specific: an explicit patent grant, which matters in a field with dense imaging-algorithm patents; permissive enough that a hospital's legal review is not a project; and no copyleft obligation that would follow a site's local integration code.

**MOS-REL-100** Every first-party source file MUST carry `SPDX-License-Identifier: Apache-2.0` as a header comment, enforced by the CI check in 15.6.2. **MOS-REL-101** Third-party code included in the tree MUST live under `third_party/<name>/` with its upstream `LICENSE`, its version, and the reason it is vendored rather than depended on. **MOS-REL-102** Any first-party directory whose licence differs from the core (the GPLv3 PACS callback under MOS-REL-034 is the only such case foreseen) MUST carry its own `LICENSE` and a `README.md` stating the boundary.

#### 15.8.2. DCO versus CLA — the decision, stated as a decision

The previous version left this unaddressed. It is one line in `CONTRIBUTING.md`, it cannot be added after contributors arrive, and it determines whether the project can ever dual-licence or relicense.

| | **DCO** (Developer Certificate of Origin 1.1) | **CLA** (individual + corporate) |
|---|---|---|
| Mechanism | `Signed-off-by:` trailer on every commit, enforced by a bot | A signed agreement recorded before the first merge, tracked by a bot |
| Contributor friction | One `git commit -s` flag | An out-of-band legal step; kills drive-by fixes and slows corporate contributors by weeks |
| What the project receives | A contributor's assertion that they have the right to submit the work under the project's licence | A copyright licence (or assignment) plus an explicit patent grant from every contributor |
| Relicensing or dual-licensing later | Effectively impossible — requires contacting every contributor | Possible unilaterally, if the CLA grants it |
| Enforcement against infringers | Each contributor holds their own copyright | Centralised standing, if assignment is taken |
| Common precedent | Linux, Kubernetes, most CNCF projects | Apache Software Foundation (ICLA), and companies that later changed licence |
| Effect on this project's stated business model | None — the revenue in this plan is the evidence layer and the certification mark, not relicensing the core | Preserves an option the plan does not currently need |
| Reversibility | A project can tighten DCO → CLA only for future contributions, and the existing corpus stays DCO | A project can relax CLA → DCO at any time |

**MOS-REL-103** The choice MUST be recorded in `CONTRIBUTING.md` **before the repository accepts its first contribution from outside the copyright holder**, and until it is recorded the project MUST NOT merge such a contribution. **MOS-REL-104** Whichever is chosen MUST be bot-enforced on every pull request; a manual check is not enforcement. **MOS-REL-105** The decision itself is carried to Chapter 16 as an open question and MUST NOT be resolved implicitly by a template, a default in a repository-creation wizard, or a first pull request that happens to be signed off.

#### 15.8.3. The plugin boundary is technical, not legal

**MOS-REL-106** The extension boundary MUST be stated as a technical boundary. Apache-2.0 imposes no copyleft, so the core licence separates nothing: a proprietary extension is equally lawful whether it is a linked library or a separate process, and a sentence saying "plugins may have separate licences" describes a permission that already exists rather than a boundary that constrains anything.

**MOS-REL-107** The boundary that does exist and is enforceable: a third-party extension is an **OCI image plus a signed manifest, executed out-of-process**, communicating only over the versioned service contract of Chapter 2. **MOS-REL-108** MedicalOS MUST NOT offer an in-process plugin API — no shared-library loading, no dynamic module import, no user-supplied code executed inside a platform process. **MOS-REL-109** The manifest, its media types, its signature and its SBOM attachment are defined in Chapter 6; this chapter requires only that they exist as a single named packaging unit, because a supply-chain requirement to sign artifacts is unimplementable while the artifact is an undefined noun — which was the previous version's state.

**MOS-REL-110** The boundary MUST be verified, not asserted: the `sealed-mode-isolation` gate check of 0.3.0 proves that a third-party container cannot reach the database, the broker or the object store.

#### 15.8.4. The certification mark

**MOS-REL-111** A "Validated on MedicalOS" mark, if adopted, MUST be a **certification mark**, and a certification mark has three requirements that constrain the product, not just the paperwork: it MUST be distinctive; it MUST be backed by a published standard that is actually applied; and it MUST be owned by an entity that does not itself sell goods in the certified class.

**MOS-REL-112** The published standard for the mark MUST be the `ValidationReport` format plus the named acceptance battery, so that the mark asserts something falsifiable rather than something atmospheric.

**MOS-REL-113** The third requirement is a live conflict for this project and MUST be recorded rather than discovered later: the first-party clinical application is itself a candidate for certification, and an entity that certifies the models it also sells has a mark worth nothing to a hospital procurement committee. Either the mark is owned by a separate entity, or the first-party application is excluded from certification, or the mark is not adopted.

**MOS-REL-114** A trademark clearance search MUST be completed and recorded before the repository is made public. "MedicalOS" is descriptive for medical software and the radiology-AI "OS" naming slot has incumbents, so the name may be unregistrable as a mark even though it is perfectly usable as a product name. This is an afternoon's work with a searchable register, and it is worth exactly that much now — no more.

**MOS-REL-115** No certification-mark language — badges, "Validated on MedicalOS" strings, marketing copy in the README — MUST appear in the repository until MOS-REL-113 and MOS-REL-114 are recorded.

### 15.9. Definition of Done

The previous version's Definition of Done listed eleven gates applied uniformly to every change, which has one of two outcomes: either it is honoured and a typo fix requires a backward-compatibility analysis, or it is quietly ignored and the gates protect nothing. It also required metrics on every feature while scheduling observability at step 18 of 20, so steps 1–17 were defined as non-compliant. Both are fixed by making the gates a function of what the change can break.

#### 15.9.1. Change classes

**MOS-REL-116** Every pull request MUST declare exactly one change class as a label. CI reads the label and enforces the matching gate column. An undeclared class defaults to **C3**.

| Class | Definition |
|---|---|
| **C0** | Documentation, comments, or non-executed content only. |
| **C1** | Internal change with no public surface: no schema, no API, no database shape, no clinical path. |
| **C2** | Platform surface change: API, registry, storage, configuration, deployment, or a public port. |
| **C3** | **Clinical-path change**: anything that can alter a stored `Result`, a generated DICOM object, a measurement, a triage or selection decision, a preprocessing step, a policy decision, or a `clinical_use_mode` behaviour. |
| **C4** | Time-boxed spike on a branch, per MOS-REL-050. Never merged to the default branch as a critical-path component. |

#### 15.9.2. The gate matrix

**MOS-REL-117** A change MUST NOT merge until every cell marked MUST in its class column is satisfied.

| Gate | C0 | C1 | C2 | C3 | C4 |
|---|---|---|---|---|---|
| Cites a requirement ID in the title | MUST | MUST | MUST | MUST | — |
| Lint and type checks pass | MUST | MUST | MUST | MUST | — |
| Unit tests for changed logic | — | MUST | MUST | MUST | — |
| Integration test against a real dependency | — | — | MUST | MUST | — |
| Named-corpus fixture case added (MOS-REL-073) | — | — | — | MUST | — |
| DICOM battery green (Chapter 14) | — | — | — | MUST | — |
| Evidence re-run: the affected `EvaluationRun` repeated and per-case deltas reported | — | — | — | MUST | — |
| `make verify-generated` clean | MUST | MUST | MUST | MUST | — |
| Migration present, reversible or declared irreversible | — | — | MUST if schema changes | MUST if schema changes | — |
| Metric or structured-log event on the new path (MOS-REL-063) | — | SHOULD | MUST | MUST | — |
| Error paths mapped to RFC 9457 with a `class` value | — | — | MUST | MUST | — |
| `CHANGELOG.md` entry | — | — | MUST | MUST | — |
| `MIGRATIONS.md` entry | — | — | MUST if breaking | MUST if breaking | — |
| ADR recorded | — | — | MUST if it changes a register row or a port | MUST if it changes a clinical contract | — |
| Security review (Chapter 8) | — | — | MUST if it touches auth, tenancy, PHI or the gateway | MUST | — |
| Documentation updated | MUST | — | MUST | MUST | — |
| Named human approver | — | — | — | MUST | — |
| Deleted or re-implemented behind a named interface at the end of its time box | — | — | — | — | MUST |

**MOS-REL-118** The matrix MUST be enforced by CI and by a pull-request template checklist, never by reviewer memory. **MOS-REL-119** A C3 change MUST NOT be relabelled to a lighter class to bypass a gate; the class is determined by what the change can break, and a reviewer who disagrees with a label escalates rather than merges. **MOS-REL-120** A gate that is failing for an environmental reason MUST be fixed or explicitly waived in the pull request with an issue ID and a named owner; a silently disabled gate is the failure mode this matrix exists to prevent.

**MOS-REL-121** Observability is a DoD gate, never a delivery phase. Because the telemetry helpers ship in the weeks 1–2 block (MOS-REL-016), the metrics cell above is satisfiable from the first merge, and there is no release block whose contents include "add observability."

**MOS-REL-122** "Production-quality code" MUST NOT appear as a DoD criterion. It is unfalsifiable, and every cell in the matrix above exists because it replaced a sentence like it.

#### 15.9.3. What Done means for a release

**MOS-REL-123** A release is Done when: every change in it passed its class gates; the release's gate row (15.1.2) is green in one CI run against one commit; the Release Decision Record exists and names a human approver; the tag is signed; images are published by digest with SBOMs and signatures; and the `CHANGELOG.md` section for the tag is written.

**MOS-REL-124** A release MUST NOT be Done on the strength of a gate run that was assembled from several commits. Green checks from different commits do not compose.

### Acceptance criteria

Each check below is executable by a CI job or by a reviewer following a written procedure.

1. **Release contents are machine-checkable.** For each of 0.1.0, 0.2.0, 0.3.0, 0.4.0, a parser over 15.1.2 and 15.1.3 yields a non-empty contents list, a non-empty gate list, and a tier assignment for every contents item. Every gate check named resolves to a test identifier that exists in the repository. Fails if any contents item has no tier or any gate name has no test.
2. **No gate names a product.** A grep of every gate cell in 15.1.2 for the strings `Kafka`, `Triton`, `Orthanc`, `OHIF`, `Kubernetes`, `MinIO` returns zero matches. (Product names are permitted in register rows and in prose, not in gate definitions — MOS-REL-008.)
3. **The slip rule is decidable.** Given a simulated red gate check and the tier of the item it covers, a reviewer applying MOS-REL-009 selects exactly one of responses (a), (b), (c) with no further input. Fails if two responses are simultaneously applicable for any (tier, check) pair.
4. **Pre-release deployment is mechanically blocked.** Attempting to create a `Deployment` with `clinical_use_mode: clinical` referencing a `v*-rc.*` build returns an RFC 9457 problem and creates no deployment row.
5. **Week 0 gated the rest.** `docs/releases/spike-week0.md` exists, records the two viewer observations, and its commit date precedes the first commit that touches the DICOM writer, the `ResultBundle` schema or the `SeriesSelector` implementation. Fails if any of those three predates the spike record.
6. **The model-artifact step exists.** The build plan contains a work item that produces weights, a `PreprocessingSpec` and a golden fixture, and its position precedes the first commit in which a worker loads a model. Fails if a worker loads a model for which no packaged artifact with a recorded licence exists.
7. **Uncertainty ordering holds.** For each row of the table in 15.2.1, the first commit implementing the item falls within its stated block. A reviewer can falsify this from `git log` alone.
8. **Every deferred subsystem has a seam.** For each row in 15.2.9 whose seam is settled, the named interface exists in the tree and has at least two implementations (one MAY be a test fake), and the `JobQueue` conformance suite passes against every registered driver. The Inference row is exempt until OQ-10 (Chapter 16) is decided at its gate: Triton is the only shipped inference runtime, and this check MUST NOT be read as declaring the `InferenceBackend` port.
9. **The register is complete and honest.** `docs/adr/BUILD_VS_ADOPT.md` contains a row for every subsystem in 15.3.1; every row whose "genuinely added" column is non-empty names at least one incumbent and one disqualifying property; and the durable-execution row states its expiry conditions (MOS-REL-029); and the six MONAI and nnU-Net rows required verbatim by Chapter 17 `MOS-TRAIN-019` are present.
10. **No forbidden linking.** A licence scan over the dependency graph of every first-party binary returns zero GPL, LGPL, AGPL or BSL components. Any first-party directory with a non-Apache licence has its own `LICENSE` and `README.md` and contains no import of a `medicalos` domain package.
11. **Codegen has no drift.** `make verify-generated` exits zero on a clean checkout. Deleting one line from `docs/api/openapi.yaml` makes it exit non-zero. Every `*.gen.*` file and `docs/api/openapi.yaml` carries a `DO NOT EDIT` banner.
12. **Every source file carries an SPDX header.** The CI step in 15.6.2 returns an empty list. Removing the header from one file makes it fail.
13. **Rule contradictions are resolved.** For each of the previous version's twenty rules, 15.4.1 assigns exactly one disposition and, where the disposition is Kept or Amended, exactly one current requirement ID. Fails if any rule has no disposition or two conflicting current rules apply to the same subject.
14. **Idempotency is declared, not assumed.** Every operation exposed on a port or tool contract carries an explicit `idempotent` field; the retry machinery refuses to retry an operation with no declaration; a test asserts that a non-idempotent operation is not retried after a transport error.
15. **Documentation set is present and no stale generated section.** Every path in 15.7 exists; the generated sections regenerate identically; a grep of the documentation set for "clinical validation", "clinically validated" and "diagnoses" returns zero matches outside passages that explicitly disclaim them.
16. **No orphan requirement IDs.** For every `MOS-<AREA>-<NNN>` in this specification, a grep of the repository finds at least one citation in a test name, a code comment or a documentation anchor. The job prints the orphan list and fails when it is non-empty for the areas the current release claims.
17. **Sign-off regime is enforced before external contribution.** `CONTRIBUTING.md` contains a sign-off section naming DCO or CLA, and a bot check is configured. Merging a pull request from a non-copyright-holder account with the section absent or the bot unconfigured fails.
18. **No in-process plugin surface.** A grep of first-party Go and Python sources for dynamic library loading and dynamic module import of non-first-party paths (`plugin.Open`, `dlopen`, `importlib.import_module` over a configuration-supplied name) returns zero matches on platform processes.
19. **No certification-mark language before clearance.** A grep for "Validated on MedicalOS" and equivalent badge markup returns zero matches while `docs/adr/` contains no recorded trademark clearance.
20. **The DoD matrix is enforced, not advisory.** A pull request labelled C3 with no fixture-corpus case, no DICOM battery run, or no named approver is blocked by CI. Relabelling it C1 and re-running produces a CI failure from the class-consistency check that inspects the changed paths against the C3 definition.
21. **Observability is not a phase.** No release contents list and no build-plan block contains an item whose description is adding logging, metrics or tracing as such. Fails if one is introduced.
22. **Release Done is a single-commit property.** For the most recent tag, every gate check in its row resolves to a CI run whose commit SHA equals the tagged commit. Fails if two gate results come from different SHAs.

---

[← 14. Testing and Acceptance](14-testing.md) · [Index](../../MEDICALOS_SPEC.md) · [16. Open Questions →](16-open-questions.md)
