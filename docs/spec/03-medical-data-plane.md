<!-- MedicalOS Specification v0.4.0 — chapter 3 of 19. Normative.
     88 requirements. Do not edit without a requirement-ID review. -->

[← 2. Architecture and the Medical Service Contract](02-architecture.md) · [Index](../../MEDICALOS_SPEC.md) · [4. Imaging Contracts: Geometry, Preprocessing and DICOM Output →](04-imaging-contracts.md)

---

## 3. Medical Data Plane: Gateway, De-identification and Triage

This chapter specifies everything that happens between a PACS and a `Job`. It owns four things: the **DICOM Gateway** (the only component in the system that holds a PACS credential), **de-identification** on Gateway egress, **study ingest** (how a study becomes known to the platform), and **triage and series selection** (how the platform decides which `ServiceVersion` runs on which series, and records why every other series was not used).

It does not own DICOM object writing (Chapter 4, `MOS-IMG`), job lifecycle (Chapter 5, `MOS-EXEC`), the registry that stores a `ServiceVersion` (Chapter 6, `MOS-REG`), or the permission model (Chapter 8, `MOS-SEC`). Those are cross-referenced by requirement ID.

### 3.1 Position, components and invariants

```
  PACS (Orthanc / dcm4chee / vendor)
        ^                     |  stored-instance callback / DICOMweb push
        | sole credential     v
  +-----+---------------------+-------------------------------------------+
  |                      DICOM  GATEWAY                                   |
  |  tenancy filter -> de-identification (egress) -> PHI access audit     |
  +---+-------------------+-------------------+-----------------------+---+
      |                   |                   |                       |
   Viewer           Ingest + Triage      Service containers     Platform DICOM
   (QIDO/WADO)      (WADO metadata)      (scoped token)         writer (STOW)
                          |
                          v
                 SeriesSelector evaluation
                          |
             +------------+-------------+
             |                          |
       TriageDecision              Job (Chapter 5)
    NOT_APPLICABLE | REJECTED      with pinned selected series
```

Every box and every arrow in this diagram is specified below: the Gateway surface in 3.2, de-identification in 3.3, ingest in 3.4, triage extraction in 3.5, selection in 3.6.

**MOS-DATA-001** The medical data plane consists of exactly these components: the **DICOM Gateway**, the **Ingest Controller**, the **Triage Engine**, and the **Selection Engine**. No other component MAY read from or write to a PACS.

**MOS-DATA-002** — **AMENDED at specification 0.4.0.** No component other than the DICOM Gateway MAY hold, mount, receive or derive a PACS credential. This includes the control plane, workers, service containers (both `native` and `sealed`), ~~the OHIF viewer~~ the operator surface~~s~~ and the origin that serves ~~them~~ it, the evidence-plane exporter, developer tooling, and Airflow-style batch jobs.

The amendment is to one item of the list and to nothing else: no component left the subject and none became conditional. The subject has always been *no component other than the Gateway*, and the list names instances rather than bounding it — one instance moved when the OHIF deployment was withdrawn at release 0.4.0, and a second when the engineering surface was withdrawn at specification 0.4.0. Naming a withdrawn product and not the surface that replaced it leaves a reviewer checking this requirement against a container the deployment does not start, which is the reading `MOS-DATA-015` is amended below to prevent. The surface is `MOS-UI-001`'s one — the clinician surface at `/mos-viewer/`~~ and the engineering surface at `/training-console/`~~ — and the origin is the single nginx (`web`, `medos-web`) that serves it~~ both~~; `MOS-DATA-015a`'s second clause is where the check on that origin is stated.

**MOS-DATA-003** A study MUST be triaged exactly once per `(tenant_id, study_instance_uid, triage_spec_version)`. Triage output MUST be cached and reused by every `SeriesSelector` evaluation against that study. Re-triage is permitted only under the conditions of MOS-DATA-063.

**MOS-DATA-004** A `Service` MUST NOT perform series selection. A service receives an explicit, ordered list of `SeriesInstanceUID`s in its job payload and MUST fail the job rather than substitute a different series. Broadcast filtering ("send every CT study to every service and let it decide") is forbidden.

### 3.2 The DICOM Gateway

#### 3.2.1 Credential custody and network isolation

**MOS-DATA-005** The Gateway is the sole holder of the PACS credential. The credential MUST be delivered to the Gateway process from a secret store at start-up and MUST NOT appear in any image layer, any Helm values file committed to the repository, or any environment variable of any other deployment unit.

**MOS-DATA-006** Network policy MUST make the PACS reachable only from the Gateway's network identity. In Kubernetes this is a `NetworkPolicy` whose `ingress.from` for the PACS pod names only the Gateway's pod selector; in `docker compose` it is a dedicated network joined only by the PACS and the Gateway. A deployment in which any other container can open a TCP connection to the PACS port is non-conformant, whether or not it has a credential.

#### 3.2.2 Exposed surface

**MOS-DATA-007** The Gateway MUST expose exactly the following DICOMweb surface and no more. `{t}` is a tenant id, `{st}` a `StudyInstanceUID`, `{se}` a `SeriesInstanceUID`, `{sop}` a `SOPInstanceUID`.

| Method | Path | Standard | Purpose |
|---|---|---|---|
| GET | `/dicomweb/{t}/studies` | QIDO-RS | study search, tenancy-filtered |
| GET | `/dicomweb/{t}/studies/{st}/series` | QIDO-RS | series search |
| GET | `/dicomweb/{t}/studies/{st}/series/{se}/instances` | QIDO-RS | instance search |
| GET | `/dicomweb/{t}/studies/{st}` | WADO-RS | study retrieve (multipart/related) |
| GET | `/dicomweb/{t}/studies/{st}/series/{se}` | WADO-RS | series retrieve |
| GET | `/dicomweb/{t}/studies/{st}/series/{se}/instances/{sop}` | WADO-RS | instance retrieve |
| GET | `/dicomweb/{t}/studies/{st}/series/{se}/instances/{sop}/frames/{f}` | WADO-RS | frame retrieve |
| GET | `/dicomweb/{t}/studies/{st}/metadata` | WADO-RS | study metadata, `application/dicom+json`, no pixel data |
| GET | `/dicomweb/{t}/studies/{st}/series/{se}/metadata` | WADO-RS | series metadata |
| GET | `/dicomweb/{t}/studies/{st}/series/{se}/rendered` | WADO-RS | rendered frame, viewer thumbnails only |
| POST | `/dicomweb/{t}/studies` | STOW-RS | store new instances |
| POST | `/dicomweb/{t}/studies/{st}` | STOW-RS | store into an existing study |

**MOS-DATA-008** The Gateway MUST NOT expose: any `DELETE` verb (0.1.0–0.2.0; deletion is an administrative operation performed against the PACS directly by an operator), the PACS product's native REST or administrative API, DIMSE services, or any path that proxies an unmatched URL to the backend. An unmatched path MUST return `404` with an RFC 9457 `problem+json` body and MUST NOT be forwarded.

#### 3.2.3 Tenancy filtering

**MOS-DATA-009** The effective tenant of a request is derived from the authenticated principal, never from the URL. The `{t}` path segment is a routing convenience and MUST be compared for equality with the principal's tenant; on mismatch the Gateway MUST return `403` with `problem+json` `type: "https://medicalos.dev/problems/tenant-mismatch"` and MUST emit an `AuditEvent` of type `gateway.tenant_mismatch`.

**MOS-DATA-010** Tenancy of imaging data is held in the `studies` projection (Chapter 12, `studies`), with `UNIQUE (tenant_id, study_instance_uid)` — Chapter 12's `studies_natural_uk` — and a non-unique index on `study_instance_uid`. There is no separate `study_tenancy` table: the projection row *is* the tenancy record, and it, not the PACS, is authoritative for who may see a study. Physical DDL is Chapter 12's; the key set and uniqueness constraints above are binding on it.

**MOS-DATA-011** QIDO-RS responses MUST be filtered by the Gateway against the `studies` projection (MOS-DATA-010) **after** the backend responds. The Gateway MUST NOT delegate the tenancy predicate to the backend query, because a PACS that has no tenancy model will silently ignore an unknown matching key and return everything. The filter is an intersection of the returned `StudyInstanceUID` set with the tenant's owned set.

**MOS-DATA-012** On STOW-RS the Gateway MUST resolve every `StudyInstanceUID` in the payload against the `studies` projection (MOS-DATA-010). If a study is owned by a different tenant, the whole request MUST be rejected with `409` and none of its instances stored. If a study is unknown, the Gateway MUST create the `studies` row for the calling principal's tenant in the same transaction that records the STOW attempt. Because this resolution is deliberately cross-tenant, it MUST be performed on the non-unique `study_instance_uid` index of MOS-DATA-010 and MUST NOT be satisfied by a tenant-scoped read alone — a query that cannot see another tenant's row reports the study as unknown and silently splits ownership.

**MOS-DATA-013** A QIDO-RS or WADO-RS request naming a study that exists in the backend but is not in the caller's tenant set MUST return `404`, not `403`. A `403` here is an existence oracle: it confirms that a given `StudyInstanceUID` — which encodes nothing secret but is guessable from a published dataset manifest — is present in the deployment. `403` is reserved for MOS-DATA-009 (tenant segment spoofing) and for a caller whose own tenant owns the study but who lacks `study.read`. Both outcomes are covered by the same acceptance check: never `2xx`, never any instance bytes.

**MOS-DATA-014** The Gateway MUST access its backend through a `PacsBackend` port resolved per tenant:

```go
type PacsBackend interface {
    BaseURL() string
    Credential() Credential
    SupportsRendered() bool
}
type BackendResolver interface {
    Resolve(ctx context.Context, tenantID string) (PacsBackend, error)
}
```

A single shared backend and one backend per tenant MUST both be expressible without a Gateway code change. The choice between them is an open deployment question (Chapter 16) and this chapter deliberately does not settle it.

#### 3.2.4 Consumers

**MOS-DATA-015** — **AMENDED at specification 0.4.0.** ~~OHIF MUST be configured against the Gateway and MUST NOT be configured against the PACS. The conformant configuration is:~~

**The clinician surface MUST be configured against the Gateway and MUST NOT be configured against the PACS.** The MUST is unchanged; its subject is re-pointed and nothing else about it moves. OHIF was the surface this requirement happened to name between 0.1.0 and 0.3.0, never the property it protects: tenancy filtering (MOS-DATA-011), the existence-oracle rule (MOS-DATA-013), quarantine invisibility (MOS-DATA-024) and the PHI access audit (MOS-DATA-022) are properties of the Gateway and of nothing else in this chapter, and a viewer configured against the PACS has none of them however carefully it is written. `MOS-CORE-038` was reversed at release 0.4.0 and the clinician surface is now the first-party viewer of `MOS-UI-009a` — `viewer/`, served at `/mos-viewer/`. Leaving the requirement pointed at a product this deployment no longer runs would have withdrawn the obligation without anyone deciding to withdraw it: the mirror image of register entry 103, where a requirement outlived its authority, and worse, because a requirement that binds nothing still reads as passing.

**MOS-DATA-015a** Conformant configuration for that surface is the four clauses below. They carry a number of their own rather than extending `MOS-DATA-015`, because `MOS-CORE-023` requires an id to mean one thing across versions: through 0.3.0 `MOS-DATA-015` was checked by reading the `window.config` block below against the deployment, and a conformance report written against 0.2.0 or 0.3.0 MUST NOT be readable as having checked four clauses that did not exist when it was written. Each clause is written so it can be checked rather than asserted, and where the check does not exist yet this requirement says so instead of implying one:

- **One DICOMweb base, and it is the Gateway's tenant prefix.** Study search, instance retrieve and any thumbnail route MUST resolve from a single `/dicomweb/{t}` base on the Gateway (MOS-DATA-007). Two bases are two routes, and the one that is not the Gateway is the one that appears in no `phi.access` row. In this repository `viewer/app.js` constructs exactly one `DicomWebClient` with `root: '/dicomweb/<tenant>'`, and `viewer/src/dicom/dicomweb.js` is the only module in the surface that issues a DICOMweb request. **Nothing asserts either fact today.** A static gate over `viewer/` MUST assert that the client is constructed from one base and that no module outside `src/dicom/dicomweb.js` issues a request to a DICOMweb path; until it does, this clause holds by reading the source, which is exactly the standard `MOS-UI-010a` declines to accept for the same surface.
- **No PACS address in the surface or in the origin that serves it.** Neither the served bundle, nor its configuration file, nor the origin in front of it MAY resolve a PACS host (`MOS-UI-002`, `MOS-DATA-002`). `MOS-DATA-006` makes the connection unopenable; this clause is what keeps the surface from being written to attempt it, which matters because the failure then appears at deployment rather than in review. This one is checked: `tests/integration/test_gateway.py::test_the_worker_and_the_viewer_are_routed_through_the_gateway` reads every `proxy_pass` in `medos/deploy/compose/nginx.conf.template` and fails if any names the PACS, and `location /dicomweb/` in that file proxies to the Gateway.
- **No credential in the bytes the browser receives.** The surface MUST hold no PACS credential (`MOS-DATA-002`) and MUST NOT carry a platform credential in a served file; the Gateway token is attached server-side by the origin. **The existing check does not reach this surface.** `tests/e2e/test_demo.py` strips comments and scans five served files — the standalone page and its `app.js`, `src/core/client.js` and `src/core/render.js`, all four under `/medicalos/`, and `app-config.js`, which this origin serves at its root — for an embedded `bearer` or `basic` header, for `btoa(`, and for the PACS port and the credential variable names. It scans nothing under `/mos-viewer/`. Extending that scan to every file the origin serves under `/mos-viewer/` is what makes this clause true of the first-party viewer rather than only of its predecessor.
- **A replacement surface is bound identically.** A viewer this platform did not write, pointed at the same `/dicomweb/{t}` prefix, inherits tenancy filtering and PHI auditing for free; pointed anywhere else it is non-conformant whoever wrote it. That is what keeps `MOS-IMG-158`'s verification in a second, independent viewer a check on rendering rather than a second route to the archive, and it is the sense in which building a first-party viewer did not make the imaging surface unreplaceable.

The OHIF `window.config` data source block `MOS-DATA-015` carried through 0.3.0 is kept below rather than deleted, struck by the comment it now opens with, for the reason `docs/spec/19-operator-surfaces.md` §19.1.2 gives: a reader holding a conformance report written against 0.2.0 or 0.3.0 checked that block by its contents and needs to find what was checked. It is not conformant configuration for anything this deployment runs. `deploy/compose/ohif-config.js` was removed at 0.4.0, and `medos/deploy/compose/medicalos-config.js` carries only the half of it that survived — the extension's deployment configuration, and no data source at all.

```js
// WITHDRAWN at specification 0.4.0 together with the sentence struck above. Retained,
// unaltered, for readers of a 0.2.0 or 0.3.0 conformance report. Markdown cannot strike
// a fenced block through, so this comment is the strikethrough. This deployment runs no
// OHIF application: the `web` service is a plain nginx and the origin redirects / to
// /mos-viewer/. Nothing below is configuration for any surface MedicalOS now ships.
window.config = {
  dataSources: [
    {
      namespace: '@ohif/extension-default.dataSourcesModule.dicomweb',
      sourceName: 'medicalos',
      configuration: {
        friendlyName: 'MedicalOS Gateway',
        name: 'medicalos',
        qidoRoot:    'https://medicalos.example.org/dicomweb/t_a41f',
        wadoRoot:    'https://medicalos.example.org/dicomweb/t_a41f',
        wadoUriRoot: 'https://medicalos.example.org/dicomweb/t_a41f',
        qidoSupportsIncludeField: true,
        supportsReject: false,
        supportsFuzzyMatching: false,
        imageRendering: 'wadors',
        thumbnailRendering: 'wadors',
        enableStudyLazyLoad: true,
        omitQuotationForMultipartRequest: true,
        bulkDataURI: { enabled: true },
      },
    },
  ],
  defaultDataSourceName: 'medicalos',
};
```

**MOS-DATA-016** — **AMENDED at specification 0.4.0.** ~~The OHIF "Analyze with MedicalOS" control MUST call `POST /api/v1/jobs` (Chapter 10) and MUST NOT call the Gateway. The viewer's only Gateway traffic is image retrieval. This keeps the viewer replaceable: a third-party viewer pointed at the same `qidoRoot` inherits tenancy filtering and PHI auditing for free.~~

**An "Analyze with MedicalOS" control MUST call `POST /api/v1/jobs` (Chapter 10) and MUST NOT call the Gateway, wherever the control ships — on either operator surface of `MOS-UI-001`, or in an extension package mounted on the origin that serves one. A surface's only Gateway traffic is image retrieval.** The MUST is unchanged. The separation is not tidiness: the Gateway's surface is closed (MOS-DATA-007) and MOS-DATA-008 forbids an unmatched path being forwarded, so a control that reached the Gateway to start work would be asking for a route that does not exist — or, worse, would be using STOW and the archive as a work queue, where nothing decides permission, nothing decides tenancy from the principal, and no `Idempotency-Key` is honoured. `POST /api/v1/jobs` is the only job-creation path (`MOS-API-001`, `MOS-SAFE-089a`), and it is the only place those three decisions are made.

The code this now binds is `medos/web/ohif-extension/`, served at `/medicalos/` on the clinician surface's origin, which was not withdrawn at 0.4.0. It is a mount and not a third surface — `MOS-UI-001` counts it as one — which is why the sentence above binds the control wherever it ships rather than naming a surface to carry it. It constructs its client against `window.MEDICALOS.apiRoot = '/api/v1'` (`medos/deploy/compose/medicalos-config.js`) and knows no Gateway address at all; `tests/unit/test_viewer_wire_contract.py` checks the envelope it posts against the Chapter 10 model, and the extension's own configuration holds no DICOMweb root to call.

**What this amendment does not repair, stated because the requirement now reads as satisfied.** The first-party clinician viewer at `viewer/` has no "Analyze with MedicalOS" control and issues no `/api/v1` request of any kind: apart from its own static assets — `presets.json`, `build.json` and the `i18n/` bundles, all served from `/mos-viewer/` — every request it makes is to `/dicomweb/`. MOS-DATA-016 is therefore satisfied by it vacuously — there is no control to check — while the obligation the platform actually holds, that a reader can start an analysis on the study open in front of them, is unmet on the surface a reader now opens. This is `MOS-UI-013`'s gap reached from the other side: at 0.3.0 the control existed as unexecuted source inside a host that never loaded it; at 0.4.0 the host runs and the control is not on it. ~~`MOS-SAFE-089a` is inapplicable rather than failed only where the button is cut in a Release Decision Record under `MOS-REL-009`, and `MOS-UI-013a` already forbids recording that cut as a pass.~~ **AMENDED at specification 0.4.0**, because chapter 9 struck the escape this sentence pointed at: `MOS-SAFE-089a` is now unconditional and its acceptance check 24 FAILS rather than lapses. The gap named here is therefore a failing check with no cut available to retire it, which is a stronger statement than the one struck and not a weaker one. Closing it is Chapter 19's, not this chapter's; this chapter's obligation is to say that re-pointing the requirement moved who it binds and did not move the surface a clinician uses.

**MOS-DATA-017** A `Service` container MUST receive a **scoped Gateway token**, never a credential. The token is minted by the Job dispatcher (Chapter 5) at dispatch and presented as `Authorization: Bearer <token>`.

**MOS-DATA-018** The token is a JWT signed by the control plane with these claims, all required:

```json
{
  "iss": "medicalos.control-plane",
  "aud": "medicalos.gateway",
  "sub": "svc:pulmoai.pleural-effusion:2.1.0",
  "tenant_id": "t_a41f",
  "job_id": "job_01JX8F3QK2M7",
  "scope": ["dicomweb.read"],
  "study_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000041",
  "series_instance_uids": [
    "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000188"
  ],
  "deid_profile_version": 3,
  "iat": 1757750400,
  "exp": 1757752200
}
```

The Gateway MUST reject any request from a service principal whose target study or series is not listed in the presented token, with `403` and `problem+json` `type: "https://medicalos.dev/problems/outside-token-scope"`. `scope` MUST NOT contain a write verb for a service principal: a service cannot STOW (spine §2, ownership boundary).

**MOS-DATA-019** Token lifetime MUST be `min(job_deadline_at, iat + 1800s)` and the Gateway MUST reject a token whose `job_id` refers to a job in a terminal state, re-checked at most every 30 s against the job table. Expiry is not sufficient on its own: a cancelled or failed job must lose PHI access immediately.

**MOS-DATA-020** The platform DICOM writer (Chapter 4) is the only principal that may STOW AI-derived objects, and it does so through the Gateway with a `platform_writer` principal. It operates in the **source UID space** (MOS-DATA-021), because the objects it writes must reference the study as the hospital knows it.

**MOS-DATA-021** Gateway egress behaviour is selected by **consumer class**, resolved from the principal and the `clinical_use_mode` of the relevant `Deployment` (Chapter 9):

| Consumer class | Principal | De-identification on egress | UID space | Pixel-PHI screening |
|---|---|---|---|---|
| `clinical_viewer` | `User` with `study.read`, Deployment `clinical_use_mode = clinical` | none | source | none |
| `research_viewer` | `User` with `study.read`, `clinical_use_mode = research_only` | per tenant policy | de-identified | per policy |
| `service` | `ServiceAccount` with a scoped token | per tenant policy, never weaker than `BASIC` | de-identified | enforced |
| `platform_writer` | control-plane DICOM writer | none | source | not applicable |
| `dataset_export` | evidence-plane exporter (Chapter 7) | per tenant policy with `clean_pixel_data: true` forced | de-identified | enforced |

A consumer class that cannot be determined MUST be treated as `service`. This is the conservative default: it de-identifies.

#### 3.2.5 PHI access audit

**MOS-DATA-022** The Gateway MUST emit one `AuditEvent` per PHI-bearing response. This is the only point in the architecture where a per-patient image access can be recorded, because every image read in the system passes through it. The record:

```json
{
  "event_type": "phi.access",
  "tenant_id": "t_a41f",
  "principal": "svc:pulmoai.pleural-effusion:2.1.0",
  "on_behalf_of": "user_9f13",
  "job_id": "job_01JX8F3QK2M7",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "patient_key": "pk_7c1e0a44",
  "study_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000041",
  "series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000188",
  "instance_count": 412,
  "operation": "WADO-RS.series",
  "consumer_class": "service",
  "deid_profile_version": 3,
  "bytes": 214958080,
  "outcome": "ALLOW",
  "occurred_at": "2026-09-13T09:41:12.338Z"
}
```

`patient_key` is a tenant-scoped surrogate, never a `PatientID`. No attribute value from the study, including `PatientName`, `StudyDescription` or `SeriesDescription`, MAY appear in this record (spine §8).

**MOS-DATA-023** WADO-RS retrieval MUST stream: the Gateway MUST NOT buffer a whole series in memory. It MUST support `Accept: multipart/related; type="application/dicom"` and `Accept: application/dicom+json`. It MUST enforce a per-principal concurrency limit (default 4 in-flight retrievals) and a per-response instance cap (default 5000); exceeding either returns `429` with `Retry-After`.

**MOS-DATA-024** Quarantined instances (MOS-DATA-047, MOS-DATA-048) MUST be invisible through every Gateway route to every principal except one holding `phi.admin`, and MUST NOT appear in QIDO-RS results.

#### 3.2.6 Failure behaviour

**MOS-DATA-025** The Gateway MUST fail closed. If the `studies` projection (MOS-DATA-010) is unreachable, if the de-identification policy for the tenant cannot be loaded, or if the `deid_uid_map` store is unavailable, the Gateway MUST return `503` and MUST NOT fall back to unfiltered or un-de-identified passthrough. The circuit breaker used for backend faults MUST NOT be applied to the tenancy or de-identification path.

#### 3.2.7 Required metrics

**MOS-DATA-026** The Gateway MUST expose: `medicalos_gateway_requests_total{operation,consumer_class,status}`, `medicalos_gateway_deid_duration_seconds{profile_version}`, `medicalos_gateway_deid_failures_total{reason}`, `medicalos_gateway_uidmap_lookups_total{result}` where `result ∈ {hit, minted}`, and `medicalos_gateway_tenant_denials_total{reason}`. Label values MUST NOT contain a UID, a patient identifier or a description string (spine §8).

### 3.3 De-identification

#### 3.3.1 Profile and policy

**MOS-DATA-027** De-identification MUST implement the DICOM **PS3.15 Annex E** Basic Application Level Confidentiality Profile as its base, with Annex E options selected per tenant. Ad-hoc tag blacklists are forbidden; the implementation MUST be driven by the PS3.15 Table E.1-1 attribute action codes.

**MOS-DATA-028** The de-identification policy is a versioned artifact per tenant, `deid_policy`, with an integer `deid_policy_version` that is monotonic and never reused. It is pinned into provenance (Chapter 9) and into every `DatasetVersion` (Chapter 7).

```yaml
deid_policy_version: 3
tenant_id: t_a41f
profile: BASIC                      # PS3.15 E.1 Basic Application Level Confidentiality Profile
options:
  retain_uids: false                # MUST be false; see MOS-DATA-031
  retain_safe_private: true
  retain_device_identity: true
  retain_institution_identity: false
  retain_patient_characteristics: true
  retain_longitudinal_temporal: MODIFIED_DATES   # NONE | FULL_DATES | MODIFIED_DATES
  clean_descriptors: true
  clean_pixel_data: true
  clean_recognizable_visual_features: false
  clean_structured_content: true
  clean_graphics: true
date_shift:
  scope: per_patient                # per_patient | per_tenant
  range_days: [-3650, -1]
pixel_phi:
  detector: ocr_v2
  action: REJECT                    # REJECT | BLACKOUT | ALLOW
  always_screen_modalities: [CR, DX, MG, US, XA, RF, SC, OT]
  screen_when_image_type_contains: [SECONDARY, DERIVED]
  screen_when_burned_in_annotation: [YES, UNKNOWN]
private_tags:
  default: REMOVE
  allowlist:
    - {creator: "SIEMENS CT VA1 DUCT", group: "0029", elements: "1010-1020",
       reason: "reconstruction kernel and dose detail required by triage"}
    - {creator: "GEMS_ACQU_01", group: "0043", elements: "1039-1039",
       reason: "CTDIvol on GE CT"}
```

**MOS-DATA-029** Every de-identified object MUST carry `(0012,0062) PatientIdentityRemoved = YES`, `(0012,0063) DeidentificationMethod` naming the implementation and `deid_policy_version`, and `(0012,0064) DeidentificationMethodCodeSequence` populated from CID 7050 with exactly the codes corresponding to the applied options:

| Applied | Code value (DCM) | Code meaning |
|---|---|---|
| always | `113100` | Basic Application Confidentiality Profile |
| `clean_pixel_data: true` | `113101` | Clean Pixel Data Option |
| `clean_recognizable_visual_features: true` | `113102` | Clean Recognizable Visual Features Option |
| `clean_graphics: true` | `113103` | Clean Graphics Option |
| `clean_structured_content: true` | `113104` | Clean Structured Content Option |
| `clean_descriptors: true` | `113105` | Clean Descriptors Option |
| `retain_longitudinal_temporal: FULL_DATES` | `113106` | Retain Longitudinal Temporal Information Full Dates Option |
| `retain_longitudinal_temporal: MODIFIED_DATES` | `113107` | Retain Longitudinal Temporal Information Modified Dates Option |
| `retain_patient_characteristics: true` | `113108` | Retain Patient Characteristics Option |
| `retain_device_identity: true` | `113109` | Retain Device Identity Option |
| `retain_safe_private: true` | `113111` | Retain Safe Private Option |

**MOS-DATA-030** `113110` (Retain UIDs Option) MUST NEVER be emitted and `retain_uids` MUST NEVER be `true`. UIDs are remapped, consistently, under MOS-DATA-031.

Attribute actions for the attributes that matter most in practice — `K` keep, `X` remove, `Z` replace with zero length, `D` replace with a non-zero dummy, `U` UID-remap, `S` date/time shift, `C` clean:

| Tag | Name | Action | Note |
|---|---|---|---|
| (0010,0010) | PatientName | `D` | tenant pseudonym, stable per patient |
| (0010,0020) | PatientID | `D` | `pk_<hex>`, stable per patient, the `patient_key` of MOS-DATA-022 |
| (0010,0021) | IssuerOfPatientID | `X` | |
| (0010,0030) | PatientBirthDate | `S` | shifted with the same offset as study dates, preserving age arithmetic |
| (0010,0040) | PatientSex | `K` | Retain Patient Characteristics |
| (0010,1010) | PatientAge | `K` | |
| (0010,1020) (0010,1030) | PatientSize, PatientWeight | `K` | needed for dose and normalisation |
| (0008,0020) (0008,0021) (0008,0022) (0008,0023) | Study/Series/Acquisition/Content Date | `S` | |
| (0008,0030)–(0008,0033) | corresponding Times | `K` | times are not shifted; only dates |
| (0008,0050) | AccessionNumber | `D` | tenant-scoped surrogate, stable |
| (0008,0080) (0008,0081) (0008,1040) | Institution Name/Address/Department | `X` | unless `retain_institution_identity` |
| (0008,0090) (0008,1048)–(0008,1070) | Referring/Physician/Operator names | `X` | |
| (0008,1030) | StudyDescription | `C` | cleaned; retained because triage applicability reads it |
| (0008,103E) | SeriesDescription | `C` | cleaned; retained because triage reads it |
| (0018,1030) | ProtocolName | `C` | |
| (0008,0070) (0008,1090) (0018,1000) (0018,1020) | Manufacturer, Model, DeviceSerialNumber, SoftwareVersions | `K` | Retain Device Identity; required by the applicability envelope |
| (0020,000D) (0020,000E) (0008,0018) (0020,0052) | Study/Series/SOP Instance, FrameOfReference UID | `U` | MOS-DATA-031 |
| any VR `UI` not under `1.2.840.10008` | — | `U` | MOS-DATA-034 |
| any odd group | private attributes | `X` | except `private_tags.allowlist` |
| (0028,0301) | BurnedInAnnotation | see MOS-DATA-040 | |

**MOS-DATA-031 — BLOCKING INVARIANT.** UID remapping MUST be **deterministic, consistent and reversible within a tenant**. For a fixed `(tenant_id, deid_key_version)` the same source UID MUST map to the same de-identified UID forever, across processes, restarts, policy-version bumps and re-ingests, and two distinct source UIDs MUST NEVER map to the same de-identified UID. Per-invocation or per-export random remapping is forbidden.

The failure mode, written out because it is silent and unrecoverable:

1. **Every result ever produced is orphaned.** A DICOM SEG carries `ReferencedSeriesSequence` and per-frame `SourceImageSequence` entries; a TID 1500 SR carries evidence sequences; a `ResultBundle` carries per-finding source SOP Instance UID references (spine §2). All of them name UIDs that the service saw. If the next de-identification run mints different UIDs, those references resolve to nothing. A viewer shows a segmentation floating over a study it claims not to belong to, or shows nothing at all, with no error anywhere.
2. **Every `DatasetVersion` silently changes identity.** A `DatasetVersion` is a content-addressed manifest of SOP Instance UIDs plus a hash (spine §10). Random remapping changes the manifest hash on every export of the same cohort, so no `EvaluationRun` is reproducible and no `ValidationReport` can be re-verified offline — which destroys the one asset the platform exists to produce.
3. **DICOM output idempotency breaks.** Chapter 4 derives output `SeriesInstanceUID`/`SOPInstanceUID` from `(idempotency_key, model_id, model_version, output_index)` and skips re-inference when the derived series is already present (spine §7). If the inputs that feed the idempotency key shift per run, the QIDO-RS skip check never matches and every retry accumulates a duplicate SEG series in the PACS, permanently and undeduplicatable after the fact.

None of the three produces an exception at the time it happens. Detection is months later, by a radiologist looking at an overlay that does not line up.

**MOS-DATA-032** The de-identified UID MUST be generated by the following function, and the implementation MUST be a single shared library used by the Gateway, the evidence-plane exporter and the ingest controller:

```python
import hmac, hashlib, uuid

def deid_uid(tenant_deid_key: bytes, source_uid: str) -> str:
    """Deterministic, key-separated, collision-resistant UID surrogate.
    Output is the DICOM 2.25.<uuid-as-integer> form, max 44 characters."""
    digest = hmac.new(tenant_deid_key, source_uid.encode("ascii"), hashlib.sha256).digest()
    b = bytearray(digest[:16])
    b[6] = (b[6] & 0x0F) | 0x40          # UUID version 4
    b[8] = (b[8] & 0x3F) | 0x80          # RFC 4122 variant
    return "2.25." + str(uuid.UUID(bytes=bytes(b)).int)
```

The key is per tenant, held in the secret store, and versioned as `deid_key_version`. Rotating the key is a **breaking** operation: it invalidates every existing reference and MUST be treated as creating a new UID space, recorded as such, **with all prior mappings retained**. A rotation MUST NOT delete, overwrite or re-key any existing row; the map is a bijection *within one* `(tenant_id, deid_key_version)` UID space, and a rotation opens a new space beside the old one. Destroying the old space would orphan every result ever produced under it, which is precisely the failure MOS-DATA-031 forbids.

**MOS-DATA-033** The `deid_uid_map` record is authoritative; MOS-DATA-032 is the generator that allows it to be rebuilt. The physical table is Chapter 12's (`deid_uid_map`, MOS-STORE-249, MOS-STORE-250); the field names, key set and constraints below are binding on that DDL: `(tenant_id, deid_key_version, source_uid_hmac)` UNIQUE; `(tenant_id, deid_key_version, mapped_uid)` UNIQUE; `uid_kind ∈ {study, series, sop, frame_of_reference, other}`; `created_at`. The lookup key `source_uid_hmac` is the HMAC of the source UID under the tenant key, and the source UID itself is held only as AEAD ciphertext (`source_uid_ct`, MOS-STORE-250) — which is what lets MOS-DATA-035 reverse a mapping without the database holding a plaintext UID. Rows MUST be append-only: `UPDATE` and `DELETE` MUST be revoked from the application database role. A generated UID that collides with an existing row for a different source UID MUST abort the de-identification with `deid_failed` rather than overwrite.

**MOS-DATA-034** A UID whose value begins with `1.2.840.10008` (the DICOM-registered root: SOP Class UIDs, Transfer Syntax UIDs, well-known coding scheme and frame-of-reference UIDs) MUST be copied unchanged. Every other attribute with VR `UI`, at any nesting depth inside any sequence, MUST be remapped. The implementation MUST walk sequences recursively; a tag-name allowlist is not acceptable because `ReferencedSOPInstanceUID` appears in dozens of sequence contexts.

**MOS-DATA-035** Re-identification (`mapped_uid → source UID`, by decrypting `source_uid_ct`) MUST be available only to a principal holding `phi.reidentify` (Chapter 8), MUST be audited with an `AuditEvent` of type `phi.reidentify` naming the requesting principal and the reason string, and MUST NEVER be exposed to a `service` consumer class or through any MCP tool.

**MOS-DATA-036** The platform MUST translate every source SOP Instance UID, Series Instance UID and Frame of Reference UID carried in a returned `ResultBundle` from the de-identified space back to the source space, via `deid_uid_map`, **before** handing the bundle to the DICOM writer of Chapter 4. A `ResultBundle` that references a UID which is not in the job's de-identified working set MUST terminate the job in `FAILED` with reason `result_references_unknown_uid`, and the platform MUST NOT write any DICOM object for that job. This check is the operational half of MOS-DATA-031: without it, a service that invents or mis-copies a UID produces a structurally valid but clinically meaningless SEG.

**MOS-DATA-037** De-identification MUST fail closed. Any error — unreadable attribute, unmappable UID, detector failure, policy load failure — MUST abort the egress with `503` for a viewer or `deid_failed` for a triage or job path, and MUST NOT emit a partially de-identified object.

#### 3.3.2 Pixel-burned-in PHI

**MOS-DATA-038** The Gateway MUST screen for burned-in PHI according to the tenant's `pixel_phi` block, using this decision order:

1. If `(0028,0301) BurnedInAnnotation` is `NO` and the modality is not in `always_screen_modalities` and `ImageType` contains none of `screen_when_image_type_contains`, the series is presumed clean and no screening is performed.
2. Otherwise the series is screened by the configured `PixelPhiDetector`.

**MOS-DATA-039** The detector is a port, not an implementation:

```python
class PixelPhiDetector(Protocol):
    name: str
    version: str
    def scan(self, frames: Sequence[np.ndarray], meta: InstanceMeta) -> list[PhiRegion]: ...

@dataclass(frozen=True)
class PhiRegion:
    frame_index: int
    x: int; y: int; width: int; height: int
    confidence: float
    text_hash: str          # SHA-256 of the recognised text; the text itself is never stored
```

`PhiRegion.text_hash` exists so two runs can be compared without persisting recognised PHI. The recognised text MUST NOT be written to logs, spans, events or the database.

**MOS-DATA-040** Policy action on a non-empty detection:

| `pixel_phi.action` | Behaviour |
|---|---|
| `REJECT` | the series is excluded at triage with per-series reason code `burned_in_phi`; the series is never sent to a service |
| `BLACKOUT` | detected regions are overwritten with the pixel value corresponding to air for the photometric interpretation, `(0028,0301)` is set to `NO`, code `113101` is added to `(0012,0064)`, and the burn-out rectangle list is recorded in provenance |
| `ALLOW` | no modification; permitted only when the consumer class is `clinical_viewer` |

**MOS-DATA-041** The specification states plainly that OCR-based detection is a mitigation, not a guarantee. Therefore: a CT series whose `ImageType[0] = ORIGINAL` and `ImageType[1] = PRIMARY` MAY be presumed free of burned-in text under rule 1 of MOS-DATA-038, and any tenant that does not accept that presumption MUST set `screen_when_burned_in_annotation: [YES, NO, UNKNOWN]`, which forces universal screening at a stated throughput cost. Screening pulls decimated frames (every 10th frame, downsampled to 512 px on the long axis) rather than the full series.

**MOS-DATA-042** Screening MUST NOT be performed during triage metadata extraction (MOS-DATA-058). `burned_in_state` is a closed four-value enum, and this requirement is normative for it:

| `burned_in_state` | Meaning |
|---|---|
| `UNSCREENED` | screening has neither run nor been determined unnecessary; the value every series carries at triage |
| `CLEAN` | the series was screened and the detector returned no region, or rule 1 of MOS-DATA-038 determined that no screening is required |
| `SUSPECTED` | at least one `PhiRegion` was detected and the pixels were left unmodified — `pixel_phi.action` `REJECT` or `ALLOW` (MOS-DATA-040) |
| `REDACTED` | at least one `PhiRegion` was detected and burned out under `pixel_phi.action: BLACKOUT` (MOS-DATA-040) |

No other value is permitted. Chapter 12's `CHECK` constraint on the column mirrors exactly this set and MUST NOT diverge from it. A series whose screening result is not yet known enters selection with `burned_in_state: UNSCREENED`; a selector declaring `burned_in_annotation: forbid` triggers screening on demand for candidate series only, never for the whole study, and admits the series only once it reaches `CLEAN` or `REDACTED`.

**MOS-DATA-043** The platform MUST NOT hold a second, identified copy of imaging data outside the PACS. The only permitted persistent de-identified copy is a sealed `DatasetVersion` export produced by the evidence plane (Chapter 7) through the `dataset_export` consumer class. Intermediate de-identified volumes held by a running job are ephemeral, scoped to the job, and MUST be deleted on job termination.

### 3.4 Study ingest

**MOS-DATA-044** There are exactly three ingest paths. All three converge on one `StudyIngest` record and one triage.

| Path | Trigger | Transport | Tenant source | Release |
|---|---|---|---|---|
| A — stored-instance callback | PACS receives a C-STORE or STOW | PACS plugin → Gateway internal endpoint | calling AE title via `ae_tenant_map` | 0.1.0 |
| B1 — DICOMweb push | remote system STOWs to MedicalOS | `POST /dicomweb/{t}/studies` | the STOW principal | 0.1.0 |
| B2 — DICOMweb poll | scheduled poll of a remote DICOMweb source | QIDO-RS with a watermark | the configured poll source | 0.2.0 |
| C — manual API push | operator or SDK | `POST /api/v1/studies/{study_id}/ingest` | the calling credential, requires `study.ingest` | 0.1.0 |

#### 3.4.1 The `StudyIngest` record

**MOS-DATA-045** Each `(tenant_id, study_instance_uid)` has one `study_ingest` row with the state machine `RECEIVING → STABLE → TRIAGED`, plus `QUARANTINED` (terminal until an operator acts) and `SUPERSEDED` (re-entry to `RECEIVING` after late instances). Fields:

```json
{
  "study_ingest_id": "ing_01JX8F2ZC4WQ",
  "tenant_id": "t_a41f",
  "study_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000041",
  "ingest_path": "pacs_callback",
  "ingest_class": "clinical",
  "source_aet": "CT_SOM_01",
  "state": "TRIAGED",
  "instance_count": 1131,
  "series_count": 7,
  "first_instance_at": "2026-09-13T09:31:02.110Z",
  "last_instance_at": "2026-09-13T09:34:41.905Z",
  "stability": "NATURAL",
  "stable_at": "2026-09-13T09:35:41.905Z",
  "triaged_at": "2026-09-13T09:35:44.612Z",
  "triage_spec_version": 4,
  "selection_hash": "sha256:6e0b1d2c8f4a5b93c0e7d1a44f2b8c61a9d3e0f7b5c284a16d9f3e07c1b42a58"
}
```

**MOS-DATA-046** Tenant assignment rules, evaluated in order, first match wins:

1. Path A: `origin.RemoteAet` looked up in `ae_tenant_map` with `UNIQUE (called_aet, calling_aet)`.
2. Path B1: the tenant of the authenticated STOW principal.
3. Path B2: the `tenant_id` configured on the poll source.
4. Path C: the tenant of the authenticated caller. The tenant MUST NOT be carried in the request body or in a path segment (Chapter 10, MOS-API-003); a platform administrator acting for another tenant does so through the `MedicalOS-Tenant-Id` header of MOS-API-003, which is audited.

**MOS-DATA-047** An instance whose tenant cannot be determined MUST be written to `quarantine` with reason `tenant_unresolved` and MUST NOT be triaged, MUST NOT create a `Job`, and MUST NOT be visible through the Gateway (MOS-DATA-024). Assigning unattributed data to a default tenant is forbidden.

**MOS-DATA-048** If instances for a `StudyInstanceUID` already owned by tenant A arrive over a route that resolves to tenant B, the arriving instances MUST be quarantined with reason `tenant_conflict`. They MUST NOT be merged into A's study and MUST NOT re-assign ownership.

**MOS-DATA-049** Ingest has two classes, declared per source:

| `ingest_class` | Meaning | UID handling at ingest | Use |
|---|---|---|---|
| `clinical` | identified patient data from a care setting | none; UIDs kept, de-identification happens on Gateway egress | production |
| `corpus` | already-public, already-de-identified research data (TCIA, LIDC-IDRI, MIDRC) | UIDs remapped **at ingest** into the tenant's space via MOS-DATA-032, `deid_uid_map` populated | evidence plane, development |

`corpus` ingest is what makes two tenants able to hold the same public collection in a shared backend without a `StudyInstanceUID` collision (MOS-DATA-010), without prejudging whether the deployment uses one backend or one per tenant (MOS-DATA-014).

#### 3.4.2 Path A — PACS stored-instance callback

**MOS-DATA-050** For an Orthanc backend the conformant callback is a Lua script loaded into Orthanc, posting to a Gateway-internal endpoint that is not exposed outside the PACS network:

```lua
function OnStoredInstance(instanceId, tags, metadata, origin)
  local payload = {
    study_instance_uid  = tags['StudyInstanceUID'],
    series_instance_uid = tags['SeriesInstanceUID'],
    sop_instance_uid    = tags['SOPInstanceUID'],
    sop_class_uid       = tags['SOPClassUID'],
    modality            = tags['Modality'],
    calling_aet         = origin['RemoteAet'],
    called_aet          = origin['CalledAet'],
    origin_request      = origin['RequestOrigin'],
    received_at         = os.date('!%Y-%m-%dT%H:%M:%SZ')
  }
  HttpPost('http://medicalos-gateway:8085/internal/v1/ingest/instance-stored',
           DumpJson(payload, true),
           { ['X-MedicalOS-Ingest-Key'] = os.getenv('MEDICALOS_INGEST_KEY'),
             ['Content-Type'] = 'application/json' })
end
```

The endpoint MUST be idempotent on `sop_instance_uid`, MUST return `202` in under 50 ms (it only updates counters and timers), and MUST NOT perform triage inline. A non-`2xx` response MUST NOT block the PACS store: a missed callback is recovered by the reconciliation sweep of MOS-DATA-054.

`/internal/v1/...` is the mesh-internal control surface, not public API. This chapter owns the contents of `/internal/v1/ingest/...`; Chapter 10 owns the boundary (MOS-API-001a), under which the surface MUST be bound to the mesh interface only, MUST authenticate by the component-scoped `X-MedicalOS-Ingest-Key` rather than a tenant credential, MUST NOT be reachable from the edge zone `Z-EDGE` (Chapter 8), and MUST NOT appear in the generated OpenAPI document or in either SDK.

**MOS-DATA-051** Study stability is determined by debounce: a study transitions `RECEIVING → STABLE` when no instance has arrived for `stability_seconds` (default 60, configurable per source, minimum 10). `stability` is then `NATURAL`.

**MOS-DATA-052** A study still in `RECEIVING` after `max_receive_window_seconds` (default 1800) MUST be forced to `STABLE` with `stability: FORCED`. A forced-stable study MUST be triaged and MUST carry `triage_warnings: ["forced_stability"]`, which a `SeriesSelector` MAY treat as disqualifying via `require_natural_stability: true`.

**MOS-DATA-053** An instance arriving for a study in `TRIAGED` MUST move it to `SUPERSEDED → RECEIVING`, and on the next stability transition the study MUST be re-triaged. After re-triage the Selection Engine recomputes `selection_hash` for every deployed `ServiceVersion`:

```
selection_hash = sha256(
    service_id + "\0" + service_version + "\0" +
    "\0".join(sorted(selected_series_instance_uids)) + "\0" +
    str(triage_spec_version))
```

If `selection_hash` is unchanged, **no new `Job` is created**. If it changed, exactly one new `Job` is created and the prior result, if any, is marked superseded by Chapter 4's rules. Without this, every trickling late instance on a slow modality creates a duplicate analysis.

`selection_hash` governs job creation only; the job's `idempotency_key` is derived separately by MOS-EXEC-053. The two are not interchangeable and neither is computed from the other: `selection_hash` answers "has the selected series set for this service version changed since the last triage?", while `idempotency_key` is the platform-wide job identity that Chapter 5 derives for every job regardless of origin and that Chapter 4 feeds into DICOM output identity (MOS-IMG-062). This chapter defines no idempotency key.

#### 3.4.3 Path B — DICOMweb

**MOS-DATA-054** The DICOMweb poll source is configured as:

```yaml
poll_source_id: ps_uk_archive
tenant_id: t_a41f
ingest_class: corpus
qido_root: "https://archive.example.org/dicom-web"
auth: {kind: bearer, secret_ref: "secret://medicalos/poll/ps_uk_archive"}
interval_seconds: 300
watermark_field: StudyDate            # StudyDate | StudyTime composite
overlap_minutes: 15
page_size: 100
includefield: ["00080061", "00201206", "00201208"]   # ModalitiesInStudy, NumberOfStudyRelatedSeries/Instances
```

The poller MUST persist the watermark, MUST re-query an `overlap_minutes` window on every pass to tolerate clock skew and late registration, and MUST deduplicate by `(tenant_id, study_instance_uid, number_of_study_related_instances)`. A study whose instance count has grown since the last pass MUST be re-ingested and re-triaged under MOS-DATA-053. The same poller loop, run against the local backend, is the reconciliation sweep that recovers studies whose Path A callback was lost.

**MOS-DATA-055** Path B1 (remote system STOWs into MedicalOS) uses the public STOW-RS route of MOS-DATA-007 and is subject to MOS-DATA-012. STOW-RS itself has no notion of study completion, so stability is determined by MOS-DATA-051 exactly as for Path A.

#### 3.4.4 Path C — manual API push

**MOS-DATA-056** `POST /api/v1/studies/{study_id}/ingest` (Chapter 10, MOS-API-012) accepts `{ingest_class, force_retriage, invalidate_triage}`. The tenant is determined by the credential and MUST NOT appear in the body or the path (MOS-API-003); the study is identified by the path parameter. The endpoint applies to a study already present in the backend and projected under the caller's tenant, requires `study.ingest`, requires an `Idempotency-Key` header (MOS-API-022), and returns `202` with a `study_ingest_id`. The request and response shapes and the error model are Chapter 10's; the ingest semantics are this chapter's. `force_retriage: true` bumps nothing and reuses the cached triage unless `triage_spec_version` has changed; a genuine re-extraction requires the operator-only `invalidate_triage: true` member on the same endpoint, which is audited.

**MOS-DATA-057** Every ingest path MUST publish exactly one `medicalos.events.system` event per state transition, with `event_type ∈ {study.receiving, study.stable, study.triaged, study.quarantined}`. These events are a derived transport (spine §4): the `study_ingest` row is the source of truth and the events MUST NOT be read to reconstruct ingest state.

### 3.5 Triage

**MOS-DATA-058** Triage MUST read metadata only. It MUST use `GET /dicomweb/{t}/studies/{st}/metadata` (one request, `application/dicom+json`, `BulkDataURI` references instead of pixel data) and MUST NOT issue any WADO-RS instance, frame or rendered request. If the study metadata response exceeds `triage_metadata_max_bytes` (default 64 MiB) the engine MUST fall back to per-series `/series/{se}/metadata` requests. Triage MUST NOT decode pixel data, and MUST NOT trigger burned-in screening (MOS-DATA-042).

**MOS-DATA-059** For each series the Triage Engine MUST extract and cache exactly the following fields. "copied" means the attribute value verbatim; "derived" means computed by the rules of MOS-DATA-060.

| Field | Type | Source | Kind |
|---|---|---|---|
| `series_instance_uid` | UID | (0020,000E) | copied |
| `modality` | enum | (0008,0060) | copied |
| `series_number` | int | (0020,0011) | copied, null if absent |
| `series_description` | string | (0008,103E) | copied, PHI-cleaned |
| `protocol_name` | string | (0018,1030) | copied, PHI-cleaned |
| `sop_class_uids` | UID[] | (0008,0016) distinct | copied |
| `image_type` | string[] | (0008,0008) | copied, upper-cased, first instance |
| `image_type_consistent` | bool | (0008,0008) | derived: identical across all instances |
| `body_part_examined` | string | (0018,0015) | copied, normalised (MOS-DATA-060.1) |
| `manufacturer` | string | (0008,0070) | copied |
| `manufacturer_model_name` | string | (0008,1090) | copied |
| `convolution_kernel` | string | (0018,1210) | copied, multi-valued joined with `\` |
| `kernel_class` | enum | — | derived (MOS-DATA-060.2) |
| `instance_count` | int | — | derived: count of instances |
| `rows`, `columns` | int | (0028,0010),(0028,0011) | copied, mode |
| `pixel_spacing_mm` | float[2] | (0028,0030) | copied, mode across instances |
| `slice_thickness_mm` | float | (0018,0050) | derived: median |
| `spacing_between_slices_mm` | float | — | derived (MOS-DATA-060.3) |
| `slice_spacing_uniformity_mm` | float | — | derived (MOS-DATA-060.3) |
| `missing_slice_gaps` | int | — | derived (MOS-DATA-060.3) |
| `duplicate_position_count` | int | — | derived (MOS-DATA-060.3) |
| `z_extent_mm` | float | — | derived (MOS-DATA-060.3) |
| `image_orientation_patient` | float[6] | (0020,0037) | copied, mode |
| `orientation_class` | enum | — | derived (MOS-DATA-060.4) |
| `axial_deviation_deg` | float | — | derived (MOS-DATA-060.4) |
| `frame_of_reference_uid` | UID | (0020,0052) | copied, null if absent |
| `frame_of_reference_consistent` | bool | — | derived: single distinct value |
| `gantry_tilt_deg` | float | (0018,1120) | copied as absolute value, 0.0 if absent |
| `contrast_agent` | string | (0018,0010) | copied, null if absent |
| `contrast_present` | bool | — | derived: attribute present and non-empty |
| `contrast_phase` | enum | — | derived (MOS-DATA-060.5) |
| `kvp` | float | (0018,0060) | copied, mode |
| `exposure_mas` | float | (0018,1152) | copied, median |
| `ctdi_vol_mgy` | float | (0018,9345) | copied, median, null if absent |
| `photometric_interpretation` | enum | (0028,0004) | copied |
| `bits_stored` | int | (0028,0101) | copied |
| `rescale_slope`, `rescale_intercept` | float | (0028,1053),(0028,1052) | copied |
| `rescale_present` | bool | — | derived |
| `transfer_syntax_uids` | UID[] | file meta | copied, distinct |
| `is_multiframe` | bool | (0028,0008) NumberOfFrames > 1 | derived |
| `has_pixel_data` | bool | — | derived: SOP Class is an image storage class |
| `is_localizer` | bool | — | derived: `LOCALIZER` in `image_type` |
| `is_derived` | bool | — | derived: `image_type[0] == DERIVED` |
| `is_secondary` | bool | — | derived: `image_type[1] == SECONDARY` |
| `is_reformatted` | bool | — | derived: `REFORMATTED` in `image_type` |
| `is_projection` | bool | — | derived: any of `MIP`, `MINIP`, `AVERAGE` in `image_type` |
| `burned_in_annotation` | enum | (0028,0301) | copied: `YES`/`NO`/`UNKNOWN` |
| `burned_in_state` | enum | — | one of `UNSCREENED`, `CLEAN`, `SUSPECTED`, `REDACTED` (MOS-DATA-042); `UNSCREENED` at triage, set by MOS-DATA-038 on demand |
| `patient_age_years` | int | (0010,1010) | derived: parsed from the `nnnY` form, null otherwise |
| `triage_warnings` | enum[] | — | derived (MOS-DATA-060.6) |

**MOS-DATA-060** Derivation rules, all normative:

1. **`body_part_examined`** — upper-cased, whitespace-stripped, mapped through the versioned `body_parts.yaml` synonym table (`THORAX → CHEST`, `LUNG → CHEST`, `TORAX → CHEST`). An unmapped non-empty value is kept verbatim; an empty value yields `null`, which is distinct from a mismatch (see `body_part_allow_missing` in 3.6).
2. **`kernel_class`** — resolved from the versioned data file `kernel_classes.yaml`, keyed on `(manufacturer, convolution_kernel)`:

   ```yaml
   version: 4
   rules:
     - {manufacturer_regex: "^SIEMENS", kernel_regex: "^B[rfv]?(\\d{2})[fsd]?$",
        numeric_group: 1, buckets: {"<40": SOFT, "40-49": STANDARD, ">=50": SHARP}}
     - {manufacturer_regex: "^SIEMENS", kernel_in: ["Bl57","Bl64","Bl69","Br69"], class: SHARP}
     - {manufacturer_regex: "^GE",      kernel_in: ["SOFT"],                      class: SOFT}
     - {manufacturer_regex: "^GE",      kernel_in: ["STANDARD","STND"],           class: STANDARD}
     - {manufacturer_regex: "^GE",      kernel_in: ["LUNG","BONE","BONEPLUS","EDGE","DETAIL"], class: SHARP}
     - {manufacturer_regex: "^Philips", kernel_in: ["A","B","C"],                 class: SOFT}
     - {manufacturer_regex: "^Philips", kernel_in: ["D","EB","FC"],               class: STANDARD}
     - {manufacturer_regex: "^Philips", kernel_in: ["L","YA","YB","YC","YD"],     class: SHARP}
     - {manufacturer_regex: "^CANON|^TOSHIBA", kernel_in: ["FC01","FC02","FC03","FC07","FC08"], class: SOFT}
     - {manufacturer_regex: "^CANON|^TOSHIBA", kernel_in: ["FC30","FC50","FC51","FC52","FC55"], class: SHARP}
   default: UNKNOWN
   ```

   `UNKNOWN` MUST NOT satisfy a `kernel_class_in` constraint unless the requirement sets `allow_unknown_kernel: true`. The file is part of `triage_spec_version`; editing it invalidates cached triage.
3. **Geometry** — slices are ordered by the projection of `(0020,0032) ImagePositionPatient` on the slice normal `n = r × c` where `r, c` are the first and second triplets of `(0020,0037)`. Let `d_i` be consecutive projection deltas. Then `spacing_between_slices_mm = median(d_i)`; `slice_spacing_uniformity_mm = max|d_i − median(d_i)|`; `missing_slice_gaps = |{i : d_i > 1.5 × median(d_i)}|`; `duplicate_position_count = |{i : d_i < 1e-4}|`; `z_extent_mm = max(proj) − min(proj) + slice_thickness_mm`. A series with fewer than 3 instances yields `spacing_between_slices_mm = null` and `slice_spacing_uniformity_mm = null`.
4. **Orientation** — `axial_deviation_deg = degrees(arccos(|n · (0,0,1)|))` in LPS. `orientation_class` is `AXIAL` if `axial_deviation_deg ≤ 20`, `CORONAL` if the angle to `(0,1,0)` is `≤ 20`, `SAGITTAL` if the angle to `(1,0,0)` is `≤ 20`, otherwise `OBLIQUE`. `UNKNOWN` when `(0020,0037)` is absent.
5. **`contrast_phase`** — `NONE` if `contrast_present` is false; otherwise `UNKNOWN` unless the tenant-configured phase regex set matches `series_description` or `protocol_name` (`ARTERIAL`, `PORTAL_VENOUS`, `DELAYED`, `PULMONARY_ARTERIAL`). The specification deliberately refuses to infer phase from `(0018,1042) ContrastBolusStartTime` versus `(0008,0032) AcquisitionTime`: the arithmetic is scanner- and protocol-dependent and a wrong phase silently routes a study to a model validated on a different one. `UNKNOWN` is a first-class value and a selector that requires a phase MUST reject it.
6. **`triage_warnings`** — any of `forced_stability`, `non_uniform_spacing`, `missing_slices`, `duplicate_positions`, `mixed_image_type`, `mixed_frame_of_reference`, `gantry_tilt_present`, `compressed_transfer_syntax`, `no_rescale`, `single_instance`.

**MOS-DATA-061** Triage MUST also record study-level fields: `study_instance_uid`, `accession_number` (surrogate under the consumer class in force), `study_date`, `study_description` (PHI-cleaned), `modalities_in_study`, `series_count`, `instance_count`, `patient_age_years`, `patient_sex`.

**MOS-DATA-062** `triage_spec_version` is an integer covering the extraction code, `kernel_classes.yaml`, `body_parts.yaml` and the phase regex set. It MUST be stamped on every row of the triage projection — Chapter 12's `series.triage_spec_version`; there is no separate `series_triage` table — and included in `selection_hash`.

**MOS-DATA-063** The triage cache key is `(tenant_id, study_instance_uid, series_instance_uid, triage_spec_version)`. A cached row MUST be invalidated only when: the series instance count changes; `triage_spec_version` increases; or an operator issues `invalidate_triage`. It MUST NOT have a time-based TTL — the inputs are immutable, so an expiring cache only adds load and non-determinism.

**MOS-DATA-064** The complete triage record for a study, and the complete selection report for a job, MUST be retrievable through the API (Chapter 10) as first-class data — not as a log line and not as a free-text `message`.

**MOS-DATA-065** `series_description`, `protocol_name` and `study_description` are the only free-text attributes retained in the triage cache, and only because selection reads them. They MUST be PHI-cleaned on the same path as `clean_descriptors`, MUST NOT be copied into metric labels, span attributes or event payloads, and MUST NOT be included in an LLM prompt unless `external_llm_allowed` is true for the tenant (spine §8).

### 3.6 SeriesSelector

#### 3.6.1 Location and shape

**MOS-DATA-066** A `ServiceVersion` manifest (`service.yaml`, Chapter 2) carries exactly one `applicability` block and exactly one `series_selector` block. `series_selector` contains an ordered list of named `inputs`, each a `SeriesRequirement`. The machine schema is `medos/schemas/series_selector.schema.json`, JSON Schema 2020-12, and is the single source from which the Go structs, the Python SDK models and the OpenAPI component are generated (spine §12).

```yaml
applicability:
  modality_in: [CT]
  body_part_in: [CHEST]
  body_part_allow_missing: true
  study_description_include_regex: "(?i)(chest|thorax|lung|hrct|pulmo)"
  study_description_exclude_regex: "(?i)(cardiac\\s+calcium|coronar)"
  patient_age_years: {min: 18, max: 120}
  patient_age_allow_missing: false

series_selector:
  selector_version: 2
  inputs:
    - name: primary_axial
      cardinality: exactly_one
      match:
        modality: [CT]
        sop_class_uid_in:
          - "1.2.840.10008.5.1.4.1.1.2"        # CT Image Storage
          - "1.2.840.10008.5.1.4.1.1.2.1"      # Enhanced CT Image Storage
        image_type_require_all: [ORIGINAL, PRIMARY]
        image_type_exclude_any: [LOCALIZER, DERIVED, SECONDARY, REFORMATTED, MIP, MINIP, AVERAGE]
        series_description_exclude_regex: "(?i)(scout|topogram|surview|dose|screen ?save|report|mip|minip|reformat|cor\\b|sag\\b)"
        body_part_examined_in: [CHEST]
        body_part_allow_missing: true
        slice_thickness_mm: {min: 0.5, max: 1.5}
        pixel_spacing_mm: {max: 1.0}
        slice_spacing_uniformity_mm: {max: 0.05}
        missing_slice_gaps_max: 0
        duplicate_positions: forbid
        instance_count: {min: 80}
        z_extent_mm: {min: 150}
        kernel_class_in: [SOFT, STANDARD]
        allow_unknown_kernel: false
        contrast: any
        contrast_phase_in: []
        orientation_class_in: [AXIAL]
        axial_deviation_deg_max: 15
        gantry_tilt_deg_max: 0.5
        frame_of_reference_required: true
        frame_of_reference_consistent: true
        photometric_interpretation_in: [MONOCHROME2]
        rescale_required: true
        multiframe: forbid
        transfer_syntax_exclude: ["1.2.840.10008.1.2.4.91"]   # JPEG 2000 lossy
        burned_in_annotation: forbid
        kvp: {min: 70, max: 150}
        require_natural_stability: true
        manufacturer_exclude_regex: ""
      rank:
        - {key: slice_thickness_mm, order: asc, tolerance: 0.05}
        - {key: kernel_class, order: preference, preference: [STANDARD, SOFT]}
        - {key: instance_count, order: desc, tolerance: 5}
        - {key: series_number, order: asc}
        - {key: series_instance_uid, order: asc}
      ambiguity:
        policy: select_best
        fan_out_max: 2
```

#### 3.6.2 The constraint vocabulary

**MOS-DATA-067** The `applicability` block is a **study-level gate** describing the clinical envelope the `ServiceVersion` publisher claims (spine §11 `input_constraints`). Its fields:

| Field | Type | Default | Semantics |
|---|---|---|---|
| `modality_in` | enum[] | required | intersection with `modalities_in_study` must be non-empty |
| `body_part_in` | string[] | `[]` | matched against any series' normalised `body_part_examined`; `[]` means unconstrained |
| `body_part_allow_missing` | bool | `true` | a null body part satisfies `body_part_in` |
| `study_description_include_regex` | regex | `""` | RE2 syntax; `""` means unconstrained |
| `study_description_exclude_regex` | regex | `""` | a match disqualifies |
| `patient_age_years` | `{min,max}` | unconstrained | |
| `patient_age_allow_missing` | bool | `true` | |

**MOS-DATA-068** A `SeriesRequirement` has `name` (unique within the selector, `^[a-z][a-z0-9_]{0,31}$`), `cardinality ∈ {exactly_one, one_or_more, optional_one}`, `match`, `rank` and `ambiguity`. `one_or_more` additionally takes `max_count` (default 8). Multiple inputs let a service declare that it needs, for example, both a thin and a thick recon, or a current and a prior study, without any core code change.

**MOS-DATA-069** The `match` vocabulary is exactly the following, and a selector containing an unknown key MUST be rejected at registration. Every field is optional; an omitted field is unconstrained.

| Field | Type | Rejection reason code on failure |
|---|---|---|
| `modality` | enum[] | `modality_mismatch` |
| `sop_class_uid_in` | UID[] | `sop_class_not_supported` |
| `image_type_require_all` | string[] | `image_type_excluded` |
| `image_type_exclude_any` | string[] | `image_type_excluded`, or the specific `localizer` / `derived_series` / `reformatted_series` / `projection_series` when the excluded token identifies one of those |
| `series_description_include_regex` | regex | `series_description_excluded` |
| `series_description_exclude_regex` | regex | `series_description_excluded` |
| `body_part_examined_in` | string[] | `body_part_mismatch` |
| `body_part_allow_missing` | bool | `body_part_mismatch` |
| `slice_thickness_mm` | `{min,max}` | `slice_thickness_out_of_range` |
| `pixel_spacing_mm` | `{min,max}` applied to `max(x,y)` | `pixel_spacing_out_of_range` |
| `slice_spacing_uniformity_mm` | `{max}` | `non_uniform_spacing` |
| `missing_slice_gaps_max` | int | `missing_slices` |
| `duplicate_positions` | `forbid`\|`allow` | `duplicate_slice_positions` |
| `instance_count` | `{min,max}` | `instance_count_below_minimum` / `instance_count_above_maximum` |
| `z_extent_mm` | `{min,max}` | `z_extent_below_minimum` |
| `kernel_class_in` | enum[] | `kernel_class_mismatch` |
| `allow_unknown_kernel` | bool | `kernel_unknown` |
| `contrast` | `required`\|`forbidden`\|`any` | `contrast_mismatch` |
| `contrast_phase_in` | enum[] | `contrast_phase_mismatch` |
| `orientation_class_in` | enum[] | `orientation_not_axial` (or `orientation_mismatch` for non-axial targets) |
| `axial_deviation_deg_max` | float | `axial_deviation_exceeded` |
| `gantry_tilt_deg_max` | float | `gantry_tilt_exceeded` |
| `frame_of_reference_required` | bool | `frame_of_reference_missing` |
| `frame_of_reference_consistent` | bool | `frame_of_reference_inconsistent` |
| `photometric_interpretation_in` | enum[] | `photometric_interpretation_unsupported` |
| `rescale_required` | bool | `rescale_missing` |
| `multiframe` | `forbid`\|`allow` | `multiframe_unsupported` |
| `transfer_syntax_exclude` | UID[] | `compressed_transfer_syntax_unsupported` |
| `burned_in_annotation` | `forbid`\|`allow` | `burned_in_phi` |
| `kvp` | `{min,max}` | `kvp_out_of_range` |
| `require_natural_stability` | bool | `study_not_stable` |
| `manufacturer_exclude_regex` | regex | `manufacturer_excluded` |
| `has_pixel_data` | bool (default `true`) | `no_pixel_data` |

Three further reason codes are produced outside the `match` vocabulary: `deid_failed`, `metadata_unreadable`, `series_quarantined`.

Every code above is spelled in lowercase `snake_case` and is carried verbatim by every transport: a problem document's `code` is SCREAMING_SNAKE_CASE (Chapter 10, MOS-API-037) but a series-rejection `reason` is not a problem `code` and MUST NOT be re-cased, abbreviated or re-spelled for transport. The vocabulary is closed; adding a code is a minor version of this specification.

These are **selection-time, per-series** codes: they say why a series was not selected. They and the job-level list of MOS-DATA-080 are the only rejection vocabularies this chapter defines. A series that *is* selected but whose canonical volume cannot then be built is rejected by Chapter 4 with exactly one code from the closed geometry enum of MOS-IMG-010 (`geometry_*`). The two vocabularies are disjoint and neither substitutes for the other: when a selector constrains a geometric property the failure is caught here, under this chapter's code (`non_uniform_spacing`, `missing_slices`, `duplicate_slice_positions`, `gantry_tilt_exceeded`, …); when the selector omits that constraint the same study reaches the volume builder and fails there, under Chapter 4's code (`geometry_non_uniform_spacing`, `geometry_gapped`, `geometry_duplicate_positions`, `geometry_gantry_tilt`, …). Both surface on the `Job` under `rejection` (Chapter 5, MOS-EXEC-001): this chapter's per-series codes in `rejection.evaluated_series[].reason_code`, Chapter 4's build failure in `rejection.reason_code`.

#### 3.6.3 Evaluation, ranking and tie-break

**MOS-DATA-070** For each `SeriesRequirement`, constraints MUST be evaluated in the exact row order of the MOS-DATA-069 table, and evaluation for a given series MUST stop at the first failure. The reported reason is therefore deterministic and is the *first* thing wrong with the series, which is what a human reading the report wants. Series MUST be iterated in `(series_number ASC NULLS LAST, series_instance_uid ASC)` order so the rejection report itself has a stable order.

**MOS-DATA-071** Eligible candidates MUST be ordered by the `rank` list using this algorithm and no other:

```python
def rank_tuple(s: SeriesTriage, rank: list[RankKey]) -> tuple:
    out = []
    for k in rank:
        v = getattr(s, k.key)
        if k.order == "asc":
            out.append(_bucket(v, k.tolerance))
        elif k.order == "desc":
            out.append(-_bucket(v, k.tolerance))
        elif k.order == "preference":
            out.append(k.preference.index(v) if v in k.preference else len(k.preference))
        else:
            raise SelectorError(f"unknown rank order {k.order!r}")
    return tuple(out)

def _bucket(v, tolerance):
    if isinstance(v, str):
        return v                          # byte-wise comparison, never numeric
    if tolerance in (None, 0):
        return v
    return round(v / tolerance)           # deterministic integer bucket

selected = sorted(eligible, key=lambda s: rank_tuple(s, req.rank))[0]
```

`tolerance` is deterministic bucketing, not a distance test: two values closer than `tolerance` may still land in adjacent buckets at a boundary, but values more than one bucket apart never compare equal. Determinism is the property that matters, and bucketing has it where a pairwise distance test does not.

**MOS-DATA-072** The final entry of every `rank` list MUST be `{key: series_instance_uid, order: asc}`. UIDs are unique within a study, so this guarantees a total order and therefore a single deterministic winner in every case. String comparison MUST be byte-wise ASCII, never numeric component-wise.

**MOS-DATA-073** At `ServiceVersion` registration (Chapter 6) the platform MUST reject a manifest whose `rank` list does not end with `series_instance_uid asc`, whose `match` contains an unknown key, whose regexes are not valid RE2, whose `rank` references a field not in the MOS-DATA-059 table, or whose `cardinality` is `exactly_one` with `max_count` set. The error MUST name the offending path, e.g. `series_selector.inputs[0].rank: last key must be series_instance_uid asc`.

**MOS-DATA-074** Two candidates are **tied** when their `rank_tuple`s are equal in every position except the final `series_instance_uid` component. The `ambiguity.policy` then applies:

| Policy | Behaviour |
|---|---|
| `select_best` (default) | take the deterministic winner; set `ambiguous: true` and list `ambiguity_peers[]` on the Job; the ambiguity flag MUST be surfaced in the provenance panel and in the generated SR |
| `reject` | terminate the Job in `REJECTED` with reason `ambiguous_series_selection` |
| `fan_out` | create one Job per tied candidate, up to `fan_out_max` (default 2); exceeding it terminates in `REJECTED` with reason `ambiguity_fan_out_exceeded` |

`select_best` is the default because the tie-break is deterministic and recorded, and because rejecting a study over two equivalent thin recons would be user-hostile. What is not acceptable is selecting silently: the flag is mandatory.

#### 3.6.4 Outcomes

**MOS-DATA-075** If `applicability` passes and any `SeriesRequirement` with cardinality `exactly_one` or `one_or_more` yields zero eligible candidates, the platform MUST create a `Job` and immediately terminate it in `REJECTED` (spine §4) with reason `no_eligible_series`, **even under ambient auto-routing**. The service MUST NOT be dispatched to. The rationale: "this study was inside the service's clinical envelope and was not analysed" is clinically actionable information that a radiologist must be able to see; silently producing nothing is the failure mode the review named.

**MOS-DATA-076** If `applicability` fails under ambient auto-routing, the platform MUST NOT create a `Job`. It MUST write a `triage_decision` row with `outcome: NOT_APPLICABLE`. Without this distinction, every chest CT would accumulate a `REJECTED` job from every neuro, cardiac and MSK service deployed in the tenant, and `REJECTED` would become noise.

**MOS-DATA-077** If a job is **explicitly requested** for a named `ServiceVersion` (`POST /api/v1/jobs`, Chapter 10), a `Job` MUST always be created, and an `applicability` failure terminates it in `REJECTED` with reason `outside_applicability_envelope`. An explicit request always produces an answerable job id.

**MOS-DATA-078** The `TriageDecision` record:

```json
{
  "triage_decision_id": "td_01JX8F4A1P2K",
  "tenant_id": "t_a41f",
  "study_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000041",
  "service_id": "pulmoai.pleural-effusion",
  "service_version": "2.1.0",
  "selector_version": 2,
  "triage_spec_version": 4,
  "outcome": "SELECTED",
  "job_id": "job_01JX8F3QK2M7",
  "selection_hash": "sha256:6e0b1d2c8f4a5b93c0e7d1a44f2b8c61a9d3e0f7b5c284a16d9f3e07c1b42a58",
  "decided_at": "2026-09-13T09:35:44.612Z"
}
```

`outcome ∈ {SELECTED, NOT_APPLICABLE, REJECTED}`. `job_id` is null for `NOT_APPLICABLE`.

**MOS-DATA-079** Per-series rejections are first-class typed data, never prose. Each record:

```json
{
  "series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000301",
  "series_number": 4,
  "series_description": "Lung 1.0 Bl57",
  "requirement_name": "primary_axial",
  "reason_code": "kernel_class_mismatch",
  "observed": {"convolution_kernel": "Bl57", "kernel_class": "SHARP"},
  "expected": {"kernel_class_in": ["SOFT", "STANDARD"], "allow_unknown_kernel": false}
}
```

`observed` and `expected` MUST contain real values from the triage record and the selector, not a rendered sentence. The UI renders the sentence; the API returns the data.

**MOS-DATA-080** Job-level `REJECTED` reason codes are exactly: `no_eligible_series`, `outside_applicability_envelope`, `ambiguous_series_selection`, `ambiguity_fan_out_exceeded`, `required_prior_missing`, `study_quarantined`, `deid_policy_blocked`. Each MUST be returned in the RFC 9457 problem body with `class: "clinical_rejection"` (spine §12) and MUST be rendered visually distinct from `FAILED` in every UI (spine §4).

**MOS-DATA-081** Selection MUST be a pure function of `(triage records, selector, triage_spec_version)`. It MUST NOT read the clock, a random source, the database ordering of the input rows, or any per-tenant state other than the selector. Running it twice on the same inputs MUST produce byte-identical selection and byte-identical rejection reports including their order.

**MOS-DATA-082** The selected series list MUST be pinned into the `Job` row at creation as an ordered `selected_series` array of `{requirement_name, series_instance_uid, instance_count}`, together with `selection_hash`, `triage_spec_version` and `selector_version`, and MUST be reused verbatim on every retry. A retry MUST NOT re-run selection.

**MOS-DATA-083** The triage and selection fixture corpus MUST exist under `tests/fixtures/dicom/triage/` with at least these named cases, each a real or synthesised DICOM study with a committed golden triage JSON and a committed golden selection report. This corpus is also the normative definition of "invalid DICOM" for the failure tests of Chapter 14.

| Fixture | Content |
|---|---|
| `chest_ct_multirecon` | the 3.7 worked example: 8 series, two kernels × two thicknesses, reformat, localizer, dose SR |
| `localizer_only` | a study containing only a 2-instance topogram |
| `dose_report_only` | a single X-Ray Radiation Dose SR, `1.2.840.10008.5.1.4.1.1.88.67`, no pixel data |
| `coronal_reformat_only` | `DERIVED\SECONDARY\REFORMATTED`, coronal |
| `gapped_series` | uniform spacing with three missing slices |
| `nonuniform_spacing` | variable table increment, `slice_spacing_uniformity_mm = 0.9` |
| `duplicate_positions` | two instances sharing `ImagePositionPatient` |
| `gantry_tilt` | `(0018,1120) = 22.5` |
| `single_instance` | a one-image CT series |
| `missing_frame_of_reference` | `(0020,0052)` absent |
| `no_rescale` | `(0028,1052)`/`(0028,1053)` absent |
| `burned_in_annotation` | secondary capture with `BurnedInAnnotation = YES` and visible text |
| `mixed_image_type` | a series whose `(0008,0008)` differs between instances |
| `brain_mri` | out-of-applicability control study |

### 3.7 Worked example: selecting the right recon from a chest CT

The study, `1.3.12.2.1107.5.1.4.73473.30000024031407221562300000041`, `StudyDescription = "CT THORAX NATIV"`, Siemens SOMATOM, 8 series, 1131 instances — an ordinary output of an ordinary chest protocol:

| # | SeriesNumber | SeriesDescription | ImageType | Kernel | Thick (mm) | Instances | Orientation |
|---|---|---|---|---|---|---|---|
| S1 | 1 | Topogram 0.6 T20f | `ORIGINAL\PRIMARY\LOCALIZER` | T20f | 0.6 | 2 | — |
| S2 | 2 | Thorax 1.0 Br40 | `ORIGINAL\PRIMARY\AXIAL` | Br40 | 1.0 | 412 | AXIAL |
| S3 | 3 | Thorax 5.0 Br40 | `ORIGINAL\PRIMARY\AXIAL` | Br40 | 5.0 | 83 | AXIAL |
| S4 | 4 | Lung 1.0 Bl57 | `ORIGINAL\PRIMARY\AXIAL` | Bl57 | 1.0 | 412 | AXIAL |
| S5 | 5 | Lung 5.0 Bl57 | `ORIGINAL\PRIMARY\AXIAL` | Bl57 | 5.0 | 83 | AXIAL |
| S6 | 6 | Cor 3.0 Br40 | `DERIVED\SECONDARY\REFORMATTED` | Br40 | 3.0 | 140 | CORONAL |
| S7 | 7 | Thorax 1.0 Br40 (re-recon) | `ORIGINAL\PRIMARY\AXIAL` | Br40 | 1.0 | 412 | AXIAL |
| S8 | 501 | Dose Report | `DERIVED\SECONDARY` | — | — | 1 | — (`1.2.840.10008.5.1.4.1.1.88.67`) |

Applicability for `pulmoai.pleural-effusion:2.1.0` passes: `CT` in `modalities_in_study`, `body_part_examined = CHEST` on S2–S5, `"CT THORAX NATIV"` matches the include regex, patient age 64.

Evaluating `primary_axial` from 3.6.1, in `series_number` order, stopping at each series' first failing constraint:

| Series | Outcome | `reason_code` | `observed` → `expected` |
|---|---|---|---|
| S1 | rejected | `localizer` | `image_type: [ORIGINAL,PRIMARY,LOCALIZER]` → excludes `LOCALIZER` |
| S2 | **eligible** | — | — |
| S3 | rejected | `slice_thickness_out_of_range` | `5.0` → `{min: 0.5, max: 1.5}` |
| S4 | rejected | `kernel_class_mismatch` | `Bl57` / `SHARP` → `[SOFT, STANDARD]` |
| S5 | rejected | `slice_thickness_out_of_range` | `5.0` → `{min: 0.5, max: 1.5}` |
| S6 | rejected | `reformatted_series` | `image_type: [DERIVED,SECONDARY,REFORMATTED]` → excludes `DERIVED` |
| S7 | **eligible** | — | — |
| S8 | rejected | `no_pixel_data` | `sop_class_uids: ["1.2.840.10008.5.1.4.1.1.88.67"]` → `has_pixel_data: true` |

Note S3 and S5 report thickness, not kernel: S3's kernel is acceptable, so thickness is its first failure; S5 fails thickness before the constraint order reaches kernel. This is deterministic under MOS-DATA-070 and is exactly the behaviour a reader wants — one primary reason per series, always the same one.

Two candidates remain, S2 and S7, an entirely ordinary situation when a technologist re-reconstructs. Ranking:

| Key | S2 | S7 | Result |
|---|---|---|---|
| `slice_thickness_mm asc`, tol 0.05 | `round(1.0/0.05) = 20` | `20` | tie |
| `kernel_class preference [STANDARD, SOFT]` | `STANDARD → 0` | `STANDARD → 0` | tie |
| `instance_count desc`, tol 5 | `-round(412/5) = -82` | `-82` | tie |
| `series_number asc` | `2` | `7` | **S2 wins** |
| `series_instance_uid asc` | not reached | | |

Outcome: `Job` created against S2, with `ambiguous: true` and `ambiguity_peers: ["<S7 UID>"]` under the default `select_best`. The `triage` block returned on the job:

```json
{
  "triage_decision_id": "td_01JX8F4A1P2K",
  "triage_spec_version": 4,
  "selector_version": 2,
  "outcome": "SELECTED",
  "selected_series": [
    {"requirement_name": "primary_axial",
     "series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000188",
     "series_number": 2,
     "instance_count": 412,
     "slice_thickness_mm": 1.0,
     "kernel_class": "STANDARD"}
  ],
  "ambiguous": true,
  "ambiguity_peers": ["1.3.12.2.1107.5.1.4.73473.30000024031407221562300000544"],
  "selection_hash": "sha256:6e0b1d2c8f4a5b93c0e7d1a44f2b8c61a9d3e0f7b5c284a16d9f3e07c1b42a58",
  "rejected_series": [
    {"series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000102",
     "series_number": 1, "series_description": "Topogram 0.6 T20f",
     "requirement_name": "primary_axial", "reason_code": "localizer",
     "observed": {"image_type": ["ORIGINAL", "PRIMARY", "LOCALIZER"]},
     "expected": {"image_type_exclude_any": ["LOCALIZER", "DERIVED", "SECONDARY",
                                             "REFORMATTED", "MIP", "MINIP", "AVERAGE"]}},
    {"series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000244",
     "series_number": 3, "series_description": "Thorax 5.0 Br40",
     "requirement_name": "primary_axial", "reason_code": "slice_thickness_out_of_range",
     "observed": {"slice_thickness_mm": 5.0},
     "expected": {"slice_thickness_mm": {"min": 0.5, "max": 1.5}}},
    {"series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000301",
     "series_number": 4, "series_description": "Lung 1.0 Bl57",
     "requirement_name": "primary_axial", "reason_code": "kernel_class_mismatch",
     "observed": {"convolution_kernel": "Bl57", "kernel_class": "SHARP"},
     "expected": {"kernel_class_in": ["SOFT", "STANDARD"], "allow_unknown_kernel": false}},
    {"series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000367",
     "series_number": 5, "series_description": "Lung 5.0 Bl57",
     "requirement_name": "primary_axial", "reason_code": "slice_thickness_out_of_range",
     "observed": {"slice_thickness_mm": 5.0},
     "expected": {"slice_thickness_mm": {"min": 0.5, "max": 1.5}}},
    {"series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000428",
     "series_number": 6, "series_description": "Cor 3.0 Br40",
     "requirement_name": "primary_axial", "reason_code": "reformatted_series",
     "observed": {"image_type": ["DERIVED", "SECONDARY", "REFORMATTED"],
                  "orientation_class": "CORONAL"},
     "expected": {"image_type_exclude_any": ["LOCALIZER", "DERIVED", "SECONDARY",
                                             "REFORMATTED", "MIP", "MINIP", "AVERAGE"]}},
    {"series_instance_uid": "1.3.12.2.1107.5.1.4.73473.30000024031407221562300000601",
     "series_number": 501, "series_description": "Dose Report",
     "requirement_name": "primary_axial", "reason_code": "no_pixel_data",
     "observed": {"sop_class_uids": ["1.2.840.10008.5.1.4.1.1.88.67"]},
     "expected": {"has_pixel_data": true}}
  ]
}
```

**The same study, a different service.** A lung-nodule service whose evidence was produced on sharp-kernel thin recons declares `kernel_class_in: [SHARP]` and selects **S4**, rejecting S2 and S7 with `kernel_class_mismatch`. The `emphysema_laa` measurement service (spine §14) goes further and pins the kernel by name — `convolution_kernel_in: ["Br40", "B30f", "B31f", "STANDARD"]` — because %LAA below −950 HU is strongly kernel-dependent and a sharp kernel inflates it by tens of percent; for that service, selecting S4 would be a wrong answer that no downstream check would catch. Three services, one study, three different correct series. This is why selection is declarative data on the `ServiceVersion` and not a heuristic in a worker: adding the nodule service changes no platform code.

**The zero-match case.** The same selector run against fixture `localizer_only`: applicability passes (modality CT, chest), `primary_axial` yields zero candidates, so under MOS-DATA-075 a `Job` is created and immediately terminated in `REJECTED` with `reason_code: "no_eligible_series"`, carrying a `rejected_series` array with one entry (`localizer`). Nothing is dispatched. The radiologist sees a distinctly-rendered "not analysed: no eligible thin axial recon", not a red failure and not silence.

### 3.8 FHIR and HL7: explicitly out of scope

**MOS-DATA-084** FHIR is **out of scope for releases 0.1.0 through 0.4.0**, as fixed by the non-goal MOS-CORE-040 (Chapter 1, which owns non-goals) and restated by MOS-SVC-120 (Chapter 2). The platform MUST NOT ship a FHIR client, a FHIR server, a FHIR facade, a FHIR profile, an IG, or any `fhir.*` tool in those releases. The `fhir.patient.read` and `fhir.observation.read` tools of the previous version are withdrawn, and the mandatory `FHIR.md` document is removed from the documentation set.

The reason is stated plainly so nobody re-adds it: nothing in the product consumes FHIR. Results reach clinicians as DICOM SEG/SR/SC in the viewer the hospital already uses (spine §7). A FHIR client written now would have no server to talk to, no resource to read that the DICOM path does not already supply, and no consumer for what it wrote — and building it would produce a subsystem that must be maintained, secured, tenancy-checked and PHI-audited for zero delivered value.

**MOS-DATA-085** HL7 v2 ADT/ORM ingest and DICOM Modality Worklist are likewise out of scope for 0.1.0–0.3.0. Tenant attribution is by calling AE title or DICOMweb credential (MOS-DATA-046), never by an HL7 feed. Order-driven routing is deferred to 0.4.0 at the earliest.

**MOS-DATA-086** The eventual FHIR mapping is fixed here as a design constraint, so that deferring it forecloses nothing:

| MedicalOS concept | FHIR R4 resource | Binding element |
|---|---|---|
| `Patient` | `Patient` | `identifier` (tenant-scoped system + `PatientID`) |
| `Study` | `ImagingStudy` | `identifier = urn:oid:{StudyInstanceUID}` |
| `Series` | `ImagingStudy.series` | `series.uid = {SeriesInstanceUID}` |
| `Instance` | `ImagingStudy.series.instance` | `instance.uid`, `instance.sopClass` |
| `Job` | `Task` | `Task.focus → ImagingStudy`, `Task.status` from the spine §4 enum |
| finding in a `Result` | `Observation` | `Observation.code` from the coded-concept dictionary |
| measurement in a `Result` | `Observation.valueQuantity` | `system = "http://unitsofmeasure.org"`, UCUM code |
| DICOM SR document | `DiagnosticReport` | `imagingStudy`, `presentedForm` |
| `ResultReview` | `Observation.performer` + `Provenance` | reviewer identity and action |
| `ValidationReport` | `DocumentReference` | content-addressed attachment |
| `AuditEvent` | `AuditEvent` | direct |

**MOS-DATA-087** For that mapping to remain a **projection** rather than a rebuild, two things MUST hold from 0.1.0, and this chapter records the dependency: study, series and instance identifiers MUST be retrievable in OID form (`urn:oid:`), which they are by construction; and every coded concept the platform emits MUST carry `(scheme, value, meaning)` with a SNOMED-CT or LOINC code populated wherever one exists, in the dictionary owned by Chapter 4 (`MOS-IMG`). No FHIR artifact MAY be built in releases 0.1.0 through 0.4.0 (MOS-CORE-040), and no schema decision MAY be justified by a FHIR requirement in those releases.

### Acceptance criteria

Each check is executable by CI or by a reviewer with a shell. "The deployment" means a `docker compose` or Helm rendering of the system under test.

1. **Credential custody.** `grep -r` over the rendered deployment manifests finds the PACS credential reference in exactly one deployment unit (the Gateway). Any second occurrence fails. (MOS-DATA-005)
2. **Network isolation.** AMENDED at specification 0.4.0. From an exec shell in the ct-worker, a service container, and ~~the OHIF container~~ the container serving the operator surfaces (`web`, `medos-web`), `nc -z -w2 <pacs-host> <pacs-port>` fails in all three; from the Gateway container it succeeds. The container was renamed, not removed, when the OHIF deployment was withdrawn at release 0.4.0 — `medos/deploy/compose/docker-compose.yml` runs a plain nginx in its place — and the check is re-pointed rather than withdrawn because a check naming a container the deployment does not start cannot fail, and a check that cannot fail is not a check that passed. (MOS-DATA-006)
3. **Cross-tenant imaging plane.** With tenant A's API key, `GET /dicomweb/t_A/studies/{tenant_B_study_uid}/metadata` returns `404` and a zero-length instance list; `GET /dicomweb/t_B/studies/{tenant_B_study_uid}/metadata` with tenant A's key returns `403` with `type: .../tenant-mismatch`. Neither response contains any DICOM attribute. (MOS-DATA-009, 013)
4. **QIDO filtering.** With a shared backend holding 10 studies, 4 owned by A, `GET /dicomweb/t_A/studies` returns exactly those 4 `StudyInstanceUID`s; asserted against the `studies` projection, not against a fixture list. (MOS-DATA-011)
5. **Token scoping.** A job's service token is replayed against a `SeriesInstanceUID` from the same study that is not in `series_instance_uids`: `403` with `type: .../outside-token-scope`. The same token after the job reaches a terminal state: `403`. (MOS-DATA-018, 019)
6. **De-identification determinism.** De-identify the `chest_ct_multirecon` fixture, restart every process, de-identify it again 24 h later (clock advanced): every VR `UI` attribute value at every sequence depth is byte-identical between the two runs, and `deid_uid_map` gained zero rows on the second run. (MOS-DATA-031)
7. **Well-known UID passthrough.** In the de-identified output, every `UI` value beginning `1.2.840.10008` equals the source value; every other `UI` value differs from the source and resolves through `deid_uid_map` back to exactly the source value. (MOS-DATA-034)
8. **Method codes.** The output carries `(0012,0062) = YES`, a non-empty `(0012,0063)` containing `deid_policy_version`, and a `(0012,0064)` whose code set equals the set computed from the policy by the MOS-DATA-029 table. `113110` is absent. (MOS-DATA-029, 030)
9. **Round-trip re-identification.** `reidentify(deidentify(study))` reproduces every source `UI` value. Performed without `phi.reidentify`: `403` plus exactly one `AuditEvent` of type `phi.reidentify` with `outcome: DENY`. (MOS-DATA-035)
10. **ResultBundle UID translation.** A `ResultBundle` referencing a SOP Instance UID absent from the job's working set terminates the job in `FAILED` with `reason: result_references_unknown_uid`, and a QIDO-RS query for the derived output series returns zero instances. (MOS-DATA-036)
11. **Private tags.** With `private_tags.default: REMOVE` and the example allowlist, the de-identified Siemens fixture contains `(0029,1010)`–`(0029,1020)` and no other odd-group element. (MOS-DATA-028)
12. **Burned-in PHI.** Fixture `burned_in_annotation` under `action: REJECT` produces a per-series rejection with `reason_code: burned_in_phi`; under `action: BLACKOUT` the emitted object has `(0028,0301) = NO`, code `113101` present, and the detected region's pixels equal the air value. (MOS-DATA-038, 040)
13. **Fail-closed.** With the `deid_uid_map` store unreachable, a `service`-class WADO-RS request returns `503` and the response body contains zero DICOM bytes; no partially de-identified object is emitted. (MOS-DATA-025, 037)
14. **PHI-free telemetry.** Over a full ingest→triage→job run, the structured log stream, the OTLP span attributes and the `/metrics` scrape contain zero occurrences of the fixture's `PatientName`, `PatientID`, any `SeriesDescription` string, and any `StudyInstanceUID`. (MOS-DATA-022, 026, 065)
15. **Ingest debounce.** Store 1131 instances with a 10 s pause after instance 400, `stability_seconds = 60`: exactly one `study_ingest` row, `stability: NATURAL`, `state: TRIAGED`, and exactly one triage execution recorded. (MOS-DATA-051)
16. **Forced stability.** Store instances at 120 s intervals with `max_receive_window_seconds = 300`: the study reaches `STABLE` with `stability: FORCED` and `triage_warnings` contains `forced_stability`. (MOS-DATA-052)
17. **Late instance, no duplicate job.** After `TRIAGED`, store one additional instance into an already-rejected localizer series: re-triage occurs, `selection_hash` is unchanged, and zero new `Job` rows are created. Then store 412 instances forming a new eligible recon: `selection_hash` changes and exactly one new `Job` is created. (MOS-DATA-053)
18. **Tenant attribution.** A C-STORE from an AE title absent from `ae_tenant_map` produces a `quarantine` row with `reason: tenant_unresolved`, zero `study_ingest` rows, zero `Job` rows, and the instances are invisible to every Gateway route for a principal without `phi.admin`. (MOS-DATA-024, 047)
19. **No pixel pull at triage.** The Gateway access log for the triage of `chest_ct_multirecon` contains only QIDO and `/metadata` entries: zero `/instances/`, `/frames/` or `/rendered` requests, and total transferred bytes below 64 MiB. (MOS-DATA-058)
20. **Triage extraction golden.** For every fixture in MOS-DATA-083, the produced per-series triage record, serialised as JSON, equals the committed golden JSON for every field of MOS-DATA-059 after canonical key ordering. (MOS-DATA-059, 060)
21. **Selection determinism.** Run selection on `chest_ct_multirecon` 100 times with the input series list shuffled by a different seed each time: identical `selected_series`, identical `selection_hash`, and identical `rejected_series` array including element order. (MOS-DATA-070, 081)
22. **Tie-break totality.** Construct a study with two series identical in every ranked field including `series_number`: selection succeeds, the winner is the lexicographically smaller `SeriesInstanceUID`, and `ambiguous` is `true`. (MOS-DATA-072, 074)
23. **Registration validation.** A `ServiceVersion` manifest whose `rank` omits the final `series_instance_uid asc` key is rejected at registration with an error naming `series_selector.inputs[0].rank`; one with an unknown `match` key is rejected naming that key. (MOS-DATA-073)
24. **Zero match is REJECTED, not FAILED.** Fixture `localizer_only` with the chest service deployed: exactly one `Job` in `REJECTED`, `reason_code: no_eligible_series`, `phase` null, `job_steps` empty, zero messages produced to `medicalos.svc.pulmoai_pleural_effusion.work`, and `GET /api/v1/jobs/{id}` returns a `triage.rejected_series` entry for every series in the study. (MOS-DATA-075)
25. **Non-applicable creates no job.** Fixture `brain_mri` with only chest services deployed: zero `Job` rows, exactly one `triage_decision` row per deployed service with `outcome: NOT_APPLICABLE`. (MOS-DATA-076)
26. **Explicit request always answers.** `POST /api/v1/jobs` naming the chest service for `brain_mri`: `202` with a `job_id`, the job terminating in `REJECTED` with `outside_applicability_envelope`, and an RFC 9457 body with `class: "clinical_rejection"`. (MOS-DATA-077, 080)
27. **Selection pinned across retry.** Force a transport retry of a running job; assert `selected_series` and `selection_hash` on the job row are byte-identical before and after, and that the selection engine was not invoked (counter `medicalos_selection_runs_total` unchanged). (MOS-DATA-082)
28. **Worked example reproduced.** Running the 3.6.1 selector over fixture `chest_ct_multirecon` produces the exact `triage` JSON of 3.7, compared after canonical key ordering. (MOS-DATA-070, 071, 079)
29. **FHIR absence.** At tags `v0.1.0` through `v0.4.0`, a case-insensitive repository grep for `fhir` returns hits only in this chapter, in the open-questions chapter, and in the release plan; zero hits in `control-plane/`, `runtime/`, `sdk/`, `medos/schemas/` or `deploy/`. (MOS-DATA-084)
30. **Mapping projection preserved.** Every coded concept emitted by the platform in an end-to-end run carries a non-null `scheme` and `value`, and for every concept present in the SNOMED-CT or LOINC column of the Chapter 4 dictionary, that column is non-null. (MOS-DATA-087)

---

[← 2. Architecture and the Medical Service Contract](02-architecture.md) · [Index](../../MEDICALOS_SPEC.md) · [4. Imaging Contracts: Geometry, Preprocessing and DICOM Output →](04-imaging-contracts.md)
