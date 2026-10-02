<!-- MedicalOS Specification v0.4.0 — chapter 9 of 19. Normative.
     105 requirements. Do not edit without a requirement-ID review. -->

[← 8. Security, Tenancy and PHI](08-security.md) · [Index](../../MEDICALOS_SPEC.md) · [10. API and SDKs →](10-api.md)

---

## 9. Clinical Safety, Regulatory Posture and Provenance

This chapter states who the manufacturer is, what metadata makes that chain traceable, how the research/clinical boundary is enforced by code rather than by policy documents, how a human review is recorded without becoming a gate nobody implemented, and what a provenance record must contain for a result to be genuinely reproducible.

It does not define the Job state machine (Chapter 5), the DICOM writing mechanics or geometry (Chapter 4), the Gateway or de-identification (Chapter 3), RBAC mechanics (Chapter 8), Deployment lifecycle states (Chapter 6), evaluation methodology (Chapter 7), or the API envelope (Chapter 10). It constrains all of them.

### 9.1. The manufacturer boundary

**MOS-SAFE-001** — MedicalOS core MUST be described, in the repository README, in every release artifact, in the API landing document and in the web UI footer, by the following statement verbatim:

> MedicalOS integrates, governs and evidences medical AI services. It does not diagnose, does not replace a PACS, and is not itself a medical device.

**MOS-SAFE-002** — The MedicalOS project (upstream maintainers, the Apache-2.0 source tree, and any binary release published under the project name) MUST NOT make, imply or transport a clinical performance claim of its own. The project publishes software; it does not publish a device.

**MOS-SAFE-003** — For every clinical output the platform produces, the **manufacturer** is the publisher of the `ServiceVersion` that produced it, as named in `clinical.legal_manufacturer` (§9.2). The platform is an integrator and a record-keeper. Every generated DICOM object, every `Result`, and every provenance record MUST carry that manufacturer identity (§9.5, §9.9), so that the chain from a rendered overlay back to a legally responsible party is resolvable offline from the object alone.

**MOS-SAFE-004** — Responsibilities are divided as follows and MUST NOT be silently reassigned by configuration:

| Concern | MedicalOS project (upstream) | Deploying site (operator) | ServiceVersion publisher (manufacturer) |
|---|---|---|---|
| Clinical claim (what the output means) | none, forbidden by MOS-SAFE-002 | none | owns; declared in `intended_use` |
| Regulatory clearance / conformity | none | verifies applicability to its jurisdiction | owns; declared in `regulatory_status` |
| Clinical evidence (performance on a named cohort) | none | may run *site acceptance testing* only (Ch. 7) | owns; a signed `ValidationReport` |
| Correctness of geometry, UID derivation, DICOM conformance | owns | — | must consume the platform contracts unmodified |
| PHI containment, tenancy, audit | owns the mechanism | owns the deployment, keys, network, retention | must not hold PACS credentials |
| Choice to run in `clinical` mode | owns the gate (§9.4) | owns the decision, with a named approver | supplies the evidence the gate checks |
| Reading workflow, who reviews what | none | owns | may state a required reading paradigm |

**MOS-SAFE-005** — A deploying site that modifies a `ServiceVersion` (rebuilds its image, substitutes weights, edits its `PreprocessingSpec`, or alters its declared operating threshold) becomes the manufacturer of the resulting service. The platform MUST make this mechanically true: any such change produces a new `ServiceVersion` whose signature no longer matches the original publisher's key, and the deployment gate in **MOS-SAFE-045** MUST fail with `violation: signature_not_from_declared_manufacturer` until a `legal_manufacturer` block naming the site is supplied and the site signs it.

**MOS-SAFE-006** — A site that assembles and uses a service entirely in-house (no transfer to another legal entity) MAY be operating under a health-institution in-house exemption in its jurisdiction. The platform MUST NOT assert that such an exemption applies. It MUST offer `regulatory_status[].status = in_house_exemption` (§9.3) as an explicitly declared, auditable claim made by the site, carrying the site's own identity in `legal_manufacturer`. This document is engineering specification, not legal advice; **MOS-SAFE-007** requires this sentence to appear in `docs/REGULATORY.md`.

**MOS-SAFE-007** — The repository MUST contain `docs/REGULATORY.md` carrying: the MOS-SAFE-001 statement, the MOS-SAFE-004 table, the enumerated `regulatory_status.status` values of §9.3, the `clinical_use_mode` gate of §9.4, and the disclaimer that the document is not legal advice.

**MOS-SAFE-008** — No platform-performed step, endpoint, UI string, log message, event name, database value or document heading may use the terms **"clinical validation"**, **"clinically validated"**, **"validated for clinical use"**, **"certified"**, **"approved for diagnosis"**, or any translation of these, to describe something the platform does. The platform-performed steps have these fixed names, and CI MUST enforce the ban (see acceptance check 1):

| Forbidden name | Required name | Owner chapter |
|---|---|---|
| clinical validation | evidence verification (signature + digest check on a `ValidationReport`) | Ch. 7 |
| functional validation | functional smoke test | Ch. 14 |
| security validation | supply-chain integrity check | Ch. 8 |
| compatibility validation | compatibility check | Ch. 6 |
| site validation | site acceptance test | Ch. 7 |

**MOS-SAFE-009** — The platform MUST NOT compute, infer, adjust or default any field of `intended_use`, `regulatory_status` or `legal_manufacturer`. These are publisher assertions. The platform MUST validate their structure, MUST verify that their digest is covered by the `ServiceVersion` signature, and MUST reject a `ServiceVersion` whose `clinical` block is absent, structurally invalid, or not covered by the signature.

**MOS-SAFE-010** — Being **open source** does not dilute MOS-SAFE-003. A contributor who merges a patch to core is not a manufacturer of any clinical output. A packager who publishes a `ServiceVersion` under their own key is. The `CONTRIBUTING.md` MUST state that the project accepts no contribution that embeds a clinical threshold, a nosology-specific heuristic, or a model weight into core — such material belongs in a `ServiceVersion` with a named manufacturer.

**MOS-SAFE-011** — The platform MUST refuse to run a `ServiceVersion` whose `intended_use.autonomy` is `autonomous` (§9.2). MedicalOS in 0.1–0.4 supports assistive services only; there is no code path in which a platform output is an executed medical decision. This is a hard refusal at deployment time, not a warning.

**MOS-SAFE-012** — **AMENDED at specification 0.4.0.** Every user-facing surface that displays an AI-derived finding (web UI result panel, ~~OHIF provenance panel~~ the clinician surface's provenance panel, exported PDF, SR rendering) MUST display, adjacent to the finding and without requiring interaction: the `service_id`, the `ServiceVersion`, the `legal_manufacturer.name`, the `clinical_use_mode` of the producing deployment, and the `review_status` of the `Result` (§9.6).

The amendment is to the parenthesis and to nothing else; no field left the set and none became optional. The subject of this requirement has always been *every user-facing surface that displays an AI-derived finding*, and the parenthesis lists instances rather than bounding the subject — one instance moved when the OHIF deployment was withdrawn at release 0.4.0. Two consequences follow and are written here so that neither has to be inferred. First, the first-party viewer at `viewer/`, served at `/mos-viewer/`, is in scope whenever it renders a generated object: a SEG produced by a `ServiceVersion` is an AI-derived finding however it is drawn, and `MOS-UI-007` already declares both surfaces MedicalOS-controlled in the sense of this requirement. Second, that scope is UNMET today and was unmet before this amendment — `legal_manufacturer` and `review_status` occur nowhere under `medos/web/`, which `docs/releases/0.1.0.md` records as the release defect `ui010-safe012-adjacency-set`. Re-pointing an example closes nothing, and MUST NOT be read as having closed it.

### 9.2. The intended-use block

**MOS-SAFE-013** — Every `ServiceVersion` manifest (`service.yaml`) MUST contain a top-level `clinical:` block. A manifest without it MUST be rejected at registration with `class: validation_error` and `pointer: /clinical`. There is no default, no inheritance from the `Service`, and no partial acceptance.

**MOS-SAFE-014** — The `clinical:` block has exactly the following members. `required` means the key MUST be present; `nullable` means the key MUST be present and MAY be `null`, with the null recorded and surfaced in the UI as "not declared" rather than hidden.

| Key | Type | Required | Constraint |
|---|---|---|---|
| `intended_use.statement` | string | required | 40–2000 chars, free prose |
| `intended_use.intended_user` | enum | required | `radiologist` \| `radiology_resident` \| `pulmonologist` \| `emergency_physician` \| `technologist` \| `researcher` \| `other` |
| `intended_use.intended_setting` | enum[] | required | ≥1 of `inpatient`, `emergency_department`, `outpatient`, `screening_programme`, `research` |
| `intended_use.reading_paradigm` | enum | required | `triage` \| `first_reader` \| `concurrent_read` \| `second_reader` \| `post_read_qa` |
| `intended_use.autonomy` | enum | required | `assistive` only; `autonomous` is refused by MOS-SAFE-011 |
| `intended_use.output_kinds` | enum[] | required | subset of `SEG`, `SR`, `SC`, `MEASUREMENT` |
| `indications[]` | object[] | required, ≥1 | each `{id, text, codes[]}`; `codes[]` entries are `{system, code, meaning}` with `system` ∈ `SNOMED-CT`, `RadLex`, `DCM`, `LOINC`, `ICD-10` |
| `contraindications[]` | object[] | required, ≥0 | `{id, text}`; empty list is a positive assertion, not an omission |
| `target_population` | object | required | `{age_min_years, age_max_years, sex, body_part, pregnancy}`; `sex` ∈ `any`,`female`,`male`; `pregnancy` ∈ `included`,`excluded`,`not_evaluated` |
| `known_limitations[]` | object[] | required, ≥1 | `{id, text, evidence_ref}`; `evidence_ref` is an `EvaluationRun` id or `null` |
| `not_validated_for[]` | object[] | required, ≥1 | `{id, text}` |
| `known_failure_modes[]` | object[] | required, ≥1 | `{id, text, detection, mitigation}`; `detection` ∈ `applicability_envelope`, `output_plausibility_gate`, `human_review`, `none` |
| `input_constraints` | object | required | human-readable twin of the machine `SeriesSelector` (Ch. 3); MOS-SAFE-018 applies |
| `training_population` | object | required, **nullable** | `{n_patients, n_studies, sites, scanner_vendors[], acquisition_years, demographics}` |
| `risk_classification.imdrf` | object | required | `{healthcare_situation, information_significance, category}`; MOS-SAFE-019 applies |
| `operating_point` | object | required, nullable | `{score_threshold, threshold_definition, source_evaluation_run_id}`; `score_threshold` is the member name Chapter 2 `MOS-SVC-020` fixes for this number and the one Chapter 12 persists on `result_findings` — it MUST NOT be respelled `threshold` or `value` here (Chapter 6 states the mapping alongside `MOS-REG-050`); nullable only when `output_kinds` contains no probabilistic finding |
| `legal_manufacturer` | object | required | MOS-SAFE-020 |
| `regulatory_status[]` | object[] | required, ≥1 | §9.3 |
| `clinical_evidence` | object | required, nullable | `{validation_report_id, validation_report_digest, signature_key_id}`; null forbidden when any `regulatory_status[].status` is in the CLEARED set |

**MOS-SAFE-015** — `not_validated_for[]` and `known_failure_modes[]` MUST each carry at least one entry. A publisher with nothing to declare MUST write the entry explicitly (e.g. "Not validated for paediatric studies; no case under 18 years in any validation cohort"). Fail closed: an empty list is a registration error, not an empty set.

**MOS-SAFE-016** — `training_population` is nullable because most defensible first services are built on public cohorts whose full demographics are unavailable. When it is `null`, the platform MUST surface the string "Training population not declared by the publisher" in every surface named in MOS-SAFE-012 and MUST set `provenance.evidence.training_population_declared = false`.

**MOS-SAFE-017** — Every `id` in `indications`, `contraindications`, `known_limitations`, `not_validated_for` and `known_failure_modes` MUST be unique within the `clinical` block and stable across `ServiceVersion`s of the same `Service` when the text is unchanged. Provenance references these ids (§9.9), so renumbering them breaks historical records.

**MOS-SAFE-018** — `input_constraints` is prose for humans; the `SeriesSelector` is the machine contract. The platform MUST NOT evaluate `input_constraints`. It MUST, at registration, emit a warning event `service.clinical.constraint_divergence` when a numeric field present in both (`slice_thickness_mm`, `modality`, `body_part`) disagrees, and MUST record the divergence on the `ServiceVersion`. The `SeriesSelector` always wins at runtime.

**MOS-SAFE-019** — `risk_classification.imdrf.category` MUST equal the value derived from the two axes by this table; a manifest whose declared category disagrees MUST be rejected. This is a structural consistency check, not a regulatory determination.

| significance \ situation | `non_serious` | `serious` | `critical` |
|---|---|---|---|
| `inform` | I | I | II |
| `drive` | I | II | III |
| `treat_or_diagnose` | II | III | IV |

**MOS-SAFE-020** — `legal_manufacturer` MUST carry `{name, legal_form, address, country, contact_email, srn_or_registration_id, signing_key_id}`. `name` MUST be ≤ 64 characters so it can be written into DICOM `Manufacturer` (0008,0070) without truncation (Ch. 4). `signing_key_id` MUST identify the key that signed the `ServiceVersion`; the platform MUST verify the match at registration and refuse on mismatch.

**MOS-SAFE-021** — The platform MUST compute `intended_use_digest = sha256(canonical_json(clinical))` (RFC 8785 JCS canonicalisation) at registration, store it on the `ServiceVersion`, and write it into every provenance record produced by that version (§9.9). Any change to any field of the `clinical` block therefore requires a new `ServiceVersion`; there is no in-place edit endpoint.

**MOS-SAFE-022** — `GET /api/v1/service-versions/{id}/clinical` MUST return the block verbatim plus `intended_use_digest`, without authentication beyond tenant scope, and MUST be renderable as a one-page "service data sheet" by the UI. Clinical claims that only exist inside a YAML file in a container image are not traceable.

#### Worked example: a pleural effusion service

```yaml
# service.yaml — pulmo.pleural-effusion, version 1.4.0 (excerpt: the whole `clinical:` block)
apiVersion: medicalos.io/v1
kind: ServiceVersion
metadata:
  service_id: pulmo.pleural-effusion
  version: 1.4.0
spec:
  execution_mode: native
  clinical:
    intended_use:
      statement: >
        Automated detection and volumetric quantification of pleural effusion on
        axial chest CT of adults, reported as a DICOM Segmentation of the effusion
        and a TID 1500 Structured Report carrying left, right and total effusion
        volume in millilitres. Intended for use by a qualified radiologist as a
        concurrent-read aid during primary interpretation. Output is a machine
        estimate and is not a diagnosis.
      intended_user: radiologist
      intended_setting: [inpatient, emergency_department]
      reading_paradigm: concurrent_read
      autonomy: assistive
      output_kinds: [SEG, SR, MEASUREMENT]

    indications:
      - id: IND-1
        text: >
          Adults aged 18 years or older undergoing non-contrast or contrast-enhanced
          chest CT for any clinical indication, where the acquisition covers both
          hemithoraces from the lung apices to the costophrenic recesses.
        codes:
          - {system: SNOMED-CT, code: "60046008", meaning: "Pleural effusion"}
          - {system: RadLex, code: "RID4872", meaning: "pleural effusion"}
          - {system: SNOMED-CT, code: "169069000", meaning: "CT of chest"}

    contraindications:
      - id: CON-1
        text: >
          Studies acquired within 72 hours of pleurodesis, or with an indwelling
          pleural catheter or chest tube in the imaged hemithorax. Iatrogenic fluid
          and tube-related collections were excluded from the validation cohort.
      - id: CON-2
        text: >
          Studies with a large extrapleural collection, prior pneumonectomy, or
          post-lobectomy space. The model does not distinguish a post-surgical
          space from an effusion.

    target_population:
      age_min_years: 18
      age_max_years: 100
      sex: any
      body_part: CHEST
      pregnancy: not_evaluated

    known_limitations:
      - id: LIM-1
        text: >
          Per-case Dice falls from 0.89 (median) to 0.71 on loculated effusions
          (n=34 subgroup of the acceptance cohort).
        evidence_ref: evr_01J9K2Q7RSTV3WXYZ8ABCD
      - id: LIM-2
        text: >
          Volumes for effusions below 25 ml carry a median absolute error of 9 ml,
          which is a large relative error. Values below 25 ml are reported but
          flagged low_confidence in the ResultBundle.
        evidence_ref: evr_01J9K2Q7RSTV3WXYZ8ABCD
      - id: LIM-3
        text: >
          Evaluated on Siemens and GE scanners only. No Canon or Philips case in
          any validation cohort.
        evidence_ref: null

    not_validated_for:
      - id: NVF-1
        text: Paediatric studies (< 18 years). No case under 18 in any cohort.
      - id: NVF-2
        text: >
          Prone, decubitus or feet-first-supine acquisitions. Orientation other
          than head-first axial was excluded at cohort construction.
      - id: NVF-3
        text: Slice thickness above 3.0 mm; no thick-recon case was evaluated.
      - id: NVF-4
        text: Longitudinal change measurement. Volumes from two studies were never compared.

    known_failure_modes:
      - id: FM-1
        text: >
          Ascites adjacent to the diaphragm is segmented as pleural fluid when the
          acquisition is truncated below the costophrenic recess.
        detection: applicability_envelope
        mitigation: >
          The SeriesSelector requires caudal coverage to the costophrenic recess;
          studies failing it are REJECTED with no_eligible_series.
      - id: FM-2
        text: >
          Dense consolidation with an adjacent thin effusion produces a merged mask
          and an over-estimated volume.
        detection: output_plausibility_gate
        mitigation: >
          Volume above 3500 ml per hemithorax sets plausibility_flag and the SR
          measurement carries a qualifier concept.
      - id: FM-3
        text: Streak artefact from a pacemaker lead creates a false small collection.
        detection: human_review
        mitigation: Concurrent-read paradigm; reviewer sees the overlay on the source series.

    input_constraints:
      modality: [CT]
      body_part: [CHEST]
      slice_thickness_mm: {min: 0.5, max: 3.0}
      kernel_class: [soft, standard]
      orientation: head_first_axial_within_15_degrees
      min_instances: 80
      contrast: [none, arterial, portal_venous]
      coverage: apices_to_costophrenic_recess

    training_population:
      n_patients: 612
      n_studies: 641
      sites: 2
      scanner_vendors: [SIEMENS, GE MEDICAL SYSTEMS]
      acquisition_years: "2019-2024"
      demographics:
        age_median: 64
        age_iqr: [52, 74]
        female_fraction: 0.43

    risk_classification:
      imdrf:
        healthcare_situation: serious
        information_significance: drive
        category: II

    operating_point:
      score_threshold: 0.45
      threshold_definition: >
        Per-voxel foreground probability threshold applied after inverse transform
        to source geometry, before connected-component filtering at 2 ml.
      source_evaluation_run_id: evr_01J9K2Q7RSTV3WXYZ8ABCD

    legal_manufacturer:
      name: "Pulmo Medical Imaging GmbH"
      legal_form: "GmbH"
      address: "Hauptstrasse 14, 10827 Berlin, Germany"
      country: DE
      contact_email: "regulatory@pulmo.example"
      srn_or_registration_id: "DE-MF-000012345"
      signing_key_id: "cosign:pulmo-release-2026"

    regulatory_status:
      - jurisdiction: EU
        status: investigational
        device_class: null
        identifier: null
        authorising_body: null
        certificate_valid_until: null
        evidence_uri: "https://pulmo.example/regulatory/eu-investigational-2026.pdf"
        declared_at: "2026-03-11"
      - jurisdiction: US
        status: not_cleared
        device_class: null
        identifier: null
        authorising_body: null
        certificate_valid_until: null
        evidence_uri: null
        declared_at: "2026-03-11"

    clinical_evidence:
      validation_report_id: vr_01J9M4T1CDEF5GHJK6LMNP
      validation_report_digest: "sha256:9f2c1ab7e4d8c0335b6a71f2e9d4408c5b1e7a03d6f2489ac13be550772a1d6e"
      signature_key_id: "cosign:pulmo-release-2026"
```

The example is deliberately a service that is **not** cleared anywhere. Under §9.4 it can only be deployed `research_only`, and every object it writes carries RUO marking. That is the honest starting state of the reference application and the platform must make it comfortable, not exceptional.

### 9.3. Regulatory status per jurisdiction

**MOS-SAFE-023** — `regulatory_status` is a list, one entry per jurisdiction, never a scalar. A jurisdiction absent from the list means "not declared", which the platform MUST treat identically to `not_cleared` for gating purposes and MUST render distinctly in the UI ("not declared for this jurisdiction").

**MOS-SAFE-024** — Each entry has the shape `{jurisdiction, status, device_class, identifier, authorising_body, certificate_valid_until, evidence_uri, declared_at}`. `jurisdiction` MUST be an ISO 3166-1 alpha-2 code or the literal `EU`. `declared_at` MUST be an ISO 8601 date.

**MOS-SAFE-025** — `status` MUST be one of this closed enum. The `Cleared` column defines the CLEARED set used by the clinical gate (MOS-SAFE-045).

| `status` | Meaning | Cleared | Extra required fields |
|---|---|---|---|
| `not_cleared` | No marketing authorisation; not offered for clinical use | no | none |
| `research_only` | Offered for research use only by the publisher | no | none |
| `investigational` | Under a clinical investigation / IDE-equivalent | no | `evidence_uri` |
| `in_house_exemption` | Health-institution in-house use, declared by the site (MOS-SAFE-006) | **yes** | `authorising_body` = the institution; `evidence_uri` |
| `ce_mdr` | CE mark under Regulation (EU) 2017/745 | **yes** | `device_class`, `identifier`, `authorising_body`, `certificate_valid_until` |
| `ce_mdd_legacy` | Legacy MDD certificate under MDR transitional provisions | **yes** | `device_class`, `identifier`, `authorising_body`, `certificate_valid_until` |
| `ukca` | UKCA marking | **yes** | `device_class`, `identifier`, `authorising_body`, `certificate_valid_until` |
| `fda_510k` | FDA 510(k) clearance | **yes** | `device_class`, `identifier` (K-number) |
| `fda_de_novo` | FDA De Novo grant | **yes** | `identifier` (DEN-number) |
| `fda_pma` | FDA PMA approval | **yes** | `identifier` (P-number) |
| `health_canada_mdl` | Health Canada Medical Device Licence | **yes** | `device_class`, `identifier` |
| `tga_artg` | ARTG inclusion (Australia) | **yes** | `identifier` |
| `pmda_shonin` | PMDA approval / certification (Japan) | **yes** | `identifier` |
| `national_registration` | Any other national registration | **yes** | `authorising_body`, `identifier`, `evidence_uri` |
| `withdrawn` | Previously cleared, withdrawn by the manufacturer or authority | no | `declared_at` |

**MOS-SAFE-026** — When `certificate_valid_until` is present and in the past relative to the platform clock, the entry MUST be treated as `withdrawn` for gating purposes from that date onward, without a manifest change. The platform MUST run this check on a daily schedule and MUST emit `service_version.regulatory_status.expired` and an `AuditEvent` when the transition occurs.

**MOS-SAFE-027** — Expiry of the last CLEARED entry for a jurisdiction MUST NOT silently stop work. It MUST: (a) demote every `clinical`-mode Deployment of that `ServiceVersion` in that jurisdiction to `research_only`, (b) write an `AuditEvent` per demoted Deployment, (c) emit the webhook `deployment.clinical_use_mode.demoted`, (d) leave every already-produced `Result` untouched and unchanged (MOS-SAFE-055). Newly produced objects from that moment carry RUO marking.

**MOS-SAFE-028** — Every `Deployment` MUST carry a `jurisdiction` field (ISO 3166-1 alpha-2 or `EU`), set at creation, immutable. It is the jurisdiction against which MOS-SAFE-045 resolves `regulatory_status`. There is no global default and a Deployment without it MUST be rejected.

**MOS-SAFE-029** — The platform MUST NOT interpret, validate against a registry, or verify the truth of `identifier`, `authorising_body` or `evidence_uri`. It MUST store them, digest them under `intended_use_digest`, display them, and reproduce them in provenance. Verification is the site's act (MOS-SAFE-004).

**MOS-SAFE-030** — `GET /api/v1/service-versions?jurisdiction=DE&cleared=true` MUST filter on the CLEARED set for the named jurisdiction. Site operators need to answer "what may I run clinically here" as a query, not as a spreadsheet.

**MOS-SAFE-031** — A `ServiceVersion` MUST NOT be deleted or its `clinical` block mutated after any `Job` has referenced it. Recall is expressed by `status: withdrawn` plus a Deployment-level stop (Ch. 6), never by erasure. **MOS-SAFE-032** — `GET /api/v1/service-versions/{id}/results-produced` MUST return a cursor-paginated list of `result_id` values produced by that version, so a recall can enumerate the affected records; this endpoint MUST be authorised by `result.read` and MUST be tenant-scoped.

### 9.4. `clinical_use_mode` and technically enforced research-only operation

**MOS-SAFE-033** — `clinical_use_mode` ∈ {`research_only`, `clinical`} is a field on `Deployment`. Its default is `research_only`. There is no tenant-wide, service-wide or environment-wide override. A Deployment created without the field MUST be created as `research_only`.

**MOS-SAFE-034** — `clinical_use_mode` MUST be copied into the `Job` row at job creation and into the `Result` row at result creation, as immutable columns. Later changes to the Deployment MUST NOT alter existing Jobs or Results. This is what makes MOS-SAFE-027 safe to execute automatically.

**MOS-SAFE-035** — The following eight enforcement points are the whole of the research/clinical boundary. Each is code with a named failure; none is a policy document.

| # | Enforcement point | Where | Failure behaviour |
|---|---|---|---|
| E1 | Clinical promotion gate | `PATCH /api/v1/deployments/{id}` | 422 problem+json, `class: policy_violation`, `violations[]` |
| E2 | Mode pinning | Job creation, Result creation | mode copied immutably; no endpoint mutates it |
| E3 | Marking gate | platform DICOM writer, before STOW | object not emitted; Job `FAILED`, `error.code = safety_marking_absent` |
| E4 | Destination gate | DICOM Gateway STOW route (Ch. 3) | 403, `reason = research_object_to_clinical_destination` |
| E5 | Read-path filter | `GET /api/v1/results`, ~~OHIF result feed~~ any UI or viewer feed derived from it (amended at specification 0.4.0; `MOS-SAFE-041` below already binds "every UI/viewer feed derived from it" and named no product) | research results excluded unless `include_research=true` **and** caller holds `result.read.research` |
| E6 | Verification gate | SR writer | `VerificationFlag` forced `UNVERIFIED`; `VerifyingObserverSequence` absent |
| E7 | Export gate | webhooks, evidence export, any egress | payload carries `clinical_use_mode`; no automated clinical-destination egress exists for research results |
| E8 | Demotion path | daily regulatory check, operator action | one-way demote is always permitted; promote always re-runs E1 |

**MOS-SAFE-036** (E1) — Setting `clinical_use_mode = clinical` on a Deployment MUST be refused unless **all** of the following hold at the moment of the request. The response MUST enumerate every failed condition, not the first.

| Condition | `violations[]` code on failure |
|---|---|
| The `ServiceVersion` signature verifies against `clinical.legal_manufacturer.signing_key_id` | `signature_not_from_declared_manufacturer` |
| `clinical` block present, structurally valid, digest matches the signed manifest | `clinical_block_digest_mismatch` |
| `intended_use.autonomy == "assistive"` | `autonomous_service_refused` |
| A `regulatory_status[]` entry exists for `deployment.jurisdiction` with `status` in the CLEARED set and not expired | `no_cleared_regulatory_status_for_jurisdiction` |
| `clinical_evidence.validation_report_id` resolves to a `ValidationReport` whose signature verifies and whose digest matches `clinical_evidence.validation_report_digest` | `validation_report_unverifiable` |
| That report's `AcceptanceCriteria` outcome is `met` for every criterion of the Capability (Ch. 7) | `acceptance_criteria_not_met` |
| The Deployment is **production-serving** as defined in MOS-SAFE-046 (Chapter 6 §6.8 supplies `environment`, `state` and `role`) | `deployment_not_production_serving` |
| `operating_point` is non-null when any declared finding is probabilistic | `operating_point_undeclared` |
| The request carries `approved_by` (a `User` id holding `deployment.approve_clinical`) and `approval_rationale` (≥ 20 chars) | `named_approver_required` |

**MOS-SAFE-037** — A successful E1 transition MUST write an `AuditEvent` with `action = deployment.clinical_use_mode.promoted` carrying the full `violations`-check result set (all passing), `approved_by`, `approval_rationale`, `intended_use_digest`, `validation_report_digest`, and the resolved `regulatory_status` entry. This audit row is the site's record that the gate was passed on evidence, and it MUST be included in the provenance of every subsequent result (`governance.clinical_promotion_audit_id`, §9.9).

**MOS-SAFE-038** — The reverse transition (`clinical` → `research_only`) MUST always be permitted to any principal holding `deployment.update`, MUST require no evidence, and MUST write an `AuditEvent`. Safety transitions are never gated.

**MOS-SAFE-039** (E3) — In `research_only` mode, the platform DICOM writer MUST NOT emit any object that lacks the complete RUO marker set of §9.5. The check MUST run on the assembled dataset immediately before STOW-RS, MUST be implemented as a single function shared by the SEG, SR and SC writers, and MUST fail the Job rather than emit an unmarked object. There is no configuration flag that disables it.

**MOS-SAFE-040** (E4) — Every configured DICOM destination MUST carry `accepts_research: bool` (default `false`). The Gateway MUST reject a STOW-RS whose originating Job has `clinical_use_mode = research_only` and whose destination has `accepts_research = false`, with HTTP 403 and `reason = research_object_to_clinical_destination`, and MUST write an `AuditEvent`. A deployment that has not configured a research destination cannot write research objects anywhere — which is the correct failure, and it MUST be reported at Deployment creation as a warning event `deployment.no_research_destination`, not discovered at STOW time.

**MOS-SAFE-041** (E5) — `GET /api/v1/results` and every UI/viewer feed derived from it MUST default to `clinical_use_mode = clinical` results only. Including research results MUST require both the explicit query parameter `include_research=true` and the permission `result.read.research`. When research results are included, the API response MUST carry `clinical_use_mode` on every item and the UI MUST render the RUO badge (MOS-SAFE-012).

**MOS-SAFE-042** (E6) — In `research_only` mode, SR objects MUST be written with `VerificationFlag` (0040,A493) = `UNVERIFIED` and MUST NOT carry `VerifyingObserverSequence` (0040,A073). No `ResultReview` outcome may change this (§9.6).

**MOS-SAFE-043** (E7) — Webhook payloads and evidence exports MUST carry `clinical_use_mode`. No automated path from a research result to a RIS, EHR, HL7 interface or paging system exists in 0.1–0.4; adding one requires a new DENY-table decision (§9.7).

**MOS-SAFE-044** — A tenant MAY hold `clinical` and `research_only` Deployments of the same `ServiceVersion` simultaneously (typically a shadow research deployment beside a clinical one). Results MUST be distinguishable by `deployment_id` and by the DICOM marker set alone, without consulting the database.

**MOS-SAFE-045** — The platform MUST expose `GET /api/v1/deployments/{id}/clinical-gate` returning the E1 condition set with pass/fail per condition, computed live, whether or not the Deployment is currently clinical. A site must be able to see exactly what is missing before it asks for a promotion.

**MOS-SAFE-046** — Three Chapter 6 facts MUST be snapshotted onto the `Result` at execution and carried in provenance: `deployment_environment` (∈ `dev`, `staging`, `production`), `deployment_state_at_execution` (∈ `PENDING`, `VERIFYING`, `SERVING`, `SUSPENDED`, `DRAINING`, `RETIRED`) and `deployment_role_at_execution` (∈ `ACTIVE`, `CANARY`, `SHADOW`, `STANDBY`), alongside `deployment_id`. **Production-serving** is defined once, here, as `deployment_environment = production AND deployment_state_at_execution = SERVING AND deployment_role_at_execution ∈ {ACTIVE, CANARY}`. Combined with E5 and policy P-014 (Chapter 8), only a Deployment that is both production-serving and `clinical` can feed the clinical read path. A single conflated `deployment_state` field MUST NOT be used: a suspended production deployment and a shadow production deployment both pass an environment-only test. The three facts have two sources and the runner MUST use exactly these: `deployment_role_at_execution` is taken from the job's pinned resolution record (`job.resolution.selected.deployment_role`, Chapter 6 `MOS-REG-066`), and `deployment_environment` and `deployment_state_at_execution` are read from the `deployments` row named by the pinned `deployment_id` at the moment execution starts (Chapter 6 §6.8). `environment` is part of the deployment slot key and immutable, so a live read cannot diverge from the pin — if Chapter 6's pin record carries it, the two values MUST be identical and a divergence MUST fail the job; `state` is deliberately the live value, because a deployment suspended after job creation MUST NOT be recorded as `SERVING`. Reading the row the pin names is not re-resolution: a runner MUST NOT call `Resolve` again (`MOS-REG-067`).

### 9.5. Marking AI-derived objects

**MOS-SAFE-047** — Every DICOM object the platform generates MUST be identifiable as AI-derived, and its `clinical_use_mode` determinable, **from metadata alone** — that is, from a QIDO-RS response, without retrieving pixel data and without access to MedicalOS. A third-party viewer, a PACS administrator and a downstream archive must all be able to tell.

**MOS-SAFE-048** — The **base marker set** MUST be present on every generated object in both modes. Attribute writing mechanics, UID derivation and the equipment identity source are defined in Chapter 4 (MOS-IMG); this table states the safety-normative values.

| Tag | Name | VR | Required value |
|---|---|---|---|
| (0008,103E) | SeriesDescription | LO | MUST begin with `AI ` in `clinical` mode, `AI RUO ` in `research_only` mode, followed by a human-readable label (e.g. `AI Pleural Effusion SEG`) |
| (0008,0070) | Manufacturer | LO | `clinical.legal_manufacturer.name`, verbatim |
| (0008,1090) | ManufacturerModelName | LO | `service_id` |
| (0018,1000) | DeviceSerialNumber | LO | `deployment_id` |
| (0018,1020) | SoftwareVersions | LO | `svc=<service_version>/model=<model_version>/pre=<preprocessing_version>` |
| (0018,A001) | ContributingEquipmentSequence | SQ | one item; `PurposeOfReferenceCodeSequence` = (109102, DCM, "Processing Equipment"); item repeats Manufacturer / ManufacturerModelName / DeviceSerialNumber / SoftwareVersions; `ContributionDescription` = `MedicalOS <platform_version> generated this object from a machine learning service output.` |
| (0008,2111) | DerivationDescription | ST | `AI-derived by <service_id> <service_version>. Machine estimate, not a diagnosis.` |
| (0020,0011) | SeriesNumber | IS | ≥ 9000 (Chapter 4 owns allocation) |

**MOS-SAFE-049** — Per-object-type markers MUST additionally be written:

| Object | Tag / concept | Required value |
|---|---|---|
| SEG | (0062,0008) SegmentAlgorithmType | `AUTOMATIC` |
| SEG | (0062,0009) SegmentAlgorithmName | `<service_id>:<model_version>` |
| SEG | (0070,0080) ContentLabel | `AI_SEG` (clinical) / `AI_RUO_SEG` (research) |
| SEG | (0070,0081) ContentDescription | `AI-derived segmentation` (clinical) / `AI-derived segmentation - RESEARCH USE ONLY` (research) |
| SEG | (0070,0084) ContentCreatorName | `<legal_manufacturer.name>` in PN form |
| SR | root `ConceptNameCodeSequence` | the TID 1500 document title required by Chapter 4, with `ObservationSubjectContext` unchanged |
| SR | Device Observer items (TID 1002) | (121012) Device Observer UID = deterministic device UID for `(service_id, service_version)`; (121013) Manufacturer; (121014) Model Name = `service_id`; (121015) Serial Number = `deployment_id`; (121016) Physical Location = `MedicalOS <deployment_id>` |
| SR | first content item under root, TEXT, concept (121106, DCM, "Comment") | `AI-derived result produced by <service_id> <service_version> (<legal_manufacturer.name>). Machine estimate; not a diagnosis. Review by a qualified reader is required.` |
| SR | (0040,A491) CompletionFlag | `COMPLETE` when the Job reached `COMPLETED`, otherwise no SR is written |
| SR | (0040,A493) VerificationFlag | `UNVERIFIED` at issuance in every mode (§9.6 governs any later verified revision) |
| SC | (0008,0060) Modality | `OT` |
| SC | (0008,0064) ConversionType | `SYN` |
| SC | (0028,0301) BurnedInAnnotation | `YES` |
| SC | rendered banner | see MOS-SAFE-051 |

**MOS-SAFE-050** — The **RUO marker set** MUST additionally be written when `clinical_use_mode = research_only`:

| Object | Marker |
|---|---|
| all | `SeriesDescription` prefix `AI RUO ` (MOS-SAFE-048) |
| all | `DerivationDescription` gains the suffix ` RESEARCH USE ONLY - NOT FOR CLINICAL DECISION MAKING.` |
| SEG | `ContentLabel` = `AI_RUO_SEG`, `ContentDescription` as in MOS-SAFE-049 |
| SR | a second TEXT content item under root, concept (121106, DCM, "Comment"), value `RESEARCH USE ONLY. NOT FOR CLINICAL DECISION MAKING. This document must not be used to inform patient management.` |
| SC | burned-in banner (MOS-SAFE-051) |

**MOS-SAFE-051** — An SC frame produced in `research_only` mode MUST carry a burned-in banner occupying the top 40 rows of the frame: opaque background, the text `RESEARCH USE ONLY - NOT FOR CLINICAL USE` rendered at a font size of at least 3% of frame height, plus a second line `<service_id> <service_version>`. The banner is the only channel by which the warning survives a screenshot, a PDF export or a viewer that ignores metadata. `BurnedInAnnotation` = `YES` is therefore literally true and MUST be set.

**MOS-SAFE-052** — The platform MUST NOT modify any attribute of a source object, MUST NOT write into the source Series, and MUST NOT mint a new `StudyInstanceUID` (Chapter 4). AI-derived marking is applied exclusively to newly minted Series and SOP Instances.

**MOS-SAFE-053** — The `Result` JSON returned by the API MUST carry `derivation: "ai_derived"`, `clinical_use_mode`, `service_id`, `service_version`, `legal_manufacturer_name` and `review_status` at the top level, not nested in a metadata bag. Client code that renders a finding must not be able to omit the marking by accident.

**MOS-SAFE-054** — Measurements delivered outside DICOM (API, SDK, agent tool output) MUST each carry `{value, unit, concept, derivation: "ai_derived", score_threshold, service_version}`, where `concept` is the coded concept of Chapter 2 §2.8.2 — `{scheme, code, display, scheme_uri}` — and `unit` is that same coded-concept object carrying `scheme: "UCUM"` (Chapter 2 `MOS-SVC-091`); `unit` is never a bare string. A bare number is never a valid measurement anywhere in MedicalOS. The member names are those of `ResultBundle` (Chapter 2 §2.8.2/§2.8.5); this requirement adds fields, it does not rename them. `score_threshold` is Chapter 2 `MOS-SVC-020`'s member name for the operating-point threshold, and this projection MUST NOT respell it `threshold`, `value` or `operating_threshold`.

**MOS-SAFE-055** — Marking is decided at write time from the Job's pinned mode and is never retroactively rewritten. Already-stored objects MUST NOT be re-marked, re-STOWed or deleted when a Deployment's mode changes; supersession (Chapter 4/5) is the only forward path.

**MOS-SAFE-056** — CI MUST include a marker-set assertion that parses every object produced by the golden-path test with pydicom, checks every row of MOS-SAFE-048/049/050 for the applicable mode, and fails on any absence. See acceptance check 6.

### 9.6. `ResultReview`

#### 9.6.1. The two meanings of human-in-the-loop, and the default

The previous specification said "Agent → recommendation → human approval → execution" and was ambiguous between two products:

- **Interpretation A — no autonomous clinical actions.** Nothing the platform produces is an executed medical decision; results are artefacts presented to a clinician. This is already satisfied by the DENY table (§9.7) and by MOS-SAFE-011.
- **Interpretation B — no unreviewed result is visible.** A result is withheld from the reading environment until a qualified human approves it. Radiology AI normally ships the opposite way: results pre-populate so the radiologist meets them during the primary read. Under B, the radiologist would have to review the result before being allowed to see it, which is incoherent for a concurrent-read product and merely expensive for a triage product.

**MOS-SAFE-057** — MedicalOS 0.2 adopts **Interpretation A with recorded review**: results are stored, marked (§9.5), visible according to §9.4 E5, and **nothing is auto-actioned**. `ResultReview` records what a human concluded; it is never a precondition for storage or for visibility. Interpretation B is expressible as `review_mode: mandatory_pre_publication` and is **RESERVED and MUST be refused** in 0.1–0.4 (MOS-SAFE-061). This is a settled default over a genuinely open product question; Chapter 16 carries the question (OQ-01).

#### 9.6.2. Entity, states and lifecycle

**MOS-SAFE-058** — `ResultReview` is a first-class entity with these fields. Chapter 12 renders it as a table; the field contract is normative here.

| Field | Type | Notes |
|---|---|---|
| `result_review_id` | id | ULID, prefix `rrv_` |
| `tenant_id` | id | RLS-scoped (Ch. 8) |
| `result_id` | id | FK to `Result`; `UNIQUE (result_id, round)` |
| `round` | int | starts at 1; incremented by reopen (MOS-SAFE-066) |
| `state` | enum | `PENDING` \| `IN_REVIEW` \| `ACCEPTED` \| `MODIFIED` \| `REJECTED` \| `EXPIRED` \| `SUPERSEDED` |
| `assignee_user_id` | id \| null | optional assignment |
| `reviewer_user_id` | id \| null | set on claim, immutable after submit |
| `reviewer_role` | text \| null | the `key` of the `Role` the reviewer acted under, captured as a snapshot at submit; plain `text`, never a foreign key to `roles` and never a closed enum of job titles (Ch. 12 §12.6; Ch. 8 `MOS-SEC-038`) |
| `reviewer_class` | enum \| null | `clinical` \| `non_clinical`; derived at submit by MOS-SAFE-068, stored on the row, immutable thereafter |
| `claimed_at` / `submitted_at` | timestamptz \| null | |
| `review_due_at` | timestamptz \| null | from `Deployment.review_sla_hours` |
| `action_rationale` | text \| null | REQUIRED, ≥ 20 chars, for `MODIFIED` and `REJECTED` |
| `modifications` | jsonb \| null | RFC 6902 patch against the `Result.findings` document; REQUIRED for `MODIFIED` |
| `rejection_reason` | enum \| null | `false_positive` \| `false_negative` \| `wrong_laterality` \| `mis_segmentation` \| `measurement_implausible` \| `wrong_series_analysed` \| `out_of_intended_use` \| `other`; REQUIRED for `REJECTED` |
| `known_failure_mode_id` | string \| null | an `id` from `clinical.known_failure_modes` when the reviewer attributes the error to a declared mode |
| `created_at` / `updated_at` | timestamptz | |

**MOS-SAFE-059** — The state machine is exactly:

| From | To | Trigger | Permission |
|---|---|---|---|
| — | `PENDING` | platform, on `Result` creation when `Deployment.review_mode != off` | platform |
| `PENDING` | `IN_REVIEW` | `POST .../claim` | `result.review.submit` |
| `PENDING` | `EXPIRED` | `review_due_at` elapsed | platform |
| `PENDING` | `SUPERSEDED` | the `Result` is superseded | platform |
| `IN_REVIEW` | `ACCEPTED` \| `MODIFIED` \| `REJECTED` | `POST .../submit` | `result.review.submit` |
| `IN_REVIEW` | `PENDING` | `POST .../release` or claim lease expiry (60 min) | `result.review.submit` |
| `IN_REVIEW` | `SUPERSEDED` | the `Result` is superseded | platform |
| `ACCEPTED` \| `MODIFIED` \| `REJECTED` \| `EXPIRED` | new row, `round + 1`, `PENDING` | `POST .../reopen` | `result.review.reopen` |

`ACCEPTED`, `MODIFIED`, `REJECTED`, `EXPIRED` and `SUPERSEDED` are terminal for that row; a review is corrected by a new round, never by mutating a submitted row. **MOS-SAFE-060** — Review rows MUST be append-only at the database role level: the application role MUST NOT hold `UPDATE` on terminal rows or `DELETE` on any row (Ch. 8 owns the grant).

**MOS-SAFE-061** — `Deployment.review_mode` ∈ {`off`, `optional`, `mandatory_post_publication`, `mandatory_pre_publication`}. Default `optional`. `mandatory_post_publication` creates the `PENDING` row, sets `review_due_at`, and drives the reminder/expiry machinery — it does **not** delay storage or visibility. `mandatory_pre_publication` MUST be refused at Deployment creation in 0.1–0.4 with `class: not_implemented`, `detail: "pre-publication review gating is reserved; see Chapter 16"`.

**MOS-SAFE-062** — `Result.review_status` is a derived, denormalised projection of the highest-round `ResultReview` row (`UNREVIEWED` when no row exists), maintained in the same transaction as the review transition. It MUST NOT be writable through any API.

**MOS-SAFE-063** — The **only** permitted side effects of any `ResultReview` transition are: an `AuditEvent`, a webhook delivery, a metric increment, and — for `ACCEPTED`/`MODIFIED` in `clinical` mode when `Deployment.emit_verified_sr_on_accept` is `true` (default `false`) — issuance of a **new SR revision** per MOS-SAFE-065. No transition may enqueue a Job, alter a stored DICOM object, notify a patient, write to any external clinical system, retrain anything, or change a Deployment. Chapter 14 MUST include a test asserting that a full review cycle produces zero rows in `jobs` and zero STOW-RS calls when `emit_verified_sr_on_accept` is false.

**MOS-SAFE-064** — A `REJECTED` review MUST NOT delete or hide the `Result`. It MUST set `Result.review_status = REJECTED`, which MUST be rendered by every surface in MOS-SAFE-012 and MUST be returned by the API. The reviewer's disagreement is data, not an erasure.

**MOS-SAFE-065** — When `emit_verified_sr_on_accept` is enabled and a review terminates `ACCEPTED` or `MODIFIED` in `clinical` mode, the platform MAY write one new SR instance that: derives its identity by `derive_uid` (Chapter 4, `MOS-IMG-062`) with `uid_kind = 'sr.instance'` and the `output_index` that `MOS-IMG-065` allocates for review round *n*, reusing the superseded SR's `series_index`, so the revision is a new instance inside that SR's existing series and MUST NOT mint a new `SeriesInstanceUID` and MUST NOT collide with the superseded instance — this chapter adds no argument to `MOS-IMG-062`; sets `VerificationFlag` = `VERIFIED`; carries `VerifyingObserverSequence` with the reviewer's name, the institution, and `VerificationDateTime`; references the superseded SR via `PredecessorDocumentsSequence`; and, for `MODIFIED`, contains the patched measurement values with the original values retained under a (121106, DCM, "Comment") item. In `research_only` mode this is forbidden (MOS-SAFE-042). The original SR MUST NOT be deleted or overwritten.

**MOS-SAFE-066** — `POST .../reopen` opens a new round and MUST require `action_rationale`. Reopening does not invalidate a previously issued verified SR; a corrected conclusion produces another SR revision.

#### 9.6.3. Permissions, endpoints and events

**MOS-SAFE-067** — These permission names are normative; Chapter 8 owns the role→permission mapping:

| Permission | Grants |
|---|---|
| `result.review.read` | read review rows and history |
| `result.review.submit` | claim, release, submit a review |
| `result.review.assign` | set `assignee_user_id` |
| `result.review.reopen` | open a new round on a terminal review |
| `result.read.research` | see `research_only` results in list endpoints (§9.4 E5) |
| `deployment.approve_clinical` | be the named approver in the E1 gate (MOS-SAFE-036) |

**MOS-SAFE-068** — `reviewer_class` MUST be derived at submit, stored on the row, and MUST NOT be inferred from the spelling of `reviewer_role`. `reviewer_class = clinical` if and only if **both** hold: the submitting principal is a `User` (not a `ServiceAccount`, not an `ApiKey`), **and** the `Role` named by `reviewer_role` is one the principal currently holds and carries `clinically_qualified = true` (MOS-SAFE-104). Every other case — a `ServiceAccount`, an `ApiKey`, a role the principal does not hold, a role whose `clinically_qualified` is `false` or absent, or a role since deleted — MUST be recorded `reviewer_class = non_clinical`. Only `reviewer_class = clinical` MAY satisfy MOS-SAFE-065. Machine review is not human review; the platform MUST refuse to let a service account produce a `VERIFIED` SR under any configuration.

**MOS-SAFE-104** — `Role` MUST carry a boolean attribute `clinically_qualified`, tenant-set, defaulting to `false`, alongside the members of Chapter 8 `MOS-SEC-038`. It is the only input by which the platform may decide that a review counts as a qualified human read. The platform MUST NOT derive that decision from the role `key`, the `display_name`, a job-title string, or a hardcoded list of keys: `MOS-SEC-038` forbids hardcoded clinical job titles, and a value set such as `RADIOLOGIST`/`CLINICIAN`/`RESEARCHER` MUST NOT appear anywhere as an enum, a database CHECK or a lookup. The seeded role `clinical_reviewer` (Chapter 8 §8.3.4) MUST be seeded `clinically_qualified = true` and every other seeded role `false`. Changing the attribute MUST write an `AuditEvent` and MUST NOT alter any already-submitted `ResultReview`, whose `reviewer_class` is a snapshot.

**MOS-SAFE-069** — Endpoints (envelope, pagination, error model and auth per Chapter 10). Every row below MUST also appear in Chapter 10 table 10.2-B and in ~~`medos/api/v1/routes.yaml`~~ the registry of the product that owns it (`MOS-API-085`, **AMENDED at specification 0.4.0**); Chapter 10 owns the route registry, this chapter owns the state machine behind it:

| Method + path | Body / params | Success | Notes |
|---|---|---|---|
| `GET /api/v1/result-reviews` | `result_id`, `state`, `assignee_user_id`, `overdue=true`, cursor params | 200 list | tenant-scoped |
| `GET /api/v1/result-reviews/{id}` | — | 200 | includes all rounds via `rounds[]` |
| `POST /api/v1/result-reviews/{id}/claim` | `{}` | 200 | 409 `already_claimed` if `reviewer_user_id` set by another principal |
| `POST /api/v1/result-reviews/{id}/release` | `{}` | 200 | only by the current claimant |
| `POST /api/v1/result-reviews/{id}/submit` | `{action, action_rationale?, modifications?, rejection_reason?, known_failure_mode_id?, reviewer_role}` | 200 | 422 on missing conditional fields |
| `POST /api/v1/result-reviews/{id}/assign` | `{assignee_user_id}` | 200 | |
| `POST /api/v1/result-reviews/{id}/reopen` | `{action_rationale}` | 201 + new round | |
| `GET /api/v1/results/{result_id}/review` | — | 200 | convenience projection |

```json
// POST /api/v1/result-reviews/rrv_01J9P5X2K4MNQ8RSTVWXYZ/submit
{
  "action": "MODIFIED",
  "reviewer_role": "clinical_reviewer",
  "action_rationale": "Right-sided volume includes subdiaphragmatic ascites; corrected to 410 ml after manual exclusion of the infradiaphragmatic component.",
  "known_failure_mode_id": "FM-1",
  "modifications": [
    {"op": "replace", "path": "/findings/0/measurements/1/value", "value": 410},
    {"op": "add", "path": "/findings/0/qualifiers/-", "value": "reader_corrected"}
  ]
}
```

**MOS-SAFE-070** — Every transition MUST write an `AuditEvent` (`action` = the event name below, `resource` = `result_review_id`, carrying `result_id`, `job_id`, `service_version`, `reviewer_user_id`, `reviewer_role`) and MUST be delivered as an HMAC-signed webhook (Chapter 10) with these event type names: `result.review.created`, `result.review.claimed`, `result.review.released`, `result.review.submitted`, `result.review.expired`, `result.review.reopened`, `result.review.superseded`. No new event-bus topic is created; these are audit and webhook events only (spine §5).

**MOS-SAFE-071** — `ResultReview` outcomes are the only production ground-truth signal the platform obtains for free. The platform MUST expose aggregate review outcome rates per `(service_id, service_version, tenant_id, month)` — `accept_rate`, `modify_rate`, `reject_rate`, `rejection_reason` histogram, `known_failure_mode_id` histogram — via `GET /api/v1/service-versions/{id}/review-statistics`. **MOS-SAFE-072** — This aggregate MUST NOT be presented as a performance metric, MUST NOT be written into a `ValidationReport`, and MUST carry the label "reviewer agreement, not a validated performance estimate"; it is an unblinded, non-consecutive, indication-biased sample. It is a monitoring signal (Ch. 7 continuous site monitoring), nothing more.

### 9.7. The default DENY table

**MOS-SAFE-073** — This table is the platform's clinical-action posture. It is normative, default-deny, and each row names the mechanism that enforces it. "Subject" is the class of actor: `service` (a running `ServiceVersion` container), `agent` (the 0.4+ agentic layer), `platform` (first-party platform code), `human` (an authenticated `User`).

| # | Action | Subject | Default | Enforced by | Override path |
|---|---|---|---|---|---|
| 1 | Read DICOM within the tenant's scope | service, agent, platform, human | **ALLOW** | Gateway tenancy filter + `study.read` (Ch. 3/8) | — |
| 2 | Execute a deployed `ModelVersion` | service, platform | **ALLOW** | capability resolution (Ch. 6) | — |
| 3 | Produce a `ResultBundle` | service | **ALLOW** | service contract (Ch. 2) | — |
| 4 | Write a DICOM SEG/SR/SC as a **new** Series under **newly minted** SOP Instance UIDs into the source Study | **platform only** | **ALLOW** | Gateway destination policy + spine §2 (services may not write DICOM) | none for service/agent |
| 5 | Alter identity or content of an **existing** Patient/Study/Series/Instance, or STOW a SOP Instance UID that already exists with different content | any | **DENY (hard)** | Gateway STOW guard: reject when the target UID exists with a different content digest | none in 0.1–0.4 |
| 6 | Delete a Study/Series/Instance from the PACS | any | **DENY (hard)** | no DELETE route exists on the Gateway; PACS credential is Gateway-only | none |
| 7 | Delete a `Result`, `ValidationReport`, `AuditEvent`, `PolicyDecision` or provenance record | any | **DENY (hard)** | DB role lacks DELETE/UPDATE on those tables (Ch. 8) | supersession only |
| 8 | Tombstone an ingested study from a tenant's imaging projection (bad dev ingest) | human | **ALLOW** with `study.retract`, TENANT_ADMIN only | tombstone flag, never a hard delete; audited | — |
| 9 | Seal-break or mutate a `DatasetVersion` | any | **DENY (hard)** | content-addressed manifest (Ch. 7) | new version |
| 10 | Prescribe, order medication, order a procedure | any | **DENY (hard)** | no such Tool exists in the catalogue; adding one requires a new row here and a release decision | none |
| 11 | Write to an EHR/RIS/HL7 interface or place an order | any | **DENY (hard)** | no egress client exists; egress network policy denies (Ch. 8/13) | none |
| 12 | Send a notification that asserts a finding to a clinician (page, escalate, SMS) | any | **DENY** | no notification transport is wired to results in 0.1–0.4 | RESERVED |
| 13 | Emit an SR with `VerificationFlag = VERIFIED` without a human `ResultReview` | any | **DENY (hard)** | MOS-SAFE-065/068 in the SR writer | none |
| 14 | Take any automated action on a `ResultReview` outcome | any | **DENY** | MOS-SAFE-063 side-effect allowlist | `emit_verified_sr_on_accept` only |
| 15 | Export PHI outside the tenant boundary | any | **DENY** | Ch. 8 export controls; de-identified evidence export only | site-configured, audited |
| 16 | Send DICOM-sourced text or pixels to an external LLM | agent, platform | **DENY** | per-tenant `external_llm_allowed`, default `false` (spine §8, Ch. 11) | tenant switch + audit |
| 17 | Change an operating threshold, `PreprocessingSpec` or weights of a deployed version in place | any | **DENY (hard)** | signature covers the artifact; change ⇒ new `ServiceVersion` (Ch. 6) | new version + gate |
| 18 | Promote a Deployment to `clinical_use_mode = clinical` | human | **DENY** until E1 passes | MOS-SAFE-036 | named approver + evidence |
| 19 | Run a `ServiceVersion` declaring `autonomy: autonomous` | any | **DENY (hard)** | MOS-SAFE-011 at deployment | none |
| 20 | A service opening a network connection to PACS, Postgres, the broker or object storage | service | **DENY (hard)** | egress network policy + no credentials issued (Ch. 2/8) | none |
| 21 | Cancel a running inference | any | **RESERVED** | not implemented before 0.3 (spine §4) | — |
| 22 | Bypass the policy decision point | agent, service | **DENY (hard)** | PDP is in-path and fail-closed (Ch. 8) | none |

**MOS-SAFE-074** — Rows marked **DENY (hard)** MUST NOT be made overridable by configuration, role, tenant setting, feature flag or environment variable. Adding an override to a hard row is a specification change requiring a new row in this table and an entry in the release notes.

**MOS-SAFE-075** — Row 5 settles an ambiguity the previous specification left open: **"modify patient" is a data-identity rule, not a write-anywhere rule.** Creating new SOP Instances under newly minted Series UIDs inside the patient's existing Study modifies nothing and is the platform's normal, permitted output path (row 4). Altering an existing object, or re-using an existing SOP Instance UID with different content, is forbidden absolutely. Implementers MUST NOT gate the output path on an exception to row 5.

**MOS-SAFE-076** — Every DENY decision taken at runtime MUST produce a `PolicyDecision` row and an `AuditEvent` carrying the row number of this table, the subject, the resource and the job context. A denial that leaves no trace is indistinguishable from a bug.

**MOS-SAFE-077** — A default-deny row for an action the system cannot yet perform is deliberate and MUST be preserved. It converts "we added an EHR write path" from an incremental feature into a visible, dated decision.

### 9.8. Agent safety rules: enforced invariants versus prose

**MOS-SAFE-078** — Safety rules MUST be published in two explicitly labelled classes. A rule in the **ENFORCED** class MUST name a component, a check and a failure mode. A rule in the **GUIDANCE** class MUST be labelled as guidance and MUST NOT be cited as a control in any document, gate, report or claim. Mixing them is how a README paragraph gets mistaken for a safeguard.

**MOS-SAFE-079** — The ten legacy agent safety rules are classified as follows. Mechanisms in Chapters 2, 8 and 11 are referenced, not redefined.

| # | Rule | Class | Enforcement point and check | Failure behaviour |
|---|---|---|---|---|
| 1 | Never invent DICOM data | **ENFORCED** | No agent or service holds a DICOM write path (DENY row 4/20); all pixel and header access is through Gateway tools whose outputs are the only source set for the narrative post-condition (rule 2) | tool call denied; `PolicyDecision` DENY |
| 2 | Never fabricate model results | **ENFORCED** | Content-safety post-condition in the narrative-producing tool (Ch. 11): extract every numeric literal, laterality term (`left`/`right`/`bilateral` and configured synonyms) and finding label from generated text and assert set-membership in the source `Result` — the finding-label token set is every `capability_concepts.code_meaning` for the concepts in that `Result` plus that row's registered `narrative_synonyms[]` (Ch. 12 §12.9.1, vocabulary owned by Ch. 6); this chapter holds no code dictionary of its own and MUST NOT be cited as one; `hallucination_rate` is the unmatched-token rate | fail closed — the narrative is discarded, never emitted; step fails |
| 3 | Never claim a tool succeeded if it failed | **ENFORCED** | Tool result envelopes carry `status` and are written by the runtime, not the agent; outputs of failed calls are excluded from the rule-2 source set, so any sentence asserting a failed step's result fails membership | as rule 2 |
| 4 | Never bypass permissions | **ENFORCED** | PDP is in-path for every tool call, evaluated per call against current role state (never a job-creation snapshot), fail-closed, no circuit breaker (Ch. 8) | call denied; audited |
| 5 | Never directly access infrastructure | **ENFORCED** | Closed domain-verb tool catalogue with no shell/SQL/HTTP/filesystem primitive; egress network policy; no credentials issued to agent or service processes (Ch. 2/8/11) | connection refused; audited |
| 6 | Never modify clinical data without permission | **ENFORCED** | DENY rows 5, 6, 7; Gateway STOW guard | 403; audited |
| 7 | Clearly distinguish prediction from confirmed diagnosis | **ENFORCED** | Three checks: (a) required disclaimer content item (MOS-SAFE-049); (b) `VerificationFlag = UNVERIFIED` at issuance (MOS-SAFE-042/049); (c) assertive-diagnosis lexicon check on generated narrative (MOS-SAFE-080) | narrative rejected; object not emitted |
| 8 | Preserve provenance | **ENFORCED** | The provenance record is written by the platform in the same transaction as the `Result` (MOS-SAFE-081); no agent or service has a write path to it | job fails if the record cannot be written |
| 9 | Return structured output | **ENFORCED** | JSON Schema 2020-12 validation of every tool output against the declared output schema (Ch. 11) | step fails with a schema error |
| 10 | Fail safely | **GUIDANCE**, with two enforced components: fail-closed PDP (rule 4) and fail-closed post-conditions (rules 2, 7) | — | — |
| 11 | Prefer the least-privileged tool; do not request PHI you do not need | **GUIDANCE** | — | — |
| 12 | Say "I do not know" rather than reason past a missing input | **GUIDANCE** | — | — |

**MOS-SAFE-080** — The assertive-diagnosis lexicon check MUST reject generated narrative containing any of the following case-insensitive patterns unless the sentence also contains an attribution token (`AI`, `automated`, `model`, `estimated`, `suggests`, `consistent with`): `diagnosis of`, `diagnosed with`, `confirms`, `confirmed`, `rules out`, `excludes`, `is definitely`, `proves`. The list MUST be configurable per tenant only by **addition**; entries MUST NOT be removable. Rejection MUST fail the step, MUST NOT silently rewrite the text, and MUST be counted in the `safety_policy_violations` metric.

**MOS-SAFE-081** — The MedicalOS documentation set MUST NOT list a GUIDANCE-class rule under any heading containing "control", "safeguard", "mitigation" or "requirement". CI MUST check `docs/SAFETY.md` for the two labelled sections and fail if any rule appears without a class label.

### 9.9. The provenance record

**MOS-SAFE-082** — Exactly one **provenance record** MUST be written per `Result`, in the **same database transaction** as the `Result` row and the terminal `Job` transition (Chapter 5). A `Result` without a provenance record MUST be impossible by foreign-key constraint, not by convention. If the record cannot be written, the Job MUST fail and any already-STOWed objects MUST be recorded for supersession rather than deleted (DENY row 6/7).

**MOS-SAFE-083** — The record MUST contain the following fields. The previous specification's list is a subset of section A/D below; the widened fields — which series were actually consumed, the preprocessing version, the de-identification policy version, the evidence dataset version, and where every output object landed — are what turn a list of version strings into a reproducible record.

**Section A — identity and lineage**

| Field | Type | Source |
|---|---|---|
| `provenance_id` | ULID `prv_` | platform |
| `tenant_id` | id | job |
| `result_id`, `job_id` | id | job |
| `parent_job_id`, `root_job_id`, `job_depth` | id, id, int | §9.10 |
| `idempotency_key` | string | job (Chapter 5 `MOS-EXEC-053`; format `^ik_[a-z2-7]{26}$`) |
| `sequence_no` | bigint | per-tenant monotonic (MOS-SAFE-090) |
| `prev_record_hash`, `record_hash` | sha256 hex | MOS-SAFE-090 |
| `record_schema_version` | string | e.g. `provenance/1.0.0` |
| `started_at`, `finished_at`, `duration_ms` | timestamptz, int | job |

**Section B — input (what was actually consumed)**

| Field | Type | Meaning |
|---|---|---|
| `input.patient_internal_id` | id | tenant-scoped internal id, never a raw MRN |
| `input.study_internal_id`, `input.study_instance_uid` | id, UID | the tenant's study identity |
| `input.series_considered[]` | object[] | `{series_instance_uid, series_number, selected: bool, rejection_reason}` for **every** series triaged — the audit trail of B4-class errors |
| `input.series_consumed[]` | UID[] | the series actually read by the service; MUST be a subset of `series_considered[].selected == true` |
| `input.instances` | object | `{count, sha256_of_sorted_sop_uid_list, manifest_uri}`; the full UID list lives in `result_provenance.input_instance_uids` (Ch. 12 §12.11) and is reachable from `manifest_uri` |
| `input.pixel_digest` | sha256 | digest of the canonical volume tensor bytes concatenated with its geometry header |
| `input.canonical_geometry` | object | `{origin, spacing, direction, shape, frame_of_reference_uid}` per Chapter 4 |
| `input.acquisition` | object | `{modality, body_part, kernel, slice_thickness_mm, kvp, contrast_phase, manufacturer, model}` — the envelope evidence |
| `input.gateway` | object | `{deid_profile, deid_policy_version, uid_map_id, uid_remap_applied: bool, burned_in_phi_check: pass\|fail\|skipped, gateway_version}` |

**Section C — resolution and governance**

| Field | Type | Meaning |
|---|---|---|
| `resolution.capability_id`, `resolution.capability_version` | string | Chapter 6 |
| `resolution.registry_snapshot_id` | id | the snapshot the pure resolve() ran against |
| `resolution.candidates[]` | object[] | `{service_id, service_version, rank, excluded_reason}` — the full ordered list, not just the winner |
| `resolution.reason` | enum | `job_pin` \| `tenant_pin` \| `deployment_state` \| `declared_metric` \| `semver` |
| `governance.deployment_id`, `governance.deployment_environment`, `governance.deployment_state_at_execution`, `governance.deployment_role_at_execution` | id, enum, enum, enum | MOS-SAFE-046; value spaces are Chapter 6 §6.8 |
| `governance.clinical_use_mode` | enum | pinned at job creation |
| `governance.jurisdiction` | string | `Deployment.jurisdiction` |
| `governance.regulatory_status_at_execution` | object | the resolved `regulatory_status[]` entry, copied verbatim |
| `governance.legal_manufacturer` | object | copied verbatim |
| `governance.intended_use_digest` | sha256 | MOS-SAFE-021 |
| `governance.clinical_promotion_audit_id` | id \| null | MOS-SAFE-037 |
| `governance.policy_decision_ids[]` | id[] | every PDP decision taken during the job |
| `governance.review` | object | `{result_review_id, review_status_at_write}` |

**Section D — execution**

| Field | Type | Meaning |
|---|---|---|
| `execution.service_id`, `execution.service_version`, `execution.service_image_digest` | string, string, digest | |
| `execution.execution_mode` | enum | `native` \| `sealed` |
| `execution.models[]` | array | `[{role, model_id, version, artifact_digest}]`, copied verbatim from `ResultBundle.diagnostics.model_versions[]` (Chapter 2 `MOS-SVC-098`); a single-model service emits one element |
| `execution.preprocessing_specs[]` | array | `[{id, version, digest}]`, copied verbatim from `ResultBundle.diagnostics.preprocessing_specs[]` — the field whose absence made the old record non-reproducible |
| `execution.preprocessing_selftest` | object | `{golden_fixture_id, expected_hash, observed_hash, match: true}` (spine §7) |
| `execution.operating_threshold`, `execution.threshold_source` | number, id | the pinned `score_threshold` value (§9.2, Chapter 2 `MOS-SVC-020`) and the `AcceptanceCriteria` it came from. The provenance member keeps the spelling `operating_threshold` because Chapter 12 §12.11 declares the column `result_provenance.operating_threshold`; it carries the same number the manifest calls `score_threshold`, and this is the one place the two spellings are mapped |
| `execution.inference` | object | `{engine, backend, engine_build_digest}` |
| `execution.accelerator` | object | `{gpu_model, driver, cuda, trt}`, copied verbatim from `ResultBundle.diagnostics.accelerator` (Chapter 2 `MOS-SVC-098`); `cudnn` MAY be present |
| `execution.runtime` | object | `{platform_version, worker_version, sdk_version, container_image_digests[]}` |
| `execution.applicability` | object | `{in_envelope: bool, violations[]}` |
| `execution.plausibility` | object | `{passed: bool, flags[]}` |
| `execution.reproducibility_class` | enum | `bitwise` \| `numeric_tolerance` \| `not_reproducible`, declared by the `ModelVersion` |

**Section E — evidence**

| Field | Type | Meaning |
|---|---|---|
| `evidence.validation_report_id`, `evidence.validation_report_digest` | id, sha256 | the signed report backing this version |
| `evidence.evaluation_run_id` | id | the run that produced the declared metrics |
| `evidence.dataset_version_id`, `evidence.dataset_version_digest` | id, sha256 | **the dataset version of the evidence** |
| `evidence.dataset_split_id`, `evidence.annotation_set_id` | id | Chapter 7 |
| `evidence.acceptance_criteria_id`, `evidence.acceptance_criteria_version` | id, string | the clinical bar in force |
| `evidence.training_population_declared` | bool | MOS-SAFE-016 |

**Section F — output (where every object landed)**

| Field | Type | Meaning |
|---|---|---|
| `outputs[]` | object[] | one entry per generated object |
| `outputs[].output_index` | int | the index used in deterministic UID derivation (Ch. 4) |
| `outputs[].kind` | enum | `SEG` \| `SR` \| `SC` \| `MEASUREMENT` \| `LABELMAP` |
| `outputs[].sop_class_uid`, `outputs[].series_instance_uid`, `outputs[].sop_instance_uids[]` | | minted identity |
| `outputs[].destination` | object | `{destination_id, stow_endpoint, accepts_research, stow_http_status, qido_verified_at, instance_count_verified}` |
| `outputs[].object_store_uri`, `outputs[].sha256`, `outputs[].size_bytes` | | the byte-level landing place |
| `outputs[].marking` | object | `{ai_derived: true, ruo: bool, series_description, verification_flag}` |
| `outputs[].supersedes`, `outputs[].superseded_by` | UID \| null | forward-only supersession |

**MOS-SAFE-084** — `input.series_considered[]` MUST include rejected series with their reasons. The most common silent clinical failure in radiology AI is analysing the wrong reconstruction; the record must show what else was on the table.

**MOS-SAFE-085** — `outputs[].destination.qido_verified_at` MUST be set only after a QIDO-RS query against the minted `SeriesInstanceUID` returned the expected instance count. A STOW-RS 200 is not proof of landing.

**MOS-SAFE-086** — The provenance record MUST NOT contain PHI beyond the tenant-scoped identifiers listed: no `PatientName`, no `PatientBirthDate`, no free-text clinical history, no `StudyDescription` copied from source, no accession number. `input.patient_internal_id` is an internal id. This record is exported (MOS-SAFE-089) and must be safe to hand a reviewer.

**MOS-SAFE-087** — `GET /api/v1/results/{result_id}/provenance` MUST return the record as JSON, authorised by `result.read`. `GET /api/v1/results/{result_id}/provenance?tree=true` MUST return the record plus the records of every descendant job (§9.10), ordered by `job_depth` then `started_at`.

**MOS-SAFE-088** — **AMENDED at specification 0.4.0.** ~~The OHIF provenance panel~~ The provenance panel of whichever operator surface renders the result (release 0.1, spine §14) MUST render, without a second request, at least: service identity and manufacturer, `clinical_use_mode`, the consumed series list with the rejected ones collapsed but present, preprocessing and model versions, the operating threshold, the evidence dataset version and validation report id, and the output object UIDs with their storage locations.

Nothing left the list and nothing in it became conditional. What changed is the host, and a requirement that names a host the deployment no longer runs is unenforceable rather than satisfied: `medos/deploy/compose/docker-compose.yml` no longer runs `ohif/app:v3.9.2`, and the viewer service is a plain nginx. The panel did not go with it. `medos/web/ohif-extension/` is retained and still served at `/medicalos/`, and what runs there is `standalone/`, importing `src/core/provenance.js` and `src/core/render.js` — the code this requirement has in fact always been checked against, because `MOS-UI-013` records that the modules needing an OHIF host were unexecuted source from the start. What the withdrawal makes permanently unexecutable is the OHIF-host half of that package — `src/index.js` and the four modules it registers, `src/getToolbarModule.js`, `src/getPanelModule.js`, `src/getCommandsModule.js` and `src/panels/provenance-panel.js` — because there is no OHIF host left for it to load into. This amendment does not settle *how* it would have loaded, and says so rather than assert it: `MOS-UI-013` records that OHIF v3.9.2 resolved extensions from its own build-time bundle, while `src/index.js` in this repository documents itself as imported at runtime from `window.config.extensions`, and the two have never been reconciled against a running OHIF. With no OHIF image deployed the question is moot for this requirement and open for `MOS-UI-013`. The subject above is the surface that renders rather than the surface that is named, and deliberately: `MOS-UI-001` as amended puts the extension package at `/medicalos/` *outside* the clinician surface, so naming the clinician surface here would have bound the one surface that has no provenance panel and released the one that has. This requirement binds `/medicalos/` today, and the first-party viewer at `/mos-viewer/` if and when it renders one, which it does not yet. `MOS-UI-036` states the same list for the clinician surface and needs no amendment, because it never named the host.

**MOS-SAFE-089a** — **AMENDED at specification 0.4.0; the condition is struck and the requirement is unconditional.** ~~This requirement is conditional on the OHIF toolbar button shipping. Chapter 15 15.1.3 assigns the button a cut tier for release 0.1.0 and `MOS-REL-009` permits a tiered item to be cut with the cut recorded in the Release Decision Record; where it is cut, this requirement and acceptance check 24 are **inapplicable**, not failed, and the substitute named there (a `curl` against `POST /api/v1/jobs`) inherits no part of them. Whenever the button ships — which spine §14 lists for 0.1.0 — it~~ The clinician surface MUST carry a control that creates a job for the study it has open, and every operator-surface control that creates a job MUST, for the study currently open in the viewer, issue exactly one `POST /api/v1/jobs` (Chapter 10) carrying the study's `study_instance_uid` and the selected `target`, MUST require the `job.create` permission and MUST render the returned `job_id` and `Job.status` inline, polling or subscribing to the SSE stream of Chapter 10. It MUST render a `REJECTED` terminal state visually distinct from `FAILED` and MUST surface the machine-readable reason verbatim (spine §4). It MUST NOT create a second job-creation path, MUST NOT hold a PACS credential, and ~~MUST live in an OHIF extension package outside the OHIF tree (Chapter 15, 15.3.1, Viewer row, `MOS-REL-027`) — an upgradability boundary that keeps the pinned OHIF version replaceable, not a copyleft one: OHIF is MIT, and `MOS-REL-034` is the GPL/LGPL/AGPL/BSL in-process-linking rule, which does not reach the viewer.~~ MUST, where it is contributed to an **adopted** viewer rather than built into the first-party one, live in an extension package outside that viewer's source tree (`MOS-REL-027`; `MOS-UI-009a` retains the four contribution kinds and they still govern `medos/web/ohif-extension/`).

The condition lapses. It is not re-pointed at the extension and it is not replaced by another, and three facts decide that. First, it was an applicability escape keyed to a decision at release 0.1.0 — a tiered item `MOS-REL-009` permits to be cut — and that release is three releases behind this amendment; a condition on a decision already taken is not a condition. Second, the escape needed a tier chapter 15 does not assign. Cutting a **Tier B** item is permitted — `MOS-REL-005` says so — but the escape claimed more than a cut; it claimed *inapplicability*, and the only response that converts a red check into an inapplicable one is `MOS-REL-009` (b), which applies where "the failing check covers a Tier C item only" and "removing that item makes the check inapplicable rather than merely unexecuted". 15.1.3 puts the button at Tier B, so the response available to it is (c), a pre-release whose notes MUST name every failing check — which retires nothing. 15.1.3 also gives as its reason for that assignment that Chapter 9 "makes it a MUST for release 0.1 and tests it in that chapter's acceptance check 24, so a `curl` against `POST /api/v1/jobs` is not an acceptable substitute" — the opposite of what this requirement said about itself, while this requirement named that same `curl` as the substitute. Each chapter's stated ground was a claim about the other that the other contradicted; that is register entry 6 in `docs/spec/99-known-inconsistencies.md`, and this amendment takes the resolution it names. Third, the condition cannot be re-pointed at `medos/web/ohif-extension/` even though the package survives and is still served at `/medicalos/`: the standalone page has no study open, so the half of this requirement that reads *for the study currently open in the viewer* is not satisfiable there at all, and `MOS-UI-013a` already forbids recording that half as a pass. The rest of this requirement does reach that page, and the subject above says "every operator-surface control that creates a job" for exactly that reason: one POST per activation, the `job.create` permission, the inline `job_id` and `Job.status`, the `REJECTED`/`FAILED` distinction with the reason verbatim, and the two prohibitions are properties of the control and not of the host it sits in. `MOS-DATA-016` binds the same control on whichever surface ships it, and a subject naming only the clinician surface would have released the one control that exists today.

What the lapse costs is stated here rather than left for a conformance reviewer to find. This requirement is now **unmet**, not inapplicable. `POST /api/v1/jobs` occurs nowhere under `viewer/`; the first-party viewer at `/mos-viewer/` is the only surface with a study open and it carries no control that creates a job from one, while the surface that carries the control has no study. Acceptance check 24 is amended to match and it FAILS: no tier under it makes it inapplicable, so a gate run that omits it records an omission and not a pass. Register entry 6 is resolved on the terms it names — chapter 15's Tier B cell kept, this requirement restored to an unconditional MUST — and what the register carries out of this amendment is a failing check rather than a closed gap. That is the honest position: the obligation was never satisfied, and until now the conditionality was what kept that from being visible.

The extension-package clause is narrowed rather than dropped. It was an upgradability boundary around a pinned third-party viewer, it still is one for `medos/web/ohif-extension/`, and it has nothing to bound in a viewer the platform builds and versions itself — `MOS-UI-009a` permits building that viewer and states the four guarantees it is held to instead. The copyleft sentence went with the product it was about; `MOS-REL-034` is untouched and still reaches whatever links in-process.

**MOS-SAFE-089** — `GET /api/v1/results/{result_id}/provenance/export` MUST return a **signed bundle**: the canonical JSON record (RFC 8785), the hash-chain segment linking it to the previous record for that tenant, and a detached signature over both. The bundle MUST be verifiable offline with the platform's public key and nothing else. Signing the model artifact while leaving the claim about the model in mutable plain rows is backwards; both are signed.

**MOS-SAFE-090** — Provenance records MUST be append-only at the database role level (no UPDATE, no DELETE for the application role) and MUST participate in a per-tenant hash chain: `record_hash = sha256(canonical_json(record without record_hash))`, `prev_record_hash` = the `record_hash` of `sequence_no - 1` for that tenant. **MOS-SAFE-091** — A daily job MUST verify the chain and MUST emit `provenance.chain.broken` plus an `AuditEvent` on any mismatch.

**MOS-SAFE-092** — The platform MUST provide `medicalos-reproduce <provenance.json>`: a command that reconstructs the run from the record alone — pulling the pinned image digests, the pinned weights and `PreprocessingSpec`, re-reading the recorded instance manifest through the Gateway, and re-running inference — then compares against `outputs[].sha256`. The assertion it makes MUST match `execution.reproducibility_class`: `bitwise` ⇒ byte-identical SEG; `numeric_tolerance` ⇒ per-case Dice ≥ 0.999 and every measurement within the tolerance declared by the `ModelVersion`; `not_reproducible` ⇒ the tool MUST refuse and report that the version declared itself non-reproducible. **MOS-SAFE-093** — A `ServiceVersion` declaring `not_reproducible` MUST NOT pass the E1 clinical gate.

#### Worked provenance record

```json
{
  "provenance_id": "prv_01J9Q7B3D5FGH7JKLM9NPQ",
  "record_schema_version": "provenance/1.0.0",
  "tenant_id": "tnt_01J8ZZ0000000000000001",
  "result_id": "res_01J9Q7B2XYZ1234567890A",
  "job_id": "job_01J9Q7B0MNOP123456789B",
  "parent_job_id": null,
  "root_job_id": "job_01J9Q7B0MNOP123456789B",
  "job_depth": 0,
  "idempotency_key": "ik_x4hq2mtn6pkz3a7fvy5rw9cdeb",
  "sequence_no": 148231,
  "prev_record_hash": "sha256:3c9d0f8a71b25e4408d6f1ac9b7350e2d41f8c6b02a95e73dd1c4f8067b2ae19",
  "record_hash": "sha256:b71e5d2c04a9f8361de7c0b53f9a2481760cd3e5a8b41f92c7d05e6a3b18f042",
  "started_at": "2026-09-13T08:41:02.118Z",
  "finished_at": "2026-09-13T08:42:37.904Z",
  "duration_ms": 95786,

  "input": {
    "patient_internal_id": "pat_01J8ZZ7Q1RSTUV234567890",
    "study_internal_id": "stu_01J9Q6ZZABCDEF123456789",
    "study_instance_uid": "1.2.840.113619.2.55.3.604688.1",
    "series_considered": [
      {"series_instance_uid": "1.2.840.113619.2.55.3.604688.1.101", "series_number": 1, "selected": false, "rejection_reason": "image_type_localizer"},
      {"series_instance_uid": "1.2.840.113619.2.55.3.604688.1.102", "series_number": 2, "selected": true,  "rejection_reason": null},
      {"series_instance_uid": "1.2.840.113619.2.55.3.604688.1.103", "series_number": 3, "selected": false, "rejection_reason": "slice_thickness_5.0mm_above_max_3.0mm"},
      {"series_instance_uid": "1.2.840.113619.2.55.3.604688.1.104", "series_number": 4, "selected": false, "rejection_reason": "image_type_reformatted_coronal"},
      {"series_instance_uid": "1.2.840.113619.2.55.3.604688.1.900", "series_number": 900, "selected": false, "rejection_reason": "sop_class_dose_report_no_pixel_data"}
    ],
    "series_consumed": ["1.2.840.113619.2.55.3.604688.1.102"],
    "instances": {
      "count": 384,
      "sha256_of_sorted_sop_uid_list": "sha256:41e0a7c2b9d3f6058c1a4e7b20d95f83a6c74e109b2d5f8370ae1c6b4d290f75",
      "manifest_uri": "s3://medicalos-tnt01/provenance/prv_01J9Q7B3D5FGH7JKLM9NPQ/instances.json"
    },
    "pixel_digest": "sha256:0d5b8e1f7a34c9026e8b15d3f427a90c6bd84e152739fac0b61d8e5c7a24309f",
    "canonical_geometry": {
      "origin": [-179.5, -337.5, -412.0],
      "spacing": [0.703125, 0.703125, 1.0],
      "direction": [1, 0, 0, 0, 1, 0, 0, 0, 1],
      "shape": [512, 512, 384],
      "frame_of_reference_uid": "1.2.840.113619.2.55.3.604688.1.55"
    },
    "acquisition": {
      "modality": "CT", "body_part": "CHEST", "kernel": "Br40f",
      "slice_thickness_mm": 1.0, "kvp": 120, "contrast_phase": "portal_venous",
      "manufacturer": "SIEMENS", "model": "SOMATOM Force"
    },
    "gateway": {
      "deid_profile": "PS3.15-AnnexE-Basic+CleanDescriptors+RetainLongModifDates",
      "deid_policy_version": "tenant-deid/2.3.0",
      "uid_map_id": "uidmap_tnt01_v2",
      "uid_remap_applied": true,
      "burned_in_phi_check": "pass",
      "gateway_version": "0.2.0"
    }
  },

  "resolution": {
    "capability_id": "pleural_effusion",
    "capability_version": "1.2.0",
    "registry_snapshot_id": "rsnap_01J9Q6Y0000000000000AA",
    "candidates": [
      {"service_id": "pulmo.pleural-effusion", "service_version": "1.4.0", "rank": 1, "excluded_reason": null},
      {"service_id": "pulmo.pleural-effusion", "service_version": "1.3.2", "rank": 2, "excluded_reason": null},
      {"service_id": "acme.effusion", "service_version": "2.0.1", "rank": null, "excluded_reason": "not_deployed_in_tenant"}
    ],
    "reason": "deployment_state"
  },

  "governance": {
    "deployment_id": "dep_01J9A1B2C3D4E5F6G7H8J9",
    "deployment_environment": "production",
    "deployment_state_at_execution": "SERVING",
    "deployment_role_at_execution": "ACTIVE",
    "clinical_use_mode": "research_only",
    "jurisdiction": "DE",
    "regulatory_status_at_execution": {
      "jurisdiction": "EU", "status": "investigational", "device_class": null,
      "identifier": null, "authorising_body": null, "certificate_valid_until": null,
      "evidence_uri": "https://pulmo.example/regulatory/eu-investigational-2026.pdf",
      "declared_at": "2026-03-11"
    },
    "legal_manufacturer": {
      "name": "Pulmo Medical Imaging GmbH", "legal_form": "GmbH",
      "address": "Hauptstrasse 14, 10827 Berlin, Germany", "country": "DE",
      "contact_email": "regulatory@pulmo.example",
      "srn_or_registration_id": "DE-MF-000012345",
      "signing_key_id": "cosign:pulmo-release-2026"
    },
    "intended_use_digest": "sha256:7a1c53e9b0d842f6c73e15a9048bd2f671e3c85049af7b2610d9e4c38b57a2f1",
    "clinical_promotion_audit_id": null,
    "policy_decision_ids": ["pol_01J9Q7B1AAA0000000000A", "pol_01J9Q7B1BBB0000000000B"],
    "review": {"result_review_id": "rrv_01J9Q7B4KKK0000000000C", "review_status_at_write": "PENDING"}
  },

  "execution": {
    "service_id": "pulmo.pleural-effusion",
    "service_version": "1.4.0",
    "service_image_digest": "sha256:5f8c0a91e7d34b6250ca9e18d7b3f402a16e5c8d97b0413fae62d5c081973ae4",
    "execution_mode": "native",
    "models": [
      {
        "role": "effusion_seg",
        "model_id": "pulmo.effusion-unet",
        "version": "1.4.0",
        "artifact_digest": "sha256:c30b7e15a924df6803e1b58c7f4029da61537e8c04b9a2f1d7e630c85ba4197d"
      }
    ],
    "preprocessing_specs": [
      {
        "id": "pre.pulmo.effusion",
        "version": "1.2.0",
        "digest": "sha256:e91a4c7b05d2f83601ac7e5b39d840f27c6b1e50a3f97d24b8e05c136a7f2081"
      }
    ],
    "preprocessing_selftest": {
      "golden_fixture_id": "gf_effusion_001",
      "expected_hash": "sha256:2b7e4109c5d3a86f014e9b72d580a3fc6174ed9b28c5031fa7e64b0d925c8f13",
      "observed_hash": "sha256:2b7e4109c5d3a86f014e9b72d580a3fc6174ed9b28c5031fa7e64b0d925c8f13",
      "match": true
    },
    "operating_threshold": 0.45,
    "threshold_source": "acc_01J9K3P0000000000000EF",
    "inference": {"engine": "triton", "backend": "onnxruntime", "engine_build_digest": "sha256:8d41f5b2069ce7a3418b05d9f627ea30c15b7492de806a3f5c1b9270ade46f38"},
    "accelerator": {"gpu_model": "NVIDIA A10", "driver": "550.90.07", "cuda": "12.4", "cudnn": "9.1.0", "trt": null},
    "runtime": {
      "platform_version": "0.2.0",
      "worker_version": "0.2.0",
      "sdk_version": "medicalos-py/0.2.0",
      "container_image_digests": [
        "sha256:5f8c0a91e7d34b6250ca9e18d7b3f402a16e5c8d97b0413fae62d5c081973ae4",
        "sha256:a4d92f0b71e3586c0d18b4a7f2950c63e81d7b4059af2c6318e0d75b93c4f612"
      ]
    },
    "applicability": {"in_envelope": true, "violations": []},
    "plausibility": {"passed": true, "flags": []},
    "reproducibility_class": "numeric_tolerance"
  },

  "evidence": {
    "validation_report_id": "vr_01J9M4T1CDEF5GHJK6LMNP",
    "validation_report_digest": "sha256:9f2c1ab7e4d8c0335b6a71f2e9d4408c5b1e7a03d6f2489ac13be550772a1d6e",
    "evaluation_run_id": "evr_01J9K2Q7RSTV3WXYZ8ABCD",
    "dataset_version_id": "dsv_01J9H8N5PQRS6TUVW7XYZ0",
    "dataset_version_digest": "sha256:1e7a03d6f2489ac13be550772a1d6e9f2c1ab7e4d8c0335b6a71f2e9d4408c5b",
    "dataset_split_id": "spl_01J9H8N6TEST000000000A",
    "annotation_set_id": "ann_01J9H8N7CONSENSUS2RDRS",
    "acceptance_criteria_id": "acc_01J9K3P0000000000000EF",
    "acceptance_criteria_version": "1.1.0",
    "training_population_declared": true
  },

  "outputs": [
    {
      "output_index": 0,
      "kind": "SEG",
      "sop_class_uid": "1.2.840.10008.5.1.4.1.1.66.4",
      "series_instance_uid": "2.25.184467440737095516150000000000001",
      "sop_instance_uids": ["2.25.184467440737095516150000000000002"],
      "destination": {
        "destination_id": "dst_research_orthanc",
        "stow_endpoint": "https://gateway.internal/tenants/tnt_01J8ZZ0000000000000001/dicomweb/studies",
        "accepts_research": true,
        "stow_http_status": 200,
        "qido_verified_at": "2026-09-13T08:42:36.550Z",
        "instance_count_verified": 1
      },
      "object_store_uri": "s3://medicalos-tnt01/results/res_01J9Q7B2XYZ1234567890A/seg-0.dcm",
      "sha256": "sha256:6b0d8f3a14c25e79308ba6d15f4c9207e3d810ba57f92c4e601d7a3b8f5029ce",
      "size_bytes": 4718592,
      "marking": {"ai_derived": true, "ruo": true, "series_description": "AI RUO Pleural Effusion SEG", "verification_flag": null},
      "supersedes": null,
      "superseded_by": null
    },
    {
      "output_index": 1,
      "kind": "SR",
      "sop_class_uid": "1.2.840.10008.5.1.4.1.1.88.33",
      "series_instance_uid": "2.25.184467440737095516150000000000003",
      "sop_instance_uids": ["2.25.184467440737095516150000000000004"],
      "destination": {
        "destination_id": "dst_research_orthanc",
        "stow_endpoint": "https://gateway.internal/tenants/tnt_01J8ZZ0000000000000001/dicomweb/studies",
        "accepts_research": true,
        "stow_http_status": 200,
        "qido_verified_at": "2026-09-13T08:42:37.410Z",
        "instance_count_verified": 1
      },
      "object_store_uri": "s3://medicalos-tnt01/results/res_01J9Q7B2XYZ1234567890A/sr-1.dcm",
      "sha256": "sha256:f402a16e5c8d97b0413fae62d5c081973ae45f8c0a91e7d34b6250ca9e18d7b3",
      "size_bytes": 38912,
      "marking": {"ai_derived": true, "ruo": true, "series_description": "AI RUO Pleural Effusion SR", "verification_flag": "UNVERIFIED"},
      "supersedes": null,
      "superseded_by": null
    }
  ]
}
```

### 9.10. Nested execution and the provenance tree

**MOS-SAFE-103** — This section applies **from 0.4.0 only**. Nested execution is reserved for 0.4.0 (Chapter 5, `MOS-EXEC-084`; spine §13), and cross-service chaining is out of scope for 0.1.0–0.3.0 (Chapter 2, `MOS-SVC-008`). In releases 0.1.0 through 0.3.0 every `Job` MUST have `job_depth = 0`, `parent_job_id IS NULL` and `root_job_id = job_id`, and a job-creation request that would create a child MUST be refused. No requirement in §9.10 may be read as a 0.1.0 obligation under `MOS-SVC-056`.

**MOS-SAFE-094** — A single `agent_id` field cannot represent a run in which one execution invokes another (a report step that is itself an agent, a service that calls a second capability). Provenance in MedicalOS is therefore a **tree**, not a row.

**MOS-SAFE-095** — Every nested execution MUST be a real `Job` row with `parent_job_id` set to the invoking job and `root_job_id` set to the tree root. A root job has `parent_job_id = NULL` and `root_job_id = job_id`. There is no in-process nested execution that skips the job table; an execution that leaves no job row leaves no provenance.

**MOS-SAFE-096** — `job_depth` MUST be stored and MUST NOT exceed **3** (root = 0). A job creation request that would produce depth 4 MUST be refused with `422`, `class: policy_violation`, `detail: "nesting depth limit exceeded"`, and an `AuditEvent`. Cycle detection MUST be performed on `(root_job_id, service_id, service_version, study_internal_id)`: a repeat of that tuple anywhere on the ancestor chain MUST be refused with `detail: "execution cycle detected"`.

**MOS-SAFE-097** — Budgets and deadlines MUST be divided, never inherited unchanged: a child's `deadline_at` MUST be ≤ the parent's `deadline_at` minus the parent's reserved postprocessing margin, and the child's resource budget MUST be deducted from the parent's remaining budget at child creation. A child that would exceed either MUST be refused before it is enqueued.

**MOS-SAFE-098** — Governance is inherited and MUST NOT be widened downward: a child job MUST carry the root's `tenant_id`, `clinical_use_mode`, `jurisdiction` and effective permission set. A child MUST NOT run under a `clinical_use_mode` stronger than its parent's, and MUST NOT hold a permission its parent lacks.

**MOS-SAFE-099** — One provenance record is written per job, including every nested job. The tree is reconstructed by `root_job_id` and ordered by `job_depth`. **MOS-SAFE-100** — A `Result` produced at depth > 0 MUST carry `root_result_id` pointing at the root job's `Result` when one exists, so that a clinician-visible finding resolves to the whole tree, not to a leaf.

**MOS-SAFE-101** — The signed export of MOS-SAFE-089 MUST, when invoked with `tree=true`, contain every record in the tree plus a `tree_digest = sha256(concat(record_hash of each record ordered by (job_depth, started_at, job_id)))`, and the signature MUST cover `tree_digest`. A tree that is verifiable only record-by-record permits a silently pruned branch.

**MOS-SAFE-102** — Failure of a nested job MUST propagate as a first-class outcome, not as a silent omission: the parent MUST record `child_jobs[] = [{job_id, terminal_state, reason}]` in its own provenance record, and a parent MUST NOT report `COMPLETED` while a child is `FAILED` unless the parent's `ServiceVersion` declares that child optional in its manifest.

### Acceptance criteria

Each check is executable by CI or by a reviewer against a running deployment. "Golden path" is the pleural-effusion service of §9.2 on a real chest CT study.

1. **Claims lint.** A CI job greps the whole repository (source, YAML, migrations, OpenAPI, docs, UI strings, i18n bundles) for the forbidden terms of MOS-SAFE-008. Zero hits outside `docs/REGULATORY.md`, where they appear only in the forbidden/required table. The MOS-SAFE-001 statement is present verbatim in `README.md`, the API landing document, and the UI footer bundle.
2. **Manifest completeness, fail-closed.** Registering a `ServiceVersion` whose `clinical` block is missing, or whose `not_validated_for[]` or `known_failure_modes[]` is empty, or whose `risk_classification.imdrf.category` disagrees with the MOS-SAFE-019 table, or whose `legal_manufacturer.signing_key_id` does not match the signing key, returns `422` with a `pointer` naming the offending member. Four negative fixtures, four distinct codes.
3. **Autonomy refusal.** A `ServiceVersion` with `intended_use.autonomy: autonomous` is refused at deployment with `violations: ["autonomous_service_refused"]`.
4. **Clinical gate.** With the §9.2 example service (no CLEARED status), `PATCH /deployments/{id}` setting `clinical_use_mode: clinical` returns `422` listing at least `no_cleared_regulatory_status_for_jurisdiction` and `named_approver_required`. After substituting a fixture manifest with `status: ce_mdr` valid to 2029, a signed `ValidationReport` whose criteria are met, and a request carrying `approved_by` + `approval_rationale`, the transition succeeds and writes one `AuditEvent` whose payload contains `intended_use_digest` and `validation_report_digest`. A Deployment that is `environment = production` but `state = SUSPENDED`, or `role = SHADOW`, fails the same gate with `deployment_not_production_serving` (MOS-SAFE-046).
5. **Expiry demotion.** Fast-forwarding the clock past `certificate_valid_until` causes the daily check to demote the Deployment to `research_only`, emit `deployment.clinical_use_mode.demoted`, leave every pre-existing `Result.clinical_use_mode` unchanged at `clinical`, and cause the next produced object to carry the RUO marker set.
6. **Marker set.** Every object produced by the golden path is parsed with pydicom and every row of MOS-SAFE-048, MOS-SAFE-049 and (in research mode) MOS-SAFE-050 is asserted present with the required value. In research mode, the SC frame's top 40 rows contain the banner text as read back by OCR or by a pixel-hash comparison against a rendered reference, and `BurnedInAnnotation == "YES"`.
7. **Metadata-only identification.** A QIDO-RS query for the study, returning metadata only, allows a test to classify every series as AI-derived or not, and every AI-derived series as clinical or RUO, using only `SeriesDescription`, `SeriesNumber` and `Manufacturer`. No pixel retrieval, no MedicalOS API call.
8. **Destination gate.** A research-mode job whose only configured destination has `accepts_research: false` fails with Gateway `403 research_object_to_clinical_destination`, writes an `AuditEvent`, and leaves zero instances in the PACS for the minted `SeriesInstanceUID` (verified by QIDO-RS).
9. **Read-path filter.** `GET /api/v1/results` as a principal without `result.read.research` returns zero research-mode results; with the permission but without `include_research=true`, still zero; with both, the research result appears and carries `clinical_use_mode: "research_only"`.
10. **Marking is not optional.** With the marking function stubbed to drop `SeriesDescription`, the golden path fails with `error.code = safety_marking_absent`, the job is `FAILED`, and zero instances reach the PACS.
11. **Review lifecycle.** A full cycle — created `PENDING`, claimed, submitted `MODIFIED` with an RFC 6902 patch and a rationale, reopened, resubmitted `ACCEPTED` — produces two `ResultReview` rows with `round` 1 and 2, five `AuditEvent`s, five webhook deliveries with the MOS-SAFE-070 type names, and `Result.review_status == "ACCEPTED"`. Submitting `MODIFIED` without `modifications`, or `REJECTED` without `rejection_reason`, returns `422`. There is no client-facing create route: `POST /api/v1/result-reviews` returns `404`.
12. **No auto-action.** Throughout check 11 with `emit_verified_sr_on_accept: false`, the test asserts zero new rows in `jobs`, zero STOW-RS requests, and zero outbound requests to any non-webhook destination. An attempt to `UPDATE` a terminal `ResultReview` row as the application database role fails with a permission error.
13. **Verification gate.** In `research_only` mode with `emit_verified_sr_on_accept: true`, an `ACCEPTED` review emits no SR and the original SR still reads `VerificationFlag == "UNVERIFIED"`. In `clinical` mode, an `ACCEPTED` review by a `ServiceAccount` principal is refused; by a `User` with `result.review.submit` acting under a role whose `clinically_qualified` is `true` (so `reviewer_class == "clinical"`, MOS-SAFE-068/104) it emits exactly one new SR with `VerificationFlag == "VERIFIED"`, a populated `VerifyingObserverSequence`, and a `PredecessorDocumentsSequence` referencing the original, which still exists. The revision's `SOPInstanceUID` differs from the original's and its `SeriesInstanceUID` is identical (MOS-SAFE-065, Chapter 4 `MOS-IMG-065`). The same review submitted under a role whose `clinically_qualified` is `false` is recorded `reviewer_class: "non_clinical"` and emits no SR; a grep of the schema finds no CHECK, enum or lookup enumerating role keys (Chapter 8 acceptance check 26).
14. **DENY table.** One test per hard-DENY row: a service container attempting a TCP connection to Postgres, the broker, object storage and the PACS is refused (row 20); a STOW-RS re-using an existing SOP Instance UID with different content is rejected (row 5); a DELETE against the Gateway returns 405 with no route (row 6); the application role's `DELETE` on `results`, `audit_events` and `result_provenance` fails (row 7); an in-place threshold edit on a deployed version is rejected (row 17). Each produces a `PolicyDecision` row citing the table row number.
15. **Safety-rule classification.** `docs/SAFETY.md` parses into exactly two labelled sections; every rule carries a class; no GUIDANCE rule appears under a heading containing "control", "safeguard", "mitigation" or "requirement".
16. **Content post-conditions.** Feeding the narrative tool a `Result` containing `{left: 320 ml, right: 410 ml}` and a generated text asserting "1200 ml on the left" fails closed with an unmatched-token report naming `1200`; text containing "confirms a diagnosis of empyema" fails the MOS-SAFE-080 lexicon check; neither produces an emitted object. Removing an entry from the tenant lexicon is rejected.
17. **Provenance completeness.** For the golden path, every field of MOS-SAFE-083 sections A–F is present and non-null except those explicitly nullable; a JSON-Schema validation of the record against `provenance/1.0.0` passes; `input.series_consumed` is a strict subset of the selected entries of `input.series_considered`; `input.series_considered` has at least one rejected entry with a non-null `rejection_reason`; `outputs[].destination.qido_verified_at` is set for every output; `execution.models[]`, `execution.preprocessing_specs[]` and `execution.accelerator` are byte-equal to the corresponding members of the service's `ResultBundle.diagnostics` (Chapter 2 `MOS-SVC-098`).
18. **Provenance atomicity.** Killing the process between the STOW-RS call and the terminal transition leaves zero `results` rows and zero provenance rows; on retry, the deterministic UIDs are re-derived, QIDO-RS finds the existing series, inference is skipped, and exactly one `results` row and one provenance row exist at the end.
19. **No PHI in provenance.** A scanner over the provenance record for `PatientName`, `PatientBirthDate`, `AccessionNumber`, `StudyDescription` keys and for values matching the tenant's known patient-name fixtures returns zero hits.
20. **Hash chain and signed export.** `GET /provenance/export` returns a bundle that `medicalos-verify` validates offline with only the public key. Mutating one byte of the record in the database and re-running the daily chain check emits `provenance.chain.broken` naming the `sequence_no`.
21. **Reproducibility.** `medicalos-reproduce prv_*.json` on the same accelerator re-runs the job from the record alone and satisfies the declared `reproducibility_class` (byte-identical SEG for `bitwise`; Dice ≥ 0.999 and measurements within declared tolerance for `numeric_tolerance`). A `ServiceVersion` declaring `not_reproducible` fails the E1 gate (check 4 variant).
22. **Provenance tree (0.4.0 only — MOS-SAFE-103).** In 0.1.0–0.3.0 the check is the inverse: every `Job` row has `job_depth = 0`, `parent_job_id IS NULL` and `root_job_id = job_id`, and a job-creation request that would create a child is refused. From 0.4.0: a fixture in which a root job invokes a child that invokes a grandchild produces three job rows with correct `parent_job_id`/`root_job_id`/`job_depth`, three provenance records, and a `tree=true` export whose `tree_digest` matches the recomputed concatenation. A fourth level is refused with `nesting depth limit exceeded`; a child repeating an ancestor's `(service_id, service_version, study_internal_id)` is refused with `execution cycle detected`; a child whose requested deadline exceeds the parent's is refused before enqueue.
23. **Manufacturer traceability, end to end.** Starting from a single generated SEG file on disk and nothing else, a reviewer resolves `Manufacturer`, `ManufacturerModelName`, `SoftwareVersions` and `DeviceSerialNumber`, calls `GET /api/v1/service-versions/{id}/clinical`, and obtains the full intended-use block whose `intended_use_digest` equals the value in the object's provenance record.
24. **~~OHIF toolbar button.~~ Clinician-surface job-creation control.** AMENDED at specification 0.4.0. ~~Applicable only when the button ships: if Chapter 15 15.1.3 cuts it under `MOS-REL-009`, this check is inapplicable rather than failed and the Release Decision Record records the cut.~~ With a study open in the viewer, one activation issues exactly one `POST /api/v1/jobs` carrying that study's `study_instance_uid` and the selected target, and the returned `job_id` and `Job.status` render inline and update from the SSE stream. A principal without `job.create` cannot issue the request. A job terminating `REJECTED` renders visually distinct from `FAILED` and shows the machine-readable reason verbatim. A network capture shows zero viewer-to-PACS traffic outside the Gateway. A surface that has no such control, or that has one with no study open in it, records this check **FAILED** — the inapplicability the struck sentence allowed required `MOS-REL-009` (b), which reaches a Tier C item only, and 15.1.3 assigns this one Tier B; a check that cannot be run is not a check that passed. (MOS-SAFE-089a)

---

[← 8. Security, Tenancy and PHI](08-security.md) · [Index](../../MEDICALOS_SPEC.md) · [10. API and SDKs →](10-api.md)
