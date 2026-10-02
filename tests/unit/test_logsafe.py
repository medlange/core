# SPDX-License-Identifier: Apache-2.0
"""`medos.security.logsafe`: the two controls that keep P2 out of the log stream.

WHY A UNIT FILE AND NOT ONLY THE GATEWAY INTEGRATION TESTS
-----------------------------------------------------------
`tests/integration/test_gateway.py` proves the OUTCOME -- no DICOM UID reaches the log
stream on any of the Gateway's routes -- and it needs Postgres and a stub PACS to do it.
This file proves the PROPERTIES of the two controls themselves, in-process and with no
containers, because three of them are invisible from the outcome:

  * the allowlist is CLOSED, so a route parameter nobody classified is dropped rather
    than logged (`MOS-SEC-107`'s "cannot express the unsafe case", in Python);
  * `MOS-SEC-110`'s shape filter is scoped to third-party loggers and MUST NOT touch a
    `medos.*` record -- the requirement says it "MUST NOT be the primary control", and a
    filter that scrubbed our own records would hide the next defect from the integration
    test above;
  * an IPv4 literal is not a UID, and `MOS-SEC-146` requires `source_ip` to be recorded.

Spec: MOS-SEC-102, MOS-SEC-105, MOS-SEC-106, MOS-SEC-107, MOS-SEC-110, MOS-SEC-117,
MOS-SEC-146, MOS-DATA-007, chapter 3 acceptance check 14.
No containers: this file constructs `LogRecord`s and ASGI scopes and reads nothing else.
"""

from __future__ import annotations

import logging

from medos.sdk.canonical import study_ref
from medos.security.logsafe import (
    LOGGABLE_PATH_PARAMS,
    REDACTED_P2,
    UNMATCHED_ROUTE,
    install_uid_shape_redaction,
    redact_p2,
    route_fields,
)

TENANT = "018f0a00-0000-7000-8000-0000000000aa"
STUDY = "1.3.6.1.4.1.14519.5.2.1.7014.4598.106943890850011666503487579262"
SERIES = "2.25.9001"
SOP = "2.25.9002"


class _Route:
    """The three members `route_fields` reads off a Starlette route."""

    def __init__(self, template: str, params: dict[str, str] | None) -> None:
        self.path_format = template
        self._params = params

    def matches(self, _scope: dict[str, object]) -> tuple[object, dict[str, object]]:
        from starlette.routing import Match

        if self._params is None:
            return Match.NONE, {}
        return Match.FULL, {"path_params": dict(self._params)}


class _App:
    def __init__(self, *routes: _Route) -> None:
        self.routes = list(routes)


def _scope(app: _App, params: dict[str, str]) -> dict[str, object]:
    """A scope as it looks AFTER the router ran: `path_params` already written on it."""
    return {"type": "http", "method": "GET", "app": app, "path_params": dict(params)}


# =====================================================================================
# The primary control: MOS-SEC-117's template, MOS-SEC-105's allowlist, MOS-SEC-106's ref
# =====================================================================================
def test_the_route_template_replaces_the_resolved_path() -> None:
    """MOS-SEC-117, applied to the surface that shares MOS-SEC-105's row with span names.

    "Span names MUST be route templates or fixed operation names.
    `GET /api/v1/studies/1.2.840.113619.2.55.3` MUST be recorded as
    `GET /api/v1/studies/{study_id}`."
    """
    template = "/dicomweb/{t}/studies/{st}/series/{se}/instances/{sop}"
    params = {"t": TENANT, "st": STUDY, "se": SERIES, "sop": SOP}
    fields = route_fields(_scope(_App(_Route(template, params)), params))
    assert fields["route"] == template


def test_no_uid_valued_path_parameter_survives_into_a_record() -> None:
    """MOS-SEC-102 classifies all three UID kinds as P2; MOS-SEC-105 forbids P2 in logs."""
    template = "/dicomweb/{t}/studies/{st}/series/{se}/instances/{sop}/frames/{f}"
    params = {"t": TENANT, "st": STUDY, "se": SERIES, "sop": SOP, "f": "1"}
    fields = route_fields(_scope(_App(_Route(template, params)), params))
    rendered = repr(fields)
    for uid in (STUDY, SERIES, SOP):
        assert uid not in rendered, f"a P2 identifier reached a log field: {uid[:9]}..."


def test_the_allowlist_is_closed_so_an_unclassified_parameter_is_dropped() -> None:
    """MOS-SEC-107's shape: the control cannot express the unsafe case.

    A denylist admits every parameter nobody thought about. This asserts the opposite
    default, which is the only property that makes the fix survive the NEXT route: a
    parameter whose name is not a key of `LOGGABLE_PATH_PARAMS` contributes nothing,
    whatever it is called and whatever it holds.
    """
    params = {"t": TENANT, "accession_number": "ACC-000123", "mystery": "whatever"}
    fields = route_fields(_scope(_App(_Route("/x/{t}", params)), params))
    assert fields["tenant_id"] == TENANT
    assert "ACC-000123" not in repr(fields)
    assert "whatever" not in repr(fields)
    assert set(fields) <= {"route", "study_ref", *LOGGABLE_PATH_PARAMS.values()}


def test_a_study_uid_is_replaced_by_study_ref_and_not_merely_removed() -> None:
    """MOS-SEC-106: "MUST be replaced by `study_ref` (P1)", not deleted.

    Deleting it would satisfy the prohibition and destroy the operator's ability to ask
    "which requests touched this study", which is the question an access log exists for.
    """
    params = {"t": TENANT, "st": STUDY}
    fields = route_fields(_scope(_App(_Route("/dicomweb/{t}/studies/{st}", params)), params))
    assert fields["study_ref"] == study_ref(TENANT, STUDY)
    assert fields["study_ref"].startswith("sr_")


def test_study_ref_is_tenant_scoped_so_two_tenants_never_share_one() -> None:
    """MOS-SEC-103's unlinkability property, as it reaches the log stream.

    The same study seen by two tenants must produce two refs, or a ref leaked from one
    tenant's log index would say something about another tenant's holdings.
    """
    other = "018f0a00-0000-7000-8000-0000000000bb"
    a = route_fields(_scope(_App(_Route("/{t}/{st}", {})), {"t": TENANT, "st": STUDY}))
    b = route_fields(_scope(_App(_Route("/{t}/{st}", {})), {"t": other, "st": STUDY}))
    assert a["study_ref"] != b["study_ref"]


def test_an_unmatched_path_yields_no_path_at_all() -> None:
    """A 404's path is entirely caller-controlled, so none of it is echoed into a record.

    `MOS-DATA-008` gives the Gateway no catch-all, so every unmatched request lands here,
    and the URL that produced it is a string an unauthenticated caller chose.
    """
    scope = {"type": "http", "method": "GET", "app": _App(_Route("/nope", None))}
    fields = route_fields(scope)
    assert fields == {"route": UNMATCHED_ROUTE}


# =====================================================================================
# The secondary control: MOS-SEC-110, and the boundary that keeps it secondary
# =====================================================================================
def test_the_shape_filter_catches_the_three_component_derived_uid_root() -> None:
    """Stricter than MOS-SEC-110's literal shape, deliberately.

    The requirement names `^\\d(\\.\\d+){3,}$`, which needs four components. `2.25.9001`
    is a legal ISO/IEC 9834-8 derived UID with three, and the Gateway suite uses exactly
    that form, so the filter would have passed it straight through.
    """
    assert redact_p2(f"GET /studies/{SERIES}/x") == f"GET /studies/{REDACTED_P2}/x"
    assert redact_p2(STUDY) == REDACTED_P2


def test_the_shape_filter_leaves_an_ipv4_address_alone() -> None:
    """`0.0.0.0` satisfies MOS-SEC-110's shape and is not a UID.

    `MOS-SEC-146` requires the audit trail to carry `source_ip`; a filter that erased
    addresses would take a bind address out of every start-up line and a peer address out
    of every client log for no gain.
    """
    assert redact_p2("Uvicorn running on http://0.0.0.0:8043") == (
        "Uvicorn running on http://0.0.0.0:8043"
    )
    assert redact_p2("from 10.1.2.3") == "from 10.1.2.3"


def test_the_shape_filter_rewrites_a_third_party_record_and_not_a_medos_one() -> None:
    """MOS-SEC-110: "It is defence in depth and MUST NOT be the primary control."

    THE BOUNDARY IS THE POINT. If this scrubbed `medos.*` records, the next call site in
    this repository that logged a raw path would be invisible to
    `tests/integration/test_gateway.py::test_no_dicom_uid_reaches_the_gateway_log_stream`
    and the control that actually holds -- `route_fields` -- would rot untested while the
    suite stayed green.
    """
    install_uid_shape_redaction()
    third_party = logging.getLogger("httpx")
    ours = logging.getLogger("medos.gateway")

    outside = third_party.makeRecord(
        "httpx", logging.INFO, __file__, 1,
        'HTTP Request: %s %s', (f"/studies/{STUDY}", "200"), None,
    )
    inside = ours.makeRecord(
        "medos.gateway", logging.INFO, __file__, 1,
        'upstream %s', (f"/studies/{STUDY}",), None,
    )

    assert STUDY not in outside.getMessage()
    assert REDACTED_P2 in outside.getMessage()
    assert STUDY in inside.getMessage(), (
        "the shape filter reached a medos.* record; it would then be the primary control "
        "and would hide the next raw-path defect (MOS-SEC-110)"
    )


def test_installing_the_shape_filter_twice_does_not_stack_it() -> None:
    """Both app factories call it, and a process may host both. Idempotent or it nests."""
    install_uid_shape_redaction()
    before = logging.getLogRecordFactory()
    install_uid_shape_redaction()
    assert logging.getLogRecordFactory() is before
