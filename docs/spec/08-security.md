<!-- MedicalOS Specification v0.4.0 — chapter 8 of 19. Normative.
     160 requirements. Do not edit without a requirement-ID review. -->

[← 7. Evidence Plane: Datasets, Evaluation and Validation Reports](07-evidence.md) · [Index](../../MEDICALOS_SPEC.md) · [9. Clinical Safety, Regulatory Posture and Provenance →](09-clinical-safety.md)

---

## 8. Security, Tenancy and PHI

MedicalOS holds other people's patients. Every control in this chapter exists because a specific failure would put one tenant's imaging, or one patient's identity, somewhere it does not belong. The previous version of this document stated that cross-tenant access "must be prevented" and left the mechanism to the implementer; this chapter states mechanisms, with the schemas, the SQL, the policy text and the decision records that make each one checkable by a test rather than by a reviewer's judgement.

Three rules govern the whole chapter and are repeated nowhere else:

- **MOS-SEC-001** — Every access decision MUST be deny-by-default. Absence of a grant is a denial; absence of a policy is a denial; absence of a tenant context is a denial; unavailability of the decision point is a denial.
- **MOS-SEC-002** — Every isolation guarantee MUST be enforced at the layer that stores or moves the data, not only at the layer that exposes it. An API check that is not backed by a database, network or credential boundary is documentation, not a control.
- **MOS-SEC-003** — Every security-relevant decision MUST leave a record that is joinable, by `job_id` and `trace_id`, to the execution that caused it. A control whose exercise cannot be reconstructed afterwards cannot be audited, and a control that cannot be audited cannot be claimed.

### 8.1 Scope, trust boundaries and threat model

#### 8.1.1 Trust zones

| Zone | Contains | Trusted to hold | Network reachability |
|---|---|---|---|
| `Z-PLATFORM` | Control Plane API, Job Dispatcher, Service Runner, Result Writer, PDP | Platform secrets, tenant KEK handles, signing keys | Ingress from `Z-EDGE`; egress to `Z-DATA`, `Z-GATEWAY`, `Z-INFER` |
| `Z-GATEWAY` | DICOM Gateway (ch. 3, MOS-DATA) | The single PACS credential, the de-identification UID mapping | Ingress from `Z-PLATFORM`, `Z-SERVICE`, `Z-EDGE`; egress to `Z-PACS` only |
| `Z-DATA` | PostgreSQL, object store, broker | All tenant metadata, results, artifacts | Ingress from `Z-PLATFORM` only |
| `Z-INFER` | Triton, GPU nodes | Model weights (native mode) | Ingress from `Z-PLATFORM` only |
| `Z-SERVICE` | `sealed`-mode service containers | Vendor weights; a Job Token | Ingress from `Z-PLATFORM`; egress to `Z-GATEWAY` and the platform callback only |
| `Z-PACS` | Hospital PACS / Orthanc | Source studies | Ingress from `Z-GATEWAY` only |
| `Z-EDGE` | OHIF, tenant browsers, tenant API clients | Nothing | Ingress to `Z-PLATFORM` and `Z-GATEWAY` API surfaces |

- **MOS-SEC-004** — A component MUST NOT open a network connection that crosses a zone boundary not listed above. This MUST be enforced by NetworkPolicy (Kubernetes) or by per-service networks (compose), not only by configuration of the client.
- **MOS-SEC-005** — No component outside `Z-GATEWAY` MUST hold a PACS credential. This is the binding form of the spine's rule that a service MUST NOT hold PACS credentials.

The mesh-internal control surface `/internal/v1/...` (ch. 3 study-arrival ingest callbacks, ch. 13 GPU-residency reservations and pseudonym resolution) lives inside `Z-PLATFORM` and `Z-GATEWAY` and MUST NOT be reachable from `Z-EDGE` (ch. 10, `MOS-API-001a`). It authenticates by workload identity or a component-scoped key, never by a tenant credential.

#### 8.1.2 Threats in scope

| # | Threat | Primary control | Requirement |
|---|---|---|---|
| T1 | Tenant A reads tenant B's metadata through a missing `WHERE tenant_id` | PostgreSQL RLS, forced, at one chokepoint | MOS-SEC-072 |
| T2 | Tenant A reads tenant B's pixels by querying the PACS directly | Gateway is the sole PACS reachability path; Job Token study scope | MOS-SEC-005, MOS-SEC-028 |
| T3 | A compromised or malicious `sealed` service exfiltrates a study | Egress deny-all, job-scoped token, no object-store or DB credential | MOS-SEC-029, MOS-SEC-087 |
| T4 | A third-party `native` model artifact executes code on shared GPU nodes | Native mode restricted to operator-signed artifacts; sealed mode for third parties | MOS-SEC-095 |
| T5 | PHI leaks into logs, traces, metrics or an LLM prompt | Typed log API, allowlisted attributes, egress gate | MOS-SEC-105, MOS-SEC-107 |
| T6 | A revoked user's long-running job keeps their access | Per-call evaluation; token scope is a ceiling | MOS-SEC-027, MOS-SEC-044 |
| T7 | A staging, suspended, shadow or research-only result reaches a clinical read path | Policy P-014 / P-015 at the read PEP | MOS-SEC-066 |
| T8 | An operator edits history to hide an access | Append-only at role level, hash chain, signed checkpoints | §8.9.3, MOS-SEC-151 |
| T9 | A tampered or unreviewed model artifact is deployed | Signature verification at registration, deployment and dispatch | MOS-SEC-138 |
| T10 | A forged claim about a model's performance | Signed ValidationReport and provenance, not only signed weights | MOS-SEC-139 |

#### 8.1.3 Out of scope

- **MOS-SEC-006** — MedicalOS MUST NOT claim to protect against a hostile hospital administrator with root on the deployment host, against physical access to GPU nodes, or against a compromised hypervisor. Documentation MUST state this.
- **MOS-SEC-007** — MedicalOS MUST NOT be represented as providing HIPAA, GDPR or MDR *compliance*. It provides controls a covered entity can use inside its own compliance programme. Marketing and product copy MUST use the spine's positioning line unchanged.

### 8.2 Principals and authentication

#### 8.2.1 Principal types

| Principal kind | Entity | Credential (0.1) | Credential (0.3+) | May act on behalf of |
|---|---|---|---|---|
| `user` | `User` | `ApiKey` | OIDC ID token → platform session JWT | — |
| `service_account` | `ServiceAccount` | `ApiKey` | OIDC client credentials | a `User` (explicit `obo`) |
| `workload` | platform component | mTLS workload certificate | same | — |
| `service_version` | `ServiceVersion` instance executing a job | `JobToken` | same | the job's `created_by` |
| `platform_admin` | `User` in the system tenant | `ApiKey` + break-glass grant | OIDC + break-glass grant | — |

- **MOS-SEC-008** — Every request reaching any PEP MUST resolve to exactly one principal of exactly one kind. Anonymous access MUST NOT exist on any surface except `/healthz`, `/readyz` and the OpenAPI document.
- **MOS-SEC-009** — Authentication MUST be implemented behind one `Authenticator` port with one method, `Authenticate(ctx, credential) (Principal, error)`. Adding OIDC in 0.3 MUST NOT require a change to any PEP.

#### 8.2.2 API keys (0.1+)

```
mos_<env>_<key_id>_<secret>
     │      │        └─ 32 random bytes, Crockford base32, 52 chars
     │      └─ 12 chars Crockford base32, the lookup column, not secret
     └─ prod | stg | dev
example: mos_prod_7Q2XK4M9AB0C_K7QW4M2X9A0BD1PCR8ZT3VYH6JNE5SFG2M4QXB0D
```

- **MOS-SEC-010** — An `ApiKey` secret MUST be stored as `argon2id(secret, salt, m=64MiB, t=3, p=1)`. The plaintext MUST be returned exactly once, at creation, and MUST NOT be recoverable.
- **MOS-SEC-011** — `api_keys` MUST carry `key_id` (unique, indexed), `tenant_id`, `principal_kind`, `principal_id`, `scope text[]`, `expires_at NOT NULL`, `last_used_at`, `created_by`, `revoked_at`, `source_ip_allowlist cidr[]`. Lookup MUST be by `key_id`; the hash MUST NOT be used as a lookup key.
- **MOS-SEC-012** — `expires_at` MUST be ≤ 365 days from creation. A key without an expiry MUST be rejected at creation.
- **MOS-SEC-013** — `scope` MUST be a subset of the permissions held by the principal the key belongs to, checked at creation and re-checked at every use (MOS-SEC-044).
- **MOS-SEC-014** — The `mos_` prefix MUST be registered as a secret-scanning pattern in the project's CI and in the public repository's push protection.
- **MOS-SEC-015** — A key MUST be revocable with effect within 5 seconds across all replicas. Implementations MUST NOT cache key validity longer than 5 s.

#### 8.2.3 OIDC / JWT (0.3+)

- **MOS-SEC-016** — Platform sessions MUST be OIDC Authorization Code with PKCE. Implicit flow MUST NOT be supported.
- **MOS-SEC-017** — Token validation MUST verify `iss` against the tenant's configured issuer, `aud` against the platform's registered client id, `exp`/`nbf` with ≤ 60 s skew, and the signature against a JWKS cached for ≤ 10 minutes. `alg: none` and symmetric algorithms MUST be rejected.
- **MOS-SEC-018** — The mapping from IdP claims to MedicalOS `Role` MUST be per-tenant configuration data in a table (`idp_role_mappings(tenant_id, claim, claim_value, role_id)`), never code. Unmapped claims MUST grant nothing.
- **MOS-SEC-019** — A tenant MUST be resolvable from the issuer alone. A token whose issuer maps to more than one tenant MUST be rejected.

#### 8.2.4 Workload identity

- **MOS-SEC-020** — Platform components MUST authenticate to each other with mTLS. A workload identity MUST be expressed as `spiffe://medicalos/<environment>/<component>` in the certificate SAN.
- **MOS-SEC-021** — The DICOM Gateway MUST authorize its callers by workload identity **and** by the presented tenant credential or Job Token. Workload identity alone MUST NOT grant study access.
- **MOS-SEC-022** — Certificates MUST have a lifetime ≤ 24 h and be rotated automatically. A component MUST fail readiness rather than serve with an expired certificate.

#### 8.2.5 How a `ServiceVersion` authenticates to the platform: the Job Token

The spine forbids a service from holding PACS credentials, from reaching the database, the broker or the object store, and from writing DICOM. It does not say what a service *does* hold. It holds exactly one thing: a Job Token. This section is the sole definition of that credential; chapters 2 and 3 describe what is done with it, not what it contains.

- **MOS-SEC-023** — At dispatch (ch. 5, MOS-EXEC), the platform MUST mint a `JobToken`: a compact JWS, `alg: EdDSA` (Ed25519), with these claims and no others.

```json
{
  "iss": "https://medicalos.internal/platform",
  "aud": ["medicalos-gateway", "medicalos-callback"],
  "sub": "svcver:pulmo.effusion@2.1.0",
  "jti": "jt_01JB8NQ7R4K2M0V8XA3ZP6WD5T",
  "iat": 1789297200,
  "exp": 1789298400,
  "tenant_id": "t_hosp_nord",
  "job_id": "job_01JB8NQ5V9TE7KH2M4RA0PXC8W",
  "deployment_id": "dep_01JB8H2K9Q",
  "mode": "sealed",
  "svc_image_digest": "sha256:3f9a1c07b2e4d8a95c1f7b60e3d2a4185f9c0b7e6d3a1c4f8b2e05d7a9c36148",
  "obo": {"kind": "user", "id": "u_7d2f4a", "role": "clinical_operator"},
  "scope": ["study.read_pixels", "result.submit"],
  "study_scope": ["st_01JB8M4R7Q"],
  "series_scope": ["se_01JB8M4R7Q_04", "se_01JB8M4R7Q_07"]
}
```

- **MOS-SEC-024** — `exp` MUST equal the job's `deadline_at` plus a 300 s grace and MUST NOT exceed 6 hours. The platform MUST reissue the token file on deadline extension; it MUST NOT issue a long-lived token to avoid reissue.
- **MOS-SEC-025** — The token MUST be delivered to the container as a file mounted at `/run/medicalos/job_token`, mode `0400`, owned by the container user. It MUST NOT be passed as an environment variable or a command-line argument, because both appear in `/proc`, crash dumps and container inspection output. Where a `sealed` service is invoked over HTTP rather than launched by the platform, the same token MAY instead be delivered as the `gateway.access_token` member of the execution request body (ch. 2, `MOS-SVC-047`); it MUST NOT be delivered any other way.
- **MOS-SEC-026** — The Gateway and the callback endpoint MUST reject a Job Token when any of the following hold: signature invalid; `exp` passed; `jti` present in the revocation set; the referenced `Job` is not in state `RUNNING`; the requested `StudyInstanceUID` does not resolve to a study id in `study_scope`; the requested series does not resolve into `series_scope`; the presented workload identity does not match the deployment's runner.
- **MOS-SEC-027** — `scope` is a **ceiling, not a grant**. Every call presenting a Job Token MUST still be evaluated against the current permissions of the `obo` principal and against the PolicySet. A scope entry whose underlying role grant has been revoked MUST stop working within 60 s.
- **MOS-SEC-028** — `study_scope` and `series_scope` MUST be populated from the triage selection pinned on the Job (spine section 6). A service MUST NOT be able to retrieve any instance the platform did not select for it. This is the requirement that makes "the service has no PACS credential" a real boundary rather than an indirection. A service principal's `scope` MUST NOT contain `study.write` or any other write verb: a service cannot STOW.
- **MOS-SEC-029** — A `sealed`-mode container MUST run with egress deny-all except the Gateway address and the platform callback address. A `service.yaml` MAY declare `network_egress:` destinations; declared egress MUST require explicit tenant approval recorded as an `AuditEvent` with action `deployment.create` and detail `{"egress_approved": [...]}`.
- **MOS-SEC-030** — The platform MUST call a `sealed` service over mTLS presenting its own workload certificate; the service MUST validate the SAN. The service MUST NOT accept unauthenticated invocations.

### 8.3 Authorization: one permission namespace, tenant-definable roles

#### 8.3.1 The namespace

The previous version carried two vocabularies — `dicom.study.read` in the tool contract and `study.read` in RBAC — with nothing joining them. There is now one.

- **MOS-SEC-031** — A permission identifier MUST match `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$`: a resource segment and an action segment, or a resource segment, a sub-resource segment and an action segment where the catalogue of section 8.3.2 registers one (`result.review.read/submit/assign/reopen`, `result.read.research`, `artifact.status.set`, `deployment.gate.override`). Four or more segments is a build error. The `dicom.*` and `fhir.*` spellings are deleted and MUST NOT appear in any manifest, role, policy or code. Segment count is not a grant: a three-segment identifier that is not a row of the catalogue is unknown and denies under MOS-SEC-033, exactly as an unregistered two-segment one does.
- **MOS-SEC-032** — The permission list MUST be generated from one source file, `medos/contracts/permissions.yaml`, into: Go constants, the Cedar action schema, the OpenAPI security scheme descriptions, and the seed-role migration. CI MUST fail if any generated file differs from a fresh generation. Every `permission:` value in ~~`medos/api/v1/routes.yaml`~~ either registry of `MOS-API-085` (ch. 10; **AMENDED at specification 0.4.0**) MUST be a key present in this file; this chapter is the only source of permission spellings, and every other chapter binds to them.
- **MOS-SEC-033** — A permission not present in `medos/contracts/permissions.yaml` MUST be treated as unknown and MUST deny. Wildcards MUST NOT be expressible in a `Role`, in a `JobToken.scope`, or in an `ApiKey.scope`.

Each permission carries a `class`. Class drives audit and caching, not grants.

| Class | Meaning | Always audited | PDP result cacheable |
|---|---|---|---|
| `read` | metadata read, no patient identifiers | no (sampled) | ≤ 5 s |
| `write` | non-clinical mutation | yes | ≤ 5 s |
| `phi` | touches class P2–P4 data (section 8.6.1) | yes | never |
| `clinical` | affects what a clinician sees or acts on | yes | never |
| `admin` | changes identity, roles, keys, tenancy | yes | never |
| `governance` | changes policy, evidence approval, audit | yes | never |

These six values are the complete `class` enum. Chapter 12 stores it and mirrors these spellings exactly.

#### 8.3.2 The complete permission list

| Permission | Grants | Class |
|---|---|---|
| `tenant.read` | read tenant settings | `read` |
| `tenant.create` | create a tenant (system tenant only) | `admin` |
| `tenant.update` | change tenant settings other than PHI policy | `admin` |
| `tenant.delete` | terminate a tenant, triggering full erasure | `admin` |
| `user.read` | list and read users | `read` |
| `user.invite` | create a user | `admin` |
| `user.update` | change a user's roles or status | `admin` |
| `user.delete` | remove a user (audit rows survive) | `admin` |
| `role.read` | read roles and their permissions | `read` |
| `role.create` | define a tenant role | `admin` |
| `role.update` | change a role's permission set | `admin` |
| `role.delete` | delete an unassigned role | `admin` |
| `api_key.read` | list key metadata (never the secret) | `read` |
| `api_key.create` | mint a key within own permissions | `admin` |
| `api_key.revoke` | revoke any key in the tenant | `admin` |
| `service_account.read` | list service accounts | `read` |
| `service_account.create` | create one | `admin` |
| `service_account.update` | change its roles | `admin` |
| `service_account.delete` | delete one | `admin` |
| `patient.read` | read the patient projection incl. identifiers | `phi` |
| `study.read` | read study/series/instance metadata | `phi` |
| `study.read_pixels` | retrieve pixel data through the Gateway | `phi` |
| `study.ingest` | push a study to the platform through the API (ch. 3, `MOS-DATA-056`) | `phi` |
| `study.write` | STOW generated objects into the PACS | `clinical` |
| `study.export` | download study data outside the platform | `phi` |
| `study.delete` | remove an ingested study from platform storage | `phi` |
| `capability.read` | read capabilities | `read` |
| `capability.create` | define a capability | `governance` |
| `capability.update` | change a capability, incl. its `AcceptanceCriteria` | `governance` |
| `capability.delete` | delete an unreferenced capability | `governance` |
| `service.read` | read services | `read` |
| `service.create` | register a service | `write` |
| `service.update` | change service identity/ownership | `write` |
| `service.delete` | delete a service with no versions | `write` |
| `service_version.read` | read versions and manifests | `read` |
| `service_version.publish` | publish a signed immutable version | `governance` |
| `service_version.approve` | accept a version for this tenant as the named human, `VALIDATED → APPROVED` (ch. 2, `MOS-SVC-106`) | `governance` |
| `service_version.deprecate` | mark deprecated / recalled | `governance` |
| `service_version.delete` | delete a never-deployed version | `governance` |
| `model.read` / `model.create` / `model.update` / `model.delete` | model catalogue | `read` / `write` / `write` / `write` |
| `model_version.read` | read a model version and its pinned `PreprocessingSpec` | `read` |
| `model_version.publish` | publish a signed model version | `governance` |
| `model_version.delete` | delete a never-deployed model version | `governance` |
| `artifact.publish` | publish an immutable `Artifact` of any kind (ch. 6, `MOS-REG-108`) | `governance` |
| `artifact.approve` | approve an artifact for use in a tenant | `governance` |
| `artifact.suspend` | suspend an artifact, stopping dispatch platform-wide | `governance` |
| `artifact.recall` | recall an artifact | `governance` |
| `artifact.status.set` | set an artifact's `lifecycle_status` (ch. 6, `MOS-REG-108`) | `governance` |
| `deployment.read` | read deployments | `read` |
| `deployment.create` | deploy a ServiceVersion into one `(tenant, environment, capability)` slot | `clinical` |
| `deployment.update` | change `traffic_permille`, `pin_range`, `promotion_policy`, `residency` | `clinical` |
| `deployment.promote` | `role` → `ACTIVE` (cutover, rollback, canary promotion) or `SUSPENDED` → `SERVING` | `clinical` |
| `deployment.suspend` | `state` → `SUSPENDED`; reversible, reason mandatory | `clinical` |
| `deployment.retire` | `state` → `DRAINING`; terminal | `clinical` |
| `deployment.approve_clinical` | be the named approver of the E1 clinical gate (ch. 9, `MOS-SAFE-036`) | `governance` |
| `deployment.gate.override` | override an `INDETERMINATE` deployment-gate verdict (ch. 7, `MOS-EVID-093`); never a `FAIL` | `governance` |
| `training_data_policy.record` | record the tenant's `TrainingDataPolicy` as the named human and set `training_use_allowed` with it (ch. 17, `MOS-TRAIN-072`, `MOS-TRAIN-073`) | `governance` |
| `harvest_batch.open` | open a `HarvestBatch` against a sealed `SamplingPlan` and draw its candidates (ch. 17, `MOS-TRAIN-078`, `MOS-TRAIN-083`) | `phi` |
| `harvest_candidate.read` | read `HarvestCandidate` rows and the settled decision on one (ch. 17, `MOS-TRAIN-079`) | `phi` |
| `curation_decision.record` | record a `CurationDecision` on a candidate as the named human (ch. 17, `MOS-TRAIN-080`) | `phi` |
| `corpus_stratification_report.read` | read a batch's `CorpusStratificationReport` (ch. 17, `MOS-TRAIN-088`) | `read` |
| `training_data_policy.read` | read whether the tenant's `TrainingDataPolicy` permits training use, and the instrument recorded (ch. 17, `MOS-TRAIN-072`; ch. 19, `MOS-UI-110`) | `read` |
| `sampling_plan.read` | read a versioned `SamplingPlan` and the `spec_digest` it was sealed under (ch. 17, `MOS-TRAIN-083`) | `read` |
| `sampling_plan.declare` | declare a versioned `SamplingPlan` before any candidate is drawn (ch. 17, `MOS-TRAIN-083`, `MOS-STORE-359`) | `write` |
| `harvest_batch.read` | read a `HarvestBatch`, its state and the composition of the candidate set drawn into it (ch. 17, `MOS-TRAIN-078`; ch. 19, `MOS-UI-115`) | `read` |
| `training_run.read` | read a `TrainingRun`, its state and its reproducibility binding (ch. 17, `MOS-TRAIN-124`) | `read` |
| `training_run.submit` | submit a `TrainingRun` against a sealed cohort and a frozen split (ch. 17, `MOS-TRAIN-122`, `MOS-TRAIN-124`) | `write` |
| `training_run.cancel` | cancel a `TrainingRun` in flight as the named human (ch. 17, `MOS-TRAIN-122`; ch. 19, `MOS-UI-156`) | `write` |
| `configuration_search.read` | read a `ConfigurationSearch`, its space, its budget and its nomination (ch. 17, `MOS-TRAIN-213`, `MOS-TRAIN-218`) | `read` |
| `configuration_search.declare` | register a `ConfigurationSearch` with a digested declarative space and a bounded budget (ch. 17, `MOS-TRAIN-220`, `MOS-TRAIN-234`) | `write` |
| `configuration_search.nominate` | nominate the one trial a search may register and evaluate on `test` (ch. 17, `MOS-TRAIN-216`, `MOS-TRAIN-217`) | `write` |
| `conversion_run.read` | read a `ConversionRun` and its equivalence result (ch. 17, `MOS-TRAIN-156`, `MOS-TRAIN-161`) | `read` |
| `conversion_run.submit` | submit a `ConversionRun` against a registered `ModelVersion` (ch. 17, `MOS-TRAIN-156`, `MOS-TRAIN-169`) | `write` |
| `dataset.read` / `dataset.create` / `dataset.update` / `dataset.delete` | dataset catalogue | `read` / `write` / `write` / `write` |
| `dataset_version.read` | read a sealed dataset version manifest | `phi` |
| `dataset_version.create` | seal a dataset version | `phi` |
| `dataset_version.delete` | delete a version not referenced by any `EvaluationRun` | `phi` |
| `dataset_split.read` | read a frozen `DatasetSplit` manifest | `phi` |
| `dataset_split.freeze` | freeze a patient-level `DatasetSplit` manifest (ch. 7, `MOS-EVID-028`) | `phi` |
| `annotation_set.read` / `.create` / `.update` / `.delete` | annotations | `phi` for all four |
| `evaluation.read` | read `EvaluationRun` incl. per-case metrics | `phi` |
| `evaluation.run` | start an evaluation | `write` |
| `evaluation.delete` | delete a run not referenced by a report | `governance` |
| `validation_report.read` | read a report | `read` |
| `validation_report.create` | generate a report from a run | `governance` |
| `validation_report.approve` | sign as the named human approver | `governance` |
| `validation_report.export` | export the signed bundle | `governance` |
| `validation_report.revoke` | revoke a report | `governance` |
| `job.read` | read jobs, steps, events | `read` |
| `job.create` | create a job | `clinical` |
| `job.retry` | re-dispatch a `FAILED` job on its pinned resolution (ch. 5, T13, `MOS-EXEC-012`) | `clinical` |
| `job.cancel` | **RESERVED, not implemented before 0.3** (spine section 4) | `clinical` |
| `job.delete` | delete a job and its execution artifacts | `phi` |
| `result.read` | read a `Result` and its `ResultBundle` | `clinical` |
| `result.read.research` | see `research_only` results in list endpoints (ch. 9 §9.4 E5, `MOS-SAFE-041`) | `clinical` |
| `result.export` | export result data | `phi` |
| `result.supersede` | mark a result superseded by a newer one | `clinical` |
| `result.submit` | submit a `ResultBundle` (held only by Job Tokens) | `clinical` |
| `result.review.read` | read review rows and their round history (ch. 9, `MOS-SAFE-067`) | `clinical` |
| `result.review.submit` | claim, release and submit a review round (ch. 9, `MOS-SAFE-067`, `MOS-SAFE-069`) | `clinical` |
| `result.review.assign` | set `assignee_user_id` on a review (ch. 9, `MOS-SAFE-067`) | `clinical` |
| `result.review.reopen` | open a new review round on a terminal review (ch. 9, `MOS-SAFE-066`) | `clinical` |
| `policy.read` | read the active PolicySet | `read` |
| `policy.author` | upload a new PolicySet | `governance` |
| `policy.activate` | activate a PolicySet for a tenant | `governance` |
| `policy.simulate` | run a decision in shadow mode | `governance` |
| `decision.read` | read `PolicyDecision` records | `governance` |
| `audit.read` | read `AuditEvent` records | `governance` |
| `audit.export` | export a signed audit range | `governance` |
| `audit.verify` | run chain verification | `governance` |
| `webhook.read` / `.create` / `.update` / `.delete` | webhook endpoints and their HMAC keys | `read` / `write` / `write` / `write` |
| `phi.reidentify` | reverse a de-identification UID mapping | `phi` |
| `phi.erase` | execute an `ErasureRequest` | `phi` |
| `phi.policy_update` | change de-identification profile or `external_llm_allowed` | `governance` |
| `llm.external_call` | invoke a provider outside the tenant boundary | `phi` |
| `break_glass.invoke` | open a time-boxed platform-admin window | `admin` |

Spellings that appear in other chapters' prose and are **not** permission identifiers — a build referencing any of them MUST fail under MOS-SEC-033: `job.requeue` (the permission is `job.retry`, ch. 5 T13); `deployment.write`, `deployment.delete`, `model.write`, `dataset.write`, `validation.read`, `validation.approve`, `service.write`, `service.publish`, `webhook.write` and `result.review` (coarsenings or inventions; use the rows above); `service.approve` (use `service_version.approve`); and `result.publish` (ch. 17, `MOS-TRAIN-009`; `result.publish` is a PEP id, section 8.4.1 — the permission is `result.submit`). The three-segment rows of the table above — `result.review.read`, `result.review.submit`, `result.review.assign`, `result.review.reopen`, `result.read.research`, `artifact.status.set` and `deployment.gate.override` — **are** permissions; MOS-SEC-031 admits them and rejects only identifiers of four segments or more.

- **MOS-SEC-034** — Every resource that has a `create` permission MUST have a disposition for removal: either a `delete` permission or an explicit statement here that the resource is never deleted. There are exactly four never-deleted resources: `AuditEvent`, `PolicyDecision`, `Result` and `Deployment`.
- **MOS-SEC-035** — A `Result` MUST NOT be deletable. A superseding result MUST set `superseded_by` on the prior result. `job.delete` MUST refuse when the job has a result in a deployment whose `clinical_use_mode` is `clinical`.
- **MOS-SEC-036** — `result.submit` MUST NOT be assignable to a `Role`. It exists only inside a `JobToken.scope`.
- **MOS-SEC-037** — `phi.reidentify`, `phi.erase` and `break_glass.invoke` MUST require a `reason` string of at least 20 characters, recorded verbatim in the `AuditEvent`.
- **MOS-SEC-037a** — There is no `deployment.delete` permission and no deletion path for a `Deployment`. A `Deployment` row is permanent provenance: `Job.deployment_id`, `Result.deployment_id` and DICOM `(0018,1000) DeviceSerialNumber` (ch. 9, `MOS-SAFE-044`) reference it after it has stopped serving. Taking a version out of service is `deployment.suspend` (reversible) or `deployment.retire` (terminal, `DRAINING → RETIRED`), which are Chapter 6's transitions (`MOS-REG-072`, `MOS-REG-073`); stopping a version platform-wide is `SUSPENDED` or `RECALLED` on the artifact (ch. 2, `MOS-SVC-115`), not a deployment edit.

#### 8.3.3 Roles

- **MOS-SEC-038** — A `Role` MUST be `(role_id, tenant_id, key, display_name, permissions text[], is_seeded bool, created_by, created_at)`. Roles are tenant data. Clinical job titles MUST NOT be hardcoded anywhere in the platform. A storage layer MAY normalise `permissions text[]` into a join table (ch. 12), but the role itself MUST remain tenant-scoped and MUST NOT be a global closed enum.
- **MOS-SEC-039** — Role definitions MUST be flat. Role inheritance, role hierarchies and nested groups MUST NOT be implemented; a site that wants composition composes at assignment time by granting several roles.
- **MOS-SEC-040** — A principal MAY hold several roles. Its permission set is the union of its roles' permission sets. Denial is never expressed in a role; it is expressed in policy (section 8.4).
- **MOS-SEC-041** — Seeded roles MUST be created at tenant creation, MUST be editable by the tenant, and MUST be marked `is_seeded = true` so that an upgrade can report drift without overwriting local edits. A platform upgrade MUST NOT modify a seeded role a tenant has edited.

#### 8.3.4 Seeded default roles

| Role key | Intent | Permissions |
|---|---|---|
| `tenant_admin` | Owns the tenant | all `tenant.read/update`, all `user.*`, `role.*`, `api_key.*`, `service_account.*`, `webhook.*`, `policy.read`, `audit.read` |
| `clinical_operator` | Runs analyses on real studies | `patient.read`, `study.read`, `study.read_pixels`, `job.create`, `job.read`, `job.retry`, `result.read`, `result.review.read`, `deployment.read`, `capability.read`, `service.read`, `service_version.read` |
| `clinical_reviewer` | Signs off results | everything in `clinical_operator` plus `result.review.submit`, `result.review.reopen`, `result.supersede` |
| `integration_engineer` | Wires services in, no pixels | `service.*`, `service_version.read/publish/deprecate`, `model.*`, `model_version.read/publish`, `capability.read`, `deployment.read/create/update/promote/suspend/retire`, `job.read`, `webhook.*` |
| `evidence_scientist` | Builds the evidence plane | `dataset.*`, `dataset_version.*`, `dataset_split.read/freeze`, `annotation_set.*`, `evaluation.read/run`, `validation_report.read/create/export`, `model_version.read`, `study.read`, `study.read_pixels`, `capability.read` |
| `evidence_approver` | The named human approver | `validation_report.read/approve/revoke`, `capability.update`, `evaluation.read` |
| `training_pipeline` | The model-development pipeline's `ServiceAccount` (ch. 17, `MOS-TRAIN-009`) | `dataset.read/create`, `dataset_version.read/create`, `dataset_split.read/freeze`, `annotation_set.read/create/update`, `evaluation.read/run`, `artifact.publish`, `study.read`, `study.read_pixels` |
| `auditor` | Reads history, touches nothing | `audit.read`, `audit.export`, `audit.verify`, `decision.read`, `policy.read`, `job.read`, `deployment.read`, `service_version.read`, `validation_report.read` |
| `data_steward` | Owns PHI policy and erasure | `phi.policy_update`, `phi.erase`, `phi.reidentify`, `study.delete`, `job.delete`, `dataset_version.read`, `audit.read` |
| `readonly` | Observes | all `*.read` permissions of class `read` |

These ten keys are the seeded set. They are inserted per tenant at tenant creation and MUST NOT be expressed as a database CHECK constraint: a tenant may add, edit and remove roles (MOS-SEC-041).

- **MOS-SEC-042** — No seeded role MUST hold both `validation_report.create` and `validation_report.approve`. Separation of the producer and the approver of evidence is a control, not a convenience, and a tenant that merges them MUST do so explicitly by editing a role.
- **MOS-SEC-043** — `platform_admin` MUST exist only in the system tenant, MUST hold no permission of class `phi`, and MUST obtain PHI reach only through break-glass (section 8.5.3).
- **MOS-SEC-158** — The model-development pipeline (ch. 17) MUST execute as a `ServiceAccount` holding the seeded `training_pipeline` role and no other role. Chapter 17's `MOS-TRAIN-009` names that permission set in its own prose; the registered spellings are this chapter's, and the mapping is: `dataset.create` → `dataset.create`; seal a dataset version → `dataset_version.create`; freeze an annotation set → `annotation_set.update`; freeze a split → `dataset_split.freeze`; submit an evaluation → `evaluation.run`; `artifact.publish` → `artifact.publish`. The pipeline's exclusions — `artifact.approve`, every `deployment.*` permission including `deployment.gate.override`, `result.submit` (which MOS-SEC-036 already makes unassignable to any `Role`) and `phi.reidentify` — MUST be enforced by their absence from the role, never by a check in pipeline code: under MOS-SEC-001 and MOS-SEC-040 a permission the principal's roles do not contain is already a denial at the PEP, and a code-level guard is a second copy of the boundary that drifts from the first. A CI test MUST compare the seeded role's `permissions` array against this list exactly.

#### 8.3.5 Effective permissions and delegation

- **MOS-SEC-044** — Permissions MUST be evaluated **per call**, against the principal's current role grants. A permission set MUST NOT be snapshotted into a job, a context object or a token payload as a grant. This deletes the old spec's `context.permissions[]` array.
- **MOS-SEC-045** — When a principal acts through a delegated context — a Job Token, an API key, an MCP session (ch. 11, MOS-AGENT) — the effective permission set MUST be the **intersection** of: (a) the current permissions of the delegating principal, (b) the credential's `scope`, (c) the permissions declared by the executing `ServiceVersion` or `Tool` manifest. It MUST NOT be a union, and a component MUST NOT be able to widen its own set.
- **MOS-SEC-046** — A `ServiceVersion` manifest MUST declare `required_permissions:`. A deployment whose declared permissions are not a subset of the deploying principal's permissions MUST be refused at `deployment.create`.

### 8.4 The Policy Engine

RBAC answers "may this role ever do this?" Policy answers "may this principal do this, to this object, in this context, right now?" The previous version drew the second as a box with three labels and gave it no table, no language, no build step and no failure mode. It has all four here.

#### 8.4.1 Placement: the PEPs and the PDP

- **MOS-SEC-047** — There MUST be exactly eight Policy Enforcement Points in 0.2. Each is a named constant recorded on every decision.

| PEP id | Component | Evaluated | Guards |
|---|---|---|---|
| `api.request` | Control Plane HTTP middleware | after authentication, before the handler | every API action |
| `gateway.query` | DICOM Gateway | per QIDO-RS request | `study.read` |
| `gateway.retrieve` | DICOM Gateway | per WADO-RS request | `study.read_pixels` |
| `gateway.store` | DICOM Gateway | per STOW-RS request | `study.write` |
| `dispatch.job` | Job Dispatcher | after triage, before enqueue | `job.create` against the resolved deployment |
| `runner.invoke` | Service Runner | before invoking a `ServiceVersion` | deployment eligibility, `clinical_use_mode` |
| `result.publish` | Result Writer | before DICOM writing and STOW | `study.write`, research marking |
| `llm.request` | LLM adapter (0.4) | before any provider call | `llm.external_call` |

- **MOS-SEC-048** — Every PEP MUST use the same PDP client library. A component that reads or writes tenant data without passing through a PEP is a defect; CI MUST enumerate HTTP handlers and Gateway routes and fail on any that does not call the middleware.
- **MOS-SEC-049** — The PDP MUST be in-process (a library) in 0.1–0.3, evaluated against a PolicySet loaded at startup and refreshed on activation events. A remote PDP MAY be added in 0.4 behind the same port; it MUST NOT change decision semantics.
- **MOS-SEC-050** — The repository chokepoint for PostgreSQL (section 8.5.2) is **not** a PEP. Row-level security is a containment control that operates whether or not a PEP was called; the two MUST NOT be collapsed into one mechanism.

#### 8.4.2 Policy language

- **MOS-SEC-051** — The 0.2 policy language MUST be **Cedar ≥ 4.2** (`cedar-policy`, Apache-2.0), evaluated in the Go control plane via `cedar-go`. MedicalOS MUST NOT define a policy language of its own (ch. 15, `MOS-REL-035`): there is no bespoke matcher syntax, no `priority` ordering and no editable rule row anywhere in the platform.

Justification, stated because the choice constrains everything downstream:

| Candidate | Why it was or was not chosen |
|---|---|
| **Cedar** (chosen) | Purpose-built for authorization, not general computation. Evaluation is guaranteed to terminate — a clinical PDP on the critical path of every pixel read must not have a policy that can loop. `forbid` unconditionally overrides `permit`, which is exactly the semantics MOS-SEC-001 needs and which does not have to be reconstructed by convention. It has a typed schema, so a policy referencing `resource.deployment_environment` on a resource type that has no such attribute — a `Study`, say — is a build error, not a runtime `undefined` that silently fails open. Its policies are analyzable by SMT, so "does this new PolicySet grant anything the old one did not?" is a question a tool can answer before activation, which is what makes MOS-SEC-064's shadow requirement finite. |
| **Rego / OPA** | Mature, ubiquitous, excellent decision logging and bundle distribution. Rejected as the default because Rego is a general-purpose language whose totality and conflict behaviour are properties of how it is written rather than of the language, and because `deny` beating `allow` is a convention each policy author re-implements. It remains the reference second driver. |
| **CEL** | An expression language, not a policy system: no policy-set semantics, no conflict resolution, no schema-checked entity model. It would force the combination rules into Go, which is where they get subtly wrong. |
| **Hand-written Go predicates** | What an implementer will build if this section is vague. Rejected: unversionable, unsignable, untestable against a stored decision, and not editable by a hospital's governance function. |

- **MOS-SEC-052** — The engine MUST sit behind a `PolicyEngine` port: `Decide(ctx, Request) (Decision, error)` where `Request` is engine-independent. A conformance suite of at least 60 fixed `(entities, request, expected effect, expected determining policies)` cases MUST live in `medos/contracts/policy/conformance/` and MUST pass for every driver.
- **MOS-SEC-053** — The Cedar schema MUST be generated from the same `medos/contracts/permissions.yaml` that generates the RBAC constants, so an action can never exist in one and not the other.

```cedar
// medos/contracts/policy/schema.cedarschema  (generated — do not edit)
namespace MedicalOS {
  entity Tenant = {
    "external_llm_allowed": Bool,
    "residency": String,
  };
  entity Role in [Tenant] = { "key": String };
  entity User in [Role] = { "tenant": Tenant, "status": String };
  entity ServiceAccount in [Role] = { "tenant": Tenant, "status": String };
  entity ServiceVersion = { "tenant": Tenant, "publisher_verified": Bool };
  entity Deployment = {
    "tenant": Tenant,
    // The three Chapter 6 facts, kept apart exactly as ch. 6 §6.8 keeps them
    // (MOS-REG-072, MOS-REG-073). They MUST NOT be collapsed into one attribute.
    "environment": String,              // dev | staging | production
    "state": String,                    // PENDING | VERIFYING | SERVING | SUSPENDED | DRAINING | RETIRED
    "role": String,                     // ACTIVE | CANARY | SHADOW | STANDBY
    "clinical_use_mode": String,
    "approved_age_groups": Set<String>,
    "approved_modalities": Set<String>,
  };
  entity Study = { "tenant": Tenant };
  entity Result = {
    "tenant": Tenant,
    // Snapshotted from the Deployment at execution (ch. 9, MOS-SAFE-046), never joined
    // live: a result must stay attributable to the conditions that produced it.
    "deployment_environment": String,   // dev | staging | production
    "deployment_state": String,         // PENDING | VERIFYING | SERVING | SUSPENDED | DRAINING | RETIRED
    "deployment_role": String,          // ACTIVE | CANARY | SHADOW | STANDBY
    "clinical_use_mode": String,
    "review_status": String,
  };
  entity Job = { "tenant": Tenant, "deployment": Deployment };

  action "result.read" appliesTo {
    principal: [User, ServiceAccount],
    resource: [Result],
    context: {
      "rbac_granted": Bool,
      "surface": String,
      "break_glass_valid": Bool,
      "tenant_id": String
    }
  };
  action "job.create" appliesTo {
    principal: [User, ServiceAccount],
    resource: [Deployment],
    context: {
      "rbac_granted": Bool,
      "surface": String,
      "break_glass_valid": Bool,
      "tenant_id": String,
      "patient_age_group": String,
      "modality": String
    }
  };
  action "llm.external_call" appliesTo {
    principal: [User, ServiceAccount],
    resource: [Tenant],
    context: { "rbac_granted": Bool, "surface": String, "break_glass_valid": Bool, "tenant_id": String }
  };
}
```

- **MOS-SEC-054** — RBAC MUST be evaluated first, in Go, against the principal's role grants, and its outcome MUST enter the Cedar context as `rbac_granted`. Roles MUST NOT be compiled into Cedar policies; tenant-definable roles would otherwise mean regenerating a PolicySet on every role edit.

```cedar
// P-000  RBAC is the only source of a grant.
permit (principal, action, resource)
when { context.rbac_granted == true };

// P-001  Tenant boundary. forbid overrides every permit, including P-040.
forbid (principal, action, resource)
when {
  resource has tenant && principal has tenant &&
  resource.tenant != principal.tenant
};

// P-002  A request with no tenant context is denied before anything else is considered.
forbid (principal, action, resource)
unless { context.tenant_id != "" };
```

#### 8.4.3 Evaluation contract

- **MOS-SEC-055** — The evaluation order MUST be: (1) authenticate; (2) resolve tenant and bind it to the request; (3) RBAC check → `rbac_granted`; (4) load entity attributes for principal and resource; (5) Cedar evaluation; (6) apply obligations. A step MUST NOT be skipped on a cache hit except step 5, and only under MOS-SEC-060.
- **MOS-SEC-056** — The effect MUST be `forbid` when any `forbid` policy is satisfied, `permit` when at least one `permit` is satisfied and no `forbid` is, and `forbid` otherwise. There is no third outcome, and there is no priority ordering between policies.
- **MOS-SEC-057** — Policy evaluation MUST be **fail-closed**. A PDP error, a schema mismatch, a missing entity attribute, or an evaluation timeout (default 50 ms) MUST produce `forbid` with `reason_code = "policy_unavailable"`, surfaced by the API as RFC 9457 problem+json with `class: "policy_unavailable"` (ch. 10, MOS-API).
- **MOS-SEC-058** — The circuit breaker, retry and degradation patterns applied to other dependencies MUST NOT be applied to the PDP path. There is no "open the breaker and allow".
- **MOS-SEC-059** — A replica that cannot load or validate the active PolicySet MUST fail its readiness probe and MUST NOT serve traffic. It MUST NOT fall back to a previously cached set from a different digest.
- **MOS-SEC-060** — Decision caching is permitted only as follows: `forbid` results MAY be cached ≤ 60 s; `permit` results for actions of class `read` or `write` MAY be cached ≤ 5 s; `permit` results for classes `phi`, `clinical`, `admin`, `governance` MUST NOT be cached. The cache key MUST include `policy_set_digest`, principal id, action, resource id and `context_digest`.
- **MOS-SEC-061** — Policy evaluation MUST NOT perform I/O. All entity attributes MUST be loaded before step 5 by the PEP. A policy that needs an attribute the PEP did not load MUST cause `policy_unavailable`, never a default value.

#### 8.4.4 PolicySet versioning and activation

- **MOS-SEC-062** — A `PolicySet` MUST be an `Artifact` of kind `policy_set` (ch. 6, MOS-REG): an immutable bundle of `schema.cedarschema`, `policies/*.cedar`, and `tests/*.json`, addressed by the SHA-256 of its canonical tar, and signed (section 8.8). Policy rule text exists only inside that bundle; it MUST NOT be stored as editable database rows.
- **MOS-SEC-063** — Activation MUST be a row: `policy_activations(tenant_id, policy_set_digest, mode, effective_from, activated_by, previous_digest, reason)`, with `mode ∈ {shadow, enforce}`. Activation MUST be append-only; rollback is a new activation naming the older digest. This is the only per-tenant policy state.
- **MOS-SEC-064** — A PolicySet MUST NOT be activated in `enforce` mode for a tenant with any deployment in `clinical_use_mode: clinical` unless it has run in `shadow` for that tenant for ≥ 24 h **or** SMT analysis shows it grants no request the currently enforced set denies. The analysis result MUST be attached to the activation.
- **MOS-SEC-065** — In `shadow` mode the PDP MUST evaluate, MUST record a `PolicyDecision` with `mode: "shadow"`, and MUST NOT influence the response. The enforced decision MUST also be recorded, so the pair is comparable.
- **MOS-SEC-066** — `policies/` MUST contain a baseline set present in every tenant's PolicySet and not removable by tenant edit: P-000, P-001, P-002, P-014, P-015, P-030. CI MUST assert their presence and exact text.

```cedar
// P-014  A result reaches a clinical surface only from a deployment that is production,
//        actually serving, and taking real traffic. Chapter 6 §6.8 owns all three values:
//        environment ∈ {dev,staging,production}; state ∈ {PENDING,VERIFYING,SERVING,
//        SUSPENDED,DRAINING,RETIRED}; role ∈ {ACTIVE,CANARY,SHADOW,STANDBY}.
//        Closes the case where a staging, suspended or shadow model's output appears in a
//        radiologist's viewer.
forbid (
  principal,
  action == MedicalOS::Action::"result.read",
  resource
)
when {
  context.surface == "clinical_viewer" &&
  (resource.deployment_environment != "production" ||
   resource.deployment_state != "SERVING" ||
   resource.deployment_role == "SHADOW" ||
   resource.deployment_role == "STANDBY")
};

// P-015  research_only output never enters the clinical read path (ch. 9, MOS-SAFE).
forbid (
  principal,
  action in [MedicalOS::Action::"result.read", MedicalOS::Action::"result.export"],
  resource
)
when {
  context.surface == "clinical_viewer" &&
  resource.clinical_use_mode == "research_only"
};

// P-021  A study outside the deployment's approved population is not dispatched.
forbid (
  principal,
  action == MedicalOS::Action::"job.create",
  resource
)
when {
  context.patient_age_group == "paediatric" &&
  !(resource.approved_age_groups.contains("paediatric"))
};

// P-030  No prompt leaves the tenant boundary unless the tenant switched it on.
forbid (
  principal,
  action == MedicalOS::Action::"llm.external_call",
  resource
)
unless { resource.external_llm_allowed == true };

// P-040  Break-glass grants platform admins a time-boxed window — and P-001 still applies
//        to everything except the explicitly targeted tenant, which the PEP encodes in
//        principal.tenant for the duration of the window.
permit (principal, action, resource)
when {
  context.break_glass_valid == true &&
  principal in MedicalOS::Role::"system::platform_admin"
};
```

#### 8.4.5 `PolicyDecision`

- **MOS-SEC-067** — Every evaluation MUST produce a `PolicyDecision` value. It MUST be persisted when: the effect is `forbid`; or the action's class is `phi`, `clinical`, `admin` or `governance`; or `mode = "shadow"`; or the tenant's `decision_sampling_rate` selects it. `decision_sampling_rate` defaults to `1.0` in 0.1–0.2.
- **MOS-SEC-068** — A `PolicyDecision` MUST carry `job_id` and `trace_id` whenever the request is executing within a job, so that a decision joins to the execution that caused it.
- **MOS-SEC-069** — `PolicyDecision.context` MUST store only keys declared `loggable: true` in the schema. Every key contributes to `context_digest`; non-loggable keys MUST NOT be stored in plaintext. `patient_age_group` is loggable; a `patient_id` would not be and MUST NOT be a context key at all (section 8.6.2).
- **MOS-SEC-070** — `PolicyDecision` MUST be append-only under the same controls as `AuditEvent` (section 8.9.3) and MUST be retained for ≥ 2 years.

```json
{
  "decision_id": "pd_01JB8QF7N2Z3H4V5K9RM0XTC6W",
  "tenant_id": "t_hosp_nord",
  "occurred_at": "2026-09-13T09:14:02.418471Z",
  "mode": "enforce",
  "engine": "cedar",
  "engine_version": "4.2.1",
  "policy_set_digest": "sha256:9f2c8d41a7b3e05f6c19d28a4b7e0c35d9a1f8b26e4c70d3a5b1e9f04c826d7a",
  "policy_set_version": "2026.09.02-3",
  "pep": "api.request",
  "principal": "MedicalOS::User::\"u_7d2f4a\"",
  "on_behalf_of": null,
  "action": "MedicalOS::Action::\"result.read\"",
  "resource": "MedicalOS::Result::\"res_01JB8PK3T8\"",
  "rbac_granted": true,
  "effect": "forbid",
  "reason_code": "deployment_not_production_serving",
  "determining_policies": ["P-014"],
  "errors": [],
  "context": {"surface": "clinical_viewer", "tenant_id": "t_hosp_nord", "break_glass_valid": false},
  "context_digest": "sha256:41ab7c9e05d2f6183b4a7c0e9d582f36a1b4c7e0d93a5f81",
  "job_id": "job_01JB8NQ5V9TE7KH2M4RA0PXC8W",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "latency_us": 412
}
```

#### 8.4.6 A worked decision, end to end

A clinical operator opens a study in OHIF. The viewer asks the platform for the results attached to that study. One of them was produced by a deployment that is serving and taking active traffic, but in `staging`. This is the exact hole the review named: nothing in the previous version prevented a staging model's output from appearing in a radiologist's viewer.

**Step 1 — the request.** OHIF calls `GET /api/v1/results/res_01JB8PK3T8` with header `X-MedicalOS-Surface: clinical_viewer` and a session credential for `u_7d2f4a`. The `api.request` PEP authenticates, resolves `tenant_id = t_hosp_nord`, and binds it to the request context.

**Step 2 — RBAC.** `u_7d2f4a` holds `clinical_operator`, which holds `result.read`. `rbac_granted = true`. Had it been false, evaluation would stop here with `forbid`, `reason_code = "rbac_denied"`.

**Step 3 — entities loaded by the PEP** (no I/O happens inside the engine):

```json
[
  { "uid": {"type": "MedicalOS::Tenant", "id": "t_hosp_nord"},
    "attrs": {"external_llm_allowed": false, "residency": "eu"},
    "parents": [] },
  { "uid": {"type": "MedicalOS::Role", "id": "t_hosp_nord::clinical_operator"},
    "attrs": {"key": "clinical_operator"},
    "parents": [{"type": "MedicalOS::Tenant", "id": "t_hosp_nord"}] },
  { "uid": {"type": "MedicalOS::User", "id": "u_7d2f4a"},
    "attrs": {"tenant": {"__entity": {"type": "MedicalOS::Tenant", "id": "t_hosp_nord"}},
              "status": "active"},
    "parents": [{"type": "MedicalOS::Role", "id": "t_hosp_nord::clinical_operator"}] },
  { "uid": {"type": "MedicalOS::Result", "id": "res_01JB8PK3T8"},
    "attrs": {"tenant": {"__entity": {"type": "MedicalOS::Tenant", "id": "t_hosp_nord"}},
              "deployment_environment": "staging",
              "deployment_state": "SERVING",
              "deployment_role": "ACTIVE",
              "clinical_use_mode": "clinical",
              "review_status": "UNREVIEWED"},
    "parents": [] }
]
```

**Step 4 — the request Cedar evaluates:**

```json
{
  "principal": {"type": "MedicalOS::User", "id": "u_7d2f4a"},
  "action":    {"type": "MedicalOS::Action", "id": "result.read"},
  "resource":  {"type": "MedicalOS::Result", "id": "res_01JB8PK3T8"},
  "context":   {"rbac_granted": true, "surface": "clinical_viewer",
                "break_glass_valid": false, "tenant_id": "t_hosp_nord"}
}
```

**Step 5 — evaluation.** P-000 permits (`rbac_granted`). P-001 does not fire (tenants match). P-002 does not fire (`tenant_id` non-empty). **P-014 fires**: `surface == "clinical_viewer"` and `deployment_environment != "production"` (the deployment is `SERVING` and `ACTIVE`, but in `staging`). `forbid` overrides `permit`. Effect: `forbid`, determining policy `P-014`.

**Step 6 — the PolicyDecision** is the record shown in section 8.4.5 verbatim.

**Step 7 — the AuditEvent** (section 8.9), joined to the decision by `policy_decision_id`:

```json
{
  "audit_id": "ae_01JB8QF7N3A1B2C4D6E8F0G2H4",
  "tenant_id": "t_hosp_nord",
  "seq": 448211,
  "occurred_at": "2026-09-13T09:14:02.419Z",
  "actor": {"kind": "user", "id": "u_7d2f4a", "auth": "session", "key_id": "oidc:eu-idp"},
  "on_behalf_of": null,
  "action": "result.read",
  "resource": {"kind": "result", "id": "res_01JB8PK3T8", "study_ref": "K7QW4M2X9A0BD1PC"},
  "outcome": "deny",
  "policy_decision_id": "pd_01JB8QF7N2Z3H4V5K9RM0XTC6W",
  "job_id": "job_01JB8NQ5V9TE7KH2M4RA0PXC8W",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "request_id": "req_01JB8QF7N2",
  "pep": "api.request",
  "source_ip": "10.42.9.113",
  "user_agent": "OHIF/3.9.0",
  "detail": {"reason_code": "deployment_not_production_serving"},
  "prev_hash": "sha256:6b1f0a7d3c94e2850b6f1a3d7c0e94b28f5a6d1c3e07b94a2f8d05c6e31b7a49",
  "hash": "sha256:0d9a41c76b2e850f3a7d1c94b0e26f38a5d71c04e9b3a86f2d05c71e4b93a6d8"
}
```

**Step 8 — the response.** The API returns `403` with problem+json, `class: "forbidden"`, `detail: "This result was produced by a deployment that is not production-serving and cannot be shown on a clinical surface."` It MUST NOT return `404`, because the result exists and the operator is entitled to know it was withheld.

- **MOS-SEC-071** — Given a stored `PolicyDecision`, replaying it against the PolicySet named by `policy_set_digest` with the same entities and context MUST produce the identical `effect` and `determining_policies`. Because a `PolicySet` is immutable and is never edited in place (MOS-SEC-062, MOS-SEC-063), the digest always resolves to the exact policy text that produced the decision. A test MUST assert this for every persisted decision in a CI fixture set.

### 8.5 Tenant isolation, layer by layer

#### 8.5.1 The isolation matrix

| Layer | Mechanism | Failure if absent | Requirement |
|---|---|---|---|
| API | `tenant_id` bound at authentication; PEP on every route | Cross-tenant read via a handler that forgot a filter | MOS-SEC-047 |
| PostgreSQL | RLS, `FORCE`, transaction-local session variable, single chokepoint | The same, one layer deeper and undetectable | MOS-SEC-072 |
| Object store | Per-tenant key prefix + per-tenant STS session policy | Artifact and result leakage through a mis-built key | MOS-SEC-083, MOS-SEC-085 |
| PACS / imaging | DICOM Gateway holds the only credential; token study scope | The failure the review named: tenancy true on metadata, false on pixels | MOS-SEC-088 |
| Inference | Model namespacing, no tenant-reachable Triton, sealed mode for third parties | Cross-tenant model invocation; code execution on shared GPU | MOS-SEC-092, MOS-SEC-095 |
| Queue | Per-service work inbox with per-principal ACLs; tenant in the partition key | One service's consumer reading another's work | MOS-SEC-096, MOS-SEC-097 |
| Cache | Tenant-prefixed keys; no shared unprefixed namespace | Result bleed on a key collision | MOS-SEC-100 |
| Telemetry | `tenant_id` label; per-tenant log index in multi-tenant hosting | Operational data disclosure between tenants | MOS-SEC-101 |
| LLM | `external_llm_allowed`, per-tenant provider credentials | Prompts crossing an organisational boundary | MOS-SEC-121, MOS-SEC-123 |

#### 8.5.2 PostgreSQL row-level security

- **MOS-SEC-072** — Every table holding tenant-owned data MUST have a `tenant_id uuid NOT NULL` column, MUST have `ROW LEVEL SECURITY` enabled **and** forced, and MUST have a policy binding it to the session variable `medicalos.tenant_id`.

```sql
-- roles (migration 0001)
CREATE ROLE medicalos_owner    NOLOGIN;
CREATE ROLE medicalos_migrator LOGIN NOBYPASSRLS;
CREATE ROLE medicalos_app      LOGIN NOBYPASSRLS;

ALTER TABLE jobs OWNER TO medicalos_owner;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE  ROW LEVEL SECURITY;

-- current_setting(name, false) raises 42704 when the variable is unset:
-- an unbound connection cannot read a single row.
CREATE POLICY jobs_tenant_isolation ON jobs
  FOR ALL
  TO medicalos_app
  USING      (tenant_id = current_setting('medicalos.tenant_id', false)::uuid)
  WITH CHECK (tenant_id = current_setting('medicalos.tenant_id', false)::uuid);

-- the break-glass override is a second, permissive policy gated on a variable
-- only the break-glass code path may set (section 8.5.3). FOR SELECT, never
-- FOR ALL: a policy that permits writes and omits WITH CHECK reuses its USING
-- expression as WITH CHECK, so FOR ALL here would turn an open window into a
-- cross-tenant INSERT and UPDATE grant (MOS-SEC-157).
CREATE POLICY jobs_break_glass ON jobs
  FOR SELECT
  TO medicalos_app
  USING (current_setting('medicalos.break_glass', true) = 'on');

GRANT SELECT, INSERT, UPDATE, DELETE ON jobs TO medicalos_app;
```

- **MOS-SEC-073** — The application MUST connect as `medicalos_app`, which MUST NOT own any table and MUST have `rolbypassrls = false`. Migrations MUST run as `medicalos_migrator`. The superuser MUST NOT be used by any running process.
- **MOS-SEC-074** — The tenant variable MUST be set with `set_config(..., is_local => true)`, i.e. transaction-scoped. `SET SESSION` MUST NOT be used, because PgBouncer in `transaction` pool mode hands the connection to another tenant's request at commit.
- **MOS-SEC-075** — All tenant-scoped database access MUST pass through exactly one repository function. A `pgx.Pool` or `sql.DB` handle MUST NOT be reachable from any package other than the repository package; CI MUST assert this by import analysis.

```go
// internal/storage/chokepoint.go — the only place a tenant-scoped transaction begins.
func (r *Repo) InTenantTx(ctx context.Context, fn func(ctx context.Context, tx pgx.Tx) error) error {
	tc, ok := tenantctx.From(ctx)
	if !ok || tc.TenantID == uuid.Nil {
		return ErrNoTenantContext // fail closed: never fall back to "all tenants"
	}
	tx, err := r.pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return fmt.Errorf("begin: %w", err)
	}
	defer tx.Rollback(context.WithoutCancel(ctx))

	// is_local = true => scoped to this transaction, safe under transaction pooling.
	if _, err := tx.Exec(ctx,
		`SELECT set_config('medicalos.tenant_id', $1, true)`, tc.TenantID.String()); err != nil {
		return fmt.Errorf("bind tenant: %w", err)
	}
	if tc.BreakGlass != nil {
		if _, err := tx.Exec(ctx,
			`SELECT set_config('medicalos.break_glass', 'on', true)`); err != nil {
			return fmt.Errorf("bind break-glass: %w", err)
		}
	}
	if err := fn(ctx, tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
```

- **MOS-SEC-076** — A query MUST NOT add `WHERE tenant_id = $1` as its isolation mechanism. RLS is the mechanism; an explicit predicate is permitted only as an index hint and MUST NOT be the only barrier.
- **MOS-SEC-077** — A migration that creates a table with a `tenant_id` column and does not enable and force RLS MUST fail CI. The check is one query:

```sql
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0
WHERE n.nspname = 'public' AND c.relkind = 'r'
  AND (c.relrowsecurity = false OR c.relforcerowsecurity = false);
-- MUST return zero rows.
```

- **MOS-SEC-078** — Background workers, the reconciler and the queue relay MUST acquire a tenant context per unit of work. A process-wide "admin" connection that reads across tenants MUST NOT exist outside the break-glass path.

#### 8.5.3 Break-glass and platform administration

- **MOS-SEC-079** — Cross-tenant access by a platform administrator MUST be possible only through a `break_glass` grant: a row `break_glass_grants(id, tenant_id, granted_to, reason, requested_at, approved_by, expires_at)` with `expires_at - requested_at ≤ 60 minutes`.
- **MOS-SEC-080** — A break-glass grant SHOULD require a second approver holding `break_glass.invoke`; in single-operator deployments it MUST at minimum emit a webhook and an `AuditEvent` of action `break_glass.invoke` before the first data access.
- **MOS-SEC-081** — Every `AuditEvent` and `PolicyDecision` produced inside a break-glass window MUST carry `break_glass_id`. A tenant with `audit.read` MUST be able to list every break-glass window opened against their data.
- **MOS-SEC-082** — Break-glass MUST NOT grant `phi.reidentify` unless that permission is named explicitly in the grant's `reason` and scope.
- **MOS-SEC-157** — A break-glass row-level-security policy MUST be declared `FOR SELECT`. Break-glass opens a time-boxed cross-tenant **read** window and MUST NOT confer write reach. PostgreSQL reuses a policy's `USING` expression as its `WITH CHECK` expression when a policy that permits writes omits one, so a break-glass policy written `FOR ALL` makes an open window a permissive INSERT and UPDATE grant over every tenant's rows — and the failure is silent, because the policy text mentions no write. Every break-glass policy in every migration, here and mirrored in chapter 12, MUST therefore name `FOR SELECT`; `FOR ALL`, `FOR INSERT`, `FOR UPDATE` and `FOR DELETE` MUST NOT be used. CI MUST assert that `SELECT count(*) FROM pg_policies WHERE policyname ~ '_break_glass$' AND cmd <> 'SELECT'` returns `0`. A platform administrator who must change a tenant's data has no break-glass path: the change is a tenant-authorised action performed under a tenant principal and audited as one.

#### 8.5.4 Object store

- **MOS-SEC-083** — Object keys MUST follow one layout, with tenancy as the first path segment after the environment:

```
s3://medicalos-prod/t/<tenant_id>/artifacts/service/<service_id>/<version>/manifest.json
s3://medicalos-prod/t/<tenant_id>/artifacts/model/<model_id>/<version>/weights.safetensors
s3://medicalos-prod/t/<tenant_id>/artifacts/model/<model_id>/<version>/preprocessing.yaml
s3://medicalos-prod/t/<tenant_id>/jobs/<job_id>/exec/<artifact_id>.nii.gz
s3://medicalos-prod/t/<tenant_id>/jobs/<job_id>/stdout/<attempt>.log.zst
s3://medicalos-prod/t/<tenant_id>/results/<job_id>/<result_kind>/<output_index>.dcm
s3://medicalos-prod/t/<tenant_id>/datasets/<dataset_id>/<dataset_version_digest>/manifest.json
```

- **MOS-SEC-084** — Object keys MUST NOT contain patient identifiers, DICOM UIDs, accession numbers, dates or free text. Keys appear in bucket listings, access logs and CDN caches; they are the surface people forget. `<output_index>` replaces the SOP Instance UID for exactly this reason.
- **MOS-SEC-085** — Workers MUST receive short-lived credentials (STS `AssumeRole` or MinIO `AssumeRole`, TTL ≤ 1 h) carrying a session policy scoped to `t/<tenant_id>/*`. Long-lived credentials with bucket-wide reach MUST NOT be mounted into any worker.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": ["arn:aws:s3:::medicalos-prod/t/8f14e45f-ceea-467a-9f2e-1c0b2d7a4e31/*"] },
    { "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": ["arn:aws:s3:::medicalos-prod"],
      "Condition": {"StringLike": {"s3:prefix": ["t/8f14e45f-ceea-467a-9f2e-1c0b2d7a4e31/*"]}} }
  ]
}
```

- **MOS-SEC-086** — Objects under `t/<tenant_id>/` MUST be encrypted at rest with a key derived from that tenant's KEK (section 8.7). Bucket-wide single-key encryption MUST NOT be used, because it makes tenant-scoped crypto-shredding impossible.
- **MOS-SEC-087** — A service container MUST NOT receive object-store credentials of any kind, in either execution mode. Inputs are handed to it by the runner; outputs are returned to the runner.

#### 8.5.5 Imaging plane

- **MOS-SEC-088** — OHIF, every worker, every service and every operator tool MUST address the DICOM Gateway, never the PACS. The PACS network address MUST NOT be resolvable or routable from `Z-EDGE`, `Z-SERVICE` or `Z-PLATFORM`.
- **MOS-SEC-089** — The Gateway MUST enforce, on every DICOMweb request: tenant binding, the `gateway.*` PEP, Job Token study/series scope where a Job Token is presented, and an `AuditEvent` of class `phi` recording the instance and series counts actually returned.
- **MOS-SEC-090** — A cross-tenant DICOMweb request MUST return `403` and MUST be audited with `outcome: "deny"`. It MUST NOT return an empty result set, which is indistinguishable from "the study does not exist" and hides probing.
- **MOS-SEC-091** — Whether a site runs one PACS instance per tenant or one partitioned instance is deliberately unsettled (Open Questions). This chapter's floor holds either way: the Gateway is the only credential holder, tenancy is filtered in the Gateway, and the choice MUST NOT be visible to any caller.
- **MOS-SEC-159** — Every component that reads imaging under the `dataset_export` consumer class (ch. 3, `MOS-DATA-021`) — each job of the model-development pipeline and the MONAI Label annotation server (ch. 17, `MOS-TRAIN-095`, `MOS-TRAIN-199`) — MUST address the DICOM Gateway and MUST be denied every network route to `Z-PACS` by NetworkPolicy, on the same terms as `Z-SERVICE` (MOS-SEC-004, MOS-SEC-088). Holding no PACS credential (MOS-SEC-005) is not sufficient on its own: a PACS that authorises by network position is reachable by anything the network lets reach it. Such a principal MUST NOT resolve to the `clinical_viewer`, `service` or `platform_writer` consumer class, and MUST NOT hold `phi.reidentify`.

#### 8.5.6 Inference plane: Triton namespacing and the shared GPU

- **MOS-SEC-092** — Triton model names MUST be composed by the `InferenceBackend` adapter, never supplied by a caller. The caller passes `(tenant_id, model_id, model_version)`; the adapter composes:

```
p__<model_id>__<version>              # platform-shared, operator-signed
t_<tenant_short>__<model_id>__<version>  # tenant-private
example: p__pulmo_lungseg__1_4_0
         t_8f14e45f__hosp_effusion__0_3_2
```

- **MOS-SEC-093** — The model repository MUST be mounted read-only into Triton, and Triton MUST run with `--model-control-mode=explicit`. Poll mode MUST NOT be used: a writable, polled repository turns any object-store write into a code-load.
- **MOS-SEC-094** — Triton's HTTP and gRPC endpoints MUST be reachable only from `Z-PLATFORM`. They MUST NOT be reachable from `Z-SERVICE` or `Z-EDGE`. Triton has no tenancy model and no authentication; its isolation is entirely network and naming, and this MUST be stated in operator documentation rather than assumed.
- **MOS-SEC-095** — Shared-GPU rules, because a GPU is a shared-memory device with no tenant concept:

| Concern | Rule |
|---|---|
| Untrusted code on the shared GPU | A `native`-mode model artifact executes inside the platform's Triton process. Native mode MUST therefore be restricted to artifacts signed by the platform operator (spine: first-party and reviewed open models). A third-party artifact MUST use `sealed` mode. A third-party Python-backend model on shared Triton is remote code execution across every tenant on that node. |
| CUDA MPS | MUST be disabled by default. MPS shares one CUDA context across clients, removing the process-level isolation that is the only isolation Triton has. |
| Partitioning | Where the hardware supports MIG, tenants with `clinical_use_mode: clinical` SHOULD be placed on distinct MIG instances. Where it does not, the deployment MUST record that isolation is process-level only. |
| Memory exhaustion | Each model MUST declare `resources.gpu_memory_mb`; instance groups MUST be sized so that the sum of declared ceilings fits the device. An admission check MUST refuse a load that would oversubscribe. |
| Starvation | Triton's rate limiter MUST be enabled with per-model priority derived from the deployment's `clinical_use_mode`: `clinical` outranks `research_only`. |
| Metrics disclosure | Triton's per-model inference counters reveal one tenant's volume to another if exposed. The metrics port MUST be scraped only by the platform collector, and per-model series MUST be relabelled to platform-owned dimensions before leaving it (section 8.6.4). |
| Names as a leak | Triton model names appear in its logs and metrics. Because they are composed from `tenant_short` and `model_id` only, they carry no P2+ data by construction; a name MUST NOT be derived from any study, patient or dataset identifier. |

#### 8.5.7 Queue and topics

- **MOS-SEC-096** — The per-service work inbox `medicalos.svc.<service_id>.work` (spine section 5) MUST be consumed by the platform-side service runner for that service, never by a service container. This is the reading that keeps the spine's "a service MUST NOT reach the broker directly" true; the inbox exists so that one service's backlog cannot block another's, not so that vendors get broker access.
- **MOS-SEC-097** — Under the Kafka driver, each runner deployment MUST have its own principal with `Read` on its own inbox and its own consumer group only, and `Write` on nothing. Lifecycle topics MUST be writable only by the control plane's principal.
- **MOS-SEC-098** — Because MOS-EXEC-003 fixes the partition key at `<tenant_id>:<study_instance_uid>`, every broker record key carries a class-P2 identifier. The broker is therefore inside the PHI boundary: it MUST have encryption at rest and in transit, MUST NOT be shared with non-MedicalOS workloads, MUST have a retention of ≤ 7 days on lifecycle topics, and its keys MUST NOT be copied into logs, metrics or traces.
- **MOS-SEC-099** — Under the PostgreSQL driver, the queue table is an ordinary tenant-owned table and inherits RLS. The claim query MUST run inside `InTenantTx` per tenant, or, for the cross-tenant dispatcher loop, under a dedicated `medicalos_dispatcher` role whose RLS policy permits only the columns `job_id, tenant_id, service_id, available_at, lease_until` and no payload.

#### 8.5.8 Caches and telemetry backends

- **MOS-SEC-100** — Every cache key MUST be prefixed `t:<tenant_id>:`. Separate logical databases or separate key namespaces without a prefix MUST NOT be treated as isolation.
- **MOS-SEC-101** — In hosted multi-tenant deployments, logs MUST be written to a per-tenant index or stream, and the query layer MUST filter by tenant at the backend, not in the UI. In on-prem single-tenant deployments this requirement is satisfied trivially and MUST still be documented as satisfied.

### 8.6 PHI

#### 8.6.1 Classification

- **MOS-SEC-102** — Every field the platform handles MUST be assigned exactly one class. The class, not the field name, determines where it may travel.

| Class | Contents | Examples |
|---|---|---|
| `P0` | Non-identifying platform data | `service_id`, `model_version`, durations, job outcome, error class |
| `P1` | Platform-internal pseudonyms | `job_id`, internal `study_id`, `study_ref`, `tenant_id` |
| `P2` | Indirect identifiers | `StudyInstanceUID`, `SeriesInstanceUID`, `SOPInstanceUID`, `StudyDate`, `DeviceSerialNumber`, `InstitutionName` |
| `P3` | Direct identifiers | `PatientName`, `PatientID`, `PatientBirthDate`, `AccessionNumber`, address, phone, national id |
| `P4` | Image data and voxel-resolution derivatives | pixel data, label maps, burned-in annotation, 3D-renderable head/face volumes |
| `SEC` | Secrets | API key secrets, tokens, private keys, PACS credentials, de-identification pepper |

```
study_ref = base32-crockford( HMAC-SHA256( pepper[tenant_id], "study|" || StudyInstanceUID ) )[0:16]
```

- **MOS-SEC-103** — `pepper[tenant_id]` MUST be 32 random bytes, generated at tenant creation, stored in the KMS, never exported, and never rotated during the tenant's life (rotation would break every historical join). Destroying it at tenant termination makes every `study_ref` ever emitted permanently unlinkable, which is the erasure story for telemetry.
- **MOS-SEC-104** — The de-identification UID mapping table (ch. 3, MOS-DATA) is class `SEC`. It MUST be envelope-encrypted with the tenant KEK, MUST NOT leave the tenant boundary, and every read MUST require `phi.reidentify` and produce an `AuditEvent`.

#### 8.6.2 Forbidden surfaces

- **MOS-SEC-105** — The following matrix is normative. "no" means MUST NOT appear, in any form, including inside a serialized error, an exception message, a stack trace or a URL.

| Surface | P0 | P1 | P2 | P3 | P4 | SEC |
|---|---|---|---|---|---|---|
| PostgreSQL (tenant-owned tables, RLS-forced) | yes | yes | yes | yes (envelope-encrypted) | no | no (hashed/encrypted only) |
| Object store under `t/<tenant>/` | yes | yes | yes | yes | yes | no |
| `AuditEvent` / `PolicyDecision` | yes | yes | yes (resource ids) | no | no | no |
| Structured logs | yes | yes | no | no | no | no |
| OpenTelemetry span attributes and span names | yes | yes | no | no | no | no |
| Metric labels | yes | `tenant_id` only | no | no | no | no |
| Event bus payloads | yes | yes | no | no | no | no |
| Event bus record keys | yes | yes | yes (fixed by MOS-EXEC-003; see MOS-SEC-098) | no | no | no |
| Webhook payloads | yes | yes | no | no | no | no |
| URLs, query strings, path segments | yes | yes | no | no | no | no |
| RFC 9457 problem+json bodies | yes | yes | no | no | no | no |
| Object-store keys and filenames | yes | yes | no | no | no | no |
| LLM prompts, completions and provider logs | yes | yes | no | no | no | no |
| Source repository and test fixtures | yes | yes | no | no | no | no |

- **MOS-SEC-106** — A P2 identifier needed for correlation MUST be replaced by `study_ref` (P1) on every surface where the matrix forbids P2. Resolution from `study_ref` back to the UID MUST require `study.read` and MUST be audited.

#### 8.6.3 Redaction mechanics

Redaction by denylist fails the first time someone adds a field. The platform uses a typed API that cannot express the unsafe case.

- **MOS-SEC-107** — Go services MUST log only through `internal/obs/log`, whose signature accepts a closed attribute type. `slog`, `log`, `fmt.Print*` and third-party loggers MUST NOT be imported outside that package; CI MUST assert it.

```go
package log

// Attr is unexported-constructible: only the functions below can make one,
// so there is no way to write log.Info(ctx, "x", log.Attr{"patient_name", name}).
type Attr struct{ k string; v any }

func TenantID(id uuid.UUID) Attr   { return Attr{"tenant_id", id.String()} }
func JobID(id string) Attr         { return Attr{"job_id", id} }
func StudyRef(r StudyRefValue) Attr{ return Attr{"study_ref", r.String()} }
func ServiceVersion(s string) Attr { return Attr{"service_version", s} }
func ErrorClass(c string) Attr     { return Attr{"error_class", c} }
func Count(name string, n int) Attr{ return Attr{"count." + name, n} }
func Duration(d time.Duration) Attr{ return Attr{"duration_ms", d.Milliseconds()} }

func Info(ctx context.Context, msg string, attrs ...Attr)
func Error(ctx context.Context, msg string, err error, attrs ...Attr) // err passes through Sanitize
```

- **MOS-SEC-108** — `Error` MUST sanitize the error chain: a `pgx` error carrying a failing statement's parameter values, or a DICOM library error quoting an attribute value, MUST be reduced to its class and code. Raw driver errors MUST NOT reach a log sink.
- **MOS-SEC-109** — Python components MUST use `medicalos_logging` with the same closed-constructor pattern. Log records MUST be JSON with a fixed key set.
- **MOS-SEC-110** — A second, independent redaction filter SHOULD run in the log shipper, matching DICOM UID shapes (`^\d(\.\d+){3,}$`), the `mos_` key prefix, and common identifier patterns, and replacing matches with `[redacted:<class>]`. It is defence in depth and MUST NOT be the primary control.
- **MOS-SEC-111** — CI MUST run a PHI scanner over the repository, over a fixture log corpus produced by the end-to-end tests, and over exported traces and metrics from those tests, seeded with known identifiers from the test studies. Any hit MUST fail the build.

#### 8.6.4 Metric labels

- **MOS-SEC-112** — Metric label **names** MUST come from a closed allowlist: `tenant_id`, `environment`, `service_id`, `service_version`, `capability`, `model_id`, `model_version`, `deployment_id`, `deployment_state`, `deployment_role`, `clinical_use_mode`, `modality`, `phase`, `job_outcome`, `rejection_reason`, `error_class`, `pep`, `policy_effect`, `queue`, `topic`, `result_kind`. `environment`, `deployment_state` and `deployment_role` carry the three Chapter 6 `Deployment` value spaces unchanged (`MOS-REG-072`, `MOS-REG-073`); they MUST NOT be conflated into a single label, and `deployment_state` MUST NOT be given an environment value.
- **MOS-SEC-113** — Metric label **values** MUST come from a declared enumeration per label. A runtime value not in the enumeration MUST be replaced with `other` and MUST increment `medicalos_metric_label_rejected_total{label=...}`. Free-form strings, user input, file paths and identifiers MUST NOT reach a label.
- **MOS-SEC-114** — Label cardinality MUST be bounded: ≤ 200 distinct values per label per tenant per day, asserted by a CI test that enumerates the metric registry.
- **MOS-SEC-115** — `patient`, `study`, `series`, `instance`, `accession`, `sop` and `uid` MUST NOT appear as substrings of any metric label name. A lint rule MUST enforce this against the registry.

#### 8.6.5 Traces

- **MOS-SEC-116** — Span attributes MUST come from the same allowlist as metric labels, plus `job_id`, `study_ref`, `trace_id`, `request_id`, `decision_id`, `attempt`.
- **MOS-SEC-117** — Span names MUST be route templates or fixed operation names. `GET /api/v1/studies/1.2.840.113619.2.55.3` MUST be recorded as `GET /api/v1/studies/{study_id}`.
- **MOS-SEC-118** — HTTP client and server instrumentation MUST be configured to drop `http.url`, `http.target` and `db.statement` attributes, all three of which capture P2+ data by default.

#### 8.6.6 Service container output

- **MOS-SEC-119** — A `sealed` service's stdout and stderr are vendor-controlled and MUST be treated as potentially PHI-bearing. They MUST be captured to `t/<tenant_id>/jobs/<job_id>/stdout/<attempt>.log.zst` with the job's retention, and MUST NOT be forwarded to the platform-wide log index.
- **MOS-SEC-120** — Reading a captured service log MUST require `job.read` plus `study.read`, and MUST be audited as class `phi`.

#### 8.6.7 `external_llm_allowed`

- **MOS-SEC-121** — `Tenant.external_llm_allowed` MUST be a boolean column, `NOT NULL DEFAULT false`. Changing it MUST require `phi.policy_update` and MUST produce an `AuditEvent` carrying old and new values and a reason. The column, the `llm.external_call` action and policy P-030 ship in 0.2.0; the enforcing adapter does not exist before 0.4.0 (ch. 11, `MOS-AGENT-002`).
- **MOS-SEC-122** — "External" means any inference endpoint outside the deployment's own trust zone, including a vendor API, a hosted model, and a model served from another tenant's namespace. A model running inside `Z-INFER` on the site's hardware is not external.
- **MOS-SEC-123** — (0.4.0) When `external_llm_allowed = false`, the `llm.request` PEP MUST deny every call to an external provider (policy P-030). It MUST NOT degrade to truncation, redaction or summarisation of the prompt; there is no "safe enough" prompt under a false switch.
- **MOS-SEC-124** — (0.4.0) When `external_llm_allowed = true`, every outbound prompt MUST still pass a pre-flight scan for P2, P3 and P4 content. A hit MUST fail the call closed and MUST record an `AuditEvent` with action `llm.external_call`, `outcome: "deny"`, `reason_code: "phi_in_prompt"`. The switch authorizes external inference; it does not authorize sending identifiers.
- **MOS-SEC-125** — Provider credentials MUST be per-tenant and envelope-encrypted. A platform-wide provider key usable by any tenant MUST NOT exist.

#### 8.6.8 Retention

- **MOS-SEC-126** — Every data class MUST have a default retention, tenant-configurable upward only for audit classes and in either direction for operational classes.

| Data | Default retention | Erasable on request | Mechanism |
|---|---|---|---|
| Source studies in the PACS | Site-owned | No — the platform does not own the PACS | Request forwarded to the site; documented as out of scope |
| De-identification UID mapping | Life of the tenant | Yes | Crypto-shred the tenant KEK-wrapped DEK |
| Platform study/series/instance projection | 7 years | Yes | Row delete + DEK destroy for P3 columns |
| Generated DICOM (results) | Site policy, default 7 years | No — superseded, never deleted (MOS-SEC-035) | `superseded_by` |
| `Job`, `JobStep`, `JobEvent` | 7 years | P3 columns only | DEK destroy |
| Execution artifacts (masks, intermediates) | 30 days | Yes | Object delete + DEK destroy |
| Service container stdout | 30 days | Yes | Object delete |
| `PolicyDecision` | 2 years | No | — |
| `AuditEvent` | ≥ 6 years, configurable upward | No | Signed export then partition detach (MOS-SEC-154) |
| Platform logs (P0/P1 only) | 90 days | N/A | — |
| Traces | 14 days | N/A | — |
| Metrics | 400 days | N/A | — |
| De-identified development corpus | Until the dataset version is deleted | N/A | `dataset_version.delete` |

#### 8.6.9 Erasure and crypto-shredding

An append-only audit table and a right-to-erasure obligation are only compatible if the erasable content is encrypted with a destroyable key. That is the design.

- **MOS-SEC-127** — The key hierarchy MUST be: KMS root key (per environment) → `Tenant` KEK (per tenant, in KMS, never exported) → DEK per `(tenant_id, subject_scope, epoch)` stored wrapped in `crypto_keys`. `subject_scope` is the patient for P3 columns and the job for execution artifacts.
- **MOS-SEC-128** — The following columns MUST be envelope-encrypted with the patient DEK: `patients.patient_name`, `patients.source_patient_id`, `patients.birth_date`, `studies.accession_number`, and every row of the de-identification UID mapping for that patient.
- **MOS-SEC-129** — An `ErasureRequest` MUST be a record: `(erasure_id, tenant_id, scope_kind ∈ {patient, study, tenant}, scope_id, requested_by, reason, requested_at, executed_at, keys_destroyed text[], objects_deleted int, rows_affected int)`. It MUST be created by a principal holding `phi.erase`.
- **MOS-SEC-130** — Execution MUST: destroy the named DEKs; delete the object-store prefixes in scope; null the encrypted columns; and append an `AuditEvent` of action `phi.erase`. It MUST NOT delete or modify any `AuditEvent` or `PolicyDecision` row — after erasure those rows still verify, and their P3 references are already absent by MOS-SEC-105.
- **MOS-SEC-131** — A tenant-scope erasure MUST additionally destroy `pepper[tenant_id]`, rendering every emitted `study_ref` unlinkable.
- **MOS-SEC-132** — The platform MUST report, in the erasure response, what it could not erase: objects in the site's PACS, objects already exported by a principal holding `study.export`, and any signed `ValidationReport` that references the dataset. Silent partial erasure MUST NOT occur.

### 8.7 Secrets and key management

- **MOS-SEC-133** — Secrets MUST NOT be committed to the repository. Development MUST use a `.env` file that is git-ignored and populated from `.env.example` containing only placeholder values. Production MUST use Kubernetes Secrets backed by an external store (Vault, or the platform's KMS) or Vault directly.
- **MOS-SEC-134** — Every secret MUST have a declared holder, store, rotation period and compromise procedure.

| Secret | Sole holder | Store | Rotation | On compromise |
|---|---|---|---|---|
| PACS credential | DICOM Gateway | Vault / K8s Secret | 90 d | Rotate at the PACS; Gateway reload; audit every `gateway.*` event in the exposure window |
| Postgres `medicalos_app` password | Control Plane, Gateway | Vault | 90 d | Rotate; RLS still holds, so exposure is bounded to the compromised tenant context |
| Object store STS role | Platform workers | Issued per job, TTL ≤ 1 h | Automatic | Revoke role; keys expire within the hour |
| Job Token signing key (Ed25519) | Control Plane signer | Vault transit | 30 d, two keys active | Publish `jti` revocation set; rotate; all tokens expire ≤ 6 h |
| Webhook HMAC key | Per webhook endpoint | DB, envelope-encrypted | 180 d, two keys active during overlap | Rotate; consumers accept either key for 24 h |
| `ApiKey` secret | Not stored (Argon2id hash only) | DB | ≤ 365 d, user-driven | Revoke by `key_id`; effective ≤ 5 s |
| Tenant KEK | KMS | KMS, never exported | 365 d (re-wrap DEKs) | Re-wrap; destroy old version |
| De-identification pepper | KMS | KMS, never exported | Never | Tenant-level incident; pseudonyms must be regenerated and old telemetry purged |
| Artifact signing key | CI OIDC identity (keyless) or offline HSM (keyed) | Sigstore / HSM | 365 d | Revoke in the trust policy; re-verify all deployed digests |
| LLM provider key | Per tenant | DB, envelope-encrypted | Tenant-driven | Tenant rotates at the provider |

- **MOS-SEC-135** — A secret MUST NOT be passed as a command-line argument or an environment variable where a file mount is possible. Job Tokens and the PACS credential MUST be file-mounted (MOS-SEC-025).
- **MOS-SEC-136** — Secret values MUST never be logged, echoed in an API response, included in a problem+json `detail`, or written to a trace. A secret's `key_id` MAY be logged.
- **MOS-SEC-137** — Two-key overlap MUST be supported for the Job Token signing key and webhook HMAC keys, so rotation never requires a coordinated restart.

### 8.8 Supply chain and artifact integrity

- **MOS-SEC-138** — Every artifact in the table below MUST be signed, and the signature MUST be verified at each listed point. Verification failure MUST block, never warn.

| Artifact | Format | Signing | Attestations | Verified at |
|---|---|---|---|---|
| Container image | OCI | cosign — keyless (Fulcio/Rekor) in cloud CI, keyed for air-gapped | SLSA provenance v1, CycloneDX SBOM | Registry admission; `deployment.create`; `runner.invoke` |
| `ServiceVersion` manifest | `service.yaml` + digest | Publisher key, detached | in-toto link to the build | `service_version.publish`; `deployment.create` |
| Model weights + `PreprocessingSpec` | OCI artifact | Same key as the ServiceVersion, covering both files as one subject | in-toto link to the `EvaluationRun` | Worker load, alongside the golden-fixture self-test (ch. 4, MOS-IMG) |
| `DatasetVersion` manifest | JSON, content-addressed | Platform key | — | Start of every `EvaluationRun` |
| `ValidationReport` | JSON + rendered PDF | Approver key **and** platform key | References model digest, dataset digest, preprocessing digest, run id | Import at a receiving site; export |
| `PolicySet` | tar, content-addressed | Platform operations key | SMT analysis result | PDP load; `policy.activate` |
| `AuditCheckpoint` | JSON | Platform key | — | `audit.verify`, offline |

- **MOS-SEC-139** — **Signing the model but not the claim about the model is backwards.** A signed weights blob with an unsigned, mutable performance record proves that bytes did not change while leaving the only clinically meaningful statement — what those bytes were shown to do, on which cohort, against which annotations — unprotected. The `ValidationReport`, the provenance record and the audit chain MUST therefore be signed with the same rigour as the image and the weights, and a report MUST bind the digests of every input it describes.
- **MOS-SEC-140** — Image references MUST be digest-pinned everywhere: manifests, Helm values, compose files, deployment rows. A mutable tag MUST NOT appear in any deployable configuration; CI MUST fail on `:latest` or any tag without an accompanying digest.
- **MOS-SEC-141** — Verification MUST support both keyless and keyed modes, and the keyed mode MUST work with no internet access. On-prem is the primary deployment shape; a verification path that requires a public transparency log at runtime would violate the spine's no-mandatory-cloud-dependency rule.
- **MOS-SEC-142** — An SBOM (CycloneDX 1.5 or SPDX 2.3) MUST be attached to every image and every model artifact and MUST be retrievable through the API for any deployed version.
- **MOS-SEC-143** — Vulnerability handling MUST follow a published budget.

| Severity (CVSS v3.1) | Action | Deadline |
|---|---|---|
| Critical (9.0–10.0), reachable | Block new deployments of the affected version; patch | 7 days |
| High (7.0–8.9), reachable | Patch; existing deployments flagged | 30 days |
| Medium (4.0–6.9) | Patch in the next minor | 90 days |
| Any severity, not reachable in the deployed configuration | Record the reachability analysis; no gate | — |

- **MOS-SEC-144** — Dependency and container scanning MUST run on every build and on a daily schedule against already-published digests, because a clean scan at build time says nothing about a CVE published afterwards.
- **MOS-SEC-145** — The `Deployment` row MUST record the digests verified at creation. A digest that later fails verification (revoked signature, recalled version) MUST move the deployment to `state = SUSPENDED` — Chapter 6's reversible stop (`MOS-REG-073`), never a row deletion — and MUST emit an `AuditEvent` and a webhook.

### 8.9 Audit

#### 8.9.1 The record

- **MOS-SEC-146** — The `AuditEvent` schema is the one shown in section 8.4.6, normatively:

| Field | Type | Required | Notes |
|---|---|---|---|
| `audit_id` | ULID | yes | |
| `tenant_id` | uuid | yes | RLS-scoped |
| `seq` | bigint | yes | Per-tenant, gapless, assigned by a sequence inside the writing transaction |
| `occurred_at` / `recorded_at` | timestamptz | yes | Both, so clock skew is visible |
| `actor` | jsonb | yes | `{kind, id, auth, key_id}`; `kind ∈ {user, service_account, workload, service_version, platform_admin}` |
| `on_behalf_of` | jsonb | no | `{kind, id, role}` — populated from the Job Token `obo` claim |
| `action` | text | yes | A permission identifier from `medos/contracts/permissions.yaml` |
| `resource` | jsonb | yes | `{kind, id}` plus `study_ref` where applicable; never a raw UID for a P3 subject |
| `outcome` | text | yes | `allow` \| `deny` \| `error` |
| `policy_decision_id` | ULID | no | Joins to `PolicyDecision` |
| `job_id` | ULID | no | **Required whenever the action occurs inside a job** |
| `trace_id` / `span_id` | hex | yes / no | W3C trace context |
| `request_id` | ULID | yes | Joins the authorization event to its effect event |
| `pep` | text | yes | One of the eight PEP ids |
| `break_glass_id` | ULID | no | Present for every event in a break-glass window |
| `source_ip` | inet | conditional | Required for an externally-originated action — one that entered through the API, the Gateway, a webhook replay or the OHIF surface. Null for a platform-originated action that has no client address (the retention sweep, the reconciler, the outbox relay); those rows carry `actor.kind = "workload"`, and the column is nullable in chapter 12. An address MUST NOT be fabricated to satisfy this field. |
| `user_agent` | text | no | Truncated to 200 chars |
| `detail` | jsonb | no | P0/P1 only; counts, reason codes, old/new non-PHI values |
| `prev_hash` / `hash` | text | yes | Section 8.9.3 |

- **MOS-SEC-147** — `actor` + `on_behalf_of` MUST be able to express "`pulmo.effusion@2.1.0` acting for `u_7d2f4a` in `job_01JB8N…`". A flat `actor: "user_123"` string MUST NOT be used; it cannot answer an access question about a delegated execution, which is the only kind of access question this platform generates.
- **MOS-SEC-148** — Every action whose permission class is `phi`, `clinical`, `admin` or `governance` MUST produce an `AuditEvent` on both `allow` and `deny`. Class `read` MAY be sampled at a tenant-configured rate, default 1.0.

#### 8.9.2 Write ordering and failure

- **MOS-SEC-149** — Where the audited action occurs inside a database transaction, the `AuditEvent` MUST be inserted in that same transaction. A committed state change with no audit row MUST NOT be possible.
- **MOS-SEC-150** — Where the action crosses a boundary with no shared transaction — a PACS read through the Gateway, a service invocation — two events MUST be written, joined by `request_id`: the authorization event (`action = study.read_pixels`, `outcome = allow`) committed **before** the upstream call, and the effect event (`action = study.read_pixels.completed`) after it, carrying counts in `detail`. Append-only storage forbids updating the first, so the pair is the mechanism. A failure to commit the authorization event MUST deny the request.

#### 8.9.3 Append-only and integrity

```sql
REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM medicalos_app;
GRANT  INSERT, SELECT             ON audit_events TO   medicalos_app;

CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only (attempted %)', TG_OP
    USING ERRCODE = '42501';
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_no_mutate
  BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
  FOR EACH STATEMENT EXECUTE FUNCTION audit_events_immutable();
```

The same two controls MUST be applied to `policy_decisions` and `policy_activations`.

- **MOS-SEC-151** — `hash` MUST be `SHA-256` over the RFC 8785 canonical JSON of the row with `hash` omitted and `prev_hash` included. `prev_hash` MUST be the `hash` of the row with `seq - 1` in the same tenant. The first row of a tenant MUST use the tenant's genesis constant.
- **MOS-SEC-152** — Every 10 000 events or every 24 hours, whichever comes first, the platform MUST write an `AuditCheckpoint`: `{tenant_id, seq_start, seq_end, merkle_root, count, created_at, key_id, signature}`, signed with the platform key. Checkpoints MUST be exportable so that a site can verify its own history offline, without the platform.
- **MOS-SEC-153** — `audit.verify` MUST recompute the chain over a range and report the first `seq` at which it diverges. A tamper test MUST exist in CI.
- **MOS-SEC-154** — `audit_events` MUST be range-partitioned by month. A partition MUST NOT be dropped; when retention expires it MUST be exported as a signed archive, the archive digest recorded in a permanently retained `audit_archives` row, and only then detached.

#### 8.9.4 Joining an audit record to what caused it

- **MOS-SEC-155** — The following joins MUST all resolve, and a CI test MUST walk the full chain for one completed job:

| From | To | Key |
|---|---|---|
| `AuditEvent` | `PolicyDecision` | `policy_decision_id` |
| `AuditEvent` | `Job` | `job_id` |
| `AuditEvent` | distributed trace | `trace_id`, `span_id` |
| `AuditEvent` (authorization) | `AuditEvent` (effect) | `request_id` |
| `Job` | `Result` → generated `SOPInstanceUID`s | provenance record (ch. 9, MOS-SAFE) |
| `Job` | `ServiceVersion`, `ModelVersion`, `PreprocessingSpec`, `PolicySet` | pinned digests on the job row |
| `Result` | `ValidationReport` | `model_version_id` → `EvaluationRun` → report |

### 8.10 What ships when

| Release | Security content |
|---|---|
| **0.1.0** | API keys; `Authenticator` port; one permission namespace and seeded roles; RLS forced with the repository chokepoint; DICOM Gateway as the sole PACS credential holder; Job Token with study scope; object-store prefixes; typed log API and the PHI scanner in CI; `AuditEvent` with `job_id`/`trace_id`/`on_behalf_of`, append-only at role level; signed images with digest pinning |
| **0.2.0** | Cedar PDP with the eight PEPs, baseline policies P-000/P-001/P-002/P-014/P-015/P-030; `PolicySet` as a signed artifact with per-tenant activation and shadow mode; `PolicyDecision` persisted and replayable; audit hash chain and signed checkpoints; crypto-shredding and `ErasureRequest`; signed `ValidationReport`; the `external_llm_allowed` column, the `llm.external_call` action and policy P-030 present and default-deny (there is no LLM adapter to enforce at before 0.4.0 — ch. 11, `MOS-AGENT-002`) |
| **0.3.0** | OIDC/JWT with per-tenant claim mapping; Kafka topic ACLs; MIG/partitioned GPU placement; remote PDP option; SMT policy analysis gating activation; `job.cancel` becomes implementable (spine section 4) |
| **0.4.0** | MCP session binding to tenant and execution context; the `llm.request` PEP inside the LLM adapter (MOS-SEC-123, MOS-SEC-124); LLM egress pre-flight scanning at scale; per-tenant telemetry isolation in hosted deployments |

- **MOS-SEC-156** — A release MUST NOT ship with a security requirement in its row unimplemented. If a gate slips, the requirement moves to the next release with an explicit entry in the delivery chapter (ch. 15, MOS-REL), never silently.

### Acceptance criteria

Each check is executable by CI or by a reviewer with a shell.

1. **RLS containment.** In a transaction bound to tenant A, `SELECT count(*) FROM jobs` returns only A's rows for a fixture containing rows for A and B. In a transaction with `medicalos.tenant_id` unset, the same query raises SQLSTATE `42704`. (MOS-SEC-072, MOS-SEC-074)
2. **RLS coverage.** The `pg_class` query in MOS-SEC-077 returns zero rows against a freshly migrated database.
3. **Role hardening.** `SELECT rolbypassrls FROM pg_roles WHERE rolname='medicalos_app'` is `false`; `has_table_privilege('medicalos_app','audit_events','UPDATE')` and the same for `DELETE` are both `false`. (MOS-SEC-073, §8.9.3)
4. **Chokepoint.** An import-graph test asserts that no package outside `internal/storage` imports `pgx` or `database/sql`. (MOS-SEC-075)
5. **Permission single source.** Regenerating from `medos/contracts/permissions.yaml` produces byte-identical Go constants, Cedar action schema and seed migration; a hand-added permission in any one of them fails the build. `medos/contracts/permissions.yaml` contains one key per row of the catalogue in section 8.3.2 and no others, and every permission identifier appearing anywhere in the repository — in `medos/api/v1/routes.{core,train}.yaml`, in a `Role`, in an `ApiKey.scope`, in a `JobToken.scope`, in a Cedar policy or in Go — is a key of that file. An identifier of four or more segments fails the regex of MOS-SEC-031. (MOS-SEC-031, MOS-SEC-032, MOS-SEC-033)
6. **Delete coverage.** For every permission `X.create` in the table, either `X.delete` exists or `X` appears in the never-deleted list of MOS-SEC-034. A test enumerates and asserts this, and asserts that no route, permission or handler named `deployment.delete` exists. (MOS-SEC-034, MOS-SEC-037a)
7. **No wildcards.** No seeded role and no fixture `ApiKey.scope` or `JobToken.scope` contains `*`. (MOS-SEC-033)
8. **Fail-closed PDP.** With the PDP forced to return an error, `GET /api/v1/results/{id}` returns `403` with `class: "policy_unavailable"`, and the replica reports not-ready. It MUST NOT return `200`. (MOS-SEC-057, MOS-SEC-059)
9. **The worked decision.** The fixture of section 8.4.6 evaluates to `forbid` with `determining_policies == ["P-014"]`, the API returns `403` (not `404`), and both a `PolicyDecision` and an `AuditEvent` are persisted, joined by `policy_decision_id` and carrying `reason_code: "deployment_not_production_serving"`. Three further fixtures MUST also evaluate to `forbid` on `P-014`: `deployment_environment = "production"` with `deployment_state = "SUSPENDED"`; `deployment_environment = "production"`, `deployment_state = "SERVING"`, `deployment_role = "SHADOW"`; and the same with `deployment_role = "STANDBY"`. A fixture with `production` / `SERVING` / `ACTIVE` MUST evaluate to `permit`. (MOS-SEC-066, MOS-SEC-067)
10. **Decision replay.** Every persisted `PolicyDecision` in the CI fixture set, re-evaluated against its `policy_set_digest`, yields the identical effect and determining policies. (MOS-SEC-071)
11. **Cross-tenant imaging.** A QIDO-RS request through the Gateway, authenticated as tenant A, for a `StudyInstanceUID` belonging to tenant B returns `403` — not an empty result — and writes an `AuditEvent` with `outcome: "deny"`. (MOS-SEC-090)
12. **Network boundaries.** From a running `sealed` service container, TCP connections to the PACS port, the PostgreSQL port, the Triton port and the object-store port all fail to connect. From `Z-EDGE`, the PACS is unreachable and every `/internal/v1/...` path is unreachable. (MOS-SEC-004, MOS-SEC-088, MOS-SEC-094)
13. **Job Token.** Four negative cases return `403` and one positive returns `200`: expired token; token for a job in state `COMPLETED`; request for a study not in `study_scope`; token whose `obo` principal's role grant has been revoked more than 60 s ago; and the valid in-scope request. (MOS-SEC-026, MOS-SEC-027)
14. **PHI scanner.** The scanner run over the repository, the end-to-end fixture log corpus, the exported spans and the exported metrics, seeded with the identifiers of the test studies, produces zero hits. (MOS-SEC-111)
15. **Metric discipline.** Every registered metric's labels are a subset of the MOS-SEC-112 allowlist, every label has a declared value enumeration, no label name contains a forbidden substring, no `deployment_state` series carries an environment value, and observed cardinality after the end-to-end suite is under the bound. (MOS-SEC-112–115)
16. **LLM gate (0.4.0 — not executable before the LLM adapter exists).** With `external_llm_allowed = false`, an attempted external call is denied by P-030, no packet reaches the provider host (asserted by a network recorder), and an `AuditEvent` is written. With it `true` and a prompt containing a `PatientName`, the call is denied with `reason_code: "phi_in_prompt"`. (MOS-SEC-123, MOS-SEC-124)
17. **Audit immutability and integrity.** `UPDATE audit_events SET action='x'` raises `42501`. Injecting a modified row directly as the table owner and running `audit.verify` reports divergence at exactly that `seq`. A checkpoint exported from the platform verifies with the platform public key using a standalone script that makes no network calls. (§8.9.3, MOS-SEC-153, MOS-SEC-152)
18. **Audit joins.** For one completed job, every row of the MOS-SEC-155 table resolves, including `AuditEvent → Job → Result → SOPInstanceUID` and `AuditEvent → PolicyDecision`.
19. **Delegated audit shape.** An `AuditEvent` produced by a service reading pixels has `actor.kind == "service_version"`, a non-null `on_behalf_of`, a non-null `job_id` and a non-null `trace_id`. (MOS-SEC-147)
20. **Supply chain.** Deployment of an image whose cosign signature is missing or whose digest does not match the manifest is refused at `deployment.create`. No deployable configuration in the repository references a tag without a digest. Verification succeeds with the network disabled in keyed mode. (MOS-SEC-138, MOS-SEC-140, MOS-SEC-141)
21. **Signed claims.** A `ValidationReport` whose approver signature is absent cannot reach `approved`; a report whose referenced model digest does not match the deployed model version is refused at import. (MOS-SEC-139)
22. **Crypto-shred.** After executing an `ErasureRequest` of scope `patient`, the patient's encrypted columns are unreadable, the DEK row is gone, the execution artifacts under the job prefixes are absent, and `audit.verify` over the tenant's full chain still passes. (MOS-SEC-130)
23. **API key hygiene.** A key created with no `expires_at` is rejected; a key with an expiry beyond 365 days is rejected; a revoked key stops working within 5 s across two replicas; the plaintext appears in exactly one response body and in no log line. (MOS-SEC-012, MOS-SEC-015, MOS-SEC-136)
24. **Effective permissions are an intersection.** A Job Token whose `scope` includes `study.export`, executing a `ServiceVersion` that does not declare `study.export`, is denied. A token whose scope is narrower than the `obo` principal's permissions is limited to the scope. (MOS-SEC-045)
25. **PEP coverage.** Every one of the eight PEPs is exercised by at least one test, and the handler-enumeration check finds no route or Gateway path that bypasses the middleware. (MOS-SEC-047, MOS-SEC-048)
26. **Roles are tenant data.** A tenant creates a role, edits a seeded role, and both survive a platform upgrade; no database constraint enumerates role keys; a grep of the codebase finds no hardcoded clinical job title as a role identifier. (MOS-SEC-038, MOS-SEC-041)
27. **Break-glass is read-only.** In a transaction bound to tenant A with `medicalos.break_glass` set to `on`, `SELECT` returns tenant B's `jobs` rows; an `INSERT` of a row carrying tenant B's `tenant_id` raises SQLSTATE `42501`, and an `UPDATE` or `DELETE` targeting tenant B's rows affects zero rows. `SELECT count(*) FROM pg_policies WHERE policyname ~ '_break_glass$' AND cmd <> 'SELECT'` returns `0` against a freshly migrated database. (MOS-SEC-157)
28. **Pipeline permission boundary.** The seeded `training_pipeline` role's permission array equals the list in MOS-SEC-158. A pipeline principal's call to `artifact.approve`, `deployment.create`, `deployment.promote`, `deployment.gate.override`, `result.submit` or `phi.reidentify` returns `403` at the PEP, with no pipeline-side check involved. (MOS-SEC-158, ch. 17 `MOS-TRAIN-009`)
29. **Evidence-plane network.** From the pipeline's training job container and from the MONAI Label server, a TCP connection to the PACS port fails to connect while the Gateway's DICOMweb port is reachable; each principal resolves to the `dataset_export` consumer class and holds no `phi.reidentify`. (MOS-SEC-159, ch. 17 `MOS-TRAIN-199`)

---

[← 7. Evidence Plane: Datasets, Evaluation and Validation Reports](07-evidence.md) · [Index](../../MEDICALOS_SPEC.md) · [9. Clinical Safety, Regulatory Posture and Provenance →](09-clinical-safety.md)
