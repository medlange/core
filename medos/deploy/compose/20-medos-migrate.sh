#!/bin/sh
# =====================================================================================
# medos/deploy/compose/20-medos-migrate.sh -- runs ONCE, on the first boot of an empty postgres
# data volume, immediately after 10-medos-schema.sql.
#
# The weeks 1-2 stack needed no migration step because `schema.sql` was the only DDL
# (CONTRACT.md section 8). Weeks 3-5 item 1 (docs/spec/15-delivery.md 15.2.4) adds
# `medos/medos/db/migrations/0002_tenancy.up.sql`, and a stack that applied the baseline but not
# the migration would run with `tenant_id` absent -- which is the one state the whole
# exercise exists to abolish. So this script applies every `*.up.sql` in order and writes
# the same `schema_migrations` ledger `medos.db.migrate` writes, so a container-bootstrapped
# database and a `medos.db.conn.apply_schema`-bootstrapped one are indistinguishable
# afterwards.
#
# It also gives `medicalos_app` its password. The migration deliberately creates that role
# with LOGIN and NO password (a migration that hardcodes a credential puts it in git);
# supplying it is the deployment's job, which is this file. MOS-SEC-073: the API and the
# worker connect as `medicalos_app`, which owns nothing and holds no BYPASSRLS -- see the
# MEDOS_DATABASE_URL of medos-api and medos-worker in docker-compose.yml.
#
# `set -e` matters: the postgres entrypoint does NOT abort initdb on a failing script
# unless the script exits non-zero, and a stack that came up with half a tenancy migration
# is worse than one that refused to come up.
# =====================================================================================
set -e

MIGRATIONS_DIR=/medos-migrations
BASELINE=/docker-entrypoint-initdb.d/10-medos-schema.sql

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     text PRIMARY KEY,
  file_digest text NOT NULL CHECK (file_digest ~ '^sha256:[0-9a-f]{64}$'),
  applied_at  timestamptz NOT NULL DEFAULT now(),
  applied_by  text NOT NULL DEFAULT current_user,
  duration_ms integer NOT NULL
);
SQL

record() {
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -c "INSERT INTO schema_migrations (version, file_digest, duration_ms)
        VALUES ('$1', '$2', $3) ON CONFLICT (version) DO NOTHING"
}

record "0001_baseline" "sha256:$(sha256sum "$BASELINE" | cut -d' ' -f1)" 0

for f in "$MIGRATIONS_DIR"/*.up.sql; do
  [ -e "$f" ] || continue
  version=$(basename "$f" .up.sql)
  echo "medos: applying migration $version"
  start=$(date +%s%3N 2>/dev/null || echo 0)
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f "$f"
  end=$(date +%s%3N 2>/dev/null || echo 0)
  record "$version" "sha256:$(sha256sum "$f" | cut -d' ' -f1)" "$((end - start))"
done

# The application credential. MEDOS_APP_PASSWORD is passed through from docker-compose.yml
# and is NEVER echoed -- CONTRACT.md section 11's logging rule has no exception that would
# make printing a credential acceptable. `format(%L)` quotes it server-side so a password
# containing a quote is a password and not a syntax error.
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v app_password="${MEDOS_APP_PASSWORD:?MEDOS_APP_PASSWORD must be set}" <<'SQL'
SELECT format('ALTER ROLE medicalos_app LOGIN PASSWORD %L', :'app_password') \gexec
SQL

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "GRANT CONNECT ON DATABASE \"$POSTGRES_DB\" TO medicalos_app"

echo "medos: migrations applied; medicalos_app provisioned"
