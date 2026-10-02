-- =====================================================================================
-- 0012_artifacts.up.sql -- the Artifact registry. ONE table, one kind discriminator.
--
-- docs/spec/15-delivery.md section 15.2.6 (weeks 10-13, tag 0.3.0), first sentence:
-- "One `artifacts` table with per-kind JSON-Schema'd manifests served on the existing API
-- paths." Chapter 6 section 6.3 is normative for the columns, the kind enums, the
-- immutability rule, the unique keys and the lifecycle graph; chapter 12 section 12.9
-- (`MOS-STORE-253`, `MOS-STORE-254`) is normative for the physical form and is what this
-- migration is generated from (`MOS-STORE-201`: "a migration MUST be generated from it and
-- never from the block above").
--
-- SCOPE -- four objects, and nothing else:
--
--     registry_changelog          MOS-REG-010   append-only, one row per registry write
--     registry_epoch (sequence)   MOS-REG-010   the monotonic epoch
--     artifact_manifest_schemas   MOS-STORE-253 one JSON Schema per kind, seeded here
--     artifacts                   MOS-STORE-253 THE registry table
--
-- NOT here, on purpose: `capabilities` / `capability_concepts` (section 12.9.1) and
-- anything under `deployments` (0008 already created it). Capability resolution and the
-- deployment state machine are separate components of this release and this migration
-- deliberately does not pre-empt either. `MOS-REG-010` names six tables whose writes must
-- append to `registry_changelog`; only `artifact` does so today, which is stated in this
-- component's report rather than left to be discovered.
--
-- ADDITIVE. It creates three tables, one sequence, four functions and five triggers, and
-- ALTERS no existing table. Nothing in 0001-0011 changes behaviour because of it.
--
-- SIX DELIBERATE DIVERGENCES FROM SECTION 12.9's DDL LISTING, each at its point of use:
--   1. `public_id` exists. Chapter 6's manifests resolve references BY id
--      (`spec.models[].ref: "mv_pulmo_effusion_unet_3_2_1"`, `MOS-REG-025`) and section
--      12.9's listing has no column such a ref can resolve against -- only a uuid primary
--      key the publisher cannot know. Section 4.
--   2. `created_at` / `updated_at` exist. `MOS-STORE-210` requires both on every mutable
--      table; section 12.9's listing carries neither and `MOS-STORE-201` makes that an
--      omission in the listing rather than an exemption. Section 4.
--   3. `registry_changelog` carries `tenant_id`. Chapter 6's DDL has none, but its
--      `row_json` reproduces tenant-owned artifact rows verbatim, so a changelog with no
--      tenant column is a cross-tenant read of every manifest in the deployment
--      (`MOS-REG-107` forbids exactly that). `MOS-SEC-077` / `MOS-STORE-229` then require
--      forced row security on it, which is what section 2 gives it.
--   4. `artifacts.published_at` is spelled `sealed_at`, which is section 12.9's name for
--      chapter 6's column. One name, chapter 12's, per `MOS-STORE-201`.
--   5. The immutability trigger is `artifacts_forbid_change()` rather than the shared
--      `forbid_mutation()` of section 12.9's listing: identical behaviour and SQLSTATE,
--      with a HINT that names `MOS-REG-018` instead of `job_events`.
--   6. `SEALED`/`DEFECTIVE` kinds and `preprocessing`, `policy_set`, `workflow` are
--      admitted by the CHECK constraints but seeded with NO manifest schema, so the
--      schema foreign key refuses the row. See medos/registry/schemas.py for the reason
--      per kind; section 15.1.3 names "`Artifact` registry breadth" as this release's
--      degradable item and this is that degradation, per kind, with its reason.
--
-- ONE CONSEQUENCE WORTH STATING PLAINLY. `artifacts_family_version_uk` is section 12.9's
-- `UNIQUE (kind, family, version)` -- global, not per tenant. Two tenants therefore cannot
-- publish the same `(kind, family, version)` with different content; the second gets the
-- 409 of `MOS-REG-019`. That is correct for a vendor catalogue keyed by a reverse-DNS
-- family name and it is what section 12.9 writes, so it is implemented as written; if a
-- deployment ever needs otherwise the fix is an amendment there, not a divergent key here.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. Domains.  MOS-STORE-211, section 12.2.
--
-- Guarded, on the 0005/0006/0011 precedent: several migrations in this release block were
-- authored in parallel and any of them may have created `sha256_digest`. A domain with no
-- state is free to leave in place and the down script does not drop it.
-- =====================================================================================
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                  WHERE t.typname = 'sha256_digest' AND n.nspname = 'public') THEN
    EXECUTE $d$CREATE DOMAIN sha256_digest AS text
                 CHECK (VALUE ~ '^sha256:[0-9a-f]{64}$')$d$;
    EXECUTE 'ALTER DOMAIN sha256_digest OWNER TO medicalos_owner';
  END IF;
  -- Section 12.2 defines `oci_digest` with the same body as `sha256_digest` and a
  -- different NAME, because an OCI manifest digest and a content digest are different
  -- nouns that happen to share an encoding today.
  IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                  WHERE t.typname = 'oci_digest' AND n.nspname = 'public') THEN
    EXECUTE $d$CREATE DOMAIN oci_digest AS text
                 CHECK (VALUE ~ '^sha256:[0-9a-f]{64}$')$d$;
    EXECUTE 'ALTER DOMAIN oci_digest OWNER TO medicalos_owner';
  END IF;
  -- MOS-REG-095: every artifact version is semver. The domain is section 12.2's, and its
  -- pattern is the same one the envelope JSON Schema carries (MOS-REG-015) -- two
  -- statements of one grammar, which section 9 asserts agree.
  IF NOT EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                  WHERE t.typname = 'semver' AND n.nspname = 'public') THEN
    EXECUTE $d$CREATE DOMAIN semver AS text CHECK (VALUE ~
      '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$')$d$;
    EXECUTE 'ALTER DOMAIN semver OWNER TO medicalos_owner';
  END IF;
END $$;


-- =====================================================================================
-- 2. `registry_changelog` and the epoch.  MOS-REG-010, MOS-REG-011.
--
--   "Every write to the `artifact` ... table MUST, in the same transaction, append a row
--    to the append-only `registry_changelog` table and advance the monotonic
--    `registry_epoch` sequence."
--
-- The append is a trigger (section 6) and not a convention, because "in the same
-- transaction" is not something a code review can guarantee and an epoch that skips a
-- write makes `MOS-REG-011`'s snapshot unreconstructible -- which in turn makes
-- `MOS-REG-071`'s offline replay of a job's resolution impossible. The one thing a
-- resolution record is FOR is answering "which version produced this patient's result",
-- so the log that answers it is not optional and not application-level.
--
-- It also answers a question section 12.9 leaves nowhere to answer: `MOS-REG-021` allows
-- `SUSPENDED -> previous status`, and no column anywhere remembers what the previous
-- status was. Section 6's lifecycle trigger reads it from here.
-- =====================================================================================
CREATE SEQUENCE registry_epoch AS bigint START 1;

CREATE TABLE registry_changelog (
  epoch        bigint PRIMARY KEY DEFAULT nextval('registry_epoch'),
  table_name   text NOT NULL CHECK (table_name IN
                 ('artifact','artifact_family','capability','deployment','tenant_pin',
                  'node_profile')),
  row_id       text NOT NULL CHECK (row_id <> ''),
  op           text NOT NULL CHECK (op IN ('INSERT','UPDATE','DELETE')),
  row_json     jsonb NOT NULL,
  actor        text NOT NULL CHECK (actor <> ''),
  written_at   timestamptz NOT NULL DEFAULT now(),
  -- Divergence 3. Chapter 6's DDL has no tenant column; `row_json` carries tenant-owned
  -- manifests, so without one every tenant reads every other tenant's catalogue.
  tenant_id    uuid REFERENCES tenants(id) ON DELETE RESTRICT
);
ALTER SEQUENCE registry_epoch OWNED BY registry_changelog.epoch;

CREATE INDEX registry_changelog_row_idx
  ON registry_changelog (table_name, row_id, epoch DESC);

COMMENT ON TABLE registry_changelog IS
  'MOS-REG-010: append-only, one row per registry write, in the writing transaction. '
  'MOS-REG-011 reconstructs a registry snapshot from this table alone.';


-- =====================================================================================
-- 3. `artifact_manifest_schemas`.  MOS-STORE-253, MOS-STORE-219, MOS-REG-015.
--
-- Platform-global: no `tenant_id`, no row security, `SELECT` only for `medicalos_app`.
-- MOS-STORE-355 says which path writes it, in terms: "`artifact_manifest_schemas` [is
-- written] by the migration introducing a schema version". So the rows below are seeded
-- here and by nothing else, and a new schema version is a NEW migration -- which is also
-- what `MOS-REG-015`'s "versioned independently per kind" means operationally.
--
-- The `json_schema` values are GENERATED from medos/registry/schemas.py, the single
-- in-repo source `MOS-REG-016` requires. Regenerate with
-- `python -m medos.registry.schemas`; tests/integration/test_artifacts.py re-derives them
-- and compares digests against the applied database, so a hand-edit here fails the suite.
-- =====================================================================================
CREATE TABLE artifact_manifest_schemas (
  kind           text NOT NULL CHECK (kind IN ('service','model','preprocessing','dataset',
                   'annotation','workflow','tool','agent','policy_set')),
  schema_version text NOT NULL CHECK (schema_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  json_schema    jsonb NOT NULL,
  schema_digest  sha256_digest NOT NULL,
  introduced_in  semver NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),   -- MOS-STORE-210
  PRIMARY KEY (kind, schema_version)
);

COMMENT ON TABLE artifact_manifest_schemas IS
  'MOS-REG-015: JSON Schema 2020-12 per artifact kind, additionalProperties false at '
  'every object level. A kind with no row here cannot be registered at all: the '
  'artifacts schema foreign key refuses it (MOS-REG-013).';

-- >>> BEGIN GENERATED SEED -- python -m medos.registry.schemas
INSERT INTO artifact_manifest_schemas (kind, schema_version, json_schema, schema_digest, introduced_in) VALUES
  ('service', '1.0.0',
   $json${"$id":"https://schemas.medicalos.org/artifact/service_version/1.0.0.json","$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"properties":{"capabilities":{"items":{"additionalProperties":false,"properties":{"id":{"pattern":"^[a-z][a-z0-9_]{2,47}$","type":"string"},"operating_points":{"items":{"additionalProperties":false,"properties":{"id":{"pattern":"^[a-z][a-z0-9_]{2,63}$","type":"string"},"score_threshold":{"maximum":1.0,"minimum":0.0,"type":"number"}},"required":["id","score_threshold"],"type":"object"},"minItems":1,"type":"array"},"outputs":{"items":{"enum":["segmentation","detection","measurement","classification"]},"minItems":1,"type":"array","uniqueItems":true}},"required":["id","outputs"],"type":"object"},"minItems":1,"type":"array"},"compatibility":{"additionalProperties":false,"properties":{"medicalos_api":{"maxLength":64,"minLength":1,"type":"string"},"runtime":{"additionalProperties":false,"properties":{"backend":{"enum":["onnxruntime","pytorch","tensorrt","python"]},"cuda":{"maxLength":32,"minLength":1,"type":"string"},"driver_min":{"maxLength":32,"minLength":1,"type":"string"},"engine":{"pattern":"^[a-z][a-z0-9_-]{1,31}$","type":"string"},"engine_version":{"maxLength":64,"minLength":1,"type":"string"},"gpu_architectures":{"items":{"pattern":"^sm_[0-9]{2,3}$","type":"string"},"minItems":1,"type":"array","uniqueItems":true},"gpu_memory_mib":{"minimum":0,"type":"integer"},"tensorrt_version":{"pattern":"^[0-9]+\\.[0-9]+(\\.[0-9]+)?$","type":"string"}},"required":["engine","engine_version","backend"],"type":"object"},"service_contract":{"maxLength":32,"minLength":1,"type":"string"}},"required":["medicalos_api","service_contract"],"type":"object"},"engineering_acceptance":{"additionalProperties":false,"properties":{"max_gpu_memory_mib":{"minimum":0,"type":"integer"},"p95_wall_clock_seconds":{"exclusiveMinimum":0,"type":"number"},"result_bundle_schema_validity":{"maximum":1.0,"minimum":0.0,"type":"number"}},"required":["p95_wall_clock_seconds","result_bundle_schema_validity"],"type":"object"},"image":{"additionalProperties":false,"properties":{"digest":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"ref":{"pattern":"^[a-z0-9][a-z0-9._/-]{1,254}$","type":"string"}},"required":["ref","digest"],"type":"object"},"legal_manufacturer":{"additionalProperties":false,"properties":{"device_serial_number":{"maxLength":64,"minLength":1,"type":"string"},"id":{"pattern":"^lm_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$","type":"string"},"name":{"maxLength":64,"minLength":1,"type":"string"},"software_versions":{"maxLength":64,"minLength":1,"type":"string"}},"required":["id","name","device_serial_number","software_versions"],"type":"object"},"modalities":{"items":{"pattern":"^[A-Z]{2,16}$","type":"string"},"minItems":1,"type":"array","uniqueItems":true},"mode":{"enum":["native","sealed"]},"models":{"items":{"additionalProperties":false,"properties":{"ref":{"pattern":"^(sv|mv|ps|dv|as|wv|pv)_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$","type":"string"},"role":{"enum":["primary","dependency"]}},"required":["ref","role"],"type":"object"},"minItems":1,"type":"array"},"preprocessing_specs":{"items":{"additionalProperties":false,"properties":{"ref":{"pattern":"^(sv|mv|ps|dv|as|wv|pv)_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$","type":"string"}},"required":["ref"],"type":"object"},"type":"array"},"regulatory_status":{"items":{"additionalProperties":false,"properties":{"evidence_ref":{"type":["string","null"]},"jurisdiction":{"pattern":"^[A-Z]{2}$","type":"string"},"status":{"enum":["not_a_medical_device","not_cleared","cleared","ce_marked","investigational"]}},"required":["jurisdiction","status","evidence_ref"],"type":"object"},"minItems":1,"type":"array"},"resources":{"additionalProperties":false,"properties":{"cpu_millicores":{"minimum":1,"type":"integer"},"gpu_memory_mib":{"minimum":0,"type":"integer"},"gpu_required":{"type":"boolean"},"max_concurrent_jobs":{"minimum":1,"type":"integer"},"memory_mib":{"minimum":1,"type":"integer"}},"required":["gpu_required","cpu_millicores","memory_mib","max_concurrent_jobs"],"type":"object"},"series_selector_ref":{"pattern":"^sel_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$","type":"string"}},"required":["mode","image","capabilities","modalities","models","resources","legal_manufacturer","regulatory_status","compatibility"],"title":"MedicalOS ServiceVersion manifest spec block","type":"object"}$json$::jsonb,
   'sha256:0f89e1c26ec693310ba9e961105a39fdc03509324be561dd0d9378e358b3ad45', '0.3.0'),
  ('model', '1.0.0',
   $json${"$id":"https://schemas.medicalos.org/artifact/model_version/1.0.0.json","$schema":"https://json-schema.org/draft/2020-12/schema","additionalProperties":false,"allOf":[{"if":{"properties":{"weights_availability":{"const":"platform_managed"}},"required":["weights_availability"]},"then":{"properties":{"golden_fixture":{"type":"object"},"weights":{"type":"object"}},"required":["weights","golden_fixture"]}},{"if":{"properties":{"weights_availability":{"const":"vendor_sealed"}},"required":["weights_availability"]},"then":{"properties":{"golden_fixture":{"type":"null"},"weights":{"type":"null"}}}},{"if":{"properties":{"io":{"properties":{"output":{"properties":{"kind":{"enum":["segmentation_logits","segmentation_fractional"]}}}}}},"required":["io"]},"then":{"properties":{"operating_point":{"required":["score_threshold"],"type":"object"}},"required":["operating_point"]}},{"if":{"properties":{"weights":{"properties":{"format":{"const":"tensorrt_plan"}},"required":["format"],"type":"object"}},"required":["weights"]},"then":{"properties":{"runtime":{"properties":{"cuda":{"pattern":"^[0-9]+\\.[0-9]+(\\.[0-9]+)?$","type":"string"},"gpu_architectures":{"minItems":1,"type":"array"},"tensorrt_version":{"pattern":"^[0-9]+\\.[0-9]+(\\.[0-9]+)?$","type":"string"}},"required":["tensorrt_version","cuda","gpu_architectures","driver_min"],"type":"object"}}}}],"properties":{"applicability_envelope_ref":{"pattern":"^ae_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$","type":"string"},"capabilities":{"items":{"pattern":"^[a-z][a-z0-9_]{2,47}$","type":"string"},"minItems":1,"type":"array","uniqueItems":true},"conversion_equivalence":{"additionalProperties":false,"properties":{"fixture_digest":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"max_abs_diff":{"minimum":0.0,"type":"number"},"post_threshold_dice":{"maximum":1.0,"minimum":0.0,"type":"number"}},"required":["fixture_digest","max_abs_diff","post_threshold_dice"],"type":["object","null"]},"derived_from":{"pattern":"^(sv|mv|ps|dv|as|wv|pv)_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$","type":["string","null"]},"evaluation_run_id":{"pattern":"^er_[0-9A-HJKMNP-TV-Z]{10,26}$","type":"string"},"golden_fixture":{"additionalProperties":false,"properties":{"output_shape":{"items":{"minimum":1,"type":"integer"},"maxItems":6,"minItems":2,"type":"array"},"output_tensor_sha256":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"sha256":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"}},"required":["sha256","output_tensor_sha256"],"type":["object","null"]},"io":{"additionalProperties":false,"properties":{"input":{"additionalProperties":false,"properties":{"dtype":{"enum":["float32","float16","int8","uint8","int16","int32"]},"layout":{"pattern":"^[NCZYXDHW]{2,6}$","type":"string"},"name":{"maxLength":64,"minLength":1,"type":"string"},"orientation":{"pattern":"^[LRAPSI]{3}$","type":"string"},"shape":{"items":{"minimum":1,"type":"integer"},"maxItems":6,"minItems":2,"type":"array"},"value_range":{"items":{"type":"number"},"maxItems":2,"minItems":2,"type":"array"}},"required":["name","shape","dtype","layout","orientation"],"type":"object"},"output":{"additionalProperties":false,"properties":{"dtype":{"enum":["float32","float16","int8","uint8","int16","int32"]},"kind":{"enum":["segmentation_logits","segmentation_binary","segmentation_fractional","detection_boxes","scalar"]},"label_map":{"additionalProperties":{"pattern":"^[a-z][a-z0-9_]{0,63}$","type":"string"},"propertyNames":{"pattern":"^[0-9]{1,4}$","type":"string"},"type":"object"},"layout":{"pattern":"^[NCZYXDHW]{2,6}$","type":"string"},"name":{"maxLength":64,"minLength":1,"type":"string"},"shape":{"items":{"minimum":1,"type":"integer"},"maxItems":6,"minItems":2,"type":"array"},"value_range":{"items":{"type":"number"},"maxItems":2,"minItems":2,"type":"array"}},"required":["name","shape","dtype","layout","kind"],"type":"object"}},"required":["input","output"],"type":"object"},"known_failure_modes":{"items":{"maxLength":512,"minLength":1,"type":"string"},"minItems":1,"type":"array"},"not_validated_for":{"items":{"maxLength":512,"minLength":1,"type":"string"},"minItems":1,"type":"array"},"operating_point":{"additionalProperties":false,"properties":{"kind":{"enum":["probability_threshold","logit_threshold","argmax","none"]},"score_threshold":{"maximum":1.0,"minimum":0.0,"type":"number"},"selected_on_evaluation_run":{"pattern":"^er_[0-9A-HJKMNP-TV-Z]{10,26}$","type":"string"},"selection_rule":{"maxLength":256,"minLength":1,"type":"string"}},"required":["kind","selected_on_evaluation_run","selection_rule"],"type":["object","null"]},"preprocessing_spec_ref":{"pattern":"^(sv|mv|ps|dv|as|wv|pv)_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$","type":"string"},"runtime":{"additionalProperties":false,"properties":{"backend":{"enum":["onnxruntime","pytorch","tensorrt","python"]},"cuda":{"maxLength":32,"minLength":1,"type":"string"},"driver_min":{"maxLength":32,"minLength":1,"type":"string"},"engine":{"pattern":"^[a-z][a-z0-9_-]{1,31}$","type":"string"},"engine_version":{"maxLength":64,"minLength":1,"type":"string"},"gpu_architectures":{"items":{"pattern":"^sm_[0-9]{2,3}$","type":"string"},"minItems":1,"type":"array","uniqueItems":true},"gpu_memory_mib":{"minimum":0,"type":"integer"},"tensorrt_version":{"maxLength":32,"minLength":1,"type":"string"}},"required":["engine","engine_version","backend"],"type":"object"},"weights":{"additionalProperties":false,"properties":{"digest":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"format":{"enum":["onnx","torchscript","tensorrt_plan","safetensors"]},"oci_ref":{"pattern":"^[a-z0-9][a-z0-9._/-]{1,254}$","type":"string"},"size_bytes":{"minimum":1,"type":"integer"}},"required":["oci_ref","digest","format","size_bytes"],"type":["object","null"]},"weights_availability":{"enum":["platform_managed","vendor_sealed"]}},"required":["capabilities","weights_availability","preprocessing_spec_ref","io","runtime","evaluation_run_id","not_validated_for","known_failure_modes"],"title":"MedicalOS ModelVersion manifest spec block","type":"object"}$json$::jsonb,
   'sha256:9d21b573f4a3ebbc76193d559556ba9bfe0785f9c874ff3d32fbbaf83a529e33', '0.3.0');
-- <<< END GENERATED SEED


-- =====================================================================================
-- 4. `artifacts`.  Section 12.9's DDL, column for column, plus divergences 1, 2 and 4.
--
-- ONE table with a kind discriminator and no per-kind artifact table (`MOS-STORE-253`,
-- `MOS-REG-013`). `kind` is the FAMILY-level value (`service`, `model`, ...); chapter 6
-- writes the same fact as a version-level kind (`service_version`, ...) and the mapping is
-- fixed -- medos/registry/lifecycle.py is the one statement of it.
-- =====================================================================================
CREATE TABLE artifacts (
  id uuid PRIMARY KEY DEFAULT uuid_generate_v7(),

  -- Divergence 1. The id a manifest reference resolves against (MOS-REG-025). Chapter 6's
  -- own examples are `mv_pulmo_effusion_unet_3_2_1`, `ps_pulmo_effusion_prep_2_0_0` and
  -- `sv_01JQ8Z3K2M`, so the pattern admits the slug form and the ULID form alike; the
  -- prefix is the version-level kind's initials and is checked against `kind` below.
  public_id text NOT NULL UNIQUE
    CHECK (public_id ~ '^(sv|mv|ps|dv|as|wv|pv)_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$'),

  tenant_id uuid REFERENCES tenants(id) ON DELETE RESTRICT,   -- NULL = platform catalogue
  kind text NOT NULL,
  family text NOT NULL CHECK (length(family) BETWEEN 1 AND 128 AND family ~
    '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$'),
  version semver NOT NULL,
  version_major integer NOT NULL CHECK (version_major >= 0),
  version_minor integer NOT NULL CHECK (version_minor >= 0),
  version_patch integer NOT NULL CHECK (version_patch >= 0),
  version_pre text NOT NULL DEFAULT '',

  lifecycle_status text NOT NULL DEFAULT 'DRAFT',   -- MOS-REG-020 (ch. 6) is normative
  status_reason text,
  published_by text NOT NULL CHECK (published_by <> ''),

  manifest_schema_version text NOT NULL,
  manifest jsonb NOT NULL,
  -- Chapter 6 calls this `content_digest` (MOS-REG-017); chapter 12 calls it
  -- `manifest_digest` and owns the physical form. Same value, one column.
  manifest_digest sha256_digest NOT NULL UNIQUE,

  -- MOS-REG-084: the signed unit is the OCI image manifest whose config blob is the
  -- MedicalOS artifact manifest. This column is that manifest's digest -- the thing
  -- `cosign verify` is run against at registry admission and again at node pull
  -- (MOS-REG-089). NULL for kinds that are not OCI artifacts (MOS-REG-112).
  oci_image_digest oci_digest,
  bundle_bucket text,
  bundle_object_key text,
  bundle_digest sha256_digest,
  bundle_size_bytes bigint CHECK (bundle_size_bytes IS NULL OR bundle_size_bytes > 0),

  signature_alg text CHECK (signature_alg IN ('cosign-sigstore','ed25519')),
  signature bytea,
  signer_identity text,
  sbom_object_key text,                            -- MOS-REG-087: CycloneDX 1.6 referrer

  sealed_at timestamptz NOT NULL DEFAULT now(),    -- ch. 6's `published_at` (divergence 4)
  created_at timestamptz NOT NULL DEFAULT now(),   -- divergence 2, MOS-STORE-210
  updated_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT artifacts_tenant_id_uk UNIQUE (tenant_id, id),
  CONSTRAINT artifacts_family_version_uk UNIQUE (kind, family, version),
  CONSTRAINT artifacts_schema_fk FOREIGN KEY (kind, manifest_schema_version)
    REFERENCES artifact_manifest_schemas (kind, schema_version),

  -- The permitted status set is per kind and is MOS-REG-020 (ch. 6) rendered as a
  -- constraint. This mirrors that table exactly and MUST NOT diverge from it: a sealed
  -- evidence artifact carries SEALED/DEFECTIVE (ch. 7 MOS-EVID-014) and a versioned
  -- executable carries the eight-value lifecycle. MOS-REG-038: STAGING, DEPLOYED and
  -- PRODUCTION appear in no branch -- liveness is Deployment's answer (MOS-REG-072).
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

  -- MOS-REG-021: a suspension names its reason, and so does the un-suspension that
  -- resolves it. A suspended version with no reason is an outage nobody can lift.
  CHECK (lifecycle_status <> 'SUSPENDED' OR status_reason IS NOT NULL),
  CHECK ((signature IS NULL) = (signature_alg IS NULL)),
  CHECK ((bundle_object_key IS NULL) = (bundle_digest IS NULL)),

  -- The three integer columns are the resolver's sort key (MOS-REG-060) and `version` is
  -- what a human reads. Two representations of one number drift silently, so the row
  -- refuses to hold a pair that disagrees.
  CONSTRAINT artifacts_version_parts_agree CHECK (
    version = version_major || '.' || version_minor || '.' || version_patch ||
      CASE WHEN version_pre = '' THEN '' ELSE '-' || version_pre END),

  -- Divergence 1's other half: the public id's prefix names the kind. `pv_` is
  -- `policy_set_version` (MOS-REG-112), whose family-level kind is `policy_set`.
  CONSTRAINT artifacts_public_id_prefix_matches_kind CHECK (
    split_part(public_id, '_', 1) = CASE kind
      WHEN 'service' THEN 'sv' WHEN 'model' THEN 'mv' WHEN 'preprocessing' THEN 'ps'
      WHEN 'dataset' THEN 'dv' WHEN 'annotation' THEN 'as' WHEN 'workflow' THEN 'wv'
      WHEN 'policy_set' THEN 'pv' ELSE split_part(public_id, '_', 1) END)
);

CREATE INDEX artifacts_resolution_idx
  ON artifacts (kind, family, version_major DESC, version_minor DESC, version_patch DESC);
CREATE INDEX artifacts_status_idx ON artifacts (kind, lifecycle_status);
CREATE INDEX artifacts_tenant_idx ON artifacts (tenant_id) WHERE tenant_id IS NOT NULL;

COMMENT ON TABLE artifacts IS
  'MOS-STORE-253 / MOS-REG-013: the single table backing every immutable, signed, '
  'versioned bundle. There is no per-kind artifact table. Section 15.2.6: "One '
  'artifacts table with per-kind JSON-Schema''d manifests".';
COMMENT ON COLUMN artifacts.oci_image_digest IS
  'MOS-REG-084: the signed unit is the OCI image manifest whose config blob is the '
  'MedicalOS artifact manifest. Signature, SBOM and attestations attach to THIS digest.';
COMMENT ON COLUMN artifacts.lifecycle_status IS
  'MOS-REG-020/021/022. Lifecycle ONLY (MOS-REG-038): whether a version is live is '
  'Deployment''s answer (MOS-REG-072), never this column''s.';


-- =====================================================================================
-- 5. Immutability.  MOS-REG-018, MOS-STORE-254, MOS-STORE-228.
--
--   "An `artifact` row's `manifest`, `content_digest`, `version` and `oci_ref` MUST be
--    immutable after insert ... Only `lifecycle_status` and `status_reason` are mutable."
--
-- Two layers, catching different things, exactly as 0009 does for `validation_reports`:
--
--   * the column-level GRANT in section 8 bounds `medicalos_app`, which is what the API
--     and the workers connect as;
--   * the TRIGGER binds every role, including `medicalos_owner` and a superuser psql
--     session. That is the case that actually rewrites a published manifest, and a GRANT
--     cannot reach it.
--
-- `BEFORE UPDATE OF <columns>` fires when a column is NAMED in the SET list, whether or
-- not the value changes -- which is the behaviour wanted here: `UPDATE artifacts SET
-- manifest = manifest` is an attempt to rewrite a published manifest and is refused.
-- `updated_at` is absent from the list because `touch_updated_at()` writes it on exactly
-- the permitted updates.
-- =====================================================================================
CREATE FUNCTION artifacts_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION
    'artifacts is immutable except lifecycle_status and status_reason: % is not permitted',
    TG_OP
    USING ERRCODE = 'MOS05',
          HINT = 'MOS-REG-018: the manifest, its digest, the version and the signed OCI '
                 'digest are frozen at publish. A corrected artifact is a NEW version '
                 '(MOS-REG-022); a corrected status is a lifecycle transition.';
END;
$$;
ALTER FUNCTION artifacts_forbid_change() OWNER TO medicalos_owner;

CREATE TRIGGER artifacts_immutable
  BEFORE UPDATE OF id, public_id, tenant_id, kind, family, version, version_major,
    version_minor, version_patch, version_pre, published_by, manifest_schema_version,
    manifest, manifest_digest, oci_image_digest, bundle_bucket, bundle_object_key,
    bundle_digest, bundle_size_bytes, signature_alg, signature, signer_identity,
    sbom_object_key, sealed_at, created_at
  OR DELETE ON artifacts
  FOR EACH ROW EXECUTE FUNCTION artifacts_forbid_change();

CREATE TRIGGER artifacts_touch BEFORE UPDATE ON artifacts
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- =====================================================================================
-- 6. The lifecycle graph and the changelog append.  MOS-REG-021, MOS-REG-022, MOS-REG-010.
--
-- The graph is `MOS-REG-021`'s table, edge for edge, as an array of `FROM>TO` strings so
-- that a test can read it back out of `pg_get_functiondef` and compare it with
-- medos/registry/lifecycle.py's `TRANSITIONS`. Two statements of one graph is a
-- duplication; two statements that CANNOT be compared is drift.
--
-- Why the graph is enforced in the database and not only in the API: a `RECALLED` version
-- is a recalled medical device. `MOS-REG-022` makes the recall irreversible, and an
-- invariant that holds only for callers who went through the API is not an invariant --
-- the case it has to survive is the operator with a psql prompt at 2am.
-- =====================================================================================
CREATE FUNCTION artifacts_lifecycle_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  edges text[] := ARRAY[
    -- MOS-REG-021, row by row.
    'DRAFT>REGISTERED',
    'REGISTERED>VALIDATING',
    'VALIDATING>VALIDATED', 'VALIDATING>REGISTERED',
    'VALIDATED>APPROVED',
    'VALIDATED>DEPRECATED', 'APPROVED>DEPRECATED',
    -- "REGISTERED, VALIDATING, VALIDATED, APPROVED, DEPRECATED -> SUSPENDED"
    'REGISTERED>SUSPENDED', 'VALIDATING>SUSPENDED', 'VALIDATED>SUSPENDED',
    'APPROVED>SUSPENDED', 'DEPRECATED>SUSPENDED',
    -- "any -> RECALLED"
    'DRAFT>RECALLED', 'REGISTERED>RECALLED', 'VALIDATING>RECALLED', 'VALIDATED>RECALLED',
    'APPROVED>RECALLED', 'DEPRECATED>RECALLED', 'SUSPENDED>RECALLED',
    -- The evidence-plane pair (MOS-EVID-014); the registry stores, never originates.
    'SEALED>DEFECTIVE'
  ];
  prev text;
BEGIN
  IF NEW.lifecycle_status = OLD.lifecycle_status THEN
    RETURN NEW;
  END IF;

  IF OLD.lifecycle_status = 'RECALLED' THEN
    RAISE EXCEPTION 'artifact % is RECALLED; RECALLED is irreversible', OLD.public_id
      USING ERRCODE = 'MOS07',
            HINT = 'MOS-REG-022: a recalled defect that is later fixed becomes a NEW '
                   'version, never a status reversal.';
  END IF;

  IF OLD.lifecycle_status = 'SUSPENDED' THEN
    -- MOS-REG-021: "SUSPENDED -> previous status ... requires `status_reason` naming the
    -- resolved issue". No column remembers the previous status, so it is read from the
    -- changelog -- the last recorded status of this row that was not SUSPENDED.
    SELECT c.row_json ->> 'lifecycle_status' INTO prev
      FROM registry_changelog c
     WHERE c.table_name = 'artifact'
       AND c.row_id = OLD.id::text
       AND c.row_json ->> 'lifecycle_status' <> 'SUSPENDED'
     ORDER BY c.epoch DESC
     LIMIT 1;
    IF prev IS NULL THEN
      RAISE EXCEPTION 'artifact %: no pre-suspension status is recorded', OLD.public_id
        USING ERRCODE = 'MOS07',
              HINT = 'MOS-REG-021 restores the PREVIOUS status. With no record of one, '
                     'restoring is guessing, and the guess would re-enter service.';
    END IF;
    IF NEW.lifecycle_status <> prev THEN
      RAISE EXCEPTION 'artifact %: un-suspension restores %, not %',
        OLD.public_id, prev, NEW.lifecycle_status
        USING ERRCODE = 'MOS07', HINT = 'MOS-REG-021.';
    END IF;
    IF NEW.status_reason IS NULL OR length(btrim(NEW.status_reason)) = 0 THEN
      RAISE EXCEPTION 'artifact %: un-suspension requires a status_reason', OLD.public_id
        USING ERRCODE = 'MOS07',
              HINT = 'MOS-REG-021: the reason names the RESOLVED issue.';
    END IF;
    RETURN NEW;
  END IF;

  IF NOT (OLD.lifecycle_status || '>' || NEW.lifecycle_status = ANY (edges)) THEN
    RAISE EXCEPTION 'artifact %: % -> % is not a transition in the lifecycle graph',
      OLD.public_id, OLD.lifecycle_status, NEW.lifecycle_status
      USING ERRCODE = 'MOS07', HINT = 'MOS-REG-021 is the whole graph.';
  END IF;
  RETURN NEW;
END;
$$;
ALTER FUNCTION artifacts_lifecycle_guard() OWNER TO medicalos_owner;

CREATE TRIGGER artifacts_lifecycle BEFORE UPDATE ON artifacts
  FOR EACH ROW EXECUTE FUNCTION artifacts_lifecycle_guard();

CREATE FUNCTION artifacts_changelog() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  who text;
BEGIN
  -- `medicalos.actor` is set by the caller that knows the principal; `current_user` is
  -- the honest fallback and is never NULL, so `actor` cannot be empty. MOS-REG-003 wants
  -- the acting identity on the AuditEvent too, which medos/registry/repo.py writes.
  who := coalesce(nullif(current_setting('medicalos.actor', true), ''), current_user);
  INSERT INTO registry_changelog (table_name, row_id, op, row_json, actor, tenant_id)
  VALUES ('artifact', NEW.id::text, TG_OP, to_jsonb(NEW), who, NEW.tenant_id);
  RETURN NULL;
END;
$$;
ALTER FUNCTION artifacts_changelog() OWNER TO medicalos_owner;

CREATE TRIGGER artifacts_changelog_append AFTER INSERT OR UPDATE ON artifacts
  FOR EACH ROW EXECUTE FUNCTION artifacts_changelog();

CREATE FUNCTION registry_changelog_forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'registry_changelog is append-only: % is not permitted', TG_OP
    USING ERRCODE = 'MOS05',
          HINT = 'MOS-REG-010/MOS-REG-011: the snapshot at any epoch is reconstructed '
                 'from this table alone. An edited row is a rewritten history of which '
                 'model version produced a patient result.';
END;
$$;
ALTER FUNCTION registry_changelog_forbid_change() OWNER TO medicalos_owner;

CREATE TRIGGER registry_changelog_append_only
  BEFORE UPDATE OR DELETE ON registry_changelog
  FOR EACH ROW EXECUTE FUNCTION registry_changelog_forbid_change();


-- =====================================================================================
-- 7. Row-level security.  MOS-REG-107, MOS-SEC-072, MOS-SEC-077, MOS-STORE-229.
--
-- `artifacts.tenant_id` is NULLABLE and NULL means the platform catalogue (section 12.9),
-- so the read policy admits two classes: the platform's rows and this tenant's. The
-- `current_tenant_id() IS NOT NULL` conjunct is not redundant -- it forces the function to
-- be evaluated whatever order the planner chooses, so a session with NO tenant bound gets
-- 42704 (MOS-STORE-224, "a connection with no tenant context is a connection with no
-- access") deterministically rather than depending on OR short-circuiting.
--
-- The WITH CHECK is narrower than the USING on purpose: a tenant may read the platform
-- catalogue and may write only its own rows. A platform-catalogue artifact (tenant_id
-- NULL) is deployment configuration, written by the bootstrap role exactly as
-- `pacs_backends` is (MOS-STORE-355), and there is deliberately NO second policy admitting
-- it for `medicalos_owner`: `MOS-STORE-225` forbids a permissive escape-hatch policy, and
-- tests/integration/test_tenancy.py::test_there_is_no_permissive_escape_hatch_policy
-- enforces that every policy in this schema is named `*_tenant_isolation` and is bound to
-- `current_tenant_id()`. A policy with `USING (true)` for one role is the thing that rule
-- exists to stop, whatever its intended use.
--
-- MOS-REG-107 also requires that private artifacts not be enumerable "including through
-- 404-vs-403 timing"; RLS gives that shape for free, and medos/registry/errors.py answers
-- 404 rather than 403 for an artifact the tenant cannot see.
--
-- REPORTED, NOT RESOLVED: chapter 6 section 6.3 carries `artifact_family.visibility` with
-- three values (`private`, `org`, `public`) and `owner_org_id`; section 12.9 has neither
-- an `artifact_family` table nor a visibility column, so this schema can express exactly
-- two visibility classes -- own-tenant and platform-wide. `org`-scoped visibility and
-- `MOS-REG-089`'s "source repo matches `artifact_family.owner_org_id`" have nowhere to
-- resolve. In this component's report.
-- =====================================================================================
ALTER TABLE artifacts OWNER TO medicalos_owner;
ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts FORCE  ROW LEVEL SECURITY;
CREATE POLICY artifacts_tenant_isolation ON artifacts
  USING (current_tenant_id() IS NOT NULL
         AND (tenant_id IS NULL OR tenant_id = current_tenant_id()))
  WITH CHECK (tenant_id = current_tenant_id());

ALTER TABLE registry_changelog OWNER TO medicalos_owner;
ALTER TABLE registry_changelog ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry_changelog FORCE  ROW LEVEL SECURITY;
CREATE POLICY registry_changelog_tenant_isolation ON registry_changelog
  USING (current_tenant_id() IS NOT NULL
         AND (tenant_id IS NULL OR tenant_id = current_tenant_id()))
  WITH CHECK (tenant_id IS NULL OR tenant_id = current_tenant_id());

ALTER TABLE artifact_manifest_schemas OWNER TO medicalos_owner;


-- =====================================================================================
-- 8. Grants.  MOS-STORE-228, MOS-STORE-219, MOS-SEC-073.
--
-- `MOS-STORE-228` puts `artifacts` in the COLUMN-LEVEL revocation group, with exactly two
-- mutable columns. Written as a positive grant rather than a REVOKE of the other
-- twenty-four: same effect, and it fails safe when a column is added later -- a new column
-- is not grantable by omission, where a REVOKE list would silently leave it writable.
-- `updated_at` is in the grant because `touch_updated_at()` writes it inside the
-- permitted update and the trigger runs as the invoking role.
-- =====================================================================================
GRANT SELECT, INSERT ON artifacts TO medicalos_app;
GRANT UPDATE (lifecycle_status, status_reason, updated_at) ON artifacts TO medicalos_app;
REVOKE DELETE ON artifacts FROM medicalos_app;
GRANT SELECT ON artifacts TO medicalos_readonly;

GRANT SELECT, INSERT ON registry_changelog TO medicalos_app;
REVOKE UPDATE, DELETE ON registry_changelog FROM medicalos_app;   -- MOS-REG-010, verbatim
GRANT SELECT ON registry_changelog TO medicalos_readonly;
GRANT USAGE, SELECT ON SEQUENCE registry_epoch TO medicalos_app;

-- MOS-STORE-219: platform-global, SELECT only. Written by the migration introducing a
-- schema version and by nothing else (MOS-STORE-355).
GRANT SELECT ON artifact_manifest_schemas TO medicalos_app;
GRANT SELECT ON artifact_manifest_schemas TO medicalos_readonly;
REVOKE INSERT, UPDATE, DELETE ON artifact_manifest_schemas FROM medicalos_app;


-- =====================================================================================
-- 9. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================
DO $$
DECLARE
  n int;
  bad text;
  mutable text[] := ARRAY['lifecycle_status','status_reason','updated_at'];
BEGIN
  -- MOS-STORE-229 / MOS-SEC-077: a tenant_id column without forced row security.
  SELECT count(*) INTO n FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
   WHERE ns.nspname = 'public' AND c.relforcerowsecurity
     AND c.relname IN ('artifacts','registry_changelog');
  IF n <> 2 THEN
    RAISE EXCEPTION 'expected FORCE ROW LEVEL SECURITY on 2 tables, found %', n;
  END IF;

  -- MOS-STORE-228: DELETE is denied on both, for every role but the owner.
  SELECT string_agg(DISTINCT table_name, ', ') INTO bad
    FROM information_schema.role_table_grants
   WHERE table_schema = 'public' AND privilege_type = 'DELETE'
     AND grantee <> 'medicalos_owner'
     AND table_name IN ('artifacts','registry_changelog','artifact_manifest_schemas');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-STORE-228: DELETE is granted on %', bad;
  END IF;

  -- MOS-REG-018: `medicalos_app` may UPDATE exactly the two mutable columns (plus the
  -- timestamp the trigger writes). Any other column would be an editable published
  -- manifest.
  SELECT string_agg(column_name, ', ' ORDER BY column_name) INTO bad
    FROM information_schema.column_privileges
   WHERE table_schema = 'public' AND table_name = 'artifacts'
     AND privilege_type = 'UPDATE' AND grantee = 'medicalos_app'
     AND NOT (column_name = ANY (mutable));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-REG-018: medicalos_app can UPDATE % on artifacts', bad;
  END IF;

  -- MOS-REG-015: every seeded kind has a schema, and every schema is closed. The count
  -- is asserted rather than the content: the content is compared against the in-repo
  -- source by tests/integration/test_artifacts.py, which can parse it.
  SELECT count(*) INTO n FROM artifact_manifest_schemas;
  IF n <> 2 THEN
    RAISE EXCEPTION 'expected 2 seeded manifest schemas (service, model), found %', n;
  END IF;
  SELECT string_agg(kind, ', ') INTO bad FROM artifact_manifest_schemas
   WHERE json_schema -> 'additionalProperties' <> 'false'::jsonb;
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-REG-015: schema for kind % is not closed at the root', bad;
  END IF;

  -- MOS-REG-013: the kind enum is closed through the schema foreign key. A kind with no
  -- seeded schema cannot produce a row, which is how section 15.2.6's "no WorkflowVersion
  -- before 0.4.0" becomes a constraint rather than a code review.
  SELECT count(*) INTO n FROM pg_constraint
   WHERE conname = 'artifacts_schema_fk' AND contype = 'f';
  IF n <> 1 THEN
    RAISE EXCEPTION 'artifacts_schema_fk is missing; the kind enum would not be closed';
  END IF;

  -- The three triggers that make MOS-REG-018, MOS-REG-021 and MOS-REG-010 structural.
  SELECT count(*) INTO n FROM pg_trigger
   WHERE NOT tgisinternal AND tgname IN
     ('artifacts_immutable','artifacts_lifecycle','artifacts_changelog_append',
      'registry_changelog_append_only','artifacts_touch');
  IF n <> 5 THEN
    RAISE EXCEPTION 'expected 5 registry triggers, found %', n;
  END IF;
END $$;
