-- =====================================================================================
-- 0014_outbox.up.sql -- the transactional outbox that `JobQueue` driver 2 needs.
--
-- docs/spec/05-execution.md section 5.7, `MOS-EXEC-040`, is the whole reason this file
-- exists and it says it in one sentence:
--
--     "Driver 2 introduces a dual write -- the job row goes to Postgres, the dispatch
--      message goes to the broker -- and therefore MUST use a transactional outbox. The
--      outbox exists for driver 2 only; introducing it in 0.1 would be cost with no
--      benefit."
--
-- Driver 1 (`medos/db/queue.py`) commits the `jobs` row and the `job_queue` row in ONE
-- transaction (`MOS-EXEC-034`), so it has no dual write and needs nothing here. Driver 2
-- replaces that single commit with `jobs` + `job_outbox` in one transaction, and a relay
-- that moves the row to the broker afterwards. `MOS-EXEC-002`: Postgres stays the sole
-- source of truth and the bus is never read to reconstruct state.
--
-- SCOPE -- one table, two functions, two triggers, four indexes. ADDITIVE: it ALTERs no
-- existing table, and nothing in 0001-0013 changes behaviour because of it.
--
-- Column names and types are chapter 12 section 12.10's listing for `job_outbox`
-- (`MOS-STORE-201`: the migration is generated from chapter 12, never from chapter 5's
-- semantic block), reconciled with chapter 5 section 5.7's DDL where 12.10 is terser:
--
--     ch. 12 section 12.10          ch. 5 section 5.7        this file
--     ---------------------------   ----------------------   -------------------------
--     id bigint identity PK         id bigserial PK          bigint GENERATED ALWAYS AS
--                                                            IDENTITY -- 12.10's form,
--                                                            and `job_events.id`'s
--     (not listed)                  tenant_id uuid NOT NULL  tenant_id, per MOS-EXEC-085
--     (not listed)                  job_id uuid NOT NULL     job_id FK, ON DELETE CASCADE
--     topic                         topic text               topic, CHECKed -- section 3
--     partition_key                 partition_key text       partition_key, CHECKed
--     envelope jsonb                envelope jsonb           envelope
--     (not listed)                  created_at timestamptz   created_at
--     published_at                  published_at             published_at
--     attempts                      attempts smallint        attempts
--     last_error                    last_error text          last_error
--
-- THREE THINGS THIS MIGRATION MAKES STRUCTURAL RATHER THAN REVIEWABLE
-- ------------------------------------------------------------------
-- 1. THE TOPIC SET IS CLOSED BY A CHECK CONSTRAINT (`MOS-EXEC-003`, `MOS-EXEC-046`).
--    Section 5.8's table is a closed set and adding to it "MUST require a spec
--    amendment"; section 5.8's `MOS-EXEC-046` forbids modality and nosology topics
--    outright, with four independently sufficient reasons. Both are expressed here as
--    `job_outbox_topic_ck` rather than as a code review, because the failure mode --
--    "promoting a row to a topic means a registry edit becomes a broker migration" --
--    is exactly the kind that arrives as a one-line convenience in someone else's
--    component. A row naming `medicalos.ct.requested` cannot be inserted.
--
--    Chapter 5's acceptance criterion 20 permits a forbidden WORD inside a registered
--    `service_id` in `medicalos.svc.<service_id>.work` -- a service really may be called
--    `pulmo.effusion` -- so the CHECK admits that one shape and no other.
--
-- 2. AN UNPUBLISHED ROW CANNOT BE DELETED (`MOS-EXEC-041` clause 5, verbatim: "Never
--    delete an unpublished row"). `job_outbox_guard()` is a BEFORE DELETE trigger, which
--    binds `medicalos_owner` and a superuser psql session as well as `medicalos_app` --
--    the same two-layer construction 0009 uses for `validation_reports` and 0012 for
--    `artifacts`. A GRANT alone would leave the owner able to erase a dispatch that no
--    consumer has ever seen, and the job would be QUEUED forever with nothing to deliver
--    it.
--
-- 3. `published_at` IS MONOTONIC AND THE ENVELOPE IS IMMUTABLE. The same trigger refuses
--    an UPDATE that changes `tenant_id`, `job_id`, `topic`, `partition_key`, `envelope`
--    or `created_at`, and refuses to move `published_at` back to NULL. `MOS-EXEC-041`
--    clause 4 makes `published_at` the record of a broker acknowledgement; a row that can
--    be un-published is a row that can be published twice with no consumer-visible
--    difference between at-least-once and unbounded.
--
-- WHAT THIS MIGRATION DELIBERATELY DOES NOT CREATE
-- ------------------------------------------------
-- `job_dead_letter` (`MOS-EXEC-066`, chapter 12 section 12.10.1). The DLQ is a
-- driver-LEVEL concept with one contract in both drivers and it is absent from driver 1
-- as well (see `medos/db/queue.py`: "`job_dead_letter` is not one of CONTRACT.md section
-- 8's eight tables"). Creating it here would give driver 2 a dead-letter destination that
-- driver 1 does not have, which is the precise asymmetry `queue-driver-parity` exists to
-- forbid. It is reported as an open gap rather than half-built.
--
-- `retention_policies` (chapter 12 section 12.16.1 wants the row `job_outbox (published)
-- | Postgres | 7 days after published_at | delete`). That table does not exist in this
-- deployment, so `MOS-STORE-274`'s "retained until published plus 7 days" is implemented
-- by `medos.bus.outbox.OutboxRelay.sweep_published()` and indexed here by
-- `job_outbox_published_idx`. Reported.
--
-- Spec: MOS-EXEC-003, MOS-EXEC-039, MOS-EXEC-040, MOS-EXEC-041, MOS-EXEC-044,
--       MOS-EXEC-046, MOS-EXEC-085, MOS-STORE-201, MOS-STORE-208, MOS-STORE-210,
--       MOS-STORE-216, MOS-STORE-221, MOS-STORE-223, MOS-STORE-225, MOS-STORE-229,
--       MOS-STORE-272, MOS-STORE-274, MOS-SEC-073, MOS-SEC-077.
-- =====================================================================================


-- =====================================================================================
-- 1. The table.
-- =====================================================================================
CREATE TABLE job_outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

  -- MOS-EXEC-085: "Every execution-plane table (`jobs`, `job_steps`, `job_events`,
  -- `job_queue`, `job_dead_letter`, `job_outbox`) MUST carry `tenant_id NOT NULL` and
  -- MUST have row-level security bound to `current_setting('medicalos.tenant_id')`."
  -- The DEFAULT mirrors `job_queue.tenant_id`, which 0002 gave the same one: the relay
  -- and the enqueuing transaction both already hold a tenant binding, so spelling the
  -- value at every INSERT would be a second place for it to be wrong.
  tenant_id uuid NOT NULL DEFAULT current_tenant_id()
    REFERENCES tenants(id) ON DELETE RESTRICT,

  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,

  -- Section 5.8's closed set. See section 3 below for the CHECK.
  topic text NOT NULL,

  -- MOS-EXEC-044: "The partition key MUST be `<tenant_id>:<study_instance_uid>` for every
  -- job-scoped topic." Chapter 5's acceptance criterion 22 gives the regex and it is the
  -- one applied here, so a key that co-locates the wrong things cannot be stored.
  partition_key text NOT NULL
    CHECK (partition_key ~ '^[0-9a-f-]{36}:[0-9.]+$'),

  -- MOS-EXEC-075: the event envelope. MOS-STORE-272 / MOS-EXEC-079: no PHI -- DICOM UIDs
  -- are permitted and nothing else patient-linked is. `medos/bus/envelope.py` validates
  -- the shape before the row is written; the CHECK below is the floor under that.
  envelope jsonb NOT NULL
    CHECK (jsonb_typeof(envelope) = 'object'
           AND envelope ? 'event_id'
           AND envelope ? 'event_type'),

  created_at timestamptz NOT NULL DEFAULT now(),

  -- MOS-EXEC-041 clause 4: set ONLY after the broker acknowledges. That is what makes
  -- the guarantee at-least-once rather than at-most-once, and it is why the relay may
  -- not batch the UPDATE ahead of the produce.
  published_at timestamptz,

  last_error text,

  attempts smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),

  -- The envelope's delivery identity, lifted out so it can be indexed and so
  -- "unique per event" is a constraint rather than a claim. MOS-EXEC-041's closing
  -- sentence -- "Consumers MUST deduplicate on `envelope.event_id`, which is unique per
  -- event (`job_events.event_id`, `UNIQUE`)" -- is unenforceable if the producer side can
  -- emit the same id twice, and at-least-once delivery makes consumer-side dedup the only
  -- thing standing between a relay restart and a duplicated dispatch.
  event_id uuid GENERATED ALWAYS AS ((envelope ->> 'event_id')::uuid) STORED
);

COMMENT ON TABLE job_outbox IS
  'Transactional outbox for JobQueue driver 2 (MOS-EXEC-040). Written in the same '
  'transaction as the jobs row; drained to the broker by medos.bus.outbox.OutboxRelay. '
  'Postgres remains the source of truth (MOS-EXEC-002); this table is a transport queue.';


-- =====================================================================================
-- 2. Indexes.
--
-- `job_outbox_pending_idx` is chapter 5 section 5.7's, verbatim:
--     CREATE INDEX job_outbox_pending_idx ON job_outbox (id) WHERE published_at IS NULL;
-- It serves the relay's `ORDER BY id ... FOR UPDATE SKIP LOCKED` (clause 2) and the
-- `min(created_at) WHERE published_at IS NULL` lag gauge of clause 6.
--
-- It does NOT lead with `tenant_id`, and `MOS-STORE-221` requires that of "every index on
-- a tenant-owned table intended to serve a tenant-filtered query". The relay's scan IS
-- tenant-filtered, because `MOS-STORE-225` makes it iterate tenants rather than read
-- across them. So the tenant-leading index is added ALONGSIDE chapter 5's rather than
-- instead of it: chapter 5's spelling is preserved because it is normative, and
-- MOS-STORE-221 is satisfied by `job_outbox_pending_tenant_idx`, which is the one the
-- planner will actually choose under RLS.
-- =====================================================================================
CREATE INDEX job_outbox_pending_idx
  ON job_outbox (id) WHERE published_at IS NULL;

CREATE INDEX job_outbox_pending_tenant_idx
  ON job_outbox (tenant_id, id) WHERE published_at IS NULL;

-- MOS-STORE-274: "`job_outbox` rows are retained until published plus 7 days." The sweep
-- is the ordinary tenant-iterating one of MOS-STORE-225; this is the index it reads.
CREATE INDEX job_outbox_published_idx
  ON job_outbox (tenant_id, published_at) WHERE published_at IS NOT NULL;

-- Producer-side uniqueness of the delivery identity. Not tenant-leading and deliberately
-- not tenant-scoped, for the same reason `jobs.public_id`'s unique index is not
-- (`MOS-STORE-357`): a dedup identity that is only unique within a tenant cannot be
-- deduplicated by a consumer that has not yet parsed the tenant out of the message.
CREATE UNIQUE INDEX job_outbox_event_id_uk ON job_outbox (event_id);

-- The consumer-side "have I already dispatched this job" question, and the driver-2
-- `depth()` read.
CREATE INDEX job_outbox_job_idx ON job_outbox (tenant_id, job_id);


-- =====================================================================================
-- 3. The closed topic set. MOS-EXEC-003 and MOS-EXEC-046, as a constraint.
--
-- Section 5.8's table, in full, is the first six alternatives; the seventh is the
-- `<topic>.dlq` sibling every one of them may have. `medicalos.jobs.running` is absent
-- and `MOS-EXEC-045` says so explicitly -- "There is no `medicalos.jobs.running` topic" --
-- so it is unrepresentable here rather than merely unused.
--
-- The per-service inbox is the one alternative carrying a free segment, and it is the one
-- place a forbidden word may legitimately appear: chapter 5's acceptance criterion 20
-- reads "No topic name matches `(?i)\b(ct|mr|xr|us|pet|chest|lung|emphysema|nodule|
-- effusion|embolism)\b` except as a substring of a registered `service_id` inside
-- `medicalos.svc.<service_id>.work`". Because the service segment is fenced between the
-- literals `medicalos.svc.` and `.work`, that exception is structural: no OTHER topic
-- name in the set has anywhere to put such a word. The regex below therefore needs no
-- second, negative clause -- the closed set IS the negative clause. `medos/bus/topics.py`
-- states the same rule in Python and a test compares the two.
-- =====================================================================================
ALTER TABLE job_outbox ADD CONSTRAINT job_outbox_topic_ck CHECK (
  topic ~ ('^(' ||
           'medicalos\.jobs\.(requested|completed|failed)' || '|' ||
           'medicalos\.events\.(system|audit)'             || '|' ||
           'medicalos\.svc\.[a-z0-9]([a-z0-9._-]{0,126}[a-z0-9])?\.work' ||
           ')(\.dlq)?$')
);


-- =====================================================================================
-- 4. The guard. MOS-EXEC-041 clauses 4 and 5.
--
-- Two layers, as 0009 does for `validation_reports` and 0012 for `artifacts`: a
-- column-level GRANT bounding `medicalos_app` (section 6), and this trigger, which also
-- binds `medicalos_owner` and any operator connected with owner rights. The GRANT is the
-- policy; the trigger is the guarantee.
-- =====================================================================================
CREATE OR REPLACE FUNCTION job_outbox_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    -- MOS-EXEC-041 clause 5, verbatim: "Never delete an unpublished row."
    IF OLD.published_at IS NULL THEN
      RAISE EXCEPTION
        'MOS-EXEC-041: job_outbox row % is unpublished and MUST NOT be deleted', OLD.id
        USING ERRCODE = 'MOS05',
              HINT = 'the relay publishes it; the 7-day sweep (MOS-STORE-274) removes it '
                     'only once published_at is set';
    END IF;
    RETURN OLD;
  END IF;

  -- MOS-EXEC-041 clause 4: published_at records a broker acknowledgement. Once set it is
  -- a fact about the past.
  IF OLD.published_at IS NOT NULL AND NEW.published_at IS DISTINCT FROM OLD.published_at THEN
    RAISE EXCEPTION
      'MOS-EXEC-041: job_outbox.published_at is write-once (row %)', OLD.id
      USING ERRCODE = 'MOS05',
            HINT = 'un-publishing a row makes a second produce indistinguishable from '
                   'at-least-once delivery';
  END IF;

  IF NEW.tenant_id     IS DISTINCT FROM OLD.tenant_id
     OR NEW.job_id     IS DISTINCT FROM OLD.job_id
     OR NEW.topic      IS DISTINCT FROM OLD.topic
     OR NEW.partition_key IS DISTINCT FROM OLD.partition_key
     OR NEW.envelope   IS DISTINCT FROM OLD.envelope
     OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
    RAISE EXCEPTION
      'MOS-EXEC-040: the dispatch recorded in job_outbox row % is immutable', OLD.id
      USING ERRCODE = 'MOS05',
            HINT = 'only published_at, attempts and last_error may be updated, and only '
                   'by the relay';
  END IF;

  RETURN NEW;
END;
$$;

ALTER FUNCTION job_outbox_guard() OWNER TO medicalos_owner;

CREATE TRIGGER job_outbox_guard_trg
BEFORE UPDATE OR DELETE ON job_outbox
FOR EACH ROW EXECUTE FUNCTION job_outbox_guard();


-- =====================================================================================
-- 5. Row-level security. MOS-EXEC-085, MOS-SEC-077, MOS-STORE-223, MOS-STORE-229.
--
-- FORCE is not optional: without it `medicalos_owner` -- and therefore the migration role
-- through its membership, and any operator with owner rights -- bypasses the policy
-- silently (MOS-STORE-223).
--
-- MOS-STORE-225: no escape hatch. The relay does NOT get a permissive policy and does NOT
-- get a BYPASSRLS role; it iterates tenants, binding `medicalos.tenant_id` per tenant,
-- which is what `medos.db.tenancy.serving_tenants()` already exists for and what its
-- docstring already cites ("Retention sweeps, the projection reconciler, the outbox relay
-- ... MUST iterate tenants ... The loop is the price of the guarantee").
--
-- REPORTED, NOT RESOLVED: `MOS-EXEC-085` says the outbox relay "run[s] as a separate
-- database role that bypasses RLS", which contradicts chapter 12 section 12.5's
-- tenant-iteration rule above, MOS-STORE-222's "five database roles exist and no others",
-- and chapter 12's acceptance criterion 7 ("`BYPASSRLS` appears in the migrations only in
-- the `medicalos_backup` role definition"). Three of those four are satisfiable together
-- and the fourth is not, so the tenant-iterating form is implemented and the conflict is
-- in this component's report.
-- =====================================================================================
ALTER TABLE job_outbox OWNER TO medicalos_owner;
ALTER TABLE job_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_outbox FORCE  ROW LEVEL SECURITY;

CREATE POLICY job_outbox_tenant_isolation ON job_outbox
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());


-- =====================================================================================
-- 6. Grants. MOS-STORE-228, MOS-SEC-073.
--
-- `medicalos_app` owns nothing and is NOBYPASSRLS, so these grants are the whole of what
-- the application role may do. The UPDATE grant is column-level and names exactly the
-- three columns MOS-EXEC-041 lets the relay write. Written as a positive grant rather
-- than a REVOKE of the others: same effect, and it fails safe when a column is added
-- later -- a new column is not grantable by omission, where a REVOKE list would silently
-- leave it writable.
--
-- DELETE is granted because the 7-day sweep of MOS-STORE-274 runs as `medicalos_app`;
-- the trigger of section 4 is what keeps that grant from reaching an unpublished row.
-- =====================================================================================
GRANT SELECT, INSERT, DELETE ON job_outbox TO medicalos_app;
GRANT UPDATE (published_at, attempts, last_error) ON job_outbox TO medicalos_app;
GRANT SELECT ON job_outbox TO medicalos_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO medicalos_app;


-- =====================================================================================
-- 7. Assert what this migration claims, in the migration, against the database it just
--    built. Every one of these has been a real defect somewhere in this repository.
-- =====================================================================================
DO $$
DECLARE
  n   integer;
  bad text;
  ok  boolean;
BEGIN
  -- MOS-EXEC-085 + MOS-STORE-223.
  SELECT count(*) INTO n FROM pg_class
   WHERE relname = 'job_outbox' AND relrowsecurity AND relforcerowsecurity;
  IF n <> 1 THEN
    RAISE EXCEPTION 'job_outbox lacks ENABLE + FORCE ROW LEVEL SECURITY (MOS-EXEC-085)';
  END IF;

  -- MOS-STORE-225: exactly one policy, and it names current_tenant_id() and nothing else.
  SELECT count(*) INTO n FROM pg_policies
   WHERE tablename = 'job_outbox';
  IF n <> 1 THEN
    RAISE EXCEPTION 'expected exactly 1 policy on job_outbox, found % (MOS-STORE-225)', n;
  END IF;
  SELECT bool_and(qual LIKE '%current_tenant_id()%' AND qual NOT LIKE '%break_glass%')
    INTO ok FROM pg_policies WHERE tablename = 'job_outbox';
  IF NOT ok THEN
    RAISE EXCEPTION 'job_outbox policy is not a plain tenant predicate (MOS-STORE-225)';
  END IF;

  -- MOS-STORE-228: medicalos_app may UPDATE exactly three columns.
  SELECT string_agg(column_name, ', ' ORDER BY column_name) INTO bad
    FROM information_schema.column_privileges
   WHERE table_schema = 'public' AND table_name = 'job_outbox'
     AND privilege_type = 'UPDATE' AND grantee = 'medicalos_app'
     AND column_name NOT IN ('published_at', 'attempts', 'last_error');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-EXEC-040: medicalos_app can UPDATE % on job_outbox', bad;
  END IF;

  -- MOS-EXEC-003 / MOS-EXEC-046, proven rather than declared: the CHECK refuses every
  -- forbidden shape chapter 5 names by example, and admits the ones it lists.
  --
  -- The probe is a TEMP table cloned with `INCLUDING CONSTRAINTS`, so the real CHECK
  -- expression is exercised with no foreign key, no row security and no tenant binding in
  -- the way. Asserting against `job_outbox` itself would have been testing whichever of
  -- the FK, the RLS policy and the CHECK happened to fire first.
  -- CONSTRAINTS for the CHECK under test; DEFAULTS and IDENTITY so the NOT NULLs the
  -- clone also copies can be satisfied. Deliberately NOT `INCLUDING GENERATED` or
  -- `INCLUDING INDEXES`: the generated `event_id` would be the same constant on every
  -- probe row and its unique index would fail the second insert for a reason that has
  -- nothing to do with topics.
  CREATE TEMP TABLE job_outbox_topic_probe
    (LIKE job_outbox INCLUDING CONSTRAINTS INCLUDING DEFAULTS INCLUDING IDENTITY)
    ON COMMIT DROP;

  FOR bad IN SELECT unnest(ARRAY[
      'medicalos.ct.requested',          -- MOS-EXEC-046, modality
      'medicalos.mr.requested',          -- MOS-EXEC-046, modality
      'medicalos.emphysema.detected',    -- MOS-EXEC-046, nosology
      'medicalos.jobs.running',          -- MOS-EXEC-045, no .running topic
      'medicalos.chest.work',            -- MOS-EXEC-046, body part
      'medicalos.jobs.completed.extra',  -- not in the closed set
      'medicalos.jobs.completed.dlq.dlq' -- MOS-EXEC-003, no DLQ of a DLQ
  ]) LOOP
    BEGIN
      INSERT INTO job_outbox_topic_probe
             (tenant_id, job_id, topic, partition_key, envelope)
      VALUES ('00000000-0000-0000-0000-000000000000'::uuid,
              '00000000-0000-0000-0000-000000000000'::uuid,
              bad, '00000000-0000-0000-0000-000000000000:1.2.3',
              '{"event_id":"00000000-0000-0000-0000-000000000000",
                "event_type":"job.dispatch"}'::jsonb);
      RAISE EXCEPTION 'MOS-EXEC-046: topic % was accepted by job_outbox_topic_ck', bad;
    EXCEPTION
      WHEN check_violation THEN NULL;   -- the constraint did its job
    END;
  END LOOP;

  -- ... and admits section 5.8's closed set, including a service_id carrying a word the
  -- forbidden list contains, which chapter 5's acceptance criterion 20 explicitly permits
  -- inside `medicalos.svc.<service_id>.work` and nowhere else.
  FOR bad IN SELECT unnest(ARRAY[
      'medicalos.jobs.requested', 'medicalos.jobs.completed', 'medicalos.jobs.failed',
      'medicalos.events.system',  'medicalos.events.audit',
      'medicalos.svc.medos.slice.work',
      'medicalos.svc.pulmo.effusion.work',      -- criterion 20's exception
      'medicalos.jobs.failed.dlq'
  ]) LOOP
    INSERT INTO job_outbox_topic_probe
           (tenant_id, job_id, topic, partition_key, envelope)
    VALUES ('00000000-0000-0000-0000-000000000000'::uuid,
            '00000000-0000-0000-0000-000000000000'::uuid,
            bad, '00000000-0000-0000-0000-000000000000:1.2.3',
            '{"event_id":"00000000-0000-0000-0000-000000000000",
              "event_type":"job.dispatch"}'::jsonb);
  END LOOP;
  DROP TABLE job_outbox_topic_probe;

  -- The guard trigger exists. Its behaviour is asserted by
  -- tests/integration/test_queue_parity.py against a real transaction.
  SELECT count(*) INTO n FROM pg_trigger
   WHERE NOT tgisinternal AND tgname = 'job_outbox_guard_trg';
  IF n <> 1 THEN
    RAISE EXCEPTION 'job_outbox_guard_trg is missing (MOS-EXEC-041 clauses 4 and 5)';
  END IF;

  -- MOS-STORE-221's tenant-leading partial index, and chapter 5's verbatim one.
  SELECT count(*) INTO n FROM pg_indexes
   WHERE tablename = 'job_outbox'
     AND indexname IN ('job_outbox_pending_idx', 'job_outbox_pending_tenant_idx',
                       'job_outbox_published_idx', 'job_outbox_event_id_uk',
                       'job_outbox_job_idx');
  IF n <> 5 THEN
    RAISE EXCEPTION 'expected 5 job_outbox indexes, found %', n;
  END IF;

  -- MOS-SEC-073, re-asserted because this migration hands out a DELETE grant: no
  -- application role gained BYPASSRLS, and no sixth relay role was invented.
  SELECT string_agg(rolname, ', ') INTO bad FROM pg_roles
   WHERE rolbypassrls AND rolname LIKE 'medicalos%'
     AND rolname NOT IN ('medicalos_backup', 'medicalos_study_oracle');
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-SEC-073: unexpected BYPASSRLS role(s): %', bad;
  END IF;
END $$;
