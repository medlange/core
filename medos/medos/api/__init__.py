# SPDX-License-Identifier: Apache-2.0
"""The thin HTTP surface for the weeks 1-2 vertical slice.

CONTRACT.md section 0: "The Go control plane replaces the HTTP surface at weeks 3-5
(`MOS-REL-084`). Therefore the HTTP layer MUST stay thin -- no business logic in
handlers."

Everything in this package validates its input, calls `medos.db.repo` / `medos.db.queue`,
and shapes the response. No module here derives a UID, decides a job state, opens a
transaction of its own, or talks to a PACS. The one rule worth restating at the package
level, because violating it is a patient-safety defect rather than a style problem
(CONTRACT.md section 9):

    The client's `Idempotency-Key` header MUST NOT feed DICOM UID derivation.

It reaches exactly one field, `JobSpec.request_idempotency_key`, which is stored for echo
and is absent from `derive_idempotency_key`'s keyword-only material set by construction
(MOS-EXEC-054, MOS-API-029). `tests/integration/test_api.py` asserts it.

Why the connection plumbing lives HERE and not in a `deps.py`
------------------------------------------------------------
CONTRACT.md section 1 fixes `medos/medos/api/` as four files. These two functions are the
app's injected collaborator lookup and they are needed by both route modules, so putting
them in either one would make the other import its sibling. They are three lines and
they belong to the package, so the package's `__init__` is where they go rather than a
fifth module the layout does not name.

CONTRACT.md section 11: "No global mutable state. Pass the connection; do not import a
singleton." There is therefore no module-level connection and no module-level pool here.
`create_app(connect=...)` stores a connection FACTORY on `app.state`; every request opens
its own connection and closes it. A factory injected at app construction is not global
state -- `MOS-REL-046` requires that a test be able to construct two independent
instances of any component in one process, and two `create_app()` calls with different
DSNs do exactly that.

Spec: MOS-API-001, MOS-API-035, MOS-API-047, MOS-API-059, MOS-REL-046, MOS-REL-084,
CONTRACT.md sections 0, 9 and 11.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import psycopg
from starlette.requests import Request

__all__ = ["create_app", "connection_factory", "db_connection"]


def connection_factory(request: Request) -> Callable[..., psycopg.Connection[Any]]:
    """The connection opener `create_app` was constructed with.

    Accepts `medos.db.conn.connect`'s keyword arguments; `routes_events` passes
    `autocommit=True` for its long-lived streaming connection.
    """
    factory = getattr(request.app.state, "medos_connect", None)
    if factory is None:  # pragma: no cover - only reachable on a hand-built app
        raise RuntimeError(
            "app.state.medos_connect is unset; build the app with medos.api.app.create_app"
        )
    return factory  # type: ignore[no-any-return]


def db_connection(request: Request) -> Iterator[psycopg.Connection[Any]]:
    """One caller-owned connection per request, closed when the request ends.

    No pool, because this surface is replaced by Go at weeks 3-5 (`MOS-REL-084`) and a
    pool here is work that gets deleted. `close()` rolls back anything a read left open;
    the write helpers in `medos.db.repo` commit themselves via `conn.transaction()`.
    """
    conn = connection_factory(request)()
    try:
        yield conn
    finally:
        conn.close()


def __getattr__(name: str) -> Any:  # pragma: no cover - import convenience only
    # Lazy so that importing this package does not pull FastAPI into a process that only
    # wants `medos.api.problems` (the worker does, for the wire shape of a rejection),
    # and so that `app.py` can import this module without a cycle.
    if name == "create_app":
        from medos.api.app import create_app

        return create_app
    raise AttributeError(name)
