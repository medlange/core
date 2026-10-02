# SPDX-License-Identifier: Apache-2.0
"""Persistence for the weeks 1-2 slice: one DDL file, one queue port, one repository.

CONTRACT.md section 1 fixes the four files:

    db/
      schema.sql   # the ONLY DDL. No ORM models.
      conn.py      # connection helper
      queue.py     # JobQueue port + PostgresJobQueue driver
      repo.py      # job/result/event row access

Nothing in this package imports a connection or holds one at module scope
(CONTRACT.md section 11). `medos.db` may import `medos.core`; `medos.core` may not import
`medos.db` -- core is pure (CONTRACT.md section 1), and the one-way edge is what keeps a
capability from reaching the database (CONTRACT.md section 6).
"""

from medos.db.conn import (
    DEFAULT_DSN,
    SCHEMA_PATH,
    apply_schema,
    connect,
    dsn_from_env,
    schema_sql,
)
from medos.db.migrate import (
    BASELINE_VERSION,
    MIGRATIONS_DIR,
    Migration,
    applied_versions,
    apply_migrations,
    discover,
    set_role_password,
)
from medos.db.queue import (
    DEFAULT_SERVICE_ID,
    FailDecision,
    JobQueue,
    Lease,
    LeaseLost,
    PostgresJobQueue,
    QueueError,
    Reclaimed,
    make_worker_id,
    queue_name,
)
from medos.db.repo import (
    SLICE_TENANT_ID,
    STEP_PLAN,
    CreatedJob,
    DicomObjectRow,
    JobSpec,
    MeasurementRow,
    ResultRow,
    SeriesVerdict,
    append_event,
    complete_job_with_results,
    create_job_queued,
    get_job,
    job_view,
    list_events,
    list_results,
    new_public_job_id,
    new_trace_id,
    plan_steps,
    record_series_verdicts,
    save_result,
)
from medos.db.tenancy import (
    DEFAULT_TENANT_ID,
    TENANT_GUC,
    NoTenantContextError,
    TenantContextConflict,
    bind_current_tenant,
    bound_tenant,
    current_tenant,
    current_tenant_or_none,
    default_tenant_id,
    platform_tx,
    reset_current_tenant,
    serving_tenants,
    tenant_context,
    tenant_tx,
)

__all__ = [
    # tenancy -- the chokepoint (MOS-SEC-075, MOS-STORE-227)
    "TENANT_GUC",
    "DEFAULT_TENANT_ID",
    "NoTenantContextError",
    "TenantContextConflict",
    "tenant_tx",
    "platform_tx",
    "tenant_context",
    "bind_current_tenant",
    "reset_current_tenant",
    "current_tenant",
    "current_tenant_or_none",
    "bound_tenant",
    "default_tenant_id",
    "serving_tenants",
    # migrations (chapter 12 section 12.15)
    "MIGRATIONS_DIR",
    "BASELINE_VERSION",
    "Migration",
    "discover",
    "applied_versions",
    "apply_migrations",
    "set_role_password",
    # conn
    "SCHEMA_PATH",
    "DEFAULT_DSN",
    "connect",
    "dsn_from_env",
    "apply_schema",
    "schema_sql",
    # queue
    "JobQueue",
    "PostgresJobQueue",
    "Lease",
    "FailDecision",
    "Reclaimed",
    "LeaseLost",
    "QueueError",
    "queue_name",
    "make_worker_id",
    "DEFAULT_SERVICE_ID",
    # repo
    "SLICE_TENANT_ID",
    "STEP_PLAN",
    "JobSpec",
    "CreatedJob",
    "SeriesVerdict",
    "MeasurementRow",
    "DicomObjectRow",
    "ResultRow",
    "new_public_job_id",
    "new_trace_id",
    "create_job_queued",
    "get_job",
    "job_view",
    "plan_steps",
    "record_series_verdicts",
    "append_event",
    "list_events",
    "save_result",
    "complete_job_with_results",
    "list_results",
]
