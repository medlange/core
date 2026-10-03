# SPDX-License-Identifier: Apache-2.0
"""`GET /api/v1/capabilities`: the dependency edges a client needs to submit a closure.

WHY THIS ROUTE EXISTS
---------------------
CONTRACT.md section 7 makes `emphysema_laa` depend on `lung_segmentation`'s mask, and
`medos.worker.steps.resolve_capability_order` refuses a job that requests the dependent
capability without its dependency -- pulling it in silently would write a SEG and a
results row the caller never asked for, and `jobs.capability_ids` would disagree with the
`results` table. The refusal is correct server-side; the defect was that no surface told
a client the edge existed. Measured live on 2026-10-03: the viewer's analyze dialog
offered `emphysema_laa` as a standalone choice, every such job FAILED with
`capability_resolution_failed`, and the reader saw a dead end. The route is the surface
telling them, and `viewer/src/ui/ai-dialog.js` reads it to submit the closure.

WHAT IS ASSERTED
----------------
The handler answers from the SAME resolver the admission check and the worker use
(`medos.capabilities.providers`), so the edges a client builds a closure from can never
disagree with the order the worker enforces. The rows carry the capability's OWN
`depends_on` declaration, sorted for a stable body. And the route is gated on `job.read`:
the set of what a deployment runs is not public cataloguing.

No database, no HTTP server: the handler is called with the `Request` it would receive,
the same way `tests/unit/test_openapi_document.py` drives `create_app` without a socket.

Spec: CONTRACT.md section 7, MOS-API-005, MOS-SEC-027.
"""

from __future__ import annotations

from typing import Any

from medos.api.routes_jobs import CapabilitiesResponse, list_capabilities
from medos.security.authn import Principal
from starlette.requests import Request

_SCOPE: dict[str, Any] = {
    "type": "http",
    "method": "GET",
    "path": "/api/v1/capabilities",
    "headers": [],
    "query_string": b"",
}


def _request(scope: tuple[str, ...] = ("job.read",)) -> Request:
    request = Request(_SCOPE)
    request.state.principal = Principal(
        kind="service_account",
        principal_id="svc-test",
        tenant_id="t-test",
        scope=scope,
        credential_key_id="key-test",
    )
    return request


def _rows(response: CapabilitiesResponse) -> dict[str, list[str]]:
    return {row.capability_id: row.depends_on for row in response.capabilities}


def test_the_served_set_with_its_dependency_edges() -> None:
    """`lung_segmentation` stands alone; `emphysema_laa` names its dependency."""
    answer = list_capabilities(_request())
    rows = _rows(answer)
    assert "lung_segmentation" in rows, "the platform base capability is not served"
    assert rows["lung_segmentation"] == [], (
        "lung_segmentation declares no dependency; a non-empty edge here would make "
        "every closure submit it spuriously"
    )
    assert rows.get("emphysema_laa") == ["lung_segmentation"], (
        "CONTRACT.md section 7's edge is the whole reason this route exists; without "
        "it the viewer cannot build the closure and the 2026-10-03 failure returns"
    )


def test_rows_are_stable_sorted_and_closed() -> None:
    answer = list_capabilities(_request())
    ids = [row.capability_id for row in answer.capabilities]
    assert ids == sorted(ids), "the body is part of the client's cache key; keep it stable"
    for row in answer.capabilities:
        assert row.depends_on == sorted(row.depends_on)
        for dep in row.depends_on:
            assert dep in ids, f"{row.capability_id} depends on {dep}, which it does not serve"


def test_the_answer_comes_from_the_deployment_resolver(monkeypatch) -> None:
    """The handler reads `capability_providers.resolve()`, not the platform singleton --
    a deployment composed with a provider must see the provider's edges here too, the
    same answer `POST /jobs` admits and the worker executes."""
    from types import SimpleNamespace

    from medos.capabilities import providers

    deployed = {
        "zeta": SimpleNamespace(depends_on=("alpha",)),
        "alpha": SimpleNamespace(depends_on=()),
    }
    # `routes_jobs` aliases this same module object (`from ... import providers as
    # capability_providers`), so patching the module's attribute patches the call the
    # route makes -- and the test cannot pass by reading a second copy.
    monkeypatch.setattr(
        providers,
        "resolve",
        lambda *a, **k: SimpleNamespace(registry=deployed),
    )
    rows = _rows(list_capabilities(_request()))
    assert rows == {"alpha": [], "zeta": ["alpha"]}, (
        "the route served something other than the resolver's registry"
    )


def test_a_credential_without_job_read_is_refused() -> None:
    answer = list_capabilities(_request(scope=("job.create",)))
    assert answer.status_code == 403, (
        "what a deployment runs is not public cataloguing; the set and its edges are "
        "gated on job.read like every other job-plane read"
    )
    body = answer.body.decode() if hasattr(answer, "body") else str(answer)
    assert "job.read" in body


def test_response_model_round_trips() -> None:
    answer = list_capabilities(_request())
    jsonable = CapabilitiesResponse.model_validate(answer).model_dump(mode="json")
    assert set(jsonable) == {"capabilities"}
    assert all(set(row) == {"capability_id", "depends_on"} for row in jsonable["capabilities"])
