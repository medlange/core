# SPDX-License-Identifier: Apache-2.0
"""The suite's declaration of what it depends on must stay honest.

WHY THIS FILE EXISTS
--------------------
`--require-stack` turns an unreachable dependency into a failure. That is only worth
anything for a dependency somebody classified. An adversarial pass over this repository
stopped six of the ten compose services -- including `medos-gateway`, which
docker-compose.yml makes the worker's ONLY route to DICOM -- and ran:

    pytest tests/unit tests/integration/test_api.py tests/integration/test_queue.py \
        -q --require-stack
    -> 233 passed, exit 0

Nothing skipped, so nothing failed, so the run was green on a platform that could not
fetch a single instance. `tests/_support/stack.py` now probes what each suite declares.

The hole that fix leaves behind is the one this file closes: the NEXT service added to
docker-compose.yml is unclassified again, and nobody finds out until an incident. These
tests fail the moment a compose service is neither probed nor explicitly excused, so the
decision has to be made when the service is added rather than after it breaks.

No container is needed for any of this: it reads two files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._support import stack

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_docker_compose_is_parsed_into_the_service_names_we_expect() -> None:
    """The parser is the load-bearing part of every assertion below.

    A parser that silently returns [] would make the coverage test below vacuously pass,
    which is the same shape of bug as the one this module exists to prevent.
    """
    names = stack.compose_service_names()
    assert len(names) >= 8, f"suspiciously few services parsed: {names}"
    for expected in ("postgres", "orthanc", "medos-api", "medos-worker", "web"):
        assert expected in names, f"{expected} missing from {names}"
    assert "volumes" not in names and "networks" not in names


def test_every_compose_service_is_either_probed_or_explicitly_excused() -> None:
    """No third option. "Nobody thought about it" is what the incidents were made of."""
    probed = {stack.PROBE_SERVICE.get(key, key) for key in stack.PROBES}
    unclassified = [
        name
        for name in stack.compose_service_names()
        if name not in probed and name not in stack.UNCOVERED
    ]
    assert unclassified == [], (
        f"these docker-compose.yml services are in neither tests._support.stack.PROBES "
        f"nor UNCOVERED: {unclassified}. Add a probe if the suite depends on the service, "
        f"or add it to UNCOVERED with the reason it cannot be one."
    )


def test_every_excuse_names_a_real_service_and_gives_a_reason() -> None:
    """A stale excuse is worse than none: it looks like a decision and is not."""
    names = set(stack.compose_service_names())
    for service, reason in stack.UNCOVERED.items():
        assert service in names, (
            f"UNCOVERED names {service!r}, which is not a service in docker-compose.yml "
            "any more. Remove the entry."
        )
        assert len(reason) > 40, f"UNCOVERED[{service!r}] is not a reason, it is a label"


def test_every_probe_key_a_suite_declares_actually_has_a_probe() -> None:
    """A typo in SUITE_DEPENDENCIES would otherwise silently probe nothing."""
    for suite, keys in stack.SUITE_DEPENDENCIES.items():
        for key in keys:
            assert key in stack.PROBES, f"{suite} declares {key!r}, which has no probe"


def test_every_suite_path_that_is_declared_exists() -> None:
    """A renamed directory would otherwise turn its whole declaration off silently."""
    for suite in stack.SUITE_DEPENDENCIES:
        assert (REPO_ROOT / suite).exists(), (
            f"SUITE_DEPENDENCIES declares {suite!r}, which is not in the tree. A suite "
            "that has moved silently stops declaring its dependencies."
        )


def test_unit_tests_declare_no_infrastructure() -> None:
    """The CI unit job runs with --require-stack and no containers at all.

    This is the assertion that keeps that job honest in both directions: a unit test that
    needs a container is in the wrong directory, and a declaration added here would make
    the unit job require a stack it is not given.
    """
    assert stack.SUITE_DEPENDENCIES["tests/unit"] == ()


def test_every_directory_under_tests_declares_its_dependencies() -> None:
    """A new suite directory inherits nothing, so it must say what it needs.

    Without this, `tests/gate/` could be added tomorrow, depend on the whole stack, and
    be probed for nothing -- back to square one.
    """
    undeclared = [
        str(child.relative_to(REPO_ROOT)).replace("\\", "/")
        for child in sorted((REPO_ROOT / "tests").iterdir())
        if child.is_dir()
        and not child.name.startswith(("_", "."))
        and any(child.glob("test_*.py"))
        and f"tests/{child.name}" not in stack.SUITE_DEPENDENCIES
    ]
    assert undeclared == [], (
        f"these test directories declare no dependencies in "
        f"tests._support.stack.SUITE_DEPENDENCIES: {undeclared}. Declare () if they "
        "genuinely need nothing."
    )


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["tests/unit/test_inference.py"], set()),
        (["tests/integration/test_api.py"], {"postgres"}),
        # `orthanc-rest` + `docker`: this module's teardown deletes studies through
        # `docker exec medos-orthanc python3`, because DICOMweb has no DELETE verb.
        (
            ["tests/integration/test_dicomweb.py"],
            {"postgres", "orthanc", "orthanc-rest", "docker"},
        ),
        # `docker` rides along with `orthanc-rest`: since register entry 70 the PACS's
        # native REST API is reached by executing INSIDE `medos-orthanc`, because
        # MOS-DATA-006 leaves no host route to it. A suite that needs the native API
        # therefore needs the docker CLI, and a dependency a suite genuinely has belongs in
        # the preflight rather than in a skip taken part-way through the run.
        (
            ["tests/integration/test_worker.py"],
            {"postgres", "orthanc", "orthanc-rest", "docker"},
        ),
    ],
)
def test_declaration_is_resolved_at_module_granularity(
    paths: list[str], expected: set[str]
) -> None:
    """Longest prefix wins.

    Module granularity is not a nicety: declaring Orthanc for the whole integration
    directory would make `pytest tests/integration/test_queue.py --require-stack` red on
    a machine with no PACS, and a switch that is red on a correct run gets deleted.
    """
    assert set(stack.dependencies_for_paths(paths)) == expected


def test_a_dependency_with_no_probe_reports_down_rather_than_up() -> None:
    """"I could not tell" must never be rendered as "it is up"."""
    ready, detail = stack.probe("no-such-dependency")
    assert ready is False
    assert "no probe" in detail


class _Resp:
    """Just enough of `requests.Response` to pin the shape rules."""

    def __init__(self, status: int, ctype: str, body: bytes) -> None:
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self.content = body
        self.text = body.decode("utf-8", "replace")

    def json(self) -> object:
        import json

        return json.loads(self.content)


# The body the origin USED to return for every unknown path, including
# /dicom-web/studies. This is not hypothetical: it was measured on the development stack
# while writing this file, and it is what made a 200-means-healthy probe call that origin
# a working PACS.
_SPA_FALLBACK = _Resp(200, "text/html", b"<!doctype html><html lang=en>...</html>")


def test_a_2xx_html_fallback_is_not_accepted_as_any_service() -> None:
    """The single most important rule in the probe table: a status code is not proof.

    An nginx with an SPA fallback answers 200 text/html for every path that does not
    exist. A probe that checks `status < 400` therefore reports EVERY service hosted
    behind such an origin as healthy -- including one that is not there at all.
    """
    for shape in ("json", "dicom-json", "javascript"):
        with pytest.raises(stack.Unready):
            stack._expect(_SPA_FALLBACK, "http://origin/whatever", shape=shape)


def test_an_empty_archive_is_ready_but_an_html_page_is_not() -> None:
    """PS3.18 8.3.4.3: 204 from QIDO-RS means no matches, which is a working PACS."""
    assert stack._expect(_Resp(204, "", b""), "u", shape="dicom-json") == []
    empty = _Resp(200, "application/dicom+json", b"[]")
    assert stack._expect(empty, "u", shape="dicom-json") == []
    with pytest.raises(stack.Unready):
        # A JSON object where QIDO-RS returns an array: answering, but not the PACS.
        stack._expect(_Resp(200, "application/json", b'{"ok":1}'), "u", shape="dicom-json")


def test_a_5xx_is_reported_with_the_status_that_produced_it() -> None:
    """The second incident was `medos-api -> HTTP 503` and the run exited 0."""
    with pytest.raises(stack.Unready, match="503"):
        starting = _Resp(503, "application/json", b'{"status":"starting"}')
        stack._expect(starting, "u", shape="json")


def test_the_api_probe_reads_readiness_and_not_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/healthz deliberately does not touch the database. That is the point of it.

    docker-compose.yml uses /healthz for `condition: service_healthy`, because an
    orchestrator must not restart a process that is working. It is exactly that property
    that makes liveness worthless as a test-suite gate: the platform can be unable to
    serve one request while every liveness probe on it stays green. The second incident
    was `medos-api -> HTTP 503` on /readyz, and the suite exited 0.
    """
    seen: list[str] = []

    def fake_get(url: str, *, accept: str | None = None) -> _Resp:
        seen.append(url)
        return _Resp(200, "application/json", b'{"status":"ready","version":"0.1.0"}')

    monkeypatch.setattr(stack, "_get", fake_get)
    stack._probe_api()
    assert seen and seen[0].endswith("/readyz"), seen

    # A 200 that does not claim readiness is not readiness.
    def not_ready(url: str, *, accept: str | None = None) -> _Resp:
        return _Resp(200, "application/json", b'{"status":"starting"}')

    monkeypatch.setattr(stack, "_get", not_ready)
    with pytest.raises(stack.Unready, match="answering, not ready"):
        stack._probe_api()
