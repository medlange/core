<!-- MedicalOS Specification v0.4.0 — chapter 16 of 19. Normative.
     41 requirements. Do not edit without a requirement-ID review. -->

[← 15. Delivery Plan and Engineering Rules](15-delivery.md) · [Index](../../MEDICALOS_SPEC.md) · [17. Model Development Pipeline →](17-training-pipeline.md)

---

## 16. Open Questions

This chapter is the register of decisions MedicalOS has **not** made. It exists because the previous version of this document presented several undecided things as decided, and an implementer reading a confident sentence with no contract behind it guesses — and the guess becomes load-bearing. Everything in Chapters 1–15 is binding. Everything in this chapter is open, and the point of writing it down is that an implementer can see the seam instead of inventing one.

Each entry states the question, why it matters, what each branch costs downstream, which chapters and requirement families change with the answer, the release gate by which it must be answered, and — where one exists — a recommended default that is safe to build against in the meantime.

### 16.1. Conventions for this chapter

#### 16.1.1. Requirement ID area

Chapter 16 uses its own area code `OPEN`, allocated to this chapter by Chapter 1 `MOS-CORE-022`. It issues no `MOS-REL-*` ID; Chapter 15 owns `REL` entirely. The `OPEN` area code is deliberately distinct from the `OQ-NN` question identifiers of 16.1.2, which are a separate namespace and are not requirement IDs.

**MOS-OPEN-001** — The `OPEN` area is allocated only by this chapter. This chapter MUST NOT issue an ID in any other area, and no other chapter MUST issue an `OPEN` ID.

Within this chapter: `MOS-OPEN-002`–`MOS-OPEN-019` govern the register and document maintenance; `MOS-OPEN-021+N` binds the gate and recommended default of open question `OQ-NN` (so `OQ-01` → `MOS-OPEN-022`, `OQ-22` → `MOS-OPEN-043`).

#### 16.1.2. Question identifiers and status

Open questions carry a stable identifier `OQ-NN`. A question identifier is permanent: once answered the entry stays in the register with `status: decided` and a link to the decision record. Identifiers are never reused and entries are never deleted.

| status | meaning |
|---|---|
| `open` | no decision; the recommended default (if any) applies |
| `decided` | an ADR exists, the affected chapters have been edited, requirement IDs updated |
| `void` | the question stopped existing because the feature it concerned was cut; the ADR records the cut |

#### 16.1.3. Decision gates

Deadlines are expressed as gates, not dates. Four are the release tags of spine §14; three are event gates that can occur at any time.

| gate | definition (falsifiable) |
|---|---|
| `G-0.1.0` | the commit tagged `v0.1.0`, which MUST satisfy the acceptance criteria of Chapters 1–15 marked for 0.1.0 |
| `G-0.2.0` | the commit tagged `v0.2.0` |
| `G-0.3.0` | the commit tagged `v0.3.0` |
| `G-0.4.0` | the commit tagged `v0.4.0` |
| `G-PUBLIC` | the first push of the core repository to a publicly readable remote |
| `G-PILOT` | the first `Job` whose input `Study` contains data originating from a living patient at a partner site, in any `clinical_use_mode` |
| `G-CLINICAL` | the first `Deployment` created with `clinical_use_mode = clinical` |

**MOS-OPEN-002** — A chapter MUST NOT resolve a question listed here. A chapter MAY state the recommended default as its current behaviour, and when it does it MUST cite the question identifier in the requirement text (form: `Default per OQ-07; see Ch. 16.`).

**MOS-OPEN-003** — Every register entry MUST carry all of: `id`, `title`, `question`, `status`, `accountable`, `gate`, `branches` (≥2, each with a stated downstream consequence), `affects` (chapters and requirement families), `recommended_default` (a value or the explicit literal `none`), `raised_by`, `reversal_seam`.

**MOS-OPEN-004** — The register MUST exist in machine-readable form at `spec/open-questions.yaml`, MUST validate against `spec/schemas/open-question.schema.json`, and MUST be checked in CI against this chapter's prose so that the set of `OQ-NN` identifiers in the two is identical.

**MOS-OPEN-005** — Where a recommended default is given, the implementation MUST place the default behind the named `reversal_seam` (a single interface, config constant, or migration-free column) such that the other branch remains reachable without a redesign. The implementation MUST NOT hard-code a default across more than one component.

**MOS-OPEN-006** — A recommended default MUST NOT be cited as justification in a `ValidationReport`, in regulatory documentation, in a `PolicyDecision` rationale, or in customer-facing material. It is a build-time placeholder, not a finding.

**MOS-OPEN-007** — A release tag MUST fail its release job while any register entry whose `gate` is that release has `status: open`.

**MOS-OPEN-008** — A question MUST be answered by an Architecture Decision Record (ADR) under `docs/adr/`, created in the same change-set as the chapter edits it causes, naming a single human approver.

**MOS-OPEN-009** — An ADR is immutable once merged. A reversal MUST be a new ADR that supersedes it by number; the original file MUST NOT be edited beyond adding a `superseded_by` line to its front matter.

**MOS-OPEN-018** — The strings `TBD`, `TODO`, `to be decided` and `placeholder` MUST NOT appear in Chapters 1–15. An undecided thing is either a register entry here or it is deleted.

**MOS-OPEN-019** — Anyone MAY file a question. An entry MUST NOT be merged without an `accountable` role and a `gate`; a question with neither is a complaint, not a decision.

#### 16.1.4. Register file format

```yaml
# spec/open-questions.yaml
spec_version: "0.2.0"
questions:
  - id: OQ-07
    title: "Partitioning of the imaging store across tenants"
    status: open
    question: >-
      Is the DICOM store partitioned per tenant (one Orthanc instance per Tenant),
      or is there one store whose tenancy is enforced only by the DICOM Gateway?
    accountable: security-lead
    gate: G-0.3.0
    raised_by: ["review:Q7", "chapter:3", "chapter:8"]
    branches:
      - key: per_tenant_instance
        consequence: >-
          Hard isolation; N Orthanc containers, N backups, N upgrade paths;
          cross-tenant dataset building for the evidence plane needs an explicit copy step.
      - key: shared_store_gateway_filter
        consequence: >-
          One store, one backup; every cross-tenant guarantee depends on Gateway code
          being correct, so the imaging plane has no defence in depth.
      - key: shared_store_labelled
        consequence: >-
          Orthanc labels plus Gateway filter; two independent checks, one store,
          but the label is written by the same component that filters on it.
    affects:
      chapters: [3, 8, 12, 13]
      requirements: ["MOS-DATA-*", "MOS-SEC-*", "MOS-STORE-*", "MOS-OPS-*"]
    recommended_default: shared_store_gateway_filter
    reversal_seam: "DicomStore port; tenant→endpoint map resolved per request in the Gateway"
    decided_by: null
    adr: null
```

### 16.2. Questions the spine already closed — do not reopen

These were open in the v0.1.0 review. The spine settled them; they are listed so that the register cannot be used to relitigate them, and so a reader of the review does not go looking for them below.

| Review question | Settled by | Answer |
|---|---|---|
| Postgres or the event bus as source of truth for job state | `MOS-EXEC-002` (spine §4) | PostgreSQL is the sole source of truth; the bus is a derived transport and is never read to reconstruct state |
| When capability resolution happens and whether it is pinned | Spine §9, Ch. 6 | Resolution is a pure function of a registry snapshot; the resolved set is pinned into the `Job` row at creation and reused on every retry |
| Is a listed model a pin, a hint, or an override | Spine §9, Ch. 6 | Precedence is fixed: explicit pin on the job > tenant pin > deployment state > declared metric on the tenant's acceptance dataset > semver descending; zero candidates ⇒ `REJECTED` |
| Who writes the DICOM objects | Spine §2 (`MOS-SVC`), Ch. 4 | The platform. A service MUST NOT write DICOM |
| Whether modality/nosology belong in topic names | `MOS-EXEC-003` (spine §5) | No. Routing is data; topics are lifecycle-only plus a per-service work inbox |
| Whether UID remapping may be per-invocation | Spine §8, Ch. 3 | No. Remapping MUST be consistent and reversible within a tenant's mapping table |
| Whether cancellation of a running inference is in scope before 0.3 | Spine §4 | No. `CANCELLED` and `job.cancel` are reserved |

The residue of the de-identification question — *who holds the reversal key and what erasure means* — is not closed and appears below as `OQ-08`.

### 16.3. Register summary

| ID | Question | Accountable | Gate | Recommended default | Primary chapters |
|---|---|---|---|---|---|
| OQ-01 | What does human-in-the-loop mean — no autonomous clinical action, or no unreviewed result reaching the PACS? | clinical-lead | G-0.2.0 (structure), G-CLINICAL (policy) | store-and-notify; review recorded, nothing auto-actioned | 9, 5, 4, 10, 12 |
| OQ-02 | Who owns the operating threshold actually applied at inference, and may a tenant change it? | clinical-lead | G-0.2.0 | field on `ModelVersion`; a change mints a new version and re-runs the gate | 6, 7, 9, 12 |
| OQ-03 | What is the reference standard for `emphysema_laa`, and does a learned emphysema (or PE) model ever ship? | clinical-lead | G-0.1.0 (measurement), G-0.3.0 (model) | phantom + cross-implementation agreement; no learned model without labels; PE out of scope | 7, 9, 4 |
| OQ-04 | Which jurisdiction do we target first, and does core adopt an IEC 62304-shaped lifecycle without claiming certification? | regulatory-lead | G-PILOT | EU first; voluntary lifecycle artifacts, no certification claim | 9, 15, 7 |
| OQ-05 | Does a numeric confidence go into the DICOM SR? | clinical-lead | G-0.2.0 | no — confidence in API and provenance panel only, until calibration is measured | 4, 7, 9, 11 |
| OQ-06 | What statistical rule promotes a new `ModelVersion` over the incumbent? | evidence-lead | G-0.2.0 | absolute floor **and** paired patient-level bootstrap non-inferiority with a declared margin | 7, 6, 14 |
| OQ-07 | Is the imaging store partitioned per tenant? | security-lead | G-0.3.0 | one store, Gateway-enforced filter, `tenant_id` carried everywhere | 3, 8, 12, 13 |
| OQ-08 | Who holds the re-identification key, and what does erasure mean when results reference source SOP Instance UIDs? | security-lead | G-PILOT | site-held key, no operator read path, erasure = crypto-shredding with rows retained | 3, 8, 9, 12 |
| OQ-09 | May a sealed-mode service open any outbound connection? | security-lead | G-0.3.0 | default-deny egress, offline licence only; exceptions documented, none before G-CLINICAL | 2, 6, 8, 13 |
| OQ-10 | Is the inference runtime declared replaceable the way the PACS is? | spec-owner | G-0.3.0 | declare an `InferenceBackend` port; ship Triton as the only driver through 0.3; a converted artifact is a new `ModelVersion` | 2, 6, 7, 13 |
| OQ-11 | What is the signing trust root, and how does an air-gapped site verify offline? | security-lead | G-0.2.0 (reports), G-0.3.0 (registry) | KMS-held org key via cosign, with any transparency-log proof bundled inside the artifact | 6, 7, 8, 13 |
| OQ-12 | Which study-arrival transport is supported, and when is a study complete enough to triage? | spec-owner | G-0.2.0 (completeness), G-0.4.0 (transport) | Gateway-owned C-STORE SCP + stored-instance callback; quiet-period completeness rule | 3, 13, 14 |
| OQ-13 | Can a `Job` take more than one `Study` (prior comparison)? | product-owner | G-0.1.0 (schema shape) | scalar `study_instance_uid` plus an empty `prior_study_instance_uids` array from 0.1.0; non-empty priors refused until 0.4.0 | 5, 10, 12, 3, 4 |
| OQ-14 | Does writing a derived series into an existing study count as modifying the patient record? | regulatory-lead | G-0.1.0 | data-identity reading: new Series is permitted; gate it on a `result.publish` permission | 9, 8, 4, 10 |
| OQ-15 | Does the platform own user identity or federate to the hospital IdP? | security-lead | G-PILOT | API keys behind an `Authenticator` port now; OIDC required by G-PILOT | 8, 10, 13 |
| OQ-16 | How long is imaging data retained, and what is erasure worth after a SEG has been STOWed? | security-lead | G-PILOT | transient platform: TTL'd artifacts, tombstoned successor dataset versions, no mutation of sealed versions | 12, 8, 9, 7, 13 |
| OQ-17 | Where is report narrative produced — a tool, or a nested agent? | spec-owner | G-0.4.0 | a tool, because the content post-conditions have no home in a nested agent | 11, 2, 9, 10 |
| OQ-18 | Is an agentic feature available at all to a tenant with `external_llm_allowed = false`? | product-owner | G-0.4.0 | BYO OpenAI-compatible endpoint inside the tenant network; ship no weights | 11, 8, 13 |
| OQ-19 | DCO or CLA, and does core stay Apache-2.0? | spec-owner | G-PUBLIC | Apache-2.0 + DCO, accepting that relicensing is foreclosed | 15 |
| OQ-20 | When a release gate slips, what is cut? | spec-owner | G-0.1.0 | fixed date, variable scope, from a pre-declared cut list; acceptance criteria are never cut | 15 |
| OQ-21 | Is MedicalOS a technical index or a commercial channel for third-party services? | product-owner | G-0.4.0 | evidence service without a storefront; keep registry federation possible, build no billing primitive | 6, 9, 15 |
| OQ-22 | Does the name survive a mark check, and what is the certification mark? | product-owner | G-PUBLIC | run the check before G-PUBLIC; keep the namespace behind one constant so a rename is config, not migration | 1, 6, 15 |

### 16.4. Clinical, evidential and regulatory questions

#### 16.4.1. OQ-01 — What does human-in-the-loop actually mean here?

**Question.** Does "human-in-the-loop" mean (A) *no autonomous clinical action* — the platform may store and display a `Result`, and a human records a `ResultReview`, but nothing downstream is actioned automatically — or (B) *no unreviewed result reaches the PACS* — the derived DICOM objects are held inside MedicalOS until a qualified reader accepts them?

**Why it matters.** These are different products, not different settings. Under (A) the radiologist meets the AI output during their primary read, which is how deployed radiology AI normally works, and `COMPLETED` means "objects written". Under (B) the radiologist must open a MedicalOS surface *before* they can see the result at all, which means MedicalOS is in the reading workflow — the thing Chapter 1 says it is not — and `COMPLETED` means "objects generated and held". The review found this ambiguity fatal precisely because a builder reading the old §39 could not tell which was meant and therefore built neither.

| Branch | Downstream consequence | Reversal cost |
|---|---|---|
| A — store and notify | `Result.review_status` is informational; the DICOM store step is the commit point; `ResultReview` is an after-the-fact record; the unattended-arrival acceptance test of Ch. 14 is meaningful | low |
| B — review-gated release | Generated objects live in platform storage with a pending state and a separate publish step; `Job` terminal state no longer implies PACS presence; a reviewer UI becomes MVP-critical; per-reader turnaround SLAs appear; every retry/idempotency rule in Ch. 4 and Ch. 5 gains a second write point | high — it moves the commit point |
| C — per-`Deployment` `release_policy ∈ {auto_release, review_gated}` | Both paths exist; the publish step is always present, sometimes automatic; costs one extra state and one extra endpoint from day one | low if built now, high if retrofitted |

**Affects.** Ch. 9 `MOS-SAFE-*` (`ResultReview` states and permission), Ch. 5 `MOS-EXEC-*` (what `COMPLETED` asserts), Ch. 4 `MOS-IMG-*` (when STOW-RS happens relative to review), Ch. 10 `MOS-API-*` (a publish endpoint or none), Ch. 12 `MOS-STORE-*` (a held-objects table), Ch. 14 `MOS-TEST-*`.

**MOS-OPEN-022** — The structural fork (A/B/C) MUST be decided by `G-0.2.0`, because `ResultReview` ships in 0.2.0. Until decided, implementations MUST follow the spine §11 default: results are stored and visible, the review is recorded, and nothing is auto-actioned (branch A), built behind the branch-C seam — a single `publish_result` step that is unconditional in 0.2.0. The clinical policy attached to a given site MAY remain open until `G-CLINICAL`.

#### 16.4.2. OQ-02 — Who owns the operating threshold, and may a tenant move it?

**Question.** The spine fixes that clinical `AcceptanceCriteria` belong to the `Capability` and engineering bars to the `ModelVersion`. It does not fix where the *operating threshold applied at inference* lives, nor whether a tenant may change it without re-passing the deployment gate.

**Why it matters.** Moving a detection threshold from 0.50 to 0.35 roughly doubles recall and floods the reader; it changes sensitivity, specificity and every number in the `ValidationReport`. A sensitivity figure without its threshold is not a measurement. If the threshold is a tunable runtime setting, every published metric becomes uninterpretable and the gate of Ch. 7 gates nothing.

| Branch | Downstream consequence |
|---|---|
| Threshold is a field on `ModelVersion` | Immutable with the weights; a threshold change mints a new `ModelVersion` and re-runs the `EvaluationRun` and the gate; no per-site tuning; simplest provenance |
| Threshold is a field on the `Capability`'s `AcceptanceCriteria`, per tenant | Per-site tuning becomes possible; every tenant's effective performance differs; `resolve()` gains a threshold argument; `ValidationReport` must be tenant-scoped |
| Threshold overridable per `Deployment`, with mandatory re-derivation | Requires the ROC/PR curve to be persisted on the `EvaluationRun` so metrics can be recomputed without re-inference; a gate must run on override; the richest and the most machinery |

**Affects.** Ch. 6 `MOS-REG-*` (does the threshold participate in resolution and pinning), Ch. 7 `MOS-EVID-*` (ROC/PR persistence, threshold-stamped metrics), Ch. 9 `MOS-SAFE-*` (a threshold change is a labelling change), Ch. 12 (schema), Ch. 4 `MOS-IMG-*` (binarisation of a probability map before SEG writing).

**MOS-OPEN-023** — Decide by `G-0.2.0`. Default: the operating threshold is a required field on `ModelVersion`; every reported sensitivity/specificity MUST carry the threshold at which it was measured; a threshold change mints a new `ModelVersion`. Per-tenant override is refused in 0.2.0 and reconsidered at 0.3.0. Reversal seam: the ROC/PR curve is persisted on every `EvaluationRun` from 0.2.0 onward so branch three stays reachable without re-running inference.

#### 16.4.3. OQ-03 — What is the ground truth for emphysema, and for pulmonary embolism?

**Question.** Three linked sub-decisions. (a) `emphysema_laa` ships in 0.1.0 as a deterministic measurement, not a learned model — so what is the reference standard against which its *implementation* is verified? (b) %LAA below −950 HU is not comparable across reconstruction kernels or slice thicknesses, so how narrow must the applicability envelope be, and what does the report say when a study falls outside it? (c) Does a learned emphysema model ever ship, and is `pulmonary_embolism` in scope at all given that no CTPA data is held?

**Why it matters.** The old document published `dice: 0.91` for an emphysema model that had no labels, no data and no training step. If the successor ships a measurement whose correctness is never checked against anything, the same hole exists with better vocabulary.

| Sub-question | Branches |
|---|---|
| (a) reference standard | synthetic phantom with analytically known voxel counts; agreement with an independent open implementation on a public cohort; correlation with pulmonary function tests (requires clinical data we do not hold); reader consensus (not meaningful for a percentage) |
| (b) envelope | single named kernel class and ≤1.5 mm only, everything else `REJECTED`; wider envelope with a recorded kernel-dependence caveat in the SR; kernel normalisation (a research project) |
| (c) learned models | never, until a labelled dataset exists; a vendored third-party model under sealed mode; build after a labelling contract |

**Affects.** Ch. 7 `MOS-EVID-*` (what a `ValidationReport` for a deterministic measurement even contains — there is no held-out split), Ch. 9 `MOS-SAFE-*` (`known_limitations`, `not_validated_for`), Ch. 4 `MOS-IMG-*` (measurement computed in source geometry; the kernel constraint is a `SeriesSelector` field), Ch. 3 `MOS-DATA-*` (triage fields that carry `ConvolutionKernel`).

**MOS-OPEN-024** — (a) and (b) MUST be decided by `G-0.1.0`; (c) by `G-0.3.0`. Defaults: verify the LAA% implementation against a synthetic phantom with analytically known low-attenuation voxel counts **and** against one independent open implementation on ≥20 public chest CT cases, requiring agreement within 1.0 percentage point absolute; restrict the envelope to a single declared kernel class at ≤1.5 mm and `REJECT` outside it; ship no learned emphysema model without a labelled `DatasetVersion`; keep `pulmonary_embolism` out of every scope statement until CTPA data is contracted.

#### 16.4.4. OQ-04 — Which regulatory regime do we design the evidence for, and does core adopt a lifecycle standard it is not required to?

**Question.** MedicalOS core is infrastructure, not a medical device (spine §11). But the evidence it produces has to be acceptable to someone. Do we shape `ValidationReport` and the provenance record for EU MDR technical-documentation review, for an FDA 510(k)/De Novo submission by a service publisher, or for neither yet? And does core voluntarily adopt IEC 62304-shaped lifecycle artifacts (SOUP inventory, traceability matrix, formal change control, a risk file under ISO 14971) even though it claims no certification?

**Why it matters.** The two regimes want different documents and different framing of the same measurements, and the choice determines whether `regulatory_status` per jurisdiction is one field or a structured object. The lifecycle question is the expensive half: adopting 62304-shaped artifacts later means reconstructing a design history for work already done, which is the most common reason small manufacturers stall.

| Branch | Downstream consequence |
|---|---|
| EU first | `ValidationReport` shaped to MDR Annex II §6.1 clinical evaluation expectations; the service publisher is the manufacturer, MedicalOS is at most a distributor (see OQ-21); on-prem hospital sales fit |
| US first | Predicate-device framing; performance testing sections; a different statistical reporting convention; the pilot market is different |
| Neither yet | Evidence is engineering-grade only; `regulatory_status` is a free string; cheapest now, most expensive at `G-CLINICAL` |
| 62304-shaped core | Traceability matrix from requirement ID → test → code, which this document's ID scheme already makes cheap; SOUP list; change control as a CI gate |
| No lifecycle standard | Faster now; a notified body or hospital QA committee has nothing to read |

**Affects.** Ch. 9 `MOS-SAFE-*` (`regulatory_status` shape, manufacturer chain), Ch. 7 `MOS-EVID-*` (report structure, signature, approver), Ch. 15 `MOS-REL-0xx` (engineering rules, change control, traceability), Ch. 14 `MOS-TEST-*` (requirement-to-test mapping).

**MOS-OPEN-025** — Decide by `G-PILOT`. Default: design the evidence for EU review first; adopt requirement-ID → test traceability and a SOUP inventory from 0.2.0 because the ID scheme makes both nearly free; make no certification claim of any kind in code, documentation or marketing.

#### 16.4.5. OQ-05 — Does a numeric confidence go into the DICOM SR?

**Question.** For a finding produced by a probabilistic model, does the generated TID 1500 SR carry a numeric probability, and under which coded concept — or does the SR carry only a categorical statement with the number available elsewhere?

**Why it matters.** A number inside an SR is read by a clinician and is therefore a clinical claim. An uncalibrated softmax output presented as "92%" is a fabricated signal. Once the SR content tree includes it, removing it later changes every previously written document's comparability.

| Branch | Downstream consequence |
|---|---|
| Numeric probability in the SR | Requires a stated calibration: ECE and a reliability curve in the `ValidationReport`, and the threshold alongside; requires a coded concept and a defined value range; Ch. 11 must then forbid narrative from restating it differently |
| Categorical only | SR states presence/absence with a qualitative certainty term; safest; loses information the reader might want |
| Numeric outside the SR | Confidence appears in the API `Result`, the provenance panel and the review UI, never in the DICOM object; the SR stays conservative; two surfaces to keep consistent |

**Affects.** Ch. 4 `MOS-IMG-*` (SR content tree, coded concepts), Ch. 7 `MOS-EVID-*` (calibration becomes a required metric under branch one), Ch. 9 `MOS-SAFE-*`, Ch. 11 `MOS-AGENT-*` (narrative post-conditions must cover the number if it exists).

**MOS-OPEN-026** — Decide by `G-0.2.0`. Default: branch three — confidence is exposed through the API and provenance panel and is **not** written into the SR until calibration (ECE plus a reliability curve) has been measured and published for that `ModelVersion` on the tenant's acceptance cohort.

#### 16.4.6. OQ-06 — What statistical rule promotes a new `ModelVersion` over the incumbent?

**Question.** Chapter 7 defines `AcceptanceCriteria` on the `Capability`. What is the *test*? A fixed floor (`dice ≥ 0.85`), a paired comparison against the incumbent with a non-inferiority margin, or both? What is the margin, and what cohort size makes the comparison meaningful?

**Why it matters.** `if new.dice < old.dice: block` fails on noise roughly half the time on a small cohort and gets disabled within two weeks; a bare floor passes a model that lost every sub-6 mm nodule while gaining on large ones. The gate is the only mechanism that protects a site from a regression, and a gate people switch off is worse than none.

| Branch | Downstream consequence |
|---|---|
| Absolute floor only | Trivial to implement; blind to regressions above the floor and to subgroup collapse |
| Paired non-inferiority only | Needs per-case metrics persisted (the spine already requires this), a declared margin δ per capability, patient-level resampling; blind to a pair that is jointly bad |
| Both, plus a subgroup report | The safe combination; requires `AcceptanceCriteria` to carry `floor`, `margin`, `cohort_min_n` and a subgroup definition list; more schema |

**Affects.** Ch. 7 `MOS-EVID-*` (`AcceptanceCriteria` fields, per-case metric persistence, subgroup strata), Ch. 6 `MOS-REG-*` (the deployment gate consumes the verdict), Ch. 14 `MOS-TEST-*` (a synthetic regression must be caught by CI).

**MOS-OPEN-027** — Decide by `G-0.2.0`. Default: both — an absolute floor declared on the `Capability`'s `AcceptanceCriteria`, and a paired non-inferiority test on the same sealed cohort using a BCa bootstrap with 10 000 patient-level resamples and a per-capability margin δ. Subgroup reporting (lesion size bins, scanner manufacturer, slice thickness band) is produced and attached from 0.2.0 and is advisory; it becomes blocking at 0.3.0. The values of δ and the minimum cohort size per capability remain open inside this entry.

### 16.5. Platform, data-plane and security questions

#### 16.5.1. OQ-07 — Is the imaging store partitioned per tenant?

**Question.** One DICOM store per `Tenant`, or one store whose tenancy is enforced only by the DICOM Gateway?

**Why it matters.** Everything except the imaging plane is protected twice: `tenant_id NOT NULL` plus Postgres row-level security. The imaging plane — where the pixels actually are — would be protected once, by Gateway code. That is the worst split to have and the easiest to believe you do not have.

| Branch | Downstream consequence | Cost |
|---|---|---|
| One store per tenant | Hard isolation; N backups, N upgrades, N credentials in the Gateway; building a cross-tenant evidence dataset requires an explicit, audited copy | operational |
| One shared store, Gateway filter only | One backup, one upgrade; a single Gateway bug is a cross-tenant PHI disclosure; no defence in depth on the plane that matters most | risk |
| One shared store with per-tenant labels plus Gateway filter | Two checks, one store — but the label is written by the component that filters on it, so the independence is partial | medium |

**Affects.** Ch. 3 `MOS-DATA-*` (Gateway routing and filter), Ch. 8 `MOS-SEC-*` (isolation model and the statement made to a hospital), Ch. 12 (store-endpoint mapping), Ch. 13 `MOS-OPS-*` (backup, RPO, container topology).

**MOS-OPEN-028** — Decide by `G-0.3.0`, or immediately at `G-PILOT` if the pilot puts two distinct legal entities on one installation. Default: one shared store behind the Gateway through 0.2.0, with `tenant_id` carried on every metadata row and RLS enforced on the control plane, and a `DicomStore` port resolving tenant → endpoint per request so per-tenant instances become a configuration change rather than a redesign.

#### 16.5.2. OQ-08 — Who holds the re-identification key, and what does erasure mean?

**Question.** The spine requires UID remapping to be consistent and reversible within a tenant's mapping table. Who may read that table, where does the key live, and what happens to it — and to results that reference the source SOP Instance UIDs — when a patient exercises a right to erasure or a tenant is offboarded?

**Why it matters.** Reversibility is a requirement (results reference source UIDs) and a liability (the mapping table is the re-identification oracle). Erasure appears easy and is not: every `Result`, SEG and SR ever produced points at source instance UIDs, so deleting the mapping orphans the evidence chain, and deleting the rows breaks referential integrity of sealed `DatasetVersion` manifests.

| Branch | Downstream consequence |
|---|---|
| Site-held key, no operator read path | The platform operator cannot re-identify even under subpoena; support and incident triage get harder; key loss is unrecoverable |
| Platform-managed key with audited break-glass | Operationally comfortable; the operator becomes a processor with re-identification capability, which changes the data-processing agreement and the threat model |
| Irreversible pseudonymisation, crosswalk outside MedicalOS | Cleanest legally; the platform can no longer resolve a result back to a patient, so the clinical read path must carry the mapping itself |

**Affects.** Ch. 3 `MOS-DATA-*` (de-identification profile and mapping table), Ch. 8 `MOS-SEC-*` (key management, break-glass, DPA), Ch. 9 `MOS-SAFE-*` (erasure and the evidence chain), Ch. 12 (mapping table schema, tombstones), Ch. 7 `MOS-EVID-*` (a sealed `DatasetVersion` cannot drop a patient without changing its hash).

**MOS-OPEN-029** — Decide by `G-PILOT`. Default: the mapping table lives in the tenant's own database, encrypted under a per-tenant key held in the site's KMS with no platform-operator read path; erasure is implemented as destruction of the per-patient key (crypto-shredding) with the mapping rows retained as opaque tokens so prior results stay referentially intact; a sealed `DatasetVersion` is never mutated — erasure produces a successor version plus a tombstone record.

#### 16.5.3. OQ-09 — May a sealed-mode service open an outbound connection?

**Question.** Sealed-mode services run vendor code next to PHI. May that container make any outbound network connection — licence check, telemetry, model download — and if so, under what control?

**Why it matters.** This is the single security decision that determines whether sealed mode is sellable to a hospital. Default-deny egress excludes every vendor whose runtime phones home to a licence server, which is a large fraction of proprietary medical AI. Allowing egress puts an unaudited channel next to patient pixels.

| Branch | Downstream consequence |
|---|---|
| Hard default-deny, offline licence file only | Container runs with no network namespace except the platform's own IPC; some vendors cannot ship; the security claim is simple and true |
| Allowlisted egress per destination, brokered and audited | Needs an egress broker, a destination allowlist in `service.yaml`, TLS policy, and audit of every byte category; expands the security chapter substantially |
| Deny by default with a documented exception process | Exception requires a signed vendor attestation and a data-flow declaration in the manifest; keeps the default honest and the door findable |

**Affects.** Ch. 2 `MOS-SVC-*` (a `network_egress` declaration in `service.yaml`), Ch. 6 `MOS-REG-*` (admission refuses a manifest whose declaration exceeds policy), Ch. 8 `MOS-SEC-*` (network policy, egress broker), Ch. 13 `MOS-OPS-*` (NetworkPolicy / `--network=none` in the compose and chart), Ch. 9 (PHI risk file).

**MOS-OPEN-030** — Decide by `G-0.3.0`, when sealed mode ships. Default: hard default-deny egress with an offline licence file; the exception process of branch three is documented but no exception is granted before `G-CLINICAL`; the manifest carries `network_egress: none` as a required field from 0.3.0 so the alternative is expressible.

#### 16.5.4. OQ-10 — Is the inference runtime declared replaceable the way the PACS is?

**Question.** The spine names Triton as the runtime for native-mode services. Do we publish a replaceability contract for it — an `InferenceBackend` port — as we do for the PACS, and is a backend-converted artifact (a TensorRT plan) a separate `ModelVersion`?

**Why it matters.** The old document had a replaceability clause for the PACS and none for the inference plane while claiming vendor neutrality, and pinned `min_triton: "2.x"` — a version constraint that is meaningless for a serialized TensorRT plan, which is bound to a GPU architecture and a TRT build. Separately, conversion changes numerics: the artifact you validated is not the artifact you serve unless you say otherwise.

| Branch | Downstream consequence |
|---|---|
| Triton mandated | Simplest; a hospital with a different accelerator or a CPU-only site cannot be served; contradicts the neutrality claim of Ch. 1 |
| `InferenceBackend` port, Triton as driver 1 | One interface (`infer(volume, model_id, model_version) → tensors`); a small in-process driver is possible for development and CPU sites; the compatibility matrix moves from platform level to driver level |
| Port plus a mandatory second driver in 0.3 | Proves the abstraction but costs a second implementation and a second evaluation surface |

**Affects.** Ch. 2 `MOS-SVC-*` (`backend` discriminator, accelerator constraints), Ch. 6 `MOS-REG-*` (the compatibility matrix belongs to the driver, not the platform), Ch. 7 `MOS-EVID-*` (whether a conversion requires its own `EvaluationRun`), Ch. 13 `MOS-OPS-*`.

**MOS-OPEN-031** — Decide by `G-0.3.0`. Default: declare the `InferenceBackend` port and keep Triton as the only shipped driver through 0.3.0; record `backend`, GPU architecture and runtime version on every `ModelVersion`; a backend conversion mints a new `ModelVersion` and requires its own `EvaluationRun`.

#### 16.5.5. OQ-11 — What signs an artifact, and how does an air-gapped site verify it offline?

**Question.** `ServiceVersion`, `PreprocessingSpec` and `ValidationReport` are all required to be signed, and the report is required to be offline-verifiable. What is the trust root — keyless transparency-log signing, an organisational key in a KMS, or both — and what does a hospital with no outbound internet actually run to verify?

**Why it matters.** Keyless signing gives the best supply-chain story and the worst air-gap story, because verification wants a log. Offline verifiability is a hard requirement of the evidence plane, so the choice is constrained but not made, and the verification command is something a hospital IT department will ask for by name.

| Branch | Downstream consequence |
|---|---|
| Keyless (Fulcio/Rekor) | Excellent provenance; identity-based; offline verification requires a mirrored trust bundle and log snapshot shipped with the artifact |
| Organisational key in a KMS, published root | Trivially offline-verifiable; key rotation and revocation become our problem; no identity binding beyond the key |
| Both: KMS key with a transparency-log inclusion proof bundled into the artifact | Offline verification never needs the network; the log adds public auditability when reachable; two things to maintain |

**Affects.** Ch. 6 `MOS-REG-*` (registry admission refuses an unsigned or unverifiable artifact), Ch. 7 `MOS-EVID-*` (the report's signature envelope and the published verification procedure), Ch. 8 `MOS-SEC-*` (key custody, rotation, revocation), Ch. 13 `MOS-OPS-*` (air-gapped install), Ch. 15 (CI signing step).

**MOS-OPEN-032** — Decide for `ValidationReport` by `G-0.2.0` and for registry artifacts by `G-0.3.0`. Default: branch three — sign with an organisational key held in a KMS, embed any transparency-log inclusion proof inside the artifact, and publish a single verification command that requires no network access.

#### 16.5.6. OQ-12 — How does a study arrive, and when is it complete enough to triage?

**Question.** Two bound decisions. Which arrival transport does the platform support — a store-callback from the PACS, a C-STORE SCP owned by the DICOM Gateway, a DICOMweb/UPS-RS subscription, or an HL7/RIS broker message? And, whichever it is, what rule decides that a study has finished arriving, so that triage sees the whole series inventory rather than the first forty slices?

**Why it matters.** Unattended, arrival-triggered operation is the deployment shape hospitals buy, and it is the 0.4.0 acceptance test. But the completeness rule is upstream of triage, which is 0.1.0 work: triage evaluates `SeriesSelector` against the series inventory, and a partial inventory produces a wrong selection, a `REJECTED` job on a study that was in fact eligible, or an analysis of half a volume.

| Branch | Downstream consequence |
|---|---|
| Store-callback from the PACS | Days of work against Orthanc; ties the platform to a store that supports callbacks; no negotiation control |
| Gateway-owned C-STORE SCP | The Gateway becomes a DICOM node with an AE title the hospital configures — the most familiar integration shape for hospital IT; adds DIMSE to a DICOMweb-only component |
| DICOMweb / UPS-RS subscription | Standards-clean; thin support in deployed PACS today |
| RIS/HL7 broker message | Carries the order context and the expected procedure; requires an HL7 interface engine on both sides |
| Completeness: quiet period | "No new instance for N seconds" — simple, always works, adds N seconds of latency and can fire mid-transfer on a slow link |
| Completeness: expected instance count | From MPPS or the modality worklist when available — exact, unavailable at many sites |

**Affects.** Ch. 3 `MOS-DATA-*` (arrival entry points, triage trigger, study-stability rule), Ch. 13 `MOS-OPS-*` (a new listener, its port and its failure modes), Ch. 14 `MOS-TEST-*` (the unattended acceptance test), Ch. 5 `MOS-EXEC-*` (jobs created by an event rather than a request).

**MOS-OPEN-033** — The completeness rule MUST be decided by `G-0.2.0`; the transport by `G-0.4.0`. Defaults: completeness is a quiet period with a per-tenant configurable value defaulting to 120 s, upgraded to an exact check when an expected instance count is available; the transport is a Gateway-owned C-STORE SCP with the store-callback path retained for development installations; UPS-RS and HL7 are deferred.

#### 16.5.7. OQ-13 — Can a `Job` take more than one `Study`?

**Question.** Prior comparison (nodule growth, effusion change) is core chest-CT work. Does the `Job` input accept a set of studies from 0.1.0, or one?

**Why it matters.** Widening a scalar `study_instance_uid` to a list later is a breaking change to the API, the event envelope, the database and every SDK. But actually supporting priors requires a prior-selection rule (which prior? matched how?) and inter-study registration, which is a substantial geometry problem that does not belong in 0.1.0.

| Branch | Downstream consequence |
|---|---|
| Scalar now, widen later | Cheapest today; a breaking API change and a migration later, at exactly the moment the product gets interesting |
| Scalar primary plus a prior array from 0.1.0, the array constrained to empty | The shape is right forever; the constraint is one validation rule; costs an hour |
| Full prior support in 0.2.0 | Requires prior-selection rules, registration between timepoints, and a change-measurement contract in Ch. 4 |

**Affects.** Ch. 5 `MOS-EXEC-*` (`Job` input schema), Ch. 10 `MOS-API-*` (`POST /api/v1/jobs` body), Ch. 12 (job and job-input tables), Ch. 3 `MOS-DATA-*` (prior selection at triage), Ch. 4 `MOS-IMG-*` (registration and change measurement).

**MOS-OPEN-034** — Decide the schema shape by `G-0.1.0`. Default: the wire shape is Chapter 10's (`MOS-API-045`) — `input.study_instance_uid`, a required scalar naming exactly one primary study, alongside `input.prior_study_instance_uids`, an array present and possibly empty from 0.1.0; the platform MUST refuse a request whose `prior_study_instance_uids` is non-empty until the prior-selection and registration contracts exist; full prior support is deferred to 0.4.0.

#### 16.5.8. OQ-14 — Does writing a derived series modify the patient record?

**Question.** The default-DENY clinical-action table denies "modify patient". Writing a SEG into an existing `Study` creates new SOP Instances under a new `SeriesInstanceUID` inside a patient's namespace. Is that a modification?

**Why it matters.** Two readings, and an implementer who takes the strict one gates the entire output path behind a policy exception that nobody will grant, which silently disables the product. The permissive reading, left unstated, invites someone later to argue the platform may alter existing objects.

| Branch | Downstream consequence |
|---|---|
| Data-identity reading | DENY means: never alter an existing SOP Instance, never alter patient or study identity attributes, never mint a `StudyInstanceUID`. Creating new Series is the normal output path and needs no exception |
| Write-anywhere reading | Every STOW-RS into a patient namespace requires an explicit ALLOW `PolicyDecision` with a recorded authorisation; adds a policy evaluation to the commit point of every job |

**Affects.** Ch. 9 `MOS-SAFE-*` (the default-deny table wording), Ch. 8 `MOS-SEC-*` (`PolicyDecision` at the store step), Ch. 4 `MOS-IMG-*` (whether the store step is policy-gated), Ch. 10 `MOS-API-*` (permission vocabulary).

**MOS-OPEN-035** — Decide by `G-0.1.0`, because the output path ships there. Default: the data-identity reading, written out explicitly, plus a named permission `result.publish` that the store step requires — so a site that wants the strict reading can express it by withholding one permission rather than by patching the pipeline.

#### 16.5.9. OQ-15 — Does the platform own user identity, or federate?

**Question.** API keys and platform-owned `User` rows, OIDC federation to the hospital identity provider, or both?

**Why it matters.** A per-patient PHI access audit is only worth something if it names a human. A shared service account breaks the chain at exactly the point a hospital's information governance officer will look. Conversely, requiring OIDC in 0.1.0 blocks development.

| Branch | Downstream consequence |
|---|---|
| Platform-owned users, API keys only | Fastest; the platform becomes a credential store and a password-reset surface; audit names a key, not a person |
| OIDC federation | Authorization code + PKCE, group → `Role` mapping, token lifetime and revocation handling; the hospital owns joiners/movers/leavers, which is what they want |
| Both, with API keys reserved for `ServiceAccount` | The realistic end state; two auth paths to test and to secure |

**Affects.** Ch. 8 `MOS-SEC-*` (authentication, `on_behalf_of` in every `AuditEvent`), Ch. 10 `MOS-API-*` (auth scheme in the generated OpenAPI), Ch. 13 `MOS-OPS-*` (IdP as a deployment dependency).

**MOS-OPEN-036** — Decide by `G-PILOT`. Default: API keys behind an `Authenticator` port for 0.1.0–0.2.0, with `on_behalf_of` present on every `AuditEvent` from 0.1.0 so the identity model can be tightened without a schema change; OIDC with group→`Role` mapping is required before `G-PILOT`.

#### 16.5.10. OQ-16 — What is retained, for how long, and what is erasure worth after STOW?

**Question.** How long does the platform retain canonical volumes, intermediate execution artifacts, de-identified copies and evidence datasets? And once a SEG has been written into the hospital's PACS, what does an erasure request against MedicalOS actually achieve?

**Why it matters.** Storage cost and legal exposure both scale with retention, and the honest answer to the second half — that the platform cannot recall an object it has already handed to the hospital's own system of record — has to be written down before a site asks, not after.

| Branch | Downstream consequence |
|---|---|
| Transient platform | Execution artifacts TTL'd in days, canonical volumes in weeks, no long-term image copies; every re-run re-fetches from the PACS; smallest footprint and smallest exposure |
| Working-copy retention per site agreement | Faster re-runs and better incident forensics; a retention policy per tenant, a GC job, and a much larger PHI surface |
| Evidence datasets separate | `DatasetVersion` is sealed and content-addressed, so it cannot participate in per-record deletion at all; it needs its own lawful basis and its own lifecycle |

**Affects.** Ch. 12 (retention fields, TTL, tombstones, GC), Ch. 8 `MOS-SEC-*`, Ch. 9 `MOS-SAFE-*` (erasure statement), Ch. 7 `MOS-EVID-*` (sealed-version immutability vs erasure), Ch. 13 `MOS-OPS-*` (storage sizing).

**MOS-OPEN-037** — Decide by `G-PILOT`. Default: transient platform — execution artifacts TTL 7 days, canonical volumes 30 days, both per-tenant configurable; sealed `DatasetVersion` objects are never mutated, an erasure request produces a successor version plus a tombstone; the documentation states plainly that objects already written to the hospital PACS are outside MedicalOS's deletion authority.

### 16.6. Agentic-layer questions

#### 16.6.1. OQ-17 — Where is report narrative produced?

**Question.** Is the narrative produced by a schema-validated tool with machine-checked post-conditions, or by a nested agent?

**Why it matters.** The spine puts the content-safety post-conditions — extract every numeric, laterality term and finding label from the generated text, assert set membership in the source `Result`, fail closed on an unmatched token — in "the tool that produces narrative". If the narrative is produced by a nested agent, that check has no component to live in, and `hallucination_rate` has no definition. This is the same question the review raised and it must be settled before the agentic layer is designed, not after.

| Branch | Downstream consequence |
|---|---|
| `report.generate` tool | Input and output JSON Schemas; post-conditions run inside the tool; fail-closed is a single code path; the agent orchestrates but never emits prose |
| Nested agent | Requires nested-execution semantics (depth limit, budget division, `parent_job_id`, provenance keyed by root execution); post-conditions must be re-implemented at the boundary |
| Narrative produced outside MedicalOS | The platform ships structured results only; the safety property becomes someone else's; the smallest scope and the smallest claim |

**Affects.** Ch. 11 `MOS-AGENT-*` (tool contract, post-conditions, `hallucination_rate` definition), Ch. 2 `MOS-SVC-*` (whether narrative is a `Service` at all), Ch. 9 `MOS-SAFE-*`, Ch. 10 `MOS-API-*`.

**MOS-OPEN-038** — Decide by `G-0.4.0`. Default: a tool. Nested agents are deferred beyond 0.4.0, so the fail-closed check has exactly one home.

#### 16.6.2. OQ-18 — What can a tenant with `external_llm_allowed = false` do?

**Question.** The per-tenant switch defaults to false. Is any agentic feature available to such a tenant — and if so, does MedicalOS ship or support a self-hosted model?

**Why it matters.** The default is false, so unless this is answered the agentic layer is unavailable to the default configuration, which makes it unavailable to every hospital. Shipping weights, by contrast, makes MedicalOS responsible for a model's behaviour and for its GPU budget.

| Branch | Downstream consequence |
|---|---|
| Nothing — features are simply unavailable | Honest and free; the agentic chapter describes a feature most tenants never see |
| Ship a supported local stack | A named serving runtime and named open weights; a quality delta the tenant must accept; a second GPU budget to plan; an evaluation obligation for the bundled model |
| BYO OpenAI-compatible endpoint inside the tenant network | The platform ships a provider port and no weights; the tenant's model choice is theirs; per-provider evaluation becomes a prerequisite for use |

**Affects.** Ch. 11 `MOS-AGENT-*` (provider abstraction, per-provider evaluation, cost accounting), Ch. 8 `MOS-SEC-*` (the switch and its enforcement point), Ch. 13 `MOS-OPS-*` (GPU planning).

**MOS-OPEN-039** — Decide by `G-0.4.0`. Default: branch three — an OpenAI-compatible provider port with no bundled weights, and a per-provider evaluation suite that MUST pass before that provider may be used for narrative in any tenant.

### 16.7. Project, licence and positioning questions

#### 16.7.1. OQ-19 — DCO or CLA, and does core stay Apache-2.0?

**Question.** Does the project take contributions under a Developer Certificate of Origin or a Contributor Licence Agreement, and does the core stay Apache-2.0?

**Why it matters.** It is one line in `CONTRIBUTING.md` and it cannot be added after contributors arrive without contacting every one of them. It also decides, permanently, whether the project can ever relicense or dual-licence. Note what it does *not* decide: the licence does not create the plugin boundary — Apache-2.0 imposes no copyleft, so the separation between core and proprietary services is technical (out-of-process, OCI image plus signed manifest), not legal.

| Branch | Downstream consequence |
|---|---|
| DCO | Lowest friction; no relicensing, no dual-licensing, ever; the licence is effectively frozen at the first external contribution |
| CLA / CAA | Relicensing and dual-licensing stay possible; measurable chill on drive-by contribution; an administrative process to run |
| Apache-2.0 core | Maximum adoption, hospital procurement comfort; no defence against a hosted competitor |
| AGPL-3.0 core | Defends against a hosted competitor; creates procurement friction and ambiguity for adjacent proprietary services that hospitals will ask about |

**Affects.** Ch. 15 `MOS-REL-0xx` (contribution rules, `CONTRIBUTING.md`, the repository layout that keeps services out of core).

**MOS-OPEN-040** — Decide by `G-PUBLIC`. Default: Apache-2.0 core with DCO, accepting that relicensing is foreclosed, on the reasoning that the defensible asset is the evidence plane and the certification mark rather than the core source.

#### 16.7.2. OQ-20 — When a gate slips, what is cut?

**Question.** A release gate is reached and one acceptance criterion fails. Is the date fixed and the scope cut, is the scope fixed and the date moved, or does the release split?

**Why it matters.** The previous document named three versions and stated the contents of none, so there was no mechanism that forced a scope cut and no list of what could be cut. The spine now fixes the contents of 0.1.0–0.4.0, which makes the slip policy the only remaining degree of freedom — and it must be agreed before a slip, not during one.

| Branch | Downstream consequence |
|---|---|
| Fixed date, variable scope | Requires a pre-declared, ordered cut list per release; keeps the release train credible; the cut list has to be written while nobody is under pressure |
| Fixed scope, variable date | Simplest; the train stops being a train; downstream commitments (pilot, partner) drift silently |
| Split the release | Two tags, two sets of release notes, two compatibility statements; useful once, corrosive as a habit |

**Affects.** Ch. 15 `MOS-REL-0xx` (release gates, the per-release must/may-cut table).

**MOS-OPEN-041** — Decide by `G-0.1.0`. Default: fixed date, variable scope, drawn from a per-release ordered cut list owned by Chapter 15, with one invariant — an acceptance criterion is never cut; only scope is. A feature removed by a cut moves to the next release's scope in the same change-set.

#### 16.7.3. OQ-21 — Technical index, or commercial channel?

**Question.** Is MedicalOS a technical index for third-party services — registry, resolution, evidence — or a commercial channel that brokers their sale?

**Why it matters.** This looks like a business question and is an engineering and regulatory one. Brokering the placing-on-market of a CE-marked device pulls the platform into distributor obligations (verification of conformity marking, storage and transport conditions, complaint handling, traceability, cooperation with authorities), which lands directly in Chapter 9's manufacturer chain and Chapter 6's registry (publisher identity, revocation, recall propagation). Deferring it deliberately is fine; deferring it by omission turns it into an obligation set nobody has staffed.

| Branch | Downstream consequence |
|---|---|
| Technical index only | No commerce primitives; publisher identity and revocation still required; the platform is never in the distribution chain |
| Commercial channel | Billing, vendor agreements, liability allocation, a storefront, distributor obligations, a certification mark to police |
| Evidence service for vendors, no storefront | Sells the scarce thing (portable, signed, re-verifiable evidence) without entering the distribution chain; needs registry federation to stay possible |

**Affects.** Ch. 6 `MOS-REG-*` (federation, publisher identity, revocation and recall propagation), Ch. 9 `MOS-SAFE-*` (manufacturer/distributor chain), Ch. 15 (non-goals), Ch. 1 (positioning).

**MOS-OPEN-042** — Decide by `G-0.4.0` and before any vendor contract is signed. Default: branch three — an evidence service without a storefront; the registry is designed so federation with an external index remains possible, and no billing primitive is built.

#### 16.7.4. OQ-22 — Does the name survive, and what is the certification mark?

**Question.** Does "MedicalOS" clear a trademark and market-collision check in the target jurisdictions, and is there a distinctive certification mark for a service that has passed the evidence gate?

**Why it matters.** A descriptive name cannot function as a certification mark, and the "OS" slot in radiology AI already has occupants. The engineering consequence is that the name is currently baked into topic prefixes (`medicalos.jobs.requested`, `medicalos.svc.<service_id>.work`), module paths, OCI repository paths and identifier prefixes — so a rename is either a configuration change or a migration, depending entirely on whether the indirection exists.

| Branch | Downstream consequence |
|---|---|
| Name clears | Nothing changes; the certification mark still needs to be a distinct, registrable mark, not the product name |
| Name collides | A rename touches topic names, module paths, image repositories and every identifier prefix; cheap if there is one namespace constant, expensive otherwise |

**Affects.** Ch. 1 (positioning line), Ch. 6 `MOS-REG-*` (OCI repository naming), Ch. 5 `MOS-EXEC-003` (topic prefix), Ch. 15 (repository and module layout).

**MOS-OPEN-043** — Decide by `G-PUBLIC`. Default: run the check before the repository is public; until then keep the product namespace behind a single constant used by code generation and the topic-name builder, so a rename costs a configuration change rather than a migration; print no certification claim anywhere until a mark is registered.

### 16.8. How this document is maintained

#### 16.8.1. Ownership

**MOS-OPEN-012** — The document has exactly one `spec-owner`, who is accountable for the whole and is the only role that may merge a change to the spine. Each chapter has exactly one `chapter-owner`. Each open question has exactly one `accountable` role. One person MAY hold several roles; a role MUST NOT be held by "the team".

| Role | Owns | Approves |
|---|---|---|
| `spec-owner` | the spine, chapter boundaries, requirement-ID allocation, this register | any normative change |
| `chapter-owner` | one chapter's contracts and acceptance criteria | edits within that chapter |
| `clinical-lead` | clinical claims, thresholds, review semantics, reference standards | OQ-01, OQ-02, OQ-03, OQ-05 |
| `evidence-lead` | datasets, evaluation, gates | OQ-06 |
| `security-lead` | tenancy, PHI, keys, egress, identity, retention | OQ-07 … OQ-09, OQ-11, OQ-15, OQ-16 |
| `regulatory-lead` | regulatory posture, manufacturer chain, default-deny table | OQ-04, OQ-14 |
| `product-owner` | scope, positioning, commercial structure | OQ-13, OQ-18, OQ-21, OQ-22 |

#### 16.8.2. Requirement ID lifecycle

**MOS-OPEN-013** — Requirement IDs are allocated from a per-area counter recorded in `spec/reqids.yaml`. An ID is immutable: it MUST NOT be renumbered when chapters are reordered, MUST NOT be reused after retirement, and MUST NOT change meaning. If the requirement's meaning changes materially, the old ID is retired and a new ID is allocated.

**MOS-OPEN-014** — An ID has exactly one of three states.

| State | Meaning | Rules |
|---|---|---|
| `active` | normative | appears in a chapter |
| `deprecated` | still normative, scheduled for removal | MUST carry `remove_in: <release>`; MUST stay in the chapter text with a deprecation note until that release |
| `retired` | no longer normative | MUST NOT appear in any chapter's normative text; MUST appear in the retirement ledger |

The retirement ledger lives in `spec/retired-requirements.md` and every row carries all five columns:

| Requirement ID | Retired in | Reason | Superseded by | ADR |
|---|---|---|---|---|
| `MOS-EXEC-0XX` | — | *(ledger format shown; the ledger is empty at 0.2.0 — no requirement has yet been retired)* | — | — |

(The row above shows the ledger's five-column format. The ledger is empty at 0.2.0: the removal of `POSTPROCESSING` from the public enum predates the requirement IDs and is carried as `MOS-EXEC-018`, Ch. 5, not as a retirement. `MOS-EXEC-017` is **not** retired — it is the live PHI rule on `failure.message` in Ch. 5.)

#### 16.8.3. Change classes and versioning

**MOS-OPEN-015** — The document carries a `spec_version` in its front matter using `MAJOR.MINOR.PATCH`. `spec_version` MUST equal the release train it normatively describes. Every change belongs to exactly one class and each class has a fixed effect.

| Class | Definition | Version effect | Approval |
|---|---|---|---|
| editorial | typography, ordering, examples that do not change a contract | `PATCH` | `chapter-owner` |
| clarifying | rewording that a conforming implementation cannot fail to satisfy if it satisfied the old text | `PATCH` + errata entry | `chapter-owner` + `spec-owner` |
| normative-additive | a new requirement ID, or a new optional field | next `MINOR` | `spec-owner` |
| normative-breaking | a change that makes a conforming implementation non-conforming; any ID retirement | next `MINOR` pre-1.0, next `MAJOR` from 1.0 | `spec-owner` + the accountable role |

**MOS-OPEN-016** — A cross-reference between chapters MUST cite a requirement ID. A chapter number MAY accompany it as a convenience but MUST NOT be the only reference, because chapter numbers move and IDs do not.

**MOS-OPEN-017** — Every released `spec_version` MUST be tagged in the repository, and every software release MUST declare `conforms_to_spec: <spec_version>` in its build metadata.

#### 16.8.4. Answering a question

**MOS-OPEN-010** — The register MUST be reviewed at every gate in 16.1.3 and whenever a change-set edits a chapter listed in a question's `affects`.

**MOS-OPEN-011** — A question's `gate` MAY be re-bound to a later gate at most once, and only with the written agreement of the accountable role and the `spec-owner`, recorded as a `deferred` block on the register entry with a reason. A second deferral is not available: the question is answered, or the feature it concerns is cut and the entry becomes `void`.

An ADR carries this front matter and nothing less:

```yaml
# docs/adr/0031-operating-threshold-ownership.md
adr: 31
question: OQ-02
title: "The operating threshold is a field on ModelVersion"
status: accepted          # proposed | accepted | superseded
date: 2026-11-04
approver: "clinical-lead: A. Petrova"
decision: model_version_field
rejected:
  - key: capability_acceptance_criteria
    because: "makes every published metric tenant-scoped and uninterpretable across sites"
  - key: deployment_override
    because: "reconsider at 0.3.0 once ROC curves are persisted on every EvaluationRun"
changes_requirements:
  added: ["MOS-EVID-044", "MOS-REG-021"]
  amended: ["MOS-EVID-012"]
  retired: []
supersedes: null
superseded_by: null
```

The full answering procedure, which CI enforces (see acceptance criteria 6–9):

1. Write the ADR with a named human approver and the list of requirement IDs it adds, amends or retires.
2. Edit the affected chapters in the same change-set; add, amend or retire the IDs exactly as the ADR lists.
3. Set the register entry to `status: decided` with `decided_by` and `adr` populated; do not delete the entry.
4. Remove the `Default per OQ-NN` citations from the chapters that carried them.
5. Bump `spec_version` according to the change class.

### Questions raised by chapter 17 (Model Development Pipeline)

Chapter 17 was added after the other sixteen. These are the decisions it surfaced and deliberately
did not make. They follow the same rule as the rest of this chapter: the specification records them
as open rather than letting an implementer settle them by assumption.

- OQ-23 — Is a model trained on one tenant's data permitted to serve another tenant, and on what basis? MOS-TRAIN-073's `permits_redistribution` flag makes the question expressible but does not answer it: a weight is not imaging data, but it is derived from imaging data, and a tenant that consented to training may not have consented to the artifact leaving. Candidate default: a ModelVersion derived from any tenant whose TrainingDataPolicy has permits_redistribution = false is tenant-scoped in the registry and cannot be resolved by another tenant. Owner: regulatory-lead. Due: G-PILOT.
- OQ-24 — What is the maximum defensible model-seeded fraction in a training cohort, and is 0.50 evidence or a guess? MOS-TRAIN-086 declares 0.50 for `train` and 0.25 for `tune` because a number is needed and zero is impractical, but neither is derived from any measurement. The anchoring sub-study of MOS-TRAIN-102 is the instrument that could calibrate it. Owner: evidence-lead. Due: G-0.3.0.
- OQ-25 — Do the corpus stratification bounds of MOS-TRAIN-088 (C1 ≤ 0.60, C2 ≤ 0.70, C5 ≥ 20 per cell) belong to the platform or to the Capability? They are clinical-generalisation judgements of exactly the kind MOS-EVID-075 assigns to the Capability, yet they are stated here as platform defaults. Candidate default: platform floors that a Capability may only tighten, mirroring MOS-EVID-081's tighten-only rule for threshold overrides. Owner: evidence-lead. Due: G-0.2.0.
- OQ-26 — Who is the manufacturer of a model trained by the platform operator on a site's data? MOS-SAFE-005 makes a site that substitutes weights the manufacturer, and MOS-SAFE-003 makes the ServiceVersion publisher the manufacturer, but a pipeline run by the operator on the site's corpus produces an artifact neither party clearly authored. Owner: regulatory-lead. Due: G-PILOT.
- OQ-27 — Is a pretrained MONAI Model Zoo bundle a `derived_from` parent or an opaque initialisation? MOS-TRAIN-025 requires the upstream digest in spec.derived_from, but does not settle whether the upstream bundle's own training corpus must be disclosed in the ValidationReport's cohort description, or whether an undisclosed pretraining corpus is a leakage risk against a public evaluation cohort such as LIDC-IDRI. Owner: evidence-lead. Due: G-0.3.0.
- OQ-28 — Does the de-novo control arm of MOS-TRAIN-101 (≥ 0.10) and the paired anchoring sub-study of MOS-TRAIN-102 survive contact with reader availability? Both spend scarce radiologist time on measurement rather than labels, and a campaign that cannot afford them will be tempted to declare the whole cohort `de_novo`. Owner: clinical-lead. Due: G-0.2.0.
- May one person hold all three promotion permissions — `evidence.report.issue`, `artifact.approve` and `deployment.approve_clinical` — in a tenant running `clinical_use_mode: clinical`? MOS-TRAIN-175 permits it where tenant governance allows and requires three separate audit records regardless, but it does not decide whether separation of duties should be mandatory at clinical scale, nor who sets that policy (the tenant, the platform operator, or the jurisdiction's regulatory regime). Related to OQ-01 and OQ-04.
- What happens when the declared non-inferiority margin δ is smaller than the capability's measured `seed_variance.sd`? MOS-TRAIN-127 requires the seed variance to be measured and MOS-TRAIN-128 requires it to be rendered beside δ, but Chapter 7 MOS-EVID-087 forbids tuning δ to make a candidate pass, so the specification deliberately does not say what to do when the gate provably cannot distinguish a genuine change from a re-run of the same code. The candidate answers are: refuse to gate on that metric, widen the evaluation cohort until the interval narrows below δ, or accept that the criterion is advisory. This is a live extension of OQ-06 and must not be resolved inside chapter 17.
- Should a converted `ModelVersion` inherit its source's promotion decision, or require its own? MOS-TRAIN-172 requires it to re-pass the AcceptanceCriteria and carry its own non-inferiority result, and MOS-TRAIN-173 requires acts A2 and A3 for every deployment — which means a two-GPU-generation fleet needs two approvals per retrain. Whether that is correct rigour or an approval-fatigue generator that will be routed around in practice is not settled. Related to OQ-10.
- What is the correct `rollback_window` default, and who pays for it? MOS-TRAIN-186 sets 30 days and reserves GPU residency for the outgoing incumbent across it, which on a two-capability single-GPU site can mean half the budget is held by versions serving no traffic. The alternatives — a shorter window, an `evictable` residency class for standbys, or cold rollback with a re-`VERIFYING` step — trade rollback latency against capacity, and the trade has not been priced against a real site's GPU budget.
- Does a federated cohort ever become sealable? MOS-TRAIN-197 defers federated training on the stated ground that L3 and L4 compare pixel digests across partitions and a federation cannot compute them centrally. Whether a privacy-preserving construction (a shared salt over pixel digests, or a private set intersection over dHash buckets) makes the leakage checks federable is an open technical question, and the answer determines whether multi-site training is deferred or foreclosed.

### Questions raised by the AutoML section (17.7.6)

Automated configuration search was added to chapter 17 after the rest of the document. These are the
decisions it surfaced and did not make.

- OQ candidate — Should ensemble membership become a first-class `ModelVersion` field (`spec.ensemble`, owned by Chapter 6, `MOS-REG-030`), or stay inside the MONAI Bundle's `configs/metadata.json` carried into `artifact.manifest.spec.bundle` as `MOS-TRAIN-227` currently has it? The bundle route needs no Chapter 6 schema change and is already covered by the `MOS-REG-084` signature, but it means the registry cannot query 'which versions are ensembles' or 'which versions contain this member checkpoint' without unpacking artifacts — which matters at recall time, when `MOS-REG-040` has to enumerate the impact set. Affects Ch. 6 and Ch. 17; reversal seam is the bundle metadata block, which can be promoted to a column without re-signing.
- OQ candidate — What is the maximum permitted `test_exposure_count` per `(capability_id, split_digest)` before the split must be retired and a fresh cohort sealed? `MOS-TRAIN-216` makes the count visible and un-resettable but sets no bound. Every `test`-partition read erodes the partition's independence, and with AutoML in the pipeline the count grows faster than anyone expects. Setting the number is a statistical and clinical judgement, not an engineering one; candidate branches are a hard cap (e.g. 5), a soft cap that marks the report, or a decay rule that widens the required margin as the count rises.
- OQ candidate — For capabilities with small cohorts, is selection on the `tune` partition or on cross-validation folds within `train` the required default? `MOS-TRAIN-215` permits both and requires only that the choice be recorded. Fold-based selection uses more patients and is less noisy, but it selects on data the model was fitted against and needs a fold-disjointness argument; `tune` selection is cleaner but at n≈80 carries the selection bias quantified in this section. There is probably a patient-count threshold below which one is clearly right, and this chapter does not know it.
- OQ candidate — Is neural architecture search (`DiNTS` and equivalents) permitted at all in 0.3.0–0.4.0, or refused outright until a capability has demonstrated that the auto-configured baseline is insufficient by a declared margin? `MOS-TRAIN-212` currently permits it under the full search contract while refusing it as a default. The stricter branch — refuse until a documented baseline failure exists — is cheap to adopt now and expensive to adopt later, once a team has a sweep they like.
- OQ candidate — May per-member `EvaluationRun`s of an ensemble be shown to a human at all? `MOS-TRAIN-229` permits them on `tune` for diagnosis and bars them from the approval dossier, on the reasoning that a member table beside an ensemble figure invites the approver to make a model-selection decision at the gate. The opposing reading is that an approver who cannot see that one member carries the ensemble is being denied a material fact about the artifact. Affects `MOS-TRAIN-176` item 15 and `MOS-TRAIN-229`.
- OQ candidate — Does an ensemble's N-fold residency cost make ensembles ineligible by policy for sites running a shared GPU pool, rather than merely subject to the `MOS-OPS-084` budget check? `MOS-TRAIN-231` refuses the deployment at capacity-check time, which is correct but late: the search has already run. The alternative is a per-tenant policy that caps `budget.max_ensemble_members` at deployment-fleet parity before any search is submitted. Affects Ch. 13 §13.10.3 and `MOS-TRAIN-231`.
### Acceptance criteria

Each check is executable by a CI job. `spec/` paths are repository-relative.

1. **Register parses and is complete.** `spec/open-questions.yaml` validates against `spec/schemas/open-question.schema.json`; every entry has all eleven fields of `MOS-OPEN-003`; every `id` matches `^OQ-[0-9]{2}$` and is unique; every entry has ≥2 branches, each with a non-empty `consequence`.
2. **Prose and register agree.** The set of `OQ-NN` identifiers appearing as `#### 16.x.y. OQ-NN` headings in this chapter — matched by `^#### 16\.[0-9]+\.[0-9]+\.? OQ-([0-9]{2})\b`, so the period after the section number is optional — is exactly equal to the set of `id` values in `spec/open-questions.yaml`. Any difference fails the build.
3. **Gates are legal.** Every `gate` value is one of `G-0.1.0`, `G-0.2.0`, `G-0.3.0`, `G-0.4.0`, `G-PUBLIC`, `G-PILOT`, `G-CLINICAL`. Every `accountable` value is one of the seven roles in 16.8.1.
4. **Affects resolve.** Every chapter number in `affects.chapters` is in 1–16; every requirement family in `affects.requirements` matches `^MOS-(CORE|SVC|DATA|IMG|EXEC|REG|EVID|SEC|SAFE|API|AGENT|STORE|OPS|TEST|REL|OPEN)-(\*|[0-9]{3})$` — the area codes allocated by Ch. 1 `MOS-CORE-022`.
5. **Release gate blocks on open questions** (`MOS-OPEN-007`). The release job for tag `vX.Y.Z` fails if any entry with `gate == G-X.Y.Z` has `status: open`. Verify by asserting the job fails on a fixture register containing one open entry at the tag's gate.
6. **No silent resolution** (`MOS-OPEN-002`). For every entry with `status: open` and a non-null `recommended_default`, at least one chapter in `affects.chapters` contains the literal string `OQ-NN` (the citation of `MOS-OPEN-002`). Warning at `spec_version` 0.2.x; hard failure from 0.3.0.
7. **No orphan citations.** Every `OQ-NN` string appearing anywhere in Chapters 1–15 resolves to an entry in the register whose `status` is `open`. A citation of a `decided` question fails the build — it means step 4 of the answering procedure was skipped.
8. **Decisions are backed.** Every entry with `status: decided` has an `adr` path that exists under `docs/adr/`, whose front matter has `question` equal to the entry `id`, a non-empty `approver`, and a `changes_requirements` block; every requirement ID listed in `changes_requirements.added` or `.amended` is found in Chapters 1–15; every ID in `.retired` is **not** found there.
9. **ADRs are immutable** (`MOS-OPEN-009`). For each file under `docs/adr/` present in the previous release tag, `git diff <prev-tag> HEAD -- <file>` shows changes only to the `superseded_by` line, or no changes at all.
10. **Requirement IDs are unique, allocated and never reused.** Extract every `MOS-<AREA>-<NNN>` from Chapters 1–15: no duplicates; every ID is ≤ the area counter in `spec/reqids.yaml`; no ID appears in both the active spec and the retirement ledger; no retired ID is re-allocated in `spec/reqids.yaml`.
11. **Area allocation holds** (`MOS-OPEN-001`, Ch. 1 `MOS-CORE-022`). Every requirement *defined* in Chapter 16 — the leading bolded token of its unit, per `MOS-CORE-021` — matches `^MOS-OPEN-0(0[1-9]|[1-9][0-9])$`; no `OPEN` requirement is defined outside Chapter 16; no `REL` requirement is defined inside Chapter 16.
12. **Retirement ledger is well-formed.** Every row in `spec/retired-requirements.md` has all five columns non-empty, a `Retired in` value that is a tagged release, and an `ADR` path that exists. The ledger file has no rows at 0.2.0, so the check is vacuously satisfied until the first retirement; the illustrative row in 16.8.2 is prose and is not part of the ledger file.
13. **No placeholders** (`MOS-OPEN-018`). `grep -nEi '\b(TBD|TODO|to be decided|placeholder)\b'` over Chapters 1–15 returns zero matches.
14. **Version bump discipline** (`MOS-OPEN-015`). If `git diff <prev-tag> HEAD` changes any line in Chapters 1–15 that contains a requirement ID together with MUST, MUST NOT, SHOULD, SHOULD NOT or MAY, then `spec_version` differs from the previous tag's `spec_version`; if any such line was removed, the change-set contains a matching retirement-ledger row.
15. **Deferrals are bounded** (`MOS-OPEN-011`). No register entry has more than one `deferred` block, and every `deferred` block has a non-empty `reason` and a named approver.
16. **Defaults are not laundered into evidence** (`MOS-OPEN-006`). No generated `ValidationReport` fixture and no file under `docs/regulatory/` contains the string `OQ-` or the phrase `recommended default`.

---

[← 15. Delivery Plan and Engineering Rules](15-delivery.md) · [Index](../../MEDICALOS_SPEC.md) · [17. Model Development Pipeline →](17-training-pipeline.md)
