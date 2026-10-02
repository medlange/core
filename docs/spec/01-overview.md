<!-- MedicalOS Specification v0.4.0 — chapter 1 of 19. Normative.
     59 requirements. Do not edit without a requirement-ID review. -->

[Index](../../MEDICALOS_SPEC.md) · [2. Architecture and the Medical Service Contract →](02-architecture.md)

---

## 1. Overview, Scope and Conventions

This chapter defines what MedicalOS is, who it is for, the vocabulary the rest of this document uses, and the conventions every other chapter is held to. It defines no architecture — chapter 2 owns that — and no mechanism. It is the chapter a reviewer reads to decide whether the other fifteen are internally consistent, and the chapter a CI job reads to decide whether they are.

Document version: **0.4.0**. This is a ground-up rewrite; specification version 0.1.0 is superseded in full and is not a normative source for anything.

0.3.0 was a minor increment because it reversed two non-goals, which `MOS-CORE-036` requires be done exactly this way: `MOS-CORE-038` (the platform MUST NOT build its own DICOM viewer), whose reversal was taken at release 0.4.0 and recorded in `docs/adr/BUILD_VS_ADOPT.md` but never written into the table below; and `MOS-CORE-045` (the platform MUST NOT *build* an annotation authoring tool), which is not reversed but BOUNDED -- see its row. The requirement withdrawn in consequence was `MOS-UI-009`, and §19.1.2 states what replaced it.

0.4.0 IS A MINOR INCREMENT FOR THE SAME REASON, ONE LAYER DOWN, AND IT EXISTS BECAUSE 0.3.0 DID NOT FINISH. Withdrawing `MOS-UI-009` closed the requirement the register had located and left standing every other requirement that said the same thing in different words: `MOS-UI-373`, chapter 19's own non-goal ("MedicalOS MUST NOT build a viewer and MUST NOT fork one"); the viewer half of `MOS-UI-204`; `MOS-UI-205`, which permits exactly one viewer per environment; and the `A first-party viewer or annotation tool — REFUSE` row `MOS-UI-202` requires be appended verbatim to `docs/adr/BUILD_VS_ADOPT.md`. Register entry 103 closed on `MOS-UI-009` alone and said so; entry 106 is the four that were left. All four are withdrawn at this version, and §19.1.2 and §19.4.2 state what replaces them.

THE SECOND HALF OF 0.4.0 IS THAT THE INCUMBENT IS GONE. The OHIF deployment is withdrawn: the compose stack no longer runs `ohif/app:v3.9.2`, and the browser origin serves the first-party viewer alone. That removes a product name from twenty-two requirements, and the removals are not all of one kind. Where a requirement's SUBJECT was adopting OHIF, it is withdrawn. Where its subject was a property that still holds and OHIF was merely the surface it named — that the clinician surface reaches pixels only through the Gateway, that a provenance panel renders without a second click, that MedicalOS's output objects render correctly in a viewer the platform did not write — the requirement is RE-POINTED and keeps its `MUST`. `MOS-IMG-157` is the one to read first: it is the only automated rendering-correctness check in this document, it was defined as a pinned-OHIF check, and its obligation is re-pointed at a pinned independent viewer rather than withdrawn — `MOS-IMG-157a` carries it — because a check whose renderer under test is also its oracle proves nothing.

### 1.1 Product identity and positioning

MedicalOS is a **control plane and medical data plane for third-party medical AI services**. It routes studies to services, governs what those services may do, writes standards-compliant DICOM results on their behalf, and produces portable evidence about how they perform. It is not a runtime whose product identity is owning model execution, and it is not a place where clinical claims originate.

The positioning statement, to be used verbatim wherever the product is positioned — README, website, API landing page, `ValidationReport` cover sheet, sales material:

> MedicalOS integrates, governs and evidences medical AI services. It does not diagnose, does not replace a PACS, and is not itself a medical device.

- **MOS-CORE-001** — The product name is `MedicalOS`. The platform, its documentation, its UI strings, its API field values and its generated DICOM attributes MUST NOT expand the name to "Operating System", "Medical Agent Operating System", or any variant, and MUST NOT use "OS" as a claim about the nature of the software.
- **MOS-CORE-002** — Where a document, UI surface, API response or exported artifact states the product's positioning, it MUST reproduce the positioning statement above byte-for-byte from `spec/positioning.txt`. Paraphrase MUST NOT be used.
- **MOS-CORE-003** — The unit of integration is the `Service` (chapter 2). The platform MUST NOT require a publisher to surrender model weights, training data or source code as a condition of integration, and MUST NOT define a second, weaker integration unit alongside the `Service`.
- **MOS-CORE-004** — The platform MUST NOT state a clinical performance claim in its own name. Every clinical claim carried by the system is attributed to the `legal_manufacturer` of a `ServiceVersion` (chapter 9), and the platform's role is limited to transporting, verifying and displaying that attribution.
- **MOS-CORE-005** — MedicalOS core is infrastructure and is not itself a medical device. Platform documentation, UI and marketing MUST NOT imply device status for the core, and MUST NOT imply that deploying on MedicalOS confers regulatory status on a `ServiceVersion`. Chapter 9 owns the full regulatory posture.

The three verbs in the positioning statement are the product, and each has a normative home:

| Verb | What it means concretely | Normative home |
|---|---|---|
| **integrates** | One manifest, one governance path and one DICOM output path for both `native` and `sealed` services; a new capability is added by registering a `ServiceVersion`, not by editing platform code | ch 2, ch 6 |
| **governs** | Tenancy, de-identification, policy decisions, applicability envelopes, `clinical_use_mode`, audit, and the fact that a service reaches PACS, database and broker through the platform or not at all | ch 3, ch 8, ch 9 |
| **evidences** | Sealed dataset versions, patient-level splits, named annotation consensus, reproducible evaluation runs, and a signed, offline-verifiable `ValidationReport` | ch 7 |

### 1.2 Scope

#### 1.2.1 In scope

| Function | What the platform does | Normative home |
|---|---|---|
| Study intake | Receives study-arrival notification from a PACS or DICOMweb source and builds a per-`Study` series inventory | ch 3 |
| De-identification | Applies a per-tenant DICOM PS3.15 Annex E profile with tenant-consistent, reversible UID remapping | ch 3 |
| Triage and routing | Evaluates every deployed `SeriesSelector` once per `Study`; creates one `Job` per matching `(ServiceVersion, Study, selected Series set)` | ch 3, ch 5 |
| Execution lifecycle | Owns `Job` state, queueing, leases, heartbeats, retries and failure classification | ch 5 |
| Capability resolution | Resolves a `Capability` to an ordered candidate list and pins the resolved set into the `Job` at creation | ch 6 |
| Geometry | Builds the canonical volume, defines model space and the inverse transform back to source geometry | ch 4 |
| DICOM output | Writes every DICOM SEG, SR and SC the system emits, with deterministic identity and inherited patient/study attributes | ch 4 |
| Evidence | Datasets, splits, annotation sets, evaluation runs, acceptance criteria, signed `ValidationReport`s | ch 7 |
| Security and tenancy | Tenant isolation, RBAC, `PolicyDecision`, PHI containment, audit | ch 8 |
| Clinical safety | `clinical_use_mode`, RUO marking, AI-derived marking, `ResultReview`, provenance | ch 9 |
| Public surface | REST API, SDKs, webhooks, job event stream | ch 10 |
| Persistence | Metadata, artifacts, provenance and audit storage | ch 12 |
| Operations | Health, metrics, traces, deployment topology, scaling | ch 13 |

#### 1.2.2 Scope discipline

- **MOS-CORE-006** — Every concept in this document has exactly one normative definition site, listed in the chapter map (§1.7.1). A chapter that uses a concept it does not own MUST cross-reference the owning requirement ID and MUST NOT restate, re-derive or extend the definition.
- **MOS-CORE-007** — A cross-reference MUST be written as the bare requirement ID (`MOS-EXEC-001`) or as the ID plus its chapter (`MOS-EXEC-001, ch 5`). Section-number-only references ("see §7") MUST NOT be used, because section numbers are not stable across revisions and requirement IDs are.
- **MOS-CORE-008** — A function that appears in neither the in-scope table (§1.2.1) nor the non-goal table (§1.6) is **undefined scope**. An implementer who encounters undefined scope MUST raise it as an open question in chapter 16 and MUST NOT resolve it by assumption. Silence in this document is never permission.
- **MOS-CORE-009** — The in-scope table and the non-goal table are jointly binding. Adding a row to either requires a minor version increment of this document and an entry in the change log.

### 1.3 Audiences and conformance

Three audiences read this document as a contract. Each maps to a **conformance profile**: a named subset of requirements a party claims to satisfy.

| Audience | Who they are | Conformance profile | What they own | What they are handed |
|---|---|---|---|---|
| **Service publisher** | The team — first-party or third-party — that builds and releases a medical AI `Service` | `service-publisher` | The clinical claim, the `service.yaml` manifest, the `PreprocessingSpec`, the inference, the `ResultBundle`, the vendor evidence | A stable input contract (canonical volume + selected series), a stable output contract (`ResultBundle`), and a signed `ValidationReport` they can carry to any site |
| **Deploying site** | Hospital, imaging network or research group installing MedicalOS next to an existing PACS | `deploying-site` | Tenant configuration, de-identification policy, `clinical_use_mode` per `Deployment`, site acceptance testing, continuous site monitoring, clinical governance sign-off | Portable vendor evidence they can re-verify offline, a site acceptance suite that runs in hours, and DICOM output their incumbent viewer already renders |
| **Platform operator** | The team running and/or building the MedicalOS installation itself | `platform-core` | Everything in §1.2.1 | A specification whose requirements are individually testable |

- **MOS-CORE-010** — This document defines exactly three conformance profiles: `platform-core`, `service-publisher` and `deploying-site`. New profiles MUST NOT be introduced by individual chapters.
- **MOS-CORE-011** — A conformance claim MUST name (a) the profile, (b) the document version it is claimed against, and (c) the `sha256` digest of the `spec/requirements.yaml` index for that version. A claim that names none of these is not a conformance claim.
- **MOS-CORE-012** — Every normative statement MUST name its actor as the grammatical subject, drawn from: `the platform`, `a Service`, `a ServiceVersion`, `a Service publisher`, `a deploying site`, `a platform operator`, or a named entity from §1.4. Statements with an implied or passive actor MUST NOT be written.
- **MOS-CORE-013** — A deploying site MUST NOT be required to write, patch or fork code in order to satisfy any `platform-core` requirement. Anything a site must do to run the platform is configuration, and MUST be expressible in the deployment configuration surface (ch 13).
- **MOS-CORE-014** — A `Service` publisher MUST integrate exclusively through the public API and SDK surface (ch 10). Service-specific code MUST NOT be merged into platform core; a capability a publisher needs and the public surface does not expose is a platform defect requiring a public API, not a private patch. Chapter 15 owns the CI enforcement of this rule.
- **MOS-CORE-015** — A conformance profile is satisfied only against the full chapter set listed for it in §1.7.2. A partial claim MUST enumerate the requirement IDs it excludes and the reason for each.

### 1.4 Terminology

The spellings below are the only spellings. They are used identically in prose, JSON field names, database columns, API paths, log fields, metric names and code identifiers, with the casing conventions of §1.5.5.

- **MOS-CORE-016** — The glossary spellings in §1.4.1 through §1.4.6 are normative. A synonym for a glossary term MUST NOT appear in normative text, schemas, identifiers or UI strings.
- **MOS-CORE-017** — The terms in §1.4.7 MUST NOT appear anywhere in the repository outside the §1.4.7 table itself: not in prose, identifiers, enum values, API paths, database columns, event names, metric labels, DICOM attributes or UI strings.
- **MOS-CORE-018** — A chapter that introduces a new term of art MUST add it to §1.4 in the same change. A term used in two or more chapters and absent from §1.4 is a defect.

#### 1.4.1 Imaging and DICOM

| Term | Definition | Normative home |
|---|---|---|
| `Patient` | The subject of imaging, as projected into the platform from DICOM patient-level attributes, scoped to a `Tenant` | ch 12 |
| `Study` | One DICOM study: the set of `Series` acquired in one imaging encounter, keyed internally by a platform id with `UNIQUE (tenant_id, study_instance_uid)` | ch 12 |
| `Series` | One DICOM series: a set of `Instance`s sharing acquisition and reconstruction parameters. A chest CT study routinely contains a localizer, two or more `ConvolutionKernel` reconstructions, thin and thick recons of one acquisition, coronal and sagittal reformats, MIP series and a dose-report series with no pixel data | ch 3 |
| `Instance` | One DICOM SOP Instance — a single DICOM object, identified by its SOP Instance UID | ch 12 |
| SOP Instance UID | The DICOM identity of an `Instance`; dotted-decimal, at most 64 characters | ch 4 |
| `StudyInstanceUID`, `SeriesInstanceUID`, `FrameOfReferenceUID` | DICOM UIDs for study identity, series identity and the spatial reference frame a series is expressed in | ch 4 |
| DICOM SEG | DICOM Segmentation object: the standard carrier for a label map | ch 4 |
| DICOM SR | DICOM Structured Report; MedicalOS uses template TID 1500 (Measurement Report) with coded concepts and UCUM units | ch 4 |
| DICOM SC | DICOM Secondary Capture: a rendered key image, overlay or heat map | ch 4 |
| QIDO-RS / WADO-RS / STOW-RS | The DICOMweb query, retrieve and store services | ch 3, ch 10 |
| LPS | The coordinate convention of the canonical volume: +x to the patient's Left, +y to the Posterior, +z to the Superior, in millimetres | ch 4 |
| highdicom | The mandated Python library for writing SEG, SR and SC objects | ch 4 |
| TID 1500 | The DICOM SR template used for every measurement report MedicalOS writes | ch 4 |
| UCUM | Unified Code for Units of Measure: the only unit vocabulary used for a reported measurement (`mm`, `mL`, `%`, `[hnsf'U]`) | ch 4 |

#### 1.4.2 Integration entities

| Term | Definition | Normative home |
|---|---|---|
| `Service` | The unit of integration: stable identity, owner, and the set of `Capability` values it claims. A `Service` is not a model and not a container | ch 2 |
| `ServiceVersion` | An immutable, signed release of a `Service`: an OCI image digest plus a `service.yaml` manifest. Everything the platform pins, resolves, deploys and evidences is a `ServiceVersion` | ch 2 |
| `native` mode | Execution mode in which the platform loads the declared model onto its shared inference server from the platform artifact store. Intended for first-party and open models | ch 2 |
| `sealed` mode | Execution mode in which the service container runs inference with its own runtime and its own weights, which never leave the vendor's control. Intended for third-party proprietary services. Governance, provenance and DICOM handling are identical to `native` | ch 2 |
| `Capability` | A clinical function claimed as an output, named as a stable lowercase identifier: `pleural_effusion`, `lung_nodule`, `emphysema_laa`. A `Capability` is nosology, not modality and not a model | ch 6 |
| `SeriesSelector` | The declarative input-constraint expression on a `ServiceVersion` — modality, `ImageType` include/exclude, `BodyPartExamined`, `ConvolutionKernel` class, slice-thickness range, spacing uniformity tolerance, minimum instance count, contrast phase, orientation tolerance — that the platform evaluates during triage to decide which `Series` of a `Study`, if any, are eligible | ch 3 |
| triage | The single platform-side pass over an arriving `Study` that builds the series inventory and evaluates every deployed `SeriesSelector`. Services never self-select and never broadcast-filter | ch 3 |
| `Model`, `ModelVersion` | A concrete set of weights and its immutable release. Engineering bars — p95 latency, GPU ceiling, output-schema validity — belong to the `ModelVersion` | ch 6 |
| `PreprocessingSpec` | A versioned artifact co-located with the weights and covered by the same signature, declaring target spacing, interpolation order separately for image and label, orientation target, HU clip window, normalisation scheme and the source of its statistics, foreground crop rule, patch size, sliding-window overlap, blending mode, TTA axes and the inverse transform | ch 4 |
| `Deployment` | The binding of a `ServiceVersion` to a `Tenant` and an environment, carrying deployment state and `clinical_use_mode` | ch 6 |
| `Artifact` | The umbrella storage entity backing `ServiceVersion`, `ModelVersion`, `DatasetVersion` and the deferred workflow versions: a kind discriminator plus a JSON-Schema'd manifest per kind | ch 12 |
| applicability envelope | The acquisition-parameter region a `ServiceVersion` declares itself valid for, derived from its validation cohort. A study outside it is `REJECTED`, not silently processed | ch 6, ch 9 |
| `legal_manufacturer` | The required identity on a `ServiceVersion` naming the legal entity that makes its clinical claim; also the source of the DICOM Enhanced General Equipment Type 1 attributes on every generated object | ch 9 |

#### 1.4.3 Geometry and results

| Term | Definition | Normative home |
|---|---|---|
| canonical volume | The single 3-D array plus spatial metadata the platform derives from a selected `Series` set: LPS orientation, slices sorted by `ImagePositionPatient` projected on the slice normal, rescale applied, direction cosines honoured, source spacing preserved, gantry tilt rejected or corrected | ch 4 |
| source geometry | The grid of the source `Series` — `PixelSpacing`, `SliceThickness`, `ImageOrientationPatient`, `FrameOfReferenceUID`. Every reported measurement is computed here | ch 4 |
| model space | The resampled and normalised grid a `ModelVersion` consumes, declared by its `PreprocessingSpec`, together with the named inverse transform back to the source grid | ch 4 |
| `ResultBundle` | The return contract from a `Service` to the platform: typed coded findings; measurements as value + UCUM unit + coded concept; label maps as NIfTI or NRRD in canonical volume geometry; and per-finding references to the source SOP Instance UIDs. The platform converts a `ResultBundle` to DICOM; a service never writes DICOM | ch 2 |
| `Result` | The platform-side persisted record derived from one `ResultBundle`: the findings and measurements, the DICOM objects written, and the provenance record | ch 12 |
| `ResultReview` | The recorded human review of a `Result` — reviewer, role, action, rationale, timestamp. Results are stored and visible; review is recorded; nothing is auto-actioned | ch 9 |
| `emphysema_laa` | A deterministic measurement capability, not a learned model: the percentage of lung-mask voxels below −950 HU, computed on a declared grid with a declared component-filtering rule | ch 4, ch 7 |
| provenance | The record that makes a `Result` reproducible and investigable: input series and instance UIDs, input pixel digest, preprocessing version, model and service versions, artifact and image digests, accelerator identity, operating threshold, and the storage location and UID of every generated object | ch 9 |

#### 1.4.4 Execution

| Term | Definition | Normative home |
|---|---|---|
| `Job` | The unit of execution: one `(ServiceVersion, Study, selected Series set)` tuple, created before dispatch | ch 5 |
| `JobStep` | One persisted step of a `Job`, the source of `steps_completed` and `steps_total` | ch 5 |
| `JobEvent` | A persisted lifecycle event for a `Job`, and the source of the public event stream | ch 5 |
| `phase` | An open string field reporting internal progress (for example `retrieving`, `inferring`, `writing_dicom`). Internal stages never appear in the public state enum | ch 5 |
| `REJECTED` | A **clinical** terminal outcome, not an error: no eligible series, study outside the applicability envelope, unsupported geometry. Carries a machine-readable reason and is visually distinct from `FAILED` in every UI | ch 5 |
| `FAILED` | A **technical** terminal outcome: the platform or the service could not complete the work | ch 5 |
| `JobQueue` | The dispatch port with operations `Enqueue`, `Claim(lease)`, `Heartbeat`, `Complete`, `Fail`, `Cancel` | ch 5 |
| work inbox | The per-service topic created at service registration, named `medicalos.svc.<service_id>.work` | ch 5 |
| `idempotency_key` | The **platform-derived** key (MOS-EXEC-053), computed from the resolved job content once resolution is pinned. Never client-supplied (MOS-EXEC-054). It is the seed for deterministic DICOM identity (MOS-IMG-062) | ch 5 |
| `request_idempotency_key` | The client's HTTP `Idempotency-Key` header, echoed onto the `Job` for audit and request replay only. It MUST NOT reach UID derivation (MOS-EXEC-054, MOS-API-029) | ch 10 |

#### 1.4.5 Evidence

| Term | Definition | Normative home |
|---|---|---|
| `Dataset`, `DatasetVersion` | A named collection of cases and its sealed release: a content-addressed manifest of SOP Instance UIDs plus hash, case count, source, licence, de-identification status and parent version. Sealed after creation | ch 7 |
| `DatasetSplit` | A patient-level — never study-level — frozen manifest assigning cases to train/tune/test. A seed is not a split | ch 7 |
| `AnnotationSet` | Ground truth for a `DatasetVersion`, naming the reader or readers and the consensus rule: single reader, ≥2 readers, union, or STAPLE | ch 7 |
| `EvaluationRun` | One binding of `ModelVersion` × `DatasetVersion` × split × `AnnotationSet` × `PreprocessingSpec` version × code commit × image digest, with per-case metrics persisted, not only aggregates | ch 7 |
| `AcceptanceCriteria` | The clinical bar, owned by the `Capability`: "sensitivity ≥ 0.90 on cohort X at operating threshold T". Engineering bars belong to the `ModelVersion` | ch 6, ch 7 |
| `ValidationReport` | The evidence deliverable: versioned, signed, exportable and offline-verifiable, referencing a content-addressed `DatasetVersion`, pinned model and preprocessing versions, a reproducible run id, declared thresholds and a named human approver | ch 7 |
| **vendor evidence** | Evidence produced once by the publisher, portable, signed, and verifiable offline by any receiving site. Not regenerated per site | ch 7 |
| **site acceptance testing** | The hours-long check a deploying site runs before going live: a fixed smoke suite plus a local cohort check. Not clinical validation | ch 7 |
| **continuous site monitoring** | Ongoing post-deployment observation at a site: drift against the applicability envelope, reviewer disagreement rate, rejection rate | ch 7, ch 13 |

#### 1.4.6 Governance, security and deferred layer

| Term | Definition | Normative home |
|---|---|---|
| `Tenant` | The isolation boundary. Every tenant-owned row carries `tenant_id`; a request without tenant context is refused | ch 8 |
| `User`, `Role`, `Permission`, `ApiKey`, `ServiceAccount` | The identity and authorisation entities | ch 8 |
| DICOM Gateway | The DICOMweb proxy that holds the **only** PACS credential. All PACS access — by the viewer, by services, by workers — passes through it, which is where tenancy filtering, de-identification and per-patient access audit happen | ch 3, ch 8 |
| de-identification | Application of a per-tenant DICOM PS3.15 Annex E profile, including burned-in pixel PHI detection and private tag handling, with UID remapping that is **consistent and reversible within a tenant's mapping table** | ch 3 |
| PHI | Protected Health Information. MUST NOT appear in structured logs, span attributes, event bus payloads, metric labels or LLM prompts | ch 8 |
| `external_llm_allowed` | Per-tenant hard switch gating any egress of tenant-derived text to an external LLM provider. Defaults to `false` | ch 8, ch 11 |
| `clinical_use_mode` | Per-`Deployment` enum, `research_only` or `clinical`. Under `research_only` every generated DICOM object is marked as research and does not enter the clinical read path | ch 9 |
| RUO | Research Use Only: the observable marking applied to DICOM objects generated under `clinical_use_mode: research_only` — series description prefix, SR document title concept, `ConversionType` on SC, and a burned-in banner on SC frames | ch 9 |
| `AuditEvent` | The append-only record of who did what to which resource, joinable to the execution that caused it | ch 8 |
| `PolicyDecision` | The recorded ALLOW/DENY outcome of a policy evaluation, with the attributes it was evaluated against | ch 8 |
| `Tool`, `ToolVersion`, `Workflow`, `WorkflowVersion`, `Agent`, `AgentVersion` | The deferred agentic layer (0.4+). Reserved nouns; they are not the unit of integration and are not in the MVP | ch 11 |

#### 1.4.7 Terms deliberately not used

<!-- specheck:allow-banned -->

| Banned term or form | Use instead | Why |
|---|---|---|
| "Medical Agent Operating System", "MedicalOS Operating System", "AgentOS" | MedicalOS | MOS-CORE-001 |
| `Agent` as the thing a vendor ships and the platform integrates | `Service` / `ServiceVersion` | The unit of integration is a service, not a reasoning entity (ch 2) |
| `POST /api/v1/ai/jobs`, `POST /api/v1/agents/{id}/execute` | `POST /api/v1/jobs` | One job-creation endpoint (ch 10) |
| `"progress": 0.65` | `phase` + `steps_completed` / `steps_total` | A status-to-number lookup shown to a clinician is a fabricated signal (ch 5) |
| `POSTPROCESSING`, `WAITING` as `Job` status values | `phase` string | Internal stages MUST NOT leak into the public enum (MOS-EXEC-001) |
| `medicalos.ct.requested` and any other modality or nosology topic | lifecycle topics + per-service work inbox | Routing is data in `SeriesSelector` and `Capability`, not topology (ch 5) |
| "MedicalOS performs clinical validation", "Clinical validation" as an install step | vendor evidence / site acceptance testing / continuous site monitoring | The platform performs none of these and confers no clinical status (MOS-CORE-052) |
| `validation_status` as a free-form field | `ValidationReport` + `AcceptanceCriteria` evaluation | A status with no artifact gates nothing (ch 7) |
| `metrics: {dice: 0.91}` inline on a model record | foreign key into `EvaluationRun` | A metric without a cohort, an n and a threshold is not a measurement (ch 7) |
| "the system diagnoses", "AI diagnosis" | "AI-derived finding", "AI-derived measurement" | MOS-CORE-004, MOS-CORE-005 |
| "preprocessing step" as a pipeline stage owned by a worker | `PreprocessingSpec` artifact version | Preprocessing belongs to the model, not the pipeline (ch 4) |
| `study_id` as a raw DICOM Study Instance UID used as a primary key | internal id with `UNIQUE (tenant_id, study_instance_uid)` | Two tenants ingesting the same public collection collide (ch 12) |

<!-- /specheck:allow-banned -->

### 1.5 Conventions

#### 1.5.1 Normative keywords

- **MOS-CORE-019** — The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be interpreted as described in RFC 2119 and RFC 8174, and carry that meaning only when written in uppercase. Lowercase "must", "should" and "may" are non-normative prose and MUST NOT be used to state a requirement.

| Keyword | Meaning in this document |
|---|---|
| MUST / MUST NOT | An absolute requirement. A conformance claim is false if it is violated. A CI check exists or is owed. |
| SHOULD / SHOULD NOT | A requirement that MAY be deviated from only with a recorded, reviewed justification referencing the requirement ID. |
| MAY | A genuinely optional behaviour. An implementation that omits it is fully conformant, and an implementation that includes it MUST still satisfy every MUST that applies. |

#### 1.5.2 Requirement identifiers

- **MOS-CORE-020** — Every normative statement MUST carry exactly one stable requirement ID of the form `MOS-<AREA>-<NNN>`, matching `^MOS-(CORE|SVC|DATA|IMG|EXEC|REG|EVID|SEC|SAFE|API|AGENT|OPS|TEST|REL|STORE|TRAIN|CONF|UI|OPEN)-\d{3}$`.
- **MOS-CORE-021** — A requirement is **defined** at the unit — list item, table row, or paragraph — whose leading bolded token is its ID. Every other occurrence of that ID is a cross-reference. A unit containing a normative keyword MUST contain at least one requirement ID.
- **MOS-CORE-022** — Area codes and numeric ranges are allocated per the table below and are binding. Each area code belongs to exactly one chapter and each chapter issues IDs in exactly one area, so an ID's area alone identifies its owner. A chapter MUST NOT issue an ID outside its allocated area and range, and MUST NOT renumber another chapter's IDs.

| Area | Subject | Owning chapter | Allocated range |
|---|---|---|---|
| `CORE` | Positioning, scope, conventions, glossary, non-goals | 1 | 001–199 |
| `SVC` | Architecture and the Medical Service contract | 2 | 001–199 |
| `DATA` | Gateway, de-identification, triage | 3 | 001–099 |
| `IMG` | Geometry, preprocessing, DICOM output | 4 | 001–199 |
| `EXEC` | Jobs, queue, failure handling | 5 | 001–199 |
| `REG` | Registries, capability resolution, deployment | 6 | 001–199 |
| `EVID` | Datasets, evaluation, validation reports | 7 | 001–199 |
| `SEC` | Security, tenancy, PHI | 8 | 001–199 |
| `SAFE` | Clinical safety, regulatory posture, provenance | 9 | 001–199 |
| `API` | API and SDKs | 10 | 001–199 |
| `AGENT` | Agentic layer, chat, LLM integration | 11 | 001–199 |
| `STORE` | Persistent data model and storage | 12 | 200–399 |
| `OPS` | Observability, deployment, scaling | 13 | 001–199 |
| `TEST` | Testing and acceptance | 14 | 001–199 |
| `REL` | Delivery plan and engineering rules | 15 | 001–199 |
| `TRAIN` | Model development pipeline | 17 | 001–399 |
| `CONF` | Standards and regulatory conformance | 18 | 001–499 |
| `UI` | Operator surfaces | 19 | 001–399 |
| `OPEN` | Open questions | 16 | 001–099 |

- **MOS-CORE-023** — Requirement IDs are immutable and MUST NOT be reused. A withdrawn requirement keeps its entry in the index with `status: withdrawn` and a `superseded_by` value; its number MUST NOT be reassigned.
- **MOS-CORE-024** — `spec/requirements.yaml` is generated from the specification text and MUST NOT be hand-edited. CI MUST fail if the generated index differs from the committed one.

```yaml
# spec/requirements.yaml — generated by `python medos/tools/specheck.py --write`.
# Do not hand-edit. MOS-CORE-024.
schema_version: 1
document_version: "0.2.0"
generated_from: docs/MEDICALOS_SPEC.md
source_sha256: "sha256:3b1f0c7a9d4e5628f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f607"
requirements:
  - id: MOS-CORE-001
    chapter: 1
    section: "1.1"
    actor: platform-core
    level: MUST NOT
    status: active
    text: >-
      The product name is MedicalOS. The platform, its documentation, its UI strings,
      its API field values and its generated DICOM attributes MUST NOT expand the name
      to "Operating System", "Medical Agent Operating System", or any variant, and MUST
      NOT use "OS" as a claim about the nature of the software.
    superseded_by: null
  - id: MOS-CORE-013
    chapter: 1
    section: "1.3"
    actor: platform-core
    level: MUST NOT
    status: active
    text: >-
      A deploying site MUST NOT be required to write, patch or fork code in order to
      satisfy any platform-core requirement.
    superseded_by: null
  - id: MOS-CORE-040
    chapter: 1
    section: "1.6"
    actor: platform-core
    level: MUST NOT
    status: active
    text: >-
      The platform MUST NOT implement an HL7 FHIR client, server or resource mapping in
      releases 0.1.0 through 0.4.0.
    superseded_by: null
```

#### 1.5.3 RESERVED, DEFERRED and NON-GOAL

These three words have distinct, non-interchangeable meanings. The previous specification lost scope control by treating omission as deferral; this document does not.

- **MOS-CORE-025** — **RESERVED** means a name is allocated in a schema, enum or permission set so that adding it later is not a breaking change, and that the platform MUST NOT implement the behaviour before the named release. A RESERVED name MUST be rejected at the API boundary with an explicit "not implemented in this release" error, never silently accepted.
- **MOS-CORE-026** — **DEFERRED** means the platform will implement the function, in a named release from chapter 15. A deferral MUST name the release. A function described as deferred without a named release is undefined scope under MOS-CORE-008.
- **MOS-CORE-027** — **NON-GOAL** means the platform will not implement the function. Every non-goal MUST carry a one-line justification and a named alternative the site or publisher uses instead. A non-goal without both is not a non-goal.

#### 1.5.4 Data and formatting conventions

- **MOS-CORE-028** — Every value the platform serialises MUST follow the table below. A chapter MUST NOT introduce a second representation for a concern the table already covers.

| Concern | Convention | Example |
|---|---|---|
| Timestamps | RFC 3339, UTC, `Z` suffix, millisecond precision | `2026-09-13T12:00:00.000Z` |
| Deadlines | Absolute instants, never durations — only an absolute deadline survives a queue hop | `"deadline_at": "2026-09-13T12:30:00.000Z"` |
| Durations and timeouts | Integer with an explicit unit suffix in the field name | `"timeout_ms": 1800000` |
| Platform identifiers | `<type>_<26-character Crockford base32 ULID>` | `job_01K4Z3M7Q8R9S0T1U2V3W4X5Y6` |
| Tenant-scoped natural keys | Internal id plus a uniqueness constraint on `(tenant_id, natural_key)` | `UNIQUE (tenant_id, study_instance_uid)` |
| Minted DICOM UIDs | Dotted decimal, ≤64 characters, under the organisation OID root or the `2.25.<uuid-as-integer>` form | `2.25.329800735698586629295641978511506172918` |
| `Capability` id | `^[a-z][a-z0-9_]{2,62}$` | `pleural_effusion` |
| `Service` id | `^[a-z0-9][a-z0-9-]{1,31}\.[a-z0-9][a-z0-9-]{1,62}$` (`<publisher>.<name>`) | `pulmo.pleural-effusion` |
| Versions | Semantic Versioning 2.0.0 | `3.2.1` |
| Version ranges | Space-separated comparator set, inclusive lower bound | `">=3.2.0 <4.0.0"` |
| Content digests | `sha256:` plus 64 lowercase hex characters | `sha256:3b1f0c7a9d4e5628f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f607` |
| Container references | Registry, repository and **digest** — never a tag | `ghcr.io/pulmo/effusion@sha256:3b1f0c7a9d4e5628f0a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f607` |
| JSON and YAML field names | `lower_snake_case` | `study_instance_uid` |
| State-machine enum values | `UPPER_SNAKE_CASE`, closed set | `REJECTED` |
| Open vocabularies | `lower_snake_case`, documented as open | `"phase": "writing_dicom"` |
| Scores | Dimensionless fraction in `[0.0, 1.0]`, always accompanied by the operating point they are compared against (MOS-SVC-089, ch 2) | `{"score": 0.94, "operating_point": {"id": "balanced", "score_threshold": 0.50}}` |
| Measurements | Value plus UCUM unit plus coded concept; a bare number MUST NOT be reported. The normative shape is `ResultBundle.measurements[]` (MOS-SVC-091 and MOS-SVC-079, ch 2), in which both `unit` and `concept` are coded-concept objects `{scheme, code, display, scheme_uri}` | `{"value": 1243.0, "unit": {"scheme": "UCUM", "code": "ml", "display": "milliliter", "scheme_uri": "http://unitsofmeasure.org"}, "concept": {"scheme": "SCT", "code": "118565006", "display": "Volume", "scheme_uri": "http://snomed.info/sct"}}` |
| Coordinates and spacing | LPS, millimetres, float64 | `"spacing_mm": [0.703125, 0.703125, 1.0]` |
| Tensor digests | `sha256` over the C-contiguous little-endian buffer, with dtype and shape recorded alongside | `{"dtype": "float32", "shape": [1, 1, 96, 160, 160], "sha256": "sha256:d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f6073b1f0c7a9d4e5628f0a1b2c3"}` |
| Trace propagation | W3C Trace Context `traceparent`; a bare `correlation_id` MUST NOT substitute for it | `00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01` |
| Text language | English, including identifiers, comments and log messages | — |

- **MOS-CORE-029** — Examples in this document and fixtures in the repository MUST NOT contain PHI. Example patient identifiers MUST come from the reserved fixture range `MOSFIX-0001` … `MOSFIX-9999`, and example DICOM UIDs MUST use the `2.25.` form.
- **MOS-CORE-030** — Every code, YAML and JSON block in this document MUST be complete and syntactically valid as written. Ellipses, `TODO`, `TBD`, `FIXME` and placeholder tokens MUST NOT appear inside a fenced block.
- **MOS-CORE-031** — A structural or data schema stated normatively in this document MUST be expressed as JSON Schema 2020-12 or as SQL DDL, and MUST be the generation source for language bindings, the OpenAPI document and validation code. A schema MUST NOT be maintained independently in two languages.
- **MOS-CORE-032** — A diagram MUST NOT be used unless every node, edge and label it shows is defined by a contract written out in full in the same chapter. A diagram that introduces an unspecified box is a defect, not a summary.
- **MOS-CORE-033** — Every chapter MUST end with a single `### Acceptance criteria` subsection whose entries are numbered and falsifiable by a CI job or a reviewer executing a stated command.
- **MOS-CORE-034** — Normative text MUST be written in English. Non-Latin-script prose MUST NOT appear in the specification, schemas, identifiers or code comments.

#### 1.5.5 Normative references

- **MOS-CORE-035** — Every external standard this document relies on MUST be pinned to a specific version in `spec/normative-references.yaml`, and cited by its key from that file. An undated reference to a moving standard MUST NOT be used.

```yaml
# spec/normative-references.yaml — pinned at document version 0.2.0.
schema_version: 1
references:
  DICOM:        {title: "DICOM PS3.1-PS3.20", pinned_as: "2026a", normative: true,  used_by: [3, 4, 12]}
  DICOM-PS3.15: {title: "PS3.15 Annex E De-identification Profiles", pinned_as: "2026a", normative: true, used_by: [3]}
  DICOM-PS3.16: {title: "PS3.16 TID 1500 Measurement Report", pinned_as: "2026a", normative: true, used_by: [4]}
  DICOM-PS3.18: {title: "PS3.18 DICOMweb (QIDO-RS, WADO-RS, STOW-RS)", pinned_as: "2026a", normative: true, used_by: [3, 10]}
  RFC2119:      {title: "Key words for use in RFCs", pinned_as: "RFC 2119 (1997-03)", normative: true, used_by: [1]}
  RFC8174:      {title: "Ambiguity of Uppercase vs Lowercase in RFC 2119 Key Words", pinned_as: "RFC 8174 (2017-05)", normative: true, used_by: [1]}
  RFC3339:      {title: "Date and Time on the Internet: Timestamps", pinned_as: "RFC 3339 (2002-07)", normative: true, used_by: [1, 10, 12]}
  RFC9457:      {title: "Problem Details for HTTP APIs", pinned_as: "RFC 9457 (2023-07)", normative: true, used_by: [10]}
  RFC9562:      {title: "Universally Unique IDentifiers (UUIDs)", pinned_as: "RFC 9562 (2024-05)", normative: true, used_by: [4, 12]}
  RFC8785:      {title: "JSON Canonicalization Scheme", pinned_as: "RFC 8785 (2020-06)", normative: true, used_by: [7]}
  JSON-SCHEMA:  {title: "JSON Schema", pinned_as: "2020-12", normative: true, used_by: [2, 6, 10, 12]}
  SEMVER:       {title: "Semantic Versioning", pinned_as: "2.0.0", normative: true, used_by: [1, 6]}
  OCI-IMAGE:    {title: "OCI Image Format Specification", pinned_as: "1.1.0", normative: true, used_by: [2, 6]}
  UCUM:         {title: "Unified Code for Units of Measure", pinned_as: "revision 2.1 (2017-11-21)", normative: true, used_by: [4, 7]}
  W3C-TRACE:    {title: "W3C Trace Context Level 1", pinned_as: "REC 2021-11-23", normative: true, used_by: [13]}
  SNOMED-CT:    {title: "SNOMED CT International Edition", pinned_as: "release recorded per coding-dictionary row", normative: true, used_by: [4, 12]}
  RADLEX:       {title: "RadLex Radiology Lexicon", pinned_as: "4.1", normative: true, used_by: [4]}
  LOINC:        {title: "LOINC", pinned_as: "release recorded per coding-dictionary row", normative: false, used_by: [12]}
  HL7-FHIR:     {title: "HL7 FHIR", pinned_as: "R4 (4.0.1)", normative: false, used_by: [1]}
```

### 1.6 Non-goals

- **MOS-CORE-036** — The table below is binding. A component whose purpose is to implement a non-goal MUST NOT be added to platform core. Reversing a non-goal requires a minor version increment of this document and a recorded decision naming the requirement ID being withdrawn.

| ID | Non-goal | Why not | Use instead |
|---|---|---|---|
| **MOS-CORE-037** | The platform MUST NOT implement a DICOM archive or replace an incumbent PACS. | The value is being installable beside the system a hospital already reads on; replacing the archive turns every install into a migration project. | Orthanc, dcm4chee, or the site's existing PACS behind the DICOM Gateway (ch 3). |
| **MOS-CORE-038** | ~~The platform MUST NOT build its own DICOM viewer.~~ **REVERSED at release 0.4.0, recorded here at specification 0.3.0.** The platform MAY build a first-party clinician-surface viewer. The decision, the incumbents considered and the disqualifying property are in `docs/adr/BUILD_VS_ADOPT.md` §"Chapter 15 §15.3.1 — the Viewer row (`MOS-REL-027`)"; what the first-party viewer MUST guarantee instead is `MOS-UI-009a` (§19.1.2). | The original reasoning stands as the reason the OUTPUT is still standard SEG/SR/SC that other viewers render (ch 4): building a viewer is permitted, writing a private object format is not, and `MOS-IMG-158`'s second-viewer check is what keeps the two apart. | Nothing. A site MAY still read results in OHIF, 3D Slicer, Weasis or its incumbent viewer, and `MOS-IMG-158` requires that at least one of them be used to verify the objects. |
| **MOS-CORE-039** | The platform MUST NOT implement an EHR or EMR. | Clinical record-keeping is a regulated product category with none of the same buyers, and entering it forfeits the "installs beside what you have" position. | The site's existing EHR. |
| **MOS-CORE-040** | The platform MUST NOT implement an HL7 FHIR client, server or resource mapping in releases 0.1.0 through 0.4.0. | FHIR appeared in the previous specification as an aspiration with no consumer; building a client nothing consumes is pure cost. One forward-compatibility hook is retained: the coding dictionary (ch 12) carries LOINC and SNOMED CT columns so a later FHIR `Observation.code` is a projection, not a rebuild. | Direct DICOM SR consumption by the site's reporting system. |
| **MOS-CORE-041** | The platform MUST NOT become an LLM provider or host foundation models as a product. | The platform's leverage is governance over LLM use (`external_llm_allowed`, ch 8), not inference economics. | Per-tenant provider configuration (ch 11). |
| **MOS-CORE-042** | The platform MUST NOT build its own vector database. | No requirement in this document needs one; the deferred knowledge surface reaches retrieval through a tool contract (ch 11). | A tenant-provided store behind a tool, from 0.4.0. |
| **MOS-CORE-043** | The platform MUST NOT implement a GPU scheduler or a distributed resource manager. | Kubernetes plus the vendor device plugin already solve this, and reimplementing it is the fastest way to become unschedulable in a hospital's existing cluster. | Kubernetes, the NVIDIA GPU Operator and device plugin; the platform declares GPU requirements only (ch 13). |
| **MOS-CORE-044** | The platform MUST NOT provide a training orchestrator, and MUST NOT produce model weights, in releases 0.1.0 through 0.4.0. From 0.5.0 it MAY, under chapter 17 (`MOS-TRAIN-001` ff.), which confines the pipeline to the offline evidence plane and forbids any automated path from production data to a serving model (`MOS-TRAIN-005`). | Model production belongs to the `Service` publisher, and a platform that trains models competes with the vendors it is meant to integrate — that tension is real and is not resolved by chapter 17, only bounded by it: the pipeline produces candidates on the same evidence terms as any third-party service, and promotes through the same human gate. See chapter 16. | Before 0.5.0, the publisher's own training stack, packaged as a signed `ServiceVersion`. |
| **MOS-CORE-045** | The platform MUST NOT *build* an annotation authoring tool. It MAY integrate a third-party one as the producer of an `AnnotationSet` (chapter 17 adopts MONAI Label for this). **BOUNDED at specification 0.3.0, not reversed:** the prohibition is on producing an `AnnotationSet` — a label map, a mask, a stored contour, anything a model can be trained on. A reader-drawn shape whose ONLY output is a scalar measurement is not an `AnnotationSet` and never becomes one; `MOS-UI-010a` (§19.1.2) states the boundary and what enforces it. | An `AnnotationSet` records who annotated and under what consensus rule; producing the contours is a separate product with mature incumbents, and integrating one is not the same as becoming one. The boundary is the OUTPUT and not the gesture, because the spec already draws it there: `MOS-UI-200`'s disqualification table says "An ROI-and-measurement toolset cannot produce a label map, so the tool cannot be the producer of an `AnnotationSet`". A tool that cannot be the producer of one is not the thing this non-goal forbids building. | MONAI Label, 3D Slicer, MD.ai, or the publisher's own tooling for anything that IS an `AnnotationSet`; the platform ingests the result (ch 7). |
| **MOS-CORE-046** | The platform MUST NOT become an identity provider. | Hospitals already have one, and owning credentials expands the breach surface for no product gain. | The site's OIDC provider; the platform consumes assertions and issues API keys and service accounts (ch 8). |
| **MOS-CORE-047** | The platform MUST NOT implement billing, claims or revenue-cycle functions. | Unrelated regulatory surface, unrelated buyer, and it would make the core non-installable in research settings. | The site's existing systems. |
| **MOS-CORE-048** | The platform MUST NOT implement prescribing or medication ordering. | These are clinical actions; the platform produces artifacts and records review, and takes no clinical action (ch 9). | CPOE in the site's EHR. |
| **MOS-CORE-049** | The platform MUST NOT take an autonomous clinical decision or action, and MUST NOT present an AI-derived output as an executed medical decision. | This is the boundary that keeps the core out of device classification and keeps the clinician accountable. | `ResultReview`: results are stored and visible, review is recorded, nothing is auto-actioned (ch 9). |
| **MOS-CORE-050** | The platform MUST NOT implement a commercial marketplace, payment flow or distribution channel in releases 0.1.0 through 0.4.0. | The legal structure of a distribution channel cannot be deferred by omission; deferring it deliberately is fine, inheriting it by accident is not. Chapter 16 carries the open question. | A registry of `ServiceVersion`s with signatures and evidence (ch 6, ch 7). |
| **MOS-CORE-051** | The platform MUST NOT diagnose, and MUST NOT compute, adjust or infer a clinically reported number outside a deterministic, versioned code path. | Every clinically load-bearing value must be reproducible from provenance; a generative path cannot offer that (ch 9, ch 11). | Deterministic measurement code with a coded concept and UCUM unit (ch 4). |
| **MOS-CORE-052** | The platform MUST NOT perform clinical validation, and MUST NOT describe any step it performs as "clinical validation". | The platform verifies integrity, compatibility and function; clinical validity is established by the publisher's evidence and the site's governance. Claiming otherwise launders unvalidated models through a governance-shaped process. | "integrity check", "compatibility check", "functional smoke test", "site acceptance test" (ch 7). |

- **MOS-CORE-053** — Deferral is not a non-goal. Functions that are deferred — sealed-mode services, the Kafka queue driver, capability resolution, job cancellation, the MCP tool surface, unattended study-arrival operation, agentic report drafting — MUST be listed against a named release in chapter 15, and MUST NOT be described as non-goals. Anything in neither table is undefined scope (MOS-CORE-008).

### 1.7 How to read this document

#### 1.7.1 Chapter map and normative ownership

- **MOS-CORE-054** — The table below is the normative ownership map required by MOS-CORE-006. A concept listed here is defined only in its owning chapter.

| Ch | Title | Area | Sole normative owner of | Primary audience |
|---|---|---|---|---|
| 1 | Overview, Scope and Conventions | `CORE` 001–199 | Positioning, scope, conformance profiles, glossary, requirement conventions, non-goals | all |
| 2 | Architecture and the Medical Service Contract | `SVC` 001–199 | The platform/service ownership boundary, execution modes, `service.yaml`, the `ResultBundle` contract | publisher, operator |
| 3 | Medical Data Plane: Gateway, De-identification and Triage | `DATA` 001–099 | DICOM Gateway, de-identification profiles and UID remapping, series inventory, `SeriesSelector` evaluation | site, operator |
| 4 | Imaging Contracts: Geometry, Preprocessing and DICOM Output | `IMG` | Canonical volume, source geometry, model space, `PreprocessingSpec`, DICOM output identity and attribute inheritance | publisher, operator |
| 5 | Execution: Jobs, Queue and Failure Handling | `EXEC` | The `Job` state machine, `phase`, the `JobQueue` port, topics, retries, DLQ | operator |
| 6 | Registries, Capability Resolution and Deployment | `REG` | `Capability`, resolution as a pure function, `Deployment`, applicability envelopes | publisher, operator |
| 7 | Evidence Plane: Datasets, Evaluation and Validation Reports | `EVID` | `DatasetVersion`, `DatasetSplit`, `AnnotationSet`, `EvaluationRun`, `AcceptanceCriteria`, `ValidationReport`, the evidence triad | publisher, site |
| 8 | Security, Tenancy and PHI | `SEC` | Tenancy enforcement, RBAC, `PolicyDecision`, PHI containment, `external_llm_allowed` | site, operator |
| 9 | Clinical Safety, Regulatory Posture and Provenance | `SAFE` | `clinical_use_mode`, RUO marking, AI-derived marking, `legal_manufacturer`, `ResultReview`, the provenance record, the OHIF result surface (toolbar button and provenance panel) | site, publisher |
| 10 | API and SDKs | `API` | Resource paths, `POST /api/v1/jobs`, the RFC 9457 error model and its `class` enum, pagination, webhooks | publisher, site |
| 11 | Agentic Layer, Chat and LLM Integration | `AGENT` | `Tool` contract, MCP session binding, content-safety post-conditions, `hallucination_rate` | operator |
| 12 | Data Model and Storage | `STORE` 200–399 | Tables, keys, constraints, the coding dictionary, the `Artifact` table | operator |
| 13 | Observability, Deployment and Scaling | `OPS` | Health, metrics, traces, topology, scaling units | operator, site |
| 14 | Testing and Acceptance | `TEST` | The DICOM test battery, acceptance tests, failure tests, fixture corpus | operator |
| 15 | Delivery Plan and Engineering Rules | `REL` | Release contents 0.1.0–0.4.0, build order, engineering rules | operator |
| 16 | Open Questions | `OPEN` 001–099 | The unresolved forks, as questions | all |
| 19 | Operator Surfaces | `UI` 001–399 | The clinician surface, the no-code engineering surface, annotation | all |
| 18 | Standards and Regulatory Conformance | `CONF` 001–499 | Clause-to-requirement mapping, owner assignment, the certification gap list | all |
| 17 | Model Development Pipeline | `TRAIN` 001–399 | Harvest, curation, annotation, AutoML, training, evaluation, the promotion gate | publisher, operator |

#### 1.7.2 Reading paths

- **MOS-CORE-055** — A party claiming a conformance profile MUST have read and MUST satisfy the chapter set listed for that profile below.

| Reader | Profile | Chapters, in order | Safe to skip |
|---|---|---|---|
| Service publisher | `service-publisher` | 1 → 2 → 4 → 6 → 7 → 9 → 10 → 14 | 5, 8, 11, 12, 13, 15 |
| Deploying site | `deploying-site` | 1 → 3 → 8 → 9 → 6 → 7 → 13 → 14 | 2, 4, 11, 12, 15 |
| Platform operator / core engineer | `platform-core` | 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 12 → 13 → 14 → 15 → 11 → 16 | none |
| Clinical governance reviewer | none (reader) | 1 → 9 → 7 → 3 → 8 | 2, 4, 5, 6, 10–15 |
| Regulatory reviewer | none (reader) | 1 → 9 → 2 → 7 | 3, 5, 6, 10–15 |

### 1.8 Document status and change control

- **MOS-CORE-056** — This document's version tracks the platform minor version. Requirements that apply only from a later release MUST name that release inline; a requirement with no release qualifier applies from 0.1.0.
- **MOS-CORE-057** — Non-normative content — rationale, background, worked examples — MUST be marked by the absence of a requirement ID, and MUST NOT contain uppercase normative keywords.
- **MOS-CORE-058** — An open question listed in chapter 16 MUST NOT be resolved implicitly by any other chapter. A chapter that cannot proceed without an answer MUST state the assumption it makes, tag it with the `OQ-NN` question identifier from chapter 16, and mark it as an assumption rather than a decision.
- **MOS-CORE-059** — A change that alters a name, a state, a boundary or a decision fixed by the canonical spine MUST be made in the spine first and propagated to every affected chapter in the same change. Chapter-local redefinition of a spine-fixed concept is a defect.

#### 1.8.1 The specification gate

The checks below run on every commit that touches the specification. They exist because the previous version's defects were overwhelmingly consistency defects — four spellings of one endpoint, three incompatible manifests, two permission namespaces — that no human reviewer caught and a fifty-line script would have.

```python
#!/usr/bin/env python3
"""specheck.py - CI gate for the MedicalOS specification.

    python medos/tools/specheck.py            # verify; exit 1 on any violation
    python medos/tools/specheck.py --write    # regenerate spec/requirements.yaml

Prints one line per violation: FAIL <check-id> <file>:<line> <detail>
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import yaml

SPEC = Path("docs/MEDICALOS_SPEC.md")
INDEX = Path("spec/requirements.yaml")
REFS = Path("spec/normative-references.yaml")
POSITIONING = Path("spec/positioning.txt")

AREAS = ("CORE", "SVC", "DATA", "IMG", "EXEC", "REG", "EVID", "SEC", "SAFE",
         "API", "AGENT", "OPS", "TEST", "REL", "STORE", "TRAIN", "CONF", "UI", "OPEN")
ALT = "|".join(AREAS)
ID_RE = re.compile(rf"MOS-(?:{ALT})-\d{{3}}")
DEF_RE = re.compile(rf"^\|?\s*(?:[-*]\s+)?\*\*(MOS-(?:{ALT})-\d{{3}})\*\*")
KEY_RE = re.compile(r"(?<![A-Za-z`])(MUST NOT|MUST|SHOULD NOT|SHOULD|MAY)(?![A-Za-z`])")
HEAD_RE = re.compile(r"^## (\d{1,2})\.\s+\S")
ACC_RE = re.compile(r"^### Acceptance criteria\s*$")
CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")
PLACEHOLDER_RE = re.compile(r"(?:\.\.\.|\bTODO\b|\bTBD\b|\bFIXME\b|<placeholder>)")

# (area, chapter) -> inclusive range. MOS-CORE-022.
RANGES = {
    ("CORE", 1): (1, 199), ("SVC", 2): (1, 199), ("DATA", 3): (1, 99),
    ("IMG", 4): (1, 199), ("EXEC", 5): (1, 199), ("REG", 6): (1, 199),
    ("EVID", 7): (1, 199), ("SEC", 8): (1, 199), ("SAFE", 9): (1, 199),
    ("API", 10): (1, 199), ("AGENT", 11): (1, 199), ("STORE", 12): (200, 399),
    ("OPS", 13): (1, 199), ("TEST", 14): (1, 199), ("REL", 15): (1, 199),
    ("TRAIN", 17): (1, 399), ("CONF", 18): (1, 499), ("UI", 19): (1, 399), ("OPEN", 16): (1, 99),
}

# MOS-CORE-017. Suppressed between the allow-banned comment markers.
BANNED = [
    (r"Medical Agent Operating System", "MOS-CORE-001"),
    (r"MedicalOS Operating System", "MOS-CORE-001"),
    (r"/api/v1/ai/jobs", "MOS-CORE-017"),
    (r"/agents/\{[a-z_]+\}/execute", "MOS-CORE-017"),
    (r'"progress"\s*:\s*[01]?\.\d', "MOS-CORE-017"),
    (r"\bPOSTPROCESSING\b", "MOS-CORE-017"),
    (r"medicalos\.(ct|mr|xr)\.[a-z]+", "MOS-CORE-017"),
    (r"performs? clinical validation", "MOS-CORE-052"),
]

fails: list[str] = []


def fail(check: str, line: int, detail: str) -> None:
    fails.append(f"FAIL {check} {SPEC}:{line} {detail}")


def scan(text: str):
    """Yield (lineno, chapter, unit_text, in_fence, banned_ok) per logical unit."""
    chapter, fence, allow, buf, start = 0, False, False, [], 1
    for n, raw in enumerate(text.splitlines(), 1):
        if raw.startswith("```"):
            fence = not fence
            if buf:
                yield start, chapter, " ".join(buf), False, allow
                buf = []
            yield n, chapter, raw, True, allow
            continue
        if fence:
            yield n, chapter, raw, True, allow
            continue
        if "specheck:allow-banned" in raw:
            allow = "/specheck" not in raw
            continue
        m = HEAD_RE.match(raw)
        if m:
            chapter = int(m.group(1))
        if raw.strip() == "" or raw.startswith(("|", "- ", "* ", "#")):
            if buf:
                yield start, chapter, " ".join(buf), False, allow
                buf = []
            if raw.strip():
                yield n, chapter, raw, False, allow
            continue
        if not buf:
            start = n
        buf.append(raw.strip())
    if buf:
        yield start, chapter, " ".join(buf), False, allow


def main(write: bool) -> int:
    text = SPEC.read_text(encoding="utf-8")
    defined: dict[str, tuple[int, int]] = {}
    referenced: set[str] = set()
    records: list[dict] = []
    chapters_seen: set[int] = set()
    acceptance: set[int] = set()

    for line, chapter, unit, in_fence, allow in scan(text):
        if chapter:
            chapters_seen.add(chapter)
        if ACC_RE.match(unit):
            acceptance.add(chapter)
        if CYRILLIC_RE.search(unit):                              # text.english_only
            fail("text.english_only", line, "non-Latin script in specification text")
        if in_fence:
            if PLACEHOLDER_RE.search(unit):                       # code.no_placeholder
                fail("code.no_placeholder", line, unit.strip()[:60])
            continue
        if not allow:
            for pat, rid in BANNED:                               # text.banned_vocabulary
                if re.search(pat, unit):
                    fail("text.banned_vocabulary", line, f"{pat} ({rid})")
        ids = ID_RE.findall(unit)
        referenced.update(ids)
        if KEY_RE.search(unit) and not ids:                       # text.keyword_has_id
            fail("text.keyword_has_id", line, unit[:70])
        m = DEF_RE.match(unit)
        if not m:
            continue
        rid = m.group(1)
        if rid in defined:                                        # ids.unique
            fail("ids.unique", line, f"{rid} already defined at line {defined[rid][0]}")
            continue
        defined[rid] = (line, chapter)
        area, num = rid.split("-")[1], int(rid.split("-")[2])
        lo, hi = RANGES.get((area, chapter), (0, -1))
        if not lo <= num <= hi:                                   # ids.range
            fail("ids.range", line, f"{rid} outside chapter {chapter} range {lo}-{hi}")
        lvl = KEY_RE.search(unit)
        records.append({
            "id": rid, "chapter": chapter, "level": lvl.group(1) if lvl else "NONE",
            "status": "active",
            "text": re.sub(r"\s+", " ", DEF_RE.sub("", unit)).lstrip(" -—").strip(),
        })

    for rid in sorted(referenced - set(defined)):                 # ids.xref
        fail("ids.xref", 0, f"{rid} referenced but never defined")
    for ch in sorted(chapters_seen - acceptance):                 # struct.acceptance_section
        fail("struct.acceptance_section", 0, f"chapter {ch} has no Acceptance criteria")
    if POSITIONING.read_text(encoding="utf-8").strip() not in text:
        fail("text.positioning_verbatim", 0, "positioning statement not present verbatim")
    refs = yaml.safe_load(REFS.read_text(encoding="utf-8"))["references"]
    for key, ref in refs.items():                                 # refs.pinned
        if not ref.get("pinned_as"):
            fail("refs.pinned", 0, f"reference {key} has no pinned version")

    index = {
        "schema_version": 1,
        "document_version": "0.2.0",
        "generated_from": str(SPEC).replace("\\", "/"),
        "source_sha256": "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "requirements": sorted(records, key=lambda r: r["id"]),
    }
    rendered = yaml.safe_dump(index, sort_keys=False, allow_unicode=False, width=100)
    if write:
        INDEX.write_text(rendered, encoding="utf-8")
    elif INDEX.read_text(encoding="utf-8") != rendered:           # ids.index_sync
        fail("ids.index_sync", 0, "spec/requirements.yaml is stale; run --write")

    for f in fails:
        print(f)
    print(f"specheck: {len(defined)} requirements, {len(fails)} violations")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main("--write" in sys.argv))
```

### Acceptance criteria

Each check below is executable. Checks 1–11 are `specheck.py` check ids and are run by `python medos/tools/specheck.py`, which MUST exit 0.

1. **`ids.unique`** — every `MOS-<AREA>-<NNN>` in the document is defined exactly once, at a unit whose leading bolded token is the ID. Falsified by defining any ID twice.
2. **`ids.range`** — every defined ID uses its chapter's allocated area and falls inside that area's allocated range per the table in §1.5.2. Falsified by issuing `MOS-CORE-200` from chapter 1, by chapter 2 issuing an `MOS-CORE-` id, or by chapter 12 issuing an `MOS-EXEC-` id instead of an `MOS-STORE-` id.
3. **`ids.xref`** — every ID referenced anywhere resolves to a defined ID. Falsified by a cross-reference to a requirement no chapter wrote.
4. **`ids.index_sync`** — `python medos/tools/specheck.py --write` produces a `spec/requirements.yaml` byte-identical to the committed file, and every record carries `id`, `chapter`, `level` and non-empty `text`. Falsified by hand-editing the index.
5. **`text.keyword_has_id`** — no unit outside a fenced block contains MUST, MUST NOT, SHOULD, SHOULD NOT or MAY without at least one requirement ID. Falsified by one un-numbered normative sentence in any chapter.
6. **`text.banned_vocabulary`** — zero matches for the §1.4.7 patterns across `docs/**/*.md`, `**/*.go`, `**/*.py`, `**/*.sql`, `**/*.yaml` and `medos/web/**/*.ts`, outside the `specheck:allow-banned` markers. Run the same patterns over the source tree with: `git grep -nE 'POSTPROCESSING|/api/v1/ai/jobs|medicalos\.(ct|mr|xr)\.' -- ':!docs/MEDICALOS_SPEC.md'` — expected output is empty.
7. **`text.positioning_verbatim`** — the contents of `spec/positioning.txt` appear in the document byte-for-byte. Falsified by paraphrasing the positioning statement anywhere.
8. **`text.english_only`** — no Cyrillic or other non-Latin script appears in the specification. Command equivalent: `grep -nP '[\x{0400}-\x{04FF}]' docs/MEDICALOS_SPEC.md` returns nothing.
9. **`code.no_placeholder`** — no fenced block contains `...`, `TODO`, `TBD`, `FIXME` or `<placeholder>`. Falsified by one elided YAML example.
10. **`struct.acceptance_section`** — every `## <N>. <Title>` chapter contains exactly one `### Acceptance criteria` subsection.
11. **`refs.pinned`** — every entry in `spec/normative-references.yaml` has a non-empty `pinned_as`, and every standard cited in the document resolves to a key in that file.
12. **Non-goal completeness** — every row of the §1.6 table has a requirement ID, a non-empty "Why not" cell and a non-empty "Use instead" cell: `python - <<'PY'` parsing the table and asserting three non-empty cells per row exits 0. Falsified by adding a non-goal with no stated alternative.
13. **Glossary coverage** — every term appearing in the §1.4 tables is used at least once outside chapter 1, and every capitalised entity name from the canonical spine's entity list (§3 of the spine) has a glossary row. Falsified by an entity name used in chapters 2–15 with no definition in §1.4.
14. **Profile coverage** — for each of `platform-core`, `service-publisher` and `deploying-site`, at least one requirement in `spec/requirements.yaml` names that actor, and every chapter in that profile's reading path (§1.7.2) contributes at least one such requirement. Falsified by a profile whose chapter set imposes no obligation on it.
15. **Ownership non-duplication** (reviewer check) — for each concept in the §1.7.1 ownership map, `git grep -n "<concept>" docs/MEDICALOS_SPEC.md` shows a definition only in the owning chapter; all other hits are cross-references of the form `MOS-<AREA>-<NNN>`. Falsified by the `Job` state enum, the ownership boundary, or the de-identification profile being restated in a second chapter.
16. **Diagram discipline** (reviewer check) — for every diagram in the document, each node and edge label it shows is defined by a table, schema or requirement in the same chapter. Falsified by one unlabelled or unspecified box.

---

[Index](../../MEDICALOS_SPEC.md) · [2. Architecture and the Medical Service Contract →](02-architecture.md)
