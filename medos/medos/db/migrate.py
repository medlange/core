# SPDX-License-Identifier: Apache-2.0
"""Ordered, recorded, digest-checked migrations. Chapter 12 section 12.15.

Weeks 1-2 had no migration runner and said so: CONTRACT.md section 8 made
`medos/medos/db/schema.sql` "the ONLY DDL", and `apply_schema()` bootstrapped an empty
database in one shot. That was the honest shape while the schema changed by rewriting the
file. It stops being honest the moment a deployment exists whose rows must survive a
schema change, which is what `0002_tenancy.up.sql` is.

So: `schema.sql` becomes migration `0001_baseline`, recorded rather than reapplied, and
every later change is a numbered pair of files in `medos/medos/db/migrations/`.

    MOS-STORE-214   every migration set begins at the bootstrap DDL
    MOS-STORE-229   RLS is enabled in the same migration that creates the table
    MOS-SEC-073     migrations run as `medicalos_migrator`, which holds no BYPASSRLS
    MOS-STORE-222   `medicalos_migrator` is a member of `medicalos_owner` for DDL rights

What this runner deliberately does NOT do
-----------------------------------------
No down-migrations are ever run by it. The `.down.sql` files exist so the forward
migration can be tested against a real database (`test_tenancy.py` uses one), not so a
production deployment can reverse one -- dropping `tenant_id` merges two tenants' rows
into one undifferentiated set and no later step can separate them again. Recovery from a
bad forward migration is restore-from-backup.

No advisory-lock-free concurrency either: two runners against one database take a session
advisory lock so the second waits rather than racing on `CREATE ROLE`.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

__all__ = [
    "MIGRATIONS_DIR",
    "BASELINE_VERSION",
    "Migration",
    "applied_versions",
    "apply_migrations",
    "discover",
    "PLANE_MIGRATIONS",
    "plane_of",
    "set_role_password",
]

MIGRATIONS_DIR = Path(__file__).with_name("migrations")

# `schema.sql` is migration 0001. It is recorded by `apply_migrations()` when the runner
# finds the schema already present, so a database bootstrapped by `apply_schema()` and one
# bootstrapped by a future `0001_baseline.up.sql` converge on the same ledger.
BASELINE_VERSION = "0001_baseline"

# =====================================================================================
# WHICH PRODUCT OWNS WHICH MIGRATION
#
# MedicalOS ships two deployables, and the schema partitions with them. Ownership here
# is not a taste: it is derived from who WRITES each table. Of the 45 tables the set
# creates, 29 are written only by Core, 12 only by `medos/medos/training/`, and 4 by nobody
# yet -- and there is NOT ONE foreign key from a Core table into a Train table. The
# dependency runs one way, so Core's subset stands alone and Train's layers on top.
#
# PROVEN, NOT ASSERTED. `tests/integration/test_migration_planes.py` applies the CORE
# set to an empty database and checks that it yields 58 tables with `jobs`, `results`,
# `deployments`, `evaluation_runs` and `dataset_versions` present and `training_runs`,
# `harvest_batches` and `seal_runs` absent -- then applies Train's three on top.
#
# THE NUMBERING IS NOT RESEQUENCED, AND THAT IS DELIBERATE. `version` is the whole
# `NNNN_name` string and the ledger stores it beside a digest of the file's bytes, so
# renaming a migration makes every deployed database report the old name missing and
# the new one unapplied -- `MOS-STORE-214`'s ledger turned into noise on every existing
# installation to make a number look tidy. Gaps are free: `discover()` sorts lexically
# and does not care that Core skips 0011 between 0010 and 0012. A partition that costs
# nothing beats a renumbering that costs every deployment.
#
# `0001_baseline` (`schema.sql`) is in NEITHER set because it is in both: it is the
# bootstrap every database begins from.
# =====================================================================================

#: plane -> the versions that plane owns. Every `NNNN_name.up.sql` is in exactly one,
#: and `tests/unit/test_migration_planes.py` fails when a new migration is in neither.
PLANE_MIGRATIONS: dict[str, frozenset[str]] = {
    "core": frozenset(
        {
            "0002_tenancy",
            "0003_audit_provenance",
            "0004_auth",
            "0005_gateway",
            "0006_evidence",
            "0007_evaluation",
            "0008_acceptance_gate",
            "0009_validation_reports",
            "0010_safety",
            "0012_artifacts",
            "0014_outbox",
        }
    ),
    # The model-preparation product: curation, the training runs themselves, and the
    # seal-run ledger. Each references Core tables (`tenants`, `artifacts`, `datasets`)
    # and none is referenced BY one.
    "train": frozenset({"0011_curation", "0013_training", "0015_seal_runs"}),
}


def plane_of(version: str) -> str:
    """Which product owns a migration. Raises on one nobody has classified."""
    for plane, versions in PLANE_MIGRATIONS.items():
        if version in versions:
            return plane
    raise KeyError(
        f"migration {version!r} belongs to no plane. Add it to PLANE_MIGRATIONS: "
        f"whether the model-preparation product owns a table is a decision, and a "
        f"migration that defaults into Core silently widens what Core ships."
    )

# Advisory lock key. Arbitrary but fixed: two processes migrating one database must
# serialise, and `CREATE ROLE` in 0002 is not idempotent under a race.
_LOCK_KEY = 0x4D45444F  # 'MEDO'

_NAME_RE = re.compile(r"^(?P<version>\d{4}_[a-z0-9_]+)\.up\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def digest(self) -> str:
        """`sha256:<hex>` -- the `sha256_digest` domain of MOS-STORE-211.

        Recorded so that editing an applied migration is detectable. A migration file is
        immutable once it has run anywhere; the runner refuses a changed one rather than
        silently accepting a schema that no longer matches its ledger.
        """
        return "sha256:" + hashlib.sha256(self.path.read_bytes()).hexdigest()


def discover(
    directory: Path | None = None, *, planes: Sequence[str] | None = None
) -> list[Migration]:
    """Every `NNNN_name.up.sql`, in lexical version order.

    `planes` restricts the set to the products named -- `("core",)` for the
    PACS-and-models deployable, `("core", "train")` for the model-preparation one, which
    needs Core's tables underneath its own. Omitted, every migration is returned, which
    is what a single-database deployment of both products wants and what every existing
    caller gets.

    Order is lexical and therefore global: filtering removes files, it never reorders
    them, so Core applying 0010 then 0012 lands in exactly the state it would have had
    if 0011 were applied and then dropped.
    """
    d = directory or MIGRATIONS_DIR
    allowed: set[str] | None = None
    if planes is not None:
        allowed = set()
        for plane in planes:
            if plane not in PLANE_MIGRATIONS:
                raise ValueError(f"unknown plane {plane!r}")
            allowed |= PLANE_MIGRATIONS[plane]
    out: list[Migration] = []
    for p in sorted(d.glob("*.up.sql")):
        m = _NAME_RE.match(p.name)
        if not m:  # pragma: no cover - a typo in a filename should be loud
            raise ValueError(f"migration filename is not NNNN_name.up.sql: {p.name}")
        version = m.group("version")
        if allowed is not None and version not in allowed:
            continue
        out.append(Migration(version=version, path=p))
    return out


def _ensure_ledger(conn: psycopg.Connection[Any]) -> None:
    """`schema_migrations` -- MOS-STORE-214's last table, created on first use.

    Platform-global (MOS-STORE-219): no `tenant_id`, no row security, written by
    `medicalos_migrator` and readable by everyone. Section 13 of 0002 asserts that a
    table with no `tenant_id` column is out of MOS-SEC-077's scope, which is why this one
    needs no policy.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version     text PRIMARY KEY,
          file_digest text NOT NULL CHECK (file_digest ~ '^sha256:[0-9a-f]{64}$'),
          applied_at  timestamptz NOT NULL DEFAULT now(),
          applied_by  text NOT NULL DEFAULT current_user,
          duration_ms integer NOT NULL
        )
        """
    )


def applied_versions(conn: psycopg.Connection[Any]) -> dict[str, str]:
    """`{version: file_digest}` for everything already applied."""
    _ensure_ledger(conn)
    rows = conn.execute("SELECT version, file_digest FROM schema_migrations").fetchall()
    out: dict[str, str] = {}
    for r in rows:
        if isinstance(r, dict):
            out[r["version"]] = r["file_digest"]
        else:  # pragma: no cover - dict_row is the configured factory
            out[r[0]] = r[1]
    return out


def _record(conn: psycopg.Connection[Any], version: str, digest: str, ms: int) -> None:
    conn.execute(
        "INSERT INTO schema_migrations (version, file_digest, duration_ms) "
        "VALUES (%s, %s, %s) ON CONFLICT (version) DO NOTHING",
        (version, digest, ms),
    )


def apply_migrations(
    conn: psycopg.Connection[Any],
    *,
    directory: Path | None = None,
    baseline_digest: str | None = None,
    planes: Sequence[str] | None = None,
) -> list[str]:
    """Apply every unapplied migration in order. Returns the versions applied.

    Each migration runs in ONE transaction, and its ledger row is written in that same
    transaction: a migration that half-applies does not exist, which is the same
    one-transaction rule MOS-EXEC-034 puts on the job row and the queue row.

    `planes` restricts the set to one product's migrations -- `("core",)` for the
    PACS-and-models deployable, `("core", "train")` for the model-preparation one.
    Omitted, every migration is applied, which is what a deployment running both
    products wants and what every existing caller gets. The ledger is shared either
    way: a database that later adds the training plane applies three more migrations
    and skips the eleven already recorded, which is ordinary forward migration and not
    a special case.
    """
    _ensure_ledger(conn)
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
    try:
        done = applied_versions(conn)
        if BASELINE_VERSION not in done:
            from medos.db.conn import SCHEMA_PATH

            digest = baseline_digest or (
                "sha256:" + hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
            )
            _record(conn, BASELINE_VERSION, digest, 0)
            done[BASELINE_VERSION] = digest

        applied: list[str] = []
        for mig in discover(directory, planes=planes):
            if mig.version in done:
                if done[mig.version] != mig.digest:
                    raise RuntimeError(
                        f"migration {mig.version} was applied with a different file "
                        f"digest ({done[mig.version][:14]}... on record, "
                        f"{mig.digest[:14]}... on disk). A migration is immutable once "
                        "it has run; add a new one."
                    )
                continue
            started = time.monotonic()
            with conn.transaction():
                conn.execute(mig.sql)  # type: ignore[arg-type]
                _record(
                    conn, mig.version, mig.digest,
                    int((time.monotonic() - started) * 1000),
                )
            applied.append(mig.version)
        return applied
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))


def set_role_password(
    conn: psycopg.Connection[Any], role: str, password: str
) -> None:
    """Give a migration-created role a password so it can actually log in.

    Credentials are deployment configuration, not schema: `0002_tenancy.up.sql` creates
    `medicalos_app` with `LOGIN NOBYPASSRLS` and no password, because a migration that
    hardcodes one puts it in git. The deployment (compose, the test harness, a hospital's
    secret store) supplies it here.

    `ALTER ROLE ... PASSWORD` takes no bind parameter, so the statement is composed with
    `psycopg.sql.Identifier`/`Literal` rather than by f-string -- the difference is that
    those two escape, and an f-string with a password containing a quote produces either
    a syntax error or an injection. The password is NEVER logged: CONTRACT.md section 11's
    rule about PHI has no exception that would make printing a credential acceptable.
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
        raise ValueError(f"refusing to ALTER a role with a suspicious name: {role!r}")
    conn.execute(
        sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )
