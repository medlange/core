<!-- MedicalOS Specification v0.4.0 — chapter 7 of 19. Normative.
     143 requirements. Do not edit without a requirement-ID review. -->

[← 6. Registries, Capability Resolution and Deployment](06-registries.md) · [Index](../../MEDICALOS_SPEC.md) · [8. Security, Tenancy and PHI →](08-security.md)

---

## 7. Evidence Plane: Datasets, Evaluation and Validation Reports

The previous version of this document gated five separate things on "validation" and never said what validation produces or what it was measured against. Validation was a *state* with no *artifact*. This chapter defines the artifact, the inputs it is computed from, the rule that consumes it, and the three distinct activities that the old document conflated into one word.

Everything in this chapter is machine-readable, content-addressed and re-computable offline. If a number cannot be traced to a row in `evaluation_case_metrics`, it is not a MedicalOS metric.

### 7.1 Position and boundary

**MOS-EVID-001** MedicalOS MUST NOT perform clinical validation, MUST NOT confer clinical validation, and MUST NOT represent any output of this chapter as clinical validation. Clinical validation is an activity of the ServiceVersion publisher acting as manufacturer (Chapter 9) and, where applicable, of a notified body or regulator. MedicalOS produces *technical evidence* about a pinned artifact on a named cohort.

**MOS-EVID-002** The platform's evidence claim is bounded by exactly this sentence, which MUST be reproduced verbatim on every generated `ValidationReport`:

> This report states the measured behaviour of a pinned software artifact on a named, content-addressed cohort under the declared conventions. It is not a clinical validation, not a regulatory clearance, and not a statement of fitness for any patient population outside the declared applicability envelope.

**MOS-EVID-003** Every evidence activity MUST be labelled with one of exactly three `kind` values, defined in 7.13: `vendor_evidence`, `site_acceptance`, `monitoring_period`. There is no fourth kind and no unlabelled evidence.

**MOS-EVID-004** The word "validation" MUST NOT appear unqualified in any platform-generated identifier, API field, UI label, log message, or report heading. Permitted qualified forms: `technical_validation`, `site_acceptance`, `schema_validation`, `envelope_validation`. 

**MOS-EVID-005** The strings `clinically validated`, `clinical validation`, `FDA approved`, `CE marked`, `diagnostic accuracy certified` MUST NOT appear in any string literal, template, translation file, or generated artifact produced by platform code. CI MUST enforce this as a repository-wide case-insensitive grep over all source, template and locale files, excluding this specification and excluding fields whose value is supplied verbatim by a publisher under Chapter 9's `regulatory_status`.

**MOS-EVID-006** The evidence plane is not a sixth architectural plane. It is a set of entities, a gate function and a report format, hosted in the control plane (Chapter 2). No component of the serving path depends on it at request time except the gate lookup performed at deployment time (7.9) and the envelope check performed at triage time (7.10).

### 7.2 Entities, identity and content addressing

| Entity | Mutability | Identity | Owner |
|---|---|---|---|
| `Dataset` | mutable metadata | `ds_<ULID>` | Tenant |
| `DatasetVersion` | **sealed** after creation | `dsv_<ULID>` + `manifest_digest` | Tenant |
| `DatasetSplit` | **frozen** after creation | `spl_<ULID>` + `split_digest` | Tenant |
| `AnnotationSet` | **frozen** after creation | `ann_<ULID>` + `annotation_digest` | Tenant |
| `EvaluationRun` | append-only | `evr_<ULID>` + `run_digest` | Tenant |
| `AcceptanceCriteria` | versioned, immutable per version | `(capability_id, version)` | **Capability** |
| `ApplicabilityEnvelope` | versioned, immutable per version | `(subject_ref, version)` | ServiceVersion / ModelVersion |
| `PlausibilityRuleSet` | versioned, immutable per version | `(capability_id, version)` | Capability |
| `ValidationReport` | immutable once signed | `vr_<ULID>` + `report_digest` | Tenant or publisher |
| `DeploymentGateDecision` | append-only | `gd_<ULID>` | Tenant |

**Relationship to Chapter 12.** Chapter 12 §12.12 is the **schema of record** for every evidence-plane table: it owns table names, column names, keys, constraints and the sealing triggers. This chapter owns the entities, their enum *values*, and the facts that MUST be recorded about them. It therefore states **field contracts**, not DDL. Where a field name used here differs from Chapter 12's column name for the same fact, Chapter 12's spelling is the name of record and the field-contract tables below give it. Where a field contract below names a fact Chapter 12 §12.12 does not yet carry a column for, the fact is still required and Chapter 12 is the place it must be added; the two MUST NOT diverge.

**MOS-EVID-007** All digests in the evidence plane MUST be SHA-256 and MUST be serialised as the string `"sha256:" + lowercase_hex`. No other algorithm is permitted in 0.2.0.

**MOS-EVID-008** All JSON that is digested or signed MUST first be canonicalised with RFC 8785 JSON Canonicalization Scheme (JCS). Non-finite numbers MUST be rejected at write time, not at digest time.

**MOS-EVID-009** A manifest is a JSONL file: one JCS-canonical JSON object per line, LF-terminated, UTF-8, no trailing blank line. The manifest digest is computed by the following function and no other:

```python
# medicalos/evidence/digest.py
import hashlib, json

def canonical_json(obj) -> bytes:
    """RFC 8785 JCS over the restricted subset MedicalOS emits:
    object keys sorted by UTF-16 code unit, no insignificant whitespace,
    UTF-8 output, NaN/Infinity rejected."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")

def manifest_digest(lines: list[dict]) -> str:
    """`lines` MUST already be in the canonical sort order declared for
    the manifest kind (MOS-EVID-017, -030, -040)."""
    h = hashlib.sha256()
    for line in lines:
        h.update(canonical_json(line))
        h.update(b"\n")
    return "sha256:" + h.hexdigest()
```

**MOS-EVID-010** `patient_key` is the evidence plane's patient identity and the unit of every split and every bootstrap resample. It MUST be computed as:

```python
def patient_key(tenant_salt: bytes, issuer: str, patient_id: str) -> str:
    import hmac, hashlib, base64
    msg = f"{issuer}|{patient_id}".encode("utf-8")
    mac = hmac.new(tenant_salt, msg, hashlib.sha256).digest()
    return "pk_" + base64.b32encode(mac[:10]).decode("ascii").rstrip("=").lower()
```

`issuer` is `IssuerOfPatientID` (0010,0021) when present; otherwise the `source_id` of the `Dataset` (for example `TCIA/LIDC-IDRI`). `tenant_salt` is a per-tenant secret from the tenant keyring (Chapter 8), stable for the life of the tenant, so `patient_key` is comparable across every DatasetVersion in that tenant and meaningless outside it.

**MOS-EVID-011** `patient_key` MUST NOT be reversible without the tenant salt and MUST NOT be exported in a `vendor_evidence` bundle that crosses a tenancy boundary; the export path MUST re-key with a per-report salt recorded in the report (`patient_key_scheme.report_salt_id`) so that per-case rows remain internally joinable but not linkable back to the originating tenant.

**MOS-EVID-012** Every evidence table MUST carry `tenant_id uuid NOT NULL` and be covered by row-level security as specified in Chapter 8. `visibility` ∈ {`tenant_private`, `tenant_shared`, `public_readonly`} controls sharing of *public* collections only; a DatasetVersion whose `deidentification_status` is not `public_deidentified` MUST NOT be set to any visibility other than `tenant_private`.

**MOS-EVID-013** Sealing/freezing is enforced at the database level, not in application code: the evidence tables listed as sealed in the table above MUST have `UPDATE` and `DELETE` revoked from the application role for all columns except the explicitly mutable status columns named in this chapter, and a `BEFORE UPDATE` trigger MUST raise on any attempt to modify a digest column. Chapter 12 implements this with its `forbid_column_change` and `forbid_mutation` triggers and the grant-level revocation of `MOS-STORE-228`; the sealed-column lists there MUST cover exactly the columns named here.

**MOS-EVID-014** A sealed object MAY be marked defective but MUST NOT be edited. `dataset_versions.status` ∈ {`SEALED`, `DEFECTIVE`}; `evaluation_runs.state` ∈ {`PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `INVALIDATED`}. Both enums are owned by this chapter; Chapter 12 stores them and Chapter 6's registry (`MOS-REG-020`) records them, but neither originates a value and neither may narrow the set. Marking a DatasetVersion `DEFECTIVE` MUST cascade `INVALIDATED` onto every EvaluationRun bound to it and MUST set `validation_reports.status = 'REVOKED'` on every report citing those runs.

### 7.3 `Dataset` and `DatasetVersion`

A `Dataset` is a named, mutable container ("LIDC-IDRI chest CT", "Site A pleural effusion acceptance cohort"). A `DatasetVersion` is a sealed, content-addressed manifest of the exact images it contains.

#### 7.3.1 Field contract

Chapter 12 §12.12 is the schema of record for `datasets` and `dataset_versions`. The tables below state what Chapter 7 requires to be recorded, in Chapter 12's column names.

`datasets`:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `ds_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant (MOS-EVID-012) |
| `slug`, `display_name` | `UNIQUE (tenant_id, slug)` | stable handle and human name |
| `purpose` | ∈ `training`, `tuning`, `evaluation`, `acceptance`, `monitoring` | what the collection exists for |
| `source_id` | `NOT NULL` | provenance handle, e.g. `TCIA/LIDC-IDRI`, `site-a/pacs`; carried verbatim into manifests and exported bundles, and used as the `issuer` fallback of MOS-EVID-010. Chapter 12 records it as `custodian` on the dataset and `source_description` on the version |
| `licence_spdx`, `licence_text`, `licence_url` | `licence_spdx` or `licence_text` non-null per MOS-EVID-027 | licence of record |
| `visibility` | ∈ `tenant_private`, `tenant_shared`, `public_readonly`; default `tenant_private` | sharing (MOS-EVID-012) |
| `deleted_at` | nullable | soft delete |
| `created_at`, `created_by` | `NOT NULL` | audit |

`dataset_versions`:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `dsv_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `dataset_id`, `version` | `UNIQUE (dataset_id, version)` | position in the lineage |
| `parent_version_id` | nullable FK | lineage (MOS-EVID-022) |
| `manifest_digest` | `NOT NULL`, `sha256:…`, `UNIQUE` | content address |
| manifest location | `NOT NULL` | Chapter 12 `manifest_bucket` + `manifest_object_key` |
| `manifest_line_count`, `case_count`, `patient_count`, `study_count`, `series_count`, `instance_count` | `NOT NULL`; `patient_count <= case_count` | cohort size, one line per series |
| `deidentification_status` | ∈ `identified`, `pseudonymised`, `public_deidentified` | de-identification state (Chapter 3) |
| `deid_policy_id` | non-null unless `identified` | Chapter 3 policy version |
| `uid_mapping_table_id` | non-null if remapped | Chapter 3 tenant UID map |
| `acquisition_profile` | `jsonb NOT NULL` | 7.3.4 |
| `status`, `defect_reason` | `status` ∈ `SEALED`, `DEFECTIVE`, default `SEALED` (MOS-EVID-014) | the only mutable evidence state on the row |
| `erasure_state`, `usable_for_new_runs` | Chapter 12 `MOS-STORE-292` | mutable for erasure, never for content |
| `sealed_at`, `sealed_by` | `NOT NULL` | audit |

**MOS-EVID-015** `dataset_versions` rows MUST be created in a single transaction that also writes the manifest object; a row whose manifest location does not resolve to an object whose SHA-256 equals `manifest_digest` MUST be treated as `DEFECTIVE` by every reader.

**MOS-EVID-016** A DatasetVersion MUST NOT be created by reference to a live query, a folder path, a DICOM query filter, or a database view. Only an explicit, materialised list of instances is a DatasetVersion.

#### 7.3.2 Manifest line format

One line per **series**. Canonical sort order: `(patient_key, study_instance_uid, series_instance_uid)`, byte-wise ascending.

```json
{"patient_key":"pk_4tqv2n5z6bhc7wqa","study_instance_uid":"1.3.6.1.4.1.14519.5.2.1.6279.6001.298806137288633453246975630178","series_instance_uid":"1.3.6.1.4.1.14519.5.2.1.6279.6001.179049373636438705059720603192","modality":"CT","sop_class_uid":"1.2.840.10008.5.1.4.1.1.2","instance_count":133,"sop_instance_uids":["1.3.6.1.4.1.14519.5.2.1.6279.6001.106164839995541418972266359372","1.3.6.1.4.1.14519.5.2.1.6279.6001.245001887729454317252188243121"],"series_pixel_digest":"sha256:9f2c1d3a5e7b4c8d0f6a2b9e1c4d7f3a8b5e0c2d6f9a1b4e7c0d3f6a9b2e5c8d","lossy_compressed":false,"acquisition":{"slice_thickness_mm":2.5,"pixel_spacing_mm":[0.703125,0.703125],"convolution_kernel":"B30f","convolution_kernel_class":"soft","manufacturer":"SIEMENS","manufacturer_model_name":"Sensation 16","kvp":120.0,"contrast_phase":"non_contrast","z_coverage_mm":332.5,"image_type":["ORIGINAL","PRIMARY","AXIAL"],"patient_age_years":67,"patient_sex":"M","body_part_examined":"CHEST"}}
```

The per-series rows of this manifest are persisted as `dataset_cases` (Chapter 12 §12.12, `MOS-STORE-291`), which references DICOM UIDs rather than foreign keys into the imaging projection for exactly the reason Chapter 12 gives: the evidence must outlive the projection.

**MOS-EVID-017** `sop_instance_uids` MUST list every instance in the series in the canonical slice order defined by Chapter 4 (MOS-IMG), and its length MUST equal `instance_count`.

**MOS-EVID-018** `series_pixel_digest` MUST be computed as below. It hashes **stored** pixel values without rescale and without geometry normalisation, so that dataset identity does not change when the geometry code changes:

```python
def series_pixel_digest(datasets_in_series) -> str:
    import hashlib, numpy as np
    ordered = canonical_slice_order(datasets_in_series)   # Chapter 4, MOS-IMG
    h = hashlib.sha256()
    for ds in ordered:
        arr = ds.pixel_array                              # stored values, rescale NOT applied
        h.update(hashlib.sha256(np.ascontiguousarray(arr).tobytes()).digest())
    return "sha256:" + h.hexdigest()
```

**MOS-EVID-019** A series whose source transfer syntax is lossy MUST set `lossy_compressed: true`. A DatasetVersion containing any `lossy_compressed: true` series MUST record that fact in `acquisition_profile.lossy_series_fraction`, and an `AcceptanceCriteria` evaluation over such a version MUST surface it in the report; the digest of a lossy series is valid only for the exact stored representation and MUST NOT be relied on for cross-site equality.

**MOS-EVID-020** `acquisition.*` values MUST be copied from the source header without imputation. A missing value MUST be serialised as JSON `null`, never as a default. `convolution_kernel_class` is the only derived field; its mapping table (vendor kernel string → `{sharp, standard, soft, unknown}`) MUST be versioned and its version recorded in `acquisition_profile.kernel_class_map_version`.

**MOS-EVID-021** If `deidentification_status` is `pseudonymised` or `public_deidentified`, the de-identification policy version (Chapter 3; Chapter 12 column `deid_policy_id`) MUST be non-null and `uid_mapping_table_id` MUST reference the tenant UID mapping table of Chapter 3. Because results reference source SOP Instance UIDs, a DatasetVersion sealed under one UID mapping is not interchangeable with the same images under another; the platform MUST refuse to compare EvaluationRuns whose DatasetVersions differ in `uid_mapping_table_id` unless their `series_pixel_digest` sets are identical.

#### 7.3.3 Lineage

**MOS-EVID-022** `parent_version_id` MUST be set whenever a version is derived from another by inclusion, exclusion or re-de-identification. The derivation MUST additionally be recorded as a `derivation` object on the version: `{"op":"exclude","reason":"gantry tilt > 3 deg","removed_series":41,"predicate_digest":"sha256:..."}`.

**MOS-EVID-023** Adding cases to a DatasetVersion is not possible; it produces a new version. Two versions sharing a parent MUST NOT be assumed disjoint, and the leakage checks of 7.4.3 MUST be re-run whenever any split spans more than one version.

#### 7.3.4 `acquisition_profile`

**MOS-EVID-024** Every sealed DatasetVersion MUST carry a computed `acquisition_profile` summarising the cohort. This object is the sole permitted input to automatic envelope derivation (7.10.2).

```json
{"n_series":1284,"n_patients":812,"kernel_class_map_version":3,"lossy_series_fraction":0.0,
 "numeric":{"slice_thickness_mm":{"min":0.625,"p01":0.625,"p50":1.25,"p99":3.0,"max":5.0},
            "pixel_spacing_mm_max":{"min":0.488,"p01":0.507,"p50":0.703,"p99":0.977,"max":0.977},
            "z_coverage_mm":{"min":168.0,"p01":181.2,"p50":312.5,"p99":448.0,"max":501.0},
            "instance_count":{"min":68,"p01":94,"p50":251,"p99":702,"max":918},
            "patient_age_years":{"min":19,"p01":24,"p50":63,"p99":89,"max":94}},
 "categorical":{"manufacturer":{"SIEMENS":611,"GE MEDICAL SYSTEMS":402,"Philips":198,"CANON MEDICAL SYSTEMS":73},
                "convolution_kernel_class":{"soft":900,"standard":384},
                "contrast_phase":{"non_contrast":1284},
                "patient_sex":{"M":431,"F":381}}}
```

**MOS-EVID-025** Percentiles MUST be computed with the linear interpolation method (`numpy.quantile` default) over the **series** population, and the patient-level counts MUST be reported separately as shown. Mixing the two is a reporting defect.

**MOS-EVID-026** A DatasetVersion whose `Dataset.purpose` is `acceptance` MUST have `n_patients >= 30`. Below that, the platform MUST refuse to seal it for that purpose, because no criterion in 7.8 can return anything but `INDETERMINATE`. `acceptance` is the single `purpose` value covering both publisher evidence cohorts and site acceptance cohorts; the distinction between them is the report `kind` of MOS-EVID-003, not the dataset purpose.

**MOS-EVID-027** `licence_spdx` or `licence_text` MUST be non-null for any DatasetVersion with `visibility != 'tenant_private'`, and the licence MUST be carried into any exported report bundle (7.12.3).

### 7.4 `DatasetSplit` — patient-level, frozen, never a seed

#### 7.4.1 Field contract

Chapter 12 §12.12 is the schema of record for `dataset_splits` and `dataset_split_members`.

`dataset_splits`:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `spl_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `dataset_version_id`, `name` | `UNIQUE (dataset_version_id, name)` | which cohort this splits |
| `split_digest` | `NOT NULL`, `sha256:…` | content address of the split manifest (Chapter 12 column `manifest_digest`) |
| manifest location | `NOT NULL` | object store location of the split `.jsonl` |
| `partition_level` | `= 'patient'` | MOS-EVID-029, Chapter 12 `MOS-STORE-294` |
| `partitions` | `text[] NOT NULL` | e.g. `{train,tune,test}` |
| `partition_patients` | `jsonb NOT NULL` | e.g. `{"train":568,"tune":81,"test":163}` |
| `assignment_method` | `text NOT NULL` | documentation only, never re-executed |
| `stratified_by` | `text[] NOT NULL DEFAULT '{}'` | declared strata |
| `leakage_report` | `jsonb NOT NULL` | 7.4.3 |
| `frozen_at`, `frozen_by` | `NOT NULL` | audit (Chapter 12 `sealed_at`) |

`dataset_split_members`:

| Field | Constraint | Meaning |
|---|---|---|
| `split_id`, `patient_key` | `PK (split_id, patient_key)` | one row per patient, which makes cross-partition leakage structurally impossible |
| `partition` | `NOT NULL`, ∈ `train`, `tune`, `test`, `excluded` | the partition vocabulary of this chapter; `val` is not a permitted value |
| `exclusion_reason` | `CHECK ((partition = 'excluded') = (exclusion_reason IS NOT NULL))` | MOS-EVID-031 |
| `stratum` | `jsonb` | stratum assignment recorded at freeze time |

**MOS-EVID-028** A split MUST be a materialised manifest. `assignment_method` (for example `"hash_mod_10(patient_key) stratified by reference_volume_ml decile"`) is descriptive metadata. The platform MUST NOT store a seed in place of a manifest and MUST NOT regenerate a split from a seed under any circumstance.

**MOS-EVID-029** The unit of assignment MUST be `patient_key`. Assignment at study, series or instance level MUST be rejected at write time.

**MOS-EVID-030** Split manifest line format, sorted ascending by `patient_key`:

```json
{"patient_key":"pk_4tqv2n5z6bhc7wqa","partition":"test","fold":null,"stratum":{"reference_volume_decile":7}}
```

`split_digest` is `manifest_digest()` over those lines. The member line's `fold` member is the optional cross-validation fold index within a partition; it is not a synonym for `partition` and MUST NOT be used as one.

**MOS-EVID-031** Every `patient_key` present in the DatasetVersion manifest MUST appear exactly once in the split manifest. A split covering a subset MUST declare the excluded patients explicitly with `"partition": "excluded"` and a `"exclusion_reason"` string; silent omission MUST be a write-time error.

#### 7.4.2 Cross-partition rule

**MOS-EVID-032** An `EvaluationRun` MUST name exactly one partition. Reporting an aggregate computed across two partitions MUST be refused.

**MOS-EVID-033** The `test` partition MUST NOT be used to select an operating threshold, a post-processing parameter, a checkpoint, a preprocessing variant, or any model configuration, network architecture, ensemble membership or combination rule produced by an automated configuration search (`MOS-TRAIN-213`, `MOS-TRAIN-214`). Threshold selection MUST use the `tune` partition and the resulting threshold MUST be recorded on the EvaluationRun that reports test performance. Selection performed by a search MUST use the `tune` partition or cross-validation folds within `train` (`MOS-EVID-030`), and which was used MUST be recorded. Where any reported metric was computed on a partition that any of these selections was performed on, the ValidationReport MUST disclose that in the report document itself, in the `evaluation_run.selection` block; an undisclosed selected metric is byte-for-byte indistinguishable from a held-out one, which is the specific way this failure stays silent until the second site. This chapter adds no partition for the purpose — `train`, `tune` and `test` are sufficient and `val` remains not a permitted value. CI MUST assert that no EvaluationRun in a ValidationReport has `threshold_selected_on == partition`, and that no ValidationReport citing a run on partition P omits the disclosure when that run's search provenance names P as its `selection_partition`.

#### 7.4.3 Leakage checks

**MOS-EVID-034** The following five checks MUST be executed at freeze time and their results stored verbatim in `leakage_report`. A split MUST NOT be frozen while any check is `fail` and unwaived.

| id | Check | Pass condition | Catches |
|---|---|---|---|
| L1 | Patient disjointness | `patient_key` sets of any two non-`excluded` partitions are disjoint | the basic error |
| L2 | Study disjointness | every `study_instance_uid` appears in exactly one partition | a patient re-registered under two MRNs within one source |
| L3 | Pixel-identity duplicates | no `series_pixel_digest` occurs in two partitions | the same images re-anonymised with fresh UIDs (endemic in public collections) |
| L4 | Near-duplicate images | for every cross-partition series pair, 64-bit dHash Hamming distance of the normalised mid-axial slice > 6 | the same acquisition re-reconstructed, or a re-scan minutes apart |
| L5 | Accession disjointness | every non-null `accession_number_hash` appears in exactly one partition | one study ingested twice through different routes |

```python
# medicalos/evidence/leakage.py
def dhash64(mid_slice_hu: "np.ndarray") -> int:
    import numpy as np
    from scipy.ndimage import zoom
    x = np.clip(mid_slice_hu, -1000.0, 400.0)
    x = (x + 1000.0) / 1400.0
    f = [9 / x.shape[0], 8 / x.shape[1]]
    small = zoom(x, f, order=1)[:9, :8]
    bits = (small[1:, :] > small[:-1, :]).flatten()
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return v

def l4_near_duplicates(series_by_partition, max_hamming=6):
    hits = []
    parts = list(series_by_partition)
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            for a in series_by_partition[parts[i]]:
                for b in series_by_partition[parts[j]]:
                    if bin(a.dhash ^ b.dhash).count("1") <= max_hamming:
                        hits.append({"a": a.series_instance_uid, "b": b.series_instance_uid,
                                     "partitions": [parts[i], parts[j]],
                                     "hamming": bin(a.dhash ^ b.dhash).count("1")})
    return hits
```

**MOS-EVID-035** `accession_number_hash` MUST be `HMAC-SHA256(tenant_salt, AccessionNumber)` truncated to 10 bytes, base32-encoded; the raw accession number MUST NOT be stored in the split manifest.

**MOS-EVID-036** A check MAY be waived only by writing a `waiver` object into `leakage_report` carrying `{check_id, waived_by, waived_at, rationale, affected_pairs}`. A waiver MUST be reproduced in full in any ValidationReport that cites the split. There is no silent waiver.

**MOS-EVID-037** L4 MUST be computed with the mid-axial slice of the canonical volume (Chapter 4) so that it is orientation-invariant, and MUST be skipped with an explicit `"skipped_reason"` rather than silently passed when a series has fewer than 3 instances.

### 7.5 `AnnotationSet` — reader identity and consensus rule

On LIDC-IDRI, whether you score against one reader, a ≥2 consensus, the union or STAPLE moves Dice more than any model change made in a year. The reference standard is therefore a first-class, frozen, digested object.

Chapter 12 §12.12 is the schema of record for `annotation_sets`, `annotations` and `annotation_readers`.

`annotation_sets`:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `ann_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `dataset_version_id`, `name` | `UNIQUE (dataset_version_id, name)` | which cohort it annotates |
| `capability_id` | `NOT NULL` | the clinical function it is a reference for |
| `label_definition_id` | `NOT NULL` | FK into `capability_concepts`, the platform's single coded-concept dictionary (Chapter 6 `MOS-REG-042`; Chapter 12 `MOS-STORE-256`) |
| `annotation_type` | ∈ `mask`, `bounding_box`, `point`, `case_label`, `measurement` | form of the reference |
| `consensus_rule` | ∈ `single_reader`, `majority_at_least_2`, `union`, `intersection`, `staple`, `arbitrated` | MOS-EVID-039; this six-value enum is owned here |
| `consensus_params` | `jsonb NOT NULL DEFAULT '{}'` | MOS-EVID-039 |
| `reader_count` | `integer NOT NULL`, `>= 1` | MOS-EVID-038 |
| `annotation_digest` | `NOT NULL`, `sha256:…` | content address (Chapter 12 column `manifest_digest`) |
| manifest location | `NOT NULL` | object store location of the annotation `.jsonl` |
| `reference_of_record` | `boolean NOT NULL DEFAULT false` | MOS-EVID-042 |
| `frozen_at`, `frozen_by` | `NOT NULL` | audit |

`annotation_readers` — the per-reader detail table required by MOS-EVID-038:

| Field | Constraint | Meaning |
|---|---|---|
| `annotation_set_id`, `reader_id` | `PK (annotation_set_id, reader_id)` | reader identity, pseudonymous, stable within the tenant |
| `role` | `NOT NULL` | `radiologist`, `resident`, `algorithm`, `registry_extract` |
| `years_experience` | nullable | reader experience |
| `board_certified` | nullable | reader qualification |
| `specialty` | nullable | e.g. `thoracic_radiology` |
| `tool` | `NOT NULL` | e.g. `3D Slicer 5.6.2 + SegmentEditor` |
| `instructions_uri` | `NOT NULL` | the instructions the reader worked from |
| `blinded_to` | `text[] NOT NULL DEFAULT '{}'` | `{'model_output','other_readers','clinical_report'}` |

**MOS-EVID-143** This chapter owns no code table. Every coded concept an evidence-plane object refers to — `annotation_sets.label_definition_id` above, and every concept reproduced in a `ValidationReport` — MUST resolve to a row of `capability_concepts`, the platform's single coded-concept dictionary (Chapter 6 `MOS-REG-042`, Chapter 12 `MOS-STORE-256`). No platform artifact of `kind = "code_dictionary"` is created, versioned or operated by this chapter, and no other chapter may cite Chapter 7 as the owner of one.

**MOS-EVID-038** Every `AnnotationSet` MUST name at least one reader in `annotation_readers`. An AnnotationSet with `reader_count = 0` MUST be refused.

**MOS-EVID-039** `consensus_rule` MUST be one of the six enumerated values. `single_reader` requires `reader_count = 1`; `majority_at_least_2` requires `reader_count >= 3` and MUST record `consensus_params.min_agreeing`; `staple` MUST record `consensus_params.{iterations, convergence_tol, initial_sensitivity, initial_specificity, rng_seed}`; `arbitrated` MUST record the arbitrator's `reader_id` in `consensus_params.arbitrator_id`.

**MOS-EVID-040** Annotation manifest line format, sorted by `(patient_key, study_instance_uid, series_instance_uid)`:

```json
{"patient_key":"pk_4tqv2n5z6bhc7wqa","study_instance_uid":"1.3.6.1.4.1.14519.5.2.1.6279.6001.298806137288633453246975630178","series_instance_uid":"1.3.6.1.4.1.14519.5.2.1.6279.6001.179049373636438705059720603192","reference":{"kind":"mask","uri":"s3://medos-evidence/ann/ann_01JB3X7K/pk_4tqv2n5z6bhc7wqa.seg.nrrd","digest":"sha256:c81fa2b7e94d0c53a16f8d2b7e0c4a95f36d1b8e2c7a409df51b6e83c2a7d419","geometry":"source","reference_volume_ml":286.4},"per_reader":[{"reader_id":"rdr_a1","uri":"s3://medos-evidence/ann/ann_01JB3X7K/readers/rdr_a1/pk_4tqv2n5z6bhc7wqa.seg.nrrd","digest":"sha256:2b7e0c4a95f36d1b8e2c7a409df51b6e83c2a7d419c81fa2b7e94d0c53a16f8d","volume_ml":291.0},{"reader_id":"rdr_b4","uri":"s3://medos-evidence/ann/ann_01JB3X7K/readers/rdr_b4/pk_4tqv2n5z6bhc7wqa.seg.nrrd","digest":"sha256:6e83c2a7d419c81fa2b7e94d0c53a16f8d2b7e0c4a95f36d1b8e2c7a409df51b","volume_ml":281.9}],"inter_reader":{"dice":0.938,"volume_difference_ml":9.1}}
```

**MOS-EVID-041** Reference masks MUST be stored in **source geometry** (Chapter 4), in NIfTI or NRRD with the source origin, spacing and direction preserved to within 1e-4. An annotation stored in model space MUST be refused.

**MOS-EVID-042** An AnnotationSet MUST NOT be derived, in whole or in part, from the output of the artifact being evaluated, nor from any artifact sharing weights, training data or a postprocessing chain with it. If the only available reference standard is an algorithmic one, the reader `role` MUST be `algorithm`, the algorithm MUST be named, and `reference_of_record` MUST be `false`. An `AcceptanceCriteria` gate (7.9) MUST refuse an AnnotationSet with `reference_of_record = false`.

**MOS-EVID-043** When `reader_count >= 2`, `inter_reader` agreement MUST be computed and persisted per case, and its cohort-level aggregate MUST appear in every ValidationReport citing the set. A model result is not interpretable without it: a Dice of 0.82 against a reference whose own inter-reader Dice is 0.84 is a different statement from the same 0.82 against an inter-reader Dice of 0.97.

**MOS-EVID-044** `reference_volume_ml` MUST be computed in source geometry using source `PixelSpacing` and `SliceThickness`, by the same code path that computes model volumes (Chapter 4). Two implementations MUST NOT exist.

**MOS-EVID-045** An `EvaluationRun` MUST bind exactly one `AnnotationSet`. Comparing a model against two reference standards is two runs.

**MOS-EVID-046** An AnnotationSet MUST cover every patient in the partition it is evaluated on. A case present in the split but absent from the annotation manifest MUST cause the run to fail, not to silently shrink `n`.

### 7.6 Metric conventions — the two that MUST be pinned

Both of the conventions below are irreversible the moment a number is published. They are fixed here, platform-wide, with the rationale, and they are recorded on every EvaluationRun so that a report read in five years is unambiguous.

#### 7.6.1 Convention 1 — aggregation of Dice

**MOS-EVID-047** The platform's primary segmentation figure is **mean-of-per-case Dice**, metric id `dice_mean_per_case`, defined as the unweighted arithmetic mean of per-case Dice over the cases eligible under MOS-EVID-051.

**MOS-EVID-048** Pooled Dice, metric id `dice_pooled`, defined as `2·Σᵢ|Aᵢ∩Bᵢ| / Σᵢ(|Aᵢ|+|Bᵢ|)` over the same eligible case set, MUST also be computed and persisted on every segmentation EvaluationRun.

**MOS-EVID-049** The unqualified word "Dice" MUST NOT be used as a field name, API key, report label or UI string. Every reported overlap figure MUST carry its metric id. A `ValidationReport` containing a key named `dice` MUST fail schema validation.

**MOS-EVID-050** An `AcceptanceCriteria` criterion MUST name `dice_mean_per_case` or `dice_pooled` explicitly; there is no default.

*Rationale (normative context, not a requirement).* Pooled Dice weights each case by its ground-truth volume plus its predicted volume. On a pleural-effusion cohort whose volumes span 10 mL to 2 L, the largest decile of cases contributes more than half the denominator, so pooled Dice can stay above 0.90 while the model scores near zero on every small effusion. Mean-of-per-case weights each patient equally, which is the unit in which clinical risk is actually borne, at the cost of higher variance on small structures. The clinical bar is a per-patient claim, so the per-patient aggregate is primary; pooled is retained because it is the figure most literature reports and its absence makes cross-study comparison impossible.

#### 7.6.2 Convention 2 — empty ground truth

**MOS-EVID-051** The platform's empty-ground-truth policy is `exclude_and_report_separately`, and it is the only policy permitted for a `vendor_evidence` or `site_acceptance` report in 0.2.0:

| Case | GT voxels | Predicted voxels | Per-case Dice | Counted in `dice_mean_per_case` |
|---|---|---|---|---|
| true positive | > 0 | > 0 | `2|A∩B|/(|A|+|B|)` | yes |
| missed | > 0 | 0 | `0.0` | **yes** |
| negative, clean | 0 | 0 | `null` | no — counted in the empty-GT block |
| negative, false positive | 0 | > 0 | `null` | no — counted in the empty-GT block |

```python
def case_dice(pred_mask, gt_mask):
    p, g = pred_mask.astype(bool), gt_mask.astype(bool)
    if g.sum() == 0:
        return None                      # empty GT: excluded from the Dice aggregate
    return float(2.0 * (p & g).sum() / (p.sum() + g.sum()))
```

**MOS-EVID-052** Every EvaluationRun whose cohort contains at least one empty-ground-truth case MUST persist and report the **empty-GT block**, and an `AcceptanceCriteria` set that constrains `dice_mean_per_case` without also constraining at least one empty-GT metric MUST be rejected as invalid at authoring time:

| Metric id | Definition |
|---|---|
| `empty_gt_case_count` | number of cases with zero reference voxels |
| `empty_gt_false_positive_rate` | fraction of those cases whose predicted volume exceeds `fp_volume_threshold_ml` |
| `empty_gt_mean_fp_volume_ml` | mean predicted volume over those cases |
| `empty_gt_p95_fp_volume_ml` | 95th percentile predicted volume over those cases |
| `empty_gt_max_fp_volume_ml` | maximum predicted volume over those cases |

**MOS-EVID-053** `fp_volume_threshold_ml` MUST be declared per capability in the `AcceptanceCriteria` and recorded on the run. For `pleural_effusion` the declared value is `10.0`.

*Rationale.* Scoring an empty-GT/empty-prediction case as 1.0 makes the headline number a function of cohort composition: if a fraction *f* of cases are negatives, mean Dice has a floor of *f* regardless of model quality, so a vendor can raise a published figure from 0.72 to 0.89 by enriching negatives and changing nothing about the model. Scoring 0.0 is unstably punitive — a single stray voxel on an otherwise perfect negative scores identically to missing a 1 L effusion — and equally cohort-dependent. Exclusion keeps Dice a pure measure of overlap quality on the cases where overlap is defined, and the empty-GT block keeps the false-positive behaviour visible rather than laundering it into the same average. The one asymmetry is deliberate: a *missed* finding (GT non-empty, prediction empty) scores 0.0 and is always counted, because that is a failure of the thing being measured.

#### 7.6.3 Metric registry

**MOS-EVID-054** Every metric the platform computes MUST be registered in the table below with a stable id. An EvaluationRun MUST NOT persist a metric id absent from the registry, and the registry version MUST be recorded on the run.

| Metric id | Applies to | Per-case | Needs threshold | Unit | Aggregation |
|---|---|---|---|---|---|
| `dice_mean_per_case` | segmentation | yes | no | 1 | mean over eligible cases |
| `dice_pooled` | segmentation | no | no | 1 | pooled formula, MOS-EVID-048 |
| `iou_mean_per_case` | segmentation | yes | no | 1 | mean |
| `hd95_mm` | segmentation | yes | no | mm | median over eligible cases |
| `assd_mm` | segmentation | yes | no | mm | median |
| `volume_error_ml` | segmentation | yes | no | mL | mean (signed), reported with mean absolute |
| `volume_ape` | segmentation | yes | no | 1 | median absolute percentage error |
| `sensitivity` | classification, detection | no | **yes** | 1 | count-based over cases |
| `specificity` | classification | no | **yes** | 1 | count-based over cases |
| `ppv` | classification, detection | no | **yes** | 1 | count-based |
| `froc_sensitivity` | detection | no | **yes** (as FP/scan) | 1 | interpolated at declared FP/scan points |
| `auroc` | classification | no | no | 1 | over case scores |
| `auprc` | classification | no | no | 1 | over case scores |
| `ece_15bin` | classification | no | no | 1 | 15 equal-width bins |
| `brier` | classification | yes | no | 1 | mean |
| `mae` | measurement | yes | no | metric unit | mean |
| `empty_gt_false_positive_rate` | segmentation | no | via volume threshold | 1 | count-based |

**MOS-EVID-055** A `sensitivity`, `specificity`, `ppv` or `froc_sensitivity` value MUST NOT be persisted, reported or published without the operating point that produced it. The persisted form is `{"metric":"sensitivity","value":0.912,"operating_point":{"name":"effusion_probability","value":0.50,"selected_on":"tune"}}`. A number without an operating point is not a measurement and the schema MUST reject it.

**MOS-EVID-056** Every aggregate MUST be persisted and reported with four companions: `n` (eligible cases), `n_patients`, a confidence interval, and the convention block. A bare scalar metric MUST fail schema validation. This is the rule that makes the previous version's `dice: 0.91` — no cohort, no n, no CI, no run — structurally impossible.

#### 7.6.4 Uncertainty

**MOS-EVID-057** Confidence intervals MUST be computed by **patient-level cluster bootstrap**: resample `patient_key` with replacement, take all cases belonging to each drawn patient, recompute the statistic. Resampling cases independently MUST NOT be used, because multiple studies of one patient are not independent observations.

**MOS-EVID-058** The default is the percentile method with `B = 2000` and `ci_method: "percentile_cluster_bootstrap"`. `bca_cluster_bootstrap` MAY be declared instead. The RNG seed MUST be recorded on the run and the interval MUST be reproducible from the persisted per-case rows alone.

```python
# medicalos/evidence/ci.py
import numpy as np

def cluster_bootstrap_ci(values, patient_keys, statistic=np.mean,
                         b=2000, seed=20260101, alpha=0.05):
    rng = np.random.default_rng(seed)
    groups: dict[str, list[float]] = {}
    for v, pk in zip(values, patient_keys):
        groups.setdefault(pk, []).append(float(v))
    keys = list(groups)
    n = len(keys)
    boot = np.empty(b, dtype=float)
    for i in range(b):
        drawn = rng.integers(0, n, n)
        sample = [v for j in drawn for v in groups[keys[j]]]
        boot[i] = statistic(sample)
    lo, hi = np.quantile(boot, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(lo), float(hi)
```

**MOS-EVID-059** For count-based metrics (`sensitivity`, `specificity`, `ppv`) the same cluster bootstrap MUST be applied to the indicator variables; a Wilson or Clopper–Pearson interval MUST NOT be used when a patient contributes more than one case, because it assumes independence that the cohort does not have.

**MOS-EVID-060** `dice_pooled` has no per-case decomposition and therefore MUST be bootstrapped by resampling patients and recomputing the pooled numerator and denominator from the persisted per-case intersection and volume counts, which MUST therefore be persisted (7.7.2).

### 7.7 `EvaluationRun`

#### 7.7.1 The binding

**MOS-EVID-061** An `EvaluationRun` MUST bind every input that can change a number. A run missing any field in the table below MUST NOT reach `state = SUCCEEDED`.

| Field | Meaning |
|---|---|
| `service_version_id` \| `model_version_id` | the evaluated artifact; **exactly one** MUST be non-null, and which one is non-null is what "sealed subject" versus "native subject" means |
| `capability_id` | the clinical function evaluated |
| `kind` | which of the three evidence activities of MOS-EVID-003 this run belongs to |
| `dataset_version_id`, `dataset_version_digest` | the cohort |
| `split_id`, `split_digest`, `partition` | which part of it |
| `annotation_set_id`, `annotation_digest` | the reference standard |
| `preprocessing_spec_version`, `preprocessing_spec_digest` | native mode (Chapter 4) |
| `internal_pipeline_digest` | sealed mode, vendor-declared, opaque |
| `code_commit`, `code_dirty` | the evaluation harness |
| `image_digest` | OCI digest of the container that produced the predictions |
| `inference_backend` | `{kind, version}` e.g. `{"kind":"triton","version":"25.03"}` |
| `accelerator` | `{gpu_model, driver, cuda, trt}` |
| `operating_thresholds` | map of threshold name → value and the partition it was selected on |
| `metric_conventions` | 7.6 block, recorded verbatim |
| `metric_registry_version` | integer |
| `started_at`, `finished_at`, `runner` | provenance |
| `aggregate_metrics` | the canonicalised aggregate block |
| `run_digest` | digest over `aggregate_metrics` |
| `state` | ∈ `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `INVALIDATED` (MOS-EVID-014); `invalidation_reason` accompanies `INVALIDATED` |

Chapter 12 §12.12 is the schema of record for `evaluation_runs`. The constraints this chapter requires on that table are:

- exactly one of `service_version_id` and `model_version_id` is non-null;
- `model_version_id IS NOT NULL` implies `preprocessing_spec_digest IS NOT NULL`;
- `service_version_id IS NOT NULL` implies `internal_pipeline_digest IS NOT NULL`;
- `state = 'SUCCEEDED'` implies `code_dirty = false` (MOS-EVID-062);
- `UNIQUE (run_digest)`;
- a partial unique index over the full binding while `state = 'SUCCEEDED'`, on `(service_version_id, model_version_id, dataset_version_digest, split_digest, partition, annotation_digest, image_digest, coalesce(preprocessing_spec_digest, internal_pipeline_digest), operating_thresholds, metric_conventions)`, so that one binding yields one successful run and a second attempt is a duplicate, not a second opinion.

**MOS-EVID-062** `code_dirty = true` MUST block `state = SUCCEEDED`. An evaluation run from an uncommitted working tree is not evidence.

**MOS-EVID-063** For a run whose subject is a `service_version_id` the platform cannot see inside the container; the run MUST bind `internal_pipeline_digest` as declared by the publisher in the ServiceVersion manifest (Chapter 2), and the report MUST state that this digest is vendor-asserted rather than platform-verified.

**MOS-EVID-064** A TensorRT engine build, an ONNX export, a quantisation, or any other backend conversion produces a different numerical artifact. It MUST require a new `ModelVersion` and its own EvaluationRun; a run MUST NOT be carried across `inference_backend` or `accelerator.trt` values.

#### 7.7.2 Per-case persistence

**MOS-EVID-065** Per-case metrics MUST be persisted. Aggregates alone MUST NOT be stored without them.

Chapter 12 §12.12 is the schema of record for `evaluation_case_metrics` and `evaluation_case_scores`. Both are append-only and fully sealed.

`evaluation_case_metrics`, keyed `UNIQUE (evaluation_run_id, case_key, metric)`:

| Field | Constraint | Meaning |
|---|---|---|
| `evaluation_run_id` | FK | the run |
| `case_key` | `NOT NULL` | the evaluated case |
| `patient_key`, `study_instance_uid`, `series_instance_uid` | `NOT NULL` | cohort identity of the case; the series UID is what the pairing function of 7.9.2 joins on |
| `metric` | `NOT NULL` | registry metric id (7.6.3) |
| `value` | `double precision`, nullable | `NULL` = not defined for this case (e.g. empty-GT Dice) |
| `undefined_reason` | ∈ `empty_ground_truth`, `empty_prediction`, `excluded`, `error`; `CHECK (value IS NOT NULL OR undefined_reason IS NOT NULL)` | why the value is null |
| `eligible` | `boolean NOT NULL` | counted in the aggregate for this metric |
| `gt_voxels`, `pred_voxels`, `intersection_voxels` | `bigint NOT NULL` | MOS-EVID-068 |
| `gt_volume_ml`, `pred_volume_ml` | `double precision NOT NULL` | source-geometry volumes |
| `strata` | `jsonb NOT NULL` | MOS-EVID-067 |

`evaluation_case_scores`, keyed `PK (evaluation_run_id, case_key)`:

| Field | Constraint | Meaning |
|---|---|---|
| `patient_key` | `NOT NULL` | clustering unit for the bootstrap |
| `case_score` | `double precision` | classification: continuous score, pre-threshold |
| `case_label` | `smallint` | reference: 0 or 1 |
| `candidates` | `jsonb NOT NULL DEFAULT '[]'` | MOS-EVID-069 |

**MOS-EVID-066** `evaluation_runs.aggregate_metrics` MUST be recomputable from `evaluation_case_metrics` alone, to within 1e-9 relative, by the reference aggregation function. CI MUST assert this for every run. The `aggregates` array of an exported `report.json` (7.12.1) is the serialised form of this column and is bound by the same rule.

**MOS-EVID-067** `strata` MUST carry, at minimum: `slice_thickness_mm`, `pixel_spacing_mm_max`, `convolution_kernel_class`, `manufacturer`, `contrast_phase`, `patient_age_years`, `patient_sex`, and the capability's declared clinical strata (for `pleural_effusion`: `reference_volume_ml`). Subgroup criteria (7.8) are evaluated by filtering on this column; a stratum that is not persisted cannot be gated on.

**MOS-EVID-068** `gt_voxels`, `pred_voxels` and `intersection_voxels` MUST be persisted for every segmentation case even when the per-case metric is `NULL`, because `dice_pooled` and its bootstrap are computed from them.

**MOS-EVID-069** `candidates` MUST carry, for detection subjects, one object per predicted candidate: `{"score":0.83,"centroid_lps_mm":[-41.2,18.7,-122.5],"volume_ml":0.41,"matched_reference_id":"nod_7","match_distance_mm":2.9}`. Together with `case_score` this makes ROC, PR and FROC curves recomputable and **makes an operating threshold re-selectable without re-running inference**, which is the property that stops a threshold change from becoming a GPU project.

**MOS-EVID-070** A metric id present in `evaluation_case_metrics.metric` but absent from the registry version recorded on the run MUST cause the run to be marked `state = INVALIDATED`.

#### 7.7.3 The metrics-as-foreign-key rule

**MOS-EVID-071** `ModelVersion` and `ServiceVersion` MUST NOT carry a free-form metrics map. Any API request containing a writable field named `metrics`, `performance`, `accuracy`, `dice` or `sensitivity` on those resources MUST be rejected with RFC 9457 `class: schema_violation`. Published performance exists only as rows in `capability_claims`, and `model_versions.primary_evaluation_run_id` (Chapter 12 `MOS-STORE-263`) is a convenience pointer to the run behind the primary claim, never the only path to a number.

Chapter 12 §12.12 is the schema of record for `capability_claims`. The contract is:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `clm_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `subject_kind` | ∈ `service_version`, `model_version` | what the claim is about |
| `subject_id` | `uuid NOT NULL` | the version row the claim is about |
| `capability_id` | `NOT NULL` | the clinical function claimed |
| `evaluation_run_id` | `NOT NULL` FK | the run the number came from |
| `metric` | `NOT NULL` | registry metric id (7.6.3) |
| `value`, `ci_low`, `ci_high` | `double precision NOT NULL` | the figure and its interval (MOS-EVID-056) |
| `n_cases`, `n_patients` | `integer NOT NULL` | the cohort behind it |
| `operating_point` | `jsonb`, non-null for threshold metrics (MOS-EVID-055) | the point that produced it |
| `is_primary` | `boolean NOT NULL DEFAULT false` | MOS-EVID-073 |
| — | `UNIQUE (subject_kind, subject_id, capability_id, metric, evaluation_run_id)`, partial `UNIQUE (subject_kind, subject_id, capability_id) WHERE is_primary`; append-only | |

**MOS-EVID-072** Every number displayed anywhere in the platform as a performance figure MUST be rendered from a `capability_claims` row and MUST link to its `evaluation_run_id`. Capability resolution (Chapter 6) reads `capability_claims.value`, never a manifest field.

**MOS-EVID-073** Exactly one claim per `(subject, capability)` MAY have `is_primary = true`; it is the figure shown in list views, and its EvaluationRun MUST be over a `test` partition.

**MOS-EVID-074** A ServiceVersion MUST NOT reach `lifecycle_status = VALIDATED` (Chapter 6, `MOS-REG-020` and `MOS-REG-021`, which own that enum and its transitions) while it has zero `capability_claims` rows for a capability it declares. Deployability follows from that status by `MOS-REG-055` filter F3; this chapter does not restate the status set.

### 7.8 `AcceptanceCriteria` — owned by the Capability, expressed executably

**MOS-EVID-075** The **clinical** bar belongs to the `Capability` and is versioned with it. The **engineering** bar (p95 latency, GPU memory ceiling, ResultBundle schema validity, throughput) belongs to the `ModelVersion`/`ServiceVersion` and is out of scope for this chapter except that its result is carried in the same report.

**MOS-EVID-076** `AcceptanceCriteria` MUST be a declarative document with a fixed grammar. It MUST NOT be an expression string evaluated by `eval`, a Python callable, a SQL fragment, or a template. The evaluator MUST be a total function over the grammar with no dynamic code path. The document and its evaluated cohort binding are persisted in `acceptance_criteria` (Chapter 12 §12.12, schema of record), whose partition column carries the value `test` and nothing else.

#### 7.8.1 Grammar

| Field | Type | Meaning |
|---|---|---|
| `spec.case_definition.positive_if` | selector | what counts as a reference-positive case |
| `spec.strata[].id` | string | stratum name |
| `spec.strata[].selector` | map of field → `{op, value}` | filter over `evaluation_case_metrics.strata`; `{}` = all cases |
| `spec.combine` | `all_of` | only value in 0.2.0 |
| `spec.absolute[]` | criterion | evaluated against the candidate run |
| `spec.regression[]` | criterion | evaluated against the paired candidate/incumbent runs |
| criterion `.id` | string | stable, appears in the verdict |
| criterion `.metric` | registry id | 7.6.3 |
| criterion `.stratum` | stratum id | — |
| criterion `.operating_threshold` | `{name, value}` | required iff the metric requires a threshold |
| criterion `.bound` | `point` \| `ci_lower_95` \| `ci_upper_95` | which statistic the comparison uses |
| criterion `.op` | `>=` \| `>` \| `<=` \| `<` | — |
| criterion `.value` | number | — |
| criterion `.margin` | number | regression criteria only: non-inferiority margin δ |
| criterion `.min_cases` | integer | — |
| criterion `.min_patients` | integer | — |
| criterion `.on_insufficient_cases` | `indeterminate` \| `fail` | — |
| criterion `.severity` | `blocking` \| `advisory` | advisory criteria are reported, never gate |

`op` values permitted on a selector: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not_in`.

**MOS-EVID-077** `bound` MUST default to `ci_lower_95` for any criterion of the form "performance at least X". Gating on a point estimate is a defect: on 40 cases the point estimate crosses a fixed bar on noise alone.

**MOS-EVID-078** A criterion whose `metric` requires a threshold (7.6.3) MUST declare `operating_threshold`, and that threshold MUST be present in `evaluation_runs.operating_thresholds` with a matching value, or the evaluation returns `INDETERMINATE` with reason `threshold_mismatch`.

**MOS-EVID-079** Criteria authoring MUST be validated against the grammar at write time. An unknown field, an unknown metric id, a stratum referencing a field absent from the run's `strata` keys, or a `dice_mean_per_case` criterion without a companion empty-GT criterion (MOS-EVID-052) MUST be rejected.

#### 7.8.2 Worked example

```yaml
apiVersion: medicalos.io/v1
kind: AcceptanceCriteria
metadata:
  capability_id: pleural_effusion
  version: 3
  effective_from: "2026-04-01T00:00:00Z"
  authored_by: "clinical-council@medicalos.example"
  supersedes: 2
spec:
  combine: all_of
  fp_volume_threshold_ml: 10.0
  case_definition:
    positive_if: { reference_volume_ml: { op: ">=", value: 10.0 } }
  strata:
    - id: all
      selector: {}
    - id: gt_positive
      selector: { reference_volume_ml: { op: ">=", value: 10.0 } }
    - id: gt_empty
      selector: { reference_volume_ml: { op: "<", value: 10.0 } }
    - id: small_effusion
      selector: { reference_volume_ml: { op: "<", value: 100.0 } }
    - id: thick_slice
      selector: { slice_thickness_mm: { op: ">", value: 3.0 } }
    - id: sharp_kernel
      selector: { convolution_kernel_class: { op: "==", value: "sharp" } }
  absolute:
    - id: sens_all
      metric: sensitivity
      stratum: all
      operating_threshold: { name: effusion_probability, value: 0.50 }
      bound: ci_lower_95
      op: ">="
      value: 0.90
      min_cases: 120
      min_patients: 100
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: spec_all
      metric: specificity
      stratum: all
      operating_threshold: { name: effusion_probability, value: 0.50 }
      bound: ci_lower_95
      op: ">="
      value: 0.85
      min_cases: 120
      min_patients: 100
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: dice_positive
      metric: dice_mean_per_case
      stratum: gt_positive
      bound: ci_lower_95
      op: ">="
      value: 0.75
      min_cases: 60
      min_patients: 50
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: fp_on_negatives
      metric: empty_gt_false_positive_rate
      stratum: gt_empty
      bound: ci_upper_95
      op: "<="
      value: 0.10
      min_cases: 40
      min_patients: 35
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: volume_agreement
      metric: volume_ape
      stratum: gt_positive
      bound: ci_upper_95
      op: "<="
      value: 0.15
      min_cases: 60
      min_patients: 50
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: sens_small
      metric: sensitivity
      stratum: small_effusion
      operating_threshold: { name: effusion_probability, value: 0.50 }
      bound: ci_lower_95
      op: ">="
      value: 0.70
      min_cases: 30
      min_patients: 25
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: dice_thick_slice
      metric: dice_mean_per_case
      stratum: thick_slice
      bound: ci_lower_95
      op: ">="
      value: 0.65
      min_cases: 25
      min_patients: 20
      on_insufficient_cases: indeterminate
      severity: advisory
  regression:
    - id: ni_dice_positive
      metric: dice_mean_per_case
      stratum: gt_positive
      margin: 0.02
      bound: ci_lower_95
      min_cases: 60
      min_patients: 50
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: ni_sens_all
      metric: sensitivity
      stratum: all
      operating_threshold: { name: effusion_probability, value: 0.50 }
      margin: 0.03
      bound: ci_lower_95
      min_cases: 120
      min_patients: 100
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: ni_sens_small
      metric: sensitivity
      stratum: small_effusion
      operating_threshold: { name: effusion_probability, value: 0.50 }
      margin: 0.05
      bound: ci_lower_95
      min_cases: 30
      min_patients: 25
      on_insufficient_cases: indeterminate
      severity: blocking
    - id: no_catastrophic
      metric: dice_mean_per_case
      stratum: gt_positive
      margin: 0.20
      bound: catastrophic_count
      op: "<="
      value: 2
      min_cases: 60
      min_patients: 50
      on_insufficient_cases: indeterminate
      severity: blocking
```

#### 7.8.3 Tenant binding

**MOS-EVID-080** The Capability owns the criteria. A tenant MUST bind them to a concrete cohort before they can be evaluated. The binding is one row per `(tenant, capability)`; Chapter 12 §12.12 is the schema of record for the evidence plane and MUST carry it as `tenant_acceptance_bindings`:

| Field | Constraint | Meaning |
|---|---|---|
| `tenant_id`, `capability_id` | `PK (tenant_id, capability_id)` | one live binding per capability per tenant |
| `criteria_version` | `integer NOT NULL` | which `AcceptanceCriteria` version is bound |
| `dataset_version_id` | `NOT NULL` FK | the cohort |
| `split_id`, `partition` | `NOT NULL` | which part of it; `partition` uses the vocabulary of 7.4.1 |
| `annotation_set_id` | `NOT NULL` FK | the reference standard |
| `threshold_overrides` | `jsonb NOT NULL DEFAULT '{}'` | MOS-EVID-081 |
| `bound_by`, `bound_at` | `NOT NULL` | audit |

**MOS-EVID-081** `threshold_overrides` MAY only **tighten**. An override that relaxes any blocking criterion relative to the Capability's version MUST be rejected at write time, comparing in the direction of the criterion's `op`.

**MOS-EVID-082** A tenant MUST NOT bind a `DatasetVersion` whose `Dataset.purpose` is `training` or `tuning` to an acceptance binding.

**MOS-EVID-083** Evaluating an `AcceptanceCriteria` produces a `CriteriaVerdict`: `PASS`, `FAIL` or `INDETERMINATE`, plus a per-criterion result array. `INDETERMINATE` arises only from insufficient cases or a threshold mismatch, and it MUST be reported as its own state, never folded into `FAIL`, because the two require different actions — one needs more data, the other needs a different model.

**MOS-EVID-084** Advisory criteria MUST be evaluated and reported and MUST NOT affect the verdict.

### 7.9 Gated deployment as an executable rule

#### 7.9.1 Why the naive comparison is not a gate

The previous version's gate was "deploy v2; if validation fails, deployment blocked." The obvious implementation is `if new.dice < old.dice: block`. It fails in both directions and must be written down so nobody rediscovers it:

- **It blocks on noise, and more data does not help.** Take a rebuild of the identical model against a different TensorRT version. The true mean paired difference is zero; the observed mean difference is a random variable centred on zero. `P(observed mean < 0) = 0.5`. That probability is 0.5 for n = 40 and for n = 4000 — a point-estimate comparison against zero has a 50 % false-block rate at zero true effect, independent of sample size. A gate that blocks half of all harmless rebuilds is switched off within two weeks, and then there is no gate at all.
- **It passes a model that lost the clinically hardest subgroup.** On a pleural-effusion cohort where 85 % of positives exceed 100 mL, a model that fails completely on every effusion under 100 mL moves `dice_mean_per_case` by roughly 0.15 × 0.75 ≈ 0.11 in the worst case and far less when the small cases were already scoring poorly — and on a detection metric where small lesions are a 10 % minority, the aggregate shift is under 0.01, below any threshold anyone would author. The aggregate cannot see a subgroup collapse.

The gate therefore has three parts: absolute criteria, a **paired non-inferiority** regression test, and **subgroup floors** with a catastrophic-case count.

#### 7.9.2 Non-inferiority, formally

**MOS-EVID-085** A regression criterion MUST be evaluated as a one-sided paired non-inferiority test on the per-case differences `dᵢ = m_new,i − m_old,i`, over the cases present in **both** runs, with hypotheses `H₀: μ_d ≤ −δ` against `H₁: μ_d > −δ`. It passes when the lower bound of the one-sided 95 % patient-cluster bootstrap interval of `mean(d)` exceeds `−δ`.

**MOS-EVID-086** The candidate and incumbent EvaluationRuns MUST share `dataset_version_digest`, `split_digest`, `partition` and `annotation_digest`. If they do not, the regression criterion returns `INDETERMINATE` with reason `unpaired_runs`. An unpaired comparison is not a regression test.

```python
# medicalos/evidence/gate.py
import numpy as np
from medicalos.evidence.ci import cluster_bootstrap_ci

def paired_series(new_rows, old_rows, metric_id, stratum_pred):
    """Rows are evaluation_case_metrics records. Returns aligned per-case arrays."""
    n = {r.series_instance_uid: r for r in new_rows
         if r.metric == metric_id and r.eligible and stratum_pred(r.strata)}
    o = {r.series_instance_uid: r for r in old_rows
         if r.metric == metric_id and r.eligible and stratum_pred(r.strata)}
    uids = sorted(set(n) & set(o))
    return (uids,
            np.array([n[u].value for u in uids], dtype=float),
            np.array([o[u].value for u in uids], dtype=float),
            [n[u].patient_key for u in uids])

def non_inferiority(new_vals, old_vals, patient_keys, margin, b=2000, seed=20260101):
    d = new_vals - old_vals
    # one-sided 95% lower bound == 5th percentile of the bootstrap distribution
    lo, _ = cluster_bootstrap_ci(d, patient_keys, np.mean, b=b, seed=seed, alpha=0.10)
    return {"mean_delta": float(d.mean()), "ci_lower_95_one_sided": lo,
            "margin": margin, "passed": bool(lo > -margin), "n": int(d.size),
            "n_patients": len(set(patient_keys))}

def catastrophic_count(new_vals, old_vals, delta):
    return int(((old_vals - new_vals) > delta).sum())
```

**MOS-EVID-087** `margin` (δ) MUST be declared per metric in the Capability's `AcceptanceCriteria` and MUST be justified in a `margin_rationale` free-text field on the criteria document. δ is a clinical judgement about how much loss is tolerable, not a statistical parameter, and MUST NOT be tuned to make a specific candidate pass; a change to δ MUST bump `AcceptanceCriteria.version` and MUST re-open the gate for every deployment relying on it.

**MOS-EVID-088** A `bound: catastrophic_count` regression criterion MUST count cases where the incumbent exceeds the candidate by more than `margin` and compare that count against `value`. This catches the failure mode non-inferiority on the mean cannot: three cases collapsing from 0.85 to 0.05 while a hundred others improve slightly.

**MOS-EVID-089** More evidence MUST make it easier, not harder, to pass a regression criterion. Cluster bootstrap satisfies this: the interval narrows with n, so a genuinely equivalent model passes more reliably on a larger cohort. Any proposed replacement test MUST preserve this property.

#### 7.9.3 The gate function

**MOS-EVID-090** Transitioning a `Deployment` into `clinical_use_mode: clinical`, or changing the ServiceVersion behind a clinical deployment, MUST call `evaluate_gate()` and MUST refuse on `FAIL` or `INDETERMINATE`. A deployment in `research_only` mode MUST record the gate result but MAY proceed on `FAIL`, with the failure surfaced on the Deployment and in the UI.

```python
def evaluate_gate(candidate_report, incumbent_report, criteria, binding):
    """Returns (verdict, per_criterion_results). verdict in {PASS, FAIL, INDETERMINATE}."""
    results, verdicts = [], []

    # 0. Report integrity and subject binding.
    for chk, reason in [
        (verify_signature(candidate_report),               "signature_invalid"),
        (candidate_report.criteria_version == criteria.version, "criteria_version_mismatch"),
        (candidate_report.subject == binding.candidate_subject, "subject_mismatch"),
        (candidate_report.valid_until > now(),             "report_expired"),
        (candidate_report.run.dataset_version_id == binding.dataset_version_id, "wrong_cohort"),
        (candidate_report.run.partition == binding.partition, "wrong_partition"),
        (candidate_report.annotation_set.reference_of_record, "reference_not_of_record"),
    ]:
        if not chk:
            return "FAIL", [{"id": "integrity", "status": "FAIL", "reason": reason}]

    # 1. Absolute criteria.
    for c in criteria.absolute:
        r = eval_absolute(c, candidate_report.run)
        results.append(r)
        if c.severity == "blocking":
            verdicts.append(r["status"])

    # 2. Regression criteria — only when an incumbent exists.
    if incumbent_report is None:
        results.append({"id": "regression", "status": "SKIPPED",
                        "reason": "no_incumbent_first_deployment"})
    else:
        for c in criteria.regression:
            r = eval_regression(c, candidate_report.run, incumbent_report.run)
            results.append(r)
            if c.severity == "blocking":
                verdicts.append(r["status"])

    if "FAIL" in verdicts:
        return "FAIL", results
    if "INDETERMINATE" in verdicts:
        return "INDETERMINATE", results
    return "PASS", results
```

**MOS-EVID-091** Every invocation MUST be persisted, whatever the outcome. Chapter 12 §12.12 is the schema of record for the evidence plane and MUST carry this as `deployment_gate_decisions`, append-only:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `gd_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `deployment_id` | `NOT NULL` | the Deployment the decision was made for (Chapter 6 §6.8) |
| `capability_id`, `criteria_version` | `NOT NULL` | what was evaluated against what |
| `candidate_report_id` | `NOT NULL` FK to `validation_reports` | the candidate's evidence |
| `incumbent_report_id` | nullable FK to `validation_reports` | null on first deployment |
| `verdict` | ∈ `PASS`, `FAIL`, `INDETERMINATE` | MOS-EVID-083 |
| `criterion_results` | `jsonb NOT NULL` | one entry per criterion, including `SKIPPED` |
| `decided_at`, `decided_by` | `NOT NULL` | audit |
| `override` | `jsonb`, null unless MOS-EVID-093 applies | the recorded override |

**MOS-EVID-092** On `FAIL` or `INDETERMINATE` the incumbent ServiceVersion MUST remain live and serving; the gate MUST NOT take the capability out of service to block a candidate.

**MOS-EVID-093** A `FAIL` MUST NOT be overridable in `clinical` mode by any role. An `INDETERMINATE` MAY be overridden only by a user holding `deployment.gate.override`, only with a written rationale and a named approver, and the override object MUST be reproduced on the Deployment, in the audit trail, and on every ValidationReport subsequently issued for that deployment.

**MOS-EVID-094** A change to any of the following MUST re-open the gate: the ServiceVersion or ModelVersion, the PreprocessingSpec version, any operating threshold, the `AcceptanceCriteria` version, the tenant acceptance binding, the inference backend or accelerator class. A threshold change is a clinical change and is gated exactly like a weights change.

### 7.10 Applicability envelopes

#### 7.10.1 Schema

**MOS-EVID-095** Every ServiceVersion that declares a capability MUST carry an `ApplicabilityEnvelope` version. A ServiceVersion without one MUST NOT be deployable in `clinical` mode.

```yaml
apiVersion: medicalos.io/v1
kind: ApplicabilityEnvelope
metadata:
  subject_kind: service_version
  subject_id: pulmoai.effusion
  subject_version: 2.1.0
  version: 4
  derived_from_evaluation_run: evr_01JB4Q8T5XN7M2VDKC3PZR9HAE
  derivation: percentile_1_99
spec:
  constraints:
    - { attribute: slice_thickness_mm,      type: range,    min: 0.625, max: 3.0, marginal_max: 5.0 }
    - { attribute: pixel_spacing_mm_max,    type: range,    min: 0.488, max: 0.977, marginal_max: 1.2 }
    - { attribute: z_coverage_mm,           type: range,    min: 180.0, max: 450.0, marginal_min: 150.0 }
    - { attribute: instance_count,          type: range,    min: 90,    max: 750,  marginal_max: 1200 }
    - { attribute: patient_age_years,       type: range,    min: 18,    max: 95 }
    - { attribute: convolution_kernel_class, type: enum_in,  values: [soft, standard], marginal_values: [sharp] }
    - { attribute: manufacturer,            type: enum_in,  values: ["SIEMENS","GE MEDICAL SYSTEMS","Philips","CANON MEDICAL SYSTEMS"], marginal_values_allowed: true }
    - { attribute: contrast_phase,          type: enum_in,  values: [non_contrast, venous] }
    - { attribute: body_part_examined,      type: enum_in,  values: ["CHEST","THORAX"] }
  marginal_policy_default: flag
```

**MOS-EVID-096** A study attribute evaluates to exactly one zone: `IN` (inside `min`/`max` or in `values`), `MARGINAL` (inside `marginal_min`/`marginal_max` or in `marginal_values`, or any value when `marginal_values_allowed: true`), or `OUT`. The study's zone is the worst zone across all constraints. A missing attribute evaluates to `MARGINAL` with reason `attribute_absent`, never to `IN`.

#### 7.10.2 Derivation and the narrow-only rule

**MOS-EVID-097** Numeric constraint bounds MUST be derivable automatically from the `acquisition_profile` of the DatasetVersion behind `derived_from_evaluation_run`, using `derivation: percentile_1_99` (`min = p01`, `max = p99`) or `derivation: min_max`. Categorical `values` MUST be derived as the set of categories present in the cohort with at least 20 series.

**MOS-EVID-098** A publisher MAY narrow any derived bound freely. A publisher MUST NOT widen a bound beyond the derived value without a new EvaluationRun on a DatasetVersion whose `acquisition_profile` covers the widened range; the platform MUST reject a widened envelope that cites no such run. This is the rule that stops "validated on 2.5 mm archival data, declared for 0.6 mm" from being a manifest edit.

**MOS-EVID-099** `not_validated_for` entries on the ServiceVersion (Chapter 9) MUST be consistent with the envelope: every population or acquisition condition listed there MUST be `OUT` or absent from `values`. CI MUST assert this and fail the ServiceVersion publish on inconsistency.

#### 7.10.3 What happens when a study falls outside

**MOS-EVID-100** Envelope evaluation happens once, at triage, after `SeriesSelector` match and before job dispatch (Chapter 3). It MUST NOT be re-evaluated inside the service, and the service MUST NOT be able to influence it.

**MOS-EVID-101** Outcomes:

| Zone | Job outcome | Result annotation | DICOM |
|---|---|---|---|
| `IN` | proceeds | `applicability: in_envelope` | normal |
| `MARGINAL`, tenant `marginal_policy: flag` (default) | proceeds | `applicability: marginal` + failing attributes | SEG `SeriesDescription` prefixed `[OUTSIDE VALIDATED RANGE]`; SR carries a coded comment naming each attribute, observed value and bound |
| `MARGINAL`, tenant `marginal_policy: reject` | `REJECTED` | reason code | none written |
| `OUT` | `REJECTED` | reason code | none written |

**MOS-EVID-102** `REJECTED` for an envelope violation is a **clinical** outcome, not a failure (Chapter 5). It MUST be visually distinct from `FAILED` in every UI and MUST carry a structured reason object, one entry per violated constraint:

```json
{"class":"clinical_rejection","code":"ENVELOPE_VIOLATION","reason_code":"envelope.slice_thickness_out_of_range","attribute":"slice_thickness_mm","observed":6.0,"bound":{"max":3.0,"marginal_max":5.0},"subject":{"id":"pulmoai.effusion","version":"2.1.0"},"envelope_version":4,"message":"Series reconstructed at 6.0 mm; this service is validated to 3.0 mm (tolerated to 5.0 mm)."}
```

`class` and `code` are the RFC 9457 problem members of Chapter 10 (`MOS-API-037`): `code` is SCREAMING_SNAKE_CASE and is `ENVELOPE_VIOLATION` for every envelope rejection. The domain vocabulary of MOS-EVID-103 is carried in `reason_code` and MUST NOT be re-cased for transport.

**MOS-EVID-103** `reason_code` values are a closed set in 0.2.0, in lowercase dotted form:

| `reason_code` | Trigger |
|---|---|
| `envelope.slice_thickness_out_of_range` | `slice_thickness_mm` OUT |
| `envelope.pixel_spacing_out_of_range` | `pixel_spacing_mm_max` OUT |
| `envelope.coverage_insufficient` | `z_coverage_mm` OUT low |
| `envelope.instance_count_out_of_range` | `instance_count` OUT |
| `envelope.kernel_not_supported` | `convolution_kernel_class` OUT |
| `envelope.manufacturer_unvalidated` | `manufacturer` OUT |
| `envelope.contrast_phase_unsupported` | `contrast_phase` OUT |
| `envelope.body_part_mismatch` | `body_part_examined` OUT |
| `envelope.age_out_of_range` | `patient_age_years` OUT |
| `envelope.attribute_absent` | required attribute missing and `marginal_policy: reject` |

**MOS-EVID-104** Envelope decisions MUST be counted and exported per `(tenant, capability, service_version, reason_code)` for the monitoring signals of 7.13.4. A capability whose envelope rejects a large share of a site's real traffic is a procurement fact the site must be able to see on day one, not a silent drop.

### 7.11 Output plausibility checks

**MOS-EVID-105** Plausibility rules are declared per capability, versioned as a `PlausibilityRuleSet`, and evaluated **by the platform** on the returned `ResultBundle` — after the service returns and before any DICOM object is written (Chapter 4). A service MUST NOT evaluate its own plausibility rules and MUST NOT be able to set their outcome.

```yaml
apiVersion: medicalos.io/v1
kind: PlausibilityRuleSet
metadata:
  capability_id: pleural_effusion
  version: 2
spec:
  body_contour:
    hu_threshold: -300.0
    keep: largest_connected_component
    fill_holes: true
  rules:
    - { id: pl_volume_range,   type: volume_range,  target: effusion,  min_ml: 1.0,  max_ml: 4000.0, on_violation: fail }
    - { id: pl_in_thorax,      type: containment,   inner: effusion,   outer: thorax_mask, min_fraction_inside: 0.98, on_violation: fail }
    - { id: pl_in_body,        type: body_contour,  target: effusion,  max_fraction_outside_body: 0.001, on_violation: fail }
    - { id: pl_components,     type: component_count, target: effusion, min_component_ml: 5.0, max_components: 4, on_violation: warn }
    - { id: pl_score_range,    type: score_range,   target: effusion_probability, min: 0.0, max: 1.0, on_violation: fail }
    - { id: pl_measurement,    type: measurement_consistency, measurement: effusion_volume_ml, derived_from_mask: effusion, max_relative_deviation: 0.02, on_violation: fail }
    - { id: pl_laterality,     type: laterality_asymmetry, targets: [effusion_left, effusion_right], min_volume_ml: 50.0, max_ratio: 50.0, on_violation: warn }
    - { id: pl_lung_nonempty,  type: nonempty_expected, target: lung_mask, min_ml: 800.0, on_violation: fail }
```

**MOS-EVID-106** Rule types are a closed set in 0.2.0: `volume_range`, `containment`, `body_contour`, `component_count`, `score_range`, `measurement_consistency`, `laterality_asymmetry`, `nonempty_expected`. Adding a type is a platform change, not a manifest change.

**MOS-EVID-107** All plausibility geometry MUST be evaluated in **source geometry** against source `PixelSpacing` and `SliceThickness` (Chapter 4). Evaluating in model space MUST be refused.

**MOS-EVID-108** `body_contour` MUST be computed deterministically: threshold the canonical volume at `hu_threshold`, retain the largest 3-D connected component under 6-connectivity, fill holes slice-wise. The parameters are declared in the rule set; no other body-extraction method is permitted, because the rule's outcome must be reproducible from the persisted inputs.

**MOS-EVID-109** `measurement_consistency` MUST recompute the measurement from the shipped mask using the platform's own volume code and compare it with the value the service reported. This is the rule that catches the failure where the reported number and the shipped mask disagree — a number in millilitres always looks plausible, and only this check notices.

**MOS-EVID-110** Outcomes:

| `on_violation` | Effect |
|---|---|
| `warn` | Result stored; `plausibility_warnings[]` populated with `{rule_id, observed, bound}`; SR carries a coded comment; DICOM written normally; counter incremented |
| `fail` | **No DICOM object is written.** Job terminates `FAILED` with `failure.class: invalid_result_bundle` and `failure.code: output_implausible` plus the rule id (the class enum is closed — ch 5 §5.3.2 and `MOS-EXEC-016a` — and `code` is the open field); the `ResultBundle` is retained in quarantine storage for the tenant's debug retention period and is not exposed in any clinical view |

**MOS-EVID-111** A plausibility `fail` is a defect of the service's output, not a property of the study, and therefore MUST be `FAILED`, not `REJECTED`. The distinction matters operationally: `REJECTED` means "this study was not analysed and that is expected"; `failure.code: output_implausible` means "the service returned something wrong and someone must look at it."

**MOS-EVID-112** A rule MUST be calibrated before it is promoted: it MUST be evaluated over the subject's acceptance EvaluationRun, and a `fail`-severity rule that fires on more than 1 % of cases that pass the AcceptanceCriteria MUST NOT be promoted. The firing rate on the evaluation cohort MUST be recorded in the ValidationReport so that a site can distinguish "this rule never fires" from "this rule has never been exercised."

### 7.12 `ValidationReport`

#### 7.12.1 Field contract

Chapter 12 §12.12 is the schema of record for `validation_reports`. The facts Chapter 7 requires on the row are:

| Field | Constraint | Meaning |
|---|---|---|
| `id` | PK, `vr_<ULID>` | identity |
| `tenant_id` | `uuid NOT NULL`, RLS | owning tenant |
| `kind` | ∈ `vendor_evidence`, `site_acceptance`, `monitoring_period` (MOS-EVID-003, MOS-EVID-127) | which of the three activities produced it |
| `schema_version` | `NOT NULL`, e.g. `medicalos.io/validation-report/v1` | document schema |
| `subject_kind`, `subject_id`, `subject_version` | `NOT NULL` | the evaluated artifact |
| `capability_id`, `criteria_version` | `NOT NULL` | what bar it was measured against |
| `evaluation_run_ids` | non-empty | the runs it cites |
| `verdict` | ∈ `PASS`, `FAIL`, `INDETERMINATE` | MOS-EVID-083, MOS-EVID-114, MOS-EVID-142; a two-valued pass/fail column cannot represent this chapter's verdict and MUST NOT be substituted |
| `report_digest` | `NOT NULL`, `UNIQUE (tenant_id, report_digest)` | content address of `report.json` |
| `envelope_digest` | `NOT NULL` | the `ApplicabilityEnvelope` version cited |
| `bundle_uri`, `bundle_digest` | `NOT NULL` | the exported `tar.gz` of 7.12.3 |
| `signature`, `signer_key_id` | `NOT NULL` | DSSE envelope and key id, 7.12.2 |
| `approver` | `jsonb NOT NULL` | MOS-EVID-117; `approver_name` and `approver_role` are denormalised so the report keeps naming its approver after the account is gone (Chapter 12 `MOS-STORE-343`) |
| `issued_at`, `valid_until` | `NOT NULL` | MOS-EVID-115 |
| `status` | ∈ `ACTIVE`, `SUPERSEDED`, `REVOKED`, `EXPIRED`, default `ACTIVE` | MOS-EVID-014, MOS-EVID-126 |
| `superseded_by`, `revocation_reason` | nullable | lifecycle |
| `reproducibility_status` | ∈ `verifiable`, `degraded` (Chapter 12 `MOS-STORE-302`; set by `MOS-STORE-341` step 9) | mutable; set when erasure removes the cohort |

```json
{
  "schema_version": "medicalos.io/validation-report/v1",
  "report_id": "vr_01JB5C2M9QK4T7WX8ZPD3HRNFA",
  "kind": "vendor_evidence",
  "issued_at": "2026-05-12T09:14:07Z",
  "valid_until": "2027-05-12T09:14:07Z",
  "disclaimer": "This report states the measured behaviour of a pinned software artifact on a named, content-addressed cohort under the declared conventions. It is not a clinical validation, not a regulatory clearance, and not a statement of fitness for any patient population outside the declared applicability envelope.",
  "subject": {
    "kind": "service_version",
    "id": "pulmoai.effusion",
    "version": "2.1.0",
    "image_digest": "sha256:5c1a84f0e7b23d96a4f81c0e5b7d2a93f6c10b8e4d7a2f95c38e0b16d4a7f2c9",
    "legal_manufacturer": {"name": "PulmoAI s.r.o.", "id": "CZ-28471902", "address": "Brno, CZ"},
    "internal_pipeline_digest": "sha256:a4f81c0e5b7d2a93f6c10b8e4d7a2f95c38e0b16d4a7f2c95c1a84f0e7b23d96",
    "internal_pipeline_digest_attestation": "vendor_asserted"
  },
  "capability": {"id": "pleural_effusion", "criteria_version": 3},
  "cohort": {
    "dataset_version_id": "dsv_01JAY7N4K2ZP8QVCM3RXTD6WEB",
    "dataset_version_digest": "sha256:3f8b1d60c4a9e27f5b03d81a6c2e47f90b5d3a18c6f2e04b9d7a1c53e8b04f26",
    "dataset_name": "PulmoAI pleural effusion evidence cohort",
    "source_id": "pulmoai/multisite-2025",
    "licence_spdx": null,
    "licence_text": "Proprietary; de-identified under DPA 2025-11 with sites A,B,C.",
    "deidentification_status": "public_deidentified",
    "deid_policy_id": "ps315-basic+pixel-ocr/v4",
    "split_id": "spl_01JAY8P0R5T3XJ7NCB2VQM4KZD",
    "split_digest": "sha256:c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e47f90b5d3a18c6f2e04b9d7a1",
    "partition": "test",
    "n_patients": 163,
    "n_series": 163,
    "annotation_set_id": "ann_01JAY9Q1S6U4YK8PDC3WRN5LAF",
    "annotation_digest": "sha256:9d7a1c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e47f90b5d3a18c6f2e04b",
    "annotation_consensus_rule": "majority_at_least_2",
    "annotation_reader_count": 3,
    "reference_of_record": true,
    "inter_reader_dice_mean_per_case": 0.921,
    "leakage_report": {"L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass", "L5": "pass", "waivers": []}
  },
  "evaluation_run": {
    "id": "evr_01JB4Q8T5XN7M2VDKC3PZR9HAE",
    "code_commit": "7e41b2c8a95d3f06b18e4c7a2d905f3b6c81e024",
    "code_dirty": false,
    "inference_backend": {"kind": "sealed_container", "version": "2.1.0"},
    "accelerator": {"gpu_model": "NVIDIA A10", "driver": "550.90.07", "cuda": "12.4", "trt": "10.0.1"},
    "operating_thresholds": {"effusion_probability": {"value": 0.50, "selected_on": "tune"}},
    "selection": {
      "search_id": "cs_01JRB4K2Z7N9X3PQD5TWM8CVFA",
      "trials_completed": 12,
      "space_digest": "sha256:9e41c05b7d2a63f8104e9b3c57d8206af41c93e5b0d7a284f6c1e390b5d72a4c",
      "selection_metric": "dice_mean_per_case",
      "selection_partition": "tune",
      "selection_rule": "argmax over completed trials of dice_mean_per_case on the tune partition at operating point 0.50; ties broken by lowest trial_index",
      "selection_margin": 0.006,
      "headline_metric_selected_on": false
    },
    "metric_conventions": {
      "dice_aggregation": "mean_of_per_case",
      "empty_gt_policy": "exclude_and_report_separately",
      "fp_volume_threshold_ml": 10.0,
      "ci_method": "percentile_cluster_bootstrap",
      "bootstrap_b": 2000,
      "bootstrap_seed": 20260101,
      "metric_registry_version": 2
    },
    "run_digest": "sha256:04b9d7a1c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e47f90b5d3a18c6f2e"
  },
  "aggregates": [
    {"metric": "sensitivity", "stratum": "all", "value": 0.926, "ci_low": 0.879, "ci_high": 0.962, "n": 163, "n_patients": 163, "operating_point": {"name": "effusion_probability", "value": 0.50}},
    {"metric": "specificity", "stratum": "all", "value": 0.903, "ci_low": 0.851, "ci_high": 0.944, "n": 163, "n_patients": 163, "operating_point": {"name": "effusion_probability", "value": 0.50}},
    {"metric": "dice_mean_per_case", "stratum": "gt_positive", "value": 0.842, "ci_low": 0.811, "ci_high": 0.871, "n": 97, "n_patients": 97},
    {"metric": "dice_pooled", "stratum": "gt_positive", "value": 0.897, "ci_low": 0.874, "ci_high": 0.918, "n": 97, "n_patients": 97},
    {"metric": "volume_ape", "stratum": "gt_positive", "value": 0.094, "ci_low": 0.078, "ci_high": 0.117, "n": 97, "n_patients": 97},
    {"metric": "sensitivity", "stratum": "small_effusion", "value": 0.816, "ci_low": 0.714, "ci_high": 0.902, "n": 38, "n_patients": 38, "operating_point": {"name": "effusion_probability", "value": 0.50}},
    {"metric": "empty_gt_false_positive_rate", "stratum": "gt_empty", "value": 0.045, "ci_low": 0.008, "ci_high": 0.115, "n": 66, "n_patients": 66},
    {"metric": "empty_gt_p95_fp_volume_ml", "stratum": "gt_empty", "value": 4.1, "ci_low": 1.9, "ci_high": 9.7, "n": 66, "n_patients": 66}
  ],
  "criteria_results": [
    {"id": "sens_all", "status": "PASS", "bound": "ci_lower_95", "observed": 0.879, "op": ">=", "required": 0.90, "severity": "blocking", "note": "FAIL would be returned here; shown as PASS only when observed >= required"},
    {"id": "dice_positive", "status": "PASS", "bound": "ci_lower_95", "observed": 0.811, "op": ">=", "required": 0.75, "severity": "blocking"},
    {"id": "fp_on_negatives", "status": "PASS", "bound": "ci_upper_95", "observed": 0.115, "op": "<=", "required": 0.10, "severity": "blocking"}
  ],
  "regression_results": [],
  "applicability_envelope": {"version": 4, "digest": "sha256:18c6f2e04b9d7a1c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e47f90b5d3a"},
  "plausibility": {"rule_set_version": 2, "fire_rates": {"pl_components": 0.012, "pl_measurement": 0.0, "pl_in_thorax": 0.0}},
  "engineering": {"p95_latency_ms": 41200, "peak_gpu_memory_mib": 9840, "result_schema_valid_rate": 1.0},
  "approver": {"name": "Dr. Jana Novakova", "role": "Head of Clinical Affairs", "organisation": "PulmoAI s.r.o.", "statement": "I have reviewed the cohort composition, the reference standard and the measured results, and I approve publication of this evidence for the declared applicability envelope.", "approved_at": "2026-05-12T08:52:31Z", "identity_assurance": "oidc:pulmoai.example/sub/8f21c4"},
  "artifacts": {"case_metrics_csv_digest": "sha256:b5d3a18c6f2e04b9d7a1c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e47f90", "case_scores_csv_digest": "sha256:47f90b5d3a18c6f2e04b9d7a1c53e8b04f263f8b1d60c4a9e27f5b03d81a6c2e"},
  "verdict": "PASS"
}
```

**MOS-EVID-113** The `criteria_results` array MUST contain one entry per criterion in the bound `AcceptanceCriteria` version, including `SKIPPED` and `INDETERMINATE` entries. A report that omits a criterion MUST fail verification.

**MOS-EVID-114** `verdict` MUST be re-derivable from `criteria_results` by the published combination rule. A report whose stated verdict differs from the re-derived verdict MUST fail verification with a distinct exit code.

**MOS-EVID-115** `valid_until` MUST be set and MUST NOT exceed 24 months from `issued_at`. Report expiry is a freshness signal, not a safety mechanism; a site's policy for expired evidence is a site decision recorded in the deployment.

**MOS-EVID-116** A ValidationReport MUST NOT contain PHI: no `PatientID`, no `PatientName`, no dates of birth, no accession numbers, no institution-identifying free text beyond the declared `source_id`, and no SOP Instance UIDs. Per-case rows are keyed by re-keyed `patient_key` and a per-report series pseudonym (`case_0001`…), never by source UIDs.

**MOS-EVID-117** `approver` MUST name a real, identifiable human with a role and an organisation, MUST carry a written statement, and MUST carry an `identity_assurance` value tying the approval to an authenticated identity. An automated approver MUST NOT be permitted.

#### 7.12.2 Signing

**MOS-EVID-118** `report.json` MUST be canonicalised with JCS (MOS-EVID-008) and signed with Ed25519 inside a DSSE envelope over the Pre-Authentication Encoding `"DSSEv1" SP len(payloadType) SP payloadType SP len(payload) SP payload`:

```json
{
  "payloadType": "application/vnd.medicalos.validation-report+json",
  "payload": "eyJzY2hlbWFfdmVyc2lvbiI6Im1lZGljYWxvcy5pby92YWxpZGF0aW9uLXJlcG9ydC92MSIsIC4uLn0",
  "signatures": [
    {"keyid": "ed25519:b41f0c7a2d95e386", "sig": "kK9bR3xQ1mZ7pO2vL8tY4hN6cA0sF5jD2wE9uI3rT7gX1bV4nM8qC6lP0zJ5yH2aS4dG7fK1oB3eR9tU6iW5vQ=="}
  ]
}
```

**MOS-EVID-119** The signing key MUST be a publisher-held or tenant-held key managed per Chapter 8. The private key MUST NOT be held by the evaluation runner. Signing MUST be a distinct, separately-authorised step from running the evaluation, so that producing a run and attesting to it are different acts by different principals.

**MOS-EVID-120** Multiple signatures on one envelope MUST be supported: a `site_acceptance` report is typically signed by the site and countersigned by the platform operator. Verification MUST report which of the presented signatures verified against which trusted key, not merely "valid".

**MOS-EVID-121** Signing the model artifact but not the claim about it is backwards. Every ValidationReport MUST be signed, and an unsigned report MUST NOT be accepted by the gate (MOS-EVID-090).

#### 7.12.3 Bundle layout and offline verification

**MOS-EVID-122** A report MUST be exportable as a self-contained `tar.gz` requiring no network access and no MedicalOS instance to verify:

```
validation-report-vr_01JB5C2M9QK4T7WX8ZPD3HRNFA/
  report.json                  # JCS-canonical
  report.dsse.json             # DSSE envelope
  criteria.yaml                # the AcceptanceCriteria version evaluated
  envelope.yaml                # the ApplicabilityEnvelope version
  plausibility.yaml            # the PlausibilityRuleSet version
  case_metrics.csv             # one row per (case, metric); re-keyed pseudonyms
  case_scores.csv              # per-case score and reference label
  dataset_manifest.jsonl       # series-level, UIDs replaced by case pseudonyms
  split_manifest.jsonl
  annotation_manifest.jsonl    # digests and volumes only, no annotation payloads
  leakage_report.json
  figures/roc_pleural_effusion.svg
  figures/calibration_pleural_effusion.svg
  keys/publisher_ed25519.pub   # raw 32-byte public key, hex
  CHECKSUMS.sha256
  README.txt                   # MOS-EVID-002 disclaimer, verification command
```

**MOS-EVID-123** `medicalos-verify` MUST perform exactly these checks, in order, and MUST stop at the first failure:

| # | Check | Exit code on failure |
|---|---|---|
| 1 | Every file in `CHECKSUMS.sha256` present and matching | 2 |
| 2 | `report.dsse.json` payload decodes to bytes identical to `report.json` | 3 |
| 3 | At least one DSSE signature verifies against a key in the supplied trust bundle | 4 |
| 4 | `report.json` validates against the `schema_version` JSON Schema | 5 |
| 5 | Every aggregate in `report.json` recomputed from `case_metrics.csv` matches within 1e-9 relative, using the conventions in `metric_conventions` | 6 |
| 6 | Every criterion in `criteria.yaml` re-evaluated against the recomputed aggregates reproduces `criteria_results`, and the combination reproduces `verdict` | 7 |
| 7 | Digests in `report.json` for cohort, split, annotation set, envelope and plausibility rule set match the digests of the bundled files | 8 |

Exit code 0 means all seven passed. Exit code 1 is reserved for usage errors.

```
$ medicalos-verify report ./validation-report-vr_01JB5C2M9QK4T7WX8ZPD3HRNFA.tar.gz \
      --trust-bundle ./trusted_publishers.pem
[1/7] checksums                      ok (14 files)
[2/7] dsse payload == report.json    ok
[3/7] signature                      ok (ed25519:b41f0c7a2d95e386 -> "PulmoAI s.r.o. release key")
[4/7] schema medicalos.io/validation-report/v1  ok
[5/7] aggregates recomputed          ok (8 aggregates, max rel. delta 2.7e-16)
[6/7] criteria re-evaluated          ok (7 criteria, verdict PASS reproduced)
[7/7] input digests                  ok (cohort, split, annotations, envelope, plausibility)
VERDICT PASS  subject pulmoai.effusion@2.1.0  capability pleural_effusion  criteria v3
NOTE  revocation status NOT checked (offline). Report issued 2026-05-12, valid until 2027-05-12.
```

**MOS-EVID-124** Verification MUST NOT require network access, and the verifier MUST refuse to make any outbound connection unless `--check-revocation` is passed explicitly.

**MOS-EVID-125** Offline verification cannot detect revocation. The verifier MUST state this on every run, as shown above. A site MUST check `revocation_url` when connectivity exists, and a deployment gate running with connectivity MUST treat an unreachable revocation feed as `INDETERMINATE`, not as "not revoked".

**MOS-EVID-126** Revoking a report (`status = 'REVOKED'`) MUST NOT delete it and MUST NOT alter its bytes. Revocation is a separate signed statement referencing `report_digest`; the original remains verifiable, which is what makes "this evidence was once accepted and has since been withdrawn" an auditable fact rather than a hole.

### 7.13 The three-way split

The previous version conflated vendor validation, site installation and ongoing operation into one pipeline step named "Clinical validation." They are three activities with different actors, different inputs, different durations and different epistemic reach.

#### 7.13.1 Comparison

| | **Vendor evidence** | **Site acceptance testing** | **Continuous site monitoring** |
|---|---|---|---|
| `kind` | `vendor_evidence` | `site_acceptance` | `monitoring_period` |
| Performed by | ServiceVersion publisher (the manufacturer, Chapter 9) | The deploying site, automated by the platform | The platform, unattended |
| Frequency | Once per ServiceVersion, plus on any change in MOS-EVID-094 | Once per (site, ServiceVersion), and on upgrade | Continuous, reported per window |
| Cohort | Publisher's own multi-site evidence cohort, n ≥ 300 patients recommended | Site's own cases, 30–60 studies | All production traffic |
| Reference standard | Frozen `AnnotationSet`, `reference_of_record: true` | Site labels, incumbent agreement, or 20-case radiologist spot review | `ResultReview` outcomes only (Chapter 9) |
| Typical duration | Weeks to months | **Target ≤ 4 h wall clock, ≤ 30 min human time** | Ongoing; windows of 7 and 30 days |
| Output | Signed portable bundle (7.12.3) | Signed site report citing the vendor report | Signed period report + alerts |
| Gate it feeds | Publisher's own release decision | `evaluate_gate()` for this site's clinical deployment | Deployment `SUSPENDED` on hard breach |
| Can establish | Performance on the named cohort under the declared conventions | Integration correctness, envelope fit, gross mismatch | Drift, disagreement, failure rates |
| **Cannot establish** | Performance at any particular site | Any accuracy figure to within less than ~15 percentage points | Any accuracy figure at all |
| Is it clinical validation | **No** | **No** | **No** |

Both the publisher's evidence cohort and the site's acceptance cohort are `Dataset`s of `purpose: acceptance` (MOS-EVID-026); it is the report `kind` above, not the dataset purpose, that distinguishes the two activities.

**MOS-EVID-127** All three MUST be represented as `ValidationReport` rows differing only in `kind`, so that a site sees one object type with one verification procedure rather than three bespoke formats.

**MOS-EVID-128** A `site_acceptance` report MUST cite the `vendor_evidence` report it was performed against, by `report_digest`, and MUST fail to issue if that report does not verify offline at the site.

#### 7.13.2 Site acceptance test suite

**MOS-EVID-129** The suite MUST consist of exactly these five steps, executed in order, with each step's result recorded in the site report:

| Step | Contents | Automated | Typical time |
|---|---|---|---|
| SAT-1 | Offline verification of the vendor bundle (7.12.3, all seven checks) against the site's trust bundle | fully | < 1 min |
| SAT-2 | Golden-fixture determinism: run the fixtures shipped in the ServiceVersion through the **deployed** instance; per-fixture metric values MUST match the publisher's recorded values within the declared tolerance | fully | 10–40 min |
| SAT-3 | DICOM output battery on the fixture outputs (Chapters 4 and 14): validator clean, SEG round-trip Dice 1.0, geometry within 1e-4, SR hydrates with coded concepts and UCUM units, STOW/QIDO round-trip | fully | 5–15 min |
| SAT-4 | Site cohort check: 30–60 local studies; envelope coverage report; agreement against the site's declared reference; plausibility fire rates | partly | 1–3 h |
| SAT-5 | Operational bar: p95 end-to-end latency, concurrent job throughput, GPU headroom under the site's peak-hour profile | fully | 20–40 min |

This suite is the "site smoke suite" that Chapter 6's `MOS-REG-075` requires before a `Deployment` may move `VERIFYING → SERVING`; its outcome is what that chapter records in `deployment.verification_ref`.

**MOS-EVID-130** SAT-2 MUST compare against the publisher's recorded fixture values, not against "does it run". A ServiceVersion whose fixture outputs differ at the site has a different effective artifact at the site, whatever the image digest says, and MUST NOT be deployed to `clinical`.

**MOS-EVID-131** SAT-4's site cohort check MUST be reported as what it is. With n = 40 cases, the 95 % interval on a sensitivity estimate is roughly ±15 percentage points, so the check has no power to confirm or refute a vendor's sensitivity claim. Its report section MUST state, verbatim:

> This site cohort check is a detection test for gross mismatch, not a measurement of accuracy. At this sample size it can detect envelope mismatch, orientation or geometry errors, systematic volume bias above roughly 20 %, and catastrophic failure rates above roughly 10 %. It cannot detect a difference of a few percentage points in sensitivity or Dice, and it does not confirm the vendor's reported performance at this site.

**MOS-EVID-132** SAT-4 MUST report envelope coverage against the site's own traffic — the share of the site's recent studies falling `IN`, `MARGINAL` and `OUT` — because a service that rejects 40 % of a site's real recon protocols is a fact that must surface during acceptance, not during the first week of production.

**MOS-EVID-133** The suite MUST be runnable by a single operator action and MUST NOT require the site to author code, write YAML, or hold the vendor's data. Anything that pushes site acceptance past a working day turns into a rubber stamp.

**MOS-EVID-134** A failed SAT step MUST leave the deployment blocked and MUST NOT be overridable for `clinical_use_mode: clinical` except under MOS-EVID-093's `INDETERMINATE` path.

#### 7.13.3 Continuous site monitoring

**MOS-EVID-135** Monitoring MUST run unattended over production traffic and MUST emit a signed `monitoring_period` report per 30-day window per `(tenant, capability, service_version)`.

**MOS-EVID-136** Monitoring has **no ground truth**. Its only outcome signal is `ResultReview` (Chapter 9), and the reviewed subset is not a random sample of production. Every monitoring report MUST state the review coverage (`reviewed / produced`) and MUST NOT present a disagreement rate as an accuracy figure.

**MOS-EVID-137** The following signals MUST be computed, exported as metrics (Chapter 13) and carried in the period report:

| Signal | Window | Default alert | Default hard breach |
|---|---|---|---|
| `envelope_out_rate` | 7 d | > 0.10 or +3σ vs SAT-4 baseline | — |
| `envelope_marginal_rate` | 7 d | > 0.25 | — |
| `input_psi` per envelope attribute vs `acquisition_profile` | 30 d | > 0.15 | > 0.25 |
| `output_volume_psi` vs evaluation-cohort predicted-volume distribution | 30 d | > 0.15 | > 0.25 |
| `empty_output_rate` | 7 d | +3σ vs evaluation cohort | — |
| `plausibility_warn_rate` | 7 d | > 3× the rate recorded in the report | — |
| `plausibility_fail_rate` | 7 d | > 0.005 | > 0.02 |
| `review_disagreement_rate` (rejected + modified / reviewed) | 30 d | > capability-declared bound | > 2× that bound |
| `review_coverage` | 30 d | < 0.05 (signal is uninformative) | — |
| `p95_latency_ms` | 7 d | > 1.5× SAT-5 value | — |

**MOS-EVID-138** Population Stability Index MUST be computed with the fixed binning declared on the `acquisition_profile` (deciles of the evaluation cohort, with a floor of 1e-6 on empty bins), so that the number is comparable across windows and sites.

**MOS-EVID-139** An alert MUST notify and MUST NOT change deployment state. A **hard breach** MUST drive the `Deployment` to `state = SUSPENDED` (Chapter 6, `MOS-REG-072`–`MOS-REG-075`) for `clinical_use_mode: clinical`, MUST stop new job dispatch for that capability, and MUST NOT affect jobs already running.

**MOS-EVID-140** Recovery from `SUSPENDED` MUST require a human action carrying a rationale and MUST re-run SAT-1 through SAT-3 at minimum. Auto-recovery when a metric drifts back under a threshold MUST NOT be implemented.

**MOS-EVID-141** Monitoring signals are aggregate and MUST carry no PHI: labels are `tenant_id`, `capability_id`, `service_version`, `reason_code` only (Chapter 8).

**MOS-EVID-142** A `monitoring_period` report MUST NOT carry a `verdict` of `PASS`. Its verdict field MUST be `INDETERMINATE` with reason `no_reference_standard`, because monitoring cannot pass or fail an artifact against a clinical bar. Only `vendor_evidence` and `site_acceptance` reports carry `PASS`/`FAIL`.

### Acceptance criteria

Each check below is executable by a CI job or by a reviewer with database access. Failing any of them means this chapter is not implemented.

1. **Sealing is a database property.** Connect as the application role and attempt `UPDATE dataset_versions SET manifest_digest = 'sha256:0' WHERE id = <any>`. Expect a permission error or a trigger exception, not a successful update. Repeat for `dataset_splits.split_digest`, `annotation_sets.annotation_digest`, `evaluation_runs.run_digest`.
2. **Content addressing round-trips.** For every sealed `dataset_versions` row: fetch the manifest object named by its manifest location, recompute `manifest_digest()` over its lines, assert equality; assert the lines are in the canonical sort order of MOS-EVID-017; assert `manifest_line_count`, `patient_count`, `study_count`, `series_count` and `instance_count` equal the values derived from the manifest.
3. **No split is a seed.** Assert every `dataset_splits` row has a resolvable split manifest whose digest equals `split_digest`, and grep the repository for any code path that assigns a partition from an RNG at evaluation time. Expect zero hits.
4. **Patient-level assignment.** For every split, assert that no `patient_key` appears in two partitions (L1), that every `study_instance_uid` appears in exactly one (L2), and that `leakage_report` contains results for L1–L5 with no unwaived `fail`. Assert `dataset_split_members.partition` takes values only from `{train, tune, test, excluded}`.
5. **L3/L4 actually fire.** Inject a fixture split in which one series is duplicated across `train` and `test` with fresh UIDs. Assert L3 reports `fail`. Re-encode the same series with a different transfer syntax so the pixel digest differs and assert L4 reports it with Hamming distance ≤ 6.
6. **Annotation binding is total.** For every `state = SUCCEEDED` EvaluationRun, assert each patient in the evaluated partition has an entry in the bound annotation manifest. Assert the run fails rather than shrinking `n` when an entry is removed.
7. **Reference of record.** Attempt `evaluate_gate()` with an `AnnotationSet` whose `reference_of_record` is false. Expect `FAIL` with reason `reference_not_of_record`.
8. **No bare Dice.** Grep the OpenAPI document, the JSON Schemas and the UI locale files for a key or label exactly equal to `dice`, `Dice`, `accuracy` or `sensitivity` used as a scalar. Expect zero hits. Assert that posting `{"metrics":{"dice":0.91}}` to a service-version or model-version endpoint returns RFC 9457 `class: schema_violation`.
9. **Empty-GT convention holds.** Construct a fixture cohort with 10 non-empty and 10 empty ground-truth cases where the model predicts empty everywhere. Assert `dice_mean_per_case` is computed over 10 eligible cases and equals 0.0, that `empty_gt_case_count` is 10, and that `empty_gt_false_positive_rate` is 0.0. Change the model to predict a 50 mL blob on every case and assert `empty_gt_false_positive_rate` becomes 1.0 while `dice_mean_per_case` does not rise.
10. **Aggregates are recomputable.** For every `state = SUCCEEDED` EvaluationRun, recompute every value in `aggregate_metrics` from `evaluation_case_metrics` and assert agreement within 1e-9 relative.
11. **CIs are reproducible.** Recompute every confidence interval from the persisted per-case rows and the recorded `bootstrap_seed` and `bootstrap_b`; assert bit-identical bounds. Assert that resampling cases instead of patients produces a *different* interval on a fixture where one patient contributes four studies, proving clustering is actually applied.
12. **Thresholds are never missing.** Assert that no row in `capability_claims` whose `metric` is in {`sensitivity`,`specificity`,`ppv`,`froc_sensitivity`} has `operating_point IS NULL`, and that inserting one is rejected by constraint.
13. **Threshold selection did not touch test.** For every ValidationReport, assert `operating_thresholds[*].selected_on != evaluation_run.partition`.
14. **Dirty trees cannot produce evidence.** Attempt to transition an EvaluationRun to `state = 'SUCCEEDED'` with `code_dirty = true`. Expect a constraint violation.
15. **Backend conversion is a new version.** Attempt to attach an EvaluationRun produced with `accelerator.trt = "10.0.1"` to a ModelVersion whose other runs used `"9.3.0"` as a regression pair. Expect `INDETERMINATE` with reason `unpaired_runs` or an explicit backend-mismatch refusal.
16. **Criteria authoring is validated.** Submit an `AcceptanceCriteria` containing a `dice_mean_per_case` criterion and no empty-GT criterion. Expect rejection (MOS-EVID-052). Submit one referencing stratum field `scanner_serial` absent from persisted `strata`. Expect rejection.
17. **Tenant overrides only tighten.** Submit a `threshold_overrides` entry lowering `sens_all` from 0.90 to 0.85. Expect rejection. Submit one raising it to 0.93. Expect acceptance.
18. **The naive gate is absent.** Grep the gate implementation for a direct comparison of two aggregate metric values (`new.value < old.value` and equivalents). Expect zero hits on any blocking path.
19. **Non-inferiority tolerates noise.** Generate 200 synthetic paired runs with true zero difference and per-case paired-difference sd 0.05 on n = 40 patients. Assert the paired non-inferiority rule with δ = 0.02 passes in more than 90 % of them, and assert a point-estimate comparison against zero passes in approximately 50 % (within 45–55 %).
20. **Non-inferiority catches a real regression.** Generate a paired fixture where the candidate loses 0.30 Dice on every case with `reference_volume_ml < 100` (25 % of the cohort) and is unchanged elsewhere. Assert `ni_sens_small` and/or `ni_dice_positive` returns `FAIL`, and assert that the overall `dice_mean_per_case` point estimate moved by less than 0.08 — demonstrating that the aggregate alone would not have blocked it.
21. **Catastrophic count fires.** Generate a fixture where 3 cases drop by 0.35 and the mean difference is +0.01. Assert `no_catastrophic` returns `FAIL` while the mean-based non-inferiority criterion returns `PASS`.
22. **Insufficient data is INDETERMINATE, not PASS.** Run the gate against a 25-patient cohort with `min_patients: 100`. Assert verdict `INDETERMINATE` with reason `insufficient_cases`, that the deployment is blocked, and that `INDETERMINATE` is stored and displayed distinctly from `FAIL`.
23. **The gate is actually wired.** Attempt a Deployment transition to `clinical_use_mode: clinical` for a ServiceVersion with no ValidationReport. Expect refusal and a `deployment_gate_decisions` row with verdict `FAIL`, reason `signature_invalid` or `no_report`. Assert the incumbent remains serving throughout.
24. **FAIL is not overridable in clinical mode.** With a user holding `deployment.gate.override`, attempt to override a `FAIL` on a `clinical` deployment. Expect refusal. Repeat for `INDETERMINATE`. Expect acceptance with a persisted rationale and approver.
25. **Envelope cannot be widened silently.** Publish an envelope version whose `slice_thickness_mm.max` exceeds the `p99` of the cited run's `acquisition_profile`. Expect rejection. Narrow it below `p99` and expect acceptance.
26. **Envelope rejection is clinical, not a failure.** Submit a study reconstructed at 6.0 mm to a service whose envelope tops out at 3.0 mm (marginal 5.0 mm). Assert the Job terminates `REJECTED`, not `FAILED`; that the reason object matches MOS-EVID-102 field-for-field including `class: "clinical_rejection"`, `code: "ENVELOPE_VIOLATION"`, `reason_code: "envelope.slice_thickness_out_of_range"`, `observed: 6.0` and `bound.max: 3.0`; that no DICOM object was written; and that the UI renders it distinctly from a failure.
27. **Marginal is flagged, not silently accepted.** Submit a 4.0 mm study to the same service under `marginal_policy: flag`. Assert the job completes, the Result carries `applicability: marginal` with the attribute, and the generated SEG `SeriesDescription` begins with `[OUTSIDE VALIDATED RANGE]`.
28. **Missing attribute is not treated as in-envelope.** Strip `ConvolutionKernel` from a study and assert the zone is `MARGINAL` with reason `attribute_absent`, never `IN`.
29. **`not_validated_for` is consistent.** For every published ServiceVersion, assert every `not_validated_for` entry maps to an `OUT` zone in its envelope. Expect zero inconsistencies.
30. **Plausibility fails closed.** Return a `ResultBundle` whose effusion mask is 30 % outside the body contour. Assert the Job terminates `FAILED` with `failure.class: invalid_result_bundle` and `failure.code: output_implausible` plus rule id `pl_in_body` (the class enum is closed — ch 5 §5.3.2 and `MOS-EXEC-016a` — and `code` is the open field), that **no** SEG, SR or SC object exists in the PACS for that job, and that the bundle is retrievable only from quarantine.
31. **Measurement consistency catches the disagreement.** Return a bundle whose reported `effusion_volume_ml` is 900 while the shipped mask integrates to 1243 mL in source geometry. Assert `pl_measurement` fires with `fail` and no DICOM is written.
32. **Plausibility is platform-side.** Assert the service container has no code path that can set `plausibility_warnings` or suppress a rule; assert a bundle arriving with `plausibility_warnings` pre-populated is rejected as a contract violation.
33. **Uncalibrated rules cannot be promoted.** Attach a `fail`-severity rule that fires on 4 % of the acceptance run's passing cases. Expect refusal to promote the rule set (MOS-EVID-112).
34. **Reports verify offline.** In a container with no network route, run `medicalos-verify` on an exported bundle. Assert exit 0 and that all seven checks are printed. Assert that a bundle with one byte flipped in `case_metrics.csv` exits 2, a re-signed-but-tampered `report.json` exits 3 or 4, a doctored aggregate exits 6, and a doctored `verdict` exits 7.
35. **Verification is honest about revocation.** Assert the offline run prints the "revocation status NOT checked" note, and that a gate run with an unreachable `revocation_url` returns `INDETERMINATE`, not `PASS`.
36. **Reports carry no PHI.** Scan every exported bundle for DICOM UIDs matching the source study, for any 8-digit date, and for any string present in the source `PatientName`/`PatientID`. Expect zero hits.
37. **Approver is a human.** Assert every `validation_reports.approver` has non-empty `name`, `role`, `organisation`, `statement` and `identity_assurance`, and that the API rejects a report whose approver identity resolves to a service account.
38. **Signing is separated from running.** Assert the evaluation runner's credentials cannot sign: attempt to sign a report using the runner's identity and expect an authorisation failure.
39. **Revocation preserves bytes.** Revoke a report, then re-verify the original bundle offline. Assert it still verifies (exit 0) and that the platform reports it as `REVOKED` when online.
40. **The three kinds are distinct and correctly constrained.** Assert every `validation_reports` row has one of the three `kind` values; assert no `monitoring_period` row has `verdict = 'PASS'`; assert every `site_acceptance` row cites a `vendor_evidence` `report_digest` that verifies.
41. **Site acceptance is fast.** Execute SAT-1 through SAT-5 against the reference deployment on the reference hardware. Assert total wall-clock time is under 4 hours and that steps SAT-1, SAT-2, SAT-3 and SAT-5 required zero human input.
42. **SAT-2 compares values, not liveness.** Perturb the deployed service so one golden fixture's output Dice changes by 0.05. Assert SAT-2 fails and the deployment stays blocked.
43. **SAT-4 states its own limits.** Assert the generated site report contains the MOS-EVID-131 paragraph verbatim and contains an envelope-coverage table over the site's traffic.
44. **Monitoring drives suspension, alerts do not.** Drive `plausibility_fail_rate` to 0.01 and assert an alert with no state change; drive it to 0.03 and assert the Deployment moves to `state = SUSPENDED`, new dispatch stops, and a running job completes unaffected. Then drive it back to 0.0 and assert the deployment does **not** auto-recover.
45. **Monitoring labels carry no PHI.** Scrape the metrics endpoint and assert the label sets are exactly `{tenant_id, capability_id, service_version, reason_code}` with no additional labels.
46. **The forbidden claims are absent.** Run the MOS-EVID-005 grep across all source, template, locale and generated-artifact files. Expect zero hits outside this specification and outside publisher-supplied `regulatory_status` values.
47. **Every published number has a run behind it.** For every performance figure rendered by the UI or returned by the API, assert it resolves to a `capability_claims` row with a non-null `evaluation_run_id`, and that following that id yields a run in `state = SUCCEEDED` with per-case rows.
48. **This chapter declares no DDL.** Grep chapter 7 for `CREATE TABLE`. Expect zero hits: every evidence-plane table is declared once, in Chapter 12 §12.12, and every field contract here resolves to a column there.

---

[← 6. Registries, Capability Resolution and Deployment](06-registries.md) · [Index](../../MEDICALOS_SPEC.md) · [8. Security, Tenancy and PHI →](08-security.md)
