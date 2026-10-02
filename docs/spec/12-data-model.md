<!-- MedicalOS Specification v0.4.0 — chapter 12 of 19. Normative.
     178 requirements. Do not edit without a requirement-ID review. -->

[← 11. Agentic Layer, Chat and LLM Integration](11-agentic-layer.md) · [Index](../../MEDICALOS_SPEC.md) · [13. Observability, Deployment and Scaling →](13-operations.md)

---

## 12. Data Model and Storage

This chapter defines the persistent state of MedicalOS: the PostgreSQL schema for every entity in the canonical entity list, the rules that make tenancy a database property rather than a code-review property, the object-store layout, the relationship between PostgreSQL and the PACS, the migration policy, and what deletion and erasure mean when clinical results permanently reference source SOP Instance UIDs.

The previous version listed twenty-five table names in one line. Three of the five levels of the DICOM hierarchy had nowhere to live, the whole evidence plane was absent, `study_id` was the raw DICOM Study Instance UID, and nothing said what happens to a result when its patient is erased.

### 12.1 Requirement-ID allocation

**MOS-STORE-200** This chapter allocates `MOS-STORE-200`…`399`; Chapter 5 retains `001`…`199`, including the three IDs fixed by the spine (`001` job state machine, `002` PostgreSQL as sole source of truth for job state, `003` topic naming). No ID may be used by both chapters.

**MOS-STORE-201** Where this chapter defines a table whose semantics another chapter owns, that chapter is normative for behaviour and this one for structure; conflicts are resolved by amending the schema, not the behaviour. Concretely: the owning chapter fixes the **field names, the enum spellings, the casing and the complete value set**, and this chapter fixes the physical form — plural table name, `uuid` v7 surrogate keys, `text` + `CHECK` rather than a native enum (`MOS-STORE-209`), `created_at`/`updated_at`, composite tenant-scoped foreign keys. A column that renders another chapter's field MUST carry an inline cross-reference to the owning requirement ID stating that it mirrors it and MUST NOT diverge; where the two texts disagree the defect is in this chapter and is fixed here, not there. The ownership map is §12.17.

### 12.2 Storage split and sources of truth

Three persistent stores exist. No cache is authoritative for anything and no broker is read to reconstruct state.

| Concern | Store | Source of truth | Rebuildable? |
|---|---|---|---|
| Pixel data, DICOM objects, the Patient→Study→Series→Instance hierarchy | PACS (Orthanc in 0.1; any DICOMweb server) | PACS | this *is* the source |
| DICOM metadata for triage, selection, display, joins | Postgres `patients`, `studies`, `series`, `instances` | PACS | **yes** — a projection (§12.7) |
| Platform state attached to imaging (ingest and triage state, internal ids, first-seen) | Postgres | Postgres | no — survives rebuild (MOS-STORE-244) |
| Job state, steps, events, queue | Postgres | Postgres (`MOS-EXEC-002`) | no |
| Results, findings, measurements, provenance, reviews | Postgres | Postgres | no |
| Generated SEG/SR/SC object content | PACS | PACS | no |
| Identity, RBAC, policy, audit | Postgres | Postgres | no |
| Model weights, service and preprocessing bundles | Object store (content-addressed) | Object store | no |
| Label maps, canonical volumes, intermediate tensors | Object store | Object store | within retention only |
| Dataset manifests, annotations, per-case metrics, validation reports | Object store (object-locked) | Object store | no |
| Event-bus payloads | Kafka (0.3+) | **nothing** — derived transport | yes, from Postgres |

**MOS-STORE-202** Pixel data MUST NOT be stored in PostgreSQL: no column may hold DICOM pixel data, a Part 10 file or a NIfTI/NRRD volume, and `bytea` columns are restricted to digests, ciphertext of short identifiers, and signatures.

**MOS-STORE-203** Weights, bundles, label maps, canonical volumes, dataset manifests, annotations, per-case evaluation output and report documents MUST live in the object store, referenced from Postgres by `(bucket, object_key, content_digest)`, with no second copy in Postgres. The key layout is Chapter 8's `MOS-SEC-083`, whose first path segment after the environment is the tenant.

**MOS-STORE-204** The event bus MUST NOT be read to reconstruct any state in this chapter; a consumer that lost its offset recovers by reading Postgres.

**MOS-STORE-205** Only the API service, the platform-side worker sidecar and the maintenance jobs may open a database connection; a `ServiceVersion` container MUST NOT receive a database DSN, broker address or object-store credential in either execution mode (Chapter 2 is normative for the execution modes; Chapter 8 `MOS-SEC-087` is normative for the object-store credential and `MOS-SEC-005` for the PACS credential).

**MOS-STORE-206** No schema object, migration or query may depend on a proprietary cloud service or vendor extension; the object store is addressed through the S3 API and MUST be satisfiable by MinIO on-premises.

### 12.3 Schema conventions

**MOS-STORE-207** The target is **PostgreSQL 17 or later** — required for identity columns on partitioned tables and the `NULLS NOT DISTINCT` unique behaviour used below — with the `pgcrypto` extension.

**MOS-STORE-208** Every primary key is a surrogate `uuid` generated as UUIDv7; natural keys are expressed as UNIQUE constraints (§12.4), never as primary keys.

**MOS-STORE-209** Enumerated columns MUST be `text` with a `CHECK (col IN (...))`, not a native `enum`: a CHECK can be replaced inside a transaction in one migration, whereas a native enum can never have a value removed and its order can never change. This is a rule about **representation only**, and it is the one place where this chapter, not the owning chapter, is normative (`MOS-STORE-201`). Where an owning chapter writes its state set as `CREATE TYPE … AS ENUM` — Chapter 5's `job_state` and `job_step_status` are the two cases — this chapter renders it as `text` + `CHECK` carrying **exactly** that chapter's values in exactly that chapter's spelling and casing; the owning chapter remains normative for every value, and an acceptance check written against `enum_range()` MUST be restated against the CHECK constraint rather than the schema changed to satisfy it.

**MOS-STORE-210** Every table has `created_at timestamptz NOT NULL DEFAULT now()`, every mutable table has `updated_at` maintained by `touch_updated_at()`, and all platform timestamps are `timestamptz`. DICOM date and time attributes reproduced in the projection stay `date` and `time`, because inventing a zone for them would be a fabrication.

**MOS-STORE-211** Digests use the domain `sha256_digest` — lower-case hex prefixed `sha256:` — so the algorithm stays readable when a second one is introduced.

**MOS-STORE-354** `jcs_canonical(jsonb) -> bytea`, the RFC 8785 canonicaliser, is a platform function on the same footing as `uuid_generate_v7()` and `pgcrypto`'s `digest()`, and `MOS-STORE-214` declares it. Every digest computed over a `jsonb` value MUST go through it — the audit hash chain of §12.13 above all — and MUST NOT use `jsonb`'s own text rendering, whose object-key order is (length, bytewise) rather than RFC 8785's UTF-16 code-unit order: the two disagree on ordinary ASCII keys, so a chain hashed over the wrong one cannot be verified by any other implementation, which is the entire point of a signed, exportable chain. The bootstrap migration MUST fail when the function is absent rather than start without it, and CI MUST assert the function against the RFC 8785 test vectors.

**MOS-STORE-212** DICOM UIDs use the domain `dicom_uid`; UID columns MUST NOT be `text` or `uuid`.

**MOS-STORE-213** A `jsonb` column is permitted only when its content is validated against a named JSON Schema before insert, or when it is a provenance/audit detail bag never queried by key in a hot path; it MUST NOT be used to avoid modelling a relationship that is joined, filtered or constrained.

**MOS-STORE-214** Bootstrap DDL. Every migration set begins here.

```sql
-- migrations/0001_bootstrap.up.sql
SET lock_timeout = '3s';
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- On PostgreSQL 18+ this MAY be replaced by the built-in uuidv7().
CREATE FUNCTION uuid_generate_v7() RETURNS uuid AS $$
BEGIN
  RETURN encode(set_bit(set_bit(
    overlay(uuid_send(gen_random_uuid())
            PLACING substring(int8send(
              floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
            FROM 1 FOR 6), 52, 1), 53, 1), 'hex')::uuid;
END $$ LANGUAGE plpgsql VOLATILE;

CREATE DOMAIN dicom_uid AS varchar(64) CHECK (VALUE ~ '^[0-2](\.(0|[1-9][0-9]*))+$');
CREATE DOMAIN sha256_digest AS text CHECK (VALUE ~ '^sha256:[0-9a-f]{64}$');
CREATE DOMAIN oci_digest AS text CHECK (VALUE ~ '^sha256:[0-9a-f]{64}$');
CREATE DOMAIN semver AS text
  CHECK (VALUE ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$');
CREATE DOMAIN ucum_unit AS text CHECK (length(VALUE) BETWEEN 1 AND 32 AND VALUE !~ '\s');

CREATE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END $$ LANGUAGE plpgsql;

-- RFC 8785 (JSON Canonicalisation Scheme), called by audit_chain() (§12.13, MOS-SEC-151)
-- and by every digest this chapter computes over a jsonb value. Declared here beside
-- uuid_generate_v7() rather than assumed: without it the audit trigger does not create
-- and the hash chain has no implementation. It MUST NOT be approximated by jsonb's own
-- text rendering, whose object-key order is (length, bytewise) where RFC 8785's is UTF-16
-- code unit; the two disagree on ordinary ASCII keys, so a chain hashed over the wrong
-- one verifies nowhere but the machine that wrote it. The implementation ships as the
-- first-party extension `medicalos_jcs` — no network access and no vendor service, so
-- MOS-STORE-206 is satisfied — and the bootstrap fails rather than falls back.
CREATE EXTENSION IF NOT EXISTS medicalos_jcs;   -- provides jcs_canonical(jsonb) -> bytea
DO $$ BEGIN
  IF to_regprocedure('jcs_canonical(jsonb)') IS NULL THEN
    RAISE EXCEPTION 'jcs_canonical(jsonb) -> bytea is required (MOS-STORE-354)'
      USING ERRCODE = '42883';
  END IF;
END $$;

CREATE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
    USING ERRCODE = '42501';
END $$ LANGUAGE plpgsql;

-- Column-level immutability: EXECUTE FUNCTION forbid_column_change('col_a','col_b');
CREATE FUNCTION forbid_column_change() RETURNS trigger AS $$
DECLARE col text;
BEGIN
  FOREACH col IN ARRAY TG_ARGV LOOP
    IF to_jsonb(OLD) -> col IS DISTINCT FROM to_jsonb(NEW) -> col THEN
      RAISE EXCEPTION 'column %.% is immutable once written', TG_TABLE_NAME, col
        USING ERRCODE = '23514';
    END IF;
  END LOOP;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- Chapter 8 MOS-SEC-072 is normative for this behaviour: current_setting(name, false)
-- raises SQLSTATE 42704 when the variable is unset, so an unbound connection cannot read
-- a single row. This function mirrors that and MUST NOT be softened to a NULL-returning
-- form, which would turn a missing-context bug into an empty result set.
CREATE FUNCTION current_tenant_id() RETURNS uuid LANGUAGE plpgsql STABLE AS $$
DECLARE v text := current_setting('medicalos.tenant_id', false);
BEGIN
  IF v = '' THEN
    RAISE EXCEPTION 'medicalos.tenant_id is not set for this transaction'
      USING ERRCODE = '42704';
  END IF;
  RETURN v::uuid;
END $$;

CREATE TABLE schema_migrations (
  version text PRIMARY KEY,
  file_digest sha256_digest NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  applied_by text NOT NULL DEFAULT current_user,
  duration_ms integer NOT NULL
);
```

### 12.4 Tenant-scoped keys: why a DICOM UID is not a primary key

**MOS-STORE-215** A raw DICOM UID MUST NOT be a primary key or a foreign-key target anywhere; every imaging row is addressed internally by its surrogate `uuid` and externally by `(tenant_id, <uid column>)`.

Five concrete reasons, all of which bite in week one rather than year two:

1. **Public collections collide.** LIDC-IDRI study `1.3.6.1.4.1.14519.5.2.1.6279.6001.100225287222365663678666836860` is a fixed string. Tenant A ingests the collection for model development; Tenant B ingests it for site acceptance. With `studies(study_instance_uid PRIMARY KEY)` the second ingest either raises a unique violation — the platform cannot serve two customers the same public dataset — or, written as an upsert, silently attaches Tenant B's series to Tenant A's study row. That is a cross-tenant leak created by a key choice.
2. **De-identification legitimately makes one physical study two UIDs.** UID remapping is per-tenant by construction (§12.8), so the same acquisition is UID *X* in Tenant A and *Y* in Tenant B; a global key models that as two unrelated studies.
3. **The same UID may legitimately mean different bytes over time.** A bad ingest is purged and re-ingested under a corrected de-identification policy; a surrogate key makes that a new row with intact audit history, a natural key forces a resurrection or a destructive delete.
4. **UIDs are not reliably unique in the field.** Anonymisers, PACS migrations and misbehaving modalities all emit duplicates, and a natural primary key turns a vendor defect into an ingestion outage.
5. **Cost.** A `varchar(64)` foreign key on `instances` — 400+ rows per chest CT — is four times the width of a `uuid` and is duplicated into every index.

**MOS-STORE-216** These are the complete tenant-scoped natural keys; each MUST exist exactly as written.

| Table | Constraint | Note |
|---|---|---|
| `patients` | `UNIQUE (tenant_id, issuer_of_patient_id, patient_id_value)` | issuer defaults to `''`, never `NULL`; a `NULL` in a unique key defeats it |
| `studies` | `UNIQUE (tenant_id, study_instance_uid)` | |
| `series` | `UNIQUE (tenant_id, series_instance_uid)` | scoped to tenant, **not** to `study_id`: one Series UID under two studies is corruption and MUST fail |
| `instances` | `UNIQUE (tenant_id, sop_instance_uid)` | same reasoning |
| `result_dicom_objects` | `UNIQUE (tenant_id, sop_instance_uid)` | generated objects share the UID namespace with source objects |
| `study_ingest` | `UNIQUE (tenant_id, study_instance_uid)` | one ingest row per study (`MOS-DATA-045`, ch. 3, which is normative for the row's state machine and field set) |
| `deid_uid_map` | `PRIMARY KEY (tenant_id, deid_key_version, source_uid_hmac)` + `UNIQUE (tenant_id, deid_key_version, mapped_uid)` | both directions ⇒ a bijection **within one key version** (§12.8). The key set mirrors `MOS-DATA-033` (ch. 3) exactly and MUST NOT diverge: a key rotation opens a new UID space and retains every prior mapping, so `deid_key_version` cannot be dropped from either constraint |
| `jobs` | `UNIQUE (tenant_id, idempotency_key)` | the key's derivation and its `^ik_[a-z2-7]{26}$` form are `MOS-EXEC-053` (ch. 5); this column mirrors them and MUST NOT diverge |
| `results` | `UNIQUE (job_id, capability_id)` | closes the duplicate-result hole. One row per capability present in `ResultBundle.capability_outcomes` (ch. 2 §2.8); keying on `result_kind` cannot represent a multi-capability bundle |

**MOS-STORE-217** Every foreign key between two tenant-owned tables MUST be composite and include `tenant_id`, so a cross-tenant reference is unwritable even with row-level security off; each tenant-owned parent therefore carries a redundant `UNIQUE (tenant_id, id)`. RLS protects reads, composite FKs protect writes, and neither substitutes for the other. The DDL below shows this in full on the highest-risk edges and omits the repetition elsewhere; criterion 4 enforces it on every edge. One class of edge is excepted and the exception is closed: a foreign key whose **parent** is a `MOS-STORE-220` dual-scope table. A tenant row referencing a platform-global parent carries `tenant_id = <tenant>` while the parent carries `tenant_id IS NULL`, which no composite `(tenant_id, id)` foreign key can satisfy, so the ordinary path — a tenant job resolving to a vendor-supplied platform-global `service_versions` row — would be unwritable. Those edges are plain single-column foreign keys to the parent's `id`, exactly as `deployments.service_version_id` already is, and criterion 4 excludes them. Nothing is given up by it: write-side cross-tenant protection on such an edge is vacuous, because a platform-global row belongs to no tenant and there is no other tenant's row to point at.

```sql
ALTER TABLE results ADD CONSTRAINT results_tenant_id_uk UNIQUE (tenant_id, id);

-- Excerpt of result_reviews (§12.11), key and FK columns only.
CREATE TABLE result_reviews (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL,
  result_id uuid NOT NULL,
  -- The state set, the round semantics and every transition are Chapter 9 MOS-SAFE-058
  -- and MOS-SAFE-059. This column mirrors them and MUST NOT diverge: no value may be
  -- added, renamed or lower-cased here, and `results.review_status` is the denormalised
  -- projection of the highest-round row (MOS-SAFE-062).
  state text NOT NULL DEFAULT 'PENDING' CHECK (state IN
    ('PENDING','IN_REVIEW','ACCEPTED','MODIFIED','REJECTED','EXPIRED','SUPERSEDED')),
  CONSTRAINT result_reviews_result_fk FOREIGN KEY (tenant_id, result_id)
    REFERENCES results (tenant_id, id) ON DELETE RESTRICT
);
-- A plain REFERENCES results(id) would let tenant A's review point at tenant B's
-- result whenever RLS is not in force. The composite key cannot.
```

**MOS-STORE-218** `tenant_id` MUST be immutable on every tenant-owned table, enforced by `forbid_column_change('tenant_id')`; moving a row between tenants is not supported, the supported operation being export then ingest.

**MOS-STORE-219** Platform-global tables carry no `tenant_id` and no RLS, contain no PHI, and grant `medicalos_app` `SELECT` only — with the single exception `MOS-STORE-355` states: `schema_migrations`, `permissions`, `capabilities`, `capability_concepts`, `artifact_manifest_schemas`, `pacs_backends`.

**MOS-STORE-355** This is the one place the chapter says which path writes the capability catalogue, and there is no second answer. `capabilities` and `capability_concepts` take `INSERT`, `UPDATE` and `DELETE` for `medicalos_app` in addition to `SELECT`, and are written by the API as an authenticated principal rather than by a migration. Chapter 8 §8.3.2 registers `capability.create`, `capability.update` and `capability.delete` as class `governance` and binds `capability.update` to the seeded role `evidence_approver` (`MOS-SEC-041`), and `MOS-REG-048` (ch. 6) makes a change to `capability_concepts.parameters` increment `capabilities.revision` and invalidate `acceptance_criteria_id` until a new `EvaluationRun` is recorded; neither is expressible as a migration, and a principal that passes the policy decision point only to be refused by a missing grant is a denial nobody can debug. Both tables stay platform-global, carry no `tenant_id` and no row-level security, so the permission check is the **only** containment on the write (`MOS-STORE-230`) — which is why those three permissions are class `governance`, why every decision on them is persisted (`MOS-STORE-310`), and why `capabilities.slug` is immutable by `MOS-REG-041` rather than by convention. The other four tables on the list above stay `SELECT`-only for `medicalos_app`: `schema_migrations` and `permissions` are written by `medicalos_migrator` (`permissions` generated from `medos/contracts/permissions.yaml`, `MOS-SEC-032`), `artifact_manifest_schemas` by the migration introducing a schema version, and `pacs_backends` by deployment configuration.

`roles`, `role_permissions` and `user_roles` are **not** on that list and MUST NOT be returned to it. `MOS-SEC-038` (ch. 8) is normative: a `Role` is tenant data, it is created and edited by the tenant, clinical job titles MUST NOT be hardcoded anywhere in the platform, and the role "MUST NOT be a global closed enum" — while the same requirement explicitly permits this chapter to normalise its `permissions text[]` into a join table. All three are therefore ordinary tenant-owned tables under §12.5: `ENABLE` and `FORCE ROW LEVEL SECURITY`, a tenant-isolation policy, `UNIQUE (tenant_id, id)`, composite foreign keys, and `INSERT`/`UPDATE`/`DELETE` for `medicalos_app` so that `role.create`, `role.update` and `role.delete` (ch. 8 §8.3.2) are executable at all. `permissions` stays global because its rows are generated from `medos/contracts/permissions.yaml` (`MOS-SEC-032`) and are a platform catalogue, not tenant data.

**MOS-STORE-220** Tables that may hold either platform-global or tenant-owned rows carry a **nullable** `tenant_id` where `NULL` means visible to every tenant — `artifacts`, `services`, `service_versions`, `series_selectors`, `models`, `model_versions`, `preprocessing_specs`, `acceptance_criteria` — and their read policy admits `tenant_id IS NULL` while their write policy does not, so the application can read the platform catalogue and never create a platform-global row.

**MOS-STORE-221** Every index on a tenant-owned table intended to serve a tenant-filtered query MUST lead with `tenant_id`; one that does not turns a tenant-scoped lookup into a full scan with the RLS predicate applied as a filter, the moment a second tenant exists.

### 12.5 Row-level security

**MOS-STORE-222** Five database roles exist and no others. `medicalos_owner` owns every object and never logs in; migrations run as a separate `medicalos_migrator` that holds no `BYPASSRLS`. This split is Chapter 8's `MOS-SEC-073`; this table mirrors it and MUST NOT diverge.

| Role | Used by | Privileges | `LOGIN` | `BYPASSRLS` |
|---|---|---|---|---|
| `medicalos_owner` | nothing — it owns objects and is never connected as | owns every object | **no** | implicit as owner, neutralised by `FORCE` |
| `medicalos_migrator` | migration job only | full DDL, by membership of `medicalos_owner`; no application traffic | yes | **no** (`NOBYPASSRLS`) |
| `medicalos_app` | API, worker sidecar, maintenance | `SELECT`/`INSERT` everywhere; `UPDATE`/`DELETE` only where permitted; **no DDL**, owns nothing | yes | no |
| `medicalos_readonly` | analytics, support | `SELECT` only | yes | no |
| `medicalos_backup` | physical backup | replication | yes | n/a |

`medicalos_migrator` MUST be a member of `medicalos_owner` so that it can issue DDL against owned objects — which is exactly why `FORCE` is not optional (`MOS-STORE-223`). The superuser MUST NOT be used by any running process.

**MOS-STORE-223** Every tenant-owned table MUST have both `ENABLE` and `FORCE ROW LEVEL SECURITY`; `FORCE` is mandatory because without it the table owner — and therefore the migration role acting through its membership of `medicalos_owner`, and any operator who connects with owner rights — bypasses every policy silently.

**MOS-STORE-224** The predicate is `tenant_id = current_tenant_id()`, and `current_tenant_id()` resolves `medicalos.tenant_id` through `current_setting(name, false)`, which raises SQLSTATE `42704` when the variable is unset. A connection with no tenant context therefore **errors** on the first row it tries to read or write. **A connection with no tenant context is not a connection with full access; it is a connection with no access.** The earlier NULL-returning form of this function is withdrawn: a `NULL` predicate yields an empty result set, which is indistinguishable from "this tenant owns nothing" and hides a missing-context bug until it reaches a report. Chapter 8 `MOS-SEC-072` is normative for this behaviour and for the error code, which its acceptance criterion 1 asserts; this chapter mirrors it and MUST NOT diverge.

**MOS-STORE-225** There is **no cross-tenant escape hatch invented by this chapter** — no `OR current_setting('medicalos.scope', true) = 'platform'` in any policy expression. Exactly one override exists and Chapter 8 owns it: the break-glass permissive policy of `MOS-SEC-072`, `FOR SELECT … USING (current_setting('medicalos.break_glass', true) = 'on')`, which only the break-glass code path may set, which requires a `break_glass_grants` row with `expires_at - requested_at ≤ 60 minutes` (`MOS-SEC-079`), which SHOULD carry a second approver holding `break_glass.invoke` (`MOS-SEC-080`), and inside whose window every `AuditEvent` and `PolicyDecision` MUST carry `break_glass_id` (`MOS-SEC-081`). Its spelling, its session variable, its time box and its audit obligations mirror Chapter 8 and MUST NOT diverge. It is `FOR SELECT` and MUST NOT be written `FOR ALL`. Break-glass is a time-boxed **read** mechanism, and a permissive `FOR ALL` policy with no `WITH CHECK` silently grants writes: PostgreSQL reuses the `USING` expression as the `WITH CHECK` expression, so `INSERT` and `UPDATE` are admitted across every tenant for the length of the window, and `DELETE` is admitted too because `DELETE` has no `WITH CHECK` to reuse. Adding `WITH CHECK (false)` is therefore **not** a sufficient substitute — it closes `INSERT` and `UPDATE` and leaves `DELETE` open. Chapter 8 `MOS-SEC-072` MUST carry the same narrowing on `jobs_break_glass`.

Ordinary platform-wide work MUST NOT use it. Retention sweeps, the projection reconciler, the outbox relay, blob GC and any migration backfill that touches tenant-owned rows MUST iterate tenants, setting `medicalos.tenant_id` per tenant, and MUST record the iteration in `audit_events`; a process-wide connection that reads across tenants MUST NOT exist outside the break-glass path (`MOS-SEC-078`). The loop is the price of the guarantee.

```sql
-- Roles (migration 0001), mirroring ch. 8 MOS-SEC-072 and MOS-SEC-073.
CREATE ROLE medicalos_owner    NOLOGIN;
CREATE ROLE medicalos_migrator LOGIN NOBYPASSRLS;
CREATE ROLE medicalos_app      LOGIN NOBYPASSRLS;
CREATE ROLE medicalos_readonly LOGIN NOBYPASSRLS;
CREATE ROLE medicalos_backup   LOGIN REPLICATION NOBYPASSRLS;
GRANT medicalos_owner TO medicalos_migrator;   -- DDL rights only; FORCE still binds it

-- Standard set, applied to every strictly tenant-owned table.
ALTER TABLE studies OWNER TO medicalos_owner;
ALTER TABLE studies ENABLE ROW LEVEL SECURITY;
ALTER TABLE studies FORCE ROW LEVEL SECURITY;
CREATE POLICY studies_tenant_isolation ON studies
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());
-- The one permitted override (ch. 8 MOS-SEC-072). With the variable unset,
-- current_setting(..., true) is NULL, the comparison is NULL, and no row is admitted.
CREATE POLICY studies_break_glass ON studies FOR SELECT TO medicalos_app
  USING (current_setting('medicalos.break_glass', true) = 'on');

-- Variant for nullable-tenant catalogue tables (MOS-STORE-220).
ALTER TABLE service_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_versions FORCE ROW LEVEL SECURITY;
CREATE POLICY service_versions_read ON service_versions FOR SELECT
  USING (tenant_id IS NULL OR tenant_id = current_tenant_id());
CREATE POLICY service_versions_write ON service_versions FOR INSERT
  WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY service_versions_update ON service_versions FOR UPDATE
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());

-- tenants is the one table keyed on id rather than tenant_id.
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenants_self ON tenants
  USING (id = current_tenant_id()) WITH CHECK (id = current_tenant_id());
```

**MOS-STORE-226** Context MUST be established with `SET LOCAL` inside the transaction doing the work — equivalently `set_config('medicalos.tenant_id', $1, true)`, the form Chapter 8 `MOS-SEC-074` writes. `SET` without `LOCAL`, and `SET SESSION`, MUST NOT be used: under PgBouncer in transaction mode — the deployment default — a session-level `SET` outlives the transaction and leaks into the next tenant's request on the same backend. This is the highest-severity misuse of the design, and criterion 6 tests for it.

```sql
BEGIN;
SET LOCAL medicalos.tenant_id = '018f3b1c-7c2a-7b31-9a4e-2d9b1f5c8e40';
-- all statements for this request
COMMIT;
```

**MOS-STORE-227** The context MUST be set in exactly one place — the transaction wrapper that opens every database transaction — and the wrapper MUST fail rather than open a transaction with no tenant; no handler, worker or query helper may set it. Chapter 8 `MOS-SEC-075` is normative for the chokepoint: a connection-pool handle MUST NOT be reachable outside the repository package, and CI MUST assert this by import analysis.

**MOS-STORE-228** `medicalos_app` MUST be denied `UPDATE` and `DELETE` at the grant level on `audit_events`, `audit_chain_checkpoints`, `audit_archives`, `policy_activations`, `job_events`, `policy_decisions`, `result_provenance`, `evaluation_case_metrics`, `evaluation_case_scores`, `erasure_requests`, `erasure_actions`, `curation_decisions`, `corpus_stratification_reports`, `deid_uid_map`, `deid_identity_map`; the grant is the control and `forbid_mutation()` is the second layer, which also binds the owner role. Two tables take a **column-level** revocation instead of a table-level one, because another chapter declares exactly two or four of their columns mutable: `artifacts`, whose `lifecycle_status` and `status_reason` are the registry's only mutable columns (`MOS-REG-018`, ch. 6), and `validation_reports`, whose standing columns `status`, `superseded_by`, `revocation_reason` and `reproducibility_status` change while the signed document does not (`MOS-EVID-126`, ch. 7). Every other column of both tables is immutable and `DELETE` is denied on both.

```sql
REVOKE UPDATE, DELETE ON audit_events, audit_chain_checkpoints, audit_archives,
  policy_activations, job_events, policy_decisions, result_provenance,
  evaluation_case_metrics, evaluation_case_scores, erasure_requests, erasure_actions,
  curation_decisions, corpus_stratification_reports
  FROM medicalos_app;

-- The two de-identification maps take the same revocation and no trigger (see below);
-- ch. 3 MOS-DATA-033 requires exactly this: "UPDATE and DELETE MUST be revoked from the
-- application database role".
REVOKE UPDATE, DELETE ON deid_uid_map, deid_identity_map FROM medicalos_app;

REVOKE DELETE ON artifacts, validation_reports FROM medicalos_app;
REVOKE UPDATE (kind, family, version, manifest, manifest_digest, oci_image_digest,
  bundle_digest, signature, signer_identity, published_by) ON artifacts FROM medicalos_app;
-- The column names are §12.12's and Chapter 7 §7.12.1's (MOS-STORE-302); the superseded
-- `document_digest`, `document_bucket`, `document_object_key`, `signature_alg`,
-- `signer_identity` and `signed_at` spellings MUST NOT reappear here, because a REVOKE
-- naming a column that does not exist fails the migration at deploy time.
REVOKE UPDATE (kind, schema_version, subject_kind, subject_id, subject_version,
  capability_id, criteria_version, evaluation_run_ids, acceptance_criteria_id, verdict,
  report_bucket, report_object_key, report_digest, envelope_digest, bundle_bucket,
  bundle_object_key, bundle_digest, signature, signer_key_id, approver,
  approver_user_id, approver_name, approver_role, issued_at, valid_until)
  ON validation_reports FROM medicalos_app;

CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

The two `deid_*` maps are the deliberate exception to the trigger: they are `INSERT`-only for `medicalos_app` but MUST stay `DELETE`-able by `medicalos_owner`, because deleting those rows is how an erasure is executed (§12.16). They get the revocation and no trigger.

**MOS-STORE-229** RLS MUST be enabled in the same migration that creates the table; a migration adding a tenant-owned table without `ENABLE`, `FORCE`, a policy and the composite-FK unique key MUST fail CI. Chapter 8 `MOS-SEC-077` gives the CI query, which scans `pg_attribute` for a `tenant_id` column without forced row security; `roles`, `role_permissions` and `user_roles` are in scope for it exactly like any other tenant-owned table.

**MOS-STORE-230** RLS is a containment control, not an authorisation control: it answers which tenant's rows a transaction may touch, not whether a principal may perform an action. Permission checks are Chapter 8's, evaluated per request against the policy decision point and recorded in `policy_decisions` (§12.13); absence of a grant, of a policy or of a tenant context is a denial (`MOS-SEC-001`).

**MOS-STORE-231** `deid_uid_map` and `deid_identity_map` carry a column-level restriction in addition to the tenant policy: `medicalos_app` MUST NOT be granted `SELECT` on `source_uid_ct` / `source_value_ct`, re-identification running on a separately-credentialed path under the `phi.reidentify` permission, which requires a reason string of at least 20 characters recorded verbatim in the `AuditEvent` (`MOS-SEC-037`, ch. 8; `MOS-DATA-035`, ch. 3).

### 12.6 Identity and tenancy tables

```sql
CREATE TABLE tenants (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9-]{1,62}$'),
  display_name text NOT NULL,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended','closed')),
  -- MOS-SEC-121 (ch. 8) requires exactly this column, type and default; changing it
  -- requires `phi.policy_update` and an AuditEvent carrying old and new values.
  external_llm_allowed boolean NOT NULL DEFAULT false,
  -- MOS-TRAIN-072 (ch. 17) requires exactly this column, type and default. There is no
  -- per-study, per-user or per-environment override and no platform-administrator bypass,
  -- so no second column may qualify it. MOS-TRAIN-073 forbids setting it true without a
  -- live `training_data_policies` row (§12.12.1); that rule is cross-table and is carried
  -- by the constraint trigger MOS-STORE-358 declares, not by a CHECK here.
  training_use_allowed boolean NOT NULL DEFAULT false,
  org_oid_root text CHECK (org_oid_root ~ '^[0-2](\.(0|[1-9][0-9]*))+$'),
  default_deid_policy_id uuid,
  -- Surfaced to the Policy Engine as the Cedar attribute `Tenant.residency`
  -- (ch. 8 MOS-SEC-053). It is deliberately *not* named `residency` here, because
  -- `deployments.residency` is the unrelated GPU residency class of ch. 13 §13.10.3.
  data_region text NOT NULL DEFAULT 'on-prem',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  closed_at timestamptz
  -- There is deliberately no `default_clinical_use_mode` column here. MOS-SAFE-033 (ch. 9)
  -- is normative: `clinical_use_mode` is a field on `Deployment`, its default is
  -- `research_only`, and "there is no tenant-wide, service-wide or environment-wide
  -- override". A tenant-level default is exactly that override, because one settings
  -- change would silently promote every deployment created afterwards.
);

CREATE TABLE api_keys (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  principal_kind text NOT NULL CHECK (principal_kind IN ('user','service_account')),
  user_id uuid, service_account_id uuid,
  key_id text NOT NULL UNIQUE,           -- public half; safe to log; the only lookup key
  secret_hash bytea NOT NULL,            -- argon2id of the secret half
  scope text[] NOT NULL DEFAULT '{}',
  expires_at timestamptz NOT NULL, last_used_at timestamptz, revoked_at timestamptz,
  created_by uuid NOT NULL,
  source_ip_allowlist cidr[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((principal_kind = 'user') = (user_id IS NOT NULL)),
  CHECK ((principal_kind = 'service_account') = (service_account_id IS NOT NULL)),
  -- MOS-SEC-012 (ch. 8): an expiry beyond 365 days, and a key with no expiry, MUST be
  -- refused at creation. This CHECK is the structural rendering of that rule; it is
  -- written as a subtraction because `timestamptz + interval` is STABLE, not IMMUTABLE,
  -- and PostgreSQL refuses a non-immutable expression in a CHECK constraint.
  CONSTRAINT api_keys_max_lifetime CHECK (expires_at - created_at <= interval '365 days'),
  CONSTRAINT api_keys_user_fk FOREIGN KEY (tenant_id, user_id)
    REFERENCES users (tenant_id, id),
  CONSTRAINT api_keys_creator_fk FOREIGN KEY (tenant_id, created_by)
    REFERENCES users (tenant_id, id)
);
-- The column set is MOS-SEC-011 (ch. 8); `principal_id` is rendered here as the
-- stronger split pair `user_id` / `service_account_id` under `principal_kind`.
```

The remaining identity tables follow the same conventions (`id uuid PK`, `tenant_id` where tenant-owned, composite FKs, `created_at`/`updated_at`); their semantics are Chapter 8's.

| Table | Scope | Key | Notable columns and constraints |
|---|---|---|---|
| `users` | tenant | `id` | `UNIQUE (tenant_id, subject)`; `subject` (OIDC sub or local login), `email`, `display_name`, `status` ∈ `active, disabled, deleted` |
| `roles` | **tenant** | `id` | `UNIQUE (tenant_id, key)`; `key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{1,62}$')`, `display_name text NOT NULL`, `is_seeded boolean NOT NULL DEFAULT false`, `created_by`. **No CHECK enumerates role keys.** `MOS-SEC-038` (ch. 8) is normative: a `Role` is tenant data, MUST remain tenant-scoped and MUST NOT be a global closed enum, and clinical job titles MUST NOT be hardcoded. The eight seeded keys of ch. 8 §8.3.4 — `tenant_admin`, `clinical_operator`, `clinical_reviewer`, `integration_engineer`, `evidence_scientist`, `evidence_approver`, `auditor`, `data_steward` — are seed **rows** written at tenant creation and marked `is_seeded = true` (`MOS-SEC-041`), editable by the tenant, never a constraint. Role inheritance MUST NOT be modelled (`MOS-SEC-039`): there is no `parent_role_id` column |
| `permissions` | global | `id text` | `CHECK (id ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$')` — a resource segment and an action segment, with at most one resource sub-segment between them; this mirrors the widened `MOS-SEC-031`, under which the catalogue's three-segment rows (`result.review.read`, `result.review.create`, `result.review.assign`, `result.review.reopen`, `result.read.research`, `deployment.gate.override`, `artifact.status.set`) are storable and a four-segment identifier is still a build error. `class` ∈ `read, write, phi, clinical, admin, governance` — the complete six-value enum of ch. 8 §8.3.1, mirrored spelling-for-spelling, which MUST NOT diverge. Rows are generated from `medos/contracts/permissions.yaml` (`MOS-SEC-032`); this table never originates a spelling, and a permission absent from it denies (`MOS-SEC-033`) |
| `role_permissions` | **tenant** | `(role_id, permission_id)` | the normalisation of `MOS-SEC-038`'s `permissions text[]`, which that requirement explicitly permits. Composite FK to `roles (tenant_id, id)`; plain FK to the global `permissions (id)`. Wildcards MUST NOT be storable (`MOS-SEC-033`), which the FK already enforces |
| `user_roles` | tenant | `(user_id, role_id)` | composite FKs to `users (tenant_id, id)` **and** `roles (tenant_id, id)`; `granted_by`, `granted_at`. A principal's permission set is the union over its roles (`MOS-SEC-040`) |
| `service_accounts` | tenant | `id` | `UNIQUE (tenant_id, name)`; `purpose` ∈ `gateway, worker, ci, integration, viewer` |

Because a role is now tenant data that may be renamed or deleted, a column recording **the role a human acted under** — `result_reviews.reviewer_role`, `validation_reports.approver_role`, `audit_events.on_behalf_of_role` — MUST store the role's `key` as plain `text` as a snapshot taken at the moment of the act, and MUST NOT be a foreign key to `roles`. A foreign key would make `role.delete` (ch. 8 §8.3.2) either fail on historical evidence or cascade it away, and would silently rewrite history when a tenant renames a role; both outcomes contradict `MOS-STORE-233`.

**MOS-STORE-232** `api_keys.secret_hash` holds an argon2id hash of the secret half only; the plaintext MUST NOT be recoverable, and the schema provides no column that could hold it. Lookup MUST be by `key_id` and the hash MUST NOT be used as a lookup key (`MOS-SEC-011`).

**MOS-STORE-233** `users.status = 'deleted'` is a tombstone, not a row removal: a `users` row referenced by `result_reviews`, `audit_events`, `validation_reports.approver_user_id` or `acceptance_criteria.approved_by` MUST NOT be deleted, because deleting a reviewer deletes the evidence that a human reviewed a clinical result. §12.16 defines what user erasure does instead.


### 12.7 The imaging projection

**MOS-STORE-234** The PACS is the source of truth for pixel data and for the Patient → Study → Series → Instance hierarchy; the Postgres tables `patients`, `studies`, `series`, `instances` are a **projection** — a queryable, joinable, indexable copy of DICOM metadata keyed by UID, fully rebuildable by MOS-STORE-245.

**MOS-STORE-235** The projection exists because DICOMweb cannot serve the queries the platform needs: QIDO-RS has no join, no aggregate, no ordering by a computed value and no way to ask "every study in this tenant with a thin axial recon and no completed effusion job".

**MOS-STORE-236** All PACS access, the projection synchroniser included, goes through the DICOM Gateway; no table, job or migration defined here may hold or use a direct PACS credential (Chapter 3 is normative).

**MOS-STORE-356** Two entity names Chapter 3 uses normatively are **roles of tables defined here, not tables of their own**, and this requirement is where they resolve; §12.17 lists no separate row for either because no separate table exists. **`study_tenancy`** (`MOS-DATA-010`, `MOS-DATA-011`, `MOS-DATA-012`) is `studies`. `MOS-DATA-010` fixes its key set as `UNIQUE (tenant_id, study_instance_uid)` plus a non-unique index on `study_instance_uid` and names Chapter 12 for the physical DDL; those are `studies_natural_uk` (`MOS-STORE-216`) and `studies_uid_global_idx` exactly. The Gateway's QIDO-RS tenancy filter (`MOS-DATA-011`) and its STOW-RS ownership check (`MOS-DATA-012`) therefore read `studies`, and the authority `MOS-DATA-010` claims — this record, not the PACS, decides who may see a study — is `studies.tenant_id` under the forced row-level security of `MOS-STORE-223`. **`series_triage`** (`MOS-DATA-062`, ch. 3 acceptance check 20) is the `MOS-DATA-059` field class on `series` (§12.7), `triage_spec_version` included; the golden-JSON comparison of that check runs against those columns. Neither name gets a second table, and one MUST NOT be added: a separate `study_tenancy` would be a second answer to "who owns this study" that `studies.tenant_id` could silently contradict, and a separate `series_triage` would be a second copy of the very fields every `SeriesSelector` matches on. Chapter 3 SHOULD re-point both names at the tables above; until it does, this requirement is the mapping.

```sql
CREATE TABLE pacs_backends (                         -- global (MOS-STORE-219)
  id text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{2,31}$'),
  kind text NOT NULL CHECK (kind IN ('orthanc','dcm4chee','dicomweb')),
  dicomweb_base_url text NOT NULL,
  supports_change_feed boolean NOT NULL DEFAULT false,
  partitioning text NOT NULL
    CHECK (partitioning IN ('shared_gateway_filtered','per_tenant')),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE patients (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  patient_id_value text NOT NULL,                -- (0010,0020) as held by the PACS
  issuer_of_patient_id text NOT NULL DEFAULT '', -- (0010,0021); '' never NULL
  patient_name text,                             -- (0010,0010)
  patient_birth_date date,                       -- (0010,0030)
  patient_sex text CHECK (patient_sex IN ('M','F','O','')),
  -- The PHI posture of the live projection. This is NOT `dataset_versions.deident_status`
  -- (ch. 7 MOS-EVID-012), which records what was done to a sealed evidence manifest; the two
  -- vocabularies are different subjects and MUST NOT be conflated or migrated onto each other.
  phi_state text NOT NULL CHECK (phi_state IN ('identified','pseudonymised','erased')),
  study_count integer NOT NULL DEFAULT 0,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_synced_at timestamptz NOT NULL DEFAULT now(),
  erased_at timestamptz, deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT patients_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT patients_natural_uk UNIQUE (tenant_id, issuer_of_patient_id, patient_id_value),
  CHECK ((phi_state = 'erased') = (erased_at IS NOT NULL))
);

CREATE TABLE studies (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL, patient_id uuid NOT NULL,
  study_instance_uid dicom_uid NOT NULL,
  accession_number text, study_date date, study_time time,
  study_description text, referring_physician text,
  modalities_in_study text[] NOT NULL DEFAULT '{}',
  series_count integer NOT NULL DEFAULT 0,
  instance_count integer NOT NULL DEFAULT 0,
  patient_age_years integer,                     -- ch. 3 MOS-DATA-061
  patient_sex text CHECK (patient_sex IN ('M','F','O','')),
  pacs_backend text NOT NULL REFERENCES pacs_backends(id),
  phi_state text NOT NULL CHECK (phi_state IN ('identified','pseudonymised','erased')),
  deid_policy_id uuid,                           -- FK added in §12.9.2 (deid_policies is later)
  deid_policy_version integer,                   -- ch. 3 MOS-DATA-028, pinned with the policy id
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_synced_at timestamptz NOT NULL DEFAULT now(),
  projection_revision bigint NOT NULL DEFAULT 1,
  erased_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT studies_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT studies_natural_uk UNIQUE (tenant_id, study_instance_uid),
  CONSTRAINT studies_patient_fk FOREIGN KEY (tenant_id, patient_id)
    REFERENCES patients (tenant_id, id) ON DELETE RESTRICT,
  CHECK ((deid_policy_id IS NULL) = (deid_policy_version IS NULL))
);
CREATE INDEX studies_patient_idx ON studies (tenant_id, patient_id, study_date DESC);
-- MOS-STORE-356: the non-unique index MOS-DATA-010 requires, and the index MOS-DATA-012's
-- cross-tenant ownership check on STOW-RS reads. It is an allow-listed MOS-STORE-221
-- exception under criterion 30 and cannot lead with `tenant_id`: the question it answers
-- is "does any OTHER tenant already own this StudyInstanceUID", which is unanswerable
-- inside one tenant's predicate. It is read only by the admission path, which runs as
-- `medicalos_owner` and returns an ownership verdict, never a row.
CREATE INDEX studies_uid_global_idx ON studies (study_instance_uid);

-- Ingest and triage state is its own row, not a column on `studies`. The state set,
-- the debounce, the quarantine reasons and the re-triage path are Chapter 3
-- MOS-DATA-044 to MOS-DATA-057; this table is their rendering. Chapter 3 is normative
-- for every value below and this table MUST NOT diverge from it.
CREATE TABLE study_ingest (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  study_id uuid NOT NULL,
  study_instance_uid dicom_uid NOT NULL,
  ingest_path text NOT NULL, ingest_class text NOT NULL, source_aet text,
  state text NOT NULL DEFAULT 'RECEIVING' CHECK (state IN
    ('RECEIVING','STABLE','TRIAGED','QUARANTINED','SUPERSEDED')),   -- MOS-DATA-045
  stability text CHECK (stability IN ('NATURAL','FORCED')),
  quarantine_reason text
    CHECK (quarantine_reason IN ('tenant_unresolved','tenant_conflict')), -- MOS-DATA-047/048
  instance_count integer NOT NULL DEFAULT 0, series_count integer NOT NULL DEFAULT 0,
  first_instance_at timestamptz, last_instance_at timestamptz,
  stable_at timestamptz, triaged_at timestamptz,
  triage_spec_version integer, selection_hash sha256_digest,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT study_ingest_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT study_ingest_uk UNIQUE (tenant_id, study_instance_uid),
  CONSTRAINT study_ingest_study_fk FOREIGN KEY (tenant_id, study_id)
    REFERENCES studies (tenant_id, id) ON DELETE RESTRICT,
  CHECK ((state = 'TRIAGED') = (triaged_at IS NOT NULL)),
  CHECK ((state = 'QUARANTINED') = (quarantine_reason IS NOT NULL))
);
CREATE INDEX study_ingest_pending_idx ON study_ingest (tenant_id, state, first_instance_at)
  WHERE state IN ('RECEIVING','STABLE');

CREATE TABLE projection_sync_state (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  pacs_backend text NOT NULL REFERENCES pacs_backends(id),
  study_instance_uid dicom_uid,            -- NULL = backend-wide cursor
  last_change_cursor text,                 -- Orthanc /changes Seq, or a QIDO watermark
  last_event_at timestamptz, last_reconcile_at timestamptz,
  last_reconcile_result text CHECK (last_reconcile_result IN ('clean','repaired','failed')),
  drift_series integer NOT NULL DEFAULT 0,
  drift_instances integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX projection_sync_backend_uk ON projection_sync_state
  (tenant_id, pacs_backend) WHERE study_instance_uid IS NULL;
CREATE UNIQUE INDEX projection_sync_study_uk ON projection_sync_state
  (tenant_id, pacs_backend, study_instance_uid) WHERE study_instance_uid IS NOT NULL;
```

`series` and `instances` follow the same conventions (`id uuid PK`, `tenant_id uuid NOT NULL`, composite FK to the parent, `created_at`, `erased_at`). Their columns are the triage and geometry contract:

**`series`** — `UNIQUE (tenant_id, series_instance_uid)`; `FOREIGN KEY (tenant_id, study_id) REFERENCES studies (tenant_id, id) ON DELETE CASCADE`.

| Column | Type | Source |
|---|---|---|
| `series_instance_uid` | `dicom_uid NOT NULL` | (0020,000E) |
| `series_number` | `integer` | (0020,0011) |
| `modality` | `text NOT NULL` | (0008,0060) |
| `series_description` | `text` | (0008,103E), PHI-cleaned |
| `protocol_name` | `text` | (0018,1030), PHI-cleaned |
| `body_part_examined` | `text` | (0018,0015), normalised (ch. 3 MOS-DATA-060.1) |
| `image_type` | `text[] NOT NULL DEFAULT '{}'` | (0008,0008) |
| `convolution_kernel` | `text` | (0018,1210) |
| `kernel_class` | `text CHECK (kernel_class IN ('SOFT','STANDARD','SHARP','UNKNOWN'))` | derived (ch. 3 MOS-DATA-060.2; mirrors it and MUST NOT diverge) |
| `frame_of_reference_uid` | `dicom_uid` | (0020,0052) |
| `patient_position` | `text` | (0018,5100), projection-only (see below) |
| `pixel_spacing_mm` | `numeric(9,6)[] CHECK (cardinality(pixel_spacing_mm) = 2)` | (0028,0030), mode across instances — the `pixel_spacing_mm float[2]` of ch. 3 MOS-DATA-059, same name, same shape |
| `slice_thickness_mm` | `numeric(9,4)` | (0018,0050), median |
| `spacing_between_slices_mm` | `numeric(9,4)` | **derived**: `median(d_i)` of the projected slice deltas (ch. 3 MOS-DATA-060.3, MOS-STORE-241). Not the (0018,0088) attribute, which the platform does not cache |
| `slice_spacing_uniformity_mm` | `numeric(9,4)` | derived: `max abs(d_i − median(d_i))` (ch. 3 MOS-DATA-060.3) |
| `missing_slice_gaps`, `duplicate_position_count` | `integer` | derived (ch. 3 MOS-DATA-060.3) |
| `z_extent_mm` | `numeric(9,2)` | derived (ch. 3 MOS-DATA-060.3) |
| `image_orientation_patient` | `numeric(9,6)[] CHECK (cardinality(image_orientation_patient) = 6)` | (0020,0037), mode |
| `orientation_class` | `text CHECK (orientation_class IN ('AXIAL','CORONAL','SAGITTAL','OBLIQUE','UNKNOWN'))` | derived (ch. 3 MOS-DATA-060.4 is normative for this value set; mirrors it and MUST NOT diverge) |
| `axial_deviation_deg`, `gantry_tilt_deg` | `numeric(6,3)` | derived (MOS-DATA-060.4), (0018,1120) as an absolute value |
| `contrast_agent` | `text` | (0018,0010) |
| `contrast_present` | `boolean NOT NULL` | derived (ch. 3 MOS-DATA-059) |
| `contrast_phase` | `text CHECK (contrast_phase IN ('NONE','ARTERIAL','PORTAL_VENOUS','DELAYED','PULMONARY_ARTERIAL','UNKNOWN'))` | derived (ch. 3 MOS-DATA-060.5 is normative for this value set; mirrors it and MUST NOT diverge — `UNKNOWN` is first-class and a selector requiring a phase MUST reject it) |
| `kvp` | `numeric(6,2)` | (0018,0060), mode |
| `exposure_mas`, `ctdi_vol_mgy` | `numeric(9,3)` | (0018,1152), (0018,9345), median |
| `image_rows`, `image_columns` | `integer` | (0028,0010), (0028,0011), mode — the `rows`/`columns` of ch. 3 MOS-DATA-059 (see the naming note below) |
| `bits_stored` | `smallint` | (0028,0101) |
| `photometric_interpretation` | `text` | (0028,0004) |
| `rescale_slope`, `rescale_intercept` | `numeric(12,6)` | (0028,1053), (0028,1052) |
| `sop_class_uids` | `dicom_uid[] NOT NULL DEFAULT '{}'` | (0008,0016), distinct |
| `transfer_syntax_uids` | `dicom_uid[] NOT NULL DEFAULT '{}'` | file meta, distinct |
| `image_type_consistent`, `frame_of_reference_consistent`, `rescale_present`, `is_multiframe`, `has_pixel_data`, `is_localizer`, `is_derived`, `is_secondary`, `is_reformatted`, `is_projection` | `boolean NOT NULL` | derived (ch. 3 MOS-DATA-059/060) |
| `burned_in_annotation` | `text CHECK (burned_in_annotation IN ('YES','NO','UNKNOWN'))` | (0028,0301) |
| `burned_in_state` | `text NOT NULL DEFAULT 'UNSCREENED' CHECK (burned_in_state IN ('UNSCREENED','CLEAN','SUSPECTED','REDACTED'))` | platform state; ch. 3 MOS-DATA-038/040/042 is normative — `UNSCREENED` until screening runs, `REDACTED` only after a `BLACKOUT` action |
| `patient_age_years` | `integer` | derived from (0010,1010) |
| `triage_warnings` | `text[] NOT NULL DEFAULT '{}' CHECK (triage_warnings <@ ARRAY['forced_stability','non_uniform_spacing','missing_slices','duplicate_positions','mixed_image_type','mixed_frame_of_reference','gantry_tilt_present','compressed_transfer_syntax','no_rescale','single_instance'])` | derived (ch. 3 MOS-DATA-060.6 is normative for this closed set; mirrors it and MUST NOT diverge) |
| `triage_spec_version` | `integer NOT NULL` | ch. 3 MOS-DATA-062 |
| `manufacturer`, `manufacturer_model_name` | `text` | (0008,0070), (0008,1090) |
| `software_versions` | `text` | (0018,1020), projection-only (see below) |
| `instance_count` | `integer NOT NULL DEFAULT 0` | derived (MOS-STORE-240) |
| `origin` | `text NOT NULL DEFAULT 'acquired' CHECK (origin IN ('acquired','ai_derived'))` | platform state |
| `produced_by_result_id` | `uuid`, `CHECK ((origin='ai_derived') = (produced_by_result_id IS NOT NULL))` | platform state |

`MOS-DATA-059` (ch. 3) is normative for **which** facts triage extracts and caches and for what each is named; this chapter is normative for their column types and for the physical table. Every `MOS-DATA-059` field MUST appear above under the same name, and the set MUST NOT diverge. Three classes of column exist and only the first is addressable by a `SeriesSelector`:

1. the `MOS-DATA-059` triage fields — a `SeriesSelector` `match` key (`MOS-DATA-069`) or `rank` key (`MOS-DATA-071`) naming a field absent from this class MUST be refused at manifest registration (`MOS-DATA-073`);
2. **projection-only** DICOM attributes that triage does not evaluate — `patient_position` and `software_versions` — which exist so the projection can answer operational queries and which MUST NOT appear in a selector;
3. **platform state** — `burned_in_state`, `origin`, `produced_by_result_id` — preserved across a rebuild by MOS-STORE-244.

One renaming is performed and it is fixed: `MOS-DATA-059`'s `rows` and `columns` are `image_rows` and `image_columns` here, because `ROWS` is a SQL keyword; a selector naming `rows` or `columns` resolves to them. No other `MOS-DATA-059` field is renamed, re-cased or re-typed by this chapter.

```sql
CREATE INDEX series_study_idx ON series (tenant_id, study_id, series_number);
CREATE INDEX series_selection_idx
  ON series (tenant_id, modality, body_part_examined, slice_thickness_mm)
  WHERE origin = 'acquired';
```

**`instances`** — `UNIQUE (tenant_id, sop_instance_uid)`; `FOREIGN KEY (tenant_id, series_id) REFERENCES series (tenant_id, id) ON DELETE CASCADE`; index on `(tenant_id, series_id, instance_number)`.

| Column | Type | Source |
|---|---|---|
| `sop_instance_uid`, `sop_class_uid` | `dicom_uid NOT NULL` | (0008,0018), (0008,0016) |
| `instance_number` | `integer` | (0020,0013) |
| `image_position_patient` | `numeric(9,4)[] CHECK (cardinality(image_position_patient) = 3)` | (0020,0032) |
| `slice_location_mm` | `numeric(9,4)` | (0020,1041) |
| `image_rows`, `image_columns` | `integer` | (0028,0010), (0028,0011) |
| `bits_allocated` | `smallint` | (0028,0100) |
| `rescale_slope`, `rescale_intercept` | `numeric(12,6)` | (0028,1053), (0028,1052) |
| `photometric_interpretation` | `text` | (0028,0004) |
| `transfer_syntax_uid` | `dicom_uid` | (0002,0010) |
| `file_size_bytes` | `bigint` | PACS |
| `pixel_digest` | `sha256_digest` | computed at first read |

#### 12.7.1 How the projection stays in sync

**MOS-STORE-237** The projection is written by exactly three paths and by no other code:

| Path | Trigger | Latency | Scope |
|---|---|---|---|
| **P1 — arrival** | Gateway observes a stored instance (change feed or stored-instance callback) | seconds | the affected study |
| **P2 — pre-flight** | a job is about to be created, or a study is opened in the viewer | synchronous | one study |
| **P3 — reconcile** | scheduled sweep per tenant per backend | hourly within 48 h of first sight, daily otherwise | tenant-wide |

**MOS-STORE-238** Every projection write MUST be an idempotent upsert on the tenant-scoped natural key, incrementing `projection_revision` and setting `last_synced_at`; re-running P1 for an already-projected instance MUST change no column but `last_synced_at`.

```sql
-- Canonical upsert shape; series and instances follow the same form.
INSERT INTO studies (tenant_id, patient_id, study_instance_uid, accession_number,
                     study_date, study_description, pacs_backend, phi_state)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (tenant_id, study_instance_uid) DO UPDATE
SET accession_number = EXCLUDED.accession_number,
    study_date = EXCLUDED.study_date,
    study_description = EXCLUDED.study_description,
    last_synced_at = now(),
    projection_revision = studies.projection_revision + 1
WHERE studies.erased_at IS NULL
RETURNING id;
```

**MOS-STORE-239** The upsert MUST NOT resurrect an erased row — the `WHERE erased_at IS NULL` guard is mandatory on all four projection tables — and re-arrival of an erased study MUST be refused by the Gateway and raise `audit_events.action = 'projection.erased_resurrection_blocked'`.

**MOS-STORE-240** The denormalised counters `studies.series_count`, `studies.instance_count`, `series.instance_count` and `patients.study_count` MUST be recomputed by P3 and compared against `COUNT(*)`; code MUST NOT assume a counter is exact, and a decision needing an exact count counts rows or asks the Gateway.

**MOS-STORE-241** The derived geometry columns are computed by the same code path that builds the canonical volume (Chapter 4 is normative for the geometry, Chapter 3 `MOS-DATA-060.3`/`MOS-DATA-060.4` for the cached field definitions), recomputed whenever the series' instance set changes, and MUST NOT be recomputed by a second implementation.

**MOS-STORE-242** P3 MUST detect and record drift rather than silently repairing it: compare projected series/instance UID sets and counts against QIDO-RS, write the differences to `projection_sync_state`, repair, and emit one audit row per repaired study with before/after counts; non-zero drift on two consecutive sweeps MUST raise an operational alert (Chapter 13).

**MOS-STORE-243** `last_synced_at` is **advisory**: any consumer needing a guarantee about a study's current content — job creation, series selection, the skip-if-present check before re-inferring — MUST re-verify against the Gateway. The projection answers which studies are interesting; the Gateway answers what is in this study right now.

#### 12.7.2 What the projection may not be the only record of

**MOS-STORE-244** These columns are **platform state**, not DICOM, and a rebuild MUST preserve them by matching on the tenant-scoped natural key:

| Table | Preserved |
|---|---|
| `patients` | `id`, `tenant_id`, `first_seen_at`, `phi_state`, `erased_at` |
| `studies` | `id`, `tenant_id`, `first_seen_at`, `deid_policy_id`, `deid_policy_version`, `erased_at`. Ingest and triage state live on `study_ingest` (`MOS-DATA-045`), never on `studies` |
| `study_ingest` | the whole row: it is platform state, not DICOM, and a rebuild MUST leave `state`, `stability`, `quarantine_reason`, `triaged_at`, `triage_spec_version` and `selection_hash` untouched |
| `series` | `id`, `tenant_id`, `origin`, `produced_by_result_id`, `burned_in_state`, `erased_at` |
| `instances` | `id`, `tenant_id`, `erased_at` |

Everything else in those tables is reconstructible from DICOM and MUST NOT be the sole record of anything — which is why `result_provenance` copies `input_series_uids` and `input_instance_uids` rather than holding only foreign keys (§12.11).

#### 12.7.3 Rebuild procedure

**MOS-STORE-245** A full rebuild for `(tenant, pacs_backend)` MUST run without stopping jobs already in flight, in this order:

1. Set `medicalos.tenant_id`; take an advisory lock on `(tenant_id, pacs_backend)` so two rebuilds cannot interleave.
2. Snapshot the MOS-STORE-244 columns into a temporary table keyed by the natural key.
3. Enumerate the backend via QIDO-RS — studies, then series, then instances — paging with the Gateway's cursor.
4. Upsert every level with MOS-STORE-238's statement; `id` values are never regenerated for existing rows, so every foreign key from `jobs`, `job_series`, `results` and `execution_artifacts` survives.
5. Mark projected rows the enumeration did not return as missing: set `last_synced_at`, leave the row, record the count. **A rebuild MUST NOT delete a projection row** — a row referenced by a completed result would take that result's referential integrity with it. Removal is an explicit purge (§12.16), never a side effect of a sync.
6. Recompute the derived counters and geometry columns.
7. Write `projection_sync_state` and emit one audit event summarising the rebuild.

**MOS-STORE-246** A rebuild MUST be idempotent: run twice against an unchanged PACS it leaves `drift_series = 0` and `drift_instances = 0` and changes no column but `last_synced_at` and `last_reconcile_at`.

**MOS-STORE-247** A rebuild MUST NOT re-run de-identification and MUST NOT mint UIDs; if the PACS holds identified data in a tenant whose `phi_state` is `pseudonymised` it MUST stop, set `last_reconcile_result = 'failed'` and alert, rather than quietly project PHI into a tenant configured not to hold it.

**MOS-STORE-248** `instances` is the highest-cardinality table and past 200 million rows SHOULD be converted to `PARTITION BY RANGE (created_at)` with monthly partitions; the schema is written so this changes no foreign key, because nothing references `instances(id)` except `dataset_cases`, which references UIDs by design (§12.12).

#### 12.7.4 Tenant attribution: `ae_tenant_map`

Every other table in this chapter is read inside a transaction that already knows its tenant. Two are not, and they are the two that exist precisely because that question has not yet been answered: `ae_tenant_map`, which answers it for Path A ingest, and `quarantine` (§12.7.5), which records that it could not be answered. A tenant-isolation policy on either is a circular definition — the resolver would have to know the tenant in order to look up the tenant — and `MOS-STORE-224` makes the failure mode loud rather than silent, because a connection with no `medicalos.tenant_id` raises `42704` on the first row it touches. `MOS-STORE-225` forbids inventing an escape hatch to get around that, so these two tables are not tenant-owned at all.

**MOS-STORE-345** Exactly two **pre-tenancy** tables exist — `ae_tenant_map` and `quarantine` — and the class is closed: a third joins it only by amending this requirement. A pre-tenancy table MUST carry no column named `tenant_id`, MUST have no row-level security and no policy, MUST hold no patient attribute (no patient name, birth date, accession number, study or series description, and no pixel data — only DICOM UIDs, AE titles and platform state, the same floor `MOS-STORE-272` and ch. 5 `MOS-EXEC-079` set for job events and DLQ rows), and MUST be gated at the API by a Chapter 8 permission rather than by the database. `MOS-STORE-219`'s platform-global list is **not** widened to include them: that list grants `medicalos_app` `SELECT` only, and both of these tables are written on the ingest path. A tenant reference these tables hold is an *output* or an *annotation*, never an ownership discriminator, and MUST therefore be named for its role (`resolved_tenant_id`, `owning_tenant_id`, `arriving_tenant_id`, `released_to_tenant_id`) — never `tenant_id`. That naming is load-bearing, not cosmetic: `MOS-STORE-229`'s CI query scans `pg_attribute` for a `tenant_id` column without forced row security, and a pre-tenancy table named the obvious way would make that query cry wolf on the two tables that are correct and be silenced for the whole schema.

**MOS-STORE-346** `ae_tenant_map` is the Path A resolution table. Chapter 3 `MOS-DATA-046` is normative for the lookup order and for the key `UNIQUE (called_aet, calling_aet)`; this table mirrors both and MUST NOT diverge.

```sql
CREATE TABLE ae_tenant_map (                        -- pre-tenancy (MOS-STORE-345)
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- DICOM AE titles: 16 bytes, default repertoire, no backslash, no control characters.
  -- The `= btrim(...)` clause is not decoration: a modality that pads its AE title to 16
  -- characters with trailing spaces and a mapping row typed without them are the same AE
  -- to a human and two different strings to an equality lookup, and the failure surfaces
  -- as a quarantined study rather than as an error.
  called_aet text NOT NULL CHECK (
    length(called_aet) BETWEEN 1 AND 16 AND called_aet = btrim(called_aet)
    AND called_aet !~ '[[:cntrl:]]' AND strpos(called_aet, '\') = 0),
  calling_aet text NOT NULL CHECK (
    length(calling_aet) BETWEEN 1 AND 16 AND calling_aet = btrim(calling_aet)
    AND calling_aet !~ '[[:cntrl:]]' AND strpos(calling_aet, '\') = 0),
  -- The resolution OUTPUT. Deliberately not named `tenant_id` (MOS-STORE-345).
  resolved_tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  pacs_backend text NOT NULL REFERENCES pacs_backends(id),
  -- Ch. 3 MOS-DATA-049 declares `ingest_class` per source; for Path A the source is this
  -- AE pair. Mirrors that two-value set and MUST NOT diverge.
  ingest_class text NOT NULL DEFAULT 'clinical'
    CHECK (ingest_class IN ('clinical','corpus')),
  -- Ch. 3 MOS-DATA-051 (default 60, minimum 10) and MOS-DATA-052 (default 1800),
  -- both "configurable per source"; mirrors their defaults and bounds.
  stability_seconds integer NOT NULL DEFAULT 60 CHECK (stability_seconds >= 10),
  max_receive_window_seconds integer NOT NULL DEFAULT 1800
    CHECK (max_receive_window_seconds > stability_seconds),
  description text NOT NULL DEFAULT '',
  enabled boolean NOT NULL DEFAULT true,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ae_tenant_map_uk UNIQUE (called_aet, calling_aet)      -- MOS-DATA-046
);
CREATE INDEX ae_tenant_map_tenant_idx ON ae_tenant_map (resolved_tenant_id);

CREATE TRIGGER ae_tenant_map_touch BEFORE UPDATE ON ae_tenant_map
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- No ENABLE/FORCE ROW LEVEL SECURITY and no policy, by MOS-STORE-345.
GRANT SELECT, INSERT, UPDATE, DELETE ON ae_tenant_map TO medicalos_app;
GRANT SELECT ON ae_tenant_map TO medicalos_readonly;
```

**MOS-STORE-347** Resolution is **exact-pair equality on the trimmed AE titles and nothing else**. There is no wildcard row, no prefix form and no catch-all: `MOS-DATA-046` says first match wins, and a wildcard is only meaningful under a precedence rule that Chapter 3 does not define, so the schema refuses to imply one. A row whose `enabled` is `false` MUST NOT match, and a lookup that matches no enabled row MUST quarantine under `MOS-DATA-047` rather than fall back to any tenant — `MOS-DATA-047` states directly that assigning unattributed data to a default tenant is forbidden, and the absence of a nullable default column here is the structural half of that prohibition.

`pacs_backend` is recorded but is **not** part of the key, because `MOS-DATA-046` fixes the key as `(called_aet, calling_aet)` and this chapter may not widen it (`MOS-STORE-201`). The consequence is worth stating plainly: two PACS backends in one deployment cannot both accept the same called/calling AE pair for different tenants. That is the correct behaviour for a site AE title, which is globally unique on a DICOM network by convention; if a deployment ever needs otherwise, the fix is an amendment to `MOS-DATA-046`, not a divergent key here.

**MOS-STORE-347a** §12.17 gains the row `| tenant attribution | `ae_tenant_map` | Ch. 3 |`, without which acceptance criterion 1 fails on the table's existence.

#### 12.7.5 `quarantine`

Chapter 12 mentioned quarantine once, as `study_ingest.quarantine_reason`, and that column is not the record `MOS-DATA-047` asks for. `study_ingest` is keyed on `(tenant_id, study_instance_uid)`, and ch. 3 acceptance check 18 requires that an instance from an unmapped AE title produce **zero** `study_ingest` rows — there is no tenant to key one on. The quarantine record is therefore a separate, per-instance, pre-tenancy table, and the two uses of the word do not overlap: `study_ingest.quarantine_reason = 'tenant_conflict'` marks a *study already owned by a tenant* whose late instances arrived over the wrong route, while a `quarantine` row is the *instance* that was held.

**MOS-STORE-348** `quarantine` is a pre-tenancy table under `MOS-STORE-345`. One row per held SOP instance. Chapter 3 `MOS-DATA-047` and `MOS-DATA-048` are normative for when a row is written and for the two `reason` values, which are the same closed pair already carried by `study_ingest.quarantine_reason`; this table mirrors them and MUST NOT diverge.

```sql
CREATE TABLE quarantine (                           -- pre-tenancy (MOS-STORE-345)
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  reason text NOT NULL
    CHECK (reason IN ('tenant_unresolved','tenant_conflict')),   -- MOS-DATA-047/048
  -- Identity only. No patient name, birth date, accession number or description:
  -- MOS-STORE-345 forbids them here, and a row about data nobody may look at is the
  -- worst possible place to keep a copy of the data.
  sop_instance_uid dicom_uid NOT NULL,
  series_instance_uid dicom_uid NOT NULL,
  study_instance_uid dicom_uid NOT NULL,
  sop_class_uid dicom_uid,
  modality text,
  pacs_backend text NOT NULL REFERENCES pacs_backends(id),
  ingest_path text NOT NULL,
  calling_aet text, called_aet text,
  -- MOS-DATA-048: for `tenant_conflict`, `owning_tenant_id` is tenant A, which already
  -- owns the StudyInstanceUID, and `arriving_tenant_id` is tenant B, which the arriving
  -- route resolved to. Ownership MUST NOT be re-assigned to B. Both are NULL for
  -- `tenant_unresolved`, which is the whole point of the row. Neither is named
  -- `tenant_id` (MOS-STORE-345).
  owning_tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
  arriving_tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
  received_at timestamptz NOT NULL,
  -- The exit. MOS-DATA-045 makes QUARANTINED terminal until an operator acts; these
  -- columns are that act. They are nullable timestamps rather than a state enum because
  -- Chapter 3 owns no release vocabulary and this chapter may not invent one
  -- (MOS-STORE-201); the state is a function of the timestamps.
  released_to_tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,
  released_by uuid REFERENCES users(id), released_at timestamptz,
  purged_by uuid REFERENCES users(id), purged_at timestamptz,
  resolution_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT quarantine_instance_uk UNIQUE (pacs_backend, sop_instance_uid),
  CHECK ((reason = 'tenant_conflict')
         = (owning_tenant_id IS NOT NULL AND arriving_tenant_id IS NOT NULL)),
  CHECK (owning_tenant_id IS NULL OR owning_tenant_id <> arriving_tenant_id),
  CHECK ((released_at IS NULL) = (released_to_tenant_id IS NULL)),
  CHECK ((released_at IS NULL) = (released_by IS NULL)),
  CHECK ((purged_at IS NULL) = (purged_by IS NULL)),
  CHECK (released_at IS NULL OR purged_at IS NULL)
);
CREATE INDEX quarantine_open_idx ON quarantine (reason, received_at)
  WHERE released_at IS NULL AND purged_at IS NULL;
-- The Gateway filter of MOS-STORE-349 runs per QIDO-RS response; it is study-keyed
-- because a 1,131-instance chest CT from an unmapped AE is 1,131 rows.
CREATE INDEX quarantine_study_idx ON quarantine (study_instance_uid)
  WHERE released_at IS NULL AND purged_at IS NULL;
CREATE INDEX quarantine_sop_idx ON quarantine (sop_instance_uid)
  WHERE released_at IS NULL AND purged_at IS NULL;

CREATE TRIGGER quarantine_touch BEFORE UPDATE ON quarantine
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER quarantine_sealed BEFORE UPDATE ON quarantine
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('reason','sop_instance_uid',
    'series_instance_uid','study_instance_uid','pacs_backend','received_at',
    'owning_tenant_id','arriving_tenant_id');

-- No ENABLE/FORCE ROW LEVEL SECURITY and no policy, by MOS-STORE-345. No DELETE grant:
-- `purged_at` is a tombstone. The row is the record that unattributed data reached the
-- deployment, which outlives the bytes it describes.
GRANT SELECT, INSERT, UPDATE ON quarantine TO medicalos_app;
GRANT SELECT ON quarantine TO medicalos_readonly;
```

**MOS-STORE-349** An **open** `quarantine` row — `released_at IS NULL AND purged_at IS NULL` — and an `instances` row for the same `sop_instance_uid` MUST NOT both exist. The ingest resolver writes one or the other in a single transaction and never both: a quarantined instance MUST NOT be projected, MUST NOT be triaged, MUST NOT produce a `study_ingest` row and MUST NOT create a `Job` (`MOS-DATA-047`). Because the bytes are already in the PACS by the time the Path A callback fires, the Gateway MUST additionally subtract every open row from every QIDO-RS response and refuse every WADO-RS retrieval that names one, for every principal except the one `MOS-DATA-024` names; `quarantine_study_idx` and `quarantine_sop_idx` exist for exactly that subtraction. The projection is the platform's index of what may be looked at, and an entry in it is itself a disclosure.

One cross-chapter defect is recorded rather than silently rendered: `MOS-DATA-024` gates this visibility on `phi.admin`, which is **not** a row of Chapter 8's permission catalogue (§8.3.1; the `phi.*` rows are `phi.reidentify`, `phi.erase`, `phi.policy_update`). `MOS-SEC-033` denies any permission absent from `medos/contracts/permissions.yaml`, so as written the gate denies everyone. Chapter 8 owns the spelling and Chapter 3 owns the citation; this chapter neither invents a permission nor renders the broken one into a grant.

**MOS-STORE-350** Releasing a row is the only path out and it is a two-step operation, in this order: add or correct the `ae_tenant_map` row (`MOS-STORE-346`), then release, which sets `released_to_tenant_id`, `released_by` and `released_at`, re-runs projection for the affected study under that tenant, and writes an `audit_events` row with `action = 'ingest.quarantine_released'` carrying the reason, the instance count and both tenant ids. Purge sets `purged_by`/`purged_at` and removes the instance from the PACS; `action = 'ingest.quarantine_purged'`. Both actions are audited, neither is automatic, and a sweep MUST NOT release or purge on a timer — unattributed clinical data that ages is still unattributed clinical data.

**MOS-STORE-350a** §12.17 gains the row `| quarantined ingest | `quarantine` | Ch. 3 |`.

#### 12.7.6 `triage_decision`

This is the table that keeps `REJECTED` meaningful. `MOS-DATA-076` is explicit about the alternative: without a place to record "this service looked at this study and it was outside its envelope", every chest CT accumulates a `REJECTED` job from every neuro, cardiac and MSK service deployed in the tenant, and a state that a radiologist is supposed to read as *clinically actionable* becomes background noise. The distinction is between a service that was not applicable — no `Job`, one row here — and a service that was applicable and found nothing usable — a `Job` in `REJECTED` with `reject_reason_code` (`MOS-DATA-075`, `MOS-STORE-270`).

**MOS-STORE-351** Chapter 3 `MOS-DATA-078` is normative for this record's field names and `MOS-DATA-076` for the three-value `outcome` set; this table mirrors both and MUST NOT diverge. Chapter 3's `triage_decision_id` is `id` here under `MOS-STORE-208`, and its `service_id` / `service_version` pair is **served by joining** `service_versions` and `services` on `service_version_id` rather than denormalised, the same treatment §12.17 gives `MOS-SAFE-053`'s marking fields.

```sql
-- Ch. 3 MOS-DATA-076 and MOS-DATA-078 are normative for every field and value below.
-- Singular, not `triage_decisions`, because MOS-DATA-076 and ch. 3 acceptance check 25
-- name the table and a rename would strand both; the `study_ingest` precedent applies.
CREATE TABLE triage_decision (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),   -- ch. 3 `triage_decision_id`
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  study_id uuid NOT NULL,                           -- MOS-STORE-215: the UID is not the key
  study_instance_uid dicom_uid NOT NULL,            -- ch. 3 carries the UID; kept verbatim
  service_version_id uuid NOT NULL,                 -- `service_id` + `service_version` join
  selector_version integer NOT NULL CHECK (selector_version >= 1),  -- MOS-DATA-078
  triage_spec_version integer NOT NULL CHECK (triage_spec_version >= 1),  -- MOS-DATA-062
  outcome text NOT NULL
    CHECK (outcome IN ('SELECTED','NOT_APPLICABLE','REJECTED')),  -- MOS-DATA-078
  job_id uuid,
  selection_hash sha256_digest,
  decided_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT triage_decision_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT triage_decision_uk UNIQUE
    (tenant_id, study_id, service_version_id, triage_spec_version, selector_version),
  CONSTRAINT triage_decision_study_fk FOREIGN KEY (tenant_id, study_id)
    REFERENCES studies (tenant_id, id) ON DELETE CASCADE,
  -- Both FKs below target tables defined later in this chapter; they are added by the
  -- migration that creates those tables, exactly as `studies.deid_policy_id` is (§12.9.2).
  -- Plain, not composite, for the MOS-STORE-217 dual-scope reason.
  CONSTRAINT triage_decision_service_version_fk
    FOREIGN KEY (service_version_id)
    REFERENCES service_versions (id) ON DELETE RESTRICT,
  -- CASCADE, not RESTRICT: `job.delete` (ch. 8) is a cleanup, and a decision whose job
  -- has been deleted would violate the biconditional below rather than degrade.
  CONSTRAINT triage_decision_job_fk FOREIGN KEY (tenant_id, job_id)
    REFERENCES jobs (tenant_id, id) ON DELETE CASCADE,
  -- MOS-DATA-078: "`job_id` is null for `NOT_APPLICABLE`". SELECTED (MOS-DATA-075) and
  -- REJECTED (MOS-DATA-075/077) both always have a Job, so the rule is a biconditional.
  CHECK ((outcome = 'NOT_APPLICABLE') = (job_id IS NULL)),
  -- Applicability fails before selection runs, so NOT_APPLICABLE has no selection to
  -- hash; SELECTED and REJECTED both ran selection and both carry one (MOS-DATA-053).
  CHECK ((outcome = 'NOT_APPLICABLE') = (selection_hash IS NULL))
);
CREATE INDEX triage_decision_study_idx
  ON triage_decision (tenant_id, study_id, decided_at DESC);
CREATE INDEX triage_decision_not_applicable_idx
  ON triage_decision (tenant_id, service_version_id, decided_at DESC)
  WHERE outcome = 'NOT_APPLICABLE';

ALTER TABLE triage_decision OWNER TO medicalos_owner;
ALTER TABLE triage_decision ENABLE ROW LEVEL SECURITY;
ALTER TABLE triage_decision FORCE ROW LEVEL SECURITY;
CREATE POLICY triage_decision_tenant_isolation ON triage_decision
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY triage_decision_break_glass ON triage_decision FOR SELECT TO medicalos_app
  USING (current_setting('medicalos.break_glass', true) = 'on');

CREATE TRIGGER triage_decision_touch BEFORE UPDATE ON triage_decision
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER triage_decision_sealed BEFORE UPDATE ON triage_decision
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','study_id',
    'study_instance_uid','service_version_id');   -- MOS-STORE-218 and the decision's subject

GRANT SELECT, INSERT, UPDATE, DELETE ON triage_decision TO medicalos_app;
GRANT SELECT ON triage_decision TO medicalos_readonly;
```

**MOS-STORE-352** `triage_decision_uk` is what ch. 3 acceptance check 25 asserts — "exactly one `triage_decision` row per deployed service" — and it is keyed on the two version stamps so that a genuine re-extraction opens a new row rather than overwriting the evidence of the old one. Re-triage after `SUPERSEDED` (`MOS-DATA-053`) recomputes `selection_hash` at the **same** `triage_spec_version`, so it MUST be written as an idempotent upsert on that key, rewriting `outcome`, `job_id`, `selection_hash` and `decided_at` and leaving `id` unchanged; a second row for the same `(study, service_version, triage_spec_version, selector_version)` is corruption and the constraint MUST refuse it. Triage decisions are not evidence-plane records and carry no append-only trigger: `MOS-STORE-228`'s list is not widened.

**MOS-STORE-353** `selector_version` is Chapter 3's (`MOS-DATA-078`, `MOS-DATA-082`) and this chapter currently has nowhere to resolve it: `series_selectors` (§12.9.2) is keyed `UNIQUE (service_version_id, name)` and carries no version column. §12.9.2 MUST therefore gain `selector_version integer NOT NULL CHECK (selector_version >= 1)` on `series_selectors`, mirroring the `series_selector.selector_version` member of the service manifest, so that `triage_decision.selector_version` and the pin `MOS-DATA-082` puts on the `Job` both denote something the schema can name. Until that column exists the value is an unresolvable integer, which is worse than absent because it looks resolvable.

**MOS-STORE-353a** §12.17 gains the row `| `TriageDecision` | `triage_decision` | Ch. 3 |`.

### 12.8 De-identification mapping tables

Results reference source SOP Instance UIDs, so a de-identification step assigning a fresh random UID per pass silently invalidates every result the platform has ever produced. The mapping is therefore a stored, tenant-scoped bijection, not a function of a per-run seed.

```sql
-- The de-identification policy is Chapter 3's artifact (MOS-DATA-027 to MOS-DATA-030,
-- MOS-DATA-038 to MOS-DATA-040). Every column below mirrors a field of that policy
-- name-for-name and value-for-value and MUST NOT diverge from it: PS3.15 Annex E is a
-- single base profile with independently selected options, never a menu of alternatives.
CREATE TABLE deid_policies (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  deid_policy_version integer NOT NULL CHECK (deid_policy_version >= 1),  -- MOS-DATA-028
  name text NOT NULL,
  profile text NOT NULL CHECK (profile IN ('BASIC')),                     -- MOS-DATA-027
  options jsonb NOT NULL,          -- the MOS-DATA-028 `options` map
  date_shift jsonb NOT NULL,       -- {scope, range_days}
  pixel_phi jsonb NOT NULL,        -- {detector, action, always_screen_modalities, ...}
  private_tags jsonb NOT NULL,     -- {default, allowlist[]}
  spec jsonb NOT NULL, spec_digest sha256_digest NOT NULL,
  sealed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT deid_policies_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT deid_policies_version_uk UNIQUE (tenant_id, deid_policy_version),
  CONSTRAINT deid_policies_uk UNIQUE (tenant_id, name, spec_digest),
  CHECK (options ?& array['retain_uids','retain_safe_private','retain_device_identity',
    'retain_institution_identity','retain_patient_characteristics',
    'retain_longitudinal_temporal','clean_descriptors','clean_pixel_data',
    'clean_recognizable_visual_features','clean_structured_content','clean_graphics']),
  CHECK (options -> 'retain_uids' = 'false'::jsonb),                      -- MOS-DATA-030
  CHECK (options ->> 'retain_longitudinal_temporal'
           IN ('NONE','FULL_DATES','MODIFIED_DATES')),                    -- MOS-DATA-028
  CHECK (date_shift ?& array['scope','range_days']),
  CHECK (date_shift ->> 'scope' IN ('per_patient','per_tenant')),
  CHECK (pixel_phi ?& array['detector','action']),
  CHECK (pixel_phi ->> 'action' IN ('REJECT','BLACKOUT','ALLOW')),        -- MOS-DATA-040
  CHECK (private_tags ?& array['default','allowlist']),
  CHECK (private_tags ->> 'default' = 'REMOVE')                           -- MOS-DATA-028
);
CREATE TRIGGER deid_policies_sealed BEFORE UPDATE ON deid_policies
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'deid_policy_version','profile','options','date_shift','pixel_phi','private_tags',
    'spec','spec_digest');

CREATE TABLE deid_uid_map (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  deid_key_version integer NOT NULL,
  uid_kind text NOT NULL
    CHECK (uid_kind IN ('study','series','sop','frame_of_reference','other')),
  source_uid_hmac bytea NOT NULL,    -- HMAC-SHA256(per-tenant key, source UID)
  source_uid_ct bytea NOT NULL,      -- AEAD ciphertext of the source UID
  mapped_uid dicom_uid NOT NULL,     -- the `2.25.<uuid-int>` surrogate of MOS-DATA-032
  deid_policy_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, deid_key_version, source_uid_hmac),
  CONSTRAINT deid_uid_map_reverse_uk UNIQUE (tenant_id, deid_key_version, mapped_uid),
  CONSTRAINT deid_uid_map_policy_fk FOREIGN KEY (tenant_id, deid_policy_id)
    REFERENCES deid_policies (tenant_id, id)
);

CREATE TABLE deid_identity_map (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  attribute text NOT NULL
    CHECK (attribute IN ('patient_id','patient_name','accession_number','other_id')),
  source_value_hmac bytea NOT NULL, source_value_ct bytea NOT NULL,
  replacement_value text NOT NULL,
  deid_policy_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, attribute, source_value_hmac),
  CONSTRAINT deid_identity_map_reverse_uk UNIQUE (tenant_id, attribute, replacement_value),
  CONSTRAINT deid_identity_map_policy_fk FOREIGN KEY (tenant_id, deid_policy_id)
    REFERENCES deid_policies (tenant_id, id)
);

-- MOS-DATA-033: both maps are append-only for the application role. Erasure (§12.16)
-- runs as a separate, audited role; there is no other path that removes a row.
REVOKE UPDATE, DELETE ON deid_uid_map, deid_identity_map FROM medicalos_app;
```

**MOS-STORE-249** UID remapping MUST be consistent and reversible within a tenant (`MOS-DATA-031` is the blocking invariant): the map is a bijection within one `(tenant_id, deid_key_version)` UID space, the primary key enforcing the forward direction and the unique constraint the reverse. `deid_key_version` is the version of the per-tenant HMAC key (Chapter 3, `MOS-DATA-032`), and rotating that key opens a **new** UID space; the rotation MUST retain every prior mapping and MUST NOT delete, overwrite or re-key any existing row, because results permanently reference the source SOP Instance UIDs those mappings translate. A second mapping of the same source UID **within one key version** is a constraint violation that MUST fail loudly rather than mint a second pseudonym, and a generated `mapped_uid` that collides with an existing row for a different source UID MUST abort the de-identification with `deid_failed` (`MOS-DATA-033`) rather than overwrite. The mapped value is the `2.25.<uuid-as-integer>` surrogate of `MOS-DATA-032`; it is not derived from a configurable per-tenant UID root, and this table stores no such root.

**MOS-STORE-250** The lookup key is an HMAC of the source UID under a per-tenant key held outside the database (Chapter 8 owns key custody) and the original is stored only as AEAD ciphertext, so a database dump does not re-identify. Reversal is `phi.reidentify`-gated and audited (`MOS-DATA-035`).

**MOS-STORE-251** The policy in force when a study was processed MUST be recorded on `studies.deid_policy_id` together with `studies.deid_policy_version`, and both MUST be copied into `result_provenance` (`MOS-DATA-028`: the policy version is pinned into provenance and into every `DatasetVersion`); changing a tenant's policy creates a new `deid_policies` row with the next `deid_policy_version` and never mutates an existing one. `deid_policy_version` is monotonic per tenant and MUST NOT be reused.

**MOS-STORE-252** Deleting a `deid_uid_map` / `deid_identity_map` row destroys the ability to re-identify the subject and is the terminal act of an erasure (§12.16); it MUST NOT happen for any other reason, MUST NOT be possible for the application role (`MOS-DATA-033`), and MUST be recorded in `erasure_actions`.

### 12.9 Artifacts, services, capabilities and models

**MOS-STORE-253** `artifacts` is the single table backing every immutable, signed, versioned bundle — service versions, model versions, preprocessing specs, dataset manifests, annotation sets and, from 0.4, workflows, tools and agents — each validated against a JSON Schema in `artifact_manifest_schemas`. There is no per-kind artifact table. `artifacts.kind` is the **family-level** kind (`service`, `model`, `preprocessing`, `dataset`, `annotation`, `workflow`, `tool`, `agent`, `policy_set`); `policy_set` is the kind `MOS-SEC-062` (ch. 8) requires for an activated `PolicySet` — the signed bundle whose artifacts-bucket path §12.14 already reserves and whose digest `policy_activations.policy_set_digest` must resolve to — and Chapter 6's artifact-kind list MUST gain it in the same change; it takes the `ELSE` branch of `artifacts_lifecycle_status_per_kind` until Chapter 6 declares a narrower one; Chapter 6 writes the same fact as a version-level kind (`service_version`, `model_version`, `preprocessing_spec`, `dataset_version`, `annotation_set`, `workflow_version`). The mapping is the identity plus the `_version`/`_spec`/`_set` suffix and is fixed; where Chapter 6 names a version-level kind, read this column's family-level value.

**MOS-STORE-254** `artifacts` rows are immutable after insert; `manifest_digest` is the row's natural identity and any change is a new row — except `lifecycle_status` and `status_reason`, which `MOS-REG-018` (ch. 6) declares mutable and whose transition graph is `MOS-REG-021`. The `artifacts_immutable` trigger MUST therefore be `BEFORE UPDATE OF` every column except those two, and `BEFORE DELETE` unconditionally.

```sql
CREATE TABLE artifact_manifest_schemas (             -- global (MOS-STORE-219)
  kind text NOT NULL CHECK (kind IN ('service','model','preprocessing','dataset',
    'annotation','workflow','tool','agent','policy_set')),
  schema_version text NOT NULL,
  json_schema jsonb NOT NULL,
  schema_digest sha256_digest NOT NULL,
  introduced_in semver NOT NULL,
  PRIMARY KEY (kind, schema_version)
);

CREATE TABLE artifacts (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid REFERENCES tenants(id),             -- NULL = platform catalogue
  kind text NOT NULL,
  family text NOT NULL,                              -- the version family, e.g. 'pulmo.pleural-effusion'
  version semver NOT NULL,
  version_major integer NOT NULL, version_minor integer NOT NULL,
  version_patch integer NOT NULL, version_pre text NOT NULL DEFAULT '',
  lifecycle_status text NOT NULL DEFAULT 'DRAFT',    -- MOS-REG-020 (ch. 6) is normative
  status_reason text,
  published_by text NOT NULL,
  manifest_schema_version text NOT NULL,
  manifest jsonb NOT NULL,
  manifest_digest sha256_digest NOT NULL UNIQUE,
  oci_image_digest oci_digest,
  bundle_bucket text, bundle_object_key text,
  bundle_digest sha256_digest, bundle_size_bytes bigint,
  signature_alg text CHECK (signature_alg IN ('cosign-sigstore','ed25519')),
  signature bytea, signer_identity text, sbom_object_key text,
  sealed_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT artifacts_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT artifacts_family_version_uk UNIQUE (kind, family, version),
  CONSTRAINT artifacts_schema_fk FOREIGN KEY (kind, manifest_schema_version)
    REFERENCES artifact_manifest_schemas (kind, schema_version),
  -- The permitted status set is per kind and is MOS-REG-020 (ch. 6) rendered as a
  -- constraint. This mirrors that table exactly and MUST NOT diverge from it: a sealed
  -- evidence artifact carries SEALED/DEFECTIVE (ch. 7 MOS-EVID-014) and a versioned
  -- executable carries the eight-value lifecycle.
  CONSTRAINT artifacts_lifecycle_status_per_kind CHECK (
    CASE kind
      WHEN 'service'       THEN lifecycle_status IN ('DRAFT','REGISTERED','VALIDATING',
                                 'VALIDATED','APPROVED','DEPRECATED','SUSPENDED','RECALLED')
      WHEN 'model'         THEN lifecycle_status IN ('DRAFT','REGISTERED','VALIDATING',
                                 'VALIDATED','APPROVED','DEPRECATED','SUSPENDED','RECALLED')
      WHEN 'preprocessing' THEN lifecycle_status IN ('REGISTERED','APPROVED','DEPRECATED',
                                 'SUSPENDED','RECALLED')
      WHEN 'workflow'      THEN lifecycle_status IN ('REGISTERED','APPROVED','DEPRECATED',
                                 'SUSPENDED','RECALLED')
      WHEN 'dataset'       THEN lifecycle_status IN ('SEALED','DEFECTIVE')
      WHEN 'annotation'    THEN lifecycle_status IN ('SEALED','DEFECTIVE')
      ELSE lifecycle_status IN ('DRAFT','REGISTERED','VALIDATING','VALIDATED',
                                'APPROVED','DEPRECATED','SUSPENDED','RECALLED')
    END),
  CHECK (lifecycle_status <> 'SUSPENDED' OR status_reason IS NOT NULL),
  CHECK ((signature IS NULL) = (signature_alg IS NULL)),
  CHECK ((bundle_object_key IS NULL) = (bundle_digest IS NULL))
);
CREATE TRIGGER artifacts_immutable
  BEFORE UPDATE OF id, tenant_id, kind, family, version, version_major, version_minor,
    version_patch, version_pre, published_by, manifest_schema_version, manifest,
    manifest_digest, oci_image_digest, bundle_bucket, bundle_object_key, bundle_digest,
    bundle_size_bytes, signature_alg, signature, signer_identity, sbom_object_key, sealed_at
  OR DELETE ON artifacts
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

The `DEFAULT 'DRAFT'` is the default for the executable kinds only; an insert of a `dataset` or `annotation` artifact MUST supply `SEALED` explicitly and a row that omits it fails `artifacts_lifecycle_status_per_kind` loudly, which is the intended behaviour.

**MOS-STORE-255** `Tool`, `ToolVersion`, `Workflow`, `WorkflowVersion`, `Agent` and `AgentVersion` have **no dedicated tables in 0.1–0.3**; they are `artifacts` rows of kind `tool`, `workflow` and `agent`, and dedicated tables, if needed, arrive with the agentic layer in 0.4 (Chapter 11). Three empty `*_versions` table pairs in the 0.1 schema would be three migrations of dead weight and three more places to forget `tenant_id`.

#### 12.9.1 Capabilities and coded concepts

```sql
CREATE TABLE capabilities (                          -- global
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- The slug is the `capability_id` of Chapter 6 MOS-REG-041: immutable, never reused for a
  -- different clinical meaning, superseded rather than renamed. The pattern mirrors that
  -- requirement and MUST NOT diverge.
  slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z][a-z0-9_]{2,47}$'),
  revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),   -- MOS-REG-048
  display_name text NOT NULL, clinical_domain text NOT NULL,
  -- The column Chapter 6 calls `capability.kind`. Chapter 6 is normative for this value set.
  output_kind text NOT NULL
    CHECK (output_kind IN ('segmentation','detection','measurement','classification')),
  status text NOT NULL DEFAULT 'reserved'
    CHECK (status IN ('reserved','supported','deprecated')),   -- MOS-REG-046 gates F1 on this
  superseded_by uuid REFERENCES capabilities(id),
  acceptance_criteria_id uuid,                                 -- MOS-REG-049
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE capability_concepts (                   -- global
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  capability_id uuid NOT NULL REFERENCES capabilities(id),
  role text NOT NULL CHECK (role IN ('finding','segmented_property','measurement',
                                     'anatomic_region','laterality')),
  -- `measurement_id` is Chapter 6's `capability.measurements[].id` (MOS-REG-045/047). It is
  -- required for measurements and absent otherwise: one capability declares several
  -- measurements that legitimately share one coded concept (three LAA percentages under
  -- SCT 87433001), so the concept code alone cannot identify a measurement.
  measurement_id text CHECK (measurement_id ~ '^[a-z][a-z0-9_]{2,63}$'),
  -- `99MEDICALOS` is the private-scheme designator of ch. 2 §2.8, which owns the wire
  -- form; `99MEDOS` is superseded and MUST NOT appear anywhere in this schema.
  coding_scheme text NOT NULL
    CHECK (coding_scheme IN ('SCT','RADLEX','DCM','LN','99MEDICALOS')),
  code_value text NOT NULL, code_meaning text NOT NULL,
  code_scheme_version text NOT NULL,                 -- MOS-REG-043; never empty
  -- The narrative lexicon. `A_lbl` (ch. 11 MOS-AGENT-077) is every concept display in the
  -- `Result` plus this array; it is here and not in a "chapter 9 code dictionary", which
  -- does not exist and MUST NOT be created (MOS-REG-042 forbids a second code table).
  -- Clinical synonyms only: lower-case, no PHI, no free text.
  narrative_synonyms text[] NOT NULL DEFAULT '{}',
  ucum_unit ucum_unit, loinc_code text,
  computation_geometry text CHECK (computation_geometry = 'source'),  -- MOS-REG-045
  parameters jsonb NOT NULL DEFAULT '{}',            -- MOS-REG-047 measurements[].parameters
  CONSTRAINT capability_concepts_uk
    UNIQUE NULLS NOT DISTINCT (capability_id, role, coding_scheme, code_value, measurement_id),
  CONSTRAINT capability_concepts_measurement_uk UNIQUE (capability_id, measurement_id),
  CHECK ((role = 'measurement') = (measurement_id IS NOT NULL)),
  CHECK ((role = 'measurement') = (ucum_unit IS NOT NULL)),
  CHECK ((role = 'measurement') = (computation_geometry IS NOT NULL)),
  CHECK (length(code_scheme_version) > 0)
);
```

**MOS-STORE-256** Every finding and measurement a service may emit MUST have a `capability_concepts` row, and a `ResultBundle` whose concept has no matching row MUST be rejected at ingest — the platform MUST NOT write a DICOM SR containing a concept it cannot code. This makes "generate the JSON result and the coded SR from one dictionary" a database constraint rather than a convention. `capability_concepts` is that single dictionary: the DICOM SR/SEG writer (Chapter 4), the API response body, any later FHIR projection and the agentic layer's finding-label lexicon MUST all read from it, and no second code table exists anywhere in the platform (`MOS-REG-042`, ch. 6). `narrative_synonyms[]` is on this table for that last consumer. `MOS-AGENT-077` (ch. 11) defines `A_lbl` as every concept display in the `Result` plus that concept's registered `narrative_synonyms[]`, attributing them to a "chapter 9 code dictionary" that does not exist; the synonyms live here, Chapter 6 owns their content exactly as it owns every other column of this table, and `A_lbl` MUST resolve against `code_meaning ∪ narrative_synonyms` — without which `hallucination_rate` has no authoritative token set and the narrative tool either over-rejects or is not implemented. The columns are the normalised form of Chapter 6's `capability.primary_code`, `additional_codes` and `measurements[]`: `role = 'finding'` or `'segmented_property'` carries the primary and additional codes, `role = 'measurement'` carries one row per `measurements[]` entry with its `measurement_id`, `ucum_unit`, `computation_geometry` and `parameters`. The column Chapter 6 calls `capability.kind` is `capabilities.output_kind` here, and `capabilities.status` is the column `MOS-REG-046` gates resolution on; Chapter 6 is normative for both value sets and this chapter MUST NOT diverge from them. A change to any value under `capability_concepts.parameters` MUST increment `capabilities.revision` and invalidate `acceptance_criteria_id` until a new `EvaluationRun` is recorded (`MOS-REG-048`).

**MOS-STORE-257** `coding_scheme = '99MEDICALOS'` is the private coding scheme designator for concepts with no standard code; it is permitted, MUST carry a stable `code_value` and readable `code_meaning`, and its use MUST be visible in the validation report. The designator is Chapter 2's — its `emphysema_laa` worked example and its acceptance check 37 both write `99MEDICALOS`, and ch. 11 §11.3.1's `99[A-Z0-9_]{1,14}` pattern admits it — and it is fixed here once: the earlier `99MEDOS` spelling is superseded and MUST NOT appear in a CHECK, a seed row, a manifest or generated code. `code_scheme_version` is the pinned terminology release the code was validated against (`MOS-REG-043`: a code without a pinned scheme version MUST be rejected); for `99MEDICALOS` it is the version of the platform dictionary that defines the private code. CI MUST validate every standard code against the bundled subset for its `code_scheme_version` and fail the build on an unknown or inactive concept (`MOS-REG-044`). `loinc_code` exists so a later FHIR `Observation.code` projection is a join, not a rebuild.

Seed rows for the 0.1 capabilities — real codes, not placeholders. The measurement ids and units are Chapter 6's starter list (`MOS-REG-046`, `MOS-REG-047`) and MUST NOT diverge from it:

| capability | role | measurement_id | scheme | code | meaning | unit |
|---|---|---|---|---|---|---|
| `pleural_effusion` | finding, segmented_property | — | SCT | 60046008 | Pleural effusion | |
| `pleural_effusion` | measurement | `effusion_volume_left` | SCT | 118565006 | Volume | `mL` |
| `pleural_effusion` | measurement | `effusion_volume_right` | SCT | 118565006 | Volume | `mL` |
| `pleural_effusion` | measurement | `effusion_max_depth` | 99MEDICALOS | EFFUSION_MAX_DEPTH | Maximum effusion depth | `mm` |
| `pleural_effusion` | anatomic_region | — | SCT | 181608008 | Pleural cavity structure | |
| `pleural_effusion` | laterality | — | SCT | 7771000 / 24028007 | Left / Right | |
| `lung_segmentation` | segmented_property | — | SCT | 39607008 | Lung structure | |
| `lung_segmentation` | measurement | `lung_volume_total` | SCT | 118565006 | Volume | `mL` |
| `lung_segmentation` | measurement | `lung_volume_left` | SCT | 118565006 | Volume | `mL` |
| `lung_segmentation` | measurement | `lung_volume_right` | SCT | 118565006 | Volume | `mL` |
| `emphysema_laa` | finding | — | SCT | 87433001 | Pulmonary emphysema | |
| `emphysema_laa` | measurement | `laa_950_percent_total` | 99MEDICALOS | LAA950 | Percent lung volume below −950 HU | `%` |
| `emphysema_laa` | measurement | `laa_950_percent_left` | 99MEDICALOS | LAA950 | Percent lung volume below −950 HU | `%` |
| `emphysema_laa` | measurement | `laa_950_percent_right` | 99MEDICALOS | LAA950 | Percent lung volume below −950 HU | `%` |
| `emphysema_laa` | measurement | `hu_percentile_15` | 99MEDICALOS | HU_P15 | 15th percentile of lung attenuation | `[hnsf'U]` |
| `emphysema_laa` | anatomic_region | — | SCT | 39607008 | Lung structure | |

Every `SCT` row above carries `code_scheme_version = 'SNOMED CT International Edition 2026-01-31'` and every `99MEDICALOS` row the platform dictionary version; the seeds are subject to the CI check of `MOS-REG-044` and MUST NOT be treated as verified merely because they are written here. Every `emphysema_laa` measurement row carries the deterministic computation parameters of `MOS-REG-047` in `parameters` (`threshold_hu: -950`, `mask_source_capability: lung_segmentation`, and the rest of that block verbatim) — the platform serves this capability from a deterministic measurement, not a learned model, so those numbers are clinically load-bearing and versioned like weights.

#### 12.9.2 Services, model versions and their catalogue tables

These tables follow the standard conventions; the columns that carry a contract are listed, and the constraints that enforce a contract are given as SQL.

| Table | Key | Columns |
|---|---|---|
| `services` | `UNIQUE NULLS NOT DISTINCT (tenant_id, slug)` | `slug`, `display_name`, `owner_org`, `contact_email`, `visibility` ∈ `private,org,public` (Ch. 6 §6.3 `artifact_family`; mirrors it and MUST NOT diverge), `deleted_at` |
| `service_versions` | `UNIQUE (service_id, version)` | `artifact_id`, `execution_mode` ∈ `native,sealed` (Ch. 2 `MOS-SVC-118`; `sealed` MUST NOT be claimed available before 0.3.0), `oci_image_digest`, `lifecycle_status` ∈ `DRAFT,REGISTERED,VALIDATING,VALIDATED,APPROVED,DEPRECATED,SUSPENDED,RECALLED` (Ch. 6 `MOS-REG-020` is normative; this column mirrors it and MUST NOT diverge), `status_reason`, `deprecated_at`, `eol_at`, `recalled_at`, `recall_reason`, and the sealed `clinical` block of Ch. 9 §9.2 rendered column-for-column: `intended_use jsonb`, `indications jsonb`, `contraindications jsonb`, `target_population jsonb`, `known_limitations jsonb`, `not_validated_for jsonb`, `known_failure_modes jsonb`, `input_constraints jsonb`, `training_population jsonb` (nullable, `MOS-SAFE-016`), `risk_classification jsonb`, `regulatory_status jsonb`, `legal_manufacturer jsonb`, `clinical_evidence jsonb` (nullable), `intended_use_digest sha256_digest` (`MOS-SAFE-021`), plus the machine applicability gate `applicability jsonb` (Ch. 3 `MOS-DATA-067`) |
| `service_version_capabilities` | `(service_version_id, capability_id)` | `deterministic boolean`, `operating_points jsonb` — the `capabilities[].operating_points` of Ch. 2 `MOS-SVC-020`, an array of `{id, score_threshold}`, empty when and only when `deterministic` — and `evidence_run_id` |
| `series_selectors` | `UNIQUE (service_version_id, name)` | `name` matching `^[a-z][a-z0-9_]{0,31}$`, `cardinality` ∈ `exactly_one,one_or_more,optional_one` (Ch. 3 `MOS-DATA-068` is normative; mirrors it and MUST NOT diverge), `max_count integer NOT NULL DEFAULT 8`, `match_spec jsonb`, `rank jsonb`, `ambiguity jsonb` |
| `models` | `UNIQUE NULLS NOT DISTINCT (tenant_id, slug)` | `display_name`, `task` ∈ `segmentation,detection,classification,regression` — a catalogue label only, never consulted by resolution, which reads `capabilities.output_kind` |
| `preprocessing_specs` | `UNIQUE (spec_digest)` | `artifact_id`, `version semver`, `spec jsonb`, `spec_digest`, `implementation_package`, `golden_fixture_object_key`, `golden_input_digest` (Ch. 4 `golden_fixture.sha256`), `golden_output_tensor_digest` (Ch. 4 `golden_fixture.output_tensor_sha256`); immutable |
| `model_versions` | `UNIQUE (model_id, version)` | `artifact_id`, `preprocessing_spec_id`, `weights_availability` ∈ `platform_managed,vendor_sealed` (Ch. 6 `MOS-REG-039`), `weights_digest`, `backend` ∈ `onnxruntime,pytorch,tensorrt,python`, `backend_build jsonb`, `input_tensor jsonb`, `output_tensor jsonb`, `output_kind` ∈ `segmentation_logits,segmentation_binary,segmentation_fractional,detection_boxes,scalar` (Ch. 6 `MOS-REG-033` is normative; mirrors it and MUST NOT diverge), `operating_point jsonb`, `label_map jsonb`, `applicability_envelope jsonb`, `engineering_bars jsonb`, `not_validated_for text[]`, `known_failure_modes text[]` (each ≥ 1 entry, `MOS-REG-032`), `derived_from uuid` and `conversion_equivalence jsonb` (`MOS-REG-103`/`MOS-REG-104`), `lifecycle_status` (same eight values and the same CHECK as `service_versions`, Ch. 6 `MOS-REG-020`), `status_reason`, `primary_evaluation_run_id` (Ch. 6 spells this `spec.evaluation_run_id`, `MOS-REG-031`; it is the same value) |
| `capability_pins` | `UNIQUE (tenant_id, capability_id, environment)` | `service_id` (the service **family** the pin names), `version_range` (e.g. `'>=3.2 <4'`), `created_by` — this is Chapter 6's `TenantPin`, read by resolution rank key P2 and covered by the changelog obligation of `MOS-REG-010` |

```sql
-- The constraints that are themselves contracts.
ALTER TABLE service_versions
  -- The `clinical` block is Chapter 9 §9.2. Every cardinality below mirrors MOS-SAFE-015
  -- and the field table of §9.2 and MUST NOT diverge from them.
  ADD CHECK (jsonb_array_length(indications) >= 1),
  ADD CHECK (jsonb_typeof(contraindications) = 'array'),
  ADD CHECK (jsonb_array_length(known_limitations) >= 1),
  ADD CHECK (jsonb_array_length(not_validated_for) >= 1),
  ADD CHECK (jsonb_array_length(known_failure_modes) >= 1),
  ADD CHECK (jsonb_array_length(regulatory_status) >= 1),
  ADD CHECK (intended_use ?& array['statement','intended_user','intended_setting',
                                   'reading_paradigm','autonomy','output_kinds']),
  ADD CHECK (intended_use ->> 'autonomy' = 'assistive'),              -- MOS-SAFE-011
  ADD CHECK (target_population ?& array['age_min_years','age_max_years','sex',
                                        'body_part','pregnancy']),
  ADD CHECK (risk_classification ? 'imdrf'),
  -- MOS-SAFE-020 (ch. 9, the owner) and MOS-REG-027 (ch. 6) together: the seven identity
  -- members plus the two the DICOM Enhanced General Equipment module is written from.
  ADD CHECK (legal_manufacturer ?& array['name','legal_form','address','country',
    'contact_email','srn_or_registration_id','signing_key_id',
    'device_serial_number','software_versions']),
  ADD CHECK (length(legal_manufacturer ->> 'name') BETWEEN 1 AND 64),  -- MOS-SAFE-020
  ADD CHECK (applicability ? 'modality_in'),                           -- MOS-DATA-067
  ADD CHECK (lifecycle_status IN ('DRAFT','REGISTERED','VALIDATING','VALIDATED',
                                  'APPROVED','DEPRECATED','SUSPENDED','RECALLED')),
  ADD CHECK ((lifecycle_status = 'RECALLED') = (recalled_at IS NOT NULL)),
  ADD CHECK (lifecycle_status <> 'RECALLED' OR recall_reason IS NOT NULL),
  ADD CHECK (lifecycle_status <> 'SUSPENDED' OR status_reason IS NOT NULL),
  ADD CHECK ((lifecycle_status = 'DEPRECATED') <= (deprecated_at IS NOT NULL));

ALTER TABLE service_version_capabilities
  -- MOS-SVC-020: the set is empty when and only when the capability is deterministic.
  ADD CHECK (deterministic = (jsonb_array_length(operating_points) = 0)),
  -- MOS-SVC-089: each declared operating point is {id, score_threshold}.
  ADD CHECK (NOT jsonb_path_exists(operating_points,
    '$[*] ? (!exists(@.id) || !exists(@.score_threshold))'));

ALTER TABLE series_selectors
  -- MOS-DATA-073: `exactly_one` with a `max_count` is a registration error.
  ADD CHECK (cardinality = 'one_or_more' OR max_count = 1),
  ALTER COLUMN rank SET DEFAULT
    '[{"key":"slice_thickness_mm","order":"asc"},
      {"key":"instance_count","order":"desc"},
      {"key":"series_instance_uid","order":"asc"}]'::jsonb;

ALTER TABLE preprocessing_specs
  -- The top-level key set is Chapter 4 MOS-IMG-049 exactly; a spec that validates against
  -- that schema MUST pass this CHECK, and this list MUST NOT diverge from it.
  ADD CHECK (spec ?& array['schema_version','id','version','model_id','model_version',
    'canonical_geometry','orientation_target','axis_order','target_spacing_mm',
    'image_interpolator','label_interpolator','probability_interpolator','clip',
    'normalisation','foreground_crop','patch','tta','io','inverse','backend',
    'golden_fixture']);

ALTER TABLE model_versions
  ADD CHECK (input_tensor ?& array['name','shape','dtype','layout','orientation']),
  -- MOS-REG-033: a logits or fractional output MUST carry an operating point.
  ADD CHECK (output_kind NOT IN ('segmentation_logits','segmentation_fractional')
             OR operating_point IS NOT NULL),
  -- MOS-REG-039: sealed weights are declared by their absence, not by a null digest column
  -- that also means "not yet uploaded".
  ADD CHECK ((weights_availability = 'vendor_sealed') = (weights_digest IS NULL)),
  ADD CHECK (cardinality(not_validated_for) >= 1),
  ADD CHECK (cardinality(known_failure_modes) >= 1),
  -- MOS-REG-103: a converted backend is a new version with its own evidence, so no two
  -- model versions may cite the same evaluation run.
  ADD CONSTRAINT model_versions_eval_run_uk UNIQUE (primary_evaluation_run_id),
  ADD CONSTRAINT model_versions_derived_from_fk
    FOREIGN KEY (derived_from) REFERENCES model_versions(id),
  ADD CHECK (backend <> 'tensorrt'
             OR backend_build ?& array['gpu_architectures','cuda','engine_version',
                                       'driver_min']);

-- The semantic definition of this table — column names, enum values, the composite
-- unique key and the partial unique index — is Chapter 6 §6.8 (MOS-REG-072 to
-- MOS-REG-083). This is its physical form under MOS-STORE-208/209/210.
CREATE TABLE deployments (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  service_version_id uuid NOT NULL REFERENCES service_versions(id),
  capability_id uuid NOT NULL REFERENCES capabilities(id),
  environment text NOT NULL CHECK (environment IN ('dev','staging','production')),
  role text NOT NULL CHECK (role IN ('ACTIVE','CANARY','SHADOW','STANDBY')),
  state text NOT NULL DEFAULT 'PENDING' CHECK (state IN
    ('PENDING','VERIFYING','SERVING','SUSPENDED','DRAINING','RETIRED')),
  traffic_permille integer NOT NULL DEFAULT 0
    CHECK (traffic_permille BETWEEN 0 AND 1000),
  clinical_use_mode text NOT NULL CHECK (clinical_use_mode IN ('research_only','clinical')),
  -- Two classes, not three. MOS-REG-076a (ch. 6) delegates the class semantics, the
  -- eviction rules and the footprint arithmetic to ch. 13 §13.10.3, whose residency-class
  -- table defines `resident` and `on_demand` only, so `evictable` is a storable value
  -- with no behaviour and `tritond` has no rule for it. It is removed here; ch. 6's
  -- CHECK (06-registries.md) MUST drop it in the same change.
  residency text NOT NULL DEFAULT 'on_demand'
    CHECK (residency IN ('resident','on_demand')),
  pin_range text NOT NULL DEFAULT '*',
  operating_point_id text,                 -- MOS-SVC-112: one declared point per capability
  promotion_policy jsonb,
  verification_ref text,
  review_mode text NOT NULL DEFAULT 'optional' CHECK (review_mode IN
    ('off','optional','mandatory_post_publication','mandatory_pre_publication')),
  review_sla_hours integer,
  emit_verified_sr_on_accept boolean NOT NULL DEFAULT false,
  state_reason text,
  acceptance_run_id uuid, validation_report_id uuid,
  approved_by uuid, approved_at timestamptz,
  activated_at timestamptz, retired_at timestamptz,
  created_by uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT deployments_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT deployments_uk UNIQUE (tenant_id, environment, capability_id, service_version_id),
  CONSTRAINT deployments_approver_fk FOREIGN KEY (tenant_id, approved_by)
    REFERENCES users (tenant_id, id),
  CONSTRAINT deployments_serving_verified
    CHECK (state <> 'SERVING' OR verification_ref IS NOT NULL),
  CONSTRAINT deployments_suspend_reason
    CHECK (state <> 'SUSPENDED' OR state_reason IS NOT NULL),
  -- MOS-SAFE-061: pre-publication review gating is reserved and MUST be refused in 0.1-0.4.
  CONSTRAINT deployments_review_mode_reserved
    CHECK (review_mode <> 'mandatory_pre_publication'),
  CONSTRAINT deployments_clinical_gate CHECK (
    clinical_use_mode = 'research_only'
    OR (approved_by IS NOT NULL AND approved_at IS NOT NULL
        AND acceptance_run_id IS NOT NULL AND validation_report_id IS NOT NULL))
);
CREATE UNIQUE INDEX deployments_one_active_per_slot
  ON deployments (tenant_id, environment, capability_id)
  WHERE role = 'ACTIVE' AND state = 'SERVING';

-- Mutually-referencing and forward FKs, added after both tables exist. All are nullable,
-- so no deferrable constraint is needed at insert time.
ALTER TABLE model_versions ADD CONSTRAINT model_versions_primary_eval_fk
  FOREIGN KEY (primary_evaluation_run_id) REFERENCES evaluation_runs(id);
ALTER TABLE service_version_capabilities ADD CONSTRAINT svc_cap_evidence_fk
  FOREIGN KEY (evidence_run_id) REFERENCES evaluation_runs(id);
ALTER TABLE capabilities ADD CONSTRAINT capabilities_acceptance_fk
  FOREIGN KEY (acceptance_criteria_id) REFERENCES acceptance_criteria(id);
ALTER TABLE deployments ADD CONSTRAINT deployments_acceptance_fk
  FOREIGN KEY (acceptance_run_id) REFERENCES evaluation_runs(id);
ALTER TABLE deployments ADD CONSTRAINT deployments_report_fk
  FOREIGN KEY (validation_report_id) REFERENCES validation_reports(id);
ALTER TABLE studies ADD CONSTRAINT studies_deid_policy_fk
  FOREIGN KEY (tenant_id, deid_policy_id) REFERENCES deid_policies (tenant_id, id);
ALTER TABLE tenants ADD CONSTRAINT tenants_deid_policy_fk
  FOREIGN KEY (default_deid_policy_id) REFERENCES deid_policies(id);
```

**MOS-STORE-258** `series_selectors.rank` is an ordered array of `{key, order}` entries, optionally carrying `tolerance` (deterministic bucketing) and, when `order` is `preference`, a `preference` list; `order` ∈ {`asc`, `desc`, `preference`}. Chapter 3 `MOS-DATA-071` is normative for the ranking algorithm and this column mirrors its member names exactly and MUST NOT diverge from them. Every `key` MUST be drawn from the closed `MOS-DATA-059` field set projected onto `series` (§12.7); it MUST NOT contain SQL, the triage code MUST NOT interpolate it into a query, and the last entry MUST be `{"key":"series_instance_uid","order":"asc"}` (`MOS-DATA-072`), which makes the ranking total and selection therefore deterministic between any two candidate series. A selector whose last key is anything else, or whose `match_spec` contains a key outside the `MOS-DATA-069` vocabulary, MUST be refused at registration (`MOS-DATA-073`). The column is `match_spec` rather than `match` only because `MATCH` is a SQL keyword; it stores Chapter 3's `match` block verbatim.

**MOS-STORE-259** `lifecycle_status` describes a version's maturity only and MUST NOT decide whether it receives traffic — that is `deployments.role` and `deployments.state` together (Chapter 6 `MOS-REG-072`). A version may be `APPROVED` and deployed nowhere, or `SUSPENDED` while a deployment is in `DRAINING` (`MOS-REG-021`, `MOS-REG-073`). The values `STAGING`, `DEPLOYED` and `PRODUCTION` MUST NOT appear in this column (`MOS-REG-038`); on the dispatch path it is read only by resolution filter F3.

**MOS-STORE-260** `legal_manufacturer` is `NOT NULL` and structurally checked. Chapter 9 `MOS-SAFE-020` owns its member set — `name`, `legal_form`, `address`, `country`, `contact_email`, `srn_or_registration_id`, `signing_key_id` — and this column mirrors it and MUST NOT diverge; `name` is bounded at 64 characters because it is written verbatim into DICOM `Manufacturer` (0008,0070) and MUST NOT be truncated. Chapter 6 `MOS-REG-027` additionally requires `device_serial_number` and `software_versions`, which are the remaining sources of the SEG Enhanced General Equipment Type 1 tags, so the CHECK is the union of the two lists: a service version missing any of the nine cannot produce a conformant SEG and MUST NOT be registerable. `signing_key_id` MUST identify the key that signed the `ServiceVersion` and the platform MUST refuse a mismatch at registration.

**MOS-STORE-261** `deployments_clinical_gate` is a database-enforced clinical gate: a deployment cannot be `clinical` without a named human approver, an acceptance evaluation run and a validation report, and the application cannot bypass it because the database checks it on every write. Chapter 7 owns what the report contains; Chapter 9 owns who may approve.

**MOS-STORE-262** `deployments` is the sole owner of liveness (Chapter 6, `MOS-REG-072`). The slot is `(tenant_id, environment, capability_id)`; canary and blue/green are two rows **in one slot** at two `role` values, which is why the unique key is `(tenant_id, environment, capability_id, service_version_id)` and why the partial unique index admits at most one `role = 'ACTIVE' AND state = 'SERVING'` row per slot (`MOS-REG-074`). `role` and `state` are orthogonal and both are required (`MOS-REG-073`): `role` says what traffic the row should get, `state` whether it may get any. Canary weight is `traffic_permille`, an absolute share of 1000 held by the `CANARY` row alone — never a percentage and never a weight that must sum across rows (`MOS-REG-079`). `operating_point_id` names the one operating point this deployment applies for its capability and MUST be one the `ServiceVersion` manifest declares in `service_version_capabilities.operating_points` (`MOS-SVC-112`); changing it is a new deployment revision, never an in-place edit of produced results. Chapter 6 is normative for every value of `role`, `state` and `residency`; per `MOS-STORE-201` this table is structure only, and a divergence between the two chapters is fixed here, not there.

**MOS-STORE-263** `model_versions` MUST NOT have a free-form metrics column and the schema provides none (`MOS-REG-031`): declared performance is reached only through `primary_evaluation_run_id` and, for further cohorts, through `evaluation_runs` rows referencing it. A number presented as platform fact must be traceable to the dataset version, split, annotation set and threshold that produced it, or it must not be presentable.

**MOS-STORE-264** The mutually-referencing foreign keys above are added by `ALTER TABLE` later in the same migration file; all are nullable, so no deferrable constraint is required.

**MOS-STORE-265** `backend_build` MUST record `gpu_architectures`, `cuda`, `engine_version` and `driver_min` for a TensorRT backend — the member names are Chapter 6's `spec.runtime` block (§6.5) and MUST NOT diverge from it — because a serialised plan is bound to a GPU architecture and runtime version: the same declared model version on a different card is numerically a different device. A backend conversion is therefore a new `model_versions` row with `derived_from` set, its own evaluation run, a recorded `conversion_equivalence`, and a re-pass of the capability's `AcceptanceCriteria` (Chapter 6 `MOS-REG-102` to `MOS-REG-104`; Chapter 7 is normative for the evidence).

### 12.10 Execution tables

#### 12.10.1 The dead-letter queue

`MOS-EXEC-085` (ch. 5) lists six execution-plane tables and this chapter rendered five of them. The sixth is the one a failure ends up in, which makes its absence the least affordable of the six: `MOS-EXEC-085` requires it to carry `tenant_id` and to be under row-level security, and a table that does not exist satisfies neither. `MOS-EXEC-066` is a driver-level contract with two implementations — this table in queue driver 1, `<topic>.dlq` in driver 2 — written by the same code path, so the columns below are also the shape of the driver-2 envelope.

**MOS-STORE-360** Chapter 5 `MOS-EXEC-066` is normative for this table's field names and contents, `MOS-EXEC-067` for its policy and `MOS-EXEC-085` for its tenancy; this table mirrors all three and MUST NOT diverge. One rendering change is made and it is the chapter-wide one: ch. 5 writes `dlq_id`, which is `id` here under `MOS-STORE-208`, exactly as ch. 5's `jobs(job_id)` is `jobs(id)` in §12.10. Ch. 5's `service_id` is the surrogate `services.id` here under `MOS-STORE-215`; the dotted service slug that appears in ch. 5's example value and in the `medicalos_job_dead_lettered_total` metric label is served by joining `services.slug`.

```sql
CREATE TABLE job_dead_letter (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),           -- ch. 5 `dlq_id`
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,  -- MOS-EXEC-085
  job_id uuid NOT NULL,
  service_id uuid NOT NULL REFERENCES services(id),
  -- ch. 5 MOS-EXEC-066: the parent queue or topic the entry came from, not the DLQ's own
  -- name. Ch. 5 §5.8 owns the topic grammar `medicalos.svc.<service_id>.work`.
  source text NOT NULL CHECK (
    source = 'job_queue'
    OR source ~ '^medicalos\.svc\.[a-z0-9][a-z0-9_.-]{0,62}\.work$'),
  -- The retry BUDGET is deliberately not duplicated here, for the same reason
  -- `job_queue.claim_count` does not duplicate it: the ceiling is `jobs.max_attempts`
  -- (MOS-EXEC-063), and a second copy is a second thing to drift.
  attempts smallint NOT NULL CHECK (attempts >= 1),
  failure jsonb NOT NULL,      -- {failure_class, failure_code, failure_detail, step_key}
  attempt_log jsonb NOT NULL,  -- [{attempt, runner_id, step_key, failure_class, ...}]
  envelope jsonb NOT NULL,     -- the last dispatch envelope, verbatim (MOS-EXEC-075)
  dead_at timestamptz NOT NULL DEFAULT now(),
  requeued_at timestamptz,
  requeued_by uuid,
  created_at timestamptz NOT NULL DEFAULT now(),   -- MOS-STORE-210; coincides with dead_at
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT job_dead_letter_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT job_dead_letter_job_uk UNIQUE (job_id),        -- MOS-EXEC-066
  CONSTRAINT job_dead_letter_job_fk FOREIGN KEY (tenant_id, job_id)
    REFERENCES jobs (tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT job_dead_letter_requeued_by_fk FOREIGN KEY (tenant_id, requeued_by)
    REFERENCES users (tenant_id, id),
  -- `failure.failure_class` mirrors the closed twelve-value enum of MOS-EXEC-017 /
  -- §5.3.2 (ch. 5) value-for-value, the same set `jobs.failure_class` carries
  -- (MOS-STORE-266), and MUST NOT diverge. `load_shed` is present in the enum and
  -- unreachable here: MOS-EXEC-064 forbids a shed from consuming budget, and
  -- MOS-EXEC-065 converts an exhausted shed to `service_unavailable` /
  -- `capacity_exhausted` before dead-lettering it.
  CONSTRAINT job_dead_letter_failure_shape CHECK (
    failure ?& array['failure_class','failure_code']
    AND failure ->> 'failure_class' IN
      ('transient_infrastructure','gateway_unavailable','service_unavailable',
       'service_crashed','lease_expired','dicom_store_failed','load_shed',
       'invalid_result_bundle','preprocessing_selftest_failed',
       'dicom_write_failed','deadline_exceeded','internal')),
  -- Ch. 5 acceptance check 19: five failures produce an `attempt_log` with five entries.
  -- `>=` rather than `=` because a load-shed lap is logged without consuming budget
  -- (MOS-EXEC-064), so the log may legitimately be longer than `attempts`.
  CONSTRAINT job_dead_letter_attempt_log_shape CHECK (
    jsonb_typeof(attempt_log) = 'array'
    AND jsonb_array_length(attempt_log) >= attempts),
  CHECK ((requeued_at IS NULL) = (requeued_by IS NULL))
);
CREATE INDEX job_dead_letter_open_idx ON job_dead_letter (tenant_id, dead_at DESC)
  WHERE requeued_at IS NULL;
CREATE INDEX job_dead_letter_service_idx
  ON job_dead_letter (tenant_id, service_id, dead_at DESC);
CREATE INDEX job_dead_letter_expiry_idx ON job_dead_letter (dead_at);

ALTER TABLE job_dead_letter OWNER TO medicalos_owner;
ALTER TABLE job_dead_letter ENABLE ROW LEVEL SECURITY;   -- MOS-EXEC-085, MOS-STORE-223
ALTER TABLE job_dead_letter FORCE ROW LEVEL SECURITY;
CREATE POLICY job_dead_letter_tenant_isolation ON job_dead_letter
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());
CREATE POLICY job_dead_letter_break_glass ON job_dead_letter FOR SELECT TO medicalos_app
  USING (current_setting('medicalos.break_glass', true) = 'on');

CREATE TRIGGER job_dead_letter_touch BEFORE UPDATE ON job_dead_letter
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER job_dead_letter_sealed BEFORE UPDATE ON job_dead_letter
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','job_id','service_id',
    'source','attempts','failure','attempt_log','envelope','dead_at');

GRANT SELECT, INSERT, UPDATE, DELETE ON job_dead_letter TO medicalos_app;
GRANT SELECT ON job_dead_letter TO medicalos_readonly;
```

**MOS-STORE-361** `requeued_at` and `requeued_by` are the only mutable columns, enforced by `job_dead_letter_sealed`. `MOS-EXEC-067` makes the reason structural rather than stylistic: DLQ entries MUST NOT be retried automatically, ever, and the only exit is an operator retry (ch. 5 T13, `POST /api/v1/jobs/{job_id}/retry`, permission `job.retry`), which is audited. A forensic record whose failure, attempt log and dispatch envelope can be edited after the fact is not a forensic record, and the `UNIQUE (job_id)` constraint means an operator retry that fails again updates the one row rather than accumulating a second.

**MOS-STORE-362** A row exists **only** for a job that reached `FAILED` on T11 or T12. `REJECTED` MUST NOT produce one: `MOS-EXEC-015` (ch. 5) says a rejected job is not retried, consumes no budget, is not dead-lettered and raises no alert, and ch. 5 acceptance check 18 asserts zero rows for a localizer-only study. This is the single most important thing about the table — a study the platform correctly declined to analyse must not appear in the same place as a study the platform failed to analyse, or the operator's first question every morning becomes unanswerable.

**MOS-STORE-363** Retention is 90 days from `dead_at`; entries are deleted, not archived, and the `jobs` row survives them (`MOS-EXEC-067`). `retention_policies.object_class` MUST therefore gain the value `'job_dead_letter'`, and §12.16.1's table the row `| `job_dead_letter` | Postgres | 90 days after `dead_at` | delete |`. The sweep is the ordinary tenant-iterating one of `MOS-STORE-333` and `MOS-STORE-225`; `job_dead_letter_expiry_idx` serves it. The reverse edge is asymmetric and intentional: deleting a DLQ row never touches its job, while deleting a job cascades its DLQ row away, because `job.delete` is a cleanup of the whole job and leaving an orphan post-mortem behind would be the worse outcome.

**MOS-STORE-364** `job_dead_letter.failure`, `attempt_log` and `envelope` MUST NOT contain PHI beyond DICOM UIDs (`MOS-EXEC-067`, ch. 5 `MOS-EXEC-079`). `MOS-STORE-357`'s schema-review rule and CI grep extend to all three columns, and §12.17's `Job` row gains `job_dead_letter` alongside `job_queue` and `job_outbox`, without which acceptance criterion 1 fails on the table's existence.


Job state machine, queue protocol, retry and failure semantics are Chapter 5's; this section defines the tables and the constraints that make those invariants structural.

```sql
CREATE TABLE jobs (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  -- The public job identifier the API emits and the agent tools accept (MOS-STORE-357).
  -- Chapter 10 owns the grammar: the literal prefix `job_` followed by a 26-character
  -- Crockford base32 ULID in upper case. This CHECK mirrors it and MUST NOT diverge.
  public_id text NOT NULL UNIQUE
    CHECK (public_id ~ '^job_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  state text NOT NULL DEFAULT 'CREATED' CHECK (state IN
    ('CREATED','QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED','REJECTED')),
                    -- mirrors the `job_state` enum of MOS-EXEC-001 / §5.1.2 (ch. 5)
                    -- value-for-value and MUST NOT diverge; see MOS-STORE-266
  phase text NOT NULL DEFAULT '',
  steps_total integer NOT NULL DEFAULT 0 CHECK (steps_total >= 0),
  steps_completed integer NOT NULL DEFAULT 0 CHECK (steps_completed >= 0),
  target_kind text NOT NULL CHECK (target_kind IN ('service_version','capability')),
  requested_service_version_id uuid,
  requested_capability_id uuid REFERENCES capabilities(id),
  requested_version_range text,
  resolved_service_version_id uuid,
  resolution_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  deployment_id uuid,
  clinical_use_mode text NOT NULL CHECK (clinical_use_mode IN ('research_only','clinical')),
  primary_study_id uuid NOT NULL,
  study_ids uuid[] NOT NULL CHECK (cardinality(study_ids) >= 1),
  idempotency_key text NOT NULL
    CHECK (idempotency_key ~ '^ik_[a-z2-7]{26}$'),           -- platform-derived, MOS-EXEC-053
  request_idempotency_key text
    CHECK (request_idempotency_key IS NULL
           OR request_idempotency_key ~ '^[A-Za-z0-9._~-]{1,255}$'),  -- client header echo only, MOS-API-023/029
  created_by_kind text NOT NULL CHECK (created_by_kind IN ('user','service_account','triage')),
  created_by_id text NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  queued_at timestamptz, started_at timestamptz, finished_at timestamptz,
  deadline_at timestamptz NOT NULL,
  attempt_deadline_at timestamptz,                            -- absolute, set at each claim, MOS-EXEC-070
  attempt integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 5
    CHECK (max_attempts BETWEEN 1 AND 10),                    -- per-class default and range, MOS-EXEC-063
  load_shed_count integer NOT NULL DEFAULT 0 CHECK (load_shed_count >= 0),  -- MOS-EXEC-064
  reject_reason_code text, reject_reason_detail text,
  failure_class text CHECK (failure_class IN
    ('transient_infrastructure','gateway_unavailable','service_unavailable','service_crashed',
     'lease_expired','dicom_store_failed','load_shed','invalid_result_bundle',
     'preprocessing_selftest_failed','dicom_write_failed',
     'deadline_exceeded','internal')),   -- ch. 5 §5.3.2 is normative; mirrors it, MUST NOT diverge
  failure_code text, failure_detail text,
  trace_id text, parent_job_id uuid, root_job_id uuid NOT NULL,
  shadow_of_job_id uuid,   -- the clinical job a SHADOW-deployment job mirrors, MOS-REG-082 (ch. 6)
  depth smallint NOT NULL DEFAULT 0 CHECK (depth BETWEEN 0 AND 3),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT jobs_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT jobs_idempotency_uk UNIQUE (tenant_id, idempotency_key),
  CONSTRAINT jobs_study_fk FOREIGN KEY (tenant_id, primary_study_id)
    REFERENCES studies (tenant_id, id) ON DELETE RESTRICT,
  -- Plain, not composite: `service_versions` is a MOS-STORE-220 dual-scope table and a
  -- tenant job routinely resolves to a platform-global (`tenant_id IS NULL`) version,
  -- which a composite key could never reference (MOS-STORE-217).
  CONSTRAINT jobs_resolved_fk FOREIGN KEY (resolved_service_version_id)
    REFERENCES service_versions (id),
  CONSTRAINT jobs_parent_fk FOREIGN KEY (tenant_id, parent_job_id)
    REFERENCES jobs (tenant_id, id),
  CONSTRAINT jobs_shadow_fk FOREIGN KEY (tenant_id, shadow_of_job_id)
    REFERENCES jobs (tenant_id, id),
  CHECK (study_ids[1] = primary_study_id),
  CHECK ((target_kind = 'service_version') = (requested_service_version_id IS NOT NULL)),
  CHECK ((target_kind = 'capability') = (requested_capability_id IS NOT NULL)),
  CHECK ((state IN ('COMPLETED','FAILED','CANCELLED','REJECTED')) = (finished_at IS NOT NULL)),
  CHECK (state <> 'REJECTED' OR reject_reason_code IS NOT NULL),
  CHECK (state <> 'FAILED' OR (failure_class IS NOT NULL AND failure_code IS NOT NULL)),
  CHECK (state IN ('CREATED','REJECTED') OR resolved_service_version_id IS NOT NULL),
  CHECK (shadow_of_job_id IS NULL OR shadow_of_job_id <> id),
  CHECK (steps_completed <= steps_total)
);
CREATE INDEX jobs_active_idx ON jobs (tenant_id, state, requested_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
CREATE INDEX jobs_deadline_idx ON jobs (deadline_at)
  WHERE state IN ('CREATED','QUEUED','RUNNING');
CREATE INDEX jobs_study_idx ON jobs (tenant_id, primary_study_id, requested_at DESC);

CREATE TRIGGER jobs_resolution_pinned BEFORE UPDATE ON jobs
  FOR EACH ROW WHEN (OLD.resolved_service_version_id IS NOT NULL)
  EXECUTE FUNCTION forbid_column_change('resolved_service_version_id','resolution_snapshot',
    'idempotency_key','request_idempotency_key','primary_study_id','study_ids',
    'clinical_use_mode','root_job_id','shadow_of_job_id');

CREATE TABLE execution_artifacts (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL, job_id uuid NOT NULL, produced_by_step_id uuid,
  kind text NOT NULL CHECK (kind IN ('canonical_volume','label_map','probability_map',
    'result_bundle','log_bundle')),
  media_type text NOT NULL CHECK (media_type IN ('application/x-nifti','application/x-nrrd',
    'application/json','application/gzip','application/octet-stream')),
  bucket text NOT NULL, object_key text NOT NULL,
  content_digest sha256_digest NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes > 0),
  dtype text, shape integer[],
  spacing_mm numeric(9,6)[] CHECK (spacing_mm IS NULL OR cardinality(spacing_mm) = 3),
  origin_mm numeric(9,4)[] CHECK (origin_mm IS NULL OR cardinality(origin_mm) = 3),
  direction numeric(9,6)[] CHECK (direction IS NULL OR cardinality(direction) = 9),
  frame_of_reference_uid dicom_uid, reference_series_id uuid,
  artifact_grid text NOT NULL CHECK (artifact_grid IN ('source','canonical','model')),
  expires_at timestamptz NOT NULL, blob_deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT execution_artifacts_uk UNIQUE (job_id, kind, content_digest),
  CONSTRAINT execution_artifacts_job_fk FOREIGN KEY (tenant_id, job_id)
    REFERENCES jobs (tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT execution_artifacts_series_fk FOREIGN KEY (tenant_id, reference_series_id)
    REFERENCES series (tenant_id, id),
  CHECK (kind NOT IN ('canonical_volume','label_map','probability_map')
    OR (spacing_mm IS NOT NULL AND origin_mm IS NOT NULL AND direction IS NOT NULL
        AND reference_series_id IS NOT NULL AND frame_of_reference_uid IS NOT NULL))
);
CREATE INDEX execution_artifacts_expiry_idx ON execution_artifacts (expires_at)
  WHERE blob_deleted_at IS NULL;

CREATE TABLE job_series (
  tenant_id uuid NOT NULL, job_id uuid NOT NULL, series_id uuid NOT NULL,
  decision text NOT NULL CHECK (decision IN ('selected','rejected')),
  selector_name text, rank integer, reason_code text, reason_detail text,
  PRIMARY KEY (job_id, series_id),
  CONSTRAINT job_series_job_fk FOREIGN KEY (tenant_id, job_id)
    REFERENCES jobs (tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT job_series_series_fk FOREIGN KEY (tenant_id, series_id)
    REFERENCES series (tenant_id, id) ON DELETE RESTRICT,
  CHECK ((decision = 'selected') = (selector_name IS NOT NULL AND rank IS NOT NULL)),
  CHECK ((decision = 'rejected') = (reason_code IS NOT NULL))
);
```

The remaining execution tables follow the standard conventions:

| Table | Key | Columns and constraints |
|---|---|---|
| `job_steps` | `UNIQUE (job_id, step_index)`, `UNIQUE (job_id, step_key)` | `step_key`, `phase`, `owner` ∈ `platform,service`, `status` ∈ `pending,running,succeeded,skipped,failed`, `skip_reason`, `attempt`, `timeout_s integer NOT NULL CHECK (timeout_s > 0)`, `input_artifact_ids uuid[]`, `output_artifact_ids uuid[]`, `detail jsonb NOT NULL DEFAULT '{}'::jsonb` (the sub-progress bag `MOS-EXEC-024` writes for the `service_invoke` row), `started_at`, `finished_at`, `duration_ms`, `error_code`, `error_detail`; `CHECK (status <> 'failed' OR error_code IS NOT NULL)`; `CHECK ((status = 'skipped') = (skip_reason IS NOT NULL))` (`MOS-EXEC-020`); FK to `jobs` `ON DELETE CASCADE`. The column names, the value set and the `steps_completed`/`steps_total` rollup trigger of §5.4.2 are Chapter 5's and are normative; that trigger reads `status` and `phase` |
| `job_events` | `id bigint identity PK`, `UNIQUE (job_id, seq)`, `UNIQUE (event_id)` | `event_id uuid NOT NULL DEFAULT gen_random_uuid()` — the envelope's delivery identity, Chapter 5 §5.2.3 — `event_type`, `from_state`, `to_state`, `phase`, `actor`, `payload jsonb`, `trace_id`, `occurred_at`; append-only trigger |
| `job_queue` | `job_id PK` | `queue` (`medicalos.svc.<service_id>.work`), `partition_key` (`<tenant_id>:<study_instance_uid>`), `priority smallint DEFAULT 100`, `available_at`, `lease_owner`, `lease_expires_at`, `claim_count`, `fence_token bigint NOT NULL DEFAULT 0`, `envelope jsonb NOT NULL`, `enqueued_at`; `CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))`; partial indexes `(queue, priority, available_at, enqueued_at) WHERE lease_owner IS NULL` and `(lease_expires_at) WHERE lease_owner IS NOT NULL`. `fence_token` (lease fencing, `MOS-EXEC-027`) and `envelope` (the dispatch payload, `MOS-EXEC-075`) carry protocol semantics defined only in ch. 5 §5.6.1; this chapter registers them by reference and MUST NOT rename them. The retry budget is **not** duplicated here — `claim_count` is compared against `jobs.max_attempts` (`MOS-EXEC-063`) |
| `job_outbox` | `id bigint identity PK` | created and used from 0.3 with queue driver 2 only (Chapter 5, `MOS-EXEC-039` and `MOS-EXEC-040`; spine §5) — **not** created in the 0.1 bootstrap migration: `topic`, `partition_key`, `envelope jsonb`, `published_at`, `attempts`, `last_error`; partial index `(id) WHERE published_at IS NULL` |

**MOS-STORE-266** The `jobs.state` CHECK is the complete public enum and MUST match the spine's state machine exactly; its value set mirrors `MOS-EXEC-001` and the `job_state` type of §5.1.2 (ch. 5) value-for-value and MUST NOT diverge. `POSTPROCESSING`, `WAITING` and every other internal stage MUST NOT be added, being values of the open `phase` column. The column is `text` with a `CHECK` rather than the native `job_state` type, because `MOS-STORE-209` binds every enumerated column in this schema; the type itself still exists, created by the execution-plane migration for ch. 5's `job_state_transition` table and `job_transition()` signature, and that function assigns to this column with an explicit `p_to::text` cast. The representation is this chapter's, the value set is Chapter 5's, and neither may be changed to suit the other. There is no float `progress` column and one MUST NOT be added — progress is `steps_completed` / `steps_total`, both derived from `job_steps`. `failure_class` mirrors the closed twelve-value enum of `MOS-EXEC-017` / §5.3.2 (ch. 5) and MUST NOT diverge from it; `output_implausible` is **not** a class — a plausibility failure is `failure_class = 'invalid_result_bundle'` with `failure_code = 'output_implausible'`, which is Chapter 7's reading (`MOS-EVID-110`, `MOS-EVID-111`) and the one Chapter 5 now carries, so the string appears in exactly one column of this schema; `failure_code` is the open code within that class, and neither value appears on the wire — the API maps the class to one of two RFC 9457 classes per `MOS-EXEC-016a` (ch. 5) and serialises `jobs.state` as the wire field `status` (`MOS-API-048`, ch. 10).

**MOS-STORE-267** `resolved_service_version_id` and `resolution_snapshot` are written at creation and immutable thereafter, enforced by `jobs_resolution_pinned`; a retry reuses them and MUST NOT re-resolve.

**MOS-STORE-357** `jobs.id` is the internal `uuid` and is never emitted; `jobs.public_id` is the external identifier, and it is the only job id that appears in a URL, an event envelope, a webhook body or an agent tool input. It exists because the `job_<ulid>` form Chapter 10 emits and Chapter 11 validates had no declared storage, so neither could be produced from a row and `result.get` failed input validation against a live job id. Chapter 10 is normative for the grammar — the prefix plus a case-fixed 26-character Crockford ULID — Chapter 11 §11.3.1 MUST mirror that grammar verbatim rather than restate it in the other case, and this column mirrors both and MUST NOT diverge. It is assigned once at insert and is immutable. It is globally unique rather than tenant-scoped, which is why it is not a row of `MOS-STORE-216` and why its unique index is an allow-listed `MOS-STORE-221` exception under criterion 30: a public id must be resolvable before the tenant is known, which is what makes it public. `audit_events.public_id` and `policy_decisions.public_id` are the same construction under the prefixes `aud_` and `pd_`.

**MOS-STORE-268** `study_ids` is an array from 0.1.0 with `primary_study_id` as its first element, because prior comparison (nodule growth against a prior study) is ordinary chest-CT work and widening a scalar later is a breaking API change. The wire shape is Chapter 10's and there is exactly one: `MOS-API-045`'s `input.study_instance_uid` plus `input.prior_study_instance_uids[]`. This is the mapping onto storage, stated here and nowhere else: `primary_study_id` is the `studies.id` that `(tenant_id, input.study_instance_uid)` resolves to under `MOS-STORE-216`, and `study_ids` is that id followed by the ids of `input.prior_study_instance_uids` in the order the request gave them. `prior_study_ids`, `study_instance_uids` and `prior_study_instance_uids` are wire or prose spellings and MUST NOT appear as column names in this schema, and no chapter may introduce a fifth form for the same concept; whether more than one prior study is permitted is OQ-13 and is not settled here.

**MOS-STORE-269** `CANCELLED` is present in the enum and is **reserved**: cancellation of a running inference is not implemented before 0.3, and no code path may write it in 0.1–0.2. Chapter 5 reaches the same result structurally by giving `CANCELLED` no row in `job_state_transition`, so `job_transition()` refuses every attempt to reach it (`MOS-EXEC-073`, `MOS-EXEC-074`).

**MOS-STORE-270** Selection results, including the reason every rejected series was rejected, are first-class rows in `job_series` — not a log line and not a `jsonb` blob; a job whose selection produced no `selected` row MUST be `REJECTED` with `reject_reason_code` set, and the per-series reasons MUST be readable through the API (Chapter 10). The per-series `reason_code` vocabulary is Chapter 3's closed `SeriesRequirement` list (`MOS-DATA-069`) in lowercase snake_case; the job-level `reject_reason_code` vocabulary is Chapter 5 §5.3.1's, and the two lists are different. `job_series.decision` and the per-series `verdict` member of the `job.rejected` payload (ch. 5 §5.14.1) are **one** value set and not two: the positive value is `selected` in the row and `selected` on the wire, the negative is `rejected` in both, and there is therefore no `selected`→`accepted` mapping for a consumer to implement or for the API's `series-selection` read to get wrong. Chapter 5 owns the payload and carries that pair; a payload spelling the positive value `accepted` is the defect, not a synonym.

**MOS-STORE-271** `job_events.seq` is a per-job monotonic integer assigned in the same transaction as the state change; consumers MUST order by `(job_id, seq)` and MUST discard an event whose `seq` is lower than one already applied, so event ordering is a property of the database record rather than of the transport.

**MOS-STORE-272** `job_events.payload`, `job_outbox.envelope`, `policy_decisions` columns and every metric label MUST NOT contain PHI — no patient name, birth date, accession number or free-text DICOM description. Chapter 8 classifies PHI; this chapter enforces it as a schema-review rule and a CI grep.

**MOS-STORE-273** An intermediate volume or mask MUST NOT pass between job steps by any means other than an `execution_artifacts` row. The spatial CHECK — spacing, origin, direction, frame of reference and reference series mandatory for every voxel-grid artifact — is what lets a downstream step verify it is on the same grid as the step that produced its input, and is the cheapest available guard against a silent geometry mismatch.

**MOS-STORE-274** `job_queue` rows are deleted when the job reaches a terminal state, the `jobs` row being the durable record; `job_outbox` rows are retained until published plus 7 days.

### 12.11 Results, provenance, generated DICOM objects, reviews

```sql
CREATE TABLE results (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL, job_id uuid NOT NULL, study_id uuid NOT NULL,
  result_kind text NOT NULL CHECK (result_kind IN ('segmentation','measurement',
    'detection','classification','narrative')),
  capability_id uuid NOT NULL REFERENCES capabilities(id),
  service_version_id uuid NOT NULL,
  clinical_use_mode text NOT NULL CHECK (clinical_use_mode IN ('research_only','clinical')),
  deployment_id uuid,
  deployment_environment text NOT NULL
    CHECK (deployment_environment IN ('dev','staging','production')),
  deployment_state_at_execution text NOT NULL CHECK (deployment_state_at_execution IN
    ('PENDING','VERIFYING','SERVING','SUSPENDED','DRAINING','RETIRED')),
  deployment_role_at_execution text NOT NULL CHECK (deployment_role_at_execution IN
    ('ACTIVE','CANARY','SHADOW','STANDBY')),
                    -- the three value spaces are Chapter 6 §6.8 (MOS-REG-072, MOS-REG-073),
                    -- snapshotted per MOS-SAFE-046 (ch. 9); they MUST NOT diverge from either
  visibility text NOT NULL DEFAULT 'normal' CHECK (visibility IN ('normal','shadow')),
  out_of_distribution boolean NOT NULL DEFAULT false,
  plausibility_state text NOT NULL DEFAULT 'ok'
    CHECK (plausibility_state IN ('ok','flagged','failed')),
                    -- 'flagged' is Chapter 7's `warn` outcome (MOS-EVID-110); a `fail`
                    -- terminates the job before any results row exists (MOS-EVID-111)
  review_status text NOT NULL DEFAULT 'UNREVIEWED'
    CHECK (review_status IN ('UNREVIEWED','PENDING','IN_REVIEW','ACCEPTED','MODIFIED',
                             'REJECTED','EXPIRED','SUPERSEDED')),
                    -- mirrors MOS-SAFE-058/MOS-SAFE-062 (ch. 9) and MUST NOT diverge
  superseded_by uuid,
  bundle_bucket text NOT NULL, bundle_object_key text NOT NULL,
  bundle_digest sha256_digest NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT results_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT results_job_capability_uk UNIQUE (job_id, capability_id),
  CONSTRAINT results_job_fk FOREIGN KEY (tenant_id, job_id)
    REFERENCES jobs (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT results_study_fk FOREIGN KEY (tenant_id, study_id)
    REFERENCES studies (tenant_id, id) ON DELETE RESTRICT,
  -- Plain, not composite, for the MOS-STORE-217 dual-scope reason.
  CONSTRAINT results_service_version_fk FOREIGN KEY (service_version_id)
    REFERENCES service_versions (id),
  CONSTRAINT results_superseded_fk FOREIGN KEY (tenant_id, superseded_by)
    REFERENCES results (tenant_id, id),
  CONSTRAINT results_deployment_fk FOREIGN KEY (tenant_id, deployment_id)
    REFERENCES deployments (tenant_id, id) ON DELETE RESTRICT,
  CHECK ((visibility = 'shadow') = (deployment_role_at_execution = 'SHADOW')),
  CHECK (superseded_by IS NULL OR superseded_by <> id)
);
CREATE INDEX results_study_idx ON results (tenant_id, study_id, created_at DESC);
CREATE INDEX results_deployment_idx ON results (tenant_id, deployment_id, created_at DESC);
CREATE INDEX results_review_idx ON results (tenant_id, review_status, created_at DESC)
  WHERE review_status IN ('UNREVIEWED','PENDING') AND superseded_by IS NULL;

CREATE TABLE result_provenance (
  result_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL, job_id uuid NOT NULL, root_job_id uuid NOT NULL,
  patient_id uuid NOT NULL, study_id uuid NOT NULL,
  study_instance_uid dicom_uid NOT NULL,
  input_series_ids uuid[] NOT NULL CHECK (cardinality(input_series_ids) >= 1),
  input_series_uids dicom_uid[] NOT NULL CHECK (cardinality(input_series_uids) >= 1),
  input_instance_uids dicom_uid[] NOT NULL,
  input_instance_count integer NOT NULL CHECK (input_instance_count >= 1),
  input_pixel_digest sha256_digest NOT NULL,
  service_id uuid NOT NULL, service_version semver NOT NULL,
  service_image_digest oci_digest NOT NULL,
  execution_mode text NOT NULL CHECK (execution_mode IN ('native','sealed')),
  models jsonb NOT NULL CHECK (jsonb_array_length(models) >= 1),
  preprocessing_specs jsonb NOT NULL CHECK (jsonb_array_length(preprocessing_specs) >= 1),
  operating_threshold numeric(6,5),   -- the value of `service_version_capabilities
                                      -- .operating_points[].score_threshold` pinned at
                                      -- execution. `score_threshold` is the one member
                                      -- name for this number (ch. 2 MOS-SVC-020) and it
                                      -- is what `result_findings.score_threshold` stores;
                                      -- this column keeps ch. 9 MOS-SAFE-054's provenance
                                      -- spelling and holds the identical value. Ch. 6
                                      -- states the manifest-to-model-version half of the
                                      -- same mapping. Per-finding operating points live
                                      -- on result_findings.
  threshold_source uuid,              -- the AcceptanceCriteria it came from (ch. 9 MOS-SAFE-083 §D)
  geometry jsonb NOT NULL, accelerator jsonb NOT NULL,
  worker_version text NOT NULL, runtime_version text NOT NULL,
  platform_commit text NOT NULL,
  deid_policy_id uuid, resolution_snapshot jsonb NOT NULL,
  generated_object_uids dicom_uid[] NOT NULL DEFAULT '{}',
  artifact_object_keys text[] NOT NULL DEFAULT '{}',
  provenance_redacted_at timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT result_provenance_result_fk FOREIGN KEY (tenant_id, result_id)
    REFERENCES results (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT result_provenance_patient_fk FOREIGN KEY (tenant_id, patient_id)
    REFERENCES patients (tenant_id, id) ON DELETE RESTRICT,
  CHECK (geometry ?& array['source_spacing_mm','source_origin_mm','source_direction',
    'source_frame_of_reference_uid','canonical','model_space','inverse_transform']),
  CHECK (accelerator ?& array['gpu_model','driver','cuda']),
  CHECK (cardinality(input_series_ids) = cardinality(input_series_uids))
);
CREATE TRIGGER result_provenance_no_delete BEFORE DELETE ON result_provenance
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER result_provenance_immutable BEFORE UPDATE ON result_provenance
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('result_id','job_id','patient_id',
    'study_id','study_instance_uid','input_pixel_digest','service_image_digest',
    'models','preprocessing_specs','geometry','accelerator',
    'platform_commit');
-- The MOS-STORE-221 allow-listed exception: a GIN index cannot lead with `tenant_id`
-- without btree_gin, and the MOS-STORE-280 recall query runs inside a tenant context
-- where RLS already supplies the tenant predicate.
CREATE INDEX result_provenance_models_idx
  ON result_provenance USING gin (models jsonb_path_ops);

CREATE TABLE result_dicom_objects (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL, result_id uuid NOT NULL,
  object_kind text NOT NULL CHECK (object_kind IN ('SEG','SR','SC','PR')),
  sop_class_uid dicom_uid NOT NULL,
  series_instance_uid dicom_uid NOT NULL,
  sop_instance_uid dicom_uid NOT NULL,
  series_number integer NOT NULL CHECK (series_number >= 9000),
  output_index integer NOT NULL CHECK (output_index >= 0),
  derivation_inputs jsonb NOT NULL,
  frame_count integer CHECK (frame_count >= 1),
  ai_derived boolean NOT NULL DEFAULT true CHECK (ai_derived),
  clinical_use_mode text NOT NULL CHECK (clinical_use_mode IN ('research_only','clinical')),
  research_marked boolean NOT NULL,
  stow_state text NOT NULL DEFAULT 'pending'
    CHECK (stow_state IN ('pending','stored','verified','failed','erased')),
  pacs_backend text NOT NULL REFERENCES pacs_backends(id),
  object_digest sha256_digest, size_bytes bigint,
  stored_at timestamptz, verified_at timestamptz, erased_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT result_dicom_objects_uid_uk UNIQUE (tenant_id, sop_instance_uid),
  CONSTRAINT result_dicom_objects_output_uk UNIQUE (result_id, object_kind, output_index),
  CONSTRAINT result_dicom_objects_result_fk FOREIGN KEY (tenant_id, result_id)
    REFERENCES results (tenant_id, id) ON DELETE RESTRICT,
  CHECK (derivation_inputs ?& array['tenant_id','idempotency_key','service_id','service_version',
    'model_id','model_version','uid_space','uid_kind','output_index']),  -- the full MOS-IMG-062 tuple
  CONSTRAINT result_dicom_objects_ruo_marking
    CHECK (clinical_use_mode <> 'research_only' OR research_marked),
  CHECK ((stow_state IN ('stored','verified')) = (stored_at IS NOT NULL)),
  CHECK ((stow_state = 'erased') = (erased_at IS NOT NULL))
);

-- The field contract, the state set and every transition are Chapter 9 MOS-SAFE-058,
-- MOS-SAFE-059 and MOS-SAFE-060; this table renders them and MUST NOT add, rename or
-- drop a field.
CREATE TABLE result_reviews (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL, result_id uuid NOT NULL,
  round integer NOT NULL DEFAULT 1 CHECK (round >= 1),
  state text NOT NULL DEFAULT 'PENDING' CHECK (state IN
    ('PENDING','IN_REVIEW','ACCEPTED','MODIFIED','REJECTED','EXPIRED','SUPERSEDED')),
  assignee_user_id uuid,
  reviewer_user_id uuid,                        -- null until the row is claimed
  -- A plain `text` snapshot of the role's `key` at the moment of the act (§12.6). No FK
  -- to `roles`: `role.delete` (ch. 8 §8.3.2) would either fail on clinical evidence or
  -- cascade it away, and `roles.id` is a `uuid`, which none of the permitted values could
  -- ever satisfy. No CHECK enumerating role keys either: `MOS-SEC-038` (ch. 8) forbids a
  -- database constraint over role keys and ch. 8 acceptance check 26 tests for one.
  -- Chapter 9 derives the reviewer's class from a role attribute (`MOS-SAFE-058`,
  -- `MOS-SAFE-068`), never from a fixed job-title spelling.
  reviewer_role text,
  claimed_at timestamptz, submitted_at timestamptz,
  review_due_at timestamptz,                    -- from deployments.review_sla_hours
  action_rationale text,
  modifications jsonb,                          -- RFC 6902 patch against Result.findings
  rejection_reason text CHECK (rejection_reason IN ('false_positive','false_negative',
    'wrong_laterality','mis_segmentation','measurement_implausible','wrong_series_analysed',
    'out_of_intended_use','other')),
  known_failure_mode_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT result_reviews_round_uk UNIQUE (result_id, round),
  CONSTRAINT result_reviews_result_fk FOREIGN KEY (tenant_id, result_id)
    REFERENCES results (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT result_reviews_reviewer_fk FOREIGN KEY (tenant_id, reviewer_user_id)
    REFERENCES users (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT result_reviews_assignee_fk FOREIGN KEY (tenant_id, assignee_user_id)
    REFERENCES users (tenant_id, id) ON DELETE RESTRICT,
  CHECK (state <> 'MODIFIED'
         OR (modifications IS NOT NULL AND length(action_rationale) >= 20)),
  CHECK (state <> 'REJECTED'
         OR (rejection_reason IS NOT NULL AND length(action_rationale) >= 20)),
  CHECK (state IN ('PENDING','EXPIRED','SUPERSEDED') OR reviewer_user_id IS NOT NULL)
);

-- MOS-SAFE-060: a submitted row is never mutated. The grant cannot express "terminal
-- rows only", so the structural form of the rule is this trigger.
CREATE TRIGGER result_reviews_terminal_immutable BEFORE UPDATE ON result_reviews
  FOR EACH ROW
  WHEN (OLD.state IN ('ACCEPTED','MODIFIED','REJECTED','EXPIRED','SUPERSEDED'))
  EXECUTE FUNCTION forbid_mutation();

CREATE FUNCTION sync_result_review_status() RETURNS trigger AS $$
BEGIN
  UPDATE results
     SET review_status = (SELECT r.state FROM result_reviews r
                           WHERE r.result_id = NEW.result_id
                           ORDER BY r.round DESC LIMIT 1),
         updated_at = now()
   WHERE id = NEW.result_id;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER result_reviews_sync AFTER INSERT OR UPDATE ON result_reviews
  FOR EACH ROW EXECUTE FUNCTION sync_result_review_status();
```

The typed clinical content of a result lives in two child tables, both `ON DELETE CASCADE` from `results`:

| Table | Key | Columns |
|---|---|---|
| `result_findings` | `UNIQUE (result_id, finding_index)` | `capability_id uuid NOT NULL REFERENCES capabilities(id)`, `concept_scheme`, `concept_code`, `concept_display` (all `NOT NULL`), `concept_scheme_uri text`, `present boolean NOT NULL`, `laterality_scheme`, `laterality_code`, `finding_sites jsonb NOT NULL CHECK (jsonb_array_length(finding_sites) >= 1)` — each element the flattened `{scheme, code, display, scheme_uri}` of ch. 2 §2.8.2 — `score double precision CHECK (score >= 0 AND score <= 1)`, `operating_point_id text`, `score_threshold double precision CHECK (score_threshold >= 0 AND score_threshold <= 1)`, `segment_number`, `source_series_instance_uid dicom_uid NOT NULL`, `source_sop_instance_uids dicom_uid[] NOT NULL CHECK (cardinality(source_sop_instance_uids) >= 1)`, `out_of_distribution boolean NOT NULL DEFAULT false`; `CHECK ((score IS NULL) = (score_threshold IS NULL) AND (score IS NULL) = (operating_point_id IS NULL))`; `CHECK (score IS NULL OR present = (score >= score_threshold))` |
| `result_measurements` | `id` | `capability_id uuid NOT NULL REFERENCES capabilities(id)`, `finding_id` (nullable FK), `concept_scheme`, `concept_code`, `concept_display`, `concept_scheme_uri text`, `value numeric(18,6) NOT NULL`, `ucum_unit NOT NULL`, `derivation_scheme`, `derivation_code`, `geometry_space text NOT NULL DEFAULT 'source' CHECK (IN ('source','derived_declared'))` (stores `ResultBundle.measurements[].computed_in` verbatim; the enums are identical), `source_series_instance_uid dicom_uid NOT NULL`, `method_detail jsonb` |

**MOS-STORE-275** `UNIQUE (job_id, capability_id)` makes a duplicate result for one job and capability impossible at the database level, and the `results` rows and the job's transition to `COMPLETED` MUST be written in one transaction; a retry that reaches the result stage again finds the constraint and MUST treat the violation as success, not as an error.

**MOS-STORE-275a** One `results` row is written per `(job_id, capability_id)` present in `ResultBundle.capability_outcomes`; all rows for one job reference the same `bundle_object_key`, because one execution returns exactly one `ResultBundle` (Chapter 2 §2.8). `result_kind` is the clinical kind of that capability's output and is **not** a key; the DICOM object kinds `SEG`/`SR`/`SC`/`PR` are `result_dicom_objects.object_kind`, a different column on a different table. A `visibility = 'shadow'` row MUST NOT have any `result_dicom_objects` child (`MOS-REG-082`, ch. 6).

**MOS-STORE-291a** The four `deployment_*` columns on `results` are a **snapshot taken at execution**, not a join: a deployment's live `role` and `state` change, and a result must remain attributable to the conditions under which it was produced (Chapter 9 `MOS-SAFE-046`). Their three value spaces are Chapter 6 §6.8's (`MOS-REG-072`, `MOS-REG-073`) and MUST NOT diverge from them. They are the columns from which the Cedar `Result` attributes `deployment_environment`, `deployment_state` and `deployment_role` are projected for policy P-014 (Chapter 8 `MOS-SEC-066`) — the three MUST NOT be conflated into one field — and the values copied into provenance and into DICOM `(0018,1000)`. `ON DELETE RESTRICT` on `results_deployment_fk` is the structural form of the rule that a `Deployment` row is never deleted, only `SUSPENDED` or `RETIRED` (Chapter 6 §6.8). `visibility = 'shadow'` marks a `role = SHADOW` result: Chapter 4's DICOM writer MUST refuse to convert it and the Gateway MUST refuse to STOW it (`MOS-REG-082`). The shadow Job itself carries `jobs.shadow_of_job_id` (§12.10), the pointer back to the clinical job it mirrors; that column is Chapter 6's (`MOS-REG-082`) and MUST NOT diverge from it. Shadow results follow the same retention as their clinical counterparts.

**MOS-STORE-276** A superseded result is marked with `superseded_by` and never deleted. `StoreResults` is the pipeline's commit point and its last mutating step, and recovery past it is forward-only; with deterministic generated UIDs (Chapter 4) this removes the need for any compensating action — which matters, because platform policy denies deleting a study.

**MOS-STORE-277** `results.clinical_use_mode` is copied from the `deployments` row at job creation and is immutable, and is not read from the deployment at display time: a deployment that later flips to `clinical` must not retroactively reclassify results produced while it was research-only.

**MOS-STORE-278** Exactly one `result_provenance` row MUST exist per `results` row, written in the same transaction, and a result without provenance MUST NOT be observable through any API.

**MOS-STORE-279** Provenance records both internal ids and DICOM UIDs for its inputs. The redundancy is deliberate: UIDs survive a projection rebuild, a PACS migration and an export to another deployment, ids do not, and `input_series_uids`, `input_instance_uids` and `generated_object_uids` are what make a result portable and the SR evidence sequence reconstructible. `result_provenance.input_instance_uids` **is** the full input UID list and it is the only one: there is no `job_input_instances` table in this chapter and one MUST NOT be added, because a second copy of the same UIDs hanging off the job would drift from the list the result was digested over. The `input.instances` block of `MOS-SAFE-083` §B (ch. 9) resolves here — its `count` is `input_instance_count`, its `sha256_of_sorted_sop_uid_list` is computed over this column, and its `manifest_uri` dereferences to it — and Chapter 9 SHOULD re-point that cross-reference at `result_provenance.input_instance_uids`; the provenance-completeness check of criterion 14 already reads this column.

**MOS-STORE-280** The recall query MUST be answerable in one statement — this is the query the table exists for and the test of whether the schema is adequate:

```sql
SELECT r.id AS result_id, p.study_instance_uid, r.review_status, r.clinical_use_mode,
       rdo.object_kind, rdo.sop_instance_uid, rdo.stow_state
FROM result_provenance p
JOIN results r ON r.id = p.result_id
LEFT JOIN result_dicom_objects rdo ON rdo.result_id = r.id
WHERE p.models @> jsonb_build_array(jsonb_build_object('model_id', $1, 'version', $2))
ORDER BY r.created_at;
```

**MOS-STORE-281** `accelerator` is `ResultBundle.diagnostics.accelerator` copied verbatim (ch. 2 `MOS-SVC-098`) and MUST record at least `gpu_model`, `driver` and `cuda`, plus `trt` for a TensorRT backend, because a result produced on a different accelerator stack than the one the model version was evaluated on is a different numerical execution and the record must show it. The member names are Chapter 2's and MUST NOT be renamed on persistence.

**MOS-STORE-281a** `models` and `preprocessing_specs` are **arrays**, copied verbatim from `ResultBundle.diagnostics.model_versions[]` and `diagnostics.preprocessing_specs[]` (ch. 2 `MOS-SVC-098`): `[{role, model_id, version, artifact_digest}]` and `[{id, version, digest}]`. The model member is spelled `version`, never `model_version` — that is ch. 2 §2.8.7's spelling and ch. 9 `MOS-SAFE-083` §D's, the copy is verbatim, and the `MOS-STORE-280` recall query therefore matches on `version`. A single-model service emits one element. Scalar `model_id` / `model_version` columns cannot hold a multi-model service's record and the `role` discriminator would have nowhere to go.

**MOS-STORE-282** `result_measurements.ucum_unit`, `concept_scheme` and `concept_code` are `NOT NULL`, so a bare number with a free-text label cannot be stored — which is what makes a conformant TID 1500 SR generatable from the table without a second, un-modelled dictionary.

**MOS-STORE-282a** `concept_scheme`, `concept_code`, `concept_display` and `concept_scheme_uri` are the flattened persistence of the ch. 2 §2.8.2 coded concept `{scheme, code, display, scheme_uri}`; the mapping is name-for-name and no third spelling exists. `finding_sites` stores an array of the same object, because `MOS-IMG-117` (ch. 4) requires at least one anatomic site per finding and permits more, and `MOS-SVC-088a` (ch. 2) is the producer-side field it persists.

**MOS-STORE-282b** `result_measurements.ucum_unit` stores `ResultBundle.measurements[].unit.code`; `unit.scheme` is always `UCUM` (ch. 2 `MOS-SVC-091`) and is not stored redundantly. The API (ch. 10) re-inflates the ch. 2 object form; the column name is a storage detail and MUST NOT appear in any wire schema.

**MOS-STORE-283** `geometry_space` defaults to `'source'` and may be `'derived_declared'` only when the model version's manifest declares derived geometry; every measurement is otherwise computed in source geometry against the source `PixelSpacing` and `SliceThickness` (Chapter 4 is normative), and the column exists so the exception is visible in the data rather than implicit in code. Its two values are exactly those of `ResultBundle.measurements[].computed_in` (`MOS-SVC-091`, ch. 2) and MUST NOT diverge from them.

**MOS-STORE-284** `result_dicom_objects_ruo_marking` enforces research-use marking in the database: an object belonging to a research-only result cannot be recorded as stored unless it is marked. The marking itself — `SeriesDescription` prefix, SR document title concept, `ConversionType`, burned-in SC banner — is Chapters 4 and 9; this constraint makes it impossible to skip the step and still have a row.

**MOS-STORE-285** `ai_derived` is `NOT NULL DEFAULT true CHECK (ai_derived)`: it exists to be queried and asserted and cannot be false, because every row here is by definition an AI-derived object.

**MOS-STORE-286** `derivation_inputs` records the exact tuple from which the deterministic `SeriesInstanceUID` and `SOPInstanceUID` were derived, so a retry can recompute the identity without re-deriving it from live state and a mismatch between stored and recomputed UID is detectable in CI. The nine members are the complete `derive_uid` argument tuple of `MOS-IMG-062` (ch. 4) less the platform-constant `org_root`; `uid_space` and `uid_kind` are load-bearing — without `uid_kind` a job producing one SEG series and one SR series derives the same `SeriesInstanceUID` twice — and the set MUST NOT be shortened.

**MOS-STORE-287** `result_reviews` is append-only per round: a changed opinion is a new row with `round + 1` (`MOS-SAFE-066`), never a mutation of a submitted row — `result_reviews_terminal_immutable` is the structural form of `MOS-SAFE-060` — and `results.review_status` mirrors the highest-round row, `UNREVIEWED` when no row exists (`MOS-SAFE-062`). Chapter 9 is normative for the state set and every transition; the review history is the clinically meaningful record and MUST NOT be overwritten.

**MOS-STORE-288** Review is a record, not a gate on execution: there is no `AWAITING_REVIEW` job state and one MUST NOT be added, since parking a job for human latency would make lease, timeout and crash-recovery semantics incoherent. The job reaches `COMPLETED` on schedule, the result is stored and visible, and the review is recorded against it; Chapter 9 is normative for what, if anything, is gated on `review_status`.

**MOS-STORE-289** `result_reviews` is the only production source of accept/modify/reject rates — the input to continuous site monitoring (Chapter 7) — and MUST NOT be discarded by retention.


### 12.12 The evidence plane

Sixteen tables, all tenant-owned, all following the standard conventions. The sealed ones carry `forbid_column_change` triggers over the listed sealed columns and `forbid_mutation` on `DELETE`. Chapter 7 is normative for which facts each row must record, for every enum value below and for every field name; this chapter is normative for the physical form — plural table names, `uuid` primary keys, `text` + `CHECK` enums (MOS-STORE-209), `(bucket, object_key)` pairs for object-store locations, `created_at`/`updated_at`. Where the two disagree the schema is amended here, never the behaviour there (MOS-STORE-201). Each column that mirrors a Chapter 7 value set names the requirement it mirrors and MUST NOT diverge from it.

| Table | Key | Columns | Sealed columns |
|---|---|---|---|
| `datasets` | `UNIQUE (tenant_id, slug)` | `display_name`, `purpose` ∈ `training,tuning,evaluation,acceptance,monitoring` (`MOS-EVID-026` is normative; mirrored exactly), `custodian` (Chapter 7's `source_id`), `visibility` ∈ `tenant_private,tenant_shared,public_readonly` default `tenant_private` (`MOS-EVID-012`), `licence_spdx`, `licence_text`, `licence_url`, `created_by`, `deleted_at`; `CHECK (visibility = 'tenant_private' OR licence_spdx IS NOT NULL OR licence_text IS NOT NULL)` (`MOS-EVID-027`) | — |
| `dataset_versions` | `UNIQUE (dataset_id, version)`, `UNIQUE (manifest_digest)` | `parent_version_id`, `derivation jsonb` (`MOS-EVID-022`), `manifest_bucket`, `manifest_object_key`, `manifest_line_count`, `case_count`, `patient_count`, `study_count`, `series_count`, `instance_count`, `source_description`, `acquisition_profile jsonb NOT NULL` (`MOS-EVID-024`), `deidentification_status` ∈ `identified,pseudonymised,public_deidentified` (`MOS-EVID-012` and `MOS-EVID-021` are normative; this column mirrors Chapter 7's spelling and value set and MUST NOT diverge), `deid_policy_id`, `uid_mapping_table_id` (the `(tenant_id, deid_key_version)` UID space of §12.8 the version was sealed under, `MOS-EVID-021`), `status` ∈ `SEALED,DEFECTIVE` (default `SEALED`, `MOS-EVID-014`), `defect_reason`, `erasure_state` ∈ `clean,contains_erased_subject`, `usable_for_new_runs`, `sealed_at`, `sealed_by`; `CHECK (patient_count <= case_count)`; `CHECK (deidentification_status = 'identified' OR deid_policy_id IS NOT NULL)` | all but `erasure_state`, `usable_for_new_runs`, `status`, `defect_reason` |
| `dataset_cases` | `PK (dataset_version_id, case_key, series_instance_uid)` | `patient_key`, `study_instance_uid`, `modality`, `sop_class_uid`, `instance_count`, `sop_instance_uids dicom_uid[] CHECK (cardinality >= 1)` in the canonical slice order of Chapter 4 (`MOS-EVID-017`), `series_pixel_digest` (`MOS-EVID-018`), `lossy_compressed boolean NOT NULL` (`MOS-EVID-019`), `accession_number_hash`, `acquisition jsonb NOT NULL` (`MOS-EVID-020`, copied without imputation), `corpus_generation smallint NOT NULL DEFAULT 0 CHECK (corpus_generation BETWEEN 0 AND 1)` — ch. 17 `MOS-TRAIN-087`: a case at generation 2 or above MUST NOT be used in any partition, so a sealed cohort cannot contain one and the CHECK is the structural form of that rule; index on `(tenant_id, study_instance_uid)` and on `(tenant_id, series_pixel_digest)` for leakage check L3; `CHECK (cardinality(sop_instance_uids) = instance_count)` | all |
| `dataset_splits` | `UNIQUE (dataset_version_id, name)` | `partition_level text NOT NULL DEFAULT 'patient' CHECK (partition_level = 'patient')` (`MOS-EVID-029`), `partitions text[] NOT NULL`, `partition_patients jsonb NOT NULL`, `assignment_method text NOT NULL` (descriptive only, never re-executed, `MOS-EVID-028`), `stratified_by text[] NOT NULL DEFAULT '{}'`, `leakage_report jsonb NOT NULL` (L1–L5), `manifest_bucket`, `manifest_object_key`, `manifest_digest` (Chapter 7's `split_digest`), `sealed_at`, `sealed_by` (Chapter 7's `frozen_at`/`frozen_by`) | all |
| `dataset_split_members` | `PK (split_id, patient_key)` | `partition` ∈ `train,tune,test,excluded` (`MOS-EVID-031` is normative; this is Chapter 7's partition vocabulary mirrored exactly — `val` is **not** a permitted value and this column is never called `fold`), `exclusion_reason`, `stratum jsonb`; `CHECK ((partition = 'excluded') = (exclusion_reason IS NOT NULL))` | all |
| `annotation_sets` | `UNIQUE (dataset_version_id, name)` | `capability_id`, `label_definition_id`, `annotation_type` ∈ `mask,bounding_box,point,case_label,measurement`, `consensus_rule` ∈ `single_reader,majority_at_least_2,union,intersection,staple,arbitrated` (`MOS-EVID-039` owns this six-value enum; mirrored exactly and MUST NOT diverge), `consensus_params jsonb NOT NULL DEFAULT '{}'`, `reader_count integer NOT NULL CHECK (reader_count >= 1)` (`MOS-EVID-038`), `reference_of_record boolean NOT NULL DEFAULT false` (`MOS-EVID-042`), `manifest_bucket`, `manifest_object_key`, `manifest_digest` (Chapter 7's `annotation_digest`), `sealed_at`, `sealed_by`; `CHECK (consensus_rule <> 'single_reader' OR reader_count = 1)`; `CHECK (consensus_rule <> 'majority_at_least_2' OR reader_count >= 3)` | all |
| `annotation_readers` | `PK (annotation_set_id, reader_id)` | `role` ∈ `radiologist,resident,algorithm,registry_extract`, `years_experience`, `board_certified`, `specialty`, `tool NOT NULL`, `instructions_uri NOT NULL`, `blinded_to text[] NOT NULL DEFAULT '{}'` — the per-reader detail table `MOS-EVID-038` requires | all |
| `annotations` | `UNIQUE (annotation_set_id, case_key, series_instance_uid)` | `patient_key`, `study_instance_uid`, `reference_kind`, `reference_bucket`, `reference_object_key`, `reference_digest`, `geometry text NOT NULL DEFAULT 'source' CHECK (geometry = 'source')` (`MOS-EVID-041`), `reference_volume_ml` (`MOS-EVID-044`), `per_reader jsonb NOT NULL CHECK (jsonb_array_length(per_reader) >= 1)` — one `{reader_id, bucket, object_key, digest, volume_ml}` per reader — `inter_reader jsonb` (`MOS-EVID-043`), `annotation_provenance text NOT NULL CHECK (annotation_provenance IN ('de_novo','model_seeded_corrected','model_output_unreviewed'))` — ch. 17 `MOS-TRAIN-098` owns this three-value enum and this column mirrors it exactly — `seed jsonb`, the `{seed_model_id, seed_model_version, seed_evaluation_run_id, seed_operating_point, seed_dice, voxels_added, voxels_removed, edit_seconds}` block of `MOS-TRAIN-099`, `corpus_generation smallint NOT NULL DEFAULT 0 CHECK (corpus_generation BETWEEN 0 AND 1)` (`MOS-TRAIN-087`), `annotated_at`; `CHECK (jsonb_array_length(per_reader) < 2 OR inter_reader IS NOT NULL)`; `CHECK ((annotation_provenance = 'model_seeded_corrected') = (seed IS NOT NULL))`; `CHECK (annotation_provenance <> 'model_output_unreviewed')` — the enum carries all three values because `MOS-TRAIN-098` owns all three, and this second CHECK is that requirement's rule that an unreviewed model output may exist only as a `seed` record and never as a member of an `AnnotationSet` | all |
| `capability_claims` | `UNIQUE (subject_kind, subject_id, capability_id, metric, evaluation_run_id)`, partial `UNIQUE (subject_kind, subject_id, capability_id) WHERE is_primary` | `subject_kind` ∈ `service_version,model_version`, `subject_id`, `capability_id`, `evaluation_run_id`, `metric` (a registry id, `MOS-EVID-054`), `value`, `ci_low`, `ci_high` (`double precision NOT NULL`, `MOS-EVID-056`), `n_cases`, `n_patients`, `operating_point jsonb`, `is_primary boolean NOT NULL DEFAULT false` (`MOS-EVID-073`); `CHECK (metric NOT IN ('sensitivity','specificity','ppv','froc_sensitivity') OR operating_point IS NOT NULL)` (`MOS-EVID-055`); append-only | all |
| `acceptance_criteria` | `UNIQUE NULLS NOT DISTINCT (tenant_id, capability_id, version)` | `spec jsonb NOT NULL` — the declarative document of `MOS-EVID-076` and §7.8.1, stored verbatim, never an expression string — `cohort_dataset_version_id`, `split_id`, `partition text NOT NULL DEFAULT 'test' CHECK (partition = 'test')` (`MOS-EVID-076`), `annotation_set_id`, `approved_by`, `approved_at`; `CHECK (jsonb_array_length(coalesce(spec->'absolute','[]'::jsonb)) + jsonb_array_length(coalesce(spec->'regression','[]'::jsonb)) >= 1)` | all but `approved_by`, `approved_at` |
| `tenant_acceptance_bindings` | `PK (tenant_id, capability_id)` (`MOS-EVID-080`) | `criteria_version integer NOT NULL`, `dataset_version_id`, `split_id`, `partition` (the vocabulary of `dataset_split_members.partition`), `annotation_set_id`, `threshold_overrides jsonb NOT NULL DEFAULT '{}'` (may only tighten, `MOS-EVID-081`), `bound_by`, `bound_at` | — |
| `evaluation_runs` | `UNIQUE (run_digest)`; partial `UNIQUE (service_version_id, model_version_id, dataset_version_digest, split_digest, partition, annotation_digest, image_digest, coalesce(preprocessing_spec_digest, internal_pipeline_digest), operating_thresholds, metric_conventions) WHERE state = 'SUCCEEDED'` | `kind` ∈ `vendor_evidence,site_acceptance,monitoring_period` (`MOS-EVID-003` fixes exactly these three), `service_version_id`, `model_version_id`, `capability_id`, `dataset_version_id`, `dataset_version_digest`, `split_id`, `split_digest`, `partition`, `annotation_set_id`, `annotation_digest`, `preprocessing_spec_id`, `preprocessing_spec_version`, `preprocessing_spec_digest`, `internal_pipeline_digest`, `code_commit`, `code_dirty boolean NOT NULL`, `evaluator_image_digest` (the harness), `image_digest` (the container that produced the predictions), `inference_backend jsonb`, `accelerator jsonb`, `operating_thresholds jsonb NOT NULL DEFAULT '{}'` (`MOS-EVID-055`), `metric_conventions jsonb NOT NULL` (the §7.6 block, recorded verbatim), `metric_registry_version integer NOT NULL` (`MOS-EVID-054`), `seed`, `runner`, `state` ∈ `PENDING,RUNNING,SUCCEEDED,FAILED,INVALIDATED` (`MOS-EVID-014` owns this enum; the column is `state`, never `status`, and MUST NOT diverge), `invalidation_reason`, `aggregate_metrics jsonb`, `run_digest`, `per_case_bucket`, `per_case_object_key`, `started_at`, `finished_at`; `CHECK ((service_version_id IS NULL) <> (model_version_id IS NULL))`; `CHECK (model_version_id IS NULL OR preprocessing_spec_digest IS NOT NULL)`; `CHECK (service_version_id IS NULL OR internal_pipeline_digest IS NOT NULL)`; `CHECK ((state='SUCCEEDED') = (finished_at IS NOT NULL AND run_digest IS NOT NULL))`; `CHECK (state <> 'SUCCEEDED' OR code_dirty = false)` (`MOS-EVID-062`); `CHECK ((state='INVALIDATED') = (invalidation_reason IS NOT NULL))` | — |
| `evaluation_case_metrics` | `id bigint identity PK`, `UNIQUE (evaluation_run_id, case_key, metric)` | `patient_key`, `study_instance_uid`, `series_instance_uid` (all `NOT NULL`; the series UID is what the pairing function of §7.9.2 joins on), `metric` (a registry id), `value double precision`, `undefined_reason` ∈ `empty_ground_truth,empty_prediction,excluded,error`, `eligible boolean NOT NULL` (`MOS-EVID-051`), `gt_voxels`, `pred_voxels`, `intersection_voxels` (`bigint NOT NULL`, `MOS-EVID-068`, so `dice_pooled` can be bootstrapped from rows), `gt_volume_ml`, `pred_volume_ml` (`double precision NOT NULL`, source geometry), `strata jsonb NOT NULL` (`MOS-EVID-067`); `CHECK (value IS NOT NULL OR undefined_reason IS NOT NULL)`; append-only | all |
| `evaluation_case_scores` | `PK (evaluation_run_id, case_key)` | `patient_key NOT NULL`, `case_score double precision` (continuous, pre-threshold), `case_label smallint CHECK (case_label IN (0,1))`, `candidates jsonb NOT NULL DEFAULT '[]'` (`MOS-EVID-069`); append-only | all |
| `deployment_gate_decisions` | `id`, index `(tenant_id, deployment_id, decided_at DESC)` | `deployment_id`, `capability_id`, `criteria_version`, `candidate_report_id` (FK `validation_reports`), `incumbent_report_id` (nullable — null on first deployment), `verdict` ∈ `PASS,FAIL,INDETERMINATE` (`MOS-EVID-083`), `criterion_results jsonb NOT NULL` (one entry per criterion, including `SKIPPED`), `decided_at`, `decided_by`, `override jsonb` (null unless `MOS-EVID-093` applies); append-only (`MOS-EVID-091`) | all |
| `validation_reports` | `UNIQUE (tenant_id, report_digest)`, `UNIQUE (tenant_id, subject_kind, subject_id, capability_id, kind, report_version)` | `kind` ∈ `vendor_evidence,site_acceptance,monitoring_period` (`MOS-EVID-003`), `schema_version`, `subject_kind` ∈ `service_version,model_version`, `subject_id`, `subject_version`, `capability_id`, `criteria_version`, `evaluation_run_ids uuid[] CHECK (cardinality >= 1)`, `acceptance_criteria_id`, `verdict` ∈ `PASS,FAIL,INDETERMINATE` (`MOS-EVID-083`, `MOS-EVID-142`; a two-valued pass/fail column cannot represent Chapter 7's verdict and MUST NOT be substituted), `report_bucket`, `report_object_key`, `report_digest`, `envelope_digest`, `bundle_bucket`, `bundle_object_key`, `bundle_digest`, `signature bytea`, `signer_key_id`, `approver jsonb NOT NULL` (`MOS-EVID-117`), `approver_user_id`, `approver_name`, `approver_role` (denormalised, `MOS-STORE-343`), `reproducibility_status` ∈ `verifiable,degraded`, `issued_at`, `valid_until`, `status` ∈ `ACTIVE,SUPERSEDED,REVOKED,EXPIRED` (default `ACTIVE`, `MOS-EVID-126`), `superseded_by`, `revocation_reason`; `CHECK (kind <> 'monitoring_period' OR verdict = 'INDETERMINATE')` (`MOS-EVID-142`) | all but `reproducibility_status`, `status`, `superseded_by`, `revocation_reason` |

Example `acceptance_criteria.spec` content, with real values, in the grammar of `MOS-EVID-076`:

```json
{
  "case_definition": {"positive_if": {"gt_volume_ml": {"op": ">", "value": 0.0}}},
  "strata": [
    {"id": "all", "selector": {}},
    {"id": "gt_positive", "selector": {"gt_volume_ml": {"op": ">", "value": 0.0}}},
    {"id": "small_effusion", "selector": {"gt_volume_ml": {"op": "<", "value": 100.0}}}
  ],
  "combine": "all_of",
  "absolute": [
    {"id": "sens_all", "metric": "sensitivity", "stratum": "all",
     "operating_threshold": {"name": "effusion_probability", "value": 0.50},
     "bound": "ci_lower_95", "op": ">=", "value": 0.90,
     "min_cases": 120, "min_patients": 100,
     "on_insufficient_cases": "indeterminate", "severity": "blocking"},
    {"id": "dice_positive", "metric": "dice_mean_per_case", "stratum": "gt_positive",
     "bound": "ci_lower_95", "op": ">=", "value": 0.80,
     "min_cases": 60, "min_patients": 50,
     "on_insufficient_cases": "indeterminate", "severity": "blocking"},
    {"id": "empty_gt_fp", "metric": "empty_gt_false_positive_rate", "stratum": "all",
     "bound": "point", "op": "<=", "value": 0.05,
     "min_cases": 30, "min_patients": 25,
     "on_insufficient_cases": "indeterminate", "severity": "blocking"}
  ],
  "regression": [
    {"id": "ni_dice_positive", "metric": "dice_mean_per_case", "stratum": "gt_positive",
     "margin": 0.02, "bound": "ci_lower_95",
     "min_cases": 60, "min_patients": 50,
     "on_insufficient_cases": "indeterminate", "severity": "blocking"}
  ]
}
```

**MOS-STORE-290** A `DatasetVersion` is sealed at creation, `manifest_digest` being computed over the canonical serialisation of the full `dataset_cases` set, so the version is content-addressed and two versions with the same digest contain the same instances; a correction is a new version with `parent_version_id` and `derivation` set (`MOS-EVID-022`).

**MOS-STORE-291** `dataset_cases` references DICOM UIDs, **not** foreign keys into `instances`. A dataset must stay valid and verifiable when the projection is rebuilt, when the data moves to a different PACS, when the manifest is exported for offline verification elsewhere, and when the source study has been purged from this deployment. A foreign key would couple the evidence plane's integrity to the imaging projection's lifecycle, which is exactly backwards: the evidence must outlive the projection.

**MOS-STORE-292** `erasure_state`, `usable_for_new_runs`, `status` and `defect_reason` are the only mutable columns on a sealed dataset version: the first two because an erasure cannot rewrite a sealed manifest (§12.16), the second two because a version found defective must be markable without being destroyed (`MOS-EVID-014`, `MOS-EVID-015`).

**MOS-STORE-292a** `dataset_versions.deidentification_status` and `patients.phi_state` are different vocabularies for different subjects and MUST NOT be conflated, notwithstanding that both contain the tokens `identified` and `pseudonymised`: the former records what was done to a sealed evidence manifest (`MOS-EVID-012`, `MOS-EVID-021`), the latter the live projection's PHI posture, and only the latter has the value `erased`. A migration MUST NOT map one onto the other and a query MUST NOT join on them.

**MOS-STORE-293** `PRIMARY KEY (split_id, patient_key)` makes patient-level leakage across partitions **structurally impossible** — a patient appears in exactly one partition — and the split is a frozen manifest of rows, never a random seed, so it is reproducible without re-running the generating code, on another machine, with a different RNG, in a different language (`MOS-EVID-028`).

**MOS-STORE-294** `partition_level` is constrained to `'patient'`; study-level splits are not supported, because the same patient's two studies landing in train and test is the most common and most invisible form of leakage in medical imaging evaluation (`MOS-EVID-029`).

**MOS-STORE-295** `consensus_rule`, `consensus_params` and `reader_count` are mandatory, and at least one `annotation_readers` row MUST exist: on LIDC-IDRI, whether a nodule is scored against one reader, a `majority_at_least_2` consensus, the union or STAPLE moves Dice more than most model changes, and a number reported without that binding is not interpretable. The six values are Chapter 7's (`MOS-EVID-039`) and the schema stores them unchanged.

**MOS-STORE-296** The empty-ground-truth policy is **not** a per-annotation-set column. It is fixed platform-wide as `exclude_and_report_separately` (`MOS-EVID-051`): a case with zero reference voxels has `value IS NULL`, `undefined_reason = 'empty_ground_truth'` and `eligible = false` in `evaluation_case_metrics`, and is counted instead in the empty-GT block of `MOS-EVID-052`, which is persisted as its own metric rows (`empty_gt_case_count`, `empty_gt_false_positive_rate`, `empty_gt_mean_fp_volume_ml`, `empty_gt_p95_fp_volume_ml`, `empty_gt_max_fp_volume_ml`). A column offering `score_one` or `score_zero` MUST NOT be added: the choice would make a published Dice a function of cohort composition, which is the defect `MOS-EVID-051` exists to prevent.

**MOS-STORE-297** `AcceptanceCriteria` belongs to the `Capability`, and `spec` carries the whole declarative document — strata, absolute criteria, regression criteria, per-criterion `operating_threshold`, `bound`, `margin`, `min_cases` and `severity` — in the grammar of `MOS-EVID-076`. The row MUST NOT reduce it to a flat metric list: a sensitivity figure without a stated operating point is not a measurement (`MOS-EVID-055`), and a bar without `bound` and `min_cases` is not a gate (`MOS-EVID-077`). Engineering bars — p95 latency, GPU ceiling, schema validity — live in `model_versions.engineering_bars` and MUST NOT appear here (`MOS-EVID-075`).

**MOS-STORE-297a** `tenant_acceptance_bindings` is the tenant's concrete cohort binding for a capability's criteria, one live row per `(tenant_id, capability_id)` (`MOS-EVID-080`). It is the only row in the evidence plane that is deliberately mutable, because a site rebinds as its cohort grows; `threshold_overrides` MAY only tighten (`MOS-EVID-081`) and a `DatasetVersion` whose `Dataset.purpose` is `training` or `tuning` MUST NOT be bound (`MOS-EVID-082`).

**MOS-STORE-298** Per-case metrics MUST be persisted, not only aggregates: without them no confidence interval can be recomputed, no paired non-inferiority test against a previous version is possible, and no subgroup analysis can be done after the fact (`MOS-EVID-065`). `gt_voxels`, `pred_voxels` and `intersection_voxels` are persisted for the same reason — `dice_pooled` has no per-case decomposition and can only be bootstrapped from the counts (`MOS-EVID-060`).

**MOS-STORE-299** `evaluation_case_scores.case_score` and `case_label` are persisted per case so a ROC or PR curve can be recomputed exactly and an operating threshold re-chosen **without re-running inference**; storing the per-case score is strictly more useful than storing a rendered curve and costs one `double precision` column. They live on `evaluation_case_scores`, not on `evaluation_case_metrics`, because a case has one score and many metrics (`MOS-EVID-069`).

**MOS-STORE-300** `metric_conventions` and `metric_registry_version` are mandatory on every run and MUST be reproduced in the validation report, because `dice_mean_per_case` and `dice_pooled` are different numbers on the same predictions and publishing one without saying which is publishing an uninterpretable figure (`MOS-EVID-047`, `MOS-EVID-048`). Both MUST be computed and persisted on every segmentation run, and every metric id MUST be present in the registry of `MOS-EVID-054`; a bare key named `dice` MUST fail validation (`MOS-EVID-049`).

**MOS-STORE-301** `run_digest` is computed over the canonical serialisation of the full binding — service version or model version, capability, dataset version digest, split digest, partition, annotation digest, preprocessing digest or internal pipeline digest, code commit, both image digests, operating thresholds, metric conventions — so two runs with the same digest are the same experiment, the `UNIQUE` constraint makes an accidental duplicate impossible, and the partial unique index over the same binding while `state = 'SUCCEEDED'` makes a second successful attempt a duplicate rather than a second opinion.

**MOS-STORE-301a** Marking a `dataset_versions` row `DEFECTIVE` MUST, in the same transaction, set `evaluation_runs.state = 'INVALIDATED'` with an `invalidation_reason` on every run bound to it, and `validation_reports.status = 'REVOKED'` with a `revocation_reason` on every report citing those runs (`MOS-EVID-014`). The cascade is a transaction, not a background job: a defective cohort must never leave a live report standing on it.

**MOS-STORE-302** A validation report is signed over its `report_digest` and is offline-verifiable, the document carrying the dataset version digest, split digest, annotation digest, preprocessing digest, image digests and run digest, so a receiving site can verify the claim without access to this database. `reproducibility_status` exists solely so an erasure can mark a report whose cohort can no longer be re-read, and is mutable together with the standing columns named in `MOS-STORE-302a`. The column names are Chapter 7's — `report_digest`, `envelope_digest`, `bundle_digest`, `signature`, `signer_key_id` (§7.12.1) — and the earlier `document_digest`, `document_bucket`, `document_object_key`, `signature_alg`, `signer_identity` and `signed_at` spellings are superseded: they MUST NOT appear in the DDL, in the `MOS-STORE-228` column-level revocation, or in generated code.

**MOS-STORE-302a** `status` and `verdict` are distinct and MUST NOT be merged: `verdict` is the evidential outcome, frozen at issuance and covered by the signature; `status` is the report's standing and is the only lifecycle column, mutable only through the transitions of `MOS-EVID-126`. Revocation MUST NOT alter the report's bytes, so `report_digest` and `signature` survive it unchanged.

**MOS-STORE-302b** A published performance figure exists only as a `capability_claims` row carrying its interval, its cohort size and its `evaluation_run_id` (`MOS-EVID-071`, `MOS-EVID-072`); `model_versions.primary_evaluation_run_id` is a convenience pointer to the run behind the primary claim, never an alternative source of a number, and no free-form metrics map may exist on a version row. Every gate invocation is persisted as a `deployment_gate_decisions` row whatever its outcome (`MOS-EVID-091`), including the `SKIPPED` criteria, so that "why was this deployment allowed" is answerable from rows. An `override` is writable only by a principal holding `deployment.gate.override` — Chapter 8 §8.3.2's spelling, class `governance`, and a legal three-segment identifier under the widened `MOS-SEC-031` — and only against an `INDETERMINATE` verdict, never a `FAIL` (`MOS-EVID-093`). Both tables are append-only under `MOS-STORE-228` and MUST be added to its table-level revocation list.

**MOS-STORE-303** The three kinds of evidence are separate rows and separate `evaluation_runs.kind` values, never one conflated record: `vendor_evidence` is produced once and is portable, `site_acceptance` is produced at install in hours, `monitoring_period` is continuous. There is no fourth kind (`MOS-EVID-003`). A `site_acceptance` report MUST NOT be presented as clinical validation, and the platform performs no clinical validation of its own (Chapter 9 is normative).

#### 12.12.1 The training corpus

Chapter 17 assembles training cohorts out of the platform's own traffic, and six of its records had no storage here. They belong in the evidence plane rather than beside it: a `DatasetVersion` sealed from a harvest is worth exactly as much as the record of how its cases were drawn and who decided to keep them, and `MOS-TRAIN-081` and `MOS-TRAIN-088` both require that record to be reproduced in a `ValidationReport` citing the cohort. All six tables are **tenant-owned** — `MOS-TRAIN-071` forbids pooling studies across tenants into one batch — and all six take the standard §12.5 set: `tenant_id uuid NOT NULL`, `ENABLE` and `FORCE ROW LEVEL SECURITY`, a tenant-isolation policy, `UNIQUE (tenant_id, id)`, composite foreign keys, the `forbid_column_change('tenant_id')` trigger of `MOS-STORE-218` and the grants of `MOS-STORE-325` as narrowed by `MOS-STORE-228`. Chapter 17 is normative for every field name, value set and rule below; this chapter is normative for the physical form (`MOS-STORE-201`).

**MOS-STORE-358** These tables render Chapter 17's `TrainingDataPolicy` (`MOS-TRAIN-073`), `SamplingPlan` (`MOS-TRAIN-083`), `HarvestBatch`, `HarvestCandidate` (`MOS-TRAIN-079`), `CurationDecision` (`MOS-TRAIN-080`) and `CorpusStratificationReport` (`MOS-TRAIN-088`), and `tenants.training_use_allowed` (`MOS-TRAIN-072`) is the flag they gate. Four rules are structural here rather than procedural, and the schema carries them. `training_use_allowed` MUST NOT be true while the tenant has no live `training_data_policies` row — unrevoked, and either open-ended or unexpired — which is why the flag carries a deferred constraint trigger rather than a CHECK, and why revoking the instrument writes the flag back to false in the same transaction (`MOS-TRAIN-075`, `MOS-TRAIN-076`); the expiry half of `MOS-TRAIN-075` is the ordinary tenant-iterating sweep of `MOS-STORE-333`. A `harvest_batches` row references its `sampling_plan_id` `NOT NULL` at insert, which is what `MOS-TRAIN-083`'s "before any candidate is drawn" means once candidates are rows. `harvest_candidates` carries UIDs, hashes and keys only: `MOS-TRAIN-079` forbids `PatientName`, `PatientBirthDate`, `AccessionNumber`, institution free text and every source-space UID, so `MOS-STORE-272`'s schema-review rule and CI grep extend to this table and `institution_key` is `MOS-TRAIN-089`'s HMAC, never a name. And `corpus_generation` appears twice — on `harvest_candidates` and on `dataset_cases` — with deliberately different CHECKs: a candidate may be generation 2 and be excluded *for* it, a sealed case may not be generation 2 at all (`MOS-TRAIN-087`).

```sql
CREATE TABLE training_data_policies (          -- ch. 17 `TrainingDataPolicy`, MOS-TRAIN-073
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  legal_basis text NOT NULL CHECK (legal_basis IN ('broad_consent',
    'research_ethics_approval','public_corpus_licence','data_processing_agreement',
    'national_derogation')),
  basis_reference text NOT NULL CHECK (length(basis_reference) > 0),
  basis_document_digest sha256_digest,
  scope jsonb NOT NULL
    CHECK (scope ?& array['modalities','body_parts','capabilities','date_from','date_to']),
  permits_redistribution boolean NOT NULL DEFAULT false,
  recorded_by uuid NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  revoked_at timestamptz, revocation_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT training_data_policies_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT training_data_policies_recorder_fk FOREIGN KEY (tenant_id, recorded_by)
    REFERENCES users (tenant_id, id),
  CHECK ((revoked_at IS NULL) = (revocation_reason IS NULL))
);
-- MOS-TRAIN-073 writes the key as `tenant_id PRIMARY KEY`. Under MOS-STORE-208 that is
-- rendered as a surrogate key plus this partial unique index: one live instrument per
-- tenant, and a withdrawn one stays readable rather than being overwritten, which is what
-- MOS-TRAIN-076 needs in order to say which candidates predate a revocation.
CREATE UNIQUE INDEX training_data_policies_live_uk ON training_data_policies (tenant_id)
  WHERE revoked_at IS NULL;
CREATE TRIGGER training_data_policies_touch BEFORE UPDATE ON training_data_policies
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- MOS-TRAIN-072 and MOS-TRAIN-073 are one fact held in two tables, so the flag and the
-- instrument are bound in both directions. Both triggers are created here rather than in
-- §12.6, because `training_data_policies` does not exist until this migration.
CREATE FUNCTION assert_training_policy_live() RETURNS trigger AS $$
BEGIN
  IF NEW.training_use_allowed AND NOT EXISTS (
       SELECT 1 FROM training_data_policies p
        WHERE p.tenant_id = NEW.id
          AND p.revoked_at IS NULL
          AND (p.expires_at IS NULL OR p.expires_at > now())) THEN
    RAISE EXCEPTION
      'training_use_allowed requires a live TrainingDataPolicy (MOS-TRAIN-073)'
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

-- DEFERRED, so the flag and its instrument may be written in one transaction.
CREATE CONSTRAINT TRIGGER tenants_training_policy_required
  AFTER INSERT OR UPDATE OF training_use_allowed ON tenants
  DEFERRABLE INITIALLY DEFERRED
  FOR EACH ROW EXECUTE FUNCTION assert_training_policy_live();

CREATE FUNCTION clear_training_use_on_revocation() RETURNS trigger AS $$
BEGIN
  IF NEW.revoked_at IS NOT NULL AND OLD.revoked_at IS NULL THEN
    UPDATE tenants SET training_use_allowed = false, updated_at = now()
     WHERE id = NEW.tenant_id AND training_use_allowed;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER training_data_policies_revocation
  AFTER UPDATE OF revoked_at ON training_data_policies
  FOR EACH ROW EXECUTE FUNCTION clear_training_use_on_revocation();

CREATE TABLE sampling_plans (                  -- ch. 17 `SamplingPlan`, MOS-TRAIN-083
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id uuid NOT NULL REFERENCES capabilities(id),
  plan_version integer NOT NULL CHECK (plan_version >= 1),
  -- One key per stratification dimension. MOS-TRAIN-083 fixes the minimum four and owns
  -- each dimension's sampling rule, which is stored under its key verbatim.
  strata jsonb NOT NULL CHECK (strata ?& array['score_band','review_outcome',
                                               'ran_on_platform','acquisition_bucket']),
  min_naive_fraction numeric(4,3) NOT NULL DEFAULT 0.200
    CHECK (min_naive_fraction >= 0 AND min_naive_fraction <= 1),
  de_novo_control_fraction numeric(4,3) NOT NULL DEFAULT 0.100
    CHECK (de_novo_control_fraction >= 0.100),            -- MOS-TRAIN-101
  spec_digest sha256_digest NOT NULL,
  sealed_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT sampling_plans_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT sampling_plans_uk UNIQUE (tenant_id, capability_id, plan_version)
);
-- A plan is declared before the draw and never edited after it; a new plan is a new
-- `plan_version` row, exactly as a `deid_policies` row is (MOS-STORE-251).
CREATE TRIGGER sampling_plans_sealed BEFORE UPDATE ON sampling_plans
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE harvest_batches (                 -- ch. 17 `HarvestBatch`
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  capability_id uuid NOT NULL REFERENCES capabilities(id),
  -- NOT NULL at insert: MOS-TRAIN-083 requires the plan to be declared before any
  -- candidate is drawn, and a nullable column would make that a convention.
  sampling_plan_id uuid NOT NULL,
  training_data_policy_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'OPEN'
    CHECK (state IN ('OPEN','SAMPLED','CURATING','SEALED','ABANDONED')),
  candidate_count integer NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
  dataset_version_id uuid,                     -- set when MOS-TRAIN-082 seals from it
  opened_by uuid NOT NULL,
  opened_at timestamptz NOT NULL DEFAULT now(),
  sealed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT harvest_batches_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT harvest_batches_plan_fk FOREIGN KEY (tenant_id, sampling_plan_id)
    REFERENCES sampling_plans (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT harvest_batches_policy_fk FOREIGN KEY (tenant_id, training_data_policy_id)
    REFERENCES training_data_policies (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT harvest_batches_dataset_version_fk
    FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT harvest_batches_opener_fk FOREIGN KEY (tenant_id, opened_by)
    REFERENCES users (tenant_id, id),
  CHECK ((state = 'SEALED') = (sealed_at IS NOT NULL)),
  CHECK (state <> 'SEALED' OR dataset_version_id IS NOT NULL)
);
CREATE TRIGGER harvest_batches_touch BEFORE UPDATE ON harvest_batches
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER harvest_batches_sealed BEFORE UPDATE ON harvest_batches
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','capability_id',
    'sampling_plan_id','training_data_policy_id','opened_by','opened_at');

CREATE TABLE harvest_candidates (              -- ch. 17 `HarvestCandidate`, MOS-TRAIN-079
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  harvest_batch_id uuid NOT NULL,
  patient_key text NOT NULL,                   -- MOS-EVID-010; never a patient identifier
  -- De-identified UID space only. MOS-TRAIN-079 forbids a source-space UID here, and
  -- MOS-STORE-272's PHI rule and CI grep extend to this table (MOS-STORE-358).
  study_instance_uid dicom_uid NOT NULL,
  series_instance_uids dicom_uid[] NOT NULL
    CHECK (cardinality(series_instance_uids) >= 1),
  instance_uids dicom_uid[] NOT NULL,
  geometry jsonb NOT NULL,                     -- the MOS-IMG-036 descriptor
  acquisition_profile jsonb NOT NULL           -- MOS-TRAIN-084, copied without imputation
    CHECK (acquisition_profile ?& array['manufacturer','manufacturer_model_name',
      'convolution_kernel','convolution_kernel_class','slice_thickness_mm',
      'pixel_spacing_mm','kvp','exposure_mas','contrast_phase',
      'iterative_recon_strength','station_key','institution_key','study_year']),
  institution_key text NOT NULL,               -- MOS-TRAIN-089 HMAC; never the raw name
  deid_policy_version integer NOT NULL,
  ran_on_platform boolean NOT NULL,
  -- The terminal `jobs.state` values (MOS-STORE-266) plus `NOT_APPLICABLE` from
  -- `triage_decision.outcome` (MOS-DATA-078). No value is invented here.
  platform_outcome text CHECK (platform_outcome IN
    ('COMPLETED','FAILED','CANCELLED','REJECTED','NOT_APPLICABLE')),
  review_outcome text CHECK (review_outcome IN ('UNREVIEWED','PENDING','IN_REVIEW',
    'ACCEPTED','MODIFIED','REJECTED','EXPIRED','SUPERSEDED')),  -- results.review_status
  score_band text, acquisition_bucket text,    -- the sampling plan's own bucket labels
  sampling_weight numeric(9,6) NOT NULL CHECK (sampling_weight > 0),   -- MOS-TRAIN-083
  corpus_generation smallint NOT NULL DEFAULT 0
    CHECK (corpus_generation BETWEEN 0 AND 2),                         -- MOS-TRAIN-087
  auto_excluded_predicate_id text,
  auto_excluded_predicate_digest sha256_digest,                        -- MOS-TRAIN-080
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT harvest_candidates_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT harvest_candidates_uk UNIQUE (harvest_batch_id, study_instance_uid),
  CONSTRAINT harvest_candidates_batch_fk FOREIGN KEY (tenant_id, harvest_batch_id)
    REFERENCES harvest_batches (tenant_id, id) ON DELETE CASCADE,
  CHECK (ran_on_platform = (platform_outcome IS NOT NULL)),
  CHECK ((auto_excluded_predicate_id IS NULL) = (auto_excluded_predicate_digest IS NULL))
);
CREATE INDEX harvest_candidates_batch_idx
  ON harvest_candidates (tenant_id, harvest_batch_id, patient_key);
CREATE TRIGGER harvest_candidates_sealed BEFORE UPDATE ON harvest_candidates
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change('tenant_id','harvest_batch_id',
    'patient_key','study_instance_uid','sampling_weight');

CREATE TABLE curation_decisions (              -- ch. 17 `CurationDecision`, MOS-TRAIN-080
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  candidate_id uuid NOT NULL,
  decision text NOT NULL CHECK (decision IN ('include','exclude','defer')),
  reason_code text CHECK (reason_code IN ('quality_artefact','wrong_anatomy','wrong_phase',
    'prior_treatment','duplicate_patient','geometry_unsupported','annotation_infeasible',
    'out_of_scope')),
  note text,                                   -- reviewer note; no PHI (MOS-STORE-272)
  review_seconds integer NOT NULL CHECK (review_seconds >= 0),
  decided_by uuid NOT NULL,
  decided_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT curation_decisions_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT curation_decisions_candidate_fk FOREIGN KEY (tenant_id, candidate_id)
    REFERENCES harvest_candidates (tenant_id, id) ON DELETE CASCADE,
  CONSTRAINT curation_decisions_decider_fk FOREIGN KEY (tenant_id, decided_by)
    REFERENCES users (tenant_id, id),
  CHECK ((decision = 'exclude') = (reason_code IS NOT NULL))
);
-- `defer` is not terminal, so a candidate may accumulate deferrals and then settle once.
-- MOS-TRAIN-080 forbids auto-inclusion, so there is no default row: a candidate with no
-- settled decision is simply not in a cohort, and the absence is the queue's backlog.
CREATE UNIQUE INDEX curation_decisions_settled_uk ON curation_decisions (candidate_id)
  WHERE decision <> 'defer';

CREATE TABLE corpus_stratification_reports (   -- ch. 17 MOS-TRAIN-088
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  harvest_batch_id uuid NOT NULL,
  dataset_version_id uuid,
  verdict text NOT NULL CHECK (verdict IN ('pass','warn','fail')),
  -- One entry per check C1…C7, each carrying its statistic, its bound and its outcome.
  checks jsonb NOT NULL CHECK (checks ?& array['C1','C2','C3','C4','C5','C6','C7']),
  computed_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT corpus_stratification_reports_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT corpus_stratification_reports_batch_fk
    FOREIGN KEY (tenant_id, harvest_batch_id)
    REFERENCES harvest_batches (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT corpus_stratification_reports_dataset_version_fk
    FOREIGN KEY (tenant_id, dataset_version_id)
    REFERENCES dataset_versions (tenant_id, id) ON DELETE RESTRICT
);
CREATE TRIGGER corpus_stratification_reports_append_only
  BEFORE UPDATE OR DELETE ON corpus_stratification_reports
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

**MOS-STORE-359** `curation_decisions` and `corpus_stratification_reports` are append-only and are on `MOS-STORE-228`'s table-level revocation list: the first is the record of a named human's decision about a case (`MOS-TRAIN-080`), the second is the record that a cohort passed the check which permitted it to seal and whose `fail` would have blocked it (`MOS-TRAIN-088`), and an editable version of either is not a record. `sampling_plans` is sealed outright, because a plan edited after the draw cannot describe the draw. `harvest_batches` and `harvest_candidates` stay mutable — a batch advances through its states and `MOS-TRAIN-080` permits auto-exclusion to annotate a candidate after it is drawn — but both seal their identity columns with `forbid_column_change`, `harvest_candidates.sampling_weight` included: a realised sampling weight that can be edited after the fact makes `MOS-TRAIN-083`'s stratification unauditable, which is the one thing the whole subsection exists to prevent.

### 12.13 Governance: audit, policy sets, policy decisions

Chapter 8 is normative for the `AuditEvent` and `PolicyDecision` field sets, their value spaces and their field names (`MOS-SEC-146`, `MOS-SEC-067`); this section is their physical rendering — `bytea` digests, monthly partitions, generated columns — and nothing below may narrow a Chapter 8 value set.

```sql
CREATE TABLE audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY,
  public_id text NOT NULL,               -- 'aud_' || ULID; Chapter 8's audit_id
  tenant_id uuid NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  chain_period date GENERATED ALWAYS AS
    (date_trunc('month', occurred_at AT TIME ZONE 'UTC')::date) STORED,
  seq bigint NOT NULL,                   -- per-tenant, gapless, MOS-SEC-146/151
  actor_kind text NOT NULL CHECK (actor_kind IN
    ('user','service_account','workload','service_version','platform_admin')),
  actor_id text NOT NULL,
  actor_auth text, actor_key_id text,
  on_behalf_of_kind text CHECK (on_behalf_of_kind IN ('user','service_account')),
  on_behalf_of_id uuid,
  on_behalf_of_role text,
  action text NOT NULL,                  -- a permission id from medos/contracts/permissions.yaml
  resource_kind text NOT NULL, resource_id text NOT NULL, resource_version text,
  outcome text NOT NULL CHECK (outcome IN ('allow','deny','error')),
  policy_decision_id bigint,
  pep text NOT NULL,
  break_glass_id uuid,
  job_id uuid, root_job_id uuid,
  trace_id text NOT NULL, span_id text, request_id text NOT NULL,
  -- Nullable, deliberately. Ch. 8 MOS-SEC-146 owns this field and its audit field table
  -- marks it required; that can only mean an externally-originated action, because the
  -- retention sweep, the projection reconciler and the outbox relay all write audit rows
  -- under MOS-STORE-225 and have no source address at all. NULL is the record that no
  -- external address existed. A platform-node constant would be worse than absence: a
  -- fabricated address is indistinguishable from a real connection, which is exactly the
  -- confusion an audit trail exists to prevent. Ch. 8 MUST qualify the requirement to
  -- externally-originated actions; this chapter does not invent a constant.
  source_ip inet, user_agent text CHECK (length(user_agent) <= 200),
  patient_id uuid,                       -- surrogate only; never demographics
  study_instance_uid dicom_uid,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  prev_hash bytea NOT NULL, hash bytea NOT NULL,
  PRIMARY KEY (id, occurred_at),
  UNIQUE (tenant_id, seq, occurred_at),
  UNIQUE (public_id, occurred_at)
) PARTITION BY RANGE (occurred_at);

CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;
CREATE TABLE audit_events_2026_09 PARTITION OF audit_events
  FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');

CREATE INDEX audit_events_policy_idx ON audit_events (tenant_id, policy_decision_id)
  WHERE policy_decision_id IS NOT NULL;
CREATE INDEX audit_events_seq_idx ON audit_events (tenant_id, seq DESC);

CREATE TABLE audit_chain_checkpoints (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  chain_period date NOT NULL,
  seq_start bigint NOT NULL, seq_end bigint NOT NULL,
  last_hash bytea NOT NULL, entry_count bigint NOT NULL,
  merkle_root bytea NOT NULL,
  signing_key_id text NOT NULL,
  signature bytea NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  exported_object_key text,
  CONSTRAINT audit_chain_checkpoints_uk UNIQUE (tenant_id, seq_start),
  CHECK (seq_end >= seq_start)
);

CREATE TABLE audit_archives (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  chain_period date NOT NULL,
  object_key text NOT NULL, archive_digest sha256_digest NOT NULL,
  signature bytea NOT NULL, signing_key_id text NOT NULL,
  entry_count bigint NOT NULL,
  detached_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT audit_archives_uk UNIQUE (tenant_id, chain_period)
);

CREATE TABLE policy_activations (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  policy_set_digest sha256_digest NOT NULL,
  previous_digest sha256_digest,
  mode text NOT NULL CHECK (mode IN ('shadow','enforce')),
  effective_from timestamptz NOT NULL DEFAULT now(),
  reason text NOT NULL,
  analysis_ref text,                     -- the MOS-SEC-064 shadow or SMT evidence
  activated_by uuid NOT NULL,
  activated_at timestamptz NOT NULL DEFAULT now(),
  superseded_at timestamptz,
  CONSTRAINT policy_activations_tenant_id_uk UNIQUE (tenant_id, id)
);

CREATE FUNCTION audit_chain() RETURNS trigger AS $$
DECLARE
  prev bytea; prevseq bigint;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text, 0));
  SELECT a.hash, a.seq INTO prev, prevseq FROM audit_events a
   WHERE a.tenant_id = NEW.tenant_id
   ORDER BY a.seq DESC LIMIT 1;
  IF prevseq IS NULL THEN
    -- the tenant's genesis constant (MOS-SEC-151)
    prev := digest(convert_to('MOS-AUDIT-GENESIS-v1|' || NEW.tenant_id::text, 'UTF8'),
                   'sha256');
    prevseq := 0;
  END IF;
  NEW.prev_hash := prev;
  NEW.seq := prevseq + 1;
  -- SHA-256 over the RFC 8785 canonical JSON of the row with `hash` omitted and
  -- `prev_hash` included (MOS-SEC-151). `chain_period` is generated and is excluded;
  -- it is a pure function of `occurred_at`, which is hashed.
  NEW.hash := digest(jcs_canonical(to_jsonb(NEW) - 'hash' - 'chain_period'), 'sha256');
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER audit_events_chain BEFORE INSERT ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_chain();

CREATE TABLE policy_decisions (
  id bigint GENERATED ALWAYS AS IDENTITY,
  public_id text NOT NULL,               -- 'pd_' || ULID; Chapter 8's decision_id
  tenant_id uuid NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  mode text NOT NULL CHECK (mode IN ('shadow','enforce')),
  engine text NOT NULL, engine_version text NOT NULL,
  policy_set_digest sha256_digest NOT NULL,
  policy_set_version text NOT NULL,
  pep text NOT NULL,
  principal text NOT NULL,
  on_behalf_of text,
  action text NOT NULL,
  resource text NOT NULL,
  rbac_granted boolean NOT NULL,
  effect text NOT NULL CHECK (effect IN ('permit','forbid')),
  reason_code text NOT NULL,
  determining_policies text[] NOT NULL DEFAULT '{}',
  errors jsonb NOT NULL DEFAULT '[]'::jsonb,
  context jsonb NOT NULL DEFAULT '{}'::jsonb,   -- loggable keys only, MOS-SEC-069
  context_digest sha256_digest NOT NULL,
  break_glass_id uuid,
  job_id uuid, trace_id text, request_id text,
  latency_us integer NOT NULL,
  sampled boolean NOT NULL DEFAULT false,
  PRIMARY KEY (id, occurred_at),
  UNIQUE (public_id, occurred_at),
  CHECK (cardinality(determining_policies) > 0
         OR (effect = 'forbid'
             AND reason_code IN ('no_matching_policy','policy_unavailable','rbac_denied')))
) PARTITION BY RANGE (occurred_at);

CREATE TABLE policy_decisions_default PARTITION OF policy_decisions DEFAULT;
CREATE TABLE policy_decisions_2026_09 PARTITION OF policy_decisions
  FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');
```

There is **no `policies` table**. Policy rule text lives only inside the signed, immutable `PolicySet` bundle of `MOS-SEC-062`, and `policy_activations` is the only per-tenant policy state (`MOS-SEC-063`). A schema that stored editable policy rows would make `MOS-SEC-071`'s replay guarantee unenforceable, because the text that produced a decision could be edited afterwards.

**MOS-STORE-304** `audit_events` is append-only at the grant level (MOS-STORE-228) and tamper-evident by hash chain from 0.2.0. The field set, the `outcome` values `allow`/`deny`/`error` and the obligation to carry `policy_decision_id`, `pep`, `request_id` and `break_glass_id` are Chapter 8's `MOS-SEC-146`, `MOS-SEC-148` and `MOS-SEC-081`, mirrored here name for name; `seq` is the per-tenant gapless sequence of `MOS-SEC-146`, and `prev_hash`/`hash` are the chain columns of `MOS-SEC-151`, rendered as `bytea` because a digest is bytes. `hash` covers the RFC 8785 canonical JSON of the whole row, not a hand-picked subset, so tampering with `pep`, `source_ip` or `detail` is detected too. A checkpoint is written every 10 000 events or every 24 hours, whichever comes first (`MOS-SEC-152`), carries a `merkle_root` and a platform-key `signature`, and is exportable — without it a site cannot verify its own history offline; a detached archive keeps its own signed digest row in `audit_archives` (`MOS-SEC-154`). Signing the model artifact while leaving the claims about it in plain mutable rows would be exactly backwards.

**MOS-STORE-305** Partitions MUST be monthly and aligned to UTC month boundaries, because `chain_period` derives from `occurred_at` and is the unit in which a partition is sealed, exported and detached (`MOS-SEC-154`); partitions MUST be created at least 60 days ahead by the maintenance task, and `audit_events_default` is a backstop that MUST be empty — a non-empty default partition is an operational alert. Note that `UNIQUE (tenant_id, seq, occurred_at)` is a partition-local backstop only: PostgreSQL requires the partition key in a unique constraint, so global gaplessness of `seq` is enforced by the advisory lock in `audit_chain()` and verified by acceptance criterion 9, not by the constraint.

**MOS-STORE-306** An audit row MUST be joinable to the execution that caused it, so `job_id`, `root_job_id`, `trace_id`, `span_id` and `request_id` are present, and to the authorisation that permitted it, so `policy_decision_id` is present and indexed — the join Chapter 8 `MOS-SEC-155` requires. `actor_kind = 'service_version'` with `on_behalf_of_id` and `on_behalf_of_kind` set expresses "this service version, acting for this user, in this job", the only form that answers an access question; a flat actor string MUST NOT be used (`MOS-SEC-147`).

**MOS-STORE-307** `audit_events` MUST NOT contain patient demographics, carrying only the surrogate `patient_id` and, for imaging access, `study_instance_uid`. This is what lets the trail survive an erasure intact: afterwards the surrogate resolves to a tombstone and the record still shows that an access occurred, without re-identifying anyone.

**MOS-STORE-308** A failed audit write blocks the audited action: where the action occurs in a database transaction the audit row is written in that same transaction, so if the insert fails the transaction rolls back and the action does not happen (`MOS-SEC-149`). There is no fire-and-forget audit path and no audit queue. Where the action crosses a boundary with no shared transaction — a PACS read, a service invocation — the authorisation row MUST be committed **before** the upstream call and the effect row written after it, the two joined by `request_id` (`MOS-SEC-150`); append-only storage forbids updating the first, which is why the pair is the mechanism.

**MOS-STORE-309** Policy evaluation is fail-closed and defaults to deny: a decision with no `determining_policies` MUST carry `effect = 'forbid'` and `reason_code` ∈ `no_matching_policy`, `policy_unavailable`, `rbac_denied` (`MOS-SEC-057`), the CHECK making an unattributed `permit` unrepresentable. `effect` uses Cedar's vocabulary — `permit` and `forbid`, never `allow`/`deny`, which are `audit_events.outcome`'s values — because `policy_decisions` is replayed against the engine (`MOS-SEC-071`) and a renamed effect would not compare.

**MOS-STORE-310** A decision MUST be persisted when its `effect` is `forbid`, when the action's permission class is `phi`, `clinical`, `admin` or `governance`, when `mode = 'shadow'`, or when the tenant's decision sampling rate selects it (`MOS-SEC-067`); sampled rows carry `sampled = true`. Every persisted decision is retained for at least two years (`MOS-SEC-070`), which is why the partition key is `occurred_at` and why there is no shorter retention class for permits.

**MOS-STORE-311** A `PolicySet` is an immutable, signed artifact addressed by the SHA-256 of its canonical tar, and its rule text MUST NOT be stored as editable database rows (`MOS-SEC-062`). Activation is append-only: a change is a new `policy_activations` row naming the new digest, and a rollback is another activation naming the older digest (`MOS-SEC-063`). A `policy_decisions` row therefore always resolves, through `policy_set_digest` plus `determining_policies`, to the exact rule text that produced it, which is what makes the replay test of `MOS-SEC-071` executable.

### 12.14 Object store layout

**MOS-STORE-312** Four buckets exist; names are configurable by prefix, the roles are not.

| Role | Default name | Contents | Versioning | Object lock | Encryption |
|---|---|---|---|---|---|
| artifacts | `medicalos-artifacts` | service / model / preprocessing bundles, policy-set bundles (`MOS-SEC-062`), manifests, signatures, SBOMs | on | governance | SSE-KMS |
| execution | `medicalos-execution` | canonical volumes, label maps, probability maps, result bundles, logs | off | none | SSE-KMS |
| evidence | `medicalos-evidence` | dataset manifests, case lists, split manifests, annotations, per-case metrics, reports, report bundles, audit exports | on | compliance | SSE-KMS |
| exports | `medicalos-exports` | user-initiated exports and report downloads | off | none | SSE-KMS |

**MOS-STORE-313** Path grammar. Every key matches exactly one of these forms; `{sha256}` is the 64-character hex digest without the `sha256:` prefix and `{sha12}` its first 12 characters.

```
artifacts/{kind}/{sha256}/bundle.tar.zst
artifacts/{kind}/{sha256}/manifest.json
artifacts/{kind}/{sha256}/signature.sig
artifacts/{kind}/{sha256}/sbom.spdx.json
artifacts/preprocessing/{sha256}/golden/input.nii.gz
artifacts/preprocessing/{sha256}/golden/expected_tensor.sha256

t/{tenant_id}/jobs/{job_id}/steps/{step_index}/{artifact_kind}-{sha12}.nii.gz
t/{tenant_id}/jobs/{job_id}/bundle/result-bundle-{sha12}.json.zst
t/{tenant_id}/jobs/{job_id}/logs/{step_index}-{sha12}.log.zst

t/{tenant_id}/datasets/{dataset_id}/v{version}/manifest.json
t/{tenant_id}/datasets/{dataset_id}/v{version}/cases.ndjson.zst
t/{tenant_id}/splits/{split_id}/members.ndjson
t/{tenant_id}/annotations/{annotation_set_id}/manifest.ndjson
t/{tenant_id}/annotations/{annotation_set_id}/reference/{case_key}/{series_uid}.{ext}
t/{tenant_id}/annotations/{annotation_set_id}/readers/{reader_id}/{case_key}/{series_uid}.{ext}
t/{tenant_id}/evaluations/{evaluation_run_id}/per_case.parquet
t/{tenant_id}/evaluations/{evaluation_run_id}/run.json
t/{tenant_id}/validation-reports/{report_id}/report.pdf
t/{tenant_id}/validation-reports/{report_id}/report.json
t/{tenant_id}/validation-reports/{report_id}/report.sig
t/{tenant_id}/validation-reports/{report_id}/bundle.tar.gz
t/{tenant_id}/audit/{chain_period}/entries.ndjson.zst

t/{tenant_id}/exports/{export_id}/{filename}
```

**MOS-STORE-314** Every tenant-scoped key MUST begin with the literal segments `t/{tenant_id}/`, which makes bucket-policy tenant isolation expressible as a prefix condition, makes a per-tenant purge a prefix delete, and makes a misplaced object visible by inspection.

**MOS-STORE-315** Object keys MUST NOT contain PHI — no patient names, medical record numbers, accession numbers or dates; pseudonymous UIDs, case keys and surrogate ids are permitted. Keys appear in logs, bucket listings and error messages, none of which are PHI-cleared surfaces.

**MOS-STORE-316** Every object referenced from Postgres MUST have a `blobs` row, and deletion is reference-counted.

```sql
CREATE TABLE blobs (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid REFERENCES tenants(id),
  bucket text NOT NULL, object_key text NOT NULL,
  content_digest sha256_digest NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  content_type text NOT NULL,
  encryption text NOT NULL CHECK (encryption IN ('sse-kms','sse-s3')),
  immutable boolean NOT NULL DEFAULT false,
  ref_count integer NOT NULL DEFAULT 0 CHECK (ref_count >= 0),
  expires_at timestamptz, deleted_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT blobs_key_uk UNIQUE (bucket, object_key),
  CHECK (NOT immutable OR expires_at IS NULL)
);
CREATE INDEX blobs_gc_idx ON blobs (expires_at)
  WHERE deleted_at IS NULL AND ref_count = 0 AND NOT immutable;
```

**MOS-STORE-317** A blob is deletable only when `ref_count = 0`, `NOT immutable`, `expires_at < now()` and no `legal_holds` row covers its tenant and class; the collector MUST delete the object first and set `deleted_at` second, because the reverse order loses the object's location if the collector crashes between the two.

**MOS-STORE-318** The artifacts bucket is content-addressed and shared across tenants — two tenants using the same public model bundle reference one object — so its blob rows have `tenant_id IS NULL` and `immutable = true`, and a tenant deletion MUST NOT delete artifact objects.

### 12.15 Migration policy

**MOS-STORE-319** Migrations are **forward-only, numbered and immutable once merged**, named `migrations/NNNN_snake_case_name.up.sql`; a merged file MUST NOT be edited, its digest is recorded in `schema_migrations.file_digest`, and a mismatch on startup is fatal rather than a warning.

**MOS-STORE-320** `.down.sql` files MAY exist for local development only and MUST NOT run against staging or production: recovery from a bad migration is roll-forward plus point-in-time restore, and a down migration that drops a column discards data the roll-forward path would have preserved.

**MOS-STORE-321** Each migration runs in one transaction; one that cannot — `CREATE INDEX CONCURRENTLY`, a partition detach — MUST carry `-- medicalos:no-transaction` on its first line and MUST be idempotent, because it may be re-run after a partial failure.

**MOS-STORE-322** Every migration begins with `SET lock_timeout = '3s';`, so a migration that cannot take its lock fails fast rather than queueing behind a long query and blocking every subsequent statement on the table.

**MOS-STORE-323** Schema changes follow **expand / migrate / contract**, and no release performs two phases on the same column.

| Phase | Release | Action |
|---|---|---|
| Expand | N | add the new column or table, nullable or defaulted; write both, read the old |
| Migrate | N | batched, idempotent, resumable backfill, in a separate file from the DDL |
| Switch | N+1 | read the new; still write the old |
| Contract | N+2 | stop writing the old; drop it in a migration of its own |

**MOS-STORE-324** Adding `NOT NULL` to a populated column MUST use the three-step form, which avoids a full-table `ACCESS EXCLUSIVE` scan:

```sql
ALTER TABLE results ADD CONSTRAINT results_bundle_digest_nn
  CHECK (bundle_digest IS NOT NULL) NOT VALID;
ALTER TABLE results VALIDATE CONSTRAINT results_bundle_digest_nn;
ALTER TABLE results ALTER COLUMN bundle_digest SET NOT NULL;
ALTER TABLE results DROP CONSTRAINT results_bundle_digest_nn;
```

**MOS-STORE-325** A migration creating a tenant-owned table MUST, in the same file, add `tenant_id uuid NOT NULL`, the composite unique key `(tenant_id, id)`, `ENABLE` and `FORCE ROW LEVEL SECURITY`, the tenant policy, the `tenant_id` immutability trigger and the grants; CI rejects a migration omitting any of them.

**MOS-STORE-326** Data backfills MUST be batched with a bounded row count per statement, idempotent, resumable from a recorded cursor and placed in their own file, and MUST NOT run inside the DDL transaction.

**MOS-STORE-327** DDL is executed only by `medicalos_owner`; `medicalos_app` MUST have no `CREATE` on the schema and no `ALTER` on any table, and the migration job runs as a separate job or compose service, never on API startup, so a rolling deploy cannot run two migrators concurrently.

**MOS-STORE-328** A checked-in `schema.sql` golden dump is the canonical rendering of the schema at HEAD; CI regenerates it by migrating an empty database from `0001` to HEAD and fails on any diff, which keeps the DDL in this chapter and the migrations in the repository from drifting apart.

**MOS-STORE-329** CI MUST additionally migrate a database seeded at the previous release tag up to HEAD and run the acceptance suite against it: migrating from empty proves the DDL is valid, migrating from the previous release proves the upgrade is.

**MOS-STORE-330** OpenAPI, the Go structs and the Python models are **generated** from the schema and the JSON Schemas in `artifact_manifest_schemas`, never hand-maintained, and a schema change not reflected in generated output fails CI (Chapter 10 owns the pipeline).

### 12.16 Retention, deletion and erasure

Three distinct operations exist, and the specification uses their names precisely. The permission column names identifiers from `medos/contracts/permissions.yaml`; Chapter 8 is the only source of permission spellings (`MOS-SEC-032`, `MOS-SEC-033`) and no chapter may invent one.

| Operation | What it does | Reversible | Permission required |
|---|---|---|---|
| **Soft delete** | sets `deleted_at`; hidden from listings, still referenceable | yes | `study.delete`, `dataset_version.delete` (class `phi`) |
| **Purge** | removes a study, series or dataset and its PACS objects from a tenant | no | `study.delete`, guarded by `MOS-STORE-335` |
| **Erasure** | destroys the link between a natural person and all data derived from them | no | `phi.erase` (class `phi`), with a reason of at least 20 characters (`MOS-SEC-037`) |

#### 12.16.1 Retention defaults

**MOS-STORE-331** Retention is configured per tenant and object class, and every default below mirrors Chapter 8 `MOS-SEC-126`, which owns the retention schedule. Audit classes are configurable upward only; operational classes in either direction.

| Object class | Store | Default retention | Action at expiry |
|---|---|---|---|
| `execution_artifacts` (volumes, masks, probability maps) | execution bucket | 30 days after the job is terminal (`MOS-SEC-126`) | delete the object, keep the row, set `blob_deleted_at` |
| `execution_artifacts` of kind `result_bundle` | execution bucket | lifetime of the `results` row | never auto-deleted |
| captured service stdout | execution bucket | 30 days | delete the object |
| `jobs`, `job_steps`, `job_events` | Postgres | 7 years | export the partition to evidence, then drop it |
| `job_outbox` (published) | Postgres | 7 days after `published_at` | delete |
| `job_dead_letter` | Postgres | 90 days after `dead_at` | delete |
| `policy_decisions` | Postgres | 2 years (`MOS-SEC-070`, `MOS-SEC-126`) | export, then drop the partition |
| `audit_events` | Postgres | 7 years, never below the 6-year floor of `MOS-SEC-126`, configurable upward only | seal the checkpoint, export a signed archive, record it in `audit_archives`, then detach the partition (`MOS-SEC-154`) |
| `results`, `result_provenance`, `result_findings`, `result_measurements`, `result_reviews` | Postgres | lifetime of the tenant | never auto-deleted |
| `result_dicom_objects` content | PACS | the PACS's own retention | not managed by MedicalOS |
| `dataset_versions`, `annotations`, `evaluation_*`, `capability_claims`, `deployment_gate_decisions`, `validation_reports` | evidence bucket, object-locked | newest referencing report's retention + 10 years | never auto-deleted |
| imaging projection (`patients`, `studies`, `series`, `instances`) | Postgres | 7 years (`MOS-SEC-126`) | removed by the sweep, or earlier by an explicit purge |
| de-identification UID map | Postgres | lifetime of the tenant | destroyed only by erasure (§12.16.3) |

```sql
CREATE TABLE retention_policies (
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
  object_class text NOT NULL CHECK (object_class IN ('execution_artifact','service_stdout',
    'job_event','job_outbox','job_dead_letter','policy_decision','audit_event','export',
    'imaging_projection','deid_uid_map')),
  retain_for interval NOT NULL,
  action text NOT NULL CHECK (action IN ('delete','export_then_drop')),
  updated_by uuid, updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, object_class)
);

CREATE TABLE legal_holds (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  scope text NOT NULL CHECK (scope IN ('tenant','patient','study','dataset_version')),
  scope_id text NOT NULL,
  reason text NOT NULL,
  placed_by uuid NOT NULL, placed_at timestamptz NOT NULL DEFAULT now(),
  released_by uuid, released_at timestamptz
);
CREATE UNIQUE INDEX legal_holds_active_uk ON legal_holds (tenant_id, scope, scope_id)
  WHERE released_at IS NULL;
```

**MOS-STORE-332** An active `legal_holds` row blocks retention expiry, purge and erasure within its scope. Each of those operations MUST check for a covering hold first and MUST fail closed with a distinguishable error rather than proceed partially.

**MOS-STORE-333** Retention is enforced by a scheduled sweep iterating tenants (MOS-STORE-225), never by a cascading `ON DELETE` rule. Retention that happens as a side effect of another delete is retention nobody can audit.

#### 12.16.2 Purging a study

**MOS-STORE-334** Purge exists because development and site onboarding involve re-ingesting public collections with corrected de-identification, and a platform with no supported removal path grows a permanent tail of bad ingests. The permission is `study.delete`, class `phi` (Chapter 8 §8.3.2); the spelling `study.purge` is not a permission identifier and MUST NOT appear in a role, a token scope or a route definition (`MOS-SEC-033`).

**MOS-STORE-335** A study MUST NOT be purged while a non-superseded `results` row references it, unless the caller supplies `force = true` and holds `study.delete`. With `force` the results are purged too and the audit row records the result ids destroyed; without it the operation fails and names the blocking results.

**MOS-STORE-336** The purge order is fixed, and the transaction boundary is per study:

1. Verify no active `legal_holds` row covers the study or its patient.
2. Verify the blocking-result condition of MOS-STORE-335.
3. Write the `study.delete` audit row **before** destroying anything, recording the study UID, series and instance counts and the result ids in scope.
4. Delete generated DICOM objects from the PACS through the Gateway; set `result_dicom_objects.stow_state = 'erased'`, `erased_at = now()`.
5. Delete source DICOM objects from the PACS through the Gateway.
6. Delete `execution_artifacts` blobs for jobs on this study; set `blob_deleted_at`.
7. Delete Postgres rows in order: `job_series`, `results` and cascaded children if forced, `jobs`, `instances`, `series`, `study_ingest`, `studies`.
8. Delete `deid_uid_map` rows for the study's UIDs, within every `deid_key_version` UID space, if no other study in the tenant maps to them.
9. Recompute `patients.study_count`; if it reaches zero and the patient has no other reference, set `deleted_at` — do not delete the row.
10. Write the completion audit row.

**MOS-STORE-337** A purge MUST NOT delete `audit_events`, `policy_decisions`, `result_provenance`, `validation_reports`, `evaluation_*`, `capability_claims`, `deployment_gate_decisions`, `dataset_versions` or `dataset_cases`: those are evidence and governance records, and a purge is an operational cleanup, not an erasure. `result_provenance`'s `ON DELETE RESTRICT` therefore blocks step 7 for a study with results — which is intended. Destroying a result is an erasure-class operation, not a cleanup.

#### 12.16.3 Erasing a patient

This is the operation the previous specification had no answer for, and it is genuinely hard because a result permanently references its source SOP Instance UIDs and a sealed dataset version cannot be rewritten.

**MOS-STORE-338** Erasure is **destroying the link between a natural person and the data derived from them**, not deleting every row that person ever touched. Row deletion would take the audit trail, the provenance record and the issued validation reports with it — the three things that exist precisely so that what happened to a patient's data can be answered later. An append-only audit table and a right-to-erasure obligation are compatible only because the erasable content is encrypted with a destroyable key (`MOS-SEC-127`, `MOS-SEC-128`); crypto-shredding is the mechanism, not row deletion.

**MOS-STORE-339** Two profiles exist and the tenant chooses per request:

| Profile | Pixel data | Postgres PHI columns | Re-identification map | UIDs in provenance | Issued evidence |
|---|---|---|---|---|---|
| `tombstone` (default) | deleted from the PACS | nulled / tombstoned, patient DEK destroyed | **deleted** | retained, now unresolvable to a person | reports stay `verifiable`; cohorts become unusable for new runs |
| `full_purge` | deleted from the PACS | nulled / tombstoned, patient DEK destroyed | **deleted** | emptied; `provenance_redacted_at` set | referencing reports marked `reproducibility_status = 'degraded'` |

**MOS-STORE-340** Once the map rows are deleted and the patient DEK destroyed, the pseudonymous UIDs retained in `result_provenance`, `dataset_cases` and `result_dicom_objects` cannot be linked to a person by anyone, the operator included. Retaining them is therefore retention of non-identifying data, and it is what keeps every previously issued result internally consistent. This is the whole reason the mapping is a table rather than a function of a seed.

```sql
CREATE TABLE erasure_requests (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
  scope_kind text NOT NULL CHECK (scope_kind IN ('patient','study','tenant')),
  scope_id text NOT NULL,
  profile text NOT NULL CHECK (profile IN ('tombstone','full_purge')),
  reason text NOT NULL CHECK (length(reason) >= 20),     -- MOS-SEC-037
  requested_by uuid NOT NULL, requested_at timestamptz NOT NULL DEFAULT now(),
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending','blocked','running','completed','failed')),
  blocked_reason text,
  executed_at timestamptz, completed_at timestamptz,
  keys_destroyed text[] NOT NULL DEFAULT '{}',
  objects_deleted integer NOT NULL DEFAULT 0,
  rows_affected bigint NOT NULL DEFAULT 0,
  unerasable jsonb NOT NULL DEFAULT '[]'::jsonb,         -- MOS-SEC-132
  certificate_digest sha256_digest,
  CHECK ((state = 'blocked') = (blocked_reason IS NOT NULL)),
  CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);

CREATE TABLE erasure_actions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  erasure_request_id uuid NOT NULL REFERENCES erasure_requests(id),
  tenant_id uuid NOT NULL,
  step text NOT NULL, target_kind text NOT NULL, target_id text NOT NULL,
  rows_affected bigint NOT NULL DEFAULT 0,
  executed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER erasure_actions_append_only BEFORE UPDATE OR DELETE ON erasure_actions
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
```

The column set mirrors Chapter 8 `MOS-SEC-129` name for name — `scope_kind`, `scope_id`, `reason`, `executed_at`, `keys_destroyed`, `objects_deleted`, `rows_affected` — and MUST NOT diverge from it; `profile`, the state machine and `certificate_digest` are this chapter's rendering and add facts without renaming any.

**MOS-STORE-341** The procedure, executed per patient inside the tenant's context:

1. **Block check.** Refuse if an active hold covers the patient or tenant; set `state = 'blocked'`.
2. **Enumerate.** Collect the patient's studies, series, instances, jobs, results, generated DICOM objects, execution artifacts, and every `dataset_versions` row whose `dataset_cases` contain one of the patient's study UIDs.
3. **PACS.** Delete all source and AI-derived objects through the Gateway; set `instances.erased_at`, `series.erased_at`, `studies.erased_at`, `result_dicom_objects.stow_state = 'erased'`.
4. **Object store.** Delete every `execution_artifacts` blob and every `annotations` reference and per-reader object derived from the patient; set `blob_deleted_at`. Evidence objects still under object lock cannot be deleted before retention expires; the request records that in `erasure_actions` and in `unerasable` rather than failing (`MOS-SEC-132`).
5. **Postgres PHI columns.** Tombstone the patient:

```sql
UPDATE patients
SET patient_name = NULL, patient_birth_date = NULL, patient_sex = NULL,
    patient_id_value = 'ERASED:' || $erasure_request_id,
    issuer_of_patient_id = '', phi_state = 'erased',
    erased_at = now(), updated_at = now()
WHERE id = $patient_id;

UPDATE studies
SET accession_number = NULL, referring_physician = NULL,
    study_description = NULL, erased_at = now()
WHERE patient_id = $patient_id;
```

6. **Keys and re-identification map.** Destroy the patient DEK and every DEK named in `keys_destroyed` (`MOS-SEC-127`, `MOS-SEC-130`), then delete the `deid_uid_map` and `deid_identity_map` rows for the patient's UIDs and identifiers in every `deid_key_version` UID space. This is the terminal, irreversible act; it runs as `medicalos_owner` and is recorded in `erasure_actions`.
7. **Provenance.** For `full_purge` only: set `result_provenance.input_instance_uids = '{}'` and `provenance_redacted_at = now()`, retaining `input_instance_count` and `input_pixel_digest` so the quantity and identity of the input stay attestable though the UIDs are gone.
8. **Datasets.** For every affected sealed `dataset_versions` row set `erasure_state = 'contains_erased_subject'` and `usable_for_new_runs = false`. **The sealed manifest MUST NOT be rewritten** — a rewritten manifest invalidates the content address on which every issued validation report depends.
9. **Evidence.** For `full_purge`, set `reproducibility_status = 'degraded'` on every `validation_reports` row whose `evaluation_run_ids` reference an affected dataset version. The report stays readable, signed and valid as a historical claim; it is simply no longer re-runnable. Every such report is also listed in `unerasable` (`MOS-SEC-132`).
10. **Results and reviews are retained.** They now describe an anonymous subject.
11. **Audit.** `audit_events` and `policy_decisions` are neither deleted nor rewritten (`MOS-SEC-130`) — the chain would break and the obligation to retain an access record outlives the erasure request. Because the row carries only the surrogate `patient_id` (MOS-STORE-307), the trail afterwards reads "principal X accessed patient `018f…` on date Y" and identifies nobody.
12. **Certificate.** Write `erasure_requests.certificate_digest` over the canonical serialisation of the `erasure_actions` rows, populate `keys_destroyed`, `objects_deleted`, `rows_affected` and `unerasable`, set `executed_at` and `state = 'completed'`, and emit the final `phi.erase` audit row. A silent partial erasure MUST NOT occur: what could not be erased is reported (`MOS-SEC-132`).

**MOS-STORE-342** A study whose patient has been erased MUST NOT be re-projectable. The `erased_at IS NULL` guard of MOS-STORE-239 blocks the upsert and re-arrival raises an audit event. Without it the next PACS sync silently undoes the erasure.

**MOS-STORE-343** Erasing a **user** is a different and much smaller operation, and it is not an `erasure_requests` row — `scope_kind` has no `user` value, because a user account is not a data subject of the imaging record. It is an account operation: tombstone the `users` row (`status = 'deleted'`, `email` and `display_name` replaced, MOS-STORE-233), revoke `api_keys`, write the audit row, and retain every reference from `result_reviews`, `validation_reports.approver_user_id`, `acceptance_criteria.approved_by` and `audit_events.on_behalf_of_id`. A reviewer's identity attached to a clinical result is part of the clinical record; `approver_name` and `approver_role` are denormalised onto `validation_reports` for exactly this reason, so an issued report keeps naming its approver after the account is gone.

**MOS-STORE-344** Deleting a **tenant** requires `tenant.delete` (class `admin`) and is an erasure request of `scope_kind = 'tenant'`: reject new work, run the retention sweep to completion, purge the execution and exports buckets by the `t/{tenant_id}/` prefix, delete the re-identification maps, destroy the tenant KEK and `pepper[tenant_id]` so that every emitted `study_ref` becomes permanently unlinkable (`MOS-SEC-131`), tombstone every `patients` row, retain the evidence bucket and `audit_events` for their configured retention, and set `tenants.status = 'closed'`. The `tenants` row itself is never deleted, because `ON DELETE RESTRICT` on every tenant foreign key is what guarantees no orphan survives.

### 12.17 Entity → table → owning chapter

| Spine entity | Table(s) | Semantics owned by |
|---|---|---|
| `Tenant` | `tenants` | Ch. 8 |
| `User`, `Role`, `Permission` | `users`, `roles`, `permissions`, `role_permissions`, `user_roles` | Ch. 8 |
| `ApiKey`, `ServiceAccount` | `api_keys`, `service_accounts` | Ch. 8 |
| `Service`, `ServiceVersion` | `services`, `service_versions`, `service_version_capabilities` | Ch. 2, Ch. 6 |
| `Capability` | `capabilities`, `capability_concepts` | Ch. 6 |
| `SeriesSelector` | `series_selectors` | Ch. 3 |
| `Deployment` | `deployments`, `capability_pins` | Ch. 6 |
| `Model`, `ModelVersion` | `models`, `model_versions` | Ch. 6 |
| `PreprocessingSpec` | `preprocessing_specs` | Ch. 4 |
| `Artifact` | `artifacts`, `artifact_manifest_schemas` | Ch. 6 |
| `Dataset`, `DatasetVersion` | `datasets`, `dataset_versions`, `dataset_cases` | Ch. 7 |
| `DatasetSplit` | `dataset_splits`, `dataset_split_members` | Ch. 7 |
| `AnnotationSet` | `annotation_sets`, `annotations`, `annotation_readers` | Ch. 7 |
| `CapabilityClaim` | `capability_claims` | Ch. 7 |
| `AcceptanceCriteria` | `acceptance_criteria`, `tenant_acceptance_bindings` | Ch. 7, Ch. 9 |
| `EvaluationRun` | `evaluation_runs`, `evaluation_case_metrics`, `evaluation_case_scores` | Ch. 7 |
| deployment gate decision | `deployment_gate_decisions` | Ch. 7 |
| `ValidationReport` | `validation_reports` | Ch. 7, Ch. 9 |
| `Job`, `JobStep`, `JobEvent` | `jobs`, `job_series`, `job_steps`, `job_events`, `job_queue`, `job_outbox`, `job_dead_letter` | Ch. 5 |
| `Result` | `results`, `result_findings`, `result_measurements`, `result_dicom_objects`, `result_provenance` | Ch. 4, Ch. 9 |
| `ResultReview` | `result_reviews` | Ch. 9 |
| `ResultBundle` | not a table — object-store blob at `results.bundle_object_key` | Ch. 2 |
| `Patient`, `Study`, `Series`, `Instance` | `patients`, `studies`, `study_ingest`, `series`, `instances`, `projection_sync_state`, `pacs_backends` | Ch. 3 |
| tenant attribution | `ae_tenant_map` | Ch. 3 |
| quarantined ingest | `quarantine` | Ch. 3 |
| `TriageDecision` | `triage_decision` | Ch. 3 |
| `AuditEvent` | `audit_events`, `audit_chain_checkpoints` | Ch. 8 |
| `PolicyDecision` | `policy_decisions` (there is no `policies` table — `MOS-SEC-062`) | Ch. 8 |
| audit archive & policy activation | `audit_archives`, `policy_activations` | Ch. 8 |
| de-identification mapping | `deid_policies`, `deid_uid_map`, `deid_identity_map` | Ch. 3 |
| intermediate data | `execution_artifacts`, `blobs` | Ch. 5 |
| retention & erasure | `retention_policies`, `legal_holds`, `erasure_requests`, `erasure_actions` | this chapter, Ch. 8 for `MOS-SEC-129` |
| `TrainingDataPolicy` | `training_data_policies` | Ch. 17 |
| `SamplingPlan` | `sampling_plans` | Ch. 17 |
| `HarvestBatch`, `HarvestCandidate` | `harvest_batches`, `harvest_candidates` | Ch. 17 |
| `CurationDecision` | `curation_decisions` | Ch. 17 |
| `CorpusStratificationReport` | `corpus_stratification_reports` | Ch. 17 |
| `Tool`, `ToolVersion`, `Workflow`, `WorkflowVersion`, `Agent`, `AgentVersion` | `artifacts` rows of kind `tool`/`workflow`/`agent`; no dedicated tables before 0.4 | Ch. 11 |

The AI-derived marking fields that `MOS-SAFE-053` (ch. 9) requires at the top level of the `Result` JSON — `service_id`, `service_version` and `legal_manufacturer_name` — are **served by joining** `service_versions` and `services` on `results.service_version_id`, and are not denormalised onto `results`; `derivation` is the constant `"ai_derived"`, and `clinical_use_mode` and `review_status` are columns of `results`. The join is declared here so that the requirement is satisfiable without a second, drifting copy of the manufacturer's identity.

### Acceptance criteria

Each check is executable by a CI job against a database migrated from `0001` to HEAD and seeded with two tenants.

1. **Schema completeness.** Every entity in §12.17 resolves to at least one existing table, and `information_schema.tables` contains no table absent from §12.17. In particular `policies` does not exist.
2. **No natural-key primary keys.** No primary key, and no unique constraint used as a foreign-key target, consists solely of columns of domain `dicom_uid`.
3. **Tenant-scoped uniqueness.** Every constraint in MOS-STORE-216 exists with exactly the listed columns in the listed order.
4. **Cross-tenant foreign keys impossible.** Every foreign key whose referencing and referenced tables both have `tenant_id` includes `tenant_id` in its column list — **except** where the referenced table is on the `MOS-STORE-220` dual-scope list, whose `tenant_id` is nullable and against which a composite key is unsatisfiable (`MOS-STORE-217`); on those edges the key must instead be a single-column key to the parent's `id`. Zero violations of either half.
5. **RLS coverage.** Every table with `tenant_id` has `relrowsecurity` and `relforcerowsecurity` true, at least one row in `pg_policies`, and the `tenant_id` immutability trigger; no table in the MOS-STORE-219 global list has RLS enabled.
6. **Pooling safety.** Through PgBouncer in transaction mode as `medicalos_app`: transaction 1 sets `SET LOCAL medicalos.tenant_id` to tenant A and reads a study; transaction 2 on the same backend runs `SELECT count(*) FROM studies` with no `SET`. Assert 0 and that an `INSERT` is refused. Repeat with `SET` instead of `SET LOCAL` in transaction 1 and assert it now leaks — proving the test detects the bug.
7. **No RLS escape hatch.** Every tenant-owned policy's `qual` and `with_check` contain `current_tenant_id()` and no other `current_setting` call; `BYPASSRLS` appears in the migrations only in the `medicalos_backup` role definition.
8. **Append-only enforcement.** As `medicalos_app`, `UPDATE` and `DELETE` on each table in MOS-STORE-228's table-level list are refused. On `artifacts` and `validation_reports`, `DELETE` and an `UPDATE` of any sealed column are refused while an `UPDATE` of `lifecycle_status`/`status_reason` and of the report's standing columns (`status`, `superseded_by`, `revocation_reason`, `reproducibility_status`) succeeds. As `medicalos_owner`, `UPDATE` on `audit_events` is refused by the trigger.
9. **Audit chain continuity.** Insert 1,000 rows across two tenants from 8 concurrent connections spanning a month boundary; recompute the chain per tenant and assert every `hash` matches the RFC 8785 recomputation of its own row, every `seq` is gapless from 1 with no duplicate, `prev_hash` of `seq = n` equals `hash` of `seq = n-1`, and the first row's `prev_hash` equals the tenant genesis constant. Mutate one `detail` value as `medicalos_owner` with the trigger disabled and assert verification reports that exact `seq` (`MOS-SEC-153`).
10. **Unattributed permit is unrepresentable.** `INSERT INTO policy_decisions` with `effect = 'permit'` and an empty `determining_policies` is rejected, as is `effect = 'forbid'` with an empty `determining_policies` and any `reason_code` outside `{no_matching_policy, policy_unavailable, rbac_denied}`. Assert no row uses `allow` or `deny` as an `effect` value.
11. **Clinical gate.** A `deployments` row with `clinical_use_mode = 'clinical'` and any of `approved_by`, `approved_at`, `acceptance_run_id`, `validation_report_id` null is rejected — all four cases.
12. **RUO marking and PHI leakage.** A `result_dicom_objects` row with `clinical_use_mode = 'research_only'` and `research_marked = false` is rejected. Grep the writers of `job_events.payload`, `job_outbox.envelope` and `policy_decisions.context` for `patient_name`, `patient_birth_date`, `accession_number`; assert no hit, and assert every key written to `policy_decisions.context` is declared `loggable: true` (`MOS-SEC-069`).
13. **Result uniqueness and idempotency.** A second `results` row with the same `(job_id, capability_id)` is rejected. Running the pipeline twice with one `idempotency_key` yields exactly one `results` row per capability, one `result_provenance` row each, and one `series_instance_uid` per `object_kind`.
14. **Provenance completeness.** Every `results` row has a `result_provenance` row with `cardinality(input_series_uids) >= 1`, a non-null `input_pixel_digest`, all seven `geometry` keys and at least one element in `models` and in `preprocessing_specs`. Every `result_findings` row has `cardinality(source_sop_instance_uids) >= 1` with every element present in `result_provenance.input_instance_uids`. The MOS-STORE-280 query returns the expected result ids with no additional join.
15. **Split leakage impossible.** Inserting one `patient_key` into two partitions of a split is rejected. `dataset_split_members.partition` takes values only from `{train, tune, test, excluded}`, and an `excluded` row without an `exclusion_reason` is rejected. For every seeded `evaluation_runs` row, no `patient_key` in `evaluation_case_metrics` appears in that split's `train` partition.
16. **Evidence bindings complete.** Every `evaluation_runs` row in `state = 'SUCCEEDED'` has a `run_digest`, a `metric_conventions` block, a `metric_registry_version`, `code_dirty = false`, exactly one of `service_version_id`/`model_version_id` non-null, and at least one `evaluation_case_metrics` row per case in the split's `test` partition. Every metric id present resolves in the `MOS-EVID-054` registry. Marking its `dataset_versions` row `DEFECTIVE` in one transaction leaves the run `INVALIDATED` with a reason and every citing `validation_reports` row `REVOKED` (MOS-STORE-301a).
17. **No free-form metrics.** `model_versions` has no column whose name contains `metric`, and every performance figure the API serves for a model version resolves to a `capability_claims` row with a non-null `evaluation_run_id` whose run is in `state = 'SUCCEEDED'` (`MOS-EVID-072`). A `capability_claims` row whose `metric` is threshold-dependent and whose `operating_point` is null is rejected by constraint.
18. **Geometry metadata on intermediates.** An `execution_artifacts` row of kind `label_map` with any of `spacing_mm`, `origin_mm`, `direction`, `reference_series_id`, `frame_of_reference_uid` null is rejected — all five cases.
19. **Projection rebuild lossless and idempotent.** Seed 3 studies, 12 series, 1,200 instances and a completed job with results referencing them; run the MOS-STORE-245 rebuild twice. Assert every `id` unchanged; `study_ingest.state`, `study_ingest.selection_hash`, `first_seen_at`, `series.origin` unchanged; every foreign key from `jobs`, `job_series`, `results`, `execution_artifacts` still resolves; `drift_series = 0` and `drift_instances = 0` after the second run; only `last_synced_at` and `last_reconcile_at` changed.
20. **Counters honest.** After the rebuild, `series.instance_count` equals `COUNT(*)` of its instances for every series, and likewise for `studies.series_count` and `studies.instance_count`.
21. **De-identification is a bijection.** Within one `(tenant_id, deid_key_version)`, mapping one source UID to a second pseudonym, or a second source UID to one pseudonym, is rejected. Running de-identification twice over one study emits identical UIDs both times. Bumping `deid_key_version` opens a new UID space and leaves every prior-version row present and readable.
21a. **Deployment slot uniqueness.** A second `deployments` row with `role = 'ACTIVE'` and `state = 'SERVING'` in one `(tenant_id, environment, capability_id)` is rejected; a `CANARY`, `SHADOW` or `STANDBY` row in the same slot is accepted. A row set to `state = 'SERVING'` with `verification_ref` null is rejected, and one set to `SUSPENDED` with `state_reason` null is rejected.
22. **Two tenants, one public collection.** Ingesting the identical LIDC-IDRI study manifest into tenants A and B succeeds in both, produces two distinct `studies.id`, and a query in A's context returns exactly one study.
23. **Object-key discipline.** Every `blobs` row with a non-null `tenant_id` has `object_key LIKE 't/' || tenant_id || '/%'`, and every row's key matches one grammar in MOS-STORE-313.
24. **Blob GC safety.** A blob with `ref_count = 1` and an expired `expires_at` survives the collector; after the reference is dropped and the collector re-runs, the object is gone and `deleted_at` is set.
25. **Migration hygiene.** (a) Empty → HEAD matches the checked-in `schema.sql`. (b) Previous release tag → HEAD on a seeded database passes criteria 1–24. (c) Every migration file's digest matches `schema_migrations.file_digest`. (d) Every migration sets `lock_timeout` first or carries the `-- medicalos:no-transaction` header. (e) Every migration creating a table with `tenant_id` also contains `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, a `CREATE POLICY`, a `UNIQUE (tenant_id, id)` and a `GRANT` in the same file.
26. **Purge guard.** A purge with `force = false` on a study with a non-superseded result fails and names the blocking result. With an active `legal_holds` row it fails with a distinguishable hold error even when `force = true`. Assert the permission checked is `study.delete` and that the identifier `study.purge` appears in no route definition, role or token scope.
27. **Erasure end to end.** Seed a patient with 2 studies, 1 completed job, 1 result with 2 generated DICOM objects, 1 review, and membership in 1 sealed dataset version referenced by 1 signed validation report; run a `tombstone` erasure. Assert: the PACS returns 404 for every source and generated SOP Instance UID; `patients.phi_state = 'erased'` with all demographic columns null; zero `deid_uid_map` rows for those UIDs in any key version; the patient DEK id appears in `erasure_requests.keys_destroyed`; the `results`, `result_provenance` and `result_reviews` rows still exist; `dataset_versions.erasure_state = 'contains_erased_subject'` and `usable_for_new_runs = false`; `dataset_versions.manifest_digest` **unchanged**; the report's `report_digest` and `signature` unchanged and still verifying; the report listed in `erasure_requests.unerasable`; and the audit chain verifying end to end.
28. **Erasure not undone by sync.** After criterion 27, re-present one erased study to the Gateway: the projection upsert affects zero rows and an `audit_events` row with `action = 'projection.erased_resurrection_blocked'` is written.
29. **Full-purge degradation recorded.** Repeat criterion 27 with `full_purge`: `result_provenance.input_instance_uids` is empty, `provenance_redacted_at` is set, `input_instance_count` and `input_pixel_digest` are retained, and every referencing `validation_reports` row has `reproducibility_status = 'degraded'`.
30. **Index discipline.** Every index on a table with `tenant_id` either leads with `tenant_id` or appears in an allow-list file with a written justification. Fail on an unlisted exception.

---

---

[← 11. Agentic Layer, Chat and LLM Integration](11-agentic-layer.md) · [Index](../../MEDICALOS_SPEC.md) · [13. Observability, Deployment and Scaling →](13-operations.md)
