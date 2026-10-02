-- =====================================================================================
-- 0003_audit_provenance.up.sql -- weeks 3-5, the "Plus:" clause of
-- docs/spec/15-delivery.md section 15.2.4:
--
--   "`audit_events` append-only at the database role level (`REVOKE UPDATE, DELETE` from
--    the application role -- one line in a migration now, an ugly backfill later); the
--    full provenance record"
--
-- ADDITIVE, like 0002 and 0004. Nothing here drops, renames or narrows an existing
-- column. In particular the weeks 1-2 provenance columns on `results` stay exactly where
-- they are and stay sealed by `results_provenance_sealed`: `result_provenance` is the
-- WIDENED record MOS-SAFE-083 requires, joined 1:1, not a replacement. Two readers exist
-- for the narrow set already (the OHIF panel and tests/e2e/test_demo.py) and removing a
-- column to add a better one is a migration nobody needs.
--
-- Requirements implemented, by id:
--
--   MOS-SEC-146   the AuditEvent field set, normatively, in chapter 12 section 12.13's
--                 physical rendering (flattened actor columns, monthly partitions).
--   MOS-SEC-147   `actor` + `on_behalf_of` MUST be able to express
--                 "`pulmo.effusion@2.1.0` acting for `u_7d2f4a` in `job_01JB8N...`".
--                 Four actor columns, three delegation columns and two job columns --
--                 never a flat `actor: "user_123"` string, which "cannot answer an
--                 access question about a delegated execution, which is the only kind of
--                 access question this platform generates".
--   MOS-SEC-148   the permission CLASS is carried on the row, because it is the class
--                 that decides whether an event was obligatory (`phi`, `clinical`,
--                 `admin`, `governance` on BOTH allow and deny) or sampleable (`read`).
--                 A trail that cannot say which of its rows were mandatory cannot be
--                 shown to be complete.
--   MOS-SEC-149   the audit row goes in the transaction that made the change. The schema
--                 half of that is the absence of any queue, outbox or async writer here.
--   MOS-SEC-150   `request_id` NOT NULL, so the authorization event and the effect event
--                 of a boundary-crossing read are one joinable pair.
--   MOS-SEC-151   `prev_hash`/`hash` over RFC 8785 canonical JSON, per tenant, from the
--                 tenant's genesis constant.
--   MOS-SEC-153   `audit.verify` recomputes the chain and reports the first divergent
--                 `seq`; `audit_row_hash()` is the single function both the writer and
--                 the verifier use, so a verifier cannot disagree with a writer.
--   MOS-SEC-154   RANGE-partitioned by month.
--   MOS-SEC-155   the joins: audit -> Job (`job_id`), audit -> trace (`trace_id`,
--                 `span_id`), authorization -> effect (`request_id`), and
--                 Job -> Result -> generated SOPInstanceUIDs (the provenance record).
--   MOS-STORE-228 `medicalos_app` is denied UPDATE and DELETE AT THE GRANT LEVEL on
--                 `audit_events` and `result_provenance`; `forbid_mutation()` is the
--                 second layer and, unlike a grant, also binds the owner.
--   MOS-SAFE-082  exactly one provenance record per Result, in the same transaction,
--                 "impossible by foreign-key constraint, not by convention".
--   MOS-SAFE-083  the record's field set, sections A-F.
--   MOS-SAFE-084  `series_considered[]` carries the REJECTED series and their reasons.
--   MOS-SAFE-085  `outputs[].destination.qido_verified_at` is set only after a QIDO-RS
--                 query returned the expected instance count.
--   MOS-SAFE-086  no PHI in the record (enforced in medos/core/provenance.py).
--   MOS-SAFE-090  provenance append-only at the role level, with a per-tenant hash chain.
--   MOS-SEC-072/073/077, MOS-STORE-217/218/221/223/229 -- every new tenant-owned table
--                 gets tenant_id, composite FKs, ENABLE + FORCE RLS and a policy, and
--                 0002's assertions are re-run at the end of this file over them.
--
-- DELIBERATE DEVIATIONS FROM THE SPEC, ALL NARROWER:
--
--   * `audit_chain_checkpoints`, `audit_archives` and `policy_decisions` (MOS-SEC-152,
--     MOS-SEC-154's archive half, MOS-SEC-067) are NOT created. Chapter 8 section 8.10
--     schedules them at 0.2.0, and a "signed" checkpoint with no platform signing key is
--     a table of unsigned rows with a column called `signature`. `policy_decision_id` and
--     `break_glass_id` EXIST as nullable columns so MOS-SEC-155's join needs no later
--     ALTER; they are null throughout 0.1.0 because there is no PDP to produce one.
--   * `audit_events.patient_id` and `result_provenance.patient_internal_id` /
--     `study_internal_id` are nullable and carry NO foreign key. The `patients` and
--     `studies` projections arrive with the Gateway (0005_gateway.up.sql) and a job in
--     this slice is still created directly against a StudyInstanceUID, so a NOT NULL FK
--     would make a provenance record unwritable for the path that actually runs.
--     MOS-STORE-279 is explicit that the UIDs are the half that survives a projection
--     rebuild, and every UID column is present and NOT NULL.
--   * `result_provenance.input_series_ids` and `service_id uuid` (chapter 12 section
--     12.11) are omitted for the same reason: there is no `series` projection and no
--     `services` table in this slice.
--   * `hash`/`prev_hash` are `text` holding lowercase hex rather than chapter 12's
--     `bytea`. Hex is chapter 8's own spelling ("sha256 hex", MOS-SEC-146), it is what an
--     offline verifier reads out of an exported JSON bundle (MOS-SAFE-089), and it needs
--     no `encode()` on every comparison.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 0. Collision guard.
--
-- `audit_events` is this component's table. The Gateway block (0004) needs it too --
-- MOS-DATA-022: "this is the only point in the architecture where a per-patient image
-- access can be recorded" -- and during parallel authoring it briefly carried its own
-- copy. One table, one definition: if a future migration creates a second one, this
-- fails here with a message that says what to do instead of a bare 42P07 three hundred
-- lines further down.
-- =====================================================================================
DO $$
BEGIN
  IF to_regclass('public.audit_events') IS NOT NULL THEN
    RAISE EXCEPTION
      'audit_events already exists. It is defined ONCE, here (MOS-SEC-146). A block that '
      'needs a new column adds it with ALTER TABLE in its own migration; it does not '
      'create a second audit table.'
      USING ERRCODE = '42P07';
  END IF;
END $$;


-- =====================================================================================
-- 1. jcs_canonical -- RFC 8785 in the database.  MOS-SEC-151.
--
-- The hash chain is computed by a BEFORE INSERT trigger and NOT by the application, for
-- one reason: a chain the writer computes is a chain the writer can decline to compute.
-- Every language that ever writes an audit row -- Python now, the Go control plane at
-- MOS-REL-084 -- gets the identical `seq`, `prev_hash` and `hash` without reimplementing
-- JCS, and `audit.verify` (MOS-SEC-153) recomputes with the SAME function, so a verifier
-- that disagrees with a writer is not expressible.
--
-- `medos/core/canonical.py` is the Python twin and hashes DIFFERENT documents (the
-- provenance record's own `record_hash` chain is computed here too, by
-- `result_provenance_chain`); the Python one exists for the pure-function digests of
-- MOS-SAFE-021 and for tests. Neither chain is ever hashed by both.
--
-- KNOWN NARROWING, stated rather than discovered: object keys are ordered with the "C"
-- collation, i.e. by UTF-8 byte value, which equals code-point order. JCS specifies
-- UTF-16 code-unit order; the two differ only for keys containing characters above the
-- BMP. Every key hashed here is drawn from the fixed ASCII field sets of MOS-SEC-146 and
-- MOS-SAFE-083, so the orders coincide. Control characters inside string VALUES are
-- emitted by PostgreSQL's jsonb writer as `\uXXXX` where JCS prefers the two-character
-- forms; audit and provenance field values are UIDs, codes, digests and enums.
-- =====================================================================================
CREATE FUNCTION jcs_canonical(v jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
  k text;
  e jsonb;
  parts text[] := ARRAY[]::text[];
  n numeric;
BEGIN
  CASE jsonb_typeof(v)
    WHEN 'object' THEN
      FOR k IN SELECT key FROM jsonb_object_keys(v) AS t(key) ORDER BY key COLLATE "C" LOOP
        parts := parts || (to_jsonb(k)::text || ':' || jcs_canonical(v -> k));
      END LOOP;
      RETURN '{' || array_to_string(parts, ',') || '}';
    WHEN 'array' THEN
      FOR e IN SELECT value FROM jsonb_array_elements(v) LOOP
        parts := parts || jcs_canonical(e);
      END LOOP;
      RETURN '[' || array_to_string(parts, ',') || ']';
    WHEN 'string' THEN
      -- to_jsonb() of the extracted text re-escapes exactly once. `v::text` would keep
      -- jsonb's own rendering, which is the same string, but going through the extractor
      -- makes the round trip explicit.
      RETURN to_jsonb(v #>> '{}')::text;
    WHEN 'number' THEN
      n := (v #>> '{}')::numeric;
      -- ECMAScript has ONE number type: 3.0 prints as `3`. A record whose duration_ms
      -- arrived as 120 on one attempt and 120.0 on the next must hash identically or the
      -- chain reports tampering on a retry.
      IF n = trunc(n) AND abs(n) < 9007199254740992::numeric THEN
        RETURN trunc(n)::bigint::text;
      END IF;
      RETURN trim(trailing '.' from
                  trim(trailing '0' from to_char(n, 'FM9999999999999999990.999999999999999999')));
    WHEN 'boolean' THEN
      RETURN v #>> '{}';
    ELSE
      RETURN 'null';
  END CASE;
END;
$$;

COMMENT ON FUNCTION jcs_canonical(jsonb) IS
  'RFC 8785 canonical JSON. MOS-SEC-151. The one canonicaliser: the audit chain and the '
  'provenance chain are both computed with it, by trigger, so a writer cannot skip it '
  'and a verifier cannot disagree with it.';


-- =====================================================================================
-- 2. audit_events.  MOS-SEC-146, chapter 12 section 12.13.
--
-- NO FOREIGN KEY TO `jobs`, and that is chapter 8's rule rather than an omission:
-- `user.delete` is documented as "remove a user (audit rows survive)", MOS-STORE-337
-- forbids a purge from touching `audit_events`, and an FK would make the trail a child
-- of the thing it is evidence about. `job_id` and `job_public_id` are BOTH carried --
-- MOS-STORE-357 makes `public_id` the only job id that appears in a URL, an event
-- envelope or a UI, while the uuid is what MOS-SEC-155's join uses.
-- =====================================================================================
CREATE TABLE audit_events (
  id bigint GENERATED ALWAYS AS IDENTITY,
  public_id text NOT NULL CHECK (public_id ~ '^aud_[0-9A-HJKMNP-TV-Z]{26}$'),
  tenant_id uuid NOT NULL,

  -- MOS-SEC-146 requires BOTH "so clock skew is visible". recorded_at is
  -- clock_timestamp() and not now(), because now() is the transaction start and would
  -- hide exactly the deciding-to-recording gap the pair exists to expose.
  occurred_at timestamptz NOT NULL DEFAULT now(),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  chain_period date GENERATED ALWAYS AS
    (date_trunc('month', occurred_at AT TIME ZONE 'UTC')::date) STORED,

  -- Per-tenant, gapless, assigned inside the writing transaction by the chain trigger.
  -- Gaplessness is what makes MOS-SEC-153's "report the first seq at which it diverges"
  -- mean anything: with gaps, a deleted row and a never-written row look identical.
  seq bigint NOT NULL CHECK (seq >= 1),

  -- ---------------------------------------------------------------- MOS-SEC-147
  -- FOUR actor columns and THREE delegation columns, never one string.
  -- `service_version` is the kind a capability writes under, which is what makes
  -- "pulmo.effusion@2.1.0 acting for u_7d2f4a in job_01JB8N..." expressible at all.
  actor_kind text NOT NULL CHECK (actor_kind IN
    ('user','service_account','workload','service_version','platform_admin')),
  actor_id text NOT NULL,
  actor_auth text CHECK (actor_auth IS NULL OR actor_auth IN
    ('api_key','oidc','job_token','internal','none')),
  actor_key_id text,

  -- "populated from the Job Token `obo` claim". Nullable because a direct action has no
  -- delegation; the shape CHECK makes a HALF-filled delegation impossible, which is the
  -- state that would answer an access question wrongly rather than not at all.
  --
  -- `text` and not chapter 12's `uuid`: chapter 8 does not type the member, and this
  -- slice's principal ids are opaque strings (`jobs.created_by_id text`, schema.sql).
  -- A uuid column cannot hold chapter 8's own example `u_7d2f4a`.
  on_behalf_of_kind text CHECK (on_behalf_of_kind IN ('user','service_account')),
  on_behalf_of_id text,
  on_behalf_of_role text,

  action text NOT NULL CHECK (action ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$'),
  -- MOS-SEC-148: "Each permission carries a class. Class drives audit and caching."
  action_class text NOT NULL CHECK (action_class IN
    ('read','write','phi','clinical','admin','governance')),
  resource_kind text NOT NULL,
  resource_id text NOT NULL,
  resource_version text,
  outcome text NOT NULL CHECK (outcome IN ('allow','deny','error')),

  -- 0.2.0 joins: present, nullable, and null throughout 0.1.0. See the header.
  policy_decision_id text,
  break_glass_id uuid,

  pep text NOT NULL,

  -- ---------------------------------------------------------------- MOS-SEC-155
  job_id uuid,
  job_public_id text CHECK (job_public_id IS NULL
                            OR job_public_id ~ '^job_[0-9A-HJKMNP-TV-Z]{26}$'),
  root_job_id uuid,
  trace_id text NOT NULL CHECK (trace_id ~ '^[0-9a-f]{32}$'),
  span_id text CHECK (span_id IS NULL OR span_id ~ '^[0-9a-f]{16}$'),
  -- MOS-SEC-150: the authorization event and the effect event of a boundary-crossing
  -- read are two rows joined by this. NOT NULL, because a pair with one id is not a pair.
  request_id text NOT NULL,

  -- Nullable, deliberately, and chapter 12 section 12.13's reasoning is why: the
  -- retention sweep, the reconciler and the outbox relay have no client address at all,
  -- and "a fabricated address is indistinguishable from a real connection, which is
  -- exactly the confusion an audit trail exists to prevent". Those rows carry
  -- actor_kind = 'workload'.
  source_ip inet,
  user_agent text CHECK (user_agent IS NULL OR length(user_agent) <= 200),

  -- Surrogate only, never demographics (MOS-STORE-307). Null in this slice.
  patient_id uuid,
  -- P2. Carried because chapter 12 section 12.13 carries it and because "who looked at
  -- this study" is otherwise a scan. The P1 form (MOS-SEC-106) is `resource_id` when
  -- `resource_kind = 'study'` -- see medos.core.canonical.study_ref.
  study_instance_uid dicom_uid,

  -- MOS-SEC-146: "P0/P1 only; counts, reason codes, old/new non-PHI values."
  detail jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(detail) = 'object'),

  prev_hash text NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{64}$'),
  hash      text NOT NULL CHECK (hash      ~ '^[0-9a-f]{64}$'),

  PRIMARY KEY (id, occurred_at),
  CONSTRAINT audit_events_seq_uk    UNIQUE (tenant_id, seq, occurred_at),
  CONSTRAINT audit_events_public_uk UNIQUE (public_id, occurred_at),
  CONSTRAINT audit_events_obo_shape CHECK (
    (on_behalf_of_kind IS NULL) = (on_behalf_of_id IS NULL)),
  -- The two spellings of one job must agree about whether there is a job at all.
  CONSTRAINT audit_events_job_shape CHECK ((job_id IS NULL) = (job_public_id IS NULL))
) PARTITION BY RANGE (occurred_at);

COMMENT ON TABLE audit_events IS
  'MOS-SEC-146. Append-only at the grant level (MOS-STORE-228) and again at the trigger '
  'level, which also binds the owner. No FK to jobs: audit rows outlive what they are '
  'evidence about (MOS-STORE-337).';


-- =====================================================================================
-- 3. The chain.  MOS-SEC-151, MOS-SEC-153.
-- =====================================================================================
CREATE FUNCTION audit_genesis_hash(p_tenant_id uuid) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(
    sha256(convert_to('medicalos/audit/genesis/v1' || chr(31) || p_tenant_id::text, 'UTF8')),
    'hex');
$$;

COMMENT ON FUNCTION audit_genesis_hash(uuid) IS
  'MOS-SEC-151: "The first row of a tenant MUST use the tenant genesis constant." A pure '
  'function of the tenant id so an offline verifier needs only SHA-256 and the id.';


-- The ONE definition of what an audit row hashes to. Both the BEFORE INSERT trigger and
-- `audit.verify` call it, which is the property MOS-SEC-153 needs: a verifier that can
-- disagree with a writer verifies nothing.
--
-- `hash` is omitted because it is what is being computed. `chain_period` is omitted
-- because it is a STORED generated column materialised AFTER a BEFORE trigger runs, so
-- it is NULL at write time and non-NULL at verify time; it is a pure function of
-- `occurred_at`, which IS hashed, so nothing is left unprotected. `id` is omitted
-- because it is an identity counter local to one database, and a row exported and
-- re-imported must verify.
CREATE FUNCTION audit_row_hash(r jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(sha256(convert_to(jcs_canonical(r - 'hash' - 'chain_period' - 'id'), 'UTF8')),
                'hex');
$$;


CREATE FUNCTION audit_events_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_prev text;
  v_seq  bigint;
BEGIN
  -- Serialises writers within one tenant for the rest of the transaction. This -- and
  -- not the partition-local UNIQUE -- is what makes `seq` globally gapless across
  -- partitions, and it rolls back with the write it numbers, which a SEQUENCE would not:
  -- a rolled-back audit write that burned a number leaves a gap, and to a verifier a gap
  -- and a deletion are the same observation.
  PERFORM pg_advisory_xact_lock(hashtextextended(NEW.tenant_id::text, 0));
  SELECT a.hash, a.seq INTO v_prev, v_seq
    FROM audit_events a
   WHERE a.tenant_id = NEW.tenant_id
   ORDER BY a.seq DESC
   LIMIT 1;
  IF v_seq IS NULL THEN
    v_prev := audit_genesis_hash(NEW.tenant_id);
    v_seq  := 0;
  END IF;
  NEW.prev_hash := v_prev;
  NEW.seq       := v_seq + 1;
  NEW.hash      := audit_row_hash(to_jsonb(NEW));
  RETURN NEW;
END;
$$;


-- MOS-SEC-153: "recompute the chain over a range and report the first `seq` at which it
-- diverges". Returns nothing when the chain is intact, which is what makes it usable as
-- an assertion rather than as a report to read.
CREATE FUNCTION audit_verify_chain(p_tenant_id uuid, p_from bigint DEFAULT 1)
RETURNS TABLE (seq bigint, reason text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  r record;
  v_expected_prev text;
  v_expected_seq  bigint;
BEGIN
  v_expected_prev := NULL;
  v_expected_seq  := NULL;
  FOR r IN SELECT a.* FROM audit_events a
            WHERE a.tenant_id = p_tenant_id AND a.seq >= p_from
            ORDER BY a.seq LOOP
    IF v_expected_seq IS NULL THEN
      v_expected_seq := r.seq;
      v_expected_prev := CASE WHEN r.seq = 1
                              THEN audit_genesis_hash(p_tenant_id)
                              ELSE r.prev_hash END;
    END IF;
    IF r.seq <> v_expected_seq THEN
      seq := r.seq; reason := 'gap: expected seq ' || v_expected_seq; RETURN NEXT; RETURN;
    END IF;
    IF r.prev_hash <> v_expected_prev THEN
      seq := r.seq; reason := 'prev_hash does not match the previous row''s hash';
      RETURN NEXT; RETURN;
    END IF;
    IF r.hash <> audit_row_hash(to_jsonb(r)) THEN
      seq := r.seq; reason := 'row content does not hash to the recorded hash';
      RETURN NEXT; RETURN;
    END IF;
    v_expected_prev := r.hash;
    v_expected_seq  := r.seq + 1;
  END LOOP;
  RETURN;
END;
$$;


-- =====================================================================================
-- 4. Partitions, ownership, row security and THE GRANTS.
--
-- Chapter 8 section 8.9.3, verbatim in intent:
--
--     REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM medicalos_app;
--     GRANT  INSERT, SELECT             ON audit_events TO   medicalos_app;
--
-- The grant is the PRIMARY control -- chapter 8 section 8.10 item 3 tests exactly
-- `has_table_privilege('medicalos_app','audit_events','UPDATE') = false`. The trigger is
-- the second layer, and it is the one that also binds `medicalos_owner`, a superuser
-- session and a psql shell, which is the case that actually destroys an audit trail.
--
-- Applied per PARTITION as well as to the parent, and that is not belt-and-braces: a
-- partition reached DIRECTLY bypasses the parent's triggers and the parent's ACL alike.
-- =====================================================================================
CREATE FUNCTION audit_events_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only (attempted %)', TG_OP
    USING ERRCODE = '42501';
END;
$$;


CREATE FUNCTION audit_events_harden(p_relname text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE format('ALTER TABLE %I OWNER TO medicalos_owner', p_relname);
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', p_relname);
  EXECUTE format('ALTER TABLE %I FORCE  ROW LEVEL SECURITY', p_relname);

  IF NOT EXISTS (SELECT 1 FROM pg_policy p JOIN pg_class c ON c.oid = p.polrelid
                  WHERE c.relname = p_relname
                    AND p.polname = p_relname || '_tenant_isolation') THEN
    -- TO PUBLIC, exactly as 0002 section 10 argues: a policy restricted TO medicalos_app
    -- leaves the FORCEd owner with no applicable policy, which in PostgreSQL means zero
    -- rows -- silent emptiness instead of the 42704 that names the missing context.
    EXECUTE format(
      'CREATE POLICY %I ON %I USING (tenant_id = current_tenant_id()) '
      'WITH CHECK (tenant_id = current_tenant_id())',
      p_relname || '_tenant_isolation', p_relname);
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_trigger
                  WHERE tgrelid = format('public.%I', p_relname)::regclass
                    AND tgname = p_relname || '_no_mutate') THEN
    EXECUTE format(
      'CREATE TRIGGER %I BEFORE UPDATE OR DELETE OR TRUNCATE ON %I '
      'FOR EACH STATEMENT EXECUTE FUNCTION audit_events_immutable()',
      p_relname || '_no_mutate', p_relname);
  END IF;

  EXECUTE format('GRANT  SELECT, INSERT           ON %I TO   medicalos_app', p_relname);
  EXECUTE format('REVOKE UPDATE, DELETE, TRUNCATE ON %I FROM medicalos_app', p_relname);
  EXECUTE format('GRANT  SELECT                   ON %I TO   medicalos_readonly', p_relname);
END;
$$;


-- MOS-SEC-154. Idempotent, so extending the window is a cron line and not a migration.
CREATE FUNCTION audit_events_ensure_partition(p_month date) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  v_start date := date_trunc('month', p_month)::date;
  v_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
  v_name  text := 'audit_events_' || to_char(v_start, 'YYYY_MM');
BEGIN
  IF to_regclass('public.' || quote_ident(v_name)) IS NULL THEN
    EXECUTE format(
      'CREATE TABLE %I PARTITION OF audit_events FOR VALUES FROM (%L) TO (%L)',
      v_name, v_start::timestamptz, v_end::timestamptz);
  END IF;
  PERFORM audit_events_harden(v_name);
  RETURN v_name;
END;
$$;


-- MOS-SEC-154's backstop. It MUST stay empty in a healthy deployment -- but it exists,
-- because an audit write that fails for want of a partition is a state change with no
-- record, which MOS-SEC-149 forbids outright. An empty default partition is a monitoring
-- signal; a missing one is an outage of the audit trail.
CREATE TABLE audit_events_default PARTITION OF audit_events DEFAULT;

CREATE TRIGGER audit_events_chain_trg BEFORE INSERT ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_events_chain();

SELECT audit_events_harden('audit_events');
SELECT audit_events_harden('audit_events_default');

-- Three months back, twelve forward, from the month this runs.
DO $$
DECLARE m date := (date_trunc('month', now() AT TIME ZONE 'UTC') - interval '3 months')::date;
        stop date := (date_trunc('month', now() AT TIME ZONE 'UTC') + interval '12 months')::date;
BEGIN
  WHILE m < stop LOOP
    PERFORM audit_events_ensure_partition(m);
    m := (m + interval '1 month')::date;
  END LOOP;
END $$;

-- MOS-STORE-221: every index serving a tenant-filtered query leads with tenant_id.
CREATE INDEX audit_events_seq_idx   ON audit_events (tenant_id, seq DESC);
CREATE INDEX audit_events_job_idx   ON audit_events (tenant_id, job_public_id, seq)
  WHERE job_public_id IS NOT NULL;
CREATE INDEX audit_events_trace_idx ON audit_events (tenant_id, trace_id, seq);
CREATE INDEX audit_events_req_idx   ON audit_events (tenant_id, request_id, seq);
CREATE INDEX audit_events_study_idx ON audit_events (tenant_id, study_instance_uid, seq)
  WHERE study_instance_uid IS NOT NULL;


-- =====================================================================================
-- 5. result_provenance -- the FULL record.  MOS-SAFE-082/083, chapter 12 section 12.11.
--
-- schema.sql folded a PARTIAL provenance field set into `results` and said in its own
-- comment that the split-out is "a mechanical CREATE TABLE ... SELECT, because
-- MOS-STORE-278 already makes the cardinality exactly one-to-one". This is that
-- split-out, WIDENED to MOS-SAFE-083 sections A-F -- the fields MOS-SAFE-083 names as
-- the ones that "turn a list of version strings into a reproducible record": which
-- series were actually consumed (and which were rejected, and why), the preprocessing
-- version, the de-identification policy version, the evidence dataset version, and where
-- every output object landed.
-- =====================================================================================
CREATE TABLE result_provenance (
  -- ------------------------------------------------ Section A: identity and lineage
  result_id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL,
  provenance_id text NOT NULL CHECK (provenance_id ~ '^prv_[0-9A-HJKMNP-TV-Z]{26}$'),
  job_id uuid NOT NULL,
  job_public_id text NOT NULL CHECK (job_public_id ~ '^job_[0-9A-HJKMNP-TV-Z]{26}$'),
  parent_job_id uuid,
  root_job_id uuid NOT NULL,
  job_depth smallint NOT NULL DEFAULT 0 CHECK (job_depth BETWEEN 0 AND 3),
  idempotency_key text NOT NULL CHECK (idempotency_key ~ '^ik_[a-z2-7]{26}$'),
  record_schema_version text NOT NULL DEFAULT 'provenance/1.0.0',
  sequence_no bigint NOT NULL CHECK (sequence_no >= 1),
  prev_record_hash text NOT NULL CHECK (prev_record_hash ~ '^[0-9a-f]{64}$'),
  record_hash      text NOT NULL CHECK (record_hash      ~ '^[0-9a-f]{64}$'),
  started_at timestamptz NOT NULL,
  finished_at timestamptz NOT NULL,
  duration_ms integer NOT NULL CHECK (duration_ms >= 0),

  -- ------------------------------------------------ Section B: what was CONSUMED
  patient_internal_id uuid,                  -- see the header: nullable, no FK, this slice
  study_internal_id uuid,
  study_instance_uid dicom_uid NOT NULL,
  -- MOS-SAFE-084, the whole reason this column exists: EVERY series triaged, selected
  -- and rejected, with its reason. "The most common silent clinical failure in radiology
  -- AI is analysing the wrong reconstruction; the record must show what else was on the
  -- table."
  series_considered jsonb NOT NULL
    CHECK (jsonb_typeof(series_considered) = 'array'
           AND jsonb_array_length(series_considered) >= 1),
  input_series_uids dicom_uid[] NOT NULL CHECK (cardinality(input_series_uids) >= 1),
  input_instance_uids dicom_uid[] NOT NULL,
  input_instance_count integer NOT NULL CHECK (input_instance_count >= 1),
  input_uid_digest text NOT NULL CHECK (input_uid_digest ~ '^[0-9a-f]{64}$'),
  input_pixel_digest text NOT NULL CHECK (input_pixel_digest ~ '^[0-9a-f]{64}$'),
  -- {origin, spacing, direction, shape, frame_of_reference_uid} per chapter 4.
  geometry jsonb NOT NULL CHECK (jsonb_typeof(geometry) = 'object'),
  -- The envelope evidence: modality, body_part, kernel, slice_thickness_mm, kvp, ...
  acquisition jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- MOS-STORE-251: the de-identification policy version is pinned INTO provenance.
  -- {deid_profile, deid_policy_version, uid_map_id, uid_remap_applied,
  --  burned_in_phi_check, gateway_version}
  gateway jsonb NOT NULL DEFAULT '{}'::jsonb,
  provenance_redacted_at timestamptz,        -- MOS-STORE-337 step 7's full_purge marker

  -- ------------------------------------------------ Section C: resolution, governance
  capability_id text NOT NULL,
  capability_version text NOT NULL,
  resolution jsonb NOT NULL DEFAULT '{}'::jsonb,
  governance jsonb NOT NULL DEFAULT '{}'::jsonb,
  clinical_use_mode text NOT NULL
    CHECK (clinical_use_mode IN ('research_only','clinical')),

  -- ------------------------------------------------ Section D: execution
  service_id text NOT NULL,
  service_version text NOT NULL,
  service_image_digest text,
  execution_mode text NOT NULL DEFAULT 'native'
    CHECK (execution_mode IN ('native','sealed')),
  models jsonb NOT NULL CHECK (jsonb_typeof(models) = 'array'),
  -- "the field whose absence made the old record non-reproducible" (MOS-SAFE-083 §D).
  preprocessing_specs jsonb NOT NULL CHECK (jsonb_array_length(preprocessing_specs) >= 1),
  preprocessing_selftest jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- MOS-SAFE-083 §D maps the manifest's `score_threshold` onto this ONE provenance
  -- spelling; this is the only place the two names are mapped.
  operating_threshold numeric(6,5),
  threshold_source text,
  inference jsonb NOT NULL DEFAULT '{}'::jsonb,
  accelerator jsonb NOT NULL DEFAULT '{}'::jsonb,
  worker_version text NOT NULL,
  runtime_version text NOT NULL,
  platform_commit text,
  applicability jsonb NOT NULL DEFAULT '{}'::jsonb,
  plausibility jsonb NOT NULL DEFAULT '{}'::jsonb,
  reproducibility_class text NOT NULL DEFAULT 'numeric_tolerance'
    CHECK (reproducibility_class IN ('bitwise','numeric_tolerance','not_reproducible')),

  -- ------------------------------------------------ Section E: evidence
  -- Every member nullable inside the object: chapter 7 ships at 0.2.0, and MOS-SAFE-016
  -- requires the ABSENCE to be surfaced rather than papered over, which is what
  -- `training_population_declared = false` does on every surface of MOS-SAFE-012.
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- ------------------------------------------------ Section F: where every object landed
  outputs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(outputs) = 'array'),
  -- MOS-STORE-279's flat projection of outputs[].sop_instance_uids, so "which result
  -- produced SOPInstanceUID X" is an index lookup and not a jsonb walk.
  generated_object_uids dicom_uid[] NOT NULL DEFAULT '{}',

  -- The record as ONE canonical document: what `record_hash` is computed over, what
  -- GET /api/v1/jobs/{id} surfaces, and what a signed export (MOS-SAFE-089) will carry.
  -- Not a second source of truth -- medos/db/provenance.py projects the columns out of
  -- it on the way in, and the integration test asserts the two agree.
  record jsonb NOT NULL CHECK (jsonb_typeof(record) = 'object'),

  recorded_at timestamptz NOT NULL DEFAULT now(),

  -- MOS-SAFE-082 + MOS-STORE-217: composite, ON DELETE RESTRICT. MOS-STORE-337: "a purge
  -- MUST NOT delete result_provenance ... result_provenance's ON DELETE RESTRICT
  -- therefore blocks step 7 for a study with results -- which is intended."
  CONSTRAINT result_provenance_result_fk FOREIGN KEY (tenant_id, result_id)
    REFERENCES results (tenant_id, id) ON DELETE RESTRICT,
  CONSTRAINT result_provenance_seq_uk UNIQUE (tenant_id, sequence_no),
  CONSTRAINT result_provenance_id_uk  UNIQUE (tenant_id, provenance_id),
  -- MOS-STORE-337 step 7 empties input_instance_uids under a full_purge and keeps the
  -- count, so the equality holds except when the row says it was redacted.
  CONSTRAINT result_provenance_instances_match
    CHECK (input_instance_count = cardinality(input_instance_uids)
           OR provenance_redacted_at IS NOT NULL)
);

CREATE INDEX result_provenance_job_idx ON result_provenance (tenant_id, job_public_id);
CREATE INDEX result_provenance_seq_idx ON result_provenance (tenant_id, sequence_no);
CREATE INDEX result_provenance_object_idx
  ON result_provenance USING gin (generated_object_uids);


CREATE FUNCTION provenance_genesis_hash(p_tenant_id uuid) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT encode(
    sha256(convert_to('medicalos/provenance/genesis/v1' || chr(31) || p_tenant_id::text,
                      'UTF8')),
    'hex');
$$;


-- MOS-SAFE-090: "record_hash = sha256(canonical_json(record without record_hash)),
-- prev_record_hash = the record_hash of sequence_no - 1 for that tenant."
--
-- Computed by trigger for the same reason the audit chain is: the writer cannot skip it,
-- and the exported document is self-verifying because `sequence_no`, `prev_record_hash`
-- and `record_hash` are written back INTO `record` before the hash is taken over the
-- rest of it.
CREATE FUNCTION result_provenance_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  v_prev text;
  v_seq  bigint;
  v_doc  jsonb;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended('prv:' || NEW.tenant_id::text, 0));
  SELECT p.record_hash, p.sequence_no INTO v_prev, v_seq
    FROM result_provenance p
   WHERE p.tenant_id = NEW.tenant_id
   ORDER BY p.sequence_no DESC
   LIMIT 1;
  IF v_seq IS NULL THEN
    v_prev := provenance_genesis_hash(NEW.tenant_id);
    v_seq  := 0;
  END IF;

  NEW.sequence_no      := v_seq + 1;
  NEW.prev_record_hash := v_prev;

  v_doc := NEW.record
           || jsonb_build_object('sequence_no', NEW.sequence_no,
                                 'prev_record_hash', NEW.prev_record_hash);
  NEW.record_hash := encode(
    sha256(convert_to(jcs_canonical(v_doc - 'record_hash'), 'UTF8')), 'hex');
  NEW.record := v_doc || jsonb_build_object('record_hash', NEW.record_hash);
  RETURN NEW;
END;
$$;


CREATE FUNCTION provenance_verify_chain(p_tenant_id uuid, p_from bigint DEFAULT 1)
RETURNS TABLE (sequence_no bigint, reason text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  r record;
  v_expected_prev text;
  v_expected_seq  bigint;
  v_recomputed    text;
BEGIN
  FOR r IN SELECT p.* FROM result_provenance p
            WHERE p.tenant_id = p_tenant_id AND p.sequence_no >= p_from
            ORDER BY p.sequence_no LOOP
    IF v_expected_seq IS NULL THEN
      v_expected_seq  := r.sequence_no;
      v_expected_prev := CASE WHEN r.sequence_no = 1
                              THEN provenance_genesis_hash(p_tenant_id)
                              ELSE r.prev_record_hash END;
    END IF;
    IF r.sequence_no <> v_expected_seq THEN
      sequence_no := r.sequence_no;
      reason := 'gap: expected sequence_no ' || v_expected_seq;
      RETURN NEXT; RETURN;
    END IF;
    IF r.prev_record_hash <> v_expected_prev THEN
      sequence_no := r.sequence_no;
      reason := 'prev_record_hash does not match the previous record''s hash';
      RETURN NEXT; RETURN;
    END IF;
    v_recomputed := encode(
      sha256(convert_to(jcs_canonical(r.record - 'record_hash'), 'UTF8')), 'hex');
    IF r.record_hash <> v_recomputed THEN
      sequence_no := r.sequence_no;
      reason := 'record does not hash to the recorded record_hash';
      RETURN NEXT; RETURN;
    END IF;
    v_expected_prev := r.record_hash;
    v_expected_seq  := r.sequence_no + 1;
  END LOOP;
  RETURN;
END;
$$;


CREATE TRIGGER result_provenance_chain_trg BEFORE INSERT ON result_provenance
  FOR EACH ROW EXECUTE FUNCTION result_provenance_chain();

ALTER TABLE result_provenance OWNER TO medicalos_owner;
ALTER TABLE result_provenance ENABLE ROW LEVEL SECURITY;
ALTER TABLE result_provenance FORCE  ROW LEVEL SECURITY;
CREATE POLICY result_provenance_tenant_isolation ON result_provenance
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

-- MOS-STORE-228 / MOS-SAFE-090: append-only at the GRANT level. No UPDATE, no DELETE.
GRANT SELECT, INSERT ON result_provenance TO medicalos_app;
REVOKE UPDATE, DELETE ON result_provenance FROM medicalos_app;
GRANT SELECT ON result_provenance TO medicalos_readonly;

-- The second layer, which also binds the owner (chapter 12 section 12.11's two triggers).
--
-- ROW-level and not statement-level, unlike `audit_events`, and the difference is
-- load-bearing rather than cosmetic: a row trigger does not fire on TRUNCATE, and
-- `result_provenance` is a CHILD of `results`, so a statement-level TRUNCATE guard here
-- would make `TRUNCATE results CASCADE` -- the test harness's only way to start clean,
-- because job_events already forbids DELETE -- fail for every weeks 1-2 test.
-- `audit_events` has no such parent and therefore takes the stronger guard.
CREATE TRIGGER result_provenance_no_delete BEFORE DELETE ON result_provenance
  FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE TRIGGER result_provenance_immutable BEFORE UPDATE ON result_provenance
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'result_id','tenant_id','provenance_id','job_id','job_public_id','root_job_id',
    'idempotency_key','sequence_no','prev_record_hash','record_hash',
    'study_instance_uid','series_considered','input_series_uids','input_instance_count',
    'input_uid_digest','input_pixel_digest','geometry','capability_id',
    'capability_version','service_id','service_version','service_image_digest','models',
    'preprocessing_specs','accelerator','worker_version','runtime_version',
    'platform_commit','outputs','generated_object_uids','record');

-- `input_instance_uids` is the ONE deliberate absence from that seal list. MOS-STORE-337
-- step 7 sets it to '{}' with `provenance_redacted_at = now()` under a full_purge,
-- "retaining input_instance_count and input_pixel_digest so the quantity and identity of
-- the input stay attestable though the UIDs are gone". Sealing it would make the erasure
-- obligation unimplementable. `medicalos_app` cannot perform that UPDATE at all -- it has
-- no grant -- so the erasure worker runs as the owner, which is the only role that can.


-- =====================================================================================
-- 6. Assertions. Same posture as 0002 section 13: the migration fails rather than
--    leaving a hole for CI to find later.
-- =====================================================================================

-- Chapter 8 section 8.10 item 3, verbatim: "has_table_privilege('medicalos_app',
-- 'audit_events','UPDATE') and the same for DELETE are both false."
DO $$
BEGIN
  IF has_table_privilege('medicalos_app', 'audit_events', 'UPDATE')
     OR has_table_privilege('medicalos_app', 'audit_events', 'DELETE')
     OR has_table_privilege('medicalos_app', 'audit_events', 'TRUNCATE') THEN
    RAISE EXCEPTION 'MOS-STORE-228: medicalos_app can mutate audit_events'
      USING ERRCODE = '42501';
  END IF;
  IF NOT (has_table_privilege('medicalos_app', 'audit_events', 'INSERT')
          AND has_table_privilege('medicalos_app', 'audit_events', 'SELECT')) THEN
    RAISE EXCEPTION 'MOS-SEC-146: medicalos_app cannot append to audit_events'
      USING ERRCODE = '42501';
  END IF;
END $$;

-- The same, per partition: a partition reached directly bypasses the parent's ACL, its
-- triggers and its policies.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname) INTO bad
    FROM pg_class c JOIN pg_inherits i ON i.inhrelid = c.oid
   WHERE i.inhparent = 'audit_events'::regclass
     AND (has_table_privilege('medicalos_app', c.oid, 'UPDATE')
       OR has_table_privilege('medicalos_app', c.oid, 'DELETE')
       OR has_table_privilege('medicalos_app', c.oid, 'TRUNCATE')
       OR NOT c.relrowsecurity OR NOT c.relforcerowsecurity
       OR NOT EXISTS (SELECT 1 FROM pg_trigger t
                       WHERE t.tgrelid = c.oid AND t.tgname = c.relname || '_no_mutate'));
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-SEC-154: audit partition(s) not hardened: %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;

DO $$
BEGIN
  IF has_table_privilege('medicalos_app', 'result_provenance', 'UPDATE')
     OR has_table_privilege('medicalos_app', 'result_provenance', 'DELETE') THEN
    RAISE EXCEPTION 'MOS-SAFE-090: medicalos_app can mutate result_provenance'
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-SEC-077 / MOS-STORE-229, re-run over everything, partitioned parents included.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname) INTO bad
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0
   WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
     AND (c.relrowsecurity = false OR c.relforcerowsecurity = false);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-SEC-077: tenant_id without forced row security on: %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-SEC-157, asserted from the first migration that could plausibly add one.
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n FROM pg_policies
   WHERE policyname ~ '_break_glass$' AND cmd <> 'SELECT';
  IF n <> 0 THEN
    RAISE EXCEPTION 'MOS-SEC-157: a break-glass policy is not FOR SELECT'
      USING ERRCODE = '42501';
  END IF;
END $$;
