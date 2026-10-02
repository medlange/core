-- =====================================================================================
-- 0004_auth.up.sql -- weeks 3-5, item 2 of docs/spec/15-delivery.md section 15.2.4:
--
--   "API-key authentication behind an `Authenticator` interface, so OIDC is a driver
--    rather than a rewrite."
--
-- This migration owns exactly one table, `api_keys`, and it is ADDITIVE. It depends on
-- `0002_tenancy.up.sql` for three things and on nothing else: the `tenants` table, the
-- `current_tenant_id()` predicate function, and the five database roles. It deliberately
-- does NOT depend on `0003_*`, on `audit_events` or on any RBAC table, so the four
-- weeks 3-5 items can land in any order.
--
-- Requirements implemented, by id:
--
--   MOS-SEC-010   the secret is stored as argon2id(secret, salt, m=64MiB, t=3, p=1) and
--                 nothing else. `secret_hash bytea NOT NULL` is the only column that
--                 could hold it and it holds a one-way digest; there is no column a
--                 plaintext could be written to, which is MOS-STORE-232's phrasing:
--                 "the schema provides no column that could hold it".
--   MOS-SEC-011   the column set, verbatim: key_id (unique, indexed), tenant_id,
--                 principal_kind, principal_id, scope text[], expires_at NOT NULL,
--                 last_used_at, created_by, revoked_at, source_ip_allowlist cidr[].
--                 Lookup is by key_id; `secret_hash` carries no index, so it cannot be
--                 used as a lookup key even by accident.
--   MOS-SEC-012   `api_keys_max_lifetime` -- expires_at <= created_at + 365 days -- and
--                 `api_keys_has_lifetime` -- expires_at > created_at. A key with no
--                 expiry is unrepresentable: the column is NOT NULL.
--   MOS-SEC-015   revocation takes effect within 5 s across replicas. The database half
--                 is `revoked_at`; the process half is the <= 5 s credential cache in
--                 medos/security/apikeys.py, which is the only cache of key validity.
--   MOS-SEC-031   scope entries match ^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$,
--                 enforced by `scope_is_wellformed()` in a CHECK constraint.
--   MOS-SEC-033   wildcards are not expressible in an ApiKey.scope. `*` does not match
--                 the MOS-SEC-031 grammar, so the same CHECK is the enforcement.
--   MOS-SEC-072   tenant_id uuid NOT NULL + ENABLE + FORCE row security + a policy bound
--                 to `medicalos.tenant_id`. `api_keys` is an ORDINARY tenant-owned table.
--                 See the note below on why it is not a pre-tenancy one.
--   MOS-SEC-073   `medicalos_app` owns nothing here either; it holds grants only.
--   MOS-SEC-077   asserted again at the end of this file, over the whole schema.
--   MOS-STORE-217 the FK to `tenants` is the tenant FK itself; `api_keys_tenant_id_uk`
--                 is the redundant UNIQUE (tenant_id, id) every parent carries.
--   MOS-STORE-218 tenant_id is immutable (forbid_column_change).
--   MOS-STORE-221 the listing index leads with tenant_id.
--   MOS-STORE-232 argon2id hash of the secret half only; lookup by key_id.
--
-- WHY `api_keys` IS NOT A PRE-TENANCY TABLE, AND HOW THE LOOKUP IS STILL NOT CIRCULAR
-- -----------------------------------------------------------------------------------
-- The obvious objection: authentication must find a row from `key_id` alone, BEFORE it
-- knows the tenant, and a policy `USING (tenant_id = current_tenant_id())` on a table
-- read with no tenant bound raises 42704 (MOS-STORE-224). That is the same circularity
-- MOS-STORE-345 solves for `ae_tenant_map` by making it pre-tenancy -- and MOS-STORE-345
-- CLOSES that class at two tables. `api_keys` cannot join it in any case: MOS-SEC-011
-- requires a `tenant_id` column on it by name.
--
-- The resolution is not a new escape hatch; it is the loop MOS-SEC-078 and MOS-STORE-225
-- already price in ("The loop is the price of the guarantee"). `key_id` is UNIQUE across
-- the whole table, so at most one tenant can own a given key. The authenticator
-- therefore iterates the tenant set this deployment serves -- `serving_tenants()`, the
-- same configuration a worker iterates -- and runs the SAME tenant-scoped lookup under
-- each, through the SAME chokepoint, `tenant_tx()`. The first hit is the answer; a miss
-- across all of them is an unknown key.
--
-- What that buys: `api_keys` needs no second database role, no permissive policy, no
-- SECURITY DEFINER resolver, and no `row_security = off`. There is still exactly zero
-- cross-tenant read path anywhere in this schema, which is the strictest posture and the
-- easiest one to relax later. What it costs: O(tenants) index probes on a cache miss.
-- The <= 5 s credential cache of MOS-SEC-015 bounds that to once per key per 5 s, and
-- when the tenant directory becomes a service the loop is replaced by one call to it
-- without touching this table, the policy, or any caller.
-- =====================================================================================

SET lock_timeout = '3s';


-- =====================================================================================
-- 1. The scope grammar, as a function, because a CHECK constraint may not contain a
--    subquery and `unnest` needs one.
--
--    MOS-SEC-031's regex verbatim: a resource segment and an action segment, or a
--    resource, a sub-resource and an action. Four or more segments is a build error;
--    here it is a constraint violation, which is the same refusal one layer down.
--    MOS-SEC-033 falls out of it: `*` is not in [a-z0-9_], so `study.*`, `*` and
--    `study.read.*` are all unrepresentable rather than merely discouraged.
-- =====================================================================================
CREATE FUNCTION scope_is_wellformed(p_scope text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(
           bool_and(s ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$'),
           true)                                   -- an empty scope is well formed
    FROM unnest(p_scope) AS s
$$;

COMMENT ON FUNCTION scope_is_wellformed(text[]) IS
  'MOS-SEC-031 / MOS-SEC-033: every scope entry is a registered-shape permission '
  'identifier and no entry can be a wildcard. Whether the identifier is a KEY of '
  'contracts/permissions.yaml is MOS-SEC-032''s question and is checked by the '
  'authorization layer, not here -- this constraint owns the grammar only.';


-- =====================================================================================
-- 2. api_keys.  Chapter 12 section 12.6's DDL, trimmed to what this block can honour.
--
-- DELIBERATE DEVIATIONS FROM CHAPTER 12, EACH NARROWER OR EQUAL, NONE WIDER:
--
--   * `uuid_generate_v7()` does not exist in this slice; `gen_random_uuid()` is used,
--     the same choice 0002 made.
--   * `users` and `service_accounts` do not exist yet, so `user_id`, `service_account_id`
--     and `created_by` are plain uuid columns rather than composite foreign keys. The
--     two CHECKs that tie them to `principal_kind` ARE present, because those are the
--     part that stops a row from naming two principals or none. The FKs are named here
--     so they are added with `users`, not discovered missing.
--   * `principal_kind` is CHECKed to ('user','service_account') exactly as chapter 12
--     writes it. `platform_admin` is NOT in the enum: section 8.2.1 gives it
--     `ApiKey` + break-glass grant, and `break_glass_grants` does not exist in this
--     block (0002 section 11 explains why the break-glass policy was not created
--     either). An unbacked platform_admin key would be a cross-tenant credential with
--     nothing gating it.
--   * `label`, `revoked_reason` and `rotated_from` are ADDITIONS, not in chapter 12.
--     They are operational metadata for the issuance/rotation CLI: which key is which
--     in a list, why a key died, and which key replaced which. None of them is a grant
--     and none is reachable from a request. Reported in the component summary.
-- =====================================================================================
CREATE TABLE api_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),

  -- MOS-SEC-072. ON DELETE RESTRICT: chapter 12 spells it this way, and it matters --
  -- deleting a tenant must not silently orphan its credentials into a state where
  -- `key_id` still resolves.
  tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,

  principal_kind text NOT NULL CHECK (principal_kind IN ('user','service_account')),
  user_id uuid,
  service_account_id uuid,

  -- MOS-SEC-011: "key_id (unique, indexed) ... the public half; safe to log; the only
  -- lookup key". 12 characters of Crockford base32 (section 8.2.2's key format), which
  -- is 60 bits -- not a secret, and not required to be one.
  key_id text NOT NULL UNIQUE CHECK (key_id ~ '^[0-9A-HJKMNP-TV-Z]{12}$'),

  -- MOS-SEC-010 / MOS-STORE-232. The encoded argon2id digest (PHC string, ASCII) of the
  -- SECRET half only, stored as bytea because it is a digest and not text anyone should
  -- be tempted to compare with `=`. There is no `secret`, no `secret_enc` and no
  -- `secret_kms_ref` column, and adding one is the defect this table is shaped to
  -- prevent. NOT indexed: MOS-SEC-011 forbids the hash as a lookup key.
  secret_hash bytea NOT NULL CHECK (length(secret_hash) BETWEEN 32 AND 512),

  scope text[] NOT NULL DEFAULT '{}',

  -- MOS-SEC-012. NOT NULL is the "a key without an expiry MUST be rejected at creation"
  -- half; the two CHECKs below are the "<= 365 days" half.
  expires_at timestamptz NOT NULL,
  last_used_at timestamptz,

  -- MOS-SEC-015. NOT a DELETE: the row is the record that the credential existed, and a
  -- revoked key must stay resolvable so that a later audit can say which key was used.
  revoked_at timestamptz,
  revoked_reason text CHECK (revoked_reason IS NULL OR length(revoked_reason) <= 500),

  -- MOS-SEC-011. Until `users` exists this is the nil uuid for a key minted by the
  -- bootstrap operator at the console (`medos.security.cli issue --bootstrap`), which is
  -- the only way the first key of a deployment can come into being. The CLI refuses to
  -- default it; `--bootstrap` has to be typed.
  created_by uuid NOT NULL,

  -- MOS-SEC-011. Empty array means "no source restriction", which is the chapter 12
  -- default. A non-empty array is evaluated in medos/security/apikeys.py, not here:
  -- the peer address is a property of the request, not of the row.
  source_ip_allowlist cidr[] NOT NULL DEFAULT '{}',

  -- Additions (see the deviation note above).
  label text CHECK (label IS NULL OR length(label) <= 200),
  rotated_from uuid,

  created_at timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT api_keys_user_principal
    CHECK ((principal_kind = 'user') = (user_id IS NOT NULL)),
  CONSTRAINT api_keys_service_account_principal
    CHECK ((principal_kind = 'service_account') = (service_account_id IS NOT NULL)),

  -- MOS-SEC-012, chapter 12's rendering verbatim, including its reason: it is written
  -- as a subtraction because `timestamptz + interval` is STABLE, not IMMUTABLE, and
  -- PostgreSQL refuses a non-immutable expression in a CHECK constraint.
  CONSTRAINT api_keys_max_lifetime CHECK (expires_at - created_at <= interval '365 days'),
  -- The other end of the same rule: a key that is born expired is not a key, it is a
  -- confusing 401 in someone's integration test.
  CONSTRAINT api_keys_has_lifetime CHECK (expires_at > created_at),

  CONSTRAINT api_keys_scope_grammar CHECK (scope_is_wellformed(scope)),

  -- MOS-STORE-217: the redundant key every tenant-owned parent carries, so that a later
  -- child table's FK can be composite.
  CONSTRAINT api_keys_tenant_id_uk UNIQUE (tenant_id, id)
);

COMMENT ON TABLE api_keys IS
  'MOS-SEC-010..015, MOS-STORE-232. The credential directory. Lookup is by key_id '
  'inside a tenant-bound transaction; the authenticator iterates serving_tenants() '
  'rather than reading across tenants (see 0004_auth.up.sql header).';

COMMENT ON COLUMN api_keys.secret_hash IS
  'MOS-SEC-010: argon2id(secret, salt, m=64MiB, t=3, p=1), PHC-encoded. The plaintext '
  'is returned exactly once, at creation, and is not recoverable from this row.';


-- -------------------------------------------------------------------------------------
-- Indexes.
--
-- MOS-STORE-221 says every index serving a tenant-filtered query leads with tenant_id,
-- and `api_keys_listing` does. The UNIQUE constraint on `key_id` deliberately does NOT:
-- MOS-SEC-011 requires key_id to be globally unique and indexed, and a composite
-- (tenant_id, key_id) index would not enforce global uniqueness -- two tenants could
-- then mint the same key_id and the credential would stop identifying a row. The
-- authenticator's probe is `WHERE key_id = $1` under a tenant policy, so the planner
-- uses this index and RLS supplies the tenant predicate. That is MOS-SEC-076 exactly:
-- "RLS is the mechanism; an explicit predicate is permitted only as an index hint".
-- -------------------------------------------------------------------------------------
CREATE INDEX api_keys_listing ON api_keys (tenant_id, created_at DESC);

-- Operational: "which keys expire this week", "which keys are still live". Partial, so
-- it stays small once a deployment has churned through a few years of credentials.
CREATE INDEX api_keys_live ON api_keys (tenant_id, expires_at)
  WHERE revoked_at IS NULL;


-- =====================================================================================
-- 3. Immutability.
--
-- MOS-STORE-218 asks for tenant_id; everything else in this list is the same argument.
-- A credential whose identity, owner, secret or scope can be UPDATEd in place is not a
-- credential, it is a mutable grant with an audit trail that lies: the `key_id` a log
-- line recorded last week would resolve to a different principal today.
--
-- The mutable columns are exactly four: last_used_at, revoked_at, revoked_reason and
-- expires_at (and expires_at only downwards -- see below). Rotation is a NEW ROW, which
-- is why `rotated_from` exists.
-- =====================================================================================
CREATE TRIGGER api_keys_immutable BEFORE UPDATE ON api_keys
  FOR EACH ROW EXECUTE FUNCTION forbid_column_change(
    'id','tenant_id','principal_kind','user_id','service_account_id',
    'key_id','secret_hash','scope','created_by','source_ip_allowlist',
    'rotated_from','created_at');

-- MOS-SEC-012 would otherwise be bypassable by UPDATE: the CHECK is evaluated against
-- `created_at`, so pushing `expires_at` out by 364 days a year keeps a key alive forever
-- while satisfying every constraint on the row at the moment it is written. The maximum
-- lifetime is only a maximum if the expiry can never move outwards.
CREATE FUNCTION api_keys_expiry_only_shortens() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.expires_at > OLD.expires_at THEN
    RAISE EXCEPTION
      'api_keys.expires_at may only move earlier (was %, requested %). Extending a '
      'credential is issuing a new one: use medos.security.cli rotate.',
      OLD.expires_at, NEW.expires_at
      USING ERRCODE = '23514';   -- check_violation
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER api_keys_expiry_only_shortens BEFORE UPDATE ON api_keys
  FOR EACH ROW EXECUTE FUNCTION api_keys_expiry_only_shortens();

COMMENT ON FUNCTION api_keys_expiry_only_shortens() IS
  'MOS-SEC-012: the 365-day ceiling is measured from created_at, so it is only a '
  'ceiling if expires_at cannot be extended by UPDATE.';


-- =====================================================================================
-- 4. Ownership, row security and the policy.  MOS-SEC-072, MOS-STORE-223.
--
-- Identical in shape to every table 0002 converted: ENABLE **and** FORCE, policy TO
-- PUBLIC so that the owner hits current_tenant_id() and gets 42704 rather than silently
-- seeing everything.
-- =====================================================================================
ALTER TABLE api_keys OWNER TO medicalos_owner;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys FORCE  ROW LEVEL SECURITY;

CREATE POLICY api_keys_tenant_isolation ON api_keys
  USING (tenant_id = current_tenant_id())
  WITH CHECK (tenant_id = current_tenant_id());


-- =====================================================================================
-- 5. Grants.  MOS-SEC-073, MOS-STORE-230.
--
-- SELECT so the authenticator can resolve a key and the management surface can list
-- metadata; INSERT so a key can be issued; UPDATE so it can be revoked and so
-- last_used_at can be stamped. NO DELETE: MOS-SEC-015 revokes, it does not erase, and
-- chapter 12 MOS-STORE-343 is explicit that erasing a user revokes their api_keys rather
-- than deleting the rows. The audit question "which credential was used" must stay
-- answerable after the credential is dead.
-- =====================================================================================
GRANT SELECT, INSERT, UPDATE ON api_keys TO medicalos_app;
REVOKE DELETE ON api_keys FROM medicalos_app;

-- MOS-STORE-219's readonly role. It can already SELECT everything else; leaving
-- api_keys out would be a false sense of protection, since the row carries no secret --
-- only a one-way digest of one.
GRANT SELECT ON api_keys TO medicalos_readonly;


-- =====================================================================================
-- 6. Assertions. The migration fails rather than leaving a hole for CI to find later.
-- =====================================================================================

-- MOS-SEC-077 / MOS-STORE-229, the same query 0002 ends with, re-run over the schema
-- this migration leaves behind. It is repeated rather than referenced because the
-- requirement is about the state after EVERY migration, not after one of them.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(c.relname, ', ' ORDER BY c.relname) INTO bad
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND a.attnum > 0
   WHERE n.nspname = 'public' AND c.relkind = 'r'
     AND (c.relrowsecurity = false OR c.relforcerowsecurity = false);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-SEC-077: tenant_id without forced row security on: %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-SEC-010 / MOS-STORE-232: "the schema provides no column that could hold it".
-- Asserted structurally so that a later migration adding `api_keys.secret` fails here
-- rather than in a code review that does not happen.
DO $$
DECLARE bad text;
BEGIN
  SELECT string_agg(a.attname, ', ') INTO bad
    FROM pg_attribute a
   WHERE a.attrelid = 'api_keys'::regclass AND a.attnum > 0 AND NOT a.attisdropped
     AND a.attname ~ '(secret|token|password|plaintext)'
     AND a.attname <> 'secret_hash';
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'MOS-SEC-010: api_keys has a column that could hold a plaintext '
                    'credential: %', bad
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-SEC-011: the hash must not be usable as a lookup key. An index on secret_hash
-- would make `WHERE secret_hash = $1` cheap, and cheap is how it becomes the lookup.
DO $$
DECLARE n integer;
BEGIN
  SELECT count(*) INTO n
    FROM pg_index i
    JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
   WHERE i.indrelid = 'api_keys'::regclass AND a.attname = 'secret_hash';
  IF n <> 0 THEN
    RAISE EXCEPTION 'MOS-SEC-011: secret_hash is indexed; lookup MUST be by key_id'
      USING ERRCODE = '42501';
  END IF;
END $$;

-- MOS-SEC-033, proved by construction rather than asserted by comment.
DO $$
BEGIN
  IF scope_is_wellformed(ARRAY['study.read']) IS NOT TRUE
     OR scope_is_wellformed(ARRAY['result.review.read']) IS NOT TRUE
     OR scope_is_wellformed(ARRAY['*']) IS NOT FALSE
     OR scope_is_wellformed(ARRAY['study.*']) IS NOT FALSE
     OR scope_is_wellformed(ARRAY['a.b.c.d']) IS NOT FALSE THEN
    RAISE EXCEPTION 'MOS-SEC-031/033: scope_is_wellformed does not implement the grammar'
      USING ERRCODE = '42501';
  END IF;
END $$;
